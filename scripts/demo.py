#!/usr/bin/env python3
"""
Demo — End-to-end pipeline demonstration for interview presentation.

Runs the complete Spatial Agents pipeline with sample data:
    1. Load sample vessel and aircraft data (SF Bay Area)
    2. Assign H3 cells at multiple resolutions
    3. Generate tile files
    4. Detect behavioral events (loitering, dark gaps, ground stops)
    5. Build causal DAG from detected events
    6. Run do-calculus interventions
    7. Evaluate FM prompt token budgets
    8. Validate structured output schemas
    9. Start FastAPI server with pre-loaded data

Usage:
    python scripts/demo.py --help          # Show all options
    python scripts/demo.py                 # Full pipeline demo (no server)
    python scripts/demo.py --serve         # Demo + start server on port 8012
    python scripts/demo.py --verbose       # Debug output

Version History:
    0.1.0  2026-03-28  Initial demo script — full pipeline walkthrough
    0.1.1  2026-03-28  Updated usage documentation with --help reference
    0.1.2  2026-03-28  Updated banner to SpeckTech Inc.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Add parent to path for direct script execution
sys.path.insert(0, str(Path(__file__).parent.parent))


def section(title: str) -> None:
    """Print a section header."""
    width = 64
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)
    print()


def subsection(title: str) -> None:
    """Print a subsection header."""
    print(f"\n  --- {title} ---\n")


async def run_demo(serve: bool = False, verbose: bool = False) -> None:
    """Execute the full pipeline demo."""

    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(name)s: %(message)s")

    # -----------------------------------------------------------------------
    section("1. SAMPLE DATA — San Francisco Bay Area")
    # -----------------------------------------------------------------------

    from spatial_agents.data import (
        SAMPLE_VESSELS,
        SAMPLE_AIRCRAFT,
        SAMPLE_DARK_GAP_TRACK,
        load_sample_data,
    )

    print(f"  Vessels loaded:     {len(SAMPLE_VESSELS)}")
    print(f"  Aircraft loaded:    {len(SAMPLE_AIRCRAFT)}")
    print(f"  Dark gap track:     {SAMPLE_DARK_GAP_TRACK.name} "
          f"({len(SAMPLE_DARK_GAP_TRACK.positions)} positions)")

    subsection("Vessel Summary")
    from collections import Counter
    type_counts = Counter(v.vessel_type.value for v in SAMPLE_VESSELS)
    for vtype, count in type_counts.most_common():
        print(f"    {vtype:>12s}: {count}")

    subsection("Aircraft Summary")
    airborne = sum(1 for a in SAMPLE_AIRCRAFT if not a.on_ground)
    grounded = len(SAMPLE_AIRCRAFT) - airborne
    print(f"    Airborne:  {airborne}")
    print(f"    On ground: {grounded}")

    # -----------------------------------------------------------------------
    section("2. H3 SPATIAL INDEXING — Multi-Resolution Cell Assignment")
    # -----------------------------------------------------------------------

    from spatial_agents.spatial.h3_indexer import H3Indexer

    indexer = H3Indexer()

    # Show cell assignments for Oakland harbor
    oakland_lat, oakland_lng = 37.7955, -122.2790
    cells = indexer.position_to_cells(oakland_lat, oakland_lng)
    print(f"  Position: Oakland Outer Harbor ({oakland_lat}, {oakland_lng})")
    print()
    for res, cell_id in sorted(cells.items()):
        edge = indexer.edge_length_km(res)
        center = indexer.cell_to_center(cell_id)
        print(f"    res {res}: {cell_id}  (edge: {edge:.2f} km, "
              f"center: {center[0]:.4f}, {center[1]:.4f})")

    subsection("Bounding Box Query — SF Bay")
    bay_cells = indexer.bbox_to_cells(37.7, -122.5, 37.9, -122.1, 5)
    print(f"    Resolution 5 cells covering SF Bay: {len(bay_cells)}")

    # -----------------------------------------------------------------------
    section("3. TILE GENERATION — H3 Tile Pyramid")
    # -----------------------------------------------------------------------

    from spatial_agents.spatial.tile_builder import TileBuilder

    tile_dir = Path(tempfile.mkdtemp(prefix="spatial_agents_tiles_"))
    builder = TileBuilder(output_dir=tile_dir)

    all_paths = builder.build_all_resolutions(SAMPLE_VESSELS, SAMPLE_AIRCRAFT)
    total_tiles = sum(len(paths) for paths in all_paths.values())

    print(f"  Tile output dir: {tile_dir}")
    print(f"  Total tiles generated: {total_tiles}")
    print()
    for res, paths in sorted(all_paths.items()):
        print(f"    Resolution {res}: {len(paths)} tiles")

    subsection("Sample Tile Content")
    if all_paths.get(5):
        sample_path = all_paths[5][0]
        sample_data = json.loads(sample_path.read_text())
        meta = sample_data["metadata"]
        print(f"    File:      {sample_path.name}")
        print(f"    Cell:      {meta['cell_id']}")
        print(f"    Vessels:   {meta['vessel_count']}")
        print(f"    Aircraft:  {meta['aircraft_count']}")
        print(f"    Size:      {sample_path.stat().st_size:,} bytes")

    # -----------------------------------------------------------------------
    section("4. GEOJSON EXPORT — MapKit-Ready Output")
    # -----------------------------------------------------------------------

    from spatial_agents.spatial.geojson_export import tile_to_geojson
    from spatial_agents.models import TileContent

    if all_paths.get(5):
        tile_data = json.loads(all_paths[5][0].read_text())
        tile = TileContent(**tile_data)
        geojson = tile_to_geojson(tile)

        print(f"  FeatureCollection: {len(geojson['features'])} features")
        for feat in geojson["features"][:5]:
            etype = feat["properties"]["entity_type"]
            if etype == "h3_cell":
                print(f"    [polygon] H3 cell boundary")
            elif etype == "vessel":
                name = feat["properties"].get("name", "?")
                print(f"    [point]   vessel: {name}")
            elif etype == "aircraft":
                cs = feat["properties"].get("callsign", "?")
                print(f"    [point]   aircraft: {cs}")

    # -----------------------------------------------------------------------
    section("5. EVENT DETECTION — Behavioral Pattern Analysis")
    # -----------------------------------------------------------------------

    from spatial_agents.causal.event_detector import EventDetector

    detector = EventDetector()
    events = detector.detect_all(
        vessels=SAMPLE_VESSELS,
        aircraft=SAMPLE_AIRCRAFT,
        h3_cell="842831dffffffff",
        tracks=[SAMPLE_DARK_GAP_TRACK],
    )

    print(f"  Events detected: {len(events)}")
    print()
    for event in events:
        print(f"    [{event.event_type}]")
        print(f"      {event.description}")
        print(f"      confidence: {event.confidence:.2f}")
        print(f"      entities: {event.entity_ids[:3]}")
        print()

    # -----------------------------------------------------------------------
    section("6. CAUSAL DAG — Pearl Structural Causal Model")
    # -----------------------------------------------------------------------

    from spatial_agents.causal.dag_builder import DAGBuilder

    dag_builder = DAGBuilder()
    graph = dag_builder.build(events, "842831dffffffff")

    print(f"  Nodes: {len(graph.nodes)}")
    print(f"  Edges: {len(graph.edges)}")
    print()

    subsection("Causal Nodes")
    for node in graph.nodes:
        print(f"    {node.id}: {node.label} (conf: {node.observed_value})")

    subsection("Causal Edges")
    for edge in graph.edges:
        print(f"    {edge.source} → {edge.target}")
        print(f"      strength: {edge.strength}, mechanism: {edge.mechanism}")

    subsection("Root Causes")
    roots = dag_builder.get_root_causes(graph)
    for root in roots:
        print(f"    {root.id}: {root.label}")

    # -----------------------------------------------------------------------
    section("7. DO-CALCULUS — Counterfactual Interventions")
    # -----------------------------------------------------------------------

    from spatial_agents.causal.intervention import InterventionEngine

    engine = InterventionEngine()

    for root in roots[:3]:
        result = engine.counterfactual(graph, root.id)
        print(f"  Intervention: {result.intervention}")
        print(f"    Original value:  {result.original_value}")
        print(f"    Forced to:       {result.intervened_value}")
        print(f"    Affected nodes:  {result.affected_nodes}")
        print(f"    Causal path:     {' → '.join(result.causal_path)}")
        print()

    # -----------------------------------------------------------------------
    section("8. GRAPH SERIALIZATION — FM-Ready Payloads")
    # -----------------------------------------------------------------------

    from spatial_agents.causal.graph_serializer import GraphSerializer

    serializer = GraphSerializer()

    subsection("Compact Format (for FM context window)")
    compact = serializer.to_compact(graph, max_nodes=8)
    compact_json = json.dumps(compact, indent=2, default=str)
    print(f"  Nodes: {len(compact['nodes'])}")
    print(f"  Edges: {len(compact['edges'])}")
    print(f"  JSON size: {len(compact_json)} chars (~{len(compact_json)//4} tokens)")

    subsection("FM Prompt Payload")
    interventions = [engine.counterfactual(graph, r.id) for r in roots[:2]]
    fm_text = serializer.to_fm_payload(graph, interventions)
    print(fm_text)
    print(f"\n  Payload: {len(fm_text)} chars (~{len(fm_text)//4} tokens)")

    # -----------------------------------------------------------------------
    section("9. TOKEN BUDGET — Context Window Management")
    # -----------------------------------------------------------------------

    from spatial_agents.intelligence.token_budget import TokenBudgetManager
    from spatial_agents.intelligence.prompt_templates import PromptLibrary

    budget = TokenBudgetManager(context_size=4096)
    library = PromptLibrary()

    template = library.get("maritime", "maritime_situation_report")
    await budget.set_instructions(template.system_instructions)

    # Simulate tool schema cost
    tool_schema = '{"name":"generateMood","description":"...","arguments":{"type":"object"}}'
    await budget.set_tool_schemas(tool_schema)

    await budget.set_payload(fm_text)

    report = budget.get_budget()
    print(f"  Context window:     {report.context_window_size} tokens")
    print(f"  Instructions:       {report.instructions_tokens} tokens")
    print(f"  Tool schemas:       {report.tool_schema_tokens} tokens")
    print(f"  Data payload:       {report.data_payload_tokens} tokens")
    print(f"  Remaining:          {report.remaining_tokens} tokens")
    print(f"  Utilization:        {report.utilization_pct:.1%}")

    # -----------------------------------------------------------------------
    section("10. SCHEMA VALIDATION — @Generable Compatibility")
    # -----------------------------------------------------------------------

    from spatial_agents.intelligence.schema_validator import SchemaValidator

    validator = SchemaValidator()

    subsection("Valid SituationReport")
    valid_report = {
        "domain": "maritime",
        "h3_cell": "842831dffffffff",
        "summary": "Oakland outer harbor showing normal cargo operations with "
                   "two vessels at berth and one inbound. Tug activity consistent "
                   "with expected docking support. One vessel loitering near anchorage.",
        "key_observations": [
            "3 cargo vessels — 2 at berth, 1 approaching",
            "2 tugs actively assisting docking operations",
            "1 cargo vessel loitering near anchorage for 30+ minutes",
            "Recreational sailing traffic moderate in central bay",
        ],
        "anomalies": [
            "Vessel UNKNOWN BULK showing sustained loitering pattern",
        ],
        "causal_narrative": "Loitering likely caused by berth unavailability.",
        "confidence": 0.82,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    result = validator.validate(valid_report, "SituationReport")
    print(f"  Result: {result.summary}")
    print(f"  Errors: {result.errors}")
    print(f"  Warnings: {result.warnings}")

    subsection("Invalid SituationReport (missing fields)")
    invalid_report = {"domain": "maritime", "summary": "test"}
    result = validator.validate(invalid_report, "SituationReport")
    print(f"  Result: {result.summary}")
    for error in result.errors:
        print(f"    - {error}")

    # -----------------------------------------------------------------------
    section("11. EVAL HARNESS — Prompt Regression Suite")
    # -----------------------------------------------------------------------

    from spatial_agents.intelligence.eval_harness import EvalHarness, MARITIME_BASIC_SUITE

    harness = EvalHarness()
    results = await harness.run_suite(MARITIME_BASIC_SUITE)
    report_data = harness.generate_report(results)

    print(f"  Suite: {MARITIME_BASIC_SUITE.name}")
    print(f"  Mode: {report_data['summary']['mode']}")
    print(f"  Cases: {report_data['summary']['total']}")
    print(f"  Avg prompt tokens: {report_data['performance']['avg_prompt_tokens']}")
    print()
    for r in report_data["results"]:
        status = "PASS" if r["passed"] else "SKIP"  # dry-run = no FM output
        print(f"    [{status}] {r['case']} — {r['tokens']} tokens")

    # -----------------------------------------------------------------------
    section("12. PIPELINE HEALTH — System Status")
    # -----------------------------------------------------------------------

    from spatial_agents.data import load_sample_data

    manager = load_sample_data()
    health = manager.health()

    print(f"  Vessels tracked:  {len(manager.get_latest_vessels())}")
    print(f"  Aircraft tracked: {len(manager.get_latest_aircraft())}")
    print()
    for feed in health:
        status = "connected" if feed.connected else "disconnected"
        rate = f"{feed.messages_per_minute:.1f} msg/min" if feed.messages_per_minute else "n/a"
        print(f"    [{status}] {feed.name} — {rate}")

    # -----------------------------------------------------------------------
    section("DEMO COMPLETE")
    # -----------------------------------------------------------------------

    print(f"  Tiles on disk:    {total_tiles} files")
    print(f"  Events detected:  {len(events)}")
    print(f"  Causal nodes:     {len(graph.nodes)}")
    print(f"  Causal edges:     {len(graph.edges)}")
    print(f"  Token utilization: {budget.utilization_pct:.1%}")
    print()
    print(f"  Tile directory:   {tile_dir}")
    print()

    if serve:
        section("STARTING SERVER — http://0.0.0.0:8012")

        from spatial_agents.serving.routes_api import set_feed_manager as set_api_feeds
        from spatial_agents.serving.routes_health import set_feed_manager as set_health_feeds

        set_api_feeds(manager)
        set_health_feeds(manager)

        # Start live feeds (AIS WebSocket if key is set, ADS-B polling)
        await manager.start()

        import uvicorn
        config = uvicorn.Config(
            "spatial_agents.serving.app:app",
            host="0.0.0.0",
            port=8012,
            log_level="info",
        )
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            await manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Spatial Agents Pipeline Demo")
    parser.add_argument("--serve", action="store_true", help="Start server after demo")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║     Spatial Agents — Pipeline Demonstration         ║")
    print("  ║     SpeckTech Inc.                                  ║")
    print("  ╚══════════════════════════════════════════════════════╝")

    asyncio.run(run_demo(serve=args.serve, verbose=args.verbose))


if __name__ == "__main__":
    main()
