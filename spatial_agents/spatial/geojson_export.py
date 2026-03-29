"""
GeoJSON Export — Convert tile content to GeoJSON FeatureCollections.

Produces standard GeoJSON that can be rendered directly by
MapKit (Swift client) or any web mapping library.

Version History:
    0.1.0  2026-03-28  Initial GeoJSON export
"""

from __future__ import annotations

from typing import Any

from spatial_agents.models import AircraftRecord, TileContent, VesselRecord
from spatial_agents.spatial.h3_indexer import H3Indexer

_indexer = H3Indexer()


def vessel_to_feature(vessel: VesselRecord) -> dict[str, Any]:
    """Convert a VesselRecord to a GeoJSON Feature."""
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [vessel.position.lng, vessel.position.lat],
        },
        "properties": {
            "entity_type": "vessel",
            "mmsi": vessel.mmsi,
            "name": vessel.name,
            "vessel_type": vessel.vessel_type.value,
            "heading_deg": vessel.heading_deg,
            "speed_knots": vessel.speed_knots,
            "course_deg": vessel.course_deg,
            "destination": vessel.destination,
            "timestamp": vessel.position.timestamp.isoformat(),
        },
    }


def aircraft_to_feature(aircraft: AircraftRecord) -> dict[str, Any]:
    """Convert an AircraftRecord to a GeoJSON Feature."""
    coords = [aircraft.position.lng, aircraft.position.lat]
    if aircraft.position.alt_m is not None:
        coords.append(aircraft.position.alt_m)

    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": coords,
        },
        "properties": {
            "entity_type": "aircraft",
            "icao24": aircraft.icao24,
            "callsign": aircraft.callsign,
            "category": aircraft.category.value,
            "velocity_knots": aircraft.velocity_knots,
            "vertical_rate_fpm": aircraft.vertical_rate_fpm,
            "heading_deg": aircraft.heading_deg,
            "on_ground": aircraft.on_ground,
            "squawk": aircraft.squawk,
            "timestamp": aircraft.position.timestamp.isoformat(),
        },
    }


def cell_boundary_feature(cell_id: str) -> dict[str, Any]:
    """Generate a GeoJSON Feature for an H3 cell boundary polygon."""
    return {
        "type": "Feature",
        "geometry": _indexer.cell_to_boundary_geojson(cell_id),
        "properties": {
            "entity_type": "h3_cell",
            "cell_id": cell_id,
            "resolution": _indexer.get_resolution(cell_id),
        },
    }


def tile_to_geojson(
    tile: TileContent,
    include_cell_boundary: bool = True,
) -> dict[str, Any]:
    """
    Convert a TileContent to a GeoJSON FeatureCollection.

    Args:
        tile: Tile content with vessels and aircraft
        include_cell_boundary: Whether to include the H3 cell polygon as a feature

    Returns:
        GeoJSON FeatureCollection dict
    """
    features: list[dict] = []

    # Optionally include the cell boundary polygon
    if include_cell_boundary:
        features.append(cell_boundary_feature(tile.metadata.cell_id))

    # Add vessel features
    for vessel in tile.vessels:
        features.append(vessel_to_feature(vessel))

    # Add aircraft features
    for aircraft in tile.aircraft:
        features.append(aircraft_to_feature(aircraft))

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "cell_id": tile.metadata.cell_id,
            "resolution": tile.metadata.resolution,
            "temporal_bin": tile.metadata.temporal_bin,
            "generated_at": tile.metadata.generated_at.isoformat(),
            "vessel_count": tile.metadata.vessel_count,
            "aircraft_count": tile.metadata.aircraft_count,
        },
    }
