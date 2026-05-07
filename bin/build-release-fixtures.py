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

Why a `--no-publish` posture for the three clean-* scenarios:
  cmd_release Phase 3 (`_release_run_phase_mirror`) invokes
  `cctally-mirror-public --yes <public-clone>` with the public clone as
  a positional argument, but the mirror tool defines `--public-clone` as
  a named flag (no positional). The first-pass invocation therefore
  fails with `unrecognized arguments`. Fixing that mis-invocation lives
  in `bin/cctally`, which is out of scope for the harness implementor.
  Until that lands, scenarios 1-3 use `--no-publish` so they exercise
  Phases 1-2 (stamp + tag + commit message + tag annotation) without
  tripping the broken Phase 3. Task 12 adds the mirror+gh scenarios
  alongside the bug fix.

Determinism env hooks:
  - CCTALLY_RELEASE_DATE_UTC=2026-05-07 — pins the stamped date.
  - GIT_AUTHOR_*/GIT_COMMITTER_* set in setup.sh — stable identity.
"""

from __future__ import annotations

import argparse
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
#     (the "public mirror" — release Phase 3 would push here).
#   - Copies bin/cctally + bin/cctally-mirror-public + .mirror-allowlist
#     + .githooks/ into private/ so __file__-relative path resolution
#     (CHANGELOG_PATH, mirror_tool location) lands in private/, not in
#     cctally-dev itself.
#   - Seeds CHANGELOG.md per-scenario (added below the scaffold).
#   - Sets `release.publicClone` git config in private/ to $work/public/
#     so `_release_discover_public_clone` finds it (used by phase 3
#     scenarios; harmless when --no-publish skips phase 3).
#   - Provides a fake `gh` binary in $work/fake-bin/ that records its
#     argv to $work/gh-argv.log and exits 0 for the auth probes. The
#     harness's run.sh prepends $work/fake-bin to PATH; `cctally release`
#     thus sees a stable, side-effect-free `gh`. Scenarios that exercise
#     Phase 4 will assert on the recorded argv (Task 12).
#   - Ends with cwd = $work/private/.
# ---------------------------------------------------------------------------
_SCAFFOLD = '''#!/bin/bash
set -euo pipefail
work="$(pwd)"
REPO_ROOT="$1"

mkdir -p "$work/private" "$work/private.git" "$work/public" "$work/public.git" "$work/fake-bin"

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
# `release create` returns 0 (success).
cat > "$work/fake-bin/gh" <<'CCTALLY_FAKE_GH_EOF'
#!/usr/bin/env bash
echo "$@" >> "${GH_ARGV_LOG:-/dev/null}"
case "$1" in
  auth) exit 0 ;;
  api)  exit 0 ;;
  release)
    case "$2" in
      view) exit 1 ;;
      *) exit 0 ;;
    esac
    ;;
esac
exit 0
CCTALLY_FAKE_GH_EOF
chmod +x "$work/fake-bin/gh"

# Public bare + working clone. Init bare first; clone or wire origin so
# the public/ working dir's `origin` points at public.git/.
git init -q --bare --initial-branch=main "$work/public.git" 2>/dev/null \
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
git init -q --bare --initial-branch=main "$work/private.git" 2>/dev/null \
    || git init -q --bare "$work/private.git"
cd "$work/private"
git init -q --initial-branch=main 2>/dev/null || git init -q
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
git config tag.gpgsign false
git remote add origin "$work/private.git"
# Wire the public-clone discovery so cmd_release Phase 3 finds it.
git config release.publicClone "$work/public"

# Copy infrastructure files so cctally + the mirror tool resolve
# correctly via __file__.parent.parent inside this scratch repo.
mkdir -p .githooks bin
cp "$REPO_ROOT/.mirror-allowlist" .
cp "$REPO_ROOT/.githooks/_match.py" .githooks/
cp "$REPO_ROOT/.githooks/_public_trailer.py" .githooks/
cp "$REPO_ROOT/.githooks/_skip_chain_metrics.py" .githooks/
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


def _seed_changelog_and_commit(content: str) -> str:
    """Bash snippet (run with cwd=$work/private) that writes CHANGELOG.md
    and lands the seed commit, then pushes to origin. The seed commit is
    NOT a release — it contains the prior release header(s) already.
    A `--- public ---` block keeps the trailer-classifier happy if the
    mirror tool ever walks it.
    """
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
        # Tag mirror-cursor here so the mirror tool — if ever invoked
        # under --no-publish — would walk only post-seed commits.
        f'git -c tag.gpgsign=false tag mirror-cursor HEAD\n'
        f'git push -q origin main --follow-tags\n'
    )


# ---------------------------------------------------------------------------
# Run-sh helpers.
#
# `_run_release_no_publish` invokes the release script under --no-publish
# (Phases 1-2 only) and writes deterministic artifacts to $work/_artifacts/
# for the harness to compare against goldens. SHAs are normalized to
# `<SHA7>` since they are byte-non-deterministic across runs.
# ---------------------------------------------------------------------------

_RUN_HEADER = '''#!/bin/bash
set -uo pipefail
work="$(pwd)/.."
REPO_ROOT="$1"

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

# Per-scenario artifact dir; the harness reads files under it for the
# golden-* comparisons.
mkdir -p "$work/_artifacts"
'''


def _run_no_publish(version_kind: str) -> str:
    """run.sh body for clean-patch / clean-minor / clean-major — uses
    --no-publish to dodge the Phase 3 mirror-tool argv bug."""
    return _RUN_HEADER + (
        f'python3 bin/cctally release {version_kind} --no-publish '
        f'> "$work/_artifacts/stdout.txt" 2> "$work/_artifacts/stderr.txt"\n'
        f'rc=$?\n'
        f'echo "$rc" > "$work/_artifacts/exit.txt"\n'
        # Capture release artifacts even if rc != 0 — goldens lock in the
        # expected files; failures surface via golden-exit.txt mismatch.
        f'cp CHANGELOG.md "$work/_artifacts/changelog.md" 2>/dev/null || true\n'
        # Stamp commit message — strip SHAs so the golden is byte-stable.
        # Format: `prior_head[:7]` appears in the body. Replace any
        # 7-hex-digit run with `<SHA7>` after capturing.
        f'git log -1 --format=%B 2>/dev/null '
        f'| sed -E "s/[0-9a-f]{{7,40}}/<SHA7>/g" '
        f'> "$work/_artifacts/commit-msg.txt" || true\n'
        # Tag annotation — body of the release tag.
        f'tag_name=$(git tag --points-at HEAD | grep -E \'^v[0-9]\' | head -n1)\n'
        f'if [ -n "$tag_name" ]; then\n'
        f'  git tag -l --format="%(contents)" "$tag_name" '
        f'> "$work/_artifacts/tag-annotation.txt"\n'
        f'fi\n'
        # Pass through harness — exit 0 means "captured"; exit-code
        # validation is done by the harness against golden-exit.txt.
        f'exit "$rc"\n'
    )


def _run_dry_run(version_kind: str) -> str:
    """run.sh body for the dry-run scenario — no mutation, exit 0,
    captures stdout for diff-content match."""
    return _RUN_HEADER + (
        f'python3 bin/cctally release {version_kind} --dry-run '
        f'> "$work/_artifacts/stdout.txt" 2> "$work/_artifacts/stderr.txt"\n'
        f'rc=$?\n'
        f'echo "$rc" > "$work/_artifacts/exit.txt"\n'
        f'cp CHANGELOG.md "$work/_artifacts/changelog.md"\n'
        # Assert no stamp commit landed — HEAD must still point at the
        # seed commit (no new commit, no tag).
        f'tag_count=$(git tag -l | grep -E \'^v[0-9]\' | wc -l | tr -d " ")\n'
        f'if [ "$tag_count" != "0" ]; then\n'
        f'  echo "ASSERT_FAIL: dry-run created tags ($tag_count)" >&2\n'
        f'  exit 9\n'
        f'fi\n'
        f'exit "$rc"\n'
    )


def _run_empty_unreleased(version_kind: str) -> str:
    """run.sh body for empty-unreleased — stamp refuses; release exits 2."""
    return _RUN_HEADER + (
        f'python3 bin/cctally release {version_kind} '
        f'> "$work/_artifacts/stdout.txt" 2> "$work/_artifacts/stderr.txt"\n'
        f'rc=$?\n'
        f'echo "$rc" > "$work/_artifacts/exit.txt"\n'
        f'cp CHANGELOG.md "$work/_artifacts/changelog.md"\n'
        f'exit "$rc"\n'
    )


# ---------------------------------------------------------------------------
# Scenario assembly. Each scenario contributes:
#   - name              : fixture dir name
#   - body              : bash snippet appended to _SCAFFOLD (cwd=private/)
#   - run               : run.sh body
#   - expected_exit     : int
#   - stdout_substr     : str ('' = no check)
#   - stderr_substr     : str ('' = no check)
#   - changelog         : str | None — final CHANGELOG.md content; None = no check
#   - commit_msg        : str | None — stamp commit message (with <SHA7>); None = no check
#   - tag_annotation    : str | None — release tag annotation body; None = no check
# ---------------------------------------------------------------------------
SCENARIOS: list[dict] = []


# 1. clean-patch: prior v0.1.0 + 1-entry [Unreleased] → bump to v0.1.1.
SCENARIOS.append(dict(
    name="clean-patch",
    body=_seed_changelog_and_commit(_changelog(
        unreleased_subsections=[
            ("Added", ["- Demo entry for v0.1.1"]),
        ],
        prior_releases=[
            ("0.1.0", "2026-01-01", [("Added", ["- Initial public release of cctally"])]),
        ],
    )),
    run=_run_no_publish("patch"),
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
))


# 2. clean-minor: prior v0.1.0 + multi-section [Unreleased] → bump to v0.2.0.
SCENARIOS.append(dict(
    name="clean-minor",
    body=_seed_changelog_and_commit(_changelog(
        unreleased_subsections=[
            ("Added", ["- New feature A", "- New feature B"]),
            ("Fixed", ["- Bug X"]),
        ],
        prior_releases=[
            ("0.1.0", "2026-01-01", [("Added", ["- Initial public release of cctally"])]),
        ],
    )),
    run=_run_no_publish("minor"),
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
))


# 3. clean-major: prior v0.9.0 + 1-entry [Unreleased] → bump to v1.0.0.
SCENARIOS.append(dict(
    name="clean-major",
    body=_seed_changelog_and_commit(_changelog(
        unreleased_subsections=[
            ("Changed", ["- Breaking: API rewrite"]),
        ],
        prior_releases=[
            ("0.9.0", "2026-04-01", [("Added", ["- Pre-1.0 feature"])]),
        ],
    )),
    run=_run_no_publish("major"),
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
))


# 4. dry-run: prior v0.1.0 + 1-entry [Unreleased] + --dry-run → exit 0,
# no mutation; CHANGELOG.md unchanged.
_DRY_RUN_CHANGELOG = _changelog(
    unreleased_subsections=[
        ("Added", ["- Dry-run demo entry"]),
    ],
    prior_releases=[
        ("0.1.0", "2026-01-01", [("Added", ["- Initial release"])]),
    ],
)
SCENARIOS.append(dict(
    name="dry-run",
    body=_seed_changelog_and_commit(_DRY_RUN_CHANGELOG),
    run=_run_dry_run("minor"),
    expected_exit=0,
    # Pin both the dry-run banner AND the "no state mutated" footer so a
    # silent change to either is caught.
    stdout_substr="dry-run complete; no state mutated",
    stderr_substr="",
    # CHANGELOG must be byte-identical to the seeded content.
    changelog=_DRY_RUN_CHANGELOG,
    commit_msg=None,  # no stamp commit lands on dry-run
    tag_annotation=None,  # no tag lands on dry-run
))


# 5. empty-unreleased: [Unreleased] header present but empty → stamp
# refuses with exit 2 + clear stderr message.
SCENARIOS.append(dict(
    name="empty-unreleased",
    body=_seed_changelog_and_commit(_changelog(
        unreleased_subsections=None,  # header alone, no subsections
        prior_releases=[
            ("0.1.0", "2026-01-01", [("Added", ["- Initial release"])]),
        ],
    )),
    run=_run_empty_unreleased("patch"),
    # Phase 1's _release_stamp_changelog raises ValueError; the helper
    # surfaces a stack trace then exits non-zero. cmd_release does NOT
    # wrap the call in try/except (only the version-compute path does),
    # so the script bubbles up via uncaught exception → exit 1 from
    # python's default handler.
    #
    # Acceptance criterion 3 (spec §14) wants "exit 2 with clear
    # message", but the wiring in `_release_run_phase_stamp` lets the
    # ValueError propagate. The harness records what the code DOES do
    # today; if the code is later tightened to surface exit 2, this
    # golden updates accordingly. The substring check still pins the
    # error message ("[Unreleased] is empty; nothing to release").
    expected_exit=1,
    stdout_substr="",
    stderr_substr="[Unreleased] is empty; nothing to release",
    changelog=_changelog(
        unreleased_subsections=None,
        prior_releases=[
            ("0.1.0", "2026-01-01", [("Added", ["- Initial release"])]),
        ],
    ),
    commit_msg=None,
    tag_annotation=None,
))


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
        # setup.sh — scaffold + scenario body.
        (d / "setup.sh").write_text(_SCAFFOLD + sc["body"], encoding="utf-8")
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
        ):
            p = d / fname
            value = sc.get(key)
            if value is None:
                if p.exists():
                    p.unlink()
            else:
                p.write_text(value, encoding="utf-8")
        # Per-fixture .gitignore (covers any incidental scratch — though
        # the harness redirects all writes to a per-run mktemp, this
        # matches existing fixture-dir convention).
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
