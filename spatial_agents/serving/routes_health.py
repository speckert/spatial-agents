"""
Health Routes — Pipeline status, feed freshness, and diagnostics.

Version History:
    0.1.0  2026-03-28  Initial health routes
    0.2.0  2026-04-02  Typed Pydantic response models for OpenAPI spec
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

from spatial_agents.config import config
from spatial_agents.models import (
    FeedHealthResponse,
    FeedStatus,
    HealthConfigResponse,
    HealthResponse,
)

router = APIRouter()

_start_time = time.monotonic()

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
    )


@router.get("/health/feeds", response_model=FeedHealthResponse)
async def feed_health() -> FeedHealthResponse:
    """Detailed feed health status."""
    if _feed_manager is None:
        return FeedHealthResponse(feeds=[])

    return FeedHealthResponse(feeds=_feed_manager.health())
