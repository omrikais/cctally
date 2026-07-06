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
from typing import Any, Callable, Iterable, Literal, NamedTuple, Optional
from zoneinfo import ZoneInfo


def _import_stable_sum():
    """Resolve ``_lib_fmt.stable_sum`` — the interpreter-stable float-summation
    chokepoint (math.fsum) used for output-bound cost totals.

    This kernel is loaded by file path from both ``bin/cctally`` and the test
    harness, where ``_lib_fmt`` (a foundational kernel) is already imported, so
    the ``sys.modules`` fast path is what runs in practice. The path-load
    fallback differs from ``_import_share_lib``'s: ``_lib_share`` is dep-free,
    but ``_lib_fmt`` imports the leaf kernels ``_cctally_core`` +
    ``_lib_display_tz`` *by name*, so the fallback must put ``bin/`` on
    ``sys.path`` before executing it or those imports raise ModuleNotFoundError.
    The leaves never import back, so this stays acyclic.
    """
    import sys
    if "_lib_fmt" in sys.modules:
        return sys.modules["_lib_fmt"].stable_sum
    from pathlib import Path
    import importlib.util
    bin_dir = Path(__file__).resolve().parent
    if str(bin_dir) not in sys.path:
        sys.path.insert(0, str(bin_dir))
    spec = importlib.util.spec_from_file_location("_lib_fmt", bin_dir / "_lib_fmt.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["_lib_fmt"] = m
    try:
        spec.loader.exec_module(m)
    except Exception:
        sys.modules.pop("_lib_fmt", None)
        raise
    return m.stable_sum


stable_sum = _import_stable_sum()


# Anthropic's per-call >200K-tokens tier — kept in sync with bin/_lib_pricing.
# Callers may override via the ``tiered_threshold`` kwarg.
DEFAULT_TIERED_THRESHOLD = 200_000


# Minimum baseline samples for the per-row anomaly classifier.
# Daily mode: >=5 trailing days. Session mode: >=10 trailing sessions
# (richer signal per sample so a higher minimum keeps thin-baseline
# false positives down).
CACHE_REPORT_MIN_BASELINE_DAYS = 5
CACHE_REPORT_MIN_BASELINE_SESSIONS = 10


# Literal alias mirroring TS `CacheAnomalyReason` at
# dashboard/web/src/types/envelope.ts:71 — keeps the two surfaces in
# lockstep so a typo on either side fails type-check.
CacheAnomalyReason = Literal["net_negative", "cache_drop"]


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
class CacheBreakdownRow:
    """One row of the panel/modal by-project / by-model breakdown.

    Carried by the kernel so by-project and by-model share a single
    aggregation path. The dashboard wraps each into the SSE-side frozen
    ``CacheReportBreakdownRow`` (same field shape — only ``key`` /
    ``cache_hit_percent`` / ``net_usd`` cross the envelope boundary)
    without further transformation. The token fields stay internal:
    they're populated so the tail-aggregate "(other)" row hit-% can
    sum directly from the head rows rather than re-walking the raw
    bucket map (EFF-4).
    """
    key: str
    cache_hit_percent: float
    net_usd: float
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class CacheRow:
    # Day-mode rows carry ``date``. Session-mode rows carry ``session_id``,
    # ``project_path``, ``last_activity``, ``source_paths``. The two are
    # never populated together.
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
    anomaly_reasons: list[CacheAnomalyReason] = field(default_factory=list)

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


@dataclass
class _Bucket:
    """Per-(day,model) / per-session / per-breakdown-key aggregation accumulator.

    Used by ``_aggregate_cache_by_day``, ``_aggregate_cache_by_session``,
    and ``_aggregate_cache_breakdown`` so all three aggregators share one
    set of field names — typos become type errors, not silent runtime
    zero. The breakdown aggregator only populates the token + cache-$
    fields (``output_tokens`` / ``cost`` stay zero); that's fine — the
    by-project / by-model paths don't surface them.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0
    saved_usd: float = 0.0
    wasted_usd: float = 0.0
    net_usd: float = 0.0


@dataclass(frozen=True)
class _ProjectPartial:
    """One (day, project) net-$/token partial — a frozen cache primitive (#272 §4).

    Produced by ``aggregate_by_day_project`` and combined across days by
    ``combine_day_project_partials``. ``net_usd`` is a ``stable_sum`` over
    that (day, project) group's per-entry nets (order-independent); the
    token components are associative int sums. Frozen so a cached day
    (§5) can hold a tuple of these without any risk of tick-to-tick
    mutation (F7).
    """
    net_usd: float
    input_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int


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


def _aggregate_cache_by_day(
    entries: Iterable,
    *,
    display_tz: ZoneInfo | None,
    pricing: dict,
    cost_calculator: Callable[[str, dict, str, Optional[float]], float],
) -> list[CacheRow]:
    """Group entries by display-tz local date.

    ``display_tz`` controls bucketing. ``None`` falls back to host-local —
    matches the legacy contract for direct callers (the pre-extraction
    site was annotated "internal fallback: host-local intentional"). The
    extraction closes a pre-existing minor bug where the CLI parsed
    ``--since`` / ``--until`` in display tz but bucketed by host-local
    (spec §1.6 / plan A3); callers pass the same resolved tz they used
    for window parsing.

    ``cost_calculator`` is the per-entry cost function (the CLI passes
    ``_calculate_entry_cost`` with embedded pricing; the dashboard
    snapshot builder injects the same). Required: the kernel does not
    fall back to a default so production callers can't accidentally
    bypass the embedded pricing tables.

    Overlaps with ``_lib_aggregators._aggregate_buckets`` but kept
    separate: the cache-report kernel is purity-contract (no internal
    imports per module docstring), and the day-bucket shape diverges
    (per-model breakdown children, cache-dollar tiered math). Cross-ref
    for future unification if the kernel ever takes an
    ``_lib_pricing`` dependency.

    Callers pre-filter entries to the desired window via their own
    ``get_entries`` query; the kernel does not re-filter.
    """
    tz = _resolve_bucket_tz(display_tz)

    day_model_buckets: dict[str, dict[str, _Bucket]] = {}
    for entry in entries:
        # ``entry.timestamp`` is an aware UTC datetime per SessionEntry
        # contract; ``astimezone(tz)`` shifts to the display tz before
        # taking the calendar date.
        day_key = entry.timestamp.astimezone(tz).strftime("%Y-%m-%d")
        cost = cost_calculator(entry.model, entry.usage, "auto", entry.cost_usd)
        create_tok = entry.usage.get("cache_creation_input_tokens", 0)
        read_tok = entry.usage.get("cache_read_input_tokens", 0)
        saved, wasted, net = _compute_entry_cache_dollars(
            entry.model, create_tok, read_tok, pricing=pricing,
        )
        models = day_model_buckets.setdefault(day_key, {})
        b = models.setdefault(entry.model, _Bucket())
        b.input_tokens += entry.usage.get("input_tokens", 0)
        b.output_tokens += entry.usage.get("output_tokens", 0)
        b.cache_creation_tokens += create_tok
        b.cache_read_tokens += read_tok
        b.cost += cost
        b.saved_usd += saved
        b.wasted_usd += wasted
        b.net_usd += net

    result: list[CacheRow] = []
    for day_key in sorted(day_model_buckets.keys()):
        models = day_model_buckets[day_key]
        row = CacheRow(date=day_key)
        for model_name in sorted(models.keys()):
            b = models[model_name]
            mb = CacheModelBreakdown(
                model_name=model_name,
                input_tokens=b.input_tokens,
                output_tokens=b.output_tokens,
                cache_creation_tokens=b.cache_creation_tokens,
                cache_read_tokens=b.cache_read_tokens,
                cache_hit_percent=_compute_cache_hit_percent(
                    b.input_tokens, b.cache_creation_tokens, b.cache_read_tokens
                ),
                cost=b.cost,
                saved_usd=b.saved_usd,
                wasted_usd=b.wasted_usd,
                net_usd=b.net_usd,
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


@dataclass
class _SessionAggregationResult:
    """Bundles session rows + the fallback warning count.

    Returned by ``_aggregate_cache_by_session`` so callers can choose
    whether to emit the "N entries lacked session_files rows" one-shot
    warning. The CLI adapter consumes ``fallback_count`` to emit the
    legacy stderr line; the dashboard snapshot builder ignores it (the
    panel surfaces freshness via the doctor chip instead).
    """
    rows: list[CacheRow]
    fallback_count: int


def _aggregate_cache_by_session(
    entries: Iterable,
    *,
    pricing: dict,
    cost_calculator: Callable[[str, dict, str, Optional[float]], float],
    project_decoder: Callable[[str], str],
) -> _SessionAggregationResult:
    """Group Claude entries by sessionId (resumed-merged).

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

    ``cost_calculator`` / ``pricing`` / ``project_decoder`` are required
    keyword-only — production callers inject ``_calculate_entry_cost`` +
    ``CLAUDE_MODEL_PRICING`` + a ``_decode_escaped_cwd``-backed decoder
    so the kernel stays free of pricing globals / cost-dispatch I/O.

    Callers pre-filter entries to the desired window via their own
    ``get_claude_session_entries`` query; the kernel does not re-filter.
    """
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
            "project_path": project_decoder(entry.source_path),
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
        model_buckets: dict[str, _Bucket] = {}
        for entry in b["entries"]:
            mb_raw = model_buckets.setdefault(entry.model, _Bucket())
            mb_raw.input_tokens += entry.input_tokens
            mb_raw.output_tokens += entry.output_tokens
            mb_raw.cache_creation_tokens += entry.cache_creation_tokens
            mb_raw.cache_read_tokens += entry.cache_read_tokens
            mb_raw.cost += cost_calculator(
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
            mb_raw.saved_usd += saved
            mb_raw.wasted_usd += wasted
            mb_raw.net_usd += net

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
                input_tokens=mb_raw.input_tokens,
                output_tokens=mb_raw.output_tokens,
                cache_creation_tokens=mb_raw.cache_creation_tokens,
                cache_read_tokens=mb_raw.cache_read_tokens,
                cache_hit_percent=_compute_cache_hit_percent(
                    mb_raw.input_tokens,
                    mb_raw.cache_creation_tokens,
                    mb_raw.cache_read_tokens,
                ),
                cost=mb_raw.cost,
                saved_usd=mb_raw.saved_usd,
                wasted_usd=mb_raw.wasted_usd,
                net_usd=mb_raw.net_usd,
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


# ---------------------------------------------------------------------------
# Anomaly classification + baseline median
# ---------------------------------------------------------------------------

def _row_anchor(r: CacheRow) -> dt.datetime | None:
    """Return the row's position in time for baseline-window comparison.

    Session rows carry ``last_activity`` (an aware datetime); daily rows
    carry ``date`` (an ISO-8601 ``YYYY-MM-DD``). For daily rows we use
    ``.astimezone()`` (not ``.replace(tzinfo=...)``) so the OS tzdb
    gives the correct offset for the given date — avoids DST drift on
    dates that straddle a DST boundary. Mirrors the idiom in
    ``_parse_cli_date_range``.
    """
    if r.last_activity is not None:
        return r.last_activity
    if r.date:
        # internal fallback: host-local intentional
        return dt.datetime.strptime(r.date, "%Y-%m-%d").astimezone()
    return None


def _compute_baseline_median(
    rows: list[CacheRow],
    *,
    anchor: dt.datetime,
    window_days: int,
    min_samples: int,
    exclude_row: CacheRow | None = None,
    is_session_mode: bool = False,
) -> float | None:
    """Median ``cache_hit_percent`` across rows whose anchor falls in
    ``[anchor − window_days, anchor − upper_offset]``.

    Returns ``None`` when fewer than ``min_samples`` rows qualify. The
    upper offset is ``1s`` in session mode (recent sessions stay
    eligible even when they collide on the second) and ``1d`` in daily
    mode (yesterday IS in the baseline but today is excluded).

    ``exclude_row`` lets the per-row classifier skip the focal row when
    computing the baseline median for that row — without this, a row's
    own hit % would self-include in its baseline. Callers passing the
    cross-row "median over the whole window" (e.g. the dashboard
    spotlight) leave ``exclude_row=None``.
    """
    import statistics

    upper_offset = (
        dt.timedelta(seconds=1) if is_session_mode else dt.timedelta(days=1)
    )
    lower_bound = anchor - dt.timedelta(days=window_days)
    upper_bound = anchor - upper_offset
    values: list[float] = []
    for r in rows:
        if exclude_row is not None and r is exclude_row:
            continue
        ra = _row_anchor(r)
        if ra is None:
            continue
        if lower_bound <= ra <= upper_bound:
            values.append(r.cache_hit_percent)
    if len(values) < min_samples:
        return None
    return statistics.median(values)


def _classify_anomalies(
    rows: list[CacheRow],
    *,
    threshold_pp: int,
    window_days: int,
    enabled: bool = True,
) -> None:
    """Mutate each row's ``anomaly_triggered`` / ``anomaly_reasons`` in place.

    Trigger 1 (``net_negative``): ``net_usd < 0`` (strict). Skipped when the
    row has zero cache activity (no-op session, not a bug).

    Trigger 2 (``cache_drop``): ``cache_hit_percent`` is ``>= threshold_pp``
    below the trailing ``window_days`` median of OTHER rows. Requires
    a minimum of ``CACHE_REPORT_MIN_BASELINE_DAYS`` (daily) or
    ``CACHE_REPORT_MIN_BASELINE_SESSIONS`` (session) baseline samples;
    silently skipped otherwise.

    Reasons are appended in deterministic order: ``net_negative`` first
    (no baseline needed), then ``cache_drop`` (matches the
    pre-extraction order tests / fixtures expect).

    Mode is inferred from the first row: if it has a ``session_id``,
    session mode (window_days back to ``<= last_activity − 1s``);
    else daily mode (window_days back to ``<= date − 1 day``).
    """
    if not enabled:
        for row in rows:
            row.anomaly_triggered = False
            row.anomaly_reasons = []
        return
    if not rows:
        return

    is_session_mode = rows[0].session_id is not None
    min_baseline = (
        CACHE_REPORT_MIN_BASELINE_SESSIONS if is_session_mode
        else CACHE_REPORT_MIN_BASELINE_DAYS
    )

    # Pre-compute anchors once to avoid O(n²·datetime-parse) overhead.
    anchors: list[dt.datetime | None] = [_row_anchor(r) for r in rows]

    for i, row in enumerate(rows):
        reasons: list[CacheAnomalyReason] = []

        # Trigger 1: net_negative (no baseline needed; cache-activity guard).
        if row.cache_creation_tokens + row.cache_read_tokens > 0:
            if row.net_usd < 0:
                reasons.append("net_negative")

        # Trigger 2: cache_drop (requires baseline).
        anchor = anchors[i]
        if anchor is not None:
            median = _compute_baseline_median(
                rows, anchor=anchor,
                window_days=window_days, min_samples=min_baseline,
                exclude_row=row, is_session_mode=is_session_mode,
            )
            if median is not None and (median - row.cache_hit_percent) >= threshold_pp:
                reasons.append("cache_drop")

        row.anomaly_reasons = reasons
        row.anomaly_triggered = bool(reasons)


# ---------------------------------------------------------------------------
# Window-wide breakdown aggregator (by-project / by-model dedup)
# ---------------------------------------------------------------------------

def _finalize_breakdown_rows(
    rows: list["CacheBreakdownRow"],
    *,
    top_n: int = 5,
    other_label: str = "(other)",
) -> tuple["CacheBreakdownRow", ...]:
    """Sort by ``abs(net_usd)`` desc, keep ``top_n``, fold the tail into ``(other)``.

    Shared by ``_aggregate_cache_breakdown``,
    ``_aggregate_cache_breakdown_from_rows``, and
    ``combine_day_project_partials`` (#272 §4) so the sort / top-N /
    ``(other)`` contract lives in one place. Byte-identical to the prior
    inline tail: the ``(other)`` net is a ``stable_sum`` over the tail's
    per-row nets and its ``cache_hit_percent`` is the true aggregate over
    the tail's summed token components (EFF-4).
    """
    rows.sort(key=lambda r: abs(r.net_usd), reverse=True)
    if len(rows) <= top_n:
        return tuple(rows)
    head = rows[:top_n]
    tail = rows[top_n:]
    other_net = stable_sum(r.net_usd for r in tail)
    tail_input = sum(r.input_tokens for r in tail)
    tail_creation = sum(r.cache_creation_tokens for r in tail)
    tail_read = sum(r.cache_read_tokens for r in tail)
    other_pct = _compute_cache_hit_percent(tail_input, tail_creation, tail_read)
    head.append(CacheBreakdownRow(
        key=other_label, cache_hit_percent=other_pct, net_usd=other_net,
        input_tokens=tail_input,
        cache_creation_tokens=tail_creation,
        cache_read_tokens=tail_read,
    ))
    return tuple(head)


def _aggregate_cache_breakdown(
    entries: Iterable,
    *,
    key_fn: Callable[[Any], str],
    pricing: dict,
    skip_synthetic: bool = True,
    top_n: int = 5,
    other_label: str = "(other)",
) -> tuple[CacheBreakdownRow, ...]:
    """Sum cache hit % + net $ per bucket; top ``top_n`` + ``(other)``.

    Single source of truth for the dashboard's by-project AND by-model
    breakdowns (spec §4.2). The caller injects ``key_fn`` to pick the
    bucket label per entry:

    - by-project: ``lambda e: getattr(e, "project_path", None) or "(unknown)"``
    - by-model:   ``lambda e: e.model``

    ``skip_synthetic`` drops ``e.model == "<synthetic>"`` entries before
    bucketing — Claude Code's internal markers aren't real model calls
    and would inflate token totals for whichever axis is keyed on
    something other than ``model``. Defaults to True so both axes agree
    on which entries contribute (closes the by-project / by-model
    drift previously caused by an inconsistent filter on the two
    dashboard-side helpers).

    Sorted by ``abs(net_usd)`` desc. When there are more than ``top_n``
    buckets, the tail collapses into a single ``(other)`` row whose
    ``cache_hit_percent`` is the TRUE aggregate hit % across the tail's
    token totals (not a placeholder zero, not the mean of the tail's
    per-bucket percentages) — matches the by-project numbers users
    would see if they widened the top-N. The aggregate is computed by
    summing the head rows' token fields rather than re-walking the raw
    bucket map (EFF-4).
    """
    buckets: dict[str, _Bucket] = {}
    for e in entries:
        if skip_synthetic and getattr(e, "model", None) == "<synthetic>":
            continue
        key = key_fn(e)
        b = buckets.setdefault(key, _Bucket())
        b.input_tokens += getattr(e, "input_tokens", 0)
        b.cache_creation_tokens += getattr(e, "cache_creation_tokens", 0)
        b.cache_read_tokens += getattr(e, "cache_read_tokens", 0)
        saved, wasted, net = _compute_entry_cache_dollars(
            getattr(e, "model", ""),
            getattr(e, "cache_creation_tokens", 0),
            getattr(e, "cache_read_tokens", 0),
            pricing=pricing,
        )
        b.saved_usd += saved
        b.wasted_usd += wasted
        b.net_usd += net

    out: list[CacheBreakdownRow] = []
    for key, b in buckets.items():
        out.append(CacheBreakdownRow(
            key=key,
            cache_hit_percent=_compute_cache_hit_percent(
                b.input_tokens, b.cache_creation_tokens, b.cache_read_tokens,
            ),
            net_usd=b.net_usd,
            input_tokens=b.input_tokens,
            cache_creation_tokens=b.cache_creation_tokens,
            cache_read_tokens=b.cache_read_tokens,
        ))
    return _finalize_breakdown_rows(out, top_n=top_n, other_label=other_label)


def _aggregate_cache_breakdown_from_rows(
    rows: Iterable["CacheRow"],
    *,
    skip_synthetic: bool = True,
    top_n: int = 5,
    other_label: str = "(other)",
) -> tuple[CacheBreakdownRow, ...]:
    """By-model breakdown folded from day-mode rows.

    Day-mode ``_aggregate_cache_by_day`` already buckets per-entry cache
    dollars by ``(date, model)``. Walking those pre-aggregated buckets is
    O(rows × distinct_models) — orders of magnitude cheaper than calling
    ``_aggregate_cache_breakdown`` a second time over the raw entries
    iterable (which re-runs the tiered-pricing math per entry). Output
    is byte-equivalent to ``_aggregate_cache_breakdown(entries, key_fn=
    lambda e: e.model)`` modulo float-addition ordering.

    ``skip_synthetic`` drops the ``"<synthetic>"`` model bucket. Day-mode
    keeps synthetic entries in ``row.model_breakdowns`` because that view
    is intra-day diagnostic; the by-model view here is the user-facing
    "where did the savings land" rollup, so synthetic is dropped to match
    ``_aggregate_cache_breakdown``'s contract.
    """
    buckets: dict[str, _Bucket] = {}
    for row in rows:
        for mb in row.model_breakdowns:
            if skip_synthetic and mb.model_name == "<synthetic>":
                continue
            b = buckets.setdefault(mb.model_name, _Bucket())
            b.input_tokens += mb.input_tokens
            b.cache_creation_tokens += mb.cache_creation_tokens
            b.cache_read_tokens += mb.cache_read_tokens
            b.net_usd += mb.net_usd

    out: list[CacheBreakdownRow] = []
    for key, b in buckets.items():
        out.append(CacheBreakdownRow(
            key=key,
            cache_hit_percent=_compute_cache_hit_percent(
                b.input_tokens, b.cache_creation_tokens, b.cache_read_tokens,
            ),
            net_usd=b.net_usd,
            input_tokens=b.input_tokens,
            cache_creation_tokens=b.cache_creation_tokens,
            cache_read_tokens=b.cache_read_tokens,
        ))
    return _finalize_breakdown_rows(out, top_n=top_n, other_label=other_label)


# ---------------------------------------------------------------------------
# Two-level by_project fold (grouping-invariant; dashboard-only) — #272 §4
# ---------------------------------------------------------------------------

def aggregate_by_day_project(
    entries: "Iterable",
    *,
    display_tz: "ZoneInfo | None",
    pricing: dict,
    skip_synthetic: bool = True,
) -> "dict[str, dict[str, _ProjectPartial]]":
    """Per-(display-tz day, project) net-$/token partials (#272 §4).

    Groups RAW entries (each carrying ``.project_path`` / ``.model`` /
    ``.cache_*`` / ``.input_tokens`` / ``.timestamp``) by (day, project).
    The per-(day, project) ``net_usd`` is a ``stable_sum`` over that
    group's per-entry nets so it is order-independent
    (``get_claude_session_entries`` orders by ``timestamp_utc`` only, with
    no ``id`` tiebreak). Token components are associative int sums. Drops
    ``<synthetic>``-model entries when ``skip_synthetic`` (matches the
    by_project contract; the day-row ``model_breakdowns`` deliberately keep
    ``<synthetic>``). Day key uses ``_resolve_bucket_tz(display_tz)`` —
    identical to ``_aggregate_cache_by_day``, so keys align with
    ``CacheRow.date`` / the builder's ``kept_dates``.
    """
    tz = _resolve_bucket_tz(display_tz)
    nets: dict[tuple[str, str], list[float]] = {}
    toks: dict[tuple[str, str], list[int]] = {}  # [input, creation, read]
    for e in entries:
        model = getattr(e, "model", "")
        if skip_synthetic and model == "<synthetic>":
            continue
        day_key = e.timestamp.astimezone(tz).strftime("%Y-%m-%d")
        proj = getattr(e, "project_path", None) or "(unknown)"
        k = (day_key, proj)
        create_tok = getattr(e, "cache_creation_tokens", 0)
        read_tok = getattr(e, "cache_read_tokens", 0)
        _s, _w, net = _compute_entry_cache_dollars(
            model, create_tok, read_tok, pricing=pricing,
        )
        nets.setdefault(k, []).append(net)
        t = toks.setdefault(k, [0, 0, 0])
        t[0] += getattr(e, "input_tokens", 0)
        t[1] += create_tok
        t[2] += read_tok
    out: dict[str, dict[str, _ProjectPartial]] = {}
    for (day_key, proj), net_list in nets.items():
        t = toks[(day_key, proj)]
        out.setdefault(day_key, {})[proj] = _ProjectPartial(
            net_usd=stable_sum(net_list),
            input_tokens=t[0],
            cache_creation_tokens=t[1],
            cache_read_tokens=t[2],
        )
    return out


def combine_day_project_partials(
    partials_by_date: "dict[str, dict[str, _ProjectPartial]]",
    *,
    top_n: int = 5,
    other_label: str = "(other)",
) -> tuple["CacheBreakdownRow", ...]:
    """Combine per-(day, project) partials into the window by_project rows (#272 §4).

    Per project: ``stable_sum`` its day-partials' ``net_usd`` across the
    given dates (byte-identical whether the dates come from a fresh
    full-window fold or a per-day cache), int-sum its token components,
    compute ``cache_hit_percent`` once, then the shared top-N / ``(other)``
    fold. Dates are iterated in sorted order so the by_project map's
    insertion order is deterministic (warm == cold); with order-independent
    partials this only affects exact-``abs(net)`` tie ordering, which the
    stable sort then resolves identically on both paths.
    """
    net_lists: dict[str, list[float]] = {}
    tok: dict[str, list[int]] = {}  # [input, creation, read]
    for day_key in sorted(partials_by_date):
        for proj, p in partials_by_date[day_key].items():
            net_lists.setdefault(proj, []).append(p.net_usd)
            t = tok.setdefault(proj, [0, 0, 0])
            t[0] += p.input_tokens
            t[1] += p.cache_creation_tokens
            t[2] += p.cache_read_tokens
    rows: list[CacheBreakdownRow] = []
    for proj, nets in net_lists.items():
        t = tok[proj]
        rows.append(CacheBreakdownRow(
            key=proj,
            cache_hit_percent=_compute_cache_hit_percent(t[0], t[1], t[2]),
            net_usd=stable_sum(nets),
            input_tokens=t[0],
            cache_creation_tokens=t[1],
            cache_read_tokens=t[2],
        ))
    return _finalize_breakdown_rows(rows, top_n=top_n, other_label=other_label)


# ---------------------------------------------------------------------------
# Frozen per-day cached unit + build / reconstruct helpers (#272 §5)
# ---------------------------------------------------------------------------

class _FrozenModelBreakdown(NamedTuple):
    """Immutable mirror of ``CacheModelBreakdown`` (#272 §5, Codex-4).

    ``CacheModelBreakdown`` is a *mutable* dataclass; a cached day must hold
    its per-model children as frozen primitives so nothing reachable from a
    published ``DataSnapshot`` is ever mutated tick-to-tick (F7). Field names
    and order mirror ``CacheModelBreakdown`` EXACTLY (``:79``) so
    ``reconstruct_cache_row`` can rebuild an equal mutable child by keyword.
    """
    model_name: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cache_hit_percent: float
    cost: float
    saved_usd: float
    wasted_usd: float
    net_usd: float


@dataclass(frozen=True)
class CachedCacheReportDay:
    """One CLOSED day's raw aggregate + per-project partials, frozen (#272 §5).

    Pre-classification: it carries NO ``anomaly_*`` state — those are
    populated on a freshly reconstructed mutable ``CacheRow`` each tick
    (``reconstruct_cache_row``), so nothing cached is ever mutated (F7).

    Fields are the non-anomaly day-mode ``CacheRow`` scalars (``:113``) —
    ``cache_hit_percent`` is a snapshot of the row's derived property (the
    reconstructed row recomputes it from tokens, so it is informational
    here) — plus:
      - ``model_breakdowns``: a tuple of ``_FrozenModelBreakdown`` KEEPING
        ``<synthetic>`` (the day-row contract; by_model applies its own
        ``skip_synthetic`` at fold time).
      - ``project_partials``: a tuple of ``(project_key, _ProjectPartial)``
        pairs (``skip_synthetic=True`` — the by_project restitch input, §4).

    Day-mode only: the session-mode ``CacheRow`` fields (``session_id`` /
    ``project_path`` / ``last_activity`` / ``source_paths``) are never
    populated by ``_aggregate_cache_by_day`` (they stay at their defaults),
    so they are not stored; ``reconstruct_cache_row`` leaves them at the
    ``CacheRow`` defaults, which the round-trip equality test confirms.
    """
    date: str
    cache_hit_percent: float
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost: float
    saved_usd: float
    wasted_usd: float
    net_usd: float
    model_breakdowns: tuple  # tuple[_FrozenModelBreakdown, ...]
    project_partials: tuple   # tuple[tuple[str, _ProjectPartial], ...]


def build_cached_days(
    day_entries: Iterable,
    raw_entries: Iterable,
    *,
    display_tz: ZoneInfo | None,
    pricing: dict,
    cost_calculator: Callable[[str, dict, str, Optional[float]], float],
) -> "dict[str, CachedCacheReportDay]":
    """Per-day frozen aggregates + project partials for the whole input (#272 §5).

    Runs ``_aggregate_cache_by_day(day_entries, ...)`` for the day rows and
    ``aggregate_by_day_project(raw_entries, ...)`` for the per-project
    partials, then zips them per display-tz date into a
    ``CachedCacheReportDay``. Both aggregators bucket via
    ``_resolve_bucket_tz(display_tz)``, so their date keys align.

    ``day_entries`` are the ``SimpleNamespace`` wrappers (``.usage`` dict,
    no ``.project_path``); ``raw_entries`` are the raw joined entries
    (flat ``.project_path`` / ``.cache_*`` / ``.input_tokens``). The day
    rows are stored pre-classification (no ``anomaly_*``); the caller
    reconstructs mutable rows and classifies fresh each tick.
    """
    rows = _aggregate_cache_by_day(
        day_entries,
        display_tz=display_tz, pricing=pricing,
        cost_calculator=cost_calculator,
    )
    partials = aggregate_by_day_project(
        raw_entries, display_tz=display_tz, pricing=pricing, skip_synthetic=True,
    )
    out: dict[str, CachedCacheReportDay] = {}
    for r in rows:
        mbs = tuple(
            _FrozenModelBreakdown(
                model_name=mb.model_name,
                input_tokens=mb.input_tokens,
                output_tokens=mb.output_tokens,
                cache_creation_tokens=mb.cache_creation_tokens,
                cache_read_tokens=mb.cache_read_tokens,
                cache_hit_percent=mb.cache_hit_percent,
                cost=mb.cost,
                saved_usd=mb.saved_usd,
                wasted_usd=mb.wasted_usd,
                net_usd=mb.net_usd,
            )
            for mb in r.model_breakdowns
        )
        pp = tuple(sorted((partials.get(r.date, {}) or {}).items()))
        out[r.date] = CachedCacheReportDay(
            date=r.date,
            cache_hit_percent=r.cache_hit_percent,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_creation_tokens=r.cache_creation_tokens,
            cache_read_tokens=r.cache_read_tokens,
            cost=r.cost,
            saved_usd=r.saved_usd,
            wasted_usd=r.wasted_usd,
            net_usd=r.net_usd,
            model_breakdowns=mbs,
            project_partials=pp,
        )
    return out


def reconstruct_cache_row(cached: "CachedCacheReportDay") -> CacheRow:
    """A fresh MUTABLE ``CacheRow`` from a frozen cached day (#272 §5, F7).

    Rebuilds a brand-new mutable ``CacheRow`` (+ mutable
    ``CacheModelBreakdown`` children) equal to the original
    ``_aggregate_cache_by_day`` row modulo the anomaly fields, which are
    cleared so ``_classify_anomalies`` can repopulate them each tick over a
    row it fully owns. NOTHING reachable from ``cached`` is aliased — the
    frozen unit is never mutated.

    ``cache_hit_percent`` is a read-only *property* on ``CacheRow`` (derived
    from the token counters), so it is NOT assigned here; setting the tokens
    makes the property recompute the stored value. The session-mode fields
    (``session_id`` / ``project_path`` / ``last_activity`` / ``source_paths``)
    stay at the ``CacheRow`` defaults — day-mode rows never carry them.
    """
    row = CacheRow(date=cached.date)
    row.input_tokens = cached.input_tokens
    row.output_tokens = cached.output_tokens
    row.cache_creation_tokens = cached.cache_creation_tokens
    row.cache_read_tokens = cached.cache_read_tokens
    row.cost = cached.cost
    row.saved_usd = cached.saved_usd
    row.wasted_usd = cached.wasted_usd
    row.net_usd = cached.net_usd
    row.model_breakdowns = [
        CacheModelBreakdown(
            model_name=m.model_name,
            input_tokens=m.input_tokens,
            output_tokens=m.output_tokens,
            cache_creation_tokens=m.cache_creation_tokens,
            cache_read_tokens=m.cache_read_tokens,
            cache_hit_percent=m.cache_hit_percent,
            cost=m.cost,
            saved_usd=m.saved_usd,
            wasted_usd=m.wasted_usd,
            net_usd=m.net_usd,
        )
        for m in cached.model_breakdowns
    ]
    row.anomaly_reasons = []
    row.anomaly_triggered = False
    return row


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

@dataclass
class _CacheReportResult:
    """Internal dataclass returned by ``_build_cache_report``.

    Consumed by both the CLI renderer (which formats into table or JSON)
    and the dashboard snapshot builder (which shapes into
    ``CacheReportSnapshot`` for the SSE envelope). ``display_tz_key`` is
    the resolved IANA zone name (or ``None`` when the caller passed
    ``display_tz=None`` and the kernel fell back to host-local).

    ``today_baseline_median`` is the median cache_hit_percent across
    "other" rows (excluding today's row) over the trailing
    ``anomaly_window_days`` — populated in day mode only (session mode
    has no equivalent "today" concept). Surfaced here so the dashboard
    snapshot builder can read it without re-running
    ``_compute_baseline_median`` over the same data (EFF-3).
    """
    rows: list[CacheRow]
    mode: Literal["day", "session"]
    window_days: int
    anomaly_threshold_pp: int
    anomaly_window_days: int
    display_tz_key: str | None
    today_baseline_median: float | None = None


def _aggregate_cache_report_rows(
    entries: Iterable,
    *,
    display_tz: ZoneInfo | None,
    pricing: dict,
    cost_calculator: Callable[[str, dict, str, Optional[float]], float],
    mode: Literal["day", "session"] = "day",
    project_decoder: Callable[[str], str] | None = None,
) -> list[CacheRow]:
    """The aggregate half of ``_build_cache_report`` (#272 §6).

    Buckets ``entries`` into ``CacheRow``s — by display-tz calendar date
    (``mode="day"``) or by Claude ``sessionId`` (``mode="session"``,
    resume-merged; requires ``project_decoder``). Split out so the
    dashboard cache path (§6) can run the aggregate over a mix of
    cached + freshly-fetched entries and then classify separately, while
    the classify phase (``classify_and_summarize``) re-runs fresh each
    tick. Byte-identical to the inline aggregate it replaced.
    """
    if mode == "day":
        return _aggregate_cache_by_day(
            entries,
            display_tz=display_tz, pricing=pricing,
            cost_calculator=cost_calculator,
        )
    if mode == "session":
        if project_decoder is None:
            raise ValueError("session mode requires project_decoder")
        return _aggregate_cache_by_session(
            entries,
            pricing=pricing,
            cost_calculator=cost_calculator,
            project_decoder=project_decoder,
        ).rows
    raise ValueError(f"unknown mode: {mode!r}")


def classify_and_summarize(
    rows: list[CacheRow],
    *,
    now_utc: dt.datetime,
    window_days: int,
    anomaly_threshold_pp: int,
    anomaly_window_days: int,
    display_tz: ZoneInfo | None,
    mode: Literal["day", "session"] = "day",
    anomaly_enabled: bool = True,
) -> _CacheReportResult:
    """The classify + summarize half of ``_build_cache_report`` (#272 §6).

    Mutates ``rows`` in place (``_classify_anomalies``), computes the
    day-mode ``today_baseline_median`` (EFF-3), and packages the
    ``_CacheReportResult``. Split out so the dashboard cache path (§6)
    can re-run this cross-row pass every tick over freshly reconstructed
    rows (the cached day aggregates carry no anomaly state), while the
    per-day aggregates are served from cache. Byte-identical to the
    inline classify half it replaced.
    """
    _classify_anomalies(
        rows,
        threshold_pp=anomaly_threshold_pp,
        window_days=anomaly_window_days,
        enabled=anomaly_enabled,
    )

    # EFF-3: surface today's baseline median directly on the result so
    # the dashboard snapshot builder doesn't have to re-run
    # _compute_baseline_median over the same row set. Day-mode only —
    # session mode has no equivalent "today" anchor concept. Anchor
    # construction mirrors the pre-EFF-3 adapter byte-for-byte —
    # the strptime + astimezone(display_tz_or_UTC) pair treats the
    # naive parsed datetime as host-local before shifting, which IS
    # the prior contract; do not change without re-verifying the
    # dashboard envelope's today.baseline_median_percent stays stable
    # against the existing golden fixtures.
    today_baseline_median: float | None = None
    if mode == "day":
        today_iso = now_utc.astimezone(
            display_tz if display_tz is not None else dt.timezone.utc
        ).strftime("%Y-%m-%d")
        today_anchor = dt.datetime.strptime(today_iso, "%Y-%m-%d").astimezone(
            display_tz if display_tz is not None else dt.timezone.utc
        )
        other_rows = [r for r in rows if r.date != today_iso]
        today_baseline_median = _compute_baseline_median(
            other_rows,
            anchor=today_anchor,
            window_days=anomaly_window_days,
            min_samples=CACHE_REPORT_MIN_BASELINE_DAYS,
        )

    return _CacheReportResult(
        rows=rows,
        mode=mode,
        window_days=window_days,
        anomaly_threshold_pp=anomaly_threshold_pp,
        anomaly_window_days=anomaly_window_days,
        display_tz_key=display_tz.key if display_tz is not None else None,
        today_baseline_median=today_baseline_median,
    )


def _build_cache_report(
    entries: Iterable,
    *,
    now_utc: dt.datetime,
    window_days: int,
    anomaly_threshold_pp: int,
    anomaly_window_days: int,
    display_tz: ZoneInfo | None,
    pricing: dict,
    cost_calculator: Callable[[str, dict, str, Optional[float]], float],
    mode: Literal["day", "session"] = "day",
    project_decoder: Callable[[str], str] | None = None,
    anomaly_enabled: bool = True,
) -> _CacheReportResult:
    """Top-level orchestrator: aggregate + classify anomalies.

    Returns a ``_CacheReportResult`` that both the CLI renderer and the
    dashboard snapshot builder consume. Pure-function — no I/O, no
    logging, no environment reads. Callers (CLI / dashboard) own all
    I/O via the ``entries`` iterable + the ``cost_calculator`` /
    ``project_decoder`` injections.

    ``mode="day"`` buckets entries by display-tz calendar date;
    ``mode="session"`` buckets by Claude ``sessionId`` (resume-merged
    across JSONL files). Session mode requires ``project_decoder`` (the
    CLI passes its ``_decode_escaped_cwd``-backed shim); day mode
    ignores it.

    The ``since`` window for both modes is ``now_utc − window_days``;
    the kernel trusts callers to pre-filter via their own query
    (``get_entries`` / ``get_claude_session_entries``). Composed (#272
    §6) from ``_aggregate_cache_report_rows`` + ``classify_and_summarize``
    — the CLI keeps this one-shot orchestrator; the dashboard cache path
    calls the two halves directly so it can memoize the aggregate.
    """
    rows = _aggregate_cache_report_rows(
        entries,
        display_tz=display_tz, pricing=pricing,
        cost_calculator=cost_calculator,
        mode=mode, project_decoder=project_decoder,
    )
    return classify_and_summarize(
        rows,
        now_utc=now_utc,
        window_days=window_days,
        anomaly_threshold_pp=anomaly_threshold_pp,
        anomaly_window_days=anomaly_window_days,
        display_tz=display_tz,
        mode=mode,
        anomaly_enabled=anomaly_enabled,
    )
