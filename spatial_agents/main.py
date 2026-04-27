"""
Main — Entry point for the Spatial Agents pipeline.

Orchestrates:
    1. Configuration loading
    2. Feed manager startup (AIS, ADS-B)
    3. Tile generation pipeline
    4. FastAPI server launch

Usage:
    # Help
    python -m spatial_agents --help
    spatial-agents --help

    # Local Mac mode (default)
    python -m spatial_agents
    spatial-agents --port 8012

    # Cloud mode
    SPATIAL_AGENTS_MODE=cloud python -m spatial_agents

Version History:
    0.1.0  2026-03-28  Initial entry point with --mode, --port, --verbose CLI
    0.1.1  2026-03-28  Added __main__.py for python -m spatial_agents support,
                       updated usage documentation
    0.1.2  2026-03-28  Updated banner to SpeckTech Inc.
    0.2.0  2026-04-26  Wires up RegionsManager — initializes from
                       data/regions_state.json before feeds start, registers
                       FeedManager.handle_region_swap as a swap callback, and
                       injects the manager into routes_regions so POST
                       /regions/swap is live. — Claude 4.7
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from spatial_agents.config import SpatialAgentsConfig, DeploymentMode


def setup_logging(verbose: bool = False) -> None:
    """Configure structured logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet noisy libraries
    for lib in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(lib).setLevel(logging.WARNING)


async def run_pipeline(config: SpatialAgentsConfig) -> None:
    """
    Main async pipeline — starts feeds, tile builder, and server.
    """
    logger = logging.getLogger(__name__)

    # Late imports to avoid circular dependencies
    from spatial_agents.ingest.feed_manager import FeedManager
    from spatial_agents.regions import RegionsManager
    from spatial_agents.spatial.tile_builder import TileBuilder
    from spatial_agents.serving.routes_api import set_feed_manager as set_api_feeds
    from spatial_agents.serving.routes_health import (
        set_feed_manager as set_health_feeds,
        set_regions_manager as set_health_regions,
    )
    from spatial_agents.regions.swap_log import SwapLog
    from spatial_agents.serving.routes_regions import (
        set_regions_manager,
        set_swap_log as set_regions_swap_log,
    )
    from spatial_agents.serving.routes_stats import (
        set_feed_manager as set_stats_feeds,
        set_swap_log as set_stats_swap_log,
    )
    from spatial_agents.serving.routes_tfr import set_feed_manager as set_tfr_feeds
    from spatial_agents.serving.routes_weather import set_feed_manager as set_weather_feeds

    # Initialize components
    feed_manager = FeedManager()
    tile_builder = TileBuilder(output_dir=config.tiling.tile_output_dir)

    # RegionsManager: load persisted slot-1 (if any) and seed ACTIVE_REGIONS
    # *before* feeds start, so the AIS subscription and ADS-B poll loop come
    # up bound to the right bboxes from frame 1.
    regions_manager = RegionsManager(
        state_path=config.data_dir / "regions_state.json",
    )
    regions_manager.initialize()
    regions_manager.on_swap(feed_manager.handle_region_swap)

    # SwapLog: append-only audit trail of /regions/swap attempts.
    # Surfaced via /stats/swaps for the logs.html dashboard.
    swap_log = SwapLog(log_path=config.data_dir / "swap_log.jsonl")

    # Wire up feed manager to API routes
    set_api_feeds(feed_manager)
    set_health_feeds(feed_manager)
    set_stats_feeds(feed_manager)
    set_weather_feeds(feed_manager)
    set_tfr_feeds(feed_manager)
    set_regions_manager(regions_manager)
    set_health_regions(regions_manager)
    set_regions_swap_log(swap_log)
    set_stats_swap_log(swap_log)

    # Register tile-building callback on new records
    def on_new_data_batch() -> None:
        """Triggered periodically to rebuild tiles from latest data."""
        vessels = feed_manager.get_latest_vessels()
        aircraft = feed_manager.get_latest_aircraft()
        if vessels or aircraft:
            tile_builder.build_all_resolutions(vessels, aircraft)

    # Start feeds
    logger.info("Starting data feeds...")
    await feed_manager.start()

    # Periodic tile rebuild task
    async def tile_rebuild_loop() -> None:
        while True:
            await asyncio.sleep(60)  # Rebuild tiles every 60 seconds
            try:
                on_new_data_batch()
            except Exception as exc:
                logger.error("Tile rebuild error: %s", exc)

    tile_task = asyncio.create_task(tile_rebuild_loop(), name="tile_rebuild")

    # Run server
    import uvicorn
    server_config = uvicorn.Config(
        "spatial_agents.serving.app:app",
        host=config.serving.host,
        port=config.serving.port,
        log_level="info",
        workers=1,
    )
    server = uvicorn.Server(server_config)

    try:
        await server.serve()
    finally:
        tile_task.cancel()
        await feed_manager.stop()
        logger.info("Pipeline shutdown complete")


def cli() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="spatial-agents",
        description="Spatial Agents — Geospatial Intelligence Pipeline",
    )
    parser.add_argument(
        "--mode",
        choices=["local_mac", "cloud"],
        default=None,
        help="Deployment mode (default: from env or local_mac)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Server port (default: 8012)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Apply CLI args to environment
    if args.mode:
        os.environ["SPATIAL_AGENTS_MODE"] = args.mode
    if args.port:
        os.environ["SPATIAL_AGENTS_PORT"] = str(args.port)

    setup_logging(verbose=args.verbose)
    logger = logging.getLogger(__name__)

    config = SpatialAgentsConfig.from_env()

    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║   Spatial Agents Intelligence Server     ║")
    logger.info("║   SpeckTech Inc.                         ║")
    logger.info("╠══════════════════════════════════════════╣")
    logger.info("║  Mode:        %-26s ║", config.mode.value)
    logger.info("║  Port:        %-26d ║", config.serving.port)
    logger.info("║  Resolutions: %-26s ║", str(config.tiling.resolutions))
    logger.info("║  FM context:  %-22d tkn ║", config.fm.context_window_size)
    logger.info("╚══════════════════════════════════════════╝")

    # Ensure data directories exist
    for d in [config.data_dir, config.tiling.tile_output_dir, config.data_dir / "cache"]:
        d.mkdir(parents=True, exist_ok=True)

    # Run
    try:
        asyncio.run(run_pipeline(config))
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
        sys.exit(0)


if __name__ == "__main__":
    cli()
