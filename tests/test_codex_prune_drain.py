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

from conftest import load_script, redirect_paths  # noqa: E402
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

    def counting_discover():
        calls["discover"] += 1
        return discover()

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
    worker = threading.Thread(
        target=drain, kwargs={"wait_for_requests": True}, daemon=True)
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

    verifier = threading.Thread(target=verify, daemon=True)
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
    worker = threading.Thread(target=cache_mod.run_codex_prune_drain,
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
