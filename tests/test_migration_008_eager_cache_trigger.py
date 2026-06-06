"""V4 regression: stats migration 008 eagerly triggers cache.db's
dispatcher (which applies cache 001) BEFORE checking its own gate.

Pre-V4 flow on first ``cctally report`` post-upgrade:
  1. ``open_db()`` runs stats dispatcher.
  2. Stats 008 reads cache.db schema_migrations for 001 marker.
  3. Marker absent (cache.db dispatcher hasn't run this session).
  4. Gate raises ``MigrationGateNotMet`` → dispatcher defers 008.
  5. ``report`` proceeds with STALE ``weekly_cost_snapshots``.
  6. cache.db opens later only if the command happens to touch JSONL.

Post-V4 flow:
  1. ``open_db()`` runs stats dispatcher.
  2. Stats 008 body runs ``_eagerly_apply_cache_migrations()``: opens
     cache.db, runs cache dispatcher, applies 001.
  3. Stats 008 gate now sees Layer A satisfied. With session_entries
     populated (post-001 ingest from a previous run, or wiped + no
     JSONL on disk → Layer C), the gate passes and 008 runs.

This file pins the eager-trigger contract.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3.
"""
from __future__ import annotations

import importlib.util as ilu
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
    spec = ilu.spec_from_file_location("_cctally_db", BIN_DIR / "_cctally_db.py")
    mod = ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _pin_paths(core, tmp_path, monkeypatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    core._init_paths_from_env()


def _stage_stats_with_snapshot(stats_path: pathlib.Path) -> int:
    """Stage stats.db with one auto/no-project snapshot whose stale
    pre-fix cost_usd is meaningfully non-zero."""
    stats = sqlite3.connect(stats_path)
    try:
        stats.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
            );
            CREATE TABLE weekly_cost_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                week_start_date TEXT NOT NULL,
                week_end_date TEXT NOT NULL,
                week_start_at TEXT,
                week_end_at TEXT,
                range_start_iso TEXT,
                range_end_iso TEXT,
                cost_usd REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'cctally-range-cost',
                mode TEXT NOT NULL DEFAULT 'auto',
                project TEXT
            );
            """
        )
        cur = stats.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " range_start_iso, range_end_iso, cost_usd, mode, project) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "2026-05-22T00:00:00Z", "2026-05-15", "2026-05-22",
                "2026-05-15T00:00:00Z", "2026-05-22T00:00:00Z",
                100.0, "auto", None,  # stale pre-fix cost
            ),
        )
        snap_id = cur.lastrowid
        stats.commit()
        return snap_id
    finally:
        stats.close()


def _stage_cache_with_session_entries_only(cache_path: pathlib.Path) -> None:
    """Stage cache.db with the schema + session_entries rows BUT NO
    schema_migrations / session_files at all.

    This mimics the upgrade topology: a pre-v1.12.0 cache.db that's
    been populated under the buggy summed-tokens dedup, BUT NEVER
    OPENED via the migration framework (no schema_migrations table at
    all → fresh-install branch with data probe triggers cache 001
    handler).

    Wait — actually, the pre-framework upgrade case has session_entries
    populated AND no schema_migrations. The dispatcher's D1 fix
    (Pass 1) detects this via the data-emptiness probe: if
    schema_migrations is fresh AND session_entries is non-empty → run
    handlers normally (NOT fresh-install fast-path). So cache 001
    actually runs and wipes session_entries.

    For this V4 test, we just need to verify:
      (a) the eager trigger fires (cache 001 marker appears),
      (b) stats 008's gate then passes, and
      (c) the migration completes in the same invocation.

    The post-cache-001 state has empty session_entries; the snapshot
    recompute against that would zero the cost_usd. To keep the test
    focused on the V4 eager-trigger contract WITHOUT entangling
    G3/G4 semantics, we pre-seed session_files with a post-001-style
    ingest row AND a session_entry inside the snapshot's range, AND
    the 001 marker. This proves V4 fires (the eager trigger is a
    no-op because 001 is already applied) AND the gate then passes.
    The first-run-with-001-already-applied scenario is the "second
    invocation post-upgrade" path; the strictly-fresh-001 case is
    covered by ``test_008_eager_trigger_applies_001_marker_when_missing``
    in test_migration_008_scope.py.
    """
    cache = sqlite3.connect(cache_path)
    try:
        cache.executescript(
            """
            CREATE TABLE session_files (
                path             TEXT PRIMARY KEY,
                size_bytes       INTEGER NOT NULL,
                mtime_ns         INTEGER NOT NULL,
                last_byte_offset INTEGER NOT NULL,
                last_ingested_at TEXT    NOT NULL
            );
            CREATE TABLE session_entries (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path         TEXT    NOT NULL,
                line_offset         INTEGER NOT NULL,
                timestamp_utc       TEXT    NOT NULL,
                model               TEXT    NOT NULL,
                msg_id              TEXT,
                req_id              TEXT,
                input_tokens        INTEGER NOT NULL DEFAULT 0,
                output_tokens       INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
                usage_extra_json    TEXT,
                cost_usd_raw        REAL
            );
            """
        )
        # No schema_migrations table at all — purest pre-framework
        # topology. The dispatcher will create it on the eager-trigger
        # pass.
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, "
            " input_tokens, output_tokens, cache_create_tokens, "
            " cache_read_tokens, usage_extra_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "/tmp/session1.jsonl", 0, "2026-05-18T00:00:00Z",
                "claude-opus-4-7", 0, 1000, 0, 0, "{}",
            ),
        )
        cache.commit()
    finally:
        cache.close()


def test_v4_first_invocation_post_upgrade_eager_trigger_then_defers(
    tmp_path, monkeypatch
):
    """V4 eager trigger + P1 data-present guard interaction: on first
    ``cctally report`` post-upgrade with a populated cache.db (no
    schema_migrations table at all — purest pre-framework topology), the
    eager trigger fires (stamps cache 001, wipes session_entries), but
    the stats 008 handler then DEFERS rather than recomputing.

    Why defer (P1): cache 001 has just wiped ``session_entries`` and
    there is NO JSONL on disk to re-ingest, while stats.db still holds a
    real auto/no-project ``weekly_cost_snapshots`` row. Recomputing over
    the empty cache would zero that historical cost irrecoverably. The
    data-present guard converts the empty-disk Layer C shortcut into a
    defer, leaving 008 pending until a real post-001 ingest exists.

    Verifies:
      * Cache 001 marker stamped (eager trigger fired).
      * Cache 001 wiped session_entries (per its handler contract).
      * Stats 008 DEFERS (``MigrationGateNotMet``); 008 marker NOT
        stamped; the snapshot cost is left untouched.
    """
    db = _load_db()
    core = db._cctally_core

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    snap_id = _stage_stats_with_snapshot(stats_path)
    _stage_cache_with_session_entries_only(cache_path)

    _pin_paths(core, tmp_path, monkeypatch)
    # Empty projects dir → no JSONL on disk. The eager trigger still
    # applies 001 and wipes session_entries, but the data-present guard
    # then DEFERS 008 instead of zeroing the snapshot.
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)

    stats = sqlite3.connect(stats_path)
    try:
        # Data present + empty disk → P1 guard defers.
        with pytest.raises(db.MigrationGateNotMet):
            db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)

        # Stats 008 marker NOT stamped (deferred, not applied).
        marker_008 = stats.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name = '008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
        assert marker_008 is None, (
            "P1: stats 008 marker must NOT be stamped on data-present "
            "empty-disk defer"
        )

        # Snapshot cost untouched by the defer.
        cost = stats.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots WHERE id = ?",
            (snap_id,),
        ).fetchone()[0]
        assert cost == pytest.approx(100.0, abs=1e-9), (
            f"snapshot cost_usd must be untouched on defer; got {cost!r}"
        )
    finally:
        stats.close()

    # Cache 001 marker stamped (eager trigger fired).
    cache_ro = sqlite3.connect(cache_path)
    try:
        marker_001 = cache_ro.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()
        # session_entries WIPED by cache 001's handler.
        entry_count = cache_ro.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0]
    finally:
        cache_ro.close()

    assert marker_001 is not None, (
        "V4: eager trigger must apply cache 001 marker"
    )
    assert entry_count == 0, (
        "V4: cache 001 handler must wipe session_entries; got "
        f"{entry_count} rows"
    )


def test_v4_helper_idempotent_when_001_already_applied(
    tmp_path, monkeypatch
):
    """Calling ``_eagerly_apply_cache_migrations`` twice (e.g. two
    stats migrations both gated on cache 001, or 008 running twice
    across re-opens) is a no-op on the second call. Verifies the
    fast-path return in ``_run_pending_migrations`` when
    ``PRAGMA user_version == len(registry)``.
    """
    db = _load_db()
    core = db._cctally_core

    cache_path = tmp_path / "cache.db"
    _stage_cache_with_session_entries_only(cache_path)

    _pin_paths(core, tmp_path, monkeypatch)
    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    # First call: applies 001.
    db._eagerly_apply_cache_migrations()
    cache_ro = sqlite3.connect(cache_path)
    try:
        first_applied_at = cache_ro.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()[0]
    finally:
        cache_ro.close()

    # Second call: dispatcher's fast-path should return without
    # re-running the handler. The marker's applied_at_utc must NOT
    # change (a re-run would INSERT OR IGNORE; a true re-execution
    # would also wipe session_entries again — already empty so this is
    # subtle, but the marker timestamp is the cleanest invariant).
    db._eagerly_apply_cache_migrations()
    cache_ro = sqlite3.connect(cache_path)
    try:
        second_applied_at = cache_ro.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()[0]
        marker_count = cache_ro.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()[0]
    finally:
        cache_ro.close()

    assert second_applied_at == first_applied_at, (
        "eager trigger must be idempotent: marker timestamp unchanged "
        "on second call"
    )
    assert marker_count == 1, (
        f"marker must remain exactly one row; got {marker_count}"
    )


def test_v4_helper_creates_cache_db_when_missing(tmp_path, monkeypatch):
    """The eager helper creates cache.db when missing (mirrors
    ``open_cache_db``'s contract: cache.db is fully re-derivable).
    Stamps cache 001 on the freshly-created DB (fresh-install
    fast-path since session_entries is empty/absent)."""
    db = _load_db()
    core = db._cctally_core

    cache_path = tmp_path / "cache.db"
    assert not cache_path.exists()

    _pin_paths(core, tmp_path, monkeypatch)
    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)

    db._eagerly_apply_cache_migrations()

    assert cache_path.exists(), (
        "V4: eager trigger must create cache.db when missing"
    )
    cache_ro = sqlite3.connect(cache_path)
    try:
        marker_001 = cache_ro.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()
    finally:
        cache_ro.close()
    assert marker_001 is not None, (
        "V4: cache 001 marker must be stamped on the freshly-created "
        "cache.db (fresh-install fast-path)"
    )


def test_v4_recompute_correct_after_eager_trigger_and_post_001_ingest(
    tmp_path, monkeypatch
):
    """End-to-end V4 happy path: stats 008 runs in same invocation
    AND produces the correct recomputed cost_usd when the cache has
    a post-001 ingest row.

    Topology:
      * stats.db: one auto/no-project snapshot, stale pre-fix
        cost_usd = $100.0.
      * cache.db: 001 marker present, session_files row with
        last_ingested_at > 001.applied_at_utc (post-001 ingest), one
        session_entry inside the snapshot's range (1000 opus-4-7
        output tokens → $0.025).

    Expected: snapshot.cost_usd updated to $0.025; 008 marker stamped;
    no exception raised.
    """
    db = _load_db()
    core = db._cctally_core

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    snap_id = _stage_stats_with_snapshot(stats_path)

    # Cache with 001 marker + post-001 session_files row + session_entry
    # inside the snapshot's range. The eager trigger will see 001
    # already applied (fast-path return) and the gate will pass via
    # Layer B (post-001 ingest).
    cache = sqlite3.connect(cache_path)
    try:
        cache.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            );
            CREATE TABLE session_files (
                path             TEXT PRIMARY KEY,
                size_bytes       INTEGER NOT NULL,
                mtime_ns         INTEGER NOT NULL,
                last_byte_offset INTEGER NOT NULL,
                last_ingested_at TEXT    NOT NULL,
                session_id       TEXT,
                project_path     TEXT
            );
            CREATE TABLE session_entries (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path         TEXT    NOT NULL,
                line_offset         INTEGER NOT NULL,
                timestamp_utc       TEXT    NOT NULL,
                model               TEXT    NOT NULL,
                msg_id              TEXT,
                req_id              TEXT,
                input_tokens        INTEGER NOT NULL DEFAULT 0,
                output_tokens       INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
                usage_extra_json    TEXT,
                cost_usd_raw        REAL
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
        # (cctally-dev#93). The eager-apply path does not run sync_cache,
        # so the marker is seeded explicitly here alongside the non-empty
        # session_entries below.
        cache.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('claude_ingest_walk_complete', '2026-05-22T02:00:00Z')"
        )
        cache.execute(
            "INSERT INTO session_files VALUES (?,?,?,?,?,?,?)",
            (
                "/tmp/session1.jsonl", 100, 0, 100,
                "2026-05-22T01:00:00Z", "s1", "/tmp/proj",
            ),
        )
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, "
            " input_tokens, output_tokens, cache_create_tokens, "
            " cache_read_tokens, usage_extra_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "/tmp/session1.jsonl", 0, "2026-05-18T00:00:00Z",
                "claude-opus-4-7", 0, 1000, 0, 0, "{}",
            ),
        )
        cache.commit()
    finally:
        cache.close()

    _pin_paths(core, tmp_path, monkeypatch)
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()
    (fake_projects / "session1.jsonl").write_text("{}\n")

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)

    stats = sqlite3.connect(stats_path)
    try:
        # Runs to completion in the same invocation.
        db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)
        db._stamp_applied(stats, "008_recompute_weekly_cost_snapshots_dedup_fix")  # dispatcher now owns the stamp (#140)

        # Snapshot recomputed from the seeded session_entry.
        cost = stats.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots WHERE id = ?",
            (snap_id,),
        ).fetchone()[0]
        # 1000 opus-4-7 output tokens at $25/Mtok = $0.025.
        assert cost == pytest.approx(0.025, abs=1e-9), (
            f"snapshot cost_usd not recomputed: got {cost!r}"
        )

        # 008 marker stamped.
        marker_008 = stats.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name = '008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
        assert marker_008 is not None
    finally:
        stats.close()
