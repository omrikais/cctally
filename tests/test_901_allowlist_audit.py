"""#901 Amendment 19 T1 (PR-1): the Q11 audit of every remaining G1 allowlist rule.

Spec ``docs/superpowers/specs/2026-10-03-901-dashboard-disk-writes.md`` I1 and
§6.2 G1: a temp structure on a recurring path is bounded only by an
independently demonstrated cardinality bound (a true top-N, LIMIT 1, or a bound
the schema enforces on what the structure holds), never by grouping scope. One
touched session's whole history, one rollout file, one attribution axis shard,
one subscription week, one trailing week, one batch and one conversation are not
bounds, and ``temp_store = MEMORY`` discharges nothing.

The audit kept the rules with a real bound (``tests/_sql_plan_guard.py`` states
each one) and rewrote every other statement so it builds no temp structure. Two
kinds of proof live here, for every rewrite:

* **G1, adversarial.** One arbitrarily large touched session, conversation,
  file, shard, week or batch, driven through the rewritten call site under BOTH
  the file and the in-memory temp store. With the rule gone from the allowlist,
  today's form fails here (RED); the rewrite passes.
* **G2-style equivalence.** The rewrite returns exactly what today's form
  returned — a frozen copy of the old statement and its Python — over data
  built to separate them: ties, NULL and empty values, missing keys, duplicate
  inputs, and the title choice and its ordering.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import pathlib
import sqlite3
import sys
import types

import pytest

import _sql_plan_guard as guard
from conftest import (
    load_script, redirect_paths, redirect_paths_without_conversation_retention,
)

BIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

TEMP_STORE_CODE = {"FILE": 1, "MEMORY": 2}
TEMP_STORES = sorted(TEMP_STORE_CODE)
UTC = dt.timezone.utc


def _bounded(recorder, call_site: str, temp_store: str) -> list:
    """Every statement whose stack reaches ``call_site``: it ran, it ran under
    the selected temp store, and no plan line builds an unexplained temp
    structure."""
    statements = [s for s in recorder.statements if call_site in s.call_sites]
    assert statements, f"non-vacuity: {call_site} executed"
    stores = {s.temp_store for s in statements}
    assert stores == {TEMP_STORE_CODE[temp_store]}, (call_site, stores)
    unexplained, _stale = guard.classify(
        statements, allowlist=guard.ALLOWLIST, pending=())
    assert unexplained == [], guard.format_findings(unexplained)
    return statements


def _pin_writer_temp_store(monkeypatch, temp_store: str) -> None:
    store = importlib.import_module("_cctally_store")
    monkeypatch.setattr(store, "WRITER_TEMP_STORE", temp_store)


def test_the_audit_removed_every_unbounded_rule_and_states_each_bound():
    """Non-vacuity of the audit itself: no removed rule is back, the
    "audit is outstanding" exemption is gone, and every surviving rule's reason
    names its bound class."""
    present = {rule.rule_id for rule in guard.ALLOWLIST}
    assert not present & guard.REMOVED_BY_AUDIT, present & guard.REMOVED_BY_AUDIT
    assert "audit is outstanding" not in pathlib.Path(guard.__file__).read_text()
    for rule in guard.ALLOWLIST:
        assert rule.reason.startswith((
            "top-1:", "top-N:", "schema bound:",
        )), (rule.rule_id, rule.reason)


# ── Claude: one long touched session (the three batch maps) ──────────────────

LONG_SESSION = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
LONG_MESSAGES = 3000
SESSION_MAP_SITES = tuple(
    f"_lib_conversation_query.{name}" for name in (
        "_session_cost_map", "_session_models_map",
        "_session_first_prompt_titles_map"))


def _claude_lines(first: int, count: int) -> str:
    lines = []
    for i in range(first, first + count):
        ts = (dt.datetime(2026, 7, 1, tzinfo=UTC)
              + dt.timedelta(seconds=7 * i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        role = "user" if i % 2 == 0 else "assistant"
        message = {"content": [{"text": f"audit turn {i}", "type": "text"}],
                   "role": role}
        record = {"cwd": "/synthetic/audit", "message": message,
                  "sessionId": LONG_SESSION, "timestamp": ts, "type": role,
                  "uuid": f"audit-u{i}"}
        if i % 2:
            message.update(id=f"audit-m{i}", model="claude-opus-4-8", usage={
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "input_tokens": 1, "output_tokens": 1})
            record.update(parentUuid=f"audit-u{i - 1}", requestId=f"audit-r{i}")
        lines.append(json.dumps(record) + "\n")
    return "".join(lines)


@pytest.mark.parametrize("temp_store", TEMP_STORES)
def test_a_long_session_resolves_cost_models_and_title_without_a_temp_structure(
    tmp_path, monkeypatch, temp_store,
):
    """A conversation-sync pass touching a session with thousands of messages
    resolves its cost (1,500 distinct turns), its models and its first-prompt
    title (1,500 human rows) with no temp structure. The title is one
    index-served ``LIMIT 12`` lookup per session."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _pin_writer_temp_store(monkeypatch, temp_store)
    ns["CONFIG_PATH"].write_text('{"conversation":{"retention_days":0}}\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    transcript = (tmp_path / "data" / ".claude" / "projects" / "-audit-long"
                  / f"{LONG_SESSION}.jsonl")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(_claude_lines(0, LONG_MESSAGES))
    conn = ns["open_conversations_db"]()
    try:
        ns["sync_claude_conversations"](conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id=?",
            (LONG_SESSION,)).fetchone()[0] == LONG_MESSAGES
    finally:
        conn.close()
    with transcript.open("a") as fh:
        fh.write(_claude_lines(LONG_MESSAGES, 20))
    dashboard = importlib.import_module("_cctally_dashboard")
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        recorder.phase = "conversation-sync-long-session"
        assert dashboard._conversation_sync_pass() == "ok"
    for site in SESSION_MAP_SITES:
        _bounded(recorder, site, temp_store)
    titles = [s for s in recorder.statements
              if SESSION_MAP_SITES[2] in s.call_sites
              and "FROM conversation_messages" in s.sql]
    assert titles and all(s.sql.endswith("LIMIT 12") for s in titles), [
        s.sql for s in titles]
    assert all(any("idx_conv_session_ts" in detail for detail in s.plan)
               for s in titles), [s.plan for s in titles]


_CLAUDE_SCHEMA = """
CREATE TABLE conversation_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT,
    uuid          TEXT,
    parent_uuid   TEXT,
    source_path   TEXT    NOT NULL,
    byte_offset   INTEGER NOT NULL,
    timestamp_utc TEXT,
    entry_type    TEXT    NOT NULL,
    text          TEXT    NOT NULL DEFAULT '',
    blocks_json   TEXT    NOT NULL DEFAULT '[]',
    model         TEXT,
    msg_id        TEXT,
    req_id        TEXT,
    cwd           TEXT,
    git_branch    TEXT,
    is_sidechain  INTEGER NOT NULL DEFAULT 0,
    source_tool_use_id TEXT,
    UNIQUE(source_path, byte_offset)
);
CREATE INDEX idx_conv_session_ts
    ON conversation_messages(session_id, timestamp_utc, id);
CREATE INDEX idx_conv_session_uuid ON conversation_messages(session_id, uuid);
CREATE INDEX idx_conv_source ON conversation_messages(source_path);
CREATE INDEX idx_conv_turnkey ON conversation_messages(msg_id, req_id);
CREATE INDEX idx_conversation_messages_model_session
    ON conversation_messages(model, session_id)
    WHERE model IS NOT NULL AND model != '';
CREATE TABLE session_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path         TEXT    NOT NULL,
    line_offset         INTEGER NOT NULL,
    timestamp_utc       TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    msg_id              TEXT,
    req_id              TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_create_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd_raw        REAL,
    speed               TEXT,
    cache_create_1h_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX idx_entries_dedup ON session_entries(msg_id, req_id)
    WHERE msg_id IS NOT NULL AND req_id IS NOT NULL;
"""

MARKER = "<command-name>/clear</command-name>"


def _claude_equivalence_db() -> sqlite3.Connection:
    """Sessions built to separate the rewrites from today's forms."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(_CLAUDE_SCHEMA)
    rows = []

    def add(row_id, sid, path, ts, kind, text="", model=None, msg=None,
            req=None, sidechain=0):
        rows.append((row_id, sid, path, len(rows), ts, kind, text, model, msg,
                     req, sidechain))

    t = "2026-07-01T00:00:{:02d}Z".format
    # sess-a: ids deliberately out of timestamp order; NULL timestamps (which
    # sort FIRST); a tie on one timestamp; markers, blank and sidechain rows
    # ahead of the real prompt; more than twelve candidates in all.
    main, resumed = "/p/sess-a.jsonl", "/p/resume/sess-a.jsonl"
    agent = "/p/sess-a/subagents/agent-abc.jsonl"
    add(90, "sess-a", main, None, "human", MARKER)
    add(80, "sess-a", main, None, "human", "   ")
    add(20, "sess-a", main, t(1), "human", "tie winner, row 20")
    add(30, "sess-a", main, t(1), "human", "tie loser, row 30")
    add(10, "sess-a", main, t(0), "human", "", )
    add(11, "sess-a", main, t(0), "human", "sidechain prompt", sidechain=1)
    for n in range(14):
        add(200 + n, "sess-a", main, t(10 + n), "human", f"later prompt {n}")
    add(300, "sess-a", main, t(2), "assistant", "a", "claude-opus-4-8",
        "m1", "r1")
    add(301, "sess-a", resumed, t(3), "assistant", "a", "claude-opus-4-8",
        "m1", "r1")
    add(302, "sess-a", main, t(4), "assistant", "a", "claude-opus-4-8",
        "m2", None)
    add(303, "sess-a", main, t(5), "assistant", "a", "claude-opus-4-8",
        None, "r3")
    add(304, "sess-a", main, t(6), "assistant", "a", "claude-opus-4-8",
        "m4", "r4")
    add(305, "sess-a", agent, t(7), "assistant", "a", "claude-haiku-4-5",
        "m5", "r5")
    add(306, "sess-a", main, t(8), "assistant", "a", "claude-sonnet-4-5",
        "m6", "r6", sidechain=1)
    add(307, "sess-a", main, t(9), "assistant", "a", "", "m7", "r7")
    add(308, "sess-a", main, t(9), "assistant", "a", None, "m8", "r8")
    # sess-b: the first twelve human candidates are all markers or blank, so
    # the real prompt is the thirteenth and the session has no title.
    for n in range(12):
        add(400 + n, "sess-b", "/p/sess-b.jsonl", t(n),
            "human", MARKER if n % 2 else " \n ")
    add(450, "sess-b", "/p/sess-b.jsonl", t(30), "human", "too late")
    add(451, "sess-b", "/p/sess-b.jsonl", t(31), "assistant", "b",
        "claude-opus-4-8", "m1", "r9")
    # sess-c: only empty and sidechain human rows.
    add(500, "sess-c", "/p/sess-c.jsonl", t(1), "human", "")
    add(501, "sess-c", "/p/sess-c.jsonl", t(2), "human", "side", sidechain=1)
    # sess-d: every human row on one timestamp; the smallest id wins.
    for row_id in (640, 610, 630, 620):
        add(row_id, "sess-d", "/p/sess-d.jsonl", t(5), "human",
            f"prompt from row {row_id}")
    conn.executemany(
        "INSERT INTO conversation_messages (id, session_id, source_path,"
        " byte_offset, timestamp_utc, entry_type, text, model, msg_id, req_id,"
        " is_sidechain) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT INTO session_entries (source_path, line_offset, timestamp_utc,"
        " model, msg_id, req_id, input_tokens, output_tokens,"
        " cache_create_tokens, cache_read_tokens, cache_create_1h_tokens)"
        " VALUES ('/e', ?, '2026-07-01T00:00:00Z', ?, ?, ?, ?, ?, ?, ?, ?)",
        [(i, model, m, r, 1000 + 7 * i, 300 + 3 * i, 50 * i, 9000 + i, 10 * i)
         for i, (model, m, r) in enumerate((
             ("claude-opus-4-8", "m1", "r1"), ("claude-opus-4-8", "m4", "r4"),
             ("claude-haiku-4-5", "m5", "r5"), ("claude-sonnet-4-5", "m6", "r6"),
             ("claude-opus-4-8", "m7", "r7"), ("claude-opus-4-8", "m1", "r9")))])
    conn.commit()
    return conn


def _legacy_session_cost_map(lq, conn, session_ids):
    costs = {sid: 0.0 for sid in session_ids}
    if not session_ids:
        return costs
    placeholders = ",".join("?" for _ in session_ids)
    pairs = conn.execute(
        "SELECT DISTINCT session_id, msg_id, req_id "
        "FROM conversation_messages "
        "WHERE session_id IN (%s) AND msg_id IS NOT NULL AND req_id IS NOT NULL"
        % placeholders,
        list(session_ids),
    ).fetchall()
    if not pairs:
        return costs
    key_cost = lq._turn_costs_for_keys(conn, [(m, r) for _, m, r in pairs])
    for sid, m, r in pairs:
        costs[sid] = costs.get(sid, 0.0) + key_cost.get((m, r), 0.0)
    return costs


def _legacy_session_models_map(lq, conn, session_ids):
    out = {sid: [] for sid in session_ids}
    if not session_ids:
        return out
    placeholders = ",".join("?" for _ in session_ids)
    per_session = {}
    for sid, model, source_path, is_sidechain in conn.execute(
            "SELECT DISTINCT session_id, model, source_path, is_sidechain "
            "FROM conversation_messages "
            "WHERE session_id IN (%s) AND model IS NOT NULL AND model != ''"
            % placeholders, list(session_ids)):
        per_session.setdefault(sid, []).append(
            (model, source_path, is_sidechain))
    for sid, rows in per_session.items():
        out[sid] = lq._models_main_first(rows)
    return out


def _legacy_first_prompt_titles_map(lq, conn, session_ids):
    if not session_ids:
        return {}
    titles = {}
    skip_skill_titles = lq._reingest_pending(conn)
    ph = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        "SELECT session_id, text FROM ("
        "  SELECT session_id, text, "
        "         ROW_NUMBER() OVER (PARTITION BY session_id "
        "                            ORDER BY timestamp_utc, id) AS rn "
        f"  FROM conversation_messages "
        f"  WHERE session_id IN ({ph}) AND entry_type='human' "
        "        AND is_sidechain=0 AND COALESCE(text,'') <> ''"
        ") WHERE rn <= 12 ORDER BY session_id, rn",
        tuple(session_ids),
    ).fetchall()
    for sid, text in rows:
        if sid in titles:
            continue
        if lq._is_system_marker(text) or lq._looks_like_command_plumbing(text):
            continue
        if (lq._is_compaction_body(text) or lq._is_notification_body(text)
                or lq._is_bash_echo_body(text)):
            continue
        if skip_skill_titles and lq._first_nonblank_line(text).startswith(
                lq._SKILL_PREAMBLE):
            continue
        t = lq._title_from_text(lq._strip_remote_control_prefix(text))
        if t:
            titles[sid] = t
    return titles


@pytest.mark.parametrize("session_ids", [
    ["sess-b", "sess-a", "sess-missing", "sess-a", "sess-d", "sess-c"],
    ["sess-a"],
    ["sess-missing"],
    [],
])
def test_the_session_batch_maps_match_todays_forms(session_ids):
    import _lib_conversation_query as lq

    conn = _claude_equivalence_db()
    try:
        for new, legacy in (
                (lq._session_cost_map, _legacy_session_cost_map),
                (lq._session_models_map, _legacy_session_models_map),
                (lq._session_first_prompt_titles_map,
                 _legacy_first_prompt_titles_map)):
            got = new(conn, list(session_ids))
            want = legacy(lq, conn, list(session_ids))
            assert got == want, (new.__name__, got, want)
            assert list(got.items()) == list(want.items()), new.__name__
        if "sess-a" in session_ids:
            titles = lq._session_first_prompt_titles_map(
                conn, ["sess-a", "sess-b", "sess-c", "sess-d"])
            # Non-vacuity: the title choice really ran over the tie, the NULL
            # timestamps and the twelve-row window.
            assert titles["sess-a"] == "tie winner, row 20"
            assert titles["sess-d"] == "prompt from row 610"
            assert "sess-b" not in titles and "sess-c" not in titles
            assert lq._session_cost_map(conn, ["sess-a"])["sess-a"] > 0
    finally:
        conn.close()


# ── Codex: one long conversation tied on a single timestamp ──────────────────

CODEX_SESSION = "a1901a19-0190-4190-8190-190190190190"
CODEX_TS = "2026-07-20T00:00:00Z"
CODEX_TURNS = 1500
CODEX_SITES = (
    "_cctally_cache._repair_codex_turn_ids_for_source",
    "_cctally_cache._load_codex_normalized_rows",
    "_lib_codex_conversation_query._load_conversation_index_rows",
    "_lib_codex_conversation_query.materialize_codex_find_projection",
)


def _codex_record(kind: str, payload: dict) -> dict:
    return {"payload": payload, "timestamp": CODEX_TS, "type": kind}


def _codex_turn(number: int) -> list:
    turn_id = f"audit-turn-{number:05d}"
    return [
        _codex_record("turn_context", {
            "model": "gpt-synthetic-codex", "model_context_window": 272000,
            "turn_id": turn_id}),
        _codex_record("response_item", {
            "content": [{"text": f"prompt {number}", "type": "input_text"}],
            "phase": "input", "role": "user", "type": "message"}),
        _codex_record("response_item", {
            "content": [{"text": f"answer {number}", "type": "output_text"}],
            "phase": "output", "role": "assistant", "type": "message"}),
    ]


def _write_jsonl(path: pathlib.Path, records: list, *, mode: str = "w") -> None:
    with path.open(mode, encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


@pytest.mark.parametrize("temp_store", TEMP_STORES)
def test_a_long_tied_codex_conversation_loads_without_a_temp_structure(
    tmp_path, monkeypatch, temp_store,
):
    """One conversation of thousands of messages on ONE timestamp is one tie
    run of its whole history. The conversation-sync pass that receives a late
    turn anchor repairs its rollout's turn ids, reloads it and rebuilds its
    find projection with no temp structure."""
    ns = load_script()
    redirect_paths_without_conversation_retention(
        ns, monkeypatch, tmp_path / "data")
    _pin_writer_temp_store(monkeypatch, temp_store)
    provider = tmp_path / "provider"
    sessions = provider / "sessions" / "2026" / "07" / "20"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    rollout = sessions / "rollout-audit.jsonl"
    records = [_codex_record("session_meta", {
        "cwd": "/synthetic/audit-codex",
        "git": {"branch": "audit", "repository": "audit"},
        "id": CODEX_SESSION, "model": "gpt-synthetic-codex",
        "model_provider": "audit", "session_id": CODEX_SESSION,
        "source": "codex", "thread_source": "audit-thread"})]
    for number in range(CODEX_TURNS):
        records.extend(_codex_turn(number))
    _write_jsonl(rollout, records)

    def sync(conn) -> None:
        cache = ns["open_cache_db"]()
        try:
            ns["sync_codex_cache"](cache)
        finally:
            cache.close()
        stats = ns["sync_codex_conversations"](conn)
        assert stats.files_failed == 0, stats

    conn = ns["open_conversations_db"]()
    try:
        sync(conn)
        key, count = conn.execute(
            "SELECT conversation_key, COUNT(*) FROM codex_conversation_messages"
            " GROUP BY conversation_key").fetchone()
        assert count >= 2 * CODEX_TURNS, "non-vacuity: one long conversation"
    finally:
        conn.close()
    # A late native turn anchor: an unanchored exchange, then the completion
    # that names its turn, which is what arms the per-file turn-id repair.
    _write_jsonl(rollout, [
        _codex_record("response_item", {
            "content": [{"text": "late prompt", "type": "input_text"}],
            "phase": "input", "role": "user", "type": "message"}),
        _codex_record("response_item", {
            "content": [{"text": "late answer", "type": "output_text"}],
            "phase": "output", "role": "assistant", "type": "message"}),
        _codex_record("event_msg", {
            "completed_at": CODEX_TS, "duration_ms": 1,
            "last_agent_message": "late answer", "turn_id": "audit-turn-late",
            "type": "task_complete"}),
    ], mode="a")
    cache_mod = importlib.import_module("_cctally_cache")
    query = importlib.import_module("_lib_codex_conversation_query")
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        recorder.phase = "codex-sync-late-anchor"
        conn = ns["open_conversations_db"]()
        try:
            sync(conn)
            recorder.phase = "codex-loaders"
            assert len(cache_mod._load_codex_normalized_rows(conn, key)) > count
            assert len(query._load_conversation_index_rows(conn, key)[0]) > count
        finally:
            conn.close()
    for site in CODEX_SITES:
        _bounded(recorder, site, temp_store)


_CODEX_SCHEMA = """
CREATE TABLE codex_conversation_messages (
    id               INTEGER PRIMARY KEY,
    conversation_key TEXT NOT NULL,
    source_root_key  TEXT NOT NULL,
    source_path      TEXT NOT NULL,
    line_offset      INTEGER NOT NULL,
    timestamp_utc    TEXT,
    turn_id          TEXT,
    call_id          TEXT,
    kind             TEXT NOT NULL,
    event_type       TEXT,
    record_family    TEXT NOT NULL,
    model            TEXT,
    text             TEXT,
    content_digest   TEXT NOT NULL,
    content_len      INTEGER NOT NULL CHECK(content_len >= 0),
    detail_json      TEXT,
    search_tool      TEXT,
    search_thinking  TEXT,
    UNIQUE(source_path, line_offset)
);
CREATE INDEX idx_codex_conv_msgs_conversation
    ON codex_conversation_messages(conversation_key, timestamp_utc, id);
CREATE INDEX idx_codex_conv_msgs_source
    ON codex_conversation_messages(source_path);
CREATE TABLE codex_conversation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL, line_offset INTEGER NOT NULL,
    source_root_key TEXT NOT NULL, conversation_key TEXT,
    native_thread_id TEXT, root_thread_id TEXT, parent_thread_id TEXT,
    timestamp_utc TEXT, record_type TEXT, event_type TEXT, turn_id TEXT,
    call_id TEXT, payload_json TEXT NOT NULL,
    UNIQUE(source_path, line_offset)
);
"""

#: (id, conversation, path, offset, timestamp). Ids run against physical order
#: inside every tie run; offsets 9/10/100 separate numeric from text order;
#: '/B' < '/a' < '/é' in BINARY order; two spellings of one instant are two
#: distinct timestamps; NULL timestamps sort first.
_CODEX_ROWS = (
    (7, "k1", "/b.jsonl", 20, None),
    (3, "k1", "/a.jsonl", 300, None),
    (9, "k1", "/a.jsonl", 40, None),
    (1, "k1", "/é.jsonl", 9, "2026-07-20T00:00:00Z"),
    (2, "k1", "/a.jsonl", 100, "2026-07-20T00:00:00Z"),
    (4, "k1", "/a.jsonl", 10, "2026-07-20T00:00:00Z"),
    (5, "k1", "/B.jsonl", 9, "2026-07-20T00:00:00Z"),
    (6, "k1", "/a.jsonl", 9, "2026-07-20T00:00:00Z"),
    (8, "k1", "/a.jsonl", 1, "2026-07-20T00:00:00.000Z"),
    (12, "k1", "/b.jsonl", 2, "2026-07-20T00:00:01Z"),
    (11, "k1", "/a.jsonl", 3, "2026-07-20T00:00:01Z"),
    (10, "k2", "/a.jsonl", 2, "2026-07-20T00:00:00Z"),
    (13, "k2", "/a.jsonl", 0, None),
)


def _codex_equivalence_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_CODEX_SCHEMA)
    conn.executemany(
        "INSERT INTO codex_conversation_messages (id, conversation_key,"
        " source_root_key, source_path, line_offset, timestamp_utc, turn_id,"
        " call_id, kind, event_type, record_family, model, text,"
        " content_digest, content_len, detail_json, search_tool,"
        " search_thinking) VALUES (?,?,'root',?,?,?,?,NULL,'message',NULL,"
        " 'response_item','m',?,?,?,?,NULL,NULL)",
        [(row_id, key, path, offset, ts, f"t{row_id}", f"text {row_id}",
          f"d{row_id}", 6, None if row_id % 3 else '{"é": 1}')
         for row_id, key, path, offset, ts in _CODEX_ROWS])
    conn.commit()
    return conn


def test_the_codex_conversation_loaders_match_todays_physical_order():
    import _cctally_cache as cache
    import _lib_codex_conversation as kern
    import _lib_codex_conversation_query as query

    conn = _codex_equivalence_db()
    try:
        for key in ("k1", "k2", "missing"):
            legacy_norm = [
                kern.CodexNormalizedRow(*row) for row in conn.execute(
                    "SELECT " + cache._CODEX_NORM_COLS
                    + " FROM codex_conversation_messages WHERE conversation_key"
                    " = ? ORDER BY timestamp_utc, source_path, line_offset",
                    (key,))]
            assert cache._load_codex_normalized_rows(conn, key) == legacy_norm
            legacy_wide = [
                kern.CodexNormalizedRow(*row) for row in conn.execute(
                    "SELECT " + query._ROW_COLS
                    + " FROM codex_conversation_messages WHERE conversation_key=?"
                    " ORDER BY timestamp_utc,source_path,line_offset", (key,))]
            assert query._load_conversation_rows(conn, key) == legacy_wide
            legacy_positions = [
                tuple(row) for row in conn.execute(
                    "SELECT " + query._NARROW_ROW_COLS
                    + " FROM codex_conversation_messages WHERE conversation_key"
                    " = ? ORDER BY timestamp_utc, source_path, line_offset",
                    (key,))]
            rows, detail_bytes = query._load_conversation_index_rows(conn, key)
            assert [(r.source_path, r.line_offset, r.timestamp_utc, r.turn_id)
                    for r in rows] == [p[:4] for p in legacy_positions]
            assert detail_bytes == {
                (p[0], p[1]): p[-1] or 0 for p in legacy_positions}
        # Non-vacuity: the tie runs really are out of id order.
        order = [(r.source_path, r.line_offset)
                 for r in query._load_conversation_rows(conn, "k1")]
        assert order[:3] == [("/a.jsonl", 40), ("/a.jsonl", 300),
                             ("/b.jsonl", 20)]
        # '.000Z' sorts before 'Z': the two spellings are two tie runs.
        assert order[3] == ("/a.jsonl", 1)
        assert order[4:9] == [("/B.jsonl", 9), ("/a.jsonl", 9),
                              ("/a.jsonl", 10), ("/a.jsonl", 100),
                              ("/é.jsonl", 9)]
    finally:
        conn.close()


def _legacy_repair_affected(conn, source_path):
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT conversation_key FROM codex_conversation_messages "
        "WHERE source_path=?", (source_path,)) if row[0]}


def test_the_turn_id_repair_reports_todays_affected_conversations():
    import _cctally_cache as cache

    def build():
        conn = _codex_equivalence_db()
        conn.executemany(
            "INSERT INTO codex_conversation_messages (conversation_key,"
            " source_root_key, source_path, line_offset, timestamp_utc,"
            " turn_id, kind, record_family, content_digest, content_len)"
            " VALUES (?, 'root', '/r.jsonl', ?, NULL, ?, 'message',"
            " 'response_item', 'd', 0)",
            [("k3", 1, "x"), ("", 2, "y"), ("k3", 3, None), ("k4", 4, "z")])
        conn.commit()
        return conn

    expected = {"/r.jsonl": {"k3", "k4"}, "/a.jsonl": {"k1", "k2"},
                "/missing.jsonl": set()}
    for path, known in expected.items():
        reference, candidate = build(), build()
        try:
            want = _legacy_repair_affected(reference, path)
            assert want == known, "non-vacuity: the fixture's keys"
            got = cache._repair_codex_turn_ids_for_source(candidate, path)
            assert got == want, (path, got, want)
            # The repair itself is unchanged: with no events every stored turn
            # on the path is cleared, exactly as before.
            assert candidate.execute(
                "SELECT COUNT(*) FROM codex_conversation_messages"
                " WHERE source_path=? AND turn_id IS NOT NULL",
                (path,)).fetchone()[0] == 0
        finally:
            reference.close()
            candidate.close()


# ── Codex quota attribution: one large axis shard ────────────────────────────

ROOT = "r" * 32
WITNESS = "2026-07-21T12:00:00Z"
ANCHOR_EXPR = "COALESCE(canonical_resets_at_utc, resets_at_utc)"


def _seed_quota_shard(conn, rows: int) -> None:
    """A shard whose witness reset is carried by ``rows`` captures, plus
    NULL/blank canonical anchors, a second anchor, a snap-equivalent length
    and rows outside the shard."""
    data = []
    for i in range(rows):
        data.append((ROOT, f"/r/{i % 7}.jsonl", i, "secondary", "codex",
                     10080, WITNESS,
                     None if i % 5 == 0 else "2026-07-21T12:00:00+00:00"))
    data += [
        (ROOT, "/r/x.jsonl", 1, "secondary", "codex", 10081, WITNESS,
         "2026-07-21T11:59:58+00:00"),
        (ROOT, "/r/x.jsonl", 2, "secondary", "codex", 10080, WITNESS, "  "),
        (ROOT, "/r/x.jsonl", 3, "primary", "codex", 10080, WITNESS,
         "2026-07-21T00:00:00+00:00"),
        ("o" * 32, "/r/x.jsonl", 4, "secondary", "codex", 10080, WITNESS,
         "2026-07-22T00:00:00+00:00"),
        (ROOT, "/r/x.jsonl", 5, "secondary", "codex", 10080,
         "2026-07-28T12:00:00Z", "2026-07-28T12:00:00+00:00"),
    ]
    conn.executemany(
        "INSERT INTO quota_window_snapshots (source, source_root_key,"
        " source_path, line_offset, captured_at_utc, observed_slot,"
        " logical_limit_key, window_minutes, used_percent, resets_at_utc,"
        " canonical_resets_at_utc) VALUES ('codex', ?, ?, ?,"
        " '2026-07-15T00:00:00Z', ?, ?, ?, 10.0, ?, ?)", data)
    conn.commit()


@pytest.mark.parametrize("temp_store", TEMP_STORES)
def test_a_large_attribution_shard_resolves_its_anchors_without_a_temp_structure(
    tmp_path, monkeypatch, temp_store,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_cache_db"]()
    try:
        _seed_quota_shard(conn, 3000)
    finally:
        conn.close()
    quota = importlib.import_module("_cctally_quota")
    assertion = {"source_root_key": ROOT, "logical_limit_key": "codex",
                 "observed_slot": "secondary", "window_minutes": 10080,
                 "raw_resets_at_utc": [WITNESS]}
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
        try:
            assert quota._load_codex_window_group_evidence(conn, [assertion])
        finally:
            conn.close()
    _bounded(recorder, "_cctally_quota._codex_witness_anchors", temp_store)
    _bounded(recorder, "_cctally_quota._load_codex_window_group_evidence",
             temp_store)


def test_the_witness_anchors_match_todays_distinct(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    quota = importlib.import_module("_cctally_quota")
    conn = ns["open_cache_db"]()
    try:
        _seed_quota_shard(conn, 40)
        for axes, witnesses in (
                ((ROOT, "codex", "secondary", 10080), [WITNESS]),
                ((ROOT, "codex", "secondary", 10080),
                 [WITNESS, "2026-07-28T12:00:00Z", "2026-01-01T00:00:00Z"]),
                ((ROOT, "codex", "primary", 10080), [WITNESS]),
                (("missing", "codex", "secondary", 10080), [WITNESS])):
            clause, params = quota._codex_shard_axis_filter(axes)
            legacy = set()
            for (anchor,) in conn.execute(
                    f"SELECT DISTINCT {ANCHOR_EXPR} AS anchor"
                    "  FROM quota_window_snapshots" + clause
                    + "   AND unixepoch(resets_at_utc) IN ("
                    + ",".join("unixepoch(?)" for _ in witnesses) + ")",
                    (*params, *witnesses)):
                if anchor is None or not str(anchor).strip():
                    continue
                legacy.add(str(anchor))
            got = quota._codex_witness_anchors(
                conn, ANCHOR_EXPR, clause, params, witnesses)
            assert got == legacy, (axes, got, legacy)
        clause, params = quota._codex_shard_axis_filter(
            (ROOT, "codex", "secondary", 10080))
        assert quota._codex_witness_anchors(
            conn, ANCHOR_EXPR, clause, params, [WITNESS]) == {
            WITNESS, "2026-07-21T12:00:00+00:00", "2026-07-21T11:59:58+00:00"}
    finally:
        conn.close()


# ── Claude weekly: one large subscription week ───────────────────────────────

NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
WEEK_START_SPELLINGS = ("2026-07-14T12:00:00+00:00", "2026-07-14T12:00:00Z")
WEEK_END = "2026-07-21T12:00:00+00:00"


def _seed_week(conn, rows: int, *, boundary: bool = True) -> None:
    """One subscription week: two spellings of its start, NULL-start legacy
    rows on its date, two accounts, held rows, NULL five-hour readings, ties on
    one capture instant, a capture after ``NOW``, and a prior week."""
    data = []
    base = dt.datetime(2026, 7, 14, 13, 0, tzinfo=UTC)
    for i in range(rows):
        captured = (base + dt.timedelta(minutes=2 * (i // 8))).isoformat()
        start = (WEEK_START_SPELLINGS[i % 2] if boundary and i % 7 else None)
        data.append((
            captured, "2026-07-14", "2026-07-21", start,
            WEEK_END if start else None, float(i % 100),
            "acct-a" if i % 4 == 0 else "unattributed", int(i % 9 == 0),
            None if i % 6 == 0 else float(i % 50)))
    data.append(((NOW + dt.timedelta(hours=1)).isoformat(), "2026-07-14",
                 "2026-07-21", WEEK_START_SPELLINGS[0] if boundary else None,
                 WEEK_END if boundary else None, 99.0, "unattributed", 0, 1.0))
    data.append(("2026-07-08T00:00:00+00:00", "2026-07-07", "2026-07-13",
                 "2026-07-07T12:00:00+00:00" if boundary else None,
                 "2026-07-14T12:00:00+00:00" if boundary else None, 50.0,
                 "unattributed", 0, 2.0))
    conn.executemany(
        "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date,"
        " week_end_date, week_start_at, week_end_at, weekly_percent, source,"
        " payload_json, account_key, weekly_observation_held,"
        " five_hour_percent) VALUES (?,?,?,?,?,?,'statusline','{}',?,?,?)",
        data)
    conn.commit()


WEEK_VARIANTS = [
    {"account_key": account, "include_held": held, "include_account": owner}
    for account in (None, "unattributed", "acct-a", "acct-missing")
    for held in (False, True) for owner in (False, True)
]


@pytest.mark.parametrize("boundary", [True, False])
@pytest.mark.parametrize("temp_store", TEMP_STORES)
def test_a_large_week_fetches_its_samples_without_a_temp_structure(
    tmp_path, monkeypatch, temp_store, boundary,
):
    """Both legs: the boundary-aware read (rewritten) and the date-only
    fallback (index-served by ``idx_usage_week_time``, unchanged), under every
    account, held and owner variant."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_db"]()
    try:
        _seed_week(conn, 3000, boundary=boundary)
    finally:
        conn.close()
    forecast = importlib.import_module("_cctally_forecast")
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        conn = sqlite3.connect(_cctally_core.DB_PATH)
        try:
            for variant in WEEK_VARIANTS:
                forecast._fetch_current_week_snapshots(conn, NOW, **variant)
        finally:
            conn.close()
    _bounded(recorder, "_cctally_forecast._fetch_current_week_snapshots",
             temp_store)


def _legacy_fetch_current_week_snapshots(conn, now_utc, *, account_key=None,
                                         include_held=False,
                                         include_account=False):
    """Today's ``_fetch_current_week_snapshots``, frozen verbatim."""
    fc = importlib.import_module("_cctally_forecast")
    parse_iso_datetime = fc.parse_iso_datetime
    _shape_week_samples = fc._shape_week_samples
    _acct_pred = "" if account_key is None else " AND account_key = ?"
    _acct_p: tuple = () if account_key is None else (account_key,)
    _held_pred = "" if include_held else " AND weekly_observation_held = 0"
    _owner_column = ", account_key" if include_account else ""
    _sample_order = " ASC, id ASC" if include_account else " ASC"
    candidates = conn.execute(
        "SELECT week_start_at, week_end_at, week_start_date, MAX(captured_at_utc) AS latest_cap "
        "FROM weekly_usage_snapshots "
        "WHERE week_start_at IS NOT NULL AND week_end_at IS NOT NULL"
        + _acct_pred + _held_pred +
        " GROUP BY week_start_at, week_end_at, week_start_date",
        _acct_p,
    ).fetchall()
    chosen = None
    chosen_cap = None
    for r in candidates:
        try:
            ws = parse_iso_datetime(r[0], "week_start_at")
            we = parse_iso_datetime(r[1], "week_end_at")
        except ValueError:
            continue
        if ws <= now_utc < we:
            cap = r[3]
            if chosen is None or (cap is not None and (chosen_cap is None or cap > chosen_cap)):
                chosen = r
                chosen_cap = cap
    if chosen is None:
        today_local_str = now_utc.astimezone().date().isoformat()
        drow = conn.execute(
            "SELECT week_start_date, week_end_date "
            "FROM weekly_usage_snapshots "
            "WHERE week_start_date <= ? AND week_end_date >= ?"
            + _acct_pred + _held_pred +
            " GROUP BY week_start_date, week_end_date "
            "ORDER BY MAX(captured_at_utc) DESC LIMIT 1",
            (today_local_str, today_local_str) + _acct_p,
        ).fetchone()
        if drow is None:
            return None
        local_tz = dt.datetime.now().astimezone().tzinfo
        ws_date = dt.date.fromisoformat(drow[0])
        we_date = dt.date.fromisoformat(drow[1])
        week_start_at = dt.datetime.combine(ws_date, dt.time(0, 0), local_tz).astimezone(dt.timezone.utc)
        week_end_at = dt.datetime.combine(we_date + dt.timedelta(days=1), dt.time(0, 0), local_tz).astimezone(dt.timezone.utc)
        rows = conn.execute(
            "SELECT captured_at_utc, weekly_percent, five_hour_percent, "
            "       weekly_observation_held" + _owner_column + " "
            "FROM weekly_usage_snapshots "
            "WHERE week_start_date = ?" + _acct_pred + _held_pred +
            " ORDER BY captured_at_utc" + _sample_order,
            (drow[0],) + _acct_p,
        ).fetchall()
        samples = _shape_week_samples(rows, include_held=include_held,
                                      include_account=include_account)
        samples = [s for s in samples if s[0] <= now_utc]
        return week_start_at, week_end_at, samples
    row = chosen
    week_start_at = parse_iso_datetime(row[0], "week_start_at")
    week_end_at = parse_iso_datetime(row[1], "week_end_at")
    matching_texts: list[str] = []
    for r in candidates:
        try:
            rws = parse_iso_datetime(r[0], "week_start_at")
        except ValueError:
            continue
        if rws == week_start_at:
            matching_texts.append(r[0])
    chosen_date = chosen[2]
    placeholders = ",".join("?" * len(matching_texts))
    rows = conn.execute(
        f"SELECT captured_at_utc, weekly_percent, five_hour_percent, "
        f"       weekly_observation_held{_owner_column} "
        f"FROM weekly_usage_snapshots "
        f"WHERE (week_start_at IN ({placeholders}) "
        f"       OR (week_start_at IS NULL AND week_start_date = ?))"
        f"{_acct_pred}{_held_pred} "
        f"ORDER BY captured_at_utc{_sample_order}",
        tuple(matching_texts) + (chosen_date,) + _acct_p,
    ).fetchall()
    samples = _shape_week_samples(rows, include_held=include_held,
                                  include_account=include_account)
    samples = [s for s in samples if s[0] <= now_utc]
    return week_start_at, week_end_at, samples


@pytest.mark.parametrize("boundary", [True, False])
def test_the_week_samples_match_todays_form(tmp_path, monkeypatch, boundary):
    """Both legs (boundary-aware and the date-only fallback), every account,
    held and owner variant, with captures tied on one instant."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_db"]()
    try:
        _seed_week(conn, 240, boundary=boundary)
    finally:
        conn.close()
    forecast = importlib.import_module("_cctally_forecast")
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        nonempty = 0
        for variant in WEEK_VARIANTS:
            got = forecast._fetch_current_week_snapshots(conn, NOW, **variant)
            want = _legacy_fetch_current_week_snapshots(conn, NOW, **variant)
            assert got == want, variant
            if want is not None and want[2]:
                nonempty += 1
                captured = [sample[0] for sample in want[2]]
                assert len(captured) > len(set(captured)), (
                    "non-vacuity: the samples carry tied captures", variant)
        assert nonempty >= 8
    finally:
        conn.close()


# ── Dashboard: the projects panel's weekly readings over its whole window ────

PROJECTS_SITE = "_cctally_dashboard._projects_week_usage_rows"
PROJECTS_SINCE, PROJECTS_END = "2026-05-05", "2026-07-28"


def _seed_projects_window(conn, rows: int) -> None:
    """Twelve weeks of readings, captured in the reverse of insertion order
    with three rows on each capture instant, plus held rows, NULL starts, an
    unparseable week date and rows on both sides of the window
    (``weekly_percent`` is NOT NULL in the schema)."""
    data = []
    base = dt.datetime(2026, 5, 5, tzinfo=UTC)
    for i in range(rows):
        week = dt.date(2026, 5, 5) + dt.timedelta(days=7 * (i % 12))
        captured = (base + dt.timedelta(minutes=7 * ((rows - i) // 3))).isoformat()
        data.append((
            captured, week.isoformat(), (week + dt.timedelta(days=6)).isoformat(),
            None if i % 5 == 0 else f"{week.isoformat()}T12:00:00+00:00",
            float(i % 100), int(i % 9 == 0)))
    data += [
        ("2026-05-01T00:00:00+00:00", "2026-04-28", "2026-05-04", None, 10.0, 0),
        ("2026-07-29T00:00:00+00:00", "2026-07-28", "2026-08-03", None, 11.0, 0),
        ("2026-06-01T00:00:00+00:00", "2026-06-xx", "2026-06-07", None, 12.0, 0),
    ]
    conn.executemany(
        "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date,"
        " week_end_date, week_start_at, weekly_percent, source, payload_json,"
        " account_key, weekly_observation_held)"
        " VALUES (?,?,?,?,?,'statusline','{}','unattributed',?)", data)
    conn.commit()


@pytest.mark.parametrize("temp_store", TEMP_STORES)
def test_a_long_projects_window_reads_its_weeks_without_a_temp_structure(
    tmp_path, monkeypatch, temp_store,
):
    """Twelve weeks of status-line readings, read the way the projects panel
    reads them on every build. The rule ``projects-week-window`` is gone: a
    week is not a bound, and twelve of them hold every reading of a quarter."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_db"]()
    try:
        _seed_projects_window(conn, 3000)
    finally:
        conn.close()
    dashboard = importlib.import_module("_cctally_dashboard")
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        conn = sqlite3.connect(_cctally_core.DB_PATH)
        try:
            rows = dashboard._projects_week_usage_rows(
                conn, PROJECTS_SINCE, PROJECTS_END)
        finally:
            conn.close()
    assert len(rows) > 2000
    _bounded(recorder, PROJECTS_SITE, temp_store)


def _legacy_projects_week_usage_rows(conn, since_date, end_date):
    """Today's projects-panel weekly read, frozen verbatim."""
    import _cctally_core
    return conn.execute(
        " SELECT week_start_date, week_start_at, weekly_percent,"
        " captured_at_utc, id"
        " FROM weekly_usage_snapshots"
        " WHERE week_start_date >= ? AND week_start_date < ?"
        " AND date(week_start_date) IS NOT NULL"
        " AND weekly_percent IS NOT NULL"
        + _cctally_core.weekly_held_exclusion(conn) +
        " ORDER BY captured_at_utc ASC, id ASC",
        (since_date, end_date),
    ).fetchall()


def test_the_projects_window_rows_match_todays_order(tmp_path, monkeypatch):
    """The same rows in the same order, over captures tied on one instant and
    captured in the reverse of their ids, with held, unparseable and
    out-of-window rows excluded."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_db"]()
    try:
        _seed_projects_window(conn, 240)
    finally:
        conn.close()
    dashboard = importlib.import_module("_cctally_dashboard")
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        got = dashboard._projects_week_usage_rows(
            conn, PROJECTS_SINCE, PROJECTS_END)
        want = _legacy_projects_week_usage_rows(
            conn, PROJECTS_SINCE, PROJECTS_END)
        total = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()[0]
    finally:
        conn.close()
    assert [tuple(row) for row in got] == [tuple(row) for row in want]
    captured = [row[3] for row in want]
    assert len(captured) > len(set(captured)), "non-vacuity: tied captures"
    ids = [row[4] for row in want]
    assert ids != sorted(ids), "non-vacuity: capture order is not id order"
    assert 0 < len(want) < total, "non-vacuity: the filters exclude rows"
    assert {row[0] for row in want} <= {
        (dt.date(2026, 5, 5) + dt.timedelta(days=7 * k)).isoformat()
        for k in range(12)}


# ── Retention: NULL-identity rows of a large legacy class ────────────────────

def _seed_null_identity(conn, rows: int) -> None:
    claude, codex = [], []
    for i in range(rows):
        path = f"/legacy/{i % 5}.jsonl"
        ts = None if i % 11 == 0 else f"2026-06-{1 + i % 28:02d}T00:00:00Z"
        claude.append((path, i, ts))
        codex.append((path, i, ts))
    claude += [("/legacy/all-null.jsonl", 1, None),
               ("/legacy/recent.jsonl", 1, "2026-09-30T00:00:00Z"),
               ("/legacy/recent.jsonl", 2, "2026-01-01T00:00:00Z"),
               ("/legacy/B.jsonl", 1, "2026-01-01T00:00:00Z")]
    codex += [("/legacy/all-null.jsonl", 1, None),
              ("/legacy/B.jsonl", 1, "2026-01-01T00:00:00Z")]
    conn.executemany(
        "INSERT INTO conversation_messages (session_id, source_path,"
        " byte_offset, timestamp_utc, entry_type, text)"
        " VALUES (NULL, ?, ?, ?, 'human', 'legacy')", claude)
    conn.executemany(
        "INSERT INTO codex_conversation_events (source_path, line_offset,"
        " source_root_key, conversation_key, timestamp_utc, payload_json)"
        " VALUES (?, ?, 'root', NULL, ?, '{}')", codex)
    conn.commit()


RETENTION_TABLES = (("conversation_messages", "session_id"),
                    ("codex_conversation_events", "conversation_key"))


@pytest.mark.parametrize("temp_store", TEMP_STORES)
def test_a_large_null_identity_class_is_pruned_without_a_temp_structure(
    tmp_path, monkeypatch, temp_store,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _pin_writer_temp_store(monkeypatch, temp_store)
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        _seed_null_identity(conn, 3000)
    finally:
        conn.close()
    retention = importlib.import_module("_lib_conversation_retention")
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        conn = ns["open_conversations_db"](attach_cache=False)
        try:
            for table, key in RETENTION_TABLES:
                assert retention._prunable_null_identity_paths(
                    conn, table, key, "2026-09-01T00:00:00Z")
        finally:
            conn.close()
    _bounded(recorder,
             "_lib_conversation_retention._prunable_null_identity_paths",
             temp_store)


def test_the_null_identity_paths_match_todays_group_by(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    retention = importlib.import_module("_lib_conversation_retention")
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        _seed_null_identity(conn, 60)
        for table, key in RETENTION_TABLES:
            for cutoff in ("2026-09-01T00:00:00Z", "2026-06-15T00:00:00Z",
                           "2025-01-01T00:00:00Z", "2026-09-30T00:00:00Z"):
                want = [row[0] for row in conn.execute(
                    f"SELECT source_path FROM {table} WHERE {key} IS NULL "
                    "GROUP BY source_path HAVING MAX(timestamp_utc) IS NOT NULL"
                    " AND MAX(timestamp_utc) < ?", (cutoff,))]
                got = retention._prunable_null_identity_paths(
                    conn, table, key, cutoff)
                assert got == want, (table, cutoff, got, want)
        # Non-vacuity: an all-NULL path never qualifies, a path whose latest
        # row is after the cutoff does not, and the output is path-ordered
        # in BINARY (byte) order.
        late = retention._prunable_null_identity_paths(
            conn, "conversation_messages", "session_id", "2027-01-01T00:00:00Z")
        assert late == sorted(late) and "/legacy/B.jsonl" in late
        assert "/legacy/all-null.jsonl" not in late
        assert "/legacy/recent.jsonl" in late and (
            "/legacy/recent.jsonl" not in retention._prunable_null_identity_paths(
                conn, "conversation_messages", "session_id",
                "2026-09-01T00:00:00Z"))
    finally:
        conn.close()


# ── Doctor: a large trailing week and a large violation batch ────────────────

def _seed_accounts_week(conn, rows: int) -> None:
    conn.executemany(
        "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date,"
        " week_end_date, weekly_percent, source, payload_json, account_key)"
        " VALUES (?, '2026-07-14', '2026-07-21', 1.0, 'statusline', '{}', ?)",
        [((NOW - dt.timedelta(minutes=3 * i)).isoformat(),
          ("unattributed", "a" * 32, "b" * 32, "")[i % 4])
         for i in range(rows)]
        + [((NOW - dt.timedelta(days=9)).isoformat(), "c" * 32)])
    conn.commit()


@pytest.mark.parametrize("temp_store", TEMP_STORES)
def test_a_large_trailing_week_counts_accounts_without_a_temp_structure(
    tmp_path, monkeypatch, temp_store,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_db"]()
    try:
        _seed_accounts_week(conn, 3000)
    finally:
        conn.close()
    doctor = importlib.import_module("_cctally_doctor")
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        state = doctor._gather_accounts_state(NOW)
    assert state["recent_attributed"] + state["recent_unattributed"] == 3000
    statements = _bounded(recorder, "_cctally_doctor._gather_accounts_state",
                          temp_store)
    assert any("FROM weekly_usage_snapshots" in s.sql for s in statements)


def test_the_trailing_week_counts_match_todays_group_by(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_db"]()
    try:
        _seed_accounts_week(conn, 97)
    finally:
        conn.close()
    doctor = importlib.import_module("_cctally_doctor")
    state = doctor._gather_accounts_state(NOW)
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        attributed = unattributed = 0
        for account_key, cnt in conn.execute(
                "SELECT account_key, COUNT(*) FROM weekly_usage_snapshots "
                "WHERE captured_at_utc >= ? GROUP BY account_key",
                ((NOW - dt.timedelta(days=7)).astimezone(UTC).isoformat(),)):
            if account_key and account_key != "unattributed":
                attributed += int(cnt)
            else:
                unattributed += int(cnt)
    finally:
        conn.close()
    assert (state["recent_attributed"], state["recent_unattributed"]) == (
        attributed, unattributed)
    assert attributed and unattributed


def _seed_violations(conn, batch_rows: int) -> None:
    rows = []
    for i in range(batch_rows):
        rows.append((f"fp-{(i * 7919) % batch_rows:06d}-é" if i % 3 else
                     f"FP-{i:06d}", "batch-big", ("kind-b", "kind-a",
                     "Kind-c")[i % 3], json.dumps({"n": i, "auditId": "x"}
                     if i % 10 == 0 else {"n": i})))
    rows += [("fp-x", "batch-a", "kind-z", json.dumps({"n": "a"})),
             ("fp-y", "batch-z", "kind-a", json.dumps({"n": "z"}))]
    conn.executemany(
        "INSERT INTO journal_protocol_violations (fingerprint, batch_id, kind,"
        " violation_json) VALUES (?,?,?,?)", rows)
    conn.commit()


@pytest.mark.parametrize("temp_store", TEMP_STORES)
def test_a_large_violation_batch_is_read_without_a_temp_structure(
    tmp_path, monkeypatch, temp_store,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_db"]()
    try:
        _seed_violations(conn, 3000)
    finally:
        conn.close()
    doctor = importlib.import_module("_cctally_doctor")
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        conn = sqlite3.connect(_cctally_core.DB_PATH)
        try:
            assert len(doctor._journal_protocol_violation_rows(conn)) == 3002
        finally:
            conn.close()
    _bounded(recorder, "_cctally_doctor._journal_protocol_violation_rows",
             temp_store)


def test_the_violation_rows_match_todays_order(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_db"]()
    try:
        _seed_violations(conn, 300)
    finally:
        conn.close()
    doctor = importlib.import_module("_cctally_doctor")
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        want = [json.loads(str(row[0])) for row in conn.execute(
            "SELECT violation_json FROM journal_protocol_violations "
            "ORDER BY batch_id, kind, fingerprint")]
        assert doctor._journal_protocol_violation_rows(conn) == want
        assert want[0] == {"n": "a"} and want[-1] == {"n": "z"}
    finally:
        conn.close()


# ── Codex cycles: the 5h rows of one large weekly cycle ──────────────────────

def _seed_five_hour_blocks(conn, rows: int) -> None:
    """Jittered 300-minute blocks across one week: ties on one start second in
    several spellings, both accepted accounts, a foreign account, orphans."""
    data = []
    base = dt.datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    for i in range(rows):
        start = base + dt.timedelta(minutes=5 * (i // 4))
        spelled = (start.isoformat(), start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   (start + dt.timedelta(milliseconds=250)).strftime(
                       "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                   start.astimezone(dt.timezone(dt.timedelta(hours=2))
                                    ).isoformat())[i % 4]
        reset = (start + dt.timedelta(hours=5, seconds=i)).isoformat()
        account = ("a" * 32, "unattributed", "b" * 32)[i % 3]
        orphaned = "2026-07-15T00:00:00Z" if i % 13 == 0 else None
        data.append((ROOT, reset, spelled, orphaned, account, float(i % 100)))
    conn.executemany(
        "INSERT INTO quota_window_blocks (source, source_root_key,"
        " logical_limit_key, observed_slot, window_minutes, limit_id,"
        " limit_name, resets_at_utc, nominal_start_at_utc,"
        " first_observed_at_utc, last_observed_at_utc, first_percent,"
        " current_percent, last_source_path, last_line_offset, generation,"
        " orphaned_at, account_key)"
        " VALUES ('codex',?,'codex','primary',300,'codex',NULL,?,?,"
        " '2026-07-14T12:00:00Z','2026-07-14T12:00:00Z',1.0,?,'/p.jsonl',1,"
        " 'g',?,?)",
        [(root, reset, start, percent, orphaned, account)
         for root, reset, start, orphaned, account, percent in data])
    conn.commit()


def _cycle():
    return types.SimpleNamespace(
        root=ROOT, account_key="a" * 32,
        start=dt.datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        end=dt.datetime(2026, 7, 21, 12, 0, tzinfo=UTC))


@pytest.mark.parametrize("temp_store", TEMP_STORES)
def test_a_large_cycle_loads_its_five_hour_rows_without_a_temp_structure(
    tmp_path, monkeypatch, temp_store,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_db"]()
    try:
        _seed_five_hour_blocks(conn, 3000)
    finally:
        conn.close()
    mh = importlib.import_module("_cctally_milestone_history")
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        conn = sqlite3.connect(_cctally_core.DB_PATH)
        try:
            for orphaned in (False, True):
                assert mh._codex_five_hour_rows(
                    conn, _cycle(), include_orphaned=orphaned)
        finally:
            conn.close()
    _bounded(recorder, "_cctally_milestone_history._codex_five_hour_rows",
             temp_store)


def test_the_five_hour_rows_match_todays_order(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_db"]()
    try:
        _seed_five_hour_blocks(conn, 400)
    finally:
        conn.close()
    mh = importlib.import_module("_cctally_milestone_history")
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        cyc = _cycle()
        for orphaned in (False, True):
            orphan_clause = "" if orphaned else "AND orphaned_at IS NULL "
            want = conn.execute(
                "SELECT source_root_key, logical_limit_key, observed_slot, "
                "       window_minutes, limit_id, limit_name, account_key, "
                "       resets_at_utc, nominal_start_at_utc, current_percent "
                "FROM quota_window_blocks "
                "WHERE source='codex' AND window_minutes=300 "
                f"{orphan_clause}"
                "AND source_root_key=? AND account_key IN (?,?) "
                "AND unixepoch(nominal_start_at_utc) < unixepoch(?) "
                "AND unixepoch(resets_at_utc) > unixepoch(?) "
                "ORDER BY unixepoch(nominal_start_at_utc) ASC",
                (cyc.root, cyc.account_key, "unattributed",
                 cyc.end.astimezone(UTC).isoformat(),
                 cyc.start.astimezone(UTC).isoformat())).fetchall()
            got = mh._codex_five_hour_rows(conn, cyc, include_orphaned=orphaned)
            assert list(got) == want, orphaned
            starts = [row[8] for row in want]
            assert len({s[:16] for s in starts}) < len(starts), (
                "non-vacuity: rows tie on one start second")
    finally:
        conn.close()


# ── Codex accounting: one large dirty rollout ────────────────────────────────

def _seed_codex_entries(conn, rows: int) -> None:
    data = []
    for i in range(rows):
        ts = (dt.datetime(2026, 7, 15, tzinfo=UTC)
              + dt.timedelta(seconds=i // 3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = "/dirty/rollout-a.jsonl" if i % 5 else "/dirty/rollout-b.jsonl"
        data.append((path, i, ts, f"s{i % 2}", "gpt-5", ROOT,
                     (None, "conv-b", "conv-a")[i % 3]))
    data.append(("/clean/rollout-c.jsonl", 0, "2026-07-15T00:00:00Z", "s",
                 "gpt-5", ROOT, "conv-a"))
    conn.executemany(
        "INSERT INTO codex_session_entries (source_path, line_offset,"
        " timestamp_utc, session_id, model, source_root_key,"
        " conversation_key) VALUES (?,?,?,?,?,?,?)", data)
    conn.commit()


QUALIFIED_START = dt.datetime(2026, 7, 1, tzinfo=UTC)
QUALIFIED_END = dt.datetime(2026, 8, 1, tzinfo=UTC)
DIRTY = ((ROOT, "/dirty/rollout-a.jsonl"), (ROOT, "/dirty/rollout-b.jsonl"))


@pytest.mark.parametrize("temp_store", TEMP_STORES)
def test_a_large_dirty_rollout_is_read_without_a_temp_structure(
    tmp_path, monkeypatch, temp_store,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_cache_db"]()
    try:
        _seed_codex_entries(conn, 3000)
    finally:
        conn.close()
    analytics = importlib.import_module("_cctally_source_analytics")
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            assert len(analytics._load_qualified_codex_rows(
                conn, QUALIFIED_START, QUALIFIED_END, DIRTY)) == 3000
        finally:
            conn.close()
    _bounded(recorder, "_cctally_source_analytics._load_qualified_codex_rows",
             temp_store)


def test_the_dirty_rollout_rows_match_todays_order(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_cache_db"]()
    try:
        _seed_codex_entries(conn, 300)
    finally:
        conn.close()
    analytics = importlib.import_module("_cctally_source_analytics")
    conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        for identities in (DIRTY, DIRTY[:1], ((ROOT, "/missing.jsonl"),), None):
            sql = (analytics._QUALIFIED_CODEX_ENTRIES_SQL if identities is None
                   else _legacy_qualified_path_sql(analytics, len(identities)))
            params = (QUALIFIED_START.isoformat(), QUALIFIED_END.isoformat(),
                      *(v for identity in (identities or ()) for v in identity))
            want = [tuple(row) for row in conn.execute(sql, params)]
            got = [tuple(row) for row in analytics._load_qualified_codex_rows(
                conn, QUALIFIED_START, QUALIFIED_END, identities)]
            assert got == want, identities
    finally:
        conn.close()


def _legacy_qualified_path_sql(analytics, identity_count: int) -> str:
    predicates = " OR ".join(
        "(entries.source_root_key = ? AND entries.source_path = ?)"
        for _ in range(identity_count))
    return analytics._QUALIFIED_CODEX_ENTRIES_SQL.replace(
        "INDEXED BY idx_codex_entries_ts_root_conversation",
        "INDEXED BY idx_codex_entries_root_path",
    ).replace(
        "     ORDER BY entries.timestamp_utc ASC",
        f"       AND ({predicates})\n     ORDER BY entries.timestamp_utc ASC",
    )
