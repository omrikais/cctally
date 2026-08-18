"""#583 S2 — `last_sync_at` means "last SUCCESSFUL validation".

The client already presents `sync_age_s` that way. Every producer named in
spec §6.1 is corrected to obey it, and that list is complete: no other site
may stamp the field. The intended visible consequence is that a dashboard
whose legs have failed for an hour reports the true age and turns amber then
red, where it used to report "synced 3s ago" in green.
"""
import dataclasses
import datetime as dt
import types

import pytest

from conftest import load_script, redirect_paths

NOW = dt.datetime(2026, 8, 16, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def mods(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import importlib
    tui = importlib.import_module("_cctally_tui")
    dash = importlib.import_module("_cctally_dashboard")
    return ns, tui, dash


def _prior(tui, **fields):
    return dataclasses.replace(tui._tui_empty_snapshot(NOW), **fields)


def test_clean_idle_tick_advances_the_stamp(mods):
    """An idle tick IS a successful validation: it re-verified through four
    independent gates that nothing changed, so the reused rows are current."""
    ns, tui, _dash = mods
    out = tui._tui_build_idle_snapshot(
        _prior(tui, last_sync_at=100.0), now_utc=NOW,
        precompute_envelope=False, runtime_bind=None, raw_config={}, errors=[],
    )
    assert out.last_sync_at is not None
    assert out.last_sync_at > 100.0
    assert out.last_sync_error is None


def test_idle_tick_with_an_error_preserves_the_prior_stamp(mods):
    ns, tui, _dash = mods
    out = tui._tui_build_idle_snapshot(
        _prior(tui, last_sync_at=100.0), now_utc=NOW,
        precompute_envelope=False, runtime_bind=None, raw_config={},
        errors=["milestones: boom"],
    )
    assert out.last_sync_at == 100.0
    assert out.last_sync_error


def test_idle_tick_with_an_error_and_no_prior_success_stays_none(mods):
    """There is no earlier success to preserve, so nothing may be invented."""
    ns, tui, _dash = mods
    out = tui._tui_build_idle_snapshot(
        _prior(tui, last_sync_at=None), now_utc=NOW,
        precompute_envelope=False, runtime_bind=None, raw_config={},
        errors=["milestones: boom"],
    )
    assert out.last_sync_at is None


def test_degraded_retry_publishes_none(mods):
    """It produced no successful snapshot, and it builds from the empty
    snapshot, so there is no earlier success to preserve."""
    ns, tui, _dash = mods
    out = tui._tui_stats_retry_degraded_snapshot(
        now_utc=NOW, exc=RuntimeError("stats open failed"),
        precompute_envelope=False, runtime_bind=None,
    )
    assert out.last_sync_at is None
    assert out.last_sync_error


def test_deferred_stats_snapshot_publishes_none(mods):
    """`_dashboard_stats_deferred_snapshot` stamped a fresh success while
    also carrying a `stats-open` error."""
    ns, _tui, dash = mods
    args = types.SimpleNamespace(no_sync=False, host="127.0.0.1")
    out = dash._dashboard_stats_deferred_snapshot(
        args, pinned_now=NOW, exc=RuntimeError("stats open failed"),
    )
    assert out.last_sync_at is None
    assert out.last_sync_error


def test_first_failed_initial_build_publishes_none_not_a_fresh_success(
        mods, monkeypatch):
    ns, tui, dash = mods
    args = types.SimpleNamespace(no_sync=False, host="127.0.0.1")

    def _boom(*a, **kw):
        raise RuntimeError("cache open failed")

    monkeypatch.setattr(tui, "_tui_build_current_week", _boom)
    monkeypatch.setattr(tui, "_tui_build_forecast_view", _boom)
    out = dash._dashboard_initial_snapshot_once(
        args, pinned_now=NOW, display_tz_pref_override=None,
        stats_heal_attempted=False,
    )
    assert out.last_sync_error
    assert out.last_sync_at is None


def test_successful_initial_build_advances_the_stamp(mods):
    ns, _tui, dash = mods
    args = types.SimpleNamespace(no_sync=False, host="127.0.0.1")
    out = dash._dashboard_initial_snapshot_once(
        args, pinned_now=NOW, display_tz_pref_override=None,
        stats_heal_attempted=False,
    )
    assert out.last_sync_error is None
    assert out.last_sync_at is not None


def test_final_publish_preserves_the_prior_stamp_on_a_failed_build(
        mods, monkeypatch):
    """The decoupled ingest failure is merged into `last_sync_error`, so the
    build recorded a failure and must not read as a fresh success."""
    ns, tui, dash = mods
    ref = dash._SnapshotRef(
        dataclasses.replace(dash._empty_dashboard_snapshot(), last_sync_at=100.0)
    )

    class _Hub:
        def __init__(self):
            self.frames = []

        def publish(self, snap):
            self.frames.append(snap)

    hub = _Hub()
    built = dataclasses.replace(
        dash._empty_dashboard_snapshot(),
        last_sync_at=500.0, last_sync_error="milestones: boom",
    )
    monkeypatch.setitem(ns, "_tui_build_snapshot", lambda **kw: built)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=None, display_tz_pref_override=None,
    )
    locked(skip_sync=False)
    assert hub.frames[-1].last_sync_error
    assert hub.frames[-1].last_sync_at == 100.0


def test_final_publish_advances_the_stamp_on_a_clean_build(mods, monkeypatch):
    ns, tui, dash = mods
    ref = dash._SnapshotRef(
        dataclasses.replace(dash._empty_dashboard_snapshot(), last_sync_at=100.0)
    )

    class _Hub:
        def __init__(self):
            self.frames = []

        def publish(self, snap):
            self.frames.append(snap)

    hub = _Hub()
    built = dataclasses.replace(
        dash._empty_dashboard_snapshot(), last_sync_at=500.0,
    )
    monkeypatch.setitem(ns, "_tui_build_snapshot", lambda **kw: built)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=None, display_tz_pref_override=None,
    )
    locked(skip_sync=False)
    assert hub.frames[-1].last_sync_error is None
    assert hub.frames[-1].last_sync_at == 500.0


def test_tui_health_renders_daemon_error_not_sync_paused(mods):
    """Preserve 20: `last_sync_error` is tested first, so `sync paused` stays
    unreachable for every path that newly publishes None."""
    ns, tui, _dash = mods
    cw = ns["TuiCurrentWeek"](
        week_start_at=dt.datetime(2026, 8, 10, 12, 0, tzinfo=dt.timezone.utc),
        week_end_at=dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.timezone.utc),
        used_pct=57.0, five_hour_pct=None, five_hour_resets_at=None,
        spent_usd=10.0, dollars_per_percent=0.18,
        latest_snapshot_at=NOW - dt.timedelta(minutes=3),
    )
    snap = _prior(
        tui, current_week=cw, last_sync_at=None, last_sync_error="boom",
    )
    runtime = ns["RuntimeState"](
        variant="expressive", focus_index=3, session_scroll=0, show_help=False,
        toast=None, color_enabled=False, tz="utc",
    )
    line = "\n".join(tui._tui_panel_current_week_hero(snap, runtime, 100))
    assert "daemon error" in line
    assert "sync paused" not in line
