"""#630 S2 — one definition of each HTTP helper the estate used to copy.

Before this module `_serve` existed in ten files, `_post_json` in six with
three different timeouts, and `_read_event` in two. A fix applied to one copy
changed one file.

Two ideas that were previously one. A PRESENCE backstop bounds how long a call
waits for something that should arrive; raising it costs nothing on a healthy
run, because the call returns as soon as the thing arrives. An ABSENCE window
is the observation period of a negative assertion; it is the test's own
semantics and is spent in full every time, so it must stay short. Conflating
them turns a three-second negative assertion into a thirty-second one.

The underscore prefix keeps this file out of collection and out of the
`test_*.py` estate scan. There is deliberately no `tests/__init__.py`: the
suite runs `python3 -m pytest` from the repository root, so `-m` puts that root
on `sys.path` and `tests` resolves as a namespace package.
"""
from __future__ import annotations

import http.client
import json
import os
import socket as _socket
import socketserver
import threading
import time
import weakref

# Derived, not chosen: the slowest pytest node observed under real three-lane
# contention was 23.1 s, and the pytest cap is 120 s.
PRESENCE_BACKSTOP_SECONDS = 30.0

# #630 S2 calibration seam, and nothing else reads it. `bin/cctally-test-load-
# invariance --calibrate slow-transport` arms it so the contended lane can be
# SHOWN to fail: a lane that has never failed is no evidence that it would. It
# is off in every ordinary run, the five contention rounds included.
#
# The fault is deliberately two-sided. A delay alone would prove nothing now
# that every budget in the estate is the 30-second presence backstop, and a
# delay large enough to blow that would make calibration take longer than the
# thing it calibrates. Delaying the transport AND shrinking the budget it is
# measured against reproduces the same condition — a transport slower than its
# backstop — in a fraction of a second, and it fails through the helpers' own
# diagnostic rather than through a bare timeout.
_FAULT = os.environ.get("CCTALLY_LOAD_INVARIANCE_FAULT", "")
_FAULT_SLOW_TRANSPORT = "slow-transport"
_FAULT_DELAY_SECONDS = 0.5
_FAULT_BACKSTOP_SECONDS = 0.1


#: The smallest budget `remaining` will hand back once a shared deadline has
#: run out. Zero or a negative number is a non-blocking attempt rather than a
#: short wait, so an exhausted deadline would turn the last wait of a sequence
#: into a poll that reports a wedge the moment it is asked.
EXHAUSTED_DEADLINE_FLOOR_SECONDS = 1.0


def remaining(deadline, *, floor=EXHAUSTED_DEADLINE_FLOOR_SECONDS):
    """Seconds left of a SHARED deadline, never zero or negative.

    Several waits in one test are one budget, not one budget each. Four
    consecutive `PRESENCE_BACKSTOP_SECONDS` waits sum to 120 seconds, which is
    the whole pytest cap: the worker is killed before the last of them can
    report anything, which is the undiagnosable red #630 S1 removed. Written
    against one deadline the same four waits cost thirty seconds between them,
    and the first one to find the collaborator wedged is the one that says so.

    `deadline` is a `time.monotonic()` reading plus the budget, so it is safe
    across a wall-clock change. The floor keeps an exhausted deadline blocking
    briefly rather than not at all, because a zero timeout is a different
    operation from a short one.
    """
    return max(floor, deadline - time.monotonic())


def _transport_backstop(backstop):
    """The backstop a transport really gets, and the delay it really pays."""
    if _FAULT != _FAULT_SLOW_TRANSPORT:
        return backstop
    time.sleep(_FAULT_DELAY_SECONDS)
    return _FAULT_BACKSTOP_SECONDS


class _TrackedHandlerThreads(list):
    """Every request-handler thread a server started, daemon ones included.

    `socketserver.ThreadingMixIn` keeps its own list and that list cannot be
    used here. Measured on both supported interpreters — the pinned Homebrew
    `python@3.13` (3.13.15) and 3.14.7 — `ThreadingHTTPServer.block_on_close`
    is `True` independently of `daemon_threads`, so `process_request` does
    create the instance-level `_threads` list on the first request — but
    `_Threads.append` returns without recording a thread whose `daemon` flag is
    set, and `ThreadingHTTPServer` sets `daemon_threads = True`. On the
    estate's servers the stdlib list therefore exists, is a `list`, and stays
    EMPTY however many handlers are running. Both halves were measured on both
    interpreters, so the argument does not rest on the newer one.

    The handlers stay daemon threads, and this holder is what makes that safe
    to keep. The argument does not depend on an interpreter version. A
    non-daemon handler that stops noticing its closed client blocks interpreter
    exit for as long as it runs, because the interpreter waits for every
    non-daemon thread and because `ThreadingMixIn.server_close` joins
    `self._threads` with no timeout at all. `Thread.daemon` cannot be set once
    a thread has started, and `pytest-timeout` bounds a test rather than
    interpreter shutdown, so a stuck non-daemon handler produces a worker that
    never exits — a CI hang in place of a readable red. Measured directly, a
    six-second non-daemon thread held interpreter exit for six seconds. There
    is also no escape hatch: neither the pinned Homebrew `python@3.13`
    (3.13.15) nor 3.14.7 still defines `threading._shutdown_locks`, so a stuck
    thread's shutdown lock cannot be discarded on either. Tracking daemon
    handlers here names them just as precisely and leaves them killable.

    `join()` is a no-op because `ThreadingMixIn.server_close` calls it with no
    timeout at all. `stop()` performs the bounded join instead.
    """

    def append(self, thread):
        self[:] = [t for t in self if t.is_alive()]
        list.append(self, thread)

    def join(self):
        pass


def start(server):
    """Start `server`'s accept loop on a daemon thread and return the thread.

    No readiness wait is needed or added: a `ThreadingHTTPServer` binds and
    listens inside its constructor, so the port is accepting connections
    before this function starts the thread. The one "started" event the estate
    had fired immediately before `serve_forever()`, so it proved scheduling
    rather than listener readiness.

    This is the primitive for a server the caller has already constructed —
    which is most of the estate, because the server class and its wiring differ
    per file even where the thread lifecycle does not.

    Installing the tracking holder here, before the first request, is what
    lets `stop()` name a surviving handler thread. `ThreadingMixIn` does
    `vars(self).setdefault('_threads', _Threads())`, so a holder already in the
    instance dict is kept.
    """
    server._threads = _TrackedHandlerThreads()
    # `socketserver.ThreadingTCPServer.daemon_threads` is False and
    # `http.server.ThreadingHTTPServer`'s is True, so the flag is set here
    # rather than inherited from whichever class the caller built.
    if hasattr(server, "daemon_threads"):
        server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name=f"support-http-accept-{id(server):x}")
    thread.start()
    return thread


def serve(server_factory):
    """Construct a server with `server_factory()` and start its accept loop."""
    srv = server_factory()
    return srv, start(srv)


def serve_dashboard(ns, *, host="127.0.0.1", port=0,
                    handler_key="DashboardHTTPHandler",
                    server_key="ThreadingHTTPServer",
                    server_class=None, configure=None):
    """Start the loaded script's dashboard handler on an ephemeral port.

    Returns `(server, thread, port)` — the exact three-tuple the copied
    `_serve` and `_serve_once` helpers returned, so adopting this is a
    call-site rename rather than a rewrite.

    `configure(server)` runs after the constructor binds and before the accept
    thread starts, which is where a file that overrides `handle_error` or
    stashes diagnostic state on the server does that work.
    """
    cls = server_class if server_class is not None else ns[server_key]

    def factory():
        srv = cls((host, port), ns[handler_key])
        if configure is not None:
            configure(srv)
        return srv

    srv, thread = serve(factory)
    return srv, thread, srv.server_address[1]


# Derived the same way as the backstop, from the handler's own loop rather
# than from taste. The `/api/events` handler blocks in
# `q.get(timeout=_SSE_KEEPALIVE_SECONDS)` and learns that its client has gone
# only when the keep-alive write after that timeout fails — and on a socket the
# peer has closed the FIRST write still succeeds, so it takes two timeouts. At
# the shipped fifteen seconds that is thirty, which is the whole presence
# backstop, so `stop()` cannot reap the handler at all.
SSE_KEEPALIVE_FOR_TESTS = 0.5


def shorten_sse_keepalive(ns, monkeypatch, *, seconds=SSE_KEEPALIVE_FOR_TESTS):
    """Shorten the `/api/events` keep-alive period for one test.

    Must be called AFTER `load_script()`, which drops and re-imports every
    `_cctally_*` sibling: a patch applied before it lands on a module object
    that is about to be replaced.
    """
    monkeypatch.setattr(
        ns["_cctally_dashboard"], "_SSE_KEEPALIVE_SECONDS", seconds)


def _handler_threads(server):
    """The live request-handler threads this module is tracking, or `None`.

    `None` means the server never went through `start()`, so no holder was
    installed and no handler can be named. That is a different fact from "there
    are no survivors", and the caller reports them differently rather than
    claiming there were none when it simply cannot see any. The distinction was
    previously drawn from the stdlib `_threads` attribute, which answered the
    question wrongly: see `_TrackedHandlerThreads` for the measurement.
    """
    holder = getattr(server, "_threads", None)
    if not isinstance(holder, _TrackedHandlerThreads):
        return None
    return tuple(t for t in tuple(holder) if t.is_alive())


def _reap_handler_threads(server, backstop):
    """Join the tracked request-handler threads within `backstop`.

    Returns the problems found, rather than raising them, so `stop()` can close
    the listening socket before it reports anything.
    """
    tracked = _handler_threads(server)
    if not tracked:
        return []
    deadline = time.monotonic() + backstop
    for handler in tracked:
        handler.join(timeout=max(0.0, deadline - time.monotonic()))
    survivors = [t for t in tracked if t.is_alive()]
    if not survivors:
        return []
    return [
        f"{len(survivors)} request-handler thread(s) were still running "
        f"{backstop}s after shutdown(): "
        + ", ".join(f"{t.name!r}(ident={t.ident})" for t in survivors)
        + ". `shutdown()` ends the accept loop but not a handler already "
        "inside a handler; closing the client socket is what ends the "
        "live-tail watch loop, so pass it as stop(..., connections=[...])."
    ]


def _detach_tracked_threads(server):
    """Empty the handler list so `server_close()` cannot block on it.

    `ThreadingMixIn.server_close` calls `self._threads.join()`, which joins
    every recorded thread with NO timeout. `_reap_handler_threads` has already
    done the bounded join by the time this runs, so the unbounded one would add
    only the possibility of a hang.
    """
    holder = vars(server).get("_threads")
    if isinstance(holder, list):
        del holder[:]
    server.block_on_close = False


def _accept_thread_problem(server, survivors, thread, backstop):
    """Describe a wedged accept thread and the handlers still running beside it.

    `survivors` is the reading `stop()` took BEFORE `_detach_tracked_threads`
    emptied the holder, not the server. Reading the holder here instead
    returned an empty tuple every time, so this always reported that every
    handler had exited — contradicting the surviving-handler line
    `_reap_handler_threads` had already appended to the same message.

    An empty tuple has two causes and they are reported separately. `start()`
    installs the holder on every server it is given, so a plain
    `socketserver.TCPServer` or `http.server.HTTPServer` — which eight adopted
    call sites build — reports `()` because it serves each request on the
    accept thread and never had a handler thread at all.
    """
    if survivors is None:
        detail = ("; this server was not started through start(), so no "
                  "handler thread was tracked and none can be named here")
    elif survivors:
        detail = "; surviving handler threads: " + ", ".join(
            f"{t.name!r}(ident={t.ident}, daemon={t.daemon})"
            for t in survivors
        )
    elif not isinstance(server, socketserver.ThreadingMixIn):
        detail = (f"; {type(server).__name__} is not a threading server, so it "
                  "serves each request on the accept thread and has no handler "
                  "threads — the wedge is in the accept loop or in the request "
                  "it is still serving")
    else:
        detail = "; every tracked handler thread has exited"
    return (
        f"thread {thread.name!r} (ident={thread.ident}, "
        f"daemon={thread.daemon}) was still alive "
        f"{backstop}s after shutdown(){detail}; "
        f"a surviving handler thread is exactly the leak the isolation "
        f"plugin refuses. Pass every SSE or keep-alive connection this "
        f"test opened as stop(..., connections=[...])."
    )


def stop(server, thread, *, connections=(), backstop=PRESENCE_BACKSTOP_SECONDS):
    """Shut the server down and prove its threads are gone.

    `connections` is not optional bookkeeping. `shutdown()` ends the accept
    loop, but a request-handler thread already inside a handler keeps running;
    the live-tail SSE handler sits in an unbounded watch loop that no
    server-side call interrupts. Closing the client socket is what ends it, so
    every connection the test opened is closed here, before the accept thread
    is joined.

    An `http.client.HTTPConnection` alone is NOT enough, and this is measured
    rather than assumed. `socket.makefile()` takes an io-reference, so
    `HTTPConnection.close()` while its `HTTPResponse` is still open only marks
    the socket closed: the file descriptor survives, the server's writes keep
    succeeding, and the handler never learns its client has gone. In a direct
    probe the handler outlived a connection-only close indefinitely and ended
    0.03 s after the response was closed too. Pass both.

    `server_close()` runs from a `finally`, so the listening socket is released
    on the failure path too. An earlier form raised out of the handler reap and
    leaked the socket, which under `--dist load` left a bound port behind for
    every failure.

    `backstop` exists so this module's own meta-tests can assert the failure
    without spending the full presence backstop waiting for it. Call sites
    leave it at the default.
    """
    for conn in connections:
        try:
            conn.close()
        except OSError:
            pass
    problems = []
    survivors = None
    try:
        if server is not None:
            server.shutdown()
            problems.extend(_reap_handler_threads(server, backstop))
    finally:
        if server is not None:
            survivors = _handler_threads(server)
            _detach_tracked_threads(server)
            server.server_close()
    if thread is not None:
        thread.join(timeout=backstop)
        if thread.is_alive():
            problems.append(
                _accept_thread_problem(server, survivors, thread, backstop))
    if problems:
        raise AssertionError("\n".join(problems))


def post_json(port, path, payload, *, backstop=PRESENCE_BACKSTOP_SECONDS,
              host="127.0.0.1", origin_host=None, headers=None, server=None):
    """POST `payload` as JSON and return `(status, decoded_body)`.

    The matched `Host` and `Origin` pair is the dashboard's loopback CSRF
    contract, and every one of the six copies this replaces sent it, with
    `skip_host` and `skip_accept_encoding` set so the client adds nothing of
    its own. `origin_host` supplies a deliberately mismatched origin;
    `headers` adds or overrides individual headers, and a header mapped to
    `None` is omitted entirely, which is how a test sends no `Origin` at all.

    `server` is optional and only feeds the diagnostic: when a call blows its
    backstop, the accept-thread state and any captured handler errors say more
    than the socket error does.

    On a body that is not JSON the raw decoded text is returned rather than
    `None`, because a helper that answers `None` for both an empty body and an
    HTML error page hides the error page.
    """
    backstop = _transport_backstop(backstop)
    body = json.dumps(payload).encode("utf-8")
    host_header = f"{host}:{port}"
    sent = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Host": host_header,
        "Origin": f"http://{origin_host or host_header}",
    }
    sent.update(headers or {})
    conn = http.client.HTTPConnection(host, port, timeout=backstop)
    started = time.monotonic()
    phase = "building the request"
    try:
        conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        for name, value in sent.items():
            if value is None:
                continue
            conn.putheader(name, value)
        phase = "sending the request body"
        conn.endheaders()
        conn.send(body)
        phase = "waiting for the response headers"
        resp = conn.getresponse()
        phase = "reading the response body"
        raw = resp.read()
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        elapsed = time.monotonic() - started
        raise AssertionError(
            f"POST {path} to {host}:{port} failed while {phase}, "
            f"{elapsed:.2f}s into a {backstop}s presence backstop: "
            f"{type(exc).__name__}: {exc}{_server_state(server)}"
        ) from exc
    finally:
        conn.close()
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return resp.status, None
    try:
        return resp.status, json.loads(text)
    except ValueError:
        return resp.status, text


def _server_state(server):
    """A one-line description of a server's threads and captured errors."""
    if server is None:
        return ""
    thread = getattr(server, "_test_thread", None)
    errors = getattr(server, "_test_handler_errors", None)
    survivors = _handler_threads(server)
    if survivors is None:
        handlers = "untracked (server not started through start())"
    else:
        handlers = ", ".join(f"{t.name!r}" for t in survivors) or "none alive"
    return (
        f"; accept_thread_alive={bool(thread and thread.is_alive())}"
        f"; handler_threads={handlers}"
        f"; handler_errors={errors if errors else 'none'}"
    )


#: Bytes this reader has already taken off a socket and not yet returned.
#:
#: A `recv` returns whatever the socket holds, which is not one frame. The
#: live-tail server writes `ready` and `baselined` back to back, so the two
#: routinely arrive in ONE segment — and a reader that discarded its buffer
#: after matching the first frame dropped the second permanently. The next call
#: then spent its whole thirty-second backstop watching keep-alives go by.
#:
#: `bin/cctally-test-load-invariance` found it: two of three contended rounds
#: failed in `tests/test_codex_dashboard_conversation_events.py`, in tests the
#: ordinary suite passes every time. Load makes it MORE likely rather than
#: less, because a reader descheduled under contention drains more slowly and
#: gives the two writes longer to coalesce.
#:
#: Keyed weakly by socket, so a closed connection's residue is collected with
#: it and nothing has to be unregistered.
_RESIDUE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _match_frame(buf, marker):
    """`(frame, remainder)` for the first complete `marker` frame, or `None`.

    The remainder is every OTHER byte in the buffer, in the order it arrived —
    not merely the bytes after the match. Taking only the tail discarded
    whatever preceded the matched frame, which contradicts `_drain_for`'s claim
    that what it read and did not return stays with the socket, and it made an
    out-of-order negative assertion pass vacuously: read `beta` first and the
    `alpha` frame that really arrived is gone, so a later
    `read_no_event(marker="alpha")` proves nothing about the server.

    The cut is frame-aligned rather than marker-aligned, so the two halves
    rejoin as a well-formed stream. A marker that happens to appear inside a
    frame's data would otherwise splice a truncated frame onto the front of the
    residue, where it could match again.
    """
    needle = marker.encode("utf-8")
    hit = buf.find(needle)
    if hit == -1:
        return None
    blank = buf.find(b"\n\n", hit)
    if blank == -1:
        return None
    boundary, width = -1, 0
    for separator in (b"\n\n", b"\r\n\r\n"):
        # Both spellings, because the first frame on a live stream is preceded
        # by the HTTP response headers, and those end `\r\n\r\n` — which does
        # NOT contain `\n\n`. Searching for the bare form alone found no
        # boundary at all there, so the whole header block was read as part of
        # the first frame and `frame.startswith("event: ready")` failed.
        found = buf.rfind(separator, 0, hit)
        if found > boundary:
            boundary, width = found, len(separator)
    start = 0 if boundary == -1 else boundary + width
    frame = buf[start:blank].decode("utf-8", "replace")
    return frame, buf[:start] + buf[blank + 2:]


def _drain_for(sock, marker, deadline_seconds):
    """Read until a frame containing `marker` completes, or time runs out.

    Returns `(frame_or_None, buffered_tail, elapsed)`. Whatever was read and
    not returned stays with the socket, so the next call sees it.

    The hand-back is in a `finally` because the buffer is POPPED on the way in.
    `sock.settimeout(...)` raises `OSError` on a closed socket, and that raise
    used to leave with the residue already popped and never restored — so a
    later `read_no_event` on the same socket would have observed an empty
    buffer and passed by having nothing to look at. That is vacuous rather than
    wrong, which is the failure this session exists to remove.
    """
    buf = _RESIDUE.pop(sock, b"")
    started = time.monotonic()
    try:
        while True:
            matched = _match_frame(buf, marker)
            if matched is not None:
                frame, remainder = matched
                tail = buf.decode("utf-8", "replace")[-512:]
                buf = remainder
                return frame, tail, time.monotonic() - started
            remaining_s = deadline_seconds - (time.monotonic() - started)
            if remaining_s <= 0:
                break
            sock.settimeout(min(0.5, remaining_s))
            try:
                chunk = sock.recv(4096)
            except (_socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        return None, buf.decode("utf-8", "replace")[-512:], \
            time.monotonic() - started
    finally:
        _RESIDUE[sock] = buf


def read_event(sock, *, marker, backstop=PRESENCE_BACKSTOP_SECONDS):
    """Wait for an SSE frame containing `marker`. Returns as soon as it lands."""
    backstop = _transport_backstop(backstop)
    frame, tail, elapsed = _drain_for(sock, marker, backstop)
    if frame is None:
        raise AssertionError(
            f"no SSE frame matching {marker!r} arrived within the {backstop}s "
            f"presence backstop (waited {elapsed:.2f}s); "
            f"last 512 buffered bytes: {tail!r}"
        )
    return frame


def read_no_event(sock, *, marker, window):
    """Assert no frame containing `marker` arrives within `window` seconds.

    `window` is the assertion's observation period and is spent in full. It is
    deliberately a separate parameter from the presence backstop, and the
    timing-budget guard treats a named absence window as annotated.
    """
    frame, _tail, _elapsed = _drain_for(sock, marker, window)
    if frame is not None:
        raise AssertionError(
            f"an SSE frame matching {marker!r} arrived within the {window}s "
            f"absence window, which the test asserts must not happen; "
            f"frame: {frame!r}"
        )
