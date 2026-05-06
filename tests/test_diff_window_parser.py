"""Tests for diff window-token grammar (`_parse_diff_window`)."""
import datetime as dt
import pytest

from conftest import load_script


def _ns():
    return load_script()


def _utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


def test_this_week_resolves_to_current_subscription_week():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    pw = parse("this-week", now_utc=now,
               anchor_resets_at=_utc("2026-04-26T07:00:00Z"),
               anchor_week_start=_utc("2026-04-19T07:00:00Z"),
               tz_name="Etc/UTC")
    assert pw.label == "this-week"
    assert pw.kind == "week"
    assert pw.start_utc == _utc("2026-04-19T07:00:00Z")
    assert pw.end_utc == now
    # Mid-week: end_utc < anchor_resets_at -> partial window, not aligned.
    assert pw.week_aligned is False
    assert pw.full_weeks_count == 0


def test_this_week_at_reset_instant_is_week_aligned():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    reset = _utc("2026-04-26T07:00:00Z")
    now = reset  # exactly at the reset instant; clamps to reset
    pw = parse("this-week", now_utc=now,
               anchor_resets_at=reset,
               anchor_week_start=_utc("2026-04-19T07:00:00Z"),
               tz_name="Etc/UTC")
    assert pw.end_utc == reset
    assert pw.week_aligned is True
    assert pw.full_weeks_count == 1


def test_this_week_mid_week_is_partial():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    pw = parse("this-week", now_utc=now,
               anchor_resets_at=_utc("2026-04-26T07:00:00Z"),
               anchor_week_start=_utc("2026-04-19T07:00:00Z"),
               tz_name="Etc/UTC")
    assert pw.week_aligned is False
    assert pw.full_weeks_count == 0


def test_last_week_resolves_to_previous_subscription_week():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    pw = parse("last-week", now_utc=now,
               anchor_resets_at=_utc("2026-04-26T07:00:00Z"),
               anchor_week_start=_utc("2026-04-19T07:00:00Z"),
               tz_name="Etc/UTC")
    assert pw.start_utc == _utc("2026-04-12T07:00:00Z")
    assert pw.end_utc == _utc("2026-04-19T07:00:00Z")
    assert abs(pw.length_days - 7.0) < 1e-9
    assert pw.week_aligned is True
    assert pw.full_weeks_count == 1


def test_nw_ago_resolves_to_subscription_week_n_back():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    pw = parse("3w-ago", now_utc=now,
               anchor_resets_at=_utc("2026-04-26T07:00:00Z"),
               anchor_week_start=_utc("2026-04-19T07:00:00Z"),
               tz_name="Etc/UTC")
    assert pw.start_utc == _utc("2026-03-29T07:00:00Z")
    assert pw.end_utc == _utc("2026-04-05T07:00:00Z")
    assert pw.week_aligned is True
    assert pw.full_weeks_count == 1


def test_this_month_resolves_to_calendar_month_in_local_tz():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    pw = parse("this-month", now_utc=now, anchor_resets_at=None,
               anchor_week_start=None, tz_name="Etc/UTC")
    assert pw.kind == "month"
    assert pw.start_utc == _utc("2026-04-01T00:00:00Z")
    assert pw.end_utc == _utc("2026-05-01T00:00:00Z")


def test_last_month_resolves_to_previous_calendar_month():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    pw = parse("last-month", now_utc=now, anchor_resets_at=None,
               anchor_week_start=None, tz_name="Etc/UTC")
    assert pw.start_utc == _utc("2026-03-01T00:00:00Z")
    assert pw.end_utc == _utc("2026-04-01T00:00:00Z")


def test_last_7d_is_rolling_window_to_now():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    pw = parse("last-7d", now_utc=now, anchor_resets_at=None,
               anchor_week_start=None, tz_name="Etc/UTC")
    assert pw.kind == "day-range"
    assert pw.end_utc == now
    assert pw.start_utc == now - dt.timedelta(days=7)


def test_prev_7d_is_window_before_last_7d():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    pw = parse("prev-7d", now_utc=now, anchor_resets_at=None,
               anchor_week_start=None, tz_name="Etc/UTC")
    assert pw.start_utc == now - dt.timedelta(days=14)
    assert pw.end_utc == now - dt.timedelta(days=7)


def test_explicit_range_is_inclusive_in_local_tz():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    pw = parse("2026-04-01..2026-04-15", now_utc=now,
               anchor_resets_at=None, anchor_week_start=None,
               tz_name="Etc/UTC")
    assert pw.kind == "explicit-range"
    assert pw.start_utc == _utc("2026-04-01T00:00:00Z")
    assert pw.end_utc == _utc("2026-04-16T00:00:00Z")


def test_explicit_range_rejects_reversed_dates():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    with pytest.raises(ValueError, match="range start must be on or before end"):
        parse("2026-04-15..2026-04-01", now_utc=now,
              anchor_resets_at=None, anchor_week_start=None,
              tz_name="Etc/UTC")


def test_no_anchor_raises_for_week_tokens():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    NoAnchorError = ns["NoAnchorError"]
    now = _utc("2026-04-25T19:30:00Z")
    with pytest.raises(NoAnchorError):
        parse("this-week", now_utc=now, anchor_resets_at=None,
              anchor_week_start=None, tz_name="Etc/UTC")


def test_unknown_token_raises_value_error():
    ns = _ns()
    parse = ns["_parse_diff_window"]
    now = _utc("2026-04-25T19:30:00Z")
    with pytest.raises(ValueError, match="invalid window token"):
        parse("bogus-token", now_utc=now, anchor_resets_at=None,
              anchor_week_start=None, tz_name="Etc/UTC")
