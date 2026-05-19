#!/bin/bash
set -uo pipefail
pushd ../public >/dev/null
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/omrikais/cctally.git
popd >/dev/null
python3 bin/cctally-mirror-public --public-clone ../public --reconcile --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: reconcile1 exit=$rc"; exit "$rc"; fi
HEAD_AFTER_RUN1=$(git -C ../public rev-parse HEAD)
python3 bin/cctally-mirror-public --public-clone ../public --reconcile --yes \
  > /tmp/cctally-reconcile-idempotent-run2.txt 2>&1
rc=$?
cat /tmp/cctally-reconcile-idempotent-run2.txt
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: reconcile2 exit=$rc"; exit "$rc"; fi
HEAD_AFTER_RUN2=$(git -C ../public rev-parse HEAD)
if [ "$HEAD_AFTER_RUN1" != "$HEAD_AFTER_RUN2" ]; then
  echo "ASSERT_FAIL: idempotent reconcile produced a second commit"
  exit 2
fi
grep -q "no drift to reconcile" /tmp/cctally-reconcile-idempotent-run2.txt || { echo "ASSERT_FAIL: expected no-drift message on second run"; exit 2; }
echo "ASSERT_OK"
