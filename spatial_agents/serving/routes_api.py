"""
API Routes — Dynamic query endpoints for live data and intelligence.

Version History:
    0.1.0  2026-03-28  Initial API routes
    0.2.0  2026-03-31  Added track history and track_points to vessel and
                       aircraft endpoint responses
    0.3.0  2026-04-02  Typed Pydantic response models for OpenAPI spec
                       generation with full field documentation
    0.4.0  2026-04-09  Added /vessels and /aircraft bbox endpoints (return all
                       entities without H3 cell queries)
    0.5.0  2026-04-25  Optional ?region=<name> filter on /vessels and
                       /aircraft. Filters by entity h3_cells[4] membership
                       against REGION_CELLS. Absent = unfiltered (all
                       active regions). Canonical pattern for clients that
                       display one region at a time — Claude 4.7
    0.6.0  2026-04-25  Added /causal/layer endpoint — geographic causal
                       DAG over all active feeds (vessels, aircraft,
                       weather alerts, TFRs). Optional ?region= filter
                       follows the canonical pattern — Claude 4.7
    0.6.1  2026-04-25  /causal/layer pulls vessels from the 15-min
                       rolling buffer (get_recent_vessels) instead of
                       the latest-snapshot map, so loitering / dark-gap
                       detectors have enough observations — Claude 4.7
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from spatial_agents.causal.dag_builder import DAGBuilder
from spatial_agents.causal.event_detector import EventDetector
from spatial_agents.causal.graph_serializer import GraphSerializer
from spatial_agents.causal.intervention import InterventionEngine
from spatial_agents.config import ACTIVE_REGIONS, REGION_CELLS
from spatial_agents.intelligence.token_budget import TokenBudgetManager
from spatial_agents.models import (
    AircraftResponse,
    AircraftWithTrack,
    CausalEmptyResponse,
    CausalLayerResponse,
    DataDomain,
    IntelligenceResponse,
    TokenBudget,
    VesselResponse,
    VesselWithTrack,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level instances (initialized once, reused across requests)
_event_detector = EventDetector()
_dag_builder = DAGBuilder()
_intervention_engine = InterventionEngine()
_graph_serializer = GraphSerializer()
_budget_manager = TokenBudgetManager()

# Lazy reference to feed manager — set during startup
_feed_manager = None


def set_feed_manager(manager: Any) -> None:
    """Set the feed manager reference (called during app startup)."""
    global _feed_manager
    _feed_manager = manager


def _region_cell_set(region: str) -> set[str] | None:
    """Return the res-4 cell set for a region (primary + 6 buffers).

    Returns None if the region is unknown — callers should treat that as
    a 400 Bad Request. Returns an empty set if the region exists but
    somehow has no cells (defensive; shouldn't happen).
    """
    cells = REGION_CELLS.get(region)
    if cells is None:
        return None
    primary = cells.get("primary")
    buffer = cells.get("buffer") or []
    out: set[str] = set()
    if primary:
        out.add(str(primary))
    for c in buffer:  # type: ignore[union-attr]
        out.add(str(c))
    return out


@router.get("/vessels", response_model=VesselResponse)
async def get_all_vessels(
    region: str | None = Query(
        default=None,
        description="Optional region name (e.g. san_francisco, boston) — "
                    "filters vessels to those in the region's 7-cell H3 tile. "
                    "Absent = all active regions.",
    ),
) -> VesselResponse:
    """Return all live vessel positions, optionally filtered to one region."""
    if _feed_manager is None:
        raise HTTPException(503, "Feed manager not initialized")

    vessels = _feed_manager.get_latest_vessels()
    if region is not None:
        cell_set = _region_cell_set(region)
        if cell_set is None:
            raise HTTPException(400, f"Unknown region: {region}")
        vessels = [v for v in vessels if v.h3_cells.get(4) in cell_set]

    vessel_list = []
    for v in vessels:
        track = _feed_manager.get_vessel_track(v.mmsi)
        vessel_list.append(VesselWithTrack(
            **v.model_dump(),
            track=track,
            track_points=len(track),
        ))
    return VesselResponse(
        h3_cell=region or "all",
        resolution=4 if region else 0,
        count=len(vessels),
        vessels=vessel_list,
        timestamp=datetime.now(timezone.utc),
    )


@router.get("/aircraft", response_model=AircraftResponse)
async def get_all_aircraft(
    region: str | None = Query(
        default=None,
        description="Optional region name (e.g. san_francisco, boston) — "
                    "filters aircraft to those in the region's 7-cell H3 tile. "
                    "Absent = all active regions.",
    ),
) -> AircraftResponse:
    """Return all live aircraft positions, optionally filtered to one region."""
    if _feed_manager is None:
        raise HTTPException(503, "Feed manager not initialized")

    aircraft = _feed_manager.get_latest_aircraft()
    if region is not None:
        cell_set = _region_cell_set(region)
        if cell_set is None:
            raise HTTPException(400, f"Unknown region: {region}")
        aircraft = [a for a in aircraft if a.h3_cells.get(4) in cell_set]

    aircraft_list = []
    for a in aircraft:
        track = _feed_manager.get_aircraft_track(a.icao24)
        aircraft_list.append(AircraftWithTrack(
            **a.model_dump(),
            track=track,
            track_points=len(track),
        ))
    return AircraftResponse(
        h3_cell=region or "all",
        resolution=4 if region else 0,
        count=len(aircraft),
        aircraft=aircraft_list,
        timestamp=datetime.now(timezone.utc),
    )


@router.get("/vessels/{h3_cell}", response_model=VesselResponse)
async def get_vessels(
    h3_cell: str,
    resolution: int = Query(default=5, ge=0, le=15),
) -> VesselResponse:
    """
    Return live vessel positions within an H3 cell.

    Each vessel includes current position, heading, speed, vessel type,
    and a position history trail (up to 5 points) for rendering movement.
    """
    if _feed_manager is None:
        raise HTTPException(503, "Feed manager not initialized")

    vessels = _feed_manager.get_vessels_in_cell(h3_cell, resolution)
    vessel_list = []
    for v in vessels:
        track = _feed_manager.get_vessel_track(v.mmsi)
        vessel_list.append(VesselWithTrack(
            **v.model_dump(),
            track=track,
            track_points=len(track),
        ))
    return VesselResponse(
        h3_cell=h3_cell,
        resolution=resolution,
        count=len(vessels),
        vessels=vessel_list,
        timestamp=datetime.now(timezone.utc),
    )


@router.get("/aircraft/{h3_cell}", response_model=AircraftResponse)
async def get_aircraft(
    h3_cell: str,
    resolution: int = Query(default=5, ge=0, le=15),
) -> AircraftResponse:
    """
    Return live aircraft positions within an H3 cell.

    Each aircraft includes position, velocity, altitude, flight_phase
    (server-classified via state machine), and a position history trail.
    """
    if _feed_manager is None:
        raise HTTPException(503, "Feed manager not initialized")

    aircraft = _feed_manager.get_aircraft_in_cell(h3_cell, resolution)
    aircraft_list = []
    for a in aircraft:
        track = _feed_manager.get_aircraft_track(a.icao24)
        aircraft_list.append(AircraftWithTrack(
            **a.model_dump(),
            track=track,
            track_points=len(track),
        ))
    return AircraftResponse(
        h3_cell=h3_cell,
        resolution=resolution,
        count=len(aircraft),
        aircraft=aircraft_list,
        timestamp=datetime.now(timezone.utc),
    )


@router.get("/intelligence/{h3_cell}", response_model=IntelligenceResponse)
async def get_intelligence(
    h3_cell: str,
    resolution: int = Query(default=5, ge=0, le=15),
    domain: str = Query(default="maritime"),
) -> IntelligenceResponse:
    """
    Return FM-evaluated situation report for an H3 cell.

    In production, this triggers on-device FM evaluation.
    Currently returns the structured prompt payload and token budget
    that would be sent to the FM.
    """
    if _feed_manager is None:
        raise HTTPException(503, "Feed manager not initialized")

    vessels = _feed_manager.get_vessels_in_cell(h3_cell, resolution)
    aircraft = _feed_manager.get_aircraft_in_cell(h3_cell, resolution)

    # Build activity summary for the prompt
    summary_parts: list[str] = []
    if vessels:
        type_counts: dict[str, int] = {}
        speeds: list[float] = []
        for v in vessels:
            type_counts[v.vessel_type.value] = type_counts.get(v.vessel_type.value, 0) + 1
            if v.speed_knots is not None:
                speeds.append(v.speed_knots)

        type_str = ", ".join(f"{c} {t}" for t, c in sorted(type_counts.items(), key=lambda x: -x[1]))
        avg_speed = sum(speeds) / len(speeds) if speeds else 0
        summary_parts.append(f"Vessels: {type_str}. Avg speed: {avg_speed:.1f} kn.")

    if aircraft:
        airborne = sum(1 for a in aircraft if not a.on_ground)
        grounded = len(aircraft) - airborne
        summary_parts.append(f"Aircraft: {airborne} airborne, {grounded} on ground.")

    activity_summary = " ".join(summary_parts) or "No activity detected."

    # Token budget analysis
    payload_text = f"Cell: {h3_cell}, Res: {resolution}\n{activity_summary}"
    payload_tokens = await _budget_manager.measure_payload(payload_text)
    budget = _budget_manager.get_budget()

    return IntelligenceResponse(
        h3_cell=h3_cell,
        resolution=resolution,
        domain=domain,
        activity_summary=activity_summary,
        vessel_count=len(vessels),
        aircraft_count=len(aircraft),
        token_budget=budget,
        payload_tokens=payload_tokens,
        note="FM evaluation pending — shows prompt payload and budget",
        timestamp=datetime.now(timezone.utc),
    )


@router.get("/causal/layer", response_model=CausalLayerResponse)
async def get_causal_layer(
    region: str | None = Query(
        default=None,
        description="Optional region name (e.g. san_francisco, boston). "
                    "Filters vessels/aircraft to that region's 7-cell tile "
                    "and weather/TFR events to those intersecting the region. "
                    "Absent = run across all active regions.",
    ),
) -> CausalLayerResponse:
    """
    Return a geographically-positioned causal DAG suitable for rendering
    as a map layer.

    Lifts the four live feeds — vessels, aircraft, NWS weather alerts,
    FAA TFRs — into a structural causal graph:

      * weather_alert and tfr_active nodes are exogenous roots positioned
        at their polygon centroid.
      * vessel_loitering, dark_vessel_gap, ground_stop_indicator, and
        density_anomaly nodes are downstream effects positioned at the
        affected entity's location.
      * Edges connect causes to effects via the domain rule engine
        (Pearl-style structural model).

    Every node carries lat/lng so a client can draw the DAG over the
    same map that shows the underlying entities and polygons.
    """
    if _feed_manager is None:
        raise HTTPException(503, "Feed manager not initialized")

    if region is not None:
        if _region_cell_set(region) is None:
            raise HTTPException(400, f"Unknown region: {region}")
        regions_to_run = [region]
    else:
        regions_to_run = list(ACTIVE_REGIONS)

    all_alerts = _feed_manager.get_latest_alerts()
    all_tfrs = _feed_manager.get_latest_tfrs()
    # Causal detection (loitering, dark gap) needs repeated observations
    # per vessel, not just the latest snapshot. Pull a rolling window from
    # the buffer instead. Aircraft uses latest — ground-stop is a snapshot
    # detector, not a temporal one.
    all_vessels = _feed_manager.get_recent_vessels(within_minutes=15)
    all_aircraft = _feed_manager.get_latest_aircraft()

    nodes_out = []
    edges_out = []

    for r in regions_to_run:
        cs = _region_cell_set(r)
        if not cs:
            continue
        vessels = [v for v in all_vessels if v.h3_cells.get(4) in cs]
        aircraft = [a for a in all_aircraft if a.h3_cells.get(4) in cs]
        alerts = [w for w in all_alerts if r in w.regions]
        tfrs = [t for t in all_tfrs if r in t.regions]

        events = _event_detector.detect_all(
            vessels=vessels,
            aircraft=aircraft,
            h3_cell=r,
            alerts=alerts,
            tfrs=tfrs,
        )
        graph = _dag_builder.build(events, r)

        # Region-prefix node IDs so a multi-region call returns globally-
        # unique IDs even though each region builds its own DAG.
        prefix = f"{r}::"
        id_remap = {n.id: prefix + n.id for n in graph.nodes}
        for n in graph.nodes:
            n.id = id_remap[n.id]
            nodes_out.append(n)
        for e in graph.edges:
            e.source = id_remap.get(e.source, e.source)
            e.target = id_remap.get(e.target, e.target)
            edges_out.append(e)

    return CausalLayerResponse(
        region=region,
        nodes=nodes_out,
        edges=edges_out,
        node_count=len(nodes_out),
        edge_count=len(edges_out),
        generated_at=datetime.now(timezone.utc),
    )


@router.get(
    "/causal/{h3_cell}",
    response_model=None,
    responses={
        200: {
            "description": "Causal graph with events, or empty result if no events detected",
            "content": {"application/json": {"schema": {
                "oneOf": [
                    {"$ref": "#/components/schemas/CausalGraph"},
                    {"$ref": "#/components/schemas/CausalEmptyResponse"},
                ]
            }}},
        }
    },
)
async def get_causal_graph(
    h3_cell: str,
    resolution: int = Query(default=5, ge=0, le=15),
    compact: bool = Query(default=False, description="Return compact FM-optimized format"),
) -> CausalEmptyResponse | dict[str, Any]:
    """
    Return causal graph for observed events in an H3 cell.

    Detects events from current data, builds a DAG using domain
    knowledge rules, and optionally runs counterfactual interventions.
    Returns CausalEmptyResponse when no significant events are found.
    """
    if _feed_manager is None:
        raise HTTPException(503, "Feed manager not initialized")

    vessels = _feed_manager.get_vessels_in_cell(h3_cell, resolution)
    aircraft = _feed_manager.get_aircraft_in_cell(h3_cell, resolution)

    # Detect events
    events = _event_detector.detect_all(vessels, aircraft, h3_cell)

    if not events:
        return CausalEmptyResponse(
            h3_cell=h3_cell,
            message="No significant events detected",
            events_checked=len(vessels) + len(aircraft),
            timestamp=datetime.now(timezone.utc),
        )

    # Build causal graph
    graph = _dag_builder.build(events, h3_cell)

    # Run counterfactual on root causes
    root_causes = _dag_builder.get_root_causes(graph)
    interventions = []
    for root in root_causes[:3]:  # Limit to top 3
        result = _intervention_engine.counterfactual(graph, root.id)
        interventions.append(result.to_dict())

    if compact:
        graph_data = _graph_serializer.to_compact(graph)
    else:
        graph_data = _graph_serializer.to_dict(graph)

    graph_data["interventions"] = interventions

    return graph_data


@router.get("/budget")
async def get_token_budget() -> TokenBudget:
    """Return current token budget allocation."""
    return _budget_manager.get_budget()
