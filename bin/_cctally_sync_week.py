"""`cctally sync-week` subcommand entry point.

Lazy I/O sibling: holds the single entry-point function `cmd_sync_week`,
which loads config + selects the target subscription week +
JSONL-aggregates the week's cost + inserts a `weekly_cost_snapshots`
row + emits the success line (or `--json` envelope).

Every helper this command calls — `load_config`, `get_week_start_name`,
`open_db`, `pick_week_selection`, `compute_week_cost`,
`format_local_iso`, `insert_cost_snapshot`, `make_week_ref`,
`get_latest_usage_for_week` — stays in `bin/cctally` (they're shared
with the rest of the subcommand surface), reached via the `_cctally()`
call-time accessor (spec §5.2 / §5.5 pattern).

bin/cctally re-exports `cmd_sync_week` so the two non-extracted internal
callers (`cmd_record_usage`'s milestone-cost-sync path and the
`cmd_report` lazy-sync path) resolve via bare name unchanged.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import json
import sys


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3: kernel symbols
# import from _cctally_core. `load_config` stays on the _cctally()
# accessor per spec §3.5 monkeypatch carve-out. Z-high helpers
# (`compute_week_cost`, `insert_cost_snapshot`) and out-of-scope
# (`pick_week_selection`) stay on the accessor per spec §3.7.
from _cctally_core import (
    get_week_start_name,
    open_db,
    format_local_iso,
    make_week_ref,
    get_latest_usage_for_week,
)


def cmd_sync_week(args: argparse.Namespace) -> int:
    c = _cctally()
    config = c.load_config()
    week_start_name = get_week_start_name(config, args.week_start_name)

    conn = open_db()
    try:
        selection = c.pick_week_selection(
            conn,
            args.week_start,
            args.week_end,
            week_start_name,
        )
        week_start = selection.week_start
        week_end = selection.week_end
        result = c.compute_week_cost(
            week_start=week_start,
            week_end=week_end,
            mode=args.mode,
            offline=args.offline,
            project=args.project,
            start_iso_override=selection.start_iso_override,
            end_iso_override=selection.end_iso_override,
        )
        week_start_at = selection.start_iso_override or format_local_iso(week_start, end_of_day=False)
        week_end_at = selection.end_iso_override or format_local_iso(week_end, end_of_day=True)
        insert_id = c.insert_cost_snapshot(
            conn,
            week_start=week_start,
            week_end=week_end,
            week_start_at=week_start_at,
            week_end_at=week_end_at,
            range_start_iso=result.start_iso,
            range_end_iso=result.end_iso,
            cost_usd=result.cost_usd,
            mode=args.mode,
            project=args.project,
        )

        week_ref = make_week_ref(
            week_start_date=week_start.isoformat(),
            week_end_date=week_end.isoformat(),
            week_start_at=week_start_at,
            week_end_at=week_end_at,
        )
        usage_row = get_latest_usage_for_week(conn, week_ref)
        weekly_percent = float(usage_row["weekly_percent"]) if usage_row else None
        dollars_per_percent = (
            result.cost_usd / weekly_percent if weekly_percent and weekly_percent > 0 else None
        )

        payload = {
            "id": insert_id,
            "weekStartDate": week_start.isoformat(),
            "weekEndDate": week_end.isoformat(),
            "weekStartAt": week_start_at,
            "weekEndAt": week_end_at,
            "rangeStartIso": result.start_iso,
            "rangeEndIso": result.end_iso,
            "costUSD": round(result.cost_usd, 9),
            "weeklyPercent": weekly_percent,
            "dollarsPerPercent": round(dollars_per_percent, 9) if dollars_per_percent is not None else None,
        }

        if args.json:
            print(json.dumps(payload, indent=2))
        elif not args.quiet:
            print(
                f"Synced week {payload['weekStartDate']}..{payload['weekEndDate']} "
                f"=> ${payload['costUSD']:.6f}"
            )
            if weekly_percent is not None:
                print(f"Latest weekly usage: {weekly_percent:.2f}%")
            if dollars_per_percent is not None:
                print(f"$ per 1% usage: ${dollars_per_percent:.6f}")
        return 0
    finally:
        conn.close()
