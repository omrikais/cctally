"""Regression: the Projects-panel trend ranking is stable against ULP tie-flips.

`_build_projects_envelope` (bin/_cctally_dashboard.py:~3434) ranks projects in
the ``trend.projects`` array by descending total window cost, ties broken by
``key``::

    trend_projects.sort(
        key=lambda p: (-stable_sum(p["weekly_cost"]), p["key"]),
    )

The cost component goes through ``stable_sum`` (``math.fsum`` — exactly-rounded,
interpreter-stable Shewchuk summation) rather than the built-in ``sum()``.
Built-in ``sum()`` over floats is *not* interpreter-stable: CPython switched it
to Neumaier compensated summation in 3.12, and a plain left-to-right reduce can
absorb tiny addends entirely. Either way, two projects whose costs are a genuine
near-tie could silently reorder on the chart depending on the interpreter or the
summation algorithm.

This test pins the ordering against a deliberately constructed near-tie so a
future regression (reverting the ranking key to a non-stable sum) is caught.

THE NEAR-TIE VECTORS
--------------------
Let ``eps = 2**-52`` (the ULP of 1.0, ~2.22e-16) and ``half = eps / 2`` (i.e.
``2**-53``, exactly the round-half boundary above 1.0). Then:

  PROJ_HI weekly_cost = [1.0, half, half]   exact total = 1.0 + eps
  PROJ_LO weekly_cost = [1.0, half]         exact total = 1.0 + half

  * ``stable_sum`` / ``math.fsum`` returns the *exactly-rounded* result:
        fsum(HI) == 1.0000000000000002   (= 1.0 + one ULP)
        fsum(LO) == 1.0                    (1.0 + half rounds to 1.0, ties-to-even)
    so the order is DEFINITE and deterministic: HI > LO, separated by 1 ULP.

  * A naive left-to-right ``reduce(add)`` absorbs each ``half`` into ``1.0``
    (``1.0 + 2**-53`` rounds back to ``1.0`` under round-half-to-even), so:
        naive(HI) == 1.0  ==  naive(LO) == 1.0
    -> the cost component TIES, and ordering collapses to the secondary ``key``.

We assign the keys so the secondary tie-break would put the *wrong* project
first if the cost component ever ties: PROJ_HI is given the lexicographically
LARGER key ("zzz-hi"), PROJ_LO the smaller ("aaa-lo"). Under the correct
``stable_sum`` key, HI still ranks first (its cost is a true ULP above LO). Under
a regressed naive-``sum`` key, the costs tie and the ascending ``key`` tie-break
puts "aaa-lo" (LO) first -> the order FLIPS. That flip is exactly what this test
forbids, which is what makes it non-vacuous (see ``test_*_naive_sum_would_flip``).
"""
from __future__ import annotations

import functools
import math
import operator
import pathlib
import sys

from conftest import load_script  # noqa: E402

# Load the main script first so `sys.modules["cctally"]` is registered — the
# dashboard module reads `sys.modules["cctally"].BLOCK_DURATION` at import time.
_NS = load_script()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _cctally_dashboard  # noqa: E402


# --- The near-tie vectors (see module docstring for the construction) ------
_EPS = 2.0 ** -52      # ULP of 1.0
_HALF = _EPS / 2.0     # 2**-53 — the round-half boundary just above 1.0

# fsum(HI) == 1.0 + 1 ULP ; fsum(LO) == 1.0  -> HI strictly ranks above LO.
# Keys chosen so the secondary tie-break ORDER OPPOSES the cost order: if the
# cost component ever ties (naive sum), the ascending-key tie-break would put LO
# first and flip the ranking.
_PROJ_HI = {"key": "zzz-hi", "weekly_cost": [1.0, _HALF, _HALF]}
_PROJ_LO = {"key": "aaa-lo", "weekly_cost": [1.0, _HALF]}


def _production_sort_key(p):
    """The EXACT ranking key used by ``_build_projects_envelope``.

    Resolves ``stable_sum`` off the production module object so a regression in
    the imported chokepoint (or the import being swapped back to ``sum``) is
    reflected here, not masked by a private copy of the symbol.
    """
    return (-_cctally_dashboard.stable_sum(p["weekly_cost"]), p["key"])


def test_near_tie_vectors_are_a_genuine_one_ulp_tie():
    """Guard the construction itself: stable_sum separates the two costs by
    exactly one ULP, while a naive left-to-right reduce collapses them to an
    exact tie. If this ever stops holding the regression test below is vacuous.
    """
    hi = _cctally_dashboard.stable_sum(_PROJ_HI["weekly_cost"])
    lo = _cctally_dashboard.stable_sum(_PROJ_LO["weekly_cost"])
    # Exactly one ULP apart, definite order.
    assert hi == math.fsum(_PROJ_HI["weekly_cost"]) == 1.0000000000000002
    assert lo == math.fsum(_PROJ_LO["weekly_cost"]) == 1.0
    assert hi > lo
    assert math.nextafter(lo, math.inf) == hi  # adjacent doubles: 1-ULP gap

    naive_hi = functools.reduce(operator.add, _PROJ_HI["weekly_cost"], 0.0)
    naive_lo = functools.reduce(operator.add, _PROJ_LO["weekly_cost"], 0.0)
    assert naive_hi == naive_lo == 1.0  # naive summation ties them


def test_trend_ranking_is_stable_sum_order():
    """The production ranking key orders PROJ_HI before PROJ_LO.

    Even though PROJ_LO sorts first by the secondary ``key`` tie-break, its true
    cost is one ULP below PROJ_HI, so the stable_sum cost component wins.
    """
    # Feed in LO-first to prove the sort actually reorders (not input order).
    projects = [dict(_PROJ_LO), dict(_PROJ_HI)]
    projects.sort(key=_production_sort_key)
    assert [p["key"] for p in projects] == ["zzz-hi", "aaa-lo"]


def test_naive_sum_would_flip_the_ranking():
    """Non-vacuity proof: a regressed naive-``sum`` ranking key REORDERS.

    Demonstrates the test above can actually fail if the ranking key stops being
    exact: under a left-to-right reduce the two costs tie, and the ascending
    ``key`` tie-break puts PROJ_LO ("aaa-lo") first -> the order flips relative
    to the stable_sum order asserted above.
    """
    def _naive_key(p):
        return (-functools.reduce(operator.add, p["weekly_cost"], 0.0), p["key"])

    projects = [dict(_PROJ_LO), dict(_PROJ_HI)]
    projects.sort(key=_naive_key)
    # The FLIP: LO first under naive summation, vs HI-first under stable_sum.
    assert [p["key"] for p in projects] == ["aaa-lo", "zzz-hi"]


def test_production_module_uses_stable_sum_for_ranking():
    """Belt-and-suspenders: the production module must expose ``stable_sum``
    (the math.fsum chokepoint) — the symbol the ranking key resolves at call
    time. If the import were reverted to the built-in ``sum``, this fails.
    """
    assert _cctally_dashboard.stable_sum is math.fsum or (
        _cctally_dashboard.stable_sum(_PROJ_HI["weekly_cost"])
        == math.fsum(_PROJ_HI["weekly_cost"])
    )
