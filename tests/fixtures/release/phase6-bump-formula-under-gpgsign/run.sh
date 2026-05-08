#!/bin/bash
set -uo pipefail
work="$(pwd)/.."
REPO_ROOT="$1"

# Suppress .pyc generation (parity with setup.sh).
export PYTHONDONTWRITEBYTECODE=1

# Determinism: pin author/committer + release date.
export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="test@example.com"
export GIT_AUTHOR_DATE="2026-05-07T00:00:00+0000"
export GIT_COMMITTER_DATE="2026-05-07T00:00:00+0000"
export CCTALLY_RELEASE_DATE_UTC="2026-05-07"

# Fake `gh` recording: prepend the scaffold's fake-bin to PATH.
export PATH="$work/fake-bin:$PATH"
export GH_ARGV_LOG="$work/gh-argv.log"
export GH_NOTES_DEST="$work/_artifacts/gh-notes.txt"

# Per-scenario artifact dir; the harness reads files under it for the
# golden-* comparisons.
mkdir -p "$work/_artifacts"
: > "$work/npm-invocations.log"
export NPM_MOCK_LOG_FILE="$work/npm-invocations.log"
python3 bin/cctally release patch > "$work/_artifacts/stdout.txt" 2> "$work/_artifacts/stderr.txt"
rc=$?
echo "$rc" > "$work/_artifacts/exit.txt"
cp CHANGELOG.md "$work/_artifacts/changelog.md" 2>/dev/null || true
cp package.json "$work/_artifacts/package.json" 2>/dev/null || true
git log -1 --format=%B 2>/dev/null | sed -E "s/[0-9a-f]{7,40}/<SHA7>/g" > "$work/_artifacts/commit-msg.txt" || true
tag_name=$(git tag --points-at HEAD 2>/dev/null | grep -E '^v[0-9]' | head -n1)
if [ -n "$tag_name" ]; then
  git tag -l --format="%(contents)" "$tag_name" > "$work/_artifacts/tag-annotation.txt"
fi
if [ -f "$work/gh-argv.log" ]; then
  cp "$work/gh-argv.log" "$work/_artifacts/gh-argv.log"
fi
if [ -f "$work/npm-invocations.log" ]; then
  cp "$work/npm-invocations.log" "$work/_artifacts/npm-invocations.log"
else
  : > "$work/_artifacts/npm-invocations.log"
fi
# Phase 6: capture the rendered formula (when produced) for golden
# substring checks. `|| true` keeps scenarios that never write
# the formula (skip / refusal / pre-release) from failing here.
cp "$work/homebrew-cctally/Formula/cctally.rb" "$work/_artifacts/formula.rb" 2>/dev/null || true
# Phase 6 (issue #25 regression): verify the tag actually
# landed on the BARE tap remote. Phase 6 returns 0 via
# auth-fallback even when the atomic push silently fails;
# the bare-remote check is what catches the regression.
if [ "$rc" = "0" ] && [ -d "$work/homebrew-cctally.git" ]; then
    if ! git -C "$work/homebrew-cctally.git" rev-parse --verify "refs/tags/v0.1.1" >/dev/null 2>&1; then
        printf 'PHASE6 VERIFY: v0.1.1 tag missing on bare remote (issue #25 regression)\n' >> "$work/_artifacts/stderr.txt"
        echo "1" > "$work/_artifacts/exit.txt"
        rc=1
    fi
fi
exit "$rc"
