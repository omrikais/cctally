#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally dashboard`.

Writes one pair of (stats.db, cache.db) per scenario under
``tests/fixtures/dashboard/<scenario>/.local/share/cctally/``.
All schema/seeding goes through ``bin/_fixture_builders.py`` — do not
duplicate schema here. Idempotent: each builder overwrites existing DBs.

Sixteen scenarios:
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
  * ``utc-tz``     — empty-schema sibling of ``no-data`` but pre-seeds
                     ``config.json`` with ``display.tz: "utc"`` to exercise
                     the explicit-utc resolver path in the ``display`` block.
  * ``tz-override``— persisted ``display.tz: "Asia/Tokyo"`` but launched with
                     ``--tz utc`` (F3 override regression); also seeds a 5h
                     block the harness fetches via ``GET /api/block/<start>``.
  * ``projected-alerts`` — projected-pace alert axis (#121): two alerted
                     ``projected_milestones`` rows + a budget config with both
                     projected toggles on, so the envelope's fourth (projected)
                     alert leg and the ``alerts_settings`` projected mirror are
                     asserted.
  * ``command-secret`` — config stores a CONFIGURED, secret-bearing
                     ``alerts.command_template`` (a webhook ``curl`` with a
                     bearer token) + ``notifier="command"``. Makes the
                     dashboard's secret-redaction line load-bearing: both the
                     SSE ``alerts_settings`` mirror and the POST echo must
                     surface only ``command_configured: true`` and never leak
                     the raw template / ``SECRETXYZ`` token to a client.
  * ``codex-cache-active`` — 14 days of fully-qualified Codex rollout
                     entries INCLUDING today (#443 S2 F27). Every other
                     scenario ships an empty Codex source, so no assembled
                     golden proved a COMPUTED Codex cache report survives
                     envelope assembly.
  * ``codex-cache-idle`` — the same history with nothing dated today, so
                     the golden captures the synthetic unobserved today
                     row (#443 F13/F14) at ``days[0]``.
  * ``cache-report-qa`` — production-shaped Claude cache activity with six
                     baseline days, an amber cache-drop today, and both
                     positive/negative net days for real-browser QA (#452).
  * ``all-combined`` — both providers populated and undecorated, so the All
                     headline publishes the provider-compatible total (#556).
  * ``all-combined-decorated`` — the same accounting with Claude decoration,
                     which withholds the All combined headline (#556).
  * ``all-budget-account-focus`` — two focusable Codex accounts with distinct
                     provider/account budgets, spend, and quota (#556 S5).
  * ``codex-account-fallback`` — two real Codex accounts where only one owns a
                     live weekly cycle, plus the unattributed sentinel and
                     spend on both sides of the trailing-cycle boundary (#591).

Each scenario writes ``input.env`` containing a single line
``AS_OF=<iso-utc>`` consumed by the dashboard harness via
``CCTALLY_AS_OF``.

Run: ``bin/build-dashboard-fixtures.py`` (idempotent; overwrites).

Migration posture (cctally-dev#94): every scenario's stats.db ships as a
fully-migrated user — `stamp_all_stats_migrations_applied` marks every
registered stats migration applied + sets `PRAGMA user_version = len(registry)`
so the dispatcher fast-paths and no dedup-recompute body (008/009/010) can
overwrite the seeded display tables on a dashboard read. This mirrors
`bin/build-share-fixtures.py` and replaces the prior fragile reliance on the
#93 upgrade-gate happening to DEFER on the `--no-sync` dashboard path (a future
dashboard change that wrote the cache_meta walk-complete marker would otherwise
flip the gate to PROCEED and recompute the seeded tables to $0). `_stamp_and_verify`
asserts the stamped state per scenario.
"""
from __future__ import annotations

import datetime as dt
import importlib.machinery
import json
import os
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    bump_codex_physical_mutation_seq,
    create_cache_db,
    create_stats_db,
    fixture_source_timestamp_z,
    seed_account,
    seed_codex_conversation_thread,
    seed_codex_quota_snapshot,
    seed_codex_session_entry,
    seed_codex_session_file,
    seed_codex_source_root,
    seed_session_entry,
    seed_session_file,
    seed_week_reset_event,
    stamp_all_stats_migrations_applied,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/dashboard"


def _stamp_and_verify(stats_conn: sqlite3.Connection) -> None:
    """Stamp the dashboard stats.db as a fully-migrated user (cctally-dev#94)
    and assert it — see the module docstring 'Migration posture' section.

    Replaces the prior load-bearing-but-undocumented invariant ("the #93
    upgrade-gate happens to DEFER on the --no-sync dashboard path") with an
    explicit, checked one: every registered stats migration is stamped applied
    so the dispatcher fast-paths and no dedup-recompute body (008/009/010) can
    overwrite the seeded display tables, regardless of whether a future
    dashboard change writes the cache_meta walk-complete marker.
    """
    from _cctally_db import _STATS_MIGRATIONS
    stamp_all_stats_migrations_applied(stats_conn)
    applied = {r[0] for r in stats_conn.execute(
        "SELECT name FROM schema_migrations")}
    expected = {m.name for m in _STATS_MIGRATIONS}
    assert applied >= expected, f"stamp incomplete: missing {expected - applied}"
    uv = stats_conn.execute("PRAGMA user_version").fetchone()[0]
    assert uv == len(_STATS_MIGRATIONS), (
        f"user_version={uv} != len(registry)={len(_STATS_MIGRATIONS)}")


def _iso(d: dt.datetime) -> str:
    """Serialize a datetime as UTC-ISO with `Z` suffix, preserving any
    fractional second (#568)."""
    return fixture_source_timestamp_z(d)


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


def _seed_budget_milestone(
    stats_conn: sqlite3.Connection,
    *,
    week_start: dt.datetime,
    threshold: int,
    budget_usd: float,
    spent_usd: float,
    crossed_at: dt.datetime,
    period: str = "subscription-week",
    vendor: str = "claude",
    alerted: bool = True,
    alerted_at_raw: str | None = None,          # #556 S3 §5.3
) -> None:
    """Seed one ``budget_milestones`` row (issue #19) in the unified
    vendor-tagged table (#143). ``vendor`` ∈ ``'claude'|'codex'`` (default
    ``'claude'`` — the scenarios this builder depicts are the Claude axis).
    ``week_start`` is the effective (post-reset) ISO timestamp the resolver
    returns, written to the renamed ``period_start_at`` column, matching the
    dispatch payload's ``budget:<period_start_at>:<threshold>`` id. ``period``
    (#137) is the write-once period discriminator the envelope now reads FROM THE
    ROW (default ``subscription-week``); it is part of the ``UNIQUE(vendor,
    period_start_at, period, threshold)`` key and the envelope id's
    ``budget:<period_start_at>:<period>:<threshold>`` shape. ``alerted_at`` is
    set to ``crossed_at`` (set-then-dispatch) when ``alerted`` so the dashboard
    envelope's budget leg (``WHERE alerted_at IS NOT NULL``) picks it up; left
    NULL otherwise.

    ``alerted_at_raw`` (#556 S3 §5.3) writes a VERBATIM ISO string as the firing
    instant instead. Two reasons it takes a string and not a datetime: the
    firing instant is a different moment from the crossing instant and a
    fixture must be able to say so, and ``_iso`` canonicalizes every aware
    datetime to UTC ``Z``, which would erase the offset spelling the
    ``all-combined`` alert rows exist to exercise."""
    consumption_pct = (spent_usd / budget_usd * 100.0) if budget_usd else 0.0
    stats_conn.execute(
        """INSERT INTO budget_milestones
           (vendor, period_start_at, period, threshold, budget_usd, spent_usd,
            consumption_pct, crossed_at_utc, alerted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(vendor),
            _iso(week_start),
            str(period),
            int(threshold),
            float(budget_usd),
            float(spent_usd),
            float(consumption_pct),
            _iso(crossed_at),
            # `alerted_at_raw` bypasses `_iso` DELIBERATELY. `_iso` canonicalizes
            # every aware datetime to UTC 'Z', which would erase the offset
            # spelling this fixture exists to exercise.
            (alerted_at_raw if alerted_at_raw is not None
             else (_iso(crossed_at) if alerted else None)),
        ),
    )


def _seed_projected_milestone(
    stats_conn: sqlite3.Connection,
    *,
    week_start: dt.datetime,
    metric: str,
    threshold: int,
    projected_value: float,
    denominator: float,
    crossed_at: dt.datetime,
    period: str = "subscription-week",
    alerted: bool = True,
) -> None:
    """Seed one ``projected_milestones`` row (issue #121). ``week_start_at``
    is the effective (post-reset) ISO timestamp the resolver returns,
    matching the dispatch payload's
    ``projected:<week_start_at>:<metric>:<threshold>`` id. ``period`` (#137) is
    the write-once period discriminator now in the
    ``UNIQUE(week_start_at, period, metric, threshold)`` key and the envelope
    id's ``projected:<week_start_at>:<period>:<metric>:<threshold>`` shape
    (default ``subscription-week`` — the legacy scenarios this builder depicts);
    projected's ``context`` stays metric-driven, so ``period`` only segments the
    id. ``alerted_at`` is set to ``crossed_at`` (set-then-dispatch) when
    ``alerted`` so the dashboard envelope's projected leg
    (``WHERE alerted_at IS NOT NULL``) picks it up; left NULL otherwise.
    ``denominator`` is the at-crossing target the envelope renders from the ROW
    (100.0 for weekly_pct, target_usd for budget_usd)."""
    stats_conn.execute(
        """INSERT INTO projected_milestones
           (week_start_at, period, metric, threshold, projected_value,
            denominator, crossed_at_utc, alerted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            _iso(week_start),
            str(period),
            str(metric),
            int(threshold),
            float(projected_value),
            float(denominator),
            _iso(crossed_at),
            _iso(crossed_at) if alerted else None,
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
            # The DATETIME, not `_iso(ts)`. `_iso` formats with `strftime`
            # ("%H:%M:%SZ"), which silently truncates microseconds — so a
            # boundary sentinel seeded one microsecond outside a bound landed
            # exactly ON it and could not discriminate at all (#556 S2 §9.2).
            # `seed_session_entry` normalizes through `fixture_timestamp_utc`,
            # which accepts a datetime and preserves the fraction. Every
            # pre-existing caller passes a whole-second datetime, so their
            # stored bytes are unchanged.
            timestamp_utc=ts,
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
            # The DATETIME, not `_iso(ts)`: `_iso` formats with `strftime`,
            # which silently truncates microseconds. The seeder normalizes
            # through `fixture_timestamp_utc`, which accepts a datetime and
            # preserves the fraction. Byte-neutral for every whole-second
            # caller here, and it keeps a future sub-second sentinel able to
            # discriminate (#556 S2 §9.2).
            timestamp_utc=ts,
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

        # Cache activity (#272 §4 non-vacuity): two distinct project_paths
        # each with non-zero cache_creation / cache_read across two UTC days,
        # so the dashboard cache-report by_project two-level ``stable_sum``
        # fold is exercised non-vacuously. Every other session in this
        # scenario seeds zero cache tokens, which would make by_project
        # ``net_usd`` 0.0 everywhere and vacuously test the fold. The two
        # projects have opposite cache profiles so their window ``net_usd``
        # values are distinct and non-zero: one read-heavy (positive net,
        # caching helped) and one creation-heavy (negative net). Each spans
        # 2026-04-14 and 2026-04-15 (both inside the 14-day cache window and
        # the current subscription week) with multiple same-day entries so
        # the within-day per-project net partial is itself a multi-term
        # ``stable_sum``. The cache-heavy token counts are deliberately
        # "awkward" (non-round) so the flat running left-fold and the
        # two-level ``stable_sum`` fold differ by one ULP
        # (flat 2.78296815 vs two-level 2.7829681500000003): this makes the
        # #272 §4 fold redefinition load-bearing — reverting the builder to
        # the old ``_aggregate_cache_breakdown`` flat fold turns this golden
        # RED, proving the two-level fold is non-vacuously tested.
        d14 = dt.datetime(2026, 4, 14, tzinfo=dt.timezone.utc)
        d15 = dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc)
        next_off = _seed_session(
            cache_conn,
            session_id="ok-cache-heavy-0000-0000-0000-000000000000",
            project_path="/fake/repos/cache-heavy",
            model="claude-sonnet-4-6",
            entries=[
                (d14 + dt.timedelta(hours=10), 40_000, 6_000, 222_571, 30_991),
                (d14 + dt.timedelta(hours=11), 22_000, 3_000, 433_509, 296_461),
                (d14 + dt.timedelta(hours=12), 12_000, 2_000, 64_908, 496_737),
                (d15 + dt.timedelta(hours=10), 35_000, 5_000, 117_042, 330_630),
                (d15 + dt.timedelta(hours=11), 18_000, 2_000, 328_956, 305_659),
                (d15 + dt.timedelta(hours=12), 10_000, 1_000, 496_873, 32_434),
            ],
            line_offset_start=next_off,
        )
        next_off = _seed_session(
            cache_conn,
            session_id="ok-cache-waste-0000-0000-0000-000000000000",
            project_path="/fake/repos/cache-waste",
            model="claude-sonnet-4-6",
            entries=[
                (d14 + dt.timedelta(hours=10), 30_000, 5_000, 300_000, 20_000),
                (d14 + dt.timedelta(hours=11), 20_000, 3_000, 200_000, 10_000),
                (d15 + dt.timedelta(hours=10), 25_000, 4_000, 250_000, 15_000),
                (d15 + dt.timedelta(hours=11), 15_000, 2_000, 150_000, 8_000),
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
        # Budget axis (issue #19): one alerted budget crossing so the
        # dashboard envelope's third (budget) alert leg + alerts_settings
        # budget mirror are asserted by the golden. budget_usd=$50;
        # spent $45 = 90% crossing. week_start_at matches the effective
        # week-start ISO the resolver returns (no mid-week reset here).
        _seed_budget_milestone(
            stats_conn,
            week_start=week_start,
            threshold=90,
            budget_usd=50.0,
            spent_usd=45.0,
            crossed_at=week_start + dt.timedelta(hours=70),
            alerted=True,
        )
        _stamp_and_verify(stats_conn)
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()

    # Persist a `budget` config block so the envelope's alerts_settings
    # carries budget_thresholds + budget_enabled=True (issue #19). Budget
    # is its OWN config block, sourced from `_get_budget_config`.
    config_path = app_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "budget": {
                    "weekly_usd": 50.0,
                    "alerts_enabled": True,
                    "alert_thresholds": [90, 100],
                }
            },
            indent=2,
        )
        + "\n"
    )

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
        _stamp_and_verify(stats_conn)
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
        _stamp_and_verify(stats_conn)
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

        _stamp_and_verify(stats_conn)
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
    """Empty DBs. All panels serialize as None; sessions.total == 0.

    Still stamped fully-migrated (cctally-dev#94) so the committed fixture is
    unambiguous. (Empty fixtures already reach applied=len(registry) at open
    because the gate has no historical rows to protect, but stamping makes the
    posture explicit and identical to the data-rich scenarios.)
    """
    scenario_dir, app_dir = _scenario_dirs("no-data")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    conn = sqlite3.connect(stats_path)
    try:
        _stamp_and_verify(conn)
        conn.commit()
    finally:
        conn.close()
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
        _stamp_and_verify(stats_conn)
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
    conn = sqlite3.connect(stats_path)
    try:
        _stamp_and_verify(conn)
        conn.commit()
    finally:
        conn.close()
    (scenario_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")


def build_projected_alerts(as_of: dt.datetime) -> None:
    """Projected-pace alert axis (issue #121). Seeds two alerted
    ``projected_milestones`` rows — one ``weekly_pct`` (100% of cap) and one
    ``budget_usd`` (100% of a $50 budget) — plus a budget config block with
    BOTH projected toggles on, so the dashboard envelope's fourth (projected)
    alert leg and the ``alerts_settings`` projected mirror
    (``projected_weekly_enabled`` / ``projected_budget_enabled``) are asserted
    by the golden. A budget milestone is also seeded so the projected + budget
    legs coexist (envelope union ordering). Minimal world otherwise (one week
    anchor + one session) — this scenario exists to lock the projected
    surface, not the panels.

    No ``FIXED_SESSION_ID`` in input.env → the harness skips the
    ``/api/session/:id`` golden leg (same as ``no-data`` / ``utc-tz``).
    """
    scenario_dir, app_dir = _scenario_dirs("projected-alerts")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)

    # AS_OF = 2026-04-16T14:00Z → 72h into the week (~day 4 of 7).
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end = week_start + dt.timedelta(days=7)

    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        # A few current-week snapshots so the week window resolves and the
        # forecast/current-week panels render a non-empty envelope.
        for hrs_in, pct in [(6, 5.0), (36, 25.0), (72, 50.0)]:
            _insert_usage_snapshot(
                stats_conn,
                captured_at=week_start + dt.timedelta(hours=hrs_in),
                week_start=week_start, week_end=week_end, pct=pct,
            )
        _seed_session(
            cache_conn,
            session_id="projected-alerts-cur-000000000000000000",
            project_path="/fake/repos/fixture-projected",
            model="claude-sonnet-4-6",
            entries=[(week_start + dt.timedelta(hours=70), 120_000, 18_000, 0, 0)],
        )

        # Budget axis (issue #19): one alerted crossing so projected + budget
        # legs coexist in the envelope union.
        _seed_budget_milestone(
            stats_conn,
            week_start=week_start,
            threshold=90,
            budget_usd=50.0,
            spent_usd=45.0,
            crossed_at=week_start + dt.timedelta(hours=68),
            alerted=True,
        )
        # Projected axis (issue #121): one weekly_pct row (denominator 100.0)
        # and one budget_usd row (denominator = $50 target). alerted_at set so
        # the envelope's `WHERE alerted_at IS NOT NULL` leg picks them up.
        _seed_projected_milestone(
            stats_conn,
            week_start=week_start,
            metric="weekly_pct",
            threshold=100,
            projected_value=104.0,
            denominator=100.0,
            crossed_at=week_start + dt.timedelta(hours=71),
            alerted=True,
        )
        _seed_projected_milestone(
            stats_conn,
            week_start=week_start,
            metric="budget_usd",
            threshold=100,
            projected_value=52.0,
            denominator=50.0,
            crossed_at=week_start + dt.timedelta(hours=72),
            alerted=True,
        )
        _stamp_and_verify(stats_conn)
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()

    # Budget config with BOTH projected toggles ON so alerts_settings carries
    # projected_weekly_enabled=True (via alerts.projected_enabled) and
    # projected_budget_enabled=True (via budget.projected_enabled).
    config_path = app_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "alerts": {
                    "enabled": True,
                    "projected_enabled": True,
                },
                "budget": {
                    "weekly_usd": 50.0,
                    "alerts_enabled": True,
                    "alert_thresholds": [90, 100],
                    "projected_enabled": True,
                },
            },
            indent=2,
        )
        + "\n"
    )

    (scenario_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")


def build_command_secret(as_of: dt.datetime) -> None:
    """Secret-redaction guard (Phase B Task 4). Seeds a config whose
    ``alerts`` block stores a CONFIGURED, secret-bearing
    ``command_template`` (a webhook ``curl`` carrying a bearer token) and
    ``alerts.notifier = "command"``. The point of this scenario is to make
    the dashboard's secret-redaction line load-bearing in the suite: the
    SSE ``alerts_settings`` mirror and the ``POST /api/settings`` 200 echo
    must BOTH surface only ``command_configured: true`` (a boolean) and
    must NEVER leak the raw ``command_template`` array — nor the literal
    ``SECRETXYZ`` token it carries — to any client.

    The whole suite otherwise only ever exercises configs where
    ``command_template`` is ``None``, so a regression that deleted the
    POST-echo redaction (``_a.pop("command_template", ...)``) would stay
    green. This fixture closes that gap (see the matching harness
    assertions in ``bin/cctally-dashboard-test``).

    Minimal world otherwise (one week anchor + one session) so the
    envelope resolves cleanly; no ``FIXED_SESSION_ID`` in input.env, so
    the harness skips the ``/api/session/:id`` golden leg (same as
    ``no-data`` / ``utc-tz`` / ``projected-alerts``).
    """
    scenario_dir, app_dir = _scenario_dirs("command-secret")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)

    # AS_OF = 2026-04-16T14:00Z → 72h into the week (~day 4 of 7).
    week_start = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
    week_end = week_start + dt.timedelta(days=7)

    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        for hrs_in, pct in [(6, 5.0), (36, 25.0), (72, 50.0)]:
            _insert_usage_snapshot(
                stats_conn,
                captured_at=week_start + dt.timedelta(hours=hrs_in),
                week_start=week_start, week_end=week_end, pct=pct,
            )
        _seed_session(
            cache_conn,
            session_id="command-secret-cur-0000000000000000000",
            project_path="/fake/repos/fixture-command-secret",
            model="claude-sonnet-4-6",
            entries=[(week_start + dt.timedelta(hours=70), 120_000, 18_000, 0, 0)],
        )
        _stamp_and_verify(stats_conn)
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()

    # Configured, secret-bearing command_template + notifier=command. The
    # bearer token (``SECRETXYZ``) is the canary the harness asserts never
    # reaches a client through either the SSE mirror or the POST echo.
    config_path = app_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "alerts": {
                    "enabled": True,
                    "notifier": "command",
                    "command_template": [
                        "curl",
                        "-H",
                        "Authorization: Bearer SECRETXYZ",
                        "https://hook.example/x",
                    ],
                }
            },
            indent=2,
        )
        + "\n"
    )

    (scenario_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")


# === #443 S2 — Codex cache-report scenarios ==============================
# Every pre-existing scenario ships an EMPTY Codex source, so no assembled
# golden ever proved a COMPUTED Codex cache report survives envelope
# assembly (#443 F27). These two do, and the pair is what separates the
# two truths S2 adds: `codex-cache-active` has a real row for today,
# `codex-cache-idle` has none and must therefore publish a synthetic
# unobserved one.

_CODEX_ROOT_KEY = "fixture-codex-root"
_CODEX_ROOT_PATH = "/fake/codex"
_CODEX_CWD = "/fake/repos/fixture-codex-project"


def _seed_codex_history(
    cache_conn: sqlite3.Connection, *, as_of: dt.datetime, day_offsets: list[int],
) -> None:
    """Seed one fully-qualified Codex rollout with one entry per offset day.

    Fully-qualified means the entry carries `source_root_key` +
    `conversation_key` AND a matching `codex_conversation_threads` row, so
    `load_codex_project_metadata_health` classifies every row as qualified
    and the dashboard resolves a real project label instead of degrading to
    the unqualified accounting fallback.
    """
    seed_codex_source_root(
        cache_conn,
        source_root_key=_CODEX_ROOT_KEY,
        canonical_root_path=_CODEX_ROOT_PATH,
    )
    session_id = "fixture-codex-0000-0000-000000000001"
    conversation_key = f"v1.{_CODEX_ROOT_KEY}.{session_id}"
    file_path = f"{_CODEX_ROOT_PATH}/sessions/{session_id}.jsonl"
    seed_codex_session_file(
        cache_conn,
        path=file_path,
        last_session_id=session_id,
        last_model="gpt-5",
        source_root_key=_CODEX_ROOT_KEY,
        last_native_thread_id=session_id,
        last_conversation_key=conversation_key,
    )
    seed_codex_conversation_thread(
        cache_conn,
        conversation_key=conversation_key,
        source_root_key=_CODEX_ROOT_KEY,
        native_thread_id=session_id,
        source_path=file_path,
        cwd=_CODEX_CWD,
    )
    # Cached share walks 60/64/68/72/76% and repeats, so the daily rows
    # differ from one another and the baseline median is a real statistic
    # rather than a constant. Deterministic: derived only from the offset.
    for line_offset, offset in enumerate(day_offsets):
        # Keep offset 0 exactly at AS_OF. The shared fixture seeder stores the
        # same +00:00 form as production, so the reader's AS_OF + 1us upper
        # bound includes this row and the scenario pins the boundary contract.
        ts = as_of - dt.timedelta(days=offset)
        input_tokens = 20_000
        cached = input_tokens * (60 + 4 * (offset % 5)) // 100
        seed_codex_session_entry(
            cache_conn,
            source_path=file_path,
            line_offset=line_offset,
            # The DATETIME, not `_iso(ts)`: `_iso` formats with `strftime`,
            # which silently truncates microseconds. The seeder normalizes
            # through `fixture_timestamp_utc`, which accepts a datetime and
            # preserves the fraction. Byte-neutral for every whole-second
            # caller here, and it keeps a future sub-second sentinel able to
            # discriminate (#556 S2 §9.2).
            timestamp_utc=ts,
            session_id=session_id,
            model="gpt-5",
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=1_500,
            reasoning_output_tokens=300,
            total_tokens=input_tokens + 1_500,
            source_root_key=_CODEX_ROOT_KEY,
            conversation_key=conversation_key,
        )


def _build_codex_cache_scenario(
    name: str, as_of: dt.datetime, *, day_offsets: list[int],
) -> None:
    scenario_dir, app_dir = _scenario_dirs(name)
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        _seed_codex_history(cache_conn, as_of=as_of, day_offsets=day_offsets)
        _stamp_and_verify(stats_conn)
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()
    (scenario_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")


def build_codex_cache_active(as_of: dt.datetime) -> None:
    """14 days of Codex history INCLUDING today.

    Proves a computed, non-empty Codex cache report survives envelope
    assembly: `cached_input_percent` beside the transitional
    `cache_hit_percent`, a populated `not_applicable`,
    `anomaly_predicates == ["cache_drop"]`, and real breakdowns.
    """
    _build_codex_cache_scenario(
        "codex-cache-active", as_of, day_offsets=list(range(0, 14)))


def build_codex_cache_idle(as_of: dt.datetime) -> None:
    """The same history with NOTHING dated today.

    Proves the synthetic unobserved today row survives assembly: `days[0]`
    carries today's date with `observed: false` rather than leaving the
    newest real row wearing the chart's positional "Today" label.
    """
    _build_codex_cache_scenario(
        "codex-cache-idle", as_of, day_offsets=list(range(1, 14)))


def build_cache_report_qa(as_of: dt.datetime) -> None:
    """Reach the rich Cache Report QA state through production aggregation.

    Start from the ordinary ``ok`` fixture so every surrounding dashboard
    panel remains realistic, then append seven cache-shaped days. Six prior
    days satisfy the classifier's baseline floor; today is creation-heavy and
    far below that baseline, so the real classifier emits amber ``cache_drop``.
    Read-heavy and creation-heavy rows make the mini net bars mixed-sign.
    """
    build_ok(as_of)
    source_app = FIXTURES_DIR / "ok" / ".local" / "share" / "cctally"
    scenario_dir, app_dir = _scenario_dirs("cache-report-qa")
    create_stats_db(app_dir / "stats.db")
    create_cache_db(app_dir / "cache.db")
    for filename in ("stats.db", "cache.db"):
        source = sqlite3.connect(source_app / filename)
        target = sqlite3.connect(app_dir / filename)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
    config_text = (source_app / "config.json").read_text()
    (app_dir / "config.json").write_text(config_text)

    cache_conn = sqlite3.connect(app_dir / "cache.db")
    try:
        entries = []
        # Five unequivocally read-heavy days plus one negative prior day: the
        # baseline has >=5 samples and the chart proves both signs.
        for offset in range(6, 1, -1):
            day = (as_of - dt.timedelta(days=offset)).replace(
                hour=10, minute=0, second=0, microsecond=0,
            )
            entries.append((day, 50_000, 5_000, 50_000, 1_200_000))
        prior_negative = (as_of - dt.timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0,
        )
        entries.append((prior_negative, 50_000, 5_000, 1_000_000, 20_000))
        today = as_of.replace(hour=10, minute=0, second=0, microsecond=0)
        entries.append((today, 50_000, 5_000, 1_200_000, 10_000))
        _seed_session(
            cache_conn,
            session_id="cache-report-qa-0000-0000-0000-000000000001",
            project_path="/fake/repos/cache-report-qa",
            model="claude-sonnet-4-6",
            entries=entries,
        )
        cache_conn.commit()
    finally:
        cache_conn.close()
    (scenario_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")


# === #556 S1 — the both-provider combined-headline scenarios ==============
#
# Every pre-existing scenario leaves the All hero's `combined` either withheld
# or Claude-only, so no golden ever showed the figure the issue is about. These
# two do: `all-combined` is the ordinary undecorated install where BOTH legs
# publish, and `all-combined-decorated` is the multi-account install where the
# figure is withheld with a named reason (spec §3.2).

# AS_OF is 2026-04-16T14:00:00Z for both. The two cycles deliberately do NOT
# share a range — that is the property under test — and Claude's starts first.
_AC_CLAUDE_WEEK_START = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
_AC_CLAUDE_WEEK_END = _AC_CLAUDE_WEEK_START + dt.timedelta(days=7)
_AC_CODEX_RESETS_AT = dt.datetime(2026, 4, 21, 8, 0, 0, tzinfo=dt.timezone.utc)
_AC_CODEX_CYCLE_START = _AC_CODEX_RESETS_AT - dt.timedelta(minutes=10_080)

_AC_CODEX_ROOT_KEY = "fixture-all-combined-root"
_AC_CODEX_ROOT_PATH = "/fake/codex-all-combined"
_AC_CODEX_CWD = "/fake/repos/all-combined-codex"
_AC_CODEX_SESSION_ID = "fixture-allcomb-0000-0000-000000000001"


def _seed_all_combined_claude(
    stats_conn: sqlite3.Connection,
    cache_conn: sqlite3.Connection,
    as_of: dt.datetime,
) -> None:
    """Seed Claude's subscription week, its percent history and its spend.

    The last percent capture is 600 seconds old, comfortably past the
    90-second `_OAUTH_USAGE_DEFAULTS["stale_after_seconds"]` bound, so the
    percent-observation clock reads STALE while the week itself is unexpired.
    That is the state that used to keep All's combined caveat permanently on.
    """
    for hours, pct in ((6, 5.0), (30, 18.0), (54, 29.0)):
        _insert_usage_snapshot(
            stats_conn,
            captured_at=_AC_CLAUDE_WEEK_START + dt.timedelta(hours=hours),
            week_start=_AC_CLAUDE_WEEK_START, week_end=_AC_CLAUDE_WEEK_END,
            pct=pct,
        )
    _insert_usage_snapshot(
        stats_conn,
        captured_at=as_of - dt.timedelta(seconds=600),
        week_start=_AC_CLAUDE_WEEK_START, week_end=_AC_CLAUDE_WEEK_END,
        pct=34.0,
    )

    next_off = 0
    # OUT OF RANGE, before the week: proves the lower bound is applied at all.
    next_off = _seed_session(
        cache_conn,
        session_id="allcomb-claude-before-week-0000-0000-0000",
        project_path="/fake/repos/all-combined-claude",
        model="claude-sonnet-4-6",
        entries=[
            (_AC_CLAUDE_WEEK_START - dt.timedelta(hours=6), 400_000, 60_000, 0, 0),
        ],
        line_offset_start=next_off,
    )
    # THE PREFIX ROW (spec §6.2). Inside Claude's week, OUTSIDE Codex's cycle.
    # Applying Codex's bounds to the Claude leg drops it, which is one of the
    # two directional mutations the oracle has to catch.
    next_off = _seed_session(
        cache_conn,
        session_id="allcomb-claude-prefix-0000-0000-0000-00",
        project_path="/fake/repos/all-combined-claude",
        model="claude-sonnet-4-6",
        entries=[
            (_AC_CLAUDE_WEEK_START + dt.timedelta(hours=4), 180_000, 26_000, 0, 0),
        ],
        line_offset_start=next_off,
    )
    # Inside BOTH cycles.
    next_off = _seed_session(
        cache_conn,
        session_id="allcomb-claude-overlap-0000-0000-0000-0",
        project_path="/fake/repos/all-combined-claude",
        model="claude-sonnet-4-6",
        entries=[
            (_AC_CODEX_CYCLE_START + dt.timedelta(hours=6), 90_000, 14_000, 40_000, 25_000),
            (as_of - dt.timedelta(hours=5), 60_000, 9_000, 0, 30_000),
        ],
        line_offset_start=next_off,
    )
    _seed_all_combined_range_sentinels(
        cache_conn, as_of=as_of, line_offset_start=next_off,
    )


# #556 S2 §9.2 — the shared-range sentinels.
#
# The shared aggregate range is midnight of the earliest built daily bucket to
# `now_utc`, exclusive at `now_utc + 1 microsecond`. This scenario's daily panel
# is a full thirty rows, so its floor is deterministic: AS_OF minus 29 days, at
# UTC midnight.
_AC_DAILY_PANEL_DAYS = 30

# The zone these two scenarios render in. They write no `EXTRA_FLAGS`, so no
# `--tz` override reaches the CLI and no `display.tz` is seeded, which leaves
# the harness environment governing — and every harness pins `TZ=Etc/UTC`.
# Named here rather than spelled `dt.timezone.utc` inside `_ac_shared_start`,
# because that function mirrors `resolve_shared_range`'s DISPLAY-zone floor:
# hardcoding UTC inside it makes the mirror hold only while the precondition
# does, silently, which is the same defect class one level up from the
# microsecond truncation this file already fixed once.
_AC_DISPLAY_TZ = dt.timezone.utc

def _assert_ac_display_zone(scenario_dir: Path) -> None:
    """Refuse to build if the scenario ever declares a display-zone override.

    `_ac_shared_start` floors to `_AC_DISPLAY_TZ`, so a scenario that gained an
    `EXTRA_FLAGS=--tz ...` line would place its "at-start" and "before-start"
    sentinels at the wrong instant while every boundary assertion kept passing.
    """
    text = (scenario_dir / "input.env").read_text()
    assert "EXTRA_FLAGS" not in text, (
        f"{scenario_dir.name} declares EXTRA_FLAGS; _ac_shared_start floors to "
        f"{_AC_DISPLAY_TZ} and would resolve the wrong shared start"
    )


def _ac_shared_start(as_of: dt.datetime) -> dt.datetime:
    """The resolved shared start, DERIVED from `as_of` rather than pinned.

    A hardcoded literal is the same defect class the microsecond truncation
    was: if AS_OF moves, the "at-start" and "before-start" sentinels quietly
    stop being at the start, and the boundary assertions keep passing while
    testing nothing. This mirrors `resolve_shared_range`'s panel branch —
    midnight, in the scenario's display timezone, of the panel's oldest row.
    """
    oldest_day = as_of.astimezone(_AC_DISPLAY_TZ).date() - dt.timedelta(
        days=_AC_DAILY_PANEL_DAYS - 1,
    )
    return dt.datetime.combine(
        oldest_day, dt.time.min, tzinfo=_AC_DISPLAY_TZ,
    )


def _seed_all_combined_range_sentinels(
    cache_conn: sqlite3.Connection,
    *,
    as_of: dt.datetime,
    line_offset_start: int,
) -> int:
    """Seed the sentinels the All aggregate contract is decided by.

    Four boundary points, because two cannot distinguish the intended
    comparisons from the erroneous ones: exactly at the start (included), one
    microsecond before it (excluded), exactly at `now_utc` (included), and
    exactly at the exclusive end (excluded). Each gets its own project, so
    membership is readable straight off the published ranking.

    Plus two more the ranking itself depends on. The PRE-WEEK project sits
    inside the shared range but before Claude's current subscription week, so it
    appears in the All ranking only if the Claude fold is genuinely
    range-bounded — a regression to week-anchoring drops it. The TWIN PAIR is
    two projects at different roots sharing a basename with identical cost and
    identical session counts: the identical accounting tuple is what made the
    client's reverse search ambiguous, and the shared basename is what forces
    `_project_disambiguate_labels` to emit overrides at all.
    """
    next_off = line_offset_start
    shared_start = _ac_shared_start(as_of)
    twin_tokens = (44_000, 6_000, 0, 0)
    for session_id, project_path, ts, tokens in (
        # INCLUDED: exactly at the resolved shared start.
        ("allcomb-range-at-start-0000-0000-0000-0",
         "/fake/repos/allcomb-at-start",
         shared_start, (52_000, 7_000, 0, 0)),
        # EXCLUDED: one microsecond before it.
        ("allcomb-range-before-start-0000-0000-000",
         "/fake/repos/allcomb-before-start",
         shared_start - dt.timedelta(microseconds=1), (61_000, 8_000, 0, 0)),
        # INCLUDED: exactly at `now_utc`.
        ("allcomb-range-at-now-0000-0000-0000-000",
         "/fake/repos/allcomb-at-now",
         as_of, (33_000, 4_000, 0, 0)),
        # EXCLUDED: exactly at the exclusive upper bound.
        ("allcomb-range-at-end-0000-0000-0000-000",
         "/fake/repos/allcomb-at-end",
         as_of + dt.timedelta(microseconds=1), (77_000, 9_000, 0, 0)),
        # The load-bearing sentinel: in range, before the subscription week.
        ("allcomb-range-preweek-0000-0000-0000-00",
         "/fake/repos/allcomb-preweek",
         _AC_CLAUDE_WEEK_START - dt.timedelta(days=3), (85_000, 11_000, 0, 0)),
        # The twin pair — same basename, different roots, identical tuples.
        ("allcomb-range-twin-repos-0000-0000-0000",
         "/fake/repos/allcomb-twin/twin",
         _AC_CLAUDE_WEEK_START - dt.timedelta(days=2), twin_tokens),
        ("allcomb-range-twin-forks-0000-0000-0000",
         "/fake/forks/allcomb-twin/twin",
         _AC_CLAUDE_WEEK_START - dt.timedelta(days=2), twin_tokens),
    ):
        inp, out, cc, cr = tokens
        next_off = _seed_session(
            cache_conn,
            session_id=session_id,
            project_path=project_path,
            model="claude-sonnet-4-6",
            entries=[(ts, inp, out, cc, cr)],
            line_offset_start=next_off,
        )
    return next_off


def _seed_all_combined_codex(
    cache_conn: sqlite3.Connection, as_of: dt.datetime,
) -> None:
    """Seed Codex's native 7-day cycle, its observation and its spend.

    The weekly observation is captured 3601 seconds before AS_OF — one second
    past `stale_after_seconds(10_080) == 3600` — with a reset strictly AFTER
    AS_OF. So the boundary is stale-but-still-future: resolvable, and the spend
    it bounds is correct.
    """
    seed_codex_source_root(
        cache_conn,
        source_root_key=_AC_CODEX_ROOT_KEY,
        canonical_root_path=_AC_CODEX_ROOT_PATH,
    )
    conversation_key = f"v1.{_AC_CODEX_ROOT_KEY}.{_AC_CODEX_SESSION_ID}"
    file_path = f"{_AC_CODEX_ROOT_PATH}/sessions/{_AC_CODEX_SESSION_ID}.jsonl"
    seed_codex_session_file(
        cache_conn,
        path=file_path,
        last_session_id=_AC_CODEX_SESSION_ID,
        last_model="gpt-5",
        source_root_key=_AC_CODEX_ROOT_KEY,
        last_native_thread_id=_AC_CODEX_SESSION_ID,
        last_conversation_key=conversation_key,
    )
    seed_codex_conversation_thread(
        cache_conn,
        conversation_key=conversation_key,
        source_root_key=_AC_CODEX_ROOT_KEY,
        native_thread_id=_AC_CODEX_SESSION_ID,
        source_path=file_path,
        cwd=_AC_CODEX_CWD,
    )
    # (timestamp, input, cached, output, reasoning) — the first row is THE
    # OUT-OF-CYCLE SENTINEL (spec §6.2): it sits inside Claude's week and
    # before Codex's cycle start, so applying Claude's bounds to the Codex leg
    # picks it up. That is the other directional mutation. The two providers'
    # accounting lives in separate tables, so one row could only ever catch one
    # direction — hence a provider-specific row for each.
    #
    # The last two are the CODEX HALF of the §9.2 boundary claim. The spec
    # requires the four boundary points to agree across both provider
    # encodings, and Codex accounting lives in its own table, so a Claude row
    # can never exercise it. The first sits exactly at the resolved shared
    # start — outside Codex's native cycle, which is the point: the shared
    # ranking range and the native cycle are different intervals, and this row
    # is in one and not the other. The second sits exactly at `as_of`, the
    # endpoint the Claude CLI kernel includes inclusively and the Codex read
    # admits through its half-open `now_utc + 1us` bound.
    rows = (
        (_AC_CLAUDE_WEEK_START + dt.timedelta(hours=6), 310_000, 120_000, 24_000, 6_000),
        (_AC_CODEX_CYCLE_START + dt.timedelta(hours=9), 140_000, 55_000, 11_000, 2_500),
        (as_of - dt.timedelta(hours=3), 95_000, 30_000, 8_000, 1_500),
        (_ac_shared_start(as_of), 21_000, 6_000, 1_800, 400),
        (as_of, 13_000, 4_000, 1_100, 250),
    )
    for line_offset, (ts, inp, cached, out, reasoning) in enumerate(rows):
        seed_codex_session_entry(
            cache_conn,
            source_path=file_path,
            line_offset=line_offset,
            # The DATETIME, not `_iso(ts)`: `_iso` formats with `strftime`,
            # which silently truncates microseconds. The seeder normalizes
            # through `fixture_timestamp_utc`, which accepts a datetime and
            # preserves the fraction. Byte-neutral for every whole-second
            # caller here, and it keeps a future sub-second sentinel able to
            # discriminate (#556 S2 §9.2).
            timestamp_utc=ts,
            session_id=_AC_CODEX_SESSION_ID,
            model="gpt-5",
            input_tokens=inp,
            cached_input_tokens=cached,
            output_tokens=out,
            reasoning_output_tokens=reasoning,
            total_tokens=inp + out,
            source_root_key=_AC_CODEX_ROOT_KEY,
            conversation_key=conversation_key,
        )
    seed_codex_quota_snapshot(
        cache_conn,
        source_root_key=_AC_CODEX_ROOT_KEY,
        source_path=f"{_AC_CODEX_ROOT_PATH}/sessions/weekly-quota.jsonl",
        line_offset=0,
        captured_at_utc=_iso(as_of - dt.timedelta(seconds=3601)),
        logical_limit_key="fixture-all-combined-weekly",
        window_minutes=10_080,
        used_percent=41.0,
        resets_at_utc=_iso(_AC_CODEX_RESETS_AT),
        limit_name="Fixture weekly quota",
    )
    bump_codex_physical_mutation_seq(cache_conn)


def _reconcile_fixture_quota_projection(
    app_dir: Path, *, root_key: str, now: dt.datetime,
) -> None:
    """Run the real quota projector against one scenario's databases.

    Without this the source adapter finds no `quota_projection_state` row,
    `codex_projection_coherence` returns `missing_projection_state`, and the
    Codex hero publishes as `codex_projection_incoherent` — which is what every
    pre-existing Codex dashboard fixture does, and which would withhold the
    very combined figure this scenario exists to pin.

    The projector reads its databases through the process-wide path constants,
    so pin `CCTALLY_DATA_DIR` and re-run `_init_paths_from_env` before calling
    it. This mirrors `bin/build-bench-fixtures.py::_pin_env`; the re-init is
    what lets a second scenario in the same process target its own directory.

    The pin is UNDONE on the way out. The path constants are process-wide, so
    leaving `CCTALLY_DATA_DIR` pointed at this scenario would make any later
    builder that reaches the real modules write into the wrong directory. That
    is latent rather than live only because the two scenarios that call this
    are currently last in `SCENARIOS`, which is not a property a builder added
    later can rely on.
    """
    import importlib.util

    previous_data_dir = os.environ.get("CCTALLY_DATA_DIR")
    os.environ["CCTALLY_DATA_DIR"] = str(app_dir)
    os.environ.setdefault("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    bin_dir = str(Path(__file__).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    if "cctally" not in sys.modules:
        spec = importlib.util.spec_from_loader(
            "cctally",
            importlib.machinery.SourceFileLoader("cctally", str(Path(bin_dir) / "cctally")),
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["cctally"] = module
        spec.loader.exec_module(module)
    import _cctally_core
    import _cctally_quota

    _cctally_core._init_paths_from_env()
    try:
        _cctally_quota.reconcile_codex_quota_projection(
            source_root_keys=(root_key,), now=now,
        )
    finally:
        _pin_reconcile_nondeterminism(app_dir)
        _prune_reconcile_side_artifacts(app_dir)
        if previous_data_dir is None:
            os.environ.pop("CCTALLY_DATA_DIR", None)
        else:
            os.environ["CCTALLY_DATA_DIR"] = previous_data_dir
        _cctally_core._init_paths_from_env()


# One literal in place of the projector's per-pass random token. The token is a
# "which pass stamped this row" marker used only to orphan rows the CURRENT
# pass did not re-stamp; nothing cross-checks it against a stats-global value,
# and `codex_stats_digest` does not select it. Pinning it therefore changes no
# behaviour and makes the committed fixture reproducible.
_FIXTURE_QUOTA_GENERATION = "fixturegenerationfixturegeneration00"


def _pin_reconcile_nondeterminism(app_dir: Path) -> None:
    """Replace the two values a real projector pass cannot repeat.

    `tests/test_fixture_builder_contract.py` requires the committed tree to be
    exactly what the builder produces AND the builder to produce the same tree
    twice. A real pass writes `secrets.token_hex(16)` into every row's
    `generation` and records the wall-clock-named bootstrap journal segment in
    `journal_cursor`, so without this the two new scenarios would redden that
    contract on every run.
    """
    conn = sqlite3.connect(app_dir / "stats.db")
    try:
        for table in (
            "quota_projection_state", "quota_window_blocks",
            "quota_percent_milestones", "quota_threshold_events",
        ):
            try:
                conn.execute(
                    f"UPDATE {table} SET generation = ? "
                    "WHERE generation IS NOT NULL",
                    (_FIXTURE_QUOTA_GENERATION,),
                )
            except sqlite3.OperationalError:
                continue
        try:
            # The segment it names is deleted below with the rest of the
            # journal, so an empty cursor is the honest state, not a loss.
            conn.execute("DELETE FROM journal_cursor")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


# Everything a projector pass mints beside the two databases, enumerated.
# Opening the real databases writes an append-only journal directory, a log
# directory, several flock files and a `config.json`. Those are runtime state,
# not fixture input, and the journal's bootstrap file carries a wall-clock
# name — so a committed copy would differ on every rebuild and a `--out`
# scratch build would not match the tracked tree.
_RECONCILE_KEEP: frozenset[str] = frozenset({"stats.db", "cache.db"})
#
# `cache.db-wal` / `cache.db-shm` appear only from the SECOND scenario onward in
# one builder process, because the previous scenario's registered cache
# connection is still open when the next scenario opens its own file. Removing
# them is what the previous delete-by-exclusion rule already did, and the built
# tree is byte-identical either way, because the projector writes only to
# stats.db — the cache WAL it leaves behind carries no frames of its own.
# `_drain_cache_wal` below ENFORCES that last clause rather than trusting it:
# unlinking a WAL that does hold frames discards committed rows silently.
_RECONCILE_SIDE_ARTIFACTS: frozenset[str] = frozenset({
    "cache.db-shm",
    "cache.db-wal",
    "config.json",
    "config.json.lock",
    "journal",
    "journal.ingest.lock",
    "journal.lock",
    "logs",
    "stats.db.maintenance.lock",
})


def _drain_cache_wal(app_dir: Path) -> None:
    """Fold any cache WAL frames into `cache.db` before the WAL is unlinked.

    The prune list deletes `cache.db-wal` while a registered cache connection
    from the projector pass is still open, which is safe only because that pass
    writes to stats.db and leaves the cache WAL frameless. Nothing checked it.
    A `TRUNCATE` checkpoint makes the claim true instead of assumed: frames are
    written into the main database first, and a non-zero count is REPORTED
    rather than deleted, because a WAL that holds committed rows means the
    committed fixture is missing them.
    """
    if not (app_dir / "cache.db-wal").exists():
        return
    conn = sqlite3.connect(app_dir / "cache.db")
    try:
        busy, frames, _checkpointed = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
    finally:
        conn.close()
    if busy or frames:
        raise RuntimeError(
            f"{app_dir / 'cache.db-wal'} holds {frames} frame(s) "
            f"(busy={busy}) at prune time. The prune list assumes the cache WAL "
            "is empty because the projector writes only stats.db; that no "
            "longer holds, so pruning here would discard committed rows."
        )


def _prune_reconcile_side_artifacts(app_dir: Path) -> None:
    """Remove the enumerated side artifacts, and refuse anything unexpected.

    This asserts the expected set rather than deleting by exclusion. A
    delete-everything-but-two-names rule silently destroys whatever a future
    scenario legitimately places in the app dir, and it would do so most
    readily on exactly the mistake it is easiest to make — pointing this
    function at the wrong directory.
    """
    import shutil

    _drain_cache_wal(app_dir)
    unexpected = sorted(
        child.name for child in app_dir.iterdir()
        if child.name not in _RECONCILE_KEEP
        and child.name not in _RECONCILE_SIDE_ARTIFACTS
    )
    if unexpected:
        raise RuntimeError(
            f"unexpected entries in {app_dir}: {', '.join(unexpected)}. "
            "Add each to _RECONCILE_SIDE_ARTIFACTS if a projector pass really "
            "mints it; otherwise this function is pointed at the wrong "
            "directory."
        )
    for name in sorted(_RECONCILE_SIDE_ARTIFACTS):
        child = app_dir / name
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def build_all_combined(as_of: dt.datetime) -> None:
    """Both providers populated, undecorated, both cycles resolvable.

    This is the state the issue is about: two percent clocks stale (Claude's
    90-second bound and Codex's 3600-second one), both cycles resolvable, and
    the combined figure published unqualified with each leg naming its own
    cycle. It is also the only state in which spec §4.7's Forecast
    reason-string change is observable.
    """
    scenario_dir, app_dir = _scenario_dirs("all-combined")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        _seed_all_combined_claude(stats_conn, cache_conn, as_of)
        _seed_all_combined_codex(cache_conn, as_of)
        # #556 S3 §5.3 — four budget alerts whose TRUE firing order interleaves
        # the providers: Claude, Codex, Claude, Codex. Seeded HERE rather than
        # in either shared seeder, because `build_all_combined_decorated` calls
        # both and would otherwise change too — and because
        # `_seed_all_combined_codex` receives only `cache_conn`, while these
        # rows live in stats.db.
        #
        # Row 2 is spelled `+02:00`. Its true instant, 13:58Z, belongs SECOND,
        # but its local-time text sorts above every `Z`-spelled row, so a
        # lexicographic compare anywhere in the chain puts it first — that is
        # the trap, and it is why the seeder needed a raw-string override.
        #
        # The crossing instants are a DIFFERENT permutation, not merely
        # different values: their descending order is Codex, Claude, Codex,
        # Claude. A uniform offset would preserve the ordering exactly, so a
        # regression to sorting on `crossed_at` would reproduce this golden and
        # pass unnoticed.
        for _vendor, _period, _threshold, _period_start, _alerted_raw, _crossed in (
            ("claude", "subscription-week", 50, _AC_CLAUDE_WEEK_START,
             "2026-04-16T13:59:00Z", dt.datetime(2026, 4, 16, 13, 40, tzinfo=dt.timezone.utc)),
            ("codex", "calendar-month", 60, _AC_CODEX_CYCLE_START,
             "2026-04-16T15:58:00+02:00", dt.datetime(2026, 4, 16, 13, 55, tzinfo=dt.timezone.utc)),
            ("claude", "subscription-week", 75, _AC_CLAUDE_WEEK_START,
             "2026-04-16T13:57:00Z", dt.datetime(2026, 4, 16, 13, 50, tzinfo=dt.timezone.utc)),
            ("codex", "calendar-month", 80, _AC_CODEX_CYCLE_START,
             "2026-04-16T13:56:00Z", dt.datetime(2026, 4, 16, 13, 45, tzinfo=dt.timezone.utc)),
        ):
            _seed_budget_milestone(
                stats_conn,
                week_start=_period_start,
                threshold=_threshold,
                budget_usd=100.0,
                spent_usd=float(_threshold),
                crossed_at=_crossed,
                period=_period,
                vendor=_vendor,
                alerted_at_raw=_alerted_raw,
            )
        _stamp_and_verify(stats_conn)
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()
    _reconcile_fixture_quota_projection(
        app_dir, root_key=_AC_CODEX_ROOT_KEY, now=as_of,
    )
    (scenario_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")
    _assert_ac_display_zone(scenario_dir)


def build_all_combined_decorated(as_of: dt.datetime) -> None:
    """The same install with TWO real Claude accounts.

    Spec §3.2 withholds the combined figure under decoration rather than
    publishing one it cannot make checkable, so the withholding path and its
    named reason need a golden of their own.
    """
    scenario_dir, app_dir = _scenario_dirs("all-combined-decorated")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        _seed_all_combined_claude(stats_conn, cache_conn, as_of)
        _seed_all_combined_codex(cache_conn, as_of)
        for key, natural, email, label, plan in (
            ("a" * 32, "uuid-allcomb-work", "work@example.com", "work", "max"),
            ("b" * 32, "uuid-allcomb-home", "home@example.com", "home", "pro"),
        ):
            seed_account(
                stats_conn,
                account_key=key,
                provider="claude",
                natural_id=natural,
                email=email,
                label=label,
                plan_type=plan,
                label_source="user",
                first_seen_utc=_iso(_AC_CLAUDE_WEEK_START),
                last_seen_utc=_iso(as_of),
            )
        _stamp_and_verify(stats_conn)
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()
    _reconcile_fixture_quota_projection(
        app_dir, root_key=_AC_CODEX_ROOT_KEY, now=as_of,
    )
    (scenario_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")
    _assert_ac_display_zone(scenario_dir)


# === #556 S5 §6.2 — `all-budget-account-focus` ==============================
#
# `all-combined-decorated` stays untouched. It is the focused oracle for Claude
# decoration and combined withholding, and extending it would conflate two
# regression purposes.
#
# Every configured amount below is DISTINCT, and the two provider periods
# differ. That is the discriminator: equal values would let a provider swap, or
# a parent-for-child substitution, pass green.
#
#   Claude vendor-wide   $180.00  calendar-month
#   Claude per-account   $120.00  (populated, and deliberately NOT consumed —
#                                  Claude publishes no `account_scopes`, so the
#                                  status stays vendor-wide)
#   Codex vendor-wide     $90.00  calendar-week
#   Codex account A       $55.00
#   Codex account B       $35.00
_BAF_CLAUDE_BUDGET_USD = 180.0
_BAF_CLAUDE_ACCOUNT_BUDGET_USD = 120.0
_BAF_CODEX_BUDGET_USD = 90.0
_BAF_CODEX_ACCOUNT_A_BUDGET_USD = 55.0
_BAF_CODEX_ACCOUNT_B_BUDGET_USD = 35.0

_BAF_CLAUDE_ACCOUNT_KEY = "c" * 32
_BAF_CODEX_ACCOUNT_A = "d" * 32
_BAF_CODEX_ACCOUNT_B = "e" * 32

# ONE physical root shared by both accounts — the production shape, and the one
# in which a bare resource key is not an identity (#429 §3.1).
_BAF_CODEX_ROOT_KEY = "fixture-budget-focus-root"
_BAF_CODEX_ROOT_PATH = "/fake/codex-budget-focus"
_BAF_CODEX_CWD = "/fake/repos/budget-focus-codex"


def _seed_budget_focus_codex(
    cache_conn: sqlite3.Connection, as_of: dt.datetime,
) -> None:
    """Two real Codex accounts on one root, with DISTINCT spend and quota."""
    seed_codex_source_root(
        cache_conn,
        source_root_key=_BAF_CODEX_ROOT_KEY,
        canonical_root_path=_BAF_CODEX_ROOT_PATH,
    )
    # (account_key, session suffix, resets_at, used_percent, rows)
    #
    # Account A carries roughly twice B's spend and a later reset, so a read
    # that returned the wrong child — or the merged parent — is visible in
    # every scalar rather than only in the key.
    accounts = (
        (
            _BAF_CODEX_ACCOUNT_A, "a", as_of + dt.timedelta(days=4, hours=2), 62.0,
            (
                (_AC_CODEX_CYCLE_START + dt.timedelta(hours=9), 220_000, 70_000, 18_000, 4_000),
                (as_of - dt.timedelta(hours=3), 150_000, 48_000, 12_000, 2_500),
            ),
        ),
        (
            _BAF_CODEX_ACCOUNT_B, "b", as_of + dt.timedelta(days=2, hours=5), 27.0,
            (
                (_AC_CODEX_CYCLE_START + dt.timedelta(hours=11), 90_000, 28_000, 7_000, 1_500),
                (as_of - dt.timedelta(hours=2), 60_000, 19_000, 5_000, 900),
            ),
        ),
    )
    for index, (account_key, suffix, resets_at, used_percent, rows) in enumerate(
        accounts,
    ):
        session_id = f"fixture-budgetfocus-{suffix}-0000-000000000001"
        conversation_key = f"v1.{_BAF_CODEX_ROOT_KEY}.{session_id}"
        file_path = f"{_BAF_CODEX_ROOT_PATH}/sessions/{session_id}.jsonl"
        seed_codex_session_file(
            cache_conn,
            path=file_path,
            last_session_id=session_id,
            last_model="gpt-5",
            source_root_key=_BAF_CODEX_ROOT_KEY,
            last_native_thread_id=session_id,
            last_conversation_key=conversation_key,
        )
        seed_codex_conversation_thread(
            cache_conn,
            conversation_key=conversation_key,
            source_root_key=_BAF_CODEX_ROOT_KEY,
            native_thread_id=session_id,
            source_path=file_path,
            cwd=_BAF_CODEX_CWD,
        )
        for line_offset, (ts, inp, cached, out, reasoning) in enumerate(rows):
            seed_codex_session_entry(
                cache_conn,
                source_path=file_path,
                line_offset=line_offset,
                timestamp_utc=ts,
                session_id=session_id,
                model="gpt-5",
                input_tokens=inp,
                cached_input_tokens=cached,
                output_tokens=out,
                reasoning_output_tokens=reasoning,
                total_tokens=inp + out,
                source_root_key=_BAF_CODEX_ROOT_KEY,
                conversation_key=conversation_key,
                account_key=account_key,
            )
        seed_codex_quota_snapshot(
            cache_conn,
            source_root_key=_BAF_CODEX_ROOT_KEY,
            source_path=f"{_BAF_CODEX_ROOT_PATH}/sessions/weekly-quota-{suffix}.jsonl",
            line_offset=index,
            captured_at_utc=_iso(as_of - dt.timedelta(seconds=1200)),
            logical_limit_key="fixture-budget-focus-weekly",
            window_minutes=10_080,
            used_percent=used_percent,
            resets_at_utc=_iso(resets_at),
            limit_name="Fixture weekly quota",
            account_key=account_key,
        )
    bump_codex_physical_mutation_seq(cache_conn)


def build_all_budget_account_focus(as_of: dt.datetime) -> None:
    """Both provider budgets configured, and two focusable Codex accounts.

    #556 S5 §6.2. The scenario the budget comparison and the account focus are
    both read from: unequal amounts over different periods, a Claude
    `budget.accounts` map alongside a vendor-wide amount (so a test can prove
    the Claude status stays vendor-wide), and two real Codex accounts with
    their own budgets, spend and quota.
    """
    scenario_dir, app_dir = _scenario_dirs("all-budget-account-focus")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        _seed_all_combined_claude(stats_conn, cache_conn, as_of)
        _seed_budget_focus_codex(cache_conn, as_of)
        seed_account(
            stats_conn,
            account_key=_BAF_CLAUDE_ACCOUNT_KEY,
            provider="claude",
            natural_id="uuid-budgetfocus-claude",
            email="solo@example.com",
            label="solo",
            plan_type="max",
            label_source="user",
            first_seen_utc=_iso(_AC_CLAUDE_WEEK_START),
            last_seen_utc=_iso(as_of),
        )
        for key, natural, email, label, plan in (
            (_BAF_CODEX_ACCOUNT_A, "uuid-budgetfocus-codex-a",
             "codex-a@example.com", "codex-work", "pro"),
            (_BAF_CODEX_ACCOUNT_B, "uuid-budgetfocus-codex-b",
             "codex-b@example.com", "codex-home", "plus"),
        ):
            seed_account(
                stats_conn,
                account_key=key,
                provider="codex",
                natural_id=natural,
                email=email,
                label=label,
                plan_type=plan,
                label_source="user",
                first_seen_utc=_iso(_AC_CODEX_CYCLE_START),
                last_seen_utc=_iso(as_of),
            )
        _stamp_and_verify(stats_conn)
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()
    _reconcile_fixture_quota_projection(
        app_dir, root_key=_BAF_CODEX_ROOT_KEY, now=as_of,
    )
    config_path = app_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "budget": {
                    "weekly_usd": _BAF_CLAUDE_BUDGET_USD,
                    "period": "calendar-month",
                    "alerts_enabled": True,
                    "alert_thresholds": [90, 100],
                    "accounts": {
                        _BAF_CLAUDE_ACCOUNT_KEY: _BAF_CLAUDE_ACCOUNT_BUDGET_USD,
                    },
                    "codex": {
                        "amount_usd": _BAF_CODEX_BUDGET_USD,
                        "period": "calendar-week",
                        "alerts_enabled": True,
                        "alert_thresholds": [80, 100],
                        "accounts": {
                            _BAF_CODEX_ACCOUNT_A: _BAF_CODEX_ACCOUNT_A_BUDGET_USD,
                            _BAF_CODEX_ACCOUNT_B: _BAF_CODEX_ACCOUNT_B_BUDGET_USD,
                        },
                    },
                },
            },
            indent=2,
        )
        + "\n"
    )
    (scenario_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")
    _assert_ac_display_zone(scenario_dir)


# === #591 — decorated Codex fallback-window golden ==========================
#
# This scenario is intentionally narrower than `all-budget-account-focus`.
# Only account F owns a live weekly cycle. Account 9 retains an expired weekly
# observation and therefore falls back to the trailing native-cycle window;
# the unattributed sentinel does the same. Their accounting rows straddle the
# seven-day boundary so the golden distinguishes the bounded totals from the
# older rows that must remain excluded.
_CAF_ACCOUNT_LIVE = "f" * 32
_CAF_ACCOUNT_FALLBACK = "9" * 32
_CAF_ROOT_KEY = "fixture-codex-account-fallback-root"
_CAF_ROOT_PATH = "/fake/codex-account-fallback"
_CAF_CWD = "/fake/repos/codex-account-fallback"


def _seed_codex_account_fallback_file(
    cache_conn: sqlite3.Connection,
    *,
    account_key: str,
    suffix: str,
    rows: tuple[tuple[dt.datetime, int, int, int, int], ...],
) -> None:
    """Seed one rooted Codex session with explicit account ownership."""
    session_id = f"fixture-account-fallback-{suffix}-000000000001"
    conversation_key = f"v1.{_CAF_ROOT_KEY}.{session_id}"
    file_path = f"{_CAF_ROOT_PATH}/sessions/{session_id}.jsonl"
    seed_codex_session_file(
        cache_conn,
        path=file_path,
        last_session_id=session_id,
        last_model="gpt-5",
        source_root_key=_CAF_ROOT_KEY,
        last_native_thread_id=session_id,
        last_conversation_key=conversation_key,
    )
    seed_codex_conversation_thread(
        cache_conn,
        conversation_key=conversation_key,
        source_root_key=_CAF_ROOT_KEY,
        native_thread_id=session_id,
        source_path=file_path,
        cwd=_CAF_CWD,
    )
    for line_offset, (timestamp, inp, cached, out, reasoning) in enumerate(rows):
        seed_codex_session_entry(
            cache_conn,
            source_path=file_path,
            line_offset=line_offset,
            timestamp_utc=timestamp,
            session_id=session_id,
            model="gpt-5",
            input_tokens=inp,
            cached_input_tokens=cached,
            output_tokens=out,
            reasoning_output_tokens=reasoning,
            total_tokens=inp + out,
            source_root_key=_CAF_ROOT_KEY,
            conversation_key=conversation_key,
            account_key=account_key,
        )


def build_codex_account_fallback(as_of: dt.datetime) -> None:
    """Decorated Codex fixture with one live cycle and two fallback cards."""
    scenario_dir, app_dir = _scenario_dirs("codex-account-fallback")
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    try:
        seed_codex_source_root(
            cache_conn,
            source_root_key=_CAF_ROOT_KEY,
            canonical_root_path=_CAF_ROOT_PATH,
        )
        for key, natural, email, label, plan in (
            (_CAF_ACCOUNT_LIVE, "uuid-caf-live", "live@example.com", "live", "pro"),
            (_CAF_ACCOUNT_FALLBACK, "uuid-caf-fallback", "fallback@example.com", "fallback", "plus"),
        ):
            seed_account(
                stats_conn,
                account_key=key,
                provider="codex",
                natural_id=natural,
                email=email,
                label=label,
                plan_type=plan,
                label_source="user",
                first_seen_utc=_iso(as_of - dt.timedelta(days=20)),
                last_seen_utc=_iso(as_of),
            )

        _seed_codex_account_fallback_file(
            cache_conn,
            account_key=_CAF_ACCOUNT_LIVE,
            suffix="live",
            rows=((as_of - dt.timedelta(days=2), 100_000, 20_000, 10_000, 1_000),),
        )
        _seed_codex_account_fallback_file(
            cache_conn,
            account_key=_CAF_ACCOUNT_FALLBACK,
            suffix="fallback",
            rows=(
                (as_of - dt.timedelta(days=1), 200_000, 40_000, 20_000, 2_000),
                (as_of - dt.timedelta(days=8), 400_000, 80_000, 40_000, 4_000),
            ),
        )
        _seed_codex_account_fallback_file(
            cache_conn,
            account_key="unattributed",
            suffix="unattributed",
            rows=(
                (as_of - dt.timedelta(days=7), 300_000, 60_000, 30_000, 3_000),
                (
                    as_of - dt.timedelta(days=7, seconds=1),
                    500_000,
                    100_000,
                    50_000,
                    5_000,
                ),
            ),
        )

        # Account F is live. Account 9's retained weekly observation expired an
        # hour before the snapshot, so its card has no cycle and must disclose
        # the trailing-cycle fallback period.
        for line_offset, (account_key, reset, used_percent) in enumerate((
            (_CAF_ACCOUNT_LIVE, as_of + dt.timedelta(days=4), 61.0),
            (_CAF_ACCOUNT_FALLBACK, as_of - dt.timedelta(hours=1), 47.0),
        )):
            seed_codex_quota_snapshot(
                cache_conn,
                source_root_key=_CAF_ROOT_KEY,
                source_path=(
                    f"{_CAF_ROOT_PATH}/sessions/weekly-quota-{line_offset}.jsonl"
                ),
                line_offset=line_offset,
                captured_at_utc=_iso(as_of - dt.timedelta(minutes=20)),
                logical_limit_key="fixture-account-fallback-weekly",
                window_minutes=10_080,
                used_percent=used_percent,
                resets_at_utc=_iso(reset),
                limit_name="Fixture weekly quota",
                account_key=account_key,
            )
        bump_codex_physical_mutation_seq(cache_conn)
        _stamp_and_verify(stats_conn)
        stats_conn.commit()
        cache_conn.commit()
    finally:
        stats_conn.close()
        cache_conn.close()

    _reconcile_fixture_quota_projection(
        app_dir, root_key=_CAF_ROOT_KEY, now=as_of,
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
    "projected-alerts": (
        dt.datetime(2026, 4, 16, 14, 0, 0, tzinfo=dt.timezone.utc),
        build_projected_alerts,
    ),
    "command-secret": (
        dt.datetime(2026, 4, 16, 14, 0, 0, tzinfo=dt.timezone.utc),
        build_command_secret,
    ),
    "codex-cache-active": (
        dt.datetime(2026, 4, 20, 12, 0, 0, tzinfo=dt.timezone.utc),
        build_codex_cache_active,
    ),
    "codex-cache-idle": (
        dt.datetime(2026, 4, 20, 12, 0, 0, tzinfo=dt.timezone.utc),
        build_codex_cache_idle,
    ),
    "cache-report-qa": (
        dt.datetime(2026, 4, 16, 14, 0, 0, tzinfo=dt.timezone.utc),
        build_cache_report_qa,
    ),
    "all-combined": (
        dt.datetime(2026, 4, 16, 14, 0, 0, tzinfo=dt.timezone.utc),
        build_all_combined,
    ),
    "all-combined-decorated": (
        dt.datetime(2026, 4, 16, 14, 0, 0, tzinfo=dt.timezone.utc),
        build_all_combined_decorated,
    ),
    "all-budget-account-focus": (
        dt.datetime(2026, 4, 16, 14, 0, 0, tzinfo=dt.timezone.utc),
        build_all_budget_account_focus,
    ),
    "codex-account-fallback": (
        dt.datetime(2026, 4, 16, 14, 0, 0, tzinfo=dt.timezone.utc),
        build_codex_account_fallback,
    ),
}


if __name__ == "__main__":
    import argparse

    _parser = argparse.ArgumentParser(description=__doc__)
    _parser.add_argument(
        "--out", type=Path, default=None,
        help="Override the output directory (defaults to tests/fixtures/dashboard/). Harnesses build into a per-run scratch dir so the committed fixtures stay byte-stable and a test run leaves the tracked tree unchanged.",
    )
    _args = _parser.parse_args()
    if _args.out is not None:
        FIXTURES_DIR = _args.out
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for name, (as_of, fn) in SCENARIOS.items():
        fn(as_of)
        print(f"built: {name}")
    print(f"Built fixtures under {FIXTURES_DIR}")
