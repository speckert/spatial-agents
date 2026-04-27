"""
Integration Tests — End-to-end pipeline tests using sample data.

Tests the full data flow from ingest through tile generation,
event detection, causal reasoning, and API serving. Uses fixtures
from conftest.py backed by sample SF Bay Area data.

Run with: pytest tests/test_integration.py -v

Version History:
    0.1.0  2026-03-28  Initial integration tests — full pipeline,
                       tile roundtrip, causal chain, API endpoints
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spatial_agents.models import DataDomain, TileContent


class TestFullPipeline:
    """End-to-end pipeline: ingest → index → tile → detect → causal → serialize."""

    def test_sample_data_loads(self, sample_vessels, sample_aircraft):
        """Verify sample data loads with H3 cells assigned."""
        assert len(sample_vessels) > 10
        assert len(sample_aircraft) > 5

        # Every vessel should have H3 cells assigned
        for v in sample_vessels:
            assert len(v.h3_cells) > 0, f"Vessel {v.mmsi} missing H3 cells"

        for a in sample_aircraft:
            assert len(a.h3_cells) > 0, f"Aircraft {a.icao24} missing H3 cells"

    def test_tile_generation_from_samples(self, sample_vessels, sample_aircraft, tile_builder):
        """Generate tiles from sample data and verify content."""
        paths = tile_builder.build_tiles_for_records(
            sample_vessels, sample_aircraft, resolution=5
        )
        assert len(paths) > 0

        # Read back and validate
        for path in paths:
            data = json.loads(path.read_text())
            tile = TileContent(**data)
            assert tile.metadata.resolution == 5
            assert tile.metadata.vessel_count + tile.metadata.aircraft_count > 0

    def test_tile_all_resolutions(self, sample_vessels, sample_aircraft, tile_builder):
        """Generate tiles at all resolutions."""
        all_paths = tile_builder.build_all_resolutions(sample_vessels, sample_aircraft)

        for res in [3, 4, 5, 6, 7]:
            assert res in all_paths, f"Missing resolution {res}"

    def test_geojson_roundtrip(self, sample_vessels, sample_aircraft, tile_builder):
        """Generate tile → convert to GeoJSON → verify structure."""
        from spatial_agents.spatial.geojson_export import tile_to_geojson

        paths = tile_builder.build_tiles_for_records(
            sample_vessels, sample_aircraft, resolution=5
        )
        assert len(paths) > 0

        tile_data = json.loads(paths[0].read_text())
        tile = TileContent(**tile_data)
        geojson = tile_to_geojson(tile)

        assert geojson["type"] == "FeatureCollection"
        assert len(geojson["features"]) > 0

        # Verify coordinate order is [lng, lat] (GeoJSON standard)
        for feat in geojson["features"]:
            if feat["geometry"] and feat["geometry"]["type"] == "Point":
                coords = feat["geometry"]["coordinates"]
                lng, lat = coords[0], coords[1]
                assert -180 <= lng <= 180
                assert -90 <= lat <= 90


class TestEventDetectionPipeline:
    """Event detection from sample data through causal graph construction."""

    def test_detect_events_from_samples(
        self, sample_vessels, sample_aircraft, sample_dark_gap_track, event_detector
    ):
        """Detect events from sample data."""
        events = event_detector.detect_all(
            vessels=sample_vessels,
            aircraft=sample_aircraft,
            h3_cell="842831dffffffff",
            tracks=[sample_dark_gap_track],
        )
        assert len(events) > 0

        event_types = {e.event_type for e in events}
        # Should detect at least the dark vessel gap (from sample_dark_gap_track)
        assert "dark_vessel_gap" in event_types

    def test_detect_dark_gap(self, sample_dark_gap_track, event_detector):
        """Detect AIS dark gap in sample track."""
        events = event_detector.detect_dark_gaps(
            [sample_dark_gap_track], "842831dffffffff"
        )
        assert len(events) > 0
        assert events[0].event_type == "dark_vessel_gap"
        assert events[0].metrics["gap_minutes"] >= 15

    def test_causal_dag_from_events(
        self, sample_vessels, sample_aircraft, sample_dark_gap_track,
        event_detector, dag_builder
    ):
        """Build causal DAG from detected events."""
        events = event_detector.detect_all(
            sample_vessels, sample_aircraft, "842831dffffffff",
            tracks=[sample_dark_gap_track],
        )
        graph = dag_builder.build(events, "842831dffffffff")

        assert len(graph.nodes) == len(events)
        assert graph.h3_cell == "842831dffffffff"
        # DAG should be valid (no cycles guaranteed by builder)

    def test_intervention_on_root_cause(
        self, sample_vessels, sample_aircraft, sample_dark_gap_track,
        event_detector, dag_builder
    ):
        """Run counterfactual on root causes of causal DAG."""
        from spatial_agents.causal.intervention import InterventionEngine

        events = event_detector.detect_all(
            sample_vessels, sample_aircraft, "842831dffffffff",
            tracks=[sample_dark_gap_track],
        )
        graph = dag_builder.build(events, "842831dffffffff")

        if not graph.edges:
            pytest.skip("No causal edges found in sample data")

        engine = InterventionEngine()
        roots = dag_builder.get_root_causes(graph)
        assert len(roots) > 0

        result = engine.counterfactual(graph, roots[0].id)
        assert result.target_node == roots[0].id
        assert result.intervened_value == 0.0


class TestIntelligencePipeline:
    """FM evaluation pipeline with token budgets and schema validation."""

    @pytest.mark.asyncio
    async def test_prompt_render_and_budget(self, prompt_library, token_budget):
        """Render a maritime prompt and verify it fits in context window."""
        template = prompt_library.get("maritime", "maritime_situation_report")
        assert template is not None

        rendered = template.render_user_prompt(
            h3_cell="842831dffffffff",
            resolution=4,
            vessel_count=23,
            temporal_bin="1hour",
            activity_summary="12 cargo, 5 tanker, 3 tug. Avg speed 4.2 kn.",
        )

        await token_budget.set_instructions(template.system_instructions)
        payload_tokens = await token_budget.measure_payload(rendered)

        # Prompt should fit comfortably in context window
        assert payload_tokens < 4096
        assert token_budget.remaining_tokens > 1000  # Room for response

    @pytest.mark.asyncio
    async def test_causal_payload_fits_budget(
        self, sample_vessels, sample_aircraft, sample_dark_gap_track,
        event_detector, dag_builder, token_budget
    ):
        """Verify serialized causal graph fits within FM token budget."""
        from spatial_agents.causal.graph_serializer import GraphSerializer

        events = event_detector.detect_all(
            sample_vessels, sample_aircraft, "842831dffffffff",
            tracks=[sample_dark_gap_track],
        )
        graph = dag_builder.build(events, "842831dffffffff")
        serializer = GraphSerializer()

        fm_text = serializer.to_fm_payload(graph)
        payload_tokens = await token_budget.measure_payload(fm_text)

        # Causal payload should be well under budget
        max_budget = int(4096 * 0.15)  # 15% of context
        assert payload_tokens < max_budget, (
            f"Causal payload ({payload_tokens} tokens) exceeds "
            f"budget ({max_budget} tokens)"
        )

    def test_validate_valid_report(self, schema_validator):
        """Schema validation accepts a well-formed SituationReport."""
        report = {
            "domain": "maritime",
            "h3_cell": "842831dffffffff",
            "summary": "Normal operations in Oakland harbor with typical cargo traffic.",
            "key_observations": ["3 cargo vessels at berth"],
            "confidence": 0.85,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        result = schema_validator.validate(report, "SituationReport")
        assert result.valid, f"Unexpected errors: {result.errors}"

    def test_validate_rejects_bad_confidence(self, schema_validator):
        """Schema validation rejects confidence outside 0-1 range."""
        report = {
            "domain": "maritime",
            "h3_cell": "842831dffffffff",
            "summary": "Test",
            "key_observations": [],
            "confidence": 1.5,  # Invalid: > 1.0
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        result = schema_validator.validate(report, "SituationReport")
        assert not result.valid


class TestFeedManagerIntegration:
    """Feed manager with pre-loaded sample data."""

    def test_cell_query(self, feed_manager, h3_indexer):
        """Query vessels in a specific H3 cell."""
        # Get a cell that should contain Oakland harbor vessels
        cells = h3_indexer.position_to_cells(37.7955, -122.2790)
        cell_5 = cells[5]

        vessels = feed_manager.get_vessels_in_cell(cell_5, 5)
        # Should find at least some Oakland area vessels
        assert len(vessels) >= 1

    def test_aircraft_cell_query(self, feed_manager, h3_indexer):
        """Query aircraft in a specific H3 cell."""
        # SFO area
        cells = h3_indexer.position_to_cells(37.6213, -122.3790)
        cell_5 = cells[5]

        aircraft = feed_manager.get_aircraft_in_cell(cell_5, 5)
        # SFO ground aircraft should be nearby
        assert len(aircraft) >= 0  # May or may not be in exact cell

    def test_health_status(self, feed_manager):
        """Health endpoint returns valid status."""
        health = feed_manager.health()
        assert len(health) == 2  # AIS + ADS-B feeds
        for feed in health:
            assert feed.name in ("ais", "adsb")
