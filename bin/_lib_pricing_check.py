"""Pure-fn kernel for the pricing-freshness check (spec 2026-05-29).

No I/O, no import of `cctally`/`_lib_pricing` at module scope — every
dependency (pricing predicates, tables, observed rows, LiteLLM snapshot)
is passed in by the I/O glue in bin/cctally. Re-exported there like the
other _lib_* kernels.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class CoverageGap:
    provider: str        # "claude" | "codex"
    model: str
    kind: str            # "unpriced" | "fallback"
    entry_count: int
    token_total: int


def classify_coverage(observed, resolve_claude, is_codex_fallback) -> list[CoverageGap]:
    """observed: iterable of (provider, model, entry_count, token_total).

    Claude model with resolve_claude(model) is None -> kind="unpriced".
    Codex model with is_codex_fallback(model) True  -> kind="fallback".
    Priced models produce no gap. Order preserved.
    """
    gaps: list[CoverageGap] = []
    for provider, model, entry_count, token_total in observed:
        if provider == "claude":
            if resolve_claude(model) is None:
                gaps.append(CoverageGap("claude", model, "unpriced", entry_count, token_total))
        elif provider == "codex":
            if is_codex_fallback(model):
                gaps.append(CoverageGap("codex", model, "fallback", entry_count, token_total))
    return gaps


def _is_codex_scope(name: str) -> bool:
    # The Codex models we track are the gpt-5* family (incl. -codex variants).
    # Keep this in sync with CODEX_MODEL_PRICING's key prefixes.
    return name.startswith("gpt-5")


def scope_litellm(litellm: dict) -> dict[str, dict]:
    """Filter a full LiteLLM model_prices map down to the models we track:
    anthropic-provider Claude models, and the gpt-5* Codex family. Skips the
    `sample_spec` doc entry and any entry lacking a dict body."""
    scoped: dict[str, dict] = {}
    for name, body in litellm.items():
        if not isinstance(body, dict):
            continue
        provider = body.get("litellm_provider")
        if provider == "anthropic" and name.startswith("claude-"):
            scoped[name] = body
        elif provider == "openai" and _is_codex_scope(name):
            scoped[name] = body
    return scoped
