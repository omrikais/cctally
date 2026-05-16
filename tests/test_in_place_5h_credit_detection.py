"""In-place 5h credit detection — record-usage tests.

Mirrors ``tests/test_in_place_credit_detection.py`` (weekly v1.7.2) for the
5h dimension. Covers spec §2/§3/§4:

* **Detection predicate** (§2.2): ≥5.0pp drop on the same
  ``five_hour_window_key`` with the block still live emits a
  ``five_hour_reset_events`` row at the 10-min-floored ``effective_iso``.

* **Threshold guard** (§2.2): drops < 5.0pp do not fire detection.

* **Future-end-at guard** (§2.2): drops on already-expired blocks (natural
  rollover) do not fire detection.

* **Post_percent-aware dedup pre-check** (§2.2, Codex r1 finding 4):
  re-observation of the same credit on a subsequent tick does not write
  a second event row. The pre-check compares against the most-recent
  stored event's ``post_percent``; the UNIQUE constraint on
  ``(five_hour_window_key, effective_reset_at_utc)`` backstops it.

* **Stacked credits across distinct 10-min slots** (§2.3): two genuine
  credits within the same block but in different 10-min slots chain as
  distinct rows.

* **Same-10-min-slot collision** (§2.3, Codex r3): two credits inside the
  same 10-min slot collide on UNIQUE; the second is silently absorbed
  by INSERT OR IGNORE.

* **HWM force-write pivot** (§4.2): a credit drops the ``hwm-5h`` file
  below its prior value, bypassing the normal monotonic guard.

* **Reset-aware DB clamp** (§4.1): post-credit fresh OAuth values land
  instead of being held back by stale pre-credit history once an event
  row exists.

* **Stale-replica DELETE** (§4.3): claude-statusline replays of the
  pre-credit value past the credit moment are removed.

Conventions:
* Each test uses ``tmp_path`` via ``redirect_paths`` so ``hwm-5h`` and the
  SQLite DBs land in an isolated scratch HOME.
* ``argparse.Namespace`` is constructed directly to drive
  ``cmd_record_usage`` without a shell. Tests use real wall-clock time
  to compare against ``now_utc`` inside the detection branch; future /
  past ISO strings are built relative to ``dt.datetime.now(dt.timezone.utc)``
  rather than monkeypatching ``dt.datetime`` itself (same pattern as
  ``test_in_place_credit_detection.py``).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# ── helpers ────────────────────────────────────────────────────────────


def _record_usage_args(
    *,
    percent: float,
    resets_at: int,
    five_hour_percent: float | None = None,
    five_hour_resets_at: str | None = None,
) -> argparse.Namespace:
    """Build a minimal Namespace matching cmd_record_usage's signature.

    ``five_hour_resets_at`` is the raw CLI string — int-as-str epoch
    seconds (the argparse default ``type`` is no-coerce so the body's
    ``int(args.five_hour_resets_at)`` handles the cast).
    """
    return argparse.Namespace(
        percent=percent,
        resets_at=resets_at,
        five_hour_percent=five_hour_percent,
        five_hour_resets_at=five_hour_resets_at,
    )


def _seed_5h_snapshot(
    conn,
    *,
    captured_at_utc: str,
    weekly_percent: float,
    five_hour_percent: float,
    five_hour_window_key: int,
    five_hour_resets_at_iso: str,
    week_end_at: str,
    week_start_at: str | None = None,
    week_start_date: str | None = None,
    week_end_date: str | None = None,
) -> int:
    """Insert a weekly_usage_snapshots row with both 7d AND 5h fields."""
    if week_start_date is None:
        end = dt.datetime.fromisoformat(week_end_at.replace("Z", "+00:00"))
        start = end - dt.timedelta(days=7)
        week_start_date = start.date().isoformat()
    if week_end_date is None:
        week_end_date = week_end_at[:10]
    if week_start_at is None:
        week_start_at = week_start_date + "T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, "
        " week_start_at, week_end_at, weekly_percent, "
        " five_hour_percent, five_hour_resets_at, five_hour_window_key, "
        " source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (captured_at_utc, week_start_date, week_end_date,
         week_start_at, week_end_at, weekly_percent,
         five_hour_percent, five_hour_resets_at_iso, five_hour_window_key,
         "test", "{}"),
    )
    return int(cur.lastrowid)


def _future_5h_block_window():
    """Return (resets_iso, resets_epoch_str, window_key) for a 5h block
    whose resets_at is a few hours in the future. The detection branch
    requires ``prior_5h_resets_dt > now_utc`` so the block must still be
    live at test-run wall time.
    """
    now = dt.datetime.now(dt.timezone.utc)
    # Pick a resets_at ~3h in the future, hour-floored.
    future = (now + dt.timedelta(hours=3)).replace(
        minute=0, second=0, microsecond=0
    )
    resets_iso = future.isoformat(timespec="seconds")
    resets_epoch = int(future.timestamp())
    return resets_iso, str(resets_epoch), resets_epoch


def _future_week_end():
    """Return (iso, epoch_int) for a week_end_at a few days in the future."""
    now = dt.datetime.now(dt.timezone.utc)
    future = (now + dt.timedelta(days=3)).replace(
        minute=0, second=0, microsecond=0
    )
    return future.isoformat(timespec="seconds"), int(future.timestamp())


def _past_5h_block_window():
    """Return (resets_iso, resets_epoch_str, window_key) for a 5h block
    whose resets_at is in the PAST — naturally expired, not a credit
    scenario.
    """
    now = dt.datetime.now(dt.timezone.utc)
    past = (now - dt.timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )
    resets_iso = past.isoformat(timespec="seconds")
    resets_epoch = int(past.timestamp())
    return resets_iso, str(resets_epoch), resets_epoch


# ── detection tests ────────────────────────────────────────────────────


def test_detection_fires_on_5pp_threshold(ns, tmp_path):
    """prior=28%, cur=8% (drop 20pp ≥ 5.0pp threshold) with the SAME
    five_hour_window_key as the new fetch and the block still live:
    writes one event row + force-writes hwm-5h.
    """
    end_iso, end_epoch = _future_week_end()
    resets_iso, resets_epoch_str, resets_epoch = _future_5h_block_window()
    # The window_key matches the canonical_5h floor of the epoch.
    window_key = ns["_canonical_5h_window_key"](resets_epoch)

    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=28.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    # Pre-seed hwm-5h so we can verify the force-write decreased it.
    hwm_path = ns["APP_DIR"] / "hwm-5h"
    hwm_path.write_text(f"{window_key} 28.0\n")

    args = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=8.0,
        five_hour_resets_at=resets_epoch_str,
    )
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT five_hour_window_key, prior_percent, post_percent, "
            "       effective_reset_at_utc "
            "  FROM five_hour_reset_events ORDER BY id"
        ).fetchall()
        assert len(events) == 1, [dict(e) for e in events]
        e = dict(events[0])
        assert e["five_hour_window_key"] == window_key
        assert round(e["prior_percent"], 1) == 28.0
        assert round(e["post_percent"], 1) == 8.0
        # effective_reset_at_utc is the 10-min floor of now_utc — it
        # carries a +00:00 offset because parse_iso_datetime returns a
        # tz-aware moment and the floor preserves it.
        eff_dt = dt.datetime.fromisoformat(e["effective_reset_at_utc"])
        assert eff_dt.tzinfo is not None
        # And it's no later than "now" (no clock skew).
        assert eff_dt <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2)
    finally:
        conn.close()

    # hwm-5h force-written to the post-credit value (lower than seed).
    parts = hwm_path.read_text().strip().split()
    assert parts == [str(window_key), "8.0"], parts


def test_detection_skipped_below_threshold(ns, tmp_path):
    """prior=12%, cur=8% (drop 4pp < 5.0pp): no event row, hwm unchanged."""
    end_iso, end_epoch = _future_week_end()
    resets_iso, resets_epoch_str, resets_epoch = _future_5h_block_window()
    window_key = ns["_canonical_5h_window_key"](resets_epoch)

    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=12.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    hwm_path = ns["APP_DIR"] / "hwm-5h"
    hwm_path.write_text(f"{window_key} 12.0\n")

    args = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=8.0,  # 12 - 8 = 4 < 5.0
        five_hour_resets_at=resets_epoch_str,
    )
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM five_hour_reset_events"
        ).fetchone()["c"]
        assert cnt == 0, "below-threshold drop must NOT write an event"
    finally:
        conn.close()
    # hwm-5h unchanged (legacy monotonic clamp blocks the lower value).
    parts = hwm_path.read_text().strip().split()
    assert parts == [str(window_key), "12.0"], parts


def test_detection_skipped_when_window_expired(ns, tmp_path):
    """prior=28, cur=8 BUT prior_5h_resets_dt <= now_utc: no event row.
    This is natural rollover, not a credit.
    """
    end_iso, end_epoch = _future_week_end()
    resets_iso, resets_epoch_str, resets_epoch = _past_5h_block_window()
    window_key = ns["_canonical_5h_window_key"](resets_epoch)

    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=28.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=8.0,
        five_hour_resets_at=resets_epoch_str,
    )
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM five_hour_reset_events"
        ).fetchone()["c"]
        assert cnt == 0, "expired-window drop is natural rollover, not credit"
    finally:
        conn.close()


def test_detection_skipped_on_different_window(ns, tmp_path):
    """prior=28% in window A, cur=8% in window B: distinct window keys,
    no in-place credit (natural rollover into a fresh block).
    """
    end_iso, end_epoch = _future_week_end()
    # Window A is in the past (the prior block, now naturally expired);
    # window B is in the future (the new live block).
    resets_iso_a, _, resets_epoch_a = _past_5h_block_window()
    resets_iso_b, resets_epoch_b_str, resets_epoch_b = _future_5h_block_window()
    window_key_a = ns["_canonical_5h_window_key"](resets_epoch_a)
    window_key_b = ns["_canonical_5h_window_key"](resets_epoch_b)
    assert window_key_a != window_key_b

    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=28.0,
            five_hour_window_key=window_key_a,
            five_hour_resets_at_iso=resets_iso_a,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=8.0,
        five_hour_resets_at=resets_epoch_b_str,
    )
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM five_hour_reset_events"
        ).fetchone()["c"]
        assert cnt == 0, "different-window drop is not an in-place credit"
    finally:
        conn.close()


def test_post_percent_aware_dedup(ns, tmp_path):
    """Pre-check (Codex r1 finding 4): same credit re-observed on a
    subsequent tick where the latest stored snapshot is the post-credit
    value does NOT produce a second event row — the predicate evaluates
    False (prior_5h_pct - cur_pct ≈ 0).

    A subsequent tick where a STALE replica re-inserts the pre-credit
    value would normally re-trigger the predicate, but the pre-check
    against ``post_percent`` of the latest event absorbs that
    (when the replay's prior_5h_pct equals the stored post_percent) —
    AND the UNIQUE on (window_key, effective_iso) backstops the same
    10-min slot collision.
    """
    end_iso, end_epoch = _future_week_end()
    resets_iso, resets_epoch_str, resets_epoch = _future_5h_block_window()
    window_key = ns["_canonical_5h_window_key"](resets_epoch)

    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=28.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    # First tick: detection fires, event row written, snapshot lands at 8%.
    args = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=8.0,
        five_hour_resets_at=resets_epoch_str,
    )
    assert ns["cmd_record_usage"](args) == 0

    # Second tick: same 8%. Now the latest snapshot is 8 → predicate
    # (28 - 8 = 20) wouldn't even evaluate because prior_5h_pct is now 8.
    args2 = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=8.0,
        five_hour_resets_at=resets_epoch_str,
    )
    assert ns["cmd_record_usage"](args2) == 0

    conn = ns["open_db"]()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM five_hour_reset_events"
        ).fetchone()["c"]
        assert cnt == 1, "re-observation must absorb to one event"
    finally:
        conn.close()


def test_stacked_credits_across_distinct_10min_slots(ns, tmp_path, monkeypatch):
    """Two credits across distinct 10-min slots chain as distinct rows.

    Spec §2.3 (Codex r1/r3): supported up to ~30 slots per block. We
    drive two consecutive credit observations and assert two event
    rows land with distinct ``effective_reset_at_utc`` values.

    To get two distinct 10-min slots without freezing the clock, we
    monkeypatch ``c._floor_to_ten_minutes`` in the second call so it
    returns a different floor. This isolates the test from wall-clock
    flakiness while still exercising the row-chaining path.
    """
    end_iso, end_epoch = _future_week_end()
    resets_iso, resets_epoch_str, resets_epoch = _future_5h_block_window()
    window_key = ns["_canonical_5h_window_key"](resets_epoch)

    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=28.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    # First credit: 28% → 8% (Δ20pp).
    args1 = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=8.0,
        five_hour_resets_at=resets_epoch_str,
    )
    assert ns["cmd_record_usage"](args1) == 0

    # Stage user climbing back to 22% (must vary 5h_percent or the dedup
    # vs-last-snapshot path swallows the insert). The detection branch
    # picks the LATEST snapshot (ORDER BY captured_at_utc DESC, id DESC)
    # as the ``prior_5h_row``, so the staged row's captured_at_utc must
    # be later than the previous record-usage tick's captured_at — use
    # ``now_utc_iso()`` plus an id-tiebreaker (the staged row's id is
    # higher than the previous tick's id).
    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc=ns["now_utc_iso"](),
            weekly_percent=42.0,
            five_hour_percent=22.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    # Force the second credit's 10-min floor to a DIFFERENT slot than the
    # first by adding +40min to whatever the helper would have returned.
    real_floor = ns["_floor_to_ten_minutes"]

    def shifted_floor(d):
        return real_floor(d) + dt.timedelta(minutes=40)

    monkeypatch.setitem(ns, "_floor_to_ten_minutes", shifted_floor)

    # Second credit: 22% → 2% (Δ20pp).
    args2 = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=2.0,
        five_hour_resets_at=resets_epoch_str,
    )
    assert ns["cmd_record_usage"](args2) == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT effective_reset_at_utc, prior_percent, post_percent "
            "  FROM five_hour_reset_events ORDER BY id"
        ).fetchall()
        assert len(events) == 2, (
            f"two distinct credits must chain; got {[dict(e) for e in events]}"
        )
        ev0, ev1 = [dict(e) for e in events]
        assert round(ev0["prior_percent"], 1) == 28.0
        assert round(ev0["post_percent"], 1) == 8.0
        assert round(ev1["prior_percent"], 1) == 22.0
        assert round(ev1["post_percent"], 1) == 2.0
        # The two slots are distinct.
        assert ev0["effective_reset_at_utc"] != ev1["effective_reset_at_utc"]
    finally:
        conn.close()


def test_same_slot_collision_absorbed_by_unique(ns, tmp_path, monkeypatch):
    """Two credits within the SAME 10-min slot collide on UNIQUE → second
    INSERT OR IGNORE'd. Spec §2.3 documented cap (Codex r3).

    We pin ``_floor_to_ten_minutes`` to a constant so both observations
    deterministically share an ``effective_reset_at_utc`` and collide on
    the UNIQUE(window_key, effective_iso) index — otherwise wall-clock
    drift across the two calls could land them in adjacent slots and
    produce two event rows (~10% flake odds; mirrors the ``shifted_floor``
    pattern from ``test_stacked_credits_across_distinct_10min_slots``,
    but with a fixed return value to force same-slot collision).

    The seed-update between the two calls is what allows the predicate
    to fire a "second" time at all (otherwise dedup-vs-last-snapshot
    would block it earlier).
    """
    end_iso, end_epoch = _future_week_end()
    resets_iso, resets_epoch_str, resets_epoch = _future_5h_block_window()
    window_key = ns["_canonical_5h_window_key"](resets_epoch)

    # Pin the 10-min floor to a fixed instant so both record-usage calls
    # produce identical ``effective_reset_at_utc`` values and the second
    # INSERT OR IGNORE collides on UNIQUE deterministically.
    fixed_floor_dt = dt.datetime(2026, 5, 1, 12, 30, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setitem(ns, "_floor_to_ten_minutes", lambda _d: fixed_floor_dt)

    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=28.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    args1 = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=8.0,
        five_hour_resets_at=resets_epoch_str,
    )
    assert ns["cmd_record_usage"](args1) == 0

    # User climbs back to 14% in the same physical 10-min slot. Stage a
    # captured_at of "now" so the detection branch picks this row (not
    # the prior tick's 8% row) as ``prior_5h_row``.
    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc=ns["now_utc_iso"](),
            weekly_percent=42.0,
            five_hour_percent=14.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    args2 = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=1.0,  # 14 → 1 = Δ13pp ≥ 5pp threshold
        five_hour_resets_at=resets_epoch_str,
    )
    assert ns["cmd_record_usage"](args2) == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT effective_reset_at_utc, prior_percent, post_percent "
            "  FROM five_hour_reset_events ORDER BY id"
        ).fetchall()
        # Both observations fall in the same wall-clock 10-min slot,
        # so the UNIQUE on (window_key, effective_iso) absorbs the
        # second INSERT OR IGNORE. First observation's prior/post win.
        assert len(events) == 1, (
            f"same-slot collision must absorb; got {[dict(e) for e in events]}"
        )
        e = dict(events[0])
        assert round(e["prior_percent"], 1) == 28.0
        assert round(e["post_percent"], 1) == 8.0
    finally:
        conn.close()


# ── pivot tests ────────────────────────────────────────────────────────


def test_clamp_pivot_post_credit_value_not_re_clamped(ns, tmp_path):
    """Reset-aware DB clamp (§4.1): after the event row lands, the MAX
    query filters samples to those captured at-or-after the credit's
    effective_iso. A subsequent OAuth fetch at 4% must NOT be re-clamped
    up to the pre-credit max (28%).
    """
    end_iso, end_epoch = _future_week_end()
    resets_iso, resets_epoch_str, resets_epoch = _future_5h_block_window()
    window_key = ns["_canonical_5h_window_key"](resets_epoch)

    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=28.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    # First, the credit tick (28 → 8).
    args1 = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=8.0,
        five_hour_resets_at=resets_epoch_str,
    )
    assert ns["cmd_record_usage"](args1) == 0

    # Next fresh OAuth fetch at 4% (the user's activity dropped further or
    # an additional credit landed). Without reset-aware clamp, this would
    # be re-clamped UP to 28% via MAX(weekly_usage_snapshots.five_hour_percent).
    # With the new clamp + event row, MAX is filtered to >= effective_iso so
    # only the 8% post-credit value contributes; 4 < 8 still clamps to 8.
    # The key assertion is "not re-clamped to 28".
    args2 = _record_usage_args(
        percent=43.0,  # vary weekly_percent so dedup-vs-last doesn't swallow
        resets_at=end_epoch,
        five_hour_percent=4.0,
        five_hour_resets_at=resets_epoch_str,
    )
    assert ns["cmd_record_usage"](args2) == 0

    conn = ns["open_db"]()
    try:
        # The most recent snapshot's five_hour_percent must NOT be 28.0.
        latest = conn.execute(
            "SELECT five_hour_percent FROM weekly_usage_snapshots "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert latest is not None
        assert round(latest["five_hour_percent"], 1) != 28.0, (
            "post-credit value must not be re-clamped to pre-credit max"
        )
        # And it must be ≤ 8 (the post-credit max).
        assert round(latest["five_hour_percent"], 1) <= 8.0
    finally:
        conn.close()


def test_stale_replica_delete(ns, tmp_path):
    """Stale-replica DELETE (§4.3): the credit branch DELETEs
    weekly_usage_snapshots rows whose five_hour_percent matches the
    prior_5h_pct and whose captured_at_utc >= effective_iso.

    Mirrors the weekly stale-replica DELETE for the 5h axis.
    """
    end_iso, end_epoch = _future_week_end()
    resets_iso, resets_epoch_str, resets_epoch = _future_5h_block_window()
    window_key = ns["_canonical_5h_window_key"](resets_epoch)

    # Seed the pre-credit snapshot in the past (captured_at well before
    # effective_iso will end up at).
    conn = ns["open_db"]()
    try:
        _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=28.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        # Pre-stage a "stale replica" row whose captured_at_utc is "now"
        # — i.e. AT-or-AFTER what the credit branch will compute as
        # effective_iso (the 10-min floor of dt.datetime.now()). This
        # row carries the pre-credit value (28) and should be deleted
        # by the post-credit cleanup.
        now_iso = ns["now_utc_iso"]()
        _seed_5h_snapshot(
            conn,
            captured_at_utc=now_iso,
            weekly_percent=42.0,
            five_hour_percent=28.0,                   # matches prior_5h_pct
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(
        percent=43.0,  # vary so dedup-vs-last doesn't swallow
        resets_at=end_epoch,
        five_hour_percent=8.0,
        five_hour_resets_at=resets_epoch_str,
    )
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        # The stale-replica row (captured "now" with 28%) must be DELETE'd.
        stale = conn.execute(
            "SELECT COUNT(*) AS c FROM weekly_usage_snapshots "
            "WHERE five_hour_window_key = ? "
            "  AND round(five_hour_percent, 1) = 28.0 "
            "  AND captured_at_utc >= ? ",
            (window_key, "2026-05-14T11:00:00Z"),
        ).fetchone()["c"]
        # The original pre-credit seed (2026-05-14T10:00:00Z) is before
        # effective_iso so it stays; the stale replica at "now" goes.
        assert stale == 0, "stale-replica row(s) must be deleted"
        # Original pre-credit seed survives.
        survived = conn.execute(
            "SELECT COUNT(*) AS c FROM weekly_usage_snapshots "
            "WHERE captured_at_utc = '2026-05-14T10:00:00Z'"
        ).fetchone()["c"]
        assert survived == 1, "pre-credit seed must not be deleted"
    finally:
        conn.close()


# ── self-heal probe parity with weekly (Round-3 Item 2) ──────────────


def test_5h_self_heal_probe_scoped_to_active_segment(ns, tmp_path):
    """Round-3 Item 2: when live record-usage bails on dedup-no-insert,
    the self-heal probe must also re-check whether a 5h-milestone is
    owed IN THE ACTIVE SEGMENT (not over the whole block ledger).

    Failure mode this guards against:
      1. Pre-credit: user climbed to 28% → milestones 1..28 stamped
         with ``reset_event_id = 0`` (segment 0).
      2. Credit fires (28 → 8); ``five_hour_reset_events`` row written
         with positive id N.
      3. Post-credit user climbs to 10% inside the same block; one tick
         crashes after ``insert_usage_snapshot`` but before
         ``maybe_update_five_hour_block`` could land milestone-10 in
         segment N.
      4. Next tick hits the dedup path (same percents as the last
         snapshot) and falls through to self-heal.

    Without segment-awareness, ``MAX(percent_threshold)`` over the
    whole block reads 28 — far above the latest_5h_floor of 10 — and
    the probe silently no-ops. Segment-aware probe correctly reads
    ``MAX(percent_threshold) WHERE reset_event_id = N`` (which is
    NULL: no rows yet) and triggers heal so milestones 1..10 in the
    post-credit segment finally land.

    Test asserts the post-credit segment ends up with at least one
    milestone after the heal tick (proving the probe fired).
    """
    end_iso, end_epoch = _future_week_end()
    resets_iso, resets_epoch_str, resets_epoch = _future_5h_block_window()
    window_key = ns["_canonical_5h_window_key"](resets_epoch)
    week_end_date = end_iso[:10]
    end_dt = dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    week_start_date = (end_dt - dt.timedelta(days=7)).date().isoformat()
    week_start_at = week_start_date + "T00:00:00+00:00"
    block_start_at = (
        dt.datetime.fromisoformat(resets_iso.replace("Z", "+00:00"))
        - dt.timedelta(hours=5)
    ).isoformat(timespec="seconds")

    conn = ns["open_db"]()
    try:
        # 1. Five-hour-block row with FRESH last_observed_at_utc
        # (so the existing pre-Round-3 probe wouldn't trigger heal).
        # Use NOW for last_observed_at so it's after the snapshot we
        # seed below.
        now_iso = ns["now_utc_iso"]()
        block_cur = conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, created_at_utc, last_updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (window_key, resets_iso, block_start_at, now_iso, now_iso,
             8.0, now_iso, now_iso),
        )
        block_id = int(block_cur.lastrowid)

        # 2. Pre-credit segment-0 milestones 1..28 (the high MAX that
        # would mask the post-credit heal under a non-segment-aware
        # probe).
        seed_usage_id = _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T09:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=8.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
            week_start_at=week_start_at,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
        )
        for pct in (1, 5, 10, 28):
            conn.execute(
                "INSERT INTO five_hour_milestones "
                "(block_id, five_hour_window_key, percent_threshold, "
                " captured_at_utc, usage_snapshot_id, "
                " block_cost_usd, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (block_id, window_key, pct, "2026-05-14T09:00:00Z",
                 seed_usage_id, 1.0 * pct, 0),
            )

        # 3. Five-hour reset event (segment positive id).
        ev_cur = conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, "
            " prior_percent, post_percent, effective_reset_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_iso, window_key, 28.0, 8.0,
             "2026-05-14T10:00:00+00:00"),
        )
        # Implicit: ev_id > 0 (AUTOINCREMENT). Heal probe should
        # scope MAX to reset_event_id = ev_id and find no rows.
        _ = int(ev_cur.lastrowid)

        # 4. Latest snapshot at 10% (post-credit) — but NO milestone
        # row yet in the post-credit segment. The live record-usage
        # path will bail on dedup since this matches the recorded
        # row's percents; self-heal MUST spot the missing milestone.
        _seed_5h_snapshot(
            conn,
            captured_at_utc=now_iso,
            weekly_percent=42.0,
            five_hour_percent=10.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
            week_start_at=week_start_at,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
        )
        conn.commit()
    finally:
        conn.close()

    # 5. Drive record-usage with the SAME 5h percent (10.0) and weekly
    # (42.0) so dedup-vs-last fires and the self-heal path runs.
    args = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=10.0,
        five_hour_resets_at=resets_epoch_str,
    )
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    # 6. The post-credit segment must now have at least one milestone
    # row. Without segment-aware probe, MAX(percent_threshold)=28
    # over the whole block would have suppressed the heal.
    conn = ns["open_db"]()
    try:
        post_credit_segment_rows = conn.execute(
            "SELECT percent_threshold, reset_event_id "
            "  FROM five_hour_milestones "
            " WHERE five_hour_window_key = ? "
            "   AND reset_event_id != 0",
            (window_key,),
        ).fetchall()
        assert len(post_credit_segment_rows) >= 1, (
            "post-credit segment milestone must be heal-emitted "
            "(pre-Round-3 bug: MAX over whole block = 28 masks the "
            "post-credit floor of 10 → probe no-ops)"
        )
        # First-observation in a fresh segment lands at current_floor
        # only (`start_threshold = current_floor` when max_existing is
        # None — see maybe_update_five_hour_block, NOT 1..floor). The
        # critical post-Round-3 assertion is that the threshold is in
        # the LOW range (post-credit climbing from zero), NOT in the
        # pre-credit 29..N range that the un-scoped MAX would have
        # required.
        thresholds = sorted(
            int(r["percent_threshold"]) for r in post_credit_segment_rows
        )
        assert thresholds[0] <= 10, (
            f"post-credit re-emission must land at the post-credit "
            f"floor (≤10), NOT max_pre+1 (=29). Got {thresholds}"
        )
    finally:
        conn.close()


# ── crash-recovery: pivots not gated on rowcount ──────────────────────


def test_pivots_run_when_event_row_already_committed(ns, tmp_path):
    """Round-3 / memory ``project_dedup_must_not_gate_side_effects.md``:
    pivots (HWM force-write + stale-replica DELETE) MUST run even when
    the ``INSERT OR IGNORE`` returns ``rowcount == 0`` because a prior
    crashed invocation already committed the event row.

    Failure mode the user could hit before this fix: tick N detects the
    credit, INSERTs the event row, ``conn.commit()`` lands — and then
    the process dies (CC self-update, OOM, kill -9) before the HWM
    force-write + DELETE could run. On tick N+1 the same predicate
    fires; ``INSERT OR IGNORE`` no-ops because the UNIQUE absorbs the
    second insert (rowcount=0); the old rowcount-gated pivots would
    therefore skip, leaving the system **permanently wedged** on the
    pre-credit HWM (status line frozen) and the stale-replica rows
    (clamp re-clamps fresh OAuth values upward).

    Fix: gate pivots on ``not is_dup`` (the post_percent-aware
    pre-check), not on rowcount. Both pivots are individually
    idempotent so re-running is safe.
    """
    end_iso, end_epoch = _future_week_end()
    resets_iso, resets_epoch_str, resets_epoch = _future_5h_block_window()
    window_key = ns["_canonical_5h_window_key"](resets_epoch)

    conn = ns["open_db"]()
    try:
        # 1. Pre-credit baseline snapshot (the LATEST snapshot when
        # tick N+1 runs — proves the detection predicate re-fires on
        # the recovery tick because prior_5h_pct still reads 28%).
        _seed_5h_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            weekly_percent=42.0,
            five_hour_percent=28.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        # 2. Pre-committed event row (simulates the crashed tick N
        # that wrote the event row before dying). Use a 10-min slot in
        # the recent past so ``unixepoch(captured_at_utc) >=
        # unixepoch(effective_iso)`` in the DELETE predicate still
        # matches stale-replica rows we stage at "now".
        now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        # Floor to the SAME 10-min slot that the recovery tick will
        # compute (within the same wall-second-ish window). Using
        # ``_floor_to_ten_minutes`` mirrors what production does.
        precommitted_floor = ns["_floor_to_ten_minutes"](now_utc)
        precommitted_iso = precommitted_floor.isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, "
            " prior_percent, post_percent, effective_reset_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                ns["now_utc_iso"](),
                window_key,
                28.0,
                8.0,
                precommitted_iso,
            ),
        )
        # 3. A stale-replica row carrying the pre-credit value, captured
        # at-or-after the pre-committed effective_iso (i.e. between the
        # crash and the recovery tick claude-statusline replayed the
        # pre-credit value into the DB). This row would dominate the
        # clamp's MAX over the post-credit segment if NOT cleaned up.
        _seed_5h_snapshot(
            conn,
            captured_at_utc=ns["now_utc_iso"](),
            weekly_percent=42.0,
            five_hour_percent=28.0,
            five_hour_window_key=window_key,
            five_hour_resets_at_iso=resets_iso,
            week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    # 4. Stage hwm-5h at the pre-credit value (proves the recovery
    # tick force-writes it back down).
    (tmp_path / ".local" / "share" / "cctally" / "hwm-5h").write_text(
        f"{window_key} 28.0\n"
    )

    # 5. Recovery tick (N+1). Same percents as the crashed tick. The
    # detection predicate re-fires (prior_5h_pct = 28 still, new = 8,
    # delta = 20pp ≥ 5pp). ``is_dup`` evaluates False because
    # ``prior_5h_pct != most_recent.post_percent`` (28 != 8). INSERT
    # OR IGNORE rowcount=0 (UNIQUE absorbs the pre-committed row).
    # Pivots MUST still fire.
    args = _record_usage_args(
        percent=42.0,
        resets_at=end_epoch,
        five_hour_percent=8.0,
        five_hour_resets_at=resets_epoch_str,
    )
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    # Assertion 1: still exactly one event row (UNIQUE absorbed).
    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT prior_percent, post_percent, effective_reset_at_utc "
            "  FROM five_hour_reset_events"
        ).fetchall()
        assert len(events) == 1, [dict(e) for e in events]
        assert round(events[0]["prior_percent"], 1) == 28.0
        assert round(events[0]["post_percent"], 1) == 8.0
    finally:
        conn.close()

    # Assertion 2: HWM was force-written back down (the pivot ran
    # despite rowcount=0). The file used to be "28.0"; recovery
    # tick must overwrite to "8.0".
    hwm_path = tmp_path / ".local" / "share" / "cctally" / "hwm-5h"
    hwm_parts = hwm_path.read_text().strip().split()
    assert len(hwm_parts) == 2, hwm_parts
    assert int(hwm_parts[0]) == window_key
    assert round(float(hwm_parts[1]), 1) == 8.0, (
        f"hwm-5h force-write pivot must run on recovery tick; got "
        f"{hwm_parts[1]} (pre-fix bug: stays at 28.0)"
    )

    # Assertion 3: stale-replica DELETE ran — no rows in
    # weekly_usage_snapshots at the pre-credit value captured AT-or-
    # AFTER the pre-committed effective_iso.
    conn = ns["open_db"]()
    try:
        stale_count = conn.execute(
            "SELECT COUNT(*) AS c FROM weekly_usage_snapshots "
            "WHERE five_hour_window_key = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "  AND round(five_hour_percent, 1) = 28.0",
            (window_key, precommitted_iso),
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
