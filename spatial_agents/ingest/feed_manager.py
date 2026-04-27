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
    0.6.0  2026-04-24  Multi-region ADS-B polling (alternating regions),
                       removed dead near-edge code — Claude Opus 4.6
    0.7.0  2026-04-25  Added NWS active-alerts poll loop (5-min cadence),
                       weather feed surfaced as a third FeedStatus and
                       cached alert list exposed via get_latest_alerts()
                       — Claude 4.7
    0.8.0  2026-04-25  Added FAA TFR poll loop (15-min cadence),
                       tfr feed surfaced as a fourth FeedStatus and
                       cached TFR list exposed via get_latest_tfrs()
                       — Claude 4.7
    0.9.0  2026-04-25  ADS-B startup warmup — poll each active region
                       once at 5s spacing before settling into the 45s
                       alternating cadence, so no region sits empty
                       for the first 45s after launch — Claude 4.7
    0.10.0 2026-04-25  get_recent_vessels(within_minutes) accessor over
                       the rolling buffer, for detectors that need
                       multiple observations per vessel (loitering,
                       dark-gap) — Claude 4.7
    0.11.0 2026-04-26  handle_region_swap() — async callback registered
                       on RegionsManager. Reconnects AIS WebSocket so
                       the new region's bbox is subscribed, kicks off
                       an immediate ADS-B fetch of the new region, and
                       purges vessel/aircraft cache entries that fell
                       in the removed region's H3 cells (so legacy
                       unfiltered /vessels stops returning ghosts) —
                       Claude 4.7
    0.11.1 2026-04-26  Fix ADS-B poll loop closure bug — region_bboxes
                       was captured ONCE at loop entry, so after a swap
                       the steady-state poll kept hitting the old slot 1
                       forever (and continuously refilled the stale-cache
                       that handle_region_swap had just purged).
                       Re-resolve ACTIVE_REGIONS each iteration. Also
                       reset polled_regions on active-set change so the
                       new region gets the warmup tempo until polled
                       once — Claude 4.7
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Callable

from spatial_agents.config import ACTIVE_REGIONS, REGION_CELLS, REGIONS, config
from spatial_agents.ingest.adsb_parser import ADSBParser
from spatial_agents.ingest.ais_parser import AISParser
from spatial_agents.ingest.aisstream_client import AISStreamClient
from spatial_agents.ingest.nws_client import NWSClient
from spatial_agents.ingest.tfr_client import TFRClient
from spatial_agents.models import AircraftRecord, FeedStatus, TFR, VesselRecord, WeatherAlert

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
        nws_client: NWSClient | None = None,
        tfr_client: TFRClient | None = None,
    ) -> None:
        self._ais_parser = ais_parser or AISParser()
        self._adsb_parser = adsb_parser or ADSBParser()
        self._aisstream = aisstream_client or AISStreamClient()
        self._nws = nws_client or NWSClient()
        self._tfr = tfr_client or TFRClient()

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

        # Weather alerts cache + health
        self._weather_alerts: list[WeatherAlert] = []
        self._weather_last_fetch: datetime | None = None
        self._weather_msg_count = 0
        self._weather_error: str | None = None

        # TFR cache + health
        self._tfrs: list[TFR] = []
        self._tfr_last_fetch: datetime | None = None
        self._tfr_msg_count = 0
        self._tfr_error: str | None = None

        # Control
        self._running = False
        self._tasks: list[asyncio.Task] = []
        # Tracked separately so a region swap can cancel + restart the
        # AIS socket without taking down the whole feed manager.
        self._ais_task: asyncio.Task | None = None

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
            asyncio.create_task(self._weather_poll_loop(), name="weather_poll"),
            asyncio.create_task(self._tfr_poll_loop(), name="tfr_poll"),
        ]
        # Start AIS WebSocket if API key is configured
        if config.feeds.ais_api_key:
            self._ais_task = asyncio.create_task(
                self._ais_websocket_loop(), name="ais_ws",
            )
            self._tasks.append(self._ais_task)
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

    def get_recent_vessels(self, within_minutes: int = 10) -> list[VesselRecord]:
        """Return all vessel observations from the rolling buffer within
        the time window. Used by detectors that need repeated observations
        (e.g. loitering, dark gap) rather than just the latest snapshot.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
        return [v for v in self._vessel_buffer if v.position.timestamp >= cutoff]

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

    def get_latest_alerts(self) -> list[WeatherAlert]:
        """Return the most recently fetched NWS active alerts."""
        return list(self._weather_alerts)

    def get_weather_last_fetch(self) -> datetime | None:
        """Time of the last successful NWS fetch (UTC)."""
        return self._weather_last_fetch

    def get_latest_tfrs(self) -> list[TFR]:
        """Return the most recently fetched FAA active TFRs."""
        return list(self._tfrs)

    def get_tfr_last_fetch(self) -> datetime | None:
        """Time of the last successful FAA TFR fetch (UTC)."""
        return self._tfr_last_fetch

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
            FeedStatus(
                name="weather",
                connected=self._running and self._weather_error is None,
                last_message_at=self._weather_last_fetch,
                messages_per_minute=self._weather_msg_count / rate_window,
                error=self._weather_error,
            ),
            FeedStatus(
                name="tfr",
                connected=self._running and self._tfr_error is None,
                last_message_at=self._tfr_last_fetch,
                messages_per_minute=self._tfr_msg_count / rate_window,
                error=self._tfr_error,
            ),
        ]

    # --- ADS-B Polling Loop ---

    async def _adsb_poll_loop(self) -> None:
        """Periodically fetch ADS-B state vectors, alternating active regions.

        Startup warmup: poll each active region once at WARMUP_INTERVAL
        spacing before entering the steady-state alternating cadence.
        This avoids a cold gap where a region has zero aircraft for up
        to one full interval after launch.
        """
        interval = config.feeds.adsb_poll_interval_sec
        warmup_interval = 5  # seconds between initial per-region polls
        logger.info(
            "ADS-B poll loop started — interval: %ds, warmup: %ds, regions: %s",
            interval, warmup_interval, list(ACTIVE_REGIONS),
        )
        idx = 0
        polled_regions: set[str] = set()
        last_active: tuple[str, ...] = ()

        while self._running:
            try:
                # Re-resolve every iteration so a /regions/swap that mutates
                # ACTIVE_REGIONS takes effect on the very next poll instead of
                # waiting for a process restart. (Prior versions captured this
                # once at loop entry — that's the bug that left the poll loop
                # polling the OLD slot 1 forever after a swap, continuously
                # refilling the cache handle_region_swap had just purged.)
                region_bboxes = [(name, REGIONS[name]) for name in ACTIVE_REGIONS]
                if not region_bboxes:
                    await asyncio.sleep(warmup_interval)
                    continue
                current_active = tuple(name for name, _ in region_bboxes)
                if current_active != last_active:
                    if last_active:
                        logger.info(
                            "ADS-B poll loop active set changed: %s → %s",
                            list(last_active), list(current_active),
                        )
                    # Reset rotation + warmup so the new region gets the fast
                    # 5s warmup tempo until it has been polled at least once.
                    polled_regions = polled_regions & set(current_active)
                    idx = 0
                    last_active = current_active
                region_name, bbox = region_bboxes[idx % len(region_bboxes)]
                records = await self._adsb_parser.fetch_region(bbox)
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

                logger.info("ADS-B poll [%s]: %d aircraft", region_name, len(records))
                polled_regions.add(region_name)
                idx += 1

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

            # Warmup: short sleep until every region has been polled once;
            # then settle into the configured steady-state interval.
            in_warmup = len(polled_regions) < len(region_bboxes)
            await asyncio.sleep(warmup_interval if in_warmup else interval)

    # --- Vessel Cleanup Loop ---

    async def _vessel_cleanup_loop(self) -> None:
        """Periodically evict vessels not seen in 8+ hours."""
        while self._running:
            try:
                await asyncio.sleep(300)  # Run every 5 minutes
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(hours=8)
                evicted = [
                    mmsi for mmsi, rec in self._vessel_latest.items()
                    if rec.position.timestamp < cutoff
                ]
                for mmsi in evicted:
                    del self._vessel_latest[mmsi]
                    self._vessel_tracks.pop(mmsi, None)
                if evicted:
                    logger.info("Vessel cleanup: evicted %d stale (>8 hr)", len(evicted))
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

    # --- NWS Weather Alerts Poll Loop ---

    _WEATHER_POLL_INTERVAL_SEC = 300  # 5 minutes — alerts change slowly

    async def _weather_poll_loop(self) -> None:
        """Periodically fetch NWS active alerts intersecting active regions."""
        # Initial small delay so logs from this loop don't drown the startup banner.
        await asyncio.sleep(5)
        while self._running:
            try:
                alerts = await self._nws.fetch_active_alerts()
                self._weather_alerts = alerts
                self._weather_last_fetch = datetime.now(timezone.utc)
                self._weather_error = None
                self._weather_msg_count += len(alerts)
                logger.info("NWS poll: %d active alerts in active regions", len(alerts))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._weather_error = str(exc)
                logger.error("NWS poll error: %s", exc)

            await asyncio.sleep(self._WEATHER_POLL_INTERVAL_SEC)

    # --- FAA TFR Poll Loop ---

    _TFR_POLL_INTERVAL_SEC = 900  # 15 minutes — TFRs change slowly

    async def _tfr_poll_loop(self) -> None:
        """Periodically fetch FAA active TFRs (CONUS-wide)."""
        # Small offset so the TFR fetch doesn't fire at the same instant as
        # the weather fetch on startup.
        await asyncio.sleep(8)
        while self._running:
            try:
                tfrs = await self._tfr.fetch_active_tfrs()
                self._tfrs = tfrs
                self._tfr_last_fetch = datetime.now(timezone.utc)
                self._tfr_error = None
                self._tfr_msg_count += len(tfrs)
                logger.info("FAA TFR poll: %d active TFRs", len(tfrs))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._tfr_error = str(exc)
                logger.error("FAA TFR poll error: %s", exc)

            await asyncio.sleep(self._TFR_POLL_INTERVAL_SEC)

    # --- Region Swap Handler ---

    async def handle_region_swap(
        self,
        old_region: str | None,
        new_region: str,
    ) -> None:
        """Refresh feeds + caches after RegionsManager swaps slot 1.

        Steps (run sequentially, but quickly):

          1. Purge vessel/aircraft cache entries whose res-4 cell falls
             inside the *removed* region's 7-cell tile. Keeps legacy
             /vessels and /aircraft (no ?region=) from returning ghosts
             from a region that's no longer subscribed. Slot 0 (SF) is
             never touched since it's pinned for legacy iOS 3.1.
          2. Cancel the AIS WebSocket task and start a new one. The
             aisstream client resolves bboxes at connect time, so the
             new subscription picks up the new region automatically.
          3. Kick off an immediate ADS-B fetch of the new region so
             the cache repopulates within seconds instead of waiting
             for the next steady-state poll.

        Errors are logged but do not propagate — a swap should never
        leave the manager in a half-restarted state.
        """
        logger.info(
            "Region swap received — old: %s, new: %s; refreshing feeds + caches",
            old_region, new_region,
        )

        # 1. Purge stale entries from removed region.
        if old_region is not None:
            try:
                self._purge_region_cache(old_region)
            except Exception as exc:
                logger.error("Region swap cache purge failed: %s", exc)

        # 2. Restart AIS WebSocket so new bbox is subscribed.
        try:
            await self._restart_ais_socket()
        except Exception as exc:
            logger.error("Region swap AIS restart failed: %s", exc)

        # 3. Immediate ADS-B fetch of new region.
        try:
            await self._fetch_region_adsb_now(new_region)
        except Exception as exc:
            logger.error("Region swap immediate ADS-B fetch failed: %s", exc)

    def _purge_region_cache(self, region: str) -> None:
        """Drop vessels + aircraft whose res-4 cell falls in `region`'s tile."""
        cells = REGION_CELLS.get(region)
        if cells is None:
            logger.debug("Cache purge skipped — region %s not registered", region)
            return
        purge_set = set(cells["all"])  # type: ignore[arg-type]

        v_drop = [
            mmsi for mmsi, rec in self._vessel_latest.items()
            if rec.h3_cells.get(4) in purge_set
        ]
        for mmsi in v_drop:
            self._vessel_latest.pop(mmsi, None)
            self._vessel_tracks.pop(mmsi, None)

        a_drop = [
            icao for icao, rec in self._aircraft_latest.items()
            if rec.h3_cells.get(4) in purge_set
        ]
        for icao in a_drop:
            self._aircraft_latest.pop(icao, None)
            self._aircraft_tracks.pop(icao, None)
            self._aircraft_phase.pop(icao, None)

        logger.info(
            "Cache purge for region %s — vessels dropped: %d, aircraft dropped: %d",
            region, len(v_drop), len(a_drop),
        )

    async def _restart_ais_socket(self) -> None:
        """Cancel + relaunch the AIS WebSocket task so it reconnects.

        No-op if AIS is disabled (no API key) or if the task isn't running.
        """
        if not config.feeds.ais_api_key:
            return
        if self._ais_task is not None and not self._ais_task.done():
            self._ais_task.cancel()
            try:
                await self._ais_task
            except (asyncio.CancelledError, Exception):
                pass
            # Remove the cancelled task from the tracked list.
            try:
                self._tasks.remove(self._ais_task)
            except ValueError:
                pass

        if self._running:
            self._ais_task = asyncio.create_task(
                self._ais_websocket_loop(), name="ais_ws",
            )
            self._tasks.append(self._ais_task)
            logger.info("AIS WebSocket relaunched after region swap")

    async def _fetch_region_adsb_now(self, region: str) -> None:
        """One-shot ADS-B fetch for `region` so the cache fills immediately."""
        bbox = REGIONS.get(region)
        if bbox is None:
            logger.debug("Immediate ADS-B fetch skipped — region %s missing bbox", region)
            return

        records = await self._adsb_parser.fetch_region(bbox)
        now = datetime.now(timezone.utc)
        self._adsb_last_msg = now
        self._adsb_error = None

        for record in records:
            self._adsb_msg_count += 1
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

        logger.info(
            "Immediate ADS-B fetch [%s] after region swap: %d aircraft",
            region, len(records),
        )

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
