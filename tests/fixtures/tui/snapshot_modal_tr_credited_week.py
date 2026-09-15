"""The Trend modal over the credited-week underlay (#734).

Reuses `snapshot_credited_week.py`, which seeds an isolated store and builds
its snapshot through the production builders, then opens the Trend modal with
the same override `snapshot_modal_tr.py` uses. The modal renders up to twelve
weeks of `weekly_history`, so this is where a credited week's two billing
cycles have to appear as two rows rather than one.
"""
import importlib.machinery
import importlib.util
import pathlib
import sys

_PATH = pathlib.Path(__file__).resolve().parent / "snapshot_credited_week.py"
_LOADER = importlib.machinery.SourceFileLoader(
    "_credited_week_underlay_tr", str(_PATH)
)
_SPEC = importlib.util.spec_from_loader("_credited_week_underlay_tr", _LOADER)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["_credited_week_underlay_tr"] = _MOD
_LOADER.exec_module(_MOD)

SNAPSHOT = _MOD.SNAPSHOT
assert len(SNAPSHOT.weekly_history) == 2, (
    "the modal's own history must carry both billing cycles",
    [row.week_start_at for row in SNAPSHOT.weekly_history],
)
RUNTIME_OVERRIDES = {"modal_kind": "trend", "modal_snap_pending": True}
