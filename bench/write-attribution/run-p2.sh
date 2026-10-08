#!/bin/bash
# usage: WRITE_ATTRIBUTION_INPUTS={frozen:FREEZE|live} run-p2.sh SLICE_DIR SRC_ROOT TAG TREE PORT [SECONDS]  (bounded and stopped)
#   SECONDS (revision 13, Q14) shortens or lengthens the measured window
#   (P2_MEASURE_SECONDS, read by run-workload.sh); omitted, it is B's 300 s and
#   P2 is unchanged. `run-lifecycle.sh` uses it for the lifecycle proof.
#   Spec §6.3 P2 (Q12, dc9 G3): B's setup and drained admission (catch-up
#   included) on an APFS clone of SRC_ROOT/data, with the slice that
#   `projection_replay.py p2-extract --window START END --out SLICE_DIR` cut from
#   the rollouts (each file truncated at the window start) placed in the clone's
#   scratch Codex root and its lines re-appended at their original relative times
#   instead of B's ~100 KB/min appender, for B's five measured minutes against a
#   running candidate dashboard. The real roots are read, read-only, as in B, so
#   their ambient growth is recorded and counts against the caps. Then the
#   `workload.py ab-verdict` semantics on the replay: within the I2 caps, zero
#   temp bytes. Verdict: $WRITE_ATTRIBUTION_SCRATCH/run-TAG/verdict.txt.
set -u
P=$(cd "$(dirname "$0")" && pwd)
ARGS=(); for a in "$@"; do if [ "$a" = --live ]; then export WA_ALLOW_LIVE=1; else ARGS+=("$a"); fi; done
set -- ${ARGS[@]+"${ARGS[@]}"}
. "$P/_inputs.sh"
SLICE=$1; SRC=$2; TAG=$3; TREE=$4; PORT=$5
if [ -n "${6:-}" ]; then
  case $6 in ''|*[!0-9]*) echo "SECONDS must be a whole number" >&2; exit 2 ;; esac
  export P2_MEASURE_SECONDS=$6
fi
[ -f "$SLICE/manifest.json" ] || { echo "no slice manifest in $SLICE" >&2; exit 2; }
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
# Amendment 19 HR-2: the inner runner leads its own process group and an
# interrupt is forwarded to it (_inputs.sh wa_child); its own guard tears its
# family down and judges it.
P2_SLICE=$(cd "$SLICE" && pwd)
export P2_SLICE
wa_child "$P/run-workload.sh" B "$SRC" "$TAG" "$TREE" "$PORT"
RC=$?
[ $RC = 0 ] || { echo "__exit_status=$RC"; exit $RC; }
/opt/homebrew/bin/python3 "$P/workload.py" ab-verdict "$X/run-$TAG" --tree "$TREE" \
  | tee "$X/run-$TAG/verdict.txt"
RC=${PIPESTATUS[0]}
echo "__exit_status=$RC"; exit $RC
