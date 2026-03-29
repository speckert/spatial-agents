"""
Local Mac Deployment — Neural Magician (M1 16GB) server configuration.

Run the full pipeline + API server on the local Mac:
    - FastAPI on localhost:8012 (behind Apache reverse proxy)
    - Apache handles HTTPS via Certbot on ports 80/443
    - ~7W idle power draw, headless operation
    - Neural Engine available for FM inference and Core ML

Usage:
    python -m spatial_agents.deploy.local_mac
    python -m spatial_agents.deploy.local_mac --help
    python -m spatial_agents.deploy.local_mac --port 9000 --verbose

Version History:
    0.1.0  2026-03-28  Initial local Mac deployment
    0.1.1  2026-03-28  Added argparse CLI with --port, --host, --tile-dir,
                       --verbose flags and --help support
    0.1.2  2026-03-28  Updated for Neural Magician (M1 16GB), localhost binding
                       behind Apache reverse proxy, removed direct LAN exposure
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn

from spatial_agents.config import SpatialAgentsConfig, DeploymentMode


def setup_logging() -> None:
    """Configure logging for local Mac deployment."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def ensure_directories(config: SpatialAgentsConfig) -> None:
    """Create required directories if they don't exist."""
    dirs = [
        config.data_dir,
        config.tiling.tile_output_dir,
        config.data_dir / "cache",
        config.data_dir / "exports",
        config.data_dir / "logs",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def run() -> None:
    """Start the Spatial Agents server in local Mac mode."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="spatial-agents-local",
        description="Spatial Agents — Local Mac Server (M1 Mini on LAN)",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Server port (default: 8012)",
    )
    parser.add_argument(
        "--host", type=str, default=None,
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--tile-dir", type=str, default=None,
        help="Tile output directory (default: /data/tiles/h3)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    setup_logging()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    logger = logging.getLogger(__name__)

    # Force local Mac mode
    os.environ["SPATIAL_AGENTS_MODE"] = "local_mac"
    if args.port:
        os.environ["SPATIAL_AGENTS_PORT"] = str(args.port)
    if args.tile_dir:
        os.environ["SPATIAL_AGENTS_TILE_DIR"] = args.tile_dir

    config = SpatialAgentsConfig.from_env()
    if args.host:
        config.serving.host = args.host

    logger.info("=" * 60)
    logger.info("Spatial Agents — Local Mac Server")
    logger.info("=" * 60)
    logger.info("Mode:        %s", config.mode.value)
    logger.info("Port:        %d", config.serving.port)
    logger.info("Host:        %s", config.serving.host)
    logger.info("Tile dir:    %s", config.tiling.tile_output_dir)
    logger.info("Resolutions: %s", config.tiling.resolutions)
    logger.info("FM context:  %d tokens", config.fm.context_window_size)
    logger.info("=" * 60)

    ensure_directories(config)

    uvicorn.run(
        "spatial_agents.serving.app:app",
        host=config.serving.host,
        port=config.serving.port,
        log_level="info",
        reload=False,
        workers=1,  # Single worker — we manage state in-process
        access_log=True,
    )


if __name__ == "__main__":
    run()
