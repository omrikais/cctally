#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run1 exit=$rc"; exit "$rc"; fi
mode=$(git -C ../public ls-tree HEAD -- bin/cctally-foo | awk '{print $1}')
[ "$mode" = "100755" ] || { echo "ASSERT_FAIL: expected 100755 after run1, got $mode"; exit 2; }
pushd ../public >/dev/null
chmod 0644 bin/cctally-foo
git update-index --chmod=-x bin/cctally-foo
git commit --no-verify -q -m "chore: simulate drift — chmod 0644"
popd >/dev/null
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -eq 2 ]; then echo "ASSERT_OK_PRECHECK_REFUSED"; exit 2; fi
echo "ASSERT_FAIL: expected exit 2 from precheck, got $rc"
exit "$rc"
