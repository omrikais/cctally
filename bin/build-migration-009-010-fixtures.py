#!/usr/bin/env python3
"""Rebuild per-migration goldens for stats migrations 009 (recompute
``five_hour_blocks``) and 010 (recompute ``percent_milestones``).

One-shot builder; writes byte-stable WAL-mode SQLite under
``tests/fixtures/migrations/per-migration/<NNN_name>/{pre,pre-cache,post}.sqlite``.

Stdlib-only; safe to re-run idempotently (overwrites existing fixtures).
Run from the repo root.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

# Shared fixture-DB registry so the atexit hook normalizes the SQLite
# writer-version bytes (96-99) on exit — otherwise every rebuild churns
# those bytes and dirties the in-tree fixtures (CLAUDE.md gotcha "SQLite
# writer-version dirties fixtures").
from _fixture_builders import register_fixture_db  # noqa: E402

FIX_BASE = REPO_ROOT / "tests" / "fixtures" / "migrations" / "per-migration"

WALK_COMPLETE_MARKER = "claude_ingest_walk_complete"


# ── Shared cache.db DDL ────────────────────────────────────────────────────
_CACHE_DDL = """
CREATE TABLE schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at_utc TEXT
);
CREATE TABLE schema_migrations_skipped (
    name TEXT PRIMARY KEY,
    skipped_at_utc TEXT,
    reason TEXT
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
    source_path TEXT,
    line_offset INTEGER,
    timestamp_utc TEXT,
    model TEXT,
    msg_id TEXT,
    req_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_create_tokens INTEGER,
    cache_read_tokens INTEGER,
    usage_extra_json TEXT,
    cost_usd_raw REAL,
    speed TEXT
);
CREATE TABLE cache_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Pricing reference: claude-opus-4-7 output rate is $25/Mtok in the
# embedded CLAUDE_MODEL_PRICING. 1000 output tokens → $0.025.
MODEL = "claude-opus-4-7"
RATE_PER_1K_OUTPUT_TOKENS = 0.025


def _wal_connect(path: pathlib.Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    for sidecar in (
        path.with_suffix(path.suffix + "-wal"),
        path.with_suffix(path.suffix + "-shm"),
    ):
        if sidecar.exists():
            sidecar.unlink()
    register_fixture_db(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _build_cache(cache_path: pathlib.Path, *, entries: list[dict]) -> None:
    """Build a cache.db with the 001 marker stamped, the cache_meta
    walk-complete marker (the new gate's walk✓ PROCEED signal,
    cctally-dev#93), session_files rows (production-shape parity), and the
    given session_entries (entries✓). Together these give the row-6
    PROCEED topology the gate requires before 009/010 recompute.
    """
    conn = _wal_connect(cache_path)
    try:
        conn.executescript(_CACHE_DDL)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            ("001_dedup_highest_wins", "2026-05-22T00:00:00Z"),
        )
        # cache_meta walk-complete marker — replaces the old "post-001
        # session_files row" proof as the gate's PROCEED signal.
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
            (WALK_COMPLETE_MARKER, "2026-05-22T01:00:00Z"),
        )
        # session_files rows retained for production-shape parity (no
        # longer the gate signal); 009 still LEFT JOINs them for
        # project_path attribution.
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, "
            " last_ingested_at, session_id, project_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "/tmp/session1.jsonl", 100, 0, 100,
                "2026-05-22T01:00:00Z", "s1", "/tmp/projA",
            ),
        )
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, "
            " last_ingested_at, session_id, project_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "/tmp/session2.jsonl", 200, 0, 200,
                "2026-05-22T01:00:00Z", "s2", "/tmp/projB",
            ),
        )
        for e in entries:
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, usage_extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    e["source"], e.get("line", 0), e["ts"], e["model"],
                    e.get("in", 0), e["out"],
                    e.get("cc", 0), e.get("cr", 0),
                    "{}",
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ── 009: five_hour_blocks recompute ────────────────────────────────────────


_STATS_DDL_009 = """
CREATE TABLE schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at_utc TEXT
);
CREATE TABLE schema_migrations_skipped (
    name TEXT PRIMARY KEY,
    skipped_at_utc TEXT,
    reason TEXT
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
    UNIQUE(five_hour_window_key, model)
);
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
    UNIQUE(five_hour_window_key, project_path)
);
"""


def _build_009_stats_pre(stats_path: pathlib.Path) -> None:
    """Two 5h blocks: one closed (historical), one active (current).
    Both have INFLATED pre-dedup totals (doubled tokens/cost) that the
    migration must recompute downward to match the corrected
    session_entries.

    Block A (closed, historical):
      window_key = 100, block_start_at = 2026-05-18T00:00:00+00:00,
      last_observed = 2026-05-18T04:50:00+00:00.
      Pre-fix totals: 2000 output tokens, $0.050 cost. Real entries (in
      cache.db) sum to 1000 output tokens, $0.025.

    Block B (active, current):
      window_key = 200, block_start_at = 2026-05-22T10:00:00+00:00,
      last_observed = 2026-05-22T11:30:00+00:00.
      Pre-fix totals: 4000 output tokens, $0.100. Real entries sum to
      2000 output tokens, $0.050.

    Rollup children (five_hour_block_models / _projects) carry the same
    inflated numbers per-model / per-project; the migration must
    replace-all them.
    """
    conn = _wal_connect(stats_path)
    try:
        conn.executescript(_STATS_DDL_009)

        # Block A (closed historical)
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(id, five_hour_window_key, five_hour_resets_at, "
            " block_start_at, first_observed_at_utc, "
            " last_observed_at_utc, final_five_hour_percent, "
            " total_input_tokens, total_output_tokens, "
            " total_cache_create_tokens, total_cache_read_tokens, "
            " total_cost_usd, is_closed, created_at_utc, "
            " last_updated_at_utc) "
            "VALUES "
            "(1, 100, '2026-05-18T05:00:00+00:00', "
            " '2026-05-18T00:00:00+00:00', "
            " '2026-05-18T00:10:00+00:00', "
            " '2026-05-18T04:50:00+00:00', 75.0, "
            " 0, 2000, 0, 0, 0.050, 1, "
            " '2026-05-18T00:10:00Z', '2026-05-18T04:50:00Z')"
        )
        # Children: per-model + per-project for block A
        conn.execute(
            "INSERT INTO five_hour_block_models "
            "(block_id, five_hour_window_key, model, "
            " input_tokens, output_tokens, "
            " cache_create_tokens, cache_read_tokens, "
            " cost_usd, entry_count) "
            "VALUES (1, 100, ?, 0, 2000, 0, 0, 0.050, 2)",
            (MODEL,),
        )
        conn.execute(
            "INSERT INTO five_hour_block_projects "
            "(block_id, five_hour_window_key, project_path, "
            " input_tokens, output_tokens, "
            " cache_create_tokens, cache_read_tokens, "
            " cost_usd, entry_count) "
            "VALUES (1, 100, '/tmp/projA', 0, 2000, 0, 0, 0.050, 2)"
        )

        # Block B (active current)
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(id, five_hour_window_key, five_hour_resets_at, "
            " block_start_at, first_observed_at_utc, "
            " last_observed_at_utc, final_five_hour_percent, "
            " total_input_tokens, total_output_tokens, "
            " total_cache_create_tokens, total_cache_read_tokens, "
            " total_cost_usd, is_closed, created_at_utc, "
            " last_updated_at_utc) "
            "VALUES "
            "(2, 200, '2026-05-22T15:00:00+00:00', "
            " '2026-05-22T10:00:00+00:00', "
            " '2026-05-22T10:05:00+00:00', "
            " '2026-05-22T11:30:00+00:00', 30.0, "
            " 0, 4000, 0, 0, 0.100, 0, "
            " '2026-05-22T10:05:00Z', '2026-05-22T11:30:00Z')"
        )
        conn.execute(
            "INSERT INTO five_hour_block_models "
            "(block_id, five_hour_window_key, model, "
            " input_tokens, output_tokens, "
            " cache_create_tokens, cache_read_tokens, "
            " cost_usd, entry_count) "
            "VALUES (2, 200, ?, 0, 4000, 0, 0, 0.100, 4)",
            (MODEL,),
        )
        # Block B touches TWO projects (A and B) but pre-fix inflated
        # numbers collapse them into one "old project A only" row to
        # exercise that the migration creates a fresh per-project row
        # for projB.
        conn.execute(
            "INSERT INTO five_hour_block_projects "
            "(block_id, five_hour_window_key, project_path, "
            " input_tokens, output_tokens, "
            " cache_create_tokens, cache_read_tokens, "
            " cost_usd, entry_count) "
            "VALUES (2, 200, '/tmp/projA', 0, 4000, 0, 0, 0.100, 4)"
        )

        conn.commit()
    finally:
        conn.close()


def _build_009_stats_post(stats_path: pathlib.Path) -> None:
    """Expected stats.db AFTER migration 009 runs. Totals match the
    real session_entries (single-emission post-dedup numbers).

    Block A: 1000 output tokens, $0.025; one model row, one project row.
    Block B: 2000 output tokens, $0.050; one model row, two project rows
      (projA $0.025 + projB $0.025).
    """
    conn = _wal_connect(stats_path)
    try:
        conn.executescript(_STATS_DDL_009)

        # Block A
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(id, five_hour_window_key, five_hour_resets_at, "
            " block_start_at, first_observed_at_utc, "
            " last_observed_at_utc, final_five_hour_percent, "
            " total_input_tokens, total_output_tokens, "
            " total_cache_create_tokens, total_cache_read_tokens, "
            " total_cost_usd, is_closed, created_at_utc, "
            " last_updated_at_utc) "
            "VALUES "
            "(1, 100, '2026-05-18T05:00:00+00:00', "
            " '2026-05-18T00:00:00+00:00', "
            " '2026-05-18T00:10:00+00:00', "
            " '2026-05-18T04:50:00+00:00', 75.0, "
            " 0, 1000, 0, 0, 0.025, 1, "
            " '2026-05-18T00:10:00Z', '2026-05-18T04:50:00Z')"
        )
        conn.execute(
            "INSERT INTO five_hour_block_models "
            "(block_id, five_hour_window_key, model, "
            " input_tokens, output_tokens, "
            " cache_create_tokens, cache_read_tokens, "
            " cost_usd, entry_count) "
            "VALUES (1, 100, ?, 0, 1000, 0, 0, 0.025, 1)",
            (MODEL,),
        )
        conn.execute(
            "INSERT INTO five_hour_block_projects "
            "(block_id, five_hour_window_key, project_path, "
            " input_tokens, output_tokens, "
            " cache_create_tokens, cache_read_tokens, "
            " cost_usd, entry_count) "
            "VALUES (1, 100, '/tmp/projA', 0, 1000, 0, 0, 0.025, 1)"
        )

        # Block B
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(id, five_hour_window_key, five_hour_resets_at, "
            " block_start_at, first_observed_at_utc, "
            " last_observed_at_utc, final_five_hour_percent, "
            " total_input_tokens, total_output_tokens, "
            " total_cache_create_tokens, total_cache_read_tokens, "
            " total_cost_usd, is_closed, created_at_utc, "
            " last_updated_at_utc) "
            "VALUES "
            "(2, 200, '2026-05-22T15:00:00+00:00', "
            " '2026-05-22T10:00:00+00:00', "
            " '2026-05-22T10:05:00+00:00', "
            " '2026-05-22T11:30:00+00:00', 30.0, "
            " 0, 2000, 0, 0, 0.050, 0, "
            " '2026-05-22T10:05:00Z', '2026-05-22T11:30:00Z')"
        )
        conn.execute(
            "INSERT INTO five_hour_block_models "
            "(block_id, five_hour_window_key, model, "
            " input_tokens, output_tokens, "
            " cache_create_tokens, cache_read_tokens, "
            " cost_usd, entry_count) "
            "VALUES (2, 200, ?, 0, 2000, 0, 0, 0.050, 2)",
            (MODEL,),
        )
        conn.execute(
            "INSERT INTO five_hour_block_projects "
            "(block_id, five_hour_window_key, project_path, "
            " input_tokens, output_tokens, "
            " cache_create_tokens, cache_read_tokens, "
            " cost_usd, entry_count) "
            "VALUES (2, 200, '/tmp/projA', 0, 1000, 0, 0, 0.025, 1)"
        )
        conn.execute(
            "INSERT INTO five_hour_block_projects "
            "(block_id, five_hour_window_key, project_path, "
            " input_tokens, output_tokens, "
            " cache_create_tokens, cache_read_tokens, "
            " cost_usd, entry_count) "
            "VALUES (2, 200, '/tmp/projB', 0, 1000, 0, 0, 0.025, 1)"
        )

        # Migration marker.
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            (
                "009_recompute_five_hour_blocks_dedup_fix",
                "2026-05-22T12:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def build_009() -> None:
    """Build the 3 fixture files for migration 009."""
    base = FIX_BASE / "009_recompute_five_hour_blocks_dedup_fix"
    base.mkdir(parents=True, exist_ok=True)

    # Cache entries:
    #   Block A range [2026-05-18T00:00:00+00:00, 2026-05-18T04:50:00+00:00]
    #     One entry: 1000 output tokens, projA. $0.025.
    #   Block B range [2026-05-22T10:00:00+00:00, 2026-05-22T11:30:00+00:00]
    #     Two entries: one projA ($0.025) and one projB ($0.025). Total
    #     2000 output tokens, $0.050.
    entries = [
        # Block A
        {
            "source": "/tmp/session1.jsonl", "line": 0,
            "ts": "2026-05-18T02:00:00Z",
            "model": MODEL, "in": 0, "out": 1000, "cc": 0, "cr": 0,
        },
        # Block B (in projA)
        {
            "source": "/tmp/session1.jsonl", "line": 1,
            "ts": "2026-05-22T10:30:00Z",
            "model": MODEL, "in": 0, "out": 1000, "cc": 0, "cr": 0,
        },
        # Block B (in projB)
        {
            "source": "/tmp/session2.jsonl", "line": 0,
            "ts": "2026-05-22T11:00:00Z",
            "model": MODEL, "in": 0, "out": 1000, "cc": 0, "cr": 0,
        },
    ]
    _build_cache(base / "pre-cache.sqlite", entries=entries)
    _build_009_stats_pre(base / "pre.sqlite")
    _build_009_stats_post(base / "post.sqlite")
    print(f"[build] 009 fixtures written to {base}")


# ── 010: percent_milestones recompute ──────────────────────────────────────


_STATS_DDL_010 = """
CREATE TABLE schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at_utc TEXT
);
CREATE TABLE schema_migrations_skipped (
    name TEXT PRIMARY KEY,
    skipped_at_utc TEXT,
    reason TEXT
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


def _build_010_stats_pre(stats_path: pathlib.Path) -> None:
    """Three percent_milestones rows for ONE subscription week
    (2026-05-15 → 2026-05-22) at thresholds 1, 2, 3 with INFLATED
    pre-dedup cumulative costs.

    Real cache.db entries:
      * One entry at 2026-05-16T12:00:00Z → $0.025 cumulative at that
        captured moment.
      * Two more entries (total $0.025 each), captured later. Cumulative
        steps: $0.025 → $0.050 → $0.075.

    Pre-fix milestones (recorded under inflated dedup):
      * threshold=1 at captured 2026-05-16T12:00:00Z: cumulative $0.050
        (2x the real $0.025).
      * threshold=2 at captured 2026-05-17T12:00:00Z: cumulative $0.100
        (2x real $0.050).
      * threshold=3 at captured 2026-05-18T12:00:00Z: cumulative $0.150
        (2x real $0.075).
    """
    conn = _wal_connect(stats_path)
    try:
        conn.executescript(_STATS_DDL_010)
        for tid, captured, cum, marginal in (
            (1, "2026-05-16T12:00:00Z", 0.050, 0.050),
            (2, "2026-05-17T12:00:00Z", 0.100, 0.050),
            (3, "2026-05-18T12:00:00Z", 0.150, 0.050),
        ):
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    captured, "2026-05-15", "2026-05-22",
                    "2026-05-15T00:00:00+00:00",
                    "2026-05-22T00:00:00+00:00",
                    tid, cum, marginal, 1, 1, 0,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _build_010_stats_post(stats_path: pathlib.Path) -> None:
    """Expected stats.db AFTER migration 010 runs. cumulative_cost_usd
    matches the real session_entries; marginal_cost_usd matches
    cumulative - prior.cumulative (or cumulative for the first row).
    """
    conn = _wal_connect(stats_path)
    try:
        conn.executescript(_STATS_DDL_010)
        # threshold=1: cumulative = $0.025 (one entry by then), marginal
        # = cumulative (first row of the week).
        # threshold=2: cumulative = $0.050 (two entries by then),
        # marginal = $0.025.
        # threshold=3: cumulative = $0.075 (all three entries),
        # marginal = $0.025.
        for tid, captured, cum, marginal in (
            (1, "2026-05-16T12:00:00Z", 0.025, 0.025),
            (2, "2026-05-17T12:00:00Z", 0.050, 0.025),
            (3, "2026-05-18T12:00:00Z", 0.075, 0.025),
        ):
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    captured, "2026-05-15", "2026-05-22",
                    "2026-05-15T00:00:00+00:00",
                    "2026-05-22T00:00:00+00:00",
                    tid, cum, marginal, 1, 1, 0,
                ),
            )
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            (
                "010_recompute_percent_milestones_dedup_fix",
                "2026-05-22T12:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def build_010() -> None:
    base = FIX_BASE / "010_recompute_percent_milestones_dedup_fix"
    base.mkdir(parents=True, exist_ok=True)
    # Cache entries: three rows, each 1000 output tokens of opus-4-7
    # ($0.025 each), at increasing timestamps so cumulative grows
    # 0.025 → 0.050 → 0.075 as thresholds 1, 2, 3 are captured.
    entries = [
        {
            "source": "/tmp/session1.jsonl", "line": 0,
            "ts": "2026-05-16T10:00:00Z",  # before threshold=1 captured
            "model": MODEL, "in": 0, "out": 1000, "cc": 0, "cr": 0,
        },
        {
            "source": "/tmp/session1.jsonl", "line": 1,
            "ts": "2026-05-17T10:00:00Z",  # before threshold=2 captured
            "model": MODEL, "in": 0, "out": 1000, "cc": 0, "cr": 0,
        },
        {
            "source": "/tmp/session1.jsonl", "line": 2,
            "ts": "2026-05-18T10:00:00Z",  # before threshold=3 captured
            "model": MODEL, "in": 0, "out": 1000, "cc": 0, "cr": 0,
        },
    ]
    _build_cache(base / "pre-cache.sqlite", entries=entries)
    _build_010_stats_pre(base / "pre.sqlite")
    _build_010_stats_post(base / "post.sqlite")
    print(f"[build] 010 fixtures written to {base}")


def main() -> int:
    build_009()
    build_010()
    return 0


if __name__ == "__main__":
    sys.exit(main())
