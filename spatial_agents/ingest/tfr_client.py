"""
FAA TFR Client — fetches active Temporary Flight Restrictions.

Pulls the public GeoJSON FeatureCollection of active TFRs from the FAA
GeoServer WFS endpoint, attaches a mixed-resolution H3 compact cell
cover per TFR polygon, and tags each TFR with the active region names
its polygon intersects.

The shape mirrors the NWS weather-alerts client: same polygon + H3
compact + h3_cells_geometry layout, so clients can render both layers
with identical machinery.

Version History:
    0.1.0  2026-04-25  Initial FAA TFR client using GeoServer WFS feed
                       (V_TFR_LOC layer, EPSG:4326, GeoJSON output).
                       Mixed-res H3 compact cover per TFR — Claude 4.7
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import h3
import httpx

from spatial_agents.config import ACTIVE_REGIONS, REGIONS
from spatial_agents.models import TFR

logger = logging.getLogger(__name__)


# FAA GeoServer WFS endpoint — V_TFR_LOC view exposes active TFRs as
# GeoJSON Features with EPSG:4326 polygons. Single GET, no auth.
TFR_WFS_URL = (
    "https://tfr.faa.gov/geoserver/TFR/ows"
    "?service=WFS&version=1.1.0&request=GetFeature"
    "&typeName=TFR:V_TFR_LOC"
    "&maxFeatures=500"
    "&outputFormat=application/json"
    "&srsname=EPSG:4326"
)

TFR_USER_AGENT = "spatial-agents/0.1 (admin@specktech.com)"

# Resolution at which we sample TFR polygons before compacting. Res-7
# (~1.2 km edge) is plenty for TFR shapes (typically 3–30 NM rings or
# multi-state polygons) without exploding cell counts.
H3_SAMPLE_RESOLUTION = 7


def _parse_mod_dt(raw: str | None) -> datetime | None:
    """Parse FAA's compact LAST_MODIFICATION_DATETIME format 'YYYYMMDDHHMM' → UTC datetime."""
    if not raw or len(raw) < 12:
        return None
    try:
        return datetime.strptime(raw[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _polygon_bbox(geometry: dict) -> tuple[float, float, float, float] | None:
    """Return (min_lat, max_lat, min_lng, max_lng) for a GeoJSON Polygon/MultiPolygon."""
    if not geometry:
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        rings = coords
    elif gtype == "MultiPolygon":
        rings = [ring for poly in coords for ring in poly]
    else:
        return None

    min_lat, max_lat = 90.0, -90.0
    min_lng, max_lng = 180.0, -180.0
    seen = False
    for ring in rings:
        for pt in ring:
            lng, lat = pt[0], pt[1]
            seen = True
            if lat < min_lat: min_lat = lat
            if lat > max_lat: max_lat = lat
            if lng < min_lng: min_lng = lng
            if lng > max_lng: max_lng = lng
    if not seen:
        return None
    return (min_lat, max_lat, min_lng, max_lng)


def _bboxes_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    """Bbox tuples are (min_lat, max_lat, min_lng, max_lng)."""
    return not (
        a[1] < b[0] or a[0] > b[1] or
        a[3] < b[2] or a[2] > b[3]
    )


def _polygon_regions(geometry: dict) -> list[str]:
    """Active region names whose bbox intersects the TFR polygon's bbox."""
    abox = _polygon_bbox(geometry)
    if abox is None:
        return []
    hits = []
    for name in ACTIVE_REGIONS:
        rbox = REGIONS[name]
        if _bboxes_overlap(abox, rbox):
            hits.append(name)
    return hits


def _geojson_to_h3_compact(geometry: dict) -> list[str]:
    """Convert a GeoJSON Polygon/MultiPolygon to a mixed-res H3 compact cover.

    Coordinates are GeoJSON order (lng, lat); h3.LatLngPoly expects (lat, lng).
    """
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    polys: list[h3.LatLngPoly] = []

    def _to_latlng(ring: list) -> list[tuple[float, float]]:
        out = [(pt[1], pt[0]) for pt in ring]
        if len(out) > 1 and out[0] == out[-1]:
            out = out[:-1]
        return out

    try:
        if gtype == "Polygon":
            outer = _to_latlng(coords[0]) if coords else []
            holes = [_to_latlng(r) for r in coords[1:]] if len(coords) > 1 else []
            if outer:
                polys.append(h3.LatLngPoly(outer, *holes))
        elif gtype == "MultiPolygon":
            for poly_coords in coords:
                if not poly_coords:
                    continue
                outer = _to_latlng(poly_coords[0])
                holes = [_to_latlng(r) for r in poly_coords[1:]] if len(poly_coords) > 1 else []
                if outer:
                    polys.append(h3.LatLngPoly(outer, *holes))
        else:
            return []
    except Exception as exc:
        logger.warning("Failed to build H3 polygon shape: %s", exc)
        return []

    cells: set[str] = set()
    for poly in polys:
        try:
            cells.update(h3.polygon_to_cells(poly, H3_SAMPLE_RESOLUTION))
        except Exception as exc:
            logger.warning("polygon_to_cells failed: %s", exc)

    if not cells:
        return []

    try:
        compact = h3.compact_cells(list(cells))
    except Exception as exc:
        logger.warning("compact_cells failed: %s", exc)
        return sorted(cells)

    return sorted(compact)


def _cells_to_multipolygon(cells: list[str]) -> dict:
    """Render H3 cells (any resolution) as a GeoJSON MultiPolygon."""
    polygons: list[list[list[list[float]]]] = []
    for cell in cells:
        try:
            boundary = h3.cell_to_boundary(cell)  # [(lat, lng), ...]
        except Exception:
            continue
        ring = [[lng, lat] for lat, lng in boundary]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        polygons.append([ring])
    if not polygons:
        return {}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _feature_to_tfr(feature: dict[str, Any]) -> TFR | None:
    """Convert a single FAA WFS Feature into a TFR record.

    Returns None only if the feature has no usable geometry.
    """
    geometry = feature.get("geometry")
    if not geometry:
        return None

    props = feature.get("properties") or {}
    notam_id = props.get("NOTAM_KEY") or props.get("GID") or ""
    h3_compact = _geojson_to_h3_compact(geometry)
    h3_geometry = _cells_to_multipolygon(h3_compact) if h3_compact else {}
    regions = _polygon_regions(geometry)

    return TFR(
        notam_id=str(notam_id),
        title=str(props.get("TITLE") or ""),
        type=str(props.get("LEGAL") or ""),
        state=str(props.get("STATE") or ""),
        facility=str(props.get("CNS_LOCATION_ID") or ""),
        last_modified=_parse_mod_dt(props.get("LAST_MODIFICATION_DATETIME")),
        polygon=geometry,
        h3_cells_compact=h3_compact,
        h3_cells_geometry=h3_geometry,
        regions=regions,
    )


class TFRClient:
    """Async client for FAA active TFRs (GeoServer WFS feed)."""

    def __init__(
        self,
        url: str = TFR_WFS_URL,
        user_agent: str = TFR_USER_AGENT,
        timeout_sec: float = 30.0,
    ) -> None:
        self._url = url
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
        }
        self._timeout = timeout_sec

    async def fetch_active_tfrs(self) -> list[TFR]:
        """Fetch the active TFR FeatureCollection and convert to TFR records."""
        async with httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
        ) as client:
            resp = await client.get(self._url)
            resp.raise_for_status()
            data = resp.json()

        features = data.get("features") or []
        tfrs: list[TFR] = []
        for feat in features:
            try:
                tfr = _feature_to_tfr(feat)
            except Exception as exc:
                logger.warning("Skipping malformed TFR feature: %s", exc)
                continue
            if tfr is not None:
                tfrs.append(tfr)

        # Sort: most recently modified first (newest TFRs at top)
        tfrs.sort(
            key=lambda t: -(t.last_modified.timestamp() if t.last_modified else 0),
        )
        in_regions = sum(1 for t in tfrs if t.regions)
        logger.info(
            "FAA TFR fetch: %d total features, %d with geometry, %d intersect active regions",
            len(features), len(tfrs), in_regions,
        )
        return tfrs
