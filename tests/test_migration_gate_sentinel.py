"""cache_meta walk-complete sentinel: write + atomic clears (cctally-dev#93, D5).

The 001-clear path is exercised directly against the migration handler; the
sync_cache write/withhold paths are driven end-to-end through the full
``cctally`` namespace (conftest ``load_script`` + ``redirect_paths``) because
``sync_cache`` resolves ``_cctally()`` / project discovery at call time.

Use ``TZ=Etc/UTC`` when running these (CLAUDE.md fixture rule); the tests
themselves are tz-agnostic (marker presence, not timestamp value, is asserted).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BIN_DIR = _ROOT / "bin"
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))

MARKER = "claude_ingest_walk_complete"


# --------------------------------------------------------------------------
# 001-clear: direct handler invocation against an in-memory cache.db
# --------------------------------------------------------------------------


def _load_db():
    """Load _cctally_db.py freshly (registers migration handlers once)."""
    for n in [n for n in sys.modules if n.startswith("_cctally_") and n != "_cctally_core"]:
        del sys.modules[n]
    spec = importlib.util.spec_from_file_location("_cctally_db", _BIN_DIR / "_cctally_db.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["_cctally_db"] = m
    spec.loader.exec_module(m)
    return m


def _seed_cache(db, tmp_path):
    conn = sqlite3.connect(tmp_path / "cache.db")
    db._apply_cache_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name           TEXT PRIMARY KEY,
            applied_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schema_migrations_skipped (
            name           TEXT PRIMARY KEY,
            skipped_at_utc TEXT NOT NULL,
            reason         TEXT
        );
        """
    )
    return conn


def test_cache_001_clears_marker(tmp_path):
    db = _load_db()
    conn = _seed_cache(db, tmp_path)
    conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                 (MARKER, "2026-01-01T00:00:00+00:00"))
    conn.execute("INSERT INTO session_entries(source_path, line_offset, timestamp_utc, model) "
                 "VALUES ('p', 0, '2026-01-01T00:00:00+00:00', 'claude-x')")
    conn.commit()
    db._001_dedup_highest_wins(conn)  # runs its own BEGIN IMMEDIATE + commit
    assert conn.execute("SELECT 1 FROM cache_meta WHERE key=?", (MARKER,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM session_entries LIMIT 1").fetchone() is None  # wiped too


# --------------------------------------------------------------------------
# sync_cache write / withhold: end-to-end through the full cctally namespace
# --------------------------------------------------------------------------


def _assistant_line(msg_id, req_id, *, ts="2026-01-01T00:00:00Z"):
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "requestId": req_id,
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
        },
    }) + "\n"


@pytest.fixture
def driver(tmp_path, monkeypatch):
    """Return (ns, conn) wired to a tmp HOME with one synthetic JSONL file."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    proj = tmp_path / ".claude" / "projects" / "-Users-u-demo"
    proj.mkdir(parents=True)
    (proj / "sess.jsonl").write_text(_assistant_line("m1", "r1"))
    conn = ns["open_cache_db"]()  # applies cache 001 + creates cache_meta
    yield ns, conn
    try:
        conn.close()
    except Exception:
        pass


def test_marker_written_after_clean_walk_when_001_applied(driver):
    """open_cache_db applies 001 in-process, so applied_at_start is True; a
    clean walk over a real JSONL writes the walk-complete marker."""
    ns, conn = driver
    assert conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name='001_dedup_highest_wins'"
    ).fetchone() is not None  # precondition: 001 applied by open_cache_db
    ns["sync_cache"](conn)
    assert conn.execute("SELECT 1 FROM cache_meta WHERE key=?", (MARKER,)).fetchone() is not None
    # The walk actually ingested the entry (sanity).
    assert conn.execute("SELECT 1 FROM session_entries LIMIT 1").fetchone() is not None


def test_marker_withheld_when_001_not_applied_at_start(driver):
    """If 001 is NOT applied when sync_cache starts, applied_at_start is False
    and the marker is withheld even on a clean walk (D5b straddle guard)."""
    ns, conn = driver
    # Simulate the pre-001 baseline: remove the 001 stamp before sync.
    conn.execute("DELETE FROM schema_migrations WHERE name='001_dedup_highest_wins'")
    conn.commit()
    ns["sync_cache"](conn)
    assert conn.execute("SELECT 1 FROM cache_meta WHERE key=?", (MARKER,)).fetchone() is None


def test_rebuild_clears_then_rewrites_marker(driver):
    """A rebuild clears the marker atomically with the wipe, then the same
    run's clean walk re-establishes it (the marker tracks THIS walk)."""
    ns, conn = driver
    ns["sync_cache"](conn)  # establish marker
    assert conn.execute("SELECT 1 FROM cache_meta WHERE key=?", (MARKER,)).fetchone() is not None
    ns["sync_cache"](conn, rebuild=True)  # wipes + clears + re-walks + rewrites
    # After a clean rebuild walk over surviving JSONL, the marker is present again.
    assert conn.execute("SELECT 1 FROM cache_meta WHERE key=?", (MARKER,)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM session_entries LIMIT 1").fetchone() is not None


def test_unchanged_file_early_exit_does_not_withhold_marker(driver):
    """A confirmed-current file (size == prev_size early-exit) still counts as
    walked: a second clean sync over unchanged files keeps the marker."""
    ns, conn = driver
    ns["sync_cache"](conn)  # first walk ingests + writes marker
    # Second walk: file unchanged -> files_skipped_unchanged path, no error.
    stats = ns["sync_cache"](conn)
    assert stats.files_skipped_unchanged >= 1
    assert conn.execute("SELECT 1 FROM cache_meta WHERE key=?", (MARKER,)).fetchone() is not None
