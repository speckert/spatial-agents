"""
AIS Parser — Decode NMEA 0183 AIS messages into VesselRecord models.

Handles both single-line and multi-line NMEA sentences. Extracts position
reports (message types 1-3, 18, 19) and static voyage data (type 5, 24).

Uses pyais for the heavy decoding, wraps results in our Pydantic models
with H3 cell assignment at configured resolutions.

Version History:
    0.1.0  2026-03-28  Initial AIS parser with pyais integration
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import h3
from pyais import decode as pyais_decode

from spatial_agents.config import config
from spatial_agents.models import GeoPosition, VesselRecord, VesselType

logger = logging.getLogger(__name__)

# AIS ship type ranges → our simplified VesselType enum
_AIS_TYPE_MAP: dict[range, VesselType] = {
    range(70, 80): VesselType.CARGO,
    range(80, 90): VesselType.TANKER,
    range(60, 70): VesselType.PASSENGER,
    range(30, 33): VesselType.FISHING,
    range(31, 33): VesselType.TUG,
    range(35, 36): VesselType.MILITARY,
    range(36, 37): VesselType.SAILING,
    range(37, 38): VesselType.PLEASURE,
    range(40, 50): VesselType.HIGH_SPEED,
}


def _classify_vessel_type(ais_type: int | None) -> VesselType:
    """Map AIS ship type integer to VesselType enum."""
    if ais_type is None:
        return VesselType.UNKNOWN
    for type_range, vessel_type in _AIS_TYPE_MAP.items():
        if ais_type in type_range:
            return vessel_type
    return VesselType.OTHER


def _assign_h3_cells(lat: float, lng: float) -> dict[int, str]:
    """Assign H3 cell IDs at all configured resolutions."""
    cells: dict[int, str] = {}
    for res in config.tiling.resolutions:
        try:
            cells[res] = h3.latlng_to_cell(lat, lng, res)
        except Exception:
            pass
    return cells


class AISParser:
    """
    Stateful AIS message parser.

    Accumulates multi-sentence messages and yields VesselRecord
    objects as complete position reports are decoded.

    Usage:
        parser = AISParser()
        for record in parser.parse_nmea_line(raw_line):
            print(record.mmsi, record.position)
    """

    def __init__(self) -> None:
        self._static_data: dict[str, dict] = {}  # MMSI → static voyage data
        self._message_count = 0
        self._error_count = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "messages_parsed": self._message_count,
            "errors": self._error_count,
            "static_records": len(self._static_data),
        }

    def parse_nmea_line(self, raw: str) -> list[VesselRecord]:
        """
        Parse a single NMEA sentence (or fragment of a multi-sentence message).
        Returns a list of VesselRecord — usually 0 or 1 items.
        """
        results: list[VesselRecord] = []

        try:
            decoded_messages = pyais_decode(raw.strip())
        except Exception as exc:
            self._error_count += 1
            logger.debug("AIS decode error: %s — raw: %s", exc, raw[:80])
            return results

        for msg in decoded_messages:
            self._message_count += 1
            record = self._message_to_record(msg)
            if record is not None:
                results.append(record)

        return results

    def parse_batch(self, lines: list[str]) -> list[VesselRecord]:
        """Parse a batch of NMEA lines, returning all decoded records."""
        records: list[VesselRecord] = []
        for line in lines:
            records.extend(self.parse_nmea_line(line))
        return records

    def _message_to_record(self, msg: Any) -> VesselRecord | None:
        """Convert a decoded pyais message to a VesselRecord, if it's a position report."""
        decoded = msg.asdict()
        msg_type = decoded.get("msg_type")

        # Static/voyage data — cache for enrichment
        if msg_type in (5, 24):
            mmsi = str(decoded.get("mmsi", ""))
            if mmsi:
                self._static_data[mmsi] = decoded
            return None

        # Position reports: types 1-3 (Class A), 18-19 (Class B)
        if msg_type not in (1, 2, 3, 18, 19):
            return None

        lat = decoded.get("lat")
        lng = decoded.get("lon")
        mmsi = str(decoded.get("mmsi", ""))

        # Validate position — AIS uses 91/181 as "not available"
        if lat is None or lng is None or abs(lat) > 90 or abs(lng) > 180:
            return None
        if lat == 91.0 or lng == 181.0:
            return None

        # Enrich from cached static data
        static = self._static_data.get(mmsi, {})
        vessel_name = static.get("shipname", "").strip()
        ship_type = static.get("ship_type") or decoded.get("ship_type")
        destination = static.get("destination", "").strip()

        now = datetime.now(timezone.utc)

        return VesselRecord(
            mmsi=mmsi,
            name=vessel_name,
            vessel_type=_classify_vessel_type(ship_type),
            position=GeoPosition(lat=lat, lng=lng, timestamp=now),
            heading_deg=_safe_heading(decoded.get("heading")),
            speed_knots=_safe_speed(decoded.get("speed")),
            course_deg=_safe_heading(decoded.get("course")),
            destination=destination,
            h3_cells=_assign_h3_cells(lat, lng),
        )


def _safe_heading(val: float | int | None) -> float | None:
    """AIS heading 511 = not available."""
    if val is None or val == 511 or val >= 360:
        return None
    return float(val)


def _safe_speed(val: float | int | None) -> float | None:
    """AIS speed 102.3 = not available."""
    if val is None or val >= 102.3:
        return None
    return float(val)
