# Spatial Agents — Regions Protocol (v4 Client)

Audience: iOS / macOS / visionOS v4 client team.
Server: `https://agents.specktech.com` (production), `http://127.0.0.1:8012` (local).
Date: 2026-04-26 — protocol introduced server-side, ready for client integration.
Updated: 2026-04-26 — `display_name` field added to every CoverageResponse;
clients should use it for tab labels (replaces local snake_case-to-Title).

This is **the contract** v4 clients must implement to follow runtime region
swaps. v3.1 (legacy) clients do *not* use regions — they call `/vessels` and
`/aircraft` without a `?region=` filter. The server treats the absence of
`?region=` as **`?region=san_francisco`** (the pinned slot-0 region), so v3.1
always renders San Francisco data regardless of what slot 1 currently is.

---

## 1. Concepts

* **Active regions**: an ordered list of region keys, e.g.
  `["san_francisco", "boston"]`.
  - Slot 0 is **pinned** to `san_francisco` (legacy v3.1 contract).
  - Slot 1 (and beyond, when added) is mutable at runtime.
* **Region key**: snake_case slug used internally and in API queries, e.g.
  `san_francisco`, `st_louis`, `tokyo`, `københavn`. Derived server-side from
  the user-typed city name via Unicode lowercase + alnum filter. Non-ASCII
  letters survive (CJK, accented Latin, etc.) — clients must URL-encode.
* **Display name**: human-readable label. The original city string the user
  typed for custom regions ("København", "東京", "São Paulo"), or a canonical
  English name for seeded ones ("San Francisco", "Chicago"). Returned per
  region in `/health.regions[<key>].display_name`. **Clients should display
  this; never derive labels from the key.**
* **Regions version (`regions_version`)**: 8-char SHA-1 prefix of the active
  list joined by `|`. Whenever the active list mutates (slot swap), this
  changes. Every region-aware response includes it; the client compares.

## 2. Endpoints

### `GET /health`

The canonical source of region truth. Call once at launch. Re-call on every
version-mismatch (see §3).

Relevant response fields:

```json
{
  "regions_version": "a1b2c3d4",
  "regions_slot_zero_pinned": "san_francisco",
  "regions": {
    "san_francisco": {
      "region": "san_francisco",
      "display_name": "San Francisco",
      "primary_cell": "84283d3...",
      "buffer_cells": ["...", "...", "...", "...", "...", "..."],
      "geometry": { "type": "MultiPolygon", "coordinates": [...] },
      "h3_cells": { "3": ["..."], "4": ["..."], "5": ["..."] },
      "advisories": []
    },
    "københavn": {
      "region": "københavn",
      "display_name": "København",
      "...": "..."
    }
  },
  "coverage": { ... }    // legacy: mirrors regions[ACTIVE_REGIONS[0]]
}
```

### `POST /regions/swap`

Replace slot 1 with a new city. Server geocodes server-side.

Request:
```json
{ "city": "St Louis" }
```

Success (`200`):
```json
{
  "old_slot_one": "boston",
  "new_slot_one": "st_louis",
  "active_regions": ["san_francisco", "st_louis"],
  "regions_version": "9e8d7c6b",
  "seconds_until_next_swap_allowed": 112
}
```

Error responses:

| Status | Code             | Meaning                                       |
| ------ | ---------------- | --------------------------------------------- |
| 429    | `rate_limited`   | Cooldown active. `Retry-After` header set.    |
| 400    | `swap_refused`   | Touching slot 0, empty name, or no-op.        |
| 400    | `geocode_failed` | Nominatim returned no result.                 |

Body shape:
```json
{ "detail": { "error": "rate_limited", "message": "...", "retry_after_seconds": 47 } }
```

**Cooldown**: 112 seconds during testing (April–May 2026). Will be raised
to ~15 minutes once App Store v4 ships. Treat the `Retry-After` header
(in seconds) as authoritative.

### `GET /regions`

Diagnostic. Returns the current snapshot — useful for debug screens.

```json
{
  "active_regions": ["san_francisco", "boston"],
  "version": "a1b2c3d4",
  "slot_zero_pinned": "san_francisco",
  "cooldown_seconds": 112,
  "seconds_until_next_swap_allowed": 0
}
```

### Region-aware data endpoints

All of these now stamp `regions_version` on every response. v4 should
always send `?region=`. If absent, `/api/vessels` and `/api/aircraft`
default to `san_francisco` (the legacy v3.1 contract) — *not* "all
regions". `/api/causal/layer` is the exception: absent `?region=` runs
across all active regions (v4-only endpoint, no legacy contract).

| Path                      | Region filter via | Absent ?region= means |
| ------------------------- | ----------------- | --------------------- |
| `GET /api/vessels`        | `?region=<key>`   | san_francisco (legacy)|
| `GET /api/aircraft`       | `?region=<key>`   | san_francisco (legacy)|
| `GET /api/causal/layer`   | `?region=<key>`   | all active regions    |
| `GET /api/weather/alerts` | global (CONUS)    | n/a                   |
| `GET /api/tfr`            | global (CONUS)    | n/a                   |

Weather/TFR are CONUS-wide and not filtered by region — only the causal
graph's filtering changes. They still stamp `regions_version` so a single
mismatch check across all polls can drive the refresh.

## 3. Version-mismatch protocol

```
on every region-aware response:
    if response.regions_version != cached_version:
        cached_version = response.regions_version
        await GET /health
        rebuild region UI from response.regions
        invalidate any per-region caches keyed on the old active set
```

Cadence: do **not** poll `/health` periodically. The version stamp on every
data response is the trigger. v4 should debounce — multiple drifts within a
single second collapse into one `/health` call.

## 4. Client UI requirements

* **Region tab strip**: render one tab per key in `regions` (in the order
  the server returns them). Slot 0 is always San Francisco.
  - **Tab label = `regions[<key>].display_name`.** Do not snake_case-to-Title
    the key yourself — that breaks for non-Latin scripts (e.g. `東京` would
    pass through unchanged but lose any prefix the user typed).
* **`+ New` button**: presents a text field. POSTs `{ "city": "<input>" }` to
  `/regions/swap`.
  - Pass the user's input verbatim. Nominatim accepts every language —
    `"København"`, `"東京"`, `"São Paulo"`, `"Москва"` all resolve.
  - Show `Retry-After` countdown on `429`.
  - Show `detail.message` on `400`.
* On success, immediately `GET /health` and switch the active tab to
  `new_slot_one`. The new tab's label comes from the fresh `display_name`.
* Persist the user's last active region locally (UserDefaults) — but always
  validate it against `/health` before honoring it.

### URL-encoding region keys

The region key may contain non-ASCII letters (`københavn`, `東京`). When
building `?region=` query strings, **always URL-encode**:

```swift
let encoded = key.addingPercentEncoding(
    withAllowedCharacters: .urlQueryAllowed
) ?? key
let url = URL(string: "\(API)/api/vessels?region=\(encoded)")!
```

Failing to encode will produce a 400 from the server (or worse, a silent
mismatch that returns no data).

## 5. Examples

Typical mismatch flow (Glen's iPad and the web map are open at the same time):

```
t=0    iPad: GET /api/vessels?region=san_francisco  → version "a1b2c3d4"
t=5    web: POST /regions/swap {city:"St Louis"}    → version "9e8d7c6b"
t=20   iPad: GET /api/aircraft?region=san_francisco → version "9e8d7c6b"  ← drift!
t=20   iPad: GET /health                            → regions has st_louis,
                                                       san_francisco; rebuild
                                                       tabs
```

Failure flow:

```
POST /regions/swap {city:"asdfasdf"}  → 400 geocode_failed
POST /regions/swap {city:"Boston"}    → 200 (works)
POST /regions/swap {city:"Chicago"}   → 429 retry-after: 47
```

## 6. Server defaults & state

* On a cold restart with no persisted state: `["san_francisco", "boston"]`.
* State persisted to `data/regions_state.json` (server-side). Custom-region
  centers (e.g. St Louis) are cached so a restart doesn't re-geocode.

## 7. Things v4 does *not* need to do

* Don't compute H3 cells client-side — use `regions[<key>].h3_cells[<res>]`.
* Don't compute bounding boxes — use `regions[<key>].geometry`
  (MultiPolygon). Fit map bounds from coordinates if needed.
* Don't try to enforce slot 0 — server rejects swaps that target it.
* Don't poll `/health` on a timer. Use the version stamp instead.

## 8. Open questions for the client team

1. Should `+ New` accept country/state qualifiers (`"Springfield, IL"`)?
   Server passes the string straight to Nominatim — yes, it works, picks
   the first hit.
2. Do we want a list of "popular regions" curated server-side that the
   client can show as quick-pick chips? Easy to add (`GET /regions/suggestions`).
3. Multi-slot (>2) future: the protocol already supports it. Slot 0 stays
   pinned; slots 1..N would all be mutable. The server endpoint is named
   `swap_slot_one` today — if we go multi-slot this becomes
   `swap_slot/{n}` with the same rate-limit + slot-0 guard.

Reach out: `speckert@specktech.com`.
