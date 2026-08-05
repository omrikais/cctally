import json
import pathlib
import sqlite3
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import _cctally_cache as cache
import _cctally_db as db
import _lib_codex_conversation as codex_kernel
import _lib_codex_conversation_query as codex_query
import _lib_conversation_query as claude_query
from conftest import load_script, redirect_paths


ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32


def _open_scoped_fixture(tmp_path: pathlib.Path, account_key: str) -> sqlite3.Connection:
    cache_path = tmp_path / "cache.db"
    cache_conn = sqlite3.connect(cache_path)
    db._apply_cache_schema(cache_conn)
    cache_conn.executemany(
        "INSERT INTO session_entries "
        "(source_path,line_offset,timestamp_utc,model,msg_id,req_id,"
        " input_tokens,output_tokens,cache_create_tokens,cache_read_tokens,"
        " cost_usd_raw,account_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("shared.jsonl", 10, "2026-08-04T10:00:00Z", "claude-opus-4-8",
             "msg-a", "req-a", 10, 20, 0, 0, 1.25, ACCOUNT_A),
            ("shared.jsonl", 20, "2026-08-04T10:01:00Z", "claude-opus-4-8",
             "msg-b", "req-b", 30, 40, 0, 0, 9.75, ACCOUNT_B),
        ],
    )
    cache_conn.execute(
        "INSERT INTO session_files "
        "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at,"
        " session_id,project_path,account_key) VALUES(?,?,?,?,?,?,?,?)",
        ("shared.jsonl", 20, 1, 20, "2026-08-04T10:01:00Z",
         "shared-session", "/Users/bravo/private-project", ACCOUNT_B),
    )
    cache_conn.commit()
    cache_conn.close()

    conn = sqlite3.connect(":memory:")
    db._apply_conversations_schema(conn)
    conn.executemany(
        "INSERT INTO conversation_messages "
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
        " blocks_json,model,msg_id,req_id,cwd,is_sidechain,account_key) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("shared-session", "uuid-a", "shared.jsonl", 10,
             "2026-08-04T10:00:00Z", "assistant", "alpha private message",
             "[]", "claude-opus-4-8", "msg-a", "req-a", "/work/alpha", 0,
             ACCOUNT_A),
            ("shared-session", "uuid-b", "shared.jsonl", 20,
             "2026-08-04T10:01:00Z", "assistant", "bravo private message",
             "[]", "claude-opus-4-8", "msg-b", "req-b", "/work/bravo", 0,
             ACCOUNT_B),
        ],
    )
    conn.execute("ATTACH DATABASE ? AS cache_db", (str(cache_path),))
    cache._recompute_conversation_sessions(conn)
    conn.commit()
    cache.scope_conversations_db_to_account(conn, account_key)
    return conn


def test_account_scope_filters_every_claude_leaf_in_shared_conversation(tmp_path):
    conn = _open_scoped_fixture(tmp_path, ACCOUNT_A)
    try:
        browse = claude_query.list_conversations(conn)
        assert [row["session_id"] for row in browse["conversations"]] == [
            "shared-session"
        ]
        assert browse["conversations"][0]["msg_count"] == 1
        assert browse["conversations"][0]["cost_usd"] == 1.25
        assert browse["conversations"][0]["project_label"] == "alpha"

        detail = claude_query.get_conversation(conn, "shared-session")
        assert detail is not None
        rendered = str(detail)
        assert "alpha private message" in rendered
        assert "bravo private message" not in rendered
        assert detail["cost_usd"] == 1.25

        outline = claude_query.get_conversation_outline(conn, "shared-session")
        assert outline is not None
        assert "alpha private message" in str(outline)
        assert "bravo private message" not in str(outline)

        exported = claude_query.get_conversation_export(
            conn, "shared-session", "all"
        )
        assert "alpha private message" in exported
        assert "bravo private message" not in exported

        search_a = claude_query.search_conversations(
            conn, "alpha private", fts_available=False
        )
        search_b = claude_query.search_conversations(
            conn, "bravo private", fts_available=False
        )
        assert search_a["total"] == 1
        assert search_b["total"] == 0

        find_b = claude_query.find_in_conversation(
            conn, "shared-session", "bravo private", fts_available=False
        )
        assert find_b is not None
        assert find_b["total"] == 0

        from _lib_conversation_anon import plan_to_wire
        anon_wire = json.dumps(plan_to_wire(
            claude_query.build_anon_plan_for_db(
                conn, home_dir="/Users/alpha"
            )
        ))
        assert "/Users/bravo/private-project" not in anon_wire
        assert "/work/bravo" not in anon_wire
    finally:
        conn.close()


def test_account_b_scope_excludes_account_a_from_same_session(tmp_path):
    conn = _open_scoped_fixture(tmp_path, ACCOUNT_B)
    try:
        detail = claude_query.get_conversation(conn, "shared-session")
        assert detail is not None
        rendered = str(detail)
        assert "bravo private message" in rendered
        assert "alpha private message" not in rendered
        assert detail["cost_usd"] == 9.75
    finally:
        conn.close()


def _open_scoped_codex_fixture(
    tmp_path: pathlib.Path, account_key: str,
) -> sqlite3.Connection:
    cache_path = tmp_path / "cache-codex.db"
    cache_conn = sqlite3.connect(cache_path)
    db._apply_cache_schema(cache_conn)
    cache_conn.executemany(
        "INSERT INTO codex_session_entries "
        "(source_path,line_offset,timestamp_utc,session_id,model,input_tokens,"
        " cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,"
        " account_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("shared-codex.jsonl", 10, "2026-08-04T10:00:00Z", "shared-thread",
             "gpt-5.6", 100, 0, 20, 0, 120, ACCOUNT_A),
            ("shared-codex.jsonl", 20, "2026-08-04T10:01:00Z", "shared-thread",
             "gpt-5.6", 300, 0, 40, 0, 340, ACCOUNT_B),
        ],
    )
    cache_conn.commit()
    cache_conn.close()

    conn = sqlite3.connect(":memory:")
    db._apply_conversations_schema(conn)
    conn.execute(
        "INSERT INTO cache_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            "codex_conversation_contract_version",
            codex_kernel.CODEX_CONVERSATION_CONTRACT_VERSION,
        ),
    )
    conn.executemany(
        "INSERT INTO codex_conversation_messages "
        "(conversation_key,source_root_key,source_path,line_offset,timestamp_utc,"
        " turn_id,call_id,kind,event_type,record_family,model,text,content_digest,"
        " content_len,detail_json,search_tool,search_thinking,account_key) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("v1.shared", "root", "shared-codex.jsonl", 10,
             "2026-08-04T10:00:00Z", "turn-a", None, "user", None,
             "event_msg", "gpt-5.6", "alpha codex private", "a" * 64,
             len("alpha codex private"), None, None, None, ACCOUNT_A),
            ("v1.shared", "root", "shared-codex.jsonl", 20,
             "2026-08-04T10:01:00Z", "turn-b", None, "user", None,
             "event_msg", "gpt-5.6", "bravo codex private", "b" * 64,
             len("bravo codex private"), None, None, None, ACCOUNT_B),
        ],
    )
    conn.execute("ATTACH DATABASE ? AS cache_db", (str(cache_path),))
    cache._recompute_codex_rollups(conn, {"v1.shared"})
    conn.commit()
    cache.scope_conversations_db_to_account(conn, account_key)
    return conn


def test_account_scope_filters_every_codex_leaf_in_shared_conversation(tmp_path):
    conn = _open_scoped_codex_fixture(tmp_path, ACCOUNT_A)
    try:
        browse = codex_query.list_codex_conversations(
            conn, effective_speed="standard"
        )
        assert browse["status"] == "ok"
        assert len(browse["rows"]) == 1
        assert browse["rows"][0]["count"] == 1

        detail = codex_query.get_codex_conversation(
            conn, "v1.shared", effective_speed="standard"
        )
        assert detail["status"] == "ok"
        assert "alpha codex private" in str(detail)
        assert "bravo codex private" not in str(detail)

        search_a = codex_query.search_codex_conversations(
            conn, "alpha codex", effective_speed="standard"
        )
        search_b = codex_query.search_codex_conversations(
            conn, "bravo codex", effective_speed="standard"
        )
        assert search_a["total"] == 1
        assert search_b["total"] == 0

        exported = codex_query.get_codex_conversation_export(
            conn, "v1.shared", effective_speed="standard"
        )
        assert exported["status"] == "ok"
        assert "alpha codex private" in exported["markdown"]
        assert "bravo codex private" not in exported["markdown"]
    finally:
        conn.close()


def _claude_user_line(uuid: str, text: str, timestamp: str) -> str:
    return json.dumps({
        "type": "user",
        "uuid": uuid,
        "sessionId": "switching-session",
        "timestamp": timestamp,
        "message": {"role": "user", "content": text},
    }) + "\n"


def test_claude_delta_ingest_stamps_once_and_rebuild_preserves_accounts(
        tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core as core

    project = tmp_path / "data" / ".claude" / "projects" / "-switching"
    project.mkdir(parents=True)
    jsonl = project / "switching.jsonl"
    jsonl.write_text(_claude_user_line(
        "u-a", "alpha forward", "2026-08-04T10:00:00Z"
    ))

    active = {"key": ACCOUNT_A, "status": "identified"}
    monkeypatch.setattr(
        core,
        "_resolve_active_claude_identity",
        lambda: {"account_key": active["key"], "status": active["status"]},
    )

    conn = ns["open_conversations_db"]()
    try:
        cache.sync_claude_conversations(conn)
        active["key"] = ACCOUNT_B
        with jsonl.open("a") as fh:
            fh.write(_claude_user_line(
                "u-b", "bravo forward", "2026-08-04T10:01:00Z"
            ))
        # A pending enrichment pass parses the whole file before the ordinary
        # delta walk. Existing offsets must retain A while the newly observed
        # tail takes the already-stable B identity (never NULL).
        conn.execute(
            "INSERT INTO cache_meta(key,value) VALUES"
            "('conversation_reingest_enrichment_pending','1')"
        )
        conn.commit()
        cache.sync_claude_conversations(conn)
        assert conn.execute(
            "SELECT text,account_key FROM conversation_messages "
            "ORDER BY byte_offset"
        ).fetchall() == [
            ("alpha forward", ACCOUNT_A),
            ("bravo forward", ACCOUNT_B),
        ]

        # A retained-store rebuild under a different active identity restores
        # the immutable physical decisions instead of re-stamping history.
        cache.sync_claude_conversations(conn, rebuild=True)
        assert conn.execute(
            "SELECT text,account_key FROM conversation_messages "
            "ORDER BY byte_offset"
        ).fetchall() == [
            ("alpha forward", ACCOUNT_A),
            ("bravo forward", ACCOUNT_B),
        ]
    finally:
        conn.close()


def test_codex_conversation_replay_uses_durable_file_account_map(
        tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data-codex")
    import _cctally_cache as cache_mod

    provider = tmp_path / "provider"
    rollout = provider / "sessions" / "2026" / "08" / "04" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    corpus = pathlib.Path(__file__).parent / "fixtures" / "codex-parity" / "v1" / "rollouts" / "modern-full.jsonl"
    rollout.write_bytes(corpus.read_bytes())
    monkeypatch.setenv("CODEX_HOME", str(provider))

    active = {"key": ACCOUNT_A}
    monkeypatch.setattr(
        cache_mod,
        "_resolve_codex_account_for_root",
        lambda _root: cache_mod._CodexRootAccount(
            "identified",
            active["key"],
            {"account_key": active["key"], "natural_id": active["key"]},
        ),
    )

    accounting = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](accounting)
    finally:
        accounting.close()

    conn = ns["open_conversations_db"]()
    try:
        cache_mod.sync_codex_conversations(conn)
        assert conn.execute(
            "SELECT DISTINCT account_key FROM codex_conversation_events"
        ).fetchall() == [(ACCOUNT_A,)]
        assert conn.execute(
            "SELECT DISTINCT account_key FROM codex_conversation_messages"
        ).fetchall() == [(ACCOUNT_A,)]

        active["key"] = ACCOUNT_B
        with rollout.open("a") as fh:
            fh.write(json.dumps({
                "type": "event_msg",
                "timestamp": "2026-08-04T10:20:00Z",
                "payload": {
                    "type": "user_message",
                    "message": "bravo codex tail",
                    "text_elements": [{"text": "bravo codex tail"}],
                    "images": [],
                    "local_images": [],
                },
            }) + "\n")
        # This is the account-scoped live-tail order: the accounting cursor and
        # durable file-range decision advance before transcript bytes consume
        # that range. Reversing these two calls stamps the new B tail as A.
        accounting = ns["open_cache_db"]()
        try:
            ns["sync_codex_cache"](accounting, only_paths={str(rollout)})
        finally:
            accounting.close()
        cache_mod.sync_codex_conversations(conn, only_paths={str(rollout)})
        assert conn.execute(
            "SELECT account_key FROM codex_conversation_messages "
            "WHERE text='bravo codex tail'"
        ).fetchall() == [(ACCOUNT_B,)]

        cache_mod.sync_codex_conversations(conn, rebuild=True)
        assert conn.execute(
            "SELECT DISTINCT account_key FROM codex_conversation_events "
            "ORDER BY account_key"
        ).fetchall() == [(ACCOUNT_A,), (ACCOUNT_B,)]
        assert conn.execute(
            "SELECT DISTINCT account_key FROM codex_conversation_messages "
            "ORDER BY account_key"
        ).fetchall() == [(ACCOUNT_A,), (ACCOUNT_B,)]
    finally:
        conn.close()
