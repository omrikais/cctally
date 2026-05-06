#!/usr/bin/env bash
# First-table latency benchmark for cctally vs. ccusage.
#
# Measures `cctally daily` (cold + warm) and `ccusage daily` on the
# user's existing ~/.claude/projects/ data. Prints median elapsed time
# in seconds for each variant. Wraps `hyperfine` if available; falls
# back to running each command 5x via `time` and reporting the median.
#
# Usage: bench/cctally-vs-ccusage.sh [--days N]
#
# The `--days` window defaults to 30. The benchmark expects ccusage
# on PATH; if absent, it skips the ccusage variant with a clear message
# (so cctally-only timings still land).
#
# Caveats: results depend on (a) hardware, (b) total session JSONL
# volume in ~/.claude/projects/, (c) cold/warm filesystem state.
# See bench/README.md for methodology.
set -euo pipefail

DAYS=30
while [[ $# -gt 0 ]]; do
    case "$1" in
        --days) DAYS="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,/^set/p' "$0" | grep -E '^# ?' | sed 's/^# *//'
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if ! command -v cctally >/dev/null 2>&1; then
    echo "bench: cctally not on PATH (run \`./bin/cctally setup\` or symlink first)" >&2
    exit 1
fi

CACHE_DB="${HOME}/.local/share/cctally/cache.db"
NOW="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

# `cctally daily` and `ccusage daily` both take --since YYYYMMDD; neither has
# a --days flag. Compute SINCE = (today - DAYS) in UTC. BSD `date` (macOS,
# the project's primary platform) and GNU `date` (Linux) take incompatible
# flags for relative-date math, so branch on uname.
if [[ "$(uname -s)" == "Darwin" ]]; then
    SINCE="$(date -u -v-"${DAYS}"d +%Y%m%d)"
else
    SINCE="$(date -u -d "$DAYS days ago" +%Y%m%d)"
fi

print_section() {
    printf '\n=== %s ===\n' "$1"
}

if command -v hyperfine >/dev/null 2>&1; then
    print_section "Using hyperfine (5 runs, 2 warmup)"

    # Cold cache: delete cache.db before each run (hyperfine --prepare).
    print_section "cctally daily — cold cache"
    hyperfine --warmup 2 --runs 5 \
        --prepare "rm -f \"$CACHE_DB\"" \
        "cctally daily --since $SINCE"

    print_section "cctally daily — warm cache"
    hyperfine --warmup 2 --runs 5 \
        "cctally daily --since $SINCE"

    if command -v ccusage >/dev/null 2>&1; then
        print_section "ccusage daily — for comparison"
        hyperfine --warmup 2 --runs 5 \
            "ccusage daily --since $SINCE"
    else
        echo
        echo "bench: ccusage not on PATH; skipping comparison row." >&2
        echo "       (install via: npm install -g ccusage)" >&2
    fi
else
    print_section "hyperfine not found; falling back to 'time' (5 runs each, median printed)"

    median_of_5() {
        local label="$1"; shift
        local times=()
        for _ in 1 2 3 4 5; do
            local t
            t=$( { time -p "$@" >/dev/null 2>&1; } 2>&1 | awk '/^real/ {print $2}' )
            times+=( "$t" )
        done
        local median
        median=$(printf '%s\n' "${times[@]}" | sort -n | sed -n '3p')
        printf '%-40s median=%ss   (samples: %s)\n' "$label" "$median" "${times[*]}"
    }

    # Each iteration: delete cache.db, then run cctally. Genuinely cold-cache.
    # A single pre-loop `rm -f` would only cold the FIRST iteration.
    cctally_cold() {
        rm -f "$CACHE_DB"
        cctally daily --since "$SINCE"
    }

    print_section "cctally daily — cold cache"
    median_of_5 "cctally daily --since $SINCE  (cold)" cctally_cold

    print_section "cctally daily — warm cache"
    median_of_5 "cctally daily --since $SINCE  (warm)" cctally daily --since "$SINCE"

    if command -v ccusage >/dev/null 2>&1; then
        print_section "ccusage daily"
        median_of_5 "ccusage daily --since $SINCE" ccusage daily --since "$SINCE"
    else
        echo
        echo "bench: ccusage not on PATH; skipping comparison row." >&2
        echo "       (install via: npm install -g ccusage)" >&2
    fi
fi

print_section "Run finished at: $NOW"
