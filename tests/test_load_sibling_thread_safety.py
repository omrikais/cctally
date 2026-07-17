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
import types

from conftest import load_script


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
        t_load = threading.Thread(target=_loader, name="loader-306", daemon=True)
        t_load.start()
        assert coord.entered.wait(5), "temp sibling body never began executing"

        # At this instant the module is registered but SENTINEL is unbound.
        t_race = threading.Thread(target=_racer, name="racer-306", daemon=True)
        t_race.start()

        # Buggy _load_sibling: the racer returns the half-built module NOW.
        # Fixed _load_sibling: the racer blocks on the load lock and stays unset.
        t_race.join(timeout=3.0)
        observed_partial = (
            "racer" in results and not hasattr(results["racer"], "SENTINEL")
        )

        coord.release.set()
        t_load.join(timeout=5)
        t_race.join(timeout=5)

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
