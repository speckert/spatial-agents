"""
__main__ — Enables `python -m spatial_agents` execution.

Delegates to the CLI entry point in spatial_agents.main.

Usage:
    python -m spatial_agents
    python -m spatial_agents --help
    python -m spatial_agents --port 8012 --verbose

Version History:
    0.1.0  2026-03-28  Initial __main__ entry point
"""

from spatial_agents.main import cli

if __name__ == "__main__":
    cli()
