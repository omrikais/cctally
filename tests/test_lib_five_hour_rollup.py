"""Direct unit tests for _lib_five_hour.five_hour_milestone_range (#279 S4 F4).

The 5h-% milestone-range decision (which integer thresholds to attempt)
moved out of maybe_update_five_hour_block's detection loop into the pure
_lib_five_hour home. These pin the exact fencing found in the loop: the
1e-9 floor snap, the first-observation-only-current-floor rule
(max_existing is None), the resume-above-max rule, and the empty-when-
covered case.
"""
from __future__ import annotations

import sys
import pathlib

# Add bin/ to path so `import _lib_five_hour` resolves.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

from _lib_five_hour import five_hour_milestone_range


def test_range_first_observation_records_only_current_floor():
    # max_existing None => start_threshold == current_floor (NO 1..floor-1
    # backfill). floor(3.2 + 1e-9) == 3.
    assert list(five_hour_milestone_range(3.2, None)) == [3]


def test_range_resumes_above_max_existing():
    assert list(five_hour_milestone_range(7.0, 5)) == [6, 7]


def test_range_empty_when_already_at_max():
    # floor 5, start 6 > 5 => empty.
    assert list(five_hour_milestone_range(5.0, 5)) == []


def test_range_empty_when_below_one():
    # current_floor < 1 => empty (mirrors the `if current_floor >= 1` guard).
    assert list(five_hour_milestone_range(0.5, None)) == []
    assert list(five_hour_milestone_range(0.0, None)) == []


def test_floor_snaps_ulp():
    # 0.57 * 100 == 56.99999999999999; the 1e-9 snap floors to 57, not 56.
    assert list(five_hour_milestone_range(0.5699999999999999 * 100, 56)) == [57]


def test_multi_threshold_catchup_from_max():
    # First observation jump: max_existing None, floor 4 => only [4].
    assert list(five_hour_milestone_range(4.0, None)) == [4]
    # With a prior max of 1, floor 4 => catch up [2, 3, 4].
    assert list(five_hour_milestone_range(4.0, 1)) == [2, 3, 4]


def test_range_start_is_start_threshold():
    # The glue reads `milestone_range.start` for its `pct == start_threshold`
    # marginal-cost check — pin that the range object carries it.
    r = five_hour_milestone_range(7.0, 5)
    assert r.start == 6
