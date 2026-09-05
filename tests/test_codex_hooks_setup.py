"""Additive, fake-home-only Codex hooks.json management contracts for #294 S2."""
from __future__ import annotations
import types

import argparse
import errno
import fcntl
import json
import os
import shutil
import stat
import sys
import threading
from types import SimpleNamespace

import pytest

from conftest import load_script, redirect_paths

from tests._support_http import PRESENCE_BACKSTOP_SECONDS


def _owned_command(binary: str = "/opt/cctally/bin/cctally") -> str:
    return f"{binary} hook-tick --foreground --source codex"


def _handlers(document: dict, event: str) -> list[dict]:
    found: list[dict] = []
    for group in document.get("hooks", {}).get(event, []):
        if isinstance(group, dict):
            found.extend(h for h in group.get("hooks", []) if isinstance(h, dict))
    return found


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    codex_home = tmp_path / "codex home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return ns, codex_home


def test_codex_hook_plan_is_additive_exact_and_idempotent(runtime):
    ns, _home = runtime
    binary = "/opt/Codex Tools/cctally"
    original = {
        "unrelated": {"keep": True},
        "hooks": {
            "Stop": [{
                "matcher": "user-rule",
                "hooks": [{"type": "command", "command": "/usr/local/bin/user-stop", "timeout": 7}],
            }],
        },
    }

    installed, added = ns["_codex_hooks_plan_install"](original, binary)
    expected = "'/opt/Codex Tools/cctally' hook-tick --foreground --source codex"
    assert added == {
        "Stop": {"added": 1, "removed": 0, "unchanged": 0},
        "SubagentStop": {"added": 1, "removed": 0, "unchanged": 0},
    }
    assert installed["unrelated"] == original["unrelated"]
    for event in ("Stop", "SubagentStop"):
        matches = [h for h in _handlers(installed, event) if h.get("command") == expected]
        assert matches == [{"type": "command", "command": expected, "timeout": 30}]

    rerun, added_again = ns["_codex_hooks_plan_install"](installed, binary)
    assert rerun == installed
    assert added_again == {
        "Stop": {"added": 0, "removed": 0, "unchanged": 1},
        "SubagentStop": {"added": 0, "removed": 0, "unchanged": 1},
    }


def test_codex_hook_plan_rejects_malformed_input_and_removes_owned_only(runtime):
    ns, _home = runtime
    command = _owned_command()
    malformed = {"hooks": {"Stop": "not-a-list"}}
    with pytest.raises(ns["CodexHooksError"], match="hooks.Stop"):
        ns["_codex_hooks_plan_install"](malformed, "/opt/cctally/bin/cctally")
    assert malformed == {"hooks": {"Stop": "not-a-list"}}

    document = {
        "hooks": {
            "Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": command, "timeout": 30},
                {"type": "command", "command": command + " --extra", "timeout": 30},
            ]}],
            "SubagentStop": [{"matcher": "", "hooks": [
                {"type": "command", "command": command, "timeout": 30},
            ]}],
        },
        "other": [1, 2, 3],
    }
    after, removed = ns["_codex_hooks_plan_uninstall"](document, "/opt/cctally/bin/cctally")
    assert removed == {
        "Stop": {"added": 0, "removed": 1, "unchanged": 0},
        "SubagentStop": {"added": 0, "removed": 1, "unchanged": 0},
    }
    assert _handlers(after, "Stop") == [
        {"type": "command", "command": command + " --extra", "timeout": 30}
    ]
    assert "SubagentStop" not in after["hooks"]
    assert after["other"] == [1, 2, 3]


@pytest.mark.parametrize("owned_duplicates", [
    [
        {"type": "command", "timeout": 99},
        {"type": "prompt", "timeout": 30},
    ],
    [
        {"type": "command", "timeout": 30},
    ],
])
def test_status_rejects_mixed_owned_handlers_and_install_uninstall_reconcile_all(
    runtime, capsys, owned_duplicates,
):
    ns, home = runtime
    binary = str(ns["_setup_resolve_hook_target"](ns["_setup_resolve_repo_root"]()))
    command = _owned_command(binary)
    user = {"type": "command", "command": "/usr/bin/user-stop", "timeout": 7}
    canonical = {"type": "command", "command": command, "timeout": 30}
    duplicates = [
        {**handler, "command": command}
        for handler in owned_duplicates
    ]
    document = {
        "hooks": {
            event: [{"hooks": [
                canonical,
                user,
                *duplicates,
            ]}]
            for event in ("Stop", "SubagentStop")
        }
    }

    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps(document))
    assert ns["_setup_status"](argparse.Namespace(json=True)) == 0
    before = json.loads(capsys.readouterr().out)["codex_hooks"]["roots"][0]
    assert before["state"] == "absent"
    expected_owned = 1 + sum(
        1 for handler in duplicates if handler["type"] == "command")
    foreign = [handler for handler in duplicates if handler["type"] != "command"]
    assert before["stop_count"] == before["subagent_stop_count"] == expected_owned

    installed_summary = ns["_setup_manage_codex_hooks"]("install", binary)
    installed = json.loads(hooks_path.read_text())
    assert installed_summary["roots"][0]["state"] == "installed_untrusted"
    for event in ("Stop", "SubagentStop"):
        assert _handlers(installed, event).count(canonical) == 1
        assert _handlers(installed, event).count(user) == 1
        assert len([
            h for h in _handlers(installed, event)
            if h.get("command") == command and h.get("type") == "command"
        ]) == 1

    hooks_path.write_text(json.dumps(document))
    removed_summary = ns["_setup_manage_codex_hooks"]("uninstall", binary)
    uninstalled = json.loads(hooks_path.read_text())
    assert removed_summary["roots"][0]["changes"] == {
        "Stop": {"added": 0, "removed": expected_owned, "unchanged": 0},
        "SubagentStop": {"added": 0, "removed": expected_owned, "unchanged": 0},
    }
    for event in ("Stop", "SubagentStop"):
        assert _handlers(uninstalled, event) == [user, *foreign]


def test_npm_shim_reinstall_reconciles_duplicate_owned_handlers(runtime):
    ns, home = runtime
    binary = "/opt/homebrew/lib/node_modules/cctally/bin/cctally-npm-shim.js"
    command = _owned_command(binary)
    user = {"type": "command", "command": "/usr/bin/user-stop", "timeout": 7}
    duplicate = {"type": "command", "command": command, "timeout": 30}
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps({
        "hooks": {
            event: [
                {"hooks": [user]},
                {"hooks": [duplicate]},
                {"hooks": [duplicate]},
            ]
            for event in ("Stop", "SubagentStop")
        }
    }))

    first = ns["_setup_manage_codex_hooks"]("install", binary)
    first_document = json.loads(hooks_path.read_text())
    assert first["roots"][0]["changes"] == {
        "Stop": {"added": 0, "removed": 1, "unchanged": 1},
        "SubagentStop": {"added": 0, "removed": 1, "unchanged": 1},
    }
    for event in ("Stop", "SubagentStop"):
        assert _handlers(first_document, event) == [user, duplicate]

    first_bytes = hooks_path.read_bytes()
    second = ns["_setup_manage_codex_hooks"]("install", binary)
    assert second["roots"][0]["changes"] == {
        "Stop": {"added": 0, "removed": 0, "unchanged": 1},
        "SubagentStop": {"added": 0, "removed": 0, "unchanged": 1},
    }
    assert hooks_path.read_bytes() == first_bytes


def test_codex_hook_write_uses_backup_atomic_permissions_and_status_json(runtime, capsys):
    ns, home = runtime
    binary = str(ns["_setup_resolve_hook_target"](ns["_setup_resolve_repo_root"]()))
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps({"kept": "yes"}))
    hooks_path.chmod(0o644)
    installed, _added = ns["_codex_hooks_plan_install"](
        {"kept": "yes"}, binary,
    )

    backup = ns["_write_codex_hooks_atomic"](hooks_path, installed)
    assert backup is not None and backup.exists()
    assert json.loads(backup.read_text()) == {"kept": "yes"}
    assert stat.S_IMODE(hooks_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700

    assert ns["_setup_status"](argparse.Namespace(json=True)) == 0
    status = json.loads(capsys.readouterr().out)
    row = status["codex_hooks"]["roots"][0]
    assert row["hooks_path"] == str(hooks_path)
    assert row["state"] == "installed_untrusted"
    assert row["requires_review"] is True
    assert row["stop_count"] == row["subagent_stop_count"] == 1


def test_codex_hook_write_waits_for_the_lock_before_creating_a_backup(
    runtime, monkeypatch,
):
    """A competing writer cannot snapshot stale contents before our lock."""
    ns, home = runtime
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps({"kept": "before"}))
    lock_path = hooks_path.with_name(hooks_path.name + ".cctally.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    import _lib_codex_hooks as hooks

    backup_started = threading.Event()
    original_copy = hooks.shutil.copy2

    def observe_copy(*args, **kwargs):
        backup_started.set()
        return original_copy(*args, **kwargs)

    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_shutil = types.SimpleNamespace(**vars(hooks.shutil))
    _iso_shutil.copy2 = observe_copy
    monkeypatch.setattr(hooks, "shutil", _iso_shutil)
    done = threading.Event()

    def write() -> None:
        ns["_write_codex_hooks_atomic"](hooks_path, {"kept": "after"})
        done.set()

    worker = threading.Thread(target=write)
    worker.start()
    try:
        assert not backup_started.wait(0.2)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # timing-budget: the hook writer has returned now that the flock is released, so `backup_started` is final
    worker.join(timeout=PRESENCE_BACKSTOP_SECONDS)
    assert done.is_set()
    assert backup_started.is_set()


def test_codex_hook_install_rereads_and_plans_after_acquiring_root_lock(
    runtime, monkeypatch,
):
    ns, home = runtime
    binary = "/opt/cctally/bin/cctally"
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps({"keep": "initial"}))
    lock_path = hooks_path.with_name(hooks_path.name + ".cctally.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    setup = sys.modules["_cctally_setup"]
    reached_lock = threading.Event()
    original_acquire = setup.acquire_codex_hooks_write_locks

    def observe_acquire(*args, **kwargs):
        # The authoritative read + plan now happen behind this lock, so the
        # lock acquisition — not the writer — is where an install must block.
        reached_lock.set()
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(setup, "acquire_codex_hooks_write_locks", observe_acquire)
    done = threading.Event()

    def install() -> None:
        ns["_setup_manage_codex_hooks"]("install", binary)
        done.set()

    worker = threading.Thread(target=install)
    worker.start()
    try:
        assert reached_lock.wait(PRESENCE_BACKSTOP_SECONDS)
        hooks_path.write_text(json.dumps({"keep": "intervening-user-edit"}))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # timing-budget: the hook writer has returned now that the flock is released, so the hooks file is final
    worker.join(timeout=PRESENCE_BACKSTOP_SECONDS)
    assert done.is_set()
    final = json.loads(hooks_path.read_text())
    assert final["keep"] == "intervening-user-edit"
    assert len(_handlers(final, "Stop")) == len(_handlers(final, "SubagentStop")) == 1


def test_explicit_invalid_codex_home_has_no_default_fallback(runtime, monkeypatch):
    ns, home = runtime
    monkeypatch.setenv("CODEX_HOME", "~cctally-nonexistent-user-294")
    assert ns["_setup_codex_hook_roots"]() == []
    assert not (home / "hooks.json").exists()


def test_dry_run_and_feature_off_never_write_a_codex_hooks_file(
    runtime, monkeypatch, capsys,
):
    ns, home = runtime
    args = argparse.Namespace(
        status=False, dry_run=True, uninstall=False, purge=False, yes=True,
        json=True, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )
    assert ns["cmd_setup"](args) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["codex_hooks"]["roots"][0]["state"] == "absent"
    assert not (home / "hooks.json").exists()

    monkeypatch.setenv("CCTALLY_DISABLE_CODEX_HOOKS", "1")
    assert ns["_setup_status"](argparse.Namespace(json=True)) == 0
    feature_off = json.loads(capsys.readouterr().out)
    row = feature_off["codex_hooks"]["roots"][0]
    assert row["state"] == "feature_disabled"
    assert row["feature_enabled"] is False
    assert row["requires_review"] is False
    assert not (home / "hooks.json").exists()


def test_codex_only_dry_run_omits_claude_actions_in_text_and_json(
    runtime, capsys,
):
    ns, home = runtime
    claude_dir = home.parent / ".claude"
    shutil.rmtree(claude_dir)
    args = argparse.Namespace(
        status=False, dry_run=True, uninstall=False, purge=False, yes=True,
        json=False, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )

    assert ns["cmd_setup"](args) == 0
    text_output = capsys.readouterr().out
    assert "Claude Code home not present — would skip Claude hooks" in text_output
    assert "Would add 3 hook entries" not in text_output
    assert "hooks.PostToolBatch" not in text_output
    assert "Would reconcile Codex hooks at" in text_output
    assert not claude_dir.exists()
    assert not (home / "hooks.json").exists()
    assert not (home.parent / ".local" / "bin" / "cctally").exists()

    args.json = True
    assert ns["cmd_setup"](args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hooks"]["would_add"] == []
    assert payload["codex_hooks"]["roots"][0]["state"] == "absent"
    assert not claude_dir.exists()
    assert not (home / "hooks.json").exists()
    assert not (home.parent / ".local" / "bin" / "cctally").exists()

def test_setup_uninstall_removes_owned_codex_handlers_when_feature_disabled(
    runtime, monkeypatch, capsys,
):
    """The feature gate suppresses installation, never owned-handler cleanup."""
    ns, home = runtime
    command = _owned_command()
    original = {
        "keep": {"user": "setting"},
        "hooks": {
            "Stop": [{"hooks": [
                {"type": "command", "command": command, "timeout": 30},
                {"type": "command", "command": "/usr/bin/user-stop", "timeout": 7},
            ]}],
            "SubagentStop": [{"hooks": [
                {"type": "command", "command": command, "timeout": 30},
            ]}],
        },
    }
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps(original))
    monkeypatch.setenv("CCTALLY_DISABLE_CODEX_HOOKS", "1")
    uninstall = argparse.Namespace(
        status=False, dry_run=False, uninstall=True, purge=False, yes=True,
        json=True, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )

    assert ns["cmd_setup"](uninstall) == 0

    payload = json.loads(capsys.readouterr().out)
    row = payload["codex_hooks"]["roots"][0]
    assert row["state"] == "feature_disabled"
    assert row["feature_enabled"] is False
    assert row["requires_review"] is False
    assert row["changes"] == {
        "Stop": {"added": 0, "removed": 1, "unchanged": 0},
        "SubagentStop": {"added": 0, "removed": 1, "unchanged": 0},
    }
    assert json.loads(hooks_path.read_text()) == {
        "keep": {"user": "setting"},
        "hooks": {
            "Stop": [{"hooks": [
                {"type": "command", "command": "/usr/bin/user-stop", "timeout": 7},
            ]}],
        },
    }


def test_disabled_uninstall_rejects_malformed_codex_hooks_before_any_mutation(
    runtime, monkeypatch, capsys,
):
    """Feature-off cleanup still validates every Codex document up front."""
    ns, home = runtime
    hooks_path = home / "hooks.json"
    malformed = '{"hooks": {"Stop": "broken"}}\n'
    hooks_path.write_text(malformed)

    settings_path = ns["CLAUDE_SETTINGS_PATH"]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = {
        "hooks": {
            "PostToolBatch": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": "/opt/cctally/bin/cctally hook-tick",
                }],
            }],
        },
    }
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    local_bin = ns["_setup_local_bin_dir"]()
    local_bin.mkdir(parents=True)
    repo_root = ns["_setup_resolve_repo_root"]()
    symlink_path = local_bin / "cctally"
    symlink_path.symlink_to(ns["_setup_resolve_symlink_source"](repo_root, "cctally"))
    before_settings = settings_path.read_bytes()
    before_hooks = hooks_path.read_bytes()
    before_link = os.readlink(symlink_path)
    monkeypatch.setenv("CCTALLY_DISABLE_CODEX_HOOKS", "1")
    uninstall = argparse.Namespace(
        status=False, dry_run=False, uninstall=True, purge=False, yes=True,
        json=False, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )

    assert ns["cmd_setup"](uninstall) == 1

    assert "hooks.Stop" in capsys.readouterr().err
    assert settings_path.read_bytes() == before_settings
    assert hooks_path.read_bytes() == before_hooks
    assert symlink_path.is_symlink()
    assert os.readlink(symlink_path) == before_link


def test_setup_rejects_a_malformed_codex_file_before_claude_or_symlink_writes(
    runtime, capsys,
):
    ns, home = runtime
    (home / "hooks.json").write_text('{"hooks": {"Stop": "broken"}}')
    claude_dir = home.parent / ".claude"
    claude_dir.mkdir(exist_ok=True)
    args = argparse.Namespace(
        status=False, dry_run=False, uninstall=False, purge=False, yes=True,
        json=False, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )

    assert ns["cmd_setup"](args) == 1
    assert "hooks.Stop" in capsys.readouterr().err
    assert not (claude_dir / "settings.json").exists()
    assert not (home.parent / ".local" / "bin" / "cctally").exists()
    assert json.loads((home / "hooks.json").read_text()) == {
        "hooks": {"Stop": "broken"}
    }


def test_feature_disabled_install_validates_malformed_codex_before_any_mutation(
    runtime, monkeypatch, capsys,
):
    ns, home = runtime
    hooks_path = home / "hooks.json"
    hooks_path.write_text('{"hooks": {"Stop": "broken"}}\n')
    settings_path = ns["CLAUDE_SETTINGS_PATH"]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"user": {"keep": True}}) + "\n")
    before_settings = settings_path.read_bytes()
    before_hooks = hooks_path.read_bytes()
    before_app_entries = sorted(
        path.relative_to(ns["APP_DIR"])
        for path in ns["APP_DIR"].rglob("*")
    )
    monkeypatch.setenv("CCTALLY_DISABLE_CODEX_HOOKS", "1")
    args = argparse.Namespace(
        status=False, dry_run=False, uninstall=False, purge=False, yes=True,
        json=False, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )

    assert ns["cmd_setup"](args) == 1

    assert "hooks.Stop" in capsys.readouterr().err
    assert settings_path.read_bytes() == before_settings
    assert hooks_path.read_bytes() == before_hooks
    assert not (home.parent / ".local" / "bin" / "cctally").exists()
    assert not list(settings_path.parent.glob("settings.json.cctally-backup-*"))
    assert sorted(
        path.relative_to(ns["APP_DIR"])
        for path in ns["APP_DIR"].rglob("*")
    ) == before_app_entries


def test_reinstall_repairs_restrictive_codex_hook_permissions(runtime):
    ns, home = runtime
    binary = "/opt/cctally/bin/cctally"
    installed, _added = ns["_codex_hooks_plan_install"]({}, binary)
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps(installed))
    hooks_path.chmod(0o644)
    home.chmod(0o755)

    ns["_setup_manage_codex_hooks"]("install", binary)

    assert stat.S_IMODE(hooks_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_uninstall_leaves_an_unowned_hooks_file_untouched(runtime):
    ns, home = runtime
    hooks_path = home / "hooks.json"
    original = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}],
        }
    }
    hooks_path.write_text(json.dumps(original))
    hooks_path.chmod(0o644)
    home.chmod(0o755)

    ns["_setup_manage_codex_hooks"]("uninstall", "/opt/cctally/bin/cctally")

    assert json.loads(hooks_path.read_text()) == original
    assert stat.S_IMODE(hooks_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(home.stat().st_mode) == 0o755


def test_setup_install_status_and_uninstall_manage_every_codex_home(
    runtime, monkeypatch, capsys, tmp_path,
):
    """The normal setup lifecycle manages roots independently and reports JSON."""
    ns, first = runtime
    second = tmp_path / "second-codex-home"
    second.mkdir()
    monkeypatch.setenv("CODEX_HOME", f"{first},{second}")
    (tmp_path / ".claude").mkdir(exist_ok=True)
    monkeypatch.setitem(ns, "_setup_create_symlinks", lambda *args: [])
    monkeypatch.setitem(ns, "_setup_path_includes_local_bin", lambda: True)
    monkeypatch.setitem(ns, "_setup_oauth_token_present", lambda: False)
    monkeypatch.setitem(ns, "_setup_progress_enabled", lambda **kwargs: False)

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_cache", lambda *args, **kwargs: SimpleNamespace(lock_contended=False, rows_changed=0),
    )
    install = argparse.Namespace(
        status=False, dry_run=False, uninstall=False, purge=False, yes=True,
        json=True, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )
    assert ns["cmd_setup"](install) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["codex_hooks"]["installed_count"] == 2
    assert {row["state"] for row in payload["codex_hooks"]["roots"]} == {
        "installed_untrusted"
    }
    for home in (first, second):
        document = json.loads((home / "hooks.json").read_text())
        assert len(_handlers(document, "Stop")) == len(_handlers(document, "SubagentStop")) == 1

    status = argparse.Namespace(json=True)
    assert ns["_setup_status"](status) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["codex_hooks"]["installed_count"] == 2
    assert {row["state"] for row in status_payload["codex_hooks"]["roots"]} == {
        "installed_untrusted"
    }

    uninstall = argparse.Namespace(
        status=False, dry_run=False, uninstall=True, purge=False, yes=True,
        json=True, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )
    assert ns["cmd_setup"](uninstall) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["codex_hooks"]["installed_count"] == 0
    for home in (first, second):
        assert json.loads((home / "hooks.json").read_text()) == {}


def test_setup_install_and_uninstall_support_a_pure_codex_home(
    runtime, monkeypatch, capsys,
):
    ns, home = runtime
    claude_dir = home.parent / ".claude"
    shutil.rmtree(claude_dir)
    assert not claude_dir.exists()
    monkeypatch.setitem(ns, "_setup_create_symlinks", lambda *args: [])
    monkeypatch.setitem(ns, "_setup_path_includes_local_bin", lambda: True)
    monkeypatch.setitem(ns, "_setup_oauth_token_present", lambda: False)
    monkeypatch.setitem(ns, "_setup_progress_enabled", lambda **kwargs: False)

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_cache",
        lambda *args, **kwargs: SimpleNamespace(lock_contended=False, rows_changed=0),
    )
    install = argparse.Namespace(
        status=False, dry_run=False, uninstall=False, purge=False, yes=True,
        json=True, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )
    assert ns["cmd_setup"](install) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["codex_hooks"]["installed_count"] == 1
    assert len(_handlers(json.loads((home / "hooks.json").read_text()), "Stop")) == 1
    assert not (home.parent / ".claude" / "settings.json").exists()

    uninstall = argparse.Namespace(
        status=False, dry_run=False, uninstall=True, purge=False, yes=True,
        json=True, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )
    assert ns["cmd_setup"](uninstall) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["codex_hooks"]["installed_count"] == 0
    assert json.loads((home / "hooks.json").read_text()) == {}
    assert not (home.parent / ".claude" / "settings.json").exists()


def test_worker_log_lines_are_not_counted_as_hook_fires(tmp_path, monkeypatch):
    """`hook-tick.log` is no longer only hook fires (public #5).

    The three detached Codex workers write their outcomes there — the log is
    the only place they can, since all their streams are `/dev/null` — and
    those lines are `provider=codex op=… result=…` with no `event=` at all. The
    scan counted every timestamped line as a fire and filed them under
    `by_event["unknown"]`, which is meant to report a hook payload whose
    `hook_event_name` cctally could not read. Both `cctally setup --status` and
    doctor's `hooks.recent_activity` read those numbers.
    """
    import datetime as _dt

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core

    stamp = _dt.datetime.now(_dt.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    log = _cctally_core.HOOK_TICK_LOG_PATH
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join([
        f"{stamp} event=Stop           session=abc ingested=0 malformed=0 "
        f"skipped=0 oauth=ok(7d=1%) dur_ms=12",
        f"{stamp} provider=codex source_root_key=rk event=Stop sync=ok "
        f"blocks=1 milestones=0 alert_eligible_roots=1 quota_alerts=0 "
        f"budget_alerts=0 backlog=0 dur_ms=3 result=success",
        f"{stamp} provider=codex op=quota-verify result=success blocks=608",
        f"{stamp} provider=codex op=quota-verify-spawn result=spawned",
        f"{stamp} provider=codex op=replay-drain result=success files=2",
    ]) + "\n", encoding="utf-8")

    counts = ns["_setup_recent_log_stats"]()

    assert counts["fires"] == 2, (
        f"the three detached-worker lines were counted as hook fires: {counts}")
    assert counts["by_event"] == {"Stop": 2}
    assert "unknown" not in counts["by_event"]


# ── #719 / #720: Codex `[hooks.state]` observability + slot preservation ──


def _codex_root(home, *, key="root-1"):
    import _lib_codex_hooks as hooks

    return hooks.CodexHookRoot(key, home, home / "hooks.json")


# The keyword is not spelled `timeout`: it is a handler field this builder
# writes into a dict, and `tests/test_timing_budget_guard.py` reads every
# `timeout=` keyword in the test estate as a blocking wait to classify.
def _hooks_document(command, *, handler_timeout=30):
    handler = {"type": "command", "command": command}
    if handler_timeout is not None:
        handler["timeout"] = handler_timeout
    return {
        "hooks": {
            event: [{"hooks": [dict(handler)]}]
            for event in ("Stop", "SubagentStop")
        }
    }


def _write_config(home, body):
    path = home / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _state_body(hooks_path, entries):
    """Render a `[hooks.state]` table from `{(event_token, g, h): fields}`."""
    lines = []
    for (token, group, handler), fields in entries.items():
        lines.append(f'[hooks.state."{hooks_path}:{token}:{group}:{handler}"]')
        for name, value in fields.items():
            if isinstance(value, bool):
                lines.append(f"{name} = {str(value).lower()}")
            else:
                lines.append(f'{name} = "{value}"')
        lines.append("")
    return "\n".join(lines)


def _age(path, seconds):
    import os as _os

    stamp = _os.stat(path).st_mtime + seconds
    _os.utime(path, (stamp, stamp))


def test_codex_hook_state_key_is_an_exact_lookup_with_snake_case_events(runtime):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    root = _codex_root(home)
    assert hooks.CODEX_HOOK_EVENT_STATE_TOKENS == {
        "Stop": "stop", "SubagentStop": "subagent_stop",
    }
    assert hooks.codex_hook_state_key(root.hooks_path, "SubagentStop", 2, 0) == (
        f"{root.hooks_path}:subagent_stop:2:0"
    )
    assert hooks.codex_hook_state_key(root.hooks_path, "Stop", 0, 1) == (
        f"{root.hooks_path}:stop:0:1"
    )


def test_codex_hook_state_reader_separates_absent_ok_and_unreadable(runtime):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    config = home / "config.toml"
    assert hooks.read_codex_hook_state(config) == ("absent", {})

    _write_config(home, 'model = "gpt-5"\n')
    assert hooks.read_codex_hook_state(config) == ("ok", {})

    _write_config(home, "this is not = = toml\n")
    status, table = hooks.read_codex_hook_state(config)
    assert status == "unreadable"

    # A plugin-spec key must survive verbatim; the reader never splits on ':'.
    _write_config(home, (
        '[hooks.state."claude-memory@Claudest:hooks/hooks.json:stop:0:0"]\n'
        'trusted_hash = "deadbeef"\n'
    ))
    status, table = hooks.read_codex_hook_state(config)
    assert status == "ok"
    assert list(table) == ["claude-memory@Claudest:hooks/hooks.json:stop:0:0"]


def test_codex_hook_classification_is_ordered_and_total(runtime, monkeypatch):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    root = _codex_root(home)
    command = "/opt/cctally/bin/cctally hook-tick --foreground --source codex"
    root.hooks_path.write_text(json.dumps(_hooks_document(command)))

    # 8. No config.toml at all → untrusted, and never enabled.
    observation = hooks.observe_codex_hook_root(root)
    assert observation.state == "installed_untrusted"
    assert observation.observed_enabled is False
    assert observation.requires_review is True

    entries = {
        ("stop", 0, 0): {"trusted_hash": "aaa"},
        ("subagent_stop", 0, 0): {"trusted_hash": "bbb"},
    }
    config = _write_config(home, _state_body(root.hooks_path, entries))
    _age(root.hooks_path, -60)
    # 10. Trust recorded, config at least as new as the handler file.
    observation = hooks.observe_codex_hook_root(root)
    assert observation.state == "installed_enabled"
    assert observation.observed_enabled is True
    assert observation.requires_review is False

    # 9. hooks.json newer than config.toml → unverified.
    _age(root.hooks_path, 600)
    assert hooks.observe_codex_hook_root(root).state == "installed_unverified"
    _age(root.hooks_path, -600)

    # 7. `enabled = false` outranks every trust rule.
    disabled = dict(entries)
    disabled[("stop", 0, 0)] = {"trusted_hash": "aaa", "enabled": False}
    _write_config(home, _state_body(root.hooks_path, disabled))
    observation = hooks.observe_codex_hook_root(root)
    assert observation.state == "installed_disabled"
    assert observation.observed_enabled is False
    assert observation.requires_review is False

    # 3. Absent `enabled` reads as enabled; only boolean false disables.
    enabled = dict(entries)
    enabled[("stop", 0, 0)] = {"trusted_hash": "aaa", "enabled": True}
    _write_config(home, _state_body(root.hooks_path, enabled))
    assert hooks.observe_codex_hook_root(root).state == "installed_enabled"

    # 6. A non-boolean `enabled` is not observable.
    _write_config(home, (
        f'[hooks.state."{root.hooks_path}:stop:0:0"]\n'
        'trusted_hash = "aaa"\n'
        'enabled = "yes"\n'
        f'[hooks.state."{root.hooks_path}:subagent_stop:0:0"]\n'
        'trusted_hash = "bbb"\n'
    ))
    observation = hooks.observe_codex_hook_root(root)
    assert observation.state == "installed_trust_unobservable"
    assert observation.observed_enabled is False
    assert observation.requires_review is None

    # 6. An unparseable config.toml is not observable either.
    _write_config(home, "= = =\n")
    assert hooks.observe_codex_hook_root(root).state == "installed_trust_unobservable"

    # 8. A recorded entry with no string trusted_hash is untrusted.
    _write_config(home, (
        f'[hooks.state."{root.hooks_path}:stop:0:0"]\n'
        'enabled = true\n'
        f'[hooks.state."{root.hooks_path}:subagent_stop:0:0"]\n'
        'trusted_hash = "bbb"\n'
    ))
    assert hooks.observe_codex_hook_root(root).state == "installed_untrusted"

    # 1. The feature switch outranks everything.
    _write_config(home, _state_body(root.hooks_path, entries))
    monkeypatch.setenv("CCTALLY_DISABLE_CODEX_HOOKS", "1")
    assert hooks.observe_codex_hook_root(root).state == "feature_disabled"
    monkeypatch.delenv("CCTALLY_DISABLE_CODEX_HOOKS")

    # 2. A malformed hooks.json outranks every state rule but the feature one.
    root.hooks_path.write_text("{not json")
    observation = hooks.observe_codex_hook_root(root)
    assert observation.state == "malformed"
    assert observation.error


def test_duplicate_and_legacy_registrations_never_reach_an_installed_state(runtime):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    root = _codex_root(home)
    command = "/opt/cctally/bin/cctally hook-tick --foreground --source codex"
    entries = {
        ("stop", 0, 0): {"trusted_hash": "aaa"},
        ("subagent_stop", 0, 0): {"trusted_hash": "bbb"},
    }
    _write_config(home, _state_body(root.hooks_path, entries))

    # Rule 4: two supported handlers on one event is the condition setup exists
    # to reconcile, so it is `absent`, never installed.
    duplicated = _hooks_document(command)
    duplicated["hooks"]["Stop"].append(
        {"hooks": [{"type": "command", "command": command, "timeout": 30}]})
    root.hooks_path.write_text(json.dumps(duplicated))
    observation = hooks.observe_codex_hook_root(root)
    assert observation.state == "absent"
    assert observation.observed_enabled is False

    # Rule 5: the legacy two-token form is managed but never functioning.
    legacy = "/opt/cctally/lib/cctally-npm-shim.js hook-tick"
    root.hooks_path.write_text(json.dumps(_hooks_document(legacy, handler_timeout=None)))
    assert hooks.observe_codex_hook_root(root).state == "absent"

    # Rule 3: no supported handler at all.
    root.hooks_path.write_text(json.dumps(_hooks_document("/usr/bin/other tool")))
    assert hooks.observe_codex_hook_root(root).state == "absent"


@pytest.mark.parametrize("command,managed,canonical", [
    ("/opt/cctally/bin/cctally hook-tick --foreground --source codex", True, True),
    ("/opt/x/cctally-npm-shim.js hook-tick --foreground --source codex", True, True),
    ("/opt/x/cctally-npm-shim.js hook-tick", True, False),
    ("/opt/cctally/bin/cctally hook-tick", False, False),
    ("cctally hook-tick --foreground --source codex", False, False),
    ("/opt/x/other hook-tick --foreground --source codex", False, False),
    ("/opt/x/cctally-npm-shim.js hook-tick --foreground", False, False),
])
def test_widened_ownership_predicate_covers_both_managed_forms(
    command, managed, canonical,
):
    import _lib_codex_hooks as hooks

    handler = {"type": "command", "command": command, "timeout": 30}
    assert hooks.is_managed_codex_hook_handler(handler) is managed
    assert hooks.is_canonical_codex_hook_form(handler) is canonical
    assert hooks.is_managed_codex_hook_handler(
        {"type": "prompt", "command": command}) is False


def test_install_delta_is_event_indexed_over_managed_handlers(runtime):
    ns, home = runtime
    binary = "/opt/cctally/bin/cctally"
    command = f"{binary} hook-tick --foreground --source codex"

    # Fresh append on both events.
    _planned, changes = ns["_codex_hooks_plan_install"]({}, binary)
    assert changes == {
        "Stop": {"added": 1, "removed": 0, "unchanged": 0},
        "SubagentStop": {"added": 1, "removed": 0, "unchanged": 0},
    }

    # An exact canonical survivor is unchanged; a re-plan is a no-op.
    installed = _planned
    _again, changes = ns["_codex_hooks_plan_install"](installed, binary)
    assert changes == {
        "Stop": {"added": 0, "removed": 0, "unchanged": 1},
        "SubagentStop": {"added": 0, "removed": 0, "unchanged": 1},
    }

    # A normalization is one removal plus one addition; each discarded
    # duplicate is one further removal.
    document = _hooks_document(command, handler_timeout=7)
    document["hooks"]["Stop"].append(
        {"hooks": [{"type": "command", "command": command, "timeout": 30}]})
    _planned, changes = ns["_codex_hooks_plan_install"](document, binary)
    assert changes["Stop"] == {"added": 1, "removed": 2, "unchanged": 0}
    assert changes["SubagentStop"] == {"added": 1, "removed": 1, "unchanged": 0}


def test_uninstall_delta_counts_managed_removals_per_event(runtime):
    ns, home = runtime
    binary = "/opt/cctally/bin/cctally"
    command = f"{binary} hook-tick --foreground --source codex"
    document = _hooks_document(command)
    document["hooks"]["Stop"][0]["hooks"].append(
        {"type": "command", "command": "/usr/local/bin/other", "timeout": 3})

    _planned, changes = ns["_codex_hooks_plan_uninstall"](document, binary)
    assert changes == {
        "Stop": {"added": 0, "removed": 1, "unchanged": 0},
        "SubagentStop": {"added": 0, "removed": 1, "unchanged": 0},
    }


def test_cross_install_duplicate_normalizes_when_the_slot_is_provably_safe(runtime):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    binary = "/opt/cctally/bin/cctally"
    root = _codex_root(home)
    foreign = "/usr/lib/node_modules/cctally/bin/cctally-npm-shim.js hook-tick --foreground --source codex"
    document = _hooks_document(foreign)

    planned, changes = ns["_codex_hooks_plan_install"](
        document, binary, state_table={}, hooks_path=root.hooks_path,
    )
    handlers = _handlers(planned, "Stop")
    assert len(handlers) == 1
    assert handlers[0]["command"] == f"{binary} hook-tick --foreground --source codex"
    assert changes["Stop"] == {"added": 1, "removed": 1, "unchanged": 0}


def test_plan_refuses_when_a_surviving_handler_would_move_onto_a_recorded_key(runtime):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    binary = "/opt/cctally/bin/cctally"
    root = _codex_root(home)
    command = f"{binary} hook-tick --foreground --source codex"
    # Group 0 holds ONLY a managed duplicate that uninstall removes, so the
    # foreign group 1 renumbers down onto group 0's recorded trust key.
    document = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": command, "timeout": 30}]},
                {"hooks": [{"type": "command", "command": "/usr/bin/other", "timeout": 3}]},
            ],
            "SubagentStop": [
                {"hooks": [{"type": "command", "command": command, "timeout": 30}]},
            ],
        }
    }
    table = {
        hooks.codex_hook_state_key(root.hooks_path, "Stop", 0, 0): {
            "trusted_hash": "aaa"},
    }
    with pytest.raises(hooks.CodexHookSlotCollision) as excinfo:
        ns["_codex_hooks_plan_uninstall"](
            document, binary, state_table=table, hooks_path=root.hooks_path)
    message = str(excinfo.value)
    assert str(root.hooks_path) in message
    assert "Stop" in message
    assert "no configuration file was changed" in message.lower()
    assert "/hooks" in message


def test_plan_refuses_when_a_new_handler_lands_on_a_stale_recorded_key(runtime):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    binary = "/opt/cctally/bin/cctally"
    root = _codex_root(home)
    stale = hooks.codex_hook_state_key(root.hooks_path, "Stop", 0, 0)
    table = {stale: {"trusted_hash": "aaa"}}

    with pytest.raises(hooks.CodexHookSlotCollision) as excinfo:
        ns["_codex_hooks_plan_install"](
            {}, binary, state_table=table, hooks_path=root.hooks_path)
    assert stale in str(excinfo.value)
    # #720 acceptance 3: a key recorded for a handler that no longer exists
    # is reported in the diagnostic, never cleaned up.
    assert excinfo.value.stale_keys == [stale]
    assert "stale [hooks.state] keys" in str(excinfo.value)


def test_c3_refuses_an_in_place_content_change_under_a_live_trust_record(runtime):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    binary = "/opt/cctally/bin/cctally"
    root = _codex_root(home)
    foreign = "/usr/lib/node_modules/cctally/bin/cctally-npm-shim.js hook-tick --foreground --source codex"
    document = _hooks_document(foreign)
    table = {
        hooks.codex_hook_state_key(root.hooks_path, "Stop", 0, 0): {
            "trusted_hash": "aaa"},
    }
    with pytest.raises(hooks.CodexHookSlotCollision):
        ns["_codex_hooks_plan_install"](
            document, binary, state_table=table, hooks_path=root.hooks_path)


def test_c3_allows_an_in_place_content_change_under_a_disabled_trust_record(runtime):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    binary = "/opt/cctally/bin/cctally"
    root = _codex_root(home)
    foreign = "/usr/lib/node_modules/cctally/bin/cctally-npm-shim.js hook-tick --foreground --source codex"
    document = _hooks_document(foreign)
    table = {
        hooks.codex_hook_state_key(root.hooks_path, "Stop", 0, 0): {
            "trusted_hash": "aaa", "enabled": False},
        hooks.codex_hook_state_key(root.hooks_path, "SubagentStop", 0, 0): {
            "trusted_hash": "bbb", "enabled": False},
    }
    planned, _changes = ns["_codex_hooks_plan_install"](
        document, binary, state_table=table, hooks_path=root.hooks_path)
    assert _handlers(planned, "Stop")[0]["command"].endswith("--source codex")


def test_an_oserror_from_the_hooks_path_probe_classifies_as_malformed(runtime):
    """An over-long name (ENAMETOOLONG) reaches `malformed`, not `absent`.

    `_read_hooks_document` opens the file and swallows only
    `FileNotFoundError`, so this errno reaches the `CodexHooksError` branch on
    every supported interpreter. It did not before: the probe was
    `Path.exists()`, which re-raised ENAMETOOLONG on 3.11 through 3.13 and
    returned False for it on 3.14, so the same tree classified `malformed` on
    the runner and `absent` on a 3.14 host.

    ENAMETOOLONG is used rather than EACCES because it reproduces for every
    uid, where a mode-0 parent does not bite as root. The EACCES case — the
    one that motivated the guard — is covered by
    `test_an_unreadable_hooks_document_classifies_as_malformed_not_absent`,
    which seeds the errno instead of depending on the process's uid.
    """
    ns, home = runtime
    import _lib_codex_hooks as hooks

    setup = sys.modules["_cctally_setup"]
    root = hooks.CodexHookRoot("root-1", home, home / ("h" * 5000 + ".json"))

    observation = hooks.observe_codex_hook_root(root)
    row = setup._codex_hook_row(root)

    assert observation.state == "malformed"
    assert observation.error
    assert row["state"] == "malformed"
    assert row["remediation"] == hooks.codex_hook_remediation("malformed")


def test_a_hooks_path_under_a_regular_file_classifies_as_malformed(runtime):
    """ENOTDIR reaches `malformed` on EVERY interpreter, and once did on none.

    `Path.exists()` counts ENOTDIR among the errnos it treats as absence on
    3.11 through 3.14 alike, so a `hooks.json` whose parent component is a
    regular file used to read as an uninstalled root on every version — the
    same fail-open EACCES produces on 3.14, in a form that needs no uid and no
    seeding to reproduce. Measured on this estate's interpreters: `exists()`
    answers False and `read_text` raises `NotADirectoryError`.
    """
    ns, home = runtime
    import _lib_codex_hooks as hooks

    setup = sys.modules["_cctally_setup"]
    blocker = home / "not-a-directory"
    blocker.write_text("this is a regular file\n", encoding="utf-8")
    root = hooks.CodexHookRoot("root-1", home, blocker / "hooks.json")

    observation = hooks.observe_codex_hook_root(root)
    row = setup._codex_hook_row(root)

    assert observation.state == "malformed", (
        "a hooks.json under a regular file read as an uninstalled root, so "
        "doctor would send the operator to `cctally setup`")
    assert observation.error
    assert row["state"] == "malformed"


def _deny_read(monkeypatch, target, method):
    """Make one path's read raise EACCES, leaving every other path alone.

    Seeded rather than provoked with `os.chmod(parent, 0o000)`, because that
    construction does nothing when the suite runs as root and the assertion
    below would then pass over a readable file — an unreachable criterion
    silently waived. `pytest.skip` is the other way out and is worse here: it
    is a suppression the estate manifest refuses without a retirement
    declaration, for a case the product genuinely has to handle.
    """
    import pathlib

    target = pathlib.Path(target)
    original = getattr(pathlib.Path, method)

    def _patched(self, *args, **kwargs):
        if self == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, method, _patched)


def test_an_unreadable_hooks_document_classifies_as_malformed_not_absent(
    runtime, monkeypatch,
):
    """EACCES on `hooks.json` is `malformed`; reading it as `absent` fails open.

    This is the failure #719 and #720 exist to eliminate. A root whose
    `hooks.json` cannot be read carries no evidence either way, so reporting it
    as uninstalled makes `doctor` recommend `cctally setup`, and setup then
    plans an install over an empty document — discarding whatever the file
    actually held.
    """
    ns, home = runtime
    import _lib_codex_hooks as hooks

    setup = sys.modules["_cctally_setup"]
    root = _codex_root(home)
    root.hooks_path.write_text(json.dumps(_hooks_document(_owned_command())),
                               encoding="utf-8")
    _deny_read(monkeypatch, root.hooks_path, "read_text")

    with pytest.raises(hooks.CodexHooksError):
        hooks._read_hooks_document(root.hooks_path)

    observation = hooks.observe_codex_hook_root(root)
    row = setup._codex_hook_row(root)

    assert observation.state == "malformed"
    assert observation.error
    assert row["state"] == "malformed"
    assert row["remediation"] == hooks.codex_hook_remediation("malformed")


def test_an_unreadable_config_toml_is_unreadable_not_absent(runtime, monkeypatch):
    """EACCES on `config.toml` must not read as "Codex recorded nothing".

    The two verdicts are not interchangeable: `absent` states that Codex made
    no trust decision, which routes to `installed_untrusted` and lets a
    mutating plan proceed, while `unreadable` states that cctally cannot prove
    it is not landing on a stale key and refuses.
    """
    ns, home = runtime
    import _lib_codex_hooks as hooks

    config_path = home / "config.toml"
    config_path.write_text('[hooks.state."x:stop:0:0"]\ntrusted_hash = "a"\n',
                           encoding="utf-8")
    _deny_read(monkeypatch, config_path, "read_bytes")

    status, table = hooks.read_codex_hook_state(config_path)

    assert status == "unreadable"
    assert table == {}


def test_a_missing_config_toml_is_still_absent(runtime):
    """The one swallowed errno stays swallowed: ENOENT is `absent`."""
    _ns, home = runtime
    import _lib_codex_hooks as hooks

    status, table = hooks.read_codex_hook_state(home / "config.toml")

    assert status == "absent"
    assert table == {}


def _setup_args(**overrides):
    base = dict(
        status=False, dry_run=False, uninstall=False, purge=False, yes=True,
        json=False, force_dev=False, migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _setup_binary(ns):
    return str(ns["_setup_resolve_hook_target"](ns["_setup_resolve_repo_root"]()))


def _seed_collision(ns, home):
    """A hooks.json + config.toml pair whose reconcile must refuse."""
    import _lib_codex_hooks as hooks

    hooks_path = home / "hooks.json"
    foreign = (
        "/usr/lib/node_modules/cctally/bin/cctally-npm-shim.js "
        "hook-tick --foreground --source codex"
    )
    hooks_path.write_text(json.dumps(_hooks_document(foreign)))
    _write_config(home, _state_body(hooks_path, {
        ("stop", 0, 0): {"trusted_hash": "aaa"},
        ("subagent_stop", 0, 0): {"trusted_hash": "bbb"},
    }))
    return hooks_path


def test_install_refuses_a_slot_collision_before_any_mutation(runtime, capsys):
    ns, home = runtime
    hooks_path = _seed_collision(ns, home)
    settings_path = ns["CLAUDE_SETTINGS_PATH"]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"user": {"keep": True}}) + "\n")
    before_settings = settings_path.read_bytes()
    before_hooks = hooks_path.read_bytes()
    before_config = (home / "config.toml").read_bytes()

    assert ns["cmd_setup"](_setup_args()) == 1

    err = capsys.readouterr().err
    assert str(hooks_path) in err
    assert "no configuration file was changed" in err.lower()
    assert settings_path.read_bytes() == before_settings
    assert hooks_path.read_bytes() == before_hooks
    assert (home / "config.toml").read_bytes() == before_config
    assert not (home.parent / ".local" / "bin" / "cctally").exists()
    assert not list(settings_path.parent.glob("settings.json.cctally-backup-*"))


def test_uninstall_refuses_a_slot_collision_before_any_mutation(runtime, capsys):
    ns, home = runtime
    import _lib_codex_hooks as hooks

    binary = _setup_binary(ns)
    command = f"{binary} hook-tick --foreground --source codex"
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps({
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": command, "timeout": 30}]},
                {"hooks": [{"type": "command", "command": "/usr/bin/other", "timeout": 3}]},
            ],
            "SubagentStop": [
                {"hooks": [{"type": "command", "command": command, "timeout": 30}]},
            ],
        }
    }))
    _write_config(home, _state_body(hooks_path, {
        ("stop", 0, 0): {"trusted_hash": "aaa"},
    }))
    settings_path = ns["CLAUDE_SETTINGS_PATH"]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "hooks": {"PostToolBatch": [{
            "matcher": "*",
            "hooks": [{"type": "command", "command": f"{binary} hook-tick"}],
        }]},
    }, indent=2) + "\n")
    before_settings = settings_path.read_bytes()
    before_hooks = hooks_path.read_bytes()

    assert ns["cmd_setup"](_setup_args(uninstall=True)) == 1

    assert "no configuration file was changed" in capsys.readouterr().err.lower()
    assert settings_path.read_bytes() == before_settings
    assert hooks_path.read_bytes() == before_hooks


def test_dry_run_refuses_a_slot_collision_and_creates_no_lock_file(runtime, capsys):
    ns, home = runtime
    hooks_path = _seed_collision(ns, home)
    before_hooks = hooks_path.read_bytes()

    assert ns["cmd_setup"](_setup_args(dry_run=True)) == 1

    assert "no configuration file was changed" in capsys.readouterr().err.lower()
    assert hooks_path.read_bytes() == before_hooks
    assert not (home / "hooks.json.cctally.lock").exists()


def test_dry_run_planning_is_read_only_and_creates_no_lock_file(runtime, capsys):
    ns, home = runtime

    assert ns["cmd_setup"](_setup_args(dry_run=True, json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    assert not (home / "hooks.json").exists()
    assert not (home / "hooks.json.cctally.lock").exists()


def _foreign_canonical_command() -> str:
    """An owned-shaped handler written by a DIFFERENT install channel."""
    return (
        "/usr/lib/node_modules/cctally/bin/cctally-npm-shim.js "
        "hook-tick --foreground --source codex"
    )


def _canonical_document(ns):
    """A hooks.json whose reconcile is provably a no-change plan."""
    import _lib_codex_hooks as hooks

    return _hooks_document(hooks.codex_hook_command(_setup_binary(ns)))


def test_an_unparseable_config_toml_refuses_a_mutating_reconcile(runtime, capsys):
    ns, home = runtime
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps(_hooks_document(_foreign_canonical_command())))
    _write_config(home, "= = not toml\n")
    before_hooks = hooks_path.read_bytes()

    assert ns["cmd_setup"](_setup_args()) == 1

    err = capsys.readouterr().err
    assert "config.toml" in err
    assert "could not be read or parsed" in err
    assert hooks_path.read_bytes() == before_hooks
    assert not (home.parent / ".local" / "bin" / "cctally").exists()


def _make_config_unreadable(home):
    """A `config.toml` whose read raises `OSError`, not `TOMLDecodeError`.

    A directory at that path rather than a mode-0 file: `read_bytes()` raises
    `IsADirectoryError` for every uid, where the mode trick does not bite as
    root and would need a skip.
    """
    (home / "config.toml").mkdir()
    return home / "config.toml"


def test_an_unreadable_config_toml_refuses_a_mutating_reconcile(runtime, capsys):
    """The `except OSError` branch of the reader, not the TOML-decode branch."""
    ns, home = runtime
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps(_hooks_document(_foreign_canonical_command())))
    _make_config_unreadable(home)
    before_hooks = hooks_path.read_bytes()

    assert ns["cmd_setup"](_setup_args()) == 1

    err = capsys.readouterr().err
    assert "config.toml" in err
    assert "could not be read or parsed" in err
    assert hooks_path.read_bytes() == before_hooks
    assert not (home.parent / ".local" / "bin" / "cctally").exists()


@pytest.mark.parametrize("unreadable", [False, True])
def test_a_no_change_plan_proceeds_when_the_state_table_is_unobservable(
    runtime, capsys, unreadable,
):
    """Spec §2.3: `A no-change plan always proceeds.`

    Refusing it stranded the WHOLE install — no symlinks, no Claude hooks —
    over a trust concern that cannot arise, because nothing would have been
    written to `hooks.json` in the first place.
    """
    ns, home = runtime
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps(_canonical_document(ns)))
    if unreadable:
        _make_config_unreadable(home)
    else:
        _write_config(home, "= = not toml\n")
    before_hooks = hooks_path.read_bytes()

    assert ns["cmd_setup"](_setup_args(json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    row = payload["codex_hooks"]["roots"][0]
    assert row["state"] == "installed_trust_unobservable"
    assert row["changes"] == {
        "Stop": {"added": 0, "removed": 0, "unchanged": 1},
        "SubagentStop": {"added": 0, "removed": 0, "unchanged": 1},
    }
    assert hooks_path.read_bytes() == before_hooks
    assert (home.parent / ".local" / "bin" / "cctally").exists()


def test_a_missing_config_toml_never_blocks_a_mutating_reconcile(runtime, capsys):
    ns, home = runtime

    assert ns["cmd_setup"](_setup_args(json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    row = payload["codex_hooks"]["roots"][0]
    assert row["state"] == "installed_untrusted"
    assert row["changes"] == {
        "Stop": {"added": 1, "removed": 0, "unchanged": 0},
        "SubagentStop": {"added": 1, "removed": 0, "unchanged": 0},
    }


@pytest.mark.parametrize("collide_from_call", [2, 3])
def test_a_state_table_that_changes_after_the_preflight_refuses_cleanly(
    runtime, capsys, monkeypatch, collide_from_call,
):
    """The TOCTOU window the read-only preflight cannot close.

    The preflight plans without the write locks, so another process (or Codex
    itself) can rewrite ``config.toml`` before ``_setup_manage_codex_hooks``
    takes them. Call 2 is the failure pre-pass, call 3 the atomic writer's
    transform. What the fix buys at either one is the diagnostic, not a
    rescue from a crash: `bin/cctally` wraps every command in a catch-all
    that prints `Error: <message>` and returns 1, so a user would never see
    a traceback. Uncaught, the operator got that bare line and no `--json`
    envelope, under wording claiming nothing had been changed while the
    symlinks and ``settings.json`` already had been.
    """
    ns, home = runtime
    setup = sys.modules["_cctally_setup"]
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps(_hooks_document(_foreign_canonical_command())))
    before_hooks = hooks_path.read_bytes()
    recorded = {f"{hooks_path}:stop:0:0": {"trusted_hash": "recorded-earlier"}}
    calls = {"n": 0}

    def racing_read(config_path):
        calls["n"] += 1
        return ("ok", dict(recorded) if calls["n"] >= collide_from_call else {})

    monkeypatch.setattr(setup, "read_codex_hook_state", racing_read)

    assert ns["cmd_setup"](_setup_args()) == 1

    err = capsys.readouterr().err
    assert calls["n"] >= collide_from_call
    assert f"{hooks_path}:stop:0:0" in err
    assert "earlier steps of this run already completed" in err.lower()
    assert "no configuration file was changed" not in err.lower()
    assert hooks_path.read_bytes() == before_hooks
    # The refusal fires only after the earlier steps landed, which is exactly
    # what its wording must admit.
    assert (home.parent / ".local" / "bin" / "cctally").exists()


def test_a_race_after_a_sibling_root_was_written_names_that_rewrite(
    runtime, capsys, monkeypatch, tmp_path,
):
    """The after-mutation refusal must not deny a write it just made.

    The failure pre-pass plans every root before the first write, so a STABLE
    collision still aborts before any ``hooks.json`` moves. Only a state
    table that changes between one root's write and the next root's transform
    reaches here — and then the earlier root's file really was rewritten, so
    a flat "the Codex hooks file itself was not changed" is a false
    statement rather than a vague one.
    """
    ns, home_a = runtime
    setup = sys.modules["_cctally_setup"]
    import _lib_codex_hooks as hooks_lib

    home_b = tmp_path / "codex home b"
    home_b.mkdir()
    first, second = sorted(
        (home_a, home_b),
        key=lambda home: hooks_lib.source_root_key(str(home.resolve())),
    )
    monkeypatch.setenv("CODEX_HOME", f"{home_a},{home_b}")
    for home in (home_a, home_b):
        (home / "hooks.json").write_text(
            json.dumps(_hooks_document(_foreign_canonical_command())))
    before_first = (first / "hooks.json").read_bytes()
    before_second = (second / "hooks.json").read_bytes()
    racing_key = f"{second / 'hooks.json'}:stop:0:0"
    seen: dict[str, int] = {}

    def racing_read(config_path):
        key = str(config_path)
        seen[key] = seen.get(key, 0) + 1
        # Per root: call 1 is the read-only preflight, call 2 the failure
        # pre-pass, call 3 the atomic writer's transform. Only call 3 for the
        # SECOND root runs after the first root's file has been replaced.
        if key == str(second / "config.toml") and seen[key] >= 3:
            return ("ok", {racing_key: {"trusted_hash": "recorded-earlier"}})
        return ("ok", {})

    monkeypatch.setattr(setup, "read_codex_hook_state", racing_read)

    assert ns["cmd_setup"](_setup_args()) == 1

    err = capsys.readouterr().err
    assert racing_key in err
    assert (first / "hooks.json").read_bytes() != before_first
    assert (second / "hooks.json").read_bytes() == before_second
    # The refusal names the rewrite it made and scopes the unchanged claim to
    # the root it actually refused on.
    assert str(first / "hooks.json") in err
    assert f"{second / 'hooks.json'} itself was not rewritten" in err
    assert "no codex hooks file was changed" not in err.lower()
    assert "rewrote the contents of no codex hooks file" not in err.lower(), (
        "the empty-changed-paths shape names no root, so it must never be "
        "emitted for a run that did rewrite one")
    # A file was replaced, so the operator is told where its previous
    # contents went. Without this the refusal reports an irreversible-looking
    # rewrite with no stated way back.
    backups = sorted((first).glob("hooks.json.cctally-backup-*"))
    assert len(backups) == 1, f"expected one dated backup, found {backups}"
    assert str(backups[0]) in err
    assert backups[0].read_bytes() == before_first


def test_an_after_mutation_json_refusal_flags_itself_as_after_mutation(
    runtime, capsys, monkeypatch,
):
    """A machine-readable caller must not substring-match English prose."""
    ns, home = runtime
    setup = sys.modules["_cctally_setup"]
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps(_hooks_document(_foreign_canonical_command())))
    recorded = {f"{hooks_path}:stop:0:0": {"trusted_hash": "recorded-earlier"}}
    calls = {"n": 0}

    def racing_read(config_path):
        calls["n"] += 1
        return ("ok", dict(recorded) if calls["n"] >= 2 else {})

    monkeypatch.setattr(setup, "read_codex_hook_state", racing_read)

    assert ns["cmd_setup"](_setup_args(json=True)) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    assert payload["reason"] == "codex_hook_slot_collision"
    assert payload["after_mutation"] is True
    assert payload["config_path"] == str(home / "config.toml")
    # The pre-pass aborts before the write loop, so nothing was rewritten.
    assert payload["changed_hooks_paths"] == []
    assert payload["changed_hooks_backups"] == {}


def test_an_orphan_state_key_refusal_names_the_manual_remedy(runtime, capsys):
    """C2 with no handler anywhere: `/hooks` has nothing to review.

    D1 forbids cctally from removing the entry, so a diagnostic pointing at
    Codex's review surface leaves the operator with no escape at all.
    """
    ns, home = runtime
    hooks_path = home / "hooks.json"
    config_path = _write_config(home, _state_body(hooks_path, {
        ("stop", 0, 0): {"trusted_hash": "recorded-for-a-handler-that-is-gone"},
    }))

    assert ns["cmd_setup"](_setup_args()) == 1

    err = capsys.readouterr().err
    assert f"{hooks_path}:stop:0:0" in err
    assert str(config_path) in err
    assert "[hooks.state]" in err
    assert "by hand" in err
    assert "Review the cctally handler in Codex /hooks" not in err


def test_the_text_render_states_all_three_delta_axes_on_both_runs(runtime, capsys):
    ns, home = runtime
    foreign = _foreign_canonical_command()
    hooks_path = home / "hooks.json"
    document = _hooks_document(foreign)
    document["hooks"]["Stop"].append(
        {"hooks": [{"type": "command", "command": foreign, "timeout": 30}]})
    hooks_path.write_text(json.dumps(document))
    tail = f"Codex hooks at {hooks_path}: 2 added, 3 removed, 0 unchanged"

    assert ns["cmd_setup"](_setup_args(dry_run=True)) == 0
    dry_run = capsys.readouterr().out
    assert f"Would reconcile {tail}" in dry_run

    assert ns["cmd_setup"](_setup_args()) == 0
    applied = capsys.readouterr().out
    assert f"\u2713 Reconciled {tail}" in applied


def test_an_idempotent_reinstall_still_states_its_zero_delta(runtime, capsys):
    """Kept deliberately: §2.7 asks for all three axes on every reconciled
    root, and a silent idempotent run cannot be compared against its own dry
    run on the text surface."""
    ns, home = runtime
    hooks_path = home / "hooks.json"

    assert ns["cmd_setup"](_setup_args()) == 0
    capsys.readouterr()

    assert ns["cmd_setup"](_setup_args()) == 0
    applied = capsys.readouterr().out
    assert (
        f"\u2713 Reconciled Codex hooks at {hooks_path}: "
        f"0 added, 0 removed, 2 unchanged"
    ) in applied


def test_dry_run_and_applied_runs_report_the_same_delta(runtime, capsys):
    ns, home = runtime
    binary = _setup_binary(ns)
    foreign = (
        "/usr/lib/node_modules/cctally/bin/cctally-npm-shim.js "
        "hook-tick --foreground --source codex"
    )
    hooks_path = home / "hooks.json"
    document = _hooks_document(foreign)
    document["hooks"]["Stop"].append(
        {"hooks": [{"type": "command", "command": foreign, "timeout": 30}]})
    hooks_path.write_text(json.dumps(document))

    assert ns["cmd_setup"](_setup_args(dry_run=True, json=True)) == 0
    dry_run = json.loads(capsys.readouterr().out)["codex_hooks"]["roots"][0]
    assert dry_run["changes"] == {
        "Stop": {"added": 1, "removed": 2, "unchanged": 0},
        "SubagentStop": {"added": 1, "removed": 1, "unchanged": 0},
    }

    assert ns["cmd_setup"](_setup_args(json=True)) == 0
    applied = json.loads(capsys.readouterr().out)["codex_hooks"]["roots"][0]
    assert applied["changes"] == dry_run["changes"]


def test_every_setup_json_envelope_carries_schema_version_2(runtime, capsys):
    ns, home = runtime

    assert ns["_setup_status"](argparse.Namespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 2

    assert ns["cmd_setup"](_setup_args(dry_run=True, json=True)) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 2

    assert ns["cmd_setup"](_setup_args(json=True)) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 2

    assert ns["cmd_setup"](_setup_args(uninstall=True, json=True)) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 2

    assert ns["cmd_setup"](
        _setup_args(uninstall=True, purge=True, yes=False, json=True)) == 3
    declined = json.loads(capsys.readouterr().out)
    assert declined["schema_version"] == 2
    assert declined["result"] == "purge_declined"


def test_a_collision_refusal_under_json_emits_a_v2_envelope(runtime, capsys):
    ns, home = runtime
    hooks_path = _seed_collision(ns, home)

    assert ns["cmd_setup"](_setup_args(json=True)) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    assert payload["result"] == "err"
    assert payload["reason"] == "codex_hook_slot_collision"
    assert payload["exit_code"] == 1
    assert payload["codex_hooks"]["roots"]
    assert "no configuration file was changed" in payload["error"].lower()
    assert payload["hooks_path"] == str(hooks_path)
    assert payload["config_path"] == str(home / "config.toml")
    assert payload["after_mutation"] is False
    assert payload["changed_hooks_paths"] == []


def test_summary_exposes_enabled_counts_and_all_roots_enabled(runtime, capsys):
    ns, home = runtime
    binary = _setup_binary(ns)
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps(
        _hooks_document(f"{binary} hook-tick --foreground --source codex")))
    _write_config(home, _state_body(hooks_path, {
        ("stop", 0, 0): {"trusted_hash": "aaa"},
        ("subagent_stop", 0, 0): {"trusted_hash": "bbb"},
    }))
    _age(hooks_path, -60)

    assert ns["_setup_status"](argparse.Namespace(json=True)) == 0
    summary = json.loads(capsys.readouterr().out)["codex_hooks"]
    assert summary["installed_count"] == 1
    assert summary["enabled_count"] == 1
    assert summary["all_roots_enabled"] is True
    assert summary["error_count"] == 0
