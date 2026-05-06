#!/bin/bash
set -euo pipefail
SCRATCH="$(pwd)"
REPO_ROOT="$1"
mkdir -p repo && cd repo
git init -q
git config user.email "test@example.com"
git config user.name "Test"
mkdir -p .githooks
cp "$REPO_ROOT/.mirror-allowlist" .
cp "$REPO_ROOT/.githooks/_match.py" .githooks/
cp "$REPO_ROOT/.githooks/_public_trailer.py" .githooks/
echo "x" > README.md
git add README.md
cat > ../msg.txt <<'CCTALLY_EOF_MSG_DELIM'
fix: thing

--- public ---
docs: tweak
CCTALLY_EOF_MSG_DELIM
