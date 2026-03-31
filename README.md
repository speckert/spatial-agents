# Spatial Agents

Geospatial intelligence pipeline that ingests real-time maritime (AIS) and
aviation (ADS-B) feeds, builds H3-indexed tile pyramids, constructs structural
causal models using Pearl's SCM framework, and serves intelligence to iOS,
macOS, and visionOS clients via a FastAPI REST API.

**Live instance:** [agents.specktech.com](https://agents.specktech.com)

## Features

- **Real-time data ingestion** — AIS vessel tracking via WebSocket, ADS-B
  aircraft positions via REST polling
- **H3 hexagonal tiling** — Multi-resolution spatial index (res 3-7) with
  temporal binning from daily aggregates down to live streams
- **Causal reasoning** — Event detection (loitering, dark gaps, density
  anomalies, ground stops), DAG construction, and do-calculus counterfactual
  interventions
- **Foundation Model integration** — Token-budgeted prompt evaluation with
  schema-validated structured outputs
- **Apple platform clients** — Designed to serve iOS, macOS, and visionOS
  apps via JSON REST API

## Quick Start

```bash
# Clone and install
git clone https://github.com/glenspecktech/spatial-agents.git
cd spatial-agents
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Start the server
spatial-agents --port 8012
```

The server starts on `http://127.0.0.1:8012`. Interactive API docs are
available at `/docs` (Swagger UI) and `/redoc`.

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `SPATIAL_AGENTS_MODE` | `local_mac` | Deployment mode: `local_mac` or `cloud` |
| `SPATIAL_AGENTS_PORT` | `8012` | Server port |
| `SPATIAL_AGENTS_AIS_KEY` | — | aisstream.io API key (enables live AIS) |
| `SPATIAL_AGENTS_ADSB_CLIENT_ID` | — | OpenSky Network OAuth2 client ID |
| `SPATIAL_AGENTS_ADSB_CLIENT_SECRET` | — | OpenSky Network OAuth2 client secret |
| `SPATIAL_AGENTS_DATA_DIR` | `./data` (local) or `/data` (cloud) | Root data directory |
| `SPATIAL_AGENTS_TILE_DIR` | `{data_dir}/tiles/h3` | Tile output directory |
| `SPATIAL_AGENTS_S3_BUCKET` | — | S3 bucket for cloud tile delivery |
| `SPATIAL_AGENTS_S3_REGION` | `us-west-2` | AWS region for S3 |

CLI arguments:

```
spatial-agents --mode local_mac --port 8012 --verbose
```

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Pipeline health, feed status, uptime |
| `GET` | `/api/vessels/{h3_cell}` | Live vessel positions in an H3 cell |
| `GET` | `/api/aircraft/{h3_cell}` | Live aircraft positions in an H3 cell |
| `GET` | `/api/intelligence/{h3_cell}` | FM situation report for a cell |
| `GET` | `/api/causal/{h3_cell}` | Causal event graph with interventions |
| `GET` | `/api/budget` | Token budget allocation |
| `GET` | `/api/tiles/info/{h3_cell}` | H3 cell metadata and geometry |
| `GET` | `/api/tiles/bbox` | Cells within a bounding box |
| `GET` | `/api/tiles/position` | Cell IDs for a lat/lng coordinate |
| `GET` | `/api/tiles/stats` | Tile storage statistics |
| `GET` | `/tiles/{path}` | Static pre-computed tile files |

Full API documentation with request/response schemas:
[agents.specktech.com/docs](https://agents.specktech.com/docs)

OpenAPI spec: [agents.specktech.com/openapi.json](https://agents.specktech.com/openapi.json)

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system design, data flow,
and component reference.

```
spatial_agents/
├── ingest/          # AIS WebSocket + ADS-B REST feed parsers
├── spatial/         # H3 hexagonal indexing + tile generation
├── intelligence/    # FM prompt evaluation + token budget management
├── causal/          # Pearl SCM DAG construction + do-calculus
├── serving/         # FastAPI REST API + static tile server
├── deploy/          # Local Mac + cloud (S3) deployment configs
└── data/            # Sample data for offline development
```

## H3 Tiling Scheme

Multi-resolution hexagonal tiles using [Uber H3](https://h3geo.org/):

| Resolution | Edge Length | Use Case | Temporal Bin |
|---|---|---|---|
| 3 | ~59.8 km | Regional density heatmap | 1 day |
| 4 | ~22.6 km | Shipping lanes / corridors | 1 hour |
| 5 | ~8.5 km | Port / airport approach | 5 min |
| 6 | ~3.2 km | Harbor / terminal detail | 1 min |
| 7 | ~1.2 km | Berth / gate level | Live |

## Deployment Tiers

| Tier | Infrastructure | Notes |
|---|---|---|
| **Free** | Client-only (iPhone, Vision Pro) | Offline sample data |
| **Mac Local** | M1 Mini on LAN, Apache reverse proxy | `local_mac` mode |
| **Cloud** | S3 tiles + FastAPI container | `cloud` mode with CDN |

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint and format
ruff check spatial_agents/ tests/
ruff format spatial_agents/ tests/

# Type check
mypy spatial_agents/

# Run the demo pipeline (no server)
python scripts/demo.py

# Run demo with server
python scripts/demo.py --serve
```

## License

[MIT](LICENSE) — SpeckTech Inc.
