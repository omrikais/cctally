#!/bin/bash
# usage: WRITE_ATTRIBUTION_INPUTS=live run-live.sh TAG DURATION_S [PORT] [TREE]   (bounded and stopped)
#   The post-release live confirmation (spec §6.3, R2): the INSTALLED
#   dashboard (TREE, default /opt/homebrew/lib/node_modules/cctally) on the
#   real store and the real transcript roots, loopback only (PORT, default
#   8914), under the write interposer, with its temp files on the external
#   drive. Revision 15 (Q16): live only - WRITE_ATTRIBUTION_INPUTS=live.
#   Every run writes a FRESH $WRITE_ATTRIBUTION_SCRATCH/run-live-TAG (an
#   existing one is refused; Amendment 19 HR-17) with the records the
#   verdicts read:
#     frontier.json   the transcript roots' finite frontier at the start
#                     (jsonl_bytes.py --baseline --frontier: every file's
#                     identity and size)
#     admission.json  the warm admission (the first non-cold tick) inside
#                     900 s, with the phase trace armed
#     window.json     workload "L-post", traceStart, warm, start, end
#     terminal.json   the dashboard alive at its terminal samples, then the
#                     terminal drain: the dashboard's process group SIGKILLed
#                     (no close-time checkpoint), its setsid workers reaped by
#                     run token (token-teardown.jsonl; HR-7), and
#                     terminal_drain.py under the interposer (WTRACE_OUT=drain)
#   Judge it with `workload.py ab-verdict RUN --tree TREE` (the drain is its
#   own section).
#   Lifecycle (Amendment 19 HR-1, `_inputs.sh` wa_guard): job control, traps
#   on EXIT INT TERM HUP and a hard deadline (DURATION_S + 1500 s;
#   WA_DEADLINE_S overrides it). Interrupted, past its deadline or orphaned,
#   the runner still kills the installed dashboard's process group and every
#   process carrying its run token (its detached workers included), so no
#   dashboard is ever left running on the live store.
set -u
P=$(cd "$(dirname "$0")" && pwd)
WA_LIVE_ONLY=1 . "$P/_inputs.sh"
[ $# -ge 2 ] && [ $# -le 4 ] || {
  echo "usage: run-live.sh TAG DURATION_S [PORT] [TREE]" >&2; exit 2; }
TAG=$1; DUR=$2; PORT=${3:-8914}; TREE=${4:-/opt/homebrew/lib/node_modules/cctally}
case $DUR in ''|*[!0-9]*) echo "DURATION_S must be a whole number" >&2; exit 2 ;; esac
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
OUT=$X/run-live-$TAG
[ -e "$OUT" ] && { echo "$OUT exists; use a fresh TAG" >&2; exit 2; }
mkdir -p "$OUT" "$X/tmp"
PY=/opt/homebrew/bin/python3
DATA=${CCTALLY_DATA_DIR:-$HOME/.local/share/cctally}
PID=""
stop() {
  if [ -n "$PID" ]; then
    wa_kill 9 "-$PID" || wa_kill 9 "$PID"
    wa_wait dashboard "$PID"; PID=""
  fi
}
invalid() {
  printf '{"valid": false, "reason": "%s"}\n' "$1" > "$OUT/admission.json"
  echo "INVALID: $1" >&2
  exit 2
}
wa_guard $((DUR + 1500)) stop
. "$P/_prep.sh"
cd "$TREE" || exit 2
# family_step NAME CMD...: one short helper process, waited on.
family_step() {
  local name=$1; shift
  ( wa_exec "$@" ) & wa_wait "$name" $!
}
family_step jsonl_bytes "$PY" "$P/jsonl_bytes.py" --baseline "$OUT/jsonl-baseline.json" \
  --frontier "$OUT/frontier.json" >> "$OUT/jsonl.jsonl" 2>>"$OUT/jsonl.err"
( export TMPDIR=$X/tmp/ WTRACE_OUT=$OUT/wtrace WTRACE_PERIOD=5 DYLD_INSERT_LIBRARIES=$DYLIB
  wa_exec "$PY" bin/cctally dashboard --host 127.0.0.1 --port "$PORT" --no-browser ) > "$OUT/dash.log" 2>&1 &
PID=$!; echo "$PID" > "$OUT/pid"; WA_EXPECT+=("$PID")
TRACE_START=$("$WA_HELPER_PY" -c 'import time; print(time.time())')
sample() {
  "$PY" "$P/rusage.py" "$PID" >> "$OUT/rusage.jsonl"
  family_step dashboard-perf "$PY" bin/cctally dashboard-perf --host 127.0.0.1 \
    --port "$PORT" --json > "$OUT/perf-$(printf %05d $SECONDS).json" 2>>"$OUT/perf.err"
}
ADMIT_DEADLINE=$((SECONDS+900)); warm=0; armed=0
while [ $SECONDS -lt $ADMIT_DEADLINE ] && kill -0 "$PID" 2>/dev/null; do
  sample
  if [ $armed = 0 ] && family_step trace-arm "$PY" bin/cctally dashboard-perf \
       --host 127.0.0.1 --port "$PORT" --trace on > "$OUT/trace-arm.txt" 2>&1; then armed=1; fi
  LAST=$(ls "$OUT"/perf-*.json | tail -1)
  if "$WA_HELPER_PY" -c 'import json,sys; d=json.load(open(sys.argv[1]))["diagnostic"] or {}; r=(d.get("tick") or {}).get("records") or []; sys.exit(0 if any(not x.get("cold") for x in r) else 1)' "$LAST" 2>/dev/null; then
    warm=1; break
  fi
  wa_sleep 15
done
[ $warm = 1 ] || invalid "no warm admission inside the 900 s admission deadline"
[ $armed = 1 ] || invalid "the phase trace could not be armed"
printf '{"valid": true, "kind": "warm", "deadlineS": 900}\n' > "$OUT/admission.json"
WARM=$("$WA_HELPER_PY" -c 'import time; print(time.time())')
START=$WARM
end=$((SECONDS+DUR))
while [ $SECONDS -lt $end ] && kill -0 "$PID" 2>/dev/null; do
  sample
  family_step jsonl_bytes "$PY" "$P/jsonl_bytes.py" --baseline "$OUT/jsonl-baseline.json" \
    >> "$OUT/jsonl.jsonl" 2>>"$OUT/jsonl.err"
  wa_sleep 15
done
END=$("$WA_HELPER_PY" -c 'import time; print(time.time())')
kill -0 "$PID" 2>/dev/null || invalid "the dashboard exited before its terminal samples"
sample
sleep 6
ALIVE=0; kill -0 "$PID" 2>/dev/null && ALIVE=1
printf '{"t": %s, "alive": %s}\n' "$("$WA_HELPER_PY" -c 'import time; print(time.time())')" "$ALIVE" > "$OUT/terminal.json"
printf '{"workload": "L-post", "traceStart": %s, "warm": %s, "start": %s, "end": %s}\n' \
  "$TRACE_START" "$WARM" "$START" "$END" > "$OUT/window.json"
# ── terminal drain: no close-time checkpoint, its workers reaped, the copier ──
stop
wa_reap_detached
( export TMPDIR=$X/tmp/ WTRACE_OUT=$OUT/drain WTRACE_PERIOD=1 DYLD_INSERT_LIBRARIES=$DYLIB
  wa_exec "$PY" "$P/terminal_drain.py" --data "$DATA" --terminal "$OUT/terminal.json" ) \
  > "$OUT/drain.log" 2>&1 &
wa_wait terminal_drain $!
echo "__exit_status=done"
