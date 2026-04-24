"""
Health Routes — Pipeline status, feed freshness, and diagnostics.

Version History:
    0.1.0  2026-03-28  Initial health routes
    0.2.0  2026-04-02  Typed Pydantic response models for OpenAPI spec
    0.3.0  2026-04-09  Bbox driven by centralized REGION in config.py
    0.4.0  2026-04-24  Multi-region coverage — per-region bbox, H3 cells,
                       and advisories in /health response — Claude Opus 4.6
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

import h3

from spatial_agents.config import ACTIVE_REGIONS, REGIONS, REGION_ADVISORIES, REGION_NAME, config
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

# Per-region bounding boxes and H3 cells
_REGION_BBOXES: dict[str, CoverageBbox] = {
    name: CoverageBbox(min_lat=r[0], max_lat=r[1], min_lng=r[2], max_lng=r[3])
    for name, r in ((n, REGIONS[n]) for n in ACTIVE_REGIONS)
}


def _compute_h3_cells(bbox: CoverageBbox) -> dict[int, list[str]]:
    """Compute minimal H3 cell set covering a bbox at each resolution."""
    h3_cells: dict[int, list[str]] = {}
    for res in config.tiling.resolutions:
        cells = set()
        for lat in [bbox.min_lat, bbox.max_lat,
                    (bbox.min_lat + bbox.max_lat) / 2]:
            for lng in [bbox.min_lng, bbox.max_lng,
                        (bbox.min_lng + bbox.max_lng) / 2]:
                cells.add(h3.latlng_to_cell(lat, lng, res))
        h3_cells[res] = sorted(cells)
    return h3_cells


_REGION_H3: dict[str, dict[int, list[str]]] = {
    name: _compute_h3_cells(bbox) for name, bbox in _REGION_BBOXES.items()
}

# Backward compat — first active region
_BBOX = _REGION_BBOXES[REGION_NAME]
_h3_cells = _REGION_H3[REGION_NAME]

# Lazy reference to feed manager
_feed_manager = None


def set_feed_manager(manager: Any) -> None:
    global _feed_manager
    _feed_manager = manager


def _build_advisories(region: str = REGION_NAME) -> list[str]:
    """Build dynamic advisories from region config and live feed status."""
    advisories = list(REGION_ADVISORIES.get(region, []))

    if _feed_manager is not None:
        from datetime import datetime, timedelta, timezone
        for feed in _feed_manager.health():
            if feed.name == "ais":
                if feed.last_message_at is None:
                    advisories.append(
                        "AIS feed has not received any vessel data since server start."
                    )
                elif (datetime.now(timezone.utc) - feed.last_message_at) > timedelta(minutes=10):
                    age_min = int((datetime.now(timezone.utc) - feed.last_message_at).total_seconds() / 60)
                    advisories.append(
                        f"AIS feed has been silent for {age_min} minutes. "
                        "Vessel positions may be stale."
                    )
                if feed.error:
                    advisories.append(f"AIS feed error: {feed.error}")
            elif feed.name == "adsb":
                if feed.last_message_at is None:
                    advisories.append(
                        "ADS-B feed has not received any aircraft data since server start."
                    )
                if feed.error:
                    advisories.append(f"ADS-B feed error: {feed.error}")

    return advisories


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

    regions = {
        name: CoverageResponse(
            region=name,
            bbox=_REGION_BBOXES[name],
            h3_cells=_REGION_H3[name],
            advisories=_build_advisories(name),
        )
        for name in ACTIVE_REGIONS
    }

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
        coverage=regions[REGION_NAME],
        regions=regions,
    )


@router.get("/health/feeds", response_model=FeedHealthResponse)
async def feed_health() -> FeedHealthResponse:
    """Detailed feed health status."""
    if _feed_manager is None:
        return FeedHealthResponse(feeds=[])

    return FeedHealthResponse(feeds=_feed_manager.health())
