"""Tests for per-region tile retention — reaper, surgical clear, migration."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h3

from spatial_agents.config import REGION_RESOLUTION
from spatial_agents.regions.manager import RegionsManager
from spatial_agents.spatial.temporal_bins import TemporalBinner
from spatial_agents.spatial.tile_reaper import (
    find_trash_dirs,
    is_flat_layout,
    migrate_flat_tree,
    reap_expired_tiles,
    trash_region,
)

# Two distinct region keys (res-4 H3 cells) for isolation tests.
KEY_A = "841f24bffffffff"
KEY_B = "8428309ffffffff"


def _bin_key(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def _write_tile(root: Path, region: str, res: int, cell: str, bin_key: str) -> Path:
    p = root / region / str(res) / cell / f"{bin_key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    return p


# --- region_key: durable, geography-derived identity ------------------------

def test_region_key_deterministic_and_stable() -> None:
    mgr = RegionsManager(state_path=None)
    sf = (37.78, -122.42)
    chi = (41.88, -87.63)
    mgr._ensure_region_registered("san_francisco", sf)
    mgr._ensure_region_registered("chicago", chi)

    # Deterministic: equals the center cell at the display resolution.
    assert mgr.region_key("san_francisco") == h3.latlng_to_cell(
        sf[0], sf[1], REGION_RESOLUTION
    )
    # Stable across repeated calls and re-registration (swap-out/swap-in).
    first = mgr.region_key("san_francisco")
    mgr._ensure_region_registered("san_francisco", sf)
    assert mgr.region_key("san_francisco") == first
    # Different centers → different keys.
    assert mgr.region_key("san_francisco") != mgr.region_key("chicago")
    # Unknown region → None.
    assert mgr.region_key("atlantis") is None


# --- is_bin_expired (max_age mode) -----------------------------------------

def test_is_bin_expired_max_age() -> None:
    b = TemporalBinner()
    now = datetime.now(timezone.utc)
    assert b.is_bin_expired(_bin_key(now - timedelta(hours=30)), 5, max_age_hours=24)
    assert not b.is_bin_expired(_bin_key(now - timedelta(hours=1)), 5, max_age_hours=24)
    assert not b.is_bin_expired("live", 7, max_age_hours=24)
    assert not b.is_bin_expired("not-a-timestamp", 5, max_age_hours=24)
    assert not b.is_bin_expired("live", 7)  # legacy mode still works


# --- reaper: per-region expiration + isolation ------------------------------

def test_reaper_deletes_old_keeps_new(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    old_p = _write_tile(tmp_path, KEY_A, 5, "85aaa", _bin_key(now - timedelta(hours=48)))
    new_p = _write_tile(tmp_path, KEY_A, 5, "85bbb", _bin_key(now - timedelta(hours=2)))

    deleted = reap_expired_tiles(tmp_path, retention_hours=24)

    assert deleted == 1
    assert not old_p.exists()
    assert new_p.exists()
    # Emptied cell dir is cleaned up; the still-populated one and the res dir stay.
    assert not old_p.parent.exists()
    assert new_p.parent.exists()


def test_reaper_isolates_regions(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    old_a = _write_tile(tmp_path, KEY_A, 5, "85aaa", _bin_key(now - timedelta(hours=48)))
    new_b = _write_tile(tmp_path, KEY_B, 5, "85bbb", _bin_key(now - timedelta(hours=2)))
    old_b = _write_tile(tmp_path, KEY_B, 5, "85ccc", _bin_key(now - timedelta(hours=48)))

    deleted = reap_expired_tiles(tmp_path, retention_hours=24)

    assert deleted == 2          # old tiles in BOTH regions
    assert not old_a.exists()
    assert not old_b.exists()
    assert new_b.exists()        # fresh tile in region B survives
    assert (tmp_path / KEY_B).exists()


def test_reaper_skips_res7_live(tmp_path: Path) -> None:
    live_p = _write_tile(tmp_path, KEY_A, 7, "87ccc", "live")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    os.utime(live_p, (old_ts, old_ts))

    assert reap_expired_tiles(tmp_path, retention_hours=24) == 0
    assert live_p.exists()


def test_reaper_ignores_trash_and_tolerates_missing(tmp_path: Path) -> None:
    # Nonexistent root → no error.
    assert reap_expired_tiles(tmp_path / "nope", retention_hours=24) == 0

    # A region trash dir (being background-deleted) is left untouched.
    now = datetime.now(timezone.utc)
    trashed = _write_tile(
        tmp_path, f"{KEY_A}.trash-20260101T000000", 5, "85aaa",
        _bin_key(now - timedelta(hours=48)),
    )
    # Malformed bin filename is left in place, not crashed on.
    bad = _write_tile(tmp_path, KEY_B, 5, "85ddd", "garbage")

    assert reap_expired_tiles(tmp_path, retention_hours=24) == 0
    assert trashed.exists()
    assert bad.exists()


# --- surgical city-change clear --------------------------------------------

def test_trash_region_is_surgical(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    a_tile = _write_tile(tmp_path, KEY_A, 5, "85aaa", _bin_key(now))
    b_tile = _write_tile(tmp_path, KEY_B, 5, "85bbb", _bin_key(now))

    trash = trash_region(tmp_path, KEY_A)

    assert trash is not None
    assert trash.name.startswith(f"{KEY_A}.trash-")
    assert trash.parent == tmp_path           # trash lives inside root
    assert not (tmp_path / KEY_A).exists()    # departing region gone from live tree
    assert (trash / "5" / "85aaa").exists()   # its tiles preserved in trash
    assert not a_tile.exists()
    # The OTHER region is completely untouched — the SF-preserved-on-swap fix.
    assert b_tile.exists()
    assert (tmp_path / KEY_B).exists()


def test_trash_region_absent_is_noop(tmp_path: Path) -> None:
    (tmp_path / KEY_A).mkdir(parents=True)  # root exists, region B does not
    assert trash_region(tmp_path, KEY_B) is None


# --- migration of the legacy flat tree -------------------------------------

def test_is_flat_layout(tmp_path: Path) -> None:
    flat = tmp_path / "flat"
    (flat / "5" / "85aaa").mkdir(parents=True)        # <res>/<cell>
    assert is_flat_layout(flat) is True

    perregion = tmp_path / "perregion"
    (perregion / KEY_A / "5" / "85aaa").mkdir(parents=True)
    assert is_flat_layout(perregion) is False


def test_migrate_flat_tree(tmp_path: Path) -> None:
    root = tmp_path / "h3"
    # Legacy flat layout: <res>/<cell>/<bin>.json, no region segment.
    flat_tile = root / "5" / "85aaa" / "20260101T000000.json"
    flat_tile.parent.mkdir(parents=True)
    flat_tile.write_text("{}")

    trash = migrate_flat_tree(root)

    assert trash is not None
    assert trash.name.startswith("h3.trash-migrate-")
    assert trash.parent == root.parent              # whole tree set aside beside root
    assert root.exists() and not any(root.iterdir())  # fresh empty root
    assert (trash / "5" / "85aaa").exists()          # old data preserved for delete

    # Already per-region → no migration.
    _write_tile(root, KEY_A, 5, "85bbb", "20260101T000000")
    assert migrate_flat_tree(root) is None


def test_find_trash_dirs_both_locations(tmp_path: Path) -> None:
    root = tmp_path / "h3"
    root.mkdir()
    inside = root / f"{KEY_A}.trash-20260101T000000"     # surgical clear (inside root)
    inside.mkdir()
    beside = tmp_path / "h3.trash-migrate-20260101T000000"  # migration (beside root)
    beside.mkdir()
    (root / KEY_B).mkdir()          # a live region — not trash
    (tmp_path / "unrelated").mkdir()

    found = {p.name for p in find_trash_dirs(root)}
    assert found == {inside.name, beside.name}
