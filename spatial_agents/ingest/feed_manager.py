"""
Feed Manager — orchestrates data source connections, reconnection, and health monitoring.

Manages the lifecycle of AIS and ADS-B feeds, providing a unified interface
for the pipeline to consume incoming records.

Version History:
    0.1.0  2026-03-28  Initial feed manager with AIS WebSocket + ADS-B polling
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from spatial_agents.config import config
from spatial_agents.ingest.adsb_parser import ADSBParser, BAY_AREA_BBOX
from spatial_agents.ingest.ais_parser import AISParser
from spatial_agents.ingest.aisstream_client import AISStreamClient
from spatial_agents.models import AircraftRecord, FeedStatus, VesselRecord

logger = logging.getLogger(__name__)


class FeedManager:
    """
    Unified feed manager for all data sources.

    Provides async iterators for vessel and aircraft records,
    handles reconnection on failure, and exposes health metrics.

    Usage:
        manager = FeedManager()
        await manager.start()

        # Consume records
        async for record in manager.vessel_stream():
            process(record)

        await manager.stop()
    """

    def __init__(
        self,
        ais_parser: AISParser | None = None,
        adsb_parser: ADSBParser | None = None,
        aisstream_client: AISStreamClient | None = None,
    ) -> None:
        self._ais_parser = ais_parser or AISParser()
        self._adsb_parser = adsb_parser or ADSBParser()
        self._aisstream = aisstream_client or AISStreamClient()

        # Record buffers — bounded deques to prevent memory growth
        self._vessel_buffer: deque[VesselRecord] = deque(maxlen=50_000)
        self._aircraft_buffer: deque[AircraftRecord] = deque(maxlen=50_000)

        # Latest records indexed by identifier for quick lookups
        self._vessel_latest: dict[str, VesselRecord] = {}
        self._aircraft_latest: dict[str, AircraftRecord] = {}

        # Health tracking
        self._start_time: float = 0.0
        self._ais_last_msg: datetime | None = None
        self._adsb_last_msg: datetime | None = None
        self._ais_msg_count = 0
        self._adsb_msg_count = 0
        self._ais_error: str | None = None
        self._adsb_error: str | None = None

        # Control
        self._running = False
        self._tasks: list[asyncio.Task] = []

        # Callbacks for downstream processing
        self._vessel_callbacks: list[Callable[[VesselRecord], None]] = []
        self._aircraft_callbacks: list[Callable[[AircraftRecord], None]] = []

    def on_vessel(self, callback: Callable[[VesselRecord], None]) -> None:
        """Register a callback for new vessel records."""
        self._vessel_callbacks.append(callback)

    def on_aircraft(self, callback: Callable[[AircraftRecord], None]) -> None:
        """Register a callback for new aircraft records."""
        self._aircraft_callbacks.append(callback)

    async def start(self) -> None:
        """Start all data feeds."""
        self._running = True
        self._start_time = time.monotonic()

        self._tasks = [
            asyncio.create_task(self._adsb_poll_loop(), name="adsb_poll"),
        ]
        # Start AIS WebSocket if API key is configured
        if config.feeds.ais_api_key:
            self._tasks.append(
                asyncio.create_task(self._ais_websocket_loop(), name="ais_ws")
            )
            logger.info("AIS WebSocket feed enabled")
        else:
            logger.warning(
                "AIS WebSocket disabled — set SPATIAL_AGENTS_AIS_KEY to enable"
            )
        logger.info("Feed manager started — %d active feeds", len(self._tasks))

    async def stop(self) -> None:
        """Stop all feeds and clean up."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._adsb_parser.close()
        logger.info("Feed manager stopped")

    def get_latest_vessels(self) -> list[VesselRecord]:
        """Return latest known position for each vessel."""
        return list(self._vessel_latest.values())

    def get_latest_aircraft(self) -> list[AircraftRecord]:
        """Return latest known state for each aircraft."""
        return list(self._aircraft_latest.values())

    def get_vessels_in_cell(self, h3_cell: str, resolution: int) -> list[VesselRecord]:
        """Return vessels currently in a specific H3 cell."""
        return [
            v for v in self._vessel_latest.values()
            if v.h3_cells.get(resolution) == h3_cell
        ]

    def get_aircraft_in_cell(self, h3_cell: str, resolution: int) -> list[AircraftRecord]:
        """Return aircraft currently in a specific H3 cell."""
        return [
            a for a in self._aircraft_latest.values()
            if a.h3_cells.get(resolution) == h3_cell
        ]

    def health(self) -> list[FeedStatus]:
        """Return health status for all feeds."""
        uptime = time.monotonic() - self._start_time if self._start_time else 0
        rate_window = max(uptime / 60, 1)  # avoid division by zero

        return [
            FeedStatus(
                name="ais",
                connected=self._running and self._ais_error is None,
                last_message_at=self._ais_last_msg,
                messages_per_minute=self._ais_msg_count / rate_window,
                error=self._ais_error,
            ),
            FeedStatus(
                name="adsb",
                connected=self._running and self._adsb_error is None,
                last_message_at=self._adsb_last_msg,
                messages_per_minute=self._adsb_msg_count / rate_window,
                error=self._adsb_error,
            ),
        ]

    # --- ADS-B Polling Loop ---

    async def _adsb_poll_loop(self) -> None:
        """Periodically fetch ADS-B state vectors."""
        interval = config.feeds.adsb_poll_interval_sec
        logger.info("ADS-B poll loop started — interval: %ds", interval)

        while self._running:
            try:
                records = await self._adsb_parser.fetch_region(BAY_AREA_BBOX)
                now = datetime.now(timezone.utc)
                self._adsb_last_msg = now
                self._adsb_error = None

                for record in records:
                    self._adsb_msg_count += 1
                    self._aircraft_buffer.append(record)
                    self._aircraft_latest[record.icao24] = record
                    for cb in self._aircraft_callbacks:
                        cb(record)

                logger.info("ADS-B poll: %d aircraft", len(records))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._adsb_error = str(exc)
                logger.error("ADS-B poll error: %s", exc)

            await asyncio.sleep(interval)

    # --- AIS WebSocket Loop ---

    async def _ais_websocket_loop(self) -> None:
        """
        Connect to aisstream.io WebSocket and ingest vessel position reports.
        Automatically reconnects on failure with exponential backoff.
        """
        backoff = 5

        while self._running:
            try:
                logger.info("AIS WebSocket connecting...")
                self._ais_error = None

                async for record in self._aisstream.stream():
                    if not self._running:
                        break
                    self._ais_msg_count += 1
                    self._vessel_buffer.append(record)
                    self._vessel_latest[record.mmsi] = record
                    self._ais_last_msg = datetime.now(timezone.utc)
                    for cb in self._vessel_callbacks:
                        cb(record)

                    # Reset backoff on successful data
                    backoff = 5

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._ais_error = str(exc)
                logger.error("AIS WebSocket error: %s — reconnecting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)  # Cap at 2 minutes

    def ingest_ais_batch(self, nmea_lines: list[str]) -> list[VesselRecord]:
        """
        Synchronous batch ingest for AIS data from files or test fixtures.
        Useful for offline processing and testing.
        """
        records = self._ais_parser.parse_batch(nmea_lines)
        now = datetime.now(timezone.utc)

        for record in records:
            self._ais_msg_count += 1
            self._vessel_buffer.append(record)
            self._vessel_latest[record.mmsi] = record
            self._ais_last_msg = now
            for cb in self._vessel_callbacks:
                cb(record)

        return records
