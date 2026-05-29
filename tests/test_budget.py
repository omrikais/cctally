"""Unit tests for the pure budget kernel (bin/_lib_budget.py) + F1 structural
invariant that forecast and budget share project_linear."""
import datetime as dt
import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name, path):
    # SourceFileLoader handles both `.py` siblings and the extensionless
    # `bin/cctally` main script (spec_from_file_location can't infer a
    # loader for the latter). Mirrors the repo's canonical loaders
    # (tests/test_config_path_override.py, tests/test_pricing_check.py).
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass's sys.modules[cls.__module__]
    # lookup resolves (Python 3.14).
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


_budget = _load("_lib_budget", REPO / "bin" / "_lib_budget.py")
BudgetInputs = _budget.BudgetInputs
compute_budget_status = _budget.compute_budget_status
project_linear = _budget.project_linear


UTC = dt.timezone.utc
WS = dt.datetime(2026, 5, 26, 0, 0, tzinfo=UTC)
WE = WS + dt.timedelta(days=7)


def _mk(spent, recent_24h, *, target=300.0, now_days=3.5, thresholds=(90, 100)):
    return BudgetInputs(
        target_usd=target,
        spent_usd=spent,
        recent_24h_usd=recent_24h,
        week_start_at=WS,
        week_end_at=WE,
        now=WS + dt.timedelta(days=now_days),
        alert_thresholds=thresholds,
    )


def test_project_linear_is_pure_unsorted():
    assert project_linear(10.0, 2.0, 1.0, 3.0) == (12.0, 16.0)
    # Does NOT sort — caller's responsibility.
    assert project_linear(0.0, 1.0, 5.0, 1.0) == (5.0, 1.0)


def test_consumption_pct_and_remaining():
    s = compute_budget_status(_mk(spent=182.40, recent_24h=36.0))
    assert abs(s.consumption_pct - 60.8) < 1e-9
    assert abs(s.remaining_usd - 117.60) < 1e-9


def test_verdict_ok_warn_over():
    # ok: tiny spend, tiny recent rate, far from target.
    assert compute_budget_status(_mk(spent=10.0, recent_24h=1.0)).verdict == "ok"
    # over: already past target.
    assert compute_budget_status(_mk(spent=310.0, recent_24h=5.0)).verdict == "over"
    # over by projection: modest spend but a recent rate that projects past target.
    hot = compute_budget_status(_mk(spent=150.0, recent_24h=120.0, now_days=3.5))
    assert hot.verdict == "over"


def test_crossed_thresholds_snap_up():
    # 89.9999999% must count as 90 via the +1e-9 snap-up.
    s = compute_budget_status(_mk(spent=269.9999999999, recent_24h=0.0, target=300.0))
    assert 90 in s.crossed_thresholds


def test_low_confidence_early_week():
    early = compute_budget_status(_mk(spent=5.0, recent_24h=5.0, now_days=0.5))
    assert early.low_confidence is True
    midweek = compute_budget_status(_mk(spent=150.0, recent_24h=40.0, now_days=3.5))
    assert midweek.low_confidence is False


def test_zero_target_is_safe():
    s = compute_budget_status(_mk(spent=50.0, recent_24h=10.0, target=0.0))
    assert s.consumption_pct == 0.0  # no divide-by-zero


def test_empty_thresholds_render_verdict():
    # alerts silenced (empty thresholds) but verdict still computes via fallback.
    s = compute_budget_status(_mk(spent=10.0, recent_24h=1.0, thresholds=()))
    assert s.verdict in {"ok", "warn", "over"}
    assert s.crossed_thresholds == ()


def test_f1_structural_budget_uses_project_linear():
    """compute_budget_status must route projection through project_linear."""
    import inspect
    src = inspect.getsource(compute_budget_status)
    assert "project_linear(" in src
    # And must NOT re-implement the primitive inline.
    assert "rate_low * remaining" not in src.replace(
        "return (current + rate_low * remaining", ""  # the primitive's own body
    )


def test_f1_structural_forecast_uses_project_linear():
    """_compute_forecast must route projection through project_linear too."""
    import inspect
    cctally = _load("cctally", REPO / "bin" / "cctally")
    src = inspect.getsource(cctally._compute_forecast)
    assert "project_linear(" in src
