# Sourced FIRST by every runner (spec §6.3 revision 15, Q16, `dc13` L1/L4),
# after P is set and before anything is created: the run's input mode and the
# one launch contract of its process family.
#
#   WRITE_ATTRIBUTION_INPUTS=frozen:FREEZE   every family process starts through
#       sandbox-exec -f FREEZE/frozen.sb python3 frozen_launch.py ... -- TARGET
#       and reads the sealed freeze through the rootmap namespace
#   WRITE_ATTRIBUTION_INPUTS=live            the real roots, read-only: run-live.sh,
#       run-scratch-proof.sh and the live-roots confirmation (run-workload.sh L)
#       only; any other runner refuses live inputs unless --live was passed
#       (the runner exports WA_ALLOW_LIVE=1 for it)
#
# A live-only runner sets WA_LIVE_ONLY=1 before sourcing. A runner without a
# mode, with a mode its kind may not use, or with a directory that is not a
# sealed freeze exits 2 here, before it builds, creates or launches anything.
# Defines WA_MODE (frozen|live), WA_FREEZE, and:
#   wa_exec CMD ARGS...   exec CMD as a family process (call it in a subshell);
#                         in frozen mode the libraries the caller exported in
#                         DYLD_INSERT_LIBRARIES (the write interposer) are
#                         handed to the launcher, which composes them after
#                         rootmap.dylib (sandbox-exec strips DYLD_*)
#   wa_wait STEP PID      wait for a top-level family process started in the
#                         background, `( ... wa_exec CMD ... ) & wa_wait STEP $!`,
#                         and return its exit status. Its parent is this shell,
#                         which the namespace does not load, so nothing else
#                         records how it ended: in frozen mode the end goes to
#                         $WA_RECEIPTS/toplevel.jsonl (Amendment 13 O3; a status
#                         above 128 is signal STATUS-128), and `frozen_roots.py
#                         family` judges a signal there as it judges a member's
#                         termination (teardown.jsonl and familyKills exempt)
#   wa_kill SIG TARGET    the runner's own teardown kill of a top-level family
#                         process (TARGET a pid, or -PGID for its process group):
#                         recorded first in $WA_RECEIPTS/teardown.jsonl (frozen
#                         mode), then sent. Returns 1, recording and sending
#                         nothing, when the process has already ended (a zombie
#                         included), so a sandbox kill is never relabelled as
#                         the harness's teardown
#   wa_finish [PID...]    frozen mode: verify the freeze again, reap any
#                         family process still alive (bounded), and judge the
#                         family's activation receipts; writes
#                         $OUT/freeze-verify-after.json and
#                         $OUT/frozen-family.json and adds both to
#                         $OUT/inputs.json. Returns 2 when either is invalid.
#   wa_finish_once [PID...]  wa_finish at most once per run (the runner's own
#                         call and the exit trap's never both judge)
#   wa_guard LIMIT_S [CLEANUP]  (Amendment 19 HR-1/HR-2) called once OUT
#                         exists and before the runner builds or starts
#                         anything: `set -m` (every background job leads its
#                         own process group), a fresh WA_RUN_TOKEN exported
#                         into every family process's environment, traps on
#                         EXIT INT TERM HUP and a hard-deadline watchdog
#                         (WA_DEADLINE_S overrides LIMIT_S). On EVERY exit
#                         the trap runs CLEANUP (the runner's own ordered
#                         kills), kills every process group the runner still
#                         leads (each kill and end recorded), reaps every
#                         process carrying the run token - workers that
#                         called setsid included - into
#                         OUT/token-teardown.jsonl (wa_procs.py; HR-7), and
#                         runs wa_finish_once with WA_EXPECT. An interrupt is
#                         recorded in OUT/interrupted.jsonl; at the deadline
#                         the watchdog writes OUT/deadline.json and sends the
#                         runner SIGTERM (SIGKILL after 60 s). If the runner
#                         itself dies without its trap (SIGKILL), the
#                         watchdog kills the groups it last saw the runner
#                         lead, reaps the token, runs CLEANUP and
#                         wa_finish_once, and writes OUT/orphaned.json.
#   wa_sleep N            a sleep a trapped signal interrupts
#   wa_reap_detached      kill the family's detached workers now (the frozen
#                         family's receipts, then the run token), recorded:
#                         a terminal drain must run with no other process on
#                         the clone (HR-21)
WA_INPUTS=${WRITE_ATTRIBUTION_INPUTS:-}
case $WA_INPUTS in
  frozen:?*) WA_MODE=frozen; WA_FREEZE=${WA_INPUTS#frozen:} ;;
  live)      WA_MODE=live; WA_FREEZE= ;;
  *) echo "refusing: no input mode; set WRITE_ATTRIBUTION_INPUTS=frozen:FREEZE or" \
          "WRITE_ATTRIBUTION_INPUTS=live (spec §6.3 revision 15)" >&2; exit 2 ;;
esac
if [ "${WA_LIVE_ONLY:-0}" = 1 ] && [ "$WA_MODE" != live ]; then
  echo "refusing: this runner reads the live roots only (WRITE_ATTRIBUTION_INPUTS=live)" >&2
  exit 2
fi
if [ "$WA_MODE" = live ] && [ "${WA_LIVE_ONLY:-0}" != 1 ] && [ "${WA_ALLOW_LIVE:-0}" != 1 ]; then
  echo "refusing: live inputs on this runner need an explicit --live" \
       "(frozen:FREEZE is the default method, spec §6.3 revision 15)" >&2
  exit 2
fi
if [ "$WA_MODE" = frozen ]; then
  if [ ! -f "$WA_FREEZE/seal.json" ] || [ ! -f "$WA_FREEZE/frozen.sb" ] \
     || [ ! -f "$WA_FREEZE/rootmap.tsv" ]; then
    echo "refusing: $WA_FREEZE is not a sealed freeze (frozen_roots.py capture)" >&2
    exit 2
  fi
  WA_FREEZE=$(cd "$WA_FREEZE" && pwd -P)
fi
export WRITE_ATTRIBUTION_INPUTS

wa_exec() {
  if [ "$WA_MODE" = frozen ]; then
    local libs=${DYLD_INSERT_LIBRARIES:-}
    unset DYLD_INSERT_LIBRARIES
    exec /usr/bin/sandbox-exec -f "$WA_FREEZE/frozen.sb" /opt/homebrew/bin/python3 \
      "$P/frozen_launch.py" --freeze "$WA_FREEZE" --rootmap "$ROOTMAP" \
      --receipts "$WA_RECEIPTS" --libs "$libs" -- "$@"
  fi
  exec "$@"
}

WA_WAITING=
wa_wait() {
  local step=$1 pid=$2 rc
  WA_WAITING=$pid
  wait "$pid" 2>/dev/null; rc=$?
  WA_WAITING=
  wa_record_end "$step" "$pid" "$rc"
  return $rc
}

# STEP PID STATUS -> one toplevel/1 line (frozen mode only).
wa_record_end() {
  [ "$WA_MODE" = frozen ] && [ -n "${WA_RECEIPTS:-}" ] || return 0
  local step pid=$2 rc=$3 exited=true code=$3 signaled=false sig=null
  step=$(printf '%s' "$1" | tr -d '"\\')
  if [ "$rc" -gt 128 ]; then exited=false; code=null; signaled=true; sig=$((rc - 128)); fi
  mkdir -p "$WA_RECEIPTS"
  printf '{"schema":"toplevel/1","pid":%d,"status":%d,"exited":%s,"code":%s,"signaled":%s,"signal":%s,"runner":"%s","step":"%s","t":%s}\n' \
    "$pid" "$rc" "$exited" "$code" "$signaled" "$sig" "$(basename "$0")" "$step" \
    "$(date +%s)" >> "$WA_RECEIPTS/toplevel.jsonl"
}

# A group kill is recorded for EVERY live member of the group (HR-21), not
# only its leader: a system program a member started is still running at
# teardown, and the family judge needs its kill on record.
wa_kill() {
  local sig=$1 target=$2 pid=${2#-} group=false state members m
  [ "$target" != "$pid" ] && group=true
  kill -0 -- "$target" 2>/dev/null || return 1
  if [ "$group" = true ]; then
    members=$(ps -axo pid=,pgid=,stat= 2>/dev/null \
      | awk -v g="$pid" '$2 == g && $3 !~ /^Z/ {print $1}')
    [ -n "$members" ] || return 1
  else
    state=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')
    case $state in Z*) return 1 ;; esac
    members=$pid
  fi
  if [ "$WA_MODE" = frozen ] && [ -n "${WA_RECEIPTS:-}" ]; then
    mkdir -p "$WA_RECEIPTS"
    for m in $members; do
      printf '{"pid":%d,"signal":%d,"group":%s,"leader":%d,"t":%s,"by":"%s"}\n' \
        "$m" "$sig" "$group" "$pid" "$(date +%s)" "$(basename "$0")" \
        >> "$WA_RECEIPTS/teardown.jsonl"
    done
  fi
  kill -"$sig" -- "$target" 2>/dev/null
}

wa_finish() {
  [ "$WA_MODE" = frozen ] || return 0
  [ -n "${WA_RECEIPTS:-}" ] || return 0          # interrupted before _prep.sh
  local rc=0 expect=() p
  for p in "$@"; do [ -n "$p" ] && expect+=(--expect-pid "$p"); done
  /opt/homebrew/bin/python3 "$P/frozen_roots.py" reap --receipts "$WA_RECEIPTS" \
    > "$OUT/frozen-reap.json" 2>&1
  /opt/homebrew/bin/python3 "$P/frozen_roots.py" verify --freeze "$WA_FREEZE" \
    --out "$OUT/freeze-verify-after.json" > /dev/null || rc=2
  /opt/homebrew/bin/python3 "$P/frozen_roots.py" family --receipts "$WA_RECEIPTS" \
    --freeze "$WA_FREEZE" ${expect[@]+"${expect[@]}"} --out "$OUT/frozen-family.json" \
    > /dev/null || rc=2
  /opt/homebrew/bin/python3 "$P/frozen_roots.py" inputs --run "$OUT" --finish > /dev/null || rc=2
  return $rc
}

# Each judging is recorded in OUT/finish.jsonl with who ran it (the runner,
# its exit trap, or its watchdog for a runner that died untrapped).
wa_finish_once() {
  [ -n "$WA_FINISHED" ] && return 0
  WA_FINISHED=1
  printf '{"t":%s,"by":"%s","runner":"%s"}\n' "$(date +%s)" "${WA_FINISH_BY:-runner}" \
    "$(basename "$0")" >> "$OUT/finish.jsonl"
  wa_finish "$@"
}

# ── the runner's lifecycle (Amendment 19 HR-1, HR-2, HR-7) ─────────────────
WA_FINISHED=
WA_GUARD_DONE=
WA_GUARD_CLEANUP=
WA_WATCHDOG=
WA_SLEEP_PID=
WA_EXPECT=()
WA_RUN_TOKEN=
# The harness's own helpers (not measured family processes) run on the
# pinned interpreter when the host has it.
WA_HELPER_PY=/opt/homebrew/bin/python3
[ -x "$WA_HELPER_PY" ] || WA_HELPER_PY=python3

# PARENT [EXCLUDE]: the children of PARENT that lead their own process group.
wa_leaders_of() {
  ps -axo pid=,ppid=,pgid= 2>/dev/null \
    | awk -v me="$1" -v wd="${2:-0}" '$2 == me && $1 == $3 && $1 != wd {print $1}'
}

wa_guard() {
  local limit=${WA_DEADLINE_S:-$1}
  WA_GUARD_CLEANUP=${2:-}
  case $limit in ''|*[!0-9]*) echo "wa_guard: a whole-second deadline is required" >&2; exit 2 ;; esac
  set -m
  # _prep.sh names the receipts here too; the watchdog forks now and must
  # judge the family's receipts if it ever outlives the runner.
  WA_RECEIPTS=${WA_RECEIPTS:-$OUT/rootmap}
  WA_RUN_TOKEN="wa-$(basename "$0")-$$-$(date +%s)-$RANDOM"
  export WA_RUN_TOKEN
  trap 'wa_on_exit' EXIT
  trap 'wa_on_signal INT 130' INT
  trap 'wa_on_signal TERM 143' TERM
  trap 'wa_on_signal HUP 129' HUP
  wa_watchdog "$limit"
}

wa_on_signal() {
  printf '{"signal":"%s","t":%s,"runner":"%s"}\n' "$1" "$(date +%s)" \
    "$(basename "$0")" >> "$OUT/interrupted.jsonl"
  exit "$2"
}

wa_sleep() {
  sleep "$1" &
  WA_SLEEP_PID=$!
  wait "$WA_SLEEP_PID" 2>/dev/null
  WA_SLEEP_PID=
}

# Kill (recorded) and wait every process group the runner still leads.
wa_teardown() {
  local p
  for p in $(wa_leaders_of "$$" "${WA_WATCHDOG:-0}"); do
    wa_kill 9 "-$p" || wa_kill 9 "$p"
    wa_wait teardown "$p"
  done
}

# Every process carrying the run token, setsid workers included (HR-7);
# in frozen mode each kill is also a teardown record of the family.
wa_reap_token() {
  [ -n "$WA_RUN_TOKEN" ] || return 0
  local teardown=()
  if [ "$WA_MODE" = frozen ] && [ -n "${WA_RECEIPTS:-}" ]; then
    mkdir -p "$WA_RECEIPTS"
    teardown=(--teardown "$WA_RECEIPTS/teardown.jsonl")
  fi
  "$WA_HELPER_PY" "$P/wa_procs.py" reap --token "$WA_RUN_TOKEN" \
    --record "$OUT/token-teardown.jsonl" ${teardown[@]+"${teardown[@]}"} \
    --by "$(basename "$0")" >> "$OUT/token-teardown.log" 2>&1
}

wa_reap_detached() {
  if [ "$WA_MODE" = frozen ] && [ -d "${WA_RECEIPTS:-/nonexistent}" ]; then
    "$WA_HELPER_PY" "$P/frozen_roots.py" reap --receipts "$WA_RECEIPTS" \
      >> "$OUT/frozen-reap-detached.json" 2>&1
  fi
  wa_reap_token
}

wa_on_exit() {
  local rc=$?
  [ -n "$WA_GUARD_DONE" ] && return
  WA_GUARD_DONE=1
  trap '' INT TERM HUP
  if [ -n "$WA_SLEEP_PID" ]; then
    kill -9 -- "-$WA_SLEEP_PID" 2>/dev/null || kill -9 "$WA_SLEEP_PID" 2>/dev/null
    wait "$WA_SLEEP_PID" 2>/dev/null
    WA_SLEEP_PID=
  fi
  if [ -n "$WA_GUARD_CLEANUP" ]; then "$WA_GUARD_CLEANUP"; fi
  wa_teardown
  wa_reap_token
  WA_FINISH_BY=trap
  wa_finish_once ${WA_EXPECT[@]+"${WA_EXPECT[@]}"} || { [ "$rc" = 0 ] && rc=2; }
  : > "$OUT/.wa-finished" 2>/dev/null
  if [ -n "$WA_WATCHDOG" ]; then
    kill -TERM -- "-$WA_WATCHDOG" 2>/dev/null || kill -TERM "$WA_WATCHDOG" 2>/dev/null
    wait "$WA_WATCHDOG" 2>/dev/null
  fi
  exit "$rc"
}

# The runner died without its trap (SIGKILL): the watchdog finishes its job.
wa_orphaned() {
  local p
  printf '{"t":%s,"runner":"%s","groups":"%s"}\n' "$(date +%s)" \
    "$(basename "$0")" "$1" > "$OUT/orphaned.json"
  for p in $1; do
    wa_kill 9 "-$p" || wa_kill 9 "$p"
  done
  wa_reap_token
  if [ -n "$WA_GUARD_CLEANUP" ]; then "$WA_GUARD_CLEANUP"; fi
  WA_FINISH_BY=watchdog
  wa_finish_once ${WA_EXPECT[@]+"${WA_EXPECT[@]}"}
  : > "$OUT/.wa-finished" 2>/dev/null
}

wa_watchdog() {
  local limit=$1 runner=$$
  (
    trap '' INT HUP
    trap 'exit 0' TERM
    exec < /dev/null > /dev/null 2>&1
    self=$(exec sh -c 'echo $PPID')
    n=0 seen=
    while kill -0 "$runner" 2>/dev/null; do
      if [ "$n" -ge "$limit" ]; then
        printf '{"limitS":%s,"t":%s,"runner":"%s"}\n' "$limit" "$(date +%s)" \
          "$(basename "$0")" > "$OUT/deadline.json"
        kill -TERM "$runner" 2>/dev/null
        g=0
        while [ "$g" -lt 60 ] && kill -0 "$runner" 2>/dev/null; do
          sleep 1; g=$((g + 1))
        done
        kill -0 "$runner" 2>/dev/null || exit 0
        kill -KILL "$runner" 2>/dev/null
        break
      fi
      now=$(wa_leaders_of "$runner" "$self")
      [ -n "$now" ] && seen=$now
      sleep 1
      n=$((n + 1))
    done
    [ -e "$OUT/.wa-finished" ] && exit 0
    wa_orphaned "$seen"
  ) &
  WA_WATCHDOG=$!
}

# A wrapper runner (run-p2.sh, run-p3.sh, run-lifecycle.sh) runs the runner
# it wraps as its own process group and forwards an interrupt to it, so the
# inner runner's own guard tears its family down and judges it.
WA_CHILD=
WA_CHILD_SIGNAL=
wa_child() {
  local rc
  set -m
  "$@" &
  WA_CHILD=$!
  trap 'WA_CHILD_SIGNAL=130; kill -TERM "$WA_CHILD" 2>/dev/null' INT
  trap 'WA_CHILD_SIGNAL=143; kill -TERM "$WA_CHILD" 2>/dev/null' TERM
  trap 'WA_CHILD_SIGNAL=129; kill -TERM "$WA_CHILD" 2>/dev/null' HUP
  wait "$WA_CHILD"; rc=$?
  while kill -0 "$WA_CHILD" 2>/dev/null; do wait "$WA_CHILD"; rc=$?; done
  trap - INT TERM HUP
  [ -n "$WA_CHILD_SIGNAL" ] && exit "$WA_CHILD_SIGNAL"
  return $rc
}

# HR-19: a store at or under the live data directory - resolved through the
# password database, never $HOME - is refused before anything starts.
wa_refuse_live_store() {
  local live store
  live=$("$WA_HELPER_PY" -c 'import os, pwd; print(os.path.realpath(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local", "share", "cctally")))')
  store=$("$WA_HELPER_PY" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1")
  case "$store/" in
    "$live"/*) echo "refusing: $1 is the live data directory ($live); measure an APFS clone of a closed copy" >&2
               return 1 ;;
  esac
  return 0
}
