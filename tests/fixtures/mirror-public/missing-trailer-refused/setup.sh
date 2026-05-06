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
cp "$REPO_ROOT/.githooks/_public_trailer.py" .githooks/
cp "$REPO_ROOT/.githooks/_skip_chain_metrics.py" .githooks/
# The mirror tool resolves _REPO_ROOT via __file__.parent.parent at
# import time. Copy it INTO the scratch private's bin/ and run from
# there so it walks the scratch private repo (not cctally-dev itself).
cp "$REPO_ROOT/bin/cctally-mirror-public" bin/
chmod +x bin/cctally-mirror-public
# Optional: .public-tag-patterns drives tag propagation. Copy it in so
# the tag-propagated/tag-held-back scenarios match the live config.
cp "$REPO_ROOT/.public-tag-patterns" .

# Infra-bootstrap commit. Contents are public-classified, but we tag
# mirror-cursor here so the mirror walks ONLY scenario-specific commits
# that follow this point. The infra commit message itself carries a
# `--- public ---` block so it would be a valid publish if walked, but
# the mirror-cursor tag below ensures the mirror starts AFTER it.
git add -A
git commit --no-verify -q -F - <<'CCTALLY_INFRA_MSG_EOF'
chore: infra bootstrap

--- public ---
chore: infra bootstrap
CCTALLY_INFRA_MSG_EOF
git -c tag.gpgsign=false tag mirror-cursor HEAD

# cwd remains $work/private/ for scenario-specific bash that follows.
echo "y" > README.md
git add README.md
git commit --no-verify -q -F - <<'CCTALLY_MSG_EOF'
fix: tweak readme
CCTALLY_MSG_EOF
