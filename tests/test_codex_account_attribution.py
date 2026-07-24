"""Codex ingest attribution (#341 Task 2, spec §1/§2).

Observe-and-stamp per provider root: ``sync_codex_cache`` reads each root's own
``auth.json`` (stable-read) and stamps ``account_key`` onto the new
``codex_session_entries`` / ``quota_window_snapshots`` rows and the journal quota
obs. A torn read defers the file WITHOUT advancing its cursor; a missing/api-key
auth stamps NULL (``NULL ≡ unattributed`` on the read path); a first-sight
account is journaled as an ``account_observe`` before any stamped row.
"""
from __future__ import annotations

import base64
import json
import pathlib
import shutil
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402
import _lib_accounts as accts  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"


def _b64(obj) -> str:
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _id_token(account_id: str, email: str) -> str:
    payload = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
        },
    }
    return f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(payload)}.sig"


def _auth_json(account_id: str, email: str) -> str:
    return json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _id_token(account_id, email),
            "access_token": "a", "refresh_token": "r",
        },
        "last_refresh": "2026-07-20T00:00:00Z",
    })


def _expected_key(account_id: str, email: str) -> str:
    return accts.account_key("codex", account_id + "\0" + email)


def _setup_root(tmp_path, rollout_name="modern-full.jsonl"):
    provider_root = tmp_path / "codex-provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / rollout_name, rollout)
    return provider_root, rollout


def _journal_records(ns, *, live_only=False):
    """Return journal records in append order.

    ``live_only`` restricts to the live ``observations-*`` segments — excludes
    the ``bootstrap-*`` cutover segment, which re-exports already-committed
    cache rows (a re-materialization, not a live causal append), so an ordering
    assertion sees only the live-append stream.
    """
    journal_dir = ns["_cctally_core"].JOURNAL_DIR
    recs = []
    for seg in sorted(journal_dir.glob("*.jsonl")):
        if live_only and not seg.name.startswith("observations-"):
            continue
        for line in seg.read_text().splitlines():
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def test_identified_root_stamps_account_key(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root, _rollout = _setup_root(tmp_path)
    (provider_root / "auth.json").write_text(_auth_json("acct-red", "red@x.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    expected = _expected_key("acct-red", "red@x.com")

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
        assert stats.files_processed == 1
        rows = cache.execute(
            "SELECT account_key FROM codex_session_entries").fetchall()
        assert rows, "expected at least one accounting row"
        assert all(r[0] == expected for r in rows)
        frow = cache.execute(
            "SELECT account_key FROM codex_session_files").fetchone()
        assert frow[0] == expected
    finally:
        cache.close()


def test_missing_auth_stamps_null(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root, _rollout = _setup_root(tmp_path)
    # No auth.json -> stably-absent -> NULL (NULL ≡ unattributed).
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
        assert stats.files_processed == 1
        rows = cache.execute(
            "SELECT account_key FROM codex_session_entries").fetchall()
        assert rows
        assert all(r[0] is None for r in rows)
    finally:
        cache.close()


def test_torn_auth_defers_file_without_advancing_cursor(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root, rollout = _setup_root(tmp_path)
    (provider_root / "auth.json").write_text(_auth_json("acct-red", "red@x.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    import _cctally_cache as cc
    real = cc._resolve_codex_account_for_root
    calls = {"n": 0}

    def flip(provider_root_arg):
        calls["n"] += 1
        if calls["n"] == 1:
            return cc._CodexRootAccount("torn", None)
        return real(provider_root_arg)

    monkeypatch.setattr(cc, "_resolve_codex_account_for_root", flip)

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
        assert stats.files_deferred_torn == 1
        assert stats.files_processed == 0
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone() == (0,)
        # Cursor NOT advanced: no codex_session_files row for this rollout.
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_files WHERE path=?",
            (str(rollout),)).fetchone() == (0,)

        # The next sync (auth now stably readable) ingests the deferred file.
        stats2 = ns["sync_codex_cache"](cache)
        assert stats2.files_processed == 1
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] >= 1
    finally:
        cache.close()


def test_account_observe_precedes_stamped_quota_obs(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root, _rollout = _setup_root(tmp_path, "modern-quota-payload.jsonl")
    (provider_root / "auth.json").write_text(_auth_json("acct-red", "red@x.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    expected = _expected_key("acct-red", "red@x.com")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        # Quota obs materialized with the account.
        acc_rows = cache.execute(
            "SELECT DISTINCT account_key FROM quota_window_snapshots").fetchall()
        assert acc_rows and all(r[0] == expected for r in acc_rows)
    finally:
        cache.close()

    # Live-append ordering only (the bootstrap segment re-exports the already-
    # committed cache rows at cutover — a re-materialization, not a causal live
    # append). The spec invariant governs the LIVE stream: the observe is
    # journaled before any cache row that carries the account commits.
    recs = _journal_records(ns, live_only=True)
    observe_idx = next(
        (i for i, r in enumerate(recs)
         if r.get("t") == "op"
         and (r.get("payload") or {}).get("kind") == "account_observe"
         and (r.get("payload") or {}).get("account_key") == expected),
        None,
    )
    quota_idx = next(
        (i for i, r in enumerate(recs)
         if r.get("t") == "obs"
         and (r.get("payload") or {}).get("kind") == "quota_window_snapshot"
         and r.get("account") == expected),
        None,
    )
    assert observe_idx is not None, "account_observe not journaled"
    assert quota_idx is not None, "stamped quota obs not journaled"
    assert observe_idx < quota_idx, "observe must precede the stamped obs"
