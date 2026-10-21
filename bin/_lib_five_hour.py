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

`latest_five_hour_reading` is the one read-only selection of an account's
newest five-hour reading (#825/#832 spec 4.7 revision 7): it takes the
caller's connection and runs one bounded statement, so the module stays free
of I/O at import time and of sibling imports.

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
from dataclasses import dataclass


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


def _round_to_ten_minutes(d: dt.datetime) -> dt.datetime:
    """Round a datetime to the NEAREST 10-minute boundary (half up).

    The *display*-side companion to ``_floor_to_ten_minutes``. Anthropic
    ``rate_limits.5h.resets_at`` (and the derived ``block_start_at``,
    ``block_start = reset - 5h``) carries sub-10-minute capture jitter, so
    a reset that truly lands on ``:40`` can be recorded as ``:39``.
    Flooring that for display shows ``:30`` — off by a full bucket — so
    every user-facing 5h-block clock time rounds to the nearest boundary
    instead.

    Rounds the ABSOLUTE instant (epoch, tz-independent) rather than the
    local minute, so it is correct regardless of the display zone's offset
    (the reset is a fixed UTC instant). Returns a UTC-aware datetime;
    callers hand it to ``format_display_dt`` for zone conversion.

    NEVER use this for keys / partitioning / lookups — those require the
    exact stored timestamp (issue #76). This is a render-only normalizer.
    """
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    bucket = _FIVE_HOUR_JITTER_FLOOR_SECONDS  # 600s = 10 min
    # floor(x + 0.5) is predictable half-UP (Python's round() is banker's).
    rounded = math.floor(d.timestamp() / bucket + 0.5) * bucket
    return dt.datetime.fromtimestamp(rounded, dt.timezone.utc)


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


@dataclass(frozen=True)
class FiveHourReading:
    """One five-hour reading, every field taken from ONE snapshot row.

    ``resets_at`` is the row's stored ``five_hour_resets_at`` text (``None`` on
    a hand-built or legacy row that stores only the canonical key) and
    ``window_key`` its canonical window: the stored key, or the
    ``_canonical_5h_window_key`` floor of the stored reset for a row that
    predates the key. ``snapshot_id`` / ``captured_at_utc`` / ``account_key`` identify the
    row, and ``weekly_observation_held`` says whether its WEEKLY axis was
    carried forward — its five-hour fields are the writing tick's own either
    way.
    """

    percent: float
    resets_at: "str | None"
    window_key: int
    captured_at_utc: str
    snapshot_id: int
    account_key: str
    weekly_observation_held: bool


def latest_five_hour_reading(
    conn,
    *,
    account_key: "str | None" = None,
    as_of_utc: "str | None" = None,
    captured_at_or_after_utc: "str | None" = None,
    week_start_date: "str | None" = None,
    week_start_at_in: "tuple[str, ...] | list[str] | None" = None,
) -> "FiveHourReading | None":
    """The newest ``weekly_usage_snapshots`` row carrying a five-hour reading
    (#825/#832 spec 4.7 revision 7 *Latest five-hour reading*).

    The ONE shared, read-only selection every reader of the newest five-hour
    value uses. A five-hour credit retires a stale replica in place (its three
    five-hour columns NULL, its weekly evidence kept), and an ordinary
    weekly-only reading has the same shape, so either can be the newest row
    while saying nothing about the five-hour axis. Eligible rows carry a
    finite, non-negative percent and a resolvable canonical window (the stored
    ``five_hour_window_key``, else ``_canonical_5h_window_key`` of a parseable
    ``five_hour_resets_at``; the recorder always writes all three together),
    held rows included (a held row's five-hour fields are the writing tick's
    own). Zero is a valid percent. Weekly evidence is NOT selected here: callers select it
    independently, with their own freshness and held-row rules.

    Every restriction is the CALLER's, applied unchanged:

    - ``account_key``: ``None`` adds no account predicate (the caller's own
      account-blind contract); a key, including ``unattributed``, scopes the
      selection to that account, so one account never borrows another's
      reading.
    - ``as_of_utc`` / ``captured_at_or_after_utc``: an inclusive capture
      window, compared as instants.
    - ``week_start_date`` alone: one week date key. With ``week_start_at_in``:
      the week whose ``week_start_at`` is one of those texts, or a
      ``week_start_at``-less row of that date (the current-week reader's
      scope).

    The selection is one ``LIMIT 1`` statement, so no history-sized sort is
    materialized. ``None`` means no eligible row.
    """
    clauses = [
        "five_hour_percent IS NOT NULL",
        "five_hour_percent >= 0",
        "five_hour_percent <= 1e308",
        "(five_hour_window_key IS NOT NULL "
        " OR unixepoch(five_hour_resets_at) IS NOT NULL)",
    ]
    params: list = []
    if account_key is not None:
        clauses.append("account_key = ?")
        params.append(account_key)
    if as_of_utc is not None:
        clauses.append("unixepoch(captured_at_utc) <= unixepoch(?)")
        params.append(as_of_utc)
    if captured_at_or_after_utc is not None:
        clauses.append("unixepoch(captured_at_utc) >= unixepoch(?)")
        params.append(captured_at_or_after_utc)
    if week_start_at_in is not None:
        texts = tuple(week_start_at_in)
        if not texts:
            clauses.append("(week_start_at IS NULL AND week_start_date = ?)")
        else:
            clauses.append(
                "(week_start_at IN (" + ",".join("?" * len(texts)) + ") "
                "OR (week_start_at IS NULL AND week_start_date = ?))")
            params.extend(texts)
        params.append(week_start_date)
    elif week_start_date is not None:
        clauses.append("week_start_date = ?")
        params.append(week_start_date)
    row = conn.execute(
        "SELECT id, account_key, captured_at_utc, five_hour_percent, "
        "       five_hour_resets_at, five_hour_window_key, "
        "       unixepoch(five_hour_resets_at), weekly_observation_held "
        "FROM weekly_usage_snapshots WHERE " + " AND ".join(clauses) +
        " ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    if row is None:
        return None
    percent = float(row[3])
    if not math.isfinite(percent) or percent < 0:
        return None
    window_key = (
        int(row[5]) if row[5] is not None
        else _canonical_5h_window_key(int(row[6])))
    return FiveHourReading(
        percent=percent,
        resets_at=row[4],
        window_key=window_key,
        captured_at_utc=row[2],
        snapshot_id=int(row[0]),
        account_key=row[1],
        weekly_observation_held=bool(row[7]),
    )
