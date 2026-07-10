#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally daily` (#279 S7 W6).

Writes one (stats.db, cache.db) pair per scenario under
tests/fixtures/daily/<scenario>/.local/share/cctally/. All schema/seeding goes
through bin/_fixture_builders.py — no schema is duplicated here. Covers the base
per-day table path (distinct from `daily --instances`, which stays
bin/cctally-daily-instances-test's surface).

The seeded session_entries carry NO recorded cost (cost_usd_raw NULL), so the
`-m/--mode` split is clean: `calculate` ≡ default `auto` (cost computed from
pricing either way → same golden), while `display` drops every cost-less entry
to $0 (a distinct golden proving the flag reaches the per-entry cost path).
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
    seed_session_entry,
    seed_session_file,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/daily"

# One opus-4-7 output-token count per day → deterministic per-day cost at the
# embedded $25/Mtok opus output rate. Days span a 3-day window so --since/--until
# can slice it.
_SESSION = "/fake/.claude/projects/-fake-proj/sess-a.jsonl"
_MODEL = "claude-opus-4-7"


def _seed(db_dir: Path) -> None:
    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")
    conn = sqlite3.connect(db_dir / "cache.db")
    try:
        seed_session_file(
            conn, path=_SESSION, session_id="sess-a", project_path="/fake/proj")
        # Three days, one entry each (output tokens differ so days are distinct).
        for i, (ts, out) in enumerate([
            ("2026-04-13T12:00:00Z", 1000),
            ("2026-04-14T12:00:00Z", 2000),
            ("2026-04-15T12:00:00Z", 3000),
        ]):
            seed_session_entry(
                conn, source_path=_SESSION, line_offset=i, timestamp_utc=ts,
                model=_MODEL, input_tokens=100, output_tokens=out)
        conn.commit()
    finally:
        conn.close()


def build_base(out: Path) -> None:
    d = out / "base"
    db_dir = d / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)
    _seed(db_dir)
    (d / "input.env").write_text('AS_OF="2026-04-15T18:00:00Z"\n')


def build_since_window(out: Path) -> None:
    d = out / "since-window"
    db_dir = d / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)
    _seed(db_dir)
    # Windowed to the middle day only (display-tz date parsing; TZ=Etc/UTC).
    (d / "input.env").write_text(
        'AS_OF="2026-04-15T18:00:00Z"\nFLAGS="--since 2026-04-14 --until 2026-04-14"\n')


def main() -> int:
    global FIXTURES_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_DIR = args.out
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    build_base(FIXTURES_DIR)
    build_since_window(FIXTURES_DIR)
    print(f"Built daily fixtures under {FIXTURES_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
