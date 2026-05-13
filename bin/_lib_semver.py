"""Pure SemVer primitives — parse, format, bump-compute, sort-key.

Eager-imported from bin/cctally to back release-flow internals and the
update-banner version-compare predicate. Zero I/O, zero module-level
side effects; safe to import from any context (script, SourceFileLoader,
compile+exec).

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import re

# Exported as a building block: bin/cctally's RELEASE_HEADER_RE and
# _cctally_release's _FORMULA_VERSION_RE both reuse this numeric-component
# pattern for SemVer matching.
_SEMVER_NUM = r'(?:0|[1-9]\d*)'

_SEMVER_RE = re.compile(
    rf'^({_SEMVER_NUM})\.({_SEMVER_NUM})\.({_SEMVER_NUM})'
    rf'(?:-([a-zA-Z][a-zA-Z0-9-]*)\.({_SEMVER_NUM}))?$'
)


def _release_parse_semver(s: str) -> tuple[int, int, int, str | None, int | None]:
    """Parse SemVer; raises ValueError on malformed input."""
    m = _SEMVER_RE.match(s)
    if not m:
        raise ValueError(f"invalid semver: {s!r}")
    major, minor, patch, prerelease_id, prerelease_n = m.groups()
    return (
        int(major),
        int(minor),
        int(patch),
        prerelease_id,
        int(prerelease_n) if prerelease_n is not None else None,
    )


def _release_format_semver(
    major: int, minor: int, patch: int,
    prerelease_id: str | None = None, prerelease_n: int | None = None,
) -> str:
    base = f"{major}.{minor}.{patch}"
    if prerelease_id is None:
        return base
    return f"{base}-{prerelease_id}.{prerelease_n}"


def _release_compute_next_version(
    current: str | None, kind: str, bump: str | None, prerelease_id: str,
) -> str:
    """Pure function. Implements bump rules from spec Section 4.4."""
    if current is None:
        # First-ever release. Treat as 0.0.0 base.
        cur_maj, cur_min, cur_pat, cur_id, cur_n = 0, 0, 0, None, None
    else:
        cur_maj, cur_min, cur_pat, cur_id, cur_n = _release_parse_semver(current)
    is_prerelease = cur_id is not None

    if kind == "finalize":
        if not is_prerelease:
            raise ValueError("cannot finalize: current version is not a prerelease")
        return _release_format_semver(cur_maj, cur_min, cur_pat)

    if kind == "prerelease":
        if is_prerelease:
            if bump is not None:
                raise ValueError("--bump invalid when current version is a prerelease; rc counter increments only")
            return _release_format_semver(cur_maj, cur_min, cur_pat, cur_id, cur_n + 1)
        if bump is None:
            raise ValueError("--bump required for first prerelease from stable version")
        # Apply bump kind to current stable, then attach -<id>.1
        nxt = _release_compute_next_version(current or "0.0.0", bump, None, prerelease_id)
        nxt_maj, nxt_min, nxt_pat, _, _ = _release_parse_semver(nxt)
        return _release_format_semver(nxt_maj, nxt_min, nxt_pat, prerelease_id, 1)

    if is_prerelease:
        raise ValueError("current version is a prerelease; run 'cctally release finalize' first or use --bump in a prerelease bump")

    if kind == "patch":
        return _release_format_semver(cur_maj, cur_min, cur_pat + 1)
    if kind == "minor":
        return _release_format_semver(cur_maj, cur_min + 1, 0)
    if kind == "major":
        return _release_format_semver(cur_maj + 1, 0, 0)
    raise ValueError(f"unknown bump kind: {kind!r}")


def _release_semver_sort_key(
    parsed: tuple[int, int, int, str | None, int | None],
) -> tuple:
    """Total-order sort key for `_release_parse_semver` output.

    SemVer §11.4: a stable release has higher precedence than a pre-release
    of the same MAJOR.MINOR.PATCH. Naive tuple comparison breaks because
    Python rejects ``None < str`` at runtime. The key returned here makes
    stable releases sort *after* their pre-releases by inverting the
    "has-prerelease" axis: stable → ``(maj, min, pat, 1, "", 0)``,
    pre-release → ``(maj, min, pat, 0, id, n)``.
    """
    maj, min_, pat, pre_id, pre_n = parsed
    if pre_id is None:
        return (maj, min_, pat, 1, "", 0)
    return (maj, min_, pat, 0, pre_id, pre_n)
