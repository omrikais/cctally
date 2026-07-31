"""The `data.codex_replay` doctor leg — visibility for a STALLED byte-zero
Codex replay.

`sync_codex_conversations` returns early on any pending cache-side replay
marker, so while that marker stands no Codex transcript is ingested. The
deferral is protective (running ahead of the replayed thread rows stamps a
materialized `"(unassigned)"` project the read path then prefers permanently),
so it stays — but a whole-tree sync that runs and still cannot consume the
marker holds the deferral open indefinitely while `cache-sync` exits 0, the
dashboard discards the stats, and nothing else says a word.

Covers the pure check, the cache-side write that feeds it, and the gather that
carries it across.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402

ROLLOUTS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"


@pytest.fixture(scope="module")
def D():
    load_script()
    import _lib_doctor
    return _lib_doctor


def _s(pending=None, blocked=None):
    return types.SimpleNamespace(
        codex_replay_pending=pending, codex_replay_blocked=blocked)


# ── the pure check ───────────────────────────────────────────────────────────


def test_ok_on_a_store_with_no_replay(D):
    r = D._check_data_codex_replay(_s())
    assert r.severity == "ok"
    assert r.summary == "none pending"


def test_ok_while_the_marker_is_merely_pending(D):
    """The ordinary state between the migration and the next sync. It clears on
    its own, so reporting it would nag every upgrade."""
    r = D._check_data_codex_replay(_s(pending=True))
    assert r.severity == "ok"
    assert "pending" in r.summary


def test_warns_once_a_completed_walk_could_not_consume_the_marker(D):
    r = D._check_data_codex_replay(_s(pending=True, blocked={
        "at": "2026-07-31T00:00:00Z", "files_failed": 0,
        "files_deferred_torn": 4,
    }))
    assert r.severity == "warn"
    assert "2026-07-31T00:00:00Z" in r.summary
    assert "transcript ingest is deferred" in r.summary
    assert r.remediation and "cache-sync --source codex" in r.remediation
    assert r.details["files_deferred_torn"] == 4


def test_a_stale_blocked_record_without_the_marker_stays_ok(D):
    """The marker is the gate. If it cleared, the stall is over regardless of a
    leftover record."""
    assert D._check_data_codex_replay(
        _s(blocked={"at": "2026-07-31T00:00:00Z"})).severity == "ok"


def test_a_malformed_blocked_record_degrades_ok(D):
    for bad in ({}, {"at": None}, {"at": ""}, {"at": 7}):
        assert D._check_data_codex_replay(
            _s(pending=True, blocked=bad)).severity == "ok", bad


def test_the_leg_is_registered(D):
    ids = [cid for _key, _title, checks in D._CATEGORY_DEFINITIONS
           for cid, _fn in checks]
    assert "data.codex_replay" in ids


# ── the write path + the gather ──────────────────────────────────────────────


def _stage(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = (
        provider_root / "sessions" / "2026" / "07" / "16" / "rollout.jsonl")
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(ROLLOUTS / "thread-source-absent-mcp.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, provider_root, rollout


def _meta(conn, key):
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def test_a_torn_auth_json_records_the_stall_and_doctor_reports_it(
    tmp_path, monkeypatch,
):
    """End to end: the deferral holds (unchanged), and it is now VISIBLE."""
    import _cctally_cache
    import _cctally_doctor

    ns, provider_root, _rollout = _stage(tmp_path, monkeypatch)
    blocked_key = _cctally_cache.CODEX_REPLAY_BLOCKED_KEY
    pending_key = _cctally_cache.CODEX_REPLAY_FROM_ZERO_KEY

    # A truncated auth.json: the stable-read protocol defers rather than guessing
    # an account, and the marker therefore cannot be consumed.
    (provider_root / "auth.json").write_text('{"tokens": {"id_toke')

    conn = ns["open_cache_db"]()
    try:
        _cctally_cache._set_cache_meta(conn, pending_key, "1")
        conn.commit()
        stats = ns["sync_codex_cache"](conn)
        assert stats.files_deferred_torn == 1, (
            "precondition: the torn auth.json must defer the rollout")
        assert _meta(conn, pending_key) == "1", (
            "the marker must survive so the repair retries")
        record = json.loads(_meta(conn, blocked_key))
        assert record["files_deferred_torn"] == 1
        assert record["at"].endswith("Z")
    finally:
        conn.close()

    state = _cctally_doctor.doctor_gather_state()
    assert state.codex_replay_pending is True
    assert state.codex_replay_blocked["files_deferred_torn"] == 1

    import _lib_doctor
    result = _lib_doctor._check_data_codex_replay(state)
    assert result.severity == "warn"

    # Fix the root cause: the next clean walk consumes both records.
    (provider_root / "auth.json").unlink()
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert _meta(conn, pending_key) is None
        assert _meta(conn, blocked_key) is None
    finally:
        conn.close()

    state = _cctally_doctor.doctor_gather_state()
    assert _lib_doctor._check_data_codex_replay(state).severity == "ok"


def test_a_clean_walk_with_no_marker_writes_no_stall_record(
    tmp_path, monkeypatch,
):
    import _cctally_cache

    ns, _provider_root, _rollout = _stage(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert _meta(conn, _cctally_cache.CODEX_REPLAY_BLOCKED_KEY) is None
    finally:
        conn.close()
