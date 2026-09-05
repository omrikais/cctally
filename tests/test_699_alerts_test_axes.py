"""#699 — `cctally alerts test` rehearses both NON-REGISTRY alert families.

Every other axis this command simulates is a member of `AXIS_REGISTRY`. The
two families here are deliberately outside it: `quota` has a payload builder
but no registry row, and `meter_rate_change` has neither, because it carries
an explicit severity rather than a percentage threshold. Both were therefore
unrehearsable — the only way to see either notification was to wait for a real
crossing or a real metering-rate transition.

There is NO committed parser or help golden for this command, so the surface
is pinned here, in the source, and in the docs. Do not go looking for a golden
to regenerate.

Both synthetics are constructed inside `cmd_alerts_test`, which is what keeps
`bin/_lib_meter_rate_change.py` and the `_build_alert_payload_*` helpers
clock-, database- and policy-free.
"""
from __future__ import annotations

import argparse

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    return ns


def _args(*, axis, threshold=None, metric="weekly_pct"):
    return argparse.Namespace(axis=axis, threshold=threshold, metric=metric)


@pytest.fixture
def captured(monkeypatch):
    """Every payload `cmd_alerts_test` dispatches.

    `cmd_alerts_test` lives in `_cctally_alerts` and calls the module-global
    `_dispatch_alert_notification` directly, not through the cctally-namespace
    shim the record path uses, so capture is wired at the module level — the
    same seam `tests/test_project_budget_alerts.py` uses.
    """
    import _cctally_alerts
    seen: list = []

    def _fake(payload, *, mode="real", **kwargs):
        seen.append((payload, mode))
        return "queued"

    monkeypatch.setattr(
        _cctally_alerts, "_dispatch_alert_notification", _fake)
    return seen


def _seed_accounts(ns, *rows):
    """Register accounts through the journal, then rebuild, exactly as the
    real registry is populated. `rows` are `(provider, natural_id, email)`."""
    import _cctally_journal as jr
    import _lib_accounts
    import _lib_journal as lj
    for provider, natural, email in rows:
        jr.append_record(lj.make_account_observe(
            at="2026-08-29T00:00:00Z",
            account_key=_lib_accounts.account_key(provider, natural),
            provider=provider, natural_id=natural, email=email))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))


# --------------------------------------------------------------------------
# The two axes reach their real payload builders
# --------------------------------------------------------------------------
def test_699_meter_rate_change_axis_rehearses_the_real_payload(ns, captured):
    assert ns["cmd_alerts_test"](_args(axis="meter-rate-change")) == 0
    payload, mode = captured[0]
    assert mode == "test"
    assert payload["axis"] == "meter_rate_change"
    # Passed through the real `alert_payload`, so the family's own contract
    # holds: an explicit severity and NO threshold key, because the dispatch
    # glue derives severity from a threshold whenever it finds one.
    assert "threshold" not in payload
    assert payload["severity"] == "alarm"
    assert payload["previous_units_per_point"] > payload["new_units_per_point"]


def test_699_the_rehearsed_severity_is_computed_not_hardcoded(ns, captured):
    """The synthetic rates must actually produce the severity claimed, or the
    rehearsal would show a tier the kernel never assigns to them."""
    mrc = ns["_load_sibling"]("_lib_meter_rate_change")
    ns["cmd_alerts_test"](_args(axis="meter-rate-change"))
    payload = captured[0][0]
    assert payload["severity"] == mrc.transition_severity(
        payload["previous_units_per_point"], payload["new_units_per_point"])
    assert payload["severity"] in mrc.RATE_CHANGE_SEVERITIES


def test_699_the_rehearsal_carries_the_disclosure(ns, captured):
    """#690's copy is only reachable on a withheld transition, so a rehearsal
    that omitted the disclosure could never show it."""
    ns["cmd_alerts_test"](_args(axis="meter-rate-change"))
    payload = captured[0][0]
    assert payload["withholding_status"] == "unsupported-model-mix"
    assert payload["baseline_withheld_days"] == 3
    assert payload["composition_provenance"] == '["forecast-aggregate"]'


def test_699_quota_axis_rehearses_the_real_payload(ns, captured):
    assert ns["cmd_alerts_test"](_args(axis="quota", threshold=90)) == 0
    payload, mode = captured[0]
    assert mode == "test"
    assert payload["axis"] == "quota"
    assert payload["threshold"] == 90
    assert payload["source"] == "codex"
    # `_build_alert_payload_quota` nests the same fields under `context`, and
    # the renderer reads that copy.
    assert payload["context"]["window_minutes"] == 10080
    assert payload["context"]["resets_at_utc"]


def test_699_the_quota_axis_still_takes_a_threshold(ns, captured):
    assert ns["cmd_alerts_test"](_args(axis="quota", threshold=75)) == 0
    assert captured[0][0]["threshold"] == 75


# --------------------------------------------------------------------------
# `--threshold` is a usage error on the rate-change axis
# --------------------------------------------------------------------------
def test_699_threshold_is_a_usage_error_on_meter_rate_change(
        ns, captured, capsys):
    """Exit 2 is native-usage in `docs/cli-contract.md`, and the message names
    both the axis and the flag — a refusal that does not say what failed costs
    more than one that gives no reason."""
    rc = ns["cmd_alerts_test"](
        _args(axis="meter-rate-change", threshold=95))
    assert rc == 2
    err = capsys.readouterr().err
    assert "meter-rate-change" in err
    assert "--threshold" in err
    assert not captured, "a refused invocation still dispatched an alert"


def test_699_an_omitted_threshold_is_not_a_refusal(ns, captured):
    """The refusal must key on SUPPLIED, not on the parser's default. With a
    non-None default the flag would be indistinguishable from its default and
    every rate-change rehearsal would exit 2."""
    assert ns["cmd_alerts_test"](_args(axis="meter-rate-change")) == 0
    assert len(captured) == 1


def test_699_an_omitted_threshold_still_defaults_to_90_elsewhere(
        ns, captured):
    """Moving the parser default to None must not change what any other axis
    rehearses."""
    assert ns["cmd_alerts_test"](_args(axis="weekly")) == 0
    assert captured[0][0]["threshold"] == 90


# --------------------------------------------------------------------------
# R8: the label prefix is exercised on a decorated install and absent on one
# --------------------------------------------------------------------------
@pytest.fixture
def dispatched_titles(monkeypatch):
    """Every notification title the REAL `_dispatch_alert_notification` built.

    `cmd_alerts_test` exposes no `popen_factory` seam and the `alerts.log`
    line carries no title, so `build_command` is the one place the assembled
    title is observable — and it is assembled downstream of the payload, which
    is why the payload alone cannot answer criterion 9. The stub records the
    title and answers `None`, which puts the dispatch on its no-notifier
    branch, so nothing is spawned.

    That answer is also why the tests below do not assert the command's exit
    code: `no_notifier` is `cmd_alerts_test`'s exit 3, and it is the stub's
    doing rather than anything about the R8 prefix. The recorded title is the
    proof the dispatch ran, because it exists only if the real dispatch built
    it. `test_699_meter_rate_change_axis_rehearses_the_real_payload` pins the
    exit code over the fully stubbed dispatch.
    """
    import _cctally_alerts
    titles: list = []

    def _fake(notifier, *, title, **kwargs):
        titles.append(title)
        return None

    monkeypatch.setattr(_cctally_alerts, "build_command", _fake)
    return titles


#: The undecorated title `_alert_text_meter_rate_change` returns. The R8
#: prefix is applied to it downstream, so it is also the tail of a decorated
#: title.
RATE_CHANGE_TITLE = "cctally - Claude metering rate changed"


def test_699_a_decorated_install_resolves_a_real_account(
        ns, dispatched_titles):
    """The stated reason this axis exists: seeing #697's `[label]` prefix
    without waiting for a real rate change.

    Asserted on the title the real dispatch built. Resolving a real account
    key and computing a prefix from it are two separate facts, and calling
    `_alert_label_prefix` here would prove both while leaving the third —
    that the prefix reaches the notification title — unexercised. Criterion 9
    asks for the third.
    """
    _seed_accounts(ns, ("claude", "a@x", "a@x"), ("claude", "b@x", "b@x"))
    ns["cmd_alerts_test"](_args(axis="meter-rate-change"))
    assert len(dispatched_titles) == 1
    title = dispatched_titles[0]
    assert title.endswith(RATE_CHANGE_TITLE), title
    assert title.startswith("[") and "] " in title, (
        f"the dispatched title carries no R8 prefix: {title!r}. Either the "
        "synthetic fell back to the vendor-wide sentinel on an install that "
        "has real accounts, or the prefix never reached the title")


def test_699_a_single_account_install_gets_no_prefix(ns, dispatched_titles):
    """R8 byte-stability: one real account decorates nothing, so the title the
    notifier builds is the undecorated one, character for character."""
    _seed_accounts(ns, ("claude", "a@x", "a@x"))
    ns["cmd_alerts_test"](_args(axis="meter-rate-change"))
    assert dispatched_titles == [RATE_CHANGE_TITLE]


def test_699_an_empty_registry_falls_back_to_the_vendor_wide_sentinel(
        ns, captured):
    """No accounts at all must degrade rather than raise, so `alerts test`
    works on a fresh install."""
    ns["cmd_alerts_test"](_args(axis="meter-rate-change"))
    assert captured[0][0]["account_key"] == "*"


def test_699_the_quota_synthetic_resolves_a_codex_account(ns, captured):
    """The quota family observes Codex, and `_AXIS_VENDOR` maps the axis to
    the same vendor, so a Claude account must not be offered here."""
    _seed_accounts(ns, ("codex", "c@x", "c@x"), ("codex", "d@x", "d@x"))
    ns["cmd_alerts_test"](_args(axis="quota", threshold=90))
    key = captured[0][0]["account_key"]
    assert key not in ("*", "unattributed")
    prefix = ns["_load_sibling"]("_cctally_alerts")._alert_label_prefix(
        "quota", key, None)
    assert prefix.startswith("[")


# --------------------------------------------------------------------------
# The parser surface, which has no golden
# --------------------------------------------------------------------------
def test_699_the_parser_offers_both_new_axes(ns):
    parser = ns["build_parser"]()
    args = parser.parse_args(["alerts", "test", "--axis", "meter-rate-change"])
    assert args.axis == "meter-rate-change"
    assert args.threshold is None
    assert parser.parse_args(
        ["alerts", "test", "--axis", "quota", "--threshold", "90"]
    ).threshold == 90


def test_699_the_parser_still_rejects_an_unknown_axis(ns):
    parser = ns["build_parser"]()
    with pytest.raises(SystemExit):
        parser.parse_args(["alerts", "test", "--axis", "not-an-axis"])
