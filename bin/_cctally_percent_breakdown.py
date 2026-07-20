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

from _cctally_core import (
    _canonicalize_optional_iso,
    compute_week_bounds,
    get_week_start_name,
    make_week_ref,
    now_utc_iso,
    open_db,
    parse_date_str,
)


def _cctally():
    """Call-time accessor to the cctally module namespace (ns-patchable)."""
    return sys.modules["cctally"]


def _render_percent_breakdown_terminal(
    *,
    week_start_date: str,
    week_end_date: str,
    display_start_iso: str | None,
    display_end_iso: str | None,
    milestone_list: list[dict[str, object]],
    tz,
    empty_message: str = "No percent milestones recorded for this week.",
) -> str:
    """Render the canonical weekly per-percent terminal design."""
    c = _cctally()
    lines: list[str] = []
    if display_start_iso and display_end_iso:
        lines.append(
            f"Week: {c._format_ts_compact(display_start_iso, tz=tz)} -> "
            f"{c._format_ts_compact(display_end_iso, tz=tz)}"
        )
    else:
        lines.append(f"Week: {week_start_date}..{week_end_date}")
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
            f"${float(marginal_value):.6f}" if marginal_value is not None else "n/a",
            f"{float(five_hour_value):.0f}%" if five_hour_value is not None else "n/a",
        ])
    lines.append(c._boxed_table(
        headers, rows, ["right", "right", "right", "right", "right"],
    ))
    return "\n".join(lines)


def cmd_percent_breakdown(args: argparse.Namespace) -> int:
    c = _cctally()
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
                """
                SELECT week_start_date
                FROM weekly_usage_snapshots
                ORDER BY captured_at_utc DESC, id DESC
                LIMIT 1
                """
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
            """
            SELECT MAX(week_end_date) AS week_end_date
            FROM (
              SELECT week_end_date FROM weekly_usage_snapshots WHERE week_start_date = ?
              UNION ALL
              SELECT week_end_date FROM weekly_cost_snapshots WHERE week_start_date = ?
              UNION ALL
              SELECT week_end_date FROM percent_milestones WHERE week_start_date = ?
            )
            """,
            (week_start_date, week_start_date, week_start_date),
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
                adjusted = c._apply_reset_events_to_weekrefs(conn, [base_ref])
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
            "WHERE week_start_date = ? AND week_end_at IS NOT NULL "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
            (week_start_date,),
        ).fetchone()
        if latest_end_row is not None:
            canon_end_for_lookup = _canonicalize_optional_iso(
                latest_end_row["week_end_at"], "pb.cur"
            )
        if canon_end_for_lookup:
            seg_row = conn.execute(
                "SELECT id FROM week_reset_events "
                "WHERE new_week_end_at = ? "
                "ORDER BY id DESC LIMIT 1",
                (canon_end_for_lookup,),
            ).fetchone()
            if seg_row is not None:
                active_segment = int(seg_row["id"])

        milestones = [
            m for m in c.get_milestones_for_week(conn, week_start_date)
            if int(m["reset_event_id"] or 0) == active_segment
        ]

        milestone_list = []
        for m in milestones:
            milestone_list.append({
                "percentThreshold": int(m["percent_threshold"]),
                "cumulativeCostUSD": round(float(m["cumulative_cost_usd"]), 9),
                "marginalCostUSD": round(float(m["marginal_cost_usd"]), 9) if m["marginal_cost_usd"] is not None else None,
                "capturedAt": m["captured_at_utc"],
                "fiveHourPercentAtCrossing": round(float(m["five_hour_percent_at_crossing"]), 1) if m["five_hour_percent_at_crossing"] is not None else None,
            })

        output = {
            "weekStartDate": week_start_date,
            "weekEndDate": week_end_date,
            "weekStartAt": display_start_iso,
            "weekEndAt": display_end_iso,
            "milestones": milestone_list,
            "generatedAt": now_utc_iso(),
        }

        if args.json:
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
        ))

        return 0
    finally:
        conn.close()
