from __future__ import annotations

import json
import pathlib
import sys
import tracemalloc

import pytest

from conftest import load_script, redirect_paths


BIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_codex_conversation_query as query  # noqa: E402


def _store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns["open_conversations_db"]()


def _projection(
    conn,
    *,
    message_id: int,
    order: int,
    text: str,
    leaves: list[dict[str, object]],
    surface: str = "body",
    kind: str = "assistant",
    block: str | None = None,
    item: str | None = None,
):
    block = block or f"block-{message_id}"
    item = item or f"item-{message_id}"
    conn.execute(
        "INSERT INTO codex_conversation_messages "
        "(id,conversation_key,source_root_key,source_path,line_offset,timestamp_utc,"
        "turn_id,call_id,kind,event_type,record_family,model,text,content_digest,"
        "content_len,detail_json,search_tool,search_thinking) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            message_id, "conv", "root", "/conv.jsonl", message_id,
            f"2026-08-04T08:00:{message_id % 60:02d}Z", f"turn-{message_id}",
            None, kind, None, "response_item", "gpt-5", text,
            f"digest-{message_id}", len(text), "{}", text, text,
        ),
    )
    conn.execute(
        "INSERT INTO codex_find_projection "
        "(message_id,conversation_key,item_key,block_key,container_block_key,"
        "surface,render_order,projected_text,leaves_json,disclosure_json,"
        "projection_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            message_id, "conv", item, block, f"container-{block}", surface,
            order, text, json.dumps(leaves),
            json.dumps([f"details-{block}"] if surface != "body" else []),
            query.CODEX_FIND_PROJECTION_VERSION,
        ),
    )


def test_exact_occurrences_and_cross_leaf_fragments(tmp_path, monkeypatch):
    conn = _store(tmp_path, monkeypatch)
    try:
        _projection(
            conn,
            message_id=1,
            order=0,
            text="needle needle abc",
            leaves=[
                {"key": "t0", "start": 0, "end": 14},
                {"key": "t1", "start": 14, "end": 15},
                {"key": "t2", "start": 15, "end": 16},
                {"key": "t3", "start": 16, "end": 17},
            ],
        )
        result = query.find_occurrences_in_codex_conversation(
            conn, "conv", "needle", regex=False, case_sensitive=False, kind="all"
        )
        assert result["schema_version"] == 2
        assert result["semantics"] == "occurrence"
        assert result["status"] == "ready"
        assert result["total"] == 2
        occurrences = result["page"]["occurrences"]
        assert len({o["occurrence_id"] for o in occurrences}) == 2
        assert occurrences[0]["fragments"] == [
            {"leaf_key": "t0", "start": 0, "end": 6}
        ]

        spanning = query.find_occurrences_in_codex_conversation(
            conn, "conv", "abc", regex=False, case_sensitive=True, kind="all"
        )
        assert spanning["page"]["occurrences"][0]["fragments"] == [
            {"leaf_key": "t1", "start": 0, "end": 1},
            {"leaf_key": "t2", "start": 0, "end": 1},
            {"leaf_key": "t3", "start": 0, "end": 1},
        ]
    finally:
        conn.close()


def test_pages_all_occurrences_without_duplicates(tmp_path, monkeypatch):
    conn = _store(tmp_path, monkeypatch)
    try:
        for index in range(205):
            _projection(
                conn,
                message_id=index + 1,
                order=index,
                text="hit",
                leaves=[{"key": "t0", "start": 0, "end": 3}],
            )
        first = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all"
        )
        assert first["total"] == 205
        assert len(first["page"]["occurrences"]) == 100
        assert first["page"]["previous_cursor"] is None
        assert first["page"]["next_cursor"].startswith("ofc1.")
        second = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            cursor=first["page"]["next_cursor"],
        )
        third = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            cursor=second["page"]["next_cursor"],
        )
        ids = [
            occurrence["occurrence_id"]
            for page in (first, second, third)
            for occurrence in page["page"]["occurrences"]
        ]
        assert len(ids) == len(set(ids)) == 205
        assert third["page"]["start_index"] == 200
        assert third["page"]["next_cursor"] is None

        maximum = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=200,
        )
        assert len(maximum["page"]["occurrences"]) == 200
        wrapped_last = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=200, direction="previous",
        )
        assert wrapped_last["page"]["start_index"] == 5
        assert len(wrapped_last["page"]["occurrences"]) == 200
        tail = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=200, cursor=maximum["page"]["next_cursor"],
        )
        backward = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=200, cursor=tail["page"]["previous_cursor"],
            direction="previous",
        )
        assert backward["page"]["start_index"] == 0
        assert [o["occurrence_id"] for o in backward["page"]["occurrences"]] == [
            o["occurrence_id"] for o in maximum["page"]["occurrences"]
        ]

        last_100 = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=100, direction="previous",
        )
        middle_100 = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=100, cursor=last_100["page"]["previous_cursor"],
            direction="previous",
        )
        head_5 = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=100, cursor=middle_100["page"]["previous_cursor"],
            direction="previous",
        )
        assert head_5["page"]["start_index"] == 0
        assert len(head_5["page"]["occurrences"]) == 5
        assert [
            occurrence["occurrence_id"]
            for page in (head_5, middle_100, last_100)
            for occurrence in page["page"]["occurrences"]
        ] == ids
    finally:
        conn.close()


def test_high_occurrence_page_keeps_server_memory_bounded(tmp_path, monkeypatch):
    conn = _store(tmp_path, monkeypatch)
    try:
        text = "a" * 25_000
        _projection(
            conn, message_id=1, order=0, text=text,
            leaves=[{"key": "t0", "start": 0, "end": len(text)}],
        )
        tracemalloc.start()
        result = query.find_occurrences_in_codex_conversation(
            conn, "conv", "a", regex=False, case_sensitive=True, kind="all",
            limit=100,
        )
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert result["total"] == 25_000
        assert len(result["page"]["occurrences"]) == 100
        assert peak < 12_000_000
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        conn.close()


def test_cursor_staleness_and_around_append_reconciliation(tmp_path, monkeypatch):
    conn = _store(tmp_path, monkeypatch)
    try:
        for index in range(3):
            _projection(
                conn, message_id=index + 1, order=index, text="hit",
                leaves=[{"key": "t0", "start": 0, "end": 3}],
            )
        first = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=1,
        )
        selected = first["page"]["occurrences"][0]["occurrence_id"]
        cursor = first["page"]["next_cursor"]
        conn.execute(
            "UPDATE cache_meta SET value=CAST(value AS INTEGER)+1 "
            "WHERE key='codex_find_projection_generation'"
        )
        _projection(
            conn, message_id=4, order=3, text="hit",
            leaves=[{"key": "t0", "start": 0, "end": 3}],
        )
        with pytest.raises(query.StaleFindCursor):
            query.find_occurrences_in_codex_conversation(
                conn, "conv", "hit", regex=False, case_sensitive=False,
                kind="all", limit=1, cursor=cursor,
            )
        reconciled = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=1, around=selected,
        )
        assert reconciled["selection_stale"] is False
        assert reconciled["page"]["occurrences"][0]["occurrence_id"] == selected

        conn.execute("DELETE FROM codex_find_projection WHERE message_id=1")
        stale = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=1, around=selected,
        )
        assert stale["selection_stale"] is True
        assert stale["page"]["start_index"] == 0
    finally:
        conn.close()


def test_regex_case_kind_surfaces_and_indexing(tmp_path, monkeypatch):
    conn = _store(tmp_path, monkeypatch)
    try:
        _projection(
            conn, message_id=1, order=0, text="Äbc abc", kind="reasoning",
            leaves=[{"key": "t0", "start": 0, "end": 7}],
        )
        _projection(
            conn, message_id=2, order=1, text="abc", kind="tool_output",
            surface="output", leaves=[{"key": "t0", "start": 0, "end": 3}],
        )
        insensitive = query.find_occurrences_in_codex_conversation(
            conn, "conv", "äBC", regex=False, case_sensitive=False, kind="all"
        )
        assert insensitive["total"] == 1
        thinking = query.find_occurrences_in_codex_conversation(
            conn, "conv", "abc", regex=True, case_sensitive=True, kind="thinking"
        )
        assert thinking["total"] == 1
        assert thinking["page"]["occurrences"][0]["match_kinds"] == ["thinking"]
        tools = query.find_occurrences_in_codex_conversation(
            conn, "conv", "(?=a)|abc", regex=True, case_sensitive=True, kind="tools"
        )
        assert tools["total"] == 1, "zero-width regex matches are omitted"
        assert tools["page"]["occurrences"][0]["surface"] == "output"
        assert tools["page"]["occurrences"][0]["disclosure"] == ["details-block-2"]

        conn.execute(
            "DELETE FROM cache_meta WHERE key='codex_find_projection_complete_version'"
        )
        indexing = query.find_occurrences_in_codex_conversation(
            conn, "conv", "abc", regex=False, case_sensitive=False, kind="all"
        )
        assert indexing == {
            "schema_version": 2,
            "semantics": "occurrence",
            "status": "indexing",
            "query_id": indexing["query_id"],
            "selection_stale": False,
            "mode": "literal",
            "kind": "all",
            "search_depth": "full",
            "page": {
                "start_index": 0,
                "previous_cursor": None,
                "next_cursor": None,
                "occurrences": [],
            },
        }
        assert "total" not in indexing
    finally:
        conn.close()


def test_malformed_and_query_mismatched_cursors(tmp_path, monkeypatch):
    conn = _store(tmp_path, monkeypatch)
    try:
        _projection(
            conn, message_id=1, order=0, text="hit hit",
            leaves=[{"key": "t0", "start": 0, "end": 7}],
        )
        with pytest.raises(query.InvalidFindCursor):
            query.find_occurrences_in_codex_conversation(
                conn, "conv", "hit", regex=False, case_sensitive=False,
                kind="all", cursor="not-a-cursor",
            )
        first = query.find_occurrences_in_codex_conversation(
            conn, "conv", "hit", regex=False, case_sensitive=False, kind="all",
            limit=1,
        )
        with pytest.raises(query.StaleFindCursor):
            query.find_occurrences_in_codex_conversation(
                conn, "conv", "other", regex=False, case_sensitive=False,
                kind="all", limit=1, cursor=first["page"]["next_cursor"],
            )
        cursor = first["page"]["next_cursor"]
        with pytest.raises(query.InvalidFindCursor):
            query.find_occurrences_in_codex_conversation(
                conn, "conv", "hit", regex=False, case_sensitive=False,
                kind="all", limit=1, cursor=f"{cursor}!!!!",
            )

        decoded = query._decode_exact_find_cursor(cursor)
        decoded["i"] = True
        noncanonical = query._CODEX_EXACT_FIND_CURSOR_PREFIX + __import__(
            "base64"
        ).urlsafe_b64encode(json.dumps(
            decoded, sort_keys=True, separators=(",", ":")
        ).encode()).decode().rstrip("=")
        with pytest.raises(query.InvalidFindCursor):
            query.find_occurrences_in_codex_conversation(
                conn, "conv", "hit", regex=False, case_sensitive=False,
                kind="all", limit=1, cursor=noncanonical,
            )
    finally:
        conn.close()
