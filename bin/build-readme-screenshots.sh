#!/usr/bin/env bash
# Refresh the public README's screenshots end-to-end.
#
# Pipeline:
#   1. Verify dev tools (freeze, python3)
#   2. Build the marketing fixture (bin/build-readme-fixtures.py)
#   3. Stage the fixture under <scratch>/home/
#   4. Export CCTALLY_AS_OF + HOME for all subsequent invocations
#   5. Capture 5 CLI SVGs via freeze (charm.sh static-frame SVG tool)
#   6. Start `cctally dashboard` ONCE against the marketing fixture;
#      run bin/_capture_dashboard.py for ALL 4 dashboard shots (the
#      marketing fixture's tuned ~103% projection now produces the
#      WARN state inline — no separate warn-fixture restage needed).
#   7. Verify all 11 outputs landed in docs/img/
#
# Idempotent (overwrites docs/img/ in place). Not run in CI; refreshing
# the README assets is a maintainer task.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_IMG="$REPO_ROOT/docs/img"
# Execution contract (issue #354): consumed by `cctally-release readme-refresh`.
# Pin the product binary (default: THIS checkout's bin/cctally, never whatever
# `cctally` sits on PATH) and the output dir (default docs/img; overridable so
# readme-refresh can capture into a temp dir + copy back only manifest-listed
# files). README_SCREENSHOTS_CONTRACT=1 is the capability marker it greps for.
#
# Dev/test knobs (issue #367 — NOT part of the capability contract above):
#   README_SCREENSHOTS_SELFTEST=port       print `port=<n>` and exit 0
#   README_SCREENSHOTS_SELFTEST=dashboard  launch, print `url=<url>`, exit 0
#   README_SCREENSHOTS_WAIT_TICKS=<n>      banner-poll ticks, 0.1s each (default 150 => ~15s)
#   README_SCREENSHOTS_READY_TICKS=<n>     readiness-poll ticks, up to 2.1s each
#                                          when the probe times out (default 30 => ~60s)
README_SCREENSHOTS_CONTRACT=1
CCTALLY_BIN="${CCTALLY_BIN:-$REPO_ROOT/bin/cctally}"
export CCTALLY_BIN
OUT_DIR="${README_SCREENSHOTS_OUT_DIR:-$DOCS_IMG}"
MARKETING_FIXTURE="$REPO_ROOT/tests/fixtures/readme/home"

# Port decision (issue #367). An explicit DASHBOARD_PORT is a PIN and still
# refuses a busy port; with none set we hand the kernel --port 0 and read back
# what it actually bound, so a dashboard already running on the maintainer's
# 8789 is a non-event rather than a hard failure.
DASHBOARD_PORT_EXPLICIT="${DASHBOARD_PORT:-}"
DASHBOARD_PORT="${DASHBOARD_PORT_EXPLICIT:-0}"
DASHBOARD_URL=""                                    # resolved after the bind
# Banner poll: 0.1s per tick and nothing else, so 150 ticks is ~15s.
WAIT_TICKS="${README_SCREENSHOTS_WAIT_TICKS:-150}"
# Readiness poll: each FAILED tick also burns curl's own `--max-time 2`, so a
# tick here costs up to 2.1s, not 0.1s. Budgeted separately (30 => ~60s worst
# case) rather than reusing WAIT_TICKS, which at 150 would stall ~5 minutes
# against a dashboard that accepts TCP and never answers. Connection-refused
# ticks return instantly, so the common retry path is still ~0.1s each.
READY_TICKS="${README_SCREENSHOTS_READY_TICKS:-30}"

require() {
    local cmd="$1" install_hint="$2"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "build-readme-screenshots: missing '$cmd'." >&2
        echo "  install with: $install_hint" >&2
        exit 1
    fi
}

# Only the tools the port decision and the dashboard launch need. `freeze` is
# required further down, AFTER the selftest dispatch, so the port tests do not
# need a charm.sh install on the runner.
require python3 "(should be on PATH)"
require curl "(should be on PATH)"

# Any listener on 127.0.0.1:$1, HTTP or not — a TCP connect, not a GET, so a
# non-HTTP process squatting the port is caught too.
port_busy() {
    python3 -c 'import socket,sys
s = socket.socket()
s.settimeout(0.5)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$1"
}

# Reject a non-numeric pin here rather than letting `int()` throw a raw
# traceback out of port_busy — that traceback lands in the promote log looking
# like a crash of the release tool, and the guard would then wrongly conclude
# "not busy" and hand the bad value to argparse much later.
if [[ -n $DASHBOARD_PORT_EXPLICIT && ! $DASHBOARD_PORT_EXPLICIT =~ ^[0-9]+$ ]]; then
    echo "build-readme-screenshots: DASHBOARD_PORT must be a number, got '$DASHBOARD_PORT_EXPLICIT'" >&2
    exit 1
fi

if [[ -n $DASHBOARD_PORT_EXPLICIT ]] && port_busy "$DASHBOARD_PORT_EXPLICIT"; then
    echo "build-readme-screenshots: DASHBOARD_PORT=$DASHBOARD_PORT_EXPLICIT is already in use" >&2
    echo "  free that port, or unset DASHBOARD_PORT to auto-select a free one" >&2
    exit 1
fi

# Scratch dir + trap, hoisted ahead of the fixture build so the selftest hook
# can use them and so the trap covers more of the run. Nothing between the old
# and new positions touches scratch state.
SCRATCH="$(mktemp -d -t cctally-readme-XXXXXX)"
DASH_PID=""
cleanup() {
    if [[ -n "$DASH_PID" ]]; then
        kill -TERM "$DASH_PID" 2>/dev/null || true
        wait "$DASH_PID" 2>/dev/null || true
    fi
    rm -rf "$SCRATCH"
}
trap cleanup EXIT INT TERM

# Echo whatever the dashboard wrote, one `  dashboard| ` prefixed line each, so
# a failure never loses it. An EMPTY log is itself a diagnosis ("the process
# produced no output at all" vs "it printed a traceback"), so say so explicitly
# rather than emitting nothing — `sed` over an empty file prints no lines.
dump_dashboard_log() {
    if [[ -s "$SCRATCH/dashboard.log" ]]; then
        sed 's/^/  dashboard| /' "$SCRATCH/dashboard.log" >&2
    else
        echo "  dashboard| (no output captured)" >&2
    fi
}

# Launch the dashboard on $DASHBOARD_PORT (0 = kernel picks), then read the
# port it actually bound out of its own startup banner. Sets DASH_PID and
# DASHBOARD_URL; exits 1 with the captured log on any failure. (Issue #367.)
start_dashboard() {
    # Create the log BEFORE forking. The child applies its redirect after the
    # fork, so the first `sed` below can otherwise race it and read a file that
    # does not exist yet — under `set -e` that aborts the whole script with a
    # bare "No such file or directory" instead of retrying.
    : > "$SCRATCH/dashboard.log"
    "$CCTALLY_BIN" dashboard --host 127.0.0.1 --port "$DASHBOARD_PORT" --no-browser \
        > "$SCRATCH/dashboard.log" 2>&1 &
    DASH_PID=$!

    # `dashboard: serving http://localhost:<port>/ — Ctrl-C to stop`
    #
    # Parsed WITHOUT a pipe on purpose: `sed … | head -1` takes SIGPIPE when
    # head closes early, pipefail propagates 141, and the assignment then
    # trips `set -e` — a load-dependent kill that a short log usually hides.
    # `{ …p; q; }` stops at the first match inside sed itself.
    #
    # The address requires `https?://` so the all-interfaces header line
    # ("dashboard: serving on all interfaces:") cannot enter the block and `q`
    # out before the URL lines. With --host 127.0.0.1 that branch is
    # unreachable; the anchoring means a future host change fails CLOSED.
    local bound=""
    local _tick
    for _tick in $(seq 1 "$WAIT_TICKS"); do
        bound=$(sed -nE '/^dashboard: serving https?:\/\//{ s#^dashboard: serving https?://[^/]*:([0-9]+)/.*#\1#p; q; }' \
                    "$SCRATCH/dashboard.log")
        [[ -n $bound ]] && break
        kill -0 "$DASH_PID" 2>/dev/null || break     # child died — stop waiting
        sleep 0.1
    done
    if [[ -z $bound ]]; then
        echo "build-readme-screenshots: dashboard did not report a bound port" >&2
        dump_dashboard_log
        exit 1
    fi
    DASHBOARD_URL="http://127.0.0.1:$bound/"

    # The banner prints once the socket is bound but BEFORE serve_forever()
    # runs (bin/_cctally_dashboard.py:7191 vs :7196), so listening is not
    # serving. Bounded probes: a bare `curl -fsS` has no timeout and a process
    # that accepts TCP without answering would hang past the loop limit.
    local ready=0
    for _tick in $(seq 1 "$READY_TICKS"); do
        if curl -fsS --connect-timeout 1 --max-time 2 "$DASHBOARD_URL" >/dev/null 2>&1; then
            ready=1
            break
        fi
        kill -0 "$DASH_PID" 2>/dev/null || break
        sleep 0.1
    done
    if [[ $ready -ne 1 ]]; then
        echo "build-readme-screenshots: dashboard at $DASHBOARD_URL never became ready" >&2
        dump_dashboard_log
        exit 1
    fi
}

# --- selftest dispatch (issue #367) ---
case "${README_SCREENSHOTS_SELFTEST:-}" in
    port)
        echo "port=$DASHBOARD_PORT"
        exit 0
        ;;
    dashboard)
        start_dashboard
        echo "url=$DASHBOARD_URL"
        exit 0
        ;;
    "")
        ;;
    *)
        echo "build-readme-screenshots: unknown README_SCREENSHOTS_SELFTEST='${README_SCREENSHOTS_SELFTEST}'" >&2
        exit 2
        ;;
esac

# 1. Verify dev tools (playwright is verified at import time inside
#    bin/_capture_dashboard.py, with its own clean error message).
require freeze "brew install charmbracelet/tap/freeze"

# 2. Build marketing fixture (today UTC anchored). The fixture builder
# normalizes `as_of` to THURSDAY 14:00 UTC of the containing week so the
# forecast projection lands at ~103% (clearly WARN, fits the modal).
AS_OF="$(date -u +'%Y-%m-%d')"
echo "[1/5] Building marketing fixture (--as-of $AS_OF)"
"$REPO_ROOT/bin/build-readme-fixtures.py" --as-of "$AS_OF" >/dev/null

# 3. Stage under the scratch dir created above (trap-based cleanup is already
# armed at that point).
mkdir -p "$SCRATCH/home"
cp -R "$MARKETING_FIXTURE/." "$SCRATCH/home/"
echo "[2/5] Staged marketing fixture at $SCRATCH/home"

# Capture original HOME so Playwright can still find its chromium cache
# after we redirect HOME for cctally + dashboard. Honors a user-set
# PLAYWRIGHT_BROWSERS_PATH (e.g. custom install location) via ${VAR:-default}.
ORIGINAL_HOME="$HOME"
if [[ "$(uname -s)" == "Darwin" ]]; then
    export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$ORIGINAL_HOME/Library/Caches/ms-playwright}"
else
    export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$ORIGINAL_HOME/.cache/ms-playwright}"
fi

# Resolve a Python site-packages tree that has BOTH `rich` (for `cctally
# tui`) and `playwright` (for the dashboard captures). HOME redirect
# below moves Python's user-site lookup to the empty scratch dir, so
# anything pip-installed into ~/Library/Python/.../site-packages becomes
# invisible to subprocesses unless we pin its path via PYTHONPATH.
#
# Resolution order:
#   1. SCREENSHOTS_PYTHONPATH env (explicit override; e.g. CI venv)
#   2. /tmp/cctally-screenshots-venv/lib/python3.14/site-packages (the
#      maintainer-local venv that this pipeline assumes — created by
#      `python3 -m venv /tmp/cctally-screenshots-venv && pip install
#      rich playwright && playwright install chromium`)
#   3. The current user-site (last resort; works only if both packages
#      are installed there)
SCREENSHOTS_VENV_SITE="/tmp/cctally-screenshots-venv/lib/python3.14/site-packages"
if [[ -n "${SCREENSHOTS_PYTHONPATH:-}" ]]; then
    ORIGINAL_USER_SITE="$SCREENSHOTS_PYTHONPATH"
elif [[ -d "$SCREENSHOTS_VENV_SITE" ]]; then
    ORIGINAL_USER_SITE="$SCREENSHOTS_VENV_SITE"
else
    ORIGINAL_USER_SITE="$(python3 -c 'import site; print(site.getusersitepackages())' 2>/dev/null || echo "")"
fi

# 4. Pin CCTALLY_AS_OF + HOME for every subsequent invocation. The
# fixture builder shifts to Thursday 14:00 UTC of the AS_OF-containing
# week; do the same here so cctally subcommands resolve "now" to the
# same instant the fixture's snapshots were captured at. Use Python to
# compute the Thursday — date(1) on macOS lacks GNU's `-d` arithmetic.
AS_OF_INSTANT="$(python3 - <<PY
import datetime as dt
d = dt.datetime.strptime("$AS_OF", "%Y-%m-%d")
d = d + dt.timedelta(days=(3 - d.weekday()))  # shift to Thursday
print(d.strftime("%Y-%m-%dT14:00:00Z"))
PY
)"
export CCTALLY_AS_OF="$AS_OF_INSTANT"
export HOME="$SCRATCH/home"
# A linked worktree is a git checkout, so `_is_dev_checkout()` is true and would
# resolve ~/.local/share/cctally-dev instead of the staged fixture HOME. Pin the
# data dir explicitly to neutralize the dev-checkout autodetect.
export CCTALLY_DATA_DIR="$SCRATCH/home/.local/share/cctally"

# Version canary: prove no installed `cctally` leaked in and the captured binary
# is exactly the version we intend (the dashboard header chip renders
# `cctally vX.Y.Z` into every PNG). readme-refresh sets README_EXPECTED_VERSION.
if [[ -n "${README_EXPECTED_VERSION:-}" ]]; then
    got="$("$CCTALLY_BIN" --version)"
    if [[ "$got" != "cctally ${README_EXPECTED_VERSION}"* ]]; then
        echo "build-readme-screenshots: version canary failed: got '$got', want cctally ${README_EXPECTED_VERSION}" >&2
        exit 1
    fi
fi

# 5. CLI SVGs via freeze (charm.sh) — static-frame SVGs, no animation.
# freeze produces clean static SVGs for terminal output (no animation;
# works in all SVG viewers + GitHub).
mkdir -p "$OUT_DIR"
echo "[3/5] Capturing CLI SVGs"

FREEZE_OPTS=(--window --background "#0d1117" --padding "20,40" --margin 0)

# NOTE on clean captures: freeze bakes the command's terminal output
# (stdout+stderr at the PTY) into the SVG and does NOT honor an in-command
# `2>/dev/null` redirect, so the marketing fixture is kept silent at the
# SOURCE instead: session_files are seeded with size_bytes=0 (no "[cache]
# … no longer on disk" orphan warning) and all stats migrations are
# stamped applied (no one-time "[cctally] Recomputing …" recompute, which
# would also zero the pre-walk trend weeks). Both live in
# bin/build-readme-fixtures.py. A cold `cctally report/forecast` against
# the fixture now emits zero stderr, so no pre-warm is needed.
freeze "${FREEZE_OPTS[@]}" --execute "$CCTALLY_BIN report"           --output "$OUT_DIR/cli-report.svg"
freeze "${FREEZE_OPTS[@]}" --execute "$CCTALLY_BIN forecast"         --output "$OUT_DIR/cli-forecast.svg"
freeze "${FREEZE_OPTS[@]}" --execute "$CCTALLY_BIN five-hour-blocks --breakdown=model" --output "$OUT_DIR/cli-five-hour-blocks.svg"
freeze "${FREEZE_OPTS[@]}" --execute "$CCTALLY_BIN codex daily"      --output "$OUT_DIR/cli-codex-daily.svg"
# TUI uses the hidden --render-once / --snapshot-module / --force-size
# dev path. These flags are argparse.SUPPRESS'd in cctally tui --help
# but verified to exist (see bin/cctally argparse setup ~L19975).
# FORCE_COLOR=1 opts the render-once code path into emitting ANSI
# escapes (default is plain text for byte-stable goldens). freeze then
# captures the ANSI and renders a colored SVG. Goldens never set
# FORCE_COLOR, so existing fixture tests are unaffected.
FORCE_COLOR=1 PYTHONPATH="$ORIGINAL_USER_SITE${PYTHONPATH:+:$PYTHONPATH}" freeze "${FREEZE_OPTS[@]}" \
    --execute "$CCTALLY_BIN tui --render-once --snapshot-module $REPO_ROOT/tests/fixtures/readme/tui_snapshot.py --force-size 120x40" \
    --output "$OUT_DIR/cli-tui.svg"

# 6. Dashboard shots — start dashboard ONCE against the marketing
# fixture and capture all 4 shots in a single pass. The marketing
# fixture's tuned ~103% projection produces the WARN state directly,
# so the prior pipeline's restage-to-dashboard/warn step is no longer
# needed.
echo "[4/5] Starting dashboard against marketing fixture"
# NOTE: flag is `--no-browser`, NOT `--no-open` (which would error). See
# `cctally dashboard --help`.
start_dashboard

# Capture all 4 shots — desktop, modal, mobile, AND warn — against the
# single marketing-fixture dashboard. PYTHONPATH=ORIGINAL_USER_SITE for
# the same reason as the TUI invocation above (playwright is typically
# pip-installed into the user-site, which HOME redirect hides).
PYTHONPATH="$ORIGINAL_USER_SITE${PYTHONPATH:+:$PYTHONPATH}" \
    "$REPO_ROOT/bin/_capture_dashboard.py" --url "$DASHBOARD_URL" --out-dir "$OUT_DIR"

kill -TERM "$DASH_PID" 2>/dev/null || true
wait "$DASH_PID" 2>/dev/null || true
DASH_PID=""

# 7. Verify all 11 outputs exist and are non-empty.
echo "[5/5] Verifying outputs"
EXPECTED=(
    "$OUT_DIR/dashboard-desktop.png"
    "$OUT_DIR/dashboard-modal.png"
    "$OUT_DIR/dashboard-mobile.png"
    "$OUT_DIR/dashboard-warn.png"
    "$OUT_DIR/conversation-reader.png"
    "$OUT_DIR/conversation-mobile.png"
    "$OUT_DIR/cli-report.svg"
    "$OUT_DIR/cli-forecast.svg"
    "$OUT_DIR/cli-five-hour-blocks.svg"
    "$OUT_DIR/cli-codex-daily.svg"
    "$OUT_DIR/cli-tui.svg"
)
MISSING=0
for f in "${EXPECTED[@]}"; do
    if [[ ! -s "$f" ]]; then
        echo "  MISSING: $f" >&2
        MISSING=$((MISSING + 1))
    else
        printf '  ok    : %s (%s bytes)\n' "$f" "$(wc -c < "$f" | tr -d ' ')"
    fi
done

if [[ $MISSING -gt 0 ]]; then
    echo "build-readme-screenshots: $MISSING expected file(s) missing" >&2
    exit 1
fi

# Machine-readable manifest (issue #354): readme-refresh copies back ONLY the
# files listed here into the main checkout's docs/img/.
python3 - "$OUT_DIR" "${EXPECTED[@]}" <<'PYMANIFEST'
import json, os, sys
out = sys.argv[1]
files = sorted(os.path.basename(p) for p in sys.argv[2:])
with open(os.path.join(out, "manifest.json"), "w") as fh:
    json.dump({"files": files}, fh, indent=2, sort_keys=True)
    fh.write("\n")
PYMANIFEST

echo
echo "All ${#EXPECTED[@]} README assets refreshed in $OUT_DIR"
