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
import shutil
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
    from spatial_agents.spatial.temporal_bins import TemporalBinner
    from spatial_agents.spatial.tile_builder import TileBuilder
    from spatial_agents.spatial.tile_reaper import (
        find_trash_dirs,
        migrate_flat_tree,
        reap_expired_tiles,
        trash_region,
    )
    from spatial_agents.config import ACTIVE_REGIONS, REGION_CELLS, REGION_RESOLUTION
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
        """Rebuild tiles from latest data, partitioned per region.

        The feed buffers commingle every active region. We split each batch by
        the region whose 7-hex flower (REGION_CELLS[...]['all']) contains the
        record's res-4 cell — the same membership test FeedManager uses to purge
        on swap — and write each region's tiles under its own durable key, so
        regions stay isolated on disk.
        """
        vessels = feed_manager.get_latest_vessels()
        aircraft = feed_manager.get_latest_aircraft()
        if not (vessels or aircraft):
            return
        for region in list(ACTIVE_REGIONS):
            key = regions_manager.region_key(region)
            cells = REGION_CELLS.get(region)
            if key is None or cells is None:
                continue
            member = set(cells["all"])  # type: ignore[arg-type]
            r_vessels = [v for v in vessels if v.h3_cells.get(REGION_RESOLUTION) in member]
            r_aircraft = [a for a in aircraft if a.h3_cells.get(REGION_RESOLUTION) in member]
            if r_vessels or r_aircraft:
                tile_builder.build_all_resolutions(r_vessels, r_aircraft, region_key=key)

    # --- Tile retention: 24 h reaper + async city-change cache clear ---------
    # The H3 snapshot tree (data/tiles/h3) has no native eviction — left alone
    # it fills the disk (see docs/DECISIONS-h3-archive.md). Two mechanisms keep
    # it bounded; both do filesystem-only work (never open a tile).
    binner = TemporalBinner()
    bg_tasks: set[asyncio.Task] = set()

    def _track(task: asyncio.Task) -> None:
        """Hold a reference to a fire-and-forget task so it isn't GC'd."""
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    async def _background_rmtree(path: Path) -> None:
        """Delete a (possibly huge) trash dir off the event loop. Failure logs."""
        try:
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
            logger.info("Background tile delete complete: %s", path.name)
        except Exception as exc:  # pragma: no cover — defensive
            logger.error("Background tile delete failed for %s: %s", path, exc)

    async def on_city_change(old_region: str | None, new_region: str) -> None:
        """Swap callback — surgically clear ONLY the departing region's tiles.

        Per-region isolation means we set aside just `output_dir/<old_key>/`,
        leaving every other region (e.g. SF in slot 0) untouched. A synchronous
        rmtree over the subtree can block for a long time (many small files), so
        we os.rename it to `<old_key>.trash-<utc>` (atomic, instant) and delete
        in the background. The new city writes to its own `<new_key>/` subtree on
        the next rebuild tick.
        """
        if old_region is None:
            return
        old_key = regions_manager.region_key(old_region)
        if old_key is None:
            return
        try:
            trash = await asyncio.to_thread(
                trash_region, config.tiling.tile_output_dir, old_key
            )
        except Exception as exc:
            logger.error(
                "City-change tile clear failed (%s -> %s): %s",
                old_region, new_region, exc,
            )
            return
        if trash is not None:
            logger.info(
                "City change %s -> %s: region %s tiles set aside to %s, deleting in background",
                old_region, new_region, old_key, trash.name,
            )
            _track(asyncio.create_task(_background_rmtree(trash)))

    regions_manager.on_swap(on_city_change)

    async def tile_reaper_loop() -> None:
        """Periodically delete tiles whose temporal bin is older than the window."""
        while True:
            await asyncio.sleep(config.tiling.reaper_interval_seconds)
            if config.tiling.retention_hours <= 0:
                continue  # expiration disabled
            try:
                deleted = await asyncio.to_thread(
                    reap_expired_tiles,
                    config.tiling.tile_output_dir,
                    config.tiling.retention_hours,
                    binner,
                )
                if deleted:
                    logger.info(
                        "Tile reaper: deleted %d expired tiles (> %d h old)",
                        deleted, config.tiling.retention_hours,
                    )
            except Exception as exc:
                logger.error("Tile reaper error: %s", exc)

    # One-time migration: if the on-disk tree is the legacy flat <res>/... layout
    # (no region segment), set the whole thing aside so it rebuilds per-region.
    # Disposable data — nothing reads tile contents — so we don't sort flat files
    # into regions; we just instant-rename and background-delete.
    migrate_trash = migrate_flat_tree(config.tiling.tile_output_dir)
    if migrate_trash is not None:
        logger.info(
            "Migrating legacy flat tile tree to per-region layout — old tree set "
            "aside to %s, deleting in background", migrate_trash.name,
        )

    # Startup cleanup: background-delete the migration trash plus any trash dirs
    # left by a run killed mid-delete (surgical clears inside root, whole-tree
    # renames beside it). Dedup so the migration trash isn't scheduled twice.
    to_delete = set(find_trash_dirs(config.tiling.tile_output_dir))
    if migrate_trash is not None:
        to_delete.add(migrate_trash)
    for leftover in sorted(to_delete):
        logger.info("Startup: scheduling background delete of %s", leftover.name)
        _track(asyncio.create_task(_background_rmtree(leftover)))

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
    reaper_task = asyncio.create_task(tile_reaper_loop(), name="tile_reaper")

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
        reaper_task.cancel()
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
