#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally dashboard`.

Writes one pair of (stats.db, cache.db) per scenario under
``tests/fixtures/dashboard/<scenario>/.local/share/cctally/``.
All schema/seeding goes through ``bin/_fixture_builders.py`` — do not
duplicate schema here. Idempotent: each builder overwrites existing DBs.

Five scenarios:
  * ``ok``         — current week at ~40% with 8 weeks of history; forecast
                     verdict ``"ok"`` (renders as GOOD in the browser).
  * ``warn``       — current week at ~67% with a heavy recent-24h burn that
                     drags ``final_percent_high`` above 100; forecast
                     verdict ``"cap"`` (renders as WARN).
  * ``over``       — current week already past 100%; forecast verdict
                     ``"capped"`` (renders as OVER).
  * ``reset-week`` — mid-week goodwill reset. Pre-reset usage climbs to 60%
                     against the original boundary; a reset shifts
                     ``week_end_at`` forward and drops usage back to 0;
                     post-reset milestones 1..5 are seeded. Regresses on
                     the Current Week modal's per-percent list: the
                     envelope MUST carry 5 milestones, not the empty-state
                     (bug where ``TuiCurrentWeek.week_start_at`` was
                     misused as the ``week_start_date`` lookup key after
                     ``_apply_midweek_reset_override`` shifted it forward).
  * ``no-data``    — empty schemas; every panel serializes as ``None``.

Each scenario writes ``input.env`` containing a single line
``AS_OF=<iso-utc>`` consumed by the dashboard harness via
``CCTALLY_AS_OF``.

Run: ``bin/build-dashboard-fixtures.py`` (idempotent; overwrites).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    seed_session_entry,
    seed_session_file,
    seed_week_reset_event,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/dashboard"


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scenario_dirs(name: str) -> tuple[Path, Path]:
    """Return (scenario_dir, app_dir). Mirrors forecast / session layouts:
    the dashboard harness will drive the CLI with ``HOME=<scenario_dir>``
    and the production code hardcodes
    ``APP_DIR = Path.home() / ".local" / "share" / "cctally"``.
    """
    scenario_dir = FIXTURES_DIR / name
    app_dir = scenario_dir / ".local" / "share" / "cctally"
    app_dir.mkdir(parents=True, exist_ok=True)
    return scenario_dir, app_dir


def _insert_usage_snapshot(
    stats_conn: sqlite3.Connection,
    *,
    captured_at: dt.datetime,
    week_start: dt.datetime,
    week_end: dt.datetime,
    pct: float,
) -> None:
    """Write one weekly_usage_snapshots row carrying both ISO-timestamp
    and date-only boundary columns so the production selector picks it
    up via either match path."""
    stats_conn.execute(
        "INSERT INTO weekly_usage_snapshots(captured_at_utc, week_start_date, "
        "week_end_date, week_start_at, week_end_at, weekly_percent, source, "
        "payload_json) VALUES (?,?,?,?,?,?,?,?)",
        (
            _iso(captured_at),
            week_start.date().isoformat(),
            week_end.date().isoformat(),
            _iso(week_start),
            _iso(week_end),
            pct,
            "fixture",
            json.dumps({"fixture": True}),
        ),
    )


def _insert_cost_snapshot(
    stats_conn: sqlite3.Connection,
    *,
    captured_at: dt.datetime,
    week_start: dt.datetime,
    week_end: dt.datetime,
    cost_usd: float,
) -> None:
    """Write one weekly_cost_snapshots row. ``weekly`` ignores this for
    cost (recomputes from session_entries) but ``report`` joins on it,
    and the historical-weeks trend relies on ``get_latest_cost_for_week``
    finding a cost row to compute $/1%."""
    stats_conn.execute(
        "INSERT INTO weekly_cost_snapshots(captured_at_utc, week_start_date, "
        "week_end_date, week_start_at, week_end_at, cost_usd, source, mode) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            _iso(captured_at),
            week_start.date().isoformat(),
            week_end.date().isoformat(),
            _iso(week_start),
            _iso(week_end),
            cost_usd,
            "fixture",
            "auto",
        ),
    )


def _insert_milestones(
    stats_conn: sqlite3.Connection,
    *,
    week_start: dt.datetime,
    week_end: dt.datetime,
    final_pct: int,
    dollars_per_percent: float,
    first_crossed_at: dt.datetime,
    per_percent_spacing: dt.timedelta,
    reset_event_id: int = 0,
) -> None:
    """Seed `final_pct` percent_milestones rows for the given week.
    percent_threshold ranges from 1..final_pct; each crossing advances
    wall-clock by `per_percent_spacing` starting from `first_crossed_at`.
    cumulative_cost_usd = dollars_per_percent * percent (rounded to 4).
    marginal_cost_usd = dollars_per_percent (same for all rows).
    five_hour_percent_at_crossing left None (fixtures have no 5-hr data).
    usage_snapshot_id / cost_snapshot_id set to 0 (schema is NOT NULL but
    the reader path does not join on them).
    reset_event_id: 0 (sentinel) for legacy / uncredited weeks, or a
    week_reset_events.id for post-credit segment milestones (Task 5).
    """
    for p in range(1, final_pct + 1):
        crossed = first_crossed_at + per_percent_spacing * (p - 1)
        cumulative = round(dollars_per_percent * p, 4)
        marginal = round(dollars_per_percent, 4)
        stats_conn.execute(
            """INSERT INTO percent_milestones
               (captured_at_utc, week_start_date, week_end_date,
                week_start_at, week_end_at, percent_threshold,
                cumulative_cost_usd, marginal_cost_usd,
                usage_snapshot_id, cost_snapshot_id,
                five_hour_percent_at_crossing, reset_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _iso(crossed),
                week_start.date().isoformat(),
                week_end.date().isoformat(),
                _iso(week_start),
                _iso(week_end),
                p,
                cumulative,
                marginal,
                0,
                0,
                None,
                reset_event_id,
            ),
        )


def _seed_session(
    cache_conn: sqlite3.Connection,
    *,
    session_id: str,
    project_path: str,
    model: str,
    entries: list[tuple[dt.datetime, int, int, int, int]],
    line_offset_start: int = 0,
) -> int:
    """Seed one session (session_files row + N session_entries rows on a
    distinct source_path). Returns the next available line_offset.

    ``entries``: list of (timestamp, input_tokens, output_tokens,
    cache_create_tokens, cache_read_tokens).
    """
    file_path = f"/fake/jsonl/{session_id}.jsonl"
    seed_session_file(
        cache_conn,
        path=file_path,
        session_id=session_id,
        project_path=project_path,
    )
    next_off = line_offset_start
    for ts, inp, out, cc, cr in entries:
        seed_session_entry(
            cache_conn,
            source_path=file_path,
            line_offset=next_off,
            timestamp_utc=_iso(ts),
            model=model,
            input_tokens=inp,
            output_tokens=out,
            cache_create=cc,
            cache_read=cr,
        )
        next_off += 1
    return next_off


def _seed_session_multi_model(
    cache_conn: sqlite3.Connection,
    *,
    session_id: str,
    project_path: str,
    entries: list[tuple[dt.datetime, str, int, int, int, int]],
    line_offset_start: int = 0,
) -> int:
    """Seed one session with per-entry model specification. Used to
    exercise TuiSessionDetail.models primary/secondary roles from a
    single session_id.

    ``entries``: list of (timestamp, model, input_tokens, output_tokens,
    cache_create_tokens, cache_read_tokens). The first distinct model
    encountered by the aggregator (chronological order) becomes
    ``primary``; any others are ``secondary``.
    """
    file_path = f"/fake/jsonl/{session_id}.jsonl"
    seed_session_file(
        cache_conn,
        path=file_path,
        session_id=session_id,
        project_path=project_path,
    )
    next_off = line_offset_start
    for ts, model, inp, out, cc, cr in entries:
        seed_session_entry(
            cache_conn,
            source_path=file_path,
            line_offset=next_off,
            timestamp_utc=_iso(ts),
            model=model,
            input_tokens=inp,
            output_tokens=out,
            cache_create=cc,
            cache_read=cr,
        )
        next_off += 1
    return next_off


# Deterministic session ids per non-empty scenario. Exposed both to the
# harness via input.env (``FIXED_SESSION_ID=<id>`` line) and used here
# to seed one multi-model session per scenario that the
# ``/api/session/:id`` harness can GET byte-stably.
FIXED_SESSION_IDS: dict[str, str] = {
    "ok":         "fixture-ok-session-0000000000000000",
    "warn":       "fixture-warn-session-0000000000000000",
    "over":       "fixture-over-session-0000000000000000",
    "reset-week": "fixture-reset-session-0000000000000000",
}


# --- Scenario helpers --------------------------------------------------

# Common subscription week shape: Monday 14:00Z → next Monday 14:00Z.
# Using the same anchor as the forecast fixtures keeps human inspection
# of both fixture suites mentally consistent.


def _seed_prior_weeks(
    stats_conn: sqlite3.Connection,
    cache_conn: sqlite3.Connection,
    *,
    current_week_start: dt.datetime,
    count: int,
    final_pct: float,
    cost_usd: float,
    model: str,
    projects: list[str],
    line_offset_start: int = 0,
) -> int:
    """Seed ``count`` complete prior weeks, each ending at ``final_pct`` and
    carrying roughly ``cost_usd`` of cost on a single session per week.

    Also writes a ``weekly_cost_snapshots`` row per week so the
    Trend/report join has a $/1% value. Cost is recomputed from
    session_entries by the live code, so the snapshot value here is
    mostly informational — but present rather than absent for parity
    with a real install.

    Returns the next available line_offset.
    """
    next_off = line_offset_start
    for k in range(count, 0, -1):
        ws = current_week_start - dt.timedelta(days=7 * k)
        we = ws + dt.timedelta(days=7)
        # Single final-week usage snapshot (168h in) so the week closes at
        # `final_pct`. Two anchoring snapshots at 24h and 96h to make the
        # week look plausibly sampled by the userscript.
        _insert_usage_snapshot(
            stats_conn, captured_at=ws + dt.timedelta(hours=24),
            week_start=ws, week_end=we, pct=final_pct * 0.2,
        )
        _insert_usage_snapshot(
            stats_conn, captured_at=ws + dt.timedelta(hours=96),
            week_start=ws, week_end=we, pct=final_pct * 0.6,
        )
        _insert_usage_snapshot(
            stats_conn, captured_at=ws + dt.timedelta(hours=168),
            week_start=ws, week_end=we, pct=final_pct,
        )
        _insert_cost_snapshot(
            stats_conn, captured_at=ws + dt.timedelta(hours=168),
            week_start=ws, week_end=we, cost_usd=cost_usd,
        )
        # One session per prior week — deterministic UUID-ish id.
        sid = f"prior-wk{k:02d}-00000000-0000-0000-0000-0000"
        proj = projects[k % len(projects)]
        # Token counts sized to approximate `cost_usd` at sonnet-4-6 pricing
        # ($3/M input + $15/M output). (Cost is recomputed; precision here
        # isn't load-bearing.)
        input_t = int(cost_usd * 200_000)  # $3/M → cost_usd/3 M tokens roughly
        output_t = int(cost_usd * 40_000)
        next_off = _seed_session(
            cache_conn,
            session_id=sid,
            project_path=proj,
            model=model,
            entries=[
                (ws + dt.timedelta(hours=40), input_t, output_t, 0, 0),
            ],
            line_offset_start=next_off,
        )
    return next_off


def build_ok(as_of: dt.datetime) -> None:
    """Steady-state healthy week. 8 prior weeks of history, current week at
    ~40% with gentle linear pace. Forecast high stays < 100 → verdict
    ``"ok"``."""
    scenario_dir, app_dir = _scenario_dirs("ok")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)

    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end = week_start + dt.timedelta(days=7)
    # AS_OF = 2026-04-16T14:00Z → 72h into the week (~day 4 of 7).

    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        # 8 weeks of history at ~42% per week (slight variance for spark
        # chart visual interest, all below-cap).
        pct_series = [38.0, 44.0, 41.0, 46.0, 39.0, 43.0, 42.0, 45.0]
        cost_series = [16.5, 18.2, 17.0, 19.4, 16.1, 18.0, 17.6, 19.1]
        next_off = 0
        for k in range(8, 0, -1):
            ws = week_start - dt.timedelta(days=7 * k)
            we = ws + dt.timedelta(days=7)
            pct_final = pct_series[8 - k]
            cost_final = cost_series[8 - k]
            _insert_usage_snapshot(
                stats_conn, captured_at=ws + dt.timedelta(hours=24),
                week_start=ws, week_end=we, pct=pct_final * 0.2,
            )
            _insert_usage_snapshot(
                stats_conn, captured_at=ws + dt.timedelta(hours=96),
                week_start=ws, week_end=we, pct=pct_final * 0.6,
            )
            _insert_usage_snapshot(
                stats_conn, captured_at=ws + dt.timedelta(hours=168),
                week_start=ws, week_end=we, pct=pct_final,
            )
            _insert_cost_snapshot(
                stats_conn, captured_at=ws + dt.timedelta(hours=168),
                week_start=ws, week_end=we, cost_usd=cost_final,
            )
            sid = f"ok-hist-wk{k:02d}-0000-0000-0000-0000"
            input_t = int(cost_final * 200_000)
            output_t = int(cost_final * 40_000)
            next_off = _seed_session(
                cache_conn,
                session_id=sid,
                project_path=f"/fake/repos/project-{(k % 3) + 1}",
                model="claude-sonnet-4-6",
                entries=[
                    (ws + dt.timedelta(hours=40), input_t, output_t, 0, 0),
                ],
                line_offset_start=next_off,
            )

        # Current week: 7 linearly-ramped snapshots ending at 40% at 72h.
        # Slope ~0.56 pct/h → projection ~94% at 168h (under-100 → ok).
        samples = [
            (6, 3.0), (18, 10.0), (30, 17.0), (42, 24.0),
            (54, 30.0), (66, 36.0), (72, 40.0),
        ]
        for hrs_in, pct in samples:
            _insert_usage_snapshot(
                stats_conn,
                captured_at=week_start + dt.timedelta(hours=hrs_in),
                week_start=week_start, week_end=week_end, pct=pct,
            )

        # ~25 distinct sessions this week so the Sessions panel is populous.
        # Distribute across 72h of wall-time so durations render sensibly.
        for i in range(25):
            sid = f"ok-cur-s{i:03d}-0000-0000-0000-0000-000000000000"
            start_h = 2 + i * 2.5  # 2h, 4.5h, 7h, ... 62h
            if start_h > 70:
                break
            proj = f"/fake/repos/project-{(i % 4) + 1}"
            # Two-entry session: start and end a few minutes apart.
            t0 = week_start + dt.timedelta(hours=start_h)
            t1 = t0 + dt.timedelta(minutes=30)
            next_off = _seed_session(
                cache_conn,
                session_id=sid,
                project_path=proj,
                model=("claude-sonnet-4-6" if i % 2 == 0 else "claude-opus-4-7"),
                entries=[
                    (t0, 120_000, 20_000, 0, 0),
                    (t1,  80_000, 12_000, 0, 0),
                ],
                line_offset_start=next_off,
            )

        # Deterministic "known id" session for GET /api/session/:id
        # goldens (Task 3.2). Multi-model to exercise primary/secondary
        # role attribution. Distinct project so the golden row is
        # recognizable.
        fixed_t0 = week_start + dt.timedelta(hours=68, minutes=0)
        fixed_t1 = fixed_t0 + dt.timedelta(minutes=15)
        fixed_t2 = fixed_t0 + dt.timedelta(minutes=40)
        next_off = _seed_session_multi_model(
            cache_conn,
            session_id=FIXED_SESSION_IDS["ok"],
            project_path="/fake/repos/fixture-demo",
            entries=[
                (fixed_t0, "claude-sonnet-4-6", 150_000, 22_000, 0, 0),
                (fixed_t1, "claude-sonnet-4-6",  90_000, 14_000, 0, 0),
                (fixed_t2, "claude-opus-4-7",   120_000, 18_000, 0, 0),
            ],
            line_offset_start=next_off,
        )

        # Per-percent milestones for the current week. 40 rows spanning
        # hours 6–72 (matches the 40% snapshot at 72h). dollars_per_percent
        # must equal the envelope-reported cw.dollar_per_pct so the
        # Phase 5 modal's per-percent sum lines up with cw.spent_usd within
        # rounding (code-quality review I1 on commit c7e4991).
        _insert_milestones(
            stats_conn,
            week_start=week_start, week_end=week_end,
            final_pct=40,
            dollars_per_percent=0.891,
            first_crossed_at=week_start + dt.timedelta(hours=6),
            per_percent_spacing=dt.timedelta(minutes=99),  # (66h span)/(40 crossings) ≈ 99min
        )
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()

    (scenario_dir / "input.env").write_text(
        f"AS_OF={_iso(as_of)}\n"
        f"FIXED_SESSION_ID={FIXED_SESSION_IDS['ok']}\n"
    )


def build_warn(as_of: dt.datetime) -> None:
    """Heavy recent burn. Current week at ~67% with steep 24h acceleration
    that drags ``final_percent_high`` above 100 while the week-average
    slope stays below → verdict ``"cap"``."""
    scenario_dir, app_dir = _scenario_dirs("warn")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)

    # AS_OF = 2026-04-18T20:00Z → 126h into the week (~day 5.25 of 7).
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end = week_start + dt.timedelta(days=7)

    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        # 6 prior weeks of middling usage (45–55%) so $/1% trend has data.
        next_off = _seed_prior_weeks(
            stats_conn, cache_conn,
            current_week_start=week_start,
            count=6,
            final_pct=50.0,
            cost_usd=22.0,
            model="claude-sonnet-4-6",
            projects=["/fake/repos/alpha", "/fake/repos/beta", "/fake/repos/gamma"],
        )

        # Current-week snapshots: steady ~0.4 pct/h for the first 100h
        # (40% at 100h) then sharp acceleration — 27pp in the last 26h
        # (~1.04 pct/h). With 42h remaining:
        #   week-avg rate ≈ 67 / 126 ≈ 0.532 pct/h  → low ≈ 67 + 0.532*42 ≈ 89.3
        #   recent-24h rate ≈ 26pp/24h ≈ 1.083 pct/h → high ≈ 67 + 1.083*42 ≈ 112.5
        # high >= 100 → projected_cap=true → verdict "cap".
        samples = [
            (6, 2.0), (18, 7.0), (30, 12.0), (42, 17.0),
            (54, 22.0), (66, 27.0), (78, 32.0), (90, 37.0),
            (100, 40.0),
            # 24h burn accelerates
            (108, 48.0), (114, 54.0), (120, 60.0), (126, 67.0),
        ]
        for hrs_in, pct in samples:
            _insert_usage_snapshot(
                stats_conn,
                captured_at=week_start + dt.timedelta(hours=hrs_in),
                week_start=week_start, week_end=week_end, pct=pct,
            )

        # ~15 sessions distributed over the week, with a burst in the last 24h.
        for i in range(15):
            sid = f"warn-cur-s{i:03d}-0000-0000-0000-0000-000000000000"
            # First 10 spread over 100h, remaining 5 packed into last 26h.
            start_h = (i * 10.0) if i < 10 else (102.0 + (i - 10) * 4.5)
            if start_h > 125:
                break
            proj = f"/fake/repos/{['alpha', 'beta', 'gamma'][i % 3]}"
            t0 = week_start + dt.timedelta(hours=start_h)
            t1 = t0 + dt.timedelta(minutes=35)
            next_off = _seed_session(
                cache_conn,
                session_id=sid,
                project_path=proj,
                model=("claude-opus-4-7" if i >= 10 else "claude-sonnet-4-6"),
                entries=[
                    (t0, 180_000, 25_000, 0, 0),
                    (t1, 120_000, 18_000, 0, 0),
                ],
                line_offset_start=next_off,
            )

        # Deterministic "known id" session for GET /api/session/:id
        # goldens (Task 3.2). Multi-model to exercise primary/secondary
        # role attribution; starts primary on opus (pre-burn baseline),
        # switches to sonnet. Placed in the last-24h burst window.
        fixed_t0 = week_start + dt.timedelta(hours=118, minutes=0)
        fixed_t1 = fixed_t0 + dt.timedelta(minutes=20)
        fixed_t2 = fixed_t0 + dt.timedelta(minutes=50)
        next_off = _seed_session_multi_model(
            cache_conn,
            session_id=FIXED_SESSION_IDS["warn"],
            project_path="/fake/repos/fixture-demo",
            entries=[
                (fixed_t0, "claude-opus-4-7",   210_000, 30_000, 0, 0),
                (fixed_t1, "claude-opus-4-7",   140_000, 22_000, 0, 0),
                (fixed_t2, "claude-sonnet-4-6", 100_000, 15_000, 0, 0),
            ],
            line_offset_start=next_off,
        )

        # 67 milestones across 126h (~113min per percent on average).
        # dollars_per_percent = cw.spent_usd / final_pct so per-percent sum
        # matches cw.spent_usd within rounding.
        _insert_milestones(
            stats_conn,
            week_start=week_start, week_end=week_end,
            final_pct=67,
            dollars_per_percent=0.4228,
            first_crossed_at=week_start + dt.timedelta(hours=6),
            per_percent_spacing=dt.timedelta(minutes=107),
        )
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()

    (scenario_dir / "input.env").write_text(
        f"AS_OF={_iso(as_of)}\n"
        f"FIXED_SESSION_ID={FIXED_SESSION_IDS['warn']}\n"
    )


def build_over(as_of: dt.datetime) -> None:
    """Already over the cap. Latest snapshot > 100% → ``already_capped``
    → verdict ``"capped"``."""
    scenario_dir, app_dir = _scenario_dirs("over")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)

    # AS_OF = 2026-04-19T10:00Z → 140h into the week (~day 5.8 of 7).
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end = week_start + dt.timedelta(days=7)

    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        # 6 prior weeks, higher baseline so the trend shows gradual approach.
        next_off = _seed_prior_weeks(
            stats_conn, cache_conn,
            current_week_start=week_start,
            count=6,
            final_pct=78.0,
            cost_usd=34.0,
            model="claude-opus-4-7",
            projects=["/fake/repos/heavy", "/fake/repos/ship-it"],
        )

        # Current-week ramp crossing 100 early and ending at 105 at 140h.
        samples = [
            (6, 5.0), (18, 14.0), (30, 23.0), (42, 32.0),
            (54, 43.0), (66, 54.0), (78, 66.0), (90, 78.0),
            (102, 89.0), (114, 98.0), (126, 103.0), (140, 105.0),
        ]
        for hrs_in, pct in samples:
            _insert_usage_snapshot(
                stats_conn,
                captured_at=week_start + dt.timedelta(hours=hrs_in),
                week_start=week_start, week_end=week_end, pct=pct,
            )

        # ~12 sessions, weighted toward the first half (matches the burn pattern).
        for i in range(12):
            sid = f"over-cur-s{i:03d}-0000-0000-0000-0000-000000000000"
            start_h = 3 + i * 11.0
            if start_h > 135:
                break
            proj = f"/fake/repos/{['heavy', 'ship-it'][i % 2]}"
            t0 = week_start + dt.timedelta(hours=start_h)
            t1 = t0 + dt.timedelta(minutes=40)
            next_off = _seed_session(
                cache_conn,
                session_id=sid,
                project_path=proj,
                model="claude-opus-4-7",
                entries=[
                    (t0, 260_000, 32_000, 0, 0),
                    (t1, 180_000, 24_000, 0, 0),
                ],
                line_offset_start=next_off,
            )

        # Deterministic "known id" session for GET /api/session/:id
        # goldens (Task 3.2). Multi-model to exercise primary/secondary
        # role attribution. Placed near the end of the over-week so the
        # session shows up prominently in the panel ordering.
        fixed_t0 = week_start + dt.timedelta(hours=132, minutes=0)
        fixed_t1 = fixed_t0 + dt.timedelta(minutes=25)
        fixed_t2 = fixed_t0 + dt.timedelta(minutes=55)
        next_off = _seed_session_multi_model(
            cache_conn,
            session_id=FIXED_SESSION_IDS["over"],
            project_path="/fake/repos/fixture-demo",
            entries=[
                (fixed_t0, "claude-opus-4-7",   280_000, 36_000, 0, 0),
                (fixed_t1, "claude-sonnet-4-6", 120_000, 18_000, 0, 0),
                (fixed_t2, "claude-opus-4-7",   160_000, 22_000, 0, 0),
            ],
            line_offset_start=next_off,
        )

        # 100 milestones — the cap is 100%, any crossing beyond that is not
        # recorded in production. Spans the full 126h when cap was hit.
        # dollars_per_percent matches envelope-reported cw.dollar_per_pct
        # (spent_usd / used_pct), not 1/100 of spent_usd — used_pct is 105
        # but milestones cap at 100 crossings so rate is per-percent-earned
        # not per-percent-of-final.
        #
        # Expected gap in the golden: final cumulative ≈ $44.57 vs.
        # cw.spent_usd ≈ $46.80 — the $2.23 delta is the 5pp of spend
        # between 100% and 105%, which cannot be represented as an extra
        # milestone row. Readers of the ``over`` golden should expect the
        # Phase 5 modal's per-percent sum to fall $2.23 short of the
        # card-level spent figure for this scenario.
        _insert_milestones(
            stats_conn,
            week_start=week_start, week_end=week_end,
            final_pct=100,
            dollars_per_percent=0.4457,
            first_crossed_at=week_start + dt.timedelta(hours=6),
            per_percent_spacing=dt.timedelta(minutes=72),
        )
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()

    (scenario_dir / "input.env").write_text(
        f"AS_OF={_iso(as_of)}\n"
        f"FIXED_SESSION_ID={FIXED_SESSION_IDS['over']}\n"
    )


def build_reset_week(as_of: dt.datetime) -> None:
    """Mid-week goodwill reset. Regresses on the Current Week modal's
    per-percent list — after the reset override shifts
    ``TuiCurrentWeek.week_start_at`` forward, the modal must still resolve
    the ORIGINAL ``week_start_date`` when looking up milestones.

    Shape:
      * Subscription week: week_start='2026-04-13T14Z', week_start_date='2026-04-13'.
      * Pre-reset snapshots (3 rows) with week_end_at='2026-04-17T14Z',
        ramping to weekly_percent=60.
      * Reset happens at 2026-04-17T13Z (1h before the pre-reset boundary).
      * Post-reset snapshots with week_end_at='2026-04-20T14Z' and
        weekly_percent starting at 0, ramping to 5 by AS_OF.
      * Post-reset per-percent milestones 1..5, captured between the
        reset instant and AS_OF, all keyed with week_start_date='2026-04-13'
        (the status line keeps reporting the same start after a reset).
      * _backfill_week_reset_events (invoked by open_db) synthesizes the
        reset row from the snapshot pattern: boundary shift +
        weekly_percent drop 60→0 (>= 25pp threshold) + capture_dt before
        prior_end_dt triggers the INSERT.

    AS_OF = 2026-04-18T14:00Z → 25h into the post-reset window.
    """
    scenario_dir, app_dir = _scenario_dirs("reset-week")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)

    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    pre_reset_end = dt.datetime(2026, 4, 17, 14, 0, 0, tzinfo=dt.timezone.utc)
    post_reset_end = dt.datetime(2026, 4, 20, 14, 0, 0, tzinfo=dt.timezone.utc)
    reset_at = dt.datetime(2026, 4, 17, 13, 0, 0, tzinfo=dt.timezone.utc)

    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        # 4 prior weeks so the $/1% trend has signal. Use the default
        # 7-day cadence keyed off the POST-reset end so the trend builder
        # reads a stable weekly ladder without reset anomalies.
        next_off = _seed_prior_weeks(
            stats_conn, cache_conn,
            current_week_start=week_start,
            count=4,
            final_pct=38.0,
            cost_usd=15.0,
            model="claude-sonnet-4-6",
            projects=["/fake/repos/alpha", "/fake/repos/beta"],
        )

        # Pre-reset snapshots: pct 20 / 40 / 60, week_end_at on the
        # OLD boundary. Captured at T+20h, T+60h, T+90h — all before the
        # 2026-04-17T14Z pre_reset_end.
        for hrs_in, pct in [(20, 20.0), (60, 40.0), (90, 60.0)]:
            _insert_usage_snapshot(
                stats_conn,
                captured_at=week_start + dt.timedelta(hours=hrs_in),
                week_start=week_start, week_end=pre_reset_end, pct=pct,
            )

        # Post-reset snapshots: pct 0 → 5, week_end_at shifted to the
        # extended boundary. First post-reset capture is 1h before the
        # pre_reset_end (2026-04-17T13Z == reset_at) so backfill's
        # captured_dt < prior_end_dt check passes and a reset row is
        # inserted automatically on the harness's first open_db().
        for hrs_after_reset, pct in [(0, 0.0), (8, 2.0), (16, 3.0), (24, 4.0), (25, 5.0)]:
            _insert_usage_snapshot(
                stats_conn,
                captured_at=reset_at + dt.timedelta(hours=hrs_after_reset),
                week_start=week_start, week_end=post_reset_end, pct=pct,
            )

        # Pre-seed the week_reset_events row that `_backfill_week_reset_events`
        # would otherwise synthesize at first open. Inserting it here lets us
        # stamp the post-credit milestones with the matching `reset_event_id`
        # (Task 5) so the dashboard milestone-panel segment filter (Task 7)
        # surfaces them. AUTOINCREMENT on a fresh table assigns id=1; backfill
        # is `INSERT OR IGNORE` keyed on UNIQUE(old, new) so it no-ops at open.
        # Production stores boundary timestamps via `_canonicalize_optional_iso`
        # which renders the UTC offset as `+00:00`, NOT `Z` — use the matching
        # form here so the UNIQUE constraint recognizes the backfill's attempt
        # as a duplicate. With `Z` form, backfill would insert a SECOND row
        # with `+00:00`, the segment lookup would pick id=2, and milestones
        # stamped with id=1 would be filtered out as a stale segment.
        def _iso_canon(d: dt.datetime) -> str:
            return d.astimezone(dt.timezone.utc).isoformat(timespec="seconds")

        seed_week_reset_event(
            stats_conn,
            detected_at_utc=_iso_canon(reset_at),
            old_week_end_at=_iso_canon(pre_reset_end),
            new_week_end_at=_iso_canon(post_reset_end),
            effective_reset_at_utc=_iso_canon(reset_at),
        )
        reset_event_id_row = stats_conn.execute(
            "SELECT id FROM week_reset_events WHERE new_week_end_at = ?",
            (_iso_canon(post_reset_end),),
        ).fetchone()
        assert reset_event_id_row is not None, "reset event row missing"
        post_credit_event_id = int(reset_event_id_row[0])

        # Post-reset milestones 1..5. Keyed with week_start_date from the
        # week_start datetime — matches what `cmd_record_usage` writes on
        # live crossings, regardless of whether a reset happened. The
        # milestone lookup path under test must re-resolve this from the
        # latest usage snapshot, NOT from
        # `TuiCurrentWeek.week_start_at.date()` (which, post-override,
        # would be '2026-04-17'). reset_event_id stamps these as
        # post-credit segment milestones so they survive the v1.7.2
        # active-segment filter.
        _insert_milestones(
            stats_conn,
            week_start=week_start, week_end=post_reset_end,
            final_pct=5,
            dollars_per_percent=0.95,
            first_crossed_at=reset_at + dt.timedelta(hours=2),
            per_percent_spacing=dt.timedelta(hours=4, minutes=30),
            reset_event_id=post_credit_event_id,
        )

        # A few current-week sessions so spent_usd / $/1% have plausible
        # values post-override. Concentrated in the post-reset window
        # so `_sum_cost_for_range(reset_at, as_of, ...)` has entries to
        # pick up.
        for i in range(4):
            sid = f"reset-cur-s{i:03d}-0000-0000-0000-0000-000000000000"
            t0 = reset_at + dt.timedelta(hours=2 + i * 5)
            t1 = t0 + dt.timedelta(minutes=25)
            next_off = _seed_session(
                cache_conn,
                session_id=sid,
                project_path=f"/fake/repos/alpha",
                model="claude-sonnet-4-6",
                entries=[
                    (t0, 140_000, 18_000, 0, 0),
                    (t1,  90_000, 12_000, 0, 0),
                ],
                line_offset_start=next_off,
            )

        # Deterministic known-id session for GET /api/session/:id golden.
        # Multi-model to exercise primary/secondary role attribution,
        # positioned in the post-reset window.
        fixed_t0 = reset_at + dt.timedelta(hours=20)
        fixed_t1 = fixed_t0 + dt.timedelta(minutes=15)
        fixed_t2 = fixed_t0 + dt.timedelta(minutes=35)
        next_off = _seed_session_multi_model(
            cache_conn,
            session_id=FIXED_SESSION_IDS["reset-week"],
            project_path="/fake/repos/fixture-demo",
            entries=[
                (fixed_t0, "claude-sonnet-4-6", 180_000, 26_000, 0, 0),
                (fixed_t1, "claude-sonnet-4-6", 110_000, 16_000, 0, 0),
                (fixed_t2, "claude-opus-4-7",   130_000, 20_000, 0, 0),
            ],
            line_offset_start=next_off,
        )

        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()

    (scenario_dir / "input.env").write_text(
        f"AS_OF={_iso(as_of)}\n"
        f"FIXED_SESSION_ID={FIXED_SESSION_IDS['reset-week']}\n"
    )


def build_no_data(as_of: dt.datetime) -> None:
    """Empty DBs. All panels serialize as None; sessions.total == 0."""
    scenario_dir, app_dir = _scenario_dirs("no-data")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    (scenario_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")


def build_tz_override(as_of: dt.datetime) -> None:
    """F3 regression: scenario where the persisted config carries
    ``display.tz: "Asia/Tokyo"`` but the dashboard server is launched
    with ``--tz utc``. Asserts the override beats the persisted config
    in the envelope's ``display`` block (``resolved_tz`` becomes
    ``Etc/UTC``, ``tz`` becomes ``"utc"``, ``pinned: true``).

    Persisted ``Asia/Tokyo`` (rather than ``"local"``) is what makes the
    override observable here: the harness runs under ``TZ=Etc/UTC``, so
    ``"local"`` would resolve to the same ``Etc/UTC`` as ``--tz utc``
    and any tz-sensitive label would be byte-identical regardless of
    which path won.

    Seeds enough state for ``GET /api/block/<start_at>`` to find an
    API-anchored 5h block at ``[10:00Z, 15:00Z)`` on 2026-04-20:

      * One ``weekly_usage_snapshots`` row carries
        ``five_hour_resets_at = 2026-04-20T15:00:00Z`` (recorded
        anchor); ``_load_recorded_five_hour_windows`` picks it up.
      * One ``session_entries`` row at 2026-04-20T12:00:00Z falls
        inside the resulting block window so
        ``_handle_get_block_detail`` aggregates a block that the harness
        can fetch by URL.

    The harness probes ``GET /api/block/2026-04-20T10:00:00+00:00`` and
    asserts the localized ``label`` uses the override zone (UTC →
    ``"10:00 Apr 20"``) rather than the persisted Tokyo zone (which
    would render as ``"19:00 Apr 20"``).

    The harness reads ``EXTRA_FLAGS`` from this scenario's input.env
    and appends the flags to its ``cctally dashboard`` invocation.
    """
    scenario_dir, app_dir = _scenario_dirs("tz-override")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    config_path = app_dir / "config.json"
    config_path.write_text(
        json.dumps({"display": {"tz": "Asia/Tokyo"}}, indent=2) + "\n"
    )

    # Subscription week containing AS_OF (2026-04-20T12:00Z): Mon
    # 2026-04-13T14Z → next Mon 2026-04-20T14Z. The single usage
    # snapshot is captured shortly before AS_OF so it anchors
    # current_week to the same week the block belongs to.
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end = week_start + dt.timedelta(days=7)
    five_hour_resets_at = dt.datetime(
        2026, 4, 20, 15, 0, 0, tzinfo=dt.timezone.utc,
    )
    captured_at = as_of  # 2026-04-20T12:00Z, inside the 10:00Z–15:00Z block
    entry_at = dt.datetime(2026, 4, 20, 12, 0, 0, tzinfo=dt.timezone.utc)

    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        stats_conn.execute(
            "INSERT INTO weekly_usage_snapshots(captured_at_utc, "
            "week_start_date, week_end_date, week_start_at, week_end_at, "
            "weekly_percent, source, payload_json, five_hour_percent, "
            "five_hour_resets_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                _iso(captured_at),
                week_start.date().isoformat(),
                week_end.date().isoformat(),
                _iso(week_start),
                _iso(week_end),
                12.0,
                "fixture",
                json.dumps({"fixture": True}),
                22.0,
                _iso(five_hour_resets_at),
            ),
        )
        # One session_entry inside the block window [10:00Z, 15:00Z)
        # so `_handle_get_block_detail` aggregates a non-empty block
        # at exactly start_at=2026-04-20T10:00Z.
        _seed_session(
            cache_conn,
            session_id="fixture-tz-override-block-0000000000000000",
            project_path="/fake/repos/fixture-tz-override",
            model="claude-sonnet-4-6",
            entries=[(entry_at, 100_000, 12_000, 0, 0)],
        )
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()

    (scenario_dir / "input.env").write_text(
        f"AS_OF={_iso(as_of)}\nEXTRA_FLAGS=--tz utc\n"
        # Probed by the harness via GET /api/block/<this value>. The
        # block is recorded-anchored at five_hour_resets_at - 5h.
        f"BLOCK_START_AT=2026-04-20T10:00:00+00:00\n"
    )


def build_utc_tz(as_of: dt.datetime) -> None:
    """Empty-DB sibling of ``no-data``, but pre-seeds
    ``config.json`` with ``display.tz: "utc"`` so the envelope's
    ``display`` block exercises the explicit-utc resolver path
    (``tz=="utc"``) rather than the default ``tz=="local"`` that the
    other four scenarios cover. Same shape as ``no-data`` everywhere
    else — the only diff in the golden vs. ``no-data`` is the
    ``display.tz`` value (``"utc"`` vs. ``"local"``).

    The dashboard harness ``cp -R``s ``$dir/.local`` into the scratch
    HOME, so the committed ``config.json`` is what the server reads.
    The shared harness lib's ``run_mode`` seed-skip gate keys on
    ``"display"`` substring presence, so this fixture's pre-seed wins
    over the lib's default ``utc`` injection in any future re-use.
    """
    scenario_dir, app_dir = _scenario_dirs("utc-tz")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    config_path = app_dir / "config.json"
    config_path.write_text(
        json.dumps({"display": {"tz": "utc"}}, indent=2) + "\n"
    )
    (scenario_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")


SCENARIOS: dict[str, tuple[dt.datetime, "callable"]] = {
    "ok": (
        dt.datetime(2026, 4, 16, 14, 0, 0, tzinfo=dt.timezone.utc),
        build_ok,
    ),
    "warn": (
        dt.datetime(2026, 4, 18, 20, 0, 0, tzinfo=dt.timezone.utc),
        build_warn,
    ),
    "over": (
        dt.datetime(2026, 4, 19, 10, 0, 0, tzinfo=dt.timezone.utc),
        build_over,
    ),
    "reset-week": (
        dt.datetime(2026, 4, 18, 14, 0, 0, tzinfo=dt.timezone.utc),
        build_reset_week,
    ),
    "no-data": (
        dt.datetime(2026, 4, 20, 12, 0, 0, tzinfo=dt.timezone.utc),
        build_no_data,
    ),
    "utc-tz": (
        dt.datetime(2026, 4, 20, 12, 0, 0, tzinfo=dt.timezone.utc),
        build_utc_tz,
    ),
    "tz-override": (
        dt.datetime(2026, 4, 20, 12, 0, 0, tzinfo=dt.timezone.utc),
        build_tz_override,
    ),
}


if __name__ == "__main__":
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for name, (as_of, fn) in SCENARIOS.items():
        fn(as_of)
        print(f"built: {name}")
    print(f"Built fixtures under {FIXTURES_DIR}")
