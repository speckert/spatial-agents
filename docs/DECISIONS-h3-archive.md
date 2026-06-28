# DECISIONS — H3 snapshot archive (`data/tiles/h3/`): write-only? + retention

**Date:** 2026-06-28
**Mode:** Read-and-report only. No code changed, no files deleted, no jobs run.
**Question:** Does anything in the codebase ever **read these snapshot files back**, or is
the directory write-only? And what is the retention fix?

---

## TL;DR

**The directory is effectively WRITE-ONLY.** No code path reads a snapshot's **contents**
back for any functional purpose. Every "reader" found is one of: dead code (zero callers),
a stats endpoint that only `stat()`s file sizes (never opens contents), or an unwired /
cloud-only / HTTP-latent path that no consumer actually invokes. The live public map does
not touch it (already confirmed via access logs + browser capture; reconfirmed here — the
only `/tiles/` references in `docs/` are documentation prose).

**Therefore:** the archive is being generated for a consumer that does not exist. It is
safe to stop writing and safe to delete. There is **no eviction logic of any kind** on this
tree — that is the root cause of the unbounded growth that filled the disk.

**Cheapest immediate fix:** stop the 60-second tile-rebuild loop from persisting (or gate it
behind a default-off flag), and stop persisting **res-5 and res-6** specifically — they are
the only unbounded-by-time consumers. See [Recommendation](#8-recommendation).

---

## 1. The WRITE path

A single continuous in-process loop, no scheduler, not lazy-on-request.

- **`spatial_agents/main.py:128-136`** — `tile_rebuild_loop()`: an `asyncio` task that wakes
  **every 60 seconds** for the life of the server.
  ```python
  async def tile_rebuild_loop() -> None:
      while True:
          await asyncio.sleep(60)  # Rebuild tiles every 60 seconds
          try:
              on_new_data_batch()
  ```
- **`spatial_agents/main.py:116-121`** — `on_new_data_batch()` pulls the current in-memory
  feed state and rebuilds every resolution:
  ```python
  vessels = feed_manager.get_latest_vessels()
  aircraft = feed_manager.get_latest_aircraft()
  if vessels or aircraft:
      tile_builder.build_all_resolutions(vessels, aircraft)
  ```
- **`spatial_agents/spatial/tile_builder.py:169-178`** — `build_all_resolutions()` loops
  `config.tiling.resolutions` → `build_tiles_for_records()` (`:122`) → `build_tile()` (`:64`),
  which writes the file at **`tile_builder.py:180-182`**:
  ```python
  def _tile_path(self, cell_id, resolution, temporal_bin):
      return self._output_dir / str(resolution) / cell_id / f"{temporal_bin}.json"
  ```

**Resolutions written:** `[3, 4, 5, 6, 7]` — `config.py:155-158` (`TilingConfig.resolutions`).
Confirmed on disk: dirs `3,4,5,6,7` all present.

**Why it accumulates** — the filename is a *temporal bin key*, and a new key = a new file.
The bin size per resolution (`config.py:159-162`, `temporal_bins.py`) determines the rate:

| res | bin size (`temporal_bins`) | filename pattern | growth behavior |
|----|----|----|----|
| 3 | `1day`  | `20260502T000000.json` | +1 file/cell/**day** |
| 4 | `1hour` | `20260502T070000.json` | +1 file/cell/**hour** |
| 5 | `5min`  | `20260502T070500.json` | +1 file/cell/**5 min** ← biggest time-driven grower |
| 6 | `1min`  | `20260628T065600.json` | +1 file/cell/**minute** ← fastest grower |
| 7 | `live`  | `live.json` (constant key) | **overwritten in place — no time accumulation** |

`bin_key()` (`temporal_bins.py:39-57`) returns the literal string `"live"` for res-7, so every
res-7 rebuild **overwrites the same `live.json`** for a given cell. Res-7 therefore grows only
with the number of *distinct cells ever occupied*, not with time.

---

## 2. The READ path — the central question

**Searched** for every way the tree could be read: `rglob`, `glob`, `open(`, `read_bytes`,
`read_text`, `get_tile`, `list_tiles`, `walk`, `iterdir`, API routes, causal/analytics
modules, deploy utilities, tests, and the client HTML/JS in `docs/`. Findings:

| Candidate reader | File:line | Reads **contents**? | Wired / reachable? | Verdict |
|---|---|---|---|---|
| `TileBuilder.get_tile()` | `tile_builder.py:184-194` | Yes (would `orjson.loads`) | **Zero callers anywhere** | **Dead code** |
| `TileBuilder.list_tiles()` | `tile_builder.py:196-203` | No — lists paths only | called only by `tile_stats()` | metadata only |
| `TileBuilder.tile_stats()` | `tile_builder.py:205-217` | No — `os.path.getsize()` only | → `GET /api/tiles/stats` | **counts/sizes only, never opens a file** |
| `S3TileSync.sync_all()` | `deploy/cloud_s3.py:91-100` | Reads to upload | **Zero callers** + cloud-mode only (this is `local_mac`) | **Unwired, dead in this deployment** |
| `StaticFiles` mount `/tiles` | `serving/app.py:72-76` | Serves over HTTP | mounted, but **no client requests it** | latent, unused (access logs null) |
| `FileExporter.list_exports()` | `serving/file_exporter.py:84-91` | Lists a *different* dir | operates on the export dir, not `tiles/h3`; also unused | not this tree |
| `docs/*.html` references to `/tiles/` | `architecture.html`, `spatial-tiling.html` | — | **prose / API tables only**, no `fetch()` | documentation, not a reader |
| `is_bin_expired()` (would enable eviction) | `temporal_bins.py:115-121` | — | **Zero callers** | **Dead code** |

**Conclusion: there is no functional reader of snapshot contents.** The only code that opens a
tile is `get_tile()`, which nothing calls. The only thing that touches the tree at runtime is
`/api/tiles/stats`, and it only sums file sizes. The S3 sync that *would* read-to-upload is
never called and is cloud-only anyway. The HTTP mount is reachable in principle but no client
(map or otherwise) fetches it.

---

## 3. Computed vs. fetched (WRITE-path data source)

Snapshot **contents** are derived from live upstream feeds held in memory by `FeedManager`,
not from anything on disk:

- **Aircraft (ADS-B):** OpenSky Network REST API, OAuth2 client-credentials
  (`ingest/adsb_parser.py`; endpoint `config.py:135-150`, poll default 45 s). Token-limited,
  rate-limited free tier.
- **Vessels (AIS):** aisstream.io WebSocket (`ingest/aisstream_client.py`; endpoint
  `config.py:127-134`), API-key auth, free tier.

Implication for deletion: the data is **re-derivable going forward** (the feeds always give
"now"), but **past snapshots are NOT recoverable** — the feeds do not serve history. Deleting
old snapshots is a permanent loss of that history. (Per the brief, historical replay is not a
current requirement; if it ever becomes one it should be designed — see
[Future design](#future-design-note).)

A large fraction of the existing history is for **regions that are no longer active anyway**:
sample tiles resolve to São Paulo, Brazil (res-5 `85a810af…`, May 2) and the Strait of Hormuz
near Dubai (res-7 `8743ac69…`, generated **2026-04-10**, never rewritten since). Current active
regions are San Francisco + Chicago (`config.py:62`). So much of the tree is orphaned snapshots
from dropped regions that will never be rewritten *or* read.

---

## 4. Eviction / retention — none exists

No TTL, max-size, LRU, age-based cleanup, or rotation touches this tree. The eviction logic
that *does* exist is all **in-memory feed state**, unrelated to disk:
- `feed_manager.py:470-489` `_vessel_cleanup_loop()` — evicts vessels not seen in 8 h (RAM).
- `feed_manager.py:444-455` — evicts aircraft not seen in 10 min (RAM).

The one helper that *could* drive disk eviction — `is_bin_expired()` (`temporal_bins.py:115`)
— **has zero callers**. **This absence of any disk retention is the root cause of unbounded
growth.**

---

## 5. Per-resolution: written-and-read vs. written-and-never-read

**Every resolution is written and never read back.** None of res 3–7 has a content consumer.
The distinction that matters for the fix is *growth shape*, measured on disk today
(`du -sh`, 2026-06-28):

| res | size on disk | cell dirs | bin | growth | notes |
|----|----|----|----|----|----|
| 3 | **25 M** | 244 | daily | slow | negligible |
| 4 | **515 M** | 887 | hourly | moderate | bounded-ish |
| 5 | **8.5 G** | 3,885 | 5-min | **unbounded w/ time** | current top consumer (since May 2) |
| 6 | **142 M** | 1,242 | 1-min | **unbounded w/ time, fastest** | only today's files — recreated after the 35 G delete |
| 7 | **233 M** | 57,087 | live | bounded by #cells | one `live.json`/cell, overwritten; mostly stale orphans |
| **total** | **~9.4 G** | | | | matches `du -sh` project total |

Key points:
- **res-5 (8.5 G)** is now the dominant consumer and grows forever at 1 file/cell/5-min.
- **res-6** held the ~35 G that filled the disk; at 1-min bins it grows **5× faster than res-5**
  and will re-dominate within weeks if left writing (142 M in a single day so far).
- **res-7** is large in *file count* (57 k cells) but is **self-limiting** — `live.json` is
  overwritten, so it does not grow with time, only with geographic spread. Many of its files are
  stale (April) orphans from dropped regions.
- res-3/res-4 are comparatively cheap.

**Pure waste, prime to stop:** res-5 and res-6 (written, never read, unbounded). res-7 is also
never read but is self-capping, so lower priority.

---

## 6. Schema — code-defined vs. on-disk (no drift found)

**Canonical schema (code):** `build_tile()` writes a `TileContent` (`models.py:217-221`):
```
TileContent
├── metadata: TileMetadata        (models.py:204-214)
│     cell_id, resolution, temporal_bin, generated_at,
│     vessel_count, aircraft_count, bbox[min_lat,min_lng,max_lat,max_lng]
├── vessels: [VesselRecord]        (models.py:120-133)
│     mmsi, name, vessel_type, position{lat,lng,alt_m,timestamp},
│     heading_deg, speed_knots, course_deg, destination, h3_cells{res:cell}
└── aircraft: [AircraftRecord]     (models.py:179-197)
      icao24, callsign, category, position{lat,lng,alt_m,timestamp},
      velocity_knots, vertical_rate_fpm, heading_deg, on_ground,
      squawk, flight_phase, h3_cells{res:cell}
```

**On-disk verification** — sampled across dates, resolutions, and entity types:
- res-5, **May 2** (aircraft): keys exactly match `AircraftRecord` + `TileMetadata`. ✔
- res-5, **May 3** (vessel `KAASSASSUK`): keys exactly match `VesselRecord`. ✔
- res-6, **June 28** (post-recreation, aircraft): identical schema. ✔
- res-7, `live.json` (April): identical `TileMetadata`. ✔

**No schema drift, no per-resolution variation, no per-entity-type anomaly.** Old and new files
are structurally identical. The owner's observed sample is the consistent, code-defined shape.

**Confirmed redundancy (relevant to a future design, not a current bug):** every record carries
its full identity in *every* snapshot it appears in, plus an `h3_cells` dict embedding that
entity's cell IDs at **all five resolutions**. Example vessel record stores
`h3_cells: {3:…,4:…,5:…,6:…,7:…}` along with `mmsi/name/vessel_type/destination` — repeated in
each of the (up to 5 res × N bins) files the vehicle passes through. This is the
"callsign written 192 times" redundancy noted in the brief.

---

## 7. The separate top-level `cache/` folder

`data/cache/` is **empty (0 bytes)** and is a different mechanism from `data/tiles/h3/`:
- It is **created** at startup — `main.py:205`:
  `for d in [config.data_dir, config.tiling.tile_output_dir, config.data_dir / "cache"]: d.mkdir(...)`
- **Nothing ever reads or writes it.** A full grep for cache usage finds only: this `mkdir`,
  in-memory caches elsewhere (`routes_stats.py` `_geo_cache`, region geocode cache in
  `regions/manager.py`, `lru_cache` decorators) — **none of which touch this directory** — and
  doc prose.

So `cache/` is an **intended-but-never-implemented** scratch directory: a placeholder. It is
not a render/tile cache and holds nothing. `data/tiles/h3/` is the only on-disk store of
substance, and it is the historical snapshot archive characterized above.

---

## 8. Recommendation — retention & growth fix

Read-back finding (#2) is **write-only / no consumer**, and history is **not a current
requirement** → the archive should stop being generated and can be deleted.

### Cheapest immediate change (stops the disk filling again)
The single line that drives all growth is the 60 s loop at `main.py:128-136` feeding
`build_all_resolutions`. Two equivalent low-risk options:

1. **Gate persistence behind a default-off flag.** Add e.g. `TilingConfig.persist_tiles:
   bool = False` and make `on_new_data_batch()` a no-op (or skip `build_all_resolutions`) when
   false. Zero data path depends on the output, so nothing else breaks. *(Recommended — reversible,
   and re-enables cleanly if a designed replay feature ever lands.)*
2. **Stop the worst resolutions only.** Narrow `config.tiling.resolutions` so res-5 and res-6
   are not persisted (e.g. `[3, 4, 7]`), keeping the cheap/self-capping ones. This caps the two
   unbounded-with-time consumers — the actual disk-fill mechanism — while leaving the daily/hourly
   overviews and the self-overwriting `live` tiles.

Either change is a few lines, no schema or API change, and the live map / API are unaffected
(no endpoint reads tile contents; `/api/tiles/stats` keeps working, just reporting a smaller or
static tree).

### Delete the existing tree
Safe to delete `data/tiles/h3/` in full — nothing reads it. If any one-time value is seen in
the *current* history, compress before deleting: this is pretty-printed, sort-keyed JSON
(`orjson` `OPT_INDENT_2 | OPT_SORT_KEYS`) and will compress ~85–90% (`tar czf` or `zstd`), taking
the ~9.4 G tree to roughly ~1 G as a cold archive. Otherwise delete outright. **Priority order
by payoff: res-5 (8.5 G) first, then res-6 (regrowing fastest), then res-7's stale orphans.**

### Can res-6/res-7 specifically stop being persisted?
- **res-6: yes — highest priority to stop.** Fastest grower (1-min bins, 5× res-5), no reader.
  It is the resolution that produced the ~35 G that filled the disk and is already regrowing.
- **res-7: yes, but lower urgency.** No reader, but `live.json` overwrites in place so it is
  self-capping (~233 M, mostly stale). Stopping it is cleanup, not a growth fix.

---

## Future design note (captured per brief — NOT acted on)

If historical replay becomes a real feature, design it deliberately rather than reviving this
write-everything scheme. The current files restore full entity identity (callsign, icao24,
category, squawk) and an all-resolution `h3_cells` map in *every* cell/bin a vehicle passes
through (confirmed in #6). A proper design would:
- store entity **identity once** (a vehicle record keyed by `icao24`/`mmsi`),
- store only the **trajectory** as time-series (the part that actually has history),
- use **compressed/columnar** storage (Parquet or compressed JSON),
- and define an explicit **retention policy** (size cap + age eviction) up front — the exact
  thing `is_bin_expired()` was scaffolded for but never wired to.

---

## Evidence appendix (file:line)

- Write loop: `spatial_agents/main.py:116-136`
- Tile write: `spatial_agents/spatial/tile_builder.py:64-120`, path `:180-182`
- Resolutions / bins: `spatial_agents/config.py:155-162`; `spatial_agents/spatial/temporal_bins.py:30-57`
- Dead content reader: `tile_builder.py:184-194` (`get_tile`, 0 callers)
- Stats (size-only) reader: `tile_builder.py:196-217` → `serving/routes_tiles.py:68-72` (`/api/tiles/stats`)
- Unwired S3 reader: `spatial_agents/deploy/cloud_s3.py:91-100` (0 callers, cloud-only)
- HTTP mount: `spatial_agents/serving/app.py:69-79` (no client fetches it)
- Dead eviction helper: `spatial_agents/spatial/temporal_bins.py:115-121` (0 callers)
- `cache/` created, never used: `main.py:205`
- Schema: `spatial_agents/models.py:108-221`
- Disk (`du -sh`, 2026-06-28): res3 25 M · res4 515 M · res5 8.5 G · res6 142 M · res7 233 M · total ~9.4 G
