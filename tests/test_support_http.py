"""#630 S2 — the shared HTTP support module's own contract.

These tests exist because the helpers this module replaces were copied ten,
six and two times, and the copies had drifted to three different timeouts.
"""
from __future__ import annotations

import http.server
import socket
import socketserver
import threading
import time

import pytest

from tests import _support_http
from tests._support_http import (
    EXHAUSTED_DEADLINE_FLOOR_SECONDS, PRESENCE_BACKSTOP_SECONDS, post_json,
    read_event, read_no_event, remaining, serve, stop,
)


def _echo_server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    return http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)


def test_the_presence_backstop_is_the_load_safe_value():
    assert PRESENCE_BACKSTOP_SECONDS == 30.0


def test_remaining_shares_one_deadline_and_never_returns_a_poll():
    """Several waits in one test are one budget, not one budget each.

    The whole point is that the SUM stays bounded, so what matters is that
    the number shrinks as the deadline is spent. The floor matters just as
    much: a zero or negative timeout is a non-blocking attempt, so an
    exhausted deadline would turn the last wait of a sequence into a poll
    that reports its collaborator wedged the instant it is asked.
    """
    started = time.monotonic()
    deadline = started + PRESENCE_BACKSTOP_SECONDS
    first = remaining(deadline)
    assert first > 0
    # Never more than the budget it was handed. Stated against the budget's own
    # two endpoints rather than against the literal 30, because a measured
    # duration bounded above by a constant is a claim about the machine.
    assert first <= deadline - started
    time.sleep(0.05)
    assert remaining(deadline) < first

    # Already elapsed, and long elapsed.
    assert remaining(time.monotonic() - 1.0) == EXHAUSTED_DEADLINE_FLOOR_SECONDS
    assert remaining(time.monotonic() - 600.0) == EXHAUSTED_DEADLINE_FLOOR_SECONDS
    assert remaining(time.monotonic() - 1.0, floor=2.5) == 2.5


def test_serve_returns_a_listening_server_and_stop_joins_its_thread():
    srv, thread = serve(_echo_server)
    try:
        assert srv.server_address[1] != 0
        code, body = post_json(srv.server_address[1], "/x", {"a": 1})
        assert code == 200 and body == {"a": 1}
    finally:
        stop(srv, thread)
    assert not thread.is_alive()


def test_stop_reports_a_surviving_thread_by_name():
    srv, accept_thread = serve(_echo_server)
    release = threading.Event()
    stuck = threading.Thread(target=lambda: release.wait(60), name="stuck-probe",
                             daemon=True)
    stuck.start()
    try:
        # `backstop` is overridden so this meta-test costs half a second rather
        # than the thirty a real teardown is allowed. The failure it asserts is
        # the same one; only the wait is shortened.
        with pytest.raises(AssertionError, match="stuck-probe"):
            stop(srv, stuck, backstop=0.5)
    finally:
        release.set()
        # timing-budget: cleanup join for a thread this test has just released
        stuck.join(timeout=PRESENCE_BACKSTOP_SECONDS)
        # timing-budget: cleanup join for the accept loop stop() already ended
        accept_thread.join(timeout=PRESENCE_BACKSTOP_SECONDS)
    assert not stuck.is_alive()
    assert not accept_thread.is_alive(), (
        "stop() shut the server down before it failed, so its accept loop must "
        "have exited")


def _recorded_deadline(monkeypatch, *, frame):
    """Record the deadline the helper hands `_drain_for`, without spending it.

    Structural rather than wall-clock on purpose. Asserting `elapsed < N`
    would measure the runner, which is the very failure class this session
    exists to remove, and the timing-budget guard forbids it outright.
    """
    seen = {}

    def fake_drain(_sock, marker, deadline_seconds):
        seen["marker"] = marker
        seen["deadline"] = deadline_seconds
        return frame, "", 0.0

    monkeypatch.setattr(_support_http, "_drain_for", fake_drain)
    return seen


def test_read_no_event_uses_its_own_window_as_the_deadline(monkeypatch):
    seen = _recorded_deadline(monkeypatch, frame=None)
    read_no_event(object(), marker="event: tail", window=3.0)
    assert seen["deadline"] == 3.0, (
        f"an absence window is spent in full, so it must not inherit the "
        f"{PRESENCE_BACKSTOP_SECONDS}s presence backstop; got "
        f"{seen['deadline']}"
    )


def test_read_event_uses_the_presence_backstop_as_the_deadline(monkeypatch):
    seen = _recorded_deadline(monkeypatch, frame="event: tail\ndata: {}")
    read_event(object(), marker="event: tail")
    assert seen["deadline"] == PRESENCE_BACKSTOP_SECONDS


def test_two_frames_in_one_segment_are_both_read():
    """#630 S2, found by `bin/cctally-test-load-invariance` on the fleet.

    A read returns whatever the socket has, which is not one frame. The server
    writes `ready` and `baselined` back to back, so they routinely arrive in
    ONE segment — and a reader that threw its buffer away after matching the
    first frame dropped the second permanently. The next call then spent its
    whole thirty-second backstop watching keep-alives go by.

    It was intermittent rather than constant, and load made it MORE likely: a
    reader descheduled under contention drains more slowly, so the two writes
    have longer to coalesce. Three contended rounds produced this failure
    twice, in a test the ordinary suite passes every time.
    """
    a, b = socket.socketpair()
    try:
        b.sendall(b"event: ready\ndata: {}\n\nevent: baselined\ndata: {}\n\n")
        assert read_event(a, marker="event: ready").startswith("event: ready")
        assert read_event(a, marker="event: baselined", backstop=3.0).startswith(
            "event: baselined")
    finally:
        a.close()
        b.close()


def test_a_raise_from_settimeout_leaves_the_residue_with_the_socket():
    """The buffer is POPPED on the way in, so an early exit must hand it back.

    `sock.settimeout(...)` raises `OSError` on a closed socket. Without a
    `finally` that raise left with the residue already popped and never
    restored, so a later `read_no_event` on the same socket would have found an
    empty buffer and passed by having nothing to look at — vacuous rather than
    wrong, which is the failure this session exists to remove.
    """
    class ClosedSocket:
        def settimeout(self, _seconds):
            raise OSError(9, "Bad file descriptor")

    sock = ClosedSocket()
    _support_http._RESIDUE[sock] = b"event: keepalive\n\n"
    with pytest.raises(OSError):
        _support_http._drain_for(sock, "tail", 5.0)
    assert _support_http._RESIDUE.get(sock) == b"event: keepalive\n\n"


def test_a_frame_buffered_by_an_absence_check_is_still_readable_after_it():
    """The absence window reads bytes too, and must not swallow them.

    `read_no_event(marker="tail")` can buffer a `baselined` frame while proving
    no `tail` arrived. Discarding that buffer would make the next positive read
    wait for a frame the socket had already delivered.
    """
    a, b = socket.socketpair()
    try:
        b.sendall(b"event: baselined\ndata: {}\n\n")
        read_no_event(a, marker="event: tail", window=0.5)
        assert read_event(a, marker="event: baselined", backstop=3.0).startswith(
            "event: baselined")
    finally:
        a.close()
        b.close()


def test_a_frame_read_OUT_OF_ORDER_leaves_the_earlier_one_with_the_socket():
    """The residue kept what came AFTER the match and dropped what came before.

    `_match_frame` took its remainder from past the matched frame only, so every
    byte preceding the match was discarded — which contradicts this module's own
    claim that whatever was read and not returned stays with the socket.

    The consequence is a negative assertion that passes VACUOUSLY. Read `beta`
    first and the `alpha` frame that really arrived is gone, so a later
    `read_no_event(marker="event: alpha")` proves nothing about the server and
    everything about the reader. No call site reads a later marker before an
    earlier one today, so this was latent rather than live — and "no test
    becomes vacuous" is this session's own invariant, which is not a thing to
    hold only while nobody is standing on it.
    """
    a, b = socket.socketpair()
    try:
        b.sendall(b"event: alpha\ndata: 1\n\nevent: beta\ndata: 2\n\n")
        assert read_event(a, marker="event: beta").startswith("event: beta")
        with pytest.raises(AssertionError, match="event: alpha"):
            read_no_event(a, marker="event: alpha", window=0.3)
    finally:
        a.close()
        b.close()


def test_the_frames_around_a_match_keep_their_order():
    """Removing one frame must leave the rest a well-formed stream.

    The residue is every frame except the one returned, in the order they
    arrived. Reading the middle frame first and then the outer two in sequence
    is what proves the join is frame-aligned rather than a byte splice that
    happens to look right.
    """
    a, b = socket.socketpair()
    try:
        b.sendall(b"event: one\ndata: 1\n\n"
                  b"event: two\ndata: 2\n\n"
                  b"event: three\ndata: 3\n\n")
        assert read_event(a, marker="event: two").startswith("event: two")
        assert read_event(a, marker="event: one", backstop=3.0).startswith(
            "event: one")
        assert read_event(a, marker="event: three", backstop=3.0).startswith(
            "event: three")
    finally:
        a.close()
        b.close()


def test_the_first_frame_is_not_glued_to_the_HTTP_PREAMBLE():
    """The frame boundary is `\\n\\n`, and the response headers do not end in one.

    A live stream's first frame is preceded by the HTTP response headers, which
    end `\\r\\n\\r\\n` — a byte sequence that does NOT contain `\\n\\n`. Anchoring
    the frame to the bare spelling alone found no boundary there and returned
    the whole header block as part of the frame, so `startswith("event: ...")`
    failed on every live-tail test at once.
    """
    a, b = socket.socketpair()
    try:
        b.sendall(b"HTTP/1.0 200 OK\r\n"
                  b"Content-Type: text/event-stream; charset=utf-8\r\n"
                  b"Cache-Control: no-cache\r\n\r\n"
                  b"event: ready\ndata: {}\n\n")
        assert read_event(a, marker="event: ready") == "event: ready\ndata: {}"
    finally:
        a.close()
        b.close()


def test_read_no_event_returns_over_a_real_socket_when_nothing_arrives():
    a, b = socket.socketpair()
    try:
        read_no_event(a, marker="event: tail", window=0.5)
    finally:
        a.close()
        b.close()


def test_read_no_event_fails_when_the_frame_does_arrive():
    a, b = socket.socketpair()
    try:
        b.sendall(b"event: tail\ndata: {}\n\n")
        with pytest.raises(AssertionError, match="event: tail"):
            read_no_event(a, marker="event: tail", window=3.0)
    finally:
        a.close()
        b.close()


def test_read_event_returns_the_frame_that_carries_the_marker():
    a, b = socket.socketpair()
    try:
        b.sendall(b"event: other\ndata: 1\n\nevent: tail\ndata: {}\n\n")
        frame = read_event(a, marker="event: tail")
    finally:
        a.close()
        b.close()
    assert frame.startswith("event: tail")
    assert "event: other" not in frame, (
        "the returned frame must start at the marker, not at the buffer")


def test_a_blown_presence_backstop_names_what_it_waited_for():
    a, b = socket.socketpair()
    try:
        with pytest.raises(AssertionError) as exc:
            read_event(a, marker="event: never", backstop=0.5)
    finally:
        a.close()
        b.close()
    message = str(exc.value)
    assert "event: never" in message
    assert "0.5" in message


def test_a_blown_post_backstop_names_the_endpoint_it_waited_on():
    """A backstop that fires without saying what it was waiting for reproduces
    the undiagnosable red S1 removed, so the diagnostic is pinned here."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        with pytest.raises(AssertionError) as exc:
            post_json(port, "/never-answers", {"a": 1}, backstop=0.5)
    finally:
        listener.close()
    message = str(exc.value)
    assert "/never-answers" in message
    assert str(port) in message
    assert "0.5" in message


# --- the harder half of stop(): connections, the reap, and the distinction ---
#
# None of the three had a test, which is why `_handler_threads` shipped stating
# a mechanism that measurement contradicts.


def _sse_server(started, ended, server_class=http.server.ThreadingHTTPServer):
    """A server whose handler writes forever until its client goes away.

    This is the shape `stop()` exists for: `shutdown()` ends the accept loop
    and leaves this handler running, and only closing the client socket makes
    the next write fail.

    `server_class` exists because the two conversation-event files build
    `socketserver.ThreadingTCPServer`, whose `daemon_threads` class default is
    `False` where `http.server.ThreadingHTTPServer`'s is `True`.
    """
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            started.set()
            try:
                while True:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    time.sleep(0.02)
            except OSError:
                pass
            finally:
                ended.set()

        def log_message(self, *_a):
            pass

    srv = server_class(("127.0.0.1", 0), H)
    srv.handle_error = lambda request, client_address: None
    return srv


def _open_stream(port):
    sock = socket.create_connection(("127.0.0.1", port), timeout=PRESENCE_BACKSTOP_SECONDS)
    sock.sendall(b"GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    return sock


def test_a_daemon_handler_thread_is_still_tracked():
    """The tracked-versus-untracked distinction, over a real running handler.

    `ThreadingHTTPServer` sets `daemon_threads = True` and
    `socketserver._Threads.append` returns without recording a daemon thread,
    so the stdlib list stays empty however many handlers are running. Keying
    the distinction on that list answered "the server tracks no surviving
    handler thread" for a server that had one — the opposite of the truth.
    """
    started, ended = threading.Event(), threading.Event()
    srv, thread = serve(lambda: _sse_server(started, ended))
    sock = _open_stream(srv.server_address[1])
    try:
        assert started.wait(PRESENCE_BACKSTOP_SECONDS), "the handler never ran"
        tracked = _support_http._handler_threads(srv)
        assert tracked is not None, (
            "a server started through start() must be trackable")
        assert len(tracked) == 1, tracked
        assert tracked[0].daemon, (
            "the handler must stay a daemon thread: a non-daemon one that "
            "stops noticing its closed client blocks this worker's interpreter "
            "exit for as long as it runs, on every supported interpreter")
    finally:
        stop(srv, thread, connections=[sock])


def test_start_makes_the_handlers_daemons_on_a_non_daemon_server_class():
    """`start()` sets the flag rather than trusting the class it was handed.

    `socketserver.ThreadingTCPServer.daemon_threads` is `False` on every
    supported interpreter, and two conversation-event files build exactly that
    class. Without this, a live-tail handler that outlives `stop()`'s reap is a
    non-daemon thread, and a non-daemon thread blocks its xdist worker's
    interpreter exit for its full remaining duration.
    """
    started, ended = threading.Event(), threading.Event()
    srv, thread = serve(lambda: _sse_server(
        started, ended, server_class=socketserver.ThreadingTCPServer))
    sock = _open_stream(srv.server_address[1])
    try:
        assert started.wait(PRESENCE_BACKSTOP_SECONDS), "the handler never ran"
        tracked = _support_http._handler_threads(srv)
        assert tracked is not None, (
            "a server started through start() must be trackable")
        assert len(tracked) == 1, tracked
        assert tracked[0].daemon, (
            "the handler thread is not a daemon, so a stuck one would hold "
            "interpreter exit open for as long as it runs")
    finally:
        stop(srv, thread, connections=[sock])


def test_a_server_not_started_through_start_reports_that_it_cannot_track():
    """`None` is the answer for "cannot see", never for "saw none"."""
    srv = _echo_server()
    try:
        assert _support_http._handler_threads(srv) is None
    finally:
        srv.server_close()


def test_stop_closes_the_connections_it_is_given_and_reaps_the_handler():
    started, ended = threading.Event(), threading.Event()
    srv, thread = serve(lambda: _sse_server(started, ended))
    sock = _open_stream(srv.server_address[1])
    assert started.wait(PRESENCE_BACKSTOP_SECONDS), "the handler never ran"
    stop(srv, thread, connections=[sock])
    assert ended.is_set(), (
        "stop() returned while the handler was still running, so passing the "
        "connection did not end it and the reap did not wait for it")
    assert not thread.is_alive()
    assert _support_http._handler_threads(srv) == ()


def test_stop_names_a_surviving_handler_and_still_closes_the_listener():
    """The failure path: named, and no leaked listening socket.

    `server_close()` used to sit after the reap raised, so a teardown that
    failed left the port bound. Under `--dist load` that is one leaked listener
    per failure.
    """
    started, ended = threading.Event(), threading.Event()
    srv, thread = serve(lambda: _sse_server(started, ended))
    sock = _open_stream(srv.server_address[1])
    handler = None
    try:
        assert started.wait(PRESENCE_BACKSTOP_SECONDS), "the handler never ran"
        handler = _support_http._handler_threads(srv)[0]
        with pytest.raises(AssertionError, match="request-handler thread"):
            # `backstop` is overridden so this meta-test costs one second
            # rather than thirty. The failure it asserts is the same one.
            stop(srv, thread, backstop=1.0)
        # Structural, not a re-connect. Probing the port by connecting to it is
        # racy under `--dist load`, because a just-released ephemeral port can
        # be rebound by any other process on the runner between the release and
        # the probe. A closed socket answers -1 whoever else is listening.
        assert srv.socket.fileno() == -1, (
            "server_close() must have released the listening socket even "
            "though the teardown failed")
    finally:
        sock.close()
        assert ended.wait(PRESENCE_BACKSTOP_SECONDS), (
            "the handler never noticed its closed client")
        # `ended` is set in the handler's `finally`, several frames before
        # `process_request_thread` returns, so the event alone leaves a window
        # in which a teardown thread comparison still sees this handler alive.
        if handler is not None:
            # timing-budget: cleanup join for a handler just released above
            handler.join(timeout=PRESENCE_BACKSTOP_SECONDS)
    assert handler is not None and not handler.is_alive(), (
        "the handler thread must be gone, not merely past its own `finally`")
    assert not thread.is_alive(), (
        "stop() shut the server down before it failed, so its accept loop must "
        "have exited")


def test_a_wedged_accept_thread_names_the_handlers_that_are_still_running():
    """The accept-thread diagnostic must read the holder before `stop()` empties it.

    `_detach_tracked_threads` runs in the teardown `finally` and does
    `del holder[:]`. A diagnostic built after that always answered "every
    tracked handler thread has exited", which contradicted the surviving-handler
    line the reap had already appended to the same message, so a blown backstop
    described the wrong state.
    """
    started, ended = threading.Event(), threading.Event()
    srv, accept_thread = serve(lambda: _sse_server(started, ended))
    sock = _open_stream(srv.server_address[1])
    release = threading.Event()
    wedged = threading.Thread(target=lambda: release.wait(60),
                              name="wedged-accept-probe", daemon=True)
    wedged.start()
    handler = None
    try:
        assert started.wait(PRESENCE_BACKSTOP_SECONDS), "the handler never ran"
        handler = _support_http._handler_threads(srv)[0]
        # `backstop` is overridden so this meta-test costs two seconds rather
        # than sixty. The failure it asserts is the same one.
        with pytest.raises(AssertionError) as exc:
            stop(srv, wedged, backstop=1.0)
        message = str(exc.value)
        assert "request-handler thread" in message, message
        assert "surviving handler threads: " in message, message
        assert "wedged-accept-probe" in message, message
        assert "every tracked handler thread has exited" not in message, message
    finally:
        release.set()
        sock.close()
        # ONE budget for the whole teardown, not one per wait. Four separate
        # `PRESENCE_BACKSTOP_SECONDS` waits summed to the whole 120-second
        # pytest cap, so a probe that never exited spent the cap here and the
        # worker was killed mid-teardown. Every one of these is a cleanup wait
        # on something this block has just released, so they are one budget.
        deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
        wedged.join(timeout=remaining(deadline))
        assert ended.wait(remaining(deadline)), (
            "the handler never noticed its closed client")
        if handler is not None:
            handler.join(timeout=remaining(deadline))
        accept_thread.join(timeout=remaining(deadline))
    assert not handler.is_alive()
    assert not accept_thread.is_alive()


def test_a_wedged_accept_thread_on_a_non_threading_server_says_so():
    """An empty holder on a plain server is not "the handlers exited".

    Eight adopted call sites build a plain `socketserver.TCPServer` or
    `http.server.HTTPServer`, which serves each request on the accept thread
    itself. `start()` installs the holder on those too, so `_handler_threads`
    answers `()` rather than `None`, and the diagnostic told a wedged accept
    thread that "every tracked handler thread has exited" about a server that
    never had a handler thread to exit.
    """
    srv, accept_thread = serve(lambda: _sse_server(
        threading.Event(), threading.Event(),
        server_class=socketserver.TCPServer))
    release = threading.Event()
    wedged = threading.Thread(target=lambda: release.wait(60),
                              name="wedged-plain-accept-probe", daemon=True)
    wedged.start()
    try:
        assert _support_http._handler_threads(srv) == (), (
            "the premise: start() installs the holder, so this server reports "
            "an empty tuple rather than None")
        # `backstop` is overridden so this meta-test costs half a second rather
        # than thirty. The failure it asserts is the same one.
        with pytest.raises(AssertionError) as exc:
            stop(srv, wedged, backstop=0.5)
        message = str(exc.value)
        assert "wedged-plain-accept-probe" in message, message
        assert "every tracked handler thread has exited" not in message, message
        assert "not a threading server" in message, message
        assert "TCPServer" in message, message
    finally:
        release.set()
        # timing-budget: cleanup join for a probe this test has just released
        wedged.join(timeout=PRESENCE_BACKSTOP_SECONDS)
        # timing-budget: cleanup join for the accept loop stop() already ended
        accept_thread.join(timeout=PRESENCE_BACKSTOP_SECONDS)
    assert not accept_thread.is_alive()


def test_reap_reports_nothing_for_a_server_that_tracks_nothing():
    """Non-vacuity for the reap: the untracked branch must not invent a finding."""
    srv = _echo_server()
    try:
        assert _support_http._reap_handler_threads(srv, 0.1) == []
    finally:
        srv.server_close()


def test_a_connection_whose_close_fails_does_not_abort_the_teardown():
    class _Hostile:
        def close(self):
            raise OSError("already closed")

    srv, thread = serve(_echo_server)
    stop(srv, thread, connections=[_Hostile()])
    assert not thread.is_alive()
