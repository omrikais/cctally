"""#929 S1 A7: conversation-viewer and `explain` cache estimates use the
request's real prompt to select the Claude Haiku 5.5 card.

Both display-only estimates price a SYNTHETIC subset of a real request (the
read prefix as input vs. read; the lost prefix as 5m write vs. read). Every
subset below stays under 100,000 tokens while its SOURCE request's prompt
exceeds it, so a helper that let the subset select its own card would report
the base-card figures this module rejects (0.0081 / 0.0092). Expected values
are independent vendor arithmetic.

One Haiku 5.5 session, two turns:
  turn 1: input 20,000 / cache read 90,000                   (prompt 110,000)
  turn 2: input 20,000 / cache creation 90,000 / read 10,000  (prompt 120,000)
Turn 2 is a cache failure (running max 90,000, read 10,000 <= 45,000,
creation share 0.9) that loses min(90,000, 90,000 - 10,000) = 80,000 tokens.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import pathlib
import sqlite3
import sys

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _cctally_db as db  # noqa: E402
import _lib_conversation_query as cq  # noqa: E402
import _lib_diagnosis as kernel  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402

HAIKU = "claude-haiku-5-5"
# HIGH card: input 5e-07, 5m write 6.25e-07, read 5e-08.
TURN1_SAVED = 90_000 * (5e-07 - 5e-08)              # 0.0405
TURN2_SAVED = 10_000 * (5e-07 - 5e-08)              # 0.0045
TURN2_WASTED = 80_000 * 6.25e-07 - 80_000 * 5e-08   # 0.046
WRONG_SAVED = 90_000 * (1e-07 - 1e-08)              # 0.0081 (base card)
WRONG_WASTED = 80_000 * 1.25e-07 - 80_000 * 1e-08   # 0.0092 (base card)

TURNS = (
    # (msg_id, req_id, input, cache_creation, cache_read)
    ("m1", "r1", 20_000, 0, 90_000),
    ("m2", "r2", 20_000, 90_000, 10_000),
)


def _close(a, b):
    return abs(a - b) < 1e-12


def test_direct_helpers_select_from_the_source_prompt():
    saved = cq._cache_read_saved_usd(HAIKU, 90_000, speed=None,
                                     prompt_tokens=110_000)
    wasted = cq._cache_failure_wasted_usd(HAIKU, 80_000, speed=None,
                                          prompt_tokens=120_000)
    assert _close(saved, 0.0405) and _close(saved, TURN1_SAVED)
    assert _close(wasted, 0.046) and _close(wasted, TURN2_WASTED)
    assert not _close(saved, WRONG_SAVED)
    assert not _close(wasted, WRONG_WASTED)
    # A source prompt at the threshold keeps the base card.
    assert _close(cq._cache_read_saved_usd(HAIKU, 90_000, speed=None,
                                           prompt_tokens=100_000), WRONG_SAVED)


# --- conversation viewer (outline + failure stamp) ------------------------

def _conn():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    return c


def _seed_outline(c, sid="haiku-sess"):
    for offset, (msg_id, req_id, inp, cc, cr) in enumerate(TURNS):
        uuid = f"a{offset + 1}"
        c.execute(
            "INSERT INTO conversation_messages "
            "(session_id,uuid,parent_uuid,source_path,byte_offset,"
            " timestamp_utc,entry_type,text,blocks_json,model,msg_id,req_id,"
            " cwd,git_branch,is_sidechain)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, uuid, None, "a.jsonl", offset,
             f"2026-10-07T00:00:{offset * 5:02d}Z", "assistant", uuid,
             json.dumps([{"kind": "text", "text": uuid}]), HAIKU, msg_id,
             req_id, None, None, 0))
        c.execute(
            "INSERT INTO session_entries "
            "(source_path,line_offset,timestamp_utc,model,msg_id,req_id,"
            " input_tokens,output_tokens,cache_create_tokens,"
            " cache_read_tokens,cost_usd_raw,speed)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a.jsonl", offset, "t", HAIKU, msg_id, req_id, inp, 0, cc, cr,
             None, None))
    return sid


def test_outline_and_failure_stamp_use_the_source_prompt():
    with contextlib.closing(_conn()) as c:
        sid = _seed_outline(c)
        out = cq.get_conversation_outline(c, sid)
        by = {t["uuid"]: t for t in out["turns"]}

        cf = by["a2"]["cache_failure"]
        assert cf["tokens_recreated"] == 80_000
        assert _close(cf["est_wasted_usd"], 0.046), cf["est_wasted_usd"]
        assert "cache_failure" not in by["a1"]

        stats = out["stats"]
        assert _close(stats["cache_saved_usd"], 0.045), stats["cache_saved_usd"]
        assert _close(stats["cache_saved_usd"], TURN1_SAVED + TURN2_SAVED)
        agg = stats["cache_failures"]
        assert _close(agg["est_wasted_usd"], 0.046)

        # The public token object is unchanged: no prompt or speed key leaks.
        for uuid in ("a1", "a2"):
            assert set(by[uuid]["tokens"]) == {
                "input", "output", "cache_creation", "cache_read"}

        # The full reader assembly stamps the same figure.
        detail = cq.get_conversation(c, sid, limit=1000)
        fails = [it["cache_failure"] for it in detail["items"]
                 if "cache_failure" in it]
        assert len(fails) == 1
        assert _close(fails[0]["est_wasted_usd"], 0.046)


# --- explain (prompt-cache churn evaluator) -------------------------------

UTC = dt.timezone.utc
WINDOW_START = dt.datetime(2026, 10, 1, tzinfo=UTC)
WINDOW_END = dt.datetime(2026, 10, 8, tzinfo=UTC)
SESSION_PATH = "/tmp/projects/sess-haiku.jsonl"


def _sources():
    import importlib
    module = sys.modules.get("_cctally_diagnosis_sources")
    if module is None:
        module = importlib.import_module("_cctally_diagnosis_sources")
    return module


def _seed_explain(ns):
    for opener in ("open_db", "open_cache_db"):
        ns[opener]().close()
    conv = ns["open_conversations_db"]()
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "INSERT OR IGNORE INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at,"
            " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            (SESSION_PATH, 0, 0, 0, "2026-10-01T00:00:00Z", "sess-haiku",
             "/repo/alpha"))
        for offset, (msg_id, req_id, inp, cc, cr) in enumerate(TURNS):
            at = WINDOW_START + dt.timedelta(hours=1 + offset)
            conv.execute(
                "INSERT INTO conversation_messages "
                "(session_id, uuid, parent_uuid, source_path, byte_offset, "
                " timestamp_utc, entry_type, text, blocks_json, model, "
                " msg_id, req_id, cwd, git_branch, is_sidechain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("sess-haiku", f"sess-haiku-{offset}", None, SESSION_PATH,
                 offset, at.isoformat(), "assistant", f"turn {offset}",
                 json.dumps([{"kind": "text", "text": f"turn {offset}"}]),
                 HAIKU, msg_id, req_id, "/repo/alpha", "main", 0))
            cache.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cache_create_1h_tokens, account_key, "
                " msg_id, req_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (SESSION_PATH, offset, at.isoformat(), HAIKU, inp, 0, cc, cr,
                 0, "unattributed", msg_id, req_id))
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()


def test_explain_cache_churn_wasted_evidence_uses_the_source_prompt(
        tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_explain(ns)
    sources = _sources()
    scope = sources.DiagnosisScope(
        source="claude", account_key=None, window_start=WINDOW_START,
        window_end=WINDOW_END, effective_speed=None, display_tz="UTC")
    plan = kernel.resolve_policy_plan("claude", transcripts_visible=True)
    bundle = sources.StoreBundle(scope, plan)
    try:
        sources._establish(scope, bundle)
        facts = sources.load_class_facts(
            bundle, scope, kernel.spec_for("cache_churn"))
    finally:
        bundle.close()
    assert facts.evidence["flaggedTurnCount"].value == 1
    wasted = facts.evidence["estWastedUsd"].value
    assert _close(wasted, 0.046), wasted
    assert not _close(wasted, WRONG_WASTED)
