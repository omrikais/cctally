"""Tests for the ONE cctally loader primitive, tests/_script_loader.py (#630 S6).

The primitive exists for two behaviours, not for tidiness, and both of them are
invisible until they are absent. A hand-rolled `SourceFileLoader` reproduces
neither, which is why sixty-four modules that hand-rolled one were each one
`monkeypatch` away from writing to the real production data directory.
"""
from __future__ import annotations

import sys

import pytest

import _script_loader
from _script_loader import SCRIPT_PATH, load_script_module
from conftest import load_script


def test_the_load_drops_cached_cctally_siblings():
    """Without this, a sibling's `import cctally` stays pinned to the OLD module.

    `tests/conftest.py` states the consequence in its own words: monkeypatches
    on the new `cctally.CHANGELOG_PATH` do not propagate into MOVED helpers, and
    tests that monkeypatch real-path constants leak writes to the on-disk repo.
    That is preserve-item 3 reached by another route, so it is asserted rather
    than assumed.
    """
    sys.modules["_cctally_a_fake_sibling"] = object()
    try:
        load_script_module()
        assert "_cctally_a_fake_sibling" not in sys.modules
    finally:
        sys.modules.pop("_cctally_a_fake_sibling", None)


def test_the_load_does_not_evict_the_loader_module_itself():
    """The primitive must not delete its own `sys.modules` entry.

    Losing that entry does not break the caller that already holds a reference,
    but the next `from _script_loader import ...` re-executes the file, which
    recompiles bin/cctally, discards the process-wide code cache, and mints a
    second identity for the module that is supposed to be the only one.
    """
    before = sys.modules["_script_loader"]
    load_script_module()
    assert sys.modules.get("_script_loader") is before


def test_the_loader_module_name_is_outside_the_sibling_eviction_pattern():
    """The estate sweeps `_cctally_*` out of `sys.modules` in its own code.

    Twenty modules run a hand-rolled sweep that deletes every `_cctally_*`
    entry except `_cctally_core`. While this module was named
    `tests/_cctally_loader.py`, every one of those sweeps deleted it, and the
    exemption inside the primitive could not prevent that — the exemption
    governs the primitive's own sweep and nothing else. Measured before the
    rename: `pytest tests/test_cli_debug_traceback.py tests/test_cctally_loader.py`
    failed with `KeyError: '_cctally_loader'`.

    The name is the fix, so the name is what is asserted.
    """
    assert not _script_loader.__name__.startswith("_cctally_")


def test_a_hand_rolled_estate_sweep_leaves_the_loader_module_bound():
    """The same rule, measured against the sweep the estate actually runs."""
    before = sys.modules["_script_loader"]
    evicted = [
        n for n in sys.modules
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]
    try:
        for name in evicted:
            del sys.modules[name]
        assert sys.modules.get("_script_loader") is before
    finally:
        load_script_module()


def test_a_module_level_sibling_reference_goes_stale_across_a_load():
    """What the eviction is FOR, measured on a real sibling rather than a stub.

    The three tests above insert synthetic `sys.modules` entries, so none of
    them shows what happens to a module a test actually holds. A caller that
    binds `bin/_cctally_db.py` at its own module top keeps that object across a
    later load — the binding is the caller's, and nothing rebinds it — while
    `sys.modules` receives a FRESH instance whose `import cctally` resolves to
    the new module. Holding the stale object is how a monkeypatch applied to the
    new `cctally` fails to reach a moved helper, so the divergence is pinned
    here rather than described.
    """
    first = load_script_module()
    import _cctally_db  # noqa: F401  -- bin/ is on sys.path

    held = sys.modules["_cctally_db"]
    second = load_script_module()
    assert second is not first

    # The eviction runs before the exec, and the exec then loads this sibling
    # again, so the entry is present afterwards — as a DIFFERENT object. That
    # difference is the whole mechanism: the caller's binding still names the
    # old module while the fresh `cctally` uses the new one.
    rebound = sys.modules.get("_cctally_db")
    assert rebound is not None, (
        "the exec must repopulate the sibling; without this the next "
        "assertion passes on absence, which is what the first draft of "
        "this test wrongly asserted"
    )
    assert rebound is not held, (
        "the load no longer replaces cached siblings; a reference captured "
        "before it would now be the same object the fresh cctally uses, and "
        "the isolation this primitive exists for has changed meaning"
    )
    assert sys.modules["cctally"] is second


def test_the_load_keeps_cctally_core():
    """The kernel is the ONE sibling that must survive.

    `_cctally_core` does not `import cctally` — it uses the call-time
    `_cctally()` accessor — so its module state is safe across reloads, and
    tests monkeypatch `_cctally_core.X` through a stable module-top import that
    would go stale if the module were replaced under it.
    """
    import _cctally_core  # noqa: F401  -- bin/ is on sys.path

    before = sys.modules["_cctally_core"]
    load_script_module()
    assert sys.modules["_cctally_core"] is before


def test_the_load_rederives_paths_from_the_current_home(monkeypatch, tmp_path):
    """`setenv("HOME", tmp)` then load must surface HOME-derived constants."""
    import _cctally_core

    monkeypatch.setenv("HOME", str(tmp_path))
    load_script_module()
    assert str(tmp_path) in str(_cctally_core.APP_DIR), _cctally_core.APP_DIR


def test_load_script_still_returns_the_module_namespace_dict():
    """369 modules index the return value as a namespace; that shape survives.

    The dict IS the module's `__dict__`, so a mutation through either spelling
    is visible through the other — which is what lets a test write
    `monkeypatch.setitem(ns, "X", v)` and have a sibling's `import cctally` see
    it.
    """
    ns = load_script()
    assert isinstance(ns, dict)
    module = sys.modules["cctally"]
    assert module.__dict__ is ns
    ns["_a_probe_binding"] = 17
    assert module._a_probe_binding == 17


def test_the_module_form_and_the_dict_form_are_the_same_load():
    module = load_script_module()
    assert module is sys.modules["cctally"]
    assert module.__file__ == str(SCRIPT_PATH)
    ns = load_script()
    assert ns is not module.__dict__, "each call must build a FRESH module"
    assert set(ns) == set(module.__dict__) - {"__name__", "__loader__", "__spec__"} | (
        set(ns) & {"__name__", "__loader__", "__spec__"}
    )


def test_an_alternate_identity_gets_its_own_durable_entry():
    """`_cctally_for_tests` is behavioural, not cosmetic — but not for the
    reason the spec gave.

    What it buys is a durable `sys.modules` entry under its own name, which the
    loading module holds across later `load_script()` calls that rebind
    `sys.modules["cctally"]`. What it does NOT buy is leaving that canonical
    entry alone; see the test below, which measures that.
    """
    canonical = load_script_module()
    try:
        alternate = load_script_module("_cctally_for_tests")
        assert sys.modules["_cctally_for_tests"] is alternate
        assert alternate is not canonical
    finally:
        # In a `finally`, because a failing assertion above would otherwise
        # leave this entry bound for the rest of the xdist worker.
        sys.modules.pop("_cctally_for_tests", None)


def test_any_load_repins_the_canonical_cctally_entry_whatever_its_name():
    """Measured, and it contradicts the design note this primitive was built from.

    `bin/cctally`'s `_load_sibling` executes `sys.modules["cctally"] =
    _THIS_MODULE` on every call, and the script loads a sibling during its own
    execution. So a load under ANY name takes the canonical entry over, and an
    alternate identity is not a way to avoid touching it. Pinned here because a
    future reader will otherwise reach the same wrong conclusion from the same
    source.
    """
    sentinel = object()
    sys.modules["cctally"] = sentinel
    try:
        alternate = load_script_module("_cctally_repin_probe", register=False)
        assert sys.modules["cctally"] is not sentinel, (
            "the script no longer re-pins the canonical entry; the alternate "
            "identity now means something different and its sites need review"
        )
        assert sys.modules["cctally"] is alternate
    finally:
        # In a `finally`, because a failing assertion above would otherwise
        # leave a bare `object()` bound as `sys.modules["cctally"]` for the rest
        # of the xdist worker, and every later load in it would read that.
        load_script_module()


def test_an_unregistered_load_leaves_its_own_name_unbound():
    """`register=False` means "do not LEAVE it bound", not "never bind it".

    An entirely unregistered load cannot work. `_THIS_MODULE` in `bin/cctally`
    resolves to `None` when nothing is bound, so the first `_load_sibling` call
    pins `sys.modules["cctally"] = None` and the first sibling reading through
    that entry raises an `AttributeError` on `NoneType` naming neither the
    script nor the cause. Measured on this tree, that is
    `'NoneType' object has no attribute 'BLOCK_DURATION'` from
    `bin/_cctally_dashboard.py`; the attribute named is whichever sibling reads
    first, so it is not part of the contract.
    """
    try:
        module = load_script_module("_cctally_unregistered_probe", register=False)
        assert "_cctally_unregistered_probe" not in sys.modules
        assert module.__name__ == "_cctally_unregistered_probe"
    finally:
        load_script_module()


def test_an_alternate_identity_load_evicts_no_sibling():
    """Eviction is scoped to a load that CLAIMS the `cctally` identity.

    The three alternate-identity sites load the script for its helpers, not to
    become the CLI, and evicting every cached sibling on their behalf would be a
    behaviour change rather than a preservation of what they do.
    """
    sentinel = object()
    sys.modules["_cctally_another_fake_sibling"] = sentinel
    try:
        load_script_module("_cctally_alternate_probe", register=False)
        assert sys.modules["_cctally_another_fake_sibling"] is sentinel
    finally:
        sys.modules.pop("_cctally_another_fake_sibling", None)
        load_script_module()


@pytest.mark.parametrize("name", ["cctally", "_cctally_for_tests", "cctally_cli"])
def test_every_migrated_identity_still_loads(name):
    """Each name in the list is one a real site asks the primitive for.

    ``cctally`` is the default and the overwhelming majority of calls.
    ``_cctally_for_tests`` is ``tests/test_lib_share.py`` and ``cctally_cli`` is
    ``tests/test_resolve_claude_tz_name.py``; both held that identity before the
    migration and both still pass it.
    """
    module = load_script_module(name, register=False)
    assert callable(getattr(module, "main", None)) or hasattr(module, "PUBLIC_REPO")
