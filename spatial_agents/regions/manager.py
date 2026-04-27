"""
RegionsManager — runtime mutation of the active region list.

Responsibilities:
    1. Geocode user-supplied city names to lat/lng (Nominatim/OSM).
    2. Persist active_regions + custom region centers to data/regions_state.json.
    3. Rate-limit swap requests (default 112s; bump to 15min after launch).
    4. Atomically swap slot 1 of ACTIVE_REGIONS, mutating REGION_CENTERS,
       REGION_CELLS, and REGIONS so all downstream consumers see the new
       region with no further plumbing.
    5. Fire registered swap callbacks (AIS reconnect, vessel/aircraft
       cache purge, immediate ADS-B fetch) so feeds switch over without
       waiting for the next polling tick.

Slot 0 is locked to "san_francisco" until iOS App v4 replaces v3.1 in
the field. Legacy v3.1 calls /vessels and /aircraft without ?region=,
so SF must always be in the active set for those unfiltered responses.

Version History:
    0.1.0  2026-04-26  Initial regions manager — Claude 4.7
    0.2.0  2026-04-26  Default swap cooldown raised from 60s to 112s.
                       Reason: the populate-wait toast on map.html
                       tells the user to expect ~60 s before the map
                       fills; offering them a "switch again" affordance
                       at 60 s invites them to swap mid-populate, which
                       (a) wastes the AIS reconnect they just paid for,
                       (b) thrashes Nominatim, and (c) makes the UX
                       feel broken. 112 s gives the populate window
                       headroom past the 60 s toast before the next
                       swap is permitted, and stays well under
                       Nominatim's 1 req/s TOS ceiling — Claude 4.7
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from spatial_agents.config import (
    ACTIVE_REGIONS,
    REGION_CELLS,
    REGION_CENTERS,
    REGIONS,
    _cells_bbox,
    _compute_region_cells,
    config,
    regions_version,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults (used when no state file exists yet)
# ---------------------------------------------------------------------------

DEFAULT_ACTIVE_REGIONS = ["san_francisco", "boston"]

# Hardcoded centers for known regions so we don't need to geocode on first
# run. Anything else the user types is geocoded on demand and cached in
# regions_state.json.
SEED_CENTERS: dict[str, tuple[float, float]] = {
    "san_francisco": (37.78, -122.42),
    "chicago":       (41.88, -87.63),
    "boston":        (42.36, -71.06),
}

PINNED_SLOT_ZERO = "san_francisco"


# ---------------------------------------------------------------------------
# Errors + result types
# ---------------------------------------------------------------------------

class SwapError(Exception):
    """Base class for all swap-related failures."""


class RateLimited(SwapError):
    """Caller is asking too soon after the last successful swap."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"Rate limited. Retry in {retry_after_seconds}s.")
        self.retry_after_seconds = retry_after_seconds


class SwapRefused(SwapError):
    """The requested swap is not allowed (e.g. trying to replace slot 0)."""


class GeocodeFailed(SwapError):
    """The geocoder couldn't resolve the city name."""


@dataclass
class SwapResult:
    old_slot_one: str | None
    new_slot_one: str
    new_active_regions: list[str]
    new_version: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_region_key(name: str) -> str:
    """City name → snake_case region key.

    "St Louis"  → "st_louis"
    "  New York " → "new_york"
    "St. Louis, MO" → "st_louis_mo"
    """
    cleaned = []
    for ch in name.strip().lower():
        if ch.isalnum():
            cleaned.append(ch)
        elif cleaned and cleaned[-1] != "_":
            cleaned.append("_")
    return "".join(cleaned).strip("_")


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

# Type for swap callbacks — async function taking (old_region, new_region).
# old_region may be None on first init.
SwapCallback = Callable[[str | None, str], Awaitable[None]]


class RegionsManager:
    """Owns runtime mutation of ACTIVE_REGIONS + persistence + callbacks."""

    def __init__(
        self,
        state_path: Path | None = None,
        cooldown_seconds: int = 112,
        geocode_user_agent: str = "SpatialAgents/0.1 (admin@specktech.com)",
    ) -> None:
        self._state_path = state_path or (config.data_dir / "regions_state.json")
        self._cooldown_seconds = cooldown_seconds
        self._geocode_user_agent = geocode_user_agent
        self._lock = asyncio.Lock()
        self._last_swap_at: float = 0.0  # monotonic clock; 0 = never
        self._on_swap: list[SwapCallback] = []
        # Custom-region center cache so we don't re-geocode after restart.
        self._custom_centers: dict[str, tuple[float, float]] = {}
        # Display-name cache (the original city string the user typed).
        self._display_names: dict[str, str] = {}

    # --- Callback registration ---------------------------------------------

    def on_swap(self, cb: SwapCallback) -> None:
        """Register an async callback fired after each successful swap."""
        self._on_swap.append(cb)

    # --- Persistence -------------------------------------------------------

    def _save_state(self) -> None:
        """Write the current state to disk (best-effort; logs on failure)."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "active_regions": list(ACTIVE_REGIONS),
                "custom_regions": {
                    name: {
                        "center": list(self._custom_centers[name]),
                        "display_name": self._display_names.get(
                            name, name.replace("_", " ").title()
                        ),
                    }
                    for name in self._custom_centers
                },
            }
            self._state_path.write_text(json.dumps(payload, indent=2))
            logger.info("Regions state persisted to %s", self._state_path)
        except Exception as exc:  # pragma: no cover — disk failure
            logger.error("Failed to persist regions state: %s", exc)

    def _load_state(self) -> dict[str, Any]:
        """Read regions_state.json, or return defaults if missing/broken."""
        if not self._state_path.exists():
            return {"active_regions": list(DEFAULT_ACTIVE_REGIONS), "custom_regions": {}}
        try:
            data = json.loads(self._state_path.read_text())
            if not isinstance(data.get("active_regions"), list):
                raise ValueError("active_regions missing or not a list")
            return data
        except Exception as exc:
            logger.error(
                "regions_state.json unreadable (%s) — falling back to defaults", exc,
            )
            return {"active_regions": list(DEFAULT_ACTIVE_REGIONS), "custom_regions": {}}

    def _ensure_region_registered(
        self,
        name: str,
        center: tuple[float, float],
        display_name: str | None = None,
    ) -> None:
        """Add a region to REGION_CENTERS / REGION_CELLS / REGIONS (idempotent)."""
        REGION_CENTERS[name] = center
        primary, buffer = _compute_region_cells(center)
        all_cells = [primary] + buffer
        bbox = _cells_bbox(all_cells)
        REGION_CELLS[name] = {
            "primary": primary,
            "buffer": buffer,
            "all": all_cells,
            "bbox": bbox,
        }
        REGIONS[name] = bbox
        if display_name:
            self._display_names[name] = display_name

    # --- Startup -----------------------------------------------------------

    def initialize(self) -> None:
        """Apply persisted state to ACTIVE_REGIONS at server startup.

        Called synchronously at startup, before FeedManager.start(), so all
        feeds pick up the right active regions on first connect.
        """
        state = self._load_state()
        custom = state.get("custom_regions") or {}

        # Hydrate custom-region centers + ensure they're registered as full
        # regions (REGION_CELLS / REGIONS entries) before any consumer uses
        # them.
        for name, info in custom.items():
            try:
                center_list = info["center"]
                center = (float(center_list[0]), float(center_list[1]))
                display = info.get("display_name") or name.replace("_", " ").title()
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping malformed custom region: %s", name)
                continue
            self._custom_centers[name] = center
            self._display_names[name] = display
            self._ensure_region_registered(name, center, display)

        # Seed any well-known regions referenced in active_regions that
        # aren't already registered (e.g. boston after a fresh install).
        wanted = list(state.get("active_regions") or DEFAULT_ACTIVE_REGIONS)
        for name in wanted:
            if name in REGION_CENTERS:
                continue
            seed = SEED_CENTERS.get(name)
            if seed is None:
                logger.warning(
                    "active_regions includes %r but no center is known; "
                    "dropping from active set", name,
                )
                continue
            self._ensure_region_registered(name, seed, name.replace("_", " ").title())

        # Defensive: enforce slot-0 pin.
        if not wanted or wanted[0] != PINNED_SLOT_ZERO:
            logger.warning(
                "Forcing slot 0 to %s (was %r)", PINNED_SLOT_ZERO, wanted[:1],
            )
            wanted = [PINNED_SLOT_ZERO] + [r for r in wanted if r != PINNED_SLOT_ZERO]

        # Drop any wanted region we couldn't register.
        wanted = [r for r in wanted if r in REGION_CENTERS]

        # Mutate ACTIVE_REGIONS in place so any module that already imported
        # the list sees the update (config.py exports it as a module-level
        # list).
        ACTIVE_REGIONS.clear()
        ACTIVE_REGIONS.extend(wanted)

        # Always persist so a fresh install gets a state file written out
        # with the canonical defaults.
        self._save_state()

        logger.info(
            "RegionsManager initialized — active=%s, version=%s, custom_cached=%d",
            ACTIVE_REGIONS, regions_version(), len(self._custom_centers),
        )

    # --- Display names -----------------------------------------------------

    def get_display_name(self, name: str) -> str:
        """Display label for a region.

        Returns the original city string the user typed (for custom regions)
        or the seeded display ("San Francisco", "Chicago") for well-known
        ones. Falls back to a snake_case → Title Case derivation if the
        region was never registered through the manager.
        """
        return self._display_names.get(name, name.replace("_", " ").title())

    # --- Geocoder ----------------------------------------------------------

    async def _geocode(self, query: str) -> tuple[float, float]:
        """Resolve a free-text city name to (lat, lng) via Nominatim.

        Nominatim TOS: max 1 req/sec, must set User-Agent. Our 112-second
        rate limit on swaps keeps us comfortably under that.
        """
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": query, "format": "json", "limit": 1}
        headers = {"User-Agent": self._geocode_user_agent}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            results = resp.json()
        except httpx.HTTPError as exc:
            raise GeocodeFailed(f"Geocoder unreachable: {exc}") from exc

        if not results:
            raise GeocodeFailed(f"No results for {query!r}")
        try:
            lat = float(results[0]["lat"])
            lng = float(results[0]["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GeocodeFailed(f"Malformed geocoder response: {exc}") from exc

        if abs(lat) > 90 or abs(lng) > 180:
            raise GeocodeFailed(f"Geocoder returned out-of-range lat/lng: {lat}, {lng}")
        return (lat, lng)

    # --- Public state introspection ----------------------------------------

    def state_snapshot(self) -> dict[str, Any]:
        """A debug-friendly snapshot — used by GET /regions for diagnostics."""
        return {
            "active_regions": list(ACTIVE_REGIONS),
            "version": regions_version(),
            "slot_zero_pinned": PINNED_SLOT_ZERO,
            "cooldown_seconds": self._cooldown_seconds,
            "seconds_until_next_swap_allowed": max(
                0,
                int(self._cooldown_seconds - (time.monotonic() - self._last_swap_at))
                if self._last_swap_at
                else 0,
            ),
        }

    # --- Swap --------------------------------------------------------------

    async def swap_slot_one(self, city_name: str) -> SwapResult:
        """Replace the second active region with a new city.

        Raises:
            RateLimited:   if called within the cooldown window.
            SwapRefused:   if the request would touch slot 0 or is a no-op.
            GeocodeFailed: if Nominatim can't resolve the city name.
        """
        async with self._lock:
            now = time.monotonic()
            if self._last_swap_at:
                elapsed = now - self._last_swap_at
                if elapsed < self._cooldown_seconds:
                    raise RateLimited(int(self._cooldown_seconds - elapsed))

            key = normalize_region_key(city_name)
            if not key:
                raise SwapRefused("Empty or invalid city name")
            if key == PINNED_SLOT_ZERO:
                raise SwapRefused(
                    f"Slot 0 is locked to {PINNED_SLOT_ZERO}. "
                    "Pick a different city for slot 1."
                )

            current_slot_one = ACTIVE_REGIONS[1] if len(ACTIVE_REGIONS) > 1 else None
            if key == current_slot_one:
                raise SwapRefused(
                    f"{key!r} is already in slot 1 — nothing to do."
                )

            # Reuse cached center if we've geocoded this city before.
            if key not in REGION_CENTERS:
                center = await self._geocode(city_name)
                self._custom_centers[key] = center
                self._display_names[key] = city_name.strip()
                self._ensure_region_registered(key, center, city_name.strip())
                logger.info(
                    "Geocoded new region %s → (%.4f, %.4f)", key, center[0], center[1],
                )

            old_slot_one = current_slot_one
            if len(ACTIVE_REGIONS) > 1:
                ACTIVE_REGIONS[1] = key
            else:
                ACTIVE_REGIONS.append(key)

            self._last_swap_at = now
            self._save_state()

            new_version = regions_version()
            logger.info(
                "Region swap: slot 1 %r → %r (version %s)",
                old_slot_one, key, new_version,
            )

        # Fire callbacks outside the lock so a slow callback doesn't block
        # the next swap-permission check.
        for cb in self._on_swap:
            try:
                await cb(old_slot_one, key)
            except Exception as exc:  # pragma: no cover — defensive
                logger.error(
                    "Swap callback %s failed: %s", getattr(cb, "__name__", "?"), exc,
                )

        return SwapResult(
            old_slot_one=old_slot_one,
            new_slot_one=key,
            new_active_regions=list(ACTIVE_REGIONS),
            new_version=new_version,
        )
