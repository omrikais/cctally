"""D1 regression — fresh-install heuristic gates on data-emptiness, not
just schema_migrations-table-absence.

Pre-fix the dispatcher's ``fresh_install`` boolean was True for any DB
where ``schema_migrations`` didn't exist yet AND was empty after
bootstrap-rename. That description fits TWO very different worlds:

  1. A genuinely fresh DB (no data, no migration framework yet).
  2. A pre-framework-era DB (populated data tables, NEVER had the
     migration framework). cache.db in particular fell into this
     bucket for every upgrading user before v1.12.0 — the cache
     migration framework was only added in this release.

Pre-fix, world (2) was misclassified as world (1) and the dispatcher's
fresh-install branch stamped EVERY pending migration's marker without
invoking its handler. That meant cache migration 001 (the
dedup-highest-wins wipe + re-ingest) silently skipped on every
upgrading user, leaving the buggy summed-tokens cache data in place
indefinitely. Symmetric concern for stats migration 008 (recompute
weekly_cost_snapshots) — a stats.db with cost rows from a pre-framework
write path would have been stamped applied without running.

Fix shape (bin/_cctally_db.py:474)

``fresh_install`` now requires BOTH the schema_migrations-table
absence (existing check) AND empty/absent probe table per DB:

  * stats.db → ``weekly_cost_snapshots``
  * cache.db → ``session_entries``

Probe-table absent (genuine pre-CREATE fresh install) keeps the
fresh_install branch active. Probe-table present with one or more rows
flips fresh_install to False and the registry walks normally with
handlers invoked.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3 (D1).
"""
from __future__ import annotations

import importlib.util as ilu
import pathlib
import sqlite3
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


@pytest.fixture
def db_module():
    """Load bin/_cctally_db.py freshly per test (isolated registries)."""
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    for name in [
        n for n in sys.modules
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]:
        del sys.modules[name]
    spec = ilu.spec_from_file_location("_cctally_db", BIN_DIR / "_cctally_db.py")
    mod = ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── cache.db side (the canonical D1 scenario) ──────────────────────────


def _seed_pre_framework_cache_db(path: pathlib.Path) -> None:
    """Stage a cache.db with the pre-v1.12.0 shape: populated session_entries +
    session_files, NO schema_migrations table.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE session_files (
                path             TEXT PRIMARY KEY,
                size_bytes       INTEGER NOT NULL,
                mtime_ns         INTEGER NOT NULL,
                last_byte_offset INTEGER NOT NULL,
                last_ingested_at TEXT NOT NULL,
                session_id       TEXT,
                project_path     TEXT
            );
            CREATE TABLE session_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT, line_offset INTEGER, timestamp_utc TEXT,
                model TEXT, msg_id TEXT, req_id TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_create_tokens INTEGER, cache_read_tokens INTEGER,
                usage_extra_json TEXT, cost_usd_raw REAL
            );
            """
        )
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " session_id, project_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("/tmp/seed.jsonl", 100, 0, 100, "2026-05-01T00:00:00Z",
             "sess-pre", "p1"),
        )
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, "
            " msg_id, req_id, input_tokens, output_tokens, "
            " cache_create_tokens, cache_read_tokens, usage_extra_json, "
            " cost_usd_raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("/tmp/seed.jsonl", 0, "2026-05-01T00:00:00Z", "claude-opus-4-7",
             "m1", "r1", 100, 500, 0, 0, None, None),
        )
        conn.commit()
    finally:
        conn.close()


def test_cache_001_actually_runs_on_pre_framework_upgrade(db_module, tmp_path):
    """Pre-v1.12.0 cache.db (session_entries populated, no schema_migrations
    table) → dispatcher must take the UPGRADE path, NOT the fresh-install
    stamp-only path. The 001 handler must actually run and wipe the
    pre-fix data.

    Pre-fix: fresh_install=True (schema_migrations absent + applied is
    empty) → stamp 001 marker without running handler → buggy data
    stays forever.

    Post-fix: data-table probe sees the pre-existing session_entries
    row → fresh_install=False → handler runs → session_entries is
    wiped → next sync_cache re-ingests under the new dedup rule.
    """
    cache_path = tmp_path / "cache.db"
    _seed_pre_framework_cache_db(cache_path)

    # Sanity: pre-state
    conn_pre = sqlite3.connect(cache_path)
    assert conn_pre.execute(
        "SELECT COUNT(*) FROM session_entries"
    ).fetchone()[0] == 1
    assert conn_pre.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='schema_migrations'"
    ).fetchone() is None
    conn_pre.close()

    # Dispatch against the real cache migration registry.
    conn = sqlite3.connect(cache_path)
    try:
        db_module._run_pending_migrations(
            conn,
            registry=db_module._CACHE_MIGRATIONS,
            db_label="cache.db",
        )

        # 001 handler MUST have run (post-fix). The handler wipes
        # session_entries + session_files. Pre-fix this assertion
        # FAILED because the handler was never invoked.
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0] == 0, (
            "session_entries must be wiped by the 001 handler — "
            "pre-fix the dispatcher's fresh-install heuristic stamped "
            "001 applied WITHOUT invoking the handler, leaving the "
            "buggy data in place."
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM session_files"
        ).fetchone()[0] == 0

        # The 001 marker should still land (handler INSERT OR IGNOREs
        # its own marker on success).
        marker = conn.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()
        assert marker is not None, "001 marker must be present"
    finally:
        conn.close()


def test_genuine_fresh_cache_db_still_uses_stamp_only_path(db_module, tmp_path):
    """A genuinely fresh cache.db (no tables at all) still takes the
    fresh-install stamp-only path. The data-table probe gracefully
    treats a missing probe table as empty.
    """
    cache_path = tmp_path / "cache.db"
    # Touch the file but leave it empty (no tables).
    sqlite3.connect(cache_path).close()

    conn = sqlite3.connect(cache_path)
    try:
        # The production cache schema (session_files + session_entries)
        # would normally be CREATEd by open_cache_db BEFORE the
        # dispatcher. For this test we skip that step so the probe
        # table genuinely doesn't exist → fresh-install path should
        # still trigger.
        db_module._run_pending_migrations(
            conn,
            registry=db_module._CACHE_MIGRATIONS,
            db_label="cache.db",
        )

        # 001 marker stamped (fresh-install path)
        marker = conn.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()
        assert marker is not None, (
            "fresh-install path must still stamp 001 applied "
            "(genuinely fresh DB has no data to migrate)"
        )

        # user_version advances on a successful all-applied walk.
        user_version = conn.execute(
            "PRAGMA user_version"
        ).fetchone()[0]
        assert user_version == len(db_module._CACHE_MIGRATIONS)
    finally:
        conn.close()


# ── stats.db side (symmetric defense) ──────────────────────────────────


def _seed_pre_framework_stats_db(path: pathlib.Path) -> None:
    """Stage a stats.db with the pre-framework shape for the symmetric
    case: populated weekly_cost_snapshots, NO schema_migrations table.

    Only seeds the columns / tables the dispatcher actually probes.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
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
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, cost_usd, mode) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-05-01T00:00:00Z", "2026-04-24", "2026-05-01", 50.0, "auto"),
        )
        conn.commit()
    finally:
        conn.close()


def test_stats_pre_framework_upgrade_marks_pending_runs_handlers(
    db_module, tmp_path, monkeypatch,
):
    """A stats.db with populated weekly_cost_snapshots but no
    schema_migrations table must take the UPGRADE path. We don't run
    real stats migration 008 here (it requires cache.db wiring); we
    register a synthetic migration that asserts it was actually invoked
    (proving the dispatcher chose the upgrade branch over the
    fresh-install branch).

    To isolate the D1 question (fresh-install vs upgrade classification),
    we pre-stamp every production stats migration before dispatching so
    only the synthetic test migration walks. The pre-stamp is itself
    a tell: in a pre-framework world the schema_migrations table didn't
    exist yet, so we have to CREATE it (without the absent-table
    schema_migrations_existed flag flipping our scenario) — we
    accomplish this by CREATEing schema_migrations explicitly AFTER
    seeding the data tables. Per the dispatcher's existing rule, the
    schema_migrations existence check at line 408 runs BEFORE the
    CREATE TABLE IF NOT EXISTS at line 412, so we need the table to
    NOT exist before the dispatcher runs. To square the circle, we
    use a TWO-PHASE approach:
      Phase 1 (this fixture): seed weekly_cost_snapshots + the
        synthetic migration's pre-stamps in schema_migrations.
      Phase 2 (dispatch): the dispatcher sees schema_migrations
        existing → schema_migrations_existed = True → not the
        fresh-install code path even pre-fix.

    To genuinely reproduce the pre-fix bug, we need the dispatcher to
    see schema_migrations ABSENT but weekly_cost_snapshots POPULATED.
    Solution: pre-stamp markers via a side-channel — we DROP the
    schema_migrations table after pre-stamping a sentinel, then re-create
    it without the sentinel… too tangled. Cleaner: drop schema_migrations
    entirely from the seed and let the dispatcher CREATE it. The
    production registry then walks every migration, all of which need
    real schema (five_hour_blocks etc.) — handlers will fail.

    The cleanest workable shape for THIS test is the cache.db scenario
    above. For stats.db we exercise a simpler proof: assert that on a
    populated-data + absent-schema_migrations DB, the dispatcher does
    NOT advance user_version after stamping every migration's marker
    via the fresh-install path. Pre-fix, fresh_install was True and
    user_version advanced to len(registry) without any handler being
    invoked. Post-fix, fresh_install is False, the dispatcher attempts
    to run the first real migration's handler against a DB without the
    handler's expected schema, the handler raises, the dispatcher logs
    and breaks (the existing failure-break behavior). The pre-fix vs
    post-fix DIFFERENCE: pre-fix user_version=len(registry) and no log
    entry; post-fix user_version=0 and log file exists.
    """
    stats_path = tmp_path / "stats.db"
    _seed_pre_framework_stats_db(stats_path)

    try:
        # Pin error log under tmp_path so we observe stale failure
        # entries.
        import _cctally_core
        log_path = tmp_path / "migration-errors.log"
        monkeypatch.setattr(_cctally_core, "MIGRATION_ERROR_LOG_PATH", log_path)
        monkeypatch.setattr(_cctally_core, "LOG_DIR", tmp_path)

        conn = sqlite3.connect(stats_path)
        conn.row_factory = sqlite3.Row
        try:
            db_module._run_pending_migrations(
                conn,
                registry=db_module._STATS_MIGRATIONS,
                db_label="stats.db",
            )

            # Post-fix: fresh_install is False (weekly_cost_snapshots
            # has one row), so handlers actually attempt to run. The
            # first handler (001_five_hour_block_models_backfill_v1)
            # SELECTs from five_hour_blocks, which doesn't exist in
            # this stripped-down fixture → handler raises → dispatcher
            # logs and breaks → user_version stays at 0.
            #
            # Pre-fix: fresh_install was True → every marker stamped
            # without invoking handler → user_version advanced to
            # len(registry) → no log entry. The test would have passed
            # with the WRONG semantics.
            user_version = conn.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            assert user_version == 0, (
                "user_version must stay at 0 (D1 — handler must be "
                "invoked, not stamp-only via the fresh-install fast "
                "path; pre-fix this advanced to len(registry))"
            )

            # Error log must exist (first handler failed because the
            # fixture intentionally doesn't seed the production
            # schema). The presence of the log entry PROVES the
            # handler was actually invoked (post-fix); pre-fix the
            # log file would not exist.
            assert log_path.exists(), (
                "log file must exist (handler was invoked and failed "
                "against the stripped-down fixture). Pre-fix the "
                "fresh-install fast path skipped handler invocation "
                "entirely and no log entry was ever written."
            )
            assert (
                "stats.db:001_five_hour_block_models_backfill_v1"
                in log_path.read_text()
            ), (
                "the first handler must have been invoked and failed"
            )
        finally:
            conn.close()
    finally:
        pass
