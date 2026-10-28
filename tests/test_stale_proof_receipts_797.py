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


#: Which admission trace each mutation control must break.
MUTATION_TARGETS = {
    "domination-alone": "d7b",
    "numerical-domination": "d7b",
    "capture-instant": "d7a",
    "statusline-sample": "floor",
    "provider-floor-before-receipt": "d7a",
}


@pytest.mark.parametrize("mutation", S.MUTATIONS)
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


def test_rebuild_invalidates_receipts(monkeypatch, tmp_path):
    import _outcome_quarantine_trace as T
    t = S.Trace(monkeypatch, tmp_path)
    a = t.session("idle74")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 74, at=DAY + "09:00:30Z")
    (old,) = t.receipts()
    T.rebuild(t.app)
    t.oauth(0, at=DAY + "09:10:00Z")
    t.render(a, 74, at=DAY + "09:10:30Z")
    # The receipt was bound to the old index identity: the replay is
    # admitted (R16) and its publication earns a new receipt.
    assert t.published() == [74.0]
    assert all(r.index_identity != old.index_identity for r in t.receipts())


def test_eviction_at_the_document_limit_means_eligible(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    sl = t.sl
    lib = sys.modules["_lib_statusline_candidates"]
    a = t.session("idle74")
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
    t.render(a, 74, at=DAY + "09:00:30Z")
    assert t.receipts() == [], "the new receipt was omitted at the limit"
    monkeypatch.setattr(sl, "_fit_transition", real)
    t.oauth(0, at=DAY + "09:10:00Z")
    monkeypatch.setattr(sl, "_fit_transition", tight)
    t.render(a, 74, at=DAY + "09:10:30Z")
    assert t.published() == [74.0], "an evicted member was refused"


def test_last_resort_eviction_of_a_refusing_receipt_re_reduces_the_work(
        monkeypatch, tmp_path):
    """Review RV-4 (spec 4.4: dependencies of outstanding work are retained
    or their refusal authority is invalidated and the work re-reduced). The
    member is refused on its held receipt; this completion cannot fit that
    receipt, so the last resort evicts it although the evaluation it refused
    references it. The refusal no longer has a receipt to rest on: the
    evaluation is reduced again with the member eligible, and its rise
    publishes now rather than waiting for another reading."""
    t = S.Trace(monkeypatch, tmp_path)
    sl = t.sl
    lib = sys.modules["_lib_statusline_candidates"]
    a = t.session("idle74")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 74, at=DAY + "09:00:30Z")
    assert len(t.receipts()) == 1
    t.oauth(0, at=DAY + "09:10:00Z")
    t.render(a, 74, at=DAY + "09:10:30Z")
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
    t.render(a, 74, at=DAY + "09:11:00Z")
    assert t.receipts() == [], "the referenced receipt was not evicted"
    assert t.published() == [74.0], "the evicted refusal was not re-reduced"


def test_a_hold_of_the_support_invalidates(monkeypatch, tmp_path, capsys):
    """A reviewed hold of the provider sample B is applied through `db
    rederive`, which rebuilds the index: the receipt it supported no longer
    authorizes a refusal. (A hold needs an earlier accepted weekly basis for
    the account, hence the first sample.)"""
    import _cctally_journal as jr
    t = S.Trace(monkeypatch, tmp_path)
    a = t.session("idle74")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(80, at=DAY + "08:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 74, at=DAY + "09:00:30Z")
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
    t.render(a, 74, at=DAY + "09:10:30Z")
    assert t.published() == [74.0]


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
