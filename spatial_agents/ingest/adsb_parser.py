"""
ADS-B Parser — Fetch and parse aircraft state vectors from OpenSky Network.

Polls the OpenSky REST API at configured intervals and converts state vectors
into AircraftRecord models with H3 cell assignment.

The OpenSky API returns up to ~10,000 aircraft per bounding box query.
For global coverage, we issue multiple regional queries.

Version History:
    0.1.0  2026-03-28  Initial ADS-B parser with OpenSky integration
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import h3
import httpx

from spatial_agents.config import config
from spatial_agents.models import AircraftCategory, AircraftRecord, GeoPosition

logger = logging.getLogger(__name__)

# OpenSky emitter category → our AircraftCategory
_CATEGORY_MAP: dict[int, AircraftCategory] = {
    0: AircraftCategory.UNKNOWN,
    1: AircraftCategory.LIGHT,
    2: AircraftCategory.LIGHT,
    3: AircraftCategory.MEDIUM,
    4: AircraftCategory.MEDIUM,
    5: AircraftCategory.HEAVY,
    6: AircraftCategory.HIGH_PERFORMANCE,
    7: AircraftCategory.ROTORCRAFT,
    10: AircraftCategory.GLIDER,
    11: AircraftCategory.GLIDER,
    14: AircraftCategory.UAV,
    15: AircraftCategory.SPACE,
    17: AircraftCategory.GROUND_VEHICLE,
    18: AircraftCategory.GROUND_VEHICLE,
}

# Predefined bounding boxes for global coverage
# Each is (min_lat, max_lat, min_lng, max_lng)
GLOBAL_REGIONS: list[tuple[str, tuple[float, float, float, float]]] = [
    ("north_america", (15.0, 72.0, -170.0, -50.0)),
    ("europe", (35.0, 72.0, -15.0, 45.0)),
    ("east_asia", (10.0, 55.0, 95.0, 155.0)),
    ("south_asia", (-10.0, 40.0, 55.0, 100.0)),
    ("middle_east", (12.0, 42.0, 25.0, 65.0)),
    ("oceania", (-50.0, 0.0, 105.0, 180.0)),
    ("south_america", (-60.0, 15.0, -85.0, -30.0)),
    ("africa", (-40.0, 38.0, -20.0, 55.0)),
]

# Bay Area region for development/testing
BAY_AREA_BBOX: tuple[float, float, float, float] = (37.0, 38.5, -123.0, -121.5)


def _assign_h3_cells(lat: float, lng: float) -> dict[int, str]:
    """Assign H3 cell IDs at all configured resolutions."""
    cells: dict[int, str] = {}
    for res in config.tiling.resolutions:
        try:
            cells[res] = h3.latlng_to_cell(lat, lng, res)
        except Exception:
            pass
    return cells


class ADSBParser:
    """
    ADS-B state vector fetcher and parser.

    Polls OpenSky Network API and converts responses into AircraftRecord models.

    Usage:
        parser = ADSBParser()
        records = await parser.fetch_region(BAY_AREA_BBOX)
        for r in records:
            print(r.callsign, r.position)
    """

    def __init__(self, endpoint: str | None = None) -> None:
        self._endpoint = endpoint or config.feeds.adsb_endpoint
        self._client = httpx.AsyncClient(timeout=30.0)
        self._fetch_count = 0
        self._record_count = 0
        self._error_count = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "fetches": self._fetch_count,
            "records_parsed": self._record_count,
            "errors": self._error_count,
        }

    async def fetch_region(
        self,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> list[AircraftRecord]:
        """
        Fetch aircraft state vectors for a bounding box.

        Args:
            bbox: (min_lat, max_lat, min_lng, max_lng). If None, fetches Bay Area.

        Returns:
            List of AircraftRecord with H3 cells assigned.
        """
        if bbox is None:
            bbox = BAY_AREA_BBOX

        min_lat, max_lat, min_lng, max_lng = bbox
        url = f"{self._endpoint}/states/all"
        params = {
            "lamin": min_lat,
            "lamax": max_lat,
            "lomin": min_lng,
            "lomax": max_lng,
        }

        try:
            self._fetch_count += 1
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            self._error_count += 1
            logger.error("ADS-B fetch error: %s", exc)
            return []

        return self._parse_state_vectors(data)

    async def fetch_global(self) -> list[AircraftRecord]:
        """Fetch aircraft across all predefined global regions."""
        all_records: list[AircraftRecord] = []
        seen_icao: set[str] = set()

        for region_name, bbox in GLOBAL_REGIONS:
            records = await self.fetch_region(bbox)
            for r in records:
                if r.icao24 not in seen_icao:
                    seen_icao.add(r.icao24)
                    all_records.append(r)
            logger.info("Fetched %d aircraft from %s", len(records), region_name)

        return all_records

    def _parse_state_vectors(self, data: dict[str, Any]) -> list[AircraftRecord]:
        """Parse OpenSky API response into AircraftRecord models."""
        states = data.get("states", [])
        if not states:
            return []

        api_time = data.get("time", 0)
        records: list[AircraftRecord] = []

        for sv in states:
            record = self._state_vector_to_record(sv, api_time)
            if record is not None:
                records.append(record)
                self._record_count += 1

        return records

    def _state_vector_to_record(
        self, sv: list[Any], api_time: int
    ) -> AircraftRecord | None:
        """
        Convert a single OpenSky state vector array to AircraftRecord.

        OpenSky state vector indices:
            0: icao24, 1: callsign, 2: origin_country, 3: time_position,
            4: last_contact, 5: longitude, 6: latitude, 7: baro_altitude,
            8: on_ground, 9: velocity, 10: true_track, 11: vertical_rate,
            12: sensors, 13: geo_altitude, 14: squawk, 15: spi, 16: position_source,
            17: category (if available)
        """
        try:
            icao24 = sv[0]
            lat = sv[6]
            lng = sv[5]

            # Skip if no valid position
            if lat is None or lng is None:
                return None

            callsign = (sv[1] or "").strip()
            timestamp = sv[3] or api_time
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)

            # Altitude: prefer geometric, fall back to barometric
            alt_m = sv[13] if sv[13] is not None else sv[7]

            # Velocity: OpenSky gives m/s, convert to knots
            velocity_ms = sv[9]
            velocity_knots = velocity_ms * 1.94384 if velocity_ms is not None else None

            # Vertical rate: OpenSky gives m/s, convert to fpm
            vr_ms = sv[11]
            vr_fpm = vr_ms * 196.85 if vr_ms is not None else None

            # Category (index 17, may not exist in all responses)
            category_int = sv[17] if len(sv) > 17 else 0
            category = _CATEGORY_MAP.get(category_int or 0, AircraftCategory.UNKNOWN)

            return AircraftRecord(
                icao24=icao24,
                callsign=callsign,
                category=category,
                position=GeoPosition(lat=lat, lng=lng, alt_m=alt_m, timestamp=dt),
                velocity_knots=velocity_knots,
                vertical_rate_fpm=vr_fpm,
                heading_deg=sv[10] if sv[10] is not None and 0 <= sv[10] < 360 else None,
                on_ground=bool(sv[8]),
                squawk=sv[14] or "",
                h3_cells=_assign_h3_cells(lat, lng),
            )
        except (IndexError, TypeError, ValueError) as exc:
            self._error_count += 1
            logger.debug("ADS-B parse error: %s", exc)
            return None

    async def close(self) -> None:
        """Shut down the HTTP client."""
        await self._client.aclose()
