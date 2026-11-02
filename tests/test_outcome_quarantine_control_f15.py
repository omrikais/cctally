"""#825/#832 Task 3.6 — drop-consensus withholding, case F15 (spec 5.6
items 1(b) and 7), rewritten for #948's per-axis recovery barrier (spec
6.3, 6.4, 6.8).

(a) Spool-only legacy activity — a frozen render writing a legacy candidate and
    no journal observation — is journaled as `legacy_spool_activity` before the
    next current publication and pauses every account, including the
    round-6 interleaving inside the cutover establishment and the stale loser
    of the establishment race.
(b) While an axis is paused a pending drop below the database baseline is
    published neither by consensus nor at its deadline; a rise publishes.
(c) Fresh readings without completed OAuth authority release nothing, so drop
    consensus stays withheld.
(d) A bound authoritative publication covers A and not B: after the fresh
    period A's drop publishes and B's stays withheld.
(e) Retained-low expiry, an empty spool and a non-expiring inflight tombstone
    never count as recovery.
(f) After spool-only activity, only the whole barrier — expiry, completed
    authority, a fresh witness and its release — resumes drops; an
    unchanged complete inventory closes an open episode with a non-trigger
    completion op whatever the sidecar says.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import types

import pytest

import _outcome_quarantine_trace as T

DEADLINE = 180
TICK = 10
#: 2026-10-09T10:00:00Z, a fixed statusline clock.
T0 = 1791540000


class Clock:
    def __init__(self, app, monkeypatch, start):
        self.value = start
        self._monkeypatch = monkeypatch
        sl = app._cctally_statusline
        pinned = types.SimpleNamespace(**vars(sl.time))
        pinned.time = lambda: self.value
        monkeypatch.setattr(sl, "time", pinned)
        import time as _time
        monkeypatch.setattr(_time, "time", lambda: float(self.value))
        self._pin()

    def _pin(self):
        self._monkeypatch.setenv("CCTALLY_AS_OF", T.iso(self.value))
        T.pin_journal_clock(self._monkeypatch, self.value)

    def advance(self, seconds):
        self.value += seconds
        self._pin()


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


def _key(name):
    import _lib_accounts
    return _lib_accounts.account_key("claude", name)


def _seed(app, clock, name, percent, *, week_reset, five=None, five_reset=None,
          source="api"):
    import _cctally_statusline as sl
    T.identity(name)
    args = argparse.Namespace(
        percent=percent, resets_at=str(week_reset), five_hour_percent=five,
        five_hour_resets_at=None if five_reset is None else str(five_reset),
        source=source)
    assert app.record_usage_bound(args, binding=sl._capture_binding(),
                                  nudge_dashboard=False) == 0


def _spool(app, clock, name, session, percent, week_reset):
    import _cctally_statusline as sl
    T.identity(name)
    parsed = app._lib_statusline.parse_statusline_stdin(json.dumps({
        "session_id": session,
        "rate_limits": {"seven_day": {"used_percentage": percent,
                                      "resets_at": T.iso(week_reset)}},
    }).encode())
    sl._write_candidate(sl._candidate_from_input(parsed, received_at=clock.value))


def _weekly(app, name):
    rows = T._rows(app, "SELECT weekly_percent FROM weekly_usage_snapshots "
                        f"WHERE account_key = '{_key(name)}' "
                        "ORDER BY captured_at_utc DESC, id DESC LIMIT 1")
    return rows[0]["weekly_percent"] if rows else None


def _drive(app, clock, name, reporters, week_reset, ticks):
    """Re-render every reporter and run one reducer pass per tick."""
    import _cctally_statusline as sl
    for _ in range(ticks):
        for session, percent in reporters:
            _spool(app, clock, name, session, percent, week_reset)
        sl._statusline_reduce_and_publish(nudge_sink=lambda: None)
        clock.advance(TICK)


def _published(name, percent):
    """Did the reducer publish ``percent`` for ``name`` (the drop-consensus
    outcome itself — a statusline publication observation), whatever the
    accounting pipeline then made of it?"""
    import _cctally_journal as jr
    import _lib_journal as J
    import _cctally_core as core
    for segment in jr.list_segments():
        for line in (core.JOURNAL_DIR / segment).read_bytes().splitlines():
            rec = J.decode_line(line)
            payload = (rec or {}).get("payload") or {}
            if (rec and rec.get("t") == "obs" and "publication_id" in payload
                    and (rec.get("account") or "unattributed") == _key(name)
                    and payload.get("weekly_percent") == float(percent)):
                return True
    return False


def _write_legacy_candidate(text='{"legacy": 70}'):
    import _cctally_core as core
    core.STATUSLINE_CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    (core.STATUSLINE_CANDIDATE_DIR / "legacy-session.json").write_text(text)


def _spool_activity_ops():
    import _cctally_journal as jr
    import _lib_journal as J
    import _cctally_core as core
    out = []
    for segment in jr.list_segments():
        for line in (core.JOURNAL_DIR / segment).read_bytes().splitlines():
            rec = J.decode_line(line)
            if rec and (rec.get("payload") or {}).get("kind") == "legacy_spool_activity":
                out.append(rec)
    return out


def _status(app, name):
    import _cctally_quarantine as G
    conn = app.open_db()
    try:
        return G.account_status(conn, _key(name))
    finally:
        conn.close()


def _pending(app, clock, name):
    control = app._read_control_state(now_epoch=clock.value, account_key=_key(name))
    return None if control is None else control.pending_drops["sevenDay"]


WEEK = T0 + 3 * 86400


def test_reference_the_unanimous_drop_publishes_without_a_trigger(app, monkeypatch):
    """Non-vacuity: with no legacy activity two sessions at 20 publish the
    drop below the database's 70 by consensus."""
    clock = Clock(app, monkeypatch, T0)
    _seed(app, clock, "A", 70, week_reset=WEEK)
    _drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK, 6)
    assert _published("A", 20)
    assert _weekly(app, "A") == 20


def test_a_and_b_spool_only_legacy_activity_withholds_the_drop(app, monkeypatch):
    clock = Clock(app, monkeypatch, T0)
    _seed(app, clock, "A", 70, week_reset=WEEK)
    _write_legacy_candidate()
    _drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK,
           1 + 2 * DEADLINE // TICK)
    ops = _spool_activity_ops()
    assert [op["payload"]["reason"] for op in ops] == ["changed"]
    for name in ("A", "B", "unattributed"):
        assert _paused(app, name), name
    # Published neither by consensus nor at its deadline: it stays pending.
    assert not _published("A", 20)
    assert _weekly(app, "A") == 70
    assert _pending(app, clock, "A") is not None
    # A rise still publishes.
    _drive(app, clock, "A", (("one", 80), ("two", 80)), WEEK, 2)
    assert _weekly(app, "A") == 80


def test_a_round_six_interleaving_inside_the_establishment(app, monkeypatch):
    """A frozen render writes a same-window 70 candidate between the
    establishment's baseline capture and its rescan; current 20 candidates
    refreshed one second apart never publish by consensus."""
    import _cctally_journal as jr
    clock = Clock(app, monkeypatch, T0)

    def seam(step):
        if step == "after-baseline-capture":
            _write_legacy_candidate('{"sevenDay": 70}')

    monkeypatch.setattr(jr, "_ACCOUNT_BOUND_CUTOVER_SEAM", seam)
    _seed(app, clock, "A", 70, week_reset=WEEK)   # establishes the cutover
    monkeypatch.setattr(jr, "_ACCOUNT_BOUND_CUTOVER_SEAM", None)
    for _ in range(6):
        _drive(app, clock, "A", (("one", 20),), WEEK, 1)
        clock.advance(-TICK + 1)
        _drive(app, clock, "A", (("two", 20),), WEEK, 1)
        clock.advance(-TICK + 1)
    assert _spool_activity_ops(), "the rescan journaled the legacy write"
    assert not _published("A", 20)


def _refuse_legacy_inventory(monkeypatch, failure, failing):
    """While ``failing[0]``, the legacy candidate's read (``file-read``) or
    its directory's listing (``directory-listing``) fails."""
    import builtins
    import errno
    import os
    import _cctally_core as core
    import _cctally_journal as jr
    candidate_dir = os.fspath(core.STATUSLINE_CANDIDATE_DIR)
    if failure == "file-read":
        def refusing_open(file, *args, **kwargs):
            if (failing[0] and not isinstance(file, int)
                    and os.path.dirname(os.fspath(file)) == candidate_dir):
                raise OSError(errno.EIO, "injected legacy read failure",
                              os.fspath(file))
            return builtins.open(file, *args, **kwargs)

        monkeypatch.setattr(jr, "open", refusing_open, raising=False)
        return
    real_scandir = os.scandir

    def refusing_scandir(path=".", *args, **kwargs):
        if (failing[0] and not isinstance(path, int)
                and os.fspath(path) == candidate_dir):
            raise PermissionError(errno.EACCES, "injected listing failure",
                                  os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", refusing_scandir)


def _marker_reasons(app):
    """The reasons of every legacy-spool trigger, in journal order (#948:
    the unresolved spool markers became the barrier's triggers)."""
    return [t["reason"] for t in T.quarantine_state(app)["triggers"]
            if t["kind"] == "legacy-spool-activity"]


def _paused(app, name):
    """Whether every axis of ``name`` is paused."""
    return not any(T.axis_allowed(app, name).values())


def _assert_markers(app, expected, step):
    assert _marker_reasons(app) == expected, step
    for name in ("A", "B", "unattributed"):
        assert _paused(app, name), (step, name)


@pytest.mark.parametrize("failure", ["file-read", "directory-listing"])
def test_a_an_incomplete_spool_inventory_never_authorizes_the_low_drop(
        app, monkeypatch, failure):
    """Review cycle 7 825-832-CD7-J01 and spec revision 5 (operator decision
    on Codex design-fix-round-5 Q4; decisive test 29): the high-peer
    interleaving with an incomplete inventory on the decisive reduction. A
    is at 70 and a frozen legacy render has written a same-window 70
    candidate after the cutover established its (empty) baseline, while two
    current sessions report 20. The legacy candidate's read — or the listing
    of its directory — fails on every reduction that could publish the drop.
    An inventory that silently omitted the candidate digested equal to the
    empty baseline and authorized the low publication.

    Revision 5: the first failed inventory appends ONE `inventory-incomplete`
    op before any publication; it quarantines every account with the
    unresolved marker, so the drop is never published, while a classifiable
    rise still publishes. Once reads recover, the complete inventory anchors
    a complete baseline (`changed`, the candidate it finally sees) and the
    marker stays unresolved through ingest, rebuild and rederive."""
    import _cctally_journal as jr
    import _cctally_core as core
    import _cctally_statusline as sl
    clock = Clock(app, monkeypatch, T0)
    _seed(app, clock, "A", 70, week_reset=WEEK)   # establishes the cutover
    _write_legacy_candidate('{"sevenDay": 70}')     # the legacy high peer
    seeded = _journal_kinds().count("publication")
    failing = [True]
    _refuse_legacy_inventory(monkeypatch, failure, failing)
    deferred = 0
    for _ in range(1 + 2 * DEADLINE // TICK):
        for session in ("one", "two"):
            _spool(app, clock, "A", session, 20, WEEK)
        try:
            results = sl._statusline_reduce_and_publish(nudge_sink=lambda: None)
        except OSError:
            deferred += 1
        else:
            # Spec 4.3 revision 5 (*Waiting heads*): the pass defers the head
            # as a waiting head on the inventory rather than raising.
            deferred += sum(1 for result in results
                            if result.reason == sl._LEGACY_INVENTORY_REASON)
        clock.advance(TICK)
    assert deferred == 0, "a durable uncertainty trigger never defers"
    assert not _published("A", 20), (
        "an incomplete legacy spool inventory authorized the low drop")
    assert _weekly(app, "A") == 70
    ops = _spool_activity_ops()
    assert [op["payload"]["reason"] for op in ops] == ["inventory-incomplete"]
    [op] = ops
    assert "digest" not in op["payload"]
    assert op["payload"]["failure"]["operation"] == (
        "read" if failure == "file-read" else "list")
    assert op["payload"]["failure"]["path"].startswith(
        "statusline-candidates")
    kinds = _journal_kinds()
    # Journaled before any publication planned after the legacy write.
    assert kinds[:kinds.index("legacy_spool_activity")].count(
        "publication") == seeded
    _assert_markers(app, ["inventory-incomplete"], "during the failure")
    # Classifiable ordinary recording continues: a rise publishes.
    _drive(app, clock, "A", (("one", 80), ("two", 80)), WEEK, 2)
    assert _weekly(app, "A") == 80
    assert _published("A", 80)
    assert len(_spool_activity_ops()) == 1, "one op per failure episode"

    failing[0] = False
    _drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK,
           1 + 2 * DEADLINE // TICK)
    assert [op["payload"]["reason"] for op in _spool_activity_ops()] == [
        "inventory-incomplete", "changed"]
    baseline = jr.read_anchored_legacy_spool_baseline()
    assert baseline is not None, "the complete inventory anchored a baseline"
    assert [entry[0] for entry in baseline.entries] == [
        "statusline-candidates/legacy-session.json"]
    assert core.LEGACY_SPOOL_BASELINE_PATH.exists()
    assert not _published("A", 20)
    assert _weekly(app, "A") == 80
    _assert_markers(app, ["inventory-incomplete", "changed"], "after ingest")
    T.rebuild(app)
    _assert_markers(app, ["inventory-incomplete", "changed"], "after rebuild")
    T.retain_session_input(app, "A")   # rederive needs retained input
    T.rederive(app)
    _assert_markers(app, ["inventory-incomplete", "changed"],
                    "after rederive")
    _drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK,
           1 + 2 * DEADLINE // TICK)
    assert not _published("A", 20)
    assert _weekly(app, "A") == 80


def test_a_pre_cutover_incomplete_inventory_is_the_cutovers_own_trigger(
        app, monkeypatch):
    """Spec revision 5, section 7 *Before the initial cutover*: the legacy
    spool cannot be read when the first current producer establishes the
    cutover. The cutover records the uncertainty in itself — no separate op,
    no baseline — and that is the trigger that opens an inventory episode:
    drops stay withheld through ingest, rebuild and rederive, while
    recording continues. The next complete inventory anchors a baseline
    under its own op, which closes the episode (and is itself a trigger,
    #948 spec 6.3). Doctor's wording of it is pinned by the doctor tests."""
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_account_protocol as P
    clock = Clock(app, monkeypatch, T0)
    _write_legacy_candidate('{"sevenDay": 70}')
    failing = [True]
    _refuse_legacy_inventory(monkeypatch, "file-read", failing)
    _seed(app, clock, "A", 70, week_reset=WEEK)   # establishes the cutover
    cutovers = [k for k in _journal_kinds() if k == P.CUTOVER_OP_KIND]
    assert len(cutovers) == 1
    boundary = jr.find_account_bound_cutover()
    assert boundary.legacy_spool_baseline_digest is None
    assert boundary.legacy_spool_inventory_failure["operation"] == "read"
    assert not core.LEGACY_SPOOL_BASELINE_PATH.exists()
    _drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK,
           1 + 2 * DEADLINE // TICK)
    assert _spool_activity_ops() == [], "the cutover IS the trigger"
    assert not _published("A", 20)
    assert _weekly(app, "A") == 70
    for step in ("ingest", "rebuild", "rederive"):
        if step == "rebuild":
            T.rebuild(app)
        elif step == "rederive":
            T.retain_session_input(app, "A")
            T.rederive(app)
        _assert_markers(app, ["inventory-incomplete"], step)
        assert T.quarantine_report(app)["inventoryEpisode"]["state"] == "open"
    _drive(app, clock, "A", (("one", 80), ("two", 80)), WEEK, 2)
    assert _weekly(app, "A") == 80
    failing[0] = False
    _drive(app, clock, "A", (("one", 80), ("two", 80)), WEEK, 1)
    # Fix round K12 (K11R-4, spec sections 5.5 step 6, 5.6 item 1(b) and 7):
    # the first complete inventory after an uncertain cutover ends the
    # episode and anchors a complete baseline under its own op. The cutover
    # anchored no baseline, so nothing is "unavailable" and nothing changed
    # against one: the op is the episode's own anchor, never a legacy-activity
    # cause, and it clears nothing.
    assert [op["payload"]["reason"] for op in _spool_activity_ops()] == [
        "inventory-complete"]
    assert "digest" in _spool_activity_ops()[0]["payload"]
    assert jr.read_anchored_legacy_spool_baseline() is not None
    for step in ("ingest", "rebuild"):
        if step == "rebuild":
            T.rebuild(app)
        assert _marker_reasons(app) == ["inventory-incomplete",
                                        "inventory-complete"], step
        assert T.quarantine_report(app)["inventoryEpisode"]["state"] == \
            "closed", step
        assert _paused(app, "A"), step
    # A later genuine legacy change is still told apart: it anchors against
    # the complete baseline as legacy activity.
    _write_legacy_candidate('{"sevenDay": 71}')
    _drive(app, clock, "A", (("one", 80), ("two", 80)), WEEK, 1)
    assert [op["payload"]["reason"] for op in _spool_activity_ops()] == [
        "inventory-complete", "changed"]
    assert T.quarantine_report(app)["cause"] == "legacy-spool-activity"


def test_a_the_stale_establishment_loser_installs_nothing(app, monkeypatch):
    import _cctally_core as core
    import _cctally_journal as jr
    clock = Clock(app, monkeypatch, T0)
    _seed(app, clock, "A", 70, week_reset=WEEK)   # the winner
    baseline = core.LEGACY_SPOOL_BASELINE_PATH.read_bytes()
    _write_legacy_candidate()
    jr.ensure_account_bound_cutover()             # the stale loser resumes
    assert core.LEGACY_SPOOL_BASELINE_PATH.read_bytes() == baseline
    _drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK, 6)
    assert _spool_activity_ops()
    assert not _published("A", 20)


def _journal_kinds():
    """Every record's (type, payload kind or publication flag) in journal
    order."""
    import _cctally_journal as jr
    import _lib_journal as J
    import _cctally_core as core
    out = []
    for segment in jr.list_segments():
        for line in (core.JOURNAL_DIR / segment).read_bytes().splitlines():
            rec = J.decode_line(line)
            if rec is None:
                continue
            payload = rec.get("payload") or {}
            if rec.get("t") == "obs" and "publication_id" in payload:
                out.append("publication")
            else:
                out.append(payload.get("kind") or rec.get("t"))
    return out


def test_a_two_producer_race_with_a_legacy_write_before_the_loser_resumes(
        app, monkeypatch):
    """The full F3/F15(a) race (#825/#832 Task 4 gap fill): two current
    producers race the locked cutover establishment — both finish the unlocked
    prefix scan before either takes the lock — the winner establishes and
    installs its journal-anchored baseline, a frozen legacy render writes a
    candidate, and only then does the stale loser resume. The loser installs
    nothing and returns the winner's boundary; the legacy write is journaled as
    `legacy_spool_activity` before any current publication, every account is
    quarantined, and current 20 candidates refreshed one second apart never
    publish their drop by consensus."""
    import threading
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_account_protocol as P
    import _lib_legacy_spool as LS
    clock = Clock(app, monkeypatch, T0)
    both_scanned = threading.Barrier(2, timeout=10)
    legacy_written = threading.Event()
    results, errors = {}, []

    def seam(step):
        if step != "after-scan":
            return
        both_scanned.wait()
        if threading.current_thread().name == "loser":
            assert legacy_written.wait(timeout=20)

    monkeypatch.setattr(jr, "_ACCOUNT_BOUND_CUTOVER_SEAM", seam)

    def run():
        try:
            results[threading.current_thread().name] = (
                jr.ensure_account_bound_cutover())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    winner = threading.Thread(target=run, name="winner")
    loser = threading.Thread(target=run, name="loser")
    winner.start()
    loser.start()
    winner.join(timeout=30)
    assert not winner.is_alive()
    installed = core.LEGACY_SPOOL_BASELINE_PATH.read_bytes()
    _write_legacy_candidate('{"sevenDay": 70}')
    legacy_written.set()
    loser.join(timeout=30)
    assert not loser.is_alive()
    monkeypatch.setattr(jr, "_ACCOUNT_BOUND_CUTOVER_SEAM", None)
    assert errors == []
    assert results["winner"] == results["loser"]
    assert _journal_kinds().count(P.CUTOVER_OP_KIND) == 1
    assert core.LEGACY_SPOOL_BASELINE_PATH.read_bytes() == installed
    baseline = jr.read_anchored_legacy_spool_baseline()
    assert baseline is not None, "the winner's baseline is journal-anchored"
    assert baseline.digest() == results["winner"].legacy_spool_baseline_digest
    assert LS.diff(baseline, jr.capture_legacy_spool_state())

    _seed(app, clock, "A", 70, week_reset=WEEK)
    for _ in range(6):
        _drive(app, clock, "A", (("one", 20),), WEEK, 1)
        clock.advance(-TICK + 1)
        _drive(app, clock, "A", (("two", 20),), WEEK, 1)
        clock.advance(-TICK + 1)
    kinds = _journal_kinds()
    assert P.LEGACY_SPOOL_ACTIVITY_KIND in kinds
    first_trigger = kinds.index(P.LEGACY_SPOOL_ACTIVITY_KIND)
    assert "publication" not in kinds[:first_trigger], kinds
    for name in ("A", "B", "unattributed"):
        assert _paused(app, name), name
    assert not _published("A", 20)
    assert _weekly(app, "A") == 70


def _authoritative(app, clock, name, weekly, week_reset, five, five_reset):
    import _cctally_statusline as sl
    T.identity(name)
    args = argparse.Namespace(
        percent=weekly, resets_at=str(week_reset), five_hour_percent=five,
        five_hour_resets_at=str(five_reset), source="api")
    # The OAuth path binds the selected credential's evidence (spec 4.6).
    result = sl._authoritative_record_usage(
        args, ("sevenDay", "fiveHour"),
        binding=sl._capture_binding(credential=f"token-{name}"),
        nudge_dashboard=False)
    assert result.status == "ok", result


def _excluded_trigger(app, clock, week_reset, five_reset):
    """A post-cutover reading of the previous release (an excluded
    observation), ingested: a trigger that leaves no spool marker."""
    T.legacy(app, None, at=T.iso(clock.value), weekly=50,
             week_end=T.iso(week_reset), five=60, five_end=T.iso(five_reset))
    T.ingest(app)


def test_c_d_fresh_readings_alone_are_not_enough_and_oauth_is_per_account(
        app, monkeypatch):
    """(c) and (d) over an excluded-observation trigger: fresh first entries
    release nothing without completed authority, so B's drop stays withheld;
    A's bound authoritative publication covers A only, and A's fresh first
    entries then release both of A's axes, so A's drop publishes."""
    clock = Clock(app, monkeypatch, T0)
    _seed(app, clock, "A", 30, week_reset=T0 + 86400, five=10,
          five_reset=T0 + 3600)
    _seed(app, clock, "B", 30, week_reset=T0 + 86400, five=10,
          five_reset=T0 + 3600)
    _excluded_trigger(app, clock, T0 + 86400, T0 + 3600)
    assert not _spool_activity_ops()
    for name in ("A", "B"):
        assert _paused(app, name), name
    clock.advance(TICK)
    # (d) A bound two-axis authoritative publication for A only.
    _authoritative(app, clock, "A", 31, T0 + 86400, 11, T0 + 3600)
    [completion] = T.ops_of_kind("outcome_authority_completion")
    assert completion["payload"]["account"] == _key("A")
    assert _paused(app, "A"), "coverage alone releases nothing"
    # An idle gap spanning the weekly reset and the five-hour window end.
    clock.advance(86400 + 3600)
    new_week = clock.value + 7 * 86400
    for name in ("A", "B"):
        _seed(app, clock, name, 70, week_reset=new_week, five=10,
              five_reset=clock.value + 3 * 3600, source="statusline")
    assert T.axis_allowed(app, "A") == {"sevenDay": True, "fiveHour": True}
    # (c) B's fresh readings release nothing without authority.
    assert _paused(app, "B")
    clock.advance(TICK)
    _drive(app, clock, "A", (("one", 20), ("two", 20)), new_week, 6)
    assert _published("A", 20)
    _drive(app, clock, "B", (("three", 20), ("four", 20)), new_week, 6)
    assert not _published("B", 20)


def _assert_withholds(app, clock, week_reset, step, names=("A", "B")):
    for name in names:
        assert _paused(app, name), (step, name)
    # The drop stays withheld: neither consensus nor its deadline publishes.
    _drive(app, clock, "A", (("one", 20), ("two", 20)), week_reset,
           1 + 2 * DEADLINE // TICK)
    assert not _published("A", 20), step


def test_f_the_whole_barrier_resumes_drops_after_spool_activity(
        app, monkeypatch):
    """(f) After a `legacy_spool_activity` trigger, fresh two-axis readings
    alone and an OAuth reconciliation alone leave the drops withheld; once
    the cohort has expired, a completed OAuth reconciliation covered A and
    A's next fresh period produced a witness and its release, A's drop
    publishes — and B, never covered, stays paused. The state is the same
    after ingest, rebuild and rederive."""
    clock = Clock(app, monkeypatch, T0)
    for name in ("A", "B", "unattributed"):
        T.retain_session_input(app, name)
    _seed(app, clock, "A", 30, week_reset=T0 + 86400, five=10,
          five_reset=T0 + 3600)
    _seed(app, clock, "B", 30, week_reset=T0 + 86400, five=10,
          five_reset=T0 + 3600)
    _write_legacy_candidate()
    _drive(app, clock, "A", (("one", 30),), T0 + 86400, 1)
    assert _spool_activity_ops()
    clock.advance(86400 + 3600)
    new_week = clock.value + 7 * 86400
    for name in ("A", "B"):
        _seed(app, clock, name, 70, week_reset=new_week, five=10,
              five_reset=clock.value + 3 * 3600, source="statusline")
        # Ordinary recording continues; the fresh reading releases nothing.
        assert _weekly(app, name) == 70, name
    clock.advance(TICK)
    _assert_withholds(app, clock, new_week, "after fresh readings")
    # A completed OAuth reconciliation for A after expiry covers A.
    _authoritative(app, clock, "A", 70, new_week, 11, clock.value + 3 * 3600)
    assert [op["payload"]["account"]
            for op in T.ops_of_kind("outcome_authority_completion")] == [_key("A")]
    clock.advance(TICK)
    _assert_withholds(app, clock, new_week, "after OAuth")
    # A's next fresh period: the first entry of the following week.
    clock.value = new_week + 60
    clock._pin()
    third_week = new_week + 7 * 86400
    _seed(app, clock, "A", 40, week_reset=third_week, five=12,
          five_reset=clock.value + 3 * 3600, source="statusline")
    assert T.axis_allowed(app, "A") == {"sevenDay": True, "fiveHour": True}
    assert _paused(app, "B")
    result = T.continue_through_maintenance(app, monkeypatch)
    assert result["live_state"] == result["rebuilt_state"] == (
        result["rederived_state"])
    assert T.axis_allowed(app, "A") == {"sevenDay": True, "fiveHour": True}
    clock.advance(TICK)
    _drive(app, clock, "A", (("one", 20), ("two", 20)), third_week,
           1 + 2 * DEADLINE // TICK)
    assert _published("A", 20)


@pytest.mark.parametrize("sidecar", ["missing", "stale-closed", "stale-open"])
def test_f_an_unchanged_complete_inventory_closes_the_episode(
        app, monkeypatch, sidecar):
    """#948 spec 6.3: the inventory pass decides from the JOURNAL whether an
    episode is open. When reads recover and the spool is unchanged against
    its anchored baseline, it appends one non-trigger
    `legacy_spool_inventory_completion` naming the episode, whatever the
    local sidecar says — missing, already closed (an upgrade over the
    previous release's sidecar), or still open."""
    clock = Clock(app, monkeypatch, T0)
    _seed(app, clock, "A", 70, week_reset=WEEK)   # establishes the cutover
    failing = [True]
    _refuse_legacy_inventory(monkeypatch, "directory-listing", failing)
    _write_legacy_candidate('{"sevenDay": 70}')
    _drive(app, clock, "A", (("one", 80),), WEEK, 1)
    [opener] = _spool_activity_ops()
    assert opener["payload"]["reason"] == "inventory-incomplete"
    generation = T.account_status(app, "A")["generation"]
    failing[0] = False
    # The first complete scan sees the candidate: `changed` closes the
    # episode and is itself a trigger.
    _drive(app, clock, "A", (("one", 80),), WEEK, 1)
    assert _marker_reasons(app) == ["inventory-incomplete", "changed"]
    # A second failure episode, then an UNCHANGED complete scan.
    failing[0] = True
    _drive(app, clock, "A", (("one", 81),), WEEK, 1)
    second = _spool_activity_ops()[-1]
    assert second["payload"]["reason"] == "inventory-incomplete"
    generation = T.account_status(app, "A")["generation"]
    import _cctally_journal as jr
    episode = jr.legacy_spool_anchor_state().uncertain
    assert episode is not None
    sidecar_path = jr._inventory_episode_path()
    if sidecar == "missing":
        sidecar_path.unlink(missing_ok=True)
    elif sidecar == "stale-closed":
        # The previous release closed the episode in its sidecar only.
        jr.close_legacy_spool_inventory_episode(episode)
        assert not jr.legacy_spool_anchor_state().episode_open
    failing[0] = False
    _drive(app, clock, "A", (("one", 81),), WEEK, 1)
    completions = T.ops_of_kind("legacy_spool_inventory_completion")
    assert len(completions) == 1, completions
    report = T.quarantine_report(app)
    assert report["inventoryEpisode"]["state"] == "closed"
    assert T.account_status(app, "A")["generation"] == generation, (
        "the completion op is no trigger")
    _drive(app, clock, "A", (("one", 81),), WEEK, 1)
    assert len(T.ops_of_kind("legacy_spool_inventory_completion")) == 1, (
        "one op per episode")
    # Review finding F18, the positive endpoint: with the episode closed the
    # rest of the barrier recovers A. Once the cohort expired, a completed
    # OAuth reconciliation covers A; the first entry of the first week that
    # starts after the cohort's bound (U7.end) witnesses and releases
    # weekly, and A's genuine drop then publishes.
    clock.advance(DEADLINE)
    _authoritative(app, clock, "A", 81, WEEK, 11, clock.value + 3600)
    assert [op["payload"]["account"] for op in T.ops_of_kind(
        "outcome_authority_completion")] == [_key("A")]
    assert T.ops_of_kind("outcome_axis_release") == []
    clock.value = WEEK + 7 * 86400 + 60
    clock._pin()
    fresh_week = WEEK + 14 * 86400
    _seed(app, clock, "A", 40, week_reset=fresh_week, source="statusline")
    assert [op["payload"]["axis"] for op in T.ops_of_kind(
        "outcome_axis_release")] == ["sevenDay"]
    assert T.axis_allowed(app, "A")["sevenDay"] is True
    clock.advance(TICK)
    _drive(app, clock, "A", (("one", 10), ("two", 10)), fresh_week,
           1 + 2 * DEADLINE // TICK)
    assert _published("A", 10)


def test_t8_a_no_trigger_render_loop_appends_no_recovery_op(app, monkeypatch):
    """Review finding F17 (spec 6.12, T8 on the reducer path): with no
    trigger, a long loop of renders through the real reducer — unchanged
    frames and changed values alike — appends no recovery op and no op of a
    kind the store did not already hold. A per-render or unbounded journal
    write on the statusline path fails here."""
    clock = Clock(app, monkeypatch, T0)
    _seed(app, clock, "A", 30, week_reset=WEEK)   # establishes the cutover
    before = {rec["payload"].get("kind") for rec in T.journal_records()
              if rec.get("t") == "op"}
    for i in range(24):
        percent = 30 + i // 6
        _drive(app, clock, "A", (("one", percent), ("two", percent)), WEEK, 1)
    kinds = {rec["payload"].get("kind") for rec in T.journal_records()
             if rec.get("t") == "op"}
    assert not kinds & {"legacy_spool_activity",
                        "legacy_spool_inventory_completion",
                        "outcome_authority_completion",
                        "outcome_axis_release"}, kinds
    assert kinds <= before, kinds - before
    assert _weekly(app, "A") == 33, "non-vacuity: the changed values published"


def test_e_expiry_an_empty_spool_and_an_inflight_tombstone_are_not_recovery(
        app, monkeypatch):
    import _cctally_statusline as sl
    import _lib_statusline_candidates as C
    clock = Clock(app, monkeypatch, T0)
    _seed(app, clock, "A", 70, week_reset=WEEK)
    _write_legacy_candidate()
    _drive(app, clock, "A", (("one", 20), ("two", 70)), WEEK, 3)
    assert _pending(app, clock, "A") is not None
    # Every supporter ages out (an empty spool) and the deadline passes: the
    # retained low would publish at expiry, but it stays withheld.
    clock.advance(2 * DEADLINE)
    sl._statusline_reduce_and_publish(nudge_sink=lambda: None)
    assert not _published("A", 20)
    assert _paused(app, "A")
    # An inflight tombstone never expires into recovery either.
    window = _pending(app, clock, "A").canonical_key
    sl._write_tombstone("sevenDay", C.Tombstone(
        axis="sevenDay", state="inflight", started_at=clock.value,
        prior_block_received_at_through=None), account_key=_key("A"),
        window=window)
    clock.advance(DEADLINE)
    _drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK, 3)
    assert not _published("A", 20)
    assert _paused(app, "A")


def test_a_plan_frozen_before_a_trigger_does_not_publish_after_it(app, monkeypatch):
    """Crash recovery of a frozen drop plan is gated like a fresh one: a
    trigger that lands between the freeze and the recovery withholds it."""
    import _cctally_statusline as sl
    clock = Clock(app, monkeypatch, T0)
    _seed(app, clock, "A", 70, week_reset=WEEK)
    real = app.cmd_record_usage
    crashed = []

    def crash_once(*args, **kwargs):
        if kwargs.get("publication") is not None and not crashed:
            crashed.append(kwargs["publication"])
            raise RuntimeError("killed after the plan was frozen")
        return real(*args, **kwargs)

    monkeypatch.setattr(app, "cmd_record_usage", crash_once)
    for _ in range(6):
        _drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK, 1)
        if crashed:
            break
    assert crashed, "a drop plan was frozen"
    transition = sl._read_transition(_key("A"), now_epoch=clock.value)
    assert transition.publication is not None
    assert transition.publication["sevenDay"]["percent"] == 20
    _write_legacy_candidate()
    import _cctally_journal as jr
    jr.append_legacy_spool_activity(
        changed=("statusline-candidates/legacy-session.json",),
        new_state=jr.capture_legacy_spool_state(), reason="changed")
    sl._statusline_reduce_and_publish(nudge_sink=lambda: None)
    assert not _published("A", 20)
    after = sl._read_transition(_key("A"), now_epoch=clock.value)
    assert after.publication is None
    assert after.quarantine["dropWithheld"] is True


_TWO_KEY = {"authorityCommit": None, "dropWithheld": True}


@pytest.mark.parametrize("doc, withheld", [
    ({"authorityCommit": None, "dropWithheld": False}, frozenset()),
    (_TWO_KEY, frozenset({"sevenDay", "fiveHour"})),
    (dict(_TWO_KEY, dropWithheldAxes=["sevenDay"]), frozenset({"sevenDay"})),
    (dict(_TWO_KEY, dropWithheldAxes=["fiveHour"]), frozenset({"fiveHour"})),
], ids=["none", "both-two-key-form", "weekly-only", "five-hour-only"])
def test_the_frozen_transition_withholds_drops_per_axis(doc, withheld):
    """Review finding F1: the render path reduces each axis with the
    withheld set the frozen transition records. A one-axis document
    round-trips with its axis; a set flag naming none — every document an
    earlier build wrote — withholds both, the conservative reading."""
    import types
    import _cctally_statusline as sl
    validated = sl._validate_transition_quarantine(doc)
    assert sl._withheld_axes(types.SimpleNamespace(quarantine=validated)) == withheld
    assert sl._quarantine_document(None, withheld) == dict(doc, authorityCommit=None)


@pytest.mark.parametrize("axes, flag", [
    (["sevenDay", "fiveHour"], True), (["fiveHour", "sevenDay"], True),
    ([], True), (["sevenDay"], False), (["daily"], True), ("sevenDay", True),
])
def test_an_invalid_per_axis_withheld_document_is_refused(axes, flag):
    """A malformed control document fails closed (it is never read as some
    other permission): only a proper, sorted, known subset with the flag set
    is valid."""
    import _cctally_statusline as sl
    import _lib_statusline_candidates as C
    with pytest.raises(C.StateValidationError):
        sl._validate_transition_quarantine(
            {"authorityCommit": None, "dropWithheld": flag,
             "dropWithheldAxes": axes})
