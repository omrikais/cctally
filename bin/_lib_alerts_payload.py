"""Alert-payload constructors and notification text builders.

Pure-fn layer (no I/O at import time): holds the deterministic helpers that
shape an alert's structured payload (`_build_alert_payload_*`) and render
its (title, subtitle, body) triple (`_alert_text_*`), plus the AppleScript
string-escape used to embed the rendered triple in an `osascript` literal
(`_escape_applescript_string`).

The companion I/O surface — `_alerts_log_path`, `_dispatch_alert_notification`,
the alerts-config validators — stays in `bin/cctally` because it touches
the filesystem (mkdir + append-log), spawns subprocesses, and reads
`os.environ` for the integration-harness escape hatch.

Cross-sibling dependency: `_alert_text_five_hour` calls `format_display_dt`,
which lives in `_lib_display_tz`. We load it via a local `_load_lib` helper
(spec_from_file_location, same shape as `bin/cctally:_load_sibling`) so this
pure layer remains independent of `bin/`'s sys.path posture and free of any
back-import of `cctally`.

`bin/cctally` re-exports every public symbol below so internal call sites
and `SourceFileLoader`-based tests (e.g. `bin/cctally-alerts-dispatch-test`)
resolve unchanged. A private `_eprint` duplicates `bin/cctally:eprint` per
the split design's §5.3 contract.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
from typing import Any

from zoneinfo import ZoneInfo


def _eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def _load_lib(name: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    import importlib.util as _ilu
    p = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(name, p)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lib_display_tz = _load_lib("_lib_display_tz")
format_display_dt = _lib_display_tz.format_display_dt

# Account dimension (#341): every alert payload carries the account_key it wrote
# under as a TOP-LEVEL field (never in `context`, to avoid touching any
# context-serialized golden). Real-account families default to the sentinel;
# vendor-wide families default to `*`. `_dispatch_alert_notification` reads it for
# the unconditional 8th `alerts.log` field + the R8 `[label]` title prefix.
_lib_accounts = _load_lib("_lib_accounts")
_UNATTRIBUTED = _lib_accounts.UNATTRIBUTED
_VENDOR_WIDE = _lib_accounts.VENDOR_WIDE

# Next-step routing (#620 S1, D11): every axis offers the command that explains
# it, scoped to that alert's own provider and window. The scope is derived from
# the retained context by `_lib_alert_scope`; nothing here parses an alert id.
_lib_alert_scope = _load_lib("_lib_alert_scope")
derive_alert_scope = _lib_alert_scope.derive_alert_scope
format_next_step = _lib_alert_scope.format_next_step
alert_next_step_command = _lib_alert_scope.alert_next_step_command


next_step_line = _lib_alert_scope.next_step_line


def alert_next_step_line(
    payload: dict, tz: "ZoneInfo | None", now: "dt.datetime | None" = None,
) -> str:
    """The one affordance line an alert body ends with.

    The command's own arguments stay in UTC, because that is what the
    selectors they feed accept (``--block-start`` is documented naive-is-UTC);
    the trailing scope statement is what renders in the display zone.

    ``now`` is the instant the liveness test compares against; it defaults to
    the current UTC instant. The clock read lives here rather than in
    ``_lib_alert_scope`` so that kernel stays a pure leaf.
    """
    axis = payload.get("axis")
    ctx = payload.get("context") or {}
    scope = derive_alert_scope(axis, ctx, payload.get("account_key"))
    command = alert_next_step_command(axis, ctx, scope)
    if command is None:
        return format_next_step(
            None,
            unavailable_reason=_lib_alert_scope.alert_target_unavailable_reason(
                axis, ctx, scope
            ),
        )
    # A command that reports only the live window cannot address one that has
    # already closed. Offering it anyway would report the current window under
    # a line naming the closed one, so the navigation is withheld with its
    # cause — the same treatment a purged five-hour block already gets.
    if (
        _lib_alert_scope.command_reports_the_live_period_only(command)
        and scope.window_end is not None
    ):
        if now is None:
            now = dt.datetime.now(dt.timezone.utc)
        if scope.window_end <= now:
            return format_next_step(
                None,
                unavailable_reason=_lib_alert_scope.closed_window_reason(command),
            )
    if scope.available:
        return next_step_line(
            command,
            provider=scope.provider,
            window_start=scope.window_start,
            window_end=scope.window_end,
            tz=tz,
            granularity=scope.window_granularity,
        )
    # A live-window command with no derivable length: name the provider and
    # say what is missing, never a window the alert did not describe.
    detail = (
        f"{scope.provider} · {scope.withheld_reason}"
        if scope.provider else scope.withheld_reason
    )
    return format_next_step(command, detail=detail)


def synthetic_preview_week_start(
    now: "dt.datetime | None" = None,
) -> dt.datetime:
    """The week start the two `alerts test --axis weekly` previews use.

    A real crossing carries `percent_milestones.week_start_at`, the week's
    reset instant, and a preview that omitted it rendered the day-granularity
    fallback — two bare dates with no reset hour and no zone — which is not
    what a real crossing produces. The instant is hour-aligned and one day
    back so its reset hour is a real clock reading rather than the midnight
    that would render identically to the fallback it replaces.
    """
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    return (now - dt.timedelta(days=1)).replace(
        minute=0, second=0, microsecond=0
    )


def _with_next_step(body: str, payload: dict, tz: "ZoneInfo | None") -> str:
    """Append the affordance as its own line. AppleScript collapses the
    newline to a space (``_escape_applescript_string``) and ``notify-send``
    keeps it, so both notifiers render the whole sentence."""
    return f"{body}\n{alert_next_step_line(payload, tz)}"


def _alert_text_weekly(payload: dict, tz: "ZoneInfo | None") -> tuple[str, str, str]:
    """Build (title, subtitle, body) for a weekly threshold alert.

    Most datetime renders go through ``format_display_dt`` (chokepoint
    rule), but ``week_start_date`` is a CALENDAR DAY (``YYYY-MM-DD``),
    not a clock instant — tagging it as UTC-midnight and then routing
    through ``format_display_dt(..., tz)`` would shift the wall-clock
    day for non-UTC ``display.tz`` (e.g. "Sun, Apr 27" → "Sat, Apr 26"
    in ``America/Los_Angeles``). Render the date directly via
    ``dt.date.fromisoformat`` so the rendered weekday/day matches the
    calendar date the user thinks of as "this week".
    """
    threshold = int(payload["threshold"])
    title = f"cctally - Weekly usage {threshold}% reached"
    ctx = payload.get("context") or {}
    week_start_date = ctx.get("week_start_date")
    if week_start_date:
        # Calendar-day render: ``week_start_date`` is a date, not an
        # instant; bypass tz conversion to avoid the off-by-one shift
        # documented above. ``tz`` is accepted for signature parity
        # with peer alert builders and intentionally unused here.
        subtitle = "Week starting " + dt.date.fromisoformat(
            week_start_date
        ).strftime("%a, %b %d")
    else:
        subtitle = "Current week"
    cumulative = float(ctx.get("cumulative_cost_usd") or 0.0)
    dpp = ctx.get("dollars_per_percent")
    if dpp is not None:
        body = f"${cumulative:.2f} spent so far - ${float(dpp):.2f} per 1%"
    else:
        body = f"${cumulative:.2f} spent so far"
    return title, subtitle, _with_next_step(body, payload, tz)


def _alert_text_five_hour(payload: dict, tz: "ZoneInfo | None") -> tuple[str, str, str]:
    """Build (title, subtitle, body) for a 5h-block threshold alert.

    All datetime renders go through ``format_display_dt`` (chokepoint rule).
    """
    threshold = int(payload["threshold"])
    title = f"cctally - 5h-block usage {threshold}% reached"
    ctx = payload.get("context") or {}
    bsa_iso = ctx.get("block_start_at")
    if bsa_iso:
        bsa = dt.datetime.fromisoformat(str(bsa_iso).replace("Z", "+00:00"))
        subtitle = "Block started " + format_display_dt(
            bsa, tz, fmt="%H:%M", suffix=False
        )
    else:
        subtitle = "Current 5h block"
    cost = float(ctx.get("block_cost_usd") or 0.0)
    model = ctx.get("primary_model")
    if model:
        body = f"${cost:.2f} spent in this block - current model: {model}"
    else:
        body = f"${cost:.2f} spent in this block"
    return title, subtitle, _with_next_step(body, payload, tz)


def _escape_applescript_string(s: str) -> str:
    """Escape ``s`` for embedding inside an AppleScript double-quoted literal.

    Order matters: backslashes first (otherwise the inserted backslashes
    from the double-quote escape get re-escaped), then double quotes,
    then newlines/CRs collapse to spaces (AppleScript chokes on raw NL).
    """
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _build_alert_payload_weekly(
    *,
    threshold: int,
    crossed_at_utc: str,
    week_start_date: str,
    cumulative_cost_usd: float,
    dollars_per_percent: "float | None",
    account_key: str = _UNATTRIBUTED,
    week_start_at: "str | None" = None,
) -> dict:
    """Build the alert payload for a weekly threshold crossing.

    ``alerted_at`` mirrors ``crossed_at`` here because the production caller
    sets the DB ``alerted_at`` BEFORE invoking ``_dispatch_alert_notification``
    (set-then-dispatch invariant, spec §3.2). Consumers (envelope builders,
    test inspectors) read the ``alerted_at`` field as the authoritative
    "alert was attempted" timestamp. ``account_key`` (#341) is the real account
    the crossing wrote under (per-account family).

    ``week_start_at`` is the week's RESET INSTANT, retained on the milestone
    row and already a declared ``AlertEntry.context`` key. Without it a reader
    can only fall back to ``week_start_date``, which names the right week but
    places both bounds at UTC midnight — wrong by the whole reset-hour offset
    for every account whose week does not reset at midnight. Empty string when
    the row predates the column, matching the five-hour axis's
    ``block_start_at``: the key stays on the wire and the reader degrades to
    day granularity rather than inventing a clock reading."""
    return {
        "id": f"weekly:{week_start_date}:{threshold}",
        "axis": "weekly",
        "threshold": int(threshold),
        "crossed_at": crossed_at_utc,
        "alerted_at": crossed_at_utc,  # set-then-dispatch
        "account_key": str(account_key),
        "context": {
            "week_start_date": week_start_date,
            "week_start_at": week_start_at or "",
            "cumulative_cost_usd": float(cumulative_cost_usd),
            "dollars_per_percent": (
                float(dollars_per_percent) if dollars_per_percent is not None else None
            ),
        },
    }


def _build_alert_payload_five_hour(
    *,
    threshold: int,
    crossed_at_utc: str,
    five_hour_window_key: int,
    block_start_at: str,
    block_cost_usd: float,
    primary_model: "str | None",
    account_key: str = _UNATTRIBUTED,
) -> dict:
    """Build the alert payload for a 5h-block threshold crossing.

    See ``_build_alert_payload_weekly`` for the ``alerted_at == crossed_at``
    rationale. ``primary_model`` is the highest-cost model active in the
    block (resolved via ``_resolve_primary_model_for_block``); ``None`` when
    the rollup-children child table is empty (e.g., direct-JSONL-fallback
    path before lazy backfill). ``account_key`` (#341) is the block's account
    (per-account family)."""
    return {
        "id": f"five_hour:{five_hour_window_key}:{threshold}",
        "axis": "five_hour",
        "threshold": int(threshold),
        "crossed_at": crossed_at_utc,
        "alerted_at": crossed_at_utc,  # set-then-dispatch
        "account_key": str(account_key),
        "context": {
            "five_hour_window_key": int(five_hour_window_key),
            "block_start_at": block_start_at,
            "block_cost_usd": float(block_cost_usd),
            "primary_model": primary_model,
        },
    }


def _alert_text_budget(payload: dict, tz: "ZoneInfo | None") -> tuple[str, str, str]:
    """Build (title, subtitle, body) for an equiv-$ budget threshold alert.

    ``week_start_at`` is an instant, but the budget alert text doesn't render
    it (the subtitle is the threshold, the body the dollar progress) — so no
    ``format_display_dt`` call is needed here. ``tz`` is accepted for
    signature parity with peer ``_alert_text_*`` builders and intentionally
    unused.
    """
    threshold = int(payload["threshold"])
    title = "cctally - budget"
    subtitle = f"{threshold}% of budget"
    ctx = payload.get("context") or {}
    spent = float(ctx.get("spent_usd") or 0.0)
    budget = float(ctx.get("budget_usd") or 0.0)
    consumption = float(ctx.get("consumption_pct") or 0.0)
    body = f"${spent:,.2f} of ${budget:,.2f} ({consumption:.0f}% of budget)"
    return title, subtitle, _with_next_step(body, payload, tz)


def _build_alert_payload_budget(
    *,
    threshold: int,
    crossed_at_utc: str,
    week_start_at: str,
    budget_usd: float,
    spent_usd: float,
    consumption_pct: float,
    period: str = "subscription-week",
    account_key: str = _VENDOR_WIDE,
) -> dict:
    """Build the alert payload for an equiv-$ budget threshold crossing.

    See ``_build_alert_payload_weekly`` for the ``alerted_at == crossed_at``
    rationale (set-then-dispatch invariant). ``axis: "budget"`` is the third
    alert axis (Task 4 surfaces it in the dashboard Recent-alerts panel).

    ``period`` defaults to ``subscription-week`` (the existing behavior — a
    calendar-period-codex-budgets generalization, spec §6). The ``week_start_at``
    key column carries the resolved PERIOD-start instant for a calendar period
    (the name stays a back-compat misnomer, like ``weekly_usd``); the
    additive ``period`` + ``period_start_at`` context fields let the dashboard
    (Task 4) label "Month" / "Calendar week" instead of the hardcoded "Week".
    The legacy subscription-week case is byte-stable on the rendered text — the
    new context keys are purely additive and consumed only by the period-aware
    label fix."""
    return {
        "id": f"budget:{week_start_at}:{threshold}",
        "axis": "budget",
        "threshold": int(threshold),
        "crossed_at": crossed_at_utc,
        "alerted_at": crossed_at_utc,  # set-then-dispatch
        "account_key": str(account_key),
        "context": {
            "week_start_at": week_start_at,
            "period": str(period),
            "period_start_at": week_start_at,
            "budget_usd": float(budget_usd),
            "spent_usd": float(spent_usd),
            "consumption_pct": float(consumption_pct),
        },
    }


def _alert_text_project_budget(
    payload: dict, tz: "ZoneInfo | None"
) -> tuple[str, str, str]:
    """Build (title, subtitle, body) for a PER-PROJECT equiv-$ budget threshold
    alert (axis ``project_budget``, spec §5.3).

    Mirrors :func:`_alert_text_budget` but prefixed with the project's basename
    so a user reading the notification knows WHICH project crossed (e.g.
    *"Project foo - $26.00 of $25.00 (104% of budget)"*). The rendered numbers
    come from the payload (snapshotted at crossing), never live config that may
    have changed since (Codex P0-4). ``week_start_at`` is an instant but the
    text doesn't render it, so no ``format_display_dt`` call is needed; ``tz`` is
    accepted for signature parity with peer ``_alert_text_*`` builders and
    intentionally unused (same as ``_alert_text_budget``).
    """
    threshold = int(payload["threshold"])
    ctx = payload.get("context") or {}
    project = ctx.get("project") or "(project)"
    title = "cctally - project budget"
    subtitle = f"{project} - {threshold}% of budget"
    spent = float(ctx.get("spent_usd") or 0.0)
    budget = float(ctx.get("budget_usd") or 0.0)
    consumption = float(ctx.get("consumption_pct") or 0.0)
    body = (
        f"Project {project} - ${spent:,.2f} of ${budget:,.2f} "
        f"({consumption:.0f}% of budget)"
    )
    return title, subtitle, _with_next_step(body, payload, tz)


def _build_alert_payload_project_budget(
    *,
    threshold: int,
    crossed_at_utc: str,
    week_start_at: str,
    project: str,
    project_key: str,
    budget_usd: float,
    spent_usd: float,
    consumption_pct: float,
    account_key: str = _VENDOR_WIDE,
) -> dict:
    """Build the alert payload for a PER-PROJECT equiv-$ budget threshold
    crossing (axis ``project_budget``, the fifth alert axis; spec §5.3).

    Mirrors :func:`_build_alert_payload_budget` with the project dimension
    added: ``project`` is the collision-disambiguated basename
    (``ProjectKey.display_key``, for human-readable notification text) and
    ``project_key`` is the canonical git-root (``ProjectKey.bucket_path``, the
    stable identity dimension of the UNIQUE dedup key). See
    :func:`_build_alert_payload_weekly` for the ``alerted_at == crossed_at``
    rationale (set-then-dispatch invariant). The dashboard envelope (Task 4)
    surfaces this axis in the Recent-alerts panel from the row-sourced context.
    """
    return {
        "id": f"project_budget:{week_start_at}:{project_key}:{int(threshold)}",
        "axis": "project_budget",
        "threshold": int(threshold),
        "crossed_at": crossed_at_utc,
        "alerted_at": crossed_at_utc,  # set-then-dispatch
        "account_key": str(account_key),
        "context": {
            "week_start_at": week_start_at,
            "project": project,
            "project_key": project_key,
            "budget_usd": float(budget_usd),
            "spent_usd": float(spent_usd),
            "consumption_pct": float(consumption_pct),
        },
    }


def _alert_text_codex_budget(
    payload: dict, tz: "ZoneInfo | None"
) -> tuple[str, str, str]:
    """Build (title, subtitle, body) for a Codex budget threshold alert (axis
    ``codex_budget``, the sixth alert axis; calendar-period-codex-budgets spec
    §6).

    Mirrors :func:`_alert_text_budget` but labels the vendor (Codex) and the
    civil period (Month / Calendar week) read from the period context so the
    notification reads apart from a Claude budget alert. The rendered numbers
    come from the payload (snapshotted at crossing), never live config that may
    have changed since. ``period_start_at`` is an instant but the text doesn't
    render it as a clock time, so no ``format_display_dt`` call is needed; ``tz``
    is accepted for signature parity with peer ``_alert_text_*`` builders and
    intentionally unused (same as ``_alert_text_budget``)."""
    threshold = int(payload["threshold"])
    ctx = payload.get("context") or {}
    period = ctx.get("period")
    period_label = {
        "calendar-month": "this month",
        "calendar-week": "this week",
    }.get(period, "this period")
    title = "cctally - Codex budget"
    subtitle = f"{threshold}% of Codex budget ({period_label})"
    spent = float(ctx.get("spent_usd") or 0.0)
    budget = float(ctx.get("budget_usd") or 0.0)
    consumption = float(ctx.get("consumption_pct") or 0.0)
    body = (
        f"Codex - ${spent:,.2f} of ${budget:,.2f} "
        f"({consumption:.0f}% of budget)"
    )
    return title, subtitle, _with_next_step(body, payload, tz)


def _build_alert_payload_codex_budget(
    *,
    threshold: int,
    crossed_at_utc: str,
    period_start_at: str,
    period: str,
    budget_usd: float,
    spent_usd: float,
    consumption_pct: float,
    account_key: str = _VENDOR_WIDE,
) -> dict:
    """Build the alert payload for a Codex budget threshold crossing (axis
    ``codex_budget``, the sixth alert axis; spec §6).

    Mirrors :func:`_build_alert_payload_budget` but keyed on the resolved
    CALENDAR-period window (``period_start_at`` in place of ``week_start_at``)
    and carrying the period DISCRIMINATOR (``period`` = calendar-week /
    calendar-month) in the context so the dashboard (Task 4) labels Month /
    Calendar week instead of the hardcoded "Week". See
    :func:`_build_alert_payload_weekly` for the ``alerted_at == crossed_at``
    rationale (set-then-dispatch invariant). The dashboard envelope (Task 4)
    surfaces this axis in the Recent-alerts panel from the row-sourced context.
    """
    return {
        "id": f"codex_budget:{period_start_at}:{threshold}",
        "axis": "codex_budget",
        "threshold": int(threshold),
        "crossed_at": crossed_at_utc,
        "alerted_at": crossed_at_utc,  # set-then-dispatch
        "account_key": str(account_key),
        "context": {
            "period": str(period),
            "period_start_at": period_start_at,
            "budget_usd": float(budget_usd),
            "spent_usd": float(spent_usd),
            "consumption_pct": float(consumption_pct),
        },
    }


def _alert_text_projected(payload: dict, tz: "ZoneInfo | None") -> tuple[str, str, str]:
    """Build (title, subtitle, body) for a projected-pace alert (#121).

    These fire on the WEEK-AVERAGE projection — what you're tracking toward at
    the current week-average pace — NOT on an actual crossing. The text carries
    an explicit "(projection)" / "on current pace" cue so a user never confuses
    a projected alert with an actual-crossing one (which the weekly/budget
    builders render). The rendered numbers come from the payload (the values
    snapshotted at crossing), never from live config (Codex P0-4). ``tz`` is
    accepted for signature parity with peer ``_alert_text_*`` builders and
    intentionally unused (no instant is rendered, same as ``_alert_text_budget``).
    """
    metric = payload["metric"]
    t = int(payload["threshold"])
    proj = float(payload["projected_value"])
    denom = float(payload["denominator"])
    if metric == "weekly_pct":
        title = f"cctally - projected to reach {t}% this week"
        subtitle = "On current pace (projection)"
        body = f"Projected ~{proj:.0f}% of cap by reset (week-average pace)"
    elif metric == "codex_budget_usd":
        title = "cctally - Codex projected to exceed budget"
        subtitle = f"On current pace (projection) - {t}% of Codex budget"
        body = (
            f"Projected ${proj:,.2f} of ${denom:,.2f} Codex budget "
            f"(period-average pace)"
        )
    else:  # budget_usd
        title = "cctally - projected to exceed budget"
        subtitle = f"On current pace (projection) - {t}% of budget"
        body = (
            f"Projected ${proj:,.2f} of ${denom:,.2f} budget "
            f"(week-average pace)"
        )
    return title, subtitle, _with_next_step(body, payload, tz)


def _build_alert_payload_projected(
    *,
    metric: str,
    threshold: int,
    projected_value: float,
    denominator: float,
    week_start_at: str,
    account_key: str = _VENDOR_WIDE,
) -> dict:
    """Build the alert payload for a projected-pace threshold crossing (#121).

    ``axis: "projected"`` is the fourth alert axis; ``metric`` discriminates
    ``weekly_pct`` (denominator 100.0, "% of cap") from ``budget_usd`` and
    ``codex_budget_usd`` (denominator = target_usd, "$ of budget"; the codex
    variant renders Codex-flavored text). The frontend renders context
    FROM these row-sourced fields (``metric`` / ``projected_value`` /
    ``denominator``), not from live config that may have changed since crossing
    (Codex P0-4). No ``crossed_at``/``alerted_at`` keys here: the projected
    detector stamps ``alerted_at`` on the DB row itself in the same txn before
    dispatch (set-then-dispatch), and the dashboard envelope reads it from the
    row — mirroring ``_build_alert_payload_budget``'s context-only shape minus
    the redundant timestamp echo.
    """
    return {
        "id": f"projected:{week_start_at}:{metric}:{int(threshold)}",
        "axis": "projected",
        "metric": str(metric),
        "threshold": int(threshold),
        "projected_value": float(projected_value),
        "denominator": float(denominator),
        "account_key": str(account_key),
        "context": {
            "week_start_at": week_start_at,
            "metric": str(metric),
            "projected_value": float(projected_value),
            "denominator": float(denominator),
        },
    }
