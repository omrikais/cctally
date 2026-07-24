#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `bin/cctally-account-test` (#341 Task 3).

Seeds a >1-real-account world (two real Claude accounts — `alice`, `bob` — plus
the implicit `unattributed` bucket) so the harness can byte-compare the account
registry renders (`account list` table + `--json`), `account show`, the
`--account`-scoped analytics render (`range-cost`), and the merged default.

Direct-seed render fixture (the established `build-*-fixtures.py` convention):
the `accounts` registry rows, per-account `weekly_usage_snapshots` (for the
`account show` attribution counts), and per-account `session_entries` (for the
`--account` cost scoping) are inserted straight into stats.db / cache.db, then
every stats migration is stamped applied (render-fixture rule — keeps a read
command's sync from flipping the recompute gate). The JOURNAL-derivation path
(rebuild_stats_index folding `account_observe` ops, honoring the two-shaped
stamp) is covered by pytest `test_account_cli.py`; a render fixture doesn't
exercise the journal.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib_accounts  # noqa: E402
from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    seed_account,
    seed_session_entry,
    seed_weekly_cost_snapshot,
    seed_weekly_usage_snapshot,
    stamp_all_stats_migrations_applied,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/account"

ALICE = _lib_accounts.account_key("claude", "uuid-alice")
BOB = _lib_accounts.account_key("claude", "uuid-bob")


def _seed(db_dir: Path) -> None:
    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")
    conn = sqlite3.connect(db_dir / "stats.db")
    try:
        # The `accounts` registry table is now created by `create_stats_db`
        # (single-source DDL, byte-matching production `_cctally_core.py`; #341
        # slice-D item 5). No inline CREATE here.
        seed_account(conn, account_key=ALICE, provider="claude",
                     natural_id="uuid-alice", email="alice@example.com",
                     label="alice", plan_type="max", label_source="auto",
                     first_seen_utc="2026-05-01T00:00:00Z",
                     last_seen_utc="2026-05-20T00:00:00Z")
        seed_account(conn, account_key=BOB, provider="claude",
                     natural_id="uuid-bob", email="bob@example.com",
                     label="bob", plan_type="pro", label_source="auto",
                     first_seen_utc="2026-05-02T00:00:00Z",
                     last_seen_utc="2026-05-21T00:00:00Z")
        # Per-account usage snapshots (attribution counts: alice=2, bob=1).
        for cap, pct in (("2026-05-18T12:00:00Z", 20.0),
                         ("2026-05-19T12:00:00Z", 40.0)):
            seed_weekly_usage_snapshot(
                conn, captured_at_utc=cap, week_start_date="2026-05-18",
                week_end_date="2026-05-25", week_start_at="2026-05-18T00:00:00Z",
                week_end_at="2026-05-25T00:00:00Z", weekly_percent=pct,
                account_key=ALICE)
        seed_weekly_usage_snapshot(
            conn, captured_at_utc="2026-05-18T13:00:00Z",
            week_start_date="2026-05-18", week_end_date="2026-05-25",
            week_start_at="2026-05-18T00:00:00Z", week_end_at="2026-05-25T00:00:00Z",
            weekly_percent=15.0, account_key=BOB)
        # Per-account weekly COST snapshots (report joins usage+cost per week for
        # the $/1% trend; the `--account` render reads the account's snapshot).
        seed_weekly_cost_snapshot(
            conn, captured_at_utc="2026-05-19T12:00:00Z",
            week_start_date="2026-05-18", week_end_date="2026-05-25",
            week_start_at="2026-05-18T00:00:00Z", week_end_at="2026-05-25T00:00:00Z",
            cost_usd=5.0, account_key=ALICE)
        seed_weekly_cost_snapshot(
            conn, captured_at_utc="2026-05-18T13:00:00Z",
            week_start_date="2026-05-18", week_end_date="2026-05-25",
            week_start_at="2026-05-18T00:00:00Z", week_end_at="2026-05-25T00:00:00Z",
            cost_usd=2.0, account_key=BOB)
        # Per-account 5h blocks sharing ONE physical window (five_hour_window_key)
        # — the account dimension owns a block EACH (UNIQUE(account_key, window)),
        # so `five-hour-blocks --account alice` renders alice's block only. Raw
        # INSERT (no seed_ helper for five_hour_blocks yet); columns == the
        # create_stats_db DDL.
        for _acct, _pct, _cost in ((ALICE, 30.0, 1.0), (BOB, 12.0, 0.5)):
            conn.execute(
                """INSERT INTO five_hour_blocks
                   (five_hour_window_key, five_hour_resets_at, block_start_at,
                    first_observed_at_utc, last_observed_at_utc,
                    final_five_hour_percent, seven_day_pct_at_block_start,
                    seven_day_pct_at_block_end, crossed_seven_day_reset,
                    total_input_tokens, total_output_tokens,
                    total_cache_create_tokens, total_cache_read_tokens,
                    total_cost_usd, is_closed, created_at_utc, last_updated_at_utc,
                    account_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (340000, "2026-05-22T15:00:00Z", "2026-05-22T10:00:00Z",
                 "2026-05-22T10:05:00Z", "2026-05-22T14:30:00Z",
                 _pct, None, None, 0, 100, 200, 0, 0, _cost, 1,
                 "2026-05-22T10:05:00Z", "2026-05-22T14:30:00Z", _acct),
            )
        stamp_all_stats_migrations_applied(conn)
        conn.commit()
    finally:
        conn.close()

    # Per-account stamped entries for the `--account` cost scoping (alice=$1,
    # bob=$2 → merged $3; --account alice → $1).
    cache = sqlite3.connect(db_dir / "cache.db")
    try:
        seed_session_entry(
            cache, source_path="/p/alice.jsonl", line_offset=0,
            timestamp_utc="2026-05-22T12:00:00Z", model="claude-opus-4-7",
            msg_id="mA", req_id="rA", input_tokens=10, output_tokens=20,
            cost_usd_raw=1.0, account_key=ALICE)
        seed_session_entry(
            cache, source_path="/p/bob.jsonl", line_offset=0,
            timestamp_utc="2026-05-22T12:00:00Z", model="claude-opus-4-7",
            msg_id="mB", req_id="rB", input_tokens=10, output_tokens=20,
            cost_usd_raw=2.0, account_key=BOB)
        cache.commit()
    finally:
        cache.close()


def build_two_real(out: Path) -> None:
    d = out / "two-real"
    db_dir = d / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)
    _seed(db_dir)
    (db_dir / "config.json").write_text(
        json.dumps({"display": {"tz": "utc"}}, indent=2) + "\n")
    (d / "input.env").write_text('AS_OF="2026-05-22T18:00:00Z"\n')


def main() -> int:
    global FIXTURES_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_DIR = args.out
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    build_two_real(FIXTURES_DIR)
    print(f"Built account fixtures under {FIXTURES_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
