#!/bin/bash
# usage: WRITE_ATTRIBUTION_INPUTS={frozen:FREEZE|live} run-p3.sh SLICE_DIR SRC_ROOT TAG TREE PORT {w9-off|shipped}   (bounded and stopped)
#   Spec §6.3 "P after revision 12" (Q13, `dc10` H3), P3 mechanism isolation:
#   P2's replay, unchanged (`run-p2.sh`: B's setup, drained admission and the
#   slice re-appended at its original relative times against a running
#   dashboard), once with W9 disabled through the test-only seam
#   CCTALLY_TEST_W9_DISABLE=1 (`w9-off`: every connection keeps SQLite's
#   automatic checkpoint and the end-of-sync shrink is not rate-limited) and
#   once as shipped. Both end with the terminal drain (SIGKILL, then the
#   measured copier). Diagnostic, no verdict of its own: compare the two runs
#   with `workload.py p3-report RUN_W9_OFF RUN_SHIPPED`, which reports per store
#   interposer bytes, commits, WAL frames, distinct pages, checkpoint copy, the
#   rusage/interposer ratio, and window plus drain. The ab-verdict (I2 window
#   plus the drain section) is still written to run-TAG/verdict.txt.
set -u
P=$(cd "$(dirname "$0")" && pwd)
ARGS=(); for a in "$@"; do if [ "$a" = --live ]; then export WA_ALLOW_LIVE=1; else ARGS+=("$a"); fi; done
set -- ${ARGS[@]+"${ARGS[@]}"}
. "$P/_inputs.sh"
[ $# -eq 6 ] || { echo "usage: run-p3.sh SLICE_DIR SRC_ROOT TAG TREE PORT {w9-off|shipped}" >&2; exit 2; }
MODE=$6
case $MODE in
  w9-off)  export CCTALLY_TEST_W9_DISABLE=1 ;;
  shipped) unset CCTALLY_TEST_W9_DISABLE ;;
  *)       echo "mode must be w9-off or shipped" >&2; exit 2 ;;
esac
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
wa_child "$P/run-p2.sh" "$1" "$2" "$3" "$4" "$5"      # an interrupt is forwarded (HR-2)
RC=$?
[ -d "$X/run-$3" ] && echo "{\"p3\": \"$MODE\"}" > "$X/run-$3/p3.json"
echo "__exit_status=$RC"; exit $RC
