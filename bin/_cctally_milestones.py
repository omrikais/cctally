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
    _as_of_or_command,
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
    *,
    account_key: "str | None" = None,
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
    # #341 P2-CQ2: scope the cost sum to the account the snapshot is stamped for
    # (None = merged / byte-identical) so per-account weekly_cost_snapshots + the
    # milestone $/1% cumulative cost carry genuinely per-account cost.
    cost = c._sum_cost_for_range(
        start_dt, end_dt, mode=mode, project=project, account_key=account_key)

    return WeekCostResult(
        week_start=week_start,
        week_end=week_end,
        start_iso=start_iso,
        end_iso=end_iso,
        cost_usd=cost,
    )


def get_latest_cost_for_week(
    conn: sqlite3.Connection, week_ref: WeekRef, *,
    account_key: "str | None" = None,
) -> sqlite3.Row | None:
    return _get_latest_row_for_week(
        conn, "weekly_cost_snapshots", week_ref, account_key=account_key)


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
    *,
    commit: bool = True,
    as_of: "str | None" = None,
    journal: "tuple | None" = None,
    account_key: str = "unattributed",
) -> int:
    """Insert a ``weekly_cost_snapshots`` row and return its rowid.

    ``account_key`` (#341): the account dimension of the write. Default
    ``"unattributed"`` mirrors the schema DEFAULT (rev 4.1 defensive backstop);
    per-account ``sync-week`` materialization (Step 9 / Task 3) passes the
    resolved account explicitly.

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    ``commit=False`` skips the inner ``conn.commit()`` so the ingester can bundle
    this insert into its single cycle transaction; ``as_of`` (ISO-Z) overrides
    the ``captured_at_utc`` wall-clock stamp with the record's capture time.
    Both defaults keep legacy callers bit-identical.

    Design A (DB journal redesign §5.3, Model-A ``weekly_cost_snapshot``):
    ``journal=(ctx, id_base)`` routes the insert THROUGH ``emit_model_a`` — the
    computed cost rides in the journaled ``columns`` so replay reads it back
    verbatim and NEVER recomputes from provider JSONL (which Claude Code prunes).
    ``id_base`` is the triggering record's logical id (the obs line id on the
    record-usage path, the op line id on the sync-week op path); the evt id is
    ``wcs:<id_base>:<week_start_date>``. Default ``None`` is today's bare insert
    and must never append an evt. Returns the target rowid (fresh or converged
    crash-replay) exactly as the direct path returns ``lastrowid``.
    """
    start_at = _canonicalize_optional_iso(week_start_at, "weekStartAt")
    end_at = _canonicalize_optional_iso(week_end_at, "weekEndAt")
    range_start = parse_iso_datetime(range_start_iso, "rangeStartIso").isoformat(timespec="seconds")
    range_end = parse_iso_datetime(range_end_iso, "rangeEndIso").isoformat(timespec="seconds")
    captured = as_of or now_utc_iso()
    if journal is not None:
        ctx, id_base = journal
        import _cctally_journal
        return _cctally_journal.emit_model_a(
            ctx,
            kind="weekly_cost_snapshot",
            evt_id=f"wcs:{id_base}:{week_start.isoformat()}",
            table="weekly_cost_snapshots",
            columns={
                "captured_at_utc": captured,
                "week_start_date": week_start.isoformat(),
                "week_end_date": week_end.isoformat(),
                "week_start_at": start_at,
                "week_end_at": end_at,
                "range_start_iso": range_start,
                "range_end_iso": range_end,
                "cost_usd": cost_usd,
                "mode": mode,
                "project": project,
                "account_key": account_key,
            },
            at=captured,
        )
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
          project,
          account_key
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            captured,
            week_start.isoformat(),
            week_end.isoformat(),
            start_at,
            end_at,
            range_start,
            range_end,
            cost_usd,
            mode,
            project,
            account_key,
        ),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def get_max_milestone_for_week(
    conn: sqlite3.Connection,
    week_start_date: str,
    *,
    reset_event_id: int = 0,
    account_key: str,
) -> int | None:
    """Return the highest percent_threshold recorded for a week's segment,
    or None.

    ``reset_event_id`` (v1.7.2): default 0 (= pre-credit / no-event
    sentinel) preserves legacy behavior on un-credited weeks. When an
    in-place credit lifts a week into a new segment, callers pass the
    segment id so the segment's threshold ledger is independent of the
    pre-credit one — the post-credit 1% / 2% / 3% milestones fire even
    if the pre-credit segment already crossed those thresholds.

    ``account_key`` (#341, review finding 11): MANDATORY — the max is scoped
    to one account's ledger so a second account crossing the SAME threshold in
    the SAME week is not silently deduped against the first. No silent global
    fallback: the caller resolves the account being processed.
    """
    row = conn.execute(
        """
        SELECT MAX(percent_threshold) AS max_pct
        FROM percent_milestones
        WHERE week_start_date = ?
          AND reset_event_id = ?
          AND account_key = ?
        """,
        (week_start_date, reset_event_id, account_key),
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
    account_key: str,
) -> float | None:
    """Return the cumulative_cost_usd for a specific (week, threshold,
    segment), or None.

    ``reset_event_id`` (v1.7.2): segment-aware lookup. Default 0 preserves
    legacy behavior. Used by ``maybe_record_milestone`` to compute the
    marginal cost between consecutive thresholds inside the SAME segment
    — without the filter, the post-credit threshold-3 row would compute
    its marginal against the pre-credit threshold-2 cost (wrong segment).

    ``account_key`` (#341): MANDATORY — scoped to one account's ledger (a
    marginal must be computed against the SAME account's prior threshold row).
    """
    row = conn.execute(
        """
        SELECT cumulative_cost_usd
        FROM percent_milestones
        WHERE week_start_date = ?
          AND percent_threshold = ?
          AND reset_event_id = ?
          AND account_key = ?
        """,
        (week_start_date, percent_threshold, reset_event_id, account_key),
    ).fetchone()
    if row:
        return float(row["cumulative_cost_usd"])
    return None


def get_milestones_for_week(
    conn: sqlite3.Connection,
    week_start_date: str,
    *,
    account_key: "str | None" = None,
) -> list[sqlite3.Row]:
    """Return all milestones for a week, ordered by threshold ascending.

    ``account_key`` (#341, spec §3): ``None`` = the account-blind merged read
    (today's byte-identical behavior); a real key / ``unattributed`` scopes the
    ``percent_milestones`` read to that account (``percent-breakdown --account``).
    """
    acct_pred = "" if account_key is None else " AND account_key = ?"
    acct_p: tuple = () if account_key is None else (account_key,)
    return conn.execute(
        f"""
        SELECT *
        FROM percent_milestones
        WHERE week_start_date = ?{acct_pred}
        ORDER BY percent_threshold ASC
        """,
        (week_start_date,) + acct_p,
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
    as_of: "str | None" = None,
    account_key: str = "unattributed",
) -> int:
    """Insert a percent_milestones row idempotently.

    ``account_key`` (#341): the account dimension of the write. Default
    ``"unattributed"`` mirrors the schema DEFAULT as a defensive backstop
    (rev 4.1); the production writer (``maybe_record_milestone``) passes the
    resolved account explicitly, enforced by the structural writer-audit test.
    Participates in the ``UNIQUE(account_key, week_start_date, percent_threshold,
    reset_event_id)`` dedup key so two accounts crossing the same threshold in
    the same week each get their own row.

    ``as_of`` (DB journal redesign §5.2.3): overrides the ``captured_at_utc``
    wall-clock stamp with the record's capture time when the ingester drives
    this at fold time; ``None`` keeps the legacy ``now_utc_iso()`` behavior.

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
          reset_event_id,
          account_key
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            as_of or now_utc_iso(),
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
            account_key,
        ),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount)


def insert_budget_milestone(
    conn: sqlite3.Connection,
    *,
    vendor: str,
    period_start_at: str,
    period: "str | None" = None,
    threshold: int,
    budget_usd: float,
    spent_usd: float,
    consumption_pct: float,
    commit: bool = True,
    as_of: "str | None" = None,
    account_key: str = "*",
) -> int:
    """INSERT OR IGNORE a budget threshold crossing into the unified vendor-tagged
    table (#143). Returns ``cur.rowcount`` (1 = genuinely new crossing, 0 =
    INSERT OR IGNORE no-op on a pre-existing ``(vendor, period_start_at, period,
    threshold)`` row).

    The merged ``budget_milestones`` table (migration 012) carries a ``vendor``
    column (``'claude'``|``'codex'``) and the renamed ``period_start_at`` key
    (the Claude subscription-week start OR the Codex calendar-period start —
    Codex has no Anthropic subscription week, spec §6). Mirrors
    :func:`insert_percent_milestone`'s rowcount contract so the alert-fire
    predicate (`if inserted == 1`) is race-safe without a follow-up SELECT.
    ``period`` (#137) is the configured period noun at crossing
    ('calendar-week'|'calendar-month'|'subscription-week'); it discriminates the
    UNIQUE key so calendar-week and calendar-month windows that share a start
    instant don't collide. ``account_key`` (#341 Step 4-eval) discriminates the
    per-account ladder from the vendor-wide ladder: ``"*"`` (the schema DEFAULT +
    this arg's default) is the vendor-wide row; a real account key is that
    account's own ladder — ``UNIQUE(vendor, account_key, period_start_at, period,
    threshold)``. A NULL ``period`` is the pre-011 "unknown" sentinel
    (only seeded migration rows carry it). ``alerted_at`` is left NULL — the
    caller stamps it in the SAME transaction BEFORE dispatching (set-then-dispatch
    invariant, CLAUDE.md Alerts gotcha). ``commit=False`` lets the caller bundle
    the INSERT with the follow-up ``alerted_at`` UPDATE in one transaction so a
    crash between them can't strand ``alerted_at`` NULL forever.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO budget_milestones "
        "(vendor, account_key, period_start_at, period, threshold, budget_usd, "
        " spent_usd, consumption_pct, crossed_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(vendor),
            str(account_key),
            period_start_at,
            period,
            int(threshold),
            float(budget_usd),
            float(spent_usd),
            float(consumption_pct),
            as_of or now_utc_iso(),
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
    as_of: "str | None" = None,
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
            as_of or now_utc_iso(),
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
    as_of: "str | None" = None,
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
            as_of or now_utc_iso(),
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


def _resolve_claude_budget_window(conn, now_utc, *, period, config, tz,
                                  window_account_key=None):
    """Resolve the Claude budget's ``(start_utc, end_utc)`` window for the
    configured ``period`` (calendar-period-codex-budgets generalization, spec
    §6). Subscription-week → the existing ``_resolve_current_budget_window``
    (snapshot-anchored; may return ``None`` when no usage snapshot has landed
    yet). Calendar period → the pure ``_resolve_calendar_window`` (derived purely
    from ``now`` + the period; NEVER ``None``). The dedup key column is now
    ``period_start_at`` (#143) — it carries the resolved PERIOD-start instant
    (subscription-week OR calendar period-start).

    ``window_account_key`` (#341, spec §6 `*`-anchor): scopes the
    subscription-week snapshot window to one account (the active account for a
    `*` ladder, the ladder's own account for a per-account ladder). ``None`` =
    merged (byte-identical); calendar periods ignore it (pure calendar)."""
    c = _cctally()
    if period == "subscription-week":
        return c._resolve_current_budget_window(
            conn, now_utc, account_key=window_account_key)
    return c._resolve_calendar_window(period, now_utc, config, tz)


def _resolve_budget_window(conn, *, vendor, now_utc, period, config, tz,
                           window_account_key=None):
    """Resolve the budget period-start instant for ``vendor`` (#143). CHEAP — does
    NO cost SUM, preserving the pre-probe-before-spend hot path (spec §4.2): the
    firing paths resolve this cheap window, pre-probe which thresholds are already
    latched, and skip the cost SUM entirely when nothing is pending.

    Dispatches to the per-vendor window primitive:
      * claude → :func:`_resolve_claude_budget_window` (snapshot-anchored for
        subscription-week → may be ``None`` pre-snapshot; calendar period → the
        pure calendar window, never ``None``).
      * codex  → :func:`_resolve_codex_budget_period_window` (pure calendar window;
        never ``None``).

    Returns the period-start ``datetime`` or ``None`` (claude subscription-week
    pre-snapshot)."""
    if vendor == "claude":
        window = _resolve_claude_budget_window(
            conn, now_utc, period=period, config=config, tz=tz,
            window_account_key=window_account_key,
        )
    else:
        window = _resolve_codex_budget_period_window(period, now_utc, config, tz)
    if window is None:
        return None
    start_at, _end_at = window
    return start_at


def _budget_spend_for_vendor(conn, *, vendor, start_at, now_utc,
                             account_key: str = "*") -> float:
    """Spend over ``[start_at, now]`` for ``vendor`` (#143) — the COSTLY leg,
    called only after the pre-probe finds pending thresholds (spec §4.2). claude
    routes through the Claude cost SUM (``mode="auto"``); codex through the Codex
    cost SUM.

    ``account_key`` (#341 Step 4-eval, spec §6): the vendor-wide sentinel ``"*"``
    sums EVERY account's spend (including ``unattributed`` — the guaranteed-
    complete vendor total), so it reads unscoped (``account_key=None`` on the
    cost SUM). A REAL account scopes the sum to that account's stamped entries so
    a per-account ladder counts only its own spend (unattributed spend can never
    trip a per-account alert)."""
    c = _cctally()
    scope = None if account_key == "*" else account_key
    # Byte-stability: the vendor-wide (`*`) path calls with the EXACT legacy
    # signature (no `account_key` kwarg), so existing cost-sum test doubles +
    # every non-account install are untouched. Only a per-account ladder passes
    # the kwarg (its doubles accept it).
    extra = {} if scope is None else {"account_key": scope}
    if vendor == "claude":
        return c._sum_cost_for_range(start_at, now_utc, mode="auto", **extra)
    return c._sum_codex_cost_for_range(start_at, now_utc, **extra)


def _reconcile_budget_milestones_on_set(
    conn, *, vendor, target, thresholds, now_utc, period, config=None, tz=None,
    as_of=None, commit=True, account_key: str = "*",
):
    """Forward-only-from-set reconcile for the budget axis (both vendors, #143):
    on `budget set`, every threshold ALREADY crossed for the current
    window/period is recorded with ``alerted_at`` SET but WITHOUT dispatch — so
    setting a budget when you're already at 95% does NOT instant-popup. Thresholds
    not yet crossed get NO row, so they fire later via the firing path
    (:func:`maybe_record_budget_milestone` / :func:`maybe_record_codex_budget_milestone`).

    A mid-window target change re-runs this; thresholds already alerted stay
    deduped via UNIQUE(vendor, period_start_at, period, threshold) + the
    ``alerted_at IS NULL`` guard on the UPDATE (so an existing alerted row is never
    re-stamped).

    Cold path — no pre-probe; resolves the cheap window then computes spend right
    after (spec §4.2 ordering). ``vendor`` selects the window + spend dispatcher;
    claude subscription-week may resolve ``None`` pre-snapshot (early return).
    Keeps its own stamp-no-dispatch tail (distinct from :func:`_budget_crossings`,
    which dispatches) — that asymmetry is intrinsic, not duplication.
    """
    start_at = _resolve_budget_window(
        conn, vendor=vendor, now_utc=now_utc, period=period, config=config, tz=tz
    )
    if start_at is None:
        return
    period_key = start_at.isoformat(timespec="seconds")
    spent = _budget_spend_for_vendor(
        conn, vendor=vendor, start_at=start_at, now_utc=now_utc,
        account_key=account_key,
    )
    # target > 0 guaranteed by the caller (validated weekly_usd / amount_usd);
    # the else is belt-and-suspenders.
    consumption_pct = (spent / target * 100.0) if target > 0 else 0.0
    for t in sorted(thresholds):
        if consumption_pct + 1e-9 >= t:
            insert_budget_milestone(
                conn,
                vendor=vendor,
                period_start_at=period_key,
                period=period,
                threshold=t,
                budget_usd=target,
                spent_usd=spent,
                consumption_pct=consumption_pct,
                commit=False,
                as_of=as_of,
                account_key=account_key,
            )
            # alerted_at UPDATE keys on the CONCRETE (vendor, account_key, period)
            # (not the wildcard): only the row we just inserted is stamped, never
            # a pre-011 NULL-period sibling (#137), another vendor's row (#143),
            # or a different account's ladder (#341).
            conn.execute(
                "UPDATE budget_milestones SET alerted_at = ? "
                "WHERE vendor = ? AND account_key = ? AND period_start_at = ? "
                "  AND period = ? AND threshold = ? AND alerted_at IS NULL",
                (as_of or now_utc_iso(), vendor, account_key, period_key, period, t),
            )
    if commit:
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


def _budget_crossings(
    conn, *, vendor, period_key, period=None, thresholds, target, spent, now_utc,
    as_of=None, account_key: str = "*",
):
    """Shared INSERT-and-arm core for the budget axis (both vendors, #143): for
    every STILL-pending threshold that's been crossed at ``spent``, ``INSERT OR
    IGNORE`` a milestone (commit=False) into the unified vendor-tagged table and —
    on the genuine-new-crossing winner (rowcount==1) — stamp ``alerted_at`` in the
    SAME transaction (set-then-dispatch), returning the list of crossings the
    caller must dispatch.

    Pure of config/window resolution: both firing sites (record-usage +
    opportunistic ``cctally budget``), for either vendor, feed the
    already-resolved ``vendor`` / ``period_key`` / ``target`` / ``spent`` here, so
    the crossing arithmetic + the set-then-dispatch invariant live in ONE place.
    Does NOT commit — the caller owns the single durable commit that bundles every
    INSERT with its ``alerted_at`` UPDATE. Applies the +1e-9 float-floor snap
    (CLAUDE.md gotcha). Returns ``[(threshold, crossed_at, spent, target,
    consumption_pct), ...]`` for the rowcount==1 winners only.

    Forward-only / fire-once is enforced by INSERT OR IGNORE's rowcount on the
    UNIQUE(vendor, period_start_at, period, threshold) key; a racing record-usage
    instance OR an already-recorded threshold gets rowcount==0 and is skipped
    ([Dedup mustn't gate side effects])."""
    consumption_pct = (spent / target * 100.0) if target > 0 else 0.0
    fired: "list" = []
    for t in sorted(thresholds):
        if consumption_pct + 1e-9 >= t:
            inserted = insert_budget_milestone(
                conn,
                vendor=vendor,
                period_start_at=period_key,
                period=period,
                threshold=t,
                budget_usd=target,
                spent_usd=spent,
                consumption_pct=consumption_pct,
                commit=False,
                as_of=as_of,
                account_key=account_key,
            )
            if inserted == 1:
                crossed_at = as_of or now_utc_iso()
                # alerted_at UPDATE keys on the CONCRETE (vendor, account_key,
                # period) (#137 / #143 / #341): only the row just inserted is
                # stamped, never a pre-011 NULL-period sibling, another vendor's
                # row, or a different account's ladder.
                conn.execute(
                    "UPDATE budget_milestones SET alerted_at = ? "
                    "WHERE vendor = ? AND account_key = ? AND period_start_at = ? "
                    "  AND period = ? AND threshold = ? AND alerted_at IS NULL",
                    (crossed_at, vendor, account_key, period_key, period, t),
                )
                fired.append((t, crossed_at, spent, target, consumption_pct))
    return fired


def _reconcile_codex_budget_on_config_write(validated_budget, *, conn=None, as_of=None):
    """Forward-only reconcile shared by the Codex-budget config write paths
    (`budget set --vendor codex`, `config set budget.codex`). Gated +
    best-effort: a Codex budget with alerts off or no thresholds records
    nothing; a stats.db failure never fails the write. Runs OUTSIDE any
    config_writer_lock (open_db has its own locking). Mirrors
    :func:`_reconcile_budget_on_config_write`.

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    ``conn`` runs the reconcile on the caller's connection (no internal
    open/commit/close); ``as_of`` (ISO-Z) injects the capture time. Both
    defaults keep the legacy own-connection, wall-clock behavior."""
    codex = (validated_budget or {}).get("codex")
    if not codex:
        return
    thresholds = codex.get("alert_thresholds") or []
    amount_usd = codex.get("amount_usd")
    # Per-account Codex ladders (#341 Step 4-eval): reconcile each account too.
    accounts = codex.get("accounts") or {}
    if not (codex.get("alerts_enabled") and thresholds
            and (amount_usd is not None or accounts)):
        return
    c = _cctally()
    own_conn = conn is None
    try:
        import argparse
        config = c.load_config()
        tz = c.resolve_display_tz(argparse.Namespace(tz=None), config)
        now_utc = _as_of_or_command(as_of)
        if own_conn:
            conn = open_db()
        try:
            ladders = []
            if amount_usd is not None:
                ladders.append(("*", amount_usd))
            ladders.extend((k, v) for k, v in accounts.items())
            for acct_key, acct_usd in ladders:
                _reconcile_budget_milestones_on_set(
                    conn,
                    vendor="codex",
                    target=acct_usd,
                    thresholds=thresholds,
                    now_utc=now_utc,
                    period=codex["period"],
                    config=config,
                    tz=tz,
                    as_of=as_of,
                    commit=False,
                    account_key=acct_key,
                )
            if own_conn:
                conn.commit()
        finally:
            if own_conn:
                conn.close()
    except Exception as exc:  # best-effort; never fail the write
        eprint(f"[codex-budget-milestone] reconcile on set failed: {exc}")


def _reconcile_budget_on_config_write(validated_budget, *, conn=None, as_of=None):
    """Forward-only reconcile shared by all three budget-config write
    paths (`budget set`, `config set budget.*`, dashboard POST
    /api/settings). Gated + best-effort: a budget with alerts off or no
    thresholds records nothing; a stats.db failure never fails the write.
    Runs OUTSIDE any config_writer_lock (open_db has its own locking).

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    ``conn`` runs the reconcile on the caller's connection (no internal
    open/commit/close); ``as_of`` (ISO-Z) injects the capture time. Both
    defaults keep the legacy own-connection, wall-clock behavior."""
    thresholds = validated_budget.get("alert_thresholds") or []
    alerts_enabled = bool(validated_budget.get("alerts_enabled"))
    weekly_usd = validated_budget.get("weekly_usd")
    # Per-account ladders (#341 Step 4-eval): reconcile each real account in
    # `budget.accounts` too, so setting a per-account budget mid-week (already
    # over) records the crossed thresholds as already-alerted WITHOUT dispatch.
    accounts = validated_budget.get("accounts") or {}
    if not thresholds or not alerts_enabled or (weekly_usd is None and not accounts):
        return
    c = _cctally()
    period = validated_budget.get("period", "subscription-week")
    own_conn = conn is None
    try:
        import argparse
        config = c.load_config()
        tz = c.resolve_display_tz(argparse.Namespace(tz=None), config)
        now_utc = _as_of_or_command(as_of)
        if own_conn:
            conn = open_db()
        try:
            ladders = []
            if weekly_usd is not None:
                ladders.append(("*", weekly_usd))
            ladders.extend((k, v) for k, v in accounts.items())
            for acct_key, acct_usd in ladders:
                _reconcile_budget_milestones_on_set(
                    conn,
                    vendor="claude",
                    target=acct_usd,
                    thresholds=thresholds,
                    now_utc=now_utc,
                    period=period,
                    config=config,
                    tz=tz,
                    as_of=as_of,
                    commit=False,
                    account_key=acct_key,
                )
            if own_conn:
                conn.commit()
        finally:
            if own_conn:
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
    validated_budget, touched_projects=None, *, conn=None, as_of=None
):
    """Forward-only-from-write reconcile for PER-PROJECT budgets (spec §6.8).

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    ``conn`` runs the reconcile on the caller's connection (no internal
    open/commit/close); ``as_of`` (ISO-Z) injects the capture time. Both
    defaults keep the legacy own-connection, wall-clock behavior.

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
    own_conn = conn is None
    try:
        if own_conn:
            conn = open_db()
        try:
            now_utc = _as_of_or_command(as_of)
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
                    as_of=as_of,
                )
                conn.execute(
                    "UPDATE project_budget_milestones SET alerted_at = ? "
                    "WHERE week_start_at = ? AND project_key = ? "
                    "  AND threshold = ? AND alerted_at IS NULL",
                    (as_of or now_utc_iso(), week_key, project_key, t),
                )
            if own_conn:
                conn.commit()
        finally:
            if own_conn:
                conn.close()
    except Exception as exc:  # best-effort; never fail the write
        eprint(
            f"[project-budget-milestone] reconcile on write failed: {exc}"
        )
