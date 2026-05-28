#!/usr/bin/env bash
# Issue #119 brew keg builder (shared shape across brew-* scenarios).
#
# The harness has copied the repo's bin/ into $REPO_SCRATCH/bin/ and runs
# this with REPO_SCRATCH + FAKE_HOME set. We reshape that copy into a fake
# Homebrew keg so the launched `cctally` runs from a /Cellar/cctally/ path
# — `_setup_resolve_repo_root()` calls Path(__file__).resolve(), so a real
# COPY (not a symlink) is required to fire `_setup_is_brew_install`.
#
# Keg layout mirrors homebrew/cctally.rb.template `install`:
#   <prefix>/Cellar/cctally/<ver>/libexec/bin/{cctally,USER_FACING_BINS,
#                                              _lib_*.py,_cctally_*.py}
#   <prefix>/Cellar/cctally/<ver>/libexec/CHANGELOG.md
#   <prefix>/bin/cctally -> ../Cellar/.../libexec/bin/cctally  (stable hook
#                                                               target)
# We write the keg's cctally path to $REPO_SCRATCH/.harness-launcher so the
# harness launches it (overriding the default repo-scratch launcher).
set -euo pipefail
: "${REPO_SCRATCH:?REPO_SCRATCH must be set}"
: "${FAKE_HOME:?FAKE_HOME must be set}"

VER="1.21.0"
PREFIX="$FAKE_HOME/opt/homebrew"
KEG="$PREFIX/Cellar/cctally/$VER/libexec/bin"
mkdir -p "$KEG" "$PREFIX/bin"

# Copy the binary set the formula ships (real copies — symlinks would
# resolve back to the source checkout and defeat brew detection).
cp "$REPO_SCRATCH/bin/cctally" "$KEG/"
cp "$REPO_SCRATCH"/bin/_lib_*.py "$KEG/" 2>/dev/null || true
cp "$REPO_SCRATCH"/bin/_cctally_*.py "$KEG/" 2>/dev/null || true
for n in cctally-alerts cctally-dashboard cctally-dollar-per-percent \
         cctally-five-hour-blocks cctally-five-hour-breakdown cctally-forecast \
         cctally-project cctally-refresh-usage cctally-statusline \
         cctally-sync-week cctally-tui cctally-update; do
    cp "$REPO_SCRATCH/bin/$n" "$KEG/" 2>/dev/null || true
done
# CHANGELOG.md powers `cctally --version`; ship it where the formula does.
if [ -f "$REPO_SCRATCH/CHANGELOG.md" ]; then
    cp "$REPO_SCRATCH/CHANGELOG.md" "$PREFIX/Cellar/cctally/$VER/libexec/"
fi
chmod +x "$KEG/cctally"

# The formula symlinks EVERY USER_FACING_BIN into <prefix>/bin
# (cctally.rb.template: `bin.install_symlink libexec/bin/#{name}`), so a
# faithful brew install has all 13 commands reachable via <prefix>/bin —
# `cctally` is the stable, version-stable hook target (#119). Relative
# targets so they survive the $fake mktemp prefix.
for n in cctally cctally-alerts cctally-dashboard cctally-dollar-per-percent \
         cctally-five-hour-blocks cctally-five-hour-breakdown cctally-forecast \
         cctally-project cctally-refresh-usage cctally-statusline \
         cctally-sync-week cctally-tui cctally-update; do
    ln -sf "../Cellar/cctally/$VER/libexec/bin/$n" "$PREFIX/bin/$n"
done

# Tell the harness to launch the keg copy (fires brew detection).
printf '%s\n' "$KEG/cctally" > "$REPO_SCRATCH/.harness-launcher"
