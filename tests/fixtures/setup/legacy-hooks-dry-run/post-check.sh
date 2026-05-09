#!/bin/bash
# Dry-run invariant: bin/cctally setup --dry-run is read-only. The four
# legacy .py files MUST still exist at their original paths and the
# settings.json MUST be byte-identical to the input. Failures produce a
# specific error line so the harness's `head -20` excerpt is informative.
set -uo pipefail

fake="$1"
repo_root="$2"
fixture_dir="$repo_root/tests/fixtures/setup/legacy-hooks-dry-run"

# 1) The four canonical legacy .py files survived the dry-run.
for f in record-usage-stop.py usage-poller-start.py usage-poller-stop.py usage-poller.py; do
    if [ ! -f "$fake/.claude/hooks/$f" ]; then
        echo "post-check: missing $f after dry-run (should be untouched)"
        exit 1
    fi
done

# 2) settings.json byte-identical to the input. The harness's input dir
# is the canonical pre-state; diff against it directly. The /REPO ->
# REPO_ROOT template-expand the harness applies (only for paths
# containing the literal string `/REPO`) is a no-op on the legacy
# settings.json since it contains no /REPO references.
if ! diff -q "$fixture_dir/input/.claude/settings.json" "$fake/.claude/settings.json" >/dev/null; then
    echo "post-check: settings.json modified by --dry-run (should be byte-identical to input)"
    diff -u "$fixture_dir/input/.claude/settings.json" "$fake/.claude/settings.json" | head -30
    exit 1
fi

# 3) No backup directory created.
if compgen -G "$fake/.claude/cctally-legacy-hook-backup-*" >/dev/null 2>&1; then
    echo "post-check: --dry-run created a backup dir (should be no-op)"
    exit 1
fi

exit 0
