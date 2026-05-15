"""Model pricing tables and cost-computation primitives.

Pure-fn layer (no I/O at import time): holds the embedded Anthropic
(`CLAUDE_MODEL_PRICING`) and OpenAI Codex (`CODEX_MODEL_PRICING`) pricing
snapshots plus the helpers that consume them — model-name normalization,
chip palette, per-entry cost calculation for both providers.

`bin/cctally` re-exports every symbol below so internal call sites resolve
unchanged. Tests reach into this layer via the re-exported names on the
`cctally` module; no direct import of `_lib_pricing` is expected from tests.

A private `_eprint` duplicates `bin/cctally:eprint` (two-line stderr helper)
so this pure layer carries zero back-imports per the split design's
Section 5.3 contract.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import re
import sys
from typing import Any


def _eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


TIERED_THRESHOLD = 200_000


def _chip_for_model(name: str) -> str:
    """Bucket a canonical model id into a small chip palette.

    Returns one of 'opus' | 'sonnet' | 'haiku' | 'other'. Used by the
    dashboard's Weekly / Monthly panels and modals so per-model
    coloring stays consistent across the UI.
    """
    n = (name or "").lower()
    if "opus" in n:
        return "opus"
    if "sonnet" in n:
        return "sonnet"
    if "haiku" in n:
        return "haiku"
    return "other"


# Anthropic API pricing snapshot:
# - Source: https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
# - Captured: 2026-05-04
# - Verified by maintainer against docs.claude.com/en/docs/about-claude/pricing;
#   update in PRs touching this table.
CLAUDE_MODEL_PRICING: dict[str, dict[str, Any]] = {
    "claude-3-5-haiku-20241022": {
        "input_cost_per_token": 8e-07,
        "output_cost_per_token": 4e-06,
        "cache_creation_input_token_cost": 1e-06,
        "cache_read_input_token_cost": 8e-08,
    },
    "claude-3-5-haiku-latest": {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 5e-06,
        "cache_creation_input_token_cost": 1.25e-06,
        "cache_read_input_token_cost": 1e-07,
    },
    "claude-3-5-sonnet-20240620": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-3-5-sonnet-20241022": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-3-5-sonnet-latest": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-3-7-sonnet-20250219": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-3-7-sonnet-latest": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-3-haiku-20240307": {
        "input_cost_per_token": 2.5e-07,
        "output_cost_per_token": 1.25e-06,
        "cache_creation_input_token_cost": 3e-07,
        "cache_read_input_token_cost": 3e-08,
    },
    "claude-3-opus-20240229": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-3-opus-latest": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-4-opus-20250514": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-4-sonnet-20250514": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
    },
    "claude-haiku-4-5": {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 5e-06,
        "cache_creation_input_token_cost": 1.25e-06,
        "cache_read_input_token_cost": 1e-07,
    },
    "claude-haiku-4-5-20251001": {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 5e-06,
        "cache_creation_input_token_cost": 1.25e-06,
        "cache_read_input_token_cost": 1e-07,
    },
    "claude-opus-4-1": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-opus-4-1-20250805": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-opus-4-20250514": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-opus-4-5": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-5-20251101": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-6": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-6-20260205": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-7": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-7-20260416": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-sonnet-4-20250514": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
    },
    "claude-sonnet-4-5": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
    },
    "claude-sonnet-4-5-20250929": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
    },
    "claude-sonnet-4-6": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
}

_unknown_model_warnings: set[str] = set()

# ---------------------------------------------------------------------------
# Codex / GPT-5 pricing table
# ---------------------------------------------------------------------------
#
# Codex (OpenAI) API pricing snapshot:
# - Source: https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
# - Captured: 2026-05-04
# - Models listed are those observed in ~/.codex/sessions/ at implementation
#   time plus common Codex/GPT-5 variants. Models absent from this table fall
#   back to `gpt-5` pricing with isFallback=true (matches upstream's
#   LEGACY_FALLBACK_MODEL behavior); a one-shot stderr warning is emitted per
#   unknown model name.
#
# Billing rules:
# - reasoning_output_tokens is billed at the *output* rate (matches
#   LiteLLM / upstream).
# - If cache_read_input_token_cost is absent for a model, we fall back to
#   input_cost_per_token / 4 (matches LiteLLM's documented fallback).
# - Above-272k tiered rates are applied per-turn (row), mirroring the Claude
#   pattern via a dedicated CODEX_TIERED_THRESHOLD.
CODEX_TIERED_THRESHOLD = 272_000

CODEX_MODEL_PRICING: dict[str, dict[str, Any]] = {
    "gpt-5": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5-codex": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5.1-codex": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5.1-codex-max": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5.1-codex-mini": {
        "input_cost_per_token": 2.5e-07,
        "cache_read_input_token_cost": 2.5e-08,
        "output_cost_per_token": 2e-06,
    },
    "gpt-5.2": {
        "input_cost_per_token": 1.75e-06,
        "cache_read_input_token_cost": 1.75e-07,
        "output_cost_per_token": 1.4e-05,
    },
    "gpt-5.2-codex": {
        "input_cost_per_token": 1.75e-06,
        "cache_read_input_token_cost": 1.75e-07,
        "output_cost_per_token": 1.4e-05,
    },
    "gpt-5.3-codex": {
        "input_cost_per_token": 1.75e-06,
        "cache_read_input_token_cost": 1.75e-07,
        "output_cost_per_token": 1.4e-05,
    },
    "gpt-5.3-codex-spark": {
        # Not in LiteLLM snapshot. Upstream emits isFallback: false with $0
        # billing contribution, so we mirror with an all-zero pricing entry
        # rather than the unknown-model warn-path.
        "input_cost_per_token": 0.0,
        "cache_read_input_token_cost": 0.0,
        "output_cost_per_token": 0.0,
    },
    "gpt-5.4": {
        # Source: LiteLLM model_prices_and_context_window.json (openai provider entry
        # for "gpt-5.4"). Also matches OpenAI's published /api/docs/pricing page
        # (input $2.50/M, cached $0.25/M, output $15.00/M; above-272k tier doubles
        # input/cache and 1.5x's output).
        "input_cost_per_token": 2.5e-06,
        "cache_read_input_token_cost": 2.5e-07,
        "output_cost_per_token": 1.5e-05,
        "input_cost_per_token_above_272k_tokens": 5e-06,
        "cache_read_input_token_cost_above_272k_tokens": 5e-07,
        "output_cost_per_token_above_272k_tokens": 2.25e-05,
    },
    "gpt-5.4-mini": {
        # Source: LiteLLM model_prices_and_context_window.json (openai provider
        # entry for "gpt-5.4-mini"). Matches OpenAI published pricing: input
        # $0.75/M, cached $0.075/M, output $4.50/M. No above-272k tier
        # (max_input_tokens in LiteLLM is 272000 — the ceiling, not a tier break).
        "input_cost_per_token": 7.5e-07,
        "cache_read_input_token_cost": 7.5e-08,
        "output_cost_per_token": 4.5e-06,
    },
    "gpt-5.5": {
        # Source: OpenAI published pricing (announced 2026-04-23). Input
        # $5.00/M, cached $0.50/M, output $30.00/M. No above-272k tier
        # announced. Add tiered fields here when LiteLLM publishes them.
        "input_cost_per_token": 5e-06,
        "cache_read_input_token_cost": 5e-07,
        "output_cost_per_token": 3e-05,
    },
}

_unknown_codex_model_warnings: set[str] = set()

# Upstream ccusage-codex maps unknown Codex model names to `gpt-5` pricing
# and marks them isFallback: true. We mirror that behavior so cost figures
# match what a user would see with `ccusage-codex` on the same JSONL data.
# Behavior matches LEGACY_FALLBACK_MODEL in upstream ccusage-codex — both
# tools fall back to gpt-5 pricing for unknown model names so output remains
# directly comparable.
CODEX_LEGACY_FALLBACK_MODEL = "gpt-5"


def _resolve_codex_pricing(model: str) -> tuple[dict[str, Any] | None, bool]:
    """Return (pricing_dict, is_fallback).

    Returns (entry, False) when the model has a direct pricing entry. Returns
    (gpt-5-entry, True) when the model is unknown — matches upstream's
    LEGACY_FALLBACK_MODEL semantics. Returns (None, True) only if the fallback
    model itself is missing from the pricing dict (programming error; warn once).
    """
    direct = CODEX_MODEL_PRICING.get(model)
    if direct is not None:
        return direct, False
    fallback = CODEX_MODEL_PRICING.get(CODEX_LEGACY_FALLBACK_MODEL)
    return fallback, True


def _is_codex_fallback(model: str) -> bool:
    """True iff `model` would resolve via the LEGACY_FALLBACK_MODEL path."""
    return model not in CODEX_MODEL_PRICING


def _resolve_model_pricing(model: str) -> dict[str, Any] | None:
    """Look up pricing for a model name. Returns None if unknown."""
    pricing = CLAUDE_MODEL_PRICING.get(model)
    if pricing is not None:
        return pricing
    for prefix in ("anthropic/", "anthropic."):
        if model.startswith(prefix):
            stripped = model[len(prefix):]
            pricing = CLAUDE_MODEL_PRICING.get(stripped)
            if pricing is not None:
                return pricing
    if model not in _unknown_model_warnings:
        _unknown_model_warnings.add(model)
        _eprint(f"[cost] unknown model, treating cost as $0: {model}")
    return None


def _calculate_entry_cost(
    model: str,
    usage: dict[str, Any],
    mode: str = "auto",
    cost_usd: float | None = None,
) -> float:
    """Calculate USD cost for a single API call entry."""
    if mode == "display":
        return cost_usd if cost_usd is not None else 0.0
    if mode == "auto" and cost_usd is not None:
        return cost_usd

    pricing = _resolve_model_pricing(model)
    if pricing is None:
        return 0.0

    def _tiered(tokens: int, base_key: str, tiered_key: str) -> float:
        base_rate = pricing.get(base_key, 0.0)
        tiered_rate = pricing.get(tiered_key)
        if tokens <= 0:
            return 0.0
        if tokens > TIERED_THRESHOLD and tiered_rate is not None:
            below = min(tokens, TIERED_THRESHOLD)
            above = tokens - TIERED_THRESHOLD
            return below * base_rate + above * tiered_rate
        return tokens * base_rate

    input_cost = _tiered(
        usage.get("input_tokens", 0),
        "input_cost_per_token",
        "input_cost_per_token_above_200k_tokens",
    )
    output_cost = _tiered(
        usage.get("output_tokens", 0),
        "output_cost_per_token",
        "output_cost_per_token_above_200k_tokens",
    )
    cache_create_cost = _tiered(
        usage.get("cache_creation_input_tokens", 0),
        "cache_creation_input_token_cost",
        "cache_creation_input_token_cost_above_200k_tokens",
    )
    cache_read_cost = _tiered(
        usage.get("cache_read_input_tokens", 0),
        "cache_read_input_token_cost",
        "cache_read_input_token_cost_above_200k_tokens",
    )
    total = input_cost + output_cost + cache_create_cost + cache_read_cost

    return total


def _warn_unknown_codex_model(model: str) -> None:
    """One-shot stderr warning for a Codex model absent from the pricing dict."""
    if model in _unknown_codex_model_warnings:
        return
    _unknown_codex_model_warnings.add(model)
    _eprint(f"[codex] unknown model, using gpt-5 fallback pricing (isFallback=true): {model}")


def _calculate_codex_entry_cost(
    model: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
) -> float:
    """Compute USD cost for one Codex `token_count` event.

    Token-field semantics (LiteLLM convention, matched by upstream ccusage-codex):
      - input_tokens INCLUDES cached_input_tokens (cached is a subset).
      - output_tokens INCLUDES reasoning_output_tokens (reasoning is a subset).
    Cost formula:
      non_cached_input = max(0, input_tokens - cached_input_tokens)
      cost = non_cached_input * input_cost_per_token
           + cached_input_tokens * cache_read_input_token_cost
           + output_tokens * output_cost_per_token
    The reasoning_output_tokens parameter is accepted for API stability but
    not used directly — its contribution is already billed inside output_tokens.

    Above-272k tier applied per-turn when the corresponding _above_272k_tokens
    key is present in the pricing entry.
    """
    del reasoning_output_tokens  # already billed inside output_tokens
    pricing, is_fallback = _resolve_codex_pricing(model)
    if pricing is None:
        # Only possible if CODEX_LEGACY_FALLBACK_MODEL itself is missing — treat as
        # $0 to avoid crashing; a programming error we want to notice.
        _warn_unknown_codex_model(model)
        return 0.0
    if is_fallback:
        _warn_unknown_codex_model(model)  # one-shot per unique model name

    def _tiered(tokens: int, base_key: str, tiered_key: str) -> float:
        if tokens <= 0:
            return 0.0
        base_rate = pricing.get(base_key, 0.0)
        if not base_rate:
            return 0.0
        tiered_rate = pricing.get(tiered_key)
        if tokens > CODEX_TIERED_THRESHOLD and tiered_rate is not None:
            return CODEX_TIERED_THRESHOLD * base_rate + (tokens - CODEX_TIERED_THRESHOLD) * tiered_rate
        return tokens * base_rate

    non_cached_input = max(0, input_tokens - cached_input_tokens)

    input_cost = _tiered(
        non_cached_input,
        "input_cost_per_token",
        "input_cost_per_token_above_272k_tokens",
    )
    cached_input_cost = _tiered(
        cached_input_tokens,
        "cache_read_input_token_cost",
        "cache_read_input_token_cost_above_272k_tokens",
    )
    output_cost = _tiered(
        output_tokens,
        "output_cost_per_token",
        "output_cost_per_token_above_272k_tokens",
    )
    return input_cost + cached_input_cost + output_cost


def _short_model_name(model: str) -> str:
    """Shorten model name for display: 'claude-opus-4-6' -> 'opus-4-6'."""
    name = model
    # Strip 'claude-' prefix
    if name.startswith("claude-"):
        name = name[len("claude-"):]
    # Strip date suffixes like '-20251001'
    if re.match(r".*-\d{8}$", name):
        name = name[:-9]
    return name
