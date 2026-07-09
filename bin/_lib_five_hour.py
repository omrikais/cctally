"""5-hour window canonical-key primitives.

Pure-fn layer (no I/O at import time): holds the two jitter-tolerant
floors that route every 5h-window identity decision through a single
granularity. `_canonical_5h_window_key` is the epoch-int chokepoint and
`_floor_to_ten_minutes` the datetime equivalent — CLAUDE.md's "5h window
key MUST go through `_canonical_5h_window_key`" invariant lives here.

Both helpers share `_FIVE_HOUR_JITTER_FLOOR_SECONDS = 600` so neither can
drift independently; the regression `bin/cctally-5h-canonical-test`
pins the cross-shape equivalence (epoch-int → datetime → epoch round-trip
matches the modulo floor on 600-aligned base epochs).

`bin/cctally` re-exports every symbol below so internal call sites and
SourceFileLoader-based tests/fixtures (`tests/test_blocks_recorded_anchor`,
`tests/test_five_hour_block_selector`, `tests/test_five_hour_blocks_json`,
`tests/test_five_hour_breakdown`, `bin/cctally-5h-canonical-test`,
`bin/cctally-record-usage-selfheal-test`) resolve unchanged. No
cross-sibling dependencies — this is a true leaf in the sibling graph.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import datetime as dt
import math


_FIVE_HOUR_JITTER_FLOOR_SECONDS = 600  # 10 minutes; tolerance band for resets_at jitter


def five_hour_milestone_range(pct: float, max_existing: "int | None") -> range:
    """Which integer 5h-% thresholds ``maybe_update_five_hour_block`` should
    attempt to record for ``pct``, given the ACTIVE segment's highest already-
    recorded threshold ``max_existing`` (#279 S4 F4).

    Mirrors the milestone-detection loop's fencing exactly (glue call site:
    ``maybe_update_five_hour_block``'s 5h-% milestone loop):

      - ``current_floor = math.floor(pct + 1e-9)`` — the 1e-9 snap flushes the
        ``0.50 * 100 == 49.99…`` ULP so the N threshold is not missed.
      - ``current_floor < 1`` → empty range (the ``if current_floor >= 1``
        glue guard).
      - ``start_threshold = current_floor`` when ``max_existing is None``
        (first observation records ONLY the current floor — no synthetic
        1..floor-1 backfill), else ``int(max_existing) + 1``.
      - result = ``range(start_threshold, current_floor + 1)`` — empty when
        ``start_threshold > current_floor`` (already at/above the floor),
        which the glue reads as ``if milestone_range:``.

    Pure: the MAX(percent_threshold) probe that yields ``max_existing``, the
    marginal-cost lookup, the per-threshold INSERT, and the alert plumbing all
    stay in glue. (The weekly ``maybe_record_milestone`` loop shares this
    fencing formula but keeps its own range in glue — its crossing decision
    early-returns BEFORE the cost sync when covered, a structure this
    range-only kernel does not encapsulate; spec §6 defers it.)
    """
    current_floor = math.floor(pct + 1e-9)
    if current_floor < 1:
        return range(0)
    start_threshold = current_floor if max_existing is None else int(max_existing) + 1
    return range(start_threshold, current_floor + 1)


def _floor_to_ten_minutes(d: dt.datetime) -> dt.datetime:
    """Floor a datetime to the previous 10-minute boundary.

    Anthropic ``rate_limits.5h.resets_at`` arrives via the status line
    with capture jitter and occasional transient bogus values that
    differ from the real reset by tens of minutes (a brief mid-window
    glitch sitting alongside the genuine reset). A 10-minute floor
    collapses fine-grained jitter into shared buckets while leaving
    truly distinct windows separable; structural conflicts that survive
    the floor are resolved downstream by
    ``_select_non_overlapping_recorded_windows``.
    """
    minute_bucket = _FIVE_HOUR_JITTER_FLOOR_SECONDS // 60
    return d.replace(
        minute=(d.minute // minute_bucket) * minute_bucket,
        second=0, microsecond=0,
    )


def _canonical_5h_window_key(
    resets_at_epoch: int,
    prior_epoch: int | None = None,
    prior_key: int | None = None,
) -> int:
    """Floor a 5h-window resets_at epoch to a jitter-tolerant canonical key.

    Anthropic's status-line API jitters resets_at by ~seconds within the same
    physical 5h window. Any code identifying 'this 5h window' across consecutive
    fetches MUST derive its key via this function. Floor granularity matches
    _floor_to_ten_minutes (the same tolerance already used for weekly_reset_events).

    Required invariant: two ``record-usage`` calls with ``resets_at`` differing
    by ≤ 599 seconds MUST resolve to the same window key. A pure modulo floor
    cannot satisfy this when the two epochs straddle a 600-second bucket
    boundary (e.g. 1746014999 → 1746014400 vs. 1746015000 → 1746015000, a
    1-second delta producing different keys).

    The optional ``prior_epoch`` / ``prior_key`` arguments close that gap: when
    callers can supply the most-recent stored sample's raw ``five_hour_resets_at``
    and its persisted ``five_hour_window_key``, the function reuses ``prior_key``
    whenever ``|resets_at_epoch - prior_epoch| < _FIVE_HOUR_JITTER_FLOOR_SECONDS``
    — boundary-straddling jitter then collapses to the first-seen anchor instead
    of forking a new key. With no anchor (or with the anchor too far away to be
    the same physical window), falls back to the pure floor.
    """
    if (
        prior_epoch is not None
        and prior_key is not None
        and abs(resets_at_epoch - prior_epoch) < _FIVE_HOUR_JITTER_FLOOR_SECONDS
    ):
        return prior_key
    return resets_at_epoch - (resets_at_epoch % _FIVE_HOUR_JITTER_FLOOR_SECONDS)
