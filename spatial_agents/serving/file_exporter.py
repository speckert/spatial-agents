"""
File Exporter — Write intelligence payloads to shared filesystem.

Alternative delivery mode for clients that poll a shared directory
rather than connecting via REST API. Useful for:
    - Vision Pro apps reading from iCloud Drive
    - iPhone apps with file-based sync
    - Debugging and offline analysis

Version History:
    0.1.0  2026-03-28  Initial file exporter
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson

logger = logging.getLogger(__name__)


class FileExporter:
    """
    Export intelligence payloads to a shared directory.

    Usage:
        exporter = FileExporter(output_dir=Path("/shared/spatial-agents"))
        exporter.write_payload("intelligence", "842831dffffffff", data)
    """

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._writes = 0

    @property
    def stats(self) -> dict[str, int]:
        return {"writes": self._writes}

    def write_payload(
        self,
        category: str,
        identifier: str,
        data: dict[str, Any],
    ) -> Path:
        """
        Write a JSON payload to the export directory.

        Args:
            category: Subdirectory (e.g. "intelligence", "causal", "tiles")
            identifier: Filename stem (e.g. H3 cell ID)
            data: Payload dict to serialize

        Returns:
            Path to the written file
        """
        # Add export metadata
        data["_exported_at"] = datetime.now(timezone.utc).isoformat()

        dir_path = self._output_dir / category
        dir_path.mkdir(parents=True, exist_ok=True)

        file_path = dir_path / f"{identifier}.json"
        file_path.write_bytes(
            orjson.dumps(data, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        )

        self._writes += 1
        logger.debug("Exported: %s", file_path)
        return file_path

    def write_latest(
        self,
        category: str,
        data: dict[str, Any],
    ) -> Path:
        """Write a 'latest' snapshot that clients can poll."""
        return self.write_payload(category, "latest", data)

    def list_exports(self, category: str | None = None) -> list[Path]:
        """List all exported files, optionally filtered by category."""
        if category:
            search_dir = self._output_dir / category
            if not search_dir.exists():
                return []
            return sorted(search_dir.glob("*.json"))
        return sorted(self._output_dir.rglob("*.json"))
