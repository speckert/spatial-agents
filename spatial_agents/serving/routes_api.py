"""
API Routes — Dynamic query endpoints for live data and intelligence.

Version History:
    0.1.0  2026-03-28  Initial API routes
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
from spatial_agents.intelligence.token_budget import TokenBudgetManager
from spatial_agents.models import DataDomain

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


@router.get("/vessels/{h3_cell}")
async def get_vessels(
    h3_cell: str,
    resolution: int = Query(default=5, ge=0, le=15),
) -> dict[str, Any]:
    """
    Return live vessel positions within an H3 cell.

    Response is a JSON array of vessel records with position,
    heading, speed, and vessel type.
    """
    if _feed_manager is None:
        raise HTTPException(503, "Feed manager not initialized")

    vessels = _feed_manager.get_vessels_in_cell(h3_cell, resolution)
    return {
        "h3_cell": h3_cell,
        "resolution": resolution,
        "count": len(vessels),
        "vessels": [v.model_dump(mode="json") for v in vessels],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/aircraft/{h3_cell}")
async def get_aircraft(
    h3_cell: str,
    resolution: int = Query(default=5, ge=0, le=15),
) -> dict[str, Any]:
    """
    Return live aircraft positions within an H3 cell.
    """
    if _feed_manager is None:
        raise HTTPException(503, "Feed manager not initialized")

    aircraft = _feed_manager.get_aircraft_in_cell(h3_cell, resolution)
    return {
        "h3_cell": h3_cell,
        "resolution": resolution,
        "count": len(aircraft),
        "aircraft": [a.model_dump(mode="json") for a in aircraft],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/intelligence/{h3_cell}")
async def get_intelligence(
    h3_cell: str,
    resolution: int = Query(default=5, ge=0, le=15),
    domain: str = Query(default="maritime"),
) -> dict[str, Any]:
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

    return {
        "h3_cell": h3_cell,
        "resolution": resolution,
        "domain": domain,
        "activity_summary": activity_summary,
        "vessel_count": len(vessels),
        "aircraft_count": len(aircraft),
        "token_budget": budget.model_dump(),
        "payload_tokens": payload_tokens,
        "note": "FM evaluation pending — shows prompt payload and budget",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/causal/{h3_cell}")
async def get_causal_graph(
    h3_cell: str,
    resolution: int = Query(default=5, ge=0, le=15),
    compact: bool = Query(default=False, description="Return compact FM-optimized format"),
) -> dict[str, Any]:
    """
    Return causal graph for observed events in an H3 cell.

    Detects events from current data, builds a DAG using domain
    knowledge rules, and optionally runs counterfactual interventions.
    """
    if _feed_manager is None:
        raise HTTPException(503, "Feed manager not initialized")

    vessels = _feed_manager.get_vessels_in_cell(h3_cell, resolution)
    aircraft = _feed_manager.get_aircraft_in_cell(h3_cell, resolution)

    # Detect events
    events = _event_detector.detect_all(vessels, aircraft, h3_cell)

    if not events:
        return {
            "h3_cell": h3_cell,
            "message": "No significant events detected",
            "events_checked": len(vessels) + len(aircraft),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

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
async def get_token_budget() -> dict[str, Any]:
    """Return current token budget allocation."""
    budget = _budget_manager.get_budget()
    return budget.model_dump()
