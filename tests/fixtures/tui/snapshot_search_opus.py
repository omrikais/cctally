"""Search confirmed for 'opus' on warn snapshot."""
import importlib.machinery, importlib.util, pathlib, sys

_PATH = pathlib.Path(__file__).resolve().parent / "snapshot_warn.py"
_LOADER = importlib.machinery.SourceFileLoader("_warn_underlay4", str(_PATH))
_SPEC = importlib.util.spec_from_loader("_warn_underlay4", _LOADER)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["_warn_underlay4"] = _MOD
_LOADER.exec_module(_MOD)

SNAPSHOT = _MOD.SNAPSHOT
RUNTIME_OVERRIDES = {
    "search_term": "opus",
    # search_matches/search_index will be auto-populated by the renderer
    # (it computes match indices over the live post-filter+sort list); the
    # initial values are placeholders that the renderer will overwrite.
    "search_matches": [],
    "search_index": 0,
}
