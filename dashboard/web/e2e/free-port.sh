#!/usr/bin/env bash
# free-port.sh — pre-flight port reaper for the conversation-reader e2e smoke
# net (#281 S3). Run it right before `npx playwright test`.
#
# WHY THIS EXISTS
#   playwright.config.ts pins the suite to the dedicated port 8797 with
#   `reuseExistingServer: false` (deliberate: each run's webServer OWNS a fresh
#   dashboard + fixture state, never silently reusing arbitrary cache/config).
#   Playwright PROBES http://127.0.0.1:8797/ *before* it launches e2e/serve.sh —
#   so if a prior run on this (shared, self-hosted) runner HARD-FAILED and leaked
#   its `cctally dashboard` server, that stale listener is still bound when the
#   next run starts, and the probe aborts the whole run with
#       Error: http://127.0.0.1:8797/ is already used ...
#   a red build with nothing to do with the code under test. Observed 2026-07-17
#   (CI run 29609519* lineage): a run whose e2e assertion failed leaked its
#   server, and the NEXT push's e2e-reader job died on the occupied port ~30 min
#   later, across strictly-serial jobs on the one runner.
#
#   Because Playwright's probe runs BEFORE the webServer command, a cleanup
#   inside serve.sh would be too late — the reap has to happen here, ahead of
#   `npx playwright test`.
#
# WHAT IT DOES (not a reuse, a cleanup)
#   Port 8797 is e2e-dedicated (never 8789 dev / 8799 ui-qa), so on the runner
#   nothing else legitimately holds it: any listener is a stale leftover. We reap
#   it — SIGTERM for a graceful dashboard shutdown, then SIGKILL the stragglers —
#   and the suite goes on to start its own fresh server + fixture state. A clean
#   port is a no-op (exit 0). Failing to free the port after both signals is a
#   hard error (exit 1): better a loud, specific failure than a confusing
#   "already used" abort deep in Playwright.
#
# Usage: free-port.sh [PORT]   (PORT defaults to the e2e port 8797)
set -uo pipefail

PORT="${1:-8797}"

# lsof is native on macOS (the self-hosted e2e-reader runner) and present on the
# remote test runner; -sTCP:LISTEN restricts to the actual listener (not
# transient client sockets in TIME_WAIT).
listeners() { lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true; }

settle() {  # wait up to ~3s (10 * 0.3s) for the port to be released
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -z "$(listeners)" ] && return 0
    sleep 0.3
  done
  return 1
}

pids="$(listeners)"
if [ -z "$pids" ]; then
  echo "free-port: tcp:${PORT} already free"
  exit 0
fi

echo "free-port: reaping stale listener(s) on tcp:${PORT}: $(echo "$pids" | tr '\n' ' ')"
# shellcheck disable=SC2086  # word-splitting the pid list is intended
kill $pids 2>/dev/null || true
settle || true

pids="$(listeners)"
if [ -n "$pids" ]; then
  echo "free-port: listener(s) survived SIGTERM; SIGKILL: $(echo "$pids" | tr '\n' ' ')"
  # shellcheck disable=SC2086
  kill -9 $pids 2>/dev/null || true
  settle || true
fi

pids="$(listeners)"
if [ -n "$pids" ]; then
  echo "free-port: FAILED to free tcp:${PORT} — still held by: $(echo "$pids" | tr '\n' ' ')" >&2
  exit 1
fi

echo "free-port: tcp:${PORT} is now free"
exit 0
