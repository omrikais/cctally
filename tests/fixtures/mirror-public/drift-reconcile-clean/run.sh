#!/bin/bash
set -uo pipefail
pushd ../public >/dev/null
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/omrikais/cctally.git
popd >/dev/null
PUB_HEAD_BEFORE=$(git -C ../public rev-parse HEAD)
CURSOR_BEFORE=$(git rev-parse refs/tags/mirror-cursor)
python3 bin/cctally-mirror-public --public-clone ../public --reconcile --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: reconcile exit=$rc"; exit "$rc"; fi
PUB_HEAD_AFTER=$(git -C ../public rev-parse HEAD)
CURSOR_AFTER=$(git rev-parse refs/tags/mirror-cursor)
if [ "$PUB_HEAD_BEFORE" = "$PUB_HEAD_AFTER" ]; then
  echo "ASSERT_FAIL: reconcile produced no commit (HEAD unchanged)"
  exit 2
fi
if [ "$CURSOR_BEFORE" != "$CURSOR_AFTER" ]; then
  echo "ASSERT_FAIL: reconcile advanced mirror-cursor (must not move)"
  exit 2
fi
test ! -e ../public/legacy/stale.md || { echo "ASSERT_FAIL: stale file survived reconcile"; exit 2; }
subject=$(git -C ../public log -1 --format=%s)
[ "$subject" = "chore: reconcile public tree against allowlist" ] || { echo "ASSERT_FAIL: subject=$subject"; exit 2; }
git -C ../public log -1 --format=%B | grep -q "^Reconcile-Source: " || { echo "ASSERT_FAIL: missing Reconcile-Source trailer"; exit 2; }
echo "ASSERT_OK"
