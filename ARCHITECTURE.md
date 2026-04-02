# Architecture

System design and component reference for Spatial Agents.

For the interactive visual version of this document, see
[agents.specktech.com/architecture.html](https://agents.specktech.com/architecture.html).

## System Overview

Spatial Agents is a geospatial intelligence pipeline that transforms raw
position data from maritime and aviation feeds into structured intelligence
products. The pipeline runs as a single async Python process serving a
FastAPI REST API.

```
┌─────────────┐    ┌─────────────┐
│  AIS Stream  │    │  OpenSky    │
│  (WebSocket) │    │  (REST)     │
└──────┬───────┘    └──────┬──────┘
       │                   │
       ▼                   ▼
┌──────────────────────────────────┐
│          Feed Manager            │
│   Buffered deques (50K max)      │
│   H3 cell assignment at ingest   │
└──────────────┬───────────────────┘
               │
       ┌───────┴───────┐
       ▼               ▼
┌─────────────┐  ┌─────────────┐
│ Tile Builder │  │   Event     │
│ H3 pyramid   │  │  Detector   │
│ res 3-7      │  │             │
└──────┬───────┘  └──────┬──────┘
       │                 │
       │                 ▼
       │          ┌─────────────┐
       │          │ DAG Builder  │
       │          │ Pearl SCM    │
       │          └──────┬──────┘
       │                 │
       ▼                 ▼
┌──────────────────────────────────┐
│          FastAPI Server          │
│  /api/* /tiles/* /health /docs   │
└──────────────────────────────────┘
               │
       ┌───────┴───────┐
       ▼               ▼
┌─────────────┐  ┌─────────────┐
│ Apple Clients│  │ Static Tiles │
│ iOS/macOS/   │  │ JSON files   │
│ visionOS     │  │ on disk/S3   │
└─────────────┘  └─────────────┘
```

## Data Flow

1. **Ingest** — Live feeds deliver vessel and aircraft position records.
   Each record is assigned H3 cell IDs at all configured resolutions (3-7)
   at ingest time.
2. **Buffer** — FeedManager maintains bounded deques (50K records max) and
   a latest-record index keyed by vessel MMSI or aircraft ICAO24. Stale
   aircraft are evicted after 10 minutes; stale vessels after 8 hours.
   A 5-point position history is maintained per entity for trail rendering.
3. **Tile Generation** — Every 60 seconds, the tile builder reads the latest
   records and writes JSON tile files organized by resolution, cell ID, and
   temporal bin.
4. **Event Detection** — Behavioral patterns (loitering, dark gaps, density
   anomalies, ground stops) are detected from buffered records.
5. **Causal Reasoning** — Detected events are assembled into a directed
   acyclic graph using domain knowledge rules, then counterfactual
   interventions are computed via do-calculus.
6. **Serving** — FastAPI exposes live queries, pre-computed tiles, causal
   graphs, and health diagnostics over REST.

## Components

### Ingest (`spatial_agents/ingest/`)

**FeedManager** orchestrates two concurrent data sources:

**AIS WebSocket** (`aisstream_client.py`)
- Endpoint: `wss://stream.aisstream.io/v0/stream`
- Subscribes to a bounding box with an API key
- Receives PositionReport (types 1-3, 18-19) and ShipStaticData (types 5, 24)
- Automatic reconnection with exponential backoff (5s to 120s cap)
- Parses NMEA via pyais (`ais_parser.py`)

**ADS-B Polling** (`adsb_parser.py`)
- Endpoint: OpenSky Network REST API
- Polls every 30 seconds for state vectors within a bounding box
- Converts units: m/s to knots, m/s to fpm
- Graceful handling of rate limits (HTTP 429)

Both feeds produce Pydantic records (`VesselRecord`, `AircraftRecord`) with
H3 cell IDs pre-computed at all resolutions.

### Spatial Indexing (`spatial_agents/spatial/`)

**H3Indexer** (`h3_indexer.py`)
- Wraps the [H3 library](https://h3geo.org/) for multi-resolution cell
  assignment, neighbor lookups, and bounding box queries
- Cell operations: `latlng_to_cell`, `cell_to_latlng`, `cell_to_boundary`,
  `grid_disk`, `geo_to_cells`

**TileBuilder** (`tile_builder.py`)
- Generates JSON tile files at all configured resolutions
- Groups records by H3 cell and temporal bin
- Output path: `{tile_dir}/{resolution}/{cell_id}/{temporal_bin}.json`
- Tile content includes metadata (counts, bbox, timestamps) plus full
  vessel/aircraft records

**TemporalBinner** (`temporal_bins.py`)
- Maps resolutions to time windows:
  - Res 3 → 1 day, Res 4 → 1 hour, Res 5 → 5 min, Res 6 → 1 min, Res 7 → live
- Aligns timestamps to bin boundaries for consistent tile keys

**GeoJSONExport** (`geojson_export.py`)
- Converts tiles to GeoJSON FeatureCollections for map rendering

### Causal Reasoning (`spatial_agents/causal/`)

Implements Pearl's structural causal model (SCM) framework.

**EventDetector** (`event_detector.py`)

Detects behavioral patterns from position data:

| Event Type | Domain | Detection Logic |
|---|---|---|
| `vessel_loitering` | Maritime | Speed < 2.0 kn, multiple reports |
| `dark_vessel_gap` | Maritime | > 15 min silence in track history |
| `density_anomaly` | Both | Z-score >= 2.0 vs. historical mean |
| `ground_stop_indicator` | Aviation | > 70% of aircraft on ground (min 5) |

Each event carries a confidence score, entity IDs, H3 cell, timestamp, and
supporting metrics.

**DAGBuilder** (`dag_builder.py`)

Constructs a directed acyclic graph from detected events:

1. Each event becomes a node
2. Edges are discovered by matching event pairs against domain knowledge rules
3. Edge strength = `base_strength * temporal_decay * confidence_factor`
4. Temporal decay penalizes distant events (max gap: 6 hours)
5. Acyclicity is verified via NetworkX

Domain knowledge rules encode causal relationships like "adverse weather
causes vessel loitering" with base strengths and mechanism descriptions.

**InterventionEngine** (`intervention.py`)

Implements Pearl's do-operator for counterfactual queries:

1. Mutilate the graph (remove incoming edges to the intervention target)
2. Propagate effects through all descendants
3. Compute effect magnitude along each causal path
4. Return affected nodes, causal paths, and natural language descriptions

**GraphSerializer** (`graph_serializer.py`)

Serializes causal graphs to JSON for the REST API and FM payloads. Supports
full and compact (token-efficient) output formats.

### Intelligence (`spatial_agents/intelligence/`)

Manages Foundation Model prompt evaluation with strict token budgets.

**TokenBudgetManager** (`token_budget.py`)
- Context window: 4096 tokens
- Allocations: instructions (~30 tokens), tool schemas (~80-200), data
  payload (budget-managed), response (remainder)
- Payload budget cap: 15% of context window
- Estimation fallback: 4 characters per token when FM SDK unavailable

**PromptLibrary** (`prompt_templates.py`)

Versioned prompt templates organized by domain:

| Template | Domain | Purpose |
|---|---|---|
| `maritime_situation_report` | Maritime | Vessel activity analysis |
| `maritime_anomaly_detection` | Maritime | Dark vessels, loitering, spoofing |
| `aviation_situation_report` | Aviation | Flight pattern analysis |
| `causal_graph_narration` | Cross-domain | Natural language causal explanation |

Each template defines system instructions, a user prompt template with
placeholders, an expected output schema, and version metadata.

**SchemaValidator** (`schema_validator.py`)
- Validates FM structured outputs against Pydantic schemas
- Semantic checks: summary length, observation count, confidence thresholds
- Returns validation results with errors and warnings

**EvalHarness** (`eval_harness.py`)
- Batch prompt evaluation with test suites
- Two modes: live (FM SDK) and dry-run (renders prompts, measures tokens,
  validates structure)

### Serving (`spatial_agents/serving/`)

**FastAPI Application** (`app.py`)
- CORS enabled for client access
- Static tile serving from disk
- Route registration for API, tiles, and health endpoints

**Routes:**
- `routes_api.py` — Dynamic query endpoints (`/api/vessels/`, `/api/aircraft/`,
  `/api/intelligence/`, `/api/causal/`, `/api/budget`)
- `routes_tiles.py` — Tile metadata, bbox queries, position lookups
  (`/api/tiles/info/`, `/api/tiles/bbox`, `/api/tiles/position`, `/api/tiles/stats`)
- `routes_health.py` — Pipeline health and feed diagnostics (`/health`,
  `/health/feeds`)
- `file_exporter.py` — Filesystem-based payload delivery (alternative to REST)

### Deploy (`spatial_agents/deploy/`)

**Local Mac** (`local_mac.py`)
- Target: M1 Mac Mini behind Apache reverse proxy with HTTPS (Let's Encrypt)
- Binds to 127.0.0.1:8012, single worker
- Creates data directories on startup

**Cloud S3** (`cloud_s3.py`)
- Target: Container deployment with S3 tile backend
- S3TileSync uploads tiles with 60-second cache headers
- Optional CloudFront CDN integration
- Requires `pip install spatial-agents[cloud]` for boto3

## Data Models

All data structures are Pydantic v2 models defined in `spatial_agents/models.py`.

**Core entities:**
- `VesselRecord` — MMSI, name, type, position, heading, speed, course,
  destination, H3 cells
  - `track`: array of [lng, lat] position history (up to 5 points)
  - `track_points`: number of track positions available
- `AircraftRecord` — ICAO24, callsign, category, position, velocity,
  vertical rate, heading, on_ground, squawk, flight_phase, H3 cells
  - `flight_phase`: server-classified phase — `ground`, `departure`,
    `approach`, `climbing`, `descending`, `cruising`
  - `track`: array of [lng, lat] position history (up to 5 points)
  - `track_points`: number of track positions available

**Spatial:**
- `TileMetadata` — Cell ID, resolution, temporal bin, timestamps, counts, bbox
- `TileContent` — Metadata + vessel and aircraft record arrays

**Causal:**
- `CausalNode` — ID, label, domain, event type, observed value, timestamp
- `CausalEdge` — Source, target, strength, mechanism description
- `CausalGraph` — H3 cell, nodes, edges, interventions

**Intelligence:**
- `SituationReport` — Summary, observations, anomalies, causal narrative,
  confidence score
- `TokenBudget` — Context window allocation breakdown

**Health:**
- `FeedStatus` — Connection state, message rate, errors per feed
- `PipelineHealth` — Overall status, uptime, feed array, tile statistics

## Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| Web framework | FastAPI + Uvicorn |
| Spatial index | H3 (Uber) |
| Data validation | Pydantic v2 |
| Graph library | NetworkX |
| AIS parsing | pyais |
| HTTP client | httpx |
| WebSocket | websockets |
| Serialization | orjson |
| Cloud storage | boto3 (optional) |
| Testing | pytest + pytest-asyncio |
| Linting | ruff |
| Type checking | mypy (strict) |
