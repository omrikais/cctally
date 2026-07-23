"""`cctally sync-week` subcommand entry point.

Lazy I/O sibling: holds the single entry-point function `cmd_sync_week`,
which loads config + selects the target subscription week +
JSONL-aggregates the week's cost + inserts a `weekly_cost_snapshots`
row + emits the success line (or `--json` envelope).

Every helper this command calls — `load_config`, `get_week_start_name`,
`open_db`, `pick_week_selection`, `format_local_iso`, `make_week_ref`,
`get_latest_usage_for_week` — stays in `bin/cctally` (they're shared
with the rest of the subcommand surface); `compute_week_cost` /
`insert_cost_snapshot` now live in `_cctally_milestones.py` (re-exported on
the ns). All are reached via the `_cctally()` call-time accessor (spec
§5.2 / §5.5 pattern).

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
    now_utc_iso,
)


def cmd_sync_week(
    args: argparse.Namespace, *, conn=None, as_of: "str | None" = None,
    journal: "tuple | None" = None,
) -> int:
    """Compute + persist the week's cost snapshot.

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    ``conn`` runs the cost-snapshot insert on the caller's connection (no
    internal open/commit/close), and ``as_of`` (ISO-Z) stamps the snapshot's
    ``captured_at_utc``. Both defaults keep the legacy own-connection,
    wall-clock behavior — so `record`/`maybe_record_milestone`'s existing
    bare `cmd_sync_week(ns)` calls are byte-identical.

    Design A (DB journal redesign §5.3, Model-A ``weekly_cost_snapshot``):
    ``journal=(ctx, id_base)`` routes the ``insert_cost_snapshot`` THROUGH
    ``emit_model_a`` so the computed cost rides a journaled evt
    (``wcs:<id_base>:<week>``) and replay reads it back verbatim rather than
    recomputing from the (pruned) provider JSONL. Threaded by the
    milestone-cost-sync (id_base = the obs line id) and the sync-week op fold
    (id_base = the op line id). Default ``None`` keeps the bare insert.

    6f writer reroute (Appendix A): the pure-CLI / bare-call path (``conn`` is
    None AND ``journal`` is None — the ``sync-week`` subcommand entry and the
    ``report --sync-current`` lazy-sync) no longer computes + inserts on its own
    connection. It appends a ``sync_week`` ``op`` line and runs an AUTHORITATIVE
    ingest — ``_pipeline_sync_week`` re-derives args from the op payload and
    re-enters THIS function with ``conn``+``journal`` set (the inline body below),
    which computes the cost on the cycle connection and journals the
    ``weekly_cost_snapshots`` row — then reads that row back for its output. The
    passed-conn / journal callers stay inline and byte-identical."""
    c = _cctally()
    if conn is None and journal is None:
        return _cmd_sync_week_via_ingest(c, args)
    config = c.load_config()
    week_start_name = get_week_start_name(config, args.week_start_name)

    own_conn = conn is None
    if own_conn:
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
            commit=own_conn,
            as_of=as_of,
            journal=journal,
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
            print(json.dumps(c.stamp_schema_version(payload), indent=2))
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
        if own_conn:
            conn.close()


def _cmd_sync_week_via_ingest(c, args: argparse.Namespace) -> int:
    """CLI / bare-call sync-week (6f writer reroute): append a ``sync_week`` op
    + AUTHORITATIVE ingest, then read the journaled ``weekly_cost_snapshots`` row
    back for output. The op carries the raw ``sync-week`` args; the
    ``_pipeline_sync_week`` hook re-enters ``cmd_sync_week`` with the cycle
    connection to compute + journal the cost (Model-A ``wcs:<op id>:<week>``).

    The command observes its own write synchronously (authoritative ingest, then
    a fresh read-back). Exit codes stay byte-identical: a genuine compute/insert
    failure propagates out of the authoritative ingest exactly as the old inline
    path let ``compute_week_cost``/``insert_cost_snapshot`` raise, and the CLI
    dispatch maps it to the same exit code."""
    import _cctally_journal as _jr
    import _lib_journal as _lj

    op = _lj.make_op(
        at=now_utc_iso(),
        src="sync-week",
        payload={
            "kind": "sync_week",
            "week_start": args.week_start,
            "week_end": args.week_end,
            "week_start_name": args.week_start_name,
            "mode": args.mode,
            "offline": args.offline,
            "project": args.project,
        },
    )
    _jr.append_record(op)
    res = _jr.run_stats_ingest(mode="authoritative")
    # authoritative blocks up to the ingest-lock timeout; on the rare timeout
    # (busy lock) it returns ran=False without consuming the op — retry once so
    # the CLI observes its own write.
    if not res.ran:
        res = _jr.run_stats_ingest(mode="authoritative")

    conn = open_db()
    try:
        row = conn.execute(
            "SELECT id, week_start_date, week_end_date, week_start_at, "
            "       week_end_at, range_start_iso, range_end_iso, cost_usd, mode "
            "  FROM weekly_cost_snapshots "
            " WHERE journal_id LIKE ? "
            " ORDER BY id DESC LIMIT 1",
            (f"wcs:{op['id']}:%",),
        ).fetchone()
        if row is None:
            # The op did not journal a cost row (only reachable if the ingest
            # never ran — a sustained ingest-lock timeout). Surface it rather
            # than printing a phantom success.
            import sys
            print(
                "sync-week: cost ingest did not run (ingest lock busy); retry",
                file=sys.stderr,
            )
            return 3

        (row_id, wsd, wed, wsa, wea, r_start, r_end, cost_usd, _mode) = row
        week_ref = make_week_ref(
            week_start_date=wsd,
            week_end_date=wed,
            week_start_at=wsa,
            week_end_at=wea,
        )
        usage_row = get_latest_usage_for_week(conn, week_ref)
        weekly_percent = float(usage_row["weekly_percent"]) if usage_row else None
        dollars_per_percent = (
            cost_usd / weekly_percent if weekly_percent and weekly_percent > 0 else None
        )

        payload = {
            "id": row_id,
            "weekStartDate": wsd,
            "weekEndDate": wed,
            "weekStartAt": wsa,
            "weekEndAt": wea,
            "rangeStartIso": r_start,
            "rangeEndIso": r_end,
            "costUSD": round(cost_usd, 9),
            "weeklyPercent": weekly_percent,
            "dollarsPerPercent": round(dollars_per_percent, 9) if dollars_per_percent is not None else None,
        }

        if args.json:
            print(json.dumps(c.stamp_schema_version(payload), indent=2))
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
