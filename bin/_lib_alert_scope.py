"""Alert scope derivation — the exact window, provider and account an alert
describes, read from the fields that alert already retains (#620 S1, D11).

Pure leaf: stdlib only, no database access, no clock read, no I/O. Model-pool
classification delegates to ``_lib_codex_pools`` so that classification keeps
one home.

Two rules shape the whole module.

**The window END is derived, never stored.** These rows live in stats.db,
which returns early at ``STATS_INDEX_EPOCH`` before ``add_column_if_missing``
is reached, so a ``window_end_at`` column would cost an epoch bump plus
journal, replay, cutover, rebuild and rederive parity — disproportionate to a
value the retained fields already determine. Recorded caveat: a future change
to the ``BUDGET_PERIODS`` definitions would make derivation disagree with fire
time, and would then require the stamp.

**Scope is never read from the alert id.** The client contract states the id
is opaque and never parsed, the journal contract independently forbids parsing
event ids, and Claude alert ids omit the account entirely — so the id could not
carry the scope even if parsing it were permitted. ``derive_alert_scope`` takes
no id, and ``tests/test_620_alert_scope.py`` asserts structurally that no code
path here reads one.

Returning a bare ``None`` for "no window" was rejected: it cannot carry the
cause, and a caller cannot distinguish "no window" from "window withheld
because the account is vendor-wide". ``AlertScope`` follows the #556 typed-
withholding shape instead — it always returns, and it always says which state
it is in.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
from dataclasses import dataclass


def _load_lib(name):
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


_lib_codex_pools = _load_lib("_lib_codex_pools")
# Rendering a derived window for a reader is a display concern, so it goes
# through the datetime chokepoint like every other user-facing instant.
_format_display_dt = _load_lib("_lib_display_tz").format_display_dt

# Both durable sentinels. `*` marks a vendor-wide row (budget-family and the
# account-blind project-budget table); `unattributed` is a real account bucket,
# not a vendor-wide stamp, so it stays account-scoped.
VENDOR_WIDE_KEY = "*"

SUBSCRIPTION_WEEK = dt.timedelta(days=7)
CALENDAR_WEEK = dt.timedelta(days=7)
FIVE_HOUR_WINDOW = dt.timedelta(hours=5)

ACCOUNT_SCOPE = "account"
VENDOR_WIDE_SCOPE = "vendor_wide"

# Window precision. Every axis but `weekly`'s fallback retains a real instant.
WINDOW_INSTANT = "instant"
WINDOW_DAY = "day"

# Every axis this kernel knows. `quota` is CLI-only: it is absent from the
# dashboard alert envelope, so six of the seven reach the client.
ALERT_AXES = (
    "weekly",
    "five_hour",
    "budget",
    "codex_budget",
    "project_budget",
    "projected",
    "quota",
)


@dataclass(frozen=True)
class AlertScope:
    """What one alert is about. ``available`` is the discriminator.

    When ``available`` is ``True``, ``withheld_reason`` is ``None`` and both
    window bounds are set. When it is ``False``, ``withheld_reason`` is a
    non-empty sentence a surface can render verbatim, and the bounds are
    ``None``. The other fields are populated as far as the retained evidence
    reaches, so a withheld window can still name its provider.
    """

    available: bool
    withheld_reason: "str | None"
    provider: "str | None"
    account_key: "str | None"
    account_scope: "str | None"
    model_pool: "str | None"
    cost_basis: "str | None"
    window_start: "dt.datetime | None"
    window_end: "dt.datetime | None"
    # How precisely the retained fields fix the bounds. ``instant`` means the
    # row retained a real clock reading. ``day`` means it retained only a
    # calendar date, so the bounds are that date at UTC midnight and a surface
    # must not state a time or a zone it does not have. Meaningful only when
    # ``available``.
    window_granularity: str = WINDOW_INSTANT


def _withheld(reason, *, provider=None, account_key=None, account_scope=None,
              model_pool=None, cost_basis=None) -> AlertScope:
    return AlertScope(
        available=False,
        withheld_reason=reason,
        provider=provider,
        account_key=account_key,
        account_scope=account_scope,
        model_pool=model_pool,
        cost_basis=cost_basis,
        window_start=None,
        window_end=None,
    )


def _parse_instant(value) -> "dt.datetime | None":
    """Read a retained ISO-8601 instant. A naive value means UTC."""
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _parse_calendar_day(value) -> "dt.datetime | None":
    """Read a retained ``YYYY-MM-DD`` calendar day as its UTC midnight."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        day = dt.date.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)


def _parse_epoch_seconds(value) -> "dt.datetime | None":
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _add_calendar_month(instant: dt.datetime) -> dt.datetime:
    """Advance one civil month on the instant's own fields.

    A ``calendar-month`` window start is the local first-of-month converted to
    UTC, so advancing the month field reproduces the next window start exactly
    whenever the zone's offset is unchanged across the boundary. A period that
    straddles a DST transition derives an end one hour away from the instant
    the next window actually starts at; the caveat is recorded in
    ``docs/alerts-gotchas.md``.
    """
    year = instant.year + (1 if instant.month == 12 else 0)
    month = 1 if instant.month == 12 else instant.month + 1
    day = instant.day
    while day > 1:
        try:
            return instant.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    return instant.replace(year=year, month=month, day=1)


def period_window_end(period, window_start) -> "dt.datetime | None":
    """End of the half-open ``[start, end)`` window a budget period names."""
    if window_start is None:
        return None
    if period == "subscription-week":
        return window_start + SUBSCRIPTION_WEEK
    if period == "calendar-week":
        return window_start + CALENDAR_WEEK
    if period == "calendar-month":
        return _add_calendar_month(window_start)
    return None


def _account_scope_for(account_key) -> str:
    return VENDOR_WIDE_SCOPE if account_key == VENDOR_WIDE_KEY else ACCOUNT_SCOPE


def _normalize_account_key(account_key) -> "str | None":
    if account_key is None:
        return None
    text = str(account_key).strip()
    return text or None


def derive_alert_scope(axis, context, account_key=None) -> AlertScope:
    """Derive one alert's scope from ``context`` plus its stamped account.

    ``axis`` is the alert axis; ``context`` is the alert's retained context
    mapping; ``account_key`` is the top-level stamp the row wrote under. No
    argument names or carries an alert identifier.
    """
    ctx = context if isinstance(context, dict) else {}
    key = _normalize_account_key(account_key)
    handler = _AXIS_HANDLERS.get(axis)
    if handler is None:
        return _withheld(f"unknown alert axis: {axis!r}")
    return handler(ctx, key)


def _scope_weekly(ctx, key) -> AlertScope:
    # A subscription week is anchored to a real reset instant, which is what
    # `percent_milestones.week_start_at` retains and what the envelope and the
    # dispatch payload both publish. `week_start_date` is the same week's
    # calendar label and is all a pre-`week_start_at` row has; falling back to
    # it recovers the week but not the reset hour, so the granularity drops
    # with it rather than a midnight instant being invented.
    start = _parse_instant(ctx.get("week_start_at"))
    granularity = WINDOW_INSTANT
    if start is None:
        start = _parse_calendar_day(ctx.get("week_start_date"))
        granularity = WINDOW_DAY
    common = {
        "provider": "claude",
        "account_key": key,
        "account_scope": _account_scope_for(key),
        "cost_basis": "cumulative_cost_usd",
    }
    if start is None:
        return _withheld(
            "the weekly alert retains no week start, so its window cannot be "
            "derived",
            **common,
        )
    return AlertScope(
        available=True,
        withheld_reason=None,
        model_pool=None,
        window_start=start,
        window_end=start + SUBSCRIPTION_WEEK,
        window_granularity=granularity,
        **common,
    )


def _scope_five_hour(ctx, key) -> AlertScope:
    common = {
        "provider": "claude",
        "account_key": key,
        "account_scope": _account_scope_for(key),
        "cost_basis": "block_cost_usd",
    }
    start = _parse_instant(ctx.get("block_start_at"))
    if start is not None:
        return AlertScope(
            available=True,
            withheld_reason=None,
            model_pool=None,
            window_start=start,
            window_end=start + FIVE_HOUR_WINDOW,
            **common,
        )
    # `five_hour_window_key` is the RESET epoch floored to ten minutes
    # (`_canonical_5h_window_key`), so it names the window's END. Reading it as
    # a start would place the window five hours early. The floor also means the
    # start recovered this way is within ten minutes of the recorded block
    # start, which is why the block join is preferred when it resolves.
    end = _parse_epoch_seconds(ctx.get("five_hour_window_key"))
    if end is None:
        return _withheld(
            "the five-hour alert retains no block start, so its window cannot "
            "be derived",
            **common,
        )
    return AlertScope(
        available=True,
        withheld_reason=None,
        model_pool=None,
        window_start=end - FIVE_HOUR_WINDOW,
        window_end=end,
        **common,
    )


def _scope_budget_family(ctx, key, *, provider, default_period) -> AlertScope:
    period = ctx.get("period") or default_period
    start = _parse_instant(ctx.get("period_start_at")) or _parse_instant(
        ctx.get("week_start_at")
    )
    common = {
        "provider": provider,
        "account_key": key,
        "account_scope": _account_scope_for(key),
        "cost_basis": "spent_usd",
    }
    if start is None:
        return _withheld(
            "the budget alert retains no period start, so its window cannot "
            "be derived",
            **common,
        )
    end = period_window_end(period, start)
    if end is None:
        return _withheld(
            f"the budget alert names no derivable period (got {period!r})",
            **common,
        )
    return AlertScope(
        available=True,
        withheld_reason=None,
        model_pool=None,
        window_start=start,
        window_end=end,
        **common,
    )


def _scope_budget(ctx, key) -> AlertScope:
    return _scope_budget_family(
        ctx, key, provider="claude", default_period="subscription-week"
    )


def _scope_codex_budget(ctx, key) -> AlertScope:
    # No default: Codex has no subscription week, and guessing between the two
    # calendar periods would double or halve the window.
    return _scope_budget_family(
        ctx, key, provider="codex", default_period=None
    )


def _scope_project_budget(ctx, key) -> AlertScope:
    # `project_budget_milestones` is deliberately account-blind and stamped
    # `*`, so the scope is vendor-wide whatever the caller passes. Narrowing it
    # to an account would invent an attribution the row never recorded.
    period = ctx.get("period") or "subscription-week"
    start = _parse_instant(ctx.get("week_start_at")) or _parse_instant(
        ctx.get("period_start_at")
    )
    common = {
        "provider": "claude",
        "account_key": key,
        "account_scope": VENDOR_WIDE_SCOPE,
        "cost_basis": "spent_usd",
    }
    if start is None:
        return _withheld(
            "the project-budget alert retains no window start, so its window "
            "cannot be derived",
            **common,
        )
    end = period_window_end(period, start)
    if end is None:
        return _withheld(
            f"the project-budget alert names no derivable period (got "
            f"{period!r})",
            **common,
        )
    return AlertScope(
        available=True,
        withheld_reason=None,
        model_pool=None,
        window_start=start,
        window_end=end,
        **common,
    )


def _scope_projected(ctx, key) -> AlertScope:
    metric = ctx.get("metric")
    provider = "codex" if metric == "codex_budget_usd" else "claude"
    # `weekly_pct` projects against the percent cap, which retains no dollar
    # basis; the two budget metrics project a dollar figure.
    cost_basis = None if metric == "weekly_pct" else "projected_value"
    common = {
        "provider": provider,
        "account_key": key,
        "account_scope": _account_scope_for(key),
        "cost_basis": cost_basis,
    }
    start = _parse_instant(ctx.get("week_start_at")) or _parse_instant(
        ctx.get("period_start_at")
    )
    if start is None:
        return _withheld(
            "the projected alert retains no period start, so its window "
            "cannot be derived",
            **common,
        )
    # The metric fixes the period for `weekly_pct` — a weekly-percent
    # projection is a subscription week by construction. The budget metrics do
    # not: a Claude budget runs on any of three periods and a Codex budget on
    # either calendar period, so an absent `period` is withheld rather than
    # guessed.
    period = ctx.get("period")
    if period is None and metric == "weekly_pct":
        period = "subscription-week"
    end = period_window_end(period, start)
    if end is None:
        return _withheld(
            f"the projected alert retains no period for metric {metric!r}, so "
            "its window length is unknown",
            **common,
        )
    return AlertScope(
        available=True,
        withheld_reason=None,
        model_pool=None,
        window_start=start,
        window_end=end,
        **common,
    )


def _scope_quota(ctx, key) -> AlertScope:
    source = ctx.get("source")
    provider = str(source) if isinstance(source, str) and source.strip() else None
    pool = _lib_codex_pools.codex_key_model_pool(ctx.get("logical_limit_key"))
    common = {
        "provider": provider,
        "account_key": key,
        "account_scope": _account_scope_for(key),
        # `quota_threshold_events` retains source, root, account, logical
        # limit, slot, duration and reset — and no dollar cost basis. Where a
        # cost is needed it is recomputed, never assumed retained.
        "cost_basis": None,
    }
    end = _parse_instant(ctx.get("resets_at_utc"))
    minutes = ctx.get("window_minutes")
    if end is None:
        return _withheld(
            "the quota alert retains no reset instant, so its window cannot "
            "be derived",
            model_pool=pool,
            **common,
        )
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return _withheld(
            "the quota alert retains no window duration, so its window start "
            "cannot be derived",
            model_pool=pool,
            **common,
        )
    if int(minutes) <= 0:
        return _withheld(
            f"the quota alert names a non-positive window duration "
            f"({int(minutes)} minutes)",
            model_pool=pool,
            **common,
        )
    return AlertScope(
        available=True,
        withheld_reason=None,
        model_pool=pool,
        window_start=end - dt.timedelta(minutes=int(minutes)),
        window_end=end,
        **common,
    )


_AXIS_HANDLERS = {
    "weekly": _scope_weekly,
    "five_hour": _scope_five_hour,
    "budget": _scope_budget,
    "codex_budget": _scope_codex_budget,
    "project_budget": _scope_project_budget,
    "projected": _scope_projected,
    "quota": _scope_quota,
}


# ─────────────────────────── the remediation idiom ──────────────────────────
# One affordance shape for every warning state in the product, alert bodies
# and CLI reports alike. `doctor` already renders `→ <remediation>`; this keeps
# the arrow and fixes the verb so a reader learns exactly one form.

def inclusive_last_day(window_end: dt.datetime) -> "dt.date":
    """The last calendar day a half-open ``[start, end)`` window touches.

    `--since` / `--until` selectors are inclusive whole days, so a window
    ending at 2026-06-02 14:00 still contains fourteen hours of 2026-06-02 and
    must name it. Subtracting a whole day is only correct when the end lands
    exactly on midnight, which a subscription week anchored to a real reset
    instant almost never does.
    """
    return (window_end - dt.timedelta(microseconds=1)).date()


def format_scope_detail(provider, window_start, window_end, tz,
                        granularity=WINDOW_INSTANT) -> "str | None":
    """The scope statement a next-step line ends with.

    Both instants go through ``format_display_dt`` (chokepoint rule); only the
    end carries the zone label, because both bounds are in the same zone. A
    command's own arguments are NOT rendered here — those stay in UTC, which
    is what the selectors they feed accept.

    ``WINDOW_DAY`` renders bare calendar dates and no zone label. The bounds
    then came from a retained ``YYYY-MM-DD``, so a clock reading would be
    invented; and converting that day's UTC midnight into a non-UTC display
    zone would move the printed date off the calendar day the row recorded
    (the same shift ``_alert_text_weekly`` avoids for ``week_start_date``).
    """
    parts = []
    if provider:
        parts.append(str(provider))
    if window_start is not None and window_end is not None:
        if granularity == WINDOW_DAY:
            parts.append(f"{window_start:%Y-%m-%d} → {window_end:%Y-%m-%d}")
        else:
            start = _format_display_dt(
                window_start, tz, fmt="%Y-%m-%d %H:%M", suffix=False
            )
            end = _format_display_dt(
                window_end, tz, fmt="%Y-%m-%d %H:%M", suffix=True
            )
            parts.append(f"{start} → {end}")
    return " · ".join(parts) or None


def next_step_line(command: "str | None", *, provider=None, window_start=None,
                   window_end=None, tz=None, granularity=WINDOW_INSTANT,
                   unavailable_reason: "str | None" = None) -> str:
    """Render the affordance for one warning state, scope statement included.

    This is the single entry point every warning state uses — CLI reports and
    alert bodies alike — so a reader learns the form once.
    """
    return format_next_step(
        command,
        detail=format_scope_detail(
            provider, window_start, window_end, tz, granularity
        ),
        unavailable_reason=unavailable_reason,
    )


def format_next_step(command: "str | None", *, detail: "str | None" = None,
                     unavailable_reason: "str | None" = None) -> str:
    """Render one next-step line.

    With a ``command``, the line offers it and appends ``detail`` (the scope
    the command addresses) after an em dash. Without one, the line states why
    no scoped explanation is available — a closed or underivable window says
    so and offers nothing, rather than silently substituting a window the
    alert never described.
    """
    if command:
        line = f"→ Run `{command}`"
        return f"{line} — {detail}" if detail else line
    reason = unavailable_reason or "the window this alert describes is not addressable"
    return f"→ No scoped explanation: {reason}"


UNRETAINED_FIVE_HOUR_BLOCK = (
    "the block this alert names is no longer retained, so its five-hour "
    "detail cannot be re-opened"
)

# `cctally budget` bare status resolves `--vendor` and `--period` and then
# consumes them only in its `set` / `unset` branches: the read path takes the
# period from config (`budget_cfg["period"]`), and the command carries no
# historical selector at all. Naming those flags would state a scope the
# command cannot honour, so the whole budget family offers the bare form.
LIVE_PERIOD_ONLY_COMMAND = "cctally budget"

# `cctally forecast` is the same shape for the same reason: its only time knob
# is `--as-of`, registered `argparse.SUPPRESS` as a test hook rather than a
# user-facing selector, so the command reports whichever subscription week is
# live when it runs.
LIVE_WEEK_ONLY_COMMAND = "cctally forecast --explain"

CLOSED_BUDGET_PERIOD = (
    "the budget period this alert names has already closed, and `cctally "
    "budget` reports only the live period"
)

CLOSED_FORECAST_WEEK = (
    "the week this alert names has already closed, and `cctally forecast` "
    "reports only the live week"
)

# Which command reports the live window only, and what to say once the
# alert's own window has closed. One table so a new live-window command
# cannot be classified without also stating its cause.
_LIVE_WINDOW_ONLY_COMMANDS = {
    LIVE_PERIOD_ONLY_COMMAND: CLOSED_BUDGET_PERIOD,
    LIVE_WEEK_ONLY_COMMAND: CLOSED_FORECAST_WEEK,
}


def command_reports_the_live_period_only(command) -> bool:
    """True when ``command`` addresses whichever period is live when it runs.

    Following such a command from an alert whose period has closed would
    report the current window under a line that names the closed one — the
    silent substitution the as-of contract forbids. Deciding that a period HAS
    closed needs a clock, which this module never reads, so the caller does
    the comparison; this states only which commands the question applies to.
    """
    return command in _LIVE_WINDOW_ONLY_COMMANDS


def closed_window_reason(command) -> str:
    """Why a live-window command is withheld once its window has closed."""
    return _LIVE_WINDOW_ONLY_COMMANDS.get(command, CLOSED_BUDGET_PERIOD)


def alert_target_unavailable_reason(axis, context, scope: AlertScope) -> str:
    """Why no command is offered. One place, so every surface says the same.

    A withheld scope states its own cause. An available scope with no command
    means the window is known but its target is gone, which today is only the
    purged five-hour block.
    """
    if scope.withheld_reason:
        return scope.withheld_reason
    if axis == "five_hour":
        return UNRETAINED_FIVE_HOUR_BLOCK
    return "the window this alert describes is not addressable"


def alert_next_step_command(axis, context, scope: AlertScope) -> "str | None":
    """The command that explains one alert axis, scoped to its own window.

    Returns ``None`` when the scope is withheld, because every command below
    needs the window the scope failed to derive — except the projected axis,
    whose targets address the live window by construction and so carry no
    window selector.
    """
    ctx = context if isinstance(context, dict) else {}
    if axis == "five_hour" and not _parse_instant(ctx.get("block_start_at")):
        # The window still derives from the retained reset key, but the block
        # row it names has been purged, so `--block-start` would select
        # nothing. Knowing the window and being able to re-open its detail are
        # different claims, and only the second one is what a command asserts.
        return None
    if axis == "projected":
        if ctx.get("metric") in ("budget_usd", "codex_budget_usd"):
            return LIVE_PERIOD_ONLY_COMMAND
        return LIVE_WEEK_ONLY_COMMAND
    if not scope.available:
        return None
    start = scope.window_start
    end = scope.window_end
    if axis == "weekly":
        return f"cctally percent-breakdown --week-start {start:%Y-%m-%d}"
    if axis == "five_hour":
        return f"cctally five-hour-breakdown --block-start {start:%Y-%m-%dT%H:%M}"
    if axis in ("budget", "codex_budget"):
        return LIVE_PERIOD_ONLY_COMMAND
    if axis == "project_budget":
        # `--until` is inclusive, so the half-open end renders as the last
        # calendar day the window touches.
        last_day = inclusive_last_day(end)
        project = ctx.get("project")
        selector = f" --project {project}" if project else ""
        return (
            f"cctally project --since {start:%Y-%m-%d} "
            f"--until {last_day:%Y-%m-%d}{selector}"
        )
    if axis == "quota":
        root = ctx.get("source_root_key")
        limit = ctx.get("logical_limit_key")
        parts = [f"cctally codex quota breakdown --reset-at {end:%Y-%m-%dT%H:%M:%SZ}"]
        if root:
            parts.append(f"--root-key {root}")
        if limit:
            parts.append(f"--limit-key {limit}")
        return " ".join(parts)
    return None
