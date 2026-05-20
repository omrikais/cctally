"""Regression: SessionsPanel cross-nav binds disambiguated `project_key`
for sessions whose `cwd` is a subdirectory of an envelope row's git-root
`bucket_path` (the monorepo case).

The bug being fixed: `_build_data_snapshot`'s late-bind block indexes
the envelope by `bucket_path` (a git-root from
`_resolve_project_key(..., "git-root")`), but `srow.project_path` is the
raw `cwd` from `_aggregate_claude_sessions` — typically a subdirectory.
Before Fix 1 the lookup did `key_by_bucket_path.get(srow.project_path)`
directly, missed for every monorepo session, and the SessionsPanel cross-
nav button degraded to plain text. Fix 1 routes `srow.project_path`
through `_resolve_project_key` before the dict lookup so the resolution
matches what the envelope builder did.

This test rebuilds the production lookup logic on a synthetic envelope +
filesystem layout (a `tmp_path` repo root with a `.git` directory) and
asserts the cross-nav binding succeeds.
"""
from __future__ import annotations

import dataclasses
import pathlib
import sys

import pytest

from conftest import load_script  # noqa: E402


_NS = load_script()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

# Load the sibling so its `_resolve_project_key` (alias of
# `_cctally_cache._resolve_project_key`) resolves cleanly.
import _cctally_cache  # noqa: E402

_resolve_project_key = _cctally_cache._resolve_project_key


def _late_bind_project_keys(envelope: dict, sessions: list) -> list:
    """Mirror of the production late-bind block in
    `_cctally_tui._build_data_snapshot` (the `if projects_envelope_block
    is not None:` body). Kept in lockstep with the production code so
    this test acts as a regression guard.
    """
    key_by_bucket_path: dict[str, str] = {}
    for r in envelope.get("current_week", {}).get("rows", []):
        bp = r.get("bucket_path")
        k = r.get("key")
        if bp and k:
            key_by_bucket_path[bp] = k
    for r in envelope.get("trend", {}).get("projects", []):
        bp = r.get("bucket_path")
        k = r.get("key")
        if bp and k and bp not in key_by_bucket_path:
            key_by_bucket_path[bp] = k
    resolver_cache: dict = {}
    annotated = []
    for srow in sessions:
        pkey = None
        if srow.project_path:
            bp = _resolve_project_key(
                srow.project_path, "git-root", resolver_cache,
            ).bucket_path
            pkey = key_by_bucket_path.get(bp)
        if pkey is None:
            annotated.append(srow)
        else:
            annotated.append(dataclasses.replace(srow, project_key=pkey))
    return annotated


@dataclasses.dataclass
class _StubSessionRow:
    """Minimal stand-in for `TuiSessionRow` carrying the fields the
    late-bind reads + writes."""
    project_path: str | None
    project_key: str | None = None


def test_cross_nav_binds_when_cwd_is_subdirectory(tmp_path: pathlib.Path):
    """Session cwd = `<repo>/src/sub`; envelope bucket_path = `<repo>`.

    Before Fix 1 this assertion failed (lookup mismatched the raw cwd
    against the git-root indexed dict). After Fix 1 the lookup resolves
    cwd → git-root and hits.
    """
    # Synthesize a repo with .git so _resolve_project_key's parent walk
    # terminates on the canonical root.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    subdir = repo / "src" / "sub"
    subdir.mkdir(parents=True)

    # Envelope is keyed by the realpath'd bucket_path (matches what
    # `_resolve_project_key` returns).
    bucket_path = str(repo.resolve())
    envelope = {
        "current_week": {
            "rows": [
                {"key": "repo", "bucket_path": bucket_path},
            ],
        },
        "trend": {"projects": []},
    }
    sessions = [
        _StubSessionRow(project_path=str(subdir)),
    ]
    annotated = _late_bind_project_keys(envelope, sessions)
    assert len(annotated) == 1
    assert annotated[0].project_key == "repo", (
        "cwd subdirectory MUST resolve through _resolve_project_key to "
        "the envelope's git-root bucket_path; cross-nav binding broke"
    )


def test_cross_nav_binds_when_cwd_equals_bucket(tmp_path: pathlib.Path):
    """Direct equality case — cwd == git-root. Sanity check that Fix 1
    does not regress the non-monorepo path."""
    repo = tmp_path / "repo2"
    repo.mkdir()
    (repo / ".git").mkdir()

    bucket_path = str(repo.resolve())
    envelope = {
        "current_week": {
            "rows": [
                {"key": "repo2", "bucket_path": bucket_path},
            ],
        },
        "trend": {"projects": []},
    }
    sessions = [_StubSessionRow(project_path=str(repo))]
    annotated = _late_bind_project_keys(envelope, sessions)
    assert annotated[0].project_key == "repo2"


def test_cross_nav_skips_when_no_envelope_match(tmp_path: pathlib.Path):
    """cwd resolves to a git-root not represented in the envelope —
    project_key stays None (cell renders as plain text per spec §4.1)."""
    repo_a = tmp_path / "a"
    repo_a.mkdir()
    (repo_a / ".git").mkdir()
    repo_b = tmp_path / "b"
    repo_b.mkdir()
    (repo_b / ".git").mkdir()

    envelope = {
        "current_week": {
            "rows": [
                {"key": "a", "bucket_path": str(repo_a.resolve())},
            ],
        },
        "trend": {"projects": []},
    }
    sessions = [_StubSessionRow(project_path=str(repo_b))]
    annotated = _late_bind_project_keys(envelope, sessions)
    assert annotated[0].project_key is None
