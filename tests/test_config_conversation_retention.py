"""#313 P3 (F8): conversation.retention_days config get/set/unset + resolver.

Mirrors the dedicated-key config tests (test_expose_transcripts_config.py):
drive the real ``_cmd_config_set`` / ``_config_known_value`` /
``_cmd_config_unset`` entry points, plus the pure ``resolve_retention_days``
resolver used by the prune orchestrator.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402

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
