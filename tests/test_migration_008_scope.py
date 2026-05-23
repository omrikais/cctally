"""Scope tests for stats migration 008: rows with mode='display' or
project IS NOT NULL must NOT be modified.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3.
Acceptance row: "mode='display' and project IS NOT NULL snapshot rows
are NOT modified" — this file pins the scope guard.
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
    """Load bin/_cctally_db.py via SourceFileLoader.

    Matches the pattern used by tests/test_migration_001_per_migration_goldens.py
    so the production handler is exercised verbatim (no copy-paste drift).
    """
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    # Drop cached sibling modules so a fresh load picks up any in-flight
    # edits to _cctally_db.py / _cctally_core.py within the same session.
    for _name in [
        n for n in list(sys.modules)
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]:
        del sys.modules[_name]
    spec = _ilu.spec_from_file_location("_cctally_db", BIN_DIR / "_cctally_db.py")
    mod = _ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_core(db_mod):
    """Return the SAME ``_cctally_core`` module instance that the loaded
    ``_cctally_db`` module is using.

    The migration reads ``_cctally_core.CACHE_DB_PATH`` /
    ``_cctally_core.CLAUDE_PROJECTS_DIR`` via the module attribute, so the
    test must ``monkeypatch.setattr`` against THAT module instance (not a
    separately-loaded copy). Loading core fresh would create a parallel
    instance with no effect on the migration's lookup.
    """
    return db_mod._cctally_core


def _pin_resolver_to_fake_home(core, tmp_path, monkeypatch):
    """Redirect HOME and clear ``CLAUDE_CONFIG_DIR`` so the resolver
    (``_resolve_claude_projects_dirs``) returns ``[]`` — the migration's
    Layer C path then falls back to scanning the test's monkeypatched
    ``CLAUDE_PROJECTS_DIR`` (single-rooted documented default).

    Migration 008 resolves projects/ dirs at call time via the env-aware
    resolver. On the developer's machine, real ``~/.claude/projects``
    likely exists with real JSONL — without this redirection, the
    resolver would pick it up and the test's "no JSONL on disk" or
    "JSONL at fixture path" assumptions would silently break.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    core._init_paths_from_env()


def _stage_stats(stats_path: pathlib.Path) -> None:
    """Stage stats.db with 3 rows: one auto/no-project (should recompute),
    one mode=display (must NOT change), one project='myproj' (must NOT change).
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
        stats.executemany(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " range_start_iso, range_end_iso, cost_usd, mode, project) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                # row 1: auto + project IS NULL → recomputed
                (
                    "2026-05-22T00:00:00Z", "2026-05-15", "2026-05-22",
                    "2026-05-15T00:00:00Z", "2026-05-22T00:00:00Z",
                    100.0, "auto", None,
                ),
                # row 2: mode='display' → must NOT change (user-supplied cost)
                (
                    "2026-05-22T00:00:00Z", "2026-05-15", "2026-05-22",
                    "2026-05-15T00:00:00Z", "2026-05-22T00:00:00Z",
                    999.0, "display", None,
                ),
                # row 3: project='myproj' → must NOT change (per-project scoped)
                (
                    "2026-05-22T00:00:00Z", "2026-05-15", "2026-05-22",
                    "2026-05-15T00:00:00Z", "2026-05-22T00:00:00Z",
                    50.0, "auto", "myproj",
                ),
            ],
        )
        stats.commit()
    finally:
        stats.close()


def _stage_cache(
    cache_path: pathlib.Path, applied_at_utc: str = "2026-05-22T00:00:00Z"
) -> None:
    """Stage cache.db with the 001 marker + one post-001 session_files row
    + one session_entry inside the week range.

    Entry: claude-opus-4-7, output_tokens=1000 → $25/Mtok * 1000 = $0.025.
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
            ("001_dedup_highest_wins", applied_at_utc),
        )
        # session_files: last_ingested_at strictly AFTER the 001 marker so
        # the post-001 ingest gate (Layer B) passes.
        cache.execute(
            "INSERT INTO session_files VALUES (?,?,?,?,?,?,?)",
            (
                "/tmp/session1.jsonl", 100, 0, 100,
                "2026-05-22T01:00:00Z", "s1", "/tmp/proj",
            ),
        )
        # One entry inside [2026-05-15, 2026-05-22) — opus-4-7 with
        # 1000 output tokens → $0.025 at $25/Mtok output rate.
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


def test_008_skips_mode_display_and_project_scoped_rows(tmp_path, monkeypatch):
    """A snapshot row with mode='display' OR project IS NOT NULL must keep
    its cost_usd value verbatim after migration 008 runs."""
    db = _load_db()
    core = _load_core(db)

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats(stats_path)
    _stage_cache(cache_path)

    # Patch CACHE_DB_PATH + CLAUDE_PROJECTS_DIR so the migration finds
    # the fixture cache.db and the gate's empty-disk fallback doesn't
    # short-circuit (we want Layer B — post-001 ingest — to pass).
    _pin_resolver_to_fake_home(core, tmp_path, monkeypatch)
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()
    (fake_projects / "session1.jsonl").write_text("{}\n")

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)

    # Run the migration handler directly against the staged stats.db.
    stats = sqlite3.connect(stats_path)
    try:
        db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)

        rows = stats.execute(
            "SELECT mode, project, cost_usd FROM weekly_cost_snapshots "
            "ORDER BY id"
        ).fetchall()
    finally:
        stats.close()

    # row 1 (auto, project IS NULL): recomputed from the seeded session_entry.
    assert rows[0] == ("auto", None, pytest.approx(0.025, abs=1e-9))
    # row 2 (display): unchanged — calculate-time cost preserved.
    assert rows[1] == ("display", None, 999.0)
    # row 3 (auto, project='myproj'): unchanged — per-project scoped snapshot.
    assert rows[2] == ("auto", "myproj", 50.0)


def test_008_skips_rows_with_null_range_columns(tmp_path, monkeypatch):
    """Legacy rows where range_start_iso/range_end_iso are NULL keep their
    pre-fix cost_usd value. Spec §I3 / CHANGELOG calls this out."""
    db = _load_db()
    core = _load_core(db)

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_cache(cache_path)

    # Stage a stats.db with one legacy auto/no-project row whose
    # range_*_iso columns are NULL.
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
        stats.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " range_start_iso, range_end_iso, cost_usd, mode, project) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "2026-05-22T00:00:00Z", "2026-05-15", "2026-05-22",
                None, None, 77.0, "auto", None,
            ),
        )
        stats.commit()
    finally:
        stats.close()

    _pin_resolver_to_fake_home(core, tmp_path, monkeypatch)
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()
    (fake_projects / "session1.jsonl").write_text("{}\n")

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)

    stats = sqlite3.connect(stats_path)
    try:
        db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)
        row = stats.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots"
        ).fetchone()
        marker = stats.execute(
            "SELECT name FROM schema_migrations "
            "WHERE name = '008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
    finally:
        stats.close()

    # cost_usd unchanged (legacy row skipped).
    assert row == (77.0,)
    # Marker stamped (the migration still completes; it's the row that's
    # skipped, not the migration as a whole).
    assert marker is not None


def test_008_gate_defers_when_post_001_ingest_missing(tmp_path, monkeypatch):
    """MigrationGateNotMet when 001 marker present but no post-001 ingest
    AND at least one JSONL file exists on disk (so the empty-disk fallback
    can't short-circuit). Verifies the composite gate's Layer B failure
    path."""
    db = _load_db()
    core = _load_core(db)

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats(stats_path)

    # Stage cache with 001 marker but NO post-001 session_files row.
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

    # JSONL exists on disk so empty-disk fallback can't pass the gate.
    _pin_resolver_to_fake_home(core, tmp_path, monkeypatch)
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()
    (fake_projects / "session1.jsonl").write_text("{}\n")

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)

    stats = sqlite3.connect(stats_path)
    try:
        with pytest.raises(db.MigrationGateNotMet):
            db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)
    finally:
        stats.close()


def test_008_gate_passes_with_zero_jsonl(tmp_path, monkeypatch):
    """With 001 marker present, no post-001 ingest, AND no JSONL files,
    the empty-disk fallback succeeds — gate passes and the migration
    completes as a no-op (no rows to recompute, marker stamped)."""
    db = _load_db()
    core = _load_core(db)

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"

    # Stage stats with no recompute-eligible rows so the no-op is clean.
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

    # Cache: 001 marker present, no session_files.
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

    # Empty projects dir → empty-disk fallback succeeds.
    _pin_resolver_to_fake_home(core, tmp_path, monkeypatch)
    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)
    # CLAUDE_PROJECTS_DIR is the documented default — point it at an
    # empty dir so the resolver's fallback (when nothing else exists)
    # also has no JSONL.
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)

    stats = sqlite3.connect(stats_path)
    try:
        db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)
        marker = stats.execute(
            "SELECT name FROM schema_migrations "
            "WHERE name = '008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
    finally:
        stats.close()

    assert marker is not None, (
        "migration 008 should stamp its marker even when there are no "
        "eligible rows to recompute"
    )


def test_008_gate_defers_when_001_marker_missing(tmp_path, monkeypatch):
    """MigrationGateNotMet when cache.db has no 001 marker at all. Layer A
    failure path."""
    db = _load_db()
    core = _load_core(db)

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats(stats_path)

    # Cache with empty schema_migrations.
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
        cache.commit()
    finally:
        cache.close()

    _pin_resolver_to_fake_home(core, tmp_path, monkeypatch)
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)

    stats = sqlite3.connect(stats_path)
    try:
        with pytest.raises(db.MigrationGateNotMet):
            db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)
    finally:
        stats.close()


def test_008_gate_honors_claude_config_dir(tmp_path, monkeypatch):
    """``CLAUDE_CONFIG_DIR`` users keep their JSONL outside
    ``~/.claude/projects``. Migration 008's gate must consult the
    env-aware resolver, otherwise Layer C falsely fires "no JSONL on disk"
    → empty-disk fallback passes → migration runs as a no-op against an
    empty session_entries → marker stamps → weekly_cost_snapshots never
    gets recomputed for those users.

    Topology:
      * ``CLAUDE_CONFIG_DIR`` set to ``tmp_path/alt_root``; its
        ``projects/`` dir contains a JSONL file.
      * ``~/.claude/projects`` (via fake HOME) does NOT exist.
      * Cache has the 001 marker but NO post-001 ingest row → Layer B
        fails.
      * With the env-aware resolver, Layer C sees the JSONL at the
        configured path → empty-disk fallback does NOT fire → gate
        raises ``MigrationGateNotMet``.
      * Pre-fix (hardcoded ``CLAUDE_PROJECTS_DIR``), Layer C would have
        seen no JSONL → empty-disk fallback fires → gate falsely
        passes → the regression we want to catch.
    """
    db = _load_db()
    core = _load_core(db)

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats(stats_path)

    # Cache with 001 marker, NO post-001 session_files row.
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

    # Alt-root JSONL location (CLAUDE_CONFIG_DIR target).
    alt_root = tmp_path / "alt_root"
    (alt_root / "projects").mkdir(parents=True)
    (alt_root / "projects" / "session1.jsonl").write_text("{}\n")

    # Fake HOME with NO ~/.claude/projects dir on disk.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt_root))

    # Re-init core's path globals so CLAUDE_PROJECTS_DIR reflects the
    # fake HOME (it'll point at a nonexistent dir; the test depends on
    # the resolver finding the alt-root location instead).
    core._init_paths_from_env()

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        # With the env-aware resolver, Layer C sees JSONL at the
        # configured path → empty-disk fallback does NOT fire → gate
        # raises (correct behavior). Pre-fix would have passed silently.
        with pytest.raises(db.MigrationGateNotMet, match="post-001 ingest"):
            db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)

        # Belt-and-suspenders: the resolver itself should report the
        # alt-root as a projects/ dir.
        resolved = core._resolve_claude_projects_dirs()
        assert resolved == [alt_root / "projects"], (
            f"resolver did not honor CLAUDE_CONFIG_DIR: {resolved}"
        )
    finally:
        stats.close()


def test_008_defers_when_cache_db_path_missing(tmp_path, monkeypatch):
    """G4 — when ``CACHE_DB_PATH`` does NOT exist on disk, ``sqlite3.connect``
    (RO URI form) raises ``SQLITE_CANTOPEN``. The migration body must
    translate this to ``MigrationGateNotMet`` via the
    ``_is_transient_sqlite_error`` predicate, NOT let the
    ``OperationalError`` escape to the dispatcher's ``except Exception``
    (which would log to ``migration-errors.log`` and render the migration
    error banner for a self-healing condition).
    """
    db = _load_db()
    core = _load_core(db)

    stats_path = tmp_path / "stats.db"
    _stage_stats(stats_path)

    # CACHE_DB_PATH points at a non-existent file.
    missing_cache = tmp_path / "does_not_exist.cache.db"
    assert not missing_cache.exists()

    _pin_resolver_to_fake_home(core, tmp_path, monkeypatch)
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()

    monkeypatch.setattr(core, "CACHE_DB_PATH", missing_cache)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)

    stats = sqlite3.connect(stats_path)
    try:
        with pytest.raises(db.MigrationGateNotMet):
            db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)
    finally:
        stats.close()
