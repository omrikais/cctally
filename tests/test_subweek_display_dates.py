"""Tests for SubWeek.display_start_date and the reset-event post-processor
that overrides it for post-reset weeks. Pre-reset weeks pass through
unchanged (only end_ts / end_date shift, both pre-existing semantics)."""
import datetime as dt
import sqlite3

from conftest import load_script


def _make_subweek(ns, *, start_iso, end_iso, source="snapshot"):
    """Build a SubWeek with display_start_date defaulting to start_date
    (mirroring _compute_subscription_weeks)."""
    SubWeek = ns["SubWeek"]
    parse = ns["parse_iso_datetime"]
    s_dt = parse(start_iso, "test.start")
    e_dt = parse(end_iso, "test.end")
    s_date = s_dt.astimezone().date()
    e_date = (e_dt - dt.timedelta(seconds=1)).astimezone().date()
    return SubWeek(
        start_ts=start_iso,
        end_ts=end_iso,
        start_date=s_date,
        end_date=e_date,
        source=source,
        display_start_date=s_date,
    )


def test_subweek_default_display_start_date_matches_start_date():
    ns = load_script()
    sw = _make_subweek(
        ns,
        start_iso="2026-04-09T15:00:00+00:00",
        end_iso="2026-04-16T15:00:00+00:00",
    )
    assert sw.display_start_date == sw.start_date


def test_apply_reset_events_never_cuts_a_sub_week_at_the_credit():
    """The credit moment is not a display boundary.

    This asserted the opposite until #703 + #707: the pre-reset week's `end_ts`
    was truncated to the credit moment and the post-reset week's `start_ts` and
    `display_start_date` moved there, so one subscription week rendered as two.
    Section 2 reverses it — an Anthropic reset never changes the week's
    boundaries — so no sub-week is cut at the credit and none is anchored to it.

    One rewrite remains and is asserted below: the pre-reset week's end carries
    FORWARD to the boundary the API's own later statement moved it to. The
    earlier name and docstring here said every sub-week was returned as it came,
    which contradicted that assertion.

    `start_date` is left alone throughout, because it is the bucket and lookup
    key into `weekly_usage_snapshots.week_start_date`.
    """
    ns = load_script()
    apply_events = ns["_apply_reset_events_to_subweeks"]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE week_reset_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at_utc TEXT NOT NULL,
            old_week_end_at TEXT NOT NULL,
            new_week_end_at TEXT NOT NULL,
            effective_reset_at_utc TEXT NOT NULL,
            UNIQUE(old_week_end_at, new_week_end_at)
        )
    """)
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, effective_reset_at_utc) "
        "VALUES (?, ?, ?, ?)",
        ("2026-04-13T18:01:00Z",
         "2026-04-16T15:00:00+00:00",
         "2026-04-18T15:00:00+00:00",
         "2026-04-13T18:00:00+00:00"),
    )

    pre = _make_subweek(
        ns,
        start_iso="2026-04-09T15:00:00+00:00",
        end_iso="2026-04-16T15:00:00+00:00",
    )
    post = _make_subweek(
        ns,
        start_iso="2026-04-11T15:00:00+00:00",  # API-derived backdated start
        end_iso="2026-04-18T15:00:00+00:00",
    )

    out = apply_events(conn, [pre, post])
    assert len(out) == 2
    pre_out, post_out = out

    # The pre-reset week is not cut at the credit. Its end carries forward to
    # the boundary the change moved it to: the two boundary columns are the
    # same week's end as the API stated it before and after, and that is one
    # week, so it ends at the later statement.
    assert pre_out.end_ts == "2026-04-18T15:00:00+00:00"
    assert pre_out.start_ts == "2026-04-09T15:00:00+00:00"
    assert pre_out.start_date == dt.date(2026, 4, 9)
    assert pre_out.display_start_date == dt.date(2026, 4, 9)

    # The post-reset week keeps its own start: it is not anchored to the credit.
    assert post_out.start_ts == "2026-04-11T15:00:00+00:00"
    assert post_out.end_ts == "2026-04-18T15:00:00+00:00"
    assert post_out.display_start_date == dt.date(2026, 4, 11)
    # Bucket / lookup key intact (still 2026-04-11, the API-derived date).
    assert post_out.start_date == dt.date(2026, 4, 11)


def test_apply_reset_events_no_event_passes_through():
    """When no reset events exist, display_start_date == start_date for
    every SubWeek."""
    ns = load_script()
    apply_events = ns["_apply_reset_events_to_subweeks"]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE week_reset_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at_utc TEXT NOT NULL,
            old_week_end_at TEXT NOT NULL,
            new_week_end_at TEXT NOT NULL,
            effective_reset_at_utc TEXT NOT NULL,
            UNIQUE(old_week_end_at, new_week_end_at)
        )
    """)

    sw = _make_subweek(
        ns,
        start_iso="2026-04-09T15:00:00+00:00",
        end_iso="2026-04-16T15:00:00+00:00",
    )
    out = apply_events(conn, [sw])
    assert out[0].display_start_date == out[0].start_date == dt.date(2026, 4, 9)
