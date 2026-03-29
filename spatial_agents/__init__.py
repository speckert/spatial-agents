"""
Spatial Agents — Python Intelligence Layer
==========================================

Geospatial data pipeline for the Spatial Agents ecosystem.
Ingests real-time maritime (AIS) and aviation (ADS-B) feeds,
builds H3-indexed tile pyramids, evaluates prompts against
Apple's on-device Foundation Model, constructs structural
causal models, and serves intelligence to iOS/macOS/visionOS clients.

Architecture:
    ingest/       — Feed parsers and data acquisition
    spatial/      — H3 indexing and tile generation
    intelligence/ — FM prompt evaluation and token management
    causal/       — Pearl SCM construction and serialization
    serving/      — FastAPI REST + static tile server
    deploy/       — Local Mac and cloud deployment configs

Copyright (c) 2026 SpeckTech Inc.

Version History:
    0.1.0  2026-03-28  Initial package structure — ingest, spatial, intelligence,
                       causal, serving, and deploy modules
    0.1.1  2026-03-28  Added __main__.py, sample data module, argparse --help
                       across all entry points
    0.1.2  2026-03-28  Updated copyright to SpeckTech Inc., removed external
                       company references
"""

__version__ = "0.1.0"
