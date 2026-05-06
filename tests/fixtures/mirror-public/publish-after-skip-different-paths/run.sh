#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "ASSERT_FAIL: mirror exit=$rc"
  exit "$rc"
fi
test -f ../public/docs/skipped.md || { echo "ASSERT_FAIL: skipped.md missing"; exit 2; }
test -f ../public/docs/published.md || { echo "ASSERT_FAIL: published.md missing"; exit 2; }
got_s=$(cat ../public/docs/skipped.md)
got_p=$(cat ../public/docs/published.md)
[ "$got_s" = "skipped content v1" ] || { echo "ASSERT_FAIL: skipped.md content=$got_s"; exit 2; }
[ "$got_p" = "published content v1" ] || { echo "ASSERT_FAIL: published.md content=$got_p"; exit 2; }
echo "ASSERT_OK"
