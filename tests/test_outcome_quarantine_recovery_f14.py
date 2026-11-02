"""#948 — per-axis automatic recovery from the outcome quarantine through the
real writers (spec 6.4, 6.6–6.10; T2 and T6 of spec 10). Rewritten from the
#825/#832 F14 certificate cases.

Recovery of one (account, axis) needs, in journal order after the trigger:
an `outcome_authority_completion` the authoritative OAuth writer appends once
its reconciliation committed (coverage), a qualifying first entry of a fresh
period (the witness), and the `outcome_axis_release` the writer of that
witness appends in the same selected-state critical section. Outcomes and
drop consensus of that axis open at the release position, in live ingest and
in every replay.

(a) A full recovery mints exactly what the no-exclusion reference mints, and
    the state is identical after ingest, rebuild and rederive (T6: the A1
    excluded-only schedule recovers outcomes and drops at one position).
(b) No completed authority, an incomplete reconciliation, an implausible
    OAuth reading, a stale witness and a renewed trigger release nothing.
(c) A weekly-only OAuth reading covers weekly only; a later two-axis reading
    supplements five-hour, which recovers on its own fresh period.
(d) A crash between the witness and its release is resumed by the next
    writer before its own append.
(e) Revocation after a release: a backdated reading, a renewed trigger, a
    legacy spool change, a manual `record-credit`, a real `db rederive`
    correction and a non-inert fallback.

The witness predicates themselves (each clause, capture order, X7, C7,
retained lows, reviewed holds) are pinned in tests/test_outcome_quarantine_kernel.py.
"""
import json

import pytest

import _outcome_quarantine_trace as T

E0 = "2026-10-12T12:00:00Z"
F0 = "2026-10-09T14:00:00Z"
W_END = "2026-10-19T13:00:00Z"
W_FIVE = "2026-10-12T18:00:00Z"
WEEKLY, FIVE = "sevenDay", "fiveHour"


def cur(at, weekly, *, end=W_END, five=None, five_end=W_FIVE, account="A",
        **extra):
    values = dict(at=at, weekly=weekly, week_end=end, account=account, **extra)
    if five is not None:
        values.update(five=five, five_end=five_end)
    return ("current", values)


def oauth(at, weekly, *, end=E0, five=None, five_end=F0, account="A", **extra):
    values = dict(at=at, weekly=weekly, week_end=end, account=account, **extra)
    if five is not None:
        values.update(five=five, five_end=five_end)
    return ("oauth", values)


PREFIX = [cur("2026-10-09T10:00:00Z", 30, end=E0, five=40, five_end=F0)]
TRIGGER = ("legacy", dict(at="2026-10-09T10:05:00Z", weekly=50, week_end=E0,
                          five=60, five_end=F0, account="B"))
#: The automatic OAuth reconciliation after the trigger: covers both axes.
COVER = oauth("2026-10-09T10:10:00Z", 31, five=41)
WITNESS = cur("2026-10-12T13:00:00Z", 20, five=70)
#: Every automatic outcome class after the witness, with a held row inside.
GENUINE = [
    cur("2026-10-12T13:10:00Z", 20, five=20),   # five-hour arm
    cur("2026-10-12T13:20:00Z", 20, five=20),   # five-hour credit
    cur("2026-10-12T13:30:00Z", 0, five=20),    # reset-to-zero arm
    cur("2026-10-12T13:40:00Z", 0, five=20),    # reset-to-zero confirm
    cur("2026-10-12T13:50:00Z", 60, five=20),
    cur("2026-10-12T13:55:00Z", 55, five=25),   # weekly held, five-hour grew
    cur("2026-10-12T14:00:00Z", 30, five=25),   # >=25 pp fire
    cur("2026-10-12T14:10:00Z", 70, five=25),
    cur("2026-10-12T14:20:00Z", 10, end="2026-10-20T14:00:00Z", five=25),
]
FURTHER = (cur("2026-10-12T14:30:00Z", 12, end="2026-10-20T14:00:00Z", five=26),
           cur("2026-10-12T14:40:00Z", 13, end="2026-10-20T14:00:00Z", five=27))


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


def _reference_outcomes(monkeypatch, tmp_path, steps):
    ref = T.make_app(monkeypatch, tmp_path / "reference")
    T.run_steps(ref, monkeypatch, steps)
    return T.outcomes(ref)


def _releases(name="A"):
    key = T.account_key_of(name)
    return [(op["payload"]["axis"], op["payload"]["witness"])
            for op in T.ops_of_kind("outcome_axis_release")
            if op["payload"]["account"] == key]


def _completions(name="A"):
    key = T.account_key_of(name)
    return [tuple(op["payload"]["axes"])
            for op in T.ops_of_kind("outcome_authority_completion")
            if op["payload"]["account"] == key]


def _report_axes(app, name="A"):
    key = T.account_key_of(name)
    for row in T.quarantine_report(app)["accounts"]:
        if row["account"] == key:
            return row["axes"]
    raise AssertionError((name, T.quarantine_report(app)))


def _obs_id_captured_at(at):
    for rec in T.journal_records():
        if (rec.get("t") == "obs"
                and (rec.get("payload") or {}).get("captured_at") == at):
            return rec["id"]
    raise AssertionError(at)


def _descent(base, five_end):
    """A genuine same-source five-hour descent and confirmation in a fresh
    window (the first reading's changed weekly value makes it materialize)."""
    return [
        cur(T.iso(base), 80, five=70, five_end=five_end),
        cur(T.iso(base + 60), 80, five=10, five_end=five_end),
        cur(T.iso(base + 120), 80, five=10, five_end=five_end),
    ]


def test_the_withheld_descent_is_not_vacuous(app, monkeypatch):
    """Without any trigger the same descent mints exactly one credit."""
    base = T.epoch("2026-10-12T19:00:00Z")
    T.run_steps(app, monkeypatch, PREFIX + [WITNESS]
                + _descent(base, T.iso(base + 4 * 3600)))
    assert len(T.outcomes(app)["five_hour"]) == 1


def _five_hour_withheld_after(app, monkeypatch, start):
    """Whether a genuine five-hour descent after ``start`` mints nothing."""
    base = T.epoch(start)
    before = T.outcomes(app)
    T.run_steps(app, monkeypatch, _descent(base, T.iso(base + 4 * 3600)))
    return T.outcomes(app)["five_hour"] == before["five_hour"]


def _assert_withheld_after(app, monkeypatch, start):
    assert _five_hour_withheld_after(app, monkeypatch, start)
    assert T.axis_allowed(app, "A")[FIVE] is False


def _recovered(app, monkeypatch):
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER, COVER, WITNESS])
    assert T.axis_allowed(app, "A") == {WEEKLY: True, FIVE: True}


# --------------------------------------------------------------------------
# (a) a full recovery, through maintenance (T2 endpoint, T6)
# --------------------------------------------------------------------------

def test_a_recovery_mints_exactly_what_the_reference_mints(app, monkeypatch, tmp_path):
    T.run_trace(app, monkeypatch, PREFIX + [TRIGGER])
    assert T.axis_allowed(app, "A") == {WEEKLY: False, FIVE: False}
    T.run_steps(app, monkeypatch, [COVER])
    assert _completions() == [(WEEKLY, FIVE)], "one op covers both axes"
    assert T.account_quarantined(app, "A"), "coverage alone releases nothing"
    T.run_steps(app, monkeypatch, [WITNESS])
    witness = _obs_id_captured_at(WITNESS[1]["at"])
    assert sorted(_releases()) == [(FIVE, witness), (WEEKLY, witness)]
    assert T.axis_allowed(app, "A") == {WEEKLY: True, FIVE: True}
    assert T.account_quarantined(app, "B"), "recovery is account-local"
    axes = _report_axes(app)
    assert {a: axes[a]["state"] for a in axes} == {
        WEEKLY: "recovered", FIVE: "recovered"}
    T.run_steps(app, monkeypatch, GENUINE)
    result = T.continue_through_maintenance(app, monkeypatch, further=FURTHER)
    assert result["live_state"] == result["rebuilt_state"]
    assert result["after_rebuild_state"] == result["rederived_state"]
    assert T.axis_allowed(app, "A") == {WEEKLY: True, FIVE: True}
    # The no-exclusion reference run of the same readings, in its own store
    # (built last: building it re-points the module paths).
    reference = _reference_outcomes(
        monkeypatch, tmp_path,
        PREFIX + [COVER, WITNESS] + GENUINE + list(FURTHER))
    assert T.outcome_count(reference) == 4, reference  # non-vacuous
    assert result["after_rederive"] == reference
    assert result["live"]["five_hour"] and result["live"]["in_place"]
    assert result["live"]["transitions"]


def test_a_the_excluded_only_schedule_opens_outcomes_and_drops_together(
        app, monkeypatch):
    """T6 (A1): the shipped excluded-only recovery let a released outcome gate
    run ahead of a paused drop gate. Now both read the same journal-derived
    axis state, so at every position they agree, before and after the
    release."""
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER, COVER])
    status = T.account_status(app, "A")
    assert status["triggered"]
    assert all(not doc["allowed"] for doc in status["axes"].values())
    T.run_steps(app, monkeypatch, [WITNESS])
    status = T.account_status(app, "A")
    assert all(doc["allowed"] for doc in status["axes"].values())
    assert {a: d["state"] for a, d in status["axes"].items()} == {
        WEEKLY: "recovered", FIVE: "recovered"}
    # The release lives in the journal: losing every local selected-state
    # file after it changes nothing (T6).
    import _cctally_core as core
    for path in (core.STATUSLINE_SELECTED_PATH,):
        path.unlink(missing_ok=True)
    for path in core.STATUSLINE_ACCOUNT_BOUND_DIR.glob("selected*"):
        path.unlink()
    assert T.axis_allowed(app, "A") == {WEEKLY: True, FIVE: True}
    # A genuine drop and a credit after the release are minted.
    T.run_steps(app, monkeypatch, GENUINE[:2])
    assert len(T.outcomes(app)["five_hour"]) == 1


# --------------------------------------------------------------------------
# (b) nothing releases without completed authority and a fresh witness
# --------------------------------------------------------------------------

def test_b_a_witness_without_completed_authority_releases_nothing(
        app, monkeypatch):
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER, WITNESS])
    assert _releases() == []
    axes = _report_axes(app)
    assert {a: axes[a]["waitingFor"] for a in axes} == {
        WEEKLY: "usage-refresh", FIVE: "usage-refresh"}
    _assert_withheld_after(app, monkeypatch, "2026-10-12T19:00:00Z")


def test_b_an_incomplete_reconciliation_covers_nothing_and_a_later_one_does(
        app, monkeypatch):
    """The OAuth observation is appended, but its selected-state
    reconciliation fails: no completion op exists, so nothing is covered. A
    later reconciliation covers, and the five-hour axis recovers on its next
    fresh window."""
    import _cctally_statusline as sl
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER])
    real = sl._reconcile_selected_control

    def fail(*args, **kwargs):
        raise OSError("reconciliation interrupted")

    monkeypatch.setattr(sl, "_reconcile_selected_control", fail)
    T.run_steps(app, monkeypatch, [oauth("2026-10-09T10:10:00Z", 31, five=41,
                                         expect="record_failed")])
    assert _completions() == []
    monkeypatch.setattr(sl, "_reconcile_selected_control", real)
    T.run_steps(app, monkeypatch, [WITNESS])
    assert _releases() == [], "the witness found no coverage"
    T.run_steps(app, monkeypatch, [oauth("2026-10-12T13:05:00Z", 21,
                                         end=W_END, five=71, five_end=W_FIVE)])
    assert _completions() == [(WEEKLY, FIVE)]
    assert _five_hour_withheld_after(app, monkeypatch, "2026-10-12T14:00:00Z"), (
        "same window: still paused")
    # The next five-hour window's first entry witnesses five-hour only; the
    # weekly axis waits for its own fresh period.
    assert not _five_hour_withheld_after(app, monkeypatch,
                                         "2026-10-12T19:00:00Z")
    assert T.axis_allowed(app, "A") == {WEEKLY: False, FIVE: True}
    assert [axis for axis, _ in _releases()] == [FIVE]


def test_b_an_implausible_oauth_reading_covers_nothing(app, monkeypatch):
    """Plausibility before fences (spec 6.4): an OAuth weekly reset outside
    the ingress band records nothing, installs no fence and covers nothing."""
    import _cctally_journal as jr
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER])
    high_water = jr.journal_high_water()
    T.run_steps(app, monkeypatch, [oauth(
        "2026-10-09T10:10:00Z", 31, end="2026-12-01T00:00:00Z", five=41,
        expect="record_failed")])
    assert jr.journal_high_water() == high_water, "nothing was appended"
    T.run_steps(app, monkeypatch, [WITNESS])
    assert _completions() == [] and _releases() == []
    assert T.account_quarantined(app, "A")


def test_b_a_stale_witness_keeps_its_axis_paused(app, monkeypatch):
    """A reading naming the old week is no weekly witness, while it is the
    first entry of a fresh five-hour window: only five-hour releases."""
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER, COVER])
    T.run_steps(app, monkeypatch, [cur("2026-10-09T15:00:00Z", 32, end=E0,
                                       five=10,
                                       five_end="2026-10-09T20:00:00Z")])
    assert [axis for axis, _ in _releases()] == [FIVE]
    assert T.axis_allowed(app, "A") == {WEEKLY: False, FIVE: True}


def _obs_id(at, week_end):
    for rec in T.journal_records():
        payload = rec.get("payload") or {}
        if (rec.get("t") == "obs" and payload.get("captured_at") == at
                and payload.get("resets_at") == T.epoch(week_end)):
            return rec["id"]
    raise AssertionError((at, week_end))


#: BV2: a delayed old-period reading and the fresh first entry in one second.
ROLLOVER = "2026-10-12T12:02:00Z"
NEXT_END = "2026-10-19T12:00:00Z"


@pytest.mark.parametrize("excluded_capture", ["canonical", "offset"])
def test_b_a_same_second_rollover_witnesses_unless_its_text_sorts_earlier(
        app, monkeypatch, excluded_capture):
    """BV2 (spec 6.6, T2) through the real writers: a delayed reading still
    naming the old week and the fresh week's first entry share one capture
    second, so the fresh entry's capture equals the retained maximum in
    instant and text and it witnesses and releases weekly. Twin: the
    excluded write's capture is the `+10:00` form of an earlier instant,
    whose text sorts above the fresh entry's: chronologically later but
    lexically earlier, the fresh entry is no witness and weekly stays
    paused. Detects a release on a capture-order regression, and a lost
    same-second rollover witness."""
    captured = (None if excluded_capture == "canonical"
                else "2026-10-12T20:05:00+10:00")
    T.run_steps(app, monkeypatch, [
        cur("2026-10-12T10:00:00Z", 30, end=E0),
        ("legacy", dict(at="2026-10-12T10:05:00Z", weekly=50, week_end=E0,
                        account="B", captured=captured)),
        oauth("2026-10-12T10:10:00Z", 31, end=E0),
        cur(ROLLOVER, 32, end=E0),
        cur(ROLLOVER, 20, end=NEXT_END)])
    assert _obs_id(ROLLOVER, E0), "the old-period reading is retained"
    if excluded_capture == "canonical":
        assert _releases() == [(WEEKLY, _obs_id(ROLLOVER, NEXT_END))]
        assert T.axis_allowed(app, "A")[WEEKLY] is True
    else:
        assert _releases() == []
        assert T.axis_allowed(app, "A")[WEEKLY] is False


def test_b_a_renewed_trigger_between_completion_and_witness(app, monkeypatch):
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER, COVER])
    first = T.account_status(app, "A")["generation"]
    T.legacy(app, monkeypatch, at="2026-10-09T10:20:00Z", weekly=51,
             week_end=E0, five=61, five_end=F0, account="B")
    T.run_steps(app, monkeypatch, [WITNESS])
    status = T.account_status(app, "A")
    assert status["generation"] > first
    assert _releases() == []
    assert all(not doc["allowed"] for doc in status["axes"].values())
    axes = _report_axes(app)
    assert axes[WEEKLY]["waitingFor"] == "usage-refresh"


# --------------------------------------------------------------------------
# (c) partial coverage then supplementation
# --------------------------------------------------------------------------

def test_c_a_weekly_only_reading_covers_weekly_and_a_later_one_supplements(
        app, monkeypatch):
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER,
                                            oauth("2026-10-09T10:10:00Z", 31)])
    assert _completions() == [(WEEKLY,)]
    T.run_steps(app, monkeypatch, [WITNESS])
    assert [axis for axis, _ in _releases()] == [WEEKLY]
    assert T.axis_allowed(app, "A") == {WEEKLY: True, FIVE: False}
    assert _report_axes(app)[FIVE]["waitingFor"] == "usage-refresh"
    T.run_steps(app, monkeypatch, [oauth("2026-10-12T13:05:00Z", 21,
                                         end=W_END, five=71, five_end=W_FIVE)])
    assert _completions() == [(WEEKLY,), (FIVE,)], "one op per newly covered axis"
    assert not _five_hour_withheld_after(app, monkeypatch,
                                         "2026-10-12T19:00:00Z")
    assert T.axis_allowed(app, "A") == {WEEKLY: True, FIVE: True}


# --------------------------------------------------------------------------
# (d) a crash between the witness and its release
# --------------------------------------------------------------------------

def test_d_the_next_writer_resumes_a_release_lost_to_a_crash(
        app, monkeypatch, tmp_path):
    import _cctally_quarantine as G
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER, COVER])
    real = G.run_release_step
    calls = []

    def crash_after_witness(account_key):
        # The record path runs the step before each append (resume) and
        # after it (live); the process dies before the live release.
        calls.append(account_key)
        if len(calls) % 2 == 0:
            return []   # the process died before the release append
        return real(account_key)

    monkeypatch.setattr(G, "run_release_step", crash_after_witness)
    T.run_steps(app, monkeypatch, [WITNESS])
    assert _releases() == []
    axes = _report_axes(app)
    witnessed = WITNESS[1]["at"]
    assert all(axes[a]["notBefore"] is None or axes[a]["notBefore"] <= witnessed
               for a in axes), (
        "a witness awaiting its resumed release shows no future notBefore",
        axes)
    monkeypatch.setattr(G, "run_release_step", real)
    T.run_steps(app, monkeypatch, GENUINE[:2])
    witness = _obs_id_captured_at(WITNESS[1]["at"])
    assert sorted(_releases()) == [(FIVE, witness), (WEEKLY, witness)]
    # The release precedes the resuming writer's own observation.
    ids = [rec["id"] for rec in T.journal_records()]
    release_at = max(ids.index(op["id"])
                     for op in T.ops_of_kind("outcome_axis_release"))
    assert release_at < ids.index(_obs_id_captured_at(GENUINE[0][1]["at"]))
    assert len(T.outcomes(app)["five_hour"]) == 1
    reference = _reference_outcomes(
        monkeypatch, tmp_path, PREFIX + [COVER, WITNESS] + GENUINE[:2])
    assert T.outcomes(app)["five_hour"] == reference["five_hour"]


# --------------------------------------------------------------------------
# (e) revocation after a release
# --------------------------------------------------------------------------

def test_e_a_backdated_weekly_reading_revokes(app, monkeypatch):
    _recovered(app, monkeypatch)
    # Weekly ingress admits resets up to 30 days in the past.
    T.run_steps(app, monkeypatch, [cur("2026-10-12T13:05:00Z", 30, end=E0,
                                       five=70)])
    assert T.axis_allowed(app, "A")[WEEKLY] is False
    assert _report_axes(app)[WEEKLY]["revocation"] == "backward-key"


def test_e_a_new_excluded_observation_renews_the_barrier(app, monkeypatch):
    _recovered(app, monkeypatch)
    T.legacy(app, monkeypatch, at="2026-10-12T13:05:00Z", weekly=1,
             week_end=W_END, five=1, five_end=W_FIVE, account="B")
    _assert_withheld_after(app, monkeypatch, "2026-10-12T14:00:00Z")
    assert T.axis_allowed(app, "A") == {WEEKLY: False, FIVE: False}


def test_e_a_legacy_spool_change_renews_the_barrier(app, monkeypatch):
    import _cctally_core as core
    import _cctally_journal as jr
    _recovered(app, monkeypatch)
    core.STATUSLINE_SELECTED_PATH.write_text('{"legacy": 1}')
    jr.append_legacy_spool_activity(
        changed=("statusline-selected.json",),
        new_state=jr.capture_legacy_spool_state(), reason="changed")
    _assert_withheld_after(app, monkeypatch, "2026-10-12T14:00:00Z")


def test_e_a_manual_record_credit_revokes_weekly(app, monkeypatch):
    _recovered(app, monkeypatch)
    T.set_clock(monkeypatch, "2026-10-12T13:05:00Z")
    T.identity("A")
    args = app.build_parser().parse_args(["record-credit", "--to", "5", "--yes"])
    assert args.func(args) == 0
    assert T.axis_allowed(app, "A")[WEEKLY] is False
    assert _report_axes(app)[WEEKLY]["revocation"] == "manual-credit"


def _journal_types():
    return [rec.get("t") for rec in T.journal_records()]


def test_e_a_real_rederive_correction_revokes(app, monkeypatch, capsys):
    """End to end: a real `db rederive` whose plan applies a correction batch
    revokes the recovered account (administrative, which also voids its
    coverage), and a genuine five-hour descent after it is withheld. The
    correction is made genuine by retaining priced session input the live
    recording never saw."""
    for name in ("A", "B", "unattributed"):
        T.retain_session_input(app, name)
    _recovered(app, monkeypatch)
    T.run_steps(app, monkeypatch, [cur("2026-10-12T13:05:00Z", 25, five=70)])
    assert not T.account_quarantined(app, "A")
    cache = app.open_cache_db()
    try:
        cache.execute(
            "INSERT INTO session_entries (source_path, line_offset, timestamp_utc,"
            " model, input_tokens, output_tokens, cache_create_tokens,"
            " cache_read_tokens, cache_create_1h_tokens, account_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("/retained/A/session.jsonl", 1, "2026-10-12T13:02:00Z",
             "claude-3-5-sonnet-20241022", 200000, 20000, 0, 0, 0,
             T.account_key_of("A")))
        cache.commit()
    finally:
        cache.close()
    before = _journal_types().count("correction_batch")
    T.rederive(app)
    capsys.readouterr()
    assert _journal_types().count("correction_batch") > before, (
        "non-vacuity: the rederive applied a correction batch")
    assert T.account_quarantined(app, "A")
    assert _report_axes(app)[FIVE]["waitingFor"] == "usage-refresh", (
        "an administrative revocation voids the coverage")
    _assert_withheld_after(app, monkeypatch, "2026-10-12T19:00:00Z")


def test_e_a_non_inert_fallback_revokes_at_the_deletion_event(app, monkeypatch):
    import _lib_detector_access_registry as R
    monkeypatch.setattr(R, "non_inert_cross_period",
                        lambda registry=None: ("unclassified",))
    _recovered(app, monkeypatch)
    T.run_steps(app, monkeypatch, GENUINE[:2])   # the five-hour credit
    assert len(T.outcomes(app)["five_hour"]) == 1
    T.ingest(app)   # folds the credit's harvested event
    assert T.account_quarantined(app, "A")
    before = T.outcomes(app)
    T.run_steps(app, monkeypatch, GENUINE[2:4])  # reset-to-zero: withheld
    assert T.outcomes(app) == before


def _arm_then_confirm(app, monkeypatch, *, rebuild_between):
    """After a release, arm a genuine same-source five-hour 70->20 descent in
    a fresh window, optionally run a real `db rebuild`, then observe 20
    again. Returns the outcomes when armed and at the end."""
    _recovered(app, monkeypatch)
    base = T.epoch("2026-10-12T19:00:00Z")
    five_end = T.iso(base + 4 * 3600)
    T.run_steps(app, monkeypatch, [
        cur(T.iso(base), 80, five=70, five_end=five_end),
        cur(T.iso(base + 60), 80, five=20, five_end=five_end),
    ])
    armed = T.outcomes(app)
    if rebuild_between:
        T.rebuild(app)
    T.run_steps(app, monkeypatch, [
        cur(T.iso(base + 120), 80, five=20, five_end=five_end)])
    return armed, T.outcomes(app)


def test_a_rebuild_between_arm_and_confirm_mints_no_credit_after_recovery(
        app, monkeypatch, tmp_path):
    """Detects a rebuild that carries the disposable five-hour confirmation
    state across (#751b): a credit armed before the rebuild cannot be
    confirmed after it (the admitted cold-start gap), while the release
    itself survives the rebuild. The uninterrupted control mints exactly one
    more credit, so the withheld one is real."""
    armed, after = _arm_then_confirm(app, monkeypatch, rebuild_between=True)
    assert after == armed, ("a credit crossed the rebuild", armed, after)
    assert T.axis_allowed(app, "A") == {WEEKLY: True, FIVE: True}
    control = T.make_app(monkeypatch, tmp_path / "uninterrupted")
    control_armed, control_after = _arm_then_confirm(
        control, monkeypatch, rebuild_between=False)
    assert control_armed == armed
    assert len(control_after["five_hour"]) == len(armed["five_hour"]) + 1, (
        control_armed, control_after)


# --------------------------------------------------------------------------
# T4 identity versus multiplicity
# --------------------------------------------------------------------------

def test_t4_an_exact_replay_adds_no_confirmation_and_cycles_stay_distinct(
        app, monkeypatch):
    """Detects an extra confirmation from a replayed observation and a lost
    second cycle: after the release, re-appending a witness-era observation
    verbatim mints nothing and releases nothing, while two genuinely
    distinct same-value five-hour descents in two windows mint two credits."""
    import _cctally_journal as jr
    _recovered(app, monkeypatch)
    first = T.epoch("2026-10-12T19:00:00Z")
    T.run_steps(app, monkeypatch, _descent(first, T.iso(first + 4 * 3600)))
    assert len(T.outcomes(app)["five_hour"]) == 1
    releases = _releases()
    [replayed] = [rec for rec in T.journal_records()
                  if rec.get("t") == "obs"
                  and rec["payload"].get("captured_at") == T.iso(first + 120)]
    jr.append_records([replayed])
    T.ingest(app)
    assert len(T.outcomes(app)["five_hour"]) == 1, "the replay confirmed again"
    assert _releases() == releases
    second = first + 5 * 3600
    T.run_steps(app, monkeypatch, _descent(second, T.iso(second + 4 * 3600)))
    assert len(T.outcomes(app)["five_hour"]) == 2, "the second cycle was lost"


# --------------------------------------------------------------------------
# T8 write bound
# --------------------------------------------------------------------------

def _op_count():
    return sum(1 for rec in T.journal_records() if rec.get("t") == "op")


def _routine(start, count, *, end=E0, five_end=F0, base=31, five0=41,
             account="A"):
    """Renders and OAuth refreshes every 30 s, values never descending."""
    at0 = T.epoch(start)
    steps = []
    for i in range(count):
        at = at0 + 30 * i
        steps.append(cur(T.iso(at), base, end=end, five=five0 + i // 5,
                         five_end=five_end, account=account))
        steps.append(oauth(T.iso(at + 15), base, end=end, five=five0 + i // 5,
                           five_end=five_end, account=account))
    return steps


def test_t8_routine_traffic_adds_no_journal_ops(app, monkeypatch):
    """Detects per-render or per-refresh journal writes: with no trigger a
    long render-plus-OAuth trace adds no op; with one, a covered account's
    routine refreshes and silent frames add none, and its witness adds one
    release per axis."""
    T.run_steps(app, monkeypatch, PREFIX)
    before = _op_count()
    T.run_steps(app, monkeypatch, _routine("2026-10-09T10:01:00Z", 6, base=30))
    assert _op_count() == before, "ops without a trigger"
    T.run_steps(app, monkeypatch, [TRIGGER, COVER])
    assert _completions() == [(WEEKLY, FIVE)]
    covered = _op_count()
    T.run_steps(app, monkeypatch, _routine("2026-10-09T10:20:00Z", 6))
    assert _op_count() == covered, "ops from routine covered refreshes"
    T.run_steps(app, monkeypatch, [WITNESS])
    assert len(_releases()) == 2
    released = _op_count()
    T.run_steps(app, monkeypatch, _routine(
        "2026-10-12T13:01:00Z", 6, end=W_END, five_end=W_FIVE, base=21,
        five0=71))
    assert _op_count() == released, "ops after the release"


def test_t8_an_append_without_a_trigger_checks_the_index_once(
        app, monkeypatch):
    """Review finding F13: with no trigger an observation append opens the
    stats index for the quarantine once (the resume step), not again after
    the append; with a trigger both steps run. Detects a doubled read-only
    open on every status-line append of an install that never had a
    trigger."""
    import _cctally_quarantine as G
    real = G._with_covered_index
    calls = []

    def counting(action):
        calls.append(1)
        return real(action)

    monkeypatch.setattr(G, "_with_covered_index", counting)
    T.run_steps(app, monkeypatch, PREFIX)
    assert len(calls) == 1, calls
    T.run_steps(app, monkeypatch, [TRIGGER, COVER])
    calls.clear()
    T.run_steps(app, monkeypatch, [WITNESS])
    assert len(_releases()) == 2, "the live step still releases the witness"
    assert len(calls) >= 2, calls


def test_an_unavailable_index_is_logged_once_per_process(app, monkeypatch):
    """Review finding F10: a failure the release and completion steps turn
    into "decide nothing" leaves one hook-tick.log line per process, never
    one per append; the steps still return nothing."""
    import _cctally_core as core
    import _cctally_quarantine as G
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER])
    T.ingest(app)
    monkeypatch.setattr(G, "_UNAVAILABLE_LOGGED", False, raising=False)
    assert G._with_covered_index(lambda conn: "decided") == "decided", (
        "non-vacuity: the covered index runs the action")

    def broken(conn):
        raise RuntimeError("injected index failure")

    for _ in range(3):
        assert G._with_covered_index(broken) is None
    lines = [line for line in core.HOOK_TICK_LOG_PATH.read_text().splitlines()
             if "op=outcome-quarantine-index" in line]
    assert len(lines) == 1, lines
    assert "result=unavailable" in lines[0]


def _administrative(app, monkeypatch, at, batch_id):
    import _cctally_journal as jr
    import _lib_journal as J
    T.set_clock(monkeypatch, at)
    jr.append_records(J.make_correction_batch(
        batch_id=batch_id, family="claude-usage", at=at, actions=[]))
    T.ingest(app)


def test_t8_completions_are_bounded_per_voided_account_axis(app, monkeypatch):
    """Detects unbounded completions: one administrative op over three
    covered accounts, then their next reconciliations, adds exactly three
    completions (one per account, both axes), and a second administrative op
    in the same generation repeats exactly that."""
    accounts = ("A", "B", "C")
    T.run_steps(app, monkeypatch, [
        cur("2026-10-09T10:00:00Z", 30, end=E0, five=40, five_end=F0,
            account=name) for name in accounts] + [TRIGGER])
    T.run_steps(app, monkeypatch, [
        oauth(f"2026-10-09T10:1{i}:00Z", 31, five=41, account=name)
        for i, name in enumerate(accounts)])
    count = len(T.ops_of_kind("outcome_authority_completion"))
    assert count == 3
    for round_, minute in ((1, 2), (2, 4)):
        _administrative(app, monkeypatch, f"2026-10-09T10:{minute}0:00Z",
                        f"batch:t8-{round_}")
        T.run_steps(app, monkeypatch, [
            oauth(f"2026-10-09T10:{minute}{i + 1}:00Z", 31 + round_, five=41,
                  account=name) for i, name in enumerate(accounts)])
        # A further refresh of a re-covered account adds nothing.
        T.run_steps(app, monkeypatch, [oauth(
            f"2026-10-09T10:{minute}5:00Z", 31 + round_, five=41)])
        now = len(T.ops_of_kind("outcome_authority_completion"))
        assert now == count + 3, (round_, now, count)
        count = now


def test_t8_a_revocation_then_renewal_add_bounded_releases(app, monkeypatch):
    """Detects repeated releases: a backward-key revocation followed by a
    fresh period adds exactly one release; a renewed trigger then adds at
    most one completion per axis and one release per axis."""
    _recovered(app, monkeypatch)
    releases = len(_releases())
    T.run_steps(app, monkeypatch, [cur("2026-10-12T13:05:00Z", 30, end=E0,
                                       five=70)])
    assert T.axis_allowed(app, "A")[WEEKLY] is False
    T.run_steps(app, monkeypatch, [
        cur("2026-10-19T13:30:00Z", 5, end="2026-10-26T13:00:00Z", five=72,
            five_end="2026-10-19T18:00:00Z"),
        cur("2026-10-19T13:40:00Z", 6, end="2026-10-26T13:00:00Z", five=72,
            five_end="2026-10-19T18:00:00Z")])
    assert len(_releases()) == releases + 1
    assert T.axis_allowed(app, "A") == {WEEKLY: True, FIVE: True}
    completions = len(_completions())
    releases = len(_releases())
    T.legacy(app, monkeypatch, at="2026-10-19T13:45:00Z", weekly=1,
             week_end="2026-10-26T13:00:00Z", five=1,
             five_end="2026-10-19T18:00:00Z", account="B")
    T.run_steps(app, monkeypatch, [
        oauth("2026-10-19T13:50:00Z", 7, end="2026-10-26T13:00:00Z", five=73,
              five_end="2026-10-19T18:00:00Z"),
        oauth("2026-10-19T13:55:00Z", 7, end="2026-10-26T13:00:00Z", five=73,
              five_end="2026-10-19T18:00:00Z"),
        cur("2026-10-26T14:00:00Z", 2, end="2026-11-02T14:00:00Z", five=3,
            five_end="2026-10-26T19:00:00Z"),
        cur("2026-10-26T14:10:00Z", 2, end="2026-11-02T14:00:00Z", five=4,
            five_end="2026-10-26T19:00:00Z")])
    assert len(_completions()) - completions <= 2
    assert len(_completions()) - completions >= 1
    assert len(_releases()) - releases == 2
    assert T.axis_allowed(app, "A") == {WEEKLY: True, FIVE: True}


# --------------------------------------------------------------------------
# T5 replay agreement: completion windows and the 1022 -> 1023 transition
# --------------------------------------------------------------------------

def test_t5_completion_replay_splits_the_witness_and_its_release(
        app, monkeypatch):
    """Detects a derivation-completion replay that disagrees with live
    ingest on a release: the witness lands, a crash loses its release, a
    `db rebuild` publishes a trusted frontier right after the witness, the
    next writer resumes the release (and a credit follows), and a second
    `db rebuild`, owed the window from that frontier, replays it from the
    checkpointed state with the witness and the release on either side. The rebuilt state and
    outcomes equal live ingest's, and the replay appends no correction."""
    import _cctally_completion as C
    import _cctally_core as core
    import _cctally_journal as jr
    import _cctally_quarantine as G
    for name in ("A", "B", "unattributed"):
        T.retain_session_input(app, name)
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER, COVER])
    real = G.run_release_step
    calls = []

    def crash_after_witness(account_key):
        # The record path runs the step before each append (resume) and
        # after it (live); the process dies before the live release.
        calls.append(account_key)
        if len(calls) % 2 == 0:
            return []
        return real(account_key)

    monkeypatch.setattr(G, "run_release_step", crash_after_witness)
    T.run_steps(app, monkeypatch, [WITNESS])
    monkeypatch.setattr(G, "run_release_step", real)
    assert _releases() == []
    T.rebuild(app)
    trusted = jr.read_trusted_derivation_state(core.DB_PATH)
    assert trusted is not None, "the rebuild published no derivation frontier"
    # A genuine descent in a fresh five-hour window (a descent from the
    # pre-rebuild witness would fall in #751b's admitted cold-start gap).
    first = T.epoch("2026-10-12T19:00:00Z")
    T.run_steps(app, monkeypatch, _descent(first, T.iso(first + 4 * 3600)))
    assert len(_releases()) == 2
    assert len(T.outcomes(app)["five_hour"]) == 1
    live_state, live_outcomes = T.quarantine_state(app), T.outcomes(app)
    batches = [rec for rec in T.journal_records()
               if rec.get("t") == "correction_batch"]
    # Live ingest advanced the proof past the release; restore the weaker,
    # still true claim the first rebuild published, so the next rebuild owes
    # (and replays) the window from just after the witness.
    import sqlite3
    conn = sqlite3.connect(core.DB_PATH)
    try:
        assert conn.execute(
            "UPDATE journal_derivation_frontier SET segment = ?, offset = ?, "
            "output_segment = NULL, output_offset = NULL WHERE id = 1",
            tuple(trusted.frontier)).rowcount == 1
        conn.commit()
    finally:
        conn.close()
    windows = []
    real_window = C._complete_window

    def spy(base, tmp, start, target, *args, **kwargs):
        windows.append((start, target))
        return real_window(base, tmp, start, target, *args, **kwargs)

    monkeypatch.setattr(C, "_complete_window", spy)
    T.rebuild(app)
    assert windows and windows[0][0] == tuple(trusted.frontier), (
        "non-vacuity: the completion window starts at the trusted frontier",
        windows, trusted)
    assert T.quarantine_state(app) == live_state
    assert T.outcomes(app) == live_outcomes
    assert [rec for rec in T.journal_records()
            if rec.get("t") == "correction_batch"] == batches


def test_t5_the_1022_to_1023_transition_reconstructs_releases(
        app, monkeypatch):
    """Detects an epoch transition that loses recovery facts: a store whose
    index is still the 1022 shape (certificates and envelope, no axis
    state) and epoch rebuilds to 1023 with the one-axis and two-axis
    releases, coverage and outcomes live ingest held."""
    import sqlite3
    import _cctally_core as core
    import _cctally_store as store
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER,
                                            oauth("2026-10-09T10:10:00Z", 31)])
    T.run_steps(app, monkeypatch, [WITNESS])
    assert [axis for axis, _ in _releases()] == [WEEKLY], "one-axis release"
    T.run_steps(app, monkeypatch, [oauth("2026-10-12T13:05:00Z", 21,
                                         end=W_END, five=71, five_end=W_FIVE)])
    assert not _five_hour_withheld_after(app, monkeypatch,
                                         "2026-10-12T19:00:00Z")
    assert T.axis_allowed(app, "A") == {WEEKLY: True, FIVE: True}
    live_state, live_outcomes = T.quarantine_state(app), T.outcomes(app)
    assert live_outcomes["five_hour"], "non-vacuity: a recovered outcome"
    conn = sqlite3.connect(core.DB_PATH)
    try:
        for table in ("outcome_quarantine_axis_state",
                      "outcome_quarantine_bounds",
                      "outcome_quarantine_episodes"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("CREATE TABLE outcome_quarantine_envelope "
                     "(scope TEXT PRIMARY KEY, envelope_json TEXT)")
        conn.execute("CREATE TABLE outcome_quarantine_certificates "
                     "(account_key TEXT, kind TEXT, certificate_json TEXT)")
        conn.execute("PRAGMA user_version = 1022")
        conn.commit()
    finally:
        conn.close()
    store.resolve_stats_epoch_mismatch().close()
    conn = sqlite3.connect(core.DB_PATH)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1023
    finally:
        conn.close()
    assert T.quarantine_state(app) == live_state
    assert T.outcomes(app) == live_outcomes
