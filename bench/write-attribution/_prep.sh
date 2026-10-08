# Sourced by the runners after _inputs.sh and after OUT and X are set. Builds
# the write interposer and the read namespace into the external scratch
# directory and runs, in order: the write-attribution self-test (unchanged)
# and its analyzer self-test, then - in frozen mode - the namespace self-test
# (frozen_selftest.py on synthetic trees, on this host's runtime, with the
# tested TREE) and the freeze's seal (frozen_roots.py verify). A failure stops
# the run before any family process starts; the output is the run's
# selftest.txt receipt that analyze.py requires (#901 SR-003/SR-004), and
# OUT/inputs.json binds the input mode, the runtime, both libraries, the
# sandbox profile and the freeze to the run (spec §6.3 revision 15, Q16).
# Amendment 15 Q2: workload.py's verdicts and terminal_drain.py's copier
# import the candidate's limits, reclaim planner and retention module from
# the harness's ../../bin; a harness snapshot without them would lose the
# run's terminal drain and its verdict, so refuse before anything starts.
for _wa_mod in _lib_write_budget.py _lib_reclaim_planner.py _lib_conversation_retention.py; do
  [ -f "$P/../../bin/$_wa_mod" ] || {
    echo "the harness has no candidate bin/$_wa_mod beside it ($P/../../bin); refusing to measure" >&2
    exit 2; }
done
# HR-21: TMPDIR is on the external drive before anything below builds or
# self-tests (clang and the self-tests write temporary files).
mkdir -p "$X/tmp"
export TMPDIR=$X/tmp/
DYLIB=$X/wtrace.dylib
clang -O2 -dynamiclib -o "$DYLIB" "$P/wtrace.c" || { echo "wtrace build failed" >&2; exit 2; }
# The frozen read namespace (spec §6.3 "Frozen namespace", Q16), built beside
# the write interposer; frozen_launch.py composes ROOTMAP:DYLIB for a frozen run.
ROOTMAP=$X/rootmap.dylib
clang -O2 -dynamiclib -o "$ROOTMAP" "$P/rootmap.c" || { echo "rootmap build failed" >&2; exit 2; }
WA_RECEIPTS=$OUT/rootmap
{
  WTRACE_TEST_PAUSE_US=300000 DYLD_INSERT_LIBRARIES=$DYLIB WTRACE_DYLIB=$DYLIB \
    /opt/homebrew/bin/python3 "$P/selftest.py" "$X/selftest" &&
  /opt/homebrew/bin/python3 "$P/analyze_selftest.py" "$X/analyze-selftest" &&
  if [ "$WA_MODE" = frozen ]; then
    TMPDIR=$X/tmp/ /opt/homebrew/bin/python3 "$P/frozen_selftest.py" \
      --tree "${TREE:-$P/../..}" --scratch "$X/frozen-selftest" \
      --rootmap "$ROOTMAP" --wtrace "$DYLIB" --out "$OUT/frozen-selftest.json" &&
    /opt/homebrew/bin/python3 "$P/frozen_roots.py" verify --freeze "$WA_FREEZE" \
      --out "$OUT/freeze-verify-before.json"
  fi &&
  /opt/homebrew/bin/python3 "$P/frozen_roots.py" inputs --run "$OUT" --begin \
    --wtrace "$DYLIB" --rootmap "$ROOTMAP" &&
  echo "selftest: PASS"
} > "$OUT/selftest.txt" 2>&1 || { cat "$OUT/selftest.txt" >&2; echo "self-test failed; refusing to measure" >&2; exit 2; }
