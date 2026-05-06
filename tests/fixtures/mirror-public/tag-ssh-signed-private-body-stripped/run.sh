#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public --public-clone ../public --yes
rc=$?
if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: mirror exit=$rc"; exit "$rc"; fi
git -C ../public tag -l | grep -qx "v1.0.0" || { echo "ASSERT_FAIL: v1.0.0 not propagated"; exit 2; }
pub_tag_obj=$(git -C ../public cat-file -p v1.0.0)
if echo "$pub_tag_obj" | grep -q "BEGIN SSH SIGNATURE"; then
  echo "ASSERT_FAIL: public v1.0.0 body leaks SSH signature"; exit 2;
fi
if ! echo "$pub_tag_obj" | grep -q "ssh-signed release"; then
  echo "ASSERT_FAIL: public v1.0.0 body missing human message"; exit 2;
fi
if echo "$pub_tag_obj" | grep -qE "BEGIN (PGP|SSH) SIGNATURE"; then
  echo "ASSERT_FAIL: public v1.0.0 tag itself is signed"; exit 2;
fi
echo "ASSERT_OK"
