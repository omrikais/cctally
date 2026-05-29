#!/usr/bin/env python3
"""Build deterministic fixtures for bin/cctally-pricing-check-test.

The pricing-check harness drives `cctally pricing-check` against a scratch
HOME with the hidden env hooks (CCTALLY_PRICING_LITELLM_FILE /
CCTALLY_PRICING_MODELS_FILE) injecting local snapshots — no test hits the
network (spec invariant #4). This builder emits the input artifacts each
scenario consumes:

  - cache.db files seeded with session entries (an unpriced Claude model
    drives the offline coverage gap).
  - LiteLLM inject JSON (a diverging value drives `value_drift`).
  - /v1/models inject JSON (Anthropic vendor list).

Run: `python3 bin/build-pricing-check-fixtures.py`
Goldens are (re)generated separately via `CCTALLY_PRICING_CHECK_REGENERATE=1
bin/cctally-pricing-check-test`.

Cache DBs are written WAL (the "Fixture SQLite DBs must PRAGMA
journal_mode=WAL" gotcha) and registered via _fixture_builders so the
SQLite writer-version field is normalized on exit (keeps the committed .db
byte-stable across hosts). Per-scenario .gitignore covers *-wal/*-shm.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import _fixture_builders

ROOT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "pricing-check"

# The Claude model we deliberately do NOT price (resolves to $0 → coverage
# gap of kind "unpriced"). Stable, obviously-synthetic name.
_UNPRICED_CLAUDE = "claude-fixture-unpriced-9000"
# A real, priced model + a real value we DIVERGE from in the drift inject so
# diff_pricing reports value_drift. Kept in sync with CLAUDE_MODEL_PRICING.
_PRICED_CLAUDE = "claude-3-5-haiku-20241022"
_PRICED_CLAUDE_INPUT = 8e-07          # the embedded value
_DRIFT_CLAUDE_INPUT = 9.99e-07        # the diverging LiteLLM value


def _seed_cache(db_path: pathlib.Path, *, unpriced: bool) -> None:
    """Create a WAL cache.db. When ``unpriced`` is True, seed ONE entry for a
    model cctally cannot price (offline coverage gap). When False, leave the
    table empty so coverage is []. Columns mirror
    bin/_cctally_db.py::_apply_cache_schema (cache_create_tokens, NOT
    cache_creation_tokens)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Idempotent rebuild: the harness re-runs this builder on every invocation,
    # so a committed cache.db must be deterministic regardless of how many
    # times it's (re)built. Start from a clean file.
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            p.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                line_offset INTEGER NOT NULL,
                timestamp_utc TEXT NOT NULL,
                model TEXT NOT NULL,
                msg_id TEXT, req_id TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                usage_extra_json TEXT, cost_usd_raw REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS codex_session_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                line_offset INTEGER NOT NULL,
                timestamp_utc TEXT NOT NULL,
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        if unpriced:
            # Timestamp is irrelevant to the subcommand's coverage leg (it's
            # ALL-HISTORY, since=None), but we use a fixed value for clarity.
            conn.execute(
                "INSERT INTO session_entries(source_path, line_offset, "
                "timestamp_utc, model, input_tokens, output_tokens, "
                "cache_create_tokens, cache_read_tokens) "
                "VALUES (?, 0, ?, ?, ?, ?, ?, ?)",
                ("/fixture/unpriced.jsonl", "2026-05-01T00:00:00Z",
                 _UNPRICED_CLAUDE, 1000, 200, 50, 10),
            )
        conn.commit()
    finally:
        conn.close()
    _fixture_builders.register_fixture_db(db_path)


def _write_json(path: pathlib.Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _write_gitignore(scenario_dir: pathlib.Path) -> None:
    # Fixture-scoped gitignore (the "spawns WAL files" gotcha): the harness
    # runs cctally against a SCRATCH copy, but a stray WAL/SHM beside the
    # committed cache.db (e.g. if someone opens it) must never be committed.
    (scenario_dir / ".gitignore").write_text(
        "# Generated by bin/build-pricing-check-fixtures.py\n"
        "*.db-wal\n"
        "*.db-shm\n"
    )


def main() -> None:
    # offline_clean: empty cache → coverage [], exit 0.
    d = ROOT / "offline_clean"
    _seed_cache(d / "cache.db", unpriced=False)
    _write_gitignore(d)

    # offline_finding: unpriced Claude model → coverage gap, exit 1.
    d = ROOT / "offline_finding"
    _seed_cache(d / "cache.db", unpriced=True)
    _write_gitignore(d)

    # drift_found: clean cache + a LiteLLM inject that diverges on one value.
    # A valid (empty) /v1/models inject keeps the existence leg ok so ONLY the
    # drift drives exit 1 and status stays "ok".
    d = ROOT / "drift_found"
    _seed_cache(d / "cache.db", unpriced=False)
    _write_json(d / "litellm.json", {
        _PRICED_CLAUDE: {
            "litellm_provider": "anthropic",
            "input_cost_per_token": _DRIFT_CLAUDE_INPUT,
        },
    })
    _write_json(d / "models.json", {"data": []})
    _write_gitignore(d)

    # degraded_clean: clean cache + UNREACHABLE LiteLLM (a directory path that
    # can't be read as JSON) + unreachable models file → both legs degrade,
    # nothing actionable → exit 0, status degraded. The harness points the env
    # hooks at /nonexistent paths; no inject file needed here, but seed the
    # empty cache so coverage is [].
    d = ROOT / "degraded_clean"
    _seed_cache(d / "cache.db", unpriced=False)
    _write_gitignore(d)

    # finding_while_degraded: diverging LiteLLM (drift → finding) + an
    # unreachable /v1/models file (existence degraded). Proves precedence:
    # exit 1 (finding wins) AND status degraded.
    d = ROOT / "finding_while_degraded"
    _seed_cache(d / "cache.db", unpriced=False)
    _write_json(d / "litellm.json", {
        _PRICED_CLAUDE: {
            "litellm_provider": "anthropic",
            "input_cost_per_token": _DRIFT_CLAUDE_INPUT,
        },
    })
    _write_gitignore(d)

    print(f"pricing-check fixtures built under {ROOT}")


if __name__ == "__main__":
    main()
