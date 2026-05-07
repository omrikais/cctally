#!/bin/bash
set -euo pipefail
work="$(pwd)"
export work
REPO_ROOT="$1"

# Suppress .pyc generation across the scenario. cctally's mirror tool
# imports `.githooks/_match`, which would otherwise leave
# `.githooks/__pycache__/` as untracked files in the private worktree
# and trip the release script's clean-tree preflight.
export PYTHONDONTWRITEBYTECODE=1

mkdir -p "$work/private" "$work/private.git" "$work/public" "$work/public.git" "$work/fake-bin" "$work/_artifacts"

# Stable identity for every git invocation in this scenario, regardless of
# host config. Matches the invariants other harnesses rely on for
# byte-stable goldens.
export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="test@example.com"
export GIT_AUTHOR_DATE="2026-05-07T00:00:00+0000"
export GIT_COMMITTER_DATE="2026-05-07T00:00:00+0000"

# Fake `gh` binary: records argv + exits 0 on auth probes. Phase 4 calls
# `gh auth status`, `gh api repos/...`, `gh release view`, then
# `gh release create`. The fake records every invocation; auth-probe
# returns 0; `release view` returns 1 (release does not yet exist);
# `release create` returns 0 (success). When `--notes-file <path>` is
# in argv, the fake copies that file's contents to the artifact dir so
# body-canonical tests can compare it byte-for-byte.
cat > "$work/fake-bin/gh" <<'CCTALLY_FAKE_GH_EOF'
#!/usr/bin/env bash
# Argv recording (one line per invocation; preserves ordering).
echo "$@" >> "${GH_ARGV_LOG:-/dev/null}"

# If the invocation includes `--notes-file <path>`, snapshot the file
# contents to the artifact dir so body-canonical scenarios have a
# stable handle on what was passed.
prev=""
for tok in "$@"; do
  if [ "$prev" = "--notes-file" ] && [ -f "$tok" ] && [ -n "${GH_NOTES_DEST:-}" ]; then
    cp "$tok" "$GH_NOTES_DEST" || true
  fi
  prev="$tok"
done

case "$1" in
  auth) exit "${FAKE_GH_AUTH_EXIT:-0}" ;;
  api)  exit "${FAKE_GH_API_EXIT:-0}" ;;
  release)
    case "$2" in
      view) exit "${FAKE_GH_RELEASE_VIEW_EXIT:-1}" ;;
      *) exit "${FAKE_GH_RELEASE_CREATE_EXIT:-0}" ;;
    esac
    ;;
esac
exit 0
CCTALLY_FAKE_GH_EOF
chmod +x "$work/fake-bin/gh"

# Fake `npm` binary: pre-empts the host's real `npm` (which may be
# installed and authenticated on dev machines, in which case Phase 5
# would attempt a real publish). Default behavior simulates "npm not
# authenticated" — `whoami` exits 1, which routes Phase 5 down its
# auth-fallback branch and returns 0. Batch 6 scenarios that exercise
# the actual publish flow override this by writing their own
# `npm-mock-state.json` and pointing NPM_MOCK_STATE_FILE at it.
cat > "$work/fake-bin/npm" <<'CCTALLY_FAKE_NPM_EOF'
#!/usr/bin/env bash
# Argv recording (one line per invocation).
LOG="${NPM_MOCK_LOG_FILE:-$work/npm-invocations.log}"
printf '%s
' "$*" >> "$LOG" 2>/dev/null || true

# Optional: read responses from a state file (used by Batch 6 scenarios).
STATE="${NPM_MOCK_STATE_FILE:-}"
if [ -n "$STATE" ] && [ -f "$STATE" ]; then
  SUB="${1:-}"
  case "$SUB" in
    whoami)  KEY=whoami ;;
    view)    KEY=view ;;
    publish) KEY=publish ;;
    *) echo "fake-npm: unknown subcommand: $SUB" >&2; exit 1 ;;
  esac
  EXIT=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get(sys.argv[2],{}).get('exit',0))" "$STATE" "$KEY" 2>/dev/null || echo 0)
  STDOUT=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get(sys.argv[2],{}).get('stdout',''))" "$STATE" "$KEY" 2>/dev/null || echo "")
  if [ -n "$STDOUT" ]; then printf '%s
' "$STDOUT"; fi
  exit "$EXIT"
fi

# Default: simulate "not authenticated" so Phase 5 hits the auth
# fallback and returns 0.
case "${1:-}" in
  whoami) exit 1 ;;
  *)      exit 0 ;;
esac
CCTALLY_FAKE_NPM_EOF
chmod +x "$work/fake-bin/npm"

# Public bare + working clone. Init bare first; clone or wire origin so
# the public/ working dir's `origin` points at public.git/.
git init -q --bare --initial-branch=main "$work/public.git" 2>/dev/null \
    || git init -q --bare "$work/public.git"
cd "$work/public"
git init -q --initial-branch=main 2>/dev/null || git init -q
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
git config tag.gpgsign false
git remote add origin "$work/public.git"
git commit -q --allow-empty -m "init"
git push -q origin main

# Private bare + working clone.
git init -q --bare --initial-branch=main "$work/private.git" 2>/dev/null \
    || git init -q --bare "$work/private.git"
cd "$work/private"
git init -q --initial-branch=main 2>/dev/null || git init -q
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
git config tag.gpgsign false
git remote add origin "$work/private.git"
# Wire the public-clone discovery so cmd_release Phase 3 finds it.
# Scenarios that test discovery refusal `git config --unset` this in
# their extra_setup snippet.
git config release.publicClone "$work/public"

# Copy infrastructure files so cctally + the mirror tool resolve
# correctly via __file__.parent.parent inside this scratch repo.
mkdir -p .githooks bin
cp "$REPO_ROOT/.mirror-allowlist" .
cp "$REPO_ROOT/.githooks/_match.py" .githooks/
cp "$REPO_ROOT/.githooks/_public_trailer.py" .githooks/
cp "$REPO_ROOT/.githooks/_skip_chain_metrics.py" .githooks/
cp "$REPO_ROOT/.githooks/commit-msg" .githooks/
cp "$REPO_ROOT/.githooks/pre-commit" .githooks/
cp "$REPO_ROOT/.public-tag-patterns" .
cp "$REPO_ROOT/bin/cctally" bin/
chmod +x bin/cctally
cp "$REPO_ROOT/bin/cctally-mirror-public" bin/
chmod +x bin/cctally-mirror-public

# CHANGELOG.md is seeded per-scenario (after this scaffold ends). The
# infra-bootstrap commit lands AFTER CHANGELOG.md is written so the
# initial commit on `main` already contains a release-shaped CHANGELOG.
cat > CHANGELOG.md <<'CCTALLY_CHANGELOG_EOF'
# Changelog

## [Unreleased]

### Added
- Demo entry

## [0.1.0] - 2026-01-01

### Added
- Initial release
CCTALLY_CHANGELOG_EOF
git add -A
git commit --no-verify -q -F - <<'CCTALLY_SEED_MSG_EOF'
chore: seed CHANGELOG

--- public ---
chore: seed CHANGELOG
CCTALLY_SEED_MSG_EOF
git -c tag.gpgsign=false tag mirror-cursor HEAD
git push -q origin main --follow-tags
git clone -q "$work/private.git" "$work/_advance"
cd "$work/_advance"
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
echo extra >> README
git add README
git commit --no-verify -q -F - <<'CCTALLY_EXTRA_MSG_EOF'
chore: advance origin

Public-Skip: true
CCTALLY_EXTRA_MSG_EOF
git push -q origin main
cd "$work/private"
