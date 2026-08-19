"""The three alert axes that reached the dashboard envelope through zero
goldens (#620 S1 Task 15, audit finding F17).

`weekly`, `five_hour` and `project_budget` had no fixture at all, and the
first two are the axes a default install actually fires. Each new scenario
carries a live row and a closed or purged one, and this module reads the
committed envelope goldens and asserts the scope derived from them: the
provider, the account and its scope, the model pool, the cost basis, the exact
half-open `[start, end)`, and the case where nothing can be offered.

Asserting only that a row renders would pass over a wrong window, so every
bound is compared to an exact instant.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_BIN = _ROOT / "bin"
_GOLDENS = _ROOT / "tests" / "fixtures" / "dashboard"
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


def _utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _alerts(scenario: str) -> list[dict]:
    path = _GOLDENS / scenario / "golden-data.json"
    assert path.is_file(), f"missing golden: {path}"
    rows = json.loads(path.read_text()).get("alerts")
    assert rows, f"{scenario} published no alert rows"
    return rows


def _by_threshold(rows: list[dict], threshold: int) -> dict:
    matching = [r for r in rows if r["threshold"] == threshold]
    assert len(matching) == 1, (
        f"expected exactly one row at threshold {threshold}, got {len(matching)}"
    )
    return matching[0]


def _scope(scope_mod, row):
    return scope_mod.derive_alert_scope(
        row["axis"], row.get("context") or {}, row.get("accountKey")
    )


# ─────────────────────────────── weekly ────────────────────────────────

def test_weekly_axis_reaches_the_envelope_with_both_windows(scope_mod):
    rows = _alerts("alerts-weekly")
    assert {r["axis"] for r in rows} == {"weekly"}
    assert len(rows) == 3, (
        "the scenario must carry a live week, a closed week, and a row that "
        "predates `week_start_at`"
    )


def test_weekly_live_window_derives_exactly(scope_mod):
    """The subscription week runs reset-to-reset. Every dashboard fixture
    anchors at 14:00 UTC, so a window that begins at midnight is the
    reset-hour offset, not a rounding of it."""
    row = _by_threshold(_alerts("alerts-weekly"), 90)
    assert row["context"]["week_start_at"] == "2026-04-13T14:00:00Z", (
        "the envelope must publish the retained reset instant, not only the "
        "calendar day"
    )
    s = _scope(scope_mod, row)
    assert s.available is True
    assert s.withheld_reason is None
    assert s.provider == "claude"
    assert s.account_scope == "account"
    assert s.model_pool is None
    assert s.cost_basis == "cumulative_cost_usd"
    assert s.window_granularity == "instant"
    assert s.window_start == _utc("2026-04-13T14:00:00Z")
    assert s.window_end == _utc("2026-04-20T14:00:00Z")


def test_weekly_closed_window_keeps_its_own_window(scope_mod):
    """A closed week still states the week it fired against. Substituting the
    live one would be the silent scope change the epic forbids."""
    s = _scope(scope_mod, _by_threshold(_alerts("alerts-weekly"), 95))
    assert s.available is True
    assert s.window_start == _utc("2026-04-06T14:00:00Z")
    assert s.window_end == _utc("2026-04-13T14:00:00Z")
    assert s.window_end <= _utc("2026-04-16T14:00:00Z"), (
        "the second row must describe a window that has already closed"
    )


def test_weekly_row_without_a_retained_instant_states_no_hour(scope_mod):
    """`percent_milestones.week_start_at` is nullable and post-dates the
    table, so a historical row carries only the week's calendar date. The
    envelope publishes the empty string for it, and the reader names the right
    week at DAY granularity rather than placing both bounds at a UTC midnight
    the row never recorded."""
    row = _by_threshold(_alerts("alerts-weekly"), 100)
    assert row["context"]["week_start_at"] == "", (
        "the pre-column fixture row must publish an empty week start instant"
    )
    s = _scope(scope_mod, row)
    assert s.available is True
    assert s.window_granularity == "day"
    assert s.window_start == _utc("2026-03-30T00:00:00Z")
    assert s.window_end == _utc("2026-04-06T00:00:00Z")
    assert scope_mod.format_scope_detail(
        s.provider, s.window_start, s.window_end, dt.timezone.utc,
        s.window_granularity,
    ) == "claude · 2026-03-30 → 2026-04-06"


# ────────────────────────────── five_hour ──────────────────────────────

def test_five_hour_axis_reaches_the_envelope_with_both_windows(scope_mod):
    rows = _alerts("alerts-five-hour")
    assert {r["axis"] for r in rows} == {"five_hour"}
    assert len(rows) == 2


def test_five_hour_retained_block_derives_exactly(scope_mod):
    row = _by_threshold(_alerts("alerts-five-hour"), 90)
    assert row["context"]["block_start_at"] == "2026-04-16T09:30:00Z"
    s = _scope(scope_mod, row)
    assert s.available is True
    assert s.provider == "claude"
    assert s.account_scope == "account"
    assert s.model_pool is None
    assert s.cost_basis == "block_cost_usd"
    assert s.window_start == _utc("2026-04-16T09:30:00Z")
    assert s.window_end == _utc("2026-04-16T14:30:00Z")


def test_five_hour_purged_block_still_names_its_window(scope_mod):
    """The block row is gone, so the envelope's join yields an empty
    `block_start_at`. The retained window key is the floored RESET epoch, so
    the window is still known — the kernel says so rather than pretending
    otherwise."""
    row = _by_threshold(_alerts("alerts-five-hour"), 95)
    assert row["context"]["block_start_at"] == "", (
        "the purged-block fixture must publish an empty block start"
    )
    s = _scope(scope_mod, row)
    assert s.available is True
    assert s.window_end == dt.datetime.fromtimestamp(
        row["context"]["five_hour_window_key"], tz=dt.timezone.utc
    )
    assert s.window_start == s.window_end - dt.timedelta(hours=5)


def test_five_hour_purged_block_offers_no_command(scope_mod):
    """Knowing the window and being able to re-open its detail are different
    claims. `--block-start` would select a block that no longer exists, so no
    command is offered and the reason is stated."""
    row = _by_threshold(_alerts("alerts-five-hour"), 95)
    s = _scope(scope_mod, row)
    command = scope_mod.alert_next_step_command(row["axis"], row["context"], s)
    assert command is None
    reason = scope_mod.alert_target_unavailable_reason(
        row["axis"], row["context"], s
    )
    assert "no longer retained" in reason
    assert scope_mod.format_next_step(None, unavailable_reason=reason) == (
        f"→ No scoped explanation: {reason}"
    )


def test_five_hour_retained_block_does_offer_a_command(scope_mod):
    """The negative above is only meaningful because the positive holds."""
    row = _by_threshold(_alerts("alerts-five-hour"), 90)
    s = _scope(scope_mod, row)
    assert scope_mod.alert_next_step_command(row["axis"], row["context"], s) == (
        "cctally five-hour-breakdown --block-start 2026-04-16T09:30"
    )


# ─────────────────────────── project_budget ────────────────────────────

def test_project_budget_axis_reaches_the_envelope_with_both_windows(scope_mod):
    rows = _alerts("alerts-project-budget")
    assert {r["axis"] for r in rows} == {"project_budget"}
    assert len(rows) == 2


@pytest.mark.parametrize(
    "threshold,start,end",
    [
        (90, "2026-04-13T14:00:00Z", "2026-04-20T14:00:00Z"),
        (100, "2026-04-06T14:00:00Z", "2026-04-13T14:00:00Z"),
    ],
)
def test_project_budget_rows_are_vendor_wide(scope_mod, threshold, start, end):
    """`project_budget_milestones` is account-blind and stamped `*`, so the
    scope must present as vendor-wide however the row reaches the client."""
    row = _by_threshold(_alerts("alerts-project-budget"), threshold)
    assert "accountKey" not in row, (
        "R8: a single-account fixture must publish no account decoration"
    )
    s = _scope(scope_mod, row)
    assert s.available is True
    assert s.provider == "claude"
    assert s.account_scope == "vendor_wide"
    assert s.model_pool is None
    assert s.cost_basis == "spent_usd"
    assert s.window_start == _utc(start)
    assert s.window_end == _utc(end)


def test_project_budget_command_carries_the_project_and_the_window(scope_mod):
    row = _by_threshold(_alerts("alerts-project-budget"), 90)
    s = _scope(scope_mod, row)
    assert scope_mod.alert_next_step_command(row["axis"], row["context"], s) == (
        "cctally project --since 2026-04-13 --until 2026-04-20 "
        "--project fixture-alerts"
    )


# ─────────────────────── the model-pool statement ──────────────────────

def test_no_dashboard_axis_carries_a_model_pool(scope_mod):
    """Model pools classify Codex quota windows, and `quota` is the one axis
    absent from the dashboard alert envelope. Every row these goldens publish
    must therefore report no pool — stated here so a future change that starts
    inventing one fails."""
    for scenario in (
        "alerts-weekly", "alerts-five-hour", "alerts-project-budget",
    ):
        for row in _alerts(scenario):
            assert _scope(scope_mod, row).model_pool is None, scenario
