"""Unit tests for the migration framework primitives in bin/cctally.

Tests are imported via the conftest.py shim that loads bin/cctally
as a module. See tests/conftest.py for the loader (already in repo).
"""

from __future__ import annotations

import sqlite3

import pytest


# ──────────────────────────────────────────────────────────────────────
# add_column_if_missing
# ──────────────────────────────────────────────────────────────────────

def test_add_column_if_missing_adds_when_absent(cctally_module):
    """Returns True and adds the column when absent."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    added = cctally_module.add_column_if_missing(conn, "t", "extra", "TEXT")
    assert added is True
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(t)").fetchall()}
    assert "extra" in cols


def test_add_column_if_missing_noop_when_present(cctally_module):
    """Returns False and does not error when the column already exists."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, extra TEXT)")
    added = cctally_module.add_column_if_missing(conn, "t", "extra", "TEXT")
    assert added is False


def test_add_column_if_missing_rejects_bad_table_name(cctally_module):
    """Defensive: reject names that don't match the identifier regex."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    with pytest.raises(ValueError, match="invalid identifier"):
        cctally_module.add_column_if_missing(conn, "t; DROP TABLE t", "x", "TEXT")
    with pytest.raises(ValueError, match="invalid identifier"):
        cctally_module.add_column_if_missing(conn, "t", "x; DROP", "TEXT")


# ──────────────────────────────────────────────────────────────────────
# Migration registry primitives
# ──────────────────────────────────────────────────────────────────────

def test_stats_migration_decorator_registers_in_order(cctally_module):
    """First registration gets seq=N+1 where N=len(_STATS_MIGRATIONS) at decoration time."""
    initial_len = len(cctally_module._STATS_MIGRATIONS)
    expected_prefix = f"{initial_len + 1:03d}_"

    @cctally_module.stats_migration(f"{expected_prefix}testing_decorator")
    def _fake(conn):
        pass

    last = cctally_module._STATS_MIGRATIONS[-1]
    assert last.seq == initial_len + 1
    assert last.name == f"{expected_prefix}testing_decorator"
    # Cleanup so this test is idempotent across reruns within the session.
    cctally_module._STATS_MIGRATIONS.pop()


def test_stats_migration_rejects_wrong_prefix(cctally_module):
    """Numeric prefix must equal len(registry) + 1 at decoration time."""
    initial_len = len(cctally_module._STATS_MIGRATIONS)
    bad_prefix = f"{initial_len + 99:03d}_"
    with pytest.raises(RuntimeError, match="must be named"):

        @cctally_module.stats_migration(f"{bad_prefix}testing_bad")
        def _fake(conn):
            pass


def test_stats_migration_rejects_invalid_name(cctally_module):
    """Names must match ^\\d{3}_[a-z0-9_]+$."""
    with pytest.raises(RuntimeError, match="invalid"):

        @cctally_module.stats_migration("00X_oops_caps_letters")
        def _fake(conn):
            pass


def test_stats_migration_rejects_duplicate(cctally_module):
    """A re-decorate of the same name should fail at the second decoration."""
    initial_len = len(cctally_module._STATS_MIGRATIONS)
    name = f"{initial_len + 1:03d}_dup_test"

    @cctally_module.stats_migration(name)
    def _first(conn):
        pass

    with pytest.raises(RuntimeError, match="duplicated"):

        @cctally_module.stats_migration(name)
        def _second(conn):
            pass

    cctally_module._STATS_MIGRATIONS.pop()


def test_cache_migration_separate_sequence(cctally_module):
    """_CACHE_MIGRATIONS numbers independently from _STATS_MIGRATIONS."""
    cache_prefix = f"{len(cctally_module._CACHE_MIGRATIONS) + 1:03d}_"

    @cctally_module.cache_migration(f"{cache_prefix}cache_test")
    def _fake(conn):
        pass

    assert cctally_module._CACHE_MIGRATIONS[-1].name == f"{cache_prefix}cache_test"
    cctally_module._CACHE_MIGRATIONS.pop()


# ──────────────────────────────────────────────────────────────────────
# DowngradeDetected
# ──────────────────────────────────────────────────────────────────────

def test_downgrade_detected_carries_fields(cctally_module):
    exc = cctally_module.DowngradeDetected("stats.db", db_version=9, max_known=7)
    assert exc.db_label == "stats.db"
    assert exc.db_version == 9
    assert exc.max_known == 7
    assert "stats.db" in str(exc)
    assert "9" in str(exc) and "7" in str(exc)


# ──────────────────────────────────────────────────────────────────────
# _bootstrap_rename_legacy_markers
# ──────────────────────────────────────────────────────────────────────

def _seed_legacy_markers(conn):
    """Helper: create schema_migrations and insert the three legacy rows."""
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        [
            ("five_hour_block_models_backfill_v1",   "2026-04-30T12:00:00Z"),
            ("five_hour_block_projects_backfill_v1", "2026-04-30T12:00:00Z"),
            ("merge_5h_block_duplicates_v1",         "2026-05-04T08:00:00Z"),
        ],
    )


def test_bootstrap_rename_renames_three_legacy(cctally_module):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_legacy_markers(conn)
    cctally_module._bootstrap_rename_legacy_markers(conn, "stats.db")
    names = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    assert names == {
        "001_five_hour_block_models_backfill_v1",
        "002_five_hour_block_projects_backfill_v1",
        "003_merge_5h_block_duplicates_v1",
    }


def test_bootstrap_rename_idempotent(cctally_module):
    """Second invocation is a no-op (UPDATE WHERE name=old finds nothing)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_legacy_markers(conn)
    cctally_module._bootstrap_rename_legacy_markers(conn, "stats.db")
    cctally_module._bootstrap_rename_legacy_markers(conn, "stats.db")
    names = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    assert len(names) == 3
    assert all(n.startswith(("001_", "002_", "003_")) for n in names)


def test_bootstrap_rename_idempotent_when_both_legacy_and_prefixed_exist(cctally_module):
    """When BOTH the legacy unprefixed AND the prefixed marker rows
    are present (a user briefly ran a framework build, then reverted
    to a pre-framework binary that re-applied the legacy markers), a
    plain ``UPDATE name = prefixed WHERE name = legacy`` would collide
    on schema_migrations.PRIMARY KEY and abort the whole bootstrap,
    permanently blocking the dispatcher from running ANY downstream
    migration. The fix: DELETE the legacy row when its prefixed
    counterpart already exists; keep the prefixed row (it carries the
    dispatcher-managed applied_at_utc)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    # Both legacy and prefixed for ONE migration; pure legacy for another.
    conn.executemany(
        "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        [
            # Prefixed (dispatcher-managed, earlier date — authoritative).
            ("001_five_hour_block_models_backfill_v1",   "2026-04-30T12:00:00Z"),
            # Legacy duplicate of #1 written by a reverted pre-framework
            # binary on a later date — colliding row.
            ("five_hour_block_models_backfill_v1",       "2026-05-06T10:33:15Z"),
            # Pure legacy for #2 — should be renamed in the normal path.
            ("five_hour_block_projects_backfill_v1",     "2026-04-30T12:00:00Z"),
        ],
    )

    # Pre-fix this raised sqlite3.IntegrityError; post-fix it returns clean.
    cctally_module._bootstrap_rename_legacy_markers(conn, "stats.db")

    rows = {r["name"]: r["applied_at_utc"]
            for r in conn.execute(
                "SELECT name, applied_at_utc FROM schema_migrations"
            ).fetchall()}
    # The duplicate-collision case: legacy DROPPED, prefixed PRESERVED
    # with its own applied_at_utc (NOT overwritten by the legacy row's
    # later timestamp).
    assert "five_hour_block_models_backfill_v1" not in rows
    assert rows["001_five_hour_block_models_backfill_v1"] == "2026-04-30T12:00:00Z"
    # The pure-legacy case: renamed to prefixed (UPDATE path).
    assert "five_hour_block_projects_backfill_v1" not in rows
    assert "002_five_hour_block_projects_backfill_v1" in rows


def test_bootstrap_rename_skips_cache_db(cctally_module):
    """Cache.db has no pre-framework markers; bootstrap is a no-op."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_legacy_markers(conn)  # seeded same way, but db_label is cache.db
    cctally_module._bootstrap_rename_legacy_markers(conn, "cache.db")
    names = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    # Cache.db path was a no-op — names are still legacy.
    assert "five_hour_block_models_backfill_v1" in names


def test_bootstrap_rename_clears_legacy_error_log_entries(cctally_module, tmp_path, monkeypatch):
    """When a legacy log entry exists, bootstrap drops it after the rename.

    Post-#84 (data-globals promotion 2026-05-22), the migration-error
    sentinel helpers in ``_cctally_db`` read
    ``_cctally_core.MIGRATION_ERROR_LOG_PATH`` at call time. Patch the
    kernel module directly — the previous ``helper.__globals__``
    indirection (which targeted the seeded bare-name lookup) no longer
    propagates.
    """
    import _cctally_core
    log_path = tmp_path / "migration-errors.log"
    monkeypatch.setattr(_cctally_core, "MIGRATION_ERROR_LOG_PATH", log_path)
    log_path.write_text(
        "[2026-05-01T10:00:00Z] merge_5h_block_duplicates_v1\n"
        "  ValueError: bad row\n"
        "  Traceback (most recent call last):\n"
        "    ...\n\n"
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_legacy_markers(conn)
    cctally_module._bootstrap_rename_legacy_markers(conn, "stats.db")
    assert not log_path.exists() or log_path.read_text().strip() == ""


# ──────────────────────────────────────────────────────────────────────
# _run_pending_migrations dispatcher
# ──────────────────────────────────────────────────────────────────────

def _fresh_conn():
    """In-memory SQLite connection with row_factory set to Row (mirrors stats.db)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def _fresh_conn_cache():
    """In-memory connection without row_factory (mirrors cache.db)."""
    return sqlite3.connect(":memory:")


def test_dispatcher_downgrade_raises(cctally_module):
    """user_version > len(registry) → raise DowngradeDetected."""
    conn = _fresh_conn()
    conn.execute("PRAGMA user_version = 99")
    fake_registry = [
        cctally_module.Migration(seq=1, name="001_a", handler=lambda c: None),
    ]
    with pytest.raises(cctally_module.DowngradeDetected) as excinfo:
        cctally_module._run_pending_migrations(
            conn, registry=fake_registry, db_label="stats.db",
        )
    assert excinfo.value.db_version == 99
    assert excinfo.value.max_known == 1


def test_dispatcher_fast_path_skips_walk(cctally_module):
    """user_version == len(registry) → return early, never invoke handler."""
    conn = _fresh_conn()
    conn.execute("PRAGMA user_version = 1")

    invoked = []
    fake_registry = [
        cctally_module.Migration(seq=1, name="001_a", handler=lambda c: invoked.append("a")),
    ]
    cctally_module._run_pending_migrations(
        conn, registry=fake_registry, db_label="stats.db",
    )
    assert invoked == []  # handler never called on fast path


def test_dispatcher_fresh_install_stamps_only(cctally_module):
    """Fresh DB (no schema_migrations) → stamp every migration applied without invoking."""
    conn = _fresh_conn()

    invoked = []
    fake_registry = [
        cctally_module.Migration(seq=1, name="001_a", handler=lambda c: invoked.append("a")),
        cctally_module.Migration(seq=2, name="002_b", handler=lambda c: invoked.append("b")),
    ]
    cctally_module._run_pending_migrations(
        conn, registry=fake_registry, db_label="stats.db",
    )
    assert invoked == []
    rows = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    assert rows == {"001_a", "002_b"}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_dispatcher_upgrade_runs_pending(cctally_module):
    """One marker present → run only the missing one."""
    conn = _fresh_conn()
    # Pre-create as if a previous open had applied 001_a.
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('001_a', '2026-05-01T00:00:00Z')"
    )
    conn.commit()

    invoked = []

    def b_handler(c):
        invoked.append("b")
        c.execute(
            "INSERT INTO schema_migrations (name, applied_at_utc) VALUES ('002_b', '2026-05-06T00:00:00Z')"
        )
        c.commit()

    fake_registry = [
        cctally_module.Migration(seq=1, name="001_a", handler=lambda c: invoked.append("a")),
        cctally_module.Migration(seq=2, name="002_b", handler=b_handler),
    ]
    cctally_module._run_pending_migrations(
        conn, registry=fake_registry, db_label="stats.db",
    )
    assert invoked == ["b"]  # 'a' skipped (already applied); 'b' ran
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_dispatcher_failure_breaks_loop(cctally_module, tmp_path, monkeypatch):
    """Migration N raises Exception → log + break; later migrations DO NOT run.

    Post-#84 the migration-error sentinel helpers read
    ``_cctally_core.MIGRATION_ERROR_LOG_PATH`` at call time, so we
    patch the kernel module directly.
    """
    import _cctally_core
    log_path = tmp_path / "migration-errors.log"
    monkeypatch.setattr(_cctally_core, "MIGRATION_ERROR_LOG_PATH", log_path)

    conn = _fresh_conn()
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    conn.commit()

    invoked = []

    def a_handler(c):
        invoked.append("a-start")
        raise RuntimeError("planned a failure")

    def b_handler(c):
        invoked.append("b-start")  # MUST NOT happen
        c.execute(
            "INSERT INTO schema_migrations VALUES ('002_b', '2026-05-06T00:00:00Z')"
        )
        c.commit()

    fake_registry = [
        cctally_module.Migration(seq=1, name="001_a", handler=a_handler),
        cctally_module.Migration(seq=2, name="002_b", handler=b_handler),
    ]
    cctally_module._run_pending_migrations(
        conn, registry=fake_registry, db_label="stats.db",
    )
    assert invoked == ["a-start"]  # 'b' was NOT invoked
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert log_path.exists()
    assert "001_a" in log_path.read_text()


def test_dispatcher_keyboard_interrupt_propagates(cctally_module):
    """BaseException is NOT caught — KeyboardInterrupt escapes the dispatcher.

    Seed schema_migrations as a pre-existing (non-empty) table so the
    dispatcher takes the upgrade path and actually invokes the handler.
    On the fresh-install path handlers are stamp-only, which would
    silently mask the BaseException-handling contract under test.
    """
    conn = _fresh_conn()
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('999_seed_marker', '2026-05-01T00:00:00Z')"
    )
    conn.commit()

    def a_handler(c):
        raise KeyboardInterrupt()

    fake_registry = [
        cctally_module.Migration(seq=1, name="001_a", handler=a_handler),
    ]
    with pytest.raises(KeyboardInterrupt):
        cctally_module._run_pending_migrations(
            conn, registry=fake_registry, db_label="stats.db",
        )


def test_dispatcher_skip_set_honored(cctally_module):
    """A migration in schema_migrations_skipped is not invoked."""
    conn = _fresh_conn()
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE schema_migrations_skipped (name TEXT PRIMARY KEY, skipped_at_utc TEXT NOT NULL, reason TEXT)"
    )
    conn.execute(
        "INSERT INTO schema_migrations_skipped VALUES ('001_a', '2026-05-06T00:00:00Z', NULL)"
    )
    conn.commit()

    invoked = []
    fake_registry = [
        cctally_module.Migration(seq=1, name="001_a", handler=lambda c: invoked.append("a")),
    ]
    cctally_module._run_pending_migrations(
        conn, registry=fake_registry, db_label="stats.db",
    )
    assert invoked == []
    # Skipped counts toward fast-path advancement (spec §3.5).
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_dispatcher_auto_clears_error_log_on_success(cctally_module, tmp_path, monkeypatch):
    """A previously-failing migration that now succeeds clears its log block.

    Post-#84 the migration-error sentinel helpers read
    ``_cctally_core.MIGRATION_ERROR_LOG_PATH`` at call time, so we
    patch the kernel module directly.
    """
    import _cctally_core
    log_path = tmp_path / "migration-errors.log"
    monkeypatch.setattr(_cctally_core, "MIGRATION_ERROR_LOG_PATH", log_path)
    log_path.write_text(
        "[2026-05-01T10:00:00Z] stats.db:001_a\n"
        "  RuntimeError: prior failure\n"
        "  Traceback: ...\n\n"
    )

    conn = _fresh_conn()
    # Seed schema_migrations as pre-existing (non-empty) so the dispatcher
    # takes the upgrade path and invokes the handler — the fresh-install
    # path is stamp-only and would skip the auto-clear we're testing.
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('999_seed_marker', '2026-05-01T00:00:00Z')"
    )
    conn.commit()

    def a_handler(c):
        c.execute(
            "INSERT INTO schema_migrations VALUES ('001_a', '2026-05-06T00:00:00Z')"
        )
        c.commit()

    fake_registry = [
        cctally_module.Migration(seq=1, name="001_a", handler=a_handler),
    ]
    cctally_module._run_pending_migrations(
        conn, registry=fake_registry, db_label="stats.db",
    )
    assert (not log_path.exists()) or log_path.read_text().strip() == ""


def test_dispatcher_works_against_cache_db_without_row_factory(cctally_module):
    """Cache.db connections don't set row_factory; dispatcher must be tuple-safe."""
    conn = _fresh_conn_cache()  # NO row_factory

    invoked = []
    fake_registry = [
        cctally_module.Migration(seq=1, name="001_cache_a", handler=lambda c: invoked.append("a")),
    ]
    # Should not raise even with default tuple row factory.
    cctally_module._run_pending_migrations(
        conn, registry=fake_registry, db_label="cache.db",
    )
    # Fresh-install stamp-only path; handler not invoked.
    assert invoked == []
    rows = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    assert "001_cache_a" in rows


def test_dispatcher_tuple_safe_select_against_seeded_cache_db(cctally_module):
    """Confirms row[0] indexing works when the SELECT actually returns rows.

    The sibling test ``test_dispatcher_works_against_cache_db_without_row_factory``
    exercises the fresh-install path only — the SELECT runs against a freshly
    CREATEd empty schema_migrations table, so the ``row[0]`` set comprehension
    never iterates over an actual row. If a future regression flipped ``row[0]``
    back to ``row["name"]`` that test would still pass because ``row["name"]``
    is never evaluated.

    This test seeds schema_migrations on a tuple-row connection (cache.db
    parity) BEFORE calling the dispatcher, so the SELECT returns non-empty
    rows. Under a ``row["name"]`` regression sqlite3 raises
    ``IndexError: No item with that key`` (or TypeError on some builds)
    before the registry loop runs, and this test fails loudly.
    """
    conn = _fresh_conn_cache()  # NO row_factory
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES ('001_cache_a', '2026-05-01T00:00:00Z')"
    )
    conn.commit()

    invoked = []

    def b_handler(c):
        invoked.append("b")
        c.execute(
            "INSERT INTO schema_migrations VALUES ('002_cache_b', '2026-05-06T00:00:00Z')"
        )
        c.commit()

    fake_registry = [
        cctally_module.Migration(seq=1, name="001_cache_a", handler=lambda c: invoked.append("a")),
        cctally_module.Migration(seq=2, name="002_cache_b", handler=b_handler),
    ]
    cctally_module._run_pending_migrations(
        conn, registry=fake_registry, db_label="cache.db",
    )
    assert invoked == ["b"]  # 'a' skipped via tuple-safe applied set
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


# ── _recover_version_ahead (issue #145) ──────────────────────────────────

def _mk_db_with_markers(tmp_path, *, user_version, applied=(), skipped=()):
    import sqlite3
    p = tmp_path / "x.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
    conn.execute("CREATE TABLE schema_migrations_skipped (name TEXT PRIMARY KEY, skipped_at_utc TEXT NOT NULL, reason TEXT)")
    for n in applied:
        conn.execute("INSERT INTO schema_migrations VALUES (?, '2026-01-01T00:00:00Z')", (n,))
    for n in skipped:
        conn.execute("INSERT INTO schema_migrations_skipped VALUES (?, '2026-01-01T00:00:00Z', 'x')", (n,))
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.commit()
    return conn


def _registry(cctally_module, *names):
    Migration = cctally_module.Migration
    return [Migration(seq=i + 1, name=n, handler=lambda c: None) for i, n in enumerate(names)]


def test_recover_noop_when_not_ahead(cctally_module, tmp_path):
    conn = _mk_db_with_markers(tmp_path, user_version=1, applied=["001_a"])
    reg = _registry(cctally_module, "001_a", "002_b")  # head 2, db at 1 (behind)
    info = cctally_module._recover_version_ahead(conn, reg, "cache.db")
    assert info["reverted_from"] == 1 and info["reverted_to"] == 1 and info["trimmed"] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_recover_trims_unknown_from_both_tables_common_case(cctally_module, tmp_path):
    # Ahead: head=1 (knows 001_a), db at 2 with an unknown applied + unknown skipped.
    conn = _mk_db_with_markers(
        tmp_path, user_version=2, applied=["001_a", "002_unknown"], skipped=["003_unknown_skip"],
    )
    reg = _registry(cctally_module, "001_a")
    info = cctally_module._recover_version_ahead(conn, reg, "cache.db")
    applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    skipped = {r[0] for r in conn.execute("SELECT name FROM schema_migrations_skipped")}
    assert applied == {"001_a"}                 # unknown applied trimmed
    assert skipped == set()                     # unknown skipped trimmed (P1#1)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1   # all known applied → head
    assert info["reverted_from"] == 2 and info["reverted_to"] == 1 and info["trimmed"] == 2


def test_recover_adversarial_missing_known_marker_resets_to_zero(cctally_module, tmp_path):
    # Ahead AND a known marker is missing → must NOT cement a fast-path (P1#2).
    conn = _mk_db_with_markers(tmp_path, user_version=2, applied=["002_unknown"])
    reg = _registry(cctally_module, "001_a")   # 001_a NOT present
    info = cctally_module._recover_version_ahead(conn, reg, "cache.db")
    assert {r[0] for r in conn.execute("SELECT name FROM schema_migrations")} == set()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0   # reconcile, not fast-path
    assert info["reverted_to"] == 0


def test_recover_tolerates_absent_skipped_table(cctally_module, tmp_path):
    import sqlite3
    p = tmp_path / "y.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations VALUES ('001_a','t'), ('002_unknown','t')")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    reg = _registry(cctally_module, "001_a")
    info = cctally_module._recover_version_ahead(conn, reg, "cache.db")   # must not raise
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert info["reverted_to"] == 1


# ── dispatcher recover_version_ahead opt-in (issue #145) ──────────────────

def test_dispatcher_recovers_cache_when_opted_in(cctally_module, tmp_path, capsys):
    conn = _mk_db_with_markers(tmp_path, user_version=5, applied=["001_z"])
    reg = _registry(cctally_module, "001_z")  # head 1, db at 5 (ahead)
    # Opted-in (cache.db semantics): heals instead of raising.
    cctally_module._run_pending_migrations(
        conn, registry=reg, db_label="cache.db", recover_version_ahead=True,
    )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    err = capsys.readouterr().err
    assert "cache.db was ahead" in err and "cache-sync --rebuild" in err


def test_dispatcher_raises_when_not_opted_in(cctally_module, tmp_path):
    import pytest
    conn = _mk_db_with_markers(tmp_path, user_version=5, applied=["001_z"])
    reg = _registry(cctally_module, "001_z")
    with pytest.raises(cctally_module.DowngradeDetected):
        cctally_module._run_pending_migrations(
            conn, registry=reg, db_label="stats.db",  # default recover_version_ahead=False
        )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5  # untouched


# ── enriched DowngradeDetected message (issue #145) ───────────────────────

def test_downgrade_detected_message(cctally_module):
    exc = cctally_module.DowngradeDetected("stats.db", db_version=9, max_known=7)
    msg = str(exc)
    assert "stats.db is at version 9 but this cctally only knows up to 7." in msg
    assert "cctally db recover --db stats" in msg


# ── central schema_migrations stamp owned by the dispatcher (issue #140) ──

def test_stampless_migration_persists_and_is_not_rewalked(
    cctally_module, tmp_path, monkeypatch
):
    """A handler that does NOT self-stamp must still get its schema_migrations
    marker persisted by the dispatcher (issue #140), so it is not re-walked
    when the registry later grows.

    Why registry growth (Codex P1): the dispatcher advances PRAGMA
    user_version from its IN-MEMORY ``applied`` set
    (``applied.add(m.name)`` after any clean handler return, then
    ``PRAGMA user_version = len(registry)``), so a fixed-registry two-call
    test would pass even with no marker persisted — run 1 already set
    user_version = len(registry) and run 2 fast-paths. The bug only
    manifests when ``cur_version < len(registry)`` and ``applied`` is
    reloaded from the persisted ``schema_migrations`` table — i.e. when the
    registry GROWS between opens.
    """
    import _cctally_core
    monkeypatch.setattr(
        _cctally_core, "MIGRATION_ERROR_LOG_PATH",
        tmp_path / "migration-errors.log",
    )

    db_path = tmp_path / "stats.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Existing install (NOT fresh): pre-create schema_migrations so the
        # dispatcher takes the handler-running path, not the fresh-install
        # stamp-without-running fast path.
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
        )
        conn.commit()

        runs = []

        def stampless(c):
            c.execute("CREATE TABLE IF NOT EXISTS _probe(x)")
            c.execute("INSERT INTO _probe VALUES (1)")
            c.commit()                      # real data commit, NO self-stamp
            runs.append("999")

        def other(c):
            c.commit()                      # trivial later migration
            runs.append("1000")

        m999 = cctally_module.Migration(
            seq=1, name="999_stampless", handler=stampless
        )
        m1000 = cctally_module.Migration(
            seq=2, name="1000_probe", handler=other
        )

        # Run 1 — registry = [999].
        cctally_module._run_pending_migrations(
            conn, registry=[m999], db_label="stats.db"
        )
        # ASSERTION A — marker persisted (RED without the central stamp:
        # schema_migrations empty here even though user_version already
        # advanced to 1 from the in-memory applied set).
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='999_stampless'"
        ).fetchone()[0] == 1

        # Run 2 — registry GREW to [999, 1000]; cur_version(1) < len(2) ⇒
        # the dispatcher reloads ``applied`` from the PERSISTED
        # schema_migrations.
        cctally_module._run_pending_migrations(
            conn, registry=[m999, m1000], db_label="stats.db"
        )
        # ASSERTION B — 999 not re-walked (RED without stamp:
        # runs == ["999", "999", "1000"]).
        assert runs == ["999", "1000"]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()
