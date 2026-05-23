#!/usr/bin/env python3
"""Builder: tests/fixtures/migrations/<scenario>/input.{stats,cache}.db.

Deterministic, WAL-mode, TZ=Etc/UTC. Emits the fixtures consumed by
bin/cctally-migrations-test:
  - 02-upgrade-with-some-applied/  — stats has 001+002 applied; cache empty
  - 03-migration-raises-banner-renders/  — stats has 001 applied + test_failure_trigger seeded
  - 07-downgrade-detected-exit-2/  — stats with PRAGMA user_version = 99
  - 09-cache-db-and-stats-db/  — both DBs in distinct mid-states
  - 10-legacy-marker-recognized-by-db-status/  — stats has unprefixed (pre-framework) markers
  - 11-five-hour-dedup-after-backfill/  — stats has jitter-forked snapshot keys, no five_hour_blocks
  - 12-skipped-dedup-not-rerun-after-backfill/  — like 11, but 003 is in schema_migrations_skipped

Per-migration goldens (lazy-adopted; one pair per migration that ships them):
  - per-migration/001_dedup_highest_wins/{pre,post}.sqlite — cache.db pre/post for
    the ccusage-parity dedup migration. Loaded by tests/test_migration_001_per_migration_goldens.py.
  - per-migration/008_recompute_weekly_cost_snapshots_dedup_fix/{pre,pre-cache,post}.sqlite
    — paired stats+cache fixture for the ccusage-parity historical recompute.
    Loaded by tests/test_migration_008_per_migration_goldens.py.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import register_fixture_db  # noqa: E402


FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "migrations"

# Stable timestamps — never use "now"; use these.
TS_001_APPLIED = "2026-04-30T12:00:00Z"
TS_002_APPLIED = "2026-04-30T12:00:00Z"
TS_003_APPLIED = "2026-05-04T08:12:11Z"

# cctally-dev#93: cache.db carries a cache_meta KV table whose
# ``claude_ingest_walk_complete`` row is the migration upgrade-gate's
# ingest-completeness sentinel. The gate PROCEEDs (rows 4/5/6) only when
# this marker is present (walk✓) AND session_entries is non-empty
# (entries✓) — or there's nothing to protect. Every cache.db the builders
# construct now creates the table (production-shape parity via
# _apply_cache_schema); gate-PASSING goldens seed the marker.
WALK_COMPLETE_MARKER = "claude_ingest_walk_complete"
CACHE_META_DDL = """
        CREATE TABLE cache_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
"""


def _new_stats_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    register_fixture_db(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    return conn


def _new_cache_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    register_fixture_db(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def build_02_upgrade(scenario_dir: Path) -> None:
    scenario_dir.mkdir(parents=True, exist_ok=True)
    stats = scenario_dir / "input.stats.db"
    conn = _new_stats_db(stats)
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('001_five_hour_block_models_backfill_v1', ?)",
        (TS_001_APPLIED,),
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('002_five_hour_block_projects_backfill_v1', ?)",
        (TS_002_APPLIED,),
    )
    # user_version = 0 forces the dispatcher to walk the registry; missing
    # marker for 003_ + dispatcher's test-injection 004_ will be applied.
    conn.commit()
    conn.close()
    cache = scenario_dir / "input.cache.db"
    _new_cache_db(cache).close()


def build_03_failure(scenario_dir: Path) -> None:
    scenario_dir.mkdir(parents=True, exist_ok=True)
    stats = scenario_dir / "input.stats.db"
    conn = _new_stats_db(stats)
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('001_five_hour_block_models_backfill_v1', ?)",
        (TS_001_APPLIED,),
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('002_five_hour_block_projects_backfill_v1', ?)",
        (TS_002_APPLIED,),
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('003_merge_5h_block_duplicates_v1', ?)",
        (TS_003_APPLIED,),
    )
    # Seed the failure trigger so the test-injection migration raises.
    conn.execute("CREATE TABLE test_failure_trigger (sentinel INTEGER)")
    conn.execute("INSERT INTO test_failure_trigger VALUES (1)")
    conn.commit()
    conn.close()
    # Cache.db: pre-bootstrap with the 001 cache migration marker + the
    # cache_meta walk-complete marker (cctally-dev#93) so stats migration
    # 008's cross-DB gate is satisfied by the new ingest-completeness
    # signal during the stats walk. (008's stats.db here holds no
    # weekly_cost_snapshots rows, so data_present=False → the resolver
    # PROCEEDs at row 5 even absent the marker; we still seed the marker
    # for production-shape parity so this golden keeps exercising the
    # PROCEED path the way a real upgraded cache.db would.) Without 008
    # proceeding, it would defer (no error logged) and break BEFORE the
    # test-injection migration runs — silently neutering scenarios
    # 03/04/05/06/08 that depend on the test-injection failure firing.
    # Cache schema mirrors production (_apply_cache_schema) incl. the
    # cache_meta table.
    cache = scenario_dir / "input.cache.db"
    cache_conn = _new_cache_db(cache)
    cache_conn.executescript(
        """
        CREATE TABLE schema_migrations (
            name           TEXT PRIMARY KEY,
            applied_at_utc TEXT NOT NULL
        );
        CREATE TABLE session_files (
            path             TEXT PRIMARY KEY,
            size_bytes       INTEGER NOT NULL,
            mtime_ns         INTEGER NOT NULL,
            last_byte_offset INTEGER NOT NULL,
            last_ingested_at TEXT NOT NULL,
            session_id       TEXT,
            project_path     TEXT
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
        + CACHE_META_DDL
    )
    # 001 cache marker — stamped at TS_003_APPLIED (production-shape).
    cache_conn.execute(
        "INSERT INTO schema_migrations VALUES (?, ?)",
        ("001_dedup_highest_wins", TS_003_APPLIED),
    )
    # cache_meta walk-complete marker — the new gate's walk✓ signal
    # (cctally-dev#93). Replaces the old "post-001 session_files row" proof.
    cache_conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
        (WALK_COMPLETE_MARKER, "2026-05-04T08:13:00Z"),
    )
    # session_files row retained for production-shape parity (no longer
    # the gate signal post-cctally-dev#93).
    cache_conn.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "/fake/.claude/projects/p1/sess-a.jsonl",
            100, 1_700_000_000_000_000_000, 100,
            "2026-05-04T08:13:00Z",
            "sess-a", "p1",
        ),
    )
    cache_conn.commit()
    cache_conn.close()


def build_07_downgrade(scenario_dir: Path) -> None:
    scenario_dir.mkdir(parents=True, exist_ok=True)
    stats = scenario_dir / "input.stats.db"
    conn = _new_stats_db(stats)
    # Pre-stamp three real markers so the registry-walk would fast-path,
    # then overwrite user_version = 99 to trigger DowngradeDetected.
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('001_five_hour_block_models_backfill_v1', ?)",
        (TS_001_APPLIED,),
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('002_five_hour_block_projects_backfill_v1', ?)",
        (TS_002_APPLIED,),
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('003_merge_5h_block_duplicates_v1', ?)",
        (TS_003_APPLIED,),
    )
    conn.commit()
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()


def build_09_both(scenario_dir: Path) -> None:
    scenario_dir.mkdir(parents=True, exist_ok=True)
    # Stats: all three real markers applied, no failure trigger.
    stats = scenario_dir / "input.stats.db"
    conn = _new_stats_db(stats)
    for name, ts in [
        ("001_five_hour_block_models_backfill_v1", TS_001_APPLIED),
        ("002_five_hour_block_projects_backfill_v1", TS_002_APPLIED),
        ("003_merge_5h_block_duplicates_v1", TS_003_APPLIED),
    ]:
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (name, ts))
    conn.commit()
    conn.close()
    # Cache: empty (fresh-install path on next open).
    cache = scenario_dir / "input.cache.db"
    _new_cache_db(cache).close()


def build_10_legacy_markers(scenario_dir: Path) -> None:
    """Stats.db carrying pre-framework unprefixed marker rows. Validates
    that cmd_db_status (raw sqlite3, bypasses open_db()) treats legacy
    names as already-applied via the alias map — fix for the review
    finding where the first `cctally db status` after upgrade reports
    legacy-marker DBs as pending."""
    scenario_dir.mkdir(parents=True, exist_ok=True)
    stats = scenario_dir / "input.stats.db"
    conn = _new_stats_db(stats)
    for legacy_name, ts in [
        ("five_hour_block_models_backfill_v1",   TS_001_APPLIED),
        ("five_hour_block_projects_backfill_v1", TS_002_APPLIED),
        ("merge_5h_block_duplicates_v1",         TS_003_APPLIED),
    ]:
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)", (legacy_name, ts)
        )
    conn.commit()
    conn.close()


def build_11_five_hour_dedup_after_backfill(scenario_dir: Path) -> None:
    """Stats.db with jitter-forked five_hour_window_key values across two
    weekly_usage_snapshots rows that represent the same physical 5h
    window — and an empty five_hour_blocks. Validates that open_db()
    re-runs the dedup migration after the historical backfill creates
    duplicates from those forked snapshot keys.

    Two snapshots seeded with five_hour_window_key=1730000000 and
    1730000600 (600s apart, i.e. one canonical-floor bucket apart but
    well within the dedup migration's 1800s grouping window). Without
    the fix, backfill creates 2 parent rows that the already-stamped
    dedup marker prevents from ever being merged.
    """
    scenario_dir.mkdir(parents=True, exist_ok=True)
    stats = scenario_dir / "input.stats.db"
    if stats.exists():
        stats.unlink()
    conn = sqlite3.connect(stats)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    # Seed weekly_usage_snapshots with the columns open_db() would create
    # plus the columns it adds via add_column_if_missing — pre-populating
    # five_hour_window_key bypasses the canonicalization backfill (which
    # only fills NULL keys) and lets us preserve the forked values.
    conn.execute(
        """
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
        )
        """
    )
    rows = [
        # epoch 1730000000 = 2024-10-27T01:13:20Z; window_key = epoch.
        # (Both epochs are exact multiples of 600 so canonical floor == epoch.)
        ("2024-10-26T22:00:00+00:00", "2024-10-21", "2024-10-27",
         "2024-10-21T00:00:00+00:00", "2024-10-28T00:00:00+00:00",
         12.0, "{}", 50.0, "2024-10-27T01:13:20+00:00", 1730000000),
        # epoch 1730000600 = 2024-10-27T01:23:20Z; one canonical bucket later.
        ("2024-10-26T22:05:00+00:00", "2024-10-21", "2024-10-27",
         "2024-10-21T00:00:00+00:00", "2024-10-28T00:00:00+00:00",
         13.0, "{}", 60.0, "2024-10-27T01:23:20+00:00", 1730000600),
    ]
    conn.executemany(
        "INSERT INTO weekly_usage_snapshots ("
        "captured_at_utc, week_start_date, week_end_date, "
        "week_start_at, week_end_at, weekly_percent, payload_json, "
        "five_hour_percent, five_hour_resets_at, five_hour_window_key"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    # Seed 001+002 markers so the only stats-side migration that would
    # walk against five_hour_blocks is 003 (then the test-injection 004
    # fires harmlessly per CCTALLY_MIGRATION_TEST_MODE).
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('001_five_hour_block_models_backfill_v1', ?)",
        (TS_001_APPLIED,),
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('002_five_hour_block_projects_backfill_v1', ?)",
        (TS_002_APPLIED,),
    )
    conn.commit()
    conn.close()


def build_12_skipped_dedup_not_rerun(scenario_dir: Path) -> None:
    """Same forked-snapshot topology as scenario 11, but the operator has
    marked 003_merge_5h_block_duplicates_v1 as skipped via `cctally db
    skip`. Validates that open_db()'s post-backfill rerun honors the
    skip and does NOT back-door run the handler. Expected post-state:
    five_hour_blocks count == 2 (forked rows kept un-merged), and
    schema_migrations_skipped still carries 003 (operator's choice
    intact).
    """
    scenario_dir.mkdir(parents=True, exist_ok=True)
    stats = scenario_dir / "input.stats.db"
    if stats.exists():
        stats.unlink()
    conn = sqlite3.connect(stats)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    # Mirror cmd_db_skip's table shape (created lazily by that command).
    conn.execute(
        "CREATE TABLE schema_migrations_skipped ("
        "name TEXT PRIMARY KEY, "
        "skipped_at_utc TEXT NOT NULL, "
        "reason TEXT)"
    )
    conn.execute(
        """
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
        )
        """
    )
    rows = [
        ("2024-10-26T22:00:00+00:00", "2024-10-21", "2024-10-27",
         "2024-10-21T00:00:00+00:00", "2024-10-28T00:00:00+00:00",
         12.0, "{}", 50.0, "2024-10-27T01:13:20+00:00", 1730000000),
        ("2024-10-26T22:05:00+00:00", "2024-10-21", "2024-10-27",
         "2024-10-21T00:00:00+00:00", "2024-10-28T00:00:00+00:00",
         13.0, "{}", 60.0, "2024-10-27T01:23:20+00:00", 1730000600),
    ]
    conn.executemany(
        "INSERT INTO weekly_usage_snapshots ("
        "captured_at_utc, week_start_date, week_end_date, "
        "week_start_at, week_end_at, weekly_percent, payload_json, "
        "five_hour_percent, five_hour_resets_at, five_hour_window_key"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    # Mark 001+002 applied (same as scenario 11), and seed the skip
    # marker for 003. The dispatcher will honor the skip and advance
    # user_version when all migrations are applied OR skipped — so the
    # post-backfill rerun is the only path that could back-door the
    # handler. This scenario verifies it doesn't.
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('001_five_hour_block_models_backfill_v1', ?)",
        (TS_001_APPLIED,),
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('002_five_hour_block_projects_backfill_v1', ?)",
        (TS_002_APPLIED,),
    )
    conn.execute(
        "INSERT INTO schema_migrations_skipped VALUES "
        "('003_merge_5h_block_duplicates_v1', ?, ?)",
        (TS_003_APPLIED, "harness skip"),
    )
    conn.commit()
    conn.close()


def build_per_migration_001_dedup_highest_wins(scenario_dir: Path) -> None:
    """Per-migration goldens for cache migration ``001_dedup_highest_wins``.

    Emits two cache.db files:
      * pre.sqlite  — cache schema + 2 ``session_files`` rows + 3 synthetic
        ``session_entries`` "loser" rows (the streaming-intermediate shape:
        ``output_tokens=1``, no ``speed`` field) + a ``cache_meta``
        ``claude_ingest_walk_complete`` marker (a stale pre-dedup walk-complete
        sentinel that 001 must clear, cctally-dev#93 D5). schema_migrations
        is empty.
      * post.sqlite — same DB after running the production migration handler.
        Both seeded tables are empty; the ``cache_meta`` marker is CLEARED
        (001 DELETEs it atomically with the wipe); ``schema_migrations`` has
        one row ``(001_dedup_highest_wins, <iso>)``.

    Loaded by ``tests/test_migration_001_per_migration_goldens.py``.
    Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I4.
    """
    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Cache schema (mirror of bin/_fixture_builders.create_cache_db,
            # minus the codex side which is unaffected by this migration).
            conn.executescript(
                """
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

                CREATE TABLE schema_migrations (
                    name           TEXT PRIMARY KEY,
                    applied_at_utc TEXT NOT NULL
                );

                CREATE TABLE cache_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            # Seed a stale walk-complete marker so post.sqlite can
            # demonstrate 001's atomic marker-CLEAR (cctally-dev#93 D5):
            # a wiped session_entries must never coexist with a "complete
            # walk" marker.
            conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                ("claude_ingest_walk_complete", "2026-04-15T16:00:00Z"),
            )
            # Seed 2 session_files rows.
            conn.executemany(
                "INSERT INTO session_files "
                "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
                " session_id, project_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("/fake/.claude/projects/p1/sess-a.jsonl", 1024,
                     1_700_000_000_000_000_000, 1024,
                     "2026-04-15T15:00:00Z", "sess-a", "p1"),
                    ("/fake/.claude/projects/p1/sess-b.jsonl", 2048,
                     1_700_000_001_000_000_000, 2048,
                     "2026-04-15T15:00:00Z", "sess-b", "p1"),
                ],
            )
            # Seed 3 "loser" session_entries — streaming-intermediate rows
            # (output_tokens=1, no speed). These represent the bug state
            # the migration is designed to wipe.
            conn.executemany(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, msg_id, "
                " req_id, input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, usage_extra_json, cost_usd_raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("/fake/.claude/projects/p1/sess-a.jsonl", 100,
                     "2026-04-15T15:00:00Z", "claude-opus-4-7",
                     "m1", "r1", 100, 1, 0, 50, None, None),
                    ("/fake/.claude/projects/p1/sess-a.jsonl", 200,
                     "2026-04-15T15:00:10Z", "claude-opus-4-7",
                     "m2", "r2", 100, 2, 0, 50, None, None),
                    ("/fake/.claude/projects/p1/sess-b.jsonl", 100,
                     "2026-04-15T15:00:20Z", "claude-opus-4-7",
                     "m3", "r3", 100, 3, 0, 50, None, None),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        # Copy pre -> post, then run the handler against the copy.
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        # Load _cctally_db so we can call the registered handler.
        # Use SourceFileLoader-style import so the production handler is
        # the exact thing we exercise (no copy-paste drift).
        import importlib.util as ilu
        bin_dir = Path(__file__).resolve().parent
        spec = ilu.spec_from_file_location(
            "_cctally_db", bin_dir / "_cctally_db.py",
        )
        mod = ilu.module_from_spec(spec)
        # _cctally_db imports `_cctally_core`, which is on sys.path because
        # bin/ was prepended at the top of this script. Register in
        # sys.modules BEFORE exec_module so the @dataclass decorator can
        # look the module up (dataclasses.py walks cls.__module__ via
        # sys.modules during _process_class).
        sys.modules["_cctally_db"] = mod
        spec.loader.exec_module(mod)
        handler = None
        for m in mod._CACHE_MIGRATIONS:
            if m.name == "001_dedup_highest_wins":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("001_dedup_highest_wins not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            handler(conn)
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_008_recompute_weekly_cost_snapshots_dedup_fix(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for stats migration
    ``008_recompute_weekly_cost_snapshots_dedup_fix``.

    Emits THREE SQLite files (paired-DB pattern — first per-migration
    scenario to need it):
      * ``pre.sqlite``  — stats.db with 3 ``weekly_cost_snapshots`` rows:
        one ``(mode='auto', project=NULL)`` with a stale pre-fix cost
        (should be recomputed), one ``(mode='display', …)`` (must NOT
        change), one ``(mode='auto', project='myproj')`` (must NOT
        change). ``schema_migrations`` is empty for 008.
      * ``pre-cache.sqlite`` — cache.db sidecar with the 001 marker, the
        ``cache_meta`` ``claude_ingest_walk_complete`` marker (the new
        gate's walk✓ PROCEED signal, cctally-dev#93), a ``session_files``
        row (production-shape parity), and ONE ``session_entries`` row
        whose ``model='claude-opus-4-7'`` and ``output_tokens=1000``
        falls inside the auto-row's
        ``[range_start_iso, range_end_iso)`` window — recomputed cost
        is $0.025 at the embedded $25/Mtok opus output rate.
      * ``post.sqlite`` — same as pre.sqlite after running the
        production handler against the paired cache: row 1 updated to
        $0.025, rows 2 & 3 unchanged, ``schema_migrations`` carries the
        008 row.

    Loaded by ``tests/test_migration_008_per_migration_goldens.py``.
    Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3.
    """
    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre_stats = scenario_dir / "pre.sqlite"
    pre_cache = scenario_dir / "pre-cache.sqlite"
    post_stats = scenario_dir / "post.sqlite"

    # Stable timestamp used by the seeded post-001 ingest row so the
    # gate's Layer B (last_ingested_at > applied_at_utc) passes
    # deterministically.
    TS_001_APPLIED = "2026-05-22T00:00:00Z"
    TS_POST_001_INGEST = "2026-05-22T01:00:00Z"
    RANGE_START = "2026-05-15T00:00:00+00:00"
    RANGE_END = "2026-05-22T00:00:00+00:00"
    ENTRY_TS = "2026-05-18T00:00:00Z"

    def _build_pre_stats(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE schema_migrations (
                    name           TEXT PRIMARY KEY,
                    applied_at_utc TEXT NOT NULL
                );
                CREATE TABLE weekly_cost_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at_utc TEXT NOT NULL,
                    week_start_date TEXT NOT NULL,
                    week_end_date   TEXT NOT NULL,
                    week_start_at   TEXT,
                    week_end_at     TEXT,
                    range_start_iso TEXT,
                    range_end_iso   TEXT,
                    cost_usd        REAL NOT NULL,
                    source          TEXT NOT NULL DEFAULT 'cctally-range-cost',
                    mode            TEXT NOT NULL DEFAULT 'auto',
                    project         TEXT
                );
                """
            )
            conn.executemany(
                "INSERT INTO weekly_cost_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " range_start_iso, range_end_iso, cost_usd, mode, project) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [
                    # row 1 — auto/no-project: 100.0 is the pre-fix stale value.
                    (
                        "2026-05-22T00:00:00Z",
                        "2026-05-15", "2026-05-22",
                        RANGE_START, RANGE_END,
                        100.0, "auto", None,
                    ),
                    # row 2 — mode='display': 999.0 is user-supplied; preserve.
                    (
                        "2026-05-22T00:00:00Z",
                        "2026-05-15", "2026-05-22",
                        RANGE_START, RANGE_END,
                        999.0, "display", None,
                    ),
                    # row 3 — auto + project='myproj': per-project scope; preserve.
                    (
                        "2026-05-22T00:00:00Z",
                        "2026-05-15", "2026-05-22",
                        RANGE_START, RANGE_END,
                        50.0, "auto", "myproj",
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _build_pre_cache(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE schema_migrations (
                    name           TEXT PRIMARY KEY,
                    applied_at_utc TEXT NOT NULL
                );
                CREATE TABLE session_files (
                    path             TEXT PRIMARY KEY,
                    size_bytes       INTEGER NOT NULL,
                    mtime_ns         INTEGER NOT NULL,
                    last_byte_offset INTEGER NOT NULL,
                    last_ingested_at TEXT NOT NULL,
                    session_id       TEXT,
                    project_path     TEXT
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
                + CACHE_META_DDL
            )
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                ("001_dedup_highest_wins", TS_001_APPLIED),
            )
            # cache_meta walk-complete marker — the new gate's walk✓ PROCEED
            # signal (cctally-dev#93). Paired with the non-empty
            # session_entries seeded below, this is the row-6 PROCEED
            # topology. Replaces the old "post-001 session_files row" proof.
            conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                (WALK_COMPLETE_MARKER, TS_POST_001_INGEST),
            )
            # session_files row retained for production-shape parity (no
            # longer the gate signal post-cctally-dev#93).
            conn.execute(
                "INSERT INTO session_files "
                "(path, size_bytes, mtime_ns, last_byte_offset, "
                " last_ingested_at, session_id, project_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "/fake/.claude/projects/p1/sess-a.jsonl",
                    100, 1_700_000_000_000_000_000, 100,
                    TS_POST_001_INGEST, "sess-a", "p1",
                ),
            )
            # One entry inside [range_start, range_end): opus-4-7,
            # 1000 output tokens → $25/Mtok * 1000 = $0.025.
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " msg_id, req_id, input_tokens, output_tokens, "
                " cache_create_tokens, cache_read_tokens, "
                " usage_extra_json, cost_usd_raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "/fake/.claude/projects/p1/sess-a.jsonl", 0,
                    ENTRY_TS, "claude-opus-4-7",
                    "m1", "r1", 0, 1000, 0, 0, "{}", None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _build_post(pre_stats_path: Path, pre_cache_path: Path, dst: Path) -> None:
        # Copy pre_stats → post, then run the production handler against
        # the post.sqlite copy with the cache sidecar in place via
        # _cctally_core.CACHE_DB_PATH / CLAUDE_PROJECTS_DIR overrides.
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(pre_stats_path, dst)
        register_fixture_db(dst)
        # Load _cctally_db so we can call the registered handler. The
        # SourceFileLoader-style import ensures we exercise the production
        # handler (no copy-paste drift).
        import importlib.util as ilu
        bin_dir = Path(__file__).resolve().parent
        spec = ilu.spec_from_file_location(
            "_cctally_db", bin_dir / "_cctally_db.py",
        )
        mod = ilu.module_from_spec(spec)
        sys.modules["_cctally_db"] = mod
        spec.loader.exec_module(mod)

        # Override the path constants on the SAME _cctally_core module
        # instance that _cctally_db is using (mod._cctally_core), so the
        # migration's lookups find our fixtures. Save + restore so other
        # builders aren't affected.
        core = mod._cctally_core
        orig_cache_path = core.CACHE_DB_PATH
        orig_projects_dir = core.CLAUDE_PROJECTS_DIR
        # Synthetic JSONL on disk so the gate's empty-disk fallback
        # doesn't short-circuit before checking Layer B.
        projects_dir = scenario_dir / "_fake_projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        (projects_dir / "session1.jsonl").write_text("{}\n")

        handler = None
        for m in mod._STATS_MIGRATIONS:
            if m.name == "008_recompute_weekly_cost_snapshots_dedup_fix":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit(
                "008_recompute_weekly_cost_snapshots_dedup_fix not registered"
            )
        try:
            core.CACHE_DB_PATH = pre_cache_path
            core.CLAUDE_PROJECTS_DIR = projects_dir
            conn = sqlite3.connect(dst)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                handler(conn)
            finally:
                conn.close()
        finally:
            core.CACHE_DB_PATH = orig_cache_path
            core.CLAUDE_PROJECTS_DIR = orig_projects_dir
            # Clean up the synthetic projects dir — fixture stays
            # byte-stable across runs (no stray sibling tree).
            import shutil as _sh
            _sh.rmtree(projects_dir, ignore_errors=True)

    _build_pre_stats(pre_stats)
    _build_pre_cache(pre_cache)
    _build_post(pre_stats, pre_cache, post_stats)


def main() -> int:
    os.environ["TZ"] = "Etc/UTC"
    FIXTURES_ROOT.mkdir(parents=True, exist_ok=True)
    build_02_upgrade(FIXTURES_ROOT / "02-upgrade-with-some-applied")
    build_03_failure(FIXTURES_ROOT / "03-migration-raises-banner-renders")
    build_07_downgrade(FIXTURES_ROOT / "07-downgrade-detected-exit-2")
    build_09_both(FIXTURES_ROOT / "09-cache-db-and-stats-db")
    build_10_legacy_markers(FIXTURES_ROOT / "10-legacy-marker-recognized-by-db-status")
    build_11_five_hour_dedup_after_backfill(
        FIXTURES_ROOT / "11-five-hour-dedup-after-backfill"
    )
    build_12_skipped_dedup_not_rerun(
        FIXTURES_ROOT / "12-skipped-dedup-not-rerun-after-backfill"
    )
    build_per_migration_001_dedup_highest_wins(
        FIXTURES_ROOT / "per-migration" / "001_dedup_highest_wins"
    )
    build_per_migration_008_recompute_weekly_cost_snapshots_dedup_fix(
        FIXTURES_ROOT / "per-migration"
        / "008_recompute_weekly_cost_snapshots_dedup_fix"
    )
    print(f"Wrote fixtures to {FIXTURES_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
