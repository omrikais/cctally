#!/bin/bash
# usage: WRITE_ATTRIBUTION_INPUTS={frozen:FREEZE|live} run-lifecycle.sh SLICE_DIR SRC_ROOT TAG TREE PORT [SECONDS]   (bounded and stopped)
#   Spec §6.3 "P after revision 13" (Q14), the lifecycle proof: one P2-style
#   replay (`run-p2.sh`: B's setup, drained admission and the slice re-appended
#   at its original relative times against a running dashboard) of SECONDS
#   (default 150, at least 120), then `workload.py lifecycle-verdict` on its WAL
#   frame log: no commit writes page 1 alone; completed checkpoints at most once
#   per 60 s plus once per 16 MiB of appended frames; a new WAL generation only
#   after a completed checkpoint. The replay's own ab-verdict is written as
#   usual but does not decide this run. Verdict:
#   $WRITE_ATTRIBUTION_SCRATCH/run-TAG/lifecycle.txt.
set -u
P=$(cd "$(dirname "$0")" && pwd)
ARGS=(); for a in "$@"; do if [ "$a" = --live ]; then export WA_ALLOW_LIVE=1; else ARGS+=("$a"); fi; done
set -- ${ARGS[@]+"${ARGS[@]}"}
. "$P/_inputs.sh"
[ $# -ge 5 ] || { echo "usage: run-lifecycle.sh SLICE_DIR SRC_ROOT TAG TREE PORT [SECONDS]" >&2; exit 2; }
SECONDS_ARG=${6:-150}
case $SECONDS_ARG in ''|*[!0-9]*) echo "SECONDS must be a whole number" >&2; exit 2 ;; esac
if [ "$SECONDS_ARG" -lt 120 ]; then
  echo "the lifecycle proof replays at least 120 s" >&2; exit 2
fi
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
wa_child "$P/run-p2.sh" "$1" "$2" "$3" "$4" "$5" "$SECONDS_ARG"   # an interrupt is forwarded (HR-2)
[ -f "$X/run-$3/window.json" ] || { echo "__exit_status=2"; exit 2; }
/opt/homebrew/bin/python3 "$P/workload.py" lifecycle-verdict "$X/run-$3" \
  | tee "$X/run-$3/lifecycle.txt"
RC=${PIPESTATUS[0]}
echo "__exit_status=$RC"; exit $RC
