# Shared bounded server-teardown helper for the golden harnesses (issues #153,
# #1087).
#
# Several harnesses (cctally-dashboard-test, cctally-conversation-test,
# cctally-settings-api-test, cctally-explain-test) run a real `cctally
# dashboard` server on an ephemeral port and tear it down at the end of each
# scenario.
#
# #153: CPython can lose a single SIGTERM that races the dashboard server's
# main-thread wait (~0.04-0.07% of single signals), so a teardown that sends
# one SIGTERM and then waits without a bound can hang the whole suite. The stop
# is therefore bounded: SIGTERM, a grace of `--grace` seconds (5 by default),
# then SIGKILL, with a non-fatal WARN on stderr rather than a test failure, so
# the rare race cannot reintroduce flakiness.
#
# #1087: the harness itself never signals the server. Bash reaps a finished
# background job on its own, so `kill -0 "$pid"` followed by `kill -TERM
# "$pid"` can reach whichever process was given that number in between. Each
# server is started under a child-scope holder (bin/_lib-hold.sh), the
# server's parent and only reaper, which sends the TERM and the KILL while it
# holds the server unreaped:
#
#   hold_start "$dir" child -- "$BIN" dashboard … >"$log" 2>&1
#   hold_alive "$HOLD_PID"          # readiness loops: is the server running?
#   stop_server_bounded "$dir"      # teardown
#
# Child scope keeps the server in the harness's own process group, so an
# enclosing bin/cctally-test-all pool holder still covers it when the pool is
# cancelled. stop_server_bounded must run in the shell that started the
# holder (it waits for its own child). A second call, or a call for a
# directory whose server was never started, does nothing. Always returns 0.

# shellcheck source=bin/_lib-hold.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib-hold.sh"

stop_server_bounded() {   # DIR
    local dir="${1:-}" pid grace label
    [ -n "$dir" ] && [ -r "$dir/holder" ] || return 0
    pid=$(cat "$dir/holder" 2>/dev/null) || pid=""
    grace=$(cat "$dir/grace" 2>/dev/null) || grace=5
    label=$(cat "$dir/label" 2>/dev/null) || label=server
    rm -f "$dir/holder"
    [ -n "$pid" ] || return 0
    hold_stop "$pid" "$dir"
    if [ "$HOLD_PUBLISHED" != 1 ]; then
        echo "WARN: server $label (holder $dir) exited without publishing its settlement" >&2
        return 0
    fi
    if [ "$HOLD_KILLED" = 1 ]; then
        echo "WARN: server $label (holder $dir) ignored SIGTERM after ${grace}s; its holder sent SIGKILL (issue #153)" >&2
    fi
    if [ "$HOLD_COMPLETE" != 1 ]; then
        echo "WARN: server $label (holder $dir) did not exit after SIGKILL" >&2
    fi
    return 0
}
