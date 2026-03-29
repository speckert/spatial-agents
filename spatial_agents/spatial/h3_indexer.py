"""
H3 Indexer — Multi-resolution hexagonal spatial indexing.

Wraps h3-py to provide cell assignment, neighbor lookups,
bounding box queries, and cell boundary geometry export.

Design note:
    This is the vector-event equivalent of raster image pyramids.
    The same concept of level-of-detail hierarchies applied to
    point events rather than pixels.

Version History:
    0.1.0  2026-03-28  Initial H3 indexer
    0.1.1  2026-03-28  Removed external references from design note
"""

from __future__ import annotations

import logging
from typing import Sequence

import h3

from spatial_agents.config import config

logger = logging.getLogger(__name__)


class H3Indexer:
    """
    Multi-resolution H3 spatial indexer.

    Assigns geographic positions to H3 cells at configured resolutions
    and provides spatial query operations.

    Usage:
        indexer = H3Indexer()
        cells = indexer.position_to_cells(37.8044, -122.2712)
        # {3: '832830fffffffff', 4: '842831dffffffff', ...}

        neighbors = indexer.get_neighbors('842831dffffffff')
        # Set of 6 adjacent cell IDs
    """

    def __init__(self, resolutions: list[int] | None = None) -> None:
        self._resolutions = resolutions or config.tiling.resolutions

    @property
    def resolutions(self) -> list[int]:
        return self._resolutions

    def position_to_cells(self, lat: float, lng: float) -> dict[int, str]:
        """
        Assign a position to H3 cells at all configured resolutions.

        Returns:
            Dict mapping resolution → cell_id string
        """
        cells: dict[int, str] = {}
        for res in self._resolutions:
            try:
                cells[res] = h3.latlng_to_cell(lat, lng, res)
            except Exception as exc:
                logger.debug("H3 cell error at res %d for (%f, %f): %s", res, lat, lng, exc)
        return cells

    def cell_to_center(self, cell_id: str) -> tuple[float, float]:
        """Return the center (lat, lng) of an H3 cell."""
        return h3.cell_to_latlng(cell_id)

    def cell_to_boundary(self, cell_id: str) -> list[tuple[float, float]]:
        """Return the boundary polygon vertices of an H3 cell as (lat, lng) pairs."""
        return list(h3.cell_to_boundary(cell_id))

    def cell_to_boundary_geojson(self, cell_id: str) -> dict:
        """
        Return cell boundary as a GeoJSON Polygon geometry.
        Note: GeoJSON uses [lng, lat] coordinate order.
        """
        boundary = self.cell_to_boundary(cell_id)
        # Convert (lat, lng) to [lng, lat] and close the ring
        coords = [[lng, lat] for lat, lng in boundary]
        coords.append(coords[0])  # close polygon ring
        return {
            "type": "Polygon",
            "coordinates": [coords],
        }

    def get_neighbors(self, cell_id: str) -> set[str]:
        """Return the set of immediately adjacent H3 cells (k-ring 1, excluding center)."""
        ring = set(h3.grid_disk(cell_id, 1))
        ring.discard(cell_id)
        return ring

    def get_disk(self, cell_id: str, k: int = 1) -> set[str]:
        """Return all cells within k steps of the given cell (including center)."""
        return set(h3.grid_disk(cell_id, k))

    def get_resolution(self, cell_id: str) -> int:
        """Return the resolution of an H3 cell."""
        return h3.get_resolution(cell_id)

    def cell_to_parent(self, cell_id: str, parent_res: int) -> str:
        """Return the parent cell at a coarser resolution."""
        return h3.cell_to_parent(cell_id, parent_res)

    def cell_to_children(self, cell_id: str, child_res: int) -> set[str]:
        """Return all child cells at a finer resolution."""
        return h3.cell_to_children(cell_id, child_res)

    def bbox_to_cells(
        self,
        min_lat: float,
        min_lng: float,
        max_lat: float,
        max_lng: float,
        resolution: int,
    ) -> set[str]:
        """
        Find all H3 cells that intersect a bounding box at a given resolution.

        Uses a polyfill approach: generates a polygon from the bbox corners
        and finds all cells whose centers fall within it.
        """
        # Construct a simple rectangular polygon
        # GeoJSON polygon: [[lng, lat], ...] — counter-clockwise
        polygon = {
            "type": "Polygon",
            "coordinates": [[
                [min_lng, min_lat],
                [max_lng, min_lat],
                [max_lng, max_lat],
                [min_lng, max_lat],
                [min_lng, min_lat],
            ]],
        }
        try:
            cells = h3.geo_to_cells(polygon, resolution)
            return set(cells)
        except Exception as exc:
            logger.error("H3 polyfill error: %s", exc)
            return set()

    def edge_length_km(self, resolution: int) -> float:
        """Return the average edge length in km for a given resolution."""
        # Approximate edge lengths from H3 documentation
        edge_lengths = {
            0: 1107.712,
            1: 418.676,
            2: 158.244,
            3: 59.811,
            4: 22.606,
            5: 8.544,
            6: 3.229,
            7: 1.220,
            8: 0.461,
            9: 0.174,
            10: 0.066,
            11: 0.025,
            12: 0.009,
            13: 0.003,
            14: 0.001,
            15: 0.001,
        }
        return edge_lengths.get(resolution, 0.0)

    def cells_summary(self, cells: Sequence[str]) -> dict:
        """Return a summary of a collection of cells."""
        if not cells:
            return {"count": 0}

        resolutions = [h3.get_resolution(c) for c in cells]
        return {
            "count": len(cells),
            "resolution_min": min(resolutions),
            "resolution_max": max(resolutions),
            "unique_resolutions": sorted(set(resolutions)),
        }
