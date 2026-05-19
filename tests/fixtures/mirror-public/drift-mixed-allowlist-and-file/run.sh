#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run1 exit=$rc"; exit "$rc"; fi
test -f ../public/bin/cctally-foo || { echo "ASSERT_FAIL: bin/cctally-foo not on public after run1"; exit 2; }
current=$(cat .mirror-allowlist)
printf '%s\n!bin/cctally-foo\n' "$current" > .mirror-allowlist
echo "#!/bin/bash
echo updated-foo" > bin/cctally-foo
chmod +x bin/cctally-foo
git add .mirror-allowlist bin/cctally-foo
git commit --no-verify -q -F - <<'CCTALLY_MSG_EOF_MIXED'
chore: privatize + tweak cctally-foo

Public-Skip: true
CCTALLY_MSG_EOF_MIXED
echo "notes v2" > docs/notes.md
git add docs/notes.md
git commit --no-verify -q -F - <<'CCTALLY_MSG_EOF_PUB2'
fix: bump notes

--- public ---
docs: refresh notes
CCTALLY_MSG_EOF_PUB2
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run2 exit=$rc (double-emit may have raised)"; exit "$rc"; fi
test ! -e ../public/bin/cctally-foo || { echo "ASSERT_FAIL: bin/cctally-foo still on public"; exit 2; }
test -f ../public/docs/notes.md || { echo "ASSERT_FAIL: docs/notes.md missing"; exit 2; }
echo "ASSERT_OK"
