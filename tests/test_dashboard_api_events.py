"""Minimal SSE protocol test — we just need to prove one event makes it
through with the right headers and framing. Longer-running behavior
(keep-alives, disconnects) is manually verified.

Python 3.14 note: http.client.HTTPResponse.read(amt) on a response
without Content-Length (as any SSE stream lacks) blocks until EOF
rather than returning partial data at timeout. We therefore read
directly from the response's underlying buffered file via read1()
(which returns whatever bytes are already buffered rather than
blocking until the full request size is satisfied) until we have the
first complete event frame (terminated by `\n\n`).
"""
import datetime as dt
import http.client
import json
import threading
import time

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture(autouse=True)
def _isolate_prod_dbs(monkeypatch, tmp_path):
    """Issue #144: the ``/api/events`` handler builds an envelope on subscribe,
    which opens ``cache.db`` + ``stats.db`` for freshness. Redirect ``$HOME`` to
    a tmp dir BEFORE the in-body ``load_script()`` (the conftest-blessed
    ``setenv("HOME", tmp) + load_script()`` ordering) so those resolve under
    ``tmp`` instead of the real ``~/.local/share/cctally`` — preventing the leak
    and the #142 prod-migration-guard trip from a dev checkout. See
    ``test_dashboard_api_data.py`` for the full rationale.
    """
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def test_events_headers_and_first_frame():
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = ns["_empty_dashboard_snapshot"]()
    ref = ns["_SnapshotRef"](snap)
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ref

    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    # #220: the /api/events SSE handler runs an infinite loop; teardown only
    # `srv.shutdown()`s (never joins the in-flight handler), so the abandoned
    # daemon thread can raise a non-disconnect exception after the test returns.
    # The stdlib default `handle_error` would dump that traceback to sys.stderr,
    # contaminating a later test's capsys window under serial pytest. Silence it.
    srv.handle_error = lambda request, client_address: None
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]

    try:
        # Publish one snapshot BEFORE the client connects so the seeded-
        # event path kicks in on subscribe and we don't wait 15s for a
        # keep-alive.
        hub.publish(snap)

        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        c.request("GET", "/api/events")
        r = c.getresponse()
        assert r.status == 200
        assert r.getheader("Content-Type").startswith("text/event-stream")
        assert r.getheader("Cache-Control") == "no-cache"

        # Read until we see a full SSE frame (terminated by blank line).
        # read1() returns already-buffered bytes up to n rather than
        # blocking until n bytes are available — essential here because
        # after the first frame the socket idles until the next publish
        # or 15s keep-alive.
        buf = b""
        deadline = time.monotonic() + 2.0
        while b"\n\n" not in buf and time.monotonic() < deadline:
            try:
                chunk = r.fp.read1(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
        raw = buf.decode("utf-8", errors="replace")
        assert "event: update" in raw, f"no event frame in {raw!r}"

        # The data: line contains valid JSON with the envelope shape.
        data_line = [ln for ln in raw.splitlines() if ln.startswith("data: ")][0]
        payload = json.loads(data_line[len("data: "):])
        assert "header" in payload
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_passive_sse_reflects_statusline_reducer_without_oauth(
        monkeypatch, tmp_path):
    """The periodic dashboard rebuild carries reducer-selected usage.

    Two statusline candidates are phase-locked at the spool boundary before
    either can reduce.  The normal periodic rebuild closure then owns the
    snapshot-ref update and hub publication; neither the reducer nor that
    passive rebuild may contact an OAuth path.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_fetch_oauth_usage", lambda *_a, **_kw: pytest.fail("OAuth called"))
    monkeypatch.setitem(ns, "_refresh_usage_inproc", lambda *_a, **_kw: pytest.fail("OAuth refresh called"))

    now = dt.datetime.now(dt.timezone.utc)
    reset = (now + dt.timedelta(days=3)).isoformat().replace("+00:00", "Z")
    parsed = [
        ns["_lib_statusline"].parse_statusline_stdin(json.dumps({
            "session_id": session_id,
            "rate_limits": {"seven_day": {"used_percentage": percent, "resets_at": reset}},
        }).encode())
        for session_id, percent in (("stale", 20.0), ("fresh", 24.0))
    ]
    phase = threading.Barrier(2)
    statusline = ns["_cctally_statusline"]
    write_candidate = statusline._write_candidate

    def phase_locked(candidate):
        write_candidate(candidate)
        phase.wait(timeout=3)

    monkeypatch.setattr(statusline, "_write_candidate", phase_locked)
    workers = [threading.Thread(
        target=statusline._statusline_persist, args=(candidate,), kwargs={"sync_for_test": True},
    ) for candidate in parsed]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    hub = ns["SSEHub"]()
    initial = ns["_empty_dashboard_snapshot"]()
    ref = ns["_SnapshotRef"](initial)
    rebuild = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=now, display_tz_pref_override=None,
        runtime_bind="127.0.0.1",
    )
    rebuild(skip_sync=True)
    snap = ref.get()
    assert snap.current_week is not None
    assert snap.current_week.used_pct == 24.0
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ref
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    srv.handle_error = lambda request, client_address: None
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=3)
        client.request("GET", "/api/events")
        response = client.getresponse()
        buf = b""
        deadline = time.monotonic() + 2.0
        while b"\n\n" not in buf and time.monotonic() < deadline:
            try:
                chunk = response.fp.read1(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
        data_line = next(
            line for line in buf.decode("utf-8", errors="replace").splitlines()
            if line.startswith("data: ")
        )
        envelope = json.loads(data_line[len("data: "):])
        assert envelope["current_week"]["used_pct"] == 24.0
    finally:
        srv.shutdown()
        thread.join(timeout=2)


# --- U8-G5: SSEHub multi-subscriber + cleanup (#217 S1) ---------------------
# Direct unit coverage of the fan-out hub (bin/_cctally_dashboard.py SSEHub):
# two subscribers both receive a published frame, and unsubscribe removes
# exactly that queue while leaving the other intact.

def _drain_nowait(q):
    """Pop every item currently queued (non-blocking) and return the list."""
    import queue as _queue
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except _queue.Empty:
            break
    return out


def test_ssehub_two_subscribers_both_receive_frame():
    ns = load_script()
    hub = ns["SSEHub"]()
    q1 = hub.subscribe()
    q2 = hub.subscribe()
    sentinel = {"frame": 1}
    hub.publish(sentinel)
    # Both subscribers see the exact published object.
    assert _drain_nowait(q1) == [sentinel]
    assert _drain_nowait(q2) == [sentinel]


def test_ssehub_unsubscribe_removes_only_that_queue():
    ns = load_script()
    hub = ns["SSEHub"]()
    q1 = hub.subscribe()
    q2 = hub.subscribe()
    # Remove q1; q2 must still be live.
    hub.unsubscribe(q1)
    frame = {"frame": 2}
    hub.publish(frame)
    # q1 is gone -> receives nothing; q2 still receives the frame.
    assert _drain_nowait(q1) == []
    assert _drain_nowait(q2) == [frame]
    # Unsubscribing an already-removed (or never-registered) queue is a no-op.
    hub.unsubscribe(q1)               # must not raise
    import queue as _queue
    hub.unsubscribe(_queue.Queue())   # never subscribed -> no-op, no raise


def test_ssehub_subscribe_seeds_last_frame():
    """A new subscriber is seeded with the last published frame so it renders
    immediately (the documented subscribe-seeding behavior)."""
    ns = load_script()
    hub = ns["SSEHub"]()
    seed = {"frame": "seed"}
    hub.publish(seed)              # published BEFORE anyone subscribes
    q = hub.subscribe()
    assert _drain_nowait(q) == [seed]
