"""
Weather Routes — NOAA NWS active alerts as polygons + H3 compact cells.

Single endpoint: GET /api/weather/alerts. No query params. Returns every
active NWS alert whose polygon intersects any active region. Each alert
carries its native NWS GeoJSON polygon plus a mixed-resolution H3 compact
cell set, suitable for spatial joins against entity h3_cells.

Version History:
    0.1.0  2026-04-25  Initial weather alerts endpoint — Claude 4.7
    0.2.0  2026-04-25  Optional ?region=<name> filter. Absent = all
                       alerts globally (every CONUS alert with geometry);
                       present = only alerts whose `regions` list contains
                       <name>. Mirrors the canonical pattern used by
                       /api/vessels and /api/aircraft — Claude 4.7
    0.3.0  2026-04-26  regions_version stamped on every response so
                       clients can detect a runtime region swap and
                       re-fetch /health — Claude 4.7
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from spatial_agents.config import REGION_CELLS, regions_version
from spatial_agents.models import WeatherAlertsResponse

router = APIRouter()

_feed_manager: Any = None


def set_feed_manager(manager: Any) -> None:
    """Inject the running FeedManager so the route can read its alert cache."""
    global _feed_manager
    _feed_manager = manager


@router.get("/weather/alerts", response_model=WeatherAlertsResponse)
async def weather_alerts(
    region: str | None = Query(
        default=None,
        description="Optional region name (e.g. san_francisco, boston) — "
                    "filters alerts to those intersecting that region. "
                    "Absent = all CONUS alerts with geometry.",
    ),
) -> WeatherAlertsResponse:
    """
    Currently active NWS alerts.

    With `?region=<name>`: only alerts whose polygon intersects that
    region (single-region clients).

    Without a region filter: every active NWS alert that has a polygon
    geometry, anywhere in CONUS — useful for the web map's global view
    and for global-overlay clients.

    Each alert carries the original NWS polygon, severity, headline,
    expiration, a mixed-resolution H3 compact cell cover, and a
    pre-rendered MultiPolygon of that cover for client-side display.
    """
    if _feed_manager is None:
        return WeatherAlertsResponse(
            alerts=[],
            count=0,
            last_updated=datetime.now(timezone.utc),
            regions_version=regions_version(),
        )

    alerts = _feed_manager.get_latest_alerts()
    if region is not None:
        if region not in REGION_CELLS:
            raise HTTPException(400, f"Unknown region: {region}")
        alerts = [a for a in alerts if region in a.regions]

    last = _feed_manager.get_weather_last_fetch() or datetime.now(timezone.utc)
    return WeatherAlertsResponse(
        alerts=alerts,
        count=len(alerts),
        last_updated=last,
        regions_version=regions_version(),
    )
