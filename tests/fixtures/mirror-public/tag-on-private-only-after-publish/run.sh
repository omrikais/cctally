#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: mirror exit=$rc"; exit "$rc"; fi
pub_tags=$(git -C ../public tag -l)
echo "$pub_tags" | grep -qx "v1.2.3" && { echo "ASSERT_FAIL: v1.2.3 wrongly propagated"; exit 2; }
echo "ASSERT_OK"
