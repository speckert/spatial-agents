"""
Health Routes — Pipeline status, feed freshness, and diagnostics.

Version History:
    0.1.0  2026-03-28  Initial health routes
    0.2.0  2026-04-02  Typed Pydantic response models for OpenAPI spec
    0.3.0  2026-04-09  Bbox driven by centralized REGION in config.py
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

import h3

from spatial_agents.config import REGION, config
from spatial_agents.models import (
    CoverageBbox,
    CoverageResponse,
    FeedHealthResponse,
    FeedStatus,
    HealthConfigResponse,
    HealthResponse,
)

router = APIRouter()

_start_time = time.monotonic()

# Collection bounding box — driven by REGION in config.py
_BBOX = CoverageBbox(min_lat=REGION[0], max_lat=REGION[1], min_lng=REGION[2], max_lng=REGION[3])


def _compute_coverage() -> CoverageResponse:
    """Build coverage response with the actual data collection bbox.

    The bbox is the authoritative boundary — it defines where AIS and
    ADS-B data is collected. H3 cells are the minimal set needed to
    query all data within the bbox at each resolution. At coarse
    resolutions (3-4), cells extend beyond the bbox — clients should
    use the bbox for map fitting and the cells for API queries.
    """
    h3_cells: dict[int, list[str]] = {}
    for res in config.tiling.resolutions:
        cells = set()
        # Sample the bbox interior to find all cells that contain data
        for lat in [_BBOX.min_lat, _BBOX.max_lat,
                    (_BBOX.min_lat + _BBOX.max_lat) / 2]:
            for lng in [_BBOX.min_lng, _BBOX.max_lng,
                        (_BBOX.min_lng + _BBOX.max_lng) / 2]:
                cells.add(h3.latlng_to_cell(lat, lng, res))
        h3_cells[res] = sorted(cells)
    return CoverageResponse(bbox=_BBOX, h3_cells=h3_cells)


_coverage = _compute_coverage()

# Lazy reference to feed manager
_feed_manager = None


def set_feed_manager(manager: Any) -> None:
    global _feed_manager
    _feed_manager = manager


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """
    Pipeline health check.

    Returns status of all data feeds, tile generation, and
    overall system health.
    """
    uptime = time.monotonic() - _start_time

    feed_statuses: list[FeedStatus] = []
    if _feed_manager is not None:
        feed_statuses = _feed_manager.health()

    # Determine overall status
    if not feed_statuses:
        status = "initializing"
    elif all(f.connected for f in feed_statuses):
        status = "ok"
    elif any(f.connected for f in feed_statuses):
        status = "degraded"
    else:
        status = "error"

    return HealthResponse(
        status=status,
        mode=config.mode.value,
        uptime_seconds=round(uptime, 1),
        port=config.serving.port,
        feeds=feed_statuses,
        config=HealthConfigResponse(
            resolutions=config.tiling.resolutions,
            context_window=config.fm.context_window_size,
        ),
        coverage=_coverage,
    )


@router.get("/health/feeds", response_model=FeedHealthResponse)
async def feed_health() -> FeedHealthResponse:
    """Detailed feed health status."""
    if _feed_manager is None:
        return FeedHealthResponse(feeds=[])

    return FeedHealthResponse(feeds=_feed_manager.health())
