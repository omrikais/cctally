"""Six previously-unwrapped _tui_build_snapshot builders now attribute (issue #278 §0).

Five (in fact six) heavy builders in ``_tui_build_snapshot`` ran in bare
``try/except`` blocks with no ``_perf.phase`` wrapper, so a ``--trace`` cold
build silently dropped their work into "unattributed". This asserts that a
traced ``_tui_build_snapshot`` now emits all six ``build.*`` phase keys —
proven non-vacuous by their absence before the wrap.
"""
import datetime as dt

from conftest import load_script, redirect_paths  # type: ignore

NOW_UTC = dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.timezone.utc)

_EXPECTED_BUILD_PHASES = {
    "build.weekly_history",
    "build.blocks",
    "build.daily",
    "build.alerts",
    "build.five_hour_milestones",
    "build.cache_report",
}


def _perf_mod():
    import _lib_perf  # bin/ is on sys.path (conftest)
    return _lib_perf


def _flatten_names(node, acc):
    acc.add(node["name"])
    for c in node.get("children", []):
        _flatten_names(c, acc)
    return acc


def test_six_build_phases_now_attribute(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    perf = _perf_mod()
    perf.set_enabled(True)
    try:
        perf.reset_thread()
        ns["_tui_build_snapshot"](
            now_utc=NOW_UTC, skip_sync=True,
            precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        root = perf.current_root().to_dict()
        assert root["name"] == "snapshot"
        names = _flatten_names(root, set())
        missing = _EXPECTED_BUILD_PHASES - names
        assert not missing, f"missing build phases: {sorted(missing)}"
    finally:
        perf.set_enabled(False)
        perf.reset_thread()
