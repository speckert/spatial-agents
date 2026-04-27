"""
Regions Routes — Runtime mutation of the active region list.

Two endpoints:

    POST /regions/swap  — replace slot 1 of ACTIVE_REGIONS with a new
                          city (geocoded server-side via Nominatim).
                          Rate-limited (112 s between swaps; bumps to 15 min post-launch).
                          Returns 429 on rate-limit, 400 on geocode
                          failure or refused swap (e.g. trying to touch
                          slot 0).

    GET  /regions       — diagnostic snapshot: current active list,
                          version hash, slot-0 pin, cooldown remaining.

Slot 0 is locked to san_francisco for as long as legacy iOS 3.1 clients
are in the field — they call /vessels and /aircraft without ?region=
and would render blank if SF were ever removed from the active set.

Version History:
    0.1.0  2026-04-26  Initial swap + diagnostics endpoint — Claude 4.7
    0.2.0  2026-04-26  Every swap attempt (success or failure) now
                       written to data/swap_log.jsonl with city, region
                       key, client IP, status. Real IP extracted from
                       X-Forwarded-For (Apache reverse-proxy aware).
                       Surfaced through /stats/swaps for logs.html
                       — Claude 4.7
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from spatial_agents.models import RegionSwapRequest, RegionSwapResponse
from spatial_agents.regions import (
    GeocodeFailed,
    RateLimited,
    RegionsManager,
    SwapRefused,
)
from spatial_agents.regions.swap_log import SwapLog

logger = logging.getLogger(__name__)

router = APIRouter()

_manager: RegionsManager | None = None
_swap_log: SwapLog | None = None


def set_regions_manager(manager: RegionsManager) -> None:
    """Inject the running RegionsManager (called at app startup)."""
    global _manager
    _manager = manager


def set_swap_log(swap_log: SwapLog) -> None:
    """Inject the running SwapLog (called at app startup)."""
    global _swap_log
    _swap_log = swap_log


def _client_ip(request: Request) -> str:
    """Extract real client IP, honoring Apache's X-Forwarded-For.

    In production, FastAPI sees 127.0.0.1 because Apache reverse-proxies
    every request from port 443 to port 8012. Apache sets
    X-Forwarded-For to the real client IP. Take the leftmost entry
    (the original client) — anything to the right is intermediate
    proxies that we don't care about.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/swap", response_model=RegionSwapResponse)
async def swap_region(
    req: RegionSwapRequest,
    request: Request,
    response: Response,
) -> RegionSwapResponse:
    """Replace slot 1 of ACTIVE_REGIONS with a new city.

    The server geocodes the supplied city name to lat/lng, snaps it
    to the nearest res-4 H3 cell, computes the 7-cell tile, and
    atomically updates ACTIVE_REGIONS, REGION_CELLS, REGIONS, and
    REGION_CENTERS. Registered swap callbacks (AIS reconnect,
    ADS-B immediate fetch, vessel/aircraft cache purge) fire
    after the swap so feeds reflect the new region within seconds.

    Slot 0 (san_francisco) is locked and cannot be replaced.

    Every attempt — success or failure — is appended to the swap log
    (data/swap_log.jsonl) with the user-typed city, the resolved
    region key (when one exists), and the client IP.
    """
    if _manager is None:
        raise HTTPException(503, "RegionsManager not initialized")

    ip = _client_ip(request)

    try:
        result = await _manager.swap_slot_one(req.city)
    except RateLimited as exc:
        if _swap_log is not None:
            _swap_log.record(
                city=req.city, region_key=None, ip=ip,
                status="rate_limited", error=str(exc),
            )
        # 429 with Retry-After header so well-behaved clients back off.
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "message": str(exc),
                "retry_after_seconds": exc.retry_after_seconds,
            },
        ) from exc
    except SwapRefused as exc:
        if _swap_log is not None:
            _swap_log.record(
                city=req.city, region_key=None, ip=ip,
                status="swap_refused", error=str(exc),
            )
        raise HTTPException(
            status_code=400,
            detail={"error": "swap_refused", "message": str(exc)},
        ) from exc
    except GeocodeFailed as exc:
        if _swap_log is not None:
            _swap_log.record(
                city=req.city, region_key=None, ip=ip,
                status="geocode_failed", error=str(exc),
            )
        raise HTTPException(
            status_code=400,
            detail={"error": "geocode_failed", "message": str(exc)},
        ) from exc

    if _swap_log is not None:
        _swap_log.record(
            city=req.city, region_key=result.new_slot_one, ip=ip,
            status="success",
        )

    snapshot = _manager.state_snapshot()
    return RegionSwapResponse(
        old_slot_one=result.old_slot_one,
        new_slot_one=result.new_slot_one,
        active_regions=result.new_active_regions,
        regions_version=result.new_version,
        seconds_until_next_swap_allowed=snapshot["seconds_until_next_swap_allowed"],
    )


@router.get("")
async def regions_state() -> dict[str, Any]:
    """Diagnostic: current active regions, version, and cooldown."""
    if _manager is None:
        raise HTTPException(503, "RegionsManager not initialized")
    return _manager.state_snapshot()
