"""Additive, fake-home-only Codex hooks.json management contracts for #294 S2."""
from __future__ import annotations

import argparse
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
    assert added == {"Stop": 1, "SubagentStop": 1}
    assert installed["unrelated"] == original["unrelated"]
    for event in ("Stop", "SubagentStop"):
        matches = [h for h in _handlers(installed, event) if h.get("command") == expected]
        assert matches == [{"type": "command", "command": expected, "timeout": 30}]

    rerun, added_again = ns["_codex_hooks_plan_install"](installed, binary)
    assert rerun == installed
    assert added_again == {"Stop": 0, "SubagentStop": 0}


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
    assert removed == {"Stop": 1, "SubagentStop": 1}
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
    expected_owned = 1 + len(duplicates)
    assert before["stop_count"] == before["subagent_stop_count"] == expected_owned

    installed_summary = ns["_setup_manage_codex_hooks"]("install", binary)
    installed = json.loads(hooks_path.read_text())
    assert installed_summary["roots"][0]["state"] == "installed_review_required"
    for event in ("Stop", "SubagentStop"):
        assert _handlers(installed, event).count(canonical) == 1
        assert _handlers(installed, event).count(user) == 1
        assert len([h for h in _handlers(installed, event) if h.get("command") == command]) == 1

    hooks_path.write_text(json.dumps(document))
    removed_summary = ns["_setup_manage_codex_hooks"]("uninstall", binary)
    uninstalled = json.loads(hooks_path.read_text())
    assert removed_summary["roots"][0]["changes"] == {
        "Stop": expected_owned,
        "SubagentStop": expected_owned,
    }
    for event in ("Stop", "SubagentStop"):
        assert _handlers(uninstalled, event) == [user]


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
    assert row["state"] == "installed_trust_unobservable"
    assert row["requires_review"] is None
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

    monkeypatch.setattr(hooks.shutil, "copy2", observe_copy)
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
    worker.join(timeout=2)
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
    entered_writer = threading.Event()
    original_write = setup._write_codex_hooks_atomic

    def observe_write(*args, **kwargs):
        entered_writer.set()
        return original_write(*args, **kwargs)

    monkeypatch.setattr(setup, "_write_codex_hooks_atomic", observe_write)
    done = threading.Event()

    def install() -> None:
        ns["_setup_manage_codex_hooks"]("install", binary)
        done.set()

    worker = threading.Thread(target=install)
    worker.start()
    try:
        assert entered_writer.wait(1)
        hooks_path.write_text(json.dumps({"keep": "intervening-user-edit"}))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    worker.join(timeout=2)
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
    assert "Would add native Codex Stop/SubagentStop handlers" in text_output
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
        "Stop": 1,
        "SubagentStop": 1,
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
        "installed_review_required"
    }
    for home in (first, second):
        document = json.loads((home / "hooks.json").read_text())
        assert len(_handlers(document, "Stop")) == len(_handlers(document, "SubagentStop")) == 1

    status = argparse.Namespace(json=True)
    assert ns["_setup_status"](status) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["codex_hooks"]["installed_count"] == 2
    assert {row["state"] for row in status_payload["codex_hooks"]["roots"]} == {
        "installed_trust_unobservable"
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
