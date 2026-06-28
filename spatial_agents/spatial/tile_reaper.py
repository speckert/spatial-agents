"""
Tile Reaper — bounded, per-region retention for the H3 snapshot tree.

The tree is region-segmented: ``<root>/<region_key>/<res>/<cell>/<bin>.json``,
where ``region_key`` is the center cell of a region's 7-hex display flower (see
RegionsManager.region_key). Region isolation lets us expire or clear ONE region
without touching the others.

All helpers are filesystem-only (never open a tile), so they stay fast over
millions of files:

    reap_expired_tiles()  — delete tiles whose temporal-bin filename is older
                            than a retention window, walking every region.
    trash_region()        — atomically set ONE region's subtree aside
                            (os.rename → ``<root>/<key>.trash-<utc>``) for an
                            instant, surgical city-change clear; the slow delete
                            happens off the swap path.
    migrate_flat_tree()   — one-time: detect the legacy flat ``<res>/...`` layout
                            and set the whole tree aside so it rebuilds per-region.
    find_trash_dirs()     — locate leftover ``*.trash-*`` dirs (inside root from
                            surgical clears, and beside root from whole-tree
                            renames) so a killed run finishes the delete on startup.

Resolution 7 uses the constant bin key "live" (tiles overwrite in place, so the
tree is self-capping at ~one file per cell). The reaper skips res-7 per region.
See docs/DECISIONS-h3-per-region.md.

Version History:
    0.1.0  2026-06-28  Initial reaper + trash helpers (flat layout)
    0.2.0  2026-06-28  Per-region layout: region-segmented walk, surgical
                       trash_region(), flat-tree migration, dual-location sweep
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from spatial_agents.spatial.temporal_bins import TemporalBinner

logger = logging.getLogger(__name__)

_TRASH_MARKER = ".trash-"


def reap_expired_tiles(
    root: Path,
    retention_hours: float,
    binner: TemporalBinner | None = None,
) -> int:
    """Delete tiles whose temporal bin is older than ``retention_hours``.

    Walks ``root/<region>/<res>/<cell>/<bin>.json`` across every region,
    parsing the bin from the filename. Resolutions whose bin size is "live"
    (res-7) are skipped. Empty ``<cell>/`` and ``<region>/<res>/`` dirs are
    removed opportunistically.

    Tolerant of dirs/files (including a whole region) disappearing mid-walk —
    e.g. a concurrent surgical clear: missing paths are skipped, not raised.
    Region trash dirs (being background-deleted) are ignored.

    Returns the number of tile files deleted.
    """
    binner = binner or TemporalBinner()
    deleted = 0

    if not root.exists():
        return 0

    for region_dir in _safe_iterdir(root):
        if not region_dir.is_dir() or _TRASH_MARKER in region_dir.name:
            continue
        deleted += _reap_region(region_dir, retention_hours, binner)

    return deleted


def _reap_region(region_dir: Path, retention_hours: float, binner: TemporalBinner) -> int:
    """Reap expired tiles within a single region subtree."""
    deleted = 0
    for res_dir in _safe_iterdir(region_dir):
        if not res_dir.is_dir():
            continue
        try:
            resolution = int(res_dir.name)
        except ValueError:
            continue  # not a resolution dir

        # res-7 "live" tiles carry a constant key, not a timestamp — skip.
        if binner.get_bin_size(resolution) == "live":
            continue

        for cell_dir in _safe_iterdir(res_dir):
            if not cell_dir.is_dir():
                continue
            for tile in _safe_iterdir(cell_dir):
                if tile.suffix != ".json":
                    continue
                if binner.is_bin_expired(tile.stem, resolution, retention_hours):
                    try:
                        tile.unlink()
                        deleted += 1
                    except FileNotFoundError:
                        pass
            # Remove emptied cell dir (OSError if non-empty or already gone).
            try:
                cell_dir.rmdir()
            except OSError:
                pass

        # Remove emptied <region>/<res> dir.
        try:
            res_dir.rmdir()
        except OSError:
            pass

    return deleted


def trash_region(root: Path, region_key: str) -> Path | None:
    """Atomically set ONE region's subtree aside for background deletion.

    Renames ``root/<region_key>`` → ``root/<region_key>.trash-<utc-timestamp>``
    (atomic, O(1) on the same filesystem). The other regions' subtrees are
    untouched. Returns the trash path to delete in the background, or None if
    the region had no tiles on disk.
    """
    region_dir = root / region_key
    if not region_dir.exists():
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    trash = root / f"{region_key}{_TRASH_MARKER}{ts}"
    n = 1
    while trash.exists():  # extremely unlikely given the swap cooldown
        trash = root / f"{region_key}{_TRASH_MARKER}{ts}-{n}"
        n += 1
    os.rename(region_dir, trash)
    return trash


def is_flat_layout(root: Path) -> bool:
    """True if ``root`` is the legacy flat layout (resolution dirs at the top).

    Region dirs are H3 hex keys (always contain letters); resolution dirs are
    bare integers. A top-level integer-named dir means the pre-region tree.
    """
    if not root.exists():
        return False
    for child in _safe_iterdir(root):
        if child.is_dir() and child.name.isdigit():
            return True
    return False


def migrate_flat_tree(root: Path) -> Path | None:
    """One-time migration: set the legacy flat tree aside so it rebuilds per-region.

    The flat tree (``<res>/<cell>/<bin>.json``, no region segment) is disposable
    — nothing reads tile contents (see docs/DECISIONS-h3-archive.md) and sorting
    flat files back into regions isn't worth it. We rename the whole tree to a
    sibling ``<name>.trash-migrate-<utc>`` (atomic, instant) and recreate an empty
    root; the caller background-deletes the trash. No-op if already per-region.

    Returns the trash path to delete, or None if no migration was needed.
    """
    if not is_flat_layout(root):
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    trash = root.parent / f"{root.name}{_TRASH_MARKER}migrate-{ts}"
    n = 1
    while trash.exists():
        trash = root.parent / f"{root.name}{_TRASH_MARKER}migrate-{ts}-{n}"
        n += 1
    os.rename(root, trash)
    root.mkdir(parents=True, exist_ok=True)
    return trash


def find_trash_dirs(root: Path) -> list[Path]:
    """Return leftover ``*.trash-*`` dirs from a run killed mid-delete.

    Two locations:
      * inside ``root``  — surgical per-region clears (``<key>.trash-*``)
      * beside ``root``  — whole-tree renames / migration (``<name>.trash-*``)
    """
    found: list[Path] = []

    if root.exists():
        found.extend(
            p for p in root.glob(f"*{_TRASH_MARKER}*") if p.is_dir()
        )

    parent = root.parent
    if parent.exists():
        found.extend(
            p for p in parent.glob(f"{root.name}{_TRASH_MARKER}*") if p.is_dir()
        )

    return sorted(set(found))


def _safe_iterdir(path: Path):
    """iterdir() that yields nothing if the dir vanished mid-walk."""
    try:
        yield from path.iterdir()
    except FileNotFoundError:
        return
