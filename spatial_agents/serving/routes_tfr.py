"""
TFR Routes — FAA Temporary Flight Restrictions as polygons + H3 compact cells.

Single endpoint: GET /api/tfr. Mirrors /api/weather/alerts in shape.
With ?region=<name>, returns only TFRs whose polygon intersects that
region; without it, returns every active TFR with geometry CONUS-wide.

Version History:
    0.1.0  2026-04-25  Initial TFR endpoint with optional ?region= filter,
                       same canonical pattern as /api/weather/alerts and
                       /api/{vessels,aircraft} — Claude 4.7
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from spatial_agents.config import REGION_CELLS
from spatial_agents.models import TFRsResponse

router = APIRouter()

_feed_manager: Any = None


def set_feed_manager(manager: Any) -> None:
    """Inject the running FeedManager so the route can read its TFR cache."""
    global _feed_manager
    _feed_manager = manager


@router.get("/tfr", response_model=TFRsResponse)
async def tfrs(
    region: str | None = Query(
        default=None,
        description="Optional region name (e.g. san_francisco, boston) — "
                    "filters TFRs to those intersecting that region. "
                    "Absent = all CONUS TFRs with geometry.",
    ),
) -> TFRsResponse:
    """
    Currently active FAA Temporary Flight Restrictions.

    With `?region=<name>`: only TFRs whose polygon intersects that
    region (single-region clients).

    Without a region filter: every active TFR with a polygon
    geometry, anywhere in CONUS — useful for the web map's global
    view and for global-overlay clients.

    Each TFR carries the original FAA polygon, type (SECURITY,
    HAZARDS, VIP, SPACE OPERATIONS, ...), title, last-modified
    timestamp, a mixed-resolution H3 compact cell cover, and a
    pre-rendered MultiPolygon of that cover for client-side display.
    """
    if _feed_manager is None:
        return TFRsResponse(
            tfrs=[],
            count=0,
            last_updated=datetime.now(timezone.utc),
        )

    items = _feed_manager.get_latest_tfrs()
    if region is not None:
        if region not in REGION_CELLS:
            raise HTTPException(400, f"Unknown region: {region}")
        items = [t for t in items if region in t.regions]

    last = _feed_manager.get_tfr_last_fetch() or datetime.now(timezone.utc)
    return TFRsResponse(
        tfrs=items,
        count=len(items),
        last_updated=last,
    )
