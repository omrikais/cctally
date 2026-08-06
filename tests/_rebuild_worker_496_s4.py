#!/usr/bin/env python3
"""#496 S4 — one `rebuild_stats_index` in a FRESH subprocess, dumped canonically.

Isolation is not cosmetic here. Quota insertion is `INSERT OR IGNORE` on a
natural key, so a comparison run against a cache.db a previous run already
populated would let a replay that wrote NOTHING reproduce the expected rows.
Every comparison therefore gets its own process, its own cloned seeded cache.db
and its own destination, with the target quota rows initially absent (spec
§8.5). `ru_maxrss` also cannot be reset, so building a fixture and measuring a
rebuild in one process reports a contaminated resident peak (spec §3).

Invoked as:  python3 tests/_rebuild_worker_496_s4.py <config.json>

The config selects what to build, how to pin the high-water, and where to write
the resulting dump. It is deliberately a separate file from the test module so
pytest never collects it.
"""
from __future__ import annotations

import json
import os
import pathlib
import resource
import sys
import tracemalloc


_REPO = pathlib.Path(__file__).resolve().parent.parent


def _rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def _load_builder():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_journal_benchmark_fixture",
        str(_REPO / "bin" / "build-journal-benchmark-fixture.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dump_tables(conn, tables) -> dict:
    out: dict = {}
    for table in tables:
        try:
            cursor = conn.execute(f"SELECT * FROM {table}")
        except Exception:
            out[table] = None
            continue
        columns = [d[0] for d in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        rows.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
        out[table] = rows
    return out


def main() -> int:
    config = json.loads(pathlib.Path(sys.argv[1]).read_text())

    data_dir = pathlib.Path(config["data_dir"])
    home = pathlib.Path(config["home"])
    for path in (data_dir, home):
        path.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    os.environ["CCTALLY_DATA_DIR"] = str(data_dir)
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    os.environ["CCTALLY_DISABLE_TELEMETRY"] = "1"
    os.environ.setdefault("TZ", "Etc/UTC")

    sys.path.insert(0, str(_REPO / "bin"))
    builder = _load_builder()
    builder._load_cctally()

    import _cctally_core
    import _cctally_journal as jr
    import _cctally_store

    shape = None
    if config.get("build"):
        shape = builder.build(**config["build"])

    trace = bool(config.get("tracemalloc"))
    if trace:
        tracemalloc.start()

    high_water = jr.journal_high_water()
    if config.get("pin") == "before-cutover":
        high_water = _pin_before_cutover(jr, _cctally_core)

    dest = data_dir / "rebuilt.db"
    # A pre-existing readable destination, so S3's in-place generation
    # publication executes rather than the physical-replacement fallback.
    _cctally_core.open_db(_target_path=str(dest)).close()

    # The same sanctioned scope `db rebuild --db stats` enters (#386). Without
    # it the scratch connection's authorizer denies the fold's first mutation,
    # and under a pytest-inherited environment that denial RAISES rather than
    # logging — so the worker has to model the real caller, not just the call.
    with _cctally_store.stats_write_scope("maintenance-rebuild"):
        result = jr.rebuild_stats_index(
            context=jr.RebuildContext(trigger="test-fixture"),
            target_path=str(dest),
            high_water=high_water,
            update_quota_cache=not config.get("no_quota_cache"),
        )

    traced_peak = tracemalloc.get_traced_memory()[1] if trace else 0
    if trace:
        tracemalloc.stop()

    import sqlite3

    payload: dict = {
        "shape": shape,
        "high_water": list(high_water) if high_water is not None else None,
        "rows_by_table": result.rows_by_table,
        "lines_folded": result.lines_folded,
        "malformed": result.malformed,
        "segments_read": result.segments_read,
        "conflicts": [
            {"event_id": c.event_id, "rev": c.rev} for c in result.conflicts
        ],
        "protocol_violations": [
            v.to_dict() for v in result.protocol_violations
        ],
        "acknowledged_protocol_violations": [
            v.to_dict() for v in result.acknowledged_protocol_violations
        ],
        "rss_peak_bytes": _rss_bytes(),
        "traced_peak_bytes": traced_peak,
    }
    for attr in ("phase_seconds", "traversal", "peak_heap_bytes",
                 "quota_lock_hold_seconds"):
        if hasattr(result, attr):
            payload[attr] = getattr(result, attr)

    stats = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        payload["stats"] = _dump_tables(stats, list(jr._REBUILD_COUNT_TABLES))
        payload["journal_effective_events"] = [
            list(row) for row in stats.execute(
                "SELECT event_id, rev, status, content_hash, batch_id, "
                "event_json FROM journal_effective_events ORDER BY event_id")
        ]
        payload["journal_protocol_violations"] = [
            str(row[0]) for row in stats.execute(
                "SELECT violation_json FROM journal_protocol_violations "
                "ORDER BY batch_id, kind, fingerprint")
        ]
        payload["accounts_last_seen"] = [
            list(row) for row in stats.execute(
                "SELECT account_key, last_seen_utc FROM accounts "
                "ORDER BY account_key")
        ]
    finally:
        stats.close()

    cache_path = _cctally_core.CACHE_DB_PATH
    if cache_path.exists():
        cache = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
        try:
            payload["cache"] = _dump_tables(
                cache,
                ["quota_window_snapshots", "codex_file_accounts",
                 "codex_file_incarnations"],
            )
        finally:
            cache.close()
    else:
        payload["cache"] = None

    pathlib.Path(config["out"]).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    return 0


def _pin_before_cutover(jr, core):
    """The line boundary immediately BEFORE the canonical cutover op.

    Placement alone never reaches the §5.1 suffix fallback: an unpinned rebuild
    always uses the current full high-water, which contains the op. Pinning here
    is what exercises it.
    """
    import _lib_journal as J

    for segment in jr.list_segments():
        path = core.JOURNAL_DIR / segment
        offset = 0
        for _name, line_offset, raw in jr._iter_segment_lines(
            path, 0, os.path.getsize(path)
        ):
            record = J.decode_line(raw)
            if record is not None and record.get("id") == jr.CUTOVER_OP_ID:
                return (segment, line_offset)
            offset = line_offset + len(raw) + 1
        del offset
    raise SystemExit("no cutover op in the fixture journal")


if __name__ == "__main__":
    sys.exit(main())
