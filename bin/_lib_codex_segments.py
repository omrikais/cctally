"""#463 S1 — the pure segmentation kernel for Codex conversation turns.

A **segment** is a run of consecutive fold groups inside one ``klass ==
"response"`` canonical item, taken greedily from the start of the turn, closed
when the block budget is reached, and closed earlier when a semantic boundary
falls inside the budget window. Items whose class is not ``response`` already
contain a single row; each is exactly one segment and its key does not change.

**The unit is a fold group, not a row, and that is not a detail.**
``_item_blocks_with_rows`` folds a ``tool_output`` into a preceding ``tool_call``
whenever the call identifier is non-empty, owned by exactly one call in the item,
and already seen — with **no adjacency requirement**. Patch, web-search and MCP
completion events fold the same way. A boundary drawn between a call and its
folded output would make the page-local builder emit a different block structure
than the whole-turn builder does, so a fold group is atomic here. Because folds
are non-adjacent a group can span intervening blocks, so a group that exceeds the
budget becomes its own segment: the budget is a target with fold-group atomicity
as a hard floor, and the ceiling is the budget plus at most one maximal group.

This module is deliberately pure — no SQLite, no I/O, and no import from the
query layer. The caller derives fold groups and the turn-scoped
``call_owner_count`` and passes them in.
"""
from __future__ import annotations

import dataclasses
from typing import Any

# Per-segment block budget (spec section 2). At the measured 27.5 DOM nodes per
# block that is roughly 1,100 nodes, close to the 776 nodes per mounted row the
# Claude control paints in 449 ms.
SEGMENT_BLOCK_BUDGET = 40

# Per-page block budget, applied alongside ``limit`` (spec section 2). Roughly
# fifty full segments, in the same range as the Claude control's 2.58 MB page.
# The per-page bound is not optional: the profiled response was
# ``total: 78, returned: 78, has_after: false`` — 13.3 MB in one page, because
# 78 is fewer than the requested 500 — so a change that capped items alone would
# not bound that conversation at all.
PAGE_BLOCK_BUDGET = 2000

# Per-page SOURCE-byte budget, applied alongside ``limit`` and
# ``PAGE_BLOCK_BUDGET``; the first bound reached closes the page. It bounds
# TRANSFER and PARSE cost, which is a byte cost the block budget does not
# express, because a Codex block is far heavier than a Claude block.
#
# It is not redundant with PAGE_BLOCK_BUDGET. After segmentation the profiled
# conversation is 128 segments carrying 1,906 blocks, so a whole-conversation
# page holds 1,713 blocks — BELOW the 2,000-block budget. The block bound never
# fires on it, and without this one the response is still 13.24 MB in one page.
#
# CALIBRATED BY MEASUREMENT, not by arithmetic, on 2026-08-02 against a
# read-only copy of the production store. At 3,000,000 source bytes the
# profiled conversation serves 16 of its 128 segments, 274 blocks and 2.54 MB
# on the wire — the 2 to 3 MB target, and effectively the Claude control's
# 2.58 MB page. Across the six heaviest conversations the served page at this
# budget ranges from 0.75 MB to 2.54 MB of wire. At 4,000,000 the maximum rises
# to 3.16 MB; at 2,000,000 the profiled conversation falls to 1.45 MB.
#
# Do NOT re-derive this figure by dividing a wire target by the
# whole-conversation source-to-wire ratio. That ratio is not uniform and not
# even close: the profiled conversation is 6.91x source-to-wire taken whole
# (91.52 MB source, 13.24 MB wire), but only 1.11x over the segments a 3 MB page
# actually serves (2.83 MB source, 2.54 MB wire), because its heaviest rows sit
# in the tail and are clipped hardest. Re-calibrate by measuring served pages.
PAGE_SOURCE_BYTE_BUDGET = 3_000_000

# Fraction of the budget the boundary snap may give up. Bounding it at a quarter
# guarantees a segment is never smaller than 75 percent of the budget, so the
# rule cannot produce a run of very small segments, and it preserves the ceiling
# because it only ever closes a segment EARLIER than the budget would.
LOOKBACK_FRACTION = 0.25


# Both dataclasses below are ``frozen=True`` with ``eq=True``, which makes
# Python synthesize a ``__hash__`` — and that synthesized hash raises TypeError
# here, because every instance carries a ``list`` field. Nothing hashes a
# FoldGroup or a Segment today and nothing should: they are records passed
# between two functions in one call, never dict keys or set members, and their
# identity is positional rather than structural. ``unsafe_hash`` is deliberately
# NOT set, and ``__hash__`` is set to None so the failure is an explicit
# "unhashable type" at the call site rather than a TypeError from inside a
# generated method.


@dataclasses.dataclass(frozen=True)
class FoldGroup:
    """A ``tool_call`` together with every row that folds into it, or a single
    non-folding row. Never divided across segments.

    ``is_title_boundary`` is true when the group's first row is a reasoning row
    whose stored projection produces a ``title``. ``is_tool_transition`` is true
    when it is the first ``tool_call`` following a run of assistant or reasoning
    rows. Those are the two semantic boundaries, in that priority order.

    ``first_pos`` and ``last_pos`` are the group's physical row positions inside
    its item. Because folds are non-adjacent, ``last_pos`` can be far past
    ``first_pos`` and can bracket a LATER group entirely, which is what
    ``plan_segments`` uses to keep a segment physically contiguous. ``None``
    disables that extension, for callers that have no positional information.
    """

    rows: list
    block_count: int
    source_bytes: int
    is_title_boundary: bool = False
    is_tool_transition: bool = False
    first_pos: int | None = None
    last_pos: int | None = None

    __hash__ = None


@dataclasses.dataclass(frozen=True)
class Segment:
    """One bounded run of fold groups inside a turn."""

    ordinal: int
    groups: list
    block_count: int
    source_bytes: int
    anchor_row: Any

    __hash__ = None


def _boundary_rank(group: FoldGroup) -> int:
    """Priority of the boundary a cut before this group would land on.

    Lower is better. 0 = a reasoning title, 1 = a tool transition, 2 = not a
    boundary at all.
    """
    if group.is_title_boundary:
        return 0
    if group.is_tool_transition:
        return 1
    return 2


def _extend_to_contiguous(groups: list, start: int, end: int, total: int) -> int:
    """Grow ``end`` until the segment covers a CONTIGUOUS physical row range.

    Fold-group atomicity alone does not give this (spec section 1). Because
    folds are non-adjacent, a group's rows can bracket a later group's rows —
    a native patch completion event sits between its call and that call's
    output, for instance. Cutting between the two groups would then produce
    segments whose physical ranges overlap: the earlier segment would render
    rows out of physical order, and the later segment's rows would fall inside
    the earlier one's time span.

    Groups are created in the physical order of their FIRST row, so ``first_pos``
    increases across the list. A cut before group ``end`` is therefore legal
    exactly when every chosen group ends before ``groups[end]`` begins; if it
    does not, that group is absorbed and the test repeats.

    The extension only ever GROWS a segment, so the 75 percent lookback floor is
    preserved. The ceiling becomes the budget plus the physical span of one
    maximal fold group, which is what spec section 2 states.
    """
    ends = [groups[i].last_pos for i in range(start, end)
            if groups[i].last_pos is not None]
    if not ends:
        return end
    max_last = max(ends)
    while end < total:
        nxt = groups[end].first_pos
        if nxt is None or nxt > max_last:
            break
        if groups[end].last_pos is not None:
            max_last = max(max_last, groups[end].last_pos)
        end += 1
    return end


def plan_segments(
    fold_groups: list,
    *,
    block_budget: int | None = None,
    lookback_fraction: float | None = None,
) -> list[Segment]:
    """Divide a turn's fold groups into ordered segments.

    Fills greedily from index 0. A segment closes at the last group that fits
    inside ``block_budget``, or earlier at the highest-priority semantic
    boundary whose cut point falls inside the lookback window — the range from
    ``(1 - lookback_fraction) * block_budget`` blocks up to the budget. A group
    that does not fit even into an empty segment becomes its own segment.

    **Greedy-from-start is the mechanism, not a convenience.** Every segment
    depends only on the groups before it, so appending groups to a growing turn
    leaves earlier segments — and therefore earlier segment keys — untouched.
    Computing boundaries from the end would renumber a conversation's history on
    every append. Segment keys 1..N remain durable only under that tail append;
    inserting or deleting a row before a boundary shifts every later boundary in
    the turn, and a former anchor becomes an interior row.

    ``block_budget`` and ``lookback_fraction`` resolve to the module constants
    at CALL time when omitted. They are deliberately not default ARGUMENT values:
    a default argument binds once at import, so a test that lowers or raises
    ``SEGMENT_BLOCK_BUDGET`` would silently keep the imported figure and pass
    vacuously.
    """
    if block_budget is None:
        block_budget = SEGMENT_BLOCK_BUDGET
    if lookback_fraction is None:
        lookback_fraction = LOOKBACK_FRACTION
    if block_budget <= 0:
        raise ValueError("block_budget must be positive")
    if not 0.0 <= lookback_fraction < 1.0:
        raise ValueError("lookback_fraction must be in [0.0, 1.0)")

    groups = list(fold_groups)
    total = len(groups)
    floor_blocks = block_budget - int(block_budget * lookback_fraction)

    segments: list[Segment] = []
    start = 0
    while start < total:
        # How far the budget alone reaches. At least one group always fits, so a
        # single oversized group becomes its own segment rather than stalling.
        end = start
        blocks = 0
        while end < total:
            candidate = blocks + groups[end].block_count
            if end > start and candidate > block_budget:
                break
            blocks = candidate
            end += 1

        # Look for a boundary inside the window. A cut happens BEFORE group
        # ``cut``, so that group must exist and must itself be a boundary, and
        # the blocks kept must already clear the lookback floor.
        best_cut = None
        best_rank = 2
        kept = 0
        for cut in range(start, end):
            if cut > start and kept >= floor_blocks and cut < total:
                rank = _boundary_rank(groups[cut])
                if rank < best_rank:
                    best_rank = rank
                    best_cut = cut
                    if rank == 0:
                        break
            kept += groups[cut].block_count
        if best_cut is not None:
            end = best_cut

        end = _extend_to_contiguous(groups, start, end, total)

        chosen = groups[start:end]
        segments.append(Segment(
            ordinal=len(segments),
            groups=chosen,
            block_count=sum(group.block_count for group in chosen),
            source_bytes=sum(group.source_bytes for group in chosen),
            anchor_row=chosen[0].rows[0] if chosen and chosen[0].rows else None,
        ))
        start = end
    return segments
