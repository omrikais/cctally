"""``dashboard.cache_failure_markers`` config-key round-trip tests (spec §5).

Boolean key, default ``True`` (opt-out — absence is treated as ON), under the
``dashboard`` block — mirrors ``dashboard.expose_transcripts``'s placement and
boolean semantics, except the DEFAULT is True (opt-out, not opt-in).

Driven through the established ``load_script() + redirect_paths()`` harness so
config reads/writes hit a temp data dir, not the developer's real
``~/.local/share/cctally`` (the "HOME-only test loader reads prod DB" gotcha).
Entry points are ``_config_known_value`` / ``_cmd_config_set`` /
``_cmd_config_unset`` — the names ``bin/cctally`` exports.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402

_KEY = "dashboard.cache_failure_markers"


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def test_default_is_true_when_unset(tmp_path, monkeypatch):
    # Opt-out: absence is treated as ON. (Differs from expose_transcripts,
    # whose default is False.)
    ns = _load(tmp_path, monkeypatch)
    assert ns["_config_known_value"](ns["load_config"](), _KEY) is True


def test_key_is_in_allowed_config_keys(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _KEY in ns["ALLOWED_CONFIG_KEYS"]


def test_set_normalizes_truthy_falsy_strings(tmp_path, monkeypatch):
    get = lambda ns: ns["_config_known_value"](ns["load_config"](), _KEY)
    for v in ("true", "True", "1", "yes", "on"):
        ns = _load(tmp_path / v, monkeypatch)
        rc = ns["_cmd_config_set"](
            argparse.Namespace(key=_KEY, value=v, emit_json=False)
        )
        assert rc == 0
        assert get(ns) is True
    for v in ("false", "False", "0", "no", "off"):
        ns = _load(tmp_path / f"f_{v}", monkeypatch)
        rc = ns["_cmd_config_set"](
            argparse.Namespace(key=_KEY, value=v, emit_json=False)
        )
        assert rc == 0
        assert get(ns) is False


def test_get_round_trips_real_json_bool(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    get = lambda: ns["_config_known_value"](ns["load_config"](), _KEY)
    ns["save_config"]({"dashboard": {"cache_failure_markers": True}})
    assert get() is True
    ns["save_config"]({"dashboard": {"cache_failure_markers": False}})
    assert get() is False


def test_hand_edited_junk_surfaces_default_true(tmp_path, monkeypatch):
    # A bare int / non-bool scalar must surface the True default, not crash
    # (mirrors dashboard.expose_transcripts' junk handling).
    ns = _load(tmp_path, monkeypatch)
    get = lambda: ns["_config_known_value"](ns["load_config"](), _KEY)
    ns["save_config"]({"dashboard": {"cache_failure_markers": 1}})
    assert get() is True
    ns["save_config"]({"dashboard": {"cache_failure_markers": "garbage"}})
    assert get() is True


def test_set_rejects_invalid_value(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    rc = ns["_cmd_config_set"](
        argparse.Namespace(key=_KEY, value="maybe", emit_json=False)
    )
    assert rc == 2
    # config untouched → still default True
    assert ns["_config_known_value"](ns["load_config"](), _KEY) is True


def test_set_false_then_get_false(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    rc = ns["_cmd_config_set"](
        argparse.Namespace(key=_KEY, value="false", emit_json=False)
    )
    assert rc == 0
    assert ns["_config_known_value"](ns["load_config"](), _KEY) is False


def test_set_preserves_sibling_dashboard_keys(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    # Pre-set the bind + expose siblings, then write cache_failure_markers.
    assert ns["_cmd_config_set"](
        argparse.Namespace(key="dashboard.bind", value="lan", emit_json=False)
    ) == 0
    assert ns["_cmd_config_set"](
        argparse.Namespace(
            key="dashboard.expose_transcripts", value="true", emit_json=False
        )
    ) == 0
    assert ns["_cmd_config_set"](
        argparse.Namespace(key=_KEY, value="false", emit_json=False)
    ) == 0
    # All three coexist.
    cfg = ns["load_config"]()
    assert ns["_config_known_value"](cfg, "dashboard.bind") == "lan"
    assert ns["_config_known_value"](cfg, "dashboard.expose_transcripts") is True
    assert ns["_config_known_value"](cfg, _KEY) is False


def test_unset_restores_default_and_preserves_siblings(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    get = lambda: ns["_config_known_value"](ns["load_config"](), _KEY)
    assert ns["_cmd_config_set"](
        argparse.Namespace(key="dashboard.bind", value="lan", emit_json=False)
    ) == 0
    assert ns["_cmd_config_set"](
        argparse.Namespace(key=_KEY, value="false", emit_json=False)
    ) == 0
    assert get() is False
    # Unset only the markers key — back to default True; sibling bind survives.
    rc = ns["_cmd_config_unset"](argparse.Namespace(key=_KEY))
    assert rc == 0
    assert get() is True
    assert ns["_config_known_value"](ns["load_config"](), "dashboard.bind") == "lan"


def test_unset_prunes_empty_dashboard_block(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert ns["_cmd_config_set"](
        argparse.Namespace(key=_KEY, value="false", emit_json=False)
    ) == 0
    rc = ns["_cmd_config_unset"](argparse.Namespace(key=_KEY))
    assert rc == 0
    assert "dashboard" not in ns["load_config"]()
