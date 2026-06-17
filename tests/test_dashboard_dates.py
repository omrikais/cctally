"""Tests for the pure browse-filter date-range parser (spec §2, Task 2A).

``_lib_dashboard_dates.parse_filter_date_range`` is the argparse-decoupled
replacement for the CLI's ``_parse_cli_date_range``: it maps a (date_from,
date_to) pair to UTC-ISO boundary strings that lexicographically compare against
the stored ``last_activity_utc`` format (``...T00:00:00Z`` — ``Z`` suffix, whole
seconds when no microseconds), localizing naive date-only inputs in ``display.tz``
(start-of-day for the lower bound, end-of-day for the upper), and raises
``ValueError`` (→ HTTP 400) on garbage.
"""
import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))


def test_parse_filter_date_range_month_utc():
    m = importlib.import_module("_lib_dashboard_dates")
    start, end = m.parse_filter_date_range("2026-06-01", "2026-06-30", tz_name="Etc/UTC")
    assert start == "2026-06-01T00:00:00Z"
    assert end == "2026-06-30T23:59:59.999999Z"


def test_parse_filter_date_range_open_ended():
    m = importlib.import_module("_lib_dashboard_dates")
    start, end = m.parse_filter_date_range("2026-06-15", None, tz_name="Etc/UTC")
    assert start == "2026-06-15T00:00:00Z" and end is None


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
    assert start == "2026-06-15T04:00:00Z"
    # End-of-day 23:59:59.999999 EDT -> 03:59:59.999999Z the NEXT calendar day.
    assert end == "2026-06-16T03:59:59.999999Z"


def test_parse_filter_full_iso_carries_offset():
    """A full-ISO bound carries its own offset and bypasses the dual-form parse
    (tz-independent), matching the CLI's full-ISO posture."""
    m = importlib.import_module("_lib_dashboard_dates")
    start, _ = m.parse_filter_date_range(
        "2026-06-15T12:00:00Z", None, tz_name="America/New_York")
    assert start == "2026-06-15T12:00:00Z"
