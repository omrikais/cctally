"""Tests for `_resolve_block_selector` (Task 1, plan §"Add helper").

The plan's reference test fixture uses `monkeypatch.setenv("HOME", ...)` plus a
top-level `importlib.util.spec_from_file_location("cctally", ...)` to import
the script. That pattern would write to the *real* `~/.local/share/cctally`
because the module-level `DB_PATH` constant is bound at module load and
ignores subsequent `HOME` changes (see `gotcha_smoke_test_pollution`). We use
the project's existing `conftest.load_script` + path-monkeypatch pattern
instead — same coverage, no production-DB pollution.
"""
import datetime as dt
import sqlite3

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture(scope="module")
def ns():
    return load_script()


def _seed_blocks(ns, conn: sqlite3.Connection) -> None:
    """Insert three API-anchored blocks at known canonical keys."""
    canon = ns["_canonical_5h_window_key"]
    rows = [
        # (resets_at_iso, block_start_at_iso) — most recent last
        ("2026-04-30T05:30:00+00:00", "2026-04-30T00:30:00+00:00"),
        ("2026-04-30T10:30:00+00:00", "2026-04-30T05:30:00+00:00"),
        ("2026-04-30T15:30:00+00:00", "2026-04-30T10:30:00+00:00"),
    ]
    for resets_iso, start_iso in rows:
        resets_dt = dt.datetime.fromisoformat(resets_iso)
        key = canon(int(resets_dt.timestamp()))
        conn.execute(
            """
            INSERT INTO five_hour_blocks (
                five_hour_window_key, five_hour_resets_at, block_start_at,
                first_observed_at_utc, last_observed_at_utc,
                final_five_hour_percent,
                created_at_utc, last_updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key, resets_iso, start_iso, start_iso, resets_iso, 50.0,
             start_iso, resets_iso),
        )
    conn.commit()


@pytest.fixture
def conn(ns, tmp_path, monkeypatch):
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    _seed_blocks(ns, conn)
    yield conn
    conn.close()


def test_default_picks_most_recent(ns, conn):
    sel = ns["_resolve_block_selector"](conn, block_start=None, ago=None)
    assert sel is not None
    assert sel["block_start_at"] == "2026-04-30T10:30:00+00:00"


def test_ago_one_picks_previous(ns, conn):
    sel = ns["_resolve_block_selector"](conn, block_start=None, ago=1)
    assert sel["block_start_at"] == "2026-04-30T05:30:00+00:00"


def test_ago_two_picks_two_back(ns, conn):
    sel = ns["_resolve_block_selector"](conn, block_start=None, ago=2)
    assert sel["block_start_at"] == "2026-04-30T00:30:00+00:00"


def test_ago_overshoots_returns_none(ns, conn):
    assert ns["_resolve_block_selector"](conn, block_start=None, ago=99) is None


def test_block_start_naive_iso_treated_as_utc(ns, conn):
    # 10:30 naive -> resets at 15:30 -> matches third row's canonical key.
    sel = ns["_resolve_block_selector"](
        conn, block_start="2026-04-30T10:30", ago=None
    )
    assert sel["block_start_at"] == "2026-04-30T10:30:00+00:00"


def test_block_start_with_offset_normalizes_to_utc(ns, conn):
    # 13:30+03:00 == 10:30Z -> same block as above.
    sel = ns["_resolve_block_selector"](
        conn, block_start="2026-04-30T13:30:00+03:00", ago=None
    )
    assert sel["block_start_at"] == "2026-04-30T10:30:00+00:00"


def test_block_start_jitter_within_10min_resolves(ns, conn):
    # 10:34 naive (4 min after the real block start) — canonical key floors
    # the resets_at to 15:30 -> matches third row.
    sel = ns["_resolve_block_selector"](
        conn, block_start="2026-04-30T10:34", ago=None
    )
    assert sel["block_start_at"] == "2026-04-30T10:30:00+00:00"


def test_block_start_no_match_returns_none(ns, conn):
    sel = ns["_resolve_block_selector"](
        conn, block_start="2025-01-01T00:00", ago=None
    )
    assert sel is None


def test_date_only_raises(ns, conn):
    with pytest.raises(ValueError, match="requires HH:MM"):
        ns["_resolve_block_selector"](conn, block_start="2026-04-30", ago=None)


def test_block_start_and_ago_mutually_exclusive(ns, conn):
    with pytest.raises(ValueError, match="mutually exclusive"):
        ns["_resolve_block_selector"](
            conn, block_start="2026-04-30T10:30", ago=1
        )
