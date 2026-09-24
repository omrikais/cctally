"""#868 — the dashboard restarts itself after a proven install.

Spec: docs/superpowers/specs/2026-09-23-868-dashboard-restart-on-install-design.md.
Replaces the #714 in-process pricing reload tests.
"""
from __future__ import annotations

import argparse
import http.client
import importlib
import json
import os
import sys
import threading
import time

import pytest

import _cctally_core
from conftest import load_script, redirect_paths
from test_update import _link, _make_brew_keg, _make_npm_install
from tests._support_http import PRESENCE_BACKSTOP_SECONDS, serve_dashboard, stop

pytestmark = pytest.mark.usefixtures("isolated_home")

DECISION_6 = ("The dashboard is restarting to load v1.111.0. "
              "Try again in a moment.")


def _wire_handler(ns):
    handler = ns["DashboardHTTPHandler"]
    handler.hub = ns["SSEHub"]()
    handler.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    handler.static_dir = ns["STATIC_DIR"]
    handler.sync_lock = threading.Lock()
    handler.run_sync_now = staticmethod(lambda *a, **k: None)
    handler.run_sync_now_locked = staticmethod(lambda *a, **k: None)
    handler.no_sync = False
    handler.display_tz_pref_override = None
    handler.cctally_host = "127.0.0.1"
    handler.cctally_api_token = None


def _post_update(port):
    conn = http.client.HTTPConnection("127.0.0.1", port,
                                      timeout=PRESENCE_BACKSTOP_SECONDS)
    try:
        body = b"{}"
        conn.putrequest("POST", "/api/update", skip_host=True,
                        skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.putheader("Origin", f"http://127.0.0.1:{port}")
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.endheaders()
        conn.send(body)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


@pytest.fixture
def ns(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


@pytest.fixture
def served(ns, monkeypatch):
    _wire_handler(ns)
    worker = ns["UpdateWorker"]()
    monkeypatch.setattr(worker, "_run", lambda run_id, version: None)
    monkeypatch.setitem(ns, "_UPDATE_WORKER", worker)
    srv, thread, port = serve_dashboard(ns)
    try:
        yield ns, worker, port
    finally:
        stop(srv, thread)


# --- R5 / decision 6: the Update button during an automatic restart ----------


def test_update_during_claim_is_refused_with_503_and_admits_no_run(served):
    ns, worker, port = served
    assert worker.claim_automatic_restart("1.111.0") is True
    status, payload = _post_update(port)
    assert status == 503
    assert payload == {"error": DECISION_6}
    assert worker.status() == {"current_run_id": None}
    worker.release_automatic_restart()
    status, payload = _post_update(port)
    assert status == 202
    assert payload == {"run_id": worker.status()["current_run_id"]}


def test_update_and_claim_interleave_through_the_endpoint(served, monkeypatch):
    ns, _, port = served
    for _ in range(20):
        worker = ns["UpdateWorker"]()
        monkeypatch.setattr(worker, "_run", lambda run_id, version: None)
        monkeypatch.setitem(ns, "_UPDATE_WORKER", worker)
        results = {}
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait()
            results["claim"] = worker.claim_automatic_restart("1.111.0")

        def post():
            barrier.wait()
            results["post"] = _post_update(port)

        ts = [threading.Thread(target=claim), threading.Thread(target=post)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(PRESENCE_BACKSTOP_SECONDS)
        status, payload = results["post"]
        if results["claim"]:
            assert (status, payload) == (503, {"error": DECISION_6})
            assert worker.status() == {"current_run_id": None}
        else:
            assert status == 202
            assert payload == {"run_id": worker.status()["current_run_id"]}


# --- The install watcher (R1-R4, R6, R7, R9, R10) ---------------------------


def _npm_env(ns, tmp_path, monkeypatch, boot_version="1.0.0"):
    base = tmp_path / "install"
    root = _make_npm_install(base / "lib/node_modules/cctally", boot_version)
    entry = _link(base / "bin/cctally", root / "bin/cctally-npm-shim.js")
    monkeypatch.setitem(ns, "ORIGINAL_ENTRYPOINT", str(entry))
    monkeypatch.setitem(ns, "ORIGINAL_SYS_ARGV", ["/x", "dashboard"])
    monkeypatch.setenv("CCTALLY_PYTHON", sys.executable)
    monkeypatch.setitem(ns, "_release_read_latest_release_version",
                        lambda: (boot_version, "2026-09-23"))
    return root


def _brew_env(ns, tmp_path, monkeypatch, boot_version="1.0.0"):
    base = tmp_path / "install"
    _make_brew_keg(base, boot_version)
    entry = _link(base / "bin/cctally",
                  base / f"Cellar/cctally/{boot_version}/bin/cctally")
    monkeypatch.setitem(ns, "ORIGINAL_ENTRYPOINT", str(entry))
    monkeypatch.setitem(ns, "ORIGINAL_SYS_ARGV", ["/x", "dashboard"])
    monkeypatch.setitem(ns, "_release_read_latest_release_version",
                        lambda: (boot_version, "2026-09-23"))
    return base, entry


def _switch_keg(base, entry, version):
    if not (base / "Cellar/cctally" / version).exists():
        _make_brew_keg(base, version)
    _link(entry, base / f"Cellar/cctally/{version}/bin/cctally")


def _set_version(root, version):
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2026-09-23\n")


def _watch(ns, boot, worker, events, **kw):
    """An InstallWatch whose exec, flush sleep and log are recorded in order.

    The exec records the claim it ran under, because the claim must be held
    from before the flush until the exec replaces the process.
    """
    def exec_fn(target, argv):
        events.append(("exec", target, list(argv), worker._auto_restart_version))

    return ns["InstallWatch"](
        boot, worker_fn=lambda: worker, exec_fn=exec_fn,
        sleep_fn=lambda s: events.append(("sleep", s)),
        log_fn=lambda msg: events.append(("log", msg)), **kw)


def _execs(events):
    return [e for e in events if e[0] == "exec"]


def test_updater_record_restarts_once_and_manual_npm_never(ns, tmp_path,
                                                            monkeypatch):
    """R1 + R4: a manual in-place npm rewrite never restarts; the same tree
    plus a qualifying install-success record restarts exactly once, with the
    direct-Python target, after a logged line and the 0.5 s flush."""
    root = _npm_env(ns, tmp_path, monkeypatch)
    boot = ns["capture_install_boot_state"]()
    assert boot.version == "1.0.0"
    assert boot.root == root.resolve()
    assert boot.record_token is None
    events, worker = [], ns["UpdateWorker"]()
    w = _watch(ns, boot, worker, events)

    _set_version(root, "1.1.0")
    assert [w.poll_once() for _ in range(3)] == ["idle"] * 3
    assert events == []

    ns["_write_install_success_record"]("1.1.0")
    assert w.poll_once() == "exec"
    argv = [sys.executable, str(root.resolve() / "bin/cctally"), "dashboard"]
    assert [e[0] for e in events] == ["log", "sleep", "exec"]
    assert "v1.0.0" in events[0][1] and "v1.1.0" in events[0][1]
    assert events[1] == ("sleep", 0.5)
    assert events[2] == ("exec", sys.executable, argv, "1.1.0")


def test_updater_negatives_each_block_until_the_record_qualifies(ns, tmp_path,
                                                                 monkeypatch):
    """R2: the boot record, a same or older version, a CHANGELOG that
    disagrees, and a held update.lock never restart; releasing the lock
    over a qualifying record does."""
    root = _npm_env(ns, tmp_path, monkeypatch)
    ns["_write_install_success_record"]("1.1.0")
    boot = ns["capture_install_boot_state"]()
    assert boot.record_token == ns["_read_install_success_record"]().token
    events = []
    w = _watch(ns, boot, ns["UpdateWorker"](), events)

    _set_version(root, "1.1.0")
    assert w.poll_once() == "idle"                    # present at boot
    ns["_write_install_success_record"]("1.0.0")
    _set_version(root, "1.0.0")
    assert w.poll_once() == "idle"                    # same version
    ns["_write_install_success_record"]("0.9.0")
    _set_version(root, "0.9.0")
    assert w.poll_once() == "idle"                    # older version
    ns["_write_install_success_record"]("1.2.0")
    _set_version(root, "1.1.0")
    assert w.poll_once() == "idle"                    # CHANGELOG disagrees
    _set_version(root, "1.2.0")
    fd = ns["_acquire_update_lock"]()
    try:
        assert w.poll_once() == "idle"                # update.lock held
    finally:
        ns["_release_update_lock"](fd)
    assert _execs(events) == []
    assert w.poll_once() == "exec"
    assert len(_execs(events)) == 1


def test_brew_keg_switch_restarts_and_unchanged_older_or_unreadable_do_not(
        ns, tmp_path, monkeypatch):
    """R3: only a switch into a different keg whose libexec CHANGELOG is
    strictly newer restarts, and it re-enters the captured entrypoint."""
    base, entry = _brew_env(ns, tmp_path, monkeypatch)
    boot = ns["capture_install_boot_state"]()
    assert boot.root == (base / "Cellar/cctally/1.0.0/libexec").resolve()
    events = []
    w = _watch(ns, boot, ns["UpdateWorker"](), events)

    assert w.poll_once() == "idle"                    # unchanged target
    _switch_keg(base, entry, "0.9.0")
    assert w.poll_once() == "idle"                    # older keg
    _make_brew_keg(base, "1.0.5")
    (base / "Cellar/cctally/1.0.5/libexec/CHANGELOG.md").unlink()
    _switch_keg(base, entry, "1.0.5")
    assert w.poll_once() == "idle"                    # no readable CHANGELOG
    assert _execs(events) == []

    _switch_keg(base, entry, "1.1.0")
    assert w.poll_once() == "exec"
    assert _execs(events) == [("exec", str(entry), [str(entry), "dashboard"],
                               "1.1.0")]


def test_both_signals_naming_one_version_produce_one_exec(ns, tmp_path,
                                                          monkeypatch):
    """R5: `cctally update` on Homebrew both writes the record and switches
    the keg; one poll issues one restart request for that version."""
    base, entry = _brew_env(ns, tmp_path, monkeypatch)
    boot = ns["capture_install_boot_state"]()
    events, worker = [], ns["UpdateWorker"]()
    w = _watch(ns, boot, worker, events)
    _switch_keg(base, entry, "1.1.0")
    ns["_write_install_success_record"]("1.1.0")
    assert w.find_candidate().version == "1.1.0"
    assert w.poll_once() == "exec"
    assert len(_execs(events)) == 1
    assert worker.claim_automatic_restart("1.1.0") is False


def test_active_update_run_defers_and_counts_no_attempt(ns, tmp_path,
                                                        monkeypatch):
    """R5: while an UpdateWorker run is admitted the watcher's claim fails,
    it never execs, and the deferral starts no retry backoff."""
    root = _npm_env(ns, tmp_path, monkeypatch)
    boot = ns["capture_install_boot_state"]()
    worker = ns["UpdateWorker"]()
    monkeypatch.setattr(worker, "_run", lambda run_id, version: None)
    assert worker.start(None)[0] is True
    events = []
    clock = [1000.0]
    w = _watch(ns, boot, worker, events, monotonic_fn=lambda: clock[0])
    _set_version(root, "1.1.0")
    ns["_write_install_success_record"]("1.1.0")
    assert [w.poll_once() for _ in range(3)] == ["deferred"] * 3
    assert events == []
    with worker._lock:
        worker._current_id = None
    assert w.poll_once() == "exec"
    assert len(_execs(events)) == 1


def test_failed_exec_releases_the_claim_and_follows_the_retry_schedule(
        ns, tmp_path, monkeypatch):
    """R6: each failed exec releases the claim and logs once; the same
    candidate is retried 1, 5 and 15 minutes later and then not again; a new
    record token is a new candidate with a fresh budget."""
    root = _npm_env(ns, tmp_path, monkeypatch)
    boot = ns["capture_install_boot_state"]()
    clock = [1000.0]
    worker = ns["UpdateWorker"]()
    monkeypatch.setattr(worker, "_run", lambda run_id, version: None)
    attempts, logs = [], []

    def boom(target, argv):
        attempts.append(clock[0])
        assert worker._auto_restart_version == "1.1.0"
        raise OSError("exec failed")

    w = ns["InstallWatch"](boot, worker_fn=lambda: worker, exec_fn=boom,
                           sleep_fn=lambda s: None, log_fn=logs.append,
                           monotonic_fn=lambda: clock[0])
    _set_version(root, "1.1.0")
    ns["_write_install_success_record"]("1.1.0")
    outcomes = []
    for advance in (0, 59, 1, 299, 1, 899, 1, 10_000, 10_000):
        clock[0] += advance
        outcomes.append(w.poll_once())
    assert outcomes == ["failed", "backoff", "failed", "backoff", "failed",
                        "backoff", "failed", "exhausted", "exhausted"]
    assert attempts == [1000.0, 1060.0, 1360.0, 2260.0]
    assert worker._auto_restart_version is None
    assert worker.start(None)[0] is True
    with worker._lock:
        worker._current_id = None
    assert sum("failed" in m for m in logs) == 4

    ns["_write_install_success_record"]("1.1.0")
    assert w.poll_once() == "failed"
    assert len(attempts) == 5


def test_invalid_target_is_a_failed_attempt_that_never_execs(ns, tmp_path,
                                                             monkeypatch):
    """R10: a missing <root>/bin/cctally makes the watcher's target invalid;
    the attempt fails without an exec and holds no claim afterward."""
    root = _npm_env(ns, tmp_path, monkeypatch)
    boot = ns["capture_install_boot_state"]()
    events, worker = [], ns["UpdateWorker"]()
    w = _watch(ns, boot, worker, events)
    _set_version(root, "1.1.0")
    ns["_write_install_success_record"]("1.1.0")
    (root / "bin" / "cctally").unlink()
    assert w.poll_once() == "failed"
    assert _execs(events) == []
    assert worker._auto_restart_version is None
    (root / "bin" / "cctally").write_text("#!/usr/bin/env python3\n")
    ns["_write_install_success_record"]("1.1.0")
    assert w.poll_once() == "exec"


def test_a_process_booted_after_the_install_never_qualifies_on_it(
        ns, tmp_path, monkeypatch):
    """R7: the restarted process captures the record token, install root and
    version at boot and never restarts on them; a later install still
    restarts it."""
    root = _npm_env(ns, tmp_path, monkeypatch, boot_version="1.1.0")
    ns["_write_install_success_record"]("1.1.0")
    boot = ns["capture_install_boot_state"]()
    events = []
    w = _watch(ns, boot, ns["UpdateWorker"](), events)
    assert [w.poll_once() for _ in range(3)] == ["idle"] * 3
    _set_version(root, "1.2.0")
    ns["_write_install_success_record"]("1.2.0")
    assert w.poll_once() == "exec"
    assert len(_execs(events)) == 1


def test_a_brew_process_booted_in_the_new_keg_never_qualifies_on_it(
        ns, tmp_path, monkeypatch):
    base, entry = _brew_env(ns, tmp_path, monkeypatch, boot_version="1.1.0")
    boot = ns["capture_install_boot_state"]()
    events = []
    w = _watch(ns, boot, ns["UpdateWorker"](), events)
    assert w.poll_once() == "idle"
    _switch_keg(base, entry, "1.2.0")
    assert w.poll_once() == "exec"


def test_a_broken_log_stream_neither_blocks_the_restart_nor_ends_the_watch(
        ns, tmp_path, monkeypatch):
    """A lost log line is not a failed restart attempt, and the watch thread
    keeps polling when its error log raises too."""
    root = _npm_env(ns, tmp_path, monkeypatch)
    boot = ns["capture_install_boot_state"]()

    def broken_log(msg):
        raise BrokenPipeError("stderr is gone")

    def boom(target, argv):
        raise OSError("exec failed")

    execs = []
    worker = ns["UpdateWorker"]()
    w = ns["InstallWatch"](boot, worker_fn=lambda: worker, exec_fn=boom,
                           sleep_fn=lambda s: None, log_fn=broken_log)
    _set_version(root, "1.1.0")
    ns["_write_install_success_record"]("1.1.0")
    assert w.poll_once() == "failed"
    assert worker._auto_restart_version is None
    ns["_write_install_success_record"]("1.1.0")
    w._exec = lambda target, argv: execs.append(target)
    assert w.poll_once() == "exec"
    assert execs == [sys.executable]

    polls, done = [], threading.Event()

    def poll_once():
        polls.append(1)
        if len(polls) == 1:
            raise RuntimeError("transient")
        done.set()
        return "idle"

    w2 = ns["InstallWatch"](boot, worker_fn=lambda: None, interval_s=0.01,
                            log_fn=broken_log)
    w2.poll_once = poll_once
    t = threading.Thread(target=w2.run, daemon=True)
    t.start()
    try:
        assert done.wait(PRESENCE_BACKSTOP_SECONDS)
    finally:
        w2.stop()
        t.join(PRESENCE_BACKSTOP_SECONDS)
    assert len(polls) >= 2


def test_the_watch_thread_survives_a_raising_poll(ns):
    boot = ns["InstallBootState"]("1.0.0", None, None)
    w = ns["InstallWatch"](boot, worker_fn=lambda: None, interval_s=0.01,
                           log_fn=lambda msg: None)
    polls = []
    done = threading.Event()

    def poll_once():
        polls.append(1)
        if len(polls) == 1:
            raise RuntimeError("transient")
        done.set()
        return "idle"

    w.poll_once = poll_once
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    try:
        assert done.wait(PRESENCE_BACKSTOP_SECONDS)
    finally:
        w.stop()
        t.join(PRESENCE_BACKSTOP_SECONDS)
    assert not t.is_alive()
    assert len(polls) >= 2


# --- Dashboard wiring (R6, R8, R9) -----------------------------------------


def _dash():
    return importlib.import_module("_cctally_dashboard")


def test_dev_checkout_never_starts_the_watch(ns, monkeypatch):
    """R9: the same process state starts the watch unless the process runs
    from a git checkout."""
    dash = _dash()
    monkeypatch.setattr(dash, "_INSTALL_WATCH", None)
    boot = ns["InstallBootState"]("1.0.0", None, None)
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: False)
    started = dash._start_install_watch(boot)
    try:
        assert started is not None
        assert "cctally-install-watch" in {t.name for t in threading.enumerate()}
    finally:
        dash._stop_install_watch()
    assert dash._INSTALL_WATCH is None
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: True)
    assert dash._start_install_watch(boot) is None
    assert dash._INSTALL_WATCH is None


class _NoopThread(threading.Thread):
    def __init__(self, *a, **k):
        super().__init__(daemon=True)

    def run(self):
        pass


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port,
                                      timeout=PRESENCE_BACKSTOP_SECONDS)
    try:
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def _run_dashboard(ns, tmp_path, monkeypatch, *, no_sync, while_serving):
    """Run the real `cmd_dashboard` from an npm-layout entrypoint on PATH and
    call `while_serving(watch, port, root)` while it serves."""
    dash = _dash()
    root = _make_npm_install(tmp_path / "install/lib/node_modules/cctally",
                             "1.0.0")
    _link(tmp_path / "install/bin/cctally", root / "bin/cctally-npm-shim.js")
    monkeypatch.setenv("PATH", f"{tmp_path / 'install/bin'}{os.pathsep}"
                               f"{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CCTALLY_PYTHON", sys.executable)
    monkeypatch.setitem(ns, "_release_read_latest_release_version",
                        lambda: ("1.0.0", "2026-09-23"))
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: False)
    monkeypatch.setattr(dash, "_DashboardUpdateCheckThread", _NoopThread)
    monkeypatch.setattr(dash, "_INSTALL_WATCH", None)

    started = []
    real_start = dash._start_install_watch

    def spy(boot):
        watch = real_start(boot)
        started.append(watch)
        return watch

    monkeypatch.setattr(dash, "_start_install_watch", spy)
    servers = []
    real_server_cls = dash._QuietThreadingHTTPServer

    class _RecordingServer(real_server_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(dash, "_QuietThreadingHTTPServer", _RecordingServer)
    seen = {}

    def serving(*_args):
        seen["result"] = while_serving(started[0], servers[0].server_address[1],
                                       root)

    monkeypatch.setattr(dash, "_dashboard_wait_for_signal", serving)
    args = argparse.Namespace(host="127.0.0.1", port=0, no_browser=True,
                              no_sync=no_sync, sync_interval=1000, tz=None)
    try:
        assert dash.cmd_dashboard(args) == 0
    finally:
        for server in servers:
            server.server_close()
    assert len(started) == 1 and started[0] is not None
    assert dash._INSTALL_WATCH is None
    return started[0], seen["result"]


def test_no_sync_dashboard_runs_the_watch_and_restarts_on_a_proven_install(
        ns, tmp_path, monkeypatch):
    """R8: `--no-sync` disables both sync threads, and the install watch still
    runs, detects a qualifying install and restarts in place. It stops with
    the dashboard."""
    calls = []

    def while_serving(watch, port, root):
        names = {t.name for t in threading.enumerate()}
        assert "cctally-install-watch" in names
        assert "tui-sync" not in names
        assert "dashboard-conversations-sync" not in names
        watch._exec = lambda target, argv: calls.append((target, list(argv)))
        watch._sleep = lambda s: None
        _set_version(root, "1.1.0")
        ns["_write_install_success_record"]("1.1.0")
        outcome = watch.poll_once()
        ns["_UPDATE_WORKER"].release_automatic_restart()
        return outcome, root

    watch, (outcome, root) = _run_dashboard(
        ns, tmp_path, monkeypatch, no_sync=True, while_serving=while_serving)
    assert outcome == "exec"
    assert calls == [(sys.executable,
                      [sys.executable, str(root.resolve() / "bin/cctally"),
                       *sys.argv[1:]])]
    assert watch._stop.is_set()


def test_a_failed_restart_leaves_the_dashboard_serving(ns, tmp_path,
                                                       monkeypatch):
    """R6: after an exec that raises, the claim is gone so POST /api/update
    admits a run, /api/data answers, and the sync and conversation threads
    are still running."""
    def while_serving(watch, port, root):
        deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
        wanted = {"tui-sync", "dashboard-conversations-sync",
                  "cctally-install-watch"}
        while not wanted <= {t.name for t in threading.enumerate()}:
            assert time.monotonic() < deadline, "threads never started"
            time.sleep(0.01)

        def boom(target, argv):
            raise OSError("exec failed")

        watch._exec = boom
        watch._sleep = lambda s: None
        _set_version(root, "1.1.0")
        ns["_write_install_success_record"]("1.1.0")
        outcome = watch.poll_once()

        worker = ns["_UPDATE_WORKER"]
        monkeypatch.setattr(worker, "_run", lambda run_id, version: None)
        post = _post_update(port)
        data_status = _get(port, "/api/data")
        names = {t.name for t in threading.enumerate()}
        return outcome, post, data_status, wanted <= names

    _, (outcome, post, data_status, alive) = _run_dashboard(
        ns, tmp_path, monkeypatch, no_sync=False, while_serving=while_serving)
    assert outcome == "failed"
    assert post[0] == 202
    assert data_status == 200
    assert alive
