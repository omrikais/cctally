#!/usr/bin/env python3
"""Builder: the ancient-DB→head long-haul fixtures (#279 S7 W2).

Emits, under ``tests/fixtures/migrations/ancient/``:
  * ``stats.sqlite``   — a PRE-FRAMEWORK stats.db (``user_version=0``, NO
    ``schema_migrations`` table) populated with the HISTORICAL shapes each
    stats migration transforms: pre-005 ``percent_milestones`` (no
    ``reset_event_id``), pre-006 ``five_hour_milestones`` (no
    ``reset_event_id``), pre-007 ``week_reset_events`` (no
    ``observed_pre_credit_pct``), the pre-011 two-table budget shape
    (``budget_milestones`` keyed ``week_start_at`` + a separate
    ``codex_budget_milestones``), duplicate ``five_hour_blocks`` (for the 003
    merge), and ``weekly_{usage,cost}_snapshots`` (for the 008 recompute).
  * ``cache.sqlite``   — a PRE-001 legacy cache.db (``user_version=0``, NO
    ``schema_migrations``/``cache_meta``, NO conversation tables, NO ``speed``
    column) with DUPLICATE ``session_entries`` for cache 001 to dedup-wipe.
  * ``cache-midera.sqlite`` — a mid-era cache.db carrying the CURRENT schema
    but the LEGACY unsplit FTS shape (``conversation_fts_aux`` +
    ``search_aux``), so ``open → sync → reopen`` drives 010's real index split
    → 016's ``search_aux`` drop → 018's title-FTS arming.
  * ``corpus/`` — a small deterministic synthetic JSONL corpus (duplicate-
    bearing streaming pairs) that a real ``sync_cache`` repopulates the wiped
    cache from, so the stats 008/009/010 recompute gate (walk-complete + non-
    empty entries) is satisfied.
  * ``README.md``      — the freeze discipline.

FREEZE DISCIPLINE — the schemas below are HISTORICAL CONSTANTS, hardcoded on
purpose. They must NEVER be "refreshed" from ``_apply_cache_schema`` /
``create_stats_db`` / ``_cctally_core`` current DDL — the whole point of the
ancient fixture is to represent a shape the current code no longer emits, so the
full migration chain has something real to migrate. Regenerate ONLY on a
deliberate decision (e.g. a new migration needs a new legacy column), and review
the diff by hand. This script is named so the #197 byte-idempotency guard does
NOT auto-discover it (it is not ``build_per_migration_*`` inside
``build-migrations-fixtures.py``); it is driven end-to-end by
``tests/test_migration_ancient_to_head.py`` instead.

Deterministic, WAL-mode, TZ=Etc/UTC, pinned timestamps.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fixture_builders import register_fixture_db, emit_streaming_pair  # noqa: E402

ANCIENT_ROOT = (
    Path(__file__).resolve().parent.parent
    / "tests" / "fixtures" / "migrations" / "ancient"
)

# Pinned instants — never wall-clock.
TS = "2026-04-19T15:00:00Z"
BLOCK_START = "2026-04-19T15:00:00Z"
BLOCK_LAST = "2026-04-19T19:55:00Z"
RESETS_AT = "2026-04-19T20:00:00Z"
RESETS_AT_JITTER = "2026-04-19T20:05:00Z"
WINDOW_KEY = 1776600000
WINDOW_KEY_JITTER = 1776600600
# The corpus session entry timestamp falls inside [BLOCK_START, BLOCK_LAST].
ENTRY_TS = "2026-04-19T17:00:00Z"
# Cost window for the 008 weekly_cost_snapshots recompute — brackets ENTRY_TS.
RANGE_START = "2026-04-13T00:00:00+00:00"
RANGE_END = "2026-04-20T00:00:00+00:00"


def _new_wal(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    register_fixture_db(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def build_ancient_stats(path: Path) -> None:
    conn = _new_wal(path)
    try:
        conn.executescript(
            """
            -- weekly_usage_snapshots: base pre-5h-column shape; open_db's
            -- add_column_if_missing backfills week_start_at / five_hour_* on
            -- upgrade (the historical inline-migration path).
            CREATE TABLE weekly_usage_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                week_start_date TEXT NOT NULL,
                week_end_date TEXT NOT NULL,
                weekly_percent REAL NOT NULL,
                page_url TEXT,
                source TEXT NOT NULL DEFAULT 'userscript',
                payload_json TEXT NOT NULL,
                five_hour_percent REAL,
                five_hour_resets_at TEXT,
                five_hour_window_key INTEGER
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
            -- pre-005 percent_milestones: NO reset_event_id, narrow UNIQUE.
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
                UNIQUE(week_start_date, percent_threshold)
            );
            -- pre-007 week_reset_events: NO observed_pre_credit_pct.
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
            -- pre-006 five_hour_milestones: NO reset_event_id, no alerted_at,
            -- narrow UNIQUE.
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
                UNIQUE(five_hour_window_key, percent_threshold)
            );
            -- pre-011 two-table budget shape.
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
        # usage snapshot with 5h binding.
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, weekly_percent, "
            " source, payload_json, five_hour_percent, five_hour_resets_at, "
            " five_hour_window_key) VALUES (?,?,?,?,?,?,?,?,?)",
            (TS, "2026-04-13", "2026-04-20", 42.0, "userscript", "{}",
             30.0, RESETS_AT, WINDOW_KEY),
        )
        # auto cost snapshot (008 recomputes this from cache session_entries).
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, range_start_iso, "
            " range_end_iso, cost_usd, mode) VALUES (?,?,?,?,?,?,?)",
            (TS, "2026-04-13", "2026-04-20", RANGE_START, RANGE_END, 999.0, "auto"),
        )
        # Two duplicate five_hour_blocks (jitter-forked keys) for the 003 merge.
        conn.executemany(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, final_five_hour_percent, "
            " total_input_tokens, total_output_tokens, total_cost_usd, "
            " created_at_utc, last_updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (WINDOW_KEY, RESETS_AT, BLOCK_START, BLOCK_START, "2026-04-19T17:00:00Z",
                 30.0, 100, 200, 1.0, BLOCK_START, "2026-04-19T17:00:00Z"),
                (WINDOW_KEY_JITTER, RESETS_AT_JITTER, BLOCK_START,
                 "2026-04-19T15:06:00Z", BLOCK_LAST, 55.0, 150, 300, 2.5,
                 "2026-04-19T15:06:00Z", BLOCK_LAST),
            ],
        )
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, percent_threshold, "
            " cumulative_cost_usd, usage_snapshot_id, cost_snapshot_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (TS, "2026-04-13", "2026-04-20", 30, 5.0, 1, 1),
        )
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, captured_at_utc, "
            " usage_snapshot_id, block_output_tokens, block_cost_usd) "
            "VALUES (?,?,?,?,?,?,?)",
            (1, WINDOW_KEY, 30, TS, 1, 200, 1.0),
        )
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, effective_reset_at_utc) "
            "VALUES (?,?,?,?)",
            ("2026-04-19T18:00:00Z", "2026-04-20T15:00:00Z", "2026-04-19T18:00:00Z",
             "2026-04-19T18:00:00Z"),
        )
        conn.execute(
            "INSERT INTO budget_milestones (week_start_at, threshold, budget_usd, "
            "spent_usd, consumption_pct, crossed_at_utc, alerted_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("2026-04-13T00:00:00+00:00", 90, 300.0, 271.0, 90.3, TS, TS),
        )
        conn.execute(
            "INSERT INTO codex_budget_milestones (period_start_at, threshold, "
            "budget_usd, spent_usd, consumption_pct, crossed_at_utc, alerted_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("2026-04-01T00:00:00+00:00", 100, 200.0, 210.0, 105.0, TS, TS),
        )
        conn.execute(
            "INSERT INTO projected_milestones (week_start_at, metric, threshold, "
            "projected_value, denominator, crossed_at_utc, alerted_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("2026-04-13T00:00:00+00:00", "weekly_pct", 90, 95.0, 100.0, TS, TS),
        )
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        conn.close()


# Legacy pre-001 cache schema — NO speed column, NO conversation tables, NO
# schema_migrations, NO cache_meta. This is the historical shape reproduced from
# the frozen mirror in build-migrations-fixtures.py's cache-001 pre.sqlite.
_LEGACY_CACHE_SCHEMA = """
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


def build_ancient_cache(path: Path) -> None:
    conn = _new_wal(path)
    try:
        conn.executescript(_LEGACY_CACHE_SCHEMA)
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            ("/fake/.claude/projects/-fake-proj/sess-a.jsonl", 1024,
             1_700_000_000_000_000_000, 1024, TS, "sess-a", "/fake/proj"),
        )
        # DUPLICATE summed-token "loser" rows the cache 001 dedup must wipe.
        conn.executemany(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, msg_id, req_id, "
            " input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, "
            " usage_extra_json, cost_usd_raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("/fake/.claude/projects/-fake-proj/sess-a.jsonl", 100, ENTRY_TS,
                 "claude-opus-4-7", "m1", "r1", 100, 1, 0, 50, None, None),
                ("/fake/.claude/projects/-fake-proj/sess-a.jsonl", 200, ENTRY_TS,
                 "claude-opus-4-7", "m2", "r2", 100, 2, 0, 50, None, None),
            ],
        )
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        conn.close()


def build_corpus(corpus_dir: Path) -> None:
    """Deterministic synthetic JSONL corpus (duplicate-bearing streaming pairs)
    that a real sync_cache repopulates the wiped cache from."""
    proj = corpus_dir / "projects" / "-fake-proj"
    proj.mkdir(parents=True, exist_ok=True)
    jsonl = proj / "sess-a.jsonl"
    if jsonl.exists():
        jsonl.unlink()
    # A dedup-bearing streaming pair (intermediate output=1 loses to final).
    emit_streaming_pair(
        jsonl,
        model="claude-opus-4-7",
        msg_id="c-msg-1",
        req_id="c-req-1",
        ts_intermediate="2026-04-19T17:00:00.100Z",
        ts_final="2026-04-19T17:00:00.500Z",
        intermediate_output_tokens=1,
        final_output_tokens=1000,
        cache_read_tokens=42,
        session_id="sess-a",
        cwd="/fake/proj",
        append=False,
    )


def build_midera_cache(path: Path) -> None:
    """A mid-era cache.db: CURRENT schema (via the real _apply_cache_schema) at
    the 009 head, then torn down to the LEGACY unsplit FTS shape (single-column
    conversation_fts referencing search_aux + the conversation_fts_aux
    external-content shadow) with a populated search_aux — so open → sync →
    reopen drives 010's real index split, 016's search_aux drop, and 018's
    title-FTS arming. Reuses the cache-010 per-migration builder's legacy-FTS
    teardown so the shape stays in lockstep with production's search-split code."""
    # Delegate to the existing cache-010 pre-builder (it already reproduces the
    # legacy unsplit FTS shape exactly). Import it lazily.
    import importlib.util as ilu
    bin_dir = Path(__file__).resolve().parent
    spec = ilu.spec_from_file_location(
        "build_migrations_fixtures", bin_dir / "build-migrations-fixtures.py"
    )
    mod = ilu.module_from_spec(spec)
    sys.modules["build_migrations_fixtures"] = mod
    spec.loader.exec_module(mod)
    # The 010 builder writes pre.sqlite/post.sqlite into a scenario dir; build
    # into a temp scenario and lift its pre.sqlite (the legacy-FTS shape) as our
    # mid-era fixture.
    import tempfile
    import shutil
    with tempfile.TemporaryDirectory() as td:
        scen = Path(td) / "010"
        mod.build_per_migration_010_conversation_search_split(scen)
        if path.exists():
            path.unlink()
        shutil.copy(scen / "pre.sqlite", path)
        register_fixture_db(path)


README = """# Ancient-DB → head long-haul fixtures (#279 S7 W2)

Frozen historical DB fixtures driven end-to-end by
`tests/test_migration_ancient_to_head.py`.

- `stats.sqlite` — a pre-framework stats.db (`user_version=0`, no
  `schema_migrations`) in the historical shapes the 12 stats migrations
  transform.
- `cache.sqlite` — a pre-001 legacy cache.db (duplicate `session_entries`, no
  conversation tables, no `speed` column) for the cache 001 dedup-wipe.
- `cache-midera.sqlite` — the mid-era legacy unsplit-FTS cache shape for the
  010 → 016 → 018 FTS interaction.
- `corpus/projects/-fake-proj/sess-a.jsonl` — the synthetic JSONL a real
  `sync_cache` repopulates the wiped cache from (so the 008/009/010 recompute
  gate is satisfied).

## FREEZE DISCIPLINE — regen is deliberate-only

The schemas inside `bin/build-ancient-migration-fixtures.py` are HISTORICAL
CONSTANTS. Do NOT "refresh" them from `_apply_cache_schema` / `create_stats_db`
/ current `_cctally_core` DDL — the whole point is to represent shapes the
current code no longer emits, so the migration chain has something real to
migrate. Regenerate ONLY when a deliberate decision requires it (e.g. a new
migration needs a new legacy column), by running
`python3 bin/build-ancient-migration-fixtures.py` and hand-reviewing the diff.
The builder is intentionally NOT a `build_per_migration_*` in
`build-migrations-fixtures.py`, so the #197 byte-idempotency guard does not
auto-discover it.
"""


def main() -> int:
    os.environ["TZ"] = "Etc/UTC"
    ANCIENT_ROOT.mkdir(parents=True, exist_ok=True)
    build_ancient_stats(ANCIENT_ROOT / "stats.sqlite")
    build_ancient_cache(ANCIENT_ROOT / "cache.sqlite")
    build_midera_cache(ANCIENT_ROOT / "cache-midera.sqlite")
    build_corpus(ANCIENT_ROOT / "corpus")
    (ANCIENT_ROOT / "README.md").write_text(README)
    print(f"Wrote ancient fixtures to {ANCIENT_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
