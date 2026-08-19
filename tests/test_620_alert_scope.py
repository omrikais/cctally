"""Scope derivation for every alert axis (#620 S1, spec D11).

`derive_alert_scope` reads only the fields an alert already retains and
returns an `AlertScope` that always says which state it is in. It never
reads the alert `id`: the client contract calls the id opaque, the journal
contract forbids parsing event ids, and Claude alert ids omit the account
entirely, so the id could not carry the scope even if parsing it were
allowed.
"""
from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import inspect
import pathlib
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


def _load(name):
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(_BIN / f"{name}.py"))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def scope_mod():
    return _load("_lib_alert_scope")


_CLOCK_ATTRS = (
    "now", "utcnow", "today", "time", "monotonic", "perf_counter",
    "time_ns", "monotonic_ns",
)


def _clock_calls_in(path: pathlib.Path) -> list:
    """Every wall-clock read in ``path``, by call name.

    `datetime.fromtimestamp` is deliberately absent: it converts a RETAINED
    epoch (`five_hour_window_key`) and reads no clock.
    """
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in _CLOCK_ATTRS:
            found.add(node.func.attr)
        elif isinstance(node.func, ast.Name) and node.func.id in (
            "time", "monotonic", "perf_counter",
        ):
            found.add(node.func.id)
    return sorted(found)


def _utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


# A fully-populated context per axis. Every entry must derive; the empty
# counterpart in `test_withheld_always_states_a_reason` must not.
POPULATED = {
    # Production shape: `percent_milestones` retains the week's reset INSTANT
    # and both write paths publish it, so the instant is the default here and
    # the date-only form is the exception a pre-column row reaches.
    "weekly": (
        {
            "week_start_date": "2026-04-27",
            "week_start_at": "2026-04-27T09:00:00+00:00",
            "cumulative_cost_usd": 42.5,
            "dollars_per_percent": 0.47,
        },
        "acct-claude-1",
    ),
    "five_hour": (
        {
            "five_hour_window_key": 1777732200,
            "block_start_at": "2026-04-29T14:30:00Z",
            "block_cost_usd": 3.87,
            "primary_model": "claude-sonnet-4-6",
        },
        "acct-claude-1",
    ),
    "budget": (
        {
            "week_start_at": "2026-04-27T09:00:00+00:00",
            "period": "subscription-week",
            "period_start_at": "2026-04-27T09:00:00+00:00",
            "budget_usd": 300.0,
            "spent_usd": 275.0,
            "consumption_pct": 91.6,
        },
        "*",
    ),
    "codex_budget": (
        {
            "period": "calendar-month",
            "period_start_at": "2026-06-01T00:00:00+00:00",
            "budget_usd": 120.0,
            "spent_usd": 118.0,
            "consumption_pct": 98.3,
        },
        "*",
    ),
    "project_budget": (
        {
            "week_start_at": "2026-04-27T09:00:00+00:00",
            "project": "cctally-dev",
            "project_key": "/home/dev/repos/cctally-dev",
            "budget_usd": 25.0,
            "spent_usd": 26.0,
            "consumption_pct": 104.0,
        },
        "*",
    ),
    "projected": (
        {
            "week_start_at": "2026-04-27T09:00:00+00:00",
            "metric": "weekly_pct",
            "projected_value": 95.0,
            "denominator": 100.0,
        },
        "acct-claude-1",
    ),
    "quota": (
        {
            "source": "codex",
            "source_root_key": "/Users/me/.codex",
            "logical_limit_key": '{"limitId":"weekly"}',
            "observed_slot": "weekly",
            "window_minutes": 10080,
            "resets_at_utc": "2026-05-04T09:00:00Z",
            "kind": "actual",
            "qualifying_percent": 91.0,
            "projected_percent": None,
        },
        "acct-codex-1",
    ),
}

ALL_AXES = tuple(POPULATED)


def test_weekly_window_is_the_subscription_week(scope_mod):
    ctx, key = POPULATED["weekly"]
    s = scope_mod.derive_alert_scope("weekly", ctx, key)
    assert s.available is True
    assert s.withheld_reason is None
    assert s.provider == "claude"
    assert s.account_key == "acct-claude-1"
    assert s.account_scope == "account"
    assert s.model_pool is None
    assert s.cost_basis == "cumulative_cost_usd"
    assert s.window_start == _utc("2026-04-27T09:00:00Z")
    assert s.window_end == _utc("2026-05-04T09:00:00Z")
    # A subscription week runs reset to reset, so the retained instant is what
    # fixes the bounds and the granularity says the row really carried one.
    assert s.window_granularity == scope_mod.WINDOW_INSTANT


def test_weekly_prefers_the_retained_instant_over_the_calendar_date(scope_mod):
    """The two fields name the same week, and only the instant names the hour.
    The date is deliberately a DIFFERENT day here, so a reader that took the
    date would land a day early rather than merely at the wrong hour."""
    ctx = dict(POPULATED["weekly"][0])
    ctx["week_start_date"] = "2026-04-26"
    s = scope_mod.derive_alert_scope("weekly", ctx, "acct-claude-1")
    assert s.window_start == _utc("2026-04-27T09:00:00Z")
    assert s.window_end == _utc("2026-05-04T09:00:00Z")
    assert s.window_granularity == scope_mod.WINDOW_INSTANT


def test_weekly_falls_back_to_the_calendar_date_at_day_granularity(scope_mod):
    """`percent_milestones.week_start_at` is nullable and post-dates the
    table, so a historical row carries only the week's calendar label. The
    week is still recovered, but the reset hour is not, and the granularity
    drops with it rather than a midnight instant being invented."""
    ctx = dict(POPULATED["weekly"][0])
    del ctx["week_start_at"]
    s = scope_mod.derive_alert_scope("weekly", ctx, "acct-claude-1")
    assert s.available is True
    assert s.window_start == _utc("2026-04-27T00:00:00Z")
    assert s.window_end == _utc("2026-05-04T00:00:00Z")
    assert s.window_granularity == scope_mod.WINDOW_DAY


def test_five_hour_window_is_five_hours_from_the_block_start(scope_mod):
    ctx, key = POPULATED["five_hour"]
    s = scope_mod.derive_alert_scope("five_hour", ctx, key)
    assert s.available is True
    assert s.provider == "claude"
    assert s.account_scope == "account"
    assert s.cost_basis == "block_cost_usd"
    assert s.window_start == _utc("2026-04-29T14:30:00Z")
    assert s.window_end == _utc("2026-04-29T19:30:00Z")


def test_five_hour_falls_back_to_the_retained_window_key(scope_mod):
    """The retained key is the floored RESET epoch, so it names the window's
    END. Reading it as a start would place the whole window five hours early,
    and the block join — which does record the start — is preferred whenever
    the block still exists."""
    ctx = dict(POPULATED["five_hour"][0])
    del ctx["block_start_at"]
    s = scope_mod.derive_alert_scope("five_hour", ctx, "acct-claude-1")
    assert s.available is True
    assert s.window_end == dt.datetime.fromtimestamp(
        1777732200, tz=dt.timezone.utc
    )
    assert s.window_start == s.window_end - dt.timedelta(hours=5)


def test_five_hour_prefers_the_block_start_over_the_reset_key(scope_mod):
    ctx, key = POPULATED["five_hour"]
    s = scope_mod.derive_alert_scope("five_hour", ctx, key)
    from_key = dt.datetime.fromtimestamp(
        ctx["five_hour_window_key"], tz=dt.timezone.utc
    )
    assert s.window_start == _utc("2026-04-29T14:30:00Z")
    assert s.window_start != from_key


@pytest.mark.parametrize(
    "period,start,expected_end",
    [
        ("subscription-week", "2026-04-27T09:00:00+00:00", "2026-05-04T09:00:00Z"),
        ("calendar-week", "2026-06-01T07:00:00+00:00", "2026-06-08T07:00:00Z"),
        ("calendar-month", "2026-06-01T07:00:00+00:00", "2026-07-01T07:00:00Z"),
        ("calendar-month", "2026-12-01T00:00:00+00:00", "2027-01-01T00:00:00Z"),
    ],
)
def test_budget_window_derives_from_period_and_start(
    scope_mod, period, start, expected_end
):
    ctx = {
        "week_start_at": start,
        "period": period,
        "period_start_at": start,
        "budget_usd": 300.0,
        "spent_usd": 275.0,
        "consumption_pct": 91.6,
    }
    s = scope_mod.derive_alert_scope("budget", ctx, "*")
    assert s.available is True
    assert s.provider == "claude"
    assert s.account_scope == "vendor_wide"
    assert s.cost_basis == "spent_usd"
    assert s.window_start == _utc(start)
    assert s.window_end == _utc(expected_end)


@pytest.mark.parametrize(
    "period,start,expected_end",
    [
        ("calendar-week", "2026-06-01T07:00:00+00:00", "2026-06-08T07:00:00Z"),
        ("calendar-month", "2026-06-01T07:00:00+00:00", "2026-07-01T07:00:00Z"),
    ],
)
def test_codex_budget_window_derives_from_period_and_start(
    scope_mod, period, start, expected_end
):
    ctx = {
        "period": period,
        "period_start_at": start,
        "budget_usd": 120.0,
        "spent_usd": 118.0,
        "consumption_pct": 98.3,
    }
    s = scope_mod.derive_alert_scope("codex_budget", ctx, "*")
    assert s.available is True
    assert s.provider == "codex"
    assert s.account_scope == "vendor_wide"
    assert s.cost_basis == "spent_usd"
    assert s.window_start == _utc(start)
    assert s.window_end == _utc(expected_end)


def test_budget_with_a_real_account_key_is_account_scoped(scope_mod):
    ctx = dict(POPULATED["budget"][0])
    s = scope_mod.derive_alert_scope("budget", ctx, "acct-claude-1")
    assert s.account_scope == "account"
    assert s.account_key == "acct-claude-1"


@pytest.mark.parametrize(
    "metric,provider,cost_basis,period,start,expected_end",
    [
        (
            "weekly_pct",
            "claude",
            None,
            None,
            "2026-04-27T09:00:00+00:00",
            "2026-05-04T09:00:00Z",
        ),
        (
            "budget_usd",
            "claude",
            "projected_value",
            "calendar-month",
            "2026-04-01T07:00:00+00:00",
            "2026-05-01T07:00:00Z",
        ),
        (
            "codex_budget_usd",
            "codex",
            "projected_value",
            "calendar-week",
            "2026-04-27T07:00:00+00:00",
            "2026-05-04T07:00:00Z",
        ),
    ],
)
def test_projected_window_derives_for_each_metric(
    scope_mod, metric, provider, cost_basis, period, start, expected_end
):
    ctx = {
        "week_start_at": start,
        "metric": metric,
        "projected_value": 95.0,
        "denominator": 100.0,
    }
    if period is not None:
        ctx["period"] = period
    s = scope_mod.derive_alert_scope("projected", ctx, "acct-1")
    assert s.available is True
    assert s.provider == provider
    assert s.cost_basis == cost_basis
    assert s.window_start == _utc(start)
    assert s.window_end == _utc(expected_end)


def test_projected_budget_metric_without_a_period_is_withheld(scope_mod):
    """`projected_milestones` retains `period`, but the alert context does not
    always carry it. A Claude budget can run on any of three periods, so the
    window length is genuinely unknown and must not be guessed."""
    ctx = {
        "week_start_at": "2026-04-27T09:00:00+00:00",
        "metric": "budget_usd",
        "projected_value": 310.0,
        "denominator": 300.0,
    }
    s = scope_mod.derive_alert_scope("projected", ctx, "*")
    assert s.available is False
    assert isinstance(s.withheld_reason, str) and s.withheld_reason
    assert s.window_end is None
    # The provider is still known even though the window is not.
    assert s.provider == "claude"


def test_project_budget_is_vendor_wide_because_the_row_is_stamped_star(scope_mod):
    ctx, key = POPULATED["project_budget"]
    s = scope_mod.derive_alert_scope("project_budget", ctx, key)
    assert s.available is True
    assert s.provider == "claude"
    assert s.account_scope == "vendor_wide"
    assert s.cost_basis == "spent_usd"
    assert s.window_start == _utc("2026-04-27T09:00:00Z")
    assert s.window_end == _utc("2026-05-04T09:00:00Z")


def test_project_budget_stays_vendor_wide_even_with_a_real_key(scope_mod):
    """Inventing an account for an account-blind row would be a fabrication."""
    ctx, _ = POPULATED["project_budget"]
    s = scope_mod.derive_alert_scope("project_budget", ctx, "acct-claude-1")
    assert s.account_scope == "vendor_wide"


def test_quota_window_derives_and_retains_no_cost_basis(scope_mod):
    ctx, key = POPULATED["quota"]
    s = scope_mod.derive_alert_scope("quota", ctx, key)
    assert s.available is True
    assert s.provider == "codex"
    assert s.account_key == "acct-codex-1"
    assert s.account_scope == "account"
    assert s.cost_basis is None, "quota_threshold_events retains no dollar basis"
    assert s.window_end == _utc("2026-05-04T09:00:00Z")
    assert s.window_start == _utc("2026-05-04T09:00:00Z") - dt.timedelta(
        minutes=10080
    )
    assert s.model_pool is None


def test_quota_model_pool_comes_from_the_pool_kernel(scope_mod):
    ctx = dict(POPULATED["quota"][0])
    ctx["logical_limit_key"] = '{"limitId":"weekly","modelPool":"gpt-5.3-codex-spark"}'
    s = scope_mod.derive_alert_scope("quota", ctx, "acct-codex-1")
    assert s.model_pool == "gpt-5.3-codex-spark"
    pools = _load("_lib_codex_pools")
    assert pools.is_model_scoped_codex_quota(ctx["logical_limit_key"], None) is True


def test_scope_is_never_derived_from_the_alert_id(scope_mod):
    params = inspect.signature(scope_mod.derive_alert_scope).parameters
    assert "id" not in params
    assert "alert_id" not in params

    src = pathlib.Path(scope_mod.__file__).read_text()
    tree = ast.parse(src)
    id_literals = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and n.value in ("id", "alert_id")
    ]
    assert not id_literals, "the kernel must never read an alert id"
    splitters = [
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("split", "rsplit", "partition", "rpartition")
    ]
    assert not splitters, f"id-shaped string splitting found: {splitters}"


def test_the_kernel_never_reads_a_clock(scope_mod):
    """The module header states "no clock read", and
    `command_reports_the_live_period_only` rests its whole design on it: the
    kernel classifies WHICH commands report the live window only, and the
    caller — which does hold a clock — decides whether a given window has
    closed. Nothing tested that claim, so it is pinned structurally here, the
    same way the no-alert-id rule above is.

    `datetime.fromtimestamp` is deliberately absent from the list: it converts
    a RETAINED epoch (`five_hour_window_key`) and reads no clock.
    """
    assert _clock_calls_in(pathlib.Path(scope_mod.__file__)) == [], (
        "clock read in a no-clock kernel"
    )
    # Non-vacuity: the same walk must FIND the clock read the design puts one
    # level up, in the caller that decides whether a window has closed.
    # Membership, not equality: this test owns the kernel, so it must not fail
    # when the caller legitimately gains or loses an unrelated clock call.
    assert "now" in _clock_calls_in(_BIN / "_lib_alerts_payload.py"), (
        "the walk found no clock in the module that is supposed to hold one, "
        "so it cannot detect one appearing in the kernel either"
    )

    imported = set()
    for node in ast.walk(ast.parse(pathlib.Path(scope_mod.__file__).read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "time" not in imported, "the kernel must not import a clock source"
    assert "os" not in imported, (
        "the kernel must not read the environment either — that is I/O"
    )


@pytest.mark.parametrize("axis", ALL_AXES)
def test_withheld_always_states_a_reason(scope_mod, axis):
    ctx, key = POPULATED[axis]

    populated = scope_mod.derive_alert_scope(axis, ctx, key)
    assert populated.available is True, f"{axis} must derive from a full context"
    assert populated.withheld_reason is None
    assert populated.window_start is not None
    assert populated.window_end is not None
    assert populated.window_end > populated.window_start
    assert populated.provider in ("claude", "codex")
    assert populated.account_scope in ("account", "vendor_wide")

    empty = scope_mod.derive_alert_scope(axis, {}, key)
    assert empty.available is False, f"{axis} must withhold on an empty context"
    assert isinstance(empty.withheld_reason, str)
    assert empty.withheld_reason.strip(), f"{axis} withheld without a reason"
    assert empty.window_start is None
    assert empty.window_end is None


def test_unknown_axis_is_withheld_with_a_reason(scope_mod):
    s = scope_mod.derive_alert_scope("no_such_axis", {}, None)
    assert s.available is False
    assert "no_such_axis" in s.withheld_reason


def test_alert_scope_is_frozen(scope_mod):
    s = scope_mod.derive_alert_scope("weekly", *POPULATED["weekly"])
    with pytest.raises(Exception):
        s.available = False
