#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally cache-report`.

Writes one pair of (stats.db, cache.db) per scenario under
tests/fixtures/cache-report/<scenario>/.local/share/cctally/.
All schema/seeding goes through bin/_fixture_builders.py — do not duplicate
schema here. Idempotent: each builder overwrites existing DBs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    FIXED_LAST_INGESTED_AT,
    create_cache_db,
    create_stats_db,
    seed_session_entry,
    seed_session_file,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/cache-report"


def _iso(ts: dt.datetime) -> str:
    """Serialize a datetime as UTC-ISO with `Z` suffix, seconds precision."""
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_healthy_cache_hit():
    """Scenario: one day, two models, high cache_read / low cache_create.
    Verifies the happy path: no anomaly glyph in terminal; JSON
    anomaly.triggered=false on every row; modelBreakdowns has 2 entries."""
    scenario_dir = FIXTURES_DIR / "healthy-cache-hit"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/hch-session.jsonl",
            session_id="hch-healthy-session-uuid",
            project_path="/fake/repos/baseline",
        )
        # Mix of two models in one day. Large cache_read vs. small
        # cache_create → saved > wasted → net > 0 on every entry, so the
        # net_negative trigger is guaranteed off. Both cache_read and
        # cache_create > 0 so the trigger's cache-activity guard is
        # exercised (not bypassed by zero cache activity). Baseline window
        # has only 1 day of data → cache_drop trigger silently skips
        # (min_baseline=5 not met) — no anomaly either way.
        entries = [
            # (hours_back, model, input_t, output_t, cache_create, cache_read)
            ( 10, "claude-opus-4-7",   40_000, 4_000, 20_000, 400_000),
            (  8, "claude-opus-4-7",   30_000, 3_000, 10_000, 300_000),
            (  6, "claude-sonnet-4-6", 20_000, 2_000,  5_000, 150_000),
            (  4, "claude-sonnet-4-6", 10_000, 1_000,  2_500,  80_000),
        ]
        for i, (hours_back, model, input_t, output_t, cc, cr) in enumerate(entries):
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/hch-session.jsonl",
                line_offset=i,
                timestamp_utc=_iso(as_of - dt.timedelta(hours=hours_back)),
                model=model,
                input_tokens=input_t,
                output_tokens=output_t,
                cache_create=cc,
                cache_read=cr,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_net_negative_anomaly():
    """Scenario: one day, heavy cache_create, trivial cache_read.
    Verifies the net_negative anomaly trigger (line 7252):

      * wasted_usd (write premium) >> saved_usd (read discount) →
        net_usd < 0 → trigger fires.
      * Terminal: ⚠ glyph prefixes the Date cell; Net $ column shows a
        negative value.
      * JSON: row's `anomaly.triggered == true`,
              `anomaly.reasons == ["net_negative"]`,
              `netUsd` < 0.

    cache_read is small but non-zero so the cache-activity guard at
    line 7251 (cache_creation + cache_read > 0) is satisfied — the
    trigger is supposed to fire here, not be skipped."""
    scenario_dir = FIXTURES_DIR / "net-negative-anomaly"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/nna-session.jsonl",
            session_id="nna-negative-session-uuid",
            project_path="/fake/repos/wasteful",
        )
        # Heavy cache_create, small cache_read → write premium dominates,
        # net < 0, net_negative fires. Single model keeps the golden
        # narrow (one modelBreakdowns entry). Two entries on the same day
        # so aggregation math is non-trivial.
        entries = [
            # (hours_back, input_t, output_t, cache_create, cache_read)
            ( 10, 20_000, 2_000, 800_000, 5_000),
            (  4, 10_000, 1_000, 600_000, 2_000),
        ]
        for i, (hours_back, input_t, output_t, cc, cr) in enumerate(entries):
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/nna-session.jsonl",
                line_offset=i,
                timestamp_utc=_iso(as_of - dt.timedelta(hours=hours_back)),
                model="claude-opus-4-7",
                input_tokens=input_t,
                output_tokens=output_t,
                cache_create=cc,
                cache_read=cr,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_cache_drop_anomaly():
    """Scenario: 6 consecutive days of entries. First 5 baseline days
    have high cache_hit_percent (~85%); the 6th (latest) day has low
    cache_hit_percent (~40%). Verifies the cache_drop anomaly trigger:

      * Baseline count of OTHER rows = 5 ≥ min_baseline (5 for daily) →
        median is computable.
      * Baseline median (~83%) - latest (~42%) = 41pp ≥ 15pp threshold
        → trigger fires on the latest row.
      * Only the latest row has the ⚠ glyph; baseline rows are clean.
      * Every row's net_usd > 0 (net_negative is NOT triggered), so
        `reasons == ["cache_drop"]` for the latest — trigger isolation
        is clean.

    Baseline window math: _row_anchor for date=2026-04-15 is midnight
    local (= UTC under TZ=UTC); upper_offset=1 day for daily mode;
    baseline window = [2026-04-01T00:00, 2026-04-14T00:00] — the five
    dates 2026-04-10 … 2026-04-14 all fall inclusively within it.
    2026-04-15 itself is the row-under-test and is excluded."""
    scenario_dir = FIXTURES_DIR / "cache-drop-anomaly"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/cda-session.jsonl",
            session_id="cda-drop-session-uuid",
            project_path="/fake/repos/drop",
        )

        # 5 baseline days with high cache %: input=15k, cache_create=5k,
        # cache_read=100k → cache_hit_percent = 100k / (15k+5k+100k) =
        # 100k / 120k ≈ 83.3%. Large cache_read vs. small cache_create
        # ensures saved_usd > wasted_usd → net_usd > 0 (net_negative off).
        # Baseline dates 2026-04-10 … 2026-04-14 at noon UTC so _row_anchor
        # sees midnight of each date comfortably inside the [anchor-14d,
        # anchor-1d] baseline window of the 2026-04-15 row.
        baseline_dates = [
            dt.datetime(2026, 4,  d, 12, 0, 0, tzinfo=dt.timezone.utc)
            for d in (10, 11, 12, 13, 14)
        ]
        line_offset = 0
        for ts in baseline_dates:
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/cda-session.jsonl",
                line_offset=line_offset,
                timestamp_utc=_iso(ts),
                model="claude-opus-4-7",
                input_tokens=15_000,
                output_tokens=1_500,
                cache_create=5_000,
                cache_read=100_000,
            )
            line_offset += 1

        # Latest (2026-04-15) with low cache %: input=30k, cache_create=5k,
        # cache_read=25k → cache_hit_percent = 25k / (30k+5k+25k) =
        # 25k / 60k ≈ 41.7%. Drop = 83.3 - 41.7 = 41.6pp > 15pp → fires.
        # cache_read still > 0 so saved_usd > 0 and net_usd > 0 (small but
        # positive) — net_negative trigger is NOT tripped. Uses 8am UTC
        # so the timestamp's UTC date (_aggregate_cache_by_day keys on
        # entry.timestamp.astimezone(UTC).strftime('%Y-%m-%d')) is
        # 2026-04-15.
        seed_session_entry(
            conn,
            source_path="/fake/jsonl/cda-session.jsonl",
            line_offset=line_offset,
            timestamp_utc=_iso(dt.datetime(2026, 4, 15, 8, 0, 0,
                                          tzinfo=dt.timezone.utc)),
            model="claude-opus-4-7",
            input_tokens=30_000,
            output_tokens=3_000,
            cache_create=5_000,
            cache_read=25_000,
        )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_thin_samples_no_baseline():
    """Scenario: 5 consecutive daily rows, with the latest's cache_hit_percent
    sharply below the others. Verifies the cache_drop trigger's
    silent-skip when baseline-sample count is below min_baseline=5:

      * 5 rows total → from the latest row's perspective, baseline rows
        (OTHER rows in the 14-day trailing window) = 4 < 5.
      * Trigger silently skips — no ⚠ glyph, no JSON reason, no stderr.
      * Every row's net_usd > 0 so net_negative also does NOT fire.

    The cache-% drop is DELIBERATELY present (baselines ~83%, latest
    ~42%) so the test proves the silent-skip GUARD is the reason the
    trigger doesn't fire, not an absence of the input signal. If
    min_baseline ever changes from 5 to 4 and silent-skip disappears,
    this golden will diff — that's by design.

    Baseline dates: 2026-04-11 … 2026-04-14 (inclusive, 4 days). Latest:
    2026-04-15. See cache-drop-anomaly for the 6-row version that fires."""
    scenario_dir = FIXTURES_DIR / "thin-samples-no-baseline"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/tsb-session.jsonl",
            session_id="tsb-thin-session-uuid",
            project_path="/fake/repos/thin",
        )

        # 4 baseline days with high cache %.
        baseline_dates = [
            dt.datetime(2026, 4,  d, 12, 0, 0, tzinfo=dt.timezone.utc)
            for d in (11, 12, 13, 14)
        ]
        line_offset = 0
        for ts in baseline_dates:
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/tsb-session.jsonl",
                line_offset=line_offset,
                timestamp_utc=_iso(ts),
                model="claude-opus-4-7",
                input_tokens=15_000,
                output_tokens=1_500,
                cache_create=5_000,
                cache_read=100_000,
            )
            line_offset += 1

        # Latest (2026-04-15) with low cache %. Drop is present but
        # silent-skip suppresses the cache_drop trigger.
        seed_session_entry(
            conn,
            source_path="/fake/jsonl/tsb-session.jsonl",
            line_offset=line_offset,
            timestamp_utc=_iso(dt.datetime(2026, 4, 15, 8, 0, 0,
                                          tzinfo=dt.timezone.utc)),
            model="claude-opus-4-7",
            input_tokens=30_000,
            output_tokens=3_000,
            cache_create=5_000,
            cache_read=25_000,
        )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_days_flag_trailing_window():
    """Scenario: entries straddling the --days 7 window boundary.
    Verifies _resolve_cache_report_window's default branch under a
    pinned now_utc:

      * since = midnight((now_local - 6days).date()) under TZ=UTC with
        AS_OF=2026-04-15T12:00Z → since = 2026-04-09T00:00Z.
      * until = now_local = 2026-04-15T12:00Z.
      * One entry at 2026-04-08T23:00Z (1h before since) — OUT of window;
        must be absent from rendered rows.
      * Three entries at 2026-04-09T01:00Z (1h into window),
        2026-04-12T12:00Z (mid-window), 2026-04-15T09:00Z (3h before
        until) — IN window; must produce 3 rows on 3 distinct dates."""
    scenario_dir = FIXTURES_DIR / "days-flag-trailing-window"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/dftw-session.jsonl",
            session_id="dftw-window-session-uuid",
            project_path="/fake/repos/window",
        )

        # Keep cache activity healthy and roughly uniform so no anomaly
        # fires — we are testing window boundary, not triggers.
        def _add_entry(line_offset: int, ts: dt.datetime) -> None:
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/dftw-session.jsonl",
                line_offset=line_offset,
                timestamp_utc=_iso(ts),
                model="claude-opus-4-7",
                input_tokens=20_000,
                output_tokens=2_000,
                cache_create=5_000,
                cache_read=120_000,
            )

        # Out-of-window entry (1 hour before since boundary).
        _add_entry(0, dt.datetime(2026, 4, 8, 23, 0, 0, tzinfo=dt.timezone.utc))

        # In-window entries on three distinct dates.
        _add_entry(1, dt.datetime(2026, 4,  9,  1, 0, 0, tzinfo=dt.timezone.utc))
        _add_entry(2, dt.datetime(2026, 4, 12, 12, 0, 0, tzinfo=dt.timezone.utc))
        _add_entry(3, dt.datetime(2026, 4, 15,  9, 0, 0, tzinfo=dt.timezone.utc))
        conn.commit()

    # Note: FLAGS="--days 7" layers ON TOP of the per-mode --json flag —
    # the run_mode helper merges both.
    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\n'
        'FLAGS="--days 7"\n'
    )


def build_by_session_merged_resume():
    """Scenario: --by-session mode + one sessionId spanning TWO
    session_files rows. Verifies _aggregate_cache_by_session's resume-
    merge path:

      * Two session_files rows, same session_id, different path, same
        project_path.
      * Entries on BOTH paths; totals in output row are the sum across
        both files.
      * Terminal: ONE row (the merge fired — not two rows).
      * JSON: sessions array length 1. sessions[0].sourcePaths is a
        sorted 2-element list with both paths. sessions[0].sessionId is
        the shared sessionId (NOT a filename-stem fallback).
        sessions[0].lastActivity == timestamp of the most-recent entry
        across both files.

    Healthy cache activity (high cache_read) on every entry → no
    anomaly triggers fire. We are testing the merge path, not triggers."""
    scenario_dir = FIXTURES_DIR / "by-session-merged-resume"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        # Two session_files rows: SAME session_id, DIFFERENT paths,
        # SAME project_path.
        seed_session_file(
            conn,
            path="/fake/jsonl/bsmr-original.jsonl",
            session_id="bsmr-resumed-session-uuid",
            project_path="/fake/repos/merged",
        )
        seed_session_file(
            conn,
            path="/fake/jsonl/bsmr-resumed.jsonl",
            session_id="bsmr-resumed-session-uuid",
            project_path="/fake/repos/merged",
        )
        # Entries across both files. The resumed file carries the
        # most-recent entry (hours_back=2), so lastActivity in JSON
        # should equal as_of - 2h.
        entries = [
            # (src, hours_back, input_t, output_t, cache_create, cache_read)
            ("/fake/jsonl/bsmr-original.jsonl", 20, 25_000, 2_500,  5_000, 120_000),
            ("/fake/jsonl/bsmr-original.jsonl", 14, 15_000, 1_500,  3_000,  90_000),
            ("/fake/jsonl/bsmr-resumed.jsonl",   8, 30_000, 3_000,  6_000, 140_000),
            ("/fake/jsonl/bsmr-resumed.jsonl",   2, 20_000, 2_000,  4_000, 100_000),
        ]
        for i, (src, hb, inp, out, cc, cr) in enumerate(entries):
            seed_session_entry(
                conn,
                source_path=src,
                line_offset=i,
                timestamp_utc=_iso(as_of - dt.timedelta(hours=hb)),
                model="claude-opus-4-7",
                input_tokens=inp,
                output_tokens=out,
                cache_create=cc,
                cache_read=cr,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\n'
        'FLAGS="--by-session"\n'
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Override output directory. Defaults to the in-tree path "
            "tests/fixtures/cache-report/. Used by "
            "cctally-cache-report-test to write into a per-run scratch dir "
            "so the in-tree fixtures stay byte-stable across harness runs."
        ),
    )
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_DIR = args.out
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    build_healthy_cache_hit()
    build_net_negative_anomaly()
    build_cache_drop_anomaly()
    build_thin_samples_no_baseline()
    build_days_flag_trailing_window()
    build_by_session_merged_resume()
    print(f"Built fixtures under {FIXTURES_DIR}")
