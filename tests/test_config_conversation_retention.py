"""#313 P3 (F8): conversation.retention_days config get/set/unset + resolver.

Mirrors the dedicated-key config tests (test_expose_transcripts_config.py):
drive the real ``_cmd_config_set`` / ``_config_known_value`` /
``_cmd_config_unset`` entry points, plus the pure ``resolve_retention_days``
resolver used by the prune orchestrator.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402
from _retention_fixtures import QUICK_TIMEOUT_S  # noqa: E402

KEY = "conversation.retention_days"


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _set(ns, value):
    return ns["_cmd_config_set"](
        argparse.Namespace(key=KEY, value=value, emit_json=False)
    )


def _get(ns):
    return ns["_config_known_value"](ns["load_config"](), KEY)


def _unset(ns):
    return ns["_cmd_config_unset"](argparse.Namespace(key=KEY))


def test_get_default_is_90(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _get(ns) == 90


def test_set_positive_persists(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, "90") == 0
    assert _get(ns) == 90


def test_set_off_and_zero_disable(tmp_path, monkeypatch):
    ns = _load(tmp_path / "off", monkeypatch)
    assert _set(ns, "off") == 0
    assert _get(ns) == 0
    ns2 = _load(tmp_path / "zero", monkeypatch)
    assert _set(ns2, "0") == 0
    assert _get(ns2) == 0


def test_set_negative_rejected(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, "-5") == 2
    assert _get(ns) == 90  # unchanged


def test_set_boolean_word_rejected(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, "true") == 2
    assert _set(ns, "false") == 2


def test_set_non_numeric_rejected(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, "abc") == 2
    assert _set(ns, "1.5") == 2


def test_unset_reverts_to_default(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, "30") == 0
    assert _get(ns) == 30
    assert _unset(ns) == 0
    assert _get(ns) == 90


def test_resolver_defaults_and_malformed(tmp_path, monkeypatch):
    _load(tmp_path, monkeypatch)
    import _cctally_config as cfg
    assert cfg.resolve_retention_days({}) == 90
    assert cfg.resolve_retention_days({"conversation": {"retention_days": 45}}) == 45
    assert cfg.resolve_retention_days({"conversation": {"retention_days": 0}}) == 0
    # Malformed persisted values degrade to the safe 90 default.
    assert cfg.resolve_retention_days({"conversation": {"retention_days": "garbage"}}) == 90
    assert cfg.resolve_retention_days({"conversation": {"retention_days": True}}) == 90
    assert cfg.resolve_retention_days({"conversation": {"retention_days": -3}}) == 90
    assert cfg.resolve_retention_days({"conversation": {"retention_days": 1.5}}) == 90
    assert cfg.resolve_retention_days({"conversation": "not-an-object"}) == 90
    # A clean integer string persisted by hand resolves.
    assert cfg.resolve_retention_days({"conversation": {"retention_days": "60"}}) == 60


# ── #965: the prune-only resolver and its eligibility predicate ───────────

SWITCH = "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF"
_REPO = Path(__file__).resolve().parent.parent


def _modules():
    """The resolver module and the core module IT uses, held for one test.

    Never reloaded: T1's call-time contract is about one imported resolver.
    """
    cfg = importlib.import_module("_cctally_config")
    return cfg, cfg._cctally_core


def _code_root(tmp_path, monkeypatch, marker):
    """Aim the `_repo_root` seam at a scratch code root with ``marker``."""
    root = tmp_path / "code"
    root.mkdir()
    if marker == "directory":
        (root / ".git").mkdir()
    elif marker == "file":
        (root / ".git").write_text("gitdir: /nonexistent/.git/worktrees/965\n")
    _, core = _modules()
    monkeypatch.setattr(core, "_repo_root", lambda: root)
    return root


# config, switch (None = unset), marker (None = absent), prune result, reported
_TABLE = [
    ({}, "1", "directory", 0, 90),
    ({}, "1", "file", 0, 90),
    ({"conversation": {}}, "1", "directory", 0, 90),
    ({"conversation": {"other": True}}, "1", "file", 0, 90),
    ({"conversation": {"retention_days": 0}}, "1", "directory", 0, 0),
    ({"conversation": {"retention_days": 30}}, "1", "directory", 30, 30),
    ({"conversation": {"retention_days": "45"}}, "1", "directory", 45, 45),
    ({"conversation": {"retention_days": " 60 "}}, "1", "directory", 60, 60),
    ({"conversation": {"retention_days": None}}, "1", "directory", 90, 90),
    ({"conversation": {"retention_days": "garbage"}}, "1", "directory", 90, 90),
    ({"conversation": {"retention_days": True}}, "1", "directory", 90, 90),
    ({"conversation": {"retention_days": -3}}, "1", "directory", 90, 90),
    ({"conversation": {"retention_days": 1.5}}, "1", "directory", 90, 90),
    ({"conversation": "not-an-object"}, "1", "directory", 90, 90),
    ({"conversation": None}, "1", "directory", 90, 90),
    ("not-a-dict", "1", "directory", 90, 90),
    ({}, None, "directory", 90, 90),
    ({}, "", "directory", 90, 90),
    ({}, "0", "directory", 90, 90),
    ({}, "true", "directory", 90, 90),
    ({}, " 1", "directory", 90, 90),
    ({}, "1", None, 90, 90),
    ({"conversation": {}}, "1", None, 90, 90),
]


@pytest.mark.parametrize("config,switch,marker,prune,reported", _TABLE)
def test_965_prune_resolver_matrix(config, switch, marker, prune, reported,
                                   tmp_path, monkeypatch):
    """T1: every row of spec 5.2, and the reporting resolver beside it (I2)."""
    cfg, _ = _modules()
    _code_root(tmp_path, monkeypatch, marker)
    if switch is None:
        monkeypatch.delenv(SWITCH, raising=False)
    else:
        monkeypatch.setenv(SWITCH, switch)
    assert cfg.resolve_prune_retention_days(config) == prune
    assert cfg.resolve_retention_days(config) == reported


@pytest.mark.parametrize("form", ["directory", "file"])
def test_965_the_marker_is_read_at_call_time(form, tmp_path, monkeypatch):
    """T1 (965-SPEC-002): one imported resolver, never reloaded; 90 -> 0 -> 90."""
    cfg, _ = _modules()
    root = _code_root(tmp_path, monkeypatch, None)
    monkeypatch.setenv(SWITCH, "1")
    resolve = cfg.resolve_prune_retention_days
    assert resolve({}) == 90
    marker = root / ".git"
    if form == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: /nonexistent/.git/worktrees/965\n")
    assert resolve({}) == 0
    if form == "directory":
        marker.rmdir()
    else:
        marker.unlink()
    assert resolve({}) == 90


def test_965_the_switch_is_read_at_call_time(tmp_path, monkeypatch):
    """T1 / I5: the marker held, the switch toggled; delenv is honored at once."""
    cfg, _ = _modules()
    _code_root(tmp_path, monkeypatch, "directory")
    resolve = cfg.resolve_prune_retention_days
    monkeypatch.setenv(SWITCH, "1")
    assert resolve({}) == 0
    monkeypatch.delenv(SWITCH)
    assert resolve({}) == 90
    monkeypatch.setenv(SWITCH, "1")
    assert resolve({}) == 0


def test_965_eligibility_follows_the_code_not_the_working_directory(
        tmp_path, monkeypatch):
    """T2 matrix (I4, second counterexample): packaged code run from inside a
    checkout ignores the switch."""
    cfg, _ = _modules()
    _code_root(tmp_path, monkeypatch, None)
    monkeypatch.chdir(_REPO)
    assert (Path.cwd() / ".git").exists(), "non-vacuity: the cwd must be a checkout"
    monkeypatch.setenv(SWITCH, "1")
    assert cfg.resolve_prune_retention_days({}) == 90


@pytest.mark.parametrize("form", ["directory", "file"])
def test_965_the_suppressors_do_not_affect_eligibility(form, tmp_path, monkeypatch):
    """T2 matrix (spec 5.1): the dev-autodetect suppressor governs dev mode only."""
    cfg, core = _modules()
    _code_root(tmp_path, monkeypatch, form)
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    monkeypatch.setenv("CCTALLY_ALLOW_PROD_MIGRATION", "1")
    assert core._is_dev_checkout() is False
    assert core._has_git_checkout_marker() is True
    assert cfg.resolve_prune_retention_days({}) == 0


def test_965_reporting_reads_90_under_the_test_switch(tmp_path, monkeypatch):
    """T11 (I2), preservation: passes on the baseline and the candidate."""
    ns = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    root = tmp_path / "code"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(core, "_repo_root", lambda: root)
    monkeypatch.setenv(SWITCH, "1")
    assert _get(ns) == 90
    env = dict(os.environ)
    env.update({SWITCH: "1", "HOME": str(tmp_path / "home"),
                "CCTALLY_DATA_DIR": str(tmp_path / "cli-data"), "TZ": "Etc/UTC"})
    env.pop("CODEX_HOME", None)
    # timing-budget: #965 hang bound for one quick cctally launch, QUICK_TIMEOUT_S (30 s) from tests/_retention_fixtures.py, under the 120 s cap
    proc = subprocess.run(
        [sys.executable, str(_REPO / "bin" / "cctally"), "config", "get", KEY, "--json"],
        env=env, capture_output=True, text=True, timeout=QUICK_TIMEOUT_S)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["conversation"]["retention_days"] == 90
