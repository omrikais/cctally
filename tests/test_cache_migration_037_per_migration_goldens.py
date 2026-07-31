"""Per-migration goldens for the Codex quota change ledger.

Public issue omrikais/cctally#5. The migration exists because the cache schema
apply is version-gated: an install already at the registry head never re-runs
it, so an install that gained the ledger only through ``_apply_cache_schema``
would keep mutating ``quota_window_snapshots`` with no ledger behind it — and
the projector would then believe nothing had changed. That failure is silent,
which is why it gets its own registered migration rather than riding along.

The ledger must land EMPTY. It is a change log, not a snapshot of existing
state: back-filling one entry per historical row would make the first pass
after the upgrade re-materialize all history, which is exactly the cost this
work removes.
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "037_codex_quota_change_ledger"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "037_codex_quota_change_ledger"
LEDGER = "quota_window_change_log"
TRIGGERS = ["trg_qws_ledger_del", "trg_qws_ledger_ins", "trg_qws_ledger_upd"]


@pytest.fixture(scope="module")
def cctally_module():
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _handler(cctally_module):
    for migration in cctally_module._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"{MIGRATION} not registered")


def _has_table(conn, name) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _triggers(conn) -> list[str]:
    return [
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_qws_ledger%' ORDER BY name")
    ]


def _snapshot_rows(conn):
    return list(conn.execute(
        "SELECT source, source_root_key, source_path, line_offset, "
        "       captured_at_utc, used_percent, resets_at_utc, "
        "       canonical_resets_at_utc "
        "  FROM quota_window_snapshots ORDER BY source_path, line_offset"))


def test_pre_fixture_is_a_036_head_install_without_the_ledger(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 36
        assert not _has_table(conn, LEDGER)
        assert _triggers(conn) == []
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
        # Non-vacuity: a ledger over an empty table would prove nothing.
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == 2
    finally:
        conn.close()


def test_post_fixture_carries_the_ledger_and_all_three_triggers(cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 37
        assert _has_table(conn, LEDGER)
        assert _triggers(conn) == TRIGGERS
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_ledger_lands_empty_and_no_observation_moves(cctally_module):
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert post.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 0
        assert _snapshot_rows(post) == _snapshot_rows(pre)
    finally:
        pre.close()
        post.close()


def test_the_installed_triggers_actually_record(cctally_module, tmp_path):
    """A migrated install must be recording from its very next write.

    The goldens only prove the objects exist; this proves they work on the
    fixture's own shape.
    """
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(cctally_module)(conn)
        conn.execute(
            "UPDATE quota_window_snapshots SET used_percent=99.0 "
            "WHERE source='codex' AND line_offset=10")
        conn.commit()
        rows = list(conn.execute(
            f"SELECT op, old_canonical_resets_at_utc, "
            f"       new_canonical_resets_at_utc FROM {LEDGER}"))
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0][0] == "update"
    assert rows[0][1] == rows[0][2] == "2026-07-31T15:00:00Z"


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = (_triggers(conn), _snapshot_rows(conn),
                 conn.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0])
        assert first[0] == TRIGGERS
        handler(conn)
        assert (_triggers(conn), _snapshot_rows(conn),
                conn.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0]
                ) == first
    finally:
        conn.close()


def test_handler_degrades_on_a_cache_without_the_anchor_column(
    cctally_module, tmp_path
):
    """A legacy-shape cache whose schema apply never reached
    ``canonical_resets_at_utc`` cannot carry the triggers — their bodies would
    fail to resolve. It keeps the whole-history sweep instead of failing the
    migration."""
    work = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(work)
    try:
        conn.execute(
            "CREATE TABLE quota_window_snapshots ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,"
            " source_root_key TEXT, source_path TEXT NOT NULL,"
            " line_offset INTEGER NOT NULL, captured_at_utc TEXT NOT NULL,"
            " observed_slot TEXT, logical_limit_key TEXT NOT NULL,"
            " limit_id TEXT, limit_name TEXT, window_minutes INTEGER NOT NULL,"
            " used_percent REAL NOT NULL, resets_at_utc TEXT NOT NULL)"
        )
        conn.commit()
        _handler(cctally_module)(conn)
        assert not _has_table(conn, LEDGER)
        assert _triggers(conn) == []
    finally:
        conn.close()
