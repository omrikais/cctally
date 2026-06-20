"""Migration 019 + file-touches schema/envelope unit tests (#217 S2 / I-3).

Covers the load-bearing pieces of subtask I-3b:

  * ``conversation_file_touches`` table + its two indexes are created by
    ``_apply_cache_schema`` (fresh installs) — and CRITICALLY, created BEFORE the
    FTS5 ``legacy_present`` early-return (it is a PLAIN table with no dependency
    on the FTS shape), so a legacy-FTS-shape upgrade still has the table. This is
    the regression class the I-2 title-FTS bug taught us (create the new derived
    table early/unconditionally).
  * 019 registered + flag-only: arms ``conversation_reingest_file_touches_pending``,
    central-stamped, idempotent on rerun.
  * **P1-2 (load-bearing).** ``conversation_reingest_file_touches_pending`` joins
    ``_TARGETED_DECLINE_FLAGS`` ONLY — never ``_REINGEST_FLAG_KEYS`` (which would
    force a full message delete/reinsert + rowid churn the file-touch backfill
    must not trigger).
"""
from __future__ import annotations

import sqlite3
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db   # noqa: E402
import _cctally_cache as cc  # noqa: E402

_FLAG = "conversation_reingest_file_touches_pending"
_MIGRATION = "019_create_conversation_file_touches"


def _conn():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    return c


def _migration_handler(name):
    for m in db._CACHE_MIGRATIONS:
        if m.name == name:
            return m.handler
    raise AssertionError(f"cache migration {name} not registered")


def _flag(conn, key=_FLAG):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _index_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone() is not None


# --- I-3b: schema + table/indexes -----------------------------------------

def test_apply_cache_schema_creates_file_touches_table_and_index():
    c = _conn()
    assert _table_exists(c, "conversation_file_touches")
    cols = [r[1] for r in c.execute("PRAGMA table_info(conversation_file_touches)")]
    assert cols == ["message_id", "session_id", "uuid", "file_path", "tool"]
    # The kind=files PREFIX search needs the path index COLLATE NOCASE (review
    # Important #1: a BINARY index can't serve the default case-insensitive LIKE).
    assert _index_exists(c, "idx_file_touches_path")
    path_ddl = c.execute(
        "SELECT sql FROM sqlite_master WHERE name='idx_file_touches_path'"
    ).fetchone()[0]
    assert "COLLATE NOCASE" in path_ddl.upper(), path_ddl
    # The session index is dropped (review Minor #2): no query seeks by session_id;
    # the filtered-search session scope is a small post-match IN, not a btree seek.
    assert not _index_exists(c, "idx_file_touches_session")


def test_file_touches_unique_constraint():
    """UNIQUE(message_id, file_path, tool): a duplicate INSERT OR IGNORE is a
    no-op, but a different tool on the same (message_id, file_path) is a new row."""
    c = _conn()
    sql = ("INSERT OR IGNORE INTO conversation_file_touches"
           "(message_id, session_id, uuid, file_path, tool) VALUES(?,?,?,?,?)")
    c.execute(sql, (1, "s1", "u1", "bin/x", "Edit"))
    c.execute(sql, (1, "s1", "u1", "bin/x", "Edit"))   # dup -> ignored
    c.execute(sql, (1, "s1", "u1", "bin/x", "Write"))  # different tool -> kept
    assert c.execute(
        "SELECT count(*) FROM conversation_file_touches").fetchone()[0] == 2


def test_file_touches_created_before_legacy_fts_early_return():
    """CRITICAL (the I-2 legacy-shape crash class): on a pre-S6 install whose
    ``conversation_fts`` is still the legacy ``(text)`` shape, ``_apply_cache_schema``
    early-returns at its ``legacy_present`` guard. ``conversation_file_touches`` is
    a PLAIN table independent of the FTS shape and is created BEFORE that
    early-return, so it MUST exist even when the early-return fires (unlike the
    title FTS, which used to be created after the early-return and crashed its
    consumer on a legacy-shape upgrade)."""
    c = _conn()
    if not db._fts5_available(c):
        # No FTS5 build -> no legacy_present early-return path; the unconditional
        # create still applies (asserted by the first test). Nothing legacy to set up.
        return
    # Tear down to the pre-S6 legacy conversation_fts(text) shape and drop the
    # file-touches table, mirroring an old install that predates migration 019.
    db._drop_conversation_fts_triggers(c)
    c.execute("DROP TABLE IF EXISTS conversation_fts")
    c.execute("DROP TABLE IF EXISTS conversation_fts_aux")
    db._drop_conversation_title_fts_triggers(c)
    c.execute("DROP TABLE IF EXISTS conversation_title_fts")
    c.execute("DROP TABLE IF EXISTS conversation_file_touches")
    c.execute("CREATE VIRTUAL TABLE conversation_fts "
              "USING fts5(text, content='conversation_messages', content_rowid='id')")
    db._create_conversation_fts_aux_table(c)
    db._create_conversation_fts_legacy_triggers(c)
    c.commit()
    assert not _table_exists(c, "conversation_file_touches")

    # Re-apply the schema. The FTS5 branch early-returns at legacy_present (the
    # title FTS stays absent), but conversation_file_touches is created BEFORE
    # that point, so it must now exist.
    db._apply_cache_schema(c)
    assert c.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='conversation_title_fts'").fetchone() is None, \
        "sanity: legacy_present early-return left the title FTS uncreated"
    assert _table_exists(c, "conversation_file_touches"), \
        "the plain file-touches table must be created BEFORE the legacy early-return"


def test_019_registered_and_flag_only():
    c = _conn()
    assert _flag(c) is None
    _migration_handler(_MIGRATION)(c)
    assert _flag(c) == "1"


def test_019_idempotent_rerun():
    c = _conn()
    h = _migration_handler(_MIGRATION)
    h(c)
    h(c)  # must not raise
    assert _flag(c) == "1"


# --- P1-2: flag joins _TARGETED_DECLINE_FLAGS ONLY -------------------------

def test_file_touches_flag_in_targeted_decline_only_not_reingest():
    assert _FLAG in cc._TARGETED_DECLINE_FLAGS
    assert _FLAG not in cc._REINGEST_FLAG_KEYS
