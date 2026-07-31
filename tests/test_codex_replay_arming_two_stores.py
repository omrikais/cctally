"""Both stores arm themselves, through their own dispatchers (spec §4.3).

A cache-only per-migration golden cannot prove the conversations marker was
armed, and neither golden can prove the two halves compose. This module opens
both stores through the real migration dispatchers and drives the whole repair:
arm, defer, replay in order, converge.

It also pins the kill points. Each handler commits its own marker and the
dispatcher stamps `schema_migrations` centrally afterwards, so a crash in
between re-invokes the handler on its own output — that must converge, not
double-apply or raise. Two separate registered migrations, one per store, mean a
crash BETWEEN them leaves the unrun one pending and self-healing at that store's
next open, rather than a half-armed cross-database state.
"""
from __future__ import annotations

import pathlib
import shutil
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

import _cctally_core  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402

CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1"
PER_MIGRATION = REPO_ROOT / "tests" / "fixtures" / "migrations" / "per-migration"
CACHE_PRE = PER_MIGRATION / "035_codex_thread_source_inference_replay" / "pre.sqlite"
CONV_PRE = (
    PER_MIGRATION
    / "conversations_002_codex_thread_source_inference_replay" / "pre.sqlite")

CACHE_MIGRATION = "035_codex_thread_source_inference_replay"
CONV_MIGRATION = "002_codex_thread_source_inference_replay"


def _markers():
    import _cctally_cache
    return (_cctally_cache.CODEX_REPLAY_FROM_ZERO_KEY,
            _cctally_cache.CODEX_CONVERSATION_REPLAY_FROM_ZERO_KEY)


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A pre-upgrade install: cache.db at 034-head, conversations.db at 001-head,
    plus one real Codex rollout on disk."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(CACHE_PRE, _cctally_core.CACHE_DB_PATH)
    shutil.copy(CONV_PRE, _cctally_core.CONVERSATIONS_DB_PATH)

    provider_root = tmp_path / "provider"
    rollout = (
        provider_root / "sessions" / "2026" / "07" / "15" / "rollout.jsonl")
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "rollouts" / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, rollout


def _meta(path, key):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()


def _stamped(path, name):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (name,)).fetchone()[0]
    finally:
        conn.close()


def test_both_dispatchers_arm_their_own_store(install):
    """Opening each store runs its own migration under its own locks."""
    ns, _rollout = install
    cache_key, conv_key = _markers()

    assert _meta(_cctally_core.CACHE_DB_PATH, cache_key) is None
    assert _meta(_cctally_core.CONVERSATIONS_DB_PATH, conv_key) is None

    ns["open_cache_db"]().close()
    assert _meta(_cctally_core.CACHE_DB_PATH, cache_key) == ("1",)
    assert _stamped(_cctally_core.CACHE_DB_PATH, CACHE_MIGRATION) == 1
    # The cache half must not reach across into the other store.
    assert _meta(_cctally_core.CONVERSATIONS_DB_PATH, conv_key) is None

    ns["open_conversations_db"]().close()
    assert _meta(_cctally_core.CONVERSATIONS_DB_PATH, conv_key) == ("1",)
    assert _stamped(_cctally_core.CONVERSATIONS_DB_PATH, CONV_MIGRATION) == 1


def test_a_crash_between_the_two_stores_self_heals_at_the_next_open(install):
    """The conversations half stays pending and arms itself later."""
    ns, _rollout = install
    _cache_key, conv_key = _markers()

    ns["open_cache_db"]().close()
    assert _stamped(_cctally_core.CONVERSATIONS_DB_PATH, CONV_MIGRATION) == 0
    assert _meta(_cctally_core.CONVERSATIONS_DB_PATH, conv_key) is None

    # ... process dies here; a later run opens the other store.
    ns["open_conversations_db"]().close()
    assert _meta(_cctally_core.CONVERSATIONS_DB_PATH, conv_key) == ("1",)


@pytest.mark.parametrize("store", ["cache", "conversations"])
def test_a_crash_before_the_central_stamp_converges_on_rerun(install, store):
    """The handler commits, the dispatcher stamps. A crash in between re-invokes
    the handler on its own output, so it must be idempotent."""
    ns, _rollout = install
    cache_key, conv_key = _markers()
    if store == "cache":
        db_path = _cctally_core.CACHE_DB_PATH
        key, name, opener = cache_key, CACHE_MIGRATION, "open_cache_db"
        registry = "_CACHE_MIGRATIONS"
    else:
        db_path = _cctally_core.CONVERSATIONS_DB_PATH
        key, name, opener = conv_key, CONV_MIGRATION, "open_conversations_db"
        registry = "_CONVERSATIONS_MIGRATIONS"

    handler = next(
        item.handler for item in ns[registry] if item.name == name)
    conn = sqlite3.connect(db_path)
    try:
        handler(conn)  # committed
    finally:
        conn.close()
    # ... crash before `_stamp_applied`.
    assert _meta(db_path, key) == ("1",)
    assert _stamped(db_path, name) == 0

    ns[opener]().close()  # the dispatcher re-invokes the handler
    assert _meta(db_path, key) == ("1",), "re-running must not clear the marker"
    assert _stamped(db_path, name) == 1


def test_the_repair_runs_in_order_and_both_markers_clear(install):
    """End to end: conversations defers, the cache replays, then conversations
    replays and the rollout ends up with a conversation identity."""
    ns, _rollout = install
    cache_key, conv_key = _markers()

    ns["open_cache_db"]().close()
    ns["open_conversations_db"]().close()
    assert _meta(_cctally_core.CACHE_DB_PATH, cache_key) == ("1",)
    assert _meta(_cctally_core.CONVERSATIONS_DB_PATH, conv_key) == ("1",)

    # The dashboard's conversation worker reaching the store first must defer.
    conv = ns["open_conversations_db"]()
    try:
        deferred = ns["sync_codex_conversations"](conv)
        assert deferred.deferred_reason == "cache_replay_pending"
        assert conv.execute(
            "SELECT COUNT(*) FROM codex_conversation_rollups").fetchone()[0] == 0
    finally:
        conv.close()
    assert _meta(_cctally_core.CONVERSATIONS_DB_PATH, conv_key) == ("1",)

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
        assert stats.files_processed == 1
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_conversation_threads").fetchone()[0] == 1
    finally:
        cache.close()
    assert _meta(_cctally_core.CACHE_DB_PATH, cache_key) is None

    conv = ns["open_conversations_db"]()
    try:
        replayed = ns["sync_codex_conversations"](conv)
        assert replayed.files_processed == 1
        rollups = conv.execute(
            "SELECT conversation_key, project_label "
            "FROM codex_conversation_rollups").fetchall()
        assert len(rollups) == 1
        assert rollups[0][0].startswith("v1.")
        assert rollups[0][1] != "(unassigned)"
    finally:
        conv.close()
    assert _meta(_cctally_core.CONVERSATIONS_DB_PATH, conv_key) is None


def test_reads_are_not_authoritative_until_both_halves_have_run(install):
    """A `--no-sync` read between the migration and the repair must say so."""
    import _lib_codex_conversation_query as q

    ns, _rollout = install
    ns["open_cache_db"]().close()
    conv = ns["open_conversations_db"]()
    try:
        assert q.codex_normalization_authoritative(conv) is False
    finally:
        conv.close()
