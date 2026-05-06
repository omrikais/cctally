"""Sort by cost (descending) on warn snapshot."""
import importlib.machinery, importlib.util, pathlib, sys

_PATH = pathlib.Path(__file__).resolve().parent / "snapshot_warn.py"
_LOADER = importlib.machinery.SourceFileLoader("_warn_underlay3", str(_PATH))
_SPEC = importlib.util.spec_from_loader("_warn_underlay3", _LOADER)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["_warn_underlay3"] = _MOD
_LOADER.exec_module(_MOD)

SNAPSHOT = _MOD.SNAPSHOT
RUNTIME_OVERRIDES = {"sort_key": "cost"}
