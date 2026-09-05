"""percent-breakdown command handler (cmd_percent_breakdown).

Eager I/O sibling: bin/cctally loads this at startup and re-exports
cmd_percent_breakdown onto the cctally namespace (parser dispatch in
_cctally_parser.py: pb.set_defaults(func=c.cmd_percent_breakdown)).

Accessor discipline (spec §2): _cctally_core kernel symbols are honest-imported;
everything else — load_config, resolve_display_tz, _format_ts_compact,
_boxed_table, _get_canonical_boundary_for_date, _apply_reset_events_to_weekrefs,
get_milestones_for_week (a C2 symbol, reached on the ns) — via the call-time
_cctally() accessor. No _lib_ kernel.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from typing import Any, Sequence

from _cctally_core import (
    _canonicalize_optional_iso,
    compute_week_bounds,
    get_week_start_name,
    make_week_ref,
    now_utc_iso,
    open_db,
    parse_date_str,
    parse_iso_datetime,
)


def _cctally():
    """Call-time accessor to the cctally module namespace (ns-patchable)."""
    return sys.modules["cctally"]


# The typed cause a back-filled marginal states instead of a bare `n/a`
# (#750 S3, spec §3.3, issue #738). A bare snake-case code, following the
# representation of the closed `WithheldCause` vocabulary at
# `bin/_lib_diagnosis.py`, and deliberately NOT a member of it: that enum is
# closed and holds diagnosis-domain causes, and this one is a milestone-domain
# cause carried on a different wire object.
OBSERVATION_GAP_CAUSE = "observation_gap"


@dataclass(frozen=True)
class ObservationGapRun:
    """One maximal back-filled run of thresholds inside one milestone epoch.

    ``indexes`` are positions in the sequence that was classified, not
    thresholds, because `report --detail` renders several epochs in one table
    and two of them can hold the same threshold. The caller classifies exactly
    the sequence it is about to render, so a position is unambiguous where a
    threshold is not.
    """

    first_threshold: int
    last_threshold: int
    captured_at_utc: str
    previous_captured_at_utc: "str | None"
    indexes: tuple


@dataclass(frozen=True)
class ObservationGapDisclosure:
    """What a rendered milestone table has to say about its own gaps."""

    runs: tuple
    withheld_indexes: frozenset


def _milestone_value(row: Any, key: str, default: Any = None) -> Any:
    """One column of a milestone row, tolerating a store that lacks it.

    `get_milestones_for_week` returns `SELECT *`, so the columns are whatever
    the reader's stats.db carries. `sqlite3.Row` raises rather than returning
    None for a name it does not hold, and a classifier that raised there would
    take down a command over a column it only needs in order to say nothing.
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _continues_run(previous: Any, row: Any) -> bool:
    """Whether `row` extends the back-filled run `previous` belongs to.

    The predicate is the shared NON-NULL `usage_snapshot_id` and the shared
    `captured_at_utc` across consecutive thresholds. `marginal_cost_usd IS
    NULL` is deliberately not part of it: a null marginal also means no prior
    milestone exists, which is the ordinary shape of an epoch's first
    crossing, so keying on it would file every fresh ladder as a gap.

    A falsy snapshot id never matches, in either direction. Two rows that both
    fail to name their originating observation agree on nothing, and treating
    `NULL == NULL` as a match would collapse unrelated legacy rows into one
    run. `0` is rejected for the same reason: it is this repository's "no
    snapshot row" sentinel for a milestone's snapshot-id columns —
    `bin/_cctally_record.py` writes `cost_snapshot_id = 0` when there is no
    cost snapshot to anchor against, and `bin/build-dashboard-fixtures.py`
    seeds `0` into both id columns on every milestone it writes. The real
    writer always puts a rowid in `usage_snapshot_id`, so this rejects no
    production row; it keeps a fixture that seeds `0` with one `captured_at`
    from reading as a back-filled run.
    """
    snapshot = _milestone_value(row, "usage_snapshot_id")
    if not snapshot or _milestone_value(previous, "usage_snapshot_id") != snapshot:
        return False
    captured = _milestone_value(row, "captured_at_utc")
    if captured is None or _milestone_value(previous, "captured_at_utc") != captured:
        return False
    return int(row["percent_threshold"]) == int(previous["percent_threshold"]) + 1


def classify_observation_gaps(milestone_rows: Sequence) -> ObservationGapDisclosure:
    """Find the back-filled runs in the milestone rows a caller will render.

    #750 S3 §3.1 (issue #738). When observation stops for long enough that the
    next reading crosses several integer percents at once, the catch-up writer
    assigns ONE `usage_snapshot_id` and ONE `captured_at_utc` to every
    threshold it fills in and puts the whole accumulated marginal on the first
    inserted row. Every later row's marginal is therefore null because the
    crossings were never observed separately — not because they cost nothing.

    Classification is at READ time, so existing history is explained without
    changing journal truth or adding a column.

    Rows are grouped by `(account_key, week_start_date, reset_event_id)`
    before runs are sought, because a run is a statement about one account's
    ladder inside one week's milestone epoch. Both current callers already fix
    `week_start_date` in their query, so carrying it in the key changes nothing
    today; it is in the key so that a caller passing two weeks' rows does not
    have to depend on an invariant nothing states.

    A run needs at least two rows. The first row of a run keeps its marginal
    whatever that marginal is: it carries the accumulated cost when the writer
    had one to put there, and its null means "no prior milestone" when it
    opens the ladder.

    A run is reported only when at least one of its later rows actually has a
    null marginal to withhold. The note says the marginals are not separable,
    so a run in which every later row carries a number would print that
    sentence above a table showing every one of them. The production writer
    cannot produce such a run, and coupling the two decisions here means no
    future writer can either.
    """
    groups: dict = {}
    for index, row in enumerate(milestone_rows):
        key = (
            _milestone_value(row, "account_key", ""),
            _milestone_value(row, "week_start_date", ""),
            int(_milestone_value(row, "reset_event_id", 0) or 0),
        )
        groups.setdefault(key, []).append((index, row))

    runs: list = []
    withheld: set = set()
    for members in groups.values():
        members.sort(key=lambda pair: int(pair[1]["percent_threshold"]))
        start = 0
        for position in range(1, len(members) + 1):
            if position < len(members) and _continues_run(
                    members[position - 1][1], members[position][1]):
                continue
            run_withheld = frozenset(
                members[i][0] for i in range(start + 1, position)
                if _milestone_value(members[i][1],
                                    "marginal_cost_usd") is None
            )
            if position - start >= 2 and run_withheld:
                first = members[start][1]
                runs.append(ObservationGapRun(
                    first_threshold=int(first["percent_threshold"]),
                    last_threshold=int(
                        members[position - 1][1]["percent_threshold"]),
                    captured_at_utc=_milestone_value(first, "captured_at_utc"),
                    # The previous recorded crossing of this same ladder,
                    # which is what the elapsed span is measured from. A run
                    # that opens the ladder has none, and the note then states
                    # no span rather than inventing an origin.
                    previous_captured_at_utc=(
                        _milestone_value(members[start - 1][1],
                                         "captured_at_utc")
                        if start > 0 else None),
                    indexes=tuple(
                        members[i][0] for i in range(start, position)),
                ))
                withheld.update(run_withheld)
            start = position
    runs.sort(key=lambda run: run.indexes[0])
    return ObservationGapDisclosure(
        runs=tuple(runs), withheld_indexes=frozenset(withheld))


def _observation_gap_span(start_iso: "str | None",
                          end_iso: "str | None") -> "str | None":
    """The elapsed span between two capture instants, in words, or None."""
    if not start_iso or not end_iso:
        return None
    try:
        start = parse_iso_datetime(start_iso, "milestone.previous_captured_at")
        end = parse_iso_datetime(end_iso, "milestone.captured_at")
    except ValueError:
        return None
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        return None
    if seconds < 60:
        # The status-line hook ticks about every 30 seconds, so a run of two
        # crossings recorded one tick apart is an ordinary shape here. Rounding
        # such a span to minutes printed "0 minutes". Clamped to 1 because the
        # span is already known to be positive.
        whole = max(1, int(seconds))
        return f"{whole} second" if whole == 1 else f"{whole} seconds"
    if seconds < 3600:
        # Truncated rather than rounded, so that 3599 seconds cannot render as
        # "60 minutes" immediately below the "1.0 hours" the next branch gives
        # 3600.
        minutes = int(seconds // 60)
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds / 3600:.1f} hours"


def observation_gap_notes(disclosure: ObservationGapDisclosure, *,
                          tz) -> list:
    """One line per run, naming its range, its instant and its elapsed span.

    Stated once above the table rather than repeated per row, because the
    fact is about the run and not about any single threshold. The instant
    goes through `_format_ts_compact`, the display-timezone chokepoint the
    week header already uses, so the two agree about which clock they are on.
    """
    c = _cctally()
    notes: list = []
    for run in disclosure.runs:
        note = (
            f"Observation gap: {run.first_threshold}%-{run.last_threshold}% "
            "were all recorded from one observation at "
            f"{c._format_ts_compact(run.captured_at_utc, tz=tz)}"
        )
        span = _observation_gap_span(
            run.previous_captured_at_utc, run.captured_at_utc)
        if span is not None:
            note += f", {span} after the previous crossing"
        notes.append(
            f"{note}. Their marginal costs are not separable "
            f"({OBSERVATION_GAP_CAUSE})."
        )
    return notes


def observation_gap_marginal_cell(marginal_value: Any, *, withheld: bool) -> str:
    """The Marginal Cost cell for one milestone row.

    The withheld cell prints the bare cause rather than `explain`'s
    `withheld (<code>)` wrapper. The wrapper belongs to that command's
    `EvidenceField` rendering, and spelling it here would widen this table
    past 80 columns on every week that has a gap, while the note above the
    table already says what the code means.
    """
    if marginal_value is not None:
        return f"${float(marginal_value):.6f}"
    return OBSERVATION_GAP_CAUSE if withheld else "n/a"


def _render_percent_breakdown_terminal(
    *,
    week_start_date: str,
    week_end_date: str,
    display_start_iso: str | None,
    display_end_iso: str | None,
    milestone_list: list[dict[str, object]],
    tz,
    empty_message: str = "No percent milestones recorded for this week.",
    gap_disclosure: "ObservationGapDisclosure | None" = None,
) -> str:
    """Render the canonical weekly per-percent terminal design.

    ``gap_disclosure`` is OPTIONAL and defaults to today's behaviour, because
    `bin/_cctally_quota.py` calls this renderer for `codex quota breakdown`
    and must keep emitting the same bytes. Its indexes address positions in
    ``milestone_list``, so a caller must classify exactly the sequence it
    passes here.
    """
    c = _cctally()
    withheld = (
        gap_disclosure.withheld_indexes if gap_disclosure is not None
        else frozenset()
    )
    lines: list[str] = []
    if display_start_iso and display_end_iso:
        lines.append(
            f"Week: {c._format_ts_compact(display_start_iso, tz=tz)} -> "
            f"{c._format_ts_compact(display_end_iso, tz=tz)}"
        )
    else:
        lines.append(f"Week: {week_start_date}..{week_end_date}")
    if gap_disclosure is not None:
        lines.extend(c.observation_gap_notes(gap_disclosure, tz=tz))
    if not milestone_list:
        lines.append(empty_message)
        return "\n".join(lines)

    lines.extend(("Percent breakdown:", ""))
    headers = ["#", "Threshold", "Cumulative Cost", "Marginal Cost", "5h at crossing"]
    rows: list[list[str]] = []
    for idx, milestone in enumerate(milestone_list, start=1):
        percent = int(milestone["percentThreshold"])
        cumulative = float(milestone["cumulativeCostUSD"])
        marginal_value = milestone["marginalCostUSD"]
        five_hour_value = milestone["fiveHourPercentAtCrossing"]
        rows.append([
            str(idx),
            f"{percent}%",
            f"${cumulative:.6f}",
            c.observation_gap_marginal_cell(
                marginal_value, withheld=(idx - 1) in withheld),
            f"{float(five_hour_value):.0f}%" if five_hour_value is not None else "n/a",
        ])
    lines.append(c._boxed_table(
        headers, rows, ["right", "right", "right", "right", "right"],
    ))
    return "\n".join(lines)


def cmd_percent_breakdown(args: argparse.Namespace) -> int:
    c = _cctally()
    # #341 --account: resolve the render filter (provider=claude). This command
    # reads only stats tables (percent_milestones / weekly_usage_snapshots), not
    # the entry cache, so needs_cache=False. None = merged / byte-stable.
    acct_key, acct_exit = c.resolve_account_filter(args, "claude", needs_cache=False)
    if acct_exit is not None:
        return acct_exit
    _acct_pred = "" if acct_key is None else " AND account_key = ?"
    _acct_p = () if acct_key is None else (acct_key,)
    config = c.load_config()
    tz = c.resolve_display_tz(args, config)
    args._resolved_tz = tz
    week_start_name = get_week_start_name(config, args.week_start_name)

    conn = open_db()
    try:
        if args.week_start:
            week_start = parse_date_str(args.week_start, "--week-start")
        else:
            latest_usage = conn.execute(
                "SELECT week_start_date FROM weekly_usage_snapshots"
                + (" WHERE 1=1" + _acct_pred if acct_key is not None else "")
                + " ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
                _acct_p,
            ).fetchone()
            if latest_usage is not None:
                week_start = dt.date.fromisoformat(latest_usage["week_start_date"])
            else:
                # internal fallback: host-local intentional
                now_local = dt.datetime.now().astimezone()
                week_start, _ = compute_week_bounds(now_local, week_start_name)

        week_start_date = week_start.isoformat()

        # Get week_end from any snapshot for this week
        end_row = conn.execute(
            f"""
            SELECT MAX(week_end_date) AS week_end_date
            FROM (
              SELECT week_end_date FROM weekly_usage_snapshots WHERE week_start_date = ?{_acct_pred}
              UNION ALL
              SELECT week_end_date FROM weekly_cost_snapshots WHERE week_start_date = ?{_acct_pred}
              UNION ALL
              SELECT week_end_date FROM percent_milestones WHERE week_start_date = ?{_acct_pred}
            )
            """,
            (week_start_date,) + _acct_p + (week_start_date,) + _acct_p
            + (week_start_date,) + _acct_p,
        ).fetchone()
        week_end_date = end_row["week_end_date"] if end_row and end_row["week_end_date"] else (
            (week_start + dt.timedelta(days=6)).isoformat()
        )

        # Apply reset-event boundary rewrites (same path get_recent_weeks
        # uses) so the display header shows the effective window — e.g.
        # a post-reset short week shows "2026-04-23..2026-04-25" rather
        # than the backdated API-derived "2026-04-18..2026-04-25".
        canon_start, canon_end = c._get_canonical_boundary_for_date(conn, week_start_date)
        display_start_iso = canon_start
        display_end_iso = canon_end
        if canon_start and canon_end:
            try:
                base_ref = make_week_ref(
                    week_start_date=week_start_date,
                    week_end_date=week_end_date,
                    week_start_at=canon_start,
                    week_end_at=canon_end,
                )
                adjusted = c._apply_reset_events_to_weekrefs(
                    conn, [base_ref], account_key=acct_key)
                if adjusted:
                    display_start_iso = adjusted[0].week_start_at
                    display_end_iso = adjusted[0].week_end_at
            except ValueError:
                pass

        # v1.7.2 segment filter: when a week_reset_events row exists for
        # the current ``week_end_at``, narrow the milestone listing to
        # the active (latest) segment so a credited week's header (which
        # already reflects the post-credit window via the canon-boundary
        # rewrite above) is coherent with the body. Sentinel ``0`` covers
        # pre-credit / no-event weeks; pre-005 DBs that didn't have the
        # column also default to 0 via the migration's ALTER DEFAULT.
        active_segment = 0
        canon_end_for_lookup = None
        latest_end_row = conn.execute(
            "SELECT week_end_at FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? AND week_end_at IS NOT NULL" + _acct_pred +
            " ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
            (week_start_date,) + _acct_p,
        ).fetchone()
        if latest_end_row is not None:
            canon_end_for_lookup = _canonicalize_optional_iso(
                latest_end_row["week_end_at"], "pb.cur"
            )
        if canon_end_for_lookup:
            # #750 S3 B3: through the one chokepoint, which scopes the read to
            # the requesting account and orders on the reset INSTANT. This site
            # used to order on insertion `id` with no account predicate, so it
            # could name another account's cut, and among this account's own
            # cuts it named whichever row was written last rather than the
            # latest one.
            seg_row = c._latest_reset_event_for_end(
                conn, canon_end_for_lookup, account_key=acct_key)
            if seg_row is not None:
                active_segment = int(seg_row["id"])

        milestones = [
            m for m in c.get_milestones_for_week(
                conn, week_start_date, account_key=acct_key)
            if int(m["reset_event_id"] or 0) == active_segment
        ]

        # #750 S3 §3.1: classify the rows this command is about to render, in
        # the order it renders them, so the disclosure's positions address
        # `milestone_list` directly.
        gap_disclosure = c.classify_observation_gaps(milestones)

        milestone_list = []
        for index, m in enumerate(milestones):
            entry = {
                "percentThreshold": int(m["percent_threshold"]),
                "cumulativeCostUSD": round(float(m["cumulative_cost_usd"]), 9),
                "marginalCostUSD": round(float(m["marginal_cost_usd"]), 9) if m["marginal_cost_usd"] is not None else None,
                "capturedAt": m["captured_at_utc"],
                "fiveHourPercentAtCrossing": round(float(m["five_hour_percent_at_crossing"]), 1) if m["five_hour_percent_at_crossing"] is not None else None,
            }
            # Additive and OPTIONAL (§3.3): `marginalCostUSD` stays
            # numeric-or-null and the cause rides beside it, present only on a
            # classified null. `docs/cli-contract.md` permits an additive
            # optional field with no `schemaVersion` bump. The JSON does not
            # expose `usage_snapshot_id`, so a consumer reconstructs a run
            # from consecutive rows that share `capturedAt` and carry this key.
            if index in gap_disclosure.withheld_indexes:
                entry["marginalCostWithheldCause"] = c.OBSERVATION_GAP_CAUSE
            milestone_list.append(entry)

        output = {
            "weekStartDate": week_start_date,
            "weekEndDate": week_end_date,
            "weekStartAt": display_start_iso,
            "weekEndAt": display_end_iso,
            "milestones": milestone_list,
            "generatedAt": now_utc_iso(),
        }

        if args.json:
            output.update(c.account_json_fields(acct_key))  # #341 R8 decoration
            print(json.dumps(c.stamp_schema_version(output), indent=2))
            return 0

        empty_message = (
            "(post-credit segment, no milestones crossed yet)"
            if active_segment > 0
            else "No percent milestones recorded for this week."
        )
        print(_render_percent_breakdown_terminal(
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            display_start_iso=display_start_iso,
            display_end_iso=display_end_iso,
            milestone_list=milestone_list,
            tz=tz,
            empty_message=empty_message,
            gap_disclosure=gap_disclosure,
        ))

        return 0
    finally:
        conn.close()
