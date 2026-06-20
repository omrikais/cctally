"""Migration 018 + title-FTS schema/envelope unit tests (#217 S2 / E7).

Covers the four load-bearing pieces of subtask I-2a / I-2b:

  * 018 registered + flag-only: creates ``conversation_title_fts`` (via
    ``_apply_cache_schema``, which 018 reruns), arms
    ``conversation_title_fts_backfill_pending``, central-stamped.
  * Title-FTS triggers keep the external-content index in sync on
    insert/update/delete of ``conversation_ai_titles``.
  * **P1-6 (load-bearing).** On an FTS5-absent build the title triggers are
    DROPPED (folded into the same ``fts5_unavailable`` envelope as the message
    FTS), so a ``conversation_ai_titles`` upsert does NOT roll back the ingest
    transaction (it would, if a trigger fired against a missing/unusable vtable).
  * **P1-7 (load-bearing).** ``_consume_title_fts`` populates the index via the
    FTS5 ``'rebuild'`` command and is idempotent under the 012-then-018
    both-pending ordering (012's backfill may have already populated the index
    via triggers; a second rebuild yields the same row count, no duplicates),
    clearing the flag.
  * **P1-2 (load-bearing).** ``conversation_title_fts_backfill_pending`` joins
    ``_TARGETED_DECLINE_FLAGS`` ONLY — never ``_REINGEST_FLAG_KEYS`` (which would
    force a full message delete/reinsert + rowid churn the title backfill must
    not trigger).
"""
from __future__ import annotations

import sqlite3
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db   # noqa: E402
import _cctally_cache as cc  # noqa: E402

_FLAG = "conversation_title_fts_backfill_pending"


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


def _upsert_title(conn, sid, title):
    conn.execute(cc._AI_TITLE_UPSERT_SQL, (sid, title, "a.jsonl", 0))


# --- I-2a: schema + migration registration --------------------------------

def test_apply_cache_schema_creates_title_fts():
    """Fresh installs get the external-content title FTS table from
    ``_apply_cache_schema`` (FTS5-available branch)."""
    c = _conn()
    if not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
    cols = [r[1] for r in c.execute("PRAGMA table_info(conversation_title_fts)")]
    assert "ai_title" in cols


def test_018_registered_and_flag_only():
    """018 is flag-only for existing installs: arms the title-FTS backfill flag,
    no data table touched; the dispatcher central-stamps the marker."""
    c = _conn()
    assert _flag(c) is None
    _migration_handler("018_create_conversation_title_fts")(c)
    assert _flag(c) == "1"


def test_018_idempotent_rerun():
    c = _conn()
    h = _migration_handler("018_create_conversation_title_fts")
    h(c)
    h(c)  # must not raise
    assert _flag(c) == "1"


# --- title-FTS trigger sync ------------------------------------------------

def test_title_fts_triggers_sync_insert_update_delete():
    c = _conn()
    if not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
    _upsert_title(c, "s1", "refactor the cache module")
    rows = c.execute(
        "SELECT rowid FROM conversation_title_fts "
        "WHERE conversation_title_fts MATCH 'refactor'").fetchall()
    assert len(rows) == 1
    # update path (AU trigger): the new term matches, the old does not
    _upsert_title(c, "s1", "rewrite the parser")
    assert c.execute(
        "SELECT count(*) FROM conversation_title_fts "
        "WHERE conversation_title_fts MATCH 'refactor'").fetchone()[0] == 0
    assert c.execute(
        "SELECT count(*) FROM conversation_title_fts "
        "WHERE conversation_title_fts MATCH 'parser'").fetchone()[0] == 1
    # delete path (AD trigger)
    c.execute("DELETE FROM conversation_ai_titles WHERE session_id='s1'")
    assert c.execute(
        "SELECT count(*) FROM conversation_title_fts "
        "WHERE conversation_title_fts MATCH 'parser'").fetchone()[0] == 0


# --- P1-6: FTS5-absent build must not roll back the ingest -----------------

def test_fts5_absent_title_upsert_does_not_roll_back(monkeypatch):
    """P1-6: on a build without FTS5, the title triggers are dropped (same
    envelope as the message FTS), so a ``conversation_ai_titles`` upsert succeeds
    instead of erroring on the missing fts5 module and rolling back the txn."""
    c = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    db._apply_cache_schema(c)
    # No conversation_title_fts vtable on a no-FTS5 build.
    has_tfts = c.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='conversation_title_fts'").fetchone()
    assert has_tfts is None
    # No title trigger may survive (it would fire against the missing vtable).
    trigs = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' "
        "AND name LIKE 'conv_title_fts_%'").fetchall()]
    assert trigs == []
    # The upsert must NOT raise / roll back.
    _upsert_title(c, "s1", "no fts here")
    got = c.execute(
        "SELECT ai_title FROM conversation_ai_titles WHERE session_id='s1'"
    ).fetchone()
    assert got and got[0] == "no fts here"


# --- P1-7: _consume_title_fts via 'rebuild', idempotent -------------------

def test_consume_title_fts_rebuild_idempotent():
    """P1-7: 'rebuild' is idempotent even if 012's ai-title backfill already
    populated the index via triggers (the 012+018 both-pending ordering)."""
    c = _conn()
    if not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
    _upsert_title(c, "s1", "alpha bravo")
    _upsert_title(c, "s2", "charlie delta")
    db._set_cache_meta(c, _FLAG, "1")
    cc._consume_title_fts(c)
    n1 = c.execute("SELECT count(*) FROM conversation_title_fts").fetchone()[0]
    db._set_cache_meta(c, _FLAG, "1")
    cc._consume_title_fts(c)               # re-run (012+018 both-pending)
    n2 = c.execute("SELECT count(*) FROM conversation_title_fts").fetchone()[0]
    assert n1 == n2 and n1 == 2            # 'rebuild' is idempotent, no dupes
    assert _flag(c) is None                # flag cleared


def test_consume_title_fts_no_fts5_just_clears_flag(monkeypatch):
    c = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    db._apply_cache_schema(c)
    db._set_cache_meta(c, _FLAG, "1")
    cc._consume_title_fts(c)               # must not raise on the absent vtable
    assert _flag(c) is None


# --- P1-2: flag joins _TARGETED_DECLINE_FLAGS ONLY -------------------------

def test_title_flag_in_targeted_decline_only_not_reingest():
    assert _FLAG in cc._TARGETED_DECLINE_FLAGS
    assert _FLAG not in cc._REINGEST_FLAG_KEYS
