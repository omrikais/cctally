"""Scope tests for stats migration 010: every percent_milestones row
gets its cumulative_cost_usd + marginal_cost_usd recomputed, with
edge cases for:

  * Multi-week histories — each week's marginal computation is scoped
    to that week.
  * Multi-segment under the v1.7.2 reset_event_id segment column — a
    pre-credit segment (event_id=0) and a post-credit segment for the
    same week + threshold coexist; marginal for the FIRST row of each
    segment equals cumulative.
  * Legacy rows with NULL ``week_start_at`` fall back to
    ``week_start_date`` at midnight UTC.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3 (B2).
"""
from __future__ import annotations

import importlib.util as _ilu
import pathlib
import sqlite3
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def _load_db():
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    for _name in [
        n for n in list(sys.modules)
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]:
        del sys.modules[_name]
    spec = _ilu.spec_from_file_location(
        "_cctally_db", BIN_DIR / "_cctally_db.py"
    )
    mod = _ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _pin_resolver_to_fake_home(core, tmp_path, monkeypatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    core._init_paths_from_env()


_STATS_DDL = """
CREATE TABLE schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at_utc TEXT
);
CREATE TABLE schema_migrations_skipped (
    name TEXT PRIMARY KEY,
    skipped_at_utc TEXT,
    reason TEXT
);
CREATE TABLE percent_milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    week_start_date TEXT NOT NULL,
    week_end_date TEXT NOT NULL,
    week_start_at TEXT,
    week_end_at TEXT,
    percent_threshold INTEGER NOT NULL,
    cumulative_cost_usd REAL NOT NULL,
    marginal_cost_usd REAL,
    usage_snapshot_id INTEGER NOT NULL,
    cost_snapshot_id INTEGER NOT NULL,
    reset_event_id INTEGER NOT NULL DEFAULT 0,
    five_hour_percent_at_crossing REAL,
    alerted_at TEXT,
    UNIQUE(week_start_date, percent_threshold, reset_event_id)
);
"""

_CACHE_DDL = """
CREATE TABLE schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at_utc TEXT
);
CREATE TABLE schema_migrations_skipped (
    name TEXT PRIMARY KEY,
    skipped_at_utc TEXT,
    reason TEXT
);
CREATE TABLE session_files (
    path TEXT PRIMARY KEY,
    size_bytes INTEGER,
    mtime_ns INTEGER,
    last_byte_offset INTEGER,
    last_ingested_at TEXT,
    session_id TEXT,
    project_path TEXT
);
CREATE TABLE session_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT,
    line_offset INTEGER,
    timestamp_utc TEXT,
    model TEXT,
    msg_id TEXT,
    req_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_create_tokens INTEGER,
    cache_read_tokens INTEGER,
    usage_extra_json TEXT,
    cost_usd_raw REAL
);
CREATE TABLE cache_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _setup_paths(db, core, tmp_path, monkeypatch):
    _pin_resolver_to_fake_home(core, tmp_path, monkeypatch)
    projects = tmp_path / "claude_projects"
    projects.mkdir()
    (projects / "x.jsonl").write_text("{}\n")
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", projects)


def _stage_cache_with_entries(cache_path, applied_at, entries):
    conn = sqlite3.connect(cache_path)
    try:
        conn.executescript(_CACHE_DDL)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            ("001_dedup_highest_wins", applied_at),
        )
        # cache_meta walk-complete marker: the gate's PROCEED signal now
        # (cctally-dev#93). Paired with the non-empty session_entries
        # seeded below.
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('claude_ingest_walk_complete', '2026-05-22T02:00:00Z')"
        )
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, "
            " last_ingested_at, session_id, project_path) "
            "VALUES ('/tmp/session1.jsonl', 100, 0, 100, ?, 's', '/tmp/p')",
            ("2026-05-22T02:00:00Z",),
        )
        for line, ts, out in entries:
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, usage_extra_json) "
                "VALUES (?, ?, ?, 'claude-opus-4-7', 0, ?, 0, 0, '{}')",
                ("/tmp/session1.jsonl", line, ts, out),
            )
        conn.commit()
    finally:
        conn.close()


def test_010_multi_week_marginal_scoped_per_week(tmp_path, monkeypatch):
    """Marginal computation MUST scope to the same week. Week B's first
    milestone (threshold=5) has marginal = cumulative even though
    Week A's milestones at threshold=1 + threshold=5 already advanced
    the global cumulative.
    """
    db = _load_db()
    core = db._cctally_core
    _setup_paths(db, core, tmp_path, monkeypatch)

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        stats.executescript(_STATS_DDL)
        # Week A (2026-05-08 → 05-15), thresholds 1 & 5.
        # Week B (2026-05-15 → 05-22), threshold 5.
        for captured, week, week_start_at, threshold, cum, marg in (
            (
                "2026-05-09T10:00:00Z", "2026-05-08",
                "2026-05-08T00:00:00+00:00", 1, 99.0, 99.0,
            ),
            (
                "2026-05-09T11:00:00Z", "2026-05-08",
                "2026-05-08T00:00:00+00:00", 5, 99.0, 0.0,
            ),
            (
                "2026-05-16T10:00:00Z", "2026-05-15",
                "2026-05-15T00:00:00+00:00", 5, 99.0, 0.0,
            ),
        ):
            stats.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    captured, week,
                    (
                        "2026-05-15" if week == "2026-05-08"
                        else "2026-05-22"
                    ),
                    week_start_at,
                    (
                        "2026-05-15T00:00:00+00:00"
                        if week == "2026-05-08"
                        else "2026-05-22T00:00:00+00:00"
                    ),
                    threshold, cum, marg, 1, 1, 0,
                ),
            )
        stats.commit()
    finally:
        stats.close()

    _stage_cache_with_entries(
        cache_path,
        applied_at="2026-05-22T00:00:00Z",
        entries=[
            # Week A: one entry before threshold=1 captured.
            (0, "2026-05-09T09:00:00Z", 1000),  # $0.025
            # Week A: another entry before threshold=5 captured.
            (1, "2026-05-09T10:30:00Z", 1000),  # $0.050 cumulative
            # Week B: one entry before threshold=5 captured.
            (2, "2026-05-16T09:00:00Z", 1000),  # $0.025 cumulative for B
        ],
    )

    stats = sqlite3.connect(stats_path)
    try:
        db._010_recompute_percent_milestones_dedup_fix(stats)
        rows = list(stats.execute(
            "SELECT week_start_date, percent_threshold, "
            "       cumulative_cost_usd, marginal_cost_usd "
            "FROM percent_milestones "
            "ORDER BY week_start_date, percent_threshold"
        ).fetchall())
    finally:
        stats.close()

    # Week A threshold=1: cumulative = $0.025 (one entry by then),
    # marginal = cumulative (first of week).
    assert rows[0] == (
        "2026-05-08", 1,
        pytest.approx(0.025, abs=1e-9),
        pytest.approx(0.025, abs=1e-9),
    )
    # Week A threshold=5: cumulative = $0.050, marginal = $0.025.
    assert rows[1] == (
        "2026-05-08", 5,
        pytest.approx(0.050, abs=1e-9),
        pytest.approx(0.025, abs=1e-9),
    )
    # Week B threshold=5: cumulative = $0.025, marginal = cumulative
    # (first row of THIS week, NOT marginal vs Week A).
    assert rows[2] == (
        "2026-05-15", 5,
        pytest.approx(0.025, abs=1e-9),
        pytest.approx(0.025, abs=1e-9),
    )


def test_010_multi_segment_marginal_resets_per_segment(
    tmp_path, monkeypatch,
):
    """In a credited week with two segments (pre-credit event_id=0,
    post-credit event_id=42), the FIRST threshold in each segment has
    marginal = cumulative. The segments are independent for marginal
    computation — segment-1 threshold=10 is NOT 'prior' to segment-42
    threshold=10.
    """
    db = _load_db()
    core = db._cctally_core
    _setup_paths(db, core, tmp_path, monkeypatch)

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        stats.executescript(_STATS_DDL)
        # One week, two segments.
        for captured, threshold, event_id in (
            ("2026-05-16T10:00:00Z", 10, 0),    # pre-credit
            ("2026-05-17T10:00:00Z", 20, 0),    # pre-credit
            ("2026-05-19T10:00:00Z", 10, 42),   # post-credit
            ("2026-05-20T10:00:00Z", 20, 42),   # post-credit
        ):
            stats.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, '2026-05-15', '2026-05-22', "
                "        '2026-05-15T00:00:00+00:00', "
                "        '2026-05-22T00:00:00+00:00', "
                "        ?, 99.0, NULL, 1, 1, ?)",
                (captured, threshold, event_id),
            )
        stats.commit()
    finally:
        stats.close()

    _stage_cache_with_entries(
        cache_path,
        applied_at="2026-05-22T00:00:00Z",
        entries=[
            (0, "2026-05-16T08:00:00Z", 1000),  # before seg0/th10
            (1, "2026-05-17T08:00:00Z", 1000),  # before seg0/th20
            (2, "2026-05-19T08:00:00Z", 1000),  # before seg42/th10
            (3, "2026-05-20T08:00:00Z", 1000),  # before seg42/th20
        ],
    )

    stats = sqlite3.connect(stats_path)
    try:
        db._010_recompute_percent_milestones_dedup_fix(stats)
        rows = list(stats.execute(
            "SELECT reset_event_id, percent_threshold, "
            "       cumulative_cost_usd, marginal_cost_usd "
            "FROM percent_milestones "
            "ORDER BY reset_event_id, percent_threshold"
        ).fetchall())
    finally:
        stats.close()

    # seg=0 th=10: cumulative=$0.025 (1 entry), marginal=cumulative.
    assert rows[0] == (
        0, 10,
        pytest.approx(0.025, abs=1e-9),
        pytest.approx(0.025, abs=1e-9),
    )
    # seg=0 th=20: cumulative=$0.050 (2 entries), marginal=$0.025.
    assert rows[1] == (
        0, 20,
        pytest.approx(0.050, abs=1e-9),
        pytest.approx(0.025, abs=1e-9),
    )
    # seg=42 th=10: cumulative=$0.075 (3 entries by captured time),
    # marginal=cumulative (first of THIS segment, not relative to seg=0).
    assert rows[2] == (
        42, 10,
        pytest.approx(0.075, abs=1e-9),
        pytest.approx(0.075, abs=1e-9),
    )
    # seg=42 th=20: cumulative=$0.100 (4 entries), marginal=$0.025.
    assert rows[3] == (
        42, 20,
        pytest.approx(0.100, abs=1e-9),
        pytest.approx(0.025, abs=1e-9),
    )


def test_010_legacy_null_week_start_at_falls_back_to_date(
    tmp_path, monkeypatch,
):
    """Legacy rows with ``week_start_at IS NULL`` use ``week_start_date``
    at midnight UTC as the lower bound. Pre-v1.2 schemas only had the
    date column."""
    db = _load_db()
    core = db._cctally_core
    _setup_paths(db, core, tmp_path, monkeypatch)

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        stats.executescript(_STATS_DDL)
        stats.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
            "VALUES (?, '2026-05-15', '2026-05-22', "
            "        NULL, NULL, 5, 999.0, 999.0, 1, 1, 0)",
            ("2026-05-16T12:00:00Z",),
        )
        stats.commit()
    finally:
        stats.close()

    _stage_cache_with_entries(
        cache_path,
        applied_at="2026-05-22T00:00:00Z",
        entries=[
            # Before week_start_date midnight: must be EXCLUDED.
            (0, "2026-05-14T23:59:00Z", 1000),
            # Inside the [week_start_date midnight, captured_at] range:
            # MUST be included via the date-midnight fallback.
            (1, "2026-05-16T08:00:00Z", 1000),
        ],
    )

    stats = sqlite3.connect(stats_path)
    try:
        db._010_recompute_percent_milestones_dedup_fix(stats)
        row = stats.execute(
            "SELECT cumulative_cost_usd FROM percent_milestones"
        ).fetchone()
    finally:
        stats.close()

    # Only the in-range entry contributes — $0.025.
    assert row[0] == pytest.approx(0.025, abs=1e-9)
