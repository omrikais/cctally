"""Scope tests for stats migration 009: every five_hour_blocks row
gets recomputed (active AND closed), AND rollup-children (models /
projects) are replace-all'd per window.

Active-vs-closed distinction matters because the live writer
(``maybe_update_five_hour_block``) ONLY recomputes the currently
active block — closed historical blocks keep their pre-dedup totals
forever absent this migration.

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


_STATS_DDL = """
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


_CACHE_DDL = """
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
"""


def _stage_stats_with_blocks(
    stats_path: pathlib.Path,
    blocks: list[tuple],
) -> None:
    """Stage stats.db with blocks given as
    (id, window_key, block_start, last_observed, is_closed,
     inflated_out_tokens, inflated_cost).

    For each block, also stage one inflated five_hour_block_models row
    and one inflated five_hour_block_projects row at 'old-projA'.
    """
    conn = sqlite3.connect(stats_path)
    try:
        conn.executescript(_STATS_DDL)
        for (
            bid, wk, block_start, last_obs, is_closed,
            out_tok, cost_usd,
        ) in blocks:
            conn.execute(
                "INSERT INTO five_hour_blocks "
                "(id, five_hour_window_key, five_hour_resets_at, "
                " block_start_at, first_observed_at_utc, "
                " last_observed_at_utc, final_five_hour_percent, "
                " total_input_tokens, total_output_tokens, "
                " total_cache_create_tokens, total_cache_read_tokens, "
                " total_cost_usd, is_closed, created_at_utc, "
                " last_updated_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bid, wk,
                    "2026-12-31T00:00:00+00:00",  # placeholder
                    block_start, block_start, last_obs,
                    50.0, 0, out_tok, 0, 0, cost_usd, is_closed,
                    block_start, last_obs,
                ),
            )
            conn.execute(
                "INSERT INTO five_hour_block_models "
                "(block_id, five_hour_window_key, model, "
                " input_tokens, output_tokens, "
                " cache_create_tokens, cache_read_tokens, "
                " cost_usd, entry_count) "
                "VALUES (?, ?, 'claude-opus-4-7', 0, ?, 0, 0, ?, 1)",
                (bid, wk, out_tok, cost_usd),
            )
            conn.execute(
                "INSERT INTO five_hour_block_projects "
                "(block_id, five_hour_window_key, project_path, "
                " input_tokens, output_tokens, "
                " cache_create_tokens, cache_read_tokens, "
                " cost_usd, entry_count) "
                "VALUES (?, ?, 'old-projA', 0, ?, 0, 0, ?, 1)",
                (bid, wk, out_tok, cost_usd),
            )
        conn.commit()
    finally:
        conn.close()


def _stage_cache_with_entries(
    cache_path: pathlib.Path, entries: list[dict],
) -> None:
    conn = sqlite3.connect(cache_path)
    try:
        conn.executescript(_CACHE_DDL)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            ("001_dedup_highest_wins", "2026-05-22T00:00:00Z"),
        )
        # Stage session_files for each entry's source so the LEFT JOIN
        # carries a project_path. Two project paths so the test can
        # observe replace-all expanding the row set.
        for proj, src in (
            ("/tmp/new-projA", "/tmp/session1.jsonl"),
            ("/tmp/new-projB", "/tmp/session2.jsonl"),
        ):
            conn.execute(
                "INSERT INTO session_files "
                "(path, size_bytes, mtime_ns, last_byte_offset, "
                " last_ingested_at, session_id, project_path) "
                "VALUES (?, 100, 0, 100, ?, ?, ?)",
                (src, "2026-05-22T02:00:00Z", "s", proj),
            )
        for e in entries:
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, usage_extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}')",
                (
                    e["source"], e.get("line", 0), e["ts"],
                    "claude-opus-4-7", 0, e["out"], 0, 0,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_009_recomputes_closed_AND_active_blocks(tmp_path, monkeypatch):
    """Both closed historical AND active current blocks have their
    inflated totals recomputed. Pre-fix the live writer would have
    recomputed neither (closed) or only one (active); without this
    migration closed blocks would carry stale 2x totals forever.
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

    _stage_stats_with_blocks(
        stats_path,
        blocks=[
            # Block A: CLOSED. Pre-fix 2000 out, $0.050. Real 1000, $0.025.
            (
                1, 100,
                "2026-05-18T00:00:00+00:00",
                "2026-05-18T04:50:00+00:00",
                1, 2000, 0.050,
            ),
            # Block B: ACTIVE. Pre-fix 4000 out, $0.100. Real 2000, $0.050.
            (
                2, 200,
                "2026-05-22T10:00:00+00:00",
                "2026-05-22T11:30:00+00:00",
                0, 4000, 0.100,
            ),
        ],
    )
    _stage_cache_with_entries(
        cache_path,
        entries=[
            {
                "source": "/tmp/session1.jsonl",
                "ts": "2026-05-18T02:00:00Z",
                "out": 1000,
            },
            {
                "source": "/tmp/session1.jsonl",
                "ts": "2026-05-22T10:30:00Z",
                "out": 1000,
            },
            {
                "source": "/tmp/session2.jsonl",
                "ts": "2026-05-22T11:00:00Z",
                "out": 1000,
            },
        ],
    )

    stats = sqlite3.connect(stats_path)
    try:
        db._009_recompute_five_hour_blocks_dedup_fix(stats)
        rows = list(stats.execute(
            "SELECT id, total_output_tokens, total_cost_usd, is_closed "
            "FROM five_hour_blocks ORDER BY id"
        ).fetchall())
    finally:
        stats.close()

    # Closed block A: 1000 out, $0.025; is_closed preserved.
    assert rows[0] == (1, 1000, pytest.approx(0.025, abs=1e-9), 1)
    # Active block B: 2000 out, $0.050; is_closed preserved.
    assert rows[1] == (2, 2000, pytest.approx(0.050, abs=1e-9), 0)


def test_009_replaces_rollup_children_per_window(tmp_path, monkeypatch):
    """The replace-all DELETE-WHERE-window + bulk INSERT must:
      (1) drop pre-fix rows that no longer correspond to any entry
          (e.g. 'old-projA' that doesn't appear in the new cache),
      (2) insert fresh rows for each (window, model) and (window,
          project) bucket the recompute discovers.
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

    _stage_stats_with_blocks(
        stats_path,
        blocks=[
            (
                1, 100,
                "2026-05-18T00:00:00+00:00",
                "2026-05-18T04:50:00+00:00",
                1, 2000, 0.050,
            ),
        ],
    )
    # Entries for window 100: two distinct project paths under session1
    # (new-projA) and session2 (new-projB). Old-projA from pre.sqlite
    # MUST be gone after the replace-all.
    _stage_cache_with_entries(
        cache_path,
        entries=[
            {
                "source": "/tmp/session1.jsonl",
                "ts": "2026-05-18T01:00:00Z",
                "out": 500,
            },
            {
                "source": "/tmp/session2.jsonl",
                "ts": "2026-05-18T03:00:00Z",
                "out": 500,
            },
        ],
    )

    stats = sqlite3.connect(stats_path)
    try:
        db._009_recompute_five_hour_blocks_dedup_fix(stats)
        proj_rows = list(stats.execute(
            "SELECT project_path, output_tokens, cost_usd "
            "FROM five_hour_block_projects "
            "WHERE five_hour_window_key = 100 "
            "ORDER BY project_path"
        ).fetchall())
        model_rows = list(stats.execute(
            "SELECT model, output_tokens "
            "FROM five_hour_block_models "
            "WHERE five_hour_window_key = 100"
        ).fetchall())
    finally:
        stats.close()

    # Old-projA must be gone; new-projA + new-projB present with
    # 500 out tokens each ($0.0125 each).
    assert len(proj_rows) == 2
    assert proj_rows[0][0] == "/tmp/new-projA"
    assert proj_rows[0][1] == 500
    assert proj_rows[0][2] == pytest.approx(0.0125, abs=1e-9)
    assert proj_rows[1][0] == "/tmp/new-projB"
    assert proj_rows[1][1] == 500
    assert proj_rows[1][2] == pytest.approx(0.0125, abs=1e-9)

    # Model rollup: one row for claude-opus-4-7 at 1000 total.
    assert len(model_rows) == 1
    assert model_rows[0] == ("claude-opus-4-7", 1000)


def test_009_null_project_path_collapses_to_unknown(tmp_path, monkeypatch):
    """When session_files.project_path is NULL (lazy backfill in
    progress), the LEFT JOIN returns NULL; the migration must collapse
    to '(unknown)' sentinel — same rule as the live writer
    (_compute_block_totals at bin/_cctally_record.py).
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

    _stage_stats_with_blocks(
        stats_path,
        blocks=[
            (
                1, 100,
                "2026-05-18T00:00:00+00:00",
                "2026-05-18T04:50:00+00:00",
                1, 2000, 0.050,
            ),
        ],
    )

    # Cache: explicitly omit session_files row for the entry source,
    # so the LEFT JOIN's project_path comes back NULL.
    conn = sqlite3.connect(cache_path)
    try:
        conn.executescript(_CACHE_DDL)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            ("001_dedup_highest_wins", "2026-05-22T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, "
            " last_ingested_at, session_id, project_path) "
            "VALUES (?, 100, 0, 100, '2026-05-22T02:00:00Z', 's', NULL)",
            ("/tmp/orphan-session.jsonl",),
        )
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, "
            " input_tokens, output_tokens, cache_create_tokens, "
            " cache_read_tokens, usage_extra_json) "
            "VALUES (?, 0, ?, 'claude-opus-4-7', 0, 1000, 0, 0, '{}')",
            ("/tmp/orphan-session.jsonl", "2026-05-18T01:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    stats = sqlite3.connect(stats_path)
    try:
        db._009_recompute_five_hour_blocks_dedup_fix(stats)
        proj_rows = list(stats.execute(
            "SELECT project_path FROM five_hour_block_projects "
            "WHERE five_hour_window_key = 100"
        ).fetchall())
    finally:
        stats.close()

    # NULL collapses to '(unknown)' sentinel.
    assert proj_rows == [("(unknown)",)]
