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

End-to-end coverage of the mirror tool's surviving surfaces (the per-commit
trailer/replay model was retired in #281 S9):
  - bootstrap (empty / non-empty-refused / force-overwrite / dry-run)
  - reconcile (clean / idempotent / refuses cursor-behind / refuses dirty)
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
#   - Copies real .mirror-allowlist + .githooks/_match.py into private/
#     (so the mirror tool classifies against the actual deployed allowlist).
#   - Seeds public/ with one empty commit so the public HEAD probe in the
#     harness has something to read.
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
# #281 S9 retired the trailer machinery — these fixtures exercise only
# cmd_bootstrap + cmd_reconcile, neither of which needs _public_trailer.py
# or _skip_chain_metrics.py (the mirror tool's skip-chain import is gone).
# The mirror tool resolves _REPO_ROOT via __file__.parent.parent at
# import time. Copy it INTO the scratch private's bin/ and run from
# there so it walks the scratch private repo (not cctally-dev itself).
cp "$REPO_ROOT/bin/cctally-mirror-public" bin/
chmod +x bin/cctally-mirror-public
# Optional: .public-tag-patterns drives tag propagation. Copy it in so
# the tag-propagated/tag-held-back scenarios match the live config.
cp "$REPO_ROOT/.public-tag-patterns" .

# Infra-bootstrap commit + mirror-cursor at HEAD (the cursor establishes
# the pre-publish baseline the reconcile scenarios drift away from).
git add -A
git commit --no-verify -q -m "chore: infra bootstrap"
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
    + _commit_msg_heredoc("docs: initial seed\n")
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












# ---------------------------------------------------------------------------
# Reconcile scenarios (Task 3 — spec §7).
#
# These exercise the `--reconcile` fix-forward recovery mode. Each
# scenario must satisfy two preconditions before reconcile itself can
# run (or refuse on the right guard):
#   1. Public clone's origin URL must resolve to `omrikais/cctally`
#      (Guard 2 — wrong-clone). The scaffold doesn't set origin, so
#      each scenario's run.sh adds it as a faux-remote pointing at a
#      bogus path (the URL is only inspected by `_normalize_origin_url`,
#      never dereferenced for fetch/push).
#   2. mirror-cursor must equal private HEAD (Guard 3 — cursor invariant)
#      EXCEPT in `drift-reconcile-refuses-cursor-behind`, which
#      deliberately advances HEAD past the cursor to trip the guard.
#
# Note: --reconcile does NOT advance mirror-cursor or push tags. The
# operator pushes manually per spec §7 *Algorithm* step 10. Scenarios
# don't try to verify push behavior.
# ---------------------------------------------------------------------------


# drift-reconcile-clean: seed the public clone with a stale (drifted)
# file directly. run.sh invokes --reconcile; expect exit 0, a single
# new commit on public removing the stale file, post-commit precheck
# passes.
SCENARIOS.append((
    "drift-reconcile-clean",
    # No scenario-specific private commits — the infra-bootstrap commit
    # is HEAD, and `mirror-cursor` is tagged at HEAD by the scaffold.
    # Seed the public clone with the stale file directly.
    'pushd "$work/public" >/dev/null\n'
    'mkdir -p legacy\n'
    'echo "stale content" > legacy/stale.md\n'
    'git add legacy/stale.md\n'
    'git commit --no-verify -q -m "chore: seed stale legacy file"\n'
    'popd >/dev/null\n',
    0, "ASSERT_OK", "",
    None,
    # run.sh: configure faux origin → run --reconcile → assert one new
    # public commit removed the stale file + verify reconcile-source
    # trailer + verify mirror-cursor unchanged (reconcile MUST NOT
    # advance it).
    'pushd ../public >/dev/null\n'
    'git remote remove origin 2>/dev/null || true\n'
    'git remote add origin https://github.com/omrikais/cctally.git\n'
    'popd >/dev/null\n'
    'PUB_HEAD_BEFORE=$(git -C ../public rev-parse HEAD)\n'
    'CURSOR_BEFORE=$(git rev-parse refs/tags/mirror-cursor)\n'
    'python3 bin/cctally-mirror-public --public-clone ../public --reconcile --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: reconcile exit=$rc"; exit "$rc"; fi\n'
    'PUB_HEAD_AFTER=$(git -C ../public rev-parse HEAD)\n'
    'CURSOR_AFTER=$(git rev-parse refs/tags/mirror-cursor)\n'
    'if [ "$PUB_HEAD_BEFORE" = "$PUB_HEAD_AFTER" ]; then\n'
    '  echo "ASSERT_FAIL: reconcile produced no commit (HEAD unchanged)"\n'
    '  exit 2\n'
    'fi\n'
    'if [ "$CURSOR_BEFORE" != "$CURSOR_AFTER" ]; then\n'
    '  echo "ASSERT_FAIL: reconcile advanced mirror-cursor (must not move)"\n'
    '  exit 2\n'
    'fi\n'
    'test ! -e ../public/legacy/stale.md || { echo "ASSERT_FAIL: stale file survived reconcile"; exit 2; }\n'
    # Subject + trailer assertions.
    'subject=$(git -C ../public log -1 --format=%s)\n'
    '[ "$subject" = "chore: reconcile public tree against allowlist" ] || { echo "ASSERT_FAIL: subject=$subject"; exit 2; }\n'
    'git -C ../public log -1 --format=%B | grep -q "^Reconcile-Source: " || { echo "ASSERT_FAIL: missing Reconcile-Source trailer"; exit 2; }\n'
    'echo "ASSERT_OK"\n',
))


# drift-reconcile-idempotent: invoke --reconcile twice in a row. The
# first run produces one cleanup commit; the second sees private HEAD
# projection == public HEAD tree and exits 0 with `no drift to
# reconcile.` — no new commit.
SCENARIOS.append((
    "drift-reconcile-idempotent",
    'pushd "$work/public" >/dev/null\n'
    'mkdir -p legacy\n'
    'echo "stale content" > legacy/stale.md\n'
    'git add legacy/stale.md\n'
    'git commit --no-verify -q -m "chore: seed stale legacy file"\n'
    'popd >/dev/null\n',
    0, "ASSERT_OK", "",
    None,
    # run.sh: configure faux origin → reconcile twice → assert second
    # run produced no new commit and stdout contains the no-op string.
    'pushd ../public >/dev/null\n'
    'git remote remove origin 2>/dev/null || true\n'
    'git remote add origin https://github.com/omrikais/cctally.git\n'
    'popd >/dev/null\n'
    'python3 bin/cctally-mirror-public --public-clone ../public --reconcile --yes\n'
    'rc=$?\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: reconcile1 exit=$rc"; exit "$rc"; fi\n'
    'HEAD_AFTER_RUN1=$(git -C ../public rev-parse HEAD)\n'
    'python3 bin/cctally-mirror-public --public-clone ../public --reconcile --yes \\\n'
    '  > /tmp/cctally-reconcile-idempotent-run2.txt 2>&1\n'
    'rc=$?\n'
    'cat /tmp/cctally-reconcile-idempotent-run2.txt\n'
    'if [ "$rc" -ne 0 ]; then echo "ASSERT_FAIL: reconcile2 exit=$rc"; exit "$rc"; fi\n'
    'HEAD_AFTER_RUN2=$(git -C ../public rev-parse HEAD)\n'
    'if [ "$HEAD_AFTER_RUN1" != "$HEAD_AFTER_RUN2" ]; then\n'
    '  echo "ASSERT_FAIL: idempotent reconcile produced a second commit"\n'
    '  exit 2\n'
    'fi\n'
    'grep -q "no drift to reconcile" /tmp/cctally-reconcile-idempotent-run2.txt || { echo "ASSERT_FAIL: expected no-drift message on second run"; exit 2; }\n'
    'echo "ASSERT_OK"\n',
))


# drift-reconcile-refuses-dirty: leave an uncommitted file in the
# public clone; --reconcile must refuse with the dirty-worktree guard
# message and exit 2 without modifying public HEAD.
SCENARIOS.append((
    "drift-reconcile-refuses-dirty",
    # No private-side scenario commits. Stage a dirty file on the
    # PUBLIC clone (the dirty-worktree guard runs `git status
    # --porcelain` on the public clone, not the private).
    'pushd "$work/public" >/dev/null\n'
    'echo "uncommitted draft" > UNCOMMITTED.md\n'
    'popd >/dev/null\n',
    2, "ASSERT_OK_REFUSE", "refuses dirty public clone",
    None,
    'pushd ../public >/dev/null\n'
    'git remote remove origin 2>/dev/null || true\n'
    'git remote add origin https://github.com/omrikais/cctally.git\n'
    'popd >/dev/null\n'
    'PUB_HEAD_BEFORE=$(git -C ../public rev-parse HEAD)\n'
    'python3 bin/cctally-mirror-public --public-clone ../public --reconcile --yes\n'
    'rc=$?\n'
    'PUB_HEAD_AFTER=$(git -C ../public rev-parse HEAD)\n'
    'if [ "$PUB_HEAD_BEFORE" != "$PUB_HEAD_AFTER" ]; then\n'
    '  echo "ASSERT_FAIL: refused reconcile mutated public HEAD"\n'
    '  exit 2\n'
    'fi\n'
    'if [ "$rc" -eq 2 ]; then echo "ASSERT_OK_REFUSE"; exit 2; fi\n'
    'echo "ASSERT_FAIL: expected exit 2, got $rc"\n'
    'exit "$rc"\n',
))


# drift-reconcile-refuses-cursor-behind: advance private HEAD past
# `mirror-cursor` without running the apply pass; --reconcile must
# refuse with the cursor-invariant guard message and exit 2.
SCENARIOS.append((
    "drift-reconcile-refuses-cursor-behind",
    # Scaffold's mirror-cursor is at the infra-bootstrap commit. Add a
    # private-only commit after the scaffold so HEAD advances past
    # mirror-cursor. The commit is private (no public files touched,
    # touches an unmatched path); commit shape is irrelevant to the
    # snapshot model.
    'mkdir -p docs\n'
    'echo "internal note" > docs/internal-only.md\n'
    'git add docs/internal-only.md\n'
    + _commit_msg_heredoc(
        "chore: private-only commit to advance HEAD past cursor\n",
        sentinel="CCTALLY_MSG_EOF_BEHIND",
    ),
    2, "ASSERT_OK_REFUSE", "--reconcile refused — mirror-cursor",
    None,
    'pushd ../public >/dev/null\n'
    'git remote remove origin 2>/dev/null || true\n'
    'git remote add origin https://github.com/omrikais/cctally.git\n'
    'popd >/dev/null\n'
    'PUB_HEAD_BEFORE=$(git -C ../public rev-parse HEAD)\n'
    'python3 bin/cctally-mirror-public --public-clone ../public --reconcile --yes\n'
    'rc=$?\n'
    'PUB_HEAD_AFTER=$(git -C ../public rev-parse HEAD)\n'
    'if [ "$PUB_HEAD_BEFORE" != "$PUB_HEAD_AFTER" ]; then\n'
    '  echo "ASSERT_FAIL: refused reconcile mutated public HEAD"\n'
    '  exit 2\n'
    'fi\n'
    'if [ "$rc" -eq 2 ]; then echo "ASSERT_OK_REFUSE"; exit 2; fi\n'
    'echo "ASSERT_FAIL: expected exit 2, got $rc"\n'
    'exit "$rc"\n',
))


# Public-clone tree goldens for the drift scenarios. Each value is the
# expected `git -C public ls-tree -r --name-only HEAD | sort` output
# after run.sh completes. The harness checks this when present.
#
# All four per-commit-drift scenarios converge on the same tree shape:
# docs/notes.md (still public) survives; bin/cctally-foo /
# bin/cctally-bar are either absent (demote) or present (promote).
# Reconcile scenarios add their own goldens (clean = empty tree, the
# refused ones omit the tree golden since exit 2 means no mutation).
PUBLIC_TREE_GOLDENS: dict[str, str] = {
    # drift-reconcile-clean: after reconcile, the only thing on public
    # is the synthetic reconcile commit's tree. The infra commit had no
    # public-classified paths (everything was private/unmatched in the
    # scaffold), so the reconcile commit's tree is empty (legacy/stale
    # .md was the only file on the prior public HEAD and reconcile
    # deletes it). Empty tree golden = empty string.
    "drift-reconcile-clean": "",
    "drift-reconcile-idempotent": "",
}


def build(out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    # Typo guard for PUBLIC_TREE_GOLDENS keys — a misspelled scenario
    # name in the dict would silently disable its tree check, masking
    # regressions. Fail loudly at build time instead.
    _scenario_names = {s[0] for s in SCENARIOS}
    for _golden_name in PUBLIC_TREE_GOLDENS:
        if _golden_name not in _scenario_names:
            raise RuntimeError(
                f"PUBLIC_TREE_GOLDENS has key {_golden_name!r} that does "
                f"not match any SCENARIOS entry — typo or stale name."
            )
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
        # Optional public-tree golden: sorted `git -C public ls-tree -r
        # --name-only HEAD`. When present, the harness verifies the
        # public clone's tree matches byte-for-byte after the mirror
        # run. Used by the drift-* scenarios where the assertion is
        # "did files disappear / appear as expected." Existing scenarios
        # without an entry in PUBLIC_TREE_GOLDENS leave their fixture
        # dir without the file → the harness skips the check.
        tree_path = d / "golden-public-tree.txt"
        if name in PUBLIC_TREE_GOLDENS:
            tree_path.write_text(PUBLIC_TREE_GOLDENS[name], encoding="utf-8")
        elif tree_path.exists():
            tree_path.unlink()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(FIXTURES_DIR))
    args = p.parse_args()
    build(Path(args.out))
    print(f"mirror-public fixtures: built {len(SCENARIOS)} scenarios → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
