"""
Event Detector — Pattern detection across geospatial data streams.

Identifies behavioral patterns and anomalies from vessel and aircraft
records that become nodes in the structural causal model:
    - Vessel loitering (low speed, small area, extended duration)
    - Dark vessel gaps (AIS transmission interruptions)
    - Route deviations (significant departure from expected track)
    - Flight diversions (aircraft deviating from filed route)
    - Correlated anomalies (co-occurring events across data streams)
    - Density anomalies (unusual concentration or absence)

Version History:
    0.1.0  2026-03-28  Initial event detector
    0.2.0  2026-04-25  Geographic positioning on detected events,
                       new detect_weather_events / detect_tfr_events
                       to lift NWS alerts and FAA TFRs into the
                       causal graph as exogenous causes — Claude 4.7
    0.2.1  2026-04-25  TEMP: relaxed LOITER_MIN_DURATION_MIN 30→3 to
                       exercise the live causal DAG against Chicago
                       weather. Restore before production — Claude 4.7
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

import numpy as np

from spatial_agents.models import (
    AircraftRecord,
    CausalNode,
    DataDomain,
    TFR,
    VesselRecord,
    VesselTrack,
    WeatherAlert,
)

logger = logging.getLogger(__name__)


@dataclass
class DetectedEvent:
    """A detected behavioral event that becomes a causal graph node."""
    event_type: str
    domain: DataDomain
    description: str
    entity_ids: list[str]      # MMSIs or ICAO24s involved
    h3_cell: str
    timestamp: datetime
    confidence: float          # 0-1
    metrics: dict[str, float]  # Supporting quantitative data
    lat: float | None = None   # Geographic position for map-layer rendering
    lng: float | None = None

    def to_causal_node(self, node_id: str) -> CausalNode:
        """Convert to a CausalNode for DAG construction."""
        return CausalNode(
            id=node_id,
            label=self.description,
            domain=self.domain,
            event_type=self.event_type,
            observed_value=self.confidence,
            timestamp=self.timestamp,
            lat=self.lat,
            lng=self.lng,
        )


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _polygon_centroid(geometry: dict | None) -> tuple[float, float] | None:
    """Compute (lat, lng) centroid of a GeoJSON Polygon/MultiPolygon outer ring.

    For map placement of causal nodes derived from weather alerts and TFRs.
    Returns the simple mean of the outer-ring vertices — close enough for
    rendering a single point on the map.
    """
    if not geometry:
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        ring = coords[0] if coords else []
    elif gtype == "MultiPolygon":
        ring = coords[0][0] if coords and coords[0] else []
    else:
        return None
    if not ring:
        return None
    lat_sum = sum(pt[1] for pt in ring)
    lng_sum = sum(pt[0] for pt in ring)
    n = len(ring)
    return (lat_sum / n, lng_sum / n)


# Severity → confidence for weather_alert events.
_WEATHER_SEVERITY_CONF = {
    "extreme": 0.95,
    "severe":  0.85,
    "moderate": 0.65,
    "minor":   0.45,
    "unknown": 0.30,
}

# TFR LEGAL category → confidence for tfr_active events.
_TFR_TYPE_CONF = {
    "SECURITY":             0.85,
    "VIP":                  0.85,
    "HAZARDS":              0.80,
    "FIRE":                 0.85,
    "SPACE OPERATIONS":     0.90,
    "AIR SHOWS/SPORTS":     0.70,
    "UAS PUBLIC GATHERING": 0.60,
    "SPECIAL":              0.60,
}


class EventDetector:
    """
    Detect behavioral patterns and anomalies in geospatial data.

    Usage:
        detector = EventDetector()
        events = detector.detect_all(
            vessels=vessel_records,
            aircraft=aircraft_records,
            h3_cell="842831dffffffff",
        )
        for event in events:
            print(event.event_type, event.confidence)
    """

    # Thresholds (configurable)
    LOITER_SPEED_KNOTS = 2.0
    LOITER_MIN_DURATION_MIN = 3  # TEMP: relaxed 30→3 to exercise the
                                 # causal DAG against live Chicago weather.
                                 # Restore to 30 before production.
    DARK_GAP_MIN_MINUTES = 15
    ROUTE_DEVIATION_NM = 5.0
    DENSITY_ZSCORE_THRESHOLD = 2.0

    def __init__(self) -> None:
        self._event_count = 0

    @property
    def stats(self) -> dict[str, int]:
        return {"events_detected": self._event_count}

    def detect_all(
        self,
        vessels: list[VesselRecord],
        aircraft: list[AircraftRecord],
        h3_cell: str,
        tracks: list[VesselTrack] | None = None,
        alerts: list[WeatherAlert] | None = None,
        tfrs: list[TFR] | None = None,
    ) -> list[DetectedEvent]:
        """Run all detection algorithms and return combined events."""
        events: list[DetectedEvent] = []

        # Exogenous causes (weather + airspace) — added first so the
        # DAG builder can attach them as roots of downstream chains.
        if alerts:
            events.extend(self.detect_weather_events(alerts, h3_cell))
        if tfrs:
            events.extend(self.detect_tfr_events(tfrs, h3_cell))

        # Vessel-based detections
        events.extend(self.detect_loitering(vessels, h3_cell))
        events.extend(self.detect_density_anomaly(vessels, h3_cell, domain=DataDomain.MARITIME))

        # Track-based detections (need temporal history)
        if tracks:
            events.extend(self.detect_dark_gaps(tracks, h3_cell))

        # Aircraft-based detections
        events.extend(self.detect_density_anomaly(aircraft, h3_cell, domain=DataDomain.AVIATION))
        events.extend(self.detect_ground_stops(aircraft, h3_cell))

        self._event_count += len(events)
        return events

    def detect_loitering(
        self,
        vessels: list[VesselRecord],
        h3_cell: str,
    ) -> list[DetectedEvent]:
        """
        Detect vessels with low speed in a confined area.

        A vessel is loitering if speed < threshold and it has been
        in the same H3 cell for an extended period.
        """
        events: list[DetectedEvent] = []

        slow_vessels = [
            v for v in vessels
            if v.speed_knots is not None and v.speed_knots < self.LOITER_SPEED_KNOTS
            and v.speed_knots >= 0  # Exclude exactly 0 (likely at berth)
        ]

        if not slow_vessels:
            return events

        # Group by vessel
        by_mmsi: dict[str, list[VesselRecord]] = {}
        for v in slow_vessels:
            by_mmsi.setdefault(v.mmsi, []).append(v)

        for mmsi, records in by_mmsi.items():
            if len(records) < 2:
                continue

            avg_speed = np.mean([r.speed_knots for r in records if r.speed_knots])
            name = records[0].name or mmsi
            last = records[-1].position

            events.append(DetectedEvent(
                event_type="vessel_loitering",
                domain=DataDomain.MARITIME,
                description=f"Vessel {name} loitering at {avg_speed:.1f} knots",
                entity_ids=[mmsi],
                h3_cell=h3_cell,
                timestamp=last.timestamp,
                confidence=min(0.9, len(records) * 0.15),
                metrics={
                    "avg_speed_knots": float(avg_speed),
                    "report_count": len(records),
                },
                lat=last.lat,
                lng=last.lng,
            ))

        return events

    def detect_dark_gaps(
        self,
        tracks: list[VesselTrack],
        h3_cell: str,
    ) -> list[DetectedEvent]:
        """
        Detect AIS transmission gaps in vessel tracks.

        A dark gap occurs when a vessel stops transmitting for longer
        than the expected interval, potentially indicating intentional
        AIS deactivation.
        """
        events: list[DetectedEvent] = []

        for track in tracks:
            if len(track.positions) < 2:
                continue

            for i in range(1, len(track.positions)):
                prev = track.positions[i - 1]
                curr = track.positions[i]
                gap = (curr.timestamp - prev.timestamp).total_seconds() / 60

                if gap >= self.DARK_GAP_MIN_MINUTES:
                    name = track.name or track.mmsi
                    events.append(DetectedEvent(
                        event_type="dark_vessel_gap",
                        domain=DataDomain.MARITIME,
                        description=f"Vessel {name} silent for {gap:.0f} minutes",
                        entity_ids=[track.mmsi],
                        h3_cell=h3_cell,
                        timestamp=curr.timestamp,
                        confidence=min(0.95, gap / 120),  # Higher confidence for longer gaps
                        metrics={
                            "gap_minutes": gap,
                            "last_known_lat": prev.lat,
                            "last_known_lng": prev.lng,
                        },
                        lat=prev.lat,
                        lng=prev.lng,
                    ))

        return events

    def detect_density_anomaly(
        self,
        records: Sequence[VesselRecord] | Sequence[AircraftRecord],
        h3_cell: str,
        domain: DataDomain = DataDomain.MARITIME,
        historical_mean: float | None = None,
        historical_std: float | None = None,
    ) -> list[DetectedEvent]:
        """
        Detect unusual entity density (too many or too few).

        Without historical data, uses simple heuristics.
        With historical data, computes z-scores.
        """
        events: list[DetectedEvent] = []
        count = len(records)

        if historical_mean is not None and historical_std is not None and historical_std > 0:
            zscore = (count - historical_mean) / historical_std

            if abs(zscore) >= self.DENSITY_ZSCORE_THRESHOLD:
                direction = "high" if zscore > 0 else "low"
                entity_type = "vessel" if domain == DataDomain.MARITIME else "aircraft"

                events.append(DetectedEvent(
                    event_type=f"density_anomaly_{direction}",
                    domain=domain,
                    description=(
                        f"Unusually {direction} {entity_type} density: "
                        f"{count} vs expected {historical_mean:.0f} (z={zscore:.1f})"
                    ),
                    entity_ids=[],
                    h3_cell=h3_cell,
                    timestamp=datetime.now(timezone.utc),
                    confidence=min(0.95, abs(zscore) / 4),
                    metrics={
                        "count": count,
                        "historical_mean": historical_mean,
                        "zscore": float(zscore),
                    },
                ))

        return events

    def detect_ground_stops(
        self,
        aircraft: list[AircraftRecord],
        h3_cell: str,
    ) -> list[DetectedEvent]:
        """Detect unusual number of aircraft on the ground (potential ground stop)."""
        events: list[DetectedEvent] = []

        grounded = [a for a in aircraft if a.on_ground]
        airborne = [a for a in aircraft if not a.on_ground]

        # Simple heuristic: if >70% of aircraft are grounded, flag it
        if len(aircraft) >= 5 and len(grounded) / len(aircraft) > 0.7:
            # Centroid of grounded aircraft for map placement
            avg_lat = float(np.mean([a.position.lat for a in grounded])) if grounded else None
            avg_lng = float(np.mean([a.position.lng for a in grounded])) if grounded else None
            events.append(DetectedEvent(
                event_type="ground_stop_indicator",
                domain=DataDomain.AVIATION,
                description=(
                    f"Possible ground stop: {len(grounded)}/{len(aircraft)} "
                    f"aircraft on ground"
                ),
                entity_ids=[a.icao24 for a in grounded[:10]],
                h3_cell=h3_cell,
                timestamp=datetime.now(timezone.utc),
                confidence=len(grounded) / len(aircraft),
                metrics={
                    "grounded_count": len(grounded),
                    "airborne_count": len(airborne),
                    "ground_pct": len(grounded) / len(aircraft),
                },
                lat=avg_lat,
                lng=avg_lng,
            ))

        return events

    def detect_weather_events(
        self,
        alerts: list[WeatherAlert],
        h3_cell: str,
    ) -> list[DetectedEvent]:
        """Lift NWS active alerts into the causal graph as exogenous causes.

        One DetectedEvent per alert with a usable polygon. The alert
        polygon's centroid becomes the map position; severity drives
        confidence.
        """
        events: list[DetectedEvent] = []
        for alert in alerts:
            ll = _polygon_centroid(alert.polygon)
            if ll is None:
                continue
            sev = (
                alert.severity.value
                if hasattr(alert.severity, "value") else str(alert.severity)
            )
            confidence = _WEATHER_SEVERITY_CONF.get(sev.lower(), 0.30)
            label = f"{alert.event}"
            if alert.headline:
                label = f"{alert.event}: {alert.headline[:80]}"
            events.append(DetectedEvent(
                event_type="weather_alert",
                domain=DataDomain.WEATHER,
                description=label,
                entity_ids=[alert.id],
                h3_cell=h3_cell,
                timestamp=alert.effective_at or datetime.now(timezone.utc),
                confidence=confidence,
                metrics={"severity": confidence},
                lat=ll[0],
                lng=ll[1],
            ))
        return events

    def detect_tfr_events(
        self,
        tfrs: list[TFR],
        h3_cell: str,
    ) -> list[DetectedEvent]:
        """Lift FAA active TFRs into the causal graph as exogenous causes.

        One DetectedEvent per TFR with a usable polygon. The TFR
        polygon's centroid becomes the map position; the FAA category
        (LEGAL field) drives confidence.
        """
        events: list[DetectedEvent] = []
        for tfr in tfrs:
            ll = _polygon_centroid(tfr.polygon)
            if ll is None:
                continue
            confidence = _TFR_TYPE_CONF.get(tfr.type, 0.50)
            label = f"{tfr.type or 'TFR'}"
            if tfr.title:
                label = f"{label}: {tfr.title[:80]}"
            events.append(DetectedEvent(
                event_type="tfr_active",
                domain=DataDomain.AIRSPACE,
                description=label,
                entity_ids=[tfr.notam_id],
                h3_cell=h3_cell,
                timestamp=tfr.last_modified or datetime.now(timezone.utc),
                confidence=confidence,
                metrics={"category_conf": confidence},
                lat=ll[0],
                lng=ll[1],
            ))
        return events
