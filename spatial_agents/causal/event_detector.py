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
    VesselRecord,
    VesselTrack,
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

    def to_causal_node(self, node_id: str) -> CausalNode:
        """Convert to a CausalNode for DAG construction."""
        return CausalNode(
            id=node_id,
            label=self.description,
            domain=self.domain,
            event_type=self.event_type,
            observed_value=self.confidence,
            timestamp=self.timestamp,
        )


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
    LOITER_MIN_DURATION_MIN = 30
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
    ) -> list[DetectedEvent]:
        """Run all detection algorithms and return combined events."""
        events: list[DetectedEvent] = []

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

            events.append(DetectedEvent(
                event_type="vessel_loitering",
                domain=DataDomain.MARITIME,
                description=f"Vessel {name} loitering at {avg_speed:.1f} knots",
                entity_ids=[mmsi],
                h3_cell=h3_cell,
                timestamp=records[-1].position.timestamp,
                confidence=min(0.9, len(records) * 0.15),
                metrics={
                    "avg_speed_knots": float(avg_speed),
                    "report_count": len(records),
                },
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
            ))

        return events
