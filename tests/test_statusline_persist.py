"""Statusline usage-persistence feeder + its marker/lock primitives.

Covers the new markers and helpers introduced for statusline-fed usage
persistence (spec 2026-07-17-usage-statusline-fallback-design):

- `_statusline_observe_*` liveness marker (mtime-based, mirrors the
  hook-tick throttle marker) — represents "the statusline is alive and
  feeding usage", touched even on a dedup no-op.
- `_oauth_backoff_*` deadline marker (absolute epoch stored in the file
  CONTENT, never the mtime) — the shared 429 cooldown deadline.
- The `_statusline_persist` feeder itself (Task 3) — a guarded, throttled,
  fork-detached feeder into `cmd_record_usage`, exercised through the
  hidden `sync_for_test=True` foreground path.

All path constants are pinned under the per-test tmp APP_DIR so nothing
touches the developer's real prod data dir.
"""
import argparse
import types
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import stat
import subprocess
import sys
import time

import pytest

import _lib_statusline_candidates as candidate_lib

from conftest import load_script, redirect_paths


_CCTALLY_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin" / "cctally"


def _iso(epoch):
    return (
        dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _status_input(app, *, seven_pct, seven_resets_epoch,
                  five_pct=None, five_resets_epoch=None, with_five_key=False,
                  model_id=None, session_id=None, transcript_path=None):
    """Parse a CC-shaped stdin payload into a StatuslineInput (as production
    does), so `_statusline_persist` sees exactly the real parsed object.

    ``model_id`` (optional) sets ``model.id`` on the payload so the
    pool-identity guard (spec D1) can be exercised end-to-end through the
    real parser (which surfaces ``model.id`` onto ``StatuslineInput.model_id``).
    """
    rl = {
        "seven_day": {
            "used_percentage": seven_pct,
            "resets_at": _iso(seven_resets_epoch),
        }
    }
    if with_five_key:
        rl["five_hour"] = {
            "used_percentage": five_pct if five_pct is not None else 0,
            "resets_at": _iso(five_resets_epoch) if five_resets_epoch is not None else None,
        }
    payload = {"rate_limits": rl}
    if session_id is not None:
        payload["session_id"] = session_id
    if transcript_path is not None:
        payload["transcript_path"] = transcript_path
    if model_id is not None:
        payload["model"] = {"id": model_id}
    return app._lib_statusline.parse_statusline_stdin(
        json.dumps(payload).encode()
    )


def _count_rows(app, table):
    """Row count for ``table``, or 0 if the table doesn't exist yet."""
    conn = app.open_db()
    try:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def _rate_limits_payload(*, seven_pct, seven_resets_epoch,
                         five_pct=None, five_resets_epoch=None, model_id=None):
    rl = {
        "seven_day": {
            "used_percentage": seven_pct,
            "resets_at": _iso(seven_resets_epoch),
        }
    }
    if five_pct is not None:
        rl["five_hour"] = {
            "used_percentage": five_pct,
            "resets_at": _iso(five_resets_epoch),
        }
    payload = {"rate_limits": rl, "session_id": "sess-herd"}
    if model_id is not None:
        payload["model"] = {"id": model_id}
    return payload


def _newest_row(app):
    """The most-recent weekly_usage_snapshots row (sqlite3.Row) or None."""
    conn = app.open_db()
    try:
        return conn.execute(
            "SELECT source, weekly_percent, five_hour_percent, "
            "  five_hour_window_key, captured_at_utc "
            "FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def _snapshot_count(app):
    conn = app.open_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    finally:
        conn.close()


def _insert_snapshot(app, *, weekly_percent, weekly_resets_epoch, captured_epoch,
                     five_percent=None, five_resets_epoch=None, source="statusline"):
    """Seed one real stats row without invoking record-usage's HWM policy."""
    week_end = _iso(weekly_resets_epoch)
    week_start = _iso(weekly_resets_epoch - 7 * 86400)
    conn = app.open_db()
    try:
        five_key = (
            app._canonical_5h_window_key(five_resets_epoch)
            if five_resets_epoch is not None else None
        )
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, week_end_at, "
            " weekly_percent, page_url, source, payload_json, five_hour_percent, "
            " five_hour_resets_at, five_hour_window_key) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, '{}', ?, ?, ?)",
            (
                _iso(captured_epoch), week_start[:10], week_end[:10], week_start, week_end,
                weekly_percent, source, five_percent,
                _iso(five_resets_epoch) if five_resets_epoch is not None else None,
                five_key,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _pending_drop(app, *, percent=20):
    """A pending drop with no retained evidence.

    #755 gives the record a persisted deadline and a retained-evidence field.
    This helper deliberately leaves the evidence empty, so the callers below
    exercise the reconciliation path rather than the publication path: a record
    with nothing to publish is cleared instead. It is a hand-built shape rather
    than one any binary writes — a version-1 control document does not parse to
    this, it parses to no pending drop at all.
    """
    first_seen = int(time.time())
    return candidate_lib.PendingDrop(
        canonical_key=1,
        reduced_percent=percent,
        first_seen_at=first_seen,
        kernel_stage="settling",
        attempts=0,
        contributors={},
        retry_signature=None,
        deadline_at=first_seen + candidate_lib.PENDING_DROP_DEADLINE_SECONDS,
        retained=None,
    )


def _record_ns(*, percent=42.0, resets_in_days=3, five_percent=None,
               five_resets_in_hours=None, **extra):
    """Build a plausible cmd_record_usage Namespace.

    ``resets_at`` lands inside the weekly plausibility band [now-30d,
    now+8d]; the optional 5h reset lands inside [now-10m, now+6h].
    ``extra`` threads additional attrs (e.g. ``source="api"``)."""
    now = int(time.time())
    ns = {
        "percent": percent,
        "resets_at": str(now + resets_in_days * 86400),
        "five_hour_percent": five_percent,
        "five_hour_resets_at": (
            str(now + int(five_resets_in_hours * 3600))
            if five_resets_in_hours is not None else None
        ),
    }
    ns.update(extra)
    return argparse.Namespace(**ns)


@pytest.fixture
def app(monkeypatch, tmp_path):
    """Fresh cctally module with every runtime path pinned under tmp.

    ``redirect_paths`` pins the three new marker/lock path constants
    (STATUSLINE_OBSERVE_MARKER_PATH / STATUSLINE_PERSIST_LOCK_PATH /
    OAUTH_BACKOFF_MARKER_PATH) under the per-test tmp APP_DIR, so nothing
    here touches the developer's real prod data dir.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["cctally"]


# --- observation marker (liveness) -----------------------------------------


def test_observe_marker_absent_is_infinite(app):
    assert app._statusline_observe_age_seconds() == float("inf")


def test_observe_touch_then_age_small(app):
    app._statusline_observe_touch()
    assert app._statusline_observe_age_seconds() < 5.0


# --- candidate spool and selected-state split (#318 Task 2) ---------------


def _future_week():
    return int(time.time()) + 3 * 86400


def test_candidate_artifacts_stay_in_redirected_app_dir(app, tmp_path):
    parsed = _status_input(
        app,
        session_id="secret-session",
        transcript_path="/private/work/transcript.jsonl",
        seven_pct=20,
        seven_resets_epoch=_future_week(),
    )
    app._statusline_persist(parsed, sync_for_test=True)
    candidate_dir = app._cctally_statusline._candidate_dir("unattributed")
    files = list(candidate_dir.iterdir())
    assert len(files) == 1
    assert re.fullmatch(r"[0-9a-f]{64}\.json", files[0].name)
    assert stat.S_IMODE(candidate_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    body = files[0].read_text()
    assert "secret-session" not in body
    assert "/private/work" not in body
    assert files[0].is_relative_to(tmp_path)
    assert app._cctally_statusline._transport_marker_path().is_relative_to(tmp_path)
    assert app._cctally_statusline._transition_path("unattributed").is_relative_to(tmp_path)
    assert not app.STATUSLINE_CANDIDATE_DIR.exists()


def test_the_statusline_persist_nudge_fires_outside_the_persist_lock(
        app, monkeypatch):
    """#583 S2 — the statusline's own record call must defer its nudge.

    `_statusline_reduce_and_publish` calls `cmd_record_usage` with
    `nudge_dashboard` defaulting true and NO sink, and every caller runs it
    inside the statusline persist flock. The nudge is a loopback POST with a
    multi-second timeout, so one unresponsive listener would stall every
    statusline render on this machine at status-line cadence.

    The path is reachable rather than theoretical: opportunistic ingest emits
    events in the uncontended common case, which is exactly the nudge gate. The
    first assertion below is what proves that, so a change that stopped
    nudging here would fail this test rather than pass it vacuously.
    """
    observed = []

    def _fake_nudge(*args, **kwargs):
        fd = app._try_acquire_persist_lock()
        observed.append(fd is not None)
        app._release_persist_lock(fd)

    monkeypatch.setitem(app.__dict__, "_nudge_dashboard_repaint", _fake_nudge)
    parsed = _status_input(
        app, session_id="nudge", seven_pct=41, seven_resets_epoch=_future_week()
    )
    app._statusline_persist(parsed, sync_for_test=True)

    assert observed, (
        "the opportunistic ingest emitted events, so the nudge gate opened — "
        "if this fails the nudge no longer fires from this path at all"
    )
    assert observed == [True], (
        "the nudge fired while the statusline persist lock was still held"
    )


#: #1064 spec section 7 test 4: the probe a real `cctally statusline` render and
#: every process it starts import at interpreter start. The spec names a
#: `sitecustomize` on `PYTHONPATH`, but under pytest the isolation interceptor
#: prepends its own `tests/isolation_bootstrap/sitecustomize.py` to every
#: child's `PYTHONPATH` (it does not chain-load, so it shadows any other), the
#: runner's venv disables `usercustomize`, and a launch that bypasses the
#: bootstrap is refused. A `PYTHONWARNINGS` category filter naming this module
#: makes CPython import it while it processes the warning options, before
#: `site` runs, in every interpreter that inherits the environment, and leaves
#: the isolation bootstrap untouched. It notes whether this process image has
#: forked since it started and records, as JSON lines in
#: `$CCTALLY_TEST_REDUCER_PROBE/records.jsonl`: its own load, every
#: `_statusline-continue` spawn (with `…_FAIL_SPAWN`, the render's spawn
#: raises), the process that runs `_run_reducer_pass` (pid, ppid, argv, the
#: fork flag, the selected-state descriptors open there, the reservation's
#: state), the pass's dashboard nudge and the pass's end. The statusline
#: module is loaded through `spec_from_file_location`, which bypasses
#: `sys.meta_path`, so the probe wraps `SourceFileLoader.exec_module`.
_REDUCER_PROBE = r"""
import fcntl, importlib.machinery, os, sys

_OUT = os.environ.get("CCTALLY_TEST_REDUCER_PROBE")
_FORKED = [False]


class ProbeWarning(Warning):
    "The category the PYTHONWARNINGS filter names; never raised."


def _record(role, **fields):
    import json

    fields.update(role=role, pid=os.getpid(), ppid=os.getppid(),
                  argv=list(sys.argv), forked=_FORKED[0])
    fd = os.open(os.path.join(_OUT, "records.jsonl"),
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(fields) + "\n").encode())
    finally:
        os.close(fd)


def _held(path):
    "Whether another open file description holds an exclusive flock on path."
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _open_descriptors_of(path):
    try:
        target = os.stat(str(path))
    except FileNotFoundError:
        return []
    found = []
    for name in os.listdir("/dev/fd"):
        try:
            st = os.fstat(int(name))
        except (OSError, ValueError):
            continue
        if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
            found.append(int(name))
    return found


def _install(sl):
    import subprocess

    real_pass = sl._run_reducer_pass

    def probed_pass(*args, **kwargs):
        def nudge(*a, **k):
            _record("nudge", selected_held=_held(sl._selected_lock_path()))

        sl._cctally()._nudge_dashboard_repaint = nudge
        reservation_fd = None
        if "--reservation-fd" in sys.argv:
            reservation_fd = int(sys.argv[sys.argv.index("--reservation-fd") + 1])
        _record("pass-start",
                selected_fds=_open_descriptors_of(sl._selected_lock_path()),
                reservation_held=_held(sl._reducer_reservation_path()),
                reservation_fd_is_the_reservation=(
                    reservation_fd is not None and reservation_fd in
                    _open_descriptors_of(sl._reducer_reservation_path())))
        try:
            return real_pass(*args, **kwargs)
        finally:
            _record("pass-end",
                    reservation_held=_held(sl._reducer_reservation_path()))

    sl._run_reducer_pass = probed_pass

    class ProbePopen(subprocess.Popen):
        def __init__(self, args, *a, **k):
            command = [str(x) for x in args]
            if "_statusline-continue" in command:
                fail = (os.environ.get("CCTALLY_TEST_REDUCER_PROBE_FAIL_SPAWN")
                        and "_statusline-continue" not in sys.argv)
                _record("spawn", command=command,
                        pass_fds=list(k.get("pass_fds", ())),
                        close_fds=k.get("close_fds"),
                        reservation_held=_held(sl._reducer_reservation_path()),
                        selected_held=_held(sl._selected_lock_path()),
                        failed=bool(fail))
                if fail:
                    raise OSError("probe: forced spawn failure")
            super().__init__(args, *a, **k)

    subprocess.Popen = ProbePopen


if _OUT:
    _record("probe-loaded")
    os.register_at_fork(after_in_child=lambda: _FORKED.__setitem__(0, True))
    _real_exec_module = importlib.machinery.SourceFileLoader.exec_module

    def _exec_module(self, module):
        _real_exec_module(self, module)
        if module.__name__ == "_cctally_statusline":
            _install(module)

    importlib.machinery.SourceFileLoader.exec_module = _exec_module
"""


def _probe_records(out):
    path = out / "records.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _flock_held(path):
    import fcntl
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_the_reducer_runs_in_the_execd_worker(tmp_path):
    """#1064 spec section 7 test 4. Detects a reducer pass that runs in an
    inherited process image — a bare-fork child of the render, or an exec'd
    worker that then forks — where macOS reloads the zone file on every
    local-time conversion (50-340x slower) and a pass held the selected state
    for up to an hour. Runs the real `cctally statusline` CLI against a
    sandbox store (bypassing the in-process continuation seam of
    `tests/_script_loader.py`) with the probe above imported at start-up.

    The pass must run in a process other than the render whose argv is
    `_statusline-continue --reservation-fd <fd>` and which has not forked
    since exec; the render hands the reservation over while it is held and
    after releasing the selected state; the worker inherits only the
    reservation (no selected-state descriptor), holds the reservation through
    the pass, nudges with the selected state free, publishes durably and
    releases the reservation at the end. A forced spawn failure leaves the
    arrival durable and the reservation free, and the next render answers
    it. A missing probe record fails the test."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    probe_dir = tmp_path / "probe"
    out = tmp_path / "probe-out"
    for path in (home / ".claude" / "projects", data, probe_dir, out):
        path.mkdir(parents=True, exist_ok=True)
    (probe_dir / "_reducer_probe.py").write_text(_REDUCER_PROBE)
    # A fresh hook-tick throttle marker keeps the render's statusline OAuth
    # timer from forking its own refresh child (it has nothing to fetch with).
    (data / "hook-tick.last-fetch").touch()
    bound = data / "statusline-account-bound-v3"
    selected_lock = bound / "selected.lock"
    reservation_lock = bound / "continuation.reducer.lock"
    db_path = data / "stats.db"

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CCTALLY_DATA_DIR"] = str(data)
    env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    env["CCTALLY_TEST_REDUCER_PROBE"] = str(out)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(probe_dir), env.get("PYTHONPATH")) if p)
    env.pop("CCTALLY_AS_OF", None)
    env["PYTHONWARNINGS"] = ",".join(
        w for w in (env.get("PYTHONWARNINGS"), "ignore::_reducer_probe.ProbeWarning") if w)
    now = int(time.time())

    def payload(percent):
        return json.dumps(_rate_limits_payload(
            seven_pct=percent, seven_resets_epoch=now + 3 * 86400,
            five_pct=12.0, five_resets_epoch=now + 2 * 3600)).encode()

    def render(percent, *, fail_spawn=False):
        render_env = dict(env)
        if fail_spawn:
            render_env["CCTALLY_TEST_REDUCER_PROBE_FAIL_SPAWN"] = "1"
        proc = subprocess.Popen(
            [sys.executable, str(_CCTALLY_BIN), "statusline"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, env=render_env)
        _, err = proc.communicate(payload(percent), timeout=60)
        loaded = [r for r in _probe_records(out)
                  if r["role"] == "probe-loaded" and r["pid"] == proc.pid]
        assert loaded, (
            "the probe did not load in the render, so nothing it records can "
            f"be trusted; render stderr: {err.decode(errors='replace')}")
        return proc.pid

    def pass_ended():
        return any(r["role"] == "pass-end" for r in _probe_records(out))

    def intake_bytes():
        return sum(s.stat().st_size for s in bound.glob("accounts/*/intake/*.jsonl"))

    def settled(worker):
        # timing-budget: the worker's final release check and WAL finalizer
        assert _wait_until(lambda: not _pid_alive(worker), 30.0), (
            "the worker never exited")
        assert not _flock_held(reservation_lock), "the reservation outlived the worker"
        assert not _flock_held(selected_lock)

    # -- A dispatching render: the pass runs in the exec'd worker.
    render_pid = render(57.0)
    # timing-budget: interpreter start-up plus one first-reading pass on an empty store
    assert _wait_until(pass_ended, 30.0), (
        f"no probe record of a completed reducer pass: {_probe_records(out)}")
    records = _probe_records(out)
    starts = [r for r in records if r["role"] == "pass-start"]
    assert starts, records
    for start in starts:
        assert start["pid"] != render_pid, (
            f"the reducer ran in the render process: {start}")
        assert start["argv"][1:3] == ["_statusline-continue", "--reservation-fd"], (
            f"the reducer ran in an inherited process image: {start}")
        assert start["forked"] is False, (
            f"the reducer ran in a process image that forked after exec: {start}")
        assert start["selected_fds"] == [], (
            f"the worker inherited a selected-state descriptor: {start}")
        assert start["reservation_held"] and start["reservation_fd_is_the_reservation"], (
            f"the worker ran without holding the reservation: {start}")
    spawns = [r for r in records if r["role"] == "spawn"]
    assert len(spawns) == 1 and spawns[0]["pid"] == render_pid, records
    spawn = spawns[0]
    assert not spawn["failed"]
    assert spawn["reservation_held"], (
        "the reservation was free at the hand-off while the work was pending")
    assert not spawn["selected_held"], (
        "the render dispatched while it still held the selected state")
    assert spawn["close_fds"] is True and len(spawn["pass_fds"]) == 1
    assert starts[0]["argv"][1:] == [
        "_statusline-continue", "--reservation-fd", str(spawn["pass_fds"][0])]
    worker = starts[0]["pid"]
    nudges = [r for r in records if r["role"] == "nudge"]
    assert nudges, "the first reading emitted events, so the pass must nudge"
    assert all(r["pid"] == worker and not r["selected_held"] for r in nudges), nudges
    settled(worker)
    assert _count_snapshots(db_path) == 1, "the pass did not publish durably"

    # -- A forced spawn failure: the arrival stays durable, nothing is held.
    (out / "records.jsonl").unlink()
    before = intake_bytes()
    failed_render = render(58.0, fail_spawn=True)
    records = _probe_records(out)
    spawns = [r for r in records if r["role"] == "spawn"]
    assert [r["pid"] for r in spawns] == [failed_render] and spawns[0]["failed"], (
        f"the render made no exec'd worker spawn to fail: {records}")
    # timing-budget: a stray pass (a fallback reducer) would publish within this
    _wait_until(pass_ended, 3.0)
    assert not [r for r in _probe_records(out) if r["role"].startswith("pass")], (
        "a failed worker spawn must leave the pass to the next event, not run it")
    assert not _flock_held(reservation_lock), "the failed spawn kept the reservation"
    assert not _flock_held(selected_lock)
    assert intake_bytes() > before, (
        "the arrival of the render whose spawn failed is not durable in the intake")
    assert _count_snapshots(db_path) == 1

    # -- The next activating event reclaims the reservation and answers it.
    render(58.0)
    # timing-budget: interpreter start-up plus one pass
    assert _wait_until(pass_ended, 30.0), _probe_records(out)
    settled([r for r in _probe_records(out) if r["role"] == "pass-start"][0]["pid"])
    assert _count_snapshots(db_path) == 2, "the retained arrival was never answered"


def test_reducer_ignores_incomplete_candidate_temp(app):
    candidate_dir = app._cctally_statusline._candidate_dir("unattributed")
    candidate_dir.mkdir(parents=True, mode=0o700)
    temp = candidate_dir / (
        "." + "a" * 64 + ".json.tmp.1." + "b" * 32
    )
    temp.write_text('{"schemaVersion":')
    assert app._load_candidate_spool(now_epoch=int(time.time())) == ()
    assert temp.exists()


def test_stale_first_fresh_second_is_order_independent(app):
    stale = _status_input(
        app, session_id="a", seven_pct=20, seven_resets_epoch=_future_week()
    )
    fresh = _status_input(
        app, session_id="b", seven_pct=24, seven_resets_epoch=_future_week()
    )
    app._statusline_persist(stale, sync_for_test=True)
    app._statusline_persist(fresh, sync_for_test=True)
    assert _newest_row(app)["weekly_percent"] == 24


def test_lock_loser_candidate_is_captured_and_published_by_the_continuation(
        app, monkeypatch):
    """#825/#832 spec 4.3 revision 3 (Codex design-fix-round-4 Q4): a render
    that loses the selected-state try-lock has already captured its reading
    in the durable intake, and it registers the wake a continuation worker
    answers (in-process, the worker runs inline) — it no longer waits for a
    next tick. A later stale tick never regresses it."""
    fresh = _status_input(
        app, session_id="fresh", seven_pct=24, seven_resets_epoch=_future_week()
    )
    real_lock = app._try_acquire_persist_lock
    monkeypatch.setattr(app, "_try_acquire_persist_lock", lambda: None)
    app._statusline_persist(fresh, sync_for_test=True)
    assert _snapshot_count(app) == 1
    assert _newest_row(app)["weekly_percent"] == 24
    monkeypatch.setattr(app, "_try_acquire_persist_lock", real_lock)
    stale = _status_input(
        app, session_id="stale", seven_pct=20, seven_resets_epoch=_future_week()
    )
    app._statusline_persist(stale, sync_for_test=True)
    assert _newest_row(app)["weekly_percent"] == 24


@pytest.mark.parametrize("first_five, second_five", [(20, 24), (24, 20)])
def test_same_raw_five_hour_candidates_preserve_each_session_percent(
        app, monkeypatch, first_five, second_five):
    """Two candidate identities may share a raw reset but not a percentage."""
    now = int(time.time())
    weekly_reset = now + 3 * 86400
    five_reset = now + 2 * 3600
    assert app.cmd_record_usage(_record_ns(
        percent=20, resets_in_days=3, five_percent=20, five_resets_in_hours=2,
    )) == 0

    real_lock = app._try_acquire_persist_lock
    monkeypatch.setattr(app, "_try_acquire_persist_lock", lambda: None)
    for session_id, five_pct in (("first", first_five), ("second", second_five)):
        app._statusline_persist(_status_input(
            app, session_id=session_id, seven_pct=20, seven_resets_epoch=weekly_reset,
            five_pct=five_pct, five_resets_epoch=five_reset, with_five_key=True,
        ), sync_for_test=True)
    monkeypatch.setattr(app, "_try_acquire_persist_lock", real_lock)
    app._statusline_reduce_and_publish()
    row = _newest_row(app)
    assert row["weekly_percent"] == 20
    assert row["five_hour_percent"] == 24


def test_weekly_advance_publishes_active_db_five_hour_companion(app):
    now = int(time.time())
    app._statusline_persist(_status_input(
        app, session_id="seed", seven_pct=20, seven_resets_epoch=now + 3 * 86400,
        five_pct=30, five_resets_epoch=now + 2 * 3600, with_five_key=True,
    ), sync_for_test=True)
    app._statusline_persist(_status_input(
        app, session_id="weekly", seven_pct=21, seven_resets_epoch=now + 3 * 86400,
    ), sync_for_test=True)
    row = _newest_row(app)
    assert row["weekly_percent"] == 21
    assert row["five_hour_percent"] == 30


def test_weekly_advance_drops_expired_db_five_hour_companion(app):
    now = int(time.time())
    app._statusline_persist(_status_input(
        app, session_id="seed", seven_pct=20, seven_resets_epoch=now + 3 * 86400,
        five_pct=30, five_resets_epoch=now, with_five_key=True,
    ), sync_for_test=True)
    app._statusline_persist(_status_input(
        app, session_id="weekly", seven_pct=21, seven_resets_epoch=now + 3 * 86400,
    ), sync_for_test=True)
    row = _newest_row(app)
    assert row["weekly_percent"] == 21
    assert row["five_hour_percent"] is None


def test_missing_control_reconciles_db_once_then_unchanged_tick_is_child_free(app, monkeypatch):
    now = int(time.time())
    assert app.cmd_record_usage(_record_ns(percent=50, resets_in_days=3)) == 0
    parsed = _status_input(
        app, session_id="equal", seven_pct=50, seven_resets_epoch=now + 3 * 86400,
    )
    app._statusline_persist(parsed, sync_for_test=True)
    assert app._cctally_statusline._transition_path("unattributed").exists()
    calls = []
    real_reduce = app._cctally_statusline._statusline_reduce_and_publish

    def counted_reduce(**kwargs):
        # #583 S2 added a keyword-only `nudge_sink`; the double forwards it
        # rather than pinning a signature it does not care about.
        calls.append(True)
        return real_reduce(**kwargs)

    monkeypatch.setattr(app._cctally_statusline, "_statusline_reduce_and_publish", counted_reduce)
    app._statusline_persist(parsed, sync_for_test=True)
    assert calls == []


def test_legacy_db_write_forces_one_control_only_reconciliation_child(app, monkeypatch):
    now = int(time.time())
    assert app.cmd_record_usage(_record_ns(
        percent=50, resets_in_days=3, five_percent=10, five_resets_in_hours=2,
    )) == 0
    parsed = _status_input(
        app, session_id="equal", seven_pct=50, seven_resets_epoch=now + 3 * 86400,
    )
    app._statusline_persist(parsed, sync_for_test=True)

    # A legacy direct writer changes only the active 5h projection.  The
    # candidate remains equal to its stale 7d control view, so repair must be
    # control-only rather than calling the kernel again.
    assert app.cmd_record_usage(_record_ns(
        percent=50, resets_in_days=3, five_percent=11, five_resets_in_hours=2,
    )) == 0
    calls = []
    real_reduce = app._cctally_statusline._statusline_reduce_and_publish

    def counted_reduce(**kwargs):
        # #583 S2 added a keyword-only `nudge_sink`; the double forwards it
        # rather than pinning a signature it does not care about.
        calls.append(True)
        return real_reduce(**kwargs)

    monkeypatch.setattr(app._cctally_statusline, "_statusline_reduce_and_publish", counted_reduce)
    app._statusline_persist(parsed, sync_for_test=True)
    assert calls == [True]
    control = app._read_control_state(now_epoch=int(time.time()), account_key="unattributed")
    assert control is not None
    assert control.db_projection.five_hour is not None
    assert control.db_projection.five_hour.percent == 11

    calls.clear()
    app._statusline_persist(parsed, sync_for_test=True)
    assert calls == []


def test_authoritative_observation_clears_only_its_axis_pending_drop(app):
    assert app.cmd_record_usage(_record_ns(percent=50, resets_in_days=3, source="api")) == 0
    projection = app._read_db_projection_stable(account_key="unattributed")
    pending = _pending_drop(app)
    app._write_control_state(candidate_lib.ControlState(
        projection, {"fiveHour": pending, "sevenDay": pending},
    ), binding=app._capture_binding())
    result = app._authoritative_record_usage(
        _record_ns(percent=50, resets_in_days=3, source="api"), {"sevenDay"},
        binding=app._capture_binding(),
    )
    assert result.status == "ok"
    control = app._read_control_state(now_epoch=int(time.time()), account_key="unattributed")
    assert control is not None
    assert control.pending_drops["sevenDay"] is None
    assert control.pending_drops["fiveHour"] is not None


def test_authoritative_equal_fifty_clears_an_actual_pending_twenty_generation(app):
    now = int(time.time())
    assert app.cmd_record_usage(_record_ns(percent=50, resets_in_days=3, source="api")) == 0
    lower = _status_input(
        app, session_id="lower", seven_pct=20, seven_resets_epoch=now + 3 * 86400,
    )
    app._statusline_persist(lower, sync_for_test=True)
    pending_control = app._read_control_state(now_epoch=int(time.time()), account_key="unattributed")
    assert pending_control is not None
    pending = pending_control.pending_drops["sevenDay"]
    assert pending is not None and pending.reduced_percent == 20

    result = app._authoritative_record_usage(
        _record_ns(percent=50, resets_in_days=3, source="api"), {"sevenDay"},
        binding=app._capture_binding(),
    )
    assert result.status == "ok"
    control = app._read_control_state(now_epoch=int(time.time()), account_key="unattributed")
    assert control is not None and control.pending_drops["sevenDay"] is None


def test_empty_spool_clears_an_evidence_free_pending_drop(app):
    """The record has nothing to publish, so reconciliation clears it.

    The name this test used to carry promised the retired contract, under which
    an empty spool expired a pending drop whatever it held. An empty spool is
    not a retraction now, and a drop that carries retained evidence survives it
    and publishes at its own deadline. What is cleared here is the evidence-free
    record `_pending_drop` builds.
    """
    assert app.cmd_record_usage(_record_ns(percent=50, resets_in_days=3)) == 0
    projection = app._read_db_projection_stable(account_key="unattributed")
    app._write_control_state(candidate_lib.ControlState(
        projection, {"fiveHour": None, "sevenDay": _pending_drop(app)},
    ), binding=app._capture_binding())
    results = app._statusline_reduce_and_publish()
    assert len(results) == 1
    decision = results[0].decision
    assert decision is not None and decision.action == "WRITE_CONTROL"
    control = app._read_control_state(now_epoch=int(time.time()), account_key="unattributed")
    assert control is not None and control.pending_drops["sevenDay"] is None


def test_reset_zero_keeps_armed_consensus_until_the_next_revalidated_tick(
        app, monkeypatch):
    """The first zero only arms cmd_record_usage's existing debounce state.

    The publications are pinned to DISTINCT capture instants, tracking the
    same clock the kernel stage uses. An observation's journal id is a digest
    over `{t, at, src, provider, payload}`, so two zero publications inside
    one wall-clock second are literally one observation, and #750 S3's
    self-confirmation rule then refuses to confirm a reset from a
    byte-identical replay of the observation that armed it. Real ticks are
    seconds apart; the test has to be too.
    """
    now = int(time.time())
    clock = {"value": now}

    def _pin_capture():
        stamp = dt.datetime.fromtimestamp(clock["value"], tz=dt.timezone.utc)
        monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
        monkeypatch.setenv(
            "CCTALLY_AS_OF", stamp.isoformat().replace("+00:00", "Z"))

    # The 20% reading is pinned to `now` as well. Unpinned, a wall clock
    # that crosses a second boundary here records it at now+1, so the
    # baseline evaluation at `now` precedes it and is (correctly) fenced,
    # and the zero is first seen at now+1, one stage behind this script.
    _pin_capture()
    assert app.cmd_record_usage(_record_ns(percent=20, resets_in_days=3)) == 0
    parsed = _status_input(
        app, session_id="zero", seven_pct=0, seven_resets_epoch=now + 3 * 86400,
    )
    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_time = types.SimpleNamespace(**vars(app._cctally_statusline.time))
    _iso_time.time = lambda: clock["value"]
    monkeypatch.setattr(app._cctally_statusline, "time", _iso_time)

    app._statusline_persist(parsed, sync_for_test=True)  # settle baseline
    clock["value"] += 1
    _pin_capture()
    app._statusline_persist(parsed, sync_for_test=True)  # first kernel attempt arms zero
    assert _newest_row(app)["weekly_percent"] == 20
    control = app._read_control_state(now_epoch=clock["value"], account_key="unattributed")
    assert control is not None
    pending = control.pending_drops["sevenDay"]
    assert pending is not None and pending.kernel_stage == "zero_armed"

    clock["value"] += 1
    _pin_capture()
    app._statusline_persist(parsed, sync_for_test=True)  # revalidated second attempt commits
    assert _newest_row(app)["weekly_percent"] == 0
    control = app._read_control_state(now_epoch=clock["value"], account_key="unattributed")
    assert control is not None and control.pending_drops["sevenDay"] is None


def test_projection_keeps_active_five_hour_when_newer_weekly_only_row_exists(app):
    now = int(time.time())
    reset = now + 3 * 86400
    five_reset = now + 2 * 3600
    _insert_snapshot(
        app, weekly_percent=20, weekly_resets_epoch=reset, captured_epoch=now - 2,
        five_percent=30, five_resets_epoch=five_reset,
    )
    _insert_snapshot(
        app, weekly_percent=21, weekly_resets_epoch=reset, captured_epoch=now - 1,
    )
    projection = app._read_db_projection_once(account_key="unattributed")
    assert projection.seven_day is not None and projection.seven_day.percent == 21
    assert projection.five_hour is not None and projection.five_hour.percent == 30


def test_projection_normalizes_weekly_jitter_and_uses_capture_order_not_row_id(app):
    now = int(time.time())
    reset = now + 3 * 86400
    # Insert the newer capture first, then an older capture with a later row id.
    _insert_snapshot(
        app, weekly_percent=24, weekly_resets_epoch=reset + 40, captured_epoch=now - 1,
    )
    _insert_snapshot(
        app, weekly_percent=20, weekly_resets_epoch=reset + 5, captured_epoch=now - 2,
    )
    projection = app._read_db_projection_once(account_key="unattributed")
    assert projection.seven_day is not None
    assert projection.seven_day.percent == 24
    expected = int(app._normalize_week_boundary_dt(
        dt.datetime.fromtimestamp(reset + 40, tz=dt.timezone.utc)
    ).timestamp())
    assert projection.seven_day.canonical_key == expected


@pytest.mark.parametrize("value", [
    "2026-11-06T06:24:00Z", "2026-11-06T06:24:00.999999+00:00",
    "2026-11-06T08:24:00+02:00", "2026-11-05T22:24:00-08:00",
    " 2026-11-06 06:24:00Z ", "2026-11-06T06:24:00",
    "2026-11-06T12:09:00+05:45", "2026-11-06T11:54:15+05:30:15",
    "1969-12-31T23:59:59.250000Z", "1969-12-31T23:59:59.25",
    "20261106T062400Z", "2026-11-06T06:24Z", "2026-11-06T06:24:00z",
    "2026-W45-5T06:24:00Z",
    "0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59+00:00",
    "", "not-a-date", None, 1762410240,
    dt.datetime(2026, 11, 6, 6, 24, tzinfo=dt.timezone.utc),
])
def test_projection_epoch_equals_the_shared_parser(app, value):
    """#1064 (accuracy: a projection timestamp read as another instant
    reorders the snapshot rows and moves the selected reset and freshness).
    The fast path for aware values gives the epoch, or the error, that
    `parse_iso_datetime` gives, under a host zone with a non-zero offset so
    the naive value keeps its host-local reading."""
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Jerusalem"
    time.tzset()
    try:
        sl = sys.modules["_cctally_statusline"]
        core = sys.modules["_cctally_core"]

        def outcome(fn):
            try:
                return ("value", fn())
            except Exception as exc:  # the error class is part of the contract
                return ("raises", type(exc))

        assert outcome(lambda: sl._epoch_from_iso(value)) == outcome(
            lambda: int(core.parse_iso_datetime(str(value), "x").timestamp()))
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def test_projection_selects_latest_post_credit_weekly_and_five_hour_segments(app):
    now = int(time.time())
    reset = now + 3 * 86400
    five_reset = now + 2 * 3600
    _insert_snapshot(
        app, weekly_percent=50, weekly_resets_epoch=reset, captured_epoch=now - 20,
        five_percent=30, five_resets_epoch=five_reset,
    )
    conn = app.open_db()
    try:
        credit_at = _iso(now - 10)
        conn.execute(
            "INSERT INTO weekly_credit_floors "
            "(week_start_date, effective_at_utc, observed_pre_credit_pct, applied_at_utc) "
            "VALUES (?, ?, ?, ?)",
            (_iso(reset - 7 * 86400)[:10], credit_at, 50, credit_at),
        )
        conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, prior_percent, post_percent, effective_reset_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (credit_at, app._canonical_5h_window_key(five_reset), 30, 5, credit_at),
        )
        conn.commit()
    finally:
        conn.close()
    _insert_snapshot(
        app, weekly_percent=20, weekly_resets_epoch=reset, captured_epoch=now - 1,
        five_percent=5, five_resets_epoch=five_reset,
    )
    projection = app._read_db_projection_once(account_key="unattributed")
    assert projection.seven_day is not None and projection.seven_day.percent == 20
    assert projection.seven_day.reset_generation > 0
    assert projection.five_hour is not None and projection.five_hour.percent == 5
    assert projection.five_hour.reset_generation != ""  # logical event generation (#832)


def test_unchanged_statusline_candidate_touches_transport_not_selected(app):
    parsed = _status_input(
        app, session_id="same", seven_pct=20, seven_resets_epoch=_future_week()
    )
    app._statusline_persist(parsed, sync_for_test=True)
    selected = app._cctally_statusline._observe_marker_path("unattributed")
    selected.unlink()
    app._statusline_persist(parsed, sync_for_test=True)
    assert app._statusline_transport_age_seconds() < 5
    assert not selected.exists()


# --- oauth backoff deadline marker -----------------------------------------


def test_backoff_absent_is_zero(app):
    assert app._oauth_backoff_remaining_seconds() == 0.0


def test_backoff_set_and_remaining(app):
    app._oauth_backoff_set(time.time() + 120)
    assert 100 < app._oauth_backoff_remaining_seconds() <= 120


def test_backoff_never_shortens(app):
    now = time.time()
    app._oauth_backoff_set(now + 300)
    app._oauth_backoff_set(now + 30)  # shorter — must be ignored
    assert app._oauth_backoff_remaining_seconds() > 200


def test_backoff_clear(app):
    app._oauth_backoff_set(time.time() + 120)
    app._oauth_backoff_clear()
    assert app._oauth_backoff_remaining_seconds() == 0.0


# --- source labeling (Task 2) ----------------------------------------------


def test_default_record_source_is_statusline(app):
    """cmd_record_usage with no `source` attr defaults to 'statusline'
    (preserves the public CLI's current behavior)."""
    assert app.cmd_record_usage(_record_ns()) == 0
    row = _newest_row(app)
    assert row is not None
    assert row["source"] == "statusline"


def test_record_source_explicit_api(app):
    """An explicit source='api' on the args is written verbatim — proves the
    hard-coded 'statusline' is gone and OAuth rows can be labeled correctly."""
    assert app.cmd_record_usage(_record_ns(source="api")) == 0
    row = _newest_row(app)
    assert row is not None
    assert row["source"] == "api"


def test_backoff_stores_epoch_in_content_not_mtime(app):
    """The deadline is a FUTURE absolute epoch stored in the file's text
    content — NOT its mtime. Future-dating the mtime would corrupt the
    hook-tick throttle-age reading (which IS mtime-based). Guard that the
    marker file's mtime stays ~now while its content holds the future
    epoch."""
    import _cctally_core

    deadline = time.time() + 3600
    app._oauth_backoff_set(deadline)
    path = _cctally_core.OAUTH_BACKOFF_MARKER_PATH
    st = path.stat()
    # mtime is ~now, not the +1h deadline.
    assert abs(st.st_mtime - time.time()) < 60
    stored = float(path.read_text().strip())
    assert abs(stored - deadline) < 1.0


# --- statusline persist feeder (Task 3) ------------------------------------


def test_persist_writes_snapshot_and_milestone(app):
    """A fresh 7d reading persists a snapshot AND crosses a percent
    milestone through the unchanged cmd_record_usage kernel."""
    now = int(time.time())
    parsed = _status_input(app, seven_pct=42.0, seven_resets_epoch=now + 3 * 86400)
    app._statusline_persist(parsed, sync_for_test=True)

    row = _newest_row(app)
    assert row is not None
    assert row["source"] == "statusline"
    assert abs(row["weekly_percent"] - 42.0) < 1e-6

    conn = app.open_db()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM percent_milestones WHERE percent_threshold = 42"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n >= 1


def test_persist_milestone_uses_canonical_week_date_across_midnight(app):
    """A 23:45 UTC reset rounds into the next date for every derived row."""
    reset_dt = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)
    ).replace(hour=23, minute=45, second=0, microsecond=0)
    raw_week_start = reset_dt - dt.timedelta(days=7)
    canonical_week_start = (
        raw_week_start + dt.timedelta(hours=1)
    ).replace(minute=0)
    assert raw_week_start.date() != canonical_week_start.date()

    parsed = _status_input(
        app,
        seven_pct=42.0,
        seven_resets_epoch=int(reset_dt.timestamp()),
    )
    app._statusline_persist(parsed, sync_for_test=True)

    conn = app.open_db()
    try:
        usage_dates = {
            row[0] for row in conn.execute(
                "SELECT week_start_date FROM weekly_usage_snapshots"
            )
        }
        cost_dates = {
            row[0] for row in conn.execute(
                "SELECT week_start_date FROM weekly_cost_snapshots"
            )
        }
        milestone_count = conn.execute(
            "SELECT COUNT(*) FROM percent_milestones "
            "WHERE percent_threshold = 42"
        ).fetchone()[0]
    finally:
        conn.close()

    expected_date = canonical_week_start.date().isoformat()
    assert usage_dates == {expected_date}
    assert cost_dates == {expected_date}
    assert milestone_count == 1


def test_same_value_render_does_not_touch_selected_marker_on_dedup(app):
    """Unchanged stdin stays transport-lively but does not claim selection."""
    import _cctally_core

    now = int(time.time())
    parsed = _status_input(
        app, session_id="same-value", seven_pct=42.0,
        seven_resets_epoch=now + 3 * 86400,
    )

    app._statusline_persist(parsed, sync_for_test=True)  # inserts row #1
    assert _snapshot_count(app) == 1

    # Remove the selected marker; the same candidate must not recreate it.
    sys.modules["_cctally_statusline"]._observe_marker_path("unattributed").unlink()
    assert app._statusline_observe_age_seconds() == float("inf")

    app._statusline_persist(parsed, sync_for_test=True)
    assert _snapshot_count(app) == 1
    assert app._statusline_observe_age_seconds() == float("inf")
    assert app._statusline_transport_age_seconds() < 5.0


def test_no_rate_limits_is_noop(app):
    """Absent rate_limits (older CC / CC not supplying them) → clean no-op."""
    parsed = app._lib_statusline.parse_statusline_stdin(b"{}")
    app._statusline_persist(parsed, sync_for_test=True)
    assert _snapshot_count(app) == 0
    # No liveness claim either.
    assert app._statusline_observe_age_seconds() == float("inf")


def test_inactive_5h_null_pair_persists_weekly_drops_5h(app):
    """An inactive 5h window arrives as {used_percentage:0, resets_at:null}.
    Pair-gate drops the whole 5h pair; the weekly snapshot still writes."""
    now = int(time.time())
    parsed = _status_input(
        app, seven_pct=33.0, seven_resets_epoch=now + 3 * 86400,
        five_pct=0, five_resets_epoch=None, with_five_key=True,
    )
    # Parser surfaces 5h pct as 0.0 but resets_at as None (null).
    assert parsed.rate_limits_5h_pct == 0.0
    assert parsed.rate_limits_5h_resets_at is None

    app._statusline_persist(parsed, sync_for_test=True)
    row = _newest_row(app)
    assert row is not None
    assert row["weekly_percent"] is not None
    assert row["five_hour_percent"] is None


def test_missing_seven_day_resets_is_noop(app):
    """A 7d percent with no 7d resets_at is not a usable reading → no-op."""
    payload = {"rate_limits": {"seven_day": {"used_percentage": 40.0}}}
    parsed = app._lib_statusline.parse_statusline_stdin(json.dumps(payload).encode())
    assert parsed.rate_limits_7d_pct == 40.0
    assert parsed.rate_limits_7d_resets_at is None
    app._statusline_persist(parsed, sync_for_test=True)
    assert _snapshot_count(app) == 0


# --- concurrent-render herd (Task 3, P1-2 regression) ----------------------


def _count_snapshots(db_path):
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        # table not created yet
        return 0
    finally:
        conn.close()


def test_concurrent_renders_yield_at_most_one_snapshot(tmp_path):
    """P1-2 regression (multi-process / REAL fork path). N simultaneous
    `cctally statusline` renders sharing one data dir must produce AT MOST
    one snapshot: the cross-process persist lock + the child's under-lock
    marker re-check serialize the detached feeders so the herd cannot each
    fork-and-insert a duplicate row.

    Uses real subprocesses (the herd is cross-process), then drives the real
    next-tick retry contract while polling the detached feeders' DB to
    quiescence. Forking is deliberately best-effort under process pressure;
    an unlaunched winner leaves its candidate spooled for a later tick."""
    tmp_home = tmp_path / "home"
    tmp_data = tmp_path / "data"
    (tmp_home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    tmp_data.mkdir(parents=True, exist_ok=True)
    db_path = tmp_data / "stats.db"

    env = dict(os.environ)
    env["HOME"] = str(tmp_home)
    env["CCTALLY_DATA_DIR"] = str(tmp_data)
    env["CLAUDE_CONFIG_DIR"] = str(tmp_home / ".claude")
    env.pop("CCTALLY_AS_OF", None)

    now = int(time.time())
    payload = json.dumps(_rate_limits_payload(
        seven_pct=57.0, seven_resets_epoch=now + 3 * 86400,
        five_pct=12.0, five_resets_epoch=now + 2 * 3600,
    )).encode()

    def start_tick():
        return subprocess.Popen(
            # No --color flag: stdout is a pipe (not a TTY) so color is
            # auto-off; passing a value to the store_true --color errors.
            [sys.executable, str(_CCTALLY_BIN), "statusline"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env,
        )

    n_procs = 5
    procs = [start_tick() for _ in range(n_procs)]
    for p in procs:
        try:
            p.stdin.write(payload)
            p.stdin.close()
        except BrokenPipeError:
            pass
    for p in procs:
        p.wait(timeout=30)

    # The winning persist runs in a DETACHED child that outlives its render.
    # Poll to quiescence, but if no child launched under CI process pressure,
    # issue another real tick so the retained candidate gets another chance.
    # This follows the production delivery contract rather than lengthening a
    # one-shot sleep deadline and remains bounded if the fork path is broken.
    delivered = False
    for attempt in range(4):
        if attempt:
            retry = start_tick()
            try:
                retry.stdin.write(payload)
                retry.stdin.close()
            except BrokenPipeError:
                pass
            retry.wait(timeout=30)

        # timing-budget: one attempt's stability window, and this loop RETRIES four times, so raising it multiplies the failing path past the pytest cap
        deadline = time.monotonic() + 8.0
        last = _count_snapshots(db_path)
        stable = 0
        while time.monotonic() < deadline:
            time.sleep(0.4)
            cur = _count_snapshots(db_path)
            if cur == last:
                stable += 1
                if stable >= 5 and cur >= 1:
                    delivered = True
                    break
            else:
                stable = 0
                last = cur
        if delivered:
            break

    final = _count_snapshots(db_path)
    assert final <= 1, f"thundering herd produced {final} snapshots (want <= 1)"
    assert final == 1, f"expected exactly one snapshot from the herd, got {final}"


def test_bracketed_context_variant_persists_global_rate_limits(app):
    """`model.id` context metadata must not suppress the global 5h/7d axes.

    Claude Code exposes model-scoped quotas separately; the top-level
    `five_hour` and `seven_day` fields remain the account-wide usage windows.
    """
    now = int(time.time())
    parsed = _status_input(
        app, seven_pct=41.0, seven_resets_epoch=now + 3 * 86400,
        five_pct=42.0, five_resets_epoch=now + 2 * 3600, with_five_key=True,
        model_id="claude-opus-4-8[1m]",
    )
    app._statusline_persist(parsed, sync_for_test=True)

    row = _newest_row(app)
    assert row is not None
    assert row["source"] == "statusline"
    assert row["weekly_percent"] == pytest.approx(41.0)
    assert row["five_hour_percent"] == pytest.approx(42.0)
    assert app._statusline_observe_age_seconds() < 5.0


def test_normal_model_id_still_persists(app):
    """A normal model id persists exactly as before."""
    now = int(time.time())
    parsed = _status_input(
        app, seven_pct=27.0, seven_resets_epoch=now + 3 * 86400,
        model_id="claude-opus-4-8",
    )
    app._statusline_persist(parsed, sync_for_test=True)

    row = _newest_row(app)
    assert row is not None
    assert row["source"] == "statusline"
    assert abs(row["weekly_percent"] - 27.0) < 1e-6
    # Marker touched → the regular pipeline is alive.
    assert app._statusline_observe_age_seconds() < 5.0


def test_absent_model_id_still_persists(app):
    """No `model.id` at all (older CC payloads) is NOT a variant match →
    persist proceeds normally."""
    now = int(time.time())
    parsed = _status_input(app, seven_pct=27.0, seven_resets_epoch=now + 3 * 86400)
    assert parsed.model_id is None
    app._statusline_persist(parsed, sync_for_test=True)
    assert _count_rows(app, "weekly_usage_snapshots") == 1


# --- statusline-driven OAuth freshness -----------------------------------


def test_statusline_tick_fetches_authoritative_usage_within_one_cycle(
        app, monkeypatch):
    """A live statusline must not wait five minutes when its payload stalls.

    Claude can keep replaying an unchanged ``rate_limits`` object after the
    provider's authoritative 5h/7d percentages have advanced.  The periodic
    statusline tick therefore owns a bounded OAuth confirmation at the same
    cadence, while the shared hook throttle keeps it account-wide.
    """
    import _cctally_core

    interval = float(_cctally_core.STATUSLINE_OAUTH_POLL_SECONDS)
    assert interval <= 30.0
    monkeypatch.setattr(app, "_statusline_observe_age_seconds", lambda **_: interval + 1)
    monkeypatch.setattr(app, "_oauth_backoff_remaining_seconds", lambda: 0.0)

    calls = []

    def refresh(*, throttle_seconds):
        calls.append(throttle_seconds)
        return "ok(7d=7,5h=2)", {}

    monkeypatch.setattr(app, "_hook_tick_oauth_refresh", refresh)
    app._statusline_oauth_tick(sync_for_test=True)

    assert calls == [interval]
    assert app._hook_tick_throttle_age_seconds() < 5.0


def test_statusline_oauth_tick_is_account_wide_throttled(app, monkeypatch):
    """Concurrent/session-local 30s timers still produce one account poll."""
    import _cctally_core

    interval = float(_cctally_core.STATUSLINE_OAUTH_POLL_SECONDS)
    monkeypatch.setattr(app, "_statusline_observe_age_seconds", lambda **_: interval + 1)
    monkeypatch.setattr(app, "_oauth_backoff_remaining_seconds", lambda: 0.0)
    calls = []
    monkeypatch.setattr(
        app,
        "_hook_tick_oauth_refresh",
        lambda *, throttle_seconds: (
            calls.append(throttle_seconds) or "ok(7d=7,5h=2)",
            {},
        ),
    )

    app._statusline_oauth_tick(sync_for_test=True)
    app._statusline_oauth_tick(sync_for_test=True)

    assert calls == [interval]


def test_statusline_oauth_tick_honors_selected_freshness_and_backoff(
        app, monkeypatch):
    """Fresh selected data and a 429 deadline both suppress timer polling."""
    import _cctally_core

    interval = float(_cctally_core.STATUSLINE_OAUTH_POLL_SECONDS)
    calls = []
    monkeypatch.setattr(
        app,
        "_hook_tick_oauth_refresh",
        lambda *, throttle_seconds: (calls.append(throttle_seconds), None),
    )

    monkeypatch.setattr(app, "_statusline_observe_age_seconds", lambda **_: interval - 1)
    monkeypatch.setattr(app, "_oauth_backoff_remaining_seconds", lambda: 0.0)
    app._statusline_oauth_tick(sync_for_test=True)

    monkeypatch.setattr(app, "_statusline_observe_age_seconds", lambda **_: interval + 1)
    monkeypatch.setattr(app, "_oauth_backoff_remaining_seconds", lambda: 60.0)
    app._statusline_oauth_tick(sync_for_test=True)

    assert calls == []


def test_statusline_oauth_tick_never_waits_for_another_session(app, monkeypatch):
    """A concurrent account poll must not add latency to statusline output."""
    import fcntl
    import _cctally_core

    interval = float(_cctally_core.STATUSLINE_OAUTH_POLL_SECONDS)
    monkeypatch.setattr(app, "_statusline_observe_age_seconds", lambda **_: interval + 1)
    monkeypatch.setattr(app, "_oauth_backoff_remaining_seconds", lambda: 0.0)
    monkeypatch.setattr(app, "_hook_tick_throttle_age_seconds", lambda: interval + 1)
    monkeypatch.setattr(
        app,
        "_hook_tick_oauth_refresh",
        lambda **kwargs: pytest.fail("busy lock must suppress the refresh"),
    )

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(
        _cctally_core.HOOK_TICK_THROTTLE_LOCK_PATH,
        os.O_WRONLY | os.O_CREAT,
        0o644,
    )
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        app._statusline_oauth_tick(sync_for_test=True)
        elapsed = time.monotonic() - started
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert elapsed < 0.1


# --- #755: content-derived staleness of a whole rate_limits payload --------
#
# `received_at` records when cctally received a candidate, not when Claude
# Code obtained the numbers in it, so the 90-second activity window bounds
# nothing about how old the DATA is. An idle session re-renders its status
# line from a cached `rate_limits` block indefinitely. Because
# `_reduced_candidate` takes the MAXIMUM percent across active candidates, one
# such session pins the 7d consensus at its stale pre-reset value and the
# post-reset drop can never publish (issue #755).
#
# A 5-hour window that has already expired is a content-derived proof that the
# whole block predates that expiry. The parser already computes it and today
# drops only the 5h axis, keeping the 7d percent it has just proven stale.

def _statusline_mod(app):
    """The statusline glue module (``_candidate_from_input`` is not on the
    ``cctally`` re-export surface)."""
    mod = getattr(app, "_cctally_statusline", None)
    if mod is None:
        mod = sys.modules["_cctally_statusline"]
    return mod


def test_expired_five_hour_window_discards_the_whole_candidate(app):
    now = int(time.time())
    parsed = _status_input(
        app,
        seven_pct=28.0,
        seven_resets_epoch=now + 6 * 3600,
        five_pct=4.0,
        five_resets_epoch=now - 3600,
        with_five_key=True,
        session_id="idle-session",
    )
    assert _statusline_mod(app)._candidate_from_input(parsed, received_at=now) is None


def test_live_five_hour_window_still_yields_a_candidate(app):
    now = int(time.time())
    parsed = _status_input(
        app,
        seven_pct=1.0,
        seven_resets_epoch=now + 6 * 3600,
        five_pct=4.0,
        five_resets_epoch=now + 1800,
        with_five_key=True,
        session_id="live-session",
    )
    got = _statusline_mod(app)._candidate_from_input(parsed, received_at=now)
    assert got is not None
    assert got.seven_day is not None
    assert got.seven_day.percent == 1.0
    assert got.five_hour is not None


def test_absent_five_hour_block_still_yields_a_seven_day_candidate(app):
    """No 5h data at all proves nothing about staleness, so it must not be
    treated as stale — only a 5h window that is present and already expired
    is evidence."""
    now = int(time.time())
    parsed = _status_input(
        app,
        seven_pct=42.0,
        seven_resets_epoch=now + 6 * 3600,
        session_id="seven-only-session",
    )
    got = _statusline_mod(app)._candidate_from_input(parsed, received_at=now)
    assert got is not None
    assert got.seven_day is not None
    assert got.seven_day.percent == 42.0
    assert got.five_hour is None
