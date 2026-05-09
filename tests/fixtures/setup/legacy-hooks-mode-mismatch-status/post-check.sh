#!/bin/bash
# Mode-mismatch invariant: --status combined with --migrate-legacy-hooks
# is rejected at the cmd_setup gate (Section 2 mode×flag matrix).
# Nothing on disk should change: settings.json byte-identical to input,
# all four canonical .py files preserved, and no ~/.local/bin/ symlinks
# created (since the install flow never reached its symlink step).
set -uo pipefail

fake="$1"
repo_root="$2"
fixture_dir="$repo_root/tests/fixtures/setup/legacy-hooks-mode-mismatch-status"

# 1) settings.json byte-identical to the input. The mode-error path
# returns 2 *before* any settings rewrite — diff against the canonical
# pre-state directly. The harness's /REPO template expansion is a no-op
# here since the fixture's settings.json contains no /REPO references.
if ! diff -q "$fixture_dir/input/.claude/settings.json" "$fake/.claude/settings.json" >/dev/null; then
    echo "post-check: settings.json modified despite mode mismatch (should be byte-identical to input)"
    diff -u "$fixture_dir/input/.claude/settings.json" "$fake/.claude/settings.json" | head -30
    exit 1
fi

# 2) All four canonical legacy .py files preserved.
for f in record-usage-stop.py usage-poller-start.py usage-poller-stop.py usage-poller.py; do
    if [ ! -f "$fake/.claude/hooks/$f" ]; then
        echo "post-check: missing $f after mode mismatch (should be read-only)"
        exit 1
    fi
done

# 3) No symlinks in ~/.local/bin/. The install flow never reached its
# symlink step, so the destination dir should be empty.
if [ -d "$fake/.local/bin" ]; then
    n=$(ls -A "$fake/.local/bin" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$n" != "0" ]; then
        echo "post-check: $n entries in ~/.local/bin/ despite mode mismatch (should be empty)"
        ls -A "$fake/.local/bin"
        exit 1
    fi
fi

# 4) No backup directory created.
if compgen -G "$fake/.claude/cctally-legacy-hook-backup-*" >/dev/null 2>&1; then
    echo "post-check: mode-mismatch path created a backup dir (should be read-only)"
    exit 1
fi

exit 0
