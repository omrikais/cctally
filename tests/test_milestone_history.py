"""Hero-modal historical milestones — backend kernel + glue tests.

Covers the new `bin/_lib_milestone_history.py` pure kernel and the
`bin/_cctally_milestone_history.py` I/O glue introduced for the dashboard
hero modal's week/cycle history navigation
(docs/superpowers/specs/2026-07-22-hero-milestone-history-design.md,
plan Tasks 1–4).

Task 1 (this section): Claude week index + week detail.

Seeding mirrors the established stats-fixture pattern (open_db() to build
the schema under an isolated fake HOME via redirect_paths, then INSERT
rows directly). All timestamps are hour-aligned UTC so
`_canonicalize_optional_iso`'s normalize+UTC pass is a no-op and the
week_reset_events maps join cleanly against WeekRef boundaries.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths
# `start` is aliased because this file also binds `start` as an ordinary
# local in several tests, and a module-level name a function reassigns is a
# local for that whole function.
from tests._support_http import start as start_accept_loop, stop


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# ── seed helpers ───────────────────────────────────────────────────────


def _seed_usage(
    conn,
    *,
    captured_at_utc: str,
    week_start_date: str,
    week_start_at: str,
    week_end_at: str,
    weekly_percent: float,
    five_hour_window_key: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            captured_at_utc,
            week_start_date,
            week_end_at[:10],
            week_start_at,
            week_end_at,
            weekly_percent,
            "test",
            "{}",
        ),
    )
    return int(cur.lastrowid)


def _seed_cost(conn, *, captured_at_utc, week_start_date, week_start_at, week_end_at):
    conn.execute(
        "INSERT INTO weekly_cost_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, range_start_iso, range_end_iso, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            captured_at_utc,
            week_start_date,
            week_end_at[:10],
            week_start_at,
            week_end_at,
            week_start_at,
            week_end_at,
            1.23,
        ),
    )


def _seed_percent_milestone(
    conn,
    *,
    week_start_date: str,
    week_end_date: str,
    percent_threshold: int,
    captured_at_utc: str,
    cumulative_cost_usd: float,
    marginal_cost_usd: float | None = None,
    reset_event_id: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO percent_milestones "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, percent_threshold, cumulative_cost_usd, "
        " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id, "
        " five_hour_percent_at_crossing, reset_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            captured_at_utc,
            week_start_date,
            week_end_date,
            None,
            None,
            percent_threshold,
            cumulative_cost_usd,
            marginal_cost_usd,
            1,
            1,
            None,
            reset_event_id,
        ),
    )


def _seed_reset_event(
    conn, *, old_week_end_at, new_week_end_at, effective, observed_pre_credit_pct
) -> int:
    cur = conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct) VALUES (?, ?, ?, ?, ?)",
        (effective, old_week_end_at, new_week_end_at, effective, observed_pre_credit_pct),
    )
    return int(cur.lastrowid)


def _seed_block(
    conn,
    *,
    window_key: int,
    block_start_at: str,
    five_hour_resets_at: str,
    final_five_hour_percent: float = 5.0,
    total_cost_usd: float = 1.0,
    crossed: int = 0,
    is_closed: int = 1,
) -> int:
    cur = conn.execute(
        "INSERT INTO five_hour_blocks "
        "(five_hour_window_key, five_hour_resets_at, block_start_at, "
        " first_observed_at_utc, last_observed_at_utc, final_five_hour_percent, "
        " crossed_seven_day_reset, total_cost_usd, is_closed, "
        " created_at_utc, last_updated_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            window_key,
            five_hour_resets_at,
            block_start_at,
            block_start_at,
            five_hour_resets_at,
            final_five_hour_percent,
            crossed,
            total_cost_usd,
            is_closed,
            block_start_at,
            five_hour_resets_at,
        ),
    )
    return int(cur.lastrowid)


def _seed_5h_milestone(
    conn, *, block_id, window_key, percent_threshold, captured_at_utc,
    block_cost_usd=0.5, reset_event_id=0,
):
    conn.execute(
        "INSERT INTO five_hour_milestones "
        "(block_id, five_hour_window_key, percent_threshold, captured_at_utc, "
        " usage_snapshot_id, block_cost_usd, reset_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (block_id, window_key, percent_threshold, captured_at_utc, 1,
         block_cost_usd, reset_event_id),
    )


# Canonical hour-aligned UTC boundaries (see module docstring).
WK_A_START = "2026-05-15T00:00:00+00:00"
WK_A_END = "2026-05-22T00:00:00+00:00"
WK_B_START = "2026-05-08T00:00:00+00:00"
WK_B_END = "2026-05-15T00:00:00+00:00"
WK_C_START = "2026-05-01T00:00:00+00:00"
WK_C_END = "2026-05-08T00:00:00+00:00"
WK_C_EFFECTIVE = "2026-05-04T12:00:00+00:00"


def _seed_full(conn) -> int:
    """Seed 3 navigable weeks + 1 cost-only week. Return the credit event id."""
    # Week C (oldest) — in-place credited: two milestone segments.
    _seed_usage(
        conn, captured_at_utc="2026-05-02T09:00:00Z",
        week_start_date="2026-05-01", week_start_at=WK_C_START,
        week_end_at=WK_C_END, weekly_percent=40.0,
    )
    event_id = _seed_reset_event(
        conn, old_week_end_at=WK_C_EFFECTIVE, new_week_end_at=WK_C_END,
        effective=WK_C_EFFECTIVE, observed_pre_credit_pct=40.0,
    )
    # pre-credit segment (reset_event_id = 0)
    _seed_percent_milestone(
        conn, week_start_date="2026-05-01", week_end_date="2026-05-08",
        percent_threshold=1, captured_at_utc="2026-05-02T10:00:00+00:00",
        cumulative_cost_usd=1.0, reset_event_id=0,
    )
    _seed_percent_milestone(
        conn, week_start_date="2026-05-01", week_end_date="2026-05-08",
        percent_threshold=2, captured_at_utc="2026-05-03T10:00:00+00:00",
        cumulative_cost_usd=2.0, reset_event_id=0,
    )
    # post-credit segment (reset_event_id = event_id)
    _seed_percent_milestone(
        conn, week_start_date="2026-05-01", week_end_date="2026-05-08",
        percent_threshold=1, captured_at_utc="2026-05-05T10:00:00+00:00",
        cumulative_cost_usd=0.5, reset_event_id=event_id,
    )
    _seed_percent_milestone(
        conn, week_start_date="2026-05-01", week_end_date="2026-05-08",
        percent_threshold=2, captured_at_utc="2026-05-06T10:00:00+00:00",
        cumulative_cost_usd=1.0, reset_event_id=event_id,
    )

    # Week B (middle) — usage only, ZERO milestones.
    _seed_usage(
        conn, captured_at_utc="2026-05-09T09:00:00Z",
        week_start_date="2026-05-08", week_start_at=WK_B_START,
        week_end_at=WK_B_END, weekly_percent=5.0,
    )

    # Straddler block: crosses the B/A boundary (2026-05-15T00:00).
    _seed_block(
        conn, window_key=5149, block_start_at="2026-05-14T22:00:00+00:00",
        five_hour_resets_at="2026-05-15T03:00:00+00:00", crossed=1,
    )

    # Week A (newest / current) — full data.
    _seed_usage(
        conn, captured_at_utc="2026-05-16T09:00:00Z",
        week_start_date="2026-05-15", week_start_at=WK_A_START,
        week_end_at=WK_A_END, weekly_percent=10.0,
    )
    _seed_usage(
        conn, captured_at_utc="2026-05-17T09:00:00Z",
        week_start_date="2026-05-15", week_start_at=WK_A_START,
        week_end_at=WK_A_END, weekly_percent=20.0,
    )
    for i, cum in ((1, 1.0), (2, 2.0), (3, 3.0)):
        _seed_percent_milestone(
            conn, week_start_date="2026-05-15", week_end_date="2026-05-22",
            percent_threshold=i, captured_at_utc=f"2026-05-16T1{i}:00:00+00:00",
            cumulative_cost_usd=cum, marginal_cost_usd=1.0, reset_event_id=0,
        )
    block_a = _seed_block(
        conn, window_key=5155, block_start_at="2026-05-16T00:00:00+00:00",
        five_hour_resets_at="2026-05-16T05:00:00+00:00", total_cost_usd=2.5,
    )
    _seed_5h_milestone(
        conn, block_id=block_a, window_key=5155, percent_threshold=1,
        captured_at_utc="2026-05-16T01:00:00+00:00",
    )
    _seed_5h_milestone(
        conn, block_id=block_a, window_key=5155, percent_threshold=2,
        captured_at_utc="2026-05-16T02:00:00+00:00",
    )

    # Cost-only week (must be EXCLUDED from the navigable index).
    _seed_cost(
        conn, captured_at_utc="2026-04-25T09:00:00Z",
        week_start_date="2026-04-24", week_start_at="2026-04-24T00:00:00+00:00",
        week_end_at="2026-05-01T00:00:00+00:00",
    )
    conn.commit()
    return event_id


# ── Task 1: Claude week index ──────────────────────────────────────────


def test_claude_week_index_enumerates_newest_first_with_counts(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_full(conn)
        idx = mh.build_claude_week_index(conn)
    finally:
        conn.close()

    starts = [e["start_at_utc"] for e in idx]
    # Newest-first, cost-only 2026-04-24 excluded, reset split retained.
    assert starts == [
        "2026-05-15T00:00:00Z",
        "2026-05-08T00:00:00Z",
        "2026-05-04T12:00:00Z",
        "2026-05-01T00:00:00Z",
    ]

    by_start = {e["start_at_utc"]: e for e in idx}
    a = by_start["2026-05-15T00:00:00Z"]
    assert a["is_current"] is True
    assert a["milestone_count"] == 3
    assert a["segment_count"] == 1
    assert a["block_count"] == 2  # fully-inside block + straddler
    assert a["start_at_utc"] == "2026-05-15T00:00:00Z"
    assert a["end_at_utc"] == "2026-05-22T00:00:00Z"
    assert a["label"]  # non-empty
    assert a["detail_stamp"]


def test_claude_week_index_survives_a_milestone_only_week_with_no_boundaries(ns):
    """A milestone row whose ``week_start_at`` is NULL must not take the whole
    index down.

    ``percent_milestones.week_start_at`` is nullable and post-dates the table,
    and ``maybe_record_milestone`` reads it from ``saved.get("weekStartAt")``
    where ``_canonicalize_optional_iso`` accepts ``None``, so a live install
    can hold such a row. When that week also has no ``weekly_usage_snapshots``
    row, ``_navigable_claude_refs`` synthesizes a ref with both boundaries
    ``None`` and ``_claude_cycle_key`` passed the empty string to
    ``dashboard_resource_key``, whose ``_nonempty_string`` guard raised. The
    dashboard caught that at the top of the phase, emptied ``week_index``
    entirely and set ``last_sync_error`` — so one boundary-less historical row
    removed every OTHER week from the hero modal's history.

    The detail leg is covered here too. ``build_claude_week_detail`` builds its
    own ``milestone_segment`` key from the same ref, unconditionally and ahead
    of the ``if rows`` guard, so a fix confined to the index restores the row to
    the list and leaves it unopenable. ``_handle_get_milestones_week``'s
    catch-all then turns the ``ValueError`` into a generic HTTP 500, not the
    404 ``unknown_key`` body the client renders as a real state, so the failure
    reaches the operator only as a server log line.
    """
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_full(conn)
        # A milestone-only week: no usage snapshot, and the nullable instant
        # columns left NULL, which is the shape of a pre-column row.
        _seed_percent_milestone(
            conn, week_start_date="2026-04-10", week_end_date="2026-04-17",
            percent_threshold=100, captured_at_utc="2026-04-15T20:00:00+00:00",
            cumulative_cost_usd=44.0, reset_event_id=0,
        )
        conn.commit()
        idx = mh.build_claude_week_index(conn)
        details = [
            mh.build_claude_week_detail(conn, e["key"])
            for e in idx
            if e["start_at_utc"] is None
        ]
    finally:
        conn.close()

    starts = [e["start_at_utc"] for e in idx]
    assert starts[:4] == [
        "2026-05-15T00:00:00Z",
        "2026-05-08T00:00:00Z",
        "2026-05-04T12:00:00Z",
        "2026-05-01T00:00:00Z",
    ], "the boundary-carrying weeks must still be enumerated"

    boundaryless = [e for e in idx if e["start_at_utc"] is None]
    assert len(boundaryless) == 1, idx
    entry = boundaryless[0]
    assert entry["end_at_utc"] is None
    assert entry["milestone_count"] == 1
    assert entry["block_count"] == 0
    assert entry["key"], "a boundary-less cycle still needs a stable key"
    assert entry["label"], "a boundary-less cycle still needs a label"

    assert len(details) == 1, details
    detail = details[0]
    assert detail is not None, "the listed boundary-less cycle must also open"
    assert detail["key"] == entry["key"]
    assert detail["start_at_utc"] is None
    assert detail["end_at_utc"] is None
    assert detail["blocks"] == [], "absent bounds select no five-hour blocks"
    assert len(detail["segments"]) == 1, detail["segments"]
    segment = detail["segments"][0]
    assert segment["key"].startswith("milestone_segment:")
    assert [m["percent"] for m in segment["milestones"]] == [100]


def test_claude_week_index_usage_only_week_zero_milestones(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_full(conn)
        idx = mh.build_claude_week_index(conn)
    finally:
        conn.close()

    b = {e["start_at_utc"]: e for e in idx}["2026-05-08T00:00:00Z"]
    assert b["milestone_count"] == 0
    assert b["segment_count"] == 0
    assert b["is_current"] is False
    assert b["block_count"] == 1  # straddler intersects week B too


def test_claude_reset_defined_week_emits_two_opaque_cycle_entries(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_full(conn)
        idx = mh.build_claude_week_index(conn)
    finally:
        conn.close()

    c_entries = [
        e for e in idx
        if e["start_at_utc"] < "2026-05-08T00:00:00Z"
        and e["end_at_utc"] > "2026-05-01T00:00:00Z"
    ]
    assert len(c_entries) == 2
    assert len({e["key"] for e in c_entries}) == 2
    assert all(e["key"].startswith("milestone_cycle:") for e in c_entries)
    assert all("2026-05-01" not in e["key"] for e in c_entries)
    assert [(e["start_at_utc"], e["end_at_utc"]) for e in c_entries] == [
        ("2026-05-04T12:00:00Z", "2026-05-08T00:00:00Z"),
        ("2026-05-01T00:00:00Z", "2026-05-04T12:00:00Z"),
    ]
    assert [e["milestone_count"] for e in c_entries] == [2, 2]
    assert all(e["segment_count"] == 1 for e in c_entries)


# ── Task 1: Claude week detail ─────────────────────────────────────────


def test_claude_cycle_detail_selects_only_its_reset_cohort(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_full(conn)
        entries = [
            e for e in mh.build_claude_week_index(conn)
            if e["start_at_utc"] < "2026-05-08T00:00:00Z"
            and e["end_at_utc"] > "2026-05-01T00:00:00Z"
        ]
        post = mh.build_claude_week_detail(conn, entries[0]["key"])
        pre = mh.build_claude_week_detail(conn, entries[1]["key"])
    finally:
        conn.close()

    assert post is not None and pre is not None
    assert post["key"] != pre["key"]
    assert len(post["segments"]) == len(pre["segments"]) == 1
    assert [m["percent"] for m in post["segments"][0]["milestones"]] == [1, 2]
    assert [m["percent"] for m in pre["segments"][0]["milestones"]] == [1, 2]
    assert post["segments"][0]["milestones"][0]["cumulative_usd"] == 0.5
    assert post["dividers"] == pre["dividers"] == []
    assert post["segments"][0]["key"].startswith("milestone_segment:")
    assert "reset_event_id" not in post["segments"][0]


def test_claude_production_shaped_98_rows_split_43_55_with_exact_blocks(ns):
    import _cctally_milestone_history as mh

    start = "2026-07-11T05:00:00+00:00"
    reset = "2026-07-16T05:00:00+00:00"
    end = "2026-07-18T05:00:00+00:00"
    conn = ns["open_db"]()
    try:
        _seed_usage(
            conn, captured_at_utc="2026-07-17T12:00:00Z",
            week_start_date="2026-07-11", week_start_at=start,
            week_end_at=end, weekly_percent=55.0,
        )
        event_id = _seed_reset_event(
            conn, old_week_end_at=reset, new_week_end_at=end,
            effective=reset, observed_pre_credit_pct=43.0,
        )
        for pct in range(1, 44):
            _seed_percent_milestone(
                conn, week_start_date="2026-07-11", week_end_date="2026-07-18",
                percent_threshold=pct, captured_at_utc="2026-07-12T12:00:00Z",
                cumulative_cost_usd=float(pct), reset_event_id=0,
            )
        for pct in range(1, 56):
            _seed_percent_milestone(
                conn, week_start_date="2026-07-11", week_end_date="2026-07-18",
                percent_threshold=pct, captured_at_utc="2026-07-17T12:00:00Z",
                cumulative_cost_usd=float(pct), reset_event_id=event_id,
            )
        _seed_block(
            conn, window_key=711, block_start_at="2026-07-12T00:00:00Z",
            five_hour_resets_at="2026-07-12T05:00:00Z",
        )
        _seed_block(
            conn, window_key=716, block_start_at="2026-07-16T03:00:00Z",
            five_hour_resets_at="2026-07-16T08:00:00Z", crossed=1,
        )
        _seed_block(
            conn, window_key=717, block_start_at="2026-07-17T00:00:00Z",
            five_hour_resets_at="2026-07-17T05:00:00Z",
        )
        conn.commit()

        entries = mh.build_claude_week_index(conn)
        details = [mh.build_claude_week_detail(conn, entry["key"]) for entry in entries]
    finally:
        conn.close()

    assert [entry["milestone_count"] for entry in entries] == [55, 43]
    assert [len(detail["segments"][0]["milestones"]) for detail in details] == [55, 43]
    assert [[b["five_hour_window_key"] for b in detail["blocks"]] for detail in details] == [
        [716, 717],
        [711, 716],
    ]
    assert [entry["block_count"] for entry in entries] == [2, 2]


def test_claude_early_reanchor_same_storage_bucket_emits_post_cycle(ns):
    import _cctally_milestone_history as mh

    start = "2026-04-13T14:00:00+00:00"
    old_end = "2026-04-17T14:00:00+00:00"
    reset = "2026-04-17T13:00:00+00:00"
    new_end = "2026-04-20T14:00:00+00:00"
    conn = ns["open_db"]()
    try:
        _seed_usage(
            conn, captured_at_utc="2026-04-16T12:00:00Z",
            week_start_date="2026-04-13", week_start_at=start,
            week_end_at=old_end, weekly_percent=60.0,
        )
        _seed_usage(
            conn, captured_at_utc=reset,
            week_start_date="2026-04-13", week_start_at=start,
            week_end_at=new_end, weekly_percent=0.0,
        )
        event_id = _seed_reset_event(
            conn, old_week_end_at=old_end, new_week_end_at=new_end,
            effective=reset, observed_pre_credit_pct=60.0,
        )
        _seed_percent_milestone(
            conn, week_start_date="2026-04-13", week_end_date="2026-04-20",
            percent_threshold=1, captured_at_utc="2026-04-17T15:00:00Z",
            cumulative_cost_usd=1.0, reset_event_id=event_id,
        )
        conn.commit()
        entries = [
            e for e in mh.build_claude_week_index(conn)
            if e["start_at_utc"] >= "2026-04-13T14:00:00Z"
        ]
        post = mh.build_claude_week_detail(conn, entries[0]["key"])
    finally:
        conn.close()

    assert [(e["start_at_utc"], e["end_at_utc"]) for e in entries] == [
        ("2026-04-17T13:00:00Z", "2026-04-20T14:00:00Z"),
        ("2026-04-13T14:00:00Z", "2026-04-17T13:00:00Z"),
    ]
    assert [e["milestone_count"] for e in entries] == [1, 0]
    assert entries[0]["is_current"] is True
    assert post is not None
    assert [m["percent"] for m in post["segments"][0]["milestones"]] == [1]


def test_claude_week_detail_unknown_key_returns_none(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_full(conn)
        assert mh.build_claude_week_detail(conn, "milestone_cycle:unknown") is None
    finally:
        conn.close()


def test_claude_week_detail_straddling_block_in_both_weeks(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_full(conn)
        idx = mh.build_claude_week_index(conn)
        entry_a = next(e for e in idx if e["start_at_utc"] == "2026-05-15T00:00:00Z")
        entry_b = next(e for e in idx if e["start_at_utc"] == "2026-05-08T00:00:00Z")
        detail_a = mh.build_claude_week_detail(conn, entry_a["key"])
        detail_b = mh.build_claude_week_detail(conn, entry_b["key"])
    finally:
        conn.close()

    a_keys = {b["five_hour_window_key"] for b in detail_a["blocks"]}
    b_keys = {b["five_hour_window_key"] for b in detail_b["blocks"]}
    assert 5149 in a_keys  # straddler appears in week A
    assert 5149 in b_keys  # ...and in week B
    assert 5155 in a_keys  # fully-inside block only in A
    assert 5155 not in b_keys
    # blocks ascending by start
    starts = [b["block_start_at"] for b in detail_a["blocks"]]
    assert starts == sorted(starts)
    # per-block milestones carried
    inside = next(b for b in detail_a["blocks"] if b["five_hour_window_key"] == 5155)
    assert [m["percent_threshold"] for m in inside["milestones"]] == [1, 2]


def test_claude_detail_stamp_moves_when_milestone_added(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_full(conn)
        before_entries = mh.build_claude_week_index(conn)
        before = {e["start_at_utc"]: e["detail_stamp"] for e in before_entries}
        _seed_percent_milestone(
            conn, week_start_date="2026-05-15", week_end_date="2026-05-22",
            percent_threshold=4, captured_at_utc="2026-05-16T20:00:00+00:00",
            cumulative_cost_usd=4.0, reset_event_id=0,
        )
        conn.commit()
        after = {
            e["start_at_utc"]: e["detail_stamp"]
            for e in mh.build_claude_week_index(conn)
        }
    finally:
        conn.close()

    assert after["2026-05-15T00:00:00Z"] != before["2026-05-15T00:00:00Z"]
    # unrelated weeks' stamps unchanged
    assert after["2026-05-08T00:00:00Z"] == before["2026-05-08T00:00:00Z"]


def test_get_recent_weeks_none_is_unbounded(ns):
    import _cctally_weekrefs  # noqa: F401 (loaded via ns namespace)

    conn = ns["open_db"]()
    try:
        _seed_full(conn)
        get_recent_weeks = ns["get_recent_weeks"]
        unbounded = get_recent_weeks(conn, None)
        limited = get_recent_weeks(conn, 2)
    finally:
        conn.close()

    unbounded_keys = {r.key for r in unbounded}
    assert {"2026-05-15", "2026-05-08", "2026-05-01"} <= unbounded_keys
    assert len(limited) == 2
    assert len(unbounded) > len(limited)


# ── Task 2: Codex cycle index + cycle detail ───────────────────────────
#
# Seeds the durable projection tables (quota_window_blocks /
# quota_percent_milestones, stats.db) directly. The hero identity is passed
# as a duck-typed object exposing source_root_keys + resets_at (the real
# source build passes the CodexCycleBoundary). codex_quota_breakdown needs
# cache evidence to emit rows, so detail segment milestones are empty in
# these unit tests — the index counts (milestone_count/block_count) and key
# disambiguation are what these prove; end-to-end breakdown correlation is
# covered by the dashboard goldens (Task 7).
import types


def _seed_quota_block(
    conn,
    *,
    source_root_key: str,
    logical_limit_key: str = "account",
    observed_slot: str = "primary",
    window_minutes: int,
    resets_at_utc: str,
    nominal_start_at_utc: str,
    current_percent: float = 30.0,
    limit_id: str | None = None,
    orphaned_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO quota_window_blocks "
        "(source, source_root_key, logical_limit_key, observed_slot, "
        " window_minutes, limit_id, limit_name, resets_at_utc, "
        " nominal_start_at_utc, first_observed_at_utc, last_observed_at_utc, "
        " first_percent, current_percent, last_source_path, last_line_offset, "
        " generation, orphaned_at) "
        "VALUES ('codex', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_root_key, logical_limit_key, observed_slot, window_minutes,
            limit_id, None, resets_at_utc, nominal_start_at_utc,
            nominal_start_at_utc, resets_at_utc, 1.0, current_percent,
            "/tmp/rollout.jsonl", 0, "gen-1", orphaned_at,
        ),
    )


def _seed_quota_milestone(
    conn,
    *,
    source_root_key: str,
    logical_limit_key: str = "account",
    observed_slot: str = "primary",
    window_minutes: int,
    resets_at_utc: str,
    percent_threshold: int,
    captured_at_utc: str,
    orphaned_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO quota_percent_milestones "
        "(source, source_root_key, logical_limit_key, observed_slot, "
        " window_minutes, resets_at_utc, percent_threshold, captured_at_utc, "
        " source_path, line_offset, high_water_percent, generation, orphaned_at) "
        "VALUES ('codex', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_root_key, logical_limit_key, observed_slot, window_minutes,
            resets_at_utc, percent_threshold, captured_at_utc, "/tmp/rollout.jsonl",
            0, percent_threshold, "gen-1", orphaned_at,
        ),
    )


def _dt(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def test_codex_cycle_index_unbounded_beyond_35_days(ns):
    import _cctally_milestone_history as mh

    base = _dt("2026-03-01T00:00:00+00:00")
    conn = ns["open_db"]()
    try:
        for k in range(8):
            start = base + dt.timedelta(days=7 * k)
            reset = start + dt.timedelta(days=7)
            _seed_quota_block(
                conn, source_root_key="root-a", window_minutes=10080,
                resets_at_utc=reset.isoformat(),
                nominal_start_at_utc=start.isoformat(),
            )
        conn.commit()
        newest_reset = base + dt.timedelta(days=7 * 8)  # reset of cycle 7
        now = newest_reset - dt.timedelta(days=1)
        identity = types.SimpleNamespace(
            source_root_keys=("root-a",), resets_at=newest_reset,
        )
        idx = mh.build_codex_cycle_index(conn, identity=identity, now_utc=now)
    finally:
        conn.close()

    assert len(idx) == 8  # nothing dropped by a 35-day presentation bound
    # newest-first
    resets = [e["resets_at_utc"] for e in idx]
    assert resets == sorted(resets, reverse=True)
    # the newest cycle (reset in the future) is current
    assert idx[0]["is_current"] is True
    assert idx[0]["resets_at_utc"] == "2026-04-26T00:00:00Z"
    # the oldest (>35 days before now) is present
    assert idx[-1]["resets_at_utc"] == "2026-03-08T00:00:00Z"
    for e in idx:
        assert e["key"].startswith("milestone_cycle:")


def test_codex_cycle_index_early_reanchor_clips_end(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        # cycle 1: nominal 03-01 → reset 03-08, but re-anchored early at 03-06.
        _seed_quota_block(
            conn, source_root_key="root-a", window_minutes=10080,
            nominal_start_at_utc="2026-03-01T00:00:00+00:00",
            resets_at_utc="2026-03-08T00:00:00+00:00",
        )
        _seed_quota_block(
            conn, source_root_key="root-a", window_minutes=10080,
            nominal_start_at_utc="2026-03-06T00:00:00+00:00",
            resets_at_utc="2026-03-13T00:00:00+00:00",
        )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a",),
            resets_at=_dt("2026-03-13T00:00:00+00:00"),
        )
        idx = mh.build_codex_cycle_index(
            conn, identity=identity, now_utc=_dt("2026-03-10T00:00:00+00:00"),
        )
    finally:
        conn.close()

    by_reset = {e["resets_at_utc"]: e for e in idx}
    early = by_reset["2026-03-08T00:00:00Z"]
    # end clipped to the next cycle's nominal start (non-overlapping).
    assert early["end_at_utc"] == "2026-03-06T00:00:00Z"
    assert early["start_at_utc"] == "2026-03-01T00:00:00Z"


def test_codex_cycle_index_same_boundary_selects_one_full_identity(ns):
    import _cctally_milestone_history as mh

    reset = "2026-03-08T00:00:00+00:00"
    start = "2026-03-01T00:00:00+00:00"
    conn = ns["open_db"]()
    try:
        _seed_quota_block(
            conn, source_root_key="root-a", window_minutes=10080,
            resets_at_utc=reset, nominal_start_at_utc=start, current_percent=30,
        )
        _seed_quota_block(
            conn, source_root_key="root-b", window_minutes=10080,
            resets_at_utc=reset, nominal_start_at_utc=start, current_percent=60,
        )
        # root-a has 3 milestones, root-b has 1 — distinguishes the identities.
        for pct in (1, 2, 3):
            _seed_quota_milestone(
                conn, source_root_key="root-a", window_minutes=10080,
                resets_at_utc=reset, percent_threshold=pct,
                captured_at_utc="2026-03-02T00:00:00+00:00",
            )
        _seed_quota_milestone(
            conn, source_root_key="root-b", window_minutes=10080,
            resets_at_utc=reset, percent_threshold=1,
            captured_at_utc="2026-03-02T00:00:00+00:00",
        )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a", "root-b"), resets_at=_dt(reset),
        )
        now = _dt("2026-03-05T00:00:00+00:00")
        idx = mh.build_codex_cycle_index(conn, identity=identity, now_utc=now)
        detail = mh.build_codex_cycle_detail(
            conn, None, identity=identity, key=idx[0]["key"], speed="auto",
            now_utc=now,
        )
    finally:
        conn.close()

    assert len(idx) == 1
    assert idx[0]["milestone_count"] == 1  # max-percent root-b identity won
    assert isinstance(detail, dict) and detail["key"] == idx[0]["key"]
    assert detail["dividers"] == []


def test_codex_cycle_no_five_hour_rows_block_count_zero(ns):
    import _cctally_milestone_history as mh

    reset = "2026-03-08T00:00:00+00:00"
    start = "2026-03-01T00:00:00+00:00"
    conn = ns["open_db"]()
    try:
        _seed_quota_block(
            conn, source_root_key="root-a", window_minutes=10080,
            resets_at_utc=reset, nominal_start_at_utc=start,
        )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a",), resets_at=_dt(reset),
        )
        now = _dt("2026-03-05T00:00:00+00:00")
        idx = mh.build_codex_cycle_index(conn, identity=identity, now_utc=now)
        key = idx[0]["key"]
        detail = mh.build_codex_cycle_detail(
            conn, None, identity=identity, key=key, speed="auto", now_utc=now,
        )
    finally:
        conn.close()

    assert idx[0]["block_count"] == 0
    assert detail["blocks"] == []


def test_codex_cycle_detail_unknown_key_returns_reason(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_quota_block(
            conn, source_root_key="root-a", window_minutes=10080,
            resets_at_utc="2026-03-08T00:00:00+00:00",
            nominal_start_at_utc="2026-03-01T00:00:00+00:00",
        )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a",),
            resets_at=_dt("2026-03-08T00:00:00+00:00"),
        )
        now = _dt("2026-03-05T00:00:00+00:00")
        result = mh.build_codex_cycle_detail(
            conn, None, identity=identity, key="milestone_cycle:nope",
            speed="auto", now_utc=now,
        )
    finally:
        conn.close()

    assert isinstance(result, tuple)
    assert result[0] is None
    assert result[1] in {"pruned", "rebuild_pending", "projection_incoherent", "unknown"}


def test_codex_cycle_with_five_hour_rows_block_count(ns):
    import _cctally_milestone_history as mh

    reset = "2026-03-08T00:00:00+00:00"
    start = "2026-03-01T00:00:00+00:00"
    conn = ns["open_db"]()
    try:
        _seed_quota_block(
            conn, source_root_key="root-a", window_minutes=10080,
            resets_at_utc=reset, nominal_start_at_utc=start,
        )
        # a 5h block inside the cycle (same root/slot/limit_id NULL)
        _seed_quota_block(
            conn, source_root_key="root-a", logical_limit_key="five-hour",
            observed_slot="secondary", limit_id="independent-5h",
            window_minutes=300,
            resets_at_utc="2026-03-02T05:00:00+00:00",
            nominal_start_at_utc="2026-03-02T00:00:00+00:00",
        )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a",), resets_at=_dt(reset),
        )
        now = _dt("2026-03-05T00:00:00+00:00")
        idx = mh.build_codex_cycle_index(conn, identity=identity, now_utc=now)
        detail = mh.build_codex_cycle_detail(
            conn, None, identity=identity, key=idx[0]["key"], speed="auto",
            now_utc=now,
        )
    finally:
        conn.close()

    assert idx[0]["block_count"] == 1
    assert len(detail["blocks"]) == 1
    assert detail["blocks"][0]["key"].startswith("block:")


def test_codex_cycle_sequence_selects_one_identity_and_never_overlaps(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        rows = (
            ("root-a", "weekly-a", "primary", 40, "2026-03-01", "2026-03-08"),
            ("root-b", "weekly-b", "secondary", 60, "2026-03-01", "2026-03-08"),
            ("root-b", "weekly-b", "secondary", 20, "2026-03-04", "2026-03-11"),
            ("root-a", "weekly-a", "primary", 50, "2026-03-06", "2026-03-13"),
        )
        for root, limit, slot, pct, start_day, reset_day in rows:
            _seed_quota_block(
                conn, source_root_key=root, logical_limit_key=limit,
                observed_slot=slot, window_minutes=10080, current_percent=pct,
                nominal_start_at_utc=f"{start_day}T00:00:00+00:00",
                resets_at_utc=f"{reset_day}T00:00:00+00:00",
            )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a", "root-b"),
            resets_at=_dt("2026-03-13T00:00:00+00:00"),
        )
        idx = mh.build_codex_cycle_index(
            conn, identity=identity, now_utc=_dt("2026-03-10T00:00:00+00:00"),
        )
    finally:
        conn.close()

    assert [(e["start_at_utc"], e["end_at_utc"]) for e in reversed(idx)] == [
        ("2026-03-01T00:00:00Z", "2026-03-04T00:00:00Z"),
        ("2026-03-04T00:00:00Z", "2026-03-06T00:00:00Z"),
        ("2026-03-06T00:00:00Z", "2026-03-13T00:00:00Z"),
    ]
    assert all(
        older["end_at_utc"] <= newer["start_at_utc"]
        for older, newer in zip(reversed(idx), list(reversed(idx))[1:])
    )


def test_codex_boundary_straddling_block_belongs_to_both_cycles(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        for start, reset in (
            ("2026-03-01T00:00:00+00:00", "2026-03-08T00:00:00+00:00"),
            ("2026-03-08T00:00:00+00:00", "2026-03-15T00:00:00+00:00"),
        ):
            _seed_quota_block(
                conn, source_root_key="root-a", window_minutes=10080,
                nominal_start_at_utc=start, resets_at_utc=reset,
            )
        _seed_quota_block(
            conn, source_root_key="root-a", logical_limit_key="five-hour",
            observed_slot="secondary", window_minutes=300,
            nominal_start_at_utc="2026-03-07T22:00:00+00:00",
            resets_at_utc="2026-03-08T03:00:00+00:00",
        )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a",),
            resets_at=_dt("2026-03-15T00:00:00+00:00"),
        )
        now = _dt("2026-03-10T00:00:00+00:00")
        idx = mh.build_codex_cycle_index(conn, identity=identity, now_utc=now)
        details = [
            mh.build_codex_cycle_detail(
                conn, None, identity=identity, key=e["key"], speed="auto",
                now_utc=now,
            )
            for e in idx
        ]
    finally:
        conn.close()

    assert [e["block_count"] for e in idx] == [1, 1]
    assert [len(detail["blocks"]) for detail in details] == [1, 1]
    assert details[0]["blocks"][0]["key"] == details[1]["blocks"][0]["key"]


# ── Codex cycle-index jitter canonicalization (ui-qa P2 real-data fix) ──
#
# Codex quota_window_blocks rows carry second-level jitter in resets_at, so one
# physical weekly reset surfaces as several rows whose resets differ by seconds.
# Before the fix, the per-row effective clip produced degenerate 1-second
# "cycles" (labels like "Jul 18–Jul 18"), inflating the index ~350×. The floor
# clusters jitter siblings into ONE cycle while keeping genuine re-anchors
# (hours apart) distinct.


def test_cluster_by_reset_jitter_collapses_subfloor():
    import _lib_milestone_history as lib

    clusters = lib.cluster_by_reset_jitter([0, 3, 5, 700, 704], reset_key=lambda x: x)
    assert [list(c) for c in clusters] == [[0, 3, 5], [700, 704]]


def test_cluster_by_reset_jitter_reanchor_hours_stay_distinct():
    import _lib_milestone_history as lib

    # 6 hours apart — a genuine early re-anchor, NOT capture jitter.
    clusters = lib.cluster_by_reset_jitter([0, 6 * 3600], reset_key=lambda x: x)
    assert len(clusters) == 2


def test_cluster_by_reset_jitter_weekly_stay_distinct():
    import _lib_milestone_history as lib

    clusters = lib.cluster_by_reset_jitter([0, 7 * 86400], reset_key=lambda x: x)
    assert len(clusters) == 2


def test_codex_cycle_jitter_floor_is_600():
    import _lib_milestone_history as lib

    assert lib.CODEX_CYCLE_JITTER_FLOOR_SECONDS == 600


def test_codex_cycle_index_jitter_collapses_to_one_entry(ns):
    import _cctally_milestone_history as mh

    start = "2026-03-01T00:00:00+00:00"
    base = _dt("2026-03-08T00:00:00+00:00")
    conn = ns["open_db"]()
    try:
        resets = [base + dt.timedelta(seconds=s) for s in (0, 3, 5)]
        for i, rz in enumerate(resets):
            _seed_quota_block(
                conn, source_root_key="root-a", window_minutes=10080,
                resets_at_utc=rz.isoformat(), nominal_start_at_utc=start,
            )
            # one milestone per jittered sibling reset (distinct percents) so a
            # correct union counts 3, not 1 (or a triple-counted degenerate).
            _seed_quota_milestone(
                conn, source_root_key="root-a", window_minutes=10080,
                resets_at_utc=rz.isoformat(), percent_threshold=i + 1,
                captured_at_utc="2026-03-02T00:00:00+00:00",
            )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a",), resets_at=resets[-1],
        )
        now = _dt("2026-03-05T00:00:00+00:00")
        idx = mh.build_codex_cycle_index(conn, identity=identity, now_utc=now)
    finally:
        conn.close()

    # Three jitter siblings collapse to ONE navigable cycle (was 3 pre-fix).
    assert len(idx) == 1
    entry = idx[0]
    # Canonical reset = the cluster max (latest observation wins).
    assert entry["resets_at_utc"] == "2026-03-08T00:00:05Z"
    # Non-degenerate 7-day span, not a 1-second "Mar 08–Mar 08".
    assert entry["start_at_utc"] == "2026-03-01T00:00:00Z"
    assert entry["end_at_utc"] == "2026-03-08T00:00:05Z"
    assert entry["start_at_utc"][:10] != entry["end_at_utc"][:10]
    # milestone_count unions across all three jittered resets.
    assert entry["milestone_count"] == 3


def test_codex_cycle_index_selects_one_live_cycle_across_nearby_identities(ns):
    """The live boundary may sit within the jitter floor of two distinct
    identity clusters.  Its selected full identity must break that ambiguity:
    one cycle is current/unclipped and the other keeps global successor
    clipping, so the account timeline never overlaps."""
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_quota_block(
            conn,
            source_root_key="root-a",
            logical_limit_key="limit-a",
            window_minutes=10080,
            nominal_start_at_utc="2026-03-07T23:54:00+00:00",
            resets_at_utc="2026-03-14T23:54:00+00:00",
        )
        _seed_quota_block(
            conn,
            source_root_key="root-b",
            logical_limit_key="limit-b",
            window_minutes=10080,
            nominal_start_at_utc="2026-03-08T00:06:00+00:00",
            resets_at_utc="2026-03-15T00:06:00+00:00",
        )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a", "root-b"),
            resets_at=_dt("2026-03-15T00:00:00+00:00"),
            quota_identity=types.SimpleNamespace(
                source_root_key="root-b",
                logical_limit_key="limit-b",
                observed_slot="primary",
                window_minutes=10080,
            ),
        )
        index = mh.build_codex_cycle_index(
            conn,
            identity=identity,
            now_utc=_dt("2026-03-10T00:00:00+00:00"),
        )
    finally:
        conn.close()

    assert len(index) == 2
    assert sum(entry["is_current"] for entry in index) == 1
    by_start = {entry["start_at_utc"]: entry for entry in index}
    earlier = by_start["2026-03-07T23:54:00Z"]
    live = by_start["2026-03-08T00:06:00Z"]
    assert live["is_current"] is True
    assert live["end_at_utc"] == live["resets_at_utc"]
    assert earlier["is_current"] is False
    assert earlier["end_at_utc"] == live["start_at_utc"]


def test_codex_cycle_detail_unions_member_milestones(ns, monkeypatch):
    import _cctally_milestone_history as mh

    start = "2026-03-01T00:00:00+00:00"
    base = _dt("2026-03-08T00:00:00+00:00")
    r0, r1 = base, base + dt.timedelta(seconds=3)

    def fake_breakdown(ident, reset, *, speed, cache_conn, stats_conn,
                       account_key=None):
        # #416: the merged route must keep reaching the breakdown unscoped —
        # asserting it here makes this double a byte-stability guard too, not
        # merely a signature mirror.
        assert account_key is None

        def mk(pct, cap):
            return types.SimpleNamespace(
                percent=pct, captured_at=_dt(cap), cost_usd=0.0,
                marginal_cost_usd=0.0, input_tokens=0, cached_input_tokens=0,
                output_tokens=0, reasoning_output_tokens=0, total_tokens=0,
            )
        if abs((reset - r0).total_seconds()) < 1:
            return (
                mk(1, "2026-03-02T00:00:00+00:00"),
                mk(2, "2026-03-02T02:00:00+00:00"),
            )
        return (
            mk(1, "2026-03-02T01:00:00+00:00"),
            mk(2, "2026-03-02T02:00:00+00:00"),
        )

    monkeypatch.setattr(mh, "codex_quota_breakdown", fake_breakdown)

    conn = ns["open_db"]()
    try:
        for rz in (r0, r1):
            _seed_quota_block(
                conn, source_root_key="root-a", window_minutes=10080,
                resets_at_utc=rz.isoformat(), nominal_start_at_utc=start,
            )
        conn.commit()
        identity = types.SimpleNamespace(source_root_keys=("root-a",), resets_at=r1)
        now = _dt("2026-03-05T00:00:00+00:00")
        idx = mh.build_codex_cycle_index(conn, identity=identity, now_utc=now)
        assert len(idx) == 1  # jitter collapsed
        detail = mh.build_codex_cycle_detail(
            conn, None, identity=identity, key=idx[0]["key"], speed="auto",
            now_utc=now,
        )
    finally:
        conn.close()

    assert isinstance(detail, dict)
    assert len(detail["segments"]) == 1
    percents = [m["percent"] for m in detail["segments"][0]["milestones"]]
    # Jitter siblings contribute evidence but one physical ledger has exactly
    # one earliest crossing per integer threshold.
    assert percents == [1, 2]
    assert len(percents) == len(set(percents))
    assert "reset_event_id" not in detail["segments"][0]


def test_codex_cycle_index_two_real_resets_7d_apart_two_entries(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        _seed_quota_block(
            conn, source_root_key="root-a", window_minutes=10080,
            nominal_start_at_utc="2026-03-01T00:00:00+00:00",
            resets_at_utc="2026-03-08T00:00:00+00:00",
        )
        _seed_quota_block(
            conn, source_root_key="root-a", window_minutes=10080,
            nominal_start_at_utc="2026-03-08T00:00:00+00:00",
            resets_at_utc="2026-03-15T00:00:00+00:00",
        )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a",),
            resets_at=_dt("2026-03-15T00:00:00+00:00"),
        )
        now = _dt("2026-03-12T00:00:00+00:00")
        idx = mh.build_codex_cycle_index(conn, identity=identity, now_utc=now)
    finally:
        conn.close()

    # Real weekly resets 7d apart stay two distinct cycles (no over-merge).
    assert len(idx) == 2
    resets = sorted(e["resets_at_utc"] for e in idx)
    assert resets == ["2026-03-08T00:00:00Z", "2026-03-15T00:00:00Z"]
    for e in idx:
        assert e["start_at_utc"][:10] != e["end_at_utc"][:10]


def test_codex_cycle_index_early_reanchor_6h_not_clustered(ns):
    import _cctally_milestone_history as mh

    conn = ns["open_db"]()
    try:
        # cycle 1: nominal 03-01 → reset 03-08.
        _seed_quota_block(
            conn, source_root_key="root-a", window_minutes=10080,
            nominal_start_at_utc="2026-03-01T00:00:00+00:00",
            resets_at_utc="2026-03-08T00:00:00+00:00",
        )
        # early re-anchor: new window whose reset is only 6h after the prior
        # reset — hours apart, so NOT jitter and MUST stay distinct.
        _seed_quota_block(
            conn, source_root_key="root-a", window_minutes=10080,
            nominal_start_at_utc="2026-03-07T18:00:00+00:00",
            resets_at_utc="2026-03-08T06:00:00+00:00",
        )
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a",),
            resets_at=_dt("2026-03-08T06:00:00+00:00"),
        )
        now = _dt("2026-03-07T12:00:00+00:00")
        idx = mh.build_codex_cycle_index(conn, identity=identity, now_utc=now)
    finally:
        conn.close()

    assert len(idx) == 2  # 6h apart → two cycles, not one jitter cluster
    by_reset = {e["resets_at_utc"]: e for e in idx}
    early = by_reset["2026-03-08T00:00:00Z"]
    # Prior cycle's end clipped to the re-anchor's nominal start.
    assert early["end_at_utc"] == "2026-03-07T18:00:00Z"
    assert early["start_at_utc"] == "2026-03-01T00:00:00Z"


# ── Task 3: envelope index field + idle-carry + hot-path guard ─────────

_UTC = dt.timezone.utc


def _pin_envelope_loaders(ns):
    """Pin host-touching loaders so snapshot_to_envelope is deterministic."""
    ns["_load_update_state"] = lambda: None
    ns["_load_update_suppress"] = lambda: {
        "skipped_versions": [], "remind_after": None,
    }
    ns["load_config"] = lambda *a, **k: {}

    def _raise_doctor(**_kw):
        raise RuntimeError("pinned: doctor disabled for envelope test")

    ns["doctor_gather_state"] = _raise_doctor


def _cw(ns):
    return ns["TuiCurrentWeek"](
        week_start_at=dt.datetime(2026, 5, 15, tzinfo=_UTC),
        week_end_at=dt.datetime(2026, 5, 22, tzinfo=_UTC),
        used_pct=20.0, five_hour_pct=None, five_hour_resets_at=None,
        spent_usd=3.0, dollars_per_percent=0.15,
        latest_snapshot_at=dt.datetime(2026, 5, 17, tzinfo=_UTC),
    )


def _make_cw_snapshot(ns, **extra):
    return ns["DataSnapshot"](
        current_week=_cw(ns), forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=dt.datetime(2026, 5, 17, 12, 0, tzinfo=_UTC),
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[], blocks_panel=[], daily_panel=[],
        **extra,
    )


def test_envelope_emits_week_index_under_current_week():
    ns = load_script()
    _pin_envelope_loaders(ns)
    entry = {
        "key": "2026-05-15", "start_at_utc": "2026-05-15T00:00:00Z",
        "end_at_utc": "2026-05-22T00:00:00Z", "label": "May 15–22",
        "is_current": True, "milestone_count": 3, "block_count": 2,
        "segment_count": 1, "detail_stamp": "abcd1234",
    }
    snap = _make_cw_snapshot(ns, week_index=[entry])
    env = ns["snapshot_to_envelope"](
        snap, now_utc=dt.datetime(2026, 5, 17, 12, 0, tzinfo=_UTC),
        monotonic_now=None,
    )
    assert env["current_week"]["week_index"] == [entry]


def test_envelope_week_index_defaults_empty_and_preserves_keys():
    ns = load_script()
    _pin_envelope_loaders(ns)
    # Snapshot WITHOUT week_index → defaults to [] (legacy/positional compat).
    snap = _make_cw_snapshot(ns)
    env = ns["snapshot_to_envelope"](
        snap, now_utc=dt.datetime(2026, 5, 17, 12, 0, tzinfo=_UTC),
        monotonic_now=None,
    )
    cw = env["current_week"]
    assert cw["week_index"] == []
    # Byte-stability: pre-existing current_week keys unchanged by the addition.
    assert cw["used_pct"] == 20.0
    assert cw["spent_usd"] == 3.0
    assert cw["milestones"] == []
    assert cw["five_hour_milestones"] == []
    assert cw["dollar_per_pct"] == 0.15


def test_week_index_built_on_non_idle_only(ns, monkeypatch):
    conn = ns["open_db"]()
    try:
        _seed_full(conn)
    finally:
        conn.close()

    calls = {"n": 0}
    real = ns["build_claude_week_index"]

    def counting(c):
        calls["n"] += 1
        return real(c)

    monkeypatch.setitem(ns, "build_claude_week_index", counting)
    now = dt.datetime(2026, 5, 18, 12, 0, tzinfo=_UTC)
    kw = dict(
        now_utc=now, skip_sync=True, precompute_envelope=True,
        runtime_bind="127.0.0.1",
    )
    ns["_tui_build_snapshot"](**kw)  # non-idle: builds the index once
    ns["_tui_build_snapshot"](**kw)  # idle short-circuit: no new query
    assert calls["n"] == 1


# ── Task 4: GET /api/milestones/<source>/week/<key> route ──────────────

import json as _json
import pathlib as _pathlib
import socketserver as _socketserver
import threading as _threading
import urllib.parse as _urlparse
from http.client import HTTPConnection


def _boot_milestones_server(ns, tmp_path, monkeypatch, *, seed):
    """Boot a real DashboardHTTPHandler over a seeded stats/cache DB.

    Mirrors tests/test_dashboard_api_block.py's handler harness.
    """
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(_pathlib.Path(ns["__file__"]).resolve().parent))
    conn = ns["open_db"]()
    try:
        seed(conn)
        conn.commit()
    finally:
        conn.close()
    # Ensure the cache DB exists (codex path opens it).
    ns["open_cache_db"]().close()

    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]
    DataSnapshot = ns["DataSnapshot"]
    snap = DataSnapshot(
        current_week=None, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=dt.datetime(2026, 5, 18, 12, 0, tzinfo=dt.timezone.utc),
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[], blocks_panel=[], daily_panel=[],
    )
    HandlerCls.snapshot_ref = SnapshotRef(snap)
    HandlerCls.hub = SSEHub()
    HandlerCls.sync_lock = _threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)
    HandlerCls.no_sync = True
    srv = _socketserver.TCPServer(("127.0.0.1", 0), HandlerCls)
    srv._test_thread = start_accept_loop(srv)
    return srv


def _get(srv, path):
    port = srv.server_address[1]
    c = HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    raw = r.read()
    body = _json.loads(raw) if raw else {}
    return r.status, body


def test_api_milestones_claude_week_200(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot_milestones_server(ns, tmp_path, monkeypatch, seed=_seed_full)
    try:
        conn = ns["open_db"]()
        try:
            key = next(
                e["key"] for e in ns["build_claude_week_index"](conn)
                if e["start_at_utc"] == "2026-05-15T00:00:00Z"
            )
        finally:
            conn.close()
        status, body = _get(
            srv, "/api/milestones/claude/week/" + _urlparse.quote(key, safe="")
        )
        assert status == 200, (status, body)
        assert body["source"] == "claude"
        assert body["key"] == key
        assert "segments" in body and "dividers" in body and "blocks" in body
        assert body["detail_stamp"]
        assert {b["five_hour_window_key"] for b in body["blocks"]} >= {5155, 5149}
    finally:
        stop(srv, srv._test_thread)


def test_api_milestones_claude_reset_cycles_fetch_independently(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot_milestones_server(ns, tmp_path, monkeypatch, seed=_seed_full)
    try:
        conn = ns["open_db"]()
        try:
            keys = [
                e["key"] for e in ns["build_claude_week_index"](conn)
                if e["start_at_utc"] < "2026-05-08T00:00:00Z"
                and e["end_at_utc"] > "2026-05-01T00:00:00Z"
            ]
        finally:
            conn.close()
        bodies = []
        for key in keys:
            status, body = _get(
                srv, "/api/milestones/claude/week/" + _urlparse.quote(key, safe="")
            )
            assert status == 200, (status, body)
            bodies.append(body)
        assert [len(body["segments"][0]["milestones"]) for body in bodies] == [2, 2]
        assert all(body["dividers"] == [] for body in bodies)
    finally:
        stop(srv, srv._test_thread)


def test_api_milestones_bad_source_400(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot_milestones_server(ns, tmp_path, monkeypatch, seed=_seed_full)
    try:
        status, _body = _get(srv, "/api/milestones/nope/week/2026-05-15")
        assert status == 400
    finally:
        stop(srv, srv._test_thread)


def test_api_milestones_malformed_claude_key_400(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot_milestones_server(ns, tmp_path, monkeypatch, seed=_seed_full)
    try:
        status, _body = _get(srv, "/api/milestones/claude/week/not-a-date")
        assert status == 400
    finally:
        stop(srv, srv._test_thread)


def test_api_milestones_claude_unknown_week_404(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot_milestones_server(ns, tmp_path, monkeypatch, seed=_seed_full)
    try:
        from _lib_dashboard_sources import dashboard_resource_key
        unknown = dashboard_resource_key(
            "milestone_cycle", "claude", "2099-01-01", "start", "end"
        )
        status, body = _get(
            srv, "/api/milestones/claude/week/" + _urlparse.quote(unknown, safe="")
        )
        assert status == 404
        assert body["code"] == "unknown_key"
        assert body["reason"] == "unknown"
    finally:
        stop(srv, srv._test_thread)


def _seed_codex_cycle(conn):
    _seed_quota_block(
        conn, source_root_key="root-a", window_minutes=10080,
        resets_at_utc="2026-03-08T00:00:00+00:00",
        nominal_start_at_utc="2026-03-01T00:00:00+00:00",
    )


def test_api_milestones_codex_cycle_200(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot_milestones_server(ns, tmp_path, monkeypatch, seed=_seed_codex_cycle)
    try:
        import _cctally_milestone_history as mh
        conn = ns["open_db"]()
        try:
            identity = types.SimpleNamespace(
                source_root_keys=("root-a",),
                resets_at=_dt("2026-03-08T00:00:00+00:00"),
            )
            idx = mh.build_codex_cycle_index(
                conn, identity=identity, now_utc=_dt("2026-03-05T00:00:00+00:00"),
            )
            key = idx[0]["key"]
        finally:
            conn.close()
        status, body = _get(srv, "/api/milestones/codex/week/" + _urlparse.quote(key, safe=""))
        assert status == 200, (status, body)
        assert body["source"] == "codex"
        assert body["key"] == key
        assert body["dividers"] == []
        assert "segments" in body and "blocks" in body
    finally:
        stop(srv, srv._test_thread)


_DETAIL_AS_OF = "2026-07-26T00:00:00Z"
_DETAIL_ROOT = "root-a"
_DETAIL_LIMIT_KEY = "account"


def _seed_codex_reanchored_cycles(conn):
    """A seven-day cycle cut short by a genuine early re-anchor (#373).

    The `Jul 21` cycle's own reset is still in the future at `_DETAIL_AS_OF`,
    so `_codex_is_current`'s `cyc.reset > now_utc` fallthrough calls it current
    even though the `Jul 25` re-anchor already ended it. That is precisely the
    shape that made the detail route and the index disagree.
    """
    _seed_quota_block(
        conn, source_root_key=_DETAIL_ROOT, logical_limit_key=_DETAIL_LIMIT_KEY,
        window_minutes=10080, limit_id="codex",
        nominal_start_at_utc="2026-07-21T17:02:32+00:00",
        resets_at_utc="2026-07-28T17:02:32+00:00", current_percent=28.0,
    )
    _seed_quota_block(
        conn, source_root_key=_DETAIL_ROOT, logical_limit_key=_DETAIL_LIMIT_KEY,
        window_minutes=10080, limit_id="codex",
        nominal_start_at_utc="2026-07-25T19:18:58+00:00",
        resets_at_utc="2026-08-01T19:18:58+00:00", current_percent=2.0,
    )


def _seed_codex_live_weekly_evidence(cache_conn):
    """The physical evidence the live-cycle resolver reads, so the detail route
    can resolve the same `CodexCycleBoundary` the index was built with."""
    cache_conn.execute(
        "INSERT INTO codex_source_roots "
        "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
        "VALUES (?, '/tmp/codex-root', ?, ?)",
        (_DETAIL_ROOT, "2026-07-21T17:02:32Z", "2026-07-25T23:30:00Z"),
    )
    cache_conn.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc,"
        " observed_slot, logical_limit_key, limit_id, limit_name, window_minutes,"
        " used_percent, resets_at_utc) "
        "VALUES ('codex', ?, '/tmp/rollout.jsonl', 1, '2026-07-25T23:30:00Z',"
        " 'primary', ?, 'codex', NULL, 10080, 2.0, '2026-08-01T19:18:58Z')",
        (_DETAIL_ROOT, _DETAIL_LIMIT_KEY),
    )
    cache_conn.commit()


def test_api_milestones_codex_detail_agrees_with_the_index(tmp_path, monkeypatch):
    """#373 — one cycle key must describe ONE cycle.

    Browser QA caught `GET /api/milestones/codex/week/<key>` returning
    `Jul 21–Jul 28` / `is_current: true` for the very key whose index entry said
    `Jul 21–Jul 25` / `is_current: false`. The detail route has no envelope, so
    it passed a stub identity with no `resets_at`; `_codex_is_current` then fell
    through to `cyc.reset > now_utc` and marked every future-ending cycle
    current — which both flipped the flag and, after #373's clip guard, left the
    label unclipped.
    """
    monkeypatch.setenv("CCTALLY_AS_OF", _DETAIL_AS_OF)
    ns = load_script()
    srv = _boot_milestones_server(
        ns, tmp_path, monkeypatch, seed=_seed_codex_reanchored_cycles,
    )
    try:
        cache_conn = ns["open_cache_db"]()
        try:
            _seed_codex_live_weekly_evidence(cache_conn)
        finally:
            cache_conn.close()

        import _cctally_dashboard_sources as ds
        import _cctally_milestone_history as mh
        now = _dt("2026-07-26T00:00:00+00:00")
        conn = ns["open_db"]()
        cache_conn = ns["open_cache_db"]()
        try:
            identity = ds.resolve_codex_cycle_detail_identity(
                cache_conn, source_root_keys=(_DETAIL_ROOT,), now_utc=now,
            )
            # Positive precondition: the live boundary really did resolve, or
            # the index below would fall through to the same broken proxy.
            assert identity.resets_at == _dt("2026-08-01T19:18:58+00:00")
            index = mh.build_codex_cycle_index(
                conn, identity=identity, now_utc=now,
            )
        finally:
            conn.close()
            cache_conn.close()
        entry = next(
            e for e in index if e["start_at_utc"] == "2026-07-21T17:02:32Z"
        )
        # Positive precondition: the index really did clip this cycle short and
        # call it historic, so there is a disagreement to detect.
        assert entry["end_at_utc"] == "2026-07-25T19:18:58Z"
        assert entry["is_current"] is False

        status, body = _get(
            srv, "/api/milestones/codex/week/" + _urlparse.quote(entry["key"], safe=""),
        )
        assert status == 200, (status, body)
        assert body["end_at_utc"] == entry["end_at_utc"]
        assert body["label"] == entry["label"]
        assert body["is_current"] == entry["is_current"]
    finally:
        stop(srv, srv._test_thread)


def test_api_milestones_codex_unknown_key_404(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot_milestones_server(ns, tmp_path, monkeypatch, seed=_seed_codex_cycle)
    try:
        status, body = _get(
            srv, "/api/milestones/codex/week/" + _urlparse.quote("milestone_cycle:bogus", safe=""),
        )
        assert status == 404
        assert body["code"] == "unknown_key"
        assert body["reason"] in {"pruned", "rebuild_pending", "projection_incoherent", "unknown"}
    finally:
        stop(srv, srv._test_thread)


# ── #834 S1 (#836): one window, two accounts, no shared milestones ──────


def _seed_account_block(
    conn, *, window_key, block_start_at, five_hour_resets_at, account_key,
    final_five_hour_percent,
) -> int:
    """`_seed_block`'s account-stamped twin. Block uniqueness is
    `(account_key, five_hour_window_key)`, so one physical window owns one block
    per account."""
    cur = conn.execute(
        "INSERT INTO five_hour_blocks "
        "(five_hour_window_key, five_hour_resets_at, block_start_at, "
        " first_observed_at_utc, last_observed_at_utc, final_five_hour_percent, "
        " crossed_seven_day_reset, total_cost_usd, is_closed, "
        " created_at_utc, last_updated_at_utc, account_key) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 1.0, 1, ?, ?, ?)",
        (window_key, five_hour_resets_at, block_start_at, block_start_at,
         five_hour_resets_at, final_five_hour_percent, block_start_at,
         five_hour_resets_at, account_key),
    )
    return int(cur.lastrowid)


def _seed_account_5h_milestone(
    conn, *, block_id, window_key, percent_threshold, captured_at_utc,
    account_key, usage_snapshot_id=1,
):
    conn.execute(
        "INSERT INTO five_hour_milestones "
        "(block_id, five_hour_window_key, percent_threshold, captured_at_utc, "
        " usage_snapshot_id, block_cost_usd, reset_event_id, account_key) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (block_id, window_key, percent_threshold, captured_at_utc,
         usage_snapshot_id, 0.5, account_key),
    )


def test_836_one_window_two_accounts_do_not_share_milestones(ns):
    """#834 S1 (#836). The historical-detail route selected its blocks without an
    account and then loaded each one's milestones by a bare
    `five_hour_window_key`, while block uniqueness is
    `(account_key, five_hour_window_key)`. One physical five-hour window
    therefore owned one block per account and every block showed EVERY account's
    milestones.

    Blocks are identified here by `final_five_hour_percent` rather than by an
    account field, because the route's published `blocks[]` shape does not carry
    one and this test must not require it to."""
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        resets = "2026-05-16T05:00:00+00:00"
        start = "2026-05-16T00:00:00+00:00"
        window_key = ns["_canonical_5h_window_key"](
            int(dt.datetime.fromisoformat(resets).timestamp()))
        alice = _seed_account_block(
            conn, window_key=window_key, block_start_at=start,
            five_hour_resets_at=resets, account_key="acct-alice",
            final_five_hour_percent=5.0)
        bob = _seed_account_block(
            conn, window_key=window_key, block_start_at=start,
            five_hour_resets_at=resets, account_key="acct-bob",
            final_five_hour_percent=9.0)
        _seed_account_5h_milestone(
            conn, block_id=alice, window_key=window_key, percent_threshold=1,
            captured_at_utc="2026-05-16T01:00:00+00:00",
            account_key="acct-alice")
        _seed_account_5h_milestone(
            conn, block_id=bob, window_key=window_key, percent_threshold=2,
            captured_at_utc="2026-05-16T02:00:00+00:00",
            account_key="acct-bob")
        conn.commit()

        blocks = mh._build_blocks(conn, WK_A_START, WK_A_END)
        by_final = {b["final_five_hour_percent"]: b for b in blocks}
        assert set(by_final) == {5.0, 9.0}, (
            f"expected one block per account, got {blocks!r}")
        assert [m["percent_threshold"] for m in by_final[5.0]["milestones"]] == [1]
        assert [m["percent_threshold"] for m in by_final[9.0]["milestones"]] == [2]
    finally:
        conn.close()


@pytest.mark.parametrize("insertion_order", [("acct-alice", "acct-bob"),
                                           ("acct-bob", "acct-alice")])
def test_839_same_window_history_keeps_each_accounts_credit_and_identity(
    ns, insertion_order,
):
    """Both physical row orders must preserve each block's own credit and owner."""
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        for account, label in (("acct-alice", "Alice"), ("acct-bob", "Bob")):
            conn.execute(
                "INSERT INTO accounts (account_key, provider, natural_id, label) "
                "VALUES (?, 'claude', ?, ?)", (account, account, label))
        resets = "2026-05-16T05:00:00+00:00"
        start = "2026-05-16T00:00:00+00:00"
        wk = ns["_canonical_5h_window_key"](int(dt.datetime.fromisoformat(resets).timestamp()))
        by_account = {"acct-alice": (5.0, 1, 4.0), "acct-bob": (9.0, 2, 17.0)}
        for account in insertion_order:
            final, threshold, credit = by_account[account]
            block_id = _seed_account_block(
                conn, window_key=wk, block_start_at=start,
                five_hour_resets_at=resets, account_key=account,
                final_five_hour_percent=final)
            _seed_account_5h_milestone(
                conn, block_id=block_id, window_key=wk,
                percent_threshold=threshold,
                captured_at_utc="2026-05-16T01:00:00+00:00",
                account_key=account)
            conn.execute(
                "INSERT INTO five_hour_reset_events "
                "(detected_at_utc, five_hour_window_key, prior_percent, "
                " post_percent, effective_reset_at_utc, account_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-05-16T02:00:00+00:00", wk, 1.0, 1.0 + credit,
                 "2026-05-16T02:00:00+00:00", account))
        conn.commit()

        blocks = mh._build_blocks(conn, WK_A_START, WK_A_END)
        assert len(blocks) == 2
        assert {b["account_key"]: (
            b["account_label"],
            [m["percent_threshold"] for m in b["milestones"]],
            [credit["delta_pp"] for credit in b["credits"]],
        ) for b in blocks} == {
            "acct-alice": ("Alice", [1], [4.0]),
            "acct-bob": ("Bob", [2], [17.0]),
        }
    finally:
        conn.close()


def test_839_single_account_history_keeps_legacy_block_wire(ns):
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO accounts (account_key, provider, natural_id) "
            "VALUES ('acct-alice', 'claude', 'acct-alice')")
        _seed_account_block(
            conn, window_key=5155, block_start_at="2026-05-16T00:00:00+00:00",
            five_hour_resets_at="2026-05-16T05:00:00+00:00",
            account_key="acct-alice", final_five_hour_percent=5.0)
        conn.commit()
        block, = mh._build_blocks(conn, WK_A_START, WK_A_END)
        assert "account_key" not in block
        assert "account_label" not in block
    finally:
        conn.close()


def test_839_decorated_detail_stamp_moves_for_other_account_5h_changes(ns):
    """The client must refetch when an account-owned credit or crossing arrives."""
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        for account in ("acct-alice", "acct-bob"):
            conn.execute(
                "INSERT INTO accounts (account_key, provider, natural_id) "
                "VALUES (?, 'claude', ?)", (account, account))
        _seed_usage(
            conn, captured_at_utc="2026-05-16T00:00:00+00:00",
            week_start_date="2026-05-15", week_start_at=WK_A_START,
            week_end_at=WK_A_END, weekly_percent=23.0)
        conn.execute(
            "UPDATE weekly_usage_snapshots SET account_key='acct-bob' "
            "WHERE week_start_date='2026-05-15'")
        reset = "2026-05-16T05:00:00+00:00"
        start = "2026-05-16T00:00:00+00:00"
        wk = ns["_canonical_5h_window_key"](int(dt.datetime.fromisoformat(reset).timestamp()))
        alice = _seed_account_block(
            conn, window_key=wk, block_start_at=start,
            five_hour_resets_at=reset, account_key="acct-alice",
            final_five_hour_percent=5.0)
        _seed_account_block(
            conn, window_key=wk, block_start_at=start,
            five_hour_resets_at=reset, account_key="acct-bob",
            final_five_hour_percent=19.0)
        conn.commit()
        stamp = mh.build_claude_week_index(conn)[0]["detail_stamp"]

        conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, prior_percent, "
            " post_percent, effective_reset_at_utc, account_key) "
            "VALUES (?, ?, 1, 5, ?, 'acct-alice')",
            ("2026-05-16T02:00:00+00:00", wk,
             "2026-05-16T02:00:00+00:00"))
        conn.commit()
        credit_stamp = mh.build_claude_week_index(conn)[0]["detail_stamp"]
        assert credit_stamp != stamp

        _seed_account_5h_milestone(
            conn, block_id=alice, window_key=wk,
            percent_threshold=4, captured_at_utc="2026-05-16T03:00:00+00:00",
            account_key="acct-alice")
        conn.commit()
        assert mh.build_claude_week_index(conn)[0]["detail_stamp"] != credit_stamp
    finally:
        conn.close()


# ── #836: one account's history on a multi-account store (spec 5.4-5.6) ──
#
# On a store with more than one REAL Claude account the Current Week card
# describes ONE account (the owner of the newest non-held weekly sample), and
# U1 makes its history navigator walk only that account's history. These
# tests detect: another account's cycles, cohorts, blocks or credits in a
# scoped payload; a stamp that does not move when one of the scope's own
# crossings loses (or regains) its weekly join; an index whose current entry
# describes a different interval than the card; and a route that answers an
# account-qualified request with the merged week.

ACCT_836_A = "1111111111111111111111111111aaaa"
ACCT_836_B = "2222222222222222222222222222bbbb"
ACCT_836_CODEX = "3333333333333333333333333333cccc"
_836_NOW = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
_836_EFFECTIVE = "2026-05-18T00:00:00+00:00"


def _seed_836_account(conn, key, label, provider="claude"):
    conn.execute(
        "INSERT INTO accounts (account_key, provider, natural_id, label) "
        "VALUES (?, ?, ?, ?)", (key, provider, key, label))


def _seed_836_usage(conn, *, account_key, captured_at_utc, weekly_percent,
                    week_start_date="2026-05-15", week_start_at=WK_A_START,
                    week_end_at=WK_A_END, held=0, payload_json="{}") -> int:
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, account_key, "
        " weekly_observation_held) "
        "VALUES (?, ?, ?, ?, ?, ?, 'test', ?, ?, ?)",
        (captured_at_utc, week_start_date,
         (week_end_at or "2026-05-22")[:10], week_start_at, week_end_at,
         weekly_percent, payload_json, account_key, held),
    )
    return int(cur.lastrowid)


def _seed_836_milestone(conn, *, account_key, threshold, captured_at_utc,
                        reset_event_id=0, week_start_date="2026-05-15",
                        week_start_at=WK_A_START, week_end_at=WK_A_END):
    conn.execute(
        "INSERT INTO percent_milestones "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, percent_threshold, cumulative_cost_usd, "
        " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id, "
        " reset_event_id, account_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, 0, 0, ?, ?)",
        (captured_at_utc, week_start_date,
         (week_end_at or "2026-05-22")[:10], week_start_at, week_end_at,
         threshold, float(threshold), reset_event_id, account_key),
    )


def _seed_836_reset(conn, *, account_key, effective):
    cur = conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, account_key) VALUES (?, ?, ?, ?, ?)",
        (effective, effective, WK_A_END, effective, account_key))
    return int(cur.lastrowid)


def _836_window(ns, resets="2026-05-16T05:00:00+00:00"):
    return ns["_canonical_5h_window_key"](
        int(dt.datetime.fromisoformat(resets).timestamp()))


def _seed_836_two_accounts(ns, conn):
    """A and B share week WK_A. B had an in-place weekly credit inside it
    (its own reset event, post-credit milestones 1-2); A has milestones 1-3
    with no reset and an older week WK_B. Each owns a block in the same 5h
    window; B's carries a 5h credit. An `unattributed` milestone and block sit
    in the same week. A owns the newest non-held weekly sample."""
    _seed_836_account(conn, ACCT_836_A, "Alice")
    _seed_836_account(conn, ACCT_836_B, "Bob")
    _seed_836_usage(conn, account_key=ACCT_836_A,
                    captured_at_utc="2026-05-09T09:00:00Z", weekly_percent=7.0,
                    week_start_date="2026-05-08", week_start_at=WK_B_START,
                    week_end_at=WK_B_END)
    _seed_836_milestone(conn, account_key=ACCT_836_A, threshold=1,
                        captured_at_utc="2026-05-09T08:00:00Z",
                        week_start_date="2026-05-08",
                        week_start_at=WK_B_START, week_end_at=WK_B_END)
    _seed_836_usage(conn, account_key=ACCT_836_B,
                    captured_at_utc="2026-05-19T09:00:00Z", weekly_percent=2.0)
    a_snap = _seed_836_usage(conn, account_key=ACCT_836_A,
                             captured_at_utc="2026-05-19T10:00:00Z",
                             weekly_percent=3.0)
    bob_reset = _seed_836_reset(conn, account_key=ACCT_836_B,
                                effective=_836_EFFECTIVE)
    for t in (1, 2, 3):
        _seed_836_milestone(conn, account_key=ACCT_836_A, threshold=t,
                            captured_at_utc=f"2026-05-16T1{t}:00:00Z")
    for t in (1, 2, 3, 4):
        _seed_836_milestone(conn, account_key=ACCT_836_B, threshold=t,
                            captured_at_utc=f"2026-05-16T0{t}:00:00Z")
    for t in (1, 2):
        _seed_836_milestone(conn, account_key=ACCT_836_B, threshold=t,
                            captured_at_utc=f"2026-05-19T0{t}:00:00Z",
                            reset_event_id=bob_reset)
    _seed_836_milestone(conn, account_key=UNATTRIBUTED_836, threshold=9,
                        captured_at_utc="2026-05-16T20:00:00Z")
    wk = _836_window(ns)
    start = "2026-05-16T00:00:00+00:00"
    resets = "2026-05-16T05:00:00+00:00"
    blocks = {}
    for account, final in ((ACCT_836_A, 5.0), (ACCT_836_B, 9.0),
                           (UNATTRIBUTED_836, 1.0)):
        blocks[account] = _seed_account_block(
            conn, window_key=wk, block_start_at=start,
            five_hour_resets_at=resets, account_key=account,
            final_five_hour_percent=final)
    _seed_account_5h_milestone(
        conn, block_id=blocks[ACCT_836_A], window_key=wk, percent_threshold=1,
        captured_at_utc="2026-05-16T01:00:00+00:00", account_key=ACCT_836_A,
        usage_snapshot_id=a_snap)
    conn.execute(
        "INSERT INTO five_hour_reset_events "
        "(detected_at_utc, five_hour_window_key, prior_percent, "
        " post_percent, effective_reset_at_utc, account_key) "
        "VALUES (?, ?, 1, 5, ?, ?)",
        ("2026-05-16T02:00:00+00:00", wk, "2026-05-16T02:00:00+00:00",
         ACCT_836_B))
    conn.commit()
    return {"a_snap": a_snap, "bob_reset": bob_reset, "window": wk,
            "blocks": blocks}


UNATTRIBUTED_836 = "unattributed"


def _836_scope(ns, conn, account, now=_836_NOW):
    return ns["_claude_current_cycle_scope"](conn, now, account_key=account)


def _836_index(ns, conn, account, now=_836_NOW):
    import _cctally_milestone_history as mh
    return mh.build_claude_week_index(
        conn, account_key=account, cycle_scope=_836_scope(ns, conn, account, now),
        now_utc=now)


def _bounds(index):
    return [(e["start_at_utc"], e["end_at_utc"]) for e in index]


def test_836_a_scoped_index_holds_only_its_accounts_cycles_and_cohorts(ns):
    """U1. Detects: B's in-place credit cutting A's week, B's post-credit
    crossings or blocks counted in A's cycle, and `unattributed` rows entering
    a real account's history."""
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        _seed_836_two_accounts(ns, conn)
        merged = mh.build_claude_week_index(conn)
        alice = _836_index(ns, conn, ACCT_836_A)
        bob = _836_index(ns, conn, ACCT_836_B)
        lone = mh.build_claude_week_index(
            conn, account_key=UNATTRIBUTED_836, now_utc=_836_NOW)
    finally:
        conn.close()
    # non-vacuity: the merged index IS cut by Bob's credit.
    assert ("2026-05-18T00:00:00Z", "2026-05-22T00:00:00Z") in _bounds(merged)
    assert _bounds(alice) == [
        ("2026-05-15T00:00:00Z", "2026-05-22T00:00:00Z"),
        ("2026-05-08T00:00:00Z", "2026-05-15T00:00:00Z"),
    ]
    assert [(e["milestone_count"], e["block_count"], e["is_current"])
            for e in alice] == [(3, 1, True), (1, 0, False)]
    assert _bounds(bob) == [
        ("2026-05-18T00:00:00Z", "2026-05-22T00:00:00Z"),
        ("2026-05-15T00:00:00Z", "2026-05-18T00:00:00Z"),
    ]
    assert [(e["milestone_count"], e["is_current"]) for e in bob] == [
        (2, True), (4, False)]
    assert [e["milestone_count"] for e in lone] == [1]
    assert not any(e["is_current"] for e in lone)
    # One cycle key is shared across accounts with the same boundaries, so the
    # stamp names its scope.
    assert alice[0]["key"] != bob[0]["key"]
    assert lone[0]["key"] == alice[0]["key"]
    assert lone[0]["detail_stamp"] != alice[0]["detail_stamp"]


def test_836_a_scoped_detail_holds_only_its_accounts_blocks_and_milestones(ns):
    """U1 / spec 3 Consequence. Detects: a scoped detail whose `blocks[]`
    steps into another account's block in the same window, or a key from
    another account's index resolving under this one."""
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        _seed_836_two_accounts(ns, conn)
        alice = _836_index(ns, conn, ACCT_836_A)
        bob = _836_index(ns, conn, ACCT_836_B)
        a_detail = mh.build_claude_week_detail(
            conn, alice[0]["key"], account_key=ACCT_836_A, now_utc=_836_NOW)
        b_detail = mh.build_claude_week_detail(
            conn, bob[1]["key"], account_key=ACCT_836_B, now_utc=_836_NOW)
        foreign = mh.build_claude_week_detail(
            conn, bob[0]["key"], account_key=ACCT_836_A, now_utc=_836_NOW)
    finally:
        conn.close()
    assert foreign is None
    assert a_detail["detail_stamp"] == alice[0]["detail_stamp"]
    assert a_detail["is_current"] is True
    assert [m["percent"] for m in a_detail["segments"][0]["milestones"]] == [
        1, 2, 3]
    [block] = a_detail["blocks"]
    assert (block["account_key"], block["account_label"]) == (
        ACCT_836_A, "Alice")
    assert [m["percent_threshold"] for m in block["milestones"]] == [1]
    assert block["credits"] == []
    assert [m["percent"] for m in b_detail["segments"][0]["milestones"]] == [
        1, 2, 3, 4]
    [b_block] = b_detail["blocks"]
    assert b_block["account_key"] == ACCT_836_B
    assert [c["delta_pp"] for c in b_block["credits"]] == [4.0]


def test_836_scoped_stamps_move_for_their_own_5h_changes_only(ns):
    """Detects: a scoped stamp that another account's 5h crossing or credit
    moves (a refetch storm) or that the scope's own new crossing does not
    move (a stale cached detail)."""
    conn = ns["open_db"]()
    try:
        seeded = _seed_836_two_accounts(ns, conn)
        stamp = _836_index(ns, conn, ACCT_836_A)[0]["detail_stamp"]
        _seed_account_5h_milestone(
            conn, block_id=seeded["blocks"][ACCT_836_B],
            window_key=seeded["window"], percent_threshold=3,
            captured_at_utc="2026-05-16T03:00:00+00:00",
            account_key=ACCT_836_B)
        conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, prior_percent, "
            " post_percent, effective_reset_at_utc, account_key) "
            "VALUES (?, ?, 2, 4, ?, ?)",
            ("2026-05-16T04:00:00+00:00", seeded["window"],
             "2026-05-16T04:00:00+00:00", ACCT_836_B))
        conn.commit()
        assert _836_index(ns, conn, ACCT_836_A)[0]["detail_stamp"] == stamp
        _seed_account_5h_milestone(
            conn, block_id=seeded["blocks"][ACCT_836_A],
            window_key=seeded["window"], percent_threshold=2,
            captured_at_utc="2026-05-16T02:00:00+00:00",
            account_key=ACCT_836_A, usage_snapshot_id=seeded["a_snap"])
        conn.commit()
        assert _836_index(ns, conn, ACCT_836_A)[0]["detail_stamp"] != stamp
    finally:
        conn.close()


def _836_single_account_join_store(ns, conn):
    """One real account; two 5h crossings joined to two unheld snapshots."""
    _seed_836_account(conn, ACCT_836_A, "Alice")
    snaps = [
        _seed_836_usage(conn, account_key=ACCT_836_A,
                        captured_at_utc=f"2026-05-16T0{h}:00:00Z",
                        weekly_percent=float(h))
        for h in (1, 2)
    ]
    wk = _836_window(ns)
    block = _seed_account_block(
        conn, window_key=wk, block_start_at="2026-05-16T00:00:00+00:00",
        five_hour_resets_at="2026-05-16T05:00:00+00:00",
        account_key=ACCT_836_A, final_five_hour_percent=5.0)
    for threshold, snap in zip((1, 2), snaps):
        _seed_account_5h_milestone(
            conn, block_id=block, window_key=wk, percent_threshold=threshold,
            captured_at_utc=f"2026-05-16T0{threshold}:00:00+00:00",
            account_key=ACCT_836_A, usage_snapshot_id=snap)
    conn.commit()
    return snaps


def _836_retire(conn, snapshot_id):
    """The state a weekly credit's cleanup leaves: an UNHELD row turned held
    with no held-write marker in its payload (spec 5.4)."""
    conn.execute("UPDATE weekly_usage_snapshots SET weekly_observation_held=1 "
                 "WHERE id=?", (snapshot_id,))
    conn.commit()


def _836_restore(conn, snapshot_id):
    """The state convergence leaves once the suppressor no longer names the
    row (`tests/test_band_removal_840.py`): held back to 0."""
    conn.execute("UPDATE weekly_usage_snapshots SET weekly_observation_held=0 "
                 "WHERE id=?", (snapshot_id,))
    conn.commit()


@pytest.mark.parametrize("transition", ["suppress", "delete", "held_write"])
def test_836_the_stamp_moves_when_a_crossings_weekly_join_becomes_unavailable(
        ns, transition):
    """D-F (A8). Detects: a suppression, or a snapshot DELETE that rewrites
    the crossing's join to the `0` sentinel, leaving every count, max id and
    capture of the stamp unchanged, so the client keeps rendering the cached
    detail's retired value as a percentage. A genuine held WRITE (marker in
    the payload) keeps its value and must not move the stamp."""
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        snaps = _836_single_account_join_store(ns, conn)
        before = mh.build_claude_week_index(conn)[0]["detail_stamp"]
        if transition == "suppress":
            _836_retire(conn, snaps[0])
        elif transition == "delete":
            conn.execute("DELETE FROM weekly_usage_snapshots WHERE id=?",
                         (snaps[0],))
            conn.commit()
            assert conn.execute(
                "SELECT COUNT(*) FROM five_hour_milestones "
                "WHERE usage_snapshot_id=0").fetchone()[0] == 1
        else:
            conn.execute(
                "UPDATE weekly_usage_snapshots SET weekly_observation_held=1, "
                "payload_json='{\"weeklyObservationHeld\": true}' WHERE id=?",
                (snaps[0],))
            conn.commit()
        after = mh.build_claude_week_index(conn)[0]["detail_stamp"]
    finally:
        conn.close()
    if transition == "held_write":
        assert after == before
    else:
        assert after != before


def test_836_a_count_preserving_retirement_swap_still_moves_the_stamp(ns):
    """R2-1. Crossing A retired and B available, then A restored and B
    retired: every count, sum and max id is equal before and after. Detects:
    a count-based component, under which the client keeps the detail that
    renders A's restored value as `—` and B's retired value as a percentage."""
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        snaps = _836_single_account_join_store(ns, conn)
        _836_retire(conn, snaps[0])
        before = mh.build_claude_week_index(conn)[0]["detail_stamp"]
        _836_restore(conn, snaps[0])
        _836_retire(conn, snaps[1])
        after = mh.build_claude_week_index(conn)[0]["detail_stamp"]
    finally:
        conn.close()
    assert after != before


def test_836_a_clean_single_account_stamp_is_byte_stable(ns):
    """Spec 5.4 nonempty-only. Detects: an unavailable-join component appended
    when no crossing is unavailable, which would move every single-account
    install's stamps (and force a refetch) for nothing."""
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        _836_single_account_join_store(ns, conn)
        entry = mh.build_claude_week_index(conn)[0]
        rows = mh._claude_cycle_rows(conn, ns["make_week_ref"](
            week_start_date="2026-05-15", week_end_date="2026-05-22",
            week_start_at=WK_A_START, week_end_at=WK_A_END))
    finally:
        conn.close()
    assert entry["detail_stamp"] == mh._mh.compute_detail_stamp(
        entry["key"], len(rows), 1, 1 if rows else 0,
        max((r["captured_at_utc"] for r in rows), default=None))


def _836_oracle_unavailable_ids(conn, start_iso, end_iso, account_key):
    """The per-cycle query the unavailable-join component originally ran
    once per index entry (#836 spec 5.4): the oracle a per-build computation
    must agree with byte for byte."""
    from _cctally_five_hour import weekly_join_unavailable_sql
    if not start_iso or not end_iso:
        return []
    acct_sql, params = ("", ()) if account_key is None else (
        " AND b.account_key = ?", (account_key,))
    return [int(r[0]) for r in conn.execute(
        "SELECT m.id FROM five_hour_blocks b "
        "JOIN five_hour_milestones m ON m.block_id = b.id "
        "LEFT JOIN weekly_usage_snapshots s ON s.id = m.usage_snapshot_id "
        "WHERE unixepoch(b.block_start_at) < unixepoch(?) "
        "AND unixepoch(b.five_hour_resets_at) > unixepoch(?)" + acct_sql
        + " AND " + weekly_join_unavailable_sql(conn, "s")
        + " ORDER BY m.id",
        (end_iso, start_iso) + params,
    ).fetchall()]


def _836_multi_cycle_join_store(ns, conn):
    """Two real accounts and `unattributed` over three weeks; Bob's in-place
    credit cuts WK_A at 05-18. 5h blocks straddle the WK_C/WK_B and WK_B/WK_A
    boundaries (one start spelled with a +02:00 offset) and Bob's cut, and
    their crossings join an available snapshot, a held WRITE (available), an
    absent snapshot, the ``0`` sentinel or a suppression-retired snapshot. Bob
    has no block in WK_C, so his scoped WK_C set is empty. One block's bounds
    do not parse, so SQLite's ``unixepoch`` is NULL for it."""
    _seed_836_account(conn, ACCT_836_A, "Alice")
    _seed_836_account(conn, ACCT_836_B, "Bob")
    weeks = {"C": ("2026-05-01", WK_C_START, WK_C_END),
             "B": ("2026-05-08", WK_B_START, WK_B_END),
             "A": ("2026-05-15", WK_A_START, WK_A_END)}
    snaps = {}
    for account, wks in ((ACCT_836_A, "CBA"), (ACCT_836_B, "CBA")):
        for w in wks:
            date, start, end = weeks[w]
            day = int(date[-2:]) + 1
            for t in (1, 2):
                _seed_836_milestone(
                    conn, account_key=account, threshold=t,
                    captured_at_utc=f"2026-05-{day:02d}T0{t}:00:00Z",
                    week_start_date=date, week_start_at=start, week_end_at=end)
            snaps[(account, w, "ok")] = _seed_836_usage(
                conn, account_key=account, weekly_percent=2.0,
                captured_at_utc=f"2026-05-{day:02d}T03:00:00Z",
                week_start_date=date, week_start_at=start, week_end_at=end)
            snaps[(account, w, "retired")] = _seed_836_usage(
                conn, account_key=account, weekly_percent=3.0,
                captured_at_utc=f"2026-05-{day:02d}T04:00:00Z",
                week_start_date=date, week_start_at=start, week_end_at=end)
            _836_retire(conn, snaps[(account, w, "retired")])
            snaps[(account, w, "held_write")] = _seed_836_usage(
                conn, account_key=account, weekly_percent=3.0, held=1,
                payload_json='{"weeklyObservationHeld": true}',
                captured_at_utc=f"2026-05-{day:02d}T05:00:00Z",
                week_start_date=date, week_start_at=start, week_end_at=end)
    # Newest unheld sample is Alice's, so the merged current week is WK_A.
    _seed_836_usage(conn, account_key=ACCT_836_B, weekly_percent=4.0,
                    captured_at_utc="2026-05-20T09:00:00Z")
    _seed_836_usage(conn, account_key=ACCT_836_A, weekly_percent=5.0,
                    captured_at_utc="2026-05-20T10:00:00Z")
    bob_reset = _seed_836_reset(conn, account_key=ACCT_836_B,
                                effective=_836_EFFECTIVE)
    for t in (1, 2):
        _seed_836_milestone(conn, account_key=ACCT_836_B, threshold=t,
                            captured_at_utc=f"2026-05-19T0{t}:00:00Z",
                            reset_event_id=bob_reset)

    def join(account, week, kind):
        if kind == "absent":
            return 987654
        if kind == "sentinel":
            return 0
        return snaps[(account, week, kind)]

    blocks = [
        # (account, start, resets, [(week-of-snapshot, join kind), ...])
        (ACCT_836_A, "2026-05-02T10:00:00+00:00", "2026-05-02T15:00:00+00:00",
         [("C", "absent"), ("C", "ok")]),
        (ACCT_836_A, "2026-05-08T01:00:00+02:00", "2026-05-08T04:00:00+00:00",
         [("C", "sentinel"), ("C", "retired"), ("C", "held_write")]),
        (ACCT_836_A, "2026-05-14T22:00:00+00:00", "2026-05-15T03:00:00+00:00",
         [("B", "retired"), ("B", "held_write"), ("B", "absent")]),
        (ACCT_836_A, "2026-05-19T10:00:00+00:00", "2026-05-19T15:00:00+00:00",
         [("A", "ok")]),
        (ACCT_836_B, "2026-05-10T10:00:00+00:00", "2026-05-10T15:00:00+00:00",
         [("B", "sentinel"), ("B", "ok")]),
        (ACCT_836_B, "2026-05-17T22:00:00+00:00", "2026-05-18T03:00:00+00:00",
         [("A", "absent"), ("A", "retired")]),
        (ACCT_836_B, "2026-05-20T01:00:00+00:00", "2026-05-20T06:00:00+00:00",
         [("A", "ok"), ("A", "held_write")]),
        (UNATTRIBUTED_836, "2026-05-16T00:00:00+00:00",
         "2026-05-16T05:00:00+00:00", [("A", "sentinel")]),
        (ACCT_836_A, "not-a-date", "not-a-date", [("A", "absent")]),
    ]
    for account, start, resets, crossings in blocks:
        wk = (_836_window(ns, resets) if resets != "not-a-date" else 4242)
        block = _seed_account_block(
            conn, window_key=wk, block_start_at=start,
            five_hour_resets_at=resets, account_key=account,
            final_five_hour_percent=5.0)
        for threshold, (week, kind) in enumerate(crossings, start=1):
            _seed_account_5h_milestone(
                conn, block_id=block, window_key=wk,
                percent_threshold=threshold,
                captured_at_utc=start if resets == "not-a-date" else resets,
                account_key=account, usage_snapshot_id=join(account, week, kind))
    conn.commit()


def test_836_unavailable_join_stamps_match_the_per_cycle_oracle(
        ns, monkeypatch):
    """Spec 5.4 / plan Constraint 9, the per-build computation of the
    unavailable-join set. Detects: a crossing bucketed into the wrong cycle at
    a week boundary, an offset-spelled bound or a reset cut, another account's
    crossing in a scoped stamp, or an unparseable block's crossing admitted —
    each leaves a cached detail rendering a retired weekly value as a
    percentage (or refetching forever). Every index entry's and fetched
    detail's stamp, merged and scoped per account, must equal the stamp built
    from the original per-cycle query's ids."""
    import _cctally_milestone_history as mh
    real = mh._mh.compute_detail_stamp
    parts_of = {}

    def recording(*parts):
        out = real(*parts)
        parts_of[out] = parts
        return out

    monkeypatch.setattr(mh._mh, "compute_detail_stamp", recording)

    def expected_stamp(conn, entry, account):
        """The entry's own stamp parts with its unavailable-join component
        replaced by the oracle's."""
        parts = parts_of[entry["detail_stamp"]]
        scope = () if account is None else ("a" + account,)
        body = parts[:len(parts) - len(scope)]
        assert tuple(parts[len(body):]) == scope
        if body and isinstance(body[-1], str) and body[-1].startswith("u"):
            body = body[:-1]
        ids = _836_oracle_unavailable_ids(
            conn, entry["start_at_utc"], entry["end_at_utc"], account)
        oracle = ("u" + real(*ids),) if ids else ()
        return real(*body, *oracle, *scope), ids

    conn = ns["open_db"]()
    try:
        _836_multi_cycle_join_store(ns, conn)
        seen = {}
        for account in (None, ACCT_836_A, ACCT_836_B):
            index = (mh.build_claude_week_index(conn) if account is None
                     else _836_index(ns, conn, account))
            assert len(index) >= 2
            for entry in index:
                want, ids = expected_stamp(conn, entry, account)
                assert entry["detail_stamp"] == want, (account, entry)
                seen[(account, entry["start_at_utc"])] = ids
                detail = (
                    mh.build_claude_week_detail(conn, entry["key"])
                    if account is None else mh.build_claude_week_detail(
                        conn, entry["key"], account_key=account,
                        now_utc=_836_NOW))
                assert detail["detail_stamp"] == want, (account, entry)
    finally:
        conn.close()
    # non-vacuity: empty and nonempty sets, a straddling crossing in two
    # cycles, and scoped sets that differ from the merged one.
    assert any(seen.values()) and not all(seen.values())
    merged = [ids for (acct, _), ids in seen.items() if acct is None]
    flat = [i for ids in merged for i in ids]
    assert len(flat) != len(set(flat))
    assert seen[(ACCT_836_A, "2026-05-15T00:00:00Z")] != seen[
        (None, "2026-05-15T00:00:00Z")]


@pytest.mark.parametrize("case", ["plain", "reset_after_as_of", "date_only"])
def test_836_the_scoped_current_entry_is_the_cards_cycle(ns, case):
    """R1-6 / R1-7 / R2-2. Exactly one current entry, in FULL interval
    agreement with the card's cycle scope, and the route's detail for that
    key has the same bounds. `reset_after_as_of`: A's own retained reset
    takes effect after the as-of, so neither the card nor the index cuts yet
    — and both cut once the instant has passed. `date_only`: the storage-key
    leg with absent bounds. Detects: a newest-by-position current flag, a
    start-only match, and an index cut ahead of the card."""
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        _seed_836_account(conn, ACCT_836_A, "Alice")
        _seed_836_account(conn, ACCT_836_B, "Bob")
        bounds = ({"week_start_at": None, "week_end_at": None}
                  if case == "date_only" else {})
        for account, pct, cap in ((ACCT_836_B, 9.0, "2026-05-20T09:00:00Z"),
                                  (ACCT_836_A, 3.0, "2026-05-20T10:00:00Z")):
            _seed_836_usage(conn, account_key=account, captured_at_utc=cap,
                            weekly_percent=pct, **bounds)
            _seed_836_milestone(conn, account_key=account, threshold=1,
                                captured_at_utc=cap, **bounds)
        if case == "reset_after_as_of":
            _seed_836_reset(conn, account_key=ACCT_836_A,
                            effective="2026-05-20T13:00:00+00:00")
        conn.commit()
        if case == "date_only":
            # The date-only leg resolves the host-local day; pin the instant
            # inside the storage week whatever the host offset.
            now = dt.datetime(2026, 5, 18, 12, 0, tzinfo=dt.timezone.utc)
            conn.execute("UPDATE weekly_usage_snapshots SET "
                         "captured_at_utc='2026-05-18T10:00:00Z'")
            conn.commit()
        else:
            now = _836_NOW
        scope = _836_scope(ns, conn, ACCT_836_A, now)
        assert scope is not None and scope.account_key == ACCT_836_A
        index = _836_index(ns, conn, ACCT_836_A, now)
        current = [e for e in index if e["is_current"]]
        assert len(current) == 1, index
        entry = current[0]
        detail = mh.build_claude_week_detail(
            conn, entry["key"], account_key=ACCT_836_A, now_utc=now)
        if case == "date_only":
            assert scope.week_start_at is None
            assert (entry["start_at_utc"], entry["end_at_utc"]) == (None, None)
        else:
            assert entry["start_at_utc"] == scope.effective_start_at
            assert ns["parse_iso_datetime"](entry["end_at_utc"], "end") == (
                ns["parse_iso_datetime"](scope.week_end_at, "end"))
        assert detail is not None and detail["is_current"] is True
        assert (detail["start_at_utc"], detail["end_at_utc"]) == (
            entry["start_at_utc"], entry["end_at_utc"])
        assert entry["milestone_count"] == 1
        if case == "reset_after_as_of":
            assert len(index) == 1, "the index cut ahead of the card"
            later = dt.datetime(2026, 5, 20, 14, 0, tzinfo=dt.timezone.utc)
            later_scope = _836_scope(ns, conn, ACCT_836_A, later)
            later_index = _836_index(ns, conn, ACCT_836_A, later)
            [later_current] = [e for e in later_index if e["is_current"]]
            assert later_scope.effective_start_at == "2026-05-20T13:00:00Z"
            assert later_current["start_at_utc"] == "2026-05-20T13:00:00Z"
            assert later_current["milestone_count"] == 0
    finally:
        conn.close()


def _seed_836_route_store(conn):
    # `ns` is not reachable from the route seed; the window key is the stored
    # canonical value for the 2026-05-16T05:00Z reset.
    _seed_836_account(conn, ACCT_836_CODEX, "Codex", provider="codex")
    _seed_836_account(conn, ACCT_836_A, "Alice")
    _seed_836_account(conn, ACCT_836_B, "Bob")
    for account, pct, cap in ((ACCT_836_B, 9.0, "2026-05-18T09:00:00Z"),
                              (ACCT_836_A, 3.0, "2026-05-18T10:00:00Z")):
        _seed_836_usage(conn, account_key=account, captured_at_utc=cap,
                        weekly_percent=pct)
        _seed_836_milestone(conn, account_key=account, threshold=1,
                            captured_at_utc=cap)
        _seed_account_block(
            conn, window_key=1778907600,
            block_start_at="2026-05-16T00:00:00+00:00",
            five_hour_resets_at="2026-05-16T05:00:00+00:00",
            account_key=account, final_five_hour_percent=pct)
    _seed_836_usage(conn, account_key=ACCT_836_A,
                    captured_at_utc="2026-05-09T09:00:00Z", weekly_percent=7.0,
                    week_start_date="2026-05-08", week_start_at=WK_B_START,
                    week_end_at=WK_B_END)


def _836_route(tmp_path, monkeypatch, seed=_seed_836_route_store):
    ns = load_script()
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-05-18T12:00:00Z")
    srv = _boot_milestones_server(ns, tmp_path, monkeypatch, seed=seed)
    conn = ns["open_db"]()
    try:
        merged = ns["build_claude_week_index"](conn)
        scoped = {
            account: ns["build_claude_week_index"](
                conn, account_key=account,
                cycle_scope=ns["_claude_current_cycle_scope"](
                    conn, dt.datetime(2026, 5, 18, 12, tzinfo=dt.timezone.utc),
                    account_key=account))
            for account in (ACCT_836_A, ACCT_836_B)
        }
    finally:
        conn.close()
    return ns, srv, merged, scoped


def _836_get(srv, key, query=""):
    return _get(srv, "/api/milestones/claude/week/"
                + _urlparse.quote(key, safe="") + query)


def test_836_the_route_serves_a_scoped_claude_detail(tmp_path, monkeypatch):
    """Spec 5.6: 200 only for a fully scoped result, and an absent qualifier
    is the shipped merged response. Detects: the qualifier ignored (Bob's
    block served under Alice) or the unqualified route changed."""
    ns, srv, merged, scoped = _836_route(tmp_path, monkeypatch)
    try:
        key = scoped[ACCT_836_A][0]["key"]
        status, body = _836_get(srv, key, "?account=" + ACCT_836_A)
        assert status == 200, (status, body)
        assert body["key"] == key and body["source"] == "claude"
        assert body["detail_stamp"] == scoped[ACCT_836_A][0]["detail_stamp"]
        assert [b["account_key"] for b in body["blocks"]] == [ACCT_836_A]
        assert body["is_current"] is True
        status, merged_body = _836_get(srv, merged[0]["key"])
        assert status == 200
        conn = ns["open_db"]()
        try:
            expected = ns["build_claude_week_detail"](conn, merged[0]["key"])
        finally:
            conn.close()
        assert merged_body == _json.loads(_json.dumps(
            {**expected, "source": "claude", "key": merged[0]["key"]}))
        assert sorted(b["account_key"] for b in merged_body["blocks"]) == [
            ACCT_836_A, ACCT_836_B]
    finally:
        stop(srv, srv._test_thread)


@pytest.mark.parametrize("query", [
    "?account=not-a-key",
    "?account=",
    "?account=*",
    "?account=" + ACCT_836_A + "&account=" + ACCT_836_B,
    "?account=" + ACCT_836_A.upper(),
])
def test_836_the_route_rejects_a_malformed_claude_qualifier(
        tmp_path, monkeypatch, query):
    ns, srv, _merged, scoped = _836_route(tmp_path, monkeypatch)
    try:
        status, body = _836_get(srv, scoped[ACCT_836_A][0]["key"], query)
        assert status == 400, (status, body)
        assert body == {"error": "invalid account"}
    finally:
        stop(srv, srv._test_thread)


@pytest.mark.parametrize("account,which", [
    ("4444444444444444444444444444dddd", "unknown account"),
    (ACCT_836_CODEX, "non-Claude account"),
    (ACCT_836_B, "key outside the account's scoped index"),
])
def test_836_the_route_answers_404_for_a_key_outside_the_scope(
        tmp_path, monkeypatch, account, which):
    """Detects: a scoped lookup that fails and is then answered from the
    merged index, which would hand Alice's (or a stranger's) cycle to a
    request that named another account."""
    ns, srv, _merged, scoped = _836_route(tmp_path, monkeypatch)
    try:
        key = scoped[ACCT_836_A][1]["key"]  # Alice's older week: Bob has none
        status, body = _836_get(srv, key, "?account=" + account)
        assert status == 404, (which, status, body)
        assert body == {"error": "unknown week", "code": "unknown_key",
                        "reason": "unknown"}
    finally:
        stop(srv, srv._test_thread)


def test_836_the_route_rejects_a_claude_qualifier_on_an_undecorated_store(
        tmp_path, monkeypatch):
    """Spec 5.6 / privacy: one real account (plus `unattributed`) partitions
    nothing, so a qualified request is refused rather than answered with the
    merged week."""
    def seed(conn):
        _seed_836_account(conn, ACCT_836_A, "Alice")
        _seed_836_usage(conn, account_key=ACCT_836_A,
                        captured_at_utc="2026-05-18T10:00:00Z",
                        weekly_percent=3.0)
        _seed_836_usage(conn, account_key=UNATTRIBUTED_836,
                        captured_at_utc="2026-05-18T09:00:00Z",
                        weekly_percent=2.0)
    ns, srv, merged, _scoped = _836_route(tmp_path, monkeypatch, seed=seed)
    try:
        key = merged[0]["key"]
        for account in (ACCT_836_A, UNATTRIBUTED_836):
            status, body = _836_get(srv, key, "?account=" + account)
            assert status == 400, (account, status, body)
            assert body == {"error": "invalid account"}
        status, _body = _836_get(srv, key)
        assert status == 200
    finally:
        stop(srv, srv._test_thread)


# The serialized undecorated history (index + every detail, labels removed:
# they follow the host's display zone) on the pre-#836 code, `main` 1d959f1fe.
_836_UNDECORATED_BASELINE = {
    0: "8ca6ac196c0a5143b1bcaf73fd262778766de57cf6141c3dd648b12ec9b18746",
    1: "4c7da9e47c46b206501b6e2f660427c4426f602d1bb1956dced894b455703ea0",
}


@pytest.mark.parametrize("real_accounts", [0, 1])
def test_836_undecorated_history_matches_the_pre_change_baseline(
        ns, real_accounts):
    """A10 / spec 7.7. Zero real accounts, and one real account beside
    same-week `unattributed` milestones and 5h credits: the merged index and
    every detail are byte-identical to the pre-change payloads. Detects: any
    account scoping, stamp component or as-of cut leaking into the
    undecorated (R8) history."""
    import hashlib
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        _seed_full(conn)
        if real_accounts:
            _seed_836_account(conn, ACCT_836_A, "Alice")
            conn.execute("UPDATE weekly_usage_snapshots SET account_key=? "
                         "WHERE week_start_date='2026-05-15'", (ACCT_836_A,))
            conn.execute(
                "INSERT INTO five_hour_reset_events "
                "(detected_at_utc, five_hour_window_key, prior_percent, "
                " post_percent, effective_reset_at_utc, account_key) "
                "VALUES ('2026-05-16T03:00:00+00:00', 5155, 2, 1, "
                " '2026-05-16T03:00:00+00:00', 'unattributed')")
            conn.commit()
        import _cctally_account
        assert not _cctally_account.provider_is_decorated(conn, "claude")
        index = mh.build_claude_week_index(conn)
        details = [mh.build_claude_week_detail(conn, e["key"]) for e in index]
    finally:
        conn.close()

    def unlabeled(value):
        if isinstance(value, dict):
            return {k: unlabeled(v) for k, v in value.items() if k != "label"}
        if isinstance(value, list):
            return [unlabeled(v) for v in value]
        return value

    payload = _json.dumps({"index": unlabeled(index),
                           "details": unlabeled(details)}, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert digest == _836_UNDECORATED_BASELINE[real_accounts], (digest, payload)
