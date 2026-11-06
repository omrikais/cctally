"""#1003 T12 — the rebuild's provider-sample projector flushes in 65,536-item
batches (spec D7).

`_cctally_oauth_samples.RebuildProjector` runs on the rebuild's
`.rebuilding-*` scratch, which keeps the live store policy
(`journal_mode=DELETE`, `synchronous=FULL`): each flush is a transaction that
creates, fsyncs and unlinks a rollback journal. At 2,048 items per flush that
cycle was ~1.8 GB of physical writes per rebuild (spec §2.7). These tests pin
the batch with the real constants, the published index's independence from
the batch size, the absence of any projected-table read before the final
flush, a failed flush's rollback, and the pending lists' memory bound.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import sqlite3
import sys
import time
import tracemalloc

import pytest

import _outcome_quarantine_trace as T
from conftest import load_script, redirect_paths

NOW = 1791417600
WEEK_END = NOW + 3 * 86400
FIVE_END = NOW + 3600
PROJECTED = ("claude_oauth_samples", "claude_weekly_observation_index",
             "rebuild_weekly_observation_unstamped")


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _set_clock(monkeypatch, epoch: float) -> None:
    monkeypatch.setattr(time, "time", lambda: float(epoch))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(epoch))


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _set_clock(monkeypatch, NOW)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    monkeypatch.setattr(cctally, "_bust_statusline_cache", lambda: "skipped")
    monkeypatch.setattr(cctally, "_resolve_oauth_token", lambda: "credential")
    cctally.open_db().close()
    T.identity("A")
    return cctally


def _refresh(app, monkeypatch, *, at, weekly, five, lead=2):
    """One forced OAuth refresh: an api observation carrying a sample and a
    weekly reading (so its `offer` queues a sample AND an index row)."""
    _set_clock(monkeypatch, at - lead)
    response = {"seven_day": {"utilization": weekly,
                              "resets_at": _iso(WEEK_END)},
                "five_hour": {"utilization": five,
                              "resets_at": _iso(FIVE_END)}}

    def fetch(**_kwargs):
        _set_clock(monkeypatch, at)
        return response

    monkeypatch.setattr(app, "_fetch_oauth_usage", fetch)
    result = app._refresh_usage_inproc()
    assert result.status == "ok", result


def _template(app, monkeypatch):
    """The journal's api observation, which queues one sample and one index
    row when offered."""
    import _cctally_oauth_samples as S
    _refresh(app, monkeypatch, at=NOW, weekly=40, five=30)
    [record] = [r for r in T.journal_records()
                if S.sample_from_record(r) is not None]
    assert S._weekly_observation_key(record) is not None
    return record


def _variants(template, count):
    """``count`` distinct observations shaped like ``template``."""
    for index in range(count):
        record = copy.deepcopy(template)
        record["id"] = f"{template['id']}-{index:06d}"
        yield record


def _scratch(tmp_path, name):
    import _cctally_core
    return _cctally_core.open_db(_target_path=str(tmp_path / name))


def _trace(conn) -> list:
    statements: list = []
    conn.set_trace_callback(statements.append)
    return statements


def _heads(statements) -> list:
    return [s.strip().split(None, 1)[0].upper() for s in statements
            if s.strip()]


def test_seventy_thousand_items_commit_twice(app, monkeypatch, tmp_path):
    """D7, RED with the real constants: 35,000 offers each queueing a sample
    and an observation-index row (70,000 items) commit exactly twice — one
    full 65,536-item batch and the final flush. At 2,048 they commit 35
    times, each a synced rollback-journal cycle on the rebuild scratch."""
    import _cctally_oauth_samples as S
    template = _template(app, monkeypatch)
    conn = _scratch(tmp_path, "projector.db")
    try:
        statements = _trace(conn)
        projector = S.RebuildProjector(conn)
        for record in _variants(template, 35_000):
            projector.offer(record)
        projector.flush()
        conn.set_trace_callback(None)
        heads = _heads(statements)
        assert heads.count("COMMIT") == 2, heads.count("COMMIT")
        assert heads.count("BEGIN") == 2
        assert projector.projected == 35_000
        rows = conn.execute(
            "SELECT COUNT(*) FROM claude_oauth_samples").fetchone()[0]
        index_rows = conn.execute(
            "SELECT COUNT(*) FROM claude_weekly_observation_index"
        ).fetchone()[0]
    finally:
        conn.close()
    assert (rows, index_rows) == (35_000, 35_000)


def test_failed_flush_rolls_back_and_reraises(app, monkeypatch, tmp_path):
    """D7: a failure inside a flush rolls back that whole batch, leaves no
    open transaction and propagates, so a larger batch never publishes a
    partial projection."""
    import _cctally_oauth_samples as S
    template = _template(app, monkeypatch)
    real = S.project_sample
    seen = [0]

    def failing(conn, sample):
        seen[0] += 1
        if seen[0] == 50:
            raise RuntimeError("simulated projection failure")
        return real(conn, sample)

    conn = _scratch(tmp_path, "failing.db")
    try:
        projector = S.RebuildProjector(conn)
        for record in _variants(template, 100):
            projector.offer(record)
        monkeypatch.setattr(S, "project_sample", failing)
        with pytest.raises(RuntimeError, match="simulated projection"):
            projector.flush()
        assert not conn.in_transaction
        assert conn.execute(
            "SELECT COUNT(*) FROM claude_oauth_samples").fetchone()[0] == 0
    finally:
        conn.close()


def test_pending_lists_at_a_full_batch_stay_under_96_mib(
        app, monkeypatch, tmp_path):
    """D7: the batch still bounds the pass's memory. The pending samples
    and index rows of one batch short of a flush (65,534 items) allocate
    under 96 MiB."""
    import _cctally_oauth_samples as S
    template = _template(app, monkeypatch)
    conn = _scratch(tmp_path, "memory.db")
    try:
        projector = S.RebuildProjector(conn)
        assert S.RebuildProjector.BATCH == 65_536
        tracemalloc.start()
        try:
            base = tracemalloc.get_traced_memory()[0]
            for record in _variants(template, 32_767):
                projector.offer(record)
            held = tracemalloc.get_traced_memory()[0] - base
        finally:
            tracemalloc.stop()
        assert len(projector._pending) + len(projector._observations) == (
            65_534)
        assert held < 96 * 1024 * 1024, held
    finally:
        conn.close()


def _published_projection(app) -> dict:
    conn = app.open_db()
    try:
        return {table: sorted(tuple(r) for r in conn.execute(
            f"SELECT * FROM {table}")) for table in PROJECTED[:2]}
    finally:
        conn.close()


def test_rebuild_projection_independent_of_batch_and_read_after_flush(
        app, monkeypatch, tmp_path, capsys):
    """D7's equality argument: every reader of the projected tables runs
    after the pass's final flush, and the flush writes items in offer order
    whatever the batch, so a `db rebuild` publishes the same samples and
    index with the real batch and with a batch of 1, and no statement on
    the rebuild scratch reads a projected table before the final flush."""
    import _cctally_oauth_samples as S
    import _lib_journal as J
    import _cctally_journal as jr
    for i in range(4):
        _refresh(app, monkeypatch, at=NOW + 60 * i, weekly=40 + i, five=30)
    # An unstamped legacy reading the rebuild stages for the cutover account.
    legacy = J.unstamped_record(J.make_obs(
        at=_iso(NOW + 400), src="record-usage", provider="claude", payload={
            "weekly_percent": 44.0, "resets_at": WEEK_END,
            "source": "statusline", "captured_at": _iso(NOW + 400)}))
    jr.append_record(legacy)
    jr.run_stats_ingest(mode="authoritative")

    traces: list = []
    real_init = S.RebuildProjector.__init__
    real_flush = S.RebuildProjector.flush

    def init(self, conn):
        statements: list = []
        conn.set_trace_callback(statements.append)
        traces.append({"statements": statements, "flushes": []})
        real_init(self, conn)

    def flush(self):
        real_flush(self)
        traces[-1]["flushes"].append(len(traces[-1]["statements"]))

    monkeypatch.setattr(S.RebuildProjector, "__init__", init)
    monkeypatch.setattr(S.RebuildProjector, "flush", flush)
    args = argparse.Namespace(db="stats", json=True)
    assert app.cmd_db_rebuild(args) == 0
    capsys.readouterr()
    published = _published_projection(app)
    assert published["claude_oauth_samples"], "non-vacuity: no sample"
    assert len(published["claude_weekly_observation_index"]) >= 5

    [trace] = traces
    final = trace["flushes"][-1]
    for statement in trace["statements"][:final]:
        head = statement.strip().split(None, 1)[0].upper()
        if head in ("SELECT", "WITH") or "FROM" in statement.upper():
            assert not any(table in statement for table in PROJECTED), (
                statement)

    monkeypatch.setattr(S.RebuildProjector, "BATCH", 1)
    traces.clear()
    assert app.cmd_db_rebuild(args) == 0
    capsys.readouterr()
    assert _published_projection(app) == published
    assert len(traces[0]["flushes"]) > 2, "BATCH=1 flushed per item"
