"""The `POST /api/settings` leaf-disposition contract (#513 S1, spec §1.1-§1.2).

`bin/_lib_dashboard_settings_contract.py` is the single source of truth for
which settings leaves the dashboard endpoint writes, which it accepts and
deliberately does not persist, and which it rejects. The module is pure and
stdlib-only so this test and `tests/test_config_documentation.py` can import it
without loading the dashboard, and so there is no import cycle with
`_cctally_config`.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
MODULE_PATH = BIN / "_lib_dashboard_settings_contract.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "_lib_dashboard_settings_contract", MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_writable_leaves_are_exactly_the_eighteen_the_endpoint_writes():
    m = _load()
    writable = {
        p for p, d in m.SETTINGS_LEAF_DISPOSITIONS.items() if d == m.WRITABLE
    }
    assert writable == {
        "display.tz",
        "alerts.enabled", "alerts.projected_enabled", "alerts.notifier",
        "dashboard.cache_failure_markers", "dashboard.live_tail",
        "dashboard.lan_auth",
        "update.check.enabled", "update.check.ttl_hours", "update.channel",
        "cache_report.anomaly_threshold_pp",
        "budget.weekly_usd", "budget.alerts_enabled",
        "budget.alert_thresholds", "budget.projected_enabled",
        "budget.project_alerts_enabled",
        "budget.codex.alerts_enabled", "budget.codex.projected_enabled",
    }


def test_known_ignored_leaves_are_exactly_the_four_pinned_ones():
    m = _load()
    ignored = {
        p for p, d in m.SETTINGS_LEAF_DISPOSITIONS.items()
        if d == m.KNOWN_IGNORED
    }
    assert ignored == {
        "budget.period",
        "budget.codex.amount_usd",
        "budget.codex.period",
        "budget.codex.alert_thresholds",
    }


def test_every_disposition_is_one_of_the_two_states():
    m = _load()
    assert set(m.SETTINGS_LEAF_DISPOSITIONS.values()) == {
        m.WRITABLE, m.KNOWN_IGNORED
    }


def test_disposition_for_returns_none_on_unknown_path():
    m = _load()
    assert m.disposition_for("budget.projects") is None
    assert m.disposition_for("budget.accounts") is None
    assert m.disposition_for("budget.codex.accounts") is None
    assert m.disposition_for("alerts.quota") is None
    assert m.disposition_for("alerts.command_template") is None
    assert m.disposition_for("nonsense.leaf") is None


def test_object_paths_are_derived_not_hand_listed():
    m = _load()
    assert "budget" in m.SETTINGS_OBJECT_PATHS
    assert "budget.codex" in m.SETTINGS_OBJECT_PATHS
    assert "update" in m.SETTINGS_OBJECT_PATHS
    assert "update.check" in m.SETTINGS_OBJECT_PATHS
    # A leaf is never also an object path.
    assert not (m.SETTINGS_OBJECT_PATHS & set(m.SETTINGS_LEAF_DISPOSITIONS))


def test_top_level_blocks_are_the_six_the_endpoint_accepts():
    m = _load()
    assert m.SETTINGS_TOP_LEVEL_BLOCKS == frozenset({
        "display", "alerts", "update", "cache_report", "budget", "dashboard",
    })


def test_display_is_the_only_block_with_a_required_leaf():
    m = _load()
    assert m.SETTINGS_REQUIRED_LEAVES == {"display": frozenset({"display.tz"})}


def test_module_declares_no_imports_at_all():
    """Parsed, not grepped.

    A substring scan over the file would read the module's own prose --
    the docstring names ``_cctally_config`` to explain why the cycle is
    avoided -- and would then derive a structural fact from a comment.
    Walk the AST instead, so the assertion is about code.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    imported = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert imported == [], (
        "the contract module must stay dependency-free so the documentation "
        "test can load it without the dashboard or the config layer"
    )
