"""#302 Task 2 — the two title seams extracted from _session_titles_map.

``_session_titles_map`` is re-expressed as *AI title (truthy) overlaid on the
first-prompt title*, single-sourcing:
  * ``_session_ai_titles_map(conn, ids)``       -> {sid: ai_title} (truthy only;
    the ``if at:`` guard drops empty-string AI titles — Codex P1-3).
  * ``_session_first_prompt_titles_map(conn, ids)`` -> {sid: first-prompt title}
    (the expensive windowed conversation_messages scan; first-wins).

The combined map's public contract is UNCHANGED — this test pins parity and the
two preserved rules (empty-AI-title fallback + earliest-first-prompt-wins).

Seeds conversation_messages + conversation_ai_titles directly (the reconcile
test's idiom) via the load_script()/redirect_paths() loader so the seed lands in
a tmp-dir cache.db, never the real prod cache.db.
"""
import pathlib
import sys

from conftest import load_script, redirect_paths  # type: ignore


def _bin_on_path(ns):
    bin_dir = str(pathlib.Path(ns["__file__"]).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)


def _msg(conn, *, session_id, uuid, byte_offset, timestamp_utc, text,
         entry_type="human", is_sidechain=0):
    conn.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id, uuid, source_path, byte_offset, timestamp_utc, "
        " entry_type, text, blocks_json, is_sidechain) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, uuid, "seed.jsonl", byte_offset, timestamp_utc,
         entry_type, text, "[]", is_sidechain))


def _ai_title(conn, *, session_id, ai_title):
    conn.execute(
        "INSERT OR REPLACE INTO conversation_ai_titles "
        "(session_id, ai_title, source_path, byte_offset) VALUES (?,?,?,?)",
        (session_id, ai_title, "seed.jsonl", 0))


def _fixture(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    import _lib_conversation_query as lq
    conn = ns["open_cache_db"]()
    # sA: AI title + a first prompt (truthy AI wins in the combined map).
    _msg(conn, session_id="sA", uuid="a1", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", text="first prompt A")
    _ai_title(conn, session_id="sA", ai_title="AI Title A")
    # sB: EMPTY-string AI title + a first prompt (empty AI dropped -> falls back).
    _msg(conn, session_id="sB", uuid="b1", byte_offset=1,
         timestamp_utc="2026-06-02T00:00:00Z", text="first prompt B")
    _ai_title(conn, session_id="sB", ai_title="")
    # sC: two eligible human prompts (earliest wins).
    _msg(conn, session_id="sC", uuid="c1", byte_offset=2,
         timestamp_utc="2026-06-03T00:00:00Z", text="earliest C")
    _msg(conn, session_id="sC", uuid="c2", byte_offset=3,
         timestamp_utc="2026-06-03T01:00:00Z", text="later C")
    # sD: first-prompt only (no AI title row at all).
    _msg(conn, session_id="sD", uuid="d1", byte_offset=4,
         timestamp_utc="2026-06-04T00:00:00Z", text="first prompt D")
    conn.commit()
    return lq, conn, ["sA", "sB", "sC", "sD"]


def test_titles_map_parity_after_extraction(tmp_path, monkeypatch):
    lq, conn, ids = _fixture(tmp_path, monkeypatch)
    try:
        combined = lq._session_titles_map(conn, ids)
        ai = lq._session_ai_titles_map(conn, ids)
        fp = lq._session_first_prompt_titles_map(conn, ids)
        expected = {sid: (ai.get(sid) or fp.get(sid)) for sid in ids
                    if (ai.get(sid) or fp.get(sid))}
        assert combined == expected
        # And the truthy-AI-wins semantic is really exercised (not vacuous).
        assert combined["sA"] == "AI Title A"
    finally:
        conn.close()


def test_ai_titles_map_drops_empty_string(tmp_path, monkeypatch):
    lq, conn, ids = _fixture(tmp_path, monkeypatch)
    try:
        ai = lq._session_ai_titles_map(conn, ids)
        assert "sB" not in ai, "empty-string AI title must be dropped (if at:)"
        assert ai.get("sA") == "AI Title A"
        # The combined map falls back to sB's first-prompt title (not "").
        assert lq._session_titles_map(conn, ["sB"]).get("sB") == "first prompt B"
    finally:
        conn.close()


def test_first_prompt_map_earliest_wins(tmp_path, monkeypatch):
    lq, conn, ids = _fixture(tmp_path, monkeypatch)
    try:
        fp = lq._session_first_prompt_titles_map(conn, ids)
        assert fp.get("sC") == "earliest C", "first-wins guard broke"
        # AI-title sessions still resolve their first-prompt in THIS map (it is
        # AI-title-independent) — the overlay happens only in the combined map.
        assert fp.get("sA") == "first prompt A"
    finally:
        conn.close()


def test_ai_titles_map_tolerates_missing_table(tmp_path, monkeypatch):
    """No conversation_ai_titles table -> {} (not a raise)."""
    lq, conn, ids = _fixture(tmp_path, monkeypatch)
    try:
        conn.execute("DROP TABLE conversation_ai_titles")
        conn.commit()
        assert lq._session_ai_titles_map(conn, ids) == {}
        # And the combined map still returns first-prompt titles.
        assert lq._session_titles_map(conn, ["sA"]).get("sA") == "first prompt A"
    finally:
        conn.close()
