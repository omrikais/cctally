#!/bin/bash
# usage: WRITE_ATTRIBUTION_INPUTS=frozen:FREEZE \
#        run-latency.sh SRC_ROOT TAG TREE [--sync-passes N] [--live]   (bounded)
#   Spec §6.3 latency evidence (Amendment 12), once per tree: without
#   --sync-passes, `latency.py` times each W1-W3 and PA-001 call site; with
#   --sync-passes N it times the revision-9 sync and ingest passes (the
#   catch-up pass, N no-change passes and N small-delta passes). The run works
#   on its own APFS clone of SRC_ROOT/data (a closed backup-API copy; cp -c)
#   with CCTALLY_DATA_DIR on the clone and TMPDIR on the external drive, after
#   _prep.sh's self-tests, freeze verification and inputs.json.
#   Revision 15 (Q16): latency.py, and B's scratch seed, are family processes
#   started through the launch contract (_inputs.sh `wa_exec`); in frozen mode
#   they read FREEZE through the namespace, and latency.py refuses a copy whose
#   qualification (SRC_ROOT/frozen-qualification.json) is missing or names
#   another freeze or copy. Live roots need an explicit --live.
#   --sync-passes N seeds the clone's scratch roots exactly as B does
#   (`workload.py seed-scratch --root ROOT`, with CLAUDE_CONFIG_DIR and
#   CODEX_HOME naming the real roots plus ROOT/scratch) and passes --root ROOT,
#   so the small-delta passes append only there. No drained admission runs
#   first: latency.py's own first `cache-sync --source all` pass is the catch-up,
#   timed and reported as the labelled `catchUp` population.
#   Bounded: the seed gets SEED_TIMEOUT_S (120) and latency.py LATENCY_TIMEOUT_S
#   (3600); a step past its bound is killed with its whole process group, named
#   in timeout.json, and makes the run INVALID (exit 2). Each step is waited
#   on through `wa_wait` and every kill goes through `wa_kill`, so how each
#   ended is recorded (Amendment 13 O3). wa_finish reaps and
#   judges the family (frozen-family.json); the clone is removed on every exit
#   path. Receipts in $WRITE_ATTRIBUTION_SCRATCH/run-TAG: latency.json,
#   inputs.json, frozen-family.json (frozen). Compare the candidate's and the
#   baseline's runs with `latency.py pair CANDIDATE_RUN BASELINE_RUN`.
#   Lifecycle (Amendment 19 HR-2, `_inputs.sh` wa_guard): an interrupt, the
#   hard deadline or the runner's own death still kills the step's group and
#   the run token's processes, judges the family once and removes the clone.
set -u
set -m          # job control: every background step gets its own process group
P=$(cd "$(dirname "$0")" && pwd)
USAGE="usage: run-latency.sh SRC_ROOT TAG TREE [--sync-passes N] [--live]"
ARGS=(); SYNC=""; BAD=0
while [ $# -gt 0 ]; do
  case $1 in
    --live) export WA_ALLOW_LIVE=1 ;;
    --sync-passes) case ${2:---} in --*) BAD=1 ;; *) SYNC=$2; shift ;; esac ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
set -- ${ARGS[@]+"${ARGS[@]}"}
. "$P/_inputs.sh"
case $SYNC in ''|*[!0-9]*) [ -n "$SYNC" ] && BAD=1 ;; *) [ "$SYNC" -ge 1 ] || BAD=1 ;; esac
{ [ $# -eq 3 ] && [ $BAD = 0 ]; } || { echo "$USAGE (N >= 1)" >&2; exit 2; }
SRC=$1; TAG=$2
TREE=$(cd "$3" 2>/dev/null && pwd) || { echo "no tree at $3" >&2; exit 2; }
[ -d "$SRC/data" ] || { echo "no store copy at $SRC/data" >&2; exit 2; }
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
OUT=$X/run-$TAG; ROOT=$X/clone-$TAG
{ [ -e "$OUT" ] || [ -e "$ROOT" ]; } && { echo "run or clone exists; use a fresh TAG" >&2; exit 2; }
mkdir -p "$OUT" "$X/tmp"
REAL_HOME=$HOME
STEP_PID=""
cleanup() {
  if [ -n "$STEP_PID" ]; then
    wa_kill 9 "-$STEP_PID" || wa_kill 9 "$STEP_PID"
    wa_wait interrupted "$STEP_PID"; STEP_PID=""
  fi
  if [ -n "$ROOT" ] && [ "$ROOT" = "$X/clone-$TAG" ]; then rm -rf -- "$ROOT"; fi
}
wa_guard $(( ${SEED_TIMEOUT_S:-120} + ${LATENCY_TIMEOUT_S:-3600} + 900 )) cleanup
. "$P/_prep.sh"
PY=/opt/homebrew/bin/python3
export_clone_env() {
  export TMPDIR=$X/tmp/ CCTALLY_DATA_DIR=$ROOT/data PYTHONDONTWRITEBYTECODE=1
  unset CLAUDE_CONFIG_DIR CODEX_HOME
  if [ -n "$SYNC" ]; then
    export CLAUDE_CONFIG_DIR=$REAL_HOME/.claude,$ROOT/scratch/claude
    export CODEX_HOME=$REAL_HOME/.codex,$ROOT/scratch/codex
  fi
}
# step NAME LIMIT_S CMD...: one family process (through wa_exec) in its own
# process group, killed with the group after LIMIT_S; its pid is left in
# STEP_PID and $OUT/NAME.pid. The watchdog polls instead of sleeping the whole
# bound, so it never outlives the step by more than a second.
step() {
  local name=$1 limit=$2 watch rc
  shift 2
  ( export_clone_env; wa_exec "$@" ) > "$OUT/$name.log" 2>&1 &
  STEP_PID=$!; echo "$STEP_PID" > "$OUT/$name.pid"; WA_EXPECT+=("$STEP_PID")
  ( exec >/dev/null 2>&1; n=0
    while [ $n -lt "$limit" ] && kill -0 "$STEP_PID" 2>/dev/null; do sleep 1; n=$((n+1)); done
    if [ $n -ge "$limit" ]; then
      echo "{\"step\": \"$name\", \"limitS\": $limit}" > "$OUT/timeout.json"
      wa_kill 9 "-$STEP_PID" || wa_kill 9 "$STEP_PID"
    fi ) &
  watch=$!
  wa_wait "$name" "$STEP_PID"; rc=$?
  wait "$watch" 2>/dev/null
  [ -e "$OUT/timeout.json" ] && rc=2
  return $rc
}
mkdir -p "$ROOT" && cp -cR "$SRC/data" "$ROOT/data" || { echo "APFS clone of $SRC/data failed" >&2; exit 2; }
cd "$TREE" || exit 2
RC=0; SEED_PID=""; LPID=""
if [ -n "$SYNC" ]; then
  step seed "${SEED_TIMEOUT_S:-120}" $PY "$P/workload.py" seed-scratch --root "$ROOT" || RC=2
  SEED_PID=$STEP_PID; STEP_PID=""
fi
if [ $RC = 0 ]; then
  EXTRA=()
  [ -n "$SYNC" ] && EXTRA=(--sync-passes "$SYNC" --root "$ROOT")
  step latency "${LATENCY_TIMEOUT_S:-3600}" $PY "$P/latency.py" --tree "$TREE" \
    --out "$OUT/latency.json" --source "$SRC" ${EXTRA[@]+"${EXTRA[@]}"}
  RC=$?
  LPID=$STEP_PID; STEP_PID=""
  echo "$LPID" > "$OUT/pid"
fi
wa_finish_once $SEED_PID $LPID || RC=2
cleanup
echo "__exit_status=$RC"; exit $RC
