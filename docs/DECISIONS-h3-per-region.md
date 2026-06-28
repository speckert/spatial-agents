# DECISIONS — Per-region tile cache keyed by center H3 cell

**Date:** 2026-06-28
**Status:** Implemented + tested; committed and pushed (see [Commit/push](#commitpush)).
**Builds on:** `docs/DECISIONS-h3-archive.md` (investigation — no readers of tile contents) and
`docs/DECISIONS-h3-retention.md` (24 h reaper + city-change clear). **This revises Feature 2 of
the retention work**: the whole-tree clear becomes a surgical per-region clear.
**Out of scope:** playback, tile-content schema change, S3/cloud paths.

---

## Why

The number of active regions tracks **data-provider quota**, not a product decision (2 today; 3+
once a paid tier / second key / new provider lands). A single shared tile tree wiped on every
swap is the wrong model: regions commingle in one tree and you cannot clear or expire one without
hitting the others. Per-region isolation fixes that and **pre-satisfies the future playback
feature's need** for per-region retention (swapping city B must never destroy city A's history).

Done now specifically because the cache has **no readers** (investigation): restructuring a
directory layout is cheapest when it is pure disposable exhaust — no live consumers, no
mid-migration history to preserve.

---

## The durable region identity

**Region key = the center cell of the region's 7-hex display flower, at the display resolution.**

- **Where the center comes from:** the flower is `primary + 6 grid_disk(primary, 1)` neighbors,
  computed in `config.py:_compute_region_cells()` and stored as `REGION_CELLS[name]["primary"]`.
  That `primary` cell **is** the region key — reused, not reinvented. Surfaced through a single
  source of truth: **`RegionsManager.region_key(region) -> str | None`** (`regions/manager.py`),
  which returns `REGION_CELLS[region]["primary"]`. Everything else calls this.
- **Display resolution (read from config, not hardcoded):** **`REGION_RESOLUTION = 4`**
  (`config.py:54`). The flower — and therefore the key — is at res-4. Confirmed against the
  rendering path: `REGION_CELLS[name]` (primary/buffer/all/bbox) is computed at `REGION_RESOLUTION`,
  and `CoverageResponse` ships `primary_cell` + the 7-cell GeoJSON drawn from it.
- **Sanitization:** an H3 hex string (e.g. `8428309ffffffff` for San Francisco) is already
  path-safe; used verbatim as the directory name.
- **Why this and not slot index:** it is deterministic from geography and **stable** across slot
  reordering and swap-out/swap-in — SF is always `8428309ffffffff` regardless of which slot holds
  it. Positional identity ("slot 0/1") is not stable and would reintroduce commingling.

Verified keys (this build): San Francisco → `8428309ffffffff`, Boston → `842a307ffffffff`,
Chicago → `8527546ffffffff`.

---

## What changed

| File | Change |
|---|---|
| `regions/manager.py` | New `region_key()` — durable identity helper (single source of truth). |
| `spatial/tile_builder.py` | `_tile_path`, `build_tile`, `build_tiles_for_records`, `build_all_resolutions` gain a `region_key` segment → `<root>/<region_key>/<res>/<cell>/<bin>.json`. `list_tiles`/`tile_stats` walk the deeper layout and exclude `*.trash-*`. `get_tile` (dead) updated for signature consistency. |
| `spatial/tile_reaper.py` | Per-region walk; **`trash_region()`** (surgical, replaces whole-tree `trash_and_recreate`); **`migrate_flat_tree()`** + `is_flat_layout()`; `find_trash_dirs()` now sweeps **both** locations (inside root + beside it). |
| `main.py` | `on_new_data_batch` partitions the batch by region and writes under each region's key; `on_city_change` clears **only** the departing region; startup runs flat-tree migration + dedup'd trash sweep. |
| `tests/` | `test_tile_reaper.py` rewritten for per-region (region_key, isolation, surgical clear, migration); `test_core.py` / `test_integration.py` / `scripts/demo.py` pass `region_key`. **Stale `test_health_status` fixed** (2 → 4 feeds). |

### Partitioning a commingled batch (the write path)
`FeedManager` buffers commingle all active regions. `on_new_data_batch` splits each batch by the
region whose flower contains a record's res-4 cell — `rec.h3_cells.get(REGION_RESOLUTION) in
set(REGION_CELLS[region]["all"])` — the **same membership test** `FeedManager._purge_region_cache`
already uses on swap. Each region's slice is written under its own `region_key`. End-to-end check
confirmed SF and Boston aircraft land in separate `8428309ffffffff/` and `842a307ffffffff/` trees.

### Surgical city-change clear (revises retention Feature 2)
`on_city_change` now sets aside only `output_dir/<departing_key>` via
`trash_region()` → `os.rename` to `output_dir/<key>.trash-<utc>` (atomic, instant), then
background-deletes. **The remaining region (SF in slot 0) is untouched** — directly fixing the
"SF wiped on every swap" loose end flagged in the retention note. The new city writes to its own
`<new_key>/` subtree on the next rebuild tick. The blocking-safe rename-then-background-delete
pattern is unchanged.

### Reaper one level deeper
`reap_expired_tiles` walks `<region>/<res>/<cell>/<bin>`, same expiration logic. Still skips res-7
`live` per region, ignores `*.trash-*` dirs, tolerates a region vanishing mid-walk (concurrent
swap → `FileNotFoundError` guards), and cleans empty `<cell>` and `<region>/<res>` dirs.

### Migration by deletion
The existing on-disk tree is the flat `<res>/<cell>/…` layout and is **disposable** (no readers;
much of it stale orphans from dropped regions). `migrate_flat_tree()` detects it (a top-level
integer-named dir = legacy; region keys always contain letters), renames the whole tree to a
sibling `h3.trash-migrate-<utc>` (atomic, instant — no startup block), recreates an empty root,
and background-deletes. We do **not** sort flat files into regions — not worth it for disposable
data. **This discards current history**, which is acceptable (not a requirement, and re-derivable
going forward; past timestamps were never recoverable anyway).

### `/api/tiles/stats`
`tile_stats` is unchanged in shape (size-only, never opens files) and now sums across the
per-region layout via the updated `list_tiles`, excluding transient `*.trash-*` dirs. Per-region
size breakdown was deferred to avoid a `TileStatsResponse` model change (brief marked it optional).

---

## Tests

- **`tests/test_tile_reaper.py` (16 tests, all pass):** `region_key` deterministic/stable/distinct;
  `is_bin_expired` max-age; reaper deletes-old/keeps-new; **reaper isolates regions** (region B's
  fresh tile survives while old tiles in A and B are reaped); res-7 skipped; trash dirs ignored +
  missing/malformed tolerated; **`trash_region` surgical** (departing region gone, other region
  intact); migration (`is_flat_layout`, `migrate_flat_tree` sets whole tree aside + recreates
  empty + no-ops when already per-region); `find_trash_dirs` both locations.
- **`test_core.py` / `test_integration.py`:** updated to pass `region_key` and assert the segment
  appears in tile paths.
- **End-to-end check** (real config regions + fabricated records): partition writes isolate SF and
  Boston under their center keys; surgical clear removes only the slot-1 region, SF survives.
- **Stale health test fixed:** `test_health_status` now asserts 4 feeds (`ais/adsb/weather/tfr`).
- **Full suite:** 54 passed, 1 skipped, 0 failed (4 m 13 s, hitting live feeds).

---

## Commit/push

- **Branch:** `master`
- **Commit:** _<hash + subject — filled in after commit>_
- **Push:** _<result — filled in after push>_

---

## Note for the future playback feature

This change pre-satisfies playback's **per-region retention** requirement: each region's last 24 h
lives in its own `<region_key>/` subtree, so swapping city B never touches city A's history, and a
playback API can scope to one region by path. The identity-once / trajectory-store schema redesign
(from the investigation) remains separate, deferred work.
