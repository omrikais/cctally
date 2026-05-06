#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: mirror exit=$rc"; exit "$rc"; fi
git -C ../public tag -l | grep -qx "v0.1.0" || { echo "ASSERT_FAIL: v0.1.0 not propagated"; exit 2; }
TAG_SHA=$(git -C ../public rev-list -n1 v0.1.0)
EXPECTED_SHA=$(git -C ../public rev-parse HEAD)
PA_SHA=$(git -C ../public rev-list --reverse HEAD | sed -n 2p)
[ "$TAG_SHA" = "$EXPECTED_SHA" ] || {
  echo "ASSERT_FAIL: v0.1.0 tag SHA=$TAG_SHA expected $EXPECTED_SHA (HEAD/PC) — bound to $PA_SHA (PA) instead?"; exit 2;
}
[ "$TAG_SHA" != "$PA_SHA" ] || {
  echo "ASSERT_FAIL: v0.1.0 tag wrongly bound to PA_SHA=$PA_SHA"; exit 2;
}
echo "ASSERT_OK"
