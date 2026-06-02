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
    threshold: int,
    budget_usd: float,
    spent_usd: float,
    consumption_pct: float,
    commit: bool = True,
) -> int:
    """INSERT OR IGNORE a budget threshold crossing. Returns ``cur.rowcount``
    (1 = genuinely new crossing, 0 = INSERT OR IGNORE no-op on a pre-existing
    ``(week_start_at, threshold)`` row).

    Mirrors :func:`insert_percent_milestone`'s rowcount contract so the
    alert-fire predicate (`if inserted == 1`) is race-safe without a
    follow-up SELECT. ``alerted_at`` is left NULL — the caller stamps it in
    the SAME transaction BEFORE dispatching (set-then-dispatch invariant,
    CLAUDE.md Alerts gotcha). ``commit=False`` lets the caller bundle the
    INSERT with the follow-up ``alerted_at`` UPDATE in one transaction so a
    crash between them can't strand ``alerted_at`` NULL forever.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO budget_milestones "
        "(week_start_at, threshold, budget_usd, spent_usd, consumption_pct, "
        " crossed_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            week_start_at,
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
    metric: str,
    threshold: int,
    projected_value: float,
    denominator: float,
    commit: bool = True,
) -> int:
    """INSERT OR IGNORE a projected-pace crossing. Returns ``cur.rowcount``
    (1 = genuinely new crossing, 0 = INSERT OR IGNORE no-op on a pre-existing
    ``(week_start_at, metric, threshold)`` row).

    Mirrors :func:`insert_budget_milestone`'s rowcount contract so the
    alert-fire predicate (`if inserted == 1`) is race-safe without a follow-up
    SELECT. ``alerted_at`` is left NULL — the caller stamps it in the SAME
    transaction BEFORE dispatching (set-then-dispatch invariant, CLAUDE.md
    Alerts gotcha). ``commit=False`` lets the caller bundle the INSERT with the
    follow-up ``alerted_at`` UPDATE in one transaction so a crash between them
    can't strand ``alerted_at`` NULL forever.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO projected_milestones "
        "(week_start_at, metric, threshold, projected_value, denominator, "
        " crossed_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            week_start_at,
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
    metric: str,
    levels: "tuple[int, ...]",
) -> bool:
    """True iff EVERY level in ``levels`` already has a row for
    ``(week_start_at, metric)``.

    Cheap indexed SELECT used as the pre-probe gate BEFORE any projection math
    / cost work ([Pre-probe before sync_cache]). Empty ``levels`` → True
    (nothing owed). When False, at least one level is still un-recorded and the
    caller must do the projection. Mirrors the per-week pre-probe SELECT in
    :func:`maybe_record_budget_milestone`.
    """
    if not levels:
        return True
    rows = conn.execute(
        "SELECT threshold FROM projected_milestones "
        "WHERE week_start_at = ? AND metric = ?",
        (week_start_at, str(metric)),
    ).fetchall()
    have = {int(r[0]) for r in rows}
    return all(int(level) in have for level in levels)


def _reconcile_budget_milestones_on_set(conn, *, target, thresholds, now_utc):
    """Forward-only-from-set reconcile (spec §5): on `budget set`, every
    threshold ALREADY crossed for the current week is recorded with
    ``alerted_at`` SET but WITHOUT dispatch — so setting a budget when you're
    already at 95% does NOT instant-popup. Thresholds not yet crossed get NO
    row, so they fire later via :func:`maybe_record_budget_milestone`.

    A mid-week target change re-runs this; thresholds already alerted stay
    deduped via UNIQUE(week_start_at, threshold) + the ``alerted_at IS NULL``
    guard on the UPDATE (so an existing alerted row is never re-stamped).
    """
    c = _cctally()
    window = c._resolve_current_budget_window(conn, now_utc)
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
                threshold=t,
                budget_usd=target,
                spent_usd=spent,
                consumption_pct=consumption_pct,
                commit=False,
            )
            conn.execute(
                "UPDATE budget_milestones SET alerted_at = ? "
                "WHERE week_start_at = ? AND threshold = ? AND alerted_at IS NULL",
                (now_utc_iso(), week_key, t),
            )
    conn.commit()


def _reconcile_budget_on_config_write(validated_budget):
    """Forward-only reconcile shared by all three budget-config write
    paths (`budget set`, `config set budget.*`, dashboard POST
    /api/settings). Gated + best-effort: a budget with alerts off or no
    thresholds records nothing; a stats.db failure never fails the write.
    Runs OUTSIDE any config_writer_lock (open_db has its own locking)."""
    thresholds = validated_budget.get("alert_thresholds") or []
    if not (_budget_alerts_active(validated_budget) and thresholds):
        return
    try:
        conn = open_db()
        try:
            _reconcile_budget_milestones_on_set(
                conn,
                target=validated_budget["weekly_usd"],
                thresholds=thresholds,
                now_utc=_command_as_of(),
            )
        finally:
            conn.close()
    except Exception as exc:  # best-effort; never fail the write
        eprint(f"[budget-milestone] reconcile on set failed: {exc}")


def _reconcile_project_budget_milestones_on_write(validated_budget):
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
            for project_key, target in projects.items():
                spent = float(by_proj.get(project_key, 0.0))
                target = float(target)
                consumption_pct = (
                    (spent / target * 100.0) if target > 0 else 0.0
                )
                for t in sorted(thresholds):
                    if consumption_pct + 1e-9 >= t:
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
