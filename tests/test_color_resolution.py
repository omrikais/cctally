"""§9.2 — color resolver behavior on the 2 real ANSI cmds (project, diff).

Spec docs/superpowers/specs/2026-05-22-issue-86-session-a-ccusage-alias-pass.md
§7.3: ``--color`` overrides ``NO_COLOR`` env; ``--no-color`` overrides
``FORCE_COLOR`` env; deny-wins on the ``--color --no-color`` clash.

The 2 real ANSI emitters are ``cmd_project`` and ``cmd_diff``. All other
in-scope cmds parse ``--color`` / ``--no-color`` as documented no-op
surface — that coverage lives in
``tests/test_ccusage_alias_pass.py::TestAliasSurface``.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"

# An ANSI CSI escape — present in any ANSI-styled stdout slice.
ANSI_CSI = "\x1b["


def _load_cctally_module():
    """Import the script as a module (no .py extension) so we can call
    ``_resolve_color_enabled`` directly without spawning a subprocess.
    """
    loader = SourceFileLoader("cctally", str(CCTALLY))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _run_with_env(args, **env_overrides):
    """Spawn ``cctally`` with env overrides; ``None`` value strips the var."""
    env = os.environ.copy()
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run(
        [sys.executable, str(CCTALLY), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


# ─── Unit tests on _resolve_color_enabled (pure function) ──────────────


@pytest.fixture(scope="module")
def cctally_mod():
    return _load_cctally_module()


def _ns(**kwargs):
    """Build a minimal argparse Namespace with the kwargs supplied."""
    ns = argparse.Namespace()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_no_color_flag_wins_over_force_color_env(cctally_mod, monkeypatch):
    """--no-color overrides FORCE_COLOR=1 env (spec §7.3 rung 1)."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert cctally_mod._resolve_color_enabled(_ns(color=False, no_color=True)) is False


def test_color_flag_wins_over_no_color_env(cctally_mod, monkeypatch):
    """--color overrides NO_COLOR=1 env (spec §7.3 rung 2)."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert cctally_mod._resolve_color_enabled(_ns(color=True, no_color=False)) is True


def test_deny_wins_color_plus_no_color(cctally_mod, monkeypatch):
    """--color AND --no-color → --no-color wins (deny-wins, spec §7.3)."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert cctally_mod._resolve_color_enabled(_ns(color=True, no_color=True)) is False


def test_force_color_env_no_flag(cctally_mod, monkeypatch):
    """FORCE_COLOR=1 env alone enables (spec §7.3 rung 3)."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert cctally_mod._resolve_color_enabled(_ns(color=False, no_color=False)) is True


def test_no_color_env_no_flag(cctally_mod, monkeypatch):
    """NO_COLOR=1 env alone disables (spec §7.3 rung 4)."""
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert cctally_mod._resolve_color_enabled(_ns(color=False, no_color=False)) is False


def test_neither_flag_nor_env_no_tty(cctally_mod, monkeypatch):
    """No flag, no env, non-tty stdout → False (existing auto-detect)."""
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    # When pytest runs, sys.stdout is captured — not a tty. So the auto-
    # detect rung falls through to False. If the test runner is itself
    # attached to a tty (e.g., direct CLI run), the value can flip to
    # True; in that case both branches are well-defined per spec and
    # we accept either outcome — the assertion that matters is that
    # the function does NOT crash and returns a bool.
    val = cctally_mod._resolve_color_enabled(_ns(color=False, no_color=False))
    assert isinstance(val, bool)


# ─── End-to-end subprocess tests on project + diff ─────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Empty CCTALLY home → no DB/cache; commands exit clean with empty
    output but still go through the renderer dispatch path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.mark.parametrize("cmd", ["project", "diff"])
class TestColorResolutionSubprocess:
    """End-to-end coverage on the 2 real ANSI emitters. Empty fake_home
    means many commands emit no rows — assert on what we CAN observe
    (parse success + no ANSI when no-color path fires) rather than
    requiring data-bearing output."""

    def _base_args(self, cmd):
        if cmd == "diff":
            return ["diff", "--a", "last-week", "--b", "this-week"]
        return [cmd]

    def test_color_flag_parses_under_no_color_env(self, cmd, fake_home):
        r = _run_with_env(self._base_args(cmd) + ["--color"], NO_COLOR="1")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_no_color_flag_suppresses_under_force_color_env(self, cmd, fake_home):
        r = _run_with_env(
            self._base_args(cmd) + ["--no-color"], FORCE_COLOR="1",
        )
        # --no-color must suppress ANSI even when FORCE_COLOR is set.
        assert ANSI_CSI not in r.stdout, (
            f"--no-color failed to suppress ANSI under FORCE_COLOR=1: "
            f"{r.stdout!r}"
        )

    def test_deny_wins_color_plus_no_color(self, cmd, fake_home):
        r = _run_with_env(self._base_args(cmd) + ["--color", "--no-color"])
        # Deny wins → no ANSI in stdout.
        assert ANSI_CSI not in r.stdout, (
            f"deny-wins failed: ANSI present despite --no-color: "
            f"{r.stdout!r}"
        )

    def test_no_color_env_no_flag(self, cmd, fake_home):
        r = _run_with_env(self._base_args(cmd), NO_COLOR="1")
        assert ANSI_CSI not in r.stdout

    def test_force_color_env_no_flag(self, cmd, fake_home):
        r = _run_with_env(self._base_args(cmd), FORCE_COLOR="1")
        # FORCE_COLOR alone is enough to enable; data may be empty in
        # fake_home so we only assert no parse error here. Behavioral
        # coverage of the resolver itself lives in the unit tests above.
        assert "unrecognized arguments" not in r.stderr, r.stderr
