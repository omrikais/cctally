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
        ``output_tokens=1``, no ``speed`` field). schema_migrations is empty.
      * post.sqlite — same DB after running the production migration handler.
        Both seeded tables are empty; ``schema_migrations`` has one row
        ``(001_dedup_highest_wins, <iso>)``.

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
                """
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
    print(f"Wrote fixtures to {FIXTURES_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
