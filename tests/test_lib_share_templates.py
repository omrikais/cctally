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
