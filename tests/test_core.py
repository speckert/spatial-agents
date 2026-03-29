"""
Test Suite — Core tests for the Spatial Agents pipeline.

Run with: pytest tests/ -v

Version History:
    0.1.0  2026-03-28  Initial test suite
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spatial_agents.models import (
    AircraftCategory,
    AircraftRecord,
    CausalEdge,
    CausalGraph,
    CausalNode,
    DataDomain,
    GeoPosition,
    SituationReport,
    TileContent,
    TileMetadata,
    VesselRecord,
    VesselType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)


def make_vessel(
    mmsi: str = "367000001",
    lat: float = 37.8044,
    lng: float = -122.2712,
    speed: float = 8.5,
    vessel_type: VesselType = VesselType.CARGO,
) -> VesselRecord:
    return VesselRecord(
        mmsi=mmsi,
        name=f"VESSEL_{mmsi}",
        vessel_type=vessel_type,
        position=GeoPosition(lat=lat, lng=lng, timestamp=NOW),
        heading_deg=180.0,
        speed_knots=speed,
        course_deg=185.0,
        destination="OAKLAND",
    )


def make_aircraft(
    icao24: str = "A00001",
    lat: float = 37.6213,
    lng: float = -122.3790,
    alt: float = 3000.0,
) -> AircraftRecord:
    return AircraftRecord(
        icao24=icao24,
        callsign="UAL123",
        category=AircraftCategory.MEDIUM,
        position=GeoPosition(lat=lat, lng=lng, alt_m=alt, timestamp=NOW),
        velocity_knots=250.0,
        vertical_rate_fpm=-800.0,
        heading_deg=280.0,
        on_ground=False,
    )


# ---------------------------------------------------------------------------
# Model Tests
# ---------------------------------------------------------------------------

class TestModels:
    def test_vessel_record_creation(self) -> None:
        v = make_vessel()
        assert v.mmsi == "367000001"
        assert v.vessel_type == VesselType.CARGO
        assert v.position.lat == 37.8044
        assert v.speed_knots == 8.5

    def test_vessel_serialization(self) -> None:
        v = make_vessel()
        data = v.model_dump(mode="json")
        assert data["mmsi"] == "367000001"
        assert "position" in data
        roundtrip = VesselRecord.model_validate(data)
        assert roundtrip.mmsi == v.mmsi

    def test_aircraft_record_creation(self) -> None:
        a = make_aircraft()
        assert a.icao24 == "A00001"
        assert a.callsign == "UAL123"
        assert a.position.alt_m == 3000.0
        assert not a.on_ground

    def test_situation_report_schema(self) -> None:
        report = SituationReport(
            domain=DataDomain.MARITIME,
            h3_cell="842831dffffffff",
            summary="Normal traffic patterns observed in Oakland outer harbor.",
            key_observations=["12 cargo vessels transiting", "3 vessels at berth"],
            anomalies=[],
            confidence=0.85,
            generated_at=NOW,
        )
        assert report.confidence == 0.85
        assert len(report.key_observations) == 2
        data = report.model_dump(mode="json")
        assert data["domain"] == "maritime"

    def test_causal_graph_schema(self) -> None:
        graph = CausalGraph(
            h3_cell="842831dffffffff",
            nodes=[
                CausalNode(
                    id="e0",
                    label="Weather event",
                    domain=DataDomain.MARITIME,
                    event_type="weather_event",
                    observed_value=0.8,
                    timestamp=NOW,
                ),
            ],
            edges=[],
            generated_at=NOW,
        )
        assert len(graph.nodes) == 1
        assert graph.nodes[0].event_type == "weather_event"


# ---------------------------------------------------------------------------
# H3 Indexer Tests
# ---------------------------------------------------------------------------

class TestH3Indexer:
    def test_position_to_cells(self) -> None:
        from spatial_agents.spatial.h3_indexer import H3Indexer

        indexer = H3Indexer(resolutions=[3, 4, 5])
        cells = indexer.position_to_cells(37.8044, -122.2712)

        assert len(cells) == 3
        assert 3 in cells
        assert 4 in cells
        assert 5 in cells
        # All cells should be non-empty hex strings
        for res, cell in cells.items():
            assert len(cell) > 0

    def test_cell_center_roundtrip(self) -> None:
        from spatial_agents.spatial.h3_indexer import H3Indexer

        indexer = H3Indexer(resolutions=[5])
        cells = indexer.position_to_cells(37.8044, -122.2712)
        cell = cells[5]

        center = indexer.cell_to_center(cell)
        assert abs(center[0] - 37.8044) < 0.1  # Within ~10km
        assert abs(center[1] - (-122.2712)) < 0.1

    def test_neighbors(self) -> None:
        from spatial_agents.spatial.h3_indexer import H3Indexer

        indexer = H3Indexer(resolutions=[5])
        cells = indexer.position_to_cells(37.8044, -122.2712)
        cell = cells[5]

        neighbors = indexer.get_neighbors(cell)
        assert len(neighbors) == 6  # Hexagons always have 6 neighbors

    def test_cell_boundary_geojson(self) -> None:
        from spatial_agents.spatial.h3_indexer import H3Indexer

        indexer = H3Indexer(resolutions=[5])
        cells = indexer.position_to_cells(37.8044, -122.2712)
        cell = cells[5]

        geojson = indexer.cell_to_boundary_geojson(cell)
        assert geojson["type"] == "Polygon"
        assert len(geojson["coordinates"]) == 1
        ring = geojson["coordinates"][0]
        assert len(ring) == 7  # 6 vertices + closing vertex
        assert ring[0] == ring[-1]  # Ring is closed

    def test_bbox_to_cells(self) -> None:
        from spatial_agents.spatial.h3_indexer import H3Indexer

        indexer = H3Indexer()
        cells = indexer.bbox_to_cells(37.7, -122.5, 37.9, -122.1, 5)
        assert len(cells) > 0


# ---------------------------------------------------------------------------
# Temporal Binner Tests
# ---------------------------------------------------------------------------

class TestTemporalBinner:
    def test_bin_key_1hour(self) -> None:
        from spatial_agents.spatial.temporal_bins import bin_key

        dt = datetime(2026, 3, 28, 14, 37, 22, tzinfo=timezone.utc)
        key = bin_key(dt, "1hour")
        assert key == "20260328T140000"

    def test_bin_key_5min(self) -> None:
        from spatial_agents.spatial.temporal_bins import bin_key

        dt = datetime(2026, 3, 28, 14, 37, 22, tzinfo=timezone.utc)
        key = bin_key(dt, "5min")
        assert key == "20260328T143500"

    def test_bin_key_live(self) -> None:
        from spatial_agents.spatial.temporal_bins import bin_key

        dt = datetime(2026, 3, 28, 14, 37, 22, tzinfo=timezone.utc)
        key = bin_key(dt, "live")
        assert key == "live"


# ---------------------------------------------------------------------------
# Tile Builder Tests
# ---------------------------------------------------------------------------

class TestTileBuilder:
    def test_build_tile(self, tmp_path: Path) -> None:
        from spatial_agents.spatial.tile_builder import TileBuilder

        builder = TileBuilder(output_dir=tmp_path)
        vessels = [make_vessel()]

        path = builder.build_tile(
            cell_id="842831dffffffff",
            resolution=4,
            temporal_bin="1hour",
            vessels=vessels,
        )

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["metadata"]["cell_id"] == "842831dffffffff"
        assert data["metadata"]["vessel_count"] == 1
        assert len(data["vessels"]) == 1

    def test_build_tiles_for_records(self, tmp_path: Path) -> None:
        from spatial_agents.spatial.h3_indexer import H3Indexer
        from spatial_agents.spatial.tile_builder import TileBuilder

        indexer = H3Indexer(resolutions=[5])
        builder = TileBuilder(output_dir=tmp_path)

        # Create vessels with H3 assignments
        v1 = make_vessel(mmsi="001", lat=37.80, lng=-122.27)
        v1.h3_cells = indexer.position_to_cells(37.80, -122.27)

        v2 = make_vessel(mmsi="002", lat=37.81, lng=-122.26)
        v2.h3_cells = indexer.position_to_cells(37.81, -122.26)

        paths = builder.build_tiles_for_records([v1, v2], [], resolution=5)
        assert len(paths) >= 1  # At least one tile generated


# ---------------------------------------------------------------------------
# GeoJSON Export Tests
# ---------------------------------------------------------------------------

class TestGeoJSONExport:
    def test_vessel_to_feature(self) -> None:
        from spatial_agents.spatial.geojson_export import vessel_to_feature

        v = make_vessel()
        feature = vessel_to_feature(v)

        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] == "Point"
        coords = feature["geometry"]["coordinates"]
        assert coords[0] == v.position.lng
        assert coords[1] == v.position.lat
        assert feature["properties"]["entity_type"] == "vessel"

    def test_tile_to_geojson(self) -> None:
        from spatial_agents.spatial.geojson_export import tile_to_geojson

        tile = TileContent(
            metadata=TileMetadata(
                cell_id="842831dffffffff",
                resolution=4,
                temporal_bin="1hour",
                generated_at=NOW,
                vessel_count=1,
            ),
            vessels=[make_vessel()],
        )
        geojson = tile_to_geojson(tile)

        assert geojson["type"] == "FeatureCollection"
        # 1 cell boundary + 1 vessel = 2 features
        assert len(geojson["features"]) == 2


# ---------------------------------------------------------------------------
# Event Detector Tests
# ---------------------------------------------------------------------------

class TestEventDetector:
    def test_detect_loitering(self) -> None:
        from spatial_agents.causal.event_detector import EventDetector

        detector = EventDetector()

        # Create slow-moving vessels
        vessels = [
            make_vessel(mmsi="001", speed=1.5),
            make_vessel(mmsi="001", speed=1.2),
            make_vessel(mmsi="002", speed=0.8),
            make_vessel(mmsi="002", speed=0.5),
        ]

        events = detector.detect_loitering(vessels, "842831dffffffff")
        assert len(events) >= 1
        assert events[0].event_type == "vessel_loitering"

    def test_detect_ground_stops(self) -> None:
        from spatial_agents.causal.event_detector import EventDetector

        detector = EventDetector()

        # 8 grounded, 2 airborne = 80% grounded
        aircraft = [
            make_aircraft(icao24=f"A{i:05d}")
            for i in range(10)
        ]
        for a in aircraft[:8]:
            a.on_ground = True

        events = detector.detect_ground_stops(aircraft, "842831dffffffff")
        assert len(events) == 1
        assert events[0].event_type == "ground_stop_indicator"


# ---------------------------------------------------------------------------
# DAG Builder Tests
# ---------------------------------------------------------------------------

class TestDAGBuilder:
    def test_build_empty(self) -> None:
        from spatial_agents.causal.dag_builder import DAGBuilder

        builder = DAGBuilder()
        graph = builder.build([], "842831dffffffff")
        assert len(graph.nodes) == 0
        assert len(graph.edges) == 0

    def test_build_with_events(self) -> None:
        from datetime import timedelta
        from spatial_agents.causal.dag_builder import DAGBuilder
        from spatial_agents.causal.event_detector import DetectedEvent

        builder = DAGBuilder()
        events = [
            DetectedEvent(
                event_type="weather_event",
                domain=DataDomain.MARITIME,
                description="Storm approaching",
                entity_ids=[],
                h3_cell="842831dffffffff",
                timestamp=NOW,
                confidence=0.9,
                metrics={},
            ),
            DetectedEvent(
                event_type="vessel_loitering",
                domain=DataDomain.MARITIME,
                description="Vessel anchored due to weather",
                entity_ids=["367000001"],
                h3_cell="842831dffffffff",
                timestamp=NOW + timedelta(hours=2),
                confidence=0.7,
                metrics={"avg_speed_knots": 0.5},
            ),
        ]

        graph = builder.build(events, "842831dffffffff")
        assert len(graph.nodes) == 2
        # Should find the weather → loitering causal rule
        assert len(graph.edges) >= 1
        assert graph.edges[0].source.startswith("e0")
        assert graph.edges[0].target.startswith("e1")

    def test_root_causes(self) -> None:
        from spatial_agents.causal.dag_builder import DAGBuilder

        builder = DAGBuilder()
        graph = CausalGraph(
            h3_cell="test",
            nodes=[
                CausalNode(id="a", label="Root", domain=DataDomain.MARITIME,
                           event_type="weather_event", timestamp=NOW),
                CausalNode(id="b", label="Effect", domain=DataDomain.MARITIME,
                           event_type="vessel_loitering", timestamp=NOW),
            ],
            edges=[CausalEdge(source="a", target="b", strength=0.7, mechanism="test")],
            generated_at=NOW,
        )
        roots = builder.get_root_causes(graph)
        assert len(roots) == 1
        assert roots[0].id == "a"


# ---------------------------------------------------------------------------
# Intervention Engine Tests
# ---------------------------------------------------------------------------

class TestInterventionEngine:
    def test_counterfactual(self) -> None:
        from spatial_agents.causal.intervention import InterventionEngine

        engine = InterventionEngine()
        graph = CausalGraph(
            h3_cell="test",
            nodes=[
                CausalNode(id="a", label="Cause", domain=DataDomain.MARITIME,
                           event_type="weather_event", observed_value=0.9, timestamp=NOW),
                CausalNode(id="b", label="Effect", domain=DataDomain.MARITIME,
                           event_type="vessel_loitering", observed_value=0.7, timestamp=NOW),
            ],
            edges=[CausalEdge(source="a", target="b", strength=0.8, mechanism="weather causes delay")],
            generated_at=NOW,
        )

        result = engine.counterfactual(graph, "a")
        assert result.target_node == "a"
        assert result.intervened_value == 0.0
        assert "b" in result.affected_nodes


# ---------------------------------------------------------------------------
# Token Budget Tests
# ---------------------------------------------------------------------------

class TestTokenBudget:
    @pytest.mark.asyncio
    async def test_estimate_tokens(self) -> None:
        from spatial_agents.intelligence.token_budget import TokenBudgetManager

        budget = TokenBudgetManager(context_size=4096)
        tokens = await budget.count_tokens("Hello world, this is a test prompt.")
        assert tokens > 0
        assert tokens < 100

    @pytest.mark.asyncio
    async def test_budget_tracking(self) -> None:
        from spatial_agents.intelligence.token_budget import TokenBudgetManager

        budget = TokenBudgetManager(context_size=4096)
        await budget.set_instructions("You are a maritime analyst.")
        await budget.set_payload("Some vessel data here")

        report = budget.get_budget()
        assert report.context_window_size == 4096
        assert report.instructions_tokens > 0
        assert report.data_payload_tokens > 0
        assert report.remaining_tokens > 0
        assert report.utilization_pct < 1.0


# ---------------------------------------------------------------------------
# Schema Validator Tests
# ---------------------------------------------------------------------------

class TestSchemaValidator:
    def test_valid_situation_report(self) -> None:
        from spatial_agents.intelligence.schema_validator import SchemaValidator

        validator = SchemaValidator()
        data = {
            "domain": "maritime",
            "h3_cell": "842831dffffffff",
            "summary": "Normal traffic patterns in Oakland outer harbor area.",
            "key_observations": ["12 cargo vessels", "3 at berth"],
            "confidence": 0.85,
            "generated_at": NOW.isoformat(),
        }

        result = validator.validate(data, "SituationReport")
        assert result.valid

    def test_invalid_situation_report(self) -> None:
        from spatial_agents.intelligence.schema_validator import SchemaValidator

        validator = SchemaValidator()
        data = {"domain": "maritime"}  # Missing required fields

        result = validator.validate(data, "SituationReport")
        assert not result.valid
        assert len(result.errors) > 0


# ---------------------------------------------------------------------------
# Prompt Templates Tests
# ---------------------------------------------------------------------------

class TestPromptTemplates:
    def test_library_lookup(self) -> None:
        from spatial_agents.intelligence.prompt_templates import PromptLibrary

        library = PromptLibrary()
        template = library.get("maritime", "maritime_situation_report")
        assert template is not None
        assert template.version == "1.0.0"

    def test_template_rendering(self) -> None:
        from spatial_agents.intelligence.prompt_templates import PromptLibrary

        library = PromptLibrary()
        template = library.get("maritime", "maritime_situation_report")

        rendered = template.render_user_prompt(
            h3_cell="842831dffffffff",
            resolution=4,
            vessel_count=23,
            temporal_bin="1hour",
            activity_summary="12 cargo, 5 tanker, 3 tug",
        )
        assert "842831dffffffff" in rendered
        assert "23" in rendered
        assert "12 cargo" in rendered

    def test_list_domains(self) -> None:
        from spatial_agents.intelligence.prompt_templates import PromptLibrary

        library = PromptLibrary()
        domains = library.domains()
        assert "maritime" in domains
        assert "aviation" in domains
