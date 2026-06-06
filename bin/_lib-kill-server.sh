# Shared bounded server-teardown helper for the golden harnesses (issue #153).
#
# Several harnesses (cctally-dashboard-test, cctally-conversation-test,
# cctally-settings-api-test) background a real `cctally dashboard` server on
# an ephemeral port and tear it down at the end of each scenario. The old
# idiom was an UNBOUNDED `kill "$pid"; wait "$pid"`.
#
# That is unsafe: CPython can lose a single SIGTERM that races the dashboard
# server's main-thread `threading.Event.wait()` (the server is woken only by
# its SIGTERM/SIGINT handler via `stop.set()`). Empirically ~0.04-0.07% of
# single signals are dropped — the Python-level handler never runs, or its
# `set()` notify fires before the waiter registers — and recovery needs a
# *second* signal. Interactive Ctrl-C never wedges (the user sends more than
# one), but a harness that sends exactly one SIGTERM then `wait`s forever
# hangs the entire suite (issue #153: a `--host 0.0.0.0` banner-scenario
# server survived 30+ minutes; SIGTERM was "ignored", only SIGKILL cleared it).
#
# `kill_server_bounded` makes teardown guaranteed: SIGTERM, poll for a
# graceful exit up to `grace` seconds, then escalate to SIGKILL (uncatchable
# at the kernel level — no signal-handling state can defeat it). A wedge is
# surfaced via a non-fatal WARN on stderr (visible in harness logs) rather
# than failing the run, so the rare race can't reintroduce flakiness.
#
# Usage:  kill_server_bounded <pid> [grace_seconds]   # grace defaults to 5
#
# `wait "$pid"` reaps the process when it is a child of the calling shell
# (always the case at the harness teardown sites); for a non-child pid it
# returns immediately and is harmless. Always returns 0.

kill_server_bounded() {
    local pid="$1" grace="${2:-5}"
    [ -n "$pid" ] || return 0

    # Already gone? Reap if it's a finished child, then we're done.
    if ! kill -0 "$pid" 2>/dev/null; then
        wait "$pid" 2>/dev/null || true
        return 0
    fi

    kill -TERM "$pid" 2>/dev/null || true

    # Poll for a graceful exit at 0.1s granularity.
    local ticks=$(( grace * 10 )) waited=0
    while [ "$waited" -lt "$ticks" ]; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
        waited=$(( waited + 1 ))
    done

    # Still alive after the grace window — escalate to the uncatchable SIGKILL.
    if kill -0 "$pid" 2>/dev/null; then
        echo "WARN: server pid $pid ignored SIGTERM after ${grace}s; escalating to SIGKILL (issue #153)" >&2
        kill -KILL "$pid" 2>/dev/null || true
    fi

    wait "$pid" 2>/dev/null || true
    return 0
}
