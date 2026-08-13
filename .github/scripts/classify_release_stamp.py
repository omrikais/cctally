#!/usr/bin/env python3
"""Decide whether a push is a provable pure release stamp (#529 S7, E1).

Emits `skipHeavy=true` ONLY for a push of exactly one commit that is a
release stamp in the shape `bin/_cctally_release.py` itself produces.
Everything else — every error included — emits `skipHeavy=false`, because
the default this gate must hold is that the full pipeline runs.

Every admitted path's content is verified by applying the release tool's OWN
transformation to the pre-image and requiring the result to equal the
post-image, so this classifier cannot drift away from the thing it
classifies. Both transformations live in stdlib-only kernels the release
tool itself imports — `bin/_lib_mirror_contributors.py` and
`bin/_lib_changelog_stamp.py` — so this script loads those files rather than
`bin/_cctally_release.py`, which pulls in `_cctally_core` and the rest of
the CLI, none of which a classifier needs.

`CHANGELOG.md` is verified for the same reason as the other two, and NOT
because a markdown-only push could reach these jobs — it cannot, since the
workflow filters on `paths-ignore: ['**/*.md']`. It is verified because a
hand-edited CHANGELOG riding beside a version bump DOES reach them, and the
pytest phase inside `test-macos` reads the repository's real `CHANGELOG.md`
(`tests/test_readme_refresh.py`, `tests/test_source_aware_share.py`).
Suppressing that phase for an unverified CHANGELOG would skip exactly the
tests that would catch the edit.

`today_utc` is recovered from the post-image's own `## [X.Y.Z] - <date>`
heading before the reconstruction runs. That is safe rather than circular:
the date is the transformation's ONLY free parameter, and every other byte
of the reconstruction comes from the already-certified pre-image, so a
post-image differing anywhere else still fails the byte comparison.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

STAMP_SUBJECT = re.compile(r"^chore\(release\): v(\d+\.\d+\.\d+)$")
ZERO = "0" * 40
CHANGELOG = "CHANGELOG.md"
PACKAGE = "package.json"
CONTRIBUTORS = ".mirror-contributors.json"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _decide(repo: Path, before: str, after: str, event_name: str) -> tuple[bool, str]:
    if event_name != "push":
        return False, f"event is {event_name!r}, not a push"
    if not before or not after or before == ZERO or after == ZERO:
        return False, "the pushed range has no usable before/after pair"
    if before == after:
        return False, "before and after are the same commit"

    try:
        head = _git(repo, "rev-parse", "HEAD").strip()
    except subprocess.CalledProcessError:
        return False, "HEAD is unreadable"
    if head != after:
        return False, f"HEAD {head[:9]} is not the pushed after {after[:9]}"

    try:
        parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    except subprocess.CalledProcessError:
        return False, "the commit's parents are unreadable"
    if len(parents) != 1:
        return False, f"the commit has {len(parents)} parents, not one"
    if parents[0] != before:
        return False, "the commit's parent is not the pushed before (force-push or a range)"

    subject = _git(repo, "log", "-1", "--format=%s", "HEAD").strip()
    match = STAMP_SUBJECT.match(subject)
    if not match:
        return False, f"subject {subject!r} is not a release stamp subject"
    version = match.group(1)

    changed = sorted(
        p for p in _git(repo, "diff", "--name-only", f"{before}..HEAD").split("\n") if p
    )
    allowed = {CHANGELOG, PACKAGE, CONTRIBUTORS}
    if not set(changed) <= allowed or CHANGELOG not in changed:
        return False, f"changed paths {changed} are not a stamp's staging set"

    if PACKAGE in changed and not _package_is_version_only(repo, before, version):
        return False, "the package.json diff is not a version-only bump to the subject's version"

    if CONTRIBUTORS in changed and not _contributors_match(repo, before, version):
        return False, "the .mirror-contributors.json diff is not stamp_pending_attributions' output"

    if not _changelog_matches(repo, before, version):
        return False, "the CHANGELOG.md diff is not _release_stamp_changelog's output"

    return True, f"a provable pure release stamp for v{version}"


def _blob(repo: Path, rev: str, path: str) -> str | None:
    try:
        return _git(repo, "show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return None


def _package_is_version_only(repo: Path, before: str, version: str) -> bool:
    old_raw, new_raw = _blob(repo, before, PACKAGE), _blob(repo, "HEAD", PACKAGE)
    if old_raw is None or new_raw is None:
        return False
    try:
        old, new = json.loads(old_raw), json.loads(new_raw)
    except ValueError:
        return False
    if new.get("version") != version:
        return False
    old.pop("version", None)
    new.pop("version", None)
    return old == new


def _load_kernel(repo: Path, name: str):
    """Load one of the release tool's stdlib-only kernels out of the tree
    under classification. Returns None when the file is absent or will not
    execute — a public clone carries neither kernel, and the caller turns
    that into `skipHeavy=false` rather than an error."""
    spec = importlib.util.spec_from_file_location(name, repo / f"bin/{name}.py")
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _contributors_match(repo: Path, before: str, version: str) -> bool:
    old = _blob(repo, before, CONTRIBUTORS)
    new = _blob(repo, "HEAD", CONTRIBUTORS)
    if old is None or new is None:
        return False
    module = _load_kernel(repo, "_lib_mirror_contributors")
    if module is None:
        return False
    try:
        expected, _bound = module.stamp_pending_attributions(
            old, version, source=CONTRIBUTORS)
    except Exception:
        return False
    return expected == new


def _changelog_matches(repo: Path, before: str, version: str) -> bool:
    old = _blob(repo, before, CHANGELOG)
    new = _blob(repo, "HEAD", CHANGELOG)
    if old is None or new is None:
        return False
    date = re.search(
        rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$", new, re.M)
    if date is None:
        return False
    module = _load_kernel(repo, "_lib_changelog_stamp")
    if module is None:
        return False
    try:
        expected, _body = module._release_stamp_changelog(
            old, version, date.group(1))
    except Exception:
        return False
    return expected == new


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--before", default="")
    parser.add_argument("--after", default="")
    parser.add_argument("--event-name", default="push")
    args = parser.parse_args()

    try:
        skip, reason = _decide(Path(args.repo), args.before, args.after, args.event_name)
    except Exception as exc:                      # never propagate: default to running
        skip, reason = False, f"classifier error: {exc!r}"

    print(f"skipHeavy={'true' if skip else 'false'}")
    print(f"reason={reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as handle:
            handle.write(f"skipHeavy={'true' if skip else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
