"""Shared pytest helpers for cctally tests.

Loads the main script (which has no .py extension and is not a
package) into a throwaway namespace so tests can exercise its
internals without running the CLI.
"""
import pathlib
import sys
import types

import pytest


def _script_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"


# Compile bin/cctally once per pytest session and reuse the code object
# across every load_script() call. Profiling: compile() is ~146ms,
# exec() ~16ms; the 26K-line script is reparsed 335+ times in a full
# pytest run, so caching the code object cuts ~50s off suite wallclock.
# Each test still gets a fresh namespace via exec(), preserving isolation.
# Under pytest-xdist (`pytest -n <N>`) each worker is a fresh Python
# process, so this cache is per-worker — the compile cost is paid N
# times instead of once. That's a small constant-cost regression
# (~146ms × N) which is well within the parallel speedup; see
# tests/requirements-dev.txt for the optional xdist dep.
_SCRIPT_PATH = _script_path()
_SCRIPT_CODE = compile(_SCRIPT_PATH.read_text(), str(_SCRIPT_PATH), "exec")

# Ensure bin/ is on sys.path so tests can do `import _cctally_core` at the
# top of the file. After 2026-05-22 (issue #84) the 23 in-scope path
# globals live in _cctally_core; tests monkeypatch them via
# ``monkeypatch.setattr(_cctally_core, "X", v)``. The module-top import
# stays stable across ``load_script()`` reloads because the load_script
# preserves ``_cctally_core`` in sys.modules (see note in load_script).
_BIN_DIR = str(_SCRIPT_PATH.parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)


def load_script():
    """Execute the main script and return its globals dict.

    The dict IS the namespace of a real types.ModuleType registered in
    sys.modules['cctally']. Two facts make this work without behaviour
    change for tests:
      1. exec(code, mod.__dict__) populates the module's namespace from
         the script's globals, and `mod.__dict__ is ns` afterwards.
      2. Attribute lookup on a module reads its __dict__; mutating the
         dict (monkeypatch.setitem(ns, "X", v)) is immediately visible
         as mod.X for siblings that import cctally.

    Net: tests keep their `ns["X"]` / `monkeypatch.setitem(ns, "X", v)`
    patterns AND `import cctally; cctally.X` from sibling lazy modules
    sees the same value. Per-test isolation: each call rebuilds a fresh
    module and re-binds sys.modules['cctally'] (latest call wins).

    Drops cached `_cctally_*.py` sibling modules from sys.modules so
    that when PEP 562 (or the dispatch thunk) next triggers
    `_load_sibling("_cctally_release")`, the sibling re-executes its
    `import cctally` against the FRESH cctally module — not the stale
    instance from the previous test's load_script(). Without this clear,
    `_cctally_release.cctally` remains pinned to the prior module, so
    monkeypatches on the new `cctally.CHANGELOG_PATH` don't propagate
    into MOVED helpers, and tests that monkeypatch real-path constants
    leak writes to the on-disk repo. Spec §5.5 (circular-import safety)
    + §6.0a.

    EXCEPTION: ``_cctally_core`` is the kernel and does NOT
    ``import cctally`` (it uses the call-time ``_cctally()`` accessor),
    so its module-load state is safe across reloads. After 2026-05-22
    (issue #84) the 23 in-scope path globals live in
    ``_cctally_core``; keeping the same instance in sys.modules lets
    tests monkeypatch ``_cctally_core.X`` via a stable module-top
    ``import _cctally_core`` reference without it going stale on the
    next ``load_script()`` call. To preserve the pre-#84 behavior where
    each ``load_script()`` re-derived path constants from the current
    HOME env var, we explicitly call
    ``_cctally_core._init_paths_from_env()`` here. That re-runs the
    same Path.home() / "..." logic against the current env without
    needing a fresh import, so tests doing ``setenv("HOME", tmp) +
    load_script()`` see fresh, HOME-derived path constants — same
    contract as before #84.

    TRAP — patch ordering matters. ``_init_paths_from_env()`` runs at
    the top of EVERY ``load_script()`` call and rebinds every promoted
    global (``APP_DIR``, ``DB_PATH``, ``CLAUDE_SETTINGS_PATH``, etc.)
    from the current ``HOME`` env var. This will CLOBBER any prior
    ``monkeypatch.setattr(_cctally_core, "X", v)`` that ran BEFORE
    ``load_script()``. The correct ordering is ALWAYS:

        ns = load_script()                                        # FIRST
        monkeypatch.setattr(_cctally_core, "X", tmp)              # THEN

    or use the ``redirect_paths(ns, monkeypatch, tmp_path)`` helper
    below which handles ordering correctly. Reversing the order
    silently leaks the patched paths to the host machine — no
    exception, no warning, just stale values from the unpatched
    ``_init_paths_from_env()`` reset.

    Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md §6.0a
    """
    for _name in [n for n in sys.modules if n.startswith("_cctally_") and n != "_cctally_core"]:
        del sys.modules[_name]
    # Re-derive _cctally_core's path constants from the current HOME env
    # var. Tests doing `setenv("HOME", tmp) + load_script()` rely on
    # this to surface a fresh path set under the test's HOME without
    # re-importing _cctally_core. Must run BEFORE the bin/cctally exec
    # below so the script's `APP_DIR = _cctally_core.APP_DIR` re-export
    # block snapshots the updated values.
    core = sys.modules.get("_cctally_core")
    if core is not None and hasattr(core, "_init_paths_from_env"):
        core._init_paths_from_env()
    mod = types.ModuleType("cctally")
    mod.__file__ = str(_SCRIPT_PATH)
    sys.modules["cctally"] = mod
    exec(_SCRIPT_CODE, mod.__dict__)
    return mod.__dict__


def redirect_paths(ns, monkeypatch, tmp_path):
    """Pin the kernel's path constants to a tmp dir.

    After 2026-05-22 (issue #84), the 23 in-scope path constants live
    in bin/_cctally_core.py and `_cctally_core` is the single legal
    monkeypatch target. Every reader (every sibling AND bin/cctally
    itself) goes through `_cctally_core.X` at call time.

    The `ns[X]` MIRROR below is NOT a second patch surface — it just
    keeps `bin/cctally`'s eager re-exports (`cctally.APP_DIR` etc.) in
    sync with the kernel patches, so tests that *read* `ns["X"]` to
    introspect values (e.g. `ns["CONFIG_PATH"].read_text()`) see the
    fixture-redirected paths instead of stale module-load snapshots.
    Tests must STILL patch via `monkeypatch.setattr(_cctally_core,
    "X", v)` (the AST guard at `test_kernel_extraction_invariants.py`
    enforces this for `test_*.py` files; conftest itself is exempt).

    Note: ``CHANGELOG_PATH`` is intentionally NOT redirected. It
    resolves to ``<repo>/CHANGELOG.md`` based on the binary's own
    filesystem location, not HOME — there is no fixture analogue
    inside ``tmp_path`` to point it at, and existing tests that need
    to override it (e.g. tests/test_release_internals.py) do so with
    their own `monkeypatch.setattr(_cctally_core, "CHANGELOG_PATH",
    …)` in the per-test fixture.

    As of the data-globals promotion (2026-05-22, #84), `_cctally_db`
    reads its four path constants
    (``DB_PATH``/``CACHE_DB_PATH``/``LOG_DIR``/``MIGRATION_ERROR_LOG_PATH``)
    via ``_cctally_core.X`` at call time, so the kernel patches above
    propagate directly without a sibling-side re-patch block — the
    previous seed-and-re-patch pair was a vestige of the pre-#84
    bare-name pattern and has been removed.
    """
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    paths = {
        "APP_DIR": share,
        "LEGACY_APP_DIR": tmp_path / ".local" / "share" / "ccusage-subscription",
        "LOG_DIR": share / "logs",
        "DB_PATH": share / "stats.db",
        "CACHE_DB_PATH": share / "cache.db",
        "CACHE_LOCK_PATH": share / "cache.db.lock",
        "CACHE_LOCK_CODEX_PATH": share / "cache.db.codex.lock",
        "CONFIG_LOCK_PATH": share / "config.json.lock",
        "CONFIG_PATH": share / "config.json",
        "MIGRATION_ERROR_LOG_PATH": share / "logs" / "migration-errors.log",
        "HOOK_TICK_LOG_DIR": share / "logs",
        "HOOK_TICK_LOG_PATH": share / "logs" / "hook-tick.log",
        "HOOK_TICK_LOG_ROTATED_PATH": share / "logs" / "hook-tick.log.1",
        "HOOK_TICK_THROTTLE_PATH": share / "hook-tick.last-fetch",
        "HOOK_TICK_THROTTLE_LOCK_PATH": share / "hook-tick.last-fetch.lock",
        "UPDATE_STATE_PATH": share / "update-state.json",
        "UPDATE_SUPPRESS_PATH": share / "update-suppress.json",
        "UPDATE_LOCK_PATH": share / "update.lock",
        "UPDATE_LOG_PATH": share / "update.log",
        "UPDATE_LOG_ROTATED_PATH": share / "update.log.1",
        "UPDATE_CHECK_LAST_FETCH_PATH": share / "update-check.last-fetch",
        "CLAUDE_SETTINGS_PATH": tmp_path / ".claude" / "settings.json",
    }

    core = sys.modules["_cctally_core"]
    for name, value in paths.items():
        monkeypatch.setattr(core, name, value)
        # Mirror the patch into bin/cctally's namespace so tests that
        # read `ns["X"]` for introspection see the fixture-redirected
        # paths. NOT a second patch target — only the `_cctally_core`
        # patch above propagates to actual readers. This mirror is for
        # test introspection only; per-test `setitem(ns, "<PROMOTED>",
        # …)` from a test_*.py file is still forbidden (AST guard at
        # `tests/test_kernel_extraction_invariants.py`).
        monkeypatch.setitem(ns, name, value)

    # Note: `_cctally_db` used to require sibling-side re-patching of
    # DB_PATH / CACHE_DB_PATH / LOG_DIR / MIGRATION_ERROR_LOG_PATH
    # because it consumed them via bare-name reads against a seeded
    # `_cctally_db.__dict__`. As of the data-globals promotion
    # (2026-05-22, #84) it reads via `_cctally_core.X` at call time, so
    # the kernel patches above propagate directly — no extra block here.

    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def cctally_module():
    """Expose bin/cctally as an attribute-accessible namespace.

    Wraps load_script()'s dict in a SimpleNamespace so unit tests can
    write `cctally_module.add_column_if_missing(...)` instead of dict
    indexing. Reuses the cached compiled code object so this is cheap.
    """
    return types.SimpleNamespace(**load_script())
