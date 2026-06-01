"""Pure-fn kernel: the ccusage-parity "Pricing Mismatch Debug Report".

No I/O at import time; no import of `cctally`. The two cost primitives it
consumes (`_resolve_model_pricing`, `_calculate_entry_cost`) are honest-
imported from `_lib_pricing` (same `sys.modules` instance bin/cctally
re-exports). `UsageEntry` is duck-typed (attribute reads only). bin/cctally
re-exports every symbol below so internal call sites resolve unchanged.

Extracted from bin/cctally (#125 Batch E, C9). Spec:
docs/superpowers/specs/2026-06-01-extract-pricing-setup-glue-design.md
Original feature: issue #89 (ccusage detectMismatches/printMismatchReport).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from _lib_pricing import _resolve_model_pricing, _calculate_entry_cost


@dataclass
class _MismatchModelStat:
    total: int = 0
    matches: int = 0
    mismatches: int = 0
    avg_percent_diff: float = 0.0


@dataclass
class _MismatchSample:
    file: str
    timestamp: str
    model: str
    original_cost: float
    calculated_cost: float
    difference: float
    percent_diff: float
    usage: dict


@dataclass
class _MismatchStats:
    command_label: str | None = None
    total_entries: int = 0
    entries_with_both: int = 0
    matches: int = 0
    mismatches: int = 0
    model_stats: dict = field(default_factory=dict)
    discrepancies: list = field(default_factory=list)


def _compute_pricing_mismatch_stats(entries):
    """Walk ``entries: Iterable[UsageEntry]`` and compute the mismatch stats
    that ``_render_pricing_mismatch_report`` consumes.

    Mirrors ccusage upstream's ``detectMismatches``
    (``~/.npm/_npx/.../node_modules/ccusage/dist/debug-DvI5DUKR.js:6-95``):

    - An entry counts toward ``entries_with_both`` iff its ``cost_usd``
      is not None AND the model has pricing in ``CLAUDE_MODEL_PRICING``.
    - Threshold: ``percent_diff < 0.1`` is a match; anything else is a
      mismatch and gets appended to ``discrepancies`` in iteration order.
    - ``percent_diff`` is ``0.0`` when recorded cost is zero (parity with
      upstream's divide-by-zero guard).
    - Per-model ``avg_percent_diff`` updated by streaming mean recurrence
      to match upstream's per-row accumulation.
    """
    stats = _MismatchStats()
    for entry in entries:
        # P1.1 (issue #89 review-loop): mirror ccusage upstream's
        # ``detectMismatches`` precondition filter at debug-DvI5DUKR.js:42
        # — synthetic entries are excluded from total_entries AND skip the
        # _resolve_model_pricing call (which would otherwise emit a
        # ``[cost] unknown model: <synthetic>`` warning and mutate the
        # module-level _unknown_model_warnings set, suppressing future
        # legitimate emissions).
        if entry.model == "<synthetic>":
            continue
        stats.total_entries += 1
        if entry.cost_usd is None:
            continue
        if _resolve_model_pricing(entry.model) is None:
            continue
        stats.entries_with_both += 1
        calculated = _calculate_entry_cost(
            entry.model, entry.usage, mode="calculate",
        )
        original = float(entry.cost_usd)
        difference = abs(original - calculated)
        percent_diff = (difference / original * 100) if original > 0 else 0.0
        ms = stats.model_stats.setdefault(entry.model, _MismatchModelStat())
        ms.total += 1
        if percent_diff < 0.1:
            stats.matches += 1
            ms.matches += 1
        else:
            stats.mismatches += 1
            ms.mismatches += 1
            stats.discrepancies.append(_MismatchSample(
                file=os.path.basename(entry.source_path),
                timestamp=entry.timestamp.isoformat(),
                model=entry.model,
                original_cost=original,
                calculated_cost=calculated,
                difference=difference,
                percent_diff=percent_diff,
                usage=dict(entry.usage),
            ))
        # Streaming-mean update for avg_percent_diff (matches upstream).
        ms.avg_percent_diff = (
            ms.avg_percent_diff * (ms.total - 1) + percent_diff
        ) / ms.total
    return stats


def _render_pricing_mismatch_report(stats, sample_limit):
    """Return the report as a list of stderr lines (caller prints \\n-joined).

    Matches ccusage upstream's ``printMismatchReport``
    (debug-DvI5DUKR.js:97-145) including:
      - Early-return ``"No pricing data found to analyze."`` when
        ``entries_with_both == 0``.
      - Model Statistics + Sample Discrepancies sections omitted when
        ``mismatches == 0``.
      - Models with ``mismatches == 0`` omitted from Model Statistics.
      - Sample header prints the requested ``sample_limit`` (not min with
        discrepancies length).
    Adds ONE intentional non-upstream line: ``Command: cctally <label>``
    under the header so the report self-identifies (issue #89 acceptance
    re: "command in each sample's context").
    """
    out = []
    if stats.entries_with_both == 0:
        out.append("No pricing data found to analyze.")
        return out

    match_rate = stats.matches / stats.entries_with_both * 100
    out.append("")
    out.append("=== Pricing Mismatch Debug Report ===")
    if stats.command_label:
        out.append(f"Command: cctally {stats.command_label}")
    out.append(f"Total entries processed: {stats.total_entries:,}")
    out.append(
        f"Entries with both costUSD and model: {stats.entries_with_both:,}"
    )
    out.append(f"Matches (within 0.1%): {stats.matches:,}")
    out.append(f"Mismatches: {stats.mismatches:,}")
    out.append(f"Match rate: {match_rate:.2f}%")

    if stats.mismatches > 0 and stats.model_stats:
        out.append("")
        out.append("=== Model Statistics ===")
        sorted_models = sorted(
            stats.model_stats.items(),
            key=lambda kv: -kv[1].mismatches,
        )
        for model, ms in sorted_models:
            if ms.mismatches == 0:
                continue
            rate = ms.matches / ms.total * 100
            out.append(f"{model}:")
            out.append(f"  Total entries: {ms.total:,}")
            out.append(f"  Matches: {ms.matches:,} ({rate:.1f}%)")
            out.append(f"  Mismatches: {ms.mismatches:,}")
            out.append(f"  Avg % difference: {ms.avg_percent_diff:.1f}%")

    if stats.discrepancies and sample_limit > 0:
        out.append("")
        out.append(f"=== Sample Discrepancies (first {sample_limit}) ===")
        for d in stats.discrepancies[:sample_limit]:
            out.append(f"File: {d.file}")
            out.append(f"Timestamp: {d.timestamp}")
            out.append(f"Model: {d.model}")
            out.append(f"Original cost: ${d.original_cost:.6f}")
            out.append(f"Calculated cost: ${d.calculated_cost:.6f}")
            out.append(
                f"Difference: ${d.difference:.6f} ({d.percent_diff:.2f}%)"
            )
            out.append(f"Tokens: {json.dumps(d.usage)}")
            out.append("---")
    return out
