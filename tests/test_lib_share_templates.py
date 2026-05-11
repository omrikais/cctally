"""Tests for the share template registry."""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TPL_PATH = _HERE.parent / "bin" / "_lib_share_templates.py"
_SPEC = importlib.util.spec_from_file_location("_lib_share_templates", _TPL_PATH)
_T = importlib.util.module_from_spec(_SPEC)
sys.modules["_lib_share_templates"] = _T
_SPEC.loader.exec_module(_T)


def test_registry_has_unique_ids():
    ids = [t.id for t in _T.SHARE_TEMPLATES]
    assert len(ids) == len(set(ids)), f"duplicate template ids: {ids}"


def test_registry_covers_all_share_capable_panels():
    if not _T.SHARE_TEMPLATES:
        import pytest
        pytest.skip("registry not yet populated — re-enabled after M1.4")
    panels_in_registry = {t.panel for t in _T.SHARE_TEMPLATES}
    assert panels_in_registry == _T.SHARE_CAPABLE_PANELS, (
        f"panel coverage mismatch — extra: {panels_in_registry - _T.SHARE_CAPABLE_PANELS}, "
        f"missing: {_T.SHARE_CAPABLE_PANELS - panels_in_registry}"
    )


def test_alerts_panel_not_in_share_capable_set():
    assert "alerts" not in _T.SHARE_CAPABLE_PANELS, (
        "alerts panel is a notification stream, not a data view — no share registry"
    )


def test_share_template_dataclass_is_frozen():
    if not _T.SHARE_TEMPLATES:
        import pytest
        pytest.skip("registry not yet populated — re-enabled after M1.4")
    sample = _T.SHARE_TEMPLATES[0]
    import dataclasses
    assert dataclasses.is_dataclass(sample)
    try:
        sample.id = "tampered"
        raise AssertionError("ShareTemplate should be frozen")
    except dataclasses.FrozenInstanceError:
        pass


def _fake_weekly_panel_data():
    """Synthetic panel_data shape returned by bin/cctally's _build_weekly_share_snapshot
    when the source helper is reused."""
    return {
        "weeks": [
            {"start_date": "2026-05-04", "cost_usd": 14.27, "pct_used": 0.71,
             "dollar_per_pct": 0.20, "top_projects": [
                ("project/aa", 5.12), ("project/bb", 3.81), ("project/cc", 2.04)]},
        ],
        "current_week_index": 0,
    }


def test_weekly_recap_builder_emits_snapshot():
    tpl = _T.get_template("weekly-recap")
    snap = tpl.builder(panel_data=_fake_weekly_panel_data(),
                       options={"theme": "light", "reveal_projects": True,
                                "no_branding": False, "top_n": 5,
                                "show_chart": True, "show_table": True,
                                "project_allowlist": None})
    import dataclasses
    assert dataclasses.is_dataclass(snap)
    assert snap.cmd == "weekly"
    assert snap.title and snap.period and snap.generated_at
    # Recap should include both chart and table:
    assert snap.chart is not None
    assert len(snap.rows) > 0
