"""A late stale replica's deletion must survive a rebuild (#703 + #707 §5.4).

A stale high reading that arrives AFTER a credit needs its own durable
mechanism, because the existing one is not durable. The automatic suppression
captures its target list only when the credit's event row is first inserted, and
the physical delete that runs on every LATER detector pass is unjournaled.
Replay can only reproduce identifiers already in the original event's payload,
and a rebuild deliberately never re-runs the detector or the clamp. So a late
replica is deleted live and restored by the next rebuild — permanently, on every
rebuild, because nothing in the journal records that it was ever removed.

The fix is a recurring, effects-only `weekly_replica_suppression` journal event.
Its applier folds at order 70, after milestone folding at 60, deleting dependent
milestones first and snapshots second, so a rebuild materializes both and then
replays the removal on top.

Two alternatives were rejected on grounds. Refusing the reading at admission is
unsound: at that instant a high reading is indistinguishable from a genuine
climb, and only a later contradicting observation tells them apart. A permanent
derived rule ignoring any in-epoch reading at or above the old pre-credit level
has the same information defect and would additionally discard a legitimate
re-climb to that level across every consumer.
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


WEEK_END_DT = dt.datetime(2026, 9, 5, 0, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END_ISO = WEEK_END_DT.isoformat(timespec="seconds")
WEEK_END_EPOCH = int(WEEK_END_DT.timestamp())
WEEK_START_DATE = "2026-08-29"
LATE_REPLICA_AT = "2026-09-01T18:05:00Z"


def _tick(ns, monkeypatch, *, at: str, percent: float) -> int:
    monkeypatch.setenv("CCTALLY_AS_OF", at)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    return ns["cmd_record_usage"](argparse.Namespace(
        percent=percent,
        resets_at=WEEK_END_EPOCH,
        five_hour_percent=None,
        five_hour_resets_at=None,
        week_start_name=None,
    ))


def _snapshots(ns):
    conn = ns["open_db"]()
    try:
        return [
            (r["captured_at_utc"], r["weekly_percent"])
            for r in conn.execute(
                "SELECT captured_at_utc, weekly_percent "
                "FROM weekly_usage_snapshots WHERE week_start_date = ? "
                "ORDER BY captured_at_utc", (WEEK_START_DATE,))
        ]
    finally:
        conn.close()


def _rebuild(ns):
    import _cctally_journal as jr
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))


def _drive_credit_then_late_replica(ns, monkeypatch):
    """The incident's own shape, then a late replica.

    The pre-credit climb happens two hours early so the hour-floored anchor
    leaves no genuine row inside the credit's window; the two zeros arm and
    confirm the reset-to-zero debounce. The 14.0 reading at 18:05 is the LATE
    replica — it lands after the credit, above the post-credit level, so the
    write clamp admits it and it is journaled as a real snapshot.
    """
    assert _tick(ns, monkeypatch, at="2026-09-01T15:33:38Z", percent=13.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T15:40:00Z", percent=14.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:41Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:47Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at=LATE_REPLICA_AT, percent=14.0) == 0
    assert (LATE_REPLICA_AT, 14.0) in _snapshots(ns), _snapshots(ns)


def test_a_late_replica_deletion_survives_a_rebuild(ns, monkeypatch):
    _drive_credit_then_late_replica(ns, monkeypatch)

    # An ORDINARY later detector pass. It fires no credit — the event row is
    # already there and this tick contradicts nothing but the replica — so the
    # removal it decides is one nothing had journaled before §5.4's recurring
    # pass existed.
    assert _tick(ns, monkeypatch, at="2026-09-01T18:10:00Z", percent=0.0) == 0
    assert len(_credit_keys(ns)) == 1, "the later pass fired a second credit"
    assert len(_snapshots(ns)) >= 1
    assert (LATE_REPLICA_AT, 14.0) not in _snapshots(ns), (
        "the later pass did not delete the late replica at all")

    _rebuild(ns)
    assert (LATE_REPLICA_AT, 14.0) not in _snapshots(ns), (
        "the late replica came back: suppression was live-only, not journaled")


def test_the_suppression_applier_runs_after_milestone_folding():
    import _cctally_journal as jr
    assert jr.fold_order_for("weekly_replica_suppression") == 70
    assert (jr.fold_order_for("weekly_replica_suppression")
            > jr.fold_order_for("percent_milestone"))
    assert (jr.fold_order_for("weekly_replica_suppression")
            > jr.fold_order_for("snapshot_accept"))


def test_the_family_is_rederived():
    """A scratch rederive must be able to add, supersede or tombstone these
    events; a `retained` classification would freeze a wrong deletion forever."""
    import _lib_rederive as rd
    cls = rd._EVT_CLASSIFICATIONS["weekly_replica_suppression"]
    assert cls.mode == "rederived", cls


def test_the_applier_deletes_dependents_before_snapshots(ns):
    """Foreign-key-safe order. A milestone referencing a deleted snapshot is a
    dangling reference, and this codebase's foreign keys are documentation-only,
    so nothing else would catch it."""
    import _cctally_journal as jr

    conn = ns["open_db"]()
    try:
        cur = conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json, journal_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (LATE_REPLICA_AT, WEEK_START_DATE, "2026-09-05",
             "2026-08-29T00:00:00+00:00", WEEK_END_ISO, 14.0, "statusline",
             "{}", "sa:o:replica"))
        snap_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, percent_threshold, cumulative_cost_usd, "
            " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id, "
            " reset_event_id, journal_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (LATE_REPLICA_AT, WEEK_START_DATE, "2026-09-05",
             "2026-08-29T00:00:00+00:00", WEEK_END_ISO, 14, 1.0, None,
             snap_id, 0, 0, "pm:o:replica"))
        conn.commit()

        jr._apply_evt(conn, {
            "v": 1, "t": "evt", "id": "wrs:test", "rev": 0,
            "at": "2026-09-01T18:10:00Z", "src": "ingest",
            "payload": {
                "kind": "weekly_replica_suppression",
                "account_key": "unattributed",
                "credit_key": "sa:o:credit",
                "snapshots": ["sa:o:replica"],
                "milestones": ["pm:o:replica"],
            },
        })
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:o:replica'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM percent_milestones "
            "WHERE journal_id = 'pm:o:replica'").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_applier_is_idempotent(ns):
    """Deleting an already-absent logical id is a clean no-op, which is what
    makes a replayed event safe."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        for _ in range(2):
            jr._apply_evt(conn, {
                "v": 1, "t": "evt", "id": "wrs:test", "rev": 0,
                "at": "2026-09-01T18:10:00Z", "src": "ingest",
                "payload": {
                    "kind": "weekly_replica_suppression",
                    "account_key": "unattributed",
                    "credit_key": "sa:o:credit",
                    "snapshots": ["sa:o:absent"],
                    "milestones": ["pm:o:absent"],
                },
            })
        conn.commit()
    finally:
        conn.close()


# ── #703 + #707: the dependent-removal legs ─────────────────────────────────
#
# Two tests that used to sit here applied their payloads in a hand-written
# sequence rather than through the production emitter, so both passed
# identically under an implementation that still wrote `milestone_suppression`
# onto the order-50 family. They pinned statement order, not fold order. The
# structural claim lives in
# `tests/test_record_credit.py::test_force_removes_the_replaced_credits_dependents_at_fold_order_70`
# and the behavioral one — a real `--force`, a real journaled dependent, a real
# `rebuild_stats_index` — beside it.


def test_the_applier_also_removes_five_hour_dependents(ns):
    """`five_hour_milestones.usage_snapshot_id` references the same snapshot
    table `percent_milestones` does, so removing a snapshot without it leaves a
    reference pointing at a gone row. The foreign keys here are
    documentation-only, so nothing raises when that happens."""
    import _cctally_journal as jr

    conn = ns["open_db"]()
    try:
        cur = conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json, journal_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (LATE_REPLICA_AT, WEEK_START_DATE, "2026-09-05",
             "2026-08-29T00:00:00+00:00", WEEK_END_ISO, 14.0, "statusline",
             "{}", "sa:o:replica5h"))
        snap_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, is_closed, created_at_utc, "
            " last_updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            (999, "2026-09-01T20:00:00Z", "2026-09-01T15:00:00Z",
             LATE_REPLICA_AT, LATE_REPLICA_AT, 40.0, 0, LATE_REPLICA_AT,
             LATE_REPLICA_AT))
        block_id = int(conn.execute(
            "SELECT id FROM five_hour_blocks WHERE five_hour_window_key = 999"
        ).fetchone()[0])
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, "
            " captured_at_utc, usage_snapshot_id, journal_id) "
            "VALUES (?,?,?,?,?,?)",
            (block_id, 999, 40, LATE_REPLICA_AT, snap_id, "fhm:o:replica"))
        conn.commit()

        jr._apply_evt(conn, {
            "v": 1, "t": "evt", "id": "wrs:test5h", "rev": 0,
            "at": "2026-09-01T18:10:00Z", "src": "ingest",
            "payload": {
                "kind": "weekly_replica_suppression",
                "account_key": "unattributed",
                "credit_key": "sa:o:credit",
                "snapshots": ["sa:o:replica5h"],
                "milestones": [],
                "five_hour_milestones": ["fhm:o:replica"],
            },
        })
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_milestones "
            "WHERE journal_id = 'fhm:o:replica'").fetchone()[0] == 0, (
            "the 5h dependent of a removed snapshot was left dangling")
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:o:replica5h'").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_target_digest_is_unchanged_when_no_five_hour_dependent_exists():
    """Adding a third leg must not re-key every event already written.

    The field is omitted from the digest payload when it is empty, so a target
    set with no 5h dependent hashes exactly as it did before the leg existed and
    a reselecting pass converges on the line already there.
    """
    import hashlib
    import json
    import _cctally_journal as jr

    legacy = hashlib.sha256(json.dumps(
        {"snapshots": ["sa:a"], "milestones": ["pm:a"]},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:16]
    assert jr.replica_suppression_target_digest(
        ["sa:a"], ["pm:a"]) == legacy
    assert jr.replica_suppression_target_digest(
        ["sa:a"], ["pm:a"], ()) == legacy
    assert jr.replica_suppression_target_digest(
        ["sa:a"], ["pm:a"], ["fhm:a"]) != legacy


# ── #703 + #707 §5.4: the recurring pass, on an ORDINARY detector tick ───────


def _credit_keys(ns):
    conn = ns["open_db"]()
    try:
        return [r["credit_key"] for r in conn.execute(
            "SELECT credit_key FROM week_reset_events ORDER BY id")]
    finally:
        conn.close()


IMMEDIATE_REPLICA_AT = "2026-09-01T17:05:00Z"


def _drive_immediate_credit_then_late_replica(ns, monkeypatch):
    """The traced failure of the Tranche 2 review, driven through real ticks.

    Anthropic credits 46 to 2 and the immediate leg fires at 17:01. The
    external `claude-statusline` then replays its cached pre-credit 46.0 at
    17:05; `hwm_clamp_applies` only suppresses readings BELOW the maximum, so
    the replay is admitted and stored.
    """
    assert _tick(ns, monkeypatch, at="2026-09-01T16:00:00Z", percent=46.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:01:00Z", percent=2.0) == 0
    assert _credit_keys(ns) != [], "the immediate credit did not fire"
    assert _tick(ns, monkeypatch, at=IMMEDIATE_REPLICA_AT, percent=46.0) == 0
    assert (IMMEDIATE_REPLICA_AT, 46.0) in _snapshots(ns), _snapshots(ns)


def test_an_ordinary_pass_removes_a_late_replica_after_an_immediate_credit(
        ns, monkeypatch):
    """No crashed marker, no second credit — just the next genuine tick.

    Before this pass existed, `_automatic_replicas` ran only from inside
    `_fire_in_place_credit`, and a credit fires once: the immediate leg needs a
    drop against `prior_pct` and the debounced leg clears its marker. So the
    bracket was evaluated exactly once, one second wide, and no later detector
    pass ran it at all.
    """
    _drive_immediate_credit_then_late_replica(ns, monkeypatch)
    assert _tick(ns, monkeypatch, at="2026-09-01T17:10:00Z", percent=2.0) == 0
    assert (IMMEDIATE_REPLICA_AT, 46.0) not in _snapshots(ns), (
        "the ordinary later pass never ran the replica selector")


def test_the_late_replica_does_not_fire_a_second_credit(ns, monkeypatch):
    """The replica becomes `prior_pct` on the next genuine tick, and the drop
    back to the credited level reads as a fresh 44pp goodwill credit. The
    high-water mark heals, but the week gains a phantom accounting epoch and the
    milestone ladder restarts."""
    _drive_immediate_credit_then_late_replica(ns, monkeypatch)
    assert _tick(ns, monkeypatch, at="2026-09-01T17:10:00Z", percent=2.0) == 0
    assert len(_credit_keys(ns)) == 1, (
        "a second credit fired against the stale replica")


def test_the_ordinary_pass_keeps_genuine_climb_below_the_replica_level(
        ns, monkeypatch):
    """The sweep must never delete real post-credit climb.

    Every reading between the credited level and the pre-credit level is
    indistinguishable from genuine usage, so only a reading at or above the
    pre-credit level is a replica by construction.
    """
    _drive_immediate_credit_then_late_replica(ns, monkeypatch)
    assert _tick(ns, monkeypatch, at="2026-09-01T17:20:00Z", percent=9.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:30:00Z", percent=12.0) == 0
    kept = {pct for _at, pct in _snapshots(ns)}
    assert {9.0, 12.0} <= kept, _snapshots(ns)
    assert (IMMEDIATE_REPLICA_AT, 46.0) not in _snapshots(ns), (
        "the replica outlived two contradicting readings")
    assert ("2026-09-01T16:00:00Z", 46.0) in _snapshots(ns), (
        "the genuine PRE-credit reading at the same level was swept with it")


def test_a_genuine_reclimb_past_the_pre_credit_level_is_never_swept(
        ns, monkeypatch):
    """The unbounded form of this rule is the one that destroys data.

    A credit of 44pp leaves the whole week to climb back through it. If the
    sweep had no upper bound in time, every reading at or above 46 would be
    deleted for the rest of the week and the counter would freeze just below
    the pre-credit level. The bound is evidence: a row is only removed when a
    LATER in-epoch reading contradicts it, and within one accounting epoch the
    counter never decreases on its own.
    """
    _drive_immediate_credit_then_late_replica(ns, monkeypatch)
    assert _tick(ns, monkeypatch, at="2026-09-01T17:10:00Z", percent=2.0) == 0
    for at, pct in (("2026-09-02T10:00:00Z", 30.0),
                    ("2026-09-03T10:00:00Z", 45.0),
                    ("2026-09-03T12:00:00Z", 46.0),
                    ("2026-09-03T14:00:00Z", 47.0)):
        assert _tick(ns, monkeypatch, at=at, percent=pct) == 0
    kept = {pct for _at, pct in _snapshots(ns)}
    assert {45.0, 46.0, 47.0} <= kept, (
        f"genuine re-climb past the pre-credit level was swept: {_snapshots(ns)}")
    assert len(_credit_keys(ns)) == 1


def test_the_replica_level_is_the_lower_of_the_recorded_and_stored_peaks(ns):
    """#703's own quantity mismatch, as a rule rather than a scenario.

    `observed_pre_credit_pct` is the level the detector REMEMBERED — the armed
    marker's baseline on the debounced leg — while a replay reproduces a value
    that was actually written to `weekly_usage_snapshots`. In the incident those
    differed by exactly 1.0, and keying on the remembered one alone leaves a
    replay of the stored peak standing. The rule takes the lower of the two, so
    neither quantity can hide a replay behind the other.
    """
    import _lib_credit_selection as sel

    conn = ns["open_db"]()
    try:
        for at, pct in (("2026-09-01T15:33:38Z", 13.0),
                        ("2026-09-01T15:40:00Z", 12.0)):
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, source, "
                " payload_json, account_key) VALUES (?,?,?,?,?,?,?,?,?)",
                (at, WEEK_START_DATE, "2026-09-05",
                 "2026-08-29T00:00:00+00:00", WEEK_END_ISO, pct, "statusline",
                 "{}", "unattributed"))
        conn.commit()
        assert sel.resolve_replica_level(
            conn, week_start_date=WEEK_START_DATE, account_key="unattributed",
            observed_at="2026-09-01T17:59:41Z",
            observed_pre_credit_pct=14.0) == 13.0
        assert sel.resolve_replica_level(
            conn, week_start_date=WEEK_START_DATE, account_key="unattributed",
            observed_at="2026-09-01T17:59:41Z",
            observed_pre_credit_pct=None) == 13.0
        assert sel.resolve_replica_level(
            conn, week_start_date=WEEK_START_DATE, account_key="unattributed",
            observed_at="2026-09-01T15:00:00Z",
            observed_pre_credit_pct=9.0) == 9.0
        assert sel.resolve_replica_level(
            conn, week_start_date=WEEK_START_DATE, account_key="unattributed",
            observed_at="2026-09-01T15:00:00Z",
            observed_pre_credit_pct=None) is None
    finally:
        conn.close()


def test_a_suppressed_threshold_is_not_re_recorded_from_a_later_climb(
        ns, monkeypatch):
    """The cost the recurring pass pays, pinned rather than left implicit.

    A replica that lands well above the credited level fills the segment's
    ladder up to its own value before anything can contradict it. The sweep
    removes those rows, but their journal lines stand: a milestone identity is
    `pm:<account>:<week>:<epoch>:<threshold>`, the journal is append-only, and a
    second emission at the same revision is a divergence the emitter withholds.
    Worse, its convergence cannot succeed either — the journaled event
    references the deleted snapshot — so before the segment's forward-only mark
    became journal-backed, the next genuine crossing of one of those thresholds
    aborted the whole ingest cycle on a NOT NULL violation.

    So a suppressed threshold stays unrecorded for that segment, and only
    thresholds above the fabricated peak are still available. Leaving the
    fabricated rows in place instead would be the #706 defect.
    """
    _drive_immediate_credit_then_late_replica(ns, monkeypatch)
    assert _tick(ns, monkeypatch, at="2026-09-01T17:10:00Z", percent=2.0) == 0
    for at, pct in (("2026-09-02T10:00:00Z", 30.0),
                    ("2026-09-03T14:00:00Z", 47.0)):
        assert _tick(ns, monkeypatch, at=at, percent=pct) == 0

    conn = ns["open_db"]()
    try:
        credit_id = conn.execute(
            "SELECT id FROM week_reset_events").fetchone()[0]
        thresholds = sorted(
            r[0] for r in conn.execute(
                "SELECT percent_threshold FROM percent_milestones "
                "WHERE reset_event_id = ?", (int(credit_id),)))
        import _cctally_milestones as ms
        assert ms.get_max_journaled_milestone_for_segment(
            conn, WEEK_START_DATE, reset_event_id=int(credit_id),
            account_key="unattributed") == 47
    finally:
        conn.close()
    assert thresholds == [2, 47], thresholds
