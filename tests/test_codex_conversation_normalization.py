"""#294 S6 — Codex conversation normalization (schema, FTS lifecycle, kernel).

Contract-pinned test module name (S0 ``futureTestTargets``). Grows task-by-task:
Task 2 adds the normalized-table schema + independent Codex FTS lifecycle here;
later tasks add the kernel, ingest, assembly, browse, search, and dispatch
classes to the same file.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _cctally_db as db  # noqa: E402
import _cctally_core as core  # noqa: E402
import _lib_codex_conversation as kern  # noqa: E402
import _lib_codex_landmarks as landmarks  # noqa: E402
import _lib_codex_conversation_export as cexport  # noqa: E402
import _lib_codex_conversation_query as q  # noqa: E402
import _lib_conversation as lc  # noqa: E402
import _lib_conversation_anon as anon  # noqa: E402
import _lib_conversation_dispatch as disp  # noqa: E402
import _lib_conversation_query as lcq  # noqa: E402
import _lib_jsonl as lj  # noqa: E402
import _lib_pricing as pricing  # noqa: E402
import _lib_source_identity as identity  # noqa: E402

CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1"
ROOT_A = "/synthetic/root-a/project-red"
ROOT_B = "/synthetic/root-b/project-blue"
MODEL = "gpt-synthetic-codex"


def _events(scenario: str, *, root: str = ROOT_A) -> list:
    """Parse a corpus scenario through the S1 fused iterator into the physical
    event batch normalize_codex_events consumes."""
    path = CORPUS / "rollouts" / f"{scenario}.jsonl"
    state = lj._CodexIterState()
    with path.open("rb") as fh:
        emissions = list(lj._iter_codex_fused_records_with_offsets(
            fh, str(path), state=state, source_root_key=identity.source_root_key(root)))
    return [em.event for em in emissions]


def _normalize(scenario: str, *, root: str = ROOT_A) -> kern.CodexNormalizationResult:
    return kern.normalize_codex_events(
        _events(scenario, root=root), initial=kern.CodexStickyState())


# ── schema helpers (mirrors tests/test_codex_fused_ingest.py) ─────────────────


def _cache_schema() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, int]]:
    return [
        (str(row[1]), str(row[2]), int(row[3]))
        for row in conn.execute(f"PRAGMA table_info({table})")
    ]


def _schema_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None and row[0] is not None, f"missing schema object {name}"
    return str(row[0])


def _trigger_map(conn: sqlite3.Connection, like: str) -> dict[str, str]:
    return {
        str(name): str(sql)
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
            (like,),
        )
    }


def _insert_msg(
    conn: sqlite3.Connection,
    *,
    offset: int,
    text: str = "",
    search_tool: str = "",
    search_thinking: str = "",
    conversation_key: str = "conv-a",
    source_root_key: str = "root-a",
    source_path: str = "/synthetic/root-a/a.jsonl",
    kind: str = "assistant",
    record_family: str = "response_item",
) -> None:
    conn.execute(
        """INSERT INTO codex_conversation_messages
           (conversation_key, source_root_key, source_path, line_offset,
            timestamp_utc, turn_id, call_id, kind, event_type, record_family,
            model, text, content_digest, content_len, detail_json,
            search_tool, search_thinking)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            conversation_key, source_root_key, source_path, offset,
            "2026-07-14T12:00:00+00:00", "turn-a", None, kind, None, record_family,
            "gpt-synthetic-codex", text, "d" * 32, len(text.encode("utf-8")), None,
            search_tool, search_thinking,
        ),
    )


# ── §3.1–§3.3 schema exactness ────────────────────────────────────────────────


def test_codex_conversation_messages_schema_is_exact():
    conn = _cache_schema()
    try:
        assert _columns(conn, "codex_conversation_messages") == [
            ("id", "INTEGER", 0),
            ("conversation_key", "TEXT", 1),
            ("source_root_key", "TEXT", 1),
            ("source_path", "TEXT", 1),
            ("line_offset", "INTEGER", 1),
            ("timestamp_utc", "TEXT", 0),
            ("turn_id", "TEXT", 0),
            ("call_id", "TEXT", 0),
            ("kind", "TEXT", 1),
            ("event_type", "TEXT", 0),
            ("record_family", "TEXT", 1),
            ("model", "TEXT", 0),
            ("text", "TEXT", 0),
            ("content_digest", "TEXT", 1),
            ("content_len", "INTEGER", 1),
            ("detail_json", "TEXT", 0),
            ("search_tool", "TEXT", 0),
            ("search_thinking", "TEXT", 0),
        ]
        sql = _schema_sql(conn, "codex_conversation_messages")
        assert "CHECK(content_len >= 0)" in sql
        assert "UNIQUE(source_path, line_offset)" in sql
        assert "AUTOINCREMENT" not in sql  # rowid alias; §3.5 byte-idempotency

        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            "idx_codex_conv_msgs_conversation",
            "idx_codex_conv_msgs_source",
        } <= indexes
    finally:
        conn.close()


def test_codex_conversation_messages_content_len_check_rejects_negative():
    conn = _cache_schema()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO codex_conversation_messages
                   (conversation_key, source_root_key, source_path, line_offset,
                    kind, record_family, content_digest, content_len)
                   VALUES ('c','r','/p',1,'assistant','response_item','d', -1)"""
            )
    finally:
        conn.close()


def test_codex_conversation_messages_unique_physical_key():
    conn = _cache_schema()
    try:
        _insert_msg(conn, offset=1, text="a")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_msg(conn, offset=1, text="b")
    finally:
        conn.close()


def test_codex_conversation_rollups_schema_is_exact():
    conn = _cache_schema()
    try:
        assert _columns(conn, "codex_conversation_rollups") == [
            ("conversation_key", "TEXT", 1),
            ("source_root_key", "TEXT", 1),
            ("parent_thread_id", "TEXT", 0),
            ("item_count", "INTEGER", 1),
            ("started_utc", "TEXT", 0),
            ("last_activity_utc", "TEXT", 0),
            ("project_key", "TEXT", 0),
            ("project_label", "TEXT", 0),
            ("models_json", "TEXT", 0),
            ("title", "TEXT", 0),
        ]
        sql = _schema_sql(conn, "codex_conversation_rollups")
        assert "PRIMARY KEY" in sql
        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_codex_conv_rollups_recent" in indexes
        recent_sql = _schema_sql(conn, "idx_codex_conv_rollups_recent")
        assert "last_activity_utc DESC" in recent_sql
        assert "conversation_key DESC" in recent_sql
    finally:
        conn.close()


def test_codex_conversation_file_touches_schema_is_exact():
    conn = _cache_schema()
    try:
        assert _columns(conn, "codex_conversation_file_touches") == [
            ("message_id", "INTEGER", 1),
            ("conversation_key", "TEXT", 1),
            ("source_path", "TEXT", 1),
            ("file_path", "TEXT", 1),
            ("tool", "TEXT", 1),
        ]
        sql = _schema_sql(conn, "codex_conversation_file_touches")
        assert "UNIQUE(message_id, file_path, tool)" in sql
        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_codex_conv_touches_source" in indexes
    finally:
        conn.close()


def test_codex_session_files_gains_last_turn_id():
    conn = _cache_schema()
    try:
        cols = {c[0] for c in _columns(conn, "codex_session_files")}
        assert "last_turn_id" in cols
    finally:
        conn.close()


# ── §3.4 independent Codex FTS lifecycle ─────────────────────────────────────


def test_fresh_cache_creates_codex_fts_and_leaves_claude_triggers_byte_unchanged():
    conn = _cache_schema()
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "codex_conversation_fts" in tables
        codex_triggers = set(_trigger_map(conn, "codex_conv_fts_%"))
        assert codex_triggers == {"codex_conv_fts_ai", "codex_conv_fts_ad", "codex_conv_fts_au"}

        # Snapshot the Claude message + title FTS trigger SQL, then run the full
        # Codex FTS lifecycle (full-clear + drop/recreate the Codex triggers).
        claude_before = {
            **_trigger_map(conn, "conv_fts_%"),
            **_trigger_map(conn, "conv_title_fts_%"),
        }
        assert "conv_fts_ai" in claude_before and "conv_title_fts_ai" in claude_before

        _insert_msg(conn, offset=1, text="alpha bravo")
        db._codex_conversation_fts_full_clear(conn)
        db._drop_codex_conversation_fts_triggers(conn)
        db._create_codex_conversation_fts_triggers(conn)

        claude_after = {
            **_trigger_map(conn, "conv_fts_%"),
            **_trigger_map(conn, "conv_title_fts_%"),
        }
        # Codex names must never appear in the Claude-scoped snapshot.
        assert not any(name.startswith("codex_") for name in claude_before)
        assert claude_after == claude_before, "Claude FTS trigger SQL must be byte-unchanged"
    finally:
        conn.close()


def test_codex_fts_indexes_and_matches_rows():
    conn = _cache_schema()
    try:
        _insert_msg(conn, offset=1, text="unmistakable prose token")
        _insert_msg(conn, offset=2, text="different words entirely", source_path="/p2")
        hits = conn.execute(
            "SELECT rowid FROM codex_conversation_fts WHERE codex_conversation_fts MATCH ?",
            ("unmistakable",),
        ).fetchall()
        assert len(hits) == 1
    finally:
        conn.close()


def test_legacy_claude_fts_cache_still_gains_codex_fts(tmp_path):
    """A cache whose Claude FTS is the legacy single-column shape (which makes
    _apply_cache_schema early-return) must STILL gain the Codex FTS, because the
    Codex lifecycle runs before that early-return."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        db._apply_cache_schema(conn)
        # Tear the split conversation_fts down to the legacy single-column shape.
        db._drop_conversation_fts_triggers(conn)
        conn.execute("DROP TABLE IF EXISTS codex_conversation_fts")
        db._drop_codex_conversation_fts_triggers(conn)
        conn.execute("DROP TABLE IF EXISTS conversation_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE conversation_fts USING fts5("
            "text, content='conversation_messages', content_rowid='id')")
        db._create_conversation_fts_legacy_triggers(conn)
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(path)
    try:
        # This early-returns for Claude (legacy shape), but must still stand up
        # the Codex FTS beforehand.
        db._apply_cache_schema(conn)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "codex_conversation_fts" in tables
        assert set(_trigger_map(conn, "codex_conv_fts_%")) == {
            "codex_conv_fts_ai", "codex_conv_fts_ad", "codex_conv_fts_au",
        }
    finally:
        conn.close()


def test_codex_fts_unavailable_at_creation_sets_marker_and_skips_ddl(monkeypatch):
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    conn = _cache_schema()
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "codex_conversation_fts" not in tables
        assert not _trigger_map(conn, "codex_conv_fts_%")
        assert conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='codex_fts_unavailable'"
        ).fetchone() is not None
        # A normalized-row INSERT must still succeed (no orphan trigger).
        _insert_msg(conn, offset=1, text="under like fallback")
    finally:
        conn.close()


def test_codex_capable_then_unavailable_reopen_drops_only_codex_triggers(tmp_path, monkeypatch):
    path = tmp_path / "cap.db"
    conn = sqlite3.connect(path)
    try:
        db._apply_cache_schema(conn)  # FTS-capable creation
        assert set(_trigger_map(conn, "codex_conv_fts_%"))
    finally:
        conn.close()

    # Reopen under a build without FTS5.
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    conn = sqlite3.connect(path)
    try:
        db._apply_cache_schema(conn)
        assert not _trigger_map(conn, "codex_conv_fts_%"), "Codex triggers must be dropped"
        # Claude triggers must ALSO be handled by their own branch; assert Codex
        # marker set and a normalized INSERT succeeds (no orphan trigger error).
        assert conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='codex_fts_unavailable'"
        ).fetchone() is not None
        _insert_msg(conn, offset=5, text="post-downgrade insert")
        conn.commit()
    finally:
        conn.close()


def test_codex_fts_recovery_recreates_and_rebuilds_and_clears_marker(tmp_path, monkeypatch):
    path = tmp_path / "rec.db"
    # Create FTS-unavailable, then ingest a row (no trigger indexes it).
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    conn = sqlite3.connect(path)
    try:
        db._apply_cache_schema(conn)
        _insert_msg(conn, offset=1, text="recoverable token")
        conn.commit()
    finally:
        conn.close()

    # Reopen FTS-capable: recovery must create the vtable, rebuild from base
    # rows, and clear the marker.
    monkeypatch.undo()
    conn = sqlite3.connect(path)
    try:
        db._apply_cache_schema(conn)
        assert conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='codex_fts_unavailable'"
        ).fetchone() is None
        hits = conn.execute(
            "SELECT rowid FROM codex_conversation_fts WHERE codex_conversation_fts MATCH ?",
            ("recoverable",),
        ).fetchall()
        assert len(hits) == 1, "recovery must rebuild pre-recovery rows into the index"
    finally:
        conn.close()


def test_codex_fts_full_clear_empties_index_and_is_shadow_byte_idempotent():
    conn = _cache_schema()
    try:
        _insert_msg(conn, offset=1, text="clearable one")
        _insert_msg(conn, offset=2, text="clearable two", source_path="/p2")
        db._codex_conversation_fts_full_clear(conn)
        # Base + FTS both empty.
        assert conn.execute("SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_fts WHERE codex_conversation_fts MATCH ?",
            ("clearable",),
        ).fetchone()[0] == 0

        def shadow_dump() -> list[str]:
            return [
                line for line in conn.iterdump()
                if "codex_conversation_fts" in line
            ]

        first = shadow_dump()
        db._codex_conversation_fts_full_clear(conn)
        second = shadow_dump()
        assert first == second, "repeated full-clear must be shadow-table byte-idempotent"
    finally:
        conn.close()


# ── Task 3: digest contract (§3.1) ────────────────────────────────────────────


def test_digest_known_vectors_and_canonicalization():
    dom = kern.CODEX_CONVERSATION_DIGEST_DOMAIN
    assert dom == b"cctally-codex-conversation-digest-v1\0"
    # empty digests the domain-only prefix.
    assert kern.content_digest("") == hashlib.sha256(dom).hexdigest()[:32]
    assert kern.content_digest(None) == kern.content_digest("")
    # line-ending normalization ONLY.
    assert kern.content_digest("x\r\ny") == kern.content_digest("x\ny")
    assert kern.content_digest("x\ry") == kern.content_digest("x\ny")
    # whitespace/indentation preserved.
    assert kern.content_digest("a  b") != kern.content_digest("a b")
    assert kern.content_digest("\tcode") != kern.content_digest("code")
    # ANSI preserved (never stripped for the digest).
    assert kern.content_digest("\x1b[31mred") != kern.content_digest("red")
    # unicode over UTF-8 bytes.
    u = "héllo — 日本語"
    assert kern.content_digest(u) == hashlib.sha256(dom + u.encode("utf-8")).hexdigest()[:32]


def test_harness_marker_parser_is_closed_trailing_and_privacy_safe():
    clean, markers = kern._segment_harness_markers(
        "Visible prose.\n\n::git-push{cwd=\"/private/synthetic\" branch=\"feat/x\"}")
    assert clean == "Visible prose."
    assert markers == [
        {"schema_version": 1, "type": "git", "action": "push"}]
    assert "/private" not in json.dumps(markers)

    lookalikes = (
        "Inline ::git-stage{cwd=\"/private/inline\"} prose.\n"
        "```text\n::git-stage{cwd=\"/private/fenced\"}\n```\n"
        "::git-stage{cwd=\"/private/bad\" extra=\"unknown\"}")
    assert kern._segment_harness_markers(lookalikes) == (lookalikes, [])
    assert kern._parse_marker_directive(
        "::git-unknown{cwd=\"/private/unknown\"}") is None
    assert kern._parse_memory_citation([
        "<oai-mem-citation>", "<citation_entries>", "not a citation",
        "</citation_entries>", "<rollout_ids>", "</rollout_ids>",
        "</oai-mem-citation>",
    ]) is None
    # content_len is UTF-8 byte length of the canonical text.
    assert kern.content_len("日本") == len("日本".encode("utf-8")) == 6
    assert kern.content_len("x\r\ny") == kern.content_len("x\ny") == 3


def test_display_caps_are_equal_by_test_to_claude_constants():
    assert kern.CODEX_TEXT_CAP == lc._TOOL_RESULT_CAP
    assert kern.CODEX_TITLE_MAX == lcq._TITLE_MAX


# ── Task 3: taxonomy + sticky state (§4.1 / §4.2) ─────────────────────────────


def test_taxonomy_mapping_over_modern_full():
    result = _normalize("modern-full")
    rows = result.rows
    # session_meta / turn_context / token_count never normalize.
    assert not any(r.event_type in ("session_meta", "turn_context", "token_count") for r in rows)
    seen = {(r.record_family, r.event_type): r.kind for r in rows}
    assert seen[("response_item", "message")] in ("user", "assistant")
    assert seen[("response_item", "reasoning")] == "reasoning"
    assert seen[("response_item", "function_call")] == "tool_call"
    assert seen[("response_item", "function_call_output")] == "tool_output"
    assert seen[("response_item", "web_search_call")] == "tool_call"
    assert seen[("event_msg", "agent_message")] == "assistant"
    assert seen[("event_msg", "agent_reasoning")] == "reasoning"
    assert seen[("event_msg", "user_message")] == "user"
    assert seen[("event_msg", "task_started")] == "event"
    assert seen[("event_msg", "patch_apply_end")] == "event"
    # Both prose families are retained (never discarded at ingest).
    families = {r.record_family for r in rows if r.kind == "assistant"}
    assert families == {"response_item", "event_msg"}
    # Sticky turn + model stamped from turn_context.
    assert all(r.turn_id == "turn-a" for r in rows)
    assert all(r.model == MODEL for r in rows)
    # patch_apply_end feeds a file touch.
    assert any(t.file_path == "synthetic.txt" and t.tool == "apply_patch"
               for t in result.touches)


def test_patch_dict_changes_feed_normalized_file_touches():
    event = dataclasses.replace(
        _events("modern-full")[0],
        record_type="event_msg",
        event_type="patch_apply_end",
        line_offset=999,
        conversation_key="dict-patch-conversation",
        payload_json=json.dumps({"payload": {
            "type": "patch_apply_end",
            "changes": {
                "src/alpha.py": {"type": "update"},
                "src/beta.py": {"type": "add", "content": "new\n"},
            },
        }}),
    )

    result = kern.normalize_codex_events(
        [event], initial=kern.CodexStickyState())

    assert [
        (touch.file_path, touch.tool, touch.line_offset)
        for touch in result.touches
    ] == [
        ("src/alpha.py", "apply_patch", 999),
        ("src/beta.py", "apply_patch", 999),
    ]


def test_search_split_columns_route_by_kind():
    rows = _normalize("modern-full").rows
    for r in rows:
        if r.kind in ("user", "assistant"):
            assert r.search_tool == "" and r.search_thinking == ""
        elif r.kind == "reasoning":
            assert r.text == "" and r.search_tool == "" and r.search_thinking
        elif r.kind in ("tool_call", "tool_output", "event"):
            assert r.text == "" and r.search_thinking == "" and r.search_tool


def test_session_meta_resets_and_unknown_types_skip():
    # unknown-records has no session_meta -> identity-less -> zero rows.
    assert _normalize("unknown-records").rows == []
    # legacy-envelope is a bare token_count record with no thread identity.
    assert _normalize("legacy-envelope").rows == []


def test_sticky_turn_delta_resume_seam():
    events = _events("modern-full")
    # Split right after the turn_context record.
    split = next(i for i, e in enumerate(events) if e.record_type == "turn_context") + 1
    first = kern.normalize_codex_events(events[:split], initial=kern.CodexStickyState())
    assert first.terminal.turn_id == "turn-a"
    assert first.terminal.model == MODEL
    second = kern.normalize_codex_events(events[split:], initial=first.terminal)
    assert second.rows, "second batch must produce rows"
    # The first response_item row in the resumed batch inherits the sticky turn.
    first_resp = next(r for r in second.rows if r.record_family == "response_item")
    assert first_resp.turn_id == "turn-a"
    assert first_resp.model == MODEL


def test_field_level_degradation_keeps_the_row():
    ev = lj.CodexPhysicalEvent(
        source_path="/synthetic/root-a/x.jsonl", line_offset=1,
        source_root_key="root-a", conversation_key="conv-x",
        native_thread_id="native", root_thread_id="root", parent_thread_id=None,
        timestamp_utc="2026-07-14T12:00:00+00:00", record_type="response_item",
        event_type="message", turn_id=None, call_id=None,
        payload_json='{"payload": {"type": "message", "role": "assistant", "content": "not-a-list"}}',
    )
    result = kern.normalize_codex_events([ev], initial=kern.CodexStickyState())
    assert len(result.rows) == 1
    assert result.rows[0].kind == "assistant"
    assert result.rows[0].text == ""  # malformed content degrades to empty prose


# ── Task 3: mirror pairing / grouping / title (§5.2 / §5.3 / §4.3) ────────────


def _kept_texts(rows):
    kept, _ = kern.pair_mirrors(rows)
    return [r.text or (r.search_tool or r.search_thinking) for r in kept]


def test_mirror_pairing_shapes():
    rows = _normalize("mirror-pairing").rows
    kept, suppressed = kern.pair_mirrors(rows)
    kept_texts = _kept_texts(rows)

    # exact mirror pair: the event_msg member is suppressed, one canonical kept.
    assert kept_texts.count("Mirror assistant reply") == 1
    # non-mirror event prose survives.
    assert "Unique event-only note" in kept_texts
    # whitespace-sensitive variants never pair (both survive).
    assert "code x  y" in kept_texts and "code x y" in kept_texts
    # multiset: 1 response + 3 identical events -> one pairs, two survive.
    assert kept_texts.count("Triple echo") == 3
    # repeated identical prompts -> both survive (distinct offsets).
    assert kept_texts.count("Repeat prompt") == 2
    # distant identical cross-TURN rows never pair.
    assert kept_texts.count("Distant cross echo") == 2

    # over-cap distinct texts sharing a capped prefix: capped text collides,
    # digests differ, so they must NOT pair.
    over = [r for r in rows if len(r.text) == kern.CODEX_TEXT_CAP]
    assert len(over) == 2
    assert over[0].text == over[1].text  # capped display collides
    assert over[0].content_digest != over[1].content_digest  # pre-cap digest differs
    assert over[0] in kept and over[1] in kept


def test_unturned_adjacency_pairing():
    rows = _normalize("unturned-event-prose").rows
    kept_texts = _kept_texts(rows)
    # adjacent mirror pair collapses to one canonical.
    assert kept_texts.count("Unturned reply") == 1
    # unique event prose survives.
    assert "Solo unturned note" in kept_texts
    # non-adjacent duplicate (intervening same-kind row) retains BOTH.
    assert kept_texts.count("Coincidence") == 2


# ── #463 S1 (F5): reasoning containment beside exact-digest pairing ───────────
#
# Exact-digest pairing structurally cannot collapse the general reasoning case,
# because the two extraction paths build the identity differently: a
# response_item reasoning row's text is `"\n".join([summary, body])` (:1225) and
# an event_msg agent_reasoning row's is the raw single string (:1268). They
# coincide only when the body is empty and the raw text equals the summary
# exactly. The real relation is containment — one aggregate carries the parts
# that each arrived earlier as their own event.
#
# Measured 2026-08-02 against a read-only copy of the production store
# (conversations.db, 5.02 GB, 27,357 reasoning rows). Of the 17,149 turned
# event_msg reasoning rows, 4,883 are already suppressed by exact-digest
# pairing; ALL 12,266 that survive it are contained in exactly one same-turn
# aggregate, with 0 orphans and 0 present-but-not-contained. Rendered reasoning
# rows drop from 22,474 to 10,208, a 2.20x reduction (2.68x measured against the
# raw 27,357-row population, versus the 2.73x the epic predicted).


def _reasoning_row(*, family, turn, title=None, summary=None, body=None,
                   digest=None, offset=None):
    """One normalized reasoning row carrying the stored projection.

    The projection is written into ``detail_json`` — the same place
    ``_reasoning_projection`` puts it and the only place the containment pass is
    allowed to read. ``search_thinking`` is deliberately NOT the source: it is
    capped at 16,000 characters (:1444) and stores `summary + "\\n" + body` for a
    response item (:1225), so it cannot tell a title from a title-plus-body.
    """
    reasoning = {"schema_version": 1,
                 "source": "response_item" if family == "response_item" else "agent_reasoning"}
    if title is not None:
        reasoning["title"] = title
    if summary is not None:
        reasoning["summary"] = summary
    if body is not None:
        reasoning["body"] = body
    text = "\n".join(p for p in (title or summary or "", body or "") if p)
    _reasoning_row.counter = getattr(_reasoning_row, "counter", 0) + 1
    line = _reasoning_row.counter if offset is None else offset
    return kern.CodexNormalizedRow(
        conversation_key="ck", source_root_key="srk",
        source_path="/synthetic/root-a/rollout.jsonl", line_offset=line,
        timestamp_utc=f"2026-07-14T12:00:{line:02d}Z", turn_id=turn,
        call_id=None, kind="reasoning", event_type=None, record_family=family,
        model=MODEL, text="",
        content_digest=digest if digest is not None else kern.content_digest(
            f"{family}:{line}:{text}"),
        content_len=len(text),
        detail_json=json.dumps({"reasoning": reasoning}),
        search_tool="", search_thinking=text)


def test_event_msg_reasoning_contained_in_an_aggregate_is_suppressed():
    rows = [
        _reasoning_row(family="response_item", turn="t1",
                       title="Inspecting git worktree usage",
                       body="checked five roots"),
        _reasoning_row(family="event_msg", turn="t1",
                       title="Inspecting git worktree usage"),
    ]
    kept, suppressed = kern.pair_mirrors(rows)
    assert len(kept) == 1
    assert kept[0].record_family == "response_item"
    assert 1 in suppressed
    # The partner map is the contract search and find use to fold a suppressed
    # row onto its survivor, so a containment match must name its aggregate.
    assert kern.pair_mirror_partners(rows)[1] == 0


def test_orphan_event_msg_reasoning_is_never_suppressed():
    rows = [_reasoning_row(family="event_msg", turn="t1", title="Solo thought")]
    kept, suppressed = kern.pair_mirrors(rows)
    assert len(kept) == 1
    assert suppressed == set()


def test_reasoning_containment_never_crosses_a_turn():
    rows = [
        _reasoning_row(family="response_item", turn="t1", title="Shared heading"),
        _reasoning_row(family="event_msg", turn="t2", title="Shared heading"),
    ]
    kept, suppressed = kern.pair_mirrors(rows)
    assert len(kept) == 2 and suppressed == set()


def test_two_identical_event_rows_consume_the_aggregate_only_once():
    rows = [
        _reasoning_row(family="response_item", turn="t1", title="Same"),
        _reasoning_row(family="event_msg", turn="t1", title="Same"),
        _reasoning_row(family="event_msg", turn="t1", title="Same"),
    ]
    kept, _suppressed = kern.pair_mirrors(rows)
    assert len(kept) == 2, "multiset semantics: the aggregate covers ONE occurrence"


def test_an_aggregate_already_consumed_by_exact_digest_pairing_is_not_reused():
    """The two passes share one budget.

    Identical digests make the existing exact-digest pass claim the aggregate
    for the first event. The containment pass must treat that occurrence as
    spent rather than suppressing the second event as well.
    """
    rows = [
        _reasoning_row(family="response_item", turn="t1", title="Echo", digest="d"),
        _reasoning_row(family="event_msg", turn="t1", title="Echo", digest="d"),
        _reasoning_row(family="event_msg", turn="t1", title="Echo", digest="d"),
    ]
    kept, _suppressed = kern.pair_mirrors(rows)
    assert len(kept) == 2


def test_containment_never_spans_two_aggregates():
    """A concatenated match must not suppress a part no aggregate contains.

    Non-vacuity was demonstrated, not assumed. The event text is chosen so that
    joining the turn's aggregates contains it while neither aggregate alone
    does: because projected texts are already whitespace-normalized, the natural
    naive form is `" ".join(projected)`, which yields "Alpha Beta" and swallows
    this event. A probe implementation written that way was run against this
    test and it failed (`assert 2 == 3`) before the per-aggregate version
    replaced it.
    """
    rows = [
        _reasoning_row(family="response_item", turn="t1", title="Alpha"),
        _reasoning_row(family="response_item", turn="t1", title="Beta"),
        _reasoning_row(family="event_msg", turn="t1", body="Alpha\nBeta"),
    ]
    kept, _suppressed = kern.pair_mirrors(rows)
    assert len(kept) == 3


def test_containment_matches_the_production_multi_part_summary_shape():
    """The shape the production store actually holds (measured 2026-08-02).

    An aggregate's ``summary`` concatenates its bold-wrapped parts; each part
    arrived earlier as its own ``event_msg`` whose projection kept the wrapper.
    Both must fold onto that one aggregate.
    """
    rows = [
        _reasoning_row(family="event_msg", turn="t1",
                       body="**Inspecting git worktree usage**\n\n<!-- -->"),
        _reasoning_row(family="event_msg", turn="t1",
                       body="**Verifying complete output reads**\n\n<!-- -->"),
        _reasoning_row(
            family="response_item", turn="t1",
            summary=("**Inspecting git worktree usage**\n\n<!-- -->\n"
                     "**Verifying complete output reads**\n\n<!-- -->")),
    ]
    kept, suppressed = kern.pair_mirrors(rows)
    assert len(kept) == 1
    assert kept[0].record_family == "response_item"
    assert suppressed == {0, 1}


def test_non_reasoning_mirror_kinds_are_untouched_by_containment():
    """Containment is reasoning-specific; an assistant part inside a longer
    assistant row must still survive, because prose is not an aggregate."""
    def _assistant(family, text, line):
        return kern.CodexNormalizedRow(
            conversation_key="ck", source_root_key="srk",
            source_path="/synthetic/root-a/rollout.jsonl", line_offset=line,
            timestamp_utc=f"2026-07-14T13:00:{line:02d}Z", turn_id="t1",
            call_id=None, kind="assistant", event_type=None,
            record_family=family, model=MODEL, text=text,
            content_digest=kern.content_digest(text), content_len=len(text),
            detail_json=None, search_tool="", search_thinking="")

    rows = [_assistant("response_item", "hello world and more", 1),
            _assistant("event_msg", "hello world", 2)]
    kept, suppressed = kern.pair_mirrors(rows)
    assert len(kept) == 2 and suppressed == set()


def test_export_stops_repeating_a_reasoning_heading(tmp_path, monkeypatch):
    """End-to-end proof of the user-facing claim (#463 S1 / F5).

    The committed export golden does NOT move under this change, because no
    scenario in tests/fixtures/codex-parity/v1/rollouts/ carries the containment
    shape — its two reasoning rows hold unrelated text. That absence is why this
    test exists: without it the CHANGELOG's "the export stops repeating
    reasoning" would rest on kernel tests alone, with nothing exercising the
    rendered markdown.
    """
    records = _codex_turn_records([]) + [
        {"payload": {"text": "**Inspecting git worktree usage**",
                     "type": "agent_reasoning"},
         "timestamp": "2026-07-14T12:02:00Z", "type": "event_msg"},
        {"payload": {"text": "**Verifying complete output reads**",
                     "type": "agent_reasoning"},
         "timestamp": "2026-07-14T12:03:00Z", "type": "event_msg"},
        {"payload": {"type": "reasoning", "content": [],
                     "summary": [{"type": "summary_text",
                                  "text": "**Inspecting git worktree usage**"},
                                 {"type": "summary_text",
                                  "text": "**Verifying complete output reads**"}]},
         "timestamp": "2026-07-14T12:04:00Z", "type": "response_item"},
        {"payload": {"content": [{"text": "done", "type": "output_text"}],
                     "role": "assistant", "type": "message"},
         "timestamp": "2026-07-14T12:05:00Z", "type": "response_item"},
    ]
    ns, _root, _rollout = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        detail = q.get_codex_conversation(
            conn, ck, effective_speed="standard", limit=0, legacy_export=True)
        assert detail["status"] == "ok"
        markdown = cexport.render_codex_conversation_markdown(detail)
    finally:
        conn.close()
    assert markdown.count("Inspecting git worktree usage") == 1
    assert markdown.count("Verifying complete output reads") == 1


def test_rollup_item_count_over_mirror_and_wrapper_scenarios():
    mirror_rows = _normalize("mirror-pairing").rows
    # turn-m response item + 2 repeated prompts + turn-n response item = 4.
    assert kern.rollup_item_count(mirror_rows) == 4
    # 7 wrapper prompts (mirror-paired -> one logical each) + 1 meaningful = 8.
    assert kern.rollup_item_count(_normalize("title-wrapper-window").rows) == 8


def test_canonical_items_classes_and_grouping():
    rows = _normalize("mirror-pairing").rows
    kept, _ = kern.pair_mirrors(rows)
    items = kern.canonical_items(kept)
    klasses = [it["klass"] for it in items]
    assert klasses.count("response") == 2      # turn-m + turn-n
    assert klasses.count("prompt") == 2        # two Repeat prompt items
    # response item for turn-m bundles many assistant-side rows.
    turn_m = next(it for it in items if it["klass"] == "response" and it["turn_id"] == "turn-m")
    assert len(turn_m["rows"]) > 1


def test_derive_title_wrapper_window_and_null_case():
    # Meaningful prompt is beyond physical row 12 but inside logical prompt 12.
    assert kern.derive_title(_normalize("title-wrapper-window").rows) == "First meaningful title prompt"
    # mirror-pairing's first (and only) user prompt is a non-wrapper prompt.
    assert kern.derive_title(_normalize("mirror-pairing").rows) == "Repeat prompt"
    # unturned-event-prose has no user prompt at all -> NULL.
    assert kern.derive_title(_normalize("unturned-event-prose").rows) is None


def test_session_a_injected_taxonomy_and_turn_correlation_kernel():
    rows = _normalize("session-a-turn-contract").rows
    meta = [r for r in rows if r.kind == "meta"]
    assert len(meta) == 9
    labels = {
        json.loads(r.detail_json)["meta_label"]
        for r in meta
    }
    assert labels == {
        "permissions", "role", "mode", "plugins", "agents", "skill",
        "model_switch", "context_bundle",
    }
    assert any(json.loads(r.detail_json)["meta_kind"] == "skill" for r in meta)
    assert all(r.text for r in meta), "meta bodies stay available for detail/export"

    # The resumed portion has no turn_context before its response rows. The
    # later explicit patch/task-complete anchor proves they belong to turn-a.
    resumed = [
        r for r in rows
        if r.timestamp_utc and "10:00:22" <= r.timestamp_utc[11:19] <= "10:00:26"
    ]
    assert resumed and {r.turn_id for r in resumed} == {"turn-a"}

    # Unknown user-authored markup is not hidden by a loose XML heuristic.
    ev = lj.CodexPhysicalEvent(
        source_path="/synthetic/root-a/user.jsonl", line_offset=1,
        source_root_key="root-a", conversation_key="conv-user",
        native_thread_id="native", root_thread_id="root", parent_thread_id=None,
        timestamp_utc="2026-07-21T11:00:00Z", record_type="response_item",
        event_type="message", turn_id="turn-user", call_id=None,
        payload_json=json.dumps({"payload": {"type": "message", "role": "user",
                                              "content": [{"text": "<future_harness>user-authored</future_harness>"}]}}),
    )
    unknown = kern.normalize_codex_events(
        [ev], initial=kern.CodexStickyState()).rows[0]
    assert unknown.kind == "user"

    agentish_ev = dataclasses.replace(
        ev,
        line_offset=2,
        payload_json=json.dumps({"payload": {
            "type": "message", "role": "user",
            "content": [{"text": (
                "# AGENTS.md instructions for /synthetic/project\n\n"
                "<INSTRUCTIONS>synthetic policy</INSTRUCTIONS>\n"
                "This trailing prompt must remain user content."
            )}],
        }}),
    )
    agentish = kern.normalize_codex_events(
        [agentish_ev], initial=kern.CodexStickyState()).rows[0]
    assert agentish.kind == "user"

    bundle_text = (
        "<recommended_plugins>synthetic plugins</recommended_plugins>\n"
        "# AGENTS.md instructions for /synthetic/project\n\n"
        "<INSTRUCTIONS>synthetic project policy</INSTRUCTIONS>\n"
        "<environment_context>synthetic environment</environment_context>"
    )
    bundle_ev = dataclasses.replace(
        ev,
        line_offset=3,
        payload_json=json.dumps({"payload": {
            "type": "message", "role": "user",
            "content": [{"text": bundle_text}],
        }}),
    )
    bundle = kern.normalize_codex_events(
        [bundle_ev], initial=kern.CodexStickyState()).rows[0]
    assert bundle.kind == "meta"
    bundle_detail = json.loads(bundle.detail_json)
    assert bundle_detail == {
        "meta_kind": "context",
        "meta_label": "context_bundle",
        "meta_sections": ["plugins", "agents", "environment"],
    }

    agents_env_text = (
        "# AGENTS.md instructions for /synthetic/project\n\n"
        "<INSTRUCTIONS>synthetic project policy</INSTRUCTIONS>\n"
        "<environment_context>synthetic environment</environment_context>"
    )
    agents_env_ev = dataclasses.replace(
        ev,
        line_offset=4,
        payload_json=json.dumps({"payload": {
            "type": "message", "role": "user",
            "content": [{"text": agents_env_text}],
        }}),
    )
    agents_env = kern.normalize_codex_events(
        [agents_env_ev], initial=kern.CodexStickyState()).rows[0]
    assert agents_env.kind == "meta"
    assert json.loads(agents_env.detail_json)["meta_sections"] == [
        "agents", "environment"]


# ── Task 4: ingest integration ───────────────────────────────────────────────


def _split_namespace(ns):
    """Expose the split stores through this legacy integration-test surface."""
    open_core = ns["open_cache_db"]
    sync_core = ns["sync_codex_cache"]
    sync_claude_core = ns["sync_cache"]

    def open_split():
        core = open_core()
        core.close()
        return ns["open_conversations_db"]()

    def sync_split(_conn, **kwargs):
        core = open_core()
        try:
            stats = sync_core(core, **kwargs)
        finally:
            core.close()
        ns["sync_codex_conversations"](_conn, **kwargs)
        return stats

    ns["open_cache_db"] = open_split
    ns["sync_codex_cache"] = sync_split

    def sync_claude_split(_conn, **kwargs):
        core = open_core()
        try:
            stats = sync_claude_core(core, **kwargs)
        finally:
            core.close()
        ns["sync_claude_conversations"](_conn, **kwargs)
        return stats

    ns["sync_cache"] = sync_claude_split
    return ns


def _stage_codex_provider(tmp_path, monkeypatch, scenarios):
    """Stage one Codex provider root with the given scenarios as rollout files."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollouts = {}
    for scenario in scenarios:
        rollout = provider_root / "sessions" / "2026" / "07" / "15" / f"rollout-{scenario}.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CORPUS / "rollouts" / f"{scenario}.jsonl", rollout)
        rollouts[scenario] = rollout
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return _split_namespace(ns), provider_root, rollouts


def _codex_turn_records(tool_payloads, *, turn_id="turn-a"):
    """session_meta + turn_context + the given response_item payloads (in order) —
    a minimal single-turn synthetic rollout for kernel tests (§3.4 payload)."""
    recs = [
        {"payload": {"context_window": 272000,
                     "cwd": "/synthetic/root-a/project-red",
                     "git": {"branch": "b", "repository": "r"},
                     "id": "root-thread-x", "instructions": "x",
                     "model": "gpt-x", "model_context_window": 272000,
                     "model_provider": "p",
                     "session_id": "22222222-2222-4222-8222-222222222222",
                     "source": "codex", "thread_source": "root-thread-x",
                     "tools": [{"name": "t"}], "user": "u"},
         "timestamp": "2026-07-14T12:00:00Z", "type": "session_meta"},
        {"payload": {"model": "gpt-x", "model_context_window": 272000,
                     "turn_id": turn_id},
         "timestamp": "2026-07-14T12:01:00Z", "type": "turn_context"},
    ]
    for i, pl in enumerate(tool_payloads):
        recs.append({"payload": pl,
                     "timestamp": f"2026-07-14T12:{2 + i:02d}:00Z",
                     "type": "response_item"})
    return recs


def _stage_codex_records(tmp_path, monkeypatch, records):
    """Stage an arbitrary record list as one Codex rollout under a provider root."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout-custom.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    with rollout.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return _split_namespace(ns), provider_root, rollout


def test_ingest_writes_normalized_rows_rollup_touches(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        rows = conn.execute(
            "SELECT kind, turn_id, model, record_family FROM codex_conversation_messages"
        ).fetchall()
        assert rows, "normalized rows must be written"
        assert {r[0] for r in rows} >= {
            "user", "assistant", "reasoning", "tool_call", "tool_output", "event"}
        assert all(r[1] == "turn-a" for r in rows)
        assert all(r[2] == MODEL for r in rows)
        assert {"response_item", "event_msg"} <= {r[3] for r in rows}

        rollup = conn.execute(
            "SELECT conversation_key, item_count, title, project_key, models_json, "
            "started_utc, last_activity_utc FROM codex_conversation_rollups"
        ).fetchall()
        assert len(rollup) == 1
        _ck, item_count, title, project_key, models_json, started, last = rollup[0]
        assert item_count == 8
        assert title == "Synthetic first meaningful user prompt"
        assert project_key and project_key.startswith("project:")
        assert MODEL in (models_json or "")
        assert started and last

        touches = conn.execute(
            "SELECT file_path, tool FROM codex_conversation_file_touches").fetchall()
        assert ("synthetic.txt", "apply_patch") in touches
        # message linkage resolves to a real normalized row.
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_file_touches t "
            "JOIN codex_conversation_messages m ON m.id = t.message_id"
        ).fetchone()[0] == len(touches)

        assert conn.execute(
            "SELECT last_turn_id FROM codex_conversation_source_files"
        ).fetchone()[0] == "turn-a"
    finally:
        conn.close()


def test_dict_patch_ingest_populates_file_search_and_skips_orphans(
        tmp_path, monkeypatch):
    records = _codex_turn_records([])
    records.append({
        "payload": {
            "type": "patch_apply_end",
            "call_id": "patch-dict-1",
            "turn_id": "turn-a",
            "status": "completed",
            "changes": {
                "src/searchable-dict.py": {
                    "type": "update",
                    "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
                },
            },
            "success": True,
        },
        "timestamp": "2026-07-14T12:02:00Z",
        "type": "event_msg",
    })
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        touch = conn.execute(
            "SELECT t.message_id,t.file_path,t.tool,m.line_offset "
            "FROM codex_conversation_file_touches t "
            "JOIN codex_conversation_messages m ON m.id=t.message_id"
        ).fetchone()
        assert touch is not None
        assert touch[1:3] == ("src/searchable-dict.py", "apply_patch")
        assert touch[3] > 0

        search = q.search_codex_conversations(
            conn, "searchable-dict.py", kind="files",
            effective_speed="standard")
        assert search["status"] == "ok"
        assert search["total"] == 1
        assert search["hits"][0]["item_key"] is not None
        assert search["hits"][0]["snippet"] == "src/searchable-dict.py"

        # message_id is intentionally an application-level link, not a declared
        # foreign key. A stale orphan must not hide or duplicate the valid hit.
        conversation_key = search["hits"][0]["conversation_key"]
        conn.execute(
            "INSERT INTO codex_conversation_file_touches "
            "(message_id,conversation_key,source_path,file_path,tool) "
            "VALUES(?,?,?,?,?)",
            (touch[0] + 1_000_000, conversation_key, "/missing.jsonl",
             "src/searchable-dict.py", "apply_patch"),
        )
        search_with_orphan = q.search_codex_conversations(
            conn, "searchable-dict.py", kind="files",
            effective_speed="standard")
        assert search_with_orphan["total"] == 1
        assert search_with_orphan["hits"] == search["hits"]
    finally:
        conn.close()


def test_ingest_normalized_batch_is_atomic_and_next_sync_retries(tmp_path, monkeypatch):
    """A failed transcript batch rolls back and its independent cursor retries."""
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    denied = {"count": 0}

    def deny_first_msg_insert(action, arg1, _arg2, _db, _source):
        if action == sqlite3.SQLITE_INSERT and arg1 == "codex_conversation_messages":
            if denied["count"] == 0:
                denied["count"] += 1
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        conn.set_authorizer(deny_first_msg_insert)
        ns["sync_codex_cache"](conn)
        conn.set_authorizer(None)
        assert denied == {"count": 1}
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages"
        ).fetchone()[0] == 0
        ns["sync_codex_cache"](conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_rollups").fetchone()[0] == 1
    finally:
        conn.set_authorizer(None)
        conn.close()


def test_truncation_rederives_normalized_rows(tmp_path, monkeypatch):
    # Stage the LARGE mirror-pairing file first, then overwrite with the smaller
    # modern-full file so the size shrinks and the truncation-reset path fires.
    ns, _root, rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["mirror-pairing"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        before = conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0]
        assert before > 0
        shutil.copyfile(CORPUS / "rollouts" / "modern-full.jsonl", rollouts["mirror-pairing"])
        ns["sync_codex_cache"](conn)
        texts = {r[0] for r in conn.execute(
            "SELECT text FROM codex_conversation_messages WHERE text != ''")}
        assert "Mirror assistant reply" not in texts        # old content gone
        assert "Synthetic assistant response" in texts       # new content present
        # rollup re-derived for the new conversation only.
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_rollups").fetchone()[0] == 1
    finally:
        conn.close()


def test_rebuild_with_empty_root_preserves_all_three_normalized_tables(
    tmp_path, monkeypatch,
):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] > 0
        tables = (
            "codex_conversation_messages",
            "codex_conversation_file_touches",
            "codex_conversation_rollups",
        )
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        before_fts = conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_fts "
            "WHERE codex_conversation_fts MATCH 'Synthetic'"
        ).fetchone()[0]

        # #485: an empty root is unknown filesystem state, not permission to
        # erase the only retained normalized transcript generation.
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-root"))
        refused = ns["sync_codex_cache"](conn, rebuild=True)
        assert refused.prune_refused is True
        assert {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        } == before
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_fts "
            "WHERE codex_conversation_fts MATCH 'Synthetic'"
        ).fetchone()[0] == before_fts
    finally:
        conn.close()


def test_orphan_prune_repairs_rollups_and_survivor_stays_searchable(tmp_path, monkeypatch):
    ns, _root, rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["modern-full", "mirror-pairing"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_rollups").fetchone()[0] == 2
        # Delete conversation A (mirror-pairing) from disk -> orphan prune.
        rollouts["mirror-pairing"].unlink()
        ns["sync_codex_cache"](conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_rollups").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages "
            "WHERE text = 'Mirror assistant reply'").fetchone()[0] == 0
        # Survivor B (modern-full) stays searchable in FTS AND in LIKE mode.
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_fts "
            "WHERE codex_conversation_fts MATCH 'Synthetic'").fetchone()[0] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages "
            "WHERE text LIKE '%Synthetic%'").fetchone()[0] > 0
        # And conversation A left no FTS residue.
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_fts "
            "WHERE codex_conversation_fts MATCH 'Mirror'").fetchone()[0] == 0
    finally:
        conn.close()


# ── Task 5: detail / outline assembly (§5.2 / §5.4 / §5.5 / §5.6) ─────────────


def _single_ck(conn) -> str:
    row = conn.execute(
        "SELECT DISTINCT conversation_key FROM codex_conversation_messages").fetchall()
    assert len(row) == 1, f"expected one conversation, got {len(row)}"
    return row[0][0]


# --- item_key algebra (pure, §5.2) ------------------------------------------


def test_item_key_prompt_and_response_share_turn_but_differ():
    resp = q.codex_item_key(
        "conv-x", klass="response", turn_id="turn-a",
        source_path=None, line_offset=None, content_digest=None)
    prompt = q.codex_item_key(
        "conv-x", klass="prompt", turn_id="turn-a",
        source_path="/p", line_offset=3, content_digest="d1")
    assert resp != prompt


def test_item_key_response_is_durable_turn_identity():
    # Same-turn content replacement (different offset + digest) keeps the key —
    # response keys represent durable native-turn identity, not a content gen.
    k1 = q.codex_item_key(
        "conv-x", klass="response", turn_id="turn-a",
        source_path="/p", line_offset=4, content_digest="d1")
    k2 = q.codex_item_key(
        "conv-x", klass="response", turn_id="turn-a",
        source_path="/p2", line_offset=99, content_digest="d2")
    assert k1 == k2


def test_item_key_row_class_offset_scoped_and_independent():
    # Repeated identical prompts -> distinct keys (different offsets); each key is
    # a pure function of its own row, so deleting an earlier duplicate or an
    # out-of-order multi-file append leaves it unchanged.
    a = q.codex_item_key("conv-x", klass="prompt", turn_id=None,
                         source_path="/p", line_offset=1, content_digest="d")
    b = q.codex_item_key("conv-x", klass="prompt", turn_id=None,
                         source_path="/p", line_offset=2, content_digest="d")
    assert a != b
    assert a == q.codex_item_key("conv-x", klass="prompt", turn_id=None,
                                 source_path="/p", line_offset=1, content_digest="d")


def test_item_key_same_offset_replacement_changes_key():
    before = q.codex_item_key("conv-x", klass="prompt", turn_id=None,
                              source_path="/p", line_offset=1, content_digest="old")
    after = q.codex_item_key("conv-x", klass="prompt", turn_id=None,
                             source_path="/p", line_offset=1, content_digest="new")
    assert before != after


def test_item_key_never_leaks_raw_path():
    key = q.codex_item_key("conv-x", klass="prompt", turn_id=None,
                           source_path="/secret/dir/private.jsonl", line_offset=1,
                           content_digest="d")
    assert "/secret/" not in key and "private.jsonl" not in key


# --- detail item grouping / anchors -----------------------------------------


def test_detail_items_grouping_and_distinct_keys(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        d = q.get_codex_conversation(conn, ck, effective_speed="standard")
        assert d["status"] == "ok"
        assert d["conversation_key"] == ck
        assert d["page"]["total"] == 8
        keys = [it["item_key"] for it in d["items"]]
        assert len(keys) == len(set(keys)) == 8    # every canonical item distinct
        prompts = [it for it in d["items"] if it["kind"] == "user"]
        responses = [it for it in d["items"] if it["kind"] == "assistant"]
        events = [it for it in d["items"] if it["kind"] == "event"]
        assert len(prompts) == 2 and len(responses) == 1 and len(events) == 5
        assert responses[0]["lifecycle"]["state"] == "started"
        # prompt + response share turn-a yet key differently.
        assert {it["item_key"] for it in prompts}.isdisjoint(
            {responses[0]["item_key"]})
        assert responses[0]["model"] == MODEL
    finally:
        conn.close()


def test_detail_tool_output_folds_into_tool_call(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        d = q.get_codex_conversation(conn, _single_ck(conn), effective_speed="standard")
        response = next(it for it in d["items"] if it["kind"] == "assistant")
        blocks = response["blocks"]
        # tool_output rows fold away — never standalone blocks.
        assert not any(b["kind"] == "tool_output" for b in blocks)
        fn = next(b for b in blocks if b.get("call_id") == "fn-1")
        assert fn["kind"] == "tool_call" and fn["output"]["text"] == '{"ok":true}'
        # web_search_call (call_id None) stays a standalone tool_call, no output.
        ws = next(b for b in blocks
                  if b["kind"] == "tool_call" and b.get("call_id") is None)
        assert "output" not in ws
    finally:
        conn.close()


# --- cost attribution (§5.4) ------------------------------------------------


@pytest.mark.parametrize("speed", ["standard", "fast"])
def test_detail_cost_reconciles_and_cross_checks(tmp_path, monkeypatch, speed):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        d = q.get_codex_conversation(conn, ck, effective_speed=speed)
        per_item = sum(it["cost_usd"] or 0.0 for it in d["items"])
        assert abs(per_item + d["unattributed_cost_usd"] - d["total_cost_usd"]) < 1e-9
        # modern-full: single accounting row after turn_context -> fully attributed.
        assert abs(d["unattributed_cost_usd"]) < 1e-12
        assert d["total_cost_usd"] > 0
        # cross-check vs codex-session identity (one file = one session = one conv).
        sid = conn.execute(
            "SELECT session_id FROM codex_session_entries").fetchone()[0]
        expected = sum(
            pricing._calculate_codex_entry_cost(m or "", i, c, o, r, speed=speed)
            for m, i, c, o, r in conn.execute(
                "SELECT model, input_tokens, cached_input_tokens, output_tokens, "
                "reasoning_output_tokens FROM codex_session_entries WHERE session_id = ?",
                (sid,)))
        assert abs(d["total_cost_usd"] - expected) < 1e-9
    finally:
        conn.close()


@pytest.mark.parametrize("speed", ["standard", "fast"])
def test_detail_unattributed_bucket_for_unturned(tmp_path, monkeypatch, speed):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["unturned-event-prose"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        d = q.get_codex_conversation(conn, _single_ck(conn), effective_speed=speed)
        # No turn_context ever -> every accounting row lands in the unattributed
        # bucket; no item carries a per-turn cost.
        assert d["total_cost_usd"] > 0
        assert abs(d["unattributed_cost_usd"] - d["total_cost_usd"]) < 1e-9
        assert all(it["cost_usd"] is None for it in d["items"])
        per_item = sum(it["cost_usd"] or 0.0 for it in d["items"])
        assert abs(per_item + d["unattributed_cost_usd"] - d["total_cost_usd"]) < 1e-9
    finally:
        conn.close()


def test_detail_tokens_are_provider_native(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        d = q.get_codex_conversation(conn, _single_ck(conn), effective_speed="standard")
        assert d["tokens"] == {
            "source": "codex", "input": 1200, "output": 400,
            "cached_input": 300, "reasoning_output": 100}
        # NEVER relabeled into Claude cache vocabulary (S0).
        assert "cache_read" not in d["tokens"] and "cache_create" not in d["tokens"]
        # the carrying response item exposes the same native token union.
        response = next(it for it in d["items"] if it["cost_usd"] is not None)
        assert response["tokens"]["source"] == "codex"
        assert response["tokens"]["reasoning_output"] == 100
    finally:
        conn.close()


# --- threading (§5.5) -------------------------------------------------------


def test_threading_parent_children_from_metadata(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["nested-parent", "nested-child"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        parent_ck = conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE native_thread_id = 'parent-thread-fixture'").fetchone()[0]
        child_ck = conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE parent_thread_id = 'parent-thread-fixture' "
            "AND native_thread_id != 'parent-thread-fixture'").fetchone()[0]

        pd = q.get_codex_conversation(conn, parent_ck, effective_speed="standard")
        assert [c["conversation_key"] for c in pd["children"]] == [child_ck]
        child = pd["children"][0]
        assert child["title"] == "Child thread question"
        assert child["item_count"] == 2
        assert child["cost_usd"] > 0
        assert pd["parent"] is None

        cd = q.get_codex_conversation(conn, child_ck, effective_speed="standard")
        assert cd["parent"] == {"conversation_key": parent_ck,
                                "title": "Parent thread question"}
        assert cd["children"] == []
    finally:
        conn.close()


# --- detail paging (§5.6) ---------------------------------------------------


def _detail_keys(detail) -> list[str]:
    return [it["item_key"] for it in detail["items"]]


# ── #463 S1: three-phase assembly + segmentation, end to end ─────────────────


def _big_turn_records(tool_calls: int = 120, *, turn_id="turn-a"):
    """A single turn far past the 40-block segment budget.

    Each call/output pair folds into ONE block, so the turn's block count is
    about ``tool_calls`` plus the surrounding prose — enough to force several
    segments while staying small enough to read in a failure message.
    """
    records = _codex_turn_records([], turn_id=turn_id)
    minute = 2
    for i in range(tool_calls):
        records.append({
            "payload": {"arguments": "{}", "call_id": f"fn-{i}",
                        "name": "fixture_function", "type": "function_call"},
            "timestamp": f"2026-07-14T12:{minute:02d}:{i % 60:02d}Z",
            "type": "response_item"})
        records.append({
            "payload": {"call_id": f"fn-{i}", "output": {"ok": True},
                        "type": "function_call_output"},
            "timestamp": f"2026-07-14T12:{minute + 1:02d}:{i % 60:02d}Z",
            "type": "response_item"})
        minute += 2
    return records


def _detail_of(conn, ck, **kwargs):
    return q.get_codex_conversation(conn, ck, effective_speed="standard", **kwargs)


def test_a_large_turn_is_served_as_several_bounded_segments(tmp_path, monkeypatch):
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _big_turn_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        detail = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    responses = [it for it in detail["items"] if it["kind"] == "assistant"]
    assert len(responses) > 1, "the oversized turn must split into segments"
    # One turn, so every segment shares one turn_item_key and the ordinals run
    # from zero without a gap.
    turn_keys = {it["turn_item_key"] for it in responses}
    assert len(turn_keys) == 1
    assert [it["segment_ordinal"] for it in responses] == list(range(len(responses)))
    # Segment 0 inherits the turn key unchanged; later segments do not.
    assert responses[0]["item_key"] == responses[0]["turn_item_key"]
    assert all(it["item_key"] != it["turn_item_key"] for it in responses[1:])
    assert len({it["item_key"] for it in detail["items"]}) == len(detail["items"])
    # The ceiling is the budget plus at most one maximal fold group; with
    # single-block groups that is just the budget.
    for item in responses:
        assert len(item["blocks"]) <= 40


def test_segmentation_does_not_move_a_turns_cost_carrier(tmp_path, monkeypatch):
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _big_turn_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        detail = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    responses = [it for it in detail["items"] if it["kind"] == "assistant"]
    # Every non-carrier segment reports null, never zero: a zero is
    # indistinguishable from a genuinely free turn.
    assert all(it["cost_usd"] is None and it["tokens"] is None
               for it in responses[1:])


_APPLY_PATCH_INPUT = (
    "*** Begin Patch\n"
    "*** Update File: synthetic.txt\n"
    "@@\n"
    "-old line\n"
    "+new line\n"
    "*** End Patch"
)


def _fold_shape_records(*, turn_id="turn-a", filler=0, web_owners=2):
    """One turn carrying BOTH fold shapes that id-matching alone does not cover.

    Shape 1 is a native patch completion whose INNER call id differs from the
    outer custom-tool call, which ``_item_blocks_with_rows`` folds by positional
    bracketing — call < event < that call's output — rather than by id. It is the
    common shape in real rollouts: 3,441 of the 4,690 patch completion events in
    the production corpus carry a call id no ``tool_call`` in their turn owns.

    Shape 2 is a ``web_search_completion`` whose call id is owned by
    ``web_owners`` calls, exactly ONE of which is the ``web_search_call``. That
    path narrows its candidates by ``detail.name == "web_search_call"`` BEFORE
    requiring a unique candidate, and imposes no bound on how many calls share
    the id, so it folds at ANY owner count. A registration gate that names a
    fixed count — ``== 1``, or the ``== 2`` this fixture used to be the only
    witness for — leaves the pair ungrouped at every other count, and a segment
    boundary can then fall between the call and its completion.
    """
    recs = _codex_turn_records([], turn_id=turn_id)
    clock = [2]

    def add(record_type, payload):
        minute = clock[0]
        recs.append({
            "payload": payload,
            "timestamp": f"2026-07-14T{12 + minute // 60:02d}:{minute % 60:02d}:00Z",
            "type": record_type,
        })
        clock[0] += 1

    for index in range(filler):
        add("response_item", {"type": "function_call", "call_id": f"pad-{index}",
                              "name": "fixture_function", "arguments": "{}"})
        add("response_item", {"type": "function_call_output",
                              "call_id": f"pad-{index}", "output": {"ok": True}})
    add("response_item", {"type": "custom_tool_call", "call_id": "patch-call-1",
                          "name": "apply_patch", "input": _APPLY_PATCH_INPUT,
                          "status": "completed"})
    add("event_msg", {"type": "patch_apply_end", "call_id": "exec-inner-1",
                      "turn_id": turn_id, "status": "completed",
                      "changes": [{"path": "synthetic.txt"}],
                      "stdout": "ok", "stderr": "", "success": True})
    add("response_item", {"type": "custom_tool_call_output",
                          "call_id": "patch-call-1", "output": {"ok": True}})
    # ``web_owners - 1`` plain function calls share the web search's id, so the
    # turn-scoped owner count is exactly ``web_owners`` and exactly one of those
    # owners is the ``web_search_call`` the completion narrows to.
    for extra in range(web_owners - 1):
        name = "fixture_function" if extra == 0 else f"fixture_function_{extra}"
        add("response_item", {"type": "function_call", "call_id": "web-1",
                              "name": name, "arguments": "{}"})
        add("response_item", {"type": "function_call_output", "call_id": "web-1",
                              "output": {"ok": True}})
    add("response_item", {"type": "web_search_call", "id": "web-1",
                          "status": "completed",
                          "action": {"type": "search", "query": "synthetic query",
                                     "queries": ["synthetic query"]}})
    add("event_msg", {"type": "web_search_end", "call_id": "web-1",
                      "query": "synthetic query",
                      "action": {"type": "search", "query": "synthetic query"},
                      "results": [{"type": "computer_initialize_state",
                                   "domain": "example.test",
                                   "ref_id": "turn0search0",
                                   "snippet": "Synthetic result",
                                   "title": "Synthetic title",
                                   "url": "https://example.test/result"}]})
    return recs


def _block_signature(env):
    """Per-turn block signature, concatenated across that turn's segments."""
    by_turn: dict[str, list] = {}
    for item in env["items"]:
        by_turn.setdefault(item["turn_item_key"], [])
        for block in item["blocks"]:
            detail = block.get("detail") or {}
            card = detail.get("card") if isinstance(detail, dict) else None
            by_turn[item["turn_item_key"]].append((
                block["kind"],
                block.get("call_id"),
                card.get("type") if isinstance(card, dict) else None,
                isinstance(card, dict) and "completion" in card,
                (block.get("output") or {}).get("text"),
            ))
    return by_turn


def test_the_whole_turn_and_the_segments_emit_the_same_blocks(tmp_path, monkeypatch):
    """The page-local builder must agree with the whole-turn builder.

    This is what fold-group atomicity, physical contiguity and the turn-scoped
    ``call_owner_count`` buy: concatenating the segments' blocks reproduces
    exactly what the unsegmented path emits for the same turn.

    The reference is the SAME code path with a large ``SEGMENT_BLOCK_BUDGET``,
    not ``legacy_export=True``. That flag also changes ``fold_patch_completions``,
    ``preserve_marker_text`` and the payload filter, so a comparison against it
    is not single-variable and could pass or fail for a reason unrelated to
    segmentation.

    The fixture carries both fold shapes id-matching alone misses, and the
    budget is swept across every value that produces a boundary, so a boundary
    lands at the tightest possible point rather than at one hand-picked place.
    """
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _fold_shape_records(filler=6))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        monkeypatch.setattr(q.segkern, "SEGMENT_BLOCK_BUDGET", 10 ** 6)
        whole = _block_signature(_detail_of(conn, ck, limit=0))
        assert any(len(sig) > 4 for sig in whole.values()), (
            "the unsegmented reference must hold enough blocks to be split")
        for budget in range(1, 24):
            monkeypatch.setattr(q.segkern, "SEGMENT_BLOCK_BUDGET", budget)
            split = _block_signature(_detail_of(conn, ck, limit=0))
            assert split == whole, f"segment budget {budget} changed the blocks"
    finally:
        conn.close()


def test_a_boundary_never_splits_a_bracketed_patch_completion(tmp_path, monkeypatch):
    """A patch completion carrying an inner call id must stay with its call.

    ``_item_blocks_with_rows`` folds it by positional bracketing rather than by
    id, so an id-only fold group leaves the event in a group of its own and a
    boundary between the two makes the page-local builder emit a standalone
    event card where the whole-turn builder emits a folded ``completion``.
    """
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _fold_shape_records(filler=6))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        for budget in range(1, 24):
            monkeypatch.setattr(q.segkern, "SEGMENT_BLOCK_BUDGET", budget)
            detail = _detail_of(conn, ck, limit=0)
            blocks = [b for it in detail["items"] for b in it["blocks"]]
            patch = [b for b in blocks
                     if isinstance((b.get("detail") or {}).get("card"), dict)
                     and b["detail"]["card"].get("type") == "patch"
                     and b["kind"] == "tool_call"]
            assert len(patch) == 1, f"budget {budget}: {len(patch)} patch calls"
            assert "completion" in patch[0]["detail"]["card"], (
                f"budget {budget}: the patch completion did not fold")
            assert not any(
                b["kind"] == "event"
                and isinstance((b.get("detail") or {}).get("card"), dict)
                and b["detail"]["card"].get("source") == "patch_apply_end"
                for b in blocks), f"budget {budget}: a standalone patch event"
    finally:
        conn.close()


def test_find_projection_reuses_the_actual_native_completion_fold_owner(
    tmp_path, monkeypatch
):
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _fold_shape_records(filler=1, web_owners=3)
    )
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        detail = _detail_of(conn, ck, limit=0)
        owner_by_event = {}
        for item in detail["items"]:
            for block in item["blocks"]:
                card = (block.get("detail") or {}).get("card")
                completion = card.get("completion") if isinstance(card, dict) else None
                if isinstance(completion, dict) and completion.get("event_block_key"):
                    owner_by_event[completion["event_block_key"]] = block["block_key"]
        assert len(owner_by_event) == 2

        projected = {
            block_key: container_block_key
            for block_key, container_block_key in conn.execute(
                "SELECT block_key,container_block_key FROM codex_find_projection "
                "WHERE conversation_key=? AND surface='completion'",
                (ck,),
            )
        }
        assert projected == owner_by_event
    finally:
        conn.close()


def test_find_projection_scopes_reused_mcp_completion_ids_to_their_turn(
    tmp_path, monkeypatch
):
    records = _codex_turn_records([
        {"type": "function_call", "name": "fixture_search_issues",
         "call_id": "reused-mcp", "arguments": "{\"state\":\"open\"}"},
    ], turn_id="turn-one")
    records.extend([
        {
            "type": "event_msg", "timestamp": "2026-07-14T12:03:00Z",
            "payload": {
                "type": "mcp_tool_call_end", "call_id": "reused-mcp",
                "invocation": {"server": "fixture", "tool": "search_issues",
                               "arguments": {"state": "open"}},
                "result": {"Ok": {"content": [{"type": "text", "text": "one"}]}},
            },
        },
        {
            "type": "turn_context", "timestamp": "2026-07-14T12:04:00Z",
            "payload": {"model": "gpt-x", "model_context_window": 272000,
                        "turn_id": "turn-two"},
        },
        {
            "type": "response_item", "timestamp": "2026-07-14T12:05:00Z",
            "payload": {"type": "function_call", "name": "fixture_search_issues",
                        "call_id": "reused-mcp", "arguments": "{\"state\":\"closed\"}"},
        },
        {
            "type": "event_msg", "timestamp": "2026-07-14T12:06:00Z",
            "payload": {
                "type": "mcp_tool_call_end", "call_id": "reused-mcp",
                "invocation": {"server": "fixture", "tool": "search_issues",
                               "arguments": {"state": "closed"}},
                "result": {"Ok": {"content": [{"type": "text", "text": "two"}]}},
            },
        },
    ])
    ns, _root, _rollout = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        detail = _detail_of(conn, ck, limit=0)
        owner_by_event = {}
        for item in detail["items"]:
            for block in item["blocks"]:
                card = (block.get("detail") or {}).get("card")
                completion = card.get("completion") if isinstance(card, dict) else None
                if isinstance(completion, dict) and completion.get("event_block_key"):
                    owner_by_event[completion["event_block_key"]] = block["block_key"]
        assert len(owner_by_event) == 2
        assert len(set(owner_by_event.values())) == 2
        projected = dict(conn.execute(
            "SELECT block_key,container_block_key FROM codex_find_projection "
            "WHERE conversation_key=? AND surface='completion'",
            (ck,),
        ))
        assert projected == owner_by_event
    finally:
        conn.close()


def test_find_projection_uses_visible_first_occurrence_reasoning_headings(
    tmp_path, monkeypatch
):
    records = _codex_turn_records([
        {
            "type": "reasoning",
            "summary": [
                {"type": "summary_text", "text": "Alpha heading"},
                {"type": "summary_text", "text": "Beta heading"},
            ],
            "content": [],
        },
        {
            "type": "reasoning",
            "summary": [
                {"type": "summary_text", "text": "Alpha heading"},
                {"type": "summary_text", "text": "Beta heading"},
                {"type": "summary_text", "text": "Gamma heading"},
            ],
            "content": [],
        },
    ])
    ns, _root, _rollout = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        alpha = q.find_occurrences_in_codex_conversation(
            conn, ck, "Alpha heading", regex=False, case_sensitive=True,
            kind="thinking",
        )
        gamma = q.find_occurrences_in_codex_conversation(
            conn, ck, "Gamma heading", regex=False, case_sensitive=True,
            kind="thinking",
        )
        assert alpha["total"] == 1
        assert gamma["total"] == 1
        assert alpha["page"]["occurrences"][0]["fragments"] == [
            {"leaf_key": "headings.0", "start": 0, "end": 13}
        ]
        assert gamma["page"]["occurrences"][0]["fragments"] == [
            {"leaf_key": "headings.2", "start": 0, "end": 13}
        ]
    finally:
        conn.close()


@pytest.mark.parametrize("web_owners", [2, 3])
def test_a_boundary_never_splits_a_web_search_completion(
        tmp_path, monkeypatch, web_owners):
    """``web_search_completion`` folds at ANY owner count, so grouping must too.

    The completion path narrows its candidates by ``detail.name ==
    "web_search_call"`` BEFORE requiring a unique candidate, and it bounds
    nothing about how many calls share the id. A call id owned by two calls of
    which one is the web search folds there, and so does a call id owned by
    three. The registration gate in ``_fold_groups_for_item`` therefore
    registers the web-search arm at ``owners >= 1``: an ``== 1`` gate never
    groups the two-owner pair and an ``== 2`` gate never groups the three-owner
    one, and in each ungrouped case a segment boundary can fall between the call
    and its completion, which makes the page-local builder emit a standalone
    event card where the whole-turn builder emits a folded ``completion``.
    """
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _fold_shape_records(filler=6, web_owners=web_owners))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        # Non-vacuity: the fixture really carries the owner count under test.
        rows = q._load_conversation_rows(conn, ck)
        owners = q._turn_scoped_call_owner_count(rows)
        assert owners.get("web-1") == web_owners, (
            f"expected {web_owners} owners of web-1, got {owners.get('web-1')}")
        for budget in range(1, 26):
            monkeypatch.setattr(q.segkern, "SEGMENT_BLOCK_BUDGET", budget)
            detail = _detail_of(conn, ck, limit=0)
            blocks = [b for it in detail["items"] for b in it["blocks"]]
            web = [b for b in blocks
                   if isinstance((b.get("detail") or {}).get("card"), dict)
                   and b["detail"]["card"].get("type") == "web_search"]
            assert len(web) == 1, f"budget {budget}: {len(web)} web search cards"
            assert "completion" in web[0]["detail"]["card"], (
                f"budget {budget}: the web search completion did not fold")
            assert not any(
                b["kind"] == "event"
                and (b.get("detail") or {}).get("event") == "web_search_end"
                for b in blocks), f"budget {budget}: a standalone web search event"
    finally:
        conn.close()


def test_a_segments_rows_are_a_contiguous_physical_range(tmp_path, monkeypatch):
    """No segment interleaves with another.

    Fold-group atomicity alone does not give this. Because folds are
    non-adjacent, a group's rows can bracket a later group's rows, so a boundary
    drawn between the two groups produces segments whose physical row ranges
    overlap. Spec section 1 requires each segment to cover a contiguous physical
    row range instead.
    """
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _fold_shape_records(filler=6))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        for budget in (1, 2, 3, 4, 6, 8):
            monkeypatch.setattr(q.segkern, "SEGMENT_BLOCK_BUDGET", budget)
            rows, detail_bytes = q._load_conversation_index_rows(conn, ck)
            kept, _suppressed = kern.pair_mirrors(rows)
            items = kern.canonical_items(kept, fold_patch_completions=True)
            index = q._build_segment_index(ck, items, detail_bytes, segmented=True)
            by_item: dict[int, list] = {}
            for entry in index:
                by_item.setdefault(entry["_item_index"], []).append(entry)
            for item_index, entries in by_item.items():
                item = items[item_index]
                lifecycle = {(r.source_path, r.line_offset)
                             for r in item.get("lifecycle_rows", [])}
                # Position within the ITEM's own non-lifecycle rows: a turn's
                # rows need not be contiguous in the conversation, because its
                # user, event and meta rows become separate items.
                order = {
                    (r.source_path, r.line_offset): i
                    for i, r in enumerate(
                        r for r in item["rows"]
                        if (r.source_path, r.line_offset) not in lifecycle)
                }
                covered = []
                for entry in entries:
                    positions = [order[(r.source_path, r.line_offset)]
                                 for r in entry["_rows"]]
                    assert positions == sorted(positions), (
                        f"budget {budget}: segment {entry['segment_ordinal']} "
                        "rows are out of physical order")
                    if positions:
                        assert positions == list(
                            range(positions[0], positions[-1] + 1)), (
                            f"budget {budget}: segment "
                            f"{entry['segment_ordinal']} of "
                            f"{entry['turn_item_key'][:16]} is not contiguous — "
                            f"{positions}")
                    covered.extend(positions)
                assert covered == list(range(len(order))), (
                    f"budget {budget}: the segments of item {item_index} are not "
                    "a partition of its rows into consecutive ranges")
    finally:
        conn.close()


def test_segment_timestamps_are_non_decreasing_across_a_turn(tmp_path, monkeypatch):
    """S2 through S5 are told they may rely on this.

    It follows from physical contiguity: rows are ordered by
    ``(timestamp_utc, source_path, line_offset)``, so consecutive physical
    ranges carry non-decreasing timestamps.

    The published wire property — non-decreasing segment ``timestamp_utc`` —
    is asserted first, but on its own it is NOT falsifiable by interleaving,
    because a segment anchors on its first row and first rows increase whether
    or not the ranges overlap. The falsifiable statement is that a segment's
    LAST row never comes after the next segment's first row, so both are
    asserted.
    """
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _fold_shape_records(filler=6))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        for budget in (1, 2, 3, 4, 6, 8):
            monkeypatch.setattr(q.segkern, "SEGMENT_BLOCK_BUDGET", budget)
            detail = _detail_of(conn, ck, limit=0)
            by_turn: dict[str, list] = {}
            for item in detail["items"]:
                by_turn.setdefault(item["turn_item_key"], []).append(item)
            for turn, segments in by_turn.items():
                stamps = [it["timestamp_utc"] for it in segments]
                assert stamps == sorted(stamps), (
                    f"budget {budget}: {turn[:16]} segment timestamps {stamps}")

            rows, detail_bytes = q._load_conversation_index_rows(conn, ck)
            kept, _suppressed = kern.pair_mirrors(rows)
            items = kern.canonical_items(kept, fold_patch_completions=True)
            index = q._build_segment_index(ck, items, detail_bytes, segmented=True)
            spans: dict[str, list] = {}
            for entry in index:
                stamps = [r.timestamp_utc for r in entry["_rows"]]
                if stamps:
                    spans.setdefault(entry["turn_item_key"], []).append(
                        (min(stamps), max(stamps)))
            for turn, ranges in spans.items():
                for (_lo, hi), (lo_next, _hi_next) in zip(ranges, ranges[1:]):
                    assert hi <= lo_next, (
                        f"budget {budget}: {turn[:16]} segments overlap in time "
                        f"— one ends at {hi}, the next starts at {lo_next}")
    finally:
        conn.close()


def test_legacy_export_emits_exactly_one_segment_per_item(tmp_path, monkeypatch):
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _big_turn_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        whole = _detail_of(conn, ck, limit=0, legacy_export=True)
    finally:
        conn.close()
    assert all(it["segment_ordinal"] == 0 for it in whole["items"])
    assert len([it for it in whole["items"] if it["kind"] == "assistant"]) == 1


def test_a_page_is_bounded_by_blocks_even_when_the_item_count_is_not(
        tmp_path, monkeypatch):
    """The page budget is wired into assembly, not just into the paginator.

    The budget is lowered for the fixture rather than staged at production
    scale: reproducing the profiled 78-item, 3,120-block page honestly would
    need a rollout of several thousand records, and the arithmetic at the real
    2,000-block figure is already pinned in
    tests/test_codex_pagination.py::test_the_page_budget_bounds_a_page_the_
    item_count_does_not. What is under test here is that the served page obeys
    the budget while the requested item limit does not bind — the exact shape of
    the profiled conversation, whose response was
    ``total: 78, returned: 78, has_after: false``.
    """
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _big_turn_records(tool_calls=200))
    monkeypatch.setattr(q.segkern, "PAGE_BLOCK_BUDGET", 120)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        every = _detail_of(conn, ck, limit=0)
        page = _detail_of(conn, ck, limit=500, tail=True)
    finally:
        conn.close()
    total_segments = page["page"]["total"]
    assert total_segments == len(every["items"])
    assert total_segments < 500, "the item count alone must not bound this page"
    served = sum(len(it["blocks"]) for it in page["items"])
    assert served <= 120
    assert page["page"]["returned"] < total_segments
    assert page["page"]["has_before"] is True


def test_page_total_counts_segments_not_items(tmp_path, monkeypatch):
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _big_turn_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        detail = _detail_of(conn, ck, limit=0)
        rows = q._load_conversation_rows(conn, ck)
        kept, _sup = kern.pair_mirrors(rows)
        item_count = len(kern.canonical_items(kept))
    finally:
        conn.close()
    assert detail["page"]["total"] == len(detail["items"])
    assert detail["page"]["total"] > item_count


def test_reverse_paging_walks_a_segmented_turn_back_to_its_head(
        tmp_path, monkeypatch):
    """F4 and segmentation together: has_before is now true on Codex."""
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _big_turn_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        every = _detail_keys(_detail_of(conn, ck, limit=0))
        seen: list[str] = []
        cursor = None
        for _ in range(50):
            detail = _detail_of(conn, ck, limit=3, tail=(cursor is None),
                                before=cursor)
            seen = _detail_keys(detail) + seen
            if not detail["page"]["has_before"]:
                break
            cursor = detail["page"]["before"]
        else:
            raise AssertionError("reverse paging did not terminate")
    finally:
        conn.close()
    assert seen == every


def test_pos_to_item_key_resolves_to_the_containing_segment(tmp_path, monkeypatch):
    """Search and find derive their anchors here (#463 S1).

    After segmentation this must resolve a physical row to the SEGMENT that
    contains it. Resolving to the turn would land every find hit on the head of
    a turn instead of on the matching content.
    """
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _big_turn_records())
    conn = ns["open_cache_db"]()
    try:
        ck = None
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        pos_map = q._pos_to_item_key(conn, ck)
        detail = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    keys = set(pos_map.values())
    responses = [it for it in detail["items"] if it["kind"] == "assistant"]
    assert len(responses) > 1
    # Every segment of the split turn is reachable, not just its head.
    assert {it["item_key"] for it in responses} <= keys
    assert len(keys) == len(detail["items"])


def test_outline_turns_expose_their_segment_keys(tmp_path, monkeypatch):
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _big_turn_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        outline = q.get_codex_conversation_outline(
            conn, ck, effective_speed="standard")
        detail = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    big = max(outline["turns"], key=lambda t: sum(t["kinds"].values()))
    assert len(big["segment_item_keys"]) > 1
    # segment_item_keys[i] is the key of segment i, so the first entry is the
    # turn's own key.
    assert big["segment_item_keys"][0] == big["item_key"]
    served = [it["item_key"] for it in detail["items"]
              if it["turn_item_key"] == big["item_key"]]
    assert big["segment_item_keys"] == served
    # The outline itself stays turn-granular.
    assert outline["stats"]["items"] == len(outline["turns"])
    assert len(outline["turns"]) < len(detail["items"])


def test_segment_keys_are_not_added_to_member_item_keys(tmp_path, monkeypatch):
    """The two memberships are different relations and must stay different.

    loadToTarget treats a uuid present in an item's member_uuids as already
    loaded, so putting segment keys there would make the drain skip a segment
    that has not been fetched, and the jump would land nowhere.

    The fixture must contain a FOLDED item, or the second assertion holds
    whatever the code does: `_big_turn_records` produces no folded item, so
    every `member_item_keys` there is empty and "the item key is not in it" is
    true by construction. `_fold_shape_records` folds a patch completion and a
    web search completion into its response item, and the guards below fail if
    that ever stops being so.
    """
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _fold_shape_records(filler=60))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        outline = q.get_codex_conversation_outline(
            conn, ck, effective_speed="standard")
        detail = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    assert any(turn["member_item_keys"] for turn in outline["turns"]), (
        "non-vacuity: the fixture must contain a folded item")
    assert any(len(turn["segment_item_keys"]) > 1 for turn in outline["turns"]), (
        "non-vacuity: the fixture must contain a turn that splits into segments")
    assert any(item["member_item_keys"] for item in detail["items"]), (
        "non-vacuity: a detail item must carry folded-item aliases")
    for turn in outline["turns"]:
        assert not set(turn["segment_item_keys"]) & set(turn["member_item_keys"])
    for item in detail["items"]:
        assert item["item_key"] not in item["member_item_keys"]


def test_every_non_response_item_is_exactly_one_segment(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        outline = q.get_codex_conversation_outline(
            conn, ck, effective_speed="standard")
    finally:
        conn.close()
    # modern-full holds no turn anywhere near the budget, so every item stays a
    # single segment and its key is unchanged.
    for turn in outline["turns"]:
        assert turn["segment_item_keys"] == [turn["item_key"]]


def test_detail_limit_bounds_the_head_page(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        every = _detail_keys(
            q.get_codex_conversation(conn, ck, effective_speed="standard", limit=0))
        assert len(every) == 8
        head = q.get_codex_conversation(conn, ck, effective_speed="standard", limit=3)
        assert _detail_keys(head) == every[:3]
        assert head["page"]["has_before"] is False
        assert head["page"]["has_after"] is True
    finally:
        conn.close()


def test_detail_tail_is_a_flag_not_an_item_count(tmp_path, monkeypatch):
    """``tail`` is the boolean the HTTP layer parses out of ``?tail=1``.

    Consuming it as a count made ``min(True, limit)`` collapse the tail page to a
    single item, which left the reader's live-tail poll to append the whole head
    page behind that one item — the reversed anchor-opened conversation.
    """
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        every = _detail_keys(
            q.get_codex_conversation(conn, ck, effective_speed="standard", limit=0))
        tail = q.get_codex_conversation(
            conn, ck, effective_speed="standard", tail=True, limit=3)
        assert _detail_keys(tail) == every[-3:]
        assert tail["page"]["has_before"] is True
        assert tail["page"]["has_after"] is False
    finally:
        conn.close()


def test_neutral_detail_threads_the_tail_flag_to_codex(tmp_path, monkeypatch):
    """The dashboard seam: the handler passes a bool straight through."""
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        every = _detail_keys(
            disp.neutral_detail(conn, ck, effective_speed="standard", limit=0))
        assert _detail_keys(disp.neutral_detail(
            conn, ck, effective_speed="standard", tail=False, limit=3)) == every[:3]
        assert _detail_keys(disp.neutral_detail(
            conn, ck, effective_speed="standard", tail=True, limit=3)) == every[-3:]
    finally:
        conn.close()


# --- status matrix (§5.6) ---------------------------------------------------


def test_detail_pending_status_exact_envelope():
    conn = _cache_schema()   # bare schema, migration 025 NOT stamped -> pending
    try:
        _insert_msg(conn, offset=1, text="x", conversation_key="conv-p")
        d = q.get_codex_conversation(conn, "conv-p", effective_speed="standard")
        assert d == {"status": "normalization_pending",
                     "conversation_key": "conv-p", "items": [], "children": []}
    finally:
        conn.close()


def test_detail_not_found_status_exact_envelope(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        d = q.get_codex_conversation(conn, "no-such-key", effective_speed="standard")
        assert d == {"status": "not_found", "conversation_key": "no-such-key"}
    finally:
        conn.close()


def test_outline_ok_over_modern_full(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        o = q.get_codex_conversation_outline(conn, ck, effective_speed="standard")
        assert o["status"] == "ok"
        assert o["stats"]["items"] == 8
        assert len(o["turns"]) == 8
        labels = [t["label"] for t in o["turns"]]
        assert "Synthetic first meaningful user prompt" in labels
        # #463 S4 §4.3 — the file list is derived read-time now, so each entry
        # also carries its touches and its diff counts. `op` is None here
        # because this fixture's list-shaped change states neither `type` nor
        # `status`, and the derivation reports what the provider said rather
        # than inventing a kind.
        assert o["files"] == [{
            "file_path": "synthetic.txt", "tool": "apply_patch", "count": 1,
            "added": None, "removed": None,
            "touches": [{"item_key": o["files"][0]["touches"][0]["item_key"],
                         "timestamp_utc": "2026-07-14T12:10:00Z", "op": None}],
        }]
        # item keys align with the detail assembly.
        d = q.get_codex_conversation(conn, ck, effective_speed="standard")
        assert [t["item_key"] for t in o["turns"]] == [it["item_key"] for it in d["items"]]
    finally:
        conn.close()


def test_session_a_detail_outline_export_search_and_cost_contract(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-a-turn-contract"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        detail = q.get_codex_conversation(
            conn, ck, effective_speed="standard", limit=0)
        assert detail["status"] == "ok"
        assert detail["title"] == "Build the synthetic widget"

        prompts = [item for item in detail["items"] if item["kind"] == "user"]
        responses = [item for item in detail["items"] if item["kind"] == "assistant"]
        metas = [item for item in detail["items"] if item["kind"] == "meta"]
        assert len(prompts) == 2
        assert [item["blocks"][0]["text"] for item in prompts] == [
            "Build the synthetic widget", "Build the synthetic widget"]
        assert len({item["item_key"] for item in prompts}) == 2
        assert len(responses) == 2
        assert len(metas) == 9
        assert all(item["meta_kind"] in {
            "context", "skill", "notification"} for item in metas)
        assert all(item["meta_label"] for item in metas)
        bundle = next(item for item in metas
                      if item["meta_label"] == "context_bundle")
        assert bundle["meta_sections"] == ["plugins", "agents", "environment"]

        turn_a = next(item for item in responses
                      if any(b["text"] == "First distinct answer" for b in item["blocks"]))
        block_texts = [block["text"] for block in turn_a["blocks"]]
        assert block_texts.index("Plan the widget") < block_texts.index("synthetic_tool\n{\"q\":\"widget\"}")
        assert block_texts.index("First distinct answer") < block_texts.index("Continue widget reasoning")
        assert block_texts.count("Repeated legitimate note") == 2
        assert "Second distinct answer" in block_texts

        per_item = sum(item["cost_usd"] or 0.0 for item in detail["items"])
        assert abs(per_item + detail["unattributed_cost_usd"] - detail["total_cost_usd"]) < 1e-9
        assert abs(detail["unattributed_cost_usd"]) < 1e-12
        assert all(item["cost_usd"] is not None for item in responses)

        outline = q.get_codex_conversation_outline(
            conn, ck, effective_speed="standard")
        assert outline["status"] == "ok"
        assert [turn["item_key"] for turn in outline["turns"]] == [
            item["item_key"] for item in detail["items"]]
        assert all("<permissions" not in turn["label"] for turn in outline["turns"])
        assert {turn.get("meta_label") for turn in outline["turns"] if turn.get("meta_kind")} >= {
            "permissions", "role", "plugins", "agents", "skill", "context_bundle"}

        prompt_search = q.search_codex_conversations(
            conn, "Build", kind="prompts", effective_speed="standard")
        assert prompt_search["total"] == 2
        assert q.search_codex_conversations(
            conn, "synthetic permissions", kind="prompts",
            effective_speed="standard")["total"] == 0
        found = q.find_in_codex_conversation(
            conn, ck, "Second distinct answer", kind="assistant")
        assert found["total"] == 1
        assert found["anchors"][0]["item_key"] == turn_a["item_key"]

        exported = q.get_codex_conversation_export(
            conn, ck, effective_speed="standard")
        assert exported["status"] == "ok"
        assert "# Build the synthetic widget" in exported["markdown"]
        assert "Context: Permissions" in exported["markdown"]
        assert "Context: Session context" in exported["markdown"]
        assert "Second distinct answer" in exported["markdown"]
    finally:
        conn.close()


def test_session_b_card_ready_detail_and_guarded_replay_contract(
    tmp_path, monkeypatch,
):
    """#331 A: supported native shell/patch records become card-ready while
    malformed shapes stay raw, and guarded v3 replay keeps rollups coherent."""
    assert int(kern.CODEX_CONVERSATION_CONTRACT_VERSION) >= 3
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-b-card-wire"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        rows_before = conn.execute(
            "SELECT source_path,line_offset,kind,detail_json,content_digest "
            "FROM codex_conversation_messages ORDER BY source_path,line_offset"
        ).fetchall()
        events_before = conn.execute(
            "SELECT source_path,line_offset,payload_json FROM codex_conversation_events "
            "ORDER BY source_path,line_offset"
        ).fetchall()

        detail = q.get_codex_conversation(
            conn, ck, effective_speed="standard", limit=0)
        blocks = [block for item in detail["items"] for block in item["blocks"]]
        by_call = {block.get("call_id"): block for block in blocks
                   if block["kind"] == "tool_call"}

        terminal = by_call["exec-ok"]
        assert terminal["detail"]["name"] == "exec"
        assert terminal["detail"]["card"] == {
            "schema_version": 1,
            "type": "terminal",
            "status": "completed",
            "commands": [{
                "command": "printf 'alpha\\n'",
                "workdir": "/synthetic/root-a/project-red",
                "metadata": {"max_output_tokens": 12000, "yield_time_ms": 10000},
            }],
        }
        assert terminal["output"]["text"] == "alpha\n"
        # #463 S3 moved this card deliberately: the served `terminal_output` now
        # carries `exit_code` and `wall_time_seconds` from the preamble reader,
        # each null when the grammar did not supply it (spec section 4.3). This
        # fixture's `Wall time 0.1 seconds` line is where the 0.1 comes from, and
        # its grammar supplies no exit code.
        assert terminal["output"]["detail"]["card"] == {
            "schema_version": 1,
            "type": "terminal_output",
            "status": "completed",
            "is_error": False,
            "parts": [{"stream": "output", "text": "alpha\n", "type": "text"}],
            "truncated": False,
            "exit_code": None,
            "wall_time_seconds": 0.1,
        }
        assert by_call["exec-string"]["output"]["text"] == "plain string output\n"
        failed = by_call["exec-failed"]["output"]["detail"]["card"]
        assert failed["status"] == "failed" and failed["is_error"] is True
        assert failed["parts"] == [{
            "stream": "output", "text": "synthetic stderr\n", "type": "text"}]

        malformed = by_call["exec-malformed"]
        assert "card" not in malformed["detail"]
        assert "tools.exec_command" in malformed["detail"]["args"]
        assert malformed["output"]["detail"]["card"]["parts"][0]["type"] == "raw"
        assert "inspectable" in malformed["output"]["text"]
        blank = by_call["exec-blank"]
        assert blank["output"]["text"] == ""
        assert blank["output"]["detail"]["card"]["parts"] == [{
            "stream": "output", "text": "", "type": "text"}]

        direct_patch = by_call["direct-patch"]
        patch_card = direct_patch["detail"]["card"]
        assert patch_card["type"] == "patch"
        assert patch_card["source"] == "apply_patch"
        # #463 S3 remediation B1 — the call side decodes the envelope it is
        # already holding, so the entry carries the same field vocabulary the
        # event side publishes rather than a bare file list.
        assert patch_card["files"] == [{
            "path": "synthetic-added.txt", "status": "added", "truncated": False,
            "diff_source": "derived",
            "unified_diff": "--- /dev/null\n+++ synthetic-added.txt\n"
                            "@@ -0,0 +1,1 @@\n+alpha\n"}]
        completion = patch_card["completion"]
        assert completion["success"] is True
        assert completion["stdout"] == "patch ok\n" and completion["stderr"] == ""
        assert completion["has_diff"] is True
        assert [entry["status"] for entry in completion["files"]] == [
            "added", "modified", "deleted", "moved"]
        assert completion["files"][3]["move_path"] == "synthetic-new.txt"
        assert completion["files"][1]["unified_diff"].endswith("-old\n+new\n")
        assert completion["event_block_key"].startswith("cbk1_")
        owner_item = next(item for item in detail["items"]
                          if direct_patch in item["blocks"])
        assert len(owner_item["member_item_keys"]) >= 1
        folded_key = owner_item["member_item_keys"][0]
        after_folded = q.get_codex_conversation(
            conn, ck, effective_speed="standard", after=folded_key, limit=1)
        assert after_folded["status"] == "ok"
        # The proven completion is folded into the call exactly once.
        assert not any(block.get("call_id") == "direct-patch" and block["kind"] == "event"
                       for block in blocks)

        bracketed = by_call["exec-patch"]["detail"]["card"]
        assert bracketed["type"] == "patch" and bracketed["source"] == "tools.apply_patch"
        assert bracketed["completion"]["files"][0]["path"] == "synthetic-edit.txt"
        heredoc = by_call["heredoc-patch"]["detail"]["card"]
        assert heredoc["type"] == "patch" and heredoc["source"] == "exec_apply_patch"
        # A V4A delete names the file and carries no body, so there is genuinely
        # no line-level diff to publish.
        assert heredoc["files"] == [{
            "path": "synthetic-delete.txt", "status": "deleted", "truncated": False}]
        repeated = [by_call["repeat-patch-1"], by_call["repeat-patch-2"]]
        assert len({block["block_key"] for block in repeated}) == 2
        assert len({block["detail"]["card"]["completion"]["event_block_key"]
                    for block in repeated}) == 2
        assert all(block["detail"]["card"]["completion"]["files"][0]["path"]
                   == "synthetic-repeat.txt" for block in repeated)

        diff_less = next(block for block in blocks
                         if block["kind"] == "event" and block.get("call_id") == "diff-less")
        assert diff_less["detail"]["card"]["has_diff"] is False
        # Per-file `truncated` is on EVERY served file entry since #463 S3
        # remediation F5, including the list branch and the entries with no diff.
        assert diff_less["detail"]["card"]["files"] == [{
            "path": "synthetic-summary.txt", "status": "modified",
            "truncated": False}]
        assert diff_less["detail"]["card"]["success"] is False
        assert diff_less["block_key"].startswith("cbk1_")
        assert diff_less["payload_which"] == "event"

        full_event = q.read_codex_payload(
            conn, ck, completion["event_block_key"], "event")
        assert full_event["status"] == "ok"
        assert full_event["card"]["files"] == completion["files"]
        assert '"unified_diff"' in full_event["content"]

        # The v3-derived view is stable: a second ordinary sync is a
        # physical/derived no-op and never changes retained events.
        ns["sync_codex_cache"](conn)
        assert conn.execute(
            "SELECT source_path,line_offset,kind,detail_json,content_digest "
            "FROM codex_conversation_messages ORDER BY source_path,line_offset"
        ).fetchall() == rows_before
        assert conn.execute(
            "SELECT source_path,line_offset,payload_json FROM codex_conversation_events "
            "ORDER BY source_path,line_offset"
        ).fetchall() == events_before
    finally:
        conn.close()


def test_session_b_export_search_and_outline_preserve_existing_contracts(
    tmp_path, monkeypatch,
):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-b-card-wire"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        outline = q.get_codex_conversation_outline(
            conn, ck, effective_speed="standard")
        assert outline["status"] == "ok"
        assert {row["file_path"] for row in outline["files"]} >= {
            "synthetic-added.txt", "synthetic-edit.txt", "synthetic-summary.txt"}
        assert any(turn["member_item_keys"] for turn in outline["turns"])
        search = q.search_codex_conversations(
            conn, "alpha", kind="tools", effective_speed="standard")
        assert search["status"] == "ok" and search["hits"]
        export = q.get_codex_conversation_export(
            conn, ck, effective_speed="standard")
        assert export["status"] == "ok"
        assert "alpha" in export["markdown"]
        # Card-ready detail is additive; byte-frozen export retains the
        # provider's canonical output wrapper instead of adopting card text.
        assert '"type":"input_text"' in export["markdown"]
        assert "patch_apply synthetic-added.txt" in export["markdown"]
        assert "patch_apply synthetic-edit.txt" in export["markdown"]
    finally:
        conn.close()


def test_session_b_v3_replays_bounded_cards_and_logical_patch_items(
    tmp_path, monkeypatch,
):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-b-card-wire"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        physical_before = conn.execute(
            "SELECT source_path,line_offset,payload_json FROM codex_conversation_events "
            "ORDER BY source_path,line_offset"
        ).fetchall()
        # Simulate the v2 derived shape over unchanged retained physical rows.
        conn.execute(
            "UPDATE codex_conversation_messages SET detail_json = CASE "
            "WHEN kind='tool_call' THEN '{\"name\":\"exec\",\"args\":\"raw\"}' "
            "WHEN kind='event' THEN '{\"event\":\"patch_apply_end\"}' "
            "ELSE NULL END WHERE kind IN ('tool_call','tool_output','event')"
        )
        conn.execute(
            "UPDATE cache_meta SET value='2' "
            "WHERE key='codex_conversation_contract_version'"
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages "
            "WHERE detail_json LIKE '%\"card\"%'"
        ).fetchone() == (0,)

        ns["sync_codex_cache"](conn)
        assert q.codex_normalization_authoritative(conn) is True
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages "
            "WHERE detail_json LIKE '%\"card\"%'"
        ).fetchone()[0] > 0
        rows = q._load_conversation_rows(conn, ck)
        kept, _suppressed = kern.pair_mirrors(rows)
        logical_count = len(kern.canonical_items(kept))
        assert conn.execute(
            "SELECT item_count FROM codex_conversation_rollups "
            "WHERE conversation_key=?", (ck,)
        ).fetchone() == (logical_count,)
        assert conn.execute(
            "SELECT source_path,line_offset,payload_json FROM codex_conversation_events "
            "ORDER BY source_path,line_offset"
        ).fetchall() == physical_before
    finally:
        conn.close()


def test_session_b_harness_parser_is_closed_bounded_and_non_executing():
    supported = (
        'const r = await tools.exec_command({cmd: "printf ok", '
        'workdir: "/synthetic", yield_time_ms: 10000}); text(r.output);'
    )
    payload = {"type": "custom_tool_call", "name": "exec",
               "status": "completed", "input": supported}
    assert kern.decode_tool_call_card(payload)["commands"][0]["command"] == "printf ok"

    unsupported = [
        'text("tools.exec_command({cmd: \\\"inside string\\\"})");',
        '// tools.exec_command({cmd: "inside comment"})',
        'const pattern = /tools.exec_command({cmd: "inside regex"})/;',
        'const r = await tools.exec_command({cmd: process.env.SECRET}); text(r.output);',
        supported + ' /* unclosed',
        'const r = await tools.exec_command({cmd: `template`}); text(r.output);',
        'const r = await evil.tools.exec_command({cmd: "nope"}); text(r.output);',
        # The same lookalike with whitespace and a newline around the dot, which
        # JavaScript permits. A guard reading the character immediately before
        # `tools` is defeated by all three; only the previous significant TOKEN
        # decides whether anything binds to `tools` on the left.
        'const r = await evil . tools.exec_command({cmd: "nope"}); text(r.output);',
        'const r = await evil.\n  tools.exec_command({cmd: "nope"}); text(r.output);',
        'const r = await this . tools.exec_command({cmd: "nope"}); text(r.output);',
        ('const r = await tools.exec_command({cmd: "ok", max_output_tokens: '
         + "9" * 5000 + '}); text(r.output);'),
        # #463 S3 adds four more, all of which would defeat a naive tokenizer
        # and let a call inside a literal or a comment reach a card.
        'const s = "he said \\"tools.exec_command(1)\\" and it\'s fine";',
        'const r = /"/; const t = "tools.exec_command(2)";',
        "// it's a comment about tools.exec_command(3)\nconst x = 1;",
        'const t = `a${"tools.exec_command(4)"}b`;',
    ]
    for raw in unsupported:
        bad = dict(payload, input=raw)
        assert kern.decode_tool_call_card(bad) is None, raw

    # TWO assertions are DELIBERATELY INVERTED here (#463 S3, spec sections 3.3
    # and 7). Both inputs are a recognized invocation with an unrecognized
    # statement beside it, and recognizing exactly that is the point of the
    # change: 17,777 uncarded `exec` calls are programs rather than command
    # chains. They no longer refuse; they produce a `program` card that states
    # what it found and says, through `complete: false`, that it is not the whole
    # story. Every other entry in the list above still asserts `None`, and the
    # scanner is what keeps that true — a textual scan would have decoded the
    # string, comment and regex cases as well.
    for raw in ['evil(); ' + supported, supported + ' sendSecret();']:
        inverted = kern.decode_tool_call_card(dict(payload, input=raw))
        assert inverted is not None, raw
        assert inverted["type"] == "program", raw
        assert inverted["complete"] is False, raw
        assert [i["kind"] for i in inverted["invocations"]] == ["command"], raw
        assert inverted["invocations"][0]["command"] == "printf ok", raw

    too_many = "\n".join(
        f'const r{i} = await tools.exec_command({{cmd: "cmd-{i}"}}); '
        f'text(r{i}.output);'
        for i in range(9)
    )
    assert kern.decode_tool_call_card(dict(payload, input=too_many)) is None

    malformed_patch = {
        "type": "custom_tool_call", "name": "apply_patch",
        "status": "completed",
        "input": "*** Begin Patch\n*** Add File: incomplete.txt\n+missing end",
    }
    assert kern.decode_tool_call_card(malformed_patch) is None


def test_session_b_oversized_numeric_literal_falls_back_without_blocking_replay(
    tmp_path, monkeypatch,
):
    program = (
        'const r = await tools.exec_command({cmd: "ok", max_output_tokens: '
        + "9" * 5000
        + '}); text(r.output);'
    )
    records = _codex_turn_records([
        {"call_id": "huge-number", "input": program, "name": "exec",
         "status": "completed", "type": "custom_tool_call"},
        {"call_id": "huge-number", "output": "raw",
         "type": "custom_tool_call_output"},
    ])
    ns, _root, _path = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert q.codex_normalization_authoritative(conn) is True
        detail = q.get_codex_conversation(
            conn, _single_ck(conn), effective_speed="standard", limit=0)
        call = next(block for item in detail["items"] for block in item["blocks"]
                    if block.get("call_id") == "huge-number")
        assert "card" not in call["detail"]
        assert "max_output_tokens" in call["detail"]["args"]
    finally:
        conn.close()


def test_session_b_patch_correlation_requires_global_owner_and_same_file_bracket():
    rows = _normalize("session-b-card-wire").rows
    direct_call = next(row for row in rows
                       if row.kind == "tool_call" and row.call_id == "direct-patch")
    direct_event = next(row for row in rows
                        if row.kind == "event" and row.call_id == "direct-patch")
    direct_output = next(row for row in rows
                         if row.kind == "tool_output" and row.call_id == "direct-patch")
    terminal = next(row for row in rows
                    if row.kind == "tool_call" and row.call_id == "exec-ok")
    reused_owner = dataclasses.replace(
        terminal, call_id="direct-patch",
        timestamp_utc="2026-07-21T11:00:11.500000+00:00",
        line_offset=direct_call.line_offset + 1,
    )
    reused_items = kern.canonical_items([
        direct_call, reused_owner, direct_event, direct_output])
    assert any(item["klass"] == "event" for item in reused_items)
    assert not any(item.get("folded_items") for item in reused_items)

    bracket_call = next(row for row in rows
                        if row.kind == "tool_call" and row.call_id == "exec-patch")
    bracket_event = next(row for row in rows
                         if row.kind == "event" and row.call_id == "inner-exec-patch")
    bracket_output = next(row for row in rows
                          if row.kind == "tool_output" and row.call_id == "exec-patch")
    cross_file_output = dataclasses.replace(
        bracket_output, source_path=bracket_output.source_path + ".other")
    cross_file_items = kern.canonical_items([
        bracket_call, bracket_event, cross_file_output])
    assert any(item["klass"] == "event" for item in cross_file_items)
    assert not any(item.get("folded_items") for item in cross_file_items)


def test_session_b_card_caps_defer_to_full_payload(tmp_path, monkeypatch):
    long_command = "x" * (kern.CODEX_TEXT_CAP + 50)
    long_patch = (
        "*** Begin Patch\n*** Add File: synthetic-long.txt\n+"
        + "y" * (kern.CODEX_TEXT_CAP + 50)
        + "\n*** End Patch"
    )
    exec_program = (
        "const r = await tools.exec_command({cmd: "
        + json.dumps(long_command)
        + "}); text(r.output);"
    )
    records = _codex_turn_records([
        {"call_id": "long-exec", "input": exec_program, "name": "exec",
         "status": "completed", "type": "custom_tool_call"},
        {"call_id": "long-exec", "output": "", "type": "custom_tool_call_output"},
        {"call_id": "long-patch", "input": long_patch, "name": "apply_patch",
         "status": "completed", "type": "custom_tool_call"},
        {"call_id": "long-patch", "output": "", "type": "custom_tool_call_output"},
    ])
    ns, _root, _path = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        detail = q.get_codex_conversation(
            conn, ck, effective_speed="standard", limit=0)
        calls = {block["call_id"]: block for item in detail["items"]
                 for block in item["blocks"] if block["kind"] == "tool_call"}
        assert calls["long-exec"]["detail"]["card"]["truncated"] is True
        assert calls["long-patch"]["detail"]["card"]["truncated"] is True
        full_exec = q.read_codex_payload(
            conn, ck, calls["long-exec"]["block_key"], "call")
        full_patch = q.read_codex_payload(
            conn, ck, calls["long-patch"]["block_key"], "call")
        assert full_exec["card"]["commands"][0]["command"] == long_command
        assert full_patch["card"]["patch"] == long_patch
        assert full_exec["card"].get("truncated") is not True
        assert full_patch["card"]["truncated"] is False
    finally:
        conn.close()


def test_session_c_secondary_tool_wire_is_structured_and_completion_folded(
    tmp_path, monkeypatch,
):
    """#332 A RED: retained plan/web/MCP/agent records must not remain a
    generic argument dump plus detached provider completion events."""
    records = _codex_turn_records([
        {"type": "function_call", "name": "update_plan", "call_id": "plan-1",
         "arguments": json.dumps({
             "explanation": "Synthetic explanation",
             "plan": [
                 {"step": "First synthetic step", "status": "completed"},
                 {"step": "Second synthetic step", "status": "in_progress"},
             ],
         })},
        {"type": "function_call_output", "call_id": "plan-1",
         "output": "Plan updated"},
        {"type": "web_search_call", "id": "web-1", "status": "completed",
         "action": {"type": "search", "query": "synthetic query",
                    "queries": ["synthetic query"]}},
        {"type": "function_call", "name": "fixture_search_issues",
         "call_id": "mcp-1", "arguments": "{\"state\":\"open\"}"},
        {"type": "function_call_output", "call_id": "mcp-1",
         "output": "synthetic MCP wrapper output"},
        {"type": "function_call", "name": "spawn_agent", "call_id": "agent-1",
         "arguments": "{\"task_name\":\"child\",\"message\":\"Do synthetic work\","
                      "\"agent_type\":\"cctally_reviewer\",\"fork_turns\":\"none\"}"},
        {"type": "function_call_output", "call_id": "agent-1",
         "output": "{\"task_name\":\"/root/child\"}"},
        {"type": "function_call", "name": "wait_agent", "call_id": "agent-2",
         "arguments": "{\"timeout_ms\":30000}"},
        {"type": "function_call_output", "call_id": "agent-2",
         "output": "{\"message\":\"synthetic update\",\"timed_out\":false}"},
    ])
    records.insert(5, {
        "timestamp": "2026-07-14T12:04:30Z", "type": "event_msg",
        "payload": {
            "type": "web_search_end", "call_id": "web-1",
            "query": "synthetic query",
            "action": {"type": "search", "query": "synthetic query"},
            "results": [{
                "type": "computer_initialize_state", "domain": "example.test",
                "ref_id": "turn0search0", "snippet": "Synthetic result",
                "title": "Synthetic title", "url": "https://example.test/result",
            }],
        },
    })
    records.insert(8, {
        "timestamp": "2026-07-14T12:05:30Z", "type": "event_msg",
        "payload": {
            "type": "mcp_tool_call_end", "call_id": "mcp-1",
            "duration": {"secs": 1, "nanos": 250000000},
            "invocation": {"server": "fixture", "tool": "search_issues",
                           "arguments": {"state": "open"}},
            "result": {"Ok": {"content": [{"type": "text", "text": "synthetic"}]}},
        },
    })
    ns, _root, _path = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        detail = q.get_codex_conversation(
            conn, _single_ck(conn), effective_speed="standard", limit=0)
        blocks = [block for item in detail["items"] for block in item["blocks"]]
        calls = {block.get("call_id"): block for block in blocks
                 if block["kind"] == "tool_call"}

        assert calls["plan-1"]["detail"]["card"] == {
            "schema_version": 1, "type": "plan", "source": "update_plan",
            "call_status": "requested", "explanation": "Synthetic explanation",
            "items": [
                {"step": "First synthetic step", "status": "completed"},
                {"step": "Second synthetic step", "status": "in_progress"},
            ],
            "result": {"status": "returned", "value": "Plan updated",
                       "truncated": False},
        }
        web = calls["web-1"]["detail"]["card"]
        assert web["type"] == "web_search" and web["query"] == "synthetic query"
        assert web["completion"]["results"][0]["url"] == "https://example.test/result"
        assert not any(block["kind"] == "event"
                       and (block.get("detail") or {}).get("event") == "web_search_end"
                       for block in blocks)
        mcp = calls["mcp-1"]["detail"]["card"]
        assert mcp["type"] == "mcp" and mcp["completion"]["server"] == "fixture"
        assert mcp["completion"]["duration"] == {"secs": 1, "nanos": 250000000}
        assert not any(block["kind"] == "event"
                       and (block.get("detail") or {}).get("event") == "mcp_tool_call_end"
                       for block in blocks)
        spawn = calls["agent-1"]["detail"]["card"]
        assert spawn["type"] == "agent" and spawn["operation"] == "spawn_agent"
        assert spawn["arguments"]["message"] == "Do synthetic work"
        assert spawn["result"]["value"] == {"task_name": "/root/child"}
        wait = calls["agent-2"]["detail"]["card"]
        assert wait["arguments"] == {"timeout_ms": 30000}
        assert wait["result"]["value"]["timed_out"] is False
    finally:
        conn.close()


def _mcp_protocol_error_payload():
    """The retained provider shape sampled for #494 (transport Ok, MCP error)."""
    return {
        "type": "mcp_tool_call_end", "call_id": "mcp-protocol-error",
        "duration": {"secs": 0, "nanos": 250000000},
        "invocation": {
            "server": "fixture", "tool": "get_issue",
            "arguments": {"number": 999},
        },
        "result": {
            "Ok": {
                "content": [{"type": "text", "text": "synthetic failure"}],
                "isError": True,
            },
        },
    }


def test_mcp_protocol_error_decodes_as_failure():
    """#494 RED: successful transport does not override MCP's own verdict."""
    card = kern.decode_secondary_event_card(_mcp_protocol_error_payload())
    assert card is not None
    assert card["status"] == "error"

    for non_error in (False, "true"):
        payload = _mcp_protocol_error_payload()
        payload["result"]["Ok"]["isError"] = non_error
        assert kern.decode_secondary_event_card(payload)["status"] == "ok"


def test_mcp_protocol_error_reaches_card_outline_and_error_landmark(
    tmp_path, monkeypatch,
):
    """#494 RED: the decoder verdict is the one downstream surfaces consume."""
    records = _codex_turn_records([{
        "type": "function_call", "name": "fixture_get_issue",
        "call_id": "mcp-protocol-error", "arguments": "{\"number\":999}",
    }])
    records.append({
        "timestamp": "2026-07-14T12:03:00Z", "type": "event_msg",
        "payload": _mcp_protocol_error_payload(),
    })
    ns, _root, _path = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        detail = q.get_codex_conversation(
            conn, ck, effective_speed="standard", limit=0)
        outline = q.get_codex_conversation_outline(
            conn, ck, effective_speed="standard")
    finally:
        conn.close()

    call = next(
        block for item in detail["items"] for block in item["blocks"]
        if block.get("call_id") == "mcp-protocol-error")
    assert call["detail"]["card"]["completion"]["status"] == "error"
    assert outline["stats"]["error_count"] == 1
    assert [(lm["kind"], lm["label"]) for lm in outline["landmarks"]] == [
        ("tool_error", "fixture_get_issue"),
    ]
    tool = next(
        tool for turn in outline["turns"] for tool in turn.get("tools", [])
        if tool["name"] == "fixture_get_issue")
    assert tool["is_error"] is True


def test_session_c_fixture_contract_links_only_exact_same_root_child(
    tmp_path, monkeypatch,
):
    scenarios = [
        "session-c-secondary-tools", "session-c-child-proven",
        "session-c-child-ambiguous-a", "session-c-child-ambiguous-b",
    ]
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, scenarios)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        keys = dict(conn.execute(
            "SELECT native_thread_id,conversation_key "
            "FROM codex_conversation_threads"))
        parent_key = keys["cccccccc-cccc-4ccc-8ccc-cccccccccccc"]
        child_key = keys["c1111111-1111-4111-8111-111111111111"]
        physical_before = conn.execute(
            "SELECT source_path,line_offset,payload_json "
            "FROM codex_conversation_events ORDER BY source_path,line_offset"
        ).fetchall()
        derived_before = conn.execute(
            "SELECT source_path,line_offset,call_id,detail_json "
            "FROM codex_conversation_messages ORDER BY source_path,line_offset"
        ).fetchall()

        detail = q.get_codex_conversation(
            conn, parent_key, effective_speed="standard", limit=0)
        blocks = [block for item in detail["items"] for block in item["blocks"]]
        calls = {block.get("call_id"): block for block in blocks
                 if block["kind"] == "tool_call"}
        assert kern.CODEX_CONVERSATION_CONTRACT_VERSION == "5"
        assert [item["status"] for item in
                calls["plan-ok"]["detail"]["card"]["items"]] == [
                    "pending", "in_progress", "completed"]
        assert "card" not in calls["plan-malformed"]["detail"]
        assert "card" not in calls["agent-malformed"]["detail"]

        web = calls["web-ok"]["detail"]["card"]
        assert web["completion"]["status"] == "returned"
        assert web["completion"]["results"][0]["ref_id"] == "turn0search0"
        web_error = calls["web-error"]["detail"]["card"]["completion"]
        assert web_error["status"] == "error"
        assert web_error["error"] == "synthetic search failure"
        assert len({calls["web-repeat-a"]["block_key"],
                    calls["web-repeat-b"]["block_key"]}) == 2
        assert calls["mcp-ok"]["detail"]["card"]["completion"]["status"] == "ok"
        mcp_error = calls["mcp-error"]["detail"]["card"]["completion"]
        assert mcp_error["status"] == "error"
        assert mcp_error["result"] == {"Err": "synthetic MCP failure"}

        agent_cards = [
            block["detail"]["card"] for block in calls.values()
            if (block.get("detail") or {}).get("card", {}).get("type") == "agent"
        ]
        assert {card["operation"] for card in agent_cards} == {
            "spawn_agent", "wait_agent", "send_message", "list_agents",
            "followup_task", "interrupt_agent",
        }
        assert len([card for card in agent_cards
                    if card["operation"] == "wait_agent"]) == 2
        assert calls["wait-b"]["detail"]["card"]["result"]["value"] == {
            "message": "synthetic timeout", "timed_out": True}
        assert calls["send"]["detail"]["card"]["arguments"] == {
            "message": "Synthetic message", "target": "/root/session_c_child"}
        assert calls["list"]["detail"]["card"]["result"]["value"] \
            ["agents"][0]["status"] == "completed"
        assert calls["followup"]["detail"]["card"]["arguments"]["message"] \
            == "Synthetic follow-up"
        assert calls["interrupt"]["detail"]["card"]["result"]["value"] == {
            "previous_status": "running"}
        proven = calls["spawn-proven"]["detail"]["card"]
        assert proven["child_conversation"] == {
            "conversation_key": child_key,
            "role": "cctally_reviewer",
            "nickname": "Synthetic Child",
        }
        assert "agent_path" not in json.dumps(proven["child_conversation"])
        assert "child_conversation" not in calls["spawn-ambiguous"]["detail"]["card"]
        assert "child_conversation" not in calls["spawn-unmatched"]["detail"]["card"]

        assert not any(
            block["kind"] == "event"
            and (block.get("detail") or {}).get("event") in {
                "web_search_end", "mcp_tool_call_end"}
            and block.get("call_id") in {
                "web-ok", "web-repeat-a", "web-repeat-b", "web-error",
                "mcp-ok", "mcp-error",
            }
            for block in blocks
        )
        raw_events = [block for block in blocks if block["kind"] == "event"]
        malformed_web = next(block for block in raw_events
                             if block.get("call_id") == "web-malformed")
        malformed_mcp = next(block for block in raw_events
                             if block.get("call_id") == "mcp-malformed")
        assert "card" not in (malformed_web.get("detail") or {})
        assert "card" not in (malformed_mcp.get("detail") or {})
        for malformed in (malformed_web, malformed_mcp):
            raw = q.read_codex_payload(
                conn, parent_key, malformed["block_key"], "event")
            assert raw["status"] == "ok" and raw["content"]
            assert "card" not in raw
        assert any("/synthetic/private/screenshot.png" in block["text"]
                   and "url:0" in block["text"] for block in blocks)

        outline = q.get_codex_conversation_outline(
            conn, parent_key, effective_speed="standard")
        assert outline["status"] == "ok"
        assert outline["stats"]["items"] == detail["page"]["total"]
        found = q.find_in_codex_conversation(
            conn, parent_key, "Synthetic message", kind="tools")
        assert found["status"] == "ok" and found["total"] >= 1
        searched = q.search_codex_conversations(
            conn, "synthetic web query", kind="tools", effective_speed="standard")
        assert searched["status"] == "ok" and searched["hits"]
        exported = q.get_codex_conversation_export(
            conn, parent_key, effective_speed="standard")
        assert exported["status"] == "ok" and "spawn_agent" in exported["markdown"]
        payload = q.read_codex_payload(
            conn, parent_key, calls["web-ok"]["block_key"], "call")
        assert payload["status"] == "ok" and "synthetic web query" in payload["content"]

        # A verbatim child meta in another source root is not linkable even if
        # parent id and canonical task path still match.
        source_root_key = conn.execute(
            "SELECT source_root_key FROM codex_conversation_threads "
            "WHERE conversation_key=?", (parent_key,)).fetchone()[0]
        conn.execute(
            "UPDATE codex_conversation_events SET source_root_key='other-root' "
            "WHERE conversation_key=?", (child_key,))
        unlinked = q.get_codex_conversation(
            conn, parent_key, effective_speed="standard", limit=0)
        unlinked_calls = {block.get("call_id"): block
                          for item in unlinked["items"] for block in item["blocks"]
                          if block["kind"] == "tool_call"}
        assert "child_conversation" not in \
            unlinked_calls["spawn-proven"]["detail"]["card"]
        conn.execute(
            "UPDATE codex_conversation_events SET source_root_key=? "
            "WHERE conversation_key=?", (source_root_key, child_key))

        ns["sync_codex_cache"](conn)
        assert conn.execute(
            "SELECT source_path,line_offset,payload_json "
            "FROM codex_conversation_events ORDER BY source_path,line_offset"
        ).fetchall() == physical_before
        assert conn.execute(
            "SELECT source_path,line_offset,call_id,detail_json "
            "FROM codex_conversation_messages ORDER BY source_path,line_offset"
        ).fetchall() == derived_before
    finally:
        conn.close()


def test_session_c_completion_correlation_rejects_reused_ids_and_cross_turns():
    rows = _normalize("session-c-secondary-tools").rows
    web_call = next(row for row in rows
                    if row.kind == "tool_call" and row.call_id == "web-ok")
    web_event = next(row for row in rows
                     if row.kind == "event" and row.call_id == "web-ok")
    reused = dataclasses.replace(
        web_call, source_path=web_call.source_path + ".duplicate",
        line_offset=web_call.line_offset + 1,
    )
    ambiguous = kern.canonical_items([web_call, reused, web_event])
    assert any(item["klass"] == "event" for item in ambiguous)
    assert not any(item.get("folded_items") for item in ambiguous)

    other_turn = dataclasses.replace(web_event, turn_id="turn-other")
    cross_turn = kern.canonical_items([web_call, other_turn])
    assert any(item["klass"] == "event" for item in cross_turn)
    assert not any(item.get("folded_items") for item in cross_turn)

    assert q._agent_session_meta({
        "parent_thread_id": "parent", "agent_path": "/root/child",
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": "parent", "agent_path": "/root/other",
        }}},
    }) is None
    nonfinite = kern.decode_secondary_tool_call_card({
        "type": "web_search_call", "action": {
            "type": "search", "query": "synthetic", "score": float("nan")},
    })
    assert nonfinite["action"]["score"] is None
    assert nonfinite["truncated"] is True
    assert kern._canonical_json(nonfinite)


def test_session_c_secondary_cards_survive_v5_replay_without_physical_rewrite(
    tmp_path, monkeypatch,
):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-c-secondary-tools"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        conversation_key = _single_ck(conn)
        physical_before = conn.execute(
            "SELECT source_path,line_offset,payload_json "
            "FROM codex_conversation_events ORDER BY source_path,line_offset"
        ).fetchall()

        # Simulate the v3 derived store over unchanged retained physical rows.
        conn.execute(
            "UPDATE codex_conversation_messages SET detail_json='{}' "
            "WHERE conversation_key=? AND call_id IN ('plan-ok','web-ok','mcp-ok')",
            (conversation_key,),
        )
        conn.execute(
            "UPDATE cache_meta SET value='3' "
            "WHERE key='codex_conversation_contract_version'"
        )
        conn.commit()

        ns["sync_codex_cache"](conn)
        detail = q.get_codex_conversation(
            conn, conversation_key, effective_speed="standard", limit=0)
        calls = {block.get("call_id"): block
                 for item in detail["items"] for block in item["blocks"]
                 if block["kind"] == "tool_call"}
        assert calls["plan-ok"]["detail"]["card"]["type"] == "plan"
        assert calls["web-ok"]["detail"]["card"]["completion"]["query"] \
            == "synthetic web query"
        assert calls["mcp-ok"]["detail"]["card"]["completion"]["server"] \
            == "fixture"
        assert calls["mcp-ok"]["detail"]["card"]["completion"]["tool"] \
            == "search_issues"
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_conversation_contract_version'"
        ).fetchone() == ("5",)
        assert conn.execute(
            "SELECT source_path,line_offset,payload_json "
            "FROM codex_conversation_events ORDER BY source_path,line_offset"
        ).fetchall() == physical_before
    finally:
        conn.close()


def test_session_d_reasoning_lifecycle_and_marker_wire_contract(
    tmp_path, monkeypatch,
):
    scenario = "session-d-reasoning-lifecycle-markers"
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, [scenario])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        conversation_key = _single_ck(conn)
        physical_reasoning = sum(
            json.loads(payload_json).get("payload", {}).get("type")
            in {"reasoning", "agent_reasoning"}
            for (payload_json,) in conn.execute(
                "SELECT payload_json FROM codex_conversation_events "
                "WHERE conversation_key=?", (conversation_key,))
        )
        normalized_reasoning = conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages "
            "WHERE conversation_key=? AND kind='reasoning'",
            (conversation_key,),
        ).fetchone()[0]
        assert physical_reasoning == 7
        assert normalized_reasoning == 5

        detail = q.get_codex_conversation(
            conn, conversation_key, effective_speed="standard", limit=0)
        assert detail["status"] == "ok"
        blocks = [block for item in detail["items"] for block in item["blocks"]]
        reasoning = [block for block in blocks if block["kind"] == "reasoning"]
        # #463 S2 §2.5 added the additive `headings` array. `title`, `summary`
        # and `body` keep exactly their pre-S2 values — they feed
        # `_row_is_reasoning_title` and are a segmentation-boundary input.
        # WHICH rows gain headings is the contract this pins: a decomposable
        # summary decomposes, a prose summary falls back to the entry verbatim,
        # and a row whose retained payload carries no summary entries gets no
        # field at all rather than an empty array.
        keys = [block["block_key"] for block in reasoning]
        assert [block["detail"]["reasoning"] for block in reasoning] == [
            {"schema_version": 1, "source": "response_item",
             "title": "Inspecting synthetic state",
             "headings": [{"key": f"{keys[0]}#0",
                           "text": "Inspecting synthetic state"}]},
            {"schema_version": 1, "source": "response_item",
             "summary": "Synthetic provider summary.",
             "body": "Detailed synthetic reasoning body.",
             "headings": [{"key": f"{keys[1]}#0",
                           "text": "Synthetic provider summary."}]},
            {"schema_version": 1, "source": "response_item",
             "body": "Body-only synthetic reasoning."},
            {"schema_version": 1, "source": "agent_reasoning",
             "title": "Inspecting synthetic state"},
        ]

        folded = next(item for item in detail["items"]
                      if item.get("lifecycle", {}).get("state") == "completed")
        assert folded["lifecycle"] == {
            "schema_version": 1,
            "state": "completed",
            "started": {
                "at": 1784700301000,
                "collaboration_mode_kind": "default",
                "model_context_window": 272000,
            },
            "completed": {
                "at": 1784700303000,
                "duration_ms": 2000,
            },
            "events": [
                {"event": "task_started", "payload_which": "event",
                 "block_key": folded["lifecycle"]["events"][0]["block_key"]},
                {"event": "task_complete", "payload_which": "event",
                 "block_key": folded["lifecycle"]["events"][1]["block_key"]},
            ],
        }
        assert len(folded["member_item_keys"]) == 2
        assert not any((block.get("detail") or {}).get("lifecycle", {}).get("event")
                       in {"task_started", "task_complete"}
                       for block in folded["blocks"])

        lifecycle_fallbacks = [
            block for block in blocks
            if (block.get("detail") or {}).get("lifecycle", {}).get("event")
            in {"task_started", "task_complete"}
        ]
        assert len(lifecycle_fallbacks) == 5
        assert all(block.get("payload_which") == "event"
                   and block.get("block_key") for block in lifecycle_fallbacks)
        assert any(block["detail"]["lifecycle"].get("message")
                   == "Unique completion message."
                   for block in lifecycle_fallbacks)
        assert any(block["detail"]["lifecycle"].get("error")
                   == "Synthetic lifecycle failure"
                   for block in lifecycle_fallbacks)

        marker_block = next(
            block for block in blocks
            if (block.get("detail") or {}).get("markers"))
        assert marker_block["text"] == "Synthetic closeout prose remains visible."
        assert marker_block["detail"]["markers"] == [
            {"schema_version": 1, "type": "git", "action": "create_branch"},
            {"schema_version": 1, "type": "git", "action": "stage"},
            {"schema_version": 1, "type": "git", "action": "commit"},
            {"schema_version": 1, "type": "git", "action": "push"},
            {"schema_version": 1, "type": "git", "action": "create_pr",
             "draft": False},
            {"schema_version": 1, "type": "memory_citation",
             "citation_count": 1, "rollout_count": 1},
        ]
        primary_json = json.dumps(marker_block, sort_keys=True)
        assert "/synthetic/project" not in primary_json
        assert "MEMORY.md" not in primary_json
        assert "11111111-2222-4333-8444-555555555555" not in primary_json
        raw = q.read_codex_payload(
            conn, conversation_key, marker_block["block_key"], "event")
        assert raw["status"] == "ok"
        assert "/synthetic/project" in raw["content"]
        assert "MEMORY.md:10-12" in raw["content"]
        lifecycle_raw = q.read_codex_payload(
            conn, conversation_key,
            lifecycle_fallbacks[0]["block_key"], "event")
        assert lifecycle_raw["status"] == "ok"
        assert '"type":"task_' in lifecycle_raw["content"]

        authored = "\n".join(block["text"] for block in blocks)
        assert "User-authored ::git-stage" in authored
        assert "::git-unknown" in authored
        assert "::git-stage{cwd=\"/synthetic/malformed\" extra=\"nope\"}" in authored
        assert "<oai-mem-citation><citation_entries>lookalike" in authored
        assert "::git-stage{cwd=\"/synthetic/fenced\"}" in authored

        outline = q.get_codex_conversation_outline(
            conn, conversation_key, effective_speed="standard")
        assert outline["status"] == "ok"
        assert outline["stats"]["items"] == detail["page"]["total"]
        thinking = q.find_in_codex_conversation(
            conn, conversation_key, "Inspecting synthetic state", kind="thinking")
        assert thinking["status"] == "ok" and thinking["total"] == 2
        empty = q.find_in_codex_conversation(
            conn, conversation_key, "encrypted-empty", kind="thinking")
        assert empty["status"] == "ok" and empty["total"] == 0
        exported = q.get_codex_conversation_export(
            conn, conversation_key, effective_speed="standard")
        assert exported["status"] == "ok"
        assert "::git-create-branch" in exported["markdown"]
        assert "<oai-mem-citation>" in exported["markdown"]
        assert exported["markdown"].count("task_started") == 3
        assert "Unique completion message." in exported["markdown"]
    finally:
        conn.close()


def test_session_d_contract_v5_replays_without_rewriting_physical_rows(
    tmp_path, monkeypatch,
):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-d-reasoning-lifecycle-markers"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        conversation_key = _single_ck(conn)
        physical_before = conn.execute(
            "SELECT source_path,line_offset,payload_json "
            "FROM codex_conversation_events ORDER BY source_path,line_offset"
        ).fetchall()
        conn.execute(
            "UPDATE codex_conversation_messages SET detail_json=NULL "
            "WHERE conversation_key=? AND kind IN ('reasoning','assistant','event')",
            (conversation_key,),
        )
        conn.execute(
            "UPDATE cache_meta SET value='4' "
            "WHERE key='codex_conversation_contract_version'"
        )
        conn.commit()

        ns["sync_codex_cache"](conn)
        assert kern.CODEX_CONVERSATION_CONTRACT_VERSION == "5"
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_conversation_contract_version'"
        ).fetchone() == ("5",)
        assert conn.execute(
            "SELECT source_path,line_offset,payload_json "
            "FROM codex_conversation_events ORDER BY source_path,line_offset"
        ).fetchall() == physical_before
        detail = q.get_codex_conversation(
            conn, conversation_key, effective_speed="standard", limit=0)
        assert any((block.get("detail") or {}).get("markers")
                   for item in detail["items"] for block in item["blocks"])

        derived_once = conn.execute(
            "SELECT source_path,line_offset,kind,detail_json "
            "FROM codex_conversation_messages ORDER BY source_path,line_offset"
        ).fetchall()
        ns["sync_codex_cache"](conn)
        assert conn.execute(
            "SELECT source_path,line_offset,kind,detail_json "
            "FROM codex_conversation_messages ORDER BY source_path,line_offset"
        ).fetchall() == derived_once
    finally:
        conn.close()


def test_session_e_native_families_are_physical_only_and_privacy_safe(
    tmp_path, monkeypatch,
):
    scenario = "session-e-native-families"
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, [scenario])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        conversation_key = _single_ck(conn)
        physical = conn.execute(
            "SELECT record_type,payload_json FROM codex_conversation_events "
            "WHERE conversation_key=? ORDER BY line_offset", (conversation_key,)
        ).fetchall()
        physical_types = [record_type for record_type, _payload in physical]
        assert physical_types.count("world_state") == 3
        assert physical_types.count("inter_agent_communication_metadata") == 3
        assert physical_types.count("turn_context") == 3
        assert "future_record_v100" in physical_types

        normalized = conn.execute(
            "SELECT kind,event_type,turn_id,model FROM codex_conversation_messages "
            "WHERE conversation_key=? ORDER BY line_offset", (conversation_key,)
        ).fetchall()
        assert len(normalized) == 4
        assert not ({"world_state", "inter_agent_communication_metadata",
                     "turn_context", "future_record_v100"}
                    & {event_type for _kind, event_type, _turn, _model in normalized})
        assert [turn for _kind, _event, turn, _model in normalized] == [
            "session-e-turn-a", "session-e-turn-a",
            "session-e-turn-b", "session-e-turn-b",
        ]
        assert normalized[-1][3] == "gpt-synthetic-codex-b"

        detail = q.get_codex_conversation(
            conn, conversation_key, effective_speed="standard", limit=0)
        outline = q.get_codex_conversation_outline(
            conn, conversation_key, effective_speed="standard")
        exported = q.get_codex_conversation_export(
            conn, conversation_key, effective_speed="standard")
        searched = q.search_codex_conversations(
            conn, "SESSION_E_PRIVATE_INSTRUCTION_CANARY",
            effective_speed="standard")
        primary = json.dumps(
            {"detail": detail, "outline": outline, "export": exported},
            sort_keys=True,
        )
        for canary in (
            "SESSION_E_PRIVATE_INSTRUCTION_CANARY",
            "/synthetic/private/session-e/workspace",
            "native-secret-opaque-335",
        ):
            assert canary in "\n".join(payload for _kind, payload in physical)
            assert canary not in primary
        assert searched["status"] == "ok" and searched["hits"] == []
        assert outline["stats"]["items"] == detail["page"]["total"] == 4
        assert exported["status"] == "ok"
        assert all(text in exported["markdown"] for text in (
            "Session E visible prompt A", "Session E visible answer A",
            "Session E visible prompt B", "Session E visible answer B",
        ))
    finally:
        conn.close()


def test_session_e_malformed_turn_context_does_not_poison_delta_sticky_state():
    events = _events("session-e-native-families")
    result = kern.normalize_codex_events(
        events, initial=kern.CodexStickyState())
    assert result.terminal.turn_id == "session-e-turn-b"
    assert result.terminal.model is None
    assert all(isinstance(row.turn_id, (str, type(None))) for row in result.rows)


def test_session_a_contract_version_replays_existing_derived_store(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-a-turn-contract"])
    conn = ns["open_cache_db"]()
    try:
        first = ns["sync_codex_cache"](conn)
        assert first.files_processed == 1
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_conversation_contract_version'"
        ).fetchone() == (kern.CODEX_CONVERSATION_CONTRACT_VERSION,)

        # Simulate an older derived store on unchanged source bytes. The next
        # ordinary sync must replay rather than skip the file as unchanged.
        conn.execute(
            "UPDATE codex_conversation_messages SET kind='user' WHERE kind='meta'"
        )
        conn.execute(
            "UPDATE cache_meta SET value='1' "
            "WHERE key='codex_conversation_contract_version'"
        )
        conn.commit()
        ns["sync_codex_cache"](conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages WHERE kind='meta'"
        ).fetchone() == (9,)
        assert q.codex_normalization_authoritative(conn) is True
    finally:
        conn.close()


def test_session_a_contract_version_converges_on_first_read_open(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-a-turn-contract"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        conn.execute(
            "UPDATE codex_conversation_messages SET kind='user' WHERE kind='meta'"
        )
        conn.execute(
            "UPDATE cache_meta SET value='1' "
            "WHERE key='codex_conversation_contract_version'"
        )
        conn.commit()
    finally:
        conn.close()

    # Qualified export/search and dashboard --no-sync only open the retained
    # store. That first read must converge the re-derivable contract without a
    # JSONL ingest or an explicit cache-sync command.
    reopened = ns["open_cache_db"]()
    try:
        assert reopened.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages WHERE kind='meta'"
        ).fetchone() == (9,)
        assert q.codex_normalization_authoritative(reopened) is True
    finally:
        reopened.close()


def test_session_a_late_task_complete_repairs_delta_turn_ids(tmp_path, monkeypatch):
    ns, _root, rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-a-turn-contract"])
    rollout = rollouts["session-a-turn-contract"]
    all_lines = rollout.read_text(encoding="utf-8").splitlines(keepends=True)
    # Stop immediately after the resumed turn's token_count, before its first
    # explicit native turn anchor (patch/task_complete).
    split = next(
        index for index, line in enumerate(all_lines)
        if '"timestamp":"2026-07-21T10:00:27Z"' in line
    ) + 1
    rollout.write_text("".join(all_lines[:split]), encoding="utf-8")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        before = conn.execute(
            "SELECT turn_id FROM codex_conversation_messages "
            "WHERE text='Second distinct answer'"
        ).fetchone()
        assert before == (None,)

        with rollout.open("a", encoding="utf-8") as fh:
            fh.write("".join(all_lines[split:]))
        ns["sync_codex_cache"](conn)
        after = conn.execute(
            "SELECT turn_id FROM codex_conversation_messages "
            "WHERE text='Second distinct answer'"
        ).fetchone()
        assert after == ("turn-a",)
        detail = q.get_codex_conversation(
            conn, _single_ck(conn), effective_speed="standard", limit=0)
        assert abs(detail["unattributed_cost_usd"]) < 1e-12
    finally:
        conn.close()


@pytest.mark.parametrize("late_anchor", ["patch_apply_end", "turn_aborted"])
def test_session_a_every_late_anchor_repairs_delta_turn_ids(
        tmp_path, monkeypatch, late_anchor):
    ns, _root, rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-a-turn-contract"])
    rollout = rollouts["session-a-turn-contract"]
    all_lines = rollout.read_text(encoding="utf-8").splitlines(keepends=True)
    split = next(
        index for index, line in enumerate(all_lines)
        if '"timestamp":"2026-07-21T10:00:27Z"' in line
    ) + 1
    rollout.write_text("".join(all_lines[:split]), encoding="utf-8")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert conn.execute(
            "SELECT turn_id FROM codex_conversation_messages "
            "WHERE text='Second distinct answer'"
        ).fetchone() == (None,)

        if late_anchor == "patch_apply_end":
            anchor_line = next(
                line for line in all_lines if '"type":"patch_apply_end"' in line)
        else:
            anchor_line = json.dumps({
                "payload": {
                    "reason": "synthetic cancellation",
                    "turn_id": "turn-a",
                    "type": "turn_aborted",
                },
                "timestamp": "2026-07-21T10:00:28Z",
                "type": "event_msg",
            }, separators=(",", ":")) + "\n"
        with rollout.open("a", encoding="utf-8") as fh:
            fh.write(anchor_line)
        ns["sync_codex_cache"](conn)

        assert conn.execute(
            "SELECT turn_id FROM codex_conversation_messages "
            "WHERE text='Second distinct answer'"
        ).fetchone() == ("turn-a",)
        detail = q.get_codex_conversation(
            conn, _single_ck(conn), effective_speed="standard", limit=0)
        assert abs(detail["unattributed_cost_usd"]) < 1e-12
    finally:
        conn.close()


def test_outline_pending_and_not_found_exact_envelopes(tmp_path, monkeypatch):
    conn = _cache_schema()
    try:
        _insert_msg(conn, offset=1, text="x", conversation_key="conv-p")
        # The insert opened an implicit write transaction; committing it is what
        # a real writer does, and #463 S4's `_read_snapshot` refuses to serve an
        # envelope from inside a foreign write rather than reading uncommitted
        # state without a snapshot of its own.
        conn.commit()
        o = q.get_codex_conversation_outline(conn, "conv-p", effective_speed="standard")
        assert o == {"status": "normalization_pending", "conversation_key": "conv-p",
                     "turns": [], "files": [], "children": []}
    finally:
        conn.close()
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        o = q.get_codex_conversation_outline(conn, "no-such", effective_speed="standard")
        assert o == {"status": "not_found", "conversation_key": "no-such"}
    finally:
        conn.close()


# --- collision proofs (§8) --------------------------------------------------


def _stage_claude_seed(tmp_path):
    """Stage the shared-UUID Claude JSONL seed under the redirected projects tree
    (HOME == tmp_path/'data' via redirect_paths)."""
    projects = tmp_path / "data" / ".claude" / "projects" / "-synthetic-root-a-project-red"
    projects.mkdir(parents=True, exist_ok=True)
    seed = CORPUS / "claude-seed" / "11111111-1111-4111-8111-111111111111.jsonl"
    shutil.copyfile(seed, projects / seed.name)


def test_collision_shared_uuid_claude_codex_content_isolated(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    _stage_claude_seed(tmp_path)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ns["sync_cache"](conn)   # ingest the Claude seed via the Claude path
        codex_ck = _single_ck(conn)
        d = q.get_codex_conversation(conn, codex_ck, effective_speed="standard")
        codex_text = " ".join(
            b.get("text", "") or "" for it in d["items"] for b in it["blocks"])
        assert "Synthetic" in codex_text
        assert "Claude seed" not in codex_text   # zero Claude rows in the Codex detail
        # And the Claude side (same session UUID) carries only Claude prose.
        claude_text = " ".join(
            (row[0] or "") + " " + (row[1] or "")
            for row in conn.execute(
                "SELECT text, blocks_json FROM conversation_messages WHERE session_id = ?",
                ("11111111-1111-4111-8111-111111111111",)))
        assert "Claude seed" in claude_text
        assert "Synthetic" not in claude_text
    finally:
        conn.close()


def test_collision_two_roots_shared_uuid_distinct_conversations(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    ns = _split_namespace(ns)
    prov_a = tmp_path / "provA"
    prov_b = tmp_path / "provB"
    for prov, scenario in ((prov_a, "root-a-collision"), (prov_b, "root-b-collision")):
        rollout = prov / "sessions" / "2026" / "07" / "15" / f"rollout-{scenario}.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CORPUS / "rollouts" / f"{scenario}.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", f"{prov_a},{prov_b}")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        keys = [r[0] for r in conn.execute(
            "SELECT DISTINCT conversation_key FROM codex_conversation_messages")]
        # Shared inner UUID under two roots -> two DISTINCT conversations.
        assert len(keys) == 2
        texts = {}
        for k in keys:
            d = q.get_codex_conversation(conn, k, effective_speed="standard")
            texts[k] = " ".join(
                b.get("text", "") or "" for it in d["items"] for b in it["blocks"])
        red = [t for t in texts.values() if "Root A red" in t]
        blue = [t for t in texts.values() if "Root B blue" in t]
        assert len(red) == 1 and len(blue) == 1
        # per-root isolation: neither conversation carries the other root's prose.
        assert not any("Root B blue" in t for t in red)
        assert not any("Root A red" in t for t in blue)
    finally:
        conn.close()


# ── Task 6: browse kernel (§6.1) ─────────────────────────────────────────────


_BROWSE_MIX = ["modern-full", "mirror-pairing", "nested-parent", "nested-child",
               "unturned-event-prose"]


def test_browse_display_chain_short_id_fallback():
    # stored title → project_label → short native-thread-id prefix.
    assert q._display_chain(
        {"title": "T", "project_label": "P", "native_thread_id": "abc12345-x"}) == "T"
    assert q._display_chain(
        {"title": None, "project_label": "P", "native_thread_id": "abc12345-x"}) == "P"
    assert q._display_chain(
        {"title": None, "project_label": None,
         "native_thread_id": "11111111-1111-4111"}) == "11111111"


def test_browse_rows_titles_counts_forks(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, _BROWSE_MIX)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        env = q.list_codex_conversations(conn, effective_speed="standard")
        assert env["status"] == "ok"
        assert len(env["rows"]) == 5
        by_native = {}
        for row in env["rows"]:
            native = conn.execute(
                "SELECT native_thread_id FROM codex_conversation_threads "
                "WHERE conversation_key = ?", (row["conversation_key"],)).fetchone()[0]
            by_native[native] = row
        # count == rendered logical item count (incl. the mirror scenario).
        assert by_native["22222222-2222-4222-8222-222222222222"]["count"] == 4  # mirror-pairing
        # title fallback chain: NULL title falls to the project_label.
        unturned = by_native["33333333-3333-4333-8333-333333333333"]
        assert unturned["title"] == "project-red"
        # a derived first-prompt title survives.
        modern = by_native["11111111-1111-4111-8111-111111111111"]
        assert modern["title"] == "Synthetic first meaningful user prompt"
        # fork badge from parent_thread_id (child forks, root does not).
        assert by_native["parent-thread-fixture"]["is_fork"] is False
        child = next(r for r in env["rows"]
                     if conn.execute(
                         "SELECT parent_thread_id FROM codex_conversation_threads "
                         "WHERE conversation_key = ?",
                         (r["conversation_key"],)).fetchone()[0] == "parent-thread-fixture"
                     and conn.execute(
                         "SELECT native_thread_id FROM codex_conversation_threads "
                         "WHERE conversation_key = ?",
                         (r["conversation_key"],)).fetchone()[0]
                     == "11111111-1111-4111-8111-111111111111")
        assert child["is_fork"] is True
        # rows ordered by last activity (descending, conversation_key tiebreak).
        keys_order = [(r["last_activity_utc"] or "", r["conversation_key"])
                      for r in env["rows"]]
        assert keys_order == sorted(keys_order, reverse=True)
    finally:
        conn.close()


def test_browse_rollup_fast_path_equals_live_recompute(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, _BROWSE_MIX)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        fast = q.list_codex_conversations(conn, effective_speed="standard")
        fast_page_1 = q.list_codex_conversations(
            conn, effective_speed="standard", limit=2)
        fast_page_2 = q.list_codex_conversations(
            conn, effective_speed="standard", limit=2,
            cursor=fast_page_1["page"]["cursor"])
        fast_invalid_cursor = q.list_codex_conversations(
            conn, effective_speed="standard", limit=2, cursor="not-present")
        selected_key = fast["rows"][-1]["conversation_key"]
        fast_selected = q.list_codex_conversations(
            conn, effective_speed="standard", limit=2, selected=selected_key)
        # Force the live-recompute branch by deleting the stored rollups.
        conn.execute("DELETE FROM codex_conversation_rollups")
        live = q.list_codex_conversations(conn, effective_speed="standard")
        live_page_1 = q.list_codex_conversations(
            conn, effective_speed="standard", limit=2)
        live_page_2 = q.list_codex_conversations(
            conn, effective_speed="standard", limit=2,
            cursor=live_page_1["page"]["cursor"])
        live_invalid_cursor = q.list_codex_conversations(
            conn, effective_speed="standard", limit=2, cursor="not-present")
        live_selected = q.list_codex_conversations(
            conn, effective_speed="standard", limit=2, selected=selected_key)
        assert fast == live
        assert fast_page_1 == live_page_1
        assert fast_page_2 == live_page_2
        assert fast_invalid_cursor == live_invalid_cursor == live_page_1
        assert fast_selected == live_selected
        assert fast_selected["selected"]["conversation_key"] == selected_key
    finally:
        conn.close()


def test_browse_rollup_fast_path_is_constant_query_count(tmp_path, monkeypatch):
    """The authoritative rollup path must page before per-conversation work.

    #438's retained-store reproduction executed four SELECTs per conversation
    before returning a 50-row page.  Count the SQL boundary directly so a
    future refactor cannot hide the same N+1 behind a faster fixture.
    """
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, _BROWSE_MIX)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        statements = []
        conn.set_trace_callback(statements.append)
        env = q.list_codex_conversations(
            conn, effective_speed="standard", limit=2)
        conn.set_trace_callback(None)

        assert env["page"]["total"] == 5
        assert len(env["rows"]) == 2
        selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        assert len(selects) <= 8, selects
    finally:
        conn.set_trace_callback(None)
        conn.close()


def test_codex_facets_do_not_build_or_price_a_browse_page(tmp_path, monkeypatch):
    """The facets endpoint owns facet aggregation, not a discarded browse page."""
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, _BROWSE_MIX)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        statements = []
        conn.set_trace_callback(statements.append)
        env = q.list_codex_conversation_facets(conn)
        conn.set_trace_callback(None)

        assert env["status"] == "ok"
        assert env["facets"]["projects"]
        assert env["facets"]["models"]
        assert not any("codex_session_entries" in sql for sql in statements)
    finally:
        conn.set_trace_callback(None)
        conn.close()


def test_browse_model_and_project_facets_and_filters(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, _BROWSE_MIX)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        env = q.list_codex_conversations(conn, effective_speed="standard")
        model_names = {m["model"] for m in env["facets"]["models"]}
        assert "gpt-synthetic-codex" in model_names
        # the purely un-turned conversation (model NULL) contributes no model facet.
        assert "unknown" not in model_names
        # model filter excludes the un-turned (empty models) conversation.
        filtered = q.list_codex_conversations(
            conn, effective_speed="standard", model="gpt-synthetic-codex")
        assert all("gpt-synthetic-codex" in r["models"] for r in filtered["rows"])
        assert all(r["count"] != 5 for r in filtered["rows"])  # unturned (5 items) dropped
        # project filter keeps only that project_key.
        pkey = env["rows"][0]["project_key"]
        by_project = q.list_codex_conversations(
            conn, effective_speed="standard", project_key=pkey)
        assert all(r["project_key"] == pkey for r in by_project["rows"])
    finally:
        conn.close()


def test_browse_project_facet_collision_safety_two_roots_same_label(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    ns = _split_namespace(ns)
    prov_a = tmp_path / "provA"
    prov_b = tmp_path / "provB"
    for prov in (prov_a, prov_b):
        # SAME fixture (cwd /synthetic/root-a/project-red -> label 'project-red')
        # under two distinct provider roots -> same label, distinct project_key.
        rollout = prov / "sessions" / "2026" / "07" / "15" / "rollout-modern-full.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CORPUS / "rollouts" / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", f"{prov_a},{prov_b}")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        env = q.list_codex_conversations(conn, effective_speed="standard")
        projects = env["facets"]["projects"]
        # Two distinct roots sharing a label must NOT merge into one facet.
        assert len(projects) == 2
        assert all(p["project_label"] == "project-red" for p in projects)
        assert projects[0]["project_key"] != projects[1]["project_key"]
    finally:
        conn.close()


def test_browse_pending_status_exact_envelope():
    conn = _cache_schema()   # migration 025 NOT stamped -> pending
    try:
        _insert_msg(conn, offset=1, text="x", conversation_key="conv-p")
        env = q.list_codex_conversations(conn, effective_speed="standard")
        assert env == {"status": "normalization_pending", "rows": [],
                       "facets": {"projects": [], "models": []},
                       "page": {"total": 0}}
    finally:
        conn.close()


# ── Task 7: search kernel (§6.2) ─────────────────────────────────────────────


def _hit_ids(env) -> set:
    return {(h["conversation_key"], h["item_key"]) for h in env["hits"]}


def _search_like(conn, query, kind):
    """Force the LIKE path by setting the Codex FTS marker for one call."""
    conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                 "VALUES('codex_fts_unavailable', '1')")
    try:
        return q.search_codex_conversations(
            conn, query, kind=kind, effective_speed="standard")
    finally:
        conn.execute("DELETE FROM cache_meta WHERE key='codex_fts_unavailable'")


def test_search_kinds_tuple_is_pinned():
    assert q.CODEX_SEARCH_KINDS == (
        "all", "prompts", "assistant", "tools", "thinking", "title", "files")


def test_search_tool_output_content_array_snippet_is_readable_prose(
        tmp_path, monkeypatch):
    """Structured tool output stays searchable without leaking its JSON wrapper."""
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["session-b-card-wire"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        env = q.search_codex_conversations(
            conn, "synthetic stderr", kind="tools", effective_speed="standard")

        assert env["hits"]
        snippet = env["hits"][0]["snippet"]
        assert "synthetic stderr" in snippet
        assert "Script failed" in snippet
        assert not snippet.lstrip().startswith("[{")
        assert '"type":"input_text"' not in snippet
    finally:
        conn.close()


def test_search_truncated_content_array_uses_complete_text_parts_only():
    raw = ('[{"text":"readable header","type":"input_text"},'
           '{"text":"unterminated provider payload')
    assert q._search_display_text(raw) == "readable header\n…"


@pytest.mark.parametrize("kind,query", [
    ("all", "Synthetic"),
    ("prompts", "Synthetic"),
    ("assistant", "Synthetic"),
    ("tools", "synthetic"),
    ("thinking", "reasoning"),
    ("title", "meaningful"),
    ("files", "synthetic.txt"),
])
def test_search_fts_like_equivalence_single_term(tmp_path, monkeypatch, kind, query):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        fts = q.search_codex_conversations(
            conn, query, kind=kind, effective_speed="standard")
        assert fts["status"] == "ok" and fts["query"] == query
        assert fts["depth"] == "full"
        assert fts["mode"] == ("fts" if kind not in ("title", "files") else fts["mode"])
        like = _search_like(conn, query, kind)
        assert like["mode"] == "like"
        # single-term queries: FTS and LIKE resolve the SAME collapsed items.
        assert _hit_ids(fts) == _hit_ids(like)
        assert fts["total"] == like["total"] > 0
    finally:
        conn.close()


def test_search_multi_term_fts_and_vs_like_substring_divergence(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        # "prompt Synthetic" — reordered vs the stored "Synthetic ... prompt".
        fts = q.search_codex_conversations(
            conn, "prompt Synthetic", kind="prompts", effective_speed="standard")
        like = _search_like(conn, "prompt Synthetic", "prompts")
        # FTS is term-wise AND (both terms present) -> matches; LIKE is a single
        # contiguous substring -> no match. The documented divergence (#149).
        assert fts["total"] >= 1
        assert like["total"] == 0
        assert _hit_ids(fts) != _hit_ids(like)
    finally:
        conn.close()


def test_search_collapses_turned_mirror_pair(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["mirror-pairing"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        for search in (
            lambda: q.search_codex_conversations(
                conn, "Mirror assistant reply", kind="assistant", effective_speed="standard"),
            lambda: _search_like(conn, "Mirror assistant reply", "assistant"),
        ):
            env = search()
            # The response_item member and its suppressed event_msg mirror collapse
            # to ONE item_key -> one hit, never two.
            assert env["total"] == 1
        # distinct repeated prompts are NOT over-collapsed (different offsets).
        repeats = q.search_codex_conversations(
            conn, "Repeat prompt", kind="prompts", effective_speed="standard")
        assert repeats["total"] == 2
        assert _hit_ids(repeats) == _hit_ids(_search_like(conn, "Repeat prompt", "prompts"))
    finally:
        conn.close()


def test_search_collapses_unturned_mirror_pair(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["unturned-event-prose"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        # Adjacent unturned mirror pair collapses to one item.
        reply = q.search_codex_conversations(
            conn, "Unturned reply", kind="assistant", effective_speed="standard")
        assert reply["total"] == 1
        assert _hit_ids(reply) == _hit_ids(_search_like(conn, "Unturned reply", "assistant"))
        # Non-adjacent identical rows are distinct items -> two hits, not one.
        coincidence = q.search_codex_conversations(
            conn, "Coincidence", kind="assistant", effective_speed="standard")
        assert coincidence["total"] == 2
    finally:
        conn.close()


def test_search_pending_status_exact_envelope():
    conn = _cache_schema()   # migration 025 NOT stamped -> pending; FTS available
    try:
        _insert_msg(conn, offset=1, text="anything", conversation_key="conv-p")
        env = q.search_codex_conversations(conn, "anything", effective_speed="standard")
        assert env == {"status": "normalization_pending", "query": "anything",
                       "hits": [], "total": 0, "mode": "fts", "depth": "full"}
    finally:
        conn.close()


# ── Task 8: provider-neutral dispatch + Claude adapter (§5.1 / §5.6) ──────────


def _claude_cache() -> sqlite3.Connection:
    """A bare in-memory cache with the schema applied, forced onto the browse
    LIVE branch (backfill pending) so directly-seeded conversation_messages are
    the browse/detail source of truth without a rollup population step."""
    conn = _cache_schema()
    conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                 "VALUES('conversation_sessions_backfill_pending', '1')")
    lcq._assemble_memo_clear()
    return conn


_CM_COLS = (
    "session_id, uuid, parent_uuid, source_path, byte_offset, timestamp_utc, "
    "entry_type, text, blocks_json, model, msg_id, req_id, cwd, git_branch, "
    "is_sidechain, source_tool_use_id, stop_reason, attribution_skill, "
    "attribution_plugin, search_tool, search_thinking"
)


def _cm(conn, *, session_id, uuid, offset, ts, entry_type, text="", blocks="[]",
        model=None, msg_id=None, req_id=None, cwd="/synthetic/claude/proj",
        parent_uuid=None, source_path="a.jsonl"):
    conn.execute(
        f"INSERT INTO conversation_messages ({_CM_COLS}) "
        f"VALUES ({','.join('?' for _ in _CM_COLS.split(','))})",
        (session_id, uuid, parent_uuid, source_path, offset, ts, entry_type, text,
         blocks, model, msg_id, req_id, cwd, "main", 0, None, None, None, None, "", ""),
    )


def _se(conn, *, source_path="a.jsonl", offset, ts, model, msg_id, req_id,
        inp=0, out=0, cc=0, cr=0):
    conn.execute(
        "INSERT INTO session_entries (source_path, line_offset, timestamp_utc, "
        "model, msg_id, req_id, input_tokens, output_tokens, cache_create_tokens, "
        "cache_read_tokens, cost_usd_raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (source_path, offset, ts, model, msg_id, req_id, inp, out, cc, cr, None),
    )


_SID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _seed_claude_turn_pair(conn, sid=_SID_A, cwd="/synthetic/claude/proj"):
    """One human prompt + one assistant turn (with a session_entries token row)."""
    _cm(conn, session_id=sid, uuid="h1", offset=0, ts="2026-06-01T00:00:00Z",
        entry_type="human", text="First Claude prompt", cwd=cwd)
    _cm(conn, session_id=sid, uuid="a1", offset=1, ts="2026-06-01T00:00:05Z",
        entry_type="assistant", text="Claude assistant reply",
        blocks='[{"kind":"text","text":"Claude assistant reply"}]',
        model="claude-opus-4-8", msg_id="m1", req_id="r1", cwd=cwd)
    _se(conn, offset=1, ts="2026-06-01T00:00:05Z", model="claude-opus-4-8",
        msg_id="m1", req_id="r1", inp=100, out=50, cc=10, cr=20)


# --- resolve_conversation_ref routing (§5.1) --------------------------------


def test_resolve_conversation_ref_routing_and_collision():
    sid = "11111111-1111-4111-8111-111111111111"
    # bare Claude session id -> claude, with a minted IdentityV1 key.
    r = disp.resolve_conversation_ref(sid)
    assert r.source == "claude" and r.native_key == sid
    assert r.conversation_key.startswith("v1.")
    # the minted claude key resolves back to the same native id + source.
    assert disp.resolve_conversation_ref(r.conversation_key) == \
        disp.ConversationRef("claude", r.conversation_key, sid)

    # a valid Codex conversation key -> codex (native key echoed).
    codex_key = identity.canonical_identity_from_root_key(
        "codex", "conversation", identity.source_root_key(ROOT_A), sid, "root-x")
    rc = disp.resolve_conversation_ref(codex_key)
    assert rc == disp.ConversationRef("codex", codex_key, sid)

    # COLLISION: the codex key's nativeKey UUID is ALSO a bare Claude session id.
    # It resolves codex-ONLY (never a cross-provider fallback), and the bare id
    # independently resolves claude.
    assert disp.resolve_conversation_ref(codex_key).source == "codex"
    assert disp.resolve_conversation_ref(sid).source == "claude"

    # garbage / empty / malformed b64 -> None.
    assert disp.resolve_conversation_ref("not-a-key") is None
    assert disp.resolve_conversation_ref("") is None
    assert disp.resolve_conversation_ref("v1.@@@not-base64") is None
    # cross-kind (resourceKind != "conversation") -> None.
    quota_key = identity.canonical_identity_from_root_key(
        "codex", "quota", identity.source_root_key(ROOT_A), sid, None)
    assert disp.resolve_conversation_ref(quota_key) is None
    # a claude opaque key resolves to its bare-session path (native id).
    claude_key = identity.canonical_identity_from_root_key(
        "claude", "conversation", None, sid, None)
    assert disp.resolve_conversation_ref(claude_key) == \
        disp.ConversationRef("claude", claude_key, sid)


# --- neutral detail: both providers, semantic values (§5.6) -----------------


def test_neutral_detail_codex_matches_kernel_and_token_union(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        direct = q.get_codex_conversation(conn, ck, effective_speed="standard")
        via = disp.neutral_detail(conn, ck, effective_speed="standard")
        assert via == direct                      # dispatch is a pure passthrough
        assert set(via["tokens"]) == {
            "source", "input", "output", "cached_input", "reasoning_output"}
        assert via["tokens"]["source"] == "codex"
        assert "unattributed_cost_usd" in via     # Codex carries the bucket
    finally:
        conn.close()


def test_neutral_detail_claude_semantic_envelope():
    conn = _claude_cache()
    try:
        _seed_claude_turn_pair(conn)
        d = disp.neutral_detail(conn, _SID_A, effective_speed="standard")
        assert d["status"] == "ok"
        assert d["title"] == "First Claude prompt"
        assert [it["kind"] for it in d["items"]] == ["human", "assistant"]
        # Claude token union members — never Codex vocabulary; unattributed absent.
        assert d["tokens"] == {"source": "claude", "input": 100, "output": 50,
                               "cache_create": 10, "cache_read": 20}
        assert "unattributed_cost_usd" not in d
        assert d["children"] == [] and d["parent"] is None
        asst = d["items"][1]
        assert asst["tokens"] == {"source": "claude", "input": 100, "output": 50,
                                  "cache_create": 10, "cache_read": 20}
        assert asst["cost_usd"] is not None and d["total_cost_usd"] > 0
        # page over item_key: total counts every rendered item, not physical rows.
        assert d["page"]["total"] == 2 and d["page"]["returned"] == 2
        assert d["page"]["has_after"] is False and d["page"]["after"] is None
    finally:
        conn.close()


def test_neutral_detail_unknown_and_garbage_ref_not_found():
    conn = _claude_cache()
    try:
        # a well-formed but unknown bare session id -> not_found (identity echoed).
        unknown = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        d = disp.neutral_detail(conn, unknown, effective_speed="standard")
        assert d["status"] == "not_found"
        assert d["conversation_key"] == \
            disp.resolve_conversation_ref(unknown).conversation_key
        # a garbage ref -> not_found echoing the raw ref.
        g = disp.neutral_detail(conn, "garbage", effective_speed="standard")
        assert g == {"status": "not_found", "conversation_key": "garbage"}
    finally:
        conn.close()


# --- Claude cursor translation (§5.6) ---------------------------------------


def _seed_ordered_humans(conn, sid, n, start_offset=0):
    for i in range(n):
        _cm(conn, session_id=sid, uuid=f"u{i:02d}", offset=start_offset + i,
            ts=f"2026-06-01T00:{i:02d}:00Z", entry_type="human",
            text=f"message number {i}")


def test_claude_cursor_forward_backward_roundtrip():
    conn = _claude_cache()
    try:
        sid = _SID_A
        _seed_ordered_humans(conn, sid, 6)
        full = disp.neutral_detail(conn, sid, limit=100)
        all_keys = [it["item_key"] for it in full["items"]]
        assert len(all_keys) == 6
        # Page forward in windows of 2 using the neutral `after` cursor.
        walked, cursor = [], None
        while True:
            page = disp.neutral_detail(conn, sid, after=cursor, limit=2)
            walked.extend(it["item_key"] for it in page["items"])
            if not page["page"]["has_after"]:
                break
            cursor = page["page"]["after"]
        assert walked == all_keys
        # Page backward from the tail using the neutral `before` cursor.
        tail = disp.neutral_detail(conn, sid, tail=1, limit=2)
        back = list(tail["items"])
        cursor = tail["page"]["before"]
        while cursor is not None:
            page = disp.neutral_detail(conn, sid, before=cursor, limit=2)
            back = list(page["items"]) + back
            cursor = page["page"]["before"] if page["page"]["has_before"] else None
        assert [it["item_key"] for it in back] == all_keys
    finally:
        conn.close()


def test_claude_cursor_duplicate_uuid_ts_vs_rowid_order():
    conn = _claude_cache()
    try:
        sid = _SID_A
        _cm(conn, session_id=sid, uuid="u00", offset=0, ts="2026-06-01T00:00:00Z",
            entry_type="human", text="first")
        _cm(conn, session_id=sid, uuid="u01", offset=1, ts="2026-06-01T00:01:00Z",
            entry_type="human", text="second")
        # Duplicate uuid: the LATER-ts copy is inserted FIRST (smaller rowid); the
        # EARLIER-ts copy is inserted SECOND (larger rowid). Assembly's canonical
        # selection is earliest (ts, id), so the larger-rowid copy is canonical —
        # timestamp order differs from rowid order.
        _cm(conn, session_id=sid, uuid="udup", offset=2, ts="2026-06-01T00:04:00Z",
            entry_type="human", text="dup later ts")
        _cm(conn, session_id=sid, uuid="udup", offset=3, ts="2026-06-01T00:02:00Z",
            entry_type="human", text="dup earlier ts CANONICAL")
        _cm(conn, session_id=sid, uuid="u03", offset=4, ts="2026-06-01T00:03:00Z",
            entry_type="human", text="third")
        d = disp.neutral_detail(conn, sid, limit=100)
        # Deduped: udup appears exactly once, at its canonical (earliest-ts) slot.
        uuids_via_key = [it["item_key"] for it in d["items"]]
        assert len(uuids_via_key) == 4                 # u00, u01, udup, u03
        dup_key = disp._claude_item_key(sid, "udup")
        assert uuids_via_key.count(dup_key) == 1
        # A forward cursor after u01 lands on the canonical udup item (resolved via
        # the rendered-anchor rule, NOT an arbitrary rowid duplicate).
        after_u01 = disp._claude_item_key(sid, "u01")
        page = disp.neutral_detail(conn, sid, after=after_u01, limit=1)
        assert page["items"][0]["item_key"] == dup_key
    finally:
        conn.close()


def test_claude_cursor_folded_out_uuid_is_not_found():
    conn = _claude_cache()
    try:
        sid = _SID_A
        _cm(conn, session_id=sid, uuid="h1", offset=0, ts="2026-06-01T00:00:00Z",
            entry_type="human", text="prompt")
        # Assistant turn split across two fragments (same msg_id/req_id): the
        # prose fragment (a2) becomes the rendered anchor; a1 is a member uuid
        # folded OUT of the emitted item.
        _cm(conn, session_id=sid, uuid="a1", offset=1, ts="2026-06-01T00:00:05Z",
            entry_type="assistant", text="",
            blocks='[{"kind":"tool_use","name":"Bash","id":"tu1"}]',
            model="claude-opus-4-8", msg_id="m1", req_id="r1")
        _cm(conn, session_id=sid, uuid="a2", offset=2, ts="2026-06-01T00:00:06Z",
            entry_type="assistant", text="the reply",
            blocks='[{"kind":"text","text":"the reply"}]',
            model="claude-opus-4-8", msg_id="m1", req_id="r1")
        d = disp.neutral_detail(conn, sid, limit=100)
        anchors = {it["item_key"] for it in d["items"]}
        assert disp._claude_item_key(sid, "a2") in anchors    # rendered anchor
        assert disp._claude_item_key(sid, "a1") not in anchors  # folded out
        # A cursor on the folded-out member uuid -> not_found (never a restart).
        folded = disp.neutral_detail(
            conn, sid, after=disp._claude_item_key(sid, "a1"), limit=2)
        assert folded["status"] == "not_found"
        # The rendered-anchor cursor resolves fine.
        ok = disp.neutral_detail(
            conn, sid, after=disp._claude_item_key(sid, "a2"), limit=2)
        assert ok["status"] == "ok"
    finally:
        conn.close()


def test_claude_cursor_survives_cache_rebuild_rowid_renumber():
    # Two caches carry the SAME logical session but insert rows in DIFFERENT order
    # (so rowids renumber, as a cache-sync --rebuild does). uuid-based item keys
    # are identical across both, and a cursor derived on one pages on the other.
    conn1 = _claude_cache()
    conn2 = _claude_cache()
    try:
        sid = _SID_A
        rows = [(f"u{i:02d}", i, f"2026-06-01T00:{i:02d}:00Z", f"msg {i}")
                for i in range(5)]
        for uuid, off, ts, text in rows:
            _cm(conn1, session_id=sid, uuid=uuid, offset=off, ts=ts,
                entry_type="human", text=text)
        # conn2 inserts in reversed order -> different rowids, same (ts, uuid).
        for uuid, off, ts, text in reversed(rows):
            _cm(conn2, session_id=sid, uuid=uuid, offset=off, ts=ts,
                entry_type="human", text=text)
        d1 = disp.neutral_detail(conn1, sid, limit=100)
        d2 = disp.neutral_detail(conn2, sid, limit=100)
        keys1 = [it["item_key"] for it in d1["items"]]
        keys2 = [it["item_key"] for it in d2["items"]]
        assert keys1 == keys2                      # rowid-independent, uuid-based
        # a cursor computed on conn1 pages correctly on conn2.
        after = keys1[1]
        page = disp.neutral_detail(conn2, sid, after=after, limit=1)
        assert page["items"][0]["item_key"] == keys1[2]
    finally:
        conn1.close()
        conn2.close()


# --- neutral browse: Claude same-basename project distinctness (§5.6) --------


def test_neutral_browse_claude_same_basename_distinct_project_key():
    conn = _claude_cache()
    try:
        # Two Claude projects sharing the basename "shared-name" under different
        # parent paths -> same display label, DISTINCT project_key.
        _cm(conn, session_id="aaaa1111-aaaa-4aaa-8aaa-aaaaaaaaaaaa", uuid="h1",
            offset=0, ts="2026-06-01T00:00:00Z", entry_type="human",
            text="prompt A", cwd="/synthetic/claude-a/shared-name", source_path="a.jsonl")
        _cm(conn, session_id="bbbb2222-bbbb-4bbb-8bbb-bbbbbbbbbbbb", uuid="h1",
            offset=0, ts="2026-06-01T00:10:00Z", entry_type="human",
            text="prompt B", cwd="/synthetic/claude-b/shared-name", source_path="b.jsonl")
        env = disp.neutral_browse(conn, source="claude")
        assert env["status"] == "ok"
        assert len(env["rows"]) == 2
        projects = env["facets"]["projects"]
        assert len(projects) == 2                   # two distinct project_key facets
        assert all(p["project_label"] == "shared-name" for p in projects)
        assert projects[0]["project_key"] != projects[1]["project_key"]
        # count is Claude's physical message count (provider-defined semantics).
        assert all(r["count"] == 1 for r in env["rows"])
        assert all(r["parent"] is None and r["is_fork"] is False for r in env["rows"])
        # filtering by one project_key returns ONLY that project's session.
        pk = projects[0]["project_key"]
        filtered = disp.neutral_browse(conn, source="claude", project_key=pk)
        assert [r["project_key"] for r in filtered["rows"]] == [pk]
        selected = disp.neutral_browse(
            conn, source="claude", limit=1,
            selected="aaaa1111-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        assert selected["selected"]["title"] == "prompt A"
        assert selected["selected"]["conversation_key"] != \
            "aaaa1111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    finally:
        conn.close()


def test_neutral_browse_codex_matches_kernel(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, _BROWSE_MIX)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert disp.neutral_browse(conn, source="codex") == \
            q.list_codex_conversations(conn, effective_speed="standard")
    finally:
        conn.close()


def test_neutral_facets_codex_matches_dedicated_kernel(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, _BROWSE_MIX)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert disp.neutral_facets(conn, source="codex") == \
            q.list_codex_conversation_facets(conn)
    finally:
        conn.close()


# --- neutral outline + search: both providers (§5.6) ------------------------


def test_neutral_outline_claude_aligns_with_detail_item_keys():
    conn = _claude_cache()
    try:
        _seed_claude_turn_pair(conn)
        o = disp.neutral_outline(conn, _SID_A)
        d = disp.neutral_detail(conn, _SID_A, limit=100)
        assert o["status"] == "ok"
        assert [t["item_key"] for t in o["turns"]] == [it["item_key"] for it in d["items"]]
        assert o["children"] == []
        assert "stats" in o and "files" in o
        # unknown session -> not_found.
        assert disp.neutral_outline(conn, "cccccccc-cccc-4ccc-8ccc-cccccccccccc") == {
            "status": "not_found",
            "conversation_key": disp.resolve_conversation_ref(
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc").conversation_key}
    finally:
        conn.close()


def test_neutral_outline_claude_preserves_navigation_shape(monkeypatch):
    """A qualified Claude outline must retain every legacy navigation fact."""
    legacy = {
        "session_id": _SID_A,
        "subagent_meta": {
            "child": {
                "kind": "general-purpose",
                "parent_subagent_key": None,
                "spawn_uuid": "a1",
                "spawn_tool_use_id": "toolu_spawn",
            },
        },
        "subagent_costs": {"child": 1.25},
        "stats": {
            "turns": {"total": 1, "human": 0, "assistant": 1,
                      "tool_result": 0, "meta": 0},
            "tool_counts": {"Bash": 1},
            "error_count": 1,
            "models": {"claude-opus-4-8": 1},
            "duration_seconds": 5,
            "tokens": {"input": 1, "output": 2,
                       "cache_creation": 3, "cache_read": 4},
            "cost_usd": 0.5,
            "cache_saved_usd": 0.1,
            "cache_failures": {
                "count": 1,
                "tokens_recreated": 10,
                "est_wasted_usd": 0.2,
                "rebuilds": [{
                    "uuid": "a1", "subagent_key": "child",
                    "ts": "2026-06-01T00:00:05Z",
                    "tokens_recreated": 10, "est_wasted_usd": 0.2,
                }],
            },
        },
        "files": [{
            "path": "src/app.py", "add": 2, "del": 1,
            "touches": [{
                "uuid": "a1", "tool_use_id": "toolu_edit", "op": "edit",
                "add": 2, "del": 1,
            }],
        }],
        "task_completion": {
            "all_done": True, "total": 2, "completed": 2,
            "anchor_uuid": "a1",
        },
        "turns": [{
            "uuid": "a1", "kind": "assistant",
            "ts": "2026-06-01T00:00:05Z", "label": "Failed command",
            "member_uuids": ["a1", "folded"],
            "subagent_key": "child", "parent_uuid": "parent",
            "is_sidechain": True,
            "tools": [{"name": "Bash", "is_error": True}],
            "thinking": ["Investigating"], "model": "claude-opus-4-8",
            "tokens": {"input": 1, "output": 2,
                       "cache_creation": 3, "cache_read": 4},
            "cache_failure": {"tokens_recreated": 10, "prev_cached": 20,
                              "est_wasted_usd": 0.2},
        }],
    }
    monkeypatch.setattr(lcq, "get_conversation_outline",
                        lambda _conn, _sid: legacy)
    monkeypatch.setattr(lcq, "_assemble_session_memoized",
                        lambda _conn, _sid: {"items": []})

    body = disp._claude_outline(object(), _SID_A, "v1.claude")
    item_key = disp._claude_item_key(_SID_A, "a1")

    assert body["turns"] == [{
        "item_key": item_key,
        "kind": "assistant",
        "label": "Failed command",
        "timestamp_utc": "2026-06-01T00:00:05Z",
        "kinds": {"assistant": 1},
        "member_item_keys": [disp._claude_item_key(_SID_A, "folded")],
        "subagent_key": "child",
        "parent_item_key": disp._claude_item_key(_SID_A, "parent"),
        "is_sidechain": True,
        "tools": [{"name": "Bash", "is_error": True}],
        "thinking": ["Investigating"],
        "model": "claude-opus-4-8",
        "tokens": {"input": 1, "output": 2,
                   "cache_creation": 3, "cache_read": 4},
        "cache_failure": {"tokens_recreated": 10, "prev_cached": 20,
                          "est_wasted_usd": 0.2},
    }]
    assert body["stats"]["cache_failures"]["rebuilds"][0]["uuid"] == item_key
    assert body["subagent_meta"]["child"]["spawn_uuid"] == item_key
    assert body["subagent_costs"] == {"child": 1.25}
    assert body["task_completion"]["anchor_uuid"] == item_key
    assert body["files"] == [{
            "file_path": "src/app.py", "tool": "edit", "count": 1,
        "added": 2, "removed": 1,
        "touches": [{
            "item_key": item_key, "timestamp_utc": None,
            "tool_use_id": "toolu_edit", "op": "edit",
            "added": 2, "removed": 1,
        }],
    }]


def test_map_claude_item_preserves_navigation_shape():
    item = {
        "anchor": {"uuid": "a1"},
        "kind": "assistant", "ts": "2026-06-01T00:00:05Z",
        "model": "claude-opus-4-8", "blocks": [], "cost_usd": 0.5,
        "tokens": {"input": 1, "output": 2,
                   "cache_creation": 3, "cache_read": 4},
        "member_uuids": ["a1", "folded"],
        "subagent_key": "child", "parent_uuid": "parent",
        "is_sidechain": True,
        "meta_kind": None, "skill_name": None,
        "command_name": None,
        "cache_failure": {"tokens_recreated": 10, "prev_cached": 20,
                          "est_wasted_usd": 0.2},
    }
    own = disp._claude_item_key(_SID_A, "a1")
    assert disp._map_claude_item(_SID_A, item) == {
        "item_key": own,
        "kind": "assistant",
        "timestamp_utc": "2026-06-01T00:00:05Z",
        "model": "claude-opus-4-8",
        "blocks": [], "cost_usd": 0.5,
        "tokens": {"source": "claude", "input": 1, "output": 2,
                   "cache_create": 3, "cache_read": 4},
        "member_item_keys": [disp._claude_item_key(_SID_A, "folded")],
        "subagent_key": "child",
        "parent_item_key": disp._claude_item_key(_SID_A, "parent"),
        "is_sidechain": True,
        "meta_kind": None, "skill_name": None, "command_name": None,
        "cache_failure": {"tokens_recreated": 10, "prev_cached": 20,
                          "est_wasted_usd": 0.2},
    }


def test_neutral_outline_codex_matches_kernel(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        outline = q.get_codex_conversation_outline(
            conn, ck, effective_speed="standard")
        assert disp.neutral_outline(conn, ck, effective_speed="standard") == outline
        detail = q.get_codex_conversation(
            conn, ck, effective_speed="standard", limit=1)
        assert outline["stats"]["cost_usd"] == pytest.approx(
            detail["total_cost_usd"], abs=1e-9)
        assert outline["stats"]["tokens"] == detail["tokens"]
    finally:
        conn.close()


def test_neutral_search_claude_hits_carry_neutral_identity():
    conn = _claude_cache()
    try:
        _seed_claude_turn_pair(conn)
        env = disp.neutral_search(conn, "Claude", source="claude", kind="all")
        assert env["status"] == "ok" and env["query"] == "Claude"
        assert env["total"] >= 1
        conv_key = disp.resolve_conversation_ref(_SID_A).conversation_key
        assert all(h["conversation_key"] == conv_key for h in env["hits"])
        # each hit carries a neutral item_key + navigational badges.
        assert all("item_key" in h and "badges" in h for h in env["hits"])
        assert "cursor" in env["page"] and "returned" in env["page"]
    finally:
        conn.close()


def test_neutral_search_codex_matches_kernel(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert disp.neutral_search(conn, "Synthetic", source="codex", kind="all") == \
            q.search_codex_conversations(
                conn, "Synthetic", kind="all", effective_speed="standard",
                limit=20, cursor=None)
    finally:
        conn.close()


def test_neutral_dispatch_claude_never_normalization_pending():
    # Claude is always authoritative — no kernel path can emit the Codex-only
    # normalization_pending status, even on a bare (unstamped-025) cache.
    conn = _claude_cache()
    try:
        _seed_claude_turn_pair(conn)
        assert disp.neutral_detail(conn, _SID_A)["status"] == "ok"
        assert disp.neutral_outline(conn, _SID_A)["status"] == "ok"
        assert disp.neutral_browse(conn, source="claude")["status"] == "ok"
        assert disp.neutral_search(conn, "Claude", source="claude")["status"] == "ok"
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# #294 S7 — capability kernels + dispatch (spec §3, §3.7, §4.3-encoding)
# ═══════════════════════════════════════════════════════════════════════════


def _detail_response_item(conn, ck):
    d = q.get_codex_conversation(conn, ck, effective_speed="standard")
    return d, next(it for it in d["items"] if it["kind"] == "assistant")


# ── A1: block_key on payload-capable detail blocks ────────────────────────────


def test_block_key_on_payload_blocks_distinct_and_absent_on_prose(
    tmp_path, monkeypatch,
):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        d, response = _detail_response_item(conn, _single_ck(conn))
        tool_blocks = [b for b in response["blocks"] if b["kind"] == "tool_call"]
        assert len(tool_blocks) == 4  # fn-1, custom-1, search-1 (folded) + web_search
        keys = [b["block_key"] for b in tool_blocks]
        assert all(k and k.startswith("cbk1_") for k in keys)
        assert len(keys) == len(set(keys))  # unique per tool_call physical row
        # #463 S2 §1 reversed the second half of this test. EVERY row-backed
        # block now carries the anchor, prose included; what stays narrow is
        # PAYLOAD-capability, which `payload_which` marks and a prose block
        # never gets. Before S2 this asserted `"block_key" not in b` for every
        # non-tool block, which conflated the two properties (§1.1).
        every_key = [b["block_key"] for it in d["items"] for b in it["blocks"]]
        assert all(k.startswith("cbk1_") for k in every_key)
        assert len(every_key) == len(set(every_key))
        prose = [b for it in d["items"] for b in it["blocks"]
                 if b["kind"] in {"user", "assistant", "reasoning"}]
        assert prose, "non-vacuity: modern-full must carry prose blocks"
        assert all("payload_which" not in b for b in prose)
        for b in prose:
            assert q._locate_payload_block(conn, _single_ck(conn),
                                           b["block_key"]) is None
        # block keys are a DISTINCT family from item keys (different domain/prefix).
        assert not any(k in {it["item_key"] for it in d["items"]} for k in keys)
    finally:
        conn.close()


# ── A5: payload locate/read (§3.4) ────────────────────────────────────────────


def test_payload_multi_pair_turn_disambiguated_and_call_only(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        _d, response = _detail_response_item(conn, ck)
        by_call = {b.get("call_id"): b for b in response["blocks"]
                   if b["kind"] == "tool_call"}
        # THREE identified call/output pairs, disambiguated by distinct block_key.
        for call_id in ("fn-1", "custom-1", "search-1"):
            bk = by_call[call_id]["block_key"]
            call = q.read_codex_payload(conn, ck, bk, "call")
            out = q.read_codex_payload(conn, ck, bk, "output")
            assert call["status"] == "ok" and call["content"]
            assert out["status"] == "ok" and out["content"]
            assert call["truncated"] is False and out["truncated"] is False
        # fn-1 exact content, un-capped, from the re-read record.
        fn_bk = by_call["fn-1"]["block_key"]
        assert q.read_codex_payload(conn, ck, fn_bk, "call")["content"] == "fixture_function\n{}"
        assert q.read_codex_payload(conn, ck, fn_bk, "output")["content"] == '{"ok":true}'
        # the call-id-less web_search_call is CALL-ONLY: which=output -> not_found.
        ws_bk = by_call[None]["block_key"]
        assert q.read_codex_payload(conn, ck, ws_bk, "call")["status"] == "ok"
        assert q.read_codex_payload(conn, ck, ws_bk, "output") == {
            "status": "not_found", "block_key": ws_bk, "which": "output"}
        # an unknown block_key / bad which -> not_found.
        assert q.read_codex_payload(conn, ck, "cbk1_nope", "call")["status"] == "not_found"
        assert q.read_codex_payload(conn, ck, fn_bk, "sideways")["status"] == "not_found"
    finally:
        conn.close()


def test_payload_beyond_cap_reread(tmp_path, monkeypatch):
    """Payload serves content beyond the normalized CODEX_TEXT_CAP (16 000)."""
    big = "x" * (kern.CODEX_TEXT_CAP + 5000)
    records = _codex_turn_records([
        {"arguments": "a", "call_id": "c1", "name": "f", "type": "function_call"},
        {"call_id": "c1", "output": big, "type": "function_call_output"},
    ])
    ns, _root, path = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        _d, response = _detail_response_item(conn, ck)
        bk = next(b["block_key"] for b in response["blocks"] if b["kind"] == "tool_call")
        # the stored/rendered output is capped...
        assert len(response["blocks"][-1].get("output", {}).get("text", "")) <= kern.CODEX_TEXT_CAP
        # ...but payload re-read serves the FULL body.
        out = q.read_codex_payload(conn, ck, bk, "output")
        assert out["status"] == "ok" and out["content"] == big and out["truncated"] is False
    finally:
        conn.close()


@pytest.mark.parametrize("length,expect_trunc", [(1_000_000, False), (1_000_001, True)])
def test_payload_ceiling_boundary_multibyte(tmp_path, monkeypatch, length, expect_trunc):
    """Ceiling is 1,000,000 Python CHARACTERS (not bytes): a multibyte payload at
    exactly the ceiling is not truncated even though it is ~3× the byte size."""
    body = "€" * length  # € = 1 char, 3 UTF-8 bytes
    records = _codex_turn_records([
        {"arguments": "a", "call_id": "c1", "name": "f", "type": "function_call"},
        {"call_id": "c1", "output": body, "type": "function_call_output"},
    ])
    ns, _root, path = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        _d, response = _detail_response_item(conn, ck)
        bk = next(b["block_key"] for b in response["blocks"] if b["kind"] == "tool_call")
        out = q.read_codex_payload(conn, ck, bk, "output")
        assert out["status"] == "ok"
        assert out["truncated"] is expect_trunc
        assert len(out["content"]) == min(length, 1_000_000)
    finally:
        conn.close()


def test_payload_gone_trio(tmp_path, monkeypatch):
    """gone (410): missing file, truncation below offset, and a STRUCTURAL-only
    mutation (call_id changed, extracted content identical) — validated against the
    stored full record, never content_digest."""
    ns, _root, rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    path = rollouts["modern-full"]
    original = path.read_bytes()

    def _bk_for(conn, call_id="fn-1"):
        ck = _single_ck(conn)
        _d, response = _detail_response_item(conn, ck)
        return ck, next(b["block_key"] for b in response["blocks"]
                        if b.get("call_id") == call_id)

    # (1) missing file
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck, bk = _bk_for(conn)
        path.unlink()
        assert q.read_codex_payload(conn, ck, bk, "call")["status"] == "gone"
    finally:
        conn.close()

    # (2) truncation below the stored offset
    path.write_bytes(original)
    conn = ns["open_cache_db"]()
    try:
        ck, bk = _bk_for(conn)
        path.write_bytes(b"")  # truncate to empty
        assert q.read_codex_payload(conn, ck, bk, "call")["status"] == "gone"
    finally:
        conn.close()

    # (3) structural-only mutation: call_id fn-1 -> fn-9 (same length, name/args
    # identical so content_digest + block_key are UNCHANGED and it still locates).
    path.write_bytes(original)
    conn = ns["open_cache_db"]()
    try:
        ck, bk = _bk_for(conn)
        mutated = original.replace(b'"call_id":"fn-1"', b'"call_id":"fn-9"')
        assert mutated != original and len(mutated) == len(original)
        path.write_bytes(mutated)
        assert q.read_codex_payload(conn, ck, bk, "call")["status"] == "gone"
    finally:
        conn.close()


def _seed_codex_tool_call(conn, *, conversation_key, source_root_key, root_path,
                          source_path, disk_path, call_id="c1"):
    """Seed one tool_call row + its events record + write its file, all consistent,
    and return the block_key. ``source_path`` is what the DB stores; ``disk_path`` is
    where the JSON line physically lives (they differ for a symlink test)."""
    record = {"payload": {"arguments": "AAA", "call_id": call_id, "name": "seedfn",
                          "type": "function_call"},
              "timestamp": "2026-07-14T12:00:00Z", "type": "response_item"}
    ex = kern._extract("response_item", record["payload"])
    digest = kern.content_digest(ex.content_text)
    clen = kern.content_len(ex.content_text)
    capped, _ = kern._cap(ex.content_text)
    core_conn = sqlite3.connect(core.CACHE_DB_PATH)
    core_conn.execute(
        "INSERT OR IGNORE INTO codex_source_roots "
        "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?)",
        (source_root_key, str(root_path), "2026-07-14T00:00:00+00:00",
         "2026-07-14T00:00:00+00:00"))
    core_conn.commit()
    core_conn.close()
    conn.execute(
        "INSERT INTO codex_conversation_messages "
        "(conversation_key, source_root_key, source_path, line_offset, timestamp_utc, "
        "turn_id, call_id, kind, event_type, record_family, model, text, "
        "content_digest, content_len, detail_json, search_tool, search_thinking) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (conversation_key, source_root_key, str(source_path), 0,
         "2026-07-14T12:00:00+00:00", "t", call_id, "tool_call", None,
         "response_item", "gpt-x", "", digest, clen, None, capped, ""))
    conn.execute(
        "INSERT INTO codex_conversation_events "
        "(source_path, line_offset, source_root_key, conversation_key, record_type, "
        "event_type, turn_id, call_id, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(source_path), 0, source_root_key, conversation_key, "response_item",
         "function_call", "t", call_id, kern._canonical_json(record)))
    pathlib.Path(disk_path).write_text(json.dumps(record) + "\n", encoding="utf-8")
    conn.commit()
    return q.codex_block_key(conversation_key, source_path=str(source_path),
                             line_offset=0, content_digest=digest)


def test_payload_containment_guard_blocks_symlink_escape(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        root = tmp_path / "seed-root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        # non-escaping companion: a real file inside the root -> ok (proves the seed
        # is consistent, so the escaping case's not_found is the guard, not a miss).
        real = root / "real.jsonl"
        ok_bk = _seed_codex_tool_call(
            conn, conversation_key="conv-in", source_root_key="rk-in", root_path=root,
            source_path=real, disk_path=real, call_id="in")
        assert q.read_codex_payload(conn, "conv-in", ok_bk, "call")["status"] == "ok"
        # escaping: a symlink INSIDE the root that realpath-resolves OUTSIDE it. The
        # target file is valid + matching, so absent the guard it would read ok.
        target = outside / "secret.jsonl"
        link = root / "link.jsonl"
        link.symlink_to(target)
        bad_bk = _seed_codex_tool_call(
            conn, conversation_key="conv-esc", source_root_key="rk-esc", root_path=root,
            source_path=link, disk_path=target, call_id="esc")
        assert q.read_codex_payload(conn, "conv-esc", bad_bk, "call") == {
            "status": "not_found", "block_key": bad_bk, "which": "call"}
    finally:
        conn.close()


# ── A2: find_in_codex_conversation (§3.1) ─────────────────────────────────────


def test_find_kinds_tuple_matches_claude():
    assert q.CODEX_FIND_KINDS == lcq._FIND_KINDS == (
        "all", "prompts", "assistant", "tools", "thinking")


def test_find_anchors_byte_equal_to_detail_item_keys(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        d = q.get_codex_conversation(conn, ck, effective_speed="standard")
        detail_keys = {it["item_key"] for it in d["items"]}
        res = q.find_in_codex_conversation(conn, ck, "Synthetic", kind="all")
        assert res["status"] == "ok" and res["total"] > 0
        assert res["search_depth"] == "full" and res["kind"] == "all"
        assert all(a["item_key"] in detail_keys for a in res["anchors"])
    finally:
        conn.close()


def test_find_kind_scoping_and_fts_like_equivalence(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)

        def _anchors(query, kind, like=False):
            if like:
                conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) "
                             "VALUES('codex_fts_unavailable','1')")
            try:
                r = q.find_in_codex_conversation(conn, ck, query, kind=kind)
            finally:
                conn.execute("DELETE FROM cache_meta WHERE key='codex_fts_unavailable'")
            return r

        prompts = _anchors("Synthetic", "prompts")
        assert prompts["mode"] == "fts" and prompts["total"] >= 1
        # thinking kind matches reasoning text only.
        thinking = _anchors("reasoning", "thinking")
        assert thinking["total"] >= 1
        # prompts kind must NOT anchor the assistant turn.
        assert prompts["total"] == len(
            [it for it in q.get_codex_conversation(conn, ck, effective_speed="standard")["items"]
             if it["kind"] == "user"
             and "Synthetic" in (it["blocks"][0].get("text") or "")])
        # FTS and LIKE resolve the same anchors for a single-term query.
        fts = _anchors("Synthetic", "all")
        like = _anchors("Synthetic", "all", like=True)
        assert like["mode"] == "like"
        assert {a["item_key"] for a in fts["anchors"]} == {a["item_key"] for a in like["anchors"]}
    finally:
        conn.close()


def test_find_collapses_mirror_pair_and_caps(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["mirror-pairing"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        res = q.find_in_codex_conversation(conn, ck, "Mirror assistant reply", kind="assistant")
        # the response_item + its suppressed event_msg mirror collapse to ONE anchor.
        assert res["total"] == 1
        # cap semantics: a cap below total truncates and flags.
        capped = q.find_in_codex_conversation(conn, ck, "Repeat prompt", kind="prompts", cap=1)
        assert capped["total"] == 2 and len(capped["anchors"]) == 1
        assert capped["anchors_truncated"] is True
    finally:
        conn.close()


def test_find_regex_case_and_unknown_kind(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        rx = q.find_in_codex_conversation(conn, ck, "Synth.tic", kind="all", regex=True)
        assert rx["mode"] == "regex" and rx["total"] >= 1
        # case-sensitive substring: the exact case matches, a wrong case does not.
        assert q.find_in_codex_conversation(conn, ck, "Synthetic", kind="all", case=True)["total"] >= 1
        assert q.find_in_codex_conversation(conn, ck, "SYNTHETIC", kind="all", case=True)["total"] == 0
        with pytest.raises(ValueError):
            q.find_in_codex_conversation(conn, ck, "x", kind="title")
    finally:
        conn.close()


def test_find_pending_and_not_found():
    conn = _cache_schema()  # migration 025 NOT stamped -> pending
    try:
        _insert_msg(conn, offset=1, text="hi", conversation_key="conv-p")
        pend = q.find_in_codex_conversation(conn, "conv-p", "hi", kind="all")
        assert pend["status"] == "normalization_pending"
        assert pend["anchors"] == [] and pend["total"] == 0
    finally:
        conn.close()


# ── A3: prompts (§3.2) ────────────────────────────────────────────────────────


def test_codex_prompts_spine(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        res = q.codex_conversation_prompts(conn, ck)
        assert res["status"] == "ok" and res["conversation_key"] == ck
        assert [p["text"] for p in res["prompts"]][0] == "Synthetic first meaningful user prompt"
        # item_key aligns 1:1 with the detail's user items (the S8 spine contract).
        d = q.get_codex_conversation(conn, ck, effective_speed="standard")
        user_keys = [it["item_key"] for it in d["items"] if it["kind"] == "user"]
        assert [p["item_key"] for p in res["prompts"]] == user_keys
    finally:
        conn.close()


def test_codex_prompts_pending_and_not_found():
    conn = _cache_schema()
    try:
        _insert_msg(conn, offset=1, text="hi", conversation_key="conv-p", kind="user")
        assert q.codex_conversation_prompts(conn, "conv-p")["status"] == "normalization_pending"
    finally:
        conn.close()


# ── A4: export renderer (§3.3) ────────────────────────────────────────────────


def test_export_deterministic_and_children_as_refs(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["nested-parent", "nested-child"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        parent_ck = conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE native_thread_id = 'parent-thread-fixture'").fetchone()[0]
        child_ck = conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE parent_thread_id = 'parent-thread-fixture' "
            "AND native_thread_id != 'parent-thread-fixture'").fetchone()[0]
        env1 = q.get_codex_conversation_export(conn, parent_ck, effective_speed="standard")
        env2 = q.get_codex_conversation_export(conn, parent_ck, effective_speed="standard")
        assert env1["status"] == "ok" and env1 == env2  # deterministic
        md = env1["markdown"]
        assert md.startswith("# Parent thread question")
        assert md.endswith("\n")
        # provider-native token label vocabulary, never Claude cache vocabulary.
        assert "reasoning_output" in md and "cache_read" not in md
        # child appears as a v1. REFERENCE, never inlined.
        assert child_ck in md and "## Child conversations" in md
        child_md = q.get_codex_conversation_export(conn, child_ck, effective_speed="standard")["markdown"]
        assert child_md not in md  # the child body is not inlined into the parent
    finally:
        conn.close()


def test_export_pending_and_not_found():
    conn = _cache_schema()
    try:
        assert q.get_codex_conversation_export(
            conn, "nope", effective_speed="standard")["status"] == "normalization_pending"
    finally:
        conn.close()


# ── A6: §3.7 hit extension (both providers) ───────────────────────────────────


def test_codex_search_hits_carry_last_activity_and_project_label(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        env = q.search_codex_conversations(conn, "Synthetic", effective_speed="standard")
        assert env["hits"]
        for h in env["hits"]:
            assert "last_activity_utc" in h and "project_label" in h
            assert h["last_activity_utc"] and h["project_label"] == "project-red"
    finally:
        conn.close()


def test_claude_neutral_search_hits_carry_section_3_7_fields():
    conn = _claude_cache()
    try:
        _seed_claude_turn_pair(conn)
        env = disp.neutral_search(conn, "Claude", source="claude", kind="all")
        assert env["hits"]
        for h in env["hits"]:
            assert "last_activity_utc" in h and "project_label" in h
            assert h["last_activity_utc"] == "2026-06-01T00:00:05Z"
    finally:
        conn.close()


# ── A7: external search-cursor codec (§4.3) ───────────────────────────────────


def test_search_cursor_codec_roundtrip_and_invalid():
    raw = "v1.someconvkey\x00civ1_someitemkey"
    ext = disp.encode_search_cursor(raw)
    assert "=" not in ext and "\x00" not in ext  # unpadded, NUL never leaks
    assert disp.decode_search_cursor(ext) == raw
    assert disp.encode_search_cursor(None) is None
    assert disp.decode_search_cursor(None) is None
    with pytest.raises(disp.InvalidSearchCursor):
        disp.decode_search_cursor("@@@not-base64@@@")


def test_neutral_search_encodes_outgoing_cursor_and_decodes_incoming(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        page1 = disp.neutral_search(conn, "Synthetic", source="codex", kind="all", limit=1)
        ext_cursor = page1["page"]["cursor"]
        assert ext_cursor is not None
        # the external cursor decodes to the kernel's NUL-separated raw form.
        assert "\x00" in disp.decode_search_cursor(ext_cursor)
        # feeding the external cursor back advances the page (decoded at the boundary).
        raw_kernel = q.search_codex_conversations(
            conn, "Synthetic", kind="all", effective_speed="standard", limit=1)
        page2 = disp.neutral_search(
            conn, "Synthetic", source="codex", kind="all", limit=1, cursor=ext_cursor)
        assert page2["hits"] and page2["hits"] != page1["hits"]
        with pytest.raises(disp.InvalidSearchCursor):
            disp.neutral_search(conn, "Synthetic", source="codex", cursor="@@@bad@@@")
        assert raw_kernel["page"]["cursor"] is not None  # kernel keeps raw form
    finally:
        conn.close()


# ── A8: provider-aware anonymization builder (§3.6) ───────────────────────────


def test_anon_plan_for_sources_covers_codex_roots_cwds_labels(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        plan = lcq.build_anon_plan_for_sources(
            conn, home_dir="/home/fixture-user", sources={"codex"})
        secret_text = (CORPUS / "rollouts" / "secret-canary.jsonl").read_text()
        scrubbed = anon.scrub_text(secret_text, plan)
        # the observed project root path + its display label are scrubbed.
        assert "/synthetic/root-a/project-red" not in scrubbed
        assert "project-red" not in scrubbed
        # the caller home dir collapses to ~.
        assert "/home/fixture-user" not in scrubbed
        # documented secret patterns are redacted.
        assert "sk-fixture-not-a-secret" not in scrubbed
        assert "Bearer fixture-token" not in scrubbed
        assert "[REDACTED:" in scrubbed
    finally:
        conn.close()


def test_anon_mixed_db_leaves_legacy_builder_and_bare_claude_bytes_unchanged(tmp_path, monkeypatch):
    """Codex rows present must NOT change legacy build_anon_plan_for_db output nor
    bare-Claude export scrub bytes (the §3.6 byte-stability regression)."""
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    text = "code at /synthetic/root-a/project-red and /claude/only/proj"

    # A: Claude-only cache with one Claude cwd.
    a = _claude_cache()
    try:
        _cm(a, session_id=_SID_A, uuid="h1", offset=0, ts="2026-06-01T00:00:00Z",
            entry_type="human", text="hi", cwd="/claude/only/proj")
        plan_a = lcq.build_anon_plan_for_db(a, home_dir="/home/u")
        scrub_a = anon.scrub_text(text, plan_a)
    finally:
        a.close()

    # B: SAME Claude cwd, PLUS a fully-ingested Codex corpus (root-a rows present).
    b = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](b)
        b.execute("INSERT OR REPLACE INTO cache_meta(key,value) "
                  "VALUES('conversation_sessions_backfill_pending','1')")
        _cm(b, session_id=_SID_A, uuid="h1", offset=0, ts="2026-06-01T00:00:00Z",
            entry_type="human", text="hi", cwd="/claude/only/proj")
        plan_b = lcq.build_anon_plan_for_db(b, home_dir="/home/u")
        scrub_b = anon.scrub_text(text, plan_b)
    finally:
        b.close()

    # legacy builder ignores Codex tables entirely -> byte-identical plan + scrub.
    assert anon.plan_to_wire(plan_a) == anon.plan_to_wire(plan_b)
    assert scrub_a == scrub_b
    # and the legacy plan does NOT scrub the Codex-only root (it never saw it).
    assert "/synthetic/root-a/project-red" in scrub_a


# ── A9: dispatch ops + entity status matrix (§3, §5.6 parity) ─────────────────


def test_neutral_find_dispatch_both_providers(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        via = disp.neutral_find(conn, ck, "Synthetic", kind="all")
        assert via == q.find_occurrences_in_codex_conversation(
            conn,
            ck,
            "Synthetic",
            kind="all",
            regex=False,
            case_sensitive=False,
        )
        assert via["semantics"] == "occurrence"
        # garbage ref -> not_found; bad kind -> ValueError (route 400).
        assert disp.neutral_find(conn, "garbage", "x")["status"] == "not_found"
        with pytest.raises(ValueError):
            disp.neutral_find(conn, ck, "x", kind="title")
    finally:
        conn.close()


def test_neutral_find_claude_anchors_are_neutral_item_keys():
    conn = _claude_cache()
    try:
        _seed_claude_turn_pair(conn)
        res = disp.neutral_find(conn, _SID_A, "Claude", kind="all")
        assert res["status"] == "ok" and res["anchors"]
        conv_key = disp.resolve_conversation_ref(_SID_A).conversation_key
        # anchors carry neutral item_keys byte-equal to the detail's.
        d = disp.neutral_detail(conn, _SID_A, effective_speed="standard")
        detail_keys = {it["item_key"] for it in d["items"]}
        assert all(a["item_key"] in detail_keys for a in res["anchors"])
        assert res["conversation_key"] == conv_key
    finally:
        conn.close()


def test_neutral_prompts_dispatch_both_providers(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        assert disp.neutral_prompts(conn, ck) == q.codex_conversation_prompts(conn, ck)
        assert disp.neutral_prompts(conn, "garbage")["status"] == "not_found"
    finally:
        conn.close()
    c = _claude_cache()
    try:
        _seed_claude_turn_pair(c)
        pr = disp.neutral_prompts(c, _SID_A)
        assert pr["status"] == "ok"
        assert pr["prompts"][0]["text"] == "First Claude prompt"
        assert pr["prompts"][0]["item_key"].startswith("cliv1_")
    finally:
        c.close()


def test_neutral_export_scope_and_dispatch(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        ok = disp.neutral_export(conn, ck, scope="all", effective_speed="standard")
        assert ok["status"] == "ok" and ok["markdown"].startswith("#")
        # a non-default scope for a Codex ref is a validation error, never a fallback.
        bad = disp.neutral_export(conn, ck, scope="chat", effective_speed="standard")
        assert bad["status"] == "validation_error" and bad["reason"] == "scope"
        assert disp.neutral_export(conn, "garbage")["status"] == "not_found"
    finally:
        conn.close()
    c = _claude_cache()
    try:
        _seed_claude_turn_pair(c)
        # Claude scopes pass through unchanged (chat is a valid Claude scope).
        assert disp.neutral_export(c, _SID_A, scope="chat")["status"] == "ok"
    finally:
        c.close()


def test_neutral_payload_dispatch_codex_and_claude(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        _d, response = _detail_response_item(conn, ck)
        bk = next(b["block_key"] for b in response["blocks"]
                  if b.get("call_id") == "fn-1")
        via = disp.neutral_payload(conn, ck, which="call", block_key=bk)
        assert via == q.read_codex_payload(conn, ck, bk, "call")
        # Codex ref addressed by the Claude selector (tool_use_id) -> not_found.
        assert disp.neutral_payload(conn, ck, which="call", tool_use_id="x")["status"] == "not_found"
        assert disp.neutral_payload(conn, "garbage", which="call")["status"] == "not_found"
    finally:
        conn.close()


def test_a_find_hit_inside_a_follower_segment_anchors_to_that_segment(
        tmp_path, monkeypatch):
    """A find hit deep inside a segmented turn must anchor to ITS segment.

    ``_pos_to_item_key`` maps a physical row to the segment that contains it, but
    the anchor emission used to walk ``kern.canonical_items`` and key each entry
    by ``_item_key_for_item`` — a TURN key. A follower segment's key can never
    equal a turn key, so every hit landing past segment 0 was silently dropped
    from the anchor list: the FindBar reported fewer matches than exist and could
    not navigate to any of them. Measured on the profiled conversation before the
    fix: a token present only in the segment at detail index 30 (ordinal 24)
    produced an anchor for that turn's HEAD and none for the segment.
    """
    records = _big_turn_records()
    # A distinctive token in the LAST tool call of the turn, so it can only fall
    # in a follower segment.
    records.append({
        "payload": {"arguments": '{"needle": "zzsentinelzz"}',
                    "call_id": "fn-sentinel", "name": "fixture_function",
                    "type": "function_call"},
        "timestamp": "2026-07-14T13:00:00Z", "type": "response_item"})
    records.append({
        "payload": {"call_id": "fn-sentinel", "output": {"ok": True},
                    "type": "function_call_output"},
        "timestamp": "2026-07-14T13:00:01Z", "type": "response_item"})
    ns, _root, _rollout = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        detail = _detail_of(conn, ck, limit=0)
        found = q.find_in_codex_conversation(conn, ck, "zzsentinelzz")
    finally:
        conn.close()

    segments = [it for it in detail["items"] if it["kind"] == "assistant"]
    assert len(segments) > 1, "the turn must split, or this proves nothing"
    # The sentinel is in the turn's last rows, so the LAST segment owns it.
    owner = segments[-1]
    assert owner["segment_ordinal"] > 0, owner["segment_ordinal"]

    assert found["status"] == "ok", found
    anchors = [a["item_key"] for a in found["anchors"]]
    assert anchors == [owner["item_key"]], (
        f"expected the follower segment {owner['item_key']!r}, got {anchors!r}")
    # And the count agrees with the anchor list rather than over-reporting.
    assert found["total"] == 1, found["total"]


# ── #463 S2 §1: every row-backed block carries an anchor ─────────────────────


def _prose_reasoning_and_tools_records(*, turn_id="turn-a"):
    """A single turn holding prose, reasoning and a tool call/output pair.

    The three block families #463 S2 §1 is about: `tool_call` already carried a
    `block_key`, prose and reasoning carried none.
    """
    records = [
        {"payload": {"context_window": 272000,
                     "cwd": "/synthetic/root-a/project-red",
                     "git": {"branch": "b", "repository": "r"},
                     "id": "root-thread-s2", "instructions": "x",
                     "model": "gpt-x", "model_context_window": 272000,
                     "model_provider": "p",
                     "session_id": "44444444-4444-4444-8444-444444444444",
                     "source": "codex", "thread_source": "root-thread-s2",
                     "tools": [{"name": "t"}], "user": "u"},
         "timestamp": "2026-07-14T12:00:00Z", "type": "session_meta"},
        {"payload": {"model": "gpt-x", "model_context_window": 272000,
                     "turn_id": turn_id},
         "timestamp": "2026-07-14T12:01:00Z", "type": "turn_context"},
        {"payload": {"content": [{"text": "Synthetic S2 prompt",
                                  "type": "input_text"}],
                     "phase": "input", "role": "user", "type": "message"},
         "timestamp": "2026-07-14T12:02:00Z", "type": "response_item"},
        {"payload": {"content": [{"text": "First assistant paragraph",
                                  "type": "output_text"}],
                     "phase": "output", "role": "assistant", "type": "message"},
         "timestamp": "2026-07-14T12:03:00Z", "type": "response_item"},
        {"payload": {"content": [{"text": "Second assistant paragraph",
                                  "type": "output_text"}],
                     "phase": "output", "role": "assistant", "type": "message"},
         "timestamp": "2026-07-14T12:04:00Z", "type": "response_item"},
        {"payload": {"content": [{"text": "Reasoning body", "type": "reasoning_text"}],
                     "encrypted_content": "enc",
                     "summary": [{"text": "**Planning the synthetic turn**",
                                  "type": "summary_text"}],
                     "type": "reasoning"},
         "timestamp": "2026-07-14T12:05:00Z", "type": "response_item"},
        {"payload": {"arguments": "{}", "call_id": "fn-s2",
                     "name": "fixture_function", "type": "function_call"},
         "timestamp": "2026-07-14T12:06:00Z", "type": "response_item"},
        {"payload": {"call_id": "fn-s2", "output": {"ok": True},
                     "type": "function_call_output"},
         "timestamp": "2026-07-14T12:07:00Z", "type": "response_item"},
    ]
    return records


def _all_blocks(page):
    return [(item["item_key"], block)
            for item in page["items"] for block in item["blocks"]]


def test_every_row_backed_block_carries_a_block_key(tmp_path, monkeypatch):
    """#463 S2 §1. Prose, reasoning and event blocks were keyless; only
    tool_call blocks carried an anchor. A finer reading unit has to anchor on a
    block key, because it is one of only two identities S1 declares
    unconditionally durable."""
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _prose_reasoning_and_tools_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        page = _detail_of(conn, _single_ck(conn), limit=0)
    finally:
        conn.close()
    keyless = [(item_key, block["kind"])
               for item_key, block in _all_blocks(page)
               if block.get("block_key") is None]
    assert keyless == [], f"blocks without an anchor: {keyless}"


def test_block_keys_are_unique_within_a_conversation(tmp_path, monkeypatch):
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _prose_reasoning_and_tools_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        page = _detail_of(conn, _single_ck(conn), limit=0)
    finally:
        conn.close()
    keys = [block["block_key"] for _item_key, block in _all_blocks(page)
            if block.get("block_key")]
    assert keys, "non-vacuity: the fixture must produce keyed blocks"
    assert len(keys) == len(set(keys))


def test_a_block_key_is_unchanged_by_segmentation(tmp_path, monkeypatch):
    """Block keys are derived per physical row and must be unaffected by where
    segment boundaries fall (#463 S1 contract)."""
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _big_turn_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        segmented = _detail_of(conn, ck, limit=0)
        unsegmented = _detail_of(conn, ck, limit=0, legacy_export=True)
    finally:
        conn.close()
    ordinals = {item["segment_ordinal"] for item in segmented["items"]}
    assert max(ordinals) > 0, "the turn must split, or this proves nothing"
    assert sorted(block["block_key"] for _k, block in _all_blocks(segmented)
                  if block.get("block_key")) == sorted(
        block["block_key"] for _k, block in _all_blocks(unsegmented)
        if block.get("block_key"))


def test_a_prose_block_key_does_not_become_payload_readable(tmp_path, monkeypatch):
    """#463 S2 §1.1. Generalizing the anchor must not widen the payload route.

    Stable identity and payload-capability are different properties: a prose
    block has a key and no retained payload, so a consumer must never infer
    payload availability from the presence of a key.
    """
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _prose_reasoning_and_tools_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        page = _detail_of(conn, ck, limit=0)
        prose_key = next(
            block["block_key"] for _k, block in _all_blocks(page)
            if block["kind"] == "assistant")
        read = q.read_codex_payload(conn, ck, prose_key, "call")
        located = q._locate_payload_block(conn, ck, prose_key)
    finally:
        conn.close()
    assert located is None, located
    assert read["status"] == "not_found", read


# ── #463 S2 §2.5: reasoning headings on the detail wire ──────────────────────


_MULTI_HEADING_SUMMARY = [
    {"text": "**Planning concurrency test**\n**Designing monkeypatch**",
     "type": "summary_text"},
]


def _multi_heading_records(*, turn_id="turn-a", summary=None):
    """One turn whose reasoning aggregate holds several authored headings."""
    records = _prose_reasoning_and_tools_records(turn_id=turn_id)
    for record in records:
        if record["type"] == "response_item" and \
                record["payload"].get("type") == "reasoning":
            record["payload"]["summary"] = summary or list(_MULTI_HEADING_SUMMARY)
            record["payload"].pop("content", None)
    return records


def _first_reasoning_block(page):
    return next(block for item in page["items"] for block in item["blocks"]
                if block["kind"] == "reasoning")


def _stored_reasoning_projections(conn, ck):
    return sorted(
        row[0] for row in conn.execute(
            "SELECT detail_json FROM codex_conversation_messages "
            "WHERE conversation_key = ? AND kind = 'reasoning' "
            "ORDER BY source_path, line_offset", (ck,)))


def _heading_page(tmp_path, monkeypatch, records=None):
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, records or _multi_heading_records())
    conn = ns["open_cache_db"]()
    ns["sync_codex_cache"](conn)
    return conn, _single_ck(conn)


def test_a_multi_heading_aggregate_publishes_one_heading_per_authored_heading(
    tmp_path, monkeypatch,
):
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    block = _first_reasoning_block(page)
    assert [h["text"] for h in block["detail"]["reasoning"]["headings"]] == [
        "Planning concurrency test", "Designing monkeypatch"]


def test_heading_keys_are_the_block_key_plus_a_zero_based_ordinal(
    tmp_path, monkeypatch,
):
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    block = _first_reasoning_block(page)
    bk = block["block_key"]
    assert [h["key"] for h in block["detail"]["reasoning"]["headings"]] == [
        f"{bk}#0", f"{bk}#1"]


def test_the_nested_schema_version_does_not_move(tmp_path, monkeypatch):
    """§2.5. Bumping it makes `codexReasoning` fall back to block.text for every
    existing client, so the field is additive precisely to avoid that."""
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    assert _first_reasoning_block(page)["detail"]["reasoning"]["schema_version"] == 1


def test_stored_title_summary_and_body_are_untouched(tmp_path, monkeypatch):
    """§2.2. These feed `_row_is_reasoning_title`, which is a segmentation
    boundary input. Moving them moves segment boundaries."""
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        before = _stored_reasoning_projections(conn, ck)
        page = _detail_of(conn, ck, limit=0)
        after = _stored_reasoning_projections(conn, ck)
    finally:
        conn.close()
    assert before == after
    # And the stored projection carries NO headings — decomposition is read-time.
    assert all("headings" not in (row or "") for row in after)
    # Non-vacuity: the served block DID gain them, so this is not passing
    # because nothing was decomposed at all.
    assert _first_reasoning_block(page)["detail"]["reasoning"]["headings"]


def test_headings_are_absent_under_legacy_export(tmp_path, monkeypatch):
    """§2.5. legacy_export loads only marker-bearing payloads; populating
    headings there would force a whole-conversation payload read for a field the
    exporter never reads."""
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        page = _detail_of(conn, ck, limit=0, legacy_export=True)
    finally:
        conn.close()
    assert "headings" not in _first_reasoning_block(page)["detail"]["reasoning"]


def test_headings_are_omitted_when_the_retained_payload_is_missing(
    tmp_path, monkeypatch,
):
    """§2.3. Decomposition never fails the request and never partially
    populates."""
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        conn.execute("DELETE FROM codex_conversation_events "
                     "WHERE conversation_key = ?", (ck,))
        conn.commit()
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    reasoning = _first_reasoning_block(page)["detail"]["reasoning"]
    assert "headings" not in reasoning
    assert reasoning.get("summary") or reasoning.get("title")


def test_headings_are_omitted_when_the_retained_payload_is_malformed(
    tmp_path, monkeypatch,
):
    """§2.3 again, on the OTHER degradation: a payload that is present but whose
    `summary` is not a list of text-bearing entries. All-or-nothing — the field
    is omitted entirely rather than partially populated."""
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        conn.execute(
            "UPDATE codex_conversation_events "
            "SET payload_json = json_set(payload_json, '$.payload.summary', "
            "  json('\"not-a-list\"')) "
            "WHERE conversation_key = ? AND record_type = 'response_item' "
            "AND json_extract(payload_json, '$.payload.type') = 'reasoning'",
            (ck,))
        conn.commit()
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    assert "headings" not in _first_reasoning_block(page)["detail"]["reasoning"]


def test_headings_come_from_summary_entries_not_from_body(tmp_path, monkeypatch):
    """§2.3. Body remains disclosure content and is never decomposed."""
    records = _multi_heading_records()
    for record in records:
        if record["type"] == "response_item" and \
                record["payload"].get("type") == "reasoning":
            record["payload"]["content"] = [
                {"text": "**Body heading that must not appear**",
                 "type": "reasoning_text"}]
    conn, ck = _heading_page(tmp_path, monkeypatch, records)
    try:
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    texts = [h["text"] for h in
             _first_reasoning_block(page)["detail"]["reasoning"]["headings"]]
    assert texts == ["Planning concurrency test", "Designing monkeypatch"]


def test_each_summary_entry_contributes_in_order(tmp_path, monkeypatch):
    """The aggregate's heading list is the per-entry results concatenated in
    entry order, not a re-split of the joined string."""
    records = _multi_heading_records(summary=[
        {"text": "**A**", "type": "summary_text"},
        {"text": "**B**\n**C**", "type": "summary_text"},
    ])
    conn, ck = _heading_page(tmp_path, monkeypatch, records)
    try:
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    reasoning = _first_reasoning_block(page)["detail"]["reasoning"]
    assert [h["text"] for h in reasoning["headings"]] == ["A", "B", "C"]
    bk = _first_reasoning_block(page)["block_key"]
    assert [h["key"] for h in reasoning["headings"]] == [
        f"{bk}#0", f"{bk}#1", f"{bk}#2"]


def test_reasoning_title_boundaries_are_computed_from_the_stored_projection(
    tmp_path, monkeypatch,
):
    """§2.2's real risk, stated as a test.

    `_row_is_reasoning_title` decides a segmentation boundary by reading the
    STORED reasoning projection, so it must see exactly what it saw before S2. A
    multi-heading aggregate has never produced a stored `title` — the whole
    reason its headings were unreachable — and publishing `headings` at read time
    must not change that, or the boundary would move.

    The cross-branch pin for segment boundaries themselves is the committed
    `wire-detail-segmented.json`, whose reasoning rows sit inside boundary
    windows and which `bin/cctally-frontend-test` byte-compares against a fresh
    regeneration.
    """
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        rows = [kern.CodexNormalizedRow(*row) for row in conn.execute(
            "SELECT " + q._ROW_COLS + " FROM codex_conversation_messages "
            "WHERE conversation_key = ? AND kind = 'reasoning'", (ck,))]
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    assert rows, "non-vacuity: the fixture must carry a reasoning row"
    # Multi-heading aggregate: no stored title, therefore no title boundary.
    assert all(not q._row_is_reasoning_title(row) for row in rows)
    # And it DID decompose, so the two facts are being asserted about the same
    # row rather than about an aggregate that produced nothing.
    assert len(_first_reasoning_block(page)["detail"]["reasoning"]["headings"]) == 2


# --- #463 S3: tool legibility ------------------------------------------------
#
# Spec docs/superpowers/specs/2026-08-03-463-s3-tool-legibility-design.md.
#
# The invariant these tests exist to hold, and the one that is easiest to break
# without noticing: EVERY card addition below is computed at READ time.
# `_extract` keeps storing today's bounded card unchanged (spec section 3.0),
# because `_row_source_bytes` is a row's content length plus its stored
# `detail_json` byte length and that total drives `PAGE_SOURCE_BYTE_BUDGET` page
# boundaries. Persisting the enrichment would make two conversations with
# identical content paginate differently according to which binary ingested them.


def _s3_stored_detail(tmp_path, monkeypatch, records):
    """`(rows_by_call_id, conn, conversation_key)` for a staged record list.

    Returns the STORED `detail_json` per row, which is what the read-time-only
    rule is asserted against.
    """
    ns, _root, _rollout = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    ns["sync_codex_cache"](conn)
    ck = _single_ck(conn)
    stored = {}
    for call_id, kind, detail_json in conn.execute(
        "SELECT call_id, kind, detail_json FROM codex_conversation_messages "
        "WHERE conversation_key = ? ORDER BY source_path, line_offset", (ck,)
    ):
        stored[(call_id, kind)] = detail_json
    return stored, conn, ck


def test_s3_output_card_resolves_status_and_strips_the_preamble():
    payload = {"type": "custom_tool_call_output", "call_id": "c1",
               "output": [{"type": "input_text",
                           "text": "Script failed\nWall time 2 seconds\nOutput:\n"},
                          {"type": "input_text", "text": "boom\n"}]}
    card, body = kern.decode_tool_output_card(payload)
    assert card["status"] == "failed" and card["is_error"] is True
    assert card["wall_time_seconds"] == 2.0
    assert "Script failed" not in body and "Wall time" not in body
    assert body == "boom\n"


def test_s3_running_is_not_an_error_and_leaks_no_session_id():
    payload = {"type": "function_call_output", "call_id": "c2",
               "output": "Chunk ID: aa11\nWall time: 1 seconds\n"
                         "Process running with session ID 59671\n"
                         "Original token count: 0\nOutput:\nrunning...\n"}
    card, body = kern.decode_tool_output_card(payload)
    assert card["status"] == "running" and card["is_error"] is False
    assert "59671" not in json.dumps(card)
    assert body == "running...\n"


def test_s3_output_card_publishes_exit_code_and_wall_time_per_grammar():
    """Every grammar, on the card rather than only in the reader (section 4.3)."""
    cases = [
        ("Chunk ID: 8c93bf\nWall time: 0.9007 seconds\n"
         "Process exited with code 0\nOriginal token count: 31\nOutput:\ndone\n",
         "completed", 0, 0.9007, False, "done\n"),
        ("Chunk ID: 8c93bf\nWall time: 0.9007 seconds\n"
         "Process exited with code 127\nOriginal token count: 31\n"
         "Output:\nnot found\n",
         "failed", 127, 0.9007, True, "not found\n"),
        ("Exit code: 0\nWall time: 0.0304 seconds\nOutput:\nSuccess\n",
         "completed", 0, 0.0304, False, "Success\n"),
        ("Wall time: 0.7312 seconds\nOutput:\n{}\n",
         "unknown", None, 0.7312, False, "{}\n"),
    ]
    for raw, status, exit_code, wall, is_error, body_text in cases:
        card, body = kern.decode_tool_output_card(
            {"type": "function_call_output", "call_id": "c", "output": raw})
        assert card["status"] == status, raw
        assert card["exit_code"] == exit_code, raw
        assert card["wall_time_seconds"] == wall, raw
        assert card["is_error"] is is_error, raw
        assert body == body_text, raw
        assert "chunk_id" not in card and "session_id" not in card, raw


def test_s3_an_output_with_no_preamble_is_left_exactly_as_it_was():
    """The 10,177 no-preamble outputs, untouched by construction (section 4.1)."""
    raw = "usage: git [--version] [--help]\n"
    card, body = kern.decode_tool_output_card(
        {"type": "function_call_output", "call_id": "c", "output": raw})
    assert card["status"] == "unknown"
    assert card["is_error"] is False
    assert card["exit_code"] is None and card["wall_time_seconds"] is None
    assert body == raw


def test_s3_extract_stores_the_frozen_card_and_never_the_enrichment(
    tmp_path, monkeypatch,
):
    """Spec section 3.0. The enrichment must not reach `detail_json`.

    A stored card that resolved its status, dropped its preamble part or gained
    `exit_code` would grow `detail_json` for newly ingested rows while historical
    rows kept their old estimates, so `_row_source_bytes` — and therefore the
    page boundary — would depend on which binary did the ingest.
    """
    records = _codex_turn_records([
        {"call_id": "s3-out", "input": "irrelevant", "name": "exec",
         "status": "completed", "type": "custom_tool_call"},
        {"call_id": "s3-out", "type": "custom_tool_call_output",
         "output": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nSuccess\n"},
    ])
    stored, conn, _ck = _s3_stored_detail(tmp_path, monkeypatch, records)
    try:
        card = json.loads(stored[("s3-out", "tool_output")])["card"]
    finally:
        conn.close()
    assert card["status"] == "unknown"
    assert "exit_code" not in card
    assert "wall_time_seconds" not in card
    # The preamble is still IN the stored part, because stripping it is a
    # read-time projection.
    assert card["parts"][0]["text"].startswith("Exit code: 0\n")


def test_s3_unrecognised_preamble_still_takes_the_call_cards_status(
    tmp_path, monkeypatch,
):
    """Spec section 4.6: the existing backfill keeps covering the remainder.

    `_item_blocks_with_rows` copies a call card's status onto an output card
    whose status is `unknown`. It is gated on `unknown`, so it stops firing for
    the 82.4% that now resolve and continues to cover the rest. This asserts the
    'rest' half directly, because a reader could easily conclude from the
    reduced firing rate that it had regressed.
    """
    records = _codex_turn_records([
        {"call_id": "s3-nopre", "name": "exec", "status": "failed",
         "type": "custom_tool_call",
         "input": ('const r = await tools.exec_command({cmd: "printf ok"}); '
                   'text(r.output);')},
        {"call_id": "s3-nopre", "type": "custom_tool_call_output",
         "output": "no preamble at all, just output\n"},
    ])
    ns, _root, _rollout = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        detail = _detail_of(conn, _single_ck(conn), limit=0)
    finally:
        conn.close()
    block = next(b for item in detail["items"] for b in item["blocks"]
                 if b.get("call_id") == "s3-nopre")
    output_card = block["output"]["detail"]["card"]
    # The preamble reader recognized nothing, so the call card's own status is
    # what reaches the output card — exactly today's behaviour.
    assert output_card["status"] == "failed"
    assert output_card["is_error"] is True
    assert block["output"]["text"] == "no preamble at all, just output\n"


# --- #463 S3 Task 3: the patch dict branch and synthesized diffs -------------


def test_s3_patch_dict_changes_decode_per_file():
    payload = {"type": "patch_apply_end", "status": "completed", "success": True,
               "stdout": "", "stderr": "",
               "changes": {
                   "/synthetic/a.py": {"move_path": None, "type": "update",
                                       "unified_diff": "@@ -1 +1 @@\n-old\n+new\n"},
                   "/synthetic/b.py": {"type": "add", "content": "one\ntwo\n"},
                   "/synthetic/c.py": {"type": "delete", "content": "gone\n"},
                   "/synthetic/d.py": {"type": "add", "content": ""}}}
    card = kern.decode_patch_event_card(payload)
    assert card["has_diff"] is True
    by_path = {f["path"]: f for f in card["files"]}
    assert by_path["/synthetic/a.py"]["status"] == "update"
    assert by_path["/synthetic/a.py"]["diff_source"] == "retained"
    assert by_path["/synthetic/b.py"]["diff_source"] == "derived"
    assert by_path["/synthetic/b.py"]["unified_diff"] == (
        "--- /dev/null\n+++ /synthetic/b.py\n@@ -0,0 +1,2 @@\n+one\n+two\n")
    assert by_path["/synthetic/c.py"]["unified_diff"] == (
        "--- /synthetic/c.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-gone\n")
    assert by_path["/synthetic/d.py"]["unified_diff"] == (
        "--- /dev/null\n+++ /synthetic/d.py\n@@ -0,0 +0,0 @@\n")
    # Payload key order is iteration order; `json.loads` preserves it.
    assert [f["path"] for f in card["files"]] == [
        "/synthetic/a.py", "/synthetic/b.py", "/synthetic/c.py", "/synthetic/d.py"]


def test_s3_patch_missing_trailing_newline_marker():
    payload = {"type": "patch_apply_end", "status": "completed", "success": True,
               "stdout": "", "stderr": "",
               "changes": {"/synthetic/e.py": {"type": "add", "content": "no-eol"}}}
    card = kern.decode_patch_event_card(payload)
    assert card["files"][0]["unified_diff"] == (
        "--- /dev/null\n+++ /synthetic/e.py\n@@ -0,0 +1,1 @@\n+no-eol\n"
        "\\ No newline at end of file\n")


def test_s3_one_huge_file_does_not_starve_its_siblings():
    payload = {"type": "patch_apply_end", "status": "completed", "success": True,
               "stdout": "", "stderr": "",
               "changes": {"/synthetic/big.py": {"type": "delete",
                                                 "content": "x\n" * 40000},
                           "/synthetic/small.py": {"type": "add",
                                                   "content": "kept\n"}}}
    card = kern.decode_patch_event_card(payload)
    by_path = {f["path"]: f for f in card["files"]}
    assert by_path["/synthetic/big.py"]["truncated"] is True
    assert "kept" in by_path["/synthetic/small.py"]["unified_diff"]
    assert by_path["/synthetic/small.py"]["truncated"] is False
    # Cut at a line boundary, never mid-line, so what is shown still parses.
    assert by_path["/synthetic/big.py"]["unified_diff"].endswith("\n")


def test_s3_patch_list_shape_unchanged():
    # The existing list branch keeps its diff bytes and gains only `diff_source`.
    payload = {"type": "patch_apply_end", "status": "completed", "success": True,
               "stdout": "", "stderr": "",
               "changes": [{"path": "/synthetic/a.py", "status": "update",
                            "unified_diff": "@@ -1 +1 @@\n-old\n+new\n"}]}
    card = kern.decode_patch_event_card(payload)
    assert card["files"][0]["unified_diff"].endswith("-old\n+new\n")
    assert card["files"][0]["diff_source"] == "retained"


def test_s3_patch_dict_entry_that_is_not_an_object_degrades_without_a_partial_card():
    payload = {"type": "patch_apply_end", "status": "completed", "success": True,
               "stdout": "", "stderr": "",
               "changes": {"/synthetic/weird.py": ["not", "an", "object"]}}
    card = kern.decode_patch_event_card(payload)
    entry = card["files"][0]
    assert entry["path"] == "/synthetic/weird.py"
    assert "unified_diff" not in entry
    assert entry["raw"].startswith("[")
    assert card["has_diff"] is False


def test_s3_a_clip_that_keeps_no_diff_body_publishes_no_unified_diff():
    """Spec section 3.1: `has_diff` means a diff is RENDERABLE.

    A minified single-line file is one physical line, so a clip at a line
    boundary keeps the `---`/`+++`/`@@` headers and nothing else — or, when the
    first line is already longer than the share, nothing at all. Either way the
    client receives a `unified_diff` it cannot render while `has_diff` says it
    can. The key is omitted instead; `truncated` and `diff_source` stay, so the
    reader is told the diff exists and was cut rather than told nothing.
    """
    # A one-line ~20,000-character delete: the whole file body is a single line.
    payload = {"type": "patch_apply_end", "status": "completed", "success": True,
               "stdout": "", "stderr": "",
               "changes": {"/synthetic/min.js": {"type": "delete",
                                                 "content": "x" * 20000}}}
    card = kern.decode_patch_event_card(payload)
    entry = card["files"][0]
    assert "unified_diff" not in entry, entry.get("unified_diff", "")[:120]
    assert entry["truncated"] is True
    assert entry["diff_source"] == "derived"
    assert card["has_diff"] is False

    # The same defect on the RETAINED side: a provider diff for a minified file
    # is a hunk header plus two enormous lines, so the clip keeps the header.
    retained = {"type": "patch_apply_end", "status": "completed", "success": True,
                "stdout": "", "stderr": "",
                "changes": {"/synthetic/min.js": {
                    "type": "update",
                    "unified_diff": "@@ -1 +1 @@\n-" + "x" * 20000
                                    + "\n+" + "y" * 20000 + "\n"}}}
    card = kern.decode_patch_event_card(retained)
    entry = card["files"][0]
    assert "unified_diff" not in entry, entry.get("unified_diff", "")[:120]
    assert entry["truncated"] is True
    assert entry["diff_source"] == "retained"
    assert card["has_diff"] is False


def test_s3_every_patch_file_entry_carries_its_own_truncated_flag():
    """Spec section 3.1 requires per-file `truncated` on EVERY file entry.

    It reached only dict-branch entries that carried a diff, so a client would
    have had to treat absent as false — and the two branches disagreed with each
    other about whether the field existed at all.
    """
    dict_payload = {
        "type": "patch_apply_end", "status": "completed", "success": True,
        "stdout": "", "stderr": "",
        "changes": {
            "/synthetic/a.py": {"type": "update",
                                "unified_diff": "@@ -1 +1 @@\n-old\n+new\n"},
            "/synthetic/b.py": {"type": "update"},        # no diff at all
            "/synthetic/c.py": ["not", "an", "object"]}}  # the raw entry
    card = kern.decode_patch_event_card(dict_payload)
    assert [entry.get("truncated") for entry in card["files"]] == [
        False, False, False]

    list_payload = {
        "type": "patch_apply_end", "status": "completed", "success": True,
        "stdout": "", "stderr": "",
        "changes": [{"path": "/synthetic/a.py", "status": "update",
                     "unified_diff": "@@ -1 +1 @@\n-old\n+new\n"},
                    {"path": "/synthetic/b.py", "status": "update"},
                    ["not", "an", "object"]]}
    served = kern.decode_patch_event_card(list_payload)
    assert [entry.get("truncated") for entry in served["files"]] == [
        False, False, False]
    # STORED stays byte-identical. Per-file `truncated` is read-time only for the
    # same reason `diff_source` is: `detail_json` bytes feed `_row_source_bytes`
    # and therefore the page boundary (spec section 3.0).
    stored = kern.decode_patch_event_card(list_payload, for_storage=True)
    assert all("truncated" not in entry for entry in stored["files"])


def test_s3_the_synthesized_diff_header_names_the_providers_path(monkeypatch):
    """Spec section 3.1, and the rule the patch card family exists for.

    `entry["path"]` is what the shared budget kept; naming THAT in the
    `---`/`+++` header would make the one card family whose purpose is
    provider-truthfulness assert a file that does not exist. The unclipped path
    goes into the header and the shared allocation clips the result, exactly as
    `_allocate_diff_budget` already does for a retained diff.
    """
    seen: list[str] = []
    real = kern._synthesized_unified_diff

    def spy(path, kind, content):
        seen.append(path)
        return real(path, kind, content)

    monkeypatch.setattr(kern, "_synthesized_unified_diff", spy)
    path = "/synthetic/" + "d" * 400 + "/deep.py"
    payload = {"type": "patch_apply_end", "status": "completed", "success": True,
               "stdout": "s" * 15900, "stderr": "",
               "changes": {path: {"type": "add", "content": "one\n"}}}
    card = kern.decode_patch_event_card(payload)
    # Non-vacuity: the budget really did clip the published path, which is the
    # only condition under which the two could differ.
    assert card["files"][0]["path"] != path
    assert seen == [path]


def test_s3_extract_stores_the_frozen_patch_card(tmp_path, monkeypatch):
    """Spec section 3.0 again, for the branch that grows the card the most.

    A dict-shaped `patch_apply_end` averages 3,812 characters of `add` content
    and 20,859 of `delete` content per entry. Persisting the decoded per-file
    diffs would be the single largest `detail_json` growth in S3, and it would
    move `PAGE_SOURCE_BYTE_BUDGET` boundaries for newly ingested rows only.
    """
    records = _codex_turn_records([]) + [{
        "timestamp": "2026-07-14T12:30:00Z", "type": "event_msg",
        "payload": {
            "type": "patch_apply_end", "status": "completed", "success": True,
            "stdout": "", "stderr": "", "turn_id": "turn-a",
            "call_id": "s3-frozen-patch",
            "changes": {"/synthetic/f.py": {"type": "add", "content": "one\ntwo\n"}}}}]
    stored, conn, _ck = _s3_stored_detail(tmp_path, monkeypatch, records)
    try:
        card = json.loads(stored[("s3-frozen-patch", "event")])["card"]
    finally:
        conn.close()
    assert card["has_diff"] is False
    assert card["files"] == [{"raw": '{"/synthetic/f.py":{"content":"one\\ntwo\\n",'
                                     '"type":"add"}}'}]


# --- #463 S3 remediation B1: the call side decodes the envelope it retains ---
#
# The call-side card held the complete `*** Begin Patch` envelope in `patch`,
# named each file and its status, and then published a file LIST with no diffs —
# so the reader was shown `1 file · 0 diffs` and "no diff retained" over a card
# that was holding the whole patch. The event side already renders full diffs for
# the sibling `patch_apply_end`, so the two patch surfaces contradicted each
# other on the same conversation.

_S3_CALL_PATCH = (
    "*** Begin Patch\n"
    "*** Update File: synthetic-legible.txt\n"
    "@@\n"
    "-old\n"
    "+new\n"
    "*** Add File: synthetic-added.txt\n"
    "+one\n"
    "+two\n"
    "*** Delete File: synthetic-gone.txt\n"
    "*** End Patch"
)


def _s3_call_patch_card(patch: str = _S3_CALL_PATCH, **kwargs):
    payload = {"type": "custom_tool_call", "name": "apply_patch",
               "status": "completed", "input": patch}
    return kern.decode_tool_call_card(payload, **kwargs)


def test_s3_call_side_patch_decodes_its_retained_envelope_into_per_file_diffs():
    card = _s3_call_patch_card()
    by_path = {entry["path"]: entry for entry in card["files"]}
    # No `has_diff` on this side: the call card publishes no such key and the
    # client derives renderability from the entries themselves.
    assert "has_diff" not in card
    assert any(entry.get("unified_diff") for entry in card["files"])
    # An UPDATE section carries the provider's own body rows. The offsets are
    # relative because the V4A envelope states none, which is the same contract
    # the edit-diff card has always rendered under.
    assert by_path["synthetic-legible.txt"]["unified_diff"] == (
        "--- synthetic-legible.txt\n+++ synthetic-legible.txt\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n")
    # An ADD section produces the SAME bytes the event side synthesizes from
    # retained `content`, so one patch does not read two ways.
    assert by_path["synthetic-added.txt"]["unified_diff"] == (
        "--- /dev/null\n+++ synthetic-added.txt\n@@ -0,0 +1,2 @@\n+one\n+two\n")
    # Every entry states its provenance and its own truncation.
    for entry in (by_path["synthetic-legible.txt"], by_path["synthetic-added.txt"]):
        assert entry["diff_source"] == "derived"
        assert entry["truncated"] is False
    # A V4A delete names the file and carries no body at all, so the card claims
    # no diff — and says only that, rather than "no diff retained".
    gone = by_path["synthetic-gone.txt"]
    assert "unified_diff" not in gone and "diff_source" not in gone
    assert gone["truncated"] is False
    # The raw envelope is still published for the payload disclosure.
    assert card["patch"] == _S3_CALL_PATCH


def test_s3_call_side_patch_diffs_are_read_time_only():
    """Spec section 3.0 — `detail_json` bytes feed page boundaries."""
    stored = _s3_call_patch_card(for_storage=True)
    assert stored["files"] == [
        {"path": "synthetic-legible.txt", "status": "modified"},
        {"path": "synthetic-added.txt", "status": "added"},
        {"path": "synthetic-gone.txt", "status": "deleted"},
    ]
    assert stored["patch"] == _S3_CALL_PATCH


def test_s3_call_side_patch_carries_a_move_and_numbers_hunks_in_sequence():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: synthetic-old.txt\n"
        "*** Move to: synthetic-new.txt\n"
        "@@\n"
        " keep\n"
        "-a\n"
        "+b\n"
        "@@\n"
        " also\n"
        "+c\n"
        "*** End Patch"
    )
    entry = _s3_call_patch_card(patch)["files"][0]
    assert entry["move_path"] == "synthetic-new.txt"
    # The second hunk continues the running count rather than restarting at 1,
    # so two hunks of one file do not render the same gutter numbers twice.
    assert entry["unified_diff"] == (
        "--- synthetic-old.txt\n+++ synthetic-new.txt\n"
        "@@ -1,2 +1,2 @@\n keep\n-a\n+b\n"
        "@@ -3,1 +3,2 @@\n also\n+c\n")


def test_s3_call_side_patch_diff_is_clipped_within_the_shared_budget():
    """The derived diffs are an ALLOCATION of the card's one text budget.

    They are taken before the raw `patch`, because `patch` is the payload
    disclosure's copy and the reader never renders it, while the diffs are the
    only thing on the card a reader can actually read.
    """
    body = "".join(f"+line {i}\n" for i in range(4000))
    patch = f"*** Begin Patch\n*** Add File: synthetic-huge.txt\n{body}*** End Patch"
    card = _s3_call_patch_card(patch)
    entry = card["files"][0]
    assert entry["truncated"] is True
    assert entry["diff_source"] == "derived"
    # Clipped at a line boundary, so what survived still parses as a hunk.
    assert entry["unified_diff"].endswith("\n")
    assert card["truncated"] is True
    assert len(json.dumps(card)) < 2 * kern.CODEX_TEXT_CAP


# --- #463 S3 Task 4: the program card and the function_call families ---------


def test_s3_wholly_recognised_exec_still_produces_a_terminal_card():
    """The 19,960 calls that already decode must be byte-unchanged."""
    supported = ('const r = await tools.exec_command({cmd: "printf ok", '
                 'workdir: "/synthetic", yield_time_ms: 10000}); text(r.output);')
    card = kern.decode_tool_call_card(
        {"type": "custom_tool_call", "name": "exec", "status": "completed",
         "input": supported})
    assert card["type"] == "terminal"
    assert card["commands"][0]["command"] == "printf ok"
    # No `complete` key: the terminal card's bytes do not move.
    assert "complete" not in card


def test_s3_mixed_program_produces_a_program_card():
    source = ('const names = ALL_TOOLS.filter(x => x.name);\n'
              'const r = await tools.exec_command({cmd: "ls"});\n'
              'const w = await tools.write_stdin({session_id: 7, chars: "y"});\n')
    card = kern.decode_tool_call_card(
        {"type": "custom_tool_call", "name": "exec", "status": "completed",
         "input": source})
    assert card["type"] == "program" and card["complete"] is False
    kinds = [i["kind"] for i in card["invocations"]]
    assert kinds == ["command", "session"]
    assert card["invocations"][1]["ref"] == "7"
    assert card["invocations"][1]["operation"] == "write"
    assert card["invocations"][1]["scope"] == "shell"


def test_s3_function_call_families():
    assert kern.decode_secondary_tool_call_card(
        {"type": "function_call", "name": "exec_command",
         "arguments": '{"cmd": "ls", "workdir": "/synthetic", "tty": true}'}
    )["type"] == "terminal"
    sess = kern.decode_secondary_tool_call_card(
        {"type": "function_call", "name": "write_stdin",
         "arguments": '{"session_id": 7, "chars": "hello"}'})
    assert sess["type"] == "session_ref" and sess["scope"] == "shell"
    assert sess["operation"] == "write" and sess["chars"] == "hello"
    cell = kern.decode_secondary_tool_call_card(
        {"type": "function_call", "name": "wait", "arguments": '{"cell_id": "12"}'})
    assert cell["scope"] == "cell" and cell["operation"] == "poll"
    js = kern.decode_secondary_tool_call_card(
        {"type": "function_call", "name": "js",
         "arguments": '{"code": "const r = await tools.exec_command({cmd: \\"ls\\"});",'
                      ' "title": "list files"}'})
    assert js["type"] == "program" and js["title"] == "list files"
    ts = kern.decode_secondary_tool_call_card(
        {"type": "function_call", "name": "tool_search_call",
         "arguments": '{"query": "github", "limit": 5}'})
    assert ts["type"] == "tool_search" and ts["query"] == "github"


def test_s3_unknown_family_still_refuses():
    assert kern.decode_secondary_tool_call_card(
        {"type": "function_call", "name": "totally_unknown",
         "arguments": '{"x": 1}'}) is None


def test_s3_exec_command_maps_onto_the_existing_terminal_card():
    """Spec section 3.2: no new type, and `BashCard` renders it unchanged."""
    card = kern.decode_secondary_tool_call_card(
        {"type": "function_call", "name": "exec_command", "status": "completed",
         "arguments": '{"cmd": "ls -1", "workdir": "/synthetic", "tty": true,'
                      ' "yield_time_ms": 5000, "unknown_key": "dropped"}'})
    assert card["commands"] == [{
        "command": "ls -1", "workdir": "/synthetic",
        "metadata": {"tty": True, "yield_time_ms": 5000}}]
    assert card["status"] == "completed"


def test_s3_a_program_whose_arguments_the_literal_parser_declines_is_an_other():
    """Spec section 3.3: an entry the parser located but could not read.

    The card degrades that ENTRY rather than failing, because the invocation was
    genuinely located; what it must never do is claim to know arguments it could
    not read.
    """
    source = ('doSomethingElse();\n'
              'const r = await tools.exec_command({cmd: process.env.SECRET});\n')
    card = kern.decode_tool_call_card(
        {"type": "custom_tool_call", "name": "exec", "status": "completed",
         "input": source})
    assert card["type"] == "program"
    assert card["invocations"] == [{"kind": "other", "name": "exec_command"}]


def test_s3_a_program_the_scanner_cannot_lex_produces_no_card():
    for source in ['evil(); const r = await tools.exec_command({cmd: "ls"}); /* open',
                   'evil(); const s = "unterminated']:
        assert kern.decode_tool_call_card(
            {"type": "custom_tool_call", "name": "exec", "status": "completed",
             "input": source}) is None, source


def test_s3_a_program_past_the_invocation_cap_is_truncated_not_dropped():
    source = "evil();\n" + "".join(
        f'await tools.wait({{cell_id: {i}}});\n' for i in range(12))
    card = kern.decode_tool_call_card(
        {"type": "custom_tool_call", "name": "exec", "status": "completed",
         "input": source})
    assert card["type"] == "program"
    assert len(card["invocations"]) == kern._CARD_MAX_COMMANDS
    assert card["truncated"] is True


def test_s3_extract_stores_no_card_for_the_new_families(tmp_path, monkeypatch):
    """Spec section 3.0, for the branch that reaches the most rows.

    34,935 uncarded calls gain a card at read time. Persisting them would grow
    `detail_json` on more rows than any other S3 change.
    """
    records = _codex_turn_records([
        {"call_id": "s3-stdin", "name": "write_stdin", "type": "function_call",
         "arguments": '{"session_id": 70001, "chars": "yes\\n"}',
         "status": "completed"},
        {"call_id": "s3-prog", "name": "exec", "type": "custom_tool_call",
         "status": "completed",
         "input": ('evil();\nconst r = await tools.exec_command({cmd: "ls"});\n')},
    ])
    stored, conn, ck = _s3_stored_detail(tmp_path, monkeypatch, records)
    try:
        detail = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    assert "card" not in json.loads(stored[("s3-stdin", "tool_call")])
    assert "card" not in json.loads(stored[("s3-prog", "tool_call")])
    # And the READ path does produce them, so the two facts are asserted about
    # the same rows rather than about a store that decoded nothing at all.
    served = {block["call_id"]: block for item in detail["items"]
              for block in item["blocks"] if block.get("call_id")}
    assert served["s3-stdin"]["detail"]["card"]["type"] == "session_ref"
    assert served["s3-prog"]["detail"]["card"]["type"] == "program"


# --- #463 S3 Task 5: the session index and the external-agent marker ---------


def _s3_tool_legibility_detail(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["tool-legibility"])
    conn = ns["open_cache_db"]()
    ns["sync_codex_cache"](conn)
    return conn, _single_ck(conn)


def test_s3_session_index_binds_openers_and_assigns_stable_ordinals(
    tmp_path, monkeypatch,
):
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _detail_of(conn, ck, tail=True, limit=500)
    finally:
        conn.close()
    idx = body["session_index"]
    assert idx["truncated"] is False
    # Three synthetic sessions: one announced by a separate `exec_command`, one
    # nothing announced at all, and one announced by its own `write_stdin` row.
    ordinals = sorted(s["ordinal"] for s in idx["sessions"].values())
    assert ordinals == [1, 2, 3]
    by_ordinal = {s["ordinal"]: s for s in idx["sessions"].values()}
    assert by_ordinal[2]["opener_block_key"] is None
    blocks = {b.get("block_key"): b for item in body["items"] for b in item["blocks"]}
    # The opener is the CALL that opened the session, not the output row that
    # announced it — the output folds into the call and has no block of its own.
    for ordinal, tool in ((1, "exec_command"), (3, "write_stdin")):
        opener_key = by_ordinal[ordinal]["opener_block_key"]
        assert opener_key.startswith("cbk1_")
        assert blocks[opener_key]["detail"]["name"] == tool
    # Ordinal 3's opener is its OWN row, which is what makes the "started this
    # session" state reachable on a `session_ref` card at all — the 80.2%
    # production case binds it to an `exec_command`, whose `terminal` card does
    # not render the note.
    assert blocks[by_ordinal[3]["opener_block_key"]]["call_id"] == "s3-fc-stdin-c"


# Every provider session id the tool-legibility rollout holds. Named once so a
# new session added to the fixture cannot quietly escape the leak assertions.
_S3_PROVIDER_SESSION_IDS = ("70001", "70002", "70003", "70004")


def test_s3_no_raw_session_id_reaches_any_card_or_the_index(tmp_path, monkeypatch):
    """Spec section 4.3 and 6.5.

    The reader sees a conversation-local ordinal, never the provider's own
    session id, because these are short integers that cannot be scrubbed without
    corrupting arbitrary text. So the token is REMOVED rather than scrubbed, and
    `build_anon_plan_for_sources` needs no new source.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _detail_of(conn, ck, tail=True, limit=500)
        stdin_block = next(b for item in body["items"] for b in item["blocks"]
                           if b.get("call_id") == "s3-fc-stdin-a")
        readback = q.read_codex_payload(
            conn, ck, stdin_block["block_key"], "call")
    finally:
        conn.close()
    cards = [(b.get("detail") or {}).get("card")
             for item in body["items"] for b in item["blocks"]]
    cards += [((b.get("output") or {}).get("detail") or {}).get("card")
              for item in body["items"] for b in item["blocks"]]
    for card in cards:
        if card is None:
            continue
        for token in _S3_PROVIDER_SESSION_IDS:
            assert token not in json.dumps(card), (token, card)
    for token in _S3_PROVIDER_SESSION_IDS:
        assert token not in json.dumps(body["session_index"]), token

    # The READBACK route serves the same card family and must publish the same
    # ordinal. Without this the field's meaning differed by route — the paged
    # detail carried the conversation-local ordinal while
    # `GET /api/conversation/<key>/payload` carried the provider's own id — and a
    # client validator written against the ordinal meaning would be wrong for one
    # of the two (spec sections 4.3 and 6.5).
    assert readback["status"] == "ok"
    assert readback["card"]["type"] == "session_ref"
    assert readback["card"]["ref"] == "1"
    assert "70001" not in json.dumps(readback["card"]), readback["card"]

    # The boundary, asserted rather than left implicit: the PRE-EXISTING generic
    # disclosure still shows the provider's own arguments verbatim, exactly as it
    # does for every uncarded call today. S3 does not change `detail.args`, and
    # it could not — `args` is stored at ingest, so rewriting it would break the
    # read-time-only rule of spec section 3.0. The same is true of the raw
    # payload the readback route exists to serve.
    stdin_call = next(b for item in body["items"] for b in item["blocks"]
                      if (b.get("detail") or {}).get("name") == "write_stdin")
    assert "70001" in stdin_call["detail"]["args"]
    assert "70001" in readback["content"]


def test_s3_session_ref_and_program_refs_carry_the_ordinal(tmp_path, monkeypatch):
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _detail_of(conn, ck, tail=True, limit=500)
    finally:
        conn.close()
    by_call = {b.get("call_id"): b for item in body["items"]
               for b in item["blocks"] if b.get("call_id")}
    assert by_call["s3-fc-stdin-a"]["detail"]["card"]["ref"] == "1"
    assert by_call["s3-fc-stdin-b"]["detail"]["card"]["ref"] == "2"
    # A cell reference is published as given and is never a shell session.
    wait_card = by_call["s3-fc-wait"]["detail"]["card"]
    assert wait_card["scope"] == "cell" and wait_card["ref"] == "12"
    # The program's own session invocation is rewritten the same way.
    program = by_call["s3-exec-program"]["detail"]["card"]
    session = next(i for i in program["invocations"] if i["kind"] == "session")
    assert session["ref"] == "1"


def test_s3_external_agent_marker_detected_at_read_time(tmp_path, monkeypatch):
    """The fixture row's stored detail has no `external_call`; detection is
    read-time, which is the only way it can reach the 3,789 historical markers."""
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        stored = [row[0] for row in conn.execute(
            "SELECT detail_json FROM codex_conversation_messages "
            "WHERE conversation_key = ? AND kind = 'assistant'", (ck,))]
        body = _detail_of(conn, ck, tail=True, limit=500)
    finally:
        conn.close()
    assert all("external_call" not in (row or "") for row in stored)
    blocks = [b for item in body["items"] for b in item["blocks"]]
    marker = [b for b in blocks if (b.get("detail") or {}).get("external_call")]
    assert len(marker) == 1, "exactly the one real marker, not the fenced lookalike"
    call = marker[0]["detail"]["external_call"]
    assert call["name"] == "ToolSearch"
    assert call["input"]["query"].startswith("select:")
    # NEVER `markers`: that key selects which rows the export path hydrates.
    assert "markers" not in marker[0]["detail"]
    # The span resolves against the SERVED text, which is what lets the client
    # hide the prose the card already renders instead of showing it twice.
    served_text = marker[0]["text"]
    start, end = call["span"]
    assert served_text[start:end].startswith("[external_agent_tool_call: ToolSearch]\n")
    assert served_text[start:end].rstrip("\n").endswith("}")
    assert kern.external_call_span_resolves(served_text, call) is True


def test_s3_external_agent_marker_negative_cases():
    for text in ["[external_agent_tool_call: X]\nno input line",
                 "[external_agent_tool_call: X]\ninput: {not json",
                 "```\n[external_agent_tool_call: X]\ninput: {}\n```",
                 "prefix [external_agent_tool_call: X]\ninput: {}",
                 "[external_agent_tool_call: " + "n" * 129 + "]\ninput: {}",
                 "[external_agent_tool_call: ]\ninput: {}",
                 "nothing here at all"]:
        assert kern._external_call_from_text(text) is None, text


def test_s3_external_agent_marker_positive_shape():
    card = kern._external_call_from_text(
        'Delegating.\n\n[external_agent_tool_call: ToolSearch]\n'
        'input: {"query": "select:Alpha", "max_results": 5}')
    assert card["schema_version"] == 1
    assert card["name"] == "ToolSearch"
    assert card["input"] == {"query": "select:Alpha", "max_results": 5}
    assert card["truncated"] is False


def test_s3_external_call_publishes_the_span_it_consumed():
    """Spec section 5.5 renders the marker as structure rather than prose.

    The full `[external_agent_tool_call: …]` / `input: …` run stays in the
    served `text`, because the export keeps it verbatim and the export bytes are
    frozen. Without the span the client cannot locate it, so the viewer would
    show the marker twice — once as a card and once as the raw prose behind it.
    """
    text = ('Delegating.\n\n[external_agent_tool_call: ToolSearch]\n'
            'input: {"query": "select:Alpha"}\nand then some prose.\n')
    card = kern._external_call_from_text(text)
    start, end = card["span"]
    assert text[start:end] == ('[external_agent_tool_call: ToolSearch]\n'
                               'input: {"query": "select:Alpha"}\n')
    # Removing the span leaves exactly the prose either side of it, with no
    # orphaned blank line where the marker was.
    assert text[:start] + text[end:] == "Delegating.\n\nand then some prose.\n"


def test_s3_external_call_span_ends_at_the_json_when_no_newline_follows():
    text = '[external_agent_tool_call: X]\ninput: {"a": 1}'
    card = kern._external_call_from_text(text)
    assert card["span"] == [0, len(text)]


def test_s3_external_call_fails_closed_when_clipping_truncates_the_marker():
    """Every strict prefix of a marker must produce no card at all.

    The row's `text` is capped at ingest, so a marker straddling the cap arrives
    clipped. A span published against a marker the served text does not wholly
    contain would not resolve, so the card is withheld instead.
    """
    full = ('[external_agent_tool_call: ToolSearch]\n'
            'input: {"query": "select:Alpha", "max_results": 5}')
    for cut in range(1, len(full)):
        assert kern._external_call_from_text(full[:cut]) is None, full[:cut]
    assert kern._external_call_from_text(full) is not None


def test_s3_external_call_span_guard_rejects_a_span_that_does_not_resolve():
    """The guard is what makes the fail-closed rule enforceable at the seam.

    The assembler publishes the card only when its span resolves inside the very
    string it is about to serve as `block["text"]`.
    """
    text = '[external_agent_tool_call: ToolSearch]\ninput: {}\n'
    card = kern._external_call_from_text(text)
    assert kern.external_call_span_resolves(text, card) is True
    assert kern.external_call_span_resolves("much shorter", card) is False
    assert kern.external_call_span_resolves(text, dict(card, span=[0, 0])) is False
    assert kern.external_call_span_resolves(text, dict(card, span=[5, 9])) is False
    assert kern.external_call_span_resolves(text, dict(card, span=[-1, 4])) is False
    assert kern.external_call_span_resolves(text, dict(card, span="0,4")) is False
    assert kern.external_call_span_resolves(text, {"name": "X"}) is False


# The read-time keys the SERVED envelope must carry. This is the positive
# control for the property below: it proves the store under test really does
# enrich at read time, so the property is asserted about a live read path rather
# than about a store that decodes nothing at all.
_S3_READ_TIME_KEYS_ON_THE_WIRE = (
    '"exit_code"', '"wall_time_seconds"', '"diff_source"', '"external_call"',
    '"invocations"', '"session_ref"', '"tool_search"', '"program"',
    '"complete"', '"session_index"', '"span"',
)


def _storage_card_for(record_json):
    """The card `_extract` persists for one physical record.

    ``record_json`` is the retained `codex_conversation_events.payload_json`,
    which holds the WHOLE canonical record — the same bytes
    `_reread_codex_full_content` validates against — so the record type and the
    inner payload are read from it exactly as the re-read path reads them.

    The SAME decoders `_extract` calls, with `for_storage=True` — which is what
    the one-off byte comparison actually measured. A key list cannot state that
    property: it has to anticipate every future read-time key, and it was already
    wrong about `unified_diff`, which the list branch genuinely stores
    (`bin/_lib_codex_conversation.py` sets it unconditionally; only `diff_source`
    is gated). The list only passed because the scenario's patch event happens to
    be dict-shaped.
    """
    record = json.loads(record_json)
    record_type = record.get("type") or record.get("record_type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    ptype = payload.get("type")
    if record_type == "response_item":
        if ptype in kern._RESPONSE_TOOL_CALLS:
            return (kern.decode_tool_call_card(payload, for_storage=True)
                    or kern.decode_secondary_tool_call_card(
                        payload, for_storage=True))
        if ptype in kern._RESPONSE_TOOL_OUTPUTS:
            shaped = kern.decode_tool_output_card(payload, for_storage=True)
            return shaped[0] if shaped is not None else None
        return None
    if record_type == "event_msg" and ptype in kern._EVENT_CARD_TYPES:
        return (kern.decode_patch_event_card(payload, for_storage=True)
                or kern.decode_secondary_event_card(payload))
    return None


def test_s3_no_read_time_enrichment_is_ever_persisted(tmp_path, monkeypatch):
    """Spec section 3.0, as a standing guard rather than a one-off measurement.

    The one-off measurement that established this ingested the whole 40-rollout
    parity corpus under the pre-S3 binary and under this one and compared every
    row's `content_len`, `detail_json` byte length and `source_bytes`: 240 rows,
    all identical. It caught a real defect — `diff_source` was reaching the
    stored list-branch card — which is why this cheaper standing form exists.

    The standing form asserts the PROPERTY rather than a list of key names: every
    stored card equals the card the same decoder produces from that row's own
    payload with `for_storage=True`. A read-time key that no list anticipated is
    caught by construction.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        rows = conn.execute(
            "SELECT m.kind, m.detail_json, e.record_type, e.payload_json "
            "FROM codex_conversation_messages AS m "
            "JOIN codex_conversation_events AS e "
            "  ON e.source_path = m.source_path "
            " AND e.line_offset = m.line_offset "
            "WHERE m.conversation_key = ? "
            "ORDER BY m.source_path, m.line_offset", (ck,)).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages "
            "WHERE conversation_key = ?", (ck,)).fetchone()[0]
        body = _detail_of(conn, ck, tail=True, limit=500)
    finally:
        conn.close()
    assert rows and len(rows) == total, "every row must carry its payload"
    carded = 0
    for kind, detail_json, record_type, payload_json in rows:
        stored = json.loads(detail_json) if detail_json else {}
        expected = _storage_card_for(payload_json)
        assert stored.get("card") == expected, (kind, record_type)
        carded += expected is not None
    assert carded >= 5, "non-vacuity: the fixture must carry stored cards"
    # And the SERVED envelope does carry the read-time keys, so the property
    # above is about a store whose read path really does enrich.
    served = json.dumps(body)
    for key in _S3_READ_TIME_KEYS_ON_THE_WIRE:
        assert key in served, key


# ── #463 S4 Task 2 — the enumerated failed-call classification (spec §6.4) ────


def test_classify_tool_failure_enumerates_every_disjunct():
    """One explicitly enumerated server definition of a failed call.

    Spec §6.4 takes CORRECTNESS OVER BUG-COMPATIBILITY here. The client's
    `OUTCOME_STATUSES` excludes `'error'`, so a card carrying `status: "error"`
    on a call whose call-side card is not `terminal` collapses to `'unknown'`
    and is not flagged client-side, while the server's own
    `decode_tool_output_card` already sets `is_error` for
    `status in {"failed", "error"}`. Reproducing the client exactly would have
    frozen that gap, so this test pins the server's definition and Task 9 brings
    the client to it.
    """
    from _lib_codex_landmarks import classify_tool_failure

    assert classify_tool_failure({"terminal_output": {"is_error": True}}) is True
    assert classify_tool_failure({"patch": {"success": False}}) is True
    assert classify_tool_failure({"patch": {"status": "failed"}}) is True
    assert classify_tool_failure({"web": {"completion": {"status": "error"}}}) is True
    assert classify_tool_failure({"mcp": {"completion": {"status": "error"}}}) is True
    # status "error" IS a failure — §6.4 takes correctness over client bug-compat
    assert classify_tool_failure({"terminal_output": {"status": "error"}}) is True
    # running and unknown are NOT failures
    assert classify_tool_failure({"terminal_output": {"status": "running"}}) is False
    assert classify_tool_failure({"terminal_output": {"status": "unknown"}}) is False


def _outline_derivation(conn, ck):
    """The wide read the outline performs, and the scoped pass over its positions."""
    rows = q._load_conversation_rows(conn, ck)
    return rows, q._derive_outline_events(conn, ck, rows)


def _positions_of_kind(rows, kind):
    return {(row.source_path, row.line_offset) for row in rows if row.kind == kind}


class _EventRowCounter:
    """A connection facade counting the PAYLOAD rows a query really pulls back.

    Only the fetched-row count can observe the scope of the payload pass; see
    ``test_derive_outline_events_reads_only_the_scoped_positions``. Returning a
    materialized list rather than the cursor is safe because every caller here
    iterates the result exactly once.

    Gated on ``payload_json`` rather than on the table name, because the S4
    derivation cache's watermark read also names that table and deliberately
    fetches NO payload: it is one covering-index aggregate. Counting its single
    row would make the scope assertion off by one and would then be "fixed" by
    loosening the assertion, which is the observation this class exists to keep
    exact.
    """

    def __init__(self, conn):
        self._conn = conn
        self.event_rows = 0

    def execute(self, sql, parameters=()):
        cursor = self._conn.execute(sql, parameters)
        if "codex_conversation_events" not in sql or "payload_json" not in sql:
            return cursor
        rows = list(cursor)
        self.event_rows += len(rows)
        return rows


def test_derive_outline_events_finds_failures_the_stored_card_cannot(
    tmp_path, monkeypatch,
):
    """Task 1 measured this against the real store and it is pinned here.

    Over 63,150 production `tool_output` rows the STORED card's `is_error` is
    True for 0 of them while the read-time decode finds 896, because the
    provider sets `status` on an output record 80 times in 63,150 rows and never
    to a failure value — the outcome is stated only in the harness preamble that
    the read-time five-grammar parse consumes. So this is not a preference for
    the more expensive read; the cheap one answers "did it fail" with silence.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        rows, derivation = _outline_derivation(conn, ck)
        stored = {
            (row.source_path, row.line_offset):
                (q._parse_detail(row.detail_json) or {}).get("card") or {}
            for row in rows if row.kind == "tool_output"
        }
    finally:
        conn.close()

    failing = derivation.failing_positions()
    assert failing, "non-vacuity: the fixture must carry a failing output"
    # Every failure the pass found is invisible in the stored projection.
    for position in failing:
        if position in stored:
            assert stored[position].get("is_error") is False, position
            assert stored[position].get("status") != "failed", position
    # `running` is not a failure, and the fixture carries two of them.
    outputs = _positions_of_kind(rows, "tool_output")
    verdicts = {p: v for p, v in derivation.errors_by_position.items() if p in outputs}
    assert len(verdicts) == len(outputs), "every output row must get a verdict"
    assert sum(verdicts.values()) == 3, (
        "the fixture's failing outputs are 'Script failed' on the mixed exec, "
        "'Process exited with code 3' on a write_stdin, and the second "
        "'Script failed' on the AMBIGUOUS-id turn (#463 S4 round 3)")


def test_derive_outline_events_reads_only_the_scoped_positions(tmp_path, monkeypatch):
    """The pass must not decode an event row no landmark can come from.

    Asserted on the rows the pass really FETCHES, which is the only observation
    that can see the regression. What the pass RECORDS cannot: it records a
    verdict only for a position it was already asked about, so widening the
    SELECT to every event row of the conversation — the exact `positions=None`
    regression this test exists to prevent — leaves the recorded set unchanged
    and any assertion over it green while the request decodes 92 MB of JSON.
    Task 1 measured that figure on the heaviest production conversation, and
    this route is refetched on every live-tail growth push.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        rows = q._load_conversation_rows(conn, ck)
        counter = _EventRowCounter(conn)
        derivation = q._derive_outline_events(counter, ck, rows)
        every_event = {
            (sp, lo) for sp, lo in conn.execute(
                "SELECT source_path, line_offset FROM codex_conversation_events "
                "WHERE conversation_key = ?", (ck,))}
    finally:
        conn.close()
    touched = (set(derivation.errors_by_position)
               | set(derivation.patch_files_by_position)
               | set(derivation.headings_by_position))
    in_scope = (_positions_of_kind(rows, "tool_output")
                | _positions_of_kind(rows, "reasoning")
                | {(row.source_path, row.line_offset) for row in rows
                   if row.kind == "event"
                   and row.event_type in q._S4_OUTCOME_EVENTS})
    assert touched <= in_scope, sorted(touched - in_scope)
    assert counter.event_rows == len(in_scope & every_event), (
        f"the pass fetched {counter.event_rows} event rows for a scope of "
        f"{len(in_scope & every_event)}")
    # Non-vacuity: the conversation must hold event rows OUTSIDE the scope, or
    # an unscoped read would fetch the same count as a scoped one.
    assert len(every_event) > len(in_scope & every_event), (
        "fixture carries no out-of-scope event row")


def test_derive_outline_events_counts_diff_lines_from_the_unbounded_changes(
    tmp_path, monkeypatch,
):
    """Spec §4.5 — counts come from the raw entry, never from a served card.

    `decode_patch_event_card` shares one 16,000-character budget across stdout,
    stderr and every file, so the clipped fixture file's card diff carries 43
    added lines while its raw `content` carries 55. Counting off the card would
    understate the change and would report nothing at all for a file whose diff
    was wholly clipped.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        _rows, derivation = _outline_derivation(conn, ck)
        payloads = q._load_row_payloads(conn, ck, set(derivation.patch_files_by_position))
    finally:
        conn.close()
    touches = [t for entry in derivation.patch_files_by_position.values()
               for t in entry]
    clipped = next(t for t in touches if t["path"].endswith("a-generated-clipped.py"))
    assert clipped == {"path": clipped["path"], "op": "add",
                       "added": 55, "removed": 0}
    # And the card a reader is served really is clipped, so the assertion above
    # is not merely restating what the card would have said.
    payload = next(iter(payloads.values()))[1]
    card = kern.decode_patch_event_card(payload, for_storage=False)
    card_file = next(f for f in card["files"]
                     if f["path"].endswith("a-generated-clipped.py"))
    assert card_file["truncated"] is True
    card_added = sum(1 for line in card_file["unified_diff"].split("\n")
                     if line.startswith("+") and not line.startswith("+++"))
    assert card_added == 43 < clipped["added"]


# A hunk whose content lines are indistinguishable from file headers by prefix.
# `-- legacy comment` is a SQL comment, and removing it renders `--- legacy
# comment`; removing the bare rule `--` renders `---`; adding `++i;` renders
# `+++i;`. A `startswith("---")`/`startswith("+++")` header filter drops three
# of these four real content lines.
_HEADER_LOOKALIKE_DIFF = (
    "--- a/schema.sql\n"
    "+++ b/schema.sql\n"
    "@@ -1,3 +1,3 @@\n"
    "--- legacy comment\n"
    "---\n"
    " shared context\n"
    "+++i;\n"
    "+ok\n"
)


def test_patch_touch_counts_do_not_swallow_content_that_looks_like_a_header():
    """Spec §4.5's undercount, arriving by a second route.

    Counting inside `@@` hunks is immune to this: a `---`/`+++` file header can
    only appear BEFORE a hunk opens, and the hunk header states exactly how many
    old-side and new-side lines follow, so nothing inside one has to be
    recognised by its prefix alone.
    """
    payload = {"type": "patch_apply_end", "changes": {
        "/synthetic/root-a/project-red/schema.sql": {
            "type": "update", "unified_diff": _HEADER_LOOKALIKE_DIFF}}}
    assert landmarks.patch_file_touches(payload) == [{
        "path": "/synthetic/root-a/project-red/schema.sql",
        "op": "update", "added": 2, "removed": 2,
    }]


def test_patch_touch_counts_decline_rather_than_guess_without_a_hunk():
    """An undetermined count is None, never 0, because 0 is a claim (§4.5).

    A change entry with no diff and no content — a move-only update — cannot be
    counted at all, and a `unified_diff` carrying no hunk header is not a unified
    diff, so counting its `+`/`-` prefixed lines would be the same guess the
    header filter above made.
    """
    payload = {"type": "patch_apply_end", "changes": {
        "/synthetic/root-a/project-red/moved.py": {
            "type": "update", "move_path": "/synthetic/root-a/project-red/new.py"},
        "/synthetic/root-a/project-red/hunkless.py": {
            "type": "update", "unified_diff": "+one\n-two\n"},
    }}
    touches = {t["path"].rsplit("/", 1)[-1]: t
               for t in landmarks.patch_file_touches(payload)}
    assert touches["moved.py"]["added"] is None
    assert touches["moved.py"]["removed"] is None
    assert touches["hunkless.py"]["added"] is None
    assert touches["hunkless.py"]["removed"] is None


def test_derive_outline_events_publishes_the_reader_routes_headings(
    tmp_path, monkeypatch,
):
    """One decomposition rule, not two (spec §4.6).

    The outline's heading texts must be exactly the texts the detail route
    publishes for the same block, or the outline row and the reader heading would
    be different sets and a jump could land on a heading the outline never
    offered.
    """
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        _rows, derivation = _outline_derivation(conn, ck)
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    served = [h["text"] for h in
              _first_reasoning_block(page)["detail"]["reasoning"]["headings"]]
    assert served, "non-vacuity: the fixture must publish headings"
    assert list(derivation.headings_by_position.values())[0] == served


def test_fold_owner_attributes_a_failing_output_to_its_tool_call(
    tmp_path, monkeypatch,
):
    """§4.1's attribution plumbing, end to end on a real store.

    `_fold_groups_for_item` computes the membership payload-free and
    `_build_segment_index` discarded it, so nothing could say WHICH call a
    failing output belongs to. A `tool_error` landmark anchors on the call, so
    for an output whose `call_id` has exactly ONE owning `tool_call` in the turn
    the answer has to be that call and not the output itself.

    #463 S4 remediation round 3 — and the OTHER branch, which this test asserted
    away until the corpus grew a turn that reaches it. An output whose `call_id`
    is owned by two or more calls belongs to no one of them, so it opens no
    group, becomes its own head, and `_outline_failing_calls` charges the failure
    to the output's own position. That is not a degradation: it is what gives the
    unfolded output its own `tool_error` landmark, and `_landmark_label` names
    that branch as reachable. Both shapes are required below, so neither can be
    lost to a later fixture change without failing here.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        rows, derivation = _outline_derivation(conn, ck)
        detail_bytes = q._detail_bytes_of(rows)
        kept, _suppressed = kern.pair_mirrors(rows)
        items = kern.canonical_items(kept)
        groups = []
        for entry in q._build_segment_index(
                ck, items, detail_bytes, segmented=True, fold_groups=True):
            groups.extend(entry["_fold_groups"])
    finally:
        conn.close()
    owners = landmarks.fold_owner_by_position(groups)
    kind_at = {(row.source_path, row.line_offset): row.kind for row in rows}
    # Round 4 — scope the oracle the way production scopes it.
    # `_build_segment_index` computes `_turn_scoped_call_owner_count` over ONE
    # item's rows; computing it here over the whole conversation is a different
    # function whenever a call id repeats across turns, and a fixture that grew
    # such a turn would make this test disagree with the implementation and fail
    # for a reason that has nothing to do with fold ownership.
    owner_count_at: dict[tuple[str, int], int] = {}
    for item in items:
        counts = q._turn_scoped_call_owner_count(item["rows"])
        for row in item["rows"]:
            owner_count_at[(row.source_path, row.line_offset)] = counts.get(
                row.call_id or "", 0)
    failing = derivation.failing_positions()
    assert failing, "non-vacuity"
    uniquely_owned = 0
    ambiguous = 0
    for position in failing:
        owner = owners.get(position)
        assert owner is not None, ("every failing position must have a fold "
                                   "group head", position)
        assert position in owner_count_at, (
            "a failing position must belong to a canonical item", position)
        if owner_count_at[position] == 1:
            assert kind_at[owner] == "tool_call", (position, owner, kind_at[owner])
            assert owner != position, "a failure must anchor on its call, not itself"
            uniquely_owned += 1
        else:
            assert owner == position, (
                "an output no single call owns is its own group head", position)
            assert kind_at[owner] == "tool_output", (position, kind_at[owner])
            ambiguous += 1
    assert uniquely_owned, "the fixture must carry a uniquely-owned failure"
    assert ambiguous, "the fixture must carry an ambiguous-id failure"
    assert uniquely_owned + ambiguous == len(failing)


def test_outline_reads_under_one_snapshot(tmp_path, monkeypatch):
    """§4.1 — the route's several reads share one snapshot, and release it."""
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    seen: list[bool] = []
    real = q._load_conversation_rows

    def spy(conn_, key):
        seen.append(conn_.in_transaction)
        return real(conn_, key)

    try:
        monkeypatch.setattr(q, "_load_conversation_rows", spy)
        body = q.get_codex_conversation_outline(conn, ck, effective_speed="standard")
        assert body["status"] == "ok"
        assert seen == [True], "the wide read must run inside the snapshot"
        assert conn.in_transaction is False, "the route must release the snapshot"
        # Nesting through the guard itself is fine: the outer scope is a read
        # snapshot this module opened, so the inner one reuses it rather than
        # issuing a second BEGIN, which SQLite would refuse.
        with q._read_snapshot(conn):
            again = q.get_codex_conversation_outline(
                conn, ck, effective_speed="standard")
            assert again == body
            assert conn.in_transaction is True
        assert conn.in_transaction is False, "the outer snapshot is released too"
    finally:
        conn.close()


def test_outline_refuses_a_transaction_it_did_not_open(tmp_path, monkeypatch):
    """A foreign transaction is not a snapshot this route may borrow (§4.1).

    ``conn.in_transaction`` is true for an outer WRITE transaction exactly as it
    is for an outer read snapshot, and Python's ``sqlite3`` exposes no
    ``txn_state``, so the two cannot be told apart after the fact. Only one of
    them is safe to inherit: inside a write, the outline would read that
    writer's uncommitted and possibly half-applied state — a message row whose
    events are already deleted — and would report an absence that no committed
    state ever held. So the route refuses rather than guessing, and a caller
    that wants several envelopes under one snapshot opens it through
    ``_read_snapshot``, which nests.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="read snapshot"):
            q.get_codex_conversation_outline(conn, ck, effective_speed="standard")
        conn.rollback()
    finally:
        conn.close()


def test_every_outline_caller_arrives_outside_a_transaction(tmp_path, monkeypatch):
    """The caller sweep, recorded as a test rather than as a claim (§4.1).

    ``get_codex_conversation_outline`` has three call paths:
    ``_lib_conversation_dispatch.neutral_outline`` (which the dashboard's
    ``/api/conversation/<v1.…>/outline`` route reaches through
    ``_run_conversation_query_impl``, on a connection ``open_conversations_db``
    returns fresh per request), ``bin/build-codex-reader-fixtures.py``, and the
    tests. None of them holds a transaction at the call, because the opener
    commits every write it makes — the schema apply, the legacy import and
    ``_ensure_codex_conversation_contract`` each end in ``commit`` or
    ``rollback`` — and a route handler opens, queries and closes. That is what
    this asserts, on a connection from the real opener after a real sync, so the
    day one of those commits is removed the refusal above surfaces here rather
    than in production.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        assert conn.in_transaction is False, (
            "open_conversations_db returned a connection inside a transaction")
        body = disp.neutral_outline(conn, ck, effective_speed="standard")
        assert body["status"] == "ok"
        assert conn.in_transaction is False
    finally:
        conn.close()


# ── #463 S4 Task 3 — tier-1 enrichment and the stats block ───────────────────
#
# These live here rather than in tests/test_codex_conversation_api.py, which the
# plan names: that file drives real HTTP and owns route-level concerns, while
# every existing outline-ENVELOPE assertion in the estate is in this file, next
# to the staging helpers these need.


def _outline_of(conn, ck):
    return q.get_codex_conversation_outline(conn, ck, effective_speed="standard")


def test_outline_turn_tools_are_deduped_by_name_with_true_count(
    tmp_path, monkeypatch,
):
    """Spec §4.4 — dedupe by name, and republish what dedupe destroys.

    A Codex turn can carry 523 calls, so a per-call array on one outline turn is
    unreasonable. But two consumers read `tools.length` and one reads the first
    failing entry's name, and dedupe changes both — a tool that succeeds early
    and fails late moves ahead of an earlier failing call once errors are
    OR-aggregated by name. `tool_call_count` and `first_failure_name` carry those
    two facts alongside the deduplicated array.

    The fixture's failing calls are the second `exec` ("Script failed") and the
    second `write_stdin` ("Process exited with code 3"), so first-failure order
    and the OR-aggregation are both observable here.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    turn = next(t for t in body["turns"] if t["kinds"].get("tool_call"))
    assert turn["tools"] == [
        {"name": "exec", "is_error": True},
        {"name": "exec_command", "is_error": False},
        {"name": "write_stdin", "is_error": True},
        {"name": "wait", "is_error": False},
        {"name": "js", "is_error": False},
        {"name": "tool_search_call", "is_error": False},
        {"name": "fixture_get_issue", "is_error": True},
        {"name": "apply_patch", "is_error": False},
    ]
    assert turn["tool_call_count"] == 11
    assert turn["first_failure_name"] == "exec"
    # A turn with no calls publishes neither, rather than a zero and a null.
    prompt = next(t for t in body["turns"] if t["kinds"] == {"user": 1})
    assert "tools" not in prompt and "tool_call_count" not in prompt


def test_outline_stats_report_tools_models_and_errors(tmp_path, monkeypatch):
    """Spec §3.5 / §4.2 — the stats card's four unreported figures.

    `models` is a histogram over canonical tier-1 ASSISTANT turns, not the
    distinct sorted list `_rollup_fields` returns: the client renders `model ×N`
    and a list is not that shape. `error_count` counts failing CALLS, which is
    why the two failing terminal outputs and the MCP protocol error collapse
    onto their calls; the fourth failure, on the AMBIGUOUS-id turn, collapses
    onto nothing and is counted at its own position.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    stats = body["stats"]
    assert stats["tool_counts"] == {
        "exec": 4, "exec_command": 1, "write_stdin": 3, "wait": 1, "js": 1,
        "tool_search_call": 1, "fixture_get_issue": 1, "apply_patch": 1,
        "update_plan": 1,
    }
    assert stats["models"] == {"gpt-synthetic-codex": 4}
    assert stats["error_count"] == 4
    assert stats["duration_seconds"] == 242
    # One entry per canonical tier-1 turn, and the model rides the ASSISTANT
    # ones — which is the counting unit the histogram above uses. The last turn
    # is a response carrying only a call and its output, and `_item_kind` files
    # a response as assistant, so it counts.
    assert [t.get("model") for t in body["turns"]] == [
        None, "gpt-synthetic-codex", None, None, None, "gpt-synthetic-codex",
        None, "gpt-synthetic-codex", None, "gpt-synthetic-codex"]


def _stamped_row(stamp):
    return kern.CodexNormalizedRow(
        conversation_key="conv", source_root_key="root", source_path="p",
        line_offset=0, timestamp_utc=stamp, turn_id=None, call_id=None,
        kind="assistant", event_type=None, record_family="response_item",
        model=None, text="", content_digest="d", content_len=0,
        detail_json=None, search_tool="", search_thinking="")


def test_outline_duration_uses_min_max_not_first_last():
    """Spec §4.2 — min/max over row timestamps, never last minus first.

    Asserted on the helper rather than through the route, because the route
    cannot exhibit the failure and a test that cannot observe the field it names
    is not evidence. `_load_conversation_rows` reads
    `ORDER BY timestamp_utc, source_path, line_offset`, so the rows the outline
    hands this helper are already sorted and the two forms coincide THERE. The
    rule is about the helper's own contract: §3.4 records that item and segment
    emission is physical order rather than timestamp order, and Task 1 found
    five decreases across turns in the corpus, so a caller that later passes an
    item-anchor list — the obvious next caller — must not get a negative
    duration out of it.
    """
    rows = [_stamped_row(s) for s in (
        "2026-07-14T12:00:02Z", "2026-07-14T12:10:00Z",
        "2026-07-14T12:05:00Z", "2026-07-14T12:05:30Z")]
    stamps = [r.timestamp_utc for r in rows]
    assert stamps[-1] < stamps[1], "non-vacuity: this list is not sorted"
    assert q._conversation_duration_seconds(rows) == 598
    assert q._conversation_duration_seconds([]) is None
    assert q._conversation_duration_seconds([_stamped_row(None)]) is None


def test_outline_error_count_is_zero_when_nothing_ran(tmp_path, monkeypatch):
    """A determinable zero, which is a different claim from null (D3)."""
    ns, _root, _rollouts = _stage_codex_provider(
        tmp_path, monkeypatch, ["title-wrapper-window"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    assert not any(t["kinds"].get("tool_output") for t in body["turns"]), (
        "non-vacuity: this fixture must carry no outcome-bearing row")
    assert body["stats"]["error_count"] == 0
    assert body["stats"]["tool_counts"] == {}


def test_outline_error_count_is_null_when_undeterminable(tmp_path, monkeypatch):
    """Spec D3 — nullable, because 0 is a claim and silence is not evidence.

    A conversation whose retained event payloads are gone still has
    outcome-bearing rows in the message table, and the stored card answers
    nothing: Task 1 measured `is_error` true for 0 of 63,150 production
    `tool_output` rows. Reporting 0 there would assert an absence nobody proved,
    which is the literal defect F13 names.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        intact = _outline_of(conn, ck)
        assert intact["stats"]["error_count"] == 4, "non-vacuity"
        conn.execute(
            "DELETE FROM codex_conversation_events WHERE conversation_key = ?", (ck,))
        conn.commit()
        stripped = _outline_of(conn, ck)
    finally:
        conn.close()
    assert stripped["stats"]["error_count"] is None
    # The outcome-bearing rows are still there; only the evidence is gone. That
    # is what makes null the honest answer rather than zero.
    assert any(t["kinds"].get("tool_output") for t in stripped["turns"])


# ── #463 S4 Task 4 — landmarks[] ─────────────────────────────────────────────


def _landmarks_of(body, kind=None):
    return [lm for lm in body["landmarks"]
            if kind is None or lm["kind"] == kind]


def test_landmark_key_is_unique_across_a_multi_heading_reasoning_block(
    tmp_path, monkeypatch,
):
    """§3.2 — the identity is the compound the server already mints.

    One reasoning block yields several headings, so `block_key` alone is not
    unique per heading and using it as the identity — as the first draft did —
    would give several landmarks the same key. The compound
    `<block_key>#<ordinal>` is the key the reader route already publishes for a
    heading, so a jump target and a landmark name the same thing.
    """
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    reasoning = _landmarks_of(body, "reasoning")
    assert len(reasoning) > 1, "non-vacuity: one block, several headings"
    keys = [lm["landmark_key"] for lm in reasoning]
    assert len(keys) == len(set(keys))
    assert all("#" in key for key in keys)
    assert {lm["block_key"] for lm in reasoning} == {reasoning[0]["block_key"]}, (
        "non-vacuity: block_key alone would have collided here")
    # The same identity the reader publishes for the same heading.
    served = _first_reasoning_block(page)["detail"]["reasoning"]["headings"]
    assert keys == [h["key"] for h in served]
    assert [lm["label"] for lm in reasoning] == [h["text"] for h in served]


def test_landmark_item_key_is_the_containing_segment(tmp_path, monkeypatch):
    """§3.2 — `item_key` is what a jump LOADS, `parent_item_key` who owns it.

    A jump to a turn key lands on segment 0, which S1 defined as the start of a
    turn whose failure may be fifteen segments later. The landmark carries the
    segment instead, and the owning turn separately, so the client can indent the
    row under its turn and still load the right page.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    assert body["landmarks"], "non-vacuity"
    segments = {key for turn in body["turns"] for key in turn["segment_item_keys"]}
    turn_keys = {turn["item_key"] for turn in body["turns"]}
    by_turn = {turn["item_key"]: turn for turn in body["turns"]}
    for landmark in body["landmarks"]:
        assert landmark["item_key"] in segments, landmark
        assert landmark["parent_item_key"] in turn_keys, landmark
        assert landmark["item_key"] in by_turn[
            landmark["parent_item_key"]]["segment_item_keys"]


def test_landmark_tool_error_anchors_on_the_failing_call(tmp_path, monkeypatch):
    """One `tool_error` per failing call, on the call rather than on the turn.

    The fixture's first three failures are the second `exec`, the second
    `write_stdin`, and the MCP protocol error; all fold into their call, so the
    landmark's label is the tool the reader is being sent to.

    The fourth is the AMBIGUOUS-id turn (#463 S4 round 3), and it is labelled
    `tool_output`. That is `_landmark_label`'s KIND fallback, taken because the
    row is a `tool_output` rather than a named `tool_call` or a typed `event`,
    and it is deliberate: §3.6 enumerates exactly two label sources, and the
    row's own text is the harness preamble that carries the provider session id,
    so the label must never come from there. Nothing covered this branch before
    the corpus grew the turn that reaches it.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    errors = _landmarks_of(body, "tool_error")
    assert [lm["label"] for lm in errors] == [
        "exec", "write_stdin", "fixture_get_issue", "tool_output",
    ]
    assert body["stats"]["error_count"] == len(errors)
    # Emission is physical order, and nothing sorts by timestamp (§3.4).
    assert [lm["timestamp_utc"] for lm in errors] == sorted(
        lm["timestamp_utc"] for lm in errors)
    keys = [lm["landmark_key"] for lm in body["landmarks"]]
    assert len(keys) == len(set(keys)), "landmark_key is unique across kinds too"


def test_update_plan_opens_the_plan_landmark_family(tmp_path, monkeypatch):
    """§3.2 — the `plan` kind is not free, and this is the mapping that opens it.

    Codex's decoded plan card is named `update_plan`, and both existing CLIENT
    plan predicates recognise only Claude's `ExitPlanMode` and
    `AskUserQuestion`. Publishing raw Codex tool names into tier-1 `tools`
    therefore would not have made the plan jump work — it would have been a
    silent no-op, and nothing would have failed if it never worked. The rollout
    fixture gained an `update_plan` turn for exactly this reason: the tool
    appeared zero times in every rollout in the corpus, so the mapping had no
    fixture anywhere and would have shipped invisible to every golden.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    plans = _landmarks_of(body, "plan")
    assert [lm["label"] for lm in plans] == ["update_plan"]
    assert body["stats"]["tool_counts"]["update_plan"] == 1
    segments = {key for turn in body["turns"] for key in turn["segment_item_keys"]}
    assert plans[0]["item_key"] in segments


def test_external_call_block_produces_no_landmark(tmp_path, monkeypatch):
    """§3.2's exclusion, and what actually guarantees it.

    S3's wire contract §7 states these blocks must not enter chips, filters, the
    Files tab or the outline, and F9 measured the marker in 7,578 rows across 46
    conversations. The property holds by construction rather than by a filter:
    `detail.external_call` is published on `assistant` blocks only, and no
    landmark kind comes from an assistant row. This pins it, so a later kind
    derived from prose cannot quietly break it — which is the only way it could
    break.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
        page = _detail_of(conn, ck, tail=True, limit=500)
    finally:
        conn.close()
    external = {block["block_key"] for item in page["items"]
                for block in item["blocks"]
                if (block.get("detail") or {}).get("external_call")}
    assert external, "non-vacuity: the fixture must carry the marker"
    assert body["landmarks"], "non-vacuity: it must carry landmarks elsewhere"
    assert not external & {lm["block_key"] for lm in body["landmarks"]}


# ── #463 S4 Task 6 — title cleaning ──────────────────────────────────────────


def test_known_harness_grammar_is_cleaned():
    """The three grammars the census of the real store actually found (§5.4).

    Counts over 438 stored Codex rollup titles on 2026-08-04: the skill link
    165, the command wrapper 41, `<recommended_plugins>` 6. Within the command
    wrapper the tags are dispositioned separately from what their content looks
    like — the slash command is stripped, the message and the args are the human
    text and are kept.
    """
    from _lib_codex_title_clean import clean_codex_title

    assert clean_codex_title(
        "<command-name>/clear</command-name> "
        "<command-message>x</command-message>") == "x"
    assert clean_codex_title(
        "<command-name>/clear</command-name> <command-message>clear"
        "</command-message> <command-args></command-args>") == "clear"
    assert clean_codex_title(
        "<command-name>/model</command-name> <command-message>model"
        "</command-message> <command-args>fable</command-args>") == "model fable"
    assert clean_codex_title(
        "[$cctally-session-kickoff](/Volumes/x/.agents/skills/k/SKILL.md) 294 S4"
    ) == "$cctally-session-kickoff 294 S4"
    # Never closes in the data: titles are capped at 120 characters, so the
    # stored value is the head of a plugin catalogue and nothing survives.
    assert clean_codex_title(
        "<recommended_plugins> Here is a list of plugins that are available") == ""


def test_unknown_markup_passes_through_untouched():
    """NEW coverage, and the spec says so rather than claiming a guard it lacks.

    The existing `<future_harness>` test asserts `unknown.kind == "user"`. It
    pins that the wrapper is not misclassified as a META row, says nothing about
    titles, and a loose stripper would pass it. The closed allowlist stands on
    its own merits — a general tag stripper would eat user-authored angle
    brackets — and this is the case that pins it.
    """
    from _lib_codex_title_clean import clean_codex_title

    for untouched in (
        "<future_harness>real title</future_harness>",
        "Why does <T> not compile?",
        "[a link](https://example.test/page) and prose",
        "  leading and  internal   spacing kept  ",
        "",
    ):
        assert clean_codex_title(untouched) == untouched
    # Idempotent, which matters because the client applies its own skill-link
    # cleaner to the same string.
    once = clean_codex_title(
        "[$commit-cctally](/Volumes/x/skills/c/SKILL.md) ship it")
    assert clean_codex_title(once) == once == "$commit-cctally ship it"


def test_display_chain_cleans_and_falls_through_when_nothing_survives():
    """§5.3 — a construct that strips to empty falls through on its own."""
    assert q._display_chain({
        "title": "<command-name>/clear</command-name> "
                 "<command-message>clear</command-message>",
        "project_label": "proj", "native_thread_id": "abcdef0123"}) == "clear"
    assert q._display_chain({
        "title": "<recommended_plugins> a catalogue",
        "project_label": "proj", "native_thread_id": "abcdef0123"}) == "proj"
    assert q._display_chain({
        "title": "<recommended_plugins> a catalogue",
        "project_label": None, "native_thread_id": "abcdef0123"}) == "abcdef01"


def _wrapped_title_records(title):
    records = _codex_turn_records([
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": title}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "acknowledged"}]},
    ])
    return records


def test_title_cleaning_reaches_all_three_read_paths(tmp_path, monkeypatch):
    """§5.1 — `_display_chain` is NOT the universal chokepoint.

    Verified routing: the detail title, the browse rail, the parent/child
    summaries and the export go through it; the OUTLINE TURN LABEL is built
    independently from anchor-row text, and the `kind=title` SEARCH path reads
    raw rollup titles directly. Cleaning only `_display_chain` would leave two
    surfaces raw, one of them user-facing on the CLI through
    `cctally transcript search --source codex --kind title`.
    """
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _wrapped_title_records(
            "<command-name>/clear</command-name> "
            "<command-message>reset the context</command-message>"))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        stored = conn.execute(
            "SELECT title FROM codex_conversation_rollups WHERE conversation_key = ?",
            (ck,)).fetchone()[0]
        detail = q.get_codex_conversation(conn, ck, effective_speed="standard")
        outline = _outline_of(conn, ck)
        hits = q.search_codex_conversations(
            conn, "reset the context", kind="title", effective_speed="standard")
        export = q.get_codex_conversation_export(
            conn, ck, effective_speed="standard")
    finally:
        conn.close()
    assert stored.startswith("<command-name>"), (
        "non-vacuity: the STORED title must still carry the markup, since this "
        "is a read-time rule and no migration rewrites history")
    assert detail["title"] == "reset the context"
    assert outline["turns"][0]["label"] == "reset the context"
    assert [h["title"] for h in hits["hits"]] == ["reset the context"]
    assert [h["snippet"] for h in hits["hits"]] == ["reset the context"]
    # §5.2 — export renders its title through `_display_chain` and its bytes are
    # goldened, so the claim that "export should be unchanged" is false in
    # general. The COMMITTED export golden happens not to move, because its
    # fixture's title carries no grammar in the allowlist; that is a property of
    # that fixture and not of the change, so the behaviour is asserted here on a
    # conversation whose title does carry one.
    assert "reset the context" in export["markdown"]
    assert "<command-name>" not in export["markdown"].split("\n", 1)[0]


# ── #463 S4 Task 5 — file touches, derived read-time ─────────────────────────


def test_files_come_from_the_payload_pass_not_the_stored_table(
    tmp_path, monkeypatch,
):
    """§1.2 — the search projection is not the richer outline source.

    Issue #489 deliberately fills the stored table for ``kind=files`` search.
    Delete that derived projection after ingest and prove the outline still
    reconstructs paths, anchors, counts, and document order from retained event
    payloads rather than quietly depending on the repaired table.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        conn.execute(
            "DELETE FROM codex_conversation_file_touches "
            "WHERE conversation_key = ?", (ck,))
        conn.commit()
        body = _outline_of(conn, ck)
        stored = conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_file_touches "
            "WHERE conversation_key = ?", (ck,)).fetchone()[0]
    finally:
        conn.close()
    assert stored == 0
    assert [f["file_path"].rsplit("/", 1)[-1] for f in body["files"]] == [
        "a-generated-clipped.py", "added.py", "empty.py", "removed.py",
        "updated.py"]
    assert all(f["tool"] == "apply_patch" for f in body["files"])
    assert all(f["count"] == len(f["touches"]) for f in body["files"])


def test_touch_anchor_resolves_to_a_segment_in_the_same_envelope(
    tmp_path, monkeypatch,
):
    """§4.3 — a touch anchor is a real jump target, not a plausible string."""
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    segments = {key for turn in body["turns"] for key in turn["segment_item_keys"]}
    touches = [t for f in body["files"] for t in f["touches"]]
    assert touches, "non-vacuity"
    for touch in touches:
        assert touch["item_key"] in segments, touch
        assert touch["timestamp_utc"]


def test_file_diff_counts_come_from_the_unbounded_changes(tmp_path, monkeypatch):
    """§4.5 — counted before card allocation, never off a served `unified_diff`.

    `decode_patch_event_card` shares one 16,000-character budget across stdout,
    stderr and every file, so the clipped fixture file's served diff carries 43
    added lines against 55 in the raw `content`. `op` is the raw change KIND —
    `add`/`delete`/`update` in the dict shape — not the tool name.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    files = {f["file_path"].rsplit("/", 1)[-1]: f for f in body["files"]}
    assert files["a-generated-clipped.py"]["added"] == 55
    assert files["a-generated-clipped.py"]["removed"] == 0
    assert files["a-generated-clipped.py"]["touches"][0]["op"] == "add"
    assert files["removed.py"]["touches"][0]["op"] == "delete"
    assert (files["updated.py"]["added"], files["updated.py"]["removed"]) == (1, 1)
    assert files["updated.py"]["touches"][0]["op"] == "update"


def _two_patch_records():
    """Two patch events whose paths invert alphabetically.

    The historical table-backed outline ordered file touches by path, while
    `OutlineFile` promises first-touch DOCUMENT order. Only a corpus where the
    two disagree can tell them apart, and neither committed rollout is one: the
    dict-shaped fixture's `changes` object is emitted with sorted keys and the
    segmented fixture touches a single path.
    """
    def patch_event(ts, path, added):
        return {"timestamp": ts, "type": "event_msg", "payload": {
            "type": "patch_apply_end", "status": "completed", "success": True,
            "stdout": "", "stderr": "",
            "changes": {path: {"type": "add", "content": added}}}}
    records = _codex_turn_records([
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "apply two patches"}]},
    ])
    records.append(patch_event("2026-07-14T12:10:00Z", "/synth/z-first.py", "one\n"))
    records.append(patch_event("2026-07-14T12:11:00Z", "/synth/a-second.py",
                               "one\ntwo\n"))
    return records


def test_files_are_in_first_touch_physical_order(tmp_path, monkeypatch):
    """§3.5 — first-touch document order, which the alphabetical SQL replaced."""
    ns, _root, _rollout = _stage_codex_records(
        tmp_path, monkeypatch, _two_patch_records())
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    paths = [f["file_path"] for f in body["files"]]
    assert paths == ["/synth/z-first.py", "/synth/a-second.py"]
    assert paths != sorted(paths), "non-vacuity: alphabetical would invert this"
    assert [f["added"] for f in body["files"]] == [1, 2]


def test_file_counts_are_null_when_they_cannot_be_determined(
    tmp_path, monkeypatch,
):
    """§4.5 — an undetermined count is null, and null is not 0.

    A move-only `update` carries neither a diff nor content, so nothing can be
    counted from it. Rendering 0 would claim the file did not change.
    """
    records = _codex_turn_records([
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "move a file"}]},
    ])
    records.append({"timestamp": "2026-07-14T12:10:00Z", "type": "event_msg",
                    "payload": {"type": "patch_apply_end", "status": "completed",
                                "success": True, "stdout": "", "stderr": "",
                                "changes": {"/synth/moved.py": {
                                    "type": "update",
                                    "move_path": "/synth/new.py"}}}})
    ns, _root, _rollout = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    assert len(body["files"]) == 1
    assert body["files"][0]["added"] is None
    assert body["files"][0]["removed"] is None
    assert body["files"][0]["count"] == 1


def test_outline_thinking_carries_the_read_time_reasoning_headings(
    tmp_path, monkeypatch,
):
    """§3.1's `thinking`, from the same decomposition the landmarks use.

    The Claude outline publishes one label per thinking block; the Codex
    equivalent is the authored reasoning headings, which only the read-time
    payload pass can produce — `_reasoning_headings` returns None without
    `payload["summary"]`, so a stored-only derivation would degrade to one line
    per block (§4.1).
    """
    conn, ck = _heading_page(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
        page = _detail_of(conn, ck, limit=0)
    finally:
        conn.close()
    served = [h["text"] for h in
              _first_reasoning_block(page)["detail"]["reasoning"]["headings"]]
    assert served, "non-vacuity: the fixture must publish headings"
    thinking = [line for t in body["turns"] for line in t.get("thinking", ())]
    assert served == thinking[:len(served)]


# ── #463 S4 — the Tasks 3-6 review findings ──────────────────────────────────


def test_outline_label_falls_back_when_cleaning_empties_it():
    """A cleaned-to-empty label must not leave the outline row wordless.

    Two of the four allowlisted grammars carry the `strip` disposition and can
    consume the whole string, and §5.3 justifies that on the grounds that
    `_display_chain` is "already a fallback chain". The outline label path has
    no chain at all: it cleans the anchor row's first non-blank line and
    publishes the result, and the client's
    `cleanQualifiedTitle(turn.label) ?? turn.label` passes `''` straight
    through. The reader then gets an outline row with no text, which is worse
    than the raw catalogue head it replaced.
    """
    from _lib_codex_title_clean import clean_codex_title

    plugins = "<recommended_plugins> Here is a list"
    command = "<command-name>/x</command-name>"
    # Non-vacuity: these are the two inputs the kernel really does empty.
    assert clean_codex_title(plugins) == ""
    assert clean_codex_title(command) == ""
    assert q._clean_outline_label(plugins) == plugins
    assert q._clean_outline_label(command) == command
    # The cleaning that CAN produce text is untouched by the fallback.
    assert q._clean_outline_label(
        "<command-name>/model</command-name> <command-message>pick</command-message>"
    ) == "pick"
    assert q._clean_outline_label("ordinary prose title") == "ordinary prose title"


def _landmark_row(**overrides):
    fields = dict(
        conversation_key="conv", source_root_key="root", source_path="p",
        line_offset=0, timestamp_utc="2026-07-14T12:00:00Z", turn_id=None,
        call_id=None, kind="tool_output", event_type=None,
        record_family="response_item", model=None, text="", content_digest="d",
        content_len=0, detail_json=None, search_tool="", search_thinking="")
    fields.update(overrides)
    return kern.CodexNormalizedRow(**fields)


def test_landmark_label_never_falls_through_to_row_prose():
    """§3.6 enumerates exactly two label sources; the fallback must not add a third.

    A failing `tool_output` whose `call_id` is owned by two or more `tool_call`s
    in its turn is not folded, becomes its own group head, and enters
    `failed_calls` directly — so it reaches `_landmark_label` as a row that is
    neither a named `tool_call` nor a typed `event`. The prose fallback then
    published the first non-blank line of the RAW stored `text` column, which
    for a Codex tool output is the harness preamble that
    `decode_tool_output_card(for_storage=False)` exists to remove and which
    `test_s3_no_raw_session_id_reaches_any_served_route` documents as carrying
    the provider `session_id`.
    """
    preamble = ("Chunk ID: ee33ff\n"
                "Session 44444444-4444-4444-8444-444444444444\n"
                "Process exited with code 3\nOutput:\nrefused\n")
    row = _landmark_row(text=preamble)
    # Non-vacuity: the prose fallback's own source really does carry it.
    assert q._first_nonblank_line(q._strip_ansi(q._row_display(row))) == "Chunk ID: ee33ff"
    label = q._landmark_label(row)
    assert "44444444" not in label and "ee33ff" not in label
    assert label == "tool_output"
    # The two enumerated sources are unchanged.
    assert q._landmark_label(_landmark_row(
        kind="tool_call", detail_json=json.dumps({"name": "exec"}))) == "exec"
    assert q._landmark_label(_landmark_row(
        kind="event", event_type="patch_apply_end")) == "patch_apply_end"
    # A `tool_call` whose stored detail names nothing falls back to the row's
    # KIND, which is normalizer vocabulary rather than conversation content.
    assert q._landmark_label(_landmark_row(
        kind="tool_call", text=preamble)) == "tool_call"


def test_a_failing_plan_call_lands_in_both_the_error_and_plan_families(
    tmp_path, monkeypatch,
):
    """§3.2 — the `plan` kind gets one entry per plan call, failing or not.

    The branch tested `if position in failed_calls` before the plan branch, so a
    failed plan call was filed only as `tool_error` and the jump cluster's plan
    family reported ZERO — which, under this spec's own rule that 0 is a claim
    and hiding is not, asserts no plan activity in a conversation that has some.
    The constraint forcing that choice was self-imposed: the bare `block_key`
    was the identity for every non-reasoning kind, so one block could carry at
    most one landmark. The reasoning kind already proved the compound shape is
    acceptable.
    """
    records = _codex_turn_records([
        {"type": "message", "role": "user", "phase": "input",
         "content": [{"type": "input_text", "text": "record the plan"}]},
        {"type": "function_call", "name": "update_plan", "call_id": "plan-fail",
         "status": "completed",
         "arguments": json.dumps({"plan": [{"status": "pending", "step": "s"}]})},
        {"type": "function_call_output", "call_id": "plan-fail",
         "output": "Wall time: 0.1 seconds\nProcess exited with code 1\n"
                   "Output:\nrefused\n"},
    ])
    ns, _root, _rollout = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    # Non-vacuity: the call really did fail, which is what suppressed the plan.
    assert body["stats"]["error_count"] == 1
    errors = _landmarks_of(body, "tool_error")
    plans = _landmarks_of(body, "plan")
    assert [lm["label"] for lm in errors] == ["update_plan"]
    assert [lm["label"] for lm in plans] == ["update_plan"]
    assert errors[0]["block_key"] == plans[0]["block_key"]
    assert errors[0]["item_key"] == plans[0]["item_key"]
    keys = [lm["landmark_key"] for lm in body["landmarks"]]
    assert len(keys) == len(set(keys)), keys


def test_file_counts_decline_when_one_touch_cannot_be_counted(
    tmp_path, monkeypatch,
):
    """§4.5 — an understated aggregate is a wrong number, not a partial one.

    The per-file sum added only the touches it could count, so a file touched
    once with a countable diff and once by a move-only `update` published the
    first touch's figure for a file that changed more, with nothing in
    `touches[]` marking the total as partial. §4.5 requires the value to be null
    where a count cannot be determined, and that rule has to reach the aggregate
    and not only the individual touch.
    """
    records = _codex_turn_records([
        {"type": "message", "role": "user", "phase": "input",
         "content": [{"type": "input_text", "text": "edit then move"}]},
    ])
    records.append({"timestamp": "2026-07-14T12:10:00Z", "type": "event_msg",
                    "payload": {"type": "patch_apply_end", "status": "completed",
                                "success": True, "stdout": "", "stderr": "",
                                "changes": {"/synth/a.py": {
                                    "type": "update",
                                    "unified_diff": "@@ -1,1 +1,2 @@\n ctx\n+added\n"}}}})
    records.append({"timestamp": "2026-07-14T12:11:00Z", "type": "event_msg",
                    "payload": {"type": "patch_apply_end", "status": "completed",
                                "success": True, "stdout": "", "stderr": "",
                                "changes": {"/synth/a.py": {
                                    "type": "update",
                                    "move_path": "/synth/b.py"}}}})
    ns, _root, _rollout = _stage_codex_records(tmp_path, monkeypatch, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    assert len(body["files"]) == 1
    entry = body["files"][0]
    assert entry["count"] == 2
    # Non-vacuity, on the kernel that produced these two touches: exactly one of
    # them is countable, so the summing form published +1/-0 for a file whose
    # second change nobody could measure.
    counted, moved = (
        landmarks.patch_file_touches(rec["payload"])[0] for rec in records[-2:])
    assert (counted["added"], counted["removed"]) == (1, 0)
    assert (moved["added"], moved["removed"]) == (None, None)
    assert entry["added"] is None
    assert entry["removed"] is None


# ── #463 S4 Task 11 — the §4.7 perf gate's escalation ────────────────────────


def test_outline_payload_pass_is_watermark_cached_and_extends(tmp_path, monkeypatch):
    """D2's fallback, reached because Task 11's re-measurement breached §4.7.

    The event payload pass is 242 ms of the warm outline's 332 ms on the
    heaviest production conversation, and the route is refetched on every
    live-tail growth push — so it costs more than the detail page it opens
    beside (229 ms), which is the ceiling "the outline must not become the
    critical path on conversation open".

    The cache is keyed on a watermark of the conversation's event rows and
    EXTENDS rather than only hitting or missing: an append decodes the new
    positions and reuses the rest. That is sound because a derivation is keyed
    by physical position and a rollout line at a byte offset is immutable — the
    same offset never names different content.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    asked: list[int] = []
    real = q._iter_row_payloads

    def spy(conn_, key, positions=None):
        asked.append(len(positions) if positions is not None else -1)
        return real(conn_, key, positions)

    monkeypatch.setattr(q, "_iter_row_payloads", spy)
    try:
        q.reset_outline_derivation_cache()
        first = _outline_of(conn, ck)
        second = _outline_of(conn, ck)
        # A repeat with no growth reads no payload at all.
        assert asked[0] > 0, "non-vacuity: the first call must do the work"
        assert len(asked) == 1, asked
        # And answers identically, which is what makes the cache invisible.
        assert second == first

        rows = q._load_conversation_rows(conn, ck)
        assert len(rows) > 4, "non-vacuity: the fixture must be splittable"
        q.reset_outline_derivation_cache()
        asked.clear()
        q._derive_outline_events(conn, ck, rows[:len(rows) // 2])
        extended = q._derive_outline_events(conn, ck, rows)
        q.reset_outline_derivation_cache()
        fresh = q._derive_outline_events(conn, ck, rows)
        # The extension asked for strictly fewer payloads than the full pass ...
        assert asked[1] < asked[2], asked
        # ... and produced exactly the same three maps.
        assert extended.errors_by_position == fresh.errors_by_position
        assert extended.headings_by_position == fresh.headings_by_position
        assert extended.patch_files_by_position == fresh.patch_files_by_position

        # A DELETE lowers the watermark, so the prefix check fails and the pass
        # recomputes rather than serving a verdict for evidence that is gone.
        q.reset_outline_derivation_cache()
        asked.clear()
        _outline_of(conn, ck)
        conn.execute(
            "DELETE FROM codex_conversation_events WHERE conversation_key = ?", (ck,))
        conn.commit()
        after = _outline_of(conn, ck)
    finally:
        conn.close()
    assert len(asked) == 2, asked
    assert after["stats"]["error_count"] is None


# ── #463 S4 remediation ───────────────────────────────────────────────────────


def _model_row(kind, model, offset):
    return kern.CodexNormalizedRow(
        conversation_key="conv", source_root_key="root", source_path="p",
        line_offset=offset, timestamp_utc="2026-08-04T00:00:00Z", turn_id=None,
        call_id=None, kind=kind, event_type=None, record_family="response_item",
        model=model, text="", content_digest="d%d" % offset, content_len=0,
        detail_json=None, search_tool="", search_thinking="")


def test_item_model_reads_the_turn_not_only_its_anchor_row():
    """§4.2 — the unit is the canonical tier-1 assistant TURN.

    Measured against the production store on 2026-08-04: a conversation with 13
    outline turns carrying assistant rows and 82 assistant rows rendered
    `gpt-5.6-sol x2`, and 182 of 200 Codex conversations reported a model total
    under a third of their turn count. The cause is that most Codex response
    items anchor on a `reasoning` row, which carries no model, so reading the
    anchor row alone discards the model the turn plainly states.
    """
    anchor = _model_row("reasoning", None, 0)
    item = {"klass": "response", "anchor_row": anchor,
            "rows": [anchor, _model_row("assistant", "gpt-5.6-sol", 1)]}
    assert q._item_model(item) == "gpt-5.6-sol"
    # The anchor still wins when it has one, so no existing turn can move.
    anchored = _model_row("assistant", "gpt-anchor", 0)
    assert q._item_model({
        "klass": "response", "anchor_row": anchored,
        "rows": [anchored, _model_row("assistant", "gpt-later", 1)]}) == "gpt-anchor"
    # A turn that names no model anywhere still names none.
    silent = _model_row("reasoning", None, 0)
    assert q._item_model({"klass": "response", "anchor_row": silent,
                          "rows": [silent]}) is None


def test_outline_models_reconcile_against_the_turns_that_name_one(
    tmp_path, monkeypatch,
):
    """`stats.models` and `turns[].model` are one derivation, counted once each.

    The Claude histogram sums to exactly `stats.turns.assistant` because every
    Claude assistant turn states a model. The Codex total sums to the tier-1
    turns that NAME one, which is the same rule and a smaller number.

    The denominator is stated here because the first version of this docstring
    got it wrong. It is **canonical tier-1 items whose kind is `assistant`** —
    NOT every canonical item, and not model-bearing rows. Measured over the 200
    Codex conversations with the largest `SUM(content_len)` in the production
    store on 2026-08-04: those conversations hold 8,062 canonical items of every
    kind, of which **1,009 are assistant turns**; the anchor-row rule attributes
    675 of them and the turn rule 725, leaving **284 (28.1%) that name no model
    on ANY of their rows**. Over the whole store the residual is 712 of 1,786
    (39.9%). The superseded figures — 4,954 of 5,489, a 9.7% residual —
    reproduce under no unit measured over that corpus; the working is in
    docs/superpowers/plans/463-s4-measurements.md section 8.1.

    That residual is an ingest-time property — `codex_normalize_events` clears
    the sticky model on every `session_meta` and only a later `turn_context`
    restores it — and no read-time rule can recover a model the rows do not
    carry.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        body = _outline_of(conn, ck)
    finally:
        conn.close()
    named = [t["model"] for t in body["turns"] if t.get("model")]
    assert named, "non-vacuity: the fixture must publish at least one model"
    recount: dict[str, int] = {}
    for model in named:
        recount[model] = recount.get(model, 0) + 1
    assert body["stats"]["models"] == recount
    assert sum(body["stats"]["models"].values()) == len(named)


def test_failing_positions_are_confined_to_this_conversation_request(
    tmp_path, monkeypatch,
):
    """F-G — `errors_by_position` is CACHED, so it can outlive one request.

    Every other consumer of the derivation indexes it by a position the current
    request produced. `failed_calls` iterated the whole map instead, so a
    verdict retained from an earlier request could file a failure against a row
    this request never read. The intersection is defensive rather than a
    reproduction of an observed miscount, which is why the assertion is on the
    filter and not on a contrived cache state.
    """
    conn, ck = _s3_tool_legibility_detail(tmp_path, monkeypatch)
    try:
        rows = q._load_conversation_rows(conn, ck)
        derivation = q._derive_outline_events(conn, ck, rows)
        # A verdict for a position no row of this conversation occupies.
        derivation.errors_by_position[("/elsewhere.jsonl", 999999)] = True
        outcomes = q._outline_outcome_positions(rows)
        failing = q._outline_failing_calls(derivation, outcomes, {})
    finally:
        conn.close()
    assert ("/elsewhere.jsonl", 999999) not in failing
    assert failing, "non-vacuity: the fixture's real failures must survive"


def test_skill_link_is_cleaned_when_prompt_text_abuts_the_paren():
    """F-E — the lookahead required whitespace or end of string after `)`.

    Verified by execution against the served titles: the no-space form returned
    unchanged, so both the reader header and the outline rail rendered the raw
    Markdown link including an absolute filesystem path. Two of 300 served
    titles in the test store carry it.
    """
    from _lib_codex_title_clean import clean_codex_title

    abutting = ("[$cctally-codex-split](/Volumes/x/.agents/skills/s/SKILL.md)"
                "Task B of issue #450.")
    spaced = ("[$cctally-codex-split](/Volumes/x/.agents/skills/s/SKILL.md) "
              "Task B of issue #450.")
    assert clean_codex_title(abutting) == "$cctally-codex-split Task B of issue #450."
    assert clean_codex_title(spaced) == "$cctally-codex-split Task B of issue #450."
    # A path is never left in the output.
    assert "/Volumes/x" not in clean_codex_title(abutting)
    # Still closed: a link that is not a SKILL.md target passes through.
    other = "[a link](https://example.test/page)and prose"
    assert clean_codex_title(other) == other
