"""`_totals` no longer re-runs the label allocator (#566 §5.1 item 7).

`_totals` returns a `TokenTotals`, which carries no label, so the
collision-safe allocation it used to run could not reach any output. It
re-created every entry through `dataclasses.replace` to sum six numeric fields
off the copies: 148 calls driving 775,568 `replace` calls, about 1.9s of every
dashboard build on the maintainer's store.

The allocation that matters runs once, over the complete population, inside
`build_codex_project_result`. These cases pin that it still does — including
for a collision that exists only in the parent scope, where an account-scoped
subset would never see the second project sharing the label.
"""
from __future__ import annotations

import datetime as dt

from conftest import load_script  # type: ignore

UTC = dt.timezone.utc
START = dt.datetime(2026, 7, 1, tzinfo=UTC)
END = dt.datetime(2026, 7, 8, tzinfo=UTC)


def _kernel():
    load_script()
    import _lib_source_analytics
    return _lib_source_analytics


def _entry(kernel, *, project_key, project_label, account_key="unattributed",
           cost=1.0, minute=0, model="gpt-5"):
    return kernel.QualifiedCodexEntry(
        timestamp=START + dt.timedelta(minutes=minute),
        conversation_key=f"v1.{project_key}.{minute}",
        source_root_key="root-a",
        project_key=project_key,
        project_label=project_label,
        model=model,
        input_tokens=10,
        cached_input_tokens=4,
        output_tokens=5,
        reasoning_output_tokens=1,
        total_tokens=15,
        cost_usd=cost,
        account_key=account_key,
    )


def test_totals_does_not_invoke_the_allocator(monkeypatch):
    kernel = _kernel()
    calls = {"n": 0}
    real = kernel.assign_collision_safe_project_labels

    def counting(entries):
        calls["n"] += 1
        return real(entries)

    monkeypatch.setattr(
        kernel, "assign_collision_safe_project_labels", counting)
    entries = [
        _entry(kernel, project_key="k1", project_label="api", minute=0),
        _entry(kernel, project_key="k2", project_label="api", minute=1),
        _entry(kernel, project_key="k3", project_label="web", minute=2),
    ]
    totals = kernel._totals(entries)
    assert calls["n"] == 0
    assert totals.total_tokens == 45
    assert totals.cost_usd == 3.0


def test_totals_are_unchanged_by_the_removal(monkeypatch):
    kernel = _kernel()
    entries = [
        _entry(kernel, project_key="k1", project_label="api",
               cost=0.1, minute=0),
        _entry(kernel, project_key="k2", project_label="api",
               cost=0.2, minute=1),
        _entry(kernel, project_key="k3", project_label="web",
               cost=0.30000000000000004, minute=2),
    ]
    # The pre-change body, reproduced literally.
    annotated = kernel.assign_collision_safe_project_labels(entries)
    import math
    assert kernel._totals(entries) == kernel.TokenTotals(
        input_tokens=sum(e.input_tokens for e in annotated),
        cached_input_tokens=sum(e.cached_input_tokens for e in annotated),
        output_tokens=sum(e.output_tokens for e in annotated),
        reasoning_output_tokens=sum(
            e.reasoning_output_tokens for e in annotated),
        total_tokens=sum(e.total_tokens for e in annotated),
        cost_usd=math.fsum(e.cost_usd for e in annotated),
    )


def test_a_real_collision_is_still_disambiguated():
    kernel = _kernel()
    entries = [
        _entry(kernel, project_key="k1", project_label="api", minute=0),
        _entry(kernel, project_key="k2", project_label="api", minute=1),
        _entry(kernel, project_key="k3", project_label="web", minute=2),
    ]
    result = kernel.build_codex_project_result(
        entries, range_start=START, range_end=END,
    )
    labels = sorted(row.display_label for row in result.data.projects)
    assert labels == ["api (1)", "api (2)", "web"]


def test_a_collision_present_only_in_the_parent_scope(monkeypatch):
    """The subset an account child sees cannot see its own collision.

    Both colliding projects belong to different accounts, so neither child
    population contains two identities sharing `api`. The parent must still
    disambiguate, and each child must still render the label the parent
    allocated rather than a bare `api` of its own.
    """
    kernel = _kernel()
    parent = [
        _entry(kernel, project_key="k1", project_label="api",
               account_key="acct-a", minute=0),
        _entry(kernel, project_key="k2", project_label="api",
               account_key="acct-b", minute=1),
    ]
    parent_result = kernel.build_codex_project_result(
        parent, range_start=START, range_end=END,
    )
    assert sorted(
        row.display_label for row in parent_result.data.projects
    ) == ["api (1)", "api (2)"]

    # The child is built from the parent's ALREADY-annotated entries, which is
    # what the dashboard does, so the parent's allocation survives the split.
    annotated = kernel.assign_collision_safe_project_labels(parent)
    for account in ("acct-a", "acct-b"):
        subset = [e for e in annotated if e.account_key == account]
        child = kernel.build_codex_project_result(
            subset, range_start=START, range_end=END,
        )
        assert [row.display_label for row in child.data.projects] != ["api"]
        assert [row.display_label for row in child.data.projects] == [
            kernel.emitted_project_label(subset[0])
        ]


def test_per_model_totals_are_unaffected():
    kernel = _kernel()
    entries = [
        _entry(kernel, project_key="k1", project_label="api",
               model="gpt-5", cost=1.0, minute=0),
        _entry(kernel, project_key="k1", project_label="api",
               model="gpt-5-codex", cost=2.0, minute=1),
    ]
    result = kernel.build_codex_project_result(
        entries, range_start=START, range_end=END,
    )
    row = result.data.projects[0]
    assert [model for model, _ in row.models] == ["gpt-5", "gpt-5-codex"]
    assert [totals.cost_usd for _, totals in row.models] == [1.0, 2.0]
    assert row.totals.cost_usd == 3.0
