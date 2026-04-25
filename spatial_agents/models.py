"""
Shared data models — Pydantic schemas for the full pipeline.

These models define the canonical data structures that flow through
ingest → spatial → intelligence → serving. The schemas are designed
to mirror the Swift-side @Generable struct definitions so that FM
structured output validation works across both Python and Swift.

Version History:
    0.1.0  2026-03-28  Initial model definitions
    0.2.0  2026-04-02  Added flight_phase field to AircraftRecord and
                       classify_flight_phase() function
    0.3.0  2026-04-24  Added regions dict to HealthResponse for
                       multi-region support — Claude Opus 4.6
    0.3.1  2026-04-24  CoverageResponse docstring updated to reference
                       boston instead of persian_gulf — Claude Opus 4
    0.4.0  2026-04-25  CoverageResponse adds primary_cell, buffer_cells,
                       and geometry (GeoJSON MultiPolygon of the 7-cell
                       region tile) — Claude 4.7
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

_METERS_TO_FEET = 3.28084


def classify_flight_phase(
    on_ground: bool | None = None,
    vertical_rate_fpm: float | None = None,
    altitude_meters: float | None = None,
) -> str:
    """
    Classify an aircraft's flight phase from raw telemetry.

    Returns one of: ground, departure, approach, descending, cruising, climbing.
    """
    if on_ground is True:
        return "ground"

    alt_feet = (altitude_meters or 0) * _METERS_TO_FEET
    vr = vertical_rate_fpm or 0

    if alt_feet < 200:
        return "departure" if vr > 0 else "approach"

    if vr < -300:
        return "descending"

    if alt_feet >= 18_000:
        return "cruising"

    return "climbing"


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
    flight_phase: str = Field(
        default="climbing",
        description="Classified flight phase: ground, departure, approach, descending, cruising, climbing",
    )
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


# ---------------------------------------------------------------------------
# API Response Models — typed responses for OpenAPI spec generation
# ---------------------------------------------------------------------------

class VesselWithTrack(VesselRecord):
    """Vessel record enriched with position history trail."""
    track: list[list[float]] = Field(
        default_factory=list,
        description="Position history as [[lng, lat], ...], newest last (up to 5 points)",
    )
    track_points: int = Field(default=0, description="Number of trail positions available")


class AircraftWithTrack(AircraftRecord):
    """Aircraft record enriched with position history trail."""
    track: list[list[float]] = Field(
        default_factory=list,
        description="Position history as [[lng, lat], ...], newest last (up to 5 points)",
    )
    track_points: int = Field(default=0, description="Number of trail positions available")


class VesselResponse(BaseModel):
    """Response for GET /api/vessels/{h3_cell}."""
    h3_cell: str = Field(description="Queried H3 cell index")
    resolution: int = Field(description="H3 resolution level used for the query")
    count: int = Field(description="Number of vessels in this cell")
    vessels: list[VesselWithTrack] = Field(description="Vessel records with position trails")
    timestamp: datetime = Field(description="Response generation time (UTC)")


class AircraftResponse(BaseModel):
    """Response for GET /api/aircraft/{h3_cell}."""
    h3_cell: str = Field(description="Queried H3 cell index")
    resolution: int = Field(description="H3 resolution level used for the query")
    count: int = Field(description="Number of aircraft in this cell")
    aircraft: list[AircraftWithTrack] = Field(description="Aircraft records with position trails")
    timestamp: datetime = Field(description="Response generation time (UTC)")


class IntelligenceResponse(BaseModel):
    """Response for GET /api/intelligence/{h3_cell}."""
    h3_cell: str = Field(description="Queried H3 cell index")
    resolution: int = Field(description="H3 resolution level")
    domain: str = Field(description="Intelligence domain: maritime, aviation, orbital")
    activity_summary: str = Field(description="Natural language summary of current activity")
    vessel_count: int = Field(description="Number of vessels in cell")
    aircraft_count: int = Field(description="Number of aircraft in cell")
    token_budget: TokenBudget = Field(description="Current FM token budget allocation")
    payload_tokens: int = Field(description="Tokens consumed by this payload")
    note: str = Field(description="Status note about FM evaluation")
    timestamp: datetime = Field(description="Response generation time (UTC)")


class CausalEmptyResponse(BaseModel):
    """Response for GET /api/causal/{h3_cell} when no events are detected."""
    h3_cell: str = Field(description="Queried H3 cell index")
    message: str = Field(description="Status message")
    events_checked: int = Field(description="Number of entities evaluated for events")
    timestamp: datetime = Field(description="Response generation time (UTC)")


class HealthConfigResponse(BaseModel):
    """Nested config section of the health response."""
    resolutions: list[int] = Field(description="Active H3 resolution levels")
    context_window: int = Field(description="FM context window size in tokens")


class CoverageBbox(BaseModel):
    """Rectangular bounding box defining the data collection region."""
    min_lat: float = Field(description="Southern boundary latitude")
    max_lat: float = Field(description="Northern boundary latitude")
    min_lng: float = Field(description="Western boundary longitude")
    max_lng: float = Field(description="Eastern boundary longitude")


class CoverageResponse(BaseModel):
    """Data collection coverage area — actual bounds and H3 cell index."""
    region: str = Field(
        description="Active region name (e.g. san_francisco, boston)",
    )
    bbox: CoverageBbox = Field(
        description="Rectangular region where AIS and ADS-B data is actively collected. "
                    "Use for map fitting and coverage display.",
    )
    h3_cells: dict[int, list[str]] = Field(
        description="Minimal set of H3 cells to query for data within the bbox. "
                    "At coarse resolutions cells extend beyond the bbox — "
                    "use bbox for display bounds, cells for API queries. "
                    "Format: {resolution: [cell_ids]}",
    )
    primary_cell: str = Field(
        default="",
        description="Primary res-4 H3 cell anchoring this region (region center).",
    )
    buffer_cells: list[str] = Field(
        default_factory=list,
        description="Six res-4 H3 neighbor cells surrounding the primary cell. "
                    "Together with primary_cell they form the 7-cell region tile.",
    )
    geometry: dict = Field(
        default_factory=dict,
        description="GeoJSON MultiPolygon (one polygon per cell, primary first) "
                    "covering the 7-cell region tile. Suitable for map rendering.",
    )
    advisories: list[str] = Field(
        default_factory=list,
        description="Data quality advisories for the active region. "
                    "Display these to users when non-empty.",
    )


class HealthResponse(BaseModel):
    """Response for GET /health."""
    status: str = Field(description="Overall status: ok, degraded, error, initializing")
    mode: str = Field(description="Deployment mode: local_mac or cloud")
    uptime_seconds: float = Field(description="Server uptime in seconds")
    port: int = Field(description="Server port")
    feeds: list[FeedStatus] = Field(description="Per-feed health status")
    config: HealthConfigResponse = Field(description="Active configuration summary")
    coverage: CoverageResponse = Field(description="Active data collection area (first region, backward compat)")
    regions: dict[str, CoverageResponse] = Field(
        default_factory=dict,
        description="Per-region coverage info keyed by region name",
    )


class FeedHealthResponse(BaseModel):
    """Response for GET /health/feeds."""
    feeds: list[FeedStatus] = Field(description="Detailed per-feed health status")


class CellCenter(BaseModel):
    """Geographic center of an H3 cell."""
    lat: float = Field(description="Latitude")
    lng: float = Field(description="Longitude")


class TileInfoResponse(BaseModel):
    """Response for GET /api/tiles/info/{h3_cell}."""
    cell_id: str = Field(description="H3 cell index")
    resolution: int = Field(description="H3 resolution level")
    center: CellCenter = Field(description="Cell center coordinates")
    boundary: dict[str, Any] = Field(description="GeoJSON polygon of cell boundary")
    edge_length_km: float = Field(description="Approximate edge length in kilometers")
    neighbors: list[str] = Field(description="Adjacent H3 cell IDs")


class BboxResponse(BaseModel):
    """Response for GET /api/tiles/bbox."""
    bbox: list[float] = Field(description="Bounding box [min_lat, min_lng, max_lat, max_lng]")
    resolution: int = Field(description="H3 resolution level")
    cell_count: int = Field(description="Number of cells covering the bbox")
    cells: list[str] = Field(description="Sorted list of H3 cell IDs")


class PositionCellsResponse(BaseModel):
    """Response for GET /api/tiles/position."""
    lat: float = Field(description="Queried latitude")
    lng: float = Field(description="Queried longitude")
    cells: dict[int, str] = Field(description="H3 cell IDs at each resolution {res: cell_id}")


class TileStatsResponse(BaseModel):
    """Response for GET /api/tiles/stats."""
    total: int = Field(description="Total number of tile files on disk")
    total_size_mb: float = Field(default=0, description="Total size of all tiles in MB")
    avg_size_kb: float = Field(default=0, description="Average tile file size in KB")
