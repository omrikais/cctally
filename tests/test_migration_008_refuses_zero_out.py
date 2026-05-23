"""G3 regression: migration 008 refuses to zero out historical
``weekly_cost_snapshots`` rows when ``_resolve_claude_projects_dirs()``
returns ``[]`` AND there are snapshot rows on the operator's stats.db.

Pre-fix the migration unconditionally fell back to
``[_cctally_core.CLAUDE_PROJECTS_DIR]`` when the resolver returned
``[]``. When THAT default ALSO didn't exist on disk (e.g.,
``CLAUDE_CONFIG_DIR`` pointed at a stale path AND ``~/.claude/projects``
was absent), the gate's Layer C empty-disk fallback fired, the
migration ran against an empty ``session_entries``, and silently
UPDATEd every ``mode='auto' AND project IS NULL`` snapshot to
``cost_usd = 0.0`` — destruction with no recovery path.

Post-fix: the migration raises ``MigrationGateNotMet`` (deferred by the
dispatcher) in this scenario. The truly-fresh-install case (no
projects dirs AND no snapshot rows) still completes cleanly as a no-op
so users with no Claude usage can still finish the upgrade.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3.
"""
from __future__ import annotations

import importlib.util as ilu
import pathlib
import sqlite3
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def _load_db():
    """Load bin/_cctally_db.py via SourceFileLoader."""
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    for _name in [
        n for n in list(sys.modules)
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]:
        del sys.modules[_name]
    spec = ilu.spec_from_file_location("_cctally_db", BIN_DIR / "_cctally_db.py")
    mod = ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _stage_stats_with_snapshots(stats_path: pathlib.Path) -> int:
    """Stage stats.db with one auto/no-project snapshot whose original
    cost_usd is meaningfully non-zero. Returns the snapshot id."""
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
        cur = stats.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " range_start_iso, range_end_iso, cost_usd, mode, project) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "2026-05-22T00:00:00Z", "2026-05-15", "2026-05-22",
                "2026-05-15T00:00:00Z", "2026-05-22T00:00:00Z",
                123.45, "auto", None,
            ),
        )
        snap_id = cur.lastrowid
        stats.commit()
        return snap_id
    finally:
        stats.close()


def _stage_stats_empty(stats_path: pathlib.Path) -> None:
    """Stage stats.db with the snapshot table but ZERO rows. Mirrors the
    'truly fresh install, no Claude usage yet' topology."""
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
        stats.commit()
    finally:
        stats.close()


def _stage_cache_with_001_marker(cache_path: pathlib.Path) -> None:
    """Stage cache.db with the 001 marker AND a post-001 session_files row
    so the gate's Layer A + Layer B both pass when the gate is reached.

    For the G3 scenarios we want the projects-dir resolution to be the
    failing signal, NOT some other gate layer.
    """
    cache = sqlite3.connect(cache_path)
    try:
        cache.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
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
                source_path TEXT, line_offset INTEGER, timestamp_utc TEXT,
                model TEXT, msg_id TEXT, req_id TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_create_tokens INTEGER, cache_read_tokens INTEGER,
                usage_extra_json TEXT, cost_usd_raw REAL
            );
            """
        )
        cache.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            ("001_dedup_highest_wins", "2026-05-22T00:00:00Z"),
        )
        # session_files row whose last_ingested_at strictly post-dates
        # the 001 marker so the gate's Layer B (post-001 ingest)
        # passes.
        cache.execute(
            "INSERT INTO session_files VALUES (?,?,?,?,?,?,?)",
            (
                "/tmp/session1.jsonl", 100, 0, 100,
                "2026-05-22T01:00:00Z", "s1", "/tmp/proj",
            ),
        )
        cache.commit()
    finally:
        cache.close()


def _stage_cache_without_post_001_ingest(cache_path: pathlib.Path) -> None:
    """Stage cache.db with the 001 marker but NO session_files row.

    For the fresh-install scenario: Layer A passes (marker present),
    Layer B fails (no post-001 ingest), Layer C should pass because no
    JSONL files exist anywhere → the migration completes as a no-op.
    """
    cache = sqlite3.connect(cache_path)
    try:
        cache.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
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
                source_path TEXT, line_offset INTEGER, timestamp_utc TEXT,
                model TEXT, msg_id TEXT, req_id TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_create_tokens INTEGER, cache_read_tokens INTEGER,
                usage_extra_json TEXT, cost_usd_raw REAL
            );
            """
        )
        cache.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            ("001_dedup_highest_wins", "2026-05-22T00:00:00Z"),
        )
        cache.commit()
    finally:
        cache.close()


def _stage_cache_with_session_entries_inside_window(cache_path: pathlib.Path) -> None:
    """Stage cache.db with the 001 marker, a post-001 session_files row,
    AND a session_entry inside [2026-05-15, 2026-05-22] so the happy
    path produces a non-zero recompute."""
    cache = sqlite3.connect(cache_path)
    try:
        cache.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
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
                source_path TEXT, line_offset INTEGER, timestamp_utc TEXT,
                model TEXT, msg_id TEXT, req_id TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_create_tokens INTEGER, cache_read_tokens INTEGER,
                usage_extra_json TEXT, cost_usd_raw REAL
            );
            """
        )
        cache.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            ("001_dedup_highest_wins", "2026-05-22T00:00:00Z"),
        )
        cache.execute(
            "INSERT INTO session_files VALUES (?,?,?,?,?,?,?)",
            (
                "/tmp/session1.jsonl", 100, 0, 100,
                "2026-05-22T01:00:00Z", "s1", "/tmp/proj",
            ),
        )
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, "
            " input_tokens, output_tokens, cache_create_tokens, "
            " cache_read_tokens, usage_extra_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "/tmp/session1.jsonl", 0, "2026-05-18T00:00:00Z",
                "claude-opus-4-7", 0, 1000, 0, 0, "{}",
            ),
        )
        cache.commit()
    finally:
        cache.close()


# ─── Scenario 1: snapshots populated, projects dir UNRESOLVABLE ──────


def test_008_refuses_to_zero_snapshots_when_no_projects_dir(
    tmp_path, monkeypatch
):
    """G3 scenario 1: snapshots present, ``CLAUDE_CONFIG_DIR`` set to a
    nonexistent path, ``~/.claude/projects`` ALSO absent → migration
    must raise ``MigrationGateNotMet`` and leave the snapshot's
    ``cost_usd`` untouched.

    Pre-fix this topology silently zeroed every auto/no-project
    snapshot — destruction with no recovery path.
    """
    db = _load_db()
    core = db._cctally_core

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    snap_id = _stage_stats_with_snapshots(stats_path)
    _stage_cache_with_001_marker(cache_path)

    # Fake HOME without ~/.claude/projects so the resolver's default
    # branch can't find anything either.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    # CLAUDE_CONFIG_DIR points at a path that doesn't exist on this
    # machine — the resolver's env branch returns [], and the default
    # branch also finds nothing (fake HOME has no .claude or .config).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nonexistent"))

    # Re-init core's path globals so CLAUDE_PROJECTS_DIR reflects fake
    # HOME (it'll point at a non-existent dir).
    core._init_paths_from_env()
    assert not core.CLAUDE_PROJECTS_DIR.is_dir(), (
        "test prep: CLAUDE_PROJECTS_DIR must NOT exist for G3 scenario "
        "1 to exercise the no-projects-dir path"
    )

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        with pytest.raises(db.MigrationGateNotMet, match="projects/"):
            db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)

        # Snapshot UNCHANGED — the migration refused to touch it.
        cost = stats.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots WHERE id = ?",
            (snap_id,),
        ).fetchone()[0]
        assert cost == pytest.approx(123.45, abs=1e-9), (
            f"migration 008 must NOT modify cost_usd when projects dir "
            f"is unresolved; got {cost!r}"
        )

        # Marker NOT stamped — migration deferred via gate.
        marker = stats.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name = '008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
        assert marker is None, "marker must NOT be stamped on gate-defer"
    finally:
        stats.close()


# ─── Scenario 2: no snapshots, no projects dir → safe no-op ──────────


def test_008_safe_noop_when_no_snapshots_and_no_projects_dir(
    tmp_path, monkeypatch
):
    """G3 scenario 2: truly fresh install — zero snapshot rows AND no
    resolvable projects dir. Migration must apply cleanly as a no-op
    (mark stamped, no rows changed). Gate-defer would loop forever on
    this topology, so the safe-noop branch is the correct outcome.
    """
    db = _load_db()
    core = db._cctally_core

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats_empty(stats_path)
    _stage_cache_without_post_001_ingest(cache_path)

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    core._init_paths_from_env()
    assert not core.CLAUDE_PROJECTS_DIR.is_dir(), (
        "test prep: CLAUDE_PROJECTS_DIR must NOT exist for G3 scenario "
        "2 to exercise the fresh-install fall-through"
    )

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        # Migration applies cleanly — no exception. The gate's Layer C
        # empty-disk fallback fires (the fall-through default
        # ``[CLAUDE_PROJECTS_DIR]`` is itself empty, so `any(p.glob(...))`
        # returns False).
        db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)

        # Marker stamped.
        marker = stats.execute(
            "SELECT name FROM schema_migrations "
            "WHERE name = '008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
        assert marker is not None, (
            "marker must be stamped on the fresh-install no-op path"
        )

        # Still zero rows in weekly_cost_snapshots (nothing to update).
        count = stats.execute(
            "SELECT COUNT(*) FROM weekly_cost_snapshots"
        ).fetchone()[0]
        assert count == 0
    finally:
        stats.close()


# ─── Scenario 3: normal happy path — preserved ───────────────────────


def test_008_happy_path_preserved(tmp_path, monkeypatch):
    """G3 scenario 3: normal happy path — snapshots populated, projects
    dir resolvable via the monkeypatched ``CLAUDE_PROJECTS_DIR`` (with
    a JSONL file inside) so the resolver-or-fallback chain finds it,
    cache populated with a session_entry inside the window. Migration
    runs to completion, snapshot's cost_usd is recomputed.

    Existing behavior — this test guards against the G3 fix
    over-tightening to break the production happy path.
    """
    db = _load_db()
    core = db._cctally_core

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    snap_id = _stage_stats_with_snapshots(stats_path)
    _stage_cache_with_session_entries_inside_window(cache_path)

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    core._init_paths_from_env()

    # Override CLAUDE_PROJECTS_DIR to a populated fixture dir. The
    # migration's defensive fallback (resolver returned [] BUT
    # CLAUDE_PROJECTS_DIR.is_dir() → use it) feeds the gate this path.
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()
    (fake_projects / "session1.jsonl").write_text("{}\n")
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)
    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)

        # cost_usd recomputed: 1000 opus-4-7 output tokens at $25/Mtok
        # = $0.025.
        cost = stats.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots WHERE id = ?",
            (snap_id,),
        ).fetchone()[0]
        assert cost == pytest.approx(0.025, abs=1e-9)

        marker = stats.execute(
            "SELECT name FROM schema_migrations "
            "WHERE name = '008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
        assert marker is not None
    finally:
        stats.close()
