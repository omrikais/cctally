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
  - per-migration/{001_five_hour_block_models_backfill_v1,
    002_five_hour_block_projects_backfill_v1, 003_merge_5h_block_duplicates_v1,
    004_heal_forked_week_start_date_buckets, 007_observed_pre_credit_pct}/{pre,post}.sqlite
    — the STATS-five backfill goldens (#279 S7 W3). Pre reuses create_stats_db
    except 007 (hardcoded pre-007 week_reset_events sans observed_pre_credit_pct);
    001/002 isolate the backfill cache read to a throwaway work cache.
  - per-migration/{005_conversation_reingest_meta,
    006_conversation_reingest_source_tool_use_id}/{pre,post}.sqlite — the two
    flag-only CACHE re-ingest goldens (#279 S7 W3), same shape as 003/004.

Builder-less goldens: only STATS 005/006 (percent/five-hour reset_event_id) are
hand-built frozen artifacts with NO build_per_migration_* function — they are
intentionally outside the #197 byte-idempotency guard (see
tests/test_build_migrations_fixtures_stamps_markers.py).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import register_fixture_db, create_stats_db  # noqa: E402


FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "migrations"

# Pinned instant for the stats-five per-migration goldens (W3 backfill, #279 S7).
# Deterministic — never wall-clock — so a builder regen is byte-idempotent under
# the #197 guard (tests/test_build_migrations_fixtures_stamps_markers.py).
TS_STATS_FIVE_APPLIED = "2026-04-30T12:00:00Z"


def _load_db_module():
    """Load the FULL ``bin/cctally`` module so the production stats/cache
    migration handlers can be called directly, then return its
    ``_cctally_db`` sibling.

    Loading ``cctally`` (not just ``_cctally_db``) is required because the
    handlers reach back into the main module via the ``_cctally()`` accessor
    (e.g. ``_compute_block_totals`` / ``_FIVE_HOUR_JITTER_FLOOR_SECONDS``),
    which resolves ``sys.modules["cctally"]`` — unset when the #197 guard
    invokes a builder in isolation. Returns the ``_cctally_db`` module;
    ``mod._cctally_core`` / ``mod.now_utc_iso`` / ``mod._apply_cache_schema``
    / ``mod._STATS_MIGRATIONS`` are the handles the builders patch/read."""
    import importlib.util as ilu
    from importlib.machinery import SourceFileLoader
    bin_dir = Path(__file__).resolve().parent
    loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return sys.modules["_cctally_db"]


def _stats_handler(mod, name: str):
    for m in mod._STATS_MIGRATIONS:
        if m.name == name:
            return m.handler
    raise SystemExit(f"stats migration {name} not registered")

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
    # Note: cache 001 is the #140 "lone carve-out" — its handler self-stamps the
    # schema_migrations marker (atomic with the destructive wipe) using a
    # wall-clock now_utc_iso(). Left as-is that made post.sqlite the ONLY
    # non-deterministic per-migration golden (a rebuild churned the applied_at_utc
    # bytes). Issue #197: _build_post overwrites that stamp with a PINNED value
    # after the handler returns, so the committed golden is byte-idempotent like
    # every other per-migration golden. The production carve-out is untouched —
    # only the fixture's stamp is pinned. The per-migration goldens test does NOT
    # assert applied_at_utc's value (only its presence).
    TS_001_APPLIED_PIN = "2026-04-15T16:00:00Z"
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
            # #197: the handler self-stamped the 001 marker with a wall-clock
            # now_utc_iso() (the #140 cache-001 carve-out). Overwrite it with a
            # pinned value so the committed golden is byte-idempotent across
            # rebuilds. Production behavior is unchanged — only this fixture's
            # stamp is pinned; the per-migration test asserts the marker's
            # PRESENCE, not its applied_at_utc value.
            conn.execute(
                "UPDATE schema_migrations SET applied_at_utc = ? "
                "WHERE name = '001_dedup_highest_wins'",
                (TS_001_APPLIED_PIN,),
            )
            conn.commit()
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
    # Note: #140 moved the schema_migrations marker stamp out of the handler and
    # into the dispatcher's central _stamp_applied. Builders bypass the
    # dispatcher, so _build_post applies that stamp itself with a pinned
    # applied_at_utc (TS_008_APPLIED) — the marker is present (the per-migration
    # test asserts it) and the committed golden stays rebuild-deterministic
    # (issue #194). The test deliberately does not assert applied_at_utc's value.
    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre_stats = scenario_dir / "pre.sqlite"
    pre_cache = scenario_dir / "pre-cache.sqlite"
    post_stats = scenario_dir / "post.sqlite"

    # Stable timestamp used by the seeded post-001 ingest row so the
    # gate's Layer B (last_ingested_at > applied_at_utc) passes
    # deterministically.
    TS_001_APPLIED = "2026-05-22T00:00:00Z"
    TS_POST_001_INGEST = "2026-05-22T01:00:00Z"
    # Pinned marker stamp for 008 (applied after the post-001 ingest). The
    # dispatcher owns the central stamp post-#140; _build_post applies it
    # explicitly with this fixed value so the committed golden carries the
    # marker AND stays rebuild-deterministic (issue #194).
    TS_008_APPLIED = "2026-05-22T02:00:00Z"
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

        # The 008 handler eagerly opens cache.db for WRITE (cache 001's
        # dispatcher runs via _eagerly_apply_cache_migrations before the gate
        # check), so point CACHE_DB_PATH at a throwaway COPY — pointing it at the
        # in-tree pre-cache.sqlite would mutate that committed fixture (it would
        # gain every downstream cache marker plus wall-clock applied_at_utc
        # stamps), dirtying it on every rebuild (issue #194). Mirrors the
        # writable-copy pattern in
        # tests/test_migration_008_per_migration_goldens.py.
        work_cache = scenario_dir / "_work_cache.db"
        if work_cache.exists():
            work_cache.unlink()
        shutil.copy(pre_cache_path, work_cache)

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
            core.CACHE_DB_PATH = work_cache
            core.CLAUDE_PROJECTS_DIR = projects_dir
            conn = sqlite3.connect(dst)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                handler(conn)
                # #140: the handler no longer self-stamps its marker — the
                # dispatcher owns the central stamp via _stamp_applied. Builders
                # call the handler directly (bypassing the dispatcher), so apply
                # that same stamp here, mirroring
                # tests/test_migration_008_per_migration_goldens.py. Without it a
                # full rebuild produced a markerless post.sqlite and broke the
                # test (issue #194). The pinned applied_at_utc keeps the committed
                # golden rebuild-deterministic. _stamp_applied commits.
                mod._stamp_applied(
                    conn,
                    "008_recompute_weekly_cost_snapshots_dedup_fix",
                    TS_008_APPLIED,
                )
            finally:
                conn.close()
        finally:
            core.CACHE_DB_PATH = orig_cache_path
            core.CLAUDE_PROJECTS_DIR = orig_projects_dir
            # Clean up the synthetic projects dir — fixture stays
            # byte-stable across runs (no stray sibling tree).
            import shutil as _sh
            _sh.rmtree(projects_dir, ignore_errors=True)
            # Drop the throwaway cache copy (+ any WAL/SHM sidecars) so the
            # committed fixture dir holds only pre/pre-cache/post .sqlite.
            for _suffix in ("", "-wal", "-shm"):
                _p = Path(str(work_cache) + _suffix)
                if _p.exists():
                    _p.unlink()

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
            # #140: the handler no longer self-stamps its schema_migrations
            # marker — the dispatcher owns the central stamp via _stamp_applied,
            # which it calls right after the handler returns cleanly. Fixture
            # builders bypass the dispatcher (they invoke the handler directly),
            # so we apply that same central stamp here, mirroring
            # tests/test_migration_002_per_migration_goldens.py. Before this, the
            # stamp was an UPDATE that silently matched zero rows post-#140, so a
            # full rebuild produced a markerless post.sqlite and broke the test
            # (issue #194). The pinned applied_at_utc makes the stamp itself
            # deterministic and matches the value HEAD's committed 002 golden
            # already carries, so a regen reproduces the marker exactly. (The
            # wider _apply_cache_schema schema drift — newer tables landing in
            # this golden as later migrations add them — was out of scope for
            # #194 and is resolved by #197: the committed goldens are refreshed
            # to the current schema and a byte-idempotency guard
            # [test_build_migrations_fixtures_stamps_markers.py] keeps them
            # current.) _stamp_applied commits.
            db._stamp_applied(
                conn, "002_conversation_messages_backfill",
                "2026-04-30T12:00:00Z",
            )
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
        state (``stop_reason``/``attribution_*`` NULL) and
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
            # at their default state (stop_reason/attribution NULL — populated
            # only by the deferred re-ingest), and the blocks_json lacks the
            # #177 keys. The explicit column list omits the enrichment columns so
            # they take their schema defaults.
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


def build_per_migration_010_conversation_search_split(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``010_conversation_search_split`` (#177 S6).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via
        ``_apply_cache_schema``) torn down to the LEGACY FTS shape (single-column
        ``conversation_fts(text)`` + ``conversation_fts_aux`` + legacy triggers;
        the ``search_tool``/``search_thinking`` base columns exist but are empty),
        a ``schema_migrations`` table carrying 001-009 (an existing install at the
        009 head) but NOT 010, and no ``conversation_search_split_pending`` flag —
        the existing-install shape before the search-column split is armed.
      * ``post.sqlite`` — same DB after running the production 010 handler. 010 is
        flag-only: it sets ``cache_meta['conversation_search_split_pending']='1'``
        (so sync_cache backfills search_tool/search_thinking from blocks_json then
        swaps the legacy FTS to the split shape under the flock) and the dispatcher
        central-stamps the 010 marker (#140).

    Loaded by ``tests/test_cache_migration_010_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 009-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the #177 S6 search split).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
    )

    def _load_cctally():
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
        spec = ilu.spec_from_loader("cctally", loader)
        mod = ilu.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        return mod, sys.modules["_cctally_db"]

    def _to_legacy_shape(conn, db) -> None:
        """Revert the fresh split FTS shape to the legacy prose+aux two-table
        shape (so 010's backfill/swap has work to do). FTS5-less builds: no-op
        (no vtable exists)."""
        if not db._fts5_available(conn):
            return
        db._drop_conversation_fts_triggers(conn)
        conn.execute("DROP TABLE IF EXISTS conversation_fts")
        conn.execute("DROP TABLE IF EXISTS conversation_fts_aux")
        conn.execute("CREATE VIRTUAL TABLE conversation_fts "
                     "USING fts5(text, content='conversation_messages', "
                     "content_rowid='id')")
        db._create_conversation_fts_aux_table(conn)
        db._create_conversation_fts_legacy_triggers(conn)

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        _cctally, db = _load_cctally()
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            db._apply_cache_schema(conn)
            _to_legacy_shape(conn, db)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            for name in _PRIOR_CHAIN:
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at_utc) "
                    "VALUES (?, ?)",
                    (name, "2026-06-13T12:00:00Z"),
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
            if m.name == "010_conversation_search_split":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit(
                "010_conversation_search_split not registered"
            )
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets the conversation_search_split_pending flag.
            # Then stamp the marker centrally (the dispatcher owns the stamp per
            # #140) with a PINNED timestamp so the committed golden is rebuild-
            # deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("010_conversation_search_split", "2026-06-13T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_011_conversation_promote_command_args(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``011_conversation_promote_command_args`` (#188 bug 4).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via ``_apply_cache_schema``)
        with a ``schema_migrations`` table carrying cache migrations 001-010 (an
        existing install at the 010 head) but NOT 011, and no
        ``conversation_promote_command_args_pending`` flag — the existing-install
        shape before the command-args promotion is armed. Seeds ONE legacy
        ``entry_type='meta'`` conversation_messages row whose blocks_json is a
        promotable slash-command marker (non-empty ``<command-args>``), so the
        flock-held consumer has a real row to flip.
      * ``post.sqlite`` — same DB after running the production 011 handler. 011 is
        flag-only: it sets
        ``cache_meta['conversation_promote_command_args_pending']='1'`` (so
        sync_cache flips legacy meta command rows to entry_type='human' with
        text=args + recomputes split search columns under the flock) and the
        dispatcher central-stamps the 011 marker (#140). The flag-only handler
        does NOT touch the seeded data row (the swap is sync-side).

    Loaded by ``tests/test_cache_migration_011_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 010-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the #188 bug-4 promotion).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
        "010_conversation_search_split",
    )

    # A legacy META command row whose <command-args> carry a real user prompt —
    # exactly the shape the consumer flips to entry_type='human' (text=args).
    _LEGACY_MARKER = (
        "<command-name>/review</command-name>"
        "<command-args>Review feat/x vs main.</command-args>"
    )
    _LEGACY_BLOCKS = json.dumps([{"kind": "text", "text": _LEGACY_MARKER}])

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
                    (name, "2026-06-13T12:00:00Z"),
                )
            # One legacy META command row the consumer will later promote
            # (entry_type='meta', text='', blocks_json = the raw marker). The
            # flag-only 011 handler leaves it untouched; the flock-held
            # _consume_promote_command_args (sync-side) is what flips it.
            conn.execute(
                "INSERT INTO conversation_messages "
                "(uuid, session_id, source_path, byte_offset, timestamp_utc, "
                " entry_type, text, blocks_json) "
                "VALUES (?, ?, ?, ?, ?, 'meta', '', ?)",
                ("u1", "s1", "/x/agent-s1.jsonl", 0, "2026-06-13T00:00:00Z",
                 _LEGACY_BLOCKS),
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
            if m.name == "011_conversation_promote_command_args":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit(
                "011_conversation_promote_command_args not registered"
            )
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets conversation_promote_command_args_pending.
            # Then stamp the marker centrally (the dispatcher owns the stamp per
            # #140) with a PINNED timestamp so the committed golden is rebuild-
            # deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("011_conversation_promote_command_args", "2026-06-13T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_012_create_conversation_ai_titles(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``012_create_conversation_ai_titles`` (#193).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via ``_apply_cache_schema``)
        with a ``schema_migrations`` table carrying cache migrations 001-011 (an
        existing install at the 011 head) but NOT 012, and no
        ``ai_titles_backfill_pending`` flag — the existing-install shape before the
        ai-title backfill is armed.
      * ``post.sqlite`` — same DB after running the production 012 handler. 012 is
        flag-only: it sets ``cache_meta['ai_titles_backfill_pending']='1'`` (so
        sync_cache walks all history once via backfill_ai_titles under the flock)
        and the dispatcher central-stamps the 012 marker (#140). The flag-only
        handler does NO data work; the conversation_ai_titles table itself is
        created by _apply_cache_schema, so it is present in BOTH pre and post.

    Loaded by ``tests/test_cache_migration_012_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 011-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the #193 ai-title backfill).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
        "010_conversation_search_split",
        "011_conversation_promote_command_args",
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
                    (name, "2026-06-14T12:00:00Z"),
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
            if m.name == "012_create_conversation_ai_titles":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit(
                "012_create_conversation_ai_titles not registered"
            )
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets ai_titles_backfill_pending. Then stamp the
            # marker centrally (#140) with a PINNED timestamp so the committed
            # golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("012_create_conversation_ai_titles", "2026-06-14T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_013_create_conversation_sessions(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``013_create_conversation_sessions`` (browse-rail rollup).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via ``_apply_cache_schema``)
        with a ``schema_migrations`` table carrying cache migrations 001-012 (an
        existing install at the 012 head) but NOT 013, and no
        ``conversation_sessions_backfill_pending`` flag — the existing-install
        shape before the rollup backfill is armed.
      * ``post.sqlite`` — same DB after running the production 013 handler. 013 is
        flag-only: it sets ``cache_meta['conversation_sessions_backfill_pending']
        ='1'`` (so sync_cache does the one-time full GROUP BY recompute under the
        flock) and the dispatcher central-stamps the 013 marker (#140). The
        flag-only handler does NO data work; the conversation_sessions table
        itself is created by _apply_cache_schema, so it is present in BOTH pre and
        post (and empty in both — the recompute is sync-side).

    Loaded by ``tests/test_cache_migration_013_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 012-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the browse-rail rollup).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
        "010_conversation_search_split",
        "011_conversation_promote_command_args",
        "012_create_conversation_ai_titles",
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
                    (name, "2026-06-14T12:00:00Z"),
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
            if m.name == "013_create_conversation_sessions":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit(
                "013_create_conversation_sessions not registered"
            )
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets conversation_sessions_backfill_pending. Then
            # stamp the marker centrally (#140) with a PINNED timestamp so the
            # committed golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("013_create_conversation_sessions", "2026-06-14T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_014_conversation_queued_prompt_reingest(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``014_conversation_queued_prompt_reingest`` (queued-while-busy user prompts).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via ``_apply_cache_schema``)
        with ``schema_migrations`` carrying cache migrations 001-013 (an existing
        install at the 013 head) but NOT 014, and no
        ``conversation_queued_prompt_reingest_pending`` flag — the existing-install
        shape before the reingest is armed.
      * ``post.sqlite`` — same DB after running the production 014 handler. 014 is
        flag-only: it sets ``cache_meta['conversation_queued_prompt_reingest_pending']
        ='1'`` (so sync_cache does the #179 resumable per-file reingest, re-parsing
        every JSONL through the parser that now promotes queued_command prompts to
        HUMAN) and the dispatcher central-stamps the 014 marker (#140). The handler
        does NO data work.

    Loaded by ``tests/test_cache_migration_014_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 013-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the queued-prompt reingest).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
        "010_conversation_search_split",
        "011_conversation_promote_command_args",
        "012_create_conversation_ai_titles",
        "013_create_conversation_sessions",
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
                    (name, "2026-06-14T12:00:00Z"),
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
            if m.name == "014_conversation_queued_prompt_reingest":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit(
                "014_conversation_queued_prompt_reingest not registered"
            )
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets conversation_queued_prompt_reingest_pending.
            # Then stamp the marker centrally (#140) with a PINNED timestamp so the
            # committed golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("014_conversation_queued_prompt_reingest", "2026-06-14T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_015_conversation_sessions_filter_columns(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``015_conversation_sessions_filter_columns`` (browse-rail filter columns).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via ``_apply_cache_schema``)
        with the ``conversation_sessions`` rollup TORN DOWN to its legacy
        four-column shape (session_id/msg_count/started_utc/last_activity_utc —
        WITHOUT the three filter columns ``_apply_cache_schema`` now emits), a
        ``schema_migrations`` table carrying cache migrations 001-014 (an existing
        install at the 014 head) but NOT 015, and no
        ``conversation_sessions_backfill_pending`` flag — the existing-install
        shape before the filter columns are added.
      * ``post.sqlite`` — same DB after running the production 015 handler. 015
        ALTER-adds ``project_label``/``cost_usd``/``cache_rebuild_count`` and sets
        ``cache_meta['conversation_sessions_backfill_pending']='1'`` (so the next
        sync_cache full recompute fills them) and the dispatcher central-stamps
        the 015 marker (#140). The handler does no per-session backfill — the
        heavy cache_rebuild_count derive rides the sync-side recompute (mirroring
        013).

    Loaded by ``tests/test_cache_migration_015_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 014-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the browse-rail filter columns).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
        "010_conversation_search_split",
        "011_conversation_promote_command_args",
        "012_create_conversation_ai_titles",
        "013_create_conversation_sessions",
        "014_conversation_queued_prompt_reingest",
    )

    def _load_cctally():
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
        spec = ilu.spec_from_loader("cctally", loader)
        mod = ilu.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        return mod, sys.modules["_cctally_db"]

    def _to_legacy_conversation_sessions_shape(conn) -> None:
        """Revert the fresh seven-column rollup to its legacy four-column shape
        (so 015's ALTERs have columns to add). The index is recreated to match
        the production DDL (its definition is unchanged across 015)."""
        conn.execute("DROP TABLE IF EXISTS conversation_sessions")
        conn.execute(
            "CREATE TABLE conversation_sessions ("
            "session_id        TEXT NOT NULL PRIMARY KEY, "
            "msg_count         INTEGER NOT NULL DEFAULT 0, "
            "started_utc       TEXT, "
            "last_activity_utc TEXT)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_sessions_recent "
            "ON conversation_sessions(last_activity_utc DESC, session_id DESC)"
        )

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        _cctally, db = _load_cctally()
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            db._apply_cache_schema(conn)
            _to_legacy_conversation_sessions_shape(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            for name in _PRIOR_CHAIN:
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at_utc) "
                    "VALUES (?, ?)",
                    (name, "2026-06-14T12:00:00Z"),
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
            if m.name == "015_conversation_sessions_filter_columns":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit(
                "015_conversation_sessions_filter_columns not registered"
            )
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # ALTER-adds the three filter columns + arms the backfill flag. Then
            # stamp the marker centrally (#140) with a PINNED timestamp so the
            # committed golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("015_conversation_sessions_filter_columns",
                 "2026-06-14T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_016_drop_search_aux(scenario_dir: Path) -> None:
    """Per-migration goldens for cache migration ``016_drop_search_aux``
    (#217 S1 / U7a).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via ``_apply_cache_schema``,
        which after #217 NO LONGER emits ``search_aux``), a ``schema_migrations``
        table carrying cache migrations 001-015 (an existing install at the 015
        head) but NOT 016 — the post-search-split, post-#217 install shape.
      * ``post.sqlite`` — same DB after running the production 016 handler. The
        builder golden is a CLEAN NO-OP (spec #197 note): ``pre.sqlite``'s schema
        already lacks ``search_aux`` (sourced from the current
        ``_apply_cache_schema``), so 016's column-presence guard skips-as-applied
        without dropping anything; the dispatcher central-stamps the 016 marker
        (#140). The ACTUAL drop on a column-carrying DB is proven by the dedicated
        unit test ``tests/test_migration_016_drop_search_aux.py`` (which manually
        ``ADD COLUMN search_aux`` rather than via ``_apply_cache_schema``), plus a
        defer-on-pending-split regression there.

    Loaded by ``tests/test_cache_migration_016_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 015-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the search_aux drop).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
        "010_conversation_search_split",
        "011_conversation_promote_command_args",
        "012_create_conversation_ai_titles",
        "013_create_conversation_sessions",
        "014_conversation_queued_prompt_reingest",
        "015_conversation_sessions_filter_columns",
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
                    (name, "2026-06-20T12:00:00Z"),
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
            if m.name == "016_drop_search_aux":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("016_drop_search_aux not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Clean no-op handler (pre already lacks search_aux). Then stamp the
            # marker centrally (#140) with a PINNED timestamp so the committed
            # golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("016_drop_search_aux", "2026-06-20T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_017_arm_nested_agent_reingest(scenario_dir: Path) -> None:
    """Per-migration goldens for cache migration ``017_arm_nested_agent_reingest``
    (#217 S1 / U6 — re-link >16 KB nested-subagent grandchildren).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via ``_apply_cache_schema``)
        with ``schema_migrations`` carrying cache migrations 001-016 (an existing
        install at the 016 head) but NOT 017, and no
        ``conversation_reingest_nested_agent_pending`` flag — the existing-install
        shape before the nested-agent reingest is armed.
      * ``post.sqlite`` — same DB after running the production 017 handler. 017 is
        flag-only: it sets ``cache_meta['conversation_reingest_nested_agent_pending']
        ='1'`` (so the #179 resumable per-file reingest re-parses every JSONL
        through the parser that now stamps a structured agent_id at INGEST for
        nested grandchildren whose agentId: trailer was clipped past 16 KB) and the
        dispatcher central-stamps the 017 marker (#140). The handler does NO data
        work and NEVER re-arms the shared ``conversation_reingest_pending`` flag.

    Loaded by ``tests/test_cache_migration_017_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 016-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the nested-agent reingest).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
        "010_conversation_search_split",
        "011_conversation_promote_command_args",
        "012_create_conversation_ai_titles",
        "013_create_conversation_sessions",
        "014_conversation_queued_prompt_reingest",
        "015_conversation_sessions_filter_columns",
        "016_drop_search_aux",
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
                    (name, "2026-06-20T12:00:00Z"),
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
            if m.name == "017_arm_nested_agent_reingest":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("017_arm_nested_agent_reingest not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets conversation_reingest_nested_agent_pending.
            # Then stamp the marker centrally (#140) with a PINNED timestamp so the
            # committed golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("017_arm_nested_agent_reingest", "2026-06-20T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_018_create_conversation_title_fts(scenario_dir: Path) -> None:
    """Per-migration goldens for cache migration ``018_create_conversation_title_fts``
    (#217 S2 / E7 — title FTS over conversation_ai_titles).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via ``_apply_cache_schema``)
        with ``schema_migrations`` carrying cache migrations 001-017 (an existing
        install at the 017 head) but NOT 018, and no
        ``conversation_title_fts_backfill_pending`` flag — the existing-install
        shape before the title FTS is armed.
      * ``post.sqlite`` — same DB after running the production 018 handler. 018 is
        flag-only: it sets ``cache_meta['conversation_title_fts_backfill_pending']
        ='1'`` (so the next flock-held full sync runs ``_consume_title_fts`` — an
        FTS5 ``'rebuild'`` — to populate the external-content title index from
        existing history) and the dispatcher central-stamps the 018 marker (#140).
        The handler does NO data work and NEVER re-arms any reingest flag (P1-2:
        the title flag joins ``_TARGETED_DECLINE_FLAGS`` only).

    Loaded by ``tests/test_cache_migration_018_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 017-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the title FTS).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
        "010_conversation_search_split",
        "011_conversation_promote_command_args",
        "012_create_conversation_ai_titles",
        "013_create_conversation_sessions",
        "014_conversation_queued_prompt_reingest",
        "015_conversation_sessions_filter_columns",
        "016_drop_search_aux",
        "017_arm_nested_agent_reingest",
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
                    (name, "2026-06-20T12:00:00Z"),
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
            if m.name == "018_create_conversation_title_fts":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("018_create_conversation_title_fts not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets conversation_title_fts_backfill_pending. Then
            # stamp the marker centrally (#140) with a PINNED timestamp so the
            # committed golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("018_create_conversation_title_fts", "2026-06-20T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_019_create_conversation_file_touches(scenario_dir: Path) -> None:
    """Per-migration goldens for cache migration ``019_create_conversation_file_touches``
    (#217 S2 / I-3 — file-path search axis over conversation_file_touches).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via ``_apply_cache_schema``)
        with ``schema_migrations`` carrying cache migrations 001-018 (an existing
        install at the 018 head) but NOT 019, and no
        ``conversation_reingest_file_touches_pending`` flag — the existing-install
        shape before the file-path axis is armed.
      * ``post.sqlite`` — same DB after running the production 019 handler. 019 is
        flag-only: it sets
        ``cache_meta['conversation_reingest_file_touches_pending']='1'`` (so the
        next flock-held full sync runs ``_consume_file_touches`` to derive
        conversation_file_touches from existing blocks_json history) and the
        dispatcher central-stamps the 019 marker (#140). The handler does NO data
        work and NEVER arms any reingest flag (P1-2: the file-touch flag joins
        ``_TARGETED_DECLINE_FLAGS`` only).

    Loaded by ``tests/test_cache_migration_019_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 018-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the file-path axis).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
        "010_conversation_search_split",
        "011_conversation_promote_command_args",
        "012_create_conversation_ai_titles",
        "013_create_conversation_sessions",
        "014_conversation_queued_prompt_reingest",
        "015_conversation_sessions_filter_columns",
        "016_drop_search_aux",
        "017_arm_nested_agent_reingest",
        "018_create_conversation_title_fts",
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
                    (name, "2026-06-20T12:00:00Z"),
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
            if m.name == "019_create_conversation_file_touches":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("019_create_conversation_file_touches not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Flag-only handler: sets conversation_reingest_file_touches_pending. Then
            # stamp the marker centrally (#140) with a PINNED timestamp so the
            # committed golden is rebuild-deterministic.
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("019_create_conversation_file_touches", "2026-06-20T12:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_020_session_entries_physical_unique(scenario_dir: Path) -> None:
    """Per-migration goldens for cache migration ``020_session_entries_physical_unique``
    (#279 S3 F3 — the physical-key UNIQUE backstop on session_entries).

    Emits two cache.db files:
      * ``pre.sqlite``  — full production cache schema (via ``_apply_cache_schema``)
        with the ``idx_entries_physical`` index DROPPED (a genuine pre-020 DB
        lacks it — that is what makes seeding physical-key duplicates possible),
        ``schema_migrations`` carrying cache migrations 001-019 (an existing
        install at the 019 head), and seeded session_entries: two NULL-keyed rows
        sharing ``(source_path, line_offset)`` (the audit's stated gap — NULL keys
        bypass the logical dedup index) + a keyed pair sharing the same physical
        slot but with DISTINCT ``(msg_id, req_id)`` (the content-rewrite/offset-
        regression class) + one clean row.
      * ``post.sqlite`` — same DB after running the production 020 handler:
        keep-first-id dedup (MIN(id) per physical key) collapses each dup group,
        the ``idx_entries_physical`` UNIQUE index is (re)created, and the
        dispatcher's 020 marker is stamped (reproduced here with a PINNED
        applied_at_utc so the committed golden is rebuild-deterministic, #197).

    Loaded by ``tests/test_cache_migration_020_per_migration_goldens.py``.
    """
    import importlib.util as ilu

    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    bin_dir = Path(__file__).resolve().parent

    # The 019-head prior chain stamped into pre.sqlite (an existing install that
    # has every prior cache migration but not yet the physical-unique backstop).
    _PRIOR_CHAIN = (
        "001_dedup_highest_wins",
        "002_conversation_messages_backfill",
        "003_conversation_reingest_tool_ids",
        "004_conversation_reingest_subagent_kind",
        "005_conversation_reingest_meta",
        "006_conversation_reingest_source_tool_use_id",
        "007_conversation_reingest_enrichment",
        "008_session_entries_speed_backfill",
        "009_conversation_media_reingest",
        "010_conversation_search_split",
        "011_conversation_promote_command_args",
        "012_create_conversation_ai_titles",
        "013_create_conversation_sessions",
        "014_conversation_queued_prompt_reingest",
        "015_conversation_sessions_filter_columns",
        "016_drop_search_aux",
        "017_arm_nested_agent_reingest",
        "018_create_conversation_title_fts",
        "019_create_conversation_file_touches",
    )

    _SEED_INSERT = (
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, msg_id, req_id, "
        " input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, "
        " usage_extra_json, speed, cost_usd_raw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    # (source_path, line_offset, ts, model, msg_id, req_id, in, out, cc, cr).
    # Insertion order fixes the AUTOINCREMENT ids so MIN(id) is deterministic.
    _SEED_ROWS = [
        # NULL-keyed physical dup (id 1 kept, id 2 dropped).
        ("/fake/.claude/projects/p1/nullkeyed.jsonl", 0, "2026-07-01T10:00:00Z",
         "claude-opus-4-8", None, None, 1, 1, 0, 0, None, None, None),
        ("/fake/.claude/projects/p1/nullkeyed.jsonl", 0, "2026-07-01T11:00:00Z",
         "claude-opus-4-8", None, None, 2, 2, 0, 0, None, None, None),
        # Keyed pair sharing the physical slot with DISTINCT logical keys
        # (id 3 kept, id 4 dropped).
        ("/fake/.claude/projects/p1/keyed.jsonl", 0, "2026-07-01T12:00:00Z",
         "claude-opus-4-8", "k1", "k1r", 3, 3, 0, 0, None, None, None),
        ("/fake/.claude/projects/p1/keyed.jsonl", 0, "2026-07-01T13:00:00Z",
         "claude-opus-4-8", "k2", "k2r", 4, 4, 0, 0, None, None, None),
        # Clean, uniquely-keyed row (id 5 kept).
        ("/fake/.claude/projects/p1/clean.jsonl", 0, "2026-07-01T14:00:00Z",
         "claude-opus-4-8", "c1", "c1r", 5, 5, 0, 0, None, None, None),
    ]

    TS_020_APPLIED_PIN = "2026-07-09T12:00:00Z"

    def _load_db():
        spec = ilu.spec_from_file_location("_cctally_db", bin_dir / "_cctally_db.py")
        mod = ilu.module_from_spec(spec)
        sys.modules["_cctally_db"] = mod
        spec.loader.exec_module(mod)
        return mod

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        db = _load_db()
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            db._apply_cache_schema(conn)
            # A genuine pre-020 DB lacks the physical index — drop it so we can
            # seed physical-key duplicates the migration then dedups.
            conn.execute("DROP INDEX IF EXISTS idx_entries_physical")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            for name in _PRIOR_CHAIN:
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at_utc) "
                    "VALUES (?, ?)",
                    (name, "2026-07-09T11:00:00Z"),
                )
            conn.executemany(_SEED_INSERT, _SEED_ROWS)
            conn.commit()
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        db = _load_db()
        handler = None
        for m in db._CACHE_MIGRATIONS:
            if m.name == "020_session_entries_physical_unique":
                handler = m.handler
                break
        if handler is None:
            raise SystemExit("020_session_entries_physical_unique not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Production handler: keep-first-id dedup + create the UNIQUE index.
            handler(conn)
            # Reproduce the dispatcher's central stamp (#140) with a PINNED
            # timestamp so the committed golden is rebuild-deterministic.
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                ("020_session_entries_physical_unique", TS_020_APPLIED_PIN),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


# ───────────────────────── W3 backfill: the 7 missing per-migration goldens ──
# STATS 001–004, 007 + CACHE 005–006 (#279 S7). The stats-five pre-fixtures
# reuse ``create_stats_db`` (the shared current-schema source, consistent with
# how the cache goldens reuse ``_apply_cache_schema``) EXCEPT 007, whose golden
# must show a week_reset_events WITHOUT ``observed_pre_credit_pct`` — so 007
# hardcodes the pre-007 historical shape. All builders pin every timestamp
# (``TS_STATS_FIVE_APPLIED``) so a regen stays byte-idempotent under the #197
# guard; 001/002/003 discard a throwaway work cache and synthetic projects dir.


def _isolate_backfill_cache(mod, scenario_dir: Path):
    """Fully isolate a backfill handler's ``_compute_block_totals(skip_sync=
    False)`` cache read so it returns NOTHING (no child rows written) instead
    of ingesting the developer's real ``~/.claude/projects`` (~GB) into a work
    cache. Returns ``(restore_fn, work_cache)``; the caller runs the handler in
    a try/finally and calls ``restore_fn()``.

    Two levers, both required (mirrors the "isolate needs BOTH env vars"
    gotcha):
      * ``_cctally_core.{APP_DIR,CACHE_DB_PATH,CACHE_LOCK_PATH}`` → a fresh
        empty work cache under the scenario dir. The work cache is built via
        ``_apply_cache_schema`` ONLY (no ``schema_migrations``), so
        ``open_cache_db`` sees a fresh install and stamps every cache migration
        WITHOUT running a handler — no migration-errors.log writes.
      * ``CLAUDE_CONFIG_DIR`` env → a scratch dir with an EMPTY ``projects/``
        subtree, because ``sync_cache`` enumerates JSONL via
        ``_get_claude_data_dirs()`` (which reads that env var), NOT
        ``CLAUDE_PROJECTS_DIR``. Without this the walk hits real data.
    Everything (work cache, lock, scratch config dir) is discarded by
    ``restore_fn``."""
    core = mod._cctally_core
    orig_app_dir = core.APP_DIR
    orig_cache = core.CACHE_DB_PATH
    orig_lock = core.CACHE_LOCK_PATH
    orig_env = os.environ.get("CLAUDE_CONFIG_DIR")
    work_cache = scenario_dir / "_work_cache.db"

    def _clean() -> None:
        for p in sorted(scenario_dir.glob("_work_cache.db*")):
            p.unlink()
        import shutil as _sh
        _sh.rmtree(scenario_dir / "_fake_claude", ignore_errors=True)

    _clean()
    wc = sqlite3.connect(work_cache)
    try:
        wc.execute("PRAGMA journal_mode=WAL")
        mod._apply_cache_schema(wc)
        wc.commit()
    finally:
        wc.close()
    fake_claude = scenario_dir / "_fake_claude"
    (fake_claude / "projects").mkdir(parents=True, exist_ok=True)

    core.APP_DIR = scenario_dir
    core.CACHE_DB_PATH = work_cache
    core.CACHE_LOCK_PATH = scenario_dir / "_work_cache.db.lock"
    os.environ["CLAUDE_CONFIG_DIR"] = str(fake_claude)

    def _restore() -> None:
        core.APP_DIR = orig_app_dir
        core.CACHE_DB_PATH = orig_cache
        core.CACHE_LOCK_PATH = orig_lock
        if orig_env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = orig_env
        _clean()

    return _restore, work_cache


def build_per_migration_001_five_hour_block_models_backfill_v1(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for stats migration
    ``001_five_hour_block_models_backfill_v1`` (#279 S7 W3).

    pre.sqlite: full current stats schema (``create_stats_db``) with ONE
    ``five_hour_blocks`` parent (id=1) and ONE ORPHAN ``five_hour_block_models``
    row (block_id=999 → no such parent). post.sqlite: the handler's defensive
    orphan cleanup DELETEs the orphan, its per-block backfill loop writes zero
    child rows (the cache read is isolated to a fresh empty work cache, so
    ``_compute_block_totals`` returns no by_model buckets), and the 001 marker
    is stamped. The orphan DELETE is the non-vacuous, deterministic effect; the
    faithful child-row backfill is covered end-to-end by the ancient→head test
    (W2) and the migrations-test scenario 11.
    """
    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"

    def _build_pre(path: Path) -> None:
        create_stats_db(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO five_hour_blocks "
                "(id, five_hour_window_key, five_hour_resets_at, block_start_at, "
                " first_observed_at_utc, last_observed_at_utc, "
                " final_five_hour_percent, created_at_utc, last_updated_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (1, 1776600000, "2026-04-19T20:00:00Z", "2026-04-19T15:00:00Z",
                 "2026-04-19T15:05:00Z", "2026-04-19T19:55:00Z", 42.0,
                 "2026-04-19T15:05:00Z", "2026-04-19T19:55:00Z"),
            )
            conn.execute(
                "INSERT INTO five_hour_block_models "
                "(block_id, five_hour_window_key, model, input_tokens, "
                " output_tokens, cache_create_tokens, cache_read_tokens, "
                " cost_usd, entry_count) VALUES (?,?,?,?,?,?,?,?,?)",
                (999, 1776500000, "claude-opus-4-7", 10, 20, 0, 0, 0.001, 1),
            )
            conn.commit()
            # create_stats_db leaves an idle connection open (its `with` commits
            # but never closes), so the schema frames sit in the WAL and a plain
            # shutil.copy of the main file would miss them. FULL-checkpoint the
            # committed frames into the main file before the copy in _build_post.
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        mod = _load_db_module()
        restore, _work_cache = _isolate_backfill_cache(mod, scenario_dir)
        handler = _stats_handler(mod, "001_five_hour_block_models_backfill_v1")
        try:
            conn = sqlite3.connect(dst)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.row_factory = sqlite3.Row  # handler reads rows by column name
                handler(conn)
                mod._stamp_applied(
                    conn, "001_five_hour_block_models_backfill_v1",
                    TS_STATS_FIVE_APPLIED,
                )
            finally:
                conn.close()
        finally:
            restore()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_002_five_hour_block_projects_backfill_v1(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for stats migration
    ``002_five_hour_block_projects_backfill_v1`` (#279 S7 W3).

    Mirror of the 001 builder but for the by-project rollup child: pre.sqlite
    seeds a ``five_hour_blocks`` parent + an ORPHAN ``five_hour_block_projects``
    row; post.sqlite has the orphan DELETEd, no child rows written (isolated
    empty cache), and the 002 marker stamped.
    """
    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"

    def _build_pre(path: Path) -> None:
        create_stats_db(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO five_hour_blocks "
                "(id, five_hour_window_key, five_hour_resets_at, block_start_at, "
                " first_observed_at_utc, last_observed_at_utc, "
                " final_five_hour_percent, created_at_utc, last_updated_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (1, 1776600000, "2026-04-19T20:00:00Z", "2026-04-19T15:00:00Z",
                 "2026-04-19T15:05:00Z", "2026-04-19T19:55:00Z", 42.0,
                 "2026-04-19T15:05:00Z", "2026-04-19T19:55:00Z"),
            )
            conn.execute(
                "INSERT INTO five_hour_block_projects "
                "(block_id, five_hour_window_key, project_path, input_tokens, "
                " output_tokens, cache_create_tokens, cache_read_tokens, "
                " cost_usd, entry_count) VALUES (?,?,?,?,?,?,?,?,?)",
                (999, 1776500000, "/fake/proj", 10, 20, 0, 0, 0.001, 1),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(FULL)")  # see 001 builder note
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        mod = _load_db_module()
        restore, _work_cache = _isolate_backfill_cache(mod, scenario_dir)
        handler = _stats_handler(mod, "002_five_hour_block_projects_backfill_v1")
        try:
            conn = sqlite3.connect(dst)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.row_factory = sqlite3.Row
                handler(conn)
                mod._stamp_applied(
                    conn, "002_five_hour_block_projects_backfill_v1",
                    TS_STATS_FIVE_APPLIED,
                )
            finally:
                conn.close()
        finally:
            restore()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_003_merge_5h_block_duplicates_v1(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for stats migration
    ``003_merge_5h_block_duplicates_v1`` (#279 S7 W3).

    pre.sqlite: full current stats schema with TWO ``five_hour_blocks`` rows
    that represent the same physical 5h window under jitter-forked
    ``five_hour_window_key`` values (their ``five_hour_resets_at`` fall within
    the 1800 s = 3×600 s grouping band), plus a ``weekly_usage_snapshots`` row
    keyed on the DROPPED block's window key. post.sqlite: the handler merges
    the pair into the canonical (earliest ``first_observed_at_utc``) block —
    group-wide MAX aggregates, the dropped parent DELETEd, and the snapshot's
    ``five_hour_window_key`` rewritten to canonical — with the 003 marker
    stamped. The handler writes ``now_utc_iso()`` into ``last_updated_at_utc``;
    the builder pins that clock so the golden is rebuild-deterministic (#197).
    Pure stats handler — no cache open.
    """
    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"

    def _build_pre(path: Path) -> None:
        create_stats_db(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Canonical block (id=1): earliest first_observed; smaller totals.
            conn.execute(
                "INSERT INTO five_hour_blocks "
                "(id, five_hour_window_key, five_hour_resets_at, block_start_at, "
                " first_observed_at_utc, last_observed_at_utc, "
                " final_five_hour_percent, seven_day_pct_at_block_end, "
                " crossed_seven_day_reset, is_closed, "
                " total_input_tokens, total_output_tokens, "
                " total_cache_create_tokens, total_cache_read_tokens, "
                " total_cost_usd, created_at_utc, last_updated_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (1, 1776600000, "2026-04-19T20:00:00Z", "2026-04-19T15:00:00Z",
                 "2026-04-19T15:05:00Z", "2026-04-19T17:00:00Z", 30.0, 40.0,
                 0, 0, 100, 200, 0, 0, 1.0,
                 "2026-04-19T15:05:00Z", "2026-04-19T17:00:00Z"),
            )
            # Dropped duplicate (id=2): later first_observed; jittered key
            # (+600 s), resets_at within 1800 s; larger totals + later
            # last_observed so the MERGE (group MAX / latest-observation) is
            # observable in canonical.
            conn.execute(
                "INSERT INTO five_hour_blocks "
                "(id, five_hour_window_key, five_hour_resets_at, block_start_at, "
                " first_observed_at_utc, last_observed_at_utc, "
                " final_five_hour_percent, seven_day_pct_at_block_end, "
                " crossed_seven_day_reset, is_closed, "
                " total_input_tokens, total_output_tokens, "
                " total_cache_create_tokens, total_cache_read_tokens, "
                " total_cost_usd, created_at_utc, last_updated_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (2, 1776600600, "2026-04-19T20:05:00Z", "2026-04-19T15:00:00Z",
                 "2026-04-19T15:06:00Z", "2026-04-19T19:55:00Z", 55.0, 60.0,
                 1, 1, 150, 300, 0, 0, 2.5,
                 "2026-04-19T15:06:00Z", "2026-04-19T19:55:00Z"),
            )
            # Snapshot keyed on the DROPPED block's window key — must be
            # rewritten to canonical (1776600000).
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " weekly_percent, source, payload_json, five_hour_window_key) "
                "VALUES (?,?,?,?,?,?,?)",
                ("2026-04-19T19:55:00Z", "2026-04-13", "2026-04-20", 22.0,
                 "userscript", "{}", 1776600600),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(FULL)")  # see 001 builder note
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        mod = _load_db_module()
        orig_now = mod.now_utc_iso
        mod.now_utc_iso = lambda *a, **k: TS_STATS_FIVE_APPLIED  # pin the clock
        handler = _stats_handler(mod, "003_merge_5h_block_duplicates_v1")
        try:
            conn = sqlite3.connect(dst)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.row_factory = sqlite3.Row
                handler(conn)
                mod._stamp_applied(
                    conn, "003_merge_5h_block_duplicates_v1",
                    TS_STATS_FIVE_APPLIED,
                )
            finally:
                conn.close()
        finally:
            mod.now_utc_iso = orig_now

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_004_heal_forked_week_start_date_buckets(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for stats migration
    ``004_heal_forked_week_start_date_buckets`` (#279 S7 W3).

    pre.sqlite: full current stats schema with a FORKED
    ``weekly_usage_snapshots`` row whose ``week_start_date`` disagrees with
    ``substr(week_start_at, 1, 10)`` (host-TZ contamination) plus a canonical
    row for the same physical week. post.sqlite: the fork is healed
    (``week_start_date`` / ``week_end_date`` rewritten from the ISO boundary)
    and the 004 marker stamped. Pure stats handler — no cache open, no clock.
    """
    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"

    def _build_pre(path: Path) -> None:
        create_stats_db(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Forked ghost row: date bucket says 2026-04-12 but the ISO
            # boundary's UTC calendar day is 2026-04-13.
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, source, "
                " payload_json) VALUES (?,?,?,?,?,?,?,?)",
                ("2026-04-14T12:00:00Z", "2026-04-12", "2026-04-19",
                 "2026-04-13T15:00:00Z", "2026-04-20T15:00:00Z", 20.0,
                 "userscript", "{}"),
            )
            # Canonical row for the same physical week (already correct).
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, source, "
                " payload_json) VALUES (?,?,?,?,?,?,?,?)",
                ("2026-04-15T12:00:00Z", "2026-04-13", "2026-04-20",
                 "2026-04-13T15:00:00Z", "2026-04-20T15:00:00Z", 31.0,
                 "userscript", "{}"),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(FULL)")  # see 001 builder note
        finally:
            conn.close()

    def _build_post(src: Path, dst: Path) -> None:
        if dst.exists():
            dst.unlink()
        import shutil
        shutil.copy(src, dst)
        register_fixture_db(dst)
        mod = _load_db_module()
        handler = _stats_handler(mod, "004_heal_forked_week_start_date_buckets")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            handler(conn)
            mod._stamp_applied(
                conn, "004_heal_forked_week_start_date_buckets",
                TS_STATS_FIVE_APPLIED,
            )
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_007_observed_pre_credit_pct(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for stats migration ``007_observed_pre_credit_pct``
    (#279 S7 W3).

    pre.sqlite: HARDCODED pre-007 historical shape — a ``week_reset_events``
    table WITHOUT the ``observed_pre_credit_pct`` column (create_stats_db now
    carries it, so the pre must be hand-built to show the ADD COLUMN), plus one
    seeded reset event and an empty ``schema_migrations``. post.sqlite: the
    column is added (NULL on the existing row) and the 007 marker stamped. Pure
    stats handler — a simple ADD COLUMN, no cache, no clock.
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
            conn.executescript(
                """
                CREATE TABLE schema_migrations (
                    name           TEXT PRIMARY KEY,
                    applied_at_utc TEXT NOT NULL
                );
                -- Pre-007 shape: NO observed_pre_credit_pct column.
                CREATE TABLE week_reset_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    detected_at_utc        TEXT NOT NULL,
                    old_week_end_at        TEXT NOT NULL,
                    new_week_end_at        TEXT NOT NULL,
                    effective_reset_at_utc TEXT NOT NULL,
                    UNIQUE(old_week_end_at, new_week_end_at)
                );
                """
            )
            conn.execute(
                "INSERT INTO week_reset_events "
                "(detected_at_utc, old_week_end_at, new_week_end_at, "
                " effective_reset_at_utc) VALUES (?,?,?,?)",
                ("2026-04-19T18:00:00Z", "2026-04-20T15:00:00Z",
                 "2026-04-19T18:00:00Z", "2026-04-19T18:00:00Z"),
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
        mod = _load_db_module()
        handler = _stats_handler(mod, "007_observed_pre_credit_pct")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            handler(conn)
            mod._stamp_applied(
                conn, "007_observed_pre_credit_pct", TS_STATS_FIVE_APPLIED,
            )
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def _build_flag_only_cache_golden(
    scenario_dir: Path,
    *,
    migration_name: str,
    predecessor_marker: str,
    flag_key: str,
) -> None:
    """Shared builder for the flag-only conversation re-ingest cache goldens
    (cache 005 / 006, #279 S7 W3). Mirrors the 003/004 cache builders: pre =
    full cache schema (``_apply_cache_schema``) + the predecessor marker + one
    ``conversation_messages`` row; post = the row UNCHANGED plus ``flag_key``
    set in ``cache_meta`` and the migration marker stamped (the flag-only
    handler defers the clear + offset-0 re-ingest to sync_cache under the
    ``cache.db.lock`` flock)."""
    import json as _json
    scenario_dir.mkdir(parents=True, exist_ok=True)
    pre = scenario_dir / "pre.sqlite"
    post = scenario_dir / "post.sqlite"
    _BLOCKS = _json.dumps(
        [{"kind": "tool_use", "name": "Read", "input_summary": "{}",
          "id": "toolu_x", "preview": "/a/b.py"}],
        separators=(",", ":"),
    )

    def _build_pre(path: Path) -> None:
        if path.exists():
            path.unlink()
        register_fixture_db(path)
        mod = _load_db_module()
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            mod._apply_cache_schema(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO schema_migrations(name, applied_at_utc) VALUES (?, ?)",
                (predecessor_marker, TS_STATS_FIVE_APPLIED),
            )
            conn.execute(
                "INSERT INTO conversation_messages "
                "(session_id,uuid,source_path,byte_offset,timestamp_utc,"
                " entry_type,text,blocks_json,model,msg_id,req_id,is_sidechain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s1", "a1", "/fake/.claude/projects/-Users-u-proj/sess.jsonl",
                 0, "2026-04-15T15:00:00Z", "assistant", "",
                 _BLOCKS, "claude-opus-4-7", "m1", "r1", 0),
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
        mod = _load_db_module()
        handler = None
        for m in mod._CACHE_MIGRATIONS:
            if m.name == migration_name:
                handler = m.handler
                break
        if handler is None:
            raise SystemExit(f"cache migration {migration_name} not registered")
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            handler(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, ?)",
                (migration_name, TS_STATS_FIVE_APPLIED),
            )
            conn.commit()
        finally:
            conn.close()

    _build_pre(pre)
    _build_post(pre, post)


def build_per_migration_005_conversation_reingest_meta(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration ``005_conversation_reingest_meta``
    (#279 S7 W3). Flag-only handler — sets the shared
    ``conversation_reingest_pending`` flag (reused from 003/004) so sync_cache
    later reclassifies injected ``isMeta`` user lines. Loaded by
    ``tests/test_cache_migration_005_per_migration_goldens.py``."""
    _build_flag_only_cache_golden(
        scenario_dir,
        migration_name="005_conversation_reingest_meta",
        predecessor_marker="004_conversation_reingest_subagent_kind",
        flag_key="conversation_reingest_pending",
    )


def build_per_migration_006_conversation_reingest_source_tool_use_id(
    scenario_dir: Path,
) -> None:
    """Per-migration goldens for cache migration
    ``006_conversation_reingest_source_tool_use_id`` (#279 S7 W3). Flag-only
    handler — sets the DISTINCT ``conversation_source_tool_use_reingest_pending``
    flag (NOT the shared one) so sync_cache backfills ``source_tool_use_id``.
    Loaded by ``tests/test_cache_migration_006_per_migration_goldens.py``."""
    _build_flag_only_cache_golden(
        scenario_dir,
        migration_name="006_conversation_reingest_source_tool_use_id",
        predecessor_marker="005_conversation_reingest_meta",
        flag_key="conversation_source_tool_use_reingest_pending",
    )


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
    build_per_migration_010_conversation_search_split(
        FIXTURES_ROOT / "per-migration" / "010_conversation_search_split"
    )
    build_per_migration_011_conversation_promote_command_args(
        FIXTURES_ROOT / "per-migration"
        / "011_conversation_promote_command_args"
    )
    build_per_migration_012_create_conversation_ai_titles(
        FIXTURES_ROOT / "per-migration"
        / "012_create_conversation_ai_titles"
    )
    build_per_migration_013_create_conversation_sessions(
        FIXTURES_ROOT / "per-migration"
        / "013_create_conversation_sessions"
    )
    build_per_migration_014_conversation_queued_prompt_reingest(
        FIXTURES_ROOT / "per-migration"
        / "014_conversation_queued_prompt_reingest"
    )
    build_per_migration_015_conversation_sessions_filter_columns(
        FIXTURES_ROOT / "per-migration"
        / "015_conversation_sessions_filter_columns"
    )
    build_per_migration_016_drop_search_aux(
        FIXTURES_ROOT / "per-migration" / "016_drop_search_aux"
    )
    build_per_migration_017_arm_nested_agent_reingest(
        FIXTURES_ROOT / "per-migration" / "017_arm_nested_agent_reingest"
    )
    build_per_migration_018_create_conversation_title_fts(
        FIXTURES_ROOT / "per-migration" / "018_create_conversation_title_fts"
    )
    build_per_migration_019_create_conversation_file_touches(
        FIXTURES_ROOT / "per-migration" / "019_create_conversation_file_touches"
    )
    build_per_migration_020_session_entries_physical_unique(
        FIXTURES_ROOT / "per-migration" / "020_session_entries_physical_unique"
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
    # W3 backfill (#279 S7): the 7 previously-missing per-migration goldens —
    # stats 001-004/007 + cache 005/006 (stats 005/006 remain the only
    # hand-built, builder-less goldens).
    build_per_migration_001_five_hour_block_models_backfill_v1(
        FIXTURES_ROOT / "per-migration"
        / "001_five_hour_block_models_backfill_v1"
    )
    build_per_migration_002_five_hour_block_projects_backfill_v1(
        FIXTURES_ROOT / "per-migration"
        / "002_five_hour_block_projects_backfill_v1"
    )
    build_per_migration_003_merge_5h_block_duplicates_v1(
        FIXTURES_ROOT / "per-migration" / "003_merge_5h_block_duplicates_v1"
    )
    build_per_migration_004_heal_forked_week_start_date_buckets(
        FIXTURES_ROOT / "per-migration"
        / "004_heal_forked_week_start_date_buckets"
    )
    build_per_migration_007_observed_pre_credit_pct(
        FIXTURES_ROOT / "per-migration" / "007_observed_pre_credit_pct"
    )
    build_per_migration_005_conversation_reingest_meta(
        FIXTURES_ROOT / "per-migration" / "005_conversation_reingest_meta"
    )
    build_per_migration_006_conversation_reingest_source_tool_use_id(
        FIXTURES_ROOT / "per-migration"
        / "006_conversation_reingest_source_tool_use_id"
    )
    print(f"Wrote fixtures to {FIXTURES_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
