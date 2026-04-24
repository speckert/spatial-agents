"""
Configuration — environment-aware settings for local Mac and cloud deployments.

Version History:
    0.1.0  2026-03-28  Initial configuration structure
    0.1.1  2026-03-28  Changed default bind from 0.0.0.0 to 127.0.0.1 for
                       Apache reverse proxy deployment on Neural Magician
    0.2.0  2026-03-30  Local_mac data dir defaults to project-relative ./data,
                       ADS-B poll interval to 60s, added OpenSky auth credentials
    0.3.0  2026-03-31  Switched to OAuth2 client_id/client_secret, unified
                       AIS+ADS-B bounding box, poll interval to 45s
    0.4.0  2026-04-09  Centralized REGION bbox with named presets
                       (san_francisco, persian_gulf) — single line to switch
    0.5.0  2026-04-24  ACTIVE_REGIONS list for simultaneous multi-region
                       ingest (SF + Persian Gulf) — Claude Opus 4.6
"""

from __future__ import annotations

import os
from enum import Enum

from dotenv import load_dotenv
load_dotenv()
from pathlib import Path
from pydantic import BaseModel, Field


class DeploymentMode(str, Enum):
    """Deployment target — determines serving and storage behavior."""
    LOCAL_MAC = "local_mac"    # M1 Mini, FastAPI on LAN, local tile storage
    CLOUD = "cloud"            # S3 tile storage, cloud-hosted API


# Named region presets — switch active region by changing REGION below
REGIONS: dict[str, tuple[float, float, float, float]] = {
    # (min_lat, max_lat, min_lng, max_lng)
    "san_francisco": (37.25, 38.2, -122.78, -121.8),
    "persian_gulf":  (23.5, 30.5, 47.5, 59.5),
}
ACTIVE_REGIONS: list[str] = ["san_francisco", "persian_gulf"]
REGION_NAME = ACTIVE_REGIONS[0]  # default / backward compat
REGION = REGIONS[REGION_NAME]

# Per-region advisories — shown to clients via /health coverage response
REGION_ADVISORIES: dict[str, list[str]] = {
    "persian_gulf": [
        "AIS data in the Strait of Hormuz is subject to GPS jamming, "
        "spoofing, and electronic warfare. Vessel positions may be "
        "inaccurate, delayed, or missing entirely.",
        "AISStream terrestrial receiver coverage in the Persian Gulf "
        "is sparse. Expect intermittent vessel data.",
    ],
}


class FeedConfig(BaseModel):
    """Data feed connection settings."""
    ais_endpoint: str = Field(
        default="https://stream.aisstream.io/v0/stream",
        description="AIS WebSocket stream endpoint",
    )
    ais_api_key: str = Field(
        default="",
        description="AIS stream API key (from env: SPATIAL_AGENTS_AIS_KEY)",
    )
    adsb_endpoint: str = Field(
        default="https://opensky-network.org/api",
        description="ADS-B REST API endpoint (OpenSky Network)",
    )
    adsb_client_id: str = Field(
        default="",
        description="OpenSky Network OAuth2 client ID",
    )
    adsb_client_secret: str = Field(
        default="",
        description="OpenSky Network OAuth2 client secret",
    )
    adsb_poll_interval_sec: int = Field(
        default=45,
        description="Seconds between ADS-B position polls",
    )


class TilingConfig(BaseModel):
    """H3 spatial indexing configuration."""
    resolutions: list[int] = Field(
        default=[3, 4, 5, 6, 7],
        description="H3 resolutions to generate tiles for",
    )
    temporal_bins: dict[int, str] = Field(
        default={3: "1day", 4: "1hour", 5: "5min", 6: "1min", 7: "live"},
        description="Temporal bin size per resolution level",
    )
    tile_output_dir: Path = Field(
        default=Path("/data/tiles/h3"),
        description="Root directory for generated tile files",
    )
    tile_format: str = Field(
        default="geojson",
        description="Tile output format: geojson or protobuf",
    )


class FMConfig(BaseModel):
    """Foundation Models evaluation settings."""
    context_window_size: int = Field(
        default=4096,
        description="On-device FM context window (tokens). Queried dynamically when SDK available.",
    )
    max_prompt_budget_pct: float = Field(
        default=0.15,
        description="Maximum fraction of context window allocated to data payload",
    )
    max_tool_budget_pct: float = Field(
        default=0.25,
        description="Maximum fraction of context window allocated to tool schemas",
    )
    prompt_template_dir: Path = Field(
        default=Path("prompts"),
        description="Directory containing versioned prompt templates",
    )


class ServingConfig(BaseModel):
    """FastAPI server settings."""
    host: str = Field(
        default="127.0.0.1",
        description="Bind address (localhost when behind Apache proxy, 0.0.0.0 for direct access)",
    )
    port: int = Field(default=8012, description="Server port")
    cors_origins: list[str] = Field(
        default=["*"],
        description="Allowed CORS origins (restrict in production)",
    )
    static_tile_dir: Path = Field(
        default=Path("/data/tiles"),
        description="Root for static file serving",
    )


class CloudConfig(BaseModel):
    """Cloud deployment settings (S3, CDN)."""
    s3_bucket: str = Field(default="", description="S3 bucket for tile storage")
    s3_region: str = Field(default="us-west-2", description="AWS region")
    s3_prefix: str = Field(default="tiles/h3", description="Key prefix for tiles")
    cdn_distribution_id: str = Field(default="", description="CloudFront distribution ID")


class SpatialAgentsConfig(BaseModel):
    """Root configuration — all subsystem settings."""
    mode: DeploymentMode = Field(
        default=DeploymentMode.LOCAL_MAC,
        description="Deployment mode: local_mac or cloud",
    )
    feeds: FeedConfig = Field(default_factory=FeedConfig)
    tiling: TilingConfig = Field(default_factory=TilingConfig)
    fm: FMConfig = Field(default_factory=FMConfig)
    serving: ServingConfig = Field(default_factory=ServingConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    data_dir: Path = Field(
        default=Path("/data"),
        description="Root data directory for tiles, cache, logs",
    )

    @classmethod
    def from_env(cls) -> SpatialAgentsConfig:
        """Build configuration from environment variables with sensible defaults."""
        mode = DeploymentMode(os.getenv("SPATIAL_AGENTS_MODE", "local_mac"))

        # Adjust defaults based on deployment mode
        if mode == DeploymentMode.LOCAL_MAC:
            default_data = Path(__file__).resolve().parent.parent / "data"
        else:
            default_data = Path("/data")
        data_dir = Path(os.getenv("SPATIAL_AGENTS_DATA_DIR", str(default_data)))
        tile_dir = Path(os.getenv("SPATIAL_AGENTS_TILE_DIR", str(data_dir / "tiles" / "h3")))

        return cls(
            mode=mode,
            feeds=FeedConfig(
                ais_api_key=os.getenv("SPATIAL_AGENTS_AIS_KEY", ""),
                adsb_client_id=os.getenv("SPATIAL_AGENTS_ADSB_CLIENT_ID", ""),
                adsb_client_secret=os.getenv("SPATIAL_AGENTS_ADSB_CLIENT_SECRET", ""),
            ),
            tiling=TilingConfig(tile_output_dir=tile_dir),
            serving=ServingConfig(
                port=int(os.getenv("SPATIAL_AGENTS_PORT", "8012")),
                static_tile_dir=tile_dir.parent,
            ),
            cloud=CloudConfig(
                s3_bucket=os.getenv("SPATIAL_AGENTS_S3_BUCKET", ""),
                s3_region=os.getenv("SPATIAL_AGENTS_S3_REGION", "us-west-2"),
            ),
            data_dir=data_dir,
        )


# Module-level singleton — import and use directly
config = SpatialAgentsConfig.from_env()
