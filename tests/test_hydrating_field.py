"""The additive `hydrating` DataSnapshot field + envelope serialization (issue #278 §1.4).

`hydrating` is a single additive boolean on ``DataSnapshot`` (default ``False``)
serialized into the dashboard envelope by ``snapshot_to_envelope``. It is
``True`` only on the cheap first-paint seed and A2's throttled partial
republishes; every other snapshot-producing path leaves/forces it ``False`` —
notably the idle short-circuit (data stable ⇒ not hydrating).
"""
import dataclasses
import datetime as dt

from conftest import load_script, redirect_paths  # type: ignore

NOW = dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.timezone.utc)


def test_datasnapshot_hydrating_defaults_false(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    snap = ns["_tui_empty_snapshot"](NOW)
    assert snap.hydrating is False


def test_envelope_serializes_hydrating(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=NOW)
    assert env["hydrating"] is False
    hydrating_snap = dataclasses.replace(snap, hydrating=True)
    env2 = ns["snapshot_to_envelope"](hydrating_snap, now_utc=NOW)
    assert env2["hydrating"] is True


def test_idle_snapshot_forces_hydrating_false(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # ``load_script`` drops cached ``_cctally_*`` siblings, so this import
    # re-executes _cctally_tui against the fresh cctally module.
    import _cctally_tui  # bin/ is on sys.path (conftest)
    prior = dataclasses.replace(
        _cctally_tui._tui_empty_snapshot(NOW), hydrating=True
    )
    idle = _cctally_tui._tui_build_idle_snapshot(
        prior, now_utc=NOW, precompute_envelope=False,
        runtime_bind=None, raw_config={}, errors=[],
    )
    assert idle.hydrating is False
