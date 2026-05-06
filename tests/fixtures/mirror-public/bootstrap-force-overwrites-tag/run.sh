#!/bin/bash
set -uo pipefail
PUB_OLD_TAG_SHA=$(git -C ../public rev-parse v1.0.0)
python3 bin/cctally-mirror-public \
  --bootstrap \
  --force-bootstrap \
  --bootstrap-message-file ../msg/bootstrap.txt \
  --bootstrap-tag v1.0.0 \
  --public-clone ../public \
  --yes
rc=$?
PUB_NEW_TAG_SHA=$(git -C ../public rev-parse v1.0.0)
if [ "$PUB_OLD_TAG_SHA" != "$PUB_NEW_TAG_SHA" ]; then
  echo "TAG_OVERWRITTEN"
else
  echo "TAG_NOT_OVERWRITTEN: $PUB_OLD_TAG_SHA"
fi
exit $rc
