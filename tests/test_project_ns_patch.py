"""F1 binding-semantics regression for `cmd_project` (spec §8.1b).

Proves cmd_project's share + cache call path resolves through cctally's
namespace (the `c.<name>` accessor), NOT honest imports — so
`monkeypatch.setattr(cctally, "X", …)` is preserved after the
`bin/_cctally_project.py` extraction. Structurally non-vacuous: GREEN at
the pre-move baseline (bare-name calls hit cctally's patched globals),
GREEN after a correct accessor move, RED only if
`get_claude_session_entries` / `_build_project_snapshot` /
`_share_render_and_emit` is honest-imported (the import binds the real
sibling symbol, bypassing the patch).

Mirrors tests/test_session_id_filter.py's in-process harness.
"""
from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from conftest import load_isolated_cctally_module

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


@pytest.fixture
def cctally_mod(tmp_path, monkeypatch):
    """Load bin/cctally under the canonical isolated data dir (#127).

    Via the shared ``load_isolated_cctally_module`` helper so the loader
    gets ``_cctally_core`` path redirection — without it, a cached
    ``_cctally_core`` made these tests read the real prod DB (#127).
    """
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _make_project_args(**overrides):
    """argparse.Namespace matching cmd_project's arg reads (re-grep
    `args.` / `getattr(args,` inside the cut to confirm completeness)."""
    ns = argparse.Namespace(
        since="2026-01-01", until="2026-01-31", weeks=None, group="model",
        sort="used", order="desc", model=None, project=None,
        breakdown=False, compact=False, json=False,
        format=None, theme="light", reveal_projects=False,
        no_branding=False, output=None, copy=False, open_after_write=False,
        tz=None, timezone=None, config=None, color=False, no_color=False,
        debug=False, debug_samples=5,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def test_cmd_project_resolves_share_and_cache_through_namespace(cctally_mod, monkeypatch):
    """--format md drives BOTH the cache read (get_claude_session_entries)
    and the share branch (_build_project_snapshot + _share_render_and_emit).
    Patching them through cctally's namespace must intercept all three."""
    mod = cctally_mod
    calls = {"entries": 0, "snapshot": 0, "emit": 0}

    def fake_entries(start, end, **kwargs):  # **kwargs absorbs #341 account_key
        calls["entries"] += 1
        return []

    def fake_snapshot(rows, **kwargs):
        calls["snapshot"] += 1
        return object()

    def fake_emit(snap, args):
        calls["emit"] += 1
        return None

    monkeypatch.setattr(mod, "get_claude_session_entries", fake_entries)
    monkeypatch.setattr(mod, "_build_project_snapshot", fake_snapshot)
    monkeypatch.setattr(mod, "_share_render_and_emit", fake_emit)
    # No-op share validation for synthetic args (also confirms
    # _share_validate_args is accessor-routed, not honest-imported).
    monkeypatch.setattr(mod, "_share_validate_args", lambda args: None)

    args = _make_project_args(format="md")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.cmd_project(args)

    assert rc == 0
    assert calls["entries"] >= 1, "get_claude_session_entries not reached via cctally namespace"
    assert calls["snapshot"] == 1, "_build_project_snapshot bypassed (honest-imported?)"
    assert calls["emit"] == 1, "_share_render_and_emit bypassed (honest-imported?)"
