"""Claude ingest attribution (#341 Task 3 slice C, spec §1/§2).

Observe-and-stamp for the Claude entry cache: ``sync_cache`` resolves the active
Claude identity ONCE per sync from ``~/.claude.json`` (stable-read, mtime-cached)
and stamps ``account_key`` onto newly-ingested ``session_entries`` rows (and the
``session_files`` last-observed diagnostic). A torn read (mid-rewrite) DEFERS the
whole Claude tail-ingest this cycle WITHOUT advancing any per-file cursor; a
missing/api-key ``~/.claude.json`` stamps NULL (``NULL ≡ unattributed`` on the
read path — byte-stable for the no-identity test corpus).

The Claude account_observe is journaled by ``record-usage`` (not ``sync_cache``),
so ``sync_cache`` owns the cache-row stamp only — no journal double-stamp.
"""
from __future__ import annotations

import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402
import _lib_accounts as accts  # noqa: E402

RED_UUID = "11111111-1111-4111-8111-111111111111"
BLUE_UUID = "22222222-2222-4222-8222-222222222222"


def _claude_json(uuid: str, email: str) -> str:
    return json.dumps({
        "oauthAccount": {
            "accountUuid": uuid,
            "emailAddress": email,
            "organizationUuid": "org-1",
            "plan": "max",
        },
    })


def _expected_key(uuid: str) -> str:
    return accts.account_key("claude", uuid)


def _write_session_jsonl(path: pathlib.Path, *, session_id: str, msg_id: str,
                         out_tokens: int = 100) -> None:
    """One assistant message that yields exactly one session_entries row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "type": "assistant",
        "sessionId": session_id,
        "cwd": "/home/u/proj",
        "timestamp": "2026-05-22T17:04:00.000Z",
        "requestId": "req-" + msg_id,
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 10,
                "output_tokens": out_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    path.write_text(json.dumps(obj) + "\n", encoding="utf-8")


def _reset_identity_cache():
    import _cctally_core as core
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.clear()
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update({"sig": None, "identity": None})


def _stub_discovery(monkeypatch, files):
    import _cctally_cache as cc
    monkeypatch.setattr(cc, "_iter_claude_jsonl_files", lambda: list(files))


def test_identified_stamps_real_account_key(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _reset_identity_cache()
    import _cctally_core as core
    core.CLAUDE_JSON_PATH.write_text(_claude_json(RED_UUID, "red@x.com"))
    jsonl = tmp_path / "projects" / "proj" / "s1.jsonl"
    _write_session_jsonl(jsonl, session_id="sess-red", msg_id="m-red")
    _stub_discovery(monkeypatch, [jsonl])
    expected = _expected_key(RED_UUID)

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_cache"](cache)
        assert stats.files_processed == 1
        rows = cache.execute(
            "SELECT account_key FROM session_entries").fetchall()
        assert rows, "expected at least one accounting row"
        assert all(r[0] == expected for r in rows), (
            f"session_entries should stamp {expected!r}, got {rows!r}")
        frow = cache.execute(
            "SELECT account_key FROM session_files WHERE path=?",
            (str(jsonl),)).fetchone()
        assert frow[0] == expected, (
            f"session_files last-observed stamp {expected!r}, got {frow!r}")
    finally:
        cache.close()


def test_missing_claude_json_stamps_null(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _reset_identity_cache()
    # No ~/.claude.json -> stably-absent -> NULL (NULL ≡ unattributed). Byte-
    # stable for the no-identity corpus.
    jsonl = tmp_path / "projects" / "proj" / "s1.jsonl"
    _write_session_jsonl(jsonl, session_id="sess-x", msg_id="m-x")
    _stub_discovery(monkeypatch, [jsonl])

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_cache"](cache)
        assert stats.files_processed == 1
        rows = cache.execute(
            "SELECT account_key FROM session_entries").fetchall()
        assert rows
        assert all(r[0] is None for r in rows), (
            f"stably-absent identity must stamp NULL, got {rows!r}")
    finally:
        cache.close()


def test_torn_claude_json_defers_without_advancing_cursor(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _reset_identity_cache()
    import _cctally_core as core
    core.CLAUDE_JSON_PATH.write_text(_claude_json(RED_UUID, "red@x.com"))
    jsonl = tmp_path / "projects" / "proj" / "s1.jsonl"
    _write_session_jsonl(jsonl, session_id="sess-red", msg_id="m-red")
    _stub_discovery(monkeypatch, [jsonl])

    real = core._resolve_active_claude_identity
    calls = {"n": 0}

    def flip():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"account_key": accts.UNATTRIBUTED, "natural_id": None,
                    "email": None, "plan_type": None, "status": "torn"}
        return real()

    monkeypatch.setattr(core, "_resolve_active_claude_identity", flip)

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_cache"](cache)
        # Torn defers the whole Claude tail-ingest: nothing ingested.
        assert stats.files_deferred_torn == 1
        assert stats.files_processed == 0
        assert cache.execute(
            "SELECT COUNT(*) FROM session_entries").fetchone() == (0,)
        # Cursor NOT advanced: no session_files row for this file.
        assert cache.execute(
            "SELECT COUNT(*) FROM session_files WHERE path=?",
            (str(jsonl),)).fetchone() == (0,)

        # Next sync (identity now stably readable) ingests the deferred file
        # and stamps the real account.
        _reset_identity_cache()
        stats2 = ns["sync_cache"](cache)
        assert stats2.files_processed == 1
        stamped = cache.execute(
            "SELECT DISTINCT account_key FROM session_entries").fetchall()
        assert stamped == [(_expected_key(RED_UUID),)], (
            f"deferred file must ingest+stamp on the next sync, got {stamped!r}")
    finally:
        cache.close()
