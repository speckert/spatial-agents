"""
AISStream Client — WebSocket client for aisstream.io real-time AIS data.

Connects to the aisstream.io WebSocket API and yields VesselRecord models.
The API sends pre-decoded AIS data as JSON, so we don't need NMEA parsing.

Requires a free API key from https://aisstream.io

Version History:
    0.1.0  2026-03-29  Initial aisstream.io WebSocket client
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import h3
import websockets

from spatial_agents.config import config
from spatial_agents.models import GeoPosition, VesselRecord, VesselType

logger = logging.getLogger(__name__)

# AISStream uses standard ITU ship type integers
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

# SF Bay Area bounding box: [[lat_min, lng_min], [lat_max, lng_max]]
BAY_AREA_BBOX = [[37.0, -123.0], [38.5, -121.5]]


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


def _build_subscription(
    api_key: str,
    bounding_boxes: list[list[list[float]]] | None = None,
) -> str:
    """Build the aisstream.io subscription message."""
    return json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": bounding_boxes or [BAY_AREA_BBOX],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    })


class AISStreamClient:
    """
    WebSocket client for aisstream.io.

    Connects, subscribes with bounding box filters, and yields
    VesselRecord models from incoming position reports.

    Usage:
        client = AISStreamClient(api_key="your-key")
        async for record in client.stream():
            print(record.mmsi, record.position)
    """

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        bounding_boxes: list[list[list[float]]] | None = None,
    ) -> None:
        self._api_key = api_key or config.feeds.ais_api_key
        self._endpoint = endpoint or "wss://stream.aisstream.io/v0/stream"
        self._bounding_boxes = bounding_boxes or [BAY_AREA_BBOX]
        self._static_data: dict[str, dict[str, Any]] = {}  # MMSI → static info
        self._message_count = 0
        self._error_count = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "messages_received": self._message_count,
            "errors": self._error_count,
            "static_records": len(self._static_data),
        }

    async def stream(self) -> AsyncIterator[VesselRecord]:
        """
        Connect to aisstream.io and yield VesselRecord objects.

        Raises websockets exceptions on connection failure.
        Caller should handle reconnection.
        """
        if not self._api_key:
            raise ValueError(
                "AIS API key required. Set SPATIAL_AGENTS_AIS_KEY env var "
                "or pass api_key to AISStreamClient. "
                "Get a free key at https://aisstream.io"
            )

        subscription = _build_subscription(self._api_key, self._bounding_boxes)

        async with websockets.connect(self._endpoint) as ws:
            await ws.send(subscription)
            logger.info(
                "AISStream connected — endpoint: %s, bboxes: %d",
                self._endpoint,
                len(self._bounding_boxes),
            )

            async for raw_msg in ws:
                self._message_count += 1
                try:
                    data = json.loads(raw_msg)
                    record = self._parse_message(data)
                    if record is not None:
                        yield record
                except Exception as exc:
                    self._error_count += 1
                    logger.debug("AISStream parse error: %s", exc)

    def _parse_message(self, data: dict[str, Any]) -> VesselRecord | None:
        """Parse an aisstream.io JSON message into a VesselRecord."""
        msg_type = data.get("MessageType", "")
        meta = data.get("MetaData", {})
        message = data.get("Message", {})

        if msg_type == "ShipStaticData":
            self._cache_static_data(message.get("ShipStaticData", {}))
            return None

        if msg_type == "PositionReport":
            return self._parse_position_report(
                message.get("PositionReport", {}),
                meta,
            )

        return None

    def _cache_static_data(self, static: dict[str, Any]) -> None:
        """Cache ship static data for enriching position reports."""
        mmsi = str(static.get("UserID", ""))
        if mmsi:
            self._static_data[mmsi] = {
                "name": static.get("Name", "").strip(),
                "ship_type": static.get("Type"),
                "destination": static.get("Destination", "").strip(),
            }

    def _parse_position_report(
        self,
        report: dict[str, Any],
        meta: dict[str, Any],
    ) -> VesselRecord | None:
        """Convert a PositionReport message to a VesselRecord."""
        lat = meta.get("latitude")
        lng = meta.get("longitude")
        mmsi = str(meta.get("MMSI", ""))

        # Validate position
        if lat is None or lng is None:
            return None
        if abs(lat) > 90 or abs(lng) > 180:
            return None
        if lat == 91.0 or lng == 181.0:
            return None

        # Enrich from cached static data
        static = self._static_data.get(mmsi, {})
        vessel_name = static.get("name", "") or meta.get("ShipName", "").strip()
        ship_type = static.get("ship_type")

        # Parse timestamp from metadata
        time_str = meta.get("time_utc", "")
        try:
            # aisstream format: "2024-01-01 12:00:00.000000Z +0000 UTC"
            timestamp = datetime.fromisoformat(
                time_str.split("+")[0].strip().rstrip("Z")
            ).replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            timestamp = datetime.now(timezone.utc)

        heading = report.get("TrueHeading")
        if heading is not None and (heading >= 511 or heading >= 360):
            heading = None

        speed = report.get("Sog")
        if speed is not None and speed >= 102.3:
            speed = None

        cog = report.get("Cog")
        if cog is not None and cog >= 360:
            cog = None

        return VesselRecord(
            mmsi=mmsi,
            name=vessel_name,
            vessel_type=_classify_vessel_type(ship_type),
            position=GeoPosition(lat=lat, lng=lng, timestamp=timestamp),
            heading_deg=float(heading) if heading is not None else None,
            speed_knots=float(speed) if speed is not None else None,
            course_deg=float(cog) if cog is not None else None,
            destination=static.get("destination", ""),
            h3_cells=_assign_h3_cells(lat, lng),
        )
