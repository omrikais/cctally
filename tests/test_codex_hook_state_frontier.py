"""Codex hook-state trust must reach both ingest frontiers (#719 §2.6, §2.6a).

`bin/cctally-tui-test` is a `--render-once` snapshot harness and never
exercises the TUI's ingest frontier, so these contracts live here rather than
there.  They also deliberately avoid `tests/test_tick_stats_integration.py`,
which another branch is editing.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import load_script, redirect_paths


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

CANONICAL_COMMAND_TAIL = "hook-tick --foreground --source codex"


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return ns, codex_home


def _install_hooks(home: pathlib.Path, binary: str = "/opt/cctally/bin/cctally"):
    handler = {
        "type": "command",
        "command": f"{binary} {CANONICAL_COMMAND_TAIL}",
        "timeout": 30,
    }
    (home / "hooks.json").write_text(json.dumps({
        "hooks": {
            event: [{"hooks": [dict(handler)]}]
            for event in ("Stop", "SubagentStop")
        }
    }))


def _write_trusted_config(home: pathlib.Path):
    hooks_path = home / "hooks.json"
    (home / "config.toml").write_text(
        f'[hooks.state."{hooks_path}:stop:0:0"]\n'
        'trusted_hash = "aaa"\n'
        f'[hooks.state."{hooks_path}:subagent_stop:0:0"]\n'
        'trusted_hash = "bbb"\n',
        encoding="utf-8",
    )
    import os

    stamp = os.stat(home / "config.toml").st_mtime
    os.utime(hooks_path, (stamp - 60, stamp - 60))


def _cache_store(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE session_files (path TEXT);"
        "CREATE TABLE codex_session_files (path TEXT);"
        "INSERT INTO cache_meta VALUES ('claude_ingest_walk_complete','1');"
        "INSERT INTO cache_meta VALUES "
        "('dashboard_codex_full_walk_complete','1');"
    )
    conn.commit()
    return conn


def _conversation_store(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE conversation_source_files "
        "(path TEXT, size_bytes INTEGER, mtime_ns INTEGER, "
        "last_byte_offset INTEGER);"
        "CREATE TABLE codex_conversation_source_files "
        "(path TEXT, size_bytes INTEGER, mtime_ns INTEGER, "
        "last_byte_offset INTEGER);"
    )
    conn.commit()
    return conn


def test_codex_frontier_guard_paths_carry_every_root_config_toml(runtime):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    roots = hooks.codex_hook_roots([home])
    assert roots
    guards = hooks.codex_frontier_guard_paths(roots)
    assert home / "hooks.json" in guards
    assert home / "config.toml" in guards


def test_every_frontier_guard_site_uses_the_shared_helper():
    """A predicate fixed in one renderer diverges in the other two."""
    for relative in (
        "bin/_cctally_tui.py",
        "bin/_cctally_dashboard.py",
        "bin/cctally-bench",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "codex_frontier_guard_paths" in source, relative


@pytest.mark.parametrize("frontier_class,store_factory", [
    ("DashboardIngestFrontier", _cache_store),
    ("ConversationSyncFrontier", _conversation_store),
])
def test_a_seeded_frontier_falls_back_when_only_config_toml_changes(
    runtime, tmp_path, frontier_class, store_factory,
):
    ns, home = runtime
    import _lib_codex_hooks as hooks
    import _lib_ingest_frontier as frontier_mod

    _install_hooks(home)
    _write_trusted_config(home)
    roots = hooks.codex_hook_roots([home])
    guards = hooks.codex_frontier_guard_paths(roots)
    sessions = home / "sessions"
    sessions.mkdir(exist_ok=True)

    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    conn = store_factory(tmp_path / f"{frontier_class}.db")
    try:
        frontier = getattr(frontier_mod, frontier_class)(app_dir)
        assert frontier.seed_provider(
            "codex", conn, roots=(sessions,), guard_paths=guards, trusted=True,
        ), frontier.last_seed_failure
        assert frontier.plan_provider(
            "codex", conn, roots=(sessions,), guard_paths=guards,
        ).mode in {"caught_up", "targeted"}

        (home / "config.toml").write_text("# operator disabled the hook\n")

        plan = frontier.plan_provider(
            "codex", conn, roots=(sessions,), guard_paths=guards,
        )
        assert plan.mode == "full"
        assert plan.reason == "hook_config_changed"
    finally:
        conn.close()


def test_all_roots_enabled_is_false_for_every_non_enabled_state(runtime):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    roots = hooks.codex_hook_roots([home])

    # No handler at all.
    assert hooks.codex_hook_roots_all_enabled(roots) is False

    # Installed but never trusted.
    _install_hooks(home)
    assert hooks.codex_hook_roots_all_enabled(roots) is False

    # Trusted and enabled.
    _write_trusted_config(home)
    assert hooks.codex_hook_roots_all_enabled(roots) is True

    # Explicitly disabled by the operator in Codex `/hooks`.
    hooks_path = home / "hooks.json"
    (home / "config.toml").write_text(
        f'[hooks.state."{hooks_path}:stop:0:0"]\n'
        'trusted_hash = "aaa"\n'
        'enabled = false\n'
        f'[hooks.state."{hooks_path}:subagent_stop:0:0"]\n'
        'trusted_hash = "bbb"\n',
        encoding="utf-8",
    )
    assert hooks.codex_hook_roots_all_enabled(roots) is False

    # An unreadable table never reads as trusted.
    (home / "config.toml").write_text("= = =\n")
    assert hooks.codex_hook_roots_all_enabled(roots) is False

    # No root at all is not a certificate either.
    assert hooks.codex_hook_roots_all_enabled([]) is False


def test_the_dashboard_conversation_frontier_is_fail_closed_on_a_disabled_root(
    runtime,
):
    ns, home = runtime
    import _cctally_dashboard

    _install_hooks(home)
    _write_trusted_config(home)
    _frontier_mod, context = _cctally_dashboard._conversation_frontier_context()
    _roots, guards, trusted = context["codex"]
    assert trusted is True
    assert home / "config.toml" in guards

    hooks_path = home / "hooks.json"
    (home / "config.toml").write_text(
        f'[hooks.state."{hooks_path}:stop:0:0"]\n'
        'trusted_hash = "aaa"\n'
        'enabled = false\n',
        encoding="utf-8",
    )
    _frontier_mod, context = _cctally_dashboard._conversation_frontier_context()
    assert context["codex"][2] is False


def _write_disabled_config(home: pathlib.Path):
    hooks_path = home / "hooks.json"
    (home / "config.toml").write_text(
        f'[hooks.state."{hooks_path}:stop:0:0"]\n'
        'trusted_hash = "aaa"\n'
        'enabled = false\n'
        f'[hooks.state."{hooks_path}:subagent_stop:0:0"]\n'
        'trusted_hash = "bbb"\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_the_tui_sync_hands_the_codex_frontier_the_kernels_verdict(
    runtime, monkeypatch, enabled,
):
    """The TUI's own `_codex_hooks_trusted()` closure, exercised end to end.

    A source-grep guard passes on a call site that reads the shared kernel and
    then uses its answer wrongly, so this drives the real dashboard sync
    closure and reads the `trusted` argument the frontier actually receives.
    """
    ns, home = runtime
    (home / "sessions").mkdir(exist_ok=True)
    _install_hooks(home)
    if enabled:
        _write_trusted_config(home)
    else:
        _write_disabled_config(home)

    frontier_mod = ns["_load_sibling"]("_lib_ingest_frontier")
    original_seed = frontier_mod.DashboardIngestFrontier.seed_provider
    seen: dict[str, object] = {}

    def spy(self, provider, conn, *, roots, guard_paths=(), trusted=True,
            cutoff=None):
        seen[provider] = trusted
        return original_seed(
            self, provider, conn, roots=roots, guard_paths=guard_paths,
            trusted=trusted, cutoff=cutoff,
        )

    monkeypatch.setattr(
        frontier_mod.DashboardIngestFrontier, "seed_provider", spy)

    stats = SimpleNamespace(full_walk_complete=True)
    monkeypatch.setitem(ns, "sync_cache", lambda *a, **k: stats)
    monkeypatch.setitem(ns, "sync_codex_cache", lambda *a, **k: stats)
    monkeypatch.setitem(
        ns, "_tui_build_snapshot",
        lambda **kwargs: ns["_empty_dashboard_snapshot"](),
    )

    class Hub:
        def __init__(self):
            self.published: list = []

        def publish(self, snapshot):
            self.published.append(snapshot)

    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=Hub(),
        pinned_now=dt.datetime(2026, 9, 4, tzinfo=dt.timezone.utc),
        display_tz_pref_override="utc",
    )
    locked(skip_sync=False)

    assert seen.get("codex") is enabled
