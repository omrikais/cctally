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
