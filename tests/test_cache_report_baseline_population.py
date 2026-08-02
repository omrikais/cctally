"""The published baseline count and the median share one population (#443 S3 F22).

Reproduced on the ORDINARY path — ``display_tz`` fully resolved, not the
``display_tz=None`` direct-caller path. Row anchors are calendar-date
midnights that follow DST (``_row_anchor`` uses ``.astimezone()``
precisely so each date gets its own correct offset), while the baseline
bounds were elapsed ``timedelta(days=N)`` INSTANTS. Across a transition
the two disagree by the offset change, so a row exactly N calendar days
old fell one hour outside an N-day elapsed window:

    host Asia/Jerusalem, display Etc/UTC, now 2026-10-30T12:00Z
    today_anchor = 2026-10-29T22:00Z   lower_bound = 2026-10-15T22:00Z
    2026-10-16 anchors at 2026-10-15T21:00Z  (IDT +3, pre-transition) -> EXCLUDED

    rows_excluding_today  = 5     published as the sample count
    today_baseline_median = None  fewer than 5 admitted

The panel then read "baseline sufficient" from the count while the median
was absent. Both halves are fixed here: daily-mode windowing becomes
CALENDAR-DATE based (so half one removes the DST drop), and the published
count is derived from the rows the median actually admitted (so half two
makes the contradiction unrepresentable rather than merely absent).

Session mode keeps elapsed-time windowing on purpose: session anchors are
real timestamps, not date midnights, so elapsed windowing is correct there.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
import time
from zoneinfo import ZoneInfo

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_BIN = ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


@pytest.fixture
def host_tz_jerusalem():
    prior = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Jerusalem"
    time.tzset()
    yield
    if prior is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = prior
    time.tzset()


DATES = ["2026-10-16", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
NOW = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)


def _rows(crk):
    rows = []
    for iso in DATES:
        r = crk.CacheRow(date=iso)
        r.input_tokens, r.cache_read_tokens = 400, 600
        rows.append(r)
    return rows


def test_the_fixture_actually_straddles_a_dst_transition(host_tz_jerusalem):
    """Non-vacuity: without an offset change inside the window, nothing drops.

    Israel leaves IDT (+3) for IST (+2) on 2026-10-25, so the oldest row's
    calendar midnight and the focal day's sit at different UTC offsets.
    If the tzdb ever moved that transition, every test below would go
    inert rather than fail.
    """
    import _lib_cache_report as crk
    oldest = crk._row_anchor(crk.CacheRow(date=DATES[0]))
    focal = dt.datetime.strptime("2026-10-30", "%Y-%m-%d").astimezone()
    assert oldest.utcoffset() != focal.utcoffset(), (
        "fixture precondition: the window must straddle a DST transition, "
        f"got {oldest.utcoffset()} and {focal.utcoffset()}"
    )
    assert (focal - oldest) == dt.timedelta(days=14, hours=1), (
        "fixture precondition: the 14-calendar-day row must sit 14d+1h back "
        f"in elapsed time, got {focal - oldest}"
    )


def test_a_row_14_calendar_days_old_survives_a_dst_transition(host_tz_jerusalem):
    import _lib_cache_report as crk
    samples = crk._baseline_samples(
        _rows(crk), anchor_date=dt.date(2026, 10, 30), window_days=14,
        is_session_mode=False,
    )
    assert len(samples) == 5, (
        "the 2026-10-16 row is 14 calendar days old and must be admitted; "
        f"got {len(samples)} samples"
    )


def test_published_count_equals_the_median_population(host_tz_jerusalem):
    import _lib_cache_report as crk
    result = crk.classify_and_summarize(
        _rows(crk), now_utc=NOW,
        window_days=14, anomaly_threshold_pp=15, anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"), mode="day",
    )
    if result.today_baseline_median is None:
        assert result.today_baseline_sample_count < crk.CACHE_REPORT_MIN_BASELINE_DAYS
    else:
        assert result.today_baseline_sample_count >= crk.CACHE_REPORT_MIN_BASELINE_DAYS


def test_the_published_count_is_the_window_population_not_every_other_row(
    host_tz_jerusalem,
):
    """The count must be bounded by the baseline window, as the median is.

    Both publishers previously counted EVERY non-today row, unbounded by
    the window, so a row far outside the baseline still inflated the
    "Building baseline · N/5 days" readout.
    """
    import _lib_cache_report as crk
    rows = _rows(crk)
    stale = crk.CacheRow(date="2020-01-01")
    stale.input_tokens, stale.cache_read_tokens = 400, 600
    rows.append(stale)

    result = crk.classify_and_summarize(
        rows, now_utc=NOW,
        window_days=14, anomaly_threshold_pp=15, anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"), mode="day",
    )
    assert result.today_baseline_sample_count == 5, (
        "a 2020 row is outside the 14-day baseline window and must not be "
        f"counted; got {result.today_baseline_sample_count}"
    )


def test_session_mode_keeps_elapsed_time_windowing():
    """Session anchors are real timestamps; elapsed windowing is correct there.

    Guards the deliberate asymmetry so a later reader does not "unify"
    the two modes onto calendar dates.
    """
    import _lib_cache_report as crk
    anchor = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)
    rows = []
    for hours in (1, 24, 48, 72, 96):
        r = crk.CacheRow(session_id=f"s-{hours}")
        r.last_activity = anchor - dt.timedelta(hours=hours)
        r.input_tokens, r.cache_read_tokens = 400, 600
        rows.append(r)
    just_outside = crk.CacheRow(session_id="s-old")
    just_outside.last_activity = anchor - dt.timedelta(days=14, seconds=1)
    just_outside.input_tokens, just_outside.cache_read_tokens = 400, 600
    rows.append(just_outside)

    samples = crk._baseline_samples(
        rows, anchor=anchor, window_days=14, is_session_mode=True,
    )
    assert len(samples) == 5, (
        "session mode compares instants: the row one second past the "
        f"14-day bound must fall out; got {len(samples)} samples"
    )
