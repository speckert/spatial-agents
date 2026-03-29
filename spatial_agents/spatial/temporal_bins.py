"""
Temporal Binner — Time window management for H3 tiles.

Each H3 resolution has an associated temporal bin size:
    res 3 → 1 day    (global overview)
    res 4 → 1 hour   (regional)
    res 5 → 5 min    (approach)
    res 6 → 1 min    (detail)
    res 7 → live     (berth-level)

Records are bucketed into time windows for tile generation.

Version History:
    0.1.0  2026-03-28  Initial temporal binner
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Sequence, TypeVar

from pydantic import BaseModel

from spatial_agents.config import config

T = TypeVar("T", bound=BaseModel)

# Bin durations mapped from config strings
BIN_DURATIONS: dict[str, timedelta] = {
    "1min": timedelta(minutes=1),
    "5min": timedelta(minutes=5),
    "1hour": timedelta(hours=1),
    "1day": timedelta(days=1),
    "live": timedelta(seconds=0),  # No binning — pass-through
}


def bin_key(timestamp: datetime, bin_size: str) -> str:
    """
    Generate a canonical bin key for a timestamp.

    Returns an ISO-format string truncated to the bin boundary.
    For "live" bins, returns "live" (no temporal grouping).
    """
    if bin_size == "live":
        return "live"

    duration = BIN_DURATIONS.get(bin_size)
    if duration is None or duration.total_seconds() == 0:
        return "live"

    total_seconds = int(duration.total_seconds())
    epoch = int(timestamp.timestamp())
    bin_start = epoch - (epoch % total_seconds)

    return datetime.fromtimestamp(bin_start, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")


class TemporalBinner:
    """
    Group records into temporal bins for tile generation.

    Usage:
        binner = TemporalBinner()
        bins = binner.bin_records(records, resolution=5, timestamp_fn=lambda r: r.position.timestamp)
        for bin_key, bin_records in bins.items():
            tile_builder.build(cell_id, resolution, bin_key, bin_records)
    """

    def __init__(self) -> None:
        self._bin_config = config.tiling.temporal_bins

    def get_bin_size(self, resolution: int) -> str:
        """Return the temporal bin size string for a given H3 resolution."""
        return self._bin_config.get(resolution, "1hour")

    def get_bin_duration(self, resolution: int) -> timedelta:
        """Return the temporal bin duration for a given H3 resolution."""
        bin_size = self.get_bin_size(resolution)
        return BIN_DURATIONS.get(bin_size, timedelta(hours=1))

    def bin_records(
        self,
        records: Sequence[T],
        resolution: int,
        timestamp_fn: callable,
    ) -> dict[str, list[T]]:
        """
        Group records into temporal bins.

        Args:
            records: Sequence of Pydantic model instances
            resolution: H3 resolution (determines bin size)
            timestamp_fn: Function to extract datetime from a record

        Returns:
            Dict mapping bin_key → list of records in that bin
        """
        bin_size = self.get_bin_size(resolution)
        bins: dict[str, list[T]] = defaultdict(list)

        for record in records:
            ts = timestamp_fn(record)
            key = bin_key(ts, bin_size)
            bins[key].append(record)

        return dict(bins)

    def current_bin_key(self, resolution: int) -> str:
        """Return the bin key for the current moment at a given resolution."""
        now = datetime.now(timezone.utc)
        return bin_key(now, self.get_bin_size(resolution))

    def is_bin_expired(self, key: str, resolution: int) -> bool:
        """Check whether a bin key is older than the current bin window."""
        if key == "live":
            return False  # Live bins don't expire

        current = self.current_bin_key(resolution)
        return key < current
