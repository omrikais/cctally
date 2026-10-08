#!/bin/bash
# usage: WRITE_ATTRIBUTION_INPUTS={frozen:FREEZE|live} \
#        run-clone.sh STORE TAG DURATION_S PORT TREE [--live]   (bounded: kills the dashboard after DURATION_S)
#   STORE is a CLOSED copy of a data directory on the external drive. The
#   dashboard never runs on it: it runs on its own APFS clone of it
#   ($WRITE_ATTRIBUTION_SCRATCH/clone-TAG, cp -c, removed afterwards), and a
#   STORE at or under the live data directory is refused before anything
#   starts (Amendment 19 HR-19).
#   Revision 15 (Q16): the dashboard and its dashboard-perf samples are family
#   processes started through the launch contract (_inputs.sh `wa_exec`); in
#   frozen mode they read FREEZE through the namespace, live roots need --live.
#   Each one is waited on through `wa_wait` and the runner's own kills go
#   through `wa_kill`, so how it ended is recorded (Amendment 13 O3).
#   Lifecycle (Amendment 19 HR-2, `_inputs.sh` wa_guard): an interrupt, the
#   hard deadline (DURATION_S + 1200 s; WA_DEADLINE_S overrides it) or the
#   runner's own death still kills the dashboard's group and every process
#   carrying the run token, and judges the family once.
set -u
P=$(cd "$(dirname "$0")" && pwd)
ARGS=(); for a in "$@"; do if [ "$a" = --live ]; then export WA_ALLOW_LIVE=1; else ARGS+=("$a"); fi; done
set -- ${ARGS[@]+"${ARGS[@]}"}
. "$P/_inputs.sh"
[ $# -eq 5 ] || { echo "usage: run-clone.sh STORE TAG DURATION_S PORT TREE [--live]" >&2; exit 2; }
STORE=$1; TAG=$2; DUR=$3; PORT=$4; TREE=$5
case $DUR in ''|*[!0-9]*) echo "DURATION_S must be a whole number" >&2; exit 2 ;; esac
wa_refuse_live_store "$STORE" || exit 2
[ -d "$STORE" ] || { echo "no store at $STORE" >&2; exit 2; }
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
OUT=$X/run-$TAG; ROOT=$X/clone-$TAG
{ [ -e "$OUT" ] || [ -e "$ROOT" ]; } && { echo "run or clone exists; use a fresh TAG" >&2; exit 2; }
mkdir -p "$OUT" "$X/tmp"
PY=/opt/homebrew/bin/python3
PID=""
stop() {
  if [ -n "$PID" ]; then
    wa_kill 15 "$PID" && sleep 3
    wa_kill 9 "-$PID" || wa_kill 9 "$PID"
    wa_wait dashboard "$PID"; PID=""
  fi
  if [ -n "$ROOT" ] && [ "$ROOT" = "$X/clone-$TAG" ]; then rm -rf -- "$ROOT"; fi
}
wa_guard $((DUR + 1200)) stop
. "$P/_prep.sh"
mkdir -p "$ROOT" && cp -cR "$STORE" "$ROOT/data" || { echo "APFS clone of $STORE failed" >&2; exit 2; }
cd "$TREE" || exit 2
( export TMPDIR=$X/tmp/ CCTALLY_DATA_DIR=$ROOT/data WTRACE_OUT=$OUT/wtrace WTRACE_PERIOD=5 DYLD_INSERT_LIBRARIES=$DYLIB
  wa_exec "$PY" bin/cctally dashboard --host 127.0.0.1 --port "$PORT" --no-browser ) > "$OUT/dash.log" 2>&1 &
PID=$!; echo "$PID" > "$OUT/pid"; WA_EXPECT+=("$PID")
end=$((SECONDS+DUR))
while [ $SECONDS -lt $end ] && kill -0 "$PID" 2>/dev/null; do
  "$PY" "$P/rusage.py" "$PID" >> "$OUT/rusage.jsonl"
  ( export TMPDIR=$X/tmp/ CCTALLY_DATA_DIR=$ROOT/data
    wa_exec "$PY" bin/cctally dashboard-perf --host 127.0.0.1 --port "$PORT" --json ) \
    > "$OUT/perf-$(printf %04d $SECONDS).json" 2>>"$OUT/perf.err" &
  wa_wait dashboard-perf $!
  wa_sleep 15
done
stop
wa_finish_once "$(cat "$OUT/pid")" || { echo "__exit_status=2"; exit 2; }
echo "__exit_status=done"
