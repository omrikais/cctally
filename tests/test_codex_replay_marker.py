"""The cache-side byte-zero replay marker (spec §4.3).

Migration 035 arms a Codex replay by writing ONE ``cache_meta`` marker; it
clears no table. ``sync_codex_cache`` consumes the marker and ORs it into its
own ``rebuild``, which is what makes the full-rebuild path capture
``rebuild_known_identities`` BEFORE the clear.

That indirection is the whole point. A migration that called
``_clear_codex_derived_rows`` directly would delete ``codex_session_files`` out
of band, leaving the next ordinary sync with an empty snapshot — every re-read
rollout would fall through to the live-``auth.json`` branch and historical Codex
spend would be re-attributed to whoever is authenticated now (spec §4.1).
"""
from __future__ import annotations

import base64
import datetime as dt
import fcntl
import json
import pathlib
import shutil
import sys

import pytest

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
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")


def _auth_json(account_id: str, email: str) -> str:
    payload = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
        },
    }
    id_token = (
        f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(payload)}.sig")
    return json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token, "access_token": "a", "refresh_token": "r",
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root, rollout = _setup_root(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, provider_root, rollout


def _arm(conn):
    import _cctally_cache
    conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
        (_cctally_cache.CODEX_REPLAY_FROM_ZERO_KEY, "1"),
    )
    conn.commit()


def _armed(conn) -> bool:
    import _cctally_cache
    return conn.execute(
        "SELECT 1 FROM cache_meta WHERE key=?",
        (_cctally_cache.CODEX_REPLAY_FROM_ZERO_KEY,),
    ).fetchone() is not None


def test_marker_promotes_an_ordinary_sync_to_rebuild(env):
    """A ``rebuild=False`` call must take the full-rebuild path while armed."""
    ns, _provider_root, _rollout = env
    conn = ns["open_cache_db"]()
    try:
        first = ns["sync_codex_cache"](conn)
        assert first.files_processed == 1
        # An unarmed second sync is the delta no-op: nothing re-read.
        unchanged = ns["sync_codex_cache"](conn)
        assert unchanged.files_skipped_unchanged == 1
        assert unchanged.files_processed == 0

        _arm(conn)
        replayed = ns["sync_codex_cache"](conn)
        # Byte-zero: the file is re-read despite an unchanged size.
        assert replayed.files_processed == 1
        assert replayed.files_skipped_unchanged == 0
    finally:
        conn.close()


def test_marker_is_cleared_only_after_a_clean_full_walk(env):
    """The marker is what makes the repair retry, so a contended or targeted
    call must leave it standing."""
    ns, _provider_root, rollout = env
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        _arm(conn)

        # Contended: another holder of the Codex writer flock.
        import _cctally_core
        _cctally_core.CACHE_LOCK_PATH.touch()
        holder = open(_cctally_core.CACHE_LOCK_PATH, "w")
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            contended = ns["sync_codex_cache"](conn)
            assert contended.lock_contended is True
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        assert _armed(conn), "a contended sync must not consume the marker"

        # Targeted: declines, and must not consume it either.
        targeted = ns["sync_codex_cache"](conn, only_paths={str(rollout)})
        assert targeted.deferred_reason is not None
        assert _armed(conn), "a targeted sync must not consume the marker"

        clean = ns["sync_codex_cache"](conn)
        assert clean.files_processed == 1
        assert not _armed(conn), "a clean full walk consumes the marker"
    finally:
        conn.close()


def test_marker_declines_a_targeted_sync_instead_of_raising(env):
    """A live-tail tick must DEFER, never crash.

    ``sync_codex_cache`` raises ``ValueError`` on ``targeted and rebuild``, so
    promoting the marker into ``rebuild`` before that guard would convert every
    live-tail tick into an exception for as long as the replay is pending.
    """
    ns, _provider_root, rollout = env
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        _arm(conn)
        stats = ns["sync_codex_cache"](conn, only_paths={str(rollout)})
        assert stats.deferred_reason == "replay_pending"
        assert stats.targeted_clean is False
    finally:
        conn.close()


def test_pre_mechanism_file_stays_unattributed_across_a_marker_replay(env):
    """The blocking regression (spec §4.1).

    A rollout ingested before durable attribution existed has a
    ``codex_session_files`` cursor and NO journaled decision. Its bytes must
    come back unattributed across the replay, even though a different account
    is authenticated now. This holds only because the marker is consumed inside
    ``sync_codex_cache``, so ``rebuild_known_identities`` is captured from that
    seeded row before the clear.
    """
    ns, provider_root, rollout = env
    live = _expected_key("acct-blue", "blue@x.com")
    (provider_root / "auth.json").write_text(
        _auth_json("acct-blue", "blue@x.com"))

    import _cctally_cache
    from _lib_source_identity import source_root_key

    conn = ns["open_cache_db"]()
    try:
        root_key = source_root_key(str(provider_root.resolve()))
        st = rollout.stat()
        # Seed the pre-#416 world: a cursor row and nothing else. No journal op,
        # no `codex_file_accounts` range — the bytes were never durably decided.
        conn.execute(
            "INSERT INTO codex_session_files"
            "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at,"
            " source_root_key) VALUES (?,?,?,?,?,?)",
            (str(rollout.resolve()), st.st_size, st.st_mtime_ns, st.st_size,
             dt.datetime.now(dt.timezone.utc).isoformat(), root_key),
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_file_accounts").fetchone()[0] == 0

        _arm(conn)
        replayed = ns["sync_codex_cache"](conn)
        assert replayed.files_processed == 1

        rows = conn.execute(
            "SELECT account_key FROM codex_session_entries").fetchall()
        assert rows, "expected the replay to re-emit accounting rows"
        assert all(row[0] is None for row in rows), (
            "pre-mechanism Codex spend was re-attributed to the live account "
            f"{live!r}; the replay must leave it unattributed"
        )
    finally:
        conn.close()
