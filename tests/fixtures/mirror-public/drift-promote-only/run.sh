#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run1 exit=$rc"; exit "$rc"; fi
test ! -e ../public/bin/cctally-bar || { echo "ASSERT_FAIL: bin/cctally-bar leaked to public on run1"; exit 2; }
test -f ../public/docs/notes.md || { echo "ASSERT_FAIL: docs/notes.md not on public after run1"; exit 2; }
grep -v '^!bin/cctally-bar$' .mirror-allowlist > .mirror-allowlist.new
mv .mirror-allowlist.new .mirror-allowlist
git add .mirror-allowlist
git commit --no-verify -q -F - <<'CCTALLY_MSG_EOF_PROMOTE'
chore: promote cctally-bar via allowlist

Public-Skip: true
CCTALLY_MSG_EOF_PROMOTE
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
test -f ../public/bin/cctally-bar || { echo "ASSERT_FAIL: bin/cctally-bar not promoted to public after run2"; exit 2; }
mode=$(git -C ../public ls-tree HEAD -- bin/cctally-bar | awk '{print $1}')
[ "$mode" = "100755" ] || { echo "ASSERT_FAIL: bin/cctally-bar mode=$mode (expected 100755)"; exit 2; }
echo "ASSERT_OK"
