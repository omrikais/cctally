"""Instrumentation + plumbing tests for the conversation-assembly phase tree
(issue #276, Session C / M5).

Two concerns, both timing-free (never assert wall-clock):
  * Task 1 — a bare in-process ``_assemble_session`` with tracing ON leaves
    ``current_root()`` named ``assemble`` with the eight ``assemble.*`` children
    (goes RED if the seams are removed — the non-vacuous instrumentation test).
  * Task 2 — a traced conversation request stashes an
    ``endpoint.conversation_detail`` tree onto ``/api/debug/backend``, and a
    ``/events`` request does NOT clobber it (Codex F3 carve-out).
  * Task 3 — the ``assembly`` fixture ladder builds deterministically and each
    rung's ``msg_count`` matches ``2 × turns``.

bin/ is on sys.path (conftest + the insert below), so ``import _lib_perf`` and
``import _lib_conversation_query`` resolve the SAME shared instances the kernel's
``_perf()`` loader returns — the phase tree the kernel writes is the tree these
tests read.
"""
import importlib.util
import pathlib
import sqlite3
import sys

import pytest

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import _cctally_db as _db          # noqa: E402
import _lib_conversation_query as cq   # noqa: E402
import _lib_perf as perf           # noqa: E402

# A real model id from CLAUDE_MODEL_PRICING so token-derived cost is non-zero.
_MODEL = "claude-opus-4-8"

_MSG_COLS = (
    "session_id", "uuid", "parent_uuid", "source_path", "byte_offset",
    "timestamp_utc", "entry_type", "text", "blocks_json", "model",
    "msg_id", "req_id", "cwd", "git_branch", "is_sidechain",
    "source_tool_use_id", "stop_reason", "attribution_skill",
    "attribution_plugin", "search_tool", "search_thinking",
)


def _msg(c, **kw):
    row = {k: kw.get(k) for k in _MSG_COLS}
    row["blocks_json"] = kw.get("blocks_json", "[]")
    row["text"] = kw.get("text", "")
    row["is_sidechain"] = kw.get("is_sidechain", 0)
    row["search_tool"] = kw.get("search_tool", "")
    row["search_thinking"] = kw.get("search_thinking", "")
    c.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain,source_tool_use_id,stop_reason,attribution_skill,"
        " attribution_plugin,search_tool,search_thinking)"
        " VALUES(:session_id,:uuid,:parent_uuid,:source_path,:byte_offset,"
        ":timestamp_utc,:entry_type,:text,:blocks_json,:model,:msg_id,:req_id,"
        ":cwd,:git_branch,:is_sidechain,:source_tool_use_id,:stop_reason,"
        ":attribution_skill,:attribution_plugin,:search_tool,:search_thinking)",
        row)


def _entry(c, *, source_path, line_offset, model, msg_id, req_id,
           inp=1000, out=500, cc=0, cr=0):
    c.execute(
        "INSERT OR IGNORE INTO session_entries "
        "(source_path,line_offset,timestamp_utc,model,msg_id,req_id,"
        " input_tokens,output_tokens,cache_create_tokens,cache_read_tokens)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (source_path, line_offset, "t", model, msg_id, req_id, inp, out, cc, cr))


def _seed_small_session(c, sid="s1", turns=3):
    """Direct-seed a small human+assistant session with cost rows, so a bare
    _assemble_session runs the WHOLE pipeline (all eight stages execute
    unconditionally — the tree carries every child even on a tiny session)."""
    prev = None
    for t in range(turns):
        hu = f"{sid}-h{t}"
        _msg(c, session_id=sid, uuid=hu, parent_uuid=prev, source_path="a.jsonl",
             byte_offset=2 * t, timestamp_utc=f"2026-06-01T00:{t:02d}:00Z",
             entry_type="human", text=f"benchmark prompt {t}",
             cwd="/bench/proj", git_branch="main")
        au = f"{sid}-a{t}"
        _msg(c, session_id=sid, uuid=au, parent_uuid=hu, source_path="a.jsonl",
             byte_offset=2 * t + 1, timestamp_utc=f"2026-06-01T00:{t:02d}:05Z",
             entry_type="assistant", text=f"benchmark reply {t}", model=_MODEL,
             msg_id=f"m{t}", req_id=f"r{t}")
        _entry(c, source_path="a.jsonl", line_offset=2 * t + 1, model=_MODEL,
               msg_id=f"m{t}", req_id=f"r{t}")
        prev = au


def _conn():
    c = sqlite3.connect(":memory:")
    _db._apply_cache_schema(c)
    return c


# ── Task 1: non-vacuous kernel instrumentation ────────────────────────────

_ASSEMBLE_CHILDREN = {
    "assemble.read", "assemble.dedup", "assemble.build", "assemble.correlate",
    "assemble.fold", "assemble.classify", "assemble.cost", "assemble.finalize",
}


def test_assemble_emits_phase_tree():
    c = _conn()
    _seed_small_session(c)
    perf.set_enabled(True)
    perf.reset_thread()
    try:
        asm = cq._assemble_session(c, "s1")
        assert asm is not None
        root = perf.current_root()
        assert root is not None and root.name == "assemble"
        names = {ch.name for ch in root.children}
        assert _ASSEMBLE_CHILDREN <= names, sorted(names)
        by_name = {ch.name: ch for ch in root.children}
        # read count = physical rows; cost meta carries the chunking legs.
        assert by_name["assemble.read"].count == 6      # 3 human + 3 assistant
        assert by_name["assemble.finalize"].count == len(asm["items"])
        assert by_name["assemble.cost"].meta["turn_keys"] == 3
        assert by_name["assemble.cost"].meta["cost_chunks"] == 1
    finally:
        perf.set_enabled(False)
        perf.reset_thread()


def test_assemble_no_tree_when_tracing_off():
    """With tracing off, a bare _assemble_session records NO root (near-noop)."""
    c = _conn()
    _seed_small_session(c)
    perf.set_enabled(False)
    perf.reset_thread()
    asm = cq._assemble_session(c, "s1")
    assert asm is not None
    assert perf.current_root() is None


def test_assemble_unknown_session_closes_cleanly():
    """The early return None (unknown session) must not strand a perf frame."""
    c = _conn()
    perf.set_enabled(True)
    perf.reset_thread()
    try:
        assert cq._assemble_session(c, "nope") is None
        root = perf.current_root()
        # `assemble` still closes (identity-aware __exit__ on the early return).
        assert root is not None and root.name == "assemble"
    finally:
        perf.set_enabled(False)
        perf.reset_thread()


# ── Task 2: /api/debug/backend plumbing (the dead-end fix) ─────────────────

def _find_node(tree, name):
    """Depth-first search for a phase node by name in a to_dict() tree."""
    if tree is None:
        return None
    if tree.get("name") == name:
        return tree
    for ch in tree.get("children", ()):
        hit = _find_node(ch, name)
        if hit is not None:
            return hit
    return None


def test_perf_scope_stashes_conversation_tree(tmp_path, monkeypatch):
    """A conversation request run under the handler's _perf_scope stashes an
    `endpoint.conversation_detail` tree onto last_backend_perf() — so
    /api/debug/backend surfaces the conversation trace, not just the snapshot
    (Session A's dead-end). And a subsequent plain (non-stashing) /events wrap
    does NOT clobber it (Codex F3 carve-out)."""
    from conftest import load_script, redirect_paths
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # Resolve the SHARED perf + kernel the handler uses (load_script does not
    # purge _lib_* siblings, so these are the same sys.modules instances the
    # handler's _perf_gate() / the kernel's _perf() resolve).
    dperf = ns["_load_sibling"]("_lib_perf")
    dcq = ns["_load_sibling"]("_lib_conversation_query")

    conn = ns["open_cache_db"]()
    _seed_small_session(conn)
    conn.commit()

    H = ns["DashboardHTTPHandler"]
    handler = H.__new__(H)     # _perf_scope only needs the staticmethod _perf_gate

    dperf.set_enabled(True)
    dperf.reset_thread()
    dperf._LAST_BACKEND_PERF = None
    try:
        with handler._perf_scope("endpoint.conversation_detail"):
            dcq.get_conversation(conn, "s1", tail=True, limit=50)
        last = dperf.last_backend_perf()
        assert last is not None, "the /events dead-end is unfixed — nothing stashed"
        assert last["phases"]["name"] == "endpoint.conversation_detail"
        # The assemble sub-tree nests under the endpoint root (end-to-end proof
        # that /api/debug/backend would surface the whole conversation trace).
        assert _find_node(last["phases"], "assemble") is not None
        assert _find_node(last["phases"], "conversation.detail") is not None

        # F3: a plain, non-stashing /events wrap must NOT overwrite the stash.
        dperf.reset_thread()
        with dperf.phase("endpoint.conversation_events"):
            pass
        still = dperf.last_backend_perf()
        assert still["phases"]["name"] == "endpoint.conversation_detail"
    finally:
        conn.close()
        dperf.set_enabled(False)
        dperf.reset_thread()
        dperf._LAST_BACKEND_PERF = None
