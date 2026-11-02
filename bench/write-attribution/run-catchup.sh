#!/bin/bash
# usage: WRITE_ATTRIBUTION_INPUTS=frozen:FREEZE WRITE_ATTRIBUTION_SCRATCH=DIR \
#        run-catchup.sh SRC_ROOT TAG TREE [--mode conversations|cache-cold]
#                       [--providers claude,codex] [--passes N] [--cache-sync]
#                       [--ownership]   (bounded)
#   #939: one catch-up pass of TREE on its own APFS clone of SRC_ROOT/data (a
#   closed store copy that `conversation_catchup.py prep` brought to TREE's
#   schema before the freeze was captured), with per-object write attribution.
#   Mode `conversations` (default) is the dashboard's first full conversation
#   pass; `cache-cold` is a cold first Claude accounting sync (#944 residual).
#   The pass runs as a family process through the launch contract
#   (`_inputs.sh` wa_exec), reading FREEZE through the namespace, under the
#   write interposer with its WAL frame log and its main-database page log
#   (WTRACE_DBPAGES=1); TMPDIR and SQLITE_TMPDIR are on the external drive.
#   Before the pass the copy's page maps are taken once (SRC_ROOT/pagemap-*,
#   immutable reads of the closed copy); after it the clone's. Then
#   `catchup_attrib.py` writes attrib.json and attrib.txt, and
#   `accounting_digest.py` the clone's content digest (digest.json) that a
#   before/after comparison on the same copy must agree on. With --ownership
#   (#939 spec 2.2) the driver also logs per-occurrence page ownership to
#   ownership.jsonl (`ownership.py`), and attrib.json charges every write
#   occurrence to its owner at its commit, reporting unresolved bytes
#   (`ownership: "per-occurrence"`); without it, or for a store without
#   pointer maps, the endpoint attribution (`ownership: "endpoint"`).
#   Refuses a copy without a qualification receipt naming this freeze and this
#   copy (frozen_roots.py qualify), and frozen inputs are required (no --live).
#   Bounded: the pass gets CATCHUP_TIMEOUT_S (3600); a step past its bound is
#   killed with its process group and makes the run INVALID (exit 2). The clone
#   is removed on every exit path unless KEEP_CLONE=1 (then the caller removes
#   $WRITE_ATTRIBUTION_SCRATCH/clone-TAG). Receipts in
#   $WRITE_ATTRIBUTION_SCRATCH/run-TAG: driver.json, w.*, pre-*/post-* page
#   maps, attrib.json, attrib.txt, digest.json, inputs.json, frozen-family.json
#   (and ownership.jsonl with --ownership).
set -u
set -m
P=$(cd "$(dirname "$0")" && pwd)
USAGE="usage: run-catchup.sh SRC_ROOT TAG TREE [--mode conversations|cache-cold] [--providers claude,codex] [--passes N] [--cache-sync] [--ownership]"
ARGS=(); MODE=conversations; PROVIDERS=claude,codex; PASSES=1; CACHE_SYNC=(); OWNERSHIP=0; BAD=0
while [ $# -gt 0 ]; do
  case $1 in
    --mode) case ${2:-} in conversations|cache-cold) MODE=$2; shift ;; *) BAD=1 ;; esac ;;
    --providers) case ${2:-} in claude|codex|claude,codex|codex,claude) PROVIDERS=$2; shift ;; *) BAD=1 ;; esac ;;
    --passes) case ${2:-} in [1-9]|[1-9][0-9]) PASSES=$2; shift ;; *) BAD=1 ;; esac ;;
    --cache-sync) CACHE_SYNC=(--cache-sync) ;;
    --ownership) OWNERSHIP=1 ;;
    --live) echo "refusing: run-catchup.sh measures frozen inputs only" >&2; exit 2 ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
set -- ${ARGS[@]+"${ARGS[@]}"}
. "$P/_inputs.sh"
[ "$WA_MODE" = frozen ] || { echo "refusing: frozen inputs only" >&2; exit 2; }
{ [ $# -eq 3 ] && [ $BAD = 0 ]; } || { echo "$USAGE" >&2; exit 2; }
SRC=$(cd "$1" 2>/dev/null && pwd) || { echo "no store copy at $1" >&2; exit 2; }
TAG=$2
TREE=$(cd "$3" 2>/dev/null && pwd) || { echo "no tree at $3" >&2; exit 2; }
[ -d "$SRC/data" ] || { echo "no store copy at $SRC/data" >&2; exit 2; }
X=${WRITE_ATTRIBUTION_SCRATCH:?set WRITE_ATTRIBUTION_SCRATCH to an external-drive directory}
OUT=$X/run-$TAG; ROOT=$X/clone-$TAG
{ [ -e "$OUT" ] || [ -e "$ROOT" ]; } && { echo "run or clone exists; use a fresh TAG" >&2; exit 2; }
mkdir -p "$OUT" "$X/tmp"
PY=/opt/homebrew/bin/python3
"$PY" -c '
import json, os, sys
sys.path.insert(0, sys.argv[1])
import frozen_roots, latency
src, freeze = sys.argv[2], sys.argv[3]
path = os.path.join(src, "frozen-qualification.json")
q = json.load(open(path)) if os.path.exists(path) else None
problems = latency.qualification_problems(q, frozen_roots.seal_identity(freeze), src)
if problems:
    print("\n".join(problems), file=sys.stderr); sys.exit(1)
' "$P" "$SRC" "$WA_FREEZE" || { echo "refusing: $SRC is not qualified for $WA_FREEZE" >&2; exit 2; }
STEP_PID=""
cleanup() {
  if [ -n "$STEP_PID" ]; then
    wa_kill 9 "-$STEP_PID" || wa_kill 9 "$STEP_PID"
    wa_wait interrupted "$STEP_PID"; STEP_PID=""
  fi
  if [ "${KEEP_CLONE:-0}" != 1 ] && [ -n "$ROOT" ] && [ "$ROOT" = "$X/clone-$TAG" ]; then
    rm -rf -- "$ROOT"
  fi
}
wa_guard $(( ${CATCHUP_TIMEOUT_S:-3600} + 1800 )) cleanup
. "$P/_prep.sh"
export_clone_env() {
  export TMPDIR=$X/tmp/ SQLITE_TMPDIR=$X/tmp/ CCTALLY_DATA_DIR=$ROOT/data \
    PYTHONDONTWRITEBYTECODE=1 CCTALLY_DISABLE_TELEMETRY=1
  unset CLAUDE_CONFIG_DIR CODEX_HOME
}
step() {
  local name=$1 limit=$2 watch rc
  shift 2
  ( export_clone_env
    export WTRACE_OUT=$OUT/w WTRACE_PERIOD=5 WTRACE_DBPAGES=1 DYLD_INSERT_LIBRARIES=$DYLIB
    wa_exec "$@" ) > "$OUT/$name.log" 2>&1 &
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
case $MODE in conversations) DBS="conversations.db" ;; cache-cold) DBS="cache.db" ;; esac
[ ${#CACHE_SYNC[@]} -gt 0 ] && [ "$MODE" = conversations ] && DBS="conversations.db cache.db"
# The copy's page maps, once per copy (immutable: the copy is closed and bound).
for db in $DBS; do
  if [ "$MODE" = cache-cold ]; then
    "$PY" -c 'import array, json, sys
open(sys.argv[1] + ".u16", "wb").write(array.array("H", [0]).tobytes())
json.dump({"db": None, "pageSize": 4096, "pageCount": 0, "freelist": 0,
           "names": ["<unowned>"], "types": {}, "tables": {}, "pages": {}},
          open(sys.argv[1] + ".json", "w"))' "$OUT/pre-$db"
    continue
  fi
  if [ ! -f "$SRC/pagemap-$db.json" ]; then
    "$PY" "$P/pagemap.py" --db "$SRC/data/$db" --out "$SRC/pagemap-$db" --immutable \
      > "$OUT/pagemap-pre-$db.log" 2>&1 || { echo "pre page map failed" >&2; exit 2; }
  fi
  cp "$SRC/pagemap-$db.json" "$OUT/pre-$db.json"; cp "$SRC/pagemap-$db.u16" "$OUT/pre-$db.u16"
done
mkdir -p "$ROOT" && cp -cR "$SRC/data" "$ROOT/data" || { echo "APFS clone of $SRC/data failed" >&2; exit 2; }
rm -f "$ROOT"/data/frozen-qualification.json
cd "$TREE" || exit 2
OWNERSHIP_ARGS=(); [ $OWNERSHIP = 1 ] && OWNERSHIP_ARGS=(--ownership "$OUT/ownership.jsonl")
step driver "${CATCHUP_TIMEOUT_S:-3600}" "$PY" "$P/conversation_catchup.py" run \
  --tree "$TREE" --out "$OUT/driver.json" --mode "$MODE" --providers "$PROVIDERS" \
  --passes "$PASSES" ${CACHE_SYNC[@]+"${CACHE_SYNC[@]}"} ${OWNERSHIP_ARGS[@]+"${OWNERSHIP_ARGS[@]}"}
RC=$?
DPID=$STEP_PID; STEP_PID=""
echo "$DPID" > "$OUT/pid"
wa_finish_once $DPID || RC=2
# The WAL each store left on disk after the pass's process exited (its final
# close normally checkpoints it to empty): retained WAL debt is outside the
# measured bytes, so a run that leaves any must say so.
"$PY" -c '
import json, os, sys
out = {}
for db in sys.argv[2:]:
    p = os.path.join(sys.argv[1], db + "-wal")
    out[db] = os.path.getsize(p) if os.path.exists(p) else None
print(json.dumps(out))' "$ROOT/data" $DBS > "$OUT/wal-after.json" 2>/dev/null || RC=2
if [ $RC = 0 ]; then
  for db in $DBS; do
    ( export TMPDIR=$X/tmp/ SQLITE_TMPDIR=$X/tmp/
      "$PY" "$P/pagemap.py" --db "$ROOT/data/$db" --out "$OUT/post-$db" ) \
      > "$OUT/pagemap-post-$db.log" 2>&1 || RC=2
  done
fi
if [ $RC = 0 ]; then
  "$PY" "$P/catchup_attrib.py" "$OUT" --json "$OUT/attrib.json" > "$OUT/attrib.txt" 2>&1 || RC=2
fi
if [ $RC = 0 ]; then
  DIGEST_DBS=(); for db in $DBS; do DIGEST_DBS+=(--db "$db"); done
  ( export TMPDIR=$X/tmp/ SQLITE_TMPDIR=$X/tmp/
    "$PY" "$P/accounting_digest.py" digest --data "$ROOT/data" --out "$OUT/digest.json" \
      --fts-integrity "${DIGEST_DBS[@]}" ) > "$OUT/digest.log" 2>&1 || RC=2
fi
cleanup
echo "__exit_status=$RC"; exit $RC
