"""The conversations half of the byte-zero Codex replay (spec §4.3).

Two stores are armed by two distinct markers, and the conversations replay is
ordered BEHIND the cache replay. `_recompute_codex_rollups` resolves project
attribution from the cache-side `codex_conversation_threads` row; with no thread
row it stamps a fully materialized `("(unassigned)", "(unassigned)")` — not a
NULL a later pass would refill — and the read path prefers the stored rollup. A
conversation with no subsequent activity would keep that wrong project forever,
which is the exact defect this work exists to remove.

The conversations marker is also deliberately distinct from
`conversation_rebuild_codex_pending`: `_ensure_codex_conversation_contract`
consumes that one by replaying normalization over already-retained events, which
preserves their NULL conversation keys, and then deletes it — silently
discarding the repair.
"""
from __future__ import annotations

import fcntl
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
CONV_PRE = (
    REPO_ROOT / "tests" / "fixtures" / "migrations" / "per-migration"
    / "conversations_002_codex_thread_source_inference_replay" / "pre.sqlite")


def _stage(tmp_path, monkeypatch, scenario="modern-full"):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = (
        provider_root / "sessions" / "2026" / "07" / "15" / "rollout.jsonl")
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "rollouts" / f"{scenario}.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, provider_root, rollout


def _set(conn, key, table="cache_meta"):
    conn.execute(
        f"INSERT OR REPLACE INTO {table}(key,value) VALUES(?,?)", (key, "1"))
    conn.commit()


def _has(conn, key, table="cache_meta") -> bool:
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE key=?", (key,)).fetchone() is not None


def _keys():
    import _cctally_cache
    return (_cctally_cache.CODEX_REPLAY_FROM_ZERO_KEY,
            _cctally_cache.CODEX_CONVERSATION_REPLAY_FROM_ZERO_KEY)


def _arm_cache_marker(ns, key):
    """Arm the cache-side marker in cache.db itself, not in conversations.db."""
    cache = ns["open_cache_db"]()
    try:
        _set(cache, key)
    finally:
        cache.close()


def test_conversation_replay_defers_while_cache_replay_is_pending(
    tmp_path, monkeypatch,
):
    ns, _root, _rollout = _stage(tmp_path, monkeypatch)
    cache_key, conv_key = _keys()

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        first = ns["sync_codex_conversations"](conv)
        assert first.files_processed == 1
    finally:
        conv.close()

    _arm_cache_marker(ns, cache_key)
    conv = ns["open_conversations_db"]()
    try:
        _set(conv, conv_key)
        deferred = ns["sync_codex_conversations"](conv)
        assert deferred.deferred_reason == "cache_replay_pending"
        assert deferred.files_processed == 0
        # Nothing was cleared: the deferral happens before any mutation.
        assert conv.execute(
            "SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0] > 0
        assert _has(conv, conv_key)
    finally:
        conv.close()

    # Cache replay finished — now the conversations replay may run.
    cache = ns["open_cache_db"]()
    try:
        cache.execute("DELETE FROM cache_meta WHERE key=?", (cache_key,))
        cache.commit()
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        replayed = ns["sync_codex_conversations"](conv)
        assert replayed.files_processed == 1, (
            "the conversations marker must promote an ordinary sync to a "
            "byte-zero rebuild once the cache side has cleared")
        assert not _has(conv, conv_key)
    finally:
        conv.close()


def test_targeted_conversation_sync_declines_while_cache_replay_is_pending(
    tmp_path, monkeypatch,
):
    """A live-tail tick declines rather than running ahead of the cache."""
    ns, _root, rollout = _stage(tmp_path, monkeypatch)
    cache_key, _conv_key = _keys()

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
    finally:
        conv.close()

    _arm_cache_marker(ns, cache_key)
    conv = ns["open_conversations_db"]()
    try:
        stats = ns["sync_codex_conversations"](conv, only_paths={str(rollout)})
        assert stats.deferred_reason == "cache_replay_pending"
        assert stats.targeted_clean is False
    finally:
        conv.close()


def test_no_rollup_is_stamped_unassigned_while_threads_are_unreplayed(
    tmp_path, monkeypatch,
):
    """The blocking regression (spec §4.2).

    Drives the ordering the dashboard's independent conversation worker
    produces: the conversations sync is reached while cache.db still holds no
    thread row for the rollout. Nothing may be stamped, because a rollup written
    now would carry a materialized "(unassigned)" project that the read path
    then prefers permanently.
    """
    ns, _root, _rollout = _stage(tmp_path, monkeypatch)
    cache_key, conv_key = _keys()

    _arm_cache_marker(ns, cache_key)
    cache = ns["open_cache_db"]()
    try:
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_conversation_threads"
        ).fetchone()[0] == 0, "the cache replay has not run yet"
    finally:
        cache.close()

    conv = ns["open_conversations_db"]()
    try:
        _set(conv, conv_key)
        stats = ns["sync_codex_conversations"](conv)
        # The hazard itself, asserted BEFORE the mechanism that prevents it, so
        # dropping the deferral fails this test on the materialized project
        # rather than on the deferral bookkeeping.
        stamped = conv.execute(
            "SELECT conversation_key, project_label "
            "FROM codex_conversation_rollups").fetchall()
        assert stamped == [], (
            "a rollup was stamped before the thread rows were replayed: "
            f"{stamped!r}")
        assert stats.deferred_reason == "cache_replay_pending"
    finally:
        conv.close()

    # Cache replay lands the thread rows, then the conversations replay runs.
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_conversation_threads"
        ).fetchone()[0] == 1
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
        rollups = conv.execute(
            "SELECT project_key, project_label "
            "FROM codex_conversation_rollups").fetchall()
        assert len(rollups) == 1
        project_key, project_label = rollups[0]
        assert project_label != "(unassigned)"
        assert project_key and project_key.startswith("project:")
    finally:
        conv.close()


def test_contract_replay_does_not_consume_the_new_marker(tmp_path, monkeypatch):
    """`_ensure_codex_conversation_contract` replays normalization over
    already-retained events, which preserves their NULL conversation keys. If it
    consumed the replay marker it would silently discard the repair."""
    import _cctally_cache

    ns, _root, _rollout = _stage(tmp_path, monkeypatch)
    _cache_key, conv_key = _keys()

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
        assert conv.execute(
            "SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0] > 0

        _set(conv, "conversation_rebuild_codex_pending")
        _set(conv, conv_key)
        assert _cctally_cache._ensure_codex_conversation_contract(conv) is True

        assert not _has(conv, "conversation_rebuild_codex_pending"), (
            "the contract replay must still consume its own marker")
        assert _has(conv, conv_key), (
            "the byte-zero replay marker must survive a contract replay")
    finally:
        conv.close()


def test_a_marker_armed_mid_walk_is_not_swallowed_by_that_walk(
    tmp_path, monkeypatch,
):
    """The clear must be guarded on the flag THIS call observed.

    conversations migration 002 runs under the dispatcher inside
    `_conversations_open_guarded`, which holds only a SHARED maintenance flock,
    while `sync_codex_conversations` serializes on a different file
    (`conversations.db.codex.lock`). So process A can start a walk with no
    marker present, process B can open the store and arm it, and A's
    unconditional clear then deletes it. The migration is already stamped, so it
    never re-arms: cache.db keeps its replayed thread rows while conversations.db
    keeps its NULL-key events, and the affected rollouts stay invisible forever.
    """
    ns, _root, _rollout = _stage(tmp_path, monkeypatch)
    _cache_key, conv_key = _keys()

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
        assert not _has(conv, conv_key)

        armed = []

        def arm_from_another_process(phase, _stats):
            # Process B: its own connection to the same store, exactly as the
            # migration dispatcher would reach it during A's walk.
            if phase != "finalize" or armed:
                return
            other = sqlite3.connect(str(_cctally_core.CONVERSATIONS_DB_PATH))
            try:
                _set(other, conv_key)
            finally:
                other.close()
            armed.append(phase)

        ns["sync_codex_conversations"](
            conv, progress=arm_from_another_process)
        assert armed, "the mid-walk arming hook never fired"
        assert _has(conv, conv_key), (
            "a marker armed during the walk was cleared by that walk; "
            "migration 002 is already stamped, so it never re-arms")
    finally:
        conv.close()


def test_a_marker_armed_mid_walk_is_not_swallowed_by_the_cache_walk(
    tmp_path, monkeypatch,
):
    """Defense in depth on the cache side (§4.3).

    `open_cache_db` and `sync_codex_cache` share an exclusive lock, so this
    interleaving is not reachable today — but the two clears must have the same
    shape, so a later lock change cannot reopen the hole on the cache side.
    """
    ns, _root, _rollout = _stage(tmp_path, monkeypatch)
    cache_key, _conv_key = _keys()

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        assert not _has(cache, cache_key)

        armed = []

        def arm_from_another_process(_path):
            if armed:
                return
            other = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
            try:
                _set(other, cache_key)
            finally:
                other.close()
            armed.append(True)

        ns["sync_codex_cache"](
            cache, rebuild=True, _on_file_committed=arm_from_another_process)
        assert armed, "the mid-walk arming hook never fired"
        assert _has(cache, cache_key), (
            "a marker armed during the walk was cleared by that walk")
    finally:
        cache.close()


def test_migration_002_defers_while_a_codex_conversation_sync_holds_the_lock(
    tmp_path, monkeypatch,
):
    """Arming under a racing walk is what makes the swallow reachable.

    The conversations dispatcher holds `CONVERSATIONS_LOCK_MAINTENANCE_PATH`
    only SHARED, and `sync_codex_conversations` serializes on
    `CONVERSATIONS_LOCK_CODEX_PATH` instead — so without taking that lock the
    migration can arm the marker in the middle of someone else's walk. It must
    defer (`MigrationGateNotMet`) and stay pending, the way cache migration
    `028_split_conversation_store` does.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(CONV_PRE, _cctally_core.CONVERSATIONS_DB_PATH)
    _cache_key, conv_key = _keys()

    _cctally_core.CONVERSATIONS_LOCK_CODEX_PATH.touch()
    holder = open(_cctally_core.CONVERSATIONS_LOCK_CODEX_PATH, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        ns["open_conversations_db"](attach_cache=False).close()
        probe = sqlite3.connect(str(_cctally_core.CONVERSATIONS_DB_PATH))
        try:
            assert not _has(probe, conv_key), (
                "migration 002 armed the marker while a Codex conversation "
                "sync held the provider lock")
            assert probe.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
                ("002_codex_thread_source_inference_replay",),
            ).fetchone()[0] == 0, "a deferred migration must stay pending"
        finally:
            probe.close()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    # Uncontended, it arms normally on the next open.
    conv = ns["open_conversations_db"](attach_cache=False)
    try:
        assert _has(conv, conv_key)
    finally:
        conv.close()


def test_migration_002_handler_raises_a_gate_error_on_contention(
    tmp_path, monkeypatch,
):
    """The handler-level contract behind the deferral above."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(CONV_PRE, _cctally_core.CONVERSATIONS_DB_PATH)
    import _cctally_db

    handler = next(
        item.handler for item in ns["_CONVERSATIONS_MIGRATIONS"]
        if item.name == "002_codex_thread_source_inference_replay")
    conn = sqlite3.connect(str(_cctally_core.CONVERSATIONS_DB_PATH))
    holder = open(_cctally_core.CONVERSATIONS_LOCK_CODEX_PATH, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(_cctally_db.MigrationGateNotMet):
            handler(conn)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
        conn.close()


@pytest.mark.parametrize(
    "marker",
    ["conversation_rebuild_codex_pending",
     "codex_conversation_replay_from_zero_pending"],
)
def test_authority_is_false_while_either_marker_is_pending(
    tmp_path, monkeypatch, marker,
):
    """A `--no-sync` read must not present a not-yet-repaired store as
    authoritative."""
    import _lib_codex_conversation_query as q

    ns, _root, _rollout = _stage(tmp_path, monkeypatch)
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
        assert q.codex_normalization_authoritative(conv) is True
        _set(conv, marker)
        assert q.codex_normalization_authoritative(conv) is False
        conv.execute("DELETE FROM cache_meta WHERE key=?", (marker,))
        conv.commit()
        assert q.codex_normalization_authoritative(conv) is True
    finally:
        conv.close()
