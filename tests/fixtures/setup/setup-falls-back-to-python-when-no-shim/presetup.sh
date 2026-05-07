#!/usr/bin/env bash
# Remove the npm shim from the per-scenario scratch repo BEFORE
# `cctally setup` runs. This simulates a source-clone or Homebrew tap
# install layout — Python script present, shim absent — and exercises
# the `_setup_resolve_symlink_source` fallback to `bin/cctally`.
set -euo pipefail
rm -f "$REPO_SCRATCH/bin/cctally-npm-shim.js"
