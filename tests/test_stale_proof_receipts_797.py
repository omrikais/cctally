"""#797 T15: provider-sample proofs and accounting receipts (spec section
4.4, 6 T15; D7, D7a-D7c).

The first part pins the pure kernel `_lib_stale_proof`: the 1.0 pp tolerance,
the strict, precision-aware order of `q` against `t_m` and `e_σ`, the window
and reset bounds, both earning paths, every weekly row of the receipt
validity table, refusal, pruning (an invalidated receipt is never revived),
eviction (eviction means eligible), the receipt codec and the compact sample
set the render path caches. The second part drives the real status-line
intake and detached reducer (`tests/_stale_contributor_trace.py`) for the
earning and checkpointing rules that need the derivation.

The five-hour rows (Task 5) follow the weekly ones: path (i) only, the
statusline-source credit row, the cross-source `api` credit row with its
three conditions, and Codex's example.
"""
from __future__ import annotations

import dataclasses
import importlib
import itertools
import json
import sys
import time

import pytest

import _lib_oauth_samples as samples_lib


ACCOUNT = "a" * 32
RESET = 1792818000  # 2026-10-24T05:00:00Z, on the hour
RESET_ISO = "2026-10-24T05:00:00Z"
FLOOR = 1792400000  # 2026-10-19T08:53:20Z
T_M = "2026-10-21T22:50:14.000Z"
E = "2026-10-22T09:20:00.000000Z"


@pytest.fixture
def sp():
    return importlib.import_module("_lib_stale_proof")


def _token(name: str) -> str:
    return (name * 64)[:64]


def sigma(sp, name="a", *, percent=74.0, t_m=T_M, window=RESET, axis="sevenDay",
          evidence=1, parser=1):
    return sp.SampleIdentity(
        token=_token(name), activity_at_utc=t_m, axis=axis, percent=float(percent),
        window=window, evidence_revision=evidence, parser_revision=parser)


def member(sp, *args, reset=RESET, **kwargs):
    return sp.Member(sigma(sp, *args, **kwargs), advertised_reset_epoch=reset)


def sample(obs, *, q, weekly, captured=None, reset=RESET_ISO, account=ACCOUNT):
    return samples_lib.OAuthSample(
        account_key=account, observation_id=obs, requested_at_utc=q,
        captured_at_utc=captured or (q[:19] + "Z"), weekly_percent=float(weekly),
        weekly_resets_at_utc=reset, five_hour_percent=None,
        five_hour_resets_at_utc=None, five_hour_window_key=None)


def accounted(sp, percent=83.0, *, credits=(), support="o:support", floor=FLOOR):
    return sp.Accounted(
        account_key=ACCOUNT, axis="sevenDay", window=RESET,
        percent=None if percent is None else float(percent), support_id=support,
        support_hash="h" * 64, floor_epoch=floor, credits=tuple(credits),
        coverage="c" * 64)


def credit(sp, event_id="wr:x", *, kind="provider", q=None, revision="r1"):
    return sp.CreditTransition(
        axis="sevenDay", event_id=event_id, event_revision=revision,
        causal_kind=kind, requested_at_utc=q)


def receipt(sp, m=None, *, path="provider_support", e=E, credits=(), index="ix"):
    m = m or member(sp)
    earned = sp.earn_receipts(
        [m], accounted(sp, credits=credits,
                       percent=m.sigma.percent if path == "maximal" else 83.0),
        [sample("o:b", q="2026-10-22T09:04:43.000000Z", weekly=83)],
        completed_at_utc=e, index_identity=index)
    assert len(earned) == 1 and earned[0].path == path
    return earned[0]


# --- constants and identity ------------------------------------------------------


def test_the_specified_tolerance_and_revisions(sp):
    assert sp.TOLERANCE_PP == 1.0
    import _lib_provider_activity as pa
    assert sp.SUPPORTED_REVISIONS == ((pa.EVIDENCE_REVISION, pa.PARSER_REVISION),)


def test_sample_identity_comes_only_from_known_supported_evidence(sp):
    import _lib_provider_activity as pa

    def witness(status="known", instant="2026-10-21T22:50:14.000Z", reason=None,
                evidence=1, parser=1):
        return pa.ActivityWitness(evidence, parser, status, instant, reason, 1, 1, "0" * 64)

    got = sp.sample_identity(_token("a"), witness(), axis="sevenDay",
                             percent=74.0, window=RESET)
    assert got == sigma(sp)
    assert sp.sample_identity(_token("a"), witness("unknown", None, "missing"),
                              axis="sevenDay", percent=74.0, window=RESET) is None
    # An unsupported evidence or parser revision is unknown evidence.
    for revisions in ((2, 1), (1, 2)):
        assert sp.sample_identity(
            _token("a"), witness(evidence=revisions[0], parser=revisions[1]),
            axis="sevenDay", percent=74.0, window=RESET) is None
    assert sp.sample_identity(_token("a"), None, axis="sevenDay", percent=74.0,
                              window=RESET) is None
    # A different frozen instant is a different sample.
    assert sigma(sp, t_m="2026-10-21T22:50:15.000Z") != sigma(sp)
    assert sigma(sp).key() != sigma(sp, t_m="2026-10-21T22:50:15.000Z").key()


# --- the proof ------------------------------------------------------------------


Q_LATE = "2026-10-22T09:31:22.000000Z"


def test_tolerance_is_strictly_more_than_one_point(sp):
    s = sigma(sp)
    assert sp.proven_stale(s, [sample("o:1", q=Q_LATE, weekly=73)], after_utc=T_M) is None
    proof = sample("o:2", q=Q_LATE, weekly=72.99)
    assert sp.proven_stale(s, [proof], after_utc=T_M) == proof


def test_proof_orders_q_never_the_capture_instant(sp):
    # Mutation control (capture instant used in place of q): requested
    # before t_m, captured after it, proves nothing.
    s = sigma(sp)
    early = sample("o:1", q="2026-10-21T22:50:13.000000Z", weekly=0,
                   captured="2026-10-21T22:59:00Z")
    assert sp.proven_stale(s, [early], after_utc=T_M) is None
    assert sp.proven_stale(s, [dataclasses.replace(
        early, requested_at_utc="2026-10-21T22:50:15.000000Z")], after_utc=T_M)


@pytest.mark.parametrize("t_m,q,proves", [
    ("2026-10-21T22:50:14.000Z", "2026-10-21T22:50:14.000000Z", False),   # equal
    ("2026-10-21T22:50:14Z", "2026-10-21T22:50:14.500000Z", False),       # overlap
    ("2026-10-21T22:50:14Z", "2026-10-21T22:50:15.000000Z", True),
    ("2026-10-21T22:50:14.999Z", "2026-10-21T22:50:14.999500Z", False),   # overlap
    ("2026-10-21T22:50:14.999Z", "2026-10-21T22:50:15.000000Z", True),
    ("2026-10-21T22:50:14.123456789Z", "2026-10-21T22:50:14.123457Z", True),
])
def test_equal_or_overlapping_instants_never_order(sp, t_m, q, proves):
    s = sigma(sp, t_m=t_m)
    got = sp.proven_stale(s, [sample("o:1", q=q, weekly=0)], after_utc=t_m)
    assert (got is not None) is proves


def test_window_and_advertised_reset_bound_the_proof(sp):
    s = sigma(sp)
    other = sample("o:1", q=Q_LATE, weekly=0, reset="2026-10-31T05:00:00Z")
    assert sp.proven_stale(s, [other], after_utc=T_M) is None
    # Captured at or after the member's advertised reset: no proof, even in
    # the normalized window (the raw reset jitters below the hour).
    late = sample("o:2", q="2026-10-24T04:59:50.000000Z", weekly=0,
                  captured="2026-10-24T04:59:59Z")
    assert sp.proven_stale(s, [late], after_utc=T_M,
                           advertised_reset_epoch=RESET - 30) is None
    assert sp.proven_stale(s, [late], after_utc=T_M,
                           advertised_reset_epoch=RESET) == late


def test_proof_after_e_sigma_needs_both_bounds(sp):
    s = sigma(sp)
    between = sample("o:1", q="2026-10-22T09:00:00.000000Z", weekly=0)
    assert sp.proven_stale(s, [between], after_utc=T_M) == between
    assert sp.proven_stale(s, [between], after_utc=E) is None


# --- earning --------------------------------------------------------------------


Q_B = "2026-10-22T09:04:43.000000Z"


def test_earning_i_is_exact_equality_and_ties_earn_independently(sp):
    a, b, c = member(sp, "a", percent=83), member(sp, "b", percent=83), \
        member(sp, "c", percent=82.9)
    earned = sp.earn_receipts([a, b, c], accounted(sp, 83), [],
                              completed_at_utc=E, index_identity="ix")
    assert sorted((r.sigma.token, r.path) for r in earned) == [
        (_token("a"), "maximal"), (_token("b"), "maximal")]
    r = earned[0]
    assert (r.completed_at_utc, r.index_identity, r.account_key, r.window) == (
        E, "ix", ACCOUNT, RESET)
    assert (r.support_id, r.accounted_percent) == ("o:support", 83.0)
    # No accounted value (a floor-retired segment): nothing earns by (i).
    assert sp.earn_receipts([a], accounted(sp, None), [], completed_at_utc=E,
                            index_identity="ix") == ()


def test_earning_ii_needs_a_raw_ordered_provider_sample_at_or_above(sp):
    m74, m82 = member(sp, "a", percent=74), member(sp, "b", percent=82,
                                                  t_m="2026-10-22T08:00:00.000Z")
    b83 = sample("o:b", q=Q_B, weekly=83)
    earned = sp.earn_receipts([m74, m82], accounted(sp, 83), [b83],
                              completed_at_utc=E, index_identity="ix")
    assert [(r.sigma.token, r.path, r.provider_observation_id,
             r.provider_percent, r.provider_requested_at_utc) for r in earned] == [
        (_token("a"), "provider_support", "o:b", 83.0, Q_B),
        (_token("b"), "provider_support", "o:b", 83.0, Q_B)]
    # Requested before t_m: no support.
    early = sample("o:e", q="2026-10-22T07:59:59.000000Z", weekly=90)
    assert [r.sigma.token for r in sp.earn_receipts(
        [m74, m82], accounted(sp, 83), [early], completed_at_utc=E,
        index_identity="ix")] == [_token("a")]
    # Raw below the member: no support, whatever the stored (MAX-clamped)
    # value says.
    raw80 = sample("o:r", q=Q_B, weekly=80)
    assert [r.sigma.token for r in sp.earn_receipts(
        [m74, m82], accounted(sp, 83), [raw80], completed_at_utc=E,
        index_identity="ix")] == [_token("a")]
    # Captured before the current segment's floor: not this segment's support.
    pre = sample("o:p", q="2026-10-19T08:00:00.000000Z", weekly=95)
    assert sp.earn_receipts([member(sp, "a", percent=74,
                                    t_m="2026-10-19T07:00:00.000Z")],
                            accounted(sp, 83), [pre], completed_at_utc=E,
                            index_identity="ix") == ()


def test_earning_ii_supports_the_completed_accounting_only(sp):
    """B supports the COMPLETED accounting of the segment: the segment's
    latest admitted sample, never an older, higher one that the accounting
    has since moved below, and never a member above the accounted value
    (found by T1 S9: after a manual credit to 10 and a genuine 12, a stale
    60 earned support from a pre-credit 60 sample captured inside the
    hour-floored segment)."""
    m60 = member(sp, "s", percent=60)
    b60 = sample("o:60", q="2026-10-22T09:04:00.000000Z", weekly=60)
    b12 = sample("o:12", q="2026-10-22T09:11:13.000000Z", weekly=12)
    assert sp.earn_receipts([m60], accounted(sp, 12), [b60, b12],
                            completed_at_utc=E, index_identity="ix") == ()
    # The latest raw sample, not the clamped accounting nor an older 83.
    m74, m82 = member(sp, "a", percent=74), member(sp, "b", percent=82)
    b83 = sample("o:83", q="2026-10-22T09:00:00.000000Z", weekly=83)
    b80 = sample("o:80", q="2026-10-22T09:02:00.000000Z", weekly=80)
    assert [(r.sigma.token, r.provider_observation_id) for r in sp.earn_receipts(
        [m74, m82], accounted(sp, 83), [b83, b80], completed_at_utc=E,
        index_identity="ix")] == [(_token("a"), "o:80")]
    # A member above the accounted value is a rise, not supported accounting.
    assert sp.earn_receipts([m74], accounted(sp, 70), [b83], completed_at_utc=E,
                            index_identity="ix") == ()


def test_earning_never_comes_from_a_numerically_dominating_member(sp):
    # Mutation control (a receipt earned by numerical domination of a lower
    # member): 74 below an accounted 82 with no provider support earns
    # nothing — 82's own accounting is not 74's.
    m74 = member(sp, "a", percent=74)
    assert sp.earn_receipts([m74, member(sp, "b", percent=82)], accounted(sp, 82),
                            [], completed_at_utc=E, index_identity="ix") != () and \
        all(r.sigma.token != _token("a") for r in sp.earn_receipts(
            [m74, member(sp, "b", percent=82)], accounted(sp, 82), [],
            completed_at_utc=E, index_identity="ix"))


def test_a_member_of_another_window_or_axis_never_earns(sp):
    other = member(sp, "a", percent=83, window=RESET + 7 * 86400)
    assert sp.earn_receipts([other], accounted(sp, 83), [], completed_at_utc=E,
                            index_identity="ix") == ()


# --- validity: the weekly rows of the section 4.4 table ---------------------------


def test_no_later_transition_keeps_the_receipt(sp):
    r = receipt(sp)
    assert sp.receipt_valid(r, [], index_identity="ix")


def test_a_rebuild_or_epoch_change_invalidates(sp):
    assert not sp.receipt_valid(receipt(sp), [], index_identity="ix2")


@pytest.mark.parametrize("kind,q,survives", [
    ("provider", "2026-10-22T09:20:00.000001Z", True),    # q > e_σ
    ("provider", E, False),                               # equal
    ("provider", "2026-10-22T09:19:59.999999Z", False),   # q < e_σ (D7a)
    ("provider", None, False),                            # unknown q
    ("statusline", None, True),                           # R15
    ("manual", None, False),
    ("legacy", None, False),
    ("corrected", None, False),
    ("unknown", None, False),
])
def test_a_later_credit_follows_its_causal_low(sp, kind, q, survives):
    r = receipt(sp)
    assert sp.receipt_valid(r, [credit(sp, kind=kind, q=q)], index_identity="ix") \
        is survives


def test_a_credit_of_the_receipts_generation_is_not_after_it(sp):
    known = credit(sp, "wr:known", kind="manual")
    r = receipt(sp, credits=[known])
    assert sp.receipt_valid(r, [known], index_identity="ix")
    # Corrected (another revision) or tombstoned (absent): invalidated.
    assert not sp.receipt_valid(r, [dataclasses.replace(known, event_revision="r2")],
                                index_identity="ix")
    assert not sp.receipt_valid(r, [], index_identity="ix")


# --- refusal --------------------------------------------------------------------


def test_refusal_needs_a_valid_receipt_and_a_proof_after_both(sp):
    m = member(sp)
    zero = sample("o:z", q="2026-10-22T09:31:22.000000Z", weekly=0)
    # Mutation control (refusal on domination alone, without a receipt).
    facts = sp.refusal_facts("sevenDay", [m], [zero], [], [], index_identity="ix")
    assert facts == {m.sigma.token: sp.MemberFacts(False, None, False)}
    r = receipt(sp, m)
    facts = sp.refusal_facts("sevenDay", [m], [zero], [r], [], index_identity="ix")
    assert facts == {m.sigma.token: sp.MemberFacts(True, "o:z", True)}
    # A proof between t_m and e_σ does not refuse.
    between = sample("o:w", q="2026-10-22T09:10:00.000000Z", weekly=0)
    facts = sp.refusal_facts("sevenDay", [m], [between], [r], [],
                             index_identity="ix")
    assert facts[m.sigma.token] == sp.MemberFacts(False, "o:w", True)
    # Mutation control (a receipt kept across a provider floor requested
    # before e_σ): the D7a credit invalidates it, so the member is admitted.
    floor = credit(sp, kind="provider", q="2026-10-22T09:19:00.000000Z")
    facts = sp.refusal_facts("sevenDay", [m], [zero], [r], [floor],
                             index_identity="ix")
    assert facts[m.sigma.token] == sp.MemberFacts(False, "o:z", False)


def test_a_receipt_for_another_sample_identity_never_refuses(sp):
    r = receipt(sp)
    later = member(sp, t_m="2026-10-22T09:37:25.000Z")
    zero = sample("o:z", q="2026-10-22T09:40:00.000000Z", weekly=0)
    facts = sp.refusal_facts("sevenDay", [later], [zero], [r], [],
                             index_identity="ix")
    assert facts[later.sigma.token].refused is False


# --- the five-hour rows (spec 4.4 table; Task 5) -------------------------------------

#: A five-hour window: its canonical key (12:00Z) and the member's raw reset.
FIVE_KEY = 1792670400          # 2026-10-22T12:00:00Z
FIVE_RESET = FIVE_KEY + 70     # the advertised reset jitters above the key
FIVE_RESET_ISO = "2026-10-22T12:01:10Z"
T_M5 = "2026-10-22T09:00:20.000Z"
E5 = "2026-10-22T09:00:30.000000Z"


def five_member(sp, name="i", *, percent=20.0, t_m=T_M5, reset=FIVE_RESET):
    return sp.Member(sigma(sp, name, percent=percent, t_m=t_m, window=FIVE_KEY,
                           axis="fiveHour"), advertised_reset_epoch=reset)


def five_sample(obs, *, q, five, captured=None, window=FIVE_KEY):
    return samples_lib.OAuthSample(
        account_key=ACCOUNT, observation_id=obs, requested_at_utc=q,
        captured_at_utc=captured or (q[:19] + "Z"), weekly_percent=40.0,
        weekly_resets_at_utc=RESET_ISO, five_hour_percent=float(five),
        five_hour_resets_at_utc=FIVE_RESET_ISO, five_hour_window_key=window)


def five_accounted(sp, percent=20.0, *, credits=()):
    return sp.Accounted(
        account_key=ACCOUNT, axis="fiveHour", window=FIVE_KEY,
        percent=None if percent is None else float(percent),
        support_id="o:baseline", support_hash="h" * 64, floor_epoch=None,
        credits=tuple(credits), coverage=None, detector_policy=2)


def five_credit(sp, event_id="fhc:x", *, kind="provider", arming=None,
                baseline=None, baseline_percent=None, revision="r1"):
    return sp.CreditTransition(
        axis="fiveHour", event_id=event_id, event_revision=revision,
        causal_kind=kind, requested_at_utc=arming,
        baseline_requested_at_utc=baseline, baseline_percent=baseline_percent,
        arming_requested_at_utc=arming)


def five_receipt(sp, m=None, *, e=E5, credits=(), index="ix"):
    m = m or five_member(sp)
    earned = sp.earn_receipts([m], five_accounted(sp, m.sigma.percent, credits=credits),
                              [], completed_at_utc=e, index_identity=index)
    assert len(earned) == 1 and earned[0].path == "maximal"
    return earned[0]


Q5_LATE = "2026-10-22T09:05:58.000000Z"


def test_five_hour_proof_rows(sp):
    m = five_member(sp)
    s = m.sigma
    low = five_sample("o:5", q=Q5_LATE, five=5)
    assert sp.proven_stale(s, [low], after_utc=E5, advertised_reset_epoch=FIVE_RESET) == low
    # The tolerance: strictly more than one point below.
    assert sp.proven_stale(s, [five_sample("o:19", q=Q5_LATE, five=19)],
                           after_utc=E5, advertised_reset_epoch=FIVE_RESET) is None
    # q, never the capture instant, orders against t_m and e_sigma.
    early = five_sample("o:e", q="2026-10-22T09:00:10.000000Z", five=5,
                        captured="2026-10-22T09:06:00Z")
    assert sp.proven_stale(s, [early], after_utc=T_M5,
                           advertised_reset_epoch=FIVE_RESET) is None
    between = five_sample("o:b", q="2026-10-22T09:00:25.000000Z", five=5)
    assert sp.proven_stale(s, [between], after_utc=T_M5,
                           advertised_reset_epoch=FIVE_RESET) == between
    assert sp.proven_stale(s, [between], after_utc=E5,
                           advertised_reset_epoch=FIVE_RESET) is None
    # Window identity by canonical key equality; a weekly-only sample proves
    # nothing on the five-hour axis.
    other = five_sample("o:o", q=Q5_LATE, five=5, window=FIVE_KEY + 600)
    weekly_only = dataclasses.replace(low, five_hour_percent=None,
                                      five_hour_window_key=None)
    assert sp.proven_stale(s, [other, weekly_only], after_utc=E5,
                           advertised_reset_epoch=FIVE_RESET) is None
    # Captured at or after the member's advertised reset: no proof.
    late = five_sample("o:l", q="2026-10-22T12:01:05.000000Z", five=0,
                       captured="2026-10-22T12:01:10Z")
    assert sp.proven_stale(s, [late], after_utc=E5,
                           advertised_reset_epoch=FIVE_RESET) is None
    assert sp.proven_stale(s, [late], after_utc=E5,
                           advertised_reset_epoch=FIVE_RESET + 1) == late


def test_five_hour_earning_is_path_i_only(sp):
    a, b = five_member(sp, "a"), five_member(sp, "b")
    lower = five_member(sp, "c", percent=15.0)
    high = five_sample("o:h", q=Q5_LATE, five=30)
    earned = sp.earn_receipts([a, b, lower], five_accounted(sp, 20.0), [high],
                              completed_at_utc=E5, index_identity="ix")
    # Exact equality with the statusline baseline; ties earn independently;
    # there is no provider-support path on this axis.
    assert sorted((r.sigma.token, r.path) for r in earned) == [
        (_token("a"), "maximal"), (_token("b"), "maximal")]
    r = earned[0]
    assert (r.window, r.accounted_percent, r.detector_policy, r.provider_observation_id
            ) == (FIVE_KEY, 20.0, 2, None)
    # No statusline baseline in the current generation: nothing earns.
    assert sp.earn_receipts([a], five_accounted(sp, None), [], completed_at_utc=E5,
                            index_identity="ix") == ()
    # A weekly member never earns from five-hour accounting.
    assert sp.earn_receipts([member(sp, percent=20.0)], five_accounted(sp, 20.0), [],
                            completed_at_utc=E5, index_identity="ix") == ()


AFTER_TM = "2026-10-22T09:03:00.000000Z"      # q of a baseline after t_m
BEFORE_TM = "2026-10-22T09:00:00.000000Z"     # q of a baseline before t_m
AFTER_E = "2026-10-22T09:04:00.000000Z"       # q of an arming after e_sigma
BEFORE_E = "2026-10-22T09:00:25.000000Z"      # q of an arming before e_sigma


@pytest.mark.parametrize("kind,arming,baseline,baseline_percent,survives", [
    # The statusline baseline's lineage continues (R15 row).
    ("statusline", None, None, None, True),
    # A credit confirmed by another source: all three conditions hold.
    ("provider", AFTER_E, AFTER_TM, 20.0, True),
    ("provider", AFTER_E, AFTER_TM, 80.0, True),
    # Each condition failing alone invalidates.
    ("provider", AFTER_E, BEFORE_TM, 80.0, False),     # baseline before t_m
    ("provider", AFTER_E, AFTER_TM, 19.9, False),      # baseline raw below v_m
    ("provider", BEFORE_E, AFTER_TM, 80.0, False),     # arming before e_sigma
    ("provider", E5, AFTER_TM, 80.0, False),           # arming equal to e_sigma
    # Unknown order or facts invalidate.
    ("provider", None, AFTER_TM, 80.0, False),
    ("provider", AFTER_E, None, 80.0, False),
    ("provider", AFTER_E, AFTER_TM, None, False),
    ("manual", None, None, None, False),
    ("legacy", None, None, None, False),
    ("corrected", None, None, None, False),
    ("unknown", None, None, None, False),
])
def test_five_hour_validity_rows(sp, kind, arming, baseline, baseline_percent, survives):
    r = five_receipt(sp)
    c = five_credit(sp, kind=kind, arming=arming, baseline=baseline,
                    baseline_percent=baseline_percent)
    assert sp.receipt_valid(r, [c], index_identity="ix") is survives


def test_five_hour_validity_follows_the_arming_observation_not_the_confirmation(sp):
    """Mutation control (arming versus confirmation): a credit whose arming
    low was requested before e_sigma invalidates, whatever the later
    confirming observation's request time."""
    r = five_receipt(sp)
    confirmed_late = dataclasses.replace(
        five_credit(sp, arming=BEFORE_E, baseline=AFTER_TM, baseline_percent=80.0),
        requested_at_utc=AFTER_E)
    assert not sp.receipt_valid(r, [confirmed_late], index_identity="ix")


def test_five_hour_codex_example(sp):
    """Codex's example: an `api` baseline 80 and a `statusline` baseline 20;
    an `api` `0, 1` confirmation invalidates the 20's receipt unless
    continuity holds (the 80 requested after t_m, the 0 after e_sigma)."""
    r = five_receipt(sp)
    no_continuity = five_credit(sp, arming=AFTER_E, baseline=BEFORE_TM,
                                baseline_percent=80.0)
    assert not sp.receipt_valid(r, [no_continuity], index_identity="ix")
    continuity = five_credit(sp, arming=AFTER_E, baseline=AFTER_TM,
                             baseline_percent=80.0)
    assert sp.receipt_valid(r, [continuity], index_identity="ix")


def test_five_hour_generation_index_and_axis_bounds(sp):
    known = five_credit(sp, "fhc:known", kind="statusline")
    r = five_receipt(sp, credits=[known])
    assert sp.receipt_valid(r, [known], index_identity="ix")
    # Corrected or tombstoned: invalidated, and never revived.
    assert not sp.receipt_valid(r, [dataclasses.replace(known, event_revision="r2")],
                                index_identity="ix")
    assert not sp.receipt_valid(r, [], index_identity="ix")
    # A rebuild, re-upgrade or epoch change of the index.
    assert not sp.receipt_valid(five_receipt(sp), [], index_identity="ix2")
    # A weekly credit is not a five-hour credit, and vice versa.
    assert sp.receipt_valid(five_receipt(sp), [credit(sp, kind="manual")],
                            index_identity="ix")
    assert sp.receipt_valid(receipt(sp), [five_credit(sp, kind="manual")],
                            index_identity="ix")
    # Pruning is per axis and window.
    five = five_receipt(sp)
    assert sp.prune_receipts([five], [], index_identity="ix", window=FIVE_KEY) == (five,)
    assert sp.prune_receipts([five], [], index_identity="ix", window=RESET) == ()


def test_five_hour_refusal_needs_a_valid_receipt_and_a_proof_after_both(sp):
    m = five_member(sp)
    low = five_sample("o:5", q=Q5_LATE, five=5)
    # Domination alone, without a receipt, never refuses.
    assert sp.refusal_facts("fiveHour", [m], [low], [], [], index_identity="ix") == {
        m.sigma.token: sp.MemberFacts(False, None, False)}
    r = five_receipt(sp, m)
    # The statusline-source credit keeps the receipt: refused.
    sl_credit = five_credit(sp, kind="statusline")
    assert sp.refusal_facts("fiveHour", [m], [low], [r], [sl_credit],
                            index_identity="ix") == {
        m.sigma.token: sp.MemberFacts(True, "o:5", True)}
    # An api credit without continuity invalidates before any refusal.
    api = five_credit(sp, arming=AFTER_E, baseline=BEFORE_TM, baseline_percent=80.0)
    assert sp.refusal_facts("fiveHour", [m], [low], [r], [api],
                            index_identity="ix")[m.sigma.token] == \
        sp.MemberFacts(False, "o:5", False)
    # A proof between t_m and e_sigma does not refuse.
    between = five_sample("o:b", q="2026-10-22T09:00:25.000000Z", five=5)
    assert sp.refusal_facts("fiveHour", [m], [between], [r], [],
                            index_identity="ix")[m.sigma.token] == \
        sp.MemberFacts(False, "o:b", True)
    # Weekly facts never come from five-hour members, and vice versa.
    assert sp.refusal_facts("sevenDay", [m], [low], [r], [], index_identity="ix") == {}


def test_five_hour_compact_samples_preserve_every_proof(sp):
    population = [five_sample(f"o:{i}", q=f"2026-10-22T09:{i:02d}:30.000000Z", five=v)
                  for i, v in enumerate([20, 5, 5, 6, 20, 0, 1, 5])]
    population.append(five_sample("o:late", q="2026-10-22T11:59:00.000000Z", five=0,
                                  captured="2026-10-22T11:59:30Z"))
    compact = sp.compact_samples(population, window=FIVE_KEY, floor_epoch=None,
                                 axis="fiveHour")
    assert len(compact) < len(population)
    for t_m, percent, e, reset in itertools.product(
            ("2026-10-22T09:00:00.000Z", "2026-10-22T09:03:00.000Z"),
            (0.5, 5, 7, 20, 21), (T_M5, "2026-10-22T09:04:00.000000Z"),
            (FIVE_RESET, FIVE_KEY - 60)):
        s = sigma(sp, t_m=t_m, percent=percent, window=FIVE_KEY, axis="fiveHour")
        assert sp.proven_stale(s, compact, after_utc=e, advertised_reset_epoch=reset) == \
            sp.proven_stale(s, population, after_utc=e, advertised_reset_epoch=reset)


def test_five_hour_credit_codec_round_trips(sp):
    c = five_credit(sp, arming=AFTER_E, baseline=AFTER_TM, baseline_percent=80.0)
    assert sp.credit_from_json(sp.credit_to_json(c)) == c
    r = five_receipt(sp, credits=[c])
    assert sp.receipt_from_json(sp.receipt_to_json(r)) == r


# --- pruning, revival and eviction ----------------------------------------------------


def test_an_invalidated_receipt_is_never_revived(sp):
    r = receipt(sp)
    bad = credit(sp, kind="manual")
    kept = sp.prune_receipts([r], [bad], index_identity="ix", window=RESET)
    assert kept == ()
    # The invalidating credit is gone again (say, tombstoned): the pruned
    # receipt does not come back; only new accounting earns a new one.
    assert sp.prune_receipts(kept, [], index_identity="ix", window=RESET) == ()
    # Pruning keeps valid receipts of the current window only.
    assert sp.prune_receipts([r], [], index_identity="ix", window=RESET) == (r,)
    assert sp.prune_receipts([r], [], index_identity="ix",
                             window=RESET + 7 * 86400) == ()
    assert sp.prune_receipts([r], [], index_identity="ix2", window=RESET) == ()


def test_merge_keeps_the_earliest_valid_receipt_of_a_sample(sp):
    first = receipt(sp)
    second = receipt(sp, e="2026-10-22T09:25:00.000000Z")
    assert sp.merge_receipts([first], [second]) == (first,)
    assert sp.merge_receipts([], [second, first]) == (first,)


def test_eviction_drops_the_oldest_unreferenced_first_and_means_eligible(sp):
    olds = [receipt(sp, member(sp, n), e=f"2026-10-22T09:2{i}:00.000000Z")
            for i, n in enumerate("abc")]
    new = receipt(sp, member(sp, "d"), e="2026-10-22T09:29:00.000000Z")
    referenced = {_token("a")}

    def fits(receipts):
        return len(receipts) <= 3

    kept = sp.bounded_receipts(olds, [new], referenced_tokens=referenced, fits=fits)
    # "b" (the oldest unreferenced) goes; the referenced "a" stays.
    assert [r.sigma.token for r in kept] == [_token(n) for n in "acd"]
    # No room even after evicting every unreferenced receipt: the new
    # receipt is omitted, accounting is never blocked.
    kept = sp.bounded_receipts(olds, [new], referenced_tokens={_token(n) for n in "abc"},
                               fits=fits)
    assert [r.sigma.token for r in kept] == [_token(n) for n in "abc"]
    # An evicted member is eligible.
    zero = sample("o:z", q="2026-10-22T09:40:00.000000Z", weekly=0)
    evicted = member(sp, "b")
    assert sp.refusal_facts("sevenDay", [evicted], [zero],
                            sp.bounded_receipts(olds, [new], referenced_tokens=referenced,
                                                fits=fits),
                            [], index_identity="ix")[evicted.sigma.token].refused is False


# --- the codecs -------------------------------------------------------------------------


def test_receipt_and_credit_codecs_round_trip_and_reject_malformed(sp):
    r = receipt(sp, credits=[credit(sp, "wr:k", kind="statusline")])
    doc = sp.receipt_to_json(r)
    assert sp.receipt_from_json(doc) == r
    for broken in ({**doc, "extra": 1}, {k: v for k, v in doc.items() if k != "path"},
                   {**doc, "path": "other"}, {**doc, "completedAt": "yesterday"}):
        with pytest.raises(sp.MalformedReceipt):
            sp.receipt_from_json(broken)
    c = credit(sp, "wr:k", kind="provider", q=E)
    assert sp.credit_from_json(sp.credit_to_json(c)) == c
    with pytest.raises(sp.MalformedReceipt):
        sp.credit_from_json({**sp.credit_to_json(c), "causalKind": "other"})


# --- the compact sample set ---------------------------------------------------------------


def test_compact_samples_preserve_every_proof_and_support(sp):
    values = [0, 1, 74, 82, 83, 83, 82, 1, 0, 90]
    population = []
    for i, v in enumerate(values):
        q = f"2026-10-22T0{i // 6}:{(i % 6) * 10:02d}:00.000000Z"
        population.append(sample(f"o:{i}", q=q, weekly=v))
    # Near the reset: every sample is kept (the member's own reset decides).
    population.append(sample("o:late", q="2026-10-24T04:40:00.000000Z", weekly=0,
                             captured="2026-10-24T04:59:30Z"))
    compact = sp.compact_samples(population, window=RESET, floor_epoch=FLOOR)
    assert len(compact) < len(population)
    instants = [f"2026-10-22T0{h}:{m:02d}:00.000Z" for h in range(2) for m in (0, 5, 30)]
    for t_m, percent, e, reset in itertools.product(
            instants, (0.5, 74, 82, 83, 91), (T_M, "2026-10-22T00:30:00.000000Z"),
            (RESET, RESET - 60)):
        s = sigma(sp, t_m=t_m, percent=percent)
        assert sp.proven_stale(s, compact, after_utc=e, advertised_reset_epoch=reset) == \
            sp.proven_stale(s, population, after_utc=e, advertised_reset_epoch=reset)
        m = sp.Member(s, reset)
        assert sp.earn_receipts([m], accounted(sp, 90), compact, completed_at_utc=E,
                                index_identity="ix") == \
            sp.earn_receipts([m], accounted(sp, 90), population, completed_at_utc=E,
                             index_identity="ix")


# --- #1019: the receipt-free drop proof (spec 4.1, L4, L13) ---------------------------


def _low_case(name):
    """``(negative, positive, mutation)`` for one boundary of
    `proven_stale_low` over a member LOW of 18 bound at ``T_M``: each is
    ``(bound, samples, kwargs)``; the negative stays eligible, its positive
    control is proven, and the named mutation of
    `tests/_stale_contributor_trace.py` (or None) proves the negative."""
    base = dict(axis="sevenDay", window=RESET, percent=18.0)
    proof = [sample("o:p", q=Q_LATE, weekly=51)]
    if name == "equal-second":
        bound = "2026-10-22T09:31:22Z"
        return ((bound, [sample("o:n", q="2026-10-22T09:31:22.900000Z", weekly=51)], base),
                (bound, [sample("o:p", q="2026-10-22T09:31:23.000000Z", weekly=51)], base),
                "drop-bound-ignored")
    if name == "earlier-q":
        return ((Q_LATE, [sample("o:n", q="2026-10-21T22:50:14.000000Z", weekly=51)], base),
                (T_M, proof, base), "drop-bound-ignored")
    if name == "tolerance":
        return ((T_M, [sample("o:n", q=Q_LATE, weekly=19.0)], base),
                (T_M, [sample("o:p", q=Q_LATE, weekly=19.0001)], base),
                "drop-no-tolerance")
    if name == "other-window":
        return ((T_M, [sample("o:n", q=Q_LATE, weekly=51,
                              reset="2026-10-31T05:00:00Z")], base),
                (T_M, proof, base), None)
    if name == "at-advertised-reset":
        late = [sample("o:n", q="2026-10-24T04:59:50.000000Z", weekly=51,
                       captured="2026-10-24T04:59:59Z")]
        return ((T_M, late, dict(base, advertised_reset_epoch=RESET - 30)),
                (T_M, late, dict(base, advertised_reset_epoch=RESET)), None)
    if name == "received-at-fallback":
        # No supported witness: the bound is the received_at second
        # (2026-10-22T09:00:00Z), and only a sample requested after it
        # proves the low.
        return (("received", 1792659600, [sample("o:n", q="2026-10-22T09:00:00.500000Z",
                                             weekly=51)], base),
                ("received", 1792659600, [sample("o:p", q="2026-10-22T09:00:01.000000Z",
                                             weekly=51)], base),
                "drop-bound-ignored")
    if name == "weekly-refused-five-hour-eligible":
        both = [five_sample("o:n", q=Q_LATE, five=5)]          # weekly 40
        return ((T_M, both, dict(base, axis="fiveHour", window=FIVE_KEY,
                                 advertised_reset_epoch=FIVE_RESET)),
                (T_M, both, base), None)
    if name == "five-hour-refused-weekly-eligible":
        both = [five_sample("o:n", q=Q_LATE, five=51)]         # weekly 40
        return ((T_M, both, dict(base, percent=45.0)),
                (T_M, both, dict(base, percent=45.0, axis="fiveHour", window=FIVE_KEY,
                                 advertised_reset_epoch=FIVE_RESET)), None)
    raise AssertionError(name)


def _bound_of(sp, case):
    if case[0] == "received":
        _tag, received, samples, kwargs = case
        bound = sp.drop_bound("a" * 64, None, received, axis=kwargs["axis"],
                              percent=kwargs["percent"], window=kwargs["window"])
        assert sp.sample_identity("a" * 64, None, axis=kwargs["axis"],
                                  percent=kwargs["percent"], window=kwargs["window"]) is None
        return bound, samples, kwargs
    return case


@pytest.mark.parametrize("name", ["equal-second", "earlier-q", "tolerance", "other-window",
                                  "at-advertised-reset", "received-at-fallback",
                                  "weekly-refused-five-hour-eligible",
                                  "five-hour-refused-weekly-eligible"])
def test_proven_stale_low_boundaries(sp, monkeypatch, name):
    """L4 (accuracy: a genuine low refused, or a stale one admitted, at a
    proof boundary): request order under the precision intervals (the equal
    second, an earlier q), the strict 1 pp tolerance, the member's window
    and advertised reset, the received_at fallback (a whole-second bound
    that never becomes a sigma), and weekly/five-hour independence. Each
    negative case has a positive control, and the mutation violating its
    boundary proves the negative. Another account's sample is the exact-
    account read's boundary, not this kernel's: L3b's b-first order and the
    `samples-cross-account` mutation pin it (`MUTATION_BITES`)."""
    negative, positive, mutation = _low_case(name)
    bound, samples, kwargs = _bound_of(sp, negative)
    assert sp.proven_stale_low(bound, samples, **kwargs) is None
    pbound, psamples, pkwargs = _bound_of(sp, positive)
    assert sp.proven_stale_low(pbound, psamples, **pkwargs) is not None
    if mutation is not None:
        S.mutate(monkeypatch, mutation)
        assert sp.proven_stale_low(bound, samples, **kwargs) is not None, mutation


def test_drop_facts_are_receipt_free_and_independent_of_rise_facts(sp):
    """L4: `refusal_facts` gives a drop-refused member its drop fields with
    no receipt held, leaves the rise fields of `ALL_ELIGIBLE`, and decides
    each axis on its own samples."""
    low = sp.DropMember("a" * 64, T_M, "sevenDay", 18.0, RESET, RESET)
    samples = [sample("o:p", q=Q_LATE, weekly=51)]
    facts = sp.refusal_facts("sevenDay", [], samples, (), (), index_identity="ix",
                             drop_members=[low])
    assert facts == {"a" * 64: sp.MemberFacts(False, None, False, drop_refused=True,
                                              drop_proof_observation_id="o:p")}
    assert sp.refusal_facts("fiveHour", [], samples, (), (), index_identity="ix",
                            drop_members=[low]) == {}


def test_compact_samples_equivalence_for_proven_stale_low(sp, monkeypatch):
    """L13 (accuracy: the render cache dropping a sample a drop proof
    needs): over randomized and edge sample sets with nonempty proving
    populations, `compact_samples` yields the same `proven_stale_low`
    answer for every bound, value and advertised reset; a compaction that
    drops a necessary sample breaks it."""
    import random
    rng = random.Random(1019)

    def population():
        out = []
        for i in range(rng.randint(6, 24)):
            minute = rng.randint(0, 900)
            q = f"2026-10-22T{minute // 60:02d}:{minute % 60:02d}:{rng.randint(0, 59):02d}." \
                f"{rng.randint(0, 999999):06d}Z"
            out.append(sample(f"o:{i}", q=q, weekly=rng.choice((0, 1, 18, 19, 19.5, 51, 52))))
        out.append(sample("o:late", q="2026-10-24T04:40:00.000000Z", weekly=60,
                          captured="2026-10-24T04:59:30Z"))
        return out

    def check(compact_of):
        for _round in range(40):
            pop = population()
            compact = compact_of(pop)
            proving = 0
            for minute in (0, 120, 450, 899):
                bound = f"2026-10-22T{minute // 60:02d}:{minute % 60:02d}:00.000Z"
                for percent in (0.0, 17.5, 18.0, 50.0):
                    for reset in (RESET, RESET - 60):
                        want = sp.proven_stale_low(bound, pop, axis="sevenDay",
                                                   window=RESET, percent=percent,
                                                   advertised_reset_epoch=reset)
                        proving += want is not None
                        got = sp.proven_stale_low(bound, compact, axis="sevenDay",
                                                  window=RESET, percent=percent,
                                                  advertised_reset_epoch=reset)
                        if got != want:
                            return False
            assert proving, "a vacuous round: nothing proven"
        return True

    assert check(lambda pop: sp.compact_samples(pop, window=RESET, floor_epoch=FLOOR))

    def drop_latest(pop):
        # Mutation `compact-drops-necessary-sample`: the latest sample of
        # each value is discarded.
        compact = list(sp.compact_samples(pop, window=RESET, floor_epoch=FLOOR))
        latest = {}
        for s in compact:
            key = s.weekly_percent
            if key not in latest or s.requested_at_utc > latest[key].requested_at_utc:
                latest[key] = s
        return tuple(s for s in compact if s not in latest.values())

    assert not check(drop_latest)


# --- #1064: the prepared sample view ---------------------------------------------------
#
# The reference below is a verbatim copy of the predicates and readers of
# `bin/_lib_stale_proof.py` at d6bd99e65, before the prepared view: compiled
# into its own namespace over the kernel's unchanged primitives and records,
# it is the independent behaviour test 1 compares the kernel against.

_REFERENCE_1064 = r'''
def _interval(text: str | None) -> tuple[int, int] | None:
    """The ``[start, end)`` nanosecond interval a canonical instant denotes."""
    if not isinstance(text, str):
        return None
    try:
        start = _activity.instant_key(text)
        digits = _activity.instant_precision(text)
    except ValueError:
        return None
    return start, start + 10 ** (9 - min(digits, 9))


def after(a: str | None, b: str | None) -> bool:
    """Whether instant ``a`` is proven to follow instant ``b``."""
    ia, ib = _interval(a), _interval(b)
    return ia is not None and ib is not None and ia[0] >= ib[1]


def _before_epoch(text: str | None, epoch: int) -> bool:
    """Whether instant ``text`` is proven to precede the exact second ``epoch``."""
    interval = _interval(text)
    return interval is not None and interval[1] <= epoch * _NS


def _at_or_after_epoch(text: str | None, epoch: int) -> bool:
    interval = _interval(text)
    return interval is not None and interval[0] >= epoch * _NS


def _instant_order(text: str) -> int:
    interval = _interval(text)
    return -1 if interval is None else interval[0]

def _window_text(window: int) -> str:
    return _samples.normalize_weekly_reset(int(window))


def _sample_window(sample) -> str | None:
    if sample.weekly_resets_at_utc is None:
        return None
    try:
        return _samples.weekly_window(sample.weekly_resets_at_utc)
    except ValueError:
        return None


def _in_window(samples: Iterable, window: int) -> list:
    text = _window_text(window)
    return [s for s in samples
            if s.weekly_percent is not None and _sample_window(s) == text]


def _five_in_window(samples: Iterable, window: int) -> list:
    """The samples of one five-hour window, by canonical key equality."""
    return [s for s in samples
            if s.five_hour_percent is not None and s.five_hour_window_key is not None
            and int(s.five_hour_window_key) == int(window)]


def _axis_window(samples: Iterable, axis: str, window: int) -> list:
    return (_five_in_window(samples, window) if axis == FIVE_HOUR
            else _in_window(samples, window))


def _axis_value(sample, axis: str) -> float:
    return float(sample.five_hour_percent if axis == FIVE_HOUR else sample.weekly_percent)


def _latest(samples: Iterable):
    best = None
    for s in samples:
        if best is None or (_instant_order(s.requested_at_utc), s.observation_id) > (
                _instant_order(best.requested_at_utc), best.observation_id):
            best = s
    return best


# --- the proof -----------------------------------------------------------------------


def proven_stale(sigma: SampleIdentity, samples: Iterable, *, after_utc: str,
                 advertised_reset_epoch: int | None = None):
    """The latest provider sample proving σ stale, or None.

    A sample proves σ stale when it is in σ's authoritative window, its
    request instant ``q`` follows both ``t_m`` and ``after_utc``, its raw
    value is strictly more than `TOLERANCE_PP` below σ's, and its response
    was captured before the member's advertised reset (the window's reset
    when the member advertised none). On the five-hour axis the window is
    the canonical key and the value is the sample's raw five-hour percent."""
    if sigma.axis not in AXES:
        return None
    reset = sigma.window if advertised_reset_epoch is None else int(advertised_reset_epoch)
    proving = [
        s for s in _axis_window(samples, sigma.axis, sigma.window)
        if _axis_value(s, sigma.axis) < sigma.percent - TOLERANCE_PP
        and after(s.requested_at_utc, sigma.activity_at_utc)
        and after(s.requested_at_utc, after_utc)
        and _before_epoch(s.captured_at_utc, reset)
    ]
    return _latest(proving)


def proven_stale_low(bound_utc: str, samples: Iterable, *, axis: str, window: int,
                     percent: float, advertised_reset_epoch: int | None = None):
    """The latest provider sample proving a member's LOW stale, or None
    (#1019 spec 4.1, Q1).

    A sample proves it when it is in the member's exact window (weekly: the
    normalized reset; five-hour: the canonical key; the caller passes only
    the member's own account's samples), its request instant ``q`` follows
    ``bound_utc`` (`drop_bound`) under the precision intervals, its raw axis
    value is strictly more than `TOLERANCE_PP` ABOVE ``percent``, and its
    response was captured before the member's advertised reset (the
    window's reset when it advertised none). No receipt, no ``e_σ``, and no
    baseline or threshold cutoff."""
    if axis not in AXES or _interval(bound_utc) is None:
        return None
    reset = int(window) if advertised_reset_epoch is None else int(advertised_reset_epoch)
    proving = [
        s for s in _axis_window(samples, axis, window)
        if _axis_value(s, axis) > float(percent) + TOLERANCE_PP
        and after(s.requested_at_utc, bound_utc)
        and _before_epoch(s.captured_at_utc, reset)
    ]
    return _latest(proving)

def earn_receipts(members: Sequence[Member], accounted: Accounted, samples: Iterable, *,
                  completed_at_utc: str, index_identity: str) -> tuple[Receipt, ...]:
    """The receipts a covered, completed derivation earns for ``members``.

    (i) A member whose value equals exactly the accounted value earns a
    maximal receipt; ties earn independently. (ii) Otherwise a weekly member
    below the accounted value earns provider support when the admitted
    provider sample that supports the completed accounting — the current
    segment's latest, B — was requested after its ``t_m`` and reads raw at
    or above its value. An older, higher sample the accounting has since
    moved below supports nothing, and neither does the MAX-clamped stored
    value. A pending or retained low, a projection fill, a numerically
    dominating member and unknown evidence never earn. On the five-hour axis
    the accounted value is the `statusline` source's detector baseline under
    the current credit generation and only (i) exists: that baseline holds
    no provider samples."""
    if accounted.axis not in AXES or accounted.percent is None:
        return ()
    segment = [s for s in _in_window(samples, accounted.window)
               if accounted.floor_epoch is None
               or _at_or_after_epoch(s.captured_at_utc, accounted.floor_epoch)]
    generation = tuple((c.event_id, c.event_revision) for c in accounted.credits
                       if c.axis == accounted.axis)
    latest = _latest(segment)
    earned = []
    for m in members:
        s = m.sigma
        if s.axis != accounted.axis or s.window != accounted.window:
            continue
        support = None
        if s.percent == accounted.percent:
            path = "maximal"
        elif accounted.axis == FIVE_HOUR:
            continue
        else:
            support = latest
            if (support is None or s.percent > accounted.percent
                    or float(support.weekly_percent) < s.percent
                    or not after(support.requested_at_utc, s.activity_at_utc)):
                continue
            path = "provider_support"
        earned.append(Receipt(
            sigma=s, path=path, account_key=accounted.account_key,
            window=accounted.window, support_id=accounted.support_id,
            support_hash=accounted.support_hash,
            accounted_percent=float(accounted.percent),
            provider_observation_id=None if support is None else support.observation_id,
            provider_percent=None if support is None else float(support.weekly_percent),
            provider_requested_at_utc=None if support is None else support.requested_at_utc,
            provider_window=None if support is None else _sample_window(support),
            detector_policy=accounted.detector_policy, credits=generation,
            coverage=accounted.coverage, completed_at_utc=completed_at_utc,
            index_identity=index_identity))
    return tuple(earned)


def receipt_valid(receipt: Receipt, transitions: Iterable[CreditTransition], *,
                  index_identity: str, supports: Collection[str] | None = None) -> bool:
    """Whether ``receipt`` stays valid against every current credit of its
    account and window (the weekly rows of the section 4.4 table).

    The index identity must match (a rebuild, re-upgrade or epoch change
    invalidates). Every credit of the receipt's generation must still be
    present at its revision (a corrected or tombstoned event invalidates).
    A credit after the receipt survives only when its causal low is an
    admitted provider sample requested after ``e_σ`` or a status-line reading
    (R15); every other or unordered credit invalidates. When ``supports`` is
    given, the receipt's supporting observation and its provider sample B
    must still be among them (a held, retracted or tombstoned support
    invalidates).

    The five-hour rows: a `statusline`-source credit survives (its causal low
    is the source's own arming observation, R15). A credit confirmed by
    another source survives only when its baseline observation is an
    admitted provider sample requested after ``t_m`` whose RAW five-hour
    value is at least σ's, and its ARMING observation — never the later
    confirming one — is a provider sample requested after ``e_σ``; every
    other or unordered credit invalidates. Supports are not checked on this
    axis: the accounted baseline is detector state, not a stored row."""
    if receipt.sigma.axis not in AXES or receipt.index_identity != index_identity:
        return False
    five = receipt.sigma.axis == FIVE_HOUR
    if supports is not None and not five:
        if receipt.support_id is not None and receipt.support_id not in supports:
            return False
        if (receipt.provider_observation_id is not None
                and receipt.provider_observation_id not in supports):
            return False
    current = {}
    for t in transitions:
        if t.axis == receipt.sigma.axis:
            current[t.event_id] = t
    generation = dict(receipt.credits)
    for event_id, revision in generation.items():
        t = current.get(event_id)
        if t is None or t.event_revision != revision:
            return False
    for event_id, t in current.items():
        if event_id in generation:
            continue
        if t.causal_kind == "statusline":
            continue
        if five:
            if (t.causal_kind == "provider"
                    and t.baseline_percent is not None
                    and float(t.baseline_percent) >= receipt.sigma.percent
                    and after(t.baseline_requested_at_utc,
                              receipt.sigma.activity_at_utc)
                    and after(t.arming_requested_at_utc, receipt.completed_at_utc)):
                continue
            return False
        if t.causal_kind == "provider" and after(t.requested_at_utc,
                                                 receipt.completed_at_utc):
            continue
        return False
    return True


def refusal_facts(axis: str, members: Sequence[Member], samples: Iterable,
                  receipts: Iterable[Receipt], transitions: Iterable[CreditTransition], *,
                  index_identity: str, supports: Collection[str] | None = None,
                  drop_members: Sequence[DropMember] = ()
                  ) -> dict[str, MemberFacts]:
    """Per-member facts for one axis, keyed by contributor token.

    A member is refused only when it holds a valid receipt for its exact σ
    and a sample requested after both ``t_m`` and ``e_σ`` proves it stale.
    A member without a receipt is eligible with no proof reported; one whose
    receipt is invalid is eligible with its proof reported. Each axis is
    decided on its own members, samples and credits.

    #1019 (spec 4.1): every ``drop_members`` entry gets its drop facts from
    `proven_stale_low`, independently of ``receipts``, so one call per axis
    yields complete facts; a token with drop facts only keeps the rise
    fields of `ALL_ELIGIBLE`."""
    if axis not in AXES:
        return {}
    samples = list(samples)
    transitions = list(transitions)
    by_sigma: dict[str, list[Receipt]] = {}
    for r in receipts:
        by_sigma.setdefault(r.sigma.key(), []).append(r)
    facts: dict[str, MemberFacts] = {}
    for m in members:
        s = m.sigma
        if s.axis != axis:
            continue
        held = sorted(by_sigma.get(s.key(), ()),
                      key=lambda r: _instant_order(r.completed_at_utc))
        if not held:
            facts[s.token] = ALL_ELIGIBLE
            continue
        valid = [r for r in held
                 if receipt_valid(r, transitions, index_identity=index_identity,
                                  supports=supports)]
        reset = m.advertised_reset_epoch
        if valid:
            strict = proven_stale(s, samples, after_utc=valid[0].completed_at_utc,
                                  advertised_reset_epoch=reset)
        else:
            strict = None
        proof = strict or proven_stale(s, samples, after_utc=s.activity_at_utc,
                                       advertised_reset_epoch=reset)
        facts[s.token] = MemberFacts(
            refused=bool(valid) and strict is not None,
            proof_observation_id=None if proof is None else proof.observation_id,
            receipt_valid=bool(valid))
    for d in drop_members:
        if d.axis != axis:
            continue
        low = proven_stale_low(d.bound_utc, samples, axis=axis, window=d.window,
                               percent=d.percent,
                               advertised_reset_epoch=d.advertised_reset_epoch)
        facts[d.token] = dataclasses.replace(
            facts.get(d.token, ALL_ELIGIBLE), drop_refused=low is not None,
            drop_proof_observation_id=None if low is None else low.observation_id)
    return facts

def compact_samples(samples: Iterable, *, window: int,
                    floor_epoch: int | None, axis: str = WEEKLY) -> tuple:
    """A subset of the window's samples that yields the same `proven_stale`,
    `proven_stale_low` and `earn_receipts` answers for every member (all pick the latest
    qualifying sample, and qualification is monotone in ``q`` within one
    value): per weekly value, the latest sample and the latest captured at
    or after the floor, plus every sample captured within an hour of the
    window's reset (a member's advertised reset may precede the window).
    ``axis`` selects the window identity and value (five-hour: the canonical
    key and the raw five-hour percent)."""
    margin = int(window) - _RESET_MARGIN_SECONDS
    keep: dict[str, object] = {}
    latest: dict[float, object] = {}
    latest_floor: dict[float, object] = {}
    for s in _axis_window(samples, axis, window):
        if not _before_epoch(s.captured_at_utc, margin):
            keep[s.observation_id] = s
            continue
        v = _axis_value(s, axis)
        latest[v] = _latest([x for x in (latest.get(v), s) if x is not None])
        if floor_epoch is None or _at_or_after_epoch(s.captured_at_utc, floor_epoch):
            latest_floor[v] = _latest([x for x in (latest_floor.get(v), s) if x is not None])
    for s in [*latest.values(), *latest_floor.values()]:
        keep[s.observation_id] = s
    return tuple(sorted(keep.values(),
                        key=lambda s: (_instant_order(s.requested_at_utc), s.observation_id)))

'''


def _reference_kernel(sp):
    import types
    namespace = {
        "dataclasses": dataclasses, "_activity": sp._activity, "_samples": sp._samples,
        "TOLERANCE_PP": 1.0, "_RESET_MARGIN_SECONDS": 3600, "_NS": 1_000_000_000,
        "WEEKLY": "sevenDay", "FIVE_HOUR": "fiveHour", "AXES": ("sevenDay", "fiveHour"),
        "Receipt": sp.Receipt, "MemberFacts": sp.MemberFacts,
        "ALL_ELIGIBLE": sp.ALL_ELIGIBLE,
    }
    exec(compile("from __future__ import annotations\n" + _REFERENCE_1064,
                 "<_lib_stale_proof at d6bd99e65>", "exec"), namespace)
    return types.SimpleNamespace(**namespace)


def _raw(obs, q, captured, *, weekly=None, reset=RESET_ISO, five=None, key=None):
    return samples_lib.OAuthSample(
        account_key=ACCOUNT, observation_id=obs, requested_at_utc=q,
        captured_at_utc=captured, weekly_percent=weekly, weekly_resets_at_utc=reset,
        five_hour_percent=five,
        five_hour_resets_at_utc=None if five is None else FIVE_RESET_ISO,
        five_hour_window_key=key)


def _instant_text(epoch_s: int, digits: int, frac: int) -> str:
    import datetime as _dt
    base = _dt.datetime.fromtimestamp(epoch_s, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S")
    return base + ("" if digits == 0 else "." + f"{frac:09d}"[:digits]) + "Z"


#: Request instant of the tie clusters: the latest qualifying sample of the
#: low weekly rises (weekly) and of the low five-hour rises (five).
_TQ = "2026-10-24T03:30:00.000000Z"
_TQ5 = "2026-10-22T11:00:00.000000Z"


def _edge_samples() -> dict:
    """The deterministic edge clusters, by population name. Within each
    list the order is significant (complete-key duplicates keep theirs)."""
    return {
        # Order ties broken by observation id, and complete-key ties
        # (equal request instant and observation id, different captures)
        # resolved by original position, on both axes.
        "ties": [
            _raw("o:tie1", _TQ, "2026-10-24T03:30:01Z", weekly=10.0),
            _raw("o:tie2", _TQ, "2026-10-24T03:30:02Z", weekly=10.0),
            _raw("o:tie2", _TQ, "2026-10-24T03:30:03Z", weekly=10.0),
            _raw("o:ftie1", _TQ5, "2026-10-22T11:00:01Z", weekly=40.0, five=3.0,
                 key=FIVE_KEY),
            _raw("o:ftie2", _TQ5, "2026-10-22T11:00:02Z", weekly=40.0, five=3.0,
                 key=FIVE_KEY),
            _raw("o:ftie2", _TQ5, "2026-10-22T11:00:03Z", weekly=40.0, five=3.0,
                 key=FIVE_KEY),
        ],
        # The tolerance boundary: 60.0 is not more than 1 pp below 61, nor
        # 59.99 more than 1 pp above 58.99.
        "tolerance": [
            _raw("o:tolA", "2026-10-23T01:00:00.000000Z", "2026-10-23T01:00:01Z",
                 weekly=60.0),
            _raw("o:tolB", "2026-10-23T00:00:00.000000Z", "2026-10-23T00:00:01Z",
                 weekly=59.99),
        ],
        # The reset boundary: a capture ending exactly at the reset is before
        # it; one starting at it is not. Both lie in the near-reset margin.
        "reset": [
            _raw("o:atreset", "2026-10-24T04:59:58.000000Z", "2026-10-24T04:59:59Z",
                 weekly=90.0),
            _raw("o:pastreset", "2026-10-24T04:59:59.500000Z", "2026-10-24T05:00:00Z",
                 weekly=90.0),
        ],
        # The floor boundary: a capture starting exactly at the floor is in
        # the segment; the later-requested one captured a second earlier is
        # not.
        "floor": [
            _raw("o:atfloor", "2026-10-19T08:53:19.000000Z", "2026-10-19T08:53:20Z",
                 weekly=33.3),
            _raw("o:prefloor", "2026-10-19T08:53:25.000000Z", "2026-10-19T08:53:19Z",
                 weekly=33.3),
        ],
        # Precision overlap: q .15 lies inside the bound .1's interval.
        "overlap": [
            _raw("o:ov", "2026-10-23T10:00:00.150000Z", "2026-10-23T10:00:01Z",
                 weekly=99.5),
        ],
        # An unparsable request instant (order -1) and capture instant, and
        # an integer-epoch weekly window.
        "malformed": [
            _raw("o:badq", "not-an-instant", "2026-10-23T01:00:00Z", weekly=10.0),
            _raw("o:badcap", "2026-10-23T02:00:00.000000Z", "bad", weekly=50.0),
            _raw("o:intwin", "2026-10-24T03:00:00.000000Z", "2026-10-24T03:00:01Z",
                 weekly=51.0, reset=RESET),
        ],
        # `_latest` selects an unparsable request instant over a pre-1970
        # one, so the provider support it picks earns nothing.
        "epoch": [
            _raw("o:old", "1969-12-31T23:59:58.000000Z", "1969-12-31T23:59:59Z",
                 weekly=90.0),
            _raw("o:noq", "garbage", "1969-12-31T23:59:59Z", weekly=90.0),
        ],
        # A five-hour key whose conversion raises: every five-hour reader of
        # this population raises it, at the same call.
        "raise": [
            _raw("o:fine", "2026-10-22T10:00:00.000000Z", "2026-10-22T10:00:01Z",
                 weekly=40.0, five=4.0, key=FIVE_KEY),
            _raw("o:raise", "2026-10-22T10:30:00.000000Z", "2026-10-22T10:30:01Z",
                 weekly=40.0, five=4.0, key="x"),
        ],
        # Outside the read_samples domain, through the kernel's unprepared
        # fallback: a value `float` accepts that is not a number, and an
        # order tie of identities that do not compare (today's `_latest`
        # and sort raise only when they compare them).
        "fallback": [
            _raw("o:str", "2026-10-23T05:00:00.000000Z", "2026-10-23T05:00:01Z",
                 weekly="50"),
            _raw(None, _TQ5, "2026-10-22T11:00:01Z", weekly=40.0, five=3.0, key=FIVE_KEY),
            _raw("o:y", _TQ5, "2026-10-22T11:00:02Z", weekly=40.0, five=30.0,
                 key=FIVE_KEY),
        ],
    }


def _random_samples(rng) -> list:
    out = []
    start = 1792620000  # 2026-10-21T22:00:00Z
    for i in range(rng.randint(10, 30)):
        q = _instant_text(start + rng.randint(0, 2 * 86400 - 7200),
                          rng.choice((0, 1, 3, 6, 9)), rng.randint(0, 999_999_999))
        captured = rng.choice((q[:19] + "Z", q[:19] + "Z", "bad"))
        out.append(_raw(
            f"o:r{i}", q, captured,
            weekly=rng.choice((None, 0.0, 1.0, 18.0, 19.0, 19.5, 50.0, 51.0, 52.0,
                               73.0, 74.0, 75.0, 83.0, 84.0)),
            reset=rng.choice((RESET_ISO, RESET_ISO, RESET_ISO, RESET, RESET + 1200,
                              "2026-10-17T05:00:00Z", None, "garbage"))))
    five_start = 1792656000  # 2026-10-22T08:00:00Z
    for i in range(rng.randint(5, 15)):
        q = _instant_text(five_start + rng.randint(0, 16200),
                          rng.choice((0, 3, 6)), rng.randint(0, 999_999_999))
        five = rng.choice((None, 3.0, 4.0, 5.0, 15.0, 20.0, 21.0, 22.0))
        out.append(_raw(
            f"o:f{i}", q, rng.choice((q[:19] + "Z", q[:19] + "Z", "bad")),
            weekly=rng.choice((None, 40.0)),
            five=five, key=rng.choice((FIVE_KEY, FIVE_KEY, FIVE_KEY - 18000, None))))
    return out


def _merge_in_order(rng, base: list, extra: list) -> list:
    """``extra`` inserted at random positions of ``base``, its own order kept."""
    out = list(base)
    positions = sorted(rng.randint(0, len(base)) for _ in extra)
    for offset, (pos, item) in enumerate(zip(positions, extra)):
        out.insert(pos + offset, item)
    return out


def _attempt(call):
    try:
        return ("ok", call())
    except Exception as exc:  # noqa: BLE001 - the exception IS the outcome
        return ("raise", type(exc).__name__, str(exc))


def _norm(value):
    """A result with every provider sample replaced by its object identity,
    so a returned sample is compared as the very object, not an equal one."""
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_norm(v) for v in value)
    if hasattr(value, "observation_id") and hasattr(value, "captured_at_utc"):
        return ("sample", id(value))
    return value


def test_prepared_view_matches_reference_oracle(sp):
    """#1064 test 1 (accuracy: a different refusal, drop fact, receipt,
    proof or compacted set after the prepared-view rewrite). Every public
    reader, called with a shared prepared view and with a plain list, returns
    exactly what today's code (the verbatim reference) returns, raises
    exactly what it raises, and returns the very sample objects it returns,
    over generated populations that make every enumerated outcome occur."""
    import random
    ref = _reference_kernel(sp)
    prepare = getattr(sp, "prepare", tuple)
    rng = random.Random(1064)
    E2 = "2026-10-23T12:00:00.000000Z"
    weekly = [member(sp, *spec[:1], percent=spec[1], t_m=spec[2], reset=spec[3])
              for spec in (("w30", 30, T_M, RESET),
                           ("w60", 60, "2026-10-22T09:00:00Z", None),
                           ("w74", 74, T_M, RESET),
                           ("w74b", 74, "2026-10-23T10:00:00.1Z", RESET - 60),
                           ("w82", 82, T_M, None),
                           ("w83", 83, T_M, RESET),
                           ("w95", 95, "1969-12-31T00:00:00.000Z", RESET),
                           ("t61", 61, T_M, RESET))]
    five = [five_member(sp, name, percent=pct, t_m=t_m, reset=reset)
            for name, pct, t_m, reset in (("f5", 5.0, T_M5, FIVE_RESET),
                                          ("f20", 20.0, T_M5, None),
                                          ("f22", 22.0, "2026-10-22T08:00:00Z",
                                           FIVE_RESET))]
    by_name = {m.sigma.token: name for name, m in zip(
        ("w30", "w60", "w74", "w74b", "w82", "w83", "w95", "t61"), weekly)}
    by_name.update({m.sigma.token: n for n, m in zip(("f5", "f20", "f22"), five)})

    def drop(name, bound, percent, reset, *, axis="sevenDay", window=RESET):
        return sp.DropMember(_token(name), bound, axis, float(percent), window, reset)

    drops = [drop("d0", "2026-10-21T00:00:00Z", 0, RESET),
             drop("d18", T_M, 18, None),
             drop("d50", "2026-10-22T09:00:00.000Z", 50, RESET - 60),
             drop("d70", "2026-10-23T00:00:00Z", 70, RESET),
             drop("d98a", "2026-10-23T10:00:00.1Z", 98, RESET),
             drop("d98b", "2026-10-23T10:00:00.000Z", 98, RESET),
             drop("dshift", T_M, 18, None, window=RESET + 600),
             drop("t5899", T_M, 58.99, RESET)]
    five_drops = [drop("e3", T_M5, 3, FIVE_RESET, axis="fiveHour", window=FIVE_KEY),
                  drop("e15", "2026-10-22T10:00:00Z", 15, None, axis="fiveHour",
                       window=FIVE_KEY)]
    names = {d.token: n for n, d in zip(
        ("d0", "d18", "d50", "d70", "d98a", "d98b", "dshift", "t5899", "e3", "e15"),
        drops + five_drops)}
    bad = credit(sp, "wr:early", q="2026-10-22T09:00:00.000000Z")
    accounteds = {"a83": accounted(sp, 83.0), "a74": accounted(sp, 74.0, floor=None),
                  "a30": accounted(sp, 30.0), "anone": accounted(sp, None),
                  "five20": five_accounted(sp, 20.0)}
    transitions = {"none": [], "late": [credit(sp, "wr:late",
                                               q="2026-10-23T23:00:00.000000Z")],
                   "early": [bad], "sl": [credit(sp, "wr:sl", kind="statusline")]}
    five_transitions = {
        "none": [], "sl": [five_credit(sp, "fhc:sl", kind="statusline")],
        "provider": [five_credit(sp, "fhc:p", arming="2026-10-22T08:00:00.000000Z",
                                 baseline="2026-10-22T07:00:00.000000Z",
                                 baseline_percent=10.0)]}
    epoch_member = member(sp, "ep", percent=80, t_m="1969-12-31T00:00:00.000Z")

    def calls(pop, name):
        """``(label, axis, call(impl, samples))`` for one population."""
        out = []
        for m in weekly + five:
            tag = by_name[m.sigma.token]
            for after_tag, after in (("own", m.sigma.activity_at_utc), ("e", E),
                                     ("e2", E2), ("e5", E5)):
                out.append((f"stale:{tag}:{after_tag}", m.sigma.axis,
                            lambda k, s, m=m, after=after: k.proven_stale(
                                m.sigma, s, after_utc=after,
                                advertised_reset_epoch=m.advertised_reset_epoch)))
        for d in drops + five_drops:
            for reset_tag, reset in (("", d.advertised_reset_epoch), (":noreset", None)):
                out.append((f"low:{names[d.token]}{reset_tag}", d.axis,
                            lambda k, s, d=d, reset=reset: k.proven_stale_low(
                                d.bound_utc, s, axis=d.axis, window=d.window,
                                percent=d.percent, advertised_reset_epoch=reset)))
        for tag, acc in accounteds.items():
            out.append((f"earn:{tag}", "sevenDay", lambda k, s, acc=acc: k.earn_receipts(
                weekly + five, acc, s, completed_at_utc=E, index_identity="ix")))
        if name == "epoch":
            out.append(("earn:epoch", "sevenDay", lambda k, s: k.earn_receipts(
                [epoch_member], accounted(sp, 85.0, floor=None), s,
                completed_at_utc=E, index_identity="ix")))
        # Receipts: earned now, and re-earned at E2 under the credit that
        # invalidates the first generation.
        held = []
        for tag in ("a83", "a74", "a30", "five20"):
            held.extend(ref.earn_receipts(weekly + five, accounteds[tag], list(pop),
                                          completed_at_utc=E, index_identity="ix"))
        again = list(ref.earn_receipts(weekly, accounted(sp, 83.0, credits=(bad,)),
                                       list(pop), completed_at_utc=E2,
                                       index_identity="ix"))
        for t_tag, trans in transitions.items():
            for s_tag, supports in (("all", None), ("support", frozenset({"o:support"}))):
                out.append((f"refusal:w:{t_tag}:{s_tag}", "sevenDay",
                            lambda k, s, trans=trans, supports=supports: k.refusal_facts(
                                "sevenDay", weekly, s, held, trans, index_identity="ix",
                                supports=supports, drop_members=drops)))
        out.append(("refusal:w:reearned", "sevenDay", lambda k, s: k.refusal_facts(
            "sevenDay", weekly, s, held + again, [bad], index_identity="ix",
            drop_members=drops)))
        out.append(("refusal:w:rebuilt", "sevenDay", lambda k, s: k.refusal_facts(
            "sevenDay", weekly, s, held, [], index_identity="other",
            drop_members=drops)))
        for t_tag, trans in five_transitions.items():
            out.append((f"refusal:f:{t_tag}", "fiveHour",
                        lambda k, s, trans=trans: k.refusal_facts(
                            "fiveHour", five, s, held, trans, index_identity="ix",
                            drop_members=five_drops)))
        for tag, kwargs in (("w:floor", dict(window=RESET, floor_epoch=FLOOR)),
                            ("w:nofloor", dict(window=RESET, floor_epoch=None)),
                            ("w:shifted", dict(window=RESET + 600, floor_epoch=FLOOR)),
                            ("f:nofloor", dict(window=FIVE_KEY, floor_epoch=None,
                                               axis="fiveHour")),
                            ("f:floor", dict(window=FIVE_KEY, floor_epoch=FIVE_KEY - 7200,
                                             axis="fiveHour"))):
            out.append((f"compact:{tag}", "fiveHour" if tag.startswith("f") else "sevenDay",
                        lambda k, s, kwargs=kwargs: k.compact_samples(s, **kwargs)))
        reearned = any(
            r1.sigma.key() == r2.sigma.key()
            and not ref.receipt_valid(r1, [bad], index_identity="ix")
            and ref.receipt_valid(r2, [bad], index_identity="ix")
            for r1 in held for r2 in again)
        return out, reearned

    edges = _edge_samples()
    merged = [s for key in ("ties", "tolerance", "reset", "floor", "overlap", "malformed")
              for s in edges[key]]
    populations = [(key, list(edges[key])) for key in edges]
    for _ in range(25):
        populations.append(("random", _merge_in_order(rng, _random_samples(rng), merged)))

    obj = {f"{s.observation_id}@{s.captured_at_utc}": s
           for _key, pop in populations for s in pop}
    targeted = {
        ("ties", "stale:w74:own"): ("obs_tie", lambda r: r is obj["o:tie2@2026-10-24T03:30:02Z"]),
        ("ties", "stale:f5:own"): ("complete_key_tie",
                                   lambda r: r is obj["o:ftie2@2026-10-22T11:00:02Z"]),
        ("ties", "compact:w:floor"): ("compact_dup_identity", lambda r: any(
            s is obj["o:tie2@2026-10-24T03:30:02Z"] for s in r) and not any(
            s is obj["o:tie2@2026-10-24T03:30:03Z"] for s in r)),
        ("tolerance", "stale:t61:own"): ("tolerance_rise",
                                        lambda r: r is obj["o:tolB@2026-10-23T00:00:01Z"]),
        ("tolerance", "low:t5899"): ("tolerance_drop",
                                     lambda r: r is obj["o:tolA@2026-10-23T01:00:01Z"]),
        ("reset", "low:d0"): ("reset_boundary",
                              lambda r: r is obj["o:atreset@2026-10-24T04:59:59Z"]),
        ("floor", "compact:w:floor"): ("floor_boundary", lambda r: any(
            s is obj["o:atfloor@2026-10-19T08:53:20Z"] for s in r)),
        ("overlap", "low:d98a"): ("precision_overlap_refused", lambda r: r is None),
        ("overlap", "low:d98b"): ("precision_overlap_ordered",
                                  lambda r: r is obj["o:ov@2026-10-23T10:00:01Z"]),
        ("epoch", "earn:epoch"): ("unparsable_q_selected", lambda r: r == () and (
            ref._latest(ref._in_window(edges["epoch"], RESET)).observation_id == "o:noq")),
        ("epoch", "stale:w95:own"): ("pre_epoch_proof",
                                     lambda r: r is obj["o:old@1969-12-31T23:59:59Z"]),
    }
    seen = set()
    for name, pop in populations:
        view = prepare(pop)
        if any(s.weekly_percent is None for s in pop):
            seen.add("missing_value")
        battery, reearned = calls(pop, name)
        if reearned:
            seen.add("reearned")
        for label, axis, call in battery:
            want = _attempt(lambda: call(ref, list(pop)))
            for variant, samples in (("list", lambda: list(pop)), ("view", lambda: view)):
                got = _attempt(lambda: call(sp, samples()))
                assert _norm(got) == _norm(want), (name, label, variant, got, want)
            if want[0] == "raise":
                assert name == "fallback" or (name == "raise" and axis == "fiveHour"), (
                    name, label, want)
                seen.add("raise_same" if name == "raise" else "fallback_raise")
                continue
            result = want[1]
            check = targeted.get((name, label))
            if check is not None:
                flag, predicate = check
                assert predicate(result), (name, label, result)
                seen.add(flag)
            if label.startswith("earn:"):
                seen.update("earned_" + r.path for r in result)
            elif label.startswith("refusal:"):
                for token, f in result.items():
                    if f.refused:
                        seen.update(("rise_refused", axis + "_refused"))
                    elif f.proof_observation_id is not None:
                        seen.add("rise_allowed")
                    if token in names:
                        seen.add("drop_refused" if f.drop_refused else "drop_allowed")
                        if f.drop_refused:
                            seen.add(axis + "_refused")
            elif label.startswith("compact:"):
                margin = RESET - 3600 if axis == "sevenDay" else FIVE_KEY - 3600
                for s in result:
                    interval = ref._interval(s.captured_at_utc)
                    if interval is None:
                        seen.add("unparsable_capture_kept")
                    elif interval[1] > margin * 1_000_000_000:
                        seen.add("margin_kept")
                    if isinstance(s.weekly_resets_at_utc, int) and axis == "sevenDay":
                        seen.add("int_window")
    required = {"earned_maximal", "earned_provider_support", "reearned", "rise_refused",
                "rise_allowed", "drop_refused", "drop_allowed", "sevenDay_refused",
                "fiveHour_refused", "margin_kept", "unparsable_capture_kept",
                "int_window", "missing_value", "raise_same", "obs_tie",
                "complete_key_tie", "compact_dup_identity", "tolerance_rise",
                "tolerance_drop", "reset_boundary", "floor_boundary",
                "precision_overlap_refused", "precision_overlap_ordered",
                "unparsable_q_selected", "pre_epoch_proof"}
    assert required <= seen, "outcomes that never occurred: " + repr(sorted(required - seen))


def _clear_kernel_memos(sp):
    for value in list(vars(sp).values()):
        clear = getattr(value, "cache_clear", None)
        if callable(clear):
            clear()


def test_parser_work_scales_with_samples_not_members(sp, monkeypatch):
    """#1064 test 2 (machine safety: a status-line reducer pass held the
    selected-state lock for up to an hour at full CPU, because every member's
    proof re-parsed every sample's instants and re-normalized its weekly
    window). The parses and normalizations of one kernel call are
    proportional to its samples, not to samples x members: more samples than
    the memo bound, every instant and weekly reset unique (so eviction cannot
    hide repeated parsing), every sample qualifying for every member's
    proof, and member populations m and 4m."""
    n = 70_000
    start = RESET - 4 * 86400
    pop = [sample(f"o:{i}", q=_instant_text(start + i, 6, i * 1000),
                  weekly=50.0, reset=f"2026-10-24T05:00:00.{i:06d}Z")
           for i in range(n)]
    drops = [sp.DropMember(_token(f"d{j}"), "2026-10-01T00:00:00Z", "sevenDay",
                           10.0, RESET, RESET) for j in range(8)]
    counts = {"parse": 0, "window": 0}
    real_key, real_window = sp._activity.instant_key, sp._samples.weekly_window

    def instant_key(text):
        counts["parse"] += 1
        return real_key(text)

    def weekly_window(value):
        counts["window"] += 1
        return real_window(value)

    monkeypatch.setattr(sp._activity, "instant_key", instant_key)
    monkeypatch.setattr(sp._samples, "weekly_window", weekly_window)

    def work(m):
        _clear_kernel_memos(sp)
        counts.update(parse=0, window=0)
        facts = sp.refusal_facts("sevenDay", [], pop, (), (), index_identity="ix",
                                 drop_members=drops[:m])
        assert [f.drop_proof_observation_id for f in facts.values()] == [f"o:{n - 1}"] * m
        return counts["parse"] + counts["window"], dict(counts)

    small, small_counts = work(2)
    large, large_counts = work(8)
    assert small >= n, small_counts
    assert large / small < 1.5, (
        f"parser work multiplies with members: m=2 {small_counts}, m=8 {large_counts}, "
        f"ratio {large / small:.2f}")


# =============================================================================
# Part 2: the real intake, detached reducer and record kernel
# (`tests/_stale_contributor_trace.py`).
# =============================================================================

import _stale_contributor_trace as S  # noqa: E402

DAY = "2026-10-22T"
at = S.at


class Crash(BaseException):
    """A simulated process death at a crash point."""


def _pcts(t):
    return [p for _o, _n, p in t.resets()]


def trace_d7b(t):
    """D7b q1: a genuine post-credit 30 retained as a pending low under a
    stale published 90 earns nothing, so after a second credit the 30 is
    admitted and the next low reveals the third."""
    p, g = t.session("idle90"), t.session("genuine")
    p.respond(DAY + "08:50:00Z")
    t.oauth(80, at=DAY + "09:00:00Z")
    t.oauth(0, at=DAY + "09:10:00Z")
    g.respond(DAY + "09:11:00Z")
    t.render(p, 90, at=DAY + "09:11:05Z")
    t.render(g, 30, at=DAY + "09:11:20Z")
    t.render(p, 90, at=DAY + "09:11:50Z")
    t.render(g, 30, at=DAY + "09:12:20Z")
    pending = sorted(r.sigma.percent for r in t.receipts())
    t.oauth(0, at=DAY + "09:13:00Z")
    t.render(p, 90, at=DAY + "09:13:30Z")
    t.render(g, 30, at=DAY + "09:13:40Z")
    t.oauth(0, at=DAY + "09:15:00Z")
    t.render(p, 90, at=DAY + "09:15:30Z")
    t.render(g, 30, at=DAY + "09:15:40Z")
    return {"receipts_under_90": pending, "resets": _pcts(t),
            "statusline": t.published()}


def trace_d7a(t):
    """D7a: a credit whose causal low is an OAuth sample requested before
    e_sigma invalidates the receipt; the replay is admitted and the later
    sample reveals the second credit."""
    g = t.session("genuine")
    t.oauth(50, at=DAY + "09:00:00Z")
    t.oauth(0, at=DAY + "09:10:00Z")
    g.respond(DAY + "09:11:00Z")
    t.render(g, 30, at=DAY + "09:11:10Z")
    earned = [(r.sigma.percent, r.path, r.completed_at_utc) for r in t.receipts()]
    t.oauth(0, at=DAY + "09:11:30Z", lead=25)   # q 09:11:05 < e_sigma
    t.oauth(0, at=DAY + "09:12:00Z")
    t.render(g, 30, at=DAY + "09:12:30Z")
    t.oauth(0, at=DAY + "09:13:00Z")
    t.render(g, 30, at=DAY + "09:13:30Z")
    t.render(g, 30, at=DAY + "09:14:00Z")
    return {"earned": earned, "resets": _pcts(t), "statusline": t.published()}


def trace_statusline_floor(t, *, provider_low: bool):
    """A credit whose causal low is a delayed status-line reading keeps the
    receipt (R15). Without a later provider sample the accounted 30 is
    admitted again; with one (``provider_low``) it is refused and the
    genuine second credit that sample reflects is lost (R15, pinned)."""
    g, z = t.session("genuine"), t.session("delayed")
    t.oauth(50, at=DAY + "09:00:00Z")
    t.oauth(0, at=DAY + "09:10:00Z")
    z.respond(DAY + "09:10:30Z")
    g.respond(DAY + "09:11:00Z")
    t.render(g, 30, at=DAY + "09:11:10Z")
    for i in range(0, 5 * 60, 30):
        t.render(z, 0, at=at(DAY + "09:12:00Z", i))
    if provider_low:
        t.oauth(0, at=DAY + "09:17:30Z")
    t.render(g, 30, at=DAY + "09:18:00Z")
    t.render(g, 30, at=DAY + "09:18:30Z")
    if provider_low:
        t.oauth(0, at=DAY + "09:19:00Z")
    return {"resets": _pcts(t), "statusline": t.published(),
            "receipt": (r := t.receipt_for(g, 30)) and r.path}


TRACE_EXPECTED = {
    # The 30 earns nothing under the stale published 90, so the second credit
    # (90 -> 0) admits it and the third low reveals the third credit.
    "d7b": {"receipts_under_90": [90.0], "resets": [80.0, 90.0, 30.0],
            "statusline": [90.0, 30.0]},
    # The provider floor requested before e_sigma invalidates the receipt: the
    # replay is admitted and the later sample reveals the second credit.
    "d7a": {"earned": [(30.0, "maximal", "2026-10-22T09:11:10.000000Z")],
            "resets": [50.0, 30.0, 30.0], "statusline": [30.0, 30.0]},
    # A status-line credit keeps the receipt (R15); without a provider proof
    # the accounted 30 is admitted again.
    "floor": {"resets": [50.0, 30.0], "statusline": [30.0, 0.0, 30.0],
              "receipt": "maximal"},
    # R15, pinned: with a provider sample after e_sigma the replay is refused.
    "floor_r15": {"resets": [50.0, 30.0], "statusline": [30.0, 0.0],
                  "receipt": "maximal"},
}


def _run(name, t):
    if name == "d7b":
        return trace_d7b(t)
    if name == "d7a":
        return trace_d7a(t)
    return trace_statusline_floor(t, provider_low=name == "floor_r15")


@pytest.mark.parametrize("name", sorted(TRACE_EXPECTED))
def test_admission_traces(monkeypatch, tmp_path, name):
    t = S.Trace(monkeypatch, tmp_path)
    result = _run(name, t)
    print(name, result)
    stores = t.maintenance()
    S.assert_same_outcomes(stores["live"], stores["rebuilt"], stores["rederived"])
    assert result == TRACE_EXPECTED[name], "RESULT=" + repr(result)


#: Which admission trace each receipt mutation control must break (the
#: drop refusal's mutations, #1019, are pinned by `MUTATION_BITES` and the
#: L-tests of `tests/test_797_stale_contributor_evidence.py`).
MUTATION_TARGETS = {
    "domination-alone": "d7b",
    "numerical-domination": "d7b",
    "capture-instant": "d7a",
    "statusline-sample": "floor",
    "provider-floor-before-receipt": "d7a",
}


@pytest.mark.parametrize("mutation", S.RECEIPT_MUTATIONS)
def test_mutation_controls_break_the_admission_traces(monkeypatch, tmp_path, mutation):
    t = S.Trace(monkeypatch, tmp_path)
    S.mutate(monkeypatch, mutation)
    name = MUTATION_TARGETS[mutation]
    result = _run(name, t)
    print(mutation, result)
    assert TRACE_EXPECTED[name] is not None
    assert result["resets"] != TRACE_EXPECTED[name]["resets"] or (
        result["statusline"] != TRACE_EXPECTED[name]["statusline"]), (
        f"{mutation} left {name} unchanged: the control is vacuous")


# --- earning and checkpointing through the real reducer ------------------------------


def test_earning_only_at_a_covered_completion(monkeypatch, tmp_path):
    """No receipt at capture or at an evaluation that cannot complete; the
    completion that follows earns it with its own, later e_sigma."""
    t = S.Trace(monkeypatch, tmp_path)
    sl = t.sl  # the trace loads a fresh module set: patch the live one
    a = t.session("idle74")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    real = sl._covered_drop_status
    monkeypatch.setattr(sl, "_covered_drop_status", lambda *a, **k: sl._UNCOVERED)
    t.render(a, 74, at=DAY + "09:00:30Z")
    assert t.receipts() == [], "an uncovered evaluation earned a receipt"
    monkeypatch.setattr(sl, "_covered_drop_status", real)
    t.render(a, 74, at=DAY + "09:01:30Z")
    (receipt,) = t.receipts()
    assert (receipt.path, receipt.completed_at_utc) == (
        "provider_support", "2026-10-22T09:01:30.000000Z")
    assert receipt.provider_percent == 83.0


def test_earning_ii_never_from_a_max_clamped_value(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    m = t.session("idle82")
    t.oauth(83, at=DAY + "09:00:00Z")
    m.respond(DAY + "09:01:00Z")
    t.oauth(80, at=DAY + "09:02:00Z")
    stored = t.stored()
    t.render(m, 82, at=DAY + "09:02:30Z")
    t.render(m, 82, at=DAY + "09:03:00Z")
    assert stored == 83.0, "the stored value is the clamped 83"
    assert t.receipt_for(m, 82) is None, "a MAX-clamped value supported 82"


def test_publication_completion_earns_maximal(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    g = t.session("genuine")
    g.respond(DAY + "09:00:00Z")
    t.render(g, 30, at=DAY + "09:00:10Z")
    assert t.published() == [30.0]
    (receipt,) = t.receipts()
    assert (receipt.path, receipt.accounted_percent, receipt.completed_at_utc) == (
        "maximal", 30.0, "2026-10-22T09:00:10.000000Z")


def test_oauth_reconciliation_preserves_receipts(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    a = t.session("idle74")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 74, at=DAY + "09:00:30Z")
    before = t.receipts()
    assert len(before) == 1
    generation = t.transition().generation
    t.oauth(84, at=DAY + "09:01:00Z")
    after = t.transition()
    assert after.generation > generation, "the OAuth path rewrote the transition"
    assert list(after.receipts) == before


def test_a_crash_loses_a_receipt_that_is_re_earned_later(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    sl = t.sl
    a = t.session("idle74")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    real = sl._write_transition
    crashed = []

    def write(transition):
        if transition.receipts and not crashed:
            crashed.append(transition.receipts)
            raise Crash("before the checkpoint")
        real(transition)

    monkeypatch.setattr(sl, "_write_transition", write)
    with pytest.raises(Crash):
        t.render(a, 74, at=DAY + "09:00:30Z")
    assert crashed and t.receipts() == []
    monkeypatch.setattr(sl, "_write_transition", real)
    t.render(a, 74, at=DAY + "09:02:00Z")
    (receipt,) = t.receipts()
    assert receipt.completed_at_utc == "2026-10-22T09:02:00.000000Z", (
        "e_sigma was reconstructed from an earlier capture")


#: #1019 (P-R2): a member a later-requested provider sample proves stale by
#: more than the 1 pp tolerance is rise-ineligible whatever its receipt, so
#: the receipt traces below present an idle 82 beside the OAuth 83 (inside
#: the drop proof's tolerance): the member's eligibility they observe is the
#: receipt's alone. #797 pinned them with an idle 74, which the drop proof
#: now refuses in both directions.
def test_rebuild_invalidates_receipts(monkeypatch, tmp_path):
    import _outcome_quarantine_trace as T
    t = S.Trace(monkeypatch, tmp_path)
    a = t.session("idle82")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 82, at=DAY + "09:00:30Z")
    (old,) = t.receipts()
    T.rebuild(t.app)
    t.oauth(0, at=DAY + "09:10:00Z")
    t.render(a, 82, at=DAY + "09:10:30Z")
    # The receipt was bound to the old index identity: the replay is
    # admitted (R16) and its publication earns a new receipt.
    assert t.published() == [82.0]
    assert all(r.index_identity != old.index_identity for r in t.receipts())


def test_eviction_at_the_document_limit_means_eligible(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    sl = t.sl
    lib = sys.modules["_lib_statusline_candidates"]
    a = t.session("idle82")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    real = sl._fit_transition

    def tight(transition, **kwargs):
        size = len(json.dumps(sl._transition_document(
            dataclasses.replace(transition, receipts=())), sort_keys=True,
            separators=(",", ":")).encode())
        monkeypatch.setattr(lib, "CONTROL_DOCUMENT_MAX_BYTES", size + 10)
        try:
            return real(transition, **kwargs)
        finally:
            monkeypatch.setattr(lib, "CONTROL_DOCUMENT_MAX_BYTES", 1 << 20)

    monkeypatch.setattr(sl, "_fit_transition", tight)
    t.render(a, 82, at=DAY + "09:00:30Z")
    assert t.receipts() == [], "the new receipt was omitted at the limit"
    monkeypatch.setattr(sl, "_fit_transition", real)
    t.oauth(0, at=DAY + "09:10:00Z")
    monkeypatch.setattr(sl, "_fit_transition", tight)
    t.render(a, 82, at=DAY + "09:10:30Z")
    assert t.published() == [82.0], "an evicted member was refused"


def test_last_resort_eviction_of_a_refusing_receipt_re_reduces_the_work(
        monkeypatch, tmp_path):
    """Review RV-4 (spec 4.4: dependencies of outstanding work are retained
    or their refusal authority is invalidated and the work re-reduced). The
    member is refused on its held receipt; this completion cannot fit that
    receipt, so the last resort evicts it although the evaluation it refused
    references it. The refusal no longer has a receipt to rest on: the
    evaluation is reduced again with the member eligible, and its rise
    publishes now rather than waiting for another reading. An unchanged
    render no longer dispatches a completion (#1019 review finding P1), so a
    new provider sample changes the index and starts it."""
    t = S.Trace(monkeypatch, tmp_path)
    sl = t.sl
    lib = sys.modules["_lib_statusline_candidates"]
    a = t.session("idle82")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 82, at=DAY + "09:00:30Z")
    assert len(t.receipts()) == 1
    t.oauth(0, at=DAY + "09:10:00Z")
    t.render(a, 82, at=DAY + "09:10:30Z")
    assert t.published() == [], "the replay must be refused on its receipt"
    assert len(t.receipts()) == 1
    real = sl._fit_transition

    def tight(transition, **kwargs):
        size = len(json.dumps(sl._transition_document(
            dataclasses.replace(transition, receipts=())), sort_keys=True,
            separators=(",", ":")).encode())
        monkeypatch.setattr(lib, "CONTROL_DOCUMENT_MAX_BYTES", size + 10)
        try:
            return real(transition, **kwargs)
        finally:
            monkeypatch.setattr(lib, "CONTROL_DOCUMENT_MAX_BYTES", 1 << 20)

    monkeypatch.setattr(sl, "_fit_transition", tight)
    t.oauth(0, at=DAY + "09:10:50Z")
    t.render(a, 82, at=DAY + "09:11:00Z")
    assert t.receipts() == [], "the referenced receipt was not evicted"
    assert t.published() == [82.0], "the evicted refusal was not re-reduced"


def test_a_hold_of_the_support_invalidates(monkeypatch, tmp_path, capsys):
    """A reviewed hold of the provider sample B is applied through `db
    rederive`, which rebuilds the index: the receipt it supported no longer
    authorizes a refusal. (A hold needs an earlier accepted weekly basis for
    the account, hence the first sample.)"""
    import _cctally_journal as jr
    t = S.Trace(monkeypatch, tmp_path)
    a = t.session("idle82")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(80, at=DAY + "08:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 82, at=DAY + "09:00:30Z")
    (receipt,) = t.receipts()
    high_water = jr.journal_high_water()
    manifest = {
        "schemaVersion": 2,
        "journalHighWater": {"segment": high_water[0], "offset": high_water[1]},
        "journalPrefixHash": jr.journal_prefix_hash(high_water),
        "reviewedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
        "reason": "T15 hold of the support",
        "weeklyAxisDecisions": [{"observationId": receipt.provider_observation_id,
                                 "disposition": "hold"}],
        "snapshotIdentityDecisions": [],
    }
    path = tmp_path / "hold.json"
    path.write_text(json.dumps(manifest))
    parser = t.app.build_parser()
    base = ["db", "rederive", "--family", "claude-usage",
            "--reviewed-weekly-decisions", str(path), "--json"]
    capsys.readouterr()
    rc = t.app.cmd_db_rederive(parser.parse_args(base))
    out = capsys.readouterr()
    assert rc == 0, "OUT=" + out.out + " ERR=" + out.err
    preview = json.loads(out.out)
    manifest.update(expectedBaselinePlanHash=preview["baselinePlanHash"],
                    expectedDecisionPlanHash=preview["decisionPlanHash"])
    path.write_text(json.dumps(manifest))
    rc = t.app.cmd_db_rederive(parser.parse_args(base + ["--yes"]))
    out = capsys.readouterr()
    assert rc == 0, "OUT=" + out.out + " ERR=" + out.err
    t.oauth(0, at=DAY + "09:10:00Z")
    t.render(a, 82, at=DAY + "09:10:30Z")
    assert t.published() == [82.0]


# --- T15 five-hour rows through the real paths (Task 5) -----------------------------


def _five_credits_of(t):
    conn = t.app.open_db()
    try:
        return t.sl._five_hour_window_credits(conn, t.account, FIVE_KEY)
    finally:
        conn.close()


def test_five_hour_provider_credit_facts_are_raw_and_from_the_arming_observation(
        monkeypatch, tmp_path):
    """A credit the `api` source confirms resolves its causal facts from the
    admitted samples of its own observations: the baseline's RAW five-hour
    value (15, under a status-line 20 that the stored column MAX-clamps to)
    and request instant, and the ARMING observation's request instant —
    never the later confirming one's (raw-versus-clamped and
    arming-versus-confirmation, through the record kernel)."""
    t = S.Trace(monkeypatch, tmp_path)
    s = t.session("sl20")
    s.respond(DAY + "08:59:50Z")
    t.oauth(40, at=DAY + "08:59:00Z")
    t.render(s, 40, five=20, at=DAY + "09:00:30Z")
    q_baseline = t.oauth(40, five=15, at=DAY + "09:01:00Z")
    q_arming = t.oauth(40, five=5, at=DAY + "09:02:00Z")
    q_confirming = t.oauth(40, five=4, at=DAY + "09:03:00Z")
    assert t.five_credits()[-1][:2] == (15.0, 5.0), t.five_credits()
    (credit,) = [c for c in _five_credits_of(t) if c.causal_kind == "provider"]
    assert credit.baseline_percent == 15.0
    assert credit.baseline_requested_at_utc == q_baseline
    assert credit.arming_requested_at_utc == q_arming != q_confirming
    # A receipt for a status-line 20 earned before the credit: the provider
    # credit survives it only with a baseline at or above 20, so it
    # invalidates (Codex's example, through the real facts).
    sp = importlib.import_module("_lib_stale_proof")
    receipt = five_receipt(sp, five_member(sp, percent=20.0, t_m="2026-10-22T08:59:50.000000Z"),
                           e="2026-10-22T09:00:30.000000Z", index="ix")
    assert not sp.receipt_valid(receipt, [credit], index_identity="ix")
    lowered = five_receipt(sp, five_member(sp, percent=15.0, t_m="2026-10-22T08:59:50.000000Z"),
                           e="2026-10-22T09:00:30.000000Z", index="ix")
    assert sp.receipt_valid(lowered, [credit], index_identity="ix")


def test_five_hour_statusline_credit_resolves_as_statusline(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    idle, x = t.session("idle20"), t.session("x")
    idle.respond(DAY + "09:00:20Z")
    t.oauth(40, at=DAY + "09:00:00Z")
    t.render(idle, 40, five=20, at=DAY + "09:00:30Z")
    x.respond(DAY + "09:01:00Z")
    t.render(x, 40, five=5, at=DAY + "09:01:10Z")
    t.render(x, 40, five=5, at=DAY + "09:04:20Z")
    t.render(x, 40, five=5, at=DAY + "09:04:40Z")
    (credit,) = _five_credits_of(t)
    assert credit.causal_kind == "statusline"
    assert credit.event_revision and credit.event_id.startswith("fhc")


# --- #1064: one prepared view per snapshot owner -----------------------------------


def test_one_view_per_stale_inputs_owner(monkeypatch, tmp_path):
    """#1064 test 3 (machine safety and accuracy: a pass that re-prepares
    one snapshot's samples per kernel call keeps the minutes-long hold; a
    view reused across snapshots answers for the wrong one). Through the
    real reducer: the classification that serves two evaluations of one
    pass prepares its samples exactly once, two classifications with
    different answers get their own views, and every refusal fact, refuted
    support, receipt map, render cache and render decision equals the one
    computed with the views disabled (each kernel call preparing its own)."""
    import collections
    import contextlib
    t = S.Trace(monkeypatch, tmp_path)
    sl = t.sl
    sp = importlib.import_module("_lib_stale_proof")
    state = {"reference": False}
    prepared: collections.Counter = collections.Counter()
    alive = []
    real_prepare = getattr(sp, "prepare", None)
    if real_prepare is not None:
        def prepare(samples):
            if not state["reference"] and isinstance(samples, tuple) and samples:
                prepared[id(samples)] += 1
                alive.append(samples)
            return real_prepare(samples)
        monkeypatch.setattr(sp, "prepare", prepare)

    @contextlib.contextmanager
    def views_disabled():
        saved = []
        for cls in (sl._StaleInputs, sl._StaleCache):
            for name, field in (("sample_view", "samples"),
                                ("five_sample_view", "five_samples")):
                saved.append((cls, name, cls.__dict__.get(name)))
                setattr(cls, name, property(lambda self, f=field: getattr(self, f)))
        state["reference"] = True
        try:
            yield
        finally:
            state["reference"] = False
            for cls, name, original in saved:
                if original is None:
                    delattr(cls, name)
                else:
                    setattr(cls, name, original)

    mismatches, owners, answers = [], [], {}

    def compared(name):
        real = getattr(sl, name)

        def wrapper(*args, **kwargs):
            got = real(*args, **kwargs)
            if not state["reference"]:
                with views_disabled():
                    want = real(*args, **kwargs)
                if got != want:
                    mismatches.append((name, got, want))
                if name == "_stale_member_facts" and args[0] is not None:
                    answers.setdefault(id(args[0]), []).append(got)
            return got
        monkeypatch.setattr(sl, name, wrapper)

    for name in ("_stale_member_facts", "_refuted_supports", "_completion_receipts",
                 "_receipt_work", "_render_member_facts"):
        compared(name)
    real_reduce = sl._reduce_evaluation

    def reduce(account_key, evaluation, transition, *, classified, **kwargs):
        owners.append(classified.stale)
        return real_reduce(account_key, evaluation, transition, classified=classified,
                           **kwargs)

    monkeypatch.setattr(sl, "_reduce_evaluation", reduce)

    a = t.session("idle82")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 82, at=DAY + "09:00:30Z")
    assert len(t.receipts()) == 1
    t.oauth(0, at=DAY + "09:10:00Z")
    b = t.session("idle81")
    b.respond("2026-10-21T22:51:00Z")
    # The first render's arrival stays in the intake (no worker runs for
    # it), so the next render's pass reduces both evaluations.
    real_worker = sl._continuation_worker
    monkeypatch.setattr(sl, "_continuation_worker", sl._release_reservation)
    t.render(a, 82, at=DAY + "09:10:30Z")
    monkeypatch.setattr(sl, "_continuation_worker", real_worker)
    first = len(owners)
    t.render(b, 81, at=DAY + "09:10:40Z")
    batch = [o for o in owners[first:] if o is not None]

    assert not mismatches, mismatches
    served = collections.Counter(id(o) for o in batch if o.samples)
    assert served and max(served.values()) >= 2, (
        "no classification served two evaluations of one pass", len(batch))
    shared = next(o for o in batch if served[id(o)] >= 2)
    assert prepared[id(shared.samples)] == 1, (
        "the shared snapshot's samples were prepared "
        f"{prepared[id(shared.samples)]} times")
    assert prepared and max(prepared.values()) == 1, prepared
    distinct = {id(o): o for o in owners if o is not None and o.samples}
    used = [o for o in distinct.values() if prepared[id(o.samples)] == 1]
    assert len(used) >= 2, "fewer than two classifications read their samples"
    assert len({id(o.sample_view) for o in used}) == len(used), "a view was shared"
    assert len({repr(answers.get(id(o))) for o in used}) >= 2, (
        "the classifications' answers do not differ: the test cannot tell their "
        "views apart")
