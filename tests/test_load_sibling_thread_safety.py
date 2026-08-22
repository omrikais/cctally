"""Regression test for issue #306 — the dashboard boot race.

``bin/cctally::_load_sibling`` registers each sibling in ``sys.modules``
*before* ``exec_module`` runs (the ``dataclass(frozen=True)`` contract needs the
name resolvable during body execution). Under the ``ThreadingHTTPServer``,
that ordering opened a window: a second request thread calling ``_load_sibling``
for the *same* not-yet-loaded module during the first thread's ``exec_module``
saw ``cached is not None`` and returned the **half-initialized** module — so a
top-level name defined mid-body (e.g. ``_lib_transcript_access.transcripts_allowed``)
wasn't bound yet → ``AttributeError`` on the transcript-gated routes at boot,
then self-heal once the body finished.

This test freezes ``exec_module`` exactly inside that pre-registration window
(no sleeps — an ``Event`` gates the sibling body) and asserts a concurrent
``_load_sibling`` never observes the partial module.
"""
from __future__ import annotations

import sys
import textwrap
import threading
import time
import types

from conftest import load_script

from tests._support_http import PRESENCE_BACKSTOP_SECONDS, remaining


def test_concurrent_load_sibling_never_returns_half_initialized_module(tmp_path, monkeypatch):
    ns = load_script()

    # Coordination channel the temp sibling body reaches through sys.modules.
    # It lets the test park the loader thread mid-``exec_module`` — the module
    # is registered in sys.modules but its SENTINEL is not yet bound — which is
    # the exact #306 window, deterministically and without a timing sleep.
    coord = types.ModuleType("_race_coord_306")
    coord.entered = threading.Event()   # set once the sibling body begins
    coord.release = threading.Event()   # test lets the body finish binding
    monkeypatch.setitem(sys.modules, "_race_coord_306", coord)

    sibling_name = "_race_sibling_306"
    (tmp_path / f"{sibling_name}.py").write_text(textwrap.dedent("""
        import sys
        _c = sys.modules["_race_coord_306"]
        _c.entered.set()          # loader is now inside exec_module, mid-body
        _c.release.wait(5)        # park here: SENTINEL still unbound above us
        SENTINEL = "ready"        # bound only AFTER the racer had its chance
    """))

    # Point _load_sibling's path resolution at tmp_path instead of bin/ (it reads
    # the module global ``__file__``), so the test never writes into bin/.
    monkeypatch.setitem(ns, "__file__", str(tmp_path / "cctally"))

    load_sibling = ns["_load_sibling"]
    results: dict[str, object] = {}

    def _loader():
        results["loader"] = load_sibling(sibling_name)

    def _racer():
        results["racer"] = load_sibling(sibling_name)

    inserted_bin_dir = str(tmp_path) in sys.path
    try:
        # ONE budget for the handoff wait and the three joins, not one each.
        # Four separate `PRESENCE_BACKSTOP_SECONDS` waits summed to the whole
        # 120-second pytest cap, so a load that never serialized spent the cap
        # here and pytest-timeout killed the worker before any of the four
        # could report which thread it had been waiting for.
        deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
        t_load = threading.Thread(target=_loader, name="loader-306", daemon=True)
        t_load.start()
        assert coord.entered.wait(remaining(deadline)), "temp sibling body never began executing"

        # At this instant the module is registered but SENTINEL is unbound.
        t_race = threading.Thread(target=_racer, name="racer-306", daemon=True)
        t_race.start()

        # Buggy _load_sibling: the racer returns the half-built module NOW.
        # Fixed _load_sibling: the racer blocks on the load lock and stays unset.
        # Either way this join returns, because the sibling body parks for at
        # most five seconds; `results` is then inspectable for a half-built
        # module.
        t_race.join(timeout=remaining(deadline))
        observed_partial = (
            "racer" in results and not hasattr(results["racer"], "SENTINEL")
        )

        coord.release.set()
        # The loader has finished the sibling import that `coord.release` just
        # unblocked, and the racer has returned with the load lock released.
        t_load.join(timeout=remaining(deadline))
        t_race.join(timeout=remaining(deadline))

        assert not observed_partial, (
            "#306: a concurrent _load_sibling returned a half-initialized module "
            "(registered in sys.modules but exec_module still mid-body — SENTINEL "
            "unbound). The load must serialize so every caller sees a complete module."
        )
        # And every caller must still receive the COMPLETE module.
        assert getattr(results.get("loader"), "SENTINEL", None) == "ready"
        assert getattr(results.get("racer"), "SENTINEL", None) == "ready"
    finally:
        coord.release.set()
        sys.modules.pop(sibling_name, None)
        if not inserted_bin_dir:
            with_tmp = str(tmp_path)
            if with_tmp in sys.path:
                sys.path.remove(with_tmp)


def test_completed_sibling_cache_keeps_its_module_across_loader_generations(
    tmp_path, monkeypatch,
):
    """An older request thread must not adopt a newer partial module.

    ``load_script()`` deliberately removes ``_cctally_*`` entries from
    ``sys.modules`` before executing a fresh cctally test namespace. A request
    thread can still be using the prior namespace. Its completed-sibling cache
    therefore has to retain the module object it completed, rather than using
    its name as permission to read whatever a newer loader has just registered
    globally under that name.
    """
    coord = types.ModuleType("_race_coord_629")
    coord.entered = threading.Event()
    coord.release = threading.Event()
    monkeypatch.setitem(sys.modules, "_race_coord_629", coord)

    sibling_name = "_cctally_race_sibling_629"
    sibling_path = tmp_path / f"{sibling_name}.py"
    sibling_path.write_text('SENTINEL = "old-complete"\n', encoding="utf-8")

    old_ns = load_script()
    monkeypatch.setitem(old_ns, "__file__", str(tmp_path / "cctally-old"))
    old_loader = old_ns["_load_sibling"]
    old_module = old_loader(sibling_name)
    assert old_module.SENTINEL == "old-complete"

    # A new test generation removes the old sys.modules entry. Park its load
    # after registration but before SENTINEL is bound, reproducing the exact
    # partially-initialized-module window from the hosted failure.
    new_ns = load_script()
    monkeypatch.setitem(new_ns, "__file__", str(tmp_path / "cctally-new"))
    sibling_path.write_text(textwrap.dedent("""
        import sys
        _c = sys.modules["_race_coord_629"]
        _c.entered.set()
        _c.release.wait(5)
        SENTINEL = "new-complete"
    """), encoding="utf-8")

    results: dict[str, object] = {}

    def _new_loader():
        results["new"] = new_ns["_load_sibling"](sibling_name)

    inserted_bin_dir = str(tmp_path) in sys.path
    t_new = threading.Thread(target=_new_loader, name="new-loader-629", daemon=True)
    try:
        t_new.start()
        assert coord.entered.wait(PRESENCE_BACKSTOP_SECONDS), "new sibling generation never began executing"

        observed = old_loader(sibling_name)

        assert observed is old_module
        assert getattr(observed, "SENTINEL", None) == "old-complete", (
            "an older completed-sibling cache returned the newer generation's "
            "half-initialized sys.modules entry"
        )
    finally:
        coord.release.set()
        # timing-budget: the second importer has returned now that `coord.release` unblocked the first
        t_new.join(timeout=PRESENCE_BACKSTOP_SECONDS)
        sys.modules.pop(sibling_name, None)
        if not inserted_bin_dir:
            with_tmp = str(tmp_path)
            if with_tmp in sys.path:
                sys.path.remove(with_tmp)

    assert getattr(results.get("new"), "SENTINEL", None) == "new-complete"
