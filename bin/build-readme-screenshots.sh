#!/usr/bin/env bash
# Refresh the public README's screenshots end-to-end.
#
# Pipeline:
#   1. Verify dev tools (freeze, python3)
#   2. Build the marketing fixture (bin/build-readme-fixtures.py)
#   3. Stage the fixture under <scratch>/home/
#   4. Export CCTALLY_AS_OF + HOME for all subsequent invocations
#   5. Capture 4 CLI SVGs via freeze (charm.sh static-frame SVG tool)
#   6. Start `cctally dashboard` ONCE against the marketing fixture;
#      run bin/_capture_dashboard.py for ALL 4 dashboard shots (the
#      marketing fixture's tuned ~103% projection now produces the
#      WARN state inline — no separate warn-fixture restage needed).
#   7. Verify all 8 outputs landed in docs/img/
#
# Idempotent (overwrites docs/img/ in place). Not run in CI; refreshing
# the README assets is a maintainer task.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_IMG="$REPO_ROOT/docs/img"
MARKETING_FIXTURE="$REPO_ROOT/tests/fixtures/readme/home"
DASHBOARD_PORT="${DASHBOARD_PORT:-8789}"
DASHBOARD_URL="http://127.0.0.1:$DASHBOARD_PORT/"

require() {
    local cmd="$1" install_hint="$2"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "build-readme-screenshots: missing '$cmd'." >&2
        echo "  install with: $install_hint" >&2
        exit 1
    fi
}

# 1. Verify dev tools (playwright is verified at import time inside
#    bin/_capture_dashboard.py, with its own clean error message).
require freeze "brew install charmbracelet/tap/freeze"
require python3 "(should be on PATH)"

# Defensive: refuse to start if something else is already serving the
# dashboard URL (a parallel `cctally dashboard` collides on bind).
if curl -fsS "$DASHBOARD_URL" >/dev/null 2>&1; then
    echo "build-readme-screenshots: something is already serving $DASHBOARD_URL" >&2
    echo "  stop your existing dashboard, OR re-run with DASHBOARD_PORT=18789" >&2
    exit 1
fi

# 2. Build marketing fixture (today UTC anchored). The fixture builder
# normalizes `as_of` to THURSDAY 14:00 UTC of the containing week so the
# forecast projection lands at ~103% (clearly WARN, fits the modal).
AS_OF="$(date -u +'%Y-%m-%d')"
echo "[1/5] Building marketing fixture (--as-of $AS_OF)"
"$REPO_ROOT/bin/build-readme-fixtures.py" --as-of "$AS_OF" >/dev/null

# 3. Stage under scratch dir; trap-based cleanup.
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

# 5. CLI SVGs via freeze (charm.sh) — static-frame SVGs, no animation.
# freeze produces clean static SVGs for terminal output (no animation;
# works in all SVG viewers + GitHub).
mkdir -p "$DOCS_IMG"
echo "[3/5] Capturing CLI SVGs"

FREEZE_OPTS=(--window --background "#0d1117" --padding "20,40" --margin 0)

freeze "${FREEZE_OPTS[@]}" --execute "cctally report"           --output "$DOCS_IMG/cli-report.svg"
freeze "${FREEZE_OPTS[@]}" --execute "cctally forecast"         --output "$DOCS_IMG/cli-forecast.svg"
freeze "${FREEZE_OPTS[@]}" --execute "cctally five-hour-blocks --breakdown=model" --output "$DOCS_IMG/cli-five-hour-blocks.svg"
# TUI uses the hidden --render-once / --snapshot-module / --force-size
# dev path. These flags are argparse.SUPPRESS'd in cctally tui --help
# but verified to exist (see bin/cctally argparse setup ~L19975).
# FORCE_COLOR=1 opts the render-once code path into emitting ANSI
# escapes (default is plain text for byte-stable goldens). freeze then
# captures the ANSI and renders a colored SVG. Goldens never set
# FORCE_COLOR, so existing fixture tests are unaffected.
FORCE_COLOR=1 PYTHONPATH="$ORIGINAL_USER_SITE${PYTHONPATH:+:$PYTHONPATH}" freeze "${FREEZE_OPTS[@]}" \
    --execute "cctally tui --render-once --snapshot-module $REPO_ROOT/tests/fixtures/readme/tui_snapshot.py --force-size 120x40" \
    --output "$DOCS_IMG/cli-tui.svg"

# 6. Dashboard shots — start dashboard ONCE against the marketing
# fixture and capture all 4 shots in a single pass. The marketing
# fixture's tuned ~103% projection produces the WARN state directly,
# so the prior pipeline's restage-to-dashboard/warn step is no longer
# needed.
echo "[4/5] Starting dashboard against marketing fixture"
# NOTE: flag is `--no-browser`, NOT `--no-open` (which would error). See
# `cctally dashboard --help`.
cctally dashboard --host 127.0.0.1 --port "$DASHBOARD_PORT" --no-browser &
DASH_PID=$!

# Wait for dashboard to come up (poll up to 15s)
ready=0
for _ in $(seq 1 15); do
    if curl -fsS "$DASHBOARD_URL" >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
done
if [[ $ready -ne 1 ]]; then
    echo "build-readme-screenshots: marketing-fixture dashboard did not come up at $DASHBOARD_URL after 15s" >&2
    exit 1
fi

# Capture all 4 shots — desktop, modal, mobile, AND warn — against the
# single marketing-fixture dashboard. PYTHONPATH=ORIGINAL_USER_SITE for
# the same reason as the TUI invocation above (playwright is typically
# pip-installed into the user-site, which HOME redirect hides).
PYTHONPATH="$ORIGINAL_USER_SITE${PYTHONPATH:+:$PYTHONPATH}" \
    "$REPO_ROOT/bin/_capture_dashboard.py" --url "$DASHBOARD_URL" --out-dir "$DOCS_IMG"

kill -TERM "$DASH_PID" 2>/dev/null || true
wait "$DASH_PID" 2>/dev/null || true
DASH_PID=""

# 7. Verify all 8 outputs exist and are non-empty.
echo "[5/5] Verifying outputs"
EXPECTED=(
    "$DOCS_IMG/dashboard-desktop.png"
    "$DOCS_IMG/dashboard-modal.png"
    "$DOCS_IMG/dashboard-mobile.png"
    "$DOCS_IMG/dashboard-warn.png"
    "$DOCS_IMG/cli-report.svg"
    "$DOCS_IMG/cli-forecast.svg"
    "$DOCS_IMG/cli-five-hour-blocks.svg"
    "$DOCS_IMG/cli-tui.svg"
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

echo
echo "All 8 README assets refreshed in $DOCS_IMG"
