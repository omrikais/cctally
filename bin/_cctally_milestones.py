"""Weekly cost-snapshot persistence + percent/budget milestone DB layer.

Eager I/O sibling: bin/cctally loads this at startup and re-exports all 11
symbols onto the cctally namespace. Consumers reach them via the call-time
``c = _cctally()`` accessor (forecast/config/dashboard/sync_week/view_models),
``sys.modules["cctally"]`` shims (record/tui), and ``ns[...]`` in tests.

Holds (11): WeekCostResult, compute_week_cost, get_latest_cost_for_week,
insert_cost_snapshot, get_max_milestone_for_week, get_milestone_cost_for_week,
get_milestones_for_week, insert_percent_milestone, insert_budget_milestone,
_reconcile_budget_milestones_on_set, _reconcile_budget_on_config_write.

Accessor discipline (spec §2): _cctally_core kernel symbols + _budget_alerts_active
are honest-imported; _sum_cost_for_range / _resolve_current_budget_window are
reached via the call-time _cctally() accessor (ns-patchable). No _lib_ kernel.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from dataclasses import dataclass

from _cctally_core import (
    _budget_alerts_active,
    _canonicalize_optional_iso,
    _command_as_of,
    _get_latest_row_for_week,
    eprint,
    format_local_iso,
    now_utc_iso,
    open_db,
    parse_iso_datetime,
)


def _cctally():
    """Call-time accessor to the cctally module namespace (ns-patchable)."""
    return sys.modules["cctally"]


@dataclass
class WeekCostResult:
    week_start: dt.date
    week_end: dt.date
    start_iso: str
    end_iso: str
    cost_usd: float


def compute_week_cost(
    week_start: dt.date,
    week_end: dt.date,
    mode: str,
    offline: bool,
    project: str | None,
    start_iso_override: str | None = None,
    end_iso_override: str | None = None,
) -> WeekCostResult:
    # internal fallback: host-local intentional
    now_local = dt.datetime.now().astimezone()
    start_dt_override = (
        parse_iso_datetime(start_iso_override, "weekStartAt")
        if start_iso_override
        else None
    )
    end_dt_override = (
        parse_iso_datetime(end_iso_override, "weekEndAt")
        if end_iso_override
        else None
    )
    if start_dt_override is not None and end_dt_override is not None:
        if end_dt_override <= start_dt_override:
            raise ValueError("weekEndAt must be after weekStartAt")

    if start_dt_override is not None:
        start_iso = start_dt_override.isoformat(timespec="seconds")
    else:
        start_iso = format_local_iso(week_start, end_of_day=False)

    if end_dt_override is not None:
        in_current_window = (
            start_dt_override is not None
            and start_dt_override <= now_local < end_dt_override
        )
        end_iso = (
            now_local.isoformat(timespec="seconds")
            if in_current_window
            else end_dt_override.isoformat(timespec="seconds")
        )
    else:
        is_current_week = week_start <= now_local.date() <= week_end
        end_iso = (
            now_local.isoformat(timespec="seconds")
            if is_current_week
            else format_local_iso(week_end, end_of_day=True)
        )

    start_dt = parse_iso_datetime(start_iso, "start")
    end_dt = parse_iso_datetime(end_iso, "end")

    c = _cctally()
    cost = c._sum_cost_for_range(start_dt, end_dt, mode=mode, project=project)

    return WeekCostResult(
        week_start=week_start,
        week_end=week_end,
        start_iso=start_iso,
        end_iso=end_iso,
        cost_usd=cost,
    )


def get_latest_cost_for_week(conn: sqlite3.Connection, week_ref: WeekRef) -> sqlite3.Row | None:
    return _get_latest_row_for_week(conn, "weekly_cost_snapshots", week_ref)


def insert_cost_snapshot(
    conn: sqlite3.Connection,
    week_start: dt.date,
    week_end: dt.date,
    week_start_at: str | None,
    week_end_at: str | None,
    range_start_iso: str,
    range_end_iso: str,
    cost_usd: float,
    mode: str,
    project: str | None,
) -> int:
    start_at = _canonicalize_optional_iso(week_start_at, "weekStartAt")
    end_at = _canonicalize_optional_iso(week_end_at, "weekEndAt")
    range_start = parse_iso_datetime(range_start_iso, "rangeStartIso").isoformat(timespec="seconds")
    range_end = parse_iso_datetime(range_end_iso, "rangeEndIso").isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO weekly_cost_snapshots
        (
          captured_at_utc,
          week_start_date,
          week_end_date,
          week_start_at,
          week_end_at,
          range_start_iso,
          range_end_iso,
          cost_usd,
          mode,
          project
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_utc_iso(),
            week_start.isoformat(),
            week_end.isoformat(),
            start_at,
            end_at,
            range_start,
            range_end,
            cost_usd,
            mode,
            project,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_max_milestone_for_week(
    conn: sqlite3.Connection,
    week_start_date: str,
    *,
    reset_event_id: int = 0,
) -> int | None:
    """Return the highest percent_threshold recorded for a week's segment,
    or None.

    ``reset_event_id`` (v1.7.2): default 0 (= pre-credit / no-event
    sentinel) preserves legacy behavior on un-credited weeks. When an
    in-place credit lifts a week into a new segment, callers pass the
    segment id so the segment's threshold ledger is independent of the
    pre-credit one — the post-credit 1% / 2% / 3% milestones fire even
    if the pre-credit segment already crossed those thresholds.
    """
    row = conn.execute(
        """
        SELECT MAX(percent_threshold) AS max_pct
        FROM percent_milestones
        WHERE week_start_date = ?
          AND reset_event_id = ?
        """,
        (week_start_date, reset_event_id),
    ).fetchone()
    if row and row["max_pct"] is not None:
        return int(row["max_pct"])
    return None


def get_milestone_cost_for_week(
    conn: sqlite3.Connection,
    week_start_date: str,
    percent_threshold: int,
    *,
    reset_event_id: int = 0,
) -> float | None:
    """Return the cumulative_cost_usd for a specific (week, threshold,
    segment), or None.

    ``reset_event_id`` (v1.7.2): segment-aware lookup. Default 0 preserves
    legacy behavior. Used by ``maybe_record_milestone`` to compute the
    marginal cost between consecutive thresholds inside the SAME segment
    — without the filter, the post-credit threshold-3 row would compute
    its marginal against the pre-credit threshold-2 cost (wrong segment).
    """
    row = conn.execute(
        """
        SELECT cumulative_cost_usd
        FROM percent_milestones
        WHERE week_start_date = ?
          AND percent_threshold = ?
          AND reset_event_id = ?
        """,
        (week_start_date, percent_threshold, reset_event_id),
    ).fetchone()
    if row:
        return float(row["cumulative_cost_usd"])
    return None


def get_milestones_for_week(
    conn: sqlite3.Connection,
    week_start_date: str,
) -> list[sqlite3.Row]:
    """Return all milestones for a week, ordered by threshold ascending."""
    return conn.execute(
        """
        SELECT *
        FROM percent_milestones
        WHERE week_start_date = ?
        ORDER BY percent_threshold ASC
        """,
        (week_start_date,),
    ).fetchall()


def insert_percent_milestone(
    conn: sqlite3.Connection,
    week_start_date: str,
    week_end_date: str,
    week_start_at: str | None,
    week_end_at: str | None,
    percent_threshold: int,
    cumulative_cost_usd: float,
    marginal_cost_usd: float | None,
    usage_snapshot_id: int,
    cost_snapshot_id: int,
    five_hour_percent_at_crossing: float | None = None,
    *,
    commit: bool = True,
    reset_event_id: int = 0,
) -> int:
    """Insert a percent_milestones row idempotently.

    Returns the SQLite rowcount: 1 on a genuinely new crossing, 0 if a row
    for (week_start_date, percent_threshold, reset_event_id) already exists.
    Race-safe under concurrent record-usage instances — aligns with the
    existing 5h-milestone INSERT OR IGNORE pattern (see five_hour_milestones
    write path).

    ``reset_event_id`` (v1.7.2 segment column, migration 005): defaults to
    ``0`` (= pre-credit / no-event sentinel). When an in-place credit fires
    for the current week, the caller (``maybe_record_milestone``) resolves
    the active segment from ``week_reset_events`` and passes it in so
    post-credit threshold crossings land as a SEPARATE row from any
    pre-credit one at the same (week, threshold). The UNIQUE constraint
    is on the 3-tuple, so (week=W, threshold=T, segment=0) and (W, T,
    event_id) coexist.

    Callers that need the row id MUST follow up with an explicit
    `SELECT id FROM percent_milestones WHERE week_start_date=? AND
    percent_threshold=? AND reset_event_id=?` query — `lastrowid` is
    unreliable when the row is the silent-duplicate target.

    ``commit=False`` skips the inner ``conn.commit()`` so the caller can
    bundle the INSERT with a follow-up ``alerted_at`` UPDATE in a single
    transaction (set-then-dispatch atomicity, spec §3.2). Used by
    ``record_percent_milestone_if_crossed`` to mirror the 5h path's
    single-commit pattern; without it, a crash between INSERT and UPDATE
    permanently strands ``alerted_at`` NULL because the next call's
    ``INSERT OR IGNORE`` returns rowcount==0 and the dispatch guard
    ``if inserted == 1`` skips re-firing.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO percent_milestones
        (
          captured_at_utc,
          week_start_date,
          week_end_date,
          week_start_at,
          week_end_at,
          percent_threshold,
          cumulative_cost_usd,
          marginal_cost_usd,
          usage_snapshot_id,
          cost_snapshot_id,
          five_hour_percent_at_crossing,
          reset_event_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_utc_iso(),
            week_start_date,
            week_end_date,
            week_start_at,
            week_end_at,
            percent_threshold,
            cumulative_cost_usd,
            marginal_cost_usd,
            usage_snapshot_id,
            cost_snapshot_id,
            five_hour_percent_at_crossing,
            reset_event_id,
        ),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount)


def insert_budget_milestone(
    conn: sqlite3.Connection,
    *,
    week_start_at: str,
    period: "str | None" = None,
    threshold: int,
    budget_usd: float,
    spent_usd: float,
    consumption_pct: float,
    commit: bool = True,
) -> int:
    """INSERT OR IGNORE a budget threshold crossing. Returns ``cur.rowcount``
    (1 = genuinely new crossing, 0 = INSERT OR IGNORE no-op on a pre-existing
    ``(week_start_at, period, threshold)`` row).

    Mirrors :func:`insert_percent_milestone`'s rowcount contract so the
    alert-fire predicate (`if inserted == 1`) is race-safe without a
    follow-up SELECT. ``period`` (#137) is the configured period noun at
    crossing ('calendar-week'|'calendar-month'|'subscription-week'); it
    discriminates the UNIQUE key so calendar-week and calendar-month windows
    that share a start instant don't collide. A NULL ``period`` is the pre-011
    "unknown" sentinel (only seeded migration rows carry it). ``alerted_at`` is
    left NULL — the caller stamps it in the SAME transaction BEFORE dispatching
    (set-then-dispatch invariant, CLAUDE.md Alerts gotcha). ``commit=False``
    lets the caller bundle the INSERT with the follow-up ``alerted_at`` UPDATE
    in one transaction so a crash between them can't strand ``alerted_at`` NULL
    forever.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO budget_milestones "
        "(week_start_at, period, threshold, budget_usd, spent_usd, "
        " consumption_pct, crossed_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            week_start_at,
            period,
            int(threshold),
            float(budget_usd),
            float(spent_usd),
            float(consumption_pct),
            now_utc_iso(),
        ),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount)


def insert_codex_budget_milestone(
    conn: sqlite3.Connection,
    *,
    period_start_at: str,
    period: "str | None" = None,
    threshold: int,
    budget_usd: float,
    spent_usd: float,
    consumption_pct: float,
    commit: bool = True,
) -> int:
    """INSERT OR IGNORE a Codex budget threshold crossing. Returns
    ``cur.rowcount`` (1 = genuinely new crossing, 0 = INSERT OR IGNORE no-op on a
    pre-existing ``(period_start_at, period, threshold)`` row).

    Mirrors :func:`insert_budget_milestone` byte-for-byte but keyed on
    ``period_start_at`` (the resolved CALENDAR-period window start instant) in
    place of ``week_start_at`` — Codex has no Anthropic subscription week, so the
    budget runs over a calendar period (spec §6). ``period`` (#137) is the
    configured Codex period noun at crossing ('calendar-week'|'calendar-month');
    NULL is the pre-011 unknown sentinel. Same rowcount contract so the
    alert-fire predicate (`if inserted == 1`) is race-safe without a follow-up
    SELECT. ``alerted_at`` is left NULL — the caller stamps it in the SAME
    transaction BEFORE dispatching (set-then-dispatch invariant, CLAUDE.md
    Alerts gotcha). ``commit=False`` lets the caller bundle the INSERT with the
    follow-up ``alerted_at`` UPDATE in one transaction so a crash between them
    can't strand ``alerted_at`` NULL forever.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO codex_budget_milestones "
        "(period_start_at, period, threshold, budget_usd, spent_usd, "
        " consumption_pct, crossed_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            period_start_at,
            period,
            int(threshold),
            float(budget_usd),
            float(spent_usd),
            float(consumption_pct),
            now_utc_iso(),
        ),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount)


def insert_project_budget_milestone(
    conn: sqlite3.Connection,
    *,
    week_start_at: str,
    project_key: str,
    threshold: int,
    budget_usd: float,
    spent_usd: float,
    consumption_pct: float,
    commit: bool = True,
) -> int:
    """INSERT OR IGNORE a per-project budget threshold crossing. Returns
    ``cur.rowcount`` (1 = genuinely new crossing, 0 = INSERT OR IGNORE no-op on a
    pre-existing ``(week_start_at, project_key, threshold)`` row).

    Mirrors :func:`insert_budget_milestone` EXACTLY, with ``project_key`` added
    as the per-project dimension of the UNIQUE dedup key (spec §5.1) — each
    project crosses each threshold once per week, independently. The rowcount
    contract matches :func:`insert_percent_milestone` so the alert-fire predicate
    (`if inserted == 1`) is race-safe without a follow-up SELECT. ``alerted_at``
    is left NULL — the caller stamps it in the SAME transaction BEFORE
    dispatching (set-then-dispatch invariant, CLAUDE.md Alerts gotcha).
    ``commit=False`` lets the caller bundle the INSERT with the follow-up
    ``alerted_at`` UPDATE in one transaction so a crash between them can't strand
    ``alerted_at`` NULL forever.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO project_budget_milestones "
        "(week_start_at, project_key, threshold, budget_usd, spent_usd, "
        " consumption_pct, crossed_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            week_start_at,
            str(project_key),
            int(threshold),
            float(budget_usd),
            float(spent_usd),
            float(consumption_pct),
            now_utc_iso(),
        ),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount)


def insert_projected_milestone(
    conn: sqlite3.Connection,
    *,
    week_start_at: str,
    period: "str | None" = None,
    metric: str,
    threshold: int,
    projected_value: float,
    denominator: float,
    commit: bool = True,
) -> int:
    """INSERT OR IGNORE a projected-pace crossing. Returns ``cur.rowcount``
    (1 = genuinely new crossing, 0 = INSERT OR IGNORE no-op on a pre-existing
    ``(week_start_at, period, metric, threshold)`` row).

    Mirrors :func:`insert_budget_milestone`'s rowcount contract so the
    alert-fire predicate (`if inserted == 1`) is race-safe without a follow-up
    SELECT. ``period`` (#137) is the configured period at crossing — for the
    ``weekly_pct`` leg it is 'subscription-week', for ``budget_usd`` the Claude
    configured period, for ``codex_budget_usd`` the Codex configured period;
    NULL is the pre-011 unknown sentinel. ``alerted_at`` is left NULL — the
    caller stamps it in the SAME transaction BEFORE dispatching (set-then-
    dispatch invariant, CLAUDE.md Alerts gotcha). ``commit=False`` lets the
    caller bundle the INSERT with the follow-up ``alerted_at`` UPDATE in one
    transaction so a crash between them can't strand ``alerted_at`` NULL forever.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO projected_milestones "
        "(week_start_at, period, metric, threshold, projected_value, "
        " denominator, crossed_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            week_start_at,
            period,
            str(metric),
            int(threshold),
            float(projected_value),
            float(denominator),
            now_utc_iso(),
        ),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount)


def _projected_levels_already_latched(
    conn: sqlite3.Connection,
    *,
    week_start_at: str,
    period: "str | None" = None,
    metric: str,
    levels: "tuple[int, ...]",
) -> bool:
    """True iff EVERY level in ``levels`` already has a row for
    ``(week_start_at, period, metric)``.

    Cheap indexed SELECT used as the pre-probe gate BEFORE any projection math
    / cost work ([Pre-probe before sync_cache]). Empty ``levels`` → True
    (nothing owed). When False, at least one level is still un-recorded and the
    caller must do the projection. Mirrors the per-week pre-probe SELECT in
    :func:`maybe_record_budget_milestone`.

    The ``period IS NULL`` arm (#137) means a pre-011 NULL-period row for the
    current window counts as latched — so an upgrading user never re-fires a
    spurious projected alert against a historical crossing.
    """
    if not levels:
        return True
    rows = conn.execute(
        "SELECT threshold FROM projected_milestones "
        "WHERE week_start_at = ? AND (period = ? OR period IS NULL) "
        "  AND metric = ?",
        (week_start_at, period, str(metric)),
    ).fetchall()
    have = {int(r[0]) for r in rows}
    return all(int(level) in have for level in levels)


def _resolve_claude_budget_window(conn, now_utc, *, period, config, tz):
    """Resolve the Claude budget's ``(start_utc, end_utc)`` window for the
    configured ``period`` (calendar-period-codex-budgets generalization, spec
    §6). Subscription-week → the existing ``_resolve_current_budget_window``
    (snapshot-anchored; may return ``None`` when no usage snapshot has landed
    yet). Calendar period → the pure ``_resolve_calendar_window`` (derived purely
    from ``now`` + the period; NEVER ``None``). The dedup key column stays
    ``week_start_at`` — for a calendar period it carries the resolved PERIOD-start
    instant (a back-compat misnomer)."""
    c = _cctally()
    if period == "subscription-week":
        return c._resolve_current_budget_window(conn, now_utc)
    return c._resolve_calendar_window(period, now_utc, config, tz)


def _reconcile_budget_milestones_on_set(
    conn, *, target, thresholds, now_utc, period="subscription-week",
    config=None, tz=None,
):
    """Forward-only-from-set reconcile (spec §5): on `budget set`, every
    threshold ALREADY crossed for the current week/period is recorded with
    ``alerted_at`` SET but WITHOUT dispatch — so setting a budget when you're
    already at 95% does NOT instant-popup. Thresholds not yet crossed get NO
    row, so they fire later via :func:`maybe_record_budget_milestone`.

    A mid-week target change re-runs this; thresholds already alerted stay
    deduped via UNIQUE(week_start_at, period, threshold) + the ``alerted_at IS
    NULL`` guard on the UPDATE (so an existing alerted row is never re-stamped).

    ``period`` defaults to subscription-week (byte-stable legacy behavior); a
    calendar period resolves the window from ``now`` + the period instead of the
    snapshot anchor (calendar-period-codex-budgets generalization, spec §6).
    """
    c = _cctally()
    window = _resolve_claude_budget_window(
        conn, now_utc, period=period, config=config, tz=tz
    )
    if window is None:
        return
    week_start_at, _week_end_at = window
    week_key = week_start_at.isoformat(timespec="seconds")
    spent = c._sum_cost_for_range(week_start_at, now_utc, mode="auto")
    # target > 0 guaranteed by the caller (_cmd_budget_set passes the validated
    # weekly_usd); the else is belt-and-suspenders.
    consumption_pct = (spent / target * 100.0) if target > 0 else 0.0
    for t in sorted(thresholds):
        if consumption_pct + 1e-9 >= t:
            insert_budget_milestone(
                conn,
                week_start_at=week_key,
                period=period,
                threshold=t,
                budget_usd=target,
                spent_usd=spent,
                consumption_pct=consumption_pct,
                commit=False,
            )
            # alerted_at UPDATE keys on the CONCRETE period (not the wildcard):
            # only the row we just inserted under `period` is stamped, never a
            # pre-011 NULL-period sibling (#137).
            conn.execute(
                "UPDATE budget_milestones SET alerted_at = ? "
                "WHERE week_start_at = ? AND period = ? AND threshold = ? "
                "  AND alerted_at IS NULL",
                (now_utc_iso(), week_key, period, t),
            )
    conn.commit()


def _resolve_codex_budget_period_window(period, now_utc, config, tz):
    """Resolve the Codex budget's ``(start_utc, end_utc)`` calendar window via
    the forecast layer's ``_resolve_calendar_window`` (which routes to the pure
    ``calendar_month_window`` / ``calendar_week_window`` kernel functions). The
    Codex axis NEVER touches ``weekly_usage_snapshots`` (no Anthropic week), so
    the window comes purely from ``now`` + the configured calendar period.
    ``period`` is canonical (calendar-week / calendar-month — the validator
    forbids subscription-week for Codex)."""
    c = _cctally()
    return c._resolve_calendar_window(period, now_utc, config, tz)


def _codex_budget_crossings(
    conn, *, period_key, period=None, thresholds, target, spent, now_utc
):
    """Shared INSERT-and-arm core for the Codex budget axis: for every
    STILL-pending threshold that's been crossed at ``spent``, ``INSERT OR
    IGNORE`` a milestone (commit=False) and — on the genuine-new-crossing winner
    (rowcount==1) — stamp ``alerted_at`` in the SAME transaction (set-then-
    dispatch), returning the list of crossings the caller must dispatch.

    Pure of config/window resolution: both firing sites (record-usage +
    opportunistic ``cctally budget``) feed the already-resolved ``period_key`` /
    ``target`` / ``spent`` here, so the crossing arithmetic + the set-then-
    dispatch invariant live in ONE place (plan §3.6 "one shared helper"). Does
    NOT commit — the caller owns the single durable commit that bundles every
    INSERT with its ``alerted_at`` UPDATE. Applies the +1e-9 float-floor snap
    (CLAUDE.md gotcha). Returns ``[(threshold, crossed_at, spent, target,
    consumption_pct), ...]`` for the rowcount==1 winners only.

    Forward-only / fire-once is enforced by INSERT OR IGNORE's rowcount on the
    UNIQUE(period_start_at, period, threshold) key; a racing record-usage
    instance OR an already-recorded threshold gets rowcount==0 and is skipped
    ([Dedup mustn't gate side effects])."""
    consumption_pct = (spent / target * 100.0) if target > 0 else 0.0
    fired: "list" = []
    for t in sorted(thresholds):
        if consumption_pct + 1e-9 >= t:
            inserted = insert_codex_budget_milestone(
                conn,
                period_start_at=period_key,
                period=period,
                threshold=t,
                budget_usd=target,
                spent_usd=spent,
                consumption_pct=consumption_pct,
                commit=False,
            )
            if inserted == 1:
                crossed_at = now_utc_iso()
                # alerted_at UPDATE keys on the CONCRETE period (#137): only the
                # row just inserted under `period` is stamped, never a pre-011
                # NULL-period sibling.
                conn.execute(
                    "UPDATE codex_budget_milestones SET alerted_at = ? "
                    "WHERE period_start_at = ? AND period = ? AND threshold = ? "
                    "  AND alerted_at IS NULL",
                    (crossed_at, period_key, period, t),
                )
                fired.append((t, crossed_at, spent, target, consumption_pct))
    return fired


def _reconcile_codex_budget_milestones_on_set(
    conn, *, target, thresholds, now_utc, period, config, tz
):
    """Forward-only-from-set reconcile for the Codex budget axis (spec §6),
    mirroring :func:`_reconcile_budget_milestones_on_set` but keyed on the
    resolved CALENDAR period window instead of the subscription week.

    On a Codex `budget set` (or `config set budget.codex`), every threshold
    ALREADY crossed for the current period is recorded with ``alerted_at`` SET
    but WITHOUT dispatch — so setting a Codex budget mid-month while already over
    does NOT instant-popup; a mid-period amount change never re-alerts an
    already-fired threshold (deduped via UNIQUE(period_start_at, period,
    threshold) + the ``alerted_at IS NULL`` UPDATE guard). Thresholds not yet
    crossed get NO row, so they fire later via
    :func:`maybe_record_codex_budget_milestone`."""
    c = _cctally()
    start_at, _end_at = _resolve_codex_budget_period_window(
        period, now_utc, config, tz
    )
    period_key = start_at.isoformat(timespec="seconds")
    spent = c._sum_codex_cost_for_range(start_at, now_utc)
    consumption_pct = (spent / target * 100.0) if target > 0 else 0.0
    for t in sorted(thresholds):
        if consumption_pct + 1e-9 >= t:
            insert_codex_budget_milestone(
                conn,
                period_start_at=period_key,
                period=period,
                threshold=t,
                budget_usd=target,
                spent_usd=spent,
                consumption_pct=consumption_pct,
                commit=False,
            )
            # alerted_at UPDATE keys on the CONCRETE period (#137).
            conn.execute(
                "UPDATE codex_budget_milestones SET alerted_at = ? "
                "WHERE period_start_at = ? AND period = ? AND threshold = ? "
                "  AND alerted_at IS NULL",
                (now_utc_iso(), period_key, period, t),
            )
    conn.commit()


def _reconcile_codex_budget_on_config_write(validated_budget):
    """Forward-only reconcile shared by the Codex-budget config write paths
    (`budget set --vendor codex`, `config set budget.codex`). Gated +
    best-effort: a Codex budget with alerts off or no thresholds records
    nothing; a stats.db failure never fails the write. Runs OUTSIDE any
    config_writer_lock (open_db has its own locking). Mirrors
    :func:`_reconcile_budget_on_config_write`."""
    codex = (validated_budget or {}).get("codex")
    if not codex:
        return
    thresholds = codex.get("alert_thresholds") or []
    if not (codex.get("alerts_enabled") and codex.get("amount_usd") and thresholds):
        return
    c = _cctally()
    try:
        import argparse
        config = c.load_config()
        tz = c.resolve_display_tz(argparse.Namespace(tz=None), config)
        conn = open_db()
        try:
            _reconcile_codex_budget_milestones_on_set(
                conn,
                target=codex["amount_usd"],
                thresholds=thresholds,
                now_utc=_command_as_of(),
                period=codex["period"],
                config=config,
                tz=tz,
            )
        finally:
            conn.close()
    except Exception as exc:  # best-effort; never fail the write
        eprint(f"[codex-budget-milestone] reconcile on set failed: {exc}")


def _reconcile_budget_on_config_write(validated_budget):
    """Forward-only reconcile shared by all three budget-config write
    paths (`budget set`, `config set budget.*`, dashboard POST
    /api/settings). Gated + best-effort: a budget with alerts off or no
    thresholds records nothing; a stats.db failure never fails the write.
    Runs OUTSIDE any config_writer_lock (open_db has its own locking)."""
    thresholds = validated_budget.get("alert_thresholds") or []
    if not (_budget_alerts_active(validated_budget) and thresholds):
        return
    c = _cctally()
    period = validated_budget.get("period", "subscription-week")
    try:
        import argparse
        config = c.load_config()
        tz = c.resolve_display_tz(argparse.Namespace(tz=None), config)
        conn = open_db()
        try:
            _reconcile_budget_milestones_on_set(
                conn,
                target=validated_budget["weekly_usd"],
                thresholds=thresholds,
                now_utc=_command_as_of(),
                period=period,
                config=config,
                tz=tz,
            )
        finally:
            conn.close()
    except Exception as exc:  # best-effort; never fail the write
        eprint(f"[budget-milestone] reconcile on set failed: {exc}")


def _project_crossings(items, thresholds, by_proj):
    """Yield ``(project_key, threshold, spent, target, consumption_pct)`` for
    every crossed (project, threshold) pair (#130). Shared by the firing path
    (record-usage) and the reconcile path (config write) so they differ ONLY in
    the dispatch tail. Pure arithmetic — no DB, no I/O. Applies the +1e-9
    float-floor snap (CLAUDE.md gotcha) and yields thresholds in sorted order.
    ``items`` is an iterable of ``(project_key, target)`` pairs."""
    sorted_thresholds = sorted(thresholds)
    for project_key, target in items:
        spent = float(by_proj.get(project_key, 0.0))
        target = float(target)
        consumption_pct = (spent / target * 100.0) if target > 0 else 0.0
        for t in sorted_thresholds:
            if consumption_pct + 1e-9 >= t:
                yield (project_key, t, spent, target, consumption_pct)


def _reconcile_project_budget_milestones_on_write(
    validated_budget, touched_projects=None
):
    """Forward-only-from-write reconcile for PER-PROJECT budgets (spec §6.8).

    Shared by all four per-project write surfaces: ``budget set --project`` /
    ``unset --project`` (call sites in ``_cmd_budget_set_project`` /
    ``_cmd_budget_unset_project``), ``config set budget.projects`` /
    ``config set budget.project_alerts_enabled`` (call site in
    ``_cmd_config_set``), and the dashboard ``project_alerts_enabled`` toggle
    (POST /api/settings, Task 4). Mirrors :func:`_reconcile_budget_on_config_write`.

    Mechanic: for each configured project, compute current-week spend (the
    shared ``_sum_cost_by_project`` scan), and for each ALREADY-crossed
    ``(project, threshold)`` ``INSERT OR IGNORE`` a milestone with ``alerted_at``
    stamped and **NO dispatch** — so setting a project budget mid-week (already
    over) records the crossed thresholds as already-alerted without an
    instant-popup; only LATER crossings fire via
    :func:`maybe_record_project_budget_milestone`.

    Dedup via ``UNIQUE(week_start_at, project_key, threshold)`` + the
    ``alerted_at IS NULL`` UPDATE guard, so a mid-week TARGET change never
    re-stamps an already-alerted row (mirrors the global reconcile's
    target-change semantics).

    Gated: runs ONLY when per-project alerts are active (``projects`` non-empty
    **and** ``project_alerts_enabled`` **and** ``alert_thresholds`` non-empty);
    else records nothing. Best-effort — a stats.db failure never fails the config
    write. Runs OUTSIDE any ``config_writer_lock`` (``open_db`` has its own
    locking).

    ``touched_projects``: when not ``None``, reconcile ONLY these project keys.
    The single-project CLI writes (``budget set/unset --project``) pass
    ``{root}`` so touching project A never latches a sibling project B's
    already-crossed-but-not-yet-dispatched threshold — which would permanently
    suppress B's real alert. ``None`` (config-set / dashboard toggle / wholesale
    ``budget.projects`` set) reconciles every configured project: the intended
    "axis enabled / map redefined → suppress the retroactive storm for all
    currently-over projects" semantics.
    """
    projects = (validated_budget or {}).get("projects") or {}
    thresholds = validated_budget.get("alert_thresholds") or []
    if not (
        projects
        and validated_budget.get("project_alerts_enabled")
        and thresholds
    ):
        return
    c = _cctally()
    try:
        conn = open_db()
        try:
            now_utc = _command_as_of()
            window = c._resolve_current_budget_window(conn, now_utc)
            if window is None:
                return
            week_start_at, _week_end_at = window
            week_key = week_start_at.isoformat(timespec="seconds")
            by_proj = c._sum_cost_by_project(week_start_at, now_utc, mode="auto")
            items = (
                projects.items()
                if touched_projects is None
                else [
                    (k, v) for k, v in projects.items()
                    if k in touched_projects
                ]
            )
            # Same crossing arithmetic as firing, via the shared generator
            # (#130). Reconcile differs ONLY in the tail: UPDATE alerted_at with
            # NO dispatch (retroactive-storm suppression). `items` already
            # honors touched_projects filtering above.
            for project_key, t, spent, target, consumption_pct in c._project_crossings(
                items, thresholds, by_proj
            ):
                insert_project_budget_milestone(
                    conn,
                    week_start_at=week_key,
                    project_key=project_key,
                    threshold=t,
                    budget_usd=target,
                    spent_usd=spent,
                    consumption_pct=consumption_pct,
                    commit=False,
                )
                conn.execute(
                    "UPDATE project_budget_milestones SET alerted_at = ? "
                    "WHERE week_start_at = ? AND project_key = ? "
                    "  AND threshold = ? AND alerted_at IS NULL",
                    (now_utc_iso(), week_key, project_key, t),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # best-effort; never fail the write
        eprint(
            f"[project-budget-milestone] reconcile on write failed: {exc}"
        )
