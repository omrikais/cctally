"""#825/#832 fix round K13 (review cycle 7 finding P1-2): unknown
classification evidence defers the WHOLE evaluation whatever the
outcome-quarantine drop gate says (spec 4.3 *Classification evidence*).

An open drop gate is no classification evidence. Before K13 three paths
treated it as one: the fresh gated reduction returned the open reduction
before it looked for an unestablished axis, the reducer kernel published an
axis with no projected baseline whenever drop consensus was allowed, and
frozen-plan recovery revalidated an unknown axis only when the gate was
closed. And `_plan_drop_authority` answered "drop" as soon as one axis
dropped, so a classified drop on one axis outranked unknown evidence on the
other and a closed gate discarded the frozen plan instead of keeping it.

These are unit tests over the reducer's own functions with synthetic,
covered projections; `test_statusline_deferred_intake_frozen.py` case 20
drives the same rules through real statusline ticks.
"""
from __future__ import annotations

import pytest

import _outcome_quarantine_trace as T

NOW = 1_791_540_000
FIVE_KEY = NOW + 4 * 3600
WEEK_KEY = NOW + 3 * 86400


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


def _lib():
    import _lib_statusline_candidates as lib
    return lib


def _axis(percent, key):
    return _lib().AxisProjection(
        percent=float(percent), raw_resets_at=key, canonical_key=key,
        captured_at=NOW - 60, source="statusline", reset_generation=0)


def _evidence(kind, predecessor=None):
    lib = _lib()
    return lib.AxisEvidence(kind, predecessor_key=predecessor,
                            predecessor_raw=predecessor)


def _projection(*, five=None, seven=None, evidence=None):
    lib = _lib()
    return lib.DbProjection(
        five_hour=None if five is None else _axis(five, FIVE_KEY),
        seven_day=None if seven is None else _axis(seven, WEEK_KEY),
        account_key="acct", evidence=evidence or {})


def _unknown():
    return _evidence(_lib().EVIDENCE_UNKNOWN)


def _candidate(token, *, five=None, seven=None, received_at=NOW):
    lib = _lib()
    return lib.Candidate(
        token=token * 64, received_at=received_at,
        five_hour=None if five is None else lib.AxisValue(
            float(five), FIVE_KEY, canonical_key=FIVE_KEY),
        seven_day=None if seven is None else lib.AxisValue(
            float(seven), WEEK_KEY, canonical_key=WEEK_KEY))


def _reduce(projection, candidates, *, allowed):
    lib = _lib()
    return lib.reduce_candidates(
        candidates, db=projection,
        control=lib.ControlState(db_projection=projection,
                                 pending_drops={"fiveHour": None,
                                                "sevenDay": None}),
        tombstones={"fiveHour": None, "sevenDay": None}, now_epoch=NOW,
        account_key="acct", drop_consensus_allowed=allowed)


# ---------------------------------------------------------------------------
# The kernel: an absent axis publishes only on classification evidence.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("allowed", [True, False], ids=["open", "closed"])
def test_the_kernel_never_publishes_an_unknown_absent_axis(allowed):
    """A covered snapshot that shows no five-hour baseline and whose
    evidence is unknown (a future-band rejection, a held row): the reading
    on that axis is unclassifiable under EITHER gate. The kernel publishes
    neither it nor — on its own — anything that would hide it, and reports
    the axis as unknown so its caller defers the whole evaluation."""
    projection = _projection(seven=60, evidence={"fiveHour": _unknown()})
    decision = _reduce(projection, [_candidate("a", five=25, seven=60)],
                       allowed=allowed)
    assert decision.plan is None or decision.plan.five_hour is None, (
        "an open drop gate classified an unknown axis as a first reading")
    assert decision.unknown_axes == frozenset({"fiveHour"})


@pytest.mark.parametrize("allowed", [True, False], ids=["open", "closed"])
@pytest.mark.parametrize("kind", ["proven_empty", "expired_predecessor"])
def test_the_kernel_still_publishes_a_classified_first_reading(allowed, kind):
    """C3 non-regression: a proven-empty baseline, or an expired five-hour
    predecessor whose window this reading's strictly follows, classifies the
    reading as a first reading or a new window, under either gate."""
    lib = _lib()
    evidence = (_evidence(lib.EVIDENCE_PROVEN_EMPTY) if kind == "proven_empty"
                else _evidence(lib.EVIDENCE_EXPIRED_PREDECESSOR,
                               predecessor=FIVE_KEY - 5 * 3600))
    projection = _projection(seven=60, evidence={"fiveHour": evidence})
    decision = _reduce(projection, [_candidate("a", five=25, seven=60)],
                       allowed=allowed)
    assert decision.action == "PUBLISH_DB"
    assert decision.plan.five_hour is not None
    assert decision.plan.five_hour.percent == 25.0
    assert decision.unknown_axes == frozenset()


# ---------------------------------------------------------------------------
# Fresh reduction: `_gated_reduction` defers on unknown evidence with the
# gate open, and with an unknown axis beside a classified drop.
# ---------------------------------------------------------------------------

def _account():
    import _lib_accounts
    return _lib_accounts.account_key("claude", "A")


def _classified(projection):
    import _cctally_statusline as sl
    return sl._Classified(projection, {}, ("", 0), NOW, (NOW, NOW))


def _gated(app, monkeypatch, projection, candidates, *, permitted):
    import _cctally_statusline as sl
    monkeypatch.setattr(sl, "_drop_permitted", lambda *a, **k: permitted)
    return sl._gated_reduction(
        _account(), tuple(candidates), None, eval_epoch=NOW, fence_epoch=NOW,
        classified=_classified(projection))


def test_an_open_gate_defers_a_fresh_evaluation_with_an_unknown_axis(
        app, monkeypatch):
    """The weekly 61 is a classifiable rise; the five-hour axis is unknown.
    With NO quarantine trigger the gate is open, and the evaluation must
    still defer whole: neither axis publishes (spec 4.3 *Deferral covers the
    whole evaluation*)."""
    projection = _projection(seven=60, evidence={"fiveHour": _unknown()})
    (decision, *_rest), withheld, unknown = _gated(
        app, monkeypatch, projection, [_candidate("a", five=25, seven=61)],
        permitted=True)
    assert unknown == frozenset({"fiveHour"}), (
        "an open drop gate let an unknown axis through fresh reduction")
    assert decision.plan is None or decision.plan.five_hour is None


@pytest.mark.parametrize("drop_axis", ["sevenDay", "fiveHour"])
def test_a_closed_gate_defers_a_fresh_drop_beside_an_unknown_axis(
        app, monkeypatch, drop_axis):
    """Both axis permutations: a classified same-window drop on one axis and
    unknown evidence on the other. The drop alone would be withheld while
    the other axis publishes; the unknown axis makes the whole evaluation
    defer instead."""
    if drop_axis == "sevenDay":
        projection = _projection(seven=70, evidence={"fiveHour": _unknown()})
        candidates = [_candidate("a", five=25, seven=20),
                      _candidate("b", five=25, seven=20)]
        unknown_axis = "fiveHour"
    else:
        projection = _projection(five=40, evidence={"sevenDay": _unknown()})
        candidates = [_candidate("a", five=10, seven=30),
                      _candidate("b", five=10, seven=30)]
        unknown_axis = "sevenDay"
    _reduced, _withheld, unknown = _gated(
        app, monkeypatch, projection, candidates, permitted=False)
    assert unknown == frozenset({unknown_axis})


def test_an_open_gate_still_publishes_a_proven_empty_fresh_reading(
        app, monkeypatch):
    """C3 non-regression through the gated reduction: proven-empty evidence
    publishes both axes of a first reading with the gate open."""
    lib = _lib()
    empty = _evidence(lib.EVIDENCE_PROVEN_EMPTY)
    projection = _projection(evidence={"fiveHour": empty, "sevenDay": empty})
    (decision, *_rest), withheld, unknown = _gated(
        app, monkeypatch, projection, [_candidate("a", five=25, seven=40)],
        permitted=True)
    assert unknown == frozenset() and not withheld
    assert decision.action == "PUBLISH_DB"
    assert decision.plan.five_hour.percent == 25.0
    assert decision.plan.seven_day.percent == 40.0


# ---------------------------------------------------------------------------
# Frozen-plan recovery: unknown evidence keeps the plan frozen under its
# identity with the gate open, and outranks a classified drop.
# ---------------------------------------------------------------------------

def _plan(*, five, seven):
    return {"fiveHour": {"canonicalKey": FIVE_KEY, "percent": float(five),
                         "rawResetsAt": FIVE_KEY},
            "sevenDay": {"canonicalKey": WEEK_KEY, "percent": float(seven),
                         "rawResetsAt": WEEK_KEY},
            "capturedAt": NOW, "binding": {}, "publicationId": "frozen-plan",
            "intakeSequence": 7}


def _drop_beside_unknown(drop_axis):
    if drop_axis == "sevenDay":
        return (_plan(five=25, seven=20),
                _projection(seven=70, evidence={"fiveHour": _unknown()}))
    return (_plan(five=10, seven=30),
            _projection(five=40, evidence={"sevenDay": _unknown()}))


@pytest.mark.parametrize("drop_axis", ["sevenDay", "fiveHour"])
def test_unknown_evidence_outranks_a_classified_drop_in_a_frozen_plan(
        drop_axis):
    import _cctally_statusline as sl
    plan, projection = _drop_beside_unknown(drop_axis)
    assert sl._plan_drop_authority(plan, projection) == sl._PLAN_UNKNOWN


def _resolve(app, monkeypatch, plan, projection, *, permitted):
    """`_resolve_frozen_publication` over a never-appended frozen ``plan``
    whose covered classification is ``projection``; returns the resolution
    and every transition write and publication attempt it made."""
    import _cctally_statusline as sl
    import _lib_account_binding
    writes, published = [], []
    monkeypatch.setattr(_lib_account_binding, "load", lambda doc: object())
    monkeypatch.setattr(sl, "_publication_appended", lambda plan: False)
    monkeypatch.setattr(sl, "_classify",
                        lambda *a, **k: _classified(projection))
    monkeypatch.setattr(sl, "_drop_permitted", lambda *a, **k: permitted)
    monkeypatch.setattr(sl, "_write_transition", writes.append)
    monkeypatch.setattr(sl, "_publication_supersession",
                        lambda *a, **k: "current")
    monkeypatch.setattr(sl, "_publish_plan",
                        lambda *a, **k: published.append(a) or sl._Resolution(
                            a[0], None, "ok"))
    transition = sl._Transition(
        account_key=_account(), binding=object(), generation=3,
        five_hour_anchor=None, control=sl._empty_control(projection),
        publication=plan, intake_acknowledged=plan["intakeSequence"] - 1)
    resolution = sl._resolve_frozen_publication(
        transition, now_epoch=NOW, nudge_sink=lambda: None, frontier=None)
    return transition, resolution, writes, published


def _assert_kept_frozen(transition, resolution, writes, published):
    import _cctally_statusline as sl
    assert published == [], "a plan with an unknown axis was published"
    assert writes == [], "the frozen plan's transition was rewritten"
    assert resolution.status == "deferred"
    assert resolution.transition is transition
    assert resolution.transition.publication["publicationId"] == "frozen-plan"
    assert resolution.transition.intake_acknowledged == 6, (
        "the acknowledgement moved past a deferred plan")
    assert resolution.stop is not None
    assert resolution.stop.reason == sl._BASELINE_UNKNOWN_REASON


def test_an_open_gate_keeps_a_frozen_plan_with_an_unknown_axis_frozen(
        app, monkeypatch):
    """No quarantine trigger, so the gate is open: a recovered plan whose
    five-hour axis has lost its classification evidence is still unknown
    work. It is neither published nor dropped."""
    plan = _plan(five=25, seven=61)
    projection = _projection(seven=60, evidence={"fiveHour": _unknown()})
    _assert_kept_frozen(*_resolve(app, monkeypatch, plan, projection,
                                  permitted=True))


@pytest.mark.parametrize("drop_axis", ["sevenDay", "fiveHour"])
def test_a_closed_gate_keeps_a_frozen_drop_beside_an_unknown_axis_frozen(
        app, monkeypatch, drop_axis):
    """Both permutations under a closed gate: the classified drop must not
    take precedence over the other axis's unknown evidence. The plan stays
    frozen under its identity, deferred and unacknowledged — never
    discarded as a withheld drop."""
    plan, projection = _drop_beside_unknown(drop_axis)
    _assert_kept_frozen(*_resolve(app, monkeypatch, plan, projection,
                                  permitted=False))


def test_a_closed_gate_still_withholds_a_fully_classified_frozen_drop(
        app, monkeypatch):
    """Control: with both axes classified (a weekly drop, a present
    five-hour baseline) the closed gate still gives the plan its explicit
    withheld disposition."""
    plan = _plan(five=40, seven=20)
    projection = _projection(five=40, seven=70)
    transition, resolution, writes, published = _resolve(
        app, monkeypatch, plan, projection, permitted=False)
    assert published == [] and resolution.status == "withheld"
    assert len(writes) == 1 and writes[0].publication is None
