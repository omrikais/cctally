"""#750 S3 Task A5 — in-place credit deduplication is exact-origin only.

Spec §1.5. The first design of that section answered the un-flooring hazard
with an evidence-based guard: reuse the latest in-place event `E` when the
predecessor reading `P` exceeded the epoch maximum `M` and sat inside the
stale-replica band of `E.observed_pre_credit_pct`. That guard was implemented
and is now WITHDRAWN, because `M` was computed over a window that excludes `P`,
so `P > M` held for any monotonically climbing series whose top value was
observed exactly once — the ordinary case rather than the pathological one. A
week credited at 63, observed climbing back through 62 to 63, and genuinely
credited a second time satisfied every condition and lost its second credit.

An in-place credit is therefore refused as a duplicate when, and only when, its
`origin_observation_id` is non-null and equals that of an existing event for the
same account. That is the identity the epoch-1013 partial unique index already
enforces, so `INSERT OR IGNORE` is the whole mechanism and there is no
pre-check. A candidate that names no origin keeps the legacy tuple identity
`(account_key, old_week_end_at, new_week_end_at)`, enforced by the other partial
index. Every other detection is admitted.

**The residual, asserted rather than described.** A stale pre-credit high
followed by a low reading, arriving under a new observation identity, is
indistinguishable from a genuine second climb and credit. Both are admitted.
The failure direction is deliberate: a phantom event is visible in the week's
segmentation and can be corrected, whereas a suppressed genuine reset does not
self-heal (maintainer decision 4).
"""
from __future__ import annotations

import argparse
import datetime as dt

import pytest

from conftest import load_script, redirect_paths

WEEK_START_DATE = "2026-08-29"
WEEK_END_DT = dt.datetime(2026, 9, 5, 15, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END_ISO = WEEK_END_DT.isoformat(timespec="seconds")
WEEK_START_AT = "2026-08-29T15:00:00+00:00"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _at(hour, minute=0):
    return dt.datetime(2026, 9, 1, hour, minute, 0, tzinfo=dt.timezone.utc)


def _iso(d):
    return d.isoformat(timespec="seconds")


def _seed_snapshot(conn, *, captured, percent, account_key="unattributed",
                   journal_id=None):
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, account_key) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (_iso(captured), WEEK_START_DATE, WEEK_END_DT.date().isoformat(),
         WEEK_START_AT, WEEK_END_ISO, percent, "test", "{}", account_key))
    rowid = int(cur.lastrowid)
    conn.execute(
        "UPDATE weekly_usage_snapshots SET journal_id = ? WHERE id = ?",
        (journal_id or f"b:weekly_usage_snapshots:{rowid}", rowid))
    return rowid


def _seed_event(conn, *, effective, pre_credit, account_key="unattributed",
                origin=None):
    """An in-place credit event: `old_week_end_at == effective`."""
    cur = conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
        " origin_observation_id) VALUES (?,?,?,?,?,?,?)",
        (_iso(effective), _iso(effective), WEEK_END_ISO, _iso(effective),
         pre_credit, account_key, origin))
    return int(cur.lastrowid)


def _seed_claude_cache(ns):
    """One Claude cache entry plus its `session_files` metadata row."""
    path = "/tmp/claude/projects/repo/session.jsonl"
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            (path, 100, 1, 100, "2026-09-05T09:00:00Z", "session-a", "/repo"))
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, input_tokens, "
            " output_tokens, cache_create_tokens, cache_read_tokens, "
            " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (path, 0, "2026-09-05T09:30:00+00:00",
             "claude-3-5-sonnet-20241022", 0, 0, 100, 0, 40, "unattributed"))
        conn.commit()
    finally:
        conn.close()


def _events(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, effective_reset_at_utc, observed_pre_credit_pct, "
        "       origin_observation_id "
        "FROM week_reset_events ORDER BY id")]


def _fire(ns, conn, *, percent, pre_credit, effective,
          account_key="unattributed", origin=None, ctx=None, commit=True):
    ns["_fire_in_place_credit"](
        conn, WEEK_START_DATE, WEEK_END_ISO, percent,
        observed_pre_credit_pct=pre_credit, effective_dt=effective,
        as_of=_iso(effective), commit=commit, ctx=ctx,
        account_key=account_key, origin_observation_id=origin)


# --------------------------------------------------------------------------
# What the withdrawn guard suppressed, and now must not
# --------------------------------------------------------------------------

def test_a5_a_climb_back_to_the_credited_level_is_credited_again(ns):
    """The counterexample that withdrew the guard, pinned as required
    behaviour.

    A week is credited at 63. The new epoch is then observed at 2, 30, 58, 62
    and 63.0, which is an ordinary monotonic climb back to the level it was
    credited from, and it is genuinely credited a second time. Under the
    withdrawn guard `M` was 62 — the maximum over a window that EXCLUDED the
    63.0 predecessor — so `P > M` held, the reading 0 was at or below `M`, and
    63.0 sat inside the 1pp band of the first credit's own pre-credit level.
    All three conditions held on a series with nothing pathological in it, and
    the second credit was discarded.

    Two events, or the guard is back.
    """
    conn = ns["open_db"]()
    try:
        _seed_event(conn, effective=_at(10), pre_credit=63.0)
        for minute, pct in ((5, 2.0), (15, 30.0), (25, 58.0), (35, 62.0)):
            _seed_snapshot(conn, captured=_at(10, minute), percent=pct)
        _seed_snapshot(conn, captured=_at(10, 50), percent=63.0)
        conn.commit()

        _fire(ns, conn, percent=0.0, pre_credit=63.0, effective=_at(11))

        events = _events(conn)
        assert len(events) == 2, (
            "the second credit of the week was discarded", events)
        assert [e["effective_reset_at_utc"] for e in events] == [
            _iso(_at(10)), _iso(_at(11))]
    finally:
        conn.close()


def test_a5_a_stale_pre_credit_high_is_admitted_and_names_the_residual(ns):
    """The residual, asserted rather than described.

    This is the production sequence 0, 1, 2, 63, 2: a stalled statusline
    republishes the level the week was already credited from, and the next
    reading is low again. The implementation cannot distinguish that from a
    genuine climb from 2 straight back to 63 followed by a real credit,
    because both present one high predecessor reading under a new observation
    identity and one low current reading. It admits both.

    The direction is deliberate. A phantom event fragments the week's
    segmentation visibly and can be corrected; a suppressed genuine reset
    leaves milestones on a superseded cycle with no visible symptom and does
    not self-heal. A future change that separates the two would be an
    improvement, not a regression — but it must not do so by reintroducing a
    series-shape heuristic, which is what this module's predecessor did.
    """
    conn = ns["open_db"]()
    try:
        _seed_event(conn, effective=_at(10), pre_credit=63.0)
        for minute, pct in ((5, 0.0), (20, 1.0), (35, 2.0)):
            _seed_snapshot(conn, captured=_at(10, minute), percent=pct)
        _seed_snapshot(conn, captured=_at(10, 50), percent=63.0)
        conn.commit()

        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11))

        assert len(_events(conn)) == 2, _events(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# What exact-origin identity does refuse
# --------------------------------------------------------------------------

def test_a5_a_replayed_observation_writes_exactly_one_event(ns):
    """The only deduplication left. A replay of one tick carries the same
    originating observation, so the origin partial unique index refuses the
    second insert."""
    conn = ns["open_db"]()
    try:
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11),
              origin="o:aaaaaaaaaaaaaaaa")
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11, 30),
              origin="o:aaaaaaaaaaaaaaaa")

        events = _events(conn)
        assert len(events) == 1, events
        assert events[0]["effective_reset_at_utc"] == _iso(_at(11)), (
            "the replay must not move the event it replayed")
    finally:
        conn.close()


def test_a5_two_origins_both_write_on_one_legacy_boundary_tuple(ns):
    """Acceptance criterion 4. Two detections whose legacy tuple
    `(account_key, old_week_end_at, new_week_end_at)` is IDENTICAL still write
    two events, because a row that names an origin is unique on the origin
    instead. The legacy tuple index is partial on `origin_observation_id IS
    NULL` precisely so it cannot reach these rows."""
    conn = ns["open_db"]()
    try:
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11),
              origin="o:aaaaaaaaaaaaaaaa")
        _fire(ns, conn, percent=1.0, pre_credit=58.0, effective=_at(11),
              origin="o:bbbbbbbbbbbbbbbb")

        events = _events(conn)
        assert len(events) == 2, events
        assert [e["origin_observation_id"] for e in events] == [
            "o:aaaaaaaaaaaaaaaa", "o:bbbbbbbbbbbbbbbb"]
        assert {e["effective_reset_at_utc"] for e in events} == {_iso(_at(11))}
    finally:
        conn.close()


def test_a5_one_origin_under_two_accounts_writes_two_events(ns):
    """The origin index leads with `account_key`, so one observation id under
    two accounts is two identities rather than one."""
    conn = ns["open_db"]()
    try:
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11),
              account_key="acct-a", origin="o:aaaaaaaaaaaaaaaa")
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11),
              account_key="acct-b", origin="o:aaaaaaaaaaaaaaaa")

        assert len(_events(conn)) == 2, _events(conn)
    finally:
        conn.close()


def test_a5_two_origin_null_credits_on_one_tuple_write_one_event(ns):
    """A caller with no journal line keeps the legacy tuple identity, and two
    candidates sharing that tuple are one event. This is the shape a
    hand-driven or pre-journal caller produces."""
    conn = ns["open_db"]()
    try:
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11))
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11))

        assert len(_events(conn)) == 1, _events(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The pivots are not gated by the dedup decision
# --------------------------------------------------------------------------

def test_a5_the_pivots_run_on_the_credits_own_terms(ns):
    """The stale-replica DELETE and the high-water-mark force-write run on
    every call, including one whose insert the origin index refused, and they
    run against the CALL's own effective instant and pre-credit baseline. A
    prior run may have committed the event and died before the pivots."""
    conn = ns["open_db"]()
    try:
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11),
              origin="o:aaaaaaaaaaaaaaaa")
        # A pre-credit replica lands AFTER the credit instant, then the same
        # observation is replayed.
        poisoned = _seed_snapshot(conn, captured=_at(11, 10), percent=63.0)
        survivor = _seed_snapshot(conn, captured=_at(11, 20), percent=2.0)
        conn.commit()

        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11),
              origin="o:aaaaaaaaaaaaaaaa")

        assert len(_events(conn)) == 1, _events(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE id = ?",
            (poisoned,)).fetchone()[0] == 0, (
            "the refused insert gated the stale-replica DELETE")
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE id = ?",
            (survivor,)).fetchone()[0] == 1
    finally:
        conn.close()
    assert (ns["APP_DIR"] / "hwm-7d").read_text().strip().split() == [
        WEEK_START_DATE, "2.0"]


# --------------------------------------------------------------------------
# Durability: a DELETE that no new event carries must still be journalled
# --------------------------------------------------------------------------

def _journal_records():
    import _cctally_core
    import _cctally_journal
    import _lib_journal as J
    out = []
    for seg in _cctally_journal.list_segments():
        for raw in (_cctally_core.JOURNAL_DIR / seg).read_bytes().splitlines():
            if not raw.strip():
                continue
            rec = J.decode_line(raw)
            if rec is not None:
                out.append(rec)
    return out


def _journal_evts():
    return [r for r in _journal_records() if r.get("t") == "evt"]


def _wce_ids():
    return [e["id"] for e in _journal_evts() if e["id"].startswith("wce:")]


def _constructed_replay_id(origin, event):
    """`wce:replay:<origin>:<digest>` built with the production helpers.

    `docs/journal-gotchas.md` makes an evt id an opaque token for construction
    only, so these cases construct the id and compare for equality instead of
    matching a prefix or splitting one apart. It is also strictly stronger: a
    prefix match passes for ANY suffix, a random one included.

    `kind` is dropped because the digest's input is the payload the emitter
    passes as `columns`, and `make_evt` adds the fold discriminator afterwards.
    """
    import _lib_journal as _lj
    payload = {k: v for k, v in event["payload"].items() if k != "kind"}
    return _lj.evt_id("wce", "replay", origin,
                      _lj.effects_payload_digest(payload))


def test_a5_a_delete_no_new_event_carries_is_journalled_as_its_own_effect(ns):
    """The durable recovery, kept because it answers a SEPARATE defect.

    When the insert wins, the doomed snapshots' journal ids ride the harvested
    `wr` event and a rebuild reproduces the removal. When the insert does not
    win there is no new `wr` row to carry them, and an inline DELETE alone is
    undone by the next rebuild, because the poisoned snapshot is itself a
    retained `snapshot_accept` event that the rebuild re-folds. The removal is
    therefore journalled as its own effects-only `weekly_credit_effects` event,
    keyed on the originating observation.
    """
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        ctx = jr.IngestContext(conn=conn, batch=[])
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11),
              origin="o:aaaaaaaaaaaaaaaa", ctx=ctx, commit=False)
        conn.commit()

        _seed_snapshot(conn, captured=_at(11, 10), percent=63.0,
                       journal_id="sa:poisoned")
        conn.commit()

        ctx2 = jr.IngestContext(conn=conn, batch=[])
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11),
              origin="o:aaaaaaaaaaaaaaaa", ctx=ctx2, commit=False)
        conn.commit()

        assert len(_events(conn)) == 1, _events(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:poisoned'").fetchone()[0] == 0
    finally:
        conn.close()

    import _lib_journal as _lj
    expected_payload = {
        "suppression": ["sa:poisoned"],
        "suppression_table": "weekly_usage_snapshots",
        "hwm_floor": {"week_start_date": WEEK_START_DATE,
                      "weekly_percent": 2.0},
    }
    expected_id = _lj.evt_id(
        "wce", "replay", "o:aaaaaaaaaaaaaaaa",
        _lj.effects_payload_digest(expected_payload))
    # Equality against the constructed id, not a prefix match: the id is an
    # opaque token, and a prefix match would pass for any suffix at all.
    assert _wce_ids() == [expected_id], _wce_ids()
    wce = [e for e in _journal_evts() if e["id"] == expected_id]
    assert wce[0]["payload"]["suppression"] == ["sa:poisoned"], (
        "the recovery event does not carry the row the DELETE removed")
    assert wce[0]["payload"]["hwm_floor"] == {
        "week_start_date": WEEK_START_DATE, "weekly_percent": 2.0}


def test_a5_a_winning_insert_emits_no_separate_recovery_event(ns):
    """The recovery is the fallback, not a second copy. When the insert wins,
    the suppression rides the `wr` harvest event through `ctx.suppression_map`
    and no effects-only event is emitted beside it."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        _seed_snapshot(conn, captured=_at(11, 10), percent=63.0,
                       journal_id="sa:poisoned")
        conn.commit()
        ctx = jr.IngestContext(conn=conn, batch=[])
        _fire(ns, conn, percent=2.0, pre_credit=63.0, effective=_at(11),
              origin="o:aaaaaaaaaaaaaaaa", ctx=ctx, commit=False)
        conn.commit()

        # `week_reset_identity_parts` spells an origin-bearing row's key
        # `(account_key, "origin", origin)`, which is what the harvested evt
        # id is built from too.
        assert ctx.suppression_map == {
            ("unattributed", "origin", "o:aaaaaaaaaaaaaaaa"): ["sa:poisoned"]}
    finally:
        conn.close()
    assert [e["id"] for e in _journal_evts()
            if e["id"].startswith("wce:")] == []


# --------------------------------------------------------------------------
# End to end through the real write path
# --------------------------------------------------------------------------

def _record(ns, monkeypatch, *, at, percent, resets_at):
    monkeypatch.setenv("CCTALLY_AS_OF", at)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    return ns["cmd_record_usage"](argparse.Namespace(
        percent=percent, resets_at=resets_at, five_hour_percent=None,
        five_hour_resets_at=None, week_start_name=None))


def test_a5_two_credits_in_one_week_survive_a_rebuild_as_two_events(
        ns, monkeypatch):
    """Acceptance criterion 4, end to end. Every reading below arrives through
    the real write path, so each is a retained journal event and the rebuild
    re-derives the week from them. Two genuine credits in one week must be two
    events live AND after the rebuild — the shape the withdrawn guard collapsed
    into one."""
    import _cctally_journal as jr

    week_end = dt.datetime(2026, 9, 8, 15, 0, 0, tzinfo=dt.timezone.utc)
    epoch = int(week_end.timestamp())

    def tick(hour, minute, percent):
        assert _record(
            ns, monkeypatch,
            at=dt.datetime(2026, 9, 5, hour, minute, 0,
                           tzinfo=dt.timezone.utc).isoformat().replace(
                               "+00:00", "Z"),
            percent=percent, resets_at=epoch) == 0

    tick(9, 0, 63.0)     # pre-credit level
    tick(10, 0, 0.0)     # >=25pp goodwill credit -> first event
    tick(10, 20, 30.0)   # an observed climb inside the new epoch
    tick(10, 35, 62.0)
    tick(10, 50, 63.0)
    tick(11, 0, 0.0)     # credited again -> second event

    conn = ns["open_db"]()
    try:
        live = _events(conn)
    finally:
        conn.close()
    assert len(live) == 2, ("the second credit of the week was lost", live)
    assert live[0]["origin_observation_id"] != live[1]["origin_observation_id"]

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    conn = ns["open_db"]()
    try:
        rebuilt = _events(conn)
    finally:
        conn.close()
    assert len(rebuilt) == 2, ("the rebuild lost or doubled an event", rebuilt)
    assert [r["origin_observation_id"] for r in rebuilt] == [
        e["origin_observation_id"] for e in live]


def test_a5_the_recovery_delete_survives_a_rebuild_from_the_journal(
        ns, monkeypatch):
    """Acceptance criterion 4's third clause: the recovery effects event
    survives a rebuild, and only a JOURNALLED delete can.

    Every reading arrives through the real write path, so the poisoned
    pre-credit replica is a retained `snapshot_accept` event that the rebuild
    re-folds from the journal. The refused insert is reached the way production
    reaches it — the crash retry of one tick: the observation id is a digest
    over the record, so replaying an identical tick carries the identical
    origin and the epoch-1013 origin index refuses the second insert. That
    call's DELETE therefore has no `wr` evt to ride, and without its own
    `weekly_credit_effects` event the rebuild resurrects exactly the row it
    removed.

    The distinction the sibling cases cannot draw is what this one is for.
    `test_a5_a_delete_no_new_event_carries_is_journalled_as_its_own_effect`
    asserts the event was emitted with the right payload, and its "the poisoned
    row is gone" check cannot tell the applier from the inline DELETE that ran
    microseconds earlier in the same call.
    `test_a5_two_credits_in_one_week_survive_a_rebuild_as_two_events` does
    rebuild, but both of its inserts win, so it never reaches the refused-insert
    path at all. Only a rebuild AFTER a refused insert separates the two, and it
    fails if the DELETE is inline rather than journalled.
    """
    import _cctally_journal as jr

    week_end = dt.datetime(2026, 9, 8, 15, 0, 0, tzinfo=dt.timezone.utc)
    epoch = int(week_end.timestamp())

    def tick(hour, minute, percent):
        assert _record(
            ns, monkeypatch,
            at=dt.datetime(2026, 9, 5, hour, minute, 0,
                           tzinfo=dt.timezone.utc).isoformat().replace(
                               "+00:00", "Z"),
            percent=percent, resets_at=epoch) == 0

    def poisoned_rows():
        conn = ns["open_db"]()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT id, captured_at_utc, weekly_percent, journal_id "
                "FROM weekly_usage_snapshots "
                "WHERE captured_at_utc LIKE '2026-09-05T10:30%' "
                "ORDER BY id")], _events(conn)
        finally:
            conn.close()

    tick(9, 0, 63.0)     # pre-credit level
    tick(10, 0, 0.0)     # >=25pp goodwill credit -> the only event
    tick(10, 30, 63.0)   # a stalled statusline republishes the credited level

    # Non-vacuity, asserted HERE rather than assumed. Both "the poisoned row is
    # gone" assertions below pass trivially if the 10:30 reading ever stops
    # landing a row at all — a clamp, band or detection change would do it —
    # and this case would then prove nothing while staying green. Its
    # non-vacuity used to rest on a requirement enforced in a DIFFERENT test,
    # which is not a property this test can rely on.
    seeded_poisoned, _ = poisoned_rows()
    assert len(seeded_poisoned) == 1, (
        "the 10:30 reading landed no poisoned replica, so every assertion "
        "below is vacuous", seeded_poisoned)

    tick(10, 0, 0.0)     # the SAME tick replayed: same id, refused insert

    live_poisoned, live_events = poisoned_rows()
    assert len(live_events) == 1, (
        "the replayed observation opened a second event", live_events)
    assert live_poisoned == [], (
        "the inline DELETE did not remove the poisoned replica", live_poisoned)

    # No assertion on the recovery event's presence here: the sibling above
    # already owns that, and asserting it would make THIS case fail on the same
    # line rather than on the rebuild, which is the only thing it adds.
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    rebuilt_poisoned, rebuilt_events = poisoned_rows()
    assert rebuilt_poisoned == [], (
        "the rebuild re-folded the poisoned `snapshot_accept` and resurrected "
        "the row the refused insert removed; an inline DELETE is not durable",
        rebuilt_poisoned)
    assert len(rebuilt_events) == 1, rebuilt_events


def test_a5_one_origin_refused_twice_journals_both_removals(ns, monkeypatch):
    """The recovery id digests its payload, so one origin can journal two
    different removals.

    `doomed_ids` is a live query over `weekly_usage_snapshots`, so an origin
    that reaches the refused-insert path twice against different poisoned rows
    produces two different payloads. Under an id naming the origin alone the
    second is a `CLASSIFY_CONFLICT`: `emit_model_a` withholds the line,
    `_record_dropped_conflict` notes it, and `_converge_row_from_effective`
    returns `CONVERGE_DROPPED` because the family is effects-only — so the call
    returns normally and the second DELETE stands as exactly the inline-only
    effect the event exists to replace. The rebuild then resurrects the second
    poisoned row while the first stays removed, which is the asymmetry this
    case asserts against.
    """
    import _cctally_journal as jr

    week_end = dt.datetime(2026, 9, 8, 15, 0, 0, tzinfo=dt.timezone.utc)
    epoch = int(week_end.timestamp())

    def tick(hour, minute, percent):
        assert _record(
            ns, monkeypatch,
            at=dt.datetime(2026, 9, 5, hour, minute, 0,
                           tzinfo=dt.timezone.utc).isoformat().replace(
                               "+00:00", "Z"),
            percent=percent, resets_at=epoch) == 0

    tick(9, 0, 63.0)
    tick(10, 0, 0.0)     # the only event; origin O
    tick(10, 30, 63.0)   # first poisoned replica
    tick(10, 0, 0.0)     # O replayed -> refused, removal #1
    tick(10, 45, 63.0)   # second poisoned replica, a DIFFERENT row
    tick(10, 0, 0.0)     # O replayed again -> refused, removal #2

    def poisoned():
        conn = ns["open_db"]()
        try:
            return sorted(
                r[0] for r in conn.execute(
                    "SELECT captured_at_utc FROM weekly_usage_snapshots "
                    "WHERE round(weekly_percent, 1) = 63.0 "
                    "  AND captured_at_utc > '2026-09-05T10:00:00Z'"))
        finally:
            conn.close()

    assert poisoned() == [], (
        "an inline DELETE did not remove both replicas", poisoned())

    conn = ns["open_db"]()
    try:
        origin = conn.execute(
            "SELECT origin_observation_id FROM week_reset_events"
        ).fetchone()[0]
    finally:
        conn.close()

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    assert poisoned() == [], (
        "the rebuild resurrected a poisoned replica whose removal was never "
        "journalled", poisoned())
    wce = [e for e in _journal_evts() if e["id"].startswith("wce:")]
    assert len(wce) == 2, (
        "the second refused insert was withheld as a conflict",
        [e["id"] for e in wce])
    # One equality, two facts: every id names the event row's origin, and every
    # suffix is the digest of THAT line's own payload. The prior form split the
    # id on its last colon, which `docs/journal-gotchas.md` forbids and which
    # would have accepted a random suffix.
    assert [e["id"] for e in wce] == [
        _constructed_replay_id(origin, e) for e in wce]
    assert len({e["id"] for e in wce}) == 2, (
        "the two removals must be two events", [e["id"] for e in wce])


def test_a5_the_recovery_effects_event_survives_db_rederive(ns, monkeypatch):
    """Acceptance criterion 4's second operation, which the rebuild sibling
    cannot stand in for.

    `db rederive` is not a rebuild with extra steps. It replays the retained
    records into a private scratch projection, DIFFS the derived desired set
    against the current effective events, and only then rebuilds.
    `weekly_credit_effects` is classified `rederived` at
    `bin/_lib_rederive.py:59`, so `_is_owned_event` returns True and the
    recovery event enters `build_claude_usage_plan`'s diff — where an owned
    event that is current but not desired is planned as a TOMBSTONE. The
    payload digest makes the id sensitive to the scratch projection's state,
    so a scratch deriving a different suppression list would retire this event
    and add a replacement; were the replacement's list empty, the rebuild
    `db rederive` runs afterwards would resurrect exactly the row this
    criterion is about.

    Convergence there is an argument, not evidence, and the sibling case above
    rebuilds without ever reaching the planner. This settles it: the plan must
    name the recovery event in NO action, and the poisoned row must still be
    gone after `--yes` has applied and rebuilt.
    """
    week_end = dt.datetime(2026, 9, 8, 15, 0, 0, tzinfo=dt.timezone.utc)
    epoch = int(week_end.timestamp())

    def tick(hour, minute, percent):
        assert _record(
            ns, monkeypatch,
            at=dt.datetime(2026, 9, 5, hour, minute, 0,
                           tzinfo=dt.timezone.utc).isoformat().replace(
                               "+00:00", "Z"),
            percent=percent, resets_at=epoch) == 0

    def poisoned():
        conn = ns["open_db"]()
        try:
            return [r[0] for r in conn.execute(
                "SELECT captured_at_utc FROM weekly_usage_snapshots "
                "WHERE captured_at_utc LIKE '2026-09-05T10:30%'")]
        finally:
            conn.close()

    # `_validate_cache_rows` refuses a plan for an account with positive usage
    # and no Claude cache rows behind it, `unattributed` included — the refusal
    # exists so a rederive can never zero a real week from an empty cache. One
    # row plus its `session_files` metadata satisfies it.
    _seed_claude_cache(ns)

    tick(9, 0, 63.0)
    tick(10, 0, 0.0)     # the only event; origin O
    tick(10, 30, 63.0)   # the poisoned pre-credit replica

    assert len(poisoned()) == 1, (
        "the 10:30 reading landed no poisoned replica, so this case is "
        "vacuous", poisoned())

    tick(10, 0, 0.0)     # O replayed -> refused insert -> recovery event

    assert poisoned() == [], "the inline DELETE did not run"
    recovery = _wce_ids()
    assert len(recovery) == 1, recovery

    preview = ns["preview_db_rederive"]("claude-usage")
    assert [
        (a.event_id, a.disposition) for a in preview.plan.actions
        if a.event_id in recovery
    ] == [], (
        "the planner acted on the recovery event; a current-but-not-desired "
        "owned event is planned as a tombstone, and the rebuild that follows "
        "would then re-fold the poisoned snapshot_accept",
        [(a.event_id, a.disposition) for a in preview.plan.actions])

    # The assertion above is a NEGATIVE over the action list, and a retained
    # event contributes a count rather than an action. On its own it cannot
    # tell "the planner retained it" from "the planner never owned it", so
    # reclassifying `weekly_credit_effects` away from `rederived` would keep it
    # green while removing the property it exists to prove.
    assert preview.plan.counts["retain"] >= 1, (
        "the planner owned and retained nothing, so the absence of an action "
        "on the recovery event proves nothing about tombstoning",
        dict(preview.plan.counts))

    assert ns["cmd_db_rederive"](argparse.Namespace(
        family="claude-usage", yes=True, json=True)) == 0

    assert poisoned() == [], (
        "db rederive resurrected the poisoned replica the refused insert "
        "removed", poisoned())

    import _lib_journal as _lj
    selection = _lj.resolve_effective_events(_journal_records())
    selected = selection.by_id.get(recovery[0])
    assert selected is not None and selected.status == "active", (
        "the recovery event did not survive db rederive as an active event",
        recovery[0], selected)
