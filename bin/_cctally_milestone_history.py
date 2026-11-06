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

import bisect
import datetime as dt
import hashlib
import json
import math
import os
import secrets
import sqlite3
import sys
import threading
from dataclasses import replace
from typing import NamedTuple

from _cctally_core import (
    _canonicalize_optional_iso, make_week_ref, parse_iso_datetime,
)
import _cctally_quota as _quota
import _lib_pricing
from _cctally_quota import assert_projection_readable, codex_quota_breakdown
from _lib_accounts import UNATTRIBUTED
from _lib_codex_pools import is_model_scoped_codex_quota
from _lib_dashboard_sources import dashboard_resource_key
from _cctally_percent_breakdown import (
    OBSERVATION_GAP_CAUSE,
    classify_observation_gaps,
)
from _lib_display_tz import _resolve_display_tz_obj, format_display_dt
from _lib_json_envelope import _iso_z
from _lib_quota import QuotaWindowIdentity

import _lib_milestone_history as _mh
import _lib_stats_content_revision as _content_revision

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


def _account_predicate(account_key, column: str) -> "tuple[str, tuple]":
    """``" AND <column> = ?"`` and its parameter for a scoped read, or nothing.

    #836 spec 5.5: an explicit account is STRICT equality everywhere, so the
    ``unattributed`` bucket never enters a real account's history and
    ``account_key="unattributed"`` selects that bucket alone. ``None`` is the
    merged read, unchanged.
    """
    if account_key is None:
        return "", ()
    return f" AND {column} = ?", (account_key,)


def _count_blocks(conn: sqlite3.Connection, start_iso, end_iso, *,
                  account_key=None) -> int:
    """Count ``five_hour_blocks`` whose interval intersects ``[start, end)``.

    Half-open intersection on epoch seconds via ``unixepoch()`` (mixed stored
    offsets). Returns 0 when the week lacks canonical boundaries.
    ``account_key`` (#836) counts only that account's blocks.
    """
    if not start_iso or not end_iso:
        return 0
    acct_sql, acct_params = _account_predicate(account_key, "account_key")
    row = conn.execute(
        "SELECT COUNT(*) FROM five_hour_blocks "
        "WHERE unixepoch(block_start_at) < unixepoch(?) "
        "  AND unixepoch(five_hour_resets_at) > unixepoch(?)" + acct_sql,
        (end_iso, start_iso) + acct_params,
    ).fetchone()
    return int(row[0] or 0)


# ── Claude week index (spec §1a) ───────────────────────────────────────


def _navigable_claude_refs(conn: sqlite3.Connection, *, account_key=None,
                           as_of_utc=None) -> list:
    """The navigable Claude week refs, newest-first, per spec §1a.

    Navigable set = weeks with ≥1 ``weekly_usage_snapshots`` row ∪ weeks with
    ≥1 ``percent_milestones`` row (cost-only weeks excluded). Same-key credit
    reset-defined segment remains independently navigable; a defensive
    milestone-only week absent from ``get_recent_weeks`` is synthesized from
    its milestone rows' stored boundaries.

    ``account_key`` (#836 spec 5.5) scopes every leg to one account's rows;
    ``as_of_utc`` admits only reset events effective at or before it. Both
    ``None`` is the merged, as-of-free enumeration, unchanged.
    """
    c = _cctally()
    # unbounded, newest-first
    refs = (c.get_recent_weeks(conn, None) if account_key is None
            else c.get_recent_weeks(conn, None, account_key=account_key))

    acct_sql, acct_params = _account_predicate(account_key, "account_key")
    acct_where = acct_sql.replace(" AND ", " WHERE ", 1)
    usage_keys = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT week_start_date FROM weekly_usage_snapshots"
            + acct_where, acct_params,
        ).fetchall()
    }
    milestone_keys = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT week_start_date FROM percent_milestones"
            + acct_where, acct_params,
        ).fetchall()
    }
    navigable = usage_keys | milestone_keys

    refs = [r for r in refs if r.key in navigable]
    present = {r.key for r in refs}
    for k in sorted(milestone_keys - present, reverse=True):
        ref = _synthesize_milestone_only_ref(conn, k, account_key=account_key)
        if ref is not None:
            refs.append(ref)
    return _derive_claude_reset_cycles(
        conn, refs, account_key=account_key, as_of_utc=as_of_utc)


def _parse_optional_iso(value):
    if not value:
        return None
    try:
        return parse_iso_datetime(value, "milestone_cycle.boundary")
    except (TypeError, ValueError):
        return None


def _claude_storage_boundaries(conn: sqlite3.Connection, key: str, *,
                               account_key=None):
    """Return the outer retained boundary for one storage date bucket
    (``account_key``: that account's rows only, in both legs)."""
    acct_sql, acct_params = _account_predicate(account_key, "account_key")
    rows = conn.execute(
        "SELECT week_start_at, week_end_at FROM weekly_usage_snapshots "
        "WHERE week_start_date=? AND week_start_at IS NOT NULL "
        "AND week_end_at IS NOT NULL" + acct_sql + " "
        "UNION ALL "
        "SELECT week_start_at, week_end_at FROM percent_milestones "
        "WHERE week_start_date=? AND week_start_at IS NOT NULL "
        "AND week_end_at IS NOT NULL" + acct_sql,
        (key,) + acct_params + (key,) + acct_params,
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


def _claude_reset_cuts_cycle(outer, effective) -> bool:
    """Whether a linked reset's effective instant starts a new cycle inside a
    storage bucket whose outer retained boundary is ``outer`` ``(start, end)``.

    The ONE rule for where a Claude reset-defined cycle begins (#836 review
    F7): the history index cuts its buckets with it, and the decorated card's
    cycle scope (`_claude_current_cycle_scope`) starts its cycle with it, so
    the card and its current index entry cannot disagree about the start. In
    particular an early re-anchor stamped at the capture second can precede
    the new window's nearest-hour start; while both windows share one storage
    key the reset instant still starts the cycle.
    """
    return (outer is not None and effective is not None
            and outer[0] < effective < outer[1])


def _linked_reset_events(conn: sqlite3.Connection, key: str, *,
                         account_key=None, as_of_utc=None) -> list:
    """Reset rows linked to any retained boundary in a storage bucket.

    #836 spec 5.5: the two ``EXISTS`` legs are one parenthesized group, so the
    account and as-of predicates below bind the whole disjunction rather than
    its second leg (``A OR B AND x`` is ``A OR (B AND x)``). ``account_key``
    scopes the event and both legs' rows; ``as_of_utc`` admits only events
    effective at or before it — the rule `_latest_reset_event_for_end` applies
    for the card, so a reset that takes effect later cuts neither yet.
    """
    acct_s, acct_p = _account_predicate(account_key, "s.account_key")
    acct_m, _ = _account_predicate(account_key, "m.account_key")
    acct_e, _ = _account_predicate(account_key, "e.account_key")
    as_of_sql = ""
    as_of_params: tuple = ()
    if as_of_utc is not None:
        as_of_sql = (" AND unixepoch(e.effective_reset_at_utc) "
                     "<= unixepoch(?)")
        as_of_params = (as_of_utc,)
    return conn.execute(
        "SELECT DISTINCT e.id, e.effective_reset_at_utc "
        "FROM week_reset_events e "
        "WHERE (EXISTS ("
        "  SELECT 1 FROM weekly_usage_snapshots s "
        "  WHERE s.week_start_date=?" + acct_s + " AND ("
        "    unixepoch(s.week_end_at)=unixepoch(e.old_week_end_at) OR "
        "    unixepoch(s.week_end_at)=unixepoch(e.new_week_end_at)"
        "  )"
        ") OR EXISTS ("
        "  SELECT 1 FROM percent_milestones m "
        "  WHERE m.week_start_date=?" + acct_m + " AND ("
        "    unixepoch(m.week_end_at)=unixepoch(e.old_week_end_at) OR "
        "    unixepoch(m.week_end_at)=unixepoch(e.new_week_end_at)"
        "  )"
        "))" + acct_e + as_of_sql + " "
        "ORDER BY unixepoch(e.effective_reset_at_utc), e.id",
        (key,) + acct_p + (key,) + acct_p + acct_p + as_of_params,
    ).fetchall()


def _derive_claude_reset_cycles(conn: sqlite3.Connection, refs: list, *,
                                account_key=None, as_of_utc=None) -> list:
    """Expand storage buckets into provider reset-defined cycles.

    ``get_recent_weeks`` can split an in-place credit, but an early re-anchor
    whose old and new boundaries both remain under one ``week_start_date``
    only exposes the pre-reset half. The retained snapshot boundary set plus
    ``week_reset_events`` is the authoritative reset ledger for both shapes.

    ``account_key`` / ``as_of_utc`` (#836): that account's boundaries and
    resets, cut only by events already effective at ``as_of_utc``.
    """
    out: list = []
    by_key: dict = {}
    for ref in refs:
        by_key.setdefault(ref.key, ref)
    for key, template in by_key.items():
        outer = _claude_storage_boundaries(conn, key, account_key=account_key)
        if outer is None:
            out.append(template)
            continue
        start, end = outer
        cuts = []
        for event in _linked_reset_events(
                conn, key, account_key=account_key, as_of_utc=as_of_utc):
            effective = _parse_optional_iso(event["effective_reset_at_utc"])
            if _claude_reset_cuts_cycle(outer, effective):
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


def _synthesize_milestone_only_ref(conn: sqlite3.Connection, key: str, *,
                                   account_key=None):
    """Defensive ref for a milestone-only week (no usage snapshot).

    Boundaries fall back to the milestone rows' stored
    ``week_start_at``/``week_end_at``/``week_end_date`` (spec §1a). Returns
    ``None`` if a valid ref can't be built. ``account_key`` (#836): from that
    account's milestone rows only.
    """
    acct_sql, acct_params = _account_predicate(account_key, "account_key")
    row = conn.execute(
        "SELECT week_end_date, week_start_at, week_end_at FROM percent_milestones "
        "WHERE week_start_date = ?" + acct_sql + " "
        "ORDER BY (week_start_at IS NULL), captured_at_utc ASC LIMIT 1",
        (key,) + acct_params,
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
    # A milestone-only week whose rows predate `percent_milestones.week_start_at`
    # carries no boundaries at all, and `_synthesize_milestone_only_ref` returns
    # that ref rather than dropping the week. `dashboard_resource_key` rejects
    # the empty string as an identity part but accepts `None` as its own typed
    # part, so the absent boundary is passed as the absence it is. Every ref
    # that does carry both boundaries keys exactly as before.
    return dashboard_resource_key(
        "milestone_cycle", "claude", ref.key,
        ref.week_start_at or None, ref.week_end_at or None,
    )


def _claude_cycle_rows(conn: sqlite3.Connection, ref, *, account_key=None,
                       cohort_id=None) -> list:
    """The cycle's weekly milestone cohort.

    ``account_key`` (#836 spec 5.5) scopes the cohort event, both of its
    supporting subqueries and the milestone read to one account. ``cohort_id``
    names the cohort outright: a scoped index passes the card's active reset
    for its CURRENT cycle, so the index, the detail and the card read one
    segment (spec 5.2 step 2) rather than re-deriving it from the cut instant.
    """
    acct_r, acct_p = _account_predicate(account_key, "account_key")
    if cohort_id is None:
        cohort_id = _claude_cycle_cohort_id(conn, ref, account_key=account_key)
    return conn.execute(
        "SELECT * FROM percent_milestones WHERE week_start_date=? "
        "AND reset_event_id=?" + acct_r + " "
        "ORDER BY unixepoch(captured_at_utc) ASC, percent_threshold ASC",
        (ref.key, cohort_id) + acct_p,
    ).fetchall()


def _claude_cycle_cohort_id(conn: sqlite3.Connection, ref, *,
                            account_key=None) -> int:
    """The cycle's weekly milestone cohort (`percent_milestones.
    reset_event_id`) as derived from its effective start: the newest reset
    event effective at that instant that names this storage week, else ``0``.
    Shared by the cohort read and the #1050 stamp, which scopes on it."""
    acct_s, acct_p = _account_predicate(account_key, "s.account_key")
    acct_m, _ = _account_predicate(account_key, "m.account_key")
    acct_e, _ = _account_predicate(account_key, "e.account_key")
    cohort_id = 0
    if ref.week_start_at:
        row = conn.execute(
            "SELECT e.id FROM week_reset_events e "
            "WHERE unixepoch(e.effective_reset_at_utc)=unixepoch(?)"
            + acct_e + " "
            "AND (EXISTS ("
            "  SELECT 1 FROM weekly_usage_snapshots s "
            "  WHERE s.week_start_date=?" + acct_s + " AND ("
            "    unixepoch(s.week_end_at)=unixepoch(e.old_week_end_at) OR "
            "    unixepoch(s.week_end_at)=unixepoch(e.new_week_end_at)"
            "  )"
            ") OR EXISTS ("
            "  SELECT 1 FROM percent_milestones m "
            "  WHERE m.week_start_date=?" + acct_m + " AND ("
            "    unixepoch(m.week_end_at)=unixepoch(e.old_week_end_at) OR "
            "    unixepoch(m.week_end_at)=unixepoch(e.new_week_end_at)"
            "  )"
            ")) ORDER BY e.id DESC LIMIT 1",
            (ref.week_start_at,) + acct_p + (ref.key,) + acct_p
            + (ref.key,) + acct_p,
        ).fetchone()
        if row is not None:
            cohort_id = int(row[0])
    return cohort_id


def _five_hour_detail_revision(conn, start_iso, end_iso, *,
                               account_key=None) -> tuple[int, ...]:
    """Append-only 5h child revision for one cycle's intersecting blocks.

    The detail cache uses the index stamp. Weekly crossing counts do not move
    when another account records a 5h credit or crossing in an existing block.
    These child tables insert new ids for those events; count and max id also
    notice deletions and replacement rows without hashing an unbounded ledger.

    #836 spec 5.4: ``TOTAL(m.usage_snapshot_id)`` moves when a snapshot DELETE
    rewrites a retained crossing's join to the ``0`` sentinel, which changes
    no count and no max id. ``account_key`` (spec 5.5) reads only that
    account's blocks, so another account's changes never move a scoped stamp.
    """
    if not start_iso or not end_iso:
        return (0, 0, 0, 0, 0, 0, 0)
    acct_sql, acct_params = _account_predicate(account_key, "b.account_key")
    interval = (
        "unixepoch(b.block_start_at) < unixepoch(?) "
        "AND unixepoch(b.five_hour_resets_at) > unixepoch(?)" + acct_sql
    )
    bounds = (end_iso, start_iso) + acct_params
    blocks = conn.execute(
        "SELECT COUNT(*), MAX(b.id) FROM five_hour_blocks b "
        f"WHERE {interval}", bounds,
    ).fetchone()
    milestones = conn.execute(
        "SELECT COUNT(*), MAX(m.id), TOTAL(m.usage_snapshot_id) "
        "FROM five_hour_blocks b "
        "JOIN five_hour_milestones m ON m.block_id = b.id "
        f"WHERE {interval}", bounds,
    ).fetchone()
    credits = conn.execute(
        "SELECT COUNT(*), MAX(e.id) FROM five_hour_blocks b "
        "JOIN five_hour_reset_events e "
        "ON e.account_key = b.account_key "
        "AND e.five_hour_window_key = b.five_hour_window_key "
        f"WHERE {interval}", bounds,
    ).fetchone()
    return tuple(int(value or 0) for row in (blocks, milestones, credits)
                 for value in row)


def _unavailable_join_rows(conn, *, account_key=None) -> list:
    """Every 5h milestone whose effective weekly join is unavailable, with
    its block's bounds as SQLite epochs, ascending by id (#836 spec 5.4).

    Read ONCE per index build and bucketed per cycle by
    `_unavailable_join_component`. The bounds are wrapped in ``unixepoch()``,
    which no index serves, so a per-cycle query scanned every milestone once
    per index entry. The epochs are SQLite's own, so the bucketing compares
    exactly what the per-cycle predicate compared; an unparseable bound is
    NULL and never matches. ``account_key`` is the same strict scope
    (`_account_predicate`).
    """
    from _cctally_five_hour import weekly_join_unavailable_sql
    acct_sql, acct_params = _account_predicate(account_key, "b.account_key")
    return [(int(r[0]), r[1], r[2]) for r in conn.execute(
        "SELECT m.id, unixepoch(b.block_start_at), "
        "unixepoch(b.five_hour_resets_at) FROM five_hour_blocks b "
        "JOIN five_hour_milestones m ON m.block_id = b.id "
        "LEFT JOIN weekly_usage_snapshots s ON s.id = m.usage_snapshot_id "
        "WHERE " + weekly_join_unavailable_sql(conn, "s") + acct_sql
        + " ORDER BY m.id",
        acct_params,
    ).fetchall()]


def _unavailable_join_component(conn, start_iso, end_iso,
                                unavailable_rows) -> tuple:
    """The detail stamp's unavailable-join component (#836 spec 5.4).

    A digest of the sorted ids of the cycle's 5h milestones whose effective
    weekly join is unavailable — the joined snapshot absent, the ``0``
    sentinel, or suppression-retired (`weekly_join_unavailable_sql`, the same
    predicate as the dashboard's retired-join signature leg). Availability is
    exactly what the fold's weekly rejection changes, and a set rather than a
    count, so a restoration of one crossing beside the retirement of another
    still moves the stamp. ``()`` when the set is empty, so a store with no
    unavailable join keeps its stamp bytes.

    ``unavailable_rows`` is the build's `_unavailable_join_rows`; a crossing
    belongs to the cycle when its block intersects ``[start, end)`` — block
    start before the cycle's end, block reset after the cycle's start — with
    the cycle bounds converted by SQLite's ``unixepoch()`` as well.
    """
    if not start_iso or not end_iso or not unavailable_rows:
        return ()
    start_epoch, end_epoch = conn.execute(
        "SELECT unixepoch(?), unixepoch(?)", (start_iso, end_iso)).fetchone()
    if start_epoch is None or end_epoch is None:
        return ()
    ids = [mid for mid, block_start, block_resets in unavailable_rows
           if block_start is not None and block_resets is not None
           and block_start < end_epoch and block_resets > start_epoch]
    if not ids:
        return ()
    return ("u" + _mh.compute_detail_stamp(*ids),)


class _ClaudeRevisionScopes:
    """The #1050 content-revision material of ONE index build (spec 4.6).

    Read once per `build_claude_week_index`: the build's `ContentRevision`
    and every scope row, in one read snapshot — the caller's transaction when
    one is open (the detail route's), otherwise a short ``BEGIN`` …
    ``ROLLBACK`` of its own. The scope rows are strict to ``account_key`` when
    the index is scoped and merged across every account (``unattributed``
    included) otherwise, exactly as the index reads its rows.

    A ``current`` store names the relevant revision of each cycle; any other
    state (absent, partial, unsupported, unreadable) cannot certify a stamp,
    so every entry of the build carries the same fresh nonce instead and the
    client never serves a detail cached under an earlier one.
    """

    def __init__(self, conn: sqlite3.Connection, *, account_key=None):
        own = not conn.in_transaction
        if own:
            conn.execute("BEGIN")
        try:
            try:
                revision = _content_revision.read_claude_content_revision(conn)
            except _content_revision.StatsContentRevisionUnreadable:
                revision = None
            trusted = (revision is not None
                       and revision.state == _content_revision.STATE_CURRENT)
            rows = (_content_revision.read_scope_rows(
                conn, account_key=account_key) if trusted else [])
        finally:
            if own:
                conn.rollback()
        self.trusted = trusted
        self.cycle_basis = revision.cycle_basis if trusted else None
        self.nonce = None if trusted else secrets.token_hex(8)
        self._keyed: dict = {"cohort": {}, "bucket": {}}
        blocks, points = [], []
        for row in rows:
            if row.kind in self._keyed:
                keyed = self._keyed[row.kind]
                keyed[row.scope_key] = max(
                    keyed.get(row.scope_key, row.revision), row.revision)
            elif row.start_epoch is None or row.end_epoch is None:
                continue  # an unparseable bound never matches, as in SQL
            elif row.kind == "block":
                blocks.append((row.start_epoch, row.end_epoch, row.revision))
            elif row.kind == "reset_point":
                points.append((row.start_epoch, row.revision))
        blocks.sort()
        points.sort()
        self._blocks = blocks
        self._block_starts = [b[0] for b in blocks]
        self._points = points
        self._point_epochs = [p[0] for p in points]

    def component(self, conn, ref, cohort_id, start_iso, end_iso) -> tuple:
        """The stamp component for one cycle: ``("c" + …,)`` over the MAX
        revision of its cohort, its bucket, every block intersecting
        ``[start, end)`` and every reset point in ``[start, end]``; ``()``
        when no scope is relevant and the store was never published or
        repaired (so such a cycle keeps its bytes); ``("u" + nonce,)`` on an
        untrusted store."""
        if not self.trusted:
            return ("u" + self.nonce,)
        relevant = [
            rev for rev in (
                self._keyed["cohort"].get(f"{ref.key}|{cohort_id}"),
                self._keyed["bucket"].get(ref.key),
            ) if rev is not None
        ]
        if (self._blocks or self._points) and start_iso and end_iso:
            start, end = conn.execute(
                "SELECT unixepoch(?), unixepoch(?)",
                (start_iso, end_iso)).fetchone()
            if start is not None and end is not None:
                # Blocks starting before the cycle's end, then ending after
                # its start; reset points inside the closed interval.
                upto = bisect.bisect_left(self._block_starts, end)
                relevant.extend(rev for _s, b_end, rev in self._blocks[:upto]
                                if b_end > start)
                lo = bisect.bisect_left(self._point_epochs, start)
                hi = bisect.bisect_right(self._point_epochs, end)
                relevant.extend(rev for _e, rev in self._points[lo:hi])
        if not relevant and self.cycle_basis is None:
            return ()
        return ("c" + _mh.compute_detail_stamp(
            self.cycle_basis, max(relevant, default=None)),)


def _index_entry(conn, ref, current_cycle_key, tz, *, unavailable_rows,
                 revision_scopes, decorated=False, account_key=None,
                 cohort_id=None) -> dict:
    if cohort_id is None:
        cohort_id = _claude_cycle_cohort_id(conn, ref, account_key=account_key)
    rows = _claude_cycle_rows(
        conn, ref, account_key=account_key, cohort_id=cohort_id)
    milestone_count = len(rows)
    segment_count = 1 if rows else 0
    max_captured = max((r["captured_at_utc"] for r in rows), default=None)
    start_z = _to_iso_z(ref.week_start_at)
    end_z = _to_iso_z(ref.week_end_at)
    block_count = _count_blocks(conn, start_z, end_z, account_key=account_key)
    key = _claude_cycle_key(ref)
    # R8: leave the <=1-real-account stamp byte-identical. On a decorated
    # provider the fetched block list contains multiple account-owned rows,
    # so its cache identity also needs their child-ledger revision.
    block_revision = (
        _five_hour_detail_revision(conn, start_z, end_z, account_key=account_key)
        if decorated else ())
    # #836 spec 5.4/5.5: every store's stamp names which crossings have an
    # unavailable weekly join (appended only when there is one), and a scoped
    # stamp names its account, because one opaque cycle key is shared by every
    # account whose subscription week has the same boundaries.
    unavailable = _unavailable_join_component(
        conn, start_z, end_z, unavailable_rows)
    scope_part = () if account_key is None else ("a" + account_key,)
    # #1050 spec 4.6: the cycle's stored-content revision, so an in-place
    # correction of any row this cycle reads re-stamps it (and only it).
    revision_part = revision_scopes.component(
        conn, ref, cohort_id, start_z, end_z)

    # #750 S4 §5.3. A CURRENT single-segment cycle neither fetches its detail
    # nor uses one that arrives, so a gap in the LIVE cycle would stay
    # invisible: the disclosure travels on the week-detail payload. This hint
    # is what gives the client a reason to fetch, and a reason to render what
    # comes back. It was written only after the failing test §5.3 requires
    # demonstrated the need — the probe is
    # `renders the disclosure for a CURRENT single-segment cycle` in
    # `dashboard/web/src/modals/currentWeekHistory.test.tsx`, and it failed.
    #
    # Additive and cheap: the rows are already loaded, and the classifier is
    # the same pure kernel the detail route uses, so the index and the detail
    # cannot disagree about whether a cycle has a gap.
    has_gap = bool(
        classify_observation_gaps(rows).runs) if rows else False

    return {
        "key": key,
        "start_at_utc": start_z,
        "end_at_utc": end_z,
        "label": _week_label(ref, tz),
        "is_current": key == current_cycle_key,
        "milestone_count": milestone_count,
        "block_count": block_count,
        "segment_count": segment_count,
        "has_observation_gap": has_gap,
        "detail_stamp": _mh.compute_detail_stamp(
            key, milestone_count, block_count, segment_count, max_captured,
            *block_revision, *revision_part, *unavailable, *scope_part,
        ),
    }


def _same_instant(a, b) -> bool:
    """Two stored boundary texts name the same instant (parsed, never by
    spelling). Both absent is a match; one absent is not."""
    if not a and not b:
        return True
    pa, pb = _parse_optional_iso(a), _parse_optional_iso(b)
    return pa is not None and pb is not None and pa == pb


def _canonical_end(value):
    try:
        return _canonicalize_optional_iso(value, "claude.cycle.end")
    except (TypeError, ValueError):
        return None


def _scoped_current_ref(refs: list, cycle_scope):
    """The ref in FULL interval agreement with the card's cycle (#836 spec
    5.5): the same storage key, the same effective start and the same
    canonical end, parsed as instants; on the date-only leg, the ref with that
    storage key and absent bounds. A start-only match is not enough and no ref
    is current merely by position. ``None`` when nothing agrees."""
    if cycle_scope is None:
        return None
    for ref in refs:
        if ref.key != cycle_scope.week_start_date:
            continue
        if cycle_scope.week_start_at is None:
            if not ref.week_start_at and not ref.week_end_at:
                return ref
            continue
        if (_same_instant(ref.week_start_at, cycle_scope.effective_start_at)
                and _same_instant(_canonical_end(ref.week_end_at),
                                  cycle_scope.week_end_at)):
            return ref
    return None


def _scoped_refs(conn, account_key, cycle_scope, now_utc=None):
    """The scoped enumeration and its current ref (#836 spec 5.5).

    Derived at the scope's as-of instant, so a retained reset that takes
    effect later cuts neither the card nor the index yet. Without a scope the
    as-of is ``now_utc`` (the build's own instant), and no ref is current.
    """
    if cycle_scope is not None:
        as_of = cycle_scope.as_of_utc
    elif now_utc is not None:
        as_of = _iso_z(now_utc)
    else:
        as_of = None
    refs = _navigable_claude_refs(
        conn, account_key=account_key, as_of_utc=as_of)
    scope = (cycle_scope if cycle_scope is not None
             and cycle_scope.account_key == account_key else None)
    return refs, _scoped_current_ref(refs, scope), scope


def build_claude_week_index(conn: sqlite3.Connection, *, account_key=None,
                            cycle_scope=None, now_utc=None) -> list:
    """Newest-first navigable Claude week index (spec §1a, §3).

    One entry per effective reset-defined cycle. Multiple cycles may share one
    storage ``week_start_date`` but always have distinct opaque keys.

    ``account_key`` (#836 spec 5.5, U1) builds ONE account's history: its own
    cycles, cohorts, block counts and stamps, derived at ``cycle_scope``'s
    as-of; the current entry is the one in full interval agreement with that
    scope (the card's cycle), and its cohort is the scope's active reset.
    ``None`` keeps the merged contract byte-for-byte apart from the
    unavailable-join stamp component.
    """
    import _cctally_account
    decorated = _cctally_account.provider_is_decorated(conn, "claude")
    tz = _resolve_display_tz()
    if account_key is None:
        refs = _navigable_claude_refs(conn)
        current_key = _current_claude_week_key(conn)
        current_refs = [r for r in refs if r.key == current_key]
        current_ref = max(
            current_refs,
            key=lambda r: r.week_start_at or "",
            default=None,
        )
        current_cycle_key = (
            _claude_cycle_key(current_ref) if current_ref else None)
        unavailable_rows = _unavailable_join_rows(conn)
        revision_scopes = _ClaudeRevisionScopes(conn)
        entries = [
            _index_entry(conn, ref, current_cycle_key, tz,
                         unavailable_rows=unavailable_rows,
                         revision_scopes=revision_scopes, decorated=decorated)
            for ref in refs
        ]
    else:
        refs, current_ref, scope = _scoped_refs(
            conn, account_key, cycle_scope, now_utc)
        current_cycle_key = (
            _claude_cycle_key(current_ref) if current_ref else None)
        unavailable_rows = _unavailable_join_rows(
            conn, account_key=account_key)
        revision_scopes = _ClaudeRevisionScopes(conn, account_key=account_key)
        entries = [
            _index_entry(
                conn, ref, current_cycle_key, tz,
                unavailable_rows=unavailable_rows,
                revision_scopes=revision_scopes, decorated=decorated,
                account_key=account_key,
                cohort_id=(scope.reset_event_id
                           if ref is current_ref else None),
            )
            for ref in refs
        ]
    entries.sort(key=lambda e: e["start_at_utc"] or "", reverse=True)
    return entries


# ── Claude week detail (spec §1b) ──────────────────────────────────────


def _shape_weekly_milestone(row, *, withheld: bool = False) -> dict:
    """Reshape a ``percent_milestones`` row to the envelope
    ``current_week.milestones`` wire shape (byte-parallel with
    ``_cctally_dashboard_envelope.snapshot_to_envelope``).

    ``withheld`` (#750 S4 §5.1) says the classifier identified this row's null
    marginal as an observation gap rather than an absent one. The shaper
    emitted five fields and dropped the cause, so the modal rendered a bare em
    dash for a figure the CLI names. The key is snake_case to match its
    neighbours on this wire shape, which is one of the frozen legacy snake
    surfaces `docs/cli-contract.md` describes; the CLI's own JSON keeps its
    camelCase `marginalCostWithheldCause`.

    The field appears ONLY on a withheld row, so every existing payload byte
    is unchanged.
    """
    marginal = row["marginal_cost_usd"]
    fh = row["five_hour_percent_at_crossing"]
    shaped = {
        "percent": int(row["percent_threshold"]),
        "crossed_at_utc": _to_iso_z(row["captured_at_utc"]),
        "cumulative_usd": round(float(row["cumulative_cost_usd"]), 4),
        "marginal_usd": None if marginal is None else round(float(marginal), 4),
        "five_hour_pct_at_cross": None if fh is None else float(fh),
    }
    if withheld and marginal is None:
        shaped["marginal_usd_withheld_cause"] = OBSERVATION_GAP_CAUSE
    return shaped


def _shape_observation_gap_runs(disclosure, rows) -> list:
    """The classified runs, as the additive week-detail wire member.

    #750 S4 §5.2. The per-row cause alone cannot state a run's first threshold
    or the instant of the previous crossing, which are what the note above the
    table is about, so the runs travel as structured data and the CLIENT
    composes the sentence through `lib/fmt.ts`. Shipping a pre-rendered
    sentence would put a presentation decision inside a data contract and
    bypass the browser's display-timezone chokepoint, so the viewer's zone
    would come from the server's idea of it.

    Ordered ascending by first threshold. Absent or empty means no gap.
    """
    out: list = []
    for run in sorted(disclosure.runs, key=lambda r: r.first_threshold):
        out.append({
            "first_percent": int(run.first_threshold),
            "last_percent": int(run.last_threshold),
            "observed_at_utc": _to_iso_z(run.captured_at_utc),
            # None when the run opens the ladder and has no predecessor.
            "previous_crossed_at_utc": (
                _to_iso_z(run.previous_captured_at_utc)
                if run.previous_captured_at_utc else None
            ),
        })
    return out


def _load_block_credits(
    conn: sqlite3.Connection, window_key: int, account_key: str,
) -> list:
    """5h in-place credit rows for a block, ascending by effective time —
    same wire shape as the envelope's ``five_hour_block.credits``."""
    rows = conn.execute(
        "SELECT effective_reset_at_utc, prior_percent, post_percent "
        "FROM five_hour_reset_events "
        "WHERE five_hour_window_key = ? AND account_key = ? "
        "ORDER BY effective_reset_at_utc ASC",
        (int(window_key), account_key),
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


def _build_blocks(conn: sqlite3.Connection, start_iso, end_iso, *,
                  account_key=None) -> list:
    """``five_hour_blocks`` intersecting ``[start, end)``, ascending by start;
    each carries its 5h milestone stream + credit rows (spec §1b). A block
    straddling a week boundary appears in every week it intersects.
    ``account_key`` (#836 spec 5.5): only that account's blocks."""
    if not start_iso or not end_iso:
        return []
    acct_sql, acct_params = _account_predicate(account_key, "account_key")
    c = _cctally()
    import _cctally_account
    decorated = _cctally_account.provider_is_decorated(conn, "claude")
    # #834 S1 (#836): `id` is selected so each block's milestones are loaded by
    # the PRECISE selector. This route used to select blocks account-blind and
    # then pass a bare `five_hour_window_key` to the milestone read, while block
    # uniqueness is `(account_key, five_hour_window_key)` — so one physical
    # window's per-account blocks each rendered EVERY account's milestones. `id`
    # stays internal: it is not added to the published `blocks[]` shape.
    rows = conn.execute(
        "SELECT id, account_key, five_hour_window_key, block_start_at, five_hour_resets_at, "
        "       final_five_hour_percent, total_cost_usd, "
        "       crossed_seven_day_reset, is_closed "
        "FROM five_hour_blocks "
        "WHERE unixepoch(block_start_at) < unixepoch(?) "
        "  AND unixepoch(five_hour_resets_at) > unixepoch(?)" + acct_sql + " "
        "ORDER BY unixepoch(block_start_at) ASC, five_hour_window_key ASC, id ASC",
        (end_iso, start_iso) + acct_params,
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
                "milestones": c._tui_build_five_hour_milestones(
                    conn, wk, block_id=int(b["id"])),
                "credits": _load_block_credits(conn, wk, b["account_key"]),
                **({
                    "account_key": b["account_key"],
                    "account_label": _cctally_account.display_account_label(
                        conn, b["account_key"]),
                } if decorated else {}),
            }
        )
    return out


def build_claude_week_detail(conn: sqlite3.Connection, key: str, *,
                             account_key=None, now_utc=None) -> "dict | None":
    """Complete payload for one reset-defined Claude cycle.

    ``account_key`` (#836 spec 5.5/5.6, U1) serves that account's cycle from
    its own scoped index: the account's current cycle scope is resolved at
    ``now_utc`` (the route's as-of), exactly as the dashboard resolves it for
    the card, so the current key's bounds here equal the card's. A key absent
    from the account's scoped index is ``None``; it is never retried merged.
    """
    if account_key is None:
        refs = _navigable_claude_refs(conn)
        ref = next((candidate for candidate in refs
                    if _claude_cycle_key(candidate) == key), None)
        if ref is None:
            return None
        entry = next(e for e in build_claude_week_index(conn) if e["key"] == key)
        cohort_id = None
    else:
        if now_utc is None:
            now_utc = dt.datetime.now(UTC)
        cycle_scope = _cctally()._claude_current_cycle_scope(
            conn, now_utc, account_key=account_key)
        refs, current_ref, scope = _scoped_refs(
            conn, account_key, cycle_scope, now_utc)
        ref = next((candidate for candidate in refs
                    if _claude_cycle_key(candidate) == key), None)
        if ref is None:
            return None
        entry = next(
            (e for e in build_claude_week_index(
                conn, account_key=account_key, cycle_scope=scope,
                now_utc=now_utc)
             if e["key"] == key), None)
        if entry is None:
            return None
        cohort_id = scope.reset_event_id if ref is current_ref else None
    rows = _claude_cycle_rows(
        conn, ref, account_key=account_key, cohort_id=cohort_id)
    # Same absent-boundary case `_claude_cycle_key` handles, and this call runs
    # unconditionally, ahead of the `if rows` guard below.
    segment_key = dashboard_resource_key(
        "milestone_segment", "claude", ref.key,
        ref.week_start_at or None, ref.week_end_at or None,
    )
    # #750 S4 §5.1: classify BEFORE shaping, through the existing pure kernel,
    # so the shaper can name a null marginal's cause instead of dropping it.
    disclosure = classify_observation_gaps(rows)
    segments = ([{"key": segment_key,
                  "milestones": [
                      _shape_weekly_milestone(
                          r, withheld=i in disclosure.withheld_indexes)
                      for i, r in enumerate(rows)
                  ]}]
                if rows else [])
    gap_runs = _shape_observation_gap_runs(disclosure, rows) if rows else []
    blocks = _build_blocks(conn, entry["start_at_utc"], entry["end_at_utc"],
                           account_key=account_key)

    return {
        "source": "claude",
        "key": entry["key"],
        "start_at_utc": entry["start_at_utc"],
        "end_at_utc": entry["end_at_utc"],
        "label": entry["label"],
        "is_current": entry["is_current"],
        "detail_stamp": entry["detail_stamp"],
        "segments": segments,
        # #750 S4 §5.2: additive and nullable. Absent or empty means no gap.
        "observation_gap_runs": gap_runs,
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
    # BEFORE the SQL, per #496 S5b section 4.7.
    assert_projection_readable(stats_conn)
    # #901 (I1/G1, Amendment 1 item 2, 1b and 1c): stream every weekly block
    # in reset order through an epoch-1017 partial expression index. Unpinned,
    # the planner seeks a root-leading index and sorts the whole weekly
    # history through a temp b-tree (measured for every root/account/orphan
    # variant).
    #
    # The ORDER BY names the tie order explicitly: `_canonicalize_codex_
    # cluster` keeps the first maximal member, so rows tied on one reset
    # second must arrive in the order the pre-#901 sorter received them, and
    # that order came from the legacy plan, which differs by root count
    # (Amendment 1c, measured on SQLite 3.53.4). With two or more roots the
    # legacy plan scanned `idx_quota_blocks_active`, whose order includes
    # orphaned_at; with exactly one it seeked the UNIQUE autoindex, which has
    # no orphaned_at (its one exception, an account-scoped live-only read, has
    # a constant orphaned_at, so the two orders agree there). The branch
    # counts the NORMALIZED roots above, so duplicate or empty arguments take
    # the single-root order, exactly as they took the single-root plan.
    #
    # Each branch pins its own index only when that index exists; on an
    # older index the statement stays unpinned with ordinary predicates and
    # the same explicit order. Under the pin, the root, account and orphan
    # predicates carry a unary `+`: SQLite treats a column that a WHERE term
    # fixes but the scan does not seek as satisfying its ORDER BY term and
    # then cannot match that index column, so the later ORDER BY terms fell
    # back to a tie-run sorter ("USE TEMP B-TREE FOR LAST n TERMS OF ORDER
    # BY", measured). Limitation: a unary `+` keeps the operand's value but
    # removes its column affinity. That is equivalent here ONLY because these
    # comparisons are TEXT against TEXT: the root keys are filtered to `str`
    # above, `account_key` is a TEXT key bound as a string, and `IS NULL`
    # involves no affinity. It is not an equivalence for numeric parameters or
    # any other affinity-dependent comparison; a caller binding a non-string
    # account key would need the unpinned predicate.
    if len(roots) == 1:
        order_index = "idx_quota_blocks_weekly_single_root_order"
        order_by = (
            "ORDER BY unixepoch(resets_at_utc) DESC, source_root_key ASC, "
            "account_key ASC, logical_limit_key ASC, observed_slot ASC, "
            "resets_at_utc ASC")
    else:
        order_index = "idx_quota_blocks_weekly_reset_order"
        order_by = (
            "ORDER BY unixepoch(resets_at_utc) DESC, source_root_key ASC, "
            "account_key ASC, orphaned_at ASC, logical_limit_key ASC, "
            "observed_slot ASC, resets_at_utc ASC")
    pinned = stats_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (order_index,),
    ).fetchone() is not None
    order_hint = f"INDEXED BY {order_index} " if pinned else ""
    plus = "+" if pinned else ""
    orphan_clause = (
        "" if include_orphaned else f"AND {plus}orphaned_at IS NULL ")
    # `quota_window_blocks.account_key` is NOT NULL DEFAULT 'unattributed'.
    account_clause = (
        "" if account_key is None else f"AND {plus}account_key=? ")
    account_params = () if account_key is None else (account_key,)
    rows = stats_conn.execute(
        "SELECT source_root_key, logical_limit_key, observed_slot, "
        "       window_minutes, limit_id, limit_name, account_key, "
        "       resets_at_utc, nominal_start_at_utc, current_percent "
        "FROM quota_window_blocks "
        f"{order_hint}"
        "WHERE source='codex' AND window_minutes=10080 "
        f"{orphan_clause}"
        f"{account_clause}"
        f"AND {plus}source_root_key IN ({placeholders}) "
        f"{order_by}",
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


def _codex_five_hour_rows(stats_conn, cyc, *, include_orphaned=False) -> list:
    """Every retained 5h block on the selected root intersecting [start, end).

    The account filter admits the cycle's own key OR the ``unattributed``
    sentinel, and deliberately NOT strict equality. Unlike
    ``_codex_cycle_raw_rows`` — whose block and milestone parameters both come
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

    #901 Amendment 19 T1 (spec I1): the rows are returned whole, so they are
    ordered here, where ``ORDER BY unixepoch(nominal_start_at_utc)`` sorted
    them through a temp b-tree. Each physical 5h block surfaces as many
    jittered rows, so one weekly cycle is not a bound. The key is SQLite's own
    ``unixepoch()`` of each distinct start spelling (a table-free scalar
    query, memoized), so it is the old key exactly, and the rows keep the
    caller's row factory. ``list.sort`` is stable over the same scan SQLite's
    in-memory sorter consumed, so rows tied on one start second keep their
    order. The WHERE clause admits no NULL start.
    """
    orphan_clause = "" if include_orphaned else "AND orphaned_at IS NULL "
    assert_projection_readable(stats_conn)
    rows = stats_conn.execute(
        "SELECT source_root_key, logical_limit_key, observed_slot, "
        "       window_minutes, limit_id, limit_name, account_key, "
        "       resets_at_utc, nominal_start_at_utc, current_percent "
        "FROM quota_window_blocks "
        "WHERE source='codex' AND window_minutes=300 "
        f"{orphan_clause}"
        "AND source_root_key=? AND account_key IN (?,?) "
        "AND unixepoch(nominal_start_at_utc) < unixepoch(?) "
        "AND unixepoch(resets_at_utc) > unixepoch(?)",
        (cyc.root, cyc.account_key, UNATTRIBUTED,
         cyc.end.astimezone(UTC).isoformat(),
         cyc.start.astimezone(UTC).isoformat()),
    ).fetchall()
    epochs: dict = {}

    def start_epoch(row):
        text = row[8]
        if text not in epochs:
            epochs[text] = stats_conn.execute(
                "SELECT unixepoch(?)", (text,)).fetchone()[0]
        return epochs[text]

    rows.sort(key=start_epoch)
    return rows


def _codex_five_hour_clusters(stats_conn, cyc, *, include_orphaned=False,
                              rows=None) -> list:
    """Retained 5h blocks intersecting the cycle, jitter-canonicalized.

    The 300-minute rows carry the same second-level ``resets_at`` jitter as the
    weekly rows (one physical 5h block surfaces as many rows), so they are
    clustered the same way — one canonical block per physical reset — before
    counting/rendering. Returns cluster-representative ``_CodexCycle`` blocks
    (``.members`` set), ordered by ascending start.

    ``rows`` (#1050) clusters rows the caller already read with
    ``_codex_five_hour_rows`` — `_codex_cycle_inputs`' block rows — in that
    function's own order, so the stamp and the detail enumerate one population.
    """
    if rows is None:
        rows = _codex_five_hour_rows(
            stats_conn, cyc, include_orphaned=include_orphaned)
    blocks: list = []
    for row in rows:
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


# ── #1050 spec 4.7: every input of a Codex cycle detail ─────────────────────
#
# The Codex cycle detail publishes block `current_percent`, the cycle ladder
# and every included block's ladder (crossings, high-water percent, physical
# boundaries), and per-milestone dollars and tokens priced at read time from
# `codex_session_entries` at the resolved speed. Its stamp named none of those
# values, so a cached detail survived every in-place rewrite of them. One
# helper reads all of them, and both the index entry (the stamp) and the
# detail route consume it.

#: Every `quota_percent_milestones` column a Codex detail reads, its unique key
#: (`source, source_root_key, account_key, logical_limit_key, observed_slot,
#: window_minutes, resets_at_utc, percent_threshold`) first, so the rows sort
#: by that key as plain tuples (every key column is NOT NULL).
_CODEX_MILESTONE_INPUT_COLUMNS = (
    "source, source_root_key, account_key, logical_limit_key, observed_slot, "
    "window_minutes, resets_at_utc, percent_threshold, captured_at_utc, "
    "source_path, line_offset, high_water_percent"
)
_CODEX_MILESTONE_KEY_WIDTH = 8


class CodexCycleInputs(NamedTuple):
    """Everything one Codex cycle detail's published values depend on.

    * ``cycle_rows`` — the cycle's raw qualifying ``quota_percent_milestones``
      rows across its jitter-member resets (the stamp's distinct-threshold count
      and raw maximum capture come from these), ordered by unique key.
    * ``block_ladder_rows`` — per selection the detail prices (the cycle's own
      members, then every included five-hour block cluster's members): the
      ladder rows ``codex_quota_breakdown`` reads for that member, by unique key.
    * ``block_rows`` — the five-hour block rows ``_codex_five_hour_rows``
      returns, by unique key.
    * ``first_boundaries`` — per priced member, the physical
      ``(captured_at_utc, source_path, line_offset)`` the breakdown opens from,
      ``"absent"`` when it has none, ``"unread"`` when the member has no ladder.
    * ``selection_aggregates`` — per priced member with a boundary, the
      aggregate twin of its accounting read.
    * ``accounting_seq``, ``speed`` (resolved), ``pricing_identity``.
    * ``block_clusters`` — derived from the block rows in their read order, the
      clusters the detail renders; excluded from the content hash.
    """

    cycle_rows: tuple
    block_ladder_rows: tuple
    block_rows: tuple
    first_boundaries: tuple
    selection_aggregates: tuple
    accounting_seq: "int | None"
    speed: str
    pricing_identity: str
    block_clusters: tuple


def _canonical_digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _member_ident(member) -> tuple:
    return (member.root, member.account_key, member.limit, member.slot,
            member.window, member.reset_iso)


def _codex_cycle_raw_rows(stats_conn, cyc) -> tuple:
    """The cycle's raw qualifying milestone rows: the population the stamp's
    count and maximum capture have always covered, now read whole."""
    resets = cyc.member_reset_isos
    placeholders = ",".join("unixepoch(?)" for _ in resets)
    rows = stats_conn.execute(
        f"SELECT {_CODEX_MILESTONE_INPUT_COLUMNS} "
        "FROM quota_percent_milestones "
        "WHERE source='codex' AND source_root_key=? AND account_key=? "
        "  AND logical_limit_key=? "
        "  AND observed_slot=? AND window_minutes=? "
        f"  AND unixepoch(resets_at_utc) IN ({placeholders}) AND orphaned_at IS NULL",
        (cyc.root, cyc.account_key, cyc.limit, cyc.slot, cyc.window, *resets),
    ).fetchall()
    return tuple(sorted(tuple(r) for r in rows))


def _codex_member_ladder_rows(stats_conn, member) -> tuple:
    """One member's ladder exactly as ``_cctally_quota._load_active_milestones``
    selects it (text-equal reset, the member's own account), every column."""
    rows = stats_conn.execute(
        f"SELECT {_CODEX_MILESTONE_INPUT_COLUMNS} "
        "FROM quota_percent_milestones "
        "WHERE source='codex' AND source_root_key=? AND account_key=? "
        "  AND logical_limit_key=? AND observed_slot=? AND window_minutes=? "
        "  AND resets_at_utc=? AND orphaned_at IS NULL",
        (member.root, member.account_key, member.limit, member.slot,
         member.window, member.reset_iso),
    ).fetchall()
    return tuple(sorted(tuple(r) for r in rows))


def _codex_block_row_key(row) -> tuple:
    # (source_root_key, account_key, logical_limit_key, observed_slot,
    #  window_minutes, resets_at_utc): the unique key minus the constant source.
    return (row[0], row[6], row[1], row[2], row[3], row[7])


def _codex_cache_file_identity(cache_conn) -> "tuple | None":
    try:
        rows = cache_conn.execute("PRAGMA database_list").fetchall()
        path = next(str(r[2]) for r in rows if str(r[1]) == "main" and r[2])
        stat = os.stat(path)
    except (OSError, StopIteration, sqlite3.Error, TypeError, ValueError):
        return None
    return (stat.st_dev, stat.st_ino)


def _codex_accounting_seq(cache_conn) -> "int | None":
    try:
        row = cache_conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_accounting_mutation_seq'").fetchone()
    except sqlite3.Error:
        return None
    try:
        return None if row is None else int(row[0])
    except (TypeError, ValueError):
        return None


def _codex_quota_log_seq(cache_conn) -> "int | None":
    try:
        row = cache_conn.execute(
            "SELECT seq FROM sqlite_sequence "
            "WHERE name='quota_window_change_log'").fetchone()
    except sqlite3.Error:
        return None
    return 0 if row is None else int(row[0])


def _codex_snapshot_seq(cache_conn) -> "int | None":
    try:
        row = cache_conn.execute(
            "SELECT seq FROM sqlite_sequence "
            "WHERE name='quota_window_snapshots'").fetchone()
    except sqlite3.Error:
        return None
    return 0 if row is None else int(row[0])


def _reset_epoch(text) -> "int | None":
    parsed = _parse_utc(text)
    return None if parsed is None else math.floor(parsed.timestamp())


def _codex_boundary_order_key(row) -> tuple:
    """``_first_block_physical_tuple``'s ``ORDER BY unixepoch(captured_at_utc),
    unixepoch(resets_at_utc), source_path, line_offset`` over a bulk row
    ``(reset_epoch, captured_epoch, captured_text, path, offset, account)``:
    SQLite sorts a NULL ``unixepoch`` first."""
    return (row[1] is not None, row[1] or 0, row[0], row[3], row[4])


def _codex_fold_boundary_row(out: dict, row: tuple) -> None:
    """Fold one snapshot ``(reset_epoch, captured_epoch, captured_text, path,
    offset, account)`` into its reset's per-flavour minimum (see
    `_codex_bulk_first_boundaries`). A minimum over a superset is the minimum
    of the old minimum and the new rows, so folding is also how appended rows
    join a memoized identity."""
    slots = out.get(row[0])
    if slots is None:
        slots = out[row[0]] = [None, None, {}]
    key = _codex_boundary_order_key(row)
    if slots[0] is None or key < slots[0][0]:
        slots[0] = (key, row)
    account = row[5]
    if account is None or account == UNATTRIBUTED:
        if slots[1] is None or key < slots[1][0]:
            slots[1] = (key, row)
    else:
        prior = slots[2].get(account)
        if prior is None or key < prior[0]:
            slots[2][account] = (key, row)


def _codex_bulk_first_boundaries(cache_conn, identity_parts) -> dict:
    """Every first boundary of one raw quota identity, in one pass (#1050).

    ``_first_block_physical_tuple`` cannot seek its own predicate:
    ``unixepoch(resets_at_utc)=unixepoch(?)`` matches no index expression, so
    each call scans the identity's whole snapshot history (11-70 ms on the
    #997 store), and the index needs one per priced member. This is that
    query's exact twin over every reset at once: per reset second, the
    minimum row in the same order for each account-predicate flavour the
    boundary read takes (``_codex_cache_account_predicate(...,
    admit_unattributed=True)``): every row (no scope), the unattributed rows
    (``NULL`` or the sentinel), and each real account's own rows — a real
    scope admits its own rows plus the unattributed ones.
    """
    root, limit, slot, window = identity_parts
    out: dict = {}
    for row in cache_conn.execute(
            "SELECT unixepoch(resets_at_utc), unixepoch(captured_at_utc), "
            "       captured_at_utc, source_path, line_offset, account_key "
            "FROM quota_window_snapshots "
            "WHERE source='codex' AND source_root_key=? "
            "  AND logical_limit_key=? AND observed_slot=? "
            "  AND window_minutes=? AND unixepoch(resets_at_utc) IS NOT NULL",
            (root, limit, slot, window)):
        _codex_fold_boundary_row(out, tuple(row))
    return out


def _codex_boundary_from_bulk(slots, account_scope):
    """One member's boundary from its reset's bulk slots, parsed exactly as
    ``_first_block_physical_tuple`` parses its row (a row that does not parse
    is no boundary)."""
    if slots is None:
        return None
    if account_scope is None:
        best = slots[0]
    elif account_scope == UNATTRIBUTED:
        best = slots[1]
    else:
        candidates = [c for c in (slots[2].get(account_scope), slots[1])
                      if c is not None]
        best = min(candidates, key=lambda c: c[0]) if candidates else None
    if best is None:
        return None
    row = best[1]
    try:
        return (_quota._parse_utc(str(row[2]), "captured_at_utc"),
                str(row[3]), int(row[4]))
    except (TypeError, ValueError):
        return None


class _CodexCycleInputsMemo:
    """Process memo of the Codex cycle-index stamps' expensive inputs (#1050).

    The index stamps every retained cycle, and the pricing inputs are
    per-member reads over `quota_window_snapshots` and `codex_session_entries`.
    Read one by one they cost seconds per index build, so the source build
    hands the index this memo. Only the index uses it: the detail route always
    reads fresh, through the breakdown's own boundary read, so
    `test_1050_codex_detail_stamp_equals_index_stamp` checks the memo against
    a fresh read.

    * **First boundaries** come from one bulk pass per raw identity
      (`_codex_bulk_first_boundaries`), kept while the cache file is the same
      and every Codex mutation since is an append. The triggers on
      `quota_window_snapshots` append one row to the AUTOINCREMENT
      `quota_window_change_log` for every Codex insert, delete and semantic
      update, and neither sequence is ever reused, so when the ledger's
      high-water moved by exactly the number of Codex snapshot rows now present
      above the snapshot high-water last seen, every change was an insert of
      one of those rows (a delete or update adds a ledger row without adding
      such a row) and they fold in (`_codex_fold_boundary_row`). Anything else
      re-reads the boundaries. The projector prunes the ledger, which leaves
      its high-water where it is, so this never reads ledger rows.
    * **Entry aggregates** are kept per selection and invalidated by
      `codex_accounting_change_log`, as
      `_lib_snapshot_cache.build_cached_codex_accounting` already trusts it: a
      selection drops when a changed path's old or new timestamp extent
      intersects it. A replaced file, a counter that went backwards, a ledger
      gap, a `full` change or too many dirty paths go cold.
    * **Per-cycle results** are keyed on the caller's `codex_stats_digest`,
      which (with the two #1050 relations) covers every stats column the helper
      reads, plus the cycle's own selection; an entry is reused only while
      every boundary and aggregate it was built from is unchanged. One stats
      generation is kept at a time.
    """

    _MAX_DIRTY_PATHS = 512
    _MAX_ENTRIES = 8192

    def __init__(self):
        self.lock = threading.Lock()
        self.cache_ident = None
        self.accounting_seq = None
        self.quota_log_seq = None
        self.snapshot_seq = None
        self.path_extents: dict = {}
        self.bulk_boundaries: dict = {}
        self.aggregates: dict = {}
        self.models = None
        self.stats_key = None
        self.cycles: dict = {}
        self.usable = False

    def _cold(self):
        self.path_extents = {}
        self.bulk_boundaries = {}
        self.aggregates = {}
        self.models = None
        self.cycles = {}

    def sync(self, cache_conn, stats_key) -> None:
        """Bring the memo to ``cache_conn``'s state before an index build."""
        ident = _codex_cache_file_identity(cache_conn)
        seq = _codex_accounting_seq(cache_conn)
        log_seq = _codex_quota_log_seq(cache_conn)
        snapshot_seq = _codex_snapshot_seq(cache_conn)
        if stats_key != self.stats_key:
            self.cycles = {}
            self.stats_key = stats_key
        if (ident is None or seq is None or log_seq is None
                or snapshot_seq is None):
            self.usable = False
            self.cache_ident = None
            self._cold()
            return
        cold = (ident != self.cache_ident or self.accounting_seq is None
                or seq < self.accounting_seq)
        if not cold and seq > self.accounting_seq:
            cold = not self._apply_accounting_changes(cache_conn, seq)
        if cold:
            self._cold()
            try:
                self.path_extents = {
                    (str(root or ""), str(path)): (lo, hi)
                    for root, path, lo, hi in cache_conn.execute(
                        "SELECT source_root_key, source_path, "
                        "       MIN(timestamp_utc), MAX(timestamp_utc) "
                        "FROM codex_session_entries "
                        "GROUP BY source_root_key, source_path")
                }
            except sqlite3.Error:
                self.usable = False
                self.cache_ident = None
                return
        elif ((log_seq, snapshot_seq)
              != (self.quota_log_seq, self.snapshot_seq)
              and not self._apply_quota_appends(
                  cache_conn, log_seq, snapshot_seq)):
            self.bulk_boundaries = {}
        self.cache_ident = ident
        self.accounting_seq = seq
        self.quota_log_seq = log_seq
        self.snapshot_seq = snapshot_seq
        self.usable = True
        for store in (self.aggregates, self.cycles):
            if len(store) > self._MAX_ENTRIES:
                store.clear()

    def _apply_quota_appends(self, cache_conn, log_seq, snapshot_seq) -> bool:
        """Fold appended snapshots into the memoized boundaries, or ``False``
        when the change since the last sync is not provably append-only."""
        if (self.quota_log_seq is None or self.snapshot_seq is None
                or log_seq < self.quota_log_seq
                or snapshot_seq < self.snapshot_seq):
            return False
        expected = log_seq - self.quota_log_seq
        if expected > self._MAX_DIRTY_PATHS * 8:
            return False
        try:
            rows = tuple(cache_conn.execute(
                "SELECT source_root_key, logical_limit_key, observed_slot, "
                "       window_minutes, unixepoch(resets_at_utc), "
                "       unixepoch(captured_at_utc), captured_at_utc, "
                "       source_path, line_offset, account_key "
                "FROM quota_window_snapshots "
                "WHERE id > ? AND id <= ? AND source='codex'",
                (self.snapshot_seq, snapshot_seq)))
        except sqlite3.Error:
            return False
        if len(rows) != expected:
            return False
        for row in rows:
            bulk = self.bulk_boundaries.get(tuple(row[:4]))
            if bulk is not None and row[4] is not None:
                _codex_fold_boundary_row(bulk, tuple(row[4:]))
        return True

    def _apply_accounting_changes(self, cache_conn, seq) -> bool:
        last = self.accounting_seq
        try:
            changes = tuple(cache_conn.execute(
                "SELECT mutation_seq, change_kind, source_root_key, source_path "
                "FROM codex_accounting_change_log "
                "WHERE mutation_seq > ? ORDER BY mutation_seq, seq", (last,)))
        except sqlite3.Error:
            return False
        if (not changes or int(changes[0][0]) > last + 1
                or int(changes[-1][0]) != seq
                or any(row[1] == "full" for row in changes)):
            return False
        dirty = {(str(row[2] or ""), str(row[3]))
                 for row in changes if row[1] == "path" and row[3]}
        if len(dirty) > self._MAX_DIRTY_PATHS:
            return False
        intervals: dict = {}
        try:
            for root, path in dirty:
                old = self.path_extents.pop((root, path), None)
                new = cache_conn.execute(
                    "SELECT MIN(timestamp_utc), MAX(timestamp_utc) "
                    "FROM codex_session_entries "
                    "WHERE source_root_key=? AND source_path=?",
                    (root, path)).fetchone()
                if new is not None and new[0] is not None:
                    self.path_extents[(root, path)] = (new[0], new[1])
                    intervals.setdefault(root, []).append((new[0], new[1]))
                if old is not None:
                    intervals.setdefault(root, []).append(old)
        except sqlite3.Error:
            return False
        for key in tuple(self.aggregates):
            root, lo, hi = key[0], key[1], key[2]
            if any(a <= hi and b >= lo for a, b in intervals.get(root, ())):
                del self.aggregates[key]
        self.models = None
        return True

    def boundary(self, cache_conn, parts, reset_epoch, account_scope):
        bulk = self.bulk_boundaries.get(parts)
        if bulk is None:
            bulk = self.bulk_boundaries[parts] = _codex_bulk_first_boundaries(
                cache_conn, parts)
        return _codex_boundary_from_bulk(bulk.get(reset_epoch), account_scope)

    def entry_models(self, cache_conn) -> tuple:
        if self.models is None:
            self.models = _quota.codex_entry_models(cache_conn)
        return self.models


_CODEX_CYCLE_INPUTS_MEMO = _CodexCycleInputsMemo()


def _codex_first_boundary(cache_conn, member, account_scope, memo, used):
    """The physical tuple ``codex_quota_breakdown`` opens ``member``'s ladder
    from: the breakdown's own read, or with the index memo its bulk twin."""
    if memo is not None:
        key = ((member.root, member.limit, member.slot, member.window),
               math.floor(member.reset.timestamp()), account_scope)
        value = memo.boundary(cache_conn, *key)
        used.append(("b", key, value))
        return value
    ident = QuotaWindowIdentity(
        source="codex", source_root_key=member.root,
        logical_limit_key=member.limit, observed_slot=member.slot,
        window_minutes=member.window, account_key=member.account_key,
        limit_id=member.limit_id, limit_name=member.limit_name,
    )
    return _quota._first_block_physical_tuple(
        ident, member.reset, cache_conn=cache_conn, account_key=account_scope)


def _codex_selection_aggregate(cache_conn, member, query_start, query_end,
                               account_scope, memo, used, models_box):
    key = (member.root, query_start, query_end, account_scope)
    if memo is not None and key in memo.aggregates:
        value = memo.aggregates[key]
        used.append(("a", key, value))
        return value
    if memo is not None:
        models = memo.entry_models(cache_conn)
    else:
        # The model list is read once per helper call, not per selection.
        if not models_box:
            models_box.append(_quota.codex_entry_models(cache_conn))
        models = models_box[0]
    value = _quota.codex_entry_selection_aggregate(
        cache_conn, source_root_key=member.root, query_start=query_start,
        query_end=query_end, account_key=account_scope, models=models)
    if memo is not None:
        memo.aggregates[key] = value
        used.append(("a", key, value))
    return value


def _codex_cycle_inputs(stats_conn, cache_conn, cyc, *, speed: str,
                        account_scope: "str | None",
                        _memo=None, _used=None) -> CodexCycleInputs:
    """Read every input of ``cyc``'s Codex detail (#1050 spec 4.7).

    ``account_scope`` is the route's or the index's account focus — the scope
    the two cache reads use (``_union_cluster_milestones``) — never
    ``cyc.account_key``. ``speed`` is resolved here exactly as the breakdown
    resolves it, so the stamp records the tier the dollars were priced at.

    ``cache_conn=None`` opens the breakdown's own read-only cache connection,
    exactly as ``codex_quota_breakdown`` does for such a caller; a missing
    cache reads every boundary as absent.

    The reads keep the projection-gate classification of the reads they
    replace: the block rows still come from the gated
    ``_codex_five_hour_rows``, and ``quota_percent_milestones`` is not a
    projection family.
    """
    used = _used if _used is not None else []
    cycle_rows = _codex_cycle_raw_rows(stats_conn, cyc)
    raw_block_rows = _codex_five_hour_rows(stats_conn, cyc)
    clusters = tuple(_codex_five_hour_clusters(
        stats_conn, cyc, rows=raw_block_rows))
    selections = [(cyc.block_key, cyc)] + [(b.block_key, b) for b in clusters]
    owns_cache = cache_conn is None
    cache = cache_conn
    if owns_cache:
        try:
            cache = _quota._cache_connection()
        except (FileNotFoundError, sqlite3.Error):
            cache = None
    ladders: list = []
    boundaries: list = []
    aggregates: list = []
    models_box: list = []
    try:
        for selection_key, cluster in selections:
            for member in cluster.cluster_members:
                ident = list(_member_ident(member))
                rows = _codex_member_ladder_rows(stats_conn, member)
                ladders.append([selection_key, ident, [list(r) for r in rows]])
                if not rows:
                    boundaries.append([selection_key, ident, "unread"])
                    continue
                start = (None if cache is None else _codex_first_boundary(
                    cache, member, account_scope, _memo, used))
                if start is None:
                    boundaries.append([selection_key, ident, "absent"])
                    continue
                boundaries.append([selection_key, ident, [
                    start[0].astimezone(UTC).isoformat(), start[1], start[2]]])
                # The breakdown's ladder is ordered by threshold; its last row
                # bounds the accounting read.
                last = max(rows, key=lambda r: r[7])
                query_start, query_end = _quota.codex_entry_selection_bounds(
                    start[0], last[8])
                count, max_rowid, per_model = _codex_selection_aggregate(
                    cache, member, query_start, query_end, account_scope,
                    _memo, used, models_box)
                aggregates.append([selection_key, ident, query_start,
                                   query_end, count, max_rowid,
                                   [list(m) for m in per_model]])
        accounting_seq = (None if cache is None
                          else _codex_accounting_seq(cache))
    finally:
        if owns_cache and cache is not None:
            cache.close()
    return CodexCycleInputs(
        cycle_rows=cycle_rows,
        block_ladder_rows=tuple(ladders),
        block_rows=tuple(sorted(
            (tuple(r) for r in raw_block_rows), key=_codex_block_row_key)),
        first_boundaries=tuple(boundaries),
        selection_aggregates=tuple(aggregates),
        accounting_seq=accounting_seq,
        speed=_cctally()._resolve_codex_speed(speed),
        pricing_identity=_lib_pricing.live_codex_pricing_identity(),
        block_clusters=clusters,
    )


def _codex_inputs_stats_digest(inputs: CodexCycleInputs) -> str:
    """The digest of everything but the per-tick scalars, so the index can
    memoize it per cycle and recombine on an accounting move."""
    return _canonical_digest([
        [list(r) for r in inputs.cycle_rows],
        list(inputs.block_ladder_rows),
        [list(r) for r in inputs.block_rows],
        list(inputs.first_boundaries),
        list(inputs.selection_aggregates),
    ])


def _codex_content_component(selection_digest: str, accounting_seq, speed,
                             pricing_identity) -> str:
    """#1050 spec 4.7: the stamp's content component — SHA-256 over the
    canonical JSON of every stats row and cost input, the rows entering
    through their own SHA-256 so an accounting move recombines without
    re-hashing every row."""
    return "i" + _canonical_digest([
        selection_digest, accounting_seq, speed, pricing_identity])[:16]


def _codex_entry_dict(cyc, tz, milestone_count, block_count, max_captured,
                      component) -> dict:
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
            key, milestone_count, block_count, max_captured, component,
        ),
    }


def _codex_stamp_parts(inputs: CodexCycleInputs) -> tuple:
    """``(milestone_count, block_count, max_captured)``: the distinct
    thresholds and the raw maximum capture over the cycle's raw rows
    (independent of the detail's earliest-crossing pick), and the clusters."""
    milestone_count = len({row[7] for row in inputs.cycle_rows})
    max_captured = max((row[8] for row in inputs.cycle_rows), default=None)
    return milestone_count, len(inputs.block_clusters), max_captured


def _codex_cycle_entry(stats_conn, cache_conn, cyc, tz, *, speed,
                       account_scope, inputs=None, memo=None) -> dict:
    """The index entry for ``cyc``, stamped from `_codex_cycle_inputs`.

    ``inputs`` lets the detail stamp from the read it renders from; ``memo``
    is the index's process memo (`_CodexCycleInputsMemo`)."""
    if inputs is not None or memo is None or not memo.usable:
        if inputs is None:
            inputs = _codex_cycle_inputs(
                stats_conn, cache_conn, cyc, speed=speed,
                account_scope=account_scope)
        milestone_count, block_count, max_captured = _codex_stamp_parts(inputs)
        component = _codex_content_component(
            _codex_inputs_stats_digest(inputs), inputs.accounting_seq,
            inputs.speed, inputs.pricing_identity)
        return _codex_entry_dict(cyc, tz, milestone_count, block_count,
                                 max_captured, component)
    cycle_key = (
        account_scope, cyc.key, _iso_z(cyc.start), _iso_z(cyc.end),
        bool(cyc.is_current),
        tuple(_member_ident(m) for m in cyc.cluster_members),
    )
    cached = memo.cycles.get(cycle_key)
    # Both kinds revalidate by VALUE. Presence alone is not enough for an
    # aggregate: two cycles can share one key (a 5-hour block straddling
    # their boundary), and after a late entry evicts it the first cycle
    # recomputes and reinserts it, so the second would find the key present
    # and keep a digest of the old value (#1050 spec 4.7 stamp equality).
    if cached is not None and all(
            (memo.boundary(cache_conn, *key) == value)
            if kind == "b"
            else (key in memo.aggregates and memo.aggregates[key] == value)
            for kind, key, value in cached[4]):
        milestone_count, block_count, max_captured, selection_digest, _ = cached
    else:
        used: list = []
        inputs = _codex_cycle_inputs(
            stats_conn, cache_conn, cyc, speed=speed,
            account_scope=account_scope, _memo=memo, _used=used)
        milestone_count, block_count, max_captured = _codex_stamp_parts(inputs)
        selection_digest = _codex_inputs_stats_digest(inputs)
        memo.cycles[cycle_key] = (milestone_count, block_count, max_captured,
                                  selection_digest, tuple(used))
    component = _codex_content_component(
        selection_digest, memo.accounting_seq,
        _cctally()._resolve_codex_speed(speed),
        _lib_pricing.live_codex_pricing_identity())
    return _codex_entry_dict(cyc, tz, milestone_count, block_count,
                             max_captured, component)

def _codex_cycle_label(cyc, tz) -> str:
    start_iso = cyc.start.astimezone(UTC).isoformat()
    end_iso = cyc.end.astimezone(UTC).isoformat()
    return (
        f"{format_display_dt(start_iso, tz, fmt='%b %d', suffix=False)}"
        f"–"
        f"{format_display_dt(end_iso, tz, fmt='%b %d', suffix=False)}"
    )


def build_codex_cycle_index(stats_conn, *, identity, now_utc,
                            account_key=None, cache_conn=None, speed="auto",
                            stats_digest=None) -> list:
    """Newest-first Codex cycle index over the durable projection (spec §1c,
    §3). Enumerates the hero root's reset-defined ledger with no depth cap;
    one selected full native identity per physical reset.

    ``account_key`` (#416 Slice 3A review B3) builds the index for ONE account,
    so a focused hero renders its own milestone history instead of the first
    account's. ``None`` is the merged parent index.

    ``cache_conn`` and ``speed`` (#1050 spec 4.7) are the cost inputs every
    stamp now names, read exactly as the detail route reads them, with
    ``account_key`` as the account scope; ``cache_conn=None`` opens the
    breakdown's own read-only connection, as the detail does for such a caller.
    ``stats_digest`` is the caller's `codex_stats_digest` of the generation it
    builds from: given together with ``cache_conn`` it admits the process memo
    (`_CodexCycleInputsMemo`), which the source build relies on to keep the
    index's cost near what it was before the stamps carried every input.
    Precondition: the memo keys its per-cycle results on ``stats_digest`` and
    reuses them without re-reading the stats rows they came from, so a caller
    supplying it must guarantee that a result built from rows newer than the
    digest is never published as the digest's. Either ``stats_conn`` reads
    the same pinned snapshot the digest was computed in, or the call sits
    inside a before/after validation that re-reads the digest and discards
    the build on movement. The dashboard source build is the second kind: it
    leaves stats.db in statement-scoped autocommit (#393) and its
    reconciliation re-reads ``codex_stats_digest`` after construction
    (`_cctally_tui._tui_build_source_bundle`, `stats_generation_moved`)."""
    now = now_utc.astimezone(UTC) if now_utc.tzinfo else now_utc.replace(tzinfo=UTC)
    tz = _resolve_display_tz()
    cycles = _load_codex_cycles(
        stats_conn, getattr(identity, "source_root_keys", ()),
        preferred_identity=getattr(identity, "quota_identity", None),
        current_boundary=identity, now_utc=now, account_key=account_key,
    )
    if cache_conn is None or stats_digest is None:
        return [
            _codex_cycle_entry(stats_conn, cache_conn, c, tz, speed=speed,
                               account_scope=account_key)
            for c in cycles
        ]
    memo = _CODEX_CYCLE_INPUTS_MEMO
    with memo.lock:
        memo.sync(cache_conn, stats_digest)
        return [
            _codex_cycle_entry(stats_conn, cache_conn, c, tz, speed=speed,
                               account_scope=account_key, memo=memo)
            for c in cycles
        ]


def reset_codex_cycle_inputs_memo() -> None:
    """Drop the index's process memo (tests and measurements)."""
    global _CODEX_CYCLE_INPUTS_MEMO
    _CODEX_CYCLE_INPUTS_MEMO = _CodexCycleInputsMemo()

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
                        account_key=None, clusters=None) -> "list | None":
    """``clusters`` (#1050): the block clusters `_codex_cycle_inputs` read, so
    the detail renders the population its stamp names."""
    out: list = []
    if clusters is None:
        clusters = _codex_five_hour_clusters(stats_conn, cyc)
    for block in clusters:
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

    # #1050 spec 4.7: ONE read of every input, stamped and rendered from. The
    # ladders' dollars still come from `codex_quota_breakdown`, over the same
    # rows: the route holds one read transaction per database around all of it.
    try:
        inputs = _codex_cycle_inputs(
            stats_conn, cache_conn, match, speed=speed,
            account_scope=account_key)
    except sqlite3.Error:
        return (None, "projection_incoherent")
    milestones = _union_cluster_milestones(
        match, match.block_key, speed, cache_conn, stats_conn,
        account_key=account_key,
    )
    if milestones is None:
        return (None, "projection_incoherent")
    blocks = _codex_cycle_blocks(
        stats_conn, cache_conn, match, speed, account_key=account_key,
        clusters=inputs.block_clusters)
    if blocks is None:
        return (None, "projection_incoherent")

    entry = _codex_cycle_entry(
        stats_conn, cache_conn, match, tz, speed=speed,
        account_scope=account_key, inputs=inputs)
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
