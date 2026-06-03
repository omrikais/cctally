import functools
import math
import operator
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _lib_fmt as fmt  # noqa: E402


def test_stable_sum_is_exact_not_naive():
    # Vector where naive left-to-right summation loses precision but the exact
    # (Shewchuk) result does not. Proves stable_sum is NOT a no-op over sum().
    v = [1e16, 1.0, -1e16, 1.0, 1.0, -1.0]
    naive = functools.reduce(operator.add, v)
    assert fmt.stable_sum(v) == math.fsum(v)
    assert fmt.stable_sum(v) != naive  # non-vacuous: exact != naive here


def test_stable_sum_empty_is_float_zero():
    # Empty -> 0.0 (float), unlike builtin sum(()) -> 0 (int).
    result = fmt.stable_sum([])
    assert result == 0.0
    assert isinstance(result, float)


def test_stable_sum_matches_fsum_on_costs():
    costs = [0.0123456789, 0.1, 0.2, 0.3, 1e-9, 2e-9]
    assert fmt.stable_sum(costs) == math.fsum(costs)
