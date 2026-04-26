"""
FastAPI Application — REST API + static tile server for Spatial Agents.

Single process serving on port 8012:
    /tiles/h3/{res}/{cell}/{bin}.json  — static pre-computed tiles
    /api/vessels/{h3_cell}             — live vessel positions
    /api/aircraft/{h3_cell}            — live aircraft positions
    /api/intelligence/{h3_cell}        — FM situation reports
    /api/causal/{h3_cell}              — causal graphs
    /api/budget                        — token budget status
    /health                            — pipeline health

Version History:
    0.1.0  2026-03-28  Initial FastAPI application
    0.2.0  2026-04-25  Added /api/weather/alerts route for NWS active
                       alerts (polygon + H3 compact cells) — Claude 4.7
    0.3.0  2026-04-25  Added /api/tfr route for FAA active TFRs
                       (polygon + H3 compact cells) — Claude 4.7
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from spatial_agents.config import config
from spatial_agents.serving.routes_api import router as api_router
from spatial_agents.serving.routes_health import router as health_router
from spatial_agents.serving.routes_stats import router as stats_router
from spatial_agents.serving.routes_tfr import router as tfr_router
from spatial_agents.serving.routes_tiles import router as tiles_router
from spatial_agents.serving.routes_weather import router as weather_router

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Spatial Agents Intelligence Server",
        description=(
            "Geospatial intelligence pipeline serving H3 tiles, "
            "live entity positions, FM situation reports, and causal graphs "
            "to iOS, macOS, and visionOS clients."
        ),
        version="0.1.0",
        servers=[
            {"url": "https://agents.specktech.com", "description": "Production"},
            {"url": "http://127.0.0.1:8012", "description": "Local development"},
        ],
    )

    # CORS — allow client apps to connect
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.serving.cors_origins,
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # Static tile serving — pre-computed H3 tiles as JSON files
    tile_dir = config.serving.static_tile_dir
    if tile_dir.exists():
        app.mount(
            "/tiles",
            StaticFiles(directory=str(tile_dir)),
            name="tiles",
        )
        logger.info("Static tile serving enabled: %s", tile_dir)
    else:
        logger.warning("Tile directory not found: %s — static serving disabled", tile_dir)

    # API routes
    app.include_router(api_router, prefix="/api", tags=["api"])
    app.include_router(tiles_router, prefix="/api/tiles", tags=["tiles"])
    app.include_router(weather_router, prefix="/api", tags=["weather"])
    app.include_router(tfr_router, prefix="/api", tags=["tfr"])
    app.include_router(health_router, tags=["health"])
    app.include_router(stats_router, tags=["stats"])

    @app.on_event("startup")
    async def startup() -> None:
        logger.info(
            "Spatial Agents server starting — mode: %s, port: %d",
            config.mode.value, config.serving.port,
        )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        logger.info("Spatial Agents server shutting down")

    return app


# Module-level app instance for uvicorn
app = create_app()
