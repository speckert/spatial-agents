# Spatial Agents — Cloud Deployment Container
# ARM64 target (matches Apple Silicon architecture)
#
# Packages the full Python pipeline for cloud-hosted operation:
# S3 tile sync, FastAPI dynamic API, and background data ingest.
#
# Version History:
#     0.1.0  2026-03-28  Initial Dockerfile — Python 3.12 slim, ARM64,
#                        cloud extras, data directory structure
#
# Build:   docker build --platform linux/arm64 -t spatial-agents .
# Run:     docker run -p 8012:8012 -e SPATIAL_AGENTS_MODE=cloud spatial-agents

FROM python:3.12-slim AS base

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[cloud]"

# Application code
COPY spatial_agents/ spatial_agents/

# Data directories
RUN mkdir -p /data/tiles/h3 /data/cache /data/exports /data/logs

# Environment
ENV SPATIAL_AGENTS_MODE=cloud
ENV SPATIAL_AGENTS_PORT=8012
ENV SPATIAL_AGENTS_DATA_DIR=/data
ENV SPATIAL_AGENTS_TILE_DIR=/data/tiles/h3

EXPOSE 8012

CMD ["python", "-m", "spatial_agents.deploy.cloud_s3"]
