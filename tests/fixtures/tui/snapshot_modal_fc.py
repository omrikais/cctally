"""Forecast --explain modal fixture (spec §4.6.2).

Reuses the WARN underlay snapshot, then overrides modal_kind so
`_tui_render_once` opens the Forecast explain modal.
"""
import importlib.machinery
import importlib.util
import pathlib
import sys

_PATH = pathlib.Path(__file__).resolve().parent / "snapshot_warn.py"
_LOADER = importlib.machinery.SourceFileLoader("_warn_underlay_fc", str(_PATH))
_SPEC = importlib.util.spec_from_loader("_warn_underlay_fc", _LOADER)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["_warn_underlay_fc"] = _MOD
_LOADER.exec_module(_MOD)
SNAPSHOT = _MOD.SNAPSHOT
RUNTIME_OVERRIDES = {"modal_kind": "forecast"}
