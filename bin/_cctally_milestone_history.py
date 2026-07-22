"""I/O glue for the hero-modal milestone-history feature.

Assembles the compact per-provider navigation index (rides the SSE
envelope, built only on non-idle snapshot rebuilds) and the on-demand
per-week/-cycle detail payload served by
``GET /api/milestones/<source>/week/<key>``. See
``docs/superpowers/specs/2026-07-22-hero-milestone-history-design.md``
(spec §1a/§1b/§1c) and the implementation plan (Tasks 1–4).

This module is the IMPURE counterpart to the pure kernel
``_lib_milestone_history.py``. Honest imports are restricted to pure
siblings (``_cctally_core`` kernel + ``_lib_*`` pure kernels, none of which
back-import ``cctally``); every impure cctally-namespace symbol
(``get_recent_weeks`` / ``get_milestones_for_week`` /
``_tui_build_five_hour_milestones`` / ``load_config``) is reached via the
call-time ``_cctally()`` accessor so test monkeypatches through the
``cctally`` namespace are preserved.

All Claude 5h keying uses stored ``five_hour_window_key`` values (already
canonical via ``_canonical_5h_window_key`` at write time) — never a new
key shape. Block/week interval comparisons run through SQL ``unixepoch()``
because stored offsets are not uniform (``block_start_at`` may carry a
host-local offset while boundaries are canonical UTC).
"""
from __future__ import annotations

import sqlite3
import sys

from _cctally_core import make_week_ref, parse_iso_datetime
from _cctally_quota import codex_quota_breakdown
from _lib_dashboard_sources import dashboard_resource_key
from _lib_display_tz import _resolve_display_tz_obj, format_display_dt
from _lib_json_envelope import _iso_z
from _lib_quota import QuotaWindowIdentity

import datetime as dt
import json

import _lib_milestone_history as _mh

UTC = dt.timezone.utc


def _cctally():
    """Resolve the current ``cctally`` module at call-time (ns-patchable)."""
    return sys.modules["cctally"]


# ── shared formatting helpers ──────────────────────────────────────────


def _to_iso_z(iso: "str | None") -> "str | None":
    """Canonical UTC-Z form of a stored ISO boundary, or ``None``."""
    if not iso:
        return None
    try:
        return _iso_z(parse_iso_datetime(iso, "milestone_history.boundary"))
    except ValueError:
        return None


def _resolve_display_tz():
    """Resolve the configured display tz (never raises)."""
    c = _cctally()
    try:
        config = c.load_config()
    except Exception:  # noqa: BLE001 — label formatting must never crash the build
        config = {}
    try:
        return _resolve_display_tz_obj(config)
    except Exception:  # noqa: BLE001
        return None


def _week_label(ref, tz) -> str:
    """Week pill label — matches the dashboard header's full-window form
    (``"Apr 13–20"``, en-dash U+2013), reusing ``format_display_dt`` so
    historic pills render identically to the live header."""
    start = ref.week_start_at
    end = ref.week_end_at
    if start and end:
        return (
            f"{format_display_dt(start, tz, fmt='%b %d', suffix=False)}"
            f"–"
            f"{format_display_dt(end, tz, fmt='%b %d', suffix=False)}"
        )
    if start:
        return format_display_dt(start, tz, fmt="%b %d", suffix=False)
    return ref.week_start.strftime("%b %d")


def _count_blocks(conn: sqlite3.Connection, start_iso, end_iso) -> int:
    """Count ``five_hour_blocks`` whose interval intersects ``[start, end)``.

    Half-open intersection on epoch seconds via ``unixepoch()`` (mixed stored
    offsets). Returns 0 when the week lacks canonical boundaries.
    """
    if not start_iso or not end_iso:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM five_hour_blocks "
        "WHERE unixepoch(block_start_at) < unixepoch(?) "
        "  AND unixepoch(five_hour_resets_at) > unixepoch(?)",
        (end_iso, start_iso),
    ).fetchone()
    return int(row[0] or 0)


# ── Claude week index (spec §1a) ───────────────────────────────────────


def _navigable_claude_refs(conn: sqlite3.Connection) -> list:
    """The navigable Claude week refs, newest-first, per spec §1a.

    Navigable set = weeks with ≥1 ``weekly_usage_snapshots`` row ∪ weeks with
    ≥1 ``percent_milestones`` row (cost-only weeks excluded). Same-key credit
    segments are coalesced to one outer-boundary ref; a defensive
    milestone-only week absent from ``get_recent_weeks`` is synthesized from
    its milestone rows' stored boundaries.
    """
    c = _cctally()
    refs = c.get_recent_weeks(conn, None)  # unbounded, newest-first

    usage_keys = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT week_start_date FROM weekly_usage_snapshots"
        ).fetchall()
    }
    milestone_keys = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT week_start_date FROM percent_milestones"
        ).fetchall()
    }
    navigable = usage_keys | milestone_keys

    refs = [r for r in refs if r.key in navigable]
    refs = _mh.coalesce_week_refs(refs)

    present = {r.key for r in refs}
    for k in sorted(milestone_keys - present, reverse=True):
        ref = _synthesize_milestone_only_ref(conn, k)
        if ref is not None:
            refs.append(ref)
    return refs


def _synthesize_milestone_only_ref(conn: sqlite3.Connection, key: str):
    """Defensive ref for a milestone-only week (no usage snapshot).

    Boundaries fall back to the milestone rows' stored
    ``week_start_at``/``week_end_at``/``week_end_date`` (spec §1a). Returns
    ``None`` if a valid ref can't be built.
    """
    row = conn.execute(
        "SELECT week_end_date, week_start_at, week_end_at FROM percent_milestones "
        "WHERE week_start_date = ? "
        "ORDER BY (week_start_at IS NULL), captured_at_utc ASC LIMIT 1",
        (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        return make_week_ref(
            week_start_date=key,
            week_end_date=row["week_end_date"],
            week_start_at=row["week_start_at"],
            week_end_at=row["week_end_at"],
        )
    except (ValueError, TypeError):
        return None


def _current_claude_week_key(conn: sqlite3.Connection) -> "str | None":
    row = conn.execute(
        "SELECT week_start_date FROM weekly_usage_snapshots "
        "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row is not None else None


def _index_entry(conn, ref, current_key, tz) -> dict:
    counts = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT reset_event_id), MAX(captured_at_utc) "
        "FROM percent_milestones WHERE week_start_date = ?",
        (ref.key,),
    ).fetchone()
    milestone_count = int(counts[0] or 0)
    segment_count = int(counts[1] or 0)
    max_captured = counts[2]

    start_z = _to_iso_z(ref.week_start_at)
    end_z = _to_iso_z(ref.week_end_at)
    block_count = _count_blocks(conn, start_z, end_z)

    return {
        "key": ref.key,
        "start_at_utc": start_z,
        "end_at_utc": end_z,
        "label": _week_label(ref, tz),
        "is_current": ref.key == current_key,
        "milestone_count": milestone_count,
        "block_count": block_count,
        "segment_count": segment_count,
        "detail_stamp": _mh.compute_detail_stamp(
            ref.key, milestone_count, block_count, segment_count, max_captured
        ),
    }


def build_claude_week_index(conn: sqlite3.Connection) -> list:
    """Newest-first navigable Claude week index (spec §1a, §3).

    One entry per navigable week; credit-split weeks collapse to a single
    entry (segment structure lives in the detail payload). ``detail_stamp``
    is a content digest that moves when the week's underlying rows change so
    the client cache revalidates.
    """
    refs = _navigable_claude_refs(conn)
    current_key = _current_claude_week_key(conn)
    tz = _resolve_display_tz()
    entries = [_index_entry(conn, ref, current_key, tz) for ref in refs]
    entries.sort(key=lambda e: e["key"], reverse=True)
    return entries


# ── Claude week detail (spec §1b) ──────────────────────────────────────


def _shape_weekly_milestone(row) -> dict:
    """Reshape a ``percent_milestones`` row to the envelope
    ``current_week.milestones`` wire shape (byte-parallel with
    ``_cctally_dashboard_envelope.snapshot_to_envelope``)."""
    marginal = row["marginal_cost_usd"]
    fh = row["five_hour_percent_at_crossing"]
    return {
        "percent": int(row["percent_threshold"]),
        "crossed_at_utc": _to_iso_z(row["captured_at_utc"]),
        "cumulative_usd": round(float(row["cumulative_cost_usd"]), 4),
        "marginal_usd": None if marginal is None else round(float(marginal), 4),
        "five_hour_pct_at_cross": None if fh is None else float(fh),
    }


def _build_segments(rows) -> list:
    """Group milestone rows into segments (by ``reset_event_id``), ordered
    chronologically by each segment's first ``captured_at_utc``; rows within a
    segment ascend by capture time then threshold (spec §1b)."""
    groups: dict = {}
    for r in rows:
        seg = int(r["reset_event_id"] or 0)
        groups.setdefault(seg, []).append(r)
    ordered: list = []
    for seg, seg_rows in groups.items():
        seg_rows.sort(key=lambda r: (r["captured_at_utc"], int(r["percent_threshold"])))
        first_cap = seg_rows[0]["captured_at_utc"]
        ordered.append(
            (first_cap, seg, [_shape_weekly_milestone(r) for r in seg_rows])
        )
    ordered.sort(key=lambda t: (t[0], t[1]))
    return [{"reset_event_id": seg, "milestones": ms} for (_c, seg, ms) in ordered]


def _build_dividers(conn: sqlite3.Connection, segments: list) -> list:
    """Credit dividers between consecutive segments (spec §1b, Q4).

    A divider is emitted for each later segment whose ``reset_event_id``
    resolves to an in-place-credit ``week_reset_events`` row
    (``old_week_end_at == effective_reset_at_utc``). It carries the effective
    time + prior percent (``observed_pre_credit_pct``); no post/delta value is
    recorded for weekly credits.
    """
    dividers: list = []
    for later in segments[1:]:
        seg_id = later["reset_event_id"]
        if not seg_id:
            continue
        ev = conn.execute(
            "SELECT effective_reset_at_utc, observed_pre_credit_pct, old_week_end_at "
            "FROM week_reset_events WHERE id = ?",
            (seg_id,),
        ).fetchone()
        if ev is None:
            continue
        if ev["old_week_end_at"] != ev["effective_reset_at_utc"]:
            continue  # not an in-place credit → no divider
        prior = ev["observed_pre_credit_pct"]
        dividers.append(
            {
                "effective_at_utc": _to_iso_z(ev["effective_reset_at_utc"]),
                "prior_percent": None if prior is None else float(prior),
            }
        )
    return dividers


def _load_block_credits(conn: sqlite3.Connection, window_key: int) -> list:
    """5h in-place credit rows for a block, ascending by effective time —
    same wire shape as the envelope's ``five_hour_block.credits``."""
    rows = conn.execute(
        "SELECT effective_reset_at_utc, prior_percent, post_percent "
        "FROM five_hour_reset_events WHERE five_hour_window_key = ? "
        "ORDER BY effective_reset_at_utc ASC",
        (int(window_key),),
    ).fetchall()
    return [
        {
            "effective_reset_at_utc": r["effective_reset_at_utc"],
            "prior_percent": float(r["prior_percent"]),
            "post_percent": float(r["post_percent"]),
            "delta_pp": round(float(r["post_percent"]) - float(r["prior_percent"]), 1),
        }
        for r in rows
    ]


def _build_blocks(conn: sqlite3.Connection, start_iso, end_iso) -> list:
    """``five_hour_blocks`` intersecting ``[start, end)``, ascending by start;
    each carries its 5h milestone stream + credit rows (spec §1b). A block
    straddling a week boundary appears in every week it intersects."""
    if not start_iso or not end_iso:
        return []
    c = _cctally()
    rows = conn.execute(
        "SELECT five_hour_window_key, block_start_at, five_hour_resets_at, "
        "       final_five_hour_percent, total_cost_usd, "
        "       crossed_seven_day_reset, is_closed "
        "FROM five_hour_blocks "
        "WHERE unixepoch(block_start_at) < unixepoch(?) "
        "  AND unixepoch(five_hour_resets_at) > unixepoch(?) "
        "ORDER BY unixepoch(block_start_at) ASC, five_hour_window_key ASC",
        (end_iso, start_iso),
    ).fetchall()
    out: list = []
    for b in rows:
        wk = int(b["five_hour_window_key"])
        final_pct = b["final_five_hour_percent"]
        cost = b["total_cost_usd"]
        out.append(
            {
                "five_hour_window_key": wk,
                "block_start_at": b["block_start_at"],
                "five_hour_resets_at": b["five_hour_resets_at"],
                "final_five_hour_percent": None if final_pct is None else float(final_pct),
                "total_cost_usd": None if cost is None else float(cost),
                "crossed_seven_day_reset": bool(b["crossed_seven_day_reset"]),
                "is_closed": bool(b["is_closed"]),
                "milestones": c._tui_build_five_hour_milestones(conn, wk),
                "credits": _load_block_credits(conn, wk),
            }
        )
    return out


def build_claude_week_detail(
    conn: sqlite3.Connection, week_start_date: str
) -> "dict | None":
    """Complete payload for one Claude week (spec §1b). ``None`` for an
    unknown/non-navigable key.

    Segments carry ALL ``reset_event_id`` cohorts (both pre- and post-credit)
    ordered chronologically; ``dividers`` sit between consecutive segments;
    ``blocks`` are the intersecting 5h blocks (dual-membership straddlers
    included). Boundary/label/``is_current``/``detail_stamp`` are taken from
    the index entry so the detail and index agree exactly.
    """
    c = _cctally()
    entry = None
    for e in build_claude_week_index(conn):
        if e["key"] == week_start_date:
            entry = e
            break
    if entry is None:
        return None

    rows = c.get_milestones_for_week(conn, week_start_date)
    segments = _build_segments(rows)
    dividers = _build_dividers(conn, segments)
    blocks = _build_blocks(conn, entry["start_at_utc"], entry["end_at_utc"])

    return {
        "source": "claude",
        "key": entry["key"],
        "start_at_utc": entry["start_at_utc"],
        "end_at_utc": entry["end_at_utc"],
        "label": entry["label"],
        "is_current": entry["is_current"],
        "detail_stamp": entry["detail_stamp"],
        "segments": segments,
        "dividers": dividers,
        "blocks": blocks,
    }


# ── Codex durable-projection cycle index + detail (spec §1c) ───────────
#
# The index is a DEDICATED UNBOUNDED query over the durable projection
# (quota_window_blocks, stats.db) — explicitly NOT the bounded public quota
# read model (35-day / 1000-observation / 250-row caps). Boundaries are the
# effective clipped, non-overlapping periods: a provider early re-anchor
# ends the prior cycle at min(raw reset, next nominal start). Cycle keys
# embed the full native identity tuple so two identities sharing a
# resets_at get distinct keys and resolve exactly.
#
# `_codex_weekly_periods` (bin/_cctally_dashboard_sources.py) computes the
# same clip formula but collapses across roots (losing the per-identity key)
# and caps at 250 rows, so it cannot back the per-identity unbounded index;
# the clip FORMULA (`end = min(reset, next nominal start)`) is applied here
# per-identity — the spec-faithful realization for the disambiguated index.


def _is_model_scoped_codex_quota(logical_limit_key) -> bool:
    """Whether an interpreted native identity is a per-model pool (outside the
    account-level standard quota). Mirrors the source builder's check."""
    if not isinstance(logical_limit_key, str):
        return False
    try:
        payload = json.loads(logical_limit_key)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("modelPool"), str)
        and bool(payload["modelPool"].strip())
    )


def _parse_utc(value) -> "dt.datetime | None":
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class _CodexCycle:
    """Parsed durable 7-day cycle with its effective clipped boundary.

    After jitter-canonicalization (``_canonicalize_codex_cluster``) a cycle is
    a *cluster representative*: its ``reset`` is the cluster's max (latest)
    observation, ``start`` the cluster's min start, and ``members`` the raw
    per-observation ``_CodexCycle`` rows the cluster collapsed (used to union
    milestones/counts across every jittered reset). ``members`` is ``None`` for
    a not-yet-clustered per-row cycle (equivalent to a single-member cluster).
    """

    __slots__ = (
        "root", "limit", "slot", "window", "limit_id", "limit_name",
        "start", "reset", "end", "current_percent", "members",
    )

    def __init__(self, row):
        self.root = row["source_root_key"]
        self.limit = row["logical_limit_key"]
        self.slot = row["observed_slot"]
        self.window = int(row["window_minutes"])
        self.limit_id = row["limit_id"]
        self.limit_name = row["limit_name"]
        self.start = _parse_utc(row["nominal_start_at_utc"])
        self.reset = _parse_utc(row["resets_at_utc"])
        self.end = self.reset
        cp = row["current_percent"]
        self.current_percent = (
            None if cp is None or isinstance(cp, bool) else float(cp)
        )
        self.members = None

    @property
    def cluster_members(self) -> list:
        """The raw member cycles this representative collapsed (``[self]`` when
        unclustered) — the resets to union milestones/counts over."""
        return self.members if self.members else [self]

    @property
    def member_reset_isos(self) -> list:
        """Canonical ISO resets of every cluster member (jitter siblings)."""
        return [m.reset_iso for m in self.cluster_members]

    @property
    def reset_iso(self) -> str:
        return self.reset.astimezone(UTC).isoformat()

    @property
    def key_parts(self) -> tuple:
        return (self.root, self.limit, self.slot, self.window, self.reset_iso)

    @property
    def key(self) -> str:
        return dashboard_resource_key("milestone_cycle", "codex", *self.key_parts)

    @property
    def block_key(self) -> str:
        return dashboard_resource_key("block", "codex", *self.key_parts)


def _load_codex_cycles(stats_conn, root_keys, *, include_orphaned=False) -> list:
    """Non-model-scoped 7-day (window_minutes=10080) cycles for the identity
    roots, effective-clipped per identity. Unbounded (no 35-day / row cap)."""
    roots = tuple(sorted({r for r in root_keys if isinstance(r, str) and r}))
    if not roots:
        return []
    placeholders = ",".join("?" for _ in roots)
    orphan_clause = "" if include_orphaned else "AND orphaned_at IS NULL "
    rows = stats_conn.execute(
        "SELECT source_root_key, logical_limit_key, observed_slot, "
        "       window_minutes, limit_id, limit_name, resets_at_utc, "
        "       nominal_start_at_utc, current_percent "
        "FROM quota_window_blocks "
        "WHERE source='codex' AND window_minutes=10080 "
        f"{orphan_clause}"
        f"AND source_root_key IN ({placeholders}) "
        "ORDER BY unixepoch(resets_at_utc) DESC",
        (*roots,),
    ).fetchall()

    cycles: list = []
    for row in rows:
        if _is_model_scoped_codex_quota(row["logical_limit_key"]):
            continue
        cyc = _CodexCycle(row)
        if cyc.start is None or cyc.reset is None or cyc.reset <= cyc.start:
            continue
        cycles.append(cyc)

    # Canonicalize per identity (root, limit, slot): jitter-cluster the raw
    # observations so one physical weekly reset — surfacing as several rows
    # whose ``resets_at`` differ by seconds — becomes ONE cycle instead of a
    # fan of degenerate 1-second spans (ui-qa P2). Genuine early re-anchors
    # (hours apart) stay distinct (see ``cluster_by_reset_jitter``).
    groups: dict = {}
    for cyc in cycles:
        groups.setdefault((cyc.root, cyc.limit, cyc.slot), []).append(cyc)
    clustered: list = []
    for members in groups.values():
        for cluster in _mh.cluster_by_reset_jitter(
            members, reset_key=lambda c: c.reset.timestamp()
        ):
            clustered.append(_canonicalize_codex_cluster(cluster))

    # Effective clip per identity: end each canonical cycle at the NEXT
    # canonical cycle's start (min with its own reset), non-overlapping.
    cgroups: dict = {}
    for cyc in clustered:
        cgroups.setdefault((cyc.root, cyc.limit, cyc.slot), []).append(cyc)
    for members in cgroups.values():
        members.sort(key=lambda c: c.start)
        for i, cyc in enumerate(members):
            nxt = members[i + 1].start if i + 1 < len(members) else None
            cyc.end = min(cyc.reset, nxt) if nxt is not None else cyc.reset
    clustered = [c for c in clustered if c.end > c.start]
    clustered.sort(key=lambda c: c.reset, reverse=True)
    return clustered


def _canonicalize_codex_cluster(members: list) -> "_CodexCycle":
    """Collapse jitter-sibling cycles into one canonical representative.

    Canonical ``reset`` = the cluster's max (latest observation wins);
    ``start`` = the cluster's min start; scalar identity fields (limit_id,
    limit_name, current_percent, window) come from the latest member. The raw
    members are retained on ``.members`` so milestone rows/counts union across
    every member reset (each via the existing per-reset breakdown path).
    """
    rep = max(members, key=lambda c: c.reset)
    rep.start = min(m.start for m in members)
    rep.reset = max(m.reset for m in members)
    rep.end = rep.reset
    rep.members = list(members)
    return rep


def _codex_is_current(cyc, identity, now_utc) -> bool:
    identity_reset = getattr(identity, "resets_at", None)
    if identity_reset is not None:
        try:
            target = identity_reset.astimezone(UTC)
        except (AttributeError, ValueError):
            target = None
        if target is not None:
            # The live boundary's reset (``select_baseline``) need not be the
            # cluster's max jittered reset, so match against ANY member within
            # the jitter floor — distinct clusters are > floor apart, so only
            # the live cluster can match.
            return any(
                abs((m.reset - target).total_seconds())
                <= _mh.CODEX_CYCLE_JITTER_FLOOR_SECONDS
                for m in cyc.cluster_members
            )
    return cyc.reset > now_utc


def _codex_milestone_count(stats_conn, cyc) -> tuple[int, str | None]:
    """Milestone count + max captured-at over the cluster's union — every
    jittered member reset contributes (rows split across sibling resets)."""
    resets = cyc.member_reset_isos
    placeholders = ",".join("unixepoch(?)" for _ in resets)
    row = stats_conn.execute(
        "SELECT COUNT(*), MAX(captured_at_utc) FROM quota_percent_milestones "
        "WHERE source='codex' AND source_root_key=? AND logical_limit_key=? "
        "  AND observed_slot=? AND window_minutes=? "
        f"  AND unixepoch(resets_at_utc) IN ({placeholders}) AND orphaned_at IS NULL",
        (cyc.root, cyc.limit, cyc.slot, cyc.window, *resets),
    ).fetchone()
    return int(row[0] or 0), row[1]


def _codex_five_hour_rows(stats_conn, cyc, *, include_orphaned=False) -> list:
    """Retained 5h (window_minutes=300) blocks for this cycle's identity
    (root + observed_slot + limit_id) intersecting [start, end)."""
    orphan_clause = "" if include_orphaned else "AND orphaned_at IS NULL "
    return stats_conn.execute(
        "SELECT source_root_key, logical_limit_key, observed_slot, "
        "       window_minutes, limit_id, limit_name, resets_at_utc, "
        "       nominal_start_at_utc, current_percent "
        "FROM quota_window_blocks "
        "WHERE source='codex' AND window_minutes=300 "
        f"{orphan_clause}"
        "AND source_root_key=? AND observed_slot=? AND limit_id IS ? "
        "AND unixepoch(nominal_start_at_utc) < unixepoch(?) "
        "AND unixepoch(resets_at_utc) > unixepoch(?) "
        "ORDER BY unixepoch(nominal_start_at_utc) ASC",
        (cyc.root, cyc.slot, cyc.limit_id,
         cyc.end.astimezone(UTC).isoformat(), cyc.start.astimezone(UTC).isoformat()),
    ).fetchall()


def _codex_five_hour_clusters(stats_conn, cyc, *, include_orphaned=False) -> list:
    """Retained 5h blocks intersecting the cycle, jitter-canonicalized.

    The 300-minute rows carry the same second-level ``resets_at`` jitter as the
    weekly rows (one physical 5h block surfaces as many rows), so they are
    clustered the same way — one canonical block per physical reset — before
    counting/rendering. Returns cluster-representative ``_CodexCycle`` blocks
    (``.members`` set), ordered by ascending start.
    """
    blocks: list = []
    for row in _codex_five_hour_rows(stats_conn, cyc, include_orphaned=include_orphaned):
        block = _CodexCycle(row)
        if block.start is None or block.reset is None:
            continue
        blocks.append(block)
    clusters = [
        _canonicalize_codex_cluster(members)
        for members in _mh.cluster_by_reset_jitter(
            blocks, reset_key=lambda b: b.reset.timestamp()
        )
    ]
    clusters.sort(key=lambda b: b.start)
    return clusters


def _codex_cycle_entry(stats_conn, cyc, identity, now_utc, tz) -> dict:
    milestone_count, max_captured = _codex_milestone_count(stats_conn, cyc)
    block_count = len(_codex_five_hour_clusters(stats_conn, cyc))
    key = cyc.key
    return {
        "key": key,
        "start_at_utc": _iso_z(cyc.start),
        "end_at_utc": _iso_z(cyc.end),
        "resets_at_utc": _iso_z(cyc.reset),
        "label": _codex_cycle_label(cyc, tz),
        "is_current": _codex_is_current(cyc, identity, now_utc),
        "milestone_count": milestone_count,
        "block_count": block_count,
        "detail_stamp": _mh.compute_detail_stamp(
            key, milestone_count, block_count, max_captured
        ),
    }


def _codex_cycle_label(cyc, tz) -> str:
    start_iso = cyc.start.astimezone(UTC).isoformat()
    end_iso = cyc.end.astimezone(UTC).isoformat()
    return (
        f"{format_display_dt(start_iso, tz, fmt='%b %d', suffix=False)}"
        f"–"
        f"{format_display_dt(end_iso, tz, fmt='%b %d', suffix=False)}"
    )


def build_codex_cycle_index(stats_conn, *, identity, now_utc) -> list:
    """Newest-first Codex cycle index over the durable projection (spec §1c,
    §3). Enumerates cycles of the hero-selected identity's roots with no depth
    cap; one entry per (root, limit, slot, reset) cycle."""
    now = now_utc.astimezone(UTC) if now_utc.tzinfo else now_utc.replace(tzinfo=UTC)
    tz = _resolve_display_tz()
    cycles = _load_codex_cycles(stats_conn, getattr(identity, "source_root_keys", ()))
    return [_codex_cycle_entry(stats_conn, c, identity, now, tz) for c in cycles]


def _shape_codex_milestone(row, *, key_parts, block_key) -> dict:
    captured_iso = row.captured_at.astimezone(UTC).isoformat()
    root, limit, slot, window, reset_iso = key_parts
    return {
        "key": dashboard_resource_key(
            "quota_milestone", "codex", *key_parts, row.percent, captured_iso
        ),
        "source": "codex",
        "block_key": block_key,
        "window_minutes": window,
        "resets_at": _iso_z(_parse_utc(reset_iso)),
        "percent": int(row.percent),
        "captured_at": _iso_z(row.captured_at),
        "cumulative_usd": row.cost_usd,
        "marginal_usd": row.marginal_cost_usd,
        "input_tokens": row.input_tokens,
        "cached_input_tokens": row.cached_input_tokens,
        "output_tokens": row.output_tokens,
        "reasoning_output_tokens": row.reasoning_output_tokens,
        "total_tokens": row.total_tokens,
        "five_hour_percent": None,
    }


def _codex_breakdown_rows(ident, reset, speed, cache_conn, stats_conn):
    try:
        return codex_quota_breakdown(
            ident, reset, speed=speed, cache_conn=cache_conn, stats_conn=stats_conn,
        )
    except sqlite3.Error:
        return None


def _union_cluster_milestones(cluster, block_key, speed, cache_conn, stats_conn):
    """Union ``codex_quota_breakdown`` milestone rows across every cluster
    member (each jittered sibling reset via the existing per-reset path).

    One physical reset can split its crossings across sibling resets, so the
    full ledger is the union — shaped and ordered by ``(captured_at, percent)``.
    Returns ``None`` if any member's projection is incoherent (mirrors the
    single-reset signal so the caller can surface ``projection_incoherent``).
    """
    shaped: list = []
    for member in cluster.cluster_members:
        ident = QuotaWindowIdentity(
            source="codex", source_root_key=member.root,
            logical_limit_key=member.limit, observed_slot=member.slot,
            window_minutes=member.window, limit_id=member.limit_id,
            limit_name=member.limit_name,
        )
        breakdown = _codex_breakdown_rows(
            ident, member.reset, speed, cache_conn, stats_conn
        )
        if breakdown is None:
            return None  # projection-incoherent signal to the caller
        shaped.extend(
            _shape_codex_milestone(r, key_parts=member.key_parts, block_key=block_key)
            for r in breakdown
        )
    shaped.sort(key=lambda m: (m["captured_at"] or "", m["percent"]))
    return shaped


def _codex_cycle_blocks(stats_conn, cache_conn, cyc, speed) -> "list | None":
    out: list = []
    for block in _codex_five_hour_clusters(stats_conn, cyc):
        block_key = block.block_key
        milestones = _union_cluster_milestones(
            block, block_key, speed, cache_conn, stats_conn
        )
        if milestones is None:
            return None  # projection-incoherent signal to the caller
        out.append(
            {
                "key": block_key,
                "block_start_at": _iso_z(block.start),
                "five_hour_resets_at": _iso_z(block.reset),
                "final_five_hour_percent": block.current_percent,
                "total_cost_usd": None,
                "crossed_seven_day_reset": False,
                "is_closed": block.reset <= cyc.reset and block.reset <= _now_guard(cyc),
                "milestones": milestones,
                "credits": [],
            }
        )
    return out


def _now_guard(cyc):
    # A 5h block is closed once its reset has passed the cycle's effective end
    # (historic cycle) — a conservative closed flag for retained blocks.
    return cyc.end


def build_codex_cycle_detail(
    stats_conn, cache_conn, *, identity, key, speed, now_utc
):
    """Complete payload for one Codex cycle (spec §1c). On success returns a
    dict mirroring the Claude detail shape (segments = a single Codex segment,
    dividers = []). On failure returns ``(None, reason)`` where reason ∈
    {pruned, rebuild_pending, projection_incoherent, unknown} — Task 4 maps it
    into the 404 body.
    """
    now = now_utc.astimezone(UTC) if now_utc.tzinfo else now_utc.replace(tzinfo=UTC)
    tz = _resolve_display_tz()
    try:
        cycles = _load_codex_cycles(
            stats_conn, getattr(identity, "source_root_keys", ())
        )
    except sqlite3.Error:
        return (None, "projection_incoherent")

    match = next((c for c in cycles if c.key == key), None)
    if match is None:
        # Distinguish a pruned/orphaned cycle from a genuinely unknown key.
        try:
            orphaned = _load_codex_cycles(
                stats_conn, getattr(identity, "source_root_keys", ()),
                include_orphaned=True,
            )
        except sqlite3.Error:
            return (None, "projection_incoherent")
        if any(c.key == key for c in orphaned):
            return (None, "pruned")
        return (None, "unknown")

    milestones = _union_cluster_milestones(
        match, match.block_key, speed, cache_conn, stats_conn
    )
    if milestones is None:
        return (None, "projection_incoherent")
    blocks = _codex_cycle_blocks(stats_conn, cache_conn, match, speed)
    if blocks is None:
        return (None, "projection_incoherent")

    entry = _codex_cycle_entry(stats_conn, match, identity, now, tz)
    return {
        "source": "codex",
        "key": entry["key"],
        "start_at_utc": entry["start_at_utc"],
        "end_at_utc": entry["end_at_utc"],
        "resets_at_utc": entry["resets_at_utc"],
        "label": entry["label"],
        "is_current": entry["is_current"],
        "detail_stamp": entry["detail_stamp"],
        "segments": [{"reset_event_id": 0, "milestones": milestones}],
        "dividers": [],
        "blocks": blocks,
    }
