#!/usr/bin/env bash
# Issue #119 — pinned-only-path brew box (spec §5.3 g, setup half).
#
# Builds the brew keg (as brew-dry-run-json/presetup.sh does) AND seeds a
# LIVE legacy `~/.local/bin/cctally` link pointing INTO the keg
# (/Cellar/cctally/ target). The scenario leaves <prefix>/bin OFF the
# scrubbed PATH (no INCLUDE_BREW_PREFIX_BIN), so `_reachable_elsewhere`
# finds no other copy and the link is the command's ONLY path. Result:
# `_setup_compute_symlink_state` classes the slot `wrong`,
# `symlinks_path_pinned` is true, and `_setup_install` LEAVES the link
# (the active-only reachability gate refuses to break the sole copy) while
# printing the PATH-fix guidance.
set -euo pipefail
: "${REPO_SCRATCH:?REPO_SCRATCH must be set}"
: "${FAKE_HOME:?FAKE_HOME must be set}"

# This version also appears in golden-symlinks.txt (the surviving keg link's
# target). Keep the two in sync if you bump it.
VER="1.21.0"
PREFIX="$FAKE_HOME/opt/homebrew"
KEG="$PREFIX/Cellar/cctally/$VER/libexec/bin"
mkdir -p "$KEG" "$PREFIX/bin"

cp "$REPO_SCRATCH/bin/cctally" "$KEG/"
cp "$REPO_SCRATCH"/bin/_lib_*.py "$KEG/" 2>/dev/null || true
cp "$REPO_SCRATCH"/bin/_cctally_*.py "$KEG/" 2>/dev/null || true
for n in cctally-alerts cctally-dashboard cctally-dollar-per-percent \
         cctally-five-hour-blocks cctally-five-hour-breakdown cctally-forecast \
         cctally-project cctally-refresh-usage cctally-statusline \
         cctally-sync-week cctally-tui cctally-update; do
    cp "$REPO_SCRATCH/bin/$n" "$KEG/" 2>/dev/null || true
done
if [ -f "$REPO_SCRATCH/CHANGELOG.md" ]; then
    cp "$REPO_SCRATCH/CHANGELOG.md" "$PREFIX/Cellar/cctally/$VER/libexec/"
fi
chmod +x "$KEG/cctally"

# Faithful brew: every USER_FACING_BIN symlinked into <prefix>/bin. But
# this scenario leaves <prefix>/bin OFF the scrubbed PATH, so none of these
# count toward reachability — the only path to `cctally` is the legacy
# ~/.local/bin link below.
for n in cctally cctally-alerts cctally-dashboard cctally-dollar-per-percent \
         cctally-five-hour-blocks cctally-five-hour-breakdown cctally-forecast \
         cctally-project cctally-refresh-usage cctally-statusline \
         cctally-sync-week cctally-tui cctally-update; do
    ln -sf "../Cellar/cctally/$VER/libexec/bin/$n" "$PREFIX/bin/$n"
done

# The legacy keg-pointing link in ~/.local/bin — the pinned ONLY path.
# Absolute target so it carries the /Cellar/cctally/ token the retirement
# predicate keys off.
mkdir -p "$FAKE_HOME/.local/bin"
ln -sf "$KEG/cctally" "$FAKE_HOME/.local/bin/cctally"

printf '%s\n' "$KEG/cctally" > "$REPO_SCRATCH/.harness-launcher"
