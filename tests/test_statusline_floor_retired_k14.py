"""#825/#832 fix round K14a: a reset or credit floor that retires every
eligible row of an axis, on an install whose ONLY producer is the statusline.

Spec 4.3 revision 5 (*Classification evidence*) calls an axis unknown when
"filtering ... leaves [its emptiness] unexplained", and fix round K13 made
unknown evidence defer the WHOLE account evaluation under either drop gate.
C3 says a statusline-only install (no hooks, no manual command) keeps
recording; the only admitted delays are a rebuild and residual 10.

The suspected deadlock: the record pipeline's dedup
(`_cctally_journal._usage_snapshot_fold_decision`) compares a tick against
the account's LATEST row with no floor filter, so a first post-floor reading
equal to a surviving pre-floor row is `skip_no_change` and writes no
post-floor row. The statusline projection
(`_cctally_statusline._read_db_projection_once`) then keeps the axis's group
but no row at or after the floor, so the axis is `unknown`, the reducer
defers every later evaluation behind that head, installs an evidence wait
whose only exits are dependency-token changes, and on a statusline-only
install nothing else can ever write the row that would make the axis
classifiable.

Every scenario below builds its setup state, then lets the statusline render
path (`_statusline_persist`, the real capture and the real reducer worker)
and extra reducer passes be the ONLY producers, and asserts that ordinary
later readings are published. A RED run on fix round K13's head
(`6b56b8e88`) is the proof that the scenario is real.

- S1  five-hour in-place credit whose post-credit value equals the latest
      surviving pre-floor row (70 -> 20 -> 20, then 80 -> 20 -> 20), set up
      through `record_usage_bound` and, separately, through statusline
      renders alone.
- S2  S1, then the five-hour window expires (and then the week ends).
- S3  a weekly floor from the real `record-credit --yes` command.
- S4  a weekly floor from the automatic detector (>=25 pp in place, and the
      debounced reset-to-zero).
- S5  `test_new_own_event_reopens_spent_retry_but_foreign_event_does_not`
      with the values fix round K13 changed from 20 to 30 restored.
- S6  render cost: forks while an account's head waits on unchanged
      evidence.

Status at `6b56b8e88` (log `fix-k14a-red.log` under
`.local/reports/825-832-account-bound/red/`): S1 (both setups), S2 (both
nodes) and S5 are RED; S3, S4 and S6 are GREEN (refuted), as is the S1
control, which shows that one reading from another producer is the only exit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

import pytest

from conftest import load_script, redirect_paths

NOW = 1791417600
#: A five-hour window that stays open for four hours after NOW, so the
#: in-window stages (including one past the wait's longest retry delay, 3,600
#: seconds) never touch expiry.
FIVE_RESET = NOW + 4 * 3600
WEEK_RESET = NOW + 3 * 86400
NEXT_FIVE_RESET = FIVE_RESET + 5 * 3600
NEXT_WEEK_RESET = WEEK_RESET + 7 * 86400
#: Passes the test grants the reducer after each render (the render itself
#: already ran the worker); "bounded" means at most this many.
PASSES = 3


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _tick(monkeypatch, at):
    monkeypatch.setattr(time, "time", lambda: float(NOW + at))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(NOW + at))


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _tick(monkeypatch, 0)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    cctally.open_db().close()
    return cctally


@pytest.fixture
def folds(monkeypatch):
    """Every fold decision the record pipeline makes, as
    ``(weekly, five_hour, snapshot_action, five_hour_disposition)``: the
    mechanism named in each failure message."""
    import _cctally_journal as jr
    seen = []
    real = jr._usage_snapshot_fold_decision

    def spy(conn, payload, **kwargs):
        result = real(conn, payload, **kwargs)
        seen.append((payload["weekly_percent"], payload["five_hour_percent"],
                     result.snapshot_action, result.five_hour.disposition))
        return result

    monkeypatch.setattr(jr, "_usage_snapshot_fold_decision", spy)
    return seen


def _identity(name="A"):
    import _cctally_core as core
    import _lib_accounts
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": name, "emailAddress": f"{name}@example.test",
        "plan": "max"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _lib_accounts.account_key("claude", name)


def _record(app, monkeypatch, at, *, weekly, five, five_reset=FIVE_RESET,
            week_reset=WEEK_RESET):
    """SETUP ONLY: one bound `record-usage` reading (a producer other than
    the statusline reducer)."""
    import _cctally_statusline as sl
    _tick(monkeypatch, at)
    _identity()
    binding = sl._capture_binding()
    args = argparse.Namespace(
        percent=weekly, resets_at=str(week_reset), five_hour_percent=five,
        five_hour_resets_at=str(five_reset), source="statusline")
    assert app.record_usage_bound(args, binding=binding, nudge_dashboard=False) == 0
    return binding.account_key


def _parsed(app, *, weekly, five, five_reset=FIVE_RESET, week_reset=WEEK_RESET):
    return app._lib_statusline.parse_statusline_stdin(json.dumps({
        "session_id": "peer", "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": _iso(five_reset)},
            "seven_day": {"used_percentage": weekly, "resets_at": _iso(week_reset)},
        }}).encode())


def _render(app, monkeypatch, at, *, sync=True, **values):
    """One real statusline tick through the render entry point: capture,
    the file-only fork decision and (``sync``) the reducer worker the
    detached child would run."""
    import _cctally_statusline as sl
    _tick(monkeypatch, at)
    _identity()
    sl._statusline_persist(_parsed(app, **values), sync_for_test=sync)


def _pass():
    import _cctally_statusline as sl
    return sl._statusline_reduce_and_publish(nudge_sink=lambda: None)


def _rows(app):
    conn = app.open_db()
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT captured_at_utc, weekly_percent, five_hour_percent, "
            "weekly_observation_held, source FROM weekly_usage_snapshots "
            "ORDER BY id")]
    finally:
        conn.close()


def _published(app, weekly, five):
    """Whether accounting holds a row carrying exactly this reading."""
    return any(row[1] == float(weekly) and row[2] == float(five)
               for row in _rows(app))


def _diagnosis(app, account, at, folds, results=()):
    """The mechanism, for a failure message: both axes' classification
    evidence at ``at``, the wait descriptor, the last pass results, the
    stored rows, the five-hour reset events and the fold decisions."""
    import _cctally_statusline as sl
    try:
        evidence = sl._read_db_projection_once(
            account_key=account, at=NOW + at).evidence
        evidence = {axis: item.kind for axis, item in evidence.items()}
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        evidence = f"unreadable: {exc!r}"
    conn = app.open_db()
    try:
        events = [tuple(r) for r in conn.execute(
            "SELECT five_hour_window_key, effective_reset_at_utc "
            "FROM five_hour_reset_events ORDER BY id")]
        weekly_floors = [tuple(r) for r in conn.execute(
            "SELECT effective_reset_at_utc FROM week_reset_events ORDER BY id")]
        credit_floors = [tuple(r) for r in conn.execute(
            "SELECT effective_at_utc FROM weekly_credit_floors ORDER BY id")]
    finally:
        conn.close()
    wait = sl._read_wait(account)
    if wait is not None:
        wait = {k: wait[k] for k in ("head", "reason", "class", "retry")}
    return (f"\n  evidence@{at}: {evidence}\n  wait: {wait}"
            f"\n  last pass: {[(r.status, r.reason) for r in results]}"
            f"\n  rows: {_rows(app)}\n  5h reset events: {events}"
            f"\n  week reset events: {weekly_floors}"
            f"\n  credit floors: {credit_floors}\n  folds: {folds}")


def _settle(app, monkeypatch, at, **values):
    """Render ``values`` at ``at``, then up to `PASSES` reducer passes;
    returns whether the reading was published and the last pass results."""
    _render(app, monkeypatch, at, **values)
    results = ()
    for _ in range(PASSES):
        if _published(app, values["weekly"], values["five"]):
            break
        results = _pass()
    return _published(app, values["weekly"], values["five"]), results


def _five_hour_equal_value_credit(app, monkeypatch):
    """S1's setup through `record-usage` (the partitions-test shape): 70 ->
    20 -> 20 fires a confirmed five-hour in-place credit, then 80 -> 20 -> 20
    fires a second one whose post-credit 20 equals the surviving pre-floor
    row. Returns the account."""
    account = _record(app, monkeypatch, 0, weekly=20, five=70)
    _record(app, monkeypatch, 2, weekly=20, five=20)
    _record(app, monkeypatch, 4, weekly=20, five=20)
    _record(app, monkeypatch, 608, weekly=20, five=80)
    _record(app, monkeypatch, 610, weekly=20, five=20)
    _record(app, monkeypatch, 612, weekly=20, five=20)
    conn = app.open_db()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM five_hour_reset_events WHERE account_key = ?",
            (account,)).fetchone()[0]
    finally:
        conn.close()
    assert events == 2, "setup: both in-place five-hour credits fired"
    return account


# ── S1 ─────────────────────────────────────────────────────────────────────


def test_s1_equal_value_five_hour_credit_keeps_statusline_recording(
        app, monkeypatch, folds):
    """S1: after the second credit, readings that differ on BOTH axes from
    everything stored (a higher five-hour percent, a changed weekly percent)
    are published by the statusline alone, at once, later in the same
    window, and after the wait's longest retry delay has elapsed."""
    account = _five_hour_equal_value_credit(app, monkeypatch)
    stages = {}
    for label, at, weekly, five in (
            ("first reading", 620, 21, 25),
            ("one minute later", 680, 22, 26),
            ("past the longest retry delay", 620 + 3600 + 60, 23, 27)):
        stages[label], results = _settle(app, monkeypatch, at,
                                         weekly=weekly, five=five)
    assert all(stages.values()), (
        f"statusline-only recording stalled: {stages}"
        + _diagnosis(app, account, 620 + 3600 + 60, folds, results))


def test_s1_statusline_only_install_reaches_and_keeps_the_stall(
        app, monkeypatch, folds):
    """S1 with NO producer but the statusline from the first reading on:
    the same two credits arrive through renders (each drop published by the
    reducer's drop consensus), then ordinary readings must still publish."""
    account = _identity()
    for at, five in ((0, 70), (2, 20), (4, 20), (100, 20), (200, 20),
                     (608, 80), (610, 20), (612, 20), (700, 20), (800, 20)):
        _render(app, monkeypatch, at, weekly=20, five=five)
        _pass()
    conn = app.open_db()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM five_hour_reset_events WHERE account_key = ?",
            (account,)).fetchone()[0]
    finally:
        conn.close()
    assert events == 2, (
        "setup: both credits fired from statusline readings alone"
        + _diagnosis(app, account, 800, folds))
    stages = {}
    for label, at, weekly, five in (
            ("next reading", 900, 21, 25),
            ("past the longest retry delay", 900 + 3600 + 60, 22, 26)):
        stages[label], results = _settle(app, monkeypatch, at,
                                         weekly=weekly, five=five)
    assert all(stages.values()), (
        f"a statusline-only install stopped recording: {stages}"
        + _diagnosis(app, account, 900 + 3600 + 60, folds, results))


def test_s1_control_another_producer_releases_the_stall(app, monkeypatch, folds):
    """Control for S1 (expected GREEN): ONE reading from another producer
    (a hook's `record-usage`) writes a post-floor row, which changes the
    wait's dependency tokens and the axis's evidence; the statusline then
    publishes again. It shows that the only exit from S1's state is a
    producer the statusline-only install does not have."""
    account = _five_hour_equal_value_credit(app, monkeypatch)
    _render(app, monkeypatch, 620, weekly=21, five=25)
    _record(app, monkeypatch, 700, weekly=22, five=26)
    published, results = _settle(app, monkeypatch, 760, weekly=23, five=27)
    assert published, "control" + _diagnosis(app, account, 760, folds, results)


# ── S2 ─────────────────────────────────────────────────────────────────────


def test_s2_publication_resumes_after_the_five_hour_window_expires(
        app, monkeypatch, folds):
    """S2: S1's state, one deferred in-window reading, then the window
    expires. A reading in the NEXT five-hour window (expired-predecessor
    evidence at its own instant) must publish, and so must a reading in the
    next week."""
    account = _five_hour_equal_value_credit(app, monkeypatch)
    _render(app, monkeypatch, 620, weekly=21, five=25)
    expired = FIVE_RESET - NOW + 600 + 1
    stages = {}
    for label, at, values in (
            ("next five-hour window", expired,
             dict(weekly=30, five=10, five_reset=NEXT_FIVE_RESET)),
            ("next window, past the longest retry delay", expired + 3600 + 60,
             dict(weekly=31, five=11, five_reset=NEXT_FIVE_RESET)),
            ("next week", WEEK_RESET - NOW + 700,
             dict(weekly=5, five=3, five_reset=WEEK_RESET + 3 * 3600,
                  week_reset=NEXT_WEEK_RESET))):
        stages[label], results = _settle(app, monkeypatch, at, **values)
    assert all(stages.values()), (
        f"the stall outlived the five-hour window: {stages}"
        + _diagnosis(app, account, WEEK_RESET - NOW + 700, folds, results))


def test_s2_the_head_is_unclassifiable_at_its_own_instant(app, monkeypatch, folds):
    """S2, mechanism: with the wait descriptor removed (so the head is
    runnable) and the five-hour window expired, the pass must publish the
    post-expiry reading. RED here means the stall is not only the wait's
    unchanged tokens: the retained head is reclassified at ITS original
    instant, where the floored axis is still unknown, and every later
    evaluation waits behind it."""
    import _cctally_statusline as sl
    account = _five_hour_equal_value_credit(app, monkeypatch)
    _render(app, monkeypatch, 620, weekly=21, five=25)
    expired = FIVE_RESET - NOW + 600 + 1
    _render(app, monkeypatch, expired, weekly=30, five=10,
            five_reset=NEXT_FIVE_RESET)
    sl._wait_path(account).unlink(missing_ok=True)
    results = _pass()
    assert _published(app, 30, 10), (
        "the head defers even when runnable"
        + _diagnosis(app, account, expired, folds, results))


# ── S3 ─────────────────────────────────────────────────────────────────────


def test_s3_manual_weekly_credit_keeps_statusline_recording(
        app, monkeypatch, folds):
    """S3: a real `record-credit --to 31 --yes` lowers the week's floor in
    place (46 -> 31). Statusline readings afterwards publish, in the same
    week and after it ends."""
    account = _record(app, monkeypatch, 0, weekly=46, five=10)
    _record(app, monkeypatch, 60, weekly=46, five=12)
    _tick(monkeypatch, 1000)
    _identity()
    args = app.build_parser().parse_args(["record-credit", "--to", "31", "--yes"])
    assert args.func(args) == 0
    stages = {}
    for label, at, values in (
            ("same week", 1100, dict(weekly=32, five=14)),
            ("same week, past the longest retry delay", 1100 + 3600 + 60,
             dict(weekly=33, five=15)),
            ("next week", WEEK_RESET - NOW + 700,
             dict(weekly=4, five=2, five_reset=WEEK_RESET + 3 * 3600,
                  week_reset=NEXT_WEEK_RESET))):
        stages[label], results = _settle(app, monkeypatch, at, **values)
    assert all(stages.values()), (
        f"recording stalled after a manual credit: {stages}"
        + _diagnosis(app, account, WEEK_RESET - NOW + 700, folds, results))


# ── S4 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shape", ["in_place_25pp", "reset_to_zero"])
def test_s4_automatic_weekly_floor_keeps_statusline_recording(
        app, monkeypatch, folds, shape):
    """S4: a same-week weekly floor from the automatic detector. The
    deadlock needs the post-floor reading to equal the latest surviving
    pre-floor row, which the automatic path cannot produce: every stored
    non-held weekly row is at or above the running maximum and a held row
    carries that maximum, so the latest row's weekly value IS the maximum,
    while the detector fires only >=25 pp below it or at <=1 % after a
    >=3 pp drop. The confirming tick therefore writes its post-floor row
    and the axis stays classifiable (expected GREEN: refuted for this
    path)."""
    account = _record(app, monkeypatch, 0, weekly=60, five=10)
    if shape == "in_place_25pp":
        _record(app, monkeypatch, 300, weekly=30, five=12)
        post = 30
    else:
        _record(app, monkeypatch, 300, weekly=0, five=12)
        _record(app, monkeypatch, 400, weekly=0, five=12)
        post = 0
    stages = {"post-floor row written": _published(app, post, 12)}
    stages["statusline reading"], results = _settle(
        app, monkeypatch, 500, weekly=post + 2, five=14)
    stages["past the longest retry delay"], results = _settle(
        app, monkeypatch, 500 + 3600 + 60, weekly=post + 3, five=15)
    assert all(stages.values()), (
        f"recording stalled after an automatic weekly floor: {stages}"
        + _diagnosis(app, account, 500 + 3600 + 60, folds, results))


# ── S5 ─────────────────────────────────────────────────────────────────────


def test_s5_original_values_reopen_spent_retry_but_foreign_event_does_not(
        app, monkeypatch, folds):
    """S5: `test_statusline_account_partitions.py::
    test_new_own_event_reopens_spent_retry_but_foreign_event_does_not` with
    the account's own second event restored to 80 -> 20 -> 20 (fix round
    K13 changed the 20s to 30 to step around this defect). The own event
    must reopen the spent retry."""
    import test_statusline_account_partitions as P

    def record(name, five, at):
        P._tick(monkeypatch, at)
        return P._record_bound(app, name, weekly=20, five=five, at=at)

    record("A", 70, 0)
    record("A", 20, 2)
    binding = record("A", 20, 4)
    account = binding.account_key
    for at in (580, 582, 584):
        P._tick(monkeypatch, at)
        P._spool(app, "A", "peer", at=at, weekly=20, five=19)
        P._reduce()
    spent = P._transition(account, 584).control.pending_drops["fiveHour"]
    assert spent.kernel_stage == "suppressed" and spent.attempts == 2
    record("B", 70, 600)
    record("B", 20, 602)
    record("B", 20, 604)
    P._tick(monkeypatch, 606)
    P._spool(app, "A", "peer", at=606, weekly=20, five=19)
    P._reduce()
    foreign = P._transition(account, 606).control.pending_drops["fiveHour"]
    assert foreign.attempts == 2 and foreign.retry_signature == spent.retry_signature
    record("A", 80, 608)
    record("A", 20, 610)
    record("A", 20, 612)
    P._tick(monkeypatch, 614)
    P._spool(app, "A", "peer", at=614, weekly=20, five=19)
    results = P._reduce()
    reopened = P._transition(account, 614).control.pending_drops["fiveHour"]
    assert reopened is None or reopened.retry_signature != spent.retry_signature, (
        "the account's own new event did not reopen the spent retry"
        + _diagnosis(app, account, 614, folds, results))


# ── S6 ─────────────────────────────────────────────────────────────────────


def _held_only_weekly_row(app, account):
    """A weekly axis whose only row is held: its emptiness is explained by
    a presentation filter alone, so it is unknown by design (spec 4.3
    *Unknown*) whatever the fold does — a waiting head that no fix of the
    S1 mechanism dissolves."""
    conn = app.open_db()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots (captured_at_utc, "
            "week_start_date, week_end_date, week_start_at, week_end_at, "
            "weekly_percent, source, payload_json, account_key, "
            "weekly_observation_held) VALUES (?,?,?,?,?,?,?,?,?,1)",
            (_iso(NOW), _iso(WEEK_RESET - 7 * 86400)[:10], _iso(WEEK_RESET)[:10],
             _iso(WEEK_RESET - 7 * 86400), _iso(WEEK_RESET), 60.0, "statusline",
             "{}", account))
        conn.commit()
    finally:
        conn.close()


def _forks(monkeypatch):
    import _cctally_statusline as sl
    forks = []
    monkeypatch.setattr(sl, "_fork_persist", lambda fd, reservation: (
        forks.append(fd), sl._release_reservation(reservation)))
    return forks


@pytest.mark.parametrize("state", ["s1_unchanged_reading", "unknown_changed_readings"])
def test_s6_renders_behind_a_waiting_head_never_fork(app, monkeypatch, folds, state):
    """S6: once the detached reducer has run against a head blocked on
    unknown evidence, N further renders fork no detached reducer (spec 4.3
    *Waiting heads*: nothing forks while a head is blocked on unchanged
    evidence), although K13's `_preliminary_decision` returns a fork
    whenever a reduction reports unknown axes. ``s1_unchanged_reading``
    repeats the deferred reading in S1's state (zero forks whether or not
    S1 is fixed: a published reading repeated is a NOOP). The other state
    is a weekly axis unknown by design, with a CHANGED reading every render,
    since captures never invalidate a wait. Expected GREEN: refuted, because
    the render's file-only `_render_head_waiting` check returns before the
    fork decision is reached."""
    renders = 10
    if state == "s1_unchanged_reading":
        account = _five_hour_equal_value_credit(app, monkeypatch)
        _render(app, monkeypatch, 620, weekly=21, five=25)
        forks = _forks(monkeypatch)
        for i in range(renders):
            _render(app, monkeypatch, 630 + 10 * i, sync=False, weekly=21, five=25)
    else:
        account = _identity()
        _held_only_weekly_row(app, account)
        _render(app, monkeypatch, 620, weekly=21, five=25)
        assert not _published(app, 21, 25), (
            "setup: the unknown weekly axis defers"
            + _diagnosis(app, account, 620, folds))
        forks = _forks(monkeypatch)
        for i in range(renders):
            _render(app, monkeypatch, 630 + 10 * i, sync=False,
                    weekly=21 + (i % 2), five=25 + i)
    assert forks == [], (
        f"{len(forks)} forks for {renders} renders behind a waiting head"
        + _diagnosis(app, account, 730, folds))
