"""#556 S3 — Claude alert ownership is a total classifier, not a predicate.

`owns_alert` was an inline boolean whose answer for an unrecognized axis was
simply `False`. A registry-completeness test over it would read that `False` as
"Codex-owned" and bless any forgotten seventh axis, so it would assert nothing
(spec section 3.4). The classifier returns an explicit owner and raises on an
axis it has no rule for.
"""
from __future__ import annotations

import pytest

import _lib_alert_axes as axes
import _cctally_tui as tui


def test_every_registered_axis_has_an_explicit_owner():
    for descriptor in axes.AXIS_REGISTRY:
        if descriptor.id == "projected":
            continue  # covered by the metric matrix below
        owner = tui.alert_row_owner(descriptor.id, descriptor.vendor, None)
        assert owner in {"claude", "codex"}, descriptor.id


@pytest.mark.parametrize("metric,expected", [
    ("weekly_pct", "claude"),
    ("budget_usd", "claude"),
    ("codex_budget_usd", "codex"),
])
def test_projected_ownership_splits_by_metric(metric, expected):
    assert tui.alert_row_owner("projected", None, metric) == expected


def test_an_unregistered_axis_raises_rather_than_defaulting():
    with pytest.raises(ValueError):
        tui.alert_row_owner("some_future_axis", None, None)


def test_project_budget_respects_an_explicit_non_claude_vendor():
    # The pre-S3 arm was unconditional, so a codex-vendored project_budget row
    # would have been relabelled as Claude's.
    assert tui.alert_row_owner("project_budget", "codex", None) == "codex"
    assert tui.alert_row_owner("project_budget", None, None) == "claude"


@pytest.mark.parametrize("axis", ["weekly", "five_hour", "budget"])
def test_the_vendor_arms_keep_their_pre_s3_answers(axis):
    assert tui.alert_row_owner(axis, None, None) == "claude"
    assert tui.alert_row_owner(axis, "claude", None) == "claude"
    assert tui.alert_row_owner(axis, "codex", None) == "codex"


def test_codex_budget_is_codex_owned_whatever_its_vendor_field_says():
    assert tui.alert_row_owner("codex_budget", "codex", None) == "codex"
    assert tui.alert_row_owner("codex_budget", None, None) == "codex"
