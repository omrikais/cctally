"""Tests for the MigrationGateNotMet dispatcher integration.

Cross-DB migrations (e.g. stats 008 depending on cache 001) raise
``MigrationGateNotMet`` when their prerequisite is unsatisfied. The
dispatcher must treat this as transient: log-and-retry on next open,
do NOT render the migration-error banner.

Coverage:

  * The ``MigrationGateNotMet`` exception class exists and subclasses
    ``Exception``.
  * The ``_gate_001_post_ingest_completed`` helper raises on
    (a) missing 001 marker, (b) 001 applied + NO ``cache_meta``
    walk-complete marker + JSONL on disk + historical rows to protect,
    and succeeds on (c) 001 applied + ``cache_meta`` walk-complete
    marker + non-empty ``session_entries``, (d) no historical rows to
    protect.

    NOTE (cctally-dev#93): the gate's post-001-ingest signal moved from
    "a ``session_files`` row whose ``last_ingested_at >= 001.applied_at``"
    to "the ``cache_meta`` ``claude_ingest_walk_complete`` marker present
    AND ``session_entries`` non-empty." Tests below seed the NEW signal;
    the DEFER/PROCEED intent of each scenario is preserved verbatim.
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


MARKER = "claude_ingest_walk_complete"


def _seed_cache_schema(conn: sqlite3.Connection) -> None:
    """Minimal schema for the gate helper at the points it reads.

    cctally-dev#93: the gate now reads the ``cache_meta``
    ``claude_ingest_walk_complete`` marker (``walk_complete``) and
    ``session_entries`` non-emptiness (``cache_has_entries``) instead of
    a post-001 ``session_files`` row. ``session_files`` stays in the
    schema for parity with production cache.db but is no longer the
    gate signal.
    """
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


def _seed_walk_complete_marker(conn: sqlite3.Connection) -> None:
    """Seed the NEW post-001-ingest PROCEED signal (cctally-dev#93): the
    ``cache_meta`` ``claude_ingest_walk_complete`` marker AND a non-empty
    ``session_entries`` (row 6 requires BOTH ``walk✓`` and ``entries✓``)."""
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
        (MARKER, "2026-05-22T17:30:00+00:00"),
    )
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model) "
        "VALUES (?, ?, ?, ?)",
        ("/tmp/session1.jsonl", 0, "2026-05-22T17:30:00Z", "claude-opus-4-7"),
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
    """Scenario B — 001 applied, NO walk-complete marker, JSONL exists,
    historical rows to protect → raise (row 7).

    cctally-dev#93: the post-001-ingest PROCEED signal is now the
    ``cache_meta`` walk-complete marker (not a ``session_files`` row).
    Absent the marker, a caller with historical rows
    (``data_present=True``) must DEFER so the recompute doesn't run over
    an incomplete cache. The reason string is the resolver's row-7
    ``walk✗ AND jsonl_present`` text ("ingest walk not yet complete").
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z"),
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    with pytest.raises(db_module.MigrationGateNotMet, match="ingest walk not yet complete"):
        db_module._gate_001_post_ingest_completed(
            cache, projects, data_present=True,
        )


def test_gate_passes_with_post_001_walk_complete_marker(db_module, tmp_path):
    """Scenario C — 001 applied, walk-complete marker present, non-empty
    ``session_entries`` → pass (row 6), even with historical rows.

    cctally-dev#93: this is the new PROCEED signal (``walk✓ AND
    entries✓``) replacing the old post-001 ``session_files`` row.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z"),
    )
    _seed_walk_complete_marker(cache)
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    # Must NOT raise — complete, non-empty post-001 cache (row 6), even
    # with historical rows to protect.
    db_module._gate_001_post_ingest_completed(
        cache, projects, data_present=True,
    )


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


def test_gate_raises_when_layer_a_select_hits_sqlite_busy(db_module, tmp_path):
    """G5 — SQLITE_BUSY on Layer A's RO SELECT must translate to
    ``MigrationGateNotMet``, NOT escape as ``sqlite3.OperationalError``.

    A genuine BUSY would require a real concurrent writer; we simulate by
    wrapping the cache connection so its ``execute`` raises ``BUSY`` on
    the first call. Pre-fix, the dispatcher would log to
    ``migration-errors.log`` and render the error banner for a transient
    self-healing condition.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)

    class _BusyConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, *args, **kwargs):
            exc = sqlite3.OperationalError("database is locked")
            # SQLITE_BUSY = 5 — Python 3.11+ exposes this attribute.
            try:
                exc.sqlite_errorcode = 5
            except AttributeError:
                pass
            raise exc

    projects = tmp_path / "projects"
    projects.mkdir()
    with pytest.raises(db_module.MigrationGateNotMet, match="transiently locked"):
        db_module._gate_001_post_ingest_completed(_BusyConn(cache), projects)


def test_gate_raises_when_second_read_hits_sqlite_locked(db_module, tmp_path):
    """G5 — SQLITE_LOCKED on the SECOND gate read also defers.

    The first read (``schema_migrations`` SELECT) must succeed (001 is
    seeded applied), so the wrapper only raises on the SECOND ``execute``
    call. Post-cctally-dev#93 the second read is the ``cache_meta``
    walk-complete probe (the old ``session_files`` Layer B read is gone);
    a transient LOCKED there flips ``marker_state_readable=False`` →
    resolver row 1 DEFER.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z"),
    )

    class _LockedOnSecond:
        def __init__(self, inner):
            self._inner = inner
            self._calls = 0

        def execute(self, *args, **kwargs):
            self._calls += 1
            if self._calls >= 2:
                exc = sqlite3.OperationalError("database table is locked")
                try:
                    exc.sqlite_errorcode = 6  # SQLITE_LOCKED
                except AttributeError:
                    pass
                raise exc
            return self._inner.execute(*args, **kwargs)

    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")
    with pytest.raises(db_module.MigrationGateNotMet, match="transiently locked"):
        db_module._gate_001_post_ingest_completed(_LockedOnSecond(cache), projects)


def test_is_transient_sqlite_error_predicate(db_module):
    """G5 — ``_is_transient_sqlite_error`` accepts BUSY/LOCKED/CANTOPEN."""
    is_pred = db_module._is_transient_sqlite_error

    for code in (5, 6, 14):  # SQLITE_BUSY, SQLITE_LOCKED, SQLITE_CANTOPEN
        exc = sqlite3.OperationalError("synthetic")
        try:
            exc.sqlite_errorcode = code
        except AttributeError:
            pytest.skip("sqlite_errorcode is not settable on this Python")
        assert is_pred(exc) is True, f"errorcode {code} should be transient"

    # SQLITE_ERROR (1) — "no such table" is NOT transient (it's a missing
    # state we handle separately via _is_no_such_table_error).
    exc = sqlite3.OperationalError("no such table: foo")
    try:
        exc.sqlite_errorcode = 1
    except AttributeError:
        pass
    assert is_pred(exc) is False

    # SQLITE_IOERR (10) — real IO failure, not transient.
    exc = sqlite3.OperationalError("disk I/O error")
    try:
        exc.sqlite_errorcode = 10
    except AttributeError:
        pass
    assert is_pred(exc) is False


def test_gate_session_files_row_without_marker_is_not_proof(
    db_module, tmp_path,
):
    """A ``session_files`` row is NOT, by itself, proof of a complete
    post-001 ingest — the gate keys completeness on the ``cache_meta``
    walk-complete marker now (cctally-dev#93).

    A ``session_files`` row can exist after a partial/straddling walk
    (delta-resume state) without the walk-complete marker. With JSONL on
    disk and historical rows to protect, the gate must still DEFER (row
    7) — the marker, not a ``session_files`` row, is the signal.

    This is the cctally-dev#93 analogue of the old "pre-001
    session_files row is not post-001 proof" guard: the discriminator
    moved from a ``last_ingested_at`` timestamp compare to marker
    presence.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z"),
    )
    # A session_files row present (delta-resume bookkeeping) but NO
    # cache_meta walk-complete marker — not proof of a clean full walk.
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
    (projects / "session1.jsonl").write_text("{}\n")

    with pytest.raises(db_module.MigrationGateNotMet, match="ingest walk not yet complete"):
        db_module._gate_001_post_ingest_completed(
            cache, projects, data_present=True,
        )


def test_gate_passes_on_marker_regardless_of_ingest_timestamp(
    db_module, tmp_path,
):
    """The walk-complete marker is presence-checked, NOT timestamp-compared
    against the 001 marker (cctally-dev#93, spec D2).

    The OLD gate used a ``last_ingested_at >= 001.applied_at_utc``
    timestamp compare (with a known same-second / clock-skew hazard,
    Codex round-1 P2). The new gate keys completeness on marker
    PRESENCE, which is immune to wall-clock non-monotonicity. So even a
    walk-complete marker whose ``value`` timestamp PREDATES the 001
    marker (an impossible-in-practice clock-skew artifact) still passes
    — presence is the signal, the stored timestamp is debug-only. This
    pins that the corrupting-direction clock-skew hole is gone.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z"),
    )
    # Marker value timestamp deliberately BEFORE the 001 marker — under
    # the old >= compare this would have falsely deferred (or worse,
    # under a skewed clock, falsely passed). Presence semantics ignore it.
    cache.execute(
        "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
        (MARKER, "2026-05-22T16:00:00+00:00"),
    )
    cache.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model) "
        "VALUES (?, ?, ?, ?)",
        ("/tmp/session1.jsonl", 0, "2026-05-22T17:30:00Z", "claude-opus-4-7"),
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    # Must NOT raise — marker present + entries present (row 6).
    db_module._gate_001_post_ingest_completed(
        cache, projects, data_present=True,
    )


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


def test_dispatcher_gate_defer_clears_stale_error_log(
    db_module, tmp_path, monkeypatch,
):
    """P2 — When a migration that previously failed (and left a row in
    migration-errors.log) later transitions to gate-deferred (because
    its prereq has shifted state — e.g. operator removed a dependency
    that no longer exists, or the handler was rewritten to gate-defer
    where it previously raised), the dispatcher must clear the stale
    log entry on the gate-defer branch, symmetric with the success
    branch's existing ``_clear_migration_error_log_entries`` call.

    Without this, the migration-errors banner would render forever
    against a name whose underlying state is now "transiently gated,"
    not "broken." The contract is uniform: any non-failure outcome
    (apply OR gate-defer) clears any prior failure log for the
    qualified name.
    """
    import _cctally_core
    log_path = tmp_path / "migration-errors.log"
    monkeypatch.setattr(_cctally_core, "MIGRATION_ERROR_LOG_PATH", log_path)
    monkeypatch.setattr(_cctally_core, "LOG_DIR", tmp_path)

    test_seq = len(db_module._STATS_MIGRATIONS) + 1
    test_name = f"{test_seq:03d}_gate_defer_clears_log_test"
    qualified_name = f"stats.db:{test_name}"

    @db_module.stats_migration(test_name)
    def _always_gates(conn):
        raise db_module.MigrationGateNotMet(
            "prereq unsatisfied (test fixture)"
        )

    try:
        # Seed a stale error-log entry for this migration's qualified
        # name. The dispatcher should clear it on the gate-defer path.
        db_module._log_migration_error(
            name=qualified_name,
            exc=RuntimeError("prior failure (stale)"),
            tb="Traceback (most recent call last):\n  ...\n"
               "RuntimeError: prior failure (stale)\n",
        )
        assert log_path.exists(), "fixture setup: log entry should exist"
        assert qualified_name in log_path.read_text(), (
            "fixture setup: log should contain the qualified name"
        )

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

        # Dispatcher walk — test migration gate-defers; stale log entry
        # for its qualified name must be cleared.
        db_module._run_pending_migrations(
            conn,
            registry=db_module._STATS_MIGRATIONS,
            db_label="stats.db",
        )

        # Either the log file is gone (it was the only entry) or the
        # qualified-name entry no longer appears in it. Both are
        # correct outcomes per _clear_migration_error_log_entries'
        # contract.
        if log_path.exists():
            remaining = log_path.read_text()
            assert qualified_name not in remaining, (
                "gate-defer branch must clear the stale log entry for "
                f"{qualified_name}; got: {remaining!r}"
            )
        # No file at all is also acceptable (and the expected outcome
        # when the seeded entry was the only one).
    finally:
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
