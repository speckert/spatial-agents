"""
Sample Data — AIS NMEA sentences and ADS-B state vectors for testing.

Provides realistic test fixtures for offline pipeline demonstration
without requiring live data feed connections. Vessels and aircraft
are positioned in the San Francisco Bay Area.

Version History:
    0.1.0  2026-03-28  Initial sample data — SF Bay vessels, Oakland/SFO aircraft
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from spatial_agents.models import (
    AircraftCategory,
    AircraftRecord,
    GeoPosition,
    VesselRecord,
    VesselTrack,
    VesselType,
)
from spatial_agents.spatial.h3_indexer import H3Indexer

_indexer = H3Indexer()
_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Sample AIS NMEA sentences (real format, synthetic positions)
# ---------------------------------------------------------------------------

SAMPLE_NMEA_LINES: list[str] = [
    # Class A position reports (type 1) — Oakland outer harbor area
    "!AIVDM,1,1,,B,13u@Dt002s000000000000000000,0*25",
    "!AIVDM,1,1,,B,15MgK90P00G?tl0E6JL002vP0000,0*3C",
    "!AIVDM,1,1,,A,15N4cJ`005Jrek0H@9n`DW5608EP,0*13",
    # Class B position reports (type 18)
    "!AIVDM,1,1,,B,B5MtL4@016J4ip01oF06RwT40000,0*0A",
]


# ---------------------------------------------------------------------------
# Sample Vessel Records — SF Bay
# ---------------------------------------------------------------------------

def _vessel(
    mmsi: str,
    name: str,
    vtype: VesselType,
    lat: float,
    lng: float,
    speed: float,
    heading: float,
    dest: str = "",
    time_offset_min: int = 0,
) -> VesselRecord:
    ts = _NOW - timedelta(minutes=time_offset_min)
    cells = _indexer.position_to_cells(lat, lng)
    return VesselRecord(
        mmsi=mmsi,
        name=name,
        vessel_type=vtype,
        position=GeoPosition(lat=lat, lng=lng, timestamp=ts),
        heading_deg=heading,
        speed_knots=speed,
        course_deg=heading + 2,
        destination=dest,
        h3_cells=cells,
    )


SAMPLE_VESSELS: list[VesselRecord] = [
    # Cargo vessels — Oakland container terminal
    _vessel("367000001", "PACIFIC TRADER", VesselType.CARGO,
            37.7955, -122.2790, 0.2, 135.0, "OAKLAND", 5),
    _vessel("367000002", "SEALAND EXPRESS", VesselType.CARGO,
            37.7980, -122.2850, 0.1, 180.0, "OAKLAND", 3),
    _vessel("367000003", "MAERSK OAKLAND", VesselType.CARGO,
            37.8100, -122.3200, 8.5, 45.0, "OAKLAND", 1),

    # Tanker — Richmond
    _vessel("367000010", "CHEVRON RICHMOND", VesselType.TANKER,
            37.9100, -122.3800, 0.0, 270.0, "RICHMOND", 10),

    # Tugs — assisting cargo
    _vessel("367000020", "PACIFIC VALOR", VesselType.TUG,
            37.7960, -122.2800, 3.2, 140.0, "", 2),
    _vessel("367000021", "BAY GUARDIAN", VesselType.TUG,
            37.7985, -122.2830, 2.8, 160.0, "", 4),

    # Passenger — SF ferry
    _vessel("367000030", "GOLDEN GATE", VesselType.PASSENGER,
            37.8070, -122.4180, 22.0, 0.0, "SAUSALITO", 1),

    # Sailing vessels — central bay
    _vessel("367000040", "WINDCHASER", VesselType.SAILING,
            37.8200, -122.3800, 5.5, 310.0, "", 8),
    _vessel("367000041", "BLUE HORIZON", VesselType.SAILING,
            37.8150, -122.3750, 6.2, 290.0, "", 6),
    _vessel("367000042", "SPIRIT WIND", VesselType.SAILING,
            37.8250, -122.3900, 4.8, 270.0, "", 12),

    # Fishing vessel — near Alcatraz
    _vessel("367000050", "LUCKY STAR", VesselType.FISHING,
            37.8270, -122.4230, 3.0, 200.0, "", 15),

    # Loitering vessel — suspicious slow mover near anchorage
    _vessel("367000060", "UNKNOWN BULK", VesselType.CARGO,
            37.8400, -122.3500, 0.8, 90.0, "", 30),
    _vessel("367000060", "UNKNOWN BULK", VesselType.CARGO,
            37.8410, -122.3510, 0.5, 95.0, "", 20),
    _vessel("367000060", "UNKNOWN BULK", VesselType.CARGO,
            37.8405, -122.3505, 0.6, 88.0, "", 10),
    _vessel("367000060", "UNKNOWN BULK", VesselType.CARGO,
            37.8408, -122.3508, 0.4, 92.0, "", 2),
]


# ---------------------------------------------------------------------------
# Sample Vessel Track (with dark gap) for anomaly detection
# ---------------------------------------------------------------------------

def _build_dark_gap_track() -> VesselTrack:
    """Vessel that goes silent for 45 minutes mid-transit."""
    positions = []
    base_lat, base_lng = 37.80, -122.40

    # Normal positions every 2 minutes for 20 minutes
    for i in range(10):
        ts = _NOW - timedelta(minutes=80 - i * 2)
        positions.append(GeoPosition(
            lat=base_lat + i * 0.002,
            lng=base_lng + i * 0.001,
            timestamp=ts,
        ))

    # 45-minute gap (no positions)

    # Resumes 45 minutes later
    for i in range(5):
        ts = _NOW - timedelta(minutes=15 - i * 3)
        positions.append(GeoPosition(
            lat=base_lat + 0.05 + i * 0.002,
            lng=base_lng + 0.03 + i * 0.001,
            timestamp=ts,
        ))

    return VesselTrack(
        mmsi="367000070",
        name="DARK RUNNER",
        vessel_type=VesselType.TANKER,
        positions=positions,
    )


SAMPLE_DARK_GAP_TRACK = _build_dark_gap_track()


# ---------------------------------------------------------------------------
# Sample Aircraft Records — SFO / Oakland approaches
# ---------------------------------------------------------------------------

def _aircraft(
    icao24: str,
    callsign: str,
    cat: AircraftCategory,
    lat: float,
    lng: float,
    alt: float,
    speed: float,
    heading: float,
    vrate: float = 0.0,
    on_ground: bool = False,
    time_offset_min: int = 0,
) -> AircraftRecord:
    ts = _NOW - timedelta(minutes=time_offset_min)
    cells = _indexer.position_to_cells(lat, lng)
    return AircraftRecord(
        icao24=icao24,
        callsign=callsign,
        category=cat,
        position=GeoPosition(lat=lat, lng=lng, alt_m=alt, timestamp=ts),
        velocity_knots=speed,
        vertical_rate_fpm=vrate,
        heading_deg=heading,
        on_ground=on_ground,
        h3_cells=cells,
    )


SAMPLE_AIRCRAFT: list[AircraftRecord] = [
    # Arrivals — SFO ILS 28R/L
    _aircraft("A00001", "UAL452", AircraftCategory.HEAVY,
              37.6500, -122.2000, 2500.0, 160.0, 280.0, -800.0, False, 1),
    _aircraft("A00002", "SWA1823", AircraftCategory.MEDIUM,
              37.6700, -122.1500, 4000.0, 180.0, 275.0, -1200.0, False, 2),
    _aircraft("A00003", "AAL298", AircraftCategory.HEAVY,
              37.6900, -122.1000, 6000.0, 200.0, 270.0, -600.0, False, 3),

    # Departures — SFO
    _aircraft("A00010", "DAL1540", AircraftCategory.HEAVY,
              37.6300, -122.4200, 3000.0, 250.0, 280.0, 2500.0, False, 1),
    _aircraft("A00011", "UAL789", AircraftCategory.MEDIUM,
              37.6250, -122.4500, 8000.0, 280.0, 310.0, 1800.0, False, 2),

    # Oakland OAK arrivals
    _aircraft("A00020", "SWA455", AircraftCategory.MEDIUM,
              37.7300, -122.1800, 3500.0, 170.0, 295.0, -900.0, False, 1),

    # On ground — SFO
    _aircraft("A00030", "UAL100", AircraftCategory.HEAVY,
              37.6213, -122.3790, 0.0, 0.0, 280.0, 0.0, True, 5),
    _aircraft("A00031", "SWA200", AircraftCategory.MEDIUM,
              37.6215, -122.3795, 0.0, 0.0, 280.0, 0.0, True, 5),
    _aircraft("A00032", "AAL300", AircraftCategory.HEAVY,
              37.6218, -122.3800, 0.0, 0.0, 280.0, 0.0, True, 5),

    # GA traffic — Palo Alto
    _aircraft("A00040", "N12345", AircraftCategory.LIGHT,
              37.4600, -122.1200, 2000.0, 100.0, 180.0, 0.0, False, 10),

    # Helicopter — SF downtown
    _aircraft("A00050", "N98765", AircraftCategory.ROTORCRAFT,
              37.7850, -122.4000, 500.0, 60.0, 45.0, 0.0, False, 3),
]


# ---------------------------------------------------------------------------
# Convenience: get all sample data loaded into a feed manager
# ---------------------------------------------------------------------------

def load_sample_data():
    """
    Load all sample data into a FeedManager for demo/testing.

    Returns a FeedManager with vessels and aircraft pre-loaded.

    Usage:
        from spatial_agents.data.samples import load_sample_data
        manager = load_sample_data()
        vessels = manager.get_latest_vessels()
    """
    from spatial_agents.ingest.feed_manager import FeedManager

    manager = FeedManager()

    # Load vessels directly into the manager's state
    for v in SAMPLE_VESSELS:
        manager._vessel_latest[v.mmsi] = v
        manager._vessel_buffer.append(v)
        manager._ais_msg_count += 1

    # Load aircraft
    for a in SAMPLE_AIRCRAFT:
        manager._aircraft_latest[a.icao24] = a
        manager._aircraft_buffer.append(a)
        manager._adsb_msg_count += 1

    manager._ais_last_msg = _NOW
    manager._adsb_last_msg = _NOW

    return manager
