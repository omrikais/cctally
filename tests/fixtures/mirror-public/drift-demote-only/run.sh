#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run1 exit=$rc"; exit "$rc"; fi
test -f ../public/bin/cctally-foo || { echo "ASSERT_FAIL: bin/cctally-foo not on public after run1"; exit 2; }
current=$(cat .mirror-allowlist)
printf '%s\n!bin/cctally-foo\n' "$current" > .mirror-allowlist
git add .mirror-allowlist
git commit --no-verify -q -F - <<'CCTALLY_MSG_EOF_DEMOTE'
chore: privatize cctally-foo via allowlist

Public-Skip: true
CCTALLY_MSG_EOF_DEMOTE
echo "notes v2" > docs/notes.md
git add docs/notes.md
git commit --no-verify -q -F - <<'CCTALLY_MSG_EOF_PUB2'
fix: bump notes

--- public ---
docs: refresh notes
CCTALLY_MSG_EOF_PUB2
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run2 exit=$rc"; exit "$rc"; fi
test ! -e ../public/bin/cctally-foo || { echo "ASSERT_FAIL: bin/cctally-foo still on public after demote"; exit 2; }
test -f ../public/docs/notes.md || { echo "ASSERT_FAIL: docs/notes.md missing"; exit 2; }
echo "ASSERT_OK"
