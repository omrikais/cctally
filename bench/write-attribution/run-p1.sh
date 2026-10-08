#!/bin/bash
# usage: WRITE_ATTRIBUTION_INPUTS=frozen:FREEZE \
#        run-p1.sh SRC_ROOT TAG TREE KEY {candidate|control} [--late-anchor] [--live]
#   Spec §6.3 P1 (Q12, dc9 G3), one conversation on one tree, bounded: an APFS
#   clone of SRC_ROOT/data (the B source copy, closed) with the interposer and
#   the namespace built and self-tested first (_prep.sh), then
#   `projection_replay.py p1` on the clone. The retained conversation KEY's
#   rollouts are copied into the clone's p1-scratch root; every pass also reads
#   the Codex roots ($CODEX_HOME, default ~/.codex), exactly as B does.
#   Revision 15 (Q16): with frozen:FREEZE (a qualified freeze of SRC_ROOT) the
#   whole P1 family - setup, selection, the measured passes and the rebuild
#   oracle - starts through the launch contract (_inputs.sh `wa_exec`) and reads
#   the freeze through the namespace; the measured children append the write
#   interposer to the namespace instead of replacing it. Live roots need an
#   explicit --live. The P1 process is waited on through `wa_wait`, which
#   records how it ended (Amendment 13 O3). The freeze is verified before and after, and the family's
#   activation receipts are judged (frozen-family.json). REPS (default 5) sets
#   the repetitions. Receipt: $WRITE_ATTRIBUTION_SCRATCH/run-TAG/p1.json; judge
#   both conversations of the candidate and of the control together with
#   `projection_replay.py p1-verdict`.
#   Lifecycle (Amendment 19 HR-2, `_inputs.sh` wa_guard): an interrupt, the
#   hard deadline (P1_TIMEOUT_S, default 14400 s; WA_DEADLINE_S overrides it)
#   or the runner's own death still kills P1's group and every process
#   carrying the run token, and judges the family once.
set -u
P=$(cd "$(dirname "$0")" && pwd)
ARGS=(); for a in "$@"; do if [ "$a" = --live ]; then export WA_ALLOW_LIVE=1; else ARGS+=("$a"); fi; done
set -- ${ARGS[@]+"${ARGS[@]}"}
. "$P/_inputs.sh"
SRC=$1; TAG=$2; TREE=$3; KEY=$4; ROLE=$5; shift 5
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
OUT=$X/run-$TAG; ROOT=$X/clone-$TAG
{ [ -e "$OUT" ] || [ -e "$ROOT" ]; } && { echo "run or clone exists; use a fresh TAG" >&2; exit 2; }
mkdir -p "$OUT" "$X/tmp"
P1PID=""
stop() {
  if [ -n "$P1PID" ]; then
    wa_kill 9 "-$P1PID" || wa_kill 9 "$P1PID"
    wa_wait interrupted "$P1PID"; P1PID=""
  fi
}
wa_guard "${P1_TIMEOUT_S:-14400}" stop
. "$P/_prep.sh"
PY=/opt/homebrew/bin/python3
mkdir -p "$ROOT" && cp -cR "$SRC/data" "$ROOT/data" || { echo "APFS clone of $SRC/data failed" >&2; exit 2; }
export TMPDIR=$X/tmp/ PYTHONDONTWRITEBYTECODE=1
( wa_exec $PY "$P/projection_replay.py" p1 --tree "$TREE" --db "$ROOT/data" --conversation "$KEY" \
  --role "$ROLE" --reps "${REPS:-5}" --dylib "$DYLIB" --out "$OUT/p1.json" "$@" ) > "$OUT/p1.log" 2>&1 &
P1PID=$!; WA_EXPECT+=("$P1PID")
wa_wait p1 "$P1PID"
RC=$?
P1PID=""
wa_finish_once || RC=2
echo "__exit_status=$RC"; exit $RC
