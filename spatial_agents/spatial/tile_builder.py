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
        cell_id: str,
        resolution: int,
        temporal_bin: str,
        vessels: list[VesselRecord] | None = None,
        aircraft: list[AircraftRecord] | None = None,
    ) -> Path:
        """
        Generate a single tile file.

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
        tile_path = self._tile_path(cell_id, resolution, temporal_bin)
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
    ) -> list[Path]:
        """
        Build tiles for all H3 cells occupied by the given records at a specific resolution.

        Groups records by their H3 cell assignment and builds one tile per cell.
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
                cell_id=cell_id,
                resolution=resolution,
                temporal_bin=temporal_bin,
                vessels=vessel_cells.get(cell_id, []),
                aircraft=aircraft_cells.get(cell_id, []),
            )
            paths.append(path)

        logger.info(
            "Built %d tiles at resolution %d (bin: %s)",
            len(paths), resolution, temporal_bin,
        )
        return paths

    def build_all_resolutions(
        self,
        vessels: list[VesselRecord],
        aircraft: list[AircraftRecord],
    ) -> dict[int, list[Path]]:
        """Build tiles across all configured resolutions."""
        result: dict[int, list[Path]] = {}
        for res in config.tiling.resolutions:
            result[res] = self.build_tiles_for_records(vessels, aircraft, res)
        return result

    def _tile_path(self, cell_id: str, resolution: int, temporal_bin: str) -> Path:
        """Compute the file path for a tile."""
        return self._output_dir / str(resolution) / cell_id / f"{temporal_bin}.json"

    def get_tile(self, cell_id: str, resolution: int, temporal_bin: str) -> TileContent | None:
        """Read a tile from disk, if it exists."""
        path = self._tile_path(cell_id, resolution, temporal_bin)
        if not path.exists():
            return None
        try:
            data = orjson.loads(path.read_bytes())
            return TileContent(**data)
        except Exception as exc:
            logger.error("Error reading tile %s: %s", path, exc)
            return None

    def list_tiles(self, resolution: int | None = None) -> list[Path]:
        """List all tile files, optionally filtered by resolution."""
        if resolution is not None:
            search_dir = self._output_dir / str(resolution)
            if not search_dir.exists():
                return []
            return list(search_dir.rglob("*.json"))
        return list(self._output_dir.rglob("*.json"))

    def tile_stats(self) -> dict:
        """Return statistics about tiles on disk."""
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
