"""``dashboard.expose_transcripts`` config-key round-trip tests (Plan 2, Task 2).

Boolean key, default ``False``, under the ``dashboard`` block — mirrors
``dashboard.bind``'s placement and ``alerts.enabled``'s boolean semantics.

Driven through the established ``load_script() + redirect_paths()`` harness so
config reads/writes hit a temp data dir, NOT the developer's real
``~/.local/share/cctally`` (see the "HOME-only test loader reads prod DB"
gotcha — a bare ``setenv(HOME)`` would read the prod DB once ``_cctally_core``
is import-cached). The get/set/unset entry points are ``_config_known_value`` /
``_cmd_config_set`` / ``_cmd_config_unset`` (the names ``bin/cctally`` exports),
driven exactly as ``tests/test_budget_alerts.py`` does.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def test_set_path_normalizes_truthy_falsy_strings(tmp_path, monkeypatch):
    # The set-path now reuses the shared _normalize_alerts_enabled_value
    # (DRY — there is no longer a dedicated expose_transcripts normalizer).
    # Drive the string spellings through the real set entry point and read
    # them back, so the test exercises the actual config path, not a
    # private helper that may diverge from it.
    get = lambda ns: ns["_config_known_value"](
        ns["load_config"](), "dashboard.expose_transcripts"
    )
    for v in ("true", "True", "1", "yes", "on"):
        ns = _load(tmp_path / v, monkeypatch)
        rc = ns["_cmd_config_set"](
            argparse.Namespace(
                key="dashboard.expose_transcripts", value=v, emit_json=False
            )
        )
        assert rc == 0
        assert get(ns) is True
    for v in ("false", "False", "0", "no", "off"):
        ns = _load(tmp_path / f"f_{v}", monkeypatch)
        rc = ns["_cmd_config_set"](
            argparse.Namespace(
                key="dashboard.expose_transcripts", value=v, emit_json=False
            )
        )
        assert rc == 0
        assert get(ns) is False


def test_get_path_round_trips_real_json_bool(tmp_path, monkeypatch):
    # After the DRY merge the get-path reads a stored JSON bool. The shared
    # _normalize_alerts_enabled_value only tolerates str spellings, so the
    # get-path short-circuits a real bool. Assert both bool values round-trip
    # straight through the get entry point.
    ns = _load(tmp_path, monkeypatch)
    get = lambda: ns["_config_known_value"](
        ns["load_config"](), "dashboard.expose_transcripts"
    )

    ns["save_config"]({"dashboard": {"expose_transcripts": True}})
    assert get() is True

    ns["save_config"]({"dashboard": {"expose_transcripts": False}})
    assert get() is False


def test_get_set_unset_round_trip(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    get = lambda: ns["_config_known_value"](ns["load_config"](), "dashboard.expose_transcripts")

    # default (unset) → False
    assert get() is False

    # set true
    rc = ns["_cmd_config_set"](
        argparse.Namespace(key="dashboard.expose_transcripts", value="true", emit_json=False)
    )
    assert rc == 0
    assert get() is True

    # a sibling dashboard.bind set must survive the expose set (same parent block)
    rc = ns["_cmd_config_set"](
        argparse.Namespace(key="dashboard.bind", value="lan", emit_json=False)
    )
    assert rc == 0
    assert get() is True
    assert ns["_config_known_value"](ns["load_config"](), "dashboard.bind") == "lan"

    # unset expose → back to default False; sibling dashboard.bind survives
    rc = ns["_cmd_config_unset"](
        argparse.Namespace(key="dashboard.expose_transcripts")
    )
    assert rc == 0
    assert get() is False
    assert ns["_config_known_value"](ns["load_config"](), "dashboard.bind") == "lan"


def test_unset_prunes_empty_dashboard_block(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    # Only expose set → unset must prune the now-empty dashboard parent block.
    rc = ns["_cmd_config_set"](
        argparse.Namespace(key="dashboard.expose_transcripts", value="on", emit_json=False)
    )
    assert rc == 0
    rc = ns["_cmd_config_unset"](
        argparse.Namespace(key="dashboard.expose_transcripts")
    )
    assert rc == 0
    cfg = ns["load_config"]()
    assert "dashboard" not in cfg


def test_set_rejects_invalid_value(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    rc = ns["_cmd_config_set"](
        argparse.Namespace(key="dashboard.expose_transcripts", value="maybe", emit_json=False)
    )
    assert rc == 2
    # config untouched → still default False
    assert ns["_config_known_value"](ns["load_config"](), "dashboard.expose_transcripts") is False
