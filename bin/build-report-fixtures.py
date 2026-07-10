#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally report` (a.k.a. dollar-per-percent)
(#279 S7 W6).

`report` joins weekly_usage_snapshots + weekly_cost_snapshots per subscription
week to build the $/1%-weekly trend table, and — unlike `weekly` — reads the
SNAPSHOTTED cost (not a live recompute from cache). It opens stats.db through
the real dispatcher, so this builder stamps every stats migration applied
(_fixture_builders.stamp_all_stats_migrations_applied — the render-fixture rule,
docs/migrations-gotchas.md): otherwise a read command's sync_cache would let the
008/009/010 recompute gate PROCEED and zero the seeded display tables.

All schema/seeding via bin/_fixture_builders.py.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    seed_weekly_cost_snapshot,
    seed_weekly_usage_snapshot,
    stamp_all_stats_migrations_applied,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/report"

# Two consecutive subscription weeks, each with a usage% + a snapshotted cost so
# the $/1% column is well-defined and distinct per week.
_WEEKS = [
    # (captured, week_start_date, week_end_date, week_start_at, week_end_at, pct, cost)
    ("2026-04-06T12:00:00Z", "2026-04-06", "2026-04-13",
     "2026-04-06T00:00:00Z", "2026-04-13T00:00:00Z", 20.0, 40.0),
    ("2026-04-13T12:00:00Z", "2026-04-13", "2026-04-20",
     "2026-04-13T00:00:00Z", "2026-04-20T00:00:00Z", 50.0, 130.0),
]


def _seed(db_dir: Path) -> None:
    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")
    conn = sqlite3.connect(db_dir / "stats.db")
    try:
        for cap, wsd, wed, wsa, wea, pct, cost in _WEEKS:
            seed_weekly_usage_snapshot(
                conn, captured_at_utc=cap, week_start_date=wsd, week_end_date=wed,
                week_start_at=wsa, week_end_at=wea, weekly_percent=pct)
            seed_weekly_cost_snapshot(
                conn, captured_at_utc=cap, week_start_date=wsd, week_end_date=wed,
                week_start_at=wsa, week_end_at=wea, cost_usd=cost)
        # Render-fixture rule: fast-path the dispatcher so a read command's
        # sync_cache can't flip the recompute gate to PROCEED and zero the
        # seeded display tables.
        stamp_all_stats_migrations_applied(conn)
        conn.commit()
    finally:
        conn.close()


def build_base(out: Path) -> None:
    d = out / "base"
    db_dir = d / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)
    _seed(db_dir)
    (d / "input.env").write_text(
        'AS_OF="2026-04-20T18:00:00Z"\nFLAGS="--weeks 4"\n')


def main() -> int:
    global FIXTURES_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_DIR = args.out
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    build_base(FIXTURES_DIR)
    print(f"Built report fixtures under {FIXTURES_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
