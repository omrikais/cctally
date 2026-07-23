#!/usr/bin/env python3
"""Generate a production-shaped ~1M-line journal for the rebuild benchmark.

Task 8 Item 6 / spec §5.4 + §10: the rebuild of a >=1M-line journal must complete
within 120s on the CI runner — a MEASURED gate, not an assumption. This builder
DIRECT-WRITES a realistic mix of journal lines (deterministic; no RNG) rather than
driving the real ingest cycle: driving ~250K real ingest cost-syncs would (a) leak
cache connections (an existing cost-read artifact, invisible at production volumes)
and (b) go O(n^2) in the block-derivation sweeps as the synthetic history grows —
both fixture-generation artifacts, neither the linear stream fold the rebuild
actually runs. Direct-writing is O(n) and exercises the exact families the rebuild
folds: ``obs`` (decoded, the bulk), ``snapshot_accept`` + ``weekly_cost_snapshot``
(Model-A generic folds) and ``percent_milestone`` (a harvest family whose logical
FK refs resolve to this line's own snapshot + cost-snapshot evts). Derived cost
values are 0 (irrelevant to fold speed); the kind mix and per-line FK topology are
faithful.

The artifact is written into the APP_DIR resolved from the ambient env
(``CCTALLY_DATA_DIR`` / ``HOME``) — a SCRATCH dir; it MUST NOT enter the git tree.
It is NOT part of the normal suite: it is driven only by
tests/test_rebuild_benchmark.py under ``CCTALLY_RUN_BENCHMARK=1``, or run
standalone for a manual measurement:

    CCTALLY_DATA_DIR=/tmp/bench HOME=/tmp/bench-home \\
        python3 bin/build-journal-benchmark-fixture.py 1000000
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys


def _load_cctally():
    """Load the cctally script when executed standalone. In a pytest context the
    harness has already loaded it, so this re-uses the existing module."""
    bin_dir = str(pathlib.Path(__file__).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    if "cctally" not in sys.modules:
        from importlib.machinery import SourceFileLoader
        import importlib.util
        loader = SourceFileLoader("cctally", os.path.join(bin_dir, "cctally"))
        spec = importlib.util.spec_from_loader("cctally", loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)


# Four lines per synthetic tick: one obs + its snapshot_accept + weekly_cost_snapshot
# + percent_milestone. 60 incremental percent ticks per synthetic subscription week
# each cross a fresh weekly milestone under a unique week_start_date.
_TICKS_PER_WEEK = 60
_TICKS_PER_5H = 12
_BASE_WEEK = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)


def build(target_lines: int = 1_000_000) -> int:
    """Direct-write journal lines until at least ``target_lines`` exist. Returns
    the total line count. Deterministic; O(n)."""
    _load_cctally()
    import _cctally_core
    import _lib_journal as J

    journal_dir = _cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    seg_path = journal_dir / J.segment_name(dt.datetime.now(dt.timezone.utc))

    lines = 0
    week = 0
    # Buffered append: write all encoded lines, fsync once at the end. append
    # discipline (leaf flock / per-line fsync) is a runtime-writer contract, not
    # needed for a single-process fixture build.
    with open(seg_path, "ab", buffering=1024 * 1024) as fh:
        while lines < target_lines:
            wsd_date = (_BASE_WEEK + dt.timedelta(days=7 * week)).date()
            wsd = wsd_date.isoformat()
            wed = (wsd_date + dt.timedelta(days=7)).isoformat()
            wsa = f"{wsd}T00:00:00+00:00"
            wea = f"{wed}T00:00:00+00:00"
            for tick in range(_TICKS_PER_WEEK):
                pct = float(tick + 1)
                slot = tick // _TICKS_PER_5H
                fh_start = _BASE_WEEK + dt.timedelta(days=7 * week, hours=5 * slot)
                fh_key = int(fh_start.timestamp())
                fhr = (fh_start + dt.timedelta(hours=5)).isoformat(
                    timespec="seconds").replace("+00:00", "Z")
                block_start = fh_start.isoformat(
                    timespec="seconds").replace("+00:00", "Z")
                at = (_BASE_WEEK + dt.timedelta(
                    days=7 * week, minutes=tick)).isoformat(
                    timespec="seconds").replace("+00:00", "Z")
                fh_pct = float((tick % _TICKS_PER_5H + 1) * 8)
                oid = f"o:w{week:07d}t{tick:02d}"
                sa_id = f"sa:{oid}"
                wcs_id = f"wcs:{oid}:{wsd}"

                obs = {
                    "v": J.LINE_VERSION, "t": "obs", "id": oid, "at": at,
                    "src": "record-usage", "provider": "claude",
                    "payload": {"weekly_percent": pct, "resets_at": fh_key,
                                "source": "statusline", "captured_at": at},
                }
                sa = J.make_evt("snapshot_accept", sa_id, at, {
                    "captured_at_utc": at, "week_start_date": wsd,
                    "week_end_date": wed, "week_start_at": wsa, "week_end_at": wea,
                    "weekly_percent": pct, "source": "statusline",
                    "payload_json": "{}", "page_url": None,
                    "five_hour_percent": fh_pct,
                    "five_hour_resets_at": fhr, "five_hour_window_key": fh_key,
                })
                wcs = J.make_evt("weekly_cost_snapshot", wcs_id, at, {
                    "captured_at_utc": at, "week_start_date": wsd,
                    "week_end_date": wed, "week_start_at": wsa, "week_end_at": wea,
                    "range_start_iso": wsa, "range_end_iso": wea,
                    "cost_usd": 0.0, "mode": "auto", "project": None,
                })
                pm = J.make_evt(
                    "percent_milestone", f"pm:{wsd}:0:{tick + 1}", at, {
                        "captured_at_utc": at, "week_start_date": wsd,
                        "week_end_date": wed, "week_start_at": wsa,
                        "week_end_at": wea, "percent_threshold": tick + 1,
                        "cumulative_cost_usd": 0.0, "marginal_cost_usd": None,
                        "five_hour_percent_at_crossing": fh_pct,
                        "alerted_at": None, "usage_snapshot_ref": sa_id,
                        "cost_snapshot_ref": wcs_id, "reset_event_ref": "0",
                    })
                fhm = J.make_evt(
                    "five_hour_milestone",
                    f"fhm:{fh_key}:0:{tick % _TICKS_PER_5H + 1}", at, {
                        "captured_at_utc": at, "five_hour_window_key": fh_key,
                        "percent_threshold": tick % _TICKS_PER_5H + 1,
                        "block_input_tokens": 0, "block_output_tokens": 0,
                        "block_cache_create_tokens": 0,
                        "block_cache_read_tokens": 0, "block_cost_usd": 0.0,
                        "marginal_cost_usd": None,
                        "seven_day_pct_at_crossing": pct, "alerted_at": None,
                        "usage_snapshot_ref": sa_id, "reset_event_ref": "0",
                    })
                batch = [obs, sa, wcs, pm, fhm]
                # One five_hour_block_close per window (emitted on its first tick),
                # so EVERY window in the fixture is closed and blocks come from
                # evts (the fast fold path this benchmark measures). There is no
                # trailing OPEN window, so the rebuild's open-block
                # re-materialization pass is a near no-op here — the benchmark
                # deliberately stresses the evt fold, not the open-block edge.
                if tick % _TICKS_PER_5H == 0:
                    batch.append(J.make_evt(
                        "five_hour_block_close", f"fhbc:{fh_key}", at, {
                            "five_hour_window_key": fh_key,
                            "five_hour_resets_at": fhr,
                            "block_start_at": block_start,
                            "first_observed_at_utc": at, "last_observed_at_utc": at,
                            "final_five_hour_percent": 96.0,
                            "seven_day_pct_at_block_start": pct,
                            "seven_day_pct_at_block_end": pct,
                            "crossed_seven_day_reset": 0,
                            "total_input_tokens": 0, "total_output_tokens": 0,
                            "total_cache_create_tokens": 0,
                            "total_cache_read_tokens": 0, "total_cost_usd": 0.0,
                            "is_closed": 1, "created_at_utc": at,
                            "last_updated_at_utc": at, "_models": [], "_projects": [],
                        }))
                for rec in batch:
                    fh.write(J.encode_line(rec))
                lines += len(batch)
            week += 1
        fh.flush()
        os.fsync(fh.fileno())
    return lines


if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    total = build(target)
    import _cctally_journal
    print(f"journal built: {total} lines across "
          f"{len(_cctally_journal.list_segments())} segment(s)")
