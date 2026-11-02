"""#797 T3: the pure reducer with injected per-member refusal facts (spec
section 4.4 *In `_reduce_axis`*, 6 T3).

A refused member contributes nothing to `rising` or to the published value
of the same-window branch, at every rising return (plain, armed, cancelled,
reconciled); low evidence, supporter retraction, unanimity, pending
membership, `reduced_percent`, deadlines and attempt accounting keep the
unfiltered set; `CLASS_FLOOR` excludes refused members; the newer- and
older-window branches are unchanged. ``member_facts=None`` means every member
is eligible, and all-eligible facts are byte-identical to no facts.

The kernel is axis-generic, so these tests inject facts on either axis; only
the detached reducer's glue keeps the five-hour axis all-eligible until Task
5. The composition and provenance cases go through the real
`_bound_publication_plan`.
"""
from __future__ import annotations

import dataclasses
import importlib

import pytest

import _lib_statusline_candidates as lib


NOW = 10_000
WEEK = 2_000_000
FIVE = 20_000


@pytest.fixture
def sp():
    return importlib.import_module("_lib_stale_proof")


def _token(name: str) -> str:
    return (name * 64)[:64]


def _cand(name, *, seven=None, five=None, at=NOW, key=WEEK, fkey=FIVE):
    return lib.Candidate(
        token=_token(name), received_at=at,
        seven_day=(None if seven is None
                   else lib.AxisValue(float(seven), key, canonical_key=key)),
        five_hour=(None if five is None
                   else lib.AxisValue(float(five), fkey, canonical_key=fkey)))


def _db_axis(percent, *, key=WEEK, generation=0, source="api"):
    return lib.AxisProjection(float(percent), key, key, NOW - 100, source, generation)


def _facts(sp, refused=(), eligible=()):
    facts = {_token(n): sp.MemberFacts(True, "obs-" + n, True) for n in refused}
    facts.update({_token(n): sp.MemberFacts(False, None, False) for n in eligible})
    return facts


def _pending(*, low, supporters, db=None, key=WEEK, deadline=NOW + 100,
             stage="settling", attempts=0, contributors=None):
    observations = tuple(
        lib.SupportingObservation(
            observation_id=lib.observation_identity(
                _token(n), "sevenDay", lib.AxisValue(float(low), key)),
            token=_token(n), percent=float(low), raw_resets_at=key,
            received_at=NOW - 10)
        for n in supporters)
    return lib.PendingDrop(
        canonical_key=key, reduced_percent=float(low), first_seen_at=NOW - 50,
        kernel_stage=stage, attempts=attempts,
        contributors=contributors or {
            _token(n): lib.Contributor(NOW - 10, False) for n in supporters},
        retry_signature=None, deadline_at=deadline,
        retained=lib.RetainedLow(float(low), key, key, observations))


def _reduce(candidates, db_axis, pending=None, facts=None, *, now=NOW,
            evidence=None, axis="sevenDay"):
    return lib._reduce_axis(
        axis, tuple(candidates), db_axis, pending, None, now,
        scope=(lib.PROTOCOL_GENERATION, "a" * 32, axis),
        evidence=evidence, member_facts=facts)


# --- byte stability ----------------------------------------------------------


_SCENARIOS = {
    "plain-rise": ([("a", 82), ("b", 1)], 0, None),
    "no-rise": ([("a", 0)], 0, None),
    "pending-new": ([("a", 82), ("b", 1)], 50, None),
    "armed-rise": ([("a", 82), ("b", 30)], 50, dict(low=30, supporters="b")),
    "cancelled": ([("a", 82), ("b", 60)], 50, dict(low=30, supporters="b")),
    "deadline": ([("a", 82), ("b", 30)], 90,
                 dict(low=30, supporters="b", deadline=NOW - 1)),
    "unanimous": ([("b", 30)], 90, dict(low=30, supporters="b")),
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_all_eligible_facts_are_byte_identical_to_no_facts(sp, name):
    members, db, pending = _SCENARIOS[name]
    candidates = [_cand(n, seven=v) for n, v in members]
    pending = None if pending is None else _pending(**pending)
    if name == "unanimous":
        pending = dataclasses.replace(pending, contributors={
            _token("b"): lib.Contributor(NOW - 20, False)})
    bare = _reduce(candidates, _db_axis(db), pending)
    # Eligible facts, with and without a valid receipt, change nothing.
    for receipt_valid in (False, True):
        facts = {c.token: sp.MemberFacts(False, None, receipt_valid)
                 for c in candidates}
        assert _reduce(candidates, _db_axis(db), pending, facts) == bare
    # So do facts naming nobody present, and an empty mapping.
    assert _reduce(candidates, _db_axis(db), pending, {}) == bare
    assert _reduce(candidates, _db_axis(db), pending,
                   _facts(sp, refused="z")) == bare


def test_all_eligible_full_reduction_is_byte_identical(sp):
    candidates = (_cand("a", seven=82, five=40), _cand("b", seven=1, five=10))
    db = lib.DbProjection(_db_axis(5, key=FIVE, generation=""), _db_axis(0))
    control = lib.ControlState(db, {"fiveHour": None, "sevenDay": None})
    kwargs = dict(db=db, control=control, tombstones={}, now_epoch=NOW,
                  account_key="a" * 32)
    bare = lib.reduce_candidates(candidates, **kwargs)
    eligible = {c.token: sp.MemberFacts(False, None, True) for c in candidates}
    assert lib.reduce_candidates(
        candidates, member_facts={"sevenDay": eligible, "fiveHour": eligible},
        **kwargs) == bare
    assert lib.reduce_candidates(candidates, member_facts=None, **kwargs) == bare


# --- the rising returns --------------------------------------------------------


def test_plain_rise_publishes_the_eligible_value(sp):
    candidates = [_cand("a", seven=82), _cand("b", seven=1)]
    got = _reduce(candidates, _db_axis(0), facts=_facts(sp, refused="a"))
    assert (got.action, got.candidate.percent) == ("PUBLISH_DB", 1.0)
    assert got.candidate.canonical_key == WEEK and got.pending is None
    # Unfiltered, the refused member's value would have published.
    assert _reduce(candidates, _db_axis(0)).candidate.percent == 82.0


def test_a_refused_member_alone_supplies_no_rise(sp):
    got = _reduce([_cand("a", seven=82)], _db_axis(0),
                  facts=_facts(sp, refused="a"))
    assert (got.action, got.candidate, got.pending) == ("NOOP", None, None)
    # The refused value still counts as present (not a drop): no pending.
    got = _reduce([_cand("a", seven=82), _cand("b", seven=0)], _db_axis(0),
                  facts=_facts(sp, refused="a"))
    assert (got.action, got.candidate, got.pending) == ("NOOP", None, None)


def test_reconciled_armed_rise_publishes_the_eligible_value_and_keeps_the_unfiltered_pending(sp):
    pending = _pending(low=30, supporters="b")
    candidates = [_cand("a", seven=82), _cand("c", seven=60), _cand("b", seven=30)]
    got = _reduce(candidates, _db_axis(50), pending, _facts(sp, refused="a"))
    assert (got.action, got.candidate.percent) == ("PUBLISH_DB", 60.0)
    # `reduced_percent` and the contributors stay the unfiltered set's.
    assert got.pending.reduced_percent == 82.0
    assert set(got.pending.contributors) == {c.token for c in candidates}
    assert got.pending.deadline_at == pending.deadline_at
    # With only a refused member above the baseline nothing rises, and the
    # pending drop is reconciled exactly as the unfiltered NOOP path would.
    got = _reduce([_cand("a", seven=82), _cand("b", seven=30)], _db_axis(50),
                  pending, _facts(sp, refused="a"))
    assert got.action in ("NOOP", "WRITE_CONTROL") and got.candidate is None
    assert got.pending is not None and got.pending.reduced_percent == 82.0


def test_cancelled_return_publishes_only_an_eligible_rise(sp):
    # Every present supporter is at or above the baseline: cancelled.
    pending = _pending(low=30, supporters="b")
    got = _reduce([_cand("a", seven=82), _cand("b", seven=60)], _db_axis(50),
                  pending, _facts(sp, refused="a"))
    assert (got.action, got.candidate.percent, got.pending) == (
        "PUBLISH_DB", 60.0, None)
    # Cancelled by retraction: the refused supporter's own report still
    # retracts (unfiltered),
    # but its value never publishes.
    got = _reduce([_cand("b", seven=82)], _db_axis(50), pending,
                  _facts(sp, refused="b"))
    assert (got.action, got.candidate, got.pending) == ("WRITE_CONTROL", None, None)


def test_recorded_drop_cancelled_return_uses_the_eligible_value(sp):
    # The database already holds the drop: the second cancelled return.
    pending = _pending(low=30, supporters="b")
    got = _reduce([_cand("a", seven=82), _cand("c", seven=40)], _db_axis(30),
                  pending, _facts(sp, refused="a"))
    assert (got.action, got.candidate.percent) == ("PUBLISH_DB", 40.0)
    got = _reduce([_cand("a", seven=82)], _db_axis(30), pending,
                  _facts(sp, refused="a"))
    assert (got.action, got.candidate) == ("WRITE_CONTROL", None)


# --- unfiltered sets ----------------------------------------------------------


def test_low_supporter_and_unanimity_sets_stay_unfiltered(sp):
    # A refused member below the baseline still supplies low evidence.
    got = _reduce([_cand("a", seven=10)], _db_axis(50),
                  facts=_facts(sp, refused="a"))
    assert got.action == "WRITE_CONTROL" and got.pending.retained.percent == 10.0
    assert got.pending.retained.observations[0].token == _token("a")
    # Unanimity is over the unfiltered reduced value: a refused high above
    # the baseline keeps the drop from publishing by consensus, and only the
    # deadline publishes the retained low.
    contributors = {_token("a"): lib.Contributor(NOW - 20, False),
                    _token("b"): lib.Contributor(NOW - 20, False)}
    pending = _pending(low=30, supporters="b", contributors=contributors)
    members = [_cand("a", seven=95), _cand("b", seven=30)]
    refused = _reduce(members, _db_axis(90), pending, _facts(sp, refused="a"))
    # Without the facts the 95 is an armed rise; refused, nothing publishes,
    # the pending keeps the unfiltered maximum and spends no attempt.
    assert _reduce(members, _db_axis(90), pending).candidate.percent == 95.0
    assert refused.candidate is None and refused.pending.attempts == 0
    assert refused.pending.reduced_percent == 95.0
    assert set(refused.pending.contributors) == {_token("a"), _token("b")}
    expired = dataclasses.replace(pending, deadline_at=NOW - 1)
    published = _reduce(members, _db_axis(90), expired, _facts(sp, refused="a"))
    assert (published.action, published.candidate.percent) == ("PUBLISH_DB", 30.0)
    assert published.pending.attempts == 1
    # A drop consensus publishes exactly as today (spec 4.4 composition case
    # 2), the unfiltered reduced value included.
    below = [_cand("a", seven=80), _cand("b", seven=30)]
    consensus = _reduce(below, _db_axis(90), pending, _facts(sp, refused="a"))
    assert consensus == _reduce(below, _db_axis(90), pending)
    assert (consensus.action, consensus.candidate.percent) == ("PUBLISH_DB", 80.0)


def test_pending_deadline_never_moves_for_a_refused_member(sp):
    pending = _pending(low=30, supporters="b", deadline=NOW + 40)
    got = _reduce([_cand("a", seven=95), _cand("b", seven=30)], _db_axis(90),
                  pending, _facts(sp, refused="a"))
    assert got.pending.deadline_at == NOW + 40
    assert got.pending.first_seen_at == pending.first_seen_at


# --- ties ----------------------------------------------------------------------


def test_refused_and_eligible_equal_value_ties_publish_the_value(sp):
    members = [_cand("a", seven=40), _cand("b", seven=40)]
    got = _reduce(members, _db_axis(0), facts=_facts(sp, refused="a"))
    assert (got.action, got.candidate.percent) == ("PUBLISH_DB", 40.0)
    got = _reduce(members, _db_axis(0), facts=_facts(sp, refused="ab"))
    assert (got.action, got.candidate) == ("NOOP", None)


# --- CLASS_FLOOR and the other window branches ---------------------------------


def _floor(group=WEEK, floor=NOW - 50):
    return lib.AxisEvidence(
        lib.EVIDENCE_FLOOR_RETIRED, account_key="a" * 32, axis="sevenDay",
        group_key=group, floor_epoch=floor, floor_identity="f", anchor_raw=group)


def test_class_floor_excludes_refused_members(sp):
    members = [_cand("a", seven=82, at=NOW), _cand("b", seven=5, at=NOW)]
    got = _reduce(members, None, evidence=_floor(), facts=_facts(sp, refused="a"))
    assert (got.action, got.candidate.percent) == ("PUBLISH_DB", 5.0)
    # Only refused members after the floor: stale (the older-window
    # disposition), nothing published.
    got = _reduce([members[0]], None, evidence=_floor(),
                  facts=_facts(sp, refused="a"))
    assert (got.action, got.candidate) == ("NOOP", None)
    # An eligible member keeps the receipt-floor test.
    early = _cand("c", seven=9, at=NOW - 60)
    got = _reduce([members[0], early], None, evidence=_floor(),
                  facts=_facts(sp, refused="a", eligible="c"))
    assert (got.action, got.candidate) == ("NOOP", None)


def test_newer_and_older_window_branches_are_unchanged(sp):
    newer = [_cand("a", seven=82, key=WEEK + 7 * 86400)]
    assert _reduce(newer, _db_axis(50), facts=_facts(sp, refused="a")) == \
        _reduce(newer, _db_axis(50))
    assert _reduce(newer, _db_axis(50)).action == "PUBLISH_DB"
    older = [_cand("a", seven=82, key=WEEK - 7 * 86400)]
    assert _reduce(older, _db_axis(50), facts=_facts(sp, refused="a")) == \
        _reduce(older, _db_axis(50))


# --- composition, projection fill and provenance (real glue) --------------------


class _Binding:
    account_key = "a" * 32

    def document(self):
        return {"accountKey": self.account_key}


def _activity(instant="2026-10-22T09:00:00Z"):
    import _lib_provider_activity as pa
    return pa.ActivityWitness(1, 1, "known", instant, None, 1, 1, "0" * 64)


def _plan(sp, candidates, db, facts):
    import _cctally_statusline as sl
    candidates = tuple(dataclasses.replace(c, activity=_activity()) for c in candidates)
    control = lib.ControlState(db, {"fiveHour": None, "sevenDay": None})
    decision = lib.reduce_candidates(
        candidates, db=db, control=control, tombstones={}, now_epoch=NOW,
        account_key=_Binding.account_key, member_facts=facts)
    refused = {axis: frozenset(t for t, f in (facts or {}).get(axis, {}).items()
                               if f.refused) for axis in lib.AXES}
    return decision, sl._bound_publication_plan(
        decision, db, candidates, _Binding(), generation=1, now_epoch=NOW,
        intake_sequence=0, refused=refused)


@pytest.fixture
def glue(monkeypatch, tmp_path):
    from conftest import load_script, redirect_paths
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _future_db(seven, five):
    return lib.DbProjection(
        lib.AxisProjection(float(five), NOW + 3600, NOW + 3600, NOW - 100, "api", ""),
        lib.AxisProjection(float(seven), NOW + 3 * 86400, NOW + 3 * 86400,
                           NOW - 100, "api", 0),
        account_key=_Binding.account_key)


def _fut(name, *, seven=None, five=None):
    return _cand(name, seven=seven, five=five, key=NOW + 3 * 86400, fkey=NOW + 3600)


def test_refused_weekly_rise_takes_the_projection_fill(sp, glue):
    db = _future_db(seven=0, five=5)
    members = [_fut("a", seven=82, five=10)]
    facts = {"sevenDay": _facts(sp, refused="a")}
    decision, plan = _plan(sp, members, db, facts)
    assert decision.plan.seven_day is None and decision.plan.five_hour.percent == 10.0
    assert plan["sevenDay"]["percent"] == 0.0
    assert plan["provenance"]["sevenDay"]["kind"] == "projection"
    assert plan["provenance"]["fiveHour"]["kind"] == "candidate"


def test_refused_five_hour_rise_takes_the_projection_fill(sp, glue):
    db = _future_db(seven=10, five=5)
    members = [_fut("a", seven=20, five=40)]
    facts = {"fiveHour": _facts(sp, refused="a")}
    decision, plan = _plan(sp, members, db, facts)
    assert decision.plan.five_hour is None and decision.plan.seven_day.percent == 20.0
    assert plan["fiveHour"]["percent"] == 5.0
    assert plan["provenance"]["fiveHour"]["kind"] == "projection"
    assert plan["provenance"]["sevenDay"]["kind"] == "candidate"


def test_provenance_names_the_eligible_winner_of_a_tie(sp, glue):
    db = _future_db(seven=0, five=5)
    # "a" sorts first, so the parent's provenance would name it.
    members = [_fut("a", seven=40), _fut("b", seven=40)]
    facts = {"sevenDay": _facts(sp, refused="a")}
    _decision, plan = _plan(sp, members, db, facts)
    assert plan["provenance"]["sevenDay"]["token"] == _token("b")
    assert plan["provenance"]["sevenDay"]["providerActivityStatus"] == "known"
    _decision, bare = _plan(sp, members, db, None)
    assert bare["provenance"]["sevenDay"]["token"] == _token("a")


def test_no_facts_plan_is_byte_identical_to_the_parent_signature(sp, glue):
    import _cctally_statusline as sl
    db = _future_db(seven=0, five=5)
    candidates = tuple(dataclasses.replace(c, activity=_activity())
                       for c in [_fut("a", seven=40, five=10)])
    control = lib.ControlState(db, {"fiveHour": None, "sevenDay": None})
    decision = lib.reduce_candidates(candidates, db=db, control=control,
                                     tombstones={}, now_epoch=NOW,
                                     account_key=_Binding.account_key)
    kwargs = dict(generation=1, now_epoch=NOW, intake_sequence=0)
    assert sl._bound_publication_plan(
        decision, db, candidates, _Binding(), **kwargs) == \
        sl._bound_publication_plan(decision, db, candidates, _Binding(),
                                   refused={}, **kwargs)


@pytest.mark.parametrize("path", ["consensus", "deadline"])
def test_a_drop_whose_only_matching_member_is_refused_keeps_candidate_provenance(
        sp, glue, path):
    """Review RV-3 (spec 4.4 composition case 2): an accepted drop publishes
    exactly as today. Refusal filters only a rise's winner, so a consensus or
    deadline drop whose only matching member is refused keeps that member's
    `candidate` provenance and activity — the plan is byte-identical to the
    one without refusal facts, never `retained_low`."""
    import _cctally_statusline as sl
    key = NOW + 3 * 86400
    db = _future_db(seven=90, five=5)
    # The deadline path: "c" stays an absent, unsatisfied contributor, so no
    # consensus forms and the expired deadline publishes the retained low.
    contributors = {_token(n): lib.Contributor(NOW - 20, False)
                    for n in ("ab" if path == "consensus" else "bc")}
    pending = _pending(low=30, supporters="b", key=key, contributors=contributors,
                       deadline=NOW - 1 if path == "deadline" else NOW + 100)
    refused_name = "a" if path == "consensus" else "b"
    members = ([_fut("a", seven=80), _fut("b", seven=30)] if path == "consensus"
               else [_fut("b", seven=30)])
    candidates = tuple(dataclasses.replace(c, activity=_activity())
                       for c in members)
    control = lib.ControlState(db, {"fiveHour": None, "sevenDay": pending})
    plans = []
    for facts in (None, {"sevenDay": _facts(sp, refused=refused_name)}):
        decision = lib.reduce_candidates(
            candidates, db=db, control=control, tombstones={}, now_epoch=NOW,
            account_key=_Binding.account_key, member_facts=facts)
        plans.append(sl._bound_publication_plan(
            decision, db, candidates, _Binding(), generation=1, now_epoch=NOW,
            intake_sequence=0, refused=sl._refused_tokens(facts)))
    bare, refused = plans
    published = 80.0 if path == "consensus" else 30.0
    assert bare["sevenDay"]["percent"] == published
    provenance = refused["provenance"]["sevenDay"]
    assert provenance["kind"] == "candidate", provenance
    assert provenance["token"] == _token(refused_name)
    assert provenance["providerActivityStatus"] == "known"
    assert refused == bare


# --- #1019: the drop side (spec 4.2, L5, L6, K3) ---------------------------------


def _drop_facts(sp, *names):
    """Facts refusing the LOW of each named member (#1019), rise facts eligible."""
    return {_token(n): sp.MemberFacts(False, None, False, drop_refused=True,
                                      drop_proof_observation_id="o:" + n)
            for n in names}


def _witnessed(name, *, low, instant, key=WEEK, received=NOW - 10):
    import _lib_provider_activity as pa
    value = lib.AxisValue(float(low), key)
    return lib.SupportingObservation(
        observation_id=lib.observation_identity(_token(name), "sevenDay", value),
        token=_token(name), percent=float(low), raw_resets_at=key,
        received_at=received,
        witness=pa.ActivityWitness(1, 1, "known", instant, None, 1, 1, "0" * 64))


def _reduce_refuting(candidates, db_axis, pending, refuted, *, facts=None, now=NOW):
    return lib._reduce_axis(
        "sevenDay", tuple(candidates), db_axis, pending, None, now,
        scope=(lib.PROTOCOL_GENERATION, "a" * 32, "sevenDay"),
        member_facts=facts, refuted_supports=frozenset(refuted))


def test_rise_eligible_member_at_baseline_still_dissents(sp):
    """Accuracy (P-R1, S4's flap): a fresh member's drop stays pending while
    a rise-eligible member at the baseline is present, then publishes once
    it leaves the active window; a drop-refused member at the baseline is
    rise-ineligible and dissents from nothing (P-R2)."""
    pending = dataclasses.replace(_pending(low=30, supporters="b"), contributors={
        _token("b"): lib.Contributor(NOW - 20, False)})
    present = [_cand("a", seven=50), _cand("b", seven=30)]
    held = _reduce(present, _db_axis(50), pending)
    assert held.action != "PUBLISH_DB", held
    assert held.pending is not None and held.pending.deadline_at == pending.deadline_at
    alone = _reduce([_cand("b", seven=30)], _db_axis(50), pending)
    assert (alone.action, alone.candidate.percent) == ("PUBLISH_DB", 30.0)
    refused = _reduce(present, _db_axis(50), pending, facts=_drop_facts(sp, "a"))
    assert (refused.action, refused.candidate.percent) == ("PUBLISH_DB", 30.0)


def test_drop_refused_member_never_rises_or_arms(sp):
    """Accuracy (P-R2): a drop-refused member is ineligible in both
    directions — its reading above the baseline is no rise, its reading
    below arms no drop — while the displayed maximum is unchanged."""
    above = _reduce([_cand("a", seven=74)], _db_axis(0), facts=_drop_facts(sp, "a"))
    assert (above.action, above.candidate) == ("NOOP", None)
    below = _reduce([_cand("a", seven=18)], _db_axis(51), facts=_drop_facts(sp, "a"))
    assert (below.action, below.pending) == ("NOOP", None)
    # An eligible peer's rise still publishes beside it.
    mixed = _reduce([_cand("a", seven=74), _cand("b", seven=10)], _db_axis(0),
                    facts=_drop_facts(sp, "a"))
    assert (mixed.action, mixed.candidate.percent) == ("PUBLISH_DB", 10.0)


def test_empty_drop_eligible_set_never_unanimous(sp):
    """Accuracy (spec 4.2): with every present member drop-refused a pending
    drop neither publishes by consensus nor arms afresh."""
    pending = dataclasses.replace(_pending(low=30, supporters="b"), contributors={
        _token("b"): lib.Contributor(NOW - 20, True)})
    got = _reduce([_cand("b", seven=30)], _db_axis(50), pending,
                  facts=_drop_facts(sp, "b"))
    assert got.action != "PUBLISH_DB", got


def test_class_floor_excludes_drop_refused_members(sp):
    """Accuracy (P-R4): the first post-floor reading of a floor-retired axis
    excludes a drop-refused member as it excludes a receipt-refused one."""
    got = _reduce([_cand("a", seven=74, at=NOW)], None, evidence=_floor(),
                  facts=_drop_facts(sp, "a"))
    assert (got.action, got.candidate) == ("NOOP", None)
    got = _reduce([_cand("a", seven=74, at=NOW), _cand("b", seven=5, at=NOW)], None,
                  evidence=_floor(), facts=_drop_facts(sp, "a"))
    assert (got.action, got.candidate.percent) == ("PUBLISH_DB", 5.0)


def test_hold_pending_cancels_refuted_retained_low():
    """L5 (accuracy: `_hold_pending` publishing a refuted low): the only
    supporter aged out and a later sample refutes its observation; at the
    deadline nothing publishes and the pending drop is cancelled."""
    pending = _pending(low=18, supporters="b", deadline=NOW - 1)
    refuted = {lib.support_identity(o) for o in pending.retained.observations}
    kept = _reduce_refuting([], _db_axis(51), pending, ())
    assert (kept.action, kept.candidate.percent) == ("PUBLISH_DB", 18.0)
    got = _reduce_refuting([], _db_axis(51), pending, refuted)
    assert (got.action, got.candidate, got.pending) == ("WRITE_CONTROL", None, None)


def test_partial_refutation_keeps_deadline_and_surviving_support():
    """L5: one refuted and one eligible supporter — the low stays pending
    with its original deadline and only the surviving observation, and
    publishes as that support at the deadline."""
    pending = _pending(low=18, supporters="bc", deadline=NOW + 50)
    b, c = pending.retained.observations
    refuted = {lib.support_identity(b if b.token == _token("b") else c)}
    got = _reduce_refuting([], _db_axis(51), pending, refuted)
    assert got.action == "WRITE_CONTROL", got
    assert got.pending.deadline_at == pending.deadline_at
    assert [o.token for o in got.pending.retained.observations] == [_token("c")]
    late = _reduce_refuting([], _db_axis(51), got.pending, refuted, now=NOW + 50)
    assert (late.action, late.candidate.percent) == ("PUBLISH_DB", 18.0)
    assert [o.token for o in late.pending.retained.observations] == [_token("c")]


def test_present_refused_supporter_withdraws_only_its_observation(sp):
    """L6: a present drop-refused supporter withdraws its own supporting
    observation, never its genuine peer's; the published drop's provenance
    names the peer's observation."""
    import _cctally_statusline as sl
    pending = dataclasses.replace(
        _pending(low=18, supporters="bc", deadline=NOW - 1),
        contributors={_token("b"): lib.Contributor(NOW - 20, False),
                      _token("c"): lib.Contributor(NOW - 20, False)})
    stale = next(o for o in pending.retained.observations if o.token == _token("b"))
    refuted = {lib.support_identity(stale)}
    candidates = [_cand("b", seven=18), _cand("c", seven=18)]
    got = _reduce_refuting(candidates, _db_axis(51), pending, refuted,
                           facts=_drop_facts(sp, "b"))
    assert (got.action, got.candidate.percent) == ("PUBLISH_DB", 18.0)
    assert [o.token for o in got.pending.retained.observations] == [_token("c")]
    db = lib.DbProjection(None, _db_axis(51))
    candidates = tuple(dataclasses.replace(c, activity=_activity()) for c in candidates)
    decision = lib.reduce_candidates(
        candidates, db=db, control=lib.ControlState(
            db, {"fiveHour": None, "sevenDay": pending}),
        tombstones={}, now_epoch=NOW, account_key=_Binding.account_key,
        member_facts={"sevenDay": _drop_facts(sp, "b")},
        refuted_supports={"sevenDay": frozenset(refuted)})
    assert decision.action == "PUBLISH_DB"
    plan = sl._bound_publication_plan(
        decision, db, candidates, _Binding(), generation=3, now_epoch=NOW,
        intake_sequence=0, refused={},
        drop_refused={"sevenDay": frozenset({_token("b")})})
    source = plan["provenance"]["sevenDay"]
    assert (source["kind"], source["token"]) == ("candidate", _token("c"))


def test_same_observation_different_witness_are_separate_supports():
    """K3 (accuracy: aliasing discards a fresh same-value support): a
    repeated observation identity with a later witness is a separate
    support, kept over the older one, so a sample refuting only the older
    bound leaves the drop supported; the record stays one support per
    observation identity, however many responses repeat the reading."""
    old = _witnessed("b", low=18, instant="2026-10-25T10:12:22.165Z")
    fresh = _witnessed("b", low=18, instant="2026-10-29T20:30:00.000Z")
    assert lib.support_identity(old) != lib.support_identity(fresh)
    assert old.observation_id == fresh.observation_id
    merged = lib._merged_retained(
        lib.RetainedLow(18.0, WEEK, WEEK, (old,)), canonical_key=WEEK, low=18.0,
        observations=(fresh,))
    assert merged.observations == (fresh,)
    # An older witness never displaces a later one, and an unchanged one
    # leaves the record byte-identical.
    assert lib._merged_retained(merged, canonical_key=WEEK, low=18.0,
                                observations=(old,)) == merged
    assert lib._merged_retained(merged, canonical_key=WEEK, low=18.0,
                                observations=(fresh,)) == merged
    pending = dataclasses.replace(_pending(low=18, supporters="b", deadline=NOW - 1),
                                  retained=merged)
    got = _reduce_refuting([], _db_axis(51), pending, {lib.support_identity(old)})
    assert (got.action, got.candidate.percent) == ("PUBLISH_DB", 18.0)
    # Bounded growth: a thousand responses at one reading keep one support.
    retained = merged
    for i in range(1000):
        retained = lib._merged_retained(
            retained, canonical_key=WEEK, low=18.0, observations=(_witnessed(
                "b", low=18, instant=f"2026-10-29T21:{i // 60:02d}:{i % 60:02d}.000Z"),))
    assert len(retained.observations) == 1


def test_proof_work_covers_drop_rise_and_floor_retired_readings():
    """L9 coverage predicate (spec 4.4, P-R3, P-R4; accuracy: an unproven
    stale reading published as "no proof" over an uncovered projection):
    a reading below or above the same window's baseline is proof work, an
    equal one is not; on a floor-retired axis (no baseline) a reading
    received at or after the floor is work (a potential first post-floor
    publication) and one received before it is not; retained support and an
    unappended plan always are."""
    import _cctally_statusline as sl
    base = _db_axis(50)

    def work(percent, received=NOW):
        return sl._DropWork(frozenset({("sevenDay", WEEK, float(percent), received)}))

    assert work(18).needs_samples(WEEK, base)
    assert work(74).needs_samples(WEEK, base)
    assert not work(50).needs_samples(WEEK, base)
    assert not work(74).needs_samples(WEEK + 7 * 86400, base)
    assert work(74).needs_samples(WEEK, None, NOW - 50)
    assert work(74, received=NOW - 50).needs_samples(WEEK, None, NOW - 50)
    assert not work(74, received=NOW - 51).needs_samples(WEEK, None, NOW - 50)
    assert not work(74).needs_samples(WEEK, None, None)
    assert sl._DropWork(retained=True).needs_samples(None, None)
    assert sl._DropWork(plan=True).needs_samples(None, None)
