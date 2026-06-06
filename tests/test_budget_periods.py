"""Pure-function unit tests for the calendar period resolver
(``bin/_lib_budget.py::calendar_month_window`` / ``calendar_week_window``).

These functions are PURE (stdlib ``datetime``/``zoneinfo`` only, no I/O), so
they're loaded directly off ``_lib_budget`` with no script-load / path
redirection needed. They build a civil boundary in ``display.tz`` and return
UTC-normalized instants, so the kernel's elapsed-seconds math stays single-tz.

Coverage (spec §3, §7.1):
  - ``calendar_month_window``: mid-month, Dec→Jan year carry, Feb leap (2028)
    vs non-leap (2026), 31→30-day rollover, a non-UTC tz whose month-start
    lands at an offset instant (America/New_York → 04:00/05:00Z).
  - ``calendar_week_window``: Monday vs Sunday week start (``week_start_idx``),
    mid-week snap-back, and the DST spring-forward (167h) / fall-back (169h)
    weeks in America/New_York.
"""
import datetime as dt
import importlib.util
import pathlib
import sys

from zoneinfo import ZoneInfo

REPO = pathlib.Path(__file__).resolve().parent.parent
_BIN = REPO / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


def _load(name, path):
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


budget = _load("_lib_budget", REPO / "bin" / "_lib_budget.py")

UTC = dt.timezone.utc


# ── calendar_month_window ────────────────────────────────────────────────────


def test_month_window_utc_midmonth():
    now = dt.datetime(2026, 6, 15, 9, 30, tzinfo=UTC)
    start, end = budget.calendar_month_window(now, UTC)
    assert start == dt.datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    # UTC-normalized instants.
    assert start.tzinfo == UTC and end.tzinfo == UTC


def test_month_window_dec_to_jan_year_carry():
    now = dt.datetime(2026, 12, 20, tzinfo=UTC)
    start, end = budget.calendar_month_window(now, UTC)
    assert (start.year, start.month) == (2026, 12)
    assert (end.year, end.month) == (2027, 1)
    assert start == dt.datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    assert end == dt.datetime(2027, 1, 1, 0, 0, tzinfo=UTC)


def test_month_window_feb_leap_2028():
    """A leap-year February spans 29 days (Feb 1 → Mar 1)."""
    now = dt.datetime(2028, 2, 14, tzinfo=UTC)
    start, end = budget.calendar_month_window(now, UTC)
    assert start == dt.datetime(2028, 2, 1, 0, 0, tzinfo=UTC)
    assert end == dt.datetime(2028, 3, 1, 0, 0, tzinfo=UTC)
    assert (end - start) == dt.timedelta(days=29)


def test_month_window_feb_non_leap_2026():
    """A non-leap February spans 28 days — civil rollover, not a fixed delta."""
    now = dt.datetime(2026, 2, 14, tzinfo=UTC)
    start, end = budget.calendar_month_window(now, UTC)
    assert start == dt.datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    assert (end - start) == dt.timedelta(days=28)


def test_month_window_31_to_30_day_rollover():
    """A 31-day month (October) rolls to the 1st of November exactly — never a
    timedelta(days=30)."""
    now = dt.datetime(2026, 10, 20, tzinfo=UTC)
    start, end = budget.calendar_month_window(now, UTC)
    assert start == dt.datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    assert (end - start) == dt.timedelta(days=31)


def test_month_window_display_tz_offset_instant():
    """The civil boundary is built in display.tz, then returned as a UTC
    instant. 2026-06-01 00:00 EDT == 04:00Z."""
    tz = ZoneInfo("America/New_York")
    now = dt.datetime(2026, 6, 15, 12, tzinfo=UTC)
    start, end = budget.calendar_month_window(now, tz)
    assert start == dt.datetime(2026, 6, 1, 4, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    assert start.tzinfo == UTC and end.tzinfo == UTC


def test_month_window_display_tz_winter_offset_instant():
    """In winter EST is UTC-5, so 2026-01-01 00:00 local == 05:00Z — proving
    the offset is read from the civil boundary, not hardcoded."""
    tz = ZoneInfo("America/New_York")
    now = dt.datetime(2026, 1, 15, 12, tzinfo=UTC)
    start, end = budget.calendar_month_window(now, tz)
    assert start == dt.datetime(2026, 1, 1, 5, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 2, 1, 5, 0, tzinfo=UTC)


def test_month_window_display_tz_picks_local_month():
    """Late on the last day of a month, UTC may already be the next month while
    local is still the previous one. The window must follow the *local* civil
    month. 2026-06-30 23:30 America/New_York == 2026-07-01 03:30Z."""
    tz = ZoneInfo("America/New_York")
    now = dt.datetime(2026, 7, 1, 3, 30, tzinfo=UTC)  # still Jun 30 local
    start, end = budget.calendar_month_window(now, tz)
    assert start == dt.datetime(2026, 6, 1, 4, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 7, 1, 4, 0, tzinfo=UTC)


# ── calendar_week_window ─────────────────────────────────────────────────────


def test_week_window_monday_start():
    now = dt.datetime(2026, 6, 17, tzinfo=UTC)  # Wednesday
    start, end = budget.calendar_week_window(now, UTC, week_start_idx=0)  # Mon
    assert start == dt.datetime(2026, 6, 15, 0, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 6, 22, 0, 0, tzinfo=UTC)
    assert start.tzinfo == UTC and end.tzinfo == UTC


def test_week_window_sunday_start():
    now = dt.datetime(2026, 6, 17, tzinfo=UTC)  # Wednesday
    start, end = budget.calendar_week_window(now, UTC, week_start_idx=6)  # Sun
    # The Sunday on/before Wed Jun 17 is Jun 14.
    assert start == dt.datetime(2026, 6, 14, 0, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 6, 21, 0, 0, tzinfo=UTC)


def test_week_window_now_on_start_day_stays():
    """When ``now`` falls on the week-start weekday, the window starts that
    same midnight (diff == 0), not 7 days earlier."""
    now = dt.datetime(2026, 6, 15, 9, 0, tzinfo=UTC)  # Monday
    start, end = budget.calendar_week_window(now, UTC, week_start_idx=0)  # Mon
    assert start == dt.datetime(2026, 6, 15, 0, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 6, 22, 0, 0, tzinfo=UTC)


def test_week_window_midweek_snaps_back():
    now = dt.datetime(2026, 6, 19, 23, 59, tzinfo=UTC)  # Friday late
    start, end = budget.calendar_week_window(now, UTC, week_start_idx=0)  # Mon
    assert start == dt.datetime(2026, 6, 15, 0, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 6, 22, 0, 0, tzinfo=UTC)


def test_week_window_dst_spring_forward_is_167h():
    """The America/New_York week containing the 2026-03-08 spring-forward is a
    true 167h span (one hour lost), because the 7-day delta is added to the
    *aware local* start before normalizing to UTC."""
    tz = ZoneInfo("America/New_York")
    now = dt.datetime(2026, 3, 10, 12, tzinfo=UTC)  # week containing Mar 8
    start, end = budget.calendar_week_window(now, tz, week_start_idx=6)  # Sun
    assert (end - start) == dt.timedelta(hours=167)
    # Sun Mar 8 00:00 EST == 05:00Z; the next Sun Mar 15 00:00 EDT == 04:00Z.
    assert start == dt.datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 3, 15, 4, 0, tzinfo=UTC)


def test_week_window_dst_fall_back_is_169h():
    """The America/New_York week containing the 2026-11-01 fall-back is a true
    169h span (one hour gained)."""
    tz = ZoneInfo("America/New_York")
    now = dt.datetime(2026, 11, 3, 12, tzinfo=UTC)  # week containing Nov 1
    start, end = budget.calendar_week_window(now, tz, week_start_idx=6)  # Sun
    assert (end - start) == dt.timedelta(hours=169)
    # Sun Nov 1 00:00 EDT == 04:00Z; the next Sun Nov 8 00:00 EST == 05:00Z.
    assert start == dt.datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 11, 8, 5, 0, tzinfo=UTC)


def test_week_window_is_pure_no_mutation_of_now():
    """The function must not mutate its inputs (datetimes are immutable, but
    this guards against an accidental in-place pattern)."""
    now = dt.datetime(2026, 6, 17, 8, 0, tzinfo=UTC)
    before = now
    budget.calendar_week_window(now, UTC, week_start_idx=0)
    assert now == before
