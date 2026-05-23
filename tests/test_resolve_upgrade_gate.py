"""Pure-resolver unit tests for the migration upgrade gate (cctally-dev#93).

No SQLite, no filesystem — the resolver is a pure function over a frozen
dataclass, so the D3 truth table IS the test matrix.
"""
import importlib.util
import pathlib
import sys

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parents[1] / "bin"
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))
_spec = importlib.util.spec_from_file_location("_cctally_db", _BIN_DIR / "_cctally_db.py")
_db = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass introspection (py3.14) can resolve the module.
sys.modules.setdefault("_cctally_db", _db)
_spec.loader.exec_module(_db)

UpgradeGateInputs = _db.UpgradeGateInputs
GateAction = _db.GateAction
resolve_upgrade_gate = _db.resolve_upgrade_gate


def _inp(**kw):
    base = dict(
        cache_001_state="applied",
        walk_complete_since_001=True,
        cache_has_entries=True,
        caller_has_historical_rows=True,
        disk_state="jsonl_present",
        marker_state_readable=True,
    )
    base.update(kw)
    return UpgradeGateInputs(**base)


def test_row1_unreadable_defers():
    assert resolve_upgrade_gate(_inp(marker_state_readable=False)).action is GateAction.DEFER


def test_row2_pending_defers():
    assert resolve_upgrade_gate(_inp(cache_001_state="pending")).action is GateAction.DEFER


def test_row3_skipped_with_history_defers():
    r = resolve_upgrade_gate(_inp(cache_001_state="skipped", caller_has_historical_rows=True))
    assert r.action is GateAction.DEFER


def test_row4_skipped_no_history_proceeds():
    r = resolve_upgrade_gate(_inp(cache_001_state="skipped", caller_has_historical_rows=False))
    assert r.action is GateAction.PROCEED


def test_row5_applied_no_history_proceeds():
    r = resolve_upgrade_gate(_inp(caller_has_historical_rows=False,
                                  walk_complete_since_001=False, cache_has_entries=False))
    assert r.action is GateAction.PROCEED


def test_row6_complete_nonempty_proceeds():
    assert resolve_upgrade_gate(_inp(walk_complete_since_001=True, cache_has_entries=True)).action is GateAction.PROCEED


def test_row7_partial_walk_defers():
    # round-3 residual: entries present but walk incomplete
    assert resolve_upgrade_gate(_inp(walk_complete_since_001=False, cache_has_entries=True)).action is GateAction.DEFER


def test_row7_empty_cache_after_rebuild_defers():
    # P1#1: complete walk but empty cache (rebuild over pruned disk)
    assert resolve_upgrade_gate(_inp(walk_complete_since_001=True, cache_has_entries=False)).action is GateAction.DEFER


@pytest.mark.parametrize("disk_state", ["jsonl_present", "pruned", "absent"])
def test_row7_reason_branches_on_disk_state(disk_state):
    r = resolve_upgrade_gate(_inp(walk_complete_since_001=False, cache_has_entries=True, disk_state=disk_state))
    assert r.action is GateAction.DEFER
    assert r.reason  # non-empty, human-readable


@pytest.mark.parametrize("s", ["applied", "skipped", "pending"])
@pytest.mark.parametrize("walk", [True, False])
@pytest.mark.parametrize("entries", [True, False])
@pytest.mark.parametrize("hist", [True, False])
@pytest.mark.parametrize("readable", [True, False])
def test_total_coverage_no_crash_and_binary_action(s, walk, entries, hist, readable):
    r = resolve_upgrade_gate(_inp(cache_001_state=s, walk_complete_since_001=walk,
                                  cache_has_entries=entries, caller_has_historical_rows=hist,
                                  marker_state_readable=readable))
    assert r.action in (GateAction.PROCEED, GateAction.DEFER)
    assert isinstance(r.reason, str) and r.reason
