"""#968 T4/T4b: the hook never deletes; the prune drains per identity, fairly."""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from conftest import as_process_owner, load_script, redirect_paths  # noqa: E402
from _codex_zstd_fixtures import token_records, write_rollout, zstd_bytes  # noqa: E402
from test_codex_hook_lifecycle import _hook_args  # noqa: E402

import _lib_codex_drain_admission as adm  # noqa: E402

ORPHANS = 3


class Clock:
    def __init__(self):
        self.now = 50_000_000_000_000

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _no_detached_drains(monkeypatch):
    """Patch each fresh sibling, including loads through imported aliases."""
    import conftest
    import subprocess
    import sys

    real_load = conftest.load_script_module
    real_popen = subprocess.Popen
    escaped = []

    def patch_current():
        update = sys.modules.get("_cctally_update")
        if update is not None:
            monkeypatch.setattr(update, "_spawn_detached", lambda command: True)

    def load_without_detaching(*args, **kwargs):
        module = real_load(*args, **kwargs)
        patch_current()
        return module

    def guard_detached(*popenargs, **kwargs):
        argv = popenargs[0] if popenargs else kwargs.get("args")
        if (kwargs.get("start_new_session") and isinstance(argv, (list, tuple))
                and argv and str(argv[-1]).startswith("_codex-")):
            escaped.append(tuple(argv))
            raise AssertionError("a Codex detached worker escaped test isolation")
        return real_popen(*popenargs, **kwargs)

    # Every conftest.load_script alias resolves this primitive at call time;
    # wrapping only this module's load_script name would miss helper-owned loads.
    monkeypatch.setattr(conftest, "load_script_module", load_without_detaching)
    monkeypatch.setattr(subprocess, "Popen", guard_detached)
    patch_current()
    yield
    assert not escaped, f"detached Codex worker attempts: {escaped!r}"


@pytest.fixture
def estate(tmp_path, monkeypatch):
    """One recognizable root; ORPHANS legitimately deleted rollouts plus a keeper."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    monkeypatch.setitem(ns, "_hook_tick_read_stdin_event", lambda: {
        "event": "Stop", "session_id": "codex-tick",
        "transcript_path": "", "cwd": ""})
    spawned: list[str] = []
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: spawned.append(command) or True)
    clock = Clock()
    monkeypatch.setattr(adm, "_monotonic_ns", clock)
    paths = []
    for i in range(ORPHANS + 1):
        p = day / f"rollout-2026-07-20T1{i}-00-00-0199aaaa-bbbb-7ccc-8ddd-eeeeffff00{i:02d}.jsonl"
        records = token_records(2 + i, session_id=f"s{i}")
        # All four identities claim the same quota partition. Each deletion
        # removes its current maximum, exposing the next retained maximum.
        stamp = f"2026-07-20T10:{50 - i:02d}:00.000Z"
        records.extend([
            {"timestamp": stamp, "type": "event_msg", "payload": {
                "type": "token_count", "info": {"rate_limits": {
                    "limit_id": "codex", "limit_name": "codex",
                    "primary": {"used_percent": 10 + i,
                                "window_minutes": 10080,
                                "resets_at": 1785150000}}}}},
        ])
        write_rollout(p, records)
        paths.append(p)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    for p in paths[:ORPHANS]:
        p.unlink()
    return ns, paths, spawned, clock


def _cache(ns):
    return ns["open_cache_db"]()


def _count(ns, sql, params=()):
    conn = _cache(ns)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _seq(ns):
    return int(_count(ns, "SELECT COALESCE(MAX(CAST(value AS INTEGER)),0) "
                          "FROM cache_meta WHERE key='codex_physical_mutation_seq'"))


def _requests(ns):
    return adm.fresh_requests(ns["APP_DIR"])


def _cache_mod():
    return sys.modules["_cctally_cache"]


def _executor():
    """The prune executor. Bound FIRST in every executor test (review P3-8),
    so on the tree this task starts from the recorded RED is the executor's
    absence (`AttributeError: ... run_codex_prune_drain`), never an earlier
    walk-side failure."""
    return _cache_mod().run_codex_prune_drain


@pytest.fixture
def debt_estate(estate):
    """``estate`` with the ORPHANS recorded as prune debt directly, without
    running the walk: the executor tests then depend on nothing the walk-side
    change provides (on 92e06df85 a walk here would already have deleted the
    orphans inline)."""
    ns, paths, spawned, clock = estate
    conn = _cache(ns)
    try:
        for p in paths[:ORPHANS]:
            conn.execute(
                "INSERT INTO codex_prune_debt (source_path, source_root_key, "
                "recorded_at_utc) SELECT path, COALESCE(source_root_key, ''), "
                "'2026-07-20T00:00:00+00:00' FROM codex_session_files "
                "WHERE path=?", (str(p),))
        conn.commit()
    finally:
        conn.close()
    assert _count(ns, "SELECT COUNT(*) FROM codex_prune_debt") == ORPHANS
    return ns, paths, spawned, clock
def test_a_due_hook_records_debt_and_deletes_nothing(estate):
    ns, paths, spawned, _clock = estate
    entries_before = _count(ns, "SELECT COUNT(*) FROM codex_session_entries")
    assert ns["cmd_hook_tick"](_hook_args()) == 0
    # 92e06df85 deletes every orphan's rows inline inside the hook.
    assert _count(ns, "SELECT COUNT(*) FROM codex_session_entries") == entries_before
    assert _count(ns, "SELECT COUNT(*) FROM codex_prune_debt") == ORPHANS
    assert spawned == ["_codex-prune-drain"]


# These tables constitute this fixture's applicable retained tuple. The
# accounting/journal attribution history is append-only and is checked below
# through the mutation ledgers rather than expected to disappear.
_IDENTITY_TABLES = {
    "codex_session_entries": "source_path",
    "quota_window_snapshots": "source_path",
    "codex_conversation_events": "source_path",
    "codex_conversation_threads": "source_path",
    "codex_conversation_messages": "source_path",
    "codex_conversation_file_touches": "source_path",
    "codex_session_files": "path",
    "codex_conversation_rollups": "conversation_key",
    "codex_prune_debt": "source_path",
}
_COVERAGE_KEYS = ("codex_journal_coverage_certificate",
                  "codex_journal_coverage_progress")


def _rows(conn, table):
    return sorted((tuple(row) for row in conn.execute(f"SELECT * FROM {table}")),
                  key=repr)


def _seed_coverage(ns):
    """Durable, well-shaped nonempty records; both must die on EVERY delete."""
    import _lib_cache_coverage as coverage

    conn = _cache(ns)
    try:
        vector = (("fixture-segment", 100, 100),)
        cert = coverage.advance(
            None, covered=("fixture-segment", 100),
            applied_through=("fixture-segment", 100), pinned_vector=vector,
            physical_seq=_seq(ns))
        progress = coverage.make_progress(
            pass_id="fixture-pass", started_at=1, chunks=1,
            identity_root=coverage.identity_root(vector),
            physical_seq=_seq(ns), source_roots_digest=coverage.source_roots_digest(
                row[0] for row in conn.execute(
                    "SELECT source_root_key FROM codex_source_roots")),
            covered=("fixture-segment", 100))
        for key, value in zip(_COVERAGE_KEYS, (cert, progress)):
            conn.execute("INSERT OR REPLACE INTO cache_meta VALUES (?,?)",
                         (key, json.dumps(value, sort_keys=True)))
        conn.commit()
        assert _cache_mod().load_codex_journal_coverage_certificate(conn) == cert
        assert _cache_mod().load_codex_recovery_progress(conn) == progress
    finally:
        conn.close()


def _assert_partition_latest_matches_recomputation(ns):
    # Independent Python reduction over the raw observations. Do not call the
    # production summary bootstrap: the same mistake could affect both paths.
    conn = _cache(ns)
    try:
        cursor = conn.execute("SELECT * FROM quota_window_snapshots")
        names = [column[0] for column in cursor.description]
        maxima = {}
        for values in cursor:
            row = dict(zip(names, values))
            assert row["source"] == "codex"  # this fixture has no other provider
            cols = ("source_root_key", "logical_limit_key", "observed_slot",
                    "window_minutes", "limit_id", "limit_name", "observed_model",
                    "account_key")
            raw_key = tuple(row[col] for col in cols) + (
                row["canonical_resets_at_utc"] or row["resets_at_utc"],)
            # The stored keys deliberately use SQLite quote() encoding.
            key = tuple(conn.execute("SELECT quote(?)", (v,)).fetchone()[0]
                        for v in raw_key)
            epoch = int(dt.datetime.fromisoformat(
                row["captured_at_utc"].replace("Z", "+00:00")).timestamp())
            maxima[key] = max(maxima.get(key, epoch), epoch)
        assert maxima, "quota recomputation must remain nonempty (keeper survives)"
        expected = sorted((key + (epoch,) for key, epoch in maxima.items()), key=repr)
        assert _rows(conn, "codex_quota_partition_latest") == expected, (
            "partition-latest differs from independent raw-observation maxima")
    finally:
        conn.close()


def _seed_legacy_cache_conversations(ns):
    """Exercise the legacy cache families the prune still has to remove.

    Current cache ingestion writes accounting/quota/thread metadata only;
    conversations migration 004 owns find projection in the other store.
    Seed the retained legacy event families here and use the existing legacy
    replay seam to derive messages, touches and rollups in this cache schema.
    """
    conn = _cache(ns)
    try:
        for path, root, key, native, root_thread in conn.execute(
                "SELECT source_path,source_root_key,conversation_key,"
                "native_thread_id,root_thread_id FROM codex_conversation_threads"
        ).fetchall():
            records = [
                ("response_item", "message", {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": f"Keep {native}"}]}),
                ("event_msg", "patch_apply_end", {"type": "patch_apply_end",
                    "changes": {f"{native}.txt": {"type": "add", "content": "kept\n"}}}),
            ]
            for offset, (family, event_type, payload) in enumerate(records, 100000):
                conn.execute(
                    "INSERT INTO codex_conversation_events "
                    "(source_path,line_offset,source_root_key,conversation_key,"
                    "native_thread_id,root_thread_id,timestamp_utc,record_type,"
                    "event_type,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (path, offset, root, key, native, root_thread,
                     "2026-07-20T10:45:00Z", family, event_type,
                     json.dumps({"type": family, "payload": payload})))
        _cache_mod()._replay_codex_normalization(conn)
        conn.commit()
    finally:
        conn.close()


class _CommittedPrefix:
    """Observe durable state through a fresh connection at identity boundaries."""
    def __init__(self, ns, paths):
        self.ns = ns
        self.paths = [str(p) for p in paths]
        self.committed = []
        self.seq0 = _seq(ns)
        _seed_legacy_cache_conversations(ns)
        conn = _cache(ns)
        try:
            self.initial = {t: _rows(conn, t) for t in _IDENTITY_TABLES}
            self.columns = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                            for t in _IDENTITY_TABLES}
            self.keys = {p: {r[0] for r in conn.execute(
                "SELECT conversation_key FROM codex_conversation_messages "
                "WHERE source_path=?", (p,))} for p in self.paths}
            for table, rows in self.initial.items():
                assert rows, f"nonempty {table} is required for this oracle"
            for path in self.paths:
                for table, column in _IDENTITY_TABLES.items():
                    if table == "codex_prune_debt" and path == self.paths[-1]:
                        continue
                    index = self.columns[table].index(column)
                    values = self.keys[path] if column == "conversation_key" else {path}
                    assert any(row[index] in values for row in self.initial[table]), (
                        table, path)
            self.accounting0 = _rows(conn, "codex_accounting_change_log")
            self.quota0 = _rows(conn, "quota_window_change_log")
            self.accounting_seq0 = int(conn.execute(
                "SELECT value FROM cache_meta WHERE key='codex_accounting_mutation_seq'"
            ).fetchone()[0])
        finally:
            conn.close()
        _assert_partition_latest_matches_recomputation(ns)
        _seed_coverage(ns)

    def assert_state(self, *, coverage_present):
        ns = self.ns
        assert _seq(ns) == self.seq0 + len(self.committed), (
            "physical mutation sequence must advance at each committed prefix")
        conn = _cache(ns)
        try:
            deleted_keys = set().union(*(self.keys[p] for p in self.committed))
            for table, column in _IDENTITY_TABLES.items():
                index = self.columns[table].index(column)
                removed = deleted_keys if column == "conversation_key" else set(self.committed)
                expected = [r for r in self.initial[table] if r[index] not in removed]
                assert _rows(conn, table) == expected, (table, self.committed)
            coverage = conn.execute(
                "SELECT key,value FROM cache_meta WHERE key IN (?,?) ORDER BY key",
                _COVERAGE_KEYS).fetchall()
            assert len(coverage) == (2 if coverage_present else 0), (
                "durable coverage certificate AND recovery progress must be invalidated",
                coverage)
            entries = self.initial["codex_session_entries"]
            entry_path = self.columns["codex_session_entries"].index("source_path")
            removed_entries = [r for r in entries if r[entry_path] in self.committed]
            accounting = _rows(conn, "codex_accounting_change_log")
            delta = [r for r in accounting if r not in self.accounting0]
            assert len(delta) == len(removed_entries)
            assert collections.Counter(r[4] for r in delta) == collections.Counter(
                r[entry_path] for r in removed_entries)
            assert all(r[2] == "path" for r in delta)
            assert sorted(r[1] for r in delta) == list(range(
                self.accounting_seq0 + 1, self.accounting_seq0 + len(delta) + 1))
            quota_cols = self.columns["quota_window_snapshots"]
            quota_path = quota_cols.index("source_path")
            group = ("source_root_key", "logical_limit_key", "observed_slot",
                     "window_minutes", "resets_at_utc", "canonical_resets_at_utc")
            expected_scopes = collections.Counter(
                tuple(r[quota_cols.index(c)] for c in group)
                for r in self.initial["quota_window_snapshots"]
                if r[quota_path] in self.committed)
            quota_delta = [r for r in _rows(conn, "quota_window_change_log")
                           if r not in self.quota0]
            assert all(r[1] == "delete" and r[8:] == (None,) * 6
                       for r in quota_delta)
            assert collections.Counter(r[2:8] for r in quota_delta) == expected_scopes
        finally:
            conn.close()
        _assert_partition_latest_matches_recomputation(ns)

    def durable_snapshot(self):
        conn = _cache(self.ns)
        try:
            tables = (*_IDENTITY_TABLES, "codex_quota_partition_latest",
                      "codex_accounting_change_log", "quota_window_change_log", "cache_meta")
            return {table: _rows(conn, table) for table in tables}
        finally:
            conn.close()


def test_the_worker_drains_one_identity_per_commit_with_consistent_state(
        debt_estate, monkeypatch):
    drain = _executor()
    ns, paths, _spawned, _clock = debt_estate
    oracle = _CommittedPrefix(ns, paths)
    pending = []
    observed = []

    def precommit(path, root_key):
        # The uncommitted next identity must still be wholly intact for readers.
        oracle.assert_state(coverage_present=True)
        pending.append(path)
        observed.append((path, _seq(ns)))

    def after_finish():
        if not pending:  # the separate final root cleanup also releases admission
            return
        oracle.committed.append(pending.pop())
        oracle.assert_state(coverage_present=False)
        if len(oracle.committed) < ORPHANS:
            _seed_coverage(ns)  # require invalidation again on the next identity

    monkeypatch.setattr(_cache_mod(), "_CODEX_PRUNE_PRECOMMIT_HOOK", precommit)
    monkeypatch.setattr(adm, "_after_finish", after_finish)
    result = drain(wait_for_requests=False)
    assert result.identities_deleted == ORPHANS and result.remaining == 0
    assert observed == [(str(p), oracle.seq0 + i)
                        for i, p in enumerate(paths[:ORPHANS])]
    assert oracle.committed == [str(p) for p in paths[:ORPHANS]]
    oracle.assert_state(coverage_present=False)


def test_a_kill_mid_identity_keeps_the_committed_prefix_and_the_rest_intact(
        debt_estate, monkeypatch):
    drain = _executor()
    ns, paths, _spawned, _clock = debt_estate
    oracle = _CommittedPrefix(ns, paths)
    seen = []
    committed_snapshot = []

    class Killed(BaseException):
        pass

    def precommit(path, root_key):
        oracle.assert_state(coverage_present=True)
        seen.append(path)
        if len(seen) == 2:
            raise Killed()

    def after_finish():
        if len(seen) == 1:
            oracle.committed.append(seen[0])
            oracle.assert_state(coverage_present=False)
            _seed_coverage(ns)
            committed_snapshot.append(oracle.durable_snapshot())

    monkeypatch.setattr(_cache_mod(), "_CODEX_PRUNE_PRECOMMIT_HOOK", precommit)
    monkeypatch.setattr(adm, "_after_finish", after_finish)
    with pytest.raises(Killed):
        drain(wait_for_requests=False)
    assert seen == [str(p) for p in paths[:2]]
    assert oracle.committed == [seen[0]]
    oracle.assert_state(coverage_present=True)
    # Includes every retained tuple, debt, both ledgers, summary, sequence,
    # certificate and recovery progress: the interrupted transaction changed none.
    assert oracle.durable_snapshot() == committed_snapshot[0]


def test_debt_is_not_permission_a_present_candidate_is_dropped(debt_estate):
    drain = _executor()
    ns, paths, _spawned, _clock = debt_estate
    write_rollout(paths[0], token_records(2, session_id="s0"))   # it came back
    result = drain(wait_for_requests=False)
    assert result.identities_dropped == 1
    assert _count(ns, "SELECT COUNT(*) FROM codex_session_entries "
                      "WHERE source_path=?", (str(paths[0]),)) > 0


def test_a_pass_discovers_once_and_scopes_every_identity(debt_estate, monkeypatch):
    """Review N10: one discovery per drain pass, redone when the root keys
    change; the #485 scope is still computed for every identity."""
    drain = _executor()
    ns, _paths, _spawned, _clock = debt_estate
    cache_mod = _cache_mod()
    calls = {"discover": 0, "scope": 0}
    discover = cache_mod._discover_codex_files_with_roots
    scope = cache_mod._codex_prune_scope

    def counting_discover(**kwargs):  # #970: walks pass `skipped=`
        calls["discover"] += 1
        return discover(**kwargs)

    def counting_scope(files):
        calls["scope"] += 1
        return scope(files)

    monkeypatch.setattr(cache_mod, "_discover_codex_files_with_roots",
                        counting_discover)
    monkeypatch.setattr(cache_mod, "_codex_prune_scope", counting_scope)
    result = drain(wait_for_requests=False)
    assert result.identities_deleted == ORPHANS and result.remaining == 0
    # The pass's one discovery plus the root cleanup's own; a scope for every
    # identity plus the cleanup's.
    assert calls == {"discover": 2, "scope": ORPHANS + 1}

    cache = cache_mod._CodexPruneDiscoveryCache()
    calls["discover"] = 0
    cache.prepare()
    cache.prepare()
    assert calls["discover"] == 1
    real_roots = cache_mod._codex_provider_roots
    monkeypatch.setattr(cache_mod, "_codex_provider_roots",
                        lambda: real_roots()[:0])     # the root set changed
    cache.prepare()
    assert calls["discover"] == 2


def test_debt_only_full_walks_are_held_and_rewalk_when_the_debt_clears(
        debt_estate, monkeypatch):
    """Review N3 (spec 5.7, T7 "no perpetual full walk"): pending debt
    withholds the certificate, so a dashboard re-planned a full walk on every
    tick until the drain finished. A walk clean except for that debt now
    leaves a token, and the frontier walks such a store at most once per
    FRONTIER_DEBT_HOLD_SECONDS, and again at once when the debt reads empty."""
    drain = _executor()
    import _lib_ingest_frontier as frontier  # noqa: PLC0415
    ns, _paths, _spawned, _clock = debt_estate
    now = [1_000_000.0]
    monkeypatch.setattr(frontier, "_now", lambda: now[0])
    roots = tuple(r.walk_root for r in _cache_mod()._codex_provider_roots())
    state = frontier.DashboardIngestFrontier(ns["APP_DIR"] / "frontier-n3")
    walks = []
    walked_at = [None]          # the clock at the last full plan

    def tick(advance=10.0):
        now[0] += advance
        conn = _cache(ns)
        try:
            plan = state.plan_provider("codex", conn, roots=roots)
            if plan.mode == "full":
                walked_at[0] = now[0]
                stats = ns["sync_codex_cache"](conn, maintenance="caller")
                walks.append(stats)
                if frontier.provider_sync_certifiable("full", stats):
                    assert state.seed_provider("codex", conn, roots=roots), (
                        state.last_seed_failure)
            return plan
        finally:
            conn.close()

    plans = [tick() for _ in range(6)]
    assert [p.mode for p in plans] == ["full"] + ["caught_up"] * 5, plans
    assert {p.reason for p in plans[1:]} == {"debt_hold"}
    assert len(walks) == 1 and walks[0].prune_debt_pending == ORPHANS
    assert walks[0].full_walk_debt_only and not walks[0].full_walk_complete
    # The hold lasts no longer than a certificate may (review r3 F5): one
    # second short of FRONTIER_DEBT_HOLD_SECONDS after the full plan the store
    # is still held, and at exactly FRONTIER_DEBT_HOLD_SECONDS it is walked.
    period = frontier.FRONTIER_DEBT_HOLD_SECONDS
    assert tick(walked_at[0] + period - 1.0 - now[0]).reason == "debt_hold"
    assert now[0] - walked_at[0] == period - 1.0 and len(walks) == 1
    assert tick(1.0).mode == "full"
    assert len(walks) == 2 and walks[1].full_walk_debt_only
    assert tick().reason == "debt_hold"
    assert drain(wait_for_requests=False).remaining == 0
    assert _count(ns, "SELECT COUNT(*) FROM codex_prune_debt") == 0
    # An empty debt table ends the hold at once, and that walk certifies.
    assert tick().mode == "full"
    assert len(walks) == 3 and walks[2].full_walk_complete
    assert not walks[2].full_walk_debt_only
    assert _count(ns, "SELECT COUNT(*) FROM cache_meta WHERE key=?",
                  (frontier.CODEX_FULL_WALK_DEBT_ONLY_KEY,)) == 0
    assert tick().mode == "caught_up" and len(walks) == 3
def test_fairness_schedule_conditional_yield(debt_estate, monkeypatch):
    drain = _executor()
    ns, paths, _spawned, clock = debt_estate
    cache_mod = _cache_mod()
    verification = paths[-1].with_name(paths[-1].name.replace("0003", "0005"))
    write_rollout(verification, token_records(2, session_id="verify"))
    conn = _cache(ns)
    try:
        ns["sync_codex_cache"](conn, maintenance="caller")
        verification.with_name(verification.name + ".zst").write_bytes(
            zstd_bytes(verification.read_bytes()))
        verification.unlink()
        ns["sync_codex_cache"](conn, maintenance="caller")
    finally:
        conn.close()
    # One absolute 30-second budget covers this entire deterministic schedule.
    deadline = time.monotonic() + 30.0

    def remaining():
        return max(0.0, deadline - time.monotonic())

    hold = threading.Event()
    in_txn = threading.Event()
    released = threading.Event()
    starts: list[str] = []
    wake = threading.Condition()
    pokes = [0]          # broadcast epoch: every worker observes each wake
    sleep_seen = {}

    def poke():
        with wake:
            pokes[0] += 1
            wake.notify_all()

    def precommit(path, root_key):
        starts.append(path)
        in_txn.set()
        assert hold.wait(remaining())

    def fake_sleep(_seconds):
        # Each worker consumes a broadcast epoch once. A release that lands
        # before either worker sleeps remains visible to that worker; one
        # worker cannot consume the other's wake and strand it after draining.
        # A fresh-request sleep still ends only after every request expires.
        with wake:
            for_requests = bool(_requests(ns))
            worker_id = threading.get_ident()
            seen = sleep_seen.get(worker_id, 0)
            assert wake.wait_for(
                (lambda: not _requests(ns)) if for_requests
                else (lambda: pokes[0] > seen), timeout=remaining())
            sleep_seen[worker_id] = pokes[0]

    def after_finish():
        released.set()
        poke()

    monkeypatch.setattr(cache_mod, "_CODEX_PRUNE_PRECOMMIT_HOOK", precommit)
    monkeypatch.setattr(adm, "_sleep", fake_sleep)
    # Review P2-7: synchronize on the lock release itself (the leaf's
    # post-release seam), never on a sleep or a row-count poll.
    monkeypatch.setattr(adm, "_after_finish", after_finish)
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    # Each drain is a detached `cctally` process owner (#787 / #861).
    worker = threading.Thread(
        target=as_process_owner(drain), kwargs={"wait_for_requests": True},
        daemon=True)
    worker.start()
    # 1. the oversized identity's transaction is admitted and held open
    assert in_txn.wait(remaining())
    in_txn.clear()
    verify_errors = []

    def verify():
        try:
            cache_mod.run_codex_container_verification_drain(wait_for_requests=True)
        except BaseException as exc:
            verify_errors.append(exc)
            poke()

    verifier = threading.Thread(target=as_process_owner(verify), daemon=True)
    verifier.start()
    # 2. hook A: contended no-op, request published, no success stamp
    assert ns["cmd_hook_tick"](_hook_args()) == 0
    assert len(_requests(ns)) == 1
    assert not any(marker_dir.glob("*.last-success"))
    # 3. hook B, still before the commit: another contended no-op
    clock.now += 15_000_000_000
    assert ns["cmd_hook_tick"](_hook_args()) == 0
    assert len(_requests(ns)) == 2
    # 4. finish the admitted work; locks released, no new identity while fresh
    released.clear()
    hold.set()
    assert released.wait(remaining())          # store locks AND gate released (finish)
    hold.clear()
    assert _count(ns, "SELECT COUNT(*) FROM codex_prune_debt") == ORPHANS - 1
    # `timeout=None` is ONE non-blocking attempt; after the seam fired it
    # cannot race the worker, which re-checks requests before any lock.
    held = sys.modules["_lib_cache_writer_lock"].acquire_cache_writer_flocks(
        sys.modules["_cctally_core"].CACHE_LOCK_PATH,
        sys.modules["_cctally_core"].CACHE_LOCK_CODEX_PATH, timeout=None)
    assert held is not None
    sys.modules["_lib_cache_writer_lock"].release_cache_writer_flocks(held)
    assert len(starts) == 1
    assert _count(ns, "SELECT COUNT(*) FROM codex_session_files WHERE path=? "
                  "AND container_state='pending'", (str(verification),)) == 1
    # 5. hook C during the yield: success, requests intact, workers yielded
    assert ns["cmd_hook_tick"](_hook_args()) == 0
    assert any(marker_dir.glob("*.last-success"))
    assert len(_requests(ns)) == 2 and len(starts) == 1
    # 6. expire every request; draining resumes; hook D overlaps -> new cycle
    clock.now += adm.REQUEST_TTL_NS + 1
    for stamp in marker_dir.glob("*.last-success"):
        os.utime(stamp, (1, 1))                    # make D due
    poke()
    assert in_txn.wait(remaining()) and len(starts) == 2
    assert ns["cmd_hook_tick"](_hook_args()) == 0
    assert len(_requests(ns)) == 1
    hold.set()
    clock.now += adm.REQUEST_TTL_NS + 1
    poke()
    worker.join(remaining())
    assert not worker.is_alive()
    verifier.join(remaining())
    assert not verifier.is_alive() and not verify_errors
    assert _count(ns, "SELECT COUNT(*) FROM codex_session_files WHERE path=? "
                  "AND container_state='accepted'", (str(verification),)) == 1


def test_a_request_landing_between_check_and_lock_stops_admission(
        debt_estate, monkeypatch):
    """T4 step 7: a missing admission recheck is a defect. The request lands
    AFTER the first check passed and before any store lock is taken (review
    P2-6): only the after-gate recheck can stop the worker."""
    drain = _executor()
    ns, _paths, _spawned, _clock = debt_estate
    cache_mod = _cache_mod()
    original_admit = adm.admit
    stages: list[str] = []
    lock_calls = {"n": 0}
    real_locks = cache_mod._cache_store_locks

    def counting_locks():
        lock_calls["n"] += 1
        return real_locks()

    def racing_admit(app_dir, try_locks, release_locks, *, on_check=None):
        def stage(name):
            stages.append(name)
            if name == "after-gate" and stages.count("after-gate") == 1:
                # `on_check` runs before the check reads the requests, so the
                # first check ("before-gate") already passed with none fresh.
                adm.publish_waiting_request(app_dir, root_keys=[])
        return original_admit(app_dir, try_locks, release_locks, on_check=stage)

    monkeypatch.setattr(cache_mod, "_cache_store_locks", counting_locks)
    monkeypatch.setattr(adm, "admit", racing_admit)
    result = drain(wait_for_requests=False)
    assert stages[:2] == ["before-gate", "after-gate"]
    assert "after-store-locks" not in stages
    assert lock_calls["n"] == 0                     # no lock taken after the request
    assert result.identities_deleted == 0 and result.yielded >= 1
    assert _count(ns, "SELECT COUNT(*) FROM codex_prune_debt") == ORPHANS
    gate = adm.DrainGate(ns["APP_DIR"])
    assert gate.try_acquire()                       # the gate was released
    gate.release()


def test_a_request_landing_after_the_store_locks_stops_admission(
        debt_estate, monkeypatch):
    """Review P2-6: the after-store-locks recheck, through the executor. The
    request lands after the store locks are taken and before any durable work;
    the worker releases the locks and the gate and deletes nothing."""
    drain = _executor()
    ns, _paths, _spawned, _clock = debt_estate
    cache_mod = _cache_mod()
    real_locks = cache_mod._cache_store_locks
    stages: list[str] = []
    original_admit = adm.admit

    def locks_then_request():
        token = real_locks()
        if token is not None and not stages.count("published"):
            stages.append("published")
            adm.publish_waiting_request(ns["APP_DIR"], root_keys=[])
        return token

    def recording_admit(app_dir, try_locks, release_locks, *, on_check=None):
        return original_admit(app_dir, try_locks, release_locks,
                              on_check=stages.append)

    monkeypatch.setattr(cache_mod, "_cache_store_locks", locks_then_request)
    monkeypatch.setattr(adm, "admit", recording_admit)
    result = drain(wait_for_requests=False)
    assert stages[:4] == ["before-gate", "after-gate", "published",
                          "after-store-locks"]
    assert result.identities_deleted == 0 and result.yielded >= 1
    assert _count(ns, "SELECT COUNT(*) FROM codex_prune_debt") == ORPHANS
    lock = sys.modules["_lib_cache_writer_lock"]
    held = lock.acquire_cache_writer_flocks(
        sys.modules["_cctally_core"].CACHE_LOCK_PATH,
        sys.modules["_cctally_core"].CACHE_LOCK_CODEX_PATH, timeout=None)
    assert held is not None                         # the store locks were released
    lock.release_cache_writer_flocks(held)
    gate = adm.DrainGate(ns["APP_DIR"])
    assert gate.try_acquire()
    gate.release()
def _run_hook(ns, provider, data_dir):
    env = dict(os.environ, CCTALLY_DATA_DIR=str(data_dir), CODEX_HOME=str(provider),
               CCTALLY_DISABLE_TELEMETRY="1", HOME=str(data_dir.parent / "home"))
    (data_dir.parent / "home").mkdir(exist_ok=True)
    started = time.monotonic()
    # Execute the real CLI entrypoint in its own Python process, with detached
    # spawning stubbed there too: the parent process's monkeypatch cannot cross
    # exec, and otherwise this fixture starts an uncontrolled prune worker.
    bootstrap = ("import runpy,sys; sys.path.insert(0,sys.argv[1]); "
                 "import _cctally_update; "
                 "_cctally_update._spawn_detached=lambda command: True; "
                 "sys.argv=sys.argv[2:]; "
                 "runpy.run_path(sys.argv[0],run_name='__main__')")
    proc = subprocess.run(
        [sys.executable, "-c", bootstrap, str(REPO_ROOT / "bin"),
         str(REPO_ROOT / "bin" / "cctally"), "hook-tick",
         "--source", "codex", "--foreground"],
        input=json.dumps({"hook_event_name": "Stop", "session_id": "t",
                          "transcript_path": "", "cwd": ""}),
        capture_output=True, text=True, env=env, timeout=60)
    return proc, time.monotonic() - started


def test_hook_wall_time_stays_within_u4_overlapping_a_worker_and_alone(
        estate, monkeypatch, tmp_path):
    ns, _paths, _spawned, _clock = estate
    data_dir = ns["APP_DIR"]
    provider = pathlib.Path(os.environ["CODEX_HOME"])
    cache_mod = _cache_mod()
    deletes = {"n": 0}
    original = cache_mod._delete_codex_file_derived_rows

    def slow_delete(*args, **kwargs):
        deletes["n"] += 1
        # injected-workload: codex-prune-deletion, exactly 3s; hook and nested-sync assertions must prove no inline deletion.
        time.sleep(3.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(cache_mod, "_delete_codex_file_derived_rows", slow_delete)
    # Uncontended: the nested sync records debt within its budget, deletes nothing.
    conn = _cache(ns)
    try:
        t0 = time.monotonic()
        cache_mod.sync_codex_cache(conn, lock_timeout=0, budget_seconds=5.0,
                                   quota_reconcile="defer")
        assert time.monotonic() - t0 < 5.0 + 1.0
    finally:
        conn.close()
    assert deletes["n"] == 0
    proc, wall = _run_hook(ns, provider, data_dir)
    assert proc.returncode == 0 and wall <= 20.0
    # Keep the existing due computation: age only this fixture's first
    # subprocess success so the second whole-process hook is genuinely due.
    for stamp in (data_dir / "codex-hook-tick").glob("*.last-success"):
        os.utime(stamp, (1, 1))
    # Overlapping a worker transaction held open in this process.
    hold = threading.Event()
    in_txn = threading.Event()
    monkeypatch.setattr(cache_mod, "_CODEX_PRUNE_PRECOMMIT_HOOK",
                        lambda p, r: (in_txn.set(), hold.wait(60)))
    worker = threading.Thread(
        target=as_process_owner(cache_mod.run_codex_prune_drain),
        kwargs={"wait_for_requests": False}, daemon=True)
    worker.start()
    assert in_txn.wait(30)
    proc, wall = _run_hook(ns, provider, data_dir)
    hold.set()
    worker.join(60)
    assert proc.returncode == 0 and wall <= 20.0
    log = pathlib.Path(sys.modules["_cctally_core"].HOOK_TICK_LOG_PATH).read_text()
    assert any("sync=contended" in line and "result=noop" in line
               and "drain_request=published" in line
               for line in log.splitlines())


def test_spawn_isolation_survives_alias_and_helper_owned_reloads(
        tmp_path, monkeypatch):
    import conftest
    import test_dashboard_source_read_model as helper

    loads = (load_script, conftest.load_script, helper.load_script)
    siblings = []
    for index, load in enumerate(loads):
        ns = load()
        redirect_paths(ns, monkeypatch, tmp_path / f"load-{index}")
        update = sys.modules["_cctally_update"]
        assert all(update is not prior for prior in siblings)
        siblings.append(update)
        # The real handoff reaches the current sibling's stub. The autouse
        # Popen guard records and rejects any actual detached-worker attempt.
        assert _cache_mod()._request_codex_drain_worker(
            "_codex-prune-drain") == "spawned"


# ── #970 T5: archive moves, strict absence and the deletion witness ─────────

def _archive_name(i: int, day: str = "20") -> str:
    return (f"rollout-2026-07-{day}T0{i}-00-00-"
            f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff09{i:02d}.jsonl")


def _archive_records(i: int, turns: int = 3) -> list:
    """A rollout that feeds every family: accounting, quota (a weekly
    account-level window) and a normalized conversation. Fewer ``turns`` is
    a byte prefix of more."""
    from _codex_zstd_fixtures import conversation_records
    records = conversation_records(turns, session_id=f"0199aaaa-arch-{i:04d}",
                                   label=f"arch{i}")
    for record in records:
        payload = record.get("payload", {})
        if payload.get("type") == "token_count":
            payload["info"]["rate_limits"] = {
                "limit_id": "codex", "limit_name": "codex",
                "primary": {"used_percent": 20 + i, "window_minutes": 10080,
                            "resets_at": 1785150000}}
    return records


def _sync_both(ns):
    conn = _cache(ns)
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
    finally:
        conv.close()


def _drain_both():
    cc = _cache_mod()
    return (cc.run_codex_prune_drain(wait_for_requests=False),
            cc.run_codex_conversation_prune_drain(wait_for_requests=False))


_ARCHIVE_CACHE_ROWS = (
    ("codex_session_entries", "source_path"),
    ("quota_window_snapshots", "source_path"),
    ("codex_conversation_threads", "source_path"),
)
_ARCHIVE_CONVERSATION_ROWS = (
    ("codex_conversation_messages", "source_path"),
    ("codex_conversation_events", "source_path"),
    ("codex_conversation_file_touches", "source_path"),
)


def _identity_state(ns, path) -> dict:
    """Every derived row of one identity in both stores, plus each cursor
    without ``container_path`` (the one column a move may write)."""
    out = {}
    for opener, tables, cursor in (
            (_cache, _ARCHIVE_CACHE_ROWS, "codex_session_files"),
            (lambda _ns: ns["open_conversations_db"](),
             _ARCHIVE_CONVERSATION_ROWS, "codex_conversation_source_files")):
        conn = opener(ns)
        try:
            for table, column in tables:
                out[table] = [tuple(r) for r in conn.execute(
                    f"SELECT * FROM {table} WHERE {column}=? ORDER BY rowid",
                    (str(path),))]
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({cursor})")
                    if r[1] != "container_path"]
            out[cursor] = [tuple(r) for r in conn.execute(
                f"SELECT {', '.join(cols)} FROM {cursor} WHERE path=?",
                (str(path),))]
        finally:
            conn.close()
    return out


def _debt(ns) -> "tuple[set, set]":
    conn = _cache(ns)
    try:
        cache = {r[0] for r in conn.execute(
            "SELECT source_path FROM codex_prune_debt")}
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        conversations = {r[0] for r in conv.execute(
            "SELECT source_path FROM codex_conversation_prune_debt")}
    finally:
        conv.close()
    return cache, conversations


@pytest.fixture
def archive_estate(tmp_path, monkeypatch):
    """A native home with a target and a keeper ingested from
    ``sessions/2026/07/20`` in both stores, and an ``archived_sessions``."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    archive = provider / "archived_sessions"
    archive.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(provider))
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: True)
    target, keeper = day / _archive_name(1), day / _archive_name(2)
    write_rollout(target, _archive_records(1))
    write_rollout(keeper, _archive_records(2))
    _sync_both(ns)
    state = _identity_state(ns, target)
    assert all(state[t] for t, _c in _ARCHIVE_CACHE_ROWS)
    assert all(state[t] for t in ("codex_conversation_messages",
                                  "codex_conversation_events"))
    return ns, provider, day, archive, target, keeper


def test_move_after_ingest_keeps_history(archive_estate):
    """#970 R1 (AC3): archiving a rollout cctally had ingested from
    `sessions/` turned its identity into an orphan, and both prune drains
    deleted its whole history — usage, cost, quota and conversation. After
    the move and both drains every derived row must be identical."""
    ns, _provider, _day, archive, target, _keeper = archive_estate
    before = _identity_state(ns, target)
    moved = archive / target.name
    os.rename(target, moved)
    _sync_both(ns)
    _drain_both()
    _sync_both(ns)
    after = _identity_state(ns, target)
    # 16b055cae: the drains found no container beside the logical path and
    # deleted every family (the comparison shows empty lists).
    assert after == before
    conn = _cache(ns)
    try:
        assert conn.execute(
            "SELECT container_path FROM codex_session_files WHERE path=?",
            (str(target),)).fetchone() == (str(moved),)
    finally:
        conn.close()


def test_stale_debt_for_archived_identity_is_dropped(archive_estate):
    """Debt an older binary recorded before an archive move (or for an
    archived identity it could not see) must be dropped, never acted on: the
    rollout is present in the archive, so deleting it loses its history.
    Non-regression pin: I1's root-aware selection already finds it present;
    this guards the strict-absence rewrite of both drains."""
    ns, _provider, _day, archive, target, _keeper = archive_estate
    before = _identity_state(ns, target)
    os.rename(target, archive / target.name)
    conn = _cache(ns)
    try:
        conn.execute(
            "INSERT INTO codex_prune_debt (source_path, source_root_key, "
            "recorded_at_utc) SELECT path, source_root_key, '2026-07-21' "
            "FROM codex_session_files WHERE path=?", (str(target),))
        conn.commit()
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        conv.execute(
            "INSERT INTO codex_conversation_prune_debt (source_path, "
            "source_root_key, recorded_at_utc) SELECT path, source_root_key, "
            "'2026-07-21' FROM codex_conversation_source_files WHERE path=?",
            (str(target),))
        conv.commit()
    finally:
        conv.close()
    cache_result, conv_result = _drain_both()
    assert (cache_result.identities_dropped, conv_result.identities_dropped) == (1, 1)
    assert (cache_result.identities_deleted, conv_result.identities_deleted) == (0, 0)
    assert _debt(ns) == (set(), set())
    assert _identity_state(ns, target) == before


def test_inaccessible_archive_keeps_debt_and_rows(archive_estate):
    """A rollout moved into an archive directory cctally cannot read is not
    proved absent: a lenient check reads the stat error as absence and the
    drains deleted its history. Its debt and rows must survive until the
    archive is readable again, when the debt is dropped."""
    ns, _provider, _day, archive, target, _keeper = archive_estate
    before = _identity_state(ns, target)
    os.rename(target, archive / target.name)
    os.chmod(archive, 0)
    try:
        _sync_both(ns)
        assert _debt(ns) == ({str(target)}, {str(target)})
        cache_result, conv_result = _drain_both()
        # BASE: the lenient selection read EACCES as absent and deleted.
        assert _identity_state(ns, target) == before
        assert _debt(ns) == ({str(target)}, {str(target)})
        assert cache_result.remaining == 1 and conv_result.remaining == 1
    finally:
        os.chmod(archive, 0o755)
    cache_result, conv_result = _drain_both()
    assert (cache_result.identities_dropped, conv_result.identities_dropped) == (1, 1)
    assert _identity_state(ns, target) == before


def test_dangling_archive_link_keeps_debt_and_rows(archive_estate, tmp_path):
    """#970 N2: an `archived_sessions` symlinked to a volume that is not
    mounted reads as a missing directory to a following stat, so both
    witnesses agreed on "missing", the strict probe found nothing, and both
    drains deleted the history of a rollout that was merely archived onto
    the absent volume. A dangling link is unknown, never absence: the debt
    and the rows must survive until the volume returns, when the debt is
    dropped. A directory that genuinely does not exist still witnesses as
    missing."""
    ns, _provider, _day, archive, target, _keeper = archive_estate
    cc = _cache_mod()
    assert cc._codex_directory_witness(tmp_path / "never-created") == "missing"
    before = _identity_state(ns, target)
    volume = tmp_path / "unmounted" / "archived_sessions"
    in_flight = tmp_path / "in-flight.jsonl"
    os.rename(target, in_flight)
    archive.rmdir()
    archive.symlink_to(volume)
    _sync_both(ns)
    assert _debt(ns) == ({str(target)}, {str(target)})
    cache_result, conv_result = _drain_both()
    # BASE: both witnesses read "missing", so both drains deleted.
    assert (cache_result.identities_deleted, conv_result.identities_deleted) == (0, 0)
    assert _identity_state(ns, target) == before
    assert _debt(ns) == ({str(target)}, {str(target)})
    volume.mkdir(parents=True)
    os.rename(in_flight, volume / target.name)
    cache_result, conv_result = _drain_both()
    assert (cache_result.identities_dropped, conv_result.identities_dropped) == (1, 1)
    assert _identity_state(ns, target) == before


def test_dangling_sessions_link_keeps_debt_and_rows(archive_estate, tmp_path):
    """#970 V2: the twin of N2 one level up. A rollout cctally accepted in the
    archive is unarchived back into `sessions/`, which is symlinked to a
    volume that is then unmounted. Every dated directory under the dangling
    link fails a following stat with ENOENT, so the dated witness read
    "missing" on both passes, the archive witness was unchanged, the strict
    probe found nothing, the deletion witness (the readable archive, its
    accepted location) authorized it, and both drains deleted the history of
    a rollout that was merely on the absent volume. A directory under a
    dangling link is unknown, never absence: the debt and the rows must
    survive until the volume returns, when the debt is dropped. A directory
    genuinely missing under real directories still witnesses as missing."""
    ns, provider, day, archive, target, _keeper = archive_estate
    cc = _cache_mod()
    assert cc._codex_directory_witness(day / "21" / "22") == "missing"
    before = _identity_state(ns, target)
    os.rename(target, archive / target.name)
    # An archived rollout keeps the root recognizable once `sessions/` goes.
    write_rollout(archive / _archive_name(3), _archive_records(3))
    _sync_both(ns)                       # accepted in the archive
    assert _identity_state(ns, target) == before
    os.rename(archive / target.name, target)   # unarchived onto the volume
    sessions = provider / "sessions"
    volume = tmp_path / "unmounted" / "sessions"
    in_flight = tmp_path / "in-flight-sessions"
    os.rename(sessions, in_flight)
    sessions.symlink_to(volume)          # the volume is unmounted
    # Debt recorded before the unmount outlives it.
    conn = _cache(ns)
    try:
        conn.execute(
            "INSERT INTO codex_prune_debt (source_path, source_root_key, "
            "recorded_at_utc) SELECT path, source_root_key, '2026-07-21' "
            "FROM codex_session_files WHERE path=?", (str(target),))
        conn.commit()
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        conv.execute(
            "INSERT INTO codex_conversation_prune_debt (source_path, "
            "source_root_key, recorded_at_utc) SELECT path, source_root_key, "
            "'2026-07-21' FROM codex_conversation_source_files WHERE path=?",
            (str(target),))
        conv.commit()
    finally:
        conv.close()
    assert _debt(ns) == ({str(target)}, {str(target)})
    cache_result, conv_result = _drain_both()
    # BASE: the dated witness read "missing" twice, so both drains deleted.
    assert (cache_result.identities_deleted, conv_result.identities_deleted) == (0, 0)
    assert _identity_state(ns, target) == before
    assert _debt(ns) == ({str(target)}, {str(target)})
    assert cc._codex_directory_witness(day) is None
    volume.parent.mkdir(parents=True)
    os.rename(in_flight, volume)         # the volume returns
    cache_result, conv_result = _drain_both()
    assert (cache_result.identities_dropped, conv_result.identities_dropped) == (1, 1)
    assert _identity_state(ns, target) == before


def test_unarchive_during_probe_keeps_debt(archive_estate, monkeypatch, tmp_path):
    """A move that races the probes (the dated candidates probed before the
    rename lands, the archive after it) reads as absent to an unwitnessed
    check, which deleted a rollout that was merely moving. The changed
    directory witness must keep the debt and the rows."""
    ns, _provider, day, _archive, target, _keeper = archive_estate
    before = _identity_state(ns, target)
    in_flight = tmp_path / "in-flight.jsonl"
    os.rename(target, in_flight)
    _sync_both(ns)
    assert _debt(ns) == ({str(target)}, {str(target)})
    cc = _cache_mod()
    landed: list[str] = []

    def unarchive_lands(path):
        if path == str(target) and in_flight.exists():
            os.rename(in_flight, target)   # modifies the dated directory
            landed.append(path)

    monkeypatch.setattr(cc, "_CODEX_ABSENCE_PROBE_HOOK", unarchive_lands,
                        raising=False)
    cache_result = cc.run_codex_prune_drain(wait_for_requests=False)
    if target.exists():
        os.rename(target, in_flight)       # in flight again for the twin
        # A directory's mtime may be coarser than one drain's prologue; let
        # the landing rename fall in a later tick than this departure.
        time.sleep(0.05)
    conv_result = cc.run_codex_conversation_prune_drain(wait_for_requests=False)
    if in_flight.exists():
        os.rename(in_flight, target)
    # BASE: no witness, so both drains deleted the "absent" identity.
    assert len(landed) == 2
    assert (cache_result.identities_deleted, conv_result.identities_deleted) == (0, 0)
    assert _debt(ns) == ({str(target)}, {str(target)})
    assert _identity_state(ns, target) == before
    monkeypatch.setattr(cc, "_CODEX_ABSENCE_PROBE_HOOK", None, raising=False)
    cache_result, conv_result = _drain_both()
    assert (cache_result.identities_dropped, conv_result.identities_dropped) == (1, 1)
    assert _identity_state(ns, target) == before


def _archive_first(ns, provider, i: int, *, compressed: bool):
    """An archive-only rollout whose dated directory (07/19) never existed."""
    from _codex_zstd_fixtures import compress_in_place
    archived = provider / "archived_sessions" / _archive_name(i, "19")
    write_rollout(archived, _archive_records(i))
    if compressed:
        archived = compress_in_place(archived)
    logical = provider / "sessions" / "2026" / "07" / "19" / _archive_name(i, "19")
    assert not logical.parent.exists()
    return archived, logical


@pytest.mark.parametrize("compressed", [False, True])
def test_archive_first_deletion_without_dated_dir_prunes(
        archive_estate, compressed):
    """A rollout deleted from the archive must still be pruned (AC3): with
    the logical parent as the only witness, an archive-first rollout whose
    dated directory never existed was refused forever and stayed counted.
    The accepted location's directory witnesses its deletion; the keeper
    survives."""
    ns, provider, _day, _archive, _target, keeper = archive_estate
    archived, logical = _archive_first(ns, provider, 5, compressed=compressed)
    _sync_both(ns)
    _sync_both(ns)   # a compressed conversation publishes behind its seal
    state = _identity_state(ns, logical)
    assert state["codex_session_entries"] and state["quota_window_snapshots"]
    keeper_before = _identity_state(ns, keeper)
    archived.unlink()
    _sync_both(ns)
    # BASE: refused for the missing dated parent, so no debt was planned.
    assert _debt(ns) == ({str(logical)}, {str(logical)})
    _drain_both()
    after = _identity_state(ns, logical)
    assert all(rows == [] for rows in after.values()), after
    assert _identity_state(ns, keeper) == keeper_before


def test_verified_archive_adoption_then_deletion_prunes(archive_estate):
    """The verifier must record a PLAIN archive location when it adopts a
    zstd→plain transition (G7). If it wrote NULL, a deletion straight after
    the adoption (no ordinary walk rewrote the cursor) would be witnessed by
    the missing dated parent and refused forever, leaving the rollout
    counted after the user deleted it."""
    ns, provider, _day, _archive, _target, keeper = archive_estate
    archived, logical = _archive_first(ns, provider, 6, compressed=True)
    _sync_both(ns)
    plain = archived.with_name(archived.name[:-len(".zst")])
    import _lib_codex_rollout_container as rc
    with rc.open_decoded(rc.select_container(logical, archive_dir=archived.parent)) as fh:
        plain.write_bytes(fh.read())
    archived.unlink()
    _sync_both(ns)                                  # records the transition
    _cache_mod().run_codex_container_verification_drain(wait_for_requests=False)
    conn = _cache(ns)
    try:
        assert conn.execute(
            "SELECT container_path, container_kind, container_state FROM "
            "codex_session_files WHERE path=?", (str(logical),)).fetchone() == (
            str(plain), "plain", "accepted")
    finally:
        conn.close()
    keeper_before = _identity_state(ns, keeper)
    plain.unlink()
    _sync_both(ns)
    _drain_both()
    after = _identity_state(ns, logical)
    assert after["codex_session_entries"] == [] and after["codex_session_files"] == []
    assert after["codex_conversation_source_files"] == []
    assert _identity_state(ns, keeper) == keeper_before


def test_uncontained_stored_location_is_ignored(archive_estate, tmp_path):
    """The deletion witness follows a stored location only when it lies in
    the root's archive directory; a stored path elsewhere (a tampered or
    foreign row) must not let a readable unrelated directory authorize the
    deletion of an identity whose own directories say nothing."""
    ns, provider, _day, archive, target, _keeper = archive_estate
    cc = _cache_mod()
    root = cc._codex_provider_roots()[0]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    conn = _cache(ns)
    try:
        def witness(stored):
            conn.execute("UPDATE codex_session_files SET container_path=? "
                         "WHERE path=?", (stored, str(target)))
            return cc._codex_deletion_witness(
                conn, "codex_session_files", str(target), root)
        assert witness(None) == target.parent
        assert witness(str(archive / target.name)) == archive.resolve()
        assert witness(str(elsewhere / target.name)) == target.parent
        link = archive / ("link-" + target.name)
        link.symlink_to(elsewhere / target.name)
        assert witness(str(link)) == target.parent   # canonicalizes outside
        conn.rollback()
    finally:
        conn.close()


def _quota_rows(path, root, limit_key, count, start=0):
    return [
        ("codex", root, path, start + i,
         f"2026-07-20T{(start + i) // 3600 % 24:02d}:"
         f"{(start + i) // 60 % 60:02d}:{(start + i) % 60:02d}Z",
         "primary", limit_key, "codex", None, 300, 10.0,
         "2026-07-21T00:00:00Z")
        for i in range(count)
    ]


def _root_qualified_delete_steps(tmp_path, others):
    ns = load_script()
    conn = sqlite3.connect(tmp_path / f"quota-{others}.db")
    try:
        ns["_cctally_db"]._apply_cache_schema(conn)
        sql = (
            "INSERT INTO quota_window_snapshots (source, source_root_key, "
            " source_path, line_offset, captured_at_utc, observed_slot, "
            " logical_limit_key, limit_id, limit_name, window_minutes, "
            " used_percent, resets_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
        root = "a" * 32
        conn.executemany(sql, _quota_rows("/r/target.jsonl", root,
                                          "limit-target", 10))
        # The other rows share the root but not the partition, so only the
        # DELETE's own access path can grow with them.
        for n in range(0, others, 100):
            conn.executemany(sql, _quota_rows(
                f"/r/other-{n}.jsonl", root, "limit-other", 100, start=n))
        conn.commit()
        counter = [0]

        def tick():
            counter[0] += 1
            return 0

        import _cctally_cache

        conn.set_progress_handler(tick, 1)
        _cctally_cache._delete_codex_file_derived_rows(
            conn, "/r/target.jsonl", source_root_key=root,
            match_source_root=True)
        conn.set_progress_handler(None, 0)
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source_path='/r/target.jsonl'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == others
        return counter[0]
    finally:
        conn.close()


def test_root_qualified_quota_delete_seeks_the_file(tmp_path):
    """#971 T4 (failure: the prune's root-qualified quota DELETE walks every
    quota row of the root for each deleted file, about 45 ms per file on a
    real store). Deleting one file's quota rows takes VM steps independent of
    the other quota rows the root holds."""
    small = _root_qualified_delete_steps(tmp_path, 100)
    big = _root_qualified_delete_steps(tmp_path, 10_000)
    assert big <= small + 64, (small, big)


# ── #1033 spec §5.3/§5.4: the reset-anchor evidence executor unit ───────────
#
# Failures detected: a summary published over a concurrent write (it would
# seed wrong anchors until the next bootstrap), a bootstrap that never
# finishes or never yields, a repair that leaves a stale bucket, and a clear
# that leaves the summary unusable or bumps the physical sequence.

_EV_STATE = "codex_reset_anchor_evidence"
_EV_GEN = "codex_reset_anchor_evidence_generation"


def _ev_meta(conn, key):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?",
                       (key,)).fetchone()
    return None if row is None else row[0]


def _ev_oracle(conn):
    out = {}
    for row in conn.execute(
            "SELECT source_root_key, observed_slot, logical_limit_key, "
            "resets_at_utc, source_path, line_offset, id "
            "FROM quota_window_snapshots "
            "WHERE source='codex' AND observed_slot IS NOT NULL"):
        count, minimum = out.get(row[:4], (0, tuple(row[4:])))
        out[row[:4]] = (count + 1, min(minimum, tuple(row[4:])))
    return out


def _ev_summary(conn):
    return {row[:4]: (row[4], tuple(row[5:8]), row[8]) for row in conn.execute(
        "SELECT source_root_key, observed_slot, logical_limit_key, "
        "resets_at_utc, row_count, min_source_path, min_line_offset, min_id, "
        "stale FROM codex_reset_anchor_evidence")}


def _assert_ev_exact(ns):
    conn = _cache(ns)
    try:
        assert _ev_meta(conn, _EV_STATE) == "ready"
        oracle = _ev_oracle(conn)
        assert oracle, "non-vacuity: the store holds in-scope quota rows"
        assert _ev_summary(conn) == {
            bucket: (count, minimum, 0)
            for bucket, (count, minimum) in oracle.items()}
    finally:
        conn.close()


def _ev_set_state(ns, value):
    conn = _cache(ns)
    try:
        conn.execute("UPDATE cache_meta SET value=? WHERE key=?",
                     (value, _EV_STATE))
        conn.commit()
    finally:
        conn.close()


def _ev_stale(ns):
    return _count(ns, "SELECT COUNT(*) FROM codex_reset_anchor_evidence "
                      "WHERE stale=1")


def test_1033_the_drain_bootstraps_first_and_repairs_once_after_pruning(
        debt_estate, monkeypatch):
    drain = _executor()
    ns, paths, _spawned, _clock = debt_estate
    cc = _cache_mod()
    _ev_set_state(ns, "bootstrap")
    conn = _cache(ns)
    try:
        cc.record_codex_owed_maintenance(conn, catch_up_paths=(str(paths[-1]),))
        conn.commit()
        assert cc._codex_owed_anything(conn)
    finally:
        conn.close()
    events: list[str] = []
    real_unit = cc._run_codex_reset_anchor_evidence_unit
    real_catch_up = cc._run_codex_ingest_catch_up_unit

    def unit(conn, adm_mod, kind, **kwargs):
        result = real_unit(conn, adm_mod, kind, **kwargs)
        events.append(f"{kind}:{result}")
        return result

    def catch_up(conn, adm_mod):
        events.append("catch_up")
        return real_catch_up(conn, adm_mod)

    monkeypatch.setattr(cc, "_run_codex_reset_anchor_evidence_unit", unit)
    monkeypatch.setattr(cc, "_run_codex_ingest_catch_up_unit", catch_up)
    monkeypatch.setattr(cc, "_CODEX_PRUNE_PRECOMMIT_HOOK",
                        lambda path, root: events.append("prune"))
    result = drain(wait_for_requests=False)
    assert result.identities_deleted == ORPHANS and result.remaining == 0
    assert events == (["reset_anchor_evidence_bootstrap:published",
                       "catch_up"] + ["prune"] * ORPHANS
                      + ["reset_anchor_evidence_repair:published"]), events
    assert _ev_stale(ns) == 0
    _assert_ev_exact(ns)
    conn = _cache(ns)
    try:
        assert cc._codex_reset_anchor_evidence_work(conn) is None
        assert not cc._codex_owed_anything(conn)
    finally:
        conn.close()


def _ev_foreign_id_update(ns):
    """An ``id``-only UPDATE by another writer: the change ledger cannot see
    it; only the evidence generation moves."""
    raw = sqlite3.connect(ns["_cctally_core"].CACHE_DB_PATH)
    try:
        raw.execute(
            "UPDATE quota_window_snapshots SET id = id + 100000 WHERE id = "
            "(SELECT MIN(id) FROM quota_window_snapshots WHERE source='codex')")
        raw.commit()
    finally:
        raw.close()


@pytest.mark.parametrize("mutations,expected", [
    (1, "published"), (3, "blocked")])
def test_1033_a_token_moved_before_publish_reprepares_then_blocks(
        estate, monkeypatch, mutations, expected):
    ns, _paths, _spawned, _clock = estate
    cc = _cache_mod()
    _ev_set_state(ns, "bootstrap")
    attempts: list[str] = []

    def prepublish(kind, attempt):
        attempts.append(kind)
        if len(attempts) <= mutations:
            _ev_foreign_id_update(ns)

    monkeypatch.setattr(cc, "_CODEX_EVIDENCE_PREPUBLISH_HOOK", prepublish)
    conn = _cache(ns)
    try:
        outcome = cc._run_codex_reset_anchor_evidence_unit(
            conn, adm, "reset_anchor_evidence_bootstrap")
        assert not conn.in_transaction
    finally:
        conn.close()
    assert outcome == expected
    assert len(attempts) == min(mutations + 1, 3)
    if expected == "published":
        _assert_ev_exact(ns)
    else:
        conn = _cache(ns)
        try:
            assert _ev_meta(conn, _EV_STATE) == "bootstrap"
            assert cc._codex_reset_anchor_evidence_work(conn) == "bootstrap"
            assert cc._codex_owed_anything(conn), "the work must stay owed"
        finally:
            conn.close()


def test_1033_the_drain_blocks_a_contended_unit_for_the_run(
        estate, monkeypatch):
    drain = _executor()
    ns, _paths, _spawned, _clock = estate
    cc = _cache_mod()
    _ev_set_state(ns, "bootstrap")
    calls: list[int] = []
    monkeypatch.setattr(cc, "_CODEX_EVIDENCE_PREPUBLISH_HOOK",
                        lambda kind, attempt: (calls.append(attempt),
                                               _ev_foreign_id_update(ns)))
    result = drain(wait_for_requests=False)
    assert result.outcome == "drained"
    assert calls == [1, 2, 3], "at most three attempts per unit per run"
    conn = _cache(ns)
    try:
        assert _ev_meta(conn, _EV_STATE) == "bootstrap"
    finally:
        conn.close()


def test_1033_admission_refusal_yields_and_keeps_the_work(estate):
    ns, _paths, _spawned, _clock = estate
    cc = _cache_mod()
    _ev_set_state(ns, "bootstrap")
    app_dir = ns["_cctally_core"].APP_DIR
    assert adm.publish_waiting_request(app_dir, root_keys=["any"])
    budget: dict = {}
    conn = _cache(ns)
    try:
        outcome = cc._run_codex_reset_anchor_evidence_unit(
            conn, adm, "reset_anchor_evidence_bootstrap", budget=budget)
        # One refusal with budget left yields (the caller backs off) and
        # spends one of the run's attempts.
        assert outcome == "yielded"
        assert budget == {"reset_anchor_evidence_bootstrap": 2}
        assert _ev_meta(conn, _EV_STATE) == "bootstrap"
    finally:
        conn.close()
        for request in (app_dir / adm.REQUEST_DIR_NAME).glob("req-*.json"):
            request.unlink()


@pytest.mark.parametrize("runner,setup,first", [
    ("drain", None, "refuse"),
    ("inline", None, "refuse"),
    ("drain", "DROP TRIGGER trg_qws_anchor_evidence_del", "refuse"),
    ("drain", None, "mismatch"),
])
def test_1033_refusals_spend_the_run_budget_across_yields(
        estate, monkeypatch, runner, setup, first):
    """Failure detected: a run that waits for requests re-ran the unit with
    three fresh attempts after every yield, so a refusing gate made it
    rescan history (seconds per scan) without end. Refusals, including the
    setup step's, and mismatches share one budget of three per run."""
    ns, _paths, _spawned, _clock = estate
    cc = _cache_mod()
    _ev_set_state(ns, "bootstrap")
    if setup is not None:
        conn = _cache(ns)
        try:
            conn.execute(setup)
            conn.commit()
        finally:
            conn.close()
    prepares: list[int] = []
    admits: list[int] = []
    backoffs: list[int] = []
    real_admit = adm.admit

    def prepublish(kind, attempt):
        prepares.append(attempt)
        if first == "mismatch" and len(prepares) == 1:
            _ev_foreign_id_update(ns)

    def admit(*args, **kwargs):
        admits.append(1)
        if len(admits) > 10:
            raise AssertionError("the run kept retrying past its budget")
        if first == "mismatch" and len(admits) == 1:
            return real_admit(*args, **kwargs)
        return adm.Admission(False, None, None)

    monkeypatch.setattr(cc, "_CODEX_EVIDENCE_PREPUBLISH_HOOK", prepublish)
    monkeypatch.setattr(adm, "admit", admit)
    monkeypatch.setattr(cc, "_drain_backoff",
                        lambda adm_mod: backoffs.append(1))
    if runner == "drain":
        _executor()(wait_for_requests=True)
    else:
        assert cc.run_codex_reset_anchor_evidence(wait_for_requests=True) == 0
    assert len(admits) == 3, admits
    assert len(prepares) == (0 if setup is not None else 3), prepares
    assert len(backoffs) == (1 if first == "mismatch" else 2), backoffs
    conn = _cache(ns)
    try:
        assert _ev_meta(conn, _EV_STATE) == "bootstrap"
        assert cc._codex_reset_anchor_evidence_work(conn) == "bootstrap"
        assert cc._codex_owed_anything(conn), "the work must stay owed"
    finally:
        conn.close()


@pytest.mark.parametrize("missing", [
    "DROP INDEX idx_codex_reset_anchor_evidence_stale",
    "DROP TRIGGER trg_qws_anchor_evidence_del",
    "DROP TABLE codex_reset_anchor_evidence"])
def test_1033_the_setup_step_restores_missing_objects(estate, missing):
    drain = _executor()
    ns, _paths, _spawned, _clock = estate
    cc = _cache_mod()
    conn = _cache(ns)
    try:
        conn.execute(missing)
        conn.commit()
        assert cc._codex_reset_anchor_evidence_work(conn) == "bootstrap"
        assert cc._codex_owed_anything(conn)
    finally:
        conn.close()
    drain(wait_for_requests=False)
    conn = _cache(ns)
    try:
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN (?,?,?,?,?)",
            ("codex_reset_anchor_evidence",
             "idx_codex_reset_anchor_evidence_stale",
             "trg_qws_anchor_evidence_ins", "trg_qws_anchor_evidence_del",
             "trg_qws_anchor_evidence_upd"))}
        assert len(present) == 5
        assert cc._codex_reset_anchor_evidence_work(conn) is None
    finally:
        conn.close()
    _assert_ev_exact(ns)


def test_1033_cache_sync_runs_the_unit_inline(estate, monkeypatch):
    import argparse
    ns, _paths, _spawned, _clock = estate
    cc = _cache_mod()
    _ev_set_state(ns, "bootstrap")
    kinds: list[str] = []
    real_unit = cc._run_codex_reset_anchor_evidence_unit

    def unit(conn, adm_mod, kind, **kwargs):
        kinds.append(kind)
        return real_unit(conn, adm_mod, kind, **kwargs)

    monkeypatch.setattr(cc, "_run_codex_reset_anchor_evidence_unit", unit)
    # The drain would run the unit too; the inline call must not need it.
    monkeypatch.setattr(cc, "run_codex_prune_drain", lambda **kwargs: (
        cc.PruneDrainResult("drained", 0, 0, 0, 0)))
    rc = cc.cmd_cache_sync(argparse.Namespace(
        source="codex", rebuild=False, prune_orphans=False))
    assert rc == 0
    assert kinds == ["reset_anchor_evidence_bootstrap"]
    _assert_ev_exact(ns)


def test_1033_bootstrap_and_repair_aggregate_the_lexicographic_minimum(
        estate):
    """The independent-minimum counterexample: after the stored minimum is
    deleted, the survivors' minimum path and minimum offset come from
    different rows. A per-column MIN() would publish (path of one, offset of
    the other); the repair and the bootstrap must publish one real row."""
    drain = _executor()
    ns, _paths, _spawned, _clock = estate
    cc = _cache_mod()
    conn = _cache(ns)
    try:
        columns = ("source", "source_root_key", "source_path", "line_offset",
                   "captured_at_utc", "observed_slot", "logical_limit_key",
                   "window_minutes", "used_percent", "resets_at_utc")
        for path, offset in (("/x/0.jsonl", 1), ("/x/a.jsonl", 90),
                             ("/x/b.jsonl", 2)):
            conn.execute(
                f"INSERT INTO quota_window_snapshots ({', '.join(columns)}) "
                "VALUES ('codex', 'root-x', ?, ?, '2026-07-20T10:00:00Z', "
                "'primary', 'limit-x', 10080, 10.0, '2026-07-27T00:00:00Z')",
                (path, offset))
        conn.execute("DELETE FROM quota_window_snapshots "
                     "WHERE source_path='/x/0.jsonl'")
        conn.commit()
        assert conn.execute(
            "SELECT stale FROM codex_reset_anchor_evidence "
            "WHERE source_root_key='root-x'").fetchone() == (1,)
    finally:
        conn.close()
    drain(wait_for_requests=False)
    _assert_ev_exact(ns)
    conn = _cache(ns)
    try:
        row = conn.execute(
            "SELECT min_source_path, min_line_offset FROM "
            "codex_reset_anchor_evidence WHERE source_root_key='root-x'"
        ).fetchone()
        assert row == ("/x/a.jsonl", 90)
    finally:
        conn.close()
    _ev_set_state(ns, "bootstrap")
    drain(wait_for_requests=False)
    _assert_ev_exact(ns)


def test_1033_the_evidence_units_never_run_the_quota_projection(
        debt_estate, monkeypatch):
    drain = _executor()
    ns, _paths, _spawned, _clock = debt_estate
    quota = sys.modules["_cctally_quota"]
    _ev_set_state(ns, "bootstrap")
    calls: list[int] = []
    monkeypatch.setattr(quota, "reconcile_codex_quota_projection",
                        lambda *a, **k: calls.append(1))
    result = drain(wait_for_requests=False)
    assert result.identities_deleted == ORPHANS
    assert calls == []
    _assert_ev_exact(ns)


@pytest.mark.parametrize("state", ["ready", "bootstrap"])
def test_1033_a_whole_provider_clear_leaves_an_empty_ready_summary(
        estate, state):
    ns, _paths, _spawned, _clock = estate
    cc = _cache_mod()
    _ev_set_state(ns, state)
    conn = _cache(ns)
    try:
        generation = int(_ev_meta(conn, _EV_GEN))
        seq = _seq(ns)
        assert cc._clear_codex_derived_rows(conn) is True
        conn.commit()
        assert _seq(ns) == seq, "summary work must not bump the physical seq"
        assert _ev_meta(conn, _EV_STATE) == "ready"
        assert int(_ev_meta(conn, _EV_GEN)) > generation
        assert _ev_summary(conn) == {}
        assert cc._clear_codex_derived_rows(conn) is False
        conn.commit()
        assert _ev_meta(conn, _EV_STATE) == "ready"
    finally:
        conn.close()


# ── #1034: the drain settles its own deletions (spec §5.2, §5.5) ─────────────

def _enable_codex_hooks(home: pathlib.Path, *, enabled: bool = True) -> None:
    """Install the managed handlers and the trust entries Codex records, so
    ``codex_hook_roots_all_enabled`` observes ``installed_enabled`` (or, with
    ``enabled=False``, ``installed_disabled``) through the real config."""
    home = home.resolve()
    handler = {"type": "command", "timeout": 30,
               "command": "/opt/cctally/bin/cctally hook-tick --foreground "
                          "--source codex"}
    hooks_path = home / "hooks.json"
    hooks_path.write_text(json.dumps({"hooks": {
        event: [{"hooks": [dict(handler)]}]
        for event in ("Stop", "SubagentStop")}}))
    flag = "" if enabled else "enabled = false\n"
    (home / "config.toml").write_text(
        f'[hooks.state."{hooks_path}:stop:0:0"]\ntrusted_hash = "aaa"\n{flag}'
        f'[hooks.state."{hooks_path}:subagent_stop:0:0"]\n'
        f'trusted_hash = "bbb"\n{flag}', encoding="utf-8")
    stamp = os.stat(home / "config.toml").st_mtime
    os.utime(hooks_path, (stamp - 60, stamp - 60))


def _record():
    return sys.modules["_cctally_record"]


@pytest.fixture
def settle_estate(debt_estate):
    """``debt_estate`` ready to settle: the root's managed hooks enabled
    through the real config, and the projection materialized by a primed
    eligible pass, so the drain's deletions change a published projection."""
    from test_codex_hook_lifecycle import _eligible_reconcile
    ns, paths, spawned, clock = debt_estate
    home = pathlib.Path(os.environ["CODEX_HOME"])
    # A second survivor, so a continuation can also delete the keeper: #485
    # never deletes a root's last recognizable rollout.
    write_rollout(paths[0].parent / "rollout-2026-07-20T15-00-00-0199aaaa-"
                  "bbbb-7ccc-8ddd-eeeeffff0099.jsonl",
                  token_records(2, session_id="survivor"))
    conn = _cache(ns)
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    _enable_codex_hooks(home)
    import _lib_codex_hooks as hooks
    assert hooks.codex_hook_roots_all_enabled(_record()._codex_lifecycle_roots())
    assert _eligible_reconcile(ns).status in ("published", "current")
    return ns, paths, spawned, clock, home


def _drain_line(ns) -> dict:
    lines = [line for line in (ns["APP_DIR"] / "logs" / "hook-tick.log")
             .read_text().splitlines() if "op=prune-drain" in line]
    assert lines, "the worker wrote no op=prune-drain line"
    return dict(tok.split("=", 1) for tok in lines[-1].split()[1:] if "=" in tok)


def _hook_evidence(ns) -> tuple:
    """Lifecycle success markers and lifecycle lines: hook-only evidence."""
    markers = sorted(p.name for p in (ns["APP_DIR"] / "codex-hook-tick")
                     .glob("*.last-success"))
    log = ns["APP_DIR"] / "logs" / "hook-tick.log"
    lines = [line for line in (log.read_text().splitlines()
                               if log.exists() else [])
             if "source_root_key=" in line]
    return markers, lines


def test_the_worker_settles_its_deletions(settle_estate):
    """Detects the defect #1034 removes: the drain commits prune deletions
    that change a published projection and leaves the change-ledger range to
    the next Codex hook, which then spends seconds re-projecting it on the
    blocking path. The worker must settle the range itself, under every
    root's lifecycle lock, so the next hook's scope is empty."""
    ns, _paths, _spawned, _clock, _home = settle_estate
    import argparse
    import _cctally_quota as quota
    from helpers.quota_projection_dump import scope_recorder
    from test_codex_hook_lifecycle import (
        _next_eligible_scope_units, _scoped_units)
    cache = _cache_mod()
    with scope_recorder(quota) as scopes:
        assert cache._cmd_codex_prune_drain_body(argparse.Namespace()) == 0
    fields = _drain_line(ns)
    assert (fields["result"], fields["deleted"]) == ("drained", str(ORPHANS))
    assert fields.get("settle") == "published", fields
    assert fields["settle_projection"] == "published", fields
    assert fields["settle_eligible_roots"] == "1", fields
    assert fields["settle_budget"] == "1", fields
    assert _scoped_units(scopes) > 0, "the drain left no ledger range"
    assert _next_eligible_scope_units(ns) == 0


@pytest.mark.parametrize("caller", ["executor", "cache_sync"])
def test_the_executor_and_cache_sync_never_settle(
    settle_estate, monkeypatch, caller,
):
    """Detects a settlement reached from a caller that is not the drain
    worker (#1034 §5.2): ``cache-sync`` and every executor test call the
    drain directly, and a settlement there would evaluate alerts at a
    command's wall clock. Their deletions stay owed to the next hook."""
    ns, _paths, _spawned, _clock, _home = settle_estate
    from test_codex_hook_lifecycle import _next_eligible_scope_units
    cache = _cache_mod()
    if caller == "executor":
        result = _executor()(wait_for_requests=False)
        assert result.identities_deleted == ORPHANS
        assert result.settlement == "none", result
        assert _next_eligible_scope_units(ns) > 0
        return
    import argparse
    seen: list = []
    real_drain = cache.run_codex_prune_drain

    def drain(**kwargs):
        result = real_drain(**kwargs)
        seen.append((kwargs, result))
        return result

    monkeypatch.setattr(cache, "run_codex_prune_drain", drain)
    assert cache.cmd_cache_sync(argparse.Namespace(
        source="codex", rebuild=False, prune_orphans=False)) == 0
    assert seen and all(
        not kwargs.get("settle_projection") and result.settlement == "none"
        for kwargs, result in seen), seen
    assert seen[0][1].identities_deleted == ORPHANS, seen


def _hold_lifecycle_locks(ns, roots):
    import fcntl
    base = ns["APP_DIR"] / "codex-hook-tick"
    base.mkdir(parents=True, exist_ok=True)
    fds = []
    for root in roots:
        fd = os.open(base / f"{root.source_root_key}.lock",
                     os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        fds.append(fd)
    return fds


def _flip(case, ns, monkeypatch, home, tmp_path, notes):
    """Flip one settlement premise at settlement time. Where the real config
    or store can express the premise it is used (hooks disabled, mixed roots,
    held lifecycle locks, prune debt for the pin); a predicate is patched only
    where a real state would also change what the drain loop itself does
    (no roots, an epoch rebuild, a replay refill, inventory, the quota index,
    root drift and an unreadable cache)."""
    cache, record = _cache_mod(), _record()
    if case == "hooks_disabled":
        _enable_codex_hooks(home, enabled=False)
    elif case == "mixed_roots":
        other = tmp_path / "codex-other"
        (other / "sessions").mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", f"{home},{other}")
    elif case == "no_roots":
        monkeypatch.setattr(record, "_codex_lifecycle_roots", lambda: [])
    elif case == "locks_busy":
        notes["fds"] = _hold_lifecycle_locks(
            ns, record._codex_lifecycle_roots())
        notes["sleeps"] = []
        monkeypatch.setattr(time, "sleep", notes["sleeps"].append)
    elif case == "partial_locks":
        # Two enabled roots, another holder keeping ONE root's lock through
        # every attempt: each attempt acquires the free root only, so the
        # partial set must be released before each retry sleep.
        import fcntl
        other = tmp_path / "codex-other"
        (other / "sessions").mkdir(parents=True)
        _enable_codex_hooks(other)
        monkeypatch.setenv("CODEX_HOME", f"{home},{other}")
        roots = record._codex_lifecycle_roots()
        assert len(roots) == 2, roots
        busy = [root for root in roots
                if root.codex_home.resolve() == other.resolve()]
        assert len(busy) == 1, roots
        free = [root for root in roots if root not in busy][0]
        notes["fds"] = _hold_lifecycle_locks(ns, busy)
        free_lock = (ns["APP_DIR"] / "codex-hook-tick"
                     / f"{free.source_root_key}.lock")
        notes["free_acquirable"] = []

        def probe_free_root(_seconds):
            fd = os.open(free_lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    notes["free_acquirable"].append(False)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    notes["free_acquirable"].append(True)
            finally:
                os.close(fd)
        monkeypatch.setattr(time, "sleep", probe_free_root)
    elif case == "root_drift":
        import _lib_codex_hooks as hooks
        real = record._codex_lifecycle_roots
        calls = {"n": 0}

        def drifting():
            calls["n"] += 1
            roots = real()
            if calls["n"] == 1:
                return roots
            # Under the complete set: the report's occupancy must cover it.
            time.sleep(_DRIFT_HOLD_SECONDS)
            return roots + [hooks.CodexHookRoot(
                "zz-drifted", home.resolve(), home.resolve() / "hooks.json")]
        monkeypatch.setattr(record, "_codex_lifecycle_roots", drifting)
    elif case == "epoch_pending":
        notes["deferred"] = []
        monkeypatch.setattr(record, "_stats_epoch_rebuild_pending", lambda: True)
        monkeypatch.setattr(record, "_defer_stats_epoch_rebuild",
                            lambda: notes["deferred"].append(1) or "spawned")
    elif case == "replay_refill":
        monkeypatch.setattr(cache, "_codex_replay_refill_unlocked",
                            lambda conn: True)
    elif case == "inventory_pending":
        monkeypatch.setattr(cache, "codex_identity_inventory_ready",
                            lambda conn: False)
    elif case == "quota_index_pending":
        real_sync = ns["sync_codex_cache"]

        def deferring_sync(conn, **kwargs):
            stats = real_sync(conn, **kwargs)
            stats.deferred_reason = "quota_index_pending"
            return stats
        monkeypatch.setitem(ns, "sync_codex_cache", deferring_sync)
    elif case == "unusable_pin":
        conn = _cache(ns)
        try:
            conn.execute(
                "INSERT INTO codex_prune_debt (source_path, source_root_key, "
                "recorded_at_utc) VALUES (?, '', '2026-07-20T00:00:00+00:00')",
                (str(home / "sessions" / "gone.jsonl"),))
            conn.commit()
        finally:
            conn.close()
    elif case == "cache_unavailable":
        def unreadable(db_path):
            raise sqlite3.OperationalError("unable to open database file")
        monkeypatch.setattr(cache, "_hook_snapshot_connect", unreadable)
    else:
        assert case == "ready", case


_DRIFT_HOLD_SECONDS = 0.05

_DECLINES = {
    "hooks_disabled": ("hooks_disabled", ""),
    "mixed_roots": ("hooks_disabled", ""),
    "no_roots": ("no_roots", ""),
    "locks_busy": ("locks_busy", ""),
    "partial_locks": ("locks_busy", ""),
    "root_drift": ("not_ready", "root_drift"),
    "epoch_pending": ("not_ready", "epoch_pending"),
    "replay_refill": ("not_ready", "replay_pending"),
    "inventory_pending": ("not_ready", "inventory_pending"),
    "quota_index_pending": ("not_ready", "quota_index_pending"),
    "unusable_pin": ("not_ready", "pin_prune_debt"),
    "cache_unavailable": ("unavailable", "cache_unavailable"),
}

#: The core ran these declines while the settlement held every root's lock.
_DECLINES_UNDER_THE_LOCKS = {"quota_index_pending", "unusable_pin"}


def _settle_with(settle_estate, monkeypatch, tmp_path, case):
    """Run the settling drain with ``case``'s premise flipped once the
    executor loop has ended, so the flip cannot change what the loop does."""
    ns, _paths, _spawned, _clock, home = settle_estate
    cache = _cache_mod()
    real_settle = cache._settle_codex_quota_after_prune
    notes: dict = {}

    def settle():
        with monkeypatch.context() as patch:
            _flip(case, ns, patch, home, tmp_path, notes)
            try:
                return real_settle()
            finally:
                for fd in notes.get("fds", ()):
                    os.close(fd)
    monkeypatch.setattr(cache, "_settle_codex_quota_after_prune", settle)
    result = _executor()(wait_for_requests=False, settle_projection=True)
    return result, notes


@pytest.mark.parametrize("case", sorted(_DECLINES))
def test_a_settlement_declines_with_its_specific_token(
    settle_estate, monkeypatch, tmp_path, case,
):
    """Detects a settlement that publishes where it must not (#1034 A8): on
    an install whose hooks Codex never invokes (alerts at drain time), with a
    lifecycle lock it does not hold, or on a store the hook would refuse
    (partial population). Each premise is flipped alone on the ready
    fixture; the paired ready control below does settle."""
    ns = settle_estate[0]
    from test_codex_hook_lifecycle import _next_eligible_scope_units
    result, notes = _settle_with(settle_estate, monkeypatch, tmp_path, case)
    assert result.identities_deleted == ORPHANS, result
    assert (result.settlement, result.settle_reason) == _DECLINES[case], result
    assert result.settle_projection == "" and result.settle_counts is None
    if case == "locks_busy":
        assert len(notes["sleeps"]) == 20, notes["sleeps"]
    if case == "partial_locks":
        # Between attempts an outside caller can take the free root's lock:
        # the partial set was released before every sleep, never held idle.
        assert notes["free_acquirable"] == [True] * 20, notes
    # §5.5: occupancy runs from the complete acquisition to the release, so
    # only the declines taken under the complete set report any: the root
    # drift recheck and the core's own declines.
    if case == "root_drift":
        assert result.settle_held_ms >= int(_DRIFT_HOLD_SECONDS * 1000), result
    elif case in _DECLINES_UNDER_THE_LOCKS:
        assert result.settle_held_ms > 0, result
    else:
        assert result.settle_held_ms == 0, result
    if case == "epoch_pending":
        assert notes["deferred"] == [1]
    assert _hook_evidence(ns) == ([], [])
    assert _next_eligible_scope_units(ns) > 0, (
        "a declined settlement consumed the drain's ledger range")


def test_the_ready_control_settles(settle_estate, monkeypatch, tmp_path):
    """The decline matrix's paired control (#1034 A8): the same fixture with
    no premise flipped settles, so each decline above is its own gate's."""
    ns = settle_estate[0]
    from test_codex_hook_lifecycle import _next_eligible_scope_units
    result, _notes = _settle_with(settle_estate, monkeypatch, tmp_path, "ready")
    assert (result.settlement, result.settle_projection) == (
        "published", "published"), result
    assert result.settle_counts.eligible_roots == 1, result
    assert _next_eligible_scope_units(ns) == 0


class _OffsetClock:
    """``_cctally_cache``'s ``time`` with a monotonic clock the test can step
    forward; every other attribute is the real module."""

    def __init__(self, on_sleep):
        self.offset = 0.0
        self._on_sleep = on_sleep

    def monotonic(self):
        return time.monotonic() + self.offset

    def sleep(self, seconds):
        self._on_sleep(self)

    def __getattr__(self, name):
        return getattr(time, name)


def test_settle_held_ms_is_the_hold_alone(settle_estate, monkeypatch):
    """Detects a lifecycle-lock occupancy that is not the hold (#1034 §5.5):
    ``settle_held_ms`` is how long hooks were locked out, from the complete
    acquisition to the release. The first attempt finds a root's lock held,
    the retry sleep steps the clock 100 s, and the core steps it 10 s while it
    holds the set: the report must carry the core's 10 s and none of the
    retry wait."""
    ns = settle_estate[0]
    cache, record = _cache_mod(), _record()
    roots = record._codex_lifecycle_roots()
    fds = _hold_lifecycle_locks(ns, roots)
    sleeps: list = []

    def retry_sleep(clock):
        sleeps.append(1)
        while fds:
            os.close(fds.pop())
        clock.offset += 100.0

    clock = _OffsetClock(retry_sleep)
    real_core = record._run_codex_lifecycle_core

    def core(locks, roots, **kwargs):
        outcome = real_core(locks, roots, **kwargs)
        clock.offset += 10.0
        return outcome
    real_settle = cache._settle_codex_quota_after_prune

    def settle():
        with monkeypatch.context() as patch:
            patch.setattr(cache, "time", clock)
            patch.setattr(record, "_run_codex_lifecycle_core", core)
            return real_settle()
    monkeypatch.setattr(cache, "_settle_codex_quota_after_prune", settle)
    try:
        result = _executor()(wait_for_requests=False, settle_projection=True)
    finally:
        while fds:
            os.close(fds.pop())
    assert sleeps == [1], "the first attempt was not refused"
    assert result.settlement == "published", result
    assert 10_000 <= result.settle_held_ms < 100_000, result


def test_a_budget_failure_after_publication_reaches_the_drain_line(
    settle_estate, monkeypatch,
):
    """Detects a settlement error that hides its committed publication on the
    worker's ``op=prune-drain`` line, or a reason that leaks a path or
    impersonates a field (#1034 §5.5, §7 shared core (3)): the budget leg
    raises after the reconcile published, and the line must still carry
    ``settle=error`` with the reconcile's status and counts, the executor's
    own ``result=``, and a sanitized ``settle_reason`` last."""
    ns = settle_estate[0]
    import argparse
    cache = _cache_mod()

    def fail_budget(saved, **kwargs):
        raise OSError(2, "boom key=value", "/Users/someone/secret/path")
    monkeypatch.setitem(ns, "maybe_record_codex_budget_milestone", fail_budget)

    assert cache._cmd_codex_prune_drain_body(argparse.Namespace()) == 0

    [line] = [line for line in (ns["APP_DIR"] / "logs" / "hook-tick.log")
              .read_text().splitlines() if "op=prune-drain" in line]
    fields = _drain_line(ns)
    assert fields["result"] == "drained", fields
    assert (fields["settle"], fields["settle_projection"]) == (
        "error", "published"), fields
    for key in ("settle_blocks", "settle_milestones", "settle_eligible_roots"):
        assert fields[key].isdigit(), (key, fields)
    head, reason = line.split(" settle_reason=", 1)
    assert "/Users/" not in line and "someone" not in line, line
    assert "[Errno 2]" in reason and "boom" in reason, reason
    assert "=" not in reason, (
        "the reason carries a key that a last-wins parser would take", line)
    assert "error=" not in head, line


def test_an_unreadable_exit_recheck_keeps_the_settlement(
    settle_estate, monkeypatch,
):
    """Detects a diagnostic read that discards committed work's report
    (#1034 §5.5): the re-check after a real settlement opens a fresh cache
    connection, and an open that raises there must not turn the worker's line
    into ``result=error`` without ``settle=``, nor skip the conversations
    drain. The re-check reports ``unknown`` and runs no continuation."""
    ns = settle_estate[0]
    import argparse
    cache = _cache_mod()
    real_open = cache.open_cache_db
    real_settle = cache._settle_codex_quota_after_prune
    armed = {"on": False, "raised": 0}

    def open_cache_db():
        if armed["on"]:
            armed["on"] = False
            armed["raised"] += 1
            raise sqlite3.OperationalError("unable to open database file")
        return real_open()

    def settle():
        report = real_settle()
        armed["on"] = True
        return report
    monkeypatch.setattr(cache, "open_cache_db", open_cache_db)
    monkeypatch.setattr(cache, "_settle_codex_quota_after_prune", settle)
    results: list = []
    real_drain = cache.run_codex_prune_drain
    monkeypatch.setattr(cache, "run_codex_prune_drain",
                        lambda **kw: results.append(real_drain(**kw))
                        or results[-1])
    phases: list = []
    real_phase = cache._run_codex_prune_drain_phase
    monkeypatch.setattr(cache, "_run_codex_prune_drain_phase",
                        lambda *a, **kw: phases.append(1) or real_phase(*a, **kw))

    assert cache._cmd_codex_prune_drain_body(argparse.Namespace()) == 0

    assert armed["raised"] == 1, "the re-check never opened the cache"
    [result] = results
    assert result.settlement == "published", result
    assert result.work_remaining is None and result.ledger_retained == -1
    assert phases == [1], "a continuation ran on an unreadable re-check"
    fields = _drain_line(ns)
    assert fields["result"] == "drained", fields
    assert (fields["settle"], fields["work_remaining"],
            fields["ledger_retained"]) == ("published", "unknown",
                                           "unknown"), fields
    assert "conv_remaining" in fields, "the conversations drain was skipped"


def test_a_settlement_leaves_no_hook_evidence(settle_estate):
    """Detects a settlement that impersonates a hook (#1034 §5.1 point 3):
    a success marker would throttle the next real hook for 15 s, a lifecycle
    line would feed doctor's hook counters, and an activity ticket would tell
    the dashboard frontier a hook ran."""
    ns = settle_estate[0]
    import _lib_ingest_frontier as frontier
    ticket = frontier.activity_marker_path(ns["APP_DIR"])
    before = ticket.read_bytes() if ticket.exists() else None
    result = _executor()(wait_for_requests=False, settle_projection=True)
    assert result.settlement == "published", result
    assert _hook_evidence(ns) == ([], [])
    assert (ticket.read_bytes() if ticket.exists() else None) == before


def test_the_drain_line_reports_unknowns_for_busy(debt_estate):
    """Detects a ``busy`` drain reported as having checked its work: the
    single-flight loser inspected nothing, so ``work_remaining`` and
    ``ledger_retained`` must read ``unknown`` rather than a false 0."""
    ns = debt_estate[0]
    import argparse
    cache = _cache_mod()
    held = cache._worker_lock(cache.CODEX_PRUNE_DRAIN_LOCK_NAME)
    assert held is not None
    try:
        result = _executor()(wait_for_requests=False, settle_projection=True)
        assert result.outcome == "busy" and result.work_remaining is None
        assert result.ledger_retained == -1 and result.settlement == "none"
        assert cache._cmd_codex_prune_drain_body(argparse.Namespace()) == 0
    finally:
        cache._release_worker_lock(held)
    fields = _drain_line(ns)
    assert (fields["result"], fields["settle"], fields["work_remaining"],
            fields["ledger_retained"]) == ("busy", "none", "unknown",
                                           "unknown"), fields


def _core_outcome(**fields):
    defaults = dict(sync="ok", result="noop", pinned=True, reconciled=True,
                    budget_completed=True)
    defaults.update(fields)
    return _record().CodexLifecycleCoreOutcome(**defaults)


def _status_projection(status, *, pending=False):
    from types import SimpleNamespace
    return SimpleNamespace(status=status, alert_work_pending=pending,
                           blocks_upserted=2, milestones_upserted=1,
                           alerts_dispatched=0)


_TOKEN_CASES = {
    "budget_contended": (dict(projection=_status_projection("published"),
                              budget_completed=False), "budget_deferred"),
    "budget_deferred": (dict(projection=_status_projection("current"),
                             budget_completed=False,
                             reason="budget_ingest_incomplete"),
                        "budget_deferred"),
    "alert_pending": (dict(projection=_status_projection(
        "published", pending=True), request_verify=True), "alert_pending"),
    "deferred_verify": (dict(projection=_status_projection("deferred_verify")),
                        "deferred_verify"),
    "deferred_model": (dict(projection=_status_projection("deferred_model")),
                       "deferred_model"),
    "contended": (dict(projection=_status_projection("contended")),
                  "contended"),
    "unavailable": (dict(projection=_status_projection("unavailable")),
                    "unavailable"),
    "published": (dict(result="success",
                       projection=_status_projection("published")),
                  "published"),
    "budget_error_after_publication": (dict(
        result="error", error="RuntimeError: x",
        projection=_status_projection("published")), "error"),
}


@pytest.mark.parametrize("case", sorted(_TOKEN_CASES))
def test_post_reconcile_outcomes_map_to_their_tokens(case):
    """Detects a post-reconcile outcome reported as ``not_ready`` or without
    the reconcile's own status (#1034 V1): a committed publication followed
    by a budget deferral, pending alert work or a budget failure must stay
    visible as such on the drain line."""
    fields, token = _TOKEN_CASES[case]
    cache = _cache_mod()
    core = _core_outcome(**fields)
    assert cache._settlement_token(core) == token
    report = cache.SettlementReport(token, core, core.reason, 0.25, 1)
    result = cache._apply_settlement(
        cache.PruneDrainResult("drained", ORPHANS, 0, 0, 0), report)
    assert result.settlement == token
    assert result.settle_projection == fields["projection"].status
    assert result.settle_counts.blocks == 2 and result.settle_held_ms == 250


def test_a_pre_reconcile_decline_maps_to_not_ready():
    """The other half of the phase rule: a ``noop`` before the reconcile is
    ``not_ready`` and carries no projection status."""
    cache = _cache_mod()
    core = _core_outcome(reconciled=False, pinned=False,
                         reason="pin_prune_debt", budget_completed=False)
    assert cache._settlement_token(core) == "not_ready"
    result = cache._apply_settlement(
        cache.PruneDrainResult("drained", ORPHANS, 0, 0, 0),
        cache.SettlementReport("not_ready", core, core.reason, 0.0))
    assert (result.settle_projection, result.settle_counts) == ("", None)


# ── #1034 J3: the continuation after the settlement (spec §5.4) ──────────────

def _after_settlement_core(monkeypatch, action, *, raise_after=False):
    """Run ``action`` inside the settlement, right after the real core
    returned and while it still holds every lifecycle lock; optionally raise
    afterwards, as a core failing after its publication committed would."""
    record = _record()
    real = record._run_codex_lifecycle_core
    outcomes: list = []

    def core(locks, roots, **kwargs):
        outcome = real(locks, roots, **kwargs)
        outcomes.append(outcome)
        action()
        if raise_after:
            raise RuntimeError("core failed after its publication")
        return outcome
    monkeypatch.setattr(record, "_run_codex_lifecycle_core", core)
    return outcomes


def _count_settlements(monkeypatch):
    cache = _cache_mod()
    real = cache._settle_codex_quota_after_prune
    calls: list = []
    monkeypatch.setattr(cache, "_settle_codex_quota_after_prune",
                        lambda: calls.append(1) or real())
    return calls


def _keeper_debt(ns, paths):
    """Delete the estate's keeper and record it as prune debt, as a hook
    would after the rollout vanished."""
    keeper = paths[ORPHANS]
    keeper.unlink()
    conn = _cache(ns)
    try:
        conn.execute(
            "INSERT INTO codex_prune_debt (source_path, source_root_key, "
            "recorded_at_utc) SELECT path, COALESCE(source_root_key, ''), "
            "'2026-07-20T00:00:00+00:00' FROM codex_session_files "
            "WHERE path=?", (str(keeper),))
        conn.commit()
    finally:
        conn.close()
    return str(keeper)


def _stale_debt(ns, home, name="gone.jsonl"):
    conn = _cache(ns)
    try:
        conn.execute(
            "INSERT INTO codex_prune_debt (source_path, source_root_key, "
            "recorded_at_utc) VALUES (?, '', '2026-07-20T00:00:00+00:00')",
            (str(home / "sessions" / name),))
        conn.commit()
    finally:
        conn.close()


def _seed_class(case, ns, paths, home):
    cache = _cache_mod()
    if case in ("prune", "core_raises"):
        _keeper_debt(ns, paths)
        return
    conn = _cache(ns)
    try:
        if case == "finalization":
            start = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
            cache.add_codex_finalization_debt(conn, spans={
                source_root_key(str(home.resolve())):
                    (start, start + dt.timedelta(days=1))})
        elif case == "owed":
            cache._store_codex_owed_maintenance(conn, {"change_log_prune": True})
        else:
            assert case == "root_cleanup", case
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta (key, value) VALUES (?, '1')",
                (cache.CODEX_PRUNE_ROOT_CLEANUP_KEY,))
        conn.commit()
    finally:
        conn.close()


def _class_present(case, ns) -> bool:
    cache = _cache_mod()
    conn = _cache(ns)
    try:
        if case in ("prune", "core_raises"):
            return conn.execute(
                "SELECT 1 FROM codex_prune_debt LIMIT 1").fetchone() is not None
        if case == "finalization":
            return not cache.load_codex_finalization_debt(conn).empty
        if case == "owed":
            return cache._codex_owed_anything(conn)
        return conn.execute(
            "SELECT 1 FROM cache_meta WHERE key=?",
            (cache.CODEX_PRUNE_ROOT_CLEANUP_KEY,)).fetchone() is not None
    finally:
        conn.close()


def source_root_key(value):
    from _lib_source_identity import source_root_key as key
    return key(value)


@pytest.mark.parametrize("case", [
    "finalization", "prune", "owed", "root_cleanup", "core_raises"])
def test_debt_recorded_during_the_settlement_is_consumed(
    settle_estate, monkeypatch, case,
):
    """Detects durable work stranded behind the attempt throttle (J3): debt
    a settlement's own sync or reconcile records while the drain holds its
    single-flight lock would otherwise wait for a spawn the original marker
    throttles for 600 s. A core that raises after its publication committed
    must not skip the continuation either."""
    ns, paths, _spawned, _clock, home = settle_estate
    present_at_return: list = []

    def seed():
        _seed_class(case, ns, paths, home)
        present_at_return.append(_class_present(case, ns))
    _after_settlement_core(monkeypatch, seed,
                           raise_after=(case == "core_raises"))
    result = _executor()(wait_for_requests=False, settle_projection=True)
    assert present_at_return == [True], "the seam seeded nothing"
    expected = "error" if case == "core_raises" else "published"
    assert result.settlement == expected, result
    assert not _class_present(case, ns), (
        f"the continuation left the {case} work the settlement recorded")
    assert result.work_remaining is False, result


def test_the_continuation_keeps_one_budget(settle_estate, monkeypatch):
    """Detects a continuation that resets the run's budget or retries a kind
    the first phase blocked (#1034 §5.4): one invocation has one
    ``max_identities`` and one blocked set, and runs one settlement."""
    ns, paths, _spawned, _clock, home = settle_estate
    cache = _cache_mod()
    conn = _cache(ns)
    try:
        cache._store_codex_owed_maintenance(conn, {"change_log_prune": True})
        conn.commit()
    finally:
        conn.close()
    real_unit = cache._run_codex_executor_unit
    attempts: list = []

    def failing_unit(conn, unit):
        if unit == "change_log_prune":
            attempts.append(unit)
            return False, False
        return real_unit(conn, unit)
    monkeypatch.setattr(cache, "_run_codex_executor_unit", failing_unit)
    debt_at_settlement: list = []

    def seed():
        debt_at_settlement.append(_count(
            ns, "SELECT COUNT(*) FROM codex_prune_debt"))
        _keeper_debt(ns, paths)
        _stale_debt(ns, home)
    _after_settlement_core(monkeypatch, seed)
    settlements = _count_settlements(monkeypatch)
    budget = ORPHANS + 1

    result = _executor()(wait_for_requests=False, max_identities=budget,
                         settle_projection=True)

    assert debt_at_settlement == [0], "the first phase consumed nothing"
    assert settlements == [1], settlements
    assert result.identities_deleted + result.identities_dropped == budget
    assert result.remaining == 1, result
    assert attempts == ["change_log_prune"], (
        "the continuation retried a kind the first phase blocked")
    assert _class_present("owed", ns)
    assert result.work_remaining is True and result.outcome == "partial"


def _publish_request(ns):
    return adm.publish_waiting_request(ns["APP_DIR"], root_keys=["hook"])


def _watermark(ns):
    import _cctally_quota as quota
    stats = ns["open_db"]()
    try:
        return quota._ledger_state(stats)["watermark"]
    finally:
        stats.close()


@pytest.mark.parametrize("case", [
    "clean", "blocked", "capped", "admission_refused", "redirtied"])
def test_the_exit_report(settle_estate, monkeypatch, case):
    """Detects a drain exit that misreports its remaining work (#1034 R-a,
    §5.4 point 6): ``outcome`` keeps the prune-row rule, ``work_remaining``
    names any remaining class, and ``ledger_retained`` is a count. A
    continuation's own deletions stay owed to the next hook, after the range
    the settlement published."""
    ns, paths, _spawned, _clock, _home = settle_estate
    cache = _cache_mod()
    kwargs = dict(wait_for_requests=False, settle_projection=True)
    watermark: list = []
    if case == "blocked":
        conn = _cache(ns)
        try:
            cache._store_codex_owed_maintenance(conn, {"change_log_prune": True})
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(cache, "_run_codex_executor_unit",
                            lambda conn, unit: (False, False))
    elif case == "capped":
        kwargs["max_identities"] = 1
    elif case == "admission_refused":
        assert _publish_request(ns)
    elif case == "redirtied":
        def seed():
            watermark.append(_watermark(ns))
            _keeper_debt(ns, paths)
        _after_settlement_core(monkeypatch, seed)

    result = _executor()(**kwargs)

    assert isinstance(result.ledger_retained, int)
    assert result.ledger_retained >= 0, result
    expected = {
        "clean": ("drained", False, "published"),
        "blocked": ("drained", True, "published"),
        # The debt the cap left makes the pin unusable (prune_debt), so the
        # settlement declines rather than publish a partial population.
        "capped": ("partial", True, "not_ready"),
        "admission_refused": ("yielded", True, "none"),
        "redirtied": ("drained", False, "published"),
    }[case]
    assert (result.outcome, result.work_remaining, result.settlement) == (
        expected), result
    if case == "redirtied":
        from test_codex_hook_lifecycle import _next_eligible_scope_units
        assert result.identities_deleted == ORPHANS + 1, result
        assert _watermark(ns) == watermark[0], (
            "something republished after the settlement")
        conn = _cache(ns)
        try:
            ops = [row[0] for row in conn.execute(
                "SELECT op FROM quota_window_change_log WHERE seq > ?",
                (watermark[0],))]
        finally:
            conn.close()
        assert ops and set(ops) == {"delete"}, ops
        assert _next_eligible_scope_units(ns) > 0


def _keeper_retained(ns, keeper):
    return _count(ns, "SELECT COUNT(*) FROM codex_prune_debt "
                      "WHERE source_path=?", (keeper,)) == 1


def test_continuation_waits_out_a_fresh_request_under_production_admission(
    settle_estate, monkeypatch,
):
    """Detects a production continuation that returns on a refusal and
    strands the work behind the attempt throttle, or one that deletes while a
    hook's waiting request is fresh (J3 admission): with
    ``wait_for_requests=True`` the phase backs off until the request expires,
    then consumes the work."""
    ns, paths, _spawned, clock, _home = settle_estate
    cache = _cache_mod()
    keeper: list = []

    def seed():
        keeper.append(_keeper_debt(ns, paths))
        assert _publish_request(ns)
    _after_settlement_core(monkeypatch, seed)
    sleeps: list = []

    def advancing_sleep(seconds):
        sleeps.append((seconds, bool(_requests(ns)),
                       _keeper_retained(ns, keeper[0])))
        clock.now += int(seconds * 1_000_000_000)
        assert len(sleeps) < 100, "the backoff never ended"
    monkeypatch.setattr(adm, "_sleep", advancing_sleep)
    fresh_at_commit: list = []
    monkeypatch.setattr(cache, "_CODEX_PRUNE_PRECOMMIT_HOOK",
                        lambda path, root: fresh_at_commit.append(
                            (path, bool(_requests(ns)))))

    result = _executor()(wait_for_requests=True, settle_projection=True)

    assert result.yielded >= 1, "the continuation was never refused"
    assert sleeps and sleeps[0][1] and sleeps[0][2], (
        "the backoff did not start while the request was fresh and the work "
        "retained", sleeps)
    assert (keeper[0], False) in fresh_at_commit, fresh_at_commit
    assert all(not fresh for _path, fresh in fresh_at_commit), fresh_at_commit
    assert not _keeper_retained(ns, keeper[0])
    assert result.outcome == "drained" and result.settlement == "published"


def test_continuation_refusal_ends_the_phase_without_waiting(
    settle_estate, monkeypatch,
):
    """The ``wait_for_requests=False`` half of J3 admission: the first
    refusal ends the phase, and the work stays as debt for a later drain."""
    ns, paths, _spawned, _clock, _home = settle_estate
    keeper: list = []

    def seed():
        keeper.append(_keeper_debt(ns, paths))
        assert _publish_request(ns)
    _after_settlement_core(monkeypatch, seed)
    sleeps: list = []
    monkeypatch.setattr(adm, "_sleep", sleeps.append)

    result = _executor()(wait_for_requests=False, settle_projection=True)

    assert sleeps == [] and result.yielded == 1, (sleeps, result)
    assert _keeper_retained(ns, keeper[0])
    assert result.outcome == "yielded" and result.work_remaining is True


def _marker(ns):
    return ns["APP_DIR"] / _cache_mod().CODEX_PRUNE_DRAIN_MARKER_NAME


def test_continuation_leaves_the_original_spawn_marker_unchanged(
    settle_estate, monkeypatch,
):
    """Detects a drain that resets or restamps its own attempt marker (J3):
    the spawn that started this drain stamped it, so a request raised inside
    the settlement (a refused projection asking for model resolution) is
    throttled, and the continuation, not a new worker, consumes the work."""
    ns, paths, spawned, _clock, _home = settle_estate
    cache = _cache_mod()
    import _cctally_quota as quota
    # The fixture's ingest already requested a drain; this run's marker is
    # the one its own spawning request stamps.
    _marker(ns).unlink(missing_ok=True)
    spawned.clear()
    assert cache._request_codex_drain_worker(
        cache.CODEX_PRUNE_DRAIN_COMMAND) == "spawned"
    assert spawned == [cache.CODEX_PRUNE_DRAIN_COMMAND]
    stamp = _marker(ns).stat().st_mtime_ns
    requests: list = []

    def seed():
        _keeper_debt(ns, paths)
        requests.append(cache._request_codex_drain_worker(
            cache.CODEX_PRUNE_DRAIN_COMMAND))
        quota._request_codex_model_resolution()
    _after_settlement_core(monkeypatch, seed)

    result = _executor()(wait_for_requests=False, settle_projection=True)

    assert requests == ["throttled"], requests
    assert _marker(ns).stat().st_mtime_ns == stamp
    assert spawned == [cache.CODEX_PRUNE_DRAIN_COMMAND], spawned
    assert result.identities_deleted == ORPHANS + 1 and result.remaining == 0


def test_an_expired_marker_restamps_and_the_second_worker_is_busy(
    settle_estate, monkeypatch,
):
    """The expired half of J3's marker rule: a request raised inside the
    settlement restamps an expired marker and spawns a worker, which finds
    the single-flight lock held and returns ``busy``; the running drain's
    continuation consumes the work."""
    ns, paths, _spawned, _clock, _home = settle_estate
    cache = _cache_mod()
    marker = _marker(ns)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    aged = time.time() - cache.CODEX_PRUNE_DRAIN_SPAWN_THROTTLE_SECONDS - 60
    os.utime(marker, (aged, aged))
    second: list = []

    def spawn(command):
        # The spawned prune worker, run in-process; other workers are inert.
        if command == cache.CODEX_PRUNE_DRAIN_COMMAND:
            second.append(cache.run_codex_prune_drain(wait_for_requests=False))
        return True
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached", spawn)
    requests: list = []

    def seed():
        _keeper_debt(ns, paths)
        requests.append(cache._request_codex_drain_worker(
            cache.CODEX_PRUNE_DRAIN_COMMAND))
    _after_settlement_core(monkeypatch, seed)

    result = _executor()(wait_for_requests=False, settle_projection=True)

    assert requests == ["spawned"], requests
    assert marker.stat().st_mtime > aged + 60, "the marker was not restamped"
    assert [r.outcome for r in second] == ["busy"], second
    assert result.identities_deleted == ORPHANS + 1 and result.remaining == 0
