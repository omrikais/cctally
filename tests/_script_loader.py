"""The ONE implementation that loads ``bin/cctally`` as a module (#630 S6, F45).

Sixty-four test modules hand-rolled this, in five spellings. That is not a style
problem. `tests/conftest.py::load_script` performs two things a hand-rolled
`SourceFileLoader` does not, and both are safety rather than convenience:

1. **It drops cached `_cctally_*` siblings**, so that when PEP 562 (or the
   dispatch thunk) next triggers `_load_sibling("_cctally_release")`, the sibling
   re-executes its `import cctally` against the FRESH module rather than the
   stale instance from the previous load. In `tests/conftest.py`'s own words:
   without this clear, "monkeypatches on the new `cctally.CHANGELOG_PATH` don't
   propagate into MOVED helpers, and tests that monkeypatch real-path constants
   leak writes to the on-disk repo". That is preserve-item 3 — tests must never
   write to the real production data directory — reached by another route.
2. **It re-runs `_cctally_core._init_paths_from_env()`**, so a test that does
   `setenv("HOME", tmp)` and then loads sees path constants derived from the new
   HOME. `_cctally_core` is deliberately NOT evicted: it is the kernel, it does
   not `import cctally` (it uses the call-time `_cctally()` accessor), and tests
   monkeypatch `_cctally_core.X` through a stable module-top import that must not
   go stale across loads.

TRAP — PATCH ORDERING. `_init_paths_from_env()` runs on EVERY load and rebinds
every promoted global (`APP_DIR`, `DB_PATH`, `CLAUDE_SETTINGS_PATH`, ...) from
the current `HOME`. It CLOBBERS any `monkeypatch.setattr(_cctally_core, "X", v)`
performed BEFORE the load — silently, by leaking writes to the host, with no
exception and no warning. The correct order is always load first, patch second,
or use `conftest.redirect_paths`.

IDENTITY IS A PARAMETER BECAUSE IT IS BEHAVIOUR, NOT STYLE.
`tests/test_lib_share.py` loads the same file under the name `_cctally_for_tests`
and `tests/test_resolve_claude_tz_name.py` under `cctally_cli`, each holding that
entry as a stable reference across later `load_script()` calls that rebind
`sys.modules["cctally"]`. A primitive offering only the `cctally` identity would
silently change what those modules do.

WHAT AN ALTERNATE IDENTITY DOES **NOT** BUY, measured rather than assumed. It
does not leave `sys.modules["cctally"]` alone. `bin/cctally`'s `_load_sibling`
re-pins `sys.modules["cctally"] = _THIS_MODULE` on every call, and the script
loads a sibling during its own execution, so ANY load of this file — under any
name — takes that entry over. The alternate identity buys the module its own
durable entry, and nothing else.

REGISTRATION IS EFFECTIVELY MANDATORY, which is why `register=False` still
registers for the duration of the exec. MEASURED, by exec'ing `bin/cctally` into
a genuinely unregistered module: `_THIS_MODULE` is
`sys.modules.get(__name__) or sys.modules.get("cctally")`, which is `None` when
nothing is bound, so the first `_load_sibling` call pins
`sys.modules["cctally"] = None` and the first sibling that reads through that
entry raises. On this tree the raise is
`AttributeError: 'NoneType' object has no attribute 'BLOCK_DURATION'` from
`bin/_cctally_dashboard.py`, at the line `BLOCK_DURATION =
sys.modules["cctally"].BLOCK_DURATION`. The failing attribute is whichever
sibling happens to read first, so do not treat that name as the contract; the
contract is that an unregistered load raises an `AttributeError` on `NoneType`
naming neither the script nor the cause. `register=False` therefore means "do
not LEAVE the module bound under its own name", not "never bind it".

Sibling eviction and path re-derivation happen only when the load claims the
`cctally` identity, because that is the only case in which a sibling's
`import cctally` would otherwise resolve to a module from a previous test.

The compiled code object is cached for the process. Under pytest-xdist each
worker is a fresh process, so the compile is paid once per worker.
"""
from __future__ import annotations

import pathlib
import sys
import types

SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"
BIN_DIR = str(SCRIPT_PATH.parent)

# bin/cctally has no `.py` suffix, so `spec_from_file_location` alone returns
# None for it. Compiling the source directly sidesteps the suffix question and
# is what `tests/conftest.py` has always done; measured on the runner, the
# compile is 5.4 ms and a warm exec into a fresh namespace is 0.2 ms.
SCRIPT_CODE = compile(SCRIPT_PATH.read_text(), str(SCRIPT_PATH), "exec")

if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)


# The eviction targets `bin/_cctally_*.py` siblings. `_cctally_core` is the
# kernel and must never be evicted: it does not `import cctally`, and tests
# monkeypatch it through a stable module-top import that would go stale if it
# were replaced.
#
# THIS module is named `_script_loader` rather than `_cctally_loader` so that it
# does not match the pattern at all. The earlier name did match it, and the
# `__name__` exemption below was not enough, because it governs only this
# sweep: twenty modules in the estate run their own `_cctally_*` sweep exempting
# only `_cctally_core`, and every one of them deleted this module's
# `sys.modules` entry. Losing that entry makes the next
# `from _script_loader import ...` re-execute the file, which recompiles
# bin/cctally, discards the process-wide code cache, and mints a second identity
# for the primitive that is supposed to be the only one. The exemption is kept
# as a second line of defence, so that a rename back into the pattern does not
# silently reintroduce the bug in this sweep as well.
_EVICTION_EXEMPT = frozenset({"_cctally_core", __name__})


def _drop_stale_siblings() -> None:
    for name in [
        n for n in sys.modules
        if n.startswith("_cctally_") and n not in _EVICTION_EXEMPT
    ]:
        del sys.modules[name]


def _rederive_paths() -> None:
    core = sys.modules.get("_cctally_core")
    if core is not None and hasattr(core, "_init_paths_from_env"):
        core._init_paths_from_env()


def load_script_module(name: str = "cctally", register: bool = True) -> types.ModuleType:
    """Execute ``bin/cctally`` into a fresh module and return it.

    ``name`` is the module identity. ``register`` says whether the binding
    SURVIVES the call: the module is always registered for the duration of the
    exec, because an entirely unregistered load raises. See the module
    docstring for the measured failure.

    ``register=False`` restores ``sys.modules[name]`` to whatever it held
    before, and NOTHING ELSE. In particular it does not restore
    ``sys.modules["cctally"]``, which the exec re-pins to this module through
    ``_load_sibling`` on every load whatever ``name`` is. So an unregistered
    load under an alternate identity still takes the canonical entry over, and
    a caller that needs the previous ``cctally`` binding back has to reload it.

    The returned object is a real ``types.ModuleType``; its ``__dict__`` is the
    script's global namespace, so ``mod.X`` and ``mod.__dict__["X"]`` are the same
    binding and a sibling that does ``import cctally`` sees a mutation to either.
    """
    claims_cctally = register and name == "cctally"
    if claims_cctally:
        _drop_stale_siblings()
        # BEFORE the exec, so the script's `APP_DIR = _cctally_core.APP_DIR`
        # re-export block snapshots the updated values rather than the previous
        # test's.
        _rederive_paths()
    mod = types.ModuleType(name)
    mod.__file__ = str(SCRIPT_PATH)
    had_previous = name in sys.modules
    previous = sys.modules.get(name)
    sys.modules[name] = mod
    try:
        exec(SCRIPT_CODE, mod.__dict__)
    finally:
        if not register:
            if had_previous:
                sys.modules[name] = previous
            else:
                sys.modules.pop(name, None)
    return mod
