"""
Regions package — runtime-mutable active region set.

The server's ACTIVE_REGIONS list is now driven by RegionsManager, which
provides geocoding (Nominatim), persistence (data/regions_state.json),
rate limiting, and atomic slot-1 swaps. Slot 0 is pinned to san_francisco
so legacy iOS 3.1 clients (which don't pass ?region=) keep getting
SF data even when slot 1 changes.

Version History:
    0.1.0  2026-04-26  Initial regions manager — Claude 4.7
"""

from spatial_agents.regions.manager import (
    RegionsManager,
    SwapResult,
    SwapError,
    RateLimited,
    SwapRefused,
    GeocodeFailed,
)

__all__ = [
    "RegionsManager",
    "SwapResult",
    "SwapError",
    "RateLimited",
    "SwapRefused",
    "GeocodeFailed",
]
