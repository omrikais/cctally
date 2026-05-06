#!/bin/bash
set -uo pipefail
python3 bin/cctally-mirror-public \
  --bootstrap \
  --bootstrap-message-file ../msg/bootstrap.txt \
  --bootstrap-tag v1.0.0 \
  --public-clone ../public \
  --yes
