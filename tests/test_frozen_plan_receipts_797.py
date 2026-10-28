"""#797 T4: frozen plans, render acknowledgement and dispatch under the
staleness proof (spec section 4.4, 6 T4; D7c q3).

A frozen plan is re-checked against the current facts before recovery
appends it: a plan whose winner is now refused is dropped and earns
nothing, both against a same-window baseline and under `CLASS_FLOOR`. An
appended plan completes under its own identity and then merges the
transition's CURRENT receipts. A render acknowledges a NOOP only when its
cached facts show no receipt work for either earning path, the
dispatcher's preliminary NOOP exit preserves and schedules that work, and
a member with unknown evidence creates none.

Every step drives the real intake, reducer and record kernel through
`tests/_stale_contributor_trace.py`.
"""
from __future__ import annotations

import dataclasses
import json
import sys

import pytest

import _outcome_quarantine_trace as T
import _stale_contributor_trace as S

DAY = "2026-10-22T"
at = S.at


class Crash(BaseException):
    """A simulated process death at a crash point."""


def crash_at(t, name):
    """Make the next ``name`` crash point raise `Crash` once."""
    real = t.sl._crash_point
    fired = []

    def point(where):
        if where == name and not fired:
            fired.append(where)
            raise Crash(where)
        real(where)

    t.mp.setattr(t.sl, "_crash_point", point)
    return fired


def quiet_render(t, session, percent, *, at, transcript=True) -> bool:
    """One real, non-synchronous render; returns whether it dispatched the
    detached reducer (the dispatch is recorded, not run)."""
    sl = t.sl
    forks = []

    def record(_lock_fd, fd):
        forks.append(fd)
        sl._release_reservation(fd)

    t.clock(at)
    T.identity(t.account_name)
    payload = {"session_id": session.name,
               "rate_limits": {"seven_day": {"used_percentage": percent,
                                             "resets_at": S.WEEK_END}}}
    if transcript:
        payload["transcript_path"] = str(session.path)
    parsed = t.app._lib_statusline.parse_statusline_stdin(json.dumps(payload).encode())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sl, "_fork_persist", record)
        sl._statusline_persist(parsed)
    t.log.append(("quiet-render", session.name, float(percent), at))
    return bool(forks)


def _statusline_floor(t, g, z):
    """A genuine 30 published, then a delayed 0 whose deadline publishes a
    status-line credit (R15: the 30's receipt survives it)."""
    t.oauth(50, at=DAY + "09:00:00Z")
    t.oauth(0, at=DAY + "09:10:00Z")
    z.respond(DAY + "09:10:30Z")
    g.respond(DAY + "09:11:00Z")
    t.render(g, 30, at=DAY + "09:11:10Z")
    for i in range(0, 5 * 60, 30):
        t.render(z, 0, at=at(DAY + "09:12:00Z", i))
    assert t.published() == [30.0, 0.0]


# --- frozen plans -------------------------------------------------------------------


def test_an_unappended_plan_whose_winner_is_now_refused_is_dropped(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    g, z = t.session("genuine"), t.session("delayed")
    _statusline_floor(t, g, z)
    before = t.receipt_for(g, 30)
    assert before is not None and before.path == "maximal"
    # The replay of 30 is eligible (no ordered sample proves it stale yet):
    # the reducer freezes its publication and the process dies.
    fired = crash_at(t, "after-freeze")
    with pytest.raises(Crash):
        for i in range(0, 120, 30):
            t.render(g, 30, at=at(DAY + "09:17:30Z", i))
    assert fired == ["after-freeze"]
    plan = t.transition().publication
    assert plan is not None and plan["provenance"]["sevenDay"]["kind"] == "candidate"
    # CLASS_FLOOR's post-floor receipts exclude the refused winner.
    token = plan["provenance"]["sevenDay"]["token"]
    kept = t.sl._plan_axis_receipts(t.account, plan)
    dropped = t.sl._plan_axis_receipts(t.account, plan,
                                       refused={"sevenDay": frozenset({token})})
    assert sorted(r[2] for r in kept["sevenDay"]) == [0.0, 30.0]
    assert [r[2] for r in dropped["sevenDay"]] == [0.0]
    # An ordered provider sample now proves the 30 stale: recovery drops the
    # plan, appends nothing and earns nothing.
    t.oauth(0, at=DAY + "09:19:00Z")
    t.render(z, 0, at=DAY + "09:19:30Z")
    assert t.transition().publication is None
    assert t.published() == [30.0, 0.0]
    assert t.receipt_for(g, 30) == before


def test_an_appended_plan_completes_and_merges_current_receipts(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    a, g = t.session("idle74"), t.session("genuine")
    a.respond("2026-10-21T22:50:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 74, at=DAY + "09:00:30Z")
    (held,) = t.receipts()
    t.oauth(0, at=DAY + "09:10:00Z")
    g.respond(DAY + "09:11:00Z")
    fired = crash_at(t, "after-append")
    with pytest.raises(Crash):
        t.render(g, 30, at=DAY + "09:11:10Z")
    assert fired == ["after-append"] and t.published() == [30.0]
    # A receipt installed after the plan froze (here, a second token's) is
    # current; recovery must merge it rather than restore the plan's map.
    transition = t.transition()
    extra = dataclasses.replace(held, sigma=dataclasses.replace(
        held.sigma, token="f" * 64))
    t.sl._write_transition(t.sl._replace_transition(
        transition, account_key=t.account, binding=transition.binding,
        projection=transition.control.db_projection,
        control=transition.control, receipts=transition.receipts + (extra,)))
    t.render(g, 30, at=DAY + "09:11:40Z")
    final = t.transition()
    assert final.publication is None
    assert t.published() == [30.0], "the appended plan was published twice"
    tokens = {r.sigma.token: r.path for r in final.receipts}
    assert tokens == {held.sigma.token: "provider_support", "f" * 64: "provider_support",
                      t.token(g): "maximal"}


# --- render acknowledgement and dispatch --------------------------------------------


def _baseline(t):
    """Stored 83 from a sample requested after every later session's last
    response, and one session presenting exactly 83: it holds a maximal
    receipt (i), and while it presents 83 a lower member leaves the
    reduction a NOOP (an unchanged render, before this task, was
    acknowledged without a child)."""
    a = t.session("at83")
    a.respond(DAY + "08:00:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 83, at=DAY + "09:00:30Z")
    assert [r.path for r in t.receipts()] == ["maximal"]
    return a


def test_render_acknowledges_a_noop_only_without_receipt_work(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    a = _baseline(t)
    # Held receipt, unchanged reading: acknowledged, no child.
    assert quiet_render(t, a, 83, at=DAY + "09:01:00Z") is False
    # A second member at exactly the accounted value reduces to the same
    # NOOP but holds no receipt: path (i) work, so the render dispatches.
    c = t.session("also83")
    c.respond(DAY + "08:30:00Z")
    assert quiet_render(t, c, 83, at=DAY + "09:01:20Z") is True
    # The detached reducer completes it and earns; the next renders are quiet.
    t.render(c, 83, at=DAY + "09:01:40Z")
    assert t.receipt_for(c, 83).path == "maximal"
    assert quiet_render(t, c, 83, at=DAY + "09:02:00Z") is False
    assert quiet_render(t, a, 83, at=DAY + "09:02:10Z") is False
    assert t.published() == []


@pytest.mark.parametrize("disabled", ["none", "render-acknowledgement", "both"])
def test_the_preliminary_noop_exit_schedules_receipt_work(monkeypatch, tmp_path, disabled):
    """With the render acknowledgement's receipt check disabled, the
    dispatcher's preliminary NOOP exit still dispatches the work; with both
    disabled the render swallows it (the control that proves the receipt
    checks, and nothing else, are what dispatch)."""
    t = S.Trace(monkeypatch, tmp_path)
    _baseline(t)
    real = t.sl._receipt_work

    def work(transition, members):
        caller = sys._getframe(1).f_code.co_name
        if disabled == "both" or (disabled != "none"
                                  and caller == "_render_acknowledge"):
            return False
        return real(transition, members)

    monkeypatch.setattr(t.sl, "_receipt_work", work)
    c = t.session("also83")
    c.respond(DAY + "08:30:00Z")
    assert quiet_render(t, c, 83, at=DAY + "09:01:20Z") is (disabled != "both")


def test_unknown_evidence_never_keeps_a_noop_detached(monkeypatch, tmp_path):
    t = S.Trace(monkeypatch, tmp_path)
    _baseline(t)
    u = t.session("unknown")
    assert quiet_render(t, u, 83, at=DAY + "09:01:20Z", transcript=False) is False
    assert quiet_render(t, u, 83, at=DAY + "09:01:40Z", transcript=False) is False


def test_receipt_work_covers_both_earning_paths_from_cached_facts(monkeypatch, tmp_path):
    """The render's file-only judgement on real members and the real cached
    facts: a below-baseline weekly member that path (ii) could make eligible
    is work; one whose response follows every cached sample is not; a held
    receipt, unknown evidence or another window is not; without a cache
    every member with known evidence is."""
    t = S.Trace(monkeypatch, tmp_path)
    a = _baseline(t)
    supported, late = t.session("below-early"), t.session("below-late")
    supported.respond(DAY + "08:00:00Z")
    late.respond(DAY + "09:00:50Z")
    u = t.session("unknown")
    # Review RV-7: both renders dispatch. Path (ii) work alone would; but a
    # reading below the published baseline also arms a pending drop, which
    # only the detached reducer may write, so the render result cannot tell
    # the two members apart — `_receipt_work` below judges them one by one.
    assert quiet_render(t, supported, 74, at=DAY + "09:01:00Z") is True
    assert quiet_render(t, late, 74, at=DAY + "09:01:05Z") is True
    quiet_render(t, u, 74, at=DAY + "09:01:10Z", transcript=False)
    spool = t.sl._load_candidate_spool(now_epoch=S.epoch(DAY + "09:01:10Z"))
    members = {}
    for candidate in spool:
        for name in ("at83", "below-early", "below-late", "unknown"):
            if candidate.token == t.token(t.session(name)):
                members[name] = candidate
    assert set(members) == {"at83", "below-early", "below-late", "unknown"}, [
        c.token for c in spool]
    transition = t.transition()
    assert transition.stale_cache is not None
    work = t.sl._receipt_work
    assert work(transition, [members["at83"]]) is False          # held (i)
    assert work(transition, [members["below-early"]]) is True    # (ii) work
    assert work(transition, [members["below-late"]]) is False    # no support
    assert work(transition, [members["unknown"]]) is False       # unknown
    uncached = dataclasses.replace(transition, stale_cache=None)
    assert work(uncached, [members["below-late"]]) is True
    assert work(uncached, [members["unknown"]]) is False


# --- F-I4-2: a saturated receipt map is not perpetual receipt work ---------------


def _saturate(t, monkeypatch):
    """Make the control document limit admit no receipt at all: the limit is
    the transition's size with every evaluated receipt dropped (and, where
    the render cache records omissions, recorded as omitted) plus 10 bytes."""
    sl = t.sl
    lib = sys.modules["_lib_statusline_candidates"]
    real = sl._fit_transition

    def tight(transition, **kwargs):
        bare = dataclasses.replace(transition, receipts=())
        if bare.stale_cache is not None:
            try:
                bare = dataclasses.replace(bare, stale_cache=dataclasses.replace(
                    bare.stale_cache,
                    omitted=frozenset(r.sigma.key() for r in transition.receipts)))
            except TypeError:   # the parent's cache records no omissions
                pass
        size = len(json.dumps(sl._transition_document(bare), sort_keys=True,
                              separators=(",", ":")).encode())
        monkeypatch.setattr(lib, "CONTROL_DOCUMENT_MAX_BYTES", size + 10)
        try:
            return real(transition, **kwargs)
        finally:
            monkeypatch.setattr(lib, "CONTROL_DOCUMENT_MAX_BYTES", 1 << 20)

    monkeypatch.setattr(sl, "_fit_transition", tight)


def test_a_saturated_document_does_not_dispatch_on_every_render(monkeypatch, tmp_path):
    """A receipt the completion evaluated and then omitted at the document
    limit is no longer receipt work for a render (its member stays
    eligible), so an unchanged render is acknowledged instead of
    dispatching a completion that would omit it again — until something
    that could change the outcome happens: a new member at the accounted
    value is work once, and its completion records it as omitted too."""
    t = S.Trace(monkeypatch, tmp_path)
    _saturate(t, monkeypatch)
    a = t.session("at83")
    a.respond(DAY + "08:00:00Z")
    t.oauth(83, at=DAY + "09:00:00Z")
    t.render(a, 83, at=DAY + "09:00:30Z")
    assert t.receipts() == [], "the receipt was omitted at the limit"
    dispatched = [quiet_render(t, a, 83, at=at(DAY + "09:01:00Z", 20 * i))
                  for i in range(5)]
    assert dispatched == [False] * 5, "a saturated document dispatched every render"
    cache = t.transition().stale_cache
    assert cache is not None and len(cache.omitted) == 1
    c = t.session("also83")
    c.respond(DAY + "08:30:00Z")
    assert quiet_render(t, c, 83, at=DAY + "09:03:00Z") is True
    t.render(c, 83, at=DAY + "09:03:20Z")
    assert t.receipts() == []
    assert len(t.transition().stale_cache.omitted) == 2
    assert [quiet_render(t, m, 83, at=at(DAY + "09:03:40Z", 20 * i))
            for i, m in enumerate((a, c, a, c))] == [False] * 4
    assert t.published() == []
