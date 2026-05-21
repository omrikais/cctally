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


# ---------------------------------------------------------------------------
# Day-mode aggregator with explicit display_tz threading
# ---------------------------------------------------------------------------

def _resolve_bucket_tz(display_tz: ZoneInfo | None) -> dt.tzinfo:
    """Return the tz used to bucket entry timestamps into calendar days.

    ``display_tz`` is the caller's resolved IANA zone (from
    ``resolve_display_tz`` in the CLI / dashboard). ``None`` triggers the
    legacy host-local fallback — preserves the pre-extraction contract
    for direct internal callers and matches the
    "internal fallback: host-local intentional" annotation in
    ``bin/cctally`` for the pre-extraction call site.
    """
    if display_tz is not None:
        return display_tz
    # internal fallback: host-local intentional
    return dt.datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def _default_entry_cost(model: str, usage: dict, mode: str,
                        cost_usd: float | None) -> float:
    """Fallback cost calculator used when the caller doesn't inject one.

    Mirrors the ``mode == "auto"`` branch of ``_calculate_entry_cost`` for
    the common case: respect a recorded ``cost_usd`` when present,
    otherwise return 0.0. The CLI / dashboard pass the full pricing-aware
    calculator (``_calculate_entry_cost`` from ``_lib_pricing``) via
    ``cost_calculator`` so unknown / re-priced models pick up the embedded
    pricing tables; this default exists so the kernel stays unit-testable
    without dragging the full pricing dispatch along.
    """
    return cost_usd if cost_usd is not None else 0.0


def _aggregate_cache_by_day(
    entries: Iterable,
    *,
    since: dt.datetime,
    until: dt.datetime,
    display_tz: ZoneInfo | None,
    pricing: dict,
    cost_calculator: Callable[[str, dict, str, Optional[float]], float] | None = None,
) -> list[CacheRow]:
    """Group entries by display-tz local date within ``[since, until]``.

    ``display_tz`` controls bucketing. ``None`` falls back to host-local —
    matches the legacy contract for direct callers (the pre-extraction
    site was annotated "internal fallback: host-local intentional"). The
    extraction closes a pre-existing minor bug where the CLI parsed
    ``--since`` / ``--until`` in display tz but bucketed by host-local
    (spec §1.6 / plan A3); callers pass the same resolved tz they used
    for window parsing.

    ``cost_calculator`` is the per-entry cost function (the CLI uses
    ``_calculate_entry_cost`` with embedded pricing; the dashboard
    snapshot builder injects the same). When None, falls back to a
    minimal default that reads ``entry.cost_usd`` if present, else
    returns 0.0 — sufficient for unit tests, insufficient for production
    runs against entries lacking a recorded ``cost_usd``.

    ``since`` / ``until`` are read by callers to bound their
    ``get_entries`` / ``get_claude_session_entries`` query; the kernel
    itself does NOT re-filter on the window (entries are assumed
    pre-filtered). They're accepted in the signature for parity with the
    pre-extraction call site and so future kernel versions can apply a
    defensive in-kernel window filter without breaking callers.
    """
    tz = _resolve_bucket_tz(display_tz)
    cc = cost_calculator if cost_calculator is not None else _default_entry_cost

    # Per spec §1.6, `since` / `until` are accepted for caller parity; the
    # kernel trusts callers to pre-filter via their own get_entries query.
    _ = since
    _ = until

    day_model_buckets: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in entries:
        # ``entry.timestamp`` is an aware UTC datetime per SessionEntry
        # contract; ``astimezone(tz)`` shifts to the display tz before
        # taking the calendar date.
        day_key = entry.timestamp.astimezone(tz).strftime("%Y-%m-%d")
        cost = cc(entry.model, entry.usage, "auto", entry.cost_usd)
        create_tok = entry.usage.get("cache_creation_input_tokens", 0)
        read_tok = entry.usage.get("cache_read_input_tokens", 0)
        saved, wasted, net = _compute_entry_cache_dollars(
            entry.model, create_tok, read_tok, pricing=pricing,
        )
        models = day_model_buckets.setdefault(day_key, {})
        b = models.setdefault(entry.model, {
            "inputTokens": 0, "outputTokens": 0,
            "cacheCreationTokens": 0, "cacheReadTokens": 0, "cost": 0.0,
            "savedUsd": 0.0, "wastedUsd": 0.0, "netUsd": 0.0,
        })
        b["inputTokens"] += entry.usage.get("input_tokens", 0)
        b["outputTokens"] += entry.usage.get("output_tokens", 0)
        b["cacheCreationTokens"] += create_tok
        b["cacheReadTokens"] += read_tok
        b["cost"] += cost
        b["savedUsd"] += saved
        b["wastedUsd"] += wasted
        b["netUsd"] += net

    result: list[CacheRow] = []
    for day_key in sorted(day_model_buckets.keys()):
        models = day_model_buckets[day_key]
        row = CacheRow(date=day_key)
        for model_name in sorted(models.keys()):
            b = models[model_name]
            mb = CacheModelBreakdown(
                model_name=model_name,
                input_tokens=b["inputTokens"],
                output_tokens=b["outputTokens"],
                cache_creation_tokens=b["cacheCreationTokens"],
                cache_read_tokens=b["cacheReadTokens"],
                cache_hit_percent=_compute_cache_hit_percent(
                    b["inputTokens"], b["cacheCreationTokens"], b["cacheReadTokens"]
                ),
                cost=b["cost"],
                saved_usd=b["savedUsd"],
                wasted_usd=b["wastedUsd"],
                net_usd=b["netUsd"],
            )
            row.model_breakdowns.append(mb)
            row.input_tokens += mb.input_tokens
            row.output_tokens += mb.output_tokens
            row.cache_creation_tokens += mb.cache_creation_tokens
            row.cache_read_tokens += mb.cache_read_tokens
            row.cost += mb.cost
            row.saved_usd += mb.saved_usd
            row.wasted_usd += mb.wasted_usd
            row.net_usd += mb.net_usd
        result.append(row)
    return result


# ---------------------------------------------------------------------------
# Session-mode aggregator (resume-merged across JSONL files)
# ---------------------------------------------------------------------------

def _filename_uuid_stem(path: str) -> str:
    """Extract the UUID stem from a JSONL filename.

    Claude JSONL files are named ``<uuid>.jsonl``; fall back to the full
    filename (without extension) if the stem isn't a valid UUID shape.
    Matches the ``session`` subcommand's convention for unresolved session
    IDs. Stays pure — uses only ``str.partition``, no ``os.path`` and no
    syscalls.
    """
    # The original lived in bin/cctally and used os.path.basename; this
    # rebuild matches that contract with pure-string slicing so the
    # kernel doesn't import os.
    last_slash = path.rfind("/")
    base = path[last_slash + 1:] if last_slash != -1 else path
    stem, _, _ = base.partition(".")
    return stem


def _default_project_decoder(source_path: str) -> str:
    """Fallback project label when the caller doesn't inject a decoder.

    Returns the basename of the directory containing the JSONL — the same
    raw label ``_decode_escaped_cwd(os.path.basename(os.path.dirname(...)))``
    operated on, but without the escape-decoding. CLI / dashboard callers
    inject the full ``_decode_escaped_cwd`` so resumed sessions display the
    decoded cwd. Pure-string slicing keeps the kernel ``os``-free.
    """
    last_slash = source_path.rfind("/")
    if last_slash == -1:
        return source_path
    parent = source_path[:last_slash]
    parent_slash = parent.rfind("/")
    return parent[parent_slash + 1:] if parent_slash != -1 else parent


@dataclass
class _SessionAggregationResult:
    """Bundles session rows + the fallback warning count.

    Returned by ``_aggregate_cache_by_session_with_warnings`` so callers
    can choose whether to emit the "N entries lacked session_files rows"
    one-shot warning. The plain ``_aggregate_cache_by_session`` wrapper
    discards the count for the common case.
    """
    rows: list[CacheRow]
    fallback_count: int


def _aggregate_cache_by_session_with_warnings(
    entries: Iterable,
    *,
    since: dt.datetime,
    until: dt.datetime,
    pricing: dict,
    cost_calculator: Callable[[str, dict, str, Optional[float]], float] | None = None,
    project_decoder: Callable[[str], str] | None = None,
) -> _SessionAggregationResult:
    """Group Claude entries by sessionId (resumed-merged) within ``[since, until]``.

    Resume-merging: entries from multiple JSONL files sharing a sessionId
    collapse into one row. ``project_path`` reflects the most-recent
    in-window entry's resolved project (with a per-session fallback to
    the decoded cwd from the source path's parent directory).

    Synthetic entries (``model == '<synthetic>'``) are dropped — they're
    Claude Code's internal markers, not real model calls — before any
    bucketing, so they don't inflate the fallback count either.

    Entries with ``session_id is None`` fall back to the filename UUID
    stem (matching ``cctally session``); the count of such fallback
    entries rides back on ``_SessionAggregationResult.fallback_count``
    so the caller can emit the legacy one-shot stderr warning.

    ``cost_calculator`` and ``pricing`` mirror the day-mode kernel — both
    are injected so the kernel stays free of pricing globals /
    cost-dispatch I/O. ``project_decoder`` is the optional
    ``_decode_escaped_cwd``-style helper (defaults to a raw dirname
    basename).

    ``since`` / ``until`` are accepted for caller parity; the kernel
    trusts callers to pre-filter entries via their own query.
    """
    cc = cost_calculator if cost_calculator is not None else _default_entry_cost
    pd = project_decoder if project_decoder is not None else _default_project_decoder

    _ = since
    _ = until

    # buckets[sid] = {"entries": [...], "project_path": str|None,
    #                 "last_activity": dt|None, "source_paths": set[str]}
    buckets: dict[str, dict[str, Any]] = {}
    fallback_count = 0
    for entry in entries:
        if entry.model == "<synthetic>":
            continue
        sid = entry.session_id
        if sid is None:
            sid = _filename_uuid_stem(entry.source_path)
            fallback_count += 1
        b = buckets.setdefault(sid, {
            "entries": [],
            # Seed with decoded-cwd fallback so rows still resolve a
            # Project cell while session_files backfill is incomplete.
            # Real project_path from session_files (if present on any
            # joined row) overrides below.
            "project_path": pd(entry.source_path),
            "last_activity": None,
            "source_paths": set(),
        })
        b["entries"].append(entry)
        b["source_paths"].add(entry.source_path)
        if b["last_activity"] is None or entry.timestamp > b["last_activity"]:
            b["last_activity"] = entry.timestamp
            # Project path from most-recent in-window entry that has it.
            if entry.project_path:
                b["project_path"] = entry.project_path

    result: list[CacheRow] = []
    for sid, b in buckets.items():
        # Per-model sub-buckets scoped to this session's entries.
        model_buckets: dict[str, dict[str, Any]] = {}
        for entry in b["entries"]:
            mb_raw = model_buckets.setdefault(entry.model, {
                "inputTokens": 0, "outputTokens": 0,
                "cacheCreationTokens": 0, "cacheReadTokens": 0, "cost": 0.0,
                "savedUsd": 0.0, "wastedUsd": 0.0, "netUsd": 0.0,
            })
            mb_raw["inputTokens"] += entry.input_tokens
            mb_raw["outputTokens"] += entry.output_tokens
            mb_raw["cacheCreationTokens"] += entry.cache_creation_tokens
            mb_raw["cacheReadTokens"] += entry.cache_read_tokens
            mb_raw["cost"] += cc(
                entry.model,
                {
                    "input_tokens": entry.input_tokens,
                    "output_tokens": entry.output_tokens,
                    "cache_creation_input_tokens": entry.cache_creation_tokens,
                    "cache_read_input_tokens": entry.cache_read_tokens,
                },
                "auto",
                entry.cost_usd,
            )
            saved, wasted, net = _compute_entry_cache_dollars(
                entry.model,
                entry.cache_creation_tokens,
                entry.cache_read_tokens,
                pricing=pricing,
            )
            mb_raw["savedUsd"] += saved
            mb_raw["wastedUsd"] += wasted
            mb_raw["netUsd"] += net

        row = CacheRow(
            session_id=sid,
            project_path=b["project_path"],
            last_activity=b["last_activity"],
            source_paths=sorted(b["source_paths"]),
        )
        for model_name in sorted(model_buckets.keys()):
            mb_raw = model_buckets[model_name]
            mb = CacheModelBreakdown(
                model_name=model_name,
                input_tokens=mb_raw["inputTokens"],
                output_tokens=mb_raw["outputTokens"],
                cache_creation_tokens=mb_raw["cacheCreationTokens"],
                cache_read_tokens=mb_raw["cacheReadTokens"],
                cache_hit_percent=_compute_cache_hit_percent(
                    mb_raw["inputTokens"],
                    mb_raw["cacheCreationTokens"],
                    mb_raw["cacheReadTokens"],
                ),
                cost=mb_raw["cost"],
                saved_usd=mb_raw["savedUsd"],
                wasted_usd=mb_raw["wastedUsd"],
                net_usd=mb_raw["netUsd"],
            )
            row.model_breakdowns.append(mb)
            row.input_tokens += mb.input_tokens
            row.output_tokens += mb.output_tokens
            row.cache_creation_tokens += mb.cache_creation_tokens
            row.cache_read_tokens += mb.cache_read_tokens
            row.cost += mb.cost
            row.saved_usd += mb.saved_usd
            row.wasted_usd += mb.wasted_usd
            row.net_usd += mb.net_usd
        result.append(row)

    # Initial ordering descending by last_activity; the CLI's
    # ``_sort_cache_rows`` may resort under ``--sort``. Use tz-aware
    # sentinel to avoid naive-vs-aware comparison errors on rows missing
    # last_activity.
    _min_dt = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    result.sort(key=lambda r: r.last_activity or _min_dt, reverse=True)
    return _SessionAggregationResult(rows=result, fallback_count=fallback_count)


def _aggregate_cache_by_session(
    entries: Iterable,
    *,
    since: dt.datetime,
    until: dt.datetime,
    pricing: dict,
    cost_calculator: Callable[[str, dict, str, Optional[float]], float] | None = None,
    project_decoder: Callable[[str], str] | None = None,
) -> list[CacheRow]:
    """Thin wrapper around ``_aggregate_cache_by_session_with_warnings``
    that discards the fallback warning count.

    Callers that want to emit the legacy "N entries lacked session_files
    rows" one-shot warning should reach for
    ``_aggregate_cache_by_session_with_warnings`` directly.
    """
    return _aggregate_cache_by_session_with_warnings(
        entries,
        since=since, until=until,
        pricing=pricing,
        cost_calculator=cost_calculator,
        project_decoder=project_decoder,
    ).rows
