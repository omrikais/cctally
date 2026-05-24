"""Shared SQLite-fixture helpers for `cctally` test harnesses.

Importable module (not executable). Consumed by `bin/build-<cmd>-fixtures.py`
scripts to seed deterministic, byte-reproducible DB fixtures for the
golden-file harnesses.

Three concerns:
  * Schema builders: `create_stats_db(path)` and `create_cache_db(path)` —
    write the full current schema (every column, every migration applied)
    so fixture DBs never trigger inline ALTER TABLE migrations at open time.
  * Row seeders: `seed_session_file(...)`, `seed_session_entry(...)`,
    `seed_weekly_usage_snapshot(...)`, `seed_codex_session_file(...)`,
    `seed_codex_session_entry(...)` — typed, keyword-only inserts with
    safe defaults.
  * JSONL emitters: `emit_streaming_pair(...)` — write the
    streaming-intermediate + post-stream-finalization two-row pattern
    into a `.claude/projects/<encoded>/<session>.jsonl` file so that
    blocks-/cache-/sync-week-style harnesses can exercise the v1.12.0
    ccusage-parity dedup ingest path (higher token total wins; `speed`-set
    breaks ties — mirrors ccusage's `should_replace_deduped_entry` at
    `rust/crates/ccusage/src/claude_loader.rs:531`).

Stdlib only. No external dependencies.
"""
from __future__ import annotations

import atexit
import json
import sqlite3
from pathlib import Path
from typing import Optional


# Used by all builders for session_files.last_ingested_at / codex_session_files.last_ingested_at
# so cache.db rebuilds are byte-deterministic. Arbitrary UTC instant — value
# doesn't matter; only stability does.
FIXED_LAST_INGESTED_AT = "2026-04-15T15:00:00Z"


# Bytes 96–99 of the SQLite header carry SQLITE_VERSION_NUMBER for the
# library that last wrote the file (per https://www.sqlite.org/fileformat.html
# — "library write version"). The field is informational only; SQLite
# does not consult it on read for compatibility decisions. Without
# normalization, the byte differs across Python interpreter sqlite3
# library versions (e.g. cpython 3.13's bundled lib is 3.53.0, 3.14's is
# 3.53.1), so every harness rebuild on a different interpreter dirties
# the in-tree fixtures by exactly one byte per file. Zeroing the field
# makes builder output byte-deterministic across SQLite library bumps.
_SQLITE_HEADER_LIBRARY_WRITE_VERSION_OFFSET = 96
_SQLITE_HEADER_LIBRARY_WRITE_VERSION_LEN = 4

# Set populated by `create_stats_db` / `create_cache_db` and any other
# builder helper that writes a `.db` file. The atexit hook below walks
# each registered parent directory at process exit and zeros the writer-
# version field on every `*.db` it finds. Registration happens at DB
# creation time; subsequent seed connections (`with sqlite3.connect(path)`
# blocks in builder scripts) re-stamp the field, but the atexit hook
# fires AFTER they close, so the final on-disk state is normalized.
_REGISTERED_FIXTURE_DIRS: set[Path] = set()


def normalize_sqlite_writer_version(path: Path) -> None:
    """Zero bytes 96–99 of the SQLite header at *path*.

    Safe to call on any well-formed SQLite file. No-op if the file does
    not exist. Does not open a sqlite3 connection — just rewrites four
    bytes via the filesystem, so it has no interaction with WAL state,
    schema, or transactions.
    """
    if not path.exists():
        return
    with path.open("r+b") as f:
        f.seek(_SQLITE_HEADER_LIBRARY_WRITE_VERSION_OFFSET)
        f.write(b"\x00" * _SQLITE_HEADER_LIBRARY_WRITE_VERSION_LEN)


def register_fixture_db(path: Path) -> None:
    """Track a fixture DB so the atexit hook normalizes it.

    Called automatically from `create_stats_db` / `create_cache_db`.
    Builder scripts that write `.db` files outside those helpers (e.g.
    `bin/build-migrations-fixtures.py`) should call this directly for
    each DB they create, so the writer-version field is normalized on
    process exit regardless of which `sqlite3.connect()` site last
    touched the file.
    """
    _REGISTERED_FIXTURE_DIRS.add(path.parent)


def _normalize_all_registered_fixture_dbs() -> None:
    # Builder scripts pervasively use `with sqlite3.connect(path) as conn:`
    # which commits but does NOT close the connection — it lingers until
    # garbage collection, and `sqlite3.Connection.__del__` runs AFTER
    # atexit handlers, restamping the writer-version field every time.
    # Force-close any open Connection objects first so the on-disk state
    # we normalize next is final. We avoid keeping our own registry of
    # connections (builders open many) and rely on gc to enumerate them.
    import gc
    gc.collect()
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.close()
            except Exception:
                pass
    for d in _REGISTERED_FIXTURE_DIRS:
        if not d.exists():
            continue
        # Per-migration fixtures live as `.sqlite` files (lazy-adopted
        # naming convention; see CLAUDE.md) — include both extensions so
        # the writer-version normalization covers them uniformly.
        for pattern in ("*.db", "*.sqlite"):
            for db in sorted(d.glob(pattern)):
                normalize_sqlite_writer_version(db)


atexit.register(_normalize_all_registered_fixture_dbs)


# Deterministic fixed instant for every stamped ``schema_migrations`` row —
# NOT wall-clock, so regenerating render-only fixtures stays byte-stable. Matches
# the instant ``create_cache_db`` stamps cache ``001`` with.
_STATS_MIGRATION_STAMP_AT = "2026-05-22T00:00:00Z"


def stamp_all_stats_migrations_applied(conn: sqlite3.Connection) -> None:
    """Stamp every registered stats migration applied + advance
    ``user_version`` so the migration dispatcher fast-paths (no body runs).

    Render-only fixtures (share, dashboard) ship as fully-migrated users so a
    read command's ``sync_cache`` walk can't flip the cctally-dev#93 upgrade-gate
    to PROCEED and recompute the seeded display tables (``weekly_cost_snapshots`` /
    ``five_hour_blocks`` / ``percent_milestones``) to ``$0``. Enumerates the live
    ``_STATS_MIGRATIONS`` registry so a future recompute migration is covered
    automatically. Uses a fixed deterministic timestamp for byte-stable output.

    The ``_cctally_db`` import is FUNCTION-LEVEL (lazy) so this module stays
    import-time stdlib-pure and there is no import cycle (``_cctally_db`` does
    not import ``_fixture_builders``). Callers already put ``bin/`` on
    ``sys.path`` before invoking the helper.
    """
    from _cctally_db import _STATS_MIGRATIONS  # noqa: E402,PLC0415 (intentional lazy)
    names = [m.name for m in _STATS_MIGRATIONS]
    conn.executemany(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
        "VALUES (?, ?)",
        [(name, _STATS_MIGRATION_STAMP_AT) for name in names],
    )
    # user_version == len(registry) is the dispatcher's all-applied fast-path
    # sentinel (_cctally_db._run_pending_migrations).
    conn.execute(f"PRAGMA user_version = {len(names)}")


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
    register_fixture_db(path)
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
                alerted_at TEXT,
                reset_event_id INTEGER NOT NULL DEFAULT 0,
                UNIQUE(week_start_date, percent_threshold, reset_event_id)
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
                alerted_at                  TEXT,
                reset_event_id              INTEGER NOT NULL DEFAULT 0,
                UNIQUE(five_hour_window_key, percent_threshold, reset_event_id),
                FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id)
            );
            CREATE INDEX idx_five_hour_milestones_block
                ON five_hour_milestones(block_id);

            -- five_hour_reset_events: parallel to week_reset_events for the
            -- 5h dimension. Added by issue #43 (live DDL in bin/cctally
            -- mirrored here so fixture stats.db files don't trigger inline
            -- CREATE TABLE at open time).
            CREATE TABLE five_hour_reset_events (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at_utc        TEXT NOT NULL,
                five_hour_window_key   INTEGER NOT NULL,
                prior_percent          REAL NOT NULL,
                post_percent           REAL NOT NULL,
                effective_reset_at_utc TEXT NOT NULL,
                UNIQUE(five_hour_window_key, effective_reset_at_utc)
            );

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
            "five_hour_reset_events",
            "five_hour_block_models", "five_hour_block_projects",
            "schema_migrations",
        }
        missing = expected - tables
        assert not missing, f"missing tables: {missing}"
    print("OK: create_stats_db")


def create_cache_db(path: Path) -> None:
    """Create (overwriting any existing file) a cache.db with the full
    current production schema by delegating to the SINGLE schema source,
    ``_cctally_db._apply_cache_schema`` (cctally-dev#96).

    Delegating (rather than hand-maintaining inline DDL) means any cache
    table/column later added to ``_apply_cache_schema`` lands in every
    fixture suite's ``cache.db`` automatically — no silent post-migration
    drift between fixtures and production. It is the same helper
    ``open_cache_db`` calls, so the two cannot diverge.

    On top of the shared schema this builder adds the two pieces that live
    OUTSIDE the shared helper in production, so fixtures match the
    post-migration on-disk state ``open_cache_db`` would produce:

      * ``codex_session_files.last_total_tokens`` — the Codex ALTER stays
        out of ``_apply_cache_schema`` because it carries a one-time purge
        side-effect (irrelevant on a fresh empty table); replayed here via
        ``add_column_if_missing``.
      * the migration-framework tables (``schema_migrations`` /
        ``schema_migrations_skipped``) with ``001_dedup_highest_wins``
        pre-stamped applied + ``user_version = 1`` — see the inline comment.

    ``_apply_cache_schema`` also creates the ``cache_meta`` sentinel table.
    It is left EMPTY here, so the upgrade-gate's walk-complete probe reads
    ``False`` exactly as it did when fixtures lacked the table entirely —
    no behavioral change for harnesses that open the fixture through a real
    ``cctally`` command.

    The ``_cctally_db`` import is FUNCTION-LEVEL (lazy), mirroring
    ``stamp_all_stats_migrations_applied``: keeps this module import-time
    stdlib-pure with no import cycle (``_cctally_db`` does not import us).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    register_fixture_db(path)
    from _cctally_db import (  # noqa: E402,PLC0415 (intentional lazy)
        _apply_cache_schema,
        add_column_if_missing,
    )
    with sqlite3.connect(path) as conn:
        # Match production's open_cache_db(): fixtures must be WAL so a
        # first open by the harness doesn't flip bytes 18/19 of the DB
        # header.
        conn.execute("PRAGMA journal_mode=WAL")
        # Single cache.db schema source — session_files (+ session_id /
        # project_path ALTER + index), session_entries (+ indexes), the
        # Codex tables, and the cache_meta sentinel.
        _apply_cache_schema(conn)
        # Codex last_total_tokens ALTER lives outside the shared helper in
        # production (it carries a one-time purge side-effect); replay it so
        # fixtures match the post-migration state. The purge is a no-op on a
        # fresh empty Codex table.
        add_column_if_missing(conn, "codex_session_files", "last_total_tokens", "INTEGER")
        # Migration framework tables. Fixture DBs represent the
        # post-migration state, so we pre-stamp every shipped cache
        # migration as applied and advance user_version. Without
        # this, the dispatcher's data-emptiness check (D1) would
        # see populated session_entries (added by callers via
        # seed_session_entry) + missing markers and trigger the
        # 001_dedup_highest_wins handler — wiping the seeded data
        # before the test could exercise it.
        conn.executescript("""
            CREATE TABLE schema_migrations (
                name           TEXT PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            );
            CREATE TABLE schema_migrations_skipped (
                name           TEXT PRIMARY KEY,
                skipped_at_utc TEXT NOT NULL,
                reason         TEXT
            );
            INSERT INTO schema_migrations (name, applied_at_utc)
            VALUES ('001_dedup_highest_wins', '2026-05-22T00:00:00Z');
            PRAGMA user_version = 1;
        """)


def _self_test_stamp_all_stats_migrations_applied() -> None:
    """Verify the shared stamp helper marks every registered stats migration
    applied and advances user_version to len(registry) (cctally-dev#94)."""
    import tempfile
    from _cctally_db import _STATS_MIGRATIONS
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "stats.db"
        create_stats_db(db)
        with sqlite3.connect(db) as conn:
            stamp_all_stats_migrations_applied(conn)
            conn.commit()
            applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            uv = conn.execute("PRAGMA user_version").fetchone()[0]
        expected = {m.name for m in _STATS_MIGRATIONS}
        assert applied == expected, f"stamp helper missing: {expected - applied}"
        assert uv == len(_STATS_MIGRATIONS), f"user_version not advanced: {uv}"
    print("OK: stamp_all_stats_migrations_applied")


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
                        "codex_session_files", "codex_session_entries",
                        "cache_meta",
                        "schema_migrations", "schema_migrations_skipped"}
            missing = expected - tables
            assert not missing, f"missing tables: {missing}"

            # cache_meta is created by _apply_cache_schema (cctally-dev#96)
            # but left EMPTY here: with no claude_ingest_walk_complete row the
            # upgrade-gate's walk-complete probe reads False — identical to the
            # pre-#96 behavior when fixtures lacked the table entirely.
            walk_row = conn.execute(
                "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
            ).fetchone()
            assert walk_row is None, "cache_meta walk-complete marker must be absent in fixtures"

            # 001_dedup_highest_wins is pre-stamped so the dispatcher's
            # D1 data-emptiness check doesn't fire 001 against seeded
            # session_entries rows.
            marker = conn.execute(
                "SELECT applied_at_utc FROM schema_migrations "
                "WHERE name = '001_dedup_highest_wins'"
            ).fetchone()
            assert marker is not None, "001_dedup_highest_wins marker not pre-stamped"
            uv = conn.execute("PRAGMA user_version").fetchone()[0]
            assert uv == 1, f"user_version not advanced to 1: {uv}"

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


def emit_streaming_pair(
    jsonl_path: Path,
    *,
    model: str,
    msg_id: str,
    req_id: str,
    ts_intermediate: str,
    ts_final: str,
    intermediate_output_tokens: int,
    final_output_tokens: int,
    cache_read_tokens: int = 0,
    cache_create_tokens: int = 0,
    input_tokens: int = 1,
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    append: bool = True,
) -> None:
    """Append the streaming-pair two-row pattern to a JSONL file.

    Writes two `type:"assistant"` rows sharing the same `(message.id,
    requestId)` pair: first the streaming intermediate (no `speed` field,
    `intermediate_output_tokens`), then the post-stream finalization
    (`speed="standard"`, `final_output_tokens`). When `sync_cache()` later
    ingests this JSONL via the v1.12.0 dedup path it MUST collapse the
    pair down to the higher-token row (`final_output_tokens`) — matches
    ccusage's `should_replace_deduped_entry` (`rust/crates/ccusage/src/
    claude_loader.rs:531`). Shared by dedup-aware fixtures so the
    same shape of pair is reproducible across harnesses.

    Args:
        jsonl_path: where to write. Parent must exist. The two rows are
            appended (default) to preserve any prior emissions on the
            same session file.
        model: Claude model id (e.g. ``claude-opus-4-7``); used for cost
            attribution by the downstream aggregator.
        msg_id: `message.id` value — same on both rows so the dedup index
            collapses them.
        req_id: `requestId` value — same on both rows.
        ts_intermediate / ts_final: ISO-8601 UTC timestamps for the two
            rows. The intermediate row's timestamp is typically a few ms
            before the final's, mirroring real Claude Code streaming.
        intermediate_output_tokens: usually ``1`` (the on-wire shape of a
            streaming intermediate in production).
        final_output_tokens: the real output token count; this is the row
            that must survive dedup.
        cache_read_tokens / cache_create_tokens: identical on both rows.
        input_tokens: identical on both rows (default ``1``, matches the
            streaming-intermediate shape).
        session_id / cwd: optional. When provided, written as ``sessionId``
            and ``cwd`` keys on each row so the cache ingest path can
            populate ``session_files.session_id`` / ``project_path`` from
            the JSONL itself rather than the file's encoded dirname.
        append: when ``True`` (default), append to ``jsonl_path``; when
            ``False``, truncate first. The default matches real Claude
            Code (a single session can emit many pairs).
    """
    intermediate: dict = {
        "type": "assistant",
        "timestamp": ts_intermediate,
        "requestId": req_id,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": intermediate_output_tokens,
                "cache_creation_input_tokens": cache_create_tokens,
                "cache_read_input_tokens": cache_read_tokens,
            },
        },
    }
    final: dict = {
        "type": "assistant",
        "timestamp": ts_final,
        "requestId": req_id,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": final_output_tokens,
                "cache_creation_input_tokens": cache_create_tokens,
                "cache_read_input_tokens": cache_read_tokens,
                "speed": "standard",
            },
        },
    }
    if session_id is not None:
        intermediate["sessionId"] = session_id
        final["sessionId"] = session_id
    if cwd is not None:
        intermediate["cwd"] = cwd
        final["cwd"] = cwd
    mode = "a" if append else "w"
    with jsonl_path.open(mode, encoding="utf-8") as f:
        f.write(json.dumps(intermediate) + "\n")
        f.write(json.dumps(final) + "\n")


def _self_test_emit_streaming_pair() -> None:
    """Verify emit_streaming_pair writes two valid JSON lines with the
    streaming-intermediate (no `speed`) → post-stream-finalization
    (`speed='standard'`) shape, and that overwrite vs append modes both
    work."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        jsonl = Path(td) / "session.jsonl"
        emit_streaming_pair(
            jsonl,
            model="claude-opus-4-7",
            msg_id="msg_test_1",
            req_id="req_test_1",
            ts_intermediate="2026-05-22T17:04:00.100Z",
            ts_final="2026-05-22T17:04:00.500Z",
            intermediate_output_tokens=1,
            final_output_tokens=3881,
            cache_read_tokens=42,
        )
        lines = jsonl.read_text().splitlines()
        assert len(lines) == 2, f"expected 2 lines, got {len(lines)}"
        intermediate, final = json.loads(lines[0]), json.loads(lines[1])
        assert intermediate["type"] == "assistant"
        assert intermediate["message"]["id"] == "msg_test_1"
        assert intermediate["requestId"] == "req_test_1"
        assert intermediate["message"]["usage"]["output_tokens"] == 1
        assert "speed" not in intermediate["message"]["usage"], \
            "intermediate row must NOT carry speed key"
        assert final["type"] == "assistant"
        assert final["message"]["id"] == "msg_test_1"
        assert final["requestId"] == "req_test_1"
        assert final["message"]["usage"]["output_tokens"] == 3881
        assert final["message"]["usage"]["speed"] == "standard"
        assert (
            intermediate["message"]["usage"]["cache_read_input_tokens"]
            == final["message"]["usage"]["cache_read_input_tokens"]
            == 42
        ), "cache_read_input_tokens must be identical on both rows"

        # Append-mode adds another pair without clobbering the first.
        emit_streaming_pair(
            jsonl,
            model="claude-opus-4-7",
            msg_id="msg_test_2",
            req_id="req_test_2",
            ts_intermediate="2026-05-22T17:05:00.100Z",
            ts_final="2026-05-22T17:05:00.500Z",
            intermediate_output_tokens=1,
            final_output_tokens=2000,
            session_id="sess-aaaa",
            cwd="/repo/example",
        )
        lines = jsonl.read_text().splitlines()
        assert len(lines) == 4, f"append did not extend file: got {len(lines)} lines"
        second_pair_final = json.loads(lines[3])
        assert second_pair_final["sessionId"] == "sess-aaaa"
        assert second_pair_final["cwd"] == "/repo/example"

        # Overwrite-mode clears the file.
        emit_streaming_pair(
            jsonl,
            model="claude-opus-4-7",
            msg_id="msg_test_3",
            req_id="req_test_3",
            ts_intermediate="2026-05-22T17:06:00.100Z",
            ts_final="2026-05-22T17:06:00.500Z",
            intermediate_output_tokens=1,
            final_output_tokens=1500,
            append=False,
        )
        lines = jsonl.read_text().splitlines()
        assert len(lines) == 2, f"overwrite did not truncate: got {len(lines)} lines"
    print("OK: emit_streaming_pair")


if __name__ == "__main__":
    _self_test_create_stats_db()
    _self_test_stamp_all_stats_migrations_applied()
    _self_test_create_cache_db()
    _self_test_claude_seeders()
    _self_test_weekly_usage_seeder()
    _self_test_codex_seeders()
    _self_test_emit_streaming_pair()
    print("all self-tests passed")
