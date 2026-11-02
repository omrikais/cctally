#!/usr/bin/env python3
"""Build deterministic fake-HOME fixtures for cctally-doctor-test.

Each scenario directory exposes a `setup.sh` that, given an empty
HARNESS_FAKE_HOME, mutates it into the state under test. The harness
then runs `cctally doctor` against that HOME and diffs vs the per-
scenario expected.* goldens.

Run: `python3 bin/build-doctor-fixtures.py`
to (re)generate the setup.sh files and per-scenario .gitignore.
Goldens are generated separately by running the harness with
CCTALLY_DOCTOR_REGENERATE=1.
"""
from __future__ import annotations

import ast
import pathlib
import re
import textwrap

ROOT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "doctor"
ENTRY_POINT = pathlib.Path(__file__).resolve().parent / "cctally"


def _user_facing_wrappers() -> str:
    """The wrapper names `cctally setup` symlinks, read from the constant.

    Three scenarios seed a fake `~/.local/bin` (or brew prefix) with "every
    user-facing binary", so `install.symlinks` reports OK. Hand-copying that
    list left it one name short the moment a wrapper was added, and the
    doctor goldens then recorded a WARN as the healthy baseline. Deriving it
    from `SETUP_SYMLINK_NAMES` is what keeps the fixture producer in step
    with the rule it is meant to exercise.
    """
    source = ENTRY_POINT.read_text()
    match = re.search(r"^SETUP_SYMLINK_NAMES = \((.*?)^\)", source,
                      re.S | re.M)
    if match is None:
        raise SystemExit("SETUP_SYMLINK_NAMES not found in bin/cctally")
    names = ast.literal_eval(f"({match.group(1)})")
    if len(names) < 10:
        raise SystemExit(f"SETUP_SYMLINK_NAMES parsed as {names!r}")
    return " ".join(names)


USER_FACING_WRAPPERS = _user_facing_wrappers()

SCENARIOS = {
    # Test regime reset, session 5 (Task 5.8): the doctor goldens keep the
    # representative cases only. A scenario whose rows another scenario
    # already renders byte for byte, or whose gather leg and check branch a
    # pytest test already drives, was deleted; section 12 of
    # docs/superpowers/plans/2026-10-20-test-regime-reset-protection-map.md
    # names the retained detector of each.
    "01-all-ok":                 "all_ok",
    "07-corrupt-update-state":   "corrupt_update_state",
    "09-codex-cache-present":    "codex_cache_present",
    # The mixed report: hooks missing, OAuth missing, a failed migration and
    # a snapshot stale past an hour, each row as its single-failure scenario
    # rendered it before those were cut.
    "11-multi-fail-mixed":       "multi_fail_mixed",
    "12-corrupt-config-json":    "corrupt_config_json",
    # Issue #119: brew ~/.local/bin policy. The only end-to-end detector of
    # the gather's `symlinks_path_pinned` predicate; see
    # bin/cctally-doctor-test::compute_fake_path for the PATH knobs.
    "17-brew-pinned-only-path":  "brew_pinned_only_path",
    # Version-ahead check: cache.db ahead auto-heals. A stats epoch mismatch
    # rebuilds only when journal bytes exist; the no-journal case reports the
    # restoration requirement instead of promising an impossible auto-heal.
    "19-cache-version-ahead":    "cache_version_ahead",
    "20-stats-version-ahead":    "stats_version_ahead",
    "21-codex-quota-fresh-stale": "codex_quota_fresh_stale",
    # #411: a rebuildable stats mismatch (journal bytes present).
    "28-stats-mismatch-with-journal": "stats_mismatch_with_journal",
    # #496 S6 §7.6: the scenarios reaching `db.retained_artifacts`'s WARN and
    # protected-evidence FAIL, and `journal.auto_heal`'s repeated-shape FAIL,
    # through the real retention gather. The malformed-policy FAIL is reached
    # end to end by 12-corrupt-config-json.
    "31-retained-artifacts-over-budget": "retained_artifacts_over_budget",
    "32-retained-artifacts-protected":   "retained_artifacts_protected",
    "33-heal-repeated-shape":            "heal_repeated_shape",
    # #583 S4 (F39): the only fixture that reaches the
    # `db.conversations_wal_size` WARN branch through the gather's stat of the
    # sidecar. Every other scenario leaves the sidecar absent (OK).
    "35-conversations-wal-oversized":    "conversations_wal_oversized",
    # #719: a healthy sibling root must never mask a disabled Codex root.
    "36-codex-hooks-disabled-mixed-roots":     "codex_hooks_disabled_mixed_roots",
    # #825/#832 (spec section 8, F9, F16): every other scenario carries no
    # current account-bound index, so `accounting.outcome_quarantine` reads
    # "unavailable" and `journal.legacy_writer` "unavailable" everywhere. This
    # one builds a current, validated store through the real bound recording
    # kernel with no legacy write, so both legs are OK.
    "40-account-bound-clear":       "account_bound_clear",
}

# user_version sentinel bumped well past either registry head so the
# version-ahead check fires regardless of how many migrations ship later
# (mirrors the migrations scenario-07 fixture's PRAGMA user_version = 99).
_VERSION_AHEAD_SENTINEL = 99


CODEX_HOMES = {
    "21-codex-quota-fresh-stale": ("codex-a", "codex-b"),
    "36-codex-hooks-disabled-mixed-roots": ("codex-a", "codex-b"),
}


def _common_setup_preamble() -> str:
    return textwrap.dedent("""\
        #!/usr/bin/env bash
        # Generated by bin/build-doctor-fixtures.py — do not edit by hand.
        set -euo pipefail
        : "${HARNESS_FAKE_HOME:?HARNESS_FAKE_HOME must be set}"
        : "${REPO_ROOT:?REPO_ROOT must be set}"
        mkdir -p "$HARNESS_FAKE_HOME/.local/share/cctally"
        touch "$HARNESS_FAKE_HOME/.local/share/cctally/cache.db.maintenance.lock"
        mkdir -p "$HARNESS_FAKE_HOME/.claude"
        mkdir -p "$HARNESS_FAKE_HOME/.local/bin"
        # The harness sets CCTALLY_AS_OF before invoking doctor, so all
        # "age" computations in the report are deterministic.

        # Helper: write a minimal .credentials.json that satisfies the
        # OAuth presence check (_resolve_oauth_token: file source with
        # {"claudeAiOauth":{"accessToken":"..."}}). Token value is
        # synthetic — doctor never makes a real OAuth call.
        write_oauth_credentials() {
          mkdir -p "$HARNESS_FAKE_HOME/.claude"
          cat > "$HARNESS_FAKE_HOME/.claude/.credentials.json" <<'JSON'
        {"claudeAiOauth":{"accessToken":"fixture-token-not-real"}}
        JSON
        }

        # Shim `security` so keychain lookups can't leak host OAuth state
        # into doctor output. Without this, on a macOS dev box where the
        # user is logged into Claude Code, every scenario would falsely
        # show OAuth=present regardless of fixture intent. The shim is
        # in PATH ahead of /usr/bin so it always wins.
        cat > "$HARNESS_FAKE_HOME/.local/bin/security" <<'SH'
        #!/usr/bin/env bash
        # No-op shim — keychain unavailable in doctor fixtures.
        exit 1
        SH
        chmod +x "$HARNESS_FAKE_HOME/.local/bin/security"

        # Helper: write CC settings.json with canonical PostToolBatch/Stop/SubagentStop
        # entries that _is_cctally_hook_command recognizes (matcher + nested
        # hooks[].type=command + command). This is the shape `cctally setup`
        # writes; the hook counter ignores anything else.
        write_canonical_settings() {
          local path="$1"
          cat > "$path" <<'JSON'
        {
          "hooks": {
            "PostToolBatch": [
              {"matcher": "*", "hooks": [{"type": "command", "command": "cctally hook-tick"}]}
            ],
            "Stop": [
              {"matcher": "", "hooks": [{"type": "command", "command": "cctally hook-tick"}]}
            ],
            "SubagentStop": [
              {"matcher": "", "hooks": [{"type": "command", "command": "cctally hook-tick"}]}
            ]
          }
        }
        JSON
        }

        write_codex_hooks() {
          local root="$1"
          mkdir -p "$root/sessions"
          cat > "$root/hooks.json" <<JSON
        {"hooks":{"Stop":[{"hooks":[{"type":"command","command":"$REPO_ROOT/bin/cctally hook-tick --foreground --source codex","timeout":30}]}],"SubagentStop":[{"hooks":[{"type":"command","command":"$REPO_ROOT/bin/cctally hook-tick --foreground --source codex","timeout":30}]}]}}
        JSON
        }
    """)


def _scenario_body(slug: str) -> str:
    """Return the per-scenario shell body that mutates HARNESS_FAKE_HOME."""
    if slug == "all_ok":
        return textwrap.dedent("""\
            # Seed install + hooks + OAuth + valid update-state + 1 fresh snapshot.
            # Symlink every user-facing binary so install.symlinks is OK.
            # The list is derived from SETUP_SYMLINK_NAMES rather than copied:
            # a hand-copied list left the fixture one name short the moment a
            # wrapper was added, and the goldens then recorded a WARN as the
            # healthy baseline.
            for name in {USER_FACING_WRAPPERS}; do
                ln -sf "$REPO_ROOT/bin/$name" "$HARNESS_FAKE_HOME/.local/bin/$name"
            done
            # CC settings.json — canonical hook shape that _is_cctally_hook_command
            # recognizes (matcher + hooks[].type=command + command).
            write_canonical_settings "$HARNESS_FAKE_HOME/.claude/settings.json"
            write_oauth_credentials
            # Fresh weekly snapshot 2s before CCTALLY_AS_OF
            python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" --emit-snapshot all_ok \\
                "$HARNESS_FAKE_HOME/.local/share/cctally/stats.db"
            """)
    if slug == "account_bound_clear":
        return textwrap.dedent("""\
            for name in {USER_FACING_WRAPPERS}; do
                ln -sf "$REPO_ROOT/bin/$name" "$HARNESS_FAKE_HOME/.local/bin/$name"
            done
            write_canonical_settings "$HARNESS_FAKE_HOME/.claude/settings.json"
            write_oauth_credentials
            # A current account-bound store, built by the real bound recording
            # kernel under pinned clocks (see `_emit_account_bound`).
            python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" --emit-account-bound clear \\
                "$HARNESS_FAKE_HOME"
            """)
    if slug == "corrupt_update_state":
        return textwrap.dedent("""\
            write_canonical_settings "$HARNESS_FAKE_HOME/.claude/settings.json"
            write_oauth_credentials
            echo '{not valid json' > "$HARNESS_FAKE_HOME/.local/share/cctally/update-state.json"
            python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" --emit-snapshot all_ok \\
                "$HARNESS_FAKE_HOME/.local/share/cctally/stats.db"
            """)
    if slug == "codex_cache_present":
        return textwrap.dedent("""\
            mkdir -p "$HARNESS_FAKE_HOME/.codex/sessions/a"
            write_canonical_settings "$HARNESS_FAKE_HOME/.claude/settings.json"
            write_oauth_credentials
            touch "$HARNESS_FAKE_HOME/.codex/sessions/a/sess-1.jsonl"
            python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" --emit-snapshot all_ok \\
                "$HARNESS_FAKE_HOME/.local/share/cctally/stats.db"
            python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" --emit-codex-cache \\
                "$HARNESS_FAKE_HOME/.local/share/cctally/cache.db"
        """)
    if slug in {"codex_quota_fresh_stale", "codex_hooks_disabled_mixed_roots"}:
        case = slug.removeprefix("codex_").replace("_", "-")
        return _scenario_body("all_ok") + textwrap.dedent(f"""\
            write_codex_hooks "$HARNESS_FAKE_HOME/codex-a"
            write_codex_hooks "$HARNESS_FAKE_HOME/codex-b"
            python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" \\
                --emit-codex-doctor-case {case} \\
                "$HARNESS_FAKE_HOME/.local/share/cctally" \\
                "$HARNESS_FAKE_HOME/codex-a" "$HARNESS_FAKE_HOME/codex-b"
        """)
    if slug in (
        "retained_artifacts_over_budget", "retained_artifacts_protected",
        "heal_repeated_shape",
    ):
        return _scenario_body("all_ok") + _retention_scenario_body(slug)
    if slug == "conversations_wal_oversized":
        # #583 S4 (F39). The check reads ONLY the size of the
        # `conversations.db-wal` sidecar, so the sidecar alone is what this
        # scenario creates: adding a conversations.db as well would move other
        # conversations checks and make the golden diff wider than the one row
        # under test. The file is created SPARSE — `truncate` extends the
        # length without allocating blocks, so it costs no repository bytes
        # (nothing here is committed) and no measurable build time.
        return _scenario_body("all_ok") + textwrap.dedent("""\
            python3 - "$HARNESS_FAKE_HOME/.local/share/cctally/conversations.db-wal" <<'PY'
            import sys
            with open(sys.argv[1], "wb") as fh:
                fh.truncate(256 * 1024 * 1024 + 4096)   # sparse; > DOCTOR_WAL_WARN_BYTES
            PY
        """)
    if slug == "multi_fail_mixed":
        return textwrap.dedent("""\
            mkdir -p "$HARNESS_FAKE_HOME/.local/share/cctally/logs"
            echo '{"hooks":{}}' > "$HARNESS_FAKE_HOME/.claude/settings.json"
            # No OAuth token → FAIL
            cat > "$HARNESS_FAKE_HOME/.local/share/cctally/logs/migration-errors.log" <<'LOG'
            [2026-05-13T14:00:00Z] stats.db:001_five_hour_block_models_backfill_v1
              Boom: details
              Traceback (most recent call last):
                File "<fixture>", line 1, in <module>

            LOG
            # Snapshot stale > 1h → data FAIL
            python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" --emit-snapshot stale_1h \\
                "$HARNESS_FAKE_HOME/.local/share/cctally/stats.db"
            """)
    if slug == "corrupt_config_json":
        return textwrap.dedent("""\
            write_canonical_settings "$HARNESS_FAKE_HOME/.claude/settings.json"
            write_oauth_credentials
            echo '{not valid' > "$HARNESS_FAKE_HOME/.local/share/cctally/config.json"
            python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" --emit-snapshot all_ok \\
                "$HARNESS_FAKE_HOME/.local/share/cctally/stats.db"
            """)
    # ── Issue #119 brew scenarios ─────────────────────────────────────
    # A faithful Homebrew install symlinks every USER_FACING_BIN into
    # <prefix>/bin (cctally.rb.template: `bin.install_symlink`). The
    # _setup_symlink_is_retired predicate keys off the symlink TARGET
    # containing `/Cellar/cctally/`, NOT off repo_root — so doctor fixtures
    # only need a ~/.local/bin link whose target carries that token. No keg
    # COPY is required (unlike the setup harness): doctor runs the dev binary.
    if slug == "brew_pinned_only_path":
        # spec §5.3 g (doctor half): the ONLY path to cctally is a LIVE
        # Cellar ~/.local/bin link; <prefix>/bin is OFF the scrubbed PATH
        # (the harness's PATH keeps ~/.local/bin but never a <prefix>/bin).
        # _reachable_elsewhere(cctally) is False, so the
        # slot classes `wrong` and symlinks_path_pinned is True → doctor emits
        # the PATH-fix remediation (NOT the generic "Run `cctally setup`").
        # The keg file MUST exist (a dangling link would also be `wrong` but
        # would NOT set symlinks_path_pinned, which requires a LIVE retired
        # link). The 12 other slots are empty + not-reachable → `missing`.
        return textwrap.dedent("""\
            KEG="$HARNESS_FAKE_HOME/opt/homebrew/Cellar/cctally/1.20.0/libexec/bin"
            mkdir -p "$KEG"
            cp "$REPO_ROOT/bin/cctally" "$KEG/cctally"
            ln -sf "$KEG/cctally" "$HARNESS_FAKE_HOME/.local/bin/cctally"
            write_canonical_settings "$HARNESS_FAKE_HOME/.claude/settings.json"
            write_oauth_credentials
            python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" --emit-snapshot all_ok \\
                "$HARNESS_FAKE_HOME/.local/share/cctally/stats.db"
            """)
    if slug == "cache_version_ahead":
        # Issue #145: reuse the clean all_ok baseline (stats.db at-head, no
        # version-ahead there), then seed a cache.db whose user_version is
        # pushed past the cache registry head. doctor's raw status read sees
        # cache.db ahead → WARN row, but overall exit stays 0 (auto-heals on
        # next open). stats.db stays NOT ahead so the worst-of-both is WARN.
        bump = textwrap.dedent(f"""\
            python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" --emit-empty-codex-project-metadata \\
                "$HARNESS_FAKE_HOME/.local/share/cctally/cache.db"
            python3 - "$HARNESS_FAKE_HOME/.local/share/cctally/cache.db" <<'PY'
            import sqlite3, sys
            conn = sqlite3.connect(sys.argv[1])
            conn.execute("PRAGMA user_version = {_VERSION_AHEAD_SENTINEL}")
            conn.commit()
            conn.close()
            PY
            """)
        return _scenario_body("all_ok") + bump
    if slug == "stats_version_ahead":
        # Reuse the all_ok baseline, then create an epoch mismatch without any
        # journal bytes. Doctor must not promise that a rebuild can self-heal.
        bump = textwrap.dedent(f"""\
            python3 - "$HARNESS_FAKE_HOME/.local/share/cctally/stats.db" <<'PY'
            import sqlite3, sys
            conn = sqlite3.connect(sys.argv[1])
            conn.execute("PRAGMA user_version = {_VERSION_AHEAD_SENTINEL}")
            conn.commit()
            conn.close()
            PY
            """)
        return _scenario_body("all_ok") + bump
    if slug == "stats_mismatch_with_journal":
        journal = textwrap.dedent(f"""\
            mkdir -p "$HARNESS_FAKE_HOME/.local/share/cctally/journal"
            python3 - "$REPO_ROOT/bin" \\
                "$HARNESS_FAKE_HOME/.local/share/cctally/journal/observations-2026-05.jsonl" <<'PY'
            import pathlib, sys
            sys.path.insert(0, sys.argv[1])
            import _lib_journal as journal
            record = journal.make_obs(
                at="2026-05-13T14:00:00Z",
                src="doctor-fixture",
                provider="claude",
                payload={{"weekly_percent": 1.0}},
            )
            pathlib.Path(sys.argv[2]).write_bytes(journal.encode_line(record))
            PY
            : > "$HARNESS_FAKE_HOME/.local/share/cctally/journal/observations-2026-06.jsonl"
            python3 - "$HARNESS_FAKE_HOME/.local/share/cctally/stats.db" <<'PY'
            import sqlite3, sys
            conn = sqlite3.connect(sys.argv[1])
            conn.execute("PRAGMA user_version = 99")
            conn.commit()
            conn.close()
            PY
            """)
        return _scenario_body("all_ok") + journal
    raise SystemExit(f"unknown scenario slug: {slug}")


def _gitignore() -> str:
    # NOTE: scenarios run in a per-test mktemp -d scratch HOME, so nothing
    # under tests/fixtures/doctor/<NN>/ is written at runtime. The .gitignore
    # exists as a guard for accidental redirection of writes into the tree
    # AND mirrors the convention shared by every other fixture dir (see
    # the "Fixture SQLite DBs must PRAGMA journal_mode=WAL" gotcha in
    # CLAUDE.md — each fixture dir carries its own *.db-wal/*.db-shm ignore).
    return textwrap.dedent("""\
        # Spawned at runtime — never commit.
        *.db
        *.db-wal
        *.db-shm
        *.lock
        config.json.lock
        oauth-token
    """)


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--emit-snapshot", choices=["all_ok", "stale_1h"])
    p.add_argument("--emit-codex-cache", action="store_true")
    p.add_argument("--emit-account-bound", choices=["clear"])
    p.add_argument("--emit-empty-codex-project-metadata", action="store_true")
    p.add_argument("--emit-retention", choices=[
        "retained_artifacts_over_budget", "retained_artifacts_protected",
        "heal_repeated_shape",
    ])
    p.add_argument("--emit-codex-doctor-case", choices=[
        "quota-fresh-stale", "hooks-disabled-mixed-roots",
    ])
    p.add_argument(
        "--out", type=pathlib.Path, default=None,
        help="Override the output directory (defaults to tests/fixtures/doctor/). "
             "Harnesses build into a per-run scratch dir so the committed "
             "fixtures stay byte-stable and a test run leaves the tracked tree "
             "unchanged. Applies to the plain scaffolding mode only; the "
             "--emit-* modes take their destination as a positional path.",
    )
    p.add_argument("path", nargs="?")
    p.add_argument("roots", nargs="*")
    args = p.parse_args()

    if args.emit_snapshot and args.path:
        _emit_snapshot(args.emit_snapshot, pathlib.Path(args.path))
        return
    if args.emit_account_bound and args.path:
        _emit_account_bound(pathlib.Path(args.path))
        return
    if args.emit_codex_cache and args.path:
        _emit_codex_cache(pathlib.Path(args.path))
        return
    if args.emit_empty_codex_project_metadata and args.path:
        _emit_empty_codex_project_metadata(pathlib.Path(args.path))
        return
    if args.emit_retention and args.path:
        _emit_retention(args.emit_retention, pathlib.Path(args.path))
        return
    if args.emit_codex_doctor_case:
        if not args.path or len(args.roots) != 2:
            raise SystemExit("--emit-codex-doctor-case needs an app directory and two roots")
        _emit_codex_doctor_case(
            args.emit_codex_doctor_case,
            pathlib.Path(args.path),
            *(pathlib.Path(root) for root in args.roots),
        )
        return

    # Plain mode: scaffold every scenario directory.
    root = ROOT if args.out is None else args.out
    root.mkdir(parents=True, exist_ok=True)
    for dir_name, slug in SCENARIOS.items():
        d = root / dir_name
        d.mkdir(parents=True, exist_ok=True)
        (d / "setup.sh").write_text(
            (_common_setup_preamble() + _scenario_body(slug)).replace(
                "{USER_FACING_WRAPPERS}", USER_FACING_WRAPPERS
            )
        )
        (d / "setup.sh").chmod(0o755)
        (d / ".gitignore").write_text(_gitignore())
        homes_path = d / ".codex-homes"
        homes = CODEX_HOMES.get(dir_name)
        if homes:
            homes_path.write_text("\n".join(homes) + "\n")
        elif homes_path.exists():
            homes_path.unlink()
    print(f"Scaffolded {len(SCENARIOS)} scenarios under {root}")



#: #496 S6 §7.6. The four scenarios that reach `db.retained_artifacts`'s WARN
#: and both FAIL branches, and `journal.auto_heal`'s repeated-shape FAIL.
#:
#: Every incident is CLASSIFIED unless the scenario is specifically about
#: protection, because an unclassified incident is protected and would make
#: the over-budget scenario reach the FAIL branch instead of the WARN one —
#: which is exactly the "one fixture stands in for two branches" problem §7.6
#: says four fixtures exist to avoid.
_RETENTION_INCIDENT_BYTES = 3 * 1024 * 1024


def _retention_scenario_body(slug: str) -> str:
    import textwrap

    return textwrap.dedent(f"""\
        python3 "$REPO_ROOT/bin/build-doctor-fixtures.py" --emit-retention \\
            {slug} "$HARNESS_FAKE_HOME/.local/share/cctally"
    """)


def _emit_retention(slug: str, app_dir) -> None:
    """Build one retained-artifact corpus for a doctor fixture (§7.6)."""
    import json
    import pathlib as _pl

    app_dir = _pl.Path(app_dir)
    quarantine = app_dir / "quarantine"
    logs = app_dir / "logs"
    quarantine.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    classified = slug != "retained_artifacts_protected"
    shape = "1702f06a27" if slug == "heal_repeated_shape" else None
    count = 2 if slug == "heal_repeated_shape" else 3
    # `journal.auto_heal` only escalates on a repeated shape seen WITHIN the
    # seven-day window, so this scenario's incidents must sit inside it
    # relative to the harness's pinned `CCTALLY_AS_OF` (2026-05-13). Dating
    # them in January instead produced a fixture that could not reach the
    # branch it exists to cover.
    days = (11, 12) if slug == "heal_repeated_shape" else (1, 2, 3)
    month = "05" if slug == "heal_repeated_shape" else "01"
    for index in range(count):
        stamp = f"2026{month}{days[index]:02d}T120000Z"
        incident = quarantine / f"stats.db-{stamp}"
        incident.mkdir(parents=True, exist_ok=True)
        (incident / "stats.db").write_bytes(b"x" * _RETENTION_INCIDENT_BYTES)
        manifest = {
            "schemaVersion": 2 if classified else 1,
            "quarantinedAtUtc": f"2026-{month}-{days[index]:02d}T12:00:00Z",
            "originalPath": str(app_dir / "stats.db"),
            "movedFiles": ["stats.db"],
            "complete": True,
        }
        if classified:
            manifest["trigger"] = "corruption-heal"
        if shape is not None:
            manifest["damage"] = {"preserved": {"shapeToken": shape}}
        (incident / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8",
        )

    config = app_dir / "config.json"
    loaded = {}
    if config.exists():
        try:
            loaded = json.loads(config.read_text(encoding="utf-8"))
        except ValueError:
            loaded = {}
    # A 1 MiB budget against a multi-megabyte corpus, so the bound fires on
    # the fixture's own scale rather than on the production default — a
    # threshold test whose fixture is below the threshold cannot fail.
    loaded["storage.artifact_retention"] = {
        "max_age_days": 1, "max_total_mib": 1, "min_free_mib": None,
    }
    config.write_text(json.dumps(loaded, indent=2), encoding="utf-8")

#: #825/#832: the pinned instants of the account-bound scenarios. The harness
#: runs doctor at 2026-05-13T14:22:31Z; the last current reading is two
#: seconds before it, so `freshness.latest_snapshot` stays fresh.
_AB_FIRST_READING = "2026-05-13T14:00:00Z"
_AB_EVIDENCE_SEEN = "2026-05-13T14:20:00Z"
_AB_LAST_READING = "2026-05-13T14:22:29Z"
_AB_WEEK_RESET = "2026-05-16T00:00:00Z"


def _emit_account_bound(home: pathlib.Path) -> None:
    """Build a current, validated epoch-1018 store under ``home`` through the
    REAL bound recording kernel, deterministically.

    Every clock the store records is pinned: `CCTALLY_AS_OF` with
    `CCTALLY_TEST_PIN_CAPTURE` for the readings, and the journal's append,
    op-time and legacy-writer-evidence clocks for the segment name, the
    cutover op and `firstObservedAt`."""
    import datetime as dt
    import importlib.machinery
    import importlib.util
    import json
    import os
    import sys

    def epoch(text):
        return int(dt.datetime.fromisoformat(
            text.replace("Z", "+00:00")).timestamp())

    def pinned(text):
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))

    os.environ["HOME"] = str(home)
    os.environ.pop("CCTALLY_DATA_DIR", None)
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    os.environ["CCTALLY_DISABLE_TELEMETRY"] = "1"
    os.environ["CCTALLY_TEST_PIN_CAPTURE"] = "1"
    bin_dir = str(ENTRY_POINT.parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    loader = importlib.machinery.SourceFileLoader("cctally", str(ENTRY_POINT))
    spec = importlib.util.spec_from_loader("cctally", loader)
    cctally = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = cctally
    loader.exec_module(cctally)
    import _cctally_core as core
    core._init_paths_from_env()
    import _cctally_journal as jr
    import _cctally_statusline as sl
    import argparse

    clock = {"now": _AB_FIRST_READING}
    jr._utc_now = lambda: pinned(clock["now"])
    jr._now_iso = lambda: clock["now"]
    jr._evidence_now = lambda: pinned(_AB_EVIDENCE_SEEN)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "accountUuid": "doctor-fixture-account",
        "emailAddress": "fixture@example.test"}}))

    def record(at, weekly, five):
        clock["now"] = at
        os.environ["CCTALLY_AS_OF"] = at
        args = argparse.Namespace(
            percent=weekly, resets_at=str(epoch(_AB_WEEK_RESET)),
            five_hour_percent=five,
            five_hour_resets_at=str(epoch("2026-05-13T18:00:00Z")),
            # Not `api`: a bound two-axis `api` reading is a control witness
            # (spec 5.6 item 7), which would restore the control certificate
            # and leave only the accounting half paused.
            source="statusline")
        rc = cctally.record_usage_bound(
            args, binding=sl._capture_binding(), nudge_dashboard=False)
        if rc != 0:
            raise SystemExit(f"record_usage_bound exited {rc}")

    record(_AB_FIRST_READING, 1.0, 2.0)
    record(_AB_LAST_READING, 2.0, 3.0)
    # The recordings' Claude cache sync stamps its parse-health record with the
    # real wall clock (`_update_parse_health_meta`); doctor's verbose and JSON
    # output show it, so pin it like every other recorded instant.
    import sqlite3
    cache = sqlite3.connect(core.APP_DIR / "cache.db")
    try:
        for key, value in cache.execute(
                "SELECT key, value FROM cache_meta").fetchall():
            try:
                doc = json.loads(value)
            except (TypeError, ValueError):
                continue
            if isinstance(doc, dict) and "last_write_at" in doc:
                doc["last_write_at"] = doc["since"] = _AB_LAST_READING
                cache.execute("UPDATE cache_meta SET value = ? WHERE key = ?",
                              (json.dumps(doc, sort_keys=True), key))
        cache.commit()
    finally:
        cache.close()


def _emit_snapshot(slug: str, db_path: pathlib.Path) -> None:
    """Seed weekly_usage_snapshots with a single row at the expected age."""
    import sqlite3
    import os
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_usage_snapshots (
            id INTEGER PRIMARY KEY,
            captured_at_utc TEXT NOT NULL,
            week_start_date TEXT NOT NULL,
            used_percent REAL NOT NULL
        )
    """)
    as_of = os.environ.get("CCTALLY_AS_OF", "2026-05-13T14:22:31+00:00")
    import datetime as dt
    now = dt.datetime.fromisoformat(as_of).astimezone(dt.timezone.utc)
    offsets = {"all_ok": 2, "stale_1h": 7200}
    captured = now - dt.timedelta(seconds=offsets[slug])
    conn.execute(
        "INSERT INTO weekly_usage_snapshots(captured_at_utc, week_start_date, used_percent) VALUES (?, ?, ?)",
        (captured.isoformat().replace("+00:00", "Z"), "2026-05-11", 12.5),
    )
    conn.commit()
    conn.close()


def _ensure_empty_codex_project_metadata_schema(conn) -> None:
    """Give doctor fixtures the #312 read-only metadata contract.

    The scenario builders intentionally use minimal databases.  The Doctor
    probe must still distinguish an empty retained Codex corpus from a failed
    query, so every fixture cache that exists provides the two empty tables.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS codex_session_entries (
            id INTEGER PRIMARY KEY,
            timestamp_utc TEXT NOT NULL,
            session_id TEXT, model TEXT,
            input_tokens INTEGER, cached_input_tokens INTEGER,
            output_tokens INTEGER, reasoning_output_tokens INTEGER,
            source_root_key TEXT, conversation_key TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS codex_conversation_threads (
            conversation_key TEXT NOT NULL,
            source_root_key TEXT NOT NULL,
            -- #845 §4.3: the decode inventory reads these four columns off the
            -- threads table on every health read, so a fixture that omits them
            -- makes `load_codex_project_metadata_health` raise
            -- `OperationalError: no such column` and doctor reports a metadata
            -- FAIL for a store shape no real cache has. The real schema in
            -- `bin/_cctally_db.py` carries all four.
            native_thread_id TEXT,
            last_seen_utc TEXT,
            cwd TEXT,
            git_json TEXT
        )
    """)


def _emit_empty_codex_project_metadata(db_path: pathlib.Path) -> None:
    """Add the #312 health tables without changing a fixture's corpus."""
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        _ensure_empty_codex_project_metadata_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _emit_codex_cache(db_path: pathlib.Path) -> None:
    import sqlite3
    import os, datetime as dt
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_empty_codex_project_metadata_schema(conn)
    as_of = os.environ.get("CCTALLY_AS_OF", "2026-05-13T14:22:31+00:00")
    now = dt.datetime.fromisoformat(as_of).astimezone(dt.timezone.utc) - dt.timedelta(seconds=60)
    conn.execute(
        "INSERT INTO codex_session_entries(timestamp_utc, session_id, model, "
        "input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, "
        "source_root_key, conversation_key) "
        "VALUES (?, 's1', 'gpt-5', 100, 50, 80, 30, 'fixture-root', 'fixture-key')",
        (now.isoformat(),),
    )
    conn.execute(
        "INSERT INTO codex_conversation_threads(conversation_key, source_root_key) "
        "VALUES ('fixture-key', 'fixture-root')"
    )
    conn.commit()
    conn.close()


def _emit_codex_doctor_case(
    case: str, app_dir: pathlib.Path, first_root: pathlib.Path,
    second_root: pathlib.Path,
) -> None:
    """Seed root-qualified quota evidence and lifecycle logs for doctor goldens."""
    import datetime as dt
    import hashlib
    import os
    import sqlite3

    def root_key(root: pathlib.Path) -> str:
        return hashlib.sha256(
            b"cctally-source-root-v1\0" + str(root.resolve()).encode("utf-8")
        ).hexdigest()[:32]

    as_of = dt.datetime.fromisoformat(
        os.environ.get("CCTALLY_AS_OF", "2026-05-13T14:22:31+00:00")
    ).astimezone(dt.timezone.utc)
    roots = sorted(((root_key(first_root), first_root), (root_key(second_root), second_root)))
    app_dir.mkdir(parents=True, exist_ok=True)
    log_path = app_dir / "logs" / "hook-tick.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_line(at: dt.datetime, key: str) -> str:
        return (
            f"{at.isoformat().replace('+00:00', 'Z')} provider=codex "
            f"source_root_key={key} event=Stop sync=ok blocks=1 milestones=1 "
            f"alert_eligible_roots=1 dur_ms=12 result=success\n"
        )

    def write_trust(root: pathlib.Path, *, enabled=None):
        """Record a Codex `[hooks.state]` trust decision for this root.

        The key's file component is the RESOLVED hooks.json path, because
        `codex_hook_roots` resolves every configured home before building it.
        `config.toml` is then made newer than `hooks.json` so the freshness
        test in the classification reports `installed_enabled` rather than
        `installed_unverified`.
        """
        hooks_path = (root / "hooks.json").resolve()
        config = root / "config.toml"
        lines = []
        for token in ("stop", "subagent_stop"):
            lines.append(f'[hooks.state."{hooks_path}:{token}:0:0"]')
            lines.append(f'trusted_hash = "fixture-hash-{token}"')
            if enabled is not None:
                lines.append(f"enabled = {str(enabled).lower()}")
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        stamp = as_of.timestamp()
        os.utime(config, (stamp, stamp))
        os.utime(hooks_path, (stamp - 3600, stamp - 3600))

    def write_marker(root_key: str, age_seconds: float):
        base = app_dir / "codex-hook-tick"
        base.mkdir(parents=True, exist_ok=True)
        marker = base / f"{root_key}.last-success"
        marker.write_text("", encoding="utf-8")
        stamp = as_of.timestamp() - age_seconds
        os.utime(marker, (stamp, stamp))

    if case == "quota-fresh-stale":
        db_path = app_dir / "cache.db"
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_empty_codex_project_metadata_schema(conn)
            conn.execute("""
                CREATE TABLE quota_window_snapshots (
                    source TEXT NOT NULL, source_root_key TEXT, source_path TEXT NOT NULL,
                    line_offset INTEGER NOT NULL, captured_at_utc TEXT NOT NULL,
                    observed_slot TEXT, logical_limit_key TEXT NOT NULL, limit_id TEXT,
                    limit_name TEXT, window_minutes INTEGER NOT NULL, used_percent REAL NOT NULL,
                    resets_at_utc TEXT NOT NULL, plan_type TEXT, individual_limit_json TEXT,
                    reached_type TEXT
                )
            """)
            unhealthy_capture = as_of - dt.timedelta(seconds=901)
            conn.executemany("""
                INSERT INTO quota_window_snapshots(
                    source, source_root_key, source_path, line_offset, captured_at_utc,
                    observed_slot, logical_limit_key, limit_id, limit_name, window_minutes,
                    used_percent, resets_at_utc, plan_type, individual_limit_json, reached_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                ("codex", roots[0][0], str(roots[0][1] / "sessions" / "a.jsonl"), 1,
                 unhealthy_capture.isoformat().replace("+00:00", "Z"), "secondary",
                 "secondary", "secondary", "Secondary", 60, 42.0,
                 "2026-05-13T15:00:00Z", None, None, None),
                ("codex", roots[1][0], str(roots[1][1] / "sessions" / "b.jsonl"), 2,
                 (as_of - dt.timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
                 "primary", "primary", "primary", "Primary", 300, 12.0,
                 "2026-05-13T19:00:00Z", None, None, None),
            ])
            conn.commit()
        finally:
            conn.close()
        # 21 exercises quota freshness, so it carries a valid enabled trust
        # record and fresh success markers: an unrelated Codex hook FAIL would
        # otherwise own its exit code.
        for _key, root in roots:
            write_trust(root)
        for key, _root in roots:
            write_marker(key, 120)
        log_path.write_text(
            log_line(as_of - dt.timedelta(seconds=30), roots[0][0])
            + log_line(as_of - dt.timedelta(seconds=20), roots[1][0])
        )
        return

    if case == "hooks-disabled-mixed-roots":
        # #719 acceptance 5: a healthy sibling must never mask a disabled root.
        write_trust(roots[0][1])
        write_trust(roots[1][1], enabled=False)
        write_marker(roots[0][0], 120)
        log_path.write_text(log_line(as_of - dt.timedelta(seconds=30), roots[0][0]))
        return
    raise ValueError(f"unsupported Codex doctor fixture case: {case}")


if __name__ == "__main__":
    main()
