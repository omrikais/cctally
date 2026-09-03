"""In-place credit detection (v1.7.2) — record-usage tests.

Covers:

* **Reset-aware DB clamp** (Task 2): the monotonic 7d clamp now joins
  against ``week_reset_events`` so the ``MAX(weekly_percent)`` query
  filters to samples captured at-or-after the segment's
  ``effective_reset_at_utc``. Legacy behavior preserved when no event
  row exists.

* **In-place credit detection branch** (Task 3): when ``resets_at``
  stays unchanged but ``weekly_percent`` drops by ≥25pp, emit a
  ``week_reset_events`` row, force-write ``hwm-7d``, and let the seed
  snapshot land via the now-reset-aware clamp.

* **Backfill extension** (Task 4): historical in-place credits get a
  parallel-branch detection in ``_backfill_week_reset_events``.

* **Milestone segment stamping** (Task 5).

* **percent-breakdown filter** (Task 6).

* **Alerts dedup** (Task 8).

Conventions:
* Each test uses ``tmp_path`` via ``redirect_paths`` so ``hwm-7d`` and
  the SQLite DBs land in an isolated scratch HOME.
* ``argparse.Namespace`` is constructed directly to drive
  ``cmd_record_usage`` without a shell.
* Most tests read wall time. The three that assert an ordering between a
  seeded capture instant and one the product stamps from its own clock pin
  both clocks instead, through ``_pin_observation_clock``; that helper states
  why.
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


# ── helpers ────────────────────────────────────────────────────────────


def _stamp_journal_id(conn, table: str, rowid: int) -> None:
    """Stamp the cutover-scheme ``journal_id = 'b:<table>:<rowid>'`` on a
    directly-seeded row so it mirrors post-cutover reality (spec §8: no NULL
    ``journal_id`` survives cutover). Without it, the ingest cycle's harvest
    reverse-refs a milestone FK to a NULL-``journal_id`` snapshot/reset and
    raises ``JournalError`` (a harvest-order violation the loud production
    failure is correct about — the fixture, not the code, is wrong)."""
    conn.execute(
        f"UPDATE {table} SET journal_id = ? WHERE id = ?",
        (f"b:{table}:{rowid}", rowid),
    )


def _record_usage_args(
    *,
    percent: float,
    resets_at: int,
    five_hour_percent: float | None = None,
    five_hour_resets_at: int | None = None,
    week_start_name: str | None = None,
) -> argparse.Namespace:
    """Build a minimal Namespace matching cmd_record_usage's signature."""
    return argparse.Namespace(
        percent=percent,
        resets_at=resets_at,
        five_hour_percent=five_hour_percent,
        five_hour_resets_at=five_hour_resets_at,
        week_start_name=week_start_name,
    )


def _seed_usage_snapshot(
    conn,
    *,
    captured_at_utc: str,
    week_start_date: str,
    week_end_at: str,
    weekly_percent: float,
    week_start_at: str | None = None,
    week_end_date: str | None = None,
) -> int:
    """Insert a weekly_usage_snapshots row and return its id."""
    if week_start_at is None:
        week_start_at = week_start_date + "T00:00:00+00:00"
    if week_end_date is None:
        week_end_date = week_end_at[:10]
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, "
        " week_start_at, week_end_at, weekly_percent, source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (captured_at_utc, week_start_date, week_end_date,
         week_start_at, week_end_at, weekly_percent, "test", "{}"),
    )
    rowid = int(cur.lastrowid)
    _stamp_journal_id(conn, "weekly_usage_snapshots", rowid)
    return rowid


def _seed_reset_event(
    conn,
    *,
    new_week_end_at: str,
    effective: str,
    old_week_end_at: str | None = None,
    detected_at_utc: str = "2026-05-15T19:35:00Z",
    credit_key: str | None = None,
    credit_order: int | None = None,
) -> int:
    """Insert a week_reset_events row and return its id.

    ``credit_key`` is the row's identity since #703 + #707. Left None the row is
    the LEGACY keyless shape — what an old `wr:` journal line folds to — which
    no longer suppresses a differently-identified credit."""
    if old_week_end_at is None:
        old_week_end_at = effective
    cur = conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, credit_key, credit_order) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (detected_at_utc, old_week_end_at, new_week_end_at, effective,
         credit_key, credit_order),
    )
    rowid = int(cur.lastrowid)
    _stamp_journal_id(conn, "week_reset_events", rowid)
    conn.commit()
    return rowid


def _epoch(iso: str) -> int:
    """Parse an ISO-8601 UTC timestamp into a unix epoch int."""
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


# ── Task 2: reset-aware DB clamp ──────────────────────────────────────


def _past_week_end_iso() -> tuple[str, int]:
    """Build an ISO + epoch tuple in the PAST. Used for Task 2 clamp
    tests where we want to exercise the clamp alone WITHOUT tripping the
    in-place credit detection branch (which requires
    ``prior_end_dt > now_utc``).
    """
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).replace(
        minute=0, second=0, microsecond=0
    )
    return past.isoformat(timespec="seconds"), int(past.timestamp())


def test_reset_aware_clamp_without_event_preserves_legacy_behavior(ns):
    """No week_reset_events row → clamp behaves like before (MAX over
    the whole week, post-credit reading is rejected as a regression).
    Uses a past end_at so the in-place credit detection branch is
    skipped (predicate ``prior_end_dt > now_utc`` is false).
    """
    end_at_iso, end_at_epoch = _past_week_end_iso()
    week_start_date, _ = _week_start_for(end_at_iso)
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_at_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Drive cmd_record_usage with percent=2.0 (post-credit shape) and the
    # same end_at — without an event row AND the window is in the past
    # so detection is skipped, the legacy clamp must reject.
    args = _record_usage_args(percent=2.0, resets_at=end_at_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 2.0"
        ).fetchone()[0]
        assert cnt == 0, "clamp should have rejected the 2% reading"
    finally:
        conn.close()


def test_reset_aware_clamp_with_event_filters_to_post_credit(ns):
    """With a week_reset_events row, the MAX query filters to samples
    captured at-or-after the reset-aware floor. Pre-credit 67% no longer
    dominates; a fresh post-credit 4% lands.
    Uses a past end_at so this test exercises the CLAMP alone, not the
    in-place credit detection branch (which would also fire on a future
    end_at and double-write the event row).

    The seeded event's effective and the seeded captured timestamps are
    anchored RELATIVE to the dynamic past window so they fall inside the
    `[week_start_at, week_end_at)` the write-site clamp derives from
    `resets_at` (record-credit M2 unified the clamp onto the window-based
    `_reset_aware_floor` predicate, not a `new_week_end_at` string match —
    a stale fixed effective from a far-earlier month would now correctly fall
    outside the window and not floor this week; memory: record-usage test
    time-bomb).
    """
    end_at_iso, end_at_epoch = _past_week_end_iso()
    week_start_date, _ = _week_start_for(end_at_iso)
    end_dt = dt.datetime.fromisoformat(end_at_iso)
    week_start_at = (end_dt - dt.timedelta(days=7)).isoformat(timespec="seconds")
    # Floor effective ~2 days before the window end (in-window); pre-credit
    # sample before it, post-credit sample after it.
    effective_dt = end_dt - dt.timedelta(days=2)
    effective_iso = effective_dt.isoformat(timespec="seconds")
    pre_iso = (effective_dt - dt.timedelta(hours=6)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    post_iso = (effective_dt + dt.timedelta(hours=1)).isoformat(
        timespec="seconds").replace("+00:00", "Z")

    conn = ns["open_db"]()
    try:
        # Pre-credit 67% sample.
        _seed_usage_snapshot(
            conn,
            captured_at_utc=pre_iso,
            week_start_date=week_start_date,
            week_start_at=week_start_at,
            week_end_at=end_at_iso,
            weekly_percent=67.0,
        )
        # Event row marking the segment boundary (effective in-window).
        _seed_reset_event(
            conn,
            new_week_end_at=end_at_iso,
            effective=effective_iso,
        )
        # Post-credit 2% sample (already past the boundary; the new
        # clamp's MAX over the post-segment window starts at 2%).
        _seed_usage_snapshot(
            conn,
            captured_at_utc=post_iso,
            week_start_date=week_start_date,
            week_start_at=week_start_at,
            week_end_at=end_at_iso,
            weekly_percent=2.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Now drive cmd_record_usage with percent=4.0 — must pass the
    # reset-aware clamp (4 > 2 over the post-credit segment).
    args = _record_usage_args(percent=4.0, resets_at=end_at_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 4.0"
        ).fetchone()[0]
        assert cnt == 1, "clamp should have passed the 4% reading post-credit"
    finally:
        conn.close()


# ── Task 3: in-place credit detection branch ─────────────────────────


def _future_week_end_iso(now: dt.datetime | None = None) -> tuple[str, int]:
    """Build an ISO + epoch tuple a few days after ``now``. The
    detection branch requires ``prior_end_dt > now_utc`` and we'd
    rather not freeze ``dt.datetime.now`` — the test owns its own
    "future" by stamping at "now + 3 days, rounded to next hour".

    A test that pins the clock passes its pinned instant, so the week end
    stays in the future of the clock the product will actually read.
    """
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    future = (now + dt.timedelta(days=3)).replace(
        minute=0, second=0, microsecond=0
    )
    return future.isoformat(timespec="seconds"), int(future.timestamp())


def _week_start_for(end_iso: str) -> tuple[str, str]:
    """Given a week_end_at ISO, return (week_start_date, week_end_date)."""
    end = dt.datetime.fromisoformat(end_iso)
    start = end - dt.timedelta(days=7)
    return start.date().isoformat(), end.date().isoformat()


# The instant the pinning tests below pretend `record-usage` ran at. It is
# analytic rather than wall-clock-derived because a credit's `observed_at_utc`
# is stamped from the CAPTURE clock, which reads wall time in production: a test
# that seeds rows at a fixed offset from the wall clock's hour floor and then
# asserts an ordering against that stamp holds only while the wall clock is far
# enough past the hour, so it fails whenever it runs in the first few minutes of
# an hour. The value is the 2026-09-01 incident the tests below describe.
_PINNED_OBSERVATION = dt.datetime(2026, 9, 1, 18, 30, 0, tzinfo=dt.timezone.utc)


def _pin_observation_clock(monkeypatch) -> dt.datetime:
    """Pin both clocks to ``_PINNED_OBSERVATION`` and return that instant.

    Both variables are needed. ``CCTALLY_AS_OF`` moves the DETECTION clock, and
    ``CCTALLY_TEST_PIN_CAPTURE`` makes the CAPTURE clock follow it — the same
    pairing ``bin/build-alerts-fixtures.py`` uses for the mid-week-reset
    scenario, and for the same reason: every other instant in the scenario is
    analytic, so leaving the capture on the runner's wall clock leaves the
    scenario asserting a different thing depending on when it runs.
    """
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        _PINNED_OBSERVATION.isoformat().replace("+00:00", "Z"),
    )
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    return _PINNED_OBSERVATION


def test_detection_fires_on_threshold(ns, tmp_path):
    """prior=67, cur=2 (drop 65pp ≥ 25pp threshold) with the SAME
    week_end_at as the new fetch: writes event row, seed snapshot
    lands via the reset-aware clamp, and hwm-7d gets force-written.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

    # Seed prior 67% snapshot with the SAME end_at as the new fetch.
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Pre-seed hwm-7d so we can verify the force-write decreased it.
    hwm_path = ns["APP_DIR"] / "hwm-7d"
    hwm_path.write_text(f"{week_start_date} 67.0\n")

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    # 1 event row written with new == cur_end_canon.
    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        # In-place credit row shape (post-Bug-1 fix): old == effective,
        # new == cur_end_canon (DISTINCT values). The previous
        # old==new==cur_end shape collapsed the credited week to a
        # zero-width window in _apply_reset_events_to_weekrefs because
        # both pre_map[old] and post_map[new] fired on the same WeekRef.
        # See bin/_cctally_record.py:cmd_record_usage for the rationale.
        assert events[0]["new_week_end_at"] == end_iso
        assert events[0]["old_week_end_at"] == events[0]["effective_reset_at_utc"]
        assert events[0]["old_week_end_at"] != events[0]["new_week_end_at"]

        # 1 new snapshot at 2%.
        cnt = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 2.0"
        ).fetchone()[0]
        assert cnt == 1, "post-credit 2% reading should have landed"
    finally:
        conn.close()

    # hwm-7d force-written to the new (lower) value.
    parts = hwm_path.read_text().strip().split()
    assert parts == [week_start_date, "2.0"], parts


def test_detection_does_not_fire_below_threshold(ns, tmp_path):
    """prior=26, cur=2 (drop 24pp < 25pp): no event row, no seed insert
    (legacy monotonic clamp blocks the lower percent), hwm unchanged.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=26.0,
        )
        conn.commit()
    finally:
        conn.close()

    hwm_path = ns["APP_DIR"] / "hwm-7d"
    hwm_path.write_text(f"{week_start_date} 26.0\n")

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events"
        ).fetchone()[0]
        assert events == 0, "drop below threshold must not fire detection"
        cnt = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 2.0"
        ).fetchone()[0]
        assert cnt == 0, "legacy clamp should still block the lower reading"
    finally:
        conn.close()

    parts = hwm_path.read_text().strip().split()
    assert parts == [week_start_date, "26.0"], parts


def test_reset_to_zero_lone_zero_arms_no_fire(ns, tmp_path):
    """#128: a LONE transient ~0 (14→0) ARMS the marker but does NOT fire.
    No event row, hwm unchanged (clamp holds the suppressed 0), marker present
    with the end boundary + baseline. This is the non-vacuity anchor: under the
    pre-debounce code this single zero fired immediately."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date, week_end_at=end_iso,
            weekly_percent=14.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 14.0\n")

    rc = ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch))
    assert rc == 0

    conn = ns["open_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 0
    finally:
        conn.close()
    assert (ns["APP_DIR"] / "hwm-7d").read_text().strip().split() == [week_start_date, "14.0"]

    import _cctally_record as rec
    # #703 + #707 §6.2a: the arm belongs to ONE account, and each account has
    # its own marker file, so the read names the account the tick ran under.
    marker = rec._read_reset_zero_marker("unattributed")
    assert marker is not None
    assert marker[0] == week_start_date          # week
    assert marker[2] == 14.0                      # baseline
    assert marker[6] == "unattributed"            # the account it belongs to


def test_detection_fires_on_reset_to_zero_below_threshold(ns, tmp_path):
    """#128 (rewritten): two consecutive ~0 readings (14→0→0) CONFIRM and fire.
    Drop 14pp < 25pp, so this exercises the debounced reset-to-zero path, not
    the 25pp path. After the second zero: one event row (old==effective,
    new==end, distinct), the post-reset 0 lands, hwm=0, marker cleared."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date, week_end_at=end_iso,
            weekly_percent=14.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 14.0\n")

    # Tick 1: arm.
    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    conn = ns["open_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 0
    finally:
        conn.close()

    # Tick 2: confirm + fire.
    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        assert events[0]["new_week_end_at"] == end_iso
        assert events[0]["old_week_end_at"] == events[0]["effective_reset_at_utc"]
        assert events[0]["old_week_end_at"] != events[0]["new_week_end_at"]
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 0.0"
        ).fetchone()[0] == 1
    finally:
        conn.close()
    assert (ns["APP_DIR"] / "hwm-7d").read_text().strip().split() == [week_start_date, "0.0"]

    import _cctally_record as rec
    assert rec._read_reset_zero_marker() is None    # cleared after fire


def test_reset_to_zero_stayed_low_confirms(ns, tmp_path):
    """#128 §2.1 regression guard: 14→0→2. The second reading 2 (<= 14/2) is a
    real reset that started climbing — it CONFIRMS (fires) so the display
    corrects to 2 instead of being stuck at 14."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date, week_end_at=end_iso, weekly_percent=14.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 14.0\n")

    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0  # arm
    assert ns["cmd_record_usage"](_record_usage_args(percent=2.0, resets_at=end_epoch)) == 0  # confirm
    conn = ns["open_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 1
    finally:
        conn.close()
    assert (ns["APP_DIR"] / "hwm-7d").read_text().strip().split() == [week_start_date, "2.0"]


def test_reset_to_zero_recovery_clears(ns, tmp_path):
    """#128: 14→0→14 (transient zero recovered to baseline). The marker arms on
    the 0 then CLEARS on the 14 (> 14/2): no event ever, hwm stays 14, marker
    gone. This is the transient-API-zero case the debounce exists to suppress."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date, week_end_at=end_iso, weekly_percent=14.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 14.0\n")

    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0   # arm
    assert ns["cmd_record_usage"](_record_usage_args(percent=14.0, resets_at=end_epoch)) == 0  # recover
    conn = ns["open_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 0
    finally:
        conn.close()
    assert (ns["APP_DIR"] / "hwm-7d").read_text().strip().split() == [week_start_date, "14.0"]
    import _cctally_record as rec
    assert rec._read_reset_zero_marker() is None    # cleared on recovery


def test_reset_to_zero_near_recovery_clears(ns, tmp_path):
    """#128 midpoint boundary: 14→0→13 (13 > 14/2=7) CLEARS — recovered to
    within the baseline band, no fire. Pairs with the 14→0→2 case to pin the
    midpoint threshold from both sides."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date, week_end_at=end_iso, weekly_percent=14.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 14.0\n")

    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0   # arm
    assert ns["cmd_record_usage"](_record_usage_args(percent=13.0, resets_at=end_epoch)) == 0  # near-recover
    conn = ns["open_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_reset_to_zero_anchor_is_first_zero(ns, tmp_path, monkeypatch):
    """#128: the effective_reset_at_utc is floored from the FIRST-zero instant
    (stored in the marker), not the confirmation tick. Pin the two ticks an hour
    apart via CCTALLY_AS_OF; assert the event anchors to the first hour."""
    # Build an end well after both pinned instants so prior_end_dt > now_utc.
    first_zero = "2026-06-02T18:00:35+00:00"
    confirm    = "2026-06-02T19:00:05+00:00"
    end_dt = dt.datetime.fromisoformat("2026-06-05T18:00:00+00:00")
    end_iso = end_dt.isoformat(timespec="seconds")
    end_epoch = int(end_dt.timestamp())
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-06-01T10:00:00Z",
            week_start_date=week_start_date, week_end_at=end_iso, weekly_percent=14.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 14.0\n")

    monkeypatch.setenv("CCTALLY_AS_OF", first_zero)
    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0  # arm
    monkeypatch.setenv("CCTALLY_AS_OF", confirm)
    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0  # confirm

    conn = ns["open_db"]()
    try:
        eff = conn.execute(
            "SELECT effective_reset_at_utc FROM week_reset_events"
        ).fetchone()[0]
    finally:
        conn.close()
    # Floored to the FIRST-zero hour (18:00), not the confirmation hour (19:00).
    assert eff.startswith("2026-06-02T18:00:00")


def test_reset_to_zero_big_drop_still_fires_immediately(ns, tmp_path):
    """#128: a ≥25pp drop to zero (30→0) takes the un-debounced big_drop path —
    fires on the FIRST tick, no marker dependency. Proves the 25pp path is
    untouched."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date, week_end_at=end_iso, weekly_percent=30.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 30.0\n")

    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    conn = ns["open_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 1
    finally:
        conn.close()
    assert (ns["APP_DIR"] / "hwm-7d").read_text().strip().split() == [week_start_date, "0.0"]


def test_reset_to_zero_crash_recovery_reruns_pivots(ns, tmp_path):
    """#128 P2a: simulate a tick that committed the event row then died before
    clearing the marker. The next confirming zero re-runs the idempotent pivots
    (hwm=0) and the INSERT OR IGNORE no-ops — exactly one event row, marker
    cleared."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date, week_end_at=end_iso, weekly_percent=14.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 14.0\n")

    # Pre-seed the mid-fire crash state: an armed marker for this end PLUS a
    # matching event row (the prior tick committed the event then died before
    # clearing the marker).
    #
    # #703 + #707: the event row is identified by `credit_key`, not by the week
    # boundary, and the debounced leg derives that key from the FIRST zero's
    # identity retained in the marker. So the crash state has to carry both, and
    # they have to agree — which is exactly what the crashed tick would have
    # left behind. Seeding a keyless row instead would no longer represent the
    # crash: the confirming tick would derive its own identity and correctly
    # record a second, different credit.
    import _cctally_record as rec
    from _lib_credit_identity import CreditSource, derive_credit_key, \
        derive_credit_order

    first_zero_iso = "2026-05-14T10:30:00+00:00"
    first_zero_identity = "sa:o:crashedfirstzero"
    crashed_source = CreditSource(
        kind="debounced",
        identity=first_zero_identity,
        order=derive_credit_order(CreditSource(
            kind="debounced", identity=first_zero_identity,
            order=_epoch(first_zero_iso))),
    )
    rec._arm_reset_zero_marker(
        week_start_date, end_iso, baseline_pct=14.0,
        first_zero_iso=first_zero_iso,
        first_zero_capture_iso=first_zero_iso,
        first_zero_identity=first_zero_identity,
    )
    conn = ns["open_db"]()
    try:
        _seed_reset_event(
            conn, new_week_end_at=end_iso,
            effective="2026-05-14T10:00:00+00:00",
            credit_key=derive_credit_key(crashed_source),
            credit_order=crashed_source.order,
        )
    finally:
        conn.close()

    # Confirming zero: pivots re-run, no duplicate event, marker cleared.
    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    conn = ns["open_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 1
    finally:
        conn.close()
    assert (ns["APP_DIR"] / "hwm-7d").read_text().strip().split() == [week_start_date, "0.0"]
    assert rec._read_reset_zero_marker() is None


def test_reset_to_zero_stale_marker_boundary_mismatch(ns, tmp_path):
    """#128 P2b: a marker armed for end E1 must NOT confirm against a tick whose
    canonical end is E2. With a mismatched end, `armed` is False, so the tick
    re-arms a fresh E2 marker instead of firing — no event row."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date, week_end_at=end_iso, weekly_percent=14.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 14.0\n")

    # Armed marker carries a DIFFERENT end boundary than the current tick's.
    import _cctally_record as rec
    rec._arm_reset_zero_marker(
        week_start_date, "2025-01-01T00:00:00+00:00", baseline_pct=14.0,
        first_zero_iso="2026-05-14T10:30:00+00:00",
    )
    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    conn = ns["open_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 0
    finally:
        conn.close()
    # Re-armed against the real end (E2 = this tick's canonical end), under the
    # tick's own account (#703 + #707 §6.2a).
    marker = rec._read_reset_zero_marker("unattributed")
    assert marker is not None and marker[1] == end_iso


def test_reset_to_zero_respects_min_drop_floor(ns, tmp_path):
    """prior=1, cur=0 (drop 1pp): below the reset-to-zero min-drop floor.
    A 1%→0% blip is stale-replica noise, NOT a reset — no event row, no
    seed (legacy clamp blocks the lower percent), hwm unchanged. Guards
    against spuriously segmenting a week on sub-floor jitter.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=1.0,
        )
        conn.commit()
    finally:
        conn.close()

    hwm_path = ns["APP_DIR"] / "hwm-7d"
    hwm_path.write_text(f"{week_start_date} 1.0\n")

    args = _record_usage_args(percent=0.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0]
        assert events == 0, "sub-floor 1pp drop must not fire reset-to-zero"
    finally:
        conn.close()

    parts = hwm_path.read_text().strip().split()
    assert parts == [week_start_date, "1.0"], parts


def test_detection_skipped_when_window_expired(ns, tmp_path):
    """prior=67, cur=2 BUT prior_end_dt <= now_utc: no event row.
    This is the natural-rollover case (the old week's end has actually
    passed), not a goodwill credit. Use a past end_at.
    """
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).replace(
        minute=0, second=0, microsecond=0
    )
    end_iso = past.isoformat(timespec="seconds")
    end_epoch = int(past.timestamp())
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events"
        ).fetchone()[0]
        assert events == 0, "expired-window case must not fire detection"
    finally:
        conn.close()


def test_dedup_via_pre_check(ns, tmp_path):
    """Pre-seed the week_reset_events row THIS credit's source record derives.
    Fire the same credit again. The pre-check fires before the INSERT, so no
    second event row is written.

    #703 + #707 moved the pre-check off ``new_week_end_at`` — which suppressed
    every later credit in the same week — onto the exact
    ``(account_key, credit_key)`` identity. The fixture therefore names the
    source record instead of the week boundary, which is what lets the pre-check
    recognise the row as the same credit.
    """
    import _cctally_journal as jr
    from _lib_credit_identity import CreditSource, derive_credit_key

    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)
    source = CreditSource(kind="immediate", identity="sa:o:dedup", order=11)
    effective_dt = dt.datetime.fromisoformat("2026-05-15T17:00:00+00:00")

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective="2026-05-15T17:00:00+00:00",
            old_week_end_at=end_iso,
            credit_key=derive_credit_key(source),
            credit_order=11,
        )
        conn.commit()

        ctx = jr.IngestContext(conn=conn, batch=[])
        ns["_fire_in_place_credit"](
            conn, week_start_date, end_iso, 2.0,
            observed_pre_credit_pct=67.0, effective_dt=effective_dt,
            as_of="2026-05-15T17:30:00+00:00", commit=True, ctx=ctx,
            credit_source=source,
            observed_at_utc="2026-05-15T17:30:00+00:00",
            confirming_capture_at_utc="2026-05-15T17:30:00+00:00",
        )

        events = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events"
        ).fetchone()[0]
        assert events == 1, "pre-check should have prevented a duplicate event row"
        assert ctx.suppression_map == {}, (
            "an already-present credit must NOT re-capture suppression")
    finally:
        conn.close()


def test_dedup_via_seed_snapshot(ns, tmp_path):
    """First call writes event + 2% seed. Second call (next OAuth fetch
    at 3%) sees prior=2 (post-credit) → branch not entered (drop is 1pp
    not >= 25pp) → no second event row.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    # First call: detection fires.
    rc = ns["cmd_record_usage"](_record_usage_args(percent=2.0, resets_at=end_epoch))
    assert rc == 0
    # Second call: prior is now 2%, drop 2→3 is +1, branch not entered.
    rc = ns["cmd_record_usage"](_record_usage_args(percent=3.0, resets_at=end_epoch))
    assert rc == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events"
        ).fetchone()[0]
        assert events == 1, "second call must not re-fire the detection branch"
    finally:
        conn.close()


# ── Task 4: backfill extension for historical in-place credits ───────


def test_backfill_detects_historical_in_place_credit(ns):
    """Seed snapshots showing 67→2 with the SAME week_end on consecutive
    captures (captured BEFORE the end_at — i.e., we were "in the window"
    when the credit landed). Run ``_backfill_week_reset_events``.
    Assert: one event row with old == new == cur_end (in-place credit
    shape), effective == floor_to_hour(captured_at_of_2pct_row).
    """
    # Use a future end_at so captured_dt < prior_end_dt is true.
    end_iso, _ = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=2.0,
        )
        # No event row pre-seeded; backfill should synthesize one.
        conn.commit()
        ns["_backfill_week_reset_events"](conn)

        events = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        # In-place credit row shape (post-Bug-1 fix): old == effective,
        # new == cur_end (DISTINCT). See live-detection test
        # ``test_detection_fires_on_threshold`` for the same shape and
        # bin/_cctally_record.py:cmd_record_usage for rationale.
        assert events[0]["new_week_end_at"] == end_iso
        assert events[0]["old_week_end_at"] == events[0]["effective_reset_at_utc"]
        assert events[0]["old_week_end_at"] != events[0]["new_week_end_at"]
        # Effective is floor-to-hour of the captured_at when the drop
        # was first observed (Anthropic's reset times are always
        # hour-aligned). Compare as UTC moment to absorb the host-tz
        # rendering quirk in ``parse_iso_datetime`` — see project
        # gotcha ``unixepoch_for_cross_offset_compare``.
        eff_dt = dt.datetime.fromisoformat(events[0]["effective_reset_at_utc"])
        assert eff_dt.astimezone(dt.timezone.utc) == dt.datetime(
            2026, 5, 14, 17, 0, 0, tzinfo=dt.timezone.utc
        )
    finally:
        conn.close()


def test_backfill_skips_historical_reset_to_zero(ns):
    """Backfill is STRICT (>=25pp only): a sub-25pp reset-to-zero
    (14→0 on the SAME week_end) must NOT synthesize an event.

    The lenient reset-to-zero discriminator (``cur <= 1pp and drop >= 3pp``)
    is scoped to LIVE current-week detection, where the #128 debounce
    filters transient API zeros. The historical one-shot
    ``_backfill_week_reset_events`` scan has NO debounce, so applying
    reset-to-zero there mis-read a single stale-replica 0% reading as a
    goodwill credit and segmented the week into a degenerate zero-width
    window (the Feb-27 prod regression). Backfill now defers sub-25pp
    resets to the live path. A genuine ≥25pp credit still backfills — see
    ``test_backfill_detects_historical_in_place_credit``.
    """
    end_iso, _ = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=14.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=0.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)

        cnt = conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0]
        assert cnt == 0, "sub-25pp reset-to-zero must NOT backfill (live-only)"
    finally:
        conn.close()


def test_backfill_skips_stale_replica_zero_blip(ns):
    """Regression for the Feb-27 prod false positive.

    A transient stale-replica 0% reading mid-week — usage climbs
    ``6% → 0% → 1%`` on the SAME (future) week_end — must NOT synthesize a
    reset event in backfill. This is the exact shape that segmented the
    ``2026-02-27 → 2026-03-06`` week into a degenerate zero-width window in
    ``get_recent_weeks``: backfill's in-place branch fired on ``6→0`` via
    the lenient reset-to-zero discriminator, and (unlike the live path) had
    no debounce to clear it when usage recovered to ``1%``. With the strict
    >=25pp backfill gate the ``6pp`` drop is ignored.
    """
    end_iso, _ = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=6.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T07:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=0.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T08:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=1.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)

        cnt = conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0]
        assert cnt == 0, "stale-replica 0% blip must not backfill a reset event"
    finally:
        conn.close()


def test_backfill_idempotent_on_rerun(ns):
    """Run ``_backfill_week_reset_events`` twice. Assert: only one event
    row exists (UNIQUE(old_week_end_at, new_week_end_at) + INSERT OR
    IGNORE).
    """
    end_iso, _ = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=2.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)
        ns["_backfill_week_reset_events"](conn)
        ns["_backfill_week_reset_events"](conn)

        cnt = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events"
        ).fetchone()[0]
        assert cnt == 1, "backfill should be idempotent (UNIQUE + IGNORE)"
    finally:
        conn.close()


def test_backfill_preserves_boundary_shift_branch(ns):
    """Boundary-shift legacy: classic mid-week reset where the API
    advances ``week_end_at`` to a new value AND ``weekly_percent``
    drops. Backfill should still emit an event row in the legacy shape
    (``old == prior_end``, ``new == cur_end``, distinct values). This
    is the regression guard for the v1.7.1 path that the in-place
    credit branch must NOT clobber.
    """
    # Future ends so captured < prior_end is satisfied.
    end_1 = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3, hours=0)).replace(
        minute=0, second=0, microsecond=0
    )
    end_2 = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2, hours=0)).replace(
        minute=0, second=0, microsecond=0
    )
    end_1_iso = end_1.isoformat(timespec="seconds")
    end_2_iso = end_2.isoformat(timespec="seconds")
    week_start = (end_1 - dt.timedelta(days=7)).date().isoformat()
    week_start_2 = (end_2 - dt.timedelta(days=7)).date().isoformat()

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start,
            week_end_at=end_1_iso,
            weekly_percent=67.0,
        )
        # Same captured ordering, NEW end_at (Anthropic shifted the boundary),
        # weekly_percent dropped 25+pp.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_2,
            week_end_at=end_2_iso,
            weekly_percent=2.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)

        events = conn.execute(
            "SELECT old_week_end_at, new_week_end_at FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        assert events[0]["old_week_end_at"] == end_1_iso
        assert events[0]["new_week_end_at"] == end_2_iso
    finally:
        conn.close()


# ── Task 5: milestone writer stamps reset_event_id ────────────────────


def _seed_cost_snapshot(
    conn,
    *,
    week_start_date: str,
    week_end_date: str,
    week_start_at: str,
    week_end_at: str,
    cost_usd: float,
    captured_at_utc: str = "2026-05-15T18:00:00Z",
) -> int:
    """Insert a weekly_cost_snapshots row (avoids the milestone writer
    bailing out on missing cost data).
    """
    cur = conn.execute(
        "INSERT INTO weekly_cost_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, "
        " week_start_at, week_end_at, cost_usd, mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (captured_at_utc, week_start_date, week_end_date,
         week_start_at, week_end_at, cost_usd, "auto"),
    )
    rowid = int(cur.lastrowid)
    _stamp_journal_id(conn, "weekly_cost_snapshots", rowid)
    return rowid


def test_milestone_segment_zero_when_no_event(ns):
    """No ``week_reset_events`` row for this week_end_at →
    ``maybe_record_milestone`` writes ``reset_event_id = 0`` (pre-credit
    sentinel).
    """
    end_iso = "2026-05-09T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Cost snapshot so the writer doesn't bail.
        _seed_cost_snapshot(
            conn,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            cost_usd=12.34,
        )
        # Usage snapshot at 3% (so floor(3) = 3 → threshold 3 crosses).
        usage_id = _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-04T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            week_start_at=week_start_at,
            weekly_percent=3.0,
        )
        conn.commit()
    finally:
        conn.close()

    saved = {
        "id": usage_id,
        "weeklyPercent": 3.0,
        "weekStartDate": week_start_date,
        "weekEndDate": week_end_date,
        "weekStartAt": week_start_at,
        "weekEndAt": end_iso,
        "fiveHourPercent": None,
    }
    ns["maybe_record_milestone"](saved)

    conn = ns["open_db"]()
    try:
        # When max_existing is None and current_floor is 3, the writer
        # records only the just-crossed threshold (3), not the prior
        # ones. The point of this test is to assert reset_event_id=0,
        # not the multi-threshold-catchup loop.
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id FROM percent_milestones "
            "WHERE week_start_date = ? ORDER BY percent_threshold",
            (week_start_date,),
        ).fetchall()
        assert len(rows) == 1, rows
        assert rows[0]["percent_threshold"] == 3
        assert rows[0]["reset_event_id"] == 0
    finally:
        conn.close()


def test_milestone_segment_assigned_when_event_active(ns):
    """``week_reset_events`` row exists with effective < captured_at:
    new milestone rows get ``reset_event_id = event.id``.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(
            conn,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            cost_usd=12.34,
        )
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        # Post-reset climb evidence (2026-09-01 incident). A post-credit epoch
        # may seed its milestone ladder only once it holds an observation
        # floored strictly below the threshold being recorded, so a stale
        # pre-credit replica can never open a fresh epoch — see
        # tests/test_milestone_epoch_stale_seed.py. This test is about
        # reset_event_id stamping, not about the seeding policy, so it seeds an
        # evidence row directly. In production that row comes from the ordinary
        # accept path recording a genuine post-credit reading — the auto-credit
        # `_fire_in_place_credit` writes no synthetic snapshot of its own (only
        # the manual record-credit op's `_apply_credit` does). A reset-to-zero
        # therefore supplies it as a matter of course, but a >=25pp goodwill
        # credit to a NON-ZERO level does not, which is the disclosed cost
        # pinned by that module's T5 test.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=0.5,
        )
        # Usage snapshot captured AFTER the credit moment.
        usage_id = _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T18:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=3.0,
        )
        conn.commit()
    finally:
        conn.close()

    saved = {
        "id": usage_id,
        "weeklyPercent": 3.0,
        "weekStartDate": week_start_date,
        "weekEndDate": week_end_date,
        "weekStartAt": week_start_at,
        "weekEndAt": end_iso,
        "fiveHourPercent": None,
        "capturedAt": "2026-05-14T18:00:00Z",
    }
    ns["maybe_record_milestone"](saved)

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id FROM percent_milestones "
            "WHERE week_start_date = ? ORDER BY percent_threshold",
            (week_start_date,),
        ).fetchall()
        # Only threshold 3 lands (no prior max_existing → start at
        # current_floor). The assertion of interest is reset_event_id.
        assert len(rows) == 1, rows
        assert rows[0]["percent_threshold"] == 3
        assert rows[0]["reset_event_id"] == evt_id
    finally:
        conn.close()


def test_milestone_post_credit_threshold_lands_as_new_row(ns):
    """Pre-credit milestone (week, threshold=3, reset_event_id=0) exists.
    Seed event row. Drive ``maybe_record_milestone`` for the same week
    + threshold=3 captured post-event.
    Assert: TWO rows exist for (week, threshold=3) — pre-credit
    reset_event_id=0 + post-credit reset_event_id=event.id.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(
            conn,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            cost_usd=12.34,
        )
        # Pre-credit milestone (reset_event_id = 0).
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 100.0, None, 1, 1, 0),
        )
        # Event row for the credit boundary.
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        # Post-reset climb evidence (2026-09-01 incident). A post-credit epoch
        # may seed its milestone ladder only once it holds an observation
        # floored strictly below the threshold being recorded, so a stale
        # pre-credit replica can never open a fresh epoch — see
        # tests/test_milestone_epoch_stale_seed.py. This test is about
        # reset_event_id stamping, not about the seeding policy, so it seeds an
        # evidence row directly. In production that row comes from the ordinary
        # accept path recording a genuine post-credit reading — the auto-credit
        # `_fire_in_place_credit` writes no synthetic snapshot of its own (only
        # the manual record-credit op's `_apply_credit` does). A reset-to-zero
        # therefore supplies it as a matter of course, but a >=25pp goodwill
        # credit to a NON-ZERO level does not, which is the disclosed cost
        # pinned by that module's T5 test.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=0.5,
        )
        # New usage snapshot at 3%, captured post-event.
        usage_id = _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T18:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=3.0,
        )
        conn.commit()
    finally:
        conn.close()

    saved = {
        "id": usage_id,
        "weeklyPercent": 3.0,
        "weekStartDate": week_start_date,
        "weekEndDate": week_end_date,
        "weekStartAt": week_start_at,
        "weekEndAt": end_iso,
        "fiveHourPercent": None,
        "capturedAt": "2026-05-14T18:00:00Z",
    }
    ns["maybe_record_milestone"](saved)

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id "
            "FROM percent_milestones "
            "WHERE week_start_date = ? AND percent_threshold = 3 "
            "ORDER BY reset_event_id ASC",
            (week_start_date,),
        ).fetchall()
        assert len(rows) == 2, rows
        assert rows[0]["reset_event_id"] == 0
        assert rows[1]["reset_event_id"] == evt_id
    finally:
        conn.close()


# ── Task 6: percent-breakdown filters by active segment ──────────────


def test_percent_breakdown_filters_by_active_segment(ns, capsys):
    """Seed pre-credit + post-credit milestones for the same week +
    threshold. Run ``cmd_percent_breakdown --json``. The active segment
    is the post-credit one; only its rows appear in the milestone list.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit row (segment 0).
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 100.0, None, 1, 1, 0),
        )
        # Event row.
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        # Post-credit row (same threshold, different segment).
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-15T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 12.0, None, 2, 2, evt_id),
        )
        # Seed a usage snapshot so cmd_percent_breakdown resolves this week.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T10:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=3.0,
        )
        conn.commit()
    finally:
        conn.close()

    args = argparse.Namespace(
        week_start=None, week_start_name=None, json=True, tz=None,
    )
    rc = ns["cmd_percent_breakdown"](args)
    assert rc == 0
    captured = capsys.readouterr()
    import json as _json
    out = _json.loads(captured.out)
    assert len(out["milestones"]) == 1, out["milestones"]
    # Post-credit row has cumulative_cost_usd = 12.0; pre-credit had 100.0.
    assert out["milestones"][0]["cumulativeCostUSD"] == 12.0


def test_percent_breakdown_empty_post_credit_hint(ns, capsys):
    """Seed event row + pre-credit milestones but no post-credit ones.
    Run cmd_percent_breakdown. The active segment has no rows, so the
    output should include a clear "post-credit segment, no milestones
    crossed yet" hint instead of the generic "No percent milestones
    recorded for this week" line.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit rows only.
        for pct in (1, 2, 3):
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
                 week_start_at, end_iso, pct, 10.0 * pct, None, pct, pct, 0),
            )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T10:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=1.0,
        )
        conn.commit()
    finally:
        conn.close()

    args = argparse.Namespace(
        week_start=None, week_start_name=None, json=False, tz=None,
    )
    rc = ns["cmd_percent_breakdown"](args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "post-credit" in captured.out.lower(), captured.out


# ── Task 7: dashboard milestone panel filter (shared with TUI) ───────


def test_tui_percent_milestones_filters_to_active_segment(ns):
    """``_tui_build_percent_milestones`` (shared builder for the TUI
    panel AND the dashboard's ``snap.percent_milestones`` envelope
    array) filters to the active segment when a credit event exists for
    the week.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit milestones (segment 0).
        for pct in (1, 2, 3):
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
                 week_start_at, end_iso, pct, 100.0 * pct, None, pct, pct, 0),
            )
        # Event row.
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        # One post-credit milestone.
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-15T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 1, 5.0, None, 4, 4, evt_id),
        )
        # Latest snapshot points the builder at this week.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T10:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=1.0,
        )
        conn.commit()

        out = ns["_tui_build_percent_milestones"](conn)
    finally:
        conn.close()

    # Active segment is evt_id; only the post-credit row (1%, $5.00) shows.
    assert len(out) == 1, out
    assert out[0].percent == 1
    assert out[0].cumulative_cost_usd == 5.0


# ── Task 8: alerts dedup (independent post-credit fire) ──────────────


def test_post_credit_alert_fires_independently(ns):
    """Pre-credit milestone (threshold=3, ``alerted_at=NOW``,
    ``reset_event_id=0``) exists. Drive a post-credit milestone for
    threshold=3 captured after the event. The new row INSERTs at
    ``reset_event_id = event.id`` (cohabits with the pre-credit row
    via the new UNIQUE), ``alerted_at`` gets stamped on the new row
    via the segment-filtered UPDATE, the pre-credit row's
    ``alerted_at`` is untouched.

    This is the alert dedup verification: the alert pipeline reads
    rows via ``maybe_record_milestone``'s INSERT OR IGNORE + UPDATE
    set-then-dispatch flow. Without the segment-filtered UPDATE
    (Task 5), the post-credit row would land but the UPDATE would
    target a row keyed on (week, threshold) ignoring segment — the
    pre-credit row matches WHERE first, gets re-stamped, and the
    post-credit row stays NULL.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"
    pre_alerted_at = "2026-05-12T11:00:00Z"

    conn = ns["open_db"]()
    try:
        # Pre-credit milestone, already alerted.
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, "
            " alerted_at, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 100.0, None, 1, 1,
             pre_alerted_at, 0),
        )
        # Cost snapshot (so the writer doesn't bail).
        _seed_cost_snapshot(
            conn,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            cost_usd=12.0,
        )
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        # Post-reset climb evidence (2026-09-01 incident). A post-credit epoch
        # may seed its milestone ladder only once it holds an observation
        # floored strictly below the threshold being recorded, so a stale
        # pre-credit replica can never open a fresh epoch — see
        # tests/test_milestone_epoch_stale_seed.py. This test is about
        # reset_event_id stamping, not about the seeding policy, so it seeds an
        # evidence row directly. In production that row comes from the ordinary
        # accept path recording a genuine post-credit reading — the auto-credit
        # `_fire_in_place_credit` writes no synthetic snapshot of its own (only
        # the manual record-credit op's `_apply_credit` does). A reset-to-zero
        # therefore supplies it as a matter of course, but a >=25pp goodwill
        # credit to a NON-ZERO level does not, which is the disclosed cost
        # pinned by that module's T5 test.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=0.5,
        )
        usage_id = _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T18:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=3.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Configure alerts: weekly_thresholds = [3] so the threshold-3 alert
    # would fire. ``five_hour_thresholds`` must be non-empty per config
    # validation (paired-axis invariant). Set in config.json.
    ns["save_config"]({"alerts": {"enabled": True,
                                   "weekly_thresholds": [3],
                                   "five_hour_thresholds": [95]}})

    saved = {
        "id": usage_id,
        "weeklyPercent": 3.0,
        "weekStartDate": week_start_date,
        "weekEndDate": week_end_date,
        "weekStartAt": week_start_at,
        "weekEndAt": end_iso,
        "fiveHourPercent": None,
        "capturedAt": "2026-05-14T18:00:00Z",
    }
    ns["maybe_record_milestone"](saved)

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id, alerted_at "
            "FROM percent_milestones "
            "WHERE week_start_date = ? AND percent_threshold = 3 "
            "ORDER BY reset_event_id ASC",
            (week_start_date,),
        ).fetchall()
        assert len(rows) == 2, rows
        # Pre-credit row: alerted_at preserved.
        assert rows[0]["reset_event_id"] == 0
        assert rows[0]["alerted_at"] == pre_alerted_at
        # Post-credit row: alerted_at stamped fresh (some recent ISO).
        assert rows[1]["reset_event_id"] == evt_id
        assert rows[1]["alerted_at"] is not None
        assert rows[1]["alerted_at"] != pre_alerted_at
    finally:
        conn.close()


def test_self_heal_probe_scoped_to_active_segment(ns, monkeypatch):
    """When the live record-usage path bails on dedup-no-insert, the
    self-heal probe re-checks whether a milestone is owed. With a
    credited week + pre-credit MAX=67 in segment 0, the post-credit
    segment N has zero rows — a probe that didn't scope to the segment
    would silently no-op even though the post-credit threshold-1 row
    is owed.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"
    # Pin "now" between the seeded snapshot (2026-05-14T18:00Z) and the
    # week reset (end_iso, 2026-05-16T05:00Z). cmd_record_usage's #112
    # plausibility guard reads now from _command_as_of() and rejects a
    # --resets-at outside [now-30d, now+8d]; without a pin this test was a
    # time-bomb that passed only while the real wall clock sat within 30d
    # of the hardcoded May-2026 dates (it broke on the 2026-06-15 public
    # Linux CI run — issue #200). A fixed AS_OF keeps the band check
    # deterministic AND keeps the in-place credit branch (prior_end_dt >
    # now) firing. Mirrors the CCTALLY_AS_OF pin used by sibling tests.
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-05-15T00:00:00Z")

    conn = ns["open_db"]()
    try:
        # Pre-credit milestones up to threshold 67.
        for pct in (1, 2, 67):
            mcur = conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
                 week_start_at, end_iso, pct, 10.0 * pct, None, pct, pct, 0),
            )
            # Stamp so the harvest skips these pre-seeded rows (their fabricated
            # usage/cost FKs point at non-existent rows and would be an
            # unresolvable reverse-ref if scanned as this-cycle inserts).
            _stamp_journal_id(conn, "percent_milestones", int(mcur.lastrowid))
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        _seed_cost_snapshot(
            conn,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            cost_usd=12.0,
        )
        # Post-reset climb evidence (2026-09-01 incident): the epoch needs an
        # observation floored below the threshold before its ladder may be
        # seeded. After a reset-to-zero the credited ~0 reading, recorded by the
        # ordinary accept path, is exactly this row.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=0.0,
        )
        # Latest snapshot at 1% (post-credit) — but NO milestone row yet
        # in the post-credit segment. The live record-usage path will
        # bail on the dedup since this matches an existing snapshot
        # shape (one-row test setup); the self-heal probe must spot
        # the missing milestone.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T18:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=1.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Drive record-usage with the SAME percent as the latest snapshot
    # to trip the dedup path (forces self-heal to run).
    end_at_epoch = _epoch(end_iso)
    args = _record_usage_args(percent=1.0, resets_at=end_at_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    # The post-credit segment should now have a threshold-1 row.
    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id "
            "FROM percent_milestones "
            "WHERE week_start_date = ? "
            "ORDER BY reset_event_id, percent_threshold",
            (week_start_date,),
        ).fetchall()
        # Pre-credit 1, 2, 67 + post-credit 1 = 4 rows total.
        assert len(rows) == 4, rows
        post = [r for r in rows if r["reset_event_id"] != 0]
        assert len(post) == 1
        assert post[0]["percent_threshold"] == 1
    finally:
        conn.close()


# ── Round-2 review regressions (v1.7.2) ──────────────────────────────


def test_event_row_old_is_effective_not_cur_end(ns, tmp_path):
    """Regression for round-2 review Bug 1: the in-place credit event
    row's ``old_week_end_at`` MUST be the effective reset moment
    (floor-to-hour of now), NOT ``cur_end_canon``. Old shape stored
    ``old==new==cur_end``, which collapsed the credited week to a
    zero-width window in ``_apply_reset_events_to_weekrefs`` because
    both ``pre_map[old]`` and ``post_map[new]`` fired on the same
    WeekRef. New shape is ``(effective, cur_end)`` — distinct values.

    Covers the live detection path; a sibling test
    ``test_backfill_event_row_old_is_effective_not_cur_end`` covers
    the backfill path.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events"
        ).fetchone()
        assert row is not None, "live detection must have written an event row"
        assert row["old_week_end_at"] != row["new_week_end_at"], (
            "old==new collapses the credited week to a zero-width window"
            f" (got old={row['old_week_end_at']!r}, new={row['new_week_end_at']!r})"
        )
        assert row["old_week_end_at"] == row["effective_reset_at_utc"]
        assert row["new_week_end_at"] == end_iso
    finally:
        conn.close()


def test_backfill_event_row_old_is_effective_not_cur_end(ns):
    """Bug-1 regression in the backfill path. Seed a historical
    snapshot pattern (67% then 2%, same end) and call
    ``_backfill_week_reset_events`` directly. The synthesized event
    row must carry ``old_week_end_at == effective_reset_at_utc``,
    NOT ``old == new``.
    """
    end_iso, _ = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        # Wipe any backfill-synthesized rows from the open_db()
        # invocation above; we want a clean slate. The backfill is
        # idempotent (UNIQUE + INSERT OR IGNORE + pre-check) so
        # re-running is safe.
        conn.execute("DELETE FROM week_reset_events")
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=2.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)

        row = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events"
        ).fetchone()
        assert row is not None, "backfill must have synthesized an event row"
        assert row["old_week_end_at"] != row["new_week_end_at"], (
            f"backfill wrote zero-width row: old={row['old_week_end_at']!r},"
            f" new={row['new_week_end_at']!r}"
        )
        assert row["old_week_end_at"] == row["effective_reset_at_utc"]
        assert row["new_week_end_at"] == end_iso
    finally:
        conn.close()


def test_reset_aware_clamp_handles_non_utc_event_offset(ns):
    """Regression for round-2 review Bug 2: the reset-aware DB clamp
    must use ``unixepoch()`` on both sides, not lex string compare.

    Setup: insert a ``week_reset_events`` row with
    ``effective_reset_at_utc='2026-03-01T14:00:00-03:00'`` (BACKFILL-
    shaped event written from a NEGATIVE-offset host before Bug 3 was
    fixed). The real UTC moment is ``2026-03-01T17:00:00Z`` (subtract a
    negative offset = add hours).

    Seed a pre-credit 67% snapshot at ``captured_at_utc=2026-03-01T15:00:00Z``:
      * Real time: 15:00 UTC — BEFORE the credit (17:00 UTC),
        i.e., legitimately pre-credit and MUST be filtered out of the
        post-credit segment's MAX.
      * Lex string compare: ``'2026-03-01T15:00:00Z'`` vs
        ``'2026-03-01T14:00:00-03:00'`` differs at char 12 (`5` > `4`)
        → ``'15:00:00Z'`` is lex-GREATER than ``'14:00:00-03:00'``,
        so a lex ``>=`` filter would WRONGLY INCLUDE the pre-credit
        67% row in the post-credit MAX.

    Drive ``cmd_record_usage(percent=4)``. Under the lex bug, the MAX
    includes 67 → 4 < 67 → ``should_insert = False`` → the new
    post-credit reading is silently dropped. With ``unixepoch()``
    wrapping both sides, 15:00Z = 15 UTC, 14:00-03:00 = 17 UTC; 15 < 17
    so the 67% row is correctly EXCLUDED, MAX = None (no post-credit
    rows yet), and the 4% reading lands.

    Negative-offset hosts are the failure mode where the bug bites the
    clamp; positive-offset hosts trip a different (also-bug) corner
    where real-time-post-credit rows get lex-EXCLUDED from MAX,
    which would also break the post-credit clamp but in a way that
    happens to allow this specific test percent to pass. Negative
    offsets surface the rejection path cleanly.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)
    end_dt = dt.datetime.fromisoformat(end_iso)
    week_start_at = (end_dt - dt.timedelta(days=7)).isoformat(timespec="seconds")
    # Effective floor ~2 days before the window end (IN-window — record-credit
    # M2 unified the clamp onto `_reset_aware_floor`'s window predicate, so the
    # event's effective must fall in [week_start_at, week_end_at); a far-earlier
    # fixed month would no longer floor this week). Store it with a NEGATIVE
    # -03:00 offset (legacy Bug-3 host shape), and a pre-credit captured `Z`
    # string that is LEX-greater than the effective string but unixepoch-EARLIER
    # — the exact lex-vs-unixepoch trap this regression guards.
    # Pin the hour to 12 so the -3h / -1h shifts below never roll the date
    # over (keeps the lex-trap arithmetic on a single calendar day).
    effective_utc = (end_dt - dt.timedelta(days=2)).replace(
        hour=12, minute=0, second=0)
    # `-03:00` wall-clock spelling of effective_utc (== effective_utc - 3h local).
    effective_neg = effective_utc.astimezone(
        dt.timezone(dt.timedelta(hours=-3))).isoformat(timespec="seconds")
    # Pre-credit sample 1h before the floor (unixepoch-earlier), `Z` spelling.
    pre_utc = effective_utc - dt.timedelta(hours=1)
    pre_z = pre_utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    # Guard the trap: the `Z` string is lex-GREATER than the `-03:00` string
    # (so a lexical >= filter would WRONGLY include it) yet the real instant is
    # earlier. If this guard ever fails, the regression is no longer exercised.
    assert pre_z > effective_neg, (pre_z, effective_neg)
    assert pre_utc < effective_utc

    conn = ns["open_db"]()
    try:
        # Pre-credit 67% snapshot — captured BEFORE the credit moment in real
        # time but LEX-greater than the effective string due to the -03:00 offset.
        _seed_usage_snapshot(
            conn,
            captured_at_utc=pre_z,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        # Event row with NEGATIVE-offset effective_reset_at_utc (legacy shape
        # that Bug 3 would have written from a host like America/Buenos_Aires
        # before the .astimezone(UTC) fix).
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            (effective_neg, effective_neg, end_iso, effective_neg),
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(percent=4.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 4.0"
        ).fetchone()[0]
        assert cnt == 1, (
            "post-credit 4% reading must land — the lex bug would have"
            " wrongly INCLUDED the pre-credit 67% row in the segment"
            " MAX (lex '15:00:00Z' > '14:00:00-03:00') and rejected 4%"
            " as a regression"
        )
    finally:
        conn.close()


def test_backfill_writes_effective_with_utc_offset(ns):
    """Regression for round-2 review Bug 3: the backfill's
    ``effective_reset_at_utc`` must be stored with ``+00:00`` offset,
    NOT host-local. ``parse_iso_datetime`` returns ``.astimezone()``
    (host-local fallback), so without the explicit ``.astimezone(UTC)``
    canonicalization before ``isoformat``, the column would be e.g.
    ``+03:00`` on a non-UTC host — breaking lex comparisons in any
    downstream consumer that hasn't yet been upgraded to ``unixepoch()``
    (Bug 2's defense applies to the clamp, but defense-in-depth on
    write keeps future readers safe).
    """
    end_iso, _ = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        conn.execute("DELETE FROM week_reset_events")
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=2.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)

        row = conn.execute(
            "SELECT effective_reset_at_utc FROM week_reset_events"
        ).fetchone()
        assert row is not None
        eff = row["effective_reset_at_utc"]
        # Either trailing `+00:00` or `Z` (both denote UTC) is acceptable;
        # a host-local offset like `+03:00` is the bug.
        assert eff.endswith("+00:00") or eff.endswith("Z"), (
            f"effective_reset_at_utc must be UTC; got {eff!r}"
        )
    finally:
        conn.close()


def test_alerts_envelope_id_unique_across_segments(ns):
    """Regression for round-2 review Bug 4: the dashboard's alerts
    envelope id MUST include ``reset_event_id`` so pre-credit (segment
    0) and post-credit (segment N) alerts at the same
    (week_start_date, threshold) don't collide on the React key.

    Without the segment in the id, both rows render with the same
    ``id``, causing duplicate-key warnings and non-deterministic
    render order in ``<li key={a.id}>`` / ``<tr key={a.id}>``.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit alerted milestone (reset_event_id = 0).
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, "
            " alerted_at, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 100.0, None, 1, 1,
             "2026-05-12T11:00:00Z", 0),
        )
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective="2026-05-14T17:00:00+00:00",
            old_week_end_at="2026-05-14T17:00:00+00:00",
        )
        # Post-credit alerted milestone at the SAME (week, threshold)
        # but reset_event_id = evt_id. Under the old envelope-id
        # format these two rows would collide on
        # `weekly:<week>:<threshold>`.
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, "
            " alerted_at, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-14T20:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 5.0, None, 2, 2,
             "2026-05-14T20:00:00Z", evt_id),
        )
        conn.commit()

        dashboard_mod = ns["_cctally_dashboard"]
        envelope = dashboard_mod._build_alerts_envelope_array(conn)
        weekly = [a for a in envelope if a.get("axis") == "weekly"
                  and a.get("threshold") == 3
                  and a.get("context", {}).get("week_start_date") == week_start_date]
        assert len(weekly) == 2, (
            "both pre-credit and post-credit alerted rows must surface"
            f" (got {len(weekly)}: {weekly})"
        )
        ids = [a["id"] for a in weekly]
        assert len(set(ids)) == 2, (
            f"alerts envelope ids must be unique across segments; got {ids}"
        )
        assert all(s.startswith(f"weekly:{week_start_date}:3:") for s in ids), ids
    finally:
        conn.close()


# ── Round-3 user-test regressions (v1.7.2) ───────────────────────────


def test_credit_branch_keeps_pre_observation_rows_and_still_seeds(
    ns, monkeypatch, tmp_path
):
    """Bug A, re-decided by #703 + #707 §5.1 and §5.3.

    Failure mode the user hit: between the moment Anthropic credited the user
    and the next `cctally record-usage` invocation, the EXTERNAL
    claude-statusline tool replayed stale pre-credit ``--percent 67`` values
    (its in-memory HWM cache had not caught up). Those replays dominated the
    reset-aware clamp's MAX over the post-credit segment, so legitimate fresh
    OAuth values were rejected.

    The original fix DELETED every row captured at or after the HOUR-FLOORED
    effective instant whose percent sat within 1.0pp of the pre-credit value.
    Both halves of that predicate are now wrong, and the second is what caused
    the 2026-09-01 incident.

    The rows this test seeds are captured BEFORE the credit was observed, and
    §5.1 keeps them deliberately: at their capture instant a high reading is
    indistinguishable from genuine pre-credit history, and only a later
    contradicting observation could tell them apart. Deleting them destroys a
    true observation whenever the reading was real. What stops them dominating
    the clamp is §5.3's floor, which is the exact observation instant rather
    than the hour floor — so they fall OUTSIDE the epoch and the 2% seed lands
    for a reason that does not require guessing about them.
    """
    observed = _pin_observation_clock(monkeypatch)
    end_iso, end_epoch = _future_week_end_iso(observed)
    week_start_date, week_end_date = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        # 1. Pre-credit baseline snapshot at 67%.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        # 2. Race condition: the EXTERNAL statusline tool already wrote rows
        # still carrying the stale 67% value. Their placement is what this test
        # discriminates on: both are captured at or after the HOUR FLOOR of the
        # observation instant and strictly before that instant, so the
        # hour-floored rule puts them inside the epoch and §5.3's
        # observation-instant rule does not. Critically, they are also the MOST
        # RECENT prior snapshots, so the in-place credit detection branch reads
        # weekly_percent=67 as `prior_pct` (latest row by captured_at_utc DESC)
        # — that is what fires the detection (prior=67 vs new=2 = 65pp drop).
        floor_hour = observed.replace(minute=0, second=0)
        # Two stale-replay rows captured AT and just-after the floor.
        _seed_usage_snapshot(
            conn,
            captured_at_utc=floor_hour.isoformat().replace("+00:00", "Z"),
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc=(floor_hour + dt.timedelta(minutes=5))
            .isoformat().replace("+00:00", "Z"),
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Drive the credit branch. percent=2 < clamp MAX over the stale 67%
    # replays if the cleanup didn't run; with cleanup, the post-credit
    # segment's MAX is 5% (the legitimate survivor), and 2% is still
    # below that — but the seed snapshot path runs in this same
    # invocation. We mainly assert the DELETE happened.
    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        # Event row written (proves we entered the credit branch).
        events = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, "
            "       effective_reset_at_utc FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        effective_iso = events[0]["effective_reset_at_utc"]

        # Every 67% row is KEPT. All three were captured before the credit was
        # observed, so nothing contradicts them; §5.1 removes only rows the two
        # bounding observations disagree with.
        kept_67 = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? AND round(weekly_percent, 1) = 67.0",
            (week_start_date,),
        ).fetchone()[0]
        assert kept_67 == 3, (
            "genuine pre-observation readings were deleted as replicas"
        )

        # The accounting floor is the EXACT observation instant, which is later
        # than every one of them, so none is inside the epoch.
        observed_at = conn.execute(
            "SELECT observed_at_utc FROM week_reset_events"
        ).fetchone()["observed_at_utc"]
        assert observed_at is not None
        in_epoch_67 = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "  AND round(weekly_percent, 1) = 67.0",
            (week_start_date, observed_at),
        ).fetchone()[0]
        assert in_epoch_67 == 0, (
            "the hour floor is still what bounds the epoch"
        )

        # And the 2% seed lands, which is the behaviour the deletion used to
        # buy: with the floor at the observation instant the segment's MAX no
        # longer sees the 67% rows, so 2% is not read as a regression.
        seed_landed = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? AND round(weekly_percent, 1) = 2.0",
            (week_start_date,),
        ).fetchone()[0]
        assert seed_landed == 1, (
            "post-credit seed snapshot must land"
        )
    finally:
        conn.close()


def test_credit_branch_needs_no_tolerance_band_for_rounding_drift(
    ns, monkeypatch, tmp_path
):
    """Issue #45's rounding-drift concern, answered without a band.

    The concern was real: if Anthropic ever rounded the ``--percent`` payload
    differently from the OAuth API, a replay at ``67.5`` against a stored
    ``prior_pct = 67.4`` would survive a strict equality predicate, dominate the
    reset-aware clamp's MAX and mask legitimate post-credit values. The answer
    at the time was a 1.0pp tolerance band around the pre-credit value.

    That band is what failed on 2026-09-01. On the debounced leg the value it
    compares against is the armed marker's baseline while the rows to remove
    hold what was written, and the two differed by exactly 1.0 — a strict
    ``< 1.0`` excluded every row and the DELETE did nothing. A band cannot be
    repaired by widening it, because the two quantities have no bounded
    relationship.

    #703 + #707 removes the need for one. The drift rows here are captured
    BEFORE the credit was observed, so §5.1 keeps them and §5.3's floor — the
    exact observation instant rather than the hour floor — puts them outside the
    epoch. The 2% seed lands regardless of how far the drift is from the stored
    baseline, which is a stronger property than any band width.
    """
    observed = _pin_observation_clock(monkeypatch)
    end_iso, end_epoch = _future_week_end_iso(observed)
    week_start_date, week_end_date = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.4,
        )
        # Both drift rows sit at or after the hour floor of the observation
        # instant and strictly before that instant, for the reason the sibling
        # test above states.
        floor_hour = observed.replace(minute=0, second=0)
        _seed_usage_snapshot(
            conn,
            captured_at_utc=(floor_hour + dt.timedelta(minutes=5))
            .isoformat().replace("+00:00", "Z"),
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.5,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc=(floor_hour + dt.timedelta(minutes=6))
            .isoformat().replace("+00:00", "Z"),
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.4,
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT effective_reset_at_utc FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        effective_iso = events[0]["effective_reset_at_utc"]

        # Every pre-observation reading is KEPT, drift and all. Their capture
        # instants precede the credit's own observation, so nothing contradicts
        # them and §5.1 removes only contradicted rows.
        kept = sorted(
            r["weekly_percent"] for r in conn.execute(
                "SELECT weekly_percent FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? AND weekly_percent > 60",
                (week_start_date,)))
        assert kept == [67.4, 67.4, 67.5], kept

        # None of them is inside the epoch, because the floor is the exact
        # observation instant rather than the hour floor.
        observed_at = conn.execute(
            "SELECT observed_at_utc FROM week_reset_events"
        ).fetchone()["observed_at_utc"]
        assert observed_at is not None
        in_epoch = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "  AND weekly_percent > 60",
            (week_start_date, observed_at),
        ).fetchone()[0]
        assert in_epoch == 0

        # So the 2% seed lands, with no band and no dependence on how far the
        # drift sits from the stored baseline.
        seed_landed = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND ABS(weekly_percent - 2.0) < 0.01",
            (week_start_date,),
        ).fetchone()[0]
        assert seed_landed == 1, (
            "post-credit 2% seed must land"
        )

        # The event row's observed_pre_credit_pct stamps the value we
        # observed (67.4 — the value that drove prior_pct at the SELECT
        # site). Decouples future cleanup tooling from re-deriving
        # prior_pct.
        ev = conn.execute(
            "SELECT observed_pre_credit_pct FROM week_reset_events"
        ).fetchone()
        assert ev["observed_pre_credit_pct"] is not None
        assert abs(float(ev["observed_pre_credit_pct"]) - 67.4) < 0.01
    finally:
        conn.close()


# ── Round-3 Bug B: pre-credit ref synthesis ──────────────────────────


def test_apply_reset_events_keeps_a_credited_week_whole(ns):
    """A credited week is ONE reference on its original boundaries.

    This test asserted the opposite until #703 + #707. It pinned a synthesized
    pre-credit reference closed at `effective_reset_at_utc` alongside a
    post-credit one re-anchored to it, so one unchanged subscription week
    rendered as two rows.

    Section 2 is the rule that reverses it: an Anthropic reset never changes the
    week's boundaries. The credit still defines an accounting epoch — a
    milestone ladder segment, a cost range, a high-water-mark floor — and that
    epoch is what the accounting reads consult. It defines no display boundary.
    """
    end_iso = "2026-05-16T17:00:00+00:00"
    effective_iso = "2026-05-15T17:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = "2026-05-09T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective_iso,
            old_week_end_at=effective_iso,
        )
        ref = ns["make_week_ref"](
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
        )
        out = ns["_apply_reset_events_to_weekrefs"](conn, [ref])
    finally:
        conn.close()

    assert len(out) == 1, f"the credited week was split again: {out}"
    assert out[0].week_start_at == week_start_at
    assert out[0].week_end_at == end_iso
    assert out[0].key == ref.key


def test_a_boundary_shift_is_neither_truncated_nor_re_anchored(ns):
    """An event with ``old != effective`` is a boundary CHANGE.

    Neither reference is touched any more. The pre-reset week used to be
    truncated at the credit moment and the post-reset week's start moved there;
    both are display boundaries a credit is not allowed to define.

    Recovering the ORIGINAL cadence for this shape — §7's coalescing — is
    deliberately not implemented; `tests/test_cadence_walk.py` records why.
    """
    pre_end_iso = "2026-05-10T17:00:00+00:00"
    new_end_iso = "2026-05-12T19:00:00+00:00"
    effective_iso = "2026-05-12T19:00:00+00:00"
    old_end_iso = pre_end_iso

    pre_week_start_at = "2026-05-03T17:00:00+00:00"
    post_week_start_at = "2026-05-05T19:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_reset_event(
            conn,
            new_week_end_at=new_end_iso,
            effective=effective_iso,
            old_week_end_at=old_end_iso,
        )
        pre_ref = ns["make_week_ref"](
            week_start_date="2026-05-03",
            week_end_date="2026-05-10",
            week_start_at=pre_week_start_at,
            week_end_at=old_end_iso,
        )
        post_ref = ns["make_week_ref"](
            week_start_date="2026-05-05",
            week_end_date="2026-05-12",
            week_start_at=post_week_start_at,
            week_end_at=new_end_iso,
        )
        out = ns["_apply_reset_events_to_weekrefs"](
            conn, [post_ref, pre_ref],
        )
    finally:
        conn.close()

    assert len(out) == 2, out
    moved = next(r for r in out if r.key == "2026-05-05")
    assert moved.week_start_at == post_week_start_at, (
        "the post-reset week was re-anchored to the credit moment")
    assert moved.week_end_at == new_end_iso

    unmoved = next(r for r in out if r.key == "2026-05-03")
    assert unmoved.week_start_at == pre_week_start_at
    assert unmoved.week_end_at == old_end_iso, (
        "the pre-reset week was truncated at the credit moment")


def test_trend_table_shows_one_row_for_a_credited_week(ns, capsys):
    """End-to-end: `cmd_report` emits ONE trend row for a credited week.

    It emitted two — a pre-credit segment closed at `effective` and a
    post-credit one opened there — for a subscription week whose boundaries
    never moved. The single row keeps the original window and reports the live
    counter, which is the post-credit value.
    """
    import json
    end_iso = "2026-05-16T17:00:00+00:00"
    effective_iso = "2026-05-15T17:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = "2026-05-09T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit snapshot at 67%.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T16:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        # Post-credit snapshot at 4%.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T20:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=4.0,
        )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective_iso,
            old_week_end_at=effective_iso,
        )
        conn.commit()
    finally:
        conn.close()

    rc = ns["cmd_report"](argparse.Namespace(
        weeks=1,
        sync_current=False,
        week_start_name=None,
        mode="auto",
        offline=True,
        project=None,
        json=True,
        detail=False,
        format=None,
        theme=None,
        reveal_projects=False,
        no_branding=False,
        output=None,
        copy=False,
        open=False,
        tz=None,
    ))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    trend = payload["trend"]
    credited = [
        r for r in trend if r["weekStartDate"] == week_start_date
    ]
    assert len(credited) == 1, f"the credited week is still split: {credited}"
    assert credited[0]["weekEndAt"] == end_iso, credited[0]
    assert credited[0]["weekStartAt"] == week_start_at, credited[0]
    assert credited[0]["weeklyPercent"] == 4.0, credited[0]
    assert effective_iso not in {r["weekEndAt"] for r in trend}, (
        "a segment boundary reached the display layer")


# ── Round-3 Bug C: cctally blocks uses API-anchored data ─────────────


def _seed_five_hour_block_row(
    conn,
    *,
    five_hour_resets_at: str,
    block_start_at: str,
    five_hour_window_key: int,
    final_pct: float = 50.0,
) -> int:
    """Insert a minimal ``five_hour_blocks`` row and return its id."""
    cur = conn.execute(
        "INSERT INTO five_hour_blocks "
        "(five_hour_window_key, five_hour_resets_at, block_start_at, "
        " first_observed_at_utc, last_observed_at_utc, "
        " final_five_hour_percent, is_closed, "
        " created_at_utc, last_updated_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (five_hour_window_key, five_hour_resets_at, block_start_at,
         block_start_at, block_start_at, final_pct,
         block_start_at, block_start_at),
    )
    return int(cur.lastrowid)


def test_blocks_anchor_picks_five_hour_blocks_when_available(ns):
    """Bug C: ``_load_recorded_five_hour_windows`` must also pull
    ``five_hour_resets_at`` from the canonical ``five_hour_blocks``
    rollup table — and the canonical entry must dominate over any
    jittered raw value sharing the same 10-minute floor.
    """
    # Use a moment in the recent past so it falls inside the default
    # range (2020-01-01 → now widened by 5h).
    canonical_resets = "2026-05-15T22:50:00+00:00"  # 17:50Z block start
    block_start = "2026-05-15T17:50:00+00:00"

    conn = ns["open_db"]()
    try:
        # Seed a weekly_usage_snapshots row whose
        # `five_hour_resets_at` is JITTERED away from the canonical
        # value by less than 10 minutes — these should collapse to the
        # same floored bucket and the canonical (heavy-weight) entry
        # should dominate.
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " source, payload_json, "
            " five_hour_percent, five_hour_resets_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-15T20:00:00Z", "2026-05-09", "2026-05-16",
             "2026-05-09T17:00:00+00:00", "2026-05-16T17:00:00+00:00",
             5.0, "test", "{}", 25.0,
             "2026-05-15T22:48:00+00:00"),  # 2min jitter from canonical
        )
        _seed_five_hour_block_row(
            conn,
            five_hour_resets_at=canonical_resets,
            block_start_at=block_start,
            five_hour_window_key=int(
                dt.datetime.fromisoformat(canonical_resets).timestamp()
            ),
        )
        conn.commit()
    finally:
        conn.close()

    range_start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    range_end = dt.datetime(2026, 5, 16, tzinfo=dt.timezone.utc)
    windows, _overrides, _intervals = ns["_load_recorded_five_hour_windows"](
        range_start, range_end,
    )

    assert len(windows) >= 1, f"expected at least one window, got {windows}"

    # The returned anchor must be the floored canonical value (22:50Z).
    expected_floor = dt.datetime(
        2026, 5, 15, 22, 50, 0, tzinfo=dt.timezone.utc,
    )
    assert expected_floor in windows, (
        f"canonical 22:50Z anchor missing from windows: {windows}"
    )


def test_blocks_anchor_falls_back_when_no_five_hour_blocks_row(ns):
    """Bug C regression guard: when the ``five_hour_blocks`` table is
    empty (no canonical anchors available), ``_load_recorded_five_hour_windows``
    must still return raw-snapshot anchors. Heuristic behavior unchanged
    in this case.
    """
    raw_resets = "2026-05-15T22:48:00+00:00"

    conn = ns["open_db"]()
    try:
        # Seed several weekly_usage_snapshots rows with the same raw
        # reset (count >= 1 to survive the
        # _select_non_overlapping_recorded_windows filter).
        for i in range(3):
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, "
                " source, payload_json, "
                " five_hour_percent, five_hour_resets_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"2026-05-15T20:0{i}:00Z", "2026-05-09", "2026-05-16",
                 "2026-05-09T17:00:00+00:00",
                 "2026-05-16T17:00:00+00:00",
                 5.0 + i, "test", "{}", 25.0 + i, raw_resets),
            )
        conn.execute("DELETE FROM five_hour_blocks")
        conn.commit()
    finally:
        conn.close()

    range_start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    range_end = dt.datetime(2026, 5, 16, tzinfo=dt.timezone.utc)
    windows, _overrides, _intervals = ns["_load_recorded_five_hour_windows"](
        range_start, range_end,
    )

    # Raw anchor (floored to 22:40Z because :48 floors to :40 in
    # 10-minute buckets).
    expected_floor = dt.datetime(
        2026, 5, 15, 22, 40, 0, tzinfo=dt.timezone.utc,
    )
    assert expected_floor in windows, (
        f"raw-snapshot anchor missing without five_hour_blocks: {windows}"
    )


# ── Round-4 Bug D: cmd_report current-row picks post-credit ref ───────


def test_cmd_report_current_row_is_the_whole_credited_week(ns, capsys):
    """Bug D's disambiguation has nothing left to disambiguate.

    It existed because a credited week produced two references sharing
    `WeekRef.key`, so the "current week" summary box matched both and
    last-write-wins rendered the closed pre-credit segment. There is one
    reference now, on the week's original boundaries, and it reports the live
    counter.
    """
    import json
    end_iso = "2026-05-16T17:00:00+00:00"
    effective_iso = "2026-05-15T17:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = "2026-05-09T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit snapshot at 67%, captured BEFORE the effective
        # reset moment.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T16:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        # Post-credit snapshot at 4%, captured AFTER the effective
        # reset moment (so it sorts as `latest_usage`).
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T20:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=4.0,
        )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective_iso,
            old_week_end_at=effective_iso,
        )
        conn.commit()
    finally:
        conn.close()

    rc = ns["cmd_report"](argparse.Namespace(
        weeks=1,
        sync_current=False,
        week_start_name=None,
        mode="auto",
        offline=True,
        project=None,
        json=True,
        detail=False,
        format=None,
        theme=None,
        reveal_projects=False,
        no_branding=False,
        output=None,
        copy=False,
        open=False,
        tz=None,
    ))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    current = payload["current"]
    assert current is not None, payload
    assert current["weekStartAt"] == week_start_at, current
    assert current["weekEndAt"] == end_iso, current
    assert current["weeklyPercent"] == 4.0, current
    assert payload["currentWeek"]["weekStartAt"] == week_start_at


def test_cmd_report_current_row_legacy_uncredited_week(ns, capsys):
    """Bug D regression guard: an uncredited week (single ref, no
    reset event row) must still pick its sole ref as the current row.
    The round-4 fix re-routes ``current_ref`` through
    ``_apply_reset_events_to_weekrefs`` but with no events the function
    is a no-op (returns ``refs`` unchanged), so non-credited weeks are
    unaffected.
    """
    import json
    end_iso = "2026-05-16T17:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = "2026-05-09T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T20:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=42.0,
        )
        conn.commit()
    finally:
        conn.close()

    rc = ns["cmd_report"](argparse.Namespace(
        weeks=1,
        sync_current=False,
        week_start_name=None,
        mode="auto",
        offline=True,
        project=None,
        json=True,
        detail=False,
        format=None,
        theme=None,
        reveal_projects=False,
        no_branding=False,
        output=None,
        copy=False,
        open=False,
        tz=None,
    ))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    current = payload["current"]
    assert current is not None
    assert current["weekStartAt"] == week_start_at
    assert current["weekEndAt"] == end_iso
    assert current["weeklyPercent"] == 42.0


# ── Round-4 Bug E: cmd_blocks active row swaps to canonical ──────────


def test_blocks_active_uses_five_hour_blocks_when_anchor_differs(
    ns, monkeypatch
):
    """Bug E (v1.7.2 round-4): when the ACTIVE 5h block is heuristic-
    anchored (e.g. activity restarted at 23:00 IDT after a gap) but a
    canonical ``five_hour_blocks`` row pins the current API window
    elsewhere (e.g. 20:50 IDT), ``cmd_blocks`` must surface the
    API-anchored window for the ACTIVE row — heuristic and canonical
    can sit in different 10-minute floor buckets, so the round-3
    anchor-overlay in ``_load_recorded_five_hour_windows`` doesn't
    catch this case.
    """
    # API-anchored window: 20:50 IDT (17:50Z) start, 01:50 IDT (22:50Z) end.
    # Heuristic anchor would be at 23:00 IDT (20:00Z) — 130 min later.
    canonical_block_start = "2026-05-15T17:50:00+00:00"
    canonical_resets_at = "2026-05-15T22:50:00+00:00"
    canonical_key = int(dt.datetime.fromisoformat(canonical_resets_at).timestamp())

    # Pin "now" between 23:00 IDT (heuristic anchor) and the canonical
    # window end (22:50Z), so the canonical window is still ACTIVE.
    now_utc = dt.datetime(2026, 5, 15, 20, 30, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", now_utc.isoformat(timespec="seconds"))

    conn = ns["open_db"]()
    try:
        # Seed a weekly_usage_snapshots row that pins the live
        # five_hour_window_key. _maybe_swap_active_block_to_canonical
        # picks the latest snapshot's key.
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " source, payload_json, "
            " five_hour_percent, five_hour_resets_at, "
            " five_hour_window_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_utc.isoformat(timespec="seconds"),
             "2026-05-09", "2026-05-16",
             "2026-05-09T17:00:00+00:00", "2026-05-16T17:00:00+00:00",
             5.0, "test", "{}", 25.0,
             canonical_resets_at, canonical_key),
        )
        # Seed the canonical five_hour_blocks row.
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, is_closed, "
            " created_at_utc, last_updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (canonical_key, canonical_resets_at, canonical_block_start,
             canonical_block_start, canonical_block_start,
             25.0, canonical_block_start, canonical_block_start),
        )
        conn.commit()
    finally:
        conn.close()

    # Build a heuristic ACTIVE block at 23:00 IDT — emulates the post-
    # gap reanchoring `_group_entries_into_blocks` produces from real
    # JSONL activity.
    Block = ns["Block"]
    heuristic_start = dt.datetime(
        2026, 5, 15, 20, 0, 0, tzinfo=dt.timezone.utc,
    )  # 23:00 IDT
    heuristic_end = heuristic_start + dt.timedelta(hours=5)
    blocks = [
        Block(
            start_time=heuristic_start,
            end_time=heuristic_end,
            actual_end_time=now_utc,
            is_active=True,
            is_gap=False,
            entries_count=3,
            input_tokens=100,
            output_tokens=200,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            total_tokens=300,
            cost_usd=1.23,
            models=["claude-opus-4-7"],
            burn_rate=None,
            projection=None,
            anchor="heuristic",
        ),
    ]

    ns["_maybe_swap_active_block_to_canonical"](blocks, [], now=now_utc)

    # The active block's times must be rewritten to the canonical
    # window and its anchor flipped to "recorded".
    active = blocks[0]
    expected_start = dt.datetime.fromisoformat(
        canonical_block_start
    ).astimezone(dt.timezone.utc)
    expected_end = dt.datetime.fromisoformat(
        canonical_resets_at
    ).astimezone(dt.timezone.utc)
    assert active.start_time == expected_start, active.start_time
    assert active.end_time == expected_end, active.end_time
    assert active.anchor == "recorded", active.anchor


def test_blocks_active_falls_back_when_no_canonical_row(ns, monkeypatch):
    """Bug E regression guard: when no ``five_hour_blocks`` row matches
    the live key (or the table is empty), the heuristic ACTIVE block
    is preserved verbatim — same times, ``anchor="heuristic"``, so the
    renderer keeps the ``~`` prefix.
    """
    now_utc = dt.datetime(2026, 5, 15, 20, 30, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", now_utc.isoformat(timespec="seconds"))

    # No five_hour_blocks row at all. (Setup doesn't need to seed
    # weekly_usage_snapshots either — the function returns early when
    # no snapshot has a five_hour_window_key.)
    Block = ns["Block"]
    heuristic_start = dt.datetime(
        2026, 5, 15, 20, 0, 0, tzinfo=dt.timezone.utc,
    )
    heuristic_end = heuristic_start + dt.timedelta(hours=5)
    blocks = [
        Block(
            start_time=heuristic_start,
            end_time=heuristic_end,
            actual_end_time=now_utc,
            is_active=True,
            is_gap=False,
            entries_count=3,
            input_tokens=100,
            output_tokens=200,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            total_tokens=300,
            cost_usd=1.23,
            models=["claude-opus-4-7"],
            burn_rate=None,
            projection=None,
            anchor="heuristic",
        ),
    ]

    ns["_maybe_swap_active_block_to_canonical"](blocks, [], now=now_utc)

    # Active block unchanged.
    active = blocks[0]
    assert active.start_time == heuristic_start
    assert active.end_time == heuristic_end
    assert active.anchor == "heuristic"


def test_blocks_active_skips_when_canonical_window_already_closed(
    ns, monkeypatch
):
    """Bug E corner case: if the canonical ``five_hour_blocks`` row's
    ``five_hour_resets_at`` is already in the past relative to ``now``,
    the canonical block is closed — the heuristic active block reflects
    a NEW window's worth of real activity and must NOT be overwritten.
    """
    # Canonical window: 12:50Z → 17:50Z (already closed at 20:30Z).
    canonical_block_start = "2026-05-15T12:50:00+00:00"
    canonical_resets_at = "2026-05-15T17:50:00+00:00"
    canonical_key = int(dt.datetime.fromisoformat(canonical_resets_at).timestamp())
    now_utc = dt.datetime(2026, 5, 15, 20, 30, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", now_utc.isoformat(timespec="seconds"))

    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " source, payload_json, "
            " five_hour_percent, five_hour_resets_at, "
            " five_hour_window_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_utc.isoformat(timespec="seconds"),
             "2026-05-09", "2026-05-16",
             "2026-05-09T17:00:00+00:00", "2026-05-16T17:00:00+00:00",
             5.0, "test", "{}", 25.0,
             canonical_resets_at, canonical_key),
        )
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, is_closed, "
            " created_at_utc, last_updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (canonical_key, canonical_resets_at, canonical_block_start,
             canonical_block_start, canonical_block_start,
             80.0, canonical_block_start, canonical_block_start),
        )
        conn.commit()
    finally:
        conn.close()

    Block = ns["Block"]
    heuristic_start = dt.datetime(
        2026, 5, 15, 20, 0, 0, tzinfo=dt.timezone.utc,
    )
    heuristic_end = heuristic_start + dt.timedelta(hours=5)
    blocks = [
        Block(
            start_time=heuristic_start,
            end_time=heuristic_end,
            actual_end_time=now_utc,
            is_active=True,
            is_gap=False,
            entries_count=3,
            input_tokens=100,
            output_tokens=200,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            total_tokens=300,
            cost_usd=1.23,
            models=["claude-opus-4-7"],
            burn_rate=None,
            projection=None,
            anchor="heuristic",
        ),
    ]

    ns["_maybe_swap_active_block_to_canonical"](blocks, [], now=now_utc)

    active = blocks[0]
    # Heuristic times preserved; anchor unchanged.
    assert active.start_time == heuristic_start
    assert active.end_time == heuristic_end
    assert active.anchor == "heuristic"


def test_blocks_active_swap_recomputes_totals_over_canonical_interval(
    ns, monkeypatch
):
    """Bug F regression guard (v1.7.2 round-5).

    Round-4's swap only rewrote ``start_time`` / ``end_time`` and flipped
    ``anchor`` to ``"recorded"`` — token / cost totals were left at the
    heuristic-grouped values. On live data the heuristic anchor at 23:00 IDT
    captured only entries from 23:00 onward (~$45), but the canonical
    window started 2h 10min earlier at 20:50 IDT and contains ~$128 of
    activity. Result: the displayed window said 20:50 → 01:50 with $45,
    confusingly mismatched.

    This test seeds entries spanning the WIDER canonical window and a
    heuristic block that only sees a slice of them, then verifies that
    after the swap the block's totals reflect the full canonical
    interval.
    """
    # Canonical window: 17:50Z → 22:50Z (5h, currently active).
    canonical_block_start = "2026-05-15T17:50:00+00:00"
    canonical_resets_at = "2026-05-15T22:50:00+00:00"
    canonical_key = int(dt.datetime.fromisoformat(canonical_resets_at).timestamp())
    now_utc = dt.datetime(2026, 5, 15, 21, 30, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", now_utc.isoformat(timespec="seconds"))

    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " source, payload_json, "
            " five_hour_percent, five_hour_resets_at, "
            " five_hour_window_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_utc.isoformat(timespec="seconds"),
             "2026-05-09", "2026-05-16",
             "2026-05-09T17:00:00+00:00", "2026-05-16T17:00:00+00:00",
             5.0, "test", "{}", 25.0,
             canonical_resets_at, canonical_key),
        )
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, is_closed, "
            " created_at_utc, last_updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (canonical_key, canonical_resets_at, canonical_block_start,
             canonical_block_start, canonical_block_start,
             25.0, canonical_block_start, canonical_block_start),
        )
        conn.commit()
    finally:
        conn.close()

    # Build a synthetic UsageEntry list spanning the canonical interval:
    # one entry early (17:55Z — INSIDE canonical, OUTSIDE heuristic) and
    # one entry late (20:30Z — inside both).
    UsageEntry = ns["UsageEntry"]
    early = UsageEntry(
        timestamp=dt.datetime(2026, 5, 15, 17, 55, 0, tzinfo=dt.timezone.utc),
        model="claude-opus-4-7",
        usage={
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 0,
        },
        cost_usd=None,
        source_path="/tmp/synth.jsonl",
    )
    late = UsageEntry(
        timestamp=dt.datetime(2026, 5, 15, 20, 30, 0, tzinfo=dt.timezone.utc),
        model="claude-opus-4-7",
        usage={
            "input_tokens": 2000,
            "output_tokens": 1000,
            "cache_creation_input_tokens": 400,
            "cache_read_input_tokens": 100,
        },
        cost_usd=None,
        source_path="/tmp/synth.jsonl",
    )
    all_entries = [early, late]

    # Heuristic block at 20:00Z (only catches `late`).
    Block = ns["Block"]
    heuristic_start = dt.datetime(2026, 5, 15, 20, 0, 0, tzinfo=dt.timezone.utc)
    heuristic_end = heuristic_start + dt.timedelta(hours=5)
    # Stub a heuristic block whose totals reflect only `late`.
    _build = ns["_build_activity_block"]
    heuristic_block = _build(
        [late], heuristic_start, heuristic_end, now_utc, "auto",
        anchor="heuristic",
    )
    blocks = [heuristic_block]
    heuristic_cost = heuristic_block.cost_usd
    heuristic_input = heuristic_block.input_tokens

    ns["_maybe_swap_active_block_to_canonical"](blocks, all_entries, now=now_utc)

    active = blocks[0]
    expected_start = dt.datetime.fromisoformat(
        canonical_block_start
    ).astimezone(dt.timezone.utc)
    expected_end = dt.datetime.fromisoformat(
        canonical_resets_at
    ).astimezone(dt.timezone.utc)

    # Timestamps + anchor: canonical.
    assert active.start_time == expected_start
    assert active.end_time == expected_end
    assert active.anchor == "recorded"

    # Totals MUST cover both entries (early + late), strictly greater
    # than the heuristic-only baseline. This is the Bug F invariant.
    assert active.input_tokens == 3000, active.input_tokens
    assert active.input_tokens > heuristic_input
    assert active.cost_usd > heuristic_cost
    assert active.entries_count == 2


def test_blocks_active_swap_threads_mode(ns, monkeypatch):
    """Session C (Codex F1): the active canonical-swap must honor --mode.
    The swap rebuilds the active block via _build_activity_block; pre-fix it
    hardcoded "auto", so calculate/display were ignored on the active block."""
    canonical_block_start = "2026-05-15T17:50:00+00:00"
    canonical_resets_at = "2026-05-15T22:50:00+00:00"
    canonical_key = int(dt.datetime.fromisoformat(canonical_resets_at).timestamp())
    now_utc = dt.datetime(2026, 5, 15, 21, 30, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", now_utc.isoformat(timespec="seconds"))

    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json, "
            " five_hour_percent, five_hour_resets_at, five_hour_window_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_utc.isoformat(timespec="seconds"), "2026-05-09", "2026-05-16",
             "2026-05-09T17:00:00+00:00", "2026-05-16T17:00:00+00:00", 5.0,
             "test", "{}", 25.0, canonical_resets_at, canonical_key),
        )
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, is_closed, created_at_utc, "
            " last_updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (canonical_key, canonical_resets_at, canonical_block_start,
             canonical_block_start, canonical_block_start, 25.0,
             canonical_block_start, canonical_block_start),
        )
        conn.commit()
    finally:
        conn.close()

    UsageEntry = ns["UsageEntry"]
    usage = {"input_tokens": 2000, "output_tokens": 1000,
             "cache_creation_input_tokens": 400, "cache_read_input_tokens": 100}
    # One entry WITH a recorded costUSD that != its computed cost, one WITHOUT.
    recorded = UsageEntry(
        timestamp=dt.datetime(2026, 5, 15, 18, 0, 0, tzinfo=dt.timezone.utc),
        model="claude-opus-4-7", usage=usage, cost_usd=42.0,
        source_path="/tmp/synth.jsonl")
    unrecorded = UsageEntry(
        timestamp=dt.datetime(2026, 5, 15, 20, 30, 0, tzinfo=dt.timezone.utc),
        model="claude-opus-4-7", usage=usage, cost_usd=None,
        source_path="/tmp/synth.jsonl")
    all_entries = [recorded, unrecorded]

    Block = ns["Block"]            # noqa: F841 (kept for parity with sibling test)
    _build = ns["_build_activity_block"]
    heuristic_start = dt.datetime(2026, 5, 15, 20, 0, 0, tzinfo=dt.timezone.utc)
    heuristic_end = heuristic_start + dt.timedelta(hours=5)
    swap = ns["_maybe_swap_active_block_to_canonical"]

    def swapped_cost(mode):
        hb = _build([unrecorded], heuristic_start, heuristic_end, now_utc,
                    "auto", anchor="heuristic")
        blocks = [hb]
        swap(blocks, all_entries, now=now_utc, mode=mode)
        return blocks[0].cost_usd

    disp = swapped_cost("display")      # recorded 42.0 + 0 for the unrecorded
    calc = swapped_cost("calculate")    # both computed from pricing, ignore 42.0
    assert abs(disp - 42.0) < 1e-9, disp
    assert abs(calc - disp) > 1e-9, (calc, disp)


# ── Round-5 Bug G: dashboard trend pre-credit row ────────────────────


def test_tui_build_trend_shows_one_row_for_a_credited_week(ns):
    """The dashboard envelope's `trend.weeks[]` carries ONE row per week.

    Bug G was the consequence of the split: `get_recent_weeks` produced two
    references sharing `WeekRef.key`, and the trend panel rendered two adjacent
    entries for one subscription week. The split is gone, so the panel shows the
    week once, on its original boundaries, reporting the live counter — and that
    row is the current one.
    """
    end_iso = "2026-05-16T17:00:00+00:00"
    effective_iso = "2026-05-15T17:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = "2026-05-09T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T16:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T20:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=4.0,
        )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective_iso,
            old_week_end_at=effective_iso,
        )
        conn.commit()
    finally:
        conn.close()

    conn = ns["open_db"]()
    try:
        now_utc = dt.datetime(2026, 5, 15, 21, 0, 0, tzinfo=dt.timezone.utc)
        rows = ns["_tui_build_trend"](conn, now_utc, count=8)
    finally:
        conn.close()

    week_dt = ns["parse_iso_datetime"](week_start_at, "test")
    eff_dt = ns["parse_iso_datetime"](effective_iso, "test")
    matching = [r for r in rows if r.week_start_at == week_dt]
    assert len(matching) == 1, [r.week_start_at for r in rows]
    assert not [r for r in rows if r.week_start_at == eff_dt], (
        "a segment boundary reached the trend panel")

    assert matching[0].used_pct == 4.0, matching[0].used_pct
    assert matching[0].is_current is True, matching[0]


def test_tui_build_trend_non_credit_week_keeps_legacy_behavior(ns):
    """Bug G fix must NOT change rendering on un-credited weeks: a
    single ref per key, no ``as_of_utc`` pin (so legacy snapshots
    written outside the API-derived window still resolve), and
    ``is_current`` falls back to key-only matching when no reset event
    exists for the latest snapshot's week.
    """
    week_start_date, week_end_date = _week_start_for(
        "2026-05-16T17:00:00+00:00"
    )

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T20:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at="2026-05-09T17:00:00+00:00",
            week_end_at="2026-05-16T17:00:00+00:00",
            weekly_percent=42.0,
        )
        conn.commit()
    finally:
        conn.close()

    conn = ns["open_db"]()
    try:
        now_utc = dt.datetime(2026, 5, 15, 21, 0, 0, tzinfo=dt.timezone.utc)
        rows = ns["_tui_build_trend"](conn, now_utc, count=8)
    finally:
        conn.close()

    # Single ref for this key; used_pct resolves to the seeded snapshot.
    credited = [r for r in rows if r.used_pct == 42.0]
    assert len(credited) == 1, [r.used_pct for r in rows]
    assert credited[0].is_current is True


# ── Round-5 Bug J: phantom heuristic block from overlapping canonicals ─


def test_load_recorded_five_hour_windows_truncates_credit_overlap(
    ns,
):
    """Bug J regression guard (v1.7.2 round-5).

    In-place credit creates two overlapping canonical 5h windows: the
    pre-credit window (anchored before the credit fired) and the
    post-credit window (anchored at-or-near the credit moment). On
    live data the user saw:

      block A: [15:50, 20:50] UTC (pre-credit 5h, supposed to end at 20:50)
      block B: [17:50, 22:50] UTC (post-credit 5h, ACTIVE)

    Without round-5, ``_select_non_overlapping_recorded_windows`` drops
    one — leaving entries 15:50-17:50Z unanchored. The renderer shows
    them as a phantom "~" heuristic block (cost ~$45) sandwiched
    between two canonical rows, confusing the user.

    The fix truncates block A's R to ``effective_reset_at_utc`` so the
    weighted scheduler keeps BOTH anchors. The override map carries
    block A's real ``block_start_at`` for display.
    """
    canonical_resets_a = "2026-05-15T20:50:00+00:00"
    block_start_a      = "2026-05-15T15:50:00+00:00"
    canonical_resets_b = "2026-05-15T22:50:00+00:00"
    block_start_b      = "2026-05-15T17:50:00+00:00"
    credit_effective   = "2026-05-15T17:58:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_five_hour_block_row(
            conn,
            five_hour_resets_at=canonical_resets_a,
            block_start_at=block_start_a,
            five_hour_window_key=int(
                dt.datetime.fromisoformat(canonical_resets_a).timestamp()
            ),
        )
        _seed_five_hour_block_row(
            conn,
            five_hour_resets_at=canonical_resets_b,
            block_start_at=block_start_b,
            five_hour_window_key=int(
                dt.datetime.fromisoformat(canonical_resets_b).timestamp()
            ),
        )
        # In-place credit event (old == effective shape).
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            ("2026-05-15T18:00:00Z",
             credit_effective,
             "2026-05-16T17:00:00+00:00",
             credit_effective),
        )
        conn.commit()
    finally:
        conn.close()

    range_start = dt.datetime(2026, 5, 15, 0, 0, tzinfo=dt.timezone.utc)
    range_end   = dt.datetime(2026, 5, 16, 0, 0, tzinfo=dt.timezone.utc)
    anchors, overrides, _intervals = ns["_load_recorded_five_hour_windows"](
        range_start, range_end,
    )

    # The earlier block's R MUST be truncated to the credit moment so
    # both anchors survive _select_non_overlapping_recorded_windows.
    credit_floored = dt.datetime(
        2026, 5, 15, 17, 50, 0, tzinfo=dt.timezone.utc,
    )  # 17:58 floors to 17:50
    post_credit_anchor = dt.datetime(
        2026, 5, 15, 22, 50, 0, tzinfo=dt.timezone.utc,
    )
    # The original 20:50 anchor must NOT appear (it was truncated).
    assert dt.datetime(2026, 5, 15, 20, 50, 0, tzinfo=dt.timezone.utc) not in anchors, anchors
    assert post_credit_anchor in anchors, anchors
    assert credit_floored in anchors, anchors

    # And the truncated R must carry an override → real block_start_a.
    expected_start = dt.datetime(
        2026, 5, 15, 15, 50, 0, tzinfo=dt.timezone.utc,
    )
    assert credit_floored in overrides, overrides
    assert overrides[credit_floored] == expected_start, overrides


def test_a_five_hour_credit_moment_is_read_across_every_account(ns):
    """The credit-moment scan is account-BLIND, and that is the decision.

    #703 + #707 0.8b. The justification first recorded for it claimed no caller
    of `_load_recorded_five_hour_windows` holds an account key, which is false —
    the statusline resolves `_statusline_active_account()` and scopes its own 7d
    clamp legs to it in the same render. The decision stands on the other two
    grounds: the canonical-block query beside this one is account-blind, and so
    are the session entries these moments disambiguate, because `blocks` is a
    ccusage drop-in with no account axis. Scoping only the credit leg would make
    the truncation moments disagree with the anchors they truncate.

    Pinned so that scoping it later is a deliberate re-decision rather than a
    silent one, and so the consequence stays visible: another account's credit,
    and a manual one, both truncate a block here.
    """
    canonical_resets_a = "2026-05-15T20:50:00+00:00"
    block_start_a = "2026-05-15T15:50:00+00:00"
    canonical_resets_b = "2026-05-15T22:50:00+00:00"
    block_start_b = "2026-05-15T17:50:00+00:00"
    credit_effective = "2026-05-15T17:58:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_five_hour_block_row(
            conn, five_hour_resets_at=canonical_resets_a,
            block_start_at=block_start_a,
            five_hour_window_key=int(
                dt.datetime.fromisoformat(canonical_resets_a).timestamp()))
        _seed_five_hour_block_row(
            conn, five_hour_resets_at=canonical_resets_b,
            block_start_at=block_start_b,
            five_hour_window_key=int(
                dt.datetime.fromisoformat(canonical_resets_b).timestamp()))
        # A MANUAL credit (both boundary columns NULL) belonging to ANOTHER
        # account. Neither fact excludes it from the scan.
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, account_key, week_start_date, "
            " observed_at_utc, observed_post_credit_pct, credit_key) "
            "VALUES (?,NULL,NULL,?,?,?,?,?,?)",
            ("2026-05-15T18:00:00Z", credit_effective, "acct-other",
             "2026-05-11", credit_effective, 2.0, "o:other-account"))
        conn.commit()
    finally:
        conn.close()

    anchors, overrides, _intervals = ns["_load_recorded_five_hour_windows"](
        dt.datetime(2026, 5, 15, 0, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 5, 16, 0, 0, tzinfo=dt.timezone.utc))
    credit_floored = dt.datetime(
        2026, 5, 15, 17, 50, 0, tzinfo=dt.timezone.utc)
    assert credit_floored in anchors, anchors
    assert overrides.get(credit_floored) == dt.datetime(
        2026, 5, 15, 15, 50, 0, tzinfo=dt.timezone.utc), overrides


def test_group_entries_into_blocks_uses_block_start_override(ns):
    """Bug J regression guard: the override threads through
    `_group_entries_into_blocks` so the recorded block's displayed
    ``start_time`` matches Anthropic's real block_start_at — not the
    R-5h default which is hours earlier for a credit-truncated block.
    """
    UsageEntry = ns["UsageEntry"]
    group = ns["_group_entries_into_blocks"]

    # Truncated R = 17:50 UTC (10-min floor of 17:58). Real start = 15:50 UTC.
    truncated_R = dt.datetime(2026, 5, 15, 17, 50, 0, tzinfo=dt.timezone.utc)
    real_start = dt.datetime(2026, 5, 15, 15, 50, 0, tzinfo=dt.timezone.utc)
    entry = UsageEntry(
        timestamp=dt.datetime(2026, 5, 15, 16, 30, 0, tzinfo=dt.timezone.utc),
        model="claude-opus-4-7",
        usage={"input_tokens": 100, "output_tokens": 50,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        cost_usd=None,
        source_path="/tmp/synth.jsonl",
    )
    now = dt.datetime(2026, 5, 15, 21, 0, 0, tzinfo=dt.timezone.utc)
    blocks = group(
        [entry],
        recorded_windows=[truncated_R],
        block_start_overrides={truncated_R: real_start},
        now=now,
    )
    activity = [b for b in blocks if not b.is_gap]
    assert len(activity) == 1
    assert activity[0].start_time == real_start
    assert activity[0].end_time == truncated_R
    assert activity[0].anchor == "recorded"


def test_load_recorded_five_hour_windows_truncates_each_overlap_independently(
    ns,
):
    """Issue #44 regression guard.

    Two in-place credits inside the same pre-credit canonical 5h window
    each spawn a post-credit canonical block. The truncation loop runs
    once per adjacent (i, i+1) canonical pair: pair (A, B) must pick
    credit_1, pair (B, C) must pick credit_2.

    Pre-fix the inner credit loop broke on the FIRST matching credit
    regardless of order. When ``credit_moments`` came back from SQLite
    in reverse-time order (no ``ORDER BY``), pair (A, B) latched onto
    credit_2 — collapsing two distinct truncated anchors onto the same
    10-minute floor and silently dropping one via override-map
    overwrite. The pre-credit entries between credit_1 and credit_2
    then surfaced as a phantom heuristic ``~`` row.
    """
    canonical_resets_a = "2026-05-15T05:00:00+00:00"
    block_start_a      = "2026-05-15T00:00:00+00:00"
    canonical_resets_b = "2026-05-15T06:00:00+00:00"
    block_start_b      = "2026-05-15T01:00:00+00:00"
    canonical_resets_c = "2026-05-15T08:00:00+00:00"
    block_start_c      = "2026-05-15T03:00:00+00:00"
    credit_1_effective = "2026-05-15T01:00:00+00:00"
    credit_2_effective = "2026-05-15T03:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_five_hour_block_row(
            conn,
            five_hour_resets_at=canonical_resets_a,
            block_start_at=block_start_a,
            five_hour_window_key=int(
                dt.datetime.fromisoformat(canonical_resets_a).timestamp()
            ),
        )
        _seed_five_hour_block_row(
            conn,
            five_hour_resets_at=canonical_resets_b,
            block_start_at=block_start_b,
            five_hour_window_key=int(
                dt.datetime.fromisoformat(canonical_resets_b).timestamp()
            ),
        )
        _seed_five_hour_block_row(
            conn,
            five_hour_resets_at=canonical_resets_c,
            block_start_at=block_start_c,
            five_hour_window_key=int(
                dt.datetime.fromisoformat(canonical_resets_c).timestamp()
            ),
        )
        # Insert credits in REVERSE-time order. Without an ORDER BY in
        # the credit-moments query, SQLite returns rows in insertion
        # order — exercising the order-dependent bug.
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            ("2026-05-15T03:00:00Z",
             credit_2_effective,
             "2026-05-16T19:00:00+00:00",
             credit_2_effective),
        )
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            ("2026-05-15T01:00:00Z",
             credit_1_effective,
             "2026-05-16T17:00:00+00:00",
             credit_1_effective),
        )
        conn.commit()
    finally:
        conn.close()

    range_start = dt.datetime(2026, 5, 15, 0, 0, tzinfo=dt.timezone.utc)
    range_end   = dt.datetime(2026, 5, 15, 23, 0, tzinfo=dt.timezone.utc)
    anchors, overrides, _intervals = ns["_load_recorded_five_hour_windows"](
        range_start, range_end,
    )

    truncated_a = dt.datetime(2026, 5, 15, 1, 0, 0, tzinfo=dt.timezone.utc)
    truncated_b = dt.datetime(2026, 5, 15, 3, 0, 0, tzinfo=dt.timezone.utc)
    canonical_c = dt.datetime(2026, 5, 15, 8, 0, 0, tzinfo=dt.timezone.utc)

    # Three distinct anchors must survive — one per canonical block.
    # Pre-fix the override map collapsed truncated_a's override onto
    # truncated_b's floor, leaving only two anchors and a phantom gap.
    assert truncated_a in anchors, anchors
    assert truncated_b in anchors, anchors
    assert canonical_c in anchors, anchors

    expected_start_a = dt.datetime(2026, 5, 15, 0, 0, 0, tzinfo=dt.timezone.utc)
    expected_start_b = dt.datetime(2026, 5, 15, 1, 0, 0, tzinfo=dt.timezone.utc)
    assert overrides.get(truncated_a) == expected_start_a, overrides
    assert overrides.get(truncated_b) == expected_start_b, overrides


def test_group_entries_into_blocks_no_phantom_between_two_credits(ns):
    """Issue #44 phantom-row regression guard at the renderer.

    With three truncated anchors threaded through, entries spanning the
    pre-credit window must land in the three canonical blocks — never a
    fourth heuristic ``~`` row covering the gap between credit_1 and
    credit_2.
    """
    UsageEntry = ns["UsageEntry"]
    group = ns["_group_entries_into_blocks"]

    truncated_a = dt.datetime(2026, 5, 15, 1, 0, 0, tzinfo=dt.timezone.utc)
    truncated_b = dt.datetime(2026, 5, 15, 3, 0, 0, tzinfo=dt.timezone.utc)
    canonical_c = dt.datetime(2026, 5, 15, 8, 0, 0, tzinfo=dt.timezone.utc)
    start_a = dt.datetime(2026, 5, 15, 0, 0, 0, tzinfo=dt.timezone.utc)
    start_b = dt.datetime(2026, 5, 15, 1, 0, 0, tzinfo=dt.timezone.utc)

    def _e(when: dt.datetime) -> object:
        return UsageEntry(
            timestamp=when,
            model="claude-opus-4-7",
            usage={"input_tokens": 100, "output_tokens": 50,
                   "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 0},
            cost_usd=None,
            source_path="/tmp/synth.jsonl",
        )

    entries = [
        _e(dt.datetime(2026, 5, 15, 0, 30, 0, tzinfo=dt.timezone.utc)),  # A
        _e(dt.datetime(2026, 5, 15, 2, 0, 0, tzinfo=dt.timezone.utc)),   # B
        _e(dt.datetime(2026, 5, 15, 5, 0, 0, tzinfo=dt.timezone.utc)),   # C
    ]
    now = dt.datetime(2026, 5, 15, 9, 0, 0, tzinfo=dt.timezone.utc)
    blocks = group(
        entries,
        recorded_windows=[truncated_a, truncated_b, canonical_c],
        block_start_overrides={truncated_a: start_a, truncated_b: start_b},
        now=now,
    )

    activity = [b for b in blocks if not b.is_gap]
    # Exactly three canonical blocks, all recorded. No phantom ``~`` row.
    assert len(activity) == 3, [
        (b.start_time, b.end_time, b.anchor) for b in activity
    ]
    assert all(b.anchor == "recorded" for b in activity), [
        (b.start_time, b.end_time, b.anchor) for b in activity
    ]


# ── Round-3: pivots run even when event row already committed ────────


def test_weekly_pivots_run_when_event_row_already_committed(
    ns, monkeypatch, tmp_path
):
    """Round-3 / memory ``project_dedup_must_not_gate_side_effects.md``:
    weekly credit pivots (hwm-7d force-write + stale-replica DELETE)
    MUST run even when the ``already`` pre-check sees the event row
    is in the table because a prior crashed invocation committed the
    INSERT before dying.

    Failure mode: tick N detects the credit, INSERTs the week_reset_events
    row, ``conn.commit()`` lands — and then the process dies (CC
    self-update, OOM, kill -9) before the HWM force-write + DELETE
    could run. On tick N+1 the same predicate fires; the
    ``already`` pre-check finds the row and (pre-fix) the entire
    ``if already is None:`` block skipped — leaving the system wedged
    on the pre-credit HWM and the stale-replica rows.

    Fix: hoist pivots out of the ``if already is None:`` body. The
    INSERT stays gated (no double-write), but the pivots are
    individually idempotent so re-running them is safe.

    #703 + #707 changed what the pre-committed row means here. The row is
    identified by ``credit_key`` now, and the row this fixture pre-commits is
    the LEGACY keyless shape — what an old ``wr:`` journal line folds to. A
    keyless row identifies no credit, so it correctly does not suppress this
    tick's own, and the tick records its credit beside it. What this test
    still proves is its actual subject: the pivots run on the recovery tick.
    The insert-skipped half of the pair moved to
    ``tests/test_multi_credit_identity.py::test_the_pivots_run_when_the_insert_is_skipped``,
    which can name the identity and therefore actually reach the skip.
    """
    observed = _pin_observation_clock(monkeypatch)
    end_iso, end_epoch = _future_week_end_iso(observed)
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        # 1. Pre-credit baseline snapshot (LATEST snapshot when tick
        # N+1 runs — proves the detection predicate re-fires because
        # prior_pct still reads 67%).
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        # 2. Pre-committed event row (simulates the crashed tick N).
        # Use the hour floor of the pinned observation instant, so the
        # ``unixepoch(captured_at_utc) >= unixepoch(effective_iso)`` bound
        # still admits the stale replica staged below.
        precommitted_floor = observed.replace(minute=0, second=0)
        precommitted_iso = precommitted_floor.isoformat(timespec="seconds")
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=precommitted_iso,
            old_week_end_at=precommitted_iso,
        )
        # 3. Stale-replica row at the pre-credit value, captured
        # at-or-after the pre-committed effective_iso (claude-statusline
        # replay landed between the crash and the recovery tick).
        #
        # It is stamped at the recovery tick's own capture instant, which is
        # what the automatic bracket selector spans. Left on the wall clock it
        # was stamped one second earlier than that capture whenever seeding and
        # the tick straddled a second boundary, and the row then fell outside
        # the one-second bracket and survived: measured 1 failure in 120 runs.
        _seed_usage_snapshot(
            conn,
            captured_at_utc=ns["now_utc_iso"](observed),
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    # 4. Stage hwm-7d at the pre-credit value (proves the recovery
    # tick force-writes it back down).
    (tmp_path / ".local" / "share" / "cctally" / "hwm-7d").write_text(
        f"{week_start_date} 67.0\n"
    )

    # 5. Recovery tick. Same percent (2%) as the original credit
    # observation, so the detection predicate re-fires.
    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    # Assertion 1: the pre-committed row is untouched, and this tick's own
    # credit is recorded beside it carrying its own identity. The keyless row
    # names no credit, so suppressing an identified one on its account would
    # discard a real credit.
    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT effective_reset_at_utc, credit_key FROM week_reset_events "
            "ORDER BY id"
        ).fetchall()
        assert events[0]["effective_reset_at_utc"] == precommitted_iso
        assert events[0]["credit_key"] is None
        assert [e["credit_key"] for e in events].count(None) == 1, [
            dict(e) for e in events]
    finally:
        conn.close()

    # Assertion 2: HWM force-written back down (pivot ran despite
    # ``already`` being non-None). File used to be "67.0"; recovery
    # tick must overwrite to "2.0".
    hwm_path = tmp_path / ".local" / "share" / "cctally" / "hwm-7d"
    hwm_parts = hwm_path.read_text().strip().split()
    assert len(hwm_parts) == 2, hwm_parts
    assert hwm_parts[0] == week_start_date
    assert round(float(hwm_parts[1]), 1) == 2.0, (
        f"hwm-7d force-write pivot must run on recovery tick; got "
        f"{hwm_parts[1]} (pre-fix bug: stays at 67.0)"
    )

    # Assertion 3: stale-replica DELETE ran — no rows at the
    # pre-credit value captured at-or-after the pre-committed
    # effective_iso.
    conn = ns["open_db"]()
    try:
        stale_count = conn.execute(
            "SELECT COUNT(*) AS c FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "  AND round(weekly_percent, 1) = 67.0",
            (week_start_date, precommitted_iso),
        ).fetchone()["c"]
        assert stale_count == 0, (
            "stale-replica DELETE pivot must run on recovery tick"
        )
        # Original pre-credit seed (well before effective_iso)
        # survives.
        original = conn.execute(
            "SELECT COUNT(*) AS c FROM weekly_usage_snapshots "
            "WHERE captured_at_utc = '2026-05-14T10:00:00Z'"
        ).fetchone()["c"]
        assert original == 1
    finally:
        conn.close()


# ── Issue #128: reset-to-zero debounce marker helpers ────────────────────


def test_reset_zero_marker_roundtrip_and_failsoft(ns):
    """The marker helpers round-trip a 6-field line and fail soft (return
    None) on every malformed shape, so a garbled marker re-arms cleanly
    instead of wedging the confirm path. APP_DIR is redirected to tmp by
    the `ns` fixture, so the marker lands in the scratch dir.

    #703 + #707 added three trailing fields: the first zero's EXACT capture
    instant, its journal identity, and the ACCOUNT the arm belongs to. The
    confirming tick cannot see the first two, and the hour floor used in their
    place back-dated the credit's epoch before observations that were still
    legitimately pre-credit; without the third, a zero arriving for a different
    account both confirmed off the first account's baseline and overwrote its
    arm. A four- or six-field line is the shape an upgrading install left
    behind and is still accepted, with the missing fields None.

    Each account has its OWN file, so two accounts arming concurrently cannot
    lose each other's arm — no read-modify-write is involved. The shared file is
    the legacy one, read as a fallback and retired the moment a per-account arm
    is written."""
    import _cctally_record as rec

    marker_path = ns["APP_DIR"] / "pending-reset-zero-7d"

    # Round-trip, under one account's own file.
    rec._arm_reset_zero_marker(
        "2026-05-25", "2026-06-08T18:00:00+00:00",
        baseline_pct=14.0, first_zero_iso="2026-06-01T18:00:35+00:00",
        first_zero_capture_iso="2026-06-01T18:00:35+00:00",
        first_zero_identity="sa:o:abc123",
        account_key="acct-a",
    )
    assert (ns["APP_DIR"] / "pending-reset-zero-7d.acct-a").exists()
    assert rec._read_reset_zero_marker("acct-a") == (
        "2026-05-25", "2026-06-08T18:00:00+00:00", 14.0,
        "2026-06-01T18:00:35+00:00",
        "2026-06-01T18:00:35+00:00", "sa:o:abc123", "acct-a",
    )

    # An armed marker with none of the three facts writes the absent sentinel
    # and reads back as None for each, rather than shortening the line.
    rec._arm_reset_zero_marker(
        "2026-05-25", "2026-06-08T18:00:00+00:00",
        baseline_pct=14.0, first_zero_iso="2026-06-01T18:00:35+00:00",
    )
    assert rec._read_reset_zero_marker() == (
        "2026-05-25", "2026-06-08T18:00:00+00:00", 14.0,
        "2026-06-01T18:00:35+00:00", None, None, None,
    )

    # The pre-#703 four-field line an upgrading install left armed.
    marker_path.write_text(
        "2026-05-25 2026-06-08T18:00:00+00:00 14.0 "
        "2026-06-01T18:00:35+00:00\n")
    assert rec._read_reset_zero_marker() == (
        "2026-05-25", "2026-06-08T18:00:00+00:00", 14.0,
        "2026-06-01T18:00:35+00:00", None, None, None,
    )
    # The six-field line the binary between the two changes wrote.
    marker_path.write_text(
        "2026-05-25 2026-06-08T18:00:00+00:00 14.0 "
        "2026-06-01T18:00:35+00:00 2026-06-01T18:00:35+00:00 sa:o:abc123\n")
    assert rec._read_reset_zero_marker() == (
        "2026-05-25", "2026-06-08T18:00:00+00:00", 14.0,
        "2026-06-01T18:00:35+00:00", "2026-06-01T18:00:35+00:00",
        "sa:o:abc123", None,
    )
    # A shared marker is read as a fallback for ANY account, because it records
    # none — and it is retired the moment that account arms its own.
    assert rec._read_reset_zero_marker("acct-b") is not None
    rec._arm_reset_zero_marker(
        "2026-05-25", "2026-06-08T18:00:00+00:00",
        baseline_pct=9.0, first_zero_iso="2026-06-01T18:00:35+00:00",
        account_key="acct-b")
    assert not marker_path.exists()

    # Clear, per account.
    rec._clear_reset_zero_marker("acct-a")
    rec._clear_reset_zero_marker("acct-b")
    assert rec._read_reset_zero_marker("acct-a") is None
    assert rec._read_reset_zero_marker("acct-b") is None
    assert not marker_path.exists()

    # Fail-soft: every malformed shape → None.
    for bad in (
        "",                                              # empty
        "2026-05-25 2026-06-08T18:00:00+00:00 14.0",     # wrong arity (3)
        "2026-05-25 end notafloat 2026-06-01T18:00:35+00:00",  # bad baseline
        "2026-05-25 end 14.0 not-a-timestamp",           # bad first_zero_iso
        "2026-05-25 end 14.0 2026-06-01T18:00:35+00:00 x",     # arity 5
        # bad capture instant in the six-field shape
        "2026-05-25 end 14.0 2026-06-01T18:00:35+00:00 nope sa:o:a",
    ):
        marker_path.write_text(bad + "\n")
        assert rec._read_reset_zero_marker() is None, bad


# ── #269 M4.4: backfill-rescan process guard ───────────────────────────────
def test_backfill_guard_skips_rescan_when_signature_unchanged(ns, monkeypatch):
    """The backfill rescans ALL weekly_usage_snapshots on every open_db(). Guard
    it on a process-level memo of (MAX(weekly_usage_snapshots.id),
    (COUNT, MAX(rowid)) over week_reset_events), scoped per DB file identity: a
    second call with NO change SKIPS the rescan. A RUN computes the signature
    twice (check + post-backfill store); a SKIP computes it once (check only).
    """
    import _cctally_weekrefs as wr

    wr._reset_backfill_reset_events_memo()
    conn = ns["open_db"]()  # first open runs the backfill (cold)
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T10:00:00Z",
            week_start_date="2026-05-12",
            week_end_at="2026-05-19T00:00:00+00:00",
            weekly_percent=46.0,
        )
        conn.commit()

        calls = {"n": 0}
        real = wr._backfill_reset_events_signature

        def spy(c):
            calls["n"] += 1
            return real(c)

        monkeypatch.setattr(wr, "_backfill_reset_events_signature", spy)

        # A snapshot was just inserted → signature moved → RUN (check + store).
        ns["_backfill_week_reset_events"](conn)
        assert calls["n"] == 2, "post-insert backfill must RUN (2 signature reads)"

        calls["n"] = 0
        # No change since → SKIP the rescan (check only).
        ns["_backfill_week_reset_events"](conn)
        assert calls["n"] == 1, "unchanged backfill must SKIP the rescan"
    finally:
        conn.close()


def test_backfill_guard_regenerates_after_reset_events_deleted(ns):
    """Regenerate contract (Codex-M4 P1): the backfill output is NOT a pure
    function of the snapshots alone. Deleting week_reset_events and re-running
    WITHOUT a snapshot insert must REGENERATE the events — a max_wus_id-only
    guard would wrongly skip, so the guard keys the week_reset_events signature
    too.
    """
    import _cctally_weekrefs as wr

    wr._reset_backfill_reset_events_memo()
    end_iso, _ = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date, week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date, week_end_at=end_iso,
            weekly_percent=2.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)
        n1 = conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0]
        assert n1 >= 1, "backfill must detect the historical in-place credit"

        # Delete the events, then re-run with NO snapshot change.
        conn.execute("DELETE FROM week_reset_events")
        conn.commit()
        ns["_backfill_week_reset_events"](conn)
        n2 = conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0]
        assert n2 == n1, "deleting the events must force a regenerate, not a skip"
    finally:
        conn.close()
