"""Every config validation error names the leaf it is about (#513 S1, spec §1.6).

``_AlertsConfigError`` and ``_BudgetConfigError`` gain a keyword-only ``field``
carrying the offending dotted path, so ``POST /api/settings`` can answer
``{"error": ..., "field": ...}`` on every rejection instead of a bare message.

The field is set explicitly at each raise site and is never inferred from the
message text: inferring it would couple protocol behavior to human prose, so a
reworded message would silently move a machine-readable pointer.
``_CacheReportConfigError`` already demonstrates the explicit pattern.

``field`` defaults to ``None`` so every existing CLI caller of these types keeps
working unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402


@pytest.fixture(scope="module")
def ns():
    return load_script()


ALERTS_CASES = [
    ("alerts", "alerts must be an object", {"alerts": "bad"}),
    ("alerts.enabled", "non-bool enabled", {"alerts": {"enabled": "yes"}}),
    (
        "alerts.projected_enabled",
        "non-bool projected_enabled",
        {"alerts": {"projected_enabled": 1}},
    ),
    ("alerts.notifier", "bad notifier enum", {"alerts": {"notifier": "nope"}}),
    (
        "alerts.notifier",
        "notifier=command with no template",
        {"alerts": {"notifier": "command"}},
    ),
    (
        "alerts.weekly_thresholds",
        "non-list weekly thresholds",
        {"alerts": {"weekly_thresholds": 90}},
    ),
    (
        "alerts.weekly_thresholds",
        "empty weekly thresholds",
        {"alerts": {"weekly_thresholds": []}},
    ),
    (
        "alerts.weekly_thresholds",
        "non-int threshold entry",
        {"alerts": {"weekly_thresholds": ["90"]}},
    ),
    (
        "alerts.weekly_thresholds",
        "out-of-range threshold entry",
        {"alerts": {"weekly_thresholds": [900]}},
    ),
    (
        "alerts.weekly_thresholds",
        "duplicate threshold entry",
        {"alerts": {"weekly_thresholds": [90, 90]}},
    ),
    (
        "alerts.weekly_thresholds",
        "non-increasing thresholds",
        {"alerts": {"weekly_thresholds": [95, 90]}},
    ),
    (
        "alerts.five_hour_thresholds",
        "non-list five-hour thresholds",
        {"alerts": {"five_hour_thresholds": "90"}},
    ),
    (
        "alerts.command_template",
        "empty template list",
        {"alerts": {"command_template": []}},
    ),
    (
        "alerts.command_template",
        "non-string template element",
        {"alerts": {"command_template": [1]}},
    ),
    (
        "alerts.command_template",
        "NUL byte in template element",
        {"alerts": {"command_template": ["ok", "a\x00b"]}},
    ),
    (
        "alerts.command_template",
        "blank program",
        {"alerts": {"command_template": ["  "]}},
    ),
]


@pytest.mark.parametrize(
    "field,label,cfg", ALERTS_CASES, ids=[c[1] for c in ALERTS_CASES]
)
def test_alerts_error_carries_the_offending_field(ns, field, label, cfg):
    with pytest.raises(ns["_AlertsConfigError"]) as exc:
        ns["_get_alerts_config"](cfg)
    assert exc.value.field == field


_CODEX = {"amount_usd": 50, "period": "calendar-month"}


def _codex(**overrides):
    block = dict(_CODEX)
    block.update(overrides)
    return {"budget": {"codex": block}}


BUDGET_CASES = [
    ("budget", "budget must be an object", {"budget": "bad"}),
    (
        "budget.weekly_usd",
        "non-number weekly_usd",
        {"budget": {"weekly_usd": "20"}},
    ),
    (
        "budget.weekly_usd",
        "non-positive weekly_usd",
        {"budget": {"weekly_usd": 0}},
    ),
    (
        "budget.alerts_enabled",
        "non-bool alerts_enabled",
        {"budget": {"alerts_enabled": "yes"}},
    ),
    (
        "budget.alert_thresholds",
        "non-list alert_thresholds",
        {"budget": {"alert_thresholds": 90}},
    ),
    (
        "budget.alert_thresholds",
        "non-int alert_thresholds entry",
        {"budget": {"alert_thresholds": [True]}},
    ),
    (
        "budget.alert_thresholds",
        "out-of-range alert_thresholds entry",
        {"budget": {"alert_thresholds": [0]}},
    ),
    ("budget.period", "bad period enum", {"budget": {"period": "fortnight"}}),
    (
        "budget.projected_enabled",
        "non-bool projected_enabled",
        {"budget": {"projected_enabled": 1}},
    ),
    (
        "budget.projects",
        "non-object projects",
        {"budget": {"projects": []}},
    ),
    (
        "budget.projects",
        "non-number project value",
        {"budget": {"projects": {"/x": "5"}}},
    ),
    (
        "budget.projects",
        "non-positive project value",
        {"budget": {"projects": {"/x": -1}}},
    ),
    (
        "budget.project_alerts_enabled",
        "non-bool project_alerts_enabled",
        {"budget": {"project_alerts_enabled": "no"}},
    ),
    (
        "budget.accounts",
        "non-object accounts",
        {"budget": {"accounts": []}},
    ),
    (
        "budget.accounts",
        "non-number account value",
        {"budget": {"accounts": {"k": "5"}}},
    ),
    (
        "budget.accounts",
        "non-positive account value",
        {"budget": {"accounts": {"k": 0}}},
    ),
    ("budget.codex", "non-object codex block", {"budget": {"codex": 7}}),
    (
        "budget.codex.amount_usd",
        "missing codex amount",
        {"budget": {"codex": {"period": "calendar-month"}}},
    ),
    (
        "budget.codex.amount_usd",
        "non-number codex amount",
        _codex(amount_usd="50"),
    ),
    ("budget.codex.period", "bad codex period", _codex(period="subscription-week")),
    (
        "budget.codex.alerts_enabled",
        "non-bool codex alerts_enabled",
        _codex(alerts_enabled="yes"),
    ),
    (
        "budget.codex.alert_thresholds",
        "non-list codex alert_thresholds",
        _codex(alert_thresholds=90),
    ),
    (
        "budget.codex.projected_enabled",
        "non-bool codex projected_enabled",
        _codex(projected_enabled=1),
    ),
    (
        "budget.codex.accounts",
        "non-object codex accounts",
        _codex(accounts=[]),
    ),
    (
        "budget.codex.accounts",
        "non-number codex account value",
        _codex(accounts={"k": "5"}),
    ),
]


@pytest.mark.parametrize(
    "field,label,cfg", BUDGET_CASES, ids=[c[1] for c in BUDGET_CASES]
)
def test_budget_error_carries_the_offending_field(ns, field, label, cfg):
    with pytest.raises(ns["_BudgetConfigError"]) as exc:
        ns["_get_budget_config"](cfg)
    assert exc.value.field == field


def test_field_defaults_to_none_so_existing_callers_still_work(ns):
    assert ns["_AlertsConfigError"]("boom").field is None
    assert ns["_BudgetConfigError"]("boom").field is None


def test_field_is_keyword_only(ns):
    """Positional passing must not silently become the field.

    A second positional argument on a ValueError subclass would land in
    ``args`` and change ``str(exc)``, so the keyword-only form is what keeps
    the message stable while the pointer is added.
    """
    with pytest.raises(TypeError):
        ns["_AlertsConfigError"]("boom", "alerts.enabled")
    with pytest.raises(TypeError):
        ns["_BudgetConfigError"]("boom", "budget.weekly_usd")


def test_message_text_is_unchanged_by_the_field(ns):
    exc = ns["_BudgetConfigError"]("boom", field="budget.weekly_usd")
    assert str(exc) == "boom"


# --- The raise sites inside bin/_cctally_config.py -----------------------
#
# The two parametrized tables above drive ``_get_alerts_config`` and
# ``_get_budget_config``, which both live in ``bin/_cctally_core.py``. The
# glue module ``bin/_cctally_config.py`` raises the same two exception types
# at twenty further sites that nothing above reaches, which is how one of them
# shipped naming ``alerts.quota`` for a message about the ``alerts`` block.
#
# Seventeen of those twenty sites are exercised below. The remaining three sit
# inside ``_cmd_config_set`` and ``_cmd_config_unset``, which catch the
# exception and return exit 2; their ``field`` is therefore not observable
# from outside the command, so the last test in this file asserts the exit
# code and the message those sites do expose.


@pytest.fixture
def confmod(ns):
    """``bin/_cctally_config.py`` as loaded by the current ``cctally`` module.

    ``ns`` is requested for its ordering effect: ``load_script`` drops cached
    ``_cctally_*`` siblings and re-imports them against the fresh ``cctally``
    module, so reading ``sys.modules`` afterwards yields that same instance.
    Resolved per test rather than cached, because any test in the run may call
    ``load_script`` again and rebind that sibling.
    """
    return sys.modules["_cctally_config"]


QUOTA_CASES = [
    # The message names the `alerts` block, so the pointer must be `alerts`.
    # `_quota_alert_error` defaults to `alerts.quota`, which is right for every
    # other call in the function and wrong for this one.
    ("alerts", "alerts is not an object", {"alerts": "bad"}),
    ("alerts.quota", "quota is not an object", {"alerts": {"quota": 7}}),
    (
        "alerts.quota",
        "non-bool quota enabled",
        {"alerts": {"quota": {"enabled": "yes"}}},
    ),
    (
        "alerts.quota",
        "non-list actual thresholds",
        {"alerts": {"quota": {"actual_thresholds": 90}}},
    ),
    (
        "alerts.quota",
        "non-int actual threshold entry",
        {"alerts": {"quota": {"actual_thresholds": ["90"]}}},
    ),
    (
        "alerts.quota",
        "out-of-range actual threshold entry",
        {"alerts": {"quota": {"actual_thresholds": [900]}}},
    ),
    (
        "alerts.quota",
        "non-increasing actual thresholds",
        {"alerts": {"quota": {"actual_thresholds": [95, 90]}}},
    ),
    (
        "alerts.quota",
        "non-list rules",
        {"alerts": {"quota": {"rules": 7}}},
    ),
    (
        "alerts.quota",
        "rule is not an object",
        {"alerts": {"quota": {"rules": [7]}}},
    ),
    (
        "alerts.quota",
        "rule missing required keys",
        {"alerts": {"quota": {"rules": [{"source": "codex"}]}}},
    ),
]


@pytest.mark.parametrize(
    "field,label,cfg", QUOTA_CASES, ids=[c[1] for c in QUOTA_CASES]
)
def test_quota_alert_error_carries_the_offending_field(
    ns, confmod, field, label, cfg
):
    with pytest.raises(ns["_AlertsConfigError"]) as exc:
        confmod._get_quota_alerts_config(cfg)
    assert exc.value.field == field


def _empty_accounts_conn():
    """An accounts registry that exists and knows nobody.

    ``resolve_account_ref`` needs a real table to query before it can decide a
    ref is unknown, which is the branch under test.
    """
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE accounts "
        "(account_key TEXT, provider TEXT, label TEXT, email TEXT)"
    )
    return conn


def _resolve_against_empty_registry(m):
    """The caller owns the connection, so this case closes its own."""
    conn = _empty_accounts_conn()
    try:
        m._resolve_one_account_budget_ref(
            conn, "work", "claude", "budget.accounts")
    finally:
        conn.close()


BUDGET_HELPER_CASES = [
    # _resolve_one_account_budget_ref — every rejection names the map itself,
    # because the offending ref is a KEY of that map and has no path of its own.
    (
        "budget.accounts",
        "account ref is not a string",
        lambda m: m._resolve_one_account_budget_ref(
            None, 5, "claude", "budget.accounts"),
    ),
    (
        "budget.accounts",
        "account ref is the empty string",
        lambda m: m._resolve_one_account_budget_ref(
            None, "", "claude", "budget.accounts"),
    ),
    (
        "budget.accounts",
        "account ref is the unattributed bucket",
        lambda m: m._resolve_one_account_budget_ref(
            None, "unattributed", "claude", "budget.accounts"),
    ),
    (
        "budget.codex.accounts",
        "account ref is the vendor-wide bucket",
        lambda m: m._resolve_one_account_budget_ref(
            None, "*", "codex", "budget.codex.accounts"),
    ),
    (
        "budget.accounts",
        "account ref unresolvable with no registry",
        lambda m: m._resolve_one_account_budget_ref(
            None, "work", "claude", "budget.accounts"),
    ),
    (
        "budget.accounts",
        "account ref unresolvable during stats maintenance",
        lambda m: m._resolve_one_account_budget_ref(
            None, "work", "claude", "budget.accounts",
            stats_maintenance_unavailable=True),
    ),
    (
        "budget.accounts",
        "account ref unknown to the registry",
        _resolve_against_empty_registry,
    ),
    # _parse_account_budget_value
    (
        "budget.accounts",
        "account budget value is not JSON",
        lambda m: m._parse_account_budget_value(
            "{", "claude", "budget.accounts"),
    ),
    (
        "budget.codex.accounts",
        "account budget value is not a JSON object",
        lambda m: m._parse_account_budget_value(
            "[]", "codex", "budget.codex.accounts"),
    ),
    # _parse_codex_budget_leaf_value — one leaf, one dotted path.
    (
        "budget.codex.amount_usd",
        "codex amount is not a number",
        lambda m: m._parse_codex_budget_leaf_value("amount_usd", "abc"),
    ),
    (
        "budget.codex.alerts_enabled",
        "codex alerts_enabled is not a boolean word",
        lambda m: m._parse_codex_budget_leaf_value("alerts_enabled", "maybe"),
    ),
    (
        "budget.codex.projected_enabled",
        "codex projected_enabled is not a boolean word",
        lambda m: m._parse_codex_budget_leaf_value("projected_enabled", "maybe"),
    ),
    (
        "budget.codex.alert_thresholds",
        "codex alert_thresholds is not a list of integers",
        lambda m: m._parse_codex_budget_leaf_value("alert_thresholds", "90,x"),
    ),
    (
        "budget.codex.bogus",
        "codex leaf is unknown to the value parser",
        lambda m: m._parse_codex_budget_leaf_value("bogus", "1"),
    ),
    # _set_codex_budget_leaf
    (
        "budget.weekly_usd",
        "set leaf key is outside the codex block",
        lambda m: m._set_codex_budget_leaf({}, "budget.weekly_usd", "5"),
    ),
    (
        "budget.codex.bogus",
        "set leaf key names an unknown codex leaf",
        lambda m: m._set_codex_budget_leaf({}, "budget.codex.bogus", "1"),
    ),
    (
        "budget",
        "set leaf meets a non-object budget block",
        lambda m: m._set_codex_budget_leaf(
            {"budget": "bad"}, "budget.codex.alerts_enabled", "true"),
    ),
    (
        "budget.codex.alerts_enabled",
        "set leaf runs before a codex amount exists",
        lambda m: m._set_codex_budget_leaf(
            {}, "budget.codex.alerts_enabled", "true"),
    ),
    (
        "budget.codex",
        "set leaf meets a non-object codex block",
        lambda m: m._set_codex_budget_leaf(
            {"budget": {"codex": 7}}, "budget.codex.alerts_enabled", "true"),
    ),
]


@pytest.mark.parametrize(
    "field,label,call",
    BUDGET_HELPER_CASES,
    ids=[c[1] for c in BUDGET_HELPER_CASES],
)
def test_config_budget_helper_error_carries_the_offending_field(
    ns, confmod, field, label, call
):
    with pytest.raises(ns["_BudgetConfigError"]) as exc:
        call(confmod)
    assert exc.value.field == field


def test_config_unset_reports_a_malformed_budget_block(
    tmp_path, monkeypatch, capsys
):
    """The three command-level raise sites, asserted on what they expose.

    ``_cmd_config_set`` and ``_cmd_config_unset`` catch ``_BudgetConfigError``
    and return exit 2, so their ``field`` never leaves the process and cannot
    be asserted the way the helper sites above are. What they do expose is the
    exit code and the message, and nothing covered either.
    """
    import argparse
    import json as _json

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _cctally_core.CONFIG_PATH.write_text(_json.dumps({"budget": "bad"}))

    rc = sys.modules["_cctally_config"]._cmd_config_unset(
        argparse.Namespace(key="budget.codex.alerts_enabled")
    )
    assert rc == 2
    assert "budget must be an object" in capsys.readouterr().err
