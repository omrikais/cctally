"""Tests for the pure browse-filter date-range parser (spec §2, Task 2A).

``_lib_dashboard_dates.parse_filter_date_range`` is the argparse-decoupled
replacement for the CLI's ``_parse_cli_date_range``: it maps a (date_from,
date_to) pair to a HALF-OPEN UTC-ISO interval ``[start, end)`` whose bounds
lexicographically compare correctly against the stored mixed-precision
``last_activity_utc`` (whole-second AND millisecond ``...Z``). Bounds are
6-digit-microsecond ``...THH:MM:SS.000000Z`` strings: an INCLUSIVE start-of-day
lower bound and an EXCLUSIVE start-of-NEXT-day upper bound (the SQL compares
``>= start`` and ``< end``). Naive date-only inputs localize in ``display.tz``;
an offset-less ``...THH:MM:SS`` is a date-only day bound (its naive time is NOT
used — review Finding 3); garbage raises ``ValueError`` (→ HTTP 400).
"""
import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))


def test_parse_filter_date_range_month_utc():
    """Half-open interval: inclusive start-of-day lower bound, EXCLUSIVE
    start-of-NEXT-day upper bound (06-30 -> 07-01 midnight), both 6-digit-micro."""
    m = importlib.import_module("_lib_dashboard_dates")
    start, end = m.parse_filter_date_range("2026-06-01", "2026-06-30", tz_name="Etc/UTC")
    assert start == "2026-06-01T00:00:00.000000Z"
    assert end == "2026-07-01T00:00:00.000000Z"


def test_parse_filter_date_range_open_ended():
    m = importlib.import_module("_lib_dashboard_dates")
    start, end = m.parse_filter_date_range("2026-06-15", None, tz_name="Etc/UTC")
    assert start == "2026-06-15T00:00:00.000000Z" and end is None


def test_parse_filter_date_range_rejects_garbage():
    m = importlib.import_module("_lib_dashboard_dates")
    import pytest
    with pytest.raises(ValueError):
        m.parse_filter_date_range("not-a-date", None, tz_name="Etc/UTC")


def test_parse_filter_date_range_both_none():
    m = importlib.import_module("_lib_dashboard_dates")
    assert m.parse_filter_date_range(None, None, tz_name="Etc/UTC") == (None, None)


def test_parse_filter_date_range_localizes_in_tz():
    """A naive date-only bound is interpreted in display.tz, then converted to
    UTC for the stored-UTC comparison. New York is UTC-4 on 2026-06-15 (EDT),
    so start-of-day there is 04:00Z."""
    m = importlib.import_module("_lib_dashboard_dates")
    start, end = m.parse_filter_date_range(
        "2026-06-15", "2026-06-15", tz_name="America/New_York")
    assert start == "2026-06-15T04:00:00.000000Z"
    # Half-open EXCLUSIVE upper: start-of-NEXT-day (06-16) midnight EDT -> the
    # following 04:00Z, so `< end` covers all of the requested 06-15 (EDT) day.
    assert end == "2026-06-16T04:00:00.000000Z"


def test_parse_filter_full_iso_carries_offset():
    """A full-ISO bound carries its own explicit offset and bypasses the
    dual-form parse (tz-independent), matching the CLI's full-ISO posture.
    The lower bound is the precise instant, micro-formatted."""
    m = importlib.import_module("_lib_dashboard_dates")
    start, _ = m.parse_filter_date_range(
        "2026-06-15T12:00:00Z", None, tz_name="America/New_York")
    assert start == "2026-06-15T12:00:00.000000Z"


def test_parse_filter_offsetless_t_is_date_only_day_bound():
    """Finding 3: an offset-less ``...THH:MM:SS`` carries NO explicit offset, so
    it is treated as a date-only DAY bound — its naive ``08:30:00`` time is NOT
    silently used as a sub-day cut. The lower bound is start-of-day in
    display.tz, the upper is start-of-next-day (half-open), both micro-formatted.
    """
    m = importlib.import_module("_lib_dashboard_dates")
    start, end = m.parse_filter_date_range(
        "2026-06-15T08:30:00", "2026-06-15T08:30:00", tz_name="Etc/UTC")
    # Time component dropped: start-of-day, NOT 08:30.
    assert start == "2026-06-15T00:00:00.000000Z"
    assert end == "2026-06-16T00:00:00.000000Z"
