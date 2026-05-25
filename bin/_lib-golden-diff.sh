# shellcheck shell=bash
# _lib-golden-diff.sh — robust golden-file comparison for the fixture
# harnesses. Pure function definitions; safe to `source`.
#
# Why this exists (issue #106): the update harness used to detect a
# golden mismatch with
#
#     if ! diff -u <(printf '%s\n' "$actual") "$golden" >/dev/null 2>&1; then
#         echo "FAIL …: stderr diverged"
#         diff -u "$golden" <(printf '%s\n' "$actual") | head -200   # re-diff
#     fi
#
# Two latent faults compounded into a rare CI flake:
#   1. The comparison ran over a `<(process substitution)`. Under heavy
#      parallel load on the self-hosted runner, the `/dev/fd/N` backing
#      that substitution intermittently failed to set up, so `diff`
#      exited 2 (trouble) rather than 0/1.
#   2. The DETECTION line redirected `2>&1` to `/dev/null`, so a diff
#      *trouble* exit was indistinguishable from a real content
#      divergence — and the verdict came from a DIFFERENT `diff`
#      invocation than the one shown. When the transient hit detection
#      but not the re-diff, the harness printed "FAIL … diverged" with
#      no diff to show (the content actually matched). Confirmed from the
#      attempt-1 CI log: FAIL line immediately followed by the next
#      scenario's PASS, no unified diff in between.
#
# The fix below removes BOTH faults: ONE `diff` over REAL files (no
# process substitution) drives both the verdict AND the displayed bytes,
# and `diff`'s own stderr is folded into the shown output (`2>&1`) so a
# trouble exit (>=2) surfaces loudly with its message instead of
# masquerading as a silent content diff. Invariant: a non-zero verdict
# is never silent.
#
# Callers must have `$name` set (the scenario name, used in the FAIL
# line). The string variants write their temp files under
# `$GOLDEN_DIFF_TMPDIR` (falls back to `$TMPDIR`/`/tmp`); point it at the
# harness scratch so they're cleaned up with the run.

# _golden_diff_files <label> <golden_file> <actual_file> [max_lines]
#   Compare two real files. Echoes a "FAIL <name>: <label> diverged" line
#   plus the unified diff on any non-zero exit; a trouble exit (>=2) is
#   labelled as harness IO trouble so it isn't mistaken for a content
#   change. Returns 0 = match, 1 = mismatch-or-trouble.
_golden_diff_files () {
    local label="$1" golden="$2" actual="$3" max="${4:-200}"
    local out rc
    # Capture diff's stdout AND stderr so a trouble message (e.g. an
    # unreadable file) is shown, never swallowed. One invocation feeds
    # both the verdict (rc) and the displayed bytes (out).
    out=$(diff -u "$golden" "$actual" 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ]; then
        return 0
    fi
    if [ "$rc" -ge 2 ]; then
        echo "FAIL ${name:-?}: $label diverged (diff error rc=$rc — harness IO trouble, not a content diff)"
    else
        echo "FAIL ${name:-?}: $label diverged"
    fi
    printf '%s\n' "$out" | head -n "$max"
    return 1
}

# _golden_diff_str <label> <golden_file> <actual_string> [max_lines]
#   The actual side is an in-memory string; materialize it (plus a
#   trailing newline, matching the golden files, which end in `\n`) to a
#   real temp file and compare via _golden_diff_files. No process
#   substitution.
_golden_diff_str () {
    local label="$1" golden="$2" actual="$3" max="${4:-200}"
    local dir tmp rc
    dir="${GOLDEN_DIFF_TMPDIR:-${TMPDIR:-/tmp}}"
    tmp=$(mktemp "$dir/golden-diff.XXXXXX") || {
        echo "FAIL ${name:-?}: $label diverged (mktemp failed under $dir)"
        return 1
    }
    printf '%s\n' "$actual" > "$tmp"
    _golden_diff_files "$label" "$golden" "$tmp" "$max"
    rc=$?
    rm -f "$tmp"
    return "$rc"
}

# _golden_diff_two_str <label> <golden_string> <actual_string> [max_lines]
#   Both sides are in-memory strings (e.g. normalized JSON), compared
#   WITHOUT an added trailing newline (printf '%s'), matching the prior
#   `<(printf '%s' …)` behavior. Materializes both to real temp files.
_golden_diff_two_str () {
    local label="$1" golden_s="$2" actual_s="$3" max="${4:-200}"
    local dir gtmp atmp rc
    dir="${GOLDEN_DIFF_TMPDIR:-${TMPDIR:-/tmp}}"
    gtmp=$(mktemp "$dir/golden-diff.XXXXXX") || {
        echo "FAIL ${name:-?}: $label diverged (mktemp failed under $dir)"
        return 1
    }
    atmp=$(mktemp "$dir/golden-diff.XXXXXX") || {
        rm -f "$gtmp"
        echo "FAIL ${name:-?}: $label diverged (mktemp failed under $dir)"
        return 1
    }
    printf '%s' "$golden_s" > "$gtmp"
    printf '%s' "$actual_s" > "$atmp"
    _golden_diff_files "$label" "$gtmp" "$atmp" "$max"
    rc=$?
    rm -f "$gtmp" "$atmp"
    return "$rc"
}
