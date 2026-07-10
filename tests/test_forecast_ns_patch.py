"""Binding-semantics regression for cmd_forecast / cmd_report / cmd_budget (§5.C).

Proves the three commands' share + view_models + budget-status call paths resolve
through cctally's namespace (the `c.<name>` accessor), NOT honest imports — so
monkeypatch.setattr(cctally, "X", …) is preserved after the bin/_cctally_forecast.py
extraction. Also guards the _iso_z override ordering and the budget set/unset paths.

R2/F1: only STAYS / accessor-routed symbols are patched here. Moved intra-module
helpers (_build_budget_status_inputs, _load_forecast_inputs, _compute_forecast) are
NOT patched — post-cut they are sibling-local globals.

Non-vacuity (Task 5 Step 3): RED if build_forecast_view / build_trend_view /
compute_budget_status / a _build_*_snapshot / _share_render_and_emit is honest-imported
in the sibling.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pytest

from conftest import load_isolated_cctally_module

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


@pytest.fixture
def cctally_mod(tmp_path, monkeypatch):
    """Load bin/cctally under the canonical isolated data dir (#127).

    Via the shared ``load_isolated_cctally_module`` helper so the loader
    gets ``_cctally_core`` path redirection — without it, a cached
    ``_cctally_core`` made these tests read the real prod DB (#127).
    """
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def test_iso_z_is_the_canonical_envelope_version(cctally_mod):
    """#279 S6 W4: cctally._iso_z collapses to the single canonical
    _lib_json_envelope._iso_z (None-safe, seconds precision). The former
    double-bind (dashboard then forecast-wins) is gone; forecast and the
    dashboard envelope now both alias the canonical, so all three are the
    same object (doctor's divergent _iso_z deliberately keeps its own name)."""
    mod = cctally_mod
    assert mod._iso_z is mod._lib_json_envelope._iso_z
    assert mod._iso_z is mod._cctally_forecast._iso_z
    assert mod._iso_z(dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)) == "2026-01-02T03:04:05Z"
    assert mod._iso_z(None) is None


def test_cmd_forecast_resolves_view_and_share_through_namespace(cctally_mod, monkeypatch):
    """forecast --format md: build_forecast_view is called pre-guard (always fires),
    then the empty-data share branch emits a snapshot. Patch the accessor-routed
    symbols and assert they fire — proving cmd_forecast reaches them via c., not
    honest import."""
    mod = cctally_mod
    calls = {"view": 0, "snap": 0, "emit": 0}
    real_view = mod.build_forecast_view

    def spy_view(*a, **k):
        calls["view"] += 1
        # Pass-through: with an empty data dir view.output is None, so the
        # empty-data share branch (which calls _build_forecast_snapshot ->
        # _share_render_and_emit) runs.
        return real_view(*a, **k)

    def spy_snap(*a, **k):
        calls["snap"] += 1
        return object()

    def spy_emit(*a, **k):
        calls["emit"] += 1
        return None

    monkeypatch.setattr(mod, "build_forecast_view", spy_view)
    monkeypatch.setattr(mod, "_build_forecast_snapshot", spy_snap)
    monkeypatch.setattr(mod, "_share_render_and_emit", spy_emit)
    monkeypatch.setattr(mod, "_share_validate_args", lambda args: None)

    rc = mod.cmd_forecast(_forecast_args())
    assert rc == 0
    assert calls["view"] >= 1, "build_forecast_view not reached via the namespace"
    assert calls["snap"] >= 1, "_build_forecast_snapshot not reached via the namespace"
    assert calls["emit"] >= 1, "_share_render_and_emit not reached via the namespace"


def test_cmd_report_resolves_view_and_share_through_namespace(cctally_mod, monkeypatch):
    """report --format md: build_trend_view sits behind `if not weeks:` where
    weeks = get_recent_weeks(...). get_recent_weeks is accessor-routed (lives in
    _cctally_weekrefs.py, re-exported on the cctally ns) → patch it non-empty to
    reach build_trend_view, then assert the view + share spies fire."""
    mod = cctally_mod
    calls = {"weeks": 0, "view": 0, "snap": 0, "emit": 0}

    # get_recent_weeks lives in _cctally_weekrefs.py, re-exported on the cctally
    # ns → namespace-patchable (accessor-routed). Return a minimal non-empty list
    # of the real WeekRef so the `if not weeks:` guard is passed and the path
    # reaches build_trend_view.
    def spy_weeks(conn, limit):
        calls["weeks"] += 1
        return [mod.make_week_ref(
            week_start_date="2026-01-05", week_end_date="2026-01-11",
            week_start_at="2026-01-05T00:00:00Z", week_end_at="2026-01-12T00:00:00Z",
        )]

    monkeypatch.setattr(mod, "get_recent_weeks", spy_weeks)
    monkeypatch.setattr(mod, "_apply_reset_events_to_weekrefs",
                        lambda conn, refs: list(refs))
    monkeypatch.setattr(mod, "build_trend_view",
                        lambda *a, **k: (calls.__setitem__("view", calls["view"] + 1), mod.TrendView())[1])
    monkeypatch.setattr(mod, "_build_report_snapshot",
                        lambda *a, **k: (calls.__setitem__("snap", calls["snap"] + 1), object())[1])
    monkeypatch.setattr(mod, "_share_render_and_emit",
                        lambda *a, **k: calls.__setitem__("emit", calls["emit"] + 1))
    monkeypatch.setattr(mod, "_share_validate_args", lambda args: None)

    rc = mod.cmd_report(_report_args())
    assert rc == 0
    assert calls["weeks"] >= 1, "get_recent_weeks not reached via the namespace"
    assert calls["view"] >= 1, "build_trend_view not reached via the namespace"
    assert calls["emit"] >= 1, "_share_render_and_emit not reached via the namespace"


def test_cmd_budget_resolves_status_and_share_through_namespace(cctally_mod, monkeypatch):
    """budget --format md: the guard _build_budget_status_inputs is MOVED (intra-module,
    NOT patchable). Seed a configured weekly_usd + a current-week usage snapshot so the
    REAL helper returns non-None inputs, reaching compute_budget_status (accessor-routed).
    Then patch only the accessor-routed tail and assert it fires.

    R2/F1: `_build_budget_snapshot` is ALSO a MOVED intra-module helper (called bare in
    cmd_budget, NOT `c._build_budget_snapshot`) — UNLIKE report/forecast whose snapshot
    builders live in _cctally_share and ARE accessor-routed. So do NOT patch it; let the
    real (accessor-fed) snapshot builder run, and assert the accessor-routed
    `compute_budget_status` + `_share_render_and_emit` fire instead."""
    mod = cctally_mod
    _seed_budget_fixture(mod, monkeypatch)  # weekly_usd + current-week usage snapshot + pinned now
    calls = {"status": 0, "emit": 0}
    real_status = mod.compute_budget_status

    def spy_status(inputs):
        calls["status"] += 1
        return real_status(inputs)

    monkeypatch.setattr(mod, "compute_budget_status", spy_status)
    monkeypatch.setattr(mod, "_share_render_and_emit",
                        lambda *a, **k: calls.__setitem__("emit", calls["emit"] + 1))
    monkeypatch.setattr(mod, "_share_validate_args", lambda args: None)

    rc = mod.cmd_budget(_budget_args(action=None))
    assert rc == 0
    assert calls["status"] >= 1, "compute_budget_status not reached via the namespace"
    assert calls["emit"] >= 1, "_share_render_and_emit not reached via the namespace"


def test_cmd_budget_set_reconciles_through_namespace(cctally_mod, monkeypatch):
    """budget set: _cmd_budget_set calls _reconcile_budget_on_config_write (STAYS,
    accessor-routed). Patch it and assert it fires — proves the §1.1 cross-cutting edge."""
    mod = cctally_mod
    fired = {"n": 0}
    monkeypatch.setattr(mod, "_reconcile_budget_on_config_write",
                        lambda *a, **k: fired.__setitem__("n", fired["n"] + 1))
    rc = mod.cmd_budget(_budget_args(action="set", amount=50.0))
    assert rc == 0
    assert fired["n"] == 1, "_cmd_budget_set did not reach _reconcile_budget_on_config_write via the namespace"


def test_cmd_budget_unset_clears_via_save_config(cctally_mod, monkeypatch):
    """budget unset: does NOT reconcile (R2/F2) — it clears weekly_usd via save_config
    (STAYS, accessor-routed). Assert rc 0, the config is cleared, and save_config fired."""
    mod = cctally_mod
    # Pre-seed a budget so unset has something to clear.
    mod.cmd_budget(_budget_args(action="set", amount=50.0))
    real_save = mod.save_config
    fired = {"n": 0}

    def spy_save(cfg):
        fired["n"] += 1
        return real_save(cfg)

    monkeypatch.setattr(mod, "save_config", spy_save)
    rc = mod.cmd_budget(_budget_args(action="unset"))
    assert rc == 0
    assert fired["n"] >= 1, "_cmd_budget_unset did not reach save_config via the namespace"
    cfg = mod.load_config()
    assert cfg.get("budget", {}).get("weekly_usd") is None


# --- arg builders (re-grep each command's args. / getattr(args, reads to complete) ---

def _report_args(**ov):
    ns = argparse.Namespace(
        weeks=8, detail=False, json=False, mode="auto", offline=False,
        project=None, reveal_projects=False, sync_current=False, theme="light",
        format="md", no_branding=False, output=None, copy=False,
        open_after_write=False, config=None, tz=None, week_start_name=None,
    )
    for k, v in ov.items():
        setattr(ns, k, v)
    return ns


def _forecast_args(**ov):
    ns = argparse.Namespace(
        as_of=None, targets="100,90", weeks=8, json=False, status_line=False,
        no_sync=True, color=False, reveal_projects=False, theme="light",
        format="md", no_branding=False, output=None, copy=False,
        open_after_write=False, config=None, tz=None, week_start_name=None,
    )
    for k, v in ov.items():
        setattr(ns, k, v)
    return ns


def _budget_args(*, action, amount=None, **ov):
    ns = argparse.Namespace(
        action=action, amount=amount, reveal_projects=False, theme="light",
        format=("md" if action is None else None), json=False, config=None,
        no_branding=False, output=None, copy=False, open_after_write=False, tz=None,
    )
    for k, v in ov.items():
        setattr(ns, k, v)
    return ns


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_budget_fixture(mod, monkeypatch):
    """Write a configured weekly_usd + a current-week usage snapshot so the real
    _build_budget_status_inputs resolves a window and returns non-None inputs.

    Mirrors tests/test_budget_alerts.py::_seed_window: pin CCTALLY_AS_OF so
    _command_as_of() is deterministic, then seed a boundary-aware
    weekly_usage_snapshots row whose [week_start_at, week_end_at) window contains
    that instant (so _fetch_current_week_snapshots / _resolve_current_budget_window
    return non-None). spent_usd is 0 (empty session cache), which is fine — budget
    still resolves and reaches compute_budget_status.
    """
    as_of = dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
    week_start = dt.datetime(2026, 5, 18, 0, 0, 0, tzinfo=dt.timezone.utc)
    week_end = dt.datetime(2026, 5, 25, 0, 0, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(as_of))

    cfg = mod.load_config()
    cfg.setdefault("budget", {})["weekly_usd"] = 50.0
    mod.save_config(cfg)

    conn = mod.open_db()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, five_hour_percent, "
            " page_url, source, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(week_start + dt.timedelta(hours=1)),
                week_start.date().isoformat(),
                (week_end - dt.timedelta(seconds=1)).date().isoformat(),
                _iso(week_start),
                _iso(week_end),
                40.0,
                10.0,
                None,
                "fixture",
                json.dumps({"fixture": True}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
