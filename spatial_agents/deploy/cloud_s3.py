"""
Cloud Deployment — S3 tile sync and cloud-hosted API configuration.

Same Python pipeline as local Mac, deployed to cloud infrastructure:
    - Tiles synced to S3 bucket for CDN delivery
    - FastAPI for dynamic queries
    - ARM64 container (matches M-series architecture)

Usage:
    python -m spatial_agents.deploy.cloud_s3
    python -m spatial_agents.deploy.cloud_s3 --help
    python -m spatial_agents.deploy.cloud_s3 --s3-bucket my-tiles --verbose

Version History:
    0.1.0  2026-03-28  Initial cloud deployment config
    0.1.1  2026-03-28  Added argparse CLI with --port, --s3-bucket, --s3-region,
                       --verbose flags and --help support
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from spatial_agents.config import SpatialAgentsConfig

logger = logging.getLogger(__name__)


class S3TileSync:
    """
    Sync locally generated tiles to an S3 bucket.

    Runs alongside the tile builder — when new tiles are written locally,
    they are uploaded to S3 for CDN distribution.

    Requires boto3 (optional dependency: pip install spatial-agents[cloud])
    """

    def __init__(self, config: SpatialAgentsConfig) -> None:
        self._bucket = config.cloud.s3_bucket
        self._prefix = config.cloud.s3_prefix
        self._region = config.cloud.s3_region
        self._local_dir = config.tiling.tile_output_dir
        self._client = None
        self._synced_count = 0

    async def initialize(self) -> None:
        """Initialize S3 client."""
        try:
            import boto3
            self._client = boto3.client("s3", region_name=self._region)
            logger.info("S3 sync initialized: s3://%s/%s", self._bucket, self._prefix)
        except ImportError:
            logger.error("boto3 not installed — run: pip install spatial-agents[cloud]")
            raise

    def sync_tile(self, local_path: Path) -> str | None:
        """Upload a single tile to S3. Returns the S3 key."""
        if self._client is None:
            logger.warning("S3 client not initialized")
            return None

        # Compute S3 key from local path relative to tile directory
        try:
            relative = local_path.relative_to(self._local_dir)
        except ValueError:
            relative = Path(local_path.name)

        s3_key = f"{self._prefix}/{relative}"

        try:
            self._client.upload_file(
                str(local_path),
                self._bucket,
                s3_key,
                ExtraArgs={
                    "ContentType": "application/json",
                    "CacheControl": "max-age=60",  # 1 minute cache for live tiles
                },
            )
            self._synced_count += 1
            logger.debug("Synced to S3: %s", s3_key)
            return s3_key
        except Exception as exc:
            logger.error("S3 upload error for %s: %s", s3_key, exc)
            return None

    def sync_all(self) -> int:
        """Sync all local tiles to S3. Returns count of synced files."""
        if not self._local_dir.exists():
            return 0

        count = 0
        for tile_path in self._local_dir.rglob("*.json"):
            if self.sync_tile(tile_path):
                count += 1

        logger.info("Synced %d tiles to S3", count)
        return count

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "bucket": self._bucket,
            "prefix": self._prefix,
            "synced_count": self._synced_count,
        }


def run() -> None:
    """Start the Spatial Agents server in cloud mode."""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(
        prog="spatial-agents-cloud",
        description="Spatial Agents — Cloud Server (S3 tile sync + FastAPI)",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Server port (default: 8012)",
    )
    parser.add_argument(
        "--s3-bucket", type=str, default=None,
        help="S3 bucket for tile storage",
    )
    parser.add_argument(
        "--s3-region", type=str, default=None,
        help="AWS region (default: us-west-2)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger(__name__)

    os.environ["SPATIAL_AGENTS_MODE"] = "cloud"
    if args.port:
        os.environ["SPATIAL_AGENTS_PORT"] = str(args.port)
    if args.s3_bucket:
        os.environ["SPATIAL_AGENTS_S3_BUCKET"] = args.s3_bucket
    if args.s3_region:
        os.environ["SPATIAL_AGENTS_S3_REGION"] = args.s3_region

    config = SpatialAgentsConfig.from_env()

    logger.info("Spatial Agents — Cloud Server")
    logger.info("S3 bucket: %s", config.cloud.s3_bucket)
    logger.info("Port: %d", config.serving.port)

    uvicorn.run(
        "spatial_agents.serving.app:app",
        host="0.0.0.0",
        port=config.serving.port,
        log_level="info",
        workers=1,
    )


if __name__ == "__main__":
    run()
