"""Filter active fixture: filter_term='subscr' applied to warn snapshot.

Re-uses snapshot_warn.py SNAPSHOT verbatim and overrides RuntimeState
to simulate a confirmed filter. Spec §5.4.
"""
import importlib.machinery, importlib.util, pathlib, sys

_PATH = pathlib.Path(__file__).resolve().parent / "snapshot_warn.py"
_LOADER = importlib.machinery.SourceFileLoader("_warn_underlay", str(_PATH))
_SPEC = importlib.util.spec_from_loader("_warn_underlay", _LOADER)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["_warn_underlay"] = _MOD
_LOADER.exec_module(_MOD)

SNAPSHOT = _MOD.SNAPSHOT
RUNTIME_OVERRIDES = {"filter_term": "subscr"}
