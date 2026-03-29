"""
Shared data models — Pydantic schemas for the full pipeline.

These models define the canonical data structures that flow through
ingest → spatial → intelligence → serving. The schemas are designed
to mirror the Swift-side @Generable struct definitions so that FM
structured output validation works across both Python and Swift.

Version History:
    0.1.0  2026-03-28  Initial model definitions
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class VesselType(str, Enum):
    """AIS vessel type classification."""
    CARGO = "cargo"
    TANKER = "tanker"
    PASSENGER = "passenger"
    FISHING = "fishing"
    TUG = "tug"
    MILITARY = "military"
    SAILING = "sailing"
    PLEASURE = "pleasure"
    HIGH_SPEED = "high_speed"
    OTHER = "other"
    UNKNOWN = "unknown"


class AircraftCategory(str, Enum):
    """ADS-B emitter category."""
    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"
    HIGH_PERFORMANCE = "high_performance"
    ROTORCRAFT = "rotorcraft"
    GLIDER = "glider"
    UAV = "uav"
    SPACE = "space"
    GROUND_VEHICLE = "ground_vehicle"
    UNKNOWN = "unknown"


class DataDomain(str, Enum):
    """Intelligence domain for prompt routing."""
    MARITIME = "maritime"
    AVIATION = "aviation"
    ORBITAL = "orbital"


# ---------------------------------------------------------------------------
# Core Position
# ---------------------------------------------------------------------------

class GeoPosition(BaseModel):
    """WGS-84 geographic position with optional altitude."""
    lat: float = Field(ge=-90, le=90, description="Latitude in decimal degrees")
    lng: float = Field(ge=-180, le=180, description="Longitude in decimal degrees")
    alt_m: float | None = Field(default=None, description="Altitude in meters (MSL)")
    timestamp: datetime = Field(description="Position fix time (UTC)")


# ---------------------------------------------------------------------------
# Vessel (AIS)
# ---------------------------------------------------------------------------

class VesselRecord(BaseModel):
    """Parsed AIS vessel record — single position report."""
    mmsi: str = Field(description="Maritime Mobile Service Identity (9 digits)")
    name: str = Field(default="", description="Vessel name from AIS static data")
    vessel_type: VesselType = Field(default=VesselType.UNKNOWN)
    position: GeoPosition
    heading_deg: float | None = Field(default=None, ge=0, lt=360)
    speed_knots: float | None = Field(default=None, ge=0)
    course_deg: float | None = Field(default=None, ge=0, lt=360)
    destination: str = Field(default="", description="Reported destination")
    h3_cells: dict[int, str] = Field(
        default_factory=dict,
        description="H3 cell IDs at each resolution {res: cell_id}",
    )


class VesselTrack(BaseModel):
    """Time-ordered sequence of positions for a single vessel."""
    mmsi: str
    name: str = ""
    vessel_type: VesselType = VesselType.UNKNOWN
    positions: list[GeoPosition] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Aircraft (ADS-B)
# ---------------------------------------------------------------------------

class AircraftRecord(BaseModel):
    """Parsed ADS-B aircraft record — single state vector."""
    icao24: str = Field(description="ICAO 24-bit transponder address (hex)")
    callsign: str = Field(default="", description="Flight callsign")
    category: AircraftCategory = Field(default=AircraftCategory.UNKNOWN)
    position: GeoPosition
    velocity_knots: float | None = Field(default=None, ge=0)
    vertical_rate_fpm: float | None = Field(default=None, description="Feet per minute")
    heading_deg: float | None = Field(default=None, ge=0, lt=360)
    on_ground: bool = Field(default=False)
    squawk: str = Field(default="", description="Transponder squawk code")
    h3_cells: dict[int, str] = Field(
        default_factory=dict,
        description="H3 cell IDs at each resolution {res: cell_id}",
    )


# ---------------------------------------------------------------------------
# H3 Tile
# ---------------------------------------------------------------------------

class TileMetadata(BaseModel):
    """Metadata for a generated H3 tile."""
    cell_id: str = Field(description="H3 cell index")
    resolution: int = Field(ge=0, le=15)
    temporal_bin: str = Field(description="Time window: 1min, 5min, 1hour, 1day, live")
    generated_at: datetime
    vessel_count: int = Field(default=0)
    aircraft_count: int = Field(default=0)
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="Bounding box (min_lat, min_lng, max_lat, max_lng)"
    )


class TileContent(BaseModel):
    """Full tile payload — metadata plus entity records."""
    metadata: TileMetadata
    vessels: list[VesselRecord] = Field(default_factory=list)
    aircraft: list[AircraftRecord] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Intelligence — FM Input/Output
# ---------------------------------------------------------------------------

class IntelligenceRequest(BaseModel):
    """Payload sent to the FM for situation analysis."""
    domain: DataDomain
    h3_cell: str
    resolution: int
    vessel_summary: dict[str, Any] | None = None
    aircraft_summary: dict[str, Any] | None = None
    causal_graph: dict[str, Any] | None = None
    timestamp: datetime


class SituationReport(BaseModel):
    """
    Structured FM output — mirrors Swift @Generable SituationReport.

    This schema is used for both Python-side validation and as the
    reference for the Swift-side guided generation definition.
    """
    domain: DataDomain
    h3_cell: str
    summary: str = Field(description="One-paragraph natural language situation summary")
    key_observations: list[str] = Field(
        description="Top 3-5 notable observations",
        max_length=5,
    )
    anomalies: list[str] = Field(
        default_factory=list,
        description="Detected anomalies or unusual patterns",
    )
    causal_narrative: str | None = Field(
        default=None,
        description="FM-generated explanation of causal relationships",
    )
    confidence: float = Field(
        ge=0, le=1,
        description="Model confidence in the analysis",
    )
    generated_at: datetime


# ---------------------------------------------------------------------------
# Causal Graph
# ---------------------------------------------------------------------------

class CausalNode(BaseModel):
    """Node in a structural causal model DAG."""
    id: str
    label: str
    domain: DataDomain
    event_type: str = Field(description="e.g. vessel_loitering, flight_diversion, weather_event")
    observed_value: float | None = None
    timestamp: datetime | None = None


class CausalEdge(BaseModel):
    """Directed edge in a structural causal model."""
    source: str = Field(description="Source node ID")
    target: str = Field(description="Target node ID")
    strength: float = Field(ge=0, le=1, description="Estimated causal strength")
    mechanism: str = Field(default="", description="Description of causal mechanism")


class CausalGraph(BaseModel):
    """Serialized structural causal model for client consumption."""
    h3_cell: str
    nodes: list[CausalNode] = Field(default_factory=list)
    edges: list[CausalEdge] = Field(default_factory=list)
    interventions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Results of do-calculus intervention queries",
    )
    generated_at: datetime


# ---------------------------------------------------------------------------
# Token Budget
# ---------------------------------------------------------------------------

class TokenBudget(BaseModel):
    """Token allocation breakdown for FM context window management."""
    context_window_size: int = Field(description="Total available tokens")
    instructions_tokens: int = Field(description="System prompt cost")
    tool_schema_tokens: int = Field(description="Tool definition serialization cost")
    data_payload_tokens: int = Field(description="Input data cost")
    remaining_tokens: int = Field(description="Available for model response")
    utilization_pct: float = Field(description="Percentage of context window used")


# ---------------------------------------------------------------------------
# Health / Status
# ---------------------------------------------------------------------------

class FeedStatus(BaseModel):
    """Health status for a single data feed."""
    name: str
    connected: bool
    last_message_at: datetime | None = None
    messages_per_minute: float = 0.0
    error: str | None = None


class PipelineHealth(BaseModel):
    """Overall pipeline health for the /health endpoint."""
    status: str = Field(description="ok, degraded, or error")
    uptime_seconds: float
    feeds: list[FeedStatus] = Field(default_factory=list)
    tile_count: int = Field(default=0, description="Total tiles on disk")
    oldest_tile_age_seconds: float | None = None
    newest_tile_age_seconds: float | None = None
