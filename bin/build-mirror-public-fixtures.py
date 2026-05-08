#!/usr/bin/env python3
"""Build fixtures for bin/cctally-mirror-public-test.

Each scenario is a directory under tests/fixtures/mirror-public/ containing:
  - setup.sh                   : bash script that builds a tiny private +
                                 public git pair under $SCRATCH/<scenario>/,
                                 sets the mirror-cursor at the infra-bootstrap
                                 commit, and runs scenario-specific commits.
  - golden-exit.txt            : single-line expected exit code for the mirror
                                 invocation (e.g. `0\n`, `1\n`).
  - golden-stdout-substr.txt   : substring expected to appear in the mirror's
                                 stdout (e.g. `mirror plan:`); empty for
                                 silent scenarios.
  - golden-stderr-substr.txt   : optional — substring expected in stderr
                                 (refusal scenarios). Empty file = no
                                 stderr check.
  - golden-public-msg.txt      : optional — exact `git log -1 --format=%B`
                                 output expected on the public HEAD after
                                 the mirror runs. Missing = no check.

The harness invokes setup.sh, then runs `python3 $REPO_ROOT/bin/cctally-mirror-public
--public-clone ../public --yes` from inside private/, capturing exit/stdout/stderr.

End-to-end coverage of cmd_mirror's trailer-driven model:
  - happy paths (single-publish, publish-after-skip)
  - silent-skip cases (private-only, clean-merge)
  - refusal paths with stderr (mixed-commit, missing-trailer, both-surfaces)
  - tag propagation (propagated, held-back)
"""

from __future__ import annotations

import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "mirror-public"


# ---------------------------------------------------------------------------
# _SCAFFOLD: bash header that every setup.sh starts with.
#
# Convention (READ before editing):
#   - cwd at script entry is $work/  (the per-scenario scratch dir).
#   - Creates $work/private/ + $work/public/ as siblings.
#   - Inits both as separate git repos with stable test identity.
#   - Copies real .mirror-allowlist + .githooks/_match.py +
#     .githooks/_public_trailer.py into private/ (so the mirror tool walks
#     against the actual deployed allowlist + parser).
#   - Seeds public/ with one empty commit so `_propagate_tags` and the
#     public HEAD probe in the harness have something to read.
#   - Commits an "infra-bootstrap" commit on private/ containing the
#     allowlist + .githooks. These files are public-classified, BUT we
#     tag mirror-cursor at this commit so the mirror walks ONLY the
#     scenario-specific commits that follow — the infra files never get
#     replayed.
#   - Ends with cwd = $work/private/.
#
# Why every commit uses --no-verify: the scratch repos don't install
# core.hooksPath, but --no-verify is the safer default in case any
# parent-repo hook ever leaks through. Mixed-commit scenarios in
# particular need the bypass to land the seed commit at all.
# ---------------------------------------------------------------------------
_SCAFFOLD = '''#!/bin/bash
set -euo pipefail
work="$(pwd)"
REPO_ROOT="$1"

mkdir -p "$work/private" "$work/public"

# Public side: init + one empty commit so HEAD is a valid commit ref.
cd "$work/public"
git init -q --initial-branch=main 2>/dev/null || git init -q
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
git config tag.gpgsign false
git commit -q --allow-empty -m "init"

# Private side: init + identity + copy real infra files in.
cd "$work/private"
git init -q --initial-branch=main 2>/dev/null || git init -q
git config user.email "test@example.com"
git config user.name "Test"
git config commit.gpgsign false
git config tag.gpgsign false

mkdir -p .githooks bin
cp "$REPO_ROOT/.mirror-allowlist" .
cp "$REPO_ROOT/.githooks/_match.py" .githooks/
cp "$REPO_ROOT/.githooks/_public_trailer.py" .githooks/
cp "$REPO_ROOT/.githooks/_skip_chain_metrics.py" .githooks/
# The mirror tool resolves _REPO_ROOT via __file__.parent.parent at
# import time. Copy it INTO the scratch private's bin/ and run from
# there so it walks the scratch private repo (not cctally-dev itself).
cp "$REPO_ROOT/bin/cctally-mirror-public" bin/
chmod +x bin/cctally-mirror-public
# Optional: .public-tag-patterns drives tag propagation. Copy it in so
# the tag-propagated/tag-held-back scenarios match the live config.
cp "$REPO_ROOT/.public-tag-patterns" .

# Infra-bootstrap commit. Contents are public-classified, but we tag
# mirror-cursor here so the mirror walks ONLY scenario-specific commits
# that follow this point. The infra commit message itself carries a
# `--- public ---` block so it would be a valid publish if walked, but
# the mirror-cursor tag below ensures the mirror starts AFTER it.
git add -A
git commit --no-verify -q -F - <<'CCTALLY_INFRA_MSG_EOF'
chore: infra bootstrap

--- public ---
chore: infra bootstrap
CCTALLY_INFRA_MSG_EOF
git -c tag.gpgsign=false tag mirror-cursor HEAD

# cwd remains $work/private/ for scenario-specific bash that follows.
'''


# Helper: emit a `git commit --no-verify -F -` heredoc with a unique
# sentinel so commit-message bytes survive shell expansion verbatim.
def _commit_msg_heredoc(message: str, sentinel: str = "CCTALLY_MSG_EOF") -> str:
    return (
        f"git commit --no-verify -q -F - <<'{sentinel}'\n"
        f"{message}"
        f"{sentinel}\n"
    )


# ---------------------------------------------------------------------------
# Scenario assembly. Each scenario contributes:
#   - name              : fixture dir name
#   - body              : bash snippet appended to _SCAFFOLD (cwd=private/)
#   - expected_exit     : int
#   - stdout_substr     : str
#   - stderr_substr     : str ('' = no check)
#   - public_msg        : str | None (None = no check; '' = empty msg expected)
#   - run               : str | None
#       When None, the harness runs the default invocation:
#           python3 bin/cctally-mirror-public --public-clone ../public --yes
#       When a string, that bash body is written to run.sh and the harness
#       executes `bash ../run.sh "$REPO_ROOT"` from inside private/. Used by
#       the bootstrap-mode scenarios that need different flags + inline
#       pre/post assertions.
# ---------------------------------------------------------------------------
SCENARIOS: list[tuple[str, str, int, str, str, str | None, str | None]] = []


# 1. single-publish: one --- public --- commit produces one public commit.
SCENARIOS.append((
    "single-publish",
    'echo "x" > README.md\n'
    'git add README.md\n'
    + _commit_msg_heredoc(
        "fix: tweak readme privately\n"
        "\n"
        "--- public ---\n"
        "docs: refresh\n"
    ),
    0, "mirror plan:", "",
    "docs: refresh\n",
    None,
))


# 2. publish-after-skip: a Public-Skip commit followed by a publish commit.
# The skip's file changes accumulate naturally into the publish commit's
# diff (no separate handling — the mirror replays the publish commit's
# tree at that SHA, which already contains the prior skipped edits).
SCENARIOS.append((
    "publish-after-skip",
    'mkdir -p docs\n'
    'echo "draft v1" > docs/notes.md\n'
    'git add docs/notes.md\n'
    + _commit_msg_heredoc(
        "wip: drafting docs (will publish later)\n"
        "\n"
        "Public-Skip: true\n",
        sentinel="CCTALLY_MSG_EOF_A",
    )
    + 'echo "final v2" > docs/notes.md\n'
      'git add docs/notes.md\n'
    + _commit_msg_heredoc(
        "fix: finalize docs\n"
        "\n"
        "--- public ---\n"
        "docs: add notes\n"
        "\n"
        "First public version of the notes file.\n",
        sentinel="CCTALLY_MSG_EOF_B",
    ),
    0, "mirror plan:", "",
    "docs: add notes\n\nFirst public version of the notes file.\n",
    None,
))


# 2b. publish-after-skip-different-paths: regression for the "accumulate
# skipped public paths into the next publish" contract. The Public-Skip
# commit edits docs/skipped.md; the publish commit edits docs/published.md
# (different path). Without accumulation, docs/skipped.md silently never
# lands on the public side. run.sh asserts both files exist with their
# final contents; emits ASSERT_OK on success so the harness's stdout
# substring check can flag failure.
#
# This complements the existing publish-after-skip fixture (which uses
# the SAME path on both sides — a same-path skip-then-publish always
# accumulates implicitly because the publish SHA's tree already carries
# the skip's edits to that path).
SCENARIOS.append((
    "publish-after-skip-different-paths",
    'mkdir -p docs\n'
    'echo "skipped content v1" > docs/skipped.md\n'
    'git add docs/skipped.md\n'
    + _commit_msg_heredoc(
        "wip: skipped doc edit\n"
        "\n"
        "Public-Skip: true\n",
        sentinel="CCTALLY_MSG_EOF_A",
    )
    + 'echo "published content v1" > docs/published.md\n'
      'git add docs/published.md\n'
    + _commit_msg_heredoc(
        "fix: add published doc\n"
        "\n"
        "--- public ---\n"
        "docs: add published\n",
        sentinel="CCTALLY_MSG_EOF_B",
    ),
    0, "ASSERT_OK", "",
    "docs: add published\n",
    # run.sh: invoke the mirror, then assert both files landed publicly
    # with their expected contents. The harness wrapper writes
    # `set -uo pipefail` (no -e), so each assertion gates explicitly via
    # `|| { print marker; exit 2; }` and we only print ASSERT_OK after
    # all checks pass. The harness's stdout-substr check on ASSERT_OK
    # then fails any scenario where one of the assertions tripped.
    'python3 bin/cctally-mirror-public --public-clone ../public --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then\n'
    '  echo "ASSERT_FAIL: mirror exit=$rc"\n'
    '  exit "$rc"\n'
    'fi\n'
    'test -f ../public/docs/skipped.md || { echo "ASSERT_FAIL: skipped.md missing"; exit 2; }\n'
    'test -f ../public/docs/published.md || { echo "ASSERT_FAIL: published.md missing"; exit 2; }\n'
    'got_s=$(cat ../public/docs/skipped.md)\n'
    'got_p=$(cat ../public/docs/published.md)\n'
    '[ "$got_s" = "skipped content v1" ] || { echo "ASSERT_FAIL: skipped.md content=$got_s"; exit 2; }\n'
    '[ "$got_p" = "published content v1" ] || { echo "ASSERT_FAIL: published.md content=$got_p"; exit 2; }\n'
    'echo "ASSERT_OK"\n',
))


# 3. private-only-silent: commit touches only private files (.githooks/);
# the .mirror-allowlist puts .githooks/ as unmatched (no positive rule
# matches), so it's treated as private. Mirror silently skips.
SCENARIOS.append((
    "private-only-silent",
    'echo "# new helper" >> .githooks/_match.py\n'
    'git add .githooks/_match.py\n'
    + _commit_msg_heredoc("chore: tweak private match helper\n"),
    0, "no public commits to produce", "",
    None,
    None,
))


# 4. clean-merge-skipped: two trailer-bearing commits on different
# branches, then a clean --no-ff merge. Mirror should publish the two
# commits AND auto-skip the merge (no evil public content in the merge
# itself).
SCENARIOS.append((
    "clean-merge-skipped",
    'git checkout -q -b feature-a\n'
    'mkdir -p docs\n'
    'echo "alpha" > docs/alpha.md\n'
    'git add docs/alpha.md\n'
    + _commit_msg_heredoc(
        "fix: alpha\n"
        "\n"
        "--- public ---\n"
        "docs: add alpha\n",
        sentinel="CCTALLY_MSG_EOF_A",
    )
    + 'git checkout -q main\n'
      'git checkout -q -b feature-b\n'
      'mkdir -p docs\n'
      'echo "beta" > docs/beta.md\n'
      'git add docs/beta.md\n'
    + _commit_msg_heredoc(
        "fix: beta\n"
        "\n"
        "--- public ---\n"
        "docs: add beta\n",
        sentinel="CCTALLY_MSG_EOF_B",
    )
    + 'git checkout -q main\n'
      'git merge --no-ff --no-verify -q feature-a -m "Merge feature-a"\n'
      'git merge --no-ff --no-verify -q feature-b -m "Merge feature-b"\n',
    0, "mirror plan:", "",
    None,
    None,
))


# 5. mixed-commit-refused: commit touches both public (README.md) and
# private (.githooks/_match.py) paths; bypass pre-commit hook with
# --no-verify; mirror's diff-tree backstop refuses.
SCENARIOS.append((
    "mixed-commit-refused",
    'echo "mixed" > README.md\n'
    'echo "# mixed" >> .githooks/_match.py\n'
    'git add README.md .githooks/_match.py\n'
    + _commit_msg_heredoc(
        "fix: mixed bag\n"
        "\n"
        "--- public ---\n"
        "docs: refresh\n"
    ),
    1, "", "mixed commit",
    None,
    None,
))


# 6. missing-trailer-refused: trailerless commit touching public path.
# Parser returns kind=none; mirror sees public files and refuses with
# E_MISSING.
SCENARIOS.append((
    "missing-trailer-refused",
    'echo "y" > README.md\n'
    'git add README.md\n'
    + _commit_msg_heredoc("fix: tweak readme\n"),
    1, "", "no trailer",
    None,
    None,
))


# 7. tag-propagated: trailer-bearing commit + annotated v1.0.0 tag.
# Mirror publishes the commit AND propagates the tag (matches default
# .public-tag-patterns SemVer pattern).
SCENARIOS.append((
    "tag-propagated",
    'echo "v1" > README.md\n'
    'git add README.md\n'
    + _commit_msg_heredoc(
        "release: cut v1\n"
        "\n"
        "--- public ---\n"
        "release: cut v1\n",
    )
    + 'git tag -a v1.0.0 -m "v1.0.0"\n',
    0, "propagated tag: v1.0.0", "",
    None,
    None,
))


# 8. tag-held-back: Public-Skip commit + tag v1.0.0 on it. Mirror skips
# the commit AND emits a held-back warning (the tag's target commit was
# never published, so it can't propagate to anything).
SCENARIOS.append((
    "tag-held-back",
    'mkdir -p docs\n'
    'echo "draft" > docs/internal.md\n'
    'git add docs/internal.md\n'
    + _commit_msg_heredoc(
        "wip: internal draft\n"
        "\n"
        "Public-Skip: true\n",
    )
    + 'git tag -a v1.0.0 -m "v1.0.0"\n',
    0, "tag not propagated", "",
    None,
    None,
))


# 9. both-surfaces-refused: commit message has BOTH a `--- public ---`
# block AND a `Public-Skip: true` trailer. Parser refuses with E_BOTH;
# mirror surfaces the parser error code.
SCENARIOS.append((
    "both-surfaces-refused",
    'echo "z" > README.md\n'
    'git add README.md\n'
    + _commit_msg_heredoc(
        "fix: confused\n"
        "\n"
        "Public-Skip: true\n"
        "\n"
        "--- public ---\n"
        "docs: x\n"
    ),
    1, "", "E_BOTH",
    None,
    None,
))


# ---------------------------------------------------------------------------
# Bootstrap-mode scenarios (10–13).
#
# These exercise `cmd_bootstrap` (the `--bootstrap` flag-path), which
# replaces the default mirror invocation. The harness routes them through
# a per-scenario `run.sh` (see SCENARIOS schema above).
#
# Common conventions:
#   - Setup writes a bootstrap-message file at $work/msg/bootstrap.txt.
#     run.sh references it via `../msg/bootstrap.txt` (cwd=private/).
#   - Setup must produce at least one PUBLIC-classified file in private/
#     so `_match.classify(...)['public']` is non-empty (else cmd_bootstrap
#     refuses with "no public files matched"). README.md is allowlisted
#     (root *.md), so we use that.
#   - For "non-empty public" scenarios, the SCAFFOLD already created an
#     init commit on public; we add a SECOND commit and (for scenario 13)
#     a stale tag.
# ---------------------------------------------------------------------------

# Heredoc helper for run.sh files.
def _bs_msg_file_setup(content: str = "Initial public release of cctally\n") -> str:
    return (
        'mkdir -p ../msg\n'
        'cat > ../msg/bootstrap.txt <<\'CCTALLY_BS_MSG_EOF\'\n'
        f'{content}'
        'CCTALLY_BS_MSG_EOF\n'
    )


# Reusable: seed README.md + a public-touching commit on private/.
_SEED_PUBLIC_PRIVATE_COMMIT = (
    'echo "# Public README" > README.md\n'
    'git add README.md\n'
    + _commit_msg_heredoc(
        "fix: seed\n"
        "\n"
        "--- public ---\n"
        "docs: initial seed\n"
    )
)


# 10. bootstrap-empty-public — happy path.
#
# `cmd_bootstrap` rejects any public clone where `git rev-parse --verify HEAD`
# returns 0 (treats "init + empty commit" as non-empty). The SCAFFOLD seeds
# public with one init commit, so we re-init public/ here to genuinely empty
# state before invoking bootstrap. After: public has exactly one commit
# (the bootstrap commit) carrying the bootstrap-message-file content, plus
# an annotated tag v1.0.0 pointing at it.
SCENARIOS.append((
    "bootstrap-empty-public",
    _SEED_PUBLIC_PRIVATE_COMMIT
    + _bs_msg_file_setup()
    # Re-init public/ to a truly empty git repo (no HEAD commit) so the
    # bootstrap happy path is exercised without --force-bootstrap.
    + 'rm -rf ../public\n'
      'mkdir -p ../public\n'
      'git -C ../public init -q --initial-branch=main 2>/dev/null '
      '|| git -C ../public init -q\n'
      'git -C ../public config user.email "test@example.com"\n'
      'git -C ../public config user.name "Test"\n'
      'git -C ../public config commit.gpgsign false\n'
      'git -C ../public config tag.gpgsign false\n',
    0, "mirror: bootstrap done.", "",
    "Initial public release of cctally\n",
    # run.sh
    'python3 bin/cctally-mirror-public \\\n'
    '  --bootstrap \\\n'
    '  --bootstrap-message-file ../msg/bootstrap.txt \\\n'
    '  --bootstrap-tag v1.0.0 \\\n'
    '  --public-clone ../public \\\n'
    '  --yes\n',
))


# 11. bootstrap-non-empty-public-rejected — refusal without --force.
#
# Public has a non-init commit. cmd_bootstrap exits 2 with the
# "non-empty" rejection message on stderr.
SCENARIOS.append((
    "bootstrap-non-empty-public-rejected",
    # Add a second commit to public so it's clearly non-empty.
    '( cd ../public && \\\n'
    '  echo "junk" > existing.md && \\\n'
    '  git add existing.md && \\\n'
    '  git commit --no-verify -q -m "junk: pre-existing public commit" )\n'
    + _SEED_PUBLIC_PRIVATE_COMMIT
    + _bs_msg_file_setup(),
    2, "", "non-empty",
    None,
    'python3 bin/cctally-mirror-public \\\n'
    '  --bootstrap \\\n'
    '  --bootstrap-message-file ../msg/bootstrap.txt \\\n'
    '  --bootstrap-tag v1.0.0 \\\n'
    '  --public-clone ../public \\\n'
    '  --yes\n',
))


# 12. bootstrap-force-dry-run-no-mutation.
#
# `--bootstrap --force-bootstrap --dry-run` must NOT mutate the public
# clone. run.sh captures public HEAD before and after; a stdout marker
# `PUBLIC_HEAD_UNCHANGED` proves the dry-run guard short-circuits before
# the destructive wipe.
SCENARIOS.append((
    "bootstrap-force-dry-run-no-mutation",
    '( cd ../public && \\\n'
    '  echo "junk" > existing.md && \\\n'
    '  git add existing.md && \\\n'
    '  git commit --no-verify -q -m "junk: pre-existing public commit" )\n'
    + _SEED_PUBLIC_PRIVATE_COMMIT
    + _bs_msg_file_setup(),
    0, "PUBLIC_HEAD_UNCHANGED", "",
    None,
    'PUB_HEAD_BEFORE=$(git -C ../public rev-parse HEAD)\n'
    'python3 bin/cctally-mirror-public \\\n'
    '  --bootstrap \\\n'
    '  --force-bootstrap \\\n'
    '  --dry-run \\\n'
    '  --bootstrap-message-file ../msg/bootstrap.txt \\\n'
    '  --bootstrap-tag v1.0.0 \\\n'
    '  --public-clone ../public \\\n'
    '  --yes\n'
    'rc=$?\n'
    'PUB_HEAD_AFTER=$(git -C ../public rev-parse HEAD)\n'
    'if [ "$PUB_HEAD_BEFORE" = "$PUB_HEAD_AFTER" ]; then\n'
    '  echo "PUBLIC_HEAD_UNCHANGED"\n'
    'else\n'
    '  echo "PUBLIC_HEAD_MUTATED: was=$PUB_HEAD_BEFORE now=$PUB_HEAD_AFTER"\n'
    'fi\n'
    'exit $rc\n',
))


# 13. bootstrap-force-overwrites-tag.
#
# Public has a stale `v1.0.0` tag pointing at a junk commit. After
# `--bootstrap --force-bootstrap --bootstrap-tag v1.0.0`, the tag exists
# and points at the new bootstrap commit (NOT the prior one). run.sh
# captures both SHAs and prints `TAG_OVERWRITTEN` on inequality.
SCENARIOS.append((
    "bootstrap-force-overwrites-tag",
    '( cd ../public && \\\n'
    '  echo "junk" > existing.md && \\\n'
    '  git add existing.md && \\\n'
    '  git commit --no-verify -q -m "junk: pre-existing" && \\\n'
    '  git tag v1.0.0 )\n'
    + _SEED_PUBLIC_PRIVATE_COMMIT
    + _bs_msg_file_setup(),
    0, "TAG_OVERWRITTEN", "",
    None,
    'PUB_OLD_TAG_SHA=$(git -C ../public rev-parse v1.0.0)\n'
    'python3 bin/cctally-mirror-public \\\n'
    '  --bootstrap \\\n'
    '  --force-bootstrap \\\n'
    '  --bootstrap-message-file ../msg/bootstrap.txt \\\n'
    '  --bootstrap-tag v1.0.0 \\\n'
    '  --public-clone ../public \\\n'
    '  --yes\n'
    'rc=$?\n'
    'PUB_NEW_TAG_SHA=$(git -C ../public rev-parse v1.0.0)\n'
    'if [ "$PUB_OLD_TAG_SHA" != "$PUB_NEW_TAG_SHA" ]; then\n'
    '  echo "TAG_OVERWRITTEN"\n'
    'else\n'
    '  echo "TAG_NOT_OVERWRITTEN: $PUB_OLD_TAG_SHA"\n'
    'fi\n'
    'exit $rc\n',
))


# ---------------------------------------------------------------------------
# Round-3 regression scenarios (14–18).
#
# Five fixes from the post-merge review:
#   - Fix 5 (P1): refuse dirty public clone before mirror runs.
#   - Fix 1 (P2): defer cursor advance past unflushed skip-with-public-paths.
#   - Fix 2 (P2): only publish-kind commits count for tag propagation map.
#   - Fix 3 (P2): pre-apply refusal of empty publish (trailer + no public diff).
#   - Fix 4 (P2): preserve executable bit (and symlinks, via shared codepath).
# ---------------------------------------------------------------------------


# 14. dirty-public-clone-refused (Fix 5).
#
# Public clone has uncommitted changes; mirror exits 2 BEFORE walking
# any commits or touching the public tree. There's a private publish
# commit prepared in setup so the dirty-guard is the only thing keeping
# the mirror from running.
SCENARIOS.append((
    "dirty-public-clone-refused",
    'echo "x" > README.md\n'
    'git add README.md\n'
    + _commit_msg_heredoc(
        "fix: tweak readme privately\n"
        "\n"
        "--- public ---\n"
        "docs: refresh\n"
    )
    # Introduce an uncommitted edit on the public clone — appended to
    # the existing tracked file the SCAFFOLD seeded (the init commit was
    # `--allow-empty`, so we first add a tracked file then dirty it).
    + '( cd ../public && \\\n'
    '  echo "tracked content" > README.md && \\\n'
    '  git add README.md && \\\n'
    '  git commit --no-verify -q -m "seed tracked file" && \\\n'
    '  echo "uncommitted edit" >> README.md )\n',
    2, "", "uncommitted changes",
    None,
    None,
))


# 15. skip-only-then-publish-different-paths-multi-run (Fix 1).
#
# Verifies the cross-run accumulation contract:
#   - Run 1: a Public-Skip commit on docs/skipped.md is the only step.
#     Cursor MUST NOT advance past it (else its public paths are lost).
#   - After run 1: add a publish commit on docs/published.md (a DIFFERENT
#     path) and run mirror again.
#   - After run 2: BOTH files exist publicly with their final contents.
#
# The single-fixture run.sh chains both steps + assertions inline. The
# harness routes through run.sh; default mirror invocation is replaced
# by the chained `python3 bin/cctally-mirror-public ...` calls below.
SCENARIOS.append((
    "skip-only-then-publish-different-paths-multi-run",
    'mkdir -p docs\n'
    'echo "skipped content v1" > docs/skipped.md\n'
    'git add docs/skipped.md\n'
    + _commit_msg_heredoc(
        "wip: skipped doc edit\n"
        "\n"
        "Public-Skip: true\n",
        sentinel="CCTALLY_MSG_EOF_A",
    ),
    0, "ASSERT_OK", "",
    None,
    # run.sh: invoke mirror once (skip-only), capture cursor, then add
    # a publish commit on a different path and invoke mirror again.
    # Assert both files exist with expected content on the public side.
    'CURSOR_BEFORE=$(git rev-parse refs/tags/mirror-cursor)\n'
    'python3 bin/cctally-mirror-public --public-clone ../public --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run1 exit=$rc"; exit "$rc"; fi\n'
    'CURSOR_AFTER1=$(git rev-parse refs/tags/mirror-cursor)\n'
    # Cursor MUST NOT advance to the skip commit. Per Fix 1 it should
    # remain on the pre-run cursor (the infra-bootstrap commit) so the
    # skip replays alongside the next-run publish.
    'if [ "$CURSOR_BEFORE" != "$CURSOR_AFTER1" ]; then\n'
    '  echo "ASSERT_FAIL: cursor advanced after skip-only run was=$CURSOR_BEFORE now=$CURSOR_AFTER1"\n'
    '  exit 2\n'
    'fi\n'
    # Step 2: add a publish on a DIFFERENT path.
    'echo "published content v1" > docs/published.md\n'
    'git add docs/published.md\n'
    "git commit --no-verify -q -F - <<'CCTALLY_MSG_EOF_B'\n"
    "fix: add published doc\n"
    "\n"
    "--- public ---\n"
    "docs: add published\n"
    "CCTALLY_MSG_EOF_B\n"
    'python3 bin/cctally-mirror-public --public-clone ../public --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run2 exit=$rc"; exit "$rc"; fi\n'
    'test -f ../public/docs/skipped.md || { echo "ASSERT_FAIL: skipped.md missing"; exit 2; }\n'
    'test -f ../public/docs/published.md || { echo "ASSERT_FAIL: published.md missing"; exit 2; }\n'
    'got_s=$(cat ../public/docs/skipped.md)\n'
    'got_p=$(cat ../public/docs/published.md)\n'
    '[ "$got_s" = "skipped content v1" ] || { echo "ASSERT_FAIL: skipped.md content=$got_s"; exit 2; }\n'
    '[ "$got_p" = "published content v1" ] || { echo "ASSERT_FAIL: published.md content=$got_p"; exit 2; }\n'
    'echo "ASSERT_OK"\n',
))


# 16. tag-on-private-only-after-publish (Fix 2).
#
# Two-commit history: a publish on README.md, then a private-only commit
# touching .githooks/_match.py (unmatched/private). v1.2.3 is tagged on
# the PRIVATE-only commit. With the pre-fix `_build_priv_to_pub_map`,
# the private-only commit's public-classified-tree fingerprint matched
# the publish commit's (both saw README.md at the same blob); the tag
# would have been propagated to the wrong public commit.
#
# Post-fix: only publish-kind commits enter the map, so the private-only
# commit doesn't match anything → "tag not propagated" warning. Asserts
# the tag did NOT land on the public side.
SCENARIOS.append((
    "tag-on-private-only-after-publish",
    'echo "v1" > README.md\n'
    'git add README.md\n'
    + _commit_msg_heredoc(
        "fix: cut v1 publicly\n"
        "\n"
        "--- public ---\n"
        "release: cut v1\n",
        sentinel="CCTALLY_MSG_EOF_A",
    )
    + 'echo "# private tweak" >> .githooks/_match.py\n'
      'git add .githooks/_match.py\n'
    + _commit_msg_heredoc(
        "chore: tweak private match helper\n",
        sentinel="CCTALLY_MSG_EOF_B",
    )
    + 'git tag -a v1.2.3 -m "v1.2.3"\n',
    0, "ASSERT_OK", "",
    None,
    'python3 bin/cctally-mirror-public --public-clone ../public --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: mirror exit=$rc"; exit "$rc"; fi\n'
    # The tag must be held back, NOT propagated to the public side.
    'pub_tags=$(git -C ../public tag -l)\n'
    'echo "$pub_tags" | grep -qx "v1.2.3" '
    '&& { echo "ASSERT_FAIL: v1.2.3 wrongly propagated"; exit 2; }\n'
    'echo "ASSERT_OK"\n',
))


# 17. publish-trailer-on-private-only-rejected (Fix 3).
#
# A single private commit touches ONLY .githooks/_match.py (unmatched →
# treated as private; `_classify_commit_paths` returns it under
# "unmatched"). The message carries a valid `--- public ---` block. With
# no public_paths and no preceding Public-Skip paths to flush, the
# publish would land an empty diff publicly. Validator refuses pre-apply.
#
# Note: a single commit on an unmatched-only path makes `public_paths`
# AND `private_paths` (which holds explicit-negation paths) both empty,
# but `unmatched` is non-empty. The mixed-commit guard fires only when
# `public_paths` is non-empty AND the others have entries — this
# scenario hits the publish branch.
SCENARIOS.append((
    "publish-trailer-on-private-only-rejected",
    'echo "# helper tweak" >> .githooks/_match.py\n'
    'git add .githooks/_match.py\n'
    + _commit_msg_heredoc(
        "fix: tweak helper privately\n"
        "\n"
        "--- public ---\n"
        "docs: refresh\n"
    ),
    1, "", "no public file changes",
    None,
    None,
))


# 18. executable-bit-preserved (Fix 4).
#
# Add a NEW executable wrapper bin/cctally-newwrapper (mode 100755) in
# a publish commit. Without Fix 4, the file lands on the public side
# as 100644 and the wrapper no longer runs. Asserts the public-side
# blob's mode is 100755.
#
# Naming: bin/cctally-newwrapper matches `bin/cctally-*` (public) but
# does NOT match the negations for bin/cctally-mirror-public or
# bin/cctally-mirror-public-test, so it's classified as public.
SCENARIOS.append((
    "executable-bit-preserved",
    'mkdir -p bin\n'
    'cat > bin/cctally-newwrapper <<\'EOF\'\n'
    '#!/bin/bash\n'
    'echo "newwrapper $@"\n'
    'EOF\n'
    'chmod +x bin/cctally-newwrapper\n'
    'git add bin/cctally-newwrapper\n'
    + _commit_msg_heredoc(
        "fix: add new wrapper script\n"
        "\n"
        "--- public ---\n"
        "feat: add cctally-newwrapper\n"
    ),
    0, "ASSERT_OK", "",
    None,
    'python3 bin/cctally-mirror-public --public-clone ../public --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: mirror exit=$rc"; exit "$rc"; fi\n'
    'mode=$(git -C ../public ls-tree HEAD -- bin/cctally-newwrapper '
    '| awk \'{print $1}\')\n'
    '[ "$mode" = "100755" ] || { echo "ASSERT_FAIL: mode=$mode (expected 100755)"; exit 2; }\n'
    'echo "ASSERT_OK"\n',
))


# ---------------------------------------------------------------------------
# Round-4 regression scenarios (19–21).
#
# Three fixes from the post-merge round-2 review:
#   - Fix A (P2): reset on-disk mode to 0o644 when private mode drops to
#     100644 from 100755 — `pathlib.Path.write_bytes` preserves existing
#     POSIX permission bits, so an executable→regular flip would otherwise
#     leave the public-side file at 0o755.
#   - Fix B (P2): pass `-c tag.gpgsign=false` to both `git tag` invocations
#     in `_propagate_tags` so a global `tag.gpgsign=true` doesn't trigger
#     a signing prompt or fail when no signing key is configured.
#   - Fix C (P2): consume `pub_log` in chronological order with a forward
#     cursor in `_build_priv_to_pub_map` so reverts (whose tree matches an
#     earlier public commit) bind to their OWN public commit, not the
#     earlier one — preventing tags from propagating to the wrong commit.
# ---------------------------------------------------------------------------


# 19. mode-downgrade-100755-to-100644 (Fix A).
#
# Two-publish history: first publish lands a new executable wrapper
# bin/cctally-newwrapper at mode 100755; second publish drops the
# executable bit (chmod -x) but keeps the contents identical — a pure
# mode-only flip on the private side. Without Fix A, the public clone's
# on-disk file stays at 0o755 (write_bytes preserves perm bits), so
# `git add` records 100755 and the second public commit either lands at
# the wrong mode or fails with "nothing to commit" (rolling back).
# Asserts post-second-mirror that the public-side blob mode is 100644.
SCENARIOS.append((
    "mode-downgrade-100755-to-100644",
    'mkdir -p bin\n'
    'cat > bin/cctally-newwrapper <<\'EOF\'\n'
    '#!/bin/bash\n'
    'echo "newwrapper $@"\n'
    'EOF\n'
    'chmod +x bin/cctally-newwrapper\n'
    'git add bin/cctally-newwrapper\n'
    + _commit_msg_heredoc(
        "fix: add new wrapper script\n"
        "\n"
        "--- public ---\n"
        "feat: add cctally-newwrapper\n",
        sentinel="CCTALLY_MSG_EOF_A",
    ),
    0, "ASSERT_OK", "",
    None,
    # run.sh: mirror once (lands 100755), then chmod -x + commit (mode-
    # only change), mirror again, assert public-side mode is 100644.
    'python3 bin/cctally-mirror-public --public-clone ../public --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run1 exit=$rc"; exit "$rc"; fi\n'
    'mode1=$(git -C ../public ls-tree HEAD -- bin/cctally-newwrapper '
    '| awk \'{print $1}\')\n'
    '[ "$mode1" = "100755" ] || { echo "ASSERT_FAIL: run1 mode=$mode1 (expected 100755)"; exit 2; }\n'
    # Second commit: drop the executable bit. Use `git update-index --chmod`
    # so git records the mode change even though file contents are
    # identical (otherwise `git add` sees no diff at all).
    'chmod -x bin/cctally-newwrapper\n'
    'git update-index --chmod=-x bin/cctally-newwrapper\n'
    "git commit --no-verify -q -F - <<'CCTALLY_MSG_EOF_B'\n"
    "fix: drop executable bit\n"
    "\n"
    "--- public ---\n"
    "chore: drop executable bit on cctally-newwrapper\n"
    "CCTALLY_MSG_EOF_B\n"
    'python3 bin/cctally-mirror-public --public-clone ../public --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: run2 exit=$rc"; exit "$rc"; fi\n'
    'mode2=$(git -C ../public ls-tree HEAD -- bin/cctally-newwrapper '
    '| awk \'{print $1}\')\n'
    '[ "$mode2" = "100644" ] || { echo "ASSERT_FAIL: run2 mode=$mode2 (expected 100644)"; exit 2; }\n'
    'echo "ASSERT_OK"\n',
))


# 20. revert-then-tag (Fix C).
#
# Three publish-kind private commits: P_A creates README.md=X, P_B
# updates it to Y, P_C reverts back to X. P_C's tree is therefore
# identical to P_A's tree. A SemVer tag v0.1.0 lands on P_C.
#
# Pre-fix: `_build_priv_to_pub_map` iterated `pub_fp.items()` from
# oldest first and short-circuited on the first fingerprint match. P_C's
# fingerprint matches the public commit produced by P_A (same tree) AND
# the public commit produced by P_C — but the loop binds P_C to the
# EARLIER public commit, so the tag propagates to the wrong commit.
#
# Post-fix: a forward cursor through `pub_entries` consumes matches
# 1-to-1 in temporal order, so P_C binds to its own public commit. The
# tag lands on the third public commit.
SCENARIOS.append((
    "revert-then-tag",
    'echo "X" > README.md\n'
    'git add README.md\n'
    + _commit_msg_heredoc(
        "fix: PA seed\n"
        "\n"
        "--- public ---\n"
        "docs: PA seed\n",
        sentinel="CCTALLY_MSG_EOF_A",
    )
    + 'echo "Y" > README.md\n'
      'git add README.md\n'
    + _commit_msg_heredoc(
        "fix: PB update\n"
        "\n"
        "--- public ---\n"
        "docs: PB update\n",
        sentinel="CCTALLY_MSG_EOF_B",
    )
    + 'echo "X" > README.md\n'
      'git add README.md\n'
    + _commit_msg_heredoc(
        "fix: PC revert\n"
        "\n"
        "--- public ---\n"
        "docs: PC revert\n",
        sentinel="CCTALLY_MSG_EOF_C",
    )
    + 'git tag -a v0.1.0 -m "v0.1.0" HEAD\n',
    0, "ASSERT_OK", "",
    None,
    # run.sh: mirror runs, then we cross-check that the public tag
    # v0.1.0 resolves to the THIRD public commit (the revert), not the
    # first one. Public log oldest→newest: <init-empty>, P_A_pub,
    # P_B_pub, P_C_pub. The third-from-top by `--reverse` (skipping the
    # init commit) is P_C_pub.
    'python3 bin/cctally-mirror-public --public-clone ../public --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: mirror exit=$rc"; exit "$rc"; fi\n'
    # Tag must exist and be propagated.
    'git -C ../public tag -l | grep -qx "v0.1.0" '
    '|| { echo "ASSERT_FAIL: v0.1.0 not propagated"; exit 2; }\n'
    'TAG_SHA=$(git -C ../public rev-list -n1 v0.1.0)\n'
    # Newest public commit (P_C_pub, the revert).
    'EXPECTED_SHA=$(git -C ../public rev-parse HEAD)\n'
    # First public commit AFTER the SCAFFOLD init-empty commit, i.e.
    # P_A_pub. The map-bug would have bound P_C → P_A_pub and the tag
    # would have landed on P_A_pub.
    'PA_SHA=$(git -C ../public rev-list --reverse HEAD | sed -n 2p)\n'
    '[ "$TAG_SHA" = "$EXPECTED_SHA" ] || {\n'
    '  echo "ASSERT_FAIL: v0.1.0 tag SHA=$TAG_SHA expected $EXPECTED_SHA (HEAD/PC) — bound to $PA_SHA (PA) instead?"; exit 2;\n'
    '}\n'
    '[ "$TAG_SHA" != "$PA_SHA" ] || {\n'
    '  echo "ASSERT_FAIL: v0.1.0 tag wrongly bound to PA_SHA=$PA_SHA"; exit 2;\n'
    '}\n'
    'echo "ASSERT_OK"\n',
))


# 21. tag-gpgsign-config-no-op (Fix B).
#
# Same shape as `tag-propagated` (one publish + annotated v1.0.0), but
# with `git config tag.gpgsign true` set on BOTH the private repo and
# the public clone before mirror runs. Pre-fix, `git tag -a` in
# `_propagate_tags` honored the global setting and would either invoke
# gpg (failing in a test env with no signing key) or silently sign with
# whatever key happened to be lying around — both bad.
#
# Post-fix, both tag invocations pass `-c tag.gpgsign=false`, so gpg is
# never invoked. The fixture verifies (a) the mirror exits 0, (b) the
# tag is propagated, and (c) `git cat-file -p <tag>` shows no PGP
# signature block in the resulting public tag object.
#
# Note: this fixture should NOT require a working gpg agent — Fix B
# means gpg is never invoked, regardless of host config. We deliberately
# avoid neutralizing gpg in setup.sh so this assertion is meaningful: if
# gpg ever IS invoked, the fixture will fail (signing prompt/error or a
# stray signature block in the output).
SCENARIOS.append((
    "tag-gpgsign-config-no-op",
    # Force tag.gpgsign=true on BOTH sides. The SCAFFOLD set both to
    # false; we overwrite that here.
    'git config tag.gpgsign true\n'
    'git -C ../public config tag.gpgsign true\n'
    'echo "v1" > README.md\n'
    'git add README.md\n'
    + _commit_msg_heredoc(
        "release: cut v1\n"
        "\n"
        "--- public ---\n"
        "release: cut v1\n",
    )
    # `git tag -a` on the private side ALSO honors tag.gpgsign — but we
    # don't care if THIS one is signed (it's the source object; we never
    # propagate the signature, the body is parsed and stripped). What
    # matters is the public-side tag invocation that mirror runs. Use
    # `-c tag.gpgsign=false` on the seed tag so the fixture's setup
    # itself doesn't need a working gpg agent.
    + 'git -c tag.gpgsign=false tag -a v1.0.0 -m "v1.0.0"\n',
    0, "ASSERT_OK", "",
    None,
    'python3 bin/cctally-mirror-public --public-clone ../public --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: mirror exit=$rc"; exit "$rc"; fi\n'
    # Tag must be propagated.
    'git -C ../public tag -l | grep -qx "v1.0.0" '
    '|| { echo "ASSERT_FAIL: v1.0.0 not propagated"; exit 2; }\n'
    # The new public tag object MUST NOT carry an inline signature of
    # any kind. `git cat-file -p v1.0.0` on a signed annotated tag
    # prints a signature block after the message body — common formats
    # are PGP (`-----BEGIN PGP SIGNATURE-----`) or SSH
    # (`-----BEGIN SSH SIGNATURE-----`) depending on `gpg.format`.
    # Pre-fix, tag.gpgsign=true would have caused the public-side
    # `git tag -a` to either fail (no signing key configured) or sign
    # silently with whatever key is configured for the host — failure
    # path means rc != 0 (caught above); silent-sign path means a
    # signature block is present. Match both formats so the assertion
    # catches whichever format the host's git is configured for.
    'pub_tag_obj=$(git -C ../public cat-file -p v1.0.0)\n'
    'if echo "$pub_tag_obj" | grep -qE "BEGIN (PGP|SSH) SIGNATURE"; then\n'
    '  echo "ASSERT_FAIL: public v1.0.0 tag is signed (tag.gpgsign leaked)"; exit 2;\n'
    'fi\n'
    'echo "ASSERT_OK"\n',
))


# 22. tag-ssh-signed-private-body-stripped (round-4 P2).
#
# A genuinely SSH-signed annotated tag on the private side. Pre-fix,
# `_propagate_tags` only stripped `\n-----BEGIN PGP SIGNATURE-----` from
# the body it parsed out of `git cat-file -p`, so an SSH signature
# block (`\n-----BEGIN SSH SIGNATURE-----...-----END SSH SIGNATURE-----`)
# would leak verbatim into the public tag's annotation body — looking
# signed (it isn't) and exposing the private tagger's identity.
#
# Post-fix, the inline-strip iterates over both PGP and SSH markers, so
# the public tag's body retains only the human message ("ssh-signed
# release"), and `git cat-file -p v1.0.0` on the public side shows
# zero `BEGIN (PGP|SSH) SIGNATURE` lines.
#
# We generate a throwaway ed25519 keypair in $work (sibling of
# private/ + public/) so the fixture is fully hermetic — no host keys
# or system gpg agent involved. `git tag -s` forces signing regardless
# of `tag.gpgsign`; with `gpg.format=ssh` git delegates to ssh-keygen
# for signing. If ssh-keygen is unavailable on the host, skip the
# signing path and exit setup non-zero with a clear marker so the
# harness reports the failure (every reasonable Linux/macOS dev box
# has ssh-keygen — minimal CI containers without it should install
# openssh-client).
SCENARIOS.append((
    "tag-ssh-signed-private-body-stripped",
    # ssh-keygen is required on the host. Default-installed on macOS
    # and on every reasonable Linux dev/CI box. If absent (minimal CI
    # container), install openssh-client — the harness has no exit-77
    # skip semantics, so we'd rather fail loudly than silently bypass.
    'command -v ssh-keygen >/dev/null '
    '|| { echo "SETUP_FAIL: ssh-keygen unavailable" >&2; exit 1; }\n'
    'ssh-keygen -t ed25519 -f "$work/sshkey" -N "" -q\n'
    # Configure the private repo to sign with the throwaway ssh key.
    'git config user.signingkey "$work/sshkey"\n'
    'git config gpg.format ssh\n'
    'echo "v1" > README.md\n'
    'git add README.md\n'
    + _commit_msg_heredoc(
        "release: cut ssh-signed v1\n"
        "\n"
        "--- public ---\n"
        "release: cut ssh-signed v1\n",
    )
    # `-s` forces signing even when tag.gpgsign is unset. With
    # gpg.format=ssh, git invokes ssh-keygen to sign. The tag body
    # ("ssh-signed release") is what the post-fix mirror should preserve
    # in the public tag — minus the appended SSH signature block.
    + 'git tag -s -m "ssh-signed release" v1.0.0\n'
    # Sanity check: the private tag really IS ssh-signed (so the test
    # is meaningful). If this fails, the host's ssh-keygen rejected the
    # signing config and the regression scenario is degenerate.
    + 'git cat-file -p v1.0.0 | grep -q "BEGIN SSH SIGNATURE" '
    '|| { echo "SETUP_FAIL: private tag was not actually SSH-signed" >&2; exit 1; }\n',
    0, "ASSERT_OK", "",
    None,
    'python3 bin/cctally-mirror-public --public-clone ../public --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: mirror exit=$rc"; exit "$rc"; fi\n'
    # Tag must be propagated.
    'git -C ../public tag -l | grep -qx "v1.0.0" '
    '|| { echo "ASSERT_FAIL: v1.0.0 not propagated"; exit 2; }\n'
    # Round-4 invariant: the public tag body must NOT carry the SSH
    # signature block from the private tag. Pre-fix this regex matches
    # because the SSH sig leaked verbatim into the body; post-fix the
    # body is truncated at the marker.
    'pub_tag_obj=$(git -C ../public cat-file -p v1.0.0)\n'
    'if echo "$pub_tag_obj" | grep -q "BEGIN SSH SIGNATURE"; then\n'
    '  echo "ASSERT_FAIL: public v1.0.0 body leaks SSH signature"; exit 2;\n'
    'fi\n'
    # Sanity: the human message survived the strip (proves we did not
    # over-truncate to an empty body).
    'if ! echo "$pub_tag_obj" | grep -q "ssh-signed release"; then\n'
    '  echo "ASSERT_FAIL: public v1.0.0 body missing human message"; exit 2;\n'
    'fi\n'
    # Belt-and-suspenders: the new public tag itself is unsigned. Same
    # combined PGP|SSH grep as tag-gpgsign-config-no-op — must match
    # exactly zero lines.
    'if echo "$pub_tag_obj" | grep -qE "BEGIN (PGP|SSH) SIGNATURE"; then\n'
    '  echo "ASSERT_FAIL: public v1.0.0 tag itself is signed"; exit 2;\n'
    'fi\n'
    'echo "ASSERT_OK"\n',
))


# ---------------------------------------------------------------------------
# Skip-chain accumulated-diff guards (issue #23, scenarios 23–26).
#
# Verifies the ⚠ ACCUMULATED-DIFF MISMATCH block + refuse gate added to
# cmd_mirror in Task 5. Pairs with the .githooks/_skip_chain_metrics
# helper module (Layer 1 + Layer 2) and preflight.py's envelope flags.
# ---------------------------------------------------------------------------


# 23. skip-chain-clean: 1 publish, 0 skips → no ⚠ block, exit 0.
SCENARIOS.append((
    "skip-chain-clean",
    'mkdir -p docs\n'
    'echo "a" > docs/a.md\n'
    'git add docs/a.md\n'
    + _commit_msg_heredoc(
        "feat: add doc\n"
        "\n"
        "--- public ---\n"
        "feat: add doc\n"
    ),
    # Tight substring: pins the publish-commit subject in the plan
    # line. "mirror plan:" alone would also pass on warn/refuse runs
    # (they print the same banner before the ⚠ block); "feat: add doc"
    # is the publish subject for THIS scenario only — warn uses
    # "docs: condense", refuse uses "fix: thing".
    0, "feat: add doc", "",
    "feat: add doc\n",
    None,
))


# 24. skip-chain-warn: 3 skips + 1 docs publish (chain triggers warn but
# not refuse) → ⚠ WARN block in stdout, exit 0 (operator proceeds).
SCENARIOS.append((
    "skip-chain-warn",
    'mkdir -p docs\n'
    'echo "skip1" > docs/s1.md\n'
    'git add docs/s1.md\n'
    + _commit_msg_heredoc(
        "chore: skip 1\n\nPublic-Skip: true\n",
        sentinel="CCTALLY_SKIP1_MSG_EOF",
    )
    + 'echo "skip2" > docs/s2.md\n'
      'git add docs/s2.md\n'
    + _commit_msg_heredoc(
        "chore: skip 2\n\nPublic-Skip: true\n",
        sentinel="CCTALLY_SKIP2_MSG_EOF",
    )
    + 'echo "skip3" > docs/s3.md\n'
      'git add docs/s3.md\n'
    + _commit_msg_heredoc(
        "chore: skip 3\n\nPublic-Skip: true\n",
        sentinel="CCTALLY_SKIP3_MSG_EOF",
    )
    + 'echo "pub" > docs/p.md\n'
      'git add docs/p.md\n'
    + _commit_msg_heredoc(
        "docs: condense\n"
        "\n"
        "--- public ---\n"
        "docs: condense\n",
        sentinel="CCTALLY_PUB_MSG_EOF",
    ),
    # Tight substring: pins the WARN-verdict line specifically.
    # "ACCUMULATED-DIFF MISMATCH" alone passes on both warn AND refuse
    # runs (same block header). "Consider bundling." appears only in
    # _render_skip_chain_warning's warn branch — refuse renders
    # "REFUSE (chain>15 + max-ratio>5× + fix/chore subject)." and
    # "→ To proceed: author a feat:/docs(changelog): bundling commit"
    # instead.
    0, "Consider bundling.", "",
    None,  # public message check skipped — multiple paths get bundled
    None,
))


# 25. skip-chain-refuse: 16 skips + 1 fix publish → ⚠ REFUSE block,
# exit 1, "refusing to apply" on stderr.
SCENARIOS.append((
    "skip-chain-refuse",
    'mkdir -p docs\n'
    + ''.join(
        f'echo "skip{i}" > docs/s{i}.md\n'
        f'git add docs/s{i}.md\n'
        + _commit_msg_heredoc(
            f"chore: skip {i}\n\nPublic-Skip: true\n",
            sentinel=f"CCTALLY_SKIP_{i}_MSG_EOF",
        )
        for i in range(1, 17)
    )
    + 'echo "pub" > docs/p.md\n'
      'git add docs/p.md\n'
    + _commit_msg_heredoc(
        "fix: thing\n"
        "\n"
        "--- public ---\n"
        "fix: thing\n",
        sentinel="CCTALLY_PUB_MSG_EOF",
    ),
    1, "ACCUMULATED-DIFF MISMATCH", "refusing to apply",
    None,
    None,
))


# 26. skip-chain-refuse-with-override: same setup as -refuse, but run.sh
# passes --accept-skip-mismatch → ⚠ REFUSE block STILL renders (operator
# sees what they're overriding) but exit 0; golden-public-msg.txt proves
# the publish landed.
SCENARIOS.append((
    "skip-chain-refuse-with-override",
    'mkdir -p docs\n'
    + ''.join(
        f'echo "skip{i}" > docs/s{i}.md\n'
        f'git add docs/s{i}.md\n'
        + _commit_msg_heredoc(
            f"chore: skip {i}\n\nPublic-Skip: true\n",
            sentinel=f"CCTALLY_SKIP_{i}_MSG_EOF",
        )
        for i in range(1, 17)
    )
    + 'echo "pub" > docs/p.md\n'
      'git add docs/p.md\n'
    + _commit_msg_heredoc(
        "fix: thing\n"
        "\n"
        "--- public ---\n"
        "fix: thing\n",
        sentinel="CCTALLY_PUB_MSG_EOF",
    ),
    0, "ACCUMULATED-DIFF MISMATCH", "",
    "fix: thing\n",  # override worked → publish landed
    # run.sh: invoke with --accept-skip-mismatch (mirrors bootstrap-* run.sh shape)
    'python3 bin/cctally-mirror-public --public-clone ../public --yes '
    '--accept-skip-mismatch\n',
))


# 27. skip-chain-refuse-dry-run: same setup as -refuse, but run.sh adds
# --dry-run → ⚠ REFUSE block renders, but exit 0 (no enforcement).
# Pins the spec §6.4 + commit f5bc2ad ordering: dry-run short-circuits
# BEFORE the refuse gate so preflight's `_run_mirror_dry_run` probe
# doesn't add `dry_run_failed` alongside `long_skip_chain_with_fix_subject`
# for the same root cause. Regression for the "two red_flags, one root
# cause" UX bug.
SCENARIOS.append((
    "skip-chain-refuse-dry-run",
    'mkdir -p docs\n'
    + ''.join(
        f'echo "skip{i}" > docs/s{i}.md\n'
        f'git add docs/s{i}.md\n'
        + _commit_msg_heredoc(
            f"chore: skip {i}\n\nPublic-Skip: true\n",
            sentinel=f"CCTALLY_SKIP_{i}_MSG_EOF",
        )
        for i in range(1, 17)
    )
    + 'echo "pub" > docs/p.md\n'
      'git add docs/p.md\n'
    + _commit_msg_heredoc(
        "fix: thing\n"
        "\n"
        "--- public ---\n"
        "fix: thing\n",
        sentinel="CCTALLY_PUB_MSG_EOF",
    ),
    0, "ACCUMULATED-DIFF MISMATCH", "",
    None,  # no public commit landed — dry-run is non-mutating
    # run.sh: invoke with --dry-run on a refuse-condition repo. The ⚠
    # block still renders to stdout (substring check above), but the
    # refuse gate must NOT fire (exit 0). Without --accept-skip-mismatch.
    'python3 bin/cctally-mirror-public --public-clone ../public --yes '
    '--dry-run\n',
))


# Commit-time allowlist regression. The bug: the mirror tool used to
# classify each commit's paths under HEAD's `.mirror-allowlist`, while
# the commit-msg hook (`.githooks/_public_trailer.py`) classified under
# the allowlist that lived in the commit's tree at the time. A commit
# that added a NOT-YET-ALLOWLISTED file followed by a later commit
# adding that file to the allowlist was accepted by the hook (correct)
# but rejected by the mirror tool with "touches public files but has
# no trailer." This scenario drives that exact sequence; the fix makes
# the mirror tool also use commit-time allowlist semantics, so the run
# succeeds end-to-end.
#
# Sequence inside the scenario:
#   S1 (private/unmatched): overwrite `.mirror-allowlist` to a minimal
#       version that doesn't match `extras/widget.txt`. The allowlist
#       file itself is unmatched in any sane allowlist (it never lists
#       itself), so this commit is private-only — no trailer needed.
#   S2 (the bug case): add `extras/widget.txt`. Under S2's tree's
#       allowlist (the minimal one), the file is unmatched. Pre-fix:
#       reclassified under HEAD's allowlist (which after S3 matches
#       `extras/**`) → flagged as public, mirror refuses.
#       Post-fix: classified under S2's tree → unmatched → silently
#       skipped, no trailer required.
#   S3 (private/unmatched): grow the allowlist to include `extras/**`.
#       Touches `.mirror-allowlist` only — still unmatched.
#
# Expected: exit 0, no public commit lands, stdout shows the cursor
# advances. None of S1/S2/S3 needs a `--- public ---` block.
SCENARIOS.append((
    "commit-time-allowlist-no-retroactive-flag",
    # S1: overwrite .mirror-allowlist with a minimal version that does
    # NOT match `extras/**`. cwd is $work/private/. We use a heredoc
    # WITHOUT command substitution so the contents land verbatim.
    'cat > .mirror-allowlist <<\'CCTALLY_ALLOWLIST_S1_EOF\'\n'
    '# minimal allowlist for the regression scenario\n'
    'README.md\n'
    'CCTALLY_ALLOWLIST_S1_EOF\n'
    'git add .mirror-allowlist\n'
    + _commit_msg_heredoc(
        "chore: trim allowlist to minimal\n",
        sentinel="CCTALLY_S1_EOF",
    )
    # S2: add extras/widget.txt — at S2's tree, the allowlist does NOT
    # match it. Pre-fix this would be reclassified as public under
    # HEAD's (S3's) allowlist and refused; post-fix it stays unmatched.
    + 'mkdir -p extras\n'
      'echo "widget" > extras/widget.txt\n'
      'git add extras/widget.txt\n'
    + _commit_msg_heredoc(
        "feat(extras): add widget\n"
        "\n"
        "The file is unmatched against `.mirror-allowlist` at this\n"
        "commit's tree; a follow-up commit promotes the path. Ordering\n"
        "is deliberate; no public-mirror trailer needed.\n",
        sentinel="CCTALLY_S2_EOF",
    )
    # S3: extend allowlist to match extras/**. Only touches the
    # allowlist file itself (still unmatched).
    + 'cat > .mirror-allowlist <<\'CCTALLY_ALLOWLIST_S3_EOF\'\n'
      '# minimal allowlist for the regression scenario\n'
      'README.md\n'
      'extras/**\n'
      'CCTALLY_ALLOWLIST_S3_EOF\n'
      'git add .mirror-allowlist\n'
    + _commit_msg_heredoc(
        "chore: promote extras/** to public\n",
        sentinel="CCTALLY_S3_EOF",
    ),
    # Expected: exit 0, advancing the cursor with no public commit
    # produced (S1/S2/S3 are all private/unmatched). The "(no public
    # commits to produce; advancing cursor only)" line is the stdout
    # signature of a private-only walk.
    0, "advancing cursor only", "",
    None,  # no public-HEAD message check (no publish happened)
    None,
))


def build(out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    for (
        name,
        body,
        exit_code,
        stdout_substr,
        stderr_substr,
        public_msg,
        run,
    ) in SCENARIOS:
        d = out_root / name
        d.mkdir(parents=True, exist_ok=True)
        setup = _SCAFFOLD + body
        (d / "setup.sh").write_text(setup, encoding="utf-8")
        (d / "setup.sh").chmod(0o755)
        (d / "golden-exit.txt").write_text(f"{exit_code}\n", encoding="utf-8")
        (d / "golden-stdout-substr.txt").write_text(
            stdout_substr + "\n", encoding="utf-8",
        )
        (d / "golden-stderr-substr.txt").write_text(
            stderr_substr + "\n", encoding="utf-8",
        )
        # Optional public-HEAD-message golden: only emit when present so
        # the harness's `[ -f golden-public-msg.txt ]` gate works.
        msg_path = d / "golden-public-msg.txt"
        if public_msg is None:
            if msg_path.exists():
                msg_path.unlink()
        else:
            msg_path.write_text(public_msg, encoding="utf-8")
        # Optional per-scenario run.sh: bootstrap-mode scenarios use it
        # to swap the default mirror invocation for a custom one.
        run_path = d / "run.sh"
        if run is None:
            if run_path.exists():
                run_path.unlink()
        else:
            run_path.write_text("#!/bin/bash\nset -uo pipefail\n" + run,
                                encoding="utf-8")
            run_path.chmod(0o755)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(FIXTURES_DIR))
    args = p.parse_args()
    build(Path(args.out))
    print(f"mirror-public fixtures: built {len(SCENARIOS)} scenarios → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
