"""WARN underlay + stale freshness chip (Task C6 / spec §3.4).

Reuses the WARN snapshot's data verbatim, then publishes a fresh
`TuiCurrentWeek` instance whose `freshness_label="stale"` /
`freshness_age=124` triggers the Current Week panel chip line.

The underlying `latest_snapshot_at` is unchanged from snapshot_warn
(`_NOW - 124s`), so the existing "· last snapshot: 2m 04s ago" line
keeps its value; the new chip ("⏱ as of HH:MM:SS · 124s ago") appends
below it. This isolates the chip path from any changes to the rest of
the panel layout.
"""
import dataclasses
import importlib.machinery
import importlib.util
import pathlib
import sys

_PATH = pathlib.Path(__file__).resolve().parent / "snapshot_warn.py"
_LOADER = importlib.machinery.SourceFileLoader("_warn_underlay_stale", str(_PATH))
_SPEC = importlib.util.spec_from_loader("_warn_underlay_stale", _LOADER)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["_warn_underlay_stale"] = _MOD
_LOADER.exec_module(_MOD)

_BASE = _MOD.SNAPSHOT
_CW = dataclasses.replace(
    _BASE.current_week,
    freshness_label="stale",
    freshness_age=124,
)
SNAPSHOT = dataclasses.replace(_BASE, current_week=_CW)
