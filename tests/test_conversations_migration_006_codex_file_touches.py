"""Issue #489: historical dict-shaped Codex patches regain file touches."""
from __future__ import annotations

import json
import sqlite3

import _cctally_core
import _cctally_db


MIGRATION = "006_backfill_codex_file_touches"


def _handler():
    migrations = {
        migration.name: migration.handler
        for migration in _cctally_db._CONVERSATIONS_MIGRATIONS
    }
    assert MIGRATION in migrations, "the historical file-touch backfill is missing"
    return migrations[MIGRATION]


def test_backfill_reads_retained_dict_payloads_and_is_idempotent(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        _cctally_core, "CONVERSATIONS_LOCK_CODEX_PATH",
        tmp_path / "conversations.db.codex.lock")
    conn = sqlite3.connect(":memory:")
    try:
        _cctally_db._apply_conversations_schema(conn)
        payload_json = json.dumps({"payload": {
            "type": "patch_apply_end",
            "changes": {
                "src/legacy-alpha.py": {"type": "update"},
                "src/legacy-beta.py": {"type": "delete", "content": "old\n"},
            },
        }})
        conn.execute(
            "INSERT INTO codex_conversation_events "
            "(source_path,line_offset,source_root_key,conversation_key,event_type,payload_json) "
            "VALUES(?,?,?,?,?,?)",
            ("/rollout.jsonl", 42, "root-a", "conversation-a",
             "patch_apply_end", payload_json),
        )
        conn.execute(
            "INSERT INTO codex_conversation_messages "
            "(conversation_key,source_root_key,source_path,line_offset,kind,event_type,"
            "record_family,content_digest,content_len) VALUES(?,?,?,?,?,?,?,?,?)",
            ("conversation-a", "root-a", "/rollout.jsonl", 42, "event",
             "patch_apply_end", "event_msg", "digest", 0),
        )
        conn.commit()

        handler = _handler()
        handler(conn)
        first = conn.execute(
            "SELECT t.conversation_key,t.source_path,t.file_path,t.tool,m.line_offset "
            "FROM codex_conversation_file_touches t "
            "JOIN codex_conversation_messages m ON m.id=t.message_id "
            "ORDER BY t.file_path"
        ).fetchall()
        handler(conn)
        second = conn.execute(
            "SELECT t.conversation_key,t.source_path,t.file_path,t.tool,m.line_offset "
            "FROM codex_conversation_file_touches t "
            "JOIN codex_conversation_messages m ON m.id=t.message_id "
            "ORDER BY t.file_path"
        ).fetchall()

        assert first == second == [
            ("conversation-a", "/rollout.jsonl", "src/legacy-alpha.py",
             "apply_patch", 42),
            ("conversation-a", "/rollout.jsonl", "src/legacy-beta.py",
             "apply_patch", 42),
        ]
    finally:
        conn.close()
