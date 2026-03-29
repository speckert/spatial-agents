"""
Ingest — Feed parsers and data acquisition.

Handles real-time connection to AIS and ADS-B data sources,
parsing raw messages into validated VesselRecord and AircraftRecord models.

Version History:
    0.1.0  2026-03-28  Initial ingest package with AIS, ADS-B, and TLE parsers
"""

from spatial_agents.ingest.ais_parser import AISParser
from spatial_agents.ingest.adsb_parser import ADSBParser
from spatial_agents.ingest.feed_manager import FeedManager

__all__ = ["AISParser", "ADSBParser", "FeedManager"]
