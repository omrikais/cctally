#!/usr/bin/env bash
# Test for e2e/free-port.sh — proves the pre-flight port reaper actually kills a
# live listener and is a clean no-op when the port is already free.
#
# Uses an EPHEMERAL test port (NOT the real e2e port 8797) so running this on a
# shared runner can never collide with, or stomp, a genuine reader e2e run.
# Self-contained: no framework, exits non-zero on the first failed assertion.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FREE_PORT="$SCRIPT_DIR/free-port.sh"

# A quiet, uncommon loopback port distinct from the documented dev/e2e/ui-qa
# trio (8789 / 8797 / 8799) so a stray real server can't perturb the test.
PORT=8749

srv_pid=""
cleanup() { [ -n "$srv_pid" ] && kill -9 "$srv_pid" 2>/dev/null || true; }
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

listening() { lsof -ti "tcp:${PORT}" -sTCP:LISTEN >/dev/null 2>&1; }

# Ensure a clean slate even if a prior aborted run leaked something here.
bash "$FREE_PORT" "$PORT" >/dev/null 2>&1 || true

# --- Case 1: no-op (exit 0) when the port is already free ---
if ! bash "$FREE_PORT" "$PORT" >/dev/null; then
  fail "free-port.sh did not exit 0 on an already-free port"
fi
if listening; then
  fail "test setup: tcp:${PORT} unexpectedly held before the listener was started"
fi

# --- Case 2: reaps a live listener ---
python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
srv_pid=$!
for _ in $(seq 1 50); do
  listening && break
  sleep 0.1
done
listening || fail "test setup: listener never came up on tcp:${PORT}"

if ! bash "$FREE_PORT" "$PORT" >/dev/null; then
  fail "free-port.sh did not exit 0 after reaping the listener"
fi
if listening; then
  fail "tcp:${PORT} is STILL held after free-port.sh — the reaper did not kill it"
fi
if kill -0 "$srv_pid" 2>/dev/null; then
  fail "the listener process (pid ${srv_pid}) survived free-port.sh"
fi
srv_pid=""

echo "PASS: free-port.sh reaps a stale listener and no-ops when the port is free"
