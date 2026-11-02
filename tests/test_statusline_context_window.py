"""Model fallback behind `cctally statusline`'s 🧠 segment (and `explain`).

`_resolve_context_window` (bin/_cctally_statusline.py) is the FALLBACK used
when Claude Code's payload carries no valid `context_window.context_window_size`
(and the only capacity source `explain` has, since transcripts record a bare
model id). It resolves a `model.id` in this order (#1002):

1. a case-insensitive terminal `[1m]` marker → 1,000,000, whatever the family,
   registered or not, and without a warning;
2. after stripping one trailing `-YYYYMMDD` snapshot date, an exact hit in the
   registry of verified current-generation defaults
   `CLAUDE_MODEL_CONTEXT_WINDOWS` (all 1,000,000);
3. a family-substring hit in `CLAUDE_MODEL_CONTEXT_WINDOW_DEFAULT_FAMILY`
   (`sonnet`/`opus`/`haiku` → 200,000), which every prior-generation bare id
   keeps;
4. otherwise `None` plus one warning (the segment renders `🧠 N/A`).

Why this needs a guard: every current-generation id also contains a family
token, so a missing or mis-keyed registry entry does not fail loudly — it
silently falls through to the 200K family default and the segment renders a
context percentage inflated five-fold (a 349,814-token Opus 5.5 session in a
1M window read as `🧠 175%` red instead of `🧠 35%` green), while an id with no
family token (Fable) renders `N/A`.
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


@pytest.mark.parametrize("model_id", [
    "claude-opus-5-5[1m]",
    "claude-sonnet-4-5-20250929[1m]",
    "CLAUDE-OPUS-5[1M]",
    # Family-independent: a `[1m]` rule tied to a family or the registry
    # would miss this id.
    "mystery-9000[1M]",
])
def test_any_1m_marker_reads_as_the_full_window_without_a_warning(
        app, model_id):
    """Detects a `[1m]` session measured against 200K (a ~5x inflated %) or
    reported as unknown because the marker was matched per family or id."""
    seen = []
    assert app._resolve_context_window(model_id, seen.append) == 1_000_000
    assert seen == []


@pytest.mark.parametrize("model_id", [
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-mythos-5-1",
    "claude-opus-5-5-20261001",
])
def test_current_generation_ids_resolve_to_their_verified_1m_default(
        app, model_id):
    """Detects a wrong current-generation default: Opus/Sonnet/Haiku 5.5
    falling to the 200K family default, or Fable/Mythos rendering `N/A`."""
    seen = []
    assert app._resolve_context_window(model_id, seen.append) == 1_000_000
    assert seen == []


@pytest.mark.parametrize("model_id", [
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
])
def test_prior_generation_bare_ids_keep_the_200k_family_default(app, model_id):
    """Detects a leaked prior-generation change, including a prefix match of
    `claude-opus-5` against the registered `claude-opus-5-5`."""
    assert app._resolve_context_window(model_id, lambda _m: None) == 200_000


@pytest.mark.parametrize("model_id", [
    "claude-mythos-preview",
    "claude-fable-6",
    "mystery-9000",
])
def test_an_unverified_id_outside_the_families_warns_once_and_returns_none(
        app, model_id):
    """Detects a guessed window for a model with no verified capacity (a
    `fable`/`mythos` family substring, or a prefix match)."""
    seen = []
    assert app._resolve_context_window(model_id, seen.append) is None
    assert len(seen) == 1 and model_id in seen[0]
