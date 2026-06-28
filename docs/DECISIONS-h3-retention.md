# DECISIONS — H3 tile retention: 24 h reaper + async city-change clear

**Date:** 2026-06-28
**Status:** Implemented.
**Builds on:** `docs/DECISIONS-h3-archive.md` (investigation — the tree has no native
eviction and nothing reads tile *contents*, so it is safe to expire/delete).
**Scope:** Two features only — time-based expiration and city-change cache clear.
**Out of scope (deferred):** playback/timeline, schema redesign, S3/cloud paths.

---

## Summary

`data/tiles/h3/` accumulated unbounded and filled the disk because nothing ever evicted it.
This change bounds it two ways, both **filesystem-only** (never open a tile, so they stay fast
over millions of files):

1. **24 h reaper** — a periodic background task deletes tiles whose temporal-bin *filename* is
   older than `retention_hours` (default 24).
2. **City-change clear** — when the active city changes, the whole tree is `os.rename`d aside
   (atomic, instant) and recreated empty; the slow delete of the renamed dir runs in the
   background so the swap never blocks.

Persistence stays **ON** — we chose *bounded retention*, not disabling the write path, so the
last 24 h stays available for the future playback feature.

---

## What changed (files)

| File | Change |
|---|---|
| `spatial_agents/config.py` | `TilingConfig`: added `retention_hours: int = 24` and `reaper_interval_seconds: int = 600`. |
| `spatial_agents/spatial/temporal_bins.py` | `is_bin_expired()` (was zero-callers) gained an optional `max_age_hours` param: parses the bin-start timestamp from the key and returns whether it is older than N hours. Legacy no-arg behavior unchanged. |
| `spatial_agents/spatial/tile_reaper.py` | **New module** — pure, testable filesystem helpers: `reap_expired_tiles()`, `trash_and_recreate()`, `find_trash_dirs()`. |
| `spatial_agents/main.py` | `run_pipeline()`: added `tile_reaper_loop()` (co-located with `tile_rebuild_loop`), an `on_city_change` swap callback (rename-then-background-delete), a startup sweep of leftover trash dirs, and registration/cancellation of the reaper task. |
| `tests/test_tile_reaper.py` | **New** — 8 unit tests covering both features. |

The write path (`tile_rebuild_loop` / `build_all_resolutions`) was **not** altered beyond adding
the sibling reaper task.

---

## Feature 1 — 24 h reaper

- **Loop:** `tile_reaper_loop()` in `main.py`, mirroring `tile_rebuild_loop` (`main.py`) and the
  in-memory `_vessel_cleanup_loop` pattern (`feed_manager.py:470-489`). Each cycle sleeps
  `reaper_interval_seconds`, skips if `retention_hours <= 0` (disabled), else runs
  `reap_expired_tiles(...)` via `asyncio.to_thread` so a large first sweep never blocks the
  event loop / API server.
- **Logic:** `reap_expired_tiles()` walks `<res>/<cell>/<bin>.json`, parses the bin from the
  **filename**, and unlinks via `is_bin_expired(stem, res, retention_hours)`. Now-empty
  `<cell>/` dirs are `rmdir`'d opportunistically (harmless `OSError` if non-empty/gone).
- **Registered** alongside `tile_rebuild_loop` and cancelled in the same `finally`.

### res-7 decision: **skip it**
res-7 tiles use the constant bin key `live.json` (overwritten in place), so the filename is not
a timestamp. res-7 is **self-capping** (one file per cell, ~233 MB, never grows with time), so
the reaper skips any resolution whose configured bin size is `"live"`
(`reap_expired_tiles` checks `binner.get_bin_size(resolution) == "live"`). An mtime-based path
was considered and rejected as unnecessary churn for a bounded ~233 MB that isn't the disk-fill
mechanism. If res-7 ever needs trimming, switch the skip to an mtime check — the hook is there.

### Robustness
- Unparseable bin filename → `is_bin_expired` returns `False` (never delete what we can't date).
- Missing dir/file mid-walk (e.g. a concurrent city-change rename) → `_safe_iterdir` and
  `FileNotFoundError` guards skip it; the loop's outer try/except catches anything else so a
  transient error never kills the loop.
- `/api/tiles/stats` is unaffected and now reports a bounded tree.

### Expiration is by bin **start** (minor, intentional)
A coarse bin (res-3 daily, res-4 hourly) is judged by its start timestamp, so it can be dropped
up to one bin-width after its newest contents cross 24 h. This matches the brief's "parse the
bin and compare to N hours" and is immaterial — res-3/res-4 are 25 MB / 515 MB and the real
growth is res-5/res-6 (5-min/1-min bins), where start≈end.

---

## Feature 2 — async city-change clear

### City-switch handler location
The active city changes via **`RegionsManager.swap_slot_one()`**
(`spatial_agents/regions/manager.py:352-422`), reached from **`POST /regions/swap`**
(`spatial_agents/serving/routes_regions.py:81-159`). After a successful swap, the manager fires
all registered `on_swap` callbacks (`manager.py:409-415`).

**Hook chosen:** register a new swap callback rather than editing swap logic. In
`main.py` we call `regions_manager.on_swap(on_city_change)` — it fires exactly on a real
city change (not on startup `initialize()`, which doesn't fire callbacks), runs in the async
context, and required no change to the swap path. Minimal and reversible.

### Rename-then-background-delete, and why
A synchronous `shutil.rmtree` over this tree blocks for a long time (millions of small files —
~an hour for ~7 M files observed). The swap must feel instant, so `on_city_change`:
1. `await asyncio.to_thread(trash_and_recreate, tile_output_dir)` — `os.rename`s `h3` →
   `h3.trash-<utc>` (atomic, O(1) on the same filesystem) and recreates an empty `h3`. Returns
   immediately; the next `tile_rebuild_loop` tick (≤60 s) repopulates for the new city.
2. `asyncio.create_task(_background_rmtree(trash))` — the slow delete runs off the request path;
   failure only logs (orphaned `h3.trash-*` dirs are harmless and swept next startup).

Fire-and-forget tasks are kept in a `bg_tasks` set (with a `done` callback to discard) so they
aren't garbage-collected mid-run.

**Note — slot 0 (SF) tiles are cleared too.** The tree mixes both active regions' cells, so the
whole-tree rename also drops SF tiles; they rebuild within one tick. No user impact: nothing
reads tile contents from disk (the live map and API don't — see the investigation), so the
≤60 s gap is invisible. `/api/tiles/stats` briefly reports a smaller tree.

### Startup sweep
On startup, `find_trash_dirs(tile_output_dir)` locates any leftover `h3.trash-*` from a run
killed mid-delete and schedules each for `_background_rmtree`, preventing accumulation.

### Concurrency
The reaper tolerates the tree being renamed out from under it mid-walk (missing-path guards), so
a city switch during a reaper cycle does not error. Both paths only ever delete; no shared
mutable state.

---

## Config defaults

| Setting | Default | Meaning |
|---|---|---|
| `tiling.retention_hours` | `24` | Keep tiles newer than 24 h; `0` disables the reaper. |
| `tiling.reaper_interval_seconds` | `600` | Reaper runs every 10 min. |

Defaults make both features safe if untouched: retention on at 24 h, reaper every 10 min.

---

## How it was tested

- **Unit tests** (`tests/test_tile_reaper.py`, 8 passed): `is_bin_expired` max-age mode
  (old/fresh/`live`/unparseable/legacy); reaper deletes old & keeps fresh & cleans empty cell
  dirs; reaper skips res-7 even when its mtime is ancient; reaper tolerates a missing root and a
  malformed filename; `trash_and_recreate` renames + recreates empty (and no-ops on an empty
  tree); `find_trash_dirs` matches only `h3.trash-*`.
- **Real-data smoke test on a COPY** (scratchpad, real tree untouched): copied 3 res-5 cells
  (1107 old May files) + 1 res-7 cell into a temp tree and ran `reap_expired_tiles(retention_hours=24)`
  → **1107 res-5 files deleted, res-7 `live.json` preserved, emptied cell dirs removed.** This is
  the short-retention check the brief asked for; production stays at 24 h.
- **Import / config check:** server module imports clean; `config.tiling.retention_hours == 24`,
  `reaper_interval_seconds == 600`.
- **Full suite:** 50 passed, 1 skipped, **1 pre-existing unrelated failure** —
  `tests/test_integration.py::TestFeedManagerIntegration::test_health_status` asserts 2 feeds but
  `FeedManager` now registers 4 (`ais`, `adsb`, `nws`, `tfr`, added by the NWS/TFR commits). It
  touches `feed_manager.health()`, none of the files changed here, and fails independently of
  this work.

---

## Deferred (next brief)

- **Playback** of the last 24 h (timeline UI + playback API). This retention work is its
  prerequisite — it guarantees ~24 h of data is on disk. The identity-once / trajectory-store
  schema redesign noted in the investigation is part of that future work, not this change.
