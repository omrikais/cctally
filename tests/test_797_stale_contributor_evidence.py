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


S4_EXPECTED = {'a_statusline': [0.0, 74.0, 0.0],
 'a_resets': [83.0, 74.0],
 'a_receipt_74': True,
 'b_api': [50.0, 0.0],
 'b_receipts': 0,
 'outcomes': {'in_place': 3, 'transitions': 0, 'five_hour': 0, 'milestones': 3}}


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
    ("capture-instant",): [],
    ("statusline-sample",): [],
    ("provider-floor-before-receipt",): [],
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
