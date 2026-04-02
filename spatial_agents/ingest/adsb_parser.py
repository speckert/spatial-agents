"""
ADS-B Parser — Fetch and parse aircraft state vectors from OpenSky Network.

Polls the OpenSky REST API at configured intervals and converts state vectors
into AircraftRecord models with H3 cell assignment.

Uses OAuth2 client credentials flow for authentication.

Version History:
    0.1.0  2026-03-28  Initial ADS-B parser with OpenSky integration
    0.2.0  2026-03-30  Added OpenSky Basic auth support and 429 backoff
    0.3.0  2026-03-31  Switched to OAuth2 client credentials, exponential
                       backoff, tighter Bay Area bbox
    0.4.0  2026-04-02  Added flight_phase classification at ingest time
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import h3
import httpx

from spatial_agents.config import config
from spatial_agents.models import AircraftCategory, AircraftRecord, GeoPosition, classify_flight_phase

logger = logging.getLogger(__name__)

# OpenSky OAuth2 token endpoint
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)

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

# Bay Area region — SFO, OAK, SF, Marin, and East Bay
BAY_AREA_BBOX: tuple[float, float, float, float] = (37.25, 38.2, -122.78, -121.8)


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

    Polls OpenSky Network API using OAuth2 client credentials and converts
    responses into AircraftRecord models.

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
        self._backoff_until: float = 0
        self._consecutive_429s: int = 0

        # OAuth2 state
        self._client_id = config.feeds.adsb_client_id
        self._client_secret = config.feeds.adsb_client_secret
        self._access_token: str | None = None
        self._token_expires_at: float = 0

        if self._client_id and self._client_secret:
            logger.info("OpenSky OAuth2 configured — client: %s", self._client_id)
        else:
            logger.info("OpenSky running in anonymous mode (no OAuth2 credentials)")

    async def _ensure_token(self) -> None:
        """Obtain or refresh the OAuth2 access token."""
        if not self._client_id or not self._client_secret:
            return

        # Refresh 60 seconds before expiry
        if self._access_token and time.monotonic() < (self._token_expires_at - 60):
            return

        try:
            response = await self._client.post(
                OPENSKY_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            token_data = response.json()
            self._access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 1800)
            self._token_expires_at = time.monotonic() + expires_in
            logger.info(
                "OpenSky OAuth2 token acquired — expires in %d min",
                expires_in // 60,
            )
        except Exception as exc:
            logger.error("OpenSky OAuth2 token request failed: %s", exc)
            self._access_token = None

    def _auth_headers(self) -> dict[str, str]:
        """Return Authorization header if we have a token."""
        if self._access_token:
            return {"Authorization": f"Bearer {self._access_token}"}
        return {}

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

        # Skip if backing off from a 429
        now = time.monotonic()
        if now < self._backoff_until:
            remaining = int(self._backoff_until - now)
            logger.debug("ADS-B backing off for %ds", remaining)
            return []

        # Ensure we have a valid OAuth2 token
        await self._ensure_token()

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
            response = await self._client.get(
                url, params=params, headers=self._auth_headers(),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            self._error_count += 1
            if exc.response.status_code == 429:
                self._consecutive_429s += 1
                wait = min(300 * (2 ** (self._consecutive_429s - 1)), 3600)
                self._backoff_until = time.monotonic() + wait
                logger.warning(
                    "ADS-B rate limited (attempt %d) — backing off %d min",
                    self._consecutive_429s, wait // 60,
                )
            elif exc.response.status_code == 401:
                logger.warning("OpenSky token expired — will refresh on next poll")
                self._access_token = None
            else:
                logger.error("ADS-B fetch error: %s", exc)
            return []
        except httpx.HTTPError as exc:
            self._error_count += 1
            logger.error("ADS-B fetch error: %s", exc)
            return []

        self._consecutive_429s = 0
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

            on_ground = bool(sv[8])

            return AircraftRecord(
                icao24=icao24,
                callsign=callsign,
                category=category,
                position=GeoPosition(lat=lat, lng=lng, alt_m=alt_m, timestamp=dt),
                velocity_knots=velocity_knots,
                vertical_rate_fpm=vr_fpm,
                heading_deg=sv[10] if sv[10] is not None and 0 <= sv[10] < 360 else None,
                on_ground=on_ground,
                squawk=sv[14] or "",
                flight_phase=classify_flight_phase(on_ground, vr_fpm, alt_m),
                h3_cells=_assign_h3_cells(lat, lng),
            )
        except (IndexError, TypeError, ValueError) as exc:
            self._error_count += 1
            logger.debug("ADS-B parse error: %s", exc)
            return None

    async def close(self) -> None:
        """Shut down the HTTP client."""
        await self._client.aclose()
