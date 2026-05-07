#!/bin/bash
set -euo pipefail
work="$(pwd)"
REPO_ROOT="$1"

mkdir -p "$work/private" "$work/private.git" "$work/public" "$work/public.git" "$work/fake-bin"

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
# `release create` returns 0 (success).
cat > "$work/fake-bin/gh" <<'CCTALLY_FAKE_GH_EOF'
#!/usr/bin/env bash
echo "$@" >> "${GH_ARGV_LOG:-/dev/null}"
case "$1" in
  auth) exit 0 ;;
  api)  exit 0 ;;
  release)
    case "$2" in
      view) exit 1 ;;
      *) exit 0 ;;
    esac
    ;;
esac
exit 0
CCTALLY_FAKE_GH_EOF
chmod +x "$work/fake-bin/gh"

# Public bare + working clone. Init bare first; clone or wire origin so
# the public/ working dir's `origin` points at public.git/.
git init -q --bare --initial-branch=main "$work/public.git" 2>/dev/null     || git init -q --bare "$work/public.git"
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
git init -q --bare --initial-branch=main "$work/private.git" 2>/dev/null     || git init -q --bare "$work/private.git"
cd "$work/private"
git init -q --initial-branch=main 2>/dev/null || git init -q
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
git config tag.gpgsign false
git remote add origin "$work/private.git"
# Wire the public-clone discovery so cmd_release Phase 3 finds it.
git config release.publicClone "$work/public"

# Copy infrastructure files so cctally + the mirror tool resolve
# correctly via __file__.parent.parent inside this scratch repo.
mkdir -p .githooks bin
cp "$REPO_ROOT/.mirror-allowlist" .
cp "$REPO_ROOT/.githooks/_match.py" .githooks/
cp "$REPO_ROOT/.githooks/_public_trailer.py" .githooks/
cp "$REPO_ROOT/.githooks/_skip_chain_metrics.py" .githooks/
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
- New feature A
- New feature B

### Fixed
- Bug X

## [0.1.0] - 2026-01-01

### Added
- Initial public release of cctally
CCTALLY_CHANGELOG_EOF
git add -A
git commit --no-verify -q -F - <<'CCTALLY_SEED_MSG_EOF'
chore: seed CHANGELOG

--- public ---
chore: seed CHANGELOG
CCTALLY_SEED_MSG_EOF
git -c tag.gpgsign=false tag mirror-cursor HEAD
git push -q origin main --follow-tags
