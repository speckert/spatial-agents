"""
Weather Routes — NOAA NWS active alerts as polygons + H3 compact cells.

Single endpoint: GET /api/weather/alerts. No query params. Returns every
active NWS alert whose polygon intersects any active region. Each alert
carries its native NWS GeoJSON polygon plus a mixed-resolution H3 compact
cell set, suitable for spatial joins against entity h3_cells.

Version History:
    0.1.0  2026-04-25  Initial weather alerts endpoint — Claude 4.7
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from spatial_agents.models import WeatherAlertsResponse

router = APIRouter()

_feed_manager: Any = None


def set_feed_manager(manager: Any) -> None:
    """Inject the running FeedManager so the route can read its alert cache."""
    global _feed_manager
    _feed_manager = manager


@router.get("/weather/alerts", response_model=WeatherAlertsResponse)
async def weather_alerts() -> WeatherAlertsResponse:
    """
    Currently active NWS alerts intersecting any active region.

    Returns one entry per alert with the original NWS polygon, severity,
    headline, expiration, and a mixed-resolution H3 compact cell cover.
    The web map uses the polygon for display; clients doing spatial joins
    (e.g. "is this aircraft inside any active alert?") use the cell cover.
    """
    if _feed_manager is None:
        return WeatherAlertsResponse(
            alerts=[],
            count=0,
            last_updated=datetime.now(timezone.utc),
        )

    alerts = _feed_manager.get_latest_alerts()
    last = _feed_manager.get_weather_last_fetch() or datetime.now(timezone.utc)
    return WeatherAlertsResponse(
        alerts=alerts,
        count=len(alerts),
        last_updated=last,
    )
