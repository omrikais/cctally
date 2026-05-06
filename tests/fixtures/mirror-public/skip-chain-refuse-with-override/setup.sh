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
mkdir -p docs
echo "skip1" > docs/s1.md
git add docs/s1.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_1_MSG_EOF'
chore: skip 1

Public-Skip: true
CCTALLY_SKIP_1_MSG_EOF
echo "skip2" > docs/s2.md
git add docs/s2.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_2_MSG_EOF'
chore: skip 2

Public-Skip: true
CCTALLY_SKIP_2_MSG_EOF
echo "skip3" > docs/s3.md
git add docs/s3.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_3_MSG_EOF'
chore: skip 3

Public-Skip: true
CCTALLY_SKIP_3_MSG_EOF
echo "skip4" > docs/s4.md
git add docs/s4.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_4_MSG_EOF'
chore: skip 4

Public-Skip: true
CCTALLY_SKIP_4_MSG_EOF
echo "skip5" > docs/s5.md
git add docs/s5.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_5_MSG_EOF'
chore: skip 5

Public-Skip: true
CCTALLY_SKIP_5_MSG_EOF
echo "skip6" > docs/s6.md
git add docs/s6.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_6_MSG_EOF'
chore: skip 6

Public-Skip: true
CCTALLY_SKIP_6_MSG_EOF
echo "skip7" > docs/s7.md
git add docs/s7.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_7_MSG_EOF'
chore: skip 7

Public-Skip: true
CCTALLY_SKIP_7_MSG_EOF
echo "skip8" > docs/s8.md
git add docs/s8.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_8_MSG_EOF'
chore: skip 8

Public-Skip: true
CCTALLY_SKIP_8_MSG_EOF
echo "skip9" > docs/s9.md
git add docs/s9.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_9_MSG_EOF'
chore: skip 9

Public-Skip: true
CCTALLY_SKIP_9_MSG_EOF
echo "skip10" > docs/s10.md
git add docs/s10.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_10_MSG_EOF'
chore: skip 10

Public-Skip: true
CCTALLY_SKIP_10_MSG_EOF
echo "skip11" > docs/s11.md
git add docs/s11.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_11_MSG_EOF'
chore: skip 11

Public-Skip: true
CCTALLY_SKIP_11_MSG_EOF
echo "skip12" > docs/s12.md
git add docs/s12.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_12_MSG_EOF'
chore: skip 12

Public-Skip: true
CCTALLY_SKIP_12_MSG_EOF
echo "skip13" > docs/s13.md
git add docs/s13.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_13_MSG_EOF'
chore: skip 13

Public-Skip: true
CCTALLY_SKIP_13_MSG_EOF
echo "skip14" > docs/s14.md
git add docs/s14.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_14_MSG_EOF'
chore: skip 14

Public-Skip: true
CCTALLY_SKIP_14_MSG_EOF
echo "skip15" > docs/s15.md
git add docs/s15.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_15_MSG_EOF'
chore: skip 15

Public-Skip: true
CCTALLY_SKIP_15_MSG_EOF
echo "skip16" > docs/s16.md
git add docs/s16.md
git commit --no-verify -q -F - <<'CCTALLY_SKIP_16_MSG_EOF'
chore: skip 16

Public-Skip: true
CCTALLY_SKIP_16_MSG_EOF
echo "pub" > docs/p.md
git add docs/p.md
git commit --no-verify -q -F - <<'CCTALLY_PUB_MSG_EOF'
fix: thing

--- public ---
fix: thing
CCTALLY_PUB_MSG_EOF
