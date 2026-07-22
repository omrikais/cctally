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


def test_codex_cycle_detail_unions_member_milestones(ns, monkeypatch):
    import _cctally_milestone_history as mh

    start = "2026-03-01T00:00:00+00:00"
    base = _dt("2026-03-08T00:00:00+00:00")
    r0, r1 = base, base + dt.timedelta(seconds=3)

    def fake_breakdown(ident, reset, *, speed, cache_conn, stats_conn):
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
    srv.daemon_threads = True
    t = _threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
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
        srv.shutdown()
        srv.server_close()


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
        srv.shutdown()
        srv.server_close()


def test_api_milestones_bad_source_400(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot_milestones_server(ns, tmp_path, monkeypatch, seed=_seed_full)
    try:
        status, _body = _get(srv, "/api/milestones/nope/week/2026-05-15")
        assert status == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_milestones_malformed_claude_key_400(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot_milestones_server(ns, tmp_path, monkeypatch, seed=_seed_full)
    try:
        status, _body = _get(srv, "/api/milestones/claude/week/not-a-date")
        assert status == 400
    finally:
        srv.shutdown()
        srv.server_close()


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
        srv.shutdown()
        srv.server_close()


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
        srv.shutdown()
        srv.server_close()


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
        srv.shutdown()
        srv.server_close()
