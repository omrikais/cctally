"""A credit is an accounting epoch, never a display boundary (#703 + #707 §2).

Section 2 is the rule under test: an Anthropic reset never changes the week's
boundaries. Whatever Anthropic does to the counter, the window keeps its
original start and end and only the running 7d percent steps down.

`_apply_reset_events_to_weekrefs` did the opposite. It truncated the pre-credit
week at the credit moment, re-anchored the post-credit week's start to it, and
for an in-place credit SYNTHESIZED a second reference so one unchanged
subscription week rendered as two rows.

What section 7 additionally asked for — recovering the original cadence for a
boundary CHANGE and coalescing the linked references into it — is NOT
implemented, and the retreat is deliberate. Every formulation of it tried here
broke something a reader would notice: pulling the moved week's end back to the
original one left every entry between the two ends inside no window and the
week's spend vanished from the panel; keeping the later end produced a ten-day
"week"; and merging the two references reported one week's counter against the
other's key. Section 7 asserts the coalescing in one clause and never says which
week's usage key the merged row reports, which window its cost is taken over, or
where spend recorded after the original end goes. A reference the API moved
therefore renders on the window it recorded, untruncated and un-re-anchored.
"""
from __future__ import annotations

import pytest

from conftest import load_script, redirect_paths

WEEK_START_DATE = "2026-08-29"
WEEK_END_DATE = "2026-09-05"
ORIGINAL_START = "2026-08-29T05:00:00+00:00"
ORIGINAL_END = "2026-09-05T05:00:00+00:00"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _event(conn, *, old, new, effective, account_key="unattributed",
           week_start_date=WEEK_START_DATE):
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
        " week_start_date, observed_at_utc, confirming_capture_at_utc, "
        " observed_post_credit_pct, credit_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (effective, old, new, effective, 46.0, account_key, week_start_date,
         effective, effective, 2.0, f"o:{effective}:{new}"))
    conn.commit()


def _ref(ns, *, end_at=ORIGINAL_END, start_at=ORIGINAL_START,
         week_start_date=WEEK_START_DATE, week_end_date=WEEK_END_DATE):
    return ns["make_week_ref"](
        week_start_date=week_start_date, week_end_date=week_end_date,
        week_start_at=start_at, week_end_at=end_at)


def test_a_credited_week_is_one_reference_with_its_original_boundaries(ns):
    """The in-place credit shape: `old == effective`, `new` unchanged.

    This is the 2026-09-01 incident's own shape, and the whole of what §6.1
    asks for on it: one reference, its own boundaries, no synthesized
    pre-credit twin.
    """
    conn = ns["open_db"]()
    try:
        _event(conn, old="2026-09-01T17:00:00+00:00",
               new=ORIGINAL_END, effective="2026-09-01T17:00:00+00:00")
        refs = ns["_apply_reset_events_to_weekrefs"](conn, [_ref(ns)])
    finally:
        conn.close()
    assert len(refs) == 1, [r for r in refs]
    assert refs[0].week_start_at == ORIGINAL_START
    assert refs[0].week_end_at == ORIGINAL_END


def test_the_credit_moment_never_reaches_a_displayed_boundary(ns):
    """Neither reference is truncated at the credit, and neither is anchored to
    it. That is what "stop re-anchoring the display window" means here."""
    conn = ns["open_db"]()
    try:
        _event(conn, old=ORIGINAL_END, new="2026-09-07T05:00:00+00:00",
               effective="2026-09-01T17:00:00+00:00")
        before = _ref(ns)
        after = _ref(ns, start_at="2026-08-31T05:00:00+00:00",
                     end_at="2026-09-07T05:00:00+00:00",
                     week_start_date="2026-08-31", week_end_date="2026-09-07")
        refs = ns["_apply_reset_events_to_weekrefs"](conn, [before, after])
    finally:
        conn.close()
    boundaries = {r.week_start_at for r in refs} | {r.week_end_at for r in refs}
    assert "2026-09-01T17:00:00+00:00" not in boundaries, boundaries
    assert (refs[0].week_start_at, refs[0].week_end_at) == (
        ORIGINAL_START, ORIGINAL_END)
    assert (refs[1].week_start_at, refs[1].week_end_at) == (
        "2026-08-31T05:00:00+00:00", "2026-09-07T05:00:00+00:00")


def test_a_manual_credit_moves_no_boundary_and_changes_no_reference(ns):
    """Both boundary columns NULL — a counter discontinuity inside an unchanged
    window, so there is nothing here even to consider rewriting."""
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
            " week_start_date, observed_at_utc, observed_post_credit_pct, "
            " credit_key) VALUES (?,NULL,NULL,?,?,?,?,?,?,?)",
            ("2026-09-01T17:00:00Z", "2026-09-01T17:00:00+00:00", 46.0,
             "unattributed", WEEK_START_DATE, "2026-09-01T17:12:00Z", 31.0,
             "o:manual"))
        conn.commit()
        refs = ns["_apply_reset_events_to_weekrefs"](conn, [_ref(ns)])
    finally:
        conn.close()
    assert len(refs) == 1
    assert refs[0].week_start_at == ORIGINAL_START
    assert refs[0].week_end_at == ORIGINAL_END


def test_the_subweek_sibling_leaves_its_windows_alone_too(ns):
    """`cctally weekly` grew a second row for one unchanged subscription week
    because this function moved a sub-week's `start_ts` to the credit."""
    import datetime as dt

    conn = ns["open_db"]()
    try:
        _event(conn, old="2026-09-01T17:00:00+00:00",
               new=ORIGINAL_END, effective="2026-09-01T17:00:00+00:00")
        sw = ns["SubWeek"](
            start_ts=ORIGINAL_START, end_ts=ORIGINAL_END,
            start_date=dt.date(2026, 8, 29), end_date=dt.date(2026, 9, 4),
            source="snapshot", display_start_date=dt.date(2026, 8, 29))
        out = ns["_apply_reset_events_to_subweeks"](conn, [sw])
    finally:
        conn.close()
    assert len(out) == 1
    assert out[0].start_ts == ORIGINAL_START
    assert out[0].end_ts == ORIGINAL_END
    assert out[0].display_start_date == dt.date(2026, 8, 29)
