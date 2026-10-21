"""#825/#832 Task 3.6 — drop-consensus withholding and the control certificate,
case F15 (spec 5.6 items 1(b) and 7).

(a) Spool-only legacy activity — a frozen render writing a legacy candidate and
    no journal observation — is journaled as `legacy_spool_activity` before the
    next current publication and quarantines every account, including the
    round-6 interleaving inside the cutover establishment and the stale loser
    of the establishment race.
(b) During control quarantine a pending drop below the database baseline is
    published neither by consensus nor at its deadline; a rise publishes.
(c) Accounting recovery without an OAuth publication leaves drop consensus
    withheld.
(d) A bound two-axis authoritative publication for A restores A's control
    certificate and not B's.
(e) Retained-low expiry, an empty spool and a non-expiring inflight tombstone
    never count as recovery.
(f) Spec revision 3: after spool-only activity the unresolved marker keeps
    accounting uncertified and drops withheld through the full recovery
    schedule, ingest, rebuild, rederive and doctor; (c) and (d) run over a
    history whose trigger leaves no marker.
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
        status = _status(app, name)
        assert not status["accounting"] and not status["control"], name
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
    import _cctally_quarantine as G
    conn = app.open_db()
    try:
        report = G.report(conn)
    finally:
        conn.close()
    return [m["reason"] for m in report.get("unresolvedSpoolMarkers") or []]


def _assert_markers(app, expected, step):
    assert _marker_reasons(app) == expected, step
    for name in ("A", "B", "unattributed"):
        status = _status(app, name)
        assert not status["accounting"] and not status["control"], (
            step, name)


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


#: Operator-approved revision 5 doctor wording (spec section 8), verbatim.
INVENTORY_MARKER_TEXT = (
    "Automatic credit, reset and status-line drop detection is paused for "
    "{account} since {time} because cctally could not read the status-line "
    "files an older cctally release used, so it cannot rule out that an "
    "older process changed them. This version of cctally cannot yet confirm "
    "a safe point to resume, so the pause does not end on its own; usage "
    "recording continues. If your usage was reset in place meanwhile, record "
    "it with `cctally record-credit`.")


def test_a_pre_cutover_incomplete_inventory_is_the_cutovers_own_trigger(
        app, monkeypatch):
    """Spec revision 5, section 7 *Before the initial cutover*: the legacy
    spool cannot be read when the first current producer establishes the
    cutover. The cutover records the uncertainty in itself — no separate op,
    no baseline — and that is the unresolved-marker trigger: drops stay
    withheld and doctor words the marker as an unreadable inventory, through
    ingest, rebuild and rederive, while recording continues. The next
    complete inventory anchors a baseline under its own op."""
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
        rows = _doctor_rows(app)
        row = rows[_key("A")]
        assert row["unresolvedSpoolMarker"], row
        assert row["message"] == INVENTORY_MARKER_TEXT.format(
            account=row["accountLabel"], time=row["accountingSince"]), row
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
        row = _doctor_rows(app)[_key("A")]
        assert row["unresolvedSpoolMarker"], (step, row)
        assert row["message"] == INVENTORY_MARKER_TEXT.format(
            account=row["accountLabel"], time=row["accountingSince"]), (
            step, row)
    # A later genuine legacy change is still told apart: it anchors against
    # the complete baseline and words the marker as legacy activity.
    _write_legacy_candidate('{"sevenDay": 71}')
    _drive(app, clock, "A", (("one", 80), ("two", 80)), WEEK, 1)
    assert [op["payload"]["reason"] for op in _spool_activity_ops()] == [
        "inventory-complete", "changed"]
    row = _doctor_rows(app)[_key("A")]
    assert row["message"] != INVENTORY_MARKER_TEXT.format(
        account=row["accountLabel"], time=row["accountingSince"]), row


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
        status = _status(app, name)
        assert not status["accounting"] and not status["control"], name
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


def test_c_d_accounting_recovery_alone_is_not_enough_and_oauth_is_per_account(
        app, monkeypatch):
    """(c) and (d) over a history WITHOUT an unresolved spool marker (spec
    revision 3, F15 (f)): the trigger is an excluded observation, so the
    idle-gap readings certify accounting."""
    clock = Clock(app, monkeypatch, T0)
    _seed(app, clock, "A", 30, week_reset=T0 + 86400, five=10,
          five_reset=T0 + 3600)
    _seed(app, clock, "B", 30, week_reset=T0 + 86400, five=10,
          five_reset=T0 + 3600)
    _excluded_trigger(app, clock, T0 + 86400, T0 + 3600)
    assert not _spool_activity_ops()
    for name in ("A", "B"):
        assert not _status(app, name)["accounting"], name
    # An idle gap spanning the weekly reset and the five-hour window end.
    clock.advance(86400 + 3600)
    new_week = clock.value + 7 * 86400
    for name in ("A", "B"):
        _seed(app, clock, name, 70, week_reset=new_week, five=10,
              five_reset=clock.value + 3 * 3600, source="statusline")
        assert _status(app, name)["accounting"], name
        assert not _status(app, name)["control"], name
    clock.advance(TICK)
    # (c) Accounting recovery without an OAuth publication: withheld.
    _drive(app, clock, "A", (("one", 20), ("two", 20)), new_week, 6)
    assert not _published("A", 20)
    # (d) A bound two-axis authoritative publication for A only.
    _authoritative(app, clock, "A", 70, new_week, 11, clock.value + 3 * 3600)
    assert _status(app, "A")["control"]
    assert not _status(app, "B")["control"]
    clock.advance(TICK)
    _drive(app, clock, "A", (("one", 20), ("two", 20)), new_week, 6)
    assert _published("A", 20)
    _drive(app, clock, "B", (("three", 20), ("four", 20)), new_week, 6)
    assert not _published("B", 20)


#: Operator-approved revision 3 doctor wording (spec section 8), verbatim.
MARKER_TEXT = (
    "Automatic credit, reset and status-line drop detection is paused for "
    "{account} since {time} because an older cctally process wrote "
    "status-line data after the upgrade. This version of cctally cannot yet "
    "confirm a safe point to resume, so the pause does not end on its own; "
    "usage recording continues. If your usage was reset in place meanwhile, "
    "record it with `cctally record-credit`.")


def _doctor_rows(app):
    import dataclasses
    import _cctally_doctor
    import _lib_doctor
    kwargs = {f.name: None for f in dataclasses.fields(_lib_doctor.DoctorState)}
    kwargs.update(outcome_quarantine=_cctally_doctor._gather_outcome_quarantine(),
                  now_utc=dt.datetime.now(dt.timezone.utc))
    result = _lib_doctor._check_accounting_outcome_quarantine(
        _lib_doctor.DoctorState(**kwargs))
    assert result.severity == "warn", result
    return {row["account"]: row for row in result.details["accounts"]}


def _assert_marker_withholds(app, clock, week_reset, step):
    for name in ("A", "B"):
        assert not _status(app, name)["accounting"], (step, name)
    rows = _doctor_rows(app)
    for name in ("A", "B"):
        row = rows[_key(name)]
        assert row["unresolvedSpoolMarker"] and row["accountingPaused"], row
        assert row["message"] == MARKER_TEXT.format(
            account=row["accountLabel"], time=row["accountingSince"]), row
    # The drop stays withheld: neither consensus nor its deadline publishes.
    _drive(app, clock, "A", (("one", 20), ("two", 20)), week_reset,
           1 + 2 * DEADLINE // TICK)
    assert not _published("A", 20), step


def test_f_the_spool_marker_persists_through_the_full_recovery_schedule(
        app, monkeypatch):
    """(f) Spec revision 3 (finding CD3-O01): after a `legacy_spool_activity`
    trigger, the full recovery schedule — an idle gap spanning a weekly reset
    and the end of the five-hour window, fresh two-axis readings and a
    completed OAuth reconciliation — leaves accounting uncertified and drops
    withheld, through ingest, rebuild and rederive, and doctor keeps
    reporting it. The control certificate still reconciles independently and
    ordinary recording continues."""
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
        # Ordinary recording continues; the fresh reading never certifies.
        assert _weekly(app, name) == 70, name
        assert not _status(app, name)["accounting"], name
    clock.advance(TICK)
    _assert_marker_withholds(app, clock, new_week, "after fresh readings")
    # A completed OAuth reconciliation for A: its control certificate
    # reconciles (as in (d)); accounting, and so drop consensus, stays paused.
    _authoritative(app, clock, "A", 70, new_week, 11, clock.value + 3 * 3600)
    assert _status(app, "A")["control"]
    assert not _status(app, "B")["control"]
    clock.advance(TICK)
    _assert_marker_withholds(app, clock, new_week, "after OAuth")
    T.rebuild(app)
    _assert_marker_withholds(app, clock, new_week, "after rebuild")
    T.rederive(app)
    _assert_marker_withholds(app, clock, new_week, "after rederive")
    assert _weekly(app, "A") == 70


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
    assert not _status(app, "A")["control"]
    # An inflight tombstone never expires into recovery either.
    window = _pending(app, clock, "A").canonical_key
    sl._write_tombstone("sevenDay", C.Tombstone(
        axis="sevenDay", state="inflight", started_at=clock.value,
        prior_block_received_at_through=None), account_key=_key("A"),
        window=window)
    clock.advance(DEADLINE)
    _drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK, 3)
    assert not _published("A", 20)
    assert not _status(app, "A")["control"]


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
