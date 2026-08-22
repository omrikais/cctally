"""#620 S3 — the four preparatory seams (spec §4.2, acceptance C16, C24).

X1, X2 and X3 are behaviour-preserving extractions, and each is pinned by
asserting **the wrapper's own output** rather than only that a downstream
golden did not move — a downstream golden can be insensitive to a change the
wrapper made. Every literal below was captured by running the wrapper against
this fixture store BEFORE the extraction and pasting the result in; none of
them is recomputed by calling the post-extraction code.

X4 replaces nothing existing and is therefore new code rather than an
extraction, so it has no before-and-after to pin.

Set `CCTALLY_S3_SEAM_CAPTURE=1` to have the capture test print the literals
instead of comparing them; that is how the values below were produced.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import importlib
import inspect
import json
import os
import sys
import textwrap

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
BASE = dt.datetime(2026, 8, 10, 1, 0, tzinfo=UTC)
SESSION = "sess-seam"
CLAUDE_PATH = "/tmp/projects/sess-seam.jsonl"
CODEX_PATH = "/tmp/codex/rollout-seam.jsonl"

COMPACTION_BODY = (
    "This session is being continued from a previous conversation that ran "
    "out of context. The conversation is summarized below:"
)


def _blocks(text):
    return json.dumps([{"kind": "text", "text": text}])


def _seed_claude_conversation(conv, cache):
    """A session with every shape the canonical assembly has to normalize.

    A human turn, an assistant turn split across two non-adjacent fragments
    with a tool_result between them, a compaction meta row that resets the
    running maximum, and a second assistant turn after it.
    """
    rows = [
        # (uuid, entry_type, text, blocks, model, msg_id, req_id, is_sidechain)
        ("u0", "human", "please do the thing", _blocks("please do the thing"),
         None, None, None, 0),
        ("u1", "assistant", "working on it", _blocks("working on it"),
         "claude-opus-4-20250514", "msg-1", "req-1", 0),
        ("u2", "tool_result", "tool output", _blocks("tool output"),
         None, None, None, 0),
        ("u3", "assistant", "done", _blocks("done"),
         "claude-opus-4-20250514", "msg-1", "req-1", 0),
        ("u4", "meta", COMPACTION_BODY, _blocks(COMPACTION_BODY),
         None, None, None, 0),
        ("u5", "human", "carry on", _blocks("carry on"),
         None, None, None, 0),
        ("u6", "assistant", "carrying on", _blocks("carrying on"),
         "claude-opus-4-20250514", "msg-2", "req-2", 0),
        # A sidechain row, which is never a main-thread human turn.
        ("u7", "human", "subagent prompt", _blocks("subagent prompt"),
         None, None, None, 1),
    ]
    for offset, (uuid, entry_type, text, blocks, model, msg_id, req_id,
                 sidechain) in enumerate(rows):
        conv.execute(
            "INSERT INTO conversation_messages "
            "(session_id, uuid, parent_uuid, source_path, byte_offset, "
            " timestamp_utc, entry_type, text, blocks_json, model, "
            " msg_id, req_id, cwd, git_branch, is_sidechain) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (SESSION, uuid, None, CLAUDE_PATH, offset,
             (BASE + dt.timedelta(minutes=offset)).isoformat(),
             entry_type, text, blocks, model, msg_id, req_id,
             "/repo/alpha", "main", sidechain),
        )
    conv.commit()

    # One deduped accounting row per turn key, with a cache profile that makes
    # the second turn a churn candidate under the shared predicate.
    entries = [
        ("msg-1", "req-1", 1000, 500, 5_000, 60_000),
        ("msg-2", "req-2", 1000, 500, 40_000, 100),
    ]
    for offset, (msg_id, req_id, inp, out, cc, cr) in enumerate(entries):
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, input_tokens, "
            " output_tokens, cache_create_tokens, cache_read_tokens, "
            " cache_create_1h_tokens, account_key, msg_id, req_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (CLAUDE_PATH, offset,
             (BASE + dt.timedelta(minutes=offset)).isoformat(),
             "claude-opus-4-20250514", inp, out, cc, cr, 0, "unattributed",
             msg_id, req_id),
        )
    cache.commit()


def _seed_codex_events(conv):
    """A rollout whose first turn anchor arrives LATE, so the backfill branch
    of the inference runs rather than only the forward one."""
    events = [
        # (record_type, event_type, turn_id, payload)
        ("session_meta", None, None, {"payload": {"type": "session_meta"}}),
        ("response_item", "agent_message", None,
         {"payload": {"type": "agent_message"}}),
        ("event_msg", "task_complete", "turn-a",
         {"payload": {"type": "task_complete"}}),
        ("turn_context", None, None,
         {"payload": {"type": "turn_context", "turn_id": "turn-b"}}),
        ("response_item", "agent_message", None,
         {"payload": {"type": "agent_message"}}),
    ]
    for offset, (record_type, event_type, turn_id, payload) in enumerate(events):
        conv.execute(
            "INSERT INTO codex_conversation_events "
            "(source_path, line_offset, source_root_key, conversation_key, "
            " native_thread_id, root_thread_id, parent_thread_id, "
            " timestamp_utc, record_type, event_type, turn_id, call_id, "
            " payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (CODEX_PATH, offset, "root-a", "v1.root-a.0", "thread-1", "user",
             None, (BASE + dt.timedelta(minutes=offset)).isoformat(),
             record_type, event_type, turn_id, None, json.dumps(payload)),
        )
    conv.commit()


@pytest.fixture
def seam_store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conv = ns["open_conversations_db"]()
    cache = ns["open_cache_db"]()
    try:
        _seed_claude_conversation(conv, cache)
        _seed_codex_events(conv)
    finally:
        conv.close()
        cache.close()
    return ns


def _conv_conn(ns):
    return ns["open_conversations_db"]()


def _query():
    return sys.modules["_lib_conversation_query"]


def _codex_query():
    return sys.modules["_lib_codex_conversation_query"]


def _codex_kernel():
    return sys.modules["_lib_codex_conversation"]


def _assembly_shape(asm):
    """A stable, readable summary of the whole assembled session."""
    return [
        {
            "kind": item.get("kind"),
            "metaKind": item.get("meta_kind"),
            "text": item.get("text"),
            "cost": item.get("cost_usd"),
            "tokens": item.get("tokens"),
            "cacheFailure": item.get("cache_failure"),
            "blocks": len(item.get("blocks") or []),
        }
        for item in asm["items"]
    ]


def _assembly_digest(asm):
    body = json.dumps(
        {"shape": _assembly_shape(asm),
         "headerCost": asm["header_cost"],
         "logical": len(asm["logical"]),
         "subagentMeta": sorted(asm["subagent_meta"])},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _source_without_docstring(fn):
    """The function's executable body, with its docstring removed.

    The claim is that X2 does not CALL X1, and a docstring cannot make or
    break a call — but this fold's docstring names `_assemble_session` in
    prose, so a raw source scan reads that prose as evidence of a call.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if ast.get_docstring(node):
        node.body = node.body[1:]
    return ast.unparse(node)


def _event_shape(events):
    return [
        (bool(ev.compaction), ev.key, ev.cc, ev.cr, ev.model, ev.speed)
        for ev in events
    ]


# --- the captured literals ----------------------------------------------
#
# Produced by running the capture test below against this fixture store on the
# PRE-EXTRACTION tree, at 26e935b4c.

EXPECTED_ASSEMBLY_DIGEST = (
    "5aeddb21b8229042e87ca5cce64751efa2a9e3b16c5c9cc8517bff125e62040d"
)

EXPECTED_ASSEMBLY_SHAPE = [
    {"kind": "human", "metaKind": None, "text": "please do the thing",
     "cost": None, "tokens": None, "cacheFailure": None, "blocks": 1},
    # The two non-adjacent fragments of one turn folded into ONE item at the
    # first fragment's position, priced once.
    {"kind": "assistant", "metaKind": None, "text": "working on it\ndone",
     "cost": 0.23625,
     "tokens": {"input": 1000, "output": 500, "cache_creation": 5000,
                "cache_read": 60000},
     "cacheFailure": None, "blocks": 2},
    {"kind": "tool_result", "metaKind": None, "text": "tool output",
     "cost": None, "tokens": None, "cacheFailure": None, "blocks": 1},
    {"kind": "meta", "metaKind": "compaction", "text": COMPACTION_BODY,
     "cost": None, "tokens": None, "cacheFailure": None, "blocks": 1},
    {"kind": "human", "metaKind": None, "text": "carry on",
     "cost": None, "tokens": None, "cacheFailure": None, "blocks": 1},
    # No `cacheFailure`: the compaction above RESET the running maximum, so
    # the 40k re-create that follows it is a legitimate re-prime.
    {"kind": "assistant", "metaKind": None, "text": "carrying on",
     "cost": 0.80265,
     "tokens": {"input": 1000, "output": 500, "cache_creation": 40000,
                "cache_read": 100},
     "cacheFailure": None, "blocks": 1},
    {"kind": "human", "metaKind": None, "text": "subagent prompt",
     "cost": None, "tokens": None, "cacheFailure": None, "blocks": 1},
]

EXPECTED_CACHE_FAILURE_EVENTS = [
    (False, (None, "claude-opus-4-20250514"), 5000, 60000,
     "claude-opus-4-20250514", None),
    (True, None, 0, 0, None, None),
    (False, (None, "claude-opus-4-20250514"), 40000, 100,
     "claude-opus-4-20250514", None),
]

EXPECTED_CODEX_TURN_MAP = {0: None, 1: "turn-a", 2: "turn-a", 3: "turn-b",
                           4: "turn-b"}


def test_capture_or_compare_the_wrapper_outputs(seam_store):
    """The one test that pins all three wrappers.

    With `CCTALLY_S3_SEAM_CAPTURE=1` it PRINTS the literals rather than
    comparing them, which is how the values above were produced against the
    pre-extraction tree.
    """
    query = _query()
    codex_query = _codex_query()
    conn = _conv_conn(seam_store)
    try:
        asm = query._assemble_session(conn, SESSION)
        events = query._lightweight_rebuild_events(conn, SESSION)
        turn_map = codex_query._file_turn_map(conn, CODEX_PATH)
    finally:
        conn.close()

    shape = _assembly_shape(asm)
    digest = _assembly_digest(asm)
    event_shape = _event_shape(events)

    if os.environ.get("CCTALLY_S3_SEAM_CAPTURE") == "1":
        print("\nEXPECTED_ASSEMBLY_DIGEST = (\n    %r\n)" % digest)
        print("\nEXPECTED_ASSEMBLY_SHAPE = %s" % json.dumps(shape, indent=4))
        print("\nEXPECTED_CACHE_FAILURE_EVENTS = %r" % (event_shape,))
        print("\nEXPECTED_CODEX_TURN_MAP = %r" % (turn_map,))
        pytest.skip("capture mode: literals printed above")

    assert shape == EXPECTED_ASSEMBLY_SHAPE
    assert digest == EXPECTED_ASSEMBLY_DIGEST
    assert event_shape == EXPECTED_CACHE_FAILURE_EVENTS
    assert turn_map == EXPECTED_CODEX_TURN_MAP


# --- the folds themselves -----------------------------------------------

def test_x1_fold_reproduces_the_wrapper_over_the_same_prefetched_facts(
        seam_store):
    """X1 takes THREE arguments. `_assemble_session` also consumes a
    separately priced cost map, so a fold taking only rows and tokens cannot
    reproduce it.

    Compared against the PRE-EXTRACTION literals, never against the
    post-extraction wrapper: the wrapper delegates to this very fold, so
    comparing the two asserts that a function equals itself.
    """
    query = _query()
    conn = _conv_conn(seam_store)
    try:
        rows = query._claude_canonical_rows(conn, SESSION)
        keys = query._claude_turn_keys(rows)
        folded = query.fold_claude_canonical(
            rows, query._turn_usage_map(conn, keys),
            query._turn_cost_map(conn, keys),
            allow_human_fallback=query._reingest_pending(conn),
        )
    finally:
        conn.close()
    assert _assembly_shape(folded) == EXPECTED_ASSEMBLY_SHAPE
    assert _assembly_digest(folded) == EXPECTED_ASSEMBLY_DIGEST


def test_x1_fold_opens_its_own_assemble_root_when_called_directly(seam_store):
    """The fold opens CHILD phases, and the diagnosis calls it directly. With
    no root open those children would be emitted into whatever phase happened
    to be on the stack (#620 S3 R8)."""
    perf = importlib.import_module("_lib_perf")
    query = _query()
    conn = _conv_conn(seam_store)
    perf.set_enabled(True)
    perf.reset_thread()
    try:
        rows = query._claude_canonical_rows(conn, SESSION)
        keys = query._claude_turn_keys(rows)
        query.fold_claude_canonical(
            rows, query._turn_usage_map(conn, keys),
            query._turn_cost_map(conn, keys))
        root = perf.current_root()
    finally:
        perf.set_enabled(False)
        perf.reset_thread()
        conn.close()
    assert root is not None and root.name == "assemble"
    assert "assemble.dedup" in {child.name for child in root.children}


def test_the_hoisted_cost_reads_are_still_inside_a_phase(seam_store):
    """Hoisting `_turn_usage_map` and `_turn_cost_map` out of the fold moved
    those chunked `session_entries` reads outside every child phase, so their
    database time was reattributed to the untraced remainder of the root
    (#620 S3 R5)."""
    perf = importlib.import_module("_lib_perf")
    query = _query()
    conn = _conv_conn(seam_store)
    perf.set_enabled(True)
    perf.reset_thread()
    try:
        query._assemble_session(conn, SESSION)
        root = perf.current_root()
    finally:
        perf.set_enabled(False)
        perf.reset_thread()
        conn.close()
    by_name = {child.name: child for child in root.children}
    assert "assemble.cost_read" in by_name
    assert by_name["assemble.cost_read"].meta["turn_keys"] == 2
    assert by_name["assemble.cost_read"].meta["cost_chunks"] == 1
    assert by_name["assemble.cost_read"].meta["usage_chunks"] == 1


def test_x2_is_a_separate_and_cheaper_fold_than_x1(seam_store):
    """Kept separate on purpose: the cache-churn normalization is deliberately
    cheaper than canonical assembly and must stay so, so X2 neither calls X1
    nor is expressed in terms of it.

    Read from the SOURCE, not the docstring — a docstring cannot detect a
    call — and compared against the pre-extraction literals rather than
    against the wrapper that now delegates here.
    """
    query = _query()
    source = _source_without_docstring(query.fold_claude_cache_failure_events)
    assert "fold_claude_canonical" not in source
    assert "_assemble_session" not in source
    conn = _conv_conn(seam_store)
    try:
        rows = query._cache_failure_rows(conn, SESSION)
        keys = query._cache_failure_turn_keys(rows)
        folded = query.fold_claude_cache_failure_events(
            rows, query._turn_usage_map(conn, keys))
    finally:
        conn.close()
    assert _event_shape(folded) == EXPECTED_CACHE_FAILURE_EVENTS


def test_x2_can_return_the_turn_key_behind_each_emitted_event(seam_store):
    """`_iter_cache_failures` yields the flagged event's INDEX, so without a
    parallel source list nothing can say WHICH turn was flagged. Opt-in, so
    every existing caller keeps the bare list it always received."""
    query = _query()
    conn = _conv_conn(seam_store)
    try:
        rows = query._cache_failure_rows(conn, SESSION)
        keys = query._cache_failure_turn_keys(rows)
        usage = query._turn_usage_map(conn, keys)
        bare = query.fold_claude_cache_failure_events(rows, usage)
        events, sources = query.fold_claude_cache_failure_events(
            rows, usage, with_sources=True)
    finally:
        conn.close()
    assert _event_shape(events) == _event_shape(bare)
    assert sources == [("msg-1", "req-1"), None, ("msg-2", "req-2")]


def test_one_compaction_predicate_answers_for_every_caller():
    """`is_compaction_row` is the single read-time authority, and the S3
    adapter routes through it rather than restating it a third time
    (#620 S3 R4)."""
    query = _query()
    sources = importlib.import_module("_cctally_diagnosis_sources")
    cases = [
        ("", _blocks(COMPACTION_BODY), True),
        ("", _blocks("an ordinary reply"), False),
        (COMPACTION_BODY, "[]", True),
        # A malformed blocks array is rejected before the stricter command
        # invocation consumer reaches it.
        ("", '["a bare string"]', False),
        ("", "not json at all", False),
    ]
    for text, blocks_json, expected in cases:
        assert query.is_compaction_row(text, blocks_json) is expected
        assert sources._is_compaction_row(text, blocks_json) is expected


def test_x3_maps_physical_offsets_to_turns_over_prefetched_events(seam_store):
    codex_query = _codex_query()
    kernel = _codex_kernel()
    conn = _conv_conn(seam_store)
    try:
        events = codex_query._codex_file_events(conn, CODEX_PATH)
        folded = kernel.fold_codex_event_turns(events)
    finally:
        conn.close()
    # The pre-extraction literal, not the wrapper that now delegates here.
    assert folded == EXPECTED_CODEX_TURN_MAP


def test_x3_backfills_only_the_unanchored_prefix_since_the_last_session_meta(
        seam_store):
    """A resumed segment can expose its first native proof on a later
    task-complete record, and only the prefix since the latest `session_meta`
    is backfilled."""
    codex_query = _codex_query()
    conn = _conv_conn(seam_store)
    try:
        turn_map = codex_query._file_turn_map(conn, CODEX_PATH)
    finally:
        conn.close()
    assert turn_map[0] is None          # the session_meta row itself
    assert turn_map[1] == "turn-a"      # backfilled by the late anchor
    assert turn_map[2] == "turn-a"
    assert turn_map[3] == "turn-b"
    assert turn_map[4] == "turn-b"


# --- X4: new code, not an extraction ------------------------------------

def test_x4_resolves_a_parent_only_on_exactly_one_non_self_match():
    """The table's uniqueness is `(source_root_key, root_thread_id,
    native_thread_id)`, not the pair. Existing resolution queries the weaker
    pair and takes an arbitrary `fetchone()`, which S3 must not copy."""
    sources = sys.modules.get("_cctally_diagnosis_sources")
    if sources is None:
        import importlib
        sources = importlib.import_module("_cctally_diagnosis_sources")
    threads = [
        {"source_root_key": "root-a", "root_thread_id": "user",
         "native_thread_id": "parent-1", "conversation_key": "v1.root-a.0",
         "parent_thread_id": None},
        {"source_root_key": "root-a", "root_thread_id": "subagent",
         "native_thread_id": "child-1", "conversation_key": "v1.root-a.1",
         "parent_thread_id": "parent-1"},
    ]
    resolved = sources.resolve_codex_fanout(threads, ["v1.root-a.1"])
    assert resolved.parents == {"v1.root-a.1": ("root-a", "parent-1")}
    assert resolved.unallocated == ()


def test_x4_leaves_a_child_unallocated_when_several_parents_match():
    sources = sys.modules["_cctally_diagnosis_sources"]
    threads = [
        {"source_root_key": "root-a", "root_thread_id": "user",
         "native_thread_id": "parent-1", "conversation_key": "v1.root-a.0",
         "parent_thread_id": None},
        {"source_root_key": "root-a", "root_thread_id": "other",
         "native_thread_id": "parent-1", "conversation_key": "v1.root-a.2",
         "parent_thread_id": None},
        {"source_root_key": "root-a", "root_thread_id": "subagent",
         "native_thread_id": "child-1", "conversation_key": "v1.root-a.1",
         "parent_thread_id": "parent-1"},
    ]
    resolved = sources.resolve_codex_fanout(threads, ["v1.root-a.1"])
    assert resolved.parents == {}
    assert resolved.unallocated == ("v1.root-a.1",)


def test_x4_never_resolves_a_child_to_itself():
    sources = sys.modules["_cctally_diagnosis_sources"]
    threads = [
        {"source_root_key": "root-a", "root_thread_id": "subagent",
         "native_thread_id": "child-1", "conversation_key": "v1.root-a.1",
         "parent_thread_id": "child-1"},
    ]
    resolved = sources.resolve_codex_fanout(threads, ["v1.root-a.1"])
    assert resolved.parents == {}
    assert resolved.unallocated == ("v1.root-a.1",)


def test_x4_is_seeded_from_the_scoped_accounting_keys_only():
    """Parent and child resolution is seeded from the scoped in-window
    accounting keys, never by walking the retained thread graph."""
    sources = sys.modules["_cctally_diagnosis_sources"]
    threads = [
        {"source_root_key": "root-a", "root_thread_id": "user",
         "native_thread_id": "parent-1", "conversation_key": "v1.root-a.0",
         "parent_thread_id": None},
        {"source_root_key": "root-a", "root_thread_id": "subagent",
         "native_thread_id": "child-1", "conversation_key": "v1.root-a.1",
         "parent_thread_id": "parent-1"},
        {"source_root_key": "root-a", "root_thread_id": "subagent",
         "native_thread_id": "child-2", "conversation_key": "v1.root-a.9",
         "parent_thread_id": "parent-1"},
    ]
    resolved = sources.resolve_codex_fanout(threads, ["v1.root-a.1"])
    assert set(resolved.parents) == {"v1.root-a.1"}


def test_x4_only_the_two_exact_literals_are_recognised_origin_categories():
    """`_inferred_codex_thread_source` returns any explicit `thread_source`
    verbatim, and no resolver maps an arbitrary string onto an origin. Every
    other value is ambiguous and belongs to neither population."""
    sources = sys.modules["_cctally_diagnosis_sources"]
    assert sources.codex_origin_category("user") == "user"
    assert sources.codex_origin_category("subagent") == "subagent"
    for value in ("vscode", "User", "SUBAGENT", "", None, "cli"):
        assert sources.codex_origin_category(value) is None
