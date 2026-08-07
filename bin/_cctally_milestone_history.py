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
(``get_recent_weeks`` / ``_tui_build_five_hour_milestones`` / ``load_config``)
is reached via the
call-time ``_cctally()`` accessor so test monkeypatches through the
``cctally`` namespace are preserved.

All Claude 5h keying uses stored ``five_hour_window_key`` values (already
canonical via ``_canonical_5h_window_key`` at write time) — never a new
key shape. Block/week interval comparisons run through SQL ``unixepoch()``
because stored offsets are not uniform (``block_start_at`` may carry a
host-local offset while boundaries are canonical UTC).
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from dataclasses import replace

from _cctally_core import make_week_ref, parse_iso_datetime
from _cctally_quota import assert_projection_readable, codex_quota_breakdown
from _lib_accounts import UNATTRIBUTED
from _lib_codex_pools import is_model_scoped_codex_quota
from _lib_dashboard_sources import dashboard_resource_key
from _lib_display_tz import _resolve_display_tz_obj, format_display_dt
from _lib_json_envelope import _iso_z
from _lib_quota import QuotaWindowIdentity

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
    reset-defined segment remains independently navigable; a defensive
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
    present = {r.key for r in refs}
    for k in sorted(milestone_keys - present, reverse=True):
        ref = _synthesize_milestone_only_ref(conn, k)
        if ref is not None:
            refs.append(ref)
    return _derive_claude_reset_cycles(conn, refs)


def _parse_optional_iso(value):
    if not value:
        return None
    try:
        return parse_iso_datetime(value, "milestone_cycle.boundary")
    except (TypeError, ValueError):
        return None


def _claude_storage_boundaries(conn: sqlite3.Connection, key: str):
    """Return the outer retained boundary for one storage date bucket."""
    rows = conn.execute(
        "SELECT week_start_at, week_end_at FROM weekly_usage_snapshots "
        "WHERE week_start_date=? AND week_start_at IS NOT NULL "
        "AND week_end_at IS NOT NULL "
        "UNION ALL "
        "SELECT week_start_at, week_end_at FROM percent_milestones "
        "WHERE week_start_date=? AND week_start_at IS NOT NULL "
        "AND week_end_at IS NOT NULL",
        (key, key),
    ).fetchall()
    pairs = [
        (start, end)
        for row in rows
        if (start := _parse_optional_iso(row[0])) is not None
        and (end := _parse_optional_iso(row[1])) is not None
        and end > start
    ]
    if not pairs:
        return None
    return min(start for start, _end in pairs), max(end for _start, end in pairs)


def _linked_reset_events(conn: sqlite3.Connection, key: str) -> list:
    """Reset rows linked to any retained boundary in a storage bucket."""
    return conn.execute(
        "SELECT DISTINCT e.id, e.effective_reset_at_utc "
        "FROM week_reset_events e "
        "WHERE EXISTS ("
        "  SELECT 1 FROM weekly_usage_snapshots s "
        "  WHERE s.week_start_date=? AND ("
        "    unixepoch(s.week_end_at)=unixepoch(e.old_week_end_at) OR "
        "    unixepoch(s.week_end_at)=unixepoch(e.new_week_end_at)"
        "  )"
        ") OR EXISTS ("
        "  SELECT 1 FROM percent_milestones m "
        "  WHERE m.week_start_date=? AND ("
        "    unixepoch(m.week_end_at)=unixepoch(e.old_week_end_at) OR "
        "    unixepoch(m.week_end_at)=unixepoch(e.new_week_end_at)"
        "  )"
        ") "
        "ORDER BY unixepoch(e.effective_reset_at_utc), e.id",
        (key, key),
    ).fetchall()


def _derive_claude_reset_cycles(conn: sqlite3.Connection, refs: list) -> list:
    """Expand storage buckets into provider reset-defined cycles.

    ``get_recent_weeks`` can split an in-place credit, but an early re-anchor
    whose old and new boundaries both remain under one ``week_start_date``
    only exposes the pre-reset half. The retained snapshot boundary set plus
    ``week_reset_events`` is the authoritative reset ledger for both shapes.
    """
    out: list = []
    by_key: dict = {}
    for ref in refs:
        by_key.setdefault(ref.key, ref)
    for key, template in by_key.items():
        outer = _claude_storage_boundaries(conn, key)
        if outer is None:
            out.append(template)
            continue
        start, end = outer
        cuts = []
        for event in _linked_reset_events(conn, key):
            effective = _parse_optional_iso(event["effective_reset_at_utc"])
            if effective is not None and start < effective < end:
                cuts.append(effective)
        boundaries = [start, *sorted(set(cuts)), end]
        cycles = []
        for cycle_start, cycle_end in zip(boundaries, boundaries[1:]):
            cycles.append(replace(
                template,
                week_start_at=cycle_start.isoformat(timespec="seconds"),
                week_end_at=cycle_end.isoformat(timespec="seconds"),
                week_end=(cycle_end - dt.timedelta(seconds=1)).date(),
            ))
        out.extend(reversed(cycles))
    return _cctally()._apply_overlap_clamp_to_weekrefs(out)


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


def _claude_cycle_key(ref) -> str:
    return dashboard_resource_key(
        "milestone_cycle", "claude", ref.key,
        ref.week_start_at or "", ref.week_end_at or "",
    )


def _claude_cycle_rows(conn: sqlite3.Connection, ref) -> list:
    cohort_id = 0
    if ref.week_start_at:
        row = conn.execute(
            "SELECT e.id FROM week_reset_events e "
            "WHERE unixepoch(e.effective_reset_at_utc)=unixepoch(?) "
            "AND (EXISTS ("
            "  SELECT 1 FROM weekly_usage_snapshots s "
            "  WHERE s.week_start_date=? AND ("
            "    unixepoch(s.week_end_at)=unixepoch(e.old_week_end_at) OR "
            "    unixepoch(s.week_end_at)=unixepoch(e.new_week_end_at)"
            "  )"
            ") OR EXISTS ("
            "  SELECT 1 FROM percent_milestones m "
            "  WHERE m.week_start_date=? AND ("
            "    unixepoch(m.week_end_at)=unixepoch(e.old_week_end_at) OR "
            "    unixepoch(m.week_end_at)=unixepoch(e.new_week_end_at)"
            "  )"
            ")) ORDER BY e.id DESC LIMIT 1",
            (ref.week_start_at, ref.key, ref.key),
        ).fetchone()
        if row is not None:
            cohort_id = int(row[0])
    return conn.execute(
        "SELECT * FROM percent_milestones WHERE week_start_date=? "
        "AND reset_event_id=? "
        "ORDER BY unixepoch(captured_at_utc) ASC, percent_threshold ASC",
        (ref.key, cohort_id),
    ).fetchall()


def _index_entry(conn, ref, current_cycle_key, tz) -> dict:
    rows = _claude_cycle_rows(conn, ref)
    milestone_count = len(rows)
    segment_count = 1 if rows else 0
    max_captured = max((r["captured_at_utc"] for r in rows), default=None)
    start_z = _to_iso_z(ref.week_start_at)
    end_z = _to_iso_z(ref.week_end_at)
    block_count = _count_blocks(conn, start_z, end_z)
    key = _claude_cycle_key(ref)

    return {
        "key": key,
        "start_at_utc": start_z,
        "end_at_utc": end_z,
        "label": _week_label(ref, tz),
        "is_current": key == current_cycle_key,
        "milestone_count": milestone_count,
        "block_count": block_count,
        "segment_count": segment_count,
        "detail_stamp": _mh.compute_detail_stamp(
            key, milestone_count, block_count, segment_count, max_captured
        ),
    }


def build_claude_week_index(conn: sqlite3.Connection) -> list:
    """Newest-first navigable Claude week index (spec §1a, §3).

    One entry per effective reset-defined cycle. Multiple cycles may share one
    storage ``week_start_date`` but always have distinct opaque keys.
    """
    refs = _navigable_claude_refs(conn)
    current_key = _current_claude_week_key(conn)
    current_refs = [r for r in refs if r.key == current_key]
    current_ref = max(
        current_refs,
        key=lambda r: r.week_start_at or "",
        default=None,
    )
    current_cycle_key = _claude_cycle_key(current_ref) if current_ref else None
    tz = _resolve_display_tz()
    entries = [_index_entry(conn, ref, current_cycle_key, tz) for ref in refs]
    entries.sort(key=lambda e: e["start_at_utc"] or "", reverse=True)
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


def build_claude_week_detail(conn: sqlite3.Connection, key: str) -> "dict | None":
    """Complete payload for one reset-defined Claude cycle."""
    refs = _navigable_claude_refs(conn)
    ref = next((candidate for candidate in refs if _claude_cycle_key(candidate) == key), None)
    if ref is None:
        return None
    entry = next(e for e in build_claude_week_index(conn) if e["key"] == key)
    rows = _claude_cycle_rows(conn, ref)
    segment_key = dashboard_resource_key(
        "milestone_segment", "claude", ref.key,
        ref.week_start_at or "", ref.week_end_at or "",
    )
    segments = ([{"key": segment_key,
                  "milestones": [_shape_weekly_milestone(r) for r in rows]}]
                if rows else [])
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
        "dividers": [],
        "blocks": blocks,
    }


# ── Codex durable-projection cycle index + detail (spec §1c) ───────────
#
# The index is a DEDICATED UNBOUNDED query over the durable projection
# (quota_window_blocks, stats.db) — explicitly NOT the bounded public quota
# read model (35-day / 1000-observation / 250-row caps). Boundaries are the
# effective clipped, non-overlapping periods: a provider early re-anchor
# ends the prior cycle at min(raw reset, next nominal start). One full native
# identity is selected per physical reset; the opaque cycle key retains that
# identity so detail resolves the exact selected ledger.
#
# `_codex_weekly_periods` (bin/_cctally_dashboard_sources.py) computes the
# same clip formula but collapses across roots (losing the per-identity key)
# and caps at 250 rows, so it cannot back the per-identity unbounded index;
# the clip FORMULA (`end = min(reset, next nominal start)`) is applied here
# per-identity — the spec-faithful realization for the disambiguated index.


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
        "account_key", "start", "reset", "end", "current_percent", "members",
        "is_current",
    )

    def __init__(self, row):
        self.root = row["source_root_key"]
        self.limit = row["logical_limit_key"]
        self.slot = row["observed_slot"]
        self.window = int(row["window_minutes"])
        # #373 root cause 3: `account_key` participates in QuotaWindowIdentity
        # equality by #341 design, so every identity this class is used to
        # reconstruct — and every query against the account-stamped projection
        # tables reached from it — must carry it.
        self.account_key = row["account_key"]
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
        self.is_current = False

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
    def identity_parts(self) -> tuple:
        return (self.root, self.limit, self.slot, self.window)

    @property
    def key(self) -> str:
        return dashboard_resource_key("milestone_cycle", "codex", *self.key_parts)

    @property
    def block_key(self) -> str:
        return dashboard_resource_key("block", "codex", *self.key_parts)


def _identity_matches_cycle(identity, cyc) -> bool:
    return (
        identity is not None
        and getattr(identity, "source_root_key", None) == cyc.root
        and getattr(identity, "logical_limit_key", None) == cyc.limit
        and getattr(identity, "observed_slot", None) == cyc.slot
        and getattr(identity, "window_minutes", None) == cyc.window
    )


def _choose_physical_cycle(candidates: list, *, preferred_identity=None) -> _CodexCycle:
    preferred = [c for c in candidates if _identity_matches_cycle(preferred_identity, c)]
    pool = preferred or candidates
    return max(
        pool,
        key=lambda c: (
            -1.0 if c.current_percent is None else c.current_percent,
            c.reset,
            c.identity_parts,
        ),
    )


def _canonical_identity_cycles(cycles: list) -> list:
    """Jitter-collapse rows within each full native identity."""
    groups: dict = {}
    for cyc in cycles:
        groups.setdefault(cyc.identity_parts, []).append(cyc)
    return [
        _canonicalize_codex_cluster(cluster)
        for members in groups.values()
        for cluster in _mh.cluster_by_reset_jitter(
            members, reset_key=lambda c: c.reset.timestamp()
        )
    ]


def _boundary_has_live_reset(boundary) -> bool:
    """Whether a boundary object actually names a live cycle.

    The §7.4 guard needs a REAL live boundary. The former future-reset proxy was
    true for EVERY future-ending cycle, so arming the guard on a stub boundary
    suppressed every clip and made a historic cycle whose nominal reset had not
    yet passed render its full seven-day length. That is how one cycle key came
    back as ``Jul 21–Jul 28`` from the detail while the index said
    ``Jul 21–Jul 25`` (#373). With no live boundary the correct behaviour is to
    select no current cycle and clip exactly as before.
    """
    return getattr(boundary, "resets_at", None) is not None


def _select_physical_cycles(cycles: list, *, preferred_identity=None,
                            current_boundary=None, now_utc=None) -> list:
    """Select one full identity per physical reset boundary, then clip globally.

    #373 §7.4: the cycle the LIVE baseline marks current is never clipped by a
    successor — it ends at its own reset. Every other cycle clips exactly as
    before, and no identity axis enters the clip (§6 Q3: cross-identity
    clipping is what collapses the concurrent secondary/primary weekly windows
    into one non-overlapping timeline).

    ``current_boundary`` is a ``CodexCycleBoundary``.  Its reset can sit within
    the jitter floor of more than one DISTINCT identity cluster, so proximity
    is only the candidate filter.  ``_select_live_physical_cycle`` resolves one
    physical cycle (preferring the boundary's selected full identity), and that
    object supplies both the no-clip guard and rendered ``is_current`` fact.

    Suppressing a clip can restore an entire ROW, not merely lengthen one: the
    ``end > start`` filter below runs after clipping, so a live cycle whose
    successor starts at the same instant would otherwise be dropped outright.
    """
    identity_cycles = _canonical_identity_cycles(cycles)
    selected = [
        _choose_physical_cycle(cluster, preferred_identity=preferred_identity)
        for cluster in _mh.cluster_by_reset_jitter(
            identity_cycles, reset_key=lambda c: c.reset.timestamp()
        )
    ]
    selected.sort(key=lambda c: (c.start, c.reset, c.identity_parts))
    live_boundary = (
        current_boundary if _boundary_has_live_reset(current_boundary) else None
    )
    live_cycle = _select_live_physical_cycle(selected, live_boundary)
    for index, cyc in enumerate(selected):
        cyc.is_current = cyc is live_cycle
        if cyc.is_current:
            cyc.end = cyc.reset
            continue
        next_start = selected[index + 1].start if index + 1 < len(selected) else None
        cyc.end = min(cyc.reset, next_start) if next_start is not None else cyc.reset
    return [c for c in selected if c.end > c.start]


def _load_codex_cycles(stats_conn, root_keys, *, include_orphaned=False,
                       preferred_identity=None, current_boundary=None,
                       now_utc=None, account_key=None) -> list:
    """Unbounded reset-defined 7-day cycles, one selected identity per boundary.

    ``account_key`` (#416 Slice 3A review B3) scopes the enumeration to one
    account. ``_CodexCycle.identity_parts`` is ``(root, limit, slot, window)``
    and deliberately excludes the account, so without the predicate two accounts
    on one root are jitter-clustered TOGETHER and one account's cycle index
    lists the other's boundaries — the milestone-history equivalent of B1.
    ``None`` keeps the merged read, which is byte-stable and is what the
    cycle-DETAIL route and every existing caller still use.
    """
    roots = tuple(sorted({r for r in root_keys if isinstance(r, str) and r}))
    if not roots:
        return []
    placeholders = ",".join("?" for _ in roots)
    orphan_clause = "" if include_orphaned else "AND orphaned_at IS NULL "
    # `quota_window_blocks.account_key` is NOT NULL DEFAULT 'unattributed'.
    account_clause = "" if account_key is None else "AND account_key=? "
    account_params = () if account_key is None else (account_key,)
    # BEFORE the SQL, per #496 S5b section 4.7.
    assert_projection_readable(stats_conn)
    rows = stats_conn.execute(
        "SELECT source_root_key, logical_limit_key, observed_slot, "
        "       window_minutes, limit_id, limit_name, account_key, "
        "       resets_at_utc, nominal_start_at_utc, current_percent "
        "FROM quota_window_blocks "
        "WHERE source='codex' AND window_minutes=10080 "
        f"{orphan_clause}"
        f"{account_clause}"
        f"AND source_root_key IN ({placeholders}) "
        "ORDER BY unixepoch(resets_at_utc) DESC",
        (*account_params, *roots),
    ).fetchall()

    cycles: list = []
    for row in rows:
        if is_model_scoped_codex_quota(row["logical_limit_key"], row["limit_name"]):
            continue
        cyc = _CodexCycle(row)
        if cyc.start is None or cyc.reset is None or cyc.reset <= cyc.start:
            continue
        cycles.append(cyc)

    selected = _select_physical_cycles(
        cycles, preferred_identity=preferred_identity,
        current_boundary=current_boundary, now_utc=now_utc,
    )
    selected.sort(key=lambda c: c.start, reverse=True)
    return selected


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


def _select_live_physical_cycle(cycles: list, boundary):
    """Resolve at most one physical cycle for the live boundary.

    Jitter clustering is identity-local before the cross-identity physical
    selection.  Two distinct clusters may therefore be more than one jitter
    floor apart from each other while each remains within the floor of the
    boundary reset.  Prefer the boundary's already-selected full quota
    identity, then the closest member reset, with stable scalar tie-breakers.
    """
    if boundary is None:
        return None
    try:
        target = boundary.resets_at.astimezone(UTC)
    except (AttributeError, ValueError):
        return None
    candidates = [
        cyc for cyc in cycles
        if any(
            abs((member.reset - target).total_seconds())
            <= _mh.CODEX_CYCLE_JITTER_FLOOR_SECONDS
            for member in cyc.cluster_members
        )
    ]
    if not candidates:
        return None
    preferred_identity = getattr(boundary, "quota_identity", None)
    preferred = [
        cyc for cyc in candidates if _identity_matches_cycle(preferred_identity, cyc)
    ]
    pool = preferred or candidates

    def _rank(cyc):
        distance = min(
            abs((member.reset - target).total_seconds())
            for member in cyc.cluster_members
        )
        current_rank = (
            float("inf") if cyc.current_percent is None else -cyc.current_percent
        )
        return (
            distance,
            current_rank,
            cyc.reset,
            cyc.identity_parts,
        )

    return min(pool, key=_rank)


def _codex_milestone_count(stats_conn, cyc) -> tuple[int, str | None]:
    """Unique threshold count + max captured-at for one selected identity."""
    resets = cyc.member_reset_isos
    placeholders = ",".join("unixepoch(?)" for _ in resets)
    row = stats_conn.execute(
        "SELECT COUNT(DISTINCT percent_threshold), MAX(captured_at_utc) "
        "FROM quota_percent_milestones "
        "WHERE source='codex' AND source_root_key=? AND account_key=? "
        "  AND logical_limit_key=? "
        "  AND observed_slot=? AND window_minutes=? "
        f"  AND unixepoch(resets_at_utc) IN ({placeholders}) AND orphaned_at IS NULL",
        (cyc.root, cyc.account_key, cyc.limit, cyc.slot, cyc.window, *resets),
    ).fetchone()
    return int(row[0] or 0), row[1]


def _codex_five_hour_rows(stats_conn, cyc, *, include_orphaned=False) -> list:
    """Every retained 5h block on the selected root intersecting [start, end).

    The account filter admits the cycle's own key OR the ``unattributed``
    sentinel, and deliberately NOT strict equality. Unlike
    ``_codex_milestone_count`` — whose block and milestone parameters both come
    from one ``block.identity`` and so cannot disagree — this query spans
    identities, selecting by root plus time range. The 300-minute windows are a
    separate physical-window group from the weekly one and
    ``adopt_unidentified_observations`` resolves attribution PER GROUP, so a
    decorated install can legitimately carry the weekly window under a real
    account while its 5h windows are still unattributed. Strict equality would
    then match nothing and the cycle would render 0 blocks (#373).

    The widening is one-directional on purpose: an unattributed CYCLE never
    picks up another account's identified 5h rows, which would violate #341's
    never-combine rule.
    """
    orphan_clause = "" if include_orphaned else "AND orphaned_at IS NULL "
    assert_projection_readable(stats_conn)
    return stats_conn.execute(
        "SELECT source_root_key, logical_limit_key, observed_slot, "
        "       window_minutes, limit_id, limit_name, account_key, "
        "       resets_at_utc, nominal_start_at_utc, current_percent "
        "FROM quota_window_blocks "
        "WHERE source='codex' AND window_minutes=300 "
        f"{orphan_clause}"
        "AND source_root_key=? AND account_key IN (?,?) "
        "AND unixepoch(nominal_start_at_utc) < unixepoch(?) "
        "AND unixepoch(resets_at_utc) > unixepoch(?) "
        "ORDER BY unixepoch(nominal_start_at_utc) ASC",
        (cyc.root, cyc.account_key, UNATTRIBUTED,
         cyc.end.astimezone(UTC).isoformat(),
         cyc.start.astimezone(UTC).isoformat()),
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
    identity_clusters = _canonical_identity_cycles(blocks)
    clusters = [
        _choose_physical_cycle(members)
        for members in _mh.cluster_by_reset_jitter(
            identity_clusters, reset_key=lambda b: b.reset.timestamp()
        )
    ]
    clusters.sort(key=lambda b: b.start)
    return clusters


def _codex_cycle_entry(stats_conn, cyc, tz) -> dict:
    milestone_count, max_captured = _codex_milestone_count(stats_conn, cyc)
    block_count = len(_codex_five_hour_clusters(stats_conn, cyc))
    key = cyc.key
    return {
        "key": key,
        "start_at_utc": _iso_z(cyc.start),
        "end_at_utc": _iso_z(cyc.end),
        "resets_at_utc": _iso_z(cyc.reset),
        "label": _codex_cycle_label(cyc, tz),
        "is_current": bool(cyc.is_current),
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


def build_codex_cycle_index(stats_conn, *, identity, now_utc,
                            account_key=None) -> list:
    """Newest-first Codex cycle index over the durable projection (spec §1c,
    §3). Enumerates the hero root's reset-defined ledger with no depth cap;
    one selected full native identity per physical reset.

    ``account_key`` (#416 Slice 3A review B3) builds the index for ONE account,
    so a focused hero renders its own milestone history instead of the first
    account's. ``None`` is the merged parent index and is byte-stable."""
    now = now_utc.astimezone(UTC) if now_utc.tzinfo else now_utc.replace(tzinfo=UTC)
    tz = _resolve_display_tz()
    cycles = _load_codex_cycles(
        stats_conn, getattr(identity, "source_root_keys", ()),
        preferred_identity=getattr(identity, "quota_identity", None),
        current_boundary=identity, now_utc=now, account_key=account_key,
    )
    return [_codex_cycle_entry(stats_conn, c, tz) for c in cycles]


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


def _codex_breakdown_rows(ident, reset, speed, cache_conn, stats_conn,
                          account_key=None):
    try:
        # #416 closeout F3: the flavour is NOT this route's to pick. Every key
        # that reaches here comes from the POST-fold durable projection, so the
        # pre-fold BOUNDARY read widens — but `codex_quota_breakdown` now
        # decides that per read, because the two reads behind it have opposite
        # correctness requirements and the single flag conflated them. The
        # accounting read stays strict, so a crossing whose spend is
        # unattributed renders $0.00 rather than adopting another scope's rows.
        return codex_quota_breakdown(
            ident, reset, speed=speed, cache_conn=cache_conn, stats_conn=stats_conn,
            account_key=account_key,
        )
    except sqlite3.Error:
        return None


def _union_cluster_milestones(cluster, block_key, speed, cache_conn, stats_conn,
                              account_key=None):
    """Union ``codex_quota_breakdown`` milestone rows across every cluster
    member (each jittered sibling reset via the existing per-reset path).

    One physical reset can split its crossings across sibling resets, so the
    full ledger is the union — shaped and ordered by ``(captured_at, percent)``.
    Returns ``None`` if any member's projection is incoherent (mirrors the
    single-reset signal so the caller can surface ``projection_incoherent``).

    ``account_key`` (#416) is the ROUTE's focus, deliberately not
    ``member.account_key``. The milestone read keeps using the member's own
    stamp — those rows were written under that exact identity — but the two
    CACHE reads are scoped to the account whose view this is. The two differ for
    the 5h leg: ``_codex_five_hour_rows`` admits a still-``unattributed`` block
    into a real account's cycle (the #373 per-window-group rule). SELECTION is
    therefore the cycle's scope, and so is the block-start boundary — but COST
    is not widened to match (#416 closeout F3): an unattributed block selected
    into a real account's cycle renders its crossings at ``$0.00``, because
    costing it under the cycle's account would file those dollars twice, in the
    focused scope and in the ``unattributed`` one. ``None`` keeps the merged
    read.
    """
    shaped: list = []
    for member in cluster.cluster_members:
        ident = QuotaWindowIdentity(
            source="codex", source_root_key=member.root,
            logical_limit_key=member.limit, observed_slot=member.slot,
            window_minutes=member.window, account_key=member.account_key,
            limit_id=member.limit_id, limit_name=member.limit_name,
        )
        breakdown = _codex_breakdown_rows(
            ident, member.reset, speed, cache_conn, stats_conn,
            account_key=account_key,
        )
        if breakdown is None:
            return None  # projection-incoherent signal to the caller
        shaped.extend(
            _shape_codex_milestone(r, key_parts=member.key_parts, block_key=block_key)
            for r in breakdown
        )
    # One physical quota ledger has one crossing per integer threshold. Jitter
    # siblings can contribute missing evidence, but never duplicate a row.
    by_percent: dict[int, dict] = {}
    for row in shaped:
        percent = int(row["percent"])
        prior = by_percent.get(percent)
        if prior is None or (row["captured_at"] or "", row["key"]) < (
            prior["captured_at"] or "", prior["key"]
        ):
            by_percent[percent] = row
    return sorted(
        by_percent.values(), key=lambda m: (m["captured_at"] or "", m["percent"])
    )


def _codex_cycle_blocks(stats_conn, cache_conn, cyc, speed,
                        account_key=None) -> "list | None":
    out: list = []
    for block in _codex_five_hour_clusters(stats_conn, cyc):
        block_key = block.block_key
        milestones = _union_cluster_milestones(
            block, block_key, speed, cache_conn, stats_conn,
            account_key=account_key,
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
    stats_conn, cache_conn, *, identity, key, speed, now_utc, account_key=None
):
    """Complete payload for one Codex cycle (spec §1c). On success returns a
    dict mirroring the Claude detail shape (segments = a single Codex segment,
    dividers = []). On failure returns ``(None, reason)`` where reason ∈
    {pruned, rebuild_pending, projection_incoherent, unknown} — Task 4 maps it
    into the 404 body.

    ``account_key`` (#416) focuses the whole route on ONE account. It scopes the
    cycle ENUMERATION and the milestone LADDER together, which has to be one
    parameter rather than two: ``build_codex_cycle_index`` already takes it
    (Slice 3A review B3) and builds each key from THAT account's cluster
    representative, while ``_canonicalize_codex_cluster`` takes ``max(reset)``
    across the cluster — so a merged enumeration resolves a jitter-separated
    sibling's reset and the focused account's own index key 404s.

    ``None`` is the merged route every shipped caller uses and is byte-stable by
    construction: nothing below it changes shape.
    """
    now = now_utc.astimezone(UTC) if now_utc.tzinfo else now_utc.replace(tzinfo=UTC)
    tz = _resolve_display_tz()
    try:
        cycles = _load_codex_cycles(
            stats_conn, getattr(identity, "source_root_keys", ()),
            preferred_identity=getattr(identity, "quota_identity", None),
            current_boundary=identity, now_utc=now, account_key=account_key,
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
                preferred_identity=getattr(identity, "quota_identity", None),
                current_boundary=identity, now_utc=now, account_key=account_key,
            )
        except sqlite3.Error:
            return (None, "projection_incoherent")
        if any(c.key == key for c in orphaned):
            return (None, "pruned")
        return (None, "unknown")

    milestones = _union_cluster_milestones(
        match, match.block_key, speed, cache_conn, stats_conn,
        account_key=account_key,
    )
    if milestones is None:
        return (None, "projection_incoherent")
    blocks = _codex_cycle_blocks(
        stats_conn, cache_conn, match, speed, account_key=account_key)
    if blocks is None:
        return (None, "projection_incoherent")

    entry = _codex_cycle_entry(stats_conn, match, tz)
    return {
        "source": "codex",
        "key": entry["key"],
        "start_at_utc": entry["start_at_utc"],
        "end_at_utc": entry["end_at_utc"],
        "resets_at_utc": entry["resets_at_utc"],
        "label": entry["label"],
        "is_current": entry["is_current"],
        "detail_stamp": entry["detail_stamp"],
        "segments": [{
            "key": dashboard_resource_key(
                "milestone_segment", "codex", *match.key_parts
            ),
            "milestones": milestones,
        }],
        "dividers": [],
        "blocks": blocks,
    }
