"""#177 S6 Task 1c/1d: migration 010 (flag-only) + the flock-held
consume-search-split backfill/swap in sync_cache, plus the
conversation_search_depth interim signal."""
import json
import sqlite3
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db
import _cctally_cache as cache
import _lib_conversation as lc


def _legacy_conn():
    """Old-shape cache DB: single-column conversation_fts(text) + legacy
    conversation_fts_aux + legacy triggers + the migration-010 pending flag set.
    The base columns search_tool/search_thinking already exist (idempotent adds)
    so the backfill UPDATEs target real columns."""
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    if not db._fts5_available(c):
        return c
    db._drop_conversation_fts_triggers(c)
    c.execute("DROP TABLE IF EXISTS conversation_fts")
    c.execute("DROP TABLE IF EXISTS conversation_fts_aux")
    c.execute("CREATE VIRTUAL TABLE conversation_fts "
              "USING fts5(text, content='conversation_messages', content_rowid='id')")
    db._create_conversation_fts_aux_table(c)
    db._create_conversation_fts_legacy_triggers(c)
    db._set_cache_meta(c, "conversation_search_split_pending", "1")
    c.commit()
    return c


def _insert_row(c, rid, blocks, *, search_tool="", search_thinking="",
                text="", uuid=None):
    c.execute(
        "INSERT INTO conversation_messages"
        "(id,session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
        " blocks_json,is_sidechain,search_tool,search_thinking)"
        " VALUES(?,?,?,?,?, 't','assistant',?,?,0,?,?)",
        (rid, "s1", uuid or f"u{rid}", f"f{rid}", rid, text,
         json.dumps(blocks, separators=(",", ":")), search_tool, search_thinking))


# === migration 010: flag-only ===

def test_migration_010_is_flag_only():
    c = _legacy_conn()
    # invoke the handler by registry lookup (matches the dispatcher's call shape)
    handler = next(m.handler for m in db._CACHE_MIGRATIONS
                   if m.name == "010_conversation_search_split")
    handler(c)
    assert c.execute(
        "SELECT value FROM cache_meta WHERE key='conversation_search_split_pending'"
    ).fetchone() == ("1",)
    # no data change, no FTS change:
    if db._fts5_available(c):
        assert db._conversation_fts_is_split(c) is False


def test_migration_010_registered_after_009():
    names = [m.name for m in db._CACHE_MIGRATIONS]
    assert "010_conversation_search_split" in names
    assert names.index("010_conversation_search_split") > names.index(
        "009_conversation_media_reingest")


# === consumer: backfill + swap ===

def test_consume_search_split_backfills_and_swaps():
    c = _legacy_conn()
    if not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
    _insert_row(c, 1, [
        {"kind": "thinking", "text": "ponder"},
        {"kind": "tool_use", "name": "Bash", "input": {"command": "rg needle"}}])
    c.commit()
    cache._consume_search_split(c)
    row = c.execute(
        "SELECT search_tool, search_thinking FROM conversation_messages WHERE id=1"
    ).fetchone()
    assert "rg needle" in row[0] and row[1] == "ponder"
    assert db._conversation_fts_is_split(c)
    assert c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='conversation_fts_aux'"
    ).fetchone() is None
    assert c.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_search_split_pending'"
    ).fetchone() is None
    assert c.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_search_split_cursor'"
    ).fetchone() is None
    # the rebuilt index actually matches:
    assert c.execute(
        "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH '{search_thinking}: ponder'"
    ).fetchall() == [(1,)]
    assert c.execute(
        "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH '{search_tool}: needle'"
    ).fetchall() == [(1,)]


def test_consume_search_split_noop_when_flag_absent():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)   # fresh = split, no pending flag
    # consume must be a no-op (no error, no cursor written)
    cache._consume_search_split(c)
    assert c.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_search_split_cursor'"
    ).fetchone() is None


def test_consume_search_split_resumes_from_cursor():
    c = _legacy_conn()
    if not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
    # three rows; pre-set row 1's search_tool + POISON its blocks_json so a
    # re-derivation would overwrite the pre-set value — proving the cursor skipped it.
    _insert_row(c, 1, [{"kind": "tool_use", "name": "Bash",
                        "input": {"command": "ORIGINAL"}}],
                search_tool="PRESET_KEEP")
    c.execute("UPDATE conversation_messages SET blocks_json='not json' WHERE id=1")
    _insert_row(c, 2, [{"kind": "tool_use", "name": "Bash",
                        "input": {"command": "two-cmd"}}])
    _insert_row(c, 3, [{"kind": "thinking", "text": "three-think"}])
    db._set_cache_meta(c, "conversation_search_split_cursor", "1")
    c.commit()
    cache._consume_search_split(c)
    r1 = c.execute("SELECT search_tool FROM conversation_messages WHERE id=1").fetchone()
    assert r1[0] == "PRESET_KEEP", "row 1 must NOT be recomputed (cursor skipped it)"
    r2 = c.execute("SELECT search_tool FROM conversation_messages WHERE id=2").fetchone()
    assert "two-cmd" in r2[0]
    r3 = c.execute("SELECT search_thinking FROM conversation_messages WHERE id=3").fetchone()
    assert r3[0] == "three-think"


def test_consume_search_split_backfill_parity_with_ingest():
    """A row whose search columns are derived by live ingest must byte-equal a
    row whose columns are derived by the backfill from the SAME blocks_json
    (the shared chokepoint). Uses a Bash tool_result carrying stderr to prove
    the post-augment ordering survives the round-trip."""
    import io
    # ingest row A through the real parser
    line = json.dumps({
        "type": "user", "uuid": "A", "sessionId": "s1", "timestamp": "t",
        "toolUseResult": {"stdout": "out\n", "stderr": "stderr-needle",
                          "interrupted": False},
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tb",
             "content": [{"type": "text", "text": "out\nstderr-needle"}],
             "is_error": True}]}}) + "\n"
    rowA = list(lc.iter_message_rows(io.StringIO(line), "f"))[0]

    c = _legacy_conn()
    if not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
    # row B carries A's blocks_json with EMPTY search columns
    c.execute(
        "INSERT INTO conversation_messages"
        "(id,session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
        " blocks_json,is_sidechain,search_tool,search_thinking)"
        " VALUES(1,'s1','B','f',0,'t','tool_result','',?,0,'','')",
        (rowA.blocks_json,))
    db._set_cache_meta(c, "conversation_search_split_pending", "1")
    c.commit()
    cache._consume_search_split(c)
    rowB = c.execute(
        "SELECT search_tool, search_thinking FROM conversation_messages WHERE id=1"
    ).fetchone()
    assert rowB[0] == rowA.search_tool
    assert rowB[1] == rowA.search_thinking
    assert "stderr-needle" in rowB[0]


def test_consume_search_split_fts5_unavailable_backfills_columns_only(monkeypatch):
    """FTS5-unavailable: the base-column backfill still runs (FTS-independent),
    the vtable swap is skipped, fts5_unavailable stays set, and the pending flag
    still clears. A later FTS-capable _apply_cache_schema lands the split shape +
    rebuilds (recovery path)."""
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)   # FTS-unavailable: no vtable, flag set
    assert c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='conversation_fts'"
    ).fetchone() is None
    c.execute(
        "INSERT INTO conversation_messages"
        "(id,session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
        " blocks_json,is_sidechain,search_tool,search_thinking)"
        " VALUES(1,'s1','u1','f',0,'t','assistant','',?,0,'','')",
        (json.dumps([{"kind": "tool_use", "name": "Bash",
                      "input": {"command": "offlinecmd"}}]),))
    db._set_cache_meta(c, "conversation_search_split_pending", "1")
    c.commit()
    cache._consume_search_split(c)
    # columns backfilled; flag cleared; still no vtable; flag fts5_unavailable set.
    assert "offlinecmd" in c.execute(
        "SELECT search_tool FROM conversation_messages WHERE id=1").fetchone()[0]
    assert c.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_search_split_pending'"
    ).fetchone() is None
    assert c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='conversation_fts'"
    ).fetchone() is None
    assert c.execute(
        "SELECT 1 FROM cache_meta WHERE key='fts5_unavailable'"
    ).fetchone() is not None
    # clear the monkeypatch and re-apply: recovery path lands the split shape.
    monkeypatch.setattr(db, "_fts5_available", lambda conn: True)
    db._apply_cache_schema(c)
    assert db._conversation_fts_is_split(c)
    assert c.execute(
        "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH '{search_tool}: offlinecmd'"
    ).fetchall() == [(1,)]


# === 1d: conversation_search_depth ===

def test_conversation_search_depth_full_on_fresh():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    assert db.conversation_search_depth(c) == "full"


def test_conversation_search_depth_prose_only_when_pending():
    c = _legacy_conn()
    assert db.conversation_search_depth(c) == "prose-only"


def test_conversation_search_depth_full_on_operational_error():
    c = sqlite3.connect(":memory:")   # no cache_meta table at all
    assert db.conversation_search_depth(c) == "full"
