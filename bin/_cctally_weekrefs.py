"""WeekRef / reset-event cluster (impure-glue sibling).

Holds the seven DB-touching WeekRef helpers + the two reset-drop-threshold
constants that drive mid-week reset-event detection:
`_get_canonical_boundary_for_date`, `get_recent_weeks`,
`_apply_reset_events_to_weekrefs`, `_backfill_week_reset_events`,
`_week_ref_has_reset_event`, `_compute_cost_for_weekref`,
`_apply_overlap_clamp_to_weekrefs`, `_RESET_PCT_DROP_THRESHOLD`,
`_FIVE_HOUR_RESET_PCT_DROP_THRESHOLD`.

These operate on the `WeekRef` type and take `sqlite3.Connection` — the
IMPURE counterpart to the PURE `SubWeek` math in `_lib_subscription_weeks.py`
(which owns `_apply_reset_events_to_subweeks` / `_apply_overlap_clamp_to_subweeks`).

Honest *name* imports are KERNEL-ONLY (`_cctally_core`). The three
cctally-ns re-exports this module needs — `_floor_to_hour` (of `_lib_blocks`),
`_clamp_end_ats_to_next_start` (of `_lib_subscription_weeks`), and
`_sum_cost_for_range` (defined in `bin/cctally`) — are reached via the
call-time `c = _cctally()` accessor so test monkeypatches through the
`cctally` namespace are preserved. (No `for c in ...` row-loop in this
cluster → the accessor binds the conventional `c`.)

bin/cctally eager-re-exports all 7 functions + 2 constants; consumers reach
them via `c.` (forecast/percent_breakdown/view_models/tui/core/diff_kernel)
or bare `def`-shims (record.py). No consumer source edits.

Spec: docs/superpowers/specs/2026-06-01-extract-weekrefs-5h-backfill-design.md
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import threading
from dataclasses import replace

from _cctally_core import (
    WeekRef,
    _canonicalize_optional_iso,
    make_week_ref,
    parse_iso_datetime,
)

# Pure stdlib kernels; no cross-sibling I/O and no `cctally` back-import.
import _lib_credit_identity
import _lib_credit_selection
import _lib_journal


def _cctally():
    """Resolve the current `cctally` module at call-time."""
    return sys.modules["cctally"]


def _get_canonical_boundary_for_date(
    conn: sqlite3.Connection,
    week_start_date_str: str,
    *,
    account_key: "str | None" = None,
) -> tuple[str | None, str | None]:
    """Return the first established (week_start_at, week_end_at) for a week.

    ``account_key`` (#703 + #707 §6.2a): a real key scopes the read to that
    account's snapshots. ``get_recent_weeks(account_key=…)`` scoped its initial
    rows and then called this helper WITHOUT the account, so the boundary one
    account's week was anchored on could come from another account's snapshot —
    and the credit cadence, marker and cost basis all follow that anchor.
    ``None`` is the explicit merged read, byte-identical on a single-account
    install and the shape every existing caller keeps.
    """
    acct_pred = "" if account_key is None else " AND account_key = ?"
    acct_param: tuple = () if account_key is None else (account_key,)
    row = conn.execute(
        f"""
        SELECT week_start_at, week_end_at
        FROM weekly_usage_snapshots
        WHERE week_start_date = ?{acct_pred}
          AND week_start_at IS NOT NULL AND week_start_at != ''
          AND week_end_at IS NOT NULL AND week_end_at != ''
        ORDER BY captured_at_utc ASC, id ASC
        LIMIT 1
        """,
        (week_start_date_str,) + acct_param,
    ).fetchone()
    if row:
        start_at = _canonicalize_optional_iso(row["week_start_at"], "weekStartAt")
        end_at = _canonicalize_optional_iso(row["week_end_at"], "weekEndAt")
        if start_at and end_at:
            return start_at, end_at
    return None, None


def get_recent_weeks(
    conn: sqlite3.Connection, limit: "int | None", *,
    account_key: "str | None" = None,
) -> list[WeekRef]:
    # ``limit=None`` → SQL ``LIMIT -1`` (unbounded): the milestone-history
    # index (#hero-milestone-history) enumerates EVERY navigable week with no
    # depth cap. Existing callers pass a concrete int and are unaffected.
    #
    # ``account_key`` (#341, spec §3): ``None`` = the account-blind merged read
    # (today's byte-identical behavior); a real key / ``unattributed`` scopes both
    # snapshot legs to that account's weeks (the ``--account`` render consumers —
    # ``report`` — pass it explicitly).
    limit_sql = -1 if limit is None else int(limit)
    acct_pred = "" if account_key is None else " WHERE account_key = ?"
    acct_p: tuple = () if account_key is None else (account_key,)
    rows = conn.execute(
        f"""
        SELECT week_start_date, MAX(week_end_date) AS week_end_date
        FROM (
          SELECT week_start_date, week_end_date FROM weekly_usage_snapshots{acct_pred}
          UNION ALL
          SELECT week_start_date, week_end_date FROM weekly_cost_snapshots{acct_pred}
        )
        GROUP BY week_start_date
        ORDER BY week_start_date DESC
        LIMIT ?
        """,
        acct_p + acct_p + (limit_sql,),
    ).fetchall()

    refs: list[WeekRef] = []
    for row in rows:
        date_str = row["week_start_date"]
        canon_start, canon_end = _get_canonical_boundary_for_date(
            conn, date_str, account_key=account_key)
        try:
            ref = make_week_ref(
                week_start_date=date_str,
                week_end_date=row["week_end_date"],
                week_start_at=canon_start,
                week_end_at=canon_end,
            )
        except ValueError:
            continue
        refs.append(ref)
    # Reset-event boundary override runs BEFORE the generic overlap clamp.
    # After the override, pre/post-reset refs are contiguous at the reset
    # moment, so the clamp becomes a no-op for them; for installs with no
    # reset events the clamp still does all the work it did before.
    return _apply_overlap_clamp_to_weekrefs(
        _apply_reset_events_to_weekrefs(conn, refs, account_key=account_key)
    )


def _apply_reset_events_to_weekrefs(
    conn: sqlite3.Connection, refs: list[WeekRef], *,
    account_key: "str | None" = None,
) -> list[WeekRef]:
    """Return the references unchanged. A credit is not a display boundary.

    #703 + #707 §2 is the rule: an Anthropic reset never changes the week's
    boundaries. Whatever Anthropic does to the counter — a partial goodwill
    credit, a full zeroing, an early reset — the window keeps its original start
    and end, and only the running 7d percent steps down.

    This function used to do the opposite three times over. It truncated the
    pre-credit week at the credit moment, it re-anchored the post-credit week's
    start to that moment, and for an in-place credit it SYNTHESIZED a second
    reference, so one unchanged subscription week rendered as two rows. A credit
    defines an ACCOUNTING EPOCH — a milestone ladder segment, a cost range, a
    high-water-mark floor — which every accounting read consults through
    `_lib_credit_selection`, and it defines no display boundary at all.

    A boundary CHANGE is left alone, and that is a deliberate retreat from the
    spec's §7. The rule there is to recover the original cadence and coalesce
    the linked references into it, and every formulation of that tried here
    broke something a reader would notice: pulling the moved week's end back to
    the original one left every entry between the two ends inside no window at
    all and the week's spend vanished; keeping the later end produced a
    ten-day "week"; and merging the two references reported one week's counter
    against the other's key. §7 asserts the coalescing in one clause and never
    says which week's usage key the merged row reports, which window its cost is
    taken over, or where spend recorded after the original end goes — and those
    are the questions that decide it. Until they are answered, a reference the
    API moved renders on the window it recorded, untruncated and un-re-anchored,
    which already satisfies §2's "stop re-anchoring the display window".

    Kept as a call site rather than deleted, because every display consumer
    routes through it and a future cadence decision belongs here. ``conn`` and
    ``account_key`` are unused for the same reason.
    """
    return list(refs)


# === #269 M4.4 — backfill-rescan process guard ==============================
#
# `_backfill_week_reset_events` rescans ALL `weekly_usage_snapshots` on every
# `open_db()`. Its output is NOT a pure function of the snapshots alone: the
# in-place-credit branch also reads existing `week_reset_events`, and the
# reset-events-regenerate contract deletes `week_reset_events` and re-runs the
# backfill WITHOUT a snapshot insert. So a `MAX(weekly_usage_snapshots.id)`-only
# guard would skip a needed regeneration. Guard instead on a process-level memo
# of BOTH `MAX(weekly_usage_snapshots.id)` AND a `week_reset_events` signature
# `(COUNT(*), MAX(rowid))`, scoped per DB FILE identity: skip the rescan only
# when both are unchanged since the last successful backfill. Byte-identical (a
# snapshot insert advances the WUS id; a reset-event delete/clear moves the
# reset-event sig; a backfill's own inserts move the sig so the next open is a
# clean skip). Production snapshots are append-only, so the WUS id moves on
# every new capture. In-memory / temp DBs (no file identity) are never memoized
# (always rescanned) — a `:memory:` identity could otherwise collide across
# distinct databases.

_BACKFILL_MEMO_LOCK = threading.Lock()
_BACKFILL_RESET_EVENTS_MEMO: dict = {}  # db_path -> (max_wus_id, (count, max_rowid))


def _reset_backfill_reset_events_memo() -> None:
    """Drop the backfill-guard memo (test hook + isolation)."""
    with _BACKFILL_MEMO_LOCK:
        _BACKFILL_RESET_EVENTS_MEMO.clear()


def _backfill_db_identity(conn: sqlite3.Connection) -> "str | None":
    """The `main` schema's on-disk file path, or None for in-memory/temp DBs
    (empty file string) — the memo key. None ⇒ never memoize (always rescan)."""
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main":
                return file or None
    except sqlite3.Error:
        pass
    return None


def _backfill_reset_events_signature(
    conn: sqlite3.Connection,
) -> "tuple[int, tuple[int, int]]":
    """`(MAX(weekly_usage_snapshots.id), (COUNT, MAX(rowid)) over
    week_reset_events)` — two O(1) MAX/COUNT descents vs the full-table rescan.
    Degrades to zero legs on a missing table so it never raises."""
    try:
        max_wus = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    except sqlite3.DatabaseError:
        max_wus = 0
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM week_reset_events"
        ).fetchone()
        wre = (int(row[0]), int(row[1]))
    except sqlite3.DatabaseError:
        wre = (0, 0)
    return (int(max_wus), wre)


def _backfill_credit_identity(row):
    """The ``CreditSource`` for a credit the backfill derives from ``row``.

    The source is the TRIGGERING snapshot. Its journal identity is its
    ``journal_id`` when the journal has stamped one, and its pre-cutover
    ``b:weekly_usage_snapshots:<rowid>`` bootstrap identity otherwise — the same
    two shapes the cutover itself exports, so a rebuilt store derives the same
    key. ``credit_order`` is that snapshot's capture instant, which is the
    instant the source record occupies in the journal's own ordering.
    """
    identity = row["journal_id"] or _lib_journal.bootstrap_id(
        "weekly_usage_snapshots", int(row["id"]))
    return _lib_credit_identity.CreditSource(
        kind="backfill",
        identity=identity,
        order=_lib_credit_identity.credit_order_from_instant(
            row["captured_at_utc"]),
    )


def _manual_coverage_lower_bound(prior_captured):
    """The earliest instant a manual credit may carry and still cover this drop.

    The drop was measured between two consecutive observations, so a manual
    credit that records it lies between them. The lower bound is floored to the
    hour because a manual row written before ``observed_at_utc`` existed carries
    only the hour-floored effective instant, which can precede the earlier
    observation by up to an hour. Returns ``None`` when there is no earlier
    observation to bound against, which disables the check rather than widening
    it to the whole week.
    """
    if not prior_captured:
        return None
    try:
        parsed = parse_iso_datetime(prior_captured, "backfill.prior_capture")
    except ValueError:
        return None
    return _cctally()._floor_to_hour(
        parsed.astimezone(dt.timezone.utc)).isoformat(timespec="seconds")


def _backfill_credit_already_recorded(conn, *, account_key, credit_key,
                                      new_week_end_at, effective_iso,
                                      week_start_date=None,
                                      observed_from=None, observed_to=None):
    """True when this credit is already represented in ``week_reset_events``.

    Three lookups, deliberately, because the table holds three kinds of row.

    The first is the exact ``(account_key, credit_key)`` identity, which is what
    replaced the singleton gate. That gate matched on ``new_week_end_at`` alone
    and therefore suppressed EVERY later credit in the same week, which is the
    defect being removed (#703 + #707 spec section 3.1).

    The second is the legacy compatibility exception the spec allows: a row
    written before this change carries no ``credit_key`` at all, so identity
    cannot find it, and re-inserting would double-count a credit already on
    file. It is matched on the retained facts a keyless row and this scan can
    both produce — the account, the new-week boundary, and the effective
    instant, the last through ``unixepoch()`` because the two producers spell
    the offset differently.

    Two retained facts are deliberately NOT in that predicate. The detection
    instant is written from the DETECTION clock by the live path and from the
    CAPTURE clock by this scan, so requiring equality would make every
    live-written legacy credit look new here and duplicate it. ``old_week_end_at``
    is excluded for the reason the pre-existing pre-check documented: a pre-fix
    store may hold ``(cur_end, cur_end)`` for the same credit this scan writes as
    ``(effective_iso, cur_end)``. The exception is scoped to keyless rows, so it
    can never suppress a second credit that carries an identity.

    The third is a MANUAL credit already covering this drop. Neither lookup
    above can see one: ``record-credit``'s key comes from its op and never
    equals a snapshot-derived key, and the keyless arm requires a NULL key a
    manual row does not have. So a manual credit of 25pp or more was recorded
    twice — once by the command, and again by this scan off the command's own
    synthetic post-credit snapshot, which presents exactly the drop this scan
    fires on. Before unification the duplicate at least landed in a different
    table; now both rows sit in ``week_reset_events`` and the week grows a
    second accounting epoch nobody recorded.

    A manual credit covers this drop when its accounting instant falls between
    the two observations the drop was measured across. The lower bound is
    floored to the hour because a manual row written before ``observed_at_utc``
    existed carries only the hour-floored effective instant, which can precede
    the earlier observation by up to an hour; flooring the bound too keeps such
    a row reachable without widening the window into an earlier credit's.
    """
    if credit_key is not None:
        found = conn.execute(
            "SELECT 1 FROM week_reset_events "
            "WHERE account_key = ? AND credit_key = ? LIMIT 1",
            (account_key, credit_key),
        ).fetchone()
        if found is not None:
            return True
    legacy = conn.execute(
        "SELECT 1 FROM week_reset_events "
        "WHERE credit_key IS NULL AND account_key = ? "
        "  AND new_week_end_at = ? "
        "  AND unixepoch(effective_reset_at_utc) = unixepoch(?) LIMIT 1",
        (account_key, new_week_end_at, effective_iso),
    ).fetchone()
    if legacy is not None:
        return True
    if week_start_date is None or observed_from is None or observed_to is None:
        return False
    manual = conn.execute(
        "SELECT 1 FROM week_reset_events "
        "WHERE account_key = ? AND week_start_date = ? "
        "  AND old_week_end_at IS NULL AND new_week_end_at IS NULL "
        "  AND unixepoch(COALESCE(observed_at_utc, effective_reset_at_utc)) "
        "      >= unixepoch(?) "
        "  AND unixepoch(COALESCE(observed_at_utc, effective_reset_at_utc)) "
        "      <= unixepoch(?) LIMIT 1",
        (account_key, week_start_date, observed_from, observed_to),
    ).fetchone()
    return manual is not None



def _backfill_week_reset_events(conn: sqlite3.Connection) -> None:
    """One-shot scan over historical snapshots to synthesize reset events
    for past mid-week resets the tool lived through before this feature
    shipped. Idempotent via UNIQUE(account_key, credit_key) + INSERT OR IGNORE,
    the identity #703 + #707 moved the row onto so several credits per week
    became representable — safe to re-run, safe to ship alongside the DDL.

    Rule mirrors the runtime detection in cmd_record_usage: when a new
    week_end_at arrives in a snapshot whose captured_at_utc is still
    BEFORE the prior week's end, that's a mid-week reset. Boundary ISO
    strings get canonicalized via `_canonicalize_optional_iso` and the
    effective reset moment is floored to the hour via `_floor_to_hour`
    so minute/second-level Anthropic jitter ("in X hr Y min" relative-text
    drift) doesn't masquerade as a reset.

    ONE deliberate divergence from the live rule: backfill passes
    ``allow_reset_to_zero=False`` to ``_is_reset_drop``, so it fires only on
    the unambiguous ``>=25pp`` drop. The lenient reset-to-zero signal is
    live-only — the live path debounces a transient API zero (issue #128),
    but this one-shot historical scan has no debounce and would otherwise
    mis-read a stale-replica 0% blip (``6% → 0% → 1%`` on a still-future
    week_end) as a credit, segmenting the week into a degenerate zero-width
    window. See ``_is_reset_drop`` for the full rationale.
    """
    c = _cctally()
    # #269 M4.4: skip the full rescan when neither the snapshots nor the
    # reset events moved since the last successful backfill on this DB file.
    #
    # #271 §9d rider (from the #269 final review): the skip signature
    # (`_backfill_reset_events_signature`, a MAX(id)/COUNT descent) is blind to a
    # NON-max `weekly_usage_snapshots` DELETE — dropping a middle row leaves
    # MAX(id) and could leave COUNT unchanged vs a prior state, so a rescan may
    # be skipped. That is byte-safe because this backfill is ADD-ONLY
    # (`INSERT OR IGNORE INTO week_reset_events`): a skipped rescan can only fail
    # to *add* an event, never *remove* a needed one, matching the pre-#269
    # idempotent behavior exactly.
    db_id = _backfill_db_identity(conn)
    sig = _backfill_reset_events_signature(conn)
    if db_id is not None:
        with _BACKFILL_MEMO_LOCK:
            if _BACKFILL_RESET_EVENTS_MEMO.get(db_id) == sig:
                # Preserve the original's transaction-flush behavior on the
                # skip path (harmless when there is nothing pending).
                conn.commit()
                return
    try:
        rows = conn.execute(
            # #703 + #707: `id` and `journal_id` come along because the credit's
            # `credit_key` is derived from the TRIGGERING SNAPSHOT's journal
            # identity — its `journal_id`, or its pre-cutover
            # `b:weekly_usage_snapshots:<rowid>` bootstrap identity when the row
            # predates the journal. `week_start_date` comes along because the
            # unified credit row records the week it belongs to.
            "SELECT id, journal_id, captured_at_utc, week_start_date, "
            "       week_end_at, weekly_percent, account_key "
            "FROM weekly_usage_snapshots "
            "WHERE week_end_at IS NOT NULL "
            "ORDER BY account_key ASC, captured_at_utc ASC, id ASC"
        ).fetchall()
    except sqlite3.DatabaseError:
        return
    # Canonicalized (hour-rounded) previous end; stored canonical form is
    # what WeekRef.week_end_at carries after make_week_ref, so maps in
    # _apply_reset_events_to_weekrefs stay joinable without extra parsing.
    # #341: the scan is PARTITIONED by account_key (ORDER BY account_key first)
    # so one account's boundary shift never derives a phantom reset off another
    # account's consecutive snapshot; prior state resets at each account boundary.
    prior_end = None
    prior_pct: float | None = None
    prior_account = None
    prior_captured: str | None = None
    for row in rows:
        cur_end_raw = row["week_end_at"]
        cur_pct = row["weekly_percent"]
        cur_account = row["account_key"]
        if cur_account != prior_account:
            # New account partition — do not compare across the boundary.
            prior_end = None
            prior_pct = None
            prior_captured = None
            prior_account = cur_account
        if not cur_end_raw:
            continue
        try:
            cur_end = _canonicalize_optional_iso(cur_end_raw, "backfill.cur")
        except ValueError:
            continue
        if cur_end is None:
            continue
        if prior_end and cur_end != prior_end:
            try:
                prior_end_dt = parse_iso_datetime(prior_end, "backfill.prior")
                captured_dt  = parse_iso_datetime(row["captured_at_utc"], "backfill.cap")
            except ValueError:
                prior_end = cur_end
                prior_pct = cur_pct
                prior_captured = row["captured_at_utc"]
                continue
            # Real mid-week reset needs three signals:
            # 1. Boundary shifted (already checked).
            # 2. Prior boundary was still in the future (not natural rollover).
            # 3. weekly_percent dropped substantially (prior_pct - cur_pct
            #    >= RESET_PCT_DROP_THRESHOLD). Filters out API jitter where
            #    Anthropic briefly reported a different reset_at but usage
            #    stayed roughly the same.
            if (
                captured_dt < prior_end_dt
                and prior_pct is not None and cur_pct is not None
                and _is_reset_drop(prior_pct, cur_pct, allow_reset_to_zero=False)
            ):
                # Floor to the hour so the display boundary lands on the
                # natural hour mark (Anthropic's reset times are always
                # hour-aligned, and users think of weeks in hour-mark
                # units). A reset at 18:08Z becomes 18:00Z in the event
                # row, rendering as "21:00" local instead of "21:08".
                effective_iso = c._floor_to_hour(captured_dt).isoformat(timespec="seconds")
                source = _backfill_credit_identity(row)
                credit_key = _lib_credit_identity.derive_credit_key(source)
                if not _backfill_credit_already_recorded(
                    conn, account_key=cur_account, credit_key=credit_key,
                    new_week_end_at=cur_end, effective_iso=effective_iso,
                    week_start_date=row["week_start_date"],
                    observed_from=_manual_coverage_lower_bound(prior_captured),
                    observed_to=row["captured_at_utc"],
                ):
                    conn.execute(
                        "INSERT OR IGNORE INTO week_reset_events "
                        "(detected_at_utc, old_week_end_at, new_week_end_at, "
                        " effective_reset_at_utc, account_key, week_start_date, "
                        " observed_at_utc, confirming_capture_at_utc, "
                        " observed_post_credit_pct, credit_key, credit_order) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (row["captured_at_utc"], prior_end, cur_end,
                         effective_iso, cur_account, row["week_start_date"],
                         row["captured_at_utc"], row["captured_at_utc"],
                         cur_pct, credit_key,
                         _lib_credit_identity.derive_credit_order(source)),
                    )
        elif prior_end and cur_end == prior_end:
            # In-place credit branch (v1.7.2). Mirrors the live detection
            # in cmd_record_usage: same end_at across two captures + ≥25pp
            # drop in weekly_percent + prior end still in the future at
            # captured_dt → Anthropic-issued goodwill credit. One event
            # row with old == new == cur_end, effective = floor_to_hour
            # of the captured_at when the drop was first observed.
            try:
                prior_end_dt = parse_iso_datetime(prior_end, "backfill.prior")
                captured_dt  = parse_iso_datetime(row["captured_at_utc"], "backfill.cap")
            except ValueError:
                prior_end = cur_end
                prior_pct = cur_pct
                prior_captured = row["captured_at_utc"]
                continue
            if (
                captured_dt < prior_end_dt
                and prior_pct is not None and cur_pct is not None
                and _is_reset_drop(prior_pct, cur_pct, allow_reset_to_zero=False)
            ):
                # Canonicalize to UTC before isoformat so the stored
                # offset is `+00:00`, matching the live detection path
                # (cmd_record_usage uses now_utc which is already UTC).
                # parse_iso_datetime returns .astimezone() (host-local
                # fallback at bin/cctally:_local_tz_name gate); without
                # this normalization, non-UTC hosts would store the
                # column as e.g. `+03:00`, breaking lex comparisons
                # downstream (CLAUDE.md gotcha: 5h-block cross-reset
                # comparisons go through unixepoch(), NOT lex
                # BETWEEN/</>; the reset-aware DB clamp here applies
                # the same rule). The reset-aware clamp now wraps both
                # sides with unixepoch() (Bug 2 fix), but a canonical
                # UTC offset on write is the right defense-in-depth.
                effective_iso = (
                    c._floor_to_hour(captured_dt.astimezone(dt.timezone.utc))
                    .isoformat(timespec="seconds")
                )
                # Row shape: old=effective_iso, new=cur_end (distinct
                # values). See the live-detection site in
                # bin/_cctally_record.py for the full rationale; in
                # short, old==new collapses the credited week to a
                # zero-width window in _apply_reset_events_to_weekrefs.
                source = _backfill_credit_identity(row)
                credit_key = _lib_credit_identity.derive_credit_key(source)
                if not _backfill_credit_already_recorded(
                    conn, account_key=cur_account, credit_key=credit_key,
                    new_week_end_at=cur_end, effective_iso=effective_iso,
                    week_start_date=row["week_start_date"],
                    observed_from=_manual_coverage_lower_bound(prior_captured),
                    observed_to=row["captured_at_utc"],
                ):
                    conn.execute(
                        "INSERT OR IGNORE INTO week_reset_events "
                        "(detected_at_utc, old_week_end_at, new_week_end_at, "
                        " effective_reset_at_utc, account_key, week_start_date, "
                        " observed_at_utc, confirming_capture_at_utc, "
                        " observed_post_credit_pct, credit_key, credit_order) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (row["captured_at_utc"], effective_iso, cur_end,
                         effective_iso, cur_account, row["week_start_date"],
                         row["captured_at_utc"], row["captured_at_utc"],
                         cur_pct, credit_key,
                         _lib_credit_identity.derive_credit_order(source)),
                    )
        prior_end = cur_end
        prior_pct = cur_pct
        prior_captured = row["captured_at_utc"]
    # Flush implicit transaction so callers using explicit BEGIN
    # (e.g. _backfill_five_hour_blocks) don't trip "cannot start a
    # transaction within a transaction".
    conn.commit()
    # #269 M4.4: store the POST-backfill signature (the scan's own inserts move
    # the reset-event sig, so the next open with no snapshot change is a clean
    # skip). Only after a completed scan — the early `except` return above
    # deliberately does not memoize a partial state.
    if db_id is not None:
        with _BACKFILL_MEMO_LOCK:
            _BACKFILL_RESET_EVENTS_MEMO[db_id] = _backfill_reset_events_signature(conn)


# Minimum weekly_percent drop that counts as a goodwill reset (percentage
# points). Real resets zero out usage, so the drop is large; transient API
# flaps show similar percents on both sides. 25pp catches both the known
# 86→0 and 34→0 cases while filtering 35→33-style jitter.
_RESET_PCT_DROP_THRESHOLD = 25.0

# In-place 5h-credit threshold. Mirrors `_RESET_PCT_DROP_THRESHOLD` but
# scaled down for the 5h dimension: typical 5h usage stays under ~10pp in
# a single block, so a 5pp drop sits well above natural variation while
# proportionally being a larger signal than 25pp is on the weekly scale.
# See spec docs/superpowers/specs/2026-05-16-5h-in-place-credit-detection.md
# §2.1 (Q1) for rationale.
_FIVE_HOUR_RESET_PCT_DROP_THRESHOLD = 5.0

# Reset-to-zero discriminator (2026-06-01 surprise-reset fix). Anthropic's
# weekly reset zeroes the counter mid-window, but the 25pp magnitude gate
# above silently masks it for any user below ~25% usage (e.g. the observed
# 14→0). A reset-to-zero is unambiguous REGARDLESS of magnitude: a lagging
# API replica reports a slightly-lower number, never a clean 0 against real
# usage. So the detector ALSO fires when the post value collapses to ~0
# (<= _RESET_ZERO_FLOOR_PCT) with a drop clearing a small min-drop floor.
# The floor rejects 1%→0% stale-replica jitter, which would otherwise write
# a spurious week_reset_events row and segment the week.
_RESET_ZERO_FLOOR_PCT = 1.0
_RESET_ZERO_MIN_DROP_PCT = 3.0


def _is_reset_drop(
    prior_pct: float, cur_pct: float, *, allow_reset_to_zero: bool = True
) -> bool:
    """True when ``prior_pct → cur_pct`` is a genuine weekly reset/credit.

    Two independent percent-shape signals (OR):

    * **Partial credit** — drop ``>= _RESET_PCT_DROP_THRESHOLD`` (25pp).
    * **Reset-to-zero** — ``cur_pct`` collapses to ~0
      (``<= _RESET_ZERO_FLOOR_PCT``) with a drop clearing
      ``_RESET_ZERO_MIN_DROP_PCT``. Gated on ``allow_reset_to_zero``.

    ``allow_reset_to_zero`` scopes the lenient reset-to-zero signal to the
    sites that can afford it. **Live** current-week detection passes the
    default ``True``: the live in-place path debounces a transient API zero
    (issue #128 — arm on the first ~0, confirm only if it stays low, clear
    on recovery). The **historical backfill**
    (``_backfill_week_reset_events``) passes ``False`` — it is a one-shot
    scan with NO debounce, so a single stale-replica 0% reading on a
    still-future ``week_end`` (e.g. a ``6% → 0% → 1%`` blip) would otherwise
    be mis-read as a goodwill credit and segment the week into a degenerate
    zero-width window. Backfill therefore fires only on the unambiguous
    ``>=25pp`` drop and defers sub-25pp reset-to-zero to the live path.

    Callers retain the boundary predicates (same/advanced ``week_end_at``
    AND ``prior_end_dt > now``); this helper owns ONLY the percent-shape
    discrimination.
    """
    cur = float(cur_pct)
    drop = float(prior_pct) - cur
    if drop >= _RESET_PCT_DROP_THRESHOLD:
        return True
    if not allow_reset_to_zero:
        return False
    return cur <= _RESET_ZERO_FLOOR_PCT and drop >= _RESET_ZERO_MIN_DROP_PCT


def _week_ref_credit_epoch(
    conn: sqlite3.Connection, ref: WeekRef, *,
    account_key: "str | None" = None,
):
    """The credit epoch governing this week's latest state, or None.

    #703 + #707 §6.3. This replaces a predicate that asked whether the
    reference's boundaries had been REWRITTEN — `effective_reset_at_utc IN
    (week_start_at, week_end_at)` — and that question is now unanswerable for
    two independent reasons. A manual credit moves no boundary, so it never
    matched at all, which is why a `record-credit` week measured its cost and
    its ratio straight across the credit. And §6.1 stopped the display layer
    rewriting boundaries for automatic credits too, after which nothing equals
    any `effective_reset_at_utc` and every credited week fell back to the cached
    full-week path.

    The question the callers actually have is whether this week holds a credit,
    and if so which one governs, so that is what this returns: the row, through
    the same shared resolver every accounting reader uses, so a reader can never
    show a different epoch from the one the writer stamped.

    The capture bound is the week's own end, because these callers ask about the
    week's latest state rather than about one observation.
    """
    week_start_date = ref.week_start.isoformat() if ref.week_start else None
    if not week_start_date:
        return None
    captured_at = ref.week_end_at
    if not captured_at:
        return None
    try:
        return _lib_credit_selection.resolve_weekly_credit_epoch(
            conn, week_start_date=week_start_date, account_key=account_key,
            captured_at=captured_at, week_end_at=ref.week_end_at,
            week_start_at=ref.week_start_at)
    except sqlite3.Error:
        # A store with no `week_reset_events`, or one that predates the credit
        # columns, has recorded no credit — so the honest answer is "none"
        # rather than taking down every weekly render. A store that HAS a
        # credit answers it; nothing is guessed either way.
        return None


def _week_ref_has_reset_event(
    conn: sqlite3.Connection, ref: WeekRef, *,
    account_key: "str | None" = None,
) -> bool:
    """Whether a credit occurred inside `ref`'s week.

    Lets cost callers bypass the `weekly_cost_snapshots` cache — computed over
    the whole week — and recompute live over the epoch's own range instead. The
    name is kept because every call site reads it as a question about the week,
    which is what it now answers.
    """
    return _week_ref_credit_epoch(
        conn, ref, account_key=account_key) is not None


def _credited_week_keys(
    conn: sqlite3.Connection, weeks, *, account_key: "str | None" = None,
) -> set:
    """The bucket keys of the sub-weeks that hold a credit (#703 + #707 §6.4).

    ``weeks`` is a list of ``SubWeek``. The key returned is
    ``start_date.isoformat()`` — the bucket and lookup key every weekly renderer
    already indexes by — so a renderer marks a row without needing a connection
    of its own.

    Resolved through the shared epoch resolver, so a marker can never disagree
    with the epoch the accounting reads used.
    """
    out: set = set()
    for week in weeks:
        start_date = getattr(week, "start_date", None)
        end_ts = getattr(week, "end_ts", None)
        if start_date is None or not end_ts:
            continue
        row = _lib_credit_selection.resolve_weekly_credit_epoch(
            conn, week_start_date=start_date.isoformat(),
            account_key=account_key, captured_at=end_ts, week_end_at=end_ts,
            week_start_at=getattr(week, "start_ts", None))
        if row is not None:
            out.add(start_date.isoformat())
    return out


def _compute_cost_for_weekref(
    ref: WeekRef, *, skip_sync: bool = False, account_key: "str | None" = None,
    as_of: "str | None" = None,
) -> float | None:
    """Live-compute USD cost over `ref`'s (possibly reset-adjusted) range
    straight from session_entries. Mirrors what cmd_sync_week writes into
    weekly_cost_snapshots, minus the cache write — used for reset-affected
    weeks where the cached range disagrees with the effective range.

    ``skip_sync`` (default ``False``) is threaded to ``_sum_cost_for_range``
    so the caller can read the cache without triggering a JSONL ingest.
    The #268 dashboard/TUI sync-thread rebuild passes ``True`` (it ingests
    once at the top of the rebuild); ``build_trend_view`` calls this once per
    reset-event week, so without the flag each reset week re-globbed the whole
    ``~/.claude/projects`` tree — the CPU peg the sync-once refactor removes.

    ``as_of`` (#410 Task A) is the retained triggering observation clock. The
    reset-adjusted range end is clamped to it before the cache query so this
    alternate milestone-cost path has the same replay boundary as
    ``compute_week_cost(as_of=...)``.
    """
    c = _cctally()
    if not ref.week_start_at or not ref.week_end_at:
        return None
    try:
        start = parse_iso_datetime(ref.week_start_at, "weekRef.week_start_at")
        end = parse_iso_datetime(ref.week_end_at, "weekRef.week_end_at")
    except ValueError:
        return None
    if end <= start:
        return 0.0
    if as_of is not None:
        try:
            retained_end = parse_iso_datetime(as_of, "asOf")
        except ValueError:
            return None
        end = min(end, retained_end)
        if end <= start:
            return 0.0
    return c._sum_cost_for_range(
        start, end, mode="auto", skip_sync=skip_sync, account_key=account_key)


def _apply_overlap_clamp_to_weekrefs(refs: list[WeekRef]) -> list[WeekRef]:
    """Clamp each WeekRef's end to the next WeekRef's start on overlap.

    Caller-visible effect: report --weeks output (and its --json
    weekEndAt / weekEndDate) now reflects the true observed week end
    instead of the stale week_end_at captured from Anthropic's
    --resets-at at week-start. See _clamp_end_ats_to_next_start for
    the underlying signal. Input order is preserved (caller contract:
    get_recent_weeks returns DESC by week_start_date).

    Only refs with both week_start_at and week_end_at participate in
    clamping; date-only refs (pre-boundary-tracking rows) pass through.
    """
    c = _cctally()
    candidates = [(i, r) for i, r in enumerate(refs) if r.week_start_at and r.week_end_at]
    if len(candidates) < 2:
        return refs
    candidates.sort(key=lambda ir: ir[1].week_start_at)  # type: ignore[arg-type,return-value]
    pairs: list[tuple[str | None, str | None]] = [(r.week_start_at, r.week_end_at) for _, r in candidates]
    new_ends = c._clamp_end_ats_to_next_start(pairs)
    out = list(refs)
    for (idx, cur), new_end in zip(candidates, new_ends):
        if new_end is None or new_end == cur.week_end_at:
            continue
        new_end_dt = parse_iso_datetime(new_end, "week.end_at (clamped)")
        # internal fallback: host-local intentional
        new_end_date = (new_end_dt - dt.timedelta(seconds=1)).astimezone().date()
        out[idx] = replace(cur, week_end_at=new_end, week_end=new_end_date)
    return out
