"""
Tile Routes — H3 tile query and metadata endpoints.

Static tile files are served directly by FastAPI's StaticFiles mount.
These routes provide tile listing, metadata, and query capabilities.

Version History:
    0.1.0  2026-03-28  Initial tile routes
    0.2.0  2026-04-02  Typed Pydantic response models for OpenAPI spec
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from spatial_agents.models import (
    BboxResponse,
    CellCenter,
    PositionCellsResponse,
    TileInfoResponse,
    TileStatsResponse,
)
from spatial_agents.spatial.h3_indexer import H3Indexer
from spatial_agents.spatial.tile_builder import TileBuilder

router = APIRouter()

_indexer = H3Indexer()
_builder = TileBuilder()


@router.get("/info/{h3_cell}", response_model=TileInfoResponse)
async def tile_info(h3_cell: str) -> TileInfoResponse:
    """Return metadata about an H3 cell — center, boundary, resolution, neighbors."""
    center = _indexer.cell_to_center(h3_cell)
    boundary = _indexer.cell_to_boundary_geojson(h3_cell)
    resolution = _indexer.get_resolution(h3_cell)
    neighbors = list(_indexer.get_neighbors(h3_cell))

    return TileInfoResponse(
        cell_id=h3_cell,
        resolution=resolution,
        center=CellCenter(lat=center[0], lng=center[1]),
        boundary=boundary,
        edge_length_km=_indexer.edge_length_km(resolution),
        neighbors=neighbors,
    )


@router.get("/bbox", response_model=BboxResponse)
async def tiles_in_bbox(
    min_lat: float = Query(..., ge=-90, le=90),
    min_lng: float = Query(..., ge=-180, le=180),
    max_lat: float = Query(..., ge=-90, le=90),
    max_lng: float = Query(..., ge=-180, le=180),
    resolution: int = Query(default=5, ge=0, le=15),
) -> BboxResponse:
    """Return H3 cell IDs covering a bounding box at a given resolution."""
    cells = _indexer.bbox_to_cells(min_lat, min_lng, max_lat, max_lng, resolution)
    return BboxResponse(
        bbox=[min_lat, min_lng, max_lat, max_lng],
        resolution=resolution,
        cell_count=len(cells),
        cells=sorted(cells),
    )


@router.get("/stats", response_model=TileStatsResponse)
async def tile_stats() -> TileStatsResponse:
    """Return statistics about generated tiles on disk."""
    data = _builder.tile_stats()
    return TileStatsResponse(**data)


@router.get("/position", response_model=PositionCellsResponse)
async def cell_for_position(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
) -> PositionCellsResponse:
    """Return H3 cell IDs at all resolutions for a given lat/lng position."""
    cells = _indexer.position_to_cells(lat, lng)
    return PositionCellsResponse(
        lat=lat,
        lng=lng,
        cells=cells,
    )
