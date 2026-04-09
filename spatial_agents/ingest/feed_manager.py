"""
Feed Manager — orchestrates data source connections, reconnection, and health monitoring.

Manages the lifecycle of AIS and ADS-B feeds, providing a unified interface
for the pipeline to consume incoming records.

Version History:
    0.1.0  2026-03-28  Initial feed manager with AIS WebSocket + ADS-B polling
    0.2.0  2026-03-31  Added per-entity position history (5-point tracks) for
                       vessel and aircraft trail rendering
    0.3.0  2026-04-02  Stale vessel eviction (8 hr) with near-edge logging,
                       aircraft eviction cutoff 10 min
    0.4.0  2026-04-02  Flight phase state machine — enforces valid transitions
                       (ground→departure→climbing→cruising→descending→approach→ground),
                       handles go-arounds and missed approaches
    0.5.0  2026-04-09  Bbox driven by centralized REGION in config.py,
                       periodic feed status logger (60s) with AIS flow warnings
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Callable

from spatial_agents.config import REGION, config
from spatial_agents.ingest.adsb_parser import ADSBParser, REGION_BBOX
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

        # Position history for trails — deques of (lng, lat) tuples, newest last
        self._vessel_tracks: dict[str, deque[tuple[float, float]]] = {}
        self._aircraft_tracks: dict[str, deque[tuple[float, float]]] = {}

        # Flight phase state machine — tracks prior phase per aircraft
        self._aircraft_phase: dict[str, str] = {}

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

    _TRACK_MAXLEN = 5  # current + 4 prior positions

    def _update_track(
        self,
        tracks: dict[str, deque[tuple[float, float]]],
        entity_id: str,
        lng: float,
        lat: float,
    ) -> None:
        """Append a position to an entity's track, skipping duplicates."""
        if entity_id not in tracks:
            tracks[entity_id] = deque(maxlen=self._TRACK_MAXLEN)
        trail = tracks[entity_id]
        if not trail or trail[-1] != (lng, lat):
            trail.append((lng, lat))

    def get_vessel_track(self, mmsi: str) -> list[tuple[float, float]]:
        """Return position history for a vessel as [(lng, lat), ...]."""
        return list(self._vessel_tracks.get(mmsi, []))

    def get_aircraft_track(self, icao24: str) -> list[tuple[float, float]]:
        """Return position history for an aircraft as [(lng, lat), ...]."""
        return list(self._aircraft_tracks.get(icao24, []))

    # Valid phase transitions — maps (prior_phase) → set of allowed next phases.
    # If the snapshot classification isn't in the allowed set, we pick the
    # closest valid transition based on telemetry direction.
    _VALID_TRANSITIONS: dict[str, set[str]] = {
        "ground":     {"ground", "departure", "climbing"},
        "departure":  {"departure", "climbing"},
        "climbing":   {"climbing", "cruising", "descending"},
        "cruising":   {"cruising", "descending"},
        "descending": {"descending", "approach", "climbing"},  # climbing = go-around
        "approach":   {"approach", "ground", "climbing"},       # climbing = missed approach
    }

    def _resolve_phase(self, icao24: str, snapshot_phase: str) -> str:
        """
        Resolve flight phase using the state machine.

        If the aircraft has a prior phase, enforce valid transitions.
        If it's new (first seen), accept the snapshot classification directly.
        """
        prior = self._aircraft_phase.get(icao24)

        if prior is None:
            # First seen — accept snapshot as-is
            self._aircraft_phase[icao24] = snapshot_phase
            return snapshot_phase

        allowed = self._VALID_TRANSITIONS.get(prior, set())

        if snapshot_phase in allowed:
            # Valid transition
            self._aircraft_phase[icao24] = snapshot_phase
            return snapshot_phase

        # Invalid transition — find the best intermediate state.
        # The snapshot tells us where telemetry *wants* to go;
        # we step through the closest valid state instead.
        bridge = self._bridge_phase(prior, snapshot_phase)
        self._aircraft_phase[icao24] = bridge
        return bridge

    @staticmethod
    def _bridge_phase(prior: str, target: str) -> str:
        """
        When a direct transition isn't valid, return the best
        intermediate phase that moves toward the target.
        """
        # Ground trying to jump to climbing/cruising — go through departure
        if prior == "ground" and target in ("cruising", "descending", "approach"):
            return "climbing"

        # Departure trying to jump to cruising — still climbing
        if prior == "departure" and target in ("cruising", "descending", "approach", "ground"):
            return "climbing"

        # Climbing trying to jump to approach/ground — must descend first
        if prior == "climbing" and target in ("approach", "ground"):
            return "descending"

        # Cruising trying to jump to approach/ground — must descend first
        if prior == "cruising" and target in ("approach", "ground", "climbing", "departure"):
            return "descending"

        # Descending trying to jump to ground — go through approach
        if prior == "descending" and target == "ground":
            return "approach"

        # Approach trying to jump to cruising — go-around, climb first
        if prior == "approach" and target in ("cruising", "descending", "departure"):
            return "climbing"

        # Fallback — hold current phase
        return prior

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
            asyncio.create_task(self._vessel_cleanup_loop(), name="vessel_cleanup"),
            asyncio.create_task(self._feed_status_loop(), name="feed_status"),
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
                records = await self._adsb_parser.fetch_region(REGION_BBOX)
                now = datetime.now(timezone.utc)
                self._adsb_last_msg = now
                self._adsb_error = None

                for record in records:
                    self._adsb_msg_count += 1
                    # Apply flight phase state machine
                    record.flight_phase = self._resolve_phase(
                        record.icao24, record.flight_phase,
                    )
                    self._aircraft_buffer.append(record)
                    self._aircraft_latest[record.icao24] = record
                    self._update_track(
                        self._aircraft_tracks, record.icao24,
                        record.position.lng, record.position.lat,
                    )
                    for cb in self._aircraft_callbacks:
                        cb(record)

                logger.info("ADS-B poll: %d aircraft", len(records))

                # Evict aircraft not seen in the last 10 minutes
                cutoff = now - timedelta(minutes=10)
                stale = [
                    icao for icao, rec in self._aircraft_latest.items()
                    if rec.position.timestamp < cutoff
                ]
                for icao in stale:
                    del self._aircraft_latest[icao]
                    self._aircraft_tracks.pop(icao, None)
                    self._aircraft_phase.pop(icao, None)
                if stale:
                    logger.info("Evicted %d stale aircraft (>10 min)", len(stale))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._adsb_error = str(exc)
                logger.error("ADS-B poll error: %s", exc)

            await asyncio.sleep(interval)

    # --- Vessel Cleanup Loop ---

    # Bounding box for "near edge" detection (matching AIS/ADS-B bbox)
    _BBOX_LAT_MIN, _BBOX_LAT_MAX = REGION[0], REGION[1]
    _BBOX_LNG_MIN, _BBOX_LNG_MAX = REGION[2], REGION[3]
    _EDGE_MARGIN = 0.05  # ~5.5 km — "near edge" threshold

    def _is_near_edge(self, lat: float, lng: float) -> bool:
        """Check if a position is within the edge margin of the bounding box."""
        return (
            lat < self._BBOX_LAT_MIN + self._EDGE_MARGIN
            or lat > self._BBOX_LAT_MAX - self._EDGE_MARGIN
            or lng < self._BBOX_LNG_MIN + self._EDGE_MARGIN
            or lng > self._BBOX_LNG_MAX - self._EDGE_MARGIN
        )

    async def _vessel_cleanup_loop(self) -> None:
        """Periodically evict vessels not seen in over 8 hours."""
        while self._running:
            try:
                await asyncio.sleep(300)  # Run every 5 minutes
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(hours=8)
                stale = []
                near_edge_count = 0
                for mmsi, rec in self._vessel_latest.items():
                    if rec.position.timestamp < cutoff:
                        near_edge = self._is_near_edge(
                            rec.position.lat, rec.position.lng,
                        )
                        stale.append(mmsi)
                        if near_edge:
                            near_edge_count += 1
                for mmsi in stale:
                    del self._vessel_latest[mmsi]
                    self._vessel_tracks.pop(mmsi, None)
                if stale:
                    logger.info(
                        "Evicted %d stale vessels (>8 hr), %d near edge",
                        len(stale), near_edge_count,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Vessel cleanup error: %s", exc)

    # --- Periodic Feed Status ---

    async def _feed_status_loop(self) -> None:
        """Log feed flow status every 60 seconds."""
        prev_ais = 0
        prev_adsb = 0
        while self._running:
            try:
                await asyncio.sleep(60)
                ais_delta = self._ais_msg_count - prev_ais
                adsb_delta = self._adsb_msg_count - prev_adsb
                prev_ais = self._ais_msg_count
                prev_adsb = self._adsb_msg_count

                ais_age = ""
                if self._ais_last_msg:
                    age_s = (datetime.now(timezone.utc) - self._ais_last_msg).total_seconds()
                    ais_age = f", last msg {age_s:.0f}s ago"
                else:
                    ais_age = ", no msgs yet"

                level = logging.WARNING if ais_delta == 0 else logging.INFO
                logger.log(
                    level,
                    "Feed status — AIS: %d msgs/min, %d vessels tracked%s | "
                    "ADS-B: %d msgs/min, %d aircraft tracked",
                    ais_delta, len(self._vessel_latest), ais_age,
                    adsb_delta, len(self._aircraft_latest),
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Feed status loop error: %s", exc)

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

                _ais_batch_count = 0
                async for record in self._aisstream.stream():
                    if not self._running:
                        break
                    self._ais_msg_count += 1
                    _ais_batch_count += 1
                    if _ais_batch_count % 100 == 0:
                        logger.info(
                            "AIS streaming: %d vessels tracked, %d msgs this session",
                            len(self._vessel_latest), _ais_batch_count,
                        )
                    self._vessel_buffer.append(record)
                    self._vessel_latest[record.mmsi] = record
                    self._update_track(
                        self._vessel_tracks, record.mmsi,
                        record.position.lng, record.position.lat,
                    )
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
            self._update_track(
                self._vessel_tracks, record.mmsi,
                record.position.lng, record.position.lat,
            )
            self._ais_last_msg = now
            for cb in self._vessel_callbacks:
                cb(record)

        return records
