#!/bin/bash
# Mode-mismatch invariant: --uninstall combined with
# --no-migrate-legacy-hooks is rejected at the cmd_setup gate
# (Section 2 mode×flag matrix). Nothing on disk should change:
# settings.json byte-identical to input, all four canonical .py files
# preserved, no symlinks created (the uninstall path never reached its
# symlink-removal step), and no backup directory.
set -uo pipefail

fake="$1"
repo_root="$2"
fixture_dir="$repo_root/tests/fixtures/setup/legacy-hooks-mode-mismatch-uninstall"

# 1) settings.json byte-identical to the input.
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

# 3) ~/.local/bin/ should be empty — the uninstall path never reached
# the symlink-removal step (which would have been a no-op anyway since
# nothing was installed).
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
