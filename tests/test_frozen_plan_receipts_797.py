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


# --- #1019 L7: frozen-plan recovery revalidates drop dependencies --------------------

#: A stale member's fixed supported activity, long before every sample.
STALE_ACTIVITY = "2026-10-21T22:50:00Z"


def _frozen(t, steps):
    """Run ``steps`` until the reducer freezes a publication plan and the
    process dies right after (`after-freeze`); return the unappended plan."""
    fired = crash_at(t, "after-freeze")
    with pytest.raises(Crash):
        steps()
    assert fired == ["after-freeze"]
    plan = t.transition().publication
    assert plan is not None
    return plan


import contextlib  # noqa: E402


@contextlib.contextmanager
def _unadmitted(t, observation_id):
    """While open, the provider sample ``observation_id`` is not yet visible
    to the sample reader: the race in which a proof is admitted after the
    reduction that froze a plan read its samples. No later reading is
    captured, so nothing supersedes the plan; its recovery meets the proof."""
    samples = sys.modules["_cctally_oauth_samples"]
    real = samples.read_samples

    def read(*args, **kwargs):
        return tuple(s for s in real(*args, **kwargs)
                     if s.observation_id != observation_id)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(samples, "read_samples", read)
        yield


def _proof(t, value, *, at):
    """An admitted OAuth ``value`` captured at ``at``; returns its
    observation id."""
    q = t.oauth(value, at=at)
    (obs,) = [r[0] for r in t._rows(
        "SELECT observation_id FROM claude_oauth_samples "
        "WHERE account_key = ? AND requested_at_utc = ?", (t.account, q))]
    return obs


def _stale_drop_plan(t, *, five=None):
    """Baseline 51 from a status-line reading, then an OAuth 51 requested
    after the stale X's activity that the freezing passes do not yet see;
    X reports 18 twice and the unanimous drop is frozen (its second report
    also carries the five-hour ``five``). Returns ``(plan, proof)``."""
    live, x = t.session("live"), t.session("staleX")
    live.respond(DAY + "09:00:00Z")
    x.respond(STALE_ACTIVITY)
    t.render(live, 51, at=DAY + "09:00:10Z", five=5)
    proof = _proof(t, 51, at=DAY + "09:04:00Z")
    with _unadmitted(t, proof):
        plan = _frozen(t, lambda: (t.render(x, 18, at=DAY + "09:05:00Z"),
                                   t.render(x, 18, at=DAY + "09:05:30Z", five=five)))
    return plan, proof


def test_unappended_candidate_drop_plan_superseded_when_winner_refused(monkeypatch, tmp_path):
    """L7 (accuracy: a frozen plan publishing a refuted low): the plan's
    candidate winner becomes drop-refused before recovery appends it, so the
    whole unappended plan is superseded and its evaluation reduced again,
    which still publishes the other axis (the five-hour rise). RED on the
    parent: the 18 publishes and resets the week."""
    t = S.Trace(monkeypatch, tmp_path)
    plan, _proof_id = _stale_drop_plan(t, five=10)
    assert plan["provenance"]["sevenDay"]["kind"] == "candidate"
    assert plan["fiveHour"]["percent"] == 10.0
    t.render(t.session("live"), 51, at=DAY + "09:06:00Z", five=10)
    assert t.transition().publication is None
    assert [p for p in t.published() if p < 51.0] == []
    assert t.resets() == []
    assert 10.0 in t.five_published(), "the other axis was discarded with the plan"


def _retained_plan(t, *, survivor):
    """A ``retained_low`` plan: stale X (and, with ``survivor``, genuine Y
    whose response follows the proof's request) report 18 once and age
    out; at the deadline the live 51 is present and the retained low
    publishes. The proof, an OAuth 51 requested after X's activity, is not
    yet visible to those passes."""
    live, x, y = t.session("live"), t.session("staleX"), t.session("genuineY")
    live.respond(DAY + "09:00:00Z")
    x.respond(STALE_ACTIVITY)
    t.render(live, 51, at=DAY + "09:00:10Z")
    proof = _proof(t, 51, at=DAY + "09:04:00Z")
    with _unadmitted(t, proof):
        t.render(x, 18, at=DAY + "09:05:00Z")
        if survivor:
            y.respond(DAY + "09:04:30Z")
            t.render(y, 18, at=DAY + "09:05:05Z")
        plan = _frozen(t, lambda: t.render(live, 51, at=DAY + "09:08:10Z"))
    source = plan["provenance"]["sevenDay"]
    assert source["kind"] == "retained_low"
    assert len(source["supports"]) == (2 if survivor else 1)
    return plan


def test_retained_low_plan_with_exhausted_support_superseded(monkeypatch, tmp_path):
    """L7: every frozen support of a ``retained_low`` plan is refuted, so
    the plan is superseded and nothing below the baseline publishes. RED on
    the parent: the retained 18 publishes."""
    t = S.Trace(monkeypatch, tmp_path)
    _retained_plan(t, survivor=False)
    t.render(t.session("live"), 51, at=DAY + "09:08:40Z")
    assert t.transition().publication is None
    assert [p for p in t.published() if p < 51.0] == []
    assert t.resets() == []


def test_retained_low_plan_with_surviving_support_publishes_as_that_support(
        monkeypatch, tmp_path):
    """L7: one frozen support is refuted and the genuine one, whose bound
    follows the proof, survives: the plan publishes under its own identity."""
    t = S.Trace(monkeypatch, tmp_path)
    plan = _retained_plan(t, survivor=True)
    t.render(t.session("live"), 51, at=DAY + "09:08:40Z")
    assert t.transition().publication is None
    assert t.published()[:2] == [51.0, 18.0]
    assert [p for _o, _n, p in t.resets()] == [51.0]
    ids = _appended_publication_ids(t)
    assert ids.count(plan["publicationId"]) == 1 and ids[1] == plan["publicationId"]


def _appended_publication_ids(t) -> list:
    """The publication identity of every status-line observation journalled."""
    return [payload["publication_id"] for rec in T.journal_records()
            if rec.get("t") == "obs"
            and (payload := rec.get("payload") or {}).get("source") == "statusline"
            and payload.get("publication_id") is not None]


def test_missing_recovery_evidence_defers_with_identity_unchanged(monkeypatch, tmp_path):
    """L7: recovery cannot bind the frozen winner to its evaluation member
    (the intake record is unreadable): the plan stays frozen under its
    identity and publishes nothing — missing evidence is never "no proof".
    Review finding P2 (machine safety): the deferral is an io wait, so the
    head waits and renders stop dispatching a pass that defers again. RED:
    no wait was installed and every render dispatched."""
    t = S.Trace(monkeypatch, tmp_path)
    plan, _proof_id = _stale_drop_plan(t)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(t.sl, "_plan_members", lambda account_key, plan: ())
        t.render(t.session("live"), 51, at=DAY + "09:06:00Z")
        held = t.transition().publication
        assert held is not None and held["publicationId"] == plan["publicationId"]
        assert [p for p in t.published() if p < 51.0] == []
        wait = t.sl._read_wait(t.account)
        assert wait is not None and (wait["class"], wait["reason"]) == (
            "io", t.sl._DROP_EVIDENCE_REASON)
        # Retried on the io backoff (it doubles), not on every render.
        due = wait["retry"]["deadline"]
        quiet = [quiet_render(t, t.session("live"), 51, at=at(DAY + "09:06:10Z", 10 * i))
                 for i in range(3)]
        assert S.epoch(at(DAY + "09:06:10Z", 20)) < due
        assert quiet == [False] * 3, "a deferred plan dispatched every render"


def test_appended_plan_completes_unchanged(monkeypatch, tmp_path):
    """L7: an already-appended plan completes under its identity, never
    revalidated, even when a proof of its winner has since arrived."""
    t = S.Trace(monkeypatch, tmp_path)
    live, x = t.session("live"), t.session("staleX")
    live.respond(DAY + "09:00:00Z")
    x.respond(STALE_ACTIVITY)
    t.render(live, 51, at=DAY + "09:00:10Z")
    proof = _proof(t, 51, at=DAY + "09:04:00Z")
    fired = crash_at(t, "after-append")
    with _unadmitted(t, proof), pytest.raises(Crash):
        t.render(x, 18, at=DAY + "09:05:00Z")
        t.render(x, 18, at=DAY + "09:05:30Z")
    assert fired == ["after-append"]
    plan = t.transition().publication
    t.render(live, 51, at=DAY + "09:06:00Z")
    assert t.transition().publication is None
    assert t.published()[:2] == [51.0, 18.0]
    ids = _appended_publication_ids(t)
    assert ids.count(plan["publicationId"]) == 1 and ids[1] == plan["publicationId"]


def _rise_plan(t, *, proof=True):
    """Baseline 0 after a status-line credit; stale X (fixed supported
    activity, no receipt) then reports 74 and the rise is frozen. With
    ``proof``, an admitted OAuth 83 requested after X's activity and before
    the credit exists but is not yet visible to the freezing pass."""
    live, x, y = t.session("live"), t.session("staleX"), t.session("freshY")
    live.respond(DAY + "09:00:00Z")
    x.respond(STALE_ACTIVITY)
    t.render(live, 83, at=DAY + "09:00:10Z")
    hidden = _proof(t, 83, at=DAY + "09:10:00Z") if proof else None
    y.respond(DAY + "09:30:00Z")
    t.render(y, 0, at=DAY + "09:30:10Z")
    t.render(y, 0, at=DAY + "09:30:40Z")
    assert t.stored() == 0.0
    assert t.receipt_for(x, 74) is None
    with (_unadmitted(t, hidden) if hidden else contextlib.nullcontext()):
        plan = _frozen(t, lambda: t.render(x, 74, at=DAY + "09:32:00Z"))
    assert plan["provenance"]["sevenDay"]["token"] == t.token(x)
    return plan


def test_unappended_rise_plan_superseded_when_winner_drop_refused(monkeypatch, tmp_path):
    """L7 rise side (P-R2, P-R3; accuracy: a stale pre-credit reading
    re-raising a credited account): an admitted OAuth 83 requested after
    X's activity makes the frozen rise's winner drop-refused, so the plan is
    superseded and the 74 never publishes. RED on the parent: it publishes
    the 74."""
    t = S.Trace(monkeypatch, tmp_path)
    _rise_plan(t)
    t.render(t.session("freshY"), 0, at=DAY + "09:32:30Z")
    assert t.transition().publication is None
    assert 74.0 not in t.published()


def test_unappended_rise_plan_with_missing_drop_evidence_defers(monkeypatch, tmp_path):
    """L7 rise side (P-R3): missing drop evidence for an unappended rise
    plan defers it under its identity instead of reading as "no proof".
    RED on the parent: the rise publishes."""
    t = S.Trace(monkeypatch, tmp_path)
    plan = _rise_plan(t, proof=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(t.sl, "_plan_members", lambda account_key, plan: ())
        t.render(t.session("freshY"), 0, at=DAY + "09:32:30Z")
        held = t.transition().publication
        assert held is not None and held["publicationId"] == plan["publicationId"]
        assert 74.0 not in t.published()


# --- #1019 L9: render acknowledgement keeps pending drop-proof work ---------------


def test_797_a_refused_stale_rise_does_not_dispatch_on_every_render(monkeypatch, tmp_path):
    """Review finding P1's twin in #797 (machine safety: one detached
    reducer pass per render). After a credit, a replayed pre-credit reading
    whose receipt an ordered sample refutes is refused; unchanged renders of
    either session are then acknowledged instead of dispatching a pass that
    refuses it again. RED (1.116.x too): the render reduced without the
    refusal, saw an eligible rise and dispatched on every render."""
    t = S.Trace(monkeypatch, tmp_path)
    g, z = t.session("genuine"), t.session("delayed")
    _statusline_floor(t, g, z)
    t.oauth(0, at=DAY + "09:19:00Z")
    t.render(z, 0, at=DAY + "09:19:30Z")
    t.render(g, 30, at=DAY + "09:20:00Z")
    t.render(z, 0, at=DAY + "09:20:10Z")
    assert t.published() == [30.0, 0.0] and t.receipt_for(g, 30) is not None
    quiet = ((g, 30), (z, 0), (g, 30), (z, 0))
    dispatched = [quiet_render(t, m, v, at=at(DAY + "09:20:20Z", 20 * i))
                  for i, (m, v) in enumerate(quiet)]
    assert dispatched == [False] * 4, "a refused stale rise dispatched every render"
    assert t.published() == [30.0, 0.0]


def test_1019_a_refused_stale_low_does_not_dispatch_on_every_render(monkeypatch, tmp_path):
    """Review finding P1 (machine safety: one detached reducer pass per
    render). Once the detached reducer refuses a proven-stale low, the
    render's covered cache decides the same refusal: unchanged renders of
    either session are acknowledged instead of dispatching a pass that
    refuses it again, until a new reading arrives (work once). RED: the
    render reduced without the drop facts, re-armed the drop and dispatched
    on every render while the stale member stayed active."""
    t = S.Trace(monkeypatch, tmp_path)
    live, x = t.session("live"), t.session("staleX")
    live.respond(DAY + "09:00:00Z")
    x.respond(STALE_ACTIVITY)
    t.render(live, 51, at=DAY + "09:00:10Z")
    t.oauth(51, at=DAY + "09:04:00Z")
    t.render(live, 51, at=DAY + "09:04:50Z")
    t.render(x, 18, at=DAY + "09:05:00Z")
    t.render(live, 51, at=DAY + "09:05:10Z")
    assert t.transition().control.pending_drops.get("sevenDay") is None
    quiet = ((live, 51), (x, 18), (live, 51), (x, 18))
    dispatched = [quiet_render(t, m, v, at=at(DAY + "09:05:20Z", 20 * i))
                  for i, (m, v) in enumerate(quiet)]
    assert dispatched == [False] * 4, "a refused stale low dispatched every render"
    # A new reading is work once; refused again, it is quiet again.
    assert quiet_render(t, x, 17, at=DAY + "09:07:00Z") is True
    t.render(x, 17, at=DAY + "09:07:20Z")
    assert [quiet_render(t, m, v, at=at(DAY + "09:07:40Z", 20 * i))
            for i, (m, v) in enumerate(((live, 51), (x, 17)))] == [False] * 2
    assert [p for p in t.published() if p < 51.0] == [] and t.resets() == []


def test_1019_l9_render_ack_keeps_pending_drop_proof_work(monkeypatch, tmp_path):
    """L9 (accuracy: a refuted retained low kept alive by the render): a
    drop armed before its proof was visible is pending beside the live 51,
    which dissents; the render's cached facts no longer cover its support.
    An unchanged render that reduces to a NOOP must not acknowledge that
    proof work but dispatch the detached reducer, which prunes the refuted
    support and cancels the drop. The receipt checks are disabled, so only
    the proof-work check decides. RED on the parent: the render acknowledges
    and the refuted drop stays pending."""
    t = S.Trace(monkeypatch, tmp_path)
    live, x = t.session("live"), t.session("staleX")
    live.respond(DAY + "09:00:00Z")
    x.respond(STALE_ACTIVITY)
    t.render(live, 51, at=DAY + "09:00:10Z")
    proof = _proof(t, 51, at=DAY + "09:04:00Z")
    with _unadmitted(t, proof):
        t.render(live, 51, at=DAY + "09:04:50Z")
        t.render(x, 18, at=DAY + "09:05:00Z")
        t.render(live, 51, at=DAY + "09:05:20Z")
        t.render(x, 18, at=DAY + "09:05:30Z")
    transition = t.transition()
    assert transition.control.pending_drops.get("sevenDay") is not None
    # The covered cache the hidden passes wrote is replaced by none, as a
    # writer of a projection it was not read for leaves it.
    t.sl._write_transition(dataclasses.replace(transition, stale_cache=None))
    monkeypatch.setattr(t.sl, "_receipt_work", lambda transition, members: False)
    assert quiet_render(t, live, 51, at=DAY + "09:05:50Z") is True
    t.render(live, 51, at=DAY + "09:06:10Z")
    assert t.transition().control.pending_drops.get("sevenDay") is None
    assert [p for p in t.published() if p < 51.0] == []
