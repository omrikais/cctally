#!/bin/bash
set -euo pipefail
work="$(pwd)"
REPO_ROOT="$1"

mkdir -p "$work/private" "$work/public"

# Public side: init + one empty commit so HEAD is a valid commit ref.
cd "$work/public"
git init -q --initial-branch=main 2>/dev/null || git init -q
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
git config tag.gpgsign false
git commit -q --allow-empty -m "init"

# Private side: init + identity + copy real infra files in.
cd "$work/private"
git init -q --initial-branch=main 2>/dev/null || git init -q
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
git config tag.gpgsign false

mkdir -p .githooks bin
cp "$REPO_ROOT/.mirror-allowlist" .
cp "$REPO_ROOT/.githooks/_match.py" .githooks/
# #281 S9 retired the trailer machinery — these fixtures exercise only
# cmd_bootstrap + cmd_reconcile, neither of which needs _public_trailer.py
# or _skip_chain_metrics.py (the mirror tool's skip-chain import is gone).
# The mirror tool resolves _REPO_ROOT via __file__.parent.parent at
# import time. Copy it INTO the scratch private's bin/ and run from
# there so it walks the scratch private repo (not cctally-dev itself).
cp "$REPO_ROOT/bin/cctally-mirror-public" bin/
chmod +x bin/cctally-mirror-public
# Optional: .public-tag-patterns drives tag propagation. Copy it in so
# the tag-propagated/tag-held-back scenarios match the live config.
cp "$REPO_ROOT/.public-tag-patterns" .

# Infra-bootstrap commit + mirror-cursor at HEAD (the cursor establishes
# the pre-publish baseline the reconcile scenarios drift away from).
git add -A
git commit --no-verify -q -m "chore: infra bootstrap"
git -c tag.gpgsign=false tag mirror-cursor HEAD

# cwd remains $work/private/ for scenario-specific bash that follows.
pushd "$work/public" >/dev/null
echo "uncommitted draft" > UNCOMMITTED.md
popd >/dev/null
