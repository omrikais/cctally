"""Post-reset milestone epochs must never be seeded from a stale replica.

Production incident, 2026-09-01 (account ``c719887886403b0a1e3004e967dbd20e``,
week ``2026-08-29``). The user sat at 14%. Anthropic zeroed the weekly counter
in place at 17:59:41Z, the debounced ``CONFIRM_RESET`` leg fired
``_fire_in_place_credit``, and the ``week_reset_events`` row it wrote carried an
HOUR-FLOORED ``effective_reset_at_utc`` of 17:00:00Z — an instant that
back-dates before observations that were still legitimately pre-credit. The
stale-replica DELETE that ran alongside it banded on
``ABS(weekly_percent - observed_pre_credit_pct) < 1.0``, so the stored 13.0
snapshot captured at 17:33:38Z (``|13 - 14| == 1.0``) survived. That band was
filed separately as issue #703 and was NOT what this module fixed.

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

#703 + #707 has since landed, and the module now pins the recovered state rather
than the loss. Nothing is deleted to get there. The 13.0 and 14.0 readings are
KEPT, because their captures precede the credit's own observation and at that
moment a high reading is indistinguishable from genuine history (spec §5.1);
what changed is that the accounting floor is the EXACT observation instant
17:59:41 rather than the hour floor, so both fall outside the epoch, the clamp
stops raising the rendered value, and the genuine 0%, 1%, 2% and 3% readings
each land as themselves. ``test_the_incident_recovers_every_genuine_crossing``
pins that outright; it is the inversion of the scope pin this module used to
carry.

Three defences are pinned here, and the first two remain necessary after the
floor moved: a replica arriving after the observation instant but removed only
on a later tick, or one arriving in the gap between the credit and its
detection, can still be the first row a fresh epoch sees.

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
                      detected_at_utc: str = "2026-09-01T17:59:47Z",
                      observed_at_utc: str | None = None,
                      observed_post_credit_pct: float | None = None,
                      week_start_date: str = WEEK_START_DATE) -> int:
    """One credit row.

    The three fact columns are parameters, not omissions. Left NULL, every test
    built on this helper ran the `post_credit_pct is None` LEGACY fallback of
    `post_reset_seed_has_climb_evidence` rather than the rule #707 introduced —
    so a mutation making that function return True whenever a post-credit fact
    is present passed every one of them. `observed_at_utc` defaults to
    ``effective`` rather than to NULL for the same reason: it is the accounting
    instant the epoch window is built from, and a NULL there falls back to the
    hour-floored display instant.
    """
    cur = conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, week_start_date, "
        " observed_at_utc, confirming_capture_at_utc, "
        " observed_post_credit_pct) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (detected_at_utc, effective, new_week_end_at, effective,
         observed_pre_credit_pct, week_start_date,
         observed_at_utc or effective, observed_at_utc or effective,
         observed_post_credit_pct),
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
    debounce, and the resulting event anchors its DISPLAY instant at the hour
    floor 17:00:00Z while recording its ACCOUNTING instant as the exact
    17:59:41Z.

    Both pre-credit readings are kept, and that is deliberate (#703 + #707
    §5.1): their capture instants precede the credit's own observation, so
    nothing contradicts them, and at their capture a high reading is
    indistinguishable from genuine history. They are outside the epoch because
    §5.3's floor is the observation instant, not because they were deleted.

    The guard this module exists for is unchanged by that: the engine must not
    seed the new epoch's ladder from a reading captured before the credit.

    This is NOT the #706 comparison any more, and saying so is the point. After
    §5.1 anchored the epoch on the exact observation instant, the only in-epoch
    reading here is the credited 0.0 itself, so the seeding guard ADMITS and the
    empty post-credit ladder below is the arithmetic of a 0% reading crossing no
    threshold. The #706 comparison — an in-epoch minimum of 13 against a
    credited 0 — is constructed in isolation by
    `test_post_reset_seed_refused_without_a_lower_in_epoch_observation`, which
    carries the post-credit fact so it exercises the rule that replaced it.
    """
    assert _tick(ns, monkeypatch, at="2026-09-01T17:33:38Z", percent=13.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:40:00Z", percent=14.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:41Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:47Z", percent=0.0) == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT id, effective_reset_at_utc, observed_pre_credit_pct, "
            "       observed_at_utc FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, list(events)
        evt_id = int(events[0]["id"])
        assert events[0]["effective_reset_at_utc"] == "2026-09-01T17:00:00+00:00"
        assert events[0]["observed_pre_credit_pct"] == 14.0

        # The event records the exact observation instant alongside the
        # hour-floored display one, and the accounting floor reads the exact
        # one. Both pre-credit readings are kept and both are outside the epoch.
        assert events[0]["observed_at_utc"] == "2026-09-01T17:59:41Z"
        kept = conn.execute(
            "SELECT captured_at_utc, weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? ORDER BY captured_at_utc, id",
            (WEEK_START_DATE,),
        ).fetchall()
        assert [r["weekly_percent"] for r in kept] == [13.0, 14.0, 0.0], \
            list(kept)
        in_epoch = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "ORDER BY captured_at_utc, id",
            (WEEK_START_DATE, events[0]["observed_at_utc"]),
        ).fetchall()
        assert [r["weekly_percent"] for r in in_epoch] == [0.0], list(in_epoch)

        rows = _milestones(conn)
        post = [r for r in rows if r["reset_event_id"] == evt_id]
        assert post == [], (
            "a post-reset epoch was seeded from the stale pre-credit replica: "
            f"{[dict(r) for r in post]}"
        )
        # The pre-credit ladder is intact. Thresholds 13 and 14 were crossed
        # before the credit and stay crossed: the evidence bracket anchors on
        # the exact observation instant, so neither reading is selected as a
        # replica and neither milestone is removed with one.
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


def test_the_incident_recovers_every_genuine_crossing(ns, monkeypatch):
    """The incident's full shape: every genuine crossing lands in its epoch.

    This test used to pin the OPPOSITE, and the inversion is the point (spec
    section 9). Under the hour-floored anchor the 13.0 reading captured at
    17:33:38 sat INSIDE the new epoch, held the reset-aware maximum at 13.0, and
    clamp-skipped the genuine 1%, 2% and 3% readings before any snapshot was
    written. It also held the detector's ``prior`` at 13.0, so the 1% tick armed
    the reset-to-zero marker and the 2% tick confirmed it — a SECOND credit
    recorded off a reading that was not real, whose only effect was to move the
    floor forward and let two of the three readings land under an epoch that
    should not exist.

    Nothing is deleted to fix this. The 13.0 and 14.0 readings are kept, exactly
    as section 5.1 requires: their captures precede the credit's observation, so
    they are genuine pre-credit history. What changed is where the epoch starts.
    The accounting floor is the exact observation instant 17:59:41, so both
    readings fall outside the epoch, the clamp stops raising the rendered value,
    the genuine 0.0 is accepted as the epoch's first observation, and 1%, 2% and
    3% each land as themselves.
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
        evt = conn.execute(
            "SELECT id, observed_at_utc, observed_post_credit_pct "
            "FROM week_reset_events ORDER BY id").fetchall()
        assert len(evt) == 1, (
            "a phantom second credit was recorded off a stale reading: "
            f"{[dict(r) for r in evt]}")
        assert evt[0]["observed_at_utc"] == "2026-09-01T17:59:41Z"
        assert evt[0]["observed_post_credit_pct"] == 0.0
        epoch = int(evt[0]["id"])

        # The genuine pre-credit readings are KEPT, and they are outside the
        # epoch because the floor is the observation instant.
        all_stored = [
            r["weekly_percent"] for r in conn.execute(
                "SELECT weekly_percent FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? ORDER BY captured_at_utc, id",
                (WEEK_START_DATE,))]
        assert all_stored == [13.0, 14.0, 0.0, 1.0, 2.0, 3.0], all_stored
        in_epoch = [
            r["weekly_percent"] for r in conn.execute(
                "SELECT weekly_percent FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? "
                "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
                "ORDER BY captured_at_utc, id",
                (WEEK_START_DATE, evt[0]["observed_at_utc"]))]
        assert in_epoch == [0.0, 1.0, 2.0, 3.0], in_epoch

        rows = _milestones(conn)
        assert [r["percent_threshold"] for r in rows
                if r["reset_event_id"] == epoch] == [1, 2, 3], \
            [dict(r) for r in rows]
        # And the pre-credit ladder is untouched.
        assert [r["percent_threshold"] for r in rows
                if r["reset_event_id"] == 0] == [13, 14], [dict(r) for r in rows]
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
                          observed_pre_credit_pct=14.0,
                          observed_post_credit_pct=0.0)
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


def test_post_reset_seed_refused_on_the_legacy_rule_without_the_landed_fact(ns):
    """The same refusal for a row that predates the post-credit column.

    A credit written before #707 records no landing level, so the comparison
    falls back to #706's "strictly below the threshold being recorded". An
    in-epoch minimum of 13 against a threshold of 13 is not strictly below it,
    so the seed is refused. Both branches are pinned because a rule that
    collapsed to one of them would still pass the other's tests.
    """
    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(conn, cost_usd=20.85)
        _seed_reset_event(conn, effective="2026-09-01T17:00:00+00:00",
                          observed_pre_credit_pct=14.0,
                          observed_post_credit_pct=None)
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


def test_post_reset_seed_refused_when_the_epoch_minimum_exceeds_the_landing(ns):
    """The discriminating case for the rule that replaced #706's.

    The epoch holds an observation BELOW the threshold being recorded — 5%
    against a seed of 13 — so the legacy rule would admit. The credit says the
    counter landed at 0, and 5 is not at or below 0, so the new rule refuses.
    Without this case a mutation returning True whenever a landing level is
    present passes the whole module.
    """
    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(conn, cost_usd=20.85)
        _seed_reset_event(conn, effective="2026-09-01T17:00:00+00:00",
                          observed_pre_credit_pct=14.0,
                          observed_post_credit_pct=0.0)
        _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T17:20:00Z", weekly_percent=5.0)
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


def test_post_reset_seed_records_the_landed_level_itself(ns):
    """#707's own case, in isolation.

    The credit landed at 2 and the epoch's minimum observation is that same 2,
    so threshold 2 is admitted — "at or below the credited level" rather than
    "strictly below the threshold". The legacy rule refuses this exact shape,
    which is the cost #706 paid and #707 removes.
    """
    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(conn, cost_usd=20.85)
        evt_id = _seed_reset_event(conn, effective="2026-09-01T17:00:00+00:00",
                                   observed_pre_credit_pct=67.0,
                                   observed_post_credit_pct=2.0)
        usage_id = _seed_usage_snapshot(
            conn, captured_at_utc="2026-09-01T17:20:00Z", weekly_percent=2.0)
        conn.commit()
    finally:
        conn.close()

    ns["maybe_record_milestone"](
        _saved(usage_id, weekly_percent=2.0,
               captured_at="2026-09-01T17:20:00Z"))

    conn = ns["open_db"]()
    try:
        rows = _milestones(conn)
        assert [(r["percent_threshold"], r["reset_event_id"]) for r in rows] \
            == [(2, evt_id)], [dict(r) for r in rows]
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
                                   observed_pre_credit_pct=14.0,
                                   observed_post_credit_pct=0.0)
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
                          observed_pre_credit_pct=14.0,
                          observed_post_credit_pct=0.0)
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


def test_a_goodwill_credit_records_the_credited_levels_threshold(
    ns, monkeypatch
):
    """#707's repair: the credited level's own threshold survives.

    This test used to assert the opposite, and #706 named the inversion as the
    follow-up that would land it. The evidence rule asked for a stored in-epoch
    observation flooring STRICTLY below the threshold being recorded. A
    reset-to-zero satisfies that for free, because the credited ~0 reading is
    itself the evidence for every threshold above it. A >=25pp goodwill credit
    to a NON-zero level did not: Anthropic drops the counter from 67% to 2%, the
    2% reading is the first observation of the new epoch, nothing lower was ever
    stored in it, and threshold 2 was refused. The ladder opened one threshold
    late, at 3.

    The reset event now records where the counter LANDED, so the epoch supplies
    its own evidence and the comparison changes from "strictly below the
    threshold" to "at or below the credited level". Nothing is fabricated: the
    admission still rests on a stored observation, and the level it is compared
    against is a fact the credit itself recorded rather than an inference.

    The #706 shape stays blocked, and
    `test_post_reset_seed_refused_without_a_lower_in_epoch_observation` is the
    isolated guard for it: an in-epoch minimum of 13 against a credited 0 is
    still refused, because 13 is not at or below 0.
    """
    assert _tick(ns, monkeypatch, at="2026-09-01T10:00:00Z", percent=67.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T10:30:00Z", percent=2.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T11:15:00Z", percent=3.0) == 0

    conn = ns["open_db"]()
    try:
        evt = conn.execute(
            "SELECT id, effective_reset_at_utc, observed_pre_credit_pct, "
            "       observed_at_utc, observed_post_credit_pct "
            "FROM week_reset_events"
        ).fetchall()
        assert len(evt) == 1, list(evt)
        evt_id = int(evt[0]["id"])
        assert evt[0]["observed_pre_credit_pct"] == 67.0
        assert evt[0]["observed_post_credit_pct"] == 2.0
        assert evt[0]["effective_reset_at_utc"] == "2026-09-01T10:00:00+00:00"
        assert evt[0]["observed_at_utc"] == "2026-09-01T10:30:00Z"

        # The 67% reading is KEPT — its capture precedes the credit's own
        # observation — and it is outside the epoch, because the accounting
        # floor is that observation instant rather than the hour floor.
        all_stored = [
            r["weekly_percent"] for r in conn.execute(
                "SELECT weekly_percent FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? ORDER BY captured_at_utc, id",
                (WEEK_START_DATE,))]
        assert all_stored == [67.0, 2.0, 3.0], all_stored
        in_epoch = [
            r["weekly_percent"] for r in conn.execute(
                "SELECT weekly_percent FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? "
                "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
                "ORDER BY captured_at_utc, id",
                (WEEK_START_DATE, evt[0]["observed_at_utc"]))]
        assert in_epoch == [2.0, 3.0], in_epoch

        post = [r["percent_threshold"] for r in _milestones(conn)
                if r["reset_event_id"] == evt_id]
        assert post == [2, 3], (
            "the credited level's own threshold was lost to the "
            f"inferred-climb rule; got {post}")

        # The pre-credit ladder keeps its own crossing at 67.
        assert [r["percent_threshold"] for r in _milestones(conn)
                if r["reset_event_id"] == 0] == [67]
    finally:
        conn.close()
