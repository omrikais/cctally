"""R3 landmine regression — 009/010 drive the cache.db RO join through
the shared ``_apply_cache_schema`` helper via the eager-apply path
(cctally-dev#93, plan Task 7 Step 2 / spec test 5d).

The original ``no such column: sf.project_path`` landmine (Codex pre-plan
round 3, fix ``b45fccbf``) was a schema-mirror drift: the hand-curated
inline DDL subset that ``_eagerly_apply_cache_migrations`` carried lacked
``session_files.project_path``, while stats migration 009's read-only
cache.db join (``LEFT JOIN session_files sf ON sf.path = se.source_path``
selecting ``sf.project_path``) needed it. Column resolution happens at
*prepare* time even over zero rows, so the migration raised at the join.

cctally-dev#93 D4 collapsed the cache.db schema into one shared
``_apply_cache_schema(conn)`` helper that BOTH ``open_cache_db`` and
``_eagerly_apply_cache_migrations`` call, so the drift class cannot recur.
``_apply_cache_schema`` runs ``add_column_if_missing(conn, "session_files",
"project_path", "TEXT")`` — meaning a legacy cache.db FILE whose
``session_files`` lacks the column gets it added by the eager-apply path
BEFORE 009's RO join prepares.

The existing ``test_migration_008_eager_cache_trigger.py`` only covers
008 (which does not join ``sf.project_path``). This file pins the
landmine-can't-recur contract for **009** (the actual ``sf.project_path``
join) and **010** (which exercises the same eager-apply → ``_apply_cache_schema``
→ gate-evaluation path even though its own read is project-agnostic).

Topology per test: stage a cache.db FILE whose ``session_files`` is the
PRE-``project_path`` shape (and no ``cache_meta`` table either — the
purest legacy shape), pin ``CACHE_DB_PATH`` at it, then run the migration
handler. The handler's ``_open_cache_ro_with_gate_defer`` →
``_eagerly_apply_cache_migrations`` → ``_apply_cache_schema`` must
back-fill ``project_path`` (and create ``cache_meta``) so the subsequent
RO join + gate read resolve without ``no such column`` / ``no such table``.
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
    spec = _ilu.spec_from_file_location("_cctally_db", BIN_DIR / "_cctally_db.py")
    mod = _ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _pin_paths(core, tmp_path, monkeypatch, cache_path):
    """Pin HOME + CACHE_DB_PATH + a populated projects/ dir so the gate's
    disk_state classifies as ``jsonl_present`` and the eager-apply path
    targets our staged legacy cache.db FILE."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    core._init_paths_from_env()
    projects = tmp_path / "claude_projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")
    monkeypatch.setattr(core, "APP_DIR", tmp_path)
    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)
    monkeypatch.setattr(core, "CACHE_LOCK_PATH", pathlib.Path(f"{cache_path}.lock"))
    monkeypatch.setattr(
        core,
        "CACHE_LOCK_CODEX_PATH",
        pathlib.Path(f"{cache_path}.codex.lock"),
    )
    monkeypatch.setattr(
        core,
        "CACHE_LOCK_MAINTENANCE_PATH",
        pathlib.Path(f"{cache_path}.maintenance.lock"),
    )
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", projects)


def _stage_legacy_cache_without_project_path(cache_path: pathlib.Path) -> None:
    """Stage a cache.db FILE in the purest pre-cctally-dev#93 legacy shape:

      * ``session_files`` WITHOUT ``session_id`` / ``project_path``
        columns (the exact drift that caused the ``no such column:
        sf.project_path`` landmine);
      * NO ``cache_meta`` table (predates the walk-complete sentinel);
      * 001 already applied + a session_entry, so the eager-apply path is
        a fast-path no-op for the dispatcher but STILL re-applies the
        shared schema (back-filling ``project_path`` + creating
        ``cache_meta``).

    After ``_apply_cache_schema`` runs over this file, ``project_path``
    must exist (so 009's RO join prepares) and ``cache_meta`` must exist —
    but the walk-complete marker is ABSENT (no clean walk has run since
    001), so the gate would DEFER if there were historical rows to
    protect. The eager-apply ITSELF must not raise; that is the landmine
    surface this file pins. (A green PROCEED is covered separately by the
    009/010 scope tests, which seed the marker.)
    """
    conn = sqlite3.connect(cache_path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            );
            CREATE TABLE session_files (
                path             TEXT PRIMARY KEY,
                size_bytes       INTEGER NOT NULL,
                mtime_ns         INTEGER NOT NULL,
                last_byte_offset INTEGER NOT NULL,
                last_ingested_at TEXT    NOT NULL
            );
            CREATE TABLE session_entries (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path         TEXT    NOT NULL,
                line_offset         INTEGER NOT NULL,
                timestamp_utc       TEXT    NOT NULL,
                model               TEXT    NOT NULL,
                msg_id              TEXT,
                req_id              TEXT,
                input_tokens        INTEGER NOT NULL DEFAULT 0,
                output_tokens       INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
                usage_extra_json    TEXT,
                cost_usd_raw        REAL
            );
            """
        )
        # 001 applied so the dispatcher's fast-path doesn't re-wipe.
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            ("001_dedup_highest_wins", "2026-05-22T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at) "
            "VALUES ('/tmp/session1.jsonl', 100, 0, 100, '2026-05-22T01:00:00Z')"
        )
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, "
            " input_tokens, output_tokens, cache_create_tokens, "
            " cache_read_tokens, usage_extra_json) "
            "VALUES ('/tmp/session1.jsonl', 0, '2026-05-18T00:00:00Z', "
            " 'claude-opus-4-7', 0, 1000, 0, 0, '{}')"
        )
        conn.commit()
    finally:
        conn.close()


def _assert_project_path_and_cache_meta_backfilled(cache_path):
    """The eager-apply path's ``_apply_cache_schema`` must have added the
    ``project_path`` column and the ``cache_meta`` table to the FILE."""
    conn = sqlite3.connect(cache_path)
    try:
        sf_cols = {r[1] for r in conn.execute("PRAGMA table_info(session_files)")}
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert "project_path" in sf_cols, (
        "eager-apply must back-fill session_files.project_path via the "
        "shared _apply_cache_schema helper (R3 landmine guard); got "
        f"{sorted(sf_cols)}"
    )
    assert "cache_meta" in tables, (
        "eager-apply must create the cache_meta table via the shared "
        f"helper; got tables {sorted(tables)}"
    )


def _stage_stats_with_one_block(stats_path: pathlib.Path) -> None:
    """Stage stats.db with one closed five_hour_block for 009 to recompute."""
    conn = sqlite3.connect(stats_path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
            );
            CREATE TABLE five_hour_blocks (
                id                            INTEGER PRIMARY KEY AUTOINCREMENT,
                five_hour_window_key          INTEGER NOT NULL UNIQUE,
                five_hour_resets_at           TEXT    NOT NULL,
                block_start_at                TEXT    NOT NULL,
                first_observed_at_utc         TEXT    NOT NULL,
                last_observed_at_utc          TEXT    NOT NULL,
                final_five_hour_percent       REAL    NOT NULL,
                seven_day_pct_at_block_start  REAL,
                seven_day_pct_at_block_end    REAL,
                crossed_seven_day_reset       INTEGER NOT NULL DEFAULT 0,
                total_input_tokens            INTEGER NOT NULL DEFAULT 0,
                total_output_tokens           INTEGER NOT NULL DEFAULT 0,
                total_cache_create_tokens     INTEGER NOT NULL DEFAULT 0,
                total_cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
                total_cost_usd                REAL    NOT NULL DEFAULT 0,
                is_closed                     INTEGER NOT NULL DEFAULT 0,
                created_at_utc                TEXT    NOT NULL,
                last_updated_at_utc           TEXT    NOT NULL
            );
            CREATE TABLE five_hour_block_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id INTEGER NOT NULL,
                five_hour_window_key INTEGER NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                entry_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(five_hour_window_key, model)
            );
            CREATE TABLE five_hour_block_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id INTEGER NOT NULL,
                five_hour_window_key INTEGER NOT NULL,
                project_path TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                entry_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(five_hour_window_key, project_path)
            );
            """
        )
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(id, five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, total_output_tokens, total_cost_usd, "
            " is_closed, created_at_utc, last_updated_at_utc) "
            "VALUES (1, 1747000000, '2026-12-31T00:00:00+00:00', "
            " '2026-05-18T00:00:00+00:00', '2026-05-18T00:00:00+00:00', "
            " '2026-05-18T04:00:00+00:00', 50.0, 999, 99.0, 1, "
            " '2026-05-18T00:00:00+00:00', '2026-05-18T04:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()


def _stage_stats_with_one_milestone(stats_path: pathlib.Path) -> None:
    """Stage stats.db with one percent_milestone for 010 to recompute."""
    conn = sqlite3.connect(stats_path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
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
        )
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, percent_threshold, cumulative_cost_usd, "
            " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id) "
            "VALUES ('2026-05-19T00:00:00+00:00', '2026-05-18', '2026-05-25', "
            " '2026-05-18T00:00:00+00:00', '2026-05-25T00:00:00+00:00', "
            " 1, 99.0, 99.0, 1, 1)"
        )
        conn.commit()
    finally:
        conn.close()


def test_009_eager_apply_resolves_project_path_join(tmp_path, monkeypatch):
    """009's ``sf.project_path`` RO join prepares without ``no such
    column`` after the eager-apply path back-fills the column via the
    shared ``_apply_cache_schema`` helper.

    Stages a legacy cache.db FILE whose ``session_files`` lacks
    ``project_path`` (and has no ``cache_meta``). Running the 009 handler
    drives ``_open_cache_ro_with_gate_defer`` →
    ``_eagerly_apply_cache_migrations`` → ``_apply_cache_schema`` over
    that file, which must add ``project_path`` so the subsequent RO join
    resolves. With the walk-complete marker absent and a real block to
    protect, the gate DEFERs (``MigrationGateNotMet``) — but crucially
    NOT with ``no such column: sf.project_path``: the recompute is
    reached/skipped via the gate, never crashed at the join.
    """
    db = _load_db()
    core = db._cctally_core

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats_with_one_block(stats_path)
    _stage_legacy_cache_without_project_path(cache_path)
    _pin_paths(core, tmp_path, monkeypatch, cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        # Marker absent + block present + jsonl on disk → row 7 DEFER.
        # The point is that we reach the GATE decision cleanly — the
        # eager-apply join-resolution did NOT raise ``no such column``.
        with pytest.raises(db.MigrationGateNotMet) as ei:
            db._009_recompute_five_hour_blocks_dedup_fix(stats)
        assert "no such column" not in str(ei.value), (
            "R3 landmine: 009 must not crash on sf.project_path; the "
            f"eager-apply path must back-fill the column. got: {ei.value}"
        )
    finally:
        stats.close()

    _assert_project_path_and_cache_meta_backfilled(cache_path)


def test_009_eager_apply_recomputes_when_walk_complete(tmp_path, monkeypatch):
    """End-to-end green path: with the walk-complete marker present, 009
    PROCEEDs and recomputes the block from ``session_entries`` through the
    eager-apply-backfilled ``sf.project_path`` join (1000 opus-4-7 output
    tokens at $25/Mtok = $0.025)."""
    db = _load_db()
    core = db._cctally_core

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats_with_one_block(stats_path)
    _stage_legacy_cache_without_project_path(cache_path)
    # Seed the walk-complete marker AFTER schema-applying the file so the
    # gate PROCEEDs. cache_meta doesn't exist yet in the legacy file, so
    # create it then insert the marker.
    seed = sqlite3.connect(cache_path)
    try:
        seed.execute(
            "CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        seed.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('claude_ingest_walk_complete', '2026-05-22T02:00:00Z')"
        )
        seed.commit()
    finally:
        seed.close()
    _pin_paths(core, tmp_path, monkeypatch, cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        db._009_recompute_five_hour_blocks_dedup_fix(stats)
        db._stamp_applied(stats, "009_recompute_five_hour_blocks_dedup_fix")  # dispatcher now owns the stamp (#140)
        cost = stats.execute(
            "SELECT total_cost_usd FROM five_hour_blocks WHERE id = 1"
        ).fetchone()[0]
        assert cost == pytest.approx(0.025, abs=1e-9), (
            f"009 must recompute the block cost from session_entries; got {cost!r}"
        )
        marker = stats.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name = '009_recompute_five_hour_blocks_dedup_fix'"
        ).fetchone()
        assert marker is not None, "009 marker must be stamped on PROCEED"
    finally:
        stats.close()

    _assert_project_path_and_cache_meta_backfilled(cache_path)


def test_010_eager_apply_evaluates_and_recomputes(tmp_path, monkeypatch):
    """010 drives the SAME eager-apply → ``_apply_cache_schema`` →
    gate-evaluation path. 010's own read is project-agnostic (it reads
    ``session_entries`` only), but it must still survive the eager-apply
    schema bootstrap over a legacy cache.db and reach a clean gate
    decision + recompute.

    With the walk-complete marker present (seeded post-stage), 010
    PROCEEDs and recomputes the milestone cumulative cost to $0.025.
    """
    db = _load_db()
    core = db._cctally_core

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats_with_one_milestone(stats_path)
    _stage_legacy_cache_without_project_path(cache_path)
    seed = sqlite3.connect(cache_path)
    try:
        seed.execute(
            "CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        seed.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('claude_ingest_walk_complete', '2026-05-22T02:00:00Z')"
        )
        seed.commit()
    finally:
        seed.close()
    _pin_paths(core, tmp_path, monkeypatch, cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        db._010_recompute_percent_milestones_dedup_fix(stats)
        db._stamp_applied(stats, "010_recompute_percent_milestones_dedup_fix")  # dispatcher now owns the stamp (#140)
        cum = stats.execute(
            "SELECT cumulative_cost_usd FROM percent_milestones WHERE id = 1"
        ).fetchone()[0]
        assert cum == pytest.approx(0.025, abs=1e-9), (
            f"010 must recompute cumulative_cost_usd; got {cum!r}"
        )
        marker = stats.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name = '010_recompute_percent_milestones_dedup_fix'"
        ).fetchone()
        assert marker is not None, "010 marker must be stamped on PROCEED"
    finally:
        stats.close()

    _assert_project_path_and_cache_meta_backfilled(cache_path)


def test_010_eager_apply_defers_cleanly_without_marker(tmp_path, monkeypatch):
    """Symmetric to the 009 DEFER case: without the walk-complete marker
    and with a milestone to protect, 010 DEFERs — but reaches that gate
    decision cleanly through the eager-apply bootstrap (no ``no such
    table: cache_meta`` / ``no such column`` from the legacy file)."""
    db = _load_db()
    core = db._cctally_core

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats_with_one_milestone(stats_path)
    _stage_legacy_cache_without_project_path(cache_path)
    _pin_paths(core, tmp_path, monkeypatch, cache_path)

    stats = sqlite3.connect(stats_path)
    try:
        with pytest.raises(db.MigrationGateNotMet) as ei:
            db._010_recompute_percent_milestones_dedup_fix(stats)
        msg = str(ei.value)
        assert "no such column" not in msg and "no such table" not in msg, (
            "010 must reach the gate via the eager-apply bootstrap, not "
            f"crash on a legacy-schema read; got: {msg}"
        )
    finally:
        stats.close()

    _assert_project_path_and_cache_meta_backfilled(cache_path)
