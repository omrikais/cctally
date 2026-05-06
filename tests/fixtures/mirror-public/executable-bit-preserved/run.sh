#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: mirror exit=$rc"; exit "$rc"; fi
mode=$(git -C ../public ls-tree HEAD -- bin/cctally-newwrapper | awk '{print $1}')
[ "$mode" = "100755" ] || { echo "ASSERT_FAIL: mode=$mode (expected 100755)"; exit 2; }
echo "ASSERT_OK"
