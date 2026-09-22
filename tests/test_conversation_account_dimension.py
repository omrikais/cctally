import json
import pathlib
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest


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


def test_claude_assembly_memo_never_crosses_account_scopes(tmp_path):
    """Equal scoped rollup watermarks must not reuse another account's body."""
    alpha_dir = tmp_path / "alpha"
    bravo_dir = tmp_path / "bravo"
    alpha_dir.mkdir()
    bravo_dir.mkdir()
    alpha = _open_scoped_fixture(alpha_dir, ACCOUNT_A)
    bravo = _open_scoped_fixture(bravo_dir, ACCOUNT_B)
    try:
        # Make every pre-#682 memo-key field identical across the two scoped
        # stores. Only store/account identity may distinguish the bodies.
        for conn in (alpha, bravo):
            conn.execute(
                "UPDATE conversation_sessions "
                "SET last_activity_utc='2026-08-04T10:00:00Z'"
            )
            conn.commit()
        claude_query._assemble_memo_clear()
        first = claude_query.get_conversation(alpha, "shared-session")
        second = claude_query.get_conversation(bravo, "shared-session")
        assert "alpha private message" in str(first)
        assert "bravo private message" in str(second)
        assert "alpha private message" not in str(second)
    finally:
        claude_query._assemble_memo_clear()
        alpha.close()
        bravo.close()


def _open_scoped_codex_fixture(
    tmp_path: pathlib.Path, account_key: str, *, drop_persisted_rollup: bool = False,
    scope: bool = True,
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
    cache_conn.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key,source_root_key,native_thread_id,root_thread_id,"
        " source_path,cwd) VALUES(?,?,?,?,?,?)",
        ("v1.shared", "root", "shared-thread", "shared-thread",
         "shared-codex.jsonl", "/work/cctally-dev"),
    )
    cache_conn.commit()
    cache_conn.close()

    conn = sqlite3.connect(":memory:")
    db._apply_conversations_schema(conn)
    db._ensure_codex_session_meta_provenance_index(conn)
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
    if drop_persisted_rollup:
        conn.execute("DELETE FROM codex_conversation_rollups")
    conn.commit()
    if scope:
        cache.scope_conversations_db_to_account(conn, account_key)
    return conn


def test_account_scope_does_not_advance_persistent_render_revision(tmp_path):
    """A read scope may build TEMP rollups but must not mutate durable state."""
    conn = _open_scoped_codex_fixture(tmp_path, ACCOUNT_A, scope=False)
    try:
        before = conn.execute(
            "SELECT value FROM main.cache_meta "
            "WHERE key='conversation_render_revision'"
        ).fetchone()
        cache.scope_conversations_db_to_account(conn, ACCOUNT_A)
        after = conn.execute(
            "SELECT value FROM main.cache_meta "
            "WHERE key='conversation_render_revision'"
        ).fetchone()
        assert after == before
    finally:
        conn.close()


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


@pytest.mark.parametrize(
    ("account_key", "own_text", "other_text"),
    [
        (ACCOUNT_A, "alpha codex private", "bravo codex private"),
        (ACCOUNT_B, "bravo codex private", "alpha codex private"),
    ],
)
def test_account_scope_preserves_safe_codex_project_attribution(
    tmp_path, account_key, own_text, other_text,
):
    fixture_dir = tmp_path / account_key
    fixture_dir.mkdir()
    conn = _open_scoped_codex_fixture(fixture_dir, account_key)
    try:
        unscoped_project = conn.execute(
            "SELECT project_key,project_label "
            "FROM main.codex_conversation_rollups "
            "WHERE conversation_key='v1.shared'"
        ).fetchone()
        scoped_project = conn.execute(
            "SELECT project_key,project_label "
            "FROM codex_conversation_rollups "
            "WHERE conversation_key='v1.shared'"
        ).fetchone()

        assert unscoped_project[1] == "cctally-dev"
        assert scoped_project == unscoped_project

        detail = codex_query.get_codex_conversation(
            conn, "v1.shared", effective_speed="standard"
        )
        assert own_text in str(detail)
        assert other_text not in str(detail)

        # Project identity is safe derived enrichment. The underlying
        # conversation-level cwd/git and source-root metadata stay hidden from
        # every account-scoped query kernel.
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_threads"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_source_roots"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_scoped_codex_anon_uses_only_physically_owned_project_paths(tmp_path):
    """A surviving B message cannot make an A session_meta CWD B's token."""
    from _lib_conversation_anon import plan_to_wire, scrub_text

    conn = _open_scoped_codex_fixture(tmp_path, ACCOUNT_A, scope=False)
    alice_path = "/Users/alice/account-a-project"
    bravo_path = "/Users/bravo/account-b-project"
    try:
        conn.execute(
            "UPDATE cache_db.codex_conversation_threads SET cwd=? "
            "WHERE conversation_key='v1.shared'", (alice_path,),
        )
        conn.execute(
            "INSERT INTO cache_db.codex_conversation_threads "
            "(conversation_key,source_root_key,native_thread_id,root_thread_id,"
            "source_path,cwd) VALUES(?,?,?,?,?,?)",
            ("v1.bravo", "root", "bravo-thread", "bravo-thread",
             "bravo-codex.jsonl", bravo_path),
        )
        for key, path, account, source in (
            ("v1.shared", alice_path, ACCOUNT_A, "shared-codex.jsonl"),
            ("v1.bravo", bravo_path, ACCOUNT_B, "bravo-codex.jsonl"),
        ):
            conn.execute(
                "INSERT INTO main.codex_conversation_events "
                "(source_path,line_offset,source_root_key,conversation_key,"
                "native_thread_id,root_thread_id,record_type,payload_json,account_key) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (source, 0, "root", key, key, key, "session_meta",
                 json.dumps({"type": "session_meta", "payload": {"cwd": path}}),
                 account),
            )
        conn.commit()
        cache.scope_conversations_db_to_account(conn, ACCOUNT_A)
        result = claude_query.build_anon_plan_for_sources(
            conn, home_dir="/Users/alice", sources={"codex"},
        )
        wire = json.dumps(plan_to_wire(result.plan))
        assert alice_path in wire
        assert bravo_path not in wire
        assert "account-b-project" not in wire
        assert alice_path not in scrub_text(alice_path, result.plan)
    finally:
        conn.close()


def test_scoped_codex_anon_refuses_thread_path_from_other_account(tmp_path):
    """The B leaf survives, but its thread CWD came from A's session_meta."""
    conn = _open_scoped_codex_fixture(tmp_path, ACCOUNT_B, scope=False)
    alice_path = "/Users/alice/account-a-project"
    try:
        conn.execute(
            "UPDATE cache_db.codex_conversation_threads SET cwd=? "
            "WHERE conversation_key='v1.shared'", (alice_path,),
        )
        conn.execute(
            "INSERT INTO main.codex_conversation_events "
            "(source_path,line_offset,source_root_key,conversation_key,"
            "native_thread_id,root_thread_id,record_type,payload_json,account_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("shared-codex.jsonl", 0, "root", "v1.shared", "shared-thread",
             "shared-thread", "session_meta",
             json.dumps({"type": "session_meta", "payload": {"cwd": alice_path}}),
             ACCOUNT_A),
        )
        conn.commit()
        cache.scope_conversations_db_to_account(conn, ACCOUNT_B)
        result = claude_query.build_anon_plan_for_sources(
            conn, home_dir="/Users/bravo", sources={"codex"},
        )
        assert result.undecodable_cwd_rows == 0
        assert result.ambiguous_cwd_rows == 1
    finally:
        conn.close()


def test_scoped_codex_anon_refuses_root_path_owned_as_foreign_cwd(tmp_path):
    """A's provider-root name must not re-admit B's known project path."""
    from _lib_conversation_anon import plan_to_wire

    conn = _open_scoped_codex_fixture(tmp_path, ACCOUNT_A, scope=False)
    owned_path = "/Users/alice/own-project"
    foreign_path = "/Users/bravo/foreign-project"
    try:
        conn.execute(
            "UPDATE cache_db.codex_conversation_threads SET cwd=? "
            "WHERE conversation_key='v1.shared'", (owned_path,),
        )
        conn.execute(
            "INSERT INTO cache_db.codex_source_roots "
            "(source_root_key,canonical_root_path,first_seen_utc,last_seen_utc) "
            "VALUES('root',?,'2026-08-04T00:00:00Z','2026-08-04T00:00:00Z')",
            (foreign_path,),
        )
        for source, root, key, path, account in (
            ("owned.jsonl", "root", "v1.shared", owned_path, ACCOUNT_A),
            ("foreign.jsonl", "foreign-root", "v1.foreign", foreign_path,
             ACCOUNT_B),
        ):
            conn.execute(
                "INSERT INTO main.codex_conversation_events "
                "(source_path,line_offset,source_root_key,conversation_key,"
                "native_thread_id,root_thread_id,record_type,payload_json,account_key) "
                "VALUES(?,0,?,?,?,?,'session_meta',?,?)",
                (source, root, key, key, key,
                 json.dumps({"type": "session_meta", "payload": {"cwd": path}}),
                 account),
            )
        conn.commit()
        cache.scope_conversations_db_to_account(conn, ACCOUNT_A)
        result = claude_query.build_anon_plan_for_sources(
            conn, home_dir="/Users/operator", sources={"codex"},
        )
        assert result.ambiguous_cwd_rows > 0
        assert foreign_path not in json.dumps(plan_to_wire(result.plan))
    finally:
        conn.close()


def test_scoped_codex_anon_refuses_failed_root_path_read(tmp_path):
    """An unreadable root must abort the plan before any token map is served."""
    conn = _open_scoped_codex_fixture(tmp_path, ACCOUNT_A, scope=False)
    try:
        conn.execute(
            "INSERT INTO cache_db.codex_source_roots "
            "(source_root_key,canonical_root_path,first_seen_utc,last_seen_utc) "
            "VALUES('root',CAST(x'ff' AS TEXT),"
            "'2026-08-04T00:00:00Z','2026-08-04T00:00:00Z')"
        )
        conn.commit()
        cache.scope_conversations_db_to_account(conn, ACCOUNT_A)
        with pytest.raises(sqlite3.OperationalError):
            claude_query.build_anon_plan_for_sources(
                conn, home_dir="/Users/operator", sources={"codex"},
            )
    finally:
        conn.close()


def test_scoped_codex_anon_handles_store_without_raw_threads_table():
    """A fresh conversations store has no Codex cache table to resolve yet."""
    conn = sqlite3.connect(":memory:")
    try:
        db._apply_conversations_schema(conn)
        assert claude_query._scoped_codex_anon_paths(conn, ACCOUNT_A) == (
            set(), 0, 0,
        )
        conn.execute(
            "INSERT INTO codex_conversation_messages "
            "(conversation_key,source_root_key,source_path,line_offset,kind,"
            "record_family,content_digest,content_len) "
            "VALUES('v1.orphan','root','orphan.jsonl',1,'assistant',"
            "'response_item','digest',0)"
        )
        with pytest.raises(RuntimeError, match="raw threads table missing"):
            claude_query._scoped_codex_anon_paths(conn, ACCOUNT_A)
    finally:
        conn.close()


def test_scoped_anon_preserves_caller_home_identity_scrub(tmp_path):
    """Account scoping must not drop the caller's home token vocabulary."""
    from _lib_conversation_anon import scrub_text

    conn = _open_scoped_fixture(tmp_path, ACCOUNT_A)
    try:
        result = claude_query.build_anon_plan_for_sources(
            conn, home_dir="/Users/alice", sources={"claude"},
        )
        scrubbed = scrub_text(
            "note in /Users/alice/private-note outside /work/alpha", result.plan,
        )
        assert "/Users/alice" not in scrubbed
        assert "~/private-note" in scrubbed
        assert "/work/alpha" not in scrubbed
    finally:
        conn.close()


def test_scoped_codex_anon_provenance_does_not_materialize_non_metadata_events(
        tmp_path):
    """The provenance read must stay bounded to session_meta rows."""
    conn = _open_scoped_codex_fixture(tmp_path, ACCOUNT_A, scope=False)
    try:
        conn.executemany(
            "INSERT INTO main.codex_conversation_events "
            "(source_path,line_offset,source_root_key,conversation_key,"
            "native_thread_id,root_thread_id,record_type,payload_json,account_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("noise.jsonl", offset, "root", "v1.shared", "shared",
                 "shared", "response_item", "{}", ACCOUNT_A)
                for offset in range(2000)
            ] + [
                ("shared-codex.jsonl", 2000, "root", "v1.shared", "shared",
                 "shared", "session_meta",
                 json.dumps({"type": "session_meta", "payload": {
                     "cwd": "/work/cctally-dev",
                 }}), ACCOUNT_A)
            ],
        )
        conn.commit()
        cache.scope_conversations_db_to_account(conn, ACCOUNT_A)

        class _CountingCursor:
            def __init__(self, cursor, owner):
                self._cursor = cursor
                self._owner = owner

            def __iter__(self):
                for row in self._cursor:
                    self._owner.event_rows += 1
                    yield row

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        class _CountingConnection:
            def __init__(self, connection):
                self._connection = connection
                self.event_rows = 0

            def execute(self, sql, parameters=()):
                cursor = self._connection.execute(sql, parameters)
                if "codex_conversation_events" in sql:
                    return _CountingCursor(cursor, self)
                return cursor

        counted = _CountingConnection(conn)
        result = claude_query.build_anon_plan_for_sources(
            counted, home_dir="/Users/alice", sources={"codex"},
        )
        assert result.ambiguous_cwd_rows == 0
        assert counted.event_rows == 1
    finally:
        conn.close()


def test_scoped_codex_anon_provenance_has_a_partial_session_meta_index(
        tmp_path):
    """The physical provenance read must have an index-only session_meta plan."""
    conn = _open_scoped_codex_fixture(tmp_path, ACCOUNT_A, scope=False)
    try:
        db._ensure_codex_session_meta_provenance_index(conn)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT source_root_key,payload_json,account_key "
            "FROM main.codex_conversation_events "
            "WHERE record_type='session_meta'"
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        assert "USING COVERING INDEX " \
            "idx_codex_events_session_meta_provenance" in detail
    finally:
        conn.close()


def test_scoped_codex_cli_export_refuses_ambiguous_path(
        tmp_path, monkeypatch, capsysbinary):
    """The CLI must not emit an anonymized copy over unproven CWD ownership."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    provider = tmp_path / "provider"
    rollout = provider / "sessions" / "2026" / "08" / "04" / "modern-full.jsonl"
    rollout.parent.mkdir(parents=True)
    corpus = pathlib.Path(__file__).parent / "fixtures" / "codex-parity" / "v1" / "rollouts" / "modern-full.jsonl"
    shutil.copyfile(corpus, rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    cache_conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache_conn, rebuild=True)
        key = cache_conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE source_path LIKE '%/modern-full.jsonl'"
        ).fetchone()[0]
    finally:
        cache_conn.close()
    conversations = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conversations, rebuild=True)
        conversations.execute(
            "UPDATE codex_conversation_messages SET account_key=?",
            (ACCOUNT_B,),
        )
        conversations.execute(
            "UPDATE codex_conversation_events SET account_key=?",
            (ACCOUNT_A,),
        )
        conversations.commit()
    finally:
        conversations.close()
    cache_conn = ns["open_cache_db"]()
    try:
        cache_conn.execute(
            "UPDATE codex_conversation_threads SET cwd=? "
            "WHERE conversation_key=?",
            ("/Users/alice/account-a-project", key),
        )
        cache_conn.commit()
    finally:
        cache_conn.close()
    stats = ns["open_db"]()
    try:
        stats.execute(
            "INSERT INTO accounts (account_key,provider,natural_id,email,label,"
            "plan_type,label_source,first_seen_utc,last_seen_utc) "
            "VALUES (?,'codex',?,?,?,NULL,'auto',?,?)",
            (ACCOUNT_B, ACCOUNT_B, "bravo@example.test", "bravo",
             "2026-08-04T00:00:00Z", "2026-08-04T00:00:00Z"),
        )
        stats.commit()
    finally:
        stats.close()

    args = SimpleNamespace(
        transcript_action="export", session_id=key, scope="all", raw=False,
        output=None, speed=None, account=ACCOUNT_B,
    )
    assert ns["cmd_transcript"](args) == 3
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert "ambiguous account provenance" in captured.err.decode("utf-8")

    # The same physical path becomes provable for A once its session_meta
    # record is account-stamped; the CLI then scrubs it from the copy.
    conversations = ns["open_conversations_db"]()
    try:
        root_key = conversations.execute(
            "SELECT source_root_key FROM codex_conversation_events "
            "WHERE conversation_key=? LIMIT 1", (key,),
        ).fetchone()[0]
        conversations.execute(
            "UPDATE codex_conversation_messages SET account_key=?",
            (ACCOUNT_A,),
        )
        conversations.execute(
            "INSERT INTO codex_conversation_events "
            "(source_path,line_offset,source_root_key,conversation_key,"
            "native_thread_id,root_thread_id,record_type,payload_json,account_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("account-a.jsonl", 0, root_key, key, "a-thread", "a-thread",
             "session_meta", json.dumps({"type": "session_meta", "payload": {
                 "cwd": "/Users/alice/account-a-project",
             }}), ACCOUNT_A),
        )
        conversations.execute(
            "UPDATE codex_conversation_messages SET text=? "
            "WHERE conversation_key=?",
            ("A says /Users/alice/account-a-project", key),
        )
        conversations.commit()
    finally:
        conversations.close()
    stats = ns["open_db"]()
    try:
        stats.execute(
            "INSERT INTO accounts (account_key,provider,natural_id,email,label,"
            "plan_type,label_source,first_seen_utc,last_seen_utc) "
            "VALUES (?,'codex',?,?,?,NULL,'auto',?,?)",
            (ACCOUNT_A, ACCOUNT_A, "alice@example.test", "alice",
             "2026-08-04T00:00:00Z", "2026-08-04T00:00:00Z"),
        )
        stats.commit()
    finally:
        stats.close()
    args.account = ACCOUNT_A
    assert ns["cmd_transcript"](args) == 0
    scrubbed = capsysbinary.readouterr()
    assert b"/Users/alice/account-a-project" not in scrubbed.out
    assert b"A says" in scrubbed.out

    args.raw = True
    assert ns["cmd_transcript"](args) == 0
    raw = capsysbinary.readouterr()
    assert b"/Users/alice/account-a-project" in raw.out
    assert raw.out.startswith(b"#")


def test_account_scope_derives_codex_project_while_rollup_is_bootstrapping(
    tmp_path,
):
    conn = _open_scoped_codex_fixture(
        tmp_path, ACCOUNT_A, drop_persisted_rollup=True,
    )
    try:
        project = conn.execute(
            "SELECT project_key,project_label "
            "FROM codex_conversation_rollups "
            "WHERE conversation_key='v1.shared'"
        ).fetchone()
        assert project is not None
        assert project[0].startswith("project:")
        assert project[1] == "cctally-dev"
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


# ===========================================================================
# #769 S3 — durable account stamps and the source-incarnation key (#777)
# ===========================================================================
# `rebuild_account_stamps` was a process-local dict built from the very rows the
# rebuild then deleted, so a rebuild killed after the clear resumed in a fresh
# process with no attribution at all and restamped every replayed record with
# whichever account happened to be active. The stamps are durable now, keyed by
# an identity that a file rewritten in place cannot forge.
#
# Production carries exactly one distinct `account_key` and zero NULLs, so no
# production state can serve as this section's corpus — every case below seeds
# two accounts explicitly. The helpers carry an `_s3_` prefix because the
# section above owns names of its own.

import os  # noqa: E402 — section-local, kept beside the cases that use it
from conftest import (  # noqa: E402
    redirect_paths_without_conversation_retention,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths_without_conversation_retention(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    conn = ns["open_conversations_db"]()
    yield ns, conn, projects
    try:
        conn.close()
    except Exception:
        pass


def _s3_asst_line(uuid, msg_id, req_id, text, *, sid="s1",
               ts="2026-06-01T00:00:00Z", model="claude-opus-4-8"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": sid,
        "requestId": req_id, "timestamp": ts,
        "message": {"role": "assistant", "id": msg_id, "model": model,
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    }) + "\n"


def _s3_pin_account(monkeypatch, ns, account_key):
    """Pin the active Claude identity the ingest boundary resolves."""
    monkeypatch.setattr(
        ns["_cctally_core"], "_resolve_active_claude_identity",
        lambda: {"status": "ok", "account_key": account_key},
    )


def _s3_accounts(conn):
    return {
        (row[0], row[1]): row[2] for row in conn.execute(
            "SELECT source_path,byte_offset,account_key FROM conversation_messages")
    }


def _s3_stamps(conn):
    return conn.execute(
        "SELECT canonical_source_path,source_incarnation_id,byte_offset,"
        "record_sha256,account_key FROM claude_conversation_account_stamps "
        "ORDER BY canonical_source_path,byte_offset").fetchall()


def _s3_incarnation(conn, path):
    row = conn.execute(
        "SELECT source_incarnation_id FROM conversation_source_files WHERE path=?",
        (str(path),)).fetchone()
    return row[0] if row else None


# --- the stamping branch the per-migration golden cannot hold --------------


def test_every_ingested_message_gets_a_stamp(store, monkeypatch):
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one")
                 + _s3_asst_line("u2", "m2", "r2", "two"))
    ns["sync_claude_conversations"](conn)
    messages = _s3_accounts(conn)
    assert len(messages) == 2
    stamps = _s3_stamps(conn)
    assert len(stamps) == 2
    incarnation = _s3_incarnation(conn, a)
    assert incarnation
    for path, inc, offset, digest, account_key in stamps:
        assert path == str(a)
        assert inc == incarnation
        assert account_key == "acct-a"
        assert len(digest) == 64 and digest == digest.lower()
        assert messages[(path, offset)] == "acct-a"


def test_the_digest_is_over_the_raw_on_disk_bytes(store, monkeypatch):
    """Not over the decoded text, and not over a re-serialized parse. The sync
    walker decodes with `errors="replace"`, so a record carrying invalid UTF-8
    decodes to a different string than it was written as."""
    import hashlib
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    line = _s3_asst_line("u1", "m1", "r1", "one").encode()
    a.write_bytes(line)
    ns["sync_claude_conversations"](conn)
    digest = _s3_stamps(conn)[0][3]
    assert digest == hashlib.sha256(line[:-1]).hexdigest()


def test_invalid_utf8_does_not_move_the_digest_between_runs(store, monkeypatch):
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    raw = _s3_asst_line("u1", "m1", "r1", "one").encode()
    # A second record whose text carries a byte no UTF-8 decoder accepts.
    bad = _s3_asst_line("u2", "m2", "r2", "two").encode().replace(b"two", b"t\xffo")
    a.write_bytes(raw + bad)
    ns["sync_claude_conversations"](conn)
    first = _s3_stamps(conn)
    assert len(first) == 2
    conn.execute("DELETE FROM conversation_messages")
    conn.execute("DELETE FROM claude_conversation_account_stamps")
    conn.execute("DELETE FROM conversation_source_files")
    conn.commit()
    ns["sync_claude_conversations"](conn)
    second = _s3_stamps(conn)
    assert [row[3] for row in second] == [row[3] for row in first]


def test_crlf_and_lf_digest_the_same_record_identically(store, monkeypatch):
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    body = _s3_asst_line("u1", "m1", "r1", "one")[:-1].encode()
    a = projects / "a.jsonl"
    a.write_bytes(body + b"\n")
    ns["sync_claude_conversations"](conn)
    lf_digest = _s3_stamps(conn)[0][3]
    b = projects / "b.jsonl"
    b.write_bytes(body + b"\r\n")
    ns["sync_claude_conversations"](conn)
    crlf = [row for row in _s3_stamps(conn) if row[0] == str(b)]
    assert crlf and crlf[0][3] == lf_digest


# --- source incarnation: one identity per append-continuous life -----------


def test_an_ordinary_append_keeps_its_s3_incarnation(store, monkeypatch):
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    first = _s3_incarnation(conn, a)
    with open(a, "a") as fh:
        fh.write(_s3_asst_line("u2", "m2", "r2", "two"))
    ns["sync_claude_conversations"](conn)
    assert _s3_incarnation(conn, a) == first
    assert len(_s3_stamps(conn)) == 2


def test_an_inode_change_mints_a_new_s3_incarnation(store, monkeypatch):
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    first = _s3_incarnation(conn, a)
    replacement = projects / "replacement.tmp"
    replacement.write_text(_s3_asst_line("u1", "m1", "r1", "one")
                           + _s3_asst_line("u2", "m2", "r2", "two"))
    os.replace(replacement, a)
    ns["sync_claude_conversations"](conn)
    assert _s3_incarnation(conn, a) != first


def test_a_size_preserving_rewrite_with_an_unchanged_mtime_is_a_new_s3_incarnation(
        store, monkeypatch):
    """The `touch`-defeating case. `_conversation_target_risk` decides this on
    mtime and is defeated by restoring it; the committed-prefix digest is not."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    first = _s3_incarnation(conn, a)
    st = a.stat()
    rewritten = _s3_asst_line("u1", "m1", "r1", "ONE")
    assert len(rewritten) == st.st_size, "the case needs an equal-size rewrite"
    with open(a, "r+b") as fh:
        fh.write(rewritten.encode())
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert a.stat().st_size == st.st_size
    assert a.stat().st_mtime_ns == st.st_mtime_ns
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert _s3_incarnation(conn, a) != first


def test_a_shrink_mints_a_new_s3_incarnation(store, monkeypatch):
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one")
                 + _s3_asst_line("u2", "m2", "r2", "two"))
    ns["sync_claude_conversations"](conn)
    first = _s3_incarnation(conn, a)
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    assert _s3_incarnation(conn, a) != first


def test_a_reused_offset_under_a_new_incarnation_inherits_nothing(
        store, monkeypatch):
    """A stamp is keyed by the incarnation, so an unrelated file that happens
    to reuse a path and an offset cannot pick up the old attribution."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    old_incarnation = _s3_incarnation(conn, a)
    old_stamps = _s3_stamps(conn)
    replacement = projects / "replacement.tmp"
    replacement.write_text(_s3_asst_line("u9", "m9", "r9", "different"))
    os.replace(replacement, a)
    _s3_pin_account(monkeypatch, ns, "acct-b")
    ns["sync_claude_conversations"](conn, rebuild=True)
    new_incarnation = _s3_incarnation(conn, a)
    assert new_incarnation != old_incarnation
    fresh = [row for row in _s3_stamps(conn) if row[1] == new_incarnation]
    assert [row[4] for row in fresh] == ["acct-b"], (
        "a record under a new incarnation is first observed now"
    )
    assert old_stamps[0][3] not in {row[3] for row in fresh}
    # The superseded stamp is a durable record of an observation that really
    # happened, so it stays. What matters is that the new incarnation cannot
    # reach it: the incarnation is part of the key.
    assert any(row[1] == old_incarnation for row in _s3_stamps(conn))
    assert set(_s3_accounts(conn).values()) == {"acct-b"}


def test_a_device_only_difference_preserves_continuity_and_attribution(
        store, monkeypatch):
    """#814. `st_dev` is assigned at mount time, so a remount renumbers every
    stored `device_id` at once without changing a single file. Deciding
    replacement on it mints a fresh incarnation whose high-water is zero, and
    every already-attributed record is then restamped to whichever account is
    active now — the corruption `_resolve_record_account` describes at
    `bin/_cctally_cache.py:12376-12380`.
    """
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one")
                 + _s3_asst_line("u2", "m2", "r2", "two"))
    ns["sync_claude_conversations"](conn)
    first_incarnation = _s3_incarnation(conn, a)
    before = _s3_accounts(conn)
    assert set(before.values()) == {"acct-a"}
    committed = conn.execute(
        "SELECT last_byte_offset FROM conversation_source_files WHERE path=?",
        (str(a),)).fetchone()[0]
    assert committed > 0

    # The remount: only the STORED device changes. The file is untouched, and
    # the real `fstat` still reports the true device, so the same-descriptor
    # guard stays exercised. This is the simulation the Codex device-only
    # tests already use (`tests/test_codex_file_identity.py:280`).
    stored_device = conn.execute(
        "SELECT device_id FROM conversation_source_files WHERE path=?",
        (str(a),)).fetchone()[0]
    conn.execute(
        "UPDATE conversation_source_files SET device_id=? WHERE path=?",
        (int(stored_device) + 1, str(a)))
    conn.commit()

    # An append is required: an ordinary sync skips a same-size file before
    # `_resolve_source_incarnation` is reached (`bin/_cctally_cache.py:12464`),
    # so the stale device is harmless until the file's size changes.
    resumed_from = []
    cache_mod = ns["_cctally_cache"]
    inner = cache_mod._iter_sync_entries

    def _record_resume(fh, *args, **kwargs):
        resumed_from.append(fh.tell())
        return inner(fh, *args, **kwargs)

    monkeypatch.setattr(cache_mod, "_iter_sync_entries", _record_resume)
    _s3_pin_account(monkeypatch, ns, "acct-b")
    a.write_text(a.read_text() + _s3_asst_line("u3", "m3", "r3", "three"))
    ns["sync_claude_conversations"](conn)

    assert _s3_incarnation(conn, a) == first_incarnation, (
        "a device-only difference at an unchanged inode is a remount, not a "
        "replacement, so the incarnation must survive it"
    )
    assert resumed_from and resumed_from[-1] == committed, (
        "continuity means resuming at the committed offset, not replaying "
        f"from byte zero; resumed at {resumed_from}"
    )
    after = _s3_accounts(conn)
    preserved = {key: value for key, value in after.items() if key in before}
    assert preserved == before, (
        "the records ingested under acct-a must still be acct-a; a false "
        "incarnation bump restamps every one of them to the active account"
    )
    appended = [value for key, value in after.items() if key not in before]
    assert appended == ["acct-b"], (
        "only the newly appended record is first observed now, so only it "
        f"takes the active account; got {appended}"
    )


def test_a_non_integer_stored_inode_degrades_instead_of_aborting_the_walk(
        store, monkeypatch):
    """#814 / spec §2.4. `source_identity_replaced` degrades an unreadable
    stored identity to "no evidence" rather than raising, because a raise here
    escapes the per-file loop and takes every LATER file in the estate with it.

    The helper's own unit tests cover the NULL combinations but not a
    non-integer value, and the only full-walk regression for that case
    exercises the two Codex stores rather than Claude's
    `conversation_source_files` resolver (`tests/test_codex_file_identity.py:400`).
    A stray `int(stored_inode)` left OUTSIDE the helper would abort the whole
    estate while every helper unit test stayed green.
    """
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    b = projects / "b.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one", sid="sa"))
    b.write_text(_s3_asst_line("v1", "n1", "q1", "other", sid="sb"))
    ns["sync_claude_conversations"](conn)
    first_a = _s3_incarnation(conn, a)
    first_b = _s3_incarnation(conn, b)
    assert first_a and first_b

    conn.execute(
        "UPDATE conversation_source_files SET inode=? WHERE path=?",
        ("not-an-inode", str(a)))
    conn.commit()

    # An append, so the same-size shortcut does not skip the resolver.
    a.write_text(a.read_text() + _s3_asst_line("u2", "m2", "r2", "two", sid="sa"))
    b.write_text(b.read_text() + _s3_asst_line("v2", "n2", "q2", "more", sid="sb"))
    ns["sync_claude_conversations"](conn)

    assert _s3_incarnation(conn, a) == first_a, (
        "an unreadable stored inode is NO EVIDENCE, which returns the resolver "
        "to size and digest — the pre-#769 behaviour. This is an append whose "
        "committed prefix still hashes the same, so continuity holds. What "
        "matters is that the walk decided it rather than raising."
    )
    assert _s3_incarnation(conn, b) == first_b, (
        "the unreadable identity on one file must not disturb any other file "
        "in the estate; a raise inside the loop would have skipped this one"
    )
    assert len(_s3_accounts(conn)) == 4, (
        "every record of both files is ingested; the walk completed"
    )


# --- the rebuild attribution rules ----------------------------------------


def test_a_rebuild_preserves_historical_attribution(store, monkeypatch):
    """The #777 headline. Ingest as A, switch the active identity to B, rebuild
    — and A's records stay A's."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    assert set(_s3_accounts(conn).values()) == {"acct-a"}
    _s3_pin_account(monkeypatch, ns, "acct-b")
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert set(_s3_accounts(conn).values()) == {"acct-a"}, (
        "a rebuild replays the same bytes; it does not re-attribute them"
    )


def test_an_interrupted_rebuild_preserves_the_account_in_a_fresh_process(
        store, monkeypatch):
    """The durable half. The old in-process dict died with the process that
    built it, so a resume attributed every replayed record to whoever was
    active. Reopening the store stands in for that fresh process."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    # The state a killed rebuild actually leaves: the pending marker committed,
    # the messages cleared, and `conversation_source_files` untouched — the
    # cursor rows are what the resumed replay verifies continuity against.
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) "
                 "VALUES('conversation_rebuild_claude_pending','1')")
    conn.execute("DELETE FROM conversation_messages")
    conn.commit()
    conn.close()
    _s3_pin_account(monkeypatch, ns, "acct-b")
    resumed = ns["open_conversations_db"]()
    try:
        ns["sync_claude_conversations"](resumed)
        assert set(_s3_accounts(resumed).values()) == {"acct-a"}
    finally:
        resumed.close()


def test_an_offset_below_the_high_water_with_no_stamp_is_unattributed(
        store, monkeypatch):
    """Historical-and-unknown is NULL, never the active account. Writing the
    active account there is exactly the silent re-attribution #777 reports."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one")
                 + _s3_asst_line("u2", "m2", "r2", "two"))
    ns["sync_claude_conversations"](conn)
    conn.execute("DELETE FROM claude_conversation_account_stamps")
    conn.execute("DELETE FROM claude_conversation_account_stamp_gaps")
    conn.commit()
    _s3_pin_account(monkeypatch, ns, "acct-b")
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert set(_s3_accounts(conn).values()) == {None}


def test_bytes_at_or_beyond_the_high_water_take_the_active_identity(
        store, monkeypatch):
    """First observed now, so it resolves the active identity freshly and
    creates its stamp — the other half of the separation the high-water map
    exists to make."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    with open(a, "a") as fh:
        fh.write(_s3_asst_line("u2", "m2", "r2", "two"))
    _s3_pin_account(monkeypatch, ns, "acct-b")
    ns["sync_claude_conversations"](conn)
    by_offset = sorted(_s3_accounts(conn).items())
    assert [value for _key, value in by_offset] == ["acct-a", "acct-b"]


def test_a_gap_row_keeps_the_attribution_a_stamp_could_not(store, monkeypatch):
    """A source file deleted since ingestion has no bytes to digest. The
    classified gap is what stops the backfill from discarding an orphan's real
    attribution."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    conn.execute("DELETE FROM claude_conversation_account_stamps")
    conn.execute("DELETE FROM cache_meta WHERE key=?",
                 ("claude_account_stamp_coverage_complete",))
    conn.commit()
    import _cctally_db as db
    db.backfill_claude_account_stamps(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM claude_conversation_account_stamps"
    ).fetchone()[0] == 1, "a readable file backfills to a stamp, not a gap"
    a.unlink()
    conn.execute("DELETE FROM claude_conversation_account_stamps")
    conn.execute("DELETE FROM cache_meta WHERE key=?",
                 ("claude_account_stamp_coverage_complete",))
    conn.commit()
    db.backfill_claude_account_stamps(conn)
    assert conn.execute(
        "SELECT source_path,cause,account_key "
        "FROM claude_conversation_account_stamp_gaps").fetchall() == [
        (str(a), "source_unreadable", "acct-a")]


# --- the publication gate --------------------------------------------------


def test_the_first_rebuild_after_the_migration_backfill_keeps_every_account(
        store, monkeypatch):
    """The case a copy-on-write rehearsal on the real store caught, and the
    suite did not.

    Every other rebuild case here starts from a store the LIVE ingester built,
    and the ingester records `device_id`, `inode` and `committed_prefix_sha256`
    alongside the incarnation. A store whose stamps came from migration 009
    instead has none of those, so the continuity check rejects it, a fresh
    incarnation is minted, every stamp misses on its incarnation column, and
    the replay re-attributes all of it. On the real store that was 43,810 of
    43,810 messages restamped to the active account, with the coverage marker
    reporting complete throughout — which is exactly the loss the backfill
    exists to prevent, performed by the code that is supposed to prevent it.
    """
    import _cctally_db as db
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one")
                 + _s3_asst_line("u2", "m2", "r2", "two"))
    ns["sync_claude_conversations"](conn)
    before = _s3_accounts(conn)
    assert set(before.values()) == {"acct-a"}

    # Reduce the store to what a pre-009 store looks like: the messages and the
    # cursor survive, the stamps and the whole identity do not.
    conn.execute("DELETE FROM claude_conversation_account_stamps")
    conn.execute("DELETE FROM claude_conversation_account_stamp_gaps")
    conn.execute("UPDATE conversation_source_files SET source_incarnation_id=NULL,"
                 " device_id=NULL, inode=NULL, committed_prefix_sha256=NULL")
    conn.execute("DELETE FROM cache_meta WHERE key=?",
                 ("claude_account_stamp_coverage_complete",))
    conn.commit()

    db.backfill_claude_account_stamps(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM claude_conversation_account_stamps"
    ).fetchone()[0] == 2
    identity = conn.execute(
        "SELECT source_incarnation_id,device_id,inode,committed_prefix_sha256 "
        "FROM conversation_source_files WHERE path=?", (str(a),)).fetchone()
    assert all(field is not None for field in identity), (
        "the backfill must record the whole identity a stamp is keyed to, or "
        "the very first rebuild cannot find the stamps it just wrote"
    )

    _s3_pin_account(monkeypatch, ns, "acct-b")
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert _s3_accounts(conn) == before


def test_a_rebuild_refuses_to_publish_while_stamp_coverage_is_incomplete(
        store, monkeypatch):
    """Shipping the lookup-miss rule over an unbackfilled store is the loss the
    backfill exists to prevent, so the rebuild refuses rather than proceeding."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    before = _s3_accounts(conn)
    conn.execute("DELETE FROM cache_meta WHERE key=?",
                 ("claude_account_stamp_coverage_complete",))
    conn.commit()
    stats = ns["sync_claude_conversations"](conn, rebuild=True)
    assert stats.deferred_reason == "stamp_backfill_pending"
    assert _s3_accounts(conn) == before, "the refusal precedes every destructive step"


def test_the_coverage_gate_lets_a_backfilled_store_through(store, monkeypatch):
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    assert conn.execute(
        "SELECT value FROM cache_meta WHERE key=?",
        ("claude_account_stamp_coverage_complete",)).fetchone() == ("1",)
    stats = ns["sync_claude_conversations"](conn, rebuild=True)
    assert stats.deferred_reason is None


# --- a concurrent append is a continuation, not an identity break ----------
#
# Tranche 2 treated ANY change in size or mtime across the descriptor's fstat
# pair as an identity break and aborted the file. The provider appends to an
# active transcript continuously, so an ordinary append landed inside that
# window, raised `files_failed`, withheld the sync certificate and forced the
# whole rebuild to run again. Spec §2 now classifies the pair by what changed.


def _appending_walker(real, path, line, fired):
    """Wrap the walker so a real append lands AFTER the read and BEFORE the
    trailing `fstat` — the exact window the descriptor pair covers."""
    def walking(*args, **kwargs):
        yield from real(*args, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            with open(path, "a") as fh:
                fh.write(line)
    return walking


def _arm_raced_append(ns, monkeypatch, cache, path, fired):
    """Make the file genuinely dirty, then arm the raced append.

    The pass has to have work to do: a sync skips a file whose size and mtime
    are unchanged, so without the first append the walker is never entered and
    the descriptor pair is never taken.
    """
    with open(path, "a") as fh:
        fh.write(_s3_asst_line("u2", "m2", "r2", "two"))
    monkeypatch.setattr(
        cache, "_iter_sync_entries",
        _appending_walker(cache._iter_sync_entries, path,
                          _s3_asst_line("u3", "m3", "r3", "three"), fired))


def test_an_append_landing_mid_read_does_not_fail_the_file(store, monkeypatch):
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    first = _s3_incarnation(conn, a)
    cache = ns["_cctally_cache"]
    fired = {"done": False}
    _arm_raced_append(ns, monkeypatch, cache, a, fired)
    stats = ns["sync_claude_conversations"](conn)
    assert fired["done"], "the append must really have landed inside the window"
    assert stats.files_failed == 0, (
        "an append is a continuation, not a file-identity break")
    assert stats.files_processed == 1
    assert _s3_incarnation(conn, a) == first


def test_an_append_landing_mid_read_keeps_the_sync_certifiable(store,
                                                               monkeypatch):
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    cache = ns["_cctally_cache"]
    fired = {"done": False}
    _arm_raced_append(ns, monkeypatch, cache, a, fired)
    stats = ns["sync_claude_conversations"](conn)
    assert fired["done"]
    import _lib_ingest_frontier as frontier
    # `common_clean` reads `files_failed`, so a raced append used to withhold
    # the conversation frontier certificate outright.
    assert frontier.provider_sync_certifiable("targeted", stats)
    assert stats.targeted_clean


def test_an_append_landing_mid_read_does_not_force_a_rebuild_rerun(
        store, monkeypatch):
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    cache = ns["_cctally_cache"]
    fired = {"done": False}
    _arm_raced_append(ns, monkeypatch, cache, a, fired)
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert fired["done"]
    assert conn.execute(
        "SELECT 1 FROM cache_meta "
        "WHERE key='conversation_rebuild_claude_pending'").fetchone() is None, (
        "one raced append must not leave the whole rebuild pending")


def test_an_append_landing_mid_read_commits_the_bytes_it_read(store,
                                                              monkeypatch):
    """The records read in that pass are kept, and the NEXT sync resumes from
    the stored cursor rather than replaying the file from zero."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    cache = ns["_cctally_cache"]
    real = cache._iter_sync_entries
    fired = {"done": False}
    _arm_raced_append(ns, monkeypatch, cache, a, fired)
    ns["sync_claude_conversations"](conn)
    assert fired["done"]
    incarnation = _s3_incarnation(conn, a)
    cursor = conn.execute(
        "SELECT last_byte_offset FROM conversation_source_files WHERE path=?",
        (str(a),)).fetchone()[0]
    assert cursor == (len(_s3_asst_line("u1", "m1", "r1", "one"))
                      + len(_s3_asst_line("u2", "m2", "r2", "two"))), (
        "the bytes this pass read are committed; the raced append is not")
    assert len(_s3_accounts(conn)) == 2
    monkeypatch.setattr(cache, "_iter_sync_entries", real)
    stats = ns["sync_claude_conversations"](conn)
    assert stats.files_failed == 0
    assert stats.files_reset_truncated == 0, (
        "continuity held, so the next sync resumes rather than replaying")
    assert _s3_incarnation(conn, a) == incarnation
    assert len(_s3_accounts(conn)) == 3
    assert len(_s3_stamps(conn)) == 3


def test_a_truncation_landing_mid_read_is_still_an_identity_break(
        store, monkeypatch):
    """The window the descriptor pair exists to cover still closes: a shrink
    inside it commits nothing for that file.

    A `rename` is deliberately NOT this case and cannot be: the descriptor
    keeps the old inode, so `fstat` reports the file the reader actually read.
    The bytes stay self-consistent, and the next sync opens the new directory
    entry, sees a different inode and mints a fresh incarnation.
    """
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    before = _s3_accounts(conn)
    cache = ns["_cctally_cache"]
    real = cache._iter_sync_entries
    fired = {"done": False}
    # Dirty the file so the pass has work and the descriptor pair is taken.
    with open(a, "a") as fh:
        fh.write(_s3_asst_line("u2", "m2", "r2", "two"))

    def truncating(*args, **kwargs):
        yield from real(*args, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            with open(a, "r+b") as fh:
                fh.truncate(10)

    monkeypatch.setattr(cache, "_iter_sync_entries", truncating)
    stats = ns["sync_claude_conversations"](conn)
    assert fired["done"]
    assert stats.files_failed == 1
    assert _s3_accounts(conn) == before


def test_a_rename_landing_mid_read_is_read_consistently_then_reincarnated(
        store, monkeypatch):
    """`os.replace` swaps the directory entry, not the open file, so the pair
    reports no break and the pass commits the bytes it really read. The NEXT
    sync opens the replacement, sees a different inode and replays from zero."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    first = _s3_incarnation(conn, a)
    cache = ns["_cctally_cache"]
    real = cache._iter_sync_entries
    fired = {"done": False}
    with open(a, "a") as fh:
        fh.write(_s3_asst_line("u2", "m2", "r2", "two"))

    def replacing(*args, **kwargs):
        yield from real(*args, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            replacement = projects / "replacement.tmp"
            replacement.write_text(_s3_asst_line("u9", "m9", "r9", "other"))
            os.replace(replacement, a)

    monkeypatch.setattr(cache, "_iter_sync_entries", replacing)
    stats = ns["sync_claude_conversations"](conn)
    assert fired["done"]
    assert stats.files_failed == 0
    assert _s3_incarnation(conn, a) == first
    monkeypatch.setattr(cache, "_iter_sync_entries", real)
    ns["sync_claude_conversations"](conn)
    assert _s3_incarnation(conn, a) != first


def test_a_size_preserving_rewrite_landing_mid_read_is_an_identity_break(
        store, monkeypatch):
    """Same size, changed mtime — the case `_conversation_target_risk` calls
    `source_replaced`. It is a rewrite, not an append, so it breaks."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    before = _s3_accounts(conn)
    cache = ns["_cctally_cache"]
    real = cache._iter_sync_entries
    fired = {"done": False}
    rewritten = _s3_asst_line("u1", "m1", "r1", "ONE")
    assert len(rewritten) == a.stat().st_size
    # Dirty the file so the pass has work and the descriptor pair is taken.
    with open(a, "a") as fh:
        fh.write(_s3_asst_line("u2", "m2", "r2", "two"))

    def rewriting(*args, **kwargs):
        yield from real(*args, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            size_before = a.stat().st_size
            with open(a, "r+b") as fh:
                fh.write(rewritten.encode())
            assert a.stat().st_size == size_before, (
                "the case needs a size-PRESERVING rewrite")
            st = a.stat()
            os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    monkeypatch.setattr(cache, "_iter_sync_entries", rewriting)
    stats = ns["sync_claude_conversations"](conn)
    assert fired["done"]
    assert stats.files_failed == 1
    assert _s3_accounts(conn) == before


# --- the backfill's chunked resume and its four gap causes -----------------
#
# `_STAMP_BACKFILL_PATHS_PER_TXN` is 25, and the golden fixture carries two
# source paths while the live case carries one, so nothing exercised the
# cursor advance or the resume-from-cursor path. Of the four gap causes only
# `source_unreadable` was asserted.


def _s3_seed_many_paths(ns, conn, projects, count):
    """One message per source path, so the path count is the chunk driver."""
    for index in range(count):
        (projects / f"f{index:03d}.jsonl").write_text(
            _s3_asst_line(f"u{index}", f"m{index}", f"r{index}", "line",
                          sid=f"s{index}"))
    ns["sync_claude_conversations"](conn)


def _s3_reset_backfill_state(conn):
    conn.execute("DELETE FROM claude_conversation_account_stamps")
    conn.execute("DELETE FROM claude_conversation_account_stamp_gaps")
    conn.execute("DELETE FROM cache_meta WHERE key=?",
                 ("claude_account_stamp_coverage_complete",))
    conn.execute("DELETE FROM cache_meta WHERE key=?",
                 ("claude_account_stamp_backfill_cursor",))
    conn.commit()


def _s3_backfill_cursor(conn):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?",
                       ("claude_account_stamp_backfill_cursor",)).fetchone()
    return row[0] if row else None


def test_the_backfill_advances_a_durable_cursor_across_chunks(store,
                                                              monkeypatch):
    import _cctally_db as db
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    _s3_seed_many_paths(ns, conn, projects, 30)
    _s3_reset_backfill_state(conn)
    assert _s3_backfill_cursor(conn) is None
    observed = []
    real_set = db._set_cache_meta

    def watching(conn_, key, value):
        if key == "claude_account_stamp_backfill_cursor":
            observed.append(value)
        return real_set(conn_, key, value)

    monkeypatch.setattr(db, "_set_cache_meta", watching)
    db.backfill_claude_account_stamps(conn)
    assert observed, (
        f"30 paths against a chunk of {db._STAMP_BACKFILL_PATHS_PER_TXN} must "
        "commit at least one cursor advance"
    )
    assert observed == sorted(observed), "the cursor advances in sorted order"
    assert _s3_backfill_cursor(conn) is None, "and is cleared on completion"


def test_an_interrupted_backfill_resumes_from_its_cursor(store, monkeypatch):
    """Interrupt mid-chunk, re-run, and the final stamp set must equal the
    uninterrupted result while the cursor really carried the progress."""
    import _cctally_db as db
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    _s3_seed_many_paths(ns, conn, projects, 30)

    _s3_reset_backfill_state(conn)
    db.backfill_claude_account_stamps(conn)
    uninterrupted = _s3_stamps(conn)
    assert len(uninterrupted) == 30

    _s3_reset_backfill_state(conn)
    real_read = db._stamp_backfill_read_records
    seen = {"paths": 0}

    def interrupting(path_str, offsets, committed_offset):
        seen["paths"] += 1
        if seen["paths"] > db._STAMP_BACKFILL_PATHS_PER_TXN + 2:
            raise KeyboardInterrupt("killed mid-chunk")
        return real_read(path_str, offsets, committed_offset)

    monkeypatch.setattr(db, "_stamp_backfill_read_records", interrupting)
    with pytest.raises(KeyboardInterrupt):
        db.backfill_claude_account_stamps(conn)
    conn.rollback()
    partial_cursor = _s3_backfill_cursor(conn)
    assert partial_cursor, "the interrupted run must leave durable progress"
    partial = len(_s3_stamps(conn))
    assert 0 < partial < 30

    monkeypatch.setattr(db, "_stamp_backfill_read_records", real_read)
    resumed = {"first": None}
    real_read_2 = db._stamp_backfill_read_records

    def recording(path_str, offsets, committed_offset):
        if resumed["first"] is None:
            resumed["first"] = path_str
        return real_read_2(path_str, offsets, committed_offset)

    monkeypatch.setattr(db, "_stamp_backfill_read_records", recording)
    db.backfill_claude_account_stamps(conn)
    assert resumed["first"] > partial_cursor, (
        "the resume starts after the cursor rather than restarting the walk")
    assert _s3_stamps(conn) == uninterrupted
    assert _s3_backfill_cursor(conn) is None


def _s3_backfill_gap_causes(conn):
    return {
        row[0]: row[1] for row in conn.execute(
            "SELECT source_path,cause FROM claude_conversation_account_stamp_gaps")
    }


def test_a_source_changed_during_the_backfill_is_classified(store, monkeypatch):
    import _cctally_db as db
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    _s3_reset_backfill_state(conn)
    fired = {"done": False}
    real_fstat = os.fstat

    class _AppendingOs:
        """Append BETWEEN the `fstat` pair, which is the window the guard
        covers. Appending before the pair would leave both halves equal."""

        def __getattr__(self, name):
            return getattr(os, name)

        def fstat(self, fd):
            st = real_fstat(fd)
            if not fired["done"]:
                fired["done"] = True
                with open(a, "a") as writer:
                    writer.write(_s3_asst_line("u2", "m2", "r2", "two"))
            return st

    monkeypatch.setattr(db, "os", _AppendingOs())
    db.backfill_claude_account_stamps(conn)
    assert fired["done"]
    assert _s3_backfill_gap_causes(conn) == {
        str(a): "source_changed_during_backfill"}
    assert conn.execute(
        "SELECT COUNT(*) FROM claude_conversation_account_stamps"
    ).fetchone()[0] == 0


def test_a_source_shorter_than_its_cursor_is_classified(store, monkeypatch):
    import _cctally_db as db
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one")
                 + _s3_asst_line("u2", "m2", "r2", "two"))
    ns["sync_claude_conversations"](conn)
    _s3_reset_backfill_state(conn)
    # The cursor still describes both records; the file now holds one.
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    db.backfill_claude_account_stamps(conn)
    assert _s3_backfill_gap_causes(conn) == {
        str(a): "source_shorter_than_cursor"}


def test_a_message_with_no_source_file_cursor_is_classified(store, monkeypatch):
    """The bytes are readable, but no cursor row exists to carry an identity,
    so a rebuild would have nothing to check continuity against."""
    import _cctally_db as db
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    _s3_reset_backfill_state(conn)
    conn.execute("DELETE FROM conversation_source_files WHERE path=?", (str(a),))
    conn.commit()
    db.backfill_claude_account_stamps(conn)
    assert _s3_backfill_gap_causes(conn) == {str(a): "no_source_file_cursor"}
    assert conn.execute(
        "SELECT COUNT(*) FROM claude_conversation_account_stamps"
    ).fetchone()[0] == 0


def test_an_offset_that_does_not_begin_a_record_is_a_gap_not_a_partial_stamp(
        store, monkeypatch):
    """A mid-record offset used to yield a partial span, whose digest no record
    reproduces — a stamp written and permanently unreachable."""
    import _cctally_db as db
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    ns["sync_claude_conversations"](conn)
    _s3_reset_backfill_state(conn)
    conn.execute(
        "UPDATE conversation_messages SET byte_offset=5 WHERE source_path=?",
        (str(a),))
    conn.commit()
    db.backfill_claude_account_stamps(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM claude_conversation_account_stamps"
    ).fetchone()[0] == 0, "a mid-record offset must not produce a stamp"
    assert _s3_backfill_gap_causes(conn) == {str(a): "offset_not_a_record_start"}


# --- pruned source paths keep no unreachable stamps ------------------------


def test_pruning_a_source_path_removes_its_unreachable_stamps(store,
                                                              monkeypatch):
    """`_resolve_record_account` keys on `(incarnation_id, offset, digest)`,
    and with the `conversation_source_files` row gone `_resolve_source_incarnation`
    sees `prev is None` and always mints a fresh incarnation. Every stamp for
    that path is therefore unreachable, and keeping it grows the table without
    bound."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    b = projects / "b.jsonl"
    b.write_text(_s3_asst_line("u2", "m2", "r2", "two", sid="s2"))
    ns["sync_claude_conversations"](conn)
    assert {row[0] for row in _s3_stamps(conn)} == {str(a), str(b)}
    b.unlink()
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_source_files WHERE path=?",
        (str(b),)).fetchone()[0] == 0
    assert {row[0] for row in _s3_stamps(conn)} == {str(a)}, (
        "a stamp no read path can reach must not survive the prune")
    assert conn.execute(
        "SELECT COUNT(*) FROM claude_conversation_account_stamp_gaps "
        "WHERE source_path=?", (str(b),)).fetchone()[0] == 0


def test_a_walk_that_discovers_nothing_deletes_no_stamp(store, monkeypatch):
    """The prune computes its stale set by difference against the walked set,
    so a walk that finds nothing used to call every tracked path stale and
    delete every stamp in the store. An unmounted volume or a wrong
    `CLAUDE_CONFIG_DIR` produces exactly that walk, and stamps are the one
    thing in this store that is not re-derivable (spec §2)."""
    ns, conn, projects = store
    cache = ns["_cctally_cache"]
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    b = projects / "b.jsonl"
    b.write_text(_s3_asst_line("u2", "m2", "r2", "two", sid="s2"))
    ns["sync_claude_conversations"](conn)
    before_stamps = {row[0] for row in _s3_stamps(conn)}
    assert before_stamps == {str(a), str(b)}
    before_files = {
        row[0] for row in conn.execute(
            "SELECT path FROM conversation_source_files")
    }

    monkeypatch.setattr(cache, "_iter_claude_jsonl_files", lambda: iter(()))
    ns["sync_claude_conversations"](conn, rebuild=True)

    assert {row[0] for row in _s3_stamps(conn)} == before_stamps, (
        "an empty walk is not evidence that any file was removed")
    assert {
        row[0] for row in conn.execute(
            "SELECT path FROM conversation_source_files")
    } == before_files


def test_a_path_still_on_disk_survives_a_walk_that_missed_it(store,
                                                             monkeypatch):
    """Absent from THIS walk is not the same fact as absent from disk. A
    partial walk — a permission error under one project directory, a root that
    momentarily failed to enumerate — must not delete the attribution for a
    file that is still there."""
    ns, conn, projects = store
    cache = ns["_cctally_cache"]
    _s3_pin_account(monkeypatch, ns, "acct-a")
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    b = projects / "b.jsonl"
    b.write_text(_s3_asst_line("u2", "m2", "r2", "two", sid="s2"))
    ns["sync_claude_conversations"](conn)
    assert {row[0] for row in _s3_stamps(conn)} == {str(a), str(b)}

    monkeypatch.setattr(cache, "_iter_claude_jsonl_files", lambda: iter([a]))
    ns["sync_claude_conversations"](conn, rebuild=True)

    assert str(b) in {row[0] for row in _s3_stamps(conn)}, (
        "b.jsonl is still on disk; only a genuine absence may prune it")
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_source_files WHERE path=?",
        (str(b),)).fetchone()[0] == 1
