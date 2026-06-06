"""DST-correctness of calendar-period window resolution under ``display.tz=local``
(issue #136).

Background
----------
``_cctally_forecast._resolve_calendar_window`` turns a calendar ``budget`` /
``codex_budget`` / ``projected`` period into a ``(start_utc, end_utc)`` window.
For an explicit ``display.tz`` (``utc`` / IANA) it hands the pure kernels
(``calendar_month_window`` / ``calendar_week_window``) a real DST-aware
``ZoneInfo``, which is already correct — proven by ``test_budget_periods.py``'s
167h / 169h DST-span tests.

The ``display.tz=local`` case (the default, ``tz is None``) is the bug this
module guards. The pre-#136 code mapped it to ``datetime.now().astimezone()
.tzinfo`` — a *fixed-offset* ``datetime.timezone`` captured at the wall clock —
and fed that single offset to the kernel. When a civil period straddles a DST
transition, the period-start local midnight is then converted to UTC at the
*wrong* offset, so the SAME civil month/week resolves to two different
``period_start_at`` values before vs after the transition. That:

  1. shifts the ``[start_utc, now]`` spend window by the DST delta (~1h), and
  2. drifts the ``UNIQUE(period_start_at, threshold)`` milestone key, re-firing
     already-crossed thresholds.

The fix gives the local case a per-instant resolution path (mirroring
``_period_label_local``): build the naive local civil boundaries and convert
each via a bare ``astimezone()`` so each boundary picks up the offset in effect
at *its own* wall-clock instant — stable regardless of where ``now`` falls
relative to the transition, and with NO dependency on the real wall clock.

Determinism
-----------
Every assertion here depends only on the injected ``now_utc`` and the
process ``TZ`` (set per test, restored by conftest's autouse
``_restore_process_timezone``). The fix removes the ``datetime.now()`` read
entirely, so the fixed values below hold in every real-world season. (Under the
pre-fix code the result tracked the real-now offset, so at least one of the
spring-forward / fall-back cases drifts off these values in any given season —
see ``test_fixed_offset_resolution_would_drift_nonvacuity`` for the explicit
non-vacuity proof.)
"""
from __future__ import annotations

import datetime as dt
import time

import pytest
from zoneinfo import ZoneInfo

from conftest import load_script

UTC = dt.timezone.utc
NYC = ZoneInfo("America/New_York")


@pytest.fixture
def ns():
    return load_script()


def _set_tz(monkeypatch, zone: str) -> None:
    """Pin the process zone so bare ``astimezone()`` observes it.

    conftest's autouse ``_restore_process_timezone`` reverts the libc tz state
    at teardown, so this can't leak into sibling tests under pytest-xdist.
    """
    monkeypatch.setenv("TZ", zone)
    if hasattr(time, "tzset"):
        time.tzset()


def _local_window(ns, period, now_utc, *, week_start="sunday"):
    """Resolve a calendar window in the ``display.tz=local`` (``tz=None``) case."""
    config = {"collector": {"week_start": week_start}}
    return ns["_resolve_calendar_window"](period, now_utc, config, None)


# (id, period, week_start, now_before_utc, now_after_utc, expected_start_utc, expected_end_utc)
#
# Each pair of `now` instants straddles a DST transition WITHIN one civil
# period, so a correct resolver returns the SAME window for both. Expected
# values verified against ZoneInfo("America/New_York"):
#   2026 spring-forward: Sun Mar 8 02:00 EST → 03:00 EDT  (offset −5 → −4)
#   2026 fall-back:      Sun Nov 1 02:00 EDT → 01:00 EST  (offset −4 → −5)
_DST_LOCAL_CASES = [
    pytest.param(
        "calendar-month", "sunday",
        dt.datetime(2026, 3, 5, 17, 0, tzinfo=UTC),   # Mar 5 12:00 EST (pre)
        dt.datetime(2026, 3, 20, 16, 0, tzinfo=UTC),  # Mar 20 12:00 EDT (post)
        dt.datetime(2026, 3, 1, 5, 0, tzinfo=UTC),    # Mar 1 00:00 EST → 05:00Z
        dt.datetime(2026, 4, 1, 4, 0, tzinfo=UTC),    # Apr 1 00:00 EDT → 04:00Z
        id="month-spring-forward",
    ),
    pytest.param(
        "calendar-month", "sunday",
        dt.datetime(2026, 11, 1, 4, 30, tzinfo=UTC),  # Nov 1 00:30 EDT (pre)
        dt.datetime(2026, 11, 20, 17, 0, tzinfo=UTC),  # Nov 20 12:00 EST (post)
        dt.datetime(2026, 11, 1, 4, 0, tzinfo=UTC),   # Nov 1 00:00 EDT → 04:00Z
        dt.datetime(2026, 12, 1, 5, 0, tzinfo=UTC),   # Dec 1 00:00 EST → 05:00Z
        id="month-fall-back",
    ),
    pytest.param(
        "calendar-week", "sunday",
        dt.datetime(2026, 3, 8, 6, 0, tzinfo=UTC),    # Mar 8 01:00 EST (pre)
        dt.datetime(2026, 3, 10, 16, 0, tzinfo=UTC),  # Mar 10 12:00 EDT (post)
        dt.datetime(2026, 3, 8, 5, 0, tzinfo=UTC),    # Sun Mar 8 00:00 EST → 05:00Z
        dt.datetime(2026, 3, 15, 4, 0, tzinfo=UTC),   # Sun Mar 15 00:00 EDT → 04:00Z
        id="week-spring-forward",
    ),
    pytest.param(
        "calendar-week", "sunday",
        dt.datetime(2026, 11, 1, 4, 30, tzinfo=UTC),  # Nov 1 00:30 EDT (pre)
        dt.datetime(2026, 11, 3, 17, 0, tzinfo=UTC),  # Nov 3 12:00 EST (post)
        dt.datetime(2026, 11, 1, 4, 0, tzinfo=UTC),   # Sun Nov 1 00:00 EDT → 04:00Z
        dt.datetime(2026, 11, 8, 5, 0, tzinfo=UTC),   # Sun Nov 8 00:00 EST → 05:00Z
        id="week-fall-back",
    ),
]


@pytest.mark.parametrize(
    "period,wk,before,after,exp_start,exp_end", _DST_LOCAL_CASES
)
def test_local_calendar_window_stable_across_dst(
    ns, monkeypatch, period, wk, before, after, exp_start, exp_end
):
    """The SAME civil period resolves to ONE window regardless of where ``now``
    falls relative to an in-period DST transition (issue #136 impact #2)."""
    _set_tz(monkeypatch, "America/New_York")

    start_before, end_before = _local_window(ns, period, before, week_start=wk)
    start_after, end_after = _local_window(ns, period, after, week_start=wk)

    # The re-fire defense: period_start_at must not drift across the transition.
    assert start_before == start_after, (
        f"{period} period_start_at drifted across the DST transition: "
        f"{start_before.isoformat()} (pre) != {start_after.isoformat()} (post)"
    )
    assert end_before == end_after

    # And both must equal the DST-correct civil boundary (offset in effect AT
    # the boundary, not at now).
    assert start_before == exp_start
    assert end_before == exp_end


@pytest.mark.parametrize(
    "now_utc",
    [
        dt.datetime(2026, 3, 5, 17, 0, tzinfo=UTC),
        dt.datetime(2026, 3, 20, 16, 0, tzinfo=UTC),
        dt.datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
        dt.datetime(2026, 11, 20, 17, 0, tzinfo=UTC),
    ],
)
def test_local_month_matches_explicit_zoneinfo(ns, monkeypatch, now_utc):
    """Under ``TZ=America/New_York``, the local (``tz=None``) month window equals
    the explicit ``ZoneInfo("America/New_York")`` kernel result — tying the local
    path to the proven-correct explicit path."""
    _set_tz(monkeypatch, "America/New_York")
    local = _local_window(ns, "calendar-month", now_utc)
    explicit = ns["calendar_month_window"](now_utc, NYC)
    assert local == explicit


@pytest.mark.parametrize(
    "now_utc",
    [
        dt.datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
        dt.datetime(2026, 3, 10, 16, 0, tzinfo=UTC),
        dt.datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
        dt.datetime(2026, 11, 3, 17, 0, tzinfo=UTC),
    ],
)
def test_local_week_matches_explicit_zoneinfo(ns, monkeypatch, now_utc):
    """Same tie-back for the Sunday-anchored calendar week."""
    _set_tz(monkeypatch, "America/New_York")
    local = _local_window(ns, "calendar-week", now_utc, week_start="sunday")
    explicit = ns["calendar_week_window"](now_utc, NYC, 6)  # Sun=6
    assert local == explicit


def test_local_calendar_window_non_dst_zone_stable(ns, monkeypatch):
    """A real zone with no DST (``America/Phoenix``, fixed −7) is trivially
    stable and lands on the constant offset — a regression guard that the new
    local path doesn't disturb fixed-offset real zones."""
    _set_tz(monkeypatch, "America/Phoenix")
    before = dt.datetime(2026, 3, 5, 17, 0, tzinfo=UTC)
    after = dt.datetime(2026, 3, 20, 16, 0, tzinfo=UTC)

    start_before, end_before = _local_window(ns, "calendar-month", before)
    start_after, end_after = _local_window(ns, "calendar-month", after)

    assert (start_before, end_before) == (start_after, end_after)
    # Mar 1 00:00 MST (−7) → 07:00Z; Apr 1 00:00 MST (−7) → 07:00Z.
    assert start_before == dt.datetime(2026, 3, 1, 7, 0, tzinfo=UTC)
    assert end_before == dt.datetime(2026, 4, 1, 7, 0, tzinfo=UTC)


def test_fixed_offset_resolution_would_drift_nonvacuity(ns, monkeypatch):
    """Non-vacuity guard: reproduce the pre-#136 fixed-offset resolution and
    show it yields two DIFFERENT period starts for one civil month — proving the
    stability assertions above genuinely exercise DST sensitivity rather than
    passing trivially. This tests a *reconstruction* of the old behavior, so it
    holds independent of the production fix."""
    _set_tz(monkeypatch, "America/New_York")
    before = dt.datetime(2026, 3, 5, 17, 0, tzinfo=UTC)   # EST −5
    after = dt.datetime(2026, 3, 20, 16, 0, tzinfo=UTC)   # EDT −4

    def fixed_offset_start(now_utc):
        # The pre-fix shape: capture a single fixed offset and feed it to the
        # pure kernel (here keyed off now_utc's own offset, the faithful
        # per-tick analogue of the old datetime.now()-captured offset).
        fixed = now_utc.astimezone().tzinfo
        return ns["calendar_month_window"](now_utc, fixed)[0]

    assert fixed_offset_start(before) != fixed_offset_start(after)
