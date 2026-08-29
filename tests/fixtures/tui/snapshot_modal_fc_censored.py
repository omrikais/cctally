"""Forecast --explain modal over a RIGHT-CENSORED reading (#661 S2 §3.2).

Reuses the censored underlay snapshot, then overrides modal_kind so
`_tui_render_once` opens the Forecast explain modal. The modal is the surface
that named the wrong cause: it printed `r_recent unavailable — no 24h-prior
sample` at a censored reading whose underlay carries a 24-hour-prior sample.
"""
import importlib.machinery
import importlib.util
import pathlib
import sys

_PATH = pathlib.Path(__file__).resolve().parent / "snapshot_censored.py"
_LOADER = importlib.machinery.SourceFileLoader("_censored_underlay_fc",
                                               str(_PATH))
_SPEC = importlib.util.spec_from_loader("_censored_underlay_fc", _LOADER)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["_censored_underlay_fc"] = _MOD
_LOADER.exec_module(_MOD)
SNAPSHOT = _MOD.SNAPSHOT
RUNTIME_OVERRIDES = {"modal_kind": "forecast"}
