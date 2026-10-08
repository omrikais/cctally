#!/bin/bash
# usage: WRITE_ATTRIBUTION_INPUTS={frozen:FREEZE|live} \
#        run-workload.sh {A|B|C|D|L} SRC_ROOT TAG TREE PORT [--live]   (bounded and stopped)
#   Input mode (spec §6.3 revision 15, Q16; _inputs.sh): A, B, C and D - and
#   P2/P3 through B - read the frozen input set FREEZE of SRC_ROOT through the
#   namespace (frozen:FREEZE); every family process - catch-up and its
#   verification, setup helpers, the dashboard and its detached workers, the
#   appender, hooks and the terminal-drain copier - starts through `wa_exec`,
#   the freeze is verified before and after, and the family's activation
#   receipts are judged (frozen-family.json). Every family process this runner
#   starts is waited on through `wa_wait`, and its own kills go through
#   `wa_kill`, so how each one ended is recorded in RECEIPTS/toplevel.jsonl
#   and a top-level process the sandbox kills invalidates the family
#   (Amendment 13 O3). Live roots on these workloads
#   need an explicit --live. L is the pre-merge live-roots confirmation and is
#   live only (WRITE_ATTRIBUTION_INPUTS=live).
#   SRC_ROOT  a CLOSED copy on the external drive whose data/ is a backup-API copy
#             of the data directory. Each run works on its own APFS clone of data/
#             (cp -c) with CCTALLY_DATA_DIR pointed at it and TMPDIR on the external
#             drive. Transcripts (operator decision esc-0db0e25cdfa4, replacing the
#             frozen relocated roots, which re-ingest all history because stored
#             paths and Codex root keys are absolute): every workload READS the real
#             roots, read-only, so stored identities match; B and D add an extra
#             scratch root (ROOT/scratch, seeded with synthetic transcripts) via
#             CLAUDE_CONFIG_DIR / CODEX_HOME and append only there. Nothing writes
#             the live store or the real roots.
#   A  idle: compacted conversations.db, fresh retention stamp, 300 s measured;
#      append-free only if the real roots did not grow in the window (jsonl.jsonl),
#      otherwise a labelled ambient-activity run
#   B  A's setup + ~100 KB/min of appended Claude and Codex records, 300 s
#   C  free pages kept, retention_days=29, fresh stamp; the stamp is rewound 25 h
#      600 s into the 1800 s measured interval (the batch it makes due is recorded
#      first); every maintenance operation is captured (run_with_capture.py) and
#      the #780 record is snapshotted at the interval's start and end
#   D  foreground `hook-tick --foreground --source codex` at a 35 s spacing until
#      20 have ingested their own append (at most 25; a budgeted hook may defer,
#      revision 11), each after ~2 KiB of appended Codex records, each under the
#      interposer and sqlattr.py (hook_runner.py); no dashboard. REPRODUCE=1 is the pre-attempt
#      reproduction mode: the same runner on the given (pre-revision-9) tree must
#      show an ingesting, temp-writing hook and name its statements
#   L  pre-merge live-roots confirmation (Q6): HOME and CODEX_HOME stay real, so
#      the dashboard READS the live transcript roots and real activity; 360 s
#   B with P2_SLICE=DIR (spec §6.3 P2, run-p2.sh): B's setup with a
#      `projection_replay.py p2-extract` slice placed in the scratch root (each
#      file truncated at the window start) and its lines re-appended at their
#      original relative times by `p2-replay` instead of the ~100 KB/min appender
# Drained admission (spec §6.3, Q11): after seeding, catchup.py drains the clone with
# the tree's own `cache-sync --source all` (under the interposer, its receipt in
# catchup.json) and admits it only on the certifiable-sync predicates, Claude's
# alternatively through the stable-residual exception bound to SRC_ROOT's
# preparation receipt (revision 10: `catchup.py prep` once per source); the existing
# 900 s admission deadline covers that drain AND the dashboard's warm admission, and
# an expired deadline or a refused drain makes the attempt INVALID (admission.json),
# never extended. Verdicts: A/B/L `workload.py ab-verdict`, C `c-verdict`, D
# `d-verdict`, each importing the candidate kernel from THIS tree. The appender runs in
# its own process group and is reaped on every exit path; the terminal rusage,
# interposer and dashboard-perf samples are taken while the dashboard is alive.
# Terminal drain (spec §6.3 revision 12, Q13): after those terminal samples every
# dashboard workload (A, B, C, L, and P2/P3 through B) stops the appender, then the
# dashboard's whole process group with SIGKILL, so no close-time checkpoint runs,
# and runs terminal_drain.py under the interposer (WTRACE_OUT=RUN/drain) with no
# other process on the clone: it reads each store's wal-index before opening
# SQLite, runs wal_checkpoint(TRUNCATE) and samples its own rusage into
# terminal.json; `workload.py drain-verdict` judges it as its own section.
# Lifecycle (Amendment 19 HR-1/HR-2, `_inputs.sh` wa_guard): job control, a
# fresh run token in every family process, traps on EXIT INT TERM HUP and a
# hard deadline (WA_DEADLINE_S overrides it); every exit path kills the
# runner's process groups and the token's processes (setsid workers
# included, recorded), and judges the family once (wa_finish_once). Before
# the terminal drain the family's detached workers are reaped, so the copier
# runs with no other process on the clone (HR-21).
set -u
set -m          # job control: every background job gets its own process group
P=$(cd "$(dirname "$0")" && pwd)
ARGS=(); for a in "$@"; do if [ "$a" = --live ]; then export WA_ALLOW_LIVE=1; else ARGS+=("$a"); fi; done
set -- ${ARGS[@]+"${ARGS[@]}"}
[ "${1:-}" = L ] && WA_LIVE_ONLY=1
. "$P/_inputs.sh"
W=$1; SRC=$2; TAG=$3; TREE=$4; PORT=$5
SLICE=${P2_SLICE:-}
if [ -n "$SLICE" ] && [ "$W" != B ]; then echo "P2_SLICE is B-only" >&2; exit 2; fi
LABEL=$W; [ -n "$SLICE" ] && LABEL=P2
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
OUT=$X/run-$TAG; ROOT=$X/clone-$TAG
{ [ -e "$OUT" ] || [ -e "$ROOT" ]; } && { echo "run or clone exists; use a fresh TAG" >&2; exit 2; }
mkdir -p "$OUT" "$X/tmp"
REAL_HOME=$HOME
PID=""; APID=""; RPID=""
case $W in
  C) LIMIT=5400 ;;                    # 900 s admission + 1830 s window + drain
  D) LIMIT=7200 ;;                    # up to 25 hooks at a 35 s spacing
  *) LIMIT=$((3600 + ${P2_MEASURE_SECONDS:-0})) ;;
esac
export_clone_env() {
  export TMPDIR=$X/tmp/ CCTALLY_DATA_DIR=$ROOT/data PYTHONDONTWRITEBYTECODE=1
  unset CLAUDE_CONFIG_DIR CODEX_HOME
  if [ "$W" = B ] || [ "$W" = D ]; then
    export CLAUDE_CONFIG_DIR=$REAL_HOME/.claude,$ROOT/scratch/claude
    export CODEX_HOME=$REAL_HOME/.codex,$ROOT/scratch/codex
  fi
}
# run_env CMD...: one foreground family process, waited on and recorded
# (the step is named after the script and its first argument).
run_env() {
  local step=${2##*/}
  case ${3:-} in ''|-*) ;; *) step="$step $3" ;; esac
  ( export_clone_env; wa_exec "$@" ) & wa_wait "$step" $!
}
invalid() {
  # The EXIT trap reaps the family (its ends recorded) and judges it once.
  echo "{\"valid\": false, \"reason\": \"$1\"}" > "$OUT/admission.json"
  echo "INVALID: $1" >&2
  exit 2
}
reap() {
  # An interrupted foreground step, the appender's whole process group, then
  # the dashboard; on EVERY exit path, each kill and end recorded.
  if [ -n "${WA_WAITING:-}" ]; then
    local w=$WA_WAITING
    wa_kill 9 "$w"; wa_wait interrupted "$w"
  fi
  if [ -n "$APID" ]; then
    wa_kill 15 "-$APID"; wa_wait appender "$APID"; APID=""
  fi
  if [ -n "$RPID" ]; then
    wa_kill 9 "-$RPID"; wa_wait rewind "$RPID"; RPID=""
  fi
  if [ -n "$PID" ]; then
    wa_kill 2 "$PID"; sleep 5; wa_kill 15 "$PID"; sleep 2
    wa_kill 9 "-$PID" || wa_kill 9 "$PID"
    wa_wait dashboard "$PID"; PID=""
  fi
  # A frozen family's detached workers too: every process still alive whose
  # pid and start second match its namespace receipt (frozen_roots.py reap).
  if [ "${WA_MODE:-}" = frozen ] && [ -d "${WA_RECEIPTS:-/nonexistent}" ]; then
    /opt/homebrew/bin/python3 "$P/frozen_roots.py" reap --receipts "$WA_RECEIPTS" >/dev/null 2>&1
  fi
}
wa_guard "$LIMIT" reap
. "$P/_prep.sh"
PY=/opt/homebrew/bin/python3
mkdir -p "$ROOT" && cp -cR "$SRC/data" "$ROOT/data" || { echo "APFS clone of $SRC/data failed" >&2; exit 2; }
ADMIT_DEADLINE=$((SECONDS+900))
run_env $PY "$P/jsonl_bytes.py" --baseline "$OUT/jsonl-baseline.json" > "$OUT/jsonl-catchup-start.json" 2>/dev/null
if [ "$W" = B ] || [ "$W" = D ]; then
  run_env $PY "$P/workload.py" seed-scratch --root "$ROOT" >> "$OUT/setup.log" 2>&1 || exit 2
fi
if [ -n "$SLICE" ]; then
  run_env $PY "$P/projection_replay.py" p2-place --slice "$SLICE" --root "$ROOT" >> "$OUT/setup.log" 2>&1 || exit 2
fi
case $W in
  A|B) run_env $PY "$P/workload.py" compact --tree "$TREE" --root "$ROOT" >> "$OUT/setup.log" 2>&1 || exit 2
       run_env $PY "$P/workload.py" stamp --root "$ROOT" --at now >> "$OUT/setup.log" 2>&1 || exit 2
       MEASURE=300 ;;
  C)   run_env $PY "$P/workload.py" retention-days --root "$ROOT" --days 29 >> "$OUT/setup.log" 2>&1 || exit 2
       run_env $PY "$P/workload.py" stamp --root "$ROOT" --at now >> "$OUT/setup.log" 2>&1 || exit 2
       MEASURE=1800 ;;
  L|D) MEASURE=360 ;;
  *)   echo "unknown workload $W" >&2; exit 2 ;;
esac
# Revision 13 (Q14): a P2-style replay may set its own window
# (`run-p2.sh ... SECONDS`, used by the lifecycle proof); every other run keeps
# its workload's window.
if [ "$W" = B ] && [ -n "$SLICE" ] && [ -n "${P2_MEASURE_SECONDS:-}" ]; then
  MEASURE=$P2_MEASURE_SECONDS
fi
# ── drained admission (catch-up under the interposer; its receipt is evidence) ──
REMAIN=$((ADMIT_DEADLINE-SECONDS))
[ $REMAIN -gt 0 ] || invalid "the admission deadline expired before the catch-up"
# --source names the copy so catchup.py reads its preparation receipt
# ($SRC/prep-receipt.json, `catchup.py prep`): the only route for a Claude walk
# that does not certify (spec §6.3 revision 10); a missing receipt is INVALID.
( export_clone_env; export WTRACE_OUT=$OUT/catchup WTRACE_PERIOD=5 DYLD_INSERT_LIBRARIES=$DYLIB
  wa_exec $PY "$P/catchup.py" --tree "$TREE" --out "$OUT/catchup.json" --deadline-s "$REMAIN" --source "$SRC" ) \
  > "$OUT/catchup.log" 2>&1 &
wa_wait catchup $! || invalid "the catch-up did not admit the clone (catchup.json)"
run_env $PY "$P/jsonl_bytes.py" --baseline "$OUT/jsonl-baseline.json" > "$OUT/jsonl-catchup-end.json" 2>/dev/null
if [ "$W" = D ]; then
  ( export_clone_env; wa_exec $PY "$P/hook_runner.py" --tree "$TREE" --root "$ROOT" --out "$OUT" \
      --dylib "$DYLIB" --hooks 20 --max-invocations 25 --spacing-s 35 ${REPRODUCE:+--reproduce} ) > "$OUT/hooks.log" 2>&1 &
  WA_EXPECT+=("$!")
  wa_wait hook_runner $!
  RC=$?
  echo "{\"valid\": true}" > "$OUT/admission.json"
  wa_finish_once || RC=2
  echo "__exit_status=$RC"; exit $RC
fi
cd "$TREE" || exit 2
LAUNCH=(bin/cctally)
[ "$W" = C ] && LAUNCH=("$P/run_with_capture.py" "$TREE" --)
( export_clone_env; export WTRACE_OUT=$OUT/wtrace WTRACE_PERIOD=5 DYLD_INSERT_LIBRARIES=$DYLIB OPS_CAPTURE=$OUT/ops.jsonl
  wa_exec $PY "${LAUNCH[@]}" dashboard --host 127.0.0.1 --port "$PORT" --no-browser ) > "$OUT/dash.log" 2>&1 &
PID=$!; echo "$PID" > "$OUT/pid"; WA_EXPECT+=("$PID")
TRACE_START=$($PY -c 'import time; print(time.time())')
sample() {
  $PY "$P/rusage.py" "$PID" >> "$OUT/rusage.jsonl"
  ( export_clone_env; wa_exec $PY bin/cctally dashboard-perf --host 127.0.0.1 --port "$PORT" --json ) \
    > "$OUT/perf-$(printf %05d $SECONDS).json" 2>>"$OUT/perf.err" &
  wa_wait dashboard-perf $!
}
# C's due batch, then the 25 h rewind that makes it due: one background family
# step (its own process group under `set -m`, killed by `reap`), so the
# ~80 s it takes never stops the sampler (Task X H-2: the synchronous form
# left a rusage gap past the kernel's 60 s coverage rule in every C window).
rewind_c() {
  run_env $PY "$P/workload.py" batch --root "$ROOT" --days 29 > "$OUT/batch.json" 2>>"$OUT/setup.log" || exit 2
  run_env $PY "$P/workload.py" stamp --root "$ROOT" --at rewind-25h > "$OUT/rewind.txt" 2>>"$OUT/setup.log" || exit 2
}
warm=0; armed=0
while [ $SECONDS -lt $ADMIT_DEADLINE ] && kill -0 "$PID" 2>/dev/null; do
  sample
  # Arm the deep phase trace once the server answers: the §6.3 performance
  # evidence reads `doctor.gather` and the sync/ingest phases from the stored
  # trees the samples carry (`phases`, `ingest_phases`).
  if [ $armed = 0 ] && run_env $PY bin/cctally dashboard-perf --host 127.0.0.1 \
       --port "$PORT" --trace on > "$OUT/trace-arm.txt" 2>&1; then armed=1; fi
  LAST=$(ls "$OUT"/perf-*.json | tail -1)
  if $PY -c 'import json,sys; d=json.load(open(sys.argv[1]))["diagnostic"] or {}; r=(d.get("tick") or {}).get("records") or []; sys.exit(0 if any(not x.get("cold") for x in r) else 1)' "$LAST" 2>/dev/null; then
    warm=1; break
  fi
  wa_sleep 15
done
[ $warm = 1 ] || invalid "no warm admission inside the 900 s admission deadline"
[ $armed = 1 ] || invalid "the phase trace could not be armed"
echo "{\"valid\": true}" > "$OUT/admission.json"
WARM=$($PY -c 'import time; print(time.time())')
if [ "$W" = B ] && [ -n "$SLICE" ]; then
  ( export_clone_env; wa_exec $PY "$P/projection_replay.py" p2-replay --slice "$SLICE" --root "$ROOT" \
      --seconds $((MEASURE+60)) ) > "$OUT/append.json" 2>>"$OUT/setup.log" &
  APID=$!; echo "$APID" > "$OUT/appender.pgid"
elif [ "$W" = B ]; then
  ( export_clone_env; wa_exec $PY "$P/workload.py" append --root "$ROOT" --seconds $((MEASURE+60)) \
      --kib-per-min 100 --scratch ) > "$OUT/append.json" 2>>"$OUT/setup.log" &
  APID=$!; echo "$APID" > "$OUT/appender.pgid"
fi
[ "$W" = C ] && run_env $PY "$P/workload.py" record-snapshot --root "$ROOT" > "$OUT/record-start.json"
START=$($PY -c 'import time; print(time.time())')
START_S=$SECONDS; end=$((SECONDS+MEASURE+30)); rewound=0
while [ $SECONDS -lt $end ] && kill -0 "$PID" 2>/dev/null; do
  sample
  # HR-14: the sampler's errors are kept (the leak tripwire needs its samples)
  run_env $PY "$P/jsonl_bytes.py" --baseline "$OUT/jsonl-baseline.json" >> "$OUT/jsonl.jsonl" 2>>"$OUT/jsonl.err"
  if [ "$W" = C ] && [ $rewound = 0 ] && [ $((SECONDS-START_S)) -ge 600 ]; then
    # §6.3 C (Q8): the batch that becomes due ten minutes into the interval,
    # in the background so the sampler keeps its period (Task X H-2).
    rewind_c & RPID=$!
    rewound=1
  fi
  # Task X H-3: a jittered period (10-20 s, mean 15 s); a fixed one locks
  # onto the dashboard's tick period and its stored trees can then miss
  # every build of a periodic phase.
  wa_sleep $((10 + RANDOM % 11))
done
END=$($PY -c 'import time; print(time.time())')
if [ -n "$RPID" ]; then
  wa_wait rewind "$RPID" || exit 2
  RPID=""
fi
# Terminal samples while the measured process is still alive: rusage, the
# dashboard-perf diagnostic, and one more periodic interposer snapshot.
kill -0 "$PID" 2>/dev/null || invalid "the dashboard exited before its terminal samples"
sample
[ "$W" = C ] && run_env $PY "$P/workload.py" record-snapshot --root "$ROOT" > "$OUT/record-end.json"
sleep 6
ALIVE=0; kill -0 "$PID" 2>/dev/null && ALIVE=1
echo "{\"t\": $($PY -c 'import time; print(time.time())'), \"alive\": $ALIVE}" > "$OUT/terminal.json"
echo "{\"workload\": \"$LABEL\", \"traceStart\": $TRACE_START, \"warm\": $WARM, \"start\": $START, \"end\": $END}" > "$OUT/window.json"
# ── terminal drain: no close-time checkpoint, then the measured copier ──
if [ -n "$APID" ]; then
  wa_kill 15 "-$APID"; wa_wait appender "$APID"; APID=""
fi
wa_kill 9 "-$PID" || wa_kill 9 "$PID"
wa_wait dashboard "$PID"; PID=""
# HR-21: the dashboard's detached workers (another session, so the group
# kill missed them) are reaped, recorded, before the copier opens the clone.
wa_reap_detached
( export_clone_env; export WTRACE_OUT=$OUT/drain WTRACE_PERIOD=1 DYLD_INSERT_LIBRARIES=$DYLIB
  wa_exec $PY "$P/terminal_drain.py" --data "$ROOT/data" --terminal "$OUT/terminal.json" ) \
  > "$OUT/drain.log" 2>&1 &
wa_wait terminal_drain $!
reap
wa_finish_once "$(cat "$OUT/pid")" || { echo "__exit_status=2"; exit 2; }
echo "__exit_status=done"
