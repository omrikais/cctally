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


def test_apply_reset_events_overrides_post_reset_display_start_date():
    """When a SubWeek's end_ts equals a reset event's new_week_end_at, the
    POST-reset week's start_ts and display_start_date both move to
    effective_reset_at_utc. start_date (the bucket / lookup key) must NOT
    shift — it stays the API-derived backdated date."""
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

    # Pre-reset: end_ts moved to reset moment (existing behavior). Both
    # end_date and display_start_date stay aligned with their source
    # (start_date unchanged; end_date shifted by existing code).
    assert pre_out.end_ts == "2026-04-13T18:00:00+00:00"
    assert pre_out.end_date == dt.date(2026, 4, 13)
    assert pre_out.start_date == dt.date(2026, 4, 9)
    assert pre_out.display_start_date == dt.date(2026, 4, 9)

    # Post-reset: start_ts moved to reset moment; display_start_date follows.
    assert post_out.start_ts == "2026-04-13T18:00:00+00:00"
    assert post_out.display_start_date == dt.date(2026, 4, 13)
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
