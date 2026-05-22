"""Tests for the MigrationGateNotMet dispatcher integration.

Cross-DB migrations (e.g. stats 008 depending on cache 001) raise
``MigrationGateNotMet`` when their prerequisite is unsatisfied. The
dispatcher must treat this as transient: log-and-retry on next open,
do NOT render the migration-error banner.

Coverage:

  * The ``MigrationGateNotMet`` exception class exists and subclasses
    ``Exception``.
  * The ``_gate_001_post_ingest_completed`` helper raises on
    (a) missing 001 marker, (b) marker present + empty ``session_files``
    + JSONL files present on disk, and succeeds on (c) marker present
    + post-001 ``session_files`` row, (d) no JSONL files on disk.
  * The dispatcher catches ``MigrationGateNotMet`` separately from
    generic ``Exception``: it does NOT write to ``migration-errors.log``,
    does NOT mark the migration as skipped, does NOT advance
    ``user_version``, and DOES retry on a subsequent dispatcher walk
    after the gate flips to "pass".

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3, §D4.
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
    """Load bin/_cctally_db.py freshly per test.

    Loading freshly per test isolates the in-process ``_STATS_MIGRATIONS``
    / ``_CACHE_MIGRATIONS`` registries between tests — needed because the
    dispatcher test registers a one-off stats migration and expects the
    next test to start from the same baseline.
    """
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    # Drop sibling caches so the next exec_module rebinds them
    # against a fresh registry (mirror of conftest.load_script).
    for name in [n for n in sys.modules if n.startswith("_cctally_") and n != "_cctally_core"]:
        del sys.modules[name]
    spec = ilu.spec_from_file_location("_cctally_db", BIN_DIR / "_cctally_db.py")
    mod = ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_migration_gate_not_met_class_exists(db_module):
    """``MigrationGateNotMet`` is a public exception subclass."""
    assert hasattr(db_module, "MigrationGateNotMet")
    cls = db_module.MigrationGateNotMet
    assert issubclass(cls, Exception)
    # Carries the message we raise with — round-trips through str().
    e = cls("test message")
    assert "test message" in str(e)


def _seed_cache_schema(conn: sqlite3.Connection) -> None:
    """Minimal schema for the gate helper: schema_migrations + session_files."""
    conn.executescript(
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
            last_ingested_at TEXT NOT NULL,
            session_id       TEXT,
            project_path     TEXT
        );
        """
    )


def test_gate_raises_when_001_marker_absent(db_module, tmp_path):
    """Scenario A — 001 marker missing → raise."""
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    projects = tmp_path / "projects"
    projects.mkdir()

    with pytest.raises(db_module.MigrationGateNotMet, match="001_dedup_highest_wins"):
        db_module._gate_001_post_ingest_completed(cache, projects)


def test_gate_raises_when_post_001_ingest_missing_and_jsonl_on_disk(
    db_module, tmp_path,
):
    """Scenario B — marker present, session_files empty, JSONL exists → raise."""
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z"),
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    with pytest.raises(db_module.MigrationGateNotMet, match="post-001 ingest"):
        db_module._gate_001_post_ingest_completed(cache, projects)


def test_gate_passes_with_post_001_session_files_row(db_module, tmp_path):
    """Scenario C — marker present, session_files has a post-001 row → pass."""
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z"),
    )
    cache.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("/tmp/session1.jsonl", 100, 0, 100,
         "2026-05-22T17:30:00Z", "sess-a", "p1"),
    )
    projects = tmp_path / "projects"
    projects.mkdir()

    # Must NOT raise.
    db_module._gate_001_post_ingest_completed(cache, projects)


def test_gate_passes_when_no_jsonl_on_disk(db_module, tmp_path):
    """Scenario D — marker present, no JSONL files on disk → pass (no-op)."""
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z"),
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    # No JSONL files; gate falls through to the empty-disk fallback.

    db_module._gate_001_post_ingest_completed(cache, projects)


def test_gate_raises_when_schema_migrations_table_absent(db_module, tmp_path):
    """A cache.db that exists as an empty file (or has no
    ``schema_migrations`` table for any reason) must surface as
    ``MigrationGateNotMet`` — NOT raw ``sqlite3.OperationalError``.

    Pre-fix this relied on substring-only match of
    ``"no such table"`` in the error message. The fix tightens to the
    two-signal predicate ``substring match AND sqlite_errorcode in
    (None, 1)`` so future SQLite version drift in the error-message
    format doesn't silently re-raise the OperationalError up the stack
    (which the dispatcher would then log to migration-errors.log and
    render the error banner for — bad UX for a transient/legitimate
    bootstrap state).
    """
    # Fresh in-memory DB with no tables at all — reading
    # schema_migrations raises ``no such table: schema_migrations``.
    cache = sqlite3.connect(":memory:")
    projects = tmp_path / "projects"
    projects.mkdir()

    with pytest.raises(db_module.MigrationGateNotMet, match="schema_migrations"):
        db_module._gate_001_post_ingest_completed(cache, projects)


def test_is_no_such_table_error_predicate(db_module):
    """The centralized predicate ``_is_no_such_table_error`` correctly
    distinguishes "no such table" from other ``OperationalError`` shapes.

    Belt-and-suspenders unit test for the substring + errorcode pair.
    SQLite's ``no such table`` always carries ``sqlite_errorcode == 1``
    (``SQLITE_ERROR``); a generic "disk I/O error" carries 10
    (``SQLITE_IOERR``) and must NOT trip the predicate even if it
    contained the substring through some future quirk.
    """
    is_pred = db_module._is_no_such_table_error

    # Real OperationalError from SQLite — guaranteed to carry the
    # canonical message + errorcode=1.
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("SELECT 1 FROM nonexistent_table")
    except sqlite3.OperationalError as exc:
        assert is_pred(exc) is True

    # Synthetic OperationalError that does NOT match — predicate stays
    # False, dispatcher re-raises as a real failure.
    other_exc = sqlite3.OperationalError("disk I/O error")
    assert is_pred(other_exc) is False


def test_gate_treats_pre_001_session_files_row_as_not_post_001(
    db_module, tmp_path,
):
    """A session_files row whose last_ingested_at PRE-DATES the 001 marker
    is NOT proof of post-001 ingest. The gate must still raise when JSONL
    exists on disk.

    This guards against the false-positive of "session_files non-empty
    means ingest happened" — only post-001-marker timestamps count.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z"),
    )
    cache.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        # last_ingested_at is BEFORE applied_at_utc — pre-001 ingest
        # somehow leaked through (e.g. an operator manually re-inserted
        # rows). Should NOT satisfy the gate.
        ("/tmp/session1.jsonl", 100, 0, 100,
         "2026-05-22T16:00:00Z", "sess-a", "p1"),
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    with pytest.raises(db_module.MigrationGateNotMet, match="post-001 ingest"):
        db_module._gate_001_post_ingest_completed(cache, projects)


# ── Dispatcher integration tests ────────────────────────────────────────


def _open_fresh_stats_db(path: pathlib.Path) -> sqlite3.Connection:
    """Create a stats.db with the dispatcher's expected base shape."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_dispatcher_treats_gate_as_transient(db_module, tmp_path, monkeypatch):
    """A handler raising ``MigrationGateNotMet`` should NOT write to
    ``migration-errors.log`` and should leave the migration pending (not
    skipped). On next open with the prereq satisfied, the handler should
    be retried.

    We register a one-off test migration into ``_STATS_MIGRATIONS`` whose
    handler raises ``MigrationGateNotMet`` on the first call and
    succeeds on the second. The dispatcher walks the registry; under
    the gate-catch path it must:

      * NOT write to ``migration-errors.log``.
      * NOT mark the migration as skipped (``schema_migrations_skipped``
        stays empty for this name).
      * NOT INSERT into ``schema_migrations`` (the body never reached
        the marker INSERT).
      * NOT advance ``user_version`` (we don't all-applied yet).
      * Retry on the next dispatcher walk.
    """
    # Pin the migration-error log under tmp_path so we observe writes
    # in isolation.
    import _cctally_core
    log_path = tmp_path / "migration-errors.log"
    monkeypatch.setattr(_cctally_core, "MIGRATION_ERROR_LOG_PATH", log_path)
    monkeypatch.setattr(_cctally_core, "LOG_DIR", tmp_path)

    # Register a one-off migration. Dynamic slot = len(registry) + 1 so
    # we don't collide with real migrations. (Same pattern as the
    # CCTALLY_MIGRATION_TEST_MODE block in bin/_cctally_db.py.)
    test_seq = len(db_module._STATS_MIGRATIONS) + 1
    test_name = f"{test_seq:03d}_gate_dispatcher_test"
    call_count = {"n": 0}

    @db_module.stats_migration(test_name)
    def _gate_test_handler(conn):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise db_module.MigrationGateNotMet("first call gated")
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at_utc) "
                "VALUES (?, ?)",
                (test_name, "2026-05-22T00:00:00Z"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    try:
        # Build a stats.db with the production migration markers
        # pre-stamped so the dispatcher only walks our test migration.
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
            if m.name == test_name:
                continue  # leave our test migration pending
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at_utc) "
                "VALUES (?, ?)",
                (m.name, "2026-05-22T00:00:00Z"),
            )
        conn.commit()

        # First dispatcher run — gate raises, NO marker, NO log entry,
        # NO skip row, NO user_version advance.
        db_module._run_pending_migrations(
            conn,
            registry=db_module._STATS_MIGRATIONS,
            db_label="stats.db",
        )

        assert call_count["n"] == 1, "first dispatcher walk must invoke handler"

        applied = {
            r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()
        }
        assert test_name not in applied, "test migration must remain pending"

        skipped = {
            r[0] for r in conn.execute(
                "SELECT name FROM schema_migrations_skipped"
            ).fetchall()
        }
        assert test_name not in skipped, "gate-not-met must NOT mark as skipped"

        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_version != len(db_module._STATS_MIGRATIONS), (
            "user_version must NOT advance while a migration is still "
            "pending via gate-not-met"
        )

        assert not log_path.exists(), (
            "MigrationGateNotMet must NOT write to migration-errors.log; "
            f"found: {log_path.read_text() if log_path.exists() else 'n/a'}"
        )

        # Second dispatcher run — handler succeeds.
        db_module._run_pending_migrations(
            conn,
            registry=db_module._STATS_MIGRATIONS,
            db_label="stats.db",
        )

        assert call_count["n"] == 2, "second dispatcher walk must invoke handler again"

        applied = {
            r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()
        }
        assert test_name in applied, "second walk must apply the migration"

        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_version == len(db_module._STATS_MIGRATIONS), (
            "user_version must advance once every migration is applied/skipped"
        )

        # Still no error log entry written.
        assert not log_path.exists(), (
            "MigrationGateNotMet path must never write to migration-errors.log"
        )
    finally:
        # Cleanup: pop the test migration so subsequent module loads
        # see a clean registry. The fixture also drops sys.modules
        # entries, but that's belt-and-suspenders.
        db_module._STATS_MIGRATIONS.pop()
        conn.close()


def test_dispatcher_normal_exception_still_logs(db_module, tmp_path, monkeypatch):
    """Belt-and-suspenders: a non-``MigrationGateNotMet`` exception in a
    migration handler must STILL write to ``migration-errors.log`` (the
    pre-existing behavior the new catch must not regress).
    """
    import _cctally_core
    log_path = tmp_path / "migration-errors.log"
    log_dir = tmp_path
    monkeypatch.setattr(_cctally_core, "MIGRATION_ERROR_LOG_PATH", log_path)
    monkeypatch.setattr(_cctally_core, "LOG_DIR", log_dir)

    test_seq = len(db_module._STATS_MIGRATIONS) + 1
    test_name = f"{test_seq:03d}_normal_exc_dispatcher_test"

    @db_module.stats_migration(test_name)
    def _normal_failure(conn):
        raise RuntimeError("intentional test failure")

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
            if m.name == test_name:
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

        # Generic Exception path DOES log.
        assert log_path.exists(), (
            "non-gate exception must write to migration-errors.log "
            "(pre-existing dispatcher contract)"
        )
        assert "intentional test failure" in log_path.read_text()
    finally:
        db_module._STATS_MIGRATIONS.pop()
        conn.close()
