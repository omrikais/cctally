"""Port-selection regression tests for bin/build-readme-screenshots.sh (#367).

Drives the pipeline through its README_SCREENSHOTS_SELFTEST dev hook, so no
`freeze`, Playwright, or marketing fixture is needed. Every subprocess call
passes an explicit timeout so a regression fails the test instead of hanging
the suite.
"""
from __future__ import annotations

import contextlib
import errno
import http.server
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "bin" / "build-readme-screenshots.sh"
DEFAULT_PORT = 8789


class _Ok200Handler(http.server.BaseHTTPRequestHandler):
    """Minimal always-200 responder."""

    def do_GET(self):  # noqa: N802 - stdlib callback name
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence stderr noise into pytest
        pass


@contextlib.contextmanager
def default_port_occupied():
    """Guarantee a real HTTP responder on 127.0.0.1:8789 for the duration.

    A bare listening socket is NOT enough: the pre-change script probes with
    `curl -fsS` and no timeout, so a socket that accepts but never answers
    makes the old code hang instead of exiting 1 — the RED run would then fail
    for the wrong reason and prove nothing about the diagnosed refusal.

    On the maintainer's machine the live dashboard already holds the port, so
    bind fails EADDRINUSE and that IS the precondition. EACCES is not
    accepted: it means we could not bind, not that a listener exists.
    """
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", DEFAULT_PORT), _Ok200Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            yield
            return
        pytest.skip(f"cannot establish a listener on {DEFAULT_PORT}: {exc}")
    with srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            yield
        finally:
            srv.shutdown()


def free_port() -> int:
    """An ephemeral port that was free a moment ago."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def holding(port: int):
    """Hold `port` with a real HTTP responder."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Ok200Handler)
    with srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            yield
        finally:
            srv.shutdown()


def run_selftest(mode: str, env_extra: dict | None = None, timeout: float = 60):
    env = dict(os.environ)
    env["README_SCREENSHOTS_SELFTEST"] = mode
    env.pop("DASHBOARD_PORT", None)          # never inherit the operator's pin
    env.update(env_extra or {})
    return subprocess.run(
        [str(SCRIPT)], env=env, capture_output=True, text=True, timeout=timeout
    )


# --- Case A: the regression ------------------------------------------------
def test_busy_default_port_auto_selects():
    """With no DASHBOARD_PORT, a busy 8789 must be a non-event (#367)."""
    with default_port_occupied():
        r = run_selftest("port")
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "port=0" in r.stdout.splitlines(), r.stdout


# --- Case B: an explicit pin on a busy port still refuses ------------------
def test_explicit_busy_port_refuses():
    p = free_port()
    with holding(p):
        r = run_selftest("port", {"DASHBOARD_PORT": str(p)})
    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert str(p) in r.stderr and "already in use" in r.stderr, r.stderr


# --- Case C: an explicit pin on a free port wins exactly -------------------
def test_explicit_free_port_is_pinned():
    p = free_port()
    r = run_selftest("port", {"DASHBOARD_PORT": str(p)})
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert f"port={p}" in r.stdout.splitlines(), r.stdout


# --- A non-numeric pin is rejected cleanly, not via a Python traceback -----
def test_non_numeric_port_is_rejected_cleanly():
    r = run_selftest("port", {"DASHBOARD_PORT": "not-a-port"})
    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "must be a number" in r.stderr, r.stderr
    # The whole point of the guard: int() must never throw out of port_busy,
    # because that traceback lands in the promote log looking like a crash of
    # the release tool itself.
    assert "Traceback" not in r.stderr, r.stderr


# --- dashboard-launch cases (D, D2, F, G, H) -------------------------------
STUB_SRC = '''#!/usr/bin/env python3
"""Stand-in for `cctally dashboard`, driven by CCTALLY_STUB_MODE.

Modes: serve (normal), nobanner, die (banner then exit), unresponsive
(binds and accepts via backlog but never answers HTTP).

nobanner writes its diagnostic and exits before the heavyweight imports and
the bind. That ordering is load-bearing, not tidiness: the caller's banner
poll re-reads the log and then breaks on observed child death, so exiting
makes "the line is in the log" happen-before "the caller gives up". A variant
that printed and then stayed alive would instead be read at a 0.1s-tick
deadline, which a cold interpreter under a loaded CI box loses (measured: a
2.5s startup tail against a 0.5s window).
"""
import os, sys

mode = os.environ.get("CCTALLY_STUB_MODE", "serve")
if mode == "nobanner":
    # Emit SOMETHING that is not a banner, so the failure path's log echo is
    # pinned against real captured content rather than only the empty-log
    # placeholder. This is what a real dashboard dying mid-startup looks like.
    print("STUB-DIAGNOSTIC: no banner for you", flush=True)
    sys.exit(0)

import argparse, http.server, pathlib, threading, time

p = argparse.ArgumentParser()
p.add_argument("command")
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--port", type=int, default=0)
p.add_argument("--no-browser", action="store_true")
args, _unknown = p.parse_known_args()


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer((args.host, args.port), H)
bound = srv.server_address[1]

rec = os.environ.get("CCTALLY_STUB_RECORD")
if rec:
    pathlib.Path(rec).write_text("requested=%s\\nbound=%s\\n" % (args.port, bound))

if mode == "silent":
    pass                       # writes nothing at all — the empty-log branch
else:
    # Byte-identical to bin/_cctally_dashboard.py's loopback banner.
    print("dashboard: serving http://localhost:%d/ \\u2014 Ctrl-C to stop" % bound, flush=True)

if mode == "die":
    sys.exit(0)
if mode == "unresponsive":
    while True:
        time.sleep(1)          # bound + backlog, but never serviced
srv.serve_forever()
'''


@pytest.fixture
def stub(tmp_path):
    path = tmp_path / "cctally-stub"
    path.write_text(STUB_SRC)
    path.chmod(0o755)
    return path


def run_dashboard_selftest(stub_path, tmp_path, mode="serve",
                           dashboard_port=None, wait_ticks=None, timeout=60):
    extra = {
        "CCTALLY_BIN": str(stub_path),
        "CCTALLY_STUB_MODE": mode,
        "CCTALLY_STUB_RECORD": str(tmp_path / "stub-record.txt"),
    }
    if dashboard_port is not None:
        extra["DASHBOARD_PORT"] = str(dashboard_port)
    if wait_ticks is not None:
        # Both budgets: a readiness tick costs up to 2.1s (curl's own
        # --max-time), so leaving READY_TICKS at its 30 default would make the
        # unresponsive case take ~60s instead of ~10s.
        extra["README_SCREENSHOTS_WAIT_TICKS"] = str(wait_ticks)
        extra["README_SCREENSHOTS_READY_TICKS"] = str(wait_ticks)
    return run_selftest("dashboard", extra, timeout=timeout)


def _record(tmp_path) -> dict:
    text = (tmp_path / "stub-record.txt").read_text()
    return dict(line.split("=", 1) for line in text.splitlines() if line)


# --- Case D: auto path parses the port the kernel actually assigned --------
def test_auto_port_parses_bound_port(stub, tmp_path):
    r = run_dashboard_selftest(stub, tmp_path)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    rec = _record(tmp_path)
    assert rec["requested"] == "0", rec
    assert f"url=http://127.0.0.1:{rec['bound']}/" in r.stdout.splitlines(), r.stdout


# --- Case D2: an explicit pin must reach the LAUNCH, not just the log line -
def test_explicit_port_reaches_the_dashboard(stub, tmp_path):
    p = free_port()
    r = run_dashboard_selftest(stub, tmp_path, dashboard_port=p)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    rec = _record(tmp_path)
    assert rec["requested"] == str(p), rec
    assert rec["bound"] == str(p), rec
    assert f"url=http://127.0.0.1:{p}/" in r.stdout.splitlines(), r.stdout


# --- Case F: output, but never a banner -----------------------------------
def test_no_banner_fails_loudly(stub, tmp_path):
    """Deliberately NOT tick-pinned: the stub exits right after it prints.

    The poll breaks as soon as it observes the child gone, so the wait budget
    is a ceiling this never spends (~0.4s end to end) rather than a deadline
    the child has to beat. Pinning it low is what made this flaky on CI: a
    cold interpreter under load took up to 2.5s to reach its first write and
    the log was still empty when a 0.5s window closed.
    """
    r = run_dashboard_selftest(stub, tmp_path, mode="nobanner")
    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "did not report a bound port" in r.stderr, r.stderr
    # Pin the CONTENT, not just the prefix: asserting only "dashboard|" would
    # be satisfied by the empty-log "(no output captured)" placeholder and
    # would never prove the captured log actually reaches stderr.
    assert "dashboard| STUB-DIAGNOSTIC: no banner for you" in r.stderr, r.stderr


# --- Case F2: the log is genuinely empty -> the placeholder branch --------
def test_silent_dashboard_reports_empty_log(stub, tmp_path):
    """The other half of dump_dashboard_log: nothing captured at all.

    `sed` over an empty file prints no lines, so without the placeholder a
    failure here would emit a bare error with no log section — indistinguishable
    from "the log was swallowed".
    """
    r = run_dashboard_selftest(stub, tmp_path, mode="silent", wait_ticks=5)
    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "did not report a bound port" in r.stderr, r.stderr
    assert "dashboard| (no output captured)" in r.stderr, r.stderr


# --- Case G: banner then the child dies ----------------------------------
def test_banner_then_child_death_fails(stub, tmp_path):
    r = run_dashboard_selftest(stub, tmp_path, mode="die", wait_ticks=5)
    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "dashboard|" in r.stderr, "captured dashboard log must be echoed"


# --- Case H: banner, accepts TCP, never answers — must NOT hang -----------
def test_unresponsive_dashboard_does_not_hang(stub, tmp_path):
    r = run_dashboard_selftest(stub, tmp_path, mode="unresponsive",
                               wait_ticks=5, timeout=45)
    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "dashboard|" in r.stderr, "captured dashboard log must be echoed"


# --- Case E: the REAL dashboard, so banner drift cannot go unnoticed ------
def test_real_dashboard_banner_is_parseable(tmp_path):
    """Drive the actual bin/cctally through the hook.

    The stub cases pin the parser against a format WE wrote down; only this
    one pins it against the format production emits. Fully isolated from the
    operator's real data: CCTALLY_DATA_DIR alone is not enough — sync_cache
    discovers transcripts via CLAUDE_CONFIG_DIR and a Codex sync walks
    CODEX_HOME, so all three are pinned at empty scratch roots plus the
    dev-autodetect suppressor.
    """
    home = tmp_path / "home"
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    (claude / "projects").mkdir(parents=True)
    codex.mkdir(parents=True)
    (home / ".local" / "share" / "cctally").mkdir(parents=True)

    r = run_selftest(
        "dashboard",
        {
            "CCTALLY_BIN": str(REPO_ROOT / "bin" / "cctally"),
            "HOME": str(home),
            "CCTALLY_DATA_DIR": str(home / ".local" / "share" / "cctally"),
            "CLAUDE_CONFIG_DIR": str(claude),
            "CODEX_HOME": str(codex),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        },
        timeout=180,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    urls = [ln for ln in r.stdout.splitlines() if ln.startswith("url=")]
    assert len(urls) == 1, r.stdout
    port = int(urls[0].rsplit(":", 1)[1].rstrip("/"))
    assert 1024 < port < 65536, urls[0]
    assert port != 8789, "auto path must not land on the hardcoded default"
