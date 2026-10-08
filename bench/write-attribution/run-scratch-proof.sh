#!/bin/bash
# usage: run-scratch-proof.sh SRC_ROOT TAG TREE PORT     (bounded and stopped)
#   The precondition of B and D (operator decision esc-0db0e25cdfa4): adding the
#   extra scratch root to CLAUDE_CONFIG_DIR / CODEX_HOME must not purge, replay or
#   duplicate anything the real roots own. On an APFS clone of SRC_ROOT/data:
#   phase 1 runs TREE's dashboard on the real roots alone until a warm tick, and a
#   CoW copy keeps that state (MID); phase 2 seeds ROOT/scratch, runs the dashboard
#   on real + scratch roots until a warm tick, appends 90 s of scratch records and
#   lets it tick. scratch_proof.py then compares MID with the result. Both clones
#   are removed afterwards; OUT keeps the logs and proof.json.
#   Revision 15 (Q16): the real roots by definition, so live only
#   (WRITE_ATTRIBUTION_INPUTS=live).
#   Lifecycle (Amendment 19 HR-2, `_inputs.sh` wa_guard): an interrupt, the
#   hard deadline (3600 s; WA_DEADLINE_S overrides it) or the runner's own
#   death still kills the dashboard's group and every process carrying the
#   run token, and the clones are removed.
set -u
P=$(cd "$(dirname "$0")" && pwd)
WA_LIVE_ONLY=1 . "$P/_inputs.sh"
SRC=$1; TAG=$2; TREE=$3; PORT=$4
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
PY=/opt/homebrew/bin/python3
ROOT=$X/clone-$TAG; MID=$X/clone-$TAG-mid; OUT=$X/run-$TAG
{ [ -e "$ROOT" ] || [ -e "$OUT" ] || [ -e "$MID" ]; } && { echo "run or clone exists; use a fresh TAG" >&2; exit 2; }
mkdir -p "$OUT" "$X/tmp" "$ROOT"
REAL_HOME=$HOME
PID=""
stop() {
  [ -n "$PID" ] || return 0
  wa_kill 2 "$PID" && sleep 5
  wa_kill 15 "$PID" && sleep 2
  wa_kill 9 "-$PID" || wa_kill 9 "$PID"
  wa_wait dashboard "$PID"; PID=""
}
cleanup() {
  stop
  if [ "$ROOT" = "$X/clone-$TAG" ]; then rm -rf -- "$ROOT" "$MID"; fi
}
wa_guard 3600 cleanup
cp -cR "$SRC/data" "$ROOT/data" || { echo "APFS clone of $SRC/data failed" >&2; exit 2; }
run_phase() {  # name, with-scratch (0|1), port
  local name=$1 scr=$2 port=$3
  cd "$TREE" || exit 2
  ( export TMPDIR=$X/tmp/ CCTALLY_DATA_DIR=$ROOT/data PYTHONDONTWRITEBYTECODE=1
    unset CLAUDE_CONFIG_DIR CODEX_HOME
    if [ "$scr" = 1 ]; then
      export CLAUDE_CONFIG_DIR=$REAL_HOME/.claude,$ROOT/scratch/claude
      export CODEX_HOME=$REAL_HOME/.codex,$ROOT/scratch/codex
    fi
    wa_exec "$PY" bin/cctally dashboard --host 127.0.0.1 --port "$port" --no-browser ) > "$OUT/dash-$name.log" 2>&1 &
  PID=$!
  local deadline=$((SECONDS+900)) warm=0
  while [ $SECONDS -lt $deadline ] && kill -0 "$PID" 2>/dev/null; do
    CCTALLY_DATA_DIR=$ROOT/data "$PY" bin/cctally dashboard-perf --host 127.0.0.1 --port "$port" --json \
      > "$OUT/perf-$name.json" 2>>"$OUT/perf.err"
    if $PY -c 'import json,sys; d=json.load(open(sys.argv[1]))["diagnostic"] or {}; r=(d.get("tick") or {}).get("records") or []; sys.exit(0 if any(not x.get("cold") for x in r) else 1)' "$OUT/perf-$name.json" 2>/dev/null; then
      warm=1; break
    fi
    wa_sleep 15
  done
  echo "$(date +%T) $name warm=$warm"
  if [ $warm = 1 ] && [ "$scr" = 1 ]; then
    CCTALLY_DATA_DIR=$ROOT/data "$PY" "$P/workload.py" append --root "$ROOT" --seconds 90 --kib-per-min 100 --scratch \
      > "$OUT/append.json" 2>>"$OUT/append.err"
    wa_sleep 45
  fi
  stop
  [ $warm = 1 ] || { echo "$name: no warm admission within 15 min" >&2; exit 2; }
}
run_phase real 0 "$PORT"
cp -cR "$ROOT/data" "$MID" || exit 2
"$PY" "$P/workload.py" seed-scratch --root "$ROOT" >> "$OUT/setup.log" 2>&1 || exit 2
run_phase scratch 1 "$((PORT + 1))"
"$PY" "$P/scratch_proof.py" "$MID" "$ROOT/data" "$ROOT/scratch" "$OUT/proof.json"; RC=$?
cleanup
echo "__exit_status=$RC"; exit $RC
