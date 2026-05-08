#!/usr/bin/env bash
# Reshape the per-scenario scratch repo as an npm-install layout
# (`node_modules/cctally/`) BUT remove `bin/cctally-npm-shim.js`. This
# exercises the inner `shim.exists()` fallback inside
# `_setup_resolve_symlink_source`: the outer gate
# (`"node_modules" in repo_root.parts`) fires, but the shim file is
# absent, so the resolver still falls through to `bin/cctally`. A real
# instance of this state is unlikely on disk (npm publishes the shim);
# the scenario protects against future regressions where the gate
# blindly assumes the shim exists once the layout matches.
set -euo pipefail

mkdir -p "$REPO_SCRATCH/node_modules/cctally"
mv "$REPO_SCRATCH/bin" "$REPO_SCRATCH/node_modules/cctally/bin"
ln -s "node_modules/cctally/bin" "$REPO_SCRATCH/bin"
rm -f "$REPO_SCRATCH/node_modules/cctally/bin/cctally-npm-shim.js"
