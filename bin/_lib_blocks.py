"""5-hour activity block grouping + JSON serialization.

Pure-fn layer (no I/O at import time): holds the `Block` dataclass and
the four helpers that group a sorted `UsageEntry` list into per-block
aggregates (`_aggregate_block`), assemble a non-gap block from those
aggregates (`_build_activity_block`), drive the recorded + heuristic
grouping pass (`_group_entries_into_blocks`), and serialize the result
to ccusage-compatible JSON (`_blocks_to_json`).

The 5-hour window constant (`BLOCK_DURATION`) and the hour-floor helper
(`_floor_to_hour`) move along with the rest of the block-grouping
domain: both are referenced from non-extracted callers in bin/cctally
(week-reset bookkeeping, dashboard plumbing) which now resolve them
through the re-export block — same identity, same value, zero behavior
change.

Sibling dependencies (loaded at module-load time via `_load_lib`):
  * `_lib_jsonl.UsageEntry` — typing + the dataclass the aggregator
    iterates over.
  * `_lib_pricing._calculate_entry_cost` — per-entry cost computation
    inside `_aggregate_block`.

This is a pure-domain leaf in the sibling graph; **zero call-time
back-references to `bin/cctally`**. No `_cctally()` accessor needed.

`bin/cctally` re-exports every public symbol below so the ~10 internal
call sites + SourceFileLoader-based tests
(`tests/test_dashboard_api_block`, `tests/test_blocks_recorded_anchor`)
resolve unchanged.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Any


def _load_lib(name: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    import importlib.util as _ilu
    p = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(name, p)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lib_jsonl = _load_lib("_lib_jsonl")
UsageEntry = _lib_jsonl.UsageEntry

_lib_pricing = _load_lib("_lib_pricing")
_calculate_entry_cost = _lib_pricing._calculate_entry_cost


@dataclass
class Block:
    start_time: dt.datetime       # Block start, floored to hour
    end_time: dt.datetime         # start_time + 5h
    actual_end_time: dt.datetime | None  # Timestamp of last entry
    is_active: bool
    is_gap: bool
    entries_count: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    total_tokens: int
    cost_usd: float
    models: list[str]             # Full model names (e.g. "claude-opus-4-6")
    burn_rate: dict[str, float] | None
    projection: dict[str, Any] | None
    anchor: str = "heuristic"     # "recorded" | "heuristic" — gap rows keep default


def _floor_to_hour(ts: dt.datetime) -> dt.datetime:
    """Floor a datetime to the start of its hour."""
    return ts.replace(minute=0, second=0, microsecond=0)


BLOCK_DURATION = dt.timedelta(hours=5)


def _group_entries_into_blocks(
    entries: list[UsageEntry],
    mode: str = "auto",
    *,
    recorded_windows: list[dt.datetime] | None = None,
    block_start_overrides: dict[dt.datetime, dt.datetime] | None = None,
    now: dt.datetime | None = None,
) -> list[Block]:
    """Group sorted UsageEntry objects into 5-hour blocks with gap detection.

    Returns a list of Block objects (activity blocks and gap blocks interleaved).
    The last block is marked active if now < block_start + 5h.

    When `recorded_windows` is non-empty, entries whose timestamp falls in
    [R - BLOCK_DURATION, R) for some R in recorded_windows are partitioned
    into per-R buckets and built as 'recorded' blocks. Leftover entries
    run through the existing gap-detection heuristic (anchor='heuristic').

    `block_start_overrides` (v1.7.2 round-5 / Bug J): an optional
    `{R → block_start_at}` map. When present for a given R, the
    recorded block's displayed ``start_time`` becomes the override
    instead of the default ``R - BLOCK_DURATION``. Used by
    ``_load_recorded_five_hour_windows`` to preserve the real
    ``five_hour_blocks.block_start_at`` for credit-truncated windows
    (an in-place credit shortens the prior 5h block's effective end
    to the credit moment, but the block's API-derived START is
    unchanged — without an override the renderer would compute
    ``start = truncated_R - 5h`` which is hours before the real start
    and confuses the user with an off-by-hours window header).

    `now` pins the current instant (typically via `_command_as_of()`). When
    omitted, falls back to wall clock so existing callers are unaffected.
    """
    if not entries:
        return []

    entries_sorted = sorted(entries, key=lambda e: e.timestamp)
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)

    recorded_windows = sorted(recorded_windows or [])
    block_start_overrides = block_start_overrides or {}

    # ── Partition entries by recorded windows ──────────────────────────
    # For each R in recorded_windows, entries whose timestamp falls in
    # [override_start_or_R-5h, R) go into recorded_buckets[R]. Everything
    # else (gaps between recorded windows, or fully outside any window)
    # drops into `leftover` and runs through the existing heuristic
    # grouper.
    #
    # Why override_start_or_R-5h, not always R-5h: a credit-truncated
    # canonical block has R = effective_reset_at_utc (e.g. 17:58Z) but
    # its real ``block_start_at`` is unchanged (e.g. 15:50Z). Using
    # `R - 5h` as the partition floor would pull entries from earlier
    # blocks (e.g. 12:58-15:50Z range) into the truncated bucket. The
    # override keeps the real start so each entry lands in the bucket
    # whose API-defined interval actually contains it.
    recorded_buckets: dict[dt.datetime, list[UsageEntry]] = {
        R: [] for R in recorded_windows
    }
    leftover: list[UsageEntry] = []
    for entry in entries_sorted:
        idx = bisect.bisect_right(recorded_windows, entry.timestamp)
        if idx < len(recorded_windows):
            R = recorded_windows[idx]
            bucket_start = block_start_overrides.get(R, R - BLOCK_DURATION)
            if bucket_start <= entry.timestamp:
                recorded_buckets[R].append(entry)
                continue
        leftover.append(entry)

    # Phase 1: Group leftover entries into raw activity blocks
    raw_blocks: list[dict[str, Any]] = []
    current_block_start: dt.datetime | None = None
    current_block_end: dt.datetime | None = None
    current_entries: list[UsageEntry] = []

    for entry in leftover:
        if current_block_end is None or entry.timestamp >= current_block_end:
            # Flush previous block
            if current_entries and current_block_start is not None and current_block_end is not None:
                raw_blocks.append({
                    "start": current_block_start,
                    "end": current_block_end,
                    "entries": current_entries,
                })
            # Start new block
            current_block_start = _floor_to_hour(entry.timestamp)
            current_block_end = current_block_start + BLOCK_DURATION
            current_entries = [entry]
        else:
            current_entries.append(entry)

    # Flush last block
    if current_entries and current_block_start is not None and current_block_end is not None:
        raw_blocks.append({
            "start": current_block_start,
            "end": current_block_end,
            "entries": current_entries,
        })

    # Clamp each raw_block's end so it cannot overlap a later recorded
    # window. Entries in `leftover` are by construction earlier than the
    # next recorded R - 5h boundary, so the heuristic block belongs to a
    # PREVIOUS 5h window that ended no later than that boundary. Without
    # this clamp, the +5h heuristic span can cross into the recorded
    # window and produce two simultaneously-active rows.
    if recorded_windows:
        for rb in raw_blocks:
            idx = bisect.bisect_right(recorded_windows, rb["start"])
            if idx < len(recorded_windows):
                next_R_start = recorded_windows[idx] - BLOCK_DURATION
                if rb["start"] < next_R_start < rb["end"]:
                    rb["end"] = next_R_start

    # Track the "actual first entry timestamp" for each block so Phase 3
    # can compute gap ends the same way the legacy interleaved code did
    # (gap.end_time = first-entry-timestamp of the next block, not the
    # floor-to-hour window start). Maps id(block) -> actual first ts.
    first_entry_ts_by_block: dict[int, dt.datetime] = {}

    # Phase 1.5: Build recorded Block objects from non-empty buckets
    recorded_block_objs: list[Block] = []
    for R in recorded_windows:
        bucket = recorded_buckets[R]
        if not bucket:
            continue
        # Display start: override when present (credit-truncated
        # canonical blocks need their real block_start_at so the
        # rendered window header matches Anthropic's actual interval);
        # default to R - BLOCK_DURATION for normal canonical anchors.
        start_time = block_start_overrides.get(R, R - BLOCK_DURATION)
        end_time = R
        bucket_sorted = sorted(bucket, key=lambda e: e.timestamp)
        blk = _build_activity_block(
            bucket_sorted, start_time, end_time, now, mode,
            anchor="recorded",
        )
        first_entry_ts_by_block[id(blk)] = bucket_sorted[0].timestamp
        recorded_block_objs.append(blk)

    # Phase 2: Build heuristic Block objects from raw_blocks using _aggregate_block
    heuristic_block_objs: list[Block] = []
    for rb in raw_blocks:
        block_entries = rb["entries"]
        start_time = rb["start"]
        end_time = rb["end"]
        blk = _build_activity_block(
            block_entries, start_time, end_time, now, mode,
            anchor="heuristic",
        )
        if block_entries:
            first_entry_ts_by_block[id(blk)] = block_entries[0].timestamp
        heuristic_block_objs.append(blk)

    # Merge + sort by start_time
    all_blocks = sorted(
        recorded_block_objs + heuristic_block_objs,
        key=lambda b: b.start_time,
    )

    # Phase 3: Gap-row insertion as a post-pass over the merged list.
    # Preserves legacy gap semantics: gap.start = prev.actual_end_time,
    # gap.end = first-entry-timestamp of next block (NOT floor-to-hour).
    final_blocks: list[Block] = []
    for i, b in enumerate(all_blocks):
        if i > 0:
            prev = all_blocks[i - 1]
            if prev.end_time < b.start_time:
                first_entry_ts = first_entry_ts_by_block.get(id(b), b.start_time)
                prev_actual_end = prev.actual_end_time or prev.end_time
                final_blocks.append(Block(
                    start_time=prev_actual_end,
                    end_time=first_entry_ts,
                    actual_end_time=None,
                    is_active=False,
                    is_gap=True,
                    entries_count=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_creation_tokens=0,
                    cache_read_tokens=0,
                    total_tokens=0,
                    cost_usd=0.0,
                    models=[],
                    burn_rate=None,
                    projection=None,
                ))
        final_blocks.append(b)

    return final_blocks


def _aggregate_block(
    entries: list[UsageEntry],
    start_time: dt.datetime,
    end_time: dt.datetime,
    now: dt.datetime,
    mode: str,
) -> dict[str, Any]:
    """Aggregate token / cost / burn / projection for one block's entries.

    Pure function — no I/O. Shared by the recorded-block path and the
    heuristic-block path in `_group_entries_into_blocks` so per-block
    math stays in one place.

    Returns a dict with keys:
        input_tokens, output_tokens, cache_creation_tokens,
        cache_read_tokens, total_tokens, cost_usd, models,
        burn_rate (dict|None), projection (dict|None)
    """
    total_input = 0
    total_output = 0
    total_cc = 0
    total_cr = 0
    total_cost = 0.0
    model_set: set[str] = set()
    for entry in entries:
        usage = entry.usage
        total_input += usage.get("input_tokens", 0)
        total_output += usage.get("output_tokens", 0)
        total_cc += usage.get("cache_creation_input_tokens", 0)
        total_cr += usage.get("cache_read_input_tokens", 0)
        total_cost += _calculate_entry_cost(
            entry.model, usage, mode=mode, cost_usd=entry.cost_usd,
        )
        model_set.add(entry.model)
    total_tokens = total_input + total_output + total_cc + total_cr

    burn_rate = None
    projection = None
    is_active = now < end_time
    if is_active:
        elapsed = (now - start_time).total_seconds()
        elapsed_minutes = elapsed / 60.0
        remaining_seconds = (end_time - now).total_seconds()
        remaining_minutes = max(remaining_seconds / 60.0, 0)
        if elapsed_minutes > 0:
            tokens_per_minute = total_tokens / elapsed_minutes
            cost_per_hour = (total_cost / elapsed_minutes) * 60
            burn_rate = {
                "tokensPerMinute": tokens_per_minute,
                "costPerHour": cost_per_hour,
            }
            total_block_minutes = BLOCK_DURATION.total_seconds() / 60.0
            projected_tokens = tokens_per_minute * total_block_minutes
            projected_cost = (cost_per_hour / 60.0) * total_block_minutes
            projection = {
                "totalTokens": int(projected_tokens),
                "totalCost": round(projected_cost, 2),
                "remainingMinutes": int(remaining_minutes),
            }

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_creation_tokens": total_cc,
        "cache_read_tokens": total_cr,
        "total_tokens": total_tokens,
        "cost_usd": total_cost,
        "models": sorted(model_set),
        "burn_rate": burn_rate,
        "projection": projection,
    }


def _build_activity_block(
    entries: list[UsageEntry],
    start_time: dt.datetime,
    end_time: dt.datetime,
    now: dt.datetime,
    mode: str,
    *,
    anchor: str,
) -> Block:
    """Build a non-gap Block from a pre-sorted entries list.

    Shared by the recorded-window path (anchor='recorded') and the
    heuristic-grouping path (anchor='heuristic') inside
    `_group_entries_into_blocks`. Keeps per-block field assembly in one
    place so the two builder sites cannot drift.

    `entries` may be empty; `actual_end_time` is `None` in that case
    (mirrors the legacy heuristic behavior). Callers that need to
    populate the gap-row side-map (first-entry timestamp) do so on the
    returned Block — that side-map write is deliberately left at the
    call site so each caller can gate it on its own emptiness rule.
    """
    agg = _aggregate_block(entries, start_time, end_time, now, mode)
    return Block(
        start_time=start_time,
        end_time=end_time,
        actual_end_time=entries[-1].timestamp if entries else None,
        is_active=now < end_time,
        is_gap=False,
        entries_count=len(entries),
        input_tokens=agg["input_tokens"],
        output_tokens=agg["output_tokens"],
        cache_creation_tokens=agg["cache_creation_tokens"],
        cache_read_tokens=agg["cache_read_tokens"],
        total_tokens=agg["total_tokens"],
        cost_usd=agg["cost_usd"],
        models=agg["models"],
        burn_rate=agg["burn_rate"],
        projection=agg["projection"],
        anchor=anchor,
    )


def _blocks_to_json(blocks: list[Block]) -> str:
    """Serialize blocks to JSON matching upstream ccusage's output structure."""

    def _iso_utc(ts: dt.datetime) -> str:
        return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{ts.astimezone(dt.timezone.utc).microsecond // 1000:03d}Z"

    result = []
    for block in blocks:
        if block.is_gap:
            block_id = f"gap-{_iso_utc(block.start_time)}"
        else:
            block_id = _iso_utc(block.start_time)

        obj: dict[str, Any] = {
            "id": block_id,
            "startTime": _iso_utc(block.start_time),
            "endTime": _iso_utc(block.end_time),
            "actualEndTime": _iso_utc(block.actual_end_time) if block.actual_end_time else None,
            "isActive": block.is_active,
            "isGap": block.is_gap,
        }
        if not block.is_gap:
            obj["anchor"] = block.anchor
        obj.update({
            "entries": block.entries_count,
            "tokenCounts": {
                "inputTokens": block.input_tokens,
                "outputTokens": block.output_tokens,
                "cacheCreationInputTokens": block.cache_creation_tokens,
                "cacheReadInputTokens": block.cache_read_tokens,
            },
            "totalTokens": block.total_tokens,
            "costUSD": block.cost_usd,
            "models": block.models,
            "burnRate": block.burn_rate,
            "projection": block.projection,
        })
        result.append(obj)

    return json.dumps({"blocks": result}, indent=2)
