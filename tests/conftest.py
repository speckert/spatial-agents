"""
Pytest Configuration — Shared fixtures for the Spatial Agents test suite.

Provides pre-loaded sample data, temporary tile directories, and
configured pipeline components for use across all test modules.

Version History:
    0.1.0  2026-03-28  Initial conftest with sample data fixtures
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def sample_vessels():
    """Pre-loaded SF Bay vessel records with H3 cells assigned."""
    from spatial_agents.data import SAMPLE_VESSELS
    return SAMPLE_VESSELS


@pytest.fixture
def sample_aircraft():
    """Pre-loaded SFO/OAK aircraft records with H3 cells assigned."""
    from spatial_agents.data import SAMPLE_AIRCRAFT
    return SAMPLE_AIRCRAFT


@pytest.fixture
def sample_dark_gap_track():
    """Vessel track with a 45-minute AIS dark gap."""
    from spatial_agents.data import SAMPLE_DARK_GAP_TRACK
    return SAMPLE_DARK_GAP_TRACK


@pytest.fixture
def feed_manager():
    """FeedManager pre-loaded with all sample data."""
    from spatial_agents.data import load_sample_data
    return load_sample_data()


@pytest.fixture
def tile_dir(tmp_path: Path) -> Path:
    """Temporary directory for tile output."""
    d = tmp_path / "tiles"
    d.mkdir()
    return d


@pytest.fixture
def h3_indexer():
    """Configured H3 indexer with default resolutions."""
    from spatial_agents.spatial.h3_indexer import H3Indexer
    return H3Indexer()


@pytest.fixture
def tile_builder(tile_dir: Path):
    """Tile builder writing to a temporary directory."""
    from spatial_agents.spatial.tile_builder import TileBuilder
    return TileBuilder(output_dir=tile_dir)


@pytest.fixture
def event_detector():
    """Event detector with default thresholds."""
    from spatial_agents.causal.event_detector import EventDetector
    return EventDetector()


@pytest.fixture
def dag_builder():
    """DAG builder with default domain rules."""
    from spatial_agents.causal.dag_builder import DAGBuilder
    return DAGBuilder()


@pytest.fixture
def prompt_library():
    """Prompt library with all default templates."""
    from spatial_agents.intelligence.prompt_templates import PromptLibrary
    return PromptLibrary()


@pytest.fixture
def token_budget():
    """Token budget manager with 4096-token context."""
    from spatial_agents.intelligence.token_budget import TokenBudgetManager
    return TokenBudgetManager(context_size=4096)


@pytest.fixture
def schema_validator():
    """Schema validator with default registry."""
    from spatial_agents.intelligence.schema_validator import SchemaValidator
    return SchemaValidator()
