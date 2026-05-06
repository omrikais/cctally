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
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "migrations"

# Stable timestamps — never use "now"; use these.
TS_001_APPLIED = "2026-04-30T12:00:00Z"
TS_002_APPLIED = "2026-04-30T12:00:00Z"
TS_003_APPLIED = "2026-05-04T08:12:11Z"


def _new_stats_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    return conn


def _new_cache_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
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
    print(f"Wrote fixtures to {FIXTURES_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
