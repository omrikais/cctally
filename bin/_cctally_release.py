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
import sys
import urllib.request

from _lib_semver import (
    _SEMVER_NUM,
    _release_compute_next_version,
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
    _cctally()._release_preflight_branch(args.allow_branch)
    if not args.dry_run:
        _cctally()._release_preflight_clean_tree()
    if not args.dry_run and not args.resume:
        _cctally()._release_preflight_up_to_date(args.remote)

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
        stamp_done, _ = _cctally()._release_phase_stamp_done(next_v)
        tag_done = _cctally()._release_phase_tag_done(next_v, args.remote)
        if args.no_publish:
            all_done = stamp_done and tag_done
        else:
            gh_done = _cctally()._release_phase_gh_done(next_v)
            if gh_done:
                # gh release exists — phase 4 is the last phase, so
                # phase 3 MUST have landed. Probe mirror as best-effort:
                # success confirms; discovery failure trusts gh.
                try:
                    public_clone = _cctally()._release_discover_public_clone(args)
                    mirror_done = _cctally()._release_phase_mirror_done(
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
            npm_done = args.skip_npm or _cctally()._release_phase_npm_done(next_v)
            is_prerelease = "-" in next_v
            if args.skip_brew or is_prerelease:
                brew_done = True
            else:
                brew_clone = _cctally()._release_discover_brew_clone(args)
                brew_done = (
                    brew_clone is not None
                    and _cctally()._release_phase_brew_done(next_v, brew_clone)
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
    invocation = f"cctally release {args.kind or '--resume'}"
    bump_kind = args.kind or "resume"

    # Phase 1 — stamp.
    stamp_sha = _cctally()._release_run_phase_stamp(
        next_v, args.remote, invocation, bump_kind
    )

    # Phase 2 — tag.
    _cctally()._release_run_phase_tag(next_v, args.remote, stamp_sha)

    if args.no_publish:
        print(
            f"release v{next_v} stamped + tagged; "
            "--no-publish set; phases 3-6 skipped"
        )
        return 0

    # Phase 3 — mirror push (replay → push branch → push tag).
    public_clone = _cctally()._release_discover_public_clone(args)
    _cctally()._release_run_phase_mirror(next_v, public_clone, args.remote)

    # Phase 4 — gh release create (auth-fallback returns 0 to keep the
    # release "published" from phases 1-3's perspective; spec §9.2).
    body = _cctally()._release_extract_body_from_changelog(next_v)
    rc = _cctally()._release_run_phase_gh(next_v, body)
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
        rc = _cctally()._release_run_phase_npm(next_v, public_clone, dist_tag=dist_tag)
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
        brew_clone = _cctally()._release_discover_brew_clone(args)
        rc = _cctally()._release_run_phase_brew(
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
