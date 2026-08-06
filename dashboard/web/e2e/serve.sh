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
   "$REPO_ROOT/tests/fixtures/codex-parity/v1/rollouts/session-d-reasoning-lifecycle-markers.jsonl" \
   "$REPO_ROOT/tests/fixtures/codex-parity/v1/rollouts/session-e-native-families.jsonl" \
   "$CODEX_ROOT_MAIN/sessions/2026/07/20/"
cp "$RUNTIME/codex-task-a/root-a-collision.jsonl" \
   "$CODEX_ROOT_A/sessions/2026/07/20/"
cp "$RUNTIME/codex-task-a/root-b-collision.jsonl" \
   "$CODEX_ROOT_B/sessions/2026/07/20/"
cp "$RUNTIME/codex-task-a/rollout-2026-07-07T12-00-00-32900000-0000-4000-8000-000000000001.jsonl" \
   "$CODEX_ROOT_MAIN/sessions/2026/07/20/"
cp "$RUNTIME/codex-task-b/session-b-card-wire.jsonl" \
   "$CODEX_ROOT_MAIN/sessions/2026/07/20/"
cp "$RUNTIME/codex-find/occurrence-find.jsonl" \
   "$CODEX_ROOT_MAIN/sessions/2026/07/20/"
cp "$RUNTIME/codex-rail-page/"*.jsonl \
   "$CODEX_ROOT_MAIN/sessions/2026/07/20/"

# #463 S5 — append an injected-context bundle to the RUNTIME COPY of the Session
# D rollout. Before this the whole e2e corpus produced only `notification` meta
# rows, so no browser test could observe the `context` kind, which is the kind
# that renders both a `.conv-meta-label` ("SESSION CONTEXT") and a
# `.conv-meta-name` ("· agents, environment") — the two elements #493 reports
# shattering on a phone. The committed fixture is left untouched, because the
# frontend harness byte-compares wire fixtures regenerated from it; only the
# scratch copy is extended, and the message is appended at the END so the rail
# title and every existing find/count assertion stay as they were.
SESSION_D_ROLLOUT="$CODEX_ROOT_MAIN/sessions/2026/07/20/session-d-reasoning-lifecycle-markers.jsonl"
# Open-for-append would CREATE a missing file, so a renamed or unstaged fixture
# would silently produce an empty rollout under `set -e` instead of failing.
[ -f "$SESSION_D_ROLLOUT" ] || {
  echo "e2e/serve.sh: expected Session D rollout is missing: $SESSION_D_ROLLOUT" >&2
  exit 1
}
python3 - "$SESSION_D_ROLLOUT" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone

bundle = (
    "# AGENTS.md instructions for /synthetic/root-a/project-red\n"
    "<INSTRUCTIONS>Synthetic agent instructions for the injected context bundle."
    "</INSTRUCTIONS>\n"
    "<environment_context>Synthetic environment context.</environment_context>"
)

# Derive the timestamp from the file's own maximum rather than hardcoding one.
# A hardcoded value only worked because it happened to land in the gap between
# two neighbouring fixture records; re-timing either neighbour would have
# silently reordered the rail with nothing failing.
path = sys.argv[1]
latest = None
with open(path, encoding="utf-8") as handle:
    for line in handle:
        line = line.strip()
        if not line:
            continue
        stamp = json.loads(line).get("timestamp")
        if not isinstance(stamp, str):
            continue
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if latest is None or parsed > latest:
            latest = parsed
if latest is None:
    raise SystemExit(f"e2e/serve.sh: no timestamped record in {path}")
appended_at = (latest + timedelta(seconds=1)).astimezone(timezone.utc)

record = {
    "payload": {
        "content": [{"text": bundle, "type": "input_text"}],
        "phase": "input",
        "role": "user",
        "type": "message",
    },
    "timestamp": appended_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "type": "response_item",
}
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, sort_keys=True) + "\n")
PY

# Shared native UUID across Claude, Codex root A, and Codex root B. The UI must
# keep all three qualified identities distinct through open/persist/compare.
mkdir -p "$CLAUDE_CONFIG_DIR/projects/-synthetic-collision"
cp "$REPO_ROOT/tests/fixtures/codex-parity/v1/claude-seed/11111111-1111-4111-8111-111111111111.jsonl" \
   "$CLAUDE_CONFIG_DIR/projects/-synthetic-collision/"
mkdir -p "$CLAUDE_CONFIG_DIR/projects/-synthetic-claude-reference"
cp "$RUNTIME/codex-task-b/claude-card-reference.jsonl" \
   "$CLAUDE_CONFIG_DIR/projects/-synthetic-claude-reference/"
mkdir -p "$CLAUDE_CONFIG_DIR/projects/-synthetic-session-d-reference"
cp "$REPO_ROOT/tests/fixtures/codex-parity/v1/claude-seed/334-claude-thinking-reference.jsonl" \
   "$CLAUDE_CONFIG_DIR/projects/-synthetic-session-d-reference/"

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
