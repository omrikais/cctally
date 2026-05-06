#!/usr/bin/env python3
"""Build the marketing SQLite fixture for the public README screenshots.

Writes a fake-home tree at the requested out_dir so HOME=<scratch>/home
resolves the production layout (~/.local/share/cctally/{stats.db,cache.db,
config.json}).

Content (per docs/superpowers/specs/2026-05-05-public-readme-design.md):
- 8 weeks of weekly_usage_snapshots + weekly_cost_snapshots with a gentle
  upward trend
- session_entries spanning the current week across 4 projects (web-app,
  api-gateway, data-pipeline, mobile-client) with Sonnet/Opus/Haiku mix
- 5h block data for the current week and a few prior windows; the open
  block's `five_hour_window_key` is mirrored onto the latest
  weekly_usage_snapshots row so the dashboard's current-week panel can
  bind to a non-null `current_week.five_hour_block` (CLAUDE.md gotcha:
  "Dashboard `current_week.five_hour_block` binds to the latest
  snapshot's `five_hour_window_key`...").
- Three current-week weekly_usage_snapshots rows (as_of, as_of-12h,
  as_of-24h) so the forecast modal lands on `confidence: high` and
  surfaces a recent-24h projection.
- percent_milestones rows so the TUI hero shot displays crossings
- config.json pinning `display.tz = "America/Los_Angeles"` so the
  dashboard + CLI render dates in LA time regardless of host TZ
  (otherwise screenshots inherit the maintainer's IDT).

Today-anchored: --as-of (default: today UTC) shifts every date so screenshots
don't visibly age.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    seed_session_entry,
    seed_session_file,
    seed_weekly_usage_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "tests/fixtures/readme/home/.local/share/cctally"
DEFAULT_TUI_SNAPSHOT = REPO_ROOT / "tests/fixtures/readme/tui_snapshot.py"

PROJECTS = ("web-app", "api-gateway", "data-pipeline", "mobile-client")
MODELS = (
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-haiku-4-5-20251001",
)


def DEFAULT_AS_OF_FN() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_weekly_cost_snapshot(
    conn: sqlite3.Connection,
    *,
    week_start_date: str,
    week_end_date: str,
    week_start_at: str,
    week_end_at: str,
    cost_usd: float,
    captured_at_utc: str,
) -> None:
    """No helper exists in _fixture_builders.py for this table; inline raw SQL.

    Schema source: production `INSERT INTO weekly_cost_snapshots` in bin/cctally.
    Required columns: captured_at_utc, week_start_date, week_end_date,
    week_start_at, week_end_at, cost_usd. `mode` and `project` default per
    schema; range_start_iso/range_end_iso default to week boundaries to mirror
    a `range-cost`-style snapshot.
    """
    conn.execute(
        """INSERT INTO weekly_cost_snapshots
           (captured_at_utc, week_start_date, week_end_date,
            week_start_at, week_end_at,
            range_start_iso, range_end_iso,
            cost_usd, source, mode, project)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            captured_at_utc,
            week_start_date,
            week_end_date,
            week_start_at,
            week_end_at,
            week_start_at,
            week_end_at,
            cost_usd,
            "build-readme-fixtures",
            "auto",
            None,
        ),
    )


def _seed_percent_milestone(
    conn: sqlite3.Connection,
    *,
    week_start_date: str,
    week_end_date: str,
    week_start_at: str,
    week_end_at: str,
    percent_threshold: int,
    captured_at_utc: str,
    cumulative_cost_usd: float,
    marginal_cost_usd: float,
    five_hour_percent_at_crossing: Optional[float] = None,
) -> None:
    """Schema source: production `INSERT OR IGNORE INTO percent_milestones`
    in bin/cctally:9614. Required NOT NULL columns: captured_at_utc,
    week_start_date, week_end_date, percent_threshold, cumulative_cost_usd,
    usage_snapshot_id, cost_snapshot_id. usage_snapshot_id / cost_snapshot_id
    are arbitrary integers in the fixture (no FK enforcement)."""
    conn.execute(
        """INSERT OR IGNORE INTO percent_milestones
           (captured_at_utc, week_start_date, week_end_date,
            week_start_at, week_end_at,
            percent_threshold,
            cumulative_cost_usd, marginal_cost_usd,
            usage_snapshot_id, cost_snapshot_id,
            five_hour_percent_at_crossing)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            captured_at_utc,
            week_start_date,
            week_end_date,
            week_start_at,
            week_end_at,
            percent_threshold,
            cumulative_cost_usd,
            marginal_cost_usd,
            0,  # usage_snapshot_id — fixture-arbitrary; no FK
            0,  # cost_snapshot_id — fixture-arbitrary; no FK
            five_hour_percent_at_crossing,
        ),
    )


def _seed_five_hour_block(
    conn: sqlite3.Connection,
    *,
    five_hour_window_key: int,
    five_hour_resets_at: str,
    block_start_at: str,
    first_observed_at_utc: str,
    last_observed_at_utc: str,
    final_five_hour_percent: float,
    seven_day_pct_at_block_start: float,
    seven_day_pct_at_block_end: float,
    total_input_tokens: int,
    total_output_tokens: int,
    total_cache_create_tokens: int,
    total_cache_read_tokens: int,
    total_cost_usd: float,
    is_closed: int,
    created_at_utc: str,
    last_updated_at_utc: str,
    crossed_seven_day_reset: int = 0,
) -> None:
    """Schema source: production `INSERT INTO five_hour_blocks`
    in bin/cctally:10076."""
    conn.execute(
        """INSERT INTO five_hour_blocks
           (five_hour_window_key, five_hour_resets_at, block_start_at,
            first_observed_at_utc, last_observed_at_utc,
            final_five_hour_percent,
            seven_day_pct_at_block_start, seven_day_pct_at_block_end,
            crossed_seven_day_reset,
            total_input_tokens, total_output_tokens,
            total_cache_create_tokens, total_cache_read_tokens,
            total_cost_usd,
            is_closed, created_at_utc, last_updated_at_utc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            five_hour_window_key,
            five_hour_resets_at,
            block_start_at,
            first_observed_at_utc,
            last_observed_at_utc,
            final_five_hour_percent,
            seven_day_pct_at_block_start,
            seven_day_pct_at_block_end,
            crossed_seven_day_reset,
            total_input_tokens,
            total_output_tokens,
            total_cache_create_tokens,
            total_cache_read_tokens,
            total_cost_usd,
            is_closed,
            created_at_utc,
            last_updated_at_utc,
        ),
    )


def _populate_weeks(
    stats_conn: sqlite3.Connection,
    *,
    as_of: dt.datetime,
    open_block_window_key: int,
    open_block_final_pct: float,
    open_block_resets_at_iso: str,
) -> None:
    """Seed 8 weekly snapshots ending at as_of's containing week.

    Narrative arc: the user starts at a higher $/1% (~$0.65, less
    efficient), gradually improves through the middle weeks (~$0.43–$0.50),
    then regresses slightly in the most recent closed week and the
    in-progress current week. The visible variance in $/1% is the
    storytelling spine of the Trend chart screenshot — without it, the
    sparkline / chart goes flat and the modal's `median $0.59` label
    collides with the chart line.

    Current-week (i=7) `weekly_percent` is INTENTIONALLY lower than the
    immediately-prior closed week (W-1, i=6) because the current week is
    in progress. Combined with the as_of-aligned-to-Thursday change in
    `build()`, this gives the forecast a clearly-WARN ~103% projection
    that fits within the modal's right edge (the prior 59%/Tuesday combo
    yielded a 261% projection that clipped the pill).

    | i | label   | weekly_pct | cost  | $/1%  |
    |---|---------|-----------:|------:|------:|
    | 0 | W-7     |       38.0 | 24.70 | 0.650 |
    | 1 | W-6     |       41.0 | 25.83 | 0.630 |
    | 2 | W-5     |       44.0 | 25.96 | 0.590 |
    | 3 | W-4     |       47.0 | 24.91 | 0.530 |
    | 4 | W-3     |       50.0 | 25.00 | 0.500 |
    | 5 | W-2     |       53.0 | 22.79 | 0.430 |
    | 6 | W-1     |       56.0 | 25.20 | 0.450 |
    | 7 | current |       53.0 | 28.12 | 0.530 |

    Current-week multiple snapshots: to lift `forecast.confidence` to
    "high" we seed three rows for i=7 (snapshot_count >= 3 AND at least
    one sample with captured_at <= now-24h, see `_assess_forecast_confidence`
    + `has_sample_ge_24h` gate in `_load_forecast_inputs`). The 24h-ago
    sample is at 42.2% so r_recent = (53.0-42.2)/24 = 0.45 %/h, giving a
    ~90% recent-24h projection — clearly below the week-avg ~103%, lands
    in the WARN range without clipping. Latest snapshot (captured at
    as_of) carries the open 5h block's `five_hour_window_key` so the
    dashboard's current_week panel can bind to `five_hour_block`.

    `open_block_window_key` is computed by `_populate_blocks` and threaded
    in here so the latest snapshot's `five_hour_window_key` matches the
    open block's key 1:1 (per the CLAUDE.md gotcha).
    """
    week_start = (as_of - dt.timedelta(days=as_of.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    # round() to keep IEEE-754 trail noise out of the snapshot values
    # (e.g. 2.4000000000000004 → 2.4) so deterministic-dump tests stay
    # stable across Python releases.
    weekly_series = [
        (38.0, 24.70),
        (41.0, 25.83),
        (44.0, 25.96),
        (47.0, 24.91),
        (50.0, 25.00),
        (53.0, 22.79),
        (56.0, 25.20),
        (53.0, 28.12),  # current (in-progress) week — matches live sum
                        # of seeded session_entries so cli-report.svg's cost
                        # column and cli-forecast.svg's spent_usd line agree.
    ]
    for i, (weekly_pct, cost) in enumerate(weekly_series):
        offset = 7 - i  # i=0 = oldest, i=7 = current
        wstart = week_start - dt.timedelta(days=7 * offset)
        wend = wstart + dt.timedelta(days=7)
        weekly_pct = round(weekly_pct, 2)
        cost = round(cost, 2)
        if i < 7:
            # Closed weeks: one captured-at-end-of-week snapshot.
            captured = wend - dt.timedelta(seconds=1)
            seed_weekly_usage_snapshot(
                stats_conn,
                captured_at_utc=_iso(captured),
                week_start_date=wstart.strftime("%Y-%m-%d"),
                week_end_date=wend.strftime("%Y-%m-%d"),
                week_start_at=_iso(wstart),
                week_end_at=_iso(wend),
                weekly_percent=weekly_pct,
                five_hour_percent=18.0,
                five_hour_resets_at=_iso(wend),
                payload_json="{}",
                source="build-readme-fixtures",
            )
        else:
            # Current week: 3 snapshots so forecast hits `confidence=high`.
            # The 24h-ago and 12h-ago samples seed `r_recent` for the
            # forecast modal's "Recent 24h" projection. ONLY the latest
            # snapshot (captured_at = as_of) carries `five_hour_window_key`
            # — `_select_current_block_for_envelope` picks the latest row
            # by captured_at_utc DESC, so the prior two stay NULL on that
            # column to avoid stale-block ambiguity.
            # Latest sample (captured_at = as_of) MUST mirror the open
            # block's final_five_hour_percent + five_hour_resets_at —
            # `_tui_build_current_week` reads these scalar fields off the
            # newest snapshot to populate `current_week.five_hour_pct` /
            # `five_hour_resets_in_sec`, which the React Header chip and
            # CurrentWeekPanel render directly. Older samples never reach
            # those readers (only the latest row by `captured_at_utc DESC`
            # does) so they keep the closed-week-default 18.0 / weekly-end
            # placeholders without affecting any rendered surface.
            current_week_samples = [
                # (captured_at, weekly_pct, five_hour_window_key,
                #  five_hour_pct, five_hour_resets_at)
                (as_of - dt.timedelta(hours=24), 42.2, None,
                    18.0, _iso(wend)),
                (as_of - dt.timedelta(hours=12), 47.6, None,
                    18.0, _iso(wend)),
                (as_of,                          53.0, open_block_window_key,
                    open_block_final_pct, open_block_resets_at_iso),
            ]
            for (captured, sample_pct, window_key,
                 fh_pct, fh_resets_at) in current_week_samples:
                seed_weekly_usage_snapshot(
                    stats_conn,
                    captured_at_utc=_iso(captured),
                    week_start_date=wstart.strftime("%Y-%m-%d"),
                    week_end_date=wend.strftime("%Y-%m-%d"),
                    week_start_at=_iso(wstart),
                    week_end_at=_iso(wend),
                    weekly_percent=round(sample_pct, 2),
                    five_hour_percent=fh_pct,
                    five_hour_resets_at=fh_resets_at,
                    five_hour_window_key=window_key,
                    payload_json="{}",
                    source="build-readme-fixtures",
                )
            captured = as_of  # for the cost snapshot below
        _seed_weekly_cost_snapshot(
            stats_conn,
            week_start_date=wstart.strftime("%Y-%m-%d"),
            week_end_date=wend.strftime("%Y-%m-%d"),
            week_start_at=_iso(wstart),
            week_end_at=_iso(wend),
            cost_usd=cost,
            captured_at_utc=_iso(captured),
        )


def _populate_milestones(
    stats_conn: sqlite3.Connection, *, as_of: dt.datetime
) -> None:
    """Seed percent_milestones for the current week so percent-breakdown
    and the TUI's milestone widget have crossings to display.
    """
    week_start = (as_of - dt.timedelta(days=as_of.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    week_end = week_start + dt.timedelta(days=7)
    wstart_str = week_start.strftime("%Y-%m-%d")
    wend_str = week_end.strftime("%Y-%m-%d")
    wstart_iso = _iso(week_start)
    wend_iso = _iso(week_end)
    # Crossings span Monday 02:00 → Wednesday 06:00, fitting within the
    # current fixture as_of (Thursday 14:00 UTC, see `build()`). Capped at
    # 50% — the current week's weekly_pct is 53.0, which has crossed 50
    # but not yet 60. Emitting 53/59 thresholds was an artifact of the
    # prior 59% current-week target.
    crossings = [
        (10, dt.timedelta(days=0, hours=2),  4.10, 4.10, 6.0),
        (20, dt.timedelta(days=0, hours=8),  8.95, 4.85, 11.0),
        (30, dt.timedelta(days=0, hours=14), 13.40, 4.45, 14.5),
        (40, dt.timedelta(days=0, hours=20), 17.80, 4.40, 16.0),
        (50, dt.timedelta(days=1, hours=6),  22.05, 4.25, 18.5),
    ]
    for pct, delta, cumul, marginal, fh_pct in crossings:
        crossed_at = week_start + delta
        if crossed_at > as_of:
            continue
        _seed_percent_milestone(
            stats_conn,
            week_start_date=wstart_str,
            week_end_date=wend_str,
            week_start_at=wstart_iso,
            week_end_at=wend_iso,
            percent_threshold=pct,
            captured_at_utc=_iso(crossed_at),
            cumulative_cost_usd=cumul,
            marginal_cost_usd=marginal,
            five_hour_percent_at_crossing=fh_pct,
        )


def _populate_session_entries(
    cache_conn: sqlite3.Connection, *, as_of: dt.datetime
) -> None:
    """Seed session_entries + session_files spread across the past 30 days.

    The dashboard's Daily heatmap renders 30 days; previously the builder
    only seeded entries inside `[week_start, as_of]`, leaving the heatmap
    mostly empty. Now we walk DAYS backward from `as_of` for 30 days and
    deterministically place 3-6 entries per day, cycling through (project,
    session, model) combinations so:
      - each of the 4 projects appears ≥5 times (regression-tested)
      - each day has ≥1 entry (heatmap fills)
      - models rotate Sonnet/Opus/Haiku for a realistic mix in the model
        breakdowns shown in cli-five-hour-blocks.svg

    Each entry is anchored to a session JSONL path under the fake home's
    .claude/projects/<project> tree (the file doesn't have to exist on
    disk for cache-only commands; session_files row is enough).
    """
    home = REPO_ROOT / "tests/fixtures/readme/home"

    # Pre-create 3 sessions per project so reads see a realistic
    # session_files set (independent of which sessions the 30-day walk
    # ends up touching).
    session_paths: dict[tuple[str, int], str] = {}
    for proj in PROJECTS:
        cwd = f"{home}/code/{proj}"
        for sess in range(3):
            session_id = f"sess-{proj}-{sess:02d}"
            jsonl_path = f"{home}/.claude/projects/-{proj}/{session_id}.jsonl"
            seed_session_file(
                cache_conn,
                path=jsonl_path,
                session_id=session_id,
                project_path=cwd,
                size_bytes=4096,
                last_byte_offset=4096,
            )
            session_paths[(proj, sess)] = jsonl_path

    line_offset = 0
    # days_ago = 0 is the day containing `as_of`; 29 is ~30 days back.
    for days_ago in range(30):
        day_anchor = as_of - dt.timedelta(days=days_ago)
        # Anchor each day at 09:00 UTC so we can fan out 3-6 entries
        # across 09:00..18:00 and stay safely before `as_of` even on the
        # `days_ago == 0` slice (as_of is 14:00 UTC; entries at 09..13
        # all fit). On past days, every entry slot is in the past anyway.
        day_start = day_anchor.replace(
            hour=9, minute=0, second=0, microsecond=0,
        )
        entries_this_day = 3 + (days_ago % 4)  # cycles 3,4,5,6
        for k in range(entries_this_day):
            proj_idx = (days_ago + k) % len(PROJECTS)
            proj = PROJECTS[proj_idx]
            sess_idx = ((days_ago // 7) + k) % 3
            jsonl_path = session_paths[(proj, sess_idx)]
            model = MODELS[(days_ago + k) % len(MODELS)]
            # Space entries across the working day. Step is 90 min so
            # `entries_this_day=6` lands the last one at 16:30, well
            # before any plausible `as_of`.
            ts = day_start + dt.timedelta(minutes=90 * k)
            if ts > as_of:
                continue
            seed_session_entry(
                cache_conn,
                source_path=jsonl_path,
                line_offset=line_offset,
                timestamp_utc=_iso(ts),
                model=model,
                # Token counts sized so the per-week live cost adds up
                # to ~$25-30 (matches the trend's $/1% × ~53% target).
                # Spread variance so per-row dashboard numbers don't
                # all look identical.
                # Coefficients are 10x the per-row variance you'd expect
                # for ~1-2k LoC operations; sized so summed live cost over
                # the current week (May 4 → as_of Thursday 14:00 UTC)
                # lands at ~$28 — matches the trend's $28.12 row, the
                # forecast's "Used 53.0% $28.x" line, and the dashboard's
                # `current_week.spent_usd`. Pre-scale, the same week
                # summed to ~$2.81, contradicting the trend's $/1% column.
                input_tokens=120_000 + 20_000 * k + 8_000 * proj_idx,
                output_tokens=48_000 + 6_000 * k + 2_000 * (days_ago % 5),
                cache_create=40_000 if k % 3 == 0 else 0,
                cache_read=180_000 + 15_000 * k + 25_000 * (days_ago % 3),
                msg_id=f"msg_{proj}_{sess_idx}_{days_ago}_{k}",
                req_id=f"req_{proj}_{sess_idx}_{days_ago}_{k}",
            )
            line_offset += 1


def _populate_blocks(
    stats_conn: sqlite3.Connection, *, as_of: dt.datetime
) -> tuple[int, float, str]:
    """Seed five_hour_blocks for the current week + 3 prior 5h windows.

    Anchor blocks at 10:00 UTC on each of the past 4 days. Latest block
    (offset 0) stays open (`is_closed=0`); prior blocks are closed.

    Returns ``(window_key, final_pct, resets_at_iso)`` for the OPEN block
    (offset_days=0). Callers (`_populate_weeks`) mirror these onto the
    latest weekly_usage_snapshots row so the snapshot scalars
    (`five_hour_percent`, `five_hour_resets_at`) match the open block —
    `_tui_build_current_week` reads them directly for the React Header
    chip and CurrentWeekPanel display, and `five_hour_window_key` joins
    `_select_current_block_for_envelope` to the same block.

    `seven_day_pct_at_block_start` for the open block is tuned BELOW the
    current-week `used_pct` (53.0) so the panel's delta lands at a
    positive few percent. Closed blocks keep the prior linear scheme.
    """
    base_today = as_of.replace(hour=10, minute=0, second=0, microsecond=0)
    open_block_window_key: Optional[int] = None
    open_block_final_pct: Optional[float] = None
    open_block_resets_at_iso: Optional[str] = None
    for offset_days in (3, 2, 1, 0):
        block_start = base_today - dt.timedelta(days=offset_days)
        if block_start > as_of:
            continue
        block_end = block_start + dt.timedelta(hours=5)
        last_observed = min(block_end - dt.timedelta(minutes=20), as_of)
        first_observed = block_start + dt.timedelta(minutes=2)
        is_closed = 0 if offset_days == 0 else 1
        five_h_pct = 22.0 + 14.0 * (3 - offset_days)
        # Calibrated to roughly match the live per-block recompute sum from
        # session_entries (which `cmd_five_hour_blocks --breakdown=model`
        # surfaces in the second column). Active block is partial — only
        # the entries inside [block_start, as_of] count, vs 3 entries
        # spanning the full 5h window for closed blocks. If the breakdown
        # rows visibly out-sum the parent after a `CLAUDE_MODEL_PRICING`
        # change, retune these constants. (Production cctally recomputes
        # the parent on every `record-usage` tick from the same
        # session_entries; the marketing fixture skips that path because
        # there's no live OAuth flow, so we hand-seed instead.)
        cost = 2.70 if offset_days == 0 else 4.50 + 0.10 * (3 - offset_days)
        if offset_days == 0:
            # Open block: anchor BELOW current 53.0 so `used_pct -
            # seven_day_pct_at_block_start` is a small positive delta
            # (+3.0pp). Anthropic's OAuth API only returns INTEGER
            # weekly percentages — `seven_day_pct_at_block_start` is
            # populated from `weekly_usage_snapshots.weekly_percent` at
            # block-start time, so any non-integer value here is
            # narratively impossible (would render "+2.5pp this block"
            # and break the screenshot's credibility).
            seven_day_start = 50.0
            seven_day_end = 53.0
        else:
            seven_day_start = 40.0 + 5.0 * (3 - offset_days)
            seven_day_end = seven_day_start + 2.0
        # Canonical 5h window key: epoch seconds floored to 10 minutes.
        # Mirrors _canonical_5h_window_key (bin/cctally) — fixtures should
        # use the same shape so harnesses join cleanly.
        window_key = int(block_start.timestamp() // 600 * 600)
        if offset_days == 0:
            open_block_window_key = window_key
            open_block_final_pct = five_h_pct
            open_block_resets_at_iso = _iso(block_end)
        _seed_five_hour_block(
            stats_conn,
            five_hour_window_key=window_key,
            five_hour_resets_at=_iso(block_end),
            block_start_at=_iso(block_start),
            first_observed_at_utc=_iso(first_observed),
            last_observed_at_utc=_iso(last_observed),
            final_five_hour_percent=five_h_pct,
            seven_day_pct_at_block_start=seven_day_start,
            seven_day_pct_at_block_end=seven_day_end,
            total_input_tokens=42_000 + 8_000 * (3 - offset_days),
            total_output_tokens=15_000 + 3_000 * (3 - offset_days),
            total_cache_create_tokens=1_200,
            total_cache_read_tokens=22_000,
            total_cost_usd=cost,
            is_closed=is_closed,
            created_at_utc=_iso(first_observed),
            last_updated_at_utc=_iso(last_observed),
        )
    if (
        open_block_window_key is None
        or open_block_final_pct is None
        or open_block_resets_at_iso is None
    ):
        # Should not happen given the (3, 2, 1, 0) sweep + fixed Thursday
        # 14:00 anchor, but fail loud rather than silently emit a fixture
        # with NULL fields on the latest snapshot.
        raise RuntimeError(
            "open block (offset_days=0) was not seeded; current 5h block "
            "envelope binding will fail"
        )
    return (
        open_block_window_key,
        open_block_final_pct,
        open_block_resets_at_iso,
    )


MARKETING_DISPLAY_TZ = "America/Los_Angeles"

# Deterministic 32-char hex placeholder for `collector.token`. cctally's
# real `_default_config_data()` uses `secrets.token_hex(16)`, but the
# marketing fixture must round-trip identically across builds (the
# screenshot pipeline is byte-stable; the determinism harness in
# `tests/test_build_readme_fixtures.py::test_deterministic_for_fixed_as_of`
# verifies SQLite dumps but config.json should also stay byte-identical
# across runs to make harness diffs easy to read). The value is a fixed
# constant — never used to authenticate against a real collector since
# `bin/cctally cache-sync` against this fixture does not POST.
_MARKETING_FIXTURE_COLLECTOR_TOKEN = "0123456789abcdef0123456789abcdef"


def _write_marketing_config(app_dir: Path) -> None:
    """Write `<app_dir>/config.json` pinning `display.tz` to LA.

    Mirrors the structure of cctally's `_default_config_data()` plus the
    `display.tz` block users would set via `cctally config set
    display.tz America/Los_Angeles`. A fresh fake-home has no prior
    config; we lay one down so the dashboard + CLI render dates in LA
    time independent of host TZ.
    """
    app_dir.mkdir(parents=True, exist_ok=True)
    config_path = app_dir / "config.json"
    data = {
        "collector": {
            "host": "127.0.0.1",
            "port": 17321,
            "token": _MARKETING_FIXTURE_COLLECTOR_TOKEN,
            "week_start": "monday",
        },
        "display": {
            "tz": MARKETING_DISPLAY_TZ,
        },
    }
    config_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_tui_snapshot(path: Path, *, as_of: dt.datetime) -> None:
    """Write a Python module exporting SNAPSHOT (DataSnapshot) for the TUI shot.

    The exact DataSnapshot type lives in bin/cctally; the snapshot module
    is loaded by `tui --render-once --snapshot-module PATH` per the dev
    contract. We keep the snapshot body minimal and let the loader's
    RUNTIME_OVERRIDES path tweak modal state if a future variant needs it.
    """
    body = f'''"""Auto-generated by bin/build-readme-fixtures.py — do not hand-edit.

Loaded by `cctally tui --render-once --snapshot-module …` per the dev path.
Exports SNAPSHOT (required) and may export RUNTIME_OVERRIDES (optional).
"""
from __future__ import annotations

# DataSnapshot is defined inside bin/cctally; we lazy-import to keep this
# module loadable by tests that don't have the binary on PYTHONPATH.
import importlib.util as _ilu
import importlib.machinery as _ilm
import sys as _sys
from pathlib import Path as _Path

# Walk up from this snapshot file to find bin/cctally. The earlier
# `parents[3]` form only worked when the snapshot lived at the default
# committed path (tests/fixtures/readme/tui_snapshot.py). For a custom
# `--tui-snapshot` path, parents[3] resolves outside the repo and the
# import fails before the tui renderer runs.
_HERE = _Path(__file__).resolve().parent
_BIN = None
for _p in [_HERE, *_HERE.parents]:
    _candidate = _p / "bin" / "cctally"
    if _candidate.is_file():
        _BIN = _candidate
        break
if _BIN is None:
    raise RuntimeError(
        "could not locate bin/cctally walking up from "
        f"{{_HERE}}; place the snapshot inside the cctally repo"
    )
_spec = _ilu.spec_from_loader(
    "_cctally_for_tui_snapshot",
    _ilm.SourceFileLoader("_cctally_for_tui_snapshot", str(_BIN)),
)
_mod = _ilu.module_from_spec(_spec)
_sys.modules.setdefault("_cctally_for_tui_snapshot", _mod)
_spec.loader.exec_module(_mod)

DataSnapshot = _mod.DataSnapshot

SNAPSHOT = DataSnapshot.synthesize_for_marketing(as_of_iso="{_iso(as_of)}")

RUNTIME_OVERRIDES = {{}}
'''
    path.write_text(body)


def build(
    *,
    out_dir: Path,
    as_of_str: str,
    tui_snapshot_path: Optional[Path] = None,
) -> None:
    """Top-level builder. Idempotent — overwrites existing DBs.

    `as_of_str` is interpreted as the calendar week to render; the actual
    "now" used for fixture generation is THURSDAY 14:00 UTC of the week
    containing that date. This makes the forecast projection land in the
    95-105% range (used_pct=53 / elapsed_fraction≈0.512 → ~103.5%) rather
    than the prior Tuesday-anchored ~261% projection that clipped the
    WARN modal's projection pill at the right edge. Callers pinning a
    specific date (e.g. tests using `2026-05-05` Tuesday) silently land
    on the same week's Thursday — `2026-05-07` in that example.
    """
    parsed = dt.datetime.strptime(as_of_str, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc,
    )
    # Shift to the THURSDAY of the containing week.
    # weekday(): Mon=0..Sun=6; Thursday=3.
    days_to_thursday = 3 - parsed.weekday()
    as_of = (parsed + dt.timedelta(days=days_to_thursday)).replace(
        hour=14, minute=0, second=0, microsecond=0,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    stats_path = out_dir / "stats.db"
    cache_path = out_dir / "cache.db"
    if stats_path.exists():
        stats_path.unlink()
    if cache_path.exists():
        cache_path.unlink()

    create_stats_db(stats_path)
    create_cache_db(cache_path)

    with sqlite3.connect(stats_path) as stats_conn:
        # WAL is set by create_stats_db() but a fresh connect()
        # re-asserts the pragma cheaply and matches production posture.
        stats_conn.execute("PRAGMA journal_mode=WAL")
        stats_conn.execute("PRAGMA foreign_keys = OFF")
        # Seed blocks FIRST so we know the open block's canonical
        # five_hour_window_key, then thread it into _populate_weeks so
        # the latest weekly_usage_snapshots row carries the same key.
        # Without that mirror, `_select_current_block_for_envelope`
        # returns None and the dashboard's current-week panel renders
        # the legacy single-big-number layout.
        (
            open_block_window_key,
            open_block_final_pct,
            open_block_resets_at_iso,
        ) = _populate_blocks(stats_conn, as_of=as_of)
        _populate_weeks(
            stats_conn,
            as_of=as_of,
            open_block_window_key=open_block_window_key,
            open_block_final_pct=open_block_final_pct,
            open_block_resets_at_iso=open_block_resets_at_iso,
        )
        _populate_milestones(stats_conn, as_of=as_of)
        stats_conn.commit()

    with sqlite3.connect(cache_path) as cache_conn:
        cache_conn.execute("PRAGMA journal_mode=WAL")
        _populate_session_entries(cache_conn, as_of=as_of)
        cache_conn.commit()

    # config.json: pin display.tz so dashboard + CLI render dates in LA
    # time regardless of host TZ. Without this, screenshots inherit the
    # maintainer's local zone (IDT on the host that built the prior
    # round) and dates drift visibly between machines. Writing directly
    # is safe — nothing else holds the file in this build path.
    # `out_dir` is `<home>/.local/share/cctally`; production reads
    # `config.json` from the same directory.
    _write_marketing_config(out_dir)

    snap_path = tui_snapshot_path or DEFAULT_TUI_SNAPSHOT
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    _write_tui_snapshot(snap_path, as_of=as_of)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--as-of",
        default=None,
        help="YYYY-MM-DD anchor for relative dates (default: today UTC)",
    )
    p.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR),
        help="Output directory for stats.db / cache.db (default: %(default)s)",
    )
    p.add_argument(
        "--tui-snapshot",
        default=str(DEFAULT_TUI_SNAPSHOT),
        help="Path for the TUI snapshot module (default: %(default)s)",
    )
    args = p.parse_args()
    as_of = args.as_of or DEFAULT_AS_OF_FN()
    build(
        out_dir=Path(args.out),
        as_of_str=as_of,
        tui_snapshot_path=Path(args.tui_snapshot),
    )
    print(f"wrote fixture: out={args.out} as_of={as_of}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
