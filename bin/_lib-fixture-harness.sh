#!/usr/bin/env bash
# Shared library for fixture-based golden-file harnesses.
#
# Sourced by per-command harness scripts (`cctally-weekly-test`,
# `cctally-session-test`, etc.). Exposes one function: `run_mode`.
#
# Caller contract — set these before sourcing:
#   HARNESS_SUBCOMMAND   : name of the subcommand, e.g. "weekly"
#   HARNESS_FIXTURES_DIR : absolute path to tests/fixtures/<cmd>/
#   HARNESS_BIN          : absolute path to bin/cctally
# Plus globals at parent scope: `pass_count=0; fail_count=0`.
#
# Per-fixture input.env may set (all optional):
#   AS_OF             : ISO-8601 timestamp (Z or explicit tz offset) —
#                       passed to cctally via the
#                       CCTALLY_AS_OF env var. REQUIRED on every fixture.
#                       Missing AS_OF is treated as a misconfigured
#                       fixture and FAILS the mode (a typo in input.env
#                       used to silently green-skip otherwise).
#   FLAGS             : extra flags appended to every run_mode invocation
#                       for this fixture (e.g. "--breakdown", "--days 7")
#   COLUMNS_OVERRIDE  : override Python's TTY-width fallback (default 120)
#   FORCE_COLOR       : forward FORCE_COLOR and DROP the harness-injected
#                       NO_COLOR=1 so fixtures can test --no-color CLI flag

set -uo pipefail

# run_mode NAME LABEL FLAGS GOLDEN_SUFFIX
#   NAME          : fixture directory basename
#   LABEL         : human-readable mode label for PASS/FAIL lines
#   FLAGS         : mode-specific flags (e.g. "", "--json", "--by-session")
#   GOLDEN_SUFFIX : suffix used in golden-<suffix>.txt
run_mode () {
    local name="$1" label="$2" flags="$3" golden_suffix="$4"
    local dir="$HARNESS_FIXTURES_DIR/$name"
    local golden="$dir/golden-$golden_suffix.txt"

    # Locally declare input.env vars so missing keys can't leak across fixtures.
    # FAKE_HOME defaults to the in-tree fixture dir (legacy behavior — the
    # subcommand reads ${FAKE_HOME}/.local/share/cctally/*.db).
    # Per-harness wrappers can opt into write-isolation by exporting
    # HARNESS_FAKE_HOME_BASE before sourcing this lib: FAKE_HOME then becomes
    # ${HARNESS_FAKE_HOME_BASE}/${name}, so builder regen and any test-process
    # SQLite writes land in scratch space instead of dirtying the tree. The
    # in-tree fixture dir is still consulted for input.env and golden-*.txt.
    local FAKE_HOME
    if [ -n "${HARNESS_FAKE_HOME_BASE:-}" ]; then
        FAKE_HOME="${HARNESS_FAKE_HOME_BASE%/}/$name"
    else
        FAKE_HOME="${dir%/}"
    fi
    local AS_OF=""
    local FLAGS=""
    local COLUMNS_OVERRIDE=""
    local FORCE_COLOR=""

    if [ ! -f "$dir/input.env" ]; then
        echo "SKIP $name/$label: no input.env"
        return 0
    fi
    # shellcheck disable=SC1091
    . "$dir/input.env"

    # Seed display.tz=utc into the scratch HOME so goldens render
    # UTC suffixes regardless of host TZ. (TZ=Etc/UTC env still pins
    # the host-zone resolver path; this just makes the test posture
    # explicit.)
    if [ -n "${HARNESS_FAKE_HOME_BASE:-}" ]; then
        local cfg_dir="$FAKE_HOME/.local/share/cctally"
        mkdir -p "$cfg_dir"
        if ! grep -q '"display"' "$cfg_dir/config.json" 2>/dev/null; then
            FAKE_HOME="$FAKE_HOME" python3 - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.environ["FAKE_HOME"]) / ".local/share/cctally/config.json"
data = {}
if p.exists():
    try:
        data = json.loads(p.read_text())
    except Exception:
        data = {}
data.setdefault("collector", {"host": "127.0.0.1", "port": 17321,
                              "token": "harness", "week_start": "monday"})
data.setdefault("display", {})["tz"] = "utc"
p.write_text(json.dumps(data, indent=2) + "\n")
PY
        fi
    fi

    if [ -z "${AS_OF:-}" ]; then
        echo "FAIL $name/$label: input.env missing AS_OF"
        fail_count=$((fail_count + 1))
        return 1
    fi

    local merged_flags="$flags $FLAGS"
    local actual

    # shellcheck disable=SC2086
    if [ -n "${FORCE_COLOR:-}" ]; then
        if [ -n "${COLUMNS_OVERRIDE:-}" ]; then
            actual=$(HOME="$FAKE_HOME" TZ=Etc/UTC COLUMNS="$COLUMNS_OVERRIDE" \
                     FORCE_COLOR="$FORCE_COLOR" CCTALLY_AS_OF="$AS_OF" \
                     "$HARNESS_BIN" "$HARNESS_SUBCOMMAND" $merged_flags 2>&1)
        else
            actual=$(HOME="$FAKE_HOME" TZ=Etc/UTC FORCE_COLOR="$FORCE_COLOR" \
                     CCTALLY_AS_OF="$AS_OF" \
                     "$HARNESS_BIN" "$HARNESS_SUBCOMMAND" $merged_flags 2>&1)
        fi
    elif [ -n "${COLUMNS_OVERRIDE:-}" ]; then
        actual=$(HOME="$FAKE_HOME" NO_COLOR=1 TZ=Etc/UTC COLUMNS="$COLUMNS_OVERRIDE" \
                 CCTALLY_AS_OF="$AS_OF" \
                 "$HARNESS_BIN" "$HARNESS_SUBCOMMAND" $merged_flags 2>&1)
    else
        actual=$(HOME="$FAKE_HOME" NO_COLOR=1 TZ=Etc/UTC CCTALLY_AS_OF="$AS_OF" \
                 "$HARNESS_BIN" "$HARNESS_SUBCOMMAND" $merged_flags 2>&1)
    fi

    if [ ! -f "$golden" ]; then
        echo "MISSING GOLDEN $name/$label — actual output:"
        echo "$actual" | sed 's/^/    /'
        fail_count=$((fail_count + 1))
        return 1
    fi
    if ! diff -u <(echo "$actual") "$golden" >/dev/null; then
        echo "FAIL $name/$label"
        # Print up to 200 lines of diff so multi-row tables surface their full
        # diff on CI (each session row in `session` renders as 5 wrapped lines,
        # so 4-session tables can blow past a 40-line cap and hide the smoking gun).
        diff -u "$golden" <(echo "$actual") | head -200
        fail_count=$((fail_count + 1))
        return 1
    fi
    echo "PASS $name/$label"
    pass_count=$((pass_count + 1))
    return 0
}

