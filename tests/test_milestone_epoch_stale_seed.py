"""Post-reset milestone epochs must never be seeded from a stale replica.

Production incident, 2026-09-01 (account ``c719887886403b0a1e3004e967dbd20e``,
week ``2026-08-29``). The user sat at 14%. Anthropic zeroed the weekly counter
in place at 17:59:41Z, the debounced ``CONFIRM_RESET`` leg fired
``_fire_in_place_credit``, and the ``week_reset_events`` row it wrote carried an
HOUR-FLOORED ``effective_reset_at_utc`` of 17:00:00Z — an instant that
back-dates before observations that were still legitimately pre-credit. The
stale-replica DELETE that runs alongside it bands on
``ABS(weekly_percent - observed_pre_credit_pct) < 1.0``, so the stored 13.0
snapshot captured at 17:33:38Z (``|13 - 14| == 1.0``) survived. That surviving
band is filed separately as issue #703 and is NOT what this module fixes.

What this module fixes is what the engine then did with that survivor:

1. The genuine ``weekly_percent = 0.0`` tick at 17:59:47Z was suppressed by the
   reset-aware high-water-mark clamp, so ``_usage_snapshot_fold_decision``
   returned a SKIP and no ``snapshot_accept`` event was emitted.
2. ``_pipeline_claude_usage`` then ran its dedup self-heal against the LATEST
   stored snapshot, which was the stale pre-credit 13.0 row.
3. ``maybe_record_milestone`` resolved that row into the NEW epoch (its
   17:33:38Z capture is at-or-after the back-dated 17:00:00Z effective
   instant), found no milestone in that epoch, and took the seeding branch —
   inserting a fabricated milestone at threshold 13.
4. Milestones are forward-only within an epoch, so the fabricated 13 opened the
   new ladder at a threshold the meter said had not been crossed and foreclosed
   every genuine crossing below 13 in that epoch.

Removing the fabricated row is the whole of what this module fixes. It does NOT
recover the 1%, 2% and 3% readings that followed: while the #703 band leaves the
13.0 replica stored inside the epoch, that row holds the reset-aware in-window
maximum at 13.0, so each of those readings is clamp-skipped before any snapshot
is written and ``maybe_record_milestone`` is never reached for it. Recovering
them needs #703. That scope boundary is itself pinned, by
``test_the_fix_does_not_recover_the_crossings_the_replica_suppresses``.

Two independent defences are pinned here.

**Fix 1 — a CLAMP skip must not derive a weekly milestone.**
``_usage_snapshot_fold_decision`` collapsed two very different skips into one
boolean. A DEDUP skip means the incoming observation AGREES with the latest
stored row, so the self-heal is correct: it exists so a tick killed between the
snapshot insert and the milestone insert still derives its milestone. A CLAMP
skip means the incoming observation CONTRADICTS the stored row (the incoming 7d
percent is strictly below the recorded in-window maximum), so deriving a weekly
milestone from that higher stored value fabricates a crossing. The decision now
returns a reason and the weekly milestone chokepoint is skipped on ``clamp``.

**Fix 2 — never seed a post-reset epoch's ladder without evidence of the
climb.** When ``reset_event_id != 0`` and the epoch holds no milestone yet,
``maybe_record_milestone`` now requires a stored observation inside the epoch
whose floored percent is strictly below the one being recorded.
"""
from __future__ import annotations

import argparse
import datetime as dt

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# The production week: 2026-08-29 -> 2026-09-05, reset on the hour.
WEEK_END_DT = dt.datetime(2026, 9, 5, 0, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END_ISO = WEEK_END_DT.isoformat(timespec="seconds")
WEEK_END_EPOCH = int(WEEK_END_DT.timestamp())
WEEK_START_DATE = "2026-08-29"
WEEK_START_AT = "2026-08-29T00:00:00+00:00"
WEEK_END_DATE = "2026-09-05"


# ── helpers ────────────────────────────────────────────────────────────


def _stamp_journal_id(conn, table: str, rowid: int) -> None:
    """Stamp the cutover-scheme ``journal_id`` on a directly-seeded row so the
    ingest cycle's harvest treats it as pre-existing rather than as a
    this-cycle insert with an unresolvable reverse-ref (same helper, same
    reason, as tests/test_in_place_credit_detection.py)."""
    conn.execute(
        f"UPDATE {table} SET journal_id = ? WHERE id = ?",
        (f"b:{table}:{rowid}", rowid),
    )


def _tick(ns, monkeypatch, *, at: str, percent: float) -> int:
    """Drive one ``record-usage`` observation at a PINNED capture instant.

    ``CCTALLY_AS_OF`` pins the detection clock and ``CCTALLY_TEST_PIN_CAPTURE``
    makes ``cmd_record_usage`` stamp the observation's ``captured_at`` from that
    same pinned instant, so the hour-floored reset anchor and each snapshot's
    ``captured_at_utc`` land exactly where the incident put them.
    """
    monkeypatch.setenv("CCTALLY_AS_OF", at)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    return ns["cmd_record_usage"](argparse.Namespace(
        percent=percent,
        resets_at=WEEK_END_EPOCH,
        five_hour_percent=None,
        five_hour_resets_at=None,
        week_start_name=None,
    ))


def _seed_usage_snapshot(conn, *, captured_at_utc: str, weekly_percent: float,
                         week_start_date: str = WEEK_START_DATE,
                         week_end_date: str = WEEK_END_DATE,
                         week_start_at: str = WEEK_START_AT,
                         week_end_at: str = WEEK_END_ISO) -> int:
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


def _seed_cost_snapshot(conn, *, cost_usd: float,
                        captured_at_utc: str = "2026-09-01T12:00:00Z",
                        week_start_date: str = WEEK_START_DATE,
                        week_end_date: str = WEEK_END_DATE,
                        week_start_at: str = WEEK_START_AT,
                        week_end_at: str = WEEK_END_ISO) -> int:
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


def _seed_reset_event(conn, *, effective: str,
                      new_week_end_at: str = WEEK_END_ISO,
                      observed_pre_credit_pct: float | None = None,
                      detected_at_utc: str = "2026-09-01T17:59:47Z") -> int:
    cur = conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct) "
        "VALUES (?, ?, ?, ?, ?)",
        (detected_at_utc, effective, new_week_end_at, effective,
         observed_pre_credit_pct),
    )
    rowid = int(cur.lastrowid)
    _stamp_journal_id(conn, "week_reset_events", rowid)
    conn.commit()
    return rowid


def _milestones(conn):
    return conn.execute(
        "SELECT percent_threshold, reset_event_id FROM percent_milestones "
        "WHERE week_start_date = ? ORDER BY reset_event_id, percent_threshold",
        (WEEK_START_DATE,),
    ).fetchall()


def _saved(usage_id: int, *, weekly_percent: float, captured_at: str) -> dict:
    return {
        "id": usage_id,
        "weeklyPercent": weekly_percent,
        "weekStartDate": WEEK_START_DATE,
        "weekEndDate": WEEK_END_DATE,
        "weekStartAt": WEEK_START_AT,
        "weekEndAt": WEEK_END_ISO,
        "fiveHourPercent": None,
        "capturedAt": captured_at,
    }


# ── T1: end-to-end reproduction of the production shape ────────────────


def test_stale_pre_credit_replica_does_not_fabricate_a_post_reset_milestone(
    ns, monkeypatch
):
    """The incident, replayed through ``cmd_record_usage``.

    The ticks are pinned to the real instants: the 13% and 14% readings land in
    the same hour as the reset, the two zeros arm and confirm the reset-to-zero
    debounce, and the resulting event anchors at the hour floor 17:00:00Z. The
    stale-replica DELETE removes the 14.0 row (``|14 - 14| < 1.0``) but leaves
    the 13.0 row (``|13 - 14| == 1.0``) — the #703 band defect, reproduced here
    as the PRECONDITION of the defect this module fixes.

    The engine must not turn that survivor into a milestone in the new epoch.
    """
    assert _tick(ns, monkeypatch, at="2026-09-01T17:33:38Z", percent=13.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:40:00Z", percent=14.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:41Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:47Z", percent=0.0) == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT id, effective_reset_at_utc, observed_pre_credit_pct "
            "FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, list(events)
        evt_id = int(events[0]["id"])
        assert events[0]["effective_reset_at_utc"] == "2026-09-01T17:00:00+00:00"
        assert events[0]["observed_pre_credit_pct"] == 14.0

        # Precondition (#703, not fixed here): the 13.0 replica survives the
        # 1.0pp band while the 14.0 row is deleted.
        survivors = conn.execute(
            "SELECT captured_at_utc, weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "ORDER BY captured_at_utc",
            (WEEK_START_DATE, "2026-09-01T17:00:00+00:00"),
        ).fetchall()
        assert [r["weekly_percent"] for r in survivors] == [13.0], list(survivors)

        rows = _milestones(conn)
        post = [r for r in rows if r["reset_event_id"] == evt_id]
        assert post == [], (
            "a post-reset epoch was seeded from the stale pre-credit replica: "
            f"{[dict(r) for r in post]}"
        )
        # The pre-credit ladder is untouched: threshold 13 seeded the epoch-0
        # ladder on the first tick, 14 followed it.
        assert [r["percent_threshold"] for r in rows
                if r["reset_event_id"] == 0] == [13, 14]
    finally:
        conn.close()


def test_genuine_post_reset_climb_records_each_threshold(ns, monkeypatch):
    """The same incident WITHOUT a surviving replica: every crossing lands.

    The pre-credit climb happens two hours before the reset, so the hour-floored
    17:00:00Z anchor leaves no pre-credit row inside the new epoch. The
    confirming zero is therefore accepted as a snapshot, and the 1%, 2% and 3%
    readings that follow are each recorded in the new epoch. This is the
    counterpart to the test above: it pins that Fix 2's evidence requirement
    does not block a real climb, and that Fix 1 leaves the accept path alone.
    """
    assert _tick(ns, monkeypatch, at="2026-09-01T15:33:38Z", percent=13.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T15:40:00Z", percent=14.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:41Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:47Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T18:18:50Z", percent=1.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T19:07:54Z", percent=2.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T20:58:10Z", percent=3.0) == 0

    conn = ns["open_db"]()
    try:
        evt = conn.execute(
            "SELECT id, effective_reset_at_utc FROM week_reset_events"
        ).fetchall()
        assert len(evt) == 1, list(evt)
        evt_id = int(evt[0]["id"])
        assert evt[0]["effective_reset_at_utc"] == "2026-09-01T17:00:00+00:00"

        rows = _milestones(conn)
        assert [r["percent_threshold"] for r in rows
                if r["reset_event_id"] == evt_id] == [1, 2, 3], \
            [dict(r) for r in rows]
    finally:
        conn.close()


def test_the_fix_does_not_recover_the_crossings_the_replica_suppresses(
    ns, monkeypatch
):
    """SCOPE PIN: removing the fabricated row does not restore the real ones.

    The incident's own shape, driven three readings further than the test
    above. While the #703 band leaves the 13.0 replica stored inside the new
    epoch, that row holds the reset-aware in-window MAXIMUM at 13.0, so the
    genuine 1%, 2% and 3% readings are each clamp-skipped before any snapshot
    is written and ``maybe_record_milestone`` is never reached for them. This
    fix stops the epoch being seeded from the replica; recovering the readings
    the replica suppresses needs #703, and nothing here should be worded or
    read as claiming otherwise.
    """
    assert _tick(ns, monkeypatch, at="2026-09-01T17:33:38Z", percent=13.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:40:00Z", percent=14.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:41Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:47Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T18:18:50Z", percent=1.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T19:07:54Z", percent=2.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T20:58:10Z", percent=3.0) == 0

    conn = ns["open_db"]()
    try:
        evt = conn.execute("SELECT id FROM week_reset_events").fetchall()
        assert len(evt) == 1, list(evt)
        evt_id = int(evt[0]["id"])

        # No snapshot was written for any of the three genuine readings: the
        # surviving replica is still the only row inside the epoch.
        stored = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "ORDER BY captured_at_utc",
            (WEEK_START_DATE, "2026-09-01T17:00:00+00:00"),
        ).fetchall()
        assert [r["weekly_percent"] for r in stored] == [13.0], list(stored)

        # And so the new epoch's ladder stays empty rather than gaining the
        # 1/2/3 rows. It is empty, not wrong, which is the whole point.
        rows = _milestones(conn)
        assert [r for r in rows if r["reset_event_id"] == evt_id] == [], \
            [dict(r) for r in rows]
    finally:
        conn.close()


# ── T2: Fix 1 in isolation — clamp skip vs dedup skip ──────────────────


def test_fold_decision_reports_why_it_skipped(ns):
    """``_usage_snapshot_fold_decision`` distinguishes its two skips.

    The reason is what ``_pipeline_claude_usage`` gates the weekly milestone
    derivation on, so it has to be part of the decision rather than re-derived
    at the call site.
    """
    import _cctally_journal as jr
    import _lib_record

    def payload(weekly_percent):
        return {
            "week_start_date": WEEK_START_DATE,
            "week_start_at": WEEK_START_AT,
            "week_end_at": WEEK_END_ISO,
            "weekly_percent": weekly_percent,
        }

    conn = ns["open_db"]()
    try:
        skip, _adj, reason = jr._usage_snapshot_fold_decision(conn, payload(10.0))
        assert (skip, reason) == (False, _lib_record.SNAPSHOT_ACCEPT)

        _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T12:00:00Z", weekly_percent=40.0)
        conn.commit()

        skip, _adj, reason = jr._usage_snapshot_fold_decision(conn, payload(39.0))
        assert (skip, reason) == (True, _lib_record.SNAPSHOT_SKIP_CLAMP)

        skip, _adj, reason = jr._usage_snapshot_fold_decision(conn, payload(40.0))
        assert (skip, reason) == (True, _lib_record.SNAPSHOT_SKIP_DEDUP)
    finally:
        conn.close()


def test_clamp_skip_does_not_derive_a_weekly_milestone(ns, monkeypatch):
    """A contradicting observation must not heal a milestone from the row it
    contradicts.

    The 40.0 snapshot is seeded directly, which is the kill-window shape: the
    snapshot exists but its milestone does not. An incoming 39.0 reading is
    below the recorded in-window maximum, so the clamp suppresses it. Pre-fix
    the self-heal read that 40.0 row back and recorded a threshold-40 milestone
    from an observation that said the meter was at 39.
    """
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T12:00:00Z", weekly_percent=40.0)
        _seed_cost_snapshot(conn, cost_usd=25.0)
        conn.commit()
    finally:
        conn.close()

    assert _tick(ns, monkeypatch, at="2026-09-01T12:05:00Z", percent=39.0) == 0

    conn = ns["open_db"]()
    try:
        assert _milestones(conn) == [], [dict(r) for r in _milestones(conn)]
        # The clamp still suppressed the snapshot itself.
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 39.0"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_dedup_skip_still_derives_the_weekly_milestone(ns, monkeypatch):
    """The kill-window self-heal keeps working on a DEDUP skip.

    Same seeded 40.0 snapshot with no milestone, but the incoming reading AGREES
    with it. That is the case the self-heal exists for, so the milestone must
    still materialize. (``bin/cctally-record-usage-selfheal-test`` is the
    harness-level regression for the same behavior.)
    """
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T12:00:00Z", weekly_percent=40.0)
        _seed_cost_snapshot(conn, cost_usd=25.0)
        conn.commit()
    finally:
        conn.close()

    assert _tick(ns, monkeypatch, at="2026-09-01T12:05:00Z", percent=40.0) == 0

    conn = ns["open_db"]()
    try:
        rows = _milestones(conn)
        assert [(r["percent_threshold"], r["reset_event_id"]) for r in rows] \
            == [(40, 0)], [dict(r) for r in rows]
    finally:
        conn.close()


# ── T3: Fix 2 in isolation — post-reset seeding needs evidence ─────────


def test_post_reset_seed_refused_without_a_lower_in_epoch_observation(ns):
    """A post-reset epoch whose only observation is the high one is refused.

    The reset event asserts the counter was at the credited level at
    17:00:00Z. A first in-epoch observation of 13% with nothing lower before it
    is therefore a stale pre-credit replica, not a crossing, and seeding the
    ladder at 13 would permanently block the genuine 1%, 2% and 3% crossings
    that follow.
    """
    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(conn, cost_usd=20.85)
        _seed_reset_event(conn, effective="2026-09-01T17:00:00+00:00",
                          observed_pre_credit_pct=14.0)
        usage_id = _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T17:33:38Z", weekly_percent=13.0)
        conn.commit()
    finally:
        conn.close()

    ns["maybe_record_milestone"](
        _saved(usage_id, weekly_percent=13.0,
               captured_at="2026-09-01T17:33:38Z"))

    conn = ns["open_db"]()
    try:
        assert _milestones(conn) == [], [dict(r) for r in _milestones(conn)]
    finally:
        conn.close()


def test_post_reset_seed_allowed_once_a_lower_observation_exists(ns):
    """The same epoch, once a lower in-epoch observation records the climb.

    A 0.4% reading captured after the reset instant is the observable evidence
    that the counter climbed from the reset, so the ladder may be seeded at the
    current floor.
    """
    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(conn, cost_usd=20.85)
        evt_id = _seed_reset_event(conn, effective="2026-09-01T17:00:00+00:00",
                                   observed_pre_credit_pct=14.0)
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T17:20:00Z", weekly_percent=0.4)
        usage_id = _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T17:33:38Z", weekly_percent=13.0)
        conn.commit()
    finally:
        conn.close()

    ns["maybe_record_milestone"](
        _saved(usage_id, weekly_percent=13.0,
               captured_at="2026-09-01T17:33:38Z"))

    conn = ns["open_db"]()
    try:
        rows = _milestones(conn)
        assert [(r["percent_threshold"], r["reset_event_id"]) for r in rows] \
            == [(13, evt_id)], [dict(r) for r in rows]
    finally:
        conn.close()


def test_post_reset_evidence_ignores_observations_before_the_reset(ns):
    """Evidence is epoch-scoped: a lower reading captured BEFORE the reset
    instant belongs to the previous epoch and proves nothing about this one."""
    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(conn, cost_usd=20.85)
        _seed_reset_event(conn, effective="2026-09-01T17:00:00+00:00",
                          observed_pre_credit_pct=14.0)
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T09:00:00Z", weekly_percent=2.0)
        usage_id = _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T17:33:38Z", weekly_percent=13.0)
        conn.commit()
    finally:
        conn.close()

    ns["maybe_record_milestone"](
        _saved(usage_id, weekly_percent=13.0,
               captured_at="2026-09-01T17:33:38Z"))

    conn = ns["open_db"]()
    try:
        assert _milestones(conn) == [], [dict(r) for r in _milestones(conn)]
    finally:
        conn.close()


# ── T4: non-regression — the pre-credit epoch is untouched ─────────────


def test_pre_credit_epoch_seeds_without_any_lower_observation(ns):
    """``reset_event_id == 0`` seeding is unchanged.

    A fresh week / fresh install / ordinary mid-week first observation has no
    reset event asserting where the counter stood, so there is no evidence to
    demand: the ladder seeds at the current floor exactly as before, even though
    this week holds no observation below 3%.
    """
    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(conn, cost_usd=12.34)
        usage_id = _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T10:00:00Z", weekly_percent=3.0)
        conn.commit()
    finally:
        conn.close()

    ns["maybe_record_milestone"](
        _saved(usage_id, weekly_percent=3.0,
               captured_at="2026-09-01T10:00:00Z"))

    conn = ns["open_db"]()
    try:
        rows = _milestones(conn)
        assert [(r["percent_threshold"], r["reset_event_id"]) for r in rows] \
            == [(3, 0)], [dict(r) for r in rows]
    finally:
        conn.close()


# ── T5: the evidence rule's deliberate, disclosed cost ─────────────────


def test_goodwill_credit_to_a_nonzero_level_loses_that_levels_threshold(
    ns, monkeypatch
):
    """DELIBERATE TRADE-OFF — this asserts a cost we accepted, not a defect.

    Do not "repair" the engine to make this test's ``[3]`` become ``[2, 3]``
    without reading the reasoning below and updating this test on purpose.

    The evidence rule asks for a stored in-epoch observation flooring strictly
    below the threshold being recorded. A reset-to-zero satisfies it for free,
    because the credited ~0 reading is itself the evidence for every threshold
    above it. The >=25pp goodwill-credit leg does not, because the level it
    credits to is non-zero: here Anthropic drops the counter from 67% to 2%,
    the 2% reading is the FIRST observation of the new epoch, and nothing
    lower was ever stored in it. Threshold 2 is therefore not recorded. The
    ladder seeds one threshold later, at 3, once the 2% reading has itself
    become the evidence for it.

    Nothing in the credit path fills that gap on its own. The auto-credit
    ``_fire_in_place_credit`` writes no synthetic snapshot — only the manual
    ``record-credit`` op's ``_apply_credit`` does — so the epoch's first row
    always arrives from the ordinary accept path, at whatever level the meter
    reports.

    We accept losing one threshold rather than fabricating one, because a
    missed threshold is recoverable and a fabricated one is permanent
    (milestones are forward-only within an epoch). Follow-up issue #707 is the
    principled repair: store the post-credit level on ``week_reset_events`` so
    the reset event itself supplies the evidence and the first threshold
    survives. Until that lands, this is the shipped behavior.
    """
    assert _tick(ns, monkeypatch, at="2026-09-01T10:00:00Z", percent=67.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T10:30:00Z", percent=2.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T11:15:00Z", percent=3.0) == 0

    conn = ns["open_db"]()
    try:
        evt = conn.execute(
            "SELECT id, effective_reset_at_utc, observed_pre_credit_pct "
            "FROM week_reset_events"
        ).fetchall()
        assert len(evt) == 1, list(evt)
        evt_id = int(evt[0]["id"])
        assert evt[0]["observed_pre_credit_pct"] == 67.0
        assert evt[0]["effective_reset_at_utc"] == "2026-09-01T10:00:00+00:00"

        # The 2% reading IS stored — the credit path accepted it. Only its
        # milestone is missing, and only because it is the epoch's floor.
        stored = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "ORDER BY captured_at_utc",
            (WEEK_START_DATE, "2026-09-01T10:00:00+00:00"),
        ).fetchall()
        assert [r["weekly_percent"] for r in stored] == [2.0, 3.0], list(stored)

        post = [r["percent_threshold"] for r in _milestones(conn)
                if r["reset_event_id"] == evt_id]
        assert post == [3], (
            "the credited level's own threshold is the documented cost of the "
            f"evidence rule (#707); got {post}"
        )

        # The pre-credit ladder is untouched: 67 seeded the epoch-0 ladder.
        assert [r["percent_threshold"] for r in _milestones(conn)
                if r["reset_event_id"] == 0] == [67]
    finally:
        conn.close()
