"""Config-plumbing tests for the projected-pace alert toggles (#121).

Two new bool keys, both default OFF (no surprise notifications on upgrade):
- ``alerts.projected_enabled`` (gates the ``weekly_pct`` projected metric)
- ``budget.projected_enabled`` (gates the ``budget_usd`` projected metric)

Covers: defaults (False), bool validation (non-bool rejected — NOT silently
coerced), the validated getters surface the key, and that setting the key emits
NO "unknown alerts/budget config key" warning (carry-forward MUST-FIX #1: the
record path now reads the validated getter, so the key must be a recognized
valid key, not warn-and-ignored). Also covers payload/text builder shape and a
``config get/set`` round-trip via a scratch ``--config`` path.
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import subprocess
import sys
from contextlib import redirect_stderr

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


def _load(name):
    # Register in sys.modules before exec so @dataclass's
    # sys.modules[cls.__module__] lookup resolves (Python 3.14).
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(_BIN / f"{name}.py"))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


# ── defaults ────────────────────────────────────────────────────────────────

def test_alerts_projected_enabled_defaults_false():
    core = _load("_cctally_core")
    out = core._get_alerts_config({})
    assert out["projected_enabled"] is False


def test_budget_projected_enabled_defaults_false():
    core = _load("_cctally_core")
    out = core._get_budget_config({})
    assert out["projected_enabled"] is False


def test_alerts_projected_enabled_true_round_trips():
    core = _load("_cctally_core")
    out = core._get_alerts_config({"alerts": {"projected_enabled": True}})
    assert out["projected_enabled"] is True


def test_budget_projected_enabled_true_round_trips():
    core = _load("_cctally_core")
    out = core._get_budget_config({"budget": {"projected_enabled": True}})
    assert out["projected_enabled"] is True


# ── bool validation (non-bool rejected, not coerced) ─────────────────────────

def test_alerts_projected_enabled_rejects_non_bool():
    core = _load("_cctally_core")
    try:
        core._get_alerts_config({"alerts": {"projected_enabled": "yes"}})
    except core._AlertsConfigError:
        return
    raise AssertionError("non-bool alerts.projected_enabled was not rejected")


def test_budget_projected_enabled_rejects_non_bool():
    core = _load("_cctally_core")
    try:
        core._get_budget_config({"budget": {"projected_enabled": "yes"}})
    except core._BudgetConfigError:
        return
    raise AssertionError("non-bool budget.projected_enabled was not rejected")


# ── carry-forward MUST-FIX #1: NO "unknown config key" warning ───────────────

def test_alerts_projected_enabled_emits_no_unknown_key_warning():
    core = _load("_cctally_core")
    buf = io.StringIO()
    with redirect_stderr(buf):
        core._get_alerts_config({"alerts": {"projected_enabled": True}})
    assert "unknown alerts config key" not in buf.getvalue()


def test_budget_projected_enabled_emits_no_unknown_key_warning():
    core = _load("_cctally_core")
    buf = io.StringIO()
    with redirect_stderr(buf):
        core._get_budget_config({"budget": {"projected_enabled": True}})
    assert "unknown budget config key" not in buf.getvalue()


# ── payload/text builders (already shipped; lock their projected shape) ───────

def test_projected_payload_and_text_weekly():
    m = _load("_lib_alerts_payload")
    p = m._build_alert_payload_projected(
        metric="weekly_pct", threshold=100, projected_value=102.0,
        denominator=100.0, week_start_at="2026-06-01T00:00:00Z",
    )
    assert p["axis"] == "projected"
    assert p["metric"] == "weekly_pct"
    title, subtitle, body = m._alert_text_projected(p, None)
    assert "100%" in title
    assert "projection" in body.lower() or "pace" in body.lower()


def test_projected_payload_and_text_budget():
    m = _load("_lib_alerts_payload")
    p = m._build_alert_payload_projected(
        metric="budget_usd", threshold=100, projected_value=312.0,
        denominator=300.0, week_start_at="2026-06-01T00:00:00Z",
    )
    assert p["axis"] == "projected"
    assert p["metric"] == "budget_usd"
    title, subtitle, body = m._alert_text_projected(p, None)
    assert "$300" in body or "$312" in body


# ── config get/set round-trip via the CLI (scratch --config path) ─────────────

def _run_cli(data_dir, *args):
    import os

    env = dict(os.environ)
    env["CCTALLY_DATA_DIR"] = str(data_dir)
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    return subprocess.run(
        [sys.executable, str(_BIN / "cctally"), *args],
        capture_output=True, text=True, env=env,
    )


def test_config_set_get_alerts_projected_enabled_round_trip(tmp_path):
    set_res = _run_cli(tmp_path, "config", "set", "alerts.projected_enabled", "true")
    assert set_res.returncode == 0, set_res.stderr
    get_res = _run_cli(tmp_path, "config", "get", "alerts.projected_enabled")
    assert get_res.returncode == 0, get_res.stderr
    assert "true" in get_res.stdout


def test_config_set_get_budget_projected_enabled_round_trip(tmp_path):
    set_res = _run_cli(tmp_path, "config", "set", "budget.projected_enabled", "true")
    assert set_res.returncode == 0, set_res.stderr
    get_res = _run_cli(tmp_path, "config", "get", "budget.projected_enabled")
    assert get_res.returncode == 0, get_res.stderr
    assert "true" in get_res.stdout


def test_config_set_alerts_projected_enabled_non_bool_names_the_right_key(tmp_path):
    # Regression: the set path reuses _normalize_alerts_enabled_value, whose
    # ValueError hardcodes "alerts.enabled"; the error message must name the
    # ACTUAL key (alerts.projected_enabled), not the sibling it borrowed from.
    res = _run_cli(tmp_path, "config", "set", "alerts.projected_enabled", "maybe")
    assert res.returncode == 2
    assert "alerts.projected_enabled" in res.stderr
    assert "alerts.enabled:" not in res.stderr
