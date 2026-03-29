# Spatial Agents — Python Intelligence Layer

<!--
  README.md — Project documentation and quickstart guide.

  Covers architecture overview, deployment tiers, API surface,
  H3 tiling scheme, Foundation Models integration, causal reasoning
  framework, and development setup.

  Version History:
      0.1.0  2026-03-28  Initial README with architecture, API docs,
                         tiling spec, FM integration, and causal overview
      0.1.1  2026-03-28  Updated license to SpeckTech Inc.
-->

Geospatial data pipeline for the **Spatial Agents** ecosystem. Ingests real-time
maritime (AIS) and aviation (ADS-B) feeds, builds H3-indexed tile pyramids,
evaluates prompts against Apple's on-device Foundation Model, constructs
structural causal models (Pearl SCM), and serves intelligence to
iOS, macOS, and visionOS clients.

## Architecture

```
spatial_agents/
├── ingest/          # AIS NMEA, ADS-B, TLE feed parsers
├── spatial/         # H3 hexagonal indexing + tile generation
├── intelligence/    # FM prompt evaluation + token budget management
├── causal/          # Pearl SCM DAG construction + do-calculus
├── serving/         # FastAPI REST API + static tile server
└── deploy/          # Local Mac (M1 Mini) + cloud (S3) configs
```

## Deployment Tiers

| Tier | Infrastructure | Monetization |
|------|---------------|--------------|
| **Free** | Client-only (iPhone, Vision Pro) | App Store download |
| **Mac Local** | M1 Mini on LAN, port 8012 | One-time purchase |
| **Cloud** | S3 tiles + FastAPI | Subscription |

## Quick Start

```bash
# Clone and install
cd spatial-agents
pip install -e ".[dev]"

# Run locally (M1 Mini mode)
spatial-agents --port 8012

# Or directly
python -m spatial_agents.main
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/tiles/h3/{res}/{cell}/{bin}.json` | Static H3 tile |
| GET | `/api/vessels/{h3_cell}` | Live vessel positions |
| GET | `/api/aircraft/{h3_cell}` | Live aircraft positions |
| GET | `/api/intelligence/{h3_cell}` | FM situation report |
| GET | `/api/causal/{h3_cell}` | Causal graph |
| GET | `/api/budget` | Token budget status |
| GET | `/health` | Pipeline health |

## H3 Tiling

Multi-resolution hexagonal tiles (Uber H3, Apache 2.0):

| Resolution | Edge Length | Use Case | Temporal Bin |
|-----------|-------------|----------|--------------|
| 3 | 12.4 km | Global density heatmap | 1 day |
| 4 | 4.7 km | Shipping lanes / corridors | 1 hour |
| 5 | 1.8 km | Port approach | 5 min |
| 6 | 0.68 km | Harbor detail | 1 min |
| 7 | 0.26 km | Berth level | Live |

## Foundation Models Integration

Uses Apple's FM Python SDK (macOS) for prompt evaluation:

- `contextSize` — query available context window (4096 tokens)
- `tokenCount(for:)` — measure token consumption per component
- Schema validation against Swift `@Generable` struct definitions
- Regression testing across model versions (e.g., 26.4 update)

## Causal Reasoning

Pearl's structural causal model framework:

1. **Event Detection** — loitering, dark gaps, diversions, density anomalies
2. **DAG Construction** — domain knowledge rules + temporal ordering
3. **do-calculus** — counterfactual intervention queries
4. **FM Narration** — on-device natural language explanation

## Tests

```bash
pytest tests/ -v
```

## License

Proprietary — SpeckTech Inc.
