"""``dashboard.lan_auth`` config-key round-trip tests (issue #282)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402

_KEY = "dashboard.lan_auth"


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def test_default_is_true_and_key_is_allowed(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _KEY in ns["ALLOWED_CONFIG_KEYS"]
    assert ns["_config_known_value"](ns["load_config"](), _KEY) is True


def test_set_accepts_boolean_spellings_and_preserves_siblings(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    ns["save_config"]({"dashboard": {"bind": "lan", "live_tail": False}})

    for raw, expected in (("false", False), ("on", True)):
        rc = ns["_cmd_config_set"](
            argparse.Namespace(key=_KEY, value=raw, emit_json=False)
        )
        assert rc == 0
        cfg = ns["load_config"]()
        assert ns["_config_known_value"](cfg, _KEY) is expected
        assert cfg["dashboard"]["bind"] == "lan"
        assert cfg["dashboard"]["live_tail"] is False


def test_invalid_set_is_rejected_without_writing(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    rc = ns["_cmd_config_set"](
        argparse.Namespace(key=_KEY, value="maybe", emit_json=False)
    )
    assert rc == 2
    assert ns["_config_known_value"](ns["load_config"](), _KEY) is True


def test_hand_edited_junk_fails_safe_true(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    for junk in (1, "garbage", "false", "off", "no", "0", [], {}):
        ns["save_config"]({"dashboard": {"lan_auth": junk}})
        assert ns["_config_known_value"](ns["load_config"](), _KEY) is True


def test_unset_restores_default_and_preserves_siblings(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    ns["save_config"]({
        "dashboard": {"bind": "lan", "lan_auth": False, "live_tail": False}
    })
    assert ns["_cmd_config_unset"](argparse.Namespace(key=_KEY)) == 0
    cfg = ns["load_config"]()
    assert ns["_config_known_value"](cfg, _KEY) is True
    assert cfg["dashboard"] == {"bind": "lan", "live_tail": False}


def test_unset_prunes_empty_dashboard_block(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    ns["save_config"]({"dashboard": {"lan_auth": False}})
    assert ns["_cmd_config_unset"](argparse.Namespace(key=_KEY)) == 0
    assert "dashboard" not in ns["load_config"]()
