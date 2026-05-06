"""Unit tests for /api/sync refresh-usage integration + lock split."""
import threading
from conftest import load_script, redirect_paths


def test_run_sync_now_locked_callable_when_lock_held(monkeypatch, tmp_path):
    """_run_sync_now_locked must be callable WITH the caller already holding sync_lock."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    sync_lock = threading.Lock()
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = ns["SSEHub"]()

    captured = {}
    def _build(now_utc=None, skip_sync=False, display_tz_pref_override=None):
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
    def _locked(skip_sync=False):
        rebuild_calls["n"] += 1
    ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(_locked)
    ns["DashboardHTTPHandler"].run_sync_now = staticmethod(
        lambda: _locked(skip_sync=False))
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


def test_post_sync_no_sync_mode_skips_refresh(monkeypatch, tmp_path):
    """Under --no-sync, _refresh_usage_inproc must NOT be invoked."""
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
        status, _ = _post_sync(port)
        assert status == 204
        assert invoked["n"] == 0  # refresh skipped
        assert rebuild_calls["n"] == 1  # snapshot rebuild still happens
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_waits_for_lock_release(monkeypatch, tmp_path):
    """Click that lands inside the periodic thread's lock-hold waits
    briefly (bounded acquire) and then succeeds with 204 — no 503 unless
    contention exceeds the timeout."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_lock = threading.Lock()
    _wire(ns, sync_lock=sync_lock)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "ok")
    # Shrink the timeout so the test stays fast; the holder thread releases
    # well within this budget.
    monkeypatch.setitem(ns, "_DASHBOARD_SYNC_LOCK_TIMEOUT_SECONDS", 1.0)
    srv, t, port = _serve(ns)
    try:
        sync_lock.acquire()
        # Release the lock from another thread shortly after the POST
        # starts, simulating a periodic-thread tick finishing while the
        # user's click is in flight.
        def _release_after_delay():
            import time as _t
            _t.sleep(0.1)
            sync_lock.release()
        releaser = threading.Thread(target=_release_after_delay)
        releaser.start()
        try:
            status, _ = _post_sync(port)
            assert status == 204
        finally:
            releaser.join(timeout=2)
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_post_sync_503_when_lock_held_beyond_timeout(monkeypatch, tmp_path):
    """If the lock stays held beyond the timeout, the handler still 503s
    rather than hanging the request indefinitely."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_lock = threading.Lock()
    _wire(ns, sync_lock=sync_lock)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "ok")
    # Tight timeout so the test runs fast; lock is never released during
    # the request.
    monkeypatch.setitem(ns, "_DASHBOARD_SYNC_LOCK_TIMEOUT_SECONDS", 0.1)
    srv, t, port = _serve(ns)
    try:
        sync_lock.acquire()
        try:
            status, _ = _post_sync(port)
            assert status == 503
        finally:
            sync_lock.release()
    finally:
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
