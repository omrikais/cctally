"""Shared pytest helpers for cctally tests.

Loads the main script (which has no .py extension and is not a
package) into a throwaway namespace so tests can exercise its
internals without running the CLI.
"""
import pathlib
import types

import pytest


def _script_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"


# Compile bin/cctally once per pytest session and reuse the code object
# across every load_script() call. Profiling: compile() is ~146ms,
# exec() ~16ms; the 26K-line script is reparsed 335+ times in a full
# pytest run, so caching the code object cuts ~50s off suite wallclock.
# Each test still gets a fresh namespace via exec(), preserving isolation.
_SCRIPT_PATH = _script_path()
_SCRIPT_CODE = compile(_SCRIPT_PATH.read_text(), str(_SCRIPT_PATH), "exec")


def load_script():
    """Execute the main script in a fresh namespace and return it."""
    ns = {"__file__": str(_SCRIPT_PATH)}
    exec(_SCRIPT_CODE, ns)
    return ns


def redirect_paths(ns, monkeypatch, tmp_path):
    """Pin the script's module-level path constants to a tmp dir.

    APP_DIR/DB_PATH/CACHE_DB_PATH are bound at module-load time, so
    setenv("HOME") alone wouldn't redirect them — we monkeypatch the
    namespace dict entries directly. Also creates an empty
    ~/.claude/projects so sync_cache walks find no JSONL files.
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
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def cctally_module():
    """Expose bin/cctally as an attribute-accessible namespace.

    Wraps load_script()'s dict in a SimpleNamespace so unit tests can
    write `cctally_module.add_column_if_missing(...)` instead of dict
    indexing. Reuses the cached compiled code object so this is cheap.
    """
    return types.SimpleNamespace(**load_script())
