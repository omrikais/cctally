#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally forecast`.

Writes one `db.sqlite` per scenario under
`tests/fixtures/forecast/<scenario>/`. Uses the same schema as
the production DB (mirrors bin/cctally open_db()).

Run: `bin/build-forecast-fixtures.py` (idempotent — overwrites existing DBs).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import create_cache_db, create_stats_db  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/forecast"

# Deterministic "now" shared with input.env `AS_OF` per scenario.
# Wall-clock is irrelevant; all scenarios pin their own `AS_OF`.


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_snapshots(stats_conn, week_start: dt.datetime, week_end: dt.datetime,
                      samples: list[tuple[float, float]]) -> None:
    """samples: list of (hours_since_week_start, weekly_percent).
    Writes to the stats.db connection."""
    week_start_date = week_start.date().isoformat()
    week_end_date = week_end.date().isoformat()
    week_start_at = _iso(week_start)
    week_end_at = _iso(week_end)
    for hours_in, pct in samples:
        captured = _iso(week_start + dt.timedelta(hours=hours_in))
        stats_conn.execute(
            "INSERT INTO weekly_usage_snapshots(captured_at_utc, week_start_date, "
            "week_end_date, week_start_at, week_end_at, weekly_percent, source, "
            "payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (captured, week_start_date, week_end_date, week_start_at, week_end_at,
             pct, "fixture", json.dumps({"fixture": True})),
        )


def _insert_snapshots_date_only(stats_conn, week_start: dt.datetime, week_end: dt.datetime,
                                samples: list[tuple[float, float]]) -> None:
    """Like _insert_snapshots but writes NULL week_start_at/week_end_at — simulates
    upgraded installs that only have legacy date-based rows for the active week."""
    week_start_date = week_start.date().isoformat()
    week_end_date = week_end.date().isoformat()
    for hours_in, pct in samples:
        captured = _iso(week_start + dt.timedelta(hours=hours_in))
        stats_conn.execute(
            "INSERT INTO weekly_usage_snapshots(captured_at_utc, week_start_date, "
            "week_end_date, week_start_at, week_end_at, weekly_percent, source, "
            "payload_json) VALUES (?,?,?,NULL,NULL,?,?,?)",
            (captured, week_start_date, week_end_date, pct,
             "fixture", json.dumps({"fixture": True, "dateOnly": True})),
        )


def _insert_entries(cache_conn, entries: list[tuple[dt.datetime, str, int, int, int, int]]) -> None:
    """entries: list of (ts, model, input_tok, output_tok, cache_create, cache_read).
    Writes to the cache.db connection."""
    for i, (ts, model, input_t, output_t, cc, cr) in enumerate(entries):
        cache_conn.execute(
            "INSERT INTO session_entries(source_path, line_offset, timestamp_utc, "
            "model, input_tokens, output_tokens, cache_create_tokens, cache_read_tokens) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"/fx/session-{i}.jsonl", 0, _iso(ts), model, input_t, output_t, cc, cr),
        )


def _build(name: str, as_of: dt.datetime, fn) -> None:
    """Build one fixture.

    We override HOME (not XDG_DATA_HOME) at test invocation because the
    production code at `bin/cctally:30-37` hardcodes
    `APP_DIR = Path.home() / ".local" / "share" / "cctally"`.
    There is no `XDG_DATA_HOME` support. The DB filenames are `stats.db`
    (opened by open_db()) and `cache.db` (opened by open_cache_db()).

    `fn(stats_conn, cache_conn)` seeds both DBs; see scenario helpers.
    """
    out_dir = FIXTURES_DIR / name
    out_dir.mkdir(parents=True, exist_ok=True)
    app_dir = out_dir / ".local" / "share" / "cctally"
    app_dir.mkdir(parents=True, exist_ok=True)

    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    # Idempotent rebuild with the full production schema + WAL mode — delegate
    # to the shared _fixture_builders helpers so schema parity is guaranteed
    # (these handle unlink-if-exists, WAL, and every CREATE TABLE / INDEX /
    # column that open_db() / open_cache_db() would apply).
    create_stats_db(stats_path)
    create_cache_db(cache_path)

    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    fn(stats_conn, cache_conn)
    stats_conn.commit(); stats_conn.close()
    cache_conn.commit(); cache_conn.close()

    # FAKE_HOME is derived from the fixture dir at test time (see
    # bin/cctally-forecast-test run_mode), so input.env only needs AS_OF.
    # Keeping absolute paths out of the committed file keeps fixtures
    # portable across repo locations.
    (out_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")


# --- Scenario: midweek-safe ---------------------------------------------
# Day 4 of 7, 40% used, steady pace. Expect `high < 90` → safe state.
def _midweek_safe(stats_conn, cache_conn):
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 4, 20, 14, 0, 0, tzinfo=dt.timezone.utc)
    # Snapshots every 12h from week_start, linear to 40% at hour 78 (~3.25d).
    samples = [(h, (h / 78.0) * 40.0) for h in (6, 18, 30, 42, 54, 66, 78)]
    _insert_snapshots(stats_conn, week_start, week_end, samples)
    # One entry representing a round-number cost. Pricing for
    # "claude-sonnet-4-6" in CLAUDE_MODEL_PRICING sets the final $
    # (1M input @ $3/M + 800K output @ $15/M = $3 + $12 = $15).
    # Use token counts that sum to a recognizable value so goldens are easy
    # to hand-verify.
    _insert_entries(cache_conn, [
        (week_start + dt.timedelta(hours=40), "claude-sonnet-4-6",
         1_000_000, 800_000, 0, 0),
    ])


# --- Scenario: fresh-week-day1 ------------------------------------------
# 6h elapsed, 3% used -> LOW CONF.
def _fresh_week_day1(stats_conn, cache_conn):
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 4, 20, 14, 0, 0, tzinfo=dt.timezone.utc)
    _insert_snapshots(stats_conn, week_start, week_end, [(2, 1.0), (4, 2.0), (6, 3.0)])
    _insert_entries(cache_conn, [
        (week_start + dt.timedelta(hours=3), "claude-sonnet-4-6",
         200_000, 150_000, 0, 0),
    ])


# --- Scenario: midweek-approaching --------------------------------------
# Day 4, 42% used at 78h, with slight acceleration in the last 24h so that
# r_avg ~= 0.538 pct/h and r_recent ~= 0.625 pct/h. With 90h remaining:
#   low  = 42 + 0.538 * 90 ~= 90.5
#   high = 42 + 0.625 * 90 ~= 98.2
# Both in [90, 100) -> yellow "approaching" state, projected_cap=false.
# Plan originally prescribed p_now=55 which the math forces above 100 with
# 90h of runway, so samples were retuned (adjustment sanctioned by Task 9's
# "adjust fixture shape rather than math" guidance).
def _midweek_approaching(stats_conn, cache_conn):
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 4, 20, 14, 0, 0, tzinfo=dt.timezone.utc)
    samples = [
        (6, 3.0), (18, 9.0), (30, 15.0), (42, 21.0),
        (54, 27.0), (60, 30.0), (72, 37.0), (78, 42.0),
    ]
    _insert_snapshots(stats_conn, week_start, week_end, samples)
    _insert_entries(cache_conn, [
        (week_start + dt.timedelta(hours=40), "claude-sonnet-4-6",
         1_400_000, 1_100_000, 0, 0),
    ])


# --- Scenario: projected-cap --------------------------------------------
# Day 5, 70% used, heavy recent burn -> high >= 100, cap_at populated.
def _projected_cap(stats_conn, cache_conn):
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 4, 20, 14, 0, 0, tzinfo=dt.timezone.utc)
    samples = [(h, (h / 80.0) * 30.0) for h in (6, 18, 30, 42, 54, 66, 78)]
    samples += [(84, 45.0), (90, 60.0), (96, 70.0)]
    _insert_snapshots(stats_conn, week_start, week_end, samples)
    _insert_entries(cache_conn, [
        (week_start + dt.timedelta(hours=50), "claude-sonnet-4-6",
         2_000_000, 1_500_000, 0, 0),
    ])


# --- Scenario: already-capped -------------------------------------------
def _already_capped(stats_conn, cache_conn):
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 4, 20, 14, 0, 0, tzinfo=dt.timezone.utc)
    samples = [(h, min(103.0, h * 1.3)) for h in (6, 24, 48, 72, 96, 120)]
    _insert_snapshots(stats_conn, week_start, week_end, samples)
    _insert_entries(cache_conn, [
        (week_start + dt.timedelta(hours=50), "claude-sonnet-4-6",
         3_000_000, 2_200_000, 0, 0),
    ])


# --- Scenario: zero-prior-weeks -----------------------------------------
# Same shape as midweek-safe but NO prior complete weeks -> source = this_week_sparse
# (p_now=40 is >=10, so selection rule still takes this_week; make p_now=5 to force sparse.)
def _zero_prior_weeks(stats_conn, cache_conn):
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 4, 20, 14, 0, 0, tzinfo=dt.timezone.utc)
    samples = [(h, (h / 78.0) * 5.0) for h in (12, 24, 36, 48, 60, 72, 78)]
    _insert_snapshots(stats_conn, week_start, week_end, samples)
    _insert_entries(cache_conn, [
        (week_start + dt.timedelta(hours=40), "claude-sonnet-4-6",
         200_000, 150_000, 0, 0),
    ])


# --- Scenario: stable-sparse-current ------------------------------------
# Current week has p_now=6 (<10), but 4 eligible prior weeks exist -> median.
def _stable_sparse_current(stats_conn, cache_conn):
    # Four prior complete weeks, each with final_pct=50 and spent=$20 -> $/1% = $0.40.
    for k in range(4, 0, -1):
        ws = dt.datetime(2026, 3, 9 + 7 * (4 - k), 14, 0, 0, tzinfo=dt.timezone.utc)
        we = ws + dt.timedelta(days=7)
        _insert_snapshots(stats_conn, ws, we, [(168, 50.0)])  # single final-week snapshot
        _insert_entries(cache_conn, [
            (ws + dt.timedelta(hours=80), "claude-sonnet-4-6",
             1_600_000, 1_070_000, 0, 0),   # ~= $20 at sonnet-4 pricing
        ])
    # Current week: day 4, 6% used
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 4, 20, 14, 0, 0, tzinfo=dt.timezone.utc)
    samples = [(h, (h / 78.0) * 6.0) for h in (12, 24, 36, 48, 60, 72, 78)]
    _insert_snapshots(stats_conn, week_start, week_end, samples)
    _insert_entries(cache_conn, [
        (week_start + dt.timedelta(hours=40), "claude-sonnet-4-6",
         200_000, 150_000, 0, 0),
    ])


# --- Scenario: date-only-current-week -----------------------------------
# Exercises the fallback in _fetch_current_week_snapshots when the active
# week's snapshots carry week_start_at=NULL / week_end_at=NULL (legacy
# date-only rows from pre-boundary-aware userscripts). The fallback
# synthesizes the window from week_start_date/week_end_date at local
# midnight. Under TZ=UTC that yields [2026-04-13T00Z, 2026-04-20T00Z) = 168h.
# AS_OF=2026-04-16T20Z → elapsed=92h, remaining=76h. Steady ~0.5pct/h pace,
# p_now=42 → HIGH CONF, safe (both proj values < 90%).
def _date_only_current_week(stats_conn, cache_conn):
    week_start = dt.datetime(2026, 4, 13, 0, 0, 0, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 4, 19, 0, 0, 0, tzinfo=dt.timezone.utc)
    samples = [(12, 6.0), (24, 12.0), (36, 18.0), (48, 24.0),
               (60, 30.0), (72, 36.0), (84, 42.0)]
    _insert_snapshots_date_only(stats_conn, week_start, week_end, samples)
    # Cost entry inside the synthesized window. Same $15 recipe as midweek-safe
    # (1M input @ $3/M + 800K output @ $15/M = $15) so it's easy to hand-verify.
    _insert_entries(cache_conn, [
        (week_start + dt.timedelta(hours=30), "claude-sonnet-4-6",
         1_000_000, 800_000, 0, 0),
    ])


# --- Scenario: mixed-boundary-current-week ------------------------------
# Simulates an upgrade-in-progress: 4 early-week NULL-timestamp rows
# (from the pre-upgrade userscript path) followed by 3 boundary-aware
# rows (post-upgrade). Fix 1 in _fetch_current_week_snapshots folds the
# NULL rows into the sample set instead of silently dropping them.
# AS_OF=2026-04-16T20Z, elapsed=92h under TZ=UTC; 7 samples total, p_now=42.
def _mixed_boundary_current_week(stats_conn, cache_conn):
    week_start = dt.datetime(2026, 4, 13, 0, 0, 0, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 4, 19, 0, 0, 0, tzinfo=dt.timezone.utc)
    null_samples = [(12, 6.0), (24, 12.0), (36, 18.0), (48, 24.0)]
    boundary_samples = [(60, 30.0), (72, 36.0), (84, 42.0)]
    _insert_snapshots_date_only(stats_conn, week_start, week_end, null_samples)
    _insert_snapshots(stats_conn, week_start, week_end, boundary_samples)
    _insert_entries(cache_conn, [
        (week_start + dt.timedelta(hours=30), "claude-sonnet-4-6",
         1_000_000, 800_000, 0, 0),
    ])


SCENARIOS = {
    "midweek-safe": (
        dt.datetime(2026, 4, 16, 20, 0, 0, tzinfo=dt.timezone.utc),  # day 4, 78h elapsed
        _midweek_safe,
    ),
    "fresh-week-day1": (
        dt.datetime(2026, 4, 13, 20, 0, 0, tzinfo=dt.timezone.utc),
        _fresh_week_day1,
    ),
    "midweek-approaching": (
        dt.datetime(2026, 4, 16, 20, 0, 0, tzinfo=dt.timezone.utc),
        _midweek_approaching,
    ),
    "projected-cap": (
        dt.datetime(2026, 4, 17, 14, 0, 0, tzinfo=dt.timezone.utc),
        _projected_cap,
    ),
    "already-capped": (
        dt.datetime(2026, 4, 18, 14, 0, 0, tzinfo=dt.timezone.utc),
        _already_capped,
    ),
    "zero-prior-weeks": (
        dt.datetime(2026, 4, 16, 20, 0, 0, tzinfo=dt.timezone.utc),
        _zero_prior_weeks,
    ),
    "stable-sparse-current": (
        dt.datetime(2026, 4, 16, 20, 0, 0, tzinfo=dt.timezone.utc),
        _stable_sparse_current,
    ),
    "date-only-current-week": (
        dt.datetime(2026, 4, 16, 20, 0, 0, tzinfo=dt.timezone.utc),
        _date_only_current_week,
    ),
    "mixed-boundary-current-week": (
        dt.datetime(2026, 4, 16, 20, 0, 0, tzinfo=dt.timezone.utc),
        _mixed_boundary_current_week,
    ),
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Override output directory. Defaults to the in-tree path "
            "tests/fixtures/forecast/. Used by cctally-forecast-test "
            "to write into a per-run scratch dir so the in-tree fixtures "
            "stay byte-stable across harness runs."
        ),
    )
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_DIR = args.out
    for name, (as_of, fn) in SCENARIOS.items():
        _build(name, as_of, fn)
        print(f"built: {name}")
