#!/usr/bin/env bash
# Verifies that no cctally* symlinks were created in ~/.local/bin/
# when setup aborts on a malformed settings.json. Spec §2.2: exit
# code 1 (settings.json malformed) is a "hard prerequisite failure"
# branch that must leave the filesystem untouched.
set -euo pipefail
fake="$1"
local_bin="$fake/.local/bin"
if [ -d "$local_bin" ]; then
    matches=$(find "$local_bin" -maxdepth 1 -name 'cctally*' 2>/dev/null || true)
    if [ -n "$matches" ]; then
        echo "expected no cctally* in $local_bin, found:"
        echo "$matches"
        exit 1
    fi
fi
exit 0
