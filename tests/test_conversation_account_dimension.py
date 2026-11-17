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
from conftest import as_process_owner, load_script, redirect_paths


ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32


def _open_scoped_fixture(tmp_path: pathlib.Path, account_key: str, *,
                         drop_fingerprint: bool = False) -> sqlite3.Connection:
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
    if drop_fingerprint:
        # A store migration 013 has not reached (#1068).
        conn.execute(
            "ALTER TABLE conversation_sessions DROP COLUMN input_fingerprint")
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


# --- #1073: model chips on an account-scoped connection ----------------------
# Rows per session, in timestamp order: (account_key, model, text). The two
# shared sessions put their halves in opposite insertion orders, n1 and u1 use
# the two stored unattributed spellings, and m1 mixes both spellings (with
# different families) with an A row.
_UNATTRIBUTED = "unattributed"
_SCOPED_MODEL_ROWS = (
    ("a1", ((ACCOUNT_A, "claude-opus-4-6", "zephyr tail poll"),
            (ACCOUNT_A, "claude-opus-4-7", "patched the cursor"))),
    ("a2", ((ACCOUNT_A, "claude-sonnet-4-6", "release notes"),
            (ACCOUNT_A, "claude-haiku-4-5", "zephyr section"))),
    ("a3", ((ACCOUNT_A, "claude-sonnet-4-6", "rename helpers"),)),
    ("b1", ((ACCOUNT_B, "claude-opus-4-6", "zephyr budget"),)),
    ("b2", ((ACCOUNT_B, "claude-haiku-4-5", "zephyr garden"),)),
    ("s1", ((ACCOUNT_A, "claude-sonnet-4-6", "zephyr run one"),
            (ACCOUNT_B, "claude-opus-4-6", "zephyr run two"))),
    ("s2", ((ACCOUNT_B, "claude-haiku-4-5", "zephyr inventory"),
            (ACCOUNT_A, "claude-opus-4-7", "finish inventory"))),
    ("n1", ((None, "claude-opus-4-6", "zephyr packing"),)),
    ("u1", ((_UNATTRIBUTED, "claude-haiku-4-5", "zephyr receipts"),)),
    ("m1", ((None, "claude-sonnet-4-6", "zephyr checklist"),
            (_UNATTRIBUTED, "claude-fable-5", "tighten checklist"),
            (ACCOUNT_A, "claude-opus-4-6", "review at work"))),
)
_SCOPE_KEYS = {"A": ACCOUNT_A, "B": ACCOUNT_B, "unattributed": _UNATTRIBUTED}
_SCOPE_STORED = {"A": {ACCOUNT_A}, "B": {ACCOUNT_B},
                 "unattributed": {None, _UNATTRIBUTED}}

_MODEL_MSG_COLUMNS = (
    "INSERT INTO conversation_messages "
    "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
    " blocks_json,model,msg_id,req_id,cwd,is_sidechain,account_key) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _scoped_model_message(sid, n, account, model, text, minute):
    return (sid, f"uuid-{sid}-{n}", f"{sid}.jsonl", 100 * n,
            "2026-08-04T10:%02d:00Z" % minute, "assistant", text, "[]",
            model, f"msg-{sid}-{n}", f"req-{sid}-{n}", "/work/models", 0,
            account)


def _scope_model_store(tmp_path: pathlib.Path,
                       account_key: str) -> sqlite3.Connection:
    """Open the file-backed #1073 store and scope it to ``account_key``."""
    conn = sqlite3.connect(tmp_path / "conversations.db")
    conn.execute("ATTACH DATABASE ? AS cache_db", (str(tmp_path / "cache.db"),))
    cache.scope_conversations_db_to_account(conn, account_key)
    return conn


def _open_scoped_model_fixture(tmp_path: pathlib.Path, account_key: str, *,
                               backfill_pending: bool = False
                               ) -> sqlite3.Connection:
    cache_conn = sqlite3.connect(tmp_path / "cache.db")
    db._apply_cache_schema(cache_conn)
    cache_conn.commit()
    cache_conn.close()

    conn = sqlite3.connect(tmp_path / "conversations.db")
    db._apply_conversations_schema(conn)
    rows = []
    minute = 0
    for sid, messages in _SCOPED_MODEL_ROWS:
        for n, (account, model, text) in enumerate(messages, start=1):
            rows.append(_scoped_model_message(sid, n, account, model, text,
                                              minute))
            minute += 1
    conn.executemany(_MODEL_MSG_COLUMNS, rows)
    conn.execute("ATTACH DATABASE ? AS cache_db", (str(tmp_path / "cache.db"),))
    cache._recompute_conversation_sessions(conn)
    conn.commit()
    if backfill_pending:
        conn.execute(
            "INSERT OR REPLACE INTO main.cache_meta(key, value) "
            "VALUES('conversation_sessions_backfill_pending', '1')")
        conn.commit()
        assert claude_query._rollup_authoritative(conn) is False
    else:
        assert claude_query._rollup_authoritative(conn) is True
    conn.close()
    return _scope_model_store(tmp_path, account_key)


def _stored_account_by_uuid():
    return {
        f"uuid-{sid}-{n}": account
        for sid, messages in _SCOPED_MODEL_ROWS
        for n, (account, _model, _text) in enumerate(messages, start=1)
    }


def _browse_sessions(conn, families):
    page = claude_query.list_conversations(conn, limit=200, models=families)
    return {row["session_id"] for row in page["conversations"]}


def _model_facet_count(conn, family):
    facets = claude_query.list_conversation_facets(conn)
    counts = {entry["family"]: entry["count"] for entry in facets["models"]}
    return counts.get(family, 0)


def _assert_scoped_search(conn, scope, families, hits):
    result = claude_query.search_conversations(
        conn, "zephyr", limit=200, models=families, fts_available=False)
    assert {hit["session_id"] for hit in result["hits"]} == hits
    stored = _stored_account_by_uuid()
    for hit in result["hits"]:
        assert stored[hit["uuid"]] in _SCOPE_STORED[scope], hit


_SCOPED_MODEL_CASES = [
    ("A", ["opus"], {"a1", "s2", "m1"}, {"a1"}),
    ("A", ["sonnet"], {"a2", "a3", "s1"}, {"a2", "s1"}),
    ("A", ["haiku"], {"a2"}, {"a2"}),
    ("A", ["opus", "haiku"], {"a1", "a2", "s2", "m1"}, {"a1", "a2"}),
    ("B", ["opus"], {"b1", "s1"}, {"b1", "s1"}),
    ("B", ["haiku"], {"b2", "s2"}, {"b2", "s2"}),
    ("B", ["sonnet"], set(), set()),
    ("unattributed", ["opus"], {"n1"}, {"n1"}),
    ("unattributed", ["haiku"], {"u1"}, {"u1"}),
    ("unattributed", ["sonnet"], {"m1"}, {"m1"}),
    ("unattributed", ["fable"], {"m1"}, {"m1"}),
]


@pytest.mark.parametrize("scope,families,browse,hits", _SCOPED_MODEL_CASES)
def test_scoped_model_chip_matches_in_scope_families(
        tmp_path, scope, families, browse, hits):
    """Accuracy (#1073): under an account focus a model chip must answer, not
    raise ``no such index`` through the scoper's TEMP view, and must match a
    session only through its in-scope messages: a shared session's other
    half, or the other unattributed spelling's family, must never leak a
    session or a hit into the scope. The chip's session set must equal the
    scoped facet count, so the popover number matches the rows it yields."""
    conn = _open_scoped_model_fixture(tmp_path, _SCOPE_KEYS[scope])
    try:
        assert _browse_sessions(conn, families) == browse
        if len(families) == 1:
            assert _model_facet_count(conn, families[0]) == len(browse)
        _assert_scoped_search(conn, scope, families, hits)
    finally:
        conn.close()


@pytest.mark.parametrize("scope,families,browse,hits", [
    case for case in _SCOPED_MODEL_CASES
    if case[0] in ("A", "B") and case[1] == ["opus"]
])
def test_scoped_model_chip_on_live_branch(tmp_path, scope, families, browse,
                                          hits):
    """Accuracy (#1073, R6): while ``main``'s rollup is not authoritative,
    browse takes the live GROUP BY branch and search takes its live
    prefilter. A scoped model chip must still answer with the in-scope
    sessions there, not raise or fall back to another account's models."""
    conn = _open_scoped_model_fixture(tmp_path, _SCOPE_KEYS[scope],
                                      backfill_pending=True)
    try:
        assert claude_query._rollup_authoritative(conn) is False
        assert _browse_sessions(conn, families) == browse
        _assert_scoped_search(conn, scope, families, hits)
    finally:
        conn.close()


def test_scoped_model_filter_and_facets_share_the_setup_snapshot(tmp_path):
    """Accuracy (#1073, D4): on one scoped connection the opus chip and the
    opus facet count must come from the same setup snapshot, so a message
    committed by another writer after setup cannot make the facet promise a
    session the chip does not return. A freshly scoped connection must see
    the committed message in both."""
    conn = _open_scoped_model_fixture(tmp_path, ACCOUNT_A)
    try:
        writer = sqlite3.connect(tmp_path / "conversations.db")
        try:
            writer.execute(_MODEL_MSG_COLUMNS, _scoped_model_message(
                "a3", 2, ACCOUNT_A, "claude-opus-4-6", "late opus reply", 59))
            writer.commit()
        finally:
            writer.close()
        assert _browse_sessions(conn, ["opus"]) == {"a1", "s2", "m1"}
        assert _model_facet_count(conn, "opus") == 3
    finally:
        conn.close()

    fresh = _scope_model_store(tmp_path, ACCOUNT_A)
    try:
        assert _browse_sessions(fresh, ["opus"]) == {"a1", "a3", "s2", "m1"}
        assert _model_facet_count(fresh, "opus") == 4
    finally:
        fresh.close()


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


def test_scoped_codex_anon_label_follows_the_thread_cwd(tmp_path):
    """#972 §5.8 rule 6. A shared conversation whose thread cwd (and so its
    rollup label) belongs to another account, while this account's own
    session_meta for it names an owned path: the label is not registered for
    that owned path, so another account's project word never enters this
    account's plan. The owned path itself still does."""
    from _lib_conversation_anon import plan_to_wire

    conn = _open_scoped_codex_fixture(tmp_path, ACCOUNT_A, scope=False)
    alice_path = "/Users/alice/account-a-project"
    bravo_path = "/Users/bravo/foreign-word"
    try:
        conn.execute(
            "UPDATE cache_db.codex_conversation_threads SET cwd=? "
            "WHERE conversation_key='v1.shared'", (bravo_path,),
        )
        conn.execute(
            "UPDATE main.codex_conversation_rollups SET project_label=? "
            "WHERE conversation_key='v1.shared'", ("foreign-word",),
        )
        for offset, path, account in ((0, alice_path, ACCOUNT_A),
                                      (1, bravo_path, ACCOUNT_B)):
            conn.execute(
                "INSERT INTO main.codex_conversation_events "
                "(source_path,line_offset,source_root_key,conversation_key,"
                "native_thread_id,root_thread_id,record_type,payload_json,account_key) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("shared-codex.jsonl", offset, "root", "v1.shared",
                 "shared-thread", "shared-thread", "session_meta",
                 json.dumps({"type": "session_meta", "payload": {"cwd": path}}),
                 account),
            )
        conn.commit()
        cache.scope_conversations_db_to_account(conn, ACCOUNT_A)
        assert conn.execute(
            "SELECT project_label FROM temp.codex_conversation_rollups "
            "WHERE conversation_key='v1.shared'").fetchone() == ("foreign-word",)
        owned, _ambiguous, _undecodable = claude_query._scoped_codex_anon_paths(
            conn, ACCOUNT_A)
        assert owned == {alice_path: frozenset()}
        result = claude_query.build_anon_plan_for_sources(
            conn, home_dir="/Users/alice", sources={"codex"},
        )
        wire = json.dumps(plan_to_wire(result.plan))
        assert alice_path in wire
        assert "foreign-word" not in wire
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
            {}, 0, 0,
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
    # Transcripts are kept regardless of age: docs/fixtures-gotchas.md,
    # "Conversation retention is off in test environments (#965)".
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
    # Transcripts are kept regardless of age: docs/fixtures-gotchas.md,
    # "Conversation retention is off in test environments (#965)".
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
    # Transcripts are kept regardless of age: docs/fixtures-gotchas.md,
    # "Conversation retention is off in test environments (#965)".
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


@pytest.fixture
def store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
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


# --- #944 spec 5.8: the conversations twin's fresh case ----------------------
#
# A record first observed now takes its write-time account, read from the
# attached cache: the accounting decision, else (while 050 is pending) the
# accounting row's stamp, else the shared kernel over the attached timeline.
# Each case pins the active identity to "acct-b", so the pre-#944 behaviour
# (the conversations-ingest identity) is the failing value.

def _t944_cache(ns):
    return sqlite3.connect(ns["_cctally_core"].CACHE_DB_PATH)


def _t944_obs(cache_conn, at, account):
    import _lib_claude_attribution as K
    cache_conn.execute(
        "INSERT INTO claude_account_timeline (captured_at_utc, precision_us,"
        " account_key, journal_segment, journal_offset) VALUES (?,?,?,?,?)",
        (K.iso_from_us(K.us_from_iso(at)), 1_000_000, account,
         "observations-2026-06.jsonl", 0))


def _t944_sync(ns, conn, projects, ts="2026-06-01T00:00:00Z"):
    a = projects / "a.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one", ts=ts))
    ns["sync_claude_conversations"](conn)
    (account,) = _s3_accounts(conn).values()
    assert [row[4] for row in _s3_stamps(conn)] == [account]
    return account


def test_a_fresh_record_consumes_the_accounting_decision(store, monkeypatch):
    """Detects: a conversation message filed under the conversations-ingest
    identity when accounting already decided the record's account."""
    import _lib_claude_attribution as K
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-b")
    cache_conn = _t944_cache(ns)
    try:
        cache_conn.execute(
            "INSERT INTO claude_cost_decisions (decision_key, form, msg_id,"
            " req_id, account_key, basis, rule, decided_at_utc,"
            " journal_segment, journal_offset) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (K.keyed_key("m1", "r1"), K.FORM_KEYED, "m1", "r1", "acct-dec",
             K.BASIS_FALLBACK, K.RULE_VERSION, "2026-06-01T00:00:01Z",
             "observations-2026-06.jsonl", 0))
        cache_conn.commit()
    finally:
        cache_conn.close()
    assert _t944_sync(ns, conn, projects) == "acct-dec"


@pytest.mark.parametrize("msg_id,req_id", [("m1", ""), (7, "r1")],
                         ids=["empty-requestId", "numeric-message-id"])
def test_an_invalid_id_record_finds_the_physical_decision(
        store, monkeypatch, msg_id, req_id):
    """#944 F1: accounting identifies a record whose id is empty or not a
    string by its bytes (physical form). Detects: the twin keying the same
    record on the invalid ids, missing accounting's decision and filing the
    message under the conversations-ingest identity instead."""
    import _lib_claude_attribution as K
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-b")
    line = _s3_asst_line("u1", msg_id, req_id, "one")
    raw = line.rstrip("\n").encode("utf-8")
    loc = K.loc_for(str(projects / "a.jsonl"))
    cache_conn = _t944_cache(ns)
    try:
        cache_conn.execute(
            "INSERT INTO claude_cost_decisions (decision_key, form, loc,"
            " byte_offset, digest, account_key, basis, rule, decided_at_utc,"
            " journal_segment, journal_offset)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (K.physical_key(loc, 0, raw), K.FORM_PHYSICAL, loc, 0,
             K.record_sha256(raw), "acct-dec", K.BASIS_FALLBACK,
             K.RULE_VERSION, "2026-06-01T00:00:01Z",
             "observations-2026-06.jsonl", 0))
        cache_conn.commit()
    finally:
        cache_conn.close()
    (projects / "a.jsonl").write_text(line)
    ns["sync_claude_conversations"](conn)
    assert list(_s3_accounts(conn).values()) == ["acct-dec"]


@pytest.mark.parametrize("export", ["pending", "applied"])
def test_a_fresh_record_takes_the_row_stamp_only_while_050_is_pending(
        store, monkeypatch, export):
    """Detects: a message ignoring an upgraded store's existing accounting
    stamp before 050 has exported it, or consulting that stamp after 050 has
    made the decisions authoritative."""
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-b")
    cache_conn = _t944_cache(ns)
    try:
        cache_conn.execute(
            "INSERT INTO session_entries (source_path, line_offset,"
            " timestamp_utc, model, msg_id, req_id, input_tokens,"
            " output_tokens, cache_create_tokens, cache_read_tokens,"
            " account_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(projects / "a.jsonl"), 0, "2026-06-01T00:00:00Z",
             "claude-opus-4-8", "m1", "r1", 10, 5, 0, 0, "acct-row"))
        if export == "pending":
            cache_conn.execute(
                "DELETE FROM schema_migrations WHERE name="
                "'050_preserve_claude_cost_attribution'")
        cache_conn.commit()
    finally:
        cache_conn.close()
    expected = "acct-row" if export == "pending" else "acct-b"
    assert _t944_sync(ns, conn, projects) == expected


@pytest.mark.parametrize("ts,expected", [
    ("2026-06-01T00:00:00Z", "acct-a"),
    # Later than the resolution clock: contradicts the write order, so it
    # falls back (clock_order) despite A on both sides.
    ("2099-06-01T00:00:00Z", "acct-b"),
])
def test_a_fresh_record_resolves_through_the_attached_timeline(
        store, monkeypatch, ts, expected):
    """Detects: a delayed conversation ingest filing a message written while
    A was active under the account active at ingest (B), or a timestamp that
    contradicts the write order being attributed from its neighbours."""
    import _lib_claude_attribution as K
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-b")
    at = K.us_from_iso(ts)
    cache_conn = _t944_cache(ns)
    try:
        _t944_obs(cache_conn, K.iso_from_us(at - 60_000_000), "acct-a")
        _t944_obs(cache_conn, K.iso_from_us(at + 60_000_000), "acct-a")
        cache_conn.commit()
    finally:
        cache_conn.close()
    assert _t944_sync(ns, conn, projects, ts=ts) == expected


def test_an_unavailable_cache_attachment_falls_back_to_the_ingest_identity():
    """Detects: a conversation ingest failing, or guessing an account, when
    the cache is not attached."""
    conn = sqlite3.connect(":memory:")
    try:
        fresh = cache._ConversationFreshAccount(conn, "acct-b")
        assert not fresh.usable
        mrow = SimpleNamespace(msg_id="m1", req_id="r1", byte_offset=0,
                               timestamp_utc="2026-06-01T00:00:00Z")
        assert fresh.resolve(mrow, b"{}\n", "a.jsonl") == "acct-b"
    finally:
        conn.close()


def test_the_fresh_account_memo_holds_only_the_current_file(store,
                                                            monkeypatch):
    """The conversations resolver's timeline-neighbour memo is scoped to the
    source file being ingested. Detects: a memo that grew with every interval
    one conversations ingest resolved, so a cold ingest of a large history
    held every record's neighbours in memory at once."""
    import _cctally_cache as cc
    ns, conn, projects = store
    _s3_pin_account(monkeypatch, ns, "acct-b")
    (projects / "a.jsonl").write_text(
        _s3_asst_line("u1", "m1", "r1", "one", ts="2026-06-01T00:00:00Z"))
    (projects / "b.jsonl").write_text(
        _s3_asst_line("u2", "m2", "r2", "two", sid="s2",
                      ts="2026-06-02T00:00:00Z"))
    real = cc._ConversationFreshAccount.resolve
    seen = []

    def spy(self, mrow, raw, source_path):
        out = real(self, mrow, raw, source_path)
        seen.append((pathlib.Path(source_path).name, set(self.memo)))
        return out
    monkeypatch.setattr(cc._ConversationFreshAccount, "resolve", spy)
    ns["sync_claude_conversations"](conn)
    assert sorted(name for name, _keys in seen) == ["a.jsonl", "b.jsonl"]
    first, second = seen[0][1], seen[1][1]
    assert first and second
    assert not first & second


@pytest.mark.parametrize("bounds", [None, (1, 1)],
                         ids=["batched", "one-file-control"])
def test_claude_decision_committed_between_preparations_is_stamped(
        store, monkeypatch, bounds):
    """#939 D1: an accounting decision committed after file A was prepared and
    before file B is the one B's stamp names, in a batch exactly as in the
    one-file control. Detects: a batched pass reading accounting state from a
    snapshot older than the file it prepares, which files the record under
    the ingest identity although accounting had decided it."""
    import _lib_claude_attribution as K
    import _cctally_cache as cc
    ns, conn, projects = store
    if bounds is not None:
        monkeypatch.setattr(cc, "CLAUDE_CONVERSATION_BATCH_FILES", bounds[0])
        monkeypatch.setattr(cc, "CLAUDE_CONVERSATION_BATCH_BYTES", bounds[1])
    _s3_pin_account(monkeypatch, ns, "acct-b")
    a = projects / "a.jsonl"
    b = projects / "b.jsonl"
    a.write_text(_s3_asst_line("u1", "m1", "r1", "one"))
    b.write_text(_s3_asst_line("u2", "m2", "r2", "two", sid="s2"))
    real = cc._iter_sync_entries

    def walking(fh, path_str, *args, **kwargs):
        if path_str == str(b):
            cache_conn = _t944_cache(ns)
            try:
                cache_conn.execute(
                    "INSERT INTO claude_cost_decisions (decision_key, form,"
                    " msg_id, req_id, account_key, basis, rule, decided_at_utc,"
                    " journal_segment, journal_offset)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (K.keyed_key("m2", "r2"), K.FORM_KEYED, "m2", "r2",
                     "acct-dec", K.BASIS_FALLBACK, K.RULE_VERSION,
                     "2026-06-01T00:00:01Z", "observations-2026-06.jsonl", 0))
                cache_conn.commit()
            finally:
                cache_conn.close()
        yield from real(fh, path_str, *args, **kwargs)
    monkeypatch.setattr(cc, "_iter_sync_entries", walking)
    ns["sync_claude_conversations"](conn)
    assert _s3_accounts(conn) == {(str(a), 0): "acct-b", (str(b), 0): "acct-dec"}
    assert {row[0]: row[4] for row in _s3_stamps(conn)} == {
        str(a): "acct-b", str(b): "acct-dec"}


# --- #1062: account-scoped title search -------------------------------------
#
# A production-shaped corpus: every title row sits at its OWN byte offset (the
# parsers never emit a title and a message from one line), and every file has
# a cursor row whose `last_byte_offset` is past its last line. Before #1062 the
# scoped title view admitted a title only when a scoped message shared its
# exact `(source_path, byte_offset)`, so on this corpus every scope held zero
# titles, and the view had no `rowid`, so the title FTS join raised and
# degraded to LIKE.

from _fts5_gate import require_fts5  # noqa: E402 — the ONE FTS5 gate

_T_BRACKET = "Review [P1] findings for the tail poll"
# (file, owner, lines). A line is ("msg", session, account, text, ts) or
# ("title", session, title). Owners order the insertion (A first / B first) so
# the title rowids differ between the two orders.
_T_CORPUS = (
    ("a1.jsonl", "A", (
        ("msg", "a1", "A", "alpha opening a1", "2026-08-04T01:00:00Z"),
        ("title", "a1", _T_BRACKET),
        ("msg", "a1", "A", "alpha follow a1", "2026-08-04T01:01:00Z"))),
    ("b1.jsonl", "B", (
        ("msg", "b1", "B", "bravo opening b1", "2026-08-04T02:00:00Z"),
        ("title", "b1", _T_BRACKET),
        ("msg", "b1", "B", "bravo follow b1", "2026-08-04T02:01:00Z"))),
    ("a2.jsonl", "A", (
        ("msg", "a2", "A", "alpha opening a2", "2026-08-04T03:00:00Z"),
        ("title", "a2", "Alpha findings second"))),
    ("b2.jsonl", "B", (
        ("msg", "b2", "B", "bravo opening b2", "2026-08-04T04:00:00Z"),
        ("title", "b2", "Bravo findings second"))),
    # One account resumed across two files, the title in the second.
    ("ar-1.jsonl", "A", (
        ("msg", "ar", "A", "alpha opening ar", "2026-08-04T05:00:00Z"),)),
    ("ar-2.jsonl", "A", (
        ("msg", "ar", "A", "alpha resumed ar", "2026-08-04T05:10:00Z"),
        ("title", "ar", "Alpha resumed findings"))),
    # Mixed A/B in one file: the B message FOLLOWS the title.
    ("mx1.jsonl", "A", (
        ("msg", "mx1", "A", "alpha opening mx1", "2026-08-04T06:00:00Z"),
        ("title", "mx1", "Mixed findings in one file"),
        ("msg", "mx1", "B", "bravo opening mx1", "2026-08-04T06:01:00Z"))),
    # Mixed A/B across a resume: B's messages sit in a second file.
    ("mx2-a.jsonl", "A", (
        ("msg", "mx2", "A", "alpha opening mx2", "2026-08-04T07:00:00Z"),
        ("title", "mx2", "Mixed findings across files"))),
    ("mx2-b.jsonl", "B", (
        ("msg", "mx2", "B", "bravo opening mx2", "2026-08-04T07:05:00Z"),)),
    ("an.jsonl", "A", (
        ("msg", "an", "A", "alpha opening an", "2026-08-04T08:00:00Z"),
        ("title", "an", "Alpha null findings"),
        ("msg", "an", None, "unattributed follow an", "2026-08-04T08:01:00Z"))),
    ("nn.jsonl", "N", (
        ("msg", "nn", None, "unattributed opening nn", "2026-08-04T09:00:00Z"),
        ("title", "nn", "Unattributed findings"))),
    ("orph.jsonl", "N", (("title", "orph", "Orphan findings"),)),
)
_T_ACCOUNTS = {"A": ACCOUNT_A, "B": ACCOUNT_B}
_T_ELIGIBLE = {ACCOUNT_A: {"a1", "a2", "ar"}, ACCOUNT_B: {"b1", "b2"},
               "unattributed": {"nn"}}
_T_MIXED = ("mx1", "mx2", "an")
_T_LINE = 100   # every line, title or message, starts at its own offset


def _t_build(tmp_path, order):
    """Seed the corpus into a file-backed store in ``order`` and return the
    store paths. The rollup is recomputed so the unscoped rail has rows."""
    cache_path = tmp_path / "cache.db"
    cache_conn = sqlite3.connect(cache_path)
    db._apply_cache_schema(cache_conn)
    cache_conn.commit()
    cache_conn.close()
    path = tmp_path / "conversations.db"
    conn = sqlite3.connect(path)
    db._apply_conversations_schema(conn)
    rank = {owner: i for i, owner in enumerate(order)}
    files = sorted(_T_CORPUS, key=lambda f: rank[f[1]])
    for name, _owner, lines in files:
        for index, line in enumerate(lines):
            offset = index * _T_LINE
            if line[0] == "msg":
                _kind, sid, acct, text, ts = line
                conn.execute(
                    "INSERT INTO conversation_messages "
                    "(session_id,uuid,source_path,byte_offset,timestamp_utc,"
                    " entry_type,text,blocks_json,model,msg_id,req_id,cwd,"
                    " is_sidechain,account_key) "
                    "VALUES(?,?,?,?,?,'human',?,'[]',NULL,NULL,NULL,?,0,?)",
                    (sid, f"{name}:{offset}", name, offset, ts, text,
                     "/work/proj", _T_ACCOUNTS.get(acct)))
            else:
                _kind, sid, title = line
                conn.execute(
                    "INSERT INTO conversation_ai_titles"
                    "(session_id,ai_title,source_path,byte_offset) "
                    "VALUES(?,?,?,?)", (sid, title, name, offset))
        conn.execute(
            "INSERT INTO conversation_source_files"
            "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at) "
            "VALUES(?,?,1,?,'2026-08-04T10:00:00Z')",
            (name, len(lines) * _T_LINE, len(lines) * _T_LINE))
    conn.execute("ATTACH DATABASE ? AS cache_db", (str(cache_path),))
    cache._recompute_conversation_sessions(conn)
    conn.commit()
    conn.close()
    return path, cache_path


def _t_open(store_paths, account_key=None):
    path, cache_path = store_paths
    conn = sqlite3.connect(path)
    conn.execute("ATTACH DATABASE ? AS cache_db", (str(cache_path),))
    if account_key is not None:
        cache.scope_conversations_db_to_account(conn, account_key)
    return conn


def _t_title_hits(conn, query, **kw):
    res = claude_query.search_conversations(conn, query, kind="title", **kw)
    return res, [hit["session_id"] for hit in res["hits"]]


def _t_matched(hit):
    return [part["text"] for part in hit["snippet_parts"] if part["match"]]


_T_ORDERS = pytest.mark.parametrize(
    "order", [("A", "B", "N"), ("B", "A", "N")], ids=["a-first", "b-first"])


@_T_ORDERS
def test_1062_scoped_title_view_exposes_the_base_rowid(tmp_path, order):
    """T1. Detects the scoped title view without a `rowid` column (the title
    FTS join raises `no such column: t.rowid` and silently degrades to LIKE),
    or a view rowid that is not the base row's, which pairs an FTS hit with
    another session's title."""
    require_fts5()
    store_paths = _t_build(tmp_path, order)
    conn = _t_open(store_paths, ACCOUNT_A)
    try:
        direct = claude_query._search_title(conn, "findings", 50, 0, True)
        assert direct["mode"] == "fts"
        assert {h["session_id"] for h in direct["hits"]} == _T_ELIGIBLE[ACCOUNT_A]
        rows = conn.execute(
            "SELECT rowid, session_id FROM conversation_ai_titles").fetchall()
        assert {sid for _rowid, sid in rows} == _T_ELIGIBLE[ACCOUNT_A]
        for rowid, sid in rows:
            assert rowid == conn.execute(
                "SELECT rowid FROM main.conversation_ai_titles "
                "WHERE session_id=?", (sid,)).fetchone()[0]
    finally:
        conn.close()


@_T_ORDERS
@pytest.mark.parametrize(
    "account", [ACCOUNT_A, ACCOUNT_B, "unattributed"], ids=["A", "B", "none"])
def test_1062_scoped_title_search_is_fts_with_prefix_highlights_and_order(
        tmp_path, order, account):
    """T2. Detects account-filtered title search degrading to the LIKE path
    (no prefix match, no highlight), returning another order than the
    unscoped search restricted to the account, or marking the literal `[P1]`
    brackets instead of only the FTS match (#1036)."""
    require_fts5()
    store_paths = _t_build(tmp_path, order)
    unscoped = _t_open(store_paths)
    scoped = _t_open(store_paths, account)
    try:
        _ref, everyone = _t_title_hits(unscoped, "findings")
        owners = {sid: acct for acct, sids in _T_ELIGIBLE.items()
                  for sid in sids}
        real = [owners[s] for s in everyone
                if owners.get(s) in (ACCOUNT_A, ACCOUNT_B)]
        assert sum(x != y for x, y in zip(real, real[1:])) >= 2, (
            "the unscoped order must interleave the accounts, or the order "
            "equality below proves nothing")
        eligible = _T_ELIGIBLE[account]
        res, sids = _t_title_hits(scoped, "findings")
        assert res["mode"] == "fts"
        assert res["total"] == len(eligible)
        assert set(sids) == eligible
        assert sids == [s for s in everyone if s in eligible]
        if account == "unattributed":
            return
        bracket_sid = "a1" if account == ACCOUNT_A else "b1"
        bracket = next(h for h in res["hits"] if h["session_id"] == bracket_sid)
        assert _t_matched(bracket) == ["findings"]
        assert any("[P1]" in p["text"] and not p["match"]
                   for p in bracket["snippet_parts"])

        prefix, prefix_sids = _t_title_hits(scoped, "findi")
        assert prefix["mode"] == "fts"
        assert prefix_sids == sids

        two, two_sids = _t_title_hits(scoped, "review find")
        assert two["mode"] == "fts"
        assert two_sids == [bracket_sid]
        assert _t_matched(two["hits"][0]) == ["Review", "findings"]
    finally:
        unscoped.close()
        scoped.close()


@_T_ORDERS
@pytest.mark.parametrize(
    "account", [ACCOUNT_A, ACCOUNT_B, "unattributed"], ids=["A", "B", "none"])
def test_1062_mixed_and_foreign_titles_never_cross_a_scope(
        tmp_path, order, account):
    """T3. Detects cross-account AI-title leakage: a mixed session's title
    (another account's message after the title, in a resumed file, or a NULL
    row) admitted into a scope, another account's sessions returned, or the
    scoped fallback title computed from messages outside the scope."""
    store_paths = _t_build(tmp_path, order)
    unscoped = _t_open(store_paths)
    scoped = _t_open(store_paths, account)
    try:
        ai_titles = dict(unscoped.execute(
            "SELECT session_id, ai_title FROM conversation_ai_titles"))
        _ref, everyone = _t_title_hits(unscoped, "findings",
                                       fts_available=False)
        assert set(_T_MIXED) <= set(everyone), (
            "the mixed sessions' titles must match the query unscoped")
        foreign = set(everyone) - _T_ELIGIBLE[account]
        assert foreign >= set(_T_MIXED)
        for fts in (None, False):
            _res, sids = _t_title_hits(scoped, "findings", fts_available=fts)
            assert set(sids) <= _T_ELIGIBLE[account]
            assert not set(sids) & foreign
        rail = {row["session_id"]: row["title"] for row in
                claude_query.list_conversations(scoped)["conversations"]}
        held = {row[0] for row in scoped.execute(
            "SELECT DISTINCT session_id FROM conversation_messages")}
        assert held, "the scope must hold messages"
        fallback = claude_query._session_first_prompt_titles_map(
            scoped, list(_T_MIXED))
        for sid in _T_MIXED:
            if sid in held:
                expected = fallback[sid]
                assert expected and expected != ai_titles[sid]
                assert rail[sid] == expected
                detail = claude_query.get_conversation(scoped, sid)
                assert detail["title"] == expected
                exported = claude_query.get_conversation_export(
                    scoped, sid, "all")
                assert exported.startswith(f"# {expected}")
                assert ai_titles[sid] not in exported
            else:
                assert sid not in rail
                assert claude_query.get_conversation(scoped, sid) is None
        if account == ACCOUNT_A:
            assert held >= set(_T_MIXED)
            assert fallback["mx1"].startswith("alpha")
        elif account == ACCOUNT_B:
            assert {"mx1", "mx2"} <= held and "an" not in held
            assert fallback["mx1"].startswith("bravo")
        assert "orph" not in rail
        for sid in held - _T_ELIGIBLE[account]:
            assert rail[sid] != ai_titles.get(sid)
    finally:
        unscoped.close()
        scoped.close()


def test_1062_eligible_ai_title_reaches_every_scoped_consumer(tmp_path):
    """T4. Detects an eligible single-account session losing its AI title in
    the scoped rail, the reader detail, a message-search hit's title or the
    export header (the #347 scoped surfaces show only fallback titles)."""
    require_fts5()
    store_paths = _t_build(tmp_path, ("A", "B", "N"))
    conn = _t_open(store_paths, ACCOUNT_A)
    try:
        rail = {row["session_id"]: row["title"] for row in
                claude_query.list_conversations(conn)["conversations"]}
        assert rail["a1"] == _T_BRACKET
        assert claude_query.get_conversation(conn, "a1")["title"] == _T_BRACKET
        hits = claude_query.search_conversations(
            conn, "alpha opening a1")["hits"]
        a1_hits = [h for h in hits if h["session_id"] == "a1"]
        assert a1_hits and all(h["title"] == _T_BRACKET for h in a1_hits)
        exported = claude_query.get_conversation_export(conn, "a1", "all")
        assert exported.startswith(f"# {_T_BRACKET}")
    finally:
        conn.close()


def test_1062_a_foreign_message_withdraws_an_admitted_title(tmp_path):
    """T5. Detects eligibility materialized at scope setup: a foreign message
    ingested after the title was admitted must remove it on the next read."""
    require_fts5()
    store_paths = _t_build(tmp_path, ("A", "B", "N"))
    conn = _t_open(store_paths, ACCOUNT_A)
    try:
        assert "a2" in {row[0] for row in conn.execute(
            "SELECT session_id FROM conversation_ai_titles")}
        assert "a2" in _t_title_hits(conn, "findings")[1]
        conn.execute(
            "INSERT INTO main.conversation_messages "
            "(session_id,uuid,source_path,byte_offset,timestamp_utc,"
            " entry_type,text,blocks_json,is_sidechain,account_key) "
            "VALUES('a2','late-b','late-b.jsonl',0,'2026-08-04T03:30:00Z',"
            "'human','bravo late a2','[]',0,?)", (ACCOUNT_B,))
        conn.commit()
        assert "a2" not in {row[0] for row in conn.execute(
            "SELECT session_id FROM conversation_ai_titles")}
        res, sids = _t_title_hits(conn, "findings")
        assert "a2" not in sids
        assert res["total"] == len(_T_ELIGIBLE[ACCOUNT_A]) - 1
    finally:
        conn.close()


@_T_ORDERS
def test_1062_scoped_title_like_path_returns_the_same_eligible_set(
        tmp_path, order):
    """T6. Detects the scoped LIKE fallback (no FTS5) returning nothing or a
    different set than the FTS path, or inventing a highlight."""
    store_paths = _t_build(tmp_path, order)
    conn = _t_open(store_paths, ACCOUNT_A)
    try:
        res, sids = _t_title_hits(conn, "findings", fts_available=False)
        assert res["mode"] == "like"
        assert set(sids) == _T_ELIGIBLE[ACCOUNT_A]
        assert res["total"] == len(_T_ELIGIBLE[ACCOUNT_A])
        for hit in res["hits"]:
            assert len(hit["snippet_parts"]) == 1
            assert hit["snippet_parts"][0]["match"] is False
    finally:
        conn.close()


def test_1062_a_title_ahead_of_its_file_cursor_is_withheld(tmp_path):
    """T7. Detects a title the message walk has not yet covered (a backfill
    ahead of the walk) being admitted: the messages before it in its file are
    not all retained, so the scope cannot judge its provenance."""
    store_paths = _t_build(tmp_path, ("A", "B", "N"))
    conn = _t_open(store_paths, ACCOUNT_A)

    def scoped_titles():
        return {row[0] for row in conn.execute(
            "SELECT session_id FROM conversation_ai_titles")}

    def set_cursor(value):
        conn.execute(
            "UPDATE main.conversation_source_files SET last_byte_offset=? "
            "WHERE path='a2.jsonl'", (value,))
        conn.commit()

    try:
        title_offset = conn.execute(
            "SELECT byte_offset FROM main.conversation_ai_titles "
            "WHERE session_id='a2'").fetchone()[0]
        assert "a2" in scoped_titles()
        set_cursor(title_offset)
        assert "a2" not in scoped_titles()
        assert "a2" not in _t_title_hits(conn, "findings",
                                         fts_available=False)[1]
        set_cursor(title_offset - 1)
        assert "a2" not in scoped_titles()
        set_cursor(title_offset + 1)
        assert "a2" in scoped_titles()
    finally:
        conn.close()


# --- #1068 D1: the sargable title clause -------------------------------------

#: Extra title-bearing sessions for both unattributed spellings, appended to
#: the #1062 corpus after it is built so the #1062 eligibility sets above stay
#: exactly as they are. (session, [(account_key, text)], title)
_T6_EXTRA = (
    ("lit", [("unattributed", "literal opening lit")], "Literal findings"),
    ("nl", [(None, "null opening nl"), ("unattributed", "literal follow nl")],
     "Null and literal findings"),
    ("al", [(ACCOUNT_A, "alpha opening al"),
            ("unattributed", "literal follow al")], "Alpha literal findings"),
)


def _t6_seed_extra(store_paths):
    path, _cache_path = store_paths
    conn = sqlite3.connect(path)
    try:
        for sid, lines, title in _T6_EXTRA:
            name = f"{sid}.jsonl"
            offset = 0
            for account, text in lines:
                conn.execute(
                    "INSERT INTO conversation_messages "
                    "(session_id,uuid,source_path,byte_offset,timestamp_utc,"
                    " entry_type,text,blocks_json,cwd,is_sidechain,account_key) "
                    "VALUES(?,?,?,?,'2026-08-04T11:00:00Z','human',?,'[]',"
                    "'/work/proj',0,?)",
                    (sid, f"{name}:{offset}", name, offset, text, account))
                offset += _T_LINE
            conn.execute(
                "INSERT INTO conversation_ai_titles"
                "(session_id,ai_title,source_path,byte_offset) VALUES(?,?,?,?)",
                (sid, title, name, offset))
            offset += _T_LINE
            conn.execute(
                "INSERT INTO conversation_source_files"
                "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at) "
                "VALUES(?,?,1,?,'2026-08-04T11:00:00Z')", (name, offset, offset))
        conn.commit()
    finally:
        conn.close()


def _t6_not_exists_oracle(conn, account_key):
    """The #1062 title view as it stood before #1068: the COALESCE predicate
    and the NOT EXISTS third clause, over the base tables only."""
    pred = "COALESCE(m.account_key,'unattributed') = ?"
    return sorted(conn.execute(
        "SELECT t.rowid, t.session_id, t.ai_title "
        "FROM main.conversation_ai_titles t "
        "WHERE EXISTS (SELECT 1 FROM main.conversation_source_files f "
        "  WHERE f.path = t.source_path AND f.last_byte_offset > t.byte_offset) "
        f"AND EXISTS (SELECT 1 FROM main.conversation_messages m "
        f"  WHERE m.session_id = t.session_id AND m.source_path = t.source_path "
        f"  AND {pred}) "
        f"AND NOT EXISTS (SELECT 1 FROM main.conversation_messages m "
        f"  WHERE m.session_id = t.session_id AND NOT ({pred})) "
        "AND NOT EXISTS (SELECT 1 FROM main.cache_meta "
        "  WHERE key = 'conversation_rebuild_claude_pending')",
        (account_key, account_key)).fetchall())


@_T_ORDERS
@pytest.mark.parametrize(
    "account", [ACCOUNT_A, ACCOUNT_B, "unattributed"], ids=["A", "B", "none"])
def test_scoped_title_clause_equals_not_exists(tmp_path, order, account):
    """#1068 T6. Accuracy/privacy failure detected: the count-equality title
    clause (and the sargable predicates under it) admitting a title the
    NOT EXISTS form excluded, such as a session holding a NULL-account or a
    literal-'unattributed' row inside a real-key scope (a naive
    ``NOT (account_key = K)`` reads NULL as unknown and admits it), or
    dropping one it admitted, such as a session split across the two
    unattributed spellings in the unattributed scope."""
    store_paths = _t_build(tmp_path, order)
    _t6_seed_extra(store_paths)
    conn = _t_open(store_paths, account)
    try:
        expected = _t6_not_exists_oracle(conn, account)
        got = sorted(conn.execute(
            "SELECT rowid, session_id, ai_title FROM conversation_ai_titles"
        ).fetchall())
        assert got == expected
        admitted = {row[1] for row in got}
        if account == "unattributed":
            assert {"nn", "lit", "nl"} <= admitted, (
                "both unattributed spellings belong to the unattributed scope")
            assert "al" not in admitted
        else:
            assert not {"an", "al", "lit", "nl"} & admitted, (
                "a NULL or literal-'unattributed' row withdraws the title")
        if account == ACCOUNT_A:
            assert "a1" in admitted
    finally:
        conn.close()


# --- #1068 D4/D7: scoped reuse of verified stored rollup rows ---------------
#
# These stores are built by the real openers (both ledgers at head, both
# incarnation ids present), so a stored rollup row is certified by the writer
# and the scoper's reuse gate can pass. Rows are seeded by SQL so each session
# shape (pure, mixed, impure, unattributed spellings) is exact.


def _r_env(tmp_path, monkeypatch):
    """Head-schema stores with settled flags and both incarnation ids."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    core = ns["open_cache_db"]()
    try:
        ns["sync_cache"](core)
    finally:
        core.close()
    conn = ns["open_conversations_db"]()
    try:
        ns["sync_claude_conversations"](conn)
    finally:
        conn.close()
    return ns, ns["_cctally_core"], ns["_cctally_cache"]


def _r_raw(path):
    return sqlite3.connect(str(path))


def _r_set_counter(cache_conn, value):
    cache_conn.execute(
        "INSERT INTO cache_meta(key,value) "
        "VALUES('session_entries_mutation_seq',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))


def _r_insert_messages(conn, messages):
    """messages: (session_id, uuid, text, ts, msg_id, req_id, account_key)."""
    for sid, uuid, text, ts, msg_id, req_id, account in messages:
        conn.execute(
            "INSERT INTO conversation_messages "
            "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,"
            " text,blocks_json,model,msg_id,req_id,cwd,is_sidechain,account_key) "
            "VALUES(?,?,?,(SELECT COALESCE(MAX(byte_offset),0)+100 FROM "
            " conversation_messages WHERE source_path=?),?,?,?,'[]',?,?,?,"
            " '/work/proj',0,?)",
            (sid, uuid, f"{sid}.jsonl", f"{sid}.jsonl", ts,
             "assistant" if msg_id else "human", text,
             "claude-opus-4-8" if msg_id else None, msg_id, req_id, account))


def _r_insert_entries(cache_conn, entries):
    """entries: (msg_id, req_id, cost_usd_raw, account_key, mutation_seq)."""
    for msg_id, req_id, cost, account, seq in entries:
        cache_conn.execute(
            "INSERT INTO session_entries "
            "(source_path,line_offset,timestamp_utc,model,msg_id,req_id,"
            " input_tokens,output_tokens,cache_create_tokens,cache_read_tokens,"
            " cost_usd_raw,account_key,mutation_seq) "
            "VALUES('acct.jsonl',(SELECT COALESCE(MAX(line_offset),0)+10 "
            " FROM session_entries),'2026-08-04T00:00:00Z','claude-opus-4-8',"
            " ?,?,10,5,0,0,?,?,?)",
            (msg_id, req_id, cost, account, seq))


def _r_seed(core, cache, messages, entries):
    """Seed accounting then transcript rows and let the writer certify the
    whole rollup (a full recompute on the writer connection)."""
    cache_conn = _r_raw(core.CACHE_DB_PATH)
    try:
        _r_insert_entries(cache_conn, entries)
        seqs = [e[4] for e in entries if e[4] is not None]
        _r_set_counter(cache_conn, max(seqs, default=0))
        cache_conn.commit()
    finally:
        cache_conn.close()
    conn = cache.open_conversations_db()
    try:
        _r_insert_messages(conn, messages)
        assert cache._recompute_conversation_sessions(conn) is True
        conn.commit()
        uncertified = conn.execute(
            "SELECT session_id FROM conversation_sessions "
            "WHERE input_fingerprint IS NULL").fetchall()
        assert uncertified == [], "the writer certifies every seeded row"
    finally:
        conn.close()


def _r_scoped(cache, account):
    conn = cache.open_conversations_db_readonly()
    cache.scope_conversations_db_to_account(conn, account)
    return conn


def _r_detail(cache, lq, account, sid):
    conn = _r_scoped(cache, account)
    try:
        return lq.get_conversation(conn, sid)
    finally:
        conn.close()


_R_T3_MESSAGES = [
    ("pa", "pa-u1", "alpha question", "2026-08-04T01:00:00Z", None, None,
     ACCOUNT_A),
    ("pa", "pa-a1", "alpha answer one", "2026-08-04T01:01:00Z", "m1", "r1",
     ACCOUNT_A),
    ("pa", "pa-a2", "alpha answer two", "2026-08-04T01:02:00Z", "m2", "r2",
     ACCOUNT_A),
]
_R_T3_ENTRIES = [("m1", "r1", 1.5, ACCOUNT_A, 1),
                 ("m2", "r2", 2.25, ACCOUNT_A, 2)]


@pytest.mark.parametrize("change", ["append", "finalize", "move", "delete"])
def test_scoped_detail_sees_change_through_primed_memo(
        tmp_path, monkeypatch, change):
    """#1068 T1/T3. Accuracy failure detected: a scoped detail request served
    from a reused stored row or from the primed assembly memo after the
    focused account's inputs changed, with no writer pass between the two
    requests: a message append, an accounting finalization (stamped), an
    accounting row moved to another account (stamped), and an accounting
    delete that stamps nothing and advances no counter (the stale cost the
    delete case returned before D7)."""
    ns, core, cache = _r_env(tmp_path, monkeypatch)
    _r_seed(core, cache, _R_T3_MESSAGES, _R_T3_ENTRIES)
    lq = cache._load_lib("_lib_conversation_query")
    lq._assemble_memo_clear()
    try:
        first = _r_detail(cache, lq, ACCOUNT_A, "pa")
        assert first["cost_usd"] == 3.75
        cache_conn = _r_raw(core.CACHE_DB_PATH)
        conv_conn = _r_raw(core.CONVERSATIONS_DB_PATH)
        try:
            if change == "append":
                _r_insert_messages(conv_conn, [
                    ("pa", "pa-a3", "alpha late answer",
                     "2026-08-04T01:03:00Z", "m3", "r3", ACCOUNT_A)])
                _r_insert_entries(cache_conn, [("m3", "r3", 0.5, ACCOUNT_A, 3)])
                _r_set_counter(cache_conn, 3)
                expected = 4.25
            elif change == "finalize":
                cache_conn.execute(
                    "UPDATE session_entries SET cost_usd_raw=3.0, "
                    "mutation_seq=3 WHERE msg_id='m2'")
                _r_set_counter(cache_conn, 3)
                expected = 4.5
            elif change == "move":
                cache_conn.execute(
                    "UPDATE session_entries SET account_key=?, mutation_seq=3 "
                    "WHERE msg_id='m2'", (ACCOUNT_B,))
                _r_set_counter(cache_conn, 3)
                expected = 1.5
            else:
                cache_conn.execute(
                    "DELETE FROM session_entries WHERE msg_id='m2'")
                expected = 1.5
            cache_conn.commit()
            conv_conn.commit()
        finally:
            cache_conn.close()
            conv_conn.close()
        second = _r_detail(cache, lq, ACCOUNT_A, "pa")
        assert second["cost_usd"] == expected
        if change == "append":
            assert "alpha late answer" in str(second)
    finally:
        lq._assemble_memo_clear()


def _r_temp_rows(conn, table="conversation_sessions"):
    return conn.execute(
        f"SELECT rowid, * FROM temp.{table} ORDER BY rowid").fetchall()


def _r_open_scoped(cache, account, raw_paths=None):
    if raw_paths is None:
        return _r_scoped(cache, account)
    conv_path, cache_path = raw_paths
    conn = sqlite3.connect(str(conv_path))
    conn.execute("ATTACH DATABASE ? AS cache_db", (str(cache_path),))
    cache.scope_conversations_db_to_account(conn, account)
    return conn


def _r_observe(cache, monkeypatch, account, *, reuse=True, raw_paths=None):
    """One scoped setup: its TEMP rollup rows in rowid order, its browse
    body, and what the reuse gate and partition returned. ``reuse=False``
    forces the global gate closed (today's full derivation)."""
    seen = {"epoch": [], "partition": []}
    real_epoch = cache._claude_rollup_reuse_epoch
    real_partition = cache._scoped_claude_reuse_partition

    def epoch_spy(conn):
        out = real_epoch(conn) if reuse else None
        seen["epoch"].append(out)
        return out

    def partition_spy(conn, epoch):
        out = real_partition(conn, epoch)
        seen["partition"].append(out)
        return out

    lq = cache._load_lib("_lib_conversation_query")
    with monkeypatch.context() as m:
        m.setattr(cache, "_claude_rollup_reuse_epoch", epoch_spy)
        m.setattr(cache, "_scoped_claude_reuse_partition", partition_spy)
        conn = _r_open_scoped(cache, account, raw_paths)
        try:
            rows = _r_temp_rows(conn)
            browse = lq.list_conversations(conn)
        finally:
            conn.close()
    return rows, browse, seen


def _r_relax_mutation_seq(cache_path):
    """Let session_entries hold a NULL ``mutation_seq``, as a store whose
    column predates the NOT NULL form does (F11). Only the stored schema text
    changes; no row is rewritten."""
    import re
    conn = sqlite3.connect(str(cache_path))
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='session_entries'").fetchone()[0]
        relaxed, count = re.subn(
            r"mutation_seq(\s+)INTEGER NOT NULL DEFAULT 0",
            r"mutation_seq\1INTEGER DEFAULT 0", sql)
        assert count == 1
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_master SET sql=? "
            "WHERE type='table' AND name='session_entries'", (relaxed,))
        conn.execute(f"PRAGMA schema_version={version + 1}")
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()
    finally:
        conn.close()


_UNATTR = "unattributed"
_A, _B = ACCOUNT_A, ACCOUNT_B
# (session, uuid, text, ts, msg_id, req_id, account)
_R_T2_MESSAGES = [
    # pure A, with one turn split across two fragments (a repeated key).
    ("pa", "pa-u", "alpha pure question", "2026-08-04T01:00:00Z", None, None, _A),
    ("pa", "pa-1a", "alpha pure answer", "2026-08-04T01:01:00Z", "m-pa1", "r-pa1", _A),
    ("pa", "pa-1b", "alpha pure answer more", "2026-08-04T01:01:30Z", "m-pa1", "r-pa1", _A),
    ("pa", "pa-2", "alpha pure second", "2026-08-04T01:02:00Z", "m-pa2", "r-pa2", _A),
    ("pb", "pb-u", "bravo pure question", "2026-08-04T02:00:00Z", None, None, _B),
    ("pb", "pb-1", "bravo pure answer", "2026-08-04T02:01:00Z", "m-pb1", "r-pb1", _B),
    # mixed: messages of both accounts.
    ("mx", "mx-u", "alpha mixed question", "2026-08-04T03:00:00Z", None, None, _A),
    ("mx", "mx-1", "alpha mixed answer", "2026-08-04T03:01:00Z", "m-mx1", "r-mx1", _A),
    ("mx", "mx-2", "bravo mixed answer", "2026-08-04T03:02:00Z", "m-mx2", "r-mx2", _B),
    # impure A: every message A, one entry B.
    ("ia", "ia-u", "alpha impure question", "2026-08-04T04:00:00Z", None, None, _A),
    ("ia", "ia-1", "alpha impure answer", "2026-08-04T04:01:00Z", "m-ia1", "r-ia1", _A),
    ("ia", "ia-2", "alpha impure second", "2026-08-04T04:02:00Z", "m-ia2", "r-ia2", _A),
    # reverse-impure B: every message B, one entry A.
    ("rb", "rb-u", "bravo impure question", "2026-08-04T05:00:00Z", None, None, _B),
    ("rb", "rb-1", "bravo impure answer", "2026-08-04T05:01:00Z", "m-rb1", "r-rb1", _B),
    ("rb", "rb-2", "bravo impure second", "2026-08-04T05:02:00Z", "m-rb2", "r-rb2", _B),
    # one turn key held by two sessions.
    ("sh1", "sh1-u", "alpha shared question", "2026-08-04T06:00:00Z", None, None, _A),
    ("sh1", "sh1-1", "alpha shared answer", "2026-08-04T06:01:00Z", "m-sh", "r-sh", _A),
    ("sh2", "sh2-1", "alpha shared resumed", "2026-08-04T06:05:00Z", "m-sh", "r-sh", _A),
    ("sh2", "sh2-2", "alpha shared second", "2026-08-04T06:06:00Z", "m-sh2", "r-sh2", _A),
    # an entry whose mutation_seq is NULL.
    ("pn", "pn-u", "alpha null seq question", "2026-08-04T07:00:00Z", None, None, _A),
    ("pn", "pn-1", "alpha null seq answer", "2026-08-04T07:01:00Z", "m-pn", "r-pn", _A),
    # both unattributed spellings.
    ("nn", "nn-u", "null question", "2026-08-04T08:00:00Z", None, None, None),
    ("nn", "nn-1", "null answer", "2026-08-04T08:01:00Z", "m-nn", "r-nn", None),
    ("lt", "lt-1", "literal answer", "2026-08-04T09:01:00Z", "m-lt", "r-lt", _UNATTR),
    ("nl", "nl-u", "null literal question", "2026-08-04T10:00:00Z", None, None, None),
    ("nl", "nl-1", "null literal answer", "2026-08-04T10:01:00Z", "m-nl", "r-nl", _UNATTR),
]
# (msg_id, req_id, cost_usd_raw, account, mutation_seq)
_R_T2_ENTRIES = [
    ("m-pa1", "r-pa1", 1.0e16, _A, 1), ("m-pa2", "r-pa2", 1.0, _A, 2),
    ("m-pb1", "r-pb1", 2.5, _B, 3),
    ("m-mx1", "r-mx1", 0.75, _A, 4), ("m-mx2", "r-mx2", 1.25, _B, 5),
    ("m-ia1", "r-ia1", 0.5, _A, 6), ("m-ia2", "r-ia2", 0.25, _B, 7),
    ("m-rb1", "r-rb1", 3.5, _B, 8), ("m-rb2", "r-rb2", 4.5, _A, 9),
    ("m-sh", "r-sh", 0.125, _A, 10), ("m-sh2", "r-sh2", 0.0625, _A, 11),
    ("m-pn", "r-pn", 0.375, _A, None),
    ("m-nn", "r-nn", 1.5, None, 12), ("m-lt", "r-lt", 2.0, _UNATTR, 13),
    ("m-nl", "r-nl", 2.75, None, 14),
]
_R_T2_EXPECTED = {
    _A: ({"pa", "sh1", "sh2", "pn"}, {"mx", "ia"}),
    _B: ({"pb"}, {"mx", "rb"}),
    _UNATTR: ({"nn", "lt", "nl"}, set()),
}


def _r_ordered(rows, order, account_index):
    rank = {acct: i for i, acct in enumerate(order)}
    return sorted(rows, key=lambda r: rank.get(r[account_index], len(rank)))


@pytest.mark.parametrize("order", [(_A, _B), (_B, _A)], ids=["a-first", "b-first"])
def test_scoped_reuse_equals_forced_derivation(tmp_path, monkeypatch, order):
    """#1068 T2 (Claude). Privacy/accuracy failure detected: a reused stored
    row leaking another account's messages or cost into a scope (a mixed,
    impure or reverse-impure session reused), a scope missing one of the two
    unattributed spellings, an accounting count that double-counts a key held
    by two fragments or two sessions, or a reused TEMP table whose row order
    differs from the full derivation's. For every scope the TEMP rollup and
    the browse body equal those with reuse forced off, row for row in rowid
    order, and the spies prove which sessions were reused and derived."""
    ns, core, cache = _r_env(tmp_path, monkeypatch)
    _r_relax_mutation_seq(core.CACHE_DB_PATH)
    _r_seed(core, cache, _r_ordered(_R_T2_MESSAGES, order, 6),
            _r_ordered(_R_T2_ENTRIES, order, 3))
    for account, (reusable, fresh) in _R_T2_EXPECTED.items():
        rows, browse, seen = _r_observe(cache, monkeypatch, account)
        off_rows, off_browse, off_seen = _r_observe(
            cache, monkeypatch, account, reuse=False)
        assert seen["epoch"][0] is not None, "the global gate passes"
        assert off_seen["partition"] == []
        assert seen["partition"] == [(sorted(reusable), sorted(fresh))], account
        assert rows == off_rows, account
        assert browse == off_browse, account
        assert [row[1] for row in rows] == sorted(reusable | fresh)


def _r_t4_store(tmp_path, monkeypatch):
    ns, core, cache = _r_env(tmp_path, monkeypatch)
    _r_seed(core, cache, _R_T3_MESSAGES + [
        ("pc", "pc-u", "alpha other question", "2026-08-04T02:00:00Z", None,
         None, ACCOUNT_A),
        ("pc", "pc-1", "alpha other answer", "2026-08-04T02:01:00Z", "mc",
         "rc", ACCOUNT_A),
    ], _R_T3_ENTRIES + [("mc", "rc", 0.5, ACCOUNT_A, 3)])
    rows, _browse, seen = _r_observe(cache, monkeypatch, ACCOUNT_A)
    assert seen["partition"] == [(["pa", "pc"], [])], \
        "precondition: both sessions are reused"
    return ns, core, cache


def _r_meta(path, sql, params=()):
    conn = _r_raw(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _r_unstamped_cost_rewrite(core, value):
    _r_meta(core.CACHE_DB_PATH,
            "UPDATE session_entries SET cost_usd_raw=? WHERE msg_id='m2'",
            (value,))


def _r_newer_migration(ns, cache, registry_name, handler):
    """Run a newer binary's migration to completion through the real
    dispatcher (an extended registry), then let this binary recover the
    version-ahead store."""
    registry = ns[registry_name]
    db_mod = ns["_cctally_db"]
    registry.append(db_mod.Migration(
        seq=len(registry) + 1, name="999_newer_untracked_rewrite",
        handler=handler))
    try:
        if registry_name == "_CACHE_MIGRATIONS":
            ns["open_cache_db"]().close()
        else:
            cache.open_conversations_db().close()
    finally:
        registry.pop()
    # This binary's next writer open trims the unknown marker (recovery).
    if registry_name == "_CACHE_MIGRATIONS":
        ns["open_cache_db"]().close()
    else:
        cache.open_conversations_db().close()


_R_T4_GLOBAL = ("blocking_flag", "pending_known_migration", "unknown_fence",
                "unknown_ledger_name", "user_version_mismatch",
                "absent_incarnation_main", "absent_incarnation_cache")
_R_T4_SESSION = ("epoch_pricing", "recreated_cache", "newer_cache_recovered",
                 "newer_conversations_recovered", "unskip", "null_fingerprint",
                 "seal_mismatch")


@pytest.mark.parametrize("case", _R_T4_GLOBAL + _R_T4_SESSION)
def test_reuse_falls_back_when_gate_fails(tmp_path, monkeypatch, case):
    """#1068 T4. Accuracy failure detected: a stored row reused when the
    reader cannot prove it equal to the fresh scoped derivation — a blocking
    flag, a pending or unknown migration, a fence left by an unknown
    migration's unstamped rewrite, a schema-version mismatch, a missing
    incarnation id, another pricing snapshot, a re-created cache.db whose
    ledger and counters match the old one, a newer migration's unstamped
    rewrite then recovery (cache and conversations), a skip/unskip that
    restores the ledger digest, a NULL fingerprint, or content rewritten
    without the fingerprint. Each falls back (a spy observes it) and the TEMP
    rows equal the forced full derivation."""
    import argparse
    ns, core, cache = _r_t4_store(tmp_path, monkeypatch)
    raw_paths = None
    expect_title = None
    expected_pa_cost = 3.75
    if case == "blocking_flag":
        _r_meta(core.CONVERSATIONS_DB_PATH,
                "INSERT INTO cache_meta(key,value) "
                "VALUES('conversation_reingest_pending','1')")
    elif case == "pending_known_migration":
        newest = ns["_CONVERSATIONS_MIGRATIONS"][-1].name
        _r_meta(core.CONVERSATIONS_DB_PATH,
                "DELETE FROM schema_migrations WHERE name=?", (newest,))
    elif case == "unknown_fence":
        _r_meta(core.CACHE_DB_PATH,
                "INSERT INTO cache_meta(key,value) "
                "VALUES('migration_fence:unknown-run','t')")
        _r_unstamped_cost_rewrite(core, 5.0)
        expected_pa_cost = 6.5
    elif case == "unknown_ledger_name":
        _r_meta(core.CONVERSATIONS_DB_PATH,
                "INSERT INTO schema_migrations(name,applied_at_utc) "
                "VALUES('999_from_a_newer_binary','2026-08-04T00:00:00Z')")
    elif case == "user_version_mismatch":
        version = len(ns["_CACHE_MIGRATIONS"]) + 1
        _r_meta(core.CACHE_DB_PATH, f"PRAGMA user_version={version}")
        raw_paths = (core.CONVERSATIONS_DB_PATH, core.CACHE_DB_PATH)
    elif case == "absent_incarnation_main":
        _r_meta(core.CONVERSATIONS_DB_PATH,
                "DELETE FROM cache_meta WHERE key='conversations_incarnation_id'")
    elif case == "absent_incarnation_cache":
        _r_meta(core.CACHE_DB_PATH,
                "DELETE FROM cache_meta WHERE key='cache_incarnation_id'")
    elif case == "epoch_pricing":
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2099-01-01")
    elif case == "recreated_cache":
        # Same ledger, counter, counts and sequences; different accounting
        # values and a new incarnation id, as a re-created file has.
        _r_meta(core.CACHE_DB_PATH,
                "UPDATE cache_meta SET value='recreated-store' "
                "WHERE key='cache_incarnation_id'")
        _r_unstamped_cost_rewrite(core, 5.0)
        expected_pa_cost = 6.5
    elif case == "newer_cache_recovered":
        def rewrite_cost(conn):
            conn.execute("UPDATE session_entries SET cost_usd_raw=5.0 "
                         "WHERE msg_id='m2'")
            conn.commit()
        _r_newer_migration(ns, cache, "_CACHE_MIGRATIONS", rewrite_cost)
        expected_pa_cost = 6.5
    elif case == "newer_conversations_recovered":
        def rewrite_title(conn):
            conn.execute("UPDATE conversation_messages SET text='rewritten "
                         "alpha question' WHERE uuid='pa-u1'")
            conn.commit()
        _r_newer_migration(ns, cache, "_CONVERSATIONS_MIGRATIONS",
                           rewrite_title)
        expect_title = "rewritten alpha question"
    elif case == "unskip":
        # Certify under a skipped migration, unskip it, rewrite an input
        # without a stamp, then skip it again with the SAME timestamp: the
        # ledger digest is the one the rows were certified under, and only
        # the rotated incarnation id tells the two states apart.
        newest = ns["_CONVERSATIONS_MIGRATIONS"][-1].name
        qname = f"conversations.db:{newest}"
        _r_meta(core.CONVERSATIONS_DB_PATH,
                "DELETE FROM schema_migrations WHERE name=?", (newest,))
        assert ns["cmd_db_skip"](argparse.Namespace(name=qname, reason="t")) == 0
        writer = cache.open_conversations_db()
        try:
            assert cache._recompute_conversation_sessions(writer) is True
            writer.commit()
        finally:
            writer.close()
        _rows, _b, seen = _r_observe(cache, monkeypatch, ACCOUNT_A)
        assert seen["partition"] == [(["pa", "pc"], [])]
        def ledger():
            conn = _r_raw(core.CONVERSATIONS_DB_PATH)
            try:
                return (
                    sorted(conn.execute(
                        "SELECT name, applied_at_utc FROM schema_migrations")),
                    sorted(conn.execute(
                        "SELECT name, skipped_at_utc "
                        "FROM schema_migrations_skipped")))
            finally:
                conn.close()

        certified_ledger = ledger()
        skipped_at = dict(certified_ledger[1])[newest]
        assert ns["cmd_db_unskip"](argparse.Namespace(name=qname)) == 0
        _r_meta(core.CONVERSATIONS_DB_PATH,
                "UPDATE conversation_messages SET text='rewritten alpha "
                "question' WHERE uuid='pa-u1'")
        assert ns["cmd_db_skip"](argparse.Namespace(name=qname, reason="t")) == 0
        _r_meta(core.CONVERSATIONS_DB_PATH,
                "UPDATE schema_migrations_skipped SET skipped_at_utc=? "
                "WHERE name=?", (skipped_at, newest))
        # Unskip lowered user_version; the next writer open's dispatcher finds
        # nothing pending and advances it again (no run, so no rotation).
        cache.open_conversations_db().close()
        assert ledger() == certified_ledger, "the ledger digest recurs"
        expect_title = "rewritten alpha question"
    elif case == "null_fingerprint":
        _r_meta(core.CONVERSATIONS_DB_PATH,
                "UPDATE conversation_sessions SET input_fingerprint=NULL "
                "WHERE session_id='pa'")
    elif case == "seal_mismatch":
        # An older binary's re-fill rewrites content, never the fingerprint.
        _r_meta(core.CONVERSATIONS_DB_PATH,
                "UPDATE conversation_sessions SET title='stale older binary' "
                "WHERE session_id='pa'")
    rows, browse, seen = _r_observe(cache, monkeypatch, ACCOUNT_A,
                                    raw_paths=raw_paths)
    off_rows, off_browse, _off = _r_observe(
        cache, monkeypatch, ACCOUNT_A, reuse=False, raw_paths=raw_paths)
    if case in _R_T4_GLOBAL:
        assert seen["epoch"] == [None] and seen["partition"] == []
    else:
        assert seen["epoch"][0] is not None
        reusable, fresh = seen["partition"][0]
        assert "pa" in fresh and "pa" not in reusable
    assert rows == off_rows
    assert browse == off_browse
    pa = next(row for row in browse["conversations"] if row["session_id"] == "pa")
    assert pa["cost_usd"] == expected_pa_cost
    if expect_title is not None:
        assert pa["title"] == expect_title
    if case == "seal_mismatch":
        assert pa["title"] != "stale older binary"


_R_CHILD_RUN = r'''
import os, pathlib, sqlite3, sys, time
sys.path.insert(0, sys.argv[1])
import _cctally_db as db
conn = sqlite3.connect(sys.argv[2])
barrier = pathlib.Path(sys.argv[3])
run = db._MigrationFenceRun(conn, "conversations.db")
run.begin()
(barrier / "fenced").write_text("1")
deadline = time.monotonic() + 60
while not (barrier / "go").exists() and time.monotonic() < deadline:
    time.sleep(0.02)
conn.execute("UPDATE conversation_messages SET text='rewritten alpha question' "
             "WHERE uuid='pa-u1'")
conn.commit()
(barrier / "changed").write_text("1")
os._exit(9)  # dies before its stamp: the row stays, the flock goes
'''


def _r_wait_for(path, timeout=60.0):
    import time
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path}"
        time.sleep(0.02)


def _r_fence_keys(path):
    conn = _r_raw(path)
    try:
        return [row[0] for row in conn.execute(
            "SELECT key FROM cache_meta WHERE key LIKE 'migration_fence:%'")]
    finally:
        conn.close()


def _r_incarnation(path, key="conversations_incarnation_id"):
    conn = _r_raw(path)
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _r_stored_fp(path, sid):
    conn = _r_raw(path)
    try:
        return conn.execute(
            "SELECT input_fingerprint FROM conversation_sessions "
            "WHERE session_id=?", (sid,)).fetchone()[0]
    finally:
        conn.close()


def test_round4_interleaving_under_barriers(tmp_path, monkeypatch):
    """#1068 T4 (round-4 interleaving). Accuracy failure detected: a row
    certified, or reused, across an untracked write of a dispatcher run that
    is still active (its fence row committed, its shared fence lock held),
    including after that run dies before its stamp. The run is a separate
    process so its death, not a tidy release, frees the fence lock."""
    import subprocess
    ns, core, cache = _r_t4_store(tmp_path, monkeypatch)
    conv_path = core.CONVERSATIONS_DB_PATH
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    bin_dir = str(pathlib.Path(cache.__file__).resolve().parent)
    child = subprocess.Popen(
        [sys.executable, "-c", _R_CHILD_RUN, bin_dir, str(conv_path),
         str(barrier)])
    try:
        _r_wait_for(barrier / "fenced")
        before = _r_incarnation(conv_path)
        # This binary's dispatcher entry cannot take the fence lock
        # exclusive, so the active run's row stays; certification refuses.
        writer = cache.open_conversations_db()
        try:
            assert len(_r_fence_keys(conv_path)) == 1
            assert cache._rollup_certification_allowed(writer) is False
            assert cache._recompute_conversation_sessions(writer, {"pa"})
            writer.commit()
        finally:
            writer.close()
        assert _r_stored_fp(conv_path, "pa") is None
        (barrier / "go").write_text("1")
        _r_wait_for(barrier / "changed")
        assert child.wait(timeout=60) == 9
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    # The dead run's row remains: the reader refuses reuse.
    assert len(_r_fence_keys(conv_path)) == 1
    rows, browse, seen = _r_observe(cache, monkeypatch, ACCOUNT_A)
    off_rows, off_browse, _off = _r_observe(
        cache, monkeypatch, ACCOUNT_A, reuse=False)
    assert seen["epoch"] == [None]
    assert rows == off_rows and browse == off_browse
    titles = {r["session_id"]: r["title"] for r in browse["conversations"]}
    assert titles["pa"] == "rewritten alpha question"
    # The next dispatcher entry holds the lock exclusive (the run is dead),
    # removes the row and rotates the id: rows certified before the rotation
    # (pc) are no longer current.
    cache.open_conversations_db().close()
    assert _r_fence_keys(conv_path) == []
    assert _r_incarnation(conv_path) not in (None, before)
    _rows, _browse, seen = _r_observe(cache, monkeypatch, ACCOUNT_A)
    assert seen["epoch"][0] is not None
    assert seen["partition"] == [([], ["pa", "pc"])]
    # Only rows certified after the rotation are reused.
    writer = cache.open_conversations_db()
    try:
        assert cache._recompute_conversation_sessions(writer) is True
        writer.commit()
    finally:
        writer.close()
    rows, browse, seen = _r_observe(cache, monkeypatch, ACCOUNT_A)
    off_rows, off_browse, _off = _r_observe(
        cache, monkeypatch, ACCOUNT_A, reuse=False)
    assert seen["partition"] == [(["pa", "pc"], [])]
    assert rows == off_rows and browse == off_browse


def test_overlapping_same_binary_runs_keep_store_fenced(tmp_path, monkeypatch):
    """#1068 T4 (overlapping runs). Accuracy failure detected: the first of
    two overlapping dispatcher runs of one binary removing the other's fence
    row (or every row) when it finishes, so a row certified while the second
    run's handler still writes is later reused."""
    import threading
    ns, core, cache = _r_t4_store(tmp_path, monkeypatch)
    conv_path = core.CONVERSATIONS_DB_PATH
    registry = ns["_CONVERSATIONS_MIGRATIONS"]
    db_mod = ns["_cctally_db"]
    entered = {1: threading.Event(), 2: threading.Event()}
    release = {1: threading.Event(), 2: threading.Event()}
    which = threading.local()

    def handler(conn):
        entered[which.n].set()
        assert release[which.n].wait(60)
        conn.execute("UPDATE conversation_messages SET text=? "
                     "WHERE uuid='pa-u1'", (f"rewritten by run {which.n}",))
        conn.commit()

    registry.append(db_mod.Migration(
        seq=len(registry) + 1, name="999_overlapping_rewrite", handler=handler))
    errors = []

    def run(n):
        which.n = n
        conn = sqlite3.connect(str(conv_path), timeout=30)
        try:
            db_mod._run_pending_migrations(
                conn, registry=registry, db_label="conversations.db")
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)
        finally:
            conn.close()

    # Each run is its own `cctally` process owner (#787 / #861).
    threads = {n: threading.Thread(target=as_process_owner(run), args=(n,))
               for n in (1, 2)}
    try:
        threads[1].start()
        assert entered[1].wait(60)
        threads[2].start()
        assert entered[2].wait(60)
        assert len(_r_fence_keys(conv_path)) == 2
        release[1].set()
        threads[1].join(60)
        assert not threads[1].is_alive()
        # The first run removed only its own row: the store stays fenced, so
        # a writer cannot certify and a reader cannot reuse.
        assert len(_r_fence_keys(conv_path)) == 1
        writer = cache.open_conversations_db()
        try:
            assert cache._rollup_certification_allowed(writer) is False
        finally:
            writer.close()
        _rows, _browse, seen = _r_observe(cache, monkeypatch, ACCOUNT_A)
        assert seen["epoch"] == [None]
        release[2].set()
        threads[2].join(60)
        assert not threads[2].is_alive()
        assert errors == []
        assert _r_fence_keys(conv_path) == []
        # After both runs, rows certified now are current and reused.
        writer = cache.open_conversations_db()
        try:
            assert cache._recompute_conversation_sessions(writer) is True
            writer.commit()
        finally:
            writer.close()
        rows, browse, seen = _r_observe(cache, monkeypatch, ACCOUNT_A)
        off_rows, off_browse, _off = _r_observe(
            cache, monkeypatch, ACCOUNT_A, reuse=False)
        assert seen["partition"] == [(["pa", "pc"], [])]
        assert rows == off_rows and browse == off_browse
        titles = {r["session_id"]: r["title"] for r in browse["conversations"]}
        assert titles["pa"] == "rewritten by run 2"
    finally:
        for n in (1, 2):
            release[n].set()
        for t in threads.values():
            if t.is_alive():
                t.join(60)
        registry.pop()


def test_scope_without_fingerprint_column_takes_todays_path(tmp_path):
    """#1068. Stored-data/availability failure detected: a store that has not
    received the input_fingerprint column (migration 013 not yet applied)
    raising in the scoper or the assembly memo key instead of serving the
    scoped request exactly as before."""
    conn = _open_scoped_fixture(tmp_path, ACCOUNT_A, drop_fingerprint=True)
    try:
        assert "input_fingerprint" not in {
            row[1] for row in conn.execute(
                "PRAGMA temp.table_info(conversation_sessions)")}
        assert cache._claude_rollup_reuse_epoch(conn) is None
        assert claude_query._assemble_memo_key(
            conn, "shared-session") is not None, "the memo stays usable"
        detail = claude_query.get_conversation(conn, "shared-session")
        assert detail["cost_usd"] == 1.25
        assert "bravo private message" not in str(detail)
    finally:
        claude_query._assemble_memo_clear()
        conn.close()


# --- #1068 D6: scoped reuse of stored Codex rollup rows ---------------------

# (conversation_key, line_offset, ts, text, account_key). "v1.a-mixed" sorts
# before "v1.b-pure" and both carry one project key with different stored
# labels, so the facets' first-label-wins rule observes the TEMP row order.
_R_CODEX_ROWS = [
    ("v1.a-mixed", 10, "2026-08-04T01:00:00Z", "alpha mixed codex", _A),
    ("v1.a-mixed", 20, "2026-08-04T01:01:00Z", "bravo mixed codex", _B),
    ("v1.b-pure", 10, "2026-08-04T02:00:00Z", "alpha pure codex", _A),
    ("v1.b-pure", 20, "2026-08-04T02:01:00Z", "alpha pure codex more", _A),
    ("v1.c-pure-b", 10, "2026-08-04T03:00:00Z", "bravo pure codex", _B),
    ("v1.d-null", 10, "2026-08-04T04:00:00Z", "null codex", None),
    ("v1.d-null", 20, "2026-08-04T04:01:00Z", "literal codex", _UNATTR),
]
_R_CODEX_LABELS = {
    "v1.a-mixed": ("pk-shared", "Label from mixed"),
    "v1.b-pure": ("pk-shared", "Label from pure"),
}
_R_CODEX_EXPECTED = {
    _A: (["v1.b-pure"], ["v1.a-mixed"]),
    _B: (["v1.c-pure-b"], ["v1.a-mixed"]),
    _UNATTR: (["v1.d-null"], []),
}


def _r_codex_seed(cache, order):
    writer = cache.open_conversations_db()
    try:
        writer.execute(
            "INSERT INTO cache_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("codex_conversation_contract_version",
             codex_kernel.CODEX_CONVERSATION_CONTRACT_VERSION))
        for key, offset, ts, text, account in _r_ordered(
                _R_CODEX_ROWS, order, 4):
            path = f"{key}.jsonl"
            writer.execute(
                "INSERT INTO codex_conversation_messages "
                "(conversation_key,source_root_key,source_path,line_offset,"
                " timestamp_utc,turn_id,call_id,kind,event_type,record_family,"
                " model,text,content_digest,content_len,detail_json,"
                " search_tool,search_thinking,account_key) "
                "VALUES(?,?,?,?,?,?,NULL,'user',NULL,'event_msg','gpt-5.6',?,"
                "?,?,NULL,NULL,NULL,?)",
                (key, "root", path, offset, ts, f"turn-{offset}", text,
                 (key + str(offset)).ljust(64, "0")[:64], len(text), account))
        cache._recompute_codex_rollups(writer, {r[0] for r in _R_CODEX_ROWS})
        for key, (project_key, label) in _R_CODEX_LABELS.items():
            writer.execute(
                "UPDATE codex_conversation_rollups "
                "SET project_key=?, project_label=? WHERE conversation_key=?",
                (project_key, label, key))
        writer.commit()
        assert codex_query.codex_normalization_authoritative(writer)
    finally:
        writer.close()


def _r_codex_observe(cache, monkeypatch, account, *, reuse=True):
    seen = []
    real = cache._scoped_codex_reuse_partition

    def spy(conn, codex_keys):
        out = real(conn, codex_keys) if reuse else (
            [], sorted(k for k in codex_keys if k))
        seen.append(out)
        return out

    with monkeypatch.context() as m:
        m.setattr(cache, "_scoped_codex_reuse_partition", spy)
        conn = _r_scoped(cache, account)
        try:
            rows = _r_temp_rows(conn, "codex_conversation_rollups")
            facets = codex_query.list_codex_conversation_facets(conn)
            browse = codex_query.list_codex_conversations(
                conn, effective_speed="standard")
        finally:
            conn.close()
    return rows, facets, browse, seen


@pytest.mark.parametrize("order", [(_A, _B), (_B, _A)], ids=["a-first", "b-first"])
def test_scoped_codex_reuse_equals_forced_derivation(
        tmp_path, monkeypatch, order):
    """#1068 T2 (Codex). Privacy/accuracy failure detected: a conversation
    with rows outside the scope reused from its stored rollup (its item
    count, title, times or models then describe another account's rows), or
    a reused TEMP table whose row order makes the facets keep another first
    label for a shared project key. For every scope the TEMP rollup rows,
    the facet response and the browse body equal reuse forced off, and the
    spy proves which conversations were reused."""
    ns, core, cache = _r_env(tmp_path, monkeypatch)
    _r_codex_seed(cache, order)
    for account, expected in _R_CODEX_EXPECTED.items():
        rows, facets, browse, seen = _r_codex_observe(
            cache, monkeypatch, account)
        off_rows, off_facets, off_browse, _off = _r_codex_observe(
            cache, monkeypatch, account, reuse=False)
        assert seen == [expected], account
        assert rows == off_rows, account
        assert facets == off_facets, account
        assert browse == off_browse, account
        assert [row[1] for row in rows] == sorted(expected[0] + expected[1])
    _rows, facets, _browse, _seen = _r_codex_observe(cache, monkeypatch, _A)
    projects = facets["facets"]["projects"]
    shared = [p for p in projects if p.get("project_key") == "pk-shared"
              or p.get("key") == "pk-shared"]
    assert shared and "Label from mixed" in str(shared), \
        "the first label in key order wins, as in the full derivation"


def _scoped_key_lookup_fixture(tmp_path, account_key):
    """One session whose logical message ``u1`` has an in-scope and an
    out-of-scope physical row (both carrying the badge term), a second in-scope
    group ``u2`` and an in-scope NULL-uuid group. Returns (conn, ids by tag)."""
    cache_path = tmp_path / "cache.db"
    cache_conn = sqlite3.connect(cache_path)
    db._apply_cache_schema(cache_conn)
    cache_conn.commit()
    cache_conn.close()
    conn = sqlite3.connect(":memory:")
    db._apply_conversations_schema(conn)
    inside = None if account_key == "unattributed" else account_key
    rows = [  # (tag, uuid, account, search_tool, offset)
        ("u1-in", "u1", inside, "grep error in", 10),
        ("u1-out", "u1", ACCOUNT_B, "grep error out", 20),
        ("u2-in", "u2", inside, "", 30),
        ("null-in", None, inside, "", 40),
    ]
    ids = {}
    for tag, uuid, account, tool, offset in rows:
        ids[tag] = conn.execute(
            "INSERT INTO conversation_messages "
            "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,"
            " text,blocks_json,model,cwd,is_sidechain,account_key,search_tool) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s1", uuid, "s1.jsonl", offset, f"2026-08-04T10:00:{offset}Z",
             "assistant", f"text {tag}", "[]", "claude-opus-4-8", "/work", 0,
             account, tool)).lastrowid
    conn.execute("ATTACH DATABASE ? AS cache_db", (str(cache_path),))
    cache._recompute_conversation_sessions(conn)
    conn.commit()
    cache.scope_conversations_db_to_account(conn, account_key)
    return conn, ids


@pytest.mark.parametrize("account", [ACCOUNT_A, "unattributed"])
def test_scoped_search_key_lookups_keep_key_indexes(tmp_path, account):
    """#1068 D1 follow-up. Performance/privacy failure detected: once the
    scope predicates are sargable, SQLite (no sqlite_stat1) estimates
    ``account_key = K`` on idx_conv_messages_account_session as a few rows and
    prefers it to the search helpers' per-key lookups, walking the whole
    account per call (measured on the 2.3M-message copy, account A: 0.97 s per
    content search for the hit-group lookup, 142 s per LIKE badge column). The
    by-key lookups must keep their key indexes and still return only in-scope
    physical rows, a same-group fragment of another account included."""
    conn, ids = _scoped_key_lookup_fixture(tmp_path, account)
    groups = [("s1", "u1"), ("s1", "u2"), ("s1", None)]
    statements = []
    conn.set_trace_callback(statements.append)
    rids = claude_query._all_matched_rids_by_group(
        conn, " AND cm.entry_type = ?", ("assistant",), groups)
    texts = claude_query._texts_for_ids(conn, sorted(ids.values()))
    badges = claude_query._like_badges(conn, "%error%", groups)
    conn.set_trace_callback(None)

    assert rids == {("s1", "u1"): [ids["u1-in"]], ("s1", "u2"): [ids["u2-in"]],
                    ("s1", None): [ids["null-in"]]}
    assert set(texts) == {ids["u1-in"], ids["u2-in"], ids["null-in"]}
    assert badges[("s1", "u1")] == ["tool"]
    assert badges[("s1", "u2")] == [] and badges[("s1", None)] == []

    lookups = [s for s in statements if "conversation_messages" in s
               and not s.lstrip().startswith("--")]
    assert len(lookups) >= 3
    for sql in lookups:
        plan = " | ".join(
            row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "idx_conv_messages_account_session" not in plan, (sql, plan)
        assert ("idx_conv_session_uuid" in plan
                or "INTEGER PRIMARY KEY" in plan), (sql, plan)
