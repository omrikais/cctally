#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally weekly`.

Writes one pair of (stats.db, cache.db) per scenario under
tests/fixtures/weekly/<scenario>/.local/share/cctally/.
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
    seed_week_reset_event,
    seed_weekly_usage_snapshot,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/weekly"


def _iso(ts: dt.datetime) -> str:
    """Serialize a datetime as UTC-ISO with `Z` suffix, seconds precision."""
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _week_bounds_for(ts: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Anchor a 7-day window starting Monday 00:00 UTC of ts's ISO week.
    Matches the week-bounds helper in build-project-fixtures.py so scenario
    authors reason about identical anchors across builders."""
    monday = (ts - dt.timedelta(days=ts.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=dt.timezone.utc
    )
    return monday, monday + dt.timedelta(days=7)


def build_current_week_partial():
    """Scenario: data through pinned AS_OF mid-week with an anchored
    weekly_usage_snapshots row for the current week. Verifies the
    overlay lookup path (get_latest_usage_for_week) hits a direct
    ISO-timestamp match."""
    scenario_dir = FIXTURES_DIR / "current-week-partial"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    create_stats_db(db_dir / "stats.db")
    with sqlite3.connect(db_dir / "stats.db") as conn:
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=_iso(as_of),
            week_start_date=week_start.date().isoformat(),
            week_end_date=week_end.date().isoformat(),
            week_start_at=_iso(week_start),
            week_end_at=_iso(week_end),
            weekly_percent=35.0,
        )

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        # Two sessions across Mon/Tue/Wed of the current week. All
        # timestamps fall inside [week_start, as_of]. Goldens lock exact
        # dollar values under current CLAUDE_MODEL_PRICING — any pricing
        # update or token-count change requires regenerating the goldens.
        seed_session_file(
            conn,
            path="/fake/jsonl/cwp-a.jsonl",
            session_id="cwp-session-a",
            project_path="/fake/repos/alpha",
        )
        seed_session_file(
            conn,
            path="/fake/jsonl/cwp-b.jsonl",
            session_id="cwp-session-b",
            project_path="/fake/repos/beta",
        )
        # Entry timestamps are as_of - ts_offset; inline labels below show
        # the resulting weekday + UTC hour so the seeded spread is visible
        # at a glance (as_of is Wed 2026-04-15 15:00 UTC).
        for i, (src, ts_offset, model, input_t, output_t) in enumerate([
            ("/fake/jsonl/cwp-a.jsonl",
             dt.timedelta(days=2, hours=10),  # Mon 05:00
             "claude-opus-4-7", 500_000, 50_000),
            ("/fake/jsonl/cwp-a.jsonl",
             dt.timedelta(days=1, hours=14),  # Tue 01:00
             "claude-opus-4-7", 300_000, 30_000),
            ("/fake/jsonl/cwp-b.jsonl",
             dt.timedelta(hours=5),           # Wed 10:00
             "claude-sonnet-4-6", 400_000, 40_000),
        ]):
            seed_session_entry(
                conn,
                source_path=src,
                line_offset=i,
                timestamp_utc=_iso(as_of - ts_offset),
                model=model,
                input_tokens=input_t,
                output_tokens=output_t,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_multi_week_anchored():
    """Scenario: three consecutive subscription weeks all with
    weekly_usage_snapshots anchors. Default ascending order; also
    drives the harness's --order desc mode via golden-desc.txt presence."""
    scenario_dir = FIXTURES_DIR / "multi-week-anchored"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    wk2_start, wk2_end = _week_bounds_for(as_of)                         # current
    wk1_start, wk1_end = wk2_start - dt.timedelta(days=7), wk2_start     # prior
    wk0_start, wk0_end = wk1_start - dt.timedelta(days=7), wk1_start     # two back

    create_stats_db(db_dir / "stats.db")
    with sqlite3.connect(db_dir / "stats.db") as conn:
        for ws, we, cap, pct in [
            (wk0_start, wk0_end, wk0_start + dt.timedelta(days=3), 40.0),
            (wk1_start, wk1_end, wk1_start + dt.timedelta(days=3), 75.0),
            (wk2_start, wk2_end, as_of, 20.0),
        ]:
            seed_weekly_usage_snapshot(
                conn,
                captured_at_utc=_iso(cap),
                week_start_date=ws.date().isoformat(),
                week_end_date=we.date().isoformat(),
                week_start_at=_iso(ws),
                week_end_at=_iso(we),
                weekly_percent=pct,
            )

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        # One Opus entry per week. Different token counts so weekly totalCost
        # differs — verifies the --order desc mode is actually reordering
        # rows, not just reversing a list of identical rows.
        seed_session_file(
            conn,
            path="/fake/jsonl/mwa.jsonl",
            session_id="mwa-session",
            project_path="/fake/repos/mwa",
        )
        for i, (ws_anchor, tokens) in enumerate([
            (wk0_start, 200_000),
            (wk1_start, 600_000),
            (wk2_start, 100_000),
        ]):
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/mwa.jsonl",
                line_offset=i,
                timestamp_utc=_iso(ws_anchor + dt.timedelta(days=2, hours=10)),
                model="claude-opus-4-7",
                input_tokens=tokens,
                output_tokens=tokens // 10,
            )
        conn.commit()

    # No FLAGS line: cmd_weekly has no --weeks flag; the default range
    # ([2020-01-01, AS_OF]) already spans all three anchored weeks, and
    # with only these three weeks seeded the output is fully deterministic.
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_pre_snapshot_extrapolation():
    """Scenario: entries span four subscription weeks but only the current
    (most-recent) week has a weekly_usage_snapshots anchor. Verifies the
    7-day-multiple extrapolation path in _compute_subscription_weeks and
    the empty-overlay path in cmd_weekly for earlier weeks."""
    scenario_dir = FIXTURES_DIR / "pre-snapshot-extrapolation"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    current_start, current_end = _week_bounds_for(as_of)

    create_stats_db(db_dir / "stats.db")
    with sqlite3.connect(db_dir / "stats.db") as conn:
        # Snapshot ONLY on the current week. Earlier weeks have entries but
        # no snapshots — _compute_subscription_weeks extrapolates backward.
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=_iso(as_of),
            week_start_date=current_start.date().isoformat(),
            week_end_date=current_end.date().isoformat(),
            week_start_at=_iso(current_start),
            week_end_at=_iso(current_end),
            weekly_percent=15.0,
        )

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/pse.jsonl",
            session_id="pse-session",
            project_path="/fake/repos/pse",
        )
        # One entry per week, 4 weeks total. Use week_start - 21d, -14d, -7d, 0d.
        for i, offset_days in enumerate([21, 14, 7, 0]):
            ts = current_start - dt.timedelta(days=offset_days) \
                + dt.timedelta(days=1, hours=10)  # Tuesday 10:00 of each week
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/pse.jsonl",
                line_offset=i,
                timestamp_utc=_iso(ts),
                model="claude-opus-4-7",
                input_tokens=300_000,
                output_tokens=30_000,
            )
        conn.commit()

    # No FLAGS: cmd_weekly has no --weeks flag; default range [2020-01-01,
    # AS_OF] covers all seeded entries. (The plan originally prescribed
    # FLAGS="--weeks 4" by mistake — confirmed via `weekly --help` that
    # no --weeks flag exists on this subcommand. See Task 3 commit.)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_hour_jitter_normalization():
    """Scenario: snapshot week_start_at values at :03 and :58 minute offsets.
    Verifies production's round-to-nearest-hour normalization produces on-the-hour
    anchors in the rendered output."""
    scenario_dir = FIXTURES_DIR / "hour-jitter-normalization"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    # Intentional minutes-off-the-hour values to exercise rounding.
    jitter_start_a = dt.datetime(2026, 4,  6, 15,  3, 0, tzinfo=dt.timezone.utc)  # :03 past 15:00
    jitter_end_a   = dt.datetime(2026, 4, 13, 15,  3, 0, tzinfo=dt.timezone.utc)
    jitter_start_b = dt.datetime(2026, 4, 13, 14, 58, 0, tzinfo=dt.timezone.utc)  # :58 → rounds to 15:00
    jitter_end_b   = dt.datetime(2026, 4, 20, 14, 58, 0, tzinfo=dt.timezone.utc)

    create_stats_db(db_dir / "stats.db")
    with sqlite3.connect(db_dir / "stats.db") as conn:
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=_iso(jitter_start_a + dt.timedelta(days=1)),
            week_start_date=jitter_start_a.date().isoformat(),
            week_end_date=jitter_end_a.date().isoformat(),
            week_start_at=_iso(jitter_start_a),
            week_end_at=_iso(jitter_end_a),
            weekly_percent=45.0,
        )
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=_iso(as_of),
            week_start_date=jitter_start_b.date().isoformat(),
            week_end_date=jitter_end_b.date().isoformat(),
            week_start_at=_iso(jitter_start_b),
            week_end_at=_iso(jitter_end_b),
            weekly_percent=22.0,
        )

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/hjn.jsonl",
            session_id="hjn-session",
            project_path="/fake/repos/hjn",
        )
        for i, ts in enumerate([
            jitter_start_a + dt.timedelta(days=2, hours=5),   # prior week
            jitter_start_b + dt.timedelta(days=1, hours=5),   # current week
        ]):
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/hjn.jsonl",
                line_offset=i,
                timestamp_utc=_iso(ts),
                model="claude-opus-4-7",
                input_tokens=250_000,
                output_tokens=25_000,
            )
        conn.commit()

    # No FLAGS: cmd_weekly has no --weeks flag; default range
    # [2020-01-01, AS_OF] covers all seeded entries.
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_mixed_boundary_fallback():
    """Scenario: two weekly_usage_snapshots rows — one with week_start_at
    populated (ISO path), one with week_start_at IS NULL (legacy date-only
    fallback). Verifies the per-bucket overlay lookup path
    (get_latest_usage_for_week → make_week_ref) handles both cases.

    Note: _compute_subscription_weeks filters NULL-anchor rows at SQL
    (week_start_at IS NOT NULL), so the legacy row contributes NO anchor
    to subscription-week enumeration. The prior week therefore emerges
    from the 7-day-multiple extrapolation walk back from the current
    week's ISO anchor. The focus is the per-bucket overlay lookup in
    cmd_weekly (line 8080): get_latest_usage_for_week matches on
    week_start_date (date-only) regardless of week_start_at, so the
    legacy row IS found when its week_start_date aligns with the
    extrapolated SubWeek.start_date. Behavior locked in by the golden."""
    scenario_dir = FIXTURES_DIR / "mixed-boundary-fallback"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    current_start, current_end = _week_bounds_for(as_of)
    prior_start, prior_end = current_start - dt.timedelta(days=7), current_start

    create_stats_db(db_dir / "stats.db")
    with sqlite3.connect(db_dir / "stats.db") as conn:
        # Prior week: legacy row (week_start_at IS NULL).
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=_iso(prior_start + dt.timedelta(days=3)),
            week_start_date=prior_start.date().isoformat(),
            week_end_date=prior_end.date().isoformat(),
            weekly_percent=60.0,
            # week_start_at/week_end_at OMITTED (default None) — legacy path.
        )
        # Current week: fully-populated ISO row.
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=_iso(as_of),
            week_start_date=current_start.date().isoformat(),
            week_end_date=current_end.date().isoformat(),
            week_start_at=_iso(current_start),
            week_end_at=_iso(current_end),
            weekly_percent=20.0,
        )

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/mbf.jsonl",
            session_id="mbf-session",
            project_path="/fake/repos/mbf",
        )
        for i, ts in enumerate([
            prior_start + dt.timedelta(days=2, hours=10),
            current_start + dt.timedelta(days=1, hours=10),
        ]):
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/mbf.jsonl",
                line_offset=i,
                timestamp_utc=_iso(ts),
                model="claude-opus-4-7",
                input_tokens=300_000,
                output_tokens=30_000,
            )
        conn.commit()

    # No FLAGS: cmd_weekly has no --weeks flag; default range
    # [2020-01-01, AS_OF] covers all seeded entries.
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_breakdown_per_model():
    """Scenario: one anchored week with three models contributing different
    token counts. FLAGS='--breakdown' renders per-model child rows."""
    scenario_dir = FIXTURES_DIR / "breakdown-per-model"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    create_stats_db(db_dir / "stats.db")
    with sqlite3.connect(db_dir / "stats.db") as conn:
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=_iso(as_of),
            week_start_date=week_start.date().isoformat(),
            week_end_date=week_end.date().isoformat(),
            week_start_at=_iso(week_start),
            week_end_at=_iso(week_end),
            weekly_percent=50.0,
        )

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/bpm.jsonl",
            session_id="bpm-session",
            project_path="/fake/repos/bpm",
        )
        # Three models, distinct token counts so breakdown rows differ.
        for i, (model, input_t, output_t) in enumerate([
            ("claude-opus-4-7",   800_000, 80_000),
            ("claude-sonnet-4-6", 400_000, 40_000),
            ("claude-haiku-4-5",  200_000, 20_000),
        ]):
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/bpm.jsonl",
                line_offset=i,
                timestamp_utc=_iso(week_start + dt.timedelta(days=1, hours=10 + i)),
                model=model,
                input_tokens=input_t,
                output_tokens=output_t,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--breakdown"\n'
    )


def build_empty_range():
    """Scenario: --since / --until bracket a period with no entries.
    Terminal output is the 'No Claude usage found.' sentinel; JSON is an
    empty weekly array. Data exists OUTSIDE the slice so the empty result
    is specifically due to the range filter, not an empty DB."""
    scenario_dir = FIXTURES_DIR / "empty-range"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    # Data lives 4 weeks ago; slice asks for a 2-week window ending today
    # (no overlap).
    old_ts = as_of - dt.timedelta(days=28)
    old_week_start, old_week_end = _week_bounds_for(old_ts)

    create_stats_db(db_dir / "stats.db")
    with sqlite3.connect(db_dir / "stats.db") as conn:
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=_iso(old_ts),
            week_start_date=old_week_start.date().isoformat(),
            week_end_date=old_week_end.date().isoformat(),
            week_start_at=_iso(old_week_start),
            week_end_at=_iso(old_week_end),
            weekly_percent=50.0,
        )

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/er.jsonl",
            session_id="er-session",
            project_path="/fake/repos/er",
        )
        seed_session_entry(
            conn,
            source_path="/fake/jsonl/er.jsonl",
            line_offset=0,
            timestamp_utc=_iso(old_ts),
            model="claude-opus-4-7",
            input_tokens=300_000,
            output_tokens=30_000,
        )
        conn.commit()

    # Slice last 2 weeks — no entries fall in this window.
    since_date = (as_of - dt.timedelta(days=14)).date().isoformat()
    until_date = as_of.date().isoformat()
    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\n'
        f'FLAGS="--since {since_date} --until {until_date}"\n'
    )


def build_reset_event_rebucketing():
    """Scenario: an early mid-week reset shifts the post-reset snapshot's
    API-derived `week_start_at` BACKWARD into the pre-reset week. Without
    `_apply_reset_events_to_subweeks`, `_apply_overlap_clamp_to_subweeks`
    then clamps the pre-reset week's end to the backdated start, slicing
    pre-reset cost into the post-reset bucket.

    Shape (mirrors the production observation, sized for clarity):
        AS_OF                = 2026-04-25T15:00Z
        week-1 (pre-reset):  start_at = 2026-04-09T15:00Z
                             end_at   = 2026-04-16T15:00Z
                             pct      = 60.0
        week-2 (post-reset): start_at = 2026-04-11T15:00Z (= new_resets_at - 7d, BACKDATES)
                             end_at   = 2026-04-18T15:00Z
                             pct      = 25.0
        reset event:         old_week_end_at        = 2026-04-16T15:00Z
                             new_week_end_at        = 2026-04-18T15:00Z
                             effective_reset_at_utc = 2026-04-13T18:00Z

    After fix:
      - week-1 ends at 2026-04-13T18:00Z (reset moment), not 2026-04-16T15:00Z.
      - week-2 starts at 2026-04-13T18:00Z, not 2026-04-11T15:00Z.

    Cost entries: two per week so the bucket boundary is easy to read in the
    rendered table. The 2026-04-12T12:00Z entry is the demonstrative one —
    pre-fix it bucket-keys to week-2 (post-reset backdated start <= 04-12);
    post-fix it bucket-keys to week-1 (reset moment = 04-13T18:00Z > 04-12)."""
    scenario_dir = FIXTURES_DIR / "reset-event-rebucketing"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 25, 15, 0, 0, tzinfo=dt.timezone.utc)

    wk1_start = dt.datetime(2026, 4,  9, 15, 0, 0, tzinfo=dt.timezone.utc)
    wk1_end   = dt.datetime(2026, 4, 16, 15, 0, 0, tzinfo=dt.timezone.utc)
    wk2_start = dt.datetime(2026, 4, 11, 15, 0, 0, tzinfo=dt.timezone.utc)  # BACKDATED
    wk2_end   = dt.datetime(2026, 4, 18, 15, 0, 0, tzinfo=dt.timezone.utc)
    reset_at  = dt.datetime(2026, 4, 13, 18, 0, 0, tzinfo=dt.timezone.utc)

    create_stats_db(db_dir / "stats.db")
    with sqlite3.connect(db_dir / "stats.db") as conn:
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=_iso(wk1_start + dt.timedelta(days=2)),
            week_start_date=wk1_start.date().isoformat(),
            week_end_date=wk1_end.date().isoformat(),
            week_start_at=_iso(wk1_start),
            week_end_at=_iso(wk1_end),
            weekly_percent=60.0,
        )
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=_iso(as_of),
            week_start_date=wk2_start.date().isoformat(),
            week_end_date=wk2_end.date().isoformat(),
            week_start_at=_iso(wk2_start),
            week_end_at=_iso(wk2_end),
            weekly_percent=25.0,
        )
        seed_week_reset_event(
            conn,
            detected_at_utc=_iso(reset_at + dt.timedelta(minutes=1)),
            old_week_end_at=_iso(wk1_end),
            new_week_end_at=_iso(wk2_end),
            effective_reset_at_utc=_iso(reset_at),
        )

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/rer.jsonl",
            session_id="rer-session",
            project_path="/fake/repos/rer",
        )
        # Two entries per week (pre-fix vs. post-fix bucket boundary):
        #   2026-04-10T12:00Z — week-1 (before reset, before any backdated start)
        #   2026-04-12T12:00Z — week-1 post-fix; week-2 pre-fix (demonstrative)
        #   2026-04-14T12:00Z — week-2 (after reset)
        #   2026-04-17T12:00Z — week-2 (after reset)
        for i, ts in enumerate([
            dt.datetime(2026, 4, 10, 12, 0, 0, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 4, 12, 12, 0, 0, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 4, 14, 12, 0, 0, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 4, 17, 12, 0, 0, tzinfo=dt.timezone.utc),
        ]):
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/rer.jsonl",
                line_offset=i,
                timestamp_utc=_iso(ts),
                model="claude-opus-4-7",
                input_tokens=500_000,
                output_tokens=50_000,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Override output directory. Defaults to the in-tree path "
            "tests/fixtures/weekly/. Used by cctally-weekly-test "
            "to write into a per-run scratch dir so the in-tree fixtures "
            "stay byte-stable across harness runs."
        ),
    )
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_DIR = args.out
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    build_current_week_partial()
    build_multi_week_anchored()
    build_pre_snapshot_extrapolation()
    build_hour_jitter_normalization()
    build_mixed_boundary_fallback()
    build_breakdown_per_model()
    build_empty_range()
    build_reset_event_rebucketing()
    print(f"Built fixtures under {FIXTURES_DIR}")
