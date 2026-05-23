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
import os
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


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="chmod-0 unreadable injection has no effect when running as root",
)
def test_marker_withheld_when_a_file_read_fails_mid_walk(tmp_path, monkeypatch):
    """SAFETY (cctally-dev#93, D5a): an INCOMPLETE walk must NOT write the
    walk-complete marker, even when cache 001 is applied. This is the
    ``walk_clean = False`` withhold property — the marker vouches for cache
    completeness, so a per-file error-skip must veto it.

    We inject a real per-file failure: one JSONL file is made unreadable
    (``os.chmod(path, 0)``), which lets ``jp.stat()`` succeed but trips the
    ``open(...)`` read-OSError branch (``walk_clean = False`` at the read-fail
    skip). A second, fully-readable file ingests alongside it so the loop
    actually runs and 001 is applied — proving the withhold is driven by the
    unclean walk, not by an empty/uninteresting walk. The positive control is
    ``test_marker_written_after_clean_walk_when_001_applied`` above: the SAME
    fixture writes the marker when every file is readable.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    proj = tmp_path / ".claude" / "projects" / "-Users-u-demo"
    proj.mkdir(parents=True)
    good = proj / "good.jsonl"
    bad = proj / "bad.jsonl"
    good.write_text(_assistant_line("m_good", "r_good"))
    bad.write_text(_assistant_line("m_bad", "r_bad"))

    conn = ns["open_cache_db"]()  # applies cache 001 -> applied_at_start True
    try:
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name='001_dedup_highest_wins'"
        ).fetchone() is not None  # precondition: 001 applied (gate would otherwise hide)

        os.chmod(bad, 0)  # unreadable -> open() raises PermissionError (OSError)
        try:
            stats = ns["sync_cache"](conn)
        finally:
            # Restore readability so pytest's tmp_path teardown can unlink it.
            os.chmod(bad, 0o644)

        # The good file still ingested (loop ran), proving this is an unclean
        # walk rather than an empty one.
        assert conn.execute(
            "SELECT 1 FROM session_entries WHERE msg_id='m_good'"
        ).fetchone() is not None
        # The bad file contributed nothing.
        assert conn.execute(
            "SELECT 1 FROM session_entries WHERE msg_id='m_bad'"
        ).fetchone() is None
        # Marker is WITHHELD: the read-fail flipped walk_clean = False.
        assert conn.execute(
            "SELECT 1 FROM cache_meta WHERE key=?", (MARKER,)
        ).fetchone() is None, (
            "incomplete walk (a file read-failed) must NOT write the "
            "walk-complete marker"
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 5b‴ — Selective per-range source loss recomputes from surviving source
#       (R5); no body-level guard (spec D7). The gate PROCEEDs because the
#       cache is complete + non-empty OVERALL; a week whose JSONL was fully
#       pruned recomputes to $0 (no preserve-guard), while a week whose
#       JSONL survived recomputes to its corrected value. This locks in the
#       D7-removal decision: 008 must NOT preserve a zero-entry range.
# --------------------------------------------------------------------------


def _stage_stats_two_weeks(stats_path):
    """Stage stats.db with two auto/no-project weekly snapshots.

    W1 [2026-05-08, 2026-05-15] — JSONL survives; cost recomputes.
    W2 [2026-05-15, 2026-05-22] — JSONL fully pruned; recomputes to $0.
    Both seeded with a large stale pre-fix cost so a no-op would be visible.
    Returns (w1_id, w2_id).
    """
    stats = sqlite3.connect(stats_path)
    try:
        stats.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
            );
            CREATE TABLE weekly_cost_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                week_start_date TEXT NOT NULL,
                week_end_date TEXT NOT NULL,
                week_start_at TEXT,
                week_end_at TEXT,
                range_start_iso TEXT,
                range_end_iso TEXT,
                cost_usd REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'cctally-range-cost',
                mode TEXT NOT NULL DEFAULT 'auto',
                project TEXT
            );
            """
        )
        w1 = stats.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " range_start_iso, range_end_iso, cost_usd, mode, project) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "2026-05-15T00:00:00Z", "2026-05-08", "2026-05-15",
                "2026-05-08T00:00:00Z", "2026-05-15T00:00:00Z",
                99.99, "auto", None,
            ),
        ).lastrowid
        w2 = stats.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " range_start_iso, range_end_iso, cost_usd, mode, project) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "2026-05-22T00:00:00Z", "2026-05-15", "2026-05-22",
                "2026-05-15T00:00:00Z", "2026-05-22T00:00:00Z",
                88.88, "auto", None,
            ),
        ).lastrowid
        stats.commit()
        return w1, w2
    finally:
        stats.close()


def test_selective_prune_recomputes_surviving_and_zeros_pruned(
    tmp_path, monkeypatch,
):
    """W1 (JSONL present) recomputes to its corrected cost; W2 (JSONL fully
    pruned, zero in-range session_entries) recomputes to $0. The gate
    PROCEEDs because the cache carries the walk-complete marker AND is
    non-empty overall — selective per-range loss is accepted
    recompute-from-source behavior (R5), not blocked by any body guard.
    """
    db = _load_db()
    core = db._cctally_core

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    w1_id, w2_id = _stage_stats_two_weeks(stats_path)

    # cache.db: the new gate PROCEED signal is the cache_meta marker +
    # non-empty session_entries (NOT a session_files last_ingested_at
    # proof). Seed one entry inside W1's window ONLY; W2's range has none
    # (its JSONL was pruned), so W2's recompute sums to $0.
    cache = sqlite3.connect(cache_path)
    db._apply_cache_schema(cache)
    cache.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    cache.execute(
        "INSERT INTO schema_migrations VALUES ('001_dedup_highest_wins', ?)",
        ("2026-05-01T00:00:00Z",),
    )
    cache.execute(
        "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
        (MARKER, "2026-05-23T00:00:00Z"),
    )
    cache.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, "
        " input_tokens, output_tokens, cache_create_tokens, "
        " cache_read_tokens, usage_extra_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "/tmp/session1.jsonl", 0, "2026-05-10T00:00:00Z",
            "claude-opus-4-7", 0, 1000, 0, 0, "{}",
        ),
    )
    cache.commit()
    cache.close()

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    core._init_paths_from_env()

    # A populated projects dir so disk_state="jsonl_present" and the gate
    # reaches the complete+non-empty PROCEED (row 6).
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()
    (fake_projects / "session1.jsonl").write_text("{}\n")
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)
    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)

        w1_cost = stats.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots WHERE id=?", (w1_id,),
        ).fetchone()[0]
        w2_cost = stats.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots WHERE id=?", (w2_id,),
        ).fetchone()[0]

        # W1: 1000 opus-4-7 output tokens at $25/Mtok = $0.025 (corrected
        # from the stale 99.99).
        assert w1_cost == pytest.approx(0.025, abs=1e-9), (
            f"surviving-source week must recompute to its corrected cost; "
            f"got {w1_cost!r}"
        )
        # W2: zero in-range entries -> $0 (no preserve-guard; D7 removed).
        assert w2_cost == pytest.approx(0.0, abs=1e-9), (
            f"fully-pruned week must recompute to $0 (R5 / no body guard); "
            f"got {w2_cost!r}"
        )

        marker = stats.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name='008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
        assert marker is not None, "008 marker must stamp on the PROCEED path"
    finally:
        stats.close()
