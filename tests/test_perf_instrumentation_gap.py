"""Six previously-unwrapped _tui_build_snapshot builders now attribute (issue #278 §0).

Five (in fact six) heavy builders in ``_tui_build_snapshot`` ran in bare
``try/except`` blocks with no ``_perf.phase`` wrapper, so a ``--trace`` cold
build silently dropped their work into "unattributed". This asserts that a
traced ``_tui_build_snapshot`` now emits all six ``build.*`` phase keys —
proven non-vacuous by their absence before the wrap.

Issue #566 §5.1 item 6 adds the second half. The ``precompute_envelope`` tail
— the legacy ``snapshot_to_envelope`` projection and ``_tui_build_source_bundle``
— was still unwrapped, so on the maintainer's store the trace accounted for
about 12.2s of an 84s root and made the region holding 79% of the build
invisible. ``test_source_bundle_and_envelope_projection_attribute`` injects a
known amount of work into each of the two calls and asserts the root's named
children account for it.
"""
import datetime as dt
import time

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


# Injected per-call work, in seconds. Large enough to dominate the tiny
# fixture store's own build cost, so the coverage ratio below measures
# attribution rather than host timing noise.
_INJECTED_S = 0.25


def test_source_bundle_and_envelope_projection_attribute(monkeypatch, tmp_path):
    """The precompute_envelope tail attributes to named children (#566).

    A bare name check would pass for a phase that exists but brackets the wrong
    statement, leaving the work just as unattributed. So each of the two calls
    gets a known 0.25s injected and its phase must account for at least that
    much: a phase around the wrong statement holds none of it.

    No coverage RATIO is asserted. An earlier form required the root's named
    children to cover 90% of the root, which passed alone and failed inside the
    full parallel estate, because the root's unnamed remainder — imports,
    configuration reads, connection setup — grows with host contention while
    the injected work does not. The assertions below compare each phase against
    a quantity that only ever grows under load, so contention cannot turn them
    red.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_tui

    real_bundle = _cctally_tui._tui_build_source_bundle
    real_envelope = ns["snapshot_to_envelope"]

    def slow_bundle(*args, **kwargs):
        time.sleep(_INJECTED_S)
        return real_bundle(*args, **kwargs)

    def slow_envelope(*args, **kwargs):
        time.sleep(_INJECTED_S)
        return real_envelope(*args, **kwargs)

    monkeypatch.setattr(_cctally_tui, "_tui_build_source_bundle", slow_bundle)
    monkeypatch.setitem(ns, "snapshot_to_envelope", slow_envelope)

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
        children = {c["name"]: c["elapsed_ms"] for c in root.get("children", [])}
        assert "build.source_bundle" in children
        assert "envelope.legacy_projection" in children
        injected_ms = _INJECTED_S * 1000.0
        assert children["build.source_bundle"] >= injected_ms
        assert children["envelope.legacy_projection"] >= injected_ms
        # Both are DIRECT children of the root, so the tail is attributed at
        # the level the trace reader looks at rather than buried under an
        # unrelated parent.
        assert {"build.source_bundle", "envelope.legacy_projection"} <= set(
            children)
    finally:
        perf.set_enabled(False)
        perf.reset_thread()
