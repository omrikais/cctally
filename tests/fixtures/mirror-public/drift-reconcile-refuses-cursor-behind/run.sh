#!/bin/bash
set -uo pipefail
pushd ../public >/dev/null
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/omrikais/cctally.git
popd >/dev/null
PUB_HEAD_BEFORE=$(git -C ../public rev-parse HEAD)
python3 bin/cctally-mirror-public --public-clone ../public --reconcile --yes
rc=$?
PUB_HEAD_AFTER=$(git -C ../public rev-parse HEAD)
if [ "$PUB_HEAD_BEFORE" != "$PUB_HEAD_AFTER" ]; then
  echo "ASSERT_FAIL: refused reconcile mutated public HEAD"
  exit 2
fi
if [ "$rc" -eq 2 ]; then echo "ASSERT_OK_REFUSE"; exit 2; fi
echo "ASSERT_FAIL: expected exit 2, got $rc"
exit "$rc"
