"""D2 regression — dispatcher does NOT halt the registry walk on a
``MigrationGateNotMet``.

Pre-fix the dispatcher caught ``MigrationGateNotMet`` and ``break``'d
the registry walk on the first deferral, mirroring the failure-break
rule used for generic ``Exception``. The two cases are NOT symmetric:

  * Generic ``Exception`` from a handler may leave a partial transaction
    state behind, so later migrations must not see it. ``break`` is
    correct there.
  * ``MigrationGateNotMet`` is raised BEFORE the handler touches any
    state (or after the handler rolls back its own BEGIN). The DB is
    in a fully-consistent prior state, and any later migration with no
    dependency on the gated one can legitimately run.

Pre-fix scenario: a hypothetical future migration 009 with NO dependency
on 008 would be blocked indefinitely whenever 008 gate-deferred (e.g.
because cache.db transiently missing or the post-001 ingest hadn't run
yet). The fix changes ``break`` to ``continue`` so 009 still runs;
008's pending state still prevents user_version from advancing.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3 (D2).
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


def _open_fresh_stats_db(path: pathlib.Path) -> sqlite3.Connection:
    """Create a stats.db with row_factory set (mirrors production)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_dispatcher_continues_past_gate_defer(db_module, tmp_path, monkeypatch):
    """A gate-deferred migration must NOT halt the walk. Register TWO
    test-only migrations:

      N+1 — raises MigrationGateNotMet on every call.
      N+2 — has no dependency on N+1; should still run.

    Pre-fix: both stay pending forever; only N+1 was ever invoked.
    Post-fix: N+1 stays pending, N+2 runs and gets its marker.
    Crucially, user_version still does NOT advance (N+1 is pending),
    so the next open re-tries N+1.
    """
    import _cctally_core
    log_path = tmp_path / "migration-errors.log"
    monkeypatch.setattr(_cctally_core, "MIGRATION_ERROR_LOG_PATH", log_path)
    monkeypatch.setattr(_cctally_core, "LOG_DIR", tmp_path)

    n_plus_1_seq = len(db_module._STATS_MIGRATIONS) + 1
    n_plus_1_name = f"{n_plus_1_seq:03d}_d2_gated_test"
    n_plus_2_seq = n_plus_1_seq + 1
    n_plus_2_name = f"{n_plus_2_seq:03d}_d2_independent_test"

    n_plus_1_calls = {"n": 0}
    n_plus_2_calls = {"n": 0}

    @db_module.stats_migration(n_plus_1_name)
    def _gated_handler(conn):
        n_plus_1_calls["n"] += 1
        raise db_module.MigrationGateNotMet(
            "synthetic gate-not-met for D2 regression"
        )

    @db_module.stats_migration(n_plus_2_name)
    def _independent_handler(conn):
        n_plus_2_calls["n"] += 1
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(name, applied_at_utc) VALUES (?, ?)",
                (n_plus_2_name, "2026-05-22T00:00:00Z"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    try:
        db_path = tmp_path / "stats.db"
        conn = _open_fresh_stats_db(db_path)
        # Seed schema_migrations as pre-existing (non-empty) so the
        # dispatcher takes the upgrade path (not the fresh-install
        # stamp-only fast-path).
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            );
            """
        )
        # Pre-stamp every real production migration so the walk only
        # touches our two test entries.
        for m in db_module._STATS_MIGRATIONS:
            if m.name in (n_plus_1_name, n_plus_2_name):
                continue
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at_utc) "
                "VALUES (?, ?)",
                (m.name, "2026-05-22T00:00:00Z"),
            )
        conn.commit()

        # Dispatch.
        db_module._run_pending_migrations(
            conn,
            registry=db_module._STATS_MIGRATIONS,
            db_label="stats.db",
        )

        # The gated migration was invoked but stayed pending.
        assert n_plus_1_calls["n"] == 1, "gated migration must be invoked once"
        assert n_plus_2_calls["n"] == 1, (
            "independent migration must run despite earlier gate defer "
            "(D2 — `continue`, not `break`)"
        )

        applied = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM schema_migrations"
            ).fetchall()
        }
        assert n_plus_1_name not in applied, (
            "gated migration must stay pending"
        )
        assert n_plus_2_name in applied, (
            "independent migration must have written its marker"
        )

        # user_version stays unchanged because the gated migration is
        # still pending. all-applied predicate uses applied | skipped.
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_version != len(db_module._STATS_MIGRATIONS), (
            "user_version must NOT advance while any migration is still "
            "pending via gate-not-met"
        )

        # No error-log write (gate defer is silent).
        assert not log_path.exists(), (
            "MigrationGateNotMet must NOT write to migration-errors.log; "
            f"found: {log_path.read_text() if log_path.exists() else 'n/a'}"
        )
    finally:
        # Clean up the test registrations so the next test sees a
        # pristine registry.
        db_module._STATS_MIGRATIONS.pop()
        db_module._STATS_MIGRATIONS.pop()
        conn.close()


def test_dispatcher_failure_still_breaks_walk(db_module, tmp_path, monkeypatch):
    """Belt-and-suspenders: the D2 change to ``continue`` for
    ``MigrationGateNotMet`` must NOT regress the generic-Exception
    path. A real handler exception still ``break``s — later migrations
    must not run when prior state is potentially partial.
    """
    import _cctally_core
    log_path = tmp_path / "migration-errors.log"
    monkeypatch.setattr(_cctally_core, "MIGRATION_ERROR_LOG_PATH", log_path)
    monkeypatch.setattr(_cctally_core, "LOG_DIR", tmp_path)

    n1_seq = len(db_module._STATS_MIGRATIONS) + 1
    n1_name = f"{n1_seq:03d}_d2_failure_test"
    n2_seq = n1_seq + 1
    n2_name = f"{n2_seq:03d}_d2_after_failure_test"

    n2_calls = {"n": 0}

    @db_module.stats_migration(n1_name)
    def _failing_handler(conn):
        raise RuntimeError("D2 belt-and-suspenders failure")

    @db_module.stats_migration(n2_name)
    def _later_handler(conn):
        n2_calls["n"] += 1

    try:
        db_path = tmp_path / "stats.db"
        conn = _open_fresh_stats_db(db_path)
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            );
            """
        )
        for m in db_module._STATS_MIGRATIONS:
            if m.name in (n1_name, n2_name):
                continue
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at_utc) "
                "VALUES (?, ?)",
                (m.name, "2026-05-22T00:00:00Z"),
            )
        conn.commit()

        db_module._run_pending_migrations(
            conn,
            registry=db_module._STATS_MIGRATIONS,
            db_label="stats.db",
        )

        # Generic Exception STILL halts the walk (later migration NOT run).
        assert n2_calls["n"] == 0, (
            "generic Exception must still halt the registry walk; D2 only "
            "changes the MigrationGateNotMet branch"
        )
        # Generic Exception DOES log.
        assert log_path.exists()
        assert "D2 belt-and-suspenders failure" in log_path.read_text()
    finally:
        db_module._STATS_MIGRATIONS.pop()
        db_module._STATS_MIGRATIONS.pop()
        conn.close()
