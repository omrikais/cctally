"""Per-model context-window registry behind `cctally statusline`'s 🧠 segment.

`_resolve_context_window` (bin/_cctally_statusline.py) resolves a Claude Code
`model.id` in two rungs: an exact hit in `CLAUDE_MODEL_CONTEXT_WINDOWS`, then a
case-insensitive family-substring fallback to
`CLAUDE_MODEL_CONTEXT_WINDOW_DEFAULT_FAMILY` (200K), then `None` + a one-shot
warning.

Why this needs a guard: the bracketed 1M-context variants Claude Code exposes
(`claude-opus-5[1m]`) still contain the `opus` family token, so a *missing*
explicit entry does not fail loudly — it silently falls through to the 200K
default and the segment renders a context percentage inflated ~5x (a 400K-token
1M session reads as 200%). Every new model's `[1m]` variant must be registered.
"""
from __future__ import annotations

import sys

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["cctally"]


def _noop_warn(msg: str) -> None:
    pass


# Bracketed 1M-context variants: every current 1M-window model Claude Code can
# report must resolve to the real window, not the 200K family default.
@pytest.mark.parametrize("model_id", [
    "claude-opus-5[1m]",
    "claude-opus-4-8[1m]",
    "claude-opus-4-7[1m]",
    "claude-sonnet-4-5[1m]",
])
def test_bracketed_1m_variants_resolve_to_full_window(app, model_id):
    assert app._resolve_context_window(model_id, _noop_warn) == 1_000_000


@pytest.mark.parametrize("model_id", [
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
])
def test_bare_ids_fall_back_to_family_default(app, model_id):
    """Non-vacuity companion: without the `[1m]` marker the resolver returns
    the 200K family default, which is what makes a missing explicit entry
    silent rather than loud."""
    assert app._resolve_context_window(model_id, _noop_warn) == 200_000


def test_unknown_model_warns_and_returns_none(app):
    seen = []
    assert app._resolve_context_window("mystery-9000", seen.append) is None
    assert len(seen) == 1 and "mystery-9000" in seen[0]
