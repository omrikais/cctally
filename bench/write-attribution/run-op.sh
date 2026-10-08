#!/bin/bash
# usage: WRITE_ATTRIBUTION_INPUTS={frozen:FREEZE|live} \
#        run-op.sh SRC_DB TAG TREE [--live] -- <maintenance_op.py args>   (bounded)
#   Revision 15 (Q16): C-op and R-cov keep their database geometry and
#   operation inputs and read no transcript, but every process below starts
#   through the launch contract (_inputs.sh `wa_exec`) so the whole family runs
#   under the namespace and its sandbox; live roots need --live. Each one is
#   waited on through `wa_wait` and the watchdog's kill goes through `wa_kill`,
#   so how each ended is recorded (Amendment 13 O3).
#   SRC_DB  a CLOSED backup-API copy of conversations.db on the external drive;
#           this run works on its own APFS clone of it (cp -c), never on SRC_DB.
#   COPIER=foreign   the final wal_checkpoint(TRUNCATE) runs in a second process
#   OP_TIMEOUT_S     kill the operation process after this many seconds (1800)
# Four processes, each under the interposer with its own trace: the baseline
# normalization of the fixed-size #780 record and the baseline checkpoint (both
# kept separate: an uncharged state write is never inside a measured run), the
# operations (maintenance_op.py; `--pin` for the classification runs), and,
# with COPIER=foreign, the copier. Every trace also writes the WAL frame log
# (`<trace>.<pid>.frames`) the deletion receipts read.
# Then: analyze_op.py "$OUT" --mode {c-op,r-cov} [--control RUN] [--require-chunk-validity].
# OUT/source.json records the copy this run cloned (its path, identity and
# size), so a segmented R-cov (i) aggregate can prove its segments chain on
# one clone (HR-21). A SRC_DB at or under the live data directory is refused.
# Lifecycle (Amendment 19 HR-2, `_inputs.sh` wa_guard): an interrupt, the hard
# deadline (OP_TIMEOUT_S + 1200 s; WA_DEADLINE_S overrides it) or the
# runner's own death still kills the operation's and the copier's groups and
# every process carrying the run token, and judges the family once.
set -u
P=$(cd "$(dirname "$0")" && pwd)
ARGS=(); for a in "$@"; do if [ "$a" = --live ]; then export WA_ALLOW_LIVE=1; else ARGS+=("$a"); fi; done
set -- ${ARGS[@]+"${ARGS[@]}"}
. "$P/_inputs.sh"
[ $# -ge 3 ] || { echo "usage: run-op.sh SRC_DB TAG TREE [--live] -- <maintenance_op.py args>" >&2; exit 2; }
SRC=$1; TAG=$2; TREE=$3; shift 3
[ "${1:-}" = "--" ] && shift
wa_refuse_live_store "$SRC" || exit 2
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
OUT=$X/run-$TAG
[ -e "$OUT" ] && { echo "$OUT exists; use a fresh TAG" >&2; exit 2; }
mkdir -p "$OUT" "$X/tmp"
OPID=""; CPID=""
stop() {
  local p
  for p in "$OPID" "$CPID"; do
    [ -n "$p" ] || continue
    wa_kill 9 "-$p" || wa_kill 9 "$p"
    wa_wait interrupted "$p"
  done
  OPID=""; CPID=""
}
wa_guard $(( ${OP_TIMEOUT_S:-1800} + 1200 )) stop
. "$P/_prep.sh"
PY=/opt/homebrew/bin/python3
DB=$OUT/conversations.db
"$WA_HELPER_PY" -c 'import json, os, sys; st = os.stat(sys.argv[1]); print(json.dumps({"src": os.path.realpath(sys.argv[1]), "dev": st.st_dev, "ino": st.st_ino, "size": st.st_size}))' "$SRC" > "$OUT/source.json" \
  || { echo "no copy at $SRC" >&2; exit 2; }
cp -c "$SRC" "$DB" || { echo "APFS clone of $SRC failed" >&2; exit 2; }
for side in -wal -shm; do
  if [ -e "$SRC$side" ]; then cp -c "$SRC$side" "$DB$side" || exit 2; fi
done
CKPT='import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); print(c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()); c.close()'
( export TMPDIR=$X/tmp/ PYTHONDONTWRITEBYTECODE=1 WTRACE_OUT=$OUT/baseline-norm WTRACE_PERIOD=1 DYLD_INSERT_LIBRARIES=$DYLIB
  wa_exec $PY "$P/maintenance_op.py" --tree "$TREE" --db "$DB" --normalize-only ) > "$OUT/baseline-norm.txt" 2>&1 &
wa_wait baseline-norm $! || exit 2
( export TMPDIR=$X/tmp/ PYTHONDONTWRITEBYTECODE=1 WTRACE_OUT=$OUT/baseline-ckpt WTRACE_PERIOD=1 DYLD_INSERT_LIBRARIES=$DYLIB
  wa_exec $PY -c "$CKPT" "$DB" ) > "$OUT/baseline-ckpt.txt" 2>&1 &
wa_wait baseline-ckpt $! || exit 2
FINAL=self
[ "${COPIER:-}" = foreign ] && FINAL=none
( export TMPDIR=$X/tmp/ PYTHONDONTWRITEBYTECODE=1 WTRACE_OUT=$OUT/wtrace WTRACE_PERIOD=1 DYLD_INSERT_LIBRARIES=$DYLIB
  wa_exec $PY "$P/maintenance_op.py" --tree "$TREE" --db "$DB" --out "$OUT/op.json" \
    --final-checkpoint "$FINAL" "$@" ) > "$OUT/op.log" 2>&1 &
OPID=$!; echo "$OPID" > "$OUT/pid"; WA_EXPECT+=("$OPID")
# The watchdog polls instead of sleeping the whole bound: a killed `( sleep N; ... )`
# subshell orphans its sleep, which keeps this script's stdout pipe open for N s.
( exec >/dev/null 2>&1; n=0; T=${OP_TIMEOUT_S:-1800}
  while [ $n -lt "$T" ] && kill -0 "$OPID" 2>/dev/null; do sleep 1; n=$((n+1)); done
  [ $n -ge "$T" ] && { wa_kill 9 "-$OPID" || wa_kill 9 "$OPID"; } ) &
WATCH=$!
wa_wait op "$OPID"; RC=$?
wait "$WATCH" 2>/dev/null
OP=$OPID; OPID=""; COPIER_PID=""
if [ "$FINAL" = none ]; then
  ( export TMPDIR=$X/tmp/ PYTHONDONTWRITEBYTECODE=1 WTRACE_OUT=$OUT/copier WTRACE_PERIOD=1 DYLD_INSERT_LIBRARIES=$DYLIB
    wa_exec $PY -c "$CKPT" "$DB" ) > "$OUT/copier.txt" 2>&1 &
  CPID=$!; echo "$CPID" > "$OUT/copier.pid"; WA_EXPECT+=("$CPID")
  wa_wait copier "$CPID" || RC=2
  COPIER_PID=$CPID; CPID=""
fi
wa_finish_once "$OP" ${COPIER_PID:-} || RC=2
echo "__exit_status=$RC"
