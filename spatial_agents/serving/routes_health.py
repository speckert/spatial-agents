"""
Health Routes — Pipeline status, feed freshness, and diagnostics.

Version History:
    0.1.0  2026-03-28  Initial health routes
    0.2.0  2026-04-02  Typed Pydantic response models for OpenAPI spec
    0.3.0  2026-04-09  Bbox driven by centralized REGION in config.py
    0.4.0  2026-04-24  Multi-region coverage — per-region bbox, H3 cells,
                       and advisories in /health response — Claude Opus 4.6
    0.5.0  2026-04-25  Coverage now includes primary_cell, buffer_cells,
                       and a GeoJSON MultiPolygon geometry derived from
                       the 7-cell region tile in REGION_CELLS — Claude 4.7
    0.6.0  2026-04-25  Experiment: bbox dropped from CoverageResponse
                       payload (geometry is canonical). Internal
                       _REGION_BBOXES kept only for h3_cells sampling.
                       Tested against legacy iOS 3.1 client — Claude 4.7
    0.7.0  2026-04-26  Per-region structures (bboxes, h3 cells, geometry)
                       computed on every /health call instead of at
                       import — required because ACTIVE_REGIONS is now
                       runtime-mutable via RegionsManager. Adds
                       regions_version + regions_slot_zero_pinned to
                       the response — Claude 4.7
    0.8.0  2026-04-26  CoverageResponse now carries display_name, looked
                       up from the injected RegionsManager. Falls back
                       to a snake_case → Title Case derivation if the
                       manager hasn't been wired up yet (e.g. tests
                       that build the response directly) — Claude 4.7
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

import h3

from spatial_agents.config import (
    ACTIVE_REGIONS,
    REGION_ADVISORIES,
    REGION_CELLS,
    REGIONS,
    config,
    regions_version,
)
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

# Lazy reference to regions manager — injected at startup by main.py.
# Used to resolve a region key into its human-readable display name.
_regions_manager: Any = None


def set_regions_manager(manager: Any) -> None:
    """Inject the running RegionsManager (called at app startup)."""
    global _regions_manager
    _regions_manager = manager


def _display_name_for(name: str) -> str:
    """Resolve a region key to a display label (manager wins; fallback OK)."""
    if _regions_manager is not None:
        return _regions_manager.get_display_name(name)
    return name.replace("_", " ").title()


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


def _cells_to_multipolygon(cells: list[str]) -> dict:
    """Build a GeoJSON MultiPolygon from H3 cell boundaries.

    Each cell becomes one polygon (single linear ring, closed). Coordinates
    are GeoJSON order (lng, lat). The primary cell should be passed first.
    """
    polygons: list[list[list[list[float]]]] = []
    for cell in cells:
        boundary = h3.cell_to_boundary(cell)  # [(lat, lng), ...]
        ring = [[lng, lat] for lat, lng in boundary]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        polygons.append([ring])
    return {"type": "MultiPolygon", "coordinates": polygons}


def _build_region_coverage(name: str) -> CoverageResponse | None:
    """Build a CoverageResponse for one region using the current REGION_CELLS.

    Returns None if the region isn't registered yet (defensive — should
    not happen during normal operation since RegionsManager registers
    regions before adding them to ACTIVE_REGIONS).
    """
    cells = REGION_CELLS.get(name)
    bbox_tuple = REGIONS.get(name)
    if cells is None or bbox_tuple is None:
        return None
    bbox = CoverageBbox(
        min_lat=bbox_tuple[0],
        max_lat=bbox_tuple[1],
        min_lng=bbox_tuple[2],
        max_lng=bbox_tuple[3],
    )
    return CoverageResponse(
        region=name,
        display_name=_display_name_for(name),
        h3_cells=_compute_h3_cells(bbox),
        primary_cell=str(cells["primary"]),
        buffer_cells=list(cells["buffer"]),  # type: ignore[arg-type]
        geometry=_cells_to_multipolygon(cells["all"]),  # type: ignore[arg-type]
        advisories=_build_advisories(name),
    )


# Lazy reference to feed manager
_feed_manager = None


def set_feed_manager(manager: Any) -> None:
    global _feed_manager
    _feed_manager = manager


def _build_advisories(region: str | None = None) -> list[str]:
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

    # Build per-region coverage at request time so a slot-1 swap is
    # reflected in the very next /health response.
    regions: dict[str, CoverageResponse] = {}
    for name in ACTIVE_REGIONS:
        cov = _build_region_coverage(name)
        if cov is not None:
            regions[name] = cov

    # Backward-compat: `coverage` mirrors the first active region (slot 0).
    coverage = regions.get(ACTIVE_REGIONS[0]) if ACTIVE_REGIONS else None
    if coverage is None:
        # Defensive fallback for an empty/broken active set.
        coverage = CoverageResponse(
            region="",
            h3_cells={},
            primary_cell="",
            buffer_cells=[],
            geometry={},
            advisories=["Active region set is empty — server misconfigured."],
        )

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
        coverage=coverage,
        regions=regions,
        regions_version=regions_version(),
        regions_slot_zero_pinned="san_francisco",
    )


@router.get("/health/feeds", response_model=FeedHealthResponse)
async def feed_health() -> FeedHealthResponse:
    """Detailed feed health status."""
    if _feed_manager is None:
        return FeedHealthResponse(feeds=[])

    return FeedHealthResponse(feeds=_feed_manager.health())
