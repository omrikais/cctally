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


# --------------------------------------------------------------------------
# #981 Part A — the hook's Codex budget reads on a pinned snapshot
# --------------------------------------------------------------------------
#
# Shared by tests/test_codex_budget_alerts.py and
# tests/test_codex_hook_lifecycle.py. Every hook here keeps the default 5 s
# nested ingest budget (`resolve_codex_hook_ingest_budget`).

BUDGET_AS_OF = "2026-07-25T12:00:00Z"
BUDGET_PERIOD_START = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
BUDGET_NOW = dt.datetime(2026, 7, 25, 12, tzinfo=dt.timezone.utc)


class BudgetHarness:
    """A real Codex root, a real cache and the hook, with read-side recorders.

    Recorders: every `sync_codex_cache` call (budgeted or not), every
    direct-parse fallback, every filesystem discovery, and every Codex spend
    sum with the total it returned."""

    def __init__(self, tmp_path, monkeypatch, *, codex_budget, claude=None):
        self.monkeypatch = monkeypatch
        self.ns = _runtime(tmp_path, monkeypatch)
        self.root = tmp_path / "codex"
        (self.root / "sessions").mkdir(parents=True)
        self.root_key = source_root_key(str(self.root))
        monkeypatch.setenv("CODEX_HOME", str(self.root))
        monkeypatch.setenv("CCTALLY_AS_OF", BUDGET_AS_OF)
        import _cctally_core as core
        self.core = core
        budget = {"codex": codex_budget}
        if claude:
            budget.update(claude)
        core.CONFIG_PATH.write_text(json.dumps(
            {"display": {"tz": "utc"}, "budget": budget}) + "\n")
        self.ns["open_db"]().close()  # current epoch: no deferred rebuild
        self.cache = sys.modules["_cctally_cache"]
        self.dispatched: list = []
        monkeypatch.setitem(
            self.ns, "_dispatch_alert_notification",
            lambda payload, *, mode="real", **kw: self.dispatched.append(
                payload) or "queued")
        self.syncs: list = []          # ("budgeted"|"unbudgeted")
        self.direct_parses: list = []
        self.discoveries = 0
        self.spend: list = []          # (start, account_key, total)
        self.after_budgeted_sync = None
        real_sync = self.cache.sync_codex_cache

        def sync(conn, **kwargs):
            budgeted = kwargs.get("budget_seconds") is not None
            self.syncs.append("budgeted" if budgeted else "unbudgeted")
            stats = real_sync(conn, **kwargs)
            if budgeted and self.after_budgeted_sync is not None:
                self.after_budgeted_sync(stats)
            return stats

        monkeypatch.setitem(self.ns, "sync_codex_cache", sync)
        monkeypatch.setattr(self.cache, "sync_codex_cache", sync)

        def direct(range_start, range_end):
            self.direct_parses.append((range_start, range_end))
            return []

        monkeypatch.setattr(self.cache, "_collect_codex_entries_direct", direct)
        real_discover = self.cache._discover_codex_files_with_roots

        def discover(**kwargs):  # #970: walks pass `skipped=`
            self.discoveries += 1
            return real_discover(**kwargs)

        monkeypatch.setattr(
            self.cache, "_discover_codex_files_with_roots", discover)
        real_sum = self.ns["_sum_codex_cost_for_range"]

        def spend_sum(start, end, **kwargs):
            total = real_sum(start, end, **kwargs)
            self.spend.append((start, kwargs.get("account_key"), total))
            return total

        monkeypatch.setitem(self.ns, "_sum_codex_cost_for_range", spend_sum)

    # ── store ──────────────────────────────────────────────────────────
    def rollout(self, name, count, *, session_id=None):
        path = self.root / "sessions" / f"{name}.jsonl"
        write_rollout(path, token_records(count, session_id=session_id or name))
        return path

    def seed_sync(self):
        """An unbudgeted sync (as `cache-sync` or the drain would run), not
        counted against the hook's read-side recorders."""
        conn = self.ns["open_cache_db"]()
        try:
            stats = self.ns["sync_codex_cache"](conn, maintenance="caller")
        finally:
            conn.close()
        self.syncs.clear()
        return stats

    def cache_conn(self):
        return sqlite3.connect(str(self.core.CACHE_DB_PATH))

    def oracle(self, *, account_key=None, start=BUDGET_PERIOD_START,
               end=BUDGET_NOW):
        """The Codex spend a complete read of the committed cache returns."""
        conn = self.cache_conn()
        try:
            entries = self.cache.iter_codex_entries(
                conn, start, end, account_key=account_key)
        finally:
            conn.close()
        speed = self.ns["_resolve_codex_speed"]("auto")
        return sum(
            self.ns["_calculate_codex_entry_cost"](
                e.model, e.input_tokens, e.cached_input_tokens,
                e.output_tokens, e.reasoning_output_tokens, speed=speed)
            for e in entries if e.timestamp < end)

    # ── hook ───────────────────────────────────────────────────────────
    def hook(self):
        assert self.ns["cmd_hook_tick"](_hook_args()) == 0

    def log(self):
        path = self.ns["APP_DIR"] / "logs" / "hook-tick.log"
        return [
            dict(tok.split("=", 1) for tok in line.split()[1:] if "=" in tok)
            for line in path.read_text().splitlines()
        ]

    def marker(self):
        return (self.ns["APP_DIR"] / "codex-hook-tick"
                / f"{self.root_key}.last-success")

    def budget_rows(self):
        conn = self.ns["open_db"]()
        try:
            return [tuple(row) for row in conn.execute(
                "SELECT account_key, period_start_at, threshold, spent_usd "
                "FROM budget_milestones WHERE vendor='codex' "
                "ORDER BY account_key, threshold")]
        finally:
            conn.close()


def _take_flocks_then_commit_spend(writer, provider, db_path, root_key,
                                   source_path, ready, release):
    """Another writer (a dashboard sync, a drain worker) that takes both cache
    writer flocks after the hook's sync released them and commits more Codex
    spend before the hook's budget read."""
    import fcntl
    import os
    import sqlite3 as sq
    fds = []
    try:
        for path in (writer, provider):
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            fds.append(fd)
        conn = sq.connect(db_path, timeout=30)
        conn.execute(
            "INSERT INTO codex_session_entries (source_path,line_offset,"
            "timestamp_utc,session_id,model,input_tokens,cached_input_tokens,"
            "output_tokens,reasoning_output_tokens,total_tokens,"
            "source_root_key) VALUES (?,999999,?,'late','gpt-5',500000,0,"
            "500000,0,1000000,?)",
            (source_path, "2026-07-21T00:00:00+00:00", root_key))
        conn.commit()
        conn.close()
        ready.set()
        release.wait(60)
    finally:
        for fd in reversed(fds):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _hold_flocks(writer, provider, ready, release):
    import fcntl
    import os
    fds = []
    try:
        for path in (writer, provider):
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            fds.append(fd)
        ready.set()
        release.wait(60)
    finally:
        for fd in reversed(fds):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _spawn_holder(target, *args):
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=target, args=(*args, ready, release))
    proc.start()
    assert ready.wait(30), "the second writer never took the flocks"
    return proc, release


def _stop_holder(holder):
    proc, release = holder
    release.set()
    proc.join(30)


def test_budget_leg_contended_after_sync_evaluates_pinned_snapshot(
    tmp_path, monkeypatch,
):
    """#981 T-A1. Detects: the production 86-90 s overrun — the hook's budget
    read ran its own unbudgeted sync and, finding the flocks held by another
    writer, a direct parse of every rollout — and any evaluation on a total
    other than the one the hook's own sync made complete."""
    h = BudgetHarness(tmp_path, monkeypatch, codex_budget={
        "amount_usd": 1000.0, "period": "calendar-month",
        "alerts_enabled": True, "alert_thresholds": [90]})
    seeded = h.rollout("seed", 3)
    h.seed_sync()
    h.rollout("seed", 4)  # an appended turn for the hook's walk
    state: dict = {}

    def contend(_stats):
        state["oracle"] = h.oracle()
        state["discoveries"] = h.discoveries
        state["holder"] = _spawn_holder(
            _take_flocks_then_commit_spend,
            str(h.core.CACHE_LOCK_PATH), str(h.core.CACHE_LOCK_CODEX_PATH),
            str(h.core.CACHE_DB_PATH), h.root_key, str(seeded))

    h.after_budgeted_sync = contend
    try:
        h.hook()
        after_commit = h.oracle()
    finally:
        if "holder" in state:
            _stop_holder(state["holder"])

    assert "holder" in state, "the contention never happened after the sync"
    assert after_commit > state["oracle"], (
        "non-vacuity: the second writer's spend is committed and visible")
    assert h.syncs == ["budgeted"], (
        "the budget read ran its own (unbudgeted) Codex sync")
    assert h.direct_parses == [], "the budget read direct-parsed rollouts"
    assert h.discoveries == state["discoveries"], (
        "the budget read ran its own filesystem discovery")
    assert state["oracle"] > 0
    (start, account, total), = h.spend
    assert account is None and start == BUDGET_PERIOD_START
    assert total == pytest.approx(state["oracle"], abs=1e-12), (
        "the ladder did not evaluate the generation the hook's sync pinned")
    fields = h.log()[-1]
    assert (fields["sync"], fields["result"]) == ("ok", "success")
    assert h.marker().is_file()


def _seed_quota_rollout(h):
    """A rollout carrying a quota observation, ingested unbudgeted (which
    builds the quota natural-key index)."""
    path = h.root / "sessions" / "quota.jsonl"
    records = token_records(1, session_id="quota")
    records.append({
        "timestamp": "2026-07-20T11:00:00.000Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {"rate_limits": {
            "limit_id": "codex", "limit_name": None, "plan_type": "pro",
            "primary": {"used_percent": 10.0, "window_minutes": 10080,
                        "resets_at": int(dt.datetime(
                            2026, 7, 27, tzinfo=dt.timezone.utc).timestamp())},
        }}}})
    write_rollout(path, records)
    return path, records


def _make_state(h, kind):
    """Drive one ineligible state through real inputs at the hook boundary."""
    cache = h.cache
    if kind == "budget_exhausted":
        # A long new rollout and a walk clock that runs out mid-file: the
        # budget stops the walk with a backlog.
        h.rollout("long", 12)
        ticks = {"now": -1.0}

        def clock():
            ticks["now"] += 1.0
            return ticks["now"]

        h.monkeypatch.setattr(cache, "_walk_clock", clock)
        return "budget_ingest_incomplete"
    if kind == "prune_debt":
        (h.root / "sessions" / "gone.jsonl").unlink()
        return "budget_prune_debt"
    if kind == "replay_pending":
        conn = h.cache_conn()
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) "
                     "VALUES(?, '1')", (cache.CODEX_REPLAY_FROM_ZERO_KEY,))
        conn.commit()
        conn.close()
        return "budget_replay_pending"
    if kind == "inventory_pending":
        conn = h.cache_conn()
        conn.execute("UPDATE cache_meta SET value='bootstrap:files:0' "
                     "WHERE key='codex_identity_inventory'")
        conn.commit()
        conn.close()
        return "inventory_pending"
    if kind == "quota_index_pending":
        import _cctally_journal as jr
        path, records = _seed_quota_rollout(h)
        h.seed_sync()
        (h.core.JOURNAL_DIR / ".quota-observation-keys").unlink()
        jr._QUOTA_DEDUP_DIR = None
        jr._QUOTA_DEDUP_KEYS.clear()
        jr._QUOTA_DEDUP_LOADED = False
        records.append(dict(records[-1], timestamp="2026-07-20T12:00:00.000Z"))
        write_rollout(path, records)
        return "quota_index_pending"
    if kind in ("torn_auth", "maintenance_failure"):
        # Real walk failures the hook's own budgeted sync produces (see
        # `_produce_walk_failure`), observed end to end at the hook boundary.
        _produce_walk_failure(h, kind)
        return "budget_ingest_incomplete"
    if kind == "unreadable_snapshot":
        def refuse(_db_path):
            raise sqlite3.OperationalError("unable to open database file")
        h.monkeypatch.setattr(
            cache, "_hook_snapshot_connect", refuse, raising=False)
        return "budget_cache_unavailable"
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", [
    "budget_exhausted", "prune_debt", "replay_pending", "inventory_pending",
    "quota_index_pending", "unreadable_snapshot", "contended_initial_sync",
    "torn_auth", "maintenance_failure",
])
def test_ineligible_states_defer_with_reason(tmp_path, monkeypatch, kind):
    """#981 T-A3 (hook boundary). Detects: a budget ladder evaluated on a walk
    that did not finish, behind pending prune/replay/inventory/index work or
    without a readable snapshot — or a read that syncs, direct-parses or
    reports a zero instead of deferring — and a deferral acknowledged as a
    lifecycle success."""
    h = BudgetHarness(tmp_path, monkeypatch, codex_budget={
        "amount_usd": 1000.0, "period": "calendar-month",
        "alerts_enabled": True, "alert_thresholds": [90]})
    h.rollout("seed", 3)
    h.rollout("gone", 2)
    h.seed_sync()
    holder = None
    if kind == "contended_initial_sync":
        holder = _spawn_holder(
            _hold_flocks, str(h.core.CACHE_LOCK_PATH),
            str(h.core.CACHE_LOCK_CODEX_PATH))
        expected = None
    else:
        expected = _make_state(h, kind)
    try:
        h.hook()
    finally:
        if holder is not None:
            _stop_holder(holder)

    fields = h.log()[-1]
    if expected is None:
        assert (fields["sync"], fields["result"]) == ("contended", "noop")
    else:
        assert (fields["sync"], fields["result"]) == ("deferred", "noop"), fields
        assert fields.get("reason") == expected, fields
    assert not h.marker().exists(), "a deferral acknowledged the root"
    assert [s for s in h.syncs if s == "unbudgeted"] == [], (
        "a budget read ran its own Codex sync")
    assert h.direct_parses == []
    assert h.spend == [], "a ladder was evaluated instead of deferred"
    assert h.budget_rows() == []


class _StepClock:
    """A hand-advanced walk clock (``_walk_clock`` drives finalization too)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _InterruptOnStatement:
    """A walk clock that reads 0.0 except ONCE inside each statement carrying
    ``marker``, where the finalization's progress handler sees the deadline
    pass and SQLite interrupts that read for real."""

    def __init__(self, conn, marker: str) -> None:
        self.armed = False
        self.fired = 0

        def trace(sql):
            if marker in sql:
                self.armed = True
        conn.set_trace_callback(trace)

    def __call__(self) -> float:
        if self.armed:
            self.armed = False
            self.fired += 1
            return 100.0
        return 0.0


def _produce_walk_failure(h, failure):
    """Create one walk failure from real inputs; return its stats witness."""
    cache = h.cache
    seed = h.root / "sessions" / "seed.jsonl"
    if failure == "files_failed":
        # An append the walk must read, and an I/O error reading it (EIO at
        # the container open). A second readable rollout keeps the root
        # recognized: with its only rollout unreadable the root is unproven
        # and the walk refuses the prune instead, a different state.
        h.rollout("other", 2)
        h.rollout("seed", 4)
        real_open = cache._rc.open_decoded

        def failing_open(container, **kwargs):
            if str(container.path) == str(seed):
                raise OSError(5, "Input/output error", str(seed))
            return real_open(container, **kwargs)
        h.monkeypatch.setattr(cache._rc, "open_decoded", failing_open)
        return lambda stats: stats.files_failed >= 1
    if failure == "torn_auth":
        (h.root / "auth.json").write_text('{"tokens": {"id_token": ')
        h.rollout("fresh", 2)                   # a first ingest reads auth
        return lambda stats: stats.files_deferred_torn >= 1
    if failure == "deferred_reset":
        h.rollout("seed", 1)                    # rewritten shorter in place
        return lambda stats: stats.files_deferred_reset >= 1
    if failure == "container_pending":
        compress_in_place(seed)                 # an accepted plain -> zstd
        return lambda stats: stats.container_pending >= 1
    if failure == "container_held":
        path = h.root / "sessions" / "held.jsonl"
        write_rollout(path, token_records(2, session_id="held"))
        zst = compress_in_place(path)
        whole = zst.read_bytes()
        zst.write_bytes(whole[: len(whole) // 2])   # undecodable new container
        return lambda stats: stats.container_reads_held >= 1
    if failure == "maintenance_failure":
        h.rollout("seed", 4)

        def failing_resolution(_conn):
            raise sqlite3.OperationalError("injected model resolution failure")
        h.monkeypatch.setattr(cache._cctally_db_sib,
                              "backfill_codex_quota_observed_model",
                              failing_resolution)
        return lambda stats: stats.maintenance_failed
    raise AssertionError(failure)


def _produce_finalization_debt(h, state, conn):
    """Leave finalization debt behind a certified real budgeted walk; return
    its witness over ``(stats, debt read on the pinned snapshot)``."""
    cache = h.cache
    if state == "work_items":
        # This walk's own batch writes the spans; the walk ends with too
        # little budget left to admit a finalization unit.
        h.rollout("seed", 4)
        clock = _StepClock()
        real_write = cache._write_codex_file_batch

        def write(conn, **kwargs):
            result = real_write(conn, **kwargs)
            clock.now = 4.8
            return result
        h.monkeypatch.setattr(cache, "_walk_clock", clock)
        h.monkeypatch.setattr(cache, "_write_codex_file_batch", write)
        return lambda stats, debt: bool(debt.first_item())
    h.monkeypatch.setattr(cache, "_CODEX_PROGRESS_HANDLER_STEPS", 1)
    if state == "full":
        cache.add_codex_finalization_debt(conn, full=True)
        marker = "MIN(timestamp_utc), MAX(timestamp_utc)"
        witness = lambda stats, debt: debt.full  # noqa: E731
    elif state == "attribution":
        cache.add_codex_finalization_debt(conn, attribution=True)
        marker = "FROM codex_window_attributions"
        witness = lambda stats, debt: debt.attribution  # noqa: E731
    elif state == "cancelled_stage":
        cache.add_codex_finalization_debt(
            conn, spans={h.root_key: (BUDGET_PERIOD_START, BUDGET_NOW)})
        marker = "ORDER BY timestamp_utc, id LIMIT"
        witness = lambda stats, debt: bool(debt.first_item())  # noqa: E731
    else:
        raise AssertionError(state)
    conn.commit()
    clock = _InterruptOnStatement(conn, marker)
    h.monkeypatch.setattr(cache, "_walk_clock", clock)
    return lambda stats, debt: clock.fired >= 1 and witness(stats, debt)


_WALK_FAILURES = ("files_failed", "torn_auth", "deferred_reset",
                  "container_pending", "container_held", "maintenance_failure")
_FINALIZATION_DEBT = ("work_items", "full", "attribution", "cancelled_stage")


@pytest.mark.parametrize("state", [*_WALK_FAILURES, *_FINALIZATION_DEBT])
def test_real_budgeted_sync_states_pin_vendor_and_account_eligibility(
    tmp_path, monkeypatch, state,
):
    """#981 T-A3 (pin boundary) and A3's split, each state produced by a real
    budgeted ``sync_codex_cache`` (real inputs, or a failing finalization
    statement / a real SQLite interrupt) and pinned by the real hook read
    policy. Detects: a ladder evaluated on a walk whose failure production
    did not translate into an uncertified walk, and -- the review's N1 --
    per-account spend evaluated while finalization debt can still move
    ``account_key``, or vendor-wide spend deferred for debt that never
    changes a row's cost."""
    h = BudgetHarness(tmp_path, monkeypatch, codex_budget={
        "amount_usd": 1000.0, "period": "calendar-month",
        "alerts_enabled": True, "alert_thresholds": [90]})
    h.rollout("seed", 3)
    h.seed_sync()
    cache = h.cache
    conn = h.ns["open_cache_db"]()
    try:
        if state in _WALK_FAILURES:
            witness = _produce_walk_failure(h, state)
        else:
            witness = _produce_finalization_debt(h, state, conn)
        with cache.hook_codex_read_policy() as policy:
            try:
                stats = cache.sync_codex_cache(conn, budget_seconds=5.0)
            finally:
                conn.set_trace_callback(None)
            assert policy.snapshot is not None, "the sync pinned a snapshot"
            if state in _WALK_FAILURES:
                assert witness(stats), (f"non-vacuity: {state} occurred", {k: v for k, v in vars(stats).items() if v})
                assert stats.walk_certified is False
                expected = ("ingest_incomplete", "ingest_incomplete")
            else:
                debt = cache.load_codex_finalization_debt(policy.snapshot)
                assert witness(stats, debt), f"non-vacuity: {state} remains"
                assert stats.walk_certified is True
                assert stats.finalization_deferred is True
                expected = (None, "finalization_debt")
            assert (policy.reason, policy.account_reason) == expected
            with pytest.raises(cache.CodexReadDeferred) as deferred:
                cache.get_codex_entries(
                    BUDGET_PERIOD_START, BUDGET_NOW, account_key="acct")
            assert deferred.value.reason == expected[1]
            if expected[0] is None:
                entries = cache.get_codex_entries(
                    BUDGET_PERIOD_START, BUDGET_NOW)
                assert len(entries) >= 3, "vendor-wide spend read the snapshot"
            else:
                with pytest.raises(cache.CodexReadDeferred) as deferred:
                    cache.get_codex_entries(BUDGET_PERIOD_START, BUDGET_NOW)
                assert deferred.value.reason == expected[0]
    finally:
        conn.close()
    assert h.syncs == ["budgeted"] and h.direct_parses == []
    assert cache._active_hook_codex_read_policy() is None
