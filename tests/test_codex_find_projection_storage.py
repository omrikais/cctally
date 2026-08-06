from __future__ import annotations

import json

from conftest import load_script, redirect_paths


MIGRATION = "004_codex_find_projection"


def _insert_message(
    conn,
    *,
    conversation_key: str,
    offset: int,
    kind: str,
    text: str,
    call_id: str | None = None,
    event_type: str | None = None,
):
    conn.execute(
        "INSERT INTO codex_conversation_messages "
        "(conversation_key,source_root_key,source_path,line_offset,timestamp_utc,"
        "turn_id,call_id,kind,event_type,record_family,model,text,content_digest,"
        "content_len,detail_json,search_tool,search_thinking) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            conversation_key,
            "root",
            f"/{conversation_key}.jsonl",
            offset,
            f"2026-08-04T08:00:{offset:02d}Z",
            f"turn-{conversation_key}",
            call_id,
            kind,
            event_type,
            "event_msg" if kind == "event" else "response_item",
            "gpt-5",
            text,
            f"digest-{conversation_key}-{offset}",
            len(text.encode()),
            json.dumps({}),
            text if kind in {"tool_call", "tool_output", "event"} else None,
            text if kind == "reasoning" else None,
        ),
    )


def _meta(conn, key):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def _insert_event_payload(conn, *, conversation_key: str, offset: int, payload: dict):
    conn.execute(
        "INSERT INTO codex_conversation_events "
        "(conversation_key,source_path,line_offset,source_root_key,record_type,payload_json) "
        "VALUES (?,?,?,?,?,?)",
        (
            conversation_key,
            f"/{conversation_key}.jsonl",
            offset,
            "root",
            "event_msg",
            json.dumps({"payload": payload}),
        ),
    )


def test_conversations_migration_registers_projection_and_fresh_store_is_complete(
    tmp_path, monkeypatch
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    assert MIGRATION in {migration.name for migration in ns["_CONVERSATIONS_MIGRATIONS"]}
    conn = ns["open_conversations_db"]()
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(codex_find_projection)")
        }
        assert columns == {
            "message_id",
            "conversation_key",
            "item_key",
            "block_key",
            "container_block_key",
            "surface",
            "render_order",
            "projected_text",
            "leaves_json",
            "disclosure_json",
            "projection_version",
        }
        assert _meta(conn, "codex_find_projection_complete_version") == "2"
        assert _meta(conn, "codex_find_projection_backfill_pending") is None
    finally:
        conn.close()


def test_materializer_keeps_physical_surfaces_and_cross_leaf_markdown(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_codex_conversation_query as query

    conn = ns["open_conversations_db"]()
    try:
        _insert_message(
            conn,
            conversation_key="conv-a",
            offset=1,
            kind="user",
            text="a**needle**c needle",
        )
        _insert_message(
            conn,
            conversation_key="conv-a",
            offset=2,
            kind="tool_call",
            call_id="call-a",
            text="needle call",
        )
        _insert_message(
            conn,
            conversation_key="conv-a",
            offset=3,
            kind="tool_output",
            call_id="call-a",
            text="needle output needle",
        )
        _insert_message(
            conn,
            conversation_key="conv-a",
            offset=4,
            kind="event",
            call_id="call-a",
            event_type="patch_apply_end",
            text="needle completion",
        )
        query.materialize_codex_find_projection(conn, {"conv-a"})
        rows = conn.execute(
            "SELECT surface,block_key,container_block_key,projected_text,leaves_json "
            "FROM codex_find_projection ORDER BY render_order,message_id"
        ).fetchall()
        assert [row[0] for row in rows] == ["body", "call", "output", "completion"]
        assert rows[0][3] == "aneedlec needle"
        assert json.loads(rows[0][4]) == [
            {"key": "t0", "start": 0, "end": 1},
            {"key": "t1", "start": 1, "end": 7},
            {"key": "t2", "start": 7, "end": 15},
        ]
        assert rows[2][1] != rows[2][2], "output retains physical identity"
        assert rows[1][1] == rows[2][2], "output shares its proven visual owner"
        assert rows[3][1] == rows[3][2], "payload-less completion remains standalone"
    finally:
        conn.close()


def test_materializer_projects_every_visible_meta_body(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_codex_conversation_query as query

    conn = ns["open_conversations_db"]()
    try:
        meta_kinds = ("context", "skill", "command", "compaction", "notification")
        for offset, meta_kind in enumerate(meta_kinds, start=1):
            conversation_key = f"conv-meta-{meta_kind}"
            body = f"{meta_kind} body contains meta-needle-{meta_kind}"
            _insert_message(
                conn,
                conversation_key=conversation_key,
                offset=offset,
                kind="meta",
                text=body,
            )
            conn.execute(
                "UPDATE codex_conversation_messages SET detail_json=? "
                "WHERE conversation_key=?",
                (json.dumps({"meta_kind": meta_kind}), conversation_key),
            )

        query.materialize_codex_find_projection(
            conn, {f"conv-meta-{meta_kind}" for meta_kind in meta_kinds}
        )
        rows = conn.execute(
            "SELECT conversation_key,surface,container_block_key,projected_text,"
            "leaves_json,disclosure_json FROM codex_find_projection "
            "WHERE conversation_key LIKE 'conv-meta-%' ORDER BY conversation_key"
        ).fetchall()

        assert {row[0] for row in rows} == {
            f"conv-meta-{meta_kind}" for meta_kind in meta_kinds
        }
        for conversation_key, surface, container, text, leaves_json, disclosure_json in rows:
            meta_kind = conversation_key.removeprefix("conv-meta-")
            assert surface == "body"
            assert text == f"{meta_kind} body contains meta-needle-{meta_kind}"
            expected_key = "segments.0.prose/t0" if meta_kind == "context" else "t0"
            assert json.loads(leaves_json) == [
                {"key": expected_key, "start": 0, "end": len(text)}
            ]
            assert json.loads(disclosure_json) == [container]
            result = query.find_occurrences_in_codex_conversation(
                conn,
                conversation_key,
                f"meta-needle-{meta_kind}",
                regex=False,
                case_sensitive=True,
                kind="all",
            )
            assert result["status"] == "ready"
            assert result["total"] == 1
            assert result["page"]["occurrences"][0]["disclosure"] == [container]
    finally:
        conn.close()


def test_materializer_projects_native_completion_render_leaves(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_codex_conversation_query as query

    conn = ns["open_conversations_db"]()
    try:
        fixtures = [
            (1, "patch-1", "patch_apply_end", {
                "type": "patch_apply_end", "call_id": "patch-1",
                "status": "completed", "success": True,
                "changes": [{
                    "path": "occurrence.txt", "status": "added",
                    "unified_diff": "--- /dev/null\n+++ b/occurrence.txt\n@@ -0,0 +1 @@\n+needle patch diff\n",
                }],
                "stdout": "\u001b[31mneedle patch stdout\u001b[0m", "stderr": "",
            }),
            (2, "web-1", "web_search_end", {
                "type": "web_search_end", "call_id": "web-1",
                "query": "fixture", "action": {"type": "search", "query": "fixture"},
                "results": [{
                    "title": "Result", "url": "https://example.test/result",
                    "domain": "example.test", "snippet": "needle web snippet",
                    "ref_id": "turn0search0",
                }],
            }),
            (3, "mcp-1", "mcp_tool_call_end", {
                "type": "mcp_tool_call_end", "call_id": "mcp-1",
                "invocation": {"server": "fixture", "tool": "search", "arguments": {"query": "fixture"}},
                "result": {"Ok": {"content": [{"type": "text", "text": "needle MCP result"}]}},
                "duration": {"secs": 0, "nanos": 1},
            }),
        ]
        for offset, call_id, event_type, payload in fixtures:
            _insert_message(
                conn, conversation_key="conv-native", offset=offset,
                kind="event", call_id=call_id, event_type=event_type,
                text=f"legacy {event_type} summary",
            )
            _insert_event_payload(
                conn, conversation_key="conv-native", offset=offset, payload=payload,
            )

        query.materialize_codex_find_projection(conn, {"conv-native"})
        rows = conn.execute(
            "SELECT projected_text,leaves_json FROM codex_find_projection "
            "WHERE conversation_key='conv-native' ORDER BY render_order"
        ).fetchall()

        assert "needle patch diff" in rows[0][0]
        assert "needle patch stdout" in rows[0][0]
        assert "\u001b" not in rows[0][0]
        assert [leaf["key"] for leaf in json.loads(rows[0][1])] == [
            "files.0.path", "files.0.diff.0.0", "stdout",
        ]
        assert "needle web snippet" in rows[1][0]
        assert "results.0.snippet" in {
            leaf["key"] for leaf in json.loads(rows[1][1])
        }
        assert "needle MCP result" in rows[2][0]
        assert "result" in {leaf["key"] for leaf in json.loads(rows[2][1])}
    finally:
        conn.close()


def test_backfill_is_bounded_resumable_and_certifies_only_after_last_conversation(
    tmp_path, monkeypatch
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cache

    conn = ns["open_conversations_db"]()
    try:
        _insert_message(conn, conversation_key="conv-a", offset=1, kind="user", text="alpha")
        _insert_message(conn, conversation_key="conv-b", offset=1, kind="user", text="beta")
        conn.execute("DELETE FROM codex_find_projection")
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES"
            "('codex_find_projection_backfill_pending','1')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES"
            "('codex_find_projection_backfill_cursor','0')"
        )
        conn.execute(
            "DELETE FROM cache_meta WHERE key='codex_find_projection_complete_version'"
        )
        conn.commit()

        first = cache.run_codex_find_projection_backfill(conn, batch_size=1)
        assert first == {"processed": 1, "complete": False}
        assert conn.execute(
            "SELECT COUNT(DISTINCT conversation_key) FROM codex_find_projection"
        ).fetchone()[0] == 1
        assert _meta(conn, "codex_find_projection_backfill_pending") == "1"

        second = cache.run_codex_find_projection_backfill(conn, batch_size=1)
        assert second == {"processed": 1, "complete": True}
        assert conn.execute(
            "SELECT COUNT(DISTINCT conversation_key) FROM codex_find_projection"
        ).fetchone()[0] == 2
        assert _meta(conn, "codex_find_projection_complete_version") == "2"
        assert _meta(conn, "codex_find_projection_backfill_pending") is None
        assert cache.run_codex_find_projection_backfill(conn, batch_size=1) == {
            "processed": 0,
            "complete": True,
        }
    finally:
        conn.close()


def test_backfill_cursor_does_not_skip_an_interleaved_conversation(
    tmp_path, monkeypatch
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cache

    conn = ns["open_conversations_db"]()
    try:
        _insert_message(conn, conversation_key="conv-a", offset=1, kind="user", text="a1")
        _insert_message(conn, conversation_key="conv-b", offset=1, kind="user", text="b1")
        _insert_message(conn, conversation_key="conv-a", offset=2, kind="assistant", text="a2")
        conn.execute("DELETE FROM codex_find_projection")
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES"
            "('codex_find_projection_backfill_pending','1')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES"
            "('codex_find_projection_backfill_cursor','0')"
        )
        conn.execute(
            "DELETE FROM cache_meta WHERE key='codex_find_projection_complete_version'"
        )
        conn.commit()

        first = cache.run_codex_find_projection_backfill(conn, batch_size=1)
        assert first == {"processed": 1, "complete": False}
        assert _meta(conn, "codex_find_projection_backfill_cursor") == "1"

        second = cache.run_codex_find_projection_backfill(conn, batch_size=1)
        assert second == {"processed": 1, "complete": False}
        projected = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT conversation_key FROM codex_find_projection"
            )
        }
        assert projected == {"conv-a", "conv-b"}

        third = cache.run_codex_find_projection_backfill(conn, batch_size=1)
        assert third == {"processed": 1, "complete": True}
        assert _meta(conn, "codex_find_projection_complete_version") == "2"
    finally:
        conn.close()


def test_message_delete_removes_projection_without_foreign_key_enforcement(
    tmp_path, monkeypatch
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_codex_conversation_query as query

    conn = ns["open_conversations_db"]()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        _insert_message(conn, conversation_key="conv-a", offset=1, kind="user", text="alpha")
        query.materialize_codex_find_projection(conn, {"conv-a"})
        assert conn.execute("SELECT COUNT(*) FROM codex_find_projection").fetchone()[0] == 1
        conn.execute("DELETE FROM codex_conversation_messages")
        assert conn.execute("SELECT COUNT(*) FROM codex_find_projection").fetchone()[0] == 0
    finally:
        conn.close()
