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
    observed_pre_credit_pct: float | None = None,
) -> int:
    """Insert a week_reset_events row and return its id.

    `observed_pre_credit_pct` stores the level the credit was issued from, so a
    seeded row carries the shape a real credit writes and round-trips through
    the `week_reset` evt payload. It arms NOTHING. Nothing in `bin/` reads
    `week_reset_events.observed_pre_credit_pct` as an input to any decision —
    the only two SELECTs of a column by that name read `weekly_credit_floors`,
    a different table, on the manual `record-credit` path (line numbers are
    deliberately omitted: an earlier revision of this docstring cited two that
    a same-commit insertion had already shifted) — and the
    unconditional stale-replica DELETE bands around the
    `observed_pre_credit_pct` ARGUMENT `_fire_in_place_credit` receives: the
    detector's `prior_pct` on the immediate leg, the armed marker's baseline on
    the debounced one. A case that needs that band must drive the fire; seeding
    this column cannot supply it.

    This corrects the second consecutive wrong justification on this helper.
    The first tied it to the withdrawn #750 S3 echo guard, which read the
    column; the guard is gone, and its replacement claim — that the DELETE
    bands around this column — was never true.
    """
    if old_week_end_at is None:
        old_week_end_at = effective
    cur = conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct) "
        "VALUES (?, ?, ?, ?, ?)",
        (detected_at_utc, old_week_end_at, new_week_end_at, effective,
         observed_pre_credit_pct),
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


def _future_week_end_iso() -> tuple[str, int]:
    """Build an ISO + epoch tuple a few days in the future. The
    detection branch requires ``prior_end_dt > now_utc`` and we'd
    rather not freeze ``dt.datetime.now`` — the test owns its own
    "future" by stamping at "now + 3 days, rounded to next hour".
    """
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


def _arm_debounce(ns, *, week_start_date, week_end_at, baseline_pct,
                  first_zero_at_utc, account_key="unattributed",
                  first_zero_observation_id=None):
    """Arm the #750 S3 `weekly_reset_debounce_state` row.

    The pre-#750 filesystem marker is retired: the debounce state is now a
    stats.db row so an ARM rolls back with the cursor and a CONFIRM rolls back
    with the event it fired.
    """
    import _cctally_record as rec
    conn = ns["open_db"]()
    try:
        rec._arm_reset_debounce_state(
            conn, account_key, week_start_date=week_start_date,
            week_end_at=week_end_at, baseline_pct=baseline_pct,
            first_zero_at_utc=first_zero_at_utc,
            first_zero_observation_id=first_zero_observation_id)
        conn.commit()
    finally:
        conn.close()


def _pin_as_of(monkeypatch, offset_seconds):
    """Give a tick a DISTINCT capture instant.

    Two readings recorded inside the same wall-clock second produce one
    observation id, and #750 S3's self-confirmation rule refuses to confirm a
    reset from a byte-identical replay of the observation that armed it.
    """
    stamp = (dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
             + dt.timedelta(seconds=offset_seconds))
    monkeypatch.setenv(
        "CCTALLY_AS_OF", stamp.isoformat().replace("+00:00", "Z"))


def _read_debounce(ns, account_key="unattributed"):
    import _cctally_record as rec
    conn = ns["open_db"]()
    try:
        return rec._read_reset_debounce_state(conn, account_key)
    finally:
        conn.close()


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
    state = _read_debounce(ns)
    assert state is not None
    assert state[0] == week_start_date            # week
    assert state[2] == 14.0                       # baseline


def test_detection_fires_on_reset_to_zero_below_threshold(ns, tmp_path,
                                                         monkeypatch):
    """#128 (rewritten): two consecutive ~0 readings (14→0→0) CONFIRM and fire.
    Drop 14pp < 25pp, so this exercises the debounced reset-to-zero path, not
    the 25pp path. After the second zero: one event row (old==effective,
    new==end, distinct), the post-reset 0 lands, hwm=0, state cleared.

    The two ticks are pinned to DISTINCT capture instants. An observation's
    journal id is a digest over `{t, at, src, provider, payload}`, so two zero
    readings recorded inside the same wall-clock second are one observation,
    and #750 S3's self-confirmation rule then refuses to confirm from it —
    correctly, because a byte-identical replay carries no new evidence. This
    test means to exercise a genuine two-tick confirm, so it separates them.
    """
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
    _pin_as_of(monkeypatch, 0)
    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    conn = ns["open_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 0
    finally:
        conn.close()

    # Tick 2: confirm + fire.
    _pin_as_of(monkeypatch, 60)
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

    assert _read_debounce(ns) is None               # cleared after fire


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
    assert _read_debounce(ns) is None               # cleared on recovery


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
    """#128: the effective_reset_at_utc is the FIRST-zero instant (recorded in
    the debounce state), not the confirmation tick.

    #750 S3 removed the hour floor, so the anchor is now that instant to the
    exact second. `CCTALLY_TEST_PIN_CAPTURE` makes `cmd_record_usage` stamp the
    observation's `captured_at` from the pinned `CCTALLY_AS_OF` instant, which
    is what the anchor follows — the detection clock and the capture stamp are
    deliberately different quantities.
    """
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

    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
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
    # The FIRST-zero instant to the second (18:00:35), not the confirmation
    # instant (19:00:05) and not the hour it falls in.
    assert eff == "2026-06-02T18:00:35+00:00"


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


def test_reset_to_zero_confirm_reruns_pivots_and_clears_the_state(ns, tmp_path):
    """#128 P2a: a confirming zero re-runs the idempotent pivots (hwm=0) and
    clears the debounce state, with an unrelated event row already in the
    table.

    The half-committed shape this once modelled (event committed, debounce
    marker not yet cleared) cannot arise through the write path any more:
    #750 S3 A3 made the debounce state a row mutated inside the same
    transaction as the event.

    The seeded event no longer suppresses anything either. #750 S3 §1.5 made
    deduplication exact-origin only, and a hand-seeded row names no origin, so
    it is governed by the legacy tuple index while the confirm leg writes an
    origin-bearing row governed by the origin index. The confirm is therefore
    ADMITTED as its own event, which is the deliberate direction: a phantom
    event is visible in the week's segmentation, whereas a suppressed genuine
    reset does not self-heal. What this test still pins is the pivots and the
    state clear, both of which run regardless.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-05-14T10:30:00Z",
            week_start_date=week_start_date, week_end_at=end_iso, weekly_percent=14.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 14.0\n")

    _arm_debounce(
        ns, week_start_date=week_start_date, week_end_at=end_iso,
        baseline_pct=14.0, first_zero_at_utc="2026-05-14T10:40:00+00:00",
        first_zero_observation_id="o:aaaaaaaaaaaaaaaa",
    )
    conn = ns["open_db"]()
    try:
        _seed_reset_event(
            conn, new_week_end_at=end_iso,
            effective="2026-05-14T10:00:00+00:00",
            observed_pre_credit_pct=14.0,
        )
    finally:
        conn.close()

    # Confirming zero: pivots re-run and the state clears. The confirm writes
    # its own origin-bearing event beside the seeded origin-null one.
    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    conn = ns["open_db"]()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT effective_reset_at_utc, origin_observation_id "
            "FROM week_reset_events ORDER BY id")]
    finally:
        conn.close()
    assert len(rows) == 2, rows
    assert rows[0]["origin_observation_id"] is None
    assert rows[0]["effective_reset_at_utc"] == "2026-05-14T10:00:00+00:00"
    # The confirm anchors at the armed first-zero instant and carries that
    # observation's id, which is the identity the origin index governs.
    assert rows[1]["origin_observation_id"] == "o:aaaaaaaaaaaaaaaa"
    assert rows[1]["effective_reset_at_utc"] == "2026-05-14T10:40:00+00:00"
    assert (ns["APP_DIR"] / "hwm-7d").read_text().strip().split() == [week_start_date, "0.0"]
    assert _read_debounce(ns) is None


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

    # Armed state carries a DIFFERENT end boundary than the current tick's.
    _arm_debounce(
        ns, week_start_date=week_start_date,
        week_end_at="2025-01-01T00:00:00+00:00", baseline_pct=14.0,
        first_zero_at_utc="2026-05-14T10:30:00+00:00",
    )
    assert ns["cmd_record_usage"](_record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    conn = ns["open_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 0
    finally:
        conn.close()
    # Re-armed against the real end (E2 = this tick's canonical end).
    state = _read_debounce(ns)
    assert state is not None and state[1] == end_iso



def test_debounced_cleanup_removes_a_replay_exactly_one_point_below_baseline(
        ns, tmp_path):
    """The 2026-09-01 incident: a stale replay 1.0pp below the armed baseline.

    The debounced reset-to-zero leg passes the ARMED MARKER's baseline as
    ``observed_pre_credit_pct``, while the rows the cleanup must remove hold
    whatever the status line last wrote. Those are two different quantities.
    On 2026-09-01 they differed by exactly 1.0 — baseline 14.0, stored replay
    13.0 — and ``ABS(weekly_percent - ?) < 1.0`` is FALSE at exactly 1.0, so
    the DELETE matched nothing. The stale row survived, held every 7d surface
    at the pre-credit percentage, and provoked a second, phantom credit.

    The band exists to absorb drift between those two quantities, so excluding
    the drift bound itself makes the tolerance one-sided. This pins the
    boundary case; the strictly-inside case is covered by
    ``test_credit_branch_defensive_cleanup_removes_stale_replays``.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    # Anchor on the hour floor itself. A forward offset from the floor is a
    # time bomb early in the hour (memory: wall-clock seed with forward
    # offset), and the DELETE predicate only needs captured_at >= effective.
    floor_hour = now_utc.replace(minute=0, second=0)
    floor_iso = floor_hour.isoformat()
    stale_iso = floor_iso.replace("+00:00", "Z")

    conn = ns["open_db"]()
    try:
        # Pre-credit reading at the baseline the marker will carry.
        _seed_usage_snapshot(
            conn,
            captured_at_utc=(floor_hour - dt.timedelta(hours=6))
            .isoformat().replace("+00:00", "Z"),
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=14.0,
        )
        # The stale replay: captured at-or-after the effective instant, one
        # full point below the armed baseline. This is the row that must go.
        _seed_usage_snapshot(
            conn,
            captured_at_utc=stale_iso,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=13.0,
        )
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} 14.0\n")

    _arm_debounce(
        ns, week_start_date=week_start_date, week_end_at=end_iso,
        baseline_pct=14.0, first_zero_at_utc=floor_iso,
    )

    assert ns["cmd_record_usage"](
        _record_usage_args(percent=0.0, resets_at=end_epoch)) == 0

    conn = ns["open_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 1, (
            "the debounced leg must confirm and fire the credit")
        survivors = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?)"
            "  AND weekly_percent = 13.0",
            (week_start_date, floor_iso),
        ).fetchall()
        assert survivors == [], (
            "a replay exactly 1.0pp below the armed baseline survived the "
            f"post-credit cleanup: {survivors}")
    finally:
        conn.close()

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


def test_a_replayed_pre_credit_high_is_admitted_as_its_own_event(ns, tmp_path):
    """A re-detected credit against a stale pre-credit level opens its own
    event, end to end through the live write path.

    This test used to assert the opposite. It first pinned a pre-check keyed on
    `new_week_end_at`, which refused ANY second event for a week and so made a
    genuine second in-place credit unreachable (#732); it then pinned the
    evidence-based echo guard that briefly replaced the pre-check. #750 S3 §1.5
    withdrew that guard, because its epoch maximum was computed over a window
    excluding the predecessor reading, so it also discarded the ordinary
    sequence of a week climbing back to the level it was credited from and
    being credited again.

    Deduplication is now exact-origin only, and this detection carries a new
    observation identity, so it is admitted. The residual is stated rather than
    guarded against: this seed is indistinguishable from a genuine second climb
    and credit. `tests/test_exact_origin_credit_dedup.py` owns the unit-level
    statement of both halves.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        # The existing epoch's own post-credit reading.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T17:10:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=2.0,
        )
        # The replayed pre-credit level, which is also the detector's
        # `prior_pct`.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T17:40:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective="2026-05-15T17:00:00+00:00",
            observed_pre_credit_pct=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        origins = [r[0] for r in conn.execute(
            "SELECT origin_observation_id FROM week_reset_events ORDER BY id")]
    finally:
        conn.close()
    assert len(origins) == 2, origins
    assert origins[0] is None and origins[1] is not None, origins


def test_second_in_place_reset_in_one_week_is_a_distinct_event(ns, tmp_path):
    """#755: Anthropic can reset an account twice inside one subscription
    week. An in-place reset never moves ``week_end_at``, so both resets share
    a ``new_week_end_at``. Keying the pre-check on that column alone let the
    first reset of the week occupy the slot permanently, and the second reset
    inserted nothing: no new cycle boundary existed, so the dashboard kept
    attributing milestones to the superseded cycle.

    What this test proves is exactly that: two events can share one
    ``new_week_end_at``. It proves nothing about how they are told apart. The
    seeded row names no origin and the tick's row does, so the two are governed
    by different partial unique indexes and never compete — the seeded row by
    ``(account_key, old_week_end_at, new_week_end_at)`` and the tick's by
    ``(account_key, origin_observation_id)``. The table-level
    ``UNIQUE(old_week_end_at, new_week_end_at)`` this docstring used to name is
    retired by epoch 1013. Exact-origin deduplication itself is pinned by
    ``tests/test_exact_origin_credit_dedup.py``.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)
    earlier_reset = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    ).replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        # The week's FIRST reset, already recorded, two days ago.
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=earlier_reset,
            old_week_end_at=earlier_reset,
        )
        conn.commit()
    finally:
        conn.close()

    rc = ns["cmd_record_usage"](
        _record_usage_args(percent=2.0, resets_at=end_epoch)
    )
    assert rc == 0

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT old_week_end_at, new_week_end_at FROM week_reset_events "
            "ORDER BY old_week_end_at"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2, (
        "the second in-place reset of the week must be its own event row, "
        f"got {rows!r}"
    )
    assert {r[1] for r in rows} == {end_iso}
    assert rows[0][0] == earlier_reset
    assert rows[1][0] != earlier_reset


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
    Assert: one event row with old == effective (in-place credit shape) and
    effective == the EXACT capture second of the 2% row. #750 S3 removed the
    hour floor: it read as a display convenience, and it back-dated the event
    before observations that still legitimately belonged to the old segment.
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
        # Effective is the EXACT captured_at second of the row where the drop
        # was first observed. Compare as a UTC moment to absorb the host-tz
        # rendering quirk in ``parse_iso_datetime`` — see project
        # gotcha ``unixepoch_for_cross_offset_compare``.
        eff_dt = dt.datetime.fromisoformat(events[0]["effective_reset_at_utc"])
        assert eff_dt.astimezone(dt.timezone.utc) == dt.datetime(
            2026, 5, 14, 17, 30, 0, tzinfo=dt.timezone.utc
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


def test_tui_percent_milestones_take_the_latest_segment_not_the_last_written(
    ns,
):
    """The TUI panel resolves the segment by INSTANT, not by insertion id.

    This builder feeds both the TUI modal and the dashboard's
    ``snap.percent_milestones`` envelope array, and it kept its own `ORDER BY
    id DESC` after the read sites moved to `_latest_reset_event_for_end`. A
    backfill row landing after a live-detected one makes "written last" the
    OLDER reset, so the panel filters milestones on a segment nothing recent
    wrote to and renders the wrong crossings — or none.

    Three cuts, with the live one written neither first nor last, so neither
    an unordered scan nor an `ORDER BY id DESC` reaches it.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    cuts = {
        "early": "2026-05-12T17:00:00+00:00",
        "late": "2026-05-14T17:00:00+00:00",
        "mid": "2026-05-13T17:00:00+00:00",
    }
    cumulative = {"early": 100.0, "late": 5.0, "mid": 77.0}

    conn = ns["open_db"]()
    try:
        ids = {}
        for name in ("early", "late", "mid"):
            ids[name] = _seed_reset_event(
                conn,
                new_week_end_at=end_iso,
                effective=cuts[name],
                old_week_end_at=cuts[name],
            )
        assert min(ids.values()) < ids["late"] < max(ids.values()), ids
        for index, name in enumerate(("early", "late", "mid")):
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cuts[name], week_start_date, week_end_date, week_start_at,
                 end_iso, 1, cumulative[name], None, index + 1, index + 1,
                 ids[name]),
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

        out = ns["_tui_build_percent_milestones"](conn)
    finally:
        conn.close()

    assert [m.cumulative_cost_usd for m in out] == [cumulative["late"]], out


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


def test_credit_branch_defensive_cleanup_removes_stale_replays(
        ns, tmp_path, monkeypatch):
    """Bug A: race-defensive cleanup in the credit-detection branch.

    Failure mode the user hit: between the moment Anthropic credited the
    user (effective_reset_at_utc) and the next cctally record-usage
    invocation, the EXTERNAL claude-statusline tool replayed stale
    pre-credit ``--percent 67`` values (its in-memory HWM cache hadn't
    caught up). Those replays landed at ``captured_at_utc >= effective``
    with ``weekly_percent == 67`` (the pre-credit MAX), then dominated
    the reset-aware clamp's MAX over the post-credit segment so
    legitimate fresh OAuth values were rejected.

    Fix: after the credit branch writes the event row + force-writes
    hwm-7d, run a defensive DELETE pass scoped to the same week, rows
    captured at-or-after ``effective``, with ``weekly_percent`` exactly
    matching the pre-credit value (round-to-1dp equality).
    """
    end_iso, end_epoch = _future_week_end_iso()
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
        # 2. Race condition: the EXTERNAL statusline tool already wrote
        # POST-credit-time rows still carrying the stale 67% value
        # (these are the rows the defensive DELETE must clean up). Use
        # captured_at_utc values that we KNOW will be >= effective_iso
        # — the credit branch computes effective_iso as floor_to_hour
        # of `now`, so anything >= "now floored to hour" works. We use
        # the very-recent-past minute so the timestamps stamp AFTER
        # floor_to_hour(now). Critically, these rows are the MOST
        # RECENT prior snapshots, so the in-place credit detection
        # branch reads weekly_percent=67 as `prior_pct` (latest row by
        # captured_at_utc DESC) — that's exactly what fires the
        # detection (prior=67 vs new=2 = 65pp drop) and what the
        # cleanup uses for its strict-equality predicate.
        # #750 S3: `effective_reset_at_utc` is the credit tick's exact
        # capture second, not the hour it falls in, so the replays have to be
        # placed against that instant rather than against an hour floor.
        # `CCTALLY_TEST_PIN_CAPTURE` makes `cmd_record_usage` stamp the
        # capture from the pinned `CCTALLY_AS_OF`, which is what makes the
        # placement deterministic.
        credit_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
        monkeypatch.setenv(
            "CCTALLY_AS_OF", credit_at.isoformat().replace("+00:00", "Z"))
        # Two stale-replay rows captured AT and just-after the credit instant.
        _seed_usage_snapshot(
            conn,
            captured_at_utc=credit_at.isoformat().replace("+00:00", "Z"),
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc=(credit_at + dt.timedelta(minutes=5))
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

        # The two 67% post-credit-time replay rows MUST be gone.
        stale_post_credit = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "  AND round(weekly_percent, 1) = 67.0",
            (week_start_date, effective_iso),
        ).fetchone()[0]
        assert stale_post_credit == 0, (
            "defensive cleanup should have deleted post-credit-time"
            " stale 67% replays"
        )

        # Pre-credit 67% row (captured BEFORE the credit moment) MUST
        # survive — its captured_at_utc is "2026-05-14T10:00:00Z" which
        # is well before any plausible floor_to_hour(now). The
        # equality predicate is fine; the filter that protects this
        # row is the timestamp half (>= effective_iso).
        pre_credit_67 = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND captured_at_utc < ? "
            "  AND round(weekly_percent, 1) = 67.0",
            (week_start_date, effective_iso),
        ).fetchone()[0]
        assert pre_credit_67 == 1, (
            "pre-credit 67% rows must survive (clamp's reset-aware"
            " filter handles them)"
        )

        # The post-credit 2% seed snapshot MUST have landed — proves
        # the cleanup unblocked the seed. Without cleanup, the
        # reset-aware clamp's MAX would still see the post-credit-time
        # 67% rows (they're at-or-after effective_reset_at_utc and
        # part of the segment's MAX window), and the 2% reading would
        # be rejected as a regression.
        seed_landed = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? AND round(weekly_percent, 1) = 2.0",
            (week_start_date,),
        ).fetchone()[0]
        assert seed_landed == 1, (
            "post-credit seed snapshot must land after cleanup"
        )
    finally:
        conn.close()


def test_credit_branch_cleanup_tolerates_rounding_drift(
        ns, tmp_path, monkeypatch):
    """Issue #45 defensive hardening: replay rows whose ``weekly_percent``
    differs from the stored pre-credit baseline by ≤1pp must still be
    cleaned up.

    Today the EXTERNAL claude-statusline tool replays cctally's
    ``hwm-7d`` value byte-identically (its in-memory HWM equals the
    HWM we just wrote), so strict ``round(.,1)`` equality has worked.
    If Anthropic ever rounds the ``--percent`` payload differently
    from the OAuth API used by record-usage, or if statusline grows
    its own coarser rounding, a replay at ``67.5`` against a stored
    ``prior_pct = 67.4`` would survive strict equality and then
    dominate the reset-aware clamp's MAX over the post-credit segment,
    masking legitimate post-credit values.

    Scenario:
      - pre-credit baseline at 67.4 (long-ago snapshot, protected by
        the cleanup's timestamp filter)
      - stale replay at 67.5 captured after ``effective_iso`` — the
        row we want deleted; 0.1pp away from prior_pct
      - post-credit OAuth-lag read at 67.4 captured even later —
        becomes the latest row, so ``prior_pct = 67.4`` at the SELECT
        site

    Under strict ``round(.,1)`` equality, the 67.5 replay survives;
    the reset-aware clamp's MAX(=67.5) then rejects the legitimate
    post-credit 2% seed. The 1.0pp tolerance band catches the drift
    so both the stale row and the seed land where they should.
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
            weekly_percent=67.4,
        )
        # #750 S3: place the replays against the credit tick's exact capture
        # second, which `CCTALLY_TEST_PIN_CAPTURE` + `CCTALLY_AS_OF` pin.
        credit_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
        monkeypatch.setenv(
            "CCTALLY_AS_OF", credit_at.isoformat().replace("+00:00", "Z"))
        _seed_usage_snapshot(
            conn,
            captured_at_utc=(credit_at + dt.timedelta(minutes=5))
            .isoformat().replace("+00:00", "Z"),
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.5,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc=(credit_at + dt.timedelta(minutes=6))
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

        # The 67.5 drift replay (post-effective) MUST be gone. Under
        # strict round-to-1dp equality it survived (round(67.5,1)=67.5
        # vs round(67.4,1)=67.4); the tolerance band cleans it up.
        stale_drift = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "  AND ABS(weekly_percent - 67.5) < 0.01",
            (week_start_date, effective_iso),
        ).fetchone()[0]
        assert stale_drift == 0, (
            "tolerance-band cleanup must remove replay rows whose "
            "percent differs from prior_pct by ≤1pp (this row at 67.5 "
            "vs prior_pct=67.4 survives strict round-to-1dp equality)"
        )

        # Pre-credit 67.4 baseline (captured before effective) MUST
        # survive — the timestamp filter is the protection.
        pre_credit = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND captured_at_utc < ? "
            "  AND ABS(weekly_percent - 67.4) < 0.01",
            (week_start_date, effective_iso),
        ).fetchone()[0]
        assert pre_credit == 1, (
            "pre-credit 67.4 baseline must survive (timestamp filter)"
        )

        # Post-credit 2% seed MUST land. With the 67.5 replay surviving
        # the cleanup under strict equality, the reset-aware clamp's
        # MAX over the post-credit segment would be 67.5, and 2% would
        # be rejected as a regression. The tolerance-band cleanup
        # removes that row so the seed lands.
        seed_landed = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND ABS(weekly_percent - 2.0) < 0.01",
            (week_start_date,),
        ).fetchone()[0]
        assert seed_landed == 1, (
            "post-credit 2% seed must land — proves the tolerance-band "
            "cleanup unblocked the reset-aware clamp"
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


def test_apply_reset_events_synthesizes_pre_credit_ref(ns):
    """Bug B: a credited week must render as TWO refs after
    ``_apply_reset_events_to_weekrefs`` — a pre-credit segment closed
    at ``effective_reset_at_utc`` AND the existing post-credit segment.
    Detected via the in-place credit row shape
    ``old_week_end_at == effective_reset_at_utc``.
    """
    end_iso = "2026-05-16T17:00:00+00:00"
    effective_iso = "2026-05-15T17:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = "2026-05-09T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Seed an in-place credit event row (old==effective shape).
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective_iso,
            old_week_end_at=effective_iso,
        )
        # Build ONE WeekRef matching the credited week.
        ref = ns["make_week_ref"](
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
        )
        out = ns["_apply_reset_events_to_weekrefs"](conn, [ref])
    finally:
        conn.close()

    # Two refs returned for the credited week.
    assert len(out) == 2, f"expected 2 refs (pre + post), got {len(out)}: {out}"

    # First ref returned is the POST-credit segment (preserves
    # ref-slot ordering in the DESC-sorted output of get_recent_weeks).
    post = out[0]
    pre = out[1]

    assert post.week_start_at == effective_iso
    assert post.week_end_at == end_iso

    assert pre.week_start_at == week_start_at
    assert pre.week_end_at == effective_iso

    # Both refs share the same lookup keys (week_start date + the
    # `key` field) so per-segment milestone readers can still join on
    # ``reset_event_id``.
    assert pre.week_start == post.week_start
    assert pre.key == post.key


def test_apply_reset_events_does_not_split_for_boundary_shift(ns):
    """Regression guard: an event with ``old != effective`` (the
    classic boundary-shift case, where Anthropic moved ``resets_at``
    forward before the natural end) must NOT trigger the new split
    behavior. The pre_map / post_map logic for boundary shifts is
    unchanged.
    """
    # Two distinct weeks for the boundary-shift event.
    pre_end_iso = "2026-05-10T17:00:00+00:00"
    new_end_iso = "2026-05-12T19:00:00+00:00"
    effective_iso = "2026-05-12T19:00:00+00:00"  # different from old
    # The old end is OLDER than effective (classic shift); critically
    # `old != effective` (the marker that distinguishes shifts from
    # in-place credits).
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

    # Exactly 2 refs (one per input). No synthesized split.
    assert len(out) == 2, out

    # Post-reset ref: week_start_at rewritten to effective.
    post_out = next(r for r in out if r.week_end_at == new_end_iso)
    assert post_out.week_start_at == effective_iso

    # Pre-reset ref: week_end_at rewritten to effective.
    pre_out = next(r for r in out if r.week_start_at == pre_week_start_at)
    assert pre_out.week_end_at == effective_iso


def test_trend_table_shows_pre_credit_row(ns, capsys):
    """End-to-end: with an in-place credit event row + seeded
    weekly_usage_snapshots, ``cmd_report`` (JSON mode) must emit TWO
    trend rows for the credited week — pre-credit (closed at
    ``effective``) and post-credit (opened at ``effective``).

    Verifies the per-segment cost paths in ``cmd_report`` don't crash
    on a duplicated lookup key (both refs share ``week_start_date``).
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
    # 2 refs for the credited week (pre + post segment).
    credited = [
        r for r in trend if r["weekStartDate"] == week_start_date
    ]
    assert len(credited) == 2, f"expected 2 trend rows, got: {credited}"

    # Identify pre vs post by week_end_at.
    pre = next(r for r in credited if r["weekEndAt"] == effective_iso)
    post = next(r for r in credited if r["weekEndAt"] == end_iso)

    # Pre-credit row carries the pre-credit usage value (67%).
    assert pre["weeklyPercent"] == 67.0, pre
    # Post-credit row carries the post-credit usage value (4%).
    assert post["weeklyPercent"] == 4.0, post


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


def test_cmd_report_current_row_picks_post_credit_for_credited_week(ns, capsys):
    """Bug D (v1.7.2 round-4): on the user's live DB, ``cmd_report``'s
    "current week" summary box rendered the PRE-credit row (67%, the
    closed segment) instead of the POST-credit row (4%, the live
    segment). Root cause: both refs share ``WeekRef.key``, the match
    predicate ``week_ref.key == current_ref.key`` matched both, and
    last-write-wins picked the wrong row.

    Fix: route ``current_ref`` through ``_apply_reset_events_to_weekrefs``
    so its ``week_start_at`` reflects the post-credit segment, then
    match on BOTH ``key`` AND ``week_start_at``.
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
    # The "current" row must be the POST-credit segment:
    # week_start_at == effective_iso, weeklyPercent == 4.0.
    assert current["weekStartAt"] == effective_iso, current
    assert current["weekEndAt"] == end_iso, current
    assert current["weeklyPercent"] == 4.0, current
    # The currentWeek envelope mirrors the same post-credit anchor.
    assert payload["currentWeek"]["weekStartAt"] == effective_iso


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

    ns["_maybe_swap_active_block_to_canonical"](
        blocks, [], now=now_utc, competing_windows=[])

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

    ns["_maybe_swap_active_block_to_canonical"](
        blocks, [], now=now_utc, competing_windows=[])

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

    ns["_maybe_swap_active_block_to_canonical"](
        blocks, [], now=now_utc, competing_windows=[])

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

    ns["_maybe_swap_active_block_to_canonical"](
        blocks, all_entries, now=now_utc, competing_windows=[])

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
        swap(blocks, all_entries, now=now_utc, mode=mode,
             competing_windows=[])
        return blocks[0].cost_usd

    disp = swapped_cost("display")      # recorded 42.0 + 0 for the unrecorded
    calc = swapped_cost("calculate")    # both computed from pricing, ignore 42.0
    assert abs(disp - 42.0) < 1e-9, disp
    assert abs(calc - disp) > 1e-9, (calc, disp)


# ── Round-5 Bug G: dashboard trend pre-credit row ────────────────────


def test_tui_build_trend_credited_week_shows_pre_credit_segment(ns):
    """Bug G regression guard (v1.7.2 round-5).

    The dashboard envelope's ``trend.weeks[]`` is built from
    ``_tui_build_trend`` (in ``bin/_cctally_tui.py``). ``get_recent_weeks``
    already routes refs through ``_apply_reset_events_to_weekrefs`` which
    splits a credited week into TWO refs (pre + post) sharing
    ``WeekRef.key``. Without the round-5 fix, ``get_latest_usage_for_week``
    returned the SAME post-credit snapshot for both refs (key-only join),
    collapsing both rendered rows to 4.0% — the user saw two adjacent
    "May 09 4%" and "May 15 4%" entries on the trend panel.

    This test seeds a credit event + two snapshots (one pre, one post),
    calls ``_tui_build_trend``, and asserts the credited week yields TWO
    rows whose ``used_pct`` correctly reflect the per-segment values
    (67% pre-credit, 4% post-credit). It also asserts that only the
    post-credit row is flagged ``is_current`` — the pre-credit segment
    is historical even though it shares the bucket key with the current
    one.
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

    # Trend is oldest-first. Find the credited week's two segments by
    # their week_start_at instant on the TuiTrendRow.
    pre_dt = ns["parse_iso_datetime"](week_start_at, "test")
    eff_dt = ns["parse_iso_datetime"](effective_iso, "test")
    pre_row = next((r for r in rows if r.week_start_at == pre_dt), None)
    post_row = next((r for r in rows if r.week_start_at == eff_dt), None)
    assert pre_row is not None, [r.week_start_at for r in rows]
    assert post_row is not None, [r.week_start_at for r in rows]

    # Per-segment used_pct lookups via as_of_utc=week_end_at must
    # resolve to the right snapshot.
    assert pre_row.used_pct == 67.0, pre_row.used_pct
    assert post_row.used_pct == 4.0, post_row.used_pct

    # is_current discriminates by week_start_at — post-credit only.
    assert post_row.is_current is True, post_row
    assert pre_row.is_current is False, pre_row


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


def test_weekly_pivots_run_beside_an_earlier_committed_event(
        ns, tmp_path, monkeypatch):
    """Round-3 / memory ``project_dedup_must_not_gate_side_effects.md``:
    weekly credit pivots (hwm-7d force-write + stale-replica DELETE) MUST run
    on a recovery tick, with a prior invocation's event row already in the
    table.

    Failure mode: tick N detects the credit, INSERTs the week_reset_events
    row, ``conn.commit()`` lands — and then the process dies (CC
    self-update, OOM, kill -9) before the HWM force-write + DELETE
    could run. On tick N+1 the same predicate fires; the dedup decision
    finds the credit already recorded and (pre-fix) the entire insert
    block skipped — leaving the system wedged on the pre-credit HWM and
    the stale-replica rows.

    Fix: hoist the pivots out of the insert branch. The INSERT stays gated (no
    double-write), but the pivots are individually idempotent so re-running
    them is safe.

    Two things changed under this test in #750 S3, and both are asserted below
    rather than assumed. Deduplication is now exact-origin only (§1.5), so the
    pre-committed origin-null row does not refuse the recovery tick's
    origin-bearing one and the tick opens its own event. And the effective
    instant is the exact capture second rather than the hour it falls in
    (§1.4), so the stale-replica DELETE is scoped to that instant and no longer
    reaches back into the previous segment. The refused-insert half of the
    original invariant — pivots running when the INSERT really is refused — is
    pinned directly by
    ``tests/test_exact_origin_credit_dedup.py::test_a5_the_pivots_run_on_the_credits_own_terms``.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    # The recovery tick's capture instant is PINNED, the way the sibling
    # reset-origin module pins it. #750 S3 §1.4 scoped the stale-replica DELETE
    # to that exact second, and the previous form of this case seeded the
    # replica two seconds ahead of the wall clock and relied on the recording
    # call reaching its own capture stamp inside that window — which a loaded
    # runner under `pytest -n 10` does not guarantee. With the pin, the
    # replica's capture and the tick's effective instant are one value by
    # construction rather than by timing.
    pinned_now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    pinned_iso = pinned_now.isoformat().replace("+00:00", "Z")
    monkeypatch.setenv("CCTALLY_AS_OF", pinned_iso)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")

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
        # 2. Pre-committed event row (simulates the crashed tick N). Its
        # instant is the hour the pinned capture falls in, which after §1.4 is
        # a DIFFERENT instant from the recovery tick's own effective second —
        # that difference is what assertion 1 reads.
        precommitted_floor = pinned_now.replace(minute=0, second=0)
        precommitted_iso = precommitted_floor.isoformat(timespec="seconds")
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=precommitted_iso,
            old_week_end_at=precommitted_iso,
            observed_pre_credit_pct=67.0,
        )
        # 3. Stale-replica row at the pre-credit value (a claude-statusline
        # replay), one second after the PINNED capture instant. That is
        # unambiguously inside the DELETE's `>= effective` band whatever the
        # runner's load, because the tick's effective instant is now a value
        # this test chose rather than a clock it races.
        stale_replica_id = _seed_usage_snapshot(
            conn,
            captured_at_utc=(pinned_now + dt.timedelta(seconds=1))
            .isoformat().replace("+00:00", "Z"),
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

    # 5. Recovery tick. Same percent (2%) as the original credit observation.
    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    # Assertion 1: the pre-committed row is untouched and the recovery tick
    # opens its own event beside it, because the two carry different
    # identities — one origin-null, one naming its observation.
    conn = ns["open_db"]()
    try:
        events = [dict(r) for r in conn.execute(
            "SELECT effective_reset_at_utc, origin_observation_id "
            "FROM week_reset_events ORDER BY id")]
    finally:
        conn.close()
    assert len(events) == 2, events
    assert events[0]["effective_reset_at_utc"] == precommitted_iso
    assert events[0]["origin_observation_id"] is None
    assert events[1]["origin_observation_id"] is not None

    # Assertion 2: HWM force-written back down (the pivot ran). File used to
    # be "67.0"; the recovery tick must overwrite to "2.0".
    hwm_path = tmp_path / ".local" / "share" / "cctally" / "hwm-7d"
    hwm_parts = hwm_path.read_text().strip().split()
    assert len(hwm_parts) == 2, hwm_parts
    assert hwm_parts[0] == week_start_date
    assert round(float(hwm_parts[1]), 1) == 2.0, (
        f"hwm-7d force-write pivot must run on recovery tick; got "
        f"{hwm_parts[1]} (pre-fix bug: stays at 67.0)"
    )

    # Assertion 3: the stale-replica DELETE ran, scoped to the tick's OWN
    # effective instant rather than to the hour that instant falls in. The
    # replay seeded ahead of the clock is inside that range and is removed;
    # the 2026-05-14 pre-credit seed is far outside it and survives, which is
    # what #750 S3 §1.4 changed — under the hour floor a reading captured
    # earlier in the same hour was pulled into the range too.
    #
    # The assertion names the seeded row's OWN id. Counting rows through the
    # DELETE's own predicate cannot detect a miss: a replica that falls outside
    # the DELETE's window is outside the counting window too, so the count is
    # zero whether or not the DELETE worked. The pinned capture is what puts
    # the replica inside the window; it does not make the assertion itself
    # discriminating, which is why the assertion names the id.
    conn = ns["open_db"]()
    try:
        stale_count = conn.execute(
            "SELECT COUNT(*) AS c FROM weekly_usage_snapshots WHERE id = ?",
            (stale_replica_id,),
        ).fetchone()["c"]
        assert stale_count == 0, (
            "stale-replica DELETE pivot must run on recovery tick"
        )
        # Original pre-credit seed (well before effective_iso) survives.
        original = conn.execute(
            "SELECT COUNT(*) AS c FROM weekly_usage_snapshots "
            "WHERE captured_at_utc = '2026-05-14T10:00:00Z'"
        ).fetchone()["c"]
        assert original == 1
    finally:
        conn.close()


# ── Issue #128 / #750 S3: reset-to-zero debounce state helpers ────────────


def test_reset_debounce_state_roundtrip_and_failsoft(ns):
    """The state helpers round-trip a row and fail soft (return None) when the
    table cannot be read, so a store that predates epoch 1013 re-arms cleanly
    instead of crashing the recording tick over its own bookkeeping.

    The pre-#750 form of this test round-tripped a four-field line in
    `APP_DIR/pending-reset-zero-7d` and checked four malformed spellings. The
    file is retired: there is no text to garble, so the surviving fail-soft
    axis is the table's own absence.
    """
    import _cctally_record as rec

    conn = ns["open_db"]()
    try:
        rec._arm_reset_debounce_state(
            conn, "unattributed", week_start_date="2026-05-25",
            week_end_at="2026-06-08T18:00:00+00:00", baseline_pct=14.0,
            first_zero_at_utc="2026-06-01T18:00:35+00:00",
            first_zero_observation_id="o:" + "c" * 16)
        conn.commit()
        assert rec._read_reset_debounce_state(conn, "unattributed") == (
            "2026-05-25", "2026-06-08T18:00:00+00:00", 14.0,
            "2026-06-01T18:00:35+00:00", "o:" + "c" * 16,
        )

        rec._clear_reset_debounce_state(conn, "unattributed")
        conn.commit()
        assert rec._read_reset_debounce_state(conn, "unattributed") is None

        # Fail-soft: an index with no such table reports "not armed".
        conn.execute("DROP TABLE weekly_reset_debounce_state")
        assert rec._read_reset_debounce_state(conn, "unattributed") is None
    finally:
        conn.rollback()
        conn.close()

    # The retired marker file is never read, so one left by an older binary
    # cannot arm anything.
    assert not (ns["APP_DIR"] / "pending-reset-zero-7d").exists()


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
