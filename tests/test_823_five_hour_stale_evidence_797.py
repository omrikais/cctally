"""#823 T1 five-hour scenarios (spec section 6 T1 S5, S6, S7; acceptance A2,
A3, A5).

Every scenario runs through the real render intake, the real detached
reducer, the real OAuth publication path and the real record kernel
(`tests/_stale_contributor_trace.py`), with distinct observation identities
and distinct arming slots, then compares live, rebuilt and rederived outcome
multisets (`collections.Counter`) and pins its literal outcomes.

S5 is the complete causal tail of the #823 characterization
(`tests/test_in_place_5h_credit_detection.py`) driven through the status-line
path: it fails on the parent, which publishes the idle session's replayed 20
as a rise and mints a second credit from the later lows. S6 and S7 are
preservation controls (they pass on the parent); their mutation controls are
pinned in `tests/test_797_stale_contributor_evidence.py`.
"""
from __future__ import annotations

import pytest

import _stale_contributor_trace as S

at = S.at

DAY = "2026-10-22T"
WEEKLY = 40


def _common(t, result):
    assert t.sl._load_candidate_spool(now_epoch=S.epoch(t.log[-1][-1])) is not None
    assert not t.quarantined()
    stores = t.maintenance()
    S.assert_same_outcomes(stores["live"], stores["rebuilt"], stores["rederived"])
    result["outcomes"] = {k: len(v) for k, v in stores["live"].items()}
    return result


def _descend_and_confirm(t, idle, x, *, low=5):
    """The idle session's 20 becomes the `statusline` baseline (09:00:30),
    then ``x`` responds and descends to ``low``: the pending drop publishes
    after its deadline (arming at 09:04:20, slot 09:00) and a distinct
    same-source observation confirms the credit (09:04:40)."""
    idle.respond(DAY + "09:00:20Z")
    t.oauth(WEEKLY, at=DAY + "09:00:00Z")
    t.render(idle, WEEKLY, five=20, at=DAY + "09:00:30Z")
    x.respond(DAY + "09:01:00Z")
    t.render(x, WEEKLY, five=low, at=DAY + "09:01:10Z")
    t.render(idle, WEEKLY, five=20, at=DAY + "09:01:20Z")
    t.render(x, WEEKLY, five=low, at=DAY + "09:04:20Z")
    t.render(x, WEEKLY, five=low, at=DAY + "09:04:40Z")


def _evidence(t, receipt):
    """The admitted five-hour provider samples requested after the receipt's
    completion instant: the qualifying evidence a refusal relies on."""
    if receipt is None:
        return []
    return [(q, five) for _obs, q, _captured, five, _key in t._rows(
        "SELECT observation_id, requested_at_utc, captured_at_utc, five_hour_percent, "
        "five_hour_window_key FROM claude_oauth_samples WHERE account_key = ? "
        "AND five_hour_percent IS NOT NULL ORDER BY requested_at_utc", (t.account,))
        if q > receipt.completed_at_utc]


def _publications(t, *, policies=False) -> dict:
    """What the status-line and provider paths actually published, from the
    journal (every observation, in order), and — new behaviour, so only
    where the scenario is not a preservation control — their policy stamps."""
    statusline, api = t.observations(), t.observations("api")
    result = {
        "statusline": [f for f, _p, _d in statusline],
        "api": [f for f, _p, _d in api],
    }
    if policies:
        result["policies"] = sorted({(p, d) for _f, p, d in statusline + api})
    return result


# --- S5 (#823 stale re-raise) ------------------------------------------------------


def scenario_s5(t):
    """The complete causal tail of the #823 characterization through the
    status-line path. The parent publishes the idle 20 as a rise (the
    detector baseline is restored to 20) and mints a second credit (20, 6)
    from the later lows in slot 09:10."""
    idle, x = t.session("idle20"), t.session("x")
    _descend_and_confirm(t, idle, x)
    confirmed = t.five_state()
    receipt = t.receipt_for(idle, 20, axis="fiveHour")
    # Provider samples at the post-credit level, requested after the credit.
    t.oauth(WEEKLY, five=5, at=DAY + "09:06:00Z")
    t.render(idle, WEEKLY, five=20, at=DAY + "09:06:30Z")     # the replay
    after_replay = t.five_state()
    # The later lows that minted the second credit in the parent.
    t.render(x, WEEKLY, five=6, at=DAY + "09:11:10Z")
    t.render(x, WEEKLY, five=6, at=DAY + "09:14:20Z")
    t.render(x, WEEKLY, five=6, at=DAY + "09:14:40Z")
    survived = t.receipt_for(idle, 20, axis="fiveHour")
    result = {
        "receipt": None if receipt is None else (receipt.path, receipt.accounted_percent),
        "evidence": _evidence(t, receipt),
        "survived": None if survived is None else survived.path,
        "confirmed": confirmed,
        "after_replay": after_replay,
        "credits": t.five_credits(),
        **_publications(t, policies=True),
        "state": t.five_state(),
    }
    return _common(t, result)


S5_EXPECTED = {
    "receipt": ("maximal", 20.0),
    "evidence": [("2026-10-22T09:05:58.000000Z", 5.0)],
    "survived": "maximal",
    "confirmed": (5.0, None),
    "after_replay": (5.0, None),
    "credits": [(20.0, 5.0, "2026-10-22T09:00:00+00:00")],
    "statusline": [20.0, 5.0, 5.0, 6.0],
    "api": [None, 5.0],
    "policies": [(1, 2)],
    "state": (6.0, None),
    "outcomes": {"in_place": 0, "transitions": 0, "five_hour": 1, "milestones": 1},
}


def test_s5_five_hour_stale_re_raise(monkeypatch, tmp_path):
    result = scenario_s5(S.Trace(monkeypatch, tmp_path))
    print(result)
    assert result == S5_EXPECTED, "RESULT=" + repr(result)


# --- S6 (genuine re-climb) ---------------------------------------------------------


def scenario_s6(t):
    """After the confirmed credit the descending session climbs genuinely,
    one fresh response per step. The 8-step carries the V1 shape: an OAuth
    request started before the response whose lower reading is captured after
    it — not a proof (its ``q`` precedes ``t_m``), which only the capture
    instant used in place of ``q`` turns into one."""
    idle, x = t.session("idle20"), t.session("x")
    _descend_and_confirm(t, idle, x)
    for k in range(6, 10):
        base = at(DAY + "09:06:00Z", 60 * (k - 6))
        x.respond(base)
        if k == 8:
            t.oauth(WEEKLY, five=5, at=at(base, 2), lead=10)
        t.render(x, WEEKLY, five=k, at=at(base, 10))
        t.render(x, WEEKLY, five=k, at=at(base, 40))
    return _common(t, {
        "credits": t.five_credits(),
        **_publications(t),
        "state": t.five_state(),
    })


S6_EXPECTED = {
    "credits": [(20.0, 5.0, "2026-10-22T09:00:00+00:00")],
    "statusline": [20.0, 5.0, 5.0, 6.0, 7.0, 8.0, 9.0],
    "api": [None, 5.0],
    "state": (9.0, None),
    "outcomes": {"in_place": 0, "transitions": 0, "five_hour": 1, "milestones": 1},
}


def test_s6_five_hour_genuine_re_climb(monkeypatch, tmp_path):
    result = scenario_s6(S.Trace(monkeypatch, tmp_path))
    print(result)
    assert result == S6_EXPECTED, "RESULT=" + repr(result)


# --- S7 (growth between arming and confirmation, review F1) ------------------------


def scenario_s7(t):
    """``20 -> 5`` arms at t1 (09:04:20); another session receives a genuine
    post-credit response showing 8 at t2 (09:04:30); a distinct 5 confirms
    the credit at t3 (09:04:40); the second session renders 8 at t4
    (09:05:00). The V1-shaped OAuth reading (requested before t2, captured
    after it, at 5) is the element the capture-instant mutation needs."""
    idle, x, y = t.session("idle20"), t.session("x"), t.session("y")
    idle.respond(DAY + "09:00:20Z")
    t.oauth(WEEKLY, at=DAY + "09:00:00Z")
    t.render(idle, WEEKLY, five=20, at=DAY + "09:00:30Z")
    x.respond(DAY + "09:01:00Z")
    t.render(x, WEEKLY, five=5, at=DAY + "09:01:10Z")
    t.render(idle, WEEKLY, five=20, at=DAY + "09:01:20Z")
    t.render(x, WEEKLY, five=5, at=DAY + "09:04:20Z")          # t1: arms
    armed = t.five_state()
    y.respond(DAY + "09:04:30Z")                                # t2
    t.render(x, WEEKLY, five=5, at=DAY + "09:04:40Z")          # t3: confirms
    confirmed = t.five_state()
    # Requested at 09:04:25 (before t2, in flight across t3), captured at
    # 09:04:50: a provider reading between arming and confirmation would
    # fence the drop's consensus, so the request is still in flight then.
    t.oauth(WEEKLY, five=5, at=DAY + "09:04:50Z", lead=25)
    t.render(y, WEEKLY, five=8, at=DAY + "09:05:00Z")          # t4
    t.render(y, WEEKLY, five=8, at=DAY + "09:05:30Z")
    return _common(t, {
        "armed": armed,
        "confirmed": confirmed,
        "credits": t.five_credits(),
        **_publications(t),
        "state": t.five_state(),
    })


S7_EXPECTED = {
    "armed": (20.0, 5.0),
    "confirmed": (5.0, None),
    "credits": [(20.0, 5.0, "2026-10-22T09:00:00+00:00")],
    "statusline": [20.0, 5.0, 5.0, 8.0],
    "api": [None, 5.0],
    "state": (8.0, None),
    "outcomes": {"in_place": 0, "transitions": 0, "five_hour": 1, "milestones": 1},
}


def test_s7_growth_between_arming_and_confirmation(monkeypatch, tmp_path):
    result = scenario_s7(S.Trace(monkeypatch, tmp_path))
    print(result)
    assert result == S7_EXPECTED, "RESULT=" + repr(result)


# --- F-I5-1: five-hour refusal without a weekly reading ------------------------------


def test_f_i5_1_five_hour_refusal_without_a_weekly_reading(monkeypatch, tmp_path):
    """The five-hour refusal inputs do not depend on a weekly reading
    (review F-I5-1). The account's weekly projection is emptied (no unheld
    weekly reading in any window, the state of an account whose weekly
    readings are all held or absent) while every five-hour reading and
    provider sample stays: S5's stale replay is still refused and still
    mints no second credit. On the parent `_read_stale_inputs` returned
    before it read the five-hour axis, so the replay published as a rise
    and the later lows minted a second credit."""
    t = S.Trace(monkeypatch, tmp_path)
    real = t.sl._read_stale_inputs
    seen = []

    def without_weekly(conn, account_key, **kwargs):
        seen.append((bool(kwargs.get("groups")), kwargs.get("five_window")))
        kwargs.update(groups={}, selected=None, projection=None, floor=None,
                      reference=None)
        stale = real(conn, account_key, **kwargs)
        assert stale.window is None and stale.accounted is None
        assert stale.samples == () and stale.credits == ()
        return stale

    monkeypatch.setattr(t.sl, "_read_stale_inputs", without_weekly)
    result = scenario_s5(t)
    # The weekly readings the wrapper removed did exist, and the five-hour
    # window reached it: the case is the one the review named.
    assert any(grouped for grouped, _five in seen)
    assert any(five is not None for _grouped, five in seen)
    expected = {k: S5_EXPECTED[k] for k in
                ("receipt", "evidence", "survived", "confirmed", "after_replay",
                 "credits", "statusline", "state", "outcomes")}
    assert {k: result[k] for k in expected} == expected, "RESULT=" + repr(result)
