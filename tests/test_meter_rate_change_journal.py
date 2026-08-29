"""#661 S2 Task C2 — the rate-change journal event and its transaction (§6.5).

`_apply_quota_threshold_event` only FOLDS an already-journaled event; it does
not claim a latch, append or dispatch. `run_stats_ingest` is the sole stats
writer and it enforces journal-first, then commit, then notify. So this family
is journaled the same way: the descriptor is decided under the calibration
file's leaf lock (§6.5 step 1), that lock is released before any stats lock
(step 2), the descriptor is passed through `run_stats_ingest` (step 3), the
event is appended and applied inside the stats transaction (step 4), a
notification is queued only when the insert actually created a row (step 5),
and dispatch happens after the commit (step 6).

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


def _record(ns, transition, *, notify=False):
    import _cctally_journal as jr
    return jr.run_stats_ingest(
        mode="authoritative",
        meter_rate_change={"transition": transition, "notify": notify,
                           "created_at": NOW.isoformat()})


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
    is structurally unable to reach the dispatch sink."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_journal()
    _record(ns, _transition(ns), notify=True)
    before = len(dispatched)
    assert before == 1, dispatched

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert len(dispatched) == before, (
        "the rebuild re-fired a notification for history")


def test_c2_a_second_recording_of_the_same_key_queues_nothing(ns, monkeypatch):
    """§6.5 step 5: a notification is queued only when the insert actually
    CREATED a row. `rowcount`, never `lastrowid`."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_journal()
    _record(ns, _transition(ns), notify=True)
    _record(ns, _transition(ns), notify=True)
    assert len(dispatched) == 1, dispatched
    conn = ns["open_db"]()
    try:
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
    _record(ns, _transition(ns), notify=True)
    _record(ns, _transition(ns, effective_from="2026-09-08T00:00:00+00:00"),
            notify=True)
    assert len(dispatched) == 2, dispatched


def test_c2_recording_never_queues_a_notification_without_the_toggle(
        ns, monkeypatch):
    """§6.2: recording is unconditional, push is not."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_journal()
    _record(ns, _transition(ns), notify=False)
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
    quarantined, transitions = glue.persist_and_detect(object(), mode)

    assert quarantined is None
    assert saved, "the reduced state was never written"
    mrc = _mrc(ns)
    assert len(transitions) == 1, transitions
    assert isinstance(transitions[0], mrc.RateChangeTransition), transitions
    assert transitions[0].effective_from == BOUNDARY

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
        _transition(ns), now=NOW) is False


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
        _transition(ns), now=NOW) is False


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
    quarantined, transitions = glue.persist_and_detect(object(), mode)
    assert quarantined is None
    # The literal consumer, spelled the way `cmd_quota` spells it.
    assert [t for t in transitions] == []


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
    `return` in the function and refuses a second element that is the literal
    `None`, so a future early return added beside the unreadable one fails
    here rather than at a user's terminal.
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
        assert len(node.value.elts) == 2, (
            f"line {node.lineno}: persist_and_detect returns "
            f"{len(node.value.elts)} values, not 2")
        second = node.value.elts[1]
        assert not (isinstance(second, ast.Constant) and second.value is None), (
            f"line {node.lineno}: the second element is a bare None, and "
            f"`cmd_quota` iterates it")
