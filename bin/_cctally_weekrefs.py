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
    _ordered_in_place_cuts,
    make_week_ref,
    parse_iso_datetime,
)


def _cctally():
    """Resolve the current `cctally` module at call-time."""
    return sys.modules["cctally"]


def _get_canonical_boundary_for_date(
    conn: sqlite3.Connection,
    week_start_date_str: str,
) -> tuple[str | None, str | None]:
    """Return the first established (week_start_at, week_end_at) for a week."""
    row = conn.execute(
        """
        SELECT week_start_at, week_end_at
        FROM weekly_usage_snapshots
        WHERE week_start_date = ?
          AND week_start_at IS NOT NULL AND week_start_at != ''
          AND week_end_at IS NOT NULL AND week_end_at != ''
        ORDER BY captured_at_utc ASC, id ASC
        LIMIT 1
        """,
        (week_start_date_str,),
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
        canon_start, canon_end = _get_canonical_boundary_for_date(conn, date_str)
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
    """Override API-derived boundaries with reset-event effective moments.

    ``account_key`` (#750 S3 B1) scopes the event read to one account, exactly
    as the `_apply_reset_events_to_subweeks` twin does. ``None`` is the
    explicit merged read and preserves the account-blind behaviour this
    applier had before. A real key matters as soon as two accounts hold
    credits in weeks that share an end instant, because the match is on that
    instant alone.

    For each row in week_reset_events:
      - A ref whose week_end_at matches `old_week_end_at` was the PRE-reset
        week: its API-declared end is in the future but Anthropic cut it
        early. Override ref.week_end_at = effective_reset_at_utc so display
        shows the real cut-off.
      - A ref whose week_end_at matches `new_week_end_at` is the POST-reset
        week: its API-derived start (= new resets_at - 7d) backdates into
        the pre-reset week. Override ref.week_start_at = effective_reset_at_utc
        so the new week starts at the actual reset moment.
      - **In-place credit (v1.7.2 round-3, Bug B).** Detected via the row
        shape ``old_week_end_at == effective_reset_at_utc`` (the live and
        backfill detection paths both write this shape — see
        ``test_event_row_old_is_effective_not_cur_end``). For these events,
        the credited week's ref matches ``new_week_end_at`` (the original
        resets_at is unchanged). Rewriting ``week_start_at`` to
        ``effective`` and stopping there drops the segment before the credit
        — where the user spent the bulk of their usage — because no other
        ref in ``refs`` carries ``week_end_at == effective``. So the week is
        SPLIT rather than shifted.

        The N-ary form (#750 S3 B2): N cuts inside one week make N+1
        segments, ``[start, c1)``, each ``[ci, ci+1)`` and ``[cn, end)``.
        The output is newest-first — tail, then the middles in reverse, then
        the head — because the ref already sat in a DESC-ordered list and
        ``cmd_report``'s trend table iterates it in that order.

    The ref's `week_start` (date) and `key` fields are intentionally left at
    the API-derived values — they're the lookup keys for
    weekly_usage_snapshots / weekly_cost_snapshots. Only the display-facing
    `week_start_at` / `week_end_at` (and the derived `week_end` date) shift.
    Every synthesized ref of one week shares the same `key` so downstream
    per-segment readers (``cmd_percent_breakdown`` / dashboard milestone
    panel) can still filter milestones by ``reset_event_id`` against the same
    lookup keys.
    """
    acct_pred = "" if account_key is None else " WHERE account_key = ?"
    acct_p: tuple = () if account_key is None else (account_key,)
    # ORDERED THE WAY `_latest_reset_event_for_end` ORDERS (#750 S3, Unit B
    # review): `unixepoch(effective_reset_at_utc) DESC, id DESC`. Two rows can
    # share one `(old_week_end_at, new_week_end_at)` pair and carry different
    # effective instants — epoch 1013 admits that whenever the rows carry
    # distinct origins — and the single-valued maps below answer with the FIRST
    # row they see. Unordered, "first" meant "whichever row was written last",
    # which is not what the chokepoint answers, so `weekly`/`report`/`project`
    # and the dashboard placed a week's start at one reset while `diff` and
    # `percent-breakdown` placed it at another. `unixepoch(...)`, never a
    # lexical compare, for the reason the chokepoint states: the column carries
    # mixed offset spellings.
    events = conn.execute(
        "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
        "FROM week_reset_events" + acct_pred
        + " ORDER BY unixepoch(effective_reset_at_utc) DESC, id DESC",
        acct_p,
    ).fetchall()
    if not events:
        return refs
    # Keyed by PARSED INSTANT, not by the raw column text. `week_reset_events`
    # holds whatever spelling the writing path stored, while a WeekRef's
    # `week_end_at` has already been canonicalized to UTC by `make_week_ref`.
    # A raw-string map therefore misses an event row written in a non-UTC
    # offset, and the miss is silent: the credited week renders as one row
    # here while `_apply_reset_events_to_subweeks`, which has always compared
    # parsed instants, renders two.
    # `test_subweek_and_weekref_appliers_agree_on_every_event_shape` is the
    # tie between the twins and covers exactly that spelling.
    pre_map: dict = {}
    post_map: dict = {}
    # In-place credit events have `old == effective` (the row shape the
    # live + backfill detection paths agree on). #750 S3 B2: this used to be
    # a bare SET of `new_week_end_at` instants beside a single-valued
    # `post_map`, so two in-place events for one week evicted each other
    # before the ref loop ran and the interval between the two cuts was lost.
    # It is now a MULTIMAP from the week's end instant to every cut inside
    # it, carried as (instant, raw text) so the stored spelling survives
    # while the instant does the ordering. The shape test itself stays a
    # raw-string compare, because both columns come from the same write and
    # the subweeks twin compares them the same way.
    in_place_cuts: dict = {}
    for e in events:
        effective = e["effective_reset_at_utc"]
        try:
            old_dt = parse_iso_datetime(
                e["old_week_end_at"], "reset_event.old_end"
            )
        except ValueError:
            old_dt = None
        try:
            new_dt = parse_iso_datetime(
                e["new_week_end_at"], "reset_event.new_end"
            )
        except ValueError:
            new_dt = None
        # `setdefault`, not assignment: the rows arrive latest-effective-first,
        # so the first row for a key is the one the chokepoint would return.
        if old_dt is not None:
            pre_map.setdefault(old_dt, effective)
        if new_dt is not None:
            if e["old_week_end_at"] == effective:
                try:
                    effective_dt = parse_iso_datetime(
                        effective, "reset_event.effective"
                    )
                except ValueError:
                    continue
                in_place_cuts.setdefault(new_dt, []).append(
                    (effective_dt, effective))
            else:
                post_map.setdefault(new_dt, effective)
    out: list[WeekRef] = []
    for ref in refs:
        new_ref = ref
        ref_end_dt = None
        if ref.week_end_at:
            try:
                ref_end_dt = parse_iso_datetime(
                    ref.week_end_at, "ref.week_end_at"
                )
            except ValueError:
                ref_end_dt = None
        if ref_end_dt is not None and ref_end_dt in pre_map:
            reset_at = pre_map[ref_end_dt]
            try:
                reset_dt = parse_iso_datetime(reset_at, "reset_event.effective")
                # internal fallback: host-local intentional
                new_end_date = (reset_dt - dt.timedelta(seconds=1)).astimezone().date()
                new_ref = replace(new_ref, week_end_at=reset_at, week_end=new_end_date)
            except ValueError:
                pass
        # A week can carry BOTH a boundary shift and an in-place cut. The
        # shift moves this week's real start, so it is the head segment's
        # start and the lower bound every cut must fall inside — deriving the
        # head from the API-derived start instead discards the shift, overlaps
        # the previous week, and lets `_apply_overlap_clamp_to_subweeks` move
        # that week's spend into this one (#750 S3, Unit B review).
        shift_raw = (
            post_map.get(ref_end_dt) if ref_end_dt is not None else None)
        head_start_raw = (
            shift_raw if shift_raw is not None else ref.week_start_at)
        cuts = _ordered_in_place_cuts(
            in_place_cuts.get(ref_end_dt), head_start_raw, ref_end_dt
        ) if ref_end_dt is not None else []
        if cuts:
            # N cuts make N+1 billing cycles. The output is NEWEST-FIRST,
            # because the original ref already sat in a DESC-ordered list and
            # `cmd_report`'s trend table iterates it in that order: emit the
            # tail (which takes the ref's own slot), then the middles in
            # reverse, then the head. Every synthesized segment is derived
            # from the ORIGINAL `ref` rather than from the progressively
            # shifted one, for the reason the subweeks twin gives: two cuts in
            # one week both carry that week's unchanged `new_week_end_at`.
            last_dt, last_raw = cuts[-1]
            out.append(replace(new_ref, week_start_at=last_raw))
            for index in range(len(cuts) - 1, -1, -1):
                cut_dt, cut_raw = cuts[index]
                seg_end_date = (
                    # internal fallback: host-local intentional
                    cut_dt - dt.timedelta(seconds=1)
                ).astimezone().date()
                if index == 0:
                    # The head starts where the week really starts — the
                    # boundary shift when this week carries one, otherwise the
                    # API-derived start — and closes at the first cut.
                    out.append(replace(
                        ref,
                        week_start_at=head_start_raw,
                        week_end_at=cut_raw,
                        week_end=seg_end_date,
                    ))
                else:
                    _prev_dt, prev_raw = cuts[index - 1]
                    out.append(replace(
                        ref,
                        week_start_at=prev_raw,
                        week_end_at=cut_raw,
                        week_end=seg_end_date,
                    ))
            continue
        if ref_end_dt is not None and ref_end_dt in post_map:
            # A boundary-shift reset: the ref's API-derived start backdates
            # into the pre-reset week, so it moves to the reset moment. No
            # segment is synthesized, because the shift moved the boundary
            # rather than splitting the week.
            out.append(replace(new_ref, week_start_at=post_map[ref_end_dt]))
            continue
        out.append(new_ref)
    return out


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


#: The raw-observation id prefix and its exact length (`"o:"` plus a 16-hex
#: digest). Mirrors the shape `_lib_journal.content_id` produces.
_RAW_OBS_ID_PREFIX = "o:"
_RAW_OBS_ID_LENGTH = 18


def _origin_from_snapshot_journal_id(journal_id):
    """The originating observation behind a `weekly_usage_snapshots.journal_id`.

    #750 S3 §1.2. A snapshot materialized by the `snapshot_accept` Model-A
    family carries `sa:<obs id>`, so stripping exactly one leading `sa:`
    recovers the raw observation the backfill should name as the reset's
    origin. Everything else stores NULL rather than inventing an identity:

    * NULL — a directly seeded or pre-cutover row names no observation;
    * `b:<table>:<rowid>` — a cutover bootstrap id, which is a row identity
      rather than an observation identity;
    * `sa:direct:<n>` — a synthetic accept with no raw observation behind it;
    * `sa:<obs id>:syn:<n>` — the synthetic post-credit snapshot a manual
      `record-credit` writes. One `sa:` strip leaves `o:<hex>:syn:<n>`, which
      is not an observation id at all, so the remainder is required to be a
      well-formed raw id rather than merely to start with `o:`.

    A NULL origin is not a loss: the row simply keeps the legacy tuple
    identity, which is the shape every retained event already has.
    """
    if not journal_id or not journal_id.startswith("sa:"):
        return None
    candidate = journal_id[3:]
    if (len(candidate) == _RAW_OBS_ID_LENGTH
            and candidate.startswith(_RAW_OBS_ID_PREFIX)):
        return candidate
    return None


def _legacy_reset_window(conn) -> bool:
    """True while this stats index is still at or below the frozen legacy head.

    #750 S3 §1.7. `_backfill_week_reset_events` runs inside the schema apply,
    which an epoch-current open never reaches, so this is True exactly for a
    pre-cutover store (and for a fresh or scratch index, where it is vacuous
    because there are no legacy rows to recognize). Scoping the guard below to
    that window is what keeps it from ever suppressing a genuine later reset.
    """
    try:
        import _cctally_core
        return int(conn.execute("PRAGMA user_version").fetchone()[0]) <= (
            _cctally_core.LEGACY_STATS_HEAD)
    except (sqlite3.DatabaseError, TypeError, ValueError):
        return False


#: The SQL shape test for a legacy IN-PLACE reset row. The first disjunct
#: recognizes the pre-v1.7.2 shape `(cur_end, cur_end)`, the second the current
#: shape `old == effective`. A boundary-shift row satisfies neither: its
#: `old_week_end_at` is the prior provider boundary, which differs from
#: `new_week_end_at` by definition and is strictly later than the instant,
#: because the backfill only fires while `captured_dt < prior_end_dt`.
_LEGACY_IN_PLACE_SHAPE = (
    "(old_week_end_at = new_week_end_at "
    " OR unixepoch(old_week_end_at) = unixepoch(effective_reset_at_utc))"
)


def _legacy_reset_row_exists(conn, *, account_key, new_week_end_at,
                             floored_iso, in_place) -> bool:
    """True when a legacy origin-null row of the SAME shape already records
    this reset.

    #750 S3 §1.7. A legacy row records the HOUR its reset fell in; the
    candidate records the exact capture second, so the two never match on the
    raw value and the partial unique indexes cannot recognize them as one
    event. The comparison is therefore against the candidate's hour-normalized
    instant.

    The instant is read from `effective_reset_at_utc` for every shape, because
    that is the only column every legacy writer filled with it:

    * the boundary-shift shape has `old_week_end_at` = the prior provider
      boundary, so only `effective_reset_at_utc` carries the instant;
    * the current in-place shape has `old_week_end_at == effective`, so the two
      columns agree and either would match;
    * the PRE-v1.7.2 in-place shape is `(cur_end, cur_end)` — both boundary
      columns hold the week end and carry no instant at all — and it is
      matchable only through `effective_reset_at_utc`. That shape is the one
      the backfill's deleted `already` pre-check named, and comparing
      `old_week_end_at` would miss it and mint a duplicate.

    The column is `TEXT NOT NULL` in the DDL and always has been, so there is
    no NULL-instant shape to fall back from.

    Reading one column for every shape is why `in_place` is a separate
    argument. Within the legacy window a boundary-shift legacy row and an
    in-place candidate can share `new_week_end_at` by construction — the shift
    moves the boundary TO the end the in-place credit later happens under — so
    an account, an end and an hour are not enough to tell the two apart, and
    matching on those alone suppresses the in-place candidate. `in_place`
    restores the shape discrimination through `_LEGACY_IN_PLACE_SHAPE`, which
    each call site asserts positively or negatively.

    Only origin-null rows are candidates, because a row that names an
    observation was written by a binary that already recorded exact instants
    and is deduped by the origin index instead.
    """
    shape = (_LEGACY_IN_PLACE_SHAPE if in_place
             else "NOT " + _LEGACY_IN_PLACE_SHAPE)
    row = conn.execute(
        "SELECT 1 FROM week_reset_events "
        "WHERE account_key = ? AND new_week_end_at = ? "
        "  AND origin_observation_id IS NULL "
        f"  AND {shape} "
        "  AND unixepoch(effective_reset_at_utc) = unixepoch(?) LIMIT 1",
        (account_key, new_week_end_at, floored_iso),
    ).fetchone()
    return row is not None


def _backfill_week_reset_events(conn: sqlite3.Connection) -> None:
    """One-shot scan over historical snapshots to synthesize reset events
    for past mid-week resets the tool lived through before this feature
    shipped. Idempotent via UNIQUE(old_week_end_at, new_week_end_at) +
    INSERT OR IGNORE — safe to re-run, safe to ship alongside the DDL.

    Rule mirrors the runtime detection in cmd_record_usage: when a new
    week_end_at arrives in a snapshot whose captured_at_utc is still
    BEFORE the prior week's end, that's a mid-week reset. Boundary ISO
    strings get canonicalized via `_canonicalize_optional_iso`; the effective
    reset moment records the snapshot's EXACT capture second in UTC (#750 S3
    §1.4). It used to be floored to the hour, which read as a display
    convenience but back-dated the event before observations that still
    legitimately belonged to the old window. Each row also names the raw
    observation it came from, when the snapshot's `journal_id` identifies one.

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
    # #750 S3: `journal_id` is what names the originating observation, but it
    # is NOT always there. `add_column_if_missing` adds it to
    # `weekly_usage_snapshots` LATER in the same schema apply that calls this
    # backfill, and a fixture-built stats.db carries neither. Selecting it
    # unconditionally raises `no such column`, which the handler below turns
    # into a SILENT return — so a legacy install's first open would back-fill
    # no historical reset event at all, and its second open returns early at
    # the epoch gate and never runs the schema apply again. Probe for the
    # column instead; when it is absent every origin is simply NULL, which is
    # the identity every retained event already has.
    try:
        has_origin_source = any(
            str(row[1]) == "journal_id"
            for row in conn.execute(
                "PRAGMA table_info(weekly_usage_snapshots)")
        )
        origin_select = ", journal_id" if has_origin_source else ""
        # #750 S3 §1.7: the legacy-row guard below is scoped to the pre-cutover
        # window, where the only rows that can carry an hour-floored instant
        # live. After cutover this is False and every genuine reset is admitted.
        legacy_window = _legacy_reset_window(conn)
        rows = conn.execute(
            "SELECT captured_at_utc, week_end_at, weekly_percent, account_key"
            f"{origin_select} "
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
    for row in rows:
        cur_end_raw = row["week_end_at"]
        cur_pct = row["weekly_percent"]
        cur_account = row["account_key"]
        if cur_account != prior_account:
            # New account partition — do not compare across the boundary.
            prior_end = None
            prior_pct = None
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
                # #750 S3 §1.4: the EXACT capture second, canonicalized to
                # UTC. Flooring to the hour was a display convenience, and it
                # back-dated the event before observations that still
                # legitimately belonged to the old window. The UTC
                # canonicalization is the same defence the in-place branch
                # below already carried: `parse_iso_datetime` returns a
                # host-local datetime on a non-UTC host, and an event row
                # spelled `+03:00` breaks the lex comparisons downstream
                # readers make.
                effective_dt_utc = captured_dt.astimezone(dt.timezone.utc)
                effective_iso = effective_dt_utc.isoformat(timespec="seconds")
                if legacy_window and _legacy_reset_row_exists(
                    conn, account_key=cur_account, new_week_end_at=cur_end,
                    floored_iso=c._floor_to_hour(effective_dt_utc)
                    .isoformat(timespec="seconds"), in_place=False,
                ):
                    prior_end = cur_end
                    prior_pct = cur_pct
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO week_reset_events "
                    "(detected_at_utc, old_week_end_at, new_week_end_at, "
                    " effective_reset_at_utc, account_key, "
                    " origin_observation_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (row["captured_at_utc"], prior_end, cur_end, effective_iso,
                     cur_account,
                     _origin_from_snapshot_journal_id(
                         row["journal_id"] if has_origin_source else None)),
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
                effective_dt_utc = captured_dt.astimezone(dt.timezone.utc)
                effective_iso = effective_dt_utc.isoformat(timespec="seconds")
                if legacy_window and _legacy_reset_row_exists(
                    conn, account_key=cur_account, new_week_end_at=cur_end,
                    floored_iso=c._floor_to_hour(effective_dt_utc)
                    .isoformat(timespec="seconds"), in_place=True,
                ):
                    prior_end = cur_end
                    prior_pct = cur_pct
                    continue
                # Row shape: old=effective_iso, new=cur_end (distinct
                # values). See the live-detection site in
                # bin/_cctally_record.py for the full rationale; in
                # short, old==new collapses the credited week to a
                # zero-width window in _apply_reset_events_to_weekrefs.
                conn.execute(
                    "INSERT OR IGNORE INTO week_reset_events "
                    "(detected_at_utc, old_week_end_at, new_week_end_at, "
                    " effective_reset_at_utc, account_key, "
                    " origin_observation_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (row["captured_at_utc"], effective_iso, cur_end, effective_iso,
                     cur_account,
                     _origin_from_snapshot_journal_id(
                         row["journal_id"] if has_origin_source else None)),
                )
        prior_end = cur_end
        prior_pct = cur_pct
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


def _week_ref_has_reset_event(
    conn: sqlite3.Connection, ref: WeekRef, *,
    account_key: "str | None" = None,
) -> bool:
    """Return True if `ref`'s effective boundaries were rewritten by a
    reset event (the ref went through _apply_reset_events_to_weekrefs
    and either its start or end now equals some effective_reset_at_utc).
    Lets cost callers bypass the weekly_cost_snapshots cache (which was
    computed over API-derived range) and recompute live over the
    effective range instead.

    ``account_key`` (#750 S3, Unit B review) scopes the read, exactly as the
    applier that produced ``ref`` is scoped. A caller that scoped the applier
    and then asked this question merged would be answering about a different
    account's events. ``None`` is the explicit merged read and is byte-stable
    on a single-account install.
    """
    if not ref.week_start_at and not ref.week_end_at:
        return False
    acct_pred = "" if account_key is None else " AND account_key = ?"
    acct_params: tuple = () if account_key is None else (account_key,)
    row = conn.execute(
        "SELECT 1 FROM week_reset_events "
        "WHERE effective_reset_at_utc IN (?, ?)" + acct_pred + " LIMIT 1",
        (ref.week_start_at, ref.week_end_at) + acct_params,
    ).fetchone()
    return row is not None


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
