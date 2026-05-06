#!/usr/bin/env bash
# Verifies the data dir is gone after `setup --uninstall --purge --yes`.
set -euo pipefail
fake="$1"
if [ -d "$fake/.local/share/cctally" ]; then
    echo "data dir still exists at $fake/.local/share/cctally"
    exit 1
fi
exit 0
