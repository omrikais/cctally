#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run1 exit=$rc"; exit "$rc"; fi
mode1=$(git -C ../public ls-tree HEAD -- bin/cctally-newwrapper | awk '{print $1}')
[ "$mode1" = "100755" ] || { echo "ASSERT_FAIL: run1 mode=$mode1 (expected 100755)"; exit 2; }
chmod -x bin/cctally-newwrapper
git update-index --chmod=-x bin/cctally-newwrapper
git commit --no-verify -q -F - <<'CCTALLY_MSG_EOF_B'
fix: drop executable bit

--- public ---
chore: drop executable bit on cctally-newwrapper
CCTALLY_MSG_EOF_B
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run2 exit=$rc"; exit "$rc"; fi
mode2=$(git -C ../public ls-tree HEAD -- bin/cctally-newwrapper | awk '{print $1}')
[ "$mode2" = "100644" ] || { echo "ASSERT_FAIL: run2 mode=$mode2 (expected 100644)"; exit 2; }
echo "ASSERT_OK"
