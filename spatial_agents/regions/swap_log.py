"""
SwapLog — Append-only audit log of /regions/swap attempts.

Persists each swap attempt to a JSONL file (one JSON object per line).
Captures the user-typed city string, the resolved region key, the
client IP, and the outcome (success / rate_limited / swap_refused /
geocode_failed). Used by the /stats/swaps endpoint that feeds the
"Region Swaps" panel on logs.html.

This is operator-facing diagnostic data, not user-facing telemetry.
The log file lives next to regions_state.json under data/.

Version History:
    0.1.0  2026-04-26  Initial implementation — Claude 4.7
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SwapLog:
    """Append-only JSONL persistence of /regions/swap attempts."""

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        # Append-only writes from the FastAPI thread pool may overlap;
        # a process-local lock keeps lines from interleaving.
        self._write_lock = threading.Lock()

    def record(
        self,
        *,
        city: str,
        region_key: str | None,
        ip: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Append one swap attempt to the log.

        Args:
            city:       The user-typed input (raw, any language).
            region_key: The normalized snake_case slug, or None if the
                        request never got far enough to resolve one
                        (e.g. geocode_failed before normalization).
            ip:         Client IP. For requests through Apache, the
                        caller should pass the X-Forwarded-For value.
            status:     One of "success", "rate_limited", "swap_refused",
                        "geocode_failed", or "error".
            error:      Optional human-readable detail for non-success
                        statuses.
        """
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "city": city,
            "region_key": region_key,
            "ip": ip,
            "status": status,
            "error": error,
        }
        line = json.dumps(entry, ensure_ascii=False)
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_lock:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as exc:  # pragma: no cover — disk failure
            logger.error("Failed to append to swap log %s: %s", self._log_path, exc)

    def read_recent(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return the last `limit` entries, newest first.

        File is read fully and tail-sliced — fine for our scale (one
        entry per swap, swaps rate-limited to one per minute, so a
        year's worth is ~525k lines / a few MB). Switch to reverse-
        seek if it ever grows past that.
        """
        if not self._log_path.exists():
            return []
        try:
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            logger.error("Failed to read swap log %s: %s", self._log_path, exc)
            return []

        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip corrupt lines rather than crash the dashboard.
                continue
        out.reverse()  # newest first
        return out
