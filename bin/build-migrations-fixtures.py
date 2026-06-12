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
  - per-migration/002_conversation_messages_backfill/{pre,post}.sqlite — cache.db
    pre/post for the conversation-viewer backfill (Plan 1). pre = existing install
    with empty conversation_messages; post = backfilled from a synthetic JSONL
    history (pointed at via CLAUDE_CONFIG_DIR; source_path normalized to a stable
    synthetic prefix so the committed golden is portable). Loaded by
    tests/test_migration_002_per_migration_goldens.py.
  - per-migration/003_conversation_reingest_tool_ids/{pre,post}.sqlite — cache.db
    pre/post for the #164 id-aware re-ingest. pre = existing install with an
    id-less conversation_messages row; post = the row UNCHANGED plus the
    conversation_reingest_pending flag + the 003 marker (flag-only handler — the
    clear+backfill run later in sync_cache under the flock). Loaded by
    tests/test_migration_003_per_migration_goldens.py.
  - per-migration/004_conversation_reingest_subagent_kind/{pre,post}.sqlite —
    cache.db pre/post for the #166 subagent-kind re-ingest. pre = existing
    install with 003 applied and an id-aware-but-kind-less conversation_messages
    row; post = the row UNCHANGED plus the conversation_reingest_pending flag
    (reused from 003) + the 004 marker (flag-only handler — the clear+backfill
    run later in sync_cache under the flock). Loaded by
    tests/test_migration_004_per_migration_goldens.py.
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
            cost_usd_raw        REAL,
            speed               TEXT
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
    # Note: post.sqlite's schema_migrations.applied_at_utc is a wall-clock
    # now_utc_iso() stamped by the real migration handler, so a rebuild churns
    # a few bytes there. The per-migration goldens test deliberately does NOT
    # assert applied_at_utc — this is expected, not a phantom dirty fixture.
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
                    cost_usd_raw        REAL,
                    speed               TEXT
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


def build_per_migration_011_budget_milestone_period_keys(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for stats migration
    ``011_budget_milestone_period_keys`` (#137).

    Emits two stats.db files:
      * pre.sqlite  — OLD-shape ``budget_milestones`` / ``codex_budget_milestones``
        / ``projected_milestones`` (no ``period`` column, narrow UNIQUE) +
        one seeded crossing each + ``schema_migrations`` rows for every
        production migration THROUGH 010 (so the fixture represents a
        fully-migrated pre-011 stats.db). 011's marker is absent.
      * post.sqlite — same DB after running the production 011 handler:
        each table gains a nullable ``period`` (historical rows -> NULL), the
        period-inclusive UNIQUE is in place, and the 011 marker is stamped.

    Loaded by ``tests/test_migration_011_per_migration_goldens.py``.
    Spec: docs/superpowers/specs/2026-06-05-budget-milestone-period-column-design.md.
    """
    # Note: post.sqlite's schema_migrations.applied_at_utc is a wall-clock
    # now_utc_iso() stamped by the real migration handler, so a rebuild churns
    # a few bytes there. The per-migration goldens test deliberately does NOT
    # assert applied_at_utc — this is expected, not a phantom dirty fixture.
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
            conn.executescript(
                """
                CREATE TABLE schema_migrations (
                    name           TEXT PRIMARY KEY,
                    applied_at_utc TEXT NOT NULL
                );
                CREATE TABLE budget_milestones (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start_at   TEXT    NOT NULL,
                    threshold       INTEGER NOT NULL,
                    budget_usd      REAL    NOT NULL,
                    spent_usd       REAL    NOT NULL,
                    consumption_pct REAL    NOT NULL,
                    crossed_at_utc  TEXT    NOT NULL,
                    alerted_at      TEXT,
                    UNIQUE(week_start_at, threshold)
                );
                CREATE TABLE codex_budget_milestones (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_start_at TEXT    NOT NULL,
                    threshold       INTEGER NOT NULL,
                    budget_usd      REAL    NOT NULL,
                    spent_usd       REAL    NOT NULL,
                    consumption_pct REAL    NOT NULL,
                    crossed_at_utc  TEXT    NOT NULL,
                    alerted_at      TEXT,
                    UNIQUE(period_start_at, threshold)
                );
                CREATE TABLE projected_milestones (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start_at   TEXT    NOT NULL,
                    metric          TEXT    NOT NULL,
                    threshold       INTEGER NOT NULL,
                    projected_value REAL    NOT NULL,
                    denominator     REAL    NOT NULL,
                    crossed_at_utc  TEXT    NOT NULL,
                    alerted_at      TEXT,
                    UNIQUE(week_start_at, metric, threshold)
                );
                """
            )
            # Seed schema_migrations with every production migration THROUGH
            # 010 (011 absent), so the pre.sqlite is a fully-migrated pre-011
            # stats.db. Imported from the real registry to avoid drift.
            for name in _PRE_011_PRODUCTION_MIGRATION_NAMES():
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations "
                    "(name, applied_at_utc) VALUES (?, ?)",
                    (name, "2026-05-22T00:00:00Z"),
                )
            conn.execute(
                "INSERT INTO budget_milestones (week_start_at, threshold, "
                "budget_usd, spent_usd, consumption_pct, crossed_at_utc, "
                "alerted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-06-01T00:00:00+00:00", 90, 300.0, 271.0, 90.3,
                 "2026-06-05T00:00:00Z", "2026-06-05T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO codex_budget_milestones (period_start_at, "
                "threshold, budget_usd, spent_usd, consumption_pct, "
                "crossed_at_utc, alerted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-06-01T00:00:00+00:00", 100, 200.0, 210.0, 105.0,
                 "2026-06-05T00:00:00Z", "2026-06-05T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO projected_milestones (week_start_at, metric, "
                "threshold, projected_value, denominator, crossed_at_utc, "
                "alerted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-06-01T00:00:00+00:00", "codex_budget_usd", 90, 195.0,
                 200.0, "2026-06-05T00:00:00Z", "2026-06-05T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        handler = None
        for m in _load_cctally_db_module()._STATS_MIGRATIONS:
            if m.name == "011_budget_milestone_period_keys":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("011_budget_milestone_period_keys not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            handler(conn)
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_012_unify_budget_milestones_vendor(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for stats migration
    ``012_unify_budget_milestones_vendor`` (#143).

    Emits two stats.db files:
      * pre.sqlite  — through-011 stats.db: v011-shape ``budget_milestones`` +
        ``codex_budget_milestones`` (period column, period-inclusive UNIQUE) +
        ``projected_milestones``, one seeded crossing in budget (vendor-to-be
        'claude'), one in codex (vendor-to-be 'codex'), and one projected row.
        ``schema_migrations`` carries every production migration THROUGH 011
        (012 absent), so the fixture represents a fully-migrated pre-012
        stats.db.
      * post.sqlite — same DB after running the production 012 handler: unified
        vendor-tagged ``budget_milestones`` (claude+codex rows), the Codex table
        dropped. (The 012 marker is NOT stamped by the handler — the dispatcher
        owns the central stamp per #140; the per-migration test does not assert
        it, matching the 011 golden.)

    Loaded by ``tests/test_migration_012_per_migration_goldens.py``.
    Spec: docs/superpowers/specs/2026-06-06-unify-budget-milestones-vendor-design.md.
    """
    # Note: like the 011 builder, post.sqlite's schema_migrations.applied_at_utc
    # is a wall-clock stamp on rebuild and SQLite stamps a writer-version in
    # bytes 96-99 on every write — register_fixture_db() zeroes the latter at
    # exit; the per-migration test deliberately does not assert applied_at_utc.
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
            conn.executescript(
                """
                CREATE TABLE schema_migrations (
                    name           TEXT PRIMARY KEY,
                    applied_at_utc TEXT NOT NULL
                );
                CREATE TABLE budget_milestones (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start_at   TEXT    NOT NULL,
                    period          TEXT,
                    threshold       INTEGER NOT NULL,
                    budget_usd      REAL    NOT NULL,
                    spent_usd       REAL    NOT NULL,
                    consumption_pct REAL    NOT NULL,
                    crossed_at_utc  TEXT    NOT NULL,
                    alerted_at      TEXT,
                    UNIQUE(week_start_at, period, threshold)
                );
                CREATE TABLE codex_budget_milestones (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_start_at TEXT    NOT NULL,
                    period          TEXT,
                    threshold       INTEGER NOT NULL,
                    budget_usd      REAL    NOT NULL,
                    spent_usd       REAL    NOT NULL,
                    consumption_pct REAL    NOT NULL,
                    crossed_at_utc  TEXT    NOT NULL,
                    alerted_at      TEXT,
                    UNIQUE(period_start_at, period, threshold)
                );
                CREATE TABLE projected_milestones (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start_at   TEXT    NOT NULL,
                    period          TEXT,
                    metric          TEXT    NOT NULL,
                    threshold       INTEGER NOT NULL,
                    projected_value REAL    NOT NULL,
                    denominator     REAL    NOT NULL,
                    crossed_at_utc  TEXT    NOT NULL,
                    alerted_at      TEXT,
                    UNIQUE(week_start_at, period, metric, threshold)
                );
                """
            )
            # Seed schema_migrations with every production migration THROUGH
            # 011 (012 absent), so the pre.sqlite is a fully-migrated pre-012
            # stats.db. Imported from the real registry to avoid drift.
            for name in _THROUGH_011_PRODUCTION_MIGRATION_NAMES():
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations "
                    "(name, applied_at_utc) VALUES (?, ?)",
                    (name, "2026-06-06T00:00:00Z"),
                )
            conn.execute(
                "INSERT INTO budget_milestones (week_start_at, period, "
                "threshold, budget_usd, spent_usd, consumption_pct, "
                "crossed_at_utc, alerted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-06-01T00:00:00+00:00", "subscription-week", 90, 100.0,
                 95.0, 95.0, "2026-06-02T00:00:00Z", "2026-06-02T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO codex_budget_milestones (period_start_at, period, "
                "threshold, budget_usd, spent_usd, consumption_pct, "
                "crossed_at_utc, alerted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-06-01T00:00:00+00:00", "calendar-month", 100, 200.0,
                 210.0, 105.0, "2026-06-03T00:00:00Z", "2026-06-03T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO projected_milestones (week_start_at, period, "
                "metric, threshold, projected_value, denominator, "
                "crossed_at_utc, alerted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-06-01T00:00:00+00:00", "subscription-week",
                 "codex_budget_usd", 90, 195.0, 200.0, "2026-06-03T00:00:00Z",
                 "2026-06-03T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        handler = None
        for m in _load_cctally_db_module()._STATS_MIGRATIONS:
            if m.name == "012_unify_budget_milestones_vendor":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("012_unify_budget_milestones_vendor not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            handler(conn)
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def _load_cctally_db_module():
    """Import the real ``_cctally_db`` module so fixture builders exercise the
    exact production migration handlers (no copy-paste drift)."""
    import importlib.util as ilu

    bin_dir = Path(__file__).resolve().parent
    spec = ilu.spec_from_file_location("_cctally_db", bin_dir / "_cctally_db.py")
    mod = ilu.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so the @dataclass decorator can
    # resolve the module via cls.__module__ during _process_class.
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _PRE_011_PRODUCTION_MIGRATION_NAMES():
    """Names of every production stats migration through 010 (011 excluded) —
    read from the real registry to stay drift-free."""
    return [
        m.name
        for m in _load_cctally_db_module()._STATS_MIGRATIONS
        if m.name != "011_budget_milestone_period_keys"
        and not m.name.endswith("_test_failure_injection")
    ]


def _THROUGH_011_PRODUCTION_MIGRATION_NAMES():
    """Names of every production stats migration through 011 (012 excluded) —
    read from the real registry to stay drift-free. Used to seed the 012
    per-migration pre.sqlite as a fully-migrated pre-012 stats.db (#143)."""
    return [
        m.name
        for m in _load_cctally_db_module()._STATS_MIGRATIONS
        if m.name != "012_unify_budget_milestones_vendor"
        and not m.name.endswith("_test_failure_injection")
    ]


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
    # Note: post.sqlite's schema_migrations.applied_at_utc is a wall-clock
    # now_utc_iso() stamped by the real migration handler, so a rebuild churns
    # a few bytes there. The per-migration goldens test deliberately does NOT
    # assert applied_at_utc — this is expected, not a phantom dirty fixture.
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
                    cost_usd_raw        REAL,
                    speed               TEXT
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


def build_per_migration_002_conversation_messages_backfill(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``002_conversation_messages_backfill`` (Plan 1 Task 5).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via
        ``_apply_cache_schema``, so it carries ``conversation_messages`` +
        ``conversation_fts`` + triggers + indexes), 2 ``session_files`` rows at
        EOF, 2 ``session_entries`` cost rows, the ``claude_ingest_walk_complete``
        marker — but an EMPTY ``conversation_messages``. This is the
        pre-feature shape of an existing install (cost already cached, no
        message index yet). ``schema_migrations`` does NOT contain 002.
      * ``post.sqlite`` — same DB after running the production 002 handler.
        Issue #139 deferred the JSONL walk to ``sync_cache``, so the handler now
        only sets the ``conversation_backfill_pending`` cache_meta flag and
        stamps the ``002_conversation_messages_backfill`` marker:
        ``conversation_messages`` stays EMPTY, the flag is set, the marker is
        present. (The flag-consume / actual backfill is covered by
        ``tests/test_conversation_ingest.py``, not this golden.)

    Loaded by ``tests/test_migration_002_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # Synthetic JSONL history. Two assistant turns + a human prompt across two
    # files. Since issue #139 the 002 handler no longer walks JSONL, so these
    # files only seed pre.sqlite's session_files ``size_bytes`` (their on-disk
    # sizes) — the post.sqlite handler run does not read them.
    import json as _json
    a_line = _json.dumps({
        "type": "assistant", "uuid": "a1", "sessionId": "s1",
        "requestId": "r1", "timestamp": "2026-04-15T15:00:00Z",
        "message": {"role": "assistant", "id": "m1",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "answer one"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    }) + "\n"
    u_line = _json.dumps({
        "type": "user", "uuid": "u2", "sessionId": "s1",
        "timestamp": "2026-04-15T15:01:00Z",
        "message": {"role": "user", "content": "next question"},
    }) + "\n"
    b_line = _json.dumps({
        "type": "assistant", "uuid": "b1", "sessionId": "s2",
        "requestId": "rb", "timestamp": "2026-04-15T15:02:00Z",
        "message": {"role": "assistant", "id": "mb",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "second session"}],
                    "usage": {"input_tokens": 8, "output_tokens": 4,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    }) + "\n"

    # The synthetic Claude tree; only its file sizes feed pre.sqlite now (the
    # handler no longer walks it — issue #139). Cleaned up after the build.
    claude_dir = scenario_dir / "_fake_claude"
    proj = claude_dir / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    file_a = proj / "sess-a.jsonl"
    file_b = proj / "sess-b.jsonl"
    file_a.write_text(a_line + u_line)
    file_b.write_text(b_line)

    # Stable synthetic source_path prefix written into the committed golden
    # (the real build-time path embeds this checkout's absolute location, which
    # is neither machine- nor rebuild-stable). The cost rows seeded in
    # pre.sqlite and the message rows normalized in post.sqlite both use it.
    _SYNTH_PROJ_PREFIX = "/fake/.claude/projects/-Users-u-proj/"

    def _load_cctally():
        """Load bin/cctally so sys.modules['cctally'] is populated (the
        backfill's _get_claude_data_dirs delegates to it) and return the
        cctally module + its _cctally_db sibling. bin/cctally has no .py
        suffix, so an explicit SourceFileLoader is required."""
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
        spec = ilu.spec_from_loader("cctally", loader)
        mod = ilu.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        return mod, sys.modules["_cctally_db"]

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        _cctally, db = _load_cctally()
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Production-parity cache schema (conversation_messages + FTS +
            # indexes + session_* + cache_meta + codex tables).
            db._apply_cache_schema(conn)
            # The migration framework's schema_migrations table (normally
            # created by the dispatcher). pre.sqlite must carry it (empty of
            # 002) so the handler's marker stamp lands on a real table — same
            # convention as the 001 golden.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            # Seed cost state at EOF — the pre-feature shape (cost ingested,
            # message index empty). Paths use the stable synthetic prefix (NOT
            # the absolute build path) so the committed golden is portable.
            # (Since issue #139 the 002 handler doesn't walk JSONL at all, so
            # these rows' paths never feed any walker.)
            size_a = file_a.stat().st_size
            size_b = file_b.stat().st_size
            synth_a = _SYNTH_PROJ_PREFIX + "sess-a.jsonl"
            synth_b = _SYNTH_PROJ_PREFIX + "sess-b.jsonl"
            conn.executemany(
                "INSERT INTO session_files "
                "(path, size_bytes, mtime_ns, last_byte_offset, "
                " last_ingested_at, session_id, project_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (synth_a, size_a, 1_700_000_000_000_000_000, size_a,
                     "2026-04-15T15:05:00Z", "s1", "-Users-u-proj"),
                    (synth_b, size_b, 1_700_000_001_000_000_000, size_b,
                     "2026-04-15T15:05:00Z", "s2", "-Users-u-proj"),
                ],
            )
            conn.executemany(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, msg_id, "
                " req_id, input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, usage_extra_json, cost_usd_raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (synth_a, 0, "2026-04-15T15:00:00Z",
                     "claude-opus-4-7", "m1", "r1", 10, 5, 0, 0, None, None),
                    (synth_b, 0, "2026-04-15T15:02:00Z",
                     "claude-opus-4-7", "mb", "rb", 8, 4, 0, 0, None, None),
                ],
            )
            # Walk-complete marker present (a normal cached install).
            conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                (WALK_COMPLETE_MARKER, "2026-04-15T15:05:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        _cctally, db = _load_cctally()
        handler = None
        for m in db._CACHE_MIGRATIONS:
            if m.name == "002_conversation_messages_backfill":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("002_conversation_messages_backfill not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Issue #139: the handler no longer walks JSONL inline — it sets the
            # ``conversation_backfill_pending`` cache_meta flag + self-stamps its
            # marker, deferring the offset-0 walk to sync_cache. So post.sqlite
            # carries an EMPTY conversation_messages + the flag + the marker
            # (no CLAUDE_CONFIG_DIR / no JSONL needed by the handler anymore).
            handler(conn)
            # Pin the marker timestamp so the committed golden is
            # rebuild-deterministic — the handler self-stamps with wall-clock
            # now_utc_iso(), which would otherwise churn a few bytes per rebuild.
            conn.execute(
                "UPDATE schema_migrations SET applied_at_utc = ? WHERE name = ?",
                ("2026-04-30T12:00:00Z",
                 "002_conversation_messages_backfill"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)
    # Clean up the synthetic Claude tree so the committed fixture dir holds only
    # pre.sqlite / post.sqlite. (Since issue #139 the 002 handler no longer
    # opens a flock sidecar, so there is no <post>.lock to remove.)
    import shutil as _sh
    _sh.rmtree(claude_dir, ignore_errors=True)


def build_per_migration_003_conversation_reingest_tool_ids(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``003_conversation_reingest_tool_ids`` (#164).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via
        ``_apply_cache_schema``), a ``schema_migrations`` table WITHOUT 003,
        and a single id-LESS ``conversation_messages`` row (the pre-#164
        shape: a tool_use block whose blocks_json carries no ``id`` /
        ``preview``). This is the existing-install shape before the re-ingest.
      * ``post.sqlite`` — same DB after running the production 003 handler.
        Because 003 is FLAG-ONLY (the destructive clear + offset-0 re-ingest
        run later in ``sync_cache`` under the ``cache.db.lock`` flock, NOT in
        the handler), post.sqlite carries the row UNCHANGED, plus
        ``cache_meta('conversation_reingest_pending','1')`` and the 003
        marker stamped. The flag-consume / actual re-ingest is covered by
        ``tests/test_migration_003_reingest.py``, not this golden.

    Loaded by ``tests/test_migration_003_per_migration_goldens.py``. Mirrors
    the 002 per-migration builder (flag-only handler, marker pinned).
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # An id-LESS tool_use block — the pre-#164 stored shape (no "id"/"preview").
    import json as _json
    _IDLESS_BLOCKS = _json.dumps(
        [{"kind": "tool_use", "name": "Read", "input_summary": "{}"}],
        separators=(",", ":"),
    )

    def _load_cctally():
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
        spec = ilu.spec_from_loader("cctally", loader)
        mod = ilu.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        return mod, sys.modules["_cctally_db"]

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        _cctally, db = _load_cctally()
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            db._apply_cache_schema(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            # One existing id-less conversation row (the pre-#164 shape).
            conn.execute(
                "INSERT INTO conversation_messages "
                "(session_id,uuid,source_path,byte_offset,timestamp_utc,"
                " entry_type,text,blocks_json,model,msg_id,req_id,is_sidechain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s1", "a1", "/fake/.claude/projects/-Users-u-proj/sess.jsonl",
                 0, "2026-04-15T15:00:00Z", "assistant", "",
                 _IDLESS_BLOCKS, "claude-opus-4-7", "m1", "r1", 0),
            )
            conn.commit()
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        _cctally, db = _load_cctally()
        handler = None
        for m in db._CACHE_MIGRATIONS:
            if m.name == "003_conversation_reingest_tool_ids":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("003_conversation_reingest_tool_ids not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets conversation_reingest_pending, leaves the
            # conversation_messages row UNCHANGED (the clear is deferred to
            # sync_cache). Then stamp the marker centrally (the dispatcher owns
            # the stamp per #140) with a PINNED timestamp so the committed
            # golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("003_conversation_reingest_tool_ids", "2026-04-30T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_004_conversation_reingest_subagent_kind(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``004_conversation_reingest_subagent_kind`` (#166).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via
        ``_apply_cache_schema``), a ``schema_migrations`` table carrying the
        ``003_conversation_reingest_tool_ids`` marker (003 already applied) but
        NOT 004, and a single ``conversation_messages`` row carrying an id-aware
        tool_use block but WITHOUT the #166 ``subagent_type`` field (the
        post-#164/pre-#166 shape of an existing install). This is the
        existing-install shape before the subagent-kind re-ingest.
      * ``post.sqlite`` — same DB after running the production 004 handler.
        Because 004 is FLAG-ONLY (it reuses 003's ``conversation_reingest_pending``
        flag; the destructive clear + offset-0 re-ingest run later in
        ``sync_cache`` under the ``cache.db.lock`` flock, NOT in the handler),
        post.sqlite carries the row UNCHANGED, plus
        ``cache_meta('conversation_reingest_pending','1')`` and the 004 marker
        stamped.

    Loaded by ``tests/test_migration_004_per_migration_goldens.py``. Mirrors
    the 003 per-migration builder (flag-only handler, marker pinned).
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # An id-aware tool_use block carrying NO subagent_type — the
    # post-#164/pre-#166 stored shape (has "id"/"preview", lacks the #166 field).
    import json as _json
    _PRE166_BLOCKS = _json.dumps(
        [{"kind": "tool_use", "name": "Read", "input_summary": "{}",
          "id": "toolu_x", "preview": "/a/b.py"}],
        separators=(",", ":"),
    )

    def _load_cctally():
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
        spec = ilu.spec_from_loader("cctally", loader)
        mod = ilu.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        return mod, sys.modules["_cctally_db"]

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        _cctally, db = _load_cctally()
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            db._apply_cache_schema(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            # 003 already applied — this is an existing install that has the
            # id-aware re-ingest but not yet the #166 subagent-kind one.
            conn.execute(
                "INSERT INTO schema_migrations(name, applied_at_utc) VALUES (?, ?)",
                ("003_conversation_reingest_tool_ids", "2026-04-30T12:00:00Z"),
            )
            # One existing id-aware-but-kind-less conversation row.
            conn.execute(
                "INSERT INTO conversation_messages "
                "(session_id,uuid,source_path,byte_offset,timestamp_utc,"
                " entry_type,text,blocks_json,model,msg_id,req_id,is_sidechain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s1", "a1", "/fake/.claude/projects/-Users-u-proj/sess.jsonl",
                 0, "2026-04-15T15:00:00Z", "assistant", "",
                 _PRE166_BLOCKS, "claude-opus-4-7", "m1", "r1", 0),
            )
            conn.commit()
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        _cctally, db = _load_cctally()
        handler = None
        for m in db._CACHE_MIGRATIONS:
            if m.name == "004_conversation_reingest_subagent_kind":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("004_conversation_reingest_subagent_kind not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets conversation_reingest_pending, leaves the
            # conversation_messages row UNCHANGED (the clear is deferred to
            # sync_cache). Then stamp the marker centrally (the dispatcher owns
            # the stamp per #140) with a PINNED timestamp so the committed
            # golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("004_conversation_reingest_subagent_kind", "2026-04-30T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_007_conversation_reingest_enrichment(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``007_conversation_reingest_enrichment`` (#177).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via
        ``_apply_cache_schema``, which already ALTER-adds the four enrichment
        columns), a ``schema_migrations`` table carrying 001-006 (an existing
        install at the 006 head) but NOT 007, the
        ``conversation_reingest_enrichment_pending`` flag UNSET, and a single
        conversation_messages row whose enrichment columns are at their default
        state (``search_aux=''``, ``stop_reason``/``attribution_*`` NULL) and
        whose blocks_json carries the post-#166 shape WITHOUT the #177 keys
        (no ``input``/``input_truncated`` on the tool_use, no ``full_length``
        on the tool_result) — the existing-install shape before the enrichment
        re-ingest.
      * ``post.sqlite`` — same DB after running the production 007 handler.
        Because 007 is FLAG-ONLY (it sets the DISTINCT
        ``conversation_reingest_enrichment_pending`` flag; the destructive
        clear + offset-0 re-ingest run later in ``sync_cache`` under the
        ``cache.db.lock`` flock, NOT in the handler), post.sqlite carries the
        row UNCHANGED, plus that flag SET and the 007 marker stamped (central
        dispatcher stamp, #140).

    Loaded by ``tests/test_migration_007_per_migration_goldens.py``. Mirrors
    the 003/004 per-migration builders (flag-only handler, marker pinned).
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # A post-#166 / pre-#177 stored block shape: an id-aware tool_use WITHOUT
    # the #177 input/input_truncated keys, and a tool_result WITHOUT full_length.
    import json as _json
    _PRE177_BLOCKS = _json.dumps(
        [{"kind": "tool_use", "name": "Read", "input_summary": "{}",
          "id": "toolu_x", "preview": "/a/b.py"}],
        separators=(",", ":"),
    )

    # The 006-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior conversation reingest but not yet the #177 enrichment one).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
    )

    def _load_cctally():
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
        spec = ilu.spec_from_loader("cctally", loader)
        mod = ilu.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        return mod, sys.modules["_cctally_db"]

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        _cctally, db = _load_cctally()
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            db._apply_cache_schema(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            for name in _PRIOR_CHAIN:
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at_utc) "
                    "VALUES (?, ?)",
                    (name, "2026-04-30T12:00:00Z"),
                )
            # One existing pre-#177 conversation row: the enrichment columns are
            # at their default state (search_aux='', stop_reason/attribution
            # NULL — populated only by the deferred re-ingest), and the
            # blocks_json lacks the #177 keys. The explicit column list omits the
            # enrichment columns so they take their schema defaults.
            conn.execute(
                "INSERT INTO conversation_messages "
                "(session_id,uuid,source_path,byte_offset,timestamp_utc,"
                " entry_type,text,blocks_json,model,msg_id,req_id,is_sidechain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s1", "a1", "/fake/.claude/projects/-Users-u-proj/sess.jsonl",
                 0, "2026-04-15T15:00:00Z", "assistant", "",
                 _PRE177_BLOCKS, "claude-opus-4-7", "m1", "r1", 0),
            )
            conn.commit()
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        _cctally, db = _load_cctally()
        handler = None
        for m in db._CACHE_MIGRATIONS:
            if m.name == "007_conversation_reingest_enrichment":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("007_conversation_reingest_enrichment not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets conversation_reingest_enrichment_pending,
            # leaves the conversation_messages row UNCHANGED (the clear is
            # deferred to sync_cache). Then stamp the marker centrally (the
            # dispatcher owns the stamp per #140) with a PINNED timestamp so the
            # committed golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("007_conversation_reingest_enrichment", "2026-04-30T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_008_session_entries_speed_backfill(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``008_session_entries_speed_backfill`` (#181).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via
        ``_apply_cache_schema``, which ALTER-adds the ``speed`` column so it is
        PRESENT but NULL on every legacy row), a ``schema_migrations`` table
        carrying 001-007 (an existing install at the 007 head) but NOT 008, and
        a single ``session_entries`` row whose ``usage_extra_json`` still holds
        the legacy ``{"speed":"fast"}`` blob while its ``speed`` column is NULL —
        the existing-install shape before the materialize-speed backfill.
      * ``post.sqlite`` — same DB after running the production 008 handler. The
        handler runs ONE C-side ``UPDATE … SET speed = json_extract(...)`` over
        rows where ``speed IS NULL AND usage_extra_json IS NOT NULL``, so
        post.sqlite carries ``speed='fast'`` with ``usage_extra_json``
        UNCHANGED (the handler never NULLs/rewrites the blob or VACUUMs), plus
        the 008 marker stamped (central dispatcher stamp, #140).

    Loaded by ``tests/test_cache_migration_008_per_migration_goldens.py``.
    Because the handler calls the cache handler ALONE (no schema-apply), the
    ``speed`` column MUST already exist in pre.sqlite (else the backfill UPDATE
    hits ``no such column: speed``); ``_apply_cache_schema`` guarantees that.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 007-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the #181 speed backfill).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
    )

    def _load_cctally():
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
        spec = ilu.spec_from_loader("cctally", loader)
        mod = ilu.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        return mod, sys.modules["_cctally_db"]

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        _cctally, db = _load_cctally()
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            db._apply_cache_schema(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            for name in _PRIOR_CHAIN:
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at_utc) "
                    "VALUES (?, ?)",
                    (name, "2026-06-12T12:00:00Z"),
                )
            # One legacy session_entries row: the speed column is NULL (the
            # materialize-speed migration had not yet run), while the legacy
            # usage_extra_json blob still carries {"speed":"fast"}. The explicit
            # column list omits `speed` so it takes its NULL schema default.
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " msg_id, req_id, input_tokens, output_tokens, "
                " cache_create_tokens, cache_read_tokens, "
                " usage_extra_json, cost_usd_raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("/fake/.claude/projects/-Users-u-proj/sess.jsonl", 0,
                 "2026-04-15T15:00:00Z", "claude-haiku-4-5", "m1", "r1",
                 200_000, 40_000, 0, 0, '{"speed": "fast"}', None),
            )
            conn.commit()
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        _cctally, db = _load_cctally()
        handler = None
        for m in db._CACHE_MIGRATIONS:
            if m.name == "008_session_entries_speed_backfill":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit(
                "008_session_entries_speed_backfill not registered"
            )
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Backfill handler: one UPDATE … SET speed = json_extract(...) over
            # rows where speed IS NULL AND usage_extra_json IS NOT NULL; the
            # usage_extra_json blob is left UNCHANGED. Then stamp the marker
            # centrally (the dispatcher owns the stamp per #140) with a PINNED
            # timestamp so the committed golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("008_session_entries_speed_backfill", "2026-06-12T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_009_conversation_media_reingest(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``009_conversation_media_reingest`` (#177 S4).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via
        ``_apply_cache_schema``), a ``schema_migrations`` table carrying 001-008
        (an existing install at the 008 head) but NOT 009, and no
        ``conversation_media_reingest_pending`` flag — the existing-install shape
        before the media/web reingest is armed.
      * ``post.sqlite`` — same DB after running the production 009 handler. 009 is
        flag-only: it sets ``cache_meta['conversation_media_reingest_pending']='1'``
        (so the resumable reingest backfills tool_result media[] placeholders +
        user-content media index + web_search/web_fetch captures onto history) and
        the dispatcher central-stamps the 009 marker (#140).

    Loaded by ``tests/test_cache_migration_009_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 008-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the #177 S4 media reingest).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
    )

    def _load_cctally():
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
        spec = ilu.spec_from_loader("cctally", loader)
        mod = ilu.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        return mod, sys.modules["_cctally_db"]

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        _cctally, db = _load_cctally()
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            db._apply_cache_schema(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            for name in _PRIOR_CHAIN:
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at_utc) "
                    "VALUES (?, ?)",
                    (name, "2026-06-12T12:00:00Z"),
                )
            conn.commit()
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        _cctally, db = _load_cctally()
        handler = None
        for m in db._CACHE_MIGRATIONS:
            if m.name == "009_conversation_media_reingest":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit(
                "009_conversation_media_reingest not registered"
            )
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets the conversation_media_reingest_pending
            # flag. Then stamp the marker centrally (the dispatcher owns the stamp
            # per #140) with a PINNED timestamp so the committed golden is
            # rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("009_conversation_media_reingest", "2026-06-12T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


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
    build_per_migration_002_conversation_messages_backfill(
        FIXTURES_ROOT / "per-migration" / "002_conversation_messages_backfill"
    )
    build_per_migration_003_conversation_reingest_tool_ids(
        FIXTURES_ROOT / "per-migration" / "003_conversation_reingest_tool_ids"
    )
    build_per_migration_004_conversation_reingest_subagent_kind(
        FIXTURES_ROOT / "per-migration" / "004_conversation_reingest_subagent_kind"
    )
    build_per_migration_007_conversation_reingest_enrichment(
        FIXTURES_ROOT / "per-migration" / "007_conversation_reingest_enrichment"
    )
    build_per_migration_008_session_entries_speed_backfill(
        FIXTURES_ROOT / "per-migration" / "008_session_entries_speed_backfill"
    )
    build_per_migration_009_conversation_media_reingest(
        FIXTURES_ROOT / "per-migration" / "009_conversation_media_reingest"
    )
    build_per_migration_008_recompute_weekly_cost_snapshots_dedup_fix(
        FIXTURES_ROOT / "per-migration"
        / "008_recompute_weekly_cost_snapshots_dedup_fix"
    )
    build_per_migration_011_budget_milestone_period_keys(
        FIXTURES_ROOT / "per-migration" / "011_budget_milestone_period_keys"
    )
    build_per_migration_012_unify_budget_milestones_vendor(
        FIXTURES_ROOT / "per-migration" / "012_unify_budget_milestones_vendor"
    )
    print(f"Wrote fixtures to {FIXTURES_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
