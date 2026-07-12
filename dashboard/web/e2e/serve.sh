#!/usr/bin/env bash
# #281 S3 — Playwright reader smoke-net launcher. Playwright's `webServer` runs
# this; it owns the fixture state + the dashboard server on the dedicated e2e
# port 8797 (never 8789 dev / 8799 ui-qa).
#
# Every cctally invocation here runs under FULL isolation (spec §5): the scratch
# CCTALLY_DATA_DIR / CLAUDE_CONFIG_DIR / CODEX_HOME plus the dev-autodetect and
# telemetry suppressors, so the suite NEVER reads or writes the operator's real
# ~/.claude, ~/.codex, or ~/.local/share/cctally*. CODEX_HOME must be pinned even
# though we only sync `--source claude`: a stray `cache-sync` default would ingest
# the real ~/.codex.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # dashboard/web/e2e
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RUNTIME="$SCRIPT_DIR/.runtime"

# 1) Rebuild the runtime dir from scratch — no state bleed between runs.
rm -rf "$RUNTIME"
mkdir -p "$RUNTIME"

# 2) Isolation env — pinned before ANY cctally call.
export CCTALLY_DATA_DIR="$RUNTIME/scratch/data"
export CLAUDE_CONFIG_DIR="$RUNTIME/scratch/claude"
export CODEX_HOME="$RUNTIME/scratch/codex"
export CCTALLY_DISABLE_DEV_AUTODETECT=1
export CCTALLY_DISABLE_TELEMETRY=1

# 3) Generate the synthetic transcripts + manifest.json under the runtime dir.
python3 "$REPO_ROOT/bin/build-e2e-fixtures.py" --out "$RUNTIME"

# 4) Disable the dashboard's update-check thread. It consults CONFIG, not the
#    environment (docs/updates-gotchas.md: `_should_show_update_banner` reads
#    `config.update.check.enabled`), so a scratch env var can't turn it off — set
#    the config key in the scratch data dir instead. Keeps the suite offline and
#    off any update banner.
"$REPO_ROOT/bin/cctally" config set update.check.enabled false >/dev/null

# 5) Pre-prime cache.db (claude only) so the per-session rollup is authoritative
#    before the first request — no cold-sync in-flux reads, no "indexing" notes.
"$REPO_ROOT/bin/cctally" cache-sync --source claude

# 6) Serve. Sync stays ENABLED (no --no-sync) so the open reader live-tails via
#    the targeted per-conversation ingest (scenario 3). exec so Playwright's
#    teardown signal reaches the server directly.
exec "$REPO_ROOT/bin/cctally" dashboard --port 8797 --host 127.0.0.1 --no-browser
