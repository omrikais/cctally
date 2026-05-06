#!/bin/bash
set -uo pipefail
PRIVATE_HEAD_A=$(git rev-parse HEAD)
CURSOR_BEFORE=$(git rev-parse refs/tags/mirror-cursor)
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run1 exit=$rc"; exit "$rc"; fi
CURSOR_AFTER1=$(git rev-parse refs/tags/mirror-cursor)
[ "$CURSOR_BEFORE" = "$CURSOR_AFTER1" ] || { echo "ASSERT_FAIL: cursor advanced after skip-only run "\"(was=$CURSOR_BEFORE now=$CURSOR_AFTER1)"; exit 2; }
echo "published content v1" > docs/published.md
git add docs/published.md
git commit --no-verify -q -F - <<'CCTALLY_MSG_EOF_B'
fix: add published doc

--- public ---
docs: add published
CCTALLY_MSG_EOF_B
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run2 exit=$rc"; exit "$rc"; fi
test -f ../public/docs/skipped.md || { echo "ASSERT_FAIL: skipped.md missing"; exit 2; }
test -f ../public/docs/published.md || { echo "ASSERT_FAIL: published.md missing"; exit 2; }
got_s=$(cat ../public/docs/skipped.md)
got_p=$(cat ../public/docs/published.md)
[ "$got_s" = "skipped content v1" ] || { echo "ASSERT_FAIL: skipped.md content=$got_s"; exit 2; }
[ "$got_p" = "published content v1" ] || { echo "ASSERT_FAIL: published.md content=$got_p"; exit 2; }
echo "ASSERT_OK"
