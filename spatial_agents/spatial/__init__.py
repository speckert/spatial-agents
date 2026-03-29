"""
Spatial — H3 hexagonal indexing and tile generation.

Transforms positioned entity records into multi-resolution H3 tiles
with temporal binning, served as static JSON/GeoJSON files.

Version History:
    0.1.0  2026-03-28  Initial spatial package with H3 indexer, tile builder,
                       temporal binner, and GeoJSON export
"""

from spatial_agents.spatial.h3_indexer import H3Indexer
from spatial_agents.spatial.tile_builder import TileBuilder
from spatial_agents.spatial.temporal_bins import TemporalBinner
from spatial_agents.spatial.geojson_export import tile_to_geojson

__all__ = ["H3Indexer", "TileBuilder", "TemporalBinner", "tile_to_geojson"]
