"""Regression tests for `_QuietThreadingHTTPServer.handle_error` (spec §5).

When a dashboard client hangs up mid-response — a backgrounded, closed, or
reloaded tab — the per-request thread's socket write raises
`BrokenPipeError`/`ConnectionResetError`/`ConnectionAbortedError`, which
`socketserver` routes through `BaseServer.handle_error`. The stock
implementation dumps a full socket-write traceback to stderr for every such
disconnect; on a local dashboard that is expected and benign noise (the
original user report was repeated `BrokenPipeError` spam after hours idle).

The `_QuietThreadingHTTPServer` subclass swallows exactly that
"peer went away during our write" family silently and delegates everything
else to `super().handle_error` (which still prints the traceback). These
tests drive `handle_error` directly with a live exception in
`sys.exc_info()` — no socket round-trip needed — and assert:

  * the three connection-disconnect types return quietly, emitting nothing
    to stderr (the quiet branch), and
  * a generic `ValueError` delegates to the base implementation, which
    writes a traceback to stderr (proving the swallow is type-scoped, not a
    blanket suppression).

The server is bound to an ephemeral loopback port and closed in a finally,
so no real listener leaks.
"""
import io
import sys
from contextlib import redirect_stderr

import pytest

from conftest import load_script


def _server_class():
    """Load the script (pulls in the dashboard sibling) and fetch the class."""
    load_script()
    return sys.modules["_cctally_dashboard"]._QuietThreadingHTTPServer


def _make_server():
    """Bind a real `_QuietThreadingHTTPServer` to an ephemeral loopback port.

    Binding is trivial and avoids any `__new__` trickery; the caller closes it
    in a finally. A throwaway `BaseHTTPRequestHandler` keeps the constructor
    happy without ever servicing a request.
    """
    cls = _server_class()
    from http.server import BaseHTTPRequestHandler

    return cls(("127.0.0.1", 0), BaseHTTPRequestHandler)


@pytest.mark.parametrize(
    "exc_type",
    [BrokenPipeError, ConnectionResetError, ConnectionAbortedError],
)
def test_handle_error_swallows_client_disconnect(exc_type):
    """Each disconnect type returns without raising and prints nothing."""
    srv = _make_server()
    try:
        buf = io.StringIO()
        with redirect_stderr(buf):
            try:
                raise exc_type("client hung up")
            except exc_type:
                # Live exception is now in sys.exc_info(), exactly as it is when
                # socketserver routes a per-request failure through here.
                ret = srv.handle_error("req-stub", ("127.0.0.1", 12345))
        assert ret is None
        assert buf.getvalue() == "", (
            f"{exc_type.__name__} should be swallowed silently, got: "
            f"{buf.getvalue()!r}"
        )
    finally:
        srv.server_close()


def test_handle_error_delegates_other_exceptions():
    """A non-disconnect error delegates to super(), which writes a traceback."""
    srv = _make_server()
    try:
        buf = io.StringIO()
        with redirect_stderr(buf):
            try:
                raise ValueError("not a disconnect")
            except ValueError:
                srv.handle_error("req-stub", ("127.0.0.1", 12345))
        out = buf.getvalue()
        # BaseServer.handle_error prints a banner + the traceback to stderr.
        assert "ValueError" in out
        assert "not a disconnect" in out
    finally:
        srv.server_close()
