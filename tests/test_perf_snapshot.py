"""_tui_build_snapshot phase-tree shape (issue #276, Session A).

Structural (never golden) assertions on the dashboard-snapshot spine's phase
tree: the root is ``snapshot`` with the named seam children, each cache
reconciliation surfaces its ``use_*_cache`` hit boolean as meta, and the
short-lived reconcile connection open is attributed separately.
"""
import datetime as dt

from conftest import load_script, redirect_paths  # type: ignore

NOW_UTC = dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.timezone.utc)


def _perf_mod():
    import _lib_perf  # bin/ is on sys.path (conftest)
    return _lib_perf


def test_snapshot_phase_tree_shape(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    perf = _perf_mod()
    perf.set_enabled(True)
    try:
        perf.reset_thread()
        ns["_tui_build_snapshot"](
            now_utc=NOW_UTC, skip_sync=False,
            precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        root = perf.current_root().to_dict()
        assert root["name"] == "snapshot"
        children = root.get("children", [])
        names = {c["name"] for c in children}
        assert {"sync", "signature"} <= names
        assert any(n.startswith("build.") for n in names)
        assert {"doctor", "envelope.precompute"} <= names
        cache_open = [c for c in children
                      if c["name"] == "reconcile.cache_open"]
        assert len(cache_open) == 1
        assert cache_open[0].get("meta", {}) == {}
        recon = [c for c in children
                 if (c["name"].startswith("reconcile.")
                     and c["name"] != "reconcile.cache_open")]
        assert recon, "expected reconcile.* phases on the first (non-idle) build"
        # use_*_cache surfaced as meta on every reconcile phase
        assert all("hit" in c.get("meta", {}) for c in recon)
        # the completed tree is stashed for the loopback /api/debug/backend endpoint
        last = perf.last_backend_perf()
        assert last is not None
        assert last["phases"]["name"] == "snapshot"
    finally:
        perf.set_enabled(False)
        perf.reset_thread()
