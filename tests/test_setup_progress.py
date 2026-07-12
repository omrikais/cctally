"""Issue #281 S7 (R6): setup first-run cold-sync progress.

Structural, CI-stable tests for the TTY-gated stderr progress reporter
added to ``bin/_cctally_setup.py`` (``_setup_progress_enabled`` +
``_SetupProgressReporter``) plus one isolated subprocess integration test
proving the wiring in ``_setup_install``. No timing assertions (those
would flake in CI); gating is proven by env force-on / force-off and by
patching ``sys.stderr`` to a fake (non-)TTY.

Loader: mirrors ``tests/test_setup_brew_policy.py`` — ``load_script()``
returns the ``bin/cctally`` namespace *dict*; the extracted setup module
is reached via ``ns["_cctally_setup"]``.
"""
from __future__ import annotations

import argparse
import io
import json as _json
import os
import pathlib
import subprocess
import sys

import pytest
from conftest import load_script, redirect_paths
import _cctally_core

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CCTALLY_BIN = REPO_ROOT / "bin" / "cctally"


@pytest.fixture
def setup_module():
    return load_script()["_cctally_setup"]


# ── Gating: _setup_progress_enabled ────────────────────────────────────


def test_progress_enabled_env_forces(setup_module, monkeypatch):
    S = setup_module
    monkeypatch.setenv("CCTALLY_SETUP_PROGRESS", "1")
    # force-on beats --json.
    assert S._setup_progress_enabled(json_mode=True) is True
    monkeypatch.setenv("CCTALLY_SETUP_PROGRESS", "0")
    # force-off, regardless of TTY / json.
    assert S._setup_progress_enabled(json_mode=False) is False


def test_progress_enabled_tty_and_json(setup_module, monkeypatch):
    S = setup_module
    monkeypatch.delenv("CCTALLY_SETUP_PROGRESS", raising=False)
    # Non-TTY stderr → off even without --json.
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert S._setup_progress_enabled(json_mode=False) is False

    class _TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stderr", _TTY())
    # TTY + not-json → on; TTY + json → auto-suppressed.
    assert S._setup_progress_enabled(json_mode=False) is True
    assert S._setup_progress_enabled(json_mode=True) is False


# ── Reporter: gating, throttle, sync callback format ───────────────────


def test_reporter_disabled_is_silent(setup_module, monkeypatch):
    S = setup_module
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    S._SetupProgressReporter(enabled=False).emit("x", force=True)
    assert buf.getvalue() == ""


def test_reporter_force_and_throttle(setup_module, monkeypatch):
    S = setup_module
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = S._SetupProgressReporter(enabled=True)
    r.emit("first", force=True)          # force → emits
    r.emit("throttled")                  # within interval → suppressed
    out = buf.getvalue()
    assert "first" in out
    assert "throttled" not in out


def test_sync_callback_format(setup_module, monkeypatch):
    S = setup_module
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = S._SetupProgressReporter(enabled=True)

    class _Stats:
        files_processed = 3
        files_total = 10

    r.emit("", force=True)               # prime last_emit
    r._last_emit = 0.0                   # defeat the throttle for the assertion
    r.sync_callback(_Stats())
    assert "3/10 session files" in buf.getvalue()


# ── Integration: _setup_install streams progress to stderr ─────────────


def _build_fake_home(tmp_path: pathlib.Path) -> pathlib.Path:
    """A minimal fake ~/.claude with settings.json + a couple JSONL sessions.

    Enough for `cctally setup` to run its install path and drive the
    cold-sync bootstrap (sync_cache walks ~/.claude/projects/**/*.jsonl).
    """
    home = tmp_path / "home"
    claude = home / ".claude"
    projects = claude / "projects" / "-tmp-proj"
    projects.mkdir(parents=True)
    (claude / "settings.json").write_text("{}\n")
    # Two tiny JSONL session files so the walk has files to process.
    for i in range(2):
        line = _json.dumps(
            {
                "type": "assistant",
                "sessionId": f"sess-{i}",
                "cwd": "/tmp/proj",
                "timestamp": "2026-07-01T00:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-4-5-20250929",
                    "usage": {"input_tokens": 5, "output_tokens": 7},
                },
            }
        )
        (projects / f"sess-{i}.jsonl").write_text(line + "\n")
    return home


def _install_env(tmp_path: pathlib.Path, home: pathlib.Path) -> dict:
    env = dict(os.environ)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["CCTALLY_DATA_DIR"] = str(data_dir)
    env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    # The setup dev guard refuses a dev checkout unless auto-detect is off.
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    env["CCTALLY_DISABLE_UPDATE_CHECK"] = "1"
    env["CCTALLY_DISABLE_TELEMETRY"] = "1"
    env.pop("CCTALLY_SETUP_PROGRESS", None)
    return env


def _run_setup(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CCTALLY_BIN), "setup"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_setup_progress_force_on_and_off(tmp_path):
    """Force-on emits the pre-sync notice to stderr; force-off suppresses it."""
    home = _build_fake_home(tmp_path)
    env = _install_env(tmp_path, home)

    env["CCTALLY_SETUP_PROGRESS"] = "1"
    r = _run_setup(env)
    assert "Syncing session history" in r.stderr, (
        f"expected pre-sync notice on stderr; got stderr={r.stderr!r} "
        f"stdout={r.stdout!r} rc={r.returncode}"
    )

    env["CCTALLY_SETUP_PROGRESS"] = "0"
    r2 = _run_setup(env)
    assert "Syncing session history" not in r2.stderr, (
        f"force-off must suppress progress; got stderr={r2.stderr!r}"
    )


# ── In-process: the pre-OAuth notice wiring (spec §4 / Codex finding 9) ──
# The subprocess test above can't stub the network OAuth refresh, so drive
# `_setup_install` in-process (brew mode → no symlink I/O) with
# `_resolve_oauth_token` truthy and `_hook_tick_oauth_refresh` stubbed. This
# proves the `if oauth:` branch force-emits "⏳ Fetching current usage…" under
# force-on and suppresses it under force-off — no network, CI-stable.


def _install_flags(**overrides):
    base = dict(
        purge=False,
        yes=False,
        migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )
    base.update(overrides)
    return base


def _pin_settings_io_with_token(ns, monkeypatch, home, *, token):
    """Pin settings I/O to a tmp path + make `_resolve_oauth_token` truthy.

    Mirrors `_pin_settings_io` in tests/test_setup_brew_policy.py, but returns
    a token so `_setup_oauth_token_present()` is True and the bootstrap reaches
    the (stubbed) OAuth refresh and its pre-fetch notice.
    """
    pinned = home / ".claude" / "settings.json"
    pinned.parent.mkdir(parents=True, exist_ok=True)
    if not pinned.exists():
        pinned.write_text("{}\n")
    monkeypatch.setattr(_cctally_core, "CLAUDE_SETTINGS_PATH", pinned)
    real_load = ns["_load_claude_settings"]
    real_write = ns["_write_claude_settings_atomic"]
    real_backup = ns["_backup_claude_settings"]
    monkeypatch.setitem(ns, "_load_claude_settings", lambda path=pinned: real_load(path))
    monkeypatch.setitem(
        ns, "_write_claude_settings_atomic",
        lambda settings, path=pinned: real_write(settings, path),
    )
    monkeypatch.setitem(ns, "_backup_claude_settings", lambda path=pinned: real_backup(path))
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **k: token)
    return pinned


@pytest.mark.parametrize("force,present", [("1", True), ("0", False)])
def test_setup_install_oauth_notice_gated(tmp_path, monkeypatch, capsys, force, present):
    ns = load_script()
    setup = ns["_cctally_setup"]
    redirect_paths(ns, monkeypatch, tmp_path)
    home = tmp_path
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    local_bin = home / ".local" / "bin"
    _pin_settings_io_with_token(ns, monkeypatch, home, token="stub-oauth-token")
    monkeypatch.setattr(setup, "_setup_local_bin_dir", lambda: local_bin)
    # brew mode → skip symlink creation (no repo sibling binaries needed).
    monkeypatch.setattr(setup, "_setup_is_brew_install", lambda repo_root: True)
    monkeypatch.setattr(setup, "_setup_path_includes_local_bin", lambda: True)
    # Stub the OAuth refresh so the bootstrap never touches the network.
    monkeypatch.setitem(ns, "_hook_tick_oauth_refresh", lambda *a, **k: ("ok:stub", {}))
    monkeypatch.setitem(ns, "_hook_tick_throttle_touch", lambda *a, **k: None)
    monkeypatch.setenv("CCTALLY_SETUP_PROGRESS", force)

    rc = ns["_setup_install"](argparse.Namespace(json=False, **_install_flags()))
    assert rc == 0
    err = capsys.readouterr().err
    assert ("Fetching current usage" in err) is present, (
        f"force={force}: expected OAuth notice present={present}; got stderr={err!r}"
    )
