"""Shared SQLite-fixture helpers for `cctally` test harnesses.

Importable module (not executable). Consumed by `bin/build-<cmd>-fixtures.py`
scripts to seed deterministic, byte-reproducible DB fixtures for the
golden-file harnesses.

Two concerns:
  * Schema builders: `create_stats_db(path)` and `create_cache_db(path)` —
    write the full current schema (every column, every migration applied)
    so fixture DBs never trigger inline ALTER TABLE migrations at open time.
  * Row seeders: `seed_session_file(...)`, `seed_session_entry(...)`,
    `seed_weekly_usage_snapshot(...)`, `seed_codex_session_file(...)`,
    `seed_codex_session_entry(...)` — typed, keyword-only inserts with
    safe defaults.

Stdlib only. No external dependencies.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


# Used by all builders for session_files.last_ingested_at / codex_session_files.last_ingested_at
# so cache.db rebuilds are byte-deterministic. Arbitrary UTC instant — value
# doesn't matter; only stability does.
FIXED_LAST_INGESTED_AT = "2026-04-15T15:00:00Z"


def create_stats_db(path: Path) -> None:
    """Create (overwriting any existing file) a stats.db with the full
    current production schema. Every column that `open_db()` would add
    via inline ALTER TABLE migrations is included here, so fixture DBs
    never trigger migration stderr at open time AND `cctally` invocations
    against the fixture (e.g. via the dashboard harness, which uses an
    in-tree HOME) don't silently add new tables under
    `CREATE TABLE IF NOT EXISTS` and dirty the working tree.

    Schema source: `open_db()` in bin/cctally. Columns
    added by inline ALTER TABLE migrations are baked into the initial
    CREATE TABLE statements below:
      * weekly_usage_snapshots: week_start_at, week_end_at,
        five_hour_percent, five_hour_resets_at, five_hour_window_key
      * weekly_cost_snapshots:  week_start_at, week_end_at,
        range_start_iso, range_end_iso
      * percent_milestones:     five_hour_percent_at_crossing

    Five-hour-feature tables (added 2026-04-30+):
      * five_hour_blocks         (rollup, one row per API-anchored block)
      * five_hour_milestones     (per-percent crossings inside a block)
      * five_hour_block_models   (per-(block, model) rollup-child)
      * five_hour_block_projects (per-(block, project_path) rollup-child)
      * schema_migrations        (durable migration-completion marker)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as conn:
        # Match production's open_db(): fixtures must be WAL so a first
        # open by the harness doesn't flip bytes 18/19 of the DB header.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE weekly_usage_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                week_start_date TEXT NOT NULL,
                week_end_date TEXT NOT NULL,
                week_start_at TEXT,
                week_end_at TEXT,
                weekly_percent REAL NOT NULL,
                page_url TEXT,
                source TEXT NOT NULL DEFAULT 'userscript',
                payload_json TEXT NOT NULL,
                five_hour_percent REAL,
                five_hour_resets_at TEXT,
                five_hour_window_key INTEGER
            );
            CREATE INDEX idx_usage_week_time
                ON weekly_usage_snapshots(week_start_date, captured_at_utc DESC, id DESC);
            CREATE INDEX idx_usage_week_start_at_time
                ON weekly_usage_snapshots(week_start_at, captured_at_utc DESC, id DESC);
            CREATE INDEX idx_weekly_usage_snapshots_5h_window_key
                ON weekly_usage_snapshots(five_hour_window_key);

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
            CREATE INDEX idx_cost_week_time
                ON weekly_cost_snapshots(week_start_date, captured_at_utc DESC, id DESC);
            CREATE INDEX idx_cost_week_start_at_time
                ON weekly_cost_snapshots(week_start_at, captured_at_utc DESC, id DESC);

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
                five_hour_percent_at_crossing REAL,
                UNIQUE(week_start_date, percent_threshold)
            );

            CREATE TABLE week_reset_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at_utc        TEXT NOT NULL,
                old_week_end_at        TEXT NOT NULL,
                new_week_end_at        TEXT NOT NULL,
                effective_reset_at_utc TEXT NOT NULL,
                UNIQUE(old_week_end_at, new_week_end_at)
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
            CREATE INDEX idx_five_hour_blocks_block_start
                ON five_hour_blocks(block_start_at DESC);

            CREATE TABLE five_hour_milestones (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id                    INTEGER NOT NULL,
                five_hour_window_key        INTEGER NOT NULL,
                percent_threshold           INTEGER NOT NULL,
                captured_at_utc             TEXT    NOT NULL,
                usage_snapshot_id           INTEGER NOT NULL,
                block_input_tokens          INTEGER NOT NULL DEFAULT 0,
                block_output_tokens         INTEGER NOT NULL DEFAULT 0,
                block_cache_create_tokens   INTEGER NOT NULL DEFAULT 0,
                block_cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
                block_cost_usd              REAL    NOT NULL DEFAULT 0,
                marginal_cost_usd           REAL,
                seven_day_pct_at_crossing   REAL,
                UNIQUE(five_hour_window_key, percent_threshold),
                FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id)
            );
            CREATE INDEX idx_five_hour_milestones_block
                ON five_hour_milestones(block_id);

            CREATE TABLE schema_migrations (
                name             TEXT PRIMARY KEY,
                applied_at_utc   TEXT NOT NULL
            );

            CREATE TABLE five_hour_block_models (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id                    INTEGER NOT NULL,
                five_hour_window_key        INTEGER NOT NULL,
                model                       TEXT    NOT NULL,
                input_tokens                INTEGER NOT NULL DEFAULT 0,
                output_tokens               INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens         INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens           INTEGER NOT NULL DEFAULT 0,
                cost_usd                    REAL    NOT NULL DEFAULT 0,
                entry_count                 INTEGER NOT NULL DEFAULT 0,
                UNIQUE(five_hour_window_key, model),
                FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id)
            );
            CREATE INDEX idx_five_hour_block_models_block
                ON five_hour_block_models(block_id);
            CREATE INDEX idx_five_hour_block_models_window
                ON five_hour_block_models(five_hour_window_key);

            CREATE TABLE five_hour_block_projects (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id                    INTEGER NOT NULL,
                five_hour_window_key        INTEGER NOT NULL,
                project_path                TEXT    NOT NULL,
                input_tokens                INTEGER NOT NULL DEFAULT 0,
                output_tokens               INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens         INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens           INTEGER NOT NULL DEFAULT 0,
                cost_usd                    REAL    NOT NULL DEFAULT 0,
                entry_count                 INTEGER NOT NULL DEFAULT 0,
                UNIQUE(five_hour_window_key, project_path),
                FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id)
            );
            CREATE INDEX idx_five_hour_block_projects_block
                ON five_hour_block_projects(block_id);
            CREATE INDEX idx_five_hour_block_projects_window
                ON five_hour_block_projects(five_hour_window_key);
        """)


def _self_test_create_stats_db() -> None:
    """Verify create_stats_db() produces a DB with the expected tables.
    The expected-set covers both pre-five-hour-feature and post-feature
    tables — when a future schema addition extends `open_db()`, this set
    must be extended in lock-step or fixture builders fall behind and
    `cctally` invocations against fixtures dirty the in-tree DBs."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "stats.db"
        create_stats_db(db)
        assert db.exists(), "stats.db not created"
        with sqlite3.connect(db) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert mode == "wal", f"stats.db journal_mode not WAL: {mode}"
        expected = {
            "weekly_usage_snapshots", "weekly_cost_snapshots",
            "percent_milestones", "week_reset_events",
            "five_hour_blocks", "five_hour_milestones",
            "five_hour_block_models", "five_hour_block_projects",
            "schema_migrations",
        }
        missing = expected - tables
        assert not missing, f"missing tables: {missing}"
    print("OK: create_stats_db")


def create_cache_db(path: Path) -> None:
    """Create (overwriting any existing file) a cache.db with the full
    current production schema — Claude side (session_files,
    session_entries) and Codex side (codex_session_files,
    codex_session_entries). All inline ALTER TABLE migrations are
    pre-applied.

    Schema source: `open_cache_db()` in bin/cctally.
    Columns added by inline ALTER TABLE migrations are baked into the
    initial CREATE TABLE statements below:
      * session_files:       session_id, project_path
      * codex_session_files: last_total_tokens

    Indexes created (to match production exactly so fixture DBs do not
    trigger index-creation on first open):
      * session_entries:        idx_entries_timestamp,
                                idx_entries_source,
                                idx_entries_dedup (UNIQUE, partial)
      * session_files:          idx_session_files_session_id
      * codex_session_entries:  idx_codex_entries_timestamp,
                                idx_codex_entries_session,
                                idx_codex_entries_source
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as conn:
        # Match production's open_cache_db(): fixtures must be WAL so a
        # first open by the harness doesn't flip bytes 18/19 of the DB
        # header.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE session_files (
                path             TEXT PRIMARY KEY,
                size_bytes       INTEGER NOT NULL,
                mtime_ns         INTEGER NOT NULL,
                last_byte_offset INTEGER NOT NULL,
                last_ingested_at TEXT NOT NULL,
                session_id       TEXT,
                project_path     TEXT
            );
            CREATE INDEX idx_session_files_session_id
                ON session_files(session_id);

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
            CREATE INDEX idx_entries_timestamp
                ON session_entries(timestamp_utc);
            CREATE INDEX idx_entries_source
                ON session_entries(source_path);
            CREATE UNIQUE INDEX idx_entries_dedup
                ON session_entries(msg_id, req_id)
                WHERE msg_id IS NOT NULL AND req_id IS NOT NULL;

            CREATE TABLE codex_session_files (
                path              TEXT PRIMARY KEY,
                size_bytes        INTEGER NOT NULL,
                mtime_ns          INTEGER NOT NULL,
                last_byte_offset  INTEGER NOT NULL,
                last_ingested_at  TEXT NOT NULL,
                last_session_id   TEXT,
                last_model        TEXT,
                last_total_tokens INTEGER
            );

            CREATE TABLE codex_session_entries (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path              TEXT    NOT NULL,
                line_offset              INTEGER NOT NULL,
                timestamp_utc            TEXT    NOT NULL,
                session_id               TEXT    NOT NULL,
                model                    TEXT    NOT NULL,
                input_tokens             INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens      INTEGER NOT NULL DEFAULT 0,
                output_tokens            INTEGER NOT NULL DEFAULT 0,
                reasoning_output_tokens  INTEGER NOT NULL DEFAULT 0,
                total_tokens             INTEGER NOT NULL DEFAULT 0,
                UNIQUE(source_path, line_offset)
            );
            CREATE INDEX idx_codex_entries_timestamp
                ON codex_session_entries(timestamp_utc);
            CREATE INDEX idx_codex_entries_session
                ON codex_session_entries(session_id);
            CREATE INDEX idx_codex_entries_source
                ON codex_session_entries(source_path);
        """)


def _self_test_create_cache_db() -> None:
    """Verify create_cache_db() produces a DB with the expected tables
    AND the expected columns per table (catches schema-drift regressions,
    not just missing-table regressions)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "cache.db"
        create_cache_db(db)
        with sqlite3.connect(db) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal", f"cache.db journal_mode not WAL: {mode}"
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            expected = {"session_files", "session_entries",
                        "codex_session_files", "codex_session_entries"}
            missing = expected - tables
            assert not missing, f"missing tables: {missing}"

            # Column-presence checks — catch a later edit that accidentally
            # drops an ALTER-added column (e.g., codex_session_files.last_total_tokens
            # or session_files.session_id/project_path).
            sf_cols = {r[1] for r in conn.execute("PRAGMA table_info(session_files)")}
            assert "session_id" in sf_cols, "session_files.session_id missing"
            assert "project_path" in sf_cols, "session_files.project_path missing"

            csf_cols = {r[1] for r in conn.execute("PRAGMA table_info(codex_session_files)")}
            assert "last_total_tokens" in csf_cols, "codex_session_files.last_total_tokens missing (migration not baked in)"
            assert "last_session_id" in csf_cols, "codex_session_files.last_session_id missing"
            assert "last_model" in csf_cols, "codex_session_files.last_model missing"

            # Index-presence checks — production creates these in open_cache_db();
            # fixture DBs must match so first-open doesn't mutate the file.
            indexes = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_autoindex_%'"
            )}
            expected_indexes = {
                "idx_entries_timestamp",
                "idx_entries_source",
                "idx_entries_dedup",
                "idx_session_files_session_id",
                "idx_codex_entries_timestamp",
                "idx_codex_entries_session",
                "idx_codex_entries_source",
            }
            missing_indexes = expected_indexes - indexes
            assert not missing_indexes, f"missing indexes: {missing_indexes}"
    print("OK: create_cache_db")


def seed_session_file(
    conn: sqlite3.Connection,
    *,
    path: str,
    session_id: Optional[str],
    project_path: Optional[str],
    size_bytes: int = 0,
    mtime_ns: int = 0,
    last_byte_offset: int = 0,
    last_ingested_at: str = FIXED_LAST_INGESTED_AT,
) -> None:
    """Insert a session_files row. `session_id` / `project_path` may be
    None to simulate lazy-population (see spec: resumed-across-files
    and missing-session-id-fallback scenarios)."""
    conn.execute(
        """INSERT INTO session_files
           (path, size_bytes, mtime_ns, last_byte_offset,
            last_ingested_at, session_id, project_path)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (path, size_bytes, mtime_ns, last_byte_offset,
         last_ingested_at, session_id, project_path),
    )


def seed_session_entry(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    line_offset: int,
    timestamp_utc: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_create: int = 0,
    cache_read: int = 0,
    msg_id: Optional[str] = None,
    req_id: Optional[str] = None,
    usage_extra_json: Optional[str] = None,
    cost_usd_raw: Optional[float] = None,
) -> None:
    """Insert a session_entries row. Matches production column list;
    cost_usd_raw left NULL by default since costs are recomputed at
    query time from CLAUDE_MODEL_PRICING."""
    conn.execute(
        """INSERT INTO session_entries
           (source_path, line_offset, timestamp_utc, model,
            msg_id, req_id,
            input_tokens, output_tokens, cache_create_tokens, cache_read_tokens,
            usage_extra_json, cost_usd_raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_path, line_offset, timestamp_utc, model,
         msg_id, req_id,
         input_tokens, output_tokens, cache_create, cache_read,
         usage_extra_json, cost_usd_raw),
    )


def seed_weekly_usage_snapshot(
    conn: sqlite3.Connection,
    *,
    captured_at_utc: str,
    week_start_date: str,
    week_end_date: str,
    weekly_percent: float,
    week_start_at: Optional[str] = None,
    week_end_at: Optional[str] = None,
    five_hour_percent: Optional[float] = None,
    five_hour_resets_at: Optional[str] = None,
    five_hour_window_key: Optional[int] = None,
    page_url: Optional[str] = None,
    source: str = "userscript",
    payload_json: str = "{}",
) -> None:
    """Insert a weekly_usage_snapshots row.

    Set `week_start_at` / `week_end_at` to None (the default) to exercise
    the date-only fallback matching path used by production when legacy
    snapshot data lacks ISO-timestamp boundaries. Provide explicit ISO
    timestamps to exercise the preferred ISO-match path (see spec:
    mixed-boundary-fallback scenario).

    `five_hour_window_key` mirrors production's lazy-population column.
    Default None matches existing fixtures (no 5h binding); pass an
    explicit canonical 5h-window key (epoch seconds floored to 600s,
    same shape as `_canonical_5h_window_key`) to exercise the dashboard's
    `_select_current_block_for_envelope` path that joins
    `weekly_usage_snapshots.five_hour_window_key` against
    `five_hour_blocks.five_hour_window_key`."""
    conn.execute(
        """INSERT INTO weekly_usage_snapshots
           (captured_at_utc, week_start_date, week_end_date,
            week_start_at, week_end_at, weekly_percent,
            page_url, source, payload_json,
            five_hour_percent, five_hour_resets_at,
            five_hour_window_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (captured_at_utc, week_start_date, week_end_date,
         week_start_at, week_end_at, weekly_percent,
         page_url, source, payload_json,
         five_hour_percent, five_hour_resets_at,
         five_hour_window_key),
    )


def seed_week_reset_event(
    conn: sqlite3.Connection,
    *,
    detected_at_utc: str,
    old_week_end_at: str,
    new_week_end_at: str,
    effective_reset_at_utc: str,
) -> None:
    """Insert a week_reset_events row.

    Mirrors production's runtime-detected (cmd_record_usage) and backfilled
    (_backfill_week_reset_events) inserts. Use this in fixtures that need
    to exercise mid-week-reset boundary overrides applied by
    `_apply_reset_events_to_subweeks` / `_apply_reset_events_to_weekrefs`.
    `INSERT OR IGNORE` matches production: UNIQUE(old_week_end_at,
    new_week_end_at) protects against double-inserts."""
    conn.execute(
        "INSERT OR IGNORE INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
        (detected_at_utc, old_week_end_at, new_week_end_at,
         effective_reset_at_utc),
    )


def seed_codex_session_file(
    conn: sqlite3.Connection,
    *,
    path: str,
    last_session_id: Optional[str],
    last_model: Optional[str],
    last_total_tokens: int = 0,
    size_bytes: int = 0,
    mtime_ns: int = 0,
    last_byte_offset: int = 0,
    last_ingested_at: str = FIXED_LAST_INGESTED_AT,
) -> None:
    """Insert a codex_session_files row.

    `last_session_id` / `last_model` may be None to simulate the
    pre-lazy-population state (row exists but sessionId / model
    extraction hasn't run). Pass explicit values to simulate a
    fully-ingested file (the normal state after the first sync-cache
    run)."""
    conn.execute(
        """INSERT INTO codex_session_files
           (path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at,
            last_session_id, last_model, last_total_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at,
         last_session_id, last_model, last_total_tokens),
    )


def seed_codex_session_entry(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    line_offset: int,
    timestamp_utc: str,
    session_id: str,
    model: str,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_output_tokens: int = 0,
    total_tokens: int = 0,
) -> None:
    """Insert a codex_session_entries row.

    NOTE LiteLLM convention: `input_tokens` INCLUDES `cached_input_tokens`,
    and `output_tokens` INCLUDES `reasoning_output_tokens`. Fixtures must
    respect this to match production's cost-computation formula:
      (input - cached) * input_rate + cached * cache_read_rate + output * output_rate
    Reasoning is NOT added separately."""
    conn.execute(
        """INSERT INTO codex_session_entries
           (source_path, line_offset, timestamp_utc, session_id, model,
            input_tokens, cached_input_tokens,
            output_tokens, reasoning_output_tokens, total_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_path, line_offset, timestamp_utc, session_id, model,
         input_tokens, cached_input_tokens,
         output_tokens, reasoning_output_tokens, total_tokens),
    )


def _self_test_claude_seeders() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "cache.db"
        create_cache_db(db)
        with sqlite3.connect(db) as conn:
            seed_session_file(
                conn,
                path="/fake/proj/session-a.jsonl",
                session_id="sess-aaaa",
                project_path="/fake/proj",
            )
            seed_session_entry(
                conn,
                source_path="/fake/proj/session-a.jsonl",
                line_offset=0,
                timestamp_utc="2026-04-15T10:00:00Z",
                model="claude-sonnet-4-5",
                input_tokens=100,
                output_tokens=50,
                cache_read=20,
            )
            conn.commit()
            n_files = conn.execute("SELECT COUNT(*) FROM session_files").fetchone()[0]
            n_entries = conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0]

            # Also verify the Python param → DB column mapping worked for
            # the shorter parameter names (cache_create / cache_read):
            row = conn.execute(
                "SELECT cache_create_tokens, cache_read_tokens FROM session_entries"
            ).fetchone()
        assert n_files == 1, f"session_files count: {n_files}"
        assert n_entries == 1, f"session_entries count: {n_entries}"
        assert row == (0, 20), f"cache_create_tokens/cache_read_tokens mapping wrong: {row}"
    print("OK: claude seeders")


def _self_test_weekly_usage_seeder() -> None:
    """Verify seed_weekly_usage_snapshot round-trips all fields,
    including both the ISO-timestamp-provided and date-only-fallback modes."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "stats.db"
        create_stats_db(db)
        with sqlite3.connect(db) as conn:
            # ISO-timestamp mode (also exercises five_hour_window_key kwarg)
            seed_weekly_usage_snapshot(
                conn,
                captured_at_utc="2026-04-15T12:00:00Z",
                week_start_date="2026-04-13",
                week_end_date="2026-04-20",
                week_start_at="2026-04-13T15:00:00Z",
                week_end_at="2026-04-20T15:00:00Z",
                weekly_percent=42.5,
                five_hour_percent=7.25,
                five_hour_window_key=1776628800,
            )
            # Date-only fallback mode
            seed_weekly_usage_snapshot(
                conn,
                captured_at_utc="2026-04-08T12:00:00Z",
                week_start_date="2026-04-06",
                week_end_date="2026-04-13",
                weekly_percent=18.0,
            )
            conn.commit()
            rows = list(conn.execute(
                "SELECT week_start_date, weekly_percent, week_start_at, "
                "five_hour_percent, five_hour_window_key "
                "FROM weekly_usage_snapshots ORDER BY week_start_date"
            ))
        assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
        # Row 0: older week (date-only fallback) — five_hour_window_key NULL.
        assert rows[0] == ("2026-04-06", 18.0, None, None, None), f"row 0 mismatch: {rows[0]}"
        # Row 1: newer week (ISO-timestamp mode + five_hour_window_key set).
        assert rows[1] == ("2026-04-13", 42.5, "2026-04-13T15:00:00Z", 7.25, 1776628800), \
            f"row 1 mismatch: {rows[1]}"
    print("OK: weekly_usage seeder")


def _self_test_codex_seeders() -> None:
    """Verify codex seeders round-trip correctly, including None-as-NULL
    for lazy-population fields and the LiteLLM-convention token fields."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "cache.db"
        create_cache_db(db)
        with sqlite3.connect(db) as conn:
            # Fully-ingested file
            seed_codex_session_file(
                conn,
                path="/fake/codex/sess-x.jsonl",
                last_session_id="codex-sess-xxxx",
                last_model="gpt-5",
                last_total_tokens=500,
            )
            # Pre-lazy-population file (session_id and model both None)
            seed_codex_session_file(
                conn,
                path="/fake/codex/sess-y.jsonl",
                last_session_id=None,
                last_model=None,
            )
            seed_codex_session_entry(
                conn,
                source_path="/fake/codex/sess-x.jsonl",
                line_offset=0,
                timestamp_utc="2026-04-15T10:00:00Z",
                session_id="codex-sess-xxxx",
                model="gpt-5",
                input_tokens=200,
                cached_input_tokens=50,
                output_tokens=300,
                reasoning_output_tokens=100,
                total_tokens=500,
            )
            conn.commit()
            files_count = conn.execute("SELECT COUNT(*) FROM codex_session_files").fetchone()[0]
            entries_count = conn.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0]
            # Verify lazy-population row stores NULL correctly
            null_row = conn.execute(
                "SELECT last_session_id, last_model, last_total_tokens "
                "FROM codex_session_files WHERE path=?",
                ("/fake/codex/sess-y.jsonl",),
            ).fetchone()
            # Verify LiteLLM-convention token fields round-trip
            tokens = conn.execute(
                "SELECT input_tokens, cached_input_tokens, output_tokens, "
                "reasoning_output_tokens, total_tokens FROM codex_session_entries"
            ).fetchone()
        assert files_count == 2, f"expected 2 files, got {files_count}"
        assert entries_count == 1, f"expected 1 entry, got {entries_count}"
        assert null_row == (None, None, 0), f"lazy-population row mismatch: {null_row}"
        assert tokens == (200, 50, 300, 100, 500), f"token fields mismatch: {tokens}"
    print("OK: codex seeders")


if __name__ == "__main__":
    _self_test_create_stats_db()
    _self_test_create_cache_db()
    _self_test_claude_seeders()
    _self_test_weekly_usage_seeder()
    _self_test_codex_seeders()
    print("all self-tests passed")
