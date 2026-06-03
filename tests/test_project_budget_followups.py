"""Unit tests for the issue #130 shared helpers: _project_budget_labels
(label dedup) and _project_crossings (crossing-arithmetic dedup), plus the
ProjectCell.rank_cost anon-mapping preservation."""
import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
_BIN = REPO / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


def _ns():
    # Canonical loader (mirrors tests/test_budget.py::_load for bin/cctally).
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("cctally", str(_BIN / "cctally"))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _mkroot(tmp_path, *parts):
    p = tmp_path.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def test_project_budget_labels_disambiguates_same_basename(tmp_path):
    c = _ns()
    work_app = _mkroot(tmp_path, "work", "app")
    personal_app = _mkroot(tmp_path, "personal", "app")
    labels = c._project_budget_labels([work_app, personal_app])
    # Same basename "app" → both disambiguated with their parent segment.
    assert labels[work_app] != labels[personal_app]
    assert labels[work_app] != "app"
    assert "app" in labels[work_app] and "work" in labels[work_app]
    assert "app" in labels[personal_app] and "personal" in labels[personal_app]


def test_project_budget_labels_distinct_basenames_keep_display_key(tmp_path):
    c = _ns()
    alpha = _mkroot(tmp_path, "alpha")
    beta = _mkroot(tmp_path, "beta")
    labels = c._project_budget_labels([alpha, beta])
    assert labels[alpha] == "alpha"
    assert labels[beta] == "beta"


def test_project_budget_labels_order_independent(tmp_path):
    c = _ns()
    work_app = _mkroot(tmp_path, "work", "app")
    personal_app = _mkroot(tmp_path, "personal", "app")
    a = c._project_budget_labels([work_app, personal_app])
    b = c._project_budget_labels([personal_app, work_app])
    assert a == b  # dict keyed by project path; order of input must not matter


def test_project_budget_labels_returns_every_input_key(tmp_path):
    c = _ns()
    alpha = _mkroot(tmp_path, "alpha")
    beta = _mkroot(tmp_path, "beta")
    labels = c._project_budget_labels([alpha, beta])
    assert set(labels.keys()) == {alpha, beta}


def test_project_crossings_basic_and_sorted_order():
    c = _ns()
    by_proj = {"/p": 80.0}
    # target 100 → 80% → crosses 25,50,75 (not 90/100), yielded in sorted order.
    out = list(c._project_crossings([("/p", 100.0)], [90, 25, 75, 50], by_proj))
    thresholds = [t for (_pk, t, *_rest) in out]
    assert thresholds == [25, 50, 75]
    pk, t, spent, target, pct = out[0]
    assert pk == "/p" and t == 25 and spent == 80.0 and target == 100.0
    assert abs(pct - 80.0) < 1e-9


def test_project_crossings_one_ulp_below_integer_threshold_still_crosses():
    c = _ns()
    # Classic float-floor case (CLAUDE.md gotcha): 0.57 * 100 is
    # 56.99999999999999 — strictly below the integer 57. Without the +1e-9 snap
    # the 57% crossing is silently MISSED; the snap rescues it. This guards BOTH
    # mutations: removing the snap (56.999... >= 57 is False) AND flipping it to
    # -1e-9 — unlike an exact-50.0 fixture, which only catches the flip.
    by_proj = {"/p": 0.57}
    out = list(c._project_crossings([("/p", 1.0)], [57], by_proj))
    assert [t for (_pk, t, *_r) in out] == [57]


def test_project_crossings_zero_target_never_crosses():
    c = _ns()
    out = list(c._project_crossings([("/p", 0.0)], [25, 50], {"/p": 999.0}))
    assert out == []


def test_project_crossings_below_threshold_no_yield():
    c = _ns()
    out = list(c._project_crossings([("/p", 100.0)], [90], {"/p": 10.0}))
    assert out == []


def test_project_crossings_missing_key_is_zero_spent():
    c = _ns()
    out = list(c._project_crossings([("/p", 100.0)], [25], {}))
    assert out == []  # spent defaults to 0 → 0% → no cross


def _share():
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("_lib_share", str(_BIN / "_lib_share.py"))
    spec = importlib.util.spec_from_loader("_lib_share", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_lib_share"] = mod
    loader.exec_module(mod)
    return mod


def _min_snapshot(s, rows):
    # Build a minimal ShareSnapshot with the given rows (other fields dummy).
    # `_collect_project_costs` / `_apply_anon_mapping` only read rows + columns,
    # so the period fields are filler — but PeriodSpec is a 4-field frozen
    # dataclass (start, end, display_tz, label), so all four must be supplied.
    import datetime as dt
    now = dt.datetime(2026, 6, 3, tzinfo=dt.timezone.utc)
    return s.ShareSnapshot(
        cmd="budget", title="t", subtitle=None,
        period=s.PeriodSpec(start=now, end=now, display_tz="UTC", label="p"),
        columns=(s.ColumnSpec(key="metric", label="Metric"),
                 s.ColumnSpec(key="value", label="Value")),
        rows=tuple(rows), chart=None, totals=(), notes=(),
        generated_at=now,
        version="1",
    )


def test_rank_cost_overrides_money_cell_sum():
    s = _share()
    # rank_cost set → that value is the project's cost, sibling MoneyCells ignored.
    row = s.Row(cells={
        "metric": s.ProjectCell("alpha", rank_cost=99.0),
        "value": s.MoneyCell(1.0),  # would be summed in the legacy path
    })
    costs = s._collect_project_costs(_min_snapshot(s, [row]))
    assert costs["alpha"] == 99.0


def test_rank_cost_none_falls_back_to_money_sum():
    s = _share()
    row = s.Row(cells={
        "metric": s.ProjectCell("beta"),  # rank_cost defaults None
        "value": s.MoneyCell(7.5),
    })
    costs = s._collect_project_costs(_min_snapshot(s, [row]))
    assert costs["beta"] == 7.5


def test_apply_anon_mapping_preserves_rank_cost():
    s = _share()
    row = s.Row(cells={"metric": s.ProjectCell("secret-proj", rank_cost=42.0)})
    snap = _min_snapshot(s, [row])
    scrubbed = s._apply_anon_mapping(snap, {"secret-proj": "project-1"})
    cell = scrubbed.rows[0].cells["metric"]
    assert cell.label == "project-1"
    assert cell.rank_cost == 42.0  # rank survives the scrub (Codex F3)
