"""Task15 corrections: immediate root reads and last-good metadata resolution."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths
from _codex_zstd_fixtures import compress_in_place, token_records, write_rollout
from _lib_source_identity import source_root_key
from test_codex_hook_lifecycle import _hook_args

START = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
END = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)


def _runtime(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached", lambda command: True)
    return ns


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
def test_current_roots_hide_retained_prior_root_before_drain(
    tmp_path, monkeypatch, relative, compressed,
):
    ns = _runtime(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    pa, pb = a / "sessions" / "a.jsonl", b / "sessions" / "b.jsonl"
    write_rollout(pa, token_records(1, session_id="a"))
    write_rollout(pb, token_records(2, session_id="b"))
    if compressed:
        compress_in_place(pb)
    monkeypatch.setenv("CODEX_HOME", "a" if relative else str(a))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, maintenance="caller")
        monkeypatch.setenv("CODEX_HOME", "b" if relative else str(b))
        ns["sync_codex_cache"](conn, maintenance="caller")
        # Off-hook cleanup is outstanding: the registry still contains A+B.
        assert {row[0] for row in conn.execute("SELECT source_root_key FROM codex_source_roots")} == {
            source_root_key(str(a)), source_root_key(str(b)),
        }
        assert len(ns["iter_codex_entries"](conn, START, END)) == 3
        conn.execute("UPDATE codex_session_entries SET account_key='account-b' WHERE source_root_key=?",
                     (source_root_key(str(b)),))
        conn.commit()
        assert len(ns["iter_codex_entries"](conn, START, END, current_roots_only=True)) == 2
        for skip_sync in (True, False):
            rows = ns["get_codex_entries"](START, END, skip_sync=skip_sync)
            assert len(rows) == 2
            assert {entry.source_path for entry in rows} == {str(pb)}
        assert len(ns["get_codex_entries"](START, END, skip_sync=True, account_key="account-b")) == 2
        assert ns["get_codex_entries"](START, END, skip_sync=True, account_key="unattributed") == []
        assert len(ns["iter_codex_entries"](conn, START, END)) == 3
        assert conn.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone() == (3,)
    finally:
        conn.close()


@pytest.mark.parametrize("replacement", ["missing", "empty", "unknown", "recognized"])
def test_root_evidence_protects_history_and_relative_fixture_paths(
    tmp_path, monkeypatch, replacement,
):
    ns = _runtime(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    write_rollout(a / "sessions" / "a.jsonl", token_records(1, session_id="a"))
    monkeypatch.setenv("CODEX_HOME", str(a))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, maintenance="caller")
        # Synthetic relative paths and identity-less historical rows are not
        # positive evidence of a retired configured root.
        for path, root in ((".codex/sessions/fixture.jsonl", "fixture-root"),
                           ("/unknown/history.jsonl", None)):
            conn.execute(
                "INSERT INTO codex_session_entries (source_path,line_offset,timestamp_utc,session_id,model,"
                "input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,source_root_key) "
                "VALUES (?,1,?,'fixture','gpt-5',1,0,1,0,2,?)",
                (path, START.isoformat(), root))
        conn.commit()
        if replacement == "empty":
            (b / "sessions").mkdir(parents=True)
        elif replacement == "unknown":
            write_rollout(b / "sessions" / "unknown.jsonl", [{"type": "future-record", "payload": {}}])
        elif replacement == "recognized":
            write_rollout(b / "sessions" / "b.jsonl", token_records(1, session_id="b"))
        monkeypatch.setenv("CODEX_HOME", str(b))
        rows = ns["get_codex_entries"](START, END, skip_sync=True)
        assert len(rows) == (2 if replacement == "recognized" else 3)
        assert {".codex/sessions/fixture.jsonl", "/unknown/history.jsonl"} <= {row.source_path for row in rows}
        monkeypatch.setenv("CODEX_HOME", f"{a},{b}")
        assert len(ns["get_codex_entries"](START, END, skip_sync=True)) == 3
    finally:
        conn.close()


def test_metadata_probe_keeps_last_good_priority_and_topology_invalidation():
    import _lib_codex_metadata as metadata
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE codex_conversation_threads (conversation_key TEXT)")
        statements = []
        conn.set_trace_callback(statements.append)
        assert metadata.resolve_codex_threads_table(conn) == "main.codex_conversation_threads"
        assert len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]) == 1
        conn.execute("CREATE TEMP VIEW codex_last_good_threads AS SELECT 'published' AS conversation_key")
        metadata.invalidate_codex_threads_table_resolution(conn)
        assert metadata.resolve_codex_threads_table(conn) == "temp.codex_last_good_threads"
        statements.clear()
        assert metadata.resolve_codex_threads_table(conn) == "temp.codex_last_good_threads"
        assert statements == []
        conn.execute("DROP VIEW temp.codex_last_good_threads")
        metadata.invalidate_codex_threads_table_resolution(conn)
        assert metadata.resolve_codex_threads_table(conn) == "main.codex_conversation_threads"
    finally:
        conn.close()


@pytest.mark.parametrize("budget_block", [{}, {"budget": {"codex": {
    "amount_usd": 100, "alerts_enabled": True, "projected_enabled": True,
    "alert_thresholds": [],
}}}])
def test_due_no_threshold_hook_ingests_and_acknowledges_without_entry_readers(
    tmp_path, monkeypatch, budget_block,
):
    ns = _runtime(tmp_path, monkeypatch)
    root = tmp_path / "codex"
    (root / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(root))
    import _cctally_core as core
    core.CONFIG_PATH.write_text(json.dumps(budget_block))
    ns["open_db"]().close()  # current epoch: the tick must not defer here
    conn = ns["open_cache_db"]()
    ns["sync_codex_cache"](conn, maintenance="caller")
    conn.close()
    path = root / "sessions" / "new.jsonl"
    write_rollout(path, token_records(1, session_id="new"))
    cache = sys.modules["_cctally_cache"]
    calls = []
    real_budget = ns["maybe_record_codex_budget_milestone"]

    def budget(*args, **kwargs):
        calls.append("budget")
        return real_budget(*args, **kwargs)

    def forbid_reader(*args, **kwargs):
        pytest.fail("a due no-threshold Codex hook must not call accounting entry readers")

    monkeypatch.setitem(ns, "maybe_record_codex_budget_milestone", budget)
    for name in ("get_codex_entries", "iter_codex_entries"):
        monkeypatch.setitem(ns, name, forbid_reader)
        monkeypatch.setattr(cache, name, forbid_reader)
    marker = ns["APP_DIR"] / "codex-hook-tick" / f"{source_root_key(str(root))}.last-success"
    assert not marker.exists()
    assert ns["cmd_hook_tick"](_hook_args()) == 0
    assert marker.is_file()  # real lifecycle success, no throttle/epoch shortcut
    assert calls == ["budget"]
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM codex_session_entries WHERE source_path=?", (str(path),)).fetchone() == (1,)
    finally:
        conn.close()
    log = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
    assert any("sync=ok" in line and "result=success" in line for line in log.splitlines())
    assert "result=noop" not in log
