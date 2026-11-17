# Run background work under bin/_lib_hold.py, so the script never signals a
# process number (#1087). Bash 3.2 compatible; safe under `set -euo pipefail`.
#
# Bash reaps a finished background job asynchronously in its SIGCHLD handler,
# so `kill -0 "$pid"` followed by `kill -TERM "$pid"` can reach whichever
# process was given that number in between. A script that later stops or
# resumes a background process therefore starts it under the holder, which is
# the work's parent and only reaper and signals it only while it holds it
# unreaped, and talks to the holder through files in a control directory:
#
#   hold_start DIR SCOPE [--grace S] [--kill-wait S] -- ARGV...
#       Starts the holder in the background with `--parent` set to the calling
#       shell's own pid (right in a subshell, where `$$` is not) and sets
#       HOLD_PID. SCOPE is `child` everywhere except bin/cctally-test-all's
#       pool and pytest legs, which use `group`. Redirections on the call reach
#       the work: `hold_start "$d" child -- server >"$log" 2>&1`.
#   hold_alive PID     status 0 while the holder is a running job of THIS
#                      shell (the job table; no signal).
#   hold_cont DIR      asks the holder to send SIGCONT to the work.
#   hold_wait PID DIR  waits for the holder without requesting a stop.
#   hold_stop PID DIR  requests a stop (DIR/stop), then does what hold_wait does.
#
# hold_wait and hold_stop `wait` for the holder, the calling shell's own child,
# so `wait` returns its saved status even after an asynchronous reap, and set
# from the published DIR/status:
#   HOLD_STARTED   1 when the work was started
#   HOLD_RC        the work's status as bash reports it, 137 when it was sent
#                  SIGKILL and not reaped, 3 when it was not started (4 when
#                  the holder could not start it), or the holder's own wait
#                  status when nothing was published
#   HOLD_COMPLETE  1 when the holder settled the work completely
#   HOLD_CHILD     the work's pid
#   HOLD_SURVIVORS the pids the holder could not confirm gone (group scope)
#   HOLD_TERMED, HOLD_KILLED  1 when the holder sent SIGTERM / SIGKILL
#   HOLD_PUBLISHED 1 when a status was read
# A missing or unreadable status, as after an outside SIGKILL of the holder,
# gives HOLD_COMPLETE=0.
#
# hold_start also records, for the callers that stop by directory alone
# (bin/_lib-kill-server.sh): DIR/holder (HOLD_PID), DIR/grace and DIR/label.
#
# Callers whose work runs in `bash -c` workers export these functions with
# `export -f`; the holder's path is in the exported `_HOLD_PY`.

# Always the holder beside this file: an inherited `_HOLD_PY` (exported by an
# enclosing script from another tree) must not pick the holder.
_HOLD_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib_hold.py"
export _HOLD_PY

hold_start() {   # DIR SCOPE [--grace S] [--kill-wait S] -- ARGV...
    local dir=$1 scope=$2 parent grace=5 label="" opts=()
    shift 2
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
        if [ "$1" = "--grace" ]; then grace=$2; fi
        opts+=("$1" "$2")
        shift 2
    done
    if [ "${1:-}" = "--" ]; then shift; fi
    HOLD_PID=""
    mkdir -p "$dir"
    rm -f "$dir/status" "$dir/stop" "$dir/cont" "$dir/holder"
    # The label names the work in messages: its first word that is not `env`,
    # one of env's `-u NAME` options or a NAME=VALUE assignment.
    local skip_next=0 word
    for word in "$@"; do
        if [ "$skip_next" = 1 ]; then skip_next=0; continue; fi
        case "$word" in
            (env) continue ;;
            (-u) skip_next=1; continue ;;
            (*=*) continue ;;
        esac
        label=${word##*/}
        break
    done
    parent=$(exec sh -c 'echo "$PPID"')
    python3 "$_HOLD_PY" --control "$dir" --parent "$parent" --scope "$scope" \
        ${opts[@]+"${opts[@]}"} -- "$@" &
    HOLD_PID=$!
    printf '%s\n' "$HOLD_PID" > "$dir/holder"
    printf '%s\n' "$grace" > "$dir/grace"
    printf '%s\n' "$label" > "$dir/label"
    return 0
}

hold_alive() {   # PID
    local job
    [ -n "${1:-}" ] || return 1
    for job in $(jobs -rp); do
        if [ "$job" = "$1" ]; then return 0; fi
    done
    return 1
}

hold_cont() {   # DIR
    { : > "$1/cont"; } 2>/dev/null || true
    return 0
}

# Reads DIR/status into the HOLD_* variables. $2 is the holder's wait status.
# The HOLD_* variables are this library's outputs, read by its callers.
# shellcheck disable=SC2034
_hold_read() {   # DIR WAIT-STATUS
    local file="$1/status" line key value
    local started="" code="" complete="" child="" survivors="" termed="" killed=""
    HOLD_STARTED=0 HOLD_COMPLETE=0 HOLD_CHILD="" HOLD_SURVIVORS=""
    HOLD_TERMED=0 HOLD_KILLED=0 HOLD_PUBLISHED=0 HOLD_RC=$2
    if [ ! -r "$file" ]; then return 0; fi
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            (\"*\":*) ;;
            (*) continue ;;
        esac
        key=${line#\"}
        key=${key%%\"*}
        value=${line#*\": }
        value=${value%,}
        case "$key" in
            (started) started=$value ;;
            (status_code) code=$value ;;
            (complete) complete=$value ;;
            (pid) child=$value ;;
            (survivors) survivors=$value ;;
            (termed) termed=$value ;;
            (killed) killed=$value ;;
        esac
    done < "$file"
    HOLD_PUBLISHED=1
    if [ "$started" = true ]; then HOLD_STARTED=1; fi
    if [ "$complete" = true ]; then HOLD_COMPLETE=1; fi
    if [ "$termed" = true ]; then HOLD_TERMED=1; fi
    if [ "$killed" = true ]; then HOLD_KILLED=1; fi
    if [ "$child" != null ]; then HOLD_CHILD=$child; fi
    survivors=${survivors#[}
    survivors=${survivors%]}
    HOLD_SURVIVORS=${survivors//,/}
    if [ "$HOLD_STARTED" = 1 ]; then
        case "$code" in
            (''|null) HOLD_RC=137 ;;
            (*) HOLD_RC=$code ;;
        esac
    else
        case "$2" in
            (3|4) ;;
            (*) HOLD_RC=3 ;;
        esac
    fi
    return 0
}

hold_wait() {   # PID DIR
    local pid=$1 dir=$2 st
    while :; do
        st=0
        wait "$pid" 2>/dev/null || st=$?
        if [ -f "$dir/status" ]; then break; fi
        # `wait` interrupted by a trapped signal returns while the holder runs.
        if ! hold_alive "$pid"; then break; fi
    done
    _hold_read "$dir" "$st"
    return 0
}

hold_stop() {   # PID DIR
    { : > "$2/stop"; } 2>/dev/null || true
    hold_wait "$1" "$2"
    return 0
}
