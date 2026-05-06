#!/bin/bash
set -uo pipefail
PUB_HEAD_BEFORE=$(git -C ../public rev-parse HEAD)
python3 bin/cctally-mirror-public \
  --bootstrap \
  --force-bootstrap \
  --dry-run \
  --bootstrap-message-file ../msg/bootstrap.txt \
  --bootstrap-tag v1.0.0 \
  --public-clone ../public \
  --yes
rc=$?
PUB_HEAD_AFTER=$(git -C ../public rev-parse HEAD)
if [ "$PUB_HEAD_BEFORE" = "$PUB_HEAD_AFTER" ]; then
  echo "PUBLIC_HEAD_UNCHANGED"
else
  echo "PUBLIC_HEAD_MUTATED: was=$PUB_HEAD_BEFORE now=$PUB_HEAD_AFTER"
fi
exit $rc
