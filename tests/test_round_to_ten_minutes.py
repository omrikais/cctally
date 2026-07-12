"""Unit tests for ``_lib_five_hour._round_to_ten_minutes``.

The display-side companion to ``_floor_to_ten_minutes``: Anthropic 5h
reset times carry sub-10-minute capture jitter, so a true ``:40``
boundary can be recorded as ``:39``. Flooring would show ``:30`` — for
*display* we round to the NEAREST 10-minute boundary so the shown clock
time matches the real reset. Internal timestamps stay exact (this helper
is never used for keys / partitioning — see issue #76).
"""
import datetime as dt
import sys
import pathlib

# Add bin/ to path so `import _lib_five_hour` resolves.
_BIN = str(pathlib.Path(__file__).resolve().parent.parent / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from _lib_five_hour import _round_to_ten_minutes  # noqa: E402


def _utc(y, mo, d, h, mi, s=0):
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone.utc)


def test_rounds_jitter_below_boundary_up():
    # The load-bearing case: 04:39:59 is a jittered :40 boundary.
    assert _round_to_ten_minutes(_utc(2026, 4, 15, 4, 39, 59)) == _utc(
        2026, 4, 15, 4, 40
    )
    assert _round_to_ten_minutes(_utc(2026, 7, 11, 10, 39)) == _utc(
        2026, 7, 11, 10, 40
    )


def test_rounds_down_when_nearer_lower_boundary():
    assert _round_to_ten_minutes(_utc(2026, 4, 15, 10, 34)) == _utc(
        2026, 4, 15, 10, 30
    )
    assert _round_to_ten_minutes(_utc(2026, 4, 15, 10, 41)) == _utc(
        2026, 4, 15, 10, 40
    )


def test_half_rounds_up():
    # Exactly :X5:00 → up (predictable half-up, not banker's).
    assert _round_to_ten_minutes(_utc(2026, 4, 15, 10, 35)) == _utc(
        2026, 4, 15, 10, 40
    )
    assert _round_to_ten_minutes(_utc(2026, 4, 15, 10, 45)) == _utc(
        2026, 4, 15, 10, 50
    )


def test_idempotent_on_boundary():
    for mi in (0, 10, 20, 30, 40, 50):
        d = _utc(2026, 4, 15, 10, mi)
        assert _round_to_ten_minutes(d) == d


def test_hour_and_day_rollover():
    assert _round_to_ten_minutes(_utc(2026, 4, 15, 10, 56)) == _utc(
        2026, 4, 15, 11, 0
    )
    assert _round_to_ten_minutes(_utc(2026, 4, 15, 23, 57)) == _utc(
        2026, 4, 16, 0, 0
    )


def test_naive_input_treated_as_utc():
    naive = dt.datetime(2026, 4, 15, 4, 39, 59)
    assert _round_to_ten_minutes(naive) == _utc(2026, 4, 15, 4, 40)


def test_rounds_the_absolute_instant_tz_independent():
    # 07:39:59+03:00 == 04:39:59Z → rounds the *instant* to 04:40:00Z.
    aware = dt.datetime(
        2026, 4, 15, 7, 39, 59,
        tzinfo=dt.timezone(dt.timedelta(hours=3)),
    )
    assert _round_to_ten_minutes(aware) == _utc(2026, 4, 15, 4, 40)
