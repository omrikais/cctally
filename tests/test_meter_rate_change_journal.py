"""#661 S2 Task C2 — the rate-change journal event and its transaction (§6.5).

`_apply_quota_threshold_event` only FOLDS an already-journaled event; it does
not claim a latch, append or dispatch. `run_stats_ingest` is the sole stats
writer and it enforces journal-first, then commit, then notify. So this family
is journaled the same way: the descriptor is decided under the calibration
file's leaf lock (§6.5 step 1), that lock is released before any stats lock
(step 2), the descriptor is passed through `run_stats_ingest` (step 3), the
event is appended and applied inside the stats transaction (step 4), a
notification is queued only when the insert actually created a row (step 5),
and dispatch happens after the commit (step 6). #695 moved step 6 for this ONE
family: the payload comes back on `IngestResult.deferred_alerts` and
`cmd_quota` dispatches it after winning its delivery ledger's compare-and-set,
so a notification the cycle lost can be retried. Invariant (iv) is unchanged —
step-4a replay still has no `IngestContext` and still cannot reach either
sink.

The key comes from an UNJOURNALED file, which is exactly why the payload must
be self-sufficient: a later deletion or quarantine of `quota-calibrations.json`
cannot affect what a rebuild reconstructs.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
BOUNDARY = "2026-08-25T00:00:00+00:00"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    return ns


def _mrc(ns):
    return ns["_load_sibling"]("_lib_meter_rate_change")


def _regime(**over):
    base = {
        "effectiveFrom": "2026-07-25T00:00:00+00:00",
        "effectiveUntil": None,
        "unitsPerPoint": 2_442_620.0,
        "status": "ok",
        "fingerprint": "FP",
    }
    base.update(over)
    return base


def _transition(ns, **over):
    mrc = _mrc(ns)
    fields = dict(
        provider="claude", account_key="unattributed",
        effective_from=BOUNDARY, previous_units_per_point=2_442_620.0,
        new_units_per_point=1_685_000.0, severity="alarm",
        detected_at=NOW.isoformat())
    fields.update(over)
    return mrc.RateChangeTransition(**fields)


def _full_events(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT * FROM meter_rate_change_events ORDER BY effective_from")]


def _events(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT provider, account_key, effective_from FROM "
        "meter_rate_change_events ORDER BY effective_from")]


def _seed_journal():
    import _cctally_journal as jr
    import _lib_journal as J
    jr.append_record(
        J.make_obs(at="2026-08-29T09:00:00Z", src="record-usage",
                   provider="claude",
                   payload={"weekly_percent": 12.0, "source": "statusline"}),
        now_utc=NOW)


def _ingest_context(ns, conn):
    """A minimal `IngestContext` for driving the emitter directly.

    The real cycle builds this at `bin/_cctally_journal.py:6231`; here only
    the fields the emitter touches matter. `batch` has no default on the
    dataclass, so it is passed explicitly — a stand-in object would make
    these tests pass for the wrong reason, because the emitter reads
    `ctx.conn`, `ctx.events_emitted`, `ctx.deferred_alerts` and
    `ctx.conflicts_dropped`.
    """
    import _cctally_journal as jr
    _seed_journal()
    return jr.IngestContext(conn=conn, batch=[])


def _mrc_evt(ns, transition, *, created_at):
    """The evt the emitter builds, assembled the same way it does."""
    import _lib_journal
    mrc = _mrc(ns)
    return _lib_journal.make_evt(
        kind=mrc.EVT_KIND,
        id=_lib_journal.evt_id(mrc.EVT_ID_PREFIX, *transition.identity()),
        at=created_at,
        payload=mrc.event_payload(transition, created_at=created_at))


def _journal_line_count(ns) -> int:
    """Total decoded journal lines across every segment."""
    import _cctally_core
    total = 0
    for path in sorted((_cctally_core.APP_DIR / "journal").glob("*.jsonl")):
        total += sum(1 for line in path.read_text().splitlines() if line.strip())
    return total


def _record(ns, transition, *, notify=False, notification_owed=False):
    import _cctally_journal as jr
    return jr.run_stats_ingest(
        mode="authoritative",
        meter_rate_change={"transition": transition, "notify": notify,
                           "created_at": NOW.isoformat(),
                           "notification_owed": notification_owed})


# --------------------------------------------------------------------------
# The pure detector (§6.3)
# --------------------------------------------------------------------------
def test_c2_the_initial_regime_never_fires(ns):
    """A new user's first fit has no predecessor to be adjacent to."""
    mrc = _mrc(ns)
    assert mrc.detect_transitions(
        [], [_regime()], provider="claude", account_key="unattributed",
        detected_at=NOW.isoformat()) == ()


def test_c2_a_confirmed_predecessor_to_successor_transition_fires(ns):
    """The discriminating twin of every negative case in this section."""
    mrc = _mrc(ns)
    after = [_regime(effectiveUntil=BOUNDARY),
             _regime(effectiveFrom=BOUNDARY, unitsPerPoint=1_685_000.0,
                     status="insufficient-history")]
    transitions = mrc.detect_transitions(
        [_regime()], after, provider="claude", account_key="unattributed",
        detected_at=NOW.isoformat())
    assert len(transitions) == 1
    transition = transitions[0]
    assert transition.identity() == ("claude", "unattributed", BOUNDARY)
    assert transition.previous_units_per_point == pytest.approx(2_442_620.0)
    assert transition.new_units_per_point == pytest.approx(1_685_000.0)
    assert transition.severity == "alarm", (
        "a 31% drop in units per point is the direction a user must act on")


def test_c2_a_detection_only_successor_still_fires(ns):
    """§1.1: the rate-change axis reads that regime pair DELIBERATELY,
    because detection is exactly what a detection-grade fit is for. The
    prediction gate suppresses PREDICTING from it, not detecting it."""
    mrc = _mrc(ns)
    after = [_regime(effectiveUntil=BOUNDARY),
             _regime(effectiveFrom=BOUNDARY, unitsPerPoint=1_685_000.0,
                     status="insufficient-history",
                     qualifications=["detection-only"])]
    assert len(mrc.detect_transitions(
        [_regime()], after, provider="claude", account_key="unattributed",
        detected_at=NOW.isoformat())) == 1


def test_c2_a_fingerprint_only_recalculation_never_fires(ns):
    """That path closes the open regime at `now` with `status: "stale"` and
    appends a fresh regime starting at the analysis start, so the two are
    neither adjacent nor a rate transition. Both guards are checked: the
    instants differ AND the predecessor is stale."""
    mrc = _mrc(ns)
    after = [_regime(effectiveUntil=NOW.isoformat(), status="stale"),
             _regime(effectiveFrom="2026-06-25T00:00:00+00:00",
                     unitsPerPoint=1_685_000.0, fingerprint="FP2")]
    assert mrc.detect_transitions(
        [_regime()], after, provider="claude", account_key="unattributed",
        detected_at=NOW.isoformat()) == ()


def test_c2_a_stale_predecessor_never_fires_even_when_adjacent(ns):
    """The status guard on its own, isolated from the instant guard."""
    mrc = _mrc(ns)
    after = [_regime(effectiveUntil=BOUNDARY, status="stale"),
             _regime(effectiveFrom=BOUNDARY, unitsPerPoint=1_685_000.0)]
    assert mrc.detect_transitions(
        [_regime()], after, provider="claude", account_key="unattributed",
        detected_at=NOW.isoformat()) == ()


def test_c2_an_already_recorded_transition_is_not_re_emitted(ns):
    """Present on BOTH sides, so a re-run of `cctally quota` appends no
    second journal line. The row's UNIQUE key is still the latch; this only
    keeps the journal from growing a line per invocation."""
    mrc = _mrc(ns)
    state = [_regime(effectiveUntil=BOUNDARY),
             _regime(effectiveFrom=BOUNDARY, unitsPerPoint=1_685_000.0)]
    assert mrc.detect_transitions(
        state, state, provider="claude", account_key="unattributed",
        detected_at=NOW.isoformat()) == ()


def test_c2_every_fresh_pair_is_reported_not_only_the_newest(ns):
    """The loss used to be permanent, not deferred (#661 S2 Stage C review).

    `fresh = [...]` then `start = max(fresh)` returned ONE descriptor, and on
    the next call both pairs were in `before`, so `fresh` was empty and the
    earlier transition was never recorded by anything. Spec section 6.3
    promises "once per exact key", which reads as at least once per key; that
    was at most one key per write. The durable row's UNIQUE key already
    dedups, so returning every fresh pair costs nothing and closes the gap.
    """
    mrc = _mrc(ns)
    first = "2026-07-25T00:00:00+00:00"
    after = [
        _regime(effectiveFrom="2026-06-01T00:00:00+00:00",
                effectiveUntil=first, unitsPerPoint=3_000_000.0),
        _regime(effectiveFrom=first, effectiveUntil=BOUNDARY,
                unitsPerPoint=2_442_620.0),
        _regime(effectiveFrom=BOUNDARY, unitsPerPoint=1_685_000.0),
    ]
    transitions = mrc.detect_transitions(
        [], after, provider="claude", account_key="unattributed",
        detected_at=NOW.isoformat())
    assert [t.effective_from for t in transitions] == [first, BOUNDARY], (
        "only the newest fresh pair was reported, and the earlier one is "
        "unrecoverable because the next call sees it in `before`")
    assert transitions[0].previous_units_per_point == pytest.approx(3_000_000.0)
    assert transitions[1].previous_units_per_point == pytest.approx(2_442_620.0)


def test_c2_a_pair_with_an_unusable_rate_is_dropped_without_dropping_the_rest(
        ns):
    """A pair whose rates are not both finite and positive describes no
    measurable transition, and skipping it must not stop the walk."""
    mrc = _mrc(ns)
    first = "2026-07-25T00:00:00+00:00"
    after = [
        _regime(effectiveFrom="2026-06-01T00:00:00+00:00",
                effectiveUntil=first, unitsPerPoint=0.0),
        _regime(effectiveFrom=first, effectiveUntil=BOUNDARY,
                unitsPerPoint=2_442_620.0),
        _regime(effectiveFrom=BOUNDARY, unitsPerPoint=1_685_000.0),
    ]
    transitions = mrc.detect_transitions(
        [], after, provider="claude", account_key="unattributed",
        detected_at=NOW.isoformat())
    assert [t.effective_from for t in transitions] == [BOUNDARY]


def test_c2_mixed_offset_spellings_are_one_transition(ns):
    """The store holds mixed UTC offset spellings, so the adjacency is over
    PARSED instants. Comparing the strings would miss the pair entirely."""
    mrc = _mrc(ns)
    after = [_regime(effectiveUntil="2026-08-25T00:00:00Z"),
             _regime(effectiveFrom="2026-08-25T02:00:00+02:00",
                     unitsPerPoint=1_685_000.0)]
    assert len(mrc.detect_transitions(
        [_regime()], after, provider="claude", account_key="unattributed",
        detected_at=NOW.isoformat())) == 1


def test_c2_enumerate_returns_every_qualified_pair_oldest_first(ns):
    """`enumerate_transitions` lists what IS persisted, with no freshness
    comparison — that is what makes recovery possible after the write."""
    mrc = _mrc(ns)
    first = "2026-08-01T00:00:00+00:00"
    second = "2026-08-25T00:00:00+00:00"
    regimes = [
        _regime(effectiveUntil=first, unitsPerPoint=3_000_000.0),
        _regime(effectiveFrom=first, effectiveUntil=second,
                unitsPerPoint=2_442_620.0),
        _regime(effectiveFrom=second, unitsPerPoint=1_665_000.0),
    ]
    got = mrc.enumerate_transitions(
        regimes, provider="claude", account_key="unattributed",
        detected_at=NOW.isoformat())
    assert [t.effective_from for t in got] == [first, second]
    assert all(t.withholding_status is None for t in got)


def test_c2_enumerate_applies_the_same_qualification_as_detection(ns):
    """One qualification, not two. A stale predecessor and an unusable rate
    must be excluded by BOTH, or recovery would record what detection
    refused."""
    mrc = _mrc(ns)
    boundary = BOUNDARY
    stale = [
        _regime(effectiveUntil=boundary, status="stale"),
        _regime(effectiveFrom=boundary, unitsPerPoint=1_665_000.0),
    ]
    unusable = [
        _regime(effectiveUntil=boundary, unitsPerPoint=0.0),
        _regime(effectiveFrom=boundary, unitsPerPoint=1_665_000.0),
    ]
    for regimes in (stale, unusable):
        assert mrc.enumerate_transitions(
            regimes, provider="claude", account_key="unattributed",
            detected_at=NOW.isoformat()) == ()
        assert mrc.detect_transitions(
            [], regimes, provider="claude", account_key="unattributed",
            detected_at=NOW.isoformat()) == ()


def test_c2_detection_subtracts_on_the_canonical_instant_not_the_spelling(ns):
    """The store holds mixed offset spellings of one instant, and the
    subtraction must resolve them to one key.

    BOTH orderings are asserted, and the pair is what makes the test
    discriminating. A descriptor's `effective_from` is already canonical, so
    comparing it against the raw `effectiveFrom` strings of the before state
    only diverges when the BEFORE side carries the non-UTC spelling — with the
    UTC spelling on that side the wrong comparison still matches and passes.
    The reverse ordering is what catches the other mistake, passing
    `_adjacent_pairs`' datetime keys through `_instant`: that returns None for
    a datetime, emptying the before set and re-emitting every transition on
    every run whichever way round the spellings fall.

    Neither mistake changes any other observable, so only this test catches
    them.
    """
    mrc = _mrc(ns)
    utc = "2026-08-25T00:00:00+00:00"
    offset = "2026-08-25T02:00:00+02:00"

    def _state(boundary):
        return [
            _regime(effectiveUntil=boundary),
            _regime(effectiveFrom=boundary, unitsPerPoint=1_665_000.0),
        ]

    for before_spelling, after_spelling in ((utc, offset), (offset, utc)):
        assert mrc.detect_transitions(
            _state(before_spelling), _state(after_spelling),
            provider="claude", account_key="unattributed",
            detected_at=NOW.isoformat()) == (), (
                f"the same instant spelled {before_spelling} before and "
                f"{after_spelling} after was treated as a fresh transition")


def test_c2_the_severity_is_explicit_and_never_threshold_derived(ns):
    """§6.1: a rate transition has no percentage threshold, so the three
    tiers come from the direction and size of the change itself."""
    mrc = _mrc(ns)
    assert mrc.transition_severity(2_000_000.0, 1_000_000.0) == "alarm"
    assert mrc.transition_severity(2_000_000.0, 1_900_000.0) == "warn"
    assert mrc.transition_severity(2_000_000.0, 1_999_000.0) == "info"
    assert mrc.transition_severity(2_000_000.0, 2_500_000.0) == "info", (
        "a MORE generous rate is information, not a warning")
    assert "threshold" not in mrc.alert_payload(_transition(ns)), (
        "a threshold key would make the dispatch glue re-derive severity")


# --------------------------------------------------------------------------
# The transaction (§6.5)
# --------------------------------------------------------------------------
def test_c2_the_event_is_recorded_through_the_sole_stats_writer(ns):
    _seed_journal()
    result = _record(ns, _transition(ns))
    assert result.ran
    conn = ns["open_db"]()
    try:
        assert _events(conn) == [("claude", "unattributed", BOUNDARY)]
    finally:
        conn.close()


def test_c2_latch_survives_a_stats_rebuild_from_the_journal_alone(ns):
    """The key comes from an UNJOURNALED file, so the payload must be
    self-sufficient. The calibration file is deleted before the rebuild."""
    import _cctally_journal as jr
    _seed_journal()
    _record(ns, _transition(ns))
    calibration = _cctally_core.APP_DIR / "quota-calibrations.json"
    calibration.write_text("{}", encoding="utf-8")
    calibration.unlink()
    assert not calibration.exists()

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    conn = ns["open_db"]()
    try:
        assert _events(conn) == [("claude", "unattributed", BOUNDARY)], (
            "the latch did not survive the rebuild, so the same transition "
            "would alert again")
    finally:
        conn.close()


def test_c2_a_rebuild_does_not_refire_the_notifier(ns, monkeypatch):
    """Invariant (iv): replay folds evt lines with NO `IngestContext`, so it
    is structurally unable to reach EITHER dispatch sink.

    #695 moved this family's payload from `ctx.pending_alerts` to
    `ctx.deferred_alerts`, which `cmd_quota` drains rather than step 6. The
    live recording below is therefore the positive control: it proves the
    fixture really does produce a notifiable transition, so the rebuild's
    silence on both channels afterwards is a fact about replay rather than
    about a fixture that never had anything to fire.
    """
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_journal()
    result = _record(ns, _transition(ns), notify=True)
    assert len(result.deferred_alerts) == 1, result.deferred_alerts
    assert dispatched == [], "step 6 dispatched a payload it must defer"

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert dispatched == [], (
        "the rebuild re-fired a notification for history")


def test_c2_a_second_recording_of_the_same_key_queues_nothing(ns, monkeypatch):
    """§6.5 step 5: a notification is queued only when the insert actually
    CREATED a row. `rowcount`, never `lastrowid`."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_journal()
    first = _record(ns, _transition(ns), notify=True)
    second = _record(ns, _transition(ns), notify=True)
    assert len(first.deferred_alerts) == 1, first.deferred_alerts
    assert second.deferred_alerts == [], second.deferred_alerts
    assert dispatched == [], "step 6 dispatched a payload it must defer"
    conn = ns["open_db"]()
    try:
        assert len(_events(conn)) == 1
    finally:
        conn.close()


def test_c2_the_latch_insert_reports_creation_not_mere_execution(ns):
    """§6.5 step 5's predicate, driven DIRECTLY at `_insert_meter_rate_change`.

    It used to be reachable through two `run_stats_ingest` calls for one key:
    the second insert was ignored, `rowcount` was 0, and no second
    notification was queued. #689 put a natural-key recheck in front of the
    emitter, so the second call now returns before the insert is reached and
    that route can no longer exercise the predicate at all. Asserting it here
    keeps `rowcount == 1` — rather than `lastrowid`, which is left over from a
    previous insert when `INSERT OR IGNORE` ignores, or the mere existence of
    a cursor — pinned by something.
    """
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        evt = _mrc_evt(ns, _transition(ns), created_at=NOW.isoformat())
        assert jr._insert_meter_rate_change(conn, evt) is True
        assert jr._insert_meter_rate_change(conn, evt) is False, (
            "an IGNORED insert was reported as a creation, which would queue "
            "a second notification for one transition")
        assert len(_events(conn)) == 1
    finally:
        conn.close()


def test_c2_a_different_effective_instant_is_a_different_transition(ns,
                                                                   monkeypatch):
    """The non-vacuity twin: the latch must not swallow a real second
    transition."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_journal()
    first = _record(ns, _transition(ns), notify=True)
    second = _record(
        ns, _transition(ns, effective_from="2026-09-08T00:00:00+00:00"),
        notify=True)
    assert len(first.deferred_alerts) == 1, first.deferred_alerts
    assert len(second.deferred_alerts) == 1, (
        "the latch swallowed a real second transition")
    assert dispatched == [], "step 6 dispatched a payload it must defer"


def test_c2_recording_never_queues_a_notification_without_the_toggle(
        ns, monkeypatch):
    """§6.2: recording is unconditional, push is not."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_journal()
    result = _record(ns, _transition(ns), notify=False)
    # #695 moved this family's dispatch out of the cycle, so `dispatched` is
    # now empty at EVERY toggle setting and asserting on it alone would hold
    # just as well with `notify=True`. The queue is what the toggle governs.
    assert result.deferred_alerts == [], (
        "the toggle is off, so nothing may be queued for dispatch")
    assert dispatched == []
    conn = ns["open_db"]()
    try:
        assert len(_events(conn)) == 1, (
            "the event must be recorded from the first upgrade whether or "
            "not notification is enabled")
    finally:
        conn.close()


def test_c2_a_malformed_payload_folds_to_a_no_op_rather_than_raising(ns):
    """A record that raised here would prefix-stop the whole fold forever."""
    import _cctally_journal as jr
    _seed_journal()
    conn = ns["open_db"]()
    try:
        jr._apply_meter_rate_change(conn, {"payload": {"provider": "claude"}})
        jr._apply_meter_rate_change(conn, {"payload": None})
        jr._apply_meter_rate_change(conn, {})
        assert _events(conn) == []
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The write boundary (#689 §6): classify before appending
# --------------------------------------------------------------------------
def test_c2_an_existing_row_short_circuits_before_any_append(ns):
    """Guard 1 (#689). Recovery re-offers persisted pairs, so the emitter must
    treat an existing physical latch as terminal BEFORE it builds or appends
    anything — otherwise a retry under a later command clock appends a
    byte-different line under an id the journal already holds."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        assert jr.record_meter_rate_change(
            ctx, t, notify=True,
            created_at=NOW.isoformat()).row_created is True
        before_lines = _journal_line_count(ns)
        emitted_before = ctx.events_emitted
        ctx.deferred_alerts.clear()
        assert jr.record_meter_rate_change(
            ctx, t, notify=True,
            created_at="2026-09-05T09:15:00+00:00").row_created is False
        assert _journal_line_count(ns) == before_lines, (
            "a retry appended a second line for an identity that already had "
            "a row")
        assert ctx.events_emitted == emitted_before
        assert ctx.deferred_alerts == []
    finally:
        conn.close()


def test_c2_a_row_with_no_effective_metadata_still_short_circuits(ns):
    """Guard 1's UNIQUE condition — the one the classifier cannot cover.

    Wherever effective metadata exists, the classifier already withholds the
    append, so disabling the natural-key recheck changes nothing there. The
    state it alone defends is a committed physical row with NO metadata, and
    that is not hypothetical: v1.104.0 shipped this family appending without
    `_record_new_effective_event`, so every rate change recorded by a released
    binary is in exactly this state. With the recheck gone, such an identity
    classifies as NEW and a second, byte-different line lands under an id the
    journal already holds — the divergent-hash quarantine candidate.
    """
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.execute(
            "DELETE FROM journal_effective_events WHERE event_id LIKE 'mrc:%'")
        lines = _journal_line_count(ns)
        assert jr.record_meter_rate_change(
            ctx, t, notify=True,
            created_at="2026-09-05T09:15:00+00:00").row_created is False
        assert _journal_line_count(ns) == lines, (
            "a second, byte-different line was appended for an identity that "
            "already had a row")
        assert len(_events(conn)) == 1
        assert ctx.deferred_alerts == [], (
            "an already-recorded identity re-fired a notification")
        assert ctx.conflicts_dropped == [], (
            "the recheck must return before anything is built or classified")
    finally:
        conn.close()


def test_c2_a_duplicate_materializes_the_row_and_does_not_append(ns):
    """#689 review P1. An earlier draft appended here and relied on step-4a
    replay to insert the row. Replay does NOT: `_preflight_live_events` omits
    a same-rev/same-hash event from `to_apply`, and step 4a applies only what
    `_record_live_effective_event` returns True for, which is NEW alone. So
    the emitter materializes the row itself."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.execute("DELETE FROM meter_rate_change_events")
        lines = _journal_line_count(ns)
        assert jr.record_meter_rate_change(
            ctx, t, notify=True,
            created_at=NOW.isoformat()).row_created is False
        assert _journal_line_count(ns) == lines, "the duplicate was appended"
        assert len(_events(conn)) == 1, "the duplicate did not restore the row"
        assert ctx.deferred_alerts == [], (
            "a duplicate re-fired a notification for history")
    finally:
        conn.close()


def test_c2_a_conflict_materializes_the_prior_event_not_the_candidate(ns):
    """The row must converge to what the JOURNAL holds, never to the rejected
    candidate. `_converge_row_from_effective` cannot do it for this family
    because its `_EVT_SPECS` entry has `table=None`."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.execute("DELETE FROM meter_rate_change_events")
        lines = _journal_line_count(ns)
        later = "2026-09-05T09:15:00+00:00"
        assert jr.record_meter_rate_change(
            ctx, t, notify=True, created_at=later).row_created is False
        assert _journal_line_count(ns) == lines, "a divergent line was appended"
        rows = _full_events(conn)
        assert len(rows) == 1
        assert later not in str(rows[0]), (
            "the row was materialized from the REJECTED candidate rather than "
            "from the prior journaled event")
        assert ctx.conflicts_dropped, "the dropped conflict was not recorded"
        assert ctx.deferred_alerts == []
    finally:
        conn.close()


def test_c2_a_tombstoned_prior_materializes_nothing(ns):
    """A tombstone is a TERMINAL negative latch. A correction saying this
    identity should have no row must not be undone by materializing one."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.execute("DELETE FROM meter_rate_change_events")
        conn.execute(
            "UPDATE journal_effective_events SET status = 'tombstone', "
            "event_json = NULL WHERE event_id LIKE 'mrc:%'")
        assert jr.record_meter_rate_change(
            ctx, t, notify=True,
            created_at="2026-09-05T09:15:00+00:00").row_created is False
        assert _events(conn) == [], "a tombstoned identity was resurrected"
    finally:
        conn.close()


def test_c2_a_retry_at_a_later_clock_leaves_the_journal_bytes_unchanged(ns):
    """Spec test 6. The whole hazard is that the payload includes the command
    clock, so a re-emission is not byte-identical. Assert the BYTES, not just
    the line count: a second line under the same id with different timestamps
    is the divergent-hash quarantine candidate #688 kept the payload frozen to
    avoid."""
    import _cctally_core
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        segments = sorted((_cctally_core.APP_DIR / "journal").glob("*.jsonl"))
        before = {p: p.read_bytes() for p in segments}
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at="2026-09-05T09:15:00+00:00")
        after = {p: p.read_bytes() for p in
                 sorted((_cctally_core.APP_DIR / "journal").glob("*.jsonl"))}
        assert after == before, (
            "a retry under a later command clock changed the journal bytes")
        assert ctx.conflicts_dropped == [], (
            "the retry reached the classifier and was withheld as a conflict; "
            "the natural-key recheck must return before anything is built")
        detected, created = conn.execute(
            "SELECT detected_at_utc, created_at_utc "
            "FROM meter_rate_change_events").fetchone()
        assert created == NOW.isoformat(), (
            "the retry's later clock overwrote the first event's created_at")
        assert detected == NOW.isoformat(), (
            "the retry's later clock overwrote the first event's detected_at")
    finally:
        conn.close()


def test_c2_a_new_emission_writes_its_live_effective_metadata(ns, capsys):
    """Spec test 9, BOTH clauses. Without the metadata write the live selector
    does not know about an already-appended event, classification depends on a
    later replay, and a divergent re-emission can still be classified NEW.

    The second clause is the consequence the write exists for: the FOLLOWING
    cycle reads the same line out of the journal and must classify it as a
    DUPLICATE — neither re-applying it nor quarantining it. Both halves are
    asserted, because they fail to different mutations. Non-application is
    asserted by deleting the physical row first, since
    `_apply_meter_rate_change` would restore it. Duplicate-rather-than-
    conflict is asserted on the cycle's own conflict count, which is what a
    change writing metadata under a different hash or status would break
    while leaving the first clause and the row assertion untouched.
    """
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        jr.record_meter_rate_change(
            ctx, _transition(ns), notify=False, created_at=NOW.isoformat())
        row = conn.execute(
            "SELECT status FROM journal_effective_events "
            "WHERE event_id LIKE 'mrc:%'").fetchone()
        assert row is not None, (
            "the emission wrote no effective metadata, so the next divergent "
            "re-emission would classify as NEW")
        assert str(row[0]) == "active"
        # The cursor has not advanced — this context was driven directly — so
        # the next real cycle re-reads the appended line from offset zero.
        conn.execute("DELETE FROM meter_rate_change_events")
        conn.commit()
    finally:
        conn.close()

    capsys.readouterr()
    result = jr.run_stats_ingest(mode="authoritative")
    err = capsys.readouterr().err
    assert result.conflicts_dropped == 0, (
        "the following cycle quarantined this process's own emission, so the "
        "metadata written does not describe the line appended")
    assert "quarantined a divergent journal event" not in err

    conn = ns["open_db"]()
    try:
        assert _events(conn) == [], (
            "the following cycle re-applied the line this process appended; "
            "step 4a must classify it as a duplicate")
        rev, status = conn.execute(
            "SELECT rev, status FROM journal_effective_events "
            "WHERE event_id LIKE 'mrc:%'").fetchone()
        assert (int(rev), str(status)) == (0, "active"), (
            "the following cycle rewrote the metadata for its own emission")
    finally:
        conn.close()


def test_c2_the_dropped_conflict_line_reports_an_insert_that_materialized_nothing(
        ns, capsys):
    """Acceptance 6, the third case: an ACTIVE prior that survives validation
    and still materializes no row.

    `_insert_meter_rate_change` returns False when `_meter_rate_change_row`
    cannot normalise the payload — a missing `provider` or `effective_from`,
    or a non-numeric rate. Claiming `CONVERGE_APPLIED` because the call was
    made would print "converged the row from the journaled event" over an
    insert that inserted nothing. The retained record is rewritten together
    with its `content_hash`, because `_effective_event_for_convergence`
    validates that hash and would otherwise raise before the insert is
    reached.
    """
    import _cctally_journal as jr
    import _lib_journal
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.execute("DELETE FROM meter_rate_change_events")
        (event_id, event_json), = conn.execute(
            "SELECT event_id, event_json FROM journal_effective_events "
            "WHERE event_id LIKE 'mrc:%'").fetchall()
        record = _lib_journal.decode_line(event_json.encode("utf-8"))
        record["payload"]["previous_units_per_point"] = "not-a-number"
        conn.execute(
            "UPDATE journal_effective_events SET event_json = ?, "
            "content_hash = ? WHERE event_id = ?",
            (_lib_journal.encode_line(record).decode("utf-8").rstrip("\n"),
             _lib_journal._sha256_canonical(record), event_id))
        capsys.readouterr()
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at="2026-09-05T09:15:00+00:00")
        err = capsys.readouterr().err
        assert "withheld a divergent emission" in err
        assert "converged the row from the journaled event" not in err, (
            "the line claimed a convergence over an insert that materialized "
            "nothing")
        assert "no row to converge" in err
        assert _events(conn) == []
    finally:
        conn.close()


def test_c2_the_dropped_conflict_line_survives_a_convergence_that_raises(
        ns, capsys):
    """The diagnostic must not be lost on the path that most needs one.

    Before the record/report split the whole `_record_dropped_conflict` call
    ran ahead of convergence, so a convergence that raised still left a line
    on stderr. Rendering the outcome only after convergence resolved would
    drop the line exactly there — for the two pre-existing emit paths as well
    as this one — which is why `_converge_and_report` prints from a `finally`.
    The exception still propagates.
    """
    import _cctally_journal as jr
    import _lib_journal
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.execute("DELETE FROM meter_rate_change_events")
        # ACTIVE with no retained record: `_effective_event_for_convergence`
        # fails closed and raises rather than returning something to stamp.
        conn.execute(
            "UPDATE journal_effective_events SET event_json = NULL "
            "WHERE event_id LIKE 'mrc:%'")
        capsys.readouterr()
        with pytest.raises(_lib_journal.JournalProtocolError):
            jr.record_meter_rate_change(
                ctx, t, notify=False,
                created_at="2026-09-05T09:15:00+00:00")
        err = capsys.readouterr().err
        assert "withheld a divergent emission" in err, (
            "the diagnostic was lost because convergence raised")
        assert "convergence failed and the row was left unchanged" in err
        assert "converged the row from the journaled event" not in err, (
            "the line claimed a convergence that raised instead")
    finally:
        conn.close()


def test_c2_the_dropped_conflict_line_states_the_convergence_it_performed(
        ns, capsys):
    """Acceptance 6, the TRUE half.

    The plan's draft required this path to claim no convergence, on the
    premise that `_converge_row_from_effective` returns CONVERGE_DROPPED for a
    `table=None` family. That premise is about the generic helper, and this
    emitter no longer uses it: it materializes the row from the prior
    journaled event itself, so on an ACTIVE prior a convergence really does
    happen and saying so is true. The false claim is the tombstoned prior,
    which the twin below owns.
    """
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.execute("DELETE FROM meter_rate_change_events")
        capsys.readouterr()
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at="2026-09-05T09:15:00+00:00")
        err = capsys.readouterr().err
        assert "withheld a divergent emission" in err
        assert "converged the row from the journaled event" in err
        assert len(_events(conn)) == 1, (
            "the line claimed a convergence and no row was materialized")
    finally:
        conn.close()


def test_c2_the_dropped_conflict_line_does_not_claim_an_absent_convergence(
        ns, capsys):
    """Acceptance 6, the FALSE half — the assertion the fixed wording failed.

    A tombstoned prior is a terminal negative latch, so nothing is
    materialized. The old line said "converged the row from the journaled
    event" unconditionally, which was untrue exactly here.
    """
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.execute("DELETE FROM meter_rate_change_events")
        conn.execute(
            "UPDATE journal_effective_events SET status = 'tombstone', "
            "event_json = NULL WHERE event_id LIKE 'mrc:%'")
        capsys.readouterr()
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at="2026-09-05T09:15:00+00:00")
        err = capsys.readouterr().err
        assert "withheld a divergent emission" in err
        assert "converged the row from the journaled event" not in err, (
            "the diagnostic claimed a convergence that did not happen")
        assert "no row to converge" in err
        assert _events(conn) == []
    finally:
        conn.close()


def test_c2_a_dropped_conflict_is_recorded_even_when_convergence_raises(ns):
    """Spec test 8's second half. `_record_dropped_conflict` appends to
    `ctx.conflicts_dropped` BEFORE convergence is attempted, so a convergence
    that fails closed still leaves the withheld emission counted. Moving the
    whole call after convergence would have lost the in-memory record too, not
    merely the stderr line."""
    import _cctally_journal as jr
    import _lib_journal
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.execute("DELETE FROM meter_rate_change_events")
        # ACTIVE with no retained record: `_effective_event_for_convergence`
        # fails closed and raises rather than returning something to stamp.
        conn.execute(
            "UPDATE journal_effective_events SET event_json = NULL "
            "WHERE event_id LIKE 'mrc:%'")
        with pytest.raises(_lib_journal.JournalProtocolError):
            jr.record_meter_rate_change(
                ctx, t, notify=False,
                created_at="2026-09-05T09:15:00+00:00")
        assert len(ctx.conflicts_dropped) == 1, (
            "the withheld emission was not counted because convergence raised")
        assert _events(conn) == []
    finally:
        conn.close()


def test_c2_failure_b_self_heals_when_the_cycle_aborts_after_the_append(
        ns, monkeypatch):
    """A REGRESSION GUARD, not evidence of the #689 fix.

    This is Failure B from the spec's §1.1 taxonomy: the line is appended and
    the transaction then rolls back. A rollback cannot unwrite an append-only
    line and the cursor did not advance, so the next fold of any kind re-reads
    that line and recreates the row. That self-healing already worked before
    #689, so this test PASSES against the unfixed code and proves nothing
    about the defect. It is kept so that the recovery change does not silently
    break the path, and it is labelled here so that a later reader does not
    mistake it for coverage of #689 — which is Failure A, the ingest failing
    BEFORE the append, and which does not self-heal.
    """
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_journal()
    real_write_cursor = jr._write_cursor

    def _abort(*_a, **_k):
        raise sqlite3.OperationalError("aborted after the append")

    monkeypatch.setattr(jr, "_write_cursor", _abort)
    with pytest.raises(sqlite3.OperationalError):
        _record(ns, _transition(ns), notify=True)
    monkeypatch.setattr(jr, "_write_cursor", real_write_cursor)

    conn = ns["open_db"]()
    try:
        assert _events(conn) == [], (
            "the rolled-back transaction left its row behind")
    finally:
        conn.close()
    assert dispatched == [], "an aborted cycle dispatched its notification"

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    conn = ns["open_db"]()
    try:
        assert _events(conn) == [("claude", "unattributed", BOUNDARY)], (
            "the orphan event did not restore the row")
    finally:
        conn.close()
    assert dispatched == [], (
        "the replay dispatched a notification, which it has no ctx to do")

    # Positive control. Since #695 moved this family's dispatch out of the
    # cycle, both `dispatched == []` assertions above hold unconditionally,
    # including for a fixture that never reached the code under test. This
    # proves the observation channel is live and that a queued notification
    # here is visible when one is genuinely produced.
    fresh = _record(
        ns, _transition(ns, effective_from="2026-09-08T00:00:00+00:00"),
        notify=True)
    assert len(fresh.deferred_alerts) == 1, (
        "the fixture cannot observe a queued notification at all, so the "
        "assertions above are vacuous")


# --------------------------------------------------------------------------
# The presence lookup (#689 §5.2)
# --------------------------------------------------------------------------
def test_c2_an_unanswerable_store_suppresses_every_candidate(ns, monkeypatch):
    """Unknown is treated as PRESENT, never absent. A store that cannot answer
    must not drive an ingest attempt on every run forever — which is what
    returning the candidates as unrecorded would do."""
    import _cctally_core
    from _cctally_db import StatsRebuildDeferred
    glue = ns["_load_sibling"]("_cctally_quota_model")
    candidates = (_transition(ns),)

    def _deferred():
        raise StatsRebuildDeferred("spawned")

    monkeypatch.setattr(_cctally_core, "open_db", _deferred)
    assert glue.unrecorded_rate_change_transitions(candidates) == ()

    def _no_table():
        raise sqlite3.OperationalError(
            "no such table: meter_rate_change_events")

    monkeypatch.setattr(_cctally_core, "open_db", _no_table)
    assert glue.unrecorded_rate_change_transitions(candidates) == ()


def test_c2_an_answerable_store_reports_an_absent_candidate(ns):
    """The non-vacuity twin of the suppression above: a store that CAN answer
    must report a candidate with neither a row nor any metadata as absent, or
    the helper would suppress everything and recovery would never happen."""
    glue = ns["_load_sibling"]("_cctally_quota_model")
    _seed_journal()
    t = _transition(ns)
    assert glue.unrecorded_rate_change_transitions((t,)) == (t,)


def test_c2_a_terminal_latch_suppresses_recovery(ns):
    """A tombstone or a higher revision is terminal. Without this axis the
    lookup re-selects the key every run, `_classify_live_effective_event`
    raises a non-recovery-eligible `CorrectionRebuildRequired`, and the
    command prints its failure line forever."""
    import _cctally_journal as jr
    glue = ns["_load_sibling"]("_cctally_quota_model")
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.execute("DELETE FROM meter_rate_change_events")
        conn.commit()
        # Active at revision 0 with no row is NOT terminal: the emitter
        # materializes it from the identical event.
        assert glue.unrecorded_rate_change_transitions((t,)) == (t,)
        conn.execute("UPDATE journal_effective_events SET status = 'tombstone'"
                     " WHERE event_id LIKE 'mrc:%'")
        conn.commit()
        assert glue.unrecorded_rate_change_transitions((t,)) == ()
        conn.execute(
            "UPDATE journal_effective_events SET status = 'active', rev = 1"
            " WHERE event_id LIKE 'mrc:%'")
        conn.commit()
        assert glue.unrecorded_rate_change_transitions((t,)) == ()
    finally:
        conn.close()


def test_c2_a_recorded_row_suppresses_recovery(ns):
    """The first axis on its own. The row is the ordinary latch, and it is
    what stops a recorded transition from paying for an ingest on every run.
    """
    import _cctally_journal as jr
    glue = ns["_load_sibling"]("_cctally_quota_model")
    conn = ns["open_db"]()
    try:
        ctx = _ingest_context(ns, conn)
        t = _transition(ns)
        jr.record_meter_rate_change(
            ctx, t, notify=False, created_at=NOW.isoformat())
        conn.commit()
        assert glue.unrecorded_rate_change_transitions((t,)) == ()
        other = _transition(ns, effective_from="2026-09-08T00:00:00+00:00")
        assert glue.unrecorded_rate_change_transitions((t, other)) == (other,), (
            "the lookup suppressed a key it had never recorded")
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The persistence trigger (§6.5 steps 1-2)
# --------------------------------------------------------------------------
def test_c2_the_descriptor_is_returned_rather_than_acted_on_under_the_lock(
        ns, monkeypatch):
    """Step 2 requires the calibration leaf lock to be released before any
    stats lock is taken, and `persist_and_detect` IS that lock's whole extent.

    Asserted behaviourally, which the earlier version of this test claimed in
    its docstring and did not do: it ran a source-text check for the literal
    strings `run_stats_ingest` and `open_db`, which is a name-channel
    assertion blind to a transitive call and to any indirection that does not
    spell the literal.

    Here the real `calibration_lock`, `stored_regimes` and `detect_transitions`
    run against a state pinned to a confirmed transition. The reducer and the
    writer are stubbed because building a trustworthy `analysis` is a
    different subject; what this test owns is the control flow after the
    reduction, which is that a descriptor comes BACK and the stats table is
    still empty.
    """
    glue = ns["_load_sibling"]("_cctally_quota_model")
    before_regimes = [_regime(effectiveUntil=BOUNDARY, status="ok")]
    after_regimes = before_regimes + [
        _regime(effectiveFrom=BOUNDARY, unitsPerPoint=1_685_000.0)]

    def _state(regimes):
        return {"version": 1,
                "accounts": {"*": {"regimes": [dict(r) for r in regimes]}}}

    monkeypatch.setattr(
        glue, "load_calibrations",
        lambda **_k: glue.LoadedCalibrations(_state(before_regimes)))
    monkeypatch.setattr(
        glue, "reduce_state",
        lambda _stored, _analysis, _mode: _state(after_regimes))
    saved: list = []
    monkeypatch.setattr(glue, "save_calibrations", saved.append)

    mode = glue.PersistMode(
        kind="automatic", account_key=None, now=NOW, fingerprint="FP",
        regime_start=NOW)
    quarantined, fresh, candidates = glue.persist_and_detect(object(), mode)

    assert quarantined is None
    assert saved, "the reduced state was never written"
    mrc = _mrc(ns)
    assert len(fresh) == 1, fresh
    assert isinstance(fresh[0], mrc.RateChangeTransition), fresh
    assert fresh[0].effective_from == BOUNDARY
    # #689: the persisted enumeration is returned too, and it is still only
    # RETURNED — nothing below is recorded from inside the lock.
    assert [c.effective_from for c in candidates] == [BOUNDARY], candidates

    conn = ns["open_db"]()
    try:
        assert _events(conn) == [], (
            "`persist_and_detect` wrote the event itself, so the stats lock "
            "was taken from inside the calibration lock and the estate's "
            "lock order is inverted")
    finally:
        conn.close()


def test_c2_a_deferred_stats_rebuild_is_absorbed_rather_than_raised(ns,
                                                                   monkeypatch):
    """The recording path must never turn `cctally quota` into an error: the
    calibration is already persisted and the transition is re-derivable."""
    glue = ns["_load_sibling"]("_cctally_quota_model")
    import _cctally_db
    import _cctally_journal as jr

    def _deferred(**_kwargs):
        raise _cctally_db.StatsEpochRebuildDeferred("spawned")

    monkeypatch.setattr(jr, "run_stats_ingest", _deferred)
    assert glue.record_rate_change_transition(
        _transition(ns), now=NOW) is None


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_c2_an_interrupt_is_not_swallowed_by_the_recording_path(
        ns, monkeypatch, signal):
    """`except BaseException` swallowed `KeyboardInterrupt` and `SystemExit`
    along with the deferral signal it was widened for, so a Ctrl-C during the
    ingest printed "could not record the metering-rate change" and the
    command carried on. The two are different kinds of event: one says this
    store is busy, the other says this process is ending."""
    glue = ns["_load_sibling"]("_cctally_quota_model")
    import _cctally_journal as jr

    def _interrupt(**_kwargs):
        raise signal()

    monkeypatch.setattr(jr, "run_stats_ingest", _interrupt)
    with pytest.raises(signal):
        glue.record_rate_change_transition(_transition(ns), now=NOW)


def test_c2_an_ordinary_store_failure_is_still_absorbed(ns, monkeypatch):
    """The non-vacuity twin: narrowing the catch must not start propagating
    the store errors the docstring promises to absorb."""
    glue = ns["_load_sibling"]("_cctally_quota_model")
    import _cctally_journal as jr

    def _boom(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jr, "run_stats_ingest", _boom)
    assert glue.record_rate_change_transition(
        _transition(ns), now=NOW) is None


def test_c2_the_alert_payload_carries_the_family_not_a_registry_axis(ns):
    """§6.1: `alert_row_owner` raises on a seventh AXIS_REGISTRY member so
    that adding one without deciding its ownership fails a test rather than
    shipping an invisible row. This family is not one."""
    mrc = _mrc(ns)
    payload = mrc.alert_payload(_transition(ns))
    assert payload["axis"] == "meter_rate_change"
    assert payload["severity"] == "alarm"
    axes = ns["AXIS_REGISTRY"]
    assert "meter_rate_change" not in {a.id for a in axes}


# --- Every return path of `persist_and_detect` yields an ITERABLE ---------
#
# The #661 S2 review found the pre-F9 sentinel still in place: an unreadable
# calibration file returned `None, None`, while `cmd_quota` does
# `for transition in transitions:` over the second element. `load_calibrations`
# sets `unreadable=True` on any `OSError` — a permissions problem, an I/O
# error, or a concurrent quarantine rename removing the primary name between
# `exists()` and `read_text()`, which is the race this session's own status
# line port is written to survive. No test reached that path, so every green
# run stayed green over a `TypeError`.

def test_c2_an_unreadable_calibration_yields_an_iterable_second_element(
        ns, monkeypatch):
    glue = ns["_load_sibling"]("_cctally_quota_model")
    monkeypatch.setattr(
        glue, "load_calibrations",
        lambda **_k: glue.LoadedCalibrations(
            glue.empty_calibration_state(), unreadable=True))
    mode = glue.PersistMode(
        kind="automatic", account_key=None, now=NOW, fingerprint="FP",
        regime_start=NOW)
    quarantined, fresh, candidates = glue.persist_and_detect(object(), mode)
    assert quarantined is None
    # The literal consumer, spelled the way `cmd_quota` spells it — for BOTH
    # descriptor collections, because `cmd_quota` iterates both (#689).
    assert [t for t in fresh] == []
    assert [t for t in candidates] == []


def test_c2_an_unreadable_calibration_writes_nothing(ns, monkeypatch):
    """The non-vacuity twin: the early return must still be an early return.

    An iterable second element bought by removing the guard would pass the
    test above and start reducing state over an empty read, which is the
    write this refusal exists to prevent.
    """
    glue = ns["_load_sibling"]("_cctally_quota_model")
    monkeypatch.setattr(
        glue, "load_calibrations",
        lambda **_k: glue.LoadedCalibrations(
            glue.empty_calibration_state(), unreadable=True))
    saved: list = []
    monkeypatch.setattr(glue, "save_calibrations", saved.append)

    def _must_not_run(*_a, **_k):
        raise AssertionError("reduce_state ran over an unreadable file")

    monkeypatch.setattr(glue, "reduce_state", _must_not_run)
    mode = glue.PersistMode(
        kind="automatic", account_key=None, now=NOW, fingerprint="FP",
        regime_start=NOW)
    glue.persist_and_detect(object(), mode)
    assert saved == []


def test_c2_no_return_path_of_persist_and_detect_yields_a_bare_none():
    """Structural, and deliberately not parametrized over the paths a test
    happens to reach.

    A behavioural test proves the ONE path it drives. This reads every
    `return` in the function and refuses a literal `None` in either
    descriptor position, so a future early return added beside the unreadable
    one fails here rather than at a user's terminal. Both positions are
    checked because `cmd_quota` iterates both (#689): the fresh descriptors
    and the persisted candidates.
    """
    import ast
    import pathlib
    source = (pathlib.Path(_cctally_core.__file__).resolve().parent
              / "_cctally_quota_model.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "persist_and_detect")
    returns = [n for n in ast.walk(target) if isinstance(n, ast.Return)]
    assert returns, "persist_and_detect has no return statement"
    for node in returns:
        assert isinstance(node.value, ast.Tuple), (
            f"line {node.lineno}: persist_and_detect returns a non-tuple")
        assert len(node.value.elts) == 3, (
            f"line {node.lineno}: persist_and_detect returns "
            f"{len(node.value.elts)} values, not 3")
        for position, element in enumerate(node.value.elts[1:], start=2):
            assert not (isinstance(element, ast.Constant)
                        and element.value is None), (
                f"line {node.lineno}: persist_and_detect returns None in "
                f"position {position}; cmd_quota iterates it")


# --------------------------------------------------------------------------
# #695 — the emitter's result, the owed branch, and deferred dispatch
# --------------------------------------------------------------------------
def test_c5_a_new_recording_reports_created_and_decided(ns, monkeypatch):
    """The ordinary path: the row is created and this call owns the decision."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_journal()
    result = _record(ns, _transition(ns), notify=True)
    assert result.meter_rate_change_result.row_created is True
    assert result.meter_rate_change_result.notification_decided is True
    assert result.meter_rate_change_result.notification_queued is True


def test_c5_the_payload_is_deferred_and_step_six_does_not_dispatch_it(
        ns, monkeypatch):
    """#695: this family's dispatch is returned to the command, so the ledger
    can gate it. Invariant (iv) is untouched — the payload still originates in
    the live context, post-commit."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_journal()
    result = _record(ns, _transition(ns), notify=True)
    assert dispatched == [], "step 6 dispatched a payload it must defer"
    assert result.alerts == []
    assert len(result.deferred_alerts) == 1
    assert result.deferred_alerts[0]["axis"] == "meter_rate_change"


def test_c5_an_ordinary_duplicate_decides_nothing(ns, monkeypatch):
    """Without an explicit owed intent, an existing row stays silent."""
    import _cctally_journal as jr
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", lambda _a: None)
    _seed_journal()
    _record(ns, _transition(ns), notify=True)
    again = _record(ns, _transition(ns), notify=True)
    assert again.meter_rate_change_result.row_created is False
    assert again.meter_rate_change_result.notification_decided is False
    assert again.deferred_alerts == []


def test_c5_an_owed_intent_queues_from_the_durable_row(ns, monkeypatch):
    """The #695 repair: the row exists, the notification does not."""
    import _cctally_journal as jr
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", lambda _a: None)
    _seed_journal()
    _record(ns, _transition(ns), notify=False)
    owed = _record(ns, _transition(ns), notify=True, notification_owed=True)
    assert owed.meter_rate_change_result.row_created is False
    assert owed.meter_rate_change_result.notification_queued is True
    assert owed.meter_rate_change_result.notification_decided is True
    payload = owed.deferred_alerts[0]
    assert payload["severity"] == "alarm"
    assert payload["withholding_status"] is None, (
        "a swept alert must not claim a disclosure it never observed")


def test_c5_the_owed_payload_is_rebuilt_from_the_row_not_the_descriptor(
        ns, monkeypatch):
    """The sweep's descriptor carries placeholder rates and an `info`
    severity, because only the IDENTITY is known before the row is read. A
    payload built from that descriptor would publish zeros under the wrong
    severity, so the emitter reads the durable row instead."""
    import _cctally_journal as jr
    mrc = _mrc(ns)
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", lambda _a: None)
    _seed_journal()
    _record(ns, _transition(ns), notify=False)
    placeholder = _transition(
        ns, previous_units_per_point=0.0, new_units_per_point=0.0,
        severity=mrc.SEVERITY_INFO)
    owed = _record(ns, placeholder, notify=True, notification_owed=True)
    payload = owed.deferred_alerts[0]
    assert payload["severity"] == "alarm", (
        "the placeholder severity reached the wire")
    assert payload["previous_units_per_point"] == 2_442_620.0
    assert payload["new_units_per_point"] == 1_685_000.0


def test_c5_an_owed_intent_with_notifications_off_still_decides(
        ns, monkeypatch):
    """Deciding NOT to notify is a decision. Leaving it undecided would make
    the identity owed forever and fire it the moment the toggle flips."""
    import _cctally_journal as jr
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", lambda _a: None)
    _seed_journal()
    _record(ns, _transition(ns), notify=False)
    owed = _record(ns, _transition(ns), notify=False, notification_owed=True)
    assert owed.meter_rate_change_result.notification_queued is False
    assert owed.meter_rate_change_result.notification_decided is True
    assert owed.deferred_alerts == []


def test_c5_a_cycle_with_no_descriptor_carries_no_result(ns, monkeypatch):
    """Every other cycle is byte-unchanged: both new fields stay at their
    defaults, so no other family's behaviour moves."""
    import _cctally_journal as jr
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", lambda _a: None)
    _seed_journal()
    result = jr.run_stats_ingest(mode="authoritative")
    assert result.meter_rate_change_result is None
    assert result.deferred_alerts == []
