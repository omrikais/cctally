"""V1-style regression for stats migration 009: the
``[block_start_at, last_observed_at_utc]`` interval is CLOSED on both
sides, matching the live writer (``_compute_block_totals`` walks
``session_entries`` with ``timestamp >= block_start AND <= range_end``).

A pre-fix half-open ``<`` end would silently exclude any
``session_entries`` row whose ``timestamp_utc`` exactly equalled a
block's ``last_observed_at_utc`` — and ``last_observed_at_utc`` IS the
timestamp of some live status-line tick, so the boundary lands on a
real entry with high probability.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3 (B1).
"""
from __future__ import annotations

import importlib.util as _ilu
import pathlib
import sqlite3
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def _load_db():
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    for _name in [
        n for n in list(sys.modules)
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]:
        del sys.modules[_name]
    spec = _ilu.spec_from_file_location(
        "_cctally_db", BIN_DIR / "_cctally_db.py"
    )
    mod = _ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _pin_resolver_to_fake_home(core, tmp_path, monkeypatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    core._init_paths_from_env()


def test_009_boundary_entries_at_block_start_and_last_observed_included(
    tmp_path, monkeypatch,
):
    """Entries whose timestamp_utc EQUALS ``block_start_at`` OR
    ``last_observed_at_utc`` must be INCLUDED in the recompute (closed
    interval on both ends).
    """
    db = _load_db()
    core = db._cctally_core
    _pin_resolver_to_fake_home(core, tmp_path, monkeypatch)
    projects = tmp_path / "claude_projects"
    projects.mkdir()
    (projects / "x.jsonl").write_text("{}\n")
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", projects)

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    block_start_iso = "2026-05-18T00:00:00+00:00"
    last_obs_iso = "2026-05-18T04:50:00+00:00"

    # stats.db with one block carrying inflated pre-dedup totals.
    stats = sqlite3.connect(stats_path)
    try:
        stats.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
            );
            CREATE TABLE schema_migrations_skipped (
                name TEXT PRIMARY KEY,
                skipped_at_utc TEXT,
                reason TEXT
            );
            CREATE TABLE five_hour_blocks (
                id                            INTEGER PRIMARY KEY AUTOINCREMENT,
                five_hour_window_key          INTEGER NOT NULL UNIQUE,
                five_hour_resets_at           TEXT    NOT NULL,
                block_start_at                TEXT    NOT NULL,
                first_observed_at_utc         TEXT    NOT NULL,
                last_observed_at_utc          TEXT    NOT NULL,
                final_five_hour_percent       REAL    NOT NULL,
                seven_day_pct_at_block_start  REAL,
                seven_day_pct_at_block_end    REAL,
                crossed_seven_day_reset       INTEGER NOT NULL DEFAULT 0,
                total_input_tokens            INTEGER NOT NULL DEFAULT 0,
                total_output_tokens           INTEGER NOT NULL DEFAULT 0,
                total_cache_create_tokens     INTEGER NOT NULL DEFAULT 0,
                total_cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
                total_cost_usd                REAL    NOT NULL DEFAULT 0,
                is_closed                     INTEGER NOT NULL DEFAULT 0,
                created_at_utc                TEXT    NOT NULL,
                last_updated_at_utc           TEXT    NOT NULL
            );
            CREATE TABLE five_hour_block_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id INTEGER NOT NULL,
                five_hour_window_key INTEGER NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                entry_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(five_hour_window_key, model)
            );
            CREATE TABLE five_hour_block_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id INTEGER NOT NULL,
                five_hour_window_key INTEGER NOT NULL,
                project_path TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                entry_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(five_hour_window_key, project_path)
            );
            """
        )
        stats.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, "
            " block_start_at, first_observed_at_utc, "
            " last_observed_at_utc, final_five_hour_percent, "
            " total_input_tokens, total_output_tokens, "
            " total_cache_create_tokens, total_cache_read_tokens, "
            " total_cost_usd, is_closed, created_at_utc, "
            " last_updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                100, "2026-05-18T05:00:00+00:00",
                block_start_iso, block_start_iso, last_obs_iso, 50.0,
                0, 999, 0, 0, 999.0, 1,
                block_start_iso, last_obs_iso,
            ),
        )
        stats.commit()
    finally:
        stats.close()

    # cache.db with 4 session_entries:
    #   (1) BEFORE block_start_at  → must be EXCLUDED.
    #   (2) AT    block_start_at   → must be INCLUDED (closed lower).
    #   (3) MIDDLE                 → must be INCLUDED.
    #   (4) AT    last_observed_at_utc → must be INCLUDED (closed upper).
    #   (5) AFTER last_observed_at_utc → must be EXCLUDED.
    # Each in-range entry contributes 1000 opus-4-7 out tokens = $0.025.
    cache = sqlite3.connect(cache_path)
    try:
        cache.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
            );
            CREATE TABLE schema_migrations_skipped (
                name TEXT PRIMARY KEY,
                skipped_at_utc TEXT,
                reason TEXT
            );
            CREATE TABLE session_files (
                path TEXT PRIMARY KEY,
                size_bytes INTEGER,
                mtime_ns INTEGER,
                last_byte_offset INTEGER,
                last_ingested_at TEXT,
                session_id TEXT,
                project_path TEXT
            );
            CREATE TABLE session_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT,
                line_offset INTEGER,
                timestamp_utc TEXT,
                model TEXT,
                msg_id TEXT,
                req_id TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_create_tokens INTEGER,
                cache_read_tokens INTEGER,
                usage_extra_json TEXT,
                cost_usd_raw REAL
            );
            CREATE TABLE cache_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        cache.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            ("001_dedup_highest_wins", "2026-05-22T00:00:00Z"),
        )
        # cache_meta walk-complete marker: the gate's PROCEED signal now
        # (cctally-dev#93), paired with the non-empty session_entries below.
        cache.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('claude_ingest_walk_complete', '2026-05-22T02:00:00Z')"
        )
        cache.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, "
            " last_ingested_at, session_id, project_path) "
            "VALUES ('/tmp/session1.jsonl', 100, 0, 100, "
            " '2026-05-22T02:00:00Z', 's', '/tmp/proj')"
        )
        for line, ts, expect_in_range in (
            (0, "2026-05-17T23:59:59+00:00", False),  # BEFORE
            (1, block_start_iso, True),                # AT lower
            (2, "2026-05-18T02:30:00+00:00", True),    # MIDDLE
            (3, last_obs_iso, True),                   # AT upper
            (4, "2026-05-18T04:50:01+00:00", False),   # AFTER
        ):
            cache.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, usage_extra_json) "
                "VALUES ('/tmp/session1.jsonl', ?, ?, "
                " 'claude-opus-4-7', 0, 1000, 0, 0, '{}')",
                (line, ts),
            )
        cache.commit()
    finally:
        cache.close()

    # Run the migration.
    stats = sqlite3.connect(stats_path)
    try:
        db._009_recompute_five_hour_blocks_dedup_fix(stats)
        row = stats.execute(
            "SELECT total_output_tokens, total_cost_usd "
            "FROM five_hour_blocks"
        ).fetchone()
    finally:
        stats.close()

    # Three entries in range (lower-AT + MIDDLE + upper-AT) × 1000
    # tokens each = 3000 tokens; × $25/Mtok = $0.075. A regression to
    # half-open `<` on either end would drop one or both boundary
    # entries → 2000 or 1000 tokens.
    assert row[0] == 3000, (
        f"closed-interval boundary entries dropped; got out={row[0]}, "
        f"expected 3000"
    )
    assert row[1] == pytest.approx(0.075, abs=1e-9), (
        f"closed-interval boundary cost wrong; got {row[1]}, "
        f"expected 0.075"
    )
