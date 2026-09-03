"""Stale-replica selection and epoch resolution for Anthropic weekly credits.

Three selectors, one level resolver and one epoch resolver, each the single
place its rule is written.

The two selectors replace a tolerance band. Before this module both the
automatic and the manual path filtered on
``ABS(weekly_percent - <a remembered pre-credit level>) < 1.0``, and the
2026-09-01 incident is what that predicate does when the remembered level and
the stored one are different quantities: on the debounced confirmation leg the
remembered level is the armed marker's ``baseline_pct`` while the rows to remove
hold what was actually written to ``weekly_usage_snapshots``. They differed by
exactly 1.0, the strict band excluded every row, and the DELETE did nothing. A
band cannot be repaired by widening it, because the two quantities have no
bounded relationship; it is replaced by comparing against evidence.

There are TWO rules and not one, and the difference is not cosmetic. The
automatic path holds two observations that bracket the credit in time, so it can
say that a row BETWEEN them reading above the credited level contradicts both.
The manual path holds no such bracket: ``record-credit --at`` accepts any past
instant inside the week, so a credit recorded hours after it happened has
genuine climb between the assertion and the command, and there is no confirming
observation to close the upper end. Applying the automatic rule there would
delete that climb as replicas — 46 to 31 at 10:00, real usage through 32, 33 and
35, recorded at 14:00 — which is data loss. What separates a replay from a climb
in that situation is the LEVEL: a replay still reads the old value.

Neither rule contains a tolerance band, and banding on the resolved high-water
mark was considered for the automatic path and rejected as circular, because
that mark is computed from the same possibly-poisoned window.

The third selector is the recurring one. A credit fires exactly once, so the
closed bracket is evaluated exactly once and cannot see a replay that arrives
minutes after the confirmation. ``select_contradicted_replicas`` is what every
LATER detector pass runs, and it identifies such a row by level plus
contradiction rather than by time.

Spec: docs/superpowers/specs/2026-09-02-703-707-anthropic-same-window-credit.md
section 5.1.
"""
from __future__ import annotations

import sqlite3


#: What every caller needs from a matched row. The physical ``id`` is what the
#: unjournaled inline delete of a row with no logical identity uses, and
#: ``journal_id`` is what the durable suppression event names. Returning both
#: from one selector is what stops the two from drifting: before this module the
#: automatic predicate was written out twice, once for the capture and once for
#: the DELETE.
_REPLICA_PROJECTION = (
    "SELECT id, journal_id, captured_at_utc, weekly_percent "
    "  FROM weekly_usage_snapshots "
)


def _account_clause(account_key: "str | None") -> "tuple[str, tuple]":
    """Scope to one account, or explicitly to all of them.

    ``None`` is the deliberate merged read the preview path uses, not a silent
    global fallback: every write path passes a real key, so one account's credit
    never selects another account's rows.
    """
    if account_key is None:
        return "", ()
    return " AND account_key = ?", (account_key,)


def select_automatic_replicas(
    conn: sqlite3.Connection,
    *,
    week_start_date: str,
    account_key: "str | None",
    observed_at: str,
    confirming_capture_at: str,
    post_credit_pct: float,
) -> list:
    """Rows an automatic credit's two bounding observations contradict.

    A bracket closed at BOTH ends. Detection follows the credit within a tick,
    so the bracket is seconds wide, and a row inside it reading above the
    credited level contradicts both of the observations that bound it.

    Both ends are inclusive. An exclusive lower end would miss a replica stamped
    in the same second as the first post-credit observation, and an exclusive
    upper end would miss one stamped in the same second as the confirmation;
    timestamps here are second-precision by design. Inclusivity cannot delete
    either genuine endpoint, because the percent comparison is STRICTLY above
    the recorded landing level and both endpoints read at that level.

    ``observed_at`` and ``confirming_capture_at`` must be in the CAPTURE clock
    domain, because that is the column they filter. The ingest path separates
    the record's detection clock from the payload's capture clock, and comparing
    across the two can place the triggering post-credit snapshot before its own
    epoch.

    ``unixepoch()`` on both sides of every instant comparison: the producers
    spell the offset differently (``Z`` against ``+00:00``), and a textual
    comparison would silently disagree about them on a non-UTC host.
    """
    acct_sql, acct_params = _account_clause(account_key)
    return conn.execute(
        _REPLICA_PROJECTION +
        " WHERE week_start_date = ?" + acct_sql +
        "   AND unixepoch(captured_at_utc) >= unixepoch(?) "
        "   AND unixepoch(captured_at_utc) <= unixepoch(?) "
        "   AND weekly_percent > ? "
        " ORDER BY id",
        (week_start_date,) + acct_params
        + (observed_at, confirming_capture_at, float(post_credit_pct)),
    ).fetchall()


def select_manual_replicas(
    conn: sqlite3.Connection,
    *,
    week_start_date: str,
    account_key: "str | None",
    observed_at: str,
    from_pct: float,
) -> list:
    """Rows still reading at the pre-credit level a manual credit asserted.

    No upper bracket end, because a retroactive assertion has no confirming
    observation. The LEVEL is what separates a replay from a climb: a replay
    still reads the old value, so the rule targets rows at or above the asserted
    pre-credit high-water mark and leaves everything below it alone.

    ``from_pct`` is safe as the threshold here in a way the incident's baseline
    was not, because it is the resolved high-water mark read from the table
    rather than a level remembered in a marker.

    ``observed_at`` is the asserted instant — ``CreditPlan.captured_iso`` — and
    NOT the hour-floored ``effective_iso``. The floored instant reaches back up
    to an hour before the assertion, so it can select genuine history the
    assertion never claimed to supersede.
    """
    acct_sql, acct_params = _account_clause(account_key)
    return conn.execute(
        _REPLICA_PROJECTION +
        " WHERE week_start_date = ?" + acct_sql +
        "   AND unixepoch(captured_at_utc) >= unixepoch(?) "
        "   AND weekly_percent >= ? "
        " ORDER BY id",
        (week_start_date,) + acct_params + (observed_at, float(from_pct)),
    ).fetchall()


#: Every column an epoch consumer reads off the governing credit. The physical
#: `id` is the `reset_event_id` foreign key milestones carry; `credit_key` is the
#: identity that survives a rebuild; the two instants are the display one and the
#: accounting one, which must stay distinguishable at every read site.
_EPOCH_PROJECTION = (
    "SELECT id, credit_key, credit_order, week_start_date, "
    "       old_week_end_at, new_week_end_at, effective_reset_at_utc, "
    "       observed_at_utc, confirming_capture_at_utc, "
    "       observed_pre_credit_pct, observed_post_credit_pct, journal_id, "
    "       COALESCE(observed_at_utc, effective_reset_at_utc) AS accounting_at "
    "  FROM week_reset_events "
)


def resolve_weekly_credit_epoch(
    conn: sqlite3.Connection,
    *,
    week_start_date: str,
    account_key: "str | None",
    captured_at: str,
    week_end_at: "str | None" = None,
    week_start_at: "str | None" = None,
):
    """The credit epoch that governs a capture, or None before the first credit.

    ONE helper, shared by milestone recording, percent-breakdown, the TUI and
    every other reader. Leaving a site on its own query is what makes a reader
    show a different epoch from the one the writer stamped.

    Ordering, in full (spec §5.3):

        unixepoch(COALESCE(observed_at_utc, effective_reset_at_utc)) DESC,
        credit_order DESC,
        credit_key DESC

    `id DESC` is gone, and it was not merely imprecise but unusable. Row
    identifiers are projection-local and a rebuild reassigns them; worse, the
    rebuild sorts by fold family before sequence, and after unification a manual
    credit folds at op order 5 while an automatic one folds at event order 30.
    So identifier order after a rebuild can reverse the real chronology of two
    credits. `credit_order` fixes that because it records a fact of the SOURCE
    record rather than the fold position of the derived row; `credit_key` is the
    final deterministic fallback for legacy rows and exact ties.

    Selection is by the WEEK. `week_start_date` is what a credit row records,
    and it is the only predicate that sees a manual credit at all: a manual
    credit moved no boundary, so both boundary columns are NULL, and a resolver
    keyed on `new_week_end_at` would leave it invisible — which is exactly why a
    manual credit opened no milestone epoch before unification.

    A row written before `week_start_date` existed carries no week key, so it is
    matched a second way, and only such a row is. The identity such a row really
    has is the WINDOW THAT CONTAINS the instant it records, and that is the
    first legacy arm: `[week_start_at, week_end_at)` around `accounting_at`.
    Matching `new_week_end_at` alone — which is what this did — was correct only
    while `_apply_reset_events_to_weekrefs` rewrote the post-credit reference's
    end to exactly that value. §6.1 removed that rewrite, after which a legacy
    BOUNDARY-CHANGE row (`old` = the prior API end, `new` = the current one)
    matched nothing: the reference keeps the boundary the API stated first, so
    the credit went unmarked, `$/1%` fell back to the whole-week form, and cost
    came from the `weekly_cost_snapshots` cache rather than the epoch's range.

    The `new_week_end_at` equality is kept as the second legacy arm, for a
    reference that carries no start instant and for a credit recorded at the
    window's own closing instant, which containment's exclusive end excludes.
    """
    acct_sql, acct_params = _account_clause(account_key)
    legacy_arms: list[str] = []
    legacy_params: tuple = ()
    if week_start_at and week_end_at:
        legacy_arms.append(
            "(unixepoch(accounting_at) >= unixepoch(?) "
            " AND unixepoch(accounting_at) < unixepoch(?))")
        legacy_params += (week_start_at, week_end_at)
    if week_end_at:
        legacy_arms.append("unixepoch(new_week_end_at) = unixepoch(?)")
        legacy_params += (week_end_at,)
    legacy_sql = ""
    if legacy_arms:
        legacy_sql = (" OR (week_start_date IS NULL AND ("
                      + " OR ".join(legacy_arms) + "))")
    return conn.execute(
        _EPOCH_PROJECTION +
        " WHERE (week_start_date = ?" + legacy_sql + ")" + acct_sql +
        "   AND accounting_at IS NOT NULL "
        "   AND unixepoch(accounting_at) <= unixepoch(?) "
        " ORDER BY unixepoch(accounting_at) DESC, "
        "          credit_order DESC, credit_key DESC "
        " LIMIT 1",
        (week_start_date,) + legacy_params + acct_params + (captured_at,),
    ).fetchone()


def resolve_replica_level(
    conn: sqlite3.Connection,
    *,
    week_start_date: str,
    account_key: "str | None",
    observed_at: str,
    observed_pre_credit_pct,
):
    """The level at or above which a post-credit reading is a replay.

    A stale reading is a REPLAY of a value the counter genuinely held before the
    credit, so the level that identifies one is the pre-credit level. Two
    quantities claim that name and they are not the same, which is exactly the
    mismatch that produced the 2026-09-01 incident: ``observed_pre_credit_pct``
    is the level the detector REMEMBERED (the armed marker's ``baseline_pct`` on
    the debounced leg), while a replay reproduces a value that was actually
    written to ``weekly_usage_snapshots``. In the incident those differed by 1.0.

    Taking the LOWER of the two means neither quantity can hide a replay behind
    the other. Going lower is safe here in a way widening a tolerance band never
    was, because the removal is additionally bounded by contradicting evidence
    (see ``select_contradicted_replicas``): a genuine row at any level, followed
    inside the same accounting epoch by a reading below it, is impossible, since
    the weekly counter only falls when a credit opens a new epoch.

    Returns ``None`` when neither quantity exists, and the sweep then selects
    nothing rather than substituting a level.
    """
    acct_sql, acct_params = _account_clause(account_key)
    row = conn.execute(
        "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots "
        " WHERE week_start_date = ?" + acct_sql +
        "   AND unixepoch(captured_at_utc) < unixepoch(?)",
        (week_start_date,) + acct_params + (observed_at,),
    ).fetchone()
    stored_peak = None if row is None or row[0] is None else float(row[0])
    candidates = [v for v in (observed_pre_credit_pct, stored_peak)
                  if v is not None]
    if not candidates:
        return None
    return min(float(v) for v in candidates)


def select_contradicted_replicas(
    conn: sqlite3.Connection,
    *,
    week_start_date: str,
    account_key: "str | None",
    replay_floor_capture_at: str,
    replica_level: float,
    contradicting_capture_at: str,
) -> list:
    """Rows above the pre-credit level that a LATER in-epoch reading contradicts.

    The automatic bracket of ``select_automatic_replicas`` is closed at both
    ends and seconds wide, so it cannot see a replay that arrives minutes after
    the confirmation — and that replay is the whole of #703's second failure:
    every 7d surface reads the pre-credit value again, and on the next genuine
    tick it becomes ``prior_pct``, so the drop back to the credited level fires
    a phantom second credit.

    What identifies such a row is not time but the pair (level, contradiction).
    Within ONE accounting epoch the weekly counter never falls — a genuine fall
    is a credit, and a credit opens the next epoch. So a stored row at or above
    the pre-credit level that is FOLLOWED by an in-epoch reading below that
    level cannot both be true, and the later reading is the live one.

    That contradiction is what bounds the rule in time, and the bound is not
    optional. Without it — deleting every reading at or above the pre-credit
    level for the rest of the week — a genuine re-climb past that level would be
    erased on arrival and the counter would freeze just below it, which is a
    worse failure than the one being fixed. Section 5.4 already rejected the
    unbounded form on exactly this ground.

    ``contradicting_capture_at`` is the capture instant of the reading that
    supplies the contradiction, and the upper bound is inclusive so a replay
    stamped in its own second is still reached.

    ``replay_floor_capture_at`` is the instant after which a reading at the
    pre-credit level can only be a replay, and the lower bound is exclusive. On
    the automatic path that instant is the credit's confirming observation,
    because ``select_automatic_replicas`` owns everything up to and including
    it. A MANUAL credit records no confirming observation at all — a retroactive
    assertion has none — so its floor is the asserted credit instant, which is
    the same role: ``select_manual_replicas`` owns everything at and above the
    asserted pre-credit level from that instant, and this rule owns what arrives
    afterwards.
    """
    acct_sql, acct_params = _account_clause(account_key)
    return conn.execute(
        _REPLICA_PROJECTION +
        " WHERE week_start_date = ?" + acct_sql +
        "   AND unixepoch(captured_at_utc) > unixepoch(?) "
        "   AND unixepoch(captured_at_utc) <= unixepoch(?) "
        "   AND weekly_percent >= ? "
        " ORDER BY id",
        (week_start_date,) + acct_params
        + (replay_floor_capture_at, contradicting_capture_at,
           float(replica_level)),
    ).fetchall()
