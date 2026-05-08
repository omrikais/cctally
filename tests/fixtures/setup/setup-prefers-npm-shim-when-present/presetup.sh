#!/usr/bin/env bash
# Move the per-scenario scratch repo into a `node_modules/cctally/`
# layout so `_setup_resolve_symlink_source` recognizes it as an npm
# install. The resolver gates shim selection on
# `"node_modules" in repo_root.parts`, since the shim file is
# committed to the source tree and its presence alone doesn't imply
# the npm install path. After this rewrite, `Path(__file__).resolve()`
# from `$REPO_SCRATCH/bin/cctally` lands inside
# `$REPO_SCRATCH/node_modules/cctally/bin/cctally`, so the resolver
# picks `cctally-npm-shim.js`.
set -euo pipefail

mkdir -p "$REPO_SCRATCH/node_modules/cctally"
mv "$REPO_SCRATCH/bin" "$REPO_SCRATCH/node_modules/cctally/bin"
ln -s "node_modules/cctally/bin" "$REPO_SCRATCH/bin"
