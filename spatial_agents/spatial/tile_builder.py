"""
Tile Builder — Generate H3 tile files from vessel and aircraft records.

Produces static JSON tile files that can be served directly by FastAPI
or synced to S3. Each tile covers one H3 cell at one resolution for
one temporal bin.

File path convention:
    {output_dir}/{resolution}/{cell_id}/{temporal_bin}.json

Version History:
    0.1.0  2026-03-28  Initial tile builder
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import h3
import orjson

from spatial_agents.config import config
from spatial_agents.models import (
    AircraftRecord,
    TileContent,
    TileMetadata,
    VesselRecord,
)
from spatial_agents.spatial.temporal_bins import TemporalBinner

logger = logging.getLogger(__name__)


class TileBuilder:
    """
    Build static H3 tile files.

    Usage:
        builder = TileBuilder()
        builder.build_tile(
            cell_id="842831dffffffff",
            resolution=4,
            temporal_bin="1hour",
            vessels=vessel_records,
            aircraft=aircraft_records,
        )
    """

    def __init__(
        self,
        output_dir: Path | None = None,
        binner: TemporalBinner | None = None,
    ) -> None:
        self._output_dir = output_dir or config.tiling.tile_output_dir
        self._binner = binner or TemporalBinner()
        self._tiles_written = 0

    @property
    def stats(self) -> dict[str, int]:
        return {"tiles_written": self._tiles_written}

    def build_tile(
        self,
        region_key: str,
        cell_id: str,
        resolution: int,
        temporal_bin: str,
        vessels: list[VesselRecord] | None = None,
        aircraft: list[AircraftRecord] | None = None,
    ) -> Path:
        """
        Generate a single tile file under the region's subtree.

        Args:
            region_key: Durable region identity (center-flower H3 cell);
                becomes the top-level path segment so regions stay isolated.

        Returns:
            Path to the written tile file.
        """
        vessels = vessels or []
        aircraft = aircraft or []

        # Compute bounding box from cell boundary
        boundary = h3.cell_to_boundary(cell_id)
        lats = [p[0] for p in boundary]
        lngs = [p[1] for p in boundary]
        bbox = (min(lats), min(lngs), max(lats), max(lngs))

        now = datetime.now(timezone.utc)

        tile = TileContent(
            metadata=TileMetadata(
                cell_id=cell_id,
                resolution=resolution,
                temporal_bin=temporal_bin,
                generated_at=now,
                vessel_count=len(vessels),
                aircraft_count=len(aircraft),
                bbox=bbox,
            ),
            vessels=vessels,
            aircraft=aircraft,
        )

        # Write file
        tile_path = self._tile_path(region_key, cell_id, resolution, temporal_bin)
        tile_path.parent.mkdir(parents=True, exist_ok=True)

        # Use orjson for fast serialization
        json_bytes = orjson.dumps(
            tile.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )
        tile_path.write_bytes(json_bytes)

        self._tiles_written += 1
        logger.debug(
            "Tile written: %s (v=%d, a=%d, %d bytes)",
            tile_path, len(vessels), len(aircraft), len(json_bytes),
        )

        return tile_path

    def build_tiles_for_records(
        self,
        vessels: list[VesselRecord],
        aircraft: list[AircraftRecord],
        resolution: int,
        region_key: str,
    ) -> list[Path]:
        """
        Build tiles for all H3 cells occupied by the given records at a specific resolution.

        Groups records by their H3 cell assignment and builds one tile per cell,
        all under `region_key`'s subtree. Callers must pass records that belong
        to that one region (partition the feed batch by region first).
        """
        # Group vessels by cell
        vessel_cells: dict[str, list[VesselRecord]] = {}
        for v in vessels:
            cell = v.h3_cells.get(resolution)
            if cell:
                vessel_cells.setdefault(cell, []).append(v)

        # Group aircraft by cell
        aircraft_cells: dict[str, list[AircraftRecord]] = {}
        for a in aircraft:
            cell = a.h3_cells.get(resolution)
            if cell:
                aircraft_cells.setdefault(cell, []).append(a)

        # Union of all occupied cells
        all_cells = set(vessel_cells.keys()) | set(aircraft_cells.keys())

        temporal_bin = self._binner.current_bin_key(resolution)
        paths: list[Path] = []

        for cell_id in all_cells:
            path = self.build_tile(
                region_key=region_key,
                cell_id=cell_id,
                resolution=resolution,
                temporal_bin=temporal_bin,
                vessels=vessel_cells.get(cell_id, []),
                aircraft=aircraft_cells.get(cell_id, []),
            )
            paths.append(path)

        logger.info(
            "Built %d tiles at resolution %d (region: %s, bin: %s)",
            len(paths), resolution, region_key, temporal_bin,
        )
        return paths

    def build_all_resolutions(
        self,
        vessels: list[VesselRecord],
        aircraft: list[AircraftRecord],
        region_key: str,
    ) -> dict[int, list[Path]]:
        """Build tiles across all configured resolutions for one region."""
        result: dict[int, list[Path]] = {}
        for res in config.tiling.resolutions:
            result[res] = self.build_tiles_for_records(vessels, aircraft, res, region_key)
        return result

    def _tile_path(
        self, region_key: str, cell_id: str, resolution: int, temporal_bin: str
    ) -> Path:
        """Compute the file path for a tile (region-segmented)."""
        return (
            self._output_dir / region_key / str(resolution) / cell_id / f"{temporal_bin}.json"
        )

    def get_tile(
        self, region_key: str, cell_id: str, resolution: int, temporal_bin: str
    ) -> TileContent | None:
        """Read a tile from disk, if it exists."""
        path = self._tile_path(region_key, cell_id, resolution, temporal_bin)
        if not path.exists():
            return None
        try:
            data = orjson.loads(path.read_bytes())
            return TileContent(**data)
        except Exception as exc:
            logger.error("Error reading tile %s: %s", path, exc)
            return None

    def list_tiles(self, resolution: int | None = None) -> list[Path]:
        """List all tile files across the per-region layout.

        Layout is <region_key>/<res>/<cell>/<bin>.json. Region trash dirs
        (``<key>.trash-*``, transiently present during a background delete)
        are excluded. Optionally filter by resolution across all regions.
        """
        tiles: list[Path] = []
        if not self._output_dir.exists():
            return tiles
        for region_dir in self._output_dir.iterdir():
            if not region_dir.is_dir() or ".trash-" in region_dir.name:
                continue
            if resolution is not None:
                res_dir = region_dir / str(resolution)
                if res_dir.is_dir():
                    tiles.extend(res_dir.rglob("*.json"))
            else:
                tiles.extend(region_dir.rglob("*.json"))
        return tiles

    def tile_stats(self) -> dict:
        """Return statistics about tiles on disk (size-only; never opens files)."""
        all_tiles = self.list_tiles()
        if not all_tiles:
            return {"total": 0}

        import os
        sizes = [os.path.getsize(p) for p in all_tiles]
        return {
            "total": len(all_tiles),
            "total_size_mb": sum(sizes) / (1024 * 1024),
            "avg_size_kb": (sum(sizes) / len(sizes)) / 1024,
        }
