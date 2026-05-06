"""Current Week per-percent modal fixture (spec §4.6.1).

Reuses the WARN underlay snapshot, then overrides modal_kind so
`_tui_render_once` opens the Current Week modal on top of the
underlying frame.
"""
import importlib.machinery
import importlib.util
import pathlib
import sys

_PATH = pathlib.Path(__file__).resolve().parent / "snapshot_warn.py"
_LOADER = importlib.machinery.SourceFileLoader("_warn_underlay_cw", str(_PATH))
_SPEC = importlib.util.spec_from_loader("_warn_underlay_cw", _LOADER)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["_warn_underlay_cw"] = _MOD
_LOADER.exec_module(_MOD)
SNAPSHOT = _MOD.SNAPSHOT
RUNTIME_OVERRIDES = {"modal_kind": "current_week", "modal_snap_pending": True}
