"""
NOAA NWS Weather Alerts Client — fetches active alerts from api.weather.gov.

Pulls the public GeoJSON FeatureCollection of currently active alerts,
filters to those that intersect any active region's bbox, and emits
WeatherAlert records carrying the original NWS polygon plus a
mixed-resolution H3 compact cell set covering the alert polygon.

The H3 compact cell set is the architecture-demo payload — it shows
how a real-world polygon (a tornado warning, a small-craft advisory)
maps onto the same hex tile structure that anchors the regions, and
enables fast spatial joins (e.g. "which aircraft are inside this
alert?") against entity h3_cells.

Version History:
    0.1.0  2026-04-25  Initial NWS active-alerts client with mixed-res H3
                       compact cell coverage per alert — Claude 4.7
    0.2.0  2026-04-25  Pre-render h3_cells_geometry (GeoJSON MultiPolygon
                       of the compact cell set) so clients can visualize
                       the cover without an h3 dependency — Claude 4.7
    0.3.0  2026-04-25  Keep all CONUS alerts with geometry (no longer
                       filter to active regions at ingest); regions[] is
                       populated per-alert and the request-time endpoint
                       handles ?region= filtering — Claude 4.7
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import h3
import httpx

from spatial_agents.config import ACTIVE_REGIONS, REGION_CELLS, REGIONS
from spatial_agents.models import WeatherAlert, WeatherAlertSeverity

logger = logging.getLogger(__name__)


NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
# NWS asks for an identifying User-Agent including a contact email.
# https://www.weather.gov/documentation/services-web-api
NWS_USER_AGENT = "spatial-agents/0.1 (admin@specktech.com)"

# Resolution at which we sample alert polygons before compacting. Res-7 is
# ~1.2 km edge, fine enough for warning-scale polygons (counties, marine
# zones, county fragments) without exploding cell counts on large advisories.
H3_SAMPLE_RESOLUTION = 7


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from NWS, returning UTC-aware datetime."""
    if not value:
        return None
    try:
        # NWS returns offsets like '2026-04-25T18:00:00-07:00'
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_severity(raw: str | None) -> WeatherAlertSeverity:
    """Map NWS CAP severity string to our enum."""
    if not raw:
        return WeatherAlertSeverity.UNKNOWN
    try:
        return WeatherAlertSeverity(raw.lower())
    except ValueError:
        return WeatherAlertSeverity.UNKNOWN


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
        a[1] < b[0] or a[0] > b[1] or  # lat disjoint
        a[3] < b[2] or a[2] > b[3]     # lng disjoint
    )


def _polygon_regions(geometry: dict) -> list[str]:
    """Active region names whose bbox intersects the alert polygon's bbox."""
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
        # h3 wants the ring closed implicitly; remove duplicate closing point if present
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


def _feature_to_alert(feature: dict[str, Any]) -> WeatherAlert | None:
    """Convert a single NWS alert Feature into a WeatherAlert.

    Returns None only if the feature has no usable geometry. Alerts that
    don't intersect any active region are still returned (with regions=[])
    so global queries can see them; per-region queries filter at the
    endpoint level by checking `regions` membership.
    """
    geometry = feature.get("geometry")
    if not geometry:
        # Zone-only alerts have no geometry; skip until we wire zone resolution.
        return None

    regions = _polygon_regions(geometry)

    props = feature.get("properties") or {}
    alert_id = props.get("id") or feature.get("id") or ""
    h3_compact = _geojson_to_h3_compact(geometry)
    h3_geometry = _cells_to_multipolygon(h3_compact) if h3_compact else {}

    return WeatherAlert(
        id=str(alert_id),
        event=str(props.get("event") or "Alert"),
        severity=_parse_severity(props.get("severity")),
        headline=str(props.get("headline") or ""),
        description=str(props.get("description") or ""),
        sender=str(props.get("senderName") or ""),
        effective_at=_parse_dt(props.get("effective")),
        expires_at=_parse_dt(props.get("expires") or props.get("ends")),
        polygon=geometry,
        h3_cells_compact=h3_compact,
        h3_cells_geometry=h3_geometry,
        regions=regions,
    )


class NWSClient:
    """Async client for NOAA NWS active alerts."""

    def __init__(
        self,
        url: str = NWS_ALERTS_URL,
        user_agent: str = NWS_USER_AGENT,
        timeout_sec: float = 30.0,
    ) -> None:
        self._url = url
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "application/geo+json",
        }
        self._timeout = timeout_sec

    async def fetch_active_alerts(self) -> list[WeatherAlert]:
        """Fetch current active alerts and filter to active regions."""
        async with httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
        ) as client:
            resp = await client.get(self._url)
            resp.raise_for_status()
            data = resp.json()

        features = data.get("features") or []
        alerts: list[WeatherAlert] = []
        for feat in features:
            try:
                alert = _feature_to_alert(feat)
            except Exception as exc:
                logger.warning("Skipping malformed NWS alert: %s", exc)
                continue
            if alert is not None:
                alerts.append(alert)

        # Sort: highest severity first, then most recent expiry
        severity_rank = {
            WeatherAlertSeverity.EXTREME: 0,
            WeatherAlertSeverity.SEVERE: 1,
            WeatherAlertSeverity.MODERATE: 2,
            WeatherAlertSeverity.MINOR: 3,
            WeatherAlertSeverity.UNKNOWN: 4,
        }
        alerts.sort(
            key=lambda a: (severity_rank.get(a.severity, 99),
                           -(a.expires_at.timestamp() if a.expires_at else 0)),
        )
        in_regions = sum(1 for a in alerts if a.regions)
        logger.info(
            "NWS fetch: %d total features, %d with geometry, %d intersect active regions",
            len(features), len(alerts), in_regions,
        )
        return alerts
