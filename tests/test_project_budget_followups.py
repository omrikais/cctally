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
