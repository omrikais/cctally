"""Pure kernel for the hero-modal milestone-history feature (stdlib only).

Small, dependency-free helpers shared by the Claude + Codex history glue
in ``_cctally_milestone_history.py``:

* ``compute_detail_stamp`` — content digest used both on the SSE index
  entry and the detail response so the client can cache by
  ``(source, key, detail_stamp)`` and revalidate when a week's underlying
  rows move (spec §2 caching/invalidation).
* ``intersects`` — half-open interval intersection on epoch seconds
  (a block appears in every week it intersects, spec §1b dual membership).
* ``coalesce_week_refs`` — collapse same-``key`` ``WeekRef`` segments
  produced by reset correction for an in-place-credited week into ONE
  navigation entry spanning the outer cycle boundary (spec §1a); the
  per-segment structure lives in the detail payload, never as duplicate
  chips.

No imports of the cctally namespace or ``_cctally_core`` — these are pure
functions. ``coalesce_week_refs`` is generic over any frozen dataclass
carrying ``key`` / ``week_start_at`` / ``week_end_at`` string fields
(the ``WeekRef`` shape) via ``dataclasses.replace``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import replace


# Codex ``quota_window_blocks`` rows carry second-level capture jitter in their
# ``resets_at``: one physical reset (weekly *or* 5h) surfaces as several rows
# whose resets differ by a few seconds. This is the Codex-scoped analogue of
# the Claude 5h jitter floor (``_FIVE_HOUR_JITTER_FLOOR_SECONDS``) — same
# concept, different provider. Deliberately NOT ``_canonical_5h_window_key``,
# which is Claude-scoped per spec §1c. 600s (10 min) sits far above the
# observed jitter yet far below any genuine early re-anchor (hours apart) or a
# real weekly reset (~7 days apart), so only jitter is collapsed.
CODEX_CYCLE_JITTER_FLOOR_SECONDS = 600


def cluster_by_reset_jitter(items, *, reset_key, floor_seconds=CODEX_CYCLE_JITTER_FLOOR_SECONDS):
    """Group items that share one physical reset, collapsing capture jitter.

    ``items`` is any iterable; ``reset_key(item)`` returns that item's reset as
    an epoch ``int``/``float``. Items are sorted by reset ascending, and each
    run of consecutive items whose neighbour-to-neighbour reset gap is
    ``<= floor_seconds`` forms one cluster. A gap greater than the floor starts
    a new cluster, so genuine early re-anchors (hours apart) and real weekly
    resets (~7 days apart) stay in distinct clusters — only sub-floor jitter is
    merged.

    Returns a list of clusters (each a non-empty list of the original items),
    ordered by ascending cluster reset; within a cluster items follow the reset
    sort. An empty ``items`` yields ``[]``.
    """
    ordered = sorted(items, key=reset_key)
    clusters: list = []
    current: list = []
    prev = None
    for item in ordered:
        r = reset_key(item)
        if prev is None or (r - prev) <= floor_seconds:
            current.append(item)
        else:
            clusters.append(current)
            current = [item]
        prev = r
    if current:
        clusters.append(current)
    return clusters


def compute_detail_stamp(*parts: object) -> str:
    """Return a 16-hex-char sha256 digest over ``"|"``-joined ``str(p)``.

    ``None`` parts serialize as the empty string so a nullable count/timestamp
    contributes deterministically. Truncating to 16 chars keeps the wire small
    while staying collision-safe for the small per-week input space.
    """
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def intersects(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    """Half-open interval intersection on epoch seconds.

    ``[start_a, end_a)`` overlaps ``[start_b, end_b)`` iff each interval
    starts before the other ends. Used for block/week dual membership so a
    block straddling a week boundary is reported in every week it intersects.
    """
    return start_a < end_b and start_b < end_a


def _parse_epoch(value: "str | None") -> "int | None":
    """Parse a canonical ISO-8601 timestamp to an epoch int, or ``None``.

    Accepts a trailing ``Z`` or an explicit offset; a naive value is treated
    as UTC. Returns ``None`` on empty / unparseable input so callers can skip
    boundary-less refs.
    """
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def coalesce_week_refs(refs: list) -> list:
    """Collapse same-``key`` refs into one ref spanning the outer boundary.

    Input is the ``get_recent_weeks`` ``WeekRef`` list (newest-first). An
    in-place-credited week is represented there as TWO same-``key`` refs
    (a pre-credit segment ``[orig_start, effective)`` and a post-credit
    segment ``[effective, orig_end)``); every other week is a single ref.

    The result preserves the newest-first order of each key's FIRST
    occurrence, and for a multi-ref key emits one ref whose
    ``week_start_at`` is the earliest segment start and ``week_end_at`` the
    latest segment end (the outer cycle boundary). The original ISO strings
    are carried through unchanged (no reformatting drift); comparison is on
    parsed epochs so mixed ``Z``/offset forms order correctly.
    """
    order: list = []  # keys in first-occurrence order
    groups: dict = {}  # key -> list of refs
    for ref in refs:
        k = ref.key
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(ref)

    out: list = []
    for k in order:
        members = groups[k]
        if len(members) == 1:
            out.append(members[0])
            continue
        # Pick earliest start / latest end across the same-key segments.
        earliest = members[0]
        earliest_e = _parse_epoch(members[0].week_start_at)
        latest_end_iso = members[0].week_end_at
        latest_e = _parse_epoch(members[0].week_end_at)
        for m in members[1:]:
            se = _parse_epoch(m.week_start_at)
            if se is not None and (earliest_e is None or se < earliest_e):
                earliest, earliest_e = m, se
            ee = _parse_epoch(m.week_end_at)
            if ee is not None and (latest_e is None or ee > latest_e):
                latest_e, latest_end_iso = ee, m.week_end_at
        # Base on the earliest-start ref (its start_at/start date are the
        # outer start), widen its end to the latest segment end.
        out.append(replace(earliest, week_end_at=latest_end_iso))
    return out
