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

    Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md §6.0a
    """
    for _name in [n for n in sys.modules if n.startswith("_cctally_")]:
        del sys.modules[_name]
    mod = types.ModuleType("cctally")
    mod.__file__ = str(_SCRIPT_PATH)
    sys.modules["cctally"] = mod
    exec(_SCRIPT_CODE, mod.__dict__)
    return mod.__dict__


def redirect_paths(ns, monkeypatch, tmp_path):
    """Pin the script's module-level path constants to a tmp dir.

    APP_DIR/DB_PATH/CACHE_DB_PATH are bound at module-load time, so
    setenv("HOME") alone wouldn't redirect them — we monkeypatch the
    namespace dict entries directly. Also creates an empty
    ~/.claude/projects so sync_cache walks find no JSONL files.

    Post Phase C #16 split: the migration framework's error sentinel
    + cmd_db_* helpers live in `bin/_cctally_db.py` and resolve
    ``DB_PATH`` / ``CACHE_DB_PATH`` / ``LOG_DIR`` /
    ``MIGRATION_ERROR_LOG_PATH`` via bare-name lookup in their own
    module's __dict__. We also patch those so any migration code
    triggered during a redirected test (e.g. `cmd_db_status` against
    fixture HOME) reads the redirected paths, not the production
    ones seeded at bin/cctally module-load time.
    """
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setitem(ns, "APP_DIR", share)
    monkeypatch.setitem(ns, "DB_PATH", share / "stats.db")
    monkeypatch.setitem(ns, "CACHE_DB_PATH", share / "cache.db")
    monkeypatch.setitem(ns, "CACHE_LOCK_PATH", share / "cache.db.lock")
    monkeypatch.setitem(ns, "CACHE_LOCK_CODEX_PATH", share / "cache.db.codex.lock")
    monkeypatch.setitem(ns, "CONFIG_PATH", share / "config.json")
    monkeypatch.setitem(ns, "CONFIG_LOCK_PATH", share / "config.json.lock")
    monkeypatch.setitem(ns, "LOG_DIR", share / "logs")
    monkeypatch.setitem(ns, "MIGRATION_ERROR_LOG_PATH", share / "logs" / "migration-errors.log")
    # _cctally_db sibling's own copies — populated by the seed block
    # near the bin/cctally path-constant region. Update them here so
    # bare-name lookups inside the migration framework + cmd_db_*
    # helpers see the fixture-redirected values.
    db_sibling = ns.get("_cctally_db")
    if db_sibling is not None:
        monkeypatch.setattr(db_sibling, "DB_PATH", share / "stats.db")
        monkeypatch.setattr(db_sibling, "CACHE_DB_PATH", share / "cache.db")
        monkeypatch.setattr(db_sibling, "LOG_DIR", share / "logs")
        monkeypatch.setattr(db_sibling, "MIGRATION_ERROR_LOG_PATH", share / "logs" / "migration-errors.log")
    # Post Phase D #17 split: the session-entry cache subsystem lives
    # in `bin/_cctally_cache.py` and routes `APP_DIR` / `CACHE_DB_PATH`
    # / `CACHE_LOCK_PATH` / `CACHE_LOCK_CODEX_PATH` /
    # `CODEX_SESSIONS_DIR` through the `c = _cctally()` call-time
    # accessor (spec §5.5). The `monkeypatch.setitem(ns, ...)` calls
    # above propagate transparently — no sibling-side patches needed.
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def cctally_module():
    """Expose bin/cctally as an attribute-accessible namespace.

    Wraps load_script()'s dict in a SimpleNamespace so unit tests can
    write `cctally_module.add_column_if_missing(...)` instead of dict
    indexing. Reuses the cached compiled code object so this is cheap.
    """
    return types.SimpleNamespace(**load_script())
