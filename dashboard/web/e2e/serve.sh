#!/usr/bin/env bash
# #281 S3 — Playwright reader smoke-net launcher. Playwright's `webServer` runs
# this; it owns the fixture state + the dashboard server on the dedicated e2e
# port 8797 (never 8789 dev / 8799 ui-qa).
#
# Every cctally invocation here runs under FULL isolation (spec §5): the scratch
# CCTALLY_DATA_DIR / CLAUDE_CONFIG_DIR / CODEX_HOME plus the dev-autodetect and
# telemetry suppressors, so the suite NEVER reads or writes the operator's real
# ~/.claude, ~/.codex, or ~/.local/share/cctally*.
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
CODEX_ROOT_MAIN="$RUNTIME/scratch/codex-main"
CODEX_ROOT_A="$RUNTIME/scratch/codex-a"
CODEX_ROOT_B="$RUNTIME/scratch/codex-b"
export CODEX_HOME="$CODEX_ROOT_MAIN,$CODEX_ROOT_A,$CODEX_ROOT_B"
export CCTALLY_DISABLE_DEV_AUTODETECT=1
export CCTALLY_DISABLE_TELEMETRY=1
# Keep Task A/B's native quota fixtures active and deterministic. The source
# builders honor this established clock seam; browser-side age rendering may
# advance, but provider capability and cycle selection stay frozen.
export CCTALLY_AS_OF=2026-07-14T16:10:00Z

# 3) Generate the synthetic transcripts + manifest.json under the runtime dir.
python3 "$REPO_ROOT/bin/build-e2e-fixtures.py" --out "$RUNTIME"

# Add the canonical S7 Codex reader corpus under the isolated Codex root. The
# parent/child pair exercises qualified navigation; modern-full carries native
# prompts, responses, reasoning, tools, events, files, tokens, and cost.
mkdir -p "$CODEX_ROOT_MAIN/sessions/2026/07/20" "$CODEX_ROOT_A/sessions/2026/07/20" "$CODEX_ROOT_B/sessions/2026/07/20"
cp "$REPO_ROOT/tests/fixtures/codex-parity/v1/rollouts/modern-full.jsonl" \
   "$REPO_ROOT/tests/fixtures/codex-parity/v1/rollouts/nested-parent.jsonl" \
   "$REPO_ROOT/tests/fixtures/codex-parity/v1/rollouts/nested-child.jsonl" \
   "$CODEX_ROOT_MAIN/sessions/2026/07/20/"
cp "$RUNTIME/codex-task-a/root-a-collision.jsonl" \
   "$CODEX_ROOT_A/sessions/2026/07/20/"
cp "$RUNTIME/codex-task-a/root-b-collision.jsonl" \
   "$CODEX_ROOT_B/sessions/2026/07/20/"
cp "$RUNTIME/codex-task-a/rollout-2026-07-07T12-00-00-32900000-0000-4000-8000-000000000001.jsonl" \
   "$CODEX_ROOT_MAIN/sessions/2026/07/20/"

# Shared native UUID across Claude, Codex root A, and Codex root B. The UI must
# keep all three qualified identities distinct through open/persist/compare.
mkdir -p "$CLAUDE_CONFIG_DIR/projects/-synthetic-collision"
cp "$REPO_ROOT/tests/fixtures/codex-parity/v1/claude-seed/11111111-1111-4111-8111-111111111111.jsonl" \
   "$CLAUDE_CONFIG_DIR/projects/-synthetic-collision/"

# 4) Disable the dashboard's update-check thread. It consults CONFIG, not the
#    environment (docs/updates-gotchas.md: `_should_show_update_banner` reads
#    `config.update.check.enabled`), so a scratch env var can't turn it off — set
#    the config key in the scratch data dir instead. Keeps the suite offline and
#    off any update banner.
"$REPO_ROOT/bin/cctally" config set update.check.enabled false >/dev/null

# 5) Pre-prime both providers so the per-session rollups are authoritative
#    before the first request — no cold-sync in-flux reads, no "indexing" notes.
"$REPO_ROOT/bin/cctally" cache-sync --source all

# 6) Serve. Sync stays ENABLED (no --no-sync) so the open reader live-tails via
#    the targeted per-conversation ingest (scenario 3). exec so Playwright's
#    teardown signal reaches the server directly.
exec "$REPO_ROOT/bin/cctally" dashboard --port 8797 --host 127.0.0.1 --no-browser
