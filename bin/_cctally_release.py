#!/usr/bin/env python3
"""cctally — release-automation phases extracted from bin/cctally.

This module hosts symbols that compose the six-phase `cctally release`
flow but are NOT directly monkeypatched by tests. The remaining release
helpers (the per-phase `_done`/`_run_phase_*` predicates, preflight gates,
clone discoverers, etc.) live in bin/cctally so test monkeypatches via
`monkeypatch.setattr(cctally, "X", ...)` keep working unchanged.

Loaded lazily by bin/cctally via a PEP 562 `__getattr__` registry (wired
in Bundle 4 / Commit #2). Until then the file exists on disk as the
out-of-band target for bin/_cctally_release.py.

References to symbols that stay in bin/cctally (path constants, regex
compiles, the audit-driven STAYING set) are routed through
`cctally.<name>`, taking advantage of the sys.modules['cctally']
registration established by bin/cctally's `__main__` setdefault and by
tests/conftest.py's exec_module bridge (spec §5.1, §6.0a).

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
Plan: docs/superpowers/plans/2026-05-13-bin-cctally-split.md (Bundle 3 / Task 16)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
import sys
import time
import urllib.request

from _lib_semver import (
    _SEMVER_NUM,
    _release_compute_next_version,
    _release_parse_semver,
    _release_semver_sort_key,
)


def _cctally():
    """Resolve the current `cctally` module at call-time.

    We DON'T `import cctally` at module top: the `import` statement
    binds `cctally` in `_cctally_release.__dict__` at the moment of
    first import. Tests that load a fresh `bin/cctally` namespace
    (e.g. via load_script() in tests/conftest.py, or per-test-file
    SourceFileLoader registration) reassign `sys.modules["cctally"]`.
    Anything bound at import time stays pinned to the OLD module, so a
    `monkeypatch.setattr(cctally, "CHANGELOG_PATH", tmp)` in a NEW
    cctally instance doesn't propagate to the back-references here —
    and stamps leak to the on-disk repo.

    Resolving via `sys.modules["cctally"]` on every call means the
    back-reference always tracks the *current* test session's cctally
    instance. One dict lookup per access; negligible vs. the test
    isolation it buys.

    Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md §5.5
    """
    return sys.modules["cctally"]


def _package_json_path() -> pathlib.Path:
    """Resolve package.json's path next to CHANGELOG.md.

    Indirected through CHANGELOG_PATH so that tests monkeypatching
    CHANGELOG_PATH (to redirect into a fixture repo) also redirect
    package.json — Phase 1 co-stamp and any read-side caller (e.g.
    the Phase 1 done-check) stay in lockstep without a separate
    monkeypatch surface.
    """
    return _cctally().CHANGELOG_PATH.parent / "package.json"


def _homebrew_template_path() -> pathlib.Path:
    """Resolve the Homebrew formula template's path under the repo root.

    Indirected through CHANGELOG_PATH for the same reason as
    :func:`_package_json_path`: Phase 6 fixture tests monkeypatch
    CHANGELOG_PATH to redirect into a fixture repo, and the template
    must follow without a separate monkeypatch surface.
    """
    return _cctally().CHANGELOG_PATH.parent / "homebrew" / "cctally.rb.template"


_FORMULA_VERSION_RE = re.compile(
    rf'/v({_SEMVER_NUM}\.{_SEMVER_NUM}\.{_SEMVER_NUM}'
    rf'(?:-[a-zA-Z][a-zA-Z0-9-]*\.{_SEMVER_NUM})?)\.tar\.gz'
)


def _release_extract_formula_version(text: str) -> str | None:
    """Extract the SemVer string from a homebrew formula's archive URL.

    Returns the matched version (e.g. `"1.3.0"`, `"1.0.0-rc.1"`) or
    ``None`` if no `/vX.Y.Z[.tar.gz]` substring is found. Used by Phase 6's
    monotonic-version gate (issue #30) — the gate refuses to write a lower
    version on top of a higher one. A formula that does not match is
    treated as unversioned (gate allows the write).
    """
    m = _FORMULA_VERSION_RE.search(text)
    return m.group(1) if m else None


def _release_brew_archive_url(version: str) -> str:
    """Return the GitHub auto-archive URL for a version tag.

    Honors hidden env hook ``CCTALLY_RELEASE_BREW_ARCHIVE_URL`` for
    fixture tests (the value is used verbatim; ``{version}`` placeholder
    is substituted if present). Mirrors the ``CCTALLY_RELEASE_DATE_UTC``
    and ``CCTALLY_AS_OF`` precedents — env-only, not in --help.
    """
    override = os.environ.get("CCTALLY_RELEASE_BREW_ARCHIVE_URL")
    if override:
        return override.replace("{version}", version)
    return (
        f"https://github.com/{_cctally().PUBLIC_REPO}/archive/refs/tags/v{version}.tar.gz"
    )


def _release_parse_changelog(text: str) -> dict:
    """Parse CHANGELOG.md into {preamble, sections[]}.

    Each section: {heading, subsections[]}
    Each subsection: {heading, bullets[]}
    Bullets preserve continuation lines (multi-line bullet blocks stay intact).
    Bullet markers recognized: "- " and "* " only (not "+ ").
    Code-fenced blocks suppress heading detection; fences recognized: ``` only (not ~~~).
    """
    lines = text.splitlines(keepends=False)
    preamble: list[str] = []
    sections: list[dict] = []
    cur_section: dict | None = None
    cur_subsection: dict | None = None
    cur_bullet_lines: list[str] = []
    in_fence = False

    def _flush_bullet():
        nonlocal cur_bullet_lines
        if cur_bullet_lines and cur_subsection is not None:
            cur_subsection["bullets"].append("\n".join(cur_bullet_lines))
            cur_bullet_lines = []

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            cur_bullet_lines.append(line) if cur_bullet_lines else None
            continue

        if not in_fence and line.startswith("## "):
            _flush_bullet()
            cur_subsection = None
            cur_section = {"heading": line, "subsections": []}
            sections.append(cur_section)
            continue

        if not in_fence and line.startswith("### ") and cur_section is not None:
            _flush_bullet()
            cur_subsection = {"heading": line, "bullets": []}
            cur_section["subsections"].append(cur_subsection)
            continue

        if cur_section is None:
            preamble.append(line)
            continue

        # Bullet detection: "- " or "* " at start of line (not in fence)
        if not in_fence and (line.startswith("- ") or line.startswith("* ")):
            _flush_bullet()
            cur_bullet_lines = [line]
            continue

        # Continuation of current bullet (indented or empty within block)
        if cur_bullet_lines and (line.startswith("  ") or line.startswith("\t") or in_fence):
            cur_bullet_lines.append(line)
            continue

        # Blank line ends the current bullet
        if line.strip() == "":
            _flush_bullet()
            continue

    _flush_bullet()
    return {"preamble": "\n".join(preamble), "sections": sections}


def _release_canonical_body(section: dict) -> str:
    """Serialize a parsed section's subsections + bullets to canonical body string.

    Used identically by:
      - public block of stamp commit
      - tag annotation body
      - GH Release --notes-file content

    Format (spec §6.4): subsection headers + bullets, blank line between
    subsections, no trailing newline.
    """
    blocks: list[str] = []
    for sub in section["subsections"]:
        if not sub["bullets"]:
            continue
        block_lines = [sub["heading"], *sub["bullets"]]
        blocks.append("\n".join(block_lines))
    return "\n\n".join(blocks)


def _release_stamp_changelog(text: str, version: str, today_utc: str) -> tuple[str, str]:
    """Stamp [Unreleased] entries into a new [version] section.

    Returns (new_changelog_text, canonical_body_string).
    Raises ValueError on empty [Unreleased].
    """
    parsed = _release_parse_changelog(text)
    sections = parsed["sections"]
    if not sections or sections[0]["heading"].strip() != "## [Unreleased]":
        raise ValueError("CHANGELOG.md must start with '## [Unreleased]' section")
    unreleased = sections[0]
    non_empty = [s for s in unreleased["subsections"] if s["bullets"]]
    if not non_empty:
        raise ValueError("[Unreleased] is empty; nothing to release")

    # Build new section as a parsed-section-shaped dict for canonical body extraction
    new_section = {
        "heading": f"## [{version}] - {today_utc}",
        "subsections": [
            {"heading": s["heading"], "bullets": list(s["bullets"])}
            for s in non_empty
        ],
    }
    body = _release_canonical_body(new_section)

    # Reset Unreleased: drop subsections, keep only heading
    unreleased["subsections"] = []

    # Insert new section after Unreleased
    sections.insert(1, new_section)

    # Serialize. rstrip trailing newlines from preamble so re-stamping is
    # idempotent on the preamble→first-section gap: splitlines() leaves a
    # trailing empty-string element when the preamble ends in a blank line,
    # which combined with the explicit "" separator below would add one
    # extra blank line on every round-trip.
    out_parts = [parsed["preamble"].rstrip("\n"), ""]
    for sec in sections:
        out_parts.append(sec["heading"])
        out_parts.append("")
        for sub in sec["subsections"]:
            out_parts.append(sub["heading"])
            out_parts.extend(sub["bullets"])
            out_parts.append("")
    new_text = "\n".join(out_parts).rstrip() + "\n"
    return (new_text, body)


def _release_stamp_package_json(text: str, version: str) -> str:
    """Rewrite package.json's `version` field to `version`.

    Idempotent: stamping the same version twice yields byte-identical
    output. Preserves two-space indent + trailing newline. Raises
    ValueError if the file is malformed JSON or has no `version` field.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"package.json: malformed JSON ({exc.msg})") from exc
    if "version" not in data:
        raise ValueError("package.json: missing 'version' field")
    data["version"] = version
    out = json.dumps(data, indent=2, ensure_ascii=False)
    if not out.endswith("\n"):
        out += "\n"
    return out


def _release_preflight_tag_clobber(version: str, remote: str) -> None:
    """Refuse if vX.Y.Z tag already exists locally or on remote (spec §10.1 step 5)."""
    tag = f"v{version}"
    local_tags = (
        subprocess.check_output(
            ["git", "tag", "-l", tag],
            text=True,
            cwd=str(_cctally().CHANGELOG_PATH.parent),
        )
        .strip()
        .splitlines()
    )
    if tag in local_tags:
        print(
            f"release: tag {tag} already exists locally; this would clobber",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        out = subprocess.check_output(
            ["git", "ls-remote", "--tags", remote, f"refs/tags/{tag}"],
            text=True,
            stderr=subprocess.DEVNULL,
            cwd=str(_cctally().CHANGELOG_PATH.parent),
        ).strip()
        if out:
            print(
                f"release: tag {tag} already exists on {remote}; this would clobber",
                file=sys.stderr,
            )
            sys.exit(2)
    except subprocess.CalledProcessError:
        # Remote unreachable; the up-to-date preflight would have caught this
        # in the non-dry-run path. Don't refuse on remote-side ambiguity.
        pass


def _release_compute_brew_sha256(version: str) -> str:
    """Download the GH archive tarball for ``version`` and return sha256.

    URL is resolved through :func:`_release_brew_archive_url` (Task 0),
    which honors the ``CCTALLY_RELEASE_BREW_ARCHIVE_URL`` env hook for
    fixture tests. Mirrors the ``CCTALLY_RELEASE_DATE_UTC`` precedent —
    env-only, not surfaced in --help.

    The 30-second timeout is sized for the GH auto-archive endpoint —
    its initial response can lag a few seconds while the tarball is
    materialized server-side. Failures bubble up as ``urllib.error``
    or ``socket.timeout`` to the caller (Phase 6), where they surface
    as a non-zero return that ``--resume`` can retry.
    """
    url = _release_brew_archive_url(version)
    req = urllib.request.Request(
        url, headers={"User-Agent": "cctally-release"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    return hashlib.sha256(data).hexdigest()


def _release_print_gh_fallback(version: str, body: str) -> None:
    """Print a copy-pasteable ``gh release create`` command (spec §9.2).

    Used when the auth probe fails. Prints two commands: first writes the
    notes body to ``/tmp/release-notes-vX.Y.Z.md`` via heredoc, then
    invokes ``gh release create --notes-file <that path>``. Splitting the
    body off into a tmpfile (vs. the original ``<(cat <<'EOF' ... EOF)``
    process-substitution heredoc) keeps the operator from having to escape
    backticks / dollar signs and makes diffing the notes easy if the
    paste doesn't take.

    The heredoc terminator is randomized per invocation
    (``CCTALLY_EOF_<pid>``) so a body that happens to contain a bare
    ``EOF`` line — common in CHANGELOG entries that quote shell snippets
    — does NOT prematurely terminate the heredoc. If the operator's body
    somehow contains a line matching the randomized terminator exactly,
    they need to either edit the body or pick a different terminator
    before pasting; we surface that constraint in the printout.

    Returning the operator to a known-good state means ``release
    v{version} published except GH Release; phase 4 awaits manual
    completion`` — phases 1-3 already landed, so the release IS published
    from the public mirror's perspective; only the GitHub Releases UI
    surface is incomplete.
    """
    is_prerelease = "-" in version
    terminator = f"CCTALLY_EOF_{os.getpid()}"
    notes_path = f"/tmp/release-notes-v{version}.md"
    print(f"release: gh release ⚠ skipped (no auth for {_cctally().PUBLIC_REPO})")
    print()
    print("Run this manually to publish the GitHub Release:")
    print()
    print(f"  cat > {notes_path} <<'{terminator}'")
    print(body)
    print(f"  {terminator}")
    print()
    print(f"  gh release create v{version} \\")
    print(f"    --repo {_cctally().PUBLIC_REPO} \\")
    print(f"    --title v{version} \\")
    if is_prerelease:
        print(f"    --notes-file {notes_path} \\")
        print("    --prerelease")
    else:
        print(f"    --notes-file {notes_path}")
    print()
    print(
        f"(If your CHANGELOG body contains a line that exactly matches the "
        f"terminator '{terminator}', edit the body or change the terminator "
        "before pasting.)"
    )
    print()
    print(
        "(or after running 'gh auth login', re-run "
        "'cctally release --resume' to complete this phase.)"
    )
    print()
    print(
        f"release v{version} published except GH Release; "
        "phase 4 awaits manual completion"
    )


_RELEASE_NPM_POLL_TIMEOUT_S_DEFAULT = 300.0


_RELEASE_NPM_POLL_INTERVAL_S_DEFAULT = 10.0


def _release_npm_poll_timing() -> tuple[float, float]:
    """Return (timeout_s, interval_s) honoring env-hook overrides.

    Hidden env hooks (mirrors ``CCTALLY_RELEASE_DATE_UTC``):
      - CCTALLY_RELEASE_NPM_POLL_TIMEOUT_S
      - CCTALLY_RELEASE_NPM_POLL_INTERVAL_S
    Used by the harness (and pytest) to make Phase 5 fixtures deterministic.
    Not in --help.
    """
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ[name])
        except (KeyError, ValueError):
            return default
    return (
        _f("CCTALLY_RELEASE_NPM_POLL_TIMEOUT_S", _RELEASE_NPM_POLL_TIMEOUT_S_DEFAULT),
        _f("CCTALLY_RELEASE_NPM_POLL_INTERVAL_S", _RELEASE_NPM_POLL_INTERVAL_S_DEFAULT),
    )


def _release_preflight_branch(allow_branch: str | None) -> None:
    """Refuse unless on main or --allow-branch matches.

    Spec §10.1 step 2: branch check fires for both real runs and --dry-run
    (a dry-run on the wrong branch would mislead).
    """
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()
    expected = allow_branch if allow_branch else "main"
    if branch != expected:
        if allow_branch:
            print(
                f"release: refusing to cut from {branch}; allow-branch was {allow_branch}",
                file=sys.stderr,
            )
        else:
            print(
                f"release: refusing to cut from {branch}; "
                f"use --allow-branch {branch} if intentional",
                file=sys.stderr,
            )
        sys.exit(2)

def _release_preflight_clean_tree() -> None:
    """Refuse if working tree is dirty (spec §10.1 step 3)."""
    out = subprocess.check_output(
        ["git", "status", "--porcelain"],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    )
    if out.strip():
        print("release: working tree dirty; commit or stash first", file=sys.stderr)
        sys.exit(2)

def _release_preflight_up_to_date(remote: str) -> None:
    """Refuse if local branch is behind <remote>/<branch>; local-ahead OK.

    Spec §10.1 step 4. Network failure is non-fatal — operator can re-run
    with --resume after fixing connectivity.
    """
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()
    try:
        subprocess.check_call(
            ["git", "fetch", "--quiet", remote, branch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(_cctally().CHANGELOG_PATH.parent),
        )
    except subprocess.CalledProcessError:
        # Network failure — proceed; operator can re-run with --resume.
        return
    local = subprocess.check_output(
        ["git", "rev-parse", branch],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()
    remote_sha = subprocess.check_output(
        ["git", "rev-parse", f"{remote}/{branch}"],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()
    if local == remote_sha:
        return
    base = subprocess.check_output(
        ["git", "merge-base", local, remote_sha],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()
    if base == remote_sha:
        return  # local is strictly ahead — fine.
    print(
        f"release: local {branch} is behind {remote}/{branch}; pull first",
        file=sys.stderr,
    )
    sys.exit(2)

def _release_phase_stamp_done(version: str) -> tuple[bool, str | None]:
    """Phase-1 read-only signal: returns (True, head_sha) when CHANGELOG.md
    on disk AND HEAD's tree both contain a `## [version] - YYYY-MM-DD` header
    line AND HEAD's commit subject is exactly `chore(release): vX.Y.Z`.

    Date is read from the existing CHANGELOG, NOT recomputed from "today" —
    `--resume` after UTC midnight rollover must still detect a stamp written
    yesterday. Returns (False, None) on any miss (file gone, header absent,
    HEAD blob lacks the header). When the header IS present on HEAD but
    HEAD's subject is not the stamp subject — meaning an unrelated commit
    landed on top of the stamp — exits 2 with a diagnostic rather than
    tagging the wrong SHA. Read-only — never mutates.
    """
    expected_prefix = f"## [{version}] - "
    try:
        text = _cctally().CHANGELOG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (False, None)
    if not any(line.startswith(expected_prefix) for line in text.splitlines()):
        return (False, None)
    head_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()
    try:
        blob = subprocess.check_output(
            ["git", "show", "HEAD:CHANGELOG.md"],
            text=True,
            stderr=subprocess.DEVNULL,
            cwd=str(_cctally().CHANGELOG_PATH.parent),
        )
    except subprocess.CalledProcessError:
        return (False, None)
    if not any(line.startswith(expected_prefix) for line in blob.splitlines()):
        return (False, None)
    # HEAD's blob carries the stamp; verify HEAD itself IS the stamp commit.
    # An unrelated commit landed on top of the stamp would still satisfy the
    # blob check (CHANGELOG.md unchanged on top), but tagging that SHA would
    # mis-tag the release. Refuse with a clear diagnostic instead.
    head_subject = subprocess.check_output(
        ["git", "log", "-1", "--format=%s", "HEAD"],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()
    expected_subject = f"chore(release): v{version}"
    if head_subject != expected_subject:
        print(
            f"release: HEAD is not the stamp commit (subject: {head_subject!r}); "
            f"--resume cannot continue. Either checkout the stamp commit or "
            f"revert the on-top commits.",
            file=sys.stderr,
        )
        sys.exit(2)
    # Belt-and-suspenders: package.json's version must also be stamped
    # (Phase 1 co-stamp invariant). When package.json doesn't exist
    # (legacy fixtures, source clones pre-Task-1), skip silently —
    # the CHANGELOG header check above is still authoritative.
    pj_path = _package_json_path()
    if pj_path.exists():
        try:
            pj = json.loads(pj_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return (False, None)
        if pj.get("version") != version:
            return (False, None)
    return (True, head_sha)

def _release_build_stamp_message(
    version: str,
    body: str,
    invocation: str,
    prior_head: str,
    bump_kind: str,
    subsection_counts: dict[str, int],
) -> str:
    """Build the full stamp commit message: private body + `--- public ---`
    block + canonical body (spec §7.1).

    Subject is identical on both surfaces (`chore(release): vX.Y.Z`); the
    public body is the canonical CHANGELOG section body byte-for-byte.
    """
    counts_str = ", ".join(
        f"{name} ({n})" for name, n in subsection_counts.items() if n > 0
    ) or "(none)"
    total = sum(subsection_counts.values())
    private_body = (
        f"chore(release): v{version}\n"
        f"\n"
        f"Stamp release v{version} over {total} [Unreleased] entries.\n"
        f"\n"
        f"Run by `{invocation}` from main at {prior_head[:7]}.\n"
        f"Bump kind: {bump_kind}.\n"
        f"Subsections stamped: {counts_str}.\n"
    )
    public_block = f"--- public ---\nchore(release): v{version}\n\n{body}\n"
    return private_body + "\n" + public_block

def _release_run_phase_stamp(
    version: str,
    remote: str,
    invocation: str,
    bump_kind: str = "(unspecified)",
) -> str:
    """Phase 1 — stamp `[Unreleased]` into a `[X.Y.Z]` section and commit.

    Idempotent: if `_release_phase_stamp_done(version)` reports done,
    short-circuits with the existing HEAD SHA. Otherwise rewrites
    CHANGELOG.md atomically (`os.replace`), stages it, verifies the
    staging cache contains exactly `CHANGELOG.md` (defends against
    operator-prestaged content slipping into the release commit), then
    invokes `git commit -F <msgfile> --cleanup=verbatim` so the
    `### Added` / `### Fixed` headings survive (default cleanup strips
    `#`-prefixed lines as comments). Returns the new commit SHA.
    """
    done, head_sha = _release_phase_stamp_done(version)
    if done:
        print(
            f"release: stamp ✓ (already done — commit {head_sha[:7]})"
        )
        return head_sha

    print(f"release: stamp v{version}")
    today = (
        os.environ.get("CCTALLY_RELEASE_DATE_UTC")
        or dt.datetime.now(dt.timezone.utc).date().isoformat()
    )
    old_text = _cctally().CHANGELOG_PATH.read_text(encoding="utf-8")
    try:
        new_text, body = _release_stamp_changelog(old_text, version, today)
    except ValueError as e:
        # Mirror the dry-run path's contract: refuse with exit 2 when
        # `[Unreleased]` is empty (spec §3 step 4 / §11.4 row 9).
        print(f"release: {e}", file=sys.stderr)
        sys.exit(2)

    # Subsection counts — read from the OLD parse (before the stamp moved
    # bullets out of [Unreleased]); private-body diagnostics only.
    parsed = _release_parse_changelog(old_text)
    counts: dict[str, int] = {}
    if parsed["sections"]:
        for sub in parsed["sections"][0]["subsections"]:
            if sub["bullets"]:
                heading = sub["heading"].replace("###", "").strip()
                counts[heading] = len(sub["bullets"])

    prior_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()

    # Atomic write: write to .tmp.<pid> sibling, then os.replace(). Wrapped
    # in try/finally so a failure between write_text and os.replace doesn't
    # leak a stray .tmp.<pid> file next to CHANGELOG.md.
    tmp = _cctally().CHANGELOG_PATH.with_suffix(f".md.tmp.{os.getpid()}")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, _cctally().CHANGELOG_PATH)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

    # Stage CHANGELOG.md only (`cwd` so the relative path resolves).
    subprocess.check_call(
        ["git", "add", _cctally().CHANGELOG_PATH.name],
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    )

    # Co-stamp package.json (npm channel; Phase 5 reads this version field).
    # Path resolved via _package_json_path() (which derives from
    # _cctally().CHANGELOG_PATH) so tests monkeypatching _cctally().CHANGELOG_PATH to a temp
    # repo automatically see the correct (absent) package.json there.
    # Guarded by `.exists()` so fixture replays for old scenarios
    # pre-dating the npm channel still pass — they have no package.json
    # to stamp.
    pj_path = _package_json_path()
    if pj_path.exists():
        pj_old = pj_path.read_text(encoding="utf-8")
        pj_new = _release_stamp_package_json(pj_old, version)
        pj_path.write_text(pj_new, encoding="utf-8")
        subprocess.check_call(
            ["git", "add", pj_path.name],
            cwd=str(_cctally().CHANGELOG_PATH.parent),
        )

    # Trailer staging guard: refuse if anything else is staged. Defensive
    # — preflight should have caught a dirty index, but operators can race
    # `git add` between preflight and stamp.
    staged = (
        subprocess.check_output(
            ["git", "diff", "--cached", "--name-only"],
            text=True,
            cwd=str(_cctally().CHANGELOG_PATH.parent),
        )
        .strip()
        .splitlines()
    )
    expected_staged = (
        ["CHANGELOG.md", "package.json"]
        if pj_path.exists()
        else ["CHANGELOG.md"]
    )
    if staged != expected_staged:
        print(
            f"release: stamp aborted; expected only {expected_staged} staged, "
            f"got {staged}",
            file=sys.stderr,
        )
        recover_paths = " ".join(expected_staged)
        print(
            "release: to recover, run `git reset HEAD` and "
            f"`git checkout -- {recover_paths}`",
            file=sys.stderr,
        )
        sys.exit(3)

    msg = _release_build_stamp_message(
        version, body, invocation, prior_head, bump_kind, counts
    )
    msg_file = _cctally().CHANGELOG_PATH.parent / f".release-msg.{os.getpid()}.txt"
    msg_file.write_text(msg, encoding="utf-8")
    try:
        subprocess.check_call(
            [
                "git",
                "commit",
                "-F",
                str(msg_file),
                "--cleanup=verbatim",
            ],
            cwd=str(_cctally().CHANGELOG_PATH.parent),
        )
    finally:
        try:
            msg_file.unlink()
        except FileNotFoundError:
            pass

    new_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()
    print(f"release: stamp ✓ (commit {new_sha[:7]})")
    return new_sha

def _release_phase_tag_done(version: str, remote: str) -> bool:
    """Phase-2 read-only signal: tag exists locally AND on `<remote>`.

    Both are required (spec §5.1): a local-only tag means the push step
    still has work; a remote-only tag (rare — implies a manual push)
    is treated as not-done so the local annotation is recreated.
    Read-only — never mutates.
    """
    tag = f"v{version}"
    local = (
        subprocess.check_output(
            ["git", "tag", "-l", tag],
            text=True,
            cwd=str(_cctally().CHANGELOG_PATH.parent),
        )
        .strip()
        .splitlines()
    )
    if tag not in local:
        return False
    try:
        out = subprocess.check_output(
            ["git", "ls-remote", "--tags", remote, f"refs/tags/{tag}"],
            text=True,
            stderr=subprocess.DEVNULL,
            cwd=str(_cctally().CHANGELOG_PATH.parent),
        ).strip()
        return bool(out)
    except subprocess.CalledProcessError:
        return False

def _release_run_phase_tag(version: str, remote: str, stamp_sha: str) -> None:
    """Phase 2 — write annotated tag `vX.Y.Z` and push commit + tag.

    `stamp_sha` is the SHA returned by Phase 1; tagging is pinned to that
    SHA rather than re-reading HEAD, so an unrelated commit landing on top
    between phases never causes a mis-tag (defense in depth — the done-
    signal in `_release_phase_stamp_done` already refuses that scenario).

    Idempotent: short-circuits when `_release_phase_tag_done`. The
    body is re-parsed from CHANGELOG.md and run through
    `_release_canonical_body` so the annotation is byte-identical to
    Phase 1's commit-message public block (body-canonical-three-sources
    invariant, spec §7.4 — same string flows into Phase 4's GH Release
    notes).

    Resume scenarios:
    - Local tag missing, remote tag missing: create + push as normal.
    - Local tag exists, remote tag missing (push failed last run, or
      operator pushed `main` manually): skip `git tag` (would conflict
      with the existing local tag) and push only the tag explicitly.
    - Both present: short-circuited by `_release_phase_tag_done` above.

    Signing: use `-s` only when both `user.signingkey` is set AND
    `tag.gpgsign` is `true`; otherwise `-a` (annotated, unsigned).
    Operators that have signing keys configured but not enabled for
    tags get the unsigned path. Signed tags will have a PGP signature
    block appended to the tag object — the eventual harness scenario
    must strip lines from `-----BEGIN PGP SIGNATURE-----` onward
    before comparing the annotation against the canonical body.

    `--cleanup=verbatim` is required: default cleanup strips
    `#`-prefixed lines, eating every `### Added` / `### Fixed`
    heading. Push uses `--follow-tags` to ship commit + tag in one
    operation, plus an explicit `refs/tags/...:refs/tags/...` push as
    belt-and-suspenders: `--follow-tags` skips tags whose target commit
    is already on the remote (the resume-after-manual-push case), so
    the explicit push is what guarantees the tag actually lands.
    """
    if _release_phase_tag_done(version, remote):
        print(
            f"release: tag ✓ (already done — v{version} on {remote})"
        )
        return

    print(f"release: tag v{version}")
    tag = f"v{version}"

    # Resume-aware: if the local tag already exists from a prior run, skip
    # `git tag` (would fail with "tag already exists") and push only.
    local_tags = (
        subprocess.check_output(
            ["git", "tag", "-l", tag],
            text=True,
            cwd=str(_cctally().CHANGELOG_PATH.parent),
        )
        .strip()
        .splitlines()
    )
    if tag in local_tags:
        print(f"release: tag {tag} exists locally; pushing tag only")
    else:
        # Body — re-parse CHANGELOG so the annotation reuses the canonical
        # body string (matches the public block of Phase 1's commit byte
        # for byte; same string flows into Phase 4's GH Release notes).
        text = _cctally().CHANGELOG_PATH.read_text(encoding="utf-8")
        parsed = _release_parse_changelog(text)
        target_section = next(
            (
                s
                for s in parsed["sections"]
                if s["heading"].lstrip().startswith(f"## [{version}]")
            ),
            None,
        )
        if target_section is None:
            print(
                f"release: cannot find [{version}] section in CHANGELOG.md",
                file=sys.stderr,
            )
            sys.exit(3)
        body = _release_canonical_body(target_section)

        annotation = f"{tag}\n\n{body}\n"
        msg_file = _cctally().CHANGELOG_PATH.parent / f".release-tag-msg.{os.getpid()}.txt"
        try:
            msg_file.write_text(annotation, encoding="utf-8")
            signing_key = subprocess.run(
                ["git", "config", "--get", "user.signingkey"],
                capture_output=True,
                text=True,
                cwd=str(_cctally().CHANGELOG_PATH.parent),
            ).stdout.strip()
            tag_gpgsign = (
                subprocess.run(
                    ["git", "config", "--get", "tag.gpgsign"],
                    capture_output=True,
                    text=True,
                    cwd=str(_cctally().CHANGELOG_PATH.parent),
                )
                .stdout.strip()
                .lower()
                == "true"
            )
            sign_flag = "-s" if (signing_key and tag_gpgsign) else "-a"
            subprocess.check_call(
                [
                    "git",
                    "tag",
                    sign_flag,
                    "-F",
                    str(msg_file),
                    "--cleanup=verbatim",
                    tag,
                    stamp_sha,
                ],
                cwd=str(_cctally().CHANGELOG_PATH.parent),
            )
        finally:
            try:
                msg_file.unlink()
            except FileNotFoundError:
                pass

    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()
    subprocess.check_call(
        ["git", "push", remote, branch, "--follow-tags"],
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    )
    # Belt-and-suspenders: --follow-tags skips tags whose target is already
    # on the remote (e.g., resume after operator pushed `main` manually).
    # Explicit refs-spec always pushes the tag; no-op when both are already
    # on remote (true resume-after-success).
    subprocess.check_call(
        ["git", "push", remote, f"refs/tags/{tag}:refs/tags/{tag}"],
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    )
    print(f"release: tag ✓ (annotated, pushed to {remote})")

def _release_discover_public_clone(args: argparse.Namespace) -> pathlib.Path:
    """Resolve the public-clone path (spec §9.1).

    Priority chain (highest first):
      1. ``--public-clone <path>`` flag.
      2. ``git config --get release.publicClone``.
      3. ``$_cctally().APP_DIR/release-public-clone-path`` plain-text marker file.

    Each source is silently skipped when missing (unset config key, no
    marker file, no flag). Refuses with exit 2 only when ALL three
    sources are absent — operator setup is one-time and explicit, no
    silent fallback to a hard-coded path.
    """
    candidates: list[tuple[str, pathlib.Path]] = []

    if getattr(args, "public_clone", None):
        candidates.append(("--public-clone", pathlib.Path(args.public_clone)))

    # Source 2 — git config. `git config --get` exits 1 when the key is
    # unset; that's the silent-skip path. Other failures (e.g., not in a
    # git repo) also fall through silently — preflight has already
    # established we're inside a repo.
    #
    # Key is `release.publicClone` (camelCase) per git config naming
    # conventions; git 2.46+ rejects underscore-bearing keys at write
    # time, so the spec's informal `release.public_clone` wording maps
    # to this canonical form. Git config lookup is case-insensitive on
    # the trailing variable, so `release.publicclone` works too.
    try:
        out = subprocess.run(
            ["git", "config", "--get", "release.publicClone"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if out:
            candidates.append(
                ("git config release.publicClone", pathlib.Path(out))
            )
    except subprocess.CalledProcessError:
        pass

    # Source 3 — marker file under _cctally().APP_DIR.
    marker = _cctally().APP_DIR / "release-public-clone-path"
    if marker.exists():
        text = marker.read_text(encoding="utf-8").strip()
        if text:
            candidates.append((str(marker), pathlib.Path(text)))

    for _source, path in candidates:
        if (path / ".git").exists():
            return path.resolve()
        # Bare repo — `<path>/HEAD` exists alongside `objects/`, `refs/`.
        if path.is_dir() and (path / "HEAD").exists():
            return path.resolve()
        # Lenient fallthrough — if the path simply exists but doesn't
        # match either layout, return it anyway. The next subprocess
        # (`git -C <path> ...`) will fail loudly with a clear error;
        # pre-validating here would only duplicate that diagnostic.
        if path.exists():
            return path.resolve()

    marker_path = _cctally().APP_DIR / "release-public-clone-path"
    print(
        "release: cannot discover public clone path; pass --public-clone "
        "<path>, set 'release.publicClone' in git config, or write the "
        f"path to {marker_path}",
        file=sys.stderr,
    )
    sys.exit(2)

def _release_discover_brew_clone(
    args: argparse.Namespace,
) -> pathlib.Path | None:
    """Resolve the brew tap clone path. Returns ``None`` if unconfigured.

    Priority chain (mirrors :func:`_release_discover_public_clone`):
      1. ``--brew-clone <path>`` flag.
      2. ``git config --get release.brewClone``.
      3. ``$_cctally().APP_DIR/release-brew-clone-path`` plain-text marker file.

    Unlike :func:`_release_discover_public_clone` (which exits 2 when
    none of the sources resolve), this returns ``None`` — Phase 6 is a
    graceful skip, not a hard refusal, since release channels are
    added incrementally and not every operator has a brew tap clone
    on hand.
    """
    if getattr(args, "brew_clone", None):
        return pathlib.Path(args.brew_clone).expanduser()

    cfg = subprocess.run(
        ["git", "config", "--get", "release.brewClone"],
        capture_output=True,
        text=True,
        check=False,
    )
    if cfg.returncode == 0 and cfg.stdout.strip():
        return pathlib.Path(cfg.stdout.strip()).expanduser()

    marker = _cctally().APP_DIR / "release-brew-clone-path"
    if marker.exists():
        path_str = marker.read_text(encoding="utf-8").strip()
        if path_str:
            return pathlib.Path(path_str).expanduser()

    return None

def _release_phase_brew_done(
    version: str, brew_clone: pathlib.Path
) -> bool:
    """Phase 6 done iff the brew tap **remote** serves the ``vX.Y.Z``
    formula on its default branch AND carries the ``v<version>`` tag.

    Tag presence alone is not enough — ``brew install`` reads the
    formula from the tap's default branch, NOT from the tag, so a
    half-failed push (tag landed, branch did not) would still serve
    the prior formula to users. Mirrors the
    :func:`_release_phase_mirror_done` pattern (Phase 3) and adds a
    branch-tip check on top.

    Three-step check (each gates the next):

      1. Local-file sniff — cheap pre-check; skip the network when
         the formula isn't even staged locally.
      2. ``git ls-remote --tags origin refs/tags/v<version>`` — tag
         must be on the remote.
      3. ``git ls-remote origin refs/heads/<branch>`` SHA == local
         clone's ``HEAD`` SHA — the local formula commit must be the
         remote default-branch tip. After a successful Phase 6 push,
         these match; after a half-failed push (branch fail, tag
         succeed), they diverge.

    Returns ``False`` on any subprocess failure (no origin configured,
    network glitch); the caller proceeds to run the phase, whose own
    push step is independently idempotent.
    """
    formula = brew_clone / "Formula" / "cctally.rb"
    if not formula.exists():
        return False
    if f"/v{version}.tar.gz" not in formula.read_text(encoding="utf-8"):
        return False
    try:
        origin = subprocess.check_output(
            ["git", "-C", str(brew_clone), "remote", "get-url", "origin"],
            text=True,
        ).strip()
        tag_out = subprocess.check_output(
            ["git", "ls-remote", "--tags", origin, f"refs/tags/v{version}"],
            text=True,
        ).strip()
        if not tag_out:
            return False
        local_head = subprocess.check_output(
            ["git", "-C", str(brew_clone), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        branch = subprocess.check_output(
            ["git", "-C", str(brew_clone), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
        ).strip()
        head_out = subprocess.check_output(
            ["git", "ls-remote", origin, f"refs/heads/{branch}"],
            text=True,
        ).strip()
        # ls-remote line format: "<sha>\trefs/heads/<branch>"; empty
        # output means the remote has no such branch (fresh tap).
        remote_head = head_out.split("\t", 1)[0] if head_out else ""
        return remote_head == local_head
    except subprocess.CalledProcessError:
        return False

def _release_phase_mirror_done(
    version: str, public_clone: pathlib.Path
) -> bool:
    """Phase 3 done iff the public clone's ``origin`` carries ``vX.Y.Z``.

    Read-only signal — runs ``git ls-remote`` against the public clone's
    own ``origin`` (the public mirror's URL is the single source of
    truth here, per spec §9.1). Returns ``False`` on any subprocess
    failure (no origin remote configured, network glitch, etc.); the
    caller proceeds to run all three sub-steps, each idempotent on
    its own.
    """
    tag = f"v{version}"
    try:
        public_origin = subprocess.check_output(
            ["git", "-C", str(public_clone), "remote", "get-url", "origin"],
            text=True,
        ).strip()
        out = subprocess.check_output(
            ["git", "ls-remote", "--tags", public_origin, f"refs/tags/{tag}"],
            text=True,
        ).strip()
        return bool(out)
    except subprocess.CalledProcessError:
        return False

def _release_run_phase_mirror(
    version: str, public_clone: pathlib.Path, remote: str
) -> None:
    """Phase 3 — replay private commits onto the public clone, then push
    the branch + tag (spec §9.1).

    Three sub-steps, each its own subprocess; any failure halts with
    exit 3 so ``--resume`` can re-run from the failed step (each
    sub-step is independently idempotent).

      3a. ``bin/cctally-mirror-public --yes <public-clone>`` — replay
          private commits onto the local public clone. ``--yes`` is
          mandatory in non-interactive context (the mirror tool prompts
          ``apply? [y/N]`` otherwise).
      3b. ``git -C <public-clone> push origin <branch>`` — push the
          public-clone branch to public origin. The branch is read
          dynamically via ``git -C <public-clone> rev-parse --abbrev-ref
          HEAD`` rather than hardcoded ``main``, since some operators
          may run their public clone on a non-default branch.
      3c. ``git -C <public-clone> push origin refs/tags/v<version>``
          — push the new tag.

    The ``remote`` arg is currently unused (the public-clone push always
    targets the clone's own ``origin``); kept in the signature for
    parity with ``_release_run_phase_tag`` and possible future
    multi-remote scenarios.
    """
    del remote  # spec §9.1 — public-clone push always targets `origin`.
    if _release_phase_mirror_done(version, public_clone):
        print(
            f"release: mirror ✓ (already done — v{version} on public origin)"
        )
        return

    print("release: mirror push")

    # Locate `bin/cctally-mirror-public` alongside this script. Resolve
    # via __file__ so symlinked invocations (~/.local/bin/cctally) still
    # find the in-repo sibling.
    mirror_tool = (
        pathlib.Path(__file__).resolve().parent / "cctally-mirror-public"
    )
    if not mirror_tool.exists():
        print(
            f"release: cannot find {mirror_tool}",
            file=sys.stderr,
        )
        sys.exit(3)

    # Step 3a — replay private commits onto the public clone.
    rc = subprocess.call(
        [str(mirror_tool), "--yes", "--public-clone", str(public_clone)]
    )
    if rc != 0:
        print(
            f"release: mirror replay (3a) failed (exit {rc}); "
            "see output above",
            file=sys.stderr,
        )
        sys.exit(3)

    # Step 3b — push the public-clone branch to public origin. The branch
    # is read dynamically (don't hardcode `main`).
    try:
        branch = subprocess.check_output(
            ["git", "-C", str(public_clone), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
        ).strip()
    except subprocess.CalledProcessError as e:
        print(
            f"release: mirror branch lookup failed (exit {e.returncode})",
            file=sys.stderr,
        )
        sys.exit(3)
    rc = subprocess.call(
        ["git", "-C", str(public_clone), "push", "origin", branch]
    )
    if rc != 0:
        print(
            f"release: mirror push branch (3b) failed (exit {rc})",
            file=sys.stderr,
        )
        sys.exit(3)

    # Step 3c — push the new tag.
    tag = f"v{version}"
    rc = subprocess.call(
        [
            "git", "-C", str(public_clone),
            "push", "origin", f"refs/tags/{tag}",
        ]
    )
    if rc != 0:
        print(
            f"release: mirror push tag (3c) failed (exit {rc})",
            file=sys.stderr,
        )
        sys.exit(3)

    print(f"release: mirror ✓ (v{version} propagated)")

def _release_phase_gh_done(version: str) -> bool:
    """Phase-4 read-only signal: ``gh release view vX.Y.Z`` returns 0.

    Read-only — issues a `gh release view` against the public repo and
    treats exit 0 as "release exists." Suppresses stderr/stdout because
    the call is purely a probe; the operator-visible state is recorded
    in ``_release_run_phase_gh``'s own logging. A never-authed operator
    sees this return False (gh exits non-zero on auth error), the helper
    falls through to its own auth probe, and the fallback path prints a
    copy-pasteable command — same UX whether the release simply doesn't
    exist yet or whether gh can't see it.
    """
    rc = subprocess.call(
        ["gh", "release", "view", f"v{version}", "--repo", _cctally().PUBLIC_REPO],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return rc == 0

def _release_phase_npm_done(version: str) -> bool:
    """True iff ``cctally@<version>`` is already published on npm.

    Probes via ``npm view cctally@<v> dist.tarball --json``. Returns
    True when the command succeeds AND stdout is a JSON-quoted
    registry.npmjs.org URL. False on any other condition (timeout,
    npm not on PATH, version absent, etc.). Used as the idempotency
    short-circuit for Phase 5 (parity with ``_release_phase_gh_done``).
    """
    try:
        result = subprocess.run(
            ["npm", "view", f"cctally@{version}", "dist.tarball", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    if result.returncode != 0:
        return False
    out = result.stdout.strip()
    return (
        out.startswith('"')
        and out.endswith('"')
        and "registry.npmjs.org" in out
    )

def _release_run_phase_gh(version: str, body: str) -> int:
    """Phase 4 — ``gh release create`` with auth fallback (spec §9.2).

    Returns:
      - ``0`` on successful publish OR auth-fallback (don't fail the
        whole release on missing gh auth — phases 1-3 already succeeded;
        Phase 4 is polish).
      - ``3`` on hard failure of ``gh release create`` after auth was
        confirmed OK (network glitch, server-side rejection, etc.); the
        operator can address it then re-run ``cctally release --resume``.

    Idempotent: ``_release_phase_gh_done`` short-circuits when the
    release already exists. The body is passed in (not re-fetched here);
    the caller is responsible for extracting it from CHANGELOG so the
    body-canonical-three-sources invariant (spec §7.4) is preserved
    across phases 1, 2, and 4.

    Auth-mismatch semantics (spec §9.3): if ``gh release view`` finds
    an existing release whose body differs from the current CHANGELOG
    section, treat the existing as authoritative — no ``gh release
    edit`` rewrite. Body divergence after the fact is a separate
    concern that warrants explicit operator action.
    """
    if _release_phase_gh_done(version):
        url = f"https://github.com/{_cctally().PUBLIC_REPO}/releases/tag/v{version}"
        print(f"release: gh release ✓ (already published — {url})")
        return 0

    print("release: gh release")

    # Auth probe — both must succeed for the operator to be able to
    # write to the public repo.
    auth_status_ok = (
        subprocess.call(
            ["gh", "auth", "status", "--hostname", "github.com"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        == 0
    )
    repo_access_ok = (
        subprocess.call(
            ["gh", "api", f"repos/{_cctally().PUBLIC_REPO}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        == 0
    )
    if not (auth_status_ok and repo_access_ok):
        _release_print_gh_fallback(version, body)
        return 0

    # Happy path. Notes go through a tmpfile to dodge shell escaping.
    notes_file = pathlib.Path(tempfile.gettempdir()) / (
        f"release-notes-v{version}-{os.getpid()}.md"
    )
    notes_file.write_text(body, encoding="utf-8")
    try:
        cmd = [
            "gh", "release", "create", f"v{version}",
            "--repo", _cctally().PUBLIC_REPO,
            "--title", f"v{version}",
            "--notes-file", str(notes_file),
        ]
        if "-" in version:
            cmd.append("--prerelease")
        rc = subprocess.call(cmd)
        if rc != 0:
            print(
                f"release: gh release create failed (exit {rc}); "
                "--resume to retry",
                file=sys.stderr,
            )
            return 3
    finally:
        notes_file.unlink(missing_ok=True)

    url = f"https://github.com/{_cctally().PUBLIC_REPO}/releases/tag/v{version}"
    print(f"release: gh release ✓ ({url})")
    return 0

def _release_run_phase_npm(
    version: str,
    public_clone: pathlib.Path,
    *,
    dist_tag: str,
) -> int:
    """Phase 5 — wait for the public-repo GHA workflow to publish ``cctally@<v>``.

    Phase 3 pushes ``v<version>`` to ``omrikais/cctally``; the workflow at
    ``.github/workflows/release-npm.yml`` fires on tag-push and runs
    ``npm publish --provenance`` via OIDC trusted publisher (no NPM_TOKEN,
    no operator 2FA round-trip — fixes the passkey-in-subprocess failure
    mode where npm 2FA blocks ``npm publish`` from a non-interactive
    subprocess).

    Phase 5 here is observation-only: poll ``npm view cctally@<v>`` until
    it appears, with timeout. ``cctally`` never invokes ``npm publish``
    locally anymore — Trusted Publisher binds the right to publish to the
    public-repo workflow, not to the operator's `npm login` token.

    Returns ``0`` on observed success OR poll-timeout (soft-success: phases
    1-4 landed; the workflow is either succeeding or visibly failing on
    github.com; ``--resume`` re-checks the registry).

    Timing overridable via ``CCTALLY_RELEASE_NPM_POLL_TIMEOUT_S`` and
    ``CCTALLY_RELEASE_NPM_POLL_INTERVAL_S`` env vars.
    """
    print(f"phase 5: await npm publish via GHA (tag={dist_tag})")
    if _release_phase_npm_done(version):
        print(f"  cctally@{version} already on npm — skipping.")
        return 0

    timeout_s, interval_s = _release_npm_poll_timing()
    deadline = time.monotonic() + timeout_s
    while True:
        if _release_phase_npm_done(version):
            print(f"  cctally@{version} on npm registry ✓")
            return 0
        if time.monotonic() >= deadline:
            print(
                f"\n  timed out after {timeout_s:.0f}s waiting for "
                f"cctally@{version} on npm. The GHA workflow may still be "
                f"running or have failed — check:\n"
                f"    https://github.com/{_cctally().PUBLIC_REPO}/actions\n"
                f"  Re-run `cctally release --resume` once the workflow "
                f"completes, or for emergency manual publish:\n"
                f"    cd {public_clone} && npm publish --access public "
                f"--tag {dist_tag}\n",
                file=sys.stderr,
            )
            return 0
        time.sleep(interval_s)

def _release_run_phase_brew(
    version: str,
    brew_clone: pathlib.Path | None,
    allow_downgrade: bool = False,
) -> int:
    """Phase 6 — render ``Formula/cctally.rb`` and push to the brew tap.

    Idempotent. Pre-releases are skipped at the cmd_release call site —
    this function never runs for them.

    Phase semantics:
      - ``brew_clone is None`` — graceful skip (no error). Operator can
        opt in later by setting ``release.brewClone`` in git config.
      - Done-signal short-circuit — prints "already at vX.Y.Z" and
        returns 0 when ``Formula/cctally.rb`` already references this
        version (idempotency under ``--resume``).
      - Dirty working tree — refuses with exit 2 and points the operator
        at ``--resume``.
      - **Monotonic-version gate (issue #30).** Refuses with exit 2 when
        the existing on-disk formula's URL pins a *higher* SemVer than
        ``version``. Catches the f02b2f1 regression class — operator
        runs Phase 6 from a stale CHANGELOG / fixture leak / accidental
        old branch and would otherwise silently write a lower version
        over a higher one. Override via ``allow_downgrade=True``
        (operator-driven, for genuine yank/revert cases).
      - Push failure — auth-fallback parity with Phase 4: prints the
        exact recovery command and returns 0. Phases 1-5 already
        succeeded, the release IS published from the user's
        perspective; the brew tap is the third channel and treated as
        polish.

    Returns:
      - ``0`` on success, graceful skip, idempotent short-circuit, OR
        push fallback.
      - ``2`` on dirty-working-tree refusal OR downgrade-gate refusal.
    """
    print("phase 6: brew formula bump")
    if brew_clone is None:
        print(
            "  brew tap clone not configured. Set with:\n"
            "    git config release.brewClone /path/to/homebrew-cctally\n"
            "  Skipping phase 6 (no error).",
            file=sys.stderr,
        )
        return 0
    if _release_phase_brew_done(version, brew_clone):
        print(f"  Formula/cctally.rb already at v{version} on tap — skipping.")
        return 0

    # Refuse on dirty working tree — we're about to write the formula
    # and commit; mixing operator-staged work into our commit is a
    # footgun. Resume after the operator resolves.
    status = subprocess.run(
        ["git", "-C", str(brew_clone), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    if status.stdout.strip():
        print(
            f"  brew clone has uncommitted changes:\n{status.stdout}\n"
            "  Resolve and re-run `cctally release --resume`.",
            file=sys.stderr,
        )
        return 2

    formula_path = brew_clone / "Formula" / "cctally.rb"
    local_at_version = (
        formula_path.exists()
        and f"/v{version}.tar.gz"
        in formula_path.read_text(encoding="utf-8")
    )

    if local_at_version:
        # Resume after a push failure (the done-check verifies remote
        # tag, so we only reach here when the local commit is in place
        # but the tap origin hasn't seen it). Skip render + commit; go
        # straight to (re)tag + push. Re-rendering would no-op the
        # commit (`git commit` exits 1 with nothing to commit), so
        # short-circuiting is also what keeps the function tidy.
        print(
            f"  local formula already at v{version}; re-pushing to tap…"
        )
    else:
        # Monotonic-version gate (issue #30). The brew tap regressed
        # from v1.3.0 → v1.0.0 twice in one day via this code path; the
        # equality fingerprint above (`local_at_version`) is False on a
        # downgrade, so without this gate we'd silently overwrite a
        # higher version. Compare the existing formula's URL-pinned
        # SemVer against `version` (SemVer-aware so prereleases sort
        # below their stable counterpart per §11.4); refuse with exit 2
        # when the on-disk version is strictly higher. Unparseable
        # formulas are treated as unversioned and allowed through.
        if formula_path.exists():
            existing_text = formula_path.read_text(encoding="utf-8")
            existing_v = _release_extract_formula_version(existing_text)
            if existing_v is not None:
                try:
                    existing_key = _release_semver_sort_key(
                        _release_parse_semver(existing_v)
                    )
                    target_key = _release_semver_sort_key(
                        _release_parse_semver(version)
                    )
                except ValueError:
                    existing_key = target_key = None
                if (
                    existing_key is not None
                    and target_key is not None
                    and existing_key > target_key
                    and not allow_downgrade
                ):
                    print(
                        f"  refuse: existing formula pins v{existing_v}, "
                        f"target is v{version} (downgrade).\n"
                        "  Common causes: stale CHANGELOG.md in this clone, "
                        "fixture leak into a real `release.brewClone`, or an "
                        "accidental old branch.\n"
                        "  Verify intent, then re-run with "
                        "`--allow-formula-downgrade` to override "
                        "(yank / revert cases). See issue #30.",
                        file=sys.stderr,
                    )
                    return 2
                if (
                    existing_key is not None
                    and target_key is not None
                    and existing_key > target_key
                    and allow_downgrade
                ):
                    print(
                        f"  WARNING: writing v{version} over existing v{existing_v} "
                        "(--allow-formula-downgrade); intentional yank?",
                        file=sys.stderr,
                    )
        print(f"  computing sha256 of v{version} archive…")
        sha = _release_compute_brew_sha256(version)

        template = _homebrew_template_path().read_text(encoding="utf-8")
        rendered = (
            template
            .replace("<<VERSION>>", version)
            .replace("<<SHA256>>", sha)
        )
        formula_path.parent.mkdir(parents=True, exist_ok=True)
        formula_path.write_text(rendered, encoding="utf-8")

        subprocess.run(
            ["git", "-C", str(brew_clone), "add", "Formula/cctally.rb"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(brew_clone), "commit", "-m",
             f"chore(formula): cctally {version}"],
            check=True,
        )
    # Tag is best-effort — re-running after a partial publish should
    # not fail just because the tag already exists locally.
    #
    # Annotated form with `-m` (issue #25): plain `git tag <name>` is
    # silently upgraded to `git tag -s <name>` under operator-global
    # `tag.gpgsign=true` and demands a message via editor. The release
    # script has no editor stdin, so git aborts with `fatal: no tag
    # message?` — the atomic push refspec then fails with "src refspec
    # does not match any" because the local tag was never created, and
    # the auth-fallback branch below silently swallows the failure as
    # exit 0. Mirrors Phase 2's signing detection (signing_key +
    # tag.gpgsign → -s, else fall back), with one defensive divergence:
    # the fallback uses --no-sign (not bare -a) so the tag still lands
    # under tag.gpgsign=true without a usable signing key configured.
    # Brew install reads the formula off the tap's default branch, not
    # the tag, so signing the tap tag is operationally moot — it exists
    # for history bookkeeping and atomic-push transport.
    signing_key = subprocess.run(
        ["git", "-C", str(brew_clone), "config", "--get", "user.signingkey"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    tag_gpgsign = (
        subprocess.run(
            ["git", "-C", str(brew_clone), "config", "--get", "tag.gpgsign"],
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .lower()
        == "true"
    )
    sign_flag = "-s" if (signing_key and tag_gpgsign) else "--no-sign"
    subprocess.run(
        ["git", "-C", str(brew_clone), "tag", sign_flag, "-a", "-m",
         f"cctally v{version}", f"v{version}"],
        check=False,
    )
    # Single ATOMIC push of branch + tag in one transaction — the
    # remote either accepts both refs or neither (server-side atomic
    # push, `--atomic`). Avoids the half-failed-push asymmetry that a
    # split branch-push + tag-push pair admits: a tag landing without
    # the branch landing would leave `brew install` serving the OLD
    # formula off the tap's default branch even though the remote
    # carries the new tag. Tag refspec is explicit (`src:dst`) for
    # atomic-push semantics — `--atomic` requires named refs, not the
    # implicit `--follow-tags` path.
    push = subprocess.run(
        ["git", "-C", str(brew_clone), "push", "--atomic", "origin",
         "HEAD", f"refs/tags/v{version}:refs/tags/v{version}"],
        check=False,
    )
    if push.returncode != 0:
        # If the local tag isn't there (e.g., the operator hit the
        # tag.gpgsign edge case from issue #25 even with the fix), the
        # plain push refspec fails. Surface a tag-create fallback in
        # the hint so copy-paste recovery is self-contained.
        print(
            f"\n  push failed. Manual recovery:\n"
            f"    # If local tag v{version} is missing (e.g., gpgsign issue):\n"
            f"    git -C {brew_clone} tag --no-sign -a -m \"cctally v{version}\" v{version}\n"
            f"    # Then push:\n"
            f"    git -C {brew_clone} push --atomic origin HEAD "
            f"refs/tags/v{version}:refs/tags/v{version}\n",
            file=sys.stderr,
        )
        return 0  # auth-fallback semantics; parity with Phase 4.

    return 0

def _release_extract_body_from_changelog(version: str) -> str:
    """Re-read the canonical body for ``version`` from CHANGELOG.md.

    Same parse + canonicalize path as Phases 1 and 2 (spec §7.4 — the
    body-canonical-three-sources invariant). Refuses with exit 3 if
    the section isn't found; ``--resume`` is the recovery once the
    operator re-stamps.
    """
    text = _cctally().CHANGELOG_PATH.read_text(encoding="utf-8")
    parsed = _release_parse_changelog(text)
    section = next(
        (
            s for s in parsed["sections"]
            if s["heading"].startswith(f"## [{version}]")
        ),
        None,
    )
    if section is None:
        print(
            f"release: cannot find [{version}] section in CHANGELOG.md",
            file=sys.stderr,
        )
        sys.exit(3)
    return _release_canonical_body(section)


def cmd_release(args: argparse.Namespace) -> int:
    """Issue #24 — release automation entry point.

    Phases (each idempotent; subsequent tasks land the real implementations):
      1. Stamp CHANGELOG.md.
      2. Annotated tag, push --follow-tags.
      3. Mirror push (replay → push branch → push tag).
      4. GitHub Release create.

    --resume infers vX.Y.Z from the latest CHANGELOG header.
    """
    # Args validation (spec §10.1 step 1).
    if args.resume and (args.kind or args.bump):
        print(
            "release: --resume is mutually exclusive with kind / --bump",
            file=sys.stderr,
        )
        return 2
    if not args.resume and not args.kind:
        print(
            "release: missing kind (patch|minor|major|prerelease|finalize)",
            file=sys.stderr,
        )
        return 2
    if args.bump and args.kind != "prerelease":
        print("release: --bump valid only with `prerelease`", file=sys.stderr)
        return 2

    # Preflight (spec §10.1).
    _release_preflight_branch(args.allow_branch)
    if not args.dry_run:
        _release_preflight_clean_tree()
    if not args.dry_run and not args.resume:
        _release_preflight_up_to_date(args.remote)

    # Determine target version.
    current = _cctally()._release_read_latest_release_version()
    current_v = current[0] if current else None
    if args.resume:
        if current_v is None:
            print(
                "release: no in-progress release found; "
                "CHANGELOG has no release header",
                file=sys.stderr,
            )
            return 2
        next_v = current_v
    else:
        try:
            next_v = _release_compute_next_version(
                current_v, args.kind, args.bump, args.prerelease_id
            )
        except ValueError as e:
            # Pass through the helper's wording verbatim — tests in Tasks 2+3
            # match the prefix substring; we're a transparent renderer here.
            print(f"release: {e}", file=sys.stderr)
            return 2

    if not args.resume:
        _release_preflight_tag_clobber(next_v, args.remote)

    # Dry-run path.
    if args.dry_run:
        return _release_dry_run(args, current_v, next_v)

    # Resume-already-complete short-circuit (spec §5.5). Only when
    # `--resume`: if all four phase signals report done, exit 0
    # immediately with `already published` — don't call any phase
    # helper. The mirror signal needs the public clone path resolved
    # first; if the operator's local clone is gone (laptop restored
    # from backup, marker deleted, etc.) but the gh release exists,
    # we trust the gh release as proof that phase 3 must have
    # succeeded earlier — phase 4 is the LAST phase, so its presence
    # implies phase 3 landed. That's the only path where we accept a
    # `mirror_done = True` without a successful probe. Partial-publish
    # cases (`gh_done=False`) skip the discovery probe entirely and
    # fall through to the phase loop, where discovery will surface
    # exit 2 with the right error UX. `--no-publish` resume skips the
    # phase-3 / phase-4 portion of the gate.
    if args.resume:
        stamp_done, _ = _release_phase_stamp_done(next_v)
        tag_done = _release_phase_tag_done(next_v, args.remote)
        if args.no_publish:
            all_done = stamp_done and tag_done
        else:
            gh_done = _release_phase_gh_done(next_v)
            if gh_done:
                # gh release exists — phase 4 is the last phase, so
                # phase 3 MUST have landed. Probe mirror as best-effort:
                # success confirms; discovery failure trusts gh.
                try:
                    public_clone = _release_discover_public_clone(args)
                    mirror_done = _release_phase_mirror_done(
                        next_v, public_clone
                    )
                except SystemExit:
                    print(
                        f"release v{next_v}: public clone not discoverable "
                        "for mirror probe; trusting gh release existence "
                        "as proof of phase 3 completion",
                        file=sys.stderr,
                    )
                    mirror_done = True
            else:
                mirror_done = False
            # Phases 5 + 6: include npm and brew in the all-done
            # computation so a fully-shipped multi-channel release
            # short-circuits with `already published`. `--skip-npm` /
            # `--skip-brew` operator-escape-hatches count as "done"
            # for gate purposes — if the operator opted out, the
            # channel is not pending. Pre-releases skip Phase 6
            # categorically (Homebrew tracks stable only), so
            # `is_prerelease` collapses brew_done to True.
            npm_done = args.skip_npm or _release_phase_npm_done(next_v)
            is_prerelease = "-" in next_v
            if args.skip_brew or is_prerelease:
                brew_done = True
            else:
                brew_clone = _release_discover_brew_clone(args)
                brew_done = (
                    brew_clone is not None
                    and _release_phase_brew_done(next_v, brew_clone)
                )
            all_done = (
                stamp_done and tag_done and mirror_done and gh_done
                and npm_done and brew_done
            )
        if all_done:
            print(f"release v{next_v} already published")
            return 0

    # Real flow — phase-table style. Each helper carries its own
    # signal-done check + short-circuit (spec §5.1), so cmd_release
    # is just an ordered call sequence with no per-phase branching.
    invocation = f"cctally-release {args.kind or '--resume'}"
    bump_kind = args.kind or "resume"

    # Phase 1 — stamp.
    stamp_sha = _release_run_phase_stamp(
        next_v, args.remote, invocation, bump_kind
    )

    # Phase 2 — tag.
    _release_run_phase_tag(next_v, args.remote, stamp_sha)

    if args.no_publish:
        print(
            f"release v{next_v} stamped + tagged; "
            "--no-publish set; phases 3-6 skipped"
        )
        return 0

    # Phase 3 — mirror push (replay → push branch → push tag).
    public_clone = _release_discover_public_clone(args)
    _release_run_phase_mirror(next_v, public_clone, args.remote)

    # Phase 4 — gh release create (auth-fallback returns 0 to keep the
    # release "published" from phases 1-3's perspective; spec §9.2).
    body = _release_extract_body_from_changelog(next_v)
    rc = _release_run_phase_gh(next_v, body)
    if rc != 0:
        return rc

    # Compute channel-publishing variables once. `is_prerelease` gates
    # both Phase 5's npm dist-tag and Phase 6's run/skip decision; the
    # `-` test holds because SemVer prereleases always carry a hyphen
    # (e.g. `1.2.0-rc.1`). Done up-front so each phase reads cleanly.
    is_prerelease = "-" in next_v
    dist_tag = "next" if is_prerelease else "latest"

    # Phase 5 — await npm publish via the public-repo GHA workflow
    # (release-npm.yml), which fires on the tag pushed in Phase 3. Phase 5
    # here is observation-only (poll `npm view` with timeout); poll-timeout
    # returns 0 — the workflow runs independently on github.com, and
    # `--resume` re-checks the registry. `--skip-npm` is the operator
    # escape hatch for ad-hoc cuts.
    if args.skip_npm:
        print("phase 5: npm skipped (--skip-npm)")
    else:
        rc = _release_run_phase_npm(next_v, public_clone, dist_tag=dist_tag)
        if rc != 0:
            return rc

    # Phase 6 — brew formula bump (graceful skip when `release.brewClone`
    # is unconfigured; pre-releases skipped categorically — Homebrew
    # users track stable versions only). `--skip-brew` is the operator
    # escape hatch; idempotent under `--resume`.
    if args.skip_brew or is_prerelease:
        suffix = " (pre-release)" if is_prerelease else ""
        print(f"phase 6: brew skipped{suffix}")
    else:
        brew_clone = _release_discover_brew_clone(args)
        rc = _release_run_phase_brew(
            next_v,
            brew_clone,
            allow_downgrade=args.allow_formula_downgrade,
        )
        if rc != 0:
            return rc

    print(f"\nrelease v{next_v} published")
    return 0


def _release_dry_run(
    args: argparse.Namespace, current_v: str | None, next_v: str
) -> int:
    """Print what the release would do; mutate nothing (spec §10.2).

    Returns 0 on a clean dry-run; 2 if the stamp itself would refuse
    (missing CHANGELOG or empty [Unreleased]). Refusal paths print a
    `(would refuse: ...)` line before returning so the operator sees why.
    """
    today = (
        os.environ.get("CCTALLY_RELEASE_DATE_UTC")
        or dt.datetime.now(dt.timezone.utc).date().isoformat()
    )
    print(
        f"release dry-run: v{current_v or '(none)'} → v{next_v} "
        f"({args.kind or 'resume'} bump)"
    )
    print()
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        text=True,
        cwd=str(_cctally().CHANGELOG_PATH.parent),
    ).strip()
    print(f"Branch:        {branch}")
    print(f"Remote:        {args.remote}")
    print("Working tree:  clean")
    print(f"Resume:        {'yes' if args.resume else 'no'}")
    print()
    # Phase 1 — stamp diff preview.
    print("Phase 1 — Stamp CHANGELOG.md")
    print("─" * 41)
    if not args.resume:
        try:
            old_text = _cctally().CHANGELOG_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"(would refuse: CHANGELOG.md not found at {_cctally().CHANGELOG_PATH})")
            return 2
        try:
            new_text, body = _release_stamp_changelog(old_text, next_v, today)
        except ValueError as e:
            print(f"(would refuse: {e})")
            return 2
        import difflib

        diff = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile="a/CHANGELOG.md",
            tofile="b/CHANGELOG.md",
            n=3,
        )
        sys.stdout.write("".join(diff))
    else:
        body = "(resume — stamp already done)"
    print()
    # Phase 2 — tag annotation preview.
    print(f"Phase 2 — Tag v{next_v}")
    print("─" * 41)
    print("Annotation:")
    print(f"v{next_v}")
    print()
    print(body)
    print()
    # Phase 3 — mirror push plan.
    print("Phase 3 — Mirror push")
    print("─" * 41)
    if args.no_publish:
        print("(skipped under --no-publish)")
    else:
        print("Would invoke (3 sub-steps):")
        print("  bin/cctally-mirror-public --yes <public-clone>")
        print("  git -C <public-clone> push origin <current-branch>")
        print(f"  git -C <public-clone> push origin refs/tags/v{next_v}")
    print()
    # Phase 4 — GitHub Release plan.
    print("Phase 4 — GitHub Release")
    print("─" * 41)
    if args.no_publish:
        print("(skipped under --no-publish)")
    else:
        prerelease_flag = " --prerelease" if "-" in next_v else ""
        print("Would invoke:")
        print(f"  gh release create v{next_v} --repo {_cctally().PUBLIC_REPO} \\")
        print(
            f"    --title v{next_v} --notes-file <body>{prerelease_flag}"
        )
        print()
        print("Body (also used for Phase 4 GH Release notes):")
        print(body)
    print()
    # Phase 5 — npm publish plan.
    print("Phase 5 — npm publish")
    print("─" * 41)
    if args.no_publish or getattr(args, "skip_npm", False):
        reason = "--no-publish" if args.no_publish else "--skip-npm"
        print(f"(skipped under {reason})")
    else:
        dist_tag = "next" if "-" in next_v else "latest"
        print(
            f"Would await: cctally@{next_v} on npmjs.org via GHA workflow "
            f"(release-npm.yml in public repo; tag={dist_tag})"
        )
    print()
    # Phase 6 — brew formula bump plan.
    print("Phase 6 — brew formula bump")
    print("─" * 41)
    if args.no_publish or getattr(args, "skip_brew", False):
        reason = "--no-publish" if args.no_publish else "--skip-brew"
        print(f"(skipped under {reason})")
    elif "-" in next_v:
        print(f"(skipped: pre-release v{next_v})")
    else:
        print(f"Would bump: Formula/cctally.rb in <brew-clone> to v{next_v}")
        print(
            f"  url:    https://github.com/{_cctally().PUBLIC_REPO}/archive/"
            f"refs/tags/v{next_v}.tar.gz"
        )
        print("  sha256: <would compute by downloading at run time>")
    print()
    print("dry-run complete; no state mutated; exit 0")
    return 0
