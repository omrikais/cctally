#!/usr/bin/env python3
"""Build fixtures for bin/cctally-release-test.

Each scenario is a directory under tests/fixtures/release/ containing:
  - setup.sh                   : bash script that builds an isolated git
                                 layout under $work/ — private working
                                 clone + private.git bare + public working
                                 clone + public.git bare — and seeds the
                                 private CHANGELOG.md per-scenario.
  - run.sh                     : bash script that the harness invokes from
                                 inside private/ to run `cctally release`
                                 with the right flags + capture phase
                                 artifacts.
  - golden-exit.txt            : single-line expected exit code.
  - golden-stdout-substr.txt   : substring expected in stdout (may be
                                 empty for silent paths).
  - golden-stderr-substr.txt   : substring expected in stderr (refusal
                                 paths). Empty file = no stderr check.
  - golden-changelog.md        : optional — exact post-release CHANGELOG.md
                                 content. Missing = no check (e.g. dry-run).
  - golden-commit-msg.txt      : optional — `git log -1 --format=%B` of the
                                 stamp commit, with SHA placeholder
                                 substitution applied (run.sh emits with
                                 `<SHA7>` placeholder).
  - golden-tag-annotation.txt  : optional — `git for-each-ref` body of the
                                 annotated release tag.

The harness invokes setup.sh, then runs run.sh from inside private/,
capturing exit/stdout/stderr.

Determinism env hooks:
  - CCTALLY_RELEASE_DATE_UTC=2026-05-07 — pins the stamped date.
  - GIT_AUTHOR_*/GIT_COMMITTER_* set in setup.sh — stable identity.

Scenario architecture (Task 12):
  Each SCENARIOS entry is a dict with keys:
    name           : fixture dir name
    seed_changelog : str — CHANGELOG.md body to seed (built via _changelog)
    extra_setup    : str — bash snippet appended after the seed commit
                     (e.g. checkout branch, dirty file, pre-run phase 1).
                     Empty string when no extra setup is needed.
    run            : str — run.sh body (use _run_release / _run_dry_run).
    expected_exit  : int
    stdout_substr  : str ('' = no check)
    stderr_substr  : str ('' = no check)
    changelog      : str | None — optional byte-exact post-state CHANGELOG.
    commit_msg     : str | None — optional byte-exact stamp commit message.
    tag_annotation : str | None — optional byte-exact tag annotation body.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "release"


# ---------------------------------------------------------------------------
# _SCAFFOLD: bash header that every setup.sh starts with.
#
# Convention:
#   - cwd at script entry is $work/ (the per-scenario scratch dir).
#   - Creates $work/private/, $work/private.git/, $work/public/,
#     $work/public.git/ as siblings.
#   - private/ is a working clone whose `origin` points at private.git/
#     (the "private remote" — release Phase 2 pushes here).
#   - public/ is a working clone whose `origin` points at public.git/
#     (the "public mirror" — release Phase 3 pushes here).
#   - Copies bin/cctally + bin/cctally-mirror-public + .mirror-allowlist
#     + .githooks/ into private/ so __file__-relative path resolution
#     (CHANGELOG_PATH, mirror_tool location) lands in private/, not in
#     cctally-dev itself.
#   - Seeds CHANGELOG.md per-scenario (added below the scaffold).
#   - Sets `release.publicClone` git config in private/ to $work/public/
#     so `_release_discover_public_clone` finds it. Scenarios that need
#     to test discovery refusal explicitly unset this in extra_setup.
#   - Provides a fake `gh` binary in $work/fake-bin/ that records its
#     argv to $work/gh-argv.log and exits 0 for the auth probes. The
#     harness's run.sh prepends $work/fake-bin to PATH.
#       - When `gh release create --notes-file <path>` is invoked, the
#         fake also COPIES the notes file content to
#         $work/_artifacts/gh-notes.txt so body-canonical scenarios can
#         compare it byte-for-byte against the commit + tag.
#   - Ends with cwd = $work/private/.
# ---------------------------------------------------------------------------
_SCAFFOLD = '''#!/bin/bash
set -euo pipefail
work="$(pwd)"
export work
REPO_ROOT="$1"

# Suppress .pyc generation across the scenario. cctally's mirror tool
# imports `.githooks/_match`, which would otherwise leave
# `.githooks/__pycache__/` as untracked files in the private worktree
# and trip the release script's clean-tree preflight.
export PYTHONDONTWRITEBYTECODE=1

mkdir -p "$work/private" "$work/private.git" "$work/public" "$work/public.git" "$work/fake-bin" "$work/_artifacts"

# Stable identity for every git invocation in this scenario, regardless of
# host config. Matches the invariants other harnesses rely on for
# byte-stable goldens.
export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="test@example.com"
export GIT_AUTHOR_DATE="2026-05-07T00:00:00+0000"
export GIT_COMMITTER_DATE="2026-05-07T00:00:00+0000"

# Fake `gh` binary: records argv + exits 0 on auth probes. Phase 4 calls
# `gh auth status`, `gh api repos/...`, `gh release view`, then
# `gh release create`. The fake records every invocation; auth-probe
# returns 0; `release view` returns 1 (release does not yet exist);
# `release create` returns 0 (success). When `--notes-file <path>` is
# in argv, the fake copies that file's contents to the artifact dir so
# body-canonical tests can compare it byte-for-byte.
cat > "$work/fake-bin/gh" <<'CCTALLY_FAKE_GH_EOF'
#!/usr/bin/env bash
# Argv recording (one line per invocation; preserves ordering).
echo "$@" >> "${GH_ARGV_LOG:-/dev/null}"

# If the invocation includes `--notes-file <path>`, snapshot the file
# contents to the artifact dir so body-canonical scenarios have a
# stable handle on what was passed.
prev=""
for tok in "$@"; do
  if [ "$prev" = "--notes-file" ] && [ -f "$tok" ] && [ -n "${GH_NOTES_DEST:-}" ]; then
    cp "$tok" "$GH_NOTES_DEST" || true
  fi
  prev="$tok"
done

case "$1" in
  auth) exit "${FAKE_GH_AUTH_EXIT:-0}" ;;
  api)  exit "${FAKE_GH_API_EXIT:-0}" ;;
  release)
    case "$2" in
      view) exit "${FAKE_GH_RELEASE_VIEW_EXIT:-1}" ;;
      *) exit "${FAKE_GH_RELEASE_CREATE_EXIT:-0}" ;;
    esac
    ;;
esac
exit 0
CCTALLY_FAKE_GH_EOF
chmod +x "$work/fake-bin/gh"

# Public bare + working clone. Init bare first; clone or wire origin so
# the public/ working dir's `origin` points at public.git/.
git init -q --bare --initial-branch=main "$work/public.git" 2>/dev/null \\
    || git init -q --bare "$work/public.git"
cd "$work/public"
git init -q --initial-branch=main 2>/dev/null || git init -q
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
git config tag.gpgsign false
git remote add origin "$work/public.git"
git commit -q --allow-empty -m "init"
git push -q origin main

# Private bare + working clone.
git init -q --bare --initial-branch=main "$work/private.git" 2>/dev/null \\
    || git init -q --bare "$work/private.git"
cd "$work/private"
git init -q --initial-branch=main 2>/dev/null || git init -q
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
git config tag.gpgsign false
git remote add origin "$work/private.git"
# Wire the public-clone discovery so cmd_release Phase 3 finds it.
# Scenarios that test discovery refusal `git config --unset` this in
# their extra_setup snippet.
git config release.publicClone "$work/public"

# Copy infrastructure files so cctally + the mirror tool resolve
# correctly via __file__.parent.parent inside this scratch repo.
mkdir -p .githooks bin
cp "$REPO_ROOT/.mirror-allowlist" .
cp "$REPO_ROOT/.githooks/_match.py" .githooks/
cp "$REPO_ROOT/.githooks/_public_trailer.py" .githooks/
cp "$REPO_ROOT/.githooks/_skip_chain_metrics.py" .githooks/
cp "$REPO_ROOT/.githooks/commit-msg" .githooks/
cp "$REPO_ROOT/.githooks/pre-commit" .githooks/
cp "$REPO_ROOT/.public-tag-patterns" .
cp "$REPO_ROOT/bin/cctally" bin/
chmod +x bin/cctally
cp "$REPO_ROOT/bin/cctally-mirror-public" bin/
chmod +x bin/cctally-mirror-public

# CHANGELOG.md is seeded per-scenario (after this scaffold ends). The
# infra-bootstrap commit lands AFTER CHANGELOG.md is written so the
# initial commit on `main` already contains a release-shaped CHANGELOG.
'''


# Helper: emit a CHANGELOG.md with a given Unreleased + prior-release block.
def _changelog(unreleased_subsections: list[tuple[str, list[str]]] | None,
               prior_releases: list[tuple[str, str, list[tuple[str, list[str]]]]]) -> str:
    """Build a CHANGELOG.md body string.

    unreleased_subsections: list of (heading, [bullets]) — e.g.
        [("Added", ["- New thing"]), ("Fixed", ["- Bug X"])].
        Pass None for "no subsections at all" (header alone). Pass []
        for "header present but empty subsections" — same on disk.
    prior_releases: list of (version, date, subsections) — emitted in
        order under the Unreleased block.
    """
    lines = ["# Changelog", ""]
    lines.append("## [Unreleased]")
    lines.append("")
    if unreleased_subsections:
        for heading, bullets in unreleased_subsections:
            lines.append(f"### {heading}")
            for b in bullets:
                lines.append(b)
            lines.append("")
    for version, date, subs in prior_releases:
        lines.append(f"## [{version}] - {date}")
        lines.append("")
        for heading, bullets in subs:
            lines.append(f"### {heading}")
            for b in bullets:
                lines.append(b)
            lines.append("")
    # Trim trailing blank lines, then exactly one terminator.
    return "\n".join(lines).rstrip() + "\n"


def _seed_changelog_and_commit(content: str, push: bool = True,
                                bootstrap_public: bool = False) -> str:
    """Bash snippet (run with cwd=$work/private) that writes CHANGELOG.md
    and lands the seed commit, then optionally pushes to origin.

    `push=False` is used by `behind-remote-refuse` so the public origin
    can be advanced one commit ahead AFTER this seed.

    `bootstrap_public=True` propagates the seed commit's public-classified
    file tree onto the public clone (without going through the mirror
    tool, which would refuse since the public clone already has commits).
    Required for any scenario that exercises Phase 3: without it, the
    mirror tool's priv→pub fingerprint-match would fail to bind the
    release commit's full public tree to the public commit (which only
    contains the diff-only files), and the v<X.Y.Z> tag would be held
    back as "tag not propagated."
    """
    push_step = (
        "git -c tag.gpgsign=false tag mirror-cursor HEAD\n"
        "git push -q origin main --follow-tags\n"
        if push else ""
    )
    # Bootstrap the public clone with the seed's full public-classified
    # tree. Walks the in-tree .mirror-allowlist semantics inline by
    # invoking the mirror tool's classifier via a tiny python one-liner
    # (rather than reimplementing matching). The tree gets committed on
    # the public side as "seed" and pushed; mirror-cursor on private
    # stays at HEAD so future mirror runs walk only post-seed commits.
    bootstrap_step = (
        '# Bootstrap public clone with seed tree (public-classified files\n'
        '# from private/HEAD). Resolves the priv→pub fingerprint-match\n'
        '# requirement for Phase 3 scenarios (full public tree must already\n'
        '# exist in the public commit for tag propagation to bind).\n'
        '# PYTHONDONTWRITEBYTECODE prevents .pyc generation in .githooks/,\n'
        '# which would otherwise leave the private worktree dirty.\n'
        'PYTHONDONTWRITEBYTECODE=1 python3 - <<\'CCTALLY_BOOTSTRAP_PY_EOF\'\n'
        'import os, shutil, subprocess, sys\n'
        'from pathlib import Path\n'
        'private = Path(os.environ["work"]) / "private"\n'
        'public = Path(os.environ["work"]) / "public"\n'
        'sys.path.insert(0, str(private / ".githooks"))\n'
        'import _match\n'
        'paths = subprocess.check_output(\n'
        '    ["git", "-C", str(private), "ls-tree", "-r", "--name-only", "HEAD"],\n'
        '    text=True,\n'
        ').splitlines()\n'
        'classified = _match.classify(paths, str(private / ".mirror-allowlist"))\n'
        'for p in classified["public"]:\n'
        '    src = private / p\n'
        '    dst = public / p\n'
        '    dst.parent.mkdir(parents=True, exist_ok=True)\n'
        '    shutil.copy2(src, dst)\n'
        'CCTALLY_BOOTSTRAP_PY_EOF\n'
        '(cd "$work/public" && \\\n'
        '  git add -A && \\\n'
        '  git commit -q --no-verify -m "seed: bootstrap public mirror" && \\\n'
        '  git push -q origin main)\n'
        if bootstrap_public else ""
    )
    return (
        f'cat > CHANGELOG.md <<\'CCTALLY_CHANGELOG_EOF\'\n'
        f'{content}'
        f'CCTALLY_CHANGELOG_EOF\n'
        f'git add -A\n'
        f"git commit --no-verify -q -F - <<'CCTALLY_SEED_MSG_EOF'\n"
        f'chore: seed CHANGELOG\n'
        f'\n'
        f'--- public ---\n'
        f'chore: seed CHANGELOG\n'
        f'CCTALLY_SEED_MSG_EOF\n'
        + push_step
        + bootstrap_step
    )


# ---------------------------------------------------------------------------
# Run-sh helpers.
#
# Every scenario's run.sh starts with `_RUN_HEADER` (env pinning + fake-gh
# wiring), then issues a single `python3 bin/cctally release ...` and
# captures artifacts into $work/_artifacts/.
# ---------------------------------------------------------------------------

_RUN_HEADER = '''#!/bin/bash
set -uo pipefail
work="$(pwd)/.."
REPO_ROOT="$1"

# Suppress .pyc generation (parity with setup.sh).
export PYTHONDONTWRITEBYTECODE=1

# Determinism: pin author/committer + release date.
export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="test@example.com"
export GIT_AUTHOR_DATE="2026-05-07T00:00:00+0000"
export GIT_COMMITTER_DATE="2026-05-07T00:00:00+0000"
export CCTALLY_RELEASE_DATE_UTC="2026-05-07"

# Fake `gh` recording: prepend the scaffold's fake-bin to PATH.
export PATH="$work/fake-bin:$PATH"
export GH_ARGV_LOG="$work/gh-argv.log"
export GH_NOTES_DEST="$work/_artifacts/gh-notes.txt"

# Per-scenario artifact dir; the harness reads files under it for the
# golden-* comparisons.
mkdir -p "$work/_artifacts"
'''


_CAPTURE_ARTIFACTS = (
    # Capture CHANGELOG (always — even on refusal).
    'cp CHANGELOG.md "$work/_artifacts/changelog.md" 2>/dev/null || true\n'
    # Capture package.json (only when the scenario seeded one; harmless
    # `|| true` so old scenarios without package.json don't fail). Used
    # by the harness's optional golden-package-json.json byte-check.
    'cp package.json "$work/_artifacts/package.json" 2>/dev/null || true\n'
    # Stamp commit message — strip SHAs so the golden is byte-stable.
    'git log -1 --format=%B 2>/dev/null '
    '| sed -E "s/[0-9a-f]{7,40}/<SHA7>/g" '
    '> "$work/_artifacts/commit-msg.txt" || true\n'
    # Tag annotation — body of the release tag at HEAD.
    'tag_name=$(git tag --points-at HEAD 2>/dev/null | grep -E \'^v[0-9]\' | head -n1)\n'
    'if [ -n "$tag_name" ]; then\n'
    '  git tag -l --format="%(contents)" "$tag_name" '
    '> "$work/_artifacts/tag-annotation.txt"\n'
    'fi\n'
    # Capture gh argv log if it exists (may be empty).
    'if [ -f "$work/gh-argv.log" ]; then\n'
    '  cp "$work/gh-argv.log" "$work/_artifacts/gh-argv.log"\n'
    'fi\n'
)


def _run_release(release_args: str) -> str:
    """run.sh body that invokes `cctally release <release_args>` and
    captures all standard artifacts (stdout, stderr, exit, CHANGELOG,
    commit-msg, tag-annotation, gh-argv).

    `release_args` is appended verbatim to the python invocation. Quoting
    is the caller's responsibility.
    """
    return _RUN_HEADER + (
        f'python3 bin/cctally release {release_args} '
        f'> "$work/_artifacts/stdout.txt" 2> "$work/_artifacts/stderr.txt"\n'
        f'rc=$?\n'
        f'echo "$rc" > "$work/_artifacts/exit.txt"\n'
        + _CAPTURE_ARTIFACTS
        + 'exit "$rc"\n'
    )


def _run_dry_run(release_args: str) -> str:
    """run.sh body for dry-run scenarios — captures artifacts AND asserts
    no tags / commits leaked beyond the seed."""
    return _RUN_HEADER + (
        f'python3 bin/cctally release {release_args} --dry-run '
        f'> "$work/_artifacts/stdout.txt" 2> "$work/_artifacts/stderr.txt"\n'
        f'rc=$?\n'
        f'echo "$rc" > "$work/_artifacts/exit.txt"\n'
        + _CAPTURE_ARTIFACTS
        + 'tag_count=$(git tag -l | grep -E \'^v[0-9]\' | wc -l | tr -d " ")\n'
        + 'if [ "$tag_count" != "0" ]; then\n'
        + '  echo "ASSERT_FAIL: dry-run created tags ($tag_count)" >&2\n'
        + '  exit 9\n'
        + 'fi\n'
        + 'exit "$rc"\n'
    )


def _run_body_canonical(release_args: str) -> str:
    """run.sh body for body-canonical-three-sources: extracts the public
    block of the stamp commit, the tag annotation body, and the gh
    --notes-file content; asserts byte-equality across all three.

    Writes:
      - $work/_artifacts/body-from-commit.txt
      - $work/_artifacts/body-from-tag.txt
      - $work/_artifacts/body-from-gh.txt
      - $work/_artifacts/body-equal.txt = "1" or "0"
    """
    return _RUN_HEADER + (
        f'python3 bin/cctally release {release_args} '
        f'> "$work/_artifacts/stdout.txt" 2> "$work/_artifacts/stderr.txt"\n'
        f'rc=$?\n'
        f'echo "$rc" > "$work/_artifacts/exit.txt"\n'
        + _CAPTURE_ARTIFACTS
        + r'''
# Extract body from stamp commit message (text after the `--- public ---`
# subject line + blank line). awk machine: switch into `in_pub` after
# the marker, drop one subject line, drop one blank line, then echo all
# subsequent lines verbatim.
git log -1 --format=%B > "$work/_artifacts/full-commit-msg.txt" 2>/dev/null || true
# Extract body, then strip trailing newline (the wrapping `\n` in the
# commit message; canonical body has none per spec §6.4).
awk '
  /^--- public ---$/ { in_pub = 1; skip = 2; next }
  in_pub && skip > 0 { skip--; next }
  in_pub { print }
' "$work/_artifacts/full-commit-msg.txt" \
  | python3 -c "import sys; sys.stdout.write(sys.stdin.read().rstrip(chr(10)))" \
  > "$work/_artifacts/body-from-commit.txt"

# Tag annotation body — strip first two lines (`vX.Y.Z` + blank) so what
# remains is the body. PGP signature block (signed tags) is stripped via
# BEGIN/END markers. Trailing newline from `git tag --format=%(contents)`
# is stripped to match canonical body (no trailing newline).
tag_name=$(git tag --points-at HEAD 2>/dev/null | grep -E '^v[0-9]' | head -n1)
if [ -n "$tag_name" ]; then
  git tag -l --format="%(contents)" "$tag_name" \
    | awk '
        /^-----BEGIN PGP SIGNATURE-----$/ { in_sig = 1 }
        !in_sig { print }
        /^-----END PGP SIGNATURE-----$/ { in_sig = 0 }
      ' \
    | sed '1,2d' \
    | python3 -c "import sys; sys.stdout.write(sys.stdin.read().rstrip(chr(10)))" \
    > "$work/_artifacts/body-from-tag.txt"
fi

# gh `--notes-file` content was copied to $work/_artifacts/gh-notes.txt
# by the fake-gh script when the release create invocation ran.
cp "$work/_artifacts/gh-notes.txt" "$work/_artifacts/body-from-gh.txt" 2>/dev/null || true

# Byte-equality check: 1 if all three files exist AND match; 0 otherwise.
equal=0
if [ -f "$work/_artifacts/body-from-commit.txt" ] \
   && [ -f "$work/_artifacts/body-from-tag.txt" ] \
   && [ -f "$work/_artifacts/body-from-gh.txt" ]; then
  if diff -q "$work/_artifacts/body-from-commit.txt" "$work/_artifacts/body-from-tag.txt" >/dev/null \
     && diff -q "$work/_artifacts/body-from-commit.txt" "$work/_artifacts/body-from-gh.txt" >/dev/null; then
    equal=1
  fi
fi
echo "$equal" > "$work/_artifacts/body-equal.txt"
exit "$rc"
'''
    )


def _run_commit_msg_hook(release_args: str) -> str:
    """run.sh body for commit-msg-hook-passes: enables core.hooksPath
    BEFORE running the release. The release commit then exercises the
    real commit-msg hook; if the hook refuses, the commit fails and the
    release script propagates the error. Successful release means the
    hook accepted the message."""
    return _RUN_HEADER + (
        # Activate the hook (the in-tree mode is 644 pre-Gate-E, so we
        # explicitly chmod +x and set core.hooksPath).
        'chmod +x .githooks/commit-msg .githooks/pre-commit\n'
        'git config core.hooksPath .githooks\n'
        f'python3 bin/cctally release {release_args} '
        f'> "$work/_artifacts/stdout.txt" 2> "$work/_artifacts/stderr.txt"\n'
        f'rc=$?\n'
        f'echo "$rc" > "$work/_artifacts/exit.txt"\n'
        + _CAPTURE_ARTIFACTS
        + 'exit "$rc"\n'
    )


# ---------------------------------------------------------------------------
# Mock infrastructure for npm + brew tap (Batch 5 / Task 20).
#
# Used by Batch 6 (Phase 5 npm-publish scenarios) and Batches 7–8 (Phase 6
# brew helpers + scenarios). These helpers emit:
#
#   _emit_fake_npm        : a fake `npm` shell script that consumes canned
#                            JSON responses from a per-scenario state file
#                            and logs every invocation to a per-scenario
#                            log file. Goldens can assert on the log shape.
#   _emit_fake_brew_tap   : a bare repo + working clone serving as a
#                            stand-in for the homebrew-cctally tap remote.
#
# The shell-side analogs (fixture_init_fake_npm_path, fixture_seed_package_json)
# live in bin/_lib-fixture-harness.sh — duplication is intentional, since
# setup.sh runs at scenario-execution time, not builder time.
# ---------------------------------------------------------------------------


def _emit_fake_npm(scratch_path_dir: pathlib.Path) -> None:
    """Write a fake `npm` shell script under `scratch_path_dir/npm`.

    Reads canned responses from `<scratch>/npm-mock-state.json`:
        {
          "whoami": {"exit": 0, "stdout": "fake-user"},
          "view":   {"exit": 0, "stdout": "\"https://registry.npmjs.org/cctally/-/cctally-1.0.1.tgz\""},
          "publish":{"exit": 0, "stdout": "+ cctally@1.0.1"}
        }
    Logs every invocation to `<scratch>/npm-invocations.log` (one line:
    "<subcmd> <args...>") so goldens can assert on call shape.
    """
    scratch_path_dir.mkdir(parents=True, exist_ok=True)
    npm = scratch_path_dir / "npm"
    npm.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -uo pipefail
        STATE="${NPM_MOCK_STATE_FILE:-$(dirname "$0")/../npm-mock-state.json}"
        LOG="${NPM_MOCK_LOG_FILE:-$(dirname "$0")/../npm-invocations.log}"
        printf '%s\\n' "$*" >> "$LOG"
        SUB="$1"; shift || true
        case "$SUB" in
          whoami)  KEY=whoami ;;
          view)    KEY=view   ;;
          publish) KEY=publish ;;
          *)       echo "fake-npm: unknown subcommand: $SUB" >&2; exit 1 ;;
        esac
        EXIT=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get(sys.argv[2],{}).get('exit',0))" "$STATE" "$KEY")
        STDOUT=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get(sys.argv[2],{}).get('stdout',''))" "$STATE" "$KEY")
        if [ -n "$STDOUT" ]; then printf '%s\\n' "$STDOUT"; fi
        exit "$EXIT"
    """))
    npm.chmod(0o755)


def _emit_fake_brew_tap(work: pathlib.Path) -> pathlib.Path:
    """Initialize a bare repo + working clone under `work/` to serve
    as a fake brew tap remote.

    Returns the working-clone path. The bare repo is at
    `<work>/homebrew-cctally.git`; the clone is at `<work>/homebrew-cctally/`.
    Caller is responsible for setting `release.brewClone` git config on
    the cctally-dev fake-repo.
    """
    bare = work / "homebrew-cctally.git"
    clone = work / "homebrew-cctally"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    (clone / "Formula").mkdir()
    (clone / "README.md").write_text("Tap for github.com/omrikais/cctally.\n")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(clone), "-c", "user.email=fake@test.local",
         "-c", "user.name=Fake", "commit", "-q", "-m", "init"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "push", "-q", "origin", "HEAD"],
        check=True,
    )
    return clone


# ---------------------------------------------------------------------------
# Scenario assembly. SCENARIOS is a list of dicts; see module docstring
# for the schema.
# ---------------------------------------------------------------------------
SCENARIOS: list[dict] = []


def _add(**kwargs) -> None:
    """Register a scenario. Defaults extra_setup="" and optional
    goldens to None when missing."""
    kwargs.setdefault("extra_setup", "")
    for key in ("changelog", "commit_msg", "tag_annotation",
                "body_equal", "gh_argv", "package_json"):
        kwargs.setdefault(key, None)
    SCENARIOS.append(kwargs)


# ===========================================================================
# Group 1 — clean bumps (Task 11 baseline; preserved verbatim).
# ===========================================================================

# 1. clean-patch: prior v0.1.0 + 1-entry [Unreleased] → bump to v0.1.1.
_add(
    name="clean-patch",
    seed_changelog=_changelog(
        unreleased_subsections=[
            ("Added", ["- Demo entry for v0.1.1"]),
        ],
        prior_releases=[
            ("0.1.0", "2026-01-01", [("Added", ["- Initial public release of cctally"])]),
        ],
    ),
    run=_run_release("patch --no-publish"),
    expected_exit=0,
    stdout_substr="release: stamp ✓",
    stderr_substr="",
    changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("0.1.1", "2026-05-07", [("Added", ["- Demo entry for v0.1.1"])]),
            ("0.1.0", "2026-01-01", [("Added", ["- Initial public release of cctally"])]),
        ],
    ),
    commit_msg=(
        "chore(release): v0.1.1\n"
        "\n"
        "Stamp release v0.1.1 over 1 [Unreleased] entries.\n"
        "\n"
        "Run by `cctally release patch` from main at <SHA7>.\n"
        "Bump kind: patch.\n"
        "Subsections stamped: Added (1).\n"
        "\n"
        "--- public ---\n"
        "chore(release): v0.1.1\n"
        "\n"
        "### Added\n"
        "- Demo entry for v0.1.1\n"
        "\n"
    ),
    tag_annotation=(
        "v0.1.1\n"
        "\n"
        "### Added\n"
        "- Demo entry for v0.1.1\n"
        "\n"
    ),
)


# 2. clean-minor: prior v0.1.0 + multi-section [Unreleased] → bump to v0.2.0.
_add(
    name="clean-minor",
    seed_changelog=_changelog(
        unreleased_subsections=[
            ("Added", ["- New feature A", "- New feature B"]),
            ("Fixed", ["- Bug X"]),
        ],
        prior_releases=[
            ("0.1.0", "2026-01-01", [("Added", ["- Initial public release of cctally"])]),
        ],
    ),
    run=_run_release("minor --no-publish"),
    expected_exit=0,
    stdout_substr="release: stamp ✓",
    stderr_substr="",
    changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("0.2.0", "2026-05-07", [
                ("Added", ["- New feature A", "- New feature B"]),
                ("Fixed", ["- Bug X"]),
            ]),
            ("0.1.0", "2026-01-01", [("Added", ["- Initial public release of cctally"])]),
        ],
    ),
    commit_msg=(
        "chore(release): v0.2.0\n"
        "\n"
        "Stamp release v0.2.0 over 3 [Unreleased] entries.\n"
        "\n"
        "Run by `cctally release minor` from main at <SHA7>.\n"
        "Bump kind: minor.\n"
        "Subsections stamped: Added (2), Fixed (1).\n"
        "\n"
        "--- public ---\n"
        "chore(release): v0.2.0\n"
        "\n"
        "### Added\n"
        "- New feature A\n"
        "- New feature B\n"
        "\n"
        "### Fixed\n"
        "- Bug X\n"
        "\n"
    ),
    tag_annotation=(
        "v0.2.0\n"
        "\n"
        "### Added\n"
        "- New feature A\n"
        "- New feature B\n"
        "\n"
        "### Fixed\n"
        "- Bug X\n"
        "\n"
    ),
)


# 3. clean-major: prior v0.9.0 + 1-entry [Unreleased] → bump to v1.0.0.
_add(
    name="clean-major",
    seed_changelog=_changelog(
        unreleased_subsections=[
            ("Changed", ["- Breaking: API rewrite"]),
        ],
        prior_releases=[
            ("0.9.0", "2026-04-01", [("Added", ["- Pre-1.0 feature"])]),
        ],
    ),
    run=_run_release("major --no-publish"),
    expected_exit=0,
    stdout_substr="release: stamp ✓",
    stderr_substr="",
    changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("1.0.0", "2026-05-07", [("Changed", ["- Breaking: API rewrite"])]),
            ("0.9.0", "2026-04-01", [("Added", ["- Pre-1.0 feature"])]),
        ],
    ),
    commit_msg=(
        "chore(release): v1.0.0\n"
        "\n"
        "Stamp release v1.0.0 over 1 [Unreleased] entries.\n"
        "\n"
        "Run by `cctally release major` from main at <SHA7>.\n"
        "Bump kind: major.\n"
        "Subsections stamped: Changed (1).\n"
        "\n"
        "--- public ---\n"
        "chore(release): v1.0.0\n"
        "\n"
        "### Changed\n"
        "- Breaking: API rewrite\n"
        "\n"
    ),
    tag_annotation=(
        "v1.0.0\n"
        "\n"
        "### Changed\n"
        "- Breaking: API rewrite\n"
        "\n"
    ),
)


# 3b. stamp-package-json-and-changelog: seed a sentinel package.json
# alongside CHANGELOG, run `release patch`, verify Phase 1 co-stamps both
# files in the same commit. Pins both post-stamp goldens byte-exactly.
#
# package.json is committed via extra_setup AFTER the seed commit (so the
# release-time clean-tree preflight passes) and pushed so up-to-date check
# passes. The release run then re-stamps `version: "0.0.0-managed-by-release"`
# to `0.1.1` and stages it in the same commit as CHANGELOG.md.
_add(
    name="stamp-package-json-and-changelog",
    seed_changelog=_changelog(
        unreleased_subsections=[
            ("Added", ["- Brand new feature"]),
        ],
        prior_releases=[
            ("0.1.0", "2026-01-01",
                [("Added", ["- Initial public release of cctally"])]),
        ],
    ),
    extra_setup=(
        # Seed package.json with the sentinel version. The release script
        # rewrites this `version` field during Phase 1.
        "cat > package.json <<'CCTALLY_PJSON_EOF'\n"
        "{\n"
        '  "name": "cctally",\n'
        '  "version": "0.0.0-managed-by-release",\n'
        '  "description": "test"\n'
        "}\n"
        "CCTALLY_PJSON_EOF\n"
        # Commit + push so the release preflight (clean tree, up-to-date
        # with origin) passes.
        "git add package.json\n"
        "git commit --no-verify -q -F - <<'CCTALLY_PJSON_MSG_EOF'\n"
        "chore: seed package.json\n"
        "\n"
        "Public-Skip: true\n"
        "CCTALLY_PJSON_MSG_EOF\n"
        "git push -q origin main\n"
    ),
    run=_run_release("patch --no-publish"),
    expected_exit=0,
    stdout_substr="release: stamp ✓",
    stderr_substr="",
    changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("0.1.1", "2026-05-07", [("Added", ["- Brand new feature"])]),
            ("0.1.0", "2026-01-01",
                [("Added", ["- Initial public release of cctally"])]),
        ],
    ),
    package_json=(
        "{\n"
        '  "name": "cctally",\n'
        '  "version": "0.1.1",\n'
        '  "description": "test"\n'
        "}\n"
    ),
)


# 4. dry-run.
_DRY_RUN_CHANGELOG = _changelog(
    unreleased_subsections=[
        ("Added", ["- Dry-run demo entry"]),
    ],
    prior_releases=[
        ("0.1.0", "2026-01-01", [("Added", ["- Initial release"])]),
    ],
)
_add(
    name="dry-run",
    seed_changelog=_DRY_RUN_CHANGELOG,
    run=_run_dry_run("minor"),
    expected_exit=0,
    stdout_substr="dry-run complete; no state mutated",
    stderr_substr="",
    changelog=_DRY_RUN_CHANGELOG,
)


# 5. empty-unreleased: header alone → exit 2 with clear message.
_add(
    name="empty-unreleased",
    seed_changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("0.1.0", "2026-01-01", [("Added", ["- Initial release"])]),
        ],
    ),
    run=_run_release("patch"),
    expected_exit=2,
    stdout_substr="",
    stderr_substr="[Unreleased] is empty; nothing to release",
    changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("0.1.0", "2026-01-01", [("Added", ["- Initial release"])]),
        ],
    ),
)


# ===========================================================================
# Group 2 — Prerelease cycle.
# ===========================================================================

# 6. prerelease-first: stable + `prerelease --bump minor` → 1.1.0-rc.1.
_PRE_FIRST_BODY = [
    ("Added", ["- New thing for the next minor"]),
]
_add(
    name="prerelease-first",
    seed_changelog=_changelog(
        unreleased_subsections=_PRE_FIRST_BODY,
        prior_releases=[
            ("1.0.0", "2026-04-01", [("Added", ["- Initial release"])]),
        ],
    ),
    run=_run_release("prerelease --bump minor --no-publish"),
    expected_exit=0,
    stdout_substr="release: stamp ✓",
    stderr_substr="",
    changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("1.1.0-rc.1", "2026-05-07", _PRE_FIRST_BODY),
            ("1.0.0", "2026-04-01", [("Added", ["- Initial release"])]),
        ],
    ),
    commit_msg=(
        "chore(release): v1.1.0-rc.1\n"
        "\n"
        "Stamp release v1.1.0-rc.1 over 1 [Unreleased] entries.\n"
        "\n"
        "Run by `cctally release prerelease` from main at <SHA7>.\n"
        "Bump kind: prerelease.\n"
        "Subsections stamped: Added (1).\n"
        "\n"
        "--- public ---\n"
        "chore(release): v1.1.0-rc.1\n"
        "\n"
        "### Added\n"
        "- New thing for the next minor\n"
        "\n"
    ),
    tag_annotation=(
        "v1.1.0-rc.1\n"
        "\n"
        "### Added\n"
        "- New thing for the next minor\n"
        "\n"
    ),
)


# 7. prerelease-cycle: 1.1.0-rc.1 + `prerelease` → 1.1.0-rc.2.
_PRE_CYCLE_BODY = [
    ("Added", ["- More work since rc.1"]),
]
_add(
    name="prerelease-cycle",
    seed_changelog=_changelog(
        unreleased_subsections=_PRE_CYCLE_BODY,
        prior_releases=[
            ("1.1.0-rc.1", "2026-04-15", [("Added", ["- First rc"])]),
            ("1.0.0", "2026-04-01", [("Added", ["- Initial release"])]),
        ],
    ),
    run=_run_release("prerelease --no-publish"),
    expected_exit=0,
    stdout_substr="release: stamp ✓",
    stderr_substr="",
    changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("1.1.0-rc.2", "2026-05-07", _PRE_CYCLE_BODY),
            ("1.1.0-rc.1", "2026-04-15", [("Added", ["- First rc"])]),
            ("1.0.0", "2026-04-01", [("Added", ["- Initial release"])]),
        ],
    ),
)


# 8. finalize: 1.1.0-rc.2 + `finalize` → 1.1.0.
_FINALIZE_BODY = [
    ("Added", ["- Final polish"]),
]
_add(
    name="finalize",
    seed_changelog=_changelog(
        unreleased_subsections=_FINALIZE_BODY,
        prior_releases=[
            ("1.1.0-rc.2", "2026-04-30", [("Added", ["- rc.2 work"])]),
            ("1.1.0-rc.1", "2026-04-15", [("Added", ["- First rc"])]),
            ("1.0.0", "2026-04-01", [("Added", ["- Initial release"])]),
        ],
    ),
    run=_run_release("finalize --no-publish"),
    expected_exit=0,
    stdout_substr="release: stamp ✓",
    stderr_substr="",
    changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("1.1.0", "2026-05-07", _FINALIZE_BODY),
            ("1.1.0-rc.2", "2026-04-30", [("Added", ["- rc.2 work"])]),
            ("1.1.0-rc.1", "2026-04-15", [("Added", ["- First rc"])]),
            ("1.0.0", "2026-04-01", [("Added", ["- Initial release"])]),
        ],
    ),
)


# 9. prerelease-required-bump-error: stable + `prerelease` (no bump) → exit 2.
_add(
    name="prerelease-required-bump-error",
    seed_changelog=_changelog(
        unreleased_subsections=[
            ("Added", ["- Some entry"]),
        ],
        prior_releases=[
            ("1.0.0", "2026-04-01", [("Added", ["- Initial release"])]),
        ],
    ),
    run=_run_release("prerelease --no-publish"),
    expected_exit=2,
    stdout_substr="",
    stderr_substr="--bump required",
)


# 10. bump-on-prerelease-error: 1.1.0-rc.1 + `patch` → exit 2 with finalize hint.
_add(
    name="bump-on-prerelease-error",
    seed_changelog=_changelog(
        unreleased_subsections=[
            ("Added", ["- Some entry"]),
        ],
        prior_releases=[
            ("1.1.0-rc.1", "2026-04-15", [("Added", ["- First rc"])]),
            ("1.0.0", "2026-04-01", [("Added", ["- Initial release"])]),
        ],
    ),
    run=_run_release("patch --no-publish"),
    expected_exit=2,
    stdout_substr="",
    # Helper-message wording (line 162 of bin/cctally) — the prefix is
    # stable; we substring-match a fragment that includes the
    # `cctally release finalize` hint.
    stderr_substr="cctally release finalize",
)


# 11. prerelease-bump-on-prerelease-error: 1.1.0-rc.1 + `prerelease --bump
# minor` → exit 2.
_add(
    name="prerelease-bump-on-prerelease-error",
    seed_changelog=_changelog(
        unreleased_subsections=[
            ("Added", ["- Some entry"]),
        ],
        prior_releases=[
            ("1.1.0-rc.1", "2026-04-15", [("Added", ["- First rc"])]),
            ("1.0.0", "2026-04-01", [("Added", ["- Initial release"])]),
        ],
    ),
    run=_run_release("prerelease --bump minor --no-publish"),
    expected_exit=2,
    stdout_substr="",
    stderr_substr="--bump invalid when current version is a prerelease",
)


# 12. finalize-on-stable-error (spec scenario 24): stable + `finalize` → exit 2.
_add(
    name="finalize-on-stable-error",
    seed_changelog=_changelog(
        unreleased_subsections=[
            ("Added", ["- Some entry"]),
        ],
        prior_releases=[
            ("1.0.0", "2026-04-01", [("Added", ["- Initial release"])]),
        ],
    ),
    run=_run_release("finalize --no-publish"),
    expected_exit=2,
    stdout_substr="",
    stderr_substr="not a prerelease",
)


# ===========================================================================
# Group 3 — Preflight refusals.
# ===========================================================================

_PATCH_SEED = _changelog(
    unreleased_subsections=[
        ("Added", ["- Demo entry"]),
    ],
    prior_releases=[
        ("0.1.0", "2026-01-01", [("Added", ["- Initial release"])]),
    ],
)


# 13. dirty-tree-refuse: write a tracked-file change after seed; release refuses.
_add(
    name="dirty-tree-refuse",
    seed_changelog=_PATCH_SEED,
    extra_setup=(
        # Modify CHANGELOG.md without committing — leaves working tree dirty.
        'echo "stray edit" >> CHANGELOG.md\n'
    ),
    run=_run_release("patch --no-publish"),
    expected_exit=2,
    stdout_substr="",
    stderr_substr="working tree dirty",
)


# 14. wrong-branch-refuse: checkout feature/foo; default refusal fires.
_add(
    name="wrong-branch-refuse",
    seed_changelog=_PATCH_SEED,
    extra_setup=(
        'git checkout -q -b feature/foo\n'
    ),
    run=_run_release("patch --no-publish"),
    expected_exit=2,
    stdout_substr="",
    stderr_substr="refusing to cut from",
)


# 15. allow-branch: same as 14 + --allow-branch feature/foo → success.
_add(
    name="allow-branch",
    seed_changelog=_PATCH_SEED,
    extra_setup=(
        'git checkout -q -b feature/foo\n'
        # Push the branch so up-to-date preflight passes.
        'git push -q -u origin feature/foo\n'
    ),
    run=_run_release("patch --allow-branch feature/foo --no-publish"),
    expected_exit=0,
    stdout_substr="release: stamp ✓",
    stderr_substr="",
    changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("0.1.1", "2026-05-07", [("Added", ["- Demo entry"])]),
            ("0.1.0", "2026-01-01", [("Added", ["- Initial release"])]),
        ],
    ),
)


# 16. behind-remote-refuse: local is behind origin/main; release refuses.
# Strategy: seed without push, push a different commit to origin first
# (so origin diverges), then push our seed. Easier: after seed-and-push,
# add a NEW commit to origin via the public clone path, leaving the
# private's local main behind origin's main.
_BEHIND_SEED = _PATCH_SEED
_add(
    name="behind-remote-refuse",
    seed_changelog=_BEHIND_SEED,
    extra_setup=(
        # Advance origin/main by one commit using a sibling clone of
        # the private bare repo, then `git fetch` so the private working
        # clone learns origin is ahead. The `fetch` inside _release_
        # _preflight_up_to_date does this too, but we pre-stage it so
        # the test is robust.
        'git clone -q "$work/private.git" "$work/_advance"\n'
        'cd "$work/_advance"\n'
        'git config user.email "test@example.com"\n'
        'git config user.name "Test"\n'
        'git config commit.gpgsign false\n'
        'echo extra >> README\n'
        'git add README\n'
        # The skip-chain guard would fire on a publish-typed commit
        # without a `--- public ---` block; use Public-Skip to keep
        # the bare repo advance silent.
        "git commit --no-verify -q -F - <<'CCTALLY_EXTRA_MSG_EOF'\n"
        'chore: advance origin\n'
        '\n'
        'Public-Skip: true\n'
        'CCTALLY_EXTRA_MSG_EOF\n'
        'git push -q origin main\n'
        'cd "$work/private"\n'
    ),
    run=_run_release("patch --no-publish"),
    expected_exit=2,
    stdout_substr="",
    stderr_substr="is behind",
)


# 17. tag-already-exists-refuse: create local v0.1.1 tag; release refuses.
_add(
    name="tag-already-exists-refuse",
    seed_changelog=_PATCH_SEED,
    extra_setup=(
        'git -c tag.gpgsign=false tag v0.1.1 HEAD\n'
    ),
    run=_run_release("patch --no-publish"),
    expected_exit=2,
    stdout_substr="",
    stderr_substr="already exists locally; this would clobber",
)


# ===========================================================================
# Group 4 — Resume.
# ===========================================================================

# 18. resume-after-stamp-failure: phase 1 done (stamp commit landed),
# phase 2 NOT (no tag yet). `--resume` short-circuits phase 1 + runs phase 2.
# We simulate the partial state by running phase 1 manually via the
# stamp + commit path, then NOT running phase 2.
#
# Actual mechanism: invoke `cctally release patch --no-publish` which
# completes both phase 1 + 2, then DELETE the local tag + push of it.
# That leaves the stamp committed but with no v0.1.1 tag. `--resume`
# detects that and runs phase 2 again.
#
# Subtle: after re-running phase 2, --no-publish stops there. So the
# resume flow's stdout has both `stamp ✓ (already done)` AND the
# tag-creation output.
_add(
    name="resume-after-stamp-failure",
    seed_changelog=_PATCH_SEED,
    extra_setup=(
        # Run a complete stamp+tag so the CHANGELOG is stamped + tag exists,
        # then delete the local tag (and any remote ref) so phase 2 is
        # "not done" again. Use --no-publish to keep phases 3-4 out.
        'CCTALLY_RELEASE_DATE_UTC=2026-05-07 GIT_AUTHOR_DATE="2026-05-07T00:00:00+0000" '
        'GIT_COMMITTER_DATE="2026-05-07T00:00:00+0000" '
        'python3 bin/cctally release patch --no-publish '
        '> "$work/_partial.stdout" 2> "$work/_partial.stderr"\n'
        # Drop the tag locally and remotely; resume should recreate + push.
        'git tag -d v0.1.1 >/dev/null\n'
        'git push -q --delete origin v0.1.1 || true\n'
    ),
    run=_run_release("--resume --no-publish"),
    expected_exit=0,
    # `stamp ✓ (already done` appears for the resume path; tag creation
    # then runs and emits `tag ✓ (annotated, pushed to origin)`.
    stdout_substr="stamp ✓ (already done",
    stderr_substr="",
)


# 19. resume-after-tag-pushed: stamp + tag already done; mirror not. `--resume`
# short-circuits phase 1+2 + runs phase 3+4. We simulate the partial state
# by running stamp+tag via --no-publish, then re-running with --resume
# (without --no-publish) so phases 3 + 4 also run.
_add(
    name="resume-after-tag-pushed",
    seed_changelog=_PATCH_SEED,
    bootstrap_public=True,
    extra_setup=(
        'CCTALLY_RELEASE_DATE_UTC=2026-05-07 GIT_AUTHOR_DATE="2026-05-07T00:00:00+0000" '
        'GIT_COMMITTER_DATE="2026-05-07T00:00:00+0000" '
        'python3 bin/cctally release patch --no-publish '
        '> "$work/_partial.stdout" 2> "$work/_partial.stderr"\n'
    ),
    run=_run_release("--resume"),
    expected_exit=0,
    stdout_substr="tag ✓ (already done",
    stderr_substr="",
)


# 20. resume-already-complete: full release done; `--resume` exits 0.
# Strategy: pre-run the entire 4-phase release, then make the fake-gh
# `release view` return 0 so phase 4 is detected as "done", then re-run
# `cctally release --resume` and assert "already published".
_add(
    name="resume-already-complete",
    seed_changelog=_PATCH_SEED,
    bootstrap_public=True,
    extra_setup=(
        # Full release run (phases 1-4). gh probe returns 0 for create
        # but 1 for view (initial state) — the create succeeds.
        'CCTALLY_RELEASE_DATE_UTC=2026-05-07 GIT_AUTHOR_DATE="2026-05-07T00:00:00+0000" '
        'GIT_COMMITTER_DATE="2026-05-07T00:00:00+0000" '
        'PATH="$work/fake-bin:$PATH" GH_ARGV_LOG="$work/gh-argv.log.partial" '
        'GH_NOTES_DEST="$work/gh-notes.partial.txt" '
        'python3 bin/cctally release patch '
        '> "$work/_partial.stdout" 2> "$work/_partial.stderr"\n'
        # After the run, cause gh release view to return 0 — i.e.,
        # "release already exists" — for the subsequent --resume probe.
        'export FAKE_GH_RELEASE_VIEW_EXIT_AFTER=0\n'
        # Replace the fake-gh script in place with one that returns 0
        # for `release view` (release already exists).
        "cat > \"$work/fake-bin/gh\" <<'CCTALLY_FAKE_GH_DONE_EOF'\n"
        '#!/usr/bin/env bash\n'
        'echo "$@" >> "${GH_ARGV_LOG:-/dev/null}"\n'
        'case "$1" in\n'
        '  auth) exit 0 ;;\n'
        '  api)  exit 0 ;;\n'
        '  release)\n'
        '    case "$2" in\n'
        '      view) exit 0 ;;\n'
        '      *) exit 0 ;;\n'
        '    esac\n'
        '    ;;\n'
        'esac\n'
        'exit 0\n'
        'CCTALLY_FAKE_GH_DONE_EOF\n'
        'chmod +x "$work/fake-bin/gh"\n'
    ),
    run=_run_release("--resume"),
    expected_exit=0,
    stdout_substr="already published",
    stderr_substr="",
)


# ===========================================================================
# Group 5 — gh + body invariant.
# ===========================================================================

# 21. gh-auth-fallback: fake gh returns 1 on auth status; release exits 0
# with fallback hint printed.
_add(
    name="gh-auth-fallback",
    seed_changelog=_PATCH_SEED,
    bootstrap_public=True,
    extra_setup=(
        # Replace fake-gh with one that returns 1 on `auth status`.
        "cat > \"$work/fake-bin/gh\" <<'CCTALLY_FAKE_GH_NOAUTH_EOF'\n"
        '#!/usr/bin/env bash\n'
        'echo "$@" >> "${GH_ARGV_LOG:-/dev/null}"\n'
        'case "$1" in\n'
        '  auth) exit 1 ;;\n'
        '  api)  exit 1 ;;\n'
        '  release)\n'
        '    case "$2" in\n'
        '      view) exit 1 ;;\n'
        '      *) exit 0 ;;\n'
        '    esac\n'
        '    ;;\n'
        'esac\n'
        'exit 0\n'
        'CCTALLY_FAKE_GH_NOAUTH_EOF\n'
        'chmod +x "$work/fake-bin/gh"\n'
    ),
    # No --no-publish — exercise phase 4.
    run=_run_release("patch"),
    expected_exit=0,
    stdout_substr="release: gh release ⚠ skipped",
    stderr_substr="",
)


# 22. body-canonical-three-sources: full clean release; assert byte-identical
# body across stamp commit's public block, tag annotation, and gh `--notes-file`.
_add(
    name="body-canonical-three-sources",
    bootstrap_public=True,
    seed_changelog=_changelog(
        unreleased_subsections=[
            ("Added", ["- Body-canonical demo entry"]),
            ("Fixed", ["- Bug Y"]),
        ],
        prior_releases=[
            ("0.1.0", "2026-01-01", [("Added", ["- Initial release"])]),
        ],
    ),
    run=_run_body_canonical("patch"),
    expected_exit=0,
    # Both phases land; gh release create reports the URL.
    stdout_substr="release: gh release ✓",
    stderr_substr="",
    body_equal="1\n",
)


# 23. commit-msg-hook-passes: enable the real commit-msg hook then run a
# clean release; the hook accepts the stamp commit, release succeeds.
_add(
    name="commit-msg-hook-passes",
    seed_changelog=_PATCH_SEED,
    run=_run_commit_msg_hook("patch --no-publish"),
    expected_exit=0,
    stdout_substr="release: stamp ✓",
    stderr_substr="",
)


# ===========================================================================
# Group 6 — Skip-chain interaction + public-clone discovery.
# ===========================================================================

# 24. skip-chain-no-refuse-on-release: 16 Public-Skip commits then a
# release stamp; the mirror tool's skip-chain refuse path does NOT fire
# because `chore(release):` is exempt.
def _build_skip_chain_setup() -> str:
    """Inject 16 commits each carrying `Public-Skip: true` BEFORE the
    seed commit's mirror-cursor advances. The release commit on top
    inherits the chain, but RELEASE_SUBJECT_RE exempts it."""
    chain = []
    for i in range(16):
        chain.append(
            f'echo "skip-{i}" > skip-file-{i}.txt\n'
            f'git add skip-file-{i}.txt\n'
            f"git commit --no-verify -q -F - <<'CCTALLY_SKIP_MSG_{i}_EOF'\n"
            f'chore: skip commit {i}\n'
            f'\n'
            f'Public-Skip: true\n'
            f'CCTALLY_SKIP_MSG_{i}_EOF\n'
        )
    return (
        # Move the mirror-cursor BACK so the chain is "before publish";
        # actually the seed bash already tags mirror-cursor at HEAD. After
        # the chain commits land, the mirror-cursor still points at the
        # seed commit, so `mirror-cursor..HEAD` enumerates all 16 skip
        # commits PLUS the eventual release commit.
        ''.join(chain) +
        'git push -q origin main --follow-tags\n'
    )


_SKIP_CHAIN_SEED = _changelog(
    unreleased_subsections=[
        ("Added", ["- Skip-chain demo entry"]),
    ],
    prior_releases=[
        ("0.1.0", "2026-01-01", [("Added", ["- Initial release"])]),
    ],
)
_add(
    name="skip-chain-no-refuse-on-release",
    seed_changelog=_SKIP_CHAIN_SEED,
    bootstrap_public=True,
    extra_setup=_build_skip_chain_setup(),
    # Full publish path so phase 3 invokes the mirror tool, which is
    # what enforces the skip-chain refuse.
    run=_run_release("patch"),
    expected_exit=0,
    # Successful mirror push proves the skip-chain refuse did NOT fire.
    stdout_substr="release: mirror ✓",
    stderr_substr="",
)


# 25. public-clone-not-discoverable: unset all three discovery sources;
# `cctally release patch` (with publish) fails phase 3 with discovery msg.
_add(
    name="public-clone-not-discoverable",
    seed_changelog=_PATCH_SEED,
    extra_setup=(
        # Unset the git config wired by the scaffold. APP_DIR (~/.local/
        # share/cctally/release-public-clone-path) marker is never set in
        # the harness scratch tree, so the only remaining source is the
        # config we just unset.
        'git config --unset release.publicClone\n'
        # Point HOME at the scratch dir so APP_DIR resolves into a
        # non-existing path (no marker file).
        'export HOME="$work/_fakehome"\n'
        'mkdir -p "$work/_fakehome"\n'
    ),
    # Run.sh must export HOME=$work/_fakehome too, so the discovery's
    # APP_DIR check sees an empty marker dir. We override HOME inline.
    run=_RUN_HEADER + (
        'export HOME="$work/_fakehome"\n'
        'python3 bin/cctally release patch '
        '> "$work/_artifacts/stdout.txt" 2> "$work/_artifacts/stderr.txt"\n'
        'rc=$?\n'
        'echo "$rc" > "$work/_artifacts/exit.txt"\n'
        + _CAPTURE_ARTIFACTS
        + 'exit "$rc"\n'
    ),
    expected_exit=2,
    stdout_substr="",
    stderr_substr="cannot discover public clone path",
)


# 26. public-clone-tag-already-on-public: public origin already has v0.1.1.
# Phase 3's signal-check (`_release_phase_mirror_done`) returns True; the
# mirror push short-circuits as already-done.
def _build_public_tag_seed() -> str:
    """Pre-create a v0.1.1 tag on the public bare repo so phase 3
    detects mirror-already-done."""
    return (
        # Push a v0.1.1 tag to the public bare. Use a side clone of the
        # public bare so we don't pollute $work/public.
        'git clone -q "$work/public.git" "$work/_pub_advance"\n'
        'cd "$work/_pub_advance"\n'
        'git config user.email "test@example.com"\n'
        'git config user.name "Test"\n'
        'git config commit.gpgsign false\n'
        'git config tag.gpgsign false\n'
        'git -c tag.gpgsign=false tag -a -m "pre-existing v0.1.1" v0.1.1 HEAD\n'
        'git push -q origin v0.1.1\n'
        'cd "$work/private"\n'
    )


_add(
    name="public-clone-tag-already-on-public",
    seed_changelog=_PATCH_SEED,
    bootstrap_public=True,
    extra_setup=_build_public_tag_seed(),
    run=_run_release("patch"),
    # Phase 3 detects "already done" and short-circuits; phase 4 then
    # runs `gh release view` (returns 1 — release doesn't exist yet),
    # auth probe succeeds, `release create` runs and returns 0, full
    # release reports "published".
    expected_exit=0,
    stdout_substr="release: mirror ✓ (already done",
    stderr_substr="",
)


# ---------------------------------------------------------------------------
# Build.
# ---------------------------------------------------------------------------
def _assemble_setup(scenario: dict) -> str:
    """Concatenate the scaffold + per-scenario CHANGELOG seed + extra setup."""
    seed = _seed_changelog_and_commit(
        scenario["seed_changelog"],
        push=scenario.get("push", True),
        bootstrap_public=scenario.get("bootstrap_public", False),
    )
    return _SCAFFOLD + seed + scenario.get("extra_setup", "")


def build(out_root: Path) -> None:
    """Materialize all scenarios under out_root.

    Goldens land directly in tests/fixtures/release/<scenario>/ so the
    in-tree golden files survive harness runs (the harness's scratch
    dir is per-run; goldens must be committed-in-tree).
    """
    out_root.mkdir(parents=True, exist_ok=True)
    for sc in SCENARIOS:
        d = out_root / sc["name"]
        d.mkdir(parents=True, exist_ok=True)
        # setup.sh — scaffold + CHANGELOG seed + extra_setup.
        (d / "setup.sh").write_text(_assemble_setup(sc), encoding="utf-8")
        (d / "setup.sh").chmod(0o755)
        # run.sh — scenario-specific invocation.
        (d / "run.sh").write_text(sc["run"], encoding="utf-8")
        (d / "run.sh").chmod(0o755)
        # Goldens.
        (d / "golden-exit.txt").write_text(
            f"{sc['expected_exit']}\n", encoding="utf-8")
        (d / "golden-stdout-substr.txt").write_text(
            sc["stdout_substr"] + "\n", encoding="utf-8")
        (d / "golden-stderr-substr.txt").write_text(
            sc["stderr_substr"] + "\n", encoding="utf-8")
        # Optional goldens — emit only when present so the harness's
        # `[ -f ... ]` gate works.
        for key, fname in (
            ("changelog", "golden-changelog.md"),
            ("commit_msg", "golden-commit-msg.txt"),
            ("tag_annotation", "golden-tag-annotation.txt"),
            ("body_equal", "golden-body-equal.txt"),
            ("gh_argv", "golden-gh-argv.txt"),
            ("package_json", "golden-package-json.json"),
        ):
            p = d / fname
            value = sc.get(key)
            if value is None:
                if p.exists():
                    p.unlink()
            else:
                p.write_text(value, encoding="utf-8")
        # Per-fixture .gitignore.
        gi = d / ".gitignore"
        if not gi.exists():
            gi.write_text("_artifacts/\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(FIXTURES_DIR))
    args = p.parse_args()
    build(Path(args.out))
    print(f"release fixtures: built {len(SCENARIOS)} scenarios → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
