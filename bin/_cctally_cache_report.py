"""Pure-function kernel for cctally cache-report.

This module owns the day/session bucketing, financial computation, and
anomaly classification logic that previously lived inline in
``bin/cctally``. The CLI command ``cctally cache-report`` and the
dashboard sync builder both consume this kernel; the kernel itself is
pure (no I/O, no logging, no environment reads, no SQLite connection).

Display-tz threading: bucketing functions accept ``display_tz``
explicitly. ``None`` means host-local fallback (legacy behavior).
Callers pass the resolved IANA zone from ``resolve_display_tz``.

See ``docs/superpowers/specs/2026-05-21-cache-report-panel-design.md``
§5 for the full contract.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Optional, Tuple
from zoneinfo import ZoneInfo


# Anthropic's per-call >200K-tokens tier — kept in sync with bin/_lib_pricing.
# Callers may override via the ``tiered_threshold`` kwarg.
DEFAULT_TIERED_THRESHOLD = 200_000


@dataclass
class CacheModelBreakdown:
    model_name: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cache_hit_percent: float
    cost: float
    saved_usd: float = 0.0
    wasted_usd: float = 0.0
    net_usd: float = 0.0


@dataclass
class CacheRow:
    # Identity (exactly one group populated)
    date: str | None = None
    session_id: str | None = None
    project_path: str | None = None
    last_activity: dt.datetime | None = None
    source_paths: list[str] = field(default_factory=list)

    # Token counters
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    # Financials
    cost: float = 0.0
    saved_usd: float = 0.0
    wasted_usd: float = 0.0
    net_usd: float = 0.0

    # Per-model breakdown children
    model_breakdowns: list[CacheModelBreakdown] = field(default_factory=list)

    # Anomaly (populated by _classify_anomalies)
    anomaly_triggered: bool = False
    anomaly_reasons: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens + self.output_tokens
            + self.cache_creation_tokens + self.cache_read_tokens
        )

    @property
    def cache_hit_percent(self) -> float:
        return _compute_cache_hit_percent(
            self.input_tokens, self.cache_creation_tokens, self.cache_read_tokens
        )


def _compute_cache_hit_percent(
    input_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
) -> float:
    """Compute cache hit percentage from token counts.

    Formula: ``cache_read / (input + cache_creation + cache_read) * 100``.
    Returns ``0.0`` when there are no tokens.
    """
    total_input = input_tokens + cache_creation_tokens + cache_read_tokens
    if total_input == 0:
        return 0.0
    return (cache_read_tokens / total_input) * 100


def _lookup_pricing(model: str, pricing: dict) -> dict | None:
    """Resolve pricing for a model. Strips ``anthropic/`` / ``anthropic.``
    aliases — same behavior as ``_lib_pricing._resolve_model_pricing`` but
    without the stderr warning side-effect (the kernel is pure).
    """
    p = pricing.get(model)
    if p is not None:
        return p
    for prefix in ("anthropic/", "anthropic."):
        if model.startswith(prefix):
            stripped = model[len(prefix):]
            p = pricing.get(stripped)
            if p is not None:
                return p
    return None


def _compute_entry_cache_dollars(
    model: str,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    *,
    pricing: dict,
    tiered_threshold: int = DEFAULT_TIERED_THRESHOLD,
) -> tuple[float, float, float]:
    """Return ``(saved_usd, wasted_usd, net_usd)`` for a single entry.

    ``saved_usd``  = ``cache_read_tokens × (base_rate − read_rate)``
        — what you'd have paid without caching.
    ``wasted_usd`` = ``cache_creation_tokens × (create_rate − base_rate)``
        — premium paid to write cache.
    ``net_usd``    = ``saved_usd − wasted_usd``. Positive = caching helped.

    Applies Anthropic's per-call >200K-tokens tier (mirrors the
    ``_tiered`` helper in ``_calculate_entry_cost``). Aggregating tokens
    across multiple calls and then pricing would under-count savings on
    any single call that crossed the tier. Resolves ``anthropic/`` and
    ``anthropic.`` aliases via ``_lookup_pricing`` so cache-dollar
    numbers stay aligned with cost numbers.

    Unknown models (no pricing entry) → ``(0.0, 0.0, 0.0)`` silently;
    the CLI's ``_calculate_entry_cost`` path emits the one-shot stderr
    warning for unknown models elsewhere.
    """
    p = _lookup_pricing(model, pricing) or {}
    if not p:
        return (0.0, 0.0, 0.0)

    def _tiered_rate(tokens: int, base_key: str, tiered_key: str) -> float:
        """Blended $/token rate for a single-call token count under tiered pricing."""
        base_rate = p.get(base_key, 0.0)
        tiered_rate = p.get(tiered_key)
        if tokens <= 0:
            return 0.0
        if tokens > tiered_threshold and tiered_rate is not None:
            below = tiered_threshold
            above = tokens - tiered_threshold
            return (below * base_rate + above * tiered_rate) / tokens
        return base_rate

    base_for_read = _tiered_rate(
        cache_read_tokens,
        "input_cost_per_token",
        "input_cost_per_token_above_200k_tokens",
    )
    read_rate = _tiered_rate(
        cache_read_tokens,
        "cache_read_input_token_cost",
        "cache_read_input_token_cost_above_200k_tokens",
    )
    base_for_create = _tiered_rate(
        cache_creation_tokens,
        "input_cost_per_token",
        "input_cost_per_token_above_200k_tokens",
    )
    create_rate = _tiered_rate(
        cache_creation_tokens,
        "cache_creation_input_token_cost",
        "cache_creation_input_token_cost_above_200k_tokens",
    )

    saved = cache_read_tokens * max(0.0, base_for_read - read_rate)
    wasted = cache_creation_tokens * max(0.0, create_rate - base_for_create)
    net = saved - wasted
    return (saved, wasted, net)
