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


def test_rebuild_clears_all_three_normalized_tables(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] > 0
        # Point CODEX_HOME at an empty root and rebuild -> full clear.
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-root"))
        ns["sync_codex_cache"](conn, rebuild=True)
        for table in ("codex_conversation_messages", "codex_conversation_file_touches",
                      "codex_conversation_rollups"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        # FTS empty too.
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_fts "
            "WHERE codex_conversation_fts MATCH 'Synthetic'").fetchone()[0] == 0
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
        assert {"file_path": "synthetic.txt", "tool": "apply_patch",
                "count": 1} in o["files"]
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
        assert terminal["output"]["detail"]["card"] == {
            "schema_version": 1,
            "type": "terminal_output",
            "status": "completed",
            "is_error": False,
            "parts": [{"stream": "output", "text": "alpha\n", "type": "text"}],
            "truncated": False,
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
        assert patch_card["files"] == [{
            "path": "synthetic-added.txt", "status": "added"}]
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
        assert heredoc["files"] == [{
            "path": "synthetic-delete.txt", "status": "deleted"}]
        repeated = [by_call["repeat-patch-1"], by_call["repeat-patch-2"]]
        assert len({block["block_key"] for block in repeated}) == 2
        assert len({block["detail"]["card"]["completion"]["event_block_key"]
                    for block in repeated}) == 2
        assert all(block["detail"]["card"]["completion"]["files"][0]["path"]
                   == "synthetic-repeat.txt" for block in repeated)

        diff_less = next(block for block in blocks
                         if block["kind"] == "event" and block.get("call_id") == "diff-less")
        assert diff_less["detail"]["card"]["has_diff"] is False
        assert diff_less["detail"]["card"]["files"] == [{
            "path": "synthetic-summary.txt", "status": "modified"}]
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
        'evil(); ' + supported,
        supported + ' sendSecret();',
        'const r = await evil.tools.exec_command({cmd: "nope"}); text(r.output);',
        ('const r = await tools.exec_command({cmd: "ok", max_output_tokens: '
         + "9" * 5000 + '}); text(r.output);'),
    ]
    for raw in unsupported:
        bad = dict(payload, input=raw)
        assert kern.decode_tool_call_card(bad) is None, raw

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
        assert [block["detail"]["reasoning"] for block in reasoning] == [
            {"schema_version": 1, "source": "response_item",
             "title": "Inspecting synthetic state"},
            {"schema_version": 1, "source": "response_item",
             "summary": "Synthetic provider summary.",
             "body": "Detailed synthetic reasoning body."},
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
        # Force the live-recompute branch by deleting the stored rollups.
        conn.execute("DELETE FROM codex_conversation_rollups")
        live = q.list_codex_conversations(conn, effective_speed="standard")
        assert fast == live
    finally:
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


def test_neutral_outline_codex_matches_kernel(tmp_path, monkeypatch):
    ns, _root, _rollouts = _stage_codex_provider(tmp_path, monkeypatch, ["modern-full"])
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        ck = _single_ck(conn)
        assert disp.neutral_outline(conn, ck, effective_speed="standard") == \
            q.get_codex_conversation_outline(conn, ck, effective_speed="standard")
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
        # Native patch events are additively payload-capable. Ordinary prose
        # and every other non-tool block remain keyless.
        for it in d["items"]:
            for b in it["blocks"]:
                card = (b.get("detail") or {}).get("card")
                if b["kind"] == "event" and b.get("payload_which") == "event":
                    assert b["block_key"].startswith("cbk1_")
                    assert b["payload_which"] == "event"
                elif b["kind"] != "tool_call":
                    assert "block_key" not in b
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
        assert via == q.find_in_codex_conversation(conn, ck, "Synthetic", kind="all")
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
