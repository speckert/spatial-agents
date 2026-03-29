<!--
  CHANGELOG.md — Architectural decisions and session history.

  This file captures session-level context that lives between per-file
  version histories. It serves as a recovery aid: upload the project zip
  to a new Claude conversation and the combination of this changelog,
  the README, the architecture HTML, and the per-file version histories
  provides full context reconstruction.

  Version History:
      0.1.0  2026-03-28  Initial changelog from first design session
      0.1.1  2026-03-28  Infrastructure decisions — host machine, network,
                         Apache/Certbot/HTTPS configuration
      0.1.2  2026-03-28  Cleaned up external company references, added
                         speckert@specktech.com contact
-->

# Changelog — Spatial Agents Python Intelligence Layer

## 2026-03-28 — Initial Architecture Session

### Context

Glen is preparing for a geospatial data engineering interview.
Rather than building a throwaway Python demo, the decision was made
to build the Python component of Spatial Agents as the interview
portfolio piece — a production geospatial pipeline feeding a shipping
App Store product.

### Key Architectural Decisions

**Python's role in the ecosystem.**
The Python layer is the intelligence server — it handles data ingest,
geospatial indexing, causal reasoning, and FM prompt evaluation. It does
NOT run on the phone or Vision Pro. It runs on a Mac (local) or cloud
server. The Swift clients are thin consumers of its output.

**Mac as server.**
The Mac (M1 Mini) is the always-on node. Phone and Vision Pro are
intermittent clients. The Mac can stay active for extended periods,
making it the natural home for a persistent data pipeline. The M1 Mini
draws ~7W idle and runs headless on a 10 Gbps LAN.

**H3 over S2 for spatial indexing.**
Chose Uber's H3 hexagonal tiling (Apache 2.0) over S2 geometry.
Reasons: H3 has uniform adjacency (6 neighbors vs S2's variable),
cleaner nesting across resolutions, better Python library (h3-py),
and no external service dependency. H3 was developed by Uber, not Amazon.

**H3 tiles are static files.**
Tiles are pre-computed JSON files served from any web server — no
special infrastructure required. FastAPI serves them via StaticFiles
mount. The path convention is `/tiles/h3/{resolution}/{cell_id}/{temporal_bin}.json`.
This applies the same concept of level-of-detail hierarchies used in
raster image pyramids, adapted for vector event streams.

**FastAPI on port 8012.**
Single process, single port. The router forwards port 8012 to the
M1 Mini. No Nginx needed — FastAPI handles both static tile serving
and dynamic API queries. Keeps the stack simple and all-Python.

**FM Python SDK for evaluation only, not production.**
The Apple FM Python SDK (macOS only) is used for prompt engineering,
token budget analysis, and regression testing. It is NOT the production
FM inference path. In production, the Swift client runs the on-device
FM via the Swift Foundation Models framework. The Python SDK ensures
prompts are correct before they ship in the Swift app.

**Context window is the critical constraint.**
The on-device FM has a 4096-token context window. System prompt + tool
schemas + data payload + response must all fit. The Python pipeline's
job is to compress raw geospatial data into context-budget payloads —
statistical summaries, top-N anomalies, causal graph excerpts — not
raw position lists. iOS 26.4 added `contextSize` and `tokenCount(for:)`
APIs to measure this precisely.

**Pearl's SCM for causal reasoning.**
Using Judea Pearl's structural causal model framework — not just
correlation dashboards. The Python layer builds DAGs from detected
events (vessel loitering, dark gaps, flight diversions), and the
on-device FM narrates the causal chains into human-readable intelligence.
The causal module uses NetworkX for graph operations and encodes domain
knowledge as typed causal rules (e.g., weather → vessel_loitering).
The guiding principle: "Know the How, Show the Why."

**Xcode is not involved in the Python layer.**
The Python pipeline runs in Terminal / as a system process. Xcode
enters only for: (a) the Swift client apps (iPhone, Vision Pro), and
(b) eventually a Mac host app (SwiftUI shell wrapping the Python
process via NSTask for App Store distribution). For development and
debugging of the Python code, the workflow is Claude conversations
with zip file uploads for context recovery.

### Three-Tier Deployment Model

**Tier 1 — Free baseline.**
Map visualization, basic feed display. No intelligence. The funnel
that drives App Store downloads.

**Tier 2 — Mac Local Server (one-time purchase).**
M1 Mini runs the Python pipeline. Serves intelligence to local clients
via REST API on port 8012 or shared files. The Mac app is what gets
sold — it wraps the Python process in a SwiftUI shell.

**Tier 3 — Cloud Service (subscription).**
Same Python pipeline on hosted infrastructure. S3 for static tile
delivery, FastAPI for dynamic queries. For phone-only users without
a Mac. Client is agnostic to data source — same payload format from
local Mac or cloud. Base URL is the only config difference.

### Pricing Discovery

Free gets volume and App Store visibility. $10 got zero downloads.
Solution: free tier as funnel, paywall the AIS/plane tracking
intelligence features. Mac app at one-time price for prosumers.
Subscription for phone-only cloud users.

### Scalability Path

Single M1 Mini handles the data volume comfortably — AIS is ~300K
vessels globally, ADS-B ~50-100K aircraft, both well within a single
server's capacity. The 10 Gbps pipe can serve thousands of concurrent
tile requests. Transition to S3 is triggered by reliability SLAs or
geographic latency, not by data volume. S3 mirroring is additive —
the pipeline writes tiles locally AND syncs to S3. Nothing gets
torn down.

### Interview Framing

"Here's a production Python geospatial pipeline that ingests real-time
maritime and aviation data, spatially indexes it with H3, manages
context budgets for an on-device LLM, and feeds a causal reasoning
framework — and it ships in an App Store product." This is
categorically different from a homework assignment.

The tiling design draws on deep experience with multi-resolution
imagery pyramids — the same level-of-detail concept applied to
vector event streams rather than raster pixels.

### Files Created

45 source files across 8 packages:
- `ingest/` — AIS (pyais), ADS-B (OpenSky), TLE, feed manager
- `spatial/` — H3 indexer, tile builder, temporal binner, GeoJSON export
- `intelligence/` — prompt templates, token budget, schema validator, eval harness
- `causal/` — event detector, DAG builder, intervention engine, graph serializer
- `serving/` — FastAPI app, API routes, tile routes, health routes, file exporter
- `deploy/` — local Mac (uvicorn), cloud (S3 sync), Dockerfile (ARM64)
- `data/` — sample SF Bay vessels, aircraft, dark gap track
- `scripts/` — 12-step pipeline demo
- `tests/` — unit tests, integration tests, conftest fixtures
- `docs/` — architecture HTML for interview presentation

All files have module docstrings with description and version history.
All Python entry points support `--help`.

## 2026-03-28 — Infrastructure Decisions

### Host Machine

**"Neural Magician"** — M1 Mac Mini, 16 GB unified memory, 256 GB SSD.
Dedicated to Spatial Agents. Running macOS Tahoe 26.4. Approximately
100 GB available after macOS and migrated web content.

The 16 GB was chosen over the 8 GB / 512 SSD unit because:
- Foundation Models loads a 3B parameter model into unified memory
  shared with the Neural Engine — needs headroom alongside the pipeline
- Memory is the hard constraint; disk is not (tiles are small)
- The M1's 16-core Neural Engine runs FM inference, Core ML anomaly
  detection, and any future Vision framework processing

The second M1 Mini (8 GB / 512 SSD) is available as a dev/test target.

An Intel Core i7 Mac Mini (Mojave, 8 GB / 512 SSD) is being retired.
Its web sites (~120 GB, being trimmed) are migrating to Neural Magician.

### Network Architecture

**Single-server model.** All traffic goes through Neural Magician.
The original plan of port 8012 on a separate machine was replaced
once both workloads (static sites + Spatial Agents) consolidated
onto one box.

```
Internet
    ↓
Router (port 80/443 → Neural Magician)
    ↓
Apache (port 80/443, with Certbot HTTPS)
    ├── spatialagents.com/            → ~/Sites/SpatialAgents/ (static)
    ├── spatialagents.com/api/*       → proxy to localhost:8012
    ├── spatialagents.com/tiles/*     → proxy to localhost:8012
    ├── spatialagents.com/health      → proxy to localhost:8012
    ├── existingsite1.com             → ~/Sites/site1/ (static)
    └── existingsite2.com             → ~/Sites/site2/ (static)
    ↓
FastAPI (localhost:8012, not externally exposed)
```

**Domain:** spatialagents.com (to be registered). DNS points to
the home IP. Router forwards 80/443 to Neural Magician.

**HTTPS:** Certbot with Apache plugin. Auto-renewing Let's Encrypt
certificates. Certbot was chosen over Caddy because Apache is already
in place for the existing sites — no reason to replace a working
web server.

**FastAPI binds to localhost only (127.0.0.1:8012).** Not exposed
to the network. Apache reverse-proxies /api, /tiles, and /health
to it. The outside world only sees ports 80 and 443.

**FileVault: disabled.** The Mini needs to boot unattended after
power outages. M1 hardware encryption via Secure Enclave is always
active regardless. The data is public feeds and public websites.

### Apache Configuration

Apache config lives at:
- `/etc/apache2/httpd.conf` — main config
- `/etc/apache2/extra/httpd-vhosts.conf` — virtual hosts

Required modules to enable:
- `mod_proxy` and `mod_proxy_http` — reverse proxy to FastAPI
- `mod_vhost_alias` — multiple virtual hosts
- `mod_ssl` — HTTPS
- `mod_rewrite` — HTTP→HTTPS redirect

Old Mojave configs copied from Intel for reference only — not
reusable directly due to Apache version differences.

### Directory Layout on Neural Magician

```
~/Sites/SpatialAgents/              ← public web assets (served by Apache)
    index.html
    docs/spatial-agents-arch.html

~/Sites/existingsite1/              ← existing sites (served by Apache)
~/Sites/existingsite2/

~/Projects/spatial-agents/          ← Python source (NOT in web root)
    spatial_agents/
    scripts/
    tests/
    ...

/data/tiles/h3/                     ← generated tiles (served by FastAPI)
```

Source code lives in `~/Projects`, outside the Apache document root.
Never expose source or config to the web.

### Disk Budget (256 GB SSD)

| Component | Size |
|---|---|
| macOS Tahoe 26.4 | ~35 GB |
| Migrated web sites (after trim) | ~15-30 GB (est.) |
| Python + venv + dependencies | ~700 MB |
| Spatial Agents source | ~1 MB |
| H3 tiles (Bay Area) | ~200 MB |
| H3 tiles (global, future) | ~5 GB |
| Headroom | ~100+ GB |

