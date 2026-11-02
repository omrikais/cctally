"""#797 T1: the end-to-end stale storm and genuine growth (spec section 6
T1, acceptance A1-A4).

Every scenario runs through the real render intake, the real detached
reducer, the real OAuth publication path and the real record kernel
(`tests/_stale_contributor_trace.py`), then compares live, rebuilt and
rederived outcome multisets and pins its literal outcomes, so a regression
to an unbounded flap — or equally empty stores — fails.

The preservation controls (S2 and S3 ``pure``, S9 and S10 ``prefix``)
assert nothing about receipts and pass on the parent commit; every other
scenario exercises the refusal and fails there (the RED proof). Every
preservation control — these and the five-hour S6 and S7 — is broken by
some mutation of the refusal rule, singly or in a pair
(`test_mutation_controls_break_preservation`, `MUTATION_BITES`).
"""
from __future__ import annotations

import sys

import pytest

import _stale_contributor_trace as S

at = S.at

DAY = "2026-10-22T"
#: The redeem of the 2026-10-22 shape (section 3.2).
REDEEM = DAY + "09:31:00Z"


def _common(t, result):
    """What every scenario asserts happened: render intake reached the
    detached reducer, no account is quarantined, every reading stayed bound
    to its account, and live = rebuilt = rederived."""
    assert t.sl._load_candidate_spool(now_epoch=S.epoch(t.log[-1][-1])) is not None
    assert not t.quarantined()
    stores = t.maintenance()
    S.assert_same_outcomes(stores["live"], stores["rebuilt"], stores["rederived"])
    result["outcomes"] = {k: len(v) for k, v in stores["live"].items()}
    return result


def _redeem_storm(t, a, b, *, minutes=26, fresh_value=1, after_fresh=None):
    """After the redeem: OAuth every minute reads 0 for six minutes and then
    alternates 1 and 0; the idle sessions replay every 30 s; ``b`` gets a
    fresh response at 5 min 25 s and from then on renders ``fresh_value``."""
    for i in range(0, minutes * 60, 30):
        if i % 60 == 0:
            t.oauth(0 if i < 6 * 60 else (1 if (i // 60) % 2 == 0 else 0),
                    at=at(REDEEM, i))
        if i == 5 * 60 + 30:
            b.respond(at(REDEEM, i - 5))
        t.render(a, 74, at=at(REDEEM, i + 10))
        t.render(b, fresh_value if i >= 5 * 60 + 30 else 82, at=at(REDEEM, i + 20))
        if after_fresh is not None:
            after_fresh(i)


# --- S1 ---------------------------------------------------------------------


def scenario_s1(t):
    a, b = t.session("idle74"), t.session("busy82")
    # The 74% session's last response was 10 h 41 min before the credit; a
    # subagent of it responded earlier still. The 82% session's last
    # response predates the 83% readings.
    a.respond("2026-10-21T22:40:00Z", subagent="a1")
    a.respond("2026-10-21T22:50:00Z")
    b.respond(DAY + "08:00:00Z")
    t.oauth(83, at=DAY + "08:59:00Z")
    for i in range(0, 30 * 60, 60):
        t.render(a, 74, at=at(DAY + "09:00:00Z", i + 10))
        t.render(b, 82, at=at(DAY + "09:00:00Z", i + 20))
        t.oauth(83, at=at(DAY + "09:00:00Z", i + 40))
    before = {"stored": t.stored(),
              "receipts": sorted((r.sigma.percent, r.path, r.provider_percent)
                                 for r in t.receipts())}
    _redeem_storm(t, a, b)
    return _common(t, {
        "before": before,
        "resets": [p for _old, _new, p in t.resets()],
        "statusline": t.published(),
        "api": t.published("api"),
        "milestones": [m[1] for m in t.milestones()],
        "stored": t.stored(),
        "samples": len(t.samples()),
        "receipt_74_valid": t.receipt_for(a, 74) is not None,
    })


S1_EXPECTED = {'before': {'stored': 83.0,
            'receipts': [(74.0, 'provider_support', 83.0),
                         (82.0, 'provider_support', 83.0)]},
 'resets': [83.0],
 'statusline': [1.0],
 'api': [83.0, 0.0],
 'milestones': [83, 1],
 'stored': 1.0,
 'samples': 57,
 'receipt_74_valid': True,
 'outcomes': {'in_place': 1, 'transitions': 0, 'five_hour': 0, 'milestones': 2}}


def test_s1_redeemed_reset_replay(monkeypatch, tmp_path):
    result = scenario_s1(S.Trace(monkeypatch, tmp_path))
    print(result)
    assert result == S1_EXPECTED, "RESULT=" + repr(result)


# --- S1b and S11 (R16, pinned) -------------------------------------------------


def scenario_s1b(t):
    a, b = t.session("idle74"), t.session("idle82")
    a.respond("2026-10-21T22:50:00Z")
    b.respond(DAY + "08:30:00Z")   # never presents before the redeem
    for i in range(0, 30 * 60, 60):
        t.oauth(74, at=at(DAY + "09:00:00Z", i))
        t.render(a, 74, at=at(DAY + "09:00:00Z", i + 30))
    before = sorted((r.sigma.percent, r.path) for r in t.receipts())
    for i in range(0, 20 * 60, 30):
        if i % 60 == 0:
            t.oauth(0 if (i // 60) % 2 == 0 else 1, at=at(REDEEM, i))
        t.render(a, 74, at=at(REDEEM, i + 10))
        t.render(b, 82, at=at(REDEEM, i + 20))
    return _common(t, {
        "before": before,
        "resets": [p for _o, _n, p in t.resets()],
        "statusline": t.published(),
        "milestones": [m[1] for m in t.milestones()],
        "receipt_82": (r := t.receipt_for(b, 82)) and r.path,
    })


S1B_EXPECTED = {'before': [(74.0, 'maximal')],
 'resets': [74.0, 82.0],
 'statusline': [82.0],
 'milestones': [74, 82],
 'receipt_82': 'maximal',
 'outcomes': {'in_place': 2, 'transitions': 0, 'five_hour': 0, 'milestones': 2}}


def test_s1b_unaccounted_stale_sample_mints_the_pinned_r16_outcomes(monkeypatch, tmp_path):
    result = scenario_s1b(S.Trace(monkeypatch, tmp_path))
    print(result)
    assert result == S1B_EXPECTED, "RESULT=" + repr(result)


def scenario_s11(t):
    x = t.session("late60")
    for i in range(0, 10 * 60, 60):
        t.oauth(74, at=at(DAY + "09:00:00Z", i))
    # x's last response follows every pre-credit OAuth request, and 60 is
    # not the accounted 74: it earns nothing before the credit.
    x.respond(DAY + "09:20:00Z")
    t.render(x, 60, at=DAY + "09:21:00Z")
    before = len(t.receipts())
    for i in range(0, 10 * 60, 30):
        if i % 60 == 0:
            t.oauth(0, at=at(REDEEM, i))
        t.render(x, 60, at=at(REDEEM, i + 15))
    return _common(t, {
        "before": before,
        "resets": [p for _o, _n, p in t.resets()],
        "statusline": t.published(),
        "receipt": (r := t.receipt_for(x, 60)) and r.path,
    })


S11_EXPECTED = {'before': 0,
 'resets': [74.0, 60.0],
 'statusline': [60.0],
 'receipt': 'maximal',
 'outcomes': {'in_place': 2, 'transitions': 0, 'five_hour': 0, 'milestones': 2}}


def test_s11_first_presentation_is_admitted_then_refused(monkeypatch, tmp_path):
    result = scenario_s11(S.Trace(monkeypatch, tmp_path))
    print(result)
    assert result == S11_EXPECTED, "RESULT=" + repr(result)


# --- S12 (R13, pinned) ---------------------------------------------------------


def scenario_s12(t, *, oauth_after):
    """The S1 shape with the redeem seen only through a fresh status-line
    session and no provider sample after it; from ``oauth_after`` seconds
    after the redeem OAuth refresh resumes and reads 0."""
    a, f = t.session("idle74"), t.session("fresh")
    a.respond("2026-10-21T22:50:00Z")
    for i in range(0, 10 * 60, 60):
        t.oauth(74, at=at(DAY + "09:00:00Z", i))
        t.render(a, 74, at=at(DAY + "09:00:00Z", i + 30))
    f.respond(at(REDEEM, 5))
    for i in range(0, 30 * 60, 30):
        if oauth_after is not None and i >= oauth_after and i % 60 == 0:
            t.oauth(0, at=at(REDEEM, i))
        t.render(f, 0, at=at(REDEEM, i + 10))
        t.render(a, 74, at=at(REDEEM, i + 20))
    return _common(t, {
        "resets": [p for _o, _n, p in t.resets()],
        "statusline": t.published(),
    })


S12_WITHOUT_EXPECTED = {'resets': [74.0, 74.0, 74.0, 74.0, 74.0, 74.0, 74.0, 74.0, 74.0],
 'statusline': [0.0,
                74.0,
                0.0,
                74.0,
                0.0,
                74.0,
                0.0,
                74.0,
                0.0,
                74.0,
                0.0,
                74.0,
                0.0,
                74.0,
                0.0,
                74.0,
                0.0,
                74.0],
 'outcomes': {'in_place': 9, 'transitions': 0, 'five_hour': 0, 'milestones': 10}}
S12_WITH_EXPECTED = {'resets': [74.0, 74.0, 74.0, 74.0],
 'statusline': [0.0, 74.0, 0.0, 74.0, 0.0, 74.0],
 'outcomes': {'in_place': 4, 'transitions': 0, 'five_hour': 0, 'milestones': 4}}


def test_s12_without_provider_samples_the_flap_continues(monkeypatch, tmp_path):
    result = scenario_s12(S.Trace(monkeypatch, tmp_path), oauth_after=None)
    print(result)
    assert result == S12_WITHOUT_EXPECTED, "RESULT=" + repr(result)


def test_s12_an_ordered_low_sample_ends_the_flap(monkeypatch, tmp_path):
    result = scenario_s12(S.Trace(monkeypatch, tmp_path), oauth_after=10 * 60)
    print(result)
    assert result == S12_WITH_EXPECTED, "RESULT=" + repr(result)


# --- S2, S3 (genuine growth) -------------------------------------------------------


def _credit(t, *, pre, at_):
    t.oauth(pre, at=at(at_, -600))
    t.oauth(0, at=at_)


def scenario_s2(t, *, stale=True):
    """``stale=False`` is the preservation control (passes on the parent);
    with ``stale`` an idle 50 replays beside the climb and is refused."""
    idle, c = t.session("idle50"), t.session("climber")
    idle.respond("2026-10-21T20:00:00Z")
    t.oauth(50, at=DAY + "09:00:00Z")
    t.render(idle if stale else c, 50, at=DAY + "09:00:30Z")
    t.oauth(0, at=DAY + "09:10:00Z")
    for k in range(1, 31):
        base = at(DAY + "09:11:00Z", 60 * k)
        c.respond(base, subagent="w" if k % 3 == 0 else None)
        if k == 15:
            # F-I4-1 (the V1 shape): an OAuth request started before this
            # response whose lower reading is captured after it. Not a proof
            # (its q precedes t_m); only the capture instant used in place of
            # q makes it one.
            t.oauth(k - 3, at=at(base, 2), lead=10)
        t.render(c, k, at=at(base, 10))
        if stale:
            t.render(idle, 50, at=at(base, 20))
        t.oauth(k, at=at(base, 40))
    return _common(t, {
        "resets": [p for _o, _n, p in t.resets()],
        "statusline": t.published(),
        "milestones": [m[1] for m in t.milestones()],
    })


S2_EXPECTED = {False: {'resets': [50.0],
         'statusline': [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0,
                        13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0,
                        23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0, 30.0],
         'milestones': [50, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
                        18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30],
         'outcomes': {'in_place': 1,
                      'transitions': 0,
                      'five_hour': 0,
                      'milestones': 31}},
 True: {'resets': [50.0],
        'statusline': [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0,
                       13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0, 23.0,
                       24.0, 25.0, 26.0, 27.0, 28.0, 29.0, 30.0],
        'milestones': [50, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
                       18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30],
        'outcomes': {'in_place': 1,
                     'transitions': 0,
                     'five_hour': 0,
                     'milestones': 31}}}


@pytest.mark.parametrize("stale", [False, True], ids=["pure", "stale"])
def test_s2_genuine_same_window_climb(monkeypatch, tmp_path, stale):
    result = scenario_s2(S.Trace(monkeypatch, tmp_path), stale=stale)
    print(result)
    assert result == S2_EXPECTED[stale], "RESULT=" + repr(result)


def scenario_s3(t, *, stale=True):
    """``stale=False`` is the preservation control (passes on the parent):
    no idle replay, and after the second redeem the climber responds fresh.
    With ``stale`` an idle 50 replays throughout and the climber replays its
    pre-redeem 40; both are refused."""
    idle, c = t.session("idle50"), t.session("climber")
    idle.respond("2026-10-21T20:00:00Z")
    t.oauth(50, at=DAY + "09:00:00Z")
    t.render(idle if stale else c, 50, at=DAY + "09:00:30Z")
    t.oauth(0, at=DAY + "09:10:00Z")
    for k in range(5, 45, 5):
        base = at(DAY + "09:11:00Z", 12 * k)
        c.respond(base)
        if k == 20:
            # F-I4-1: the V1 shape (see S2).
            t.oauth(k - 5, at=at(base, 2), lead=10)
        t.render(c, k, at=at(base, 10))
        if stale:
            t.render(idle, 50, at=at(base, 20))
        t.oauth(k, at=at(base, 40))
    t.oauth(0, at=DAY + "10:30:00Z")
    if not stale:
        c.respond(DAY + "10:30:05Z")
    for i in range(0, 5 * 60, 30):
        if stale:
            t.render(idle, 50, at=at(DAY + "10:30:00Z", i + 10))
        t.render(c, 40 if stale else 1, at=at(DAY + "10:30:00Z", i + 20))
    return _common(t, {
        "resets": t.resets(),
        "statusline": t.published(),
        "milestones": [m[1] for m in t.milestones()],
    })


S3_EXPECTED = {False: {'resets': [('2026-10-22T09:10:00+00:00', '2026-10-24T05:00:00+00:00', 50.0),
                    ('2026-10-22T10:30:00+00:00', '2026-10-24T05:00:00+00:00', 40.0)],
         'statusline': [5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 1.0],
         'milestones': [50, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                        21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
                        37, 38, 39, 40, 1],
         'outcomes': {'in_place': 2,
                      'transitions': 0,
                      'five_hour': 0,
                      'milestones': 38}},
 True: {'resets': [('2026-10-22T09:10:00+00:00', '2026-10-24T05:00:00+00:00', 50.0),
                   ('2026-10-22T10:30:00+00:00', '2026-10-24T05:00:00+00:00', 40.0)],
        'statusline': [5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0],
        'milestones': [50, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                       21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
                       37, 38, 39, 40],
        'outcomes': {'in_place': 2,
                     'transitions': 0,
                     'five_hour': 0,
                     'milestones': 37}}}


@pytest.mark.parametrize("stale", [False, True], ids=["pure", "stale"])
def test_s3_genuine_second_credit(monkeypatch, tmp_path, stale):
    result = scenario_s3(S.Trace(monkeypatch, tmp_path), stale=stale)
    print(result)
    assert result == S3_EXPECTED[stale], "RESULT=" + repr(result)


# --- S4 (accounts) ---------------------------------------------------------------------


def scenario_s4(t, *, b_first):
    def account_b():
        t.as_account("B")
        for i in range(0, 6 * 60, 60):
            t.oauth(50 if i == 0 else 0, at=at(DAY + "09:40:00Z", i))

    def account_a():
        t.as_account("A")
        a, f = t.session("idle74"), t.session("freshA")
        a.respond("2026-10-21T22:50:00Z")
        t.oauth(83, at=DAY + "09:00:00Z")
        t.render(a, 74, at=DAY + "09:00:30Z")
        f.respond(DAY + "09:30:00Z")
        for i in range(0, 8 * 60, 30):
            t.render(f, 0, at=at(DAY + "09:30:10Z", i))
            if i == 5 * 60:
                t.render(a, 74, at=at(DAY + "09:30:20Z", i))
        return a

    if b_first:
        account_b()
        a = account_a()
    else:
        a = account_a()
        account_b()
    t.as_account("A")
    return _common(t, {
        "a_statusline": t.published(),
        "a_resets": [p for _o, _n, p in t.resets()],
        "a_receipt_74": t.receipt_for(a, 74) is not None,
        "b_api": t.published("api", account=t.as_account("B")),
        "b_receipts": len(t.receipts()),
    })


#: #1019 (spec L6 case c, P-R2): the idle 74 is drop-refused (the OAuth 83
#: was requested after its activity), so it is rise-ineligible after the
#: genuine 83 -> 0 credit: S4 loses exactly its stale-74 rise and what that
#: rise caused — the 74 publication, the 74 -> 0 credit and the milestone
#: the 74 seeded (#797 pinned [0, 74, 0], [83, 74], three credits and three
#: milestones).
S4_EXPECTED = {'a_statusline': [0.0],
 'a_resets': [83.0],
 'a_receipt_74': True,
 'b_api': [50.0, 0.0],
 'b_receipts': 0,
 'outcomes': {'in_place': 2, 'transitions': 0, 'five_hour': 0, 'milestones': 2}}


@pytest.mark.parametrize("b_first", [False, True])
def test_s4_accounts_never_cross(monkeypatch, tmp_path, b_first):
    result = scenario_s4(S.Trace(monkeypatch, tmp_path), b_first=b_first)
    print(result)
    assert result == S4_EXPECTED, "RESULT=" + repr(result)


# --- S8 (R9, pinned) -------------------------------------------------------------------


def scenario_s8(t):
    x = t.session("crossed")
    x.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.oauth(0, at=REDEEM)
    # Two inputs composed before x's fresh post-credit response, both scanned
    # after it: each carries the post-credit t_m.
    x.respond(at(REDEEM, 120))
    t.render(x, 74, at=at(REDEEM, 130))
    t.render(x, 82, at=at(REDEEM, 160))
    for i in range(0, 6 * 60, 60):
        t.oauth(0, at=at(REDEEM, 200 + i))
        t.render(x, 74, at=at(REDEEM, 210 + i))
        t.render(x, 82, at=at(REDEEM, 240 + i))
    t.render(x, 1, at=at(REDEEM, 700))
    return _common(t, {
        "resets": [p for _o, _n, p in t.resets()],
        "statusline": t.published(),
        "milestones": [m[1] for m in t.milestones()],
    })


S8_EXPECTED = {'resets': [83.0, 82.0],
 'statusline': [74.0, 82.0, 1.0],
 'milestones': [83, 74, 75, 76, 77, 78, 79, 80, 81, 82, 1],
 'outcomes': {'in_place': 2, 'transitions': 0, 'five_hour': 0, 'milestones': 11}}


def test_s8_crossed_input_is_bounded(monkeypatch, tmp_path):
    result = scenario_s8(S.Trace(monkeypatch, tmp_path))
    print(result)
    assert result == S8_EXPECTED, "RESULT=" + repr(result)


# --- S9 (manual credit) ------------------------------------------------------------------


def scenario_s9(t, *, prefix=False):
    """``prefix`` ends at the ordered low sample that follows the stale 60's
    publication and mints its credit: the preservation control (the R16 admission and the credit
    it costs happen on the parent too). The full trace adds the refusal
    after the 60 re-earns its receipt."""
    s, g = t.session("idle60"), t.session("after")
    s.respond("2026-10-21T22:50:00Z")
    for i in range(0, 5 * 60, 60):
        t.oauth(60, at=at(DAY + "09:00:00Z", i))
        t.render(s, 60, at=at(DAY + "09:00:00Z", i + 30))
    before = sorted((r.sigma.percent, r.path) for r in t.receipts())
    t.manual_credit(10, at=DAY + "09:10:00Z", effective=DAY + "09:10:00Z")
    g.respond(DAY + "09:11:00Z")
    t.render(g, 12, at=DAY + "09:11:10Z")
    # An ordered sample already dominates the stale 60, but the manual credit
    # invalidated its receipt: it is admitted (R16) — the domination-alone
    # mutation refuses it here.
    t.oauth(12, at=DAY + "09:11:15Z")
    t.render(s, 60, at=DAY + "09:11:20Z")
    for i in range(0, 5 * 60, 60):
        t.oauth(12, at=at(DAY + "09:12:00Z", i))
        if prefix and i == 60:
            break
        t.render(s, 60, at=at(DAY + "09:12:00Z", i + 20))
        t.render(g, 12, at=at(DAY + "09:12:00Z", i + 30))
    result = {} if prefix else {"before": before}   # receipts: new behaviour
    result.update({
        "floors": [p for _e, p in t.floors()],
        "resets": [p for _o, _n, p in t.resets()],
        "statusline": t.published(),
    })
    return _common(t, result)


S9_EXPECTED = {True: {'floors': [60.0],
        'resets': [60.0],
        'statusline': [12.0, 60.0],
        'outcomes': {'in_place': 1, 'transitions': 0, 'five_hour': 0, 'milestones': 1}},
 False: {'before': [(60.0, 'maximal')],
         'floors': [60.0],
         'resets': [60.0],
         'statusline': [12.0, 60.0],
         'outcomes': {'in_place': 1,
                      'transitions': 0,
                      'five_hour': 0,
                      'milestones': 1}}}


@pytest.mark.parametrize("prefix", [True, False], ids=["prefix", "full"])
def test_s9_manual_credit_invalidates_receipts(monkeypatch, tmp_path, prefix):
    result = scenario_s9(S.Trace(monkeypatch, tmp_path), prefix=prefix)
    print(result)
    assert result == S9_EXPECTED[prefix], "RESULT=" + repr(result)


# --- S10 (delayed causal observation) --------------------------------------------------------


def scenario_s10(t, *, variant, prefix=False):
    """``prefix`` ends at the sample that establishes the second credit: the
    preservation control (B's 30 admitted, both credits minted, as on the
    parent). The full trace adds the refusal of B's later replay."""
    a, b = t.session("A"), t.session("B")
    a.respond(DAY + "08:00:00Z")
    b.respond(DAY + "08:00:00Z")
    for i in range(0, 5 * 60, 60):
        t.oauth(74, at=at(DAY + "08:50:00Z", i))
    t.render(a, 74, at=DAY + "08:56:00Z")
    t.render(b, 74, at=DAY + "08:56:10Z")
    credit = DAY + "09:00:00Z"
    if variant == "V2":
        # A genuine OAuth 80 requested before A's delayed 0 is captured.
        t.oauth(80, at=at(credit, -30), lead=5)
    a.respond(at(credit, 60))       # A receives 0
    b.respond(at(credit, 120))      # B then genuine growth to 30
    if variant == "V1":
        # An OAuth request started before the credit whose response records
        # 74 after B's response.
        t.oauth(74, at=at(credit, 130), lead=140)
    t.render(a, 0, at=at(credit, 140))   # A's delayed input establishes it
    for i in range(0, 6 * 60, 30):
        t.render(a, 0, at=at(credit, 150 + i))
    # F-I4-1 (all variants): an OAuth 0 requested before B's response (its
    # request was in flight while A rendered) and captured after it — not a
    # proof against B's 30 (its q precedes B's t_m); only the capture
    # instant used in place of q makes it one.
    t.oauth(0, at=at(credit, 510), lead=400)
    t.render(b, 30, at=at(credit, 520))
    for i in range(0, 3 * 60, 60):
        t.oauth(30, at=at(credit, 540 + i))
        t.render(b, 30, at=at(credit, 560 + i))
        # A keeps presenting its older 0 beside B's accounted 30.
        t.render(a, 0, at=at(credit, 570 + i))
    second = DAY + "09:20:00Z"
    a.respond(at(second, 30))
    for i in range(0, 6 * 60, 30):
        if i % 60 == 0:
            t.oauth(0, at=at(second, 40 + i))
            if prefix:
                break
        t.render(a, 0, at=at(second, 50 + i))
        t.render(b, 30, at=at(second, 60 + i))
    return _common(t, {
        "resets": [p for _o, _n, p in t.resets()],
        "statusline": t.published(),
    })


S10_EXPECTED = {('base', True): {'resets': [74.0, 30.0], 'statusline': [0.0, 30.0], 'outcomes': {'in_place': 2, 'transitions': 0, 'five_hour': 0, 'milestones': 2}},
 ('base', False): {'resets': [74.0, 30.0],
                   'statusline': [0.0, 30.0],
                   'outcomes': {'in_place': 2,
                                'transitions': 0,
                                'five_hour': 0,
                                'milestones': 2}},
 ('V1', True): {'resets': [74.0, 30.0], 'statusline': [0.0, 30.0], 'outcomes': {'in_place': 2, 'transitions': 0, 'five_hour': 0, 'milestones': 2}},
 ('V1', False): {'resets': [74.0, 30.0],
                 'statusline': [0.0, 30.0],
                 'outcomes': {'in_place': 2,
                              'transitions': 0,
                              'five_hour': 0,
                              'milestones': 2}},
 ('V2', True): {'resets': [80.0, 30.0], 'statusline': [0.0, 30.0], 'outcomes': {'in_place': 2, 'transitions': 0, 'five_hour': 0, 'milestones': 8}},
 ('V2', False): {'resets': [80.0, 30.0],
                 'statusline': [0.0, 30.0],
                 'outcomes': {'in_place': 2,
                              'transitions': 0,
                              'five_hour': 0,
                              'milestones': 8}}}


@pytest.mark.parametrize("prefix", [True, False], ids=["prefix", "full"])
@pytest.mark.parametrize("variant", ["base", "V1", "V2"])
def test_s10_delayed_causal_observation(monkeypatch, tmp_path, variant, prefix):
    result = scenario_s10(S.Trace(monkeypatch, tmp_path), variant=variant,
                          prefix=prefix)
    print(result)
    assert result == S10_EXPECTED[(variant, prefix)], "RESULT=" + repr(result)


# --- #1019: a proven-stale status-line low never publishes a drop ----------------------

#: E1's weekly window: resets 2026-10-31T05:00:00Z (epoch 1793422800).
E1_WEEK = 1793422800
E1 = "2026-10-29T"
#: E1's five-hour window, always 0% (any future reset inside five hours).
E1_FIVE_END = E1 + "23:00:00Z"
E1_ACCOUNT = "c7198878-0000-4000-8000-000000000001"


def _e1_oauth(t, *, at_, q):
    """One E1 provider sample: weekly 51, five-hour 0, request ``q``."""
    t.oauth(51, at=at_, requested_at=q, five=0, five_end=E1_FIVE_END)


def _e1_render(t, session, value, *, at_):
    t.render(session, value, at=at_, five=0, five_end=E1_FIVE_END)


def _e1_baseline(t):
    """E1 before the incident: the idle session's last response
    (2026-10-25T10:12:22.165Z), the live session's (2026-10-28T18:04:24.165Z),
    and q-bearing OAuth 51 samples every 45 s from 19:58:03Z through
    q 20:16:52.357576Z (captured 20:16:57Z)."""
    idle, live = t.session("idle18"), t.session("live51")
    idle.respond("2026-10-25T10:12:22.165Z")
    live.respond("2026-10-28T18:04:24.165Z")
    first = S.epoch(E1 + "19:58:03Z")
    last = S.epoch(E1 + "20:16:57Z")
    for captured in range(first, last - 30, 45):
        _e1_oauth(t, at_=S.iso(captured),
                  q=S.iso(captured - 5).replace("Z", ".357576Z"))
    _e1_oauth(t, at_=E1 + "20:16:57Z", q=E1 + "20:16:52.357576Z")
    return idle, live


def scenario_1019_l1(t):
    """L1 (#1019 spec section 6): E1's publication reconstruction. The idle
    18% arms at 20:17:16Z and its own later arrival at 20:20:22Z satisfies
    the only contributor (unanimous publication 1); the live 51% rises at
    20:20:28Z and re-arms against 51 at 20:21:00Z beside the still-active idle
    arrival; the idle 18% at 20:23:28Z is again the only contributor
    (unanimous publication 2); the live 51% rises at 20:23:29Z; OAuth reads
    51 at q 20:24:22.378727Z."""
    t.track_routes()
    idle, live = _e1_baseline(t)
    covered = [s for s in t.samples() if s[1] == E1 + "20:16:52.357576Z"]
    receipts_before = list(t.receipts())
    _e1_render(t, idle, 18, at_=E1 + "20:17:16Z")
    _e1_render(t, idle, 18, at_=E1 + "20:20:22Z")
    _e1_render(t, live, 51, at_=E1 + "20:20:28Z")
    _e1_render(t, live, 51, at_=E1 + "20:21:00Z")
    _e1_render(t, idle, 18, at_=E1 + "20:23:28Z")
    _e1_render(t, live, 51, at_=E1 + "20:23:29Z")
    _e1_oauth(t, at_=E1 + "20:24:24Z", q=E1 + "20:24:22.378727Z")
    return _common(t, {
        "covered_sample": covered,
        "receipts_before": receipts_before,
        "routes": [(a, axis, p, r) for a, axis, p, r in t.routes if axis == "sevenDay"],
        "resets": [p for _o, _n, p in t.resets()],
        "below_baseline": [p for p in t.published() if p < 51.0],
        "milestones": [(threshold, segment != 0, cost)
                       for threshold, segment, cost in t.milestone_rows()],
    })


def test_1019_l1_replay_unanimous_route(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path, account=E1_ACCOUNT, week_resets_at=E1_WEEK)
    result = scenario_1019_l1(t)
    print(result)
    # A covered provider sample exists, no receipt was held when the stale
    # 18 first arrived, and the quarantine is open (`_common`).
    assert result["covered_sample"] == [(51.0, E1 + "20:16:52.357576Z")]
    assert result["receipts_before"] == []
    # GREEN: no status-line publication below 51, no reset, credit or
    # milestone beyond the 51% baseline (live = rebuilt = rederived).
    assert result["routes"] == []
    assert result["below_baseline"] == []
    assert result["resets"] == []
    assert [(m, post) for m, post, _cost in result["milestones"]] == [(51, False)]


def scenario_1019_l2(t):
    """L2: the stale 18% beside the live 51% that keeps refreshing every
    30 s, so unanimity never holds, through the 180 s deadline (gate open)."""
    t.track_routes()
    idle, live = _e1_baseline(t)
    for i in range(0, 5 * 60, 30):
        _e1_render(t, idle, 18, at_=S.at(E1 + "20:17:16Z", i))
        _e1_render(t, live, 51, at_=S.at(E1 + "20:17:20Z", i))
    return _common(t, {
        "routes": [(a, axis, p, r) for a, axis, p, r in t.routes if axis == "sevenDay"],
        "resets": [p for _o, _n, p in t.resets()],
        "below_baseline": [p for p in t.published() if p < 51.0],
        "pending": t.transition().control.pending_drops.get("sevenDay"),
    })


def test_1019_l2_replay_deadline_route(monkeypatch, tmp_path):
    """L2 (accuracy: a false in-place reset by the deadline route): a
    present higher contributor keeps the stale low from unanimity, and the
    deadline must not publish it. RED on the parent: a deadline-route 18,
    then a reset with pre-credit 51."""
    t = S.Trace(monkeypatch, tmp_path, account=E1_ACCOUNT, week_resets_at=E1_WEEK)
    result = scenario_1019_l2(t)
    print(result)
    assert result["routes"] == []
    assert result["below_baseline"] == []
    assert result["resets"] == []
    assert result["pending"] is None, "the refused low armed a pending drop"


def _two_accounts(t, own, other, *, b_first):
    """Run ``own`` on account A and ``other`` on account B in the given
    insertion order, ending on A."""
    steps = [("B", other), ("A", own)] if b_first else [("A", own), ("B", other)]
    for name, step in steps:
        t.as_account(name)
        step()
    t.as_account("A")


def scenario_1019_l3a(t, *, b_first):
    """L3a: OAuth reads 60, then a genuine 10 (the api path fires the
    credit itself); a fresh session then renders the 10. Account B reads 70
    throughout, its sessions untouched by A's credit."""
    def own():
        s = t.session("ownA")
        t.oauth(60, at=DAY + "09:00:00Z")
        s.respond(DAY + "09:00:10Z")
        t.render(s, 60, at=DAY + "09:00:20Z")
        t.oauth(10, at=DAY + "09:10:00Z")
        s.respond(DAY + "09:10:10Z")
        t.render(s, 10, at=DAY + "09:10:20Z")
        t.render(s, 10, at=DAY + "09:10:50Z")

    def other():
        b = t.session("ownB")
        b.respond(DAY + "09:05:00Z")
        for i in range(0, 12 * 60, 120):
            t.oauth(70, at=S.at(DAY + "09:05:10Z", i))
            t.render(b, 70, at=S.at(DAY + "09:05:20Z", i))

    _two_accounts(t, own, other, b_first=b_first)
    b_key = T_account(t, "B")
    return _common(t, {
        "a_resets": [p for _o, _n, p in t.resets()],
        "a_api": t.published("api"),
        "b_resets": [p for _o, _n, p in t.resets(account=b_key)],
        "b_statusline": t.published(account=b_key),
    })


def T_account(t, name):
    """The account key of ``name`` without changing the selected account."""
    current = t.account_name
    key = t.as_account(name)
    t.as_account(current)
    return key


@pytest.mark.parametrize("b_first", [False, True], ids=["a-first", "b-first"])
def test_1019_l3a_provider_caused_credit_two_accounts_both_orders(
        monkeypatch, tmp_path, b_first):
    """L3a (preservation control, accuracy: a lost real credit): a genuine
    >=25 pp credit first observed by OAuth fires through the api path on
    both tips; the other account is unaffected in either insertion order."""
    result = scenario_1019_l3a(S.Trace(monkeypatch, tmp_path), b_first=b_first)
    print(result)
    assert result["a_resets"] == [60.0]
    assert result["a_api"] == [60.0, 10.0]
    assert result["b_resets"] == []
    assert set(result["b_statusline"]) <= {70.0}


def scenario_1019_l3b(t, *, b_first=False):
    """L3b: OAuth reads the pre-credit 60 until the credit, then takes no
    sample; the session responds after the credit (its t_m follows every
    provider sample) and renders the genuine 10 twice. Account B's later,
    higher OAuth 70 samples follow A's response: another account's sample
    never refuses A's low."""
    def own():
        s = t.session("ownA")
        for i in range(0, 5 * 60, 60):
            t.oauth(60, at=S.at(DAY + "09:00:00Z", i))
        s.respond(DAY + "09:10:00Z")
        t.render(s, 10, at=DAY + "09:10:10Z")
        t.render(s, 10, at=DAY + "09:10:40Z")

    def other():
        b = t.session("ownB")
        b.respond(DAY + "09:05:00Z")
        for i in range(0, 12 * 60, 120):
            t.oauth(70, at=S.at(DAY + "09:05:10Z", i + 300))
            t.render(b, 70, at=S.at(DAY + "09:05:20Z", i + 300))

    _two_accounts(t, own, other, b_first=b_first)
    b_key = T_account(t, "B")
    return _common(t, {
        "a_resets": [p for _o, _n, p in t.resets()],
        "a_statusline": t.published(),
        "a_api": t.published("api"),
        "b_resets": [p for _o, _n, p in t.resets(account=b_key)],
    })


L3B_EXPECTED = {"a_resets": [60.0], "a_statusline": [10.0], "a_api": [60.0],
                "b_resets": [],
                "outcomes": {"in_place": 1, "transitions": 0, "five_hour": 0,
                             "milestones": 2}}


@pytest.mark.parametrize("b_first", [False, True], ids=["a-first", "b-first"])
def test_1019_l3b_statusline_caused_credit_two_accounts_both_orders(
        monkeypatch, tmp_path, b_first):
    """L3b (accuracy: a lost real credit): the status-line publication of
    the genuine 10 causes the 60 -> 10 reset (no provider sample ever read
    10), in either insertion order, with account B unaffected. Passes on
    the parent; `drop-bound-ignored`, and in the b-first order
    `samples-cross-account` (A3: B's samples read as A's refuse the genuine
    10), remove the causal reset (`MUTATION_BITES`)."""
    result = scenario_1019_l3b(S.Trace(monkeypatch, tmp_path), b_first=b_first)
    print(result)
    assert result == L3B_EXPECTED, "RESULT=" + repr(result)


def scenario_1019_persistent(t):
    """Codex's persistent schedule (spec L6 case b): baseline 83 from an
    admitted OAuth 83 requested after idle X's fixed supported activity; X
    reports 74 and fresh Y a genuine 0, both every 30 s for twelve minutes
    (past several 180 s deadlines)."""
    x, y = t.session("idleX"), t.session("freshY")
    x.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    y.respond(DAY + "09:30:00Z")
    for i in range(0, 12 * 60, 30):
        t.render(x, 74, at=S.at(DAY + "09:30:10Z", i))
        t.render(y, 0, at=S.at(DAY + "09:30:20Z", i))
    return _common(t, {
        "resets": [p for _o, _n, p in t.resets()],
        "statusline": t.published(),
    })


def test_drop_refused_member_neither_rises_nor_dissents(monkeypatch, tmp_path):
    """Dissent and drop-refused rises (spec L6 case b; accuracy: false
    credits from a drop/rise flap): exactly one 83 -> 0 credit, no
    publication of X's 74 and no further credit. Reducing with drop-refused
    members allowed to rise (`drop-refused-may-rise`) re-raises the baseline
    to the stale 74 and mints a 74 -> 0 credit at each deadline."""
    result = scenario_1019_persistent(S.Trace(monkeypatch, tmp_path / "branch"))
    print(result)
    assert result["resets"] == [83.0]
    assert result["statusline"] == [0.0]
    with pytest.MonkeyPatch.context() as mp:
        t = S.Trace(mp, tmp_path / "mutated")
        S.mutate(mp, "drop-refused-may-rise")
        mutated = scenario_1019_persistent(t)
    print("mutated", mutated)
    assert mutated["resets"][0] == 83.0 and mutated["resets"][1:]
    assert set(mutated["resets"][1:]) == {74.0}
    assert 74.0 in mutated["statusline"]


def scenario_1019_witnessless_receipt(t):
    """After a credit to 0, a member without supported activity and fresh Y
    both report the accounted 0 when an OAuth 0 completes."""
    _credited_to_zero(t)
    y, z = t.session("freshY"), t.session("noactivity")
    t.render(z, 0, at=DAY + "09:31:00Z")
    t.render(y, 0, at=DAY + "09:31:05Z")
    t.oauth(0, at=DAY + "09:31:30Z")
    return {"supported": t.receipt_for(y, 0) is not None,
            "witnessless": t.receipt_for(z, 0) is not None}


def test_received_at_bound_never_becomes_a_receipt(monkeypatch, tmp_path):
    """Spec 4.1 (accuracy: a received_at-bounded reading refused for its
    rise as if its provider activity were known): the received_at fallback
    is a drop bound only, so a member without supported activity earns no
    receipt beside a supported peer that does. Treating the fallback as a
    known t_m (`drop-received-at-as-sigma`) earns it one."""
    result = scenario_1019_witnessless_receipt(S.Trace(monkeypatch, tmp_path / "branch"))
    assert result == {"supported": True, "witnessless": False}, result
    with pytest.MonkeyPatch.context() as mp:
        t = S.Trace(mp, tmp_path / "mutated")
        S.mutate(mp, "drop-received-at-as-sigma")
        mutated = scenario_1019_witnessless_receipt(t)
    assert mutated == {"supported": True, "witnessless": True}, mutated


# --- L9: coverage --------------------------------------------------------------------


def _uncovered(monkeypatch, how):
    """Make the provider sample projection unavailable: ``uncovered`` (the
    projection does not cover the account) or ``failed-read`` (the sample
    read raises)."""
    import sqlite3
    samples = sys.modules["_cctally_oauth_samples"]
    if how == "uncovered":
        monkeypatch.setattr(samples, "projection_covered",
                            lambda conn, *, account_key: False)
    else:
        def fail(*_a, **_k):
            raise sqlite3.OperationalError("simulated sample read failure")
        monkeypatch.setattr(samples, "read_samples", fail)


@pytest.mark.parametrize("how", ["uncovered", "failed-read"])
def test_1019_l9_uncovered_projection_defers_drop(monkeypatch, tmp_path, how):
    """L9 (accuracy: an unproven stale low published as "no proof"): with
    drop-proof work and no receipt, an uncovered projection or a failed
    sample read defers and retains the work — nothing below the baseline
    publishes — and once the samples are readable again the low is refused.
    The failed read uses a member without supported activity (its bound is
    its received_at), the case the parent never reads samples for. RED on
    the parent: the 18 publishes and resets the week."""
    t = S.Trace(monkeypatch, tmp_path, account=E1_ACCOUNT, week_resets_at=E1_WEEK)
    idle, _live = _e1_baseline(t)
    if how == "failed-read":
        idle = t.session("noactivity")
    assert t.receipts() == []
    with pytest.MonkeyPatch.context() as mp:
        _uncovered(mp, how)
        _e1_render(t, idle, 18, at_=E1 + "20:17:16Z")
        _e1_render(t, idle, 18, at_=E1 + "20:17:46Z")
        assert [p for p in t.published() if p < 51.0] == []
        assert t.resets() == []
    if how == "failed-read":
        # A later provider sample, requested after the retained arrivals.
        _e1_oauth(t, at_=E1 + "20:18:20Z", q=E1 + "20:18:15.000000Z")
    _e1_render(t, idle, 18, at_=E1 + "20:18:16Z")
    result = _common(t, {"below": [p for p in t.published() if p < 51.0],
                         "resets": [p for _o, _n, p in t.resets()]})
    assert result["below"] == [] and result["resets"] == [], result


def test_1019_l9_covered_account_without_samples_publishes_drop(monkeypatch, tmp_path):
    """L9 anti-vacuity control (accuracy: perpetual deferral, K4): an
    account whose covered projection holds no provider sample publishes a
    status-line drop exactly as today."""
    t = S.Trace(monkeypatch, tmp_path)
    live, low = t.session("live"), t.session("low")
    live.respond(DAY + "09:00:00Z")
    t.render(live, 51, at=DAY + "09:00:10Z")
    assert t.samples() == []
    low.respond(DAY + "09:05:00Z")
    t.render(low, 18, at=DAY + "09:05:10Z")
    t.render(low, 18, at=DAY + "09:05:40Z")
    result = _common(t, {"statusline": t.published(),
                         "resets": [p for _o, _n, p in t.resets()]})
    assert result["statusline"] == [51.0, 18.0]
    assert result["resets"] == [51.0]


def _credited_to_zero(t):
    """Baseline 0 after a status-line credit: an admitted OAuth 83 requested
    after idle X's fixed supported activity, then fresh Y's genuine 0 twice.
    X never rendered before the credit, so it holds no receipt."""
    x, y = t.session("idleX"), t.session("freshY")
    x.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    y.respond(DAY + "09:30:00Z")
    t.render(y, 0, at=DAY + "09:30:10Z")
    t.render(y, 0, at=DAY + "09:30:40Z")
    assert t.resets() and t.stored() == 0.0
    assert t.receipt_for(x, 74) is None
    return x


@pytest.mark.parametrize("how", ["uncovered", "covered"])
def test_1019_l9_uncovered_projection_defers_drop_refused_rise(monkeypatch, tmp_path, how):
    """L9 rise side (P-R3; accuracy: a stale pre-credit reading re-raising a
    credited account): receipt-free rise-proof work over an uncovered
    projection defers instead of publishing the drop-refused 74, and over a
    covered one the 74 is rise-ineligible. RED on the parent (uncovered):
    the 74 publishes."""
    t = S.Trace(monkeypatch, tmp_path)
    x = _credited_to_zero(t)
    with pytest.MonkeyPatch.context() as mp:
        if how == "uncovered":
            _uncovered(mp, "uncovered")
        t.render(x, 74, at=DAY + "09:32:00Z")
        t.render(x, 74, at=DAY + "09:32:30Z")
        assert 74.0 not in t.published()
    t.render(x, 74, at=DAY + "09:33:00Z")
    result = _common(t, {"statusline": t.published(),
                         "resets": [p for _o, _n, p in t.resets()]})
    assert result["statusline"] == [0.0] and result["resets"] == [83.0], result


def test_1019_l9_covered_account_without_samples_publishes_rise(monkeypatch, tmp_path):
    """L9 rise-side control (K4): with no provider sample on a covered
    projection a member's rise publishes exactly as today."""
    t = S.Trace(monkeypatch, tmp_path)
    low, x = t.session("low"), t.session("riser")
    low.respond(DAY + "09:00:00Z")
    t.render(low, 10, at=DAY + "09:00:10Z")
    x.respond("2026-10-21T22:50:00Z")
    t.render(x, 74, at=DAY + "09:01:00Z")
    result = _common(t, {"statusline": t.published()})
    assert t.samples() == []
    assert result["statusline"] == [10.0, 74.0]


# --- mutation controls over the preservation scenarios -------------------------------

import test_823_five_hour_stale_evidence_797 as F   # noqa: E402  (S6, S7)

#: The preservation scenarios: each passes on the parent commit, and some
#: mutation of the refusal rule (or pair of them) must break each one.
PRESERVATION = {
    "S2": (scenario_s2, {"stale": False}, lambda: S2_EXPECTED[False]),
    "S3": (scenario_s3, {"stale": False}, lambda: S3_EXPECTED[False]),
    "S9": (scenario_s9, {"prefix": True}, lambda: S9_EXPECTED[True]),
    **{f"S10-{v}": (scenario_s10, {"variant": v, "prefix": True},
                    lambda v=v: S10_EXPECTED[(v, True)])
       for v in ("base", "V1", "V2")},
    "S6": (F.scenario_s6, {}, lambda: F.S6_EXPECTED),
    "S7": (F.scenario_s7, {}, lambda: F.S7_EXPECTED),
    # #1019: the status-line-caused genuine credit, in the order where
    # account B's later, higher samples already exist when A's low renders
    # (the A3 cross-account control).
    "L3b": (scenario_1019_l3b, {"b_first": True}, lambda: L3B_EXPECTED),
}

#: Every single mutation, and the pair that weakens both conjuncts of a
#: refusal at once. Refusal needs a staleness proof AND a valid receipt, so
#: where a scenario's only near-proof is the V1 shape (an OAuth request
#: started before a genuine response whose lower reading is captured after
#: it) no single mutation can refuse a genuine member at its first
#: presentation: the capture instant supplies the proof and domination alone
#: drops the receipt.
COMBINATIONS = (*((m,) for m in S.MUTATIONS),
                ("domination-alone", "capture-instant"))

#: Which preservation scenarios each combination breaks (pinned).
MUTATION_BITES = {
    ("domination-alone",): ["S9"],
    ("numerical-domination",): [],
    # #1019: the drop proof keys on request time too, so the capture
    # instant in place of q now also refuses S10-V1's genuine 0 (its OAuth
    # 74 was requested before the credit and captured after A's response).
    ("capture-instant",): ["S10-V1"],
    ("statusline-sample",): [],
    ("provider-floor-before-receipt",): [],
    ("drop-bound-ignored",): ["S2", "S3", "S9", "S10-base", "S10-V1", "S10-V2", "L3b"],
    ("drop-no-tolerance",): [],
    ("drop-received-at-as-sigma",): [],
    ("drop-refused-may-rise",): [],
    ("samples-cross-account",): ["L3b"],
    ("domination-alone", "capture-instant"):
        ["S2", "S3", "S9", "S10-base", "S10-V1", "S10-V2", "S6", "S7"],
}


def test_every_preservation_scenario_has_a_biting_mutation():
    bitten = {name for names in MUTATION_BITES.values() for name in names}
    assert bitten == set(PRESERVATION)
    assert set(MUTATION_BITES) == set(COMBINATIONS)


@pytest.mark.parametrize("mutations", COMBINATIONS, ids="+".join)
def test_mutation_controls_break_preservation(monkeypatch, tmp_path, mutations):
    broken = []
    for name, (scenario, kwargs, expected) in PRESERVATION.items():
        with pytest.MonkeyPatch.context() as mp:
            t = S.Trace(mp, tmp_path / name)
            S.mutate(mp, *mutations)
            try:
                changed = scenario(t, **kwargs) != expected()
            except AssertionError:
                changed = True   # e.g. live and rebuilt outcomes diverged
            if changed:
                broken.append(name)
    assert broken == MUTATION_BITES[mutations], (
        "BROKEN=" + repr((mutations, broken)))
