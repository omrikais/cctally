"""Unit tests for /api/sync refresh-usage integration + lock split."""
import importlib
import queue as _queue
import threading

import pytest

import _lib_snapshot_cache as _snapshot_cache

from conftest import load_script, redirect_paths


def test_run_sync_now_locked_callable_when_lock_held(monkeypatch, tmp_path):
    """_run_sync_now_locked must be callable WITH the caller already holding sync_lock."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    sync_lock = threading.Lock()
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = ns["SSEHub"]()

    captured = {}
    def _build(now_utc=None, skip_sync=False, display_tz_pref_override=None,
               **kwargs):
        # `**kwargs` absorbs the #268 M4 additions (precompute_envelope,
        # runtime_bind) the dashboard `_make_run_sync_now_locked` now passes.
        captured["called"] = True
        return ns["_empty_dashboard_snapshot"]()
    monkeypatch.setitem(ns, "_tui_build_snapshot", _build)

    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=None,
        display_tz_pref_override=None,
    )
    with sync_lock:
        locked(skip_sync=True)
    assert captured.get("called") is True


def test_run_sync_now_public_acquires_lock(monkeypatch, tmp_path):
    """The public _run_sync_now wrapper acquires sync_lock before calling locked variant."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    sync_lock = threading.Lock()
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = ns["SSEHub"]()
    monkeypatch.setitem(ns, "_tui_build_snapshot",
                        lambda **kw: ns["_empty_dashboard_snapshot"]())

    public = ns["_make_run_sync_now"](
        sync_lock=sync_lock, ref=ref, hub=hub, pinned_now=None,
        display_tz_pref_override=None,
    )
    public(skip_sync=False)
    # If the wrapper didn't release the lock, this acquire would block forever.
    acquired = sync_lock.acquire(blocking=False)
    assert acquired is True
    sync_lock.release()


# ----------------------------------------------------------------------
# HTTP-level scenarios (Task 4): /api/sync handler integration.
# ----------------------------------------------------------------------
import http.client
import json


def _serve(ns, host="127.0.0.1", port=0):
    srv = ns["ThreadingHTTPServer"]((host, port), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, srv.server_address[1]


def _wire(ns, *, no_sync=False, refresh_result=None, sync_lock=None):
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    ns["DashboardHTTPHandler"].static_dir = ns["STATIC_DIR"]
    ns["DashboardHTTPHandler"].sync_lock = sync_lock or threading.Lock()
    rebuild_calls = {"n": 0}
    # NO-ARGUMENT, like the production wiring at
    # bin/_cctally_dashboard.py:9355 and :9358, which closes each staticmethod
    # over `args.no_sync` instead of taking a parameter. A stub that accepted
    # `skip_sync=` would accept a `cls.run_sync_now_locked(skip_sync=True)`
    # call that raises TypeError against the shipped wiring.
    def _locked():
        rebuild_calls["n"] += 1
    ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(_locked)
    ns["DashboardHTTPHandler"].run_sync_now = staticmethod(lambda: _locked())
    ns["DashboardHTTPHandler"].no_sync = no_sync
    ns["DashboardHTTPHandler"].display_tz_pref_override = None
    return rebuild_calls


def _post_sync(port):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    c.request("POST", "/api/sync", body="{}", headers={
        "Content-Type": "application/json",
        "Origin": f"http://127.0.0.1:{port}",
        "Host": f"127.0.0.1:{port}",
    })
    r = c.getresponse()
    body = r.read().decode()
    return r.status, body


def test_post_sync_ok_returns_204(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rebuild_calls = _wire(ns)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "ok")
    srv, t, port = _serve(ns)
    try:
        status, _ = _post_sync(port)
        assert status == 204
        assert rebuild_calls["n"] == 1
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_rate_limited_returns_200_with_warning(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rebuild_calls = _wire(ns)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "rate_limited")
    srv, t, port = _serve(ns)
    try:
        status, body = _post_sync(port)
        assert status == 200
        env = json.loads(body)
        assert env["status"] == "ok"
        codes = [w["code"] for w in env["warnings"]]
        assert codes == ["rate_limited"]
        assert rebuild_calls["n"] == 1
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_fetch_failed_returns_200_with_warning(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rebuild_calls = _wire(ns)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "fetch_failed")
    srv, t, port = _serve(ns)
    try:
        status, body = _post_sync(port)
        assert status == 200
        env = json.loads(body)
        codes = [w["code"] for w in env["warnings"]]
        assert codes == ["fetch_failed"]
        assert rebuild_calls["n"] == 1
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_parse_failed(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire(ns)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "parse_failed")
    srv, t, port = _serve(ns)
    try:
        status, body = _post_sync(port)
        assert status == 200
        env = json.loads(body)
        assert [w["code"] for w in env["warnings"]] == ["parse_failed"]
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_no_token(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire(ns)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "no_oauth_token")
    srv, t, port = _serve(ns)
    try:
        status, body = _post_sync(port)
        assert status == 200
        env = json.loads(body)
        assert [w["code"] for w in env["warnings"]] == ["no_oauth_token"]
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_record_failed(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire(ns)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "record_failed")
    srv, t, port = _serve(ns)
    try:
        status, body = _post_sync(port)
        assert status == 200
        env = json.loads(body)
        assert [w["code"] for w in env["warnings"]] == ["record_failed"]
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_no_sync_manual_refresh_reports_refresh_skipped(
        monkeypatch, tmp_path):
    """Under --no-sync, _refresh_usage_inproc must NOT be invoked.

    #583 S2 §4: the skip is now stated rather than silent — a manual
    ``refresh=1`` answers 200 with an explicit warning instead of a 204 that
    looks like a refresh happened.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rebuild_calls = _wire(ns, no_sync=True)
    invoked = {"n": 0}
    def _spy(timeout_seconds=5.0):
        invoked["n"] += 1
        return ns["_RefreshUsageResult"](status="ok")
    monkeypatch.setitem(ns, "_refresh_usage_inproc", _spy)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_sync(port)
        assert status == 200
        assert {"code": "refresh_skipped_no_sync"} in json.loads(body)["warnings"]
        assert invoked["n"] == 0  # refresh skipped
        assert rebuild_calls["n"] == 1  # snapshot rebuild still happens
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_free_lock_still_runs_synchronously(monkeypatch, tmp_path):
    """#583 S2 §4: the lock-free human click keeps exactly today's path.

    The non-blocking acquire replaced a bounded one, so the case that used to
    wait out a periodic tick is the case that now queues. A free lock must
    still refresh and rebuild inline and answer 204.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_lock = threading.Lock()
    rebuild_calls = _wire(ns, sync_lock=sync_lock)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "ok")
    srv, t, port = _serve(ns)
    try:
        status, _ = _post_sync(port)
        assert status == 204
        assert rebuild_calls["n"] == 1
        # Released, so the next caller is not blocked.
        assert sync_lock.acquire(blocking=False) is True
        sync_lock.release()
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_202_when_lock_held(monkeypatch, tmp_path):
    """#583 S2 — a contended refresh queues instead of 503-ing."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_lock = threading.Lock()
    _wire(ns, sync_lock=sync_lock)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "ok")
    srv, t, port = _serve(ns)
    try:
        sync_lock.acquire()          # hold it for the whole request
        try:
            status, body = _post_sync(port)
        finally:
            sync_lock.release()
    finally:
        srv.shutdown(); t.join(timeout=2)
    assert status == 202
    env = json.loads(body)
    assert env["status"] == "queued"
    assert env["request_id"] >= 1
    assert len(env["server_epoch"]) == 16


def test_post_sync_202_does_not_refresh_or_rebuild_inline(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_lock = threading.Lock()
    rebuild_calls = _wire(ns, sync_lock=sync_lock)
    invoked = {"n": 0}
    def _spy(timeout_seconds=5.0):
        invoked["n"] += 1
        return ns["_RefreshUsageResult"](status="ok")
    monkeypatch.setitem(ns, "_refresh_usage_inproc", _spy)
    srv, t, port = _serve(ns)
    try:
        sync_lock.acquire()
        try:
            status, _ = _post_sync_path(port, "/api/sync?refresh=1")
        finally:
            sync_lock.release()
    finally:
        srv.shutdown(); t.join(timeout=2)
    assert status == 202
    assert invoked["n"] == 0
    assert rebuild_calls["n"] == 0


def test_post_sync_queued_request_records_the_refresh_intent(
        monkeypatch, tmp_path):
    """The deferred OAuth leg is carried on the batch, not dropped."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_lock = threading.Lock()
    _wire(ns, sync_lock=sync_lock)
    ref = ns["DashboardHTTPHandler"].snapshot_ref
    srv, t, port = _serve(ns)
    try:
        sync_lock.acquire()
        try:
            _post_sync_path(port, "/api/sync?refresh=0")
            _post_sync_path(port, "/api/sync?refresh=1")
        finally:
            sync_lock.release()
    finally:
        srv.shutdown(); t.join(timeout=2)
    assert ref.activity()["requested_id"] == 2
    batch_id, refresh = ref.capture_batch()
    assert batch_id == 2
    assert refresh is True          # the two requests coalesced, OR-ed


def test_post_sync_never_returns_503(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_lock = threading.Lock()
    _wire(ns, sync_lock=sync_lock)
    srv, t, port = _serve(ns)
    try:
        sync_lock.acquire()
        try:
            status, _ = _post_sync(port)
        finally:
            sync_lock.release()
    finally:
        srv.shutdown(); t.join(timeout=2)
    assert status != 503


def test_machine_nudge_queues_even_when_the_lock_is_free(monkeypatch, tmp_path):
    """#583 S2 §4, load-bearing.

    `cmd_record_usage` fires at Claude Code's status-line cadence, so a nudge
    taking the synchronous path would rebuild at that frequency and reopen the
    #313 peg, bypassing the loop's duty floor entirely.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rebuild_calls = _wire(ns)          # lock free for the whole request
    ref = ns["DashboardHTTPHandler"].snapshot_ref
    srv, t, port = _serve(ns)
    try:
        status, body = _post_sync_path(port, "/api/sync?refresh=0&queue=1")
    finally:
        srv.shutdown(); t.join(timeout=2)
    assert status == 202
    assert json.loads(body)["status"] == "queued"
    assert rebuild_calls["n"] == 0
    assert ref.activity()["requested_id"] == 1


def test_no_sync_refuses_a_machine_nudge_without_queueing(monkeypatch, tmp_path):
    """--no-sync freezes data to the startup snapshot, so a queued nudge
    serviced by a skip_sync rebuild would read newly persisted rows and
    unfreeze it."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rebuild_calls = _wire(ns, no_sync=True)
    ref = ns["DashboardHTTPHandler"].snapshot_ref
    before = ref.activity()["requested_id"]
    srv, t, port = _serve(ns)
    try:
        status, _ = _post_sync_path(port, "/api/sync?refresh=0&queue=1")
    finally:
        srv.shutdown(); t.join(timeout=2)
    assert status == 204
    assert ref.activity()["requested_id"] == before   # the freeze holds
    assert rebuild_calls["n"] == 0


def test_a_contended_manual_click_under_no_sync_waits_instead_of_queueing(
        monkeypatch, tmp_path):
    """#583 S2 — nothing drains a queue under --no-sync, so nothing may queue.

    The spec justified having no --no-sync drainer by asserting the lock is
    free in that mode. It is not always free: `POST /api/settings` calls
    `run_sync_now()`, which takes `sync_lock` BLOCKING precisely so a config
    change propagates in a mode whose periodic thread never runs. A click
    landing inside that hold took the non-blocking-acquire queue branch and
    answered 202 — and `sync_thread` is None under --no-sync, so nothing ever
    captured the batch. `requested_id > started_id` stayed true forever and the
    client showed `queued…` for the life of the process.

    The holder in that mode is always another short synchronous rebuild, so the
    handler waits for it and keeps the request on today's synchronous path.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_lock = threading.Lock()
    rebuild_calls = _wire(ns, no_sync=True, sync_lock=sync_lock)
    ref = ns["DashboardHTTPHandler"].snapshot_ref
    srv, t, port = _serve(ns)
    releaser = None
    try:
        # Stand in for the /api/settings broadcast: the lock is genuinely held
        # when the request arrives, and released while the handler waits on it.
        sync_lock.acquire()
        releaser = threading.Timer(0.2, sync_lock.release)
        releaser.start()
        status, body = _post_sync_path(port, "/api/sync?refresh=1")
    finally:
        if releaser is not None:
            releaser.join(timeout=5)
        srv.shutdown(); t.join(timeout=2)

    assert status == 200, body
    assert {"code": "refresh_skipped_no_sync"} in json.loads(body)["warnings"]
    assert rebuild_calls["n"] == 1, "the click must be serviced synchronously"
    act = ref.activity()
    assert act["requested_id"] == act["started_id"], (
        "an enqueued request under --no-sync has no drainer and strands"
    )
    assert sync_lock.acquire(blocking=False) is True
    sync_lock.release()


def test_a_wedged_rebuild_under_no_sync_answers_sync_busy(monkeypatch, tmp_path):
    """#583 S2 — the `--no-sync` blocking acquire needs an upper bound.

    A bare `sync_lock.acquire()` pins an HTTP handler thread FOREVER when a
    rebuild wedges (a `cache.db` read that never returns, a hung builder), with
    no diagnostic at all — where the pre-#583 code answered 503. The bound
    answers inside the endpoint's own status vocabulary rather than
    reintroducing 503, and does not strand the client the way a 202 would in a
    mode where nothing drains the queue.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    dash = importlib.import_module("_cctally_dashboard")
    monkeypatch.setattr(
        dash, "_DASHBOARD_NO_SYNC_LOCK_TIMEOUT_SECONDS", 0.05)
    sync_lock = threading.Lock()
    rebuild_calls = _wire(ns, no_sync=True, sync_lock=sync_lock)
    ref = ns["DashboardHTTPHandler"].snapshot_ref
    srv, t, port = _serve(ns)
    try:
        sync_lock.acquire()          # a wedged rebuild: never released
        try:
            status, body = _post_sync_path(port, "/api/sync?refresh=1")
        except (TimeoutError, OSError) as exc:
            pytest.fail(
                "the handler thread stayed pinned by the wedged rebuild "
                f"instead of answering: {exc!r}"
            )
        finally:
            sync_lock.release()
    finally:
        srv.shutdown(); t.join(timeout=2)

    assert status == 200, body
    env = json.loads(body)
    assert env["status"] == "ok"
    assert env["warnings"] == [{"code": "sync_busy"}]
    assert rebuild_calls["n"] == 0
    act = ref.activity()
    assert act["requested_id"] == 0, (
        "nothing drains a queue under --no-sync, so nothing may be enqueued"
    )


def test_post_sync_immediate_during_long_automatic_cooldown(monkeypatch, tmp_path):
    """F10 (#313 P2): the automatic sync thread's work-proportional cooldown
    holds no lock, so a manual POST /api/sync runs immediately even while the
    automatic thread is parked in a long cooldown."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    dash = importlib.import_module("_cctally_dashboard")
    sync_lock = threading.Lock()
    rebuild_calls = _wire(ns, sync_lock=sync_lock)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "ok")

    first_iteration_done = threading.Event()
    stop = threading.Event()

    def run_iteration():
        # Mirror the real automatic tick: hold the shared sync_lock only for the
        # "rebuild", then release it before entering the cooldown.
        with sync_lock:
            pass
        first_iteration_done.set()

    loop_thread = threading.Thread(
        target=lambda: dash._dashboard_sync_loop(
            stop=stop, interval=100.0, run_iteration=run_iteration,
            take_sync_request=lambda: False,
        ),
        daemon=True,
    )
    loop_thread.start()
    srv, t, port = _serve(ns)
    try:
        # The automatic thread has finished one iteration and is now parked in
        # its 100s cooldown, holding no lock.
        assert first_iteration_done.wait(timeout=2)
        status, _ = _post_sync(port)
        assert status == 204
        assert rebuild_calls["n"] == 1  # ran immediately — not gated by the cooldown
    finally:
        stop.set()
        loop_thread.join(timeout=2)
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_lock_released_between_calls(monkeypatch, tmp_path):
    """After a successful POST, the lock must be released so the next POST can run."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_lock = threading.Lock()
    _wire(ns, sync_lock=sync_lock)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "ok")
    srv, t, port = _serve(ns)
    try:
        s1, _ = _post_sync(port)
        s2, _ = _post_sync(port)
        assert s1 == 204 and s2 == 204
    finally:
        srv.shutdown(); t.join(timeout=2)


# ----------------------------------------------------------------------
# Task 1 (#180): ?refresh=0 rebuild-only mode on POST /api/sync.
# ----------------------------------------------------------------------
def _post_sync_path(port, path="/api/sync"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body="", headers={
        "Origin": f"http://127.0.0.1:{port}",
        "Host": f"127.0.0.1:{port}",
    })
    r = c.getresponse()
    body = r.read().decode()
    return r.status, body


def test_post_sync_refresh_zero_skips_fetch_rebuild_only(monkeypatch, tmp_path):
    """POST /api/sync?refresh=0 must NOT call _refresh_usage_inproc, but
    must still rebuild + return 204 (rebuild-only repaint)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rebuild_calls = _wire(ns)
    invoked = {"n": 0}
    def _spy(timeout_seconds=5.0):
        invoked["n"] += 1
        return ns["_RefreshUsageResult"](status="ok")
    monkeypatch.setitem(ns, "_refresh_usage_inproc", _spy)
    srv, t, port = _serve(ns)
    try:
        status, _ = _post_sync_path(port, "/api/sync?refresh=0")
        assert status == 204
        assert invoked["n"] == 0        # fetch skipped
        assert rebuild_calls["n"] == 1  # rebuild still ran
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_paramless_still_fetches(monkeypatch, tmp_path):
    """Regression: a param-less POST /api/sync keeps fetching (refresh
    defaults to '1'), so the SyncChip path is byte-identical to today."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rebuild_calls = _wire(ns)
    invoked = {"n": 0}
    def _spy(timeout_seconds=5.0):
        invoked["n"] += 1
        return ns["_RefreshUsageResult"](status="ok")
    monkeypatch.setitem(ns, "_refresh_usage_inproc", _spy)
    srv, t, port = _serve(ns)
    try:
        status, _ = _post_sync_path(port, "/api/sync")
        assert status == 204
        assert invoked["n"] == 1        # fetch ran (default refresh=1)
        assert rebuild_calls["n"] == 1
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_nudge_helper_enqueues_against_a_real_server(monkeypatch, tmp_path):
    """End-to-end: the real `_nudge_dashboard_repaint` client helper POSTs
    `/api/sync?refresh=0&queue=1` to a real handler — headers pass CSRF (not
    403), the request is ENQUEUED, and zero OAuth fetch fires.

    #583 S2 §4: the helper used to return only after the rebuild had settled,
    because the handler rebuilt inline. It now returns as soon as the request
    is queued, so the observable outcome is the advanced queue state rather
    than a completed rebuild. That is the whole point — a nudge firing at the
    status-line cadence must not drive a rebuild at that cadence.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rebuild_calls = _wire(ns)
    ref = ns["DashboardHTTPHandler"].snapshot_ref
    invoked = {"n": 0}
    def _spy(timeout_seconds=5.0):
        invoked["n"] += 1
        return ns["_RefreshUsageResult"](status="ok")
    monkeypatch.setitem(ns, "_refresh_usage_inproc", _spy)
    srv, t, port = _serve(ns)
    try:
        # Real helper, real ephemeral port. urlopen returns only after the
        # handler has sent the 202, so the queue state is already settled.
        ns["_nudge_dashboard_repaint"](port=port, timeout_seconds=5.0)
        assert ref.activity()["requested_id"] == 1   # enqueued, not dropped
        assert rebuild_calls["n"] == 0               # nothing rebuilt inline
        assert invoked["n"] == 0                     # zero OAuth fetch
        batch_id, refresh = ref.capture_batch()
        assert batch_id == 1
        assert refresh is False                      # refresh=0 was honoured
    finally:
        srv.shutdown(); t.join(timeout=2)


# ----------------------------------------------------------------------
# #583 S2: the nudge authenticates, and the #282 boundary is not weakened.
# ----------------------------------------------------------------------
def _post_sync_with_token(port, path="/api/sync?refresh=0&queue=1", token=None):
    headers = {
        "Origin": f"http://127.0.0.1:{port}",
        "Host": f"127.0.0.1:{port}",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body="", headers=headers)
    r = c.getresponse()
    body = r.read().decode()
    return r.status, body


# ----------------------------------------------------------------------
# #583 S2 — a handler-driven SYNCHRONOUS rebuild publishes `rebuilding`.
#
# An uncontended manual refresh takes the non-blocking acquire, succeeds, and
# rebuilds inline; `202 queued` is only the CONTENDED branch. So the ordinary
# case of a user clicking the sync chip runs a 1.9-6.3 s rebuild during which
# every OTHER connected tab used to publish `rebuilding: false` and could not
# distinguish a busy dashboard from a wedged one. `POST /api/settings` has the
# identical hole.
# ----------------------------------------------------------------------


class _RecordingHub:
    """An SSEHub that also retains every published frame, so a test can assert
    the exact frame SEQUENCE as well as what a subscriber received."""

    def __init__(self, inner):
        self._inner = inner
        self.frames = []

    def subscribe(self):
        return self._inner.subscribe()

    def unsubscribe(self, q):
        self._inner.unsubscribe(q)

    def publish(self, snapshot):
        self.frames.append(snapshot)
        self._inner.publish(snapshot)

    def flags(self):
        return [f.sync_activity["rebuilding"] for f in self.frames]


def _drain_flags(q):
    """Every `rebuilding` value a second observer has received so far.

    #583 S3 §5: the hub's queues carry a `_SSEDelivery` wrapping each
    publication, so the published snapshot is read off `.snapshot`.
    """
    flags = []
    while True:
        try:
            flags.append(q.get_nowait().snapshot.sync_activity["rebuilding"])
        except _queue.Empty:
            return flags


def _post_settings(port, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    raw = json.dumps(body).encode()
    c.putrequest("POST", "/api/settings", skip_host=True,
                 skip_accept_encoding=True)
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(raw)))
    c.putheader("Host", f"127.0.0.1:{port}")
    c.putheader("Origin", f"http://127.0.0.1:{port}")
    c.endheaders()
    c.send(raw)
    r = c.getresponse()
    return r.status, r.read().decode()


def test_a_synchronous_api_sync_rebuild_is_visible_to_a_second_observer(
        monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire(ns)                       # lock free — the SYNCHRONOUS branch
    hub = ns["DashboardHTTPHandler"].hub
    observer = hub.subscribe()      # a second tab, not the requesting client
    seen = {}

    def _locked():
        seen["mid_rebuild"] = _drain_flags(observer)

    ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(_locked)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "ok")
    srv, t, port = _serve(ns)
    try:
        status, _ = _post_sync(port)
    finally:
        srv.shutdown(); t.join(timeout=2)

    assert status == 204
    assert True in seen.get("mid_rebuild", []), (
        "a second tab must be able to see that this dashboard is rebuilding "
        "while the requesting client's rebuild is still running"
    )
    act = ns["DashboardHTTPHandler"].snapshot_ref.activity()
    assert act["rebuilding"] is False


def test_a_settings_broadcast_rebuild_is_visible_to_a_second_observer(
        monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire(ns)
    hub = ns["DashboardHTTPHandler"].hub
    observer = hub.subscribe()
    seen = {}

    def _run_sync_now():
        seen["mid_rebuild"] = _drain_flags(observer)

    ns["DashboardHTTPHandler"].run_sync_now = staticmethod(_run_sync_now)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_settings(port, {"cache_report": {}})
    finally:
        srv.shutdown(); t.join(timeout=2)

    assert status == 200, body
    assert True in seen.get("mid_rebuild", []), (
        "POST /api/settings rebuilds under the same lock and must report it"
    )
    act = ns["DashboardHTTPHandler"].snapshot_ref.activity()
    assert act["rebuilding"] is False


def _stub_the_ingest(ns, monkeypatch):
    """Neutralize the standalone ingest the `skip_sync=False` branch runs.

    Neither stub calls its progress callback, so no A2 partial publishes and the
    frame count is the handler's own two.
    """
    monkeypatch.setitem(ns, "sync_cache", lambda conn, *, progress=None, **kw: None)
    monkeypatch.setitem(ns, "sync_codex_cache", lambda conn, *a, **kw: None)


@pytest.mark.parametrize("no_sync", [False, True])
def test_a_synchronous_api_sync_rebuild_still_publishes_only_two_frames(
        monkeypatch, tmp_path, no_sync):
    """The two fixes compose: brackets the rebuild WITHOUT adding a third frame.

    The rebuild's own terminal publish clears the flag, so the handler's
    trailing mark finds no transition and publishes nothing.

    Parametrized because the two deployments terminate at DIFFERENT lines and
    only one of them is the common case. Production wires
    ``run_sync_now_locked`` as a NO-ARGUMENT staticmethod closed over
    ``args.no_sync`` (bin/_cctally_dashboard.py:9358), and ``_handle_post_sync``
    calls it with no arguments — so an ordinary install runs ``skip_sync=False``
    and terminates at bin/_cctally_tui.py:7848, while only a ``--no-sync``
    install runs ``skip_sync=True`` and terminates at :7863. This test used to
    override the staticmethod with ``lambda skip_sync=False: real_locked(
    skip_sync=True)``, which forced the ``--no-sync`` line unconditionally and
    so exercised the wrong terminal publish for its own subject. The lambda now
    mirrors production exactly: it takes NO argument and closes over the
    parametrized ``no_sync``, and ``cls.no_sync`` is set to the same value, so
    each leg is a configuration that can actually ship. Keeping a ``skip_sync``
    parameter here would leave the affordance the old bug abused — a future
    ``cls.run_sync_now_locked(skip_sync=True)`` would raise ``TypeError``
    against the production wiring while this test silently accepted it and
    drove the wrong branch.

    Two frames, exactly, only because the `skip_sync=False` stubs suppress the
    A2 progressive fill; the real count on that branch is two PLUS one per A2
    progress publish that clears the throttle.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire(ns, no_sync=no_sync)
    hub = _RecordingHub(ns["DashboardHTTPHandler"].hub)
    ns["DashboardHTTPHandler"].hub = hub
    ref = ns["DashboardHTTPHandler"].snapshot_ref
    monkeypatch.setitem(ns, "_tui_build_snapshot",
                        lambda **kw: ns["_empty_dashboard_snapshot"]())
    if not no_sync:
        _stub_the_ingest(ns, monkeypatch)
    real_locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=None, display_tz_pref_override=None,
    )
    ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(
        lambda: real_locked(skip_sync=no_sync))
    srv, t, port = _serve(ns)
    try:
        status, _ = _post_sync_path(port, "/api/sync?refresh=0")
    finally:
        srv.shutdown(); t.join(timeout=2)
        # The real locked body arms the #279 S5 owner-thread tripwire, and it
        # armed it on a SERVER REQUEST thread that is now gone. That global
        # lives in `_lib_snapshot_cache`, which `load_script()` does not reset,
        # so leaving it armed makes every later test that mutates the snapshot
        # cache from the main thread raise "mutation from non-owner thread".
        # Same discipline as tests/test_snapshot_cache_owner_thread.py.
        _snapshot_cache.reset_owner_thread()

    assert status == 204
    assert hub.flags() == [True, False]


def test_a_raising_synchronous_rebuild_still_clears_rebuilding(
        monkeypatch, tmp_path):
    """The 500 path is an exit path too. A flag left set would pin every
    client's chip at `syncing…` for the life of the process."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire(ns)

    def _boom():
        raise RuntimeError("rebuild exploded")

    ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(_boom)
    srv, t, port = _serve(ns)
    try:
        status, _ = _post_sync_path(port, "/api/sync?refresh=0")
    finally:
        srv.shutdown(); t.join(timeout=2)

    assert status == 500
    act = ns["DashboardHTTPHandler"].snapshot_ref.activity()
    assert act["rebuilding"] is False


def test_unauthenticated_api_sync_still_401s(monkeypatch, tmp_path):
    """The nudge gains a bearer header; the boundary it must satisfy does not
    move. A token-configured install still refuses an unauthenticated POST."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire(ns)
    ns["DashboardHTTPHandler"].cctally_api_token = "s3cret"
    srv, t, port = _serve(ns)
    try:
        status, _ = _post_sync_with_token(port, token=None)
        assert status == 401
    finally:
        ns["DashboardHTTPHandler"].cctally_api_token = None
        srv.shutdown(); t.join(timeout=2)


def test_authenticated_machine_nudge_queues(monkeypatch, tmp_path):
    """With the bearer header the same nudge is accepted and enqueued —
    which is what makes F17's nudge load-bearing rather than swallowed."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire(ns)
    ns["DashboardHTTPHandler"].cctally_api_token = "s3cret"
    ref = ns["DashboardHTTPHandler"].snapshot_ref
    srv, t, port = _serve(ns)
    try:
        status, body = _post_sync_with_token(port, token="s3cret")
    finally:
        ns["DashboardHTTPHandler"].cctally_api_token = None
        srv.shutdown(); t.join(timeout=2)
    assert status == 202
    assert json.loads(body)["status"] == "queued"
    assert ref.activity()["requested_id"] == 1
