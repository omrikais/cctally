"""Task15 corrections: immediate root reads and last-good metadata resolution."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import threading

import pytest

from conftest import load_script, redirect_paths
from _codex_zstd_fixtures import compress_in_place, token_records, write_rollout
from _lib_source_identity import source_root_key
from test_codex_hook_lifecycle import _hook_args
from test_codex_conversation_normalization import (  # noqa: F401
    _v850_corrupt, _v850_project_key, v850_store,
)

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
        # #1108 T6: the extracted predicate is exactly the inline rule
        # `iter_codex_entries` applies, for every row it covers, plus a BLOB
        # absolute path and an unregistered absolute root.
        cache = sys.modules["_cctally_cache"]
        monkeypatch.setenv("CODEX_HOME", str(b))
        scope = cache._codex_prune_scope(cache._discover_codex_files_with_roots())
        conn.execute(
            "INSERT INTO codex_session_entries (source_path,line_offset,timestamp_utc,session_id,model,"
            "input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,source_root_key) "
            "VALUES (?,1,?,'ghost','gpt-5',1,0,1,0,2,'ghost-root')",
            ("/ghost/unregistered.jsonl", START.isoformat()))
        conn.commit()
        kept = {row.source_path for row in ns["iter_codex_entries"](
            conn, START, END, root_scope=scope)}
        stored = conn.execute(
            "SELECT source_root_key, source_path FROM codex_session_entries").fetchall()
        assert {path for root, path in stored
                if not cache.codex_row_withheld(scope, root, path)} == kept
        expect_withheld = replacement == "recognized"
        assert ("/ghost/unregistered.jsonl" not in kept) is expect_withheld
        assert cache.codex_row_withheld(
            scope, "ghost-root", b"/ghost/blob.jsonl") is expect_withheld
        assert not cache.codex_row_withheld(scope, "ghost-root", b"rel/blob.jsonl")
        assert not cache.codex_row_withheld(scope, None, "/unknown/history.jsonl")
        assert not cache.codex_row_withheld(None, "ghost-root", "/ghost/x.jsonl")
        assert cache.codex_root_withheld(scope, "ghost-root") is expect_withheld
        assert not cache.codex_root_withheld(scope, None)
        for key in scope.configured_root_keys:
            assert not cache.codex_root_withheld(scope, key)
        assert cache.codex_root_scope_identity(None) is None
        assert cache.codex_root_scope_identity(scope) == (
            tuple(sorted(scope.configured_root_keys)),
            bool(scope.recognized_root_keys))
        # #1108 T6, the dashboard readers: the qualified kernel and the rooted
        # fallback keep exactly the predicate's population. A BLOB absolute
        # path joins the rows; a NULL identity is kept and still meets the
        # rooted reader's existing typed refusal.
        analytics = sys.modules["_cctally_source_analytics"]
        conn.execute(
            "INSERT INTO codex_session_entries (source_path,line_offset,timestamp_utc,session_id,model,"
            "input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,source_root_key) "
            "VALUES (?,2,?,'ghost','gpt-5',1,0,1,0,2,'ghost-root')",
            (b"/ghost/blob.jsonl", START.isoformat()))
        conn.commit()
        expected = {(root, path) for root, path in conn.execute(
            "SELECT source_root_key, source_path FROM codex_session_entries")
            if not cache.codex_row_withheld(scope, root, path)}
        previous = conn.row_factory
        try:
            rows, _inherited = analytics._read_qualified_codex_rows(
                conn, START, END, None, None, root_scope=scope)
        finally:
            conn.row_factory = previous
        assert {(row["source_root_key"], row["source_path"])
                for row in rows} == expected
        with pytest.raises(analytics.QualifiedMetadataUnavailable):
            analytics.load_cached_rooted_codex_accounting_entries(
                START, END, speed="standard", cache_conn=conn,
                root_scope=scope)
        conn.execute("DELETE FROM codex_session_entries WHERE source_root_key IS NULL"
                     " OR typeof(source_path) = 'blob'")
        conn.commit()
        rooted = analytics.load_cached_rooted_codex_accounting_entries(
            START, END, speed="standard", cache_conn=conn, root_scope=scope)
        assert {(row.source_root_key, row.source_path) for row in rooted} == {
            (root, path) for root, path in expected
            if root is not None and isinstance(path, str)}
    finally:
        conn.close()


def _scope_store(tmp_path, monkeypatch):
    """A synced current root `a` plus a cache connection (#1108 T3)."""
    ns = _runtime(tmp_path, monkeypatch)
    a = tmp_path / "a"
    write_rollout(a / "sessions" / "a.jsonl", token_records(1, session_id="a"))
    monkeypatch.setenv("CODEX_HOME", str(a))
    conn = ns["open_cache_db"]()
    ns["sync_codex_cache"](conn, maintenance="caller")
    return ns, sys.modules["_cctally_cache"], a, conn


def _register_root(conn, key, path):
    conn.execute(
        "INSERT INTO codex_source_roots (source_root_key, canonical_root_path,"
        " first_seen_utc, last_seen_utc) VALUES (?,?,?,?)",
        (key, path, START.isoformat(), START.isoformat()))
    conn.commit()


def _counting_discover(cache, calls):
    real = cache._discover_codex_files_with_roots

    def discover():
        calls.append(1)
        return real()
    return discover


def test_codex_read_scope_steady_state_runs_no_filesystem_probe(
    tmp_path, monkeypatch,
):
    """#1108 T3: with no withholding candidate the resolver touches no
    rollout. Failure it detects: an owner tick paying canonical discovery and
    the recognition probe on every build of an ordinary store."""
    ns, cache, _a, conn = _scope_store(tmp_path, monkeypatch)

    def refuse(*_args, **_kwargs):
        raise AssertionError("steady state must not probe the filesystem")

    monkeypatch.setattr(cache, "_discover_codex_files_with_roots", refuse)
    monkeypatch.setattr(cache, "_codex_file_is_recognizable", refuse)
    try:
        assert cache.resolve_codex_read_scope(
            conn, sync_stats=None, retained=None, now_monotonic=100.0,
        ) == (None, None)
    finally:
        conn.close()


def test_codex_read_scope_resolution_freshness(tmp_path, monkeypatch):
    """#1108 T3: candidate and configured-key changes rediscover at once,
    unchanged evidence is reused only within the bound, and a whole-walk sync
    scope is used only while young. Failures it detects: stale withholding
    after a root change, an expired sync renewing evidence, and discovery on
    every tick while candidates exist."""
    ns, cache, a, conn = _scope_store(tmp_path, monkeypatch)
    bound = cache.CODEX_READ_SCOPE_MAX_AGE_S
    calls: list = []
    discover = _counting_discover(cache, calls)

    def resolve(now, retained=None, sync_stats=None):
        return cache.resolve_codex_read_scope(
            conn, sync_stats=sync_stats, retained=retained,
            now_monotonic=now, discover=discover)

    try:
        # Only an unregistered absolute root's rows: no short-circuit.
        conn.execute(
            "INSERT INTO codex_session_entries (source_path,line_offset,timestamp_utc,session_id,model,"
            "input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,source_root_key) "
            "VALUES ('/ghost/old.jsonl',1,?,'ghost','gpt-5',1,0,1,0,2,'ghost-root')",
            (START.isoformat(),))
        conn.commit()
        scope, evidence = resolve(1000.0)
        assert scope is not None and scope.recognized_root_keys
        assert evidence.candidate_root_keys == frozenset({"ghost-root"})
        assert evidence.observed_at == 1000.0 and len(calls) == 1
        # Unchanged (K, C) within the bound: reused, anchor unchanged.
        again, kept = resolve(1000.0 + bound, retained=evidence)
        assert again is scope and kept is evidence and len(calls) == 1
        # Past the bound: rediscovered.
        _scope, aged = resolve(1000.0 + bound + 1, retained=evidence)
        assert len(calls) == 2 and aged.observed_at == 1000.0 + bound + 1
        # A candidate change rediscovers at once.
        _register_root(conn, "retired-root", "/retired")
        _scope, widened = resolve(1001.0 + bound, retained=aged)
        assert len(calls) == 3
        assert widened.candidate_root_keys == {"ghost-root", "retired-root"}
        # A configured-key change rediscovers at once.
        b = tmp_path / "b"
        write_rollout(b / "sessions" / "b.jsonl", token_records(1, session_id="b"))
        monkeypatch.setenv("CODEX_HOME", f"{a},{b}")
        moved, rekeyed = resolve(1002.0 + bound, retained=widened)
        assert len(calls) == 4
        assert rekeyed.configured_root_keys == moved.configured_root_keys
        assert source_root_key(str(b)) in moved.configured_root_keys

        # A fresh whole-walk sync scope is used without discovery and keeps
        # its own anchor; an expired one is rejected though it arrived now.
        sync = ns["sync_codex_cache"](conn, maintenance="caller")
        assert sync.whole_walk_scope is not None
        assert sync.whole_walk_scope.configured_root_keys == moved.configured_root_keys
        observed = sync.whole_walk_scope_observed_at
        used, from_sync = resolve(observed + bound, sync_stats=sync)
        assert used is sync.whole_walk_scope and len(calls) == 4
        assert from_sync.observed_at == observed
        reused, still = resolve(observed + bound, retained=from_sync)
        assert reused is used and still.observed_at == observed
        _scope, fresh = resolve(observed + bound + 1, sync_stats=sync,
                                retained=from_sync)
        assert len(calls) == 5 and fresh.observed_at == observed + bound + 1
        # A targeted sync exposes no whole-walk scope.
        targeted = ns["sync_codex_cache"](
            conn, maintenance="caller",
            only_paths=[str(b / "sessions" / "b.jsonl")])
        assert targeted.whole_walk_scope is None
        assert targeted.whole_walk_scope_observed_at is None
    finally:
        conn.close()


@pytest.mark.parametrize("flip", ["gains_envelope", "displaced"])
def test_codex_read_scope_recognition_flip_is_seen_after_the_bound(
    tmp_path, monkeypatch, flip,
):
    """#1108 T3: a recognition change that moves neither K nor C (an envelope
    appended to an existing file; a recognition witness displaced by 32 newer
    unrecognizable files) is seen on the first resolution past the bound.
    Failure it detects: canonical evidence retained indefinitely."""
    ns, cache, a, conn = _scope_store(tmp_path, monkeypatch)
    bound = cache.CODEX_READ_SCOPE_MAX_AGE_S
    b = tmp_path / "b"
    rollout = b / "sessions" / "b.jsonl"
    if flip == "gains_envelope":
        write_rollout(rollout, [{"type": "future-record", "payload": {}}])
    else:
        write_rollout(rollout, token_records(1, session_id="b"))
    monkeypatch.setenv("CODEX_HOME", str(b))
    calls: list = []
    discover = _counting_discover(cache, calls)
    try:
        first, evidence = cache.resolve_codex_read_scope(
            conn, sync_stats=None, retained=None, now_monotonic=10.0,
            discover=discover)
        assert bool(first.recognized_root_keys) is (flip == "displaced")
        if flip == "gains_envelope":
            with rollout.open("a", encoding="utf-8") as fh:
                for record in token_records(1, session_id="b"):
                    fh.write(json.dumps(record) + "\n")
        else:
            for index in range(cache._CODEX_ROOT_RECOGNITION_MAX_FILES):
                write_rollout(b / "sessions" / f"z-{index:02d}.jsonl",
                              [{"type": "future-record", "payload": {}}])
        held, _ = cache.resolve_codex_read_scope(
            conn, sync_stats=None, retained=evidence,
            now_monotonic=10.0 + bound, discover=discover)
        assert held is first and len(calls) == 1
        flipped, _ = cache.resolve_codex_read_scope(
            conn, sync_stats=None, retained=evidence,
            now_monotonic=10.0 + bound + 1, discover=discover)
        assert len(calls) == 2
        assert bool(flipped.recognized_root_keys) is (flip == "gains_envelope")
    finally:
        conn.close()


# ── #1108 T3: the no-lag gate (spec §3.2 step 2a) ────────────────────────────
# Each case drives real syncs over one rollout `r.jsonl` and resolves the
# scope a dashboard owner would read under. Failure every case detects: a
# frame that withholds a retired copy holding progress no current copy has
# (an empty or short Codex section), or one that stops withholding a
# retired copy every current copy has caught up with (a doubled section).


def _gate_store(tmp_path, monkeypatch):
    ns = _runtime(tmp_path, monkeypatch)
    return ns, sys.modules["_cctally_cache"]


def _home(monkeypatch, *roots):
    monkeypatch.setenv("CODEX_HOME", ",".join(str(root) for root in roots))


def _fresh(cache, conn):
    """A fresh resolution: no sync stats and no retained evidence."""
    return cache.resolve_codex_read_scope(
        conn, sync_stats=None, retained=None, now_monotonic=1_000.0)


def _from_sync(cache, conn, stats):
    """Step 3: the sync's own discovery against this post-sync connection."""
    assert stats.whole_walk_scope is not None
    return cache.resolve_codex_read_scope(
        conn, sync_stats=stats, retained=None,
        now_monotonic=stats.whole_walk_scope_observed_at)


def _visible(cache, conn, scope):
    return sorted(
        (str(path), offset, root) for path, offset, root in conn.execute(
            "SELECT source_path, line_offset, source_root_key "
            "FROM codex_session_entries")
        if not cache.codex_row_withheld(scope, root, path))


def _cursors(conn):
    return {str(path): (root, offset, complete)
            for path, root, offset, complete in conn.execute(
                "SELECT path, source_root_key, last_byte_offset, ingest_complete "
                "FROM codex_session_files")}


def _budget_stopped_sync(ns, cache, conn, monkeypatch):
    """A real budgeted walk whose budget runs out right after the first
    record it reads, so the file commits a partial cursor."""
    real_iter = cache._iter_codex_fused_records_with_offsets
    state = {"expired": False}

    def iterate(*args, **kwargs):
        for emission in real_iter(*args, **kwargs):
            yield emission
            state["expired"] = True

    with monkeypatch.context() as patch:
        patch.setattr(cache, "_iter_codex_fused_records_with_offsets", iterate)
        patch.setattr(cache, "_walk_clock",
                      lambda: 1e9 if state["expired"] else 0.0)
        stats = ns["sync_codex_cache"](
            conn, maintenance="caller", budget_seconds=1_000.0)
    assert stats.budget_exhausted, "non-vacuity: the walk was not budget-stopped"
    return stats


def test_no_lag_gate_return_after_drain_waits_for_re_ingest(tmp_path, monkeypatch):
    """(i) A->B, B's walk completes, the drain deletes A's cursors, B->A: no
    withholding until A is re-ingested."""
    ns, cache = _gate_store(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    write_rollout(a / "sessions" / "r.jsonl", token_records(2, session_id="r"))
    _home(monkeypatch, a)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, maintenance="caller")
        a.rename(b)
        _home(monkeypatch, b)
        caught_up = ns["sync_codex_cache"](conn, maintenance="caller")
        assert _from_sync(cache, conn, caught_up)[0] is not None
        cache.run_codex_prune_drain(wait_for_requests=False)
        assert all(root != source_root_key(str(a))
                   for root, _o, _c in _cursors(conn).values()), (
            "non-vacuity: the drain left the retired root's cursors")
        before = _visible(cache, conn, None)
        assert before, "non-vacuity: the relocated copy holds no rows"
        b.rename(a)
        _home(monkeypatch, a)
        scope, _evidence = _fresh(cache, conn)
        assert scope is None, "withheld a retired copy A has no cursor for"
        assert _visible(cache, conn, scope) == before
        re_ingest = ns["sync_codex_cache"](conn, maintenance="caller")
        assert _from_sync(cache, conn, re_ingest)[0] is not None
        scope, _evidence = _fresh(cache, conn)
        assert scope is not None and scope.recognized_root_keys
        assert {root for _p, _o, root in _visible(cache, conn, scope)} == {
            source_root_key(str(a))}
    finally:
        conn.close()


def test_no_lag_gate_partial_walks(tmp_path, monkeypatch):
    """(ii) A budget-stopped re-ingest walk blocks (its own discovery against
    the post-sync store); once the relocation completed, a budget-stopped
    APPEND (`ingest_complete = 0`) only moves the current copy further ahead,
    so a freshly evaluated gate still withholds."""
    ns, cache = _gate_store(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    write_rollout(a / "sessions" / "r.jsonl", token_records(3, session_id="r"))
    _home(monkeypatch, a)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, maintenance="caller")
        a.rename(b)
        _home(monkeypatch, b)
        rollout = str(b / "sessions" / "r.jsonl")
        partial = _budget_stopped_sync(ns, cache, conn, monkeypatch)
        assert _cursors(conn)[rollout][2] == 0, "non-vacuity: no partial cursor"
        assert _from_sync(cache, conn, partial)[0] is None
        assert _fresh(cache, conn)[0] is None
        ns["sync_codex_cache"](conn, maintenance="caller")
        _root, caught_up, _complete = _cursors(conn)[rollout]
        write_rollout(b / "sessions" / "r.jsonl", token_records(6, session_id="r"))
        appended = _budget_stopped_sync(ns, cache, conn, monkeypatch)
        _root, progress, complete = _cursors(conn)[rollout]
        assert complete == 0 and progress > caught_up, (
            f"non-vacuity: no partial append was committed "
            f"({caught_up=} {progress=} {complete=})")
        assert _from_sync(cache, conn, appended)[0] is not None
        scope, _evidence = _fresh(cache, conn)
        assert scope is not None and scope.recognized_root_keys
        assert {root for _p, _o, root in _visible(cache, conn, scope)} == {
            source_root_key(str(b))}
    finally:
        conn.close()


@pytest.mark.parametrize("order", ["requalified_away", "deduplicated_into"])
def test_no_lag_gate_overlapping_roots(tmp_path, monkeypatch, order):
    """(iii) Parent A completes, then `[B, A]` requalifies the files to B,
    then `[A]` alone: blocked until A re-owns them. (iv) `[A, B]` keeps B's
    files under A, then `[B]` alone: blocked. B is a directory A walks, so
    both roots see `r.jsonl` and the current owner has no own cursor; A's
    own rollout keeps A recognized while B owns `r.jsonl`."""
    ns, cache = _gate_store(tmp_path, monkeypatch)
    a = tmp_path / "a"
    nested = a / "sessions" / "2026"
    shared = str(nested / "r.jsonl")
    write_rollout(nested / "r.jsonl", token_records(2, session_id="r"))
    write_rollout(a / "sessions" / "own.jsonl", token_records(1, session_id="o"))
    conn = ns["open_cache_db"]()
    try:
        if order == "requalified_away":
            _home(monkeypatch, a)
            ns["sync_codex_cache"](conn, maintenance="caller")
            _home(monkeypatch, nested, a)
            owner, alone = nested, a
        else:
            _home(monkeypatch, a, nested)
            owner, alone = a, nested
        ns["sync_codex_cache"](conn, maintenance="caller")
        assert _cursors(conn)[shared][0] == source_root_key(str(owner)), (
            "non-vacuity: unexpected owner")
        before = _visible(cache, conn, None)
        assert before, "non-vacuity: no rows"
        _home(monkeypatch, alone)
        scope, _evidence = _fresh(cache, conn)
        assert scope is None, "withheld the only copy of the rows"
        assert _visible(cache, conn, scope) == before
        if order == "requalified_away":
            re_owned = ns["sync_codex_cache"](conn, maintenance="caller")
            assert _cursors(conn)[shared][0] == source_root_key(str(a)), (
                "non-vacuity: A did not re-own")
            assert _from_sync(cache, conn, re_owned)[0] is not None
            assert len(_visible(cache, conn, _fresh(cache, conn)[0])) == len(before)
    finally:
        conn.close()


def test_no_lag_gate_return_after_growth(tmp_path, monkeypatch):
    """(v) A ingested to X; A->B; append Y; B ingested to X+Y with A's debt
    pending; B->A; before A is re-ingested its cursor stops at X, behind
    B's X+Y, so the gate blocks and B's Y stays visible."""
    ns, cache = _gate_store(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    write_rollout(a / "sessions" / "r.jsonl", token_records(2, session_id="r"))
    _home(monkeypatch, a)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, maintenance="caller")
        a.rename(b)
        write_rollout(b / "sessions" / "r.jsonl", token_records(4, session_id="r"))
        _home(monkeypatch, b)
        ns["sync_codex_cache"](conn, maintenance="caller")
        cursors = _cursors(conn)
        x = cursors[str(a / "sessions" / "r.jsonl")][1]
        x_plus_y = cursors[str(b / "sessions" / "r.jsonl")][1]
        assert x < x_plus_y, f"non-vacuity: no growth ({cursors!r})"
        b.rename(a)
        _home(monkeypatch, a)
        scope, _evidence = _fresh(cache, conn)
        assert scope is None, "withheld B, whose copy is ahead of A's"
        grown = [row for row in _visible(cache, conn, scope)
                 if row[2] == source_root_key(str(b)) and row[1] >= x]
        assert grown, "B's appended rows are hidden"
    finally:
        conn.close()


@pytest.mark.parametrize("compressed", [False, True])
def test_no_lag_gate_caught_up_store_withholds_on_first_resolution(
    tmp_path, monkeypatch, compressed,
):
    """(vi) A fresh process over a caught-up store withholds on its first
    resolution, also when the current copy is the `.jsonl.zst` twin of the
    retired plain copy (progress is the decoded-stream offset)."""
    ns, cache = _gate_store(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    write_rollout(a / "sessions" / "r.jsonl", token_records(2, session_id="r"))
    _home(monkeypatch, a)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, maintenance="caller")
        a.rename(b)
        if compressed:
            compress_in_place(b / "sessions" / "r.jsonl")
        _home(monkeypatch, b)
        ns["sync_codex_cache"](conn, maintenance="caller")
        cursors = _cursors(conn)
        assert cursors[str(b / "sessions" / "r.jsonl")][1] == (
            cursors[str(a / "sessions" / "r.jsonl")][1]), cursors
    finally:
        conn.close()
    fresh = ns["open_cache_db"]()
    try:
        scope, _evidence = _fresh(cache, fresh)
        assert scope is not None and scope.recognized_root_keys
        assert {root for _p, _o, root in _visible(cache, fresh, scope)} == {
            source_root_key(str(b))}
    finally:
        fresh.close()


def test_no_lag_gate_ignores_rollouts_without_a_retired_copy(tmp_path, monkeypatch):
    """(vii) A brand-new rollout under a current root, and an unrelated home
    switch (#108's deliberate A -> C scoping), never block."""
    ns, cache = _gate_store(tmp_path, monkeypatch)
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    write_rollout(a / "sessions" / "r.jsonl", token_records(2, session_id="r"))
    _home(monkeypatch, a)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, maintenance="caller")
        a.rename(b)
        _home(monkeypatch, b)
        ns["sync_codex_cache"](conn, maintenance="caller")
        write_rollout(b / "sessions" / "new.jsonl", token_records(1, session_id="n"))
        scope, _evidence = _fresh(cache, conn)
        assert scope is not None and scope.recognized_root_keys
        write_rollout(c / "sessions" / "other.jsonl",
                      token_records(1, session_id="o"))
        _home(monkeypatch, c)
        scope, _evidence = _fresh(cache, conn)
        assert scope is not None and scope.recognized_root_keys
        assert _visible(cache, conn, scope) == []
    finally:
        conn.close()


def test_no_lag_gate_covers_unrecognized_configured_roots(tmp_path, monkeypatch):
    """Mixed recognition: configured A is recognized, B was relocated to C,
    and 32 newer unrecognizable files under C push C's recognizable rollout
    out of the bounded recognition probe. A's recognition authorizes
    withholding every retired root, B included, so C's copy of B's rollout
    must be checked too: before C is ingested it lags, and B stays visible.
    Failure it detects: B's rows vanishing because the gate looked only at
    rollouts under recognized roots."""
    ns, cache = _gate_store(tmp_path, monkeypatch)
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    write_rollout(a / "sessions" / "own.jsonl", token_records(1, session_id="o"))
    write_rollout(b / "sessions" / "r.jsonl", token_records(2, session_id="r"))
    _home(monkeypatch, a, b)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, maintenance="caller")
        before = _visible(cache, conn, None)
        b.rename(c)
        for index in range(cache._CODEX_ROOT_RECOGNITION_MAX_FILES):
            write_rollout(c / "sessions" / f"z-{index:02d}.jsonl",
                          [{"type": "future-record", "payload": {}}])
        _home(monkeypatch, a, c)
        probe = cache._codex_prune_scope(cache._discover_codex_files_with_roots())
        assert probe.recognized_root_keys == {source_root_key(str(a))}, (
            "non-vacuity: C must be configured but unrecognized")
        scope, _evidence = _fresh(cache, conn)
        assert scope is None, "withheld B, whose only current copy lags"
        assert _visible(cache, conn, scope) == before
    finally:
        conn.close()


def test_no_lag_gate_retained_verdict_never_outlives_cursor_growth(
    tmp_path, monkeypatch,
):
    """The retained-owner round trip: retired A and current B both ingested
    through X, and the owner retains a lag-free verdict for K={B}, C={A}.
    Between ticks B->A, Y is appended, A is ingested through X+Y, then A->B
    before B is re-ingested. The next resolution sees the same K and C within
    the bound, so it may reuse the discovery, but the verdict is evaluated
    against the current cursors and blocks. Failure it detects: a retained
    verdict withholding A and hiding Y."""
    ns, cache = _gate_store(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    write_rollout(a / "sessions" / "r.jsonl", token_records(2, session_id="r"))
    _home(monkeypatch, a)
    conn = ns["open_cache_db"]()
    calls: list = []
    discover = _counting_discover(cache, calls)
    try:
        ns["sync_codex_cache"](conn, maintenance="caller")
        a.rename(b)
        _home(monkeypatch, b)
        ns["sync_codex_cache"](conn, maintenance="caller")
        scope, evidence = cache.resolve_codex_read_scope(
            conn, sync_stats=None, retained=None, now_monotonic=100.0,
            discover=discover)
        assert scope is not None and evidence.lag_free, (
            "non-vacuity: the caught-up copy must withhold first")
        b.rename(a)
        write_rollout(a / "sessions" / "r.jsonl", token_records(4, session_id="r"))
        _home(monkeypatch, a)
        ns["sync_codex_cache"](conn, maintenance="caller")
        a.rename(b)
        _home(monkeypatch, b)
        cursors = _cursors(conn)
        x = cursors[str(b / "sessions" / "r.jsonl")][1]
        assert x < cursors[str(a / "sessions" / "r.jsonl")][1], (
            f"non-vacuity: A did not grow past B ({cursors!r})")
        held, kept = cache.resolve_codex_read_scope(
            conn, sync_stats=None, retained=evidence, now_monotonic=101.0,
            discover=discover)
        assert len(calls) == 1, "non-vacuity: the discovery was not reused"
        assert (kept.configured_root_keys, kept.candidate_root_keys) == (
            evidence.configured_root_keys, evidence.candidate_root_keys)
        assert kept.observed_at == evidence.observed_at
        assert held is None, "a retained verdict hid A's growth"
        grown = [row for row in _visible(cache, conn, held)
                 if row[2] == source_root_key(str(a)) and row[1] >= x]
        assert grown, "A's appended rows are hidden"
    finally:
        conn.close()


def _new_rollout_round_trip(ns, cache, tmp_path, monkeypatch, conn):
    """Retired A and current B caught up on `r.jsonl`; returns B's sync stats
    and a resolution's evidence taken then. Afterwards, between ticks, B->A,
    a NEW rollout `n.jsonl` is created and ingested under A, then A->B before
    B is re-ingested: `n` is a retired-root cursor the evidence's discovery
    never saw, and B holds no copy of it yet."""
    a, b = tmp_path / "a", tmp_path / "b"
    write_rollout(a / "sessions" / "r.jsonl", token_records(2, session_id="r"))
    _home(monkeypatch, a)
    ns["sync_codex_cache"](conn, maintenance="caller")
    a.rename(b)
    _home(monkeypatch, b)
    caught_up = ns["sync_codex_cache"](conn, maintenance="caller")
    scope, evidence = cache.resolve_codex_read_scope(
        conn, sync_stats=None, retained=None, now_monotonic=100.0)
    assert scope is not None and evidence.lag_free, (
        "non-vacuity: the caught-up copy must withhold first")
    b.rename(a)
    write_rollout(a / "sessions" / "n.jsonl", token_records(2, session_id="n"))
    _home(monkeypatch, a)
    ns["sync_codex_cache"](conn, maintenance="caller")
    a.rename(b)
    _home(monkeypatch, b)
    new_retired = str(a / "sessions" / "n.jsonl")
    assert _cursors(conn)[new_retired][0] == source_root_key(str(a)), (
        "non-vacuity: n was not ingested under the retired root")
    assert str(b / "sessions" / "n.jsonl") not in _cursors(conn), (
        "non-vacuity: B already holds n")
    return caught_up, evidence, new_retired


@pytest.mark.parametrize("source", ["retained", "sync"])
def test_no_lag_gate_reused_discovery_never_hides_a_new_rollout(
    tmp_path, monkeypatch, source,
):
    """#1108 V1: the new-rollout variant of the retained-owner round trip.
    K and C are unchanged and inside the bound, so the reused discovery (the
    retained evidence's, or the tick's whole-walk sync's) holds only
    `r.jsonl`, and `n`'s retired copy is never compared. Failure it detects:
    A withheld on that stale discovery, so `n`'s accounting vanishes from
    the frame (and every idle tick that reuses it)."""
    ns, cache = _gate_store(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        caught_up, evidence, new_retired = _new_rollout_round_trip(
            ns, cache, tmp_path, monkeypatch, conn)
        if source == "retained":
            held, _kept = cache.resolve_codex_read_scope(
                conn, sync_stats=None, retained=evidence, now_monotonic=101.0)
        else:
            held, _kept = cache.resolve_codex_read_scope(
                conn, sync_stats=caught_up, retained=None,
                now_monotonic=caught_up.whole_walk_scope_observed_at)
        assert held is None, "a stale discovery withheld the new rollout"
        assert any(path == new_retired
                   for path, _o, _r in _visible(cache, conn, held)), (
            "n's accounting is hidden")
    finally:
        conn.close()


def test_build_recheck_downgrades_on_a_new_retired_root_cursor(
    tmp_path, monkeypatch,
):
    """#1108 V1, the build-read variant: a new retired-root cursor appears
    between the owner's resolution and the build's pinned read. Failure it
    detects: the build's binding re-check (`codex_read_scope_lags`) judged
    the resolution's discovery lag-free and applied the scope, hiding `n`."""
    ns, cache = _gate_store(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        _caught_up, evidence, _new = _new_rollout_round_trip(
            ns, cache, tmp_path, monkeypatch, conn)
        assert cache.codex_read_scope_lags(conn, evidence), (
            "the build applied a scope whose discovery missed a new rollout")
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


# ── #854 R2: request-scoped threads-table resolution ──────────────────────

_854_LAST_GOOD = "temp.codex_last_good_threads"


def _854_store(tmp_path, monkeypatch):
    """A head-schema store pair with one barrier-population conversation.

    ``854-pop`` is in the barrier population and its published thread names
    ``/published`` while its unpublished raw row names ``/raw/unpublished``;
    ``854-raw`` is outside the population. A reader that bypasses the #968
    publication boundary therefore reads a different ``cwd`` than one that
    honors it.
    """
    ns = _runtime(tmp_path, monkeypatch)
    cols = sys.modules["_cctally_cache"]._CODEX_THREAD_SNAPSHOT_COLUMNS
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "INSERT INTO codex_source_roots (source_root_key, canonical_root_path,"
            " first_seen_utc, last_seen_utc) VALUES (?,?,?,?)",
            ("854-root", "/raw", "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z"))
        for key, cwd in (("854-pop", "/raw/unpublished"), ("854-raw", "/raw/plain")):
            cache.execute(
                "INSERT INTO codex_conversation_threads "
                "(conversation_key, source_root_key, native_thread_id,"
                " root_thread_id, parent_thread_id, source_path, cwd, git_json,"
                " first_seen_utc, last_seen_utc) VALUES (?,?,?,?,NULL,?,?,NULL,?,?)",
                (key, "854-root", f"native-{key}", f"native-{key}",
                 f"/raw/{key}.jsonl", cwd, "2026-11-01T00:00:00Z",
                 "2026-11-01T00:00:00Z"))
        cache.execute(
            "INSERT INTO codex_barrier_population "
            "(conversation_key, required_revision, entered_at_utc) "
            "VALUES ('854-pop', 1, '2026-11-01T00:00:00Z')")
        cache.commit()
        published = dict(zip(
            [name.strip() for name in cols.split(",")],
            cache.execute(
                f"SELECT {cols} FROM codex_conversation_threads "
                "WHERE conversation_key = '854-pop'").fetchone()))
    finally:
        cache.close()
    published["cwd"] = "/published"
    conversations = ns["open_conversations_db"]()
    try:
        conversations.execute(
            "INSERT INTO codex_conversation_publications "
            "(conversation_key, published_revision, updated_at_utc) "
            "VALUES ('854-pop', 1, '2026-11-01T00:00:00Z')")
        conversations.execute(
            f"INSERT INTO codex_conversation_published_threads ({cols}) "
            f"VALUES ({','.join('?' for _ in published)})",
            tuple(published.values()))
        conversations.commit()
    finally:
        conversations.close()
    return ns


def test_854_readonly_open_seeds_the_last_good_resolution(tmp_path, monkeypatch):
    """A viewer connection resolves the raw threads reference with no probe.

    Failure detected: wrong or stale project attribution on the viewer. The
    seed must name exactly what an uncached resolution computes (the #968
    last-good view), so a population conversation reads its PUBLISHED
    metadata and never its unpublished raw row, and account scoping (which
    replaces only the unqualified TEMP view) must leave that answer intact.
    """
    import _lib_codex_metadata as metadata

    _854_store(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    ro = cache_mod.open_conversations_db_readonly()
    try:
        statements = []
        ro.set_trace_callback(statements.append)
        assert metadata.resolve_codex_threads_table(ro) == _854_LAST_GOOD
        assert statements == [], "a read-only viewer connection re-probed its seeded resolution"
        metadata.invalidate_codex_threads_table_resolution(ro)
        assert metadata.resolve_codex_threads_table(ro) == _854_LAST_GOOD
        assert statements, "the uncached comparison never probed"
        cwd = dict(ro.execute(f"SELECT conversation_key, cwd FROM {_854_LAST_GOOD}"))
        assert cwd["854-pop"] == "/published", cwd
        assert cwd["854-raw"] == "/raw/plain", cwd
    finally:
        metadata.invalidate_codex_threads_table_resolution(ro)
        ro.close()

    # Traced from before scoping: scoping resolves the reference itself, so a
    # trace attached afterwards would pass on any tree where scoping re-probes
    # and re-caches the answer.
    scoped = cache_mod.open_conversations_db_readonly()
    try:
        resolution = []

        def trace(sql):
            if _854_in_resolution():
                resolution.append(sql)

        scoped.set_trace_callback(trace)
        cache_mod.scope_conversations_db_to_account(scoped, "854-account")
        assert metadata.resolve_codex_threads_table(scoped) == _854_LAST_GOOD
        assert resolution == [], (
            "account scoping or the scoped resolve re-probed the seeded resolution")
        assert scoped.execute(
            "SELECT COUNT(*) FROM codex_conversation_threads").fetchone() == (0,)
    finally:
        metadata.invalidate_codex_threads_table_resolution(scoped)
        scoped.close()


def test_854_a_full_open_holds_no_resolution_entry(tmp_path, monkeypatch):
    """The full opener never seeds, so a failed full open cannot pin one.

    Failure detected: a resolver-cache entry pinning a connection its owner
    never received. The full opener can close or abandon its connection after
    installing the last-good views (post-open refusals, a raising derivation),
    and nothing evicts on those paths, so the seed belongs to the read-only
    opener alone. The first open makes both derivations current, so the
    second performs no resolver call of its own.
    """
    import _lib_codex_metadata as metadata

    ns = _854_store(tmp_path, monkeypatch)
    ns["open_conversations_db"]().close()
    conn = ns["open_conversations_db"]()
    try:
        assert conn not in metadata._threads_table_resolution_cache
    finally:
        metadata.invalidate_codex_threads_table_resolution(conn)
        conn.close()


@pytest.mark.parametrize("failure", ["barrier_absent", "interrupted"])
def test_854_a_failed_installation_leaves_no_cached_answer(
    tmp_path, monkeypatch, failure,
):
    """An installation that does not complete forgets the connection's answer.

    Failure detected: stale metadata attribution. A cached reference from
    before a failed or interrupted reinstall can name a view that is gone or
    that no longer reflects the barrier, and the fail-closed readers would
    keep reading through it instead of probing afresh.
    """
    import _lib_codex_metadata as metadata

    entries = metadata._threads_table_resolution_cache
    if failure == "barrier_absent":
        _runtime(tmp_path, monkeypatch)
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("ATTACH DATABASE ':memory:' AS cache_db")
            conn.execute("CREATE TEMP VIEW codex_last_good_threads AS "
                         "SELECT 'k' AS conversation_key")
            assert metadata.resolve_codex_threads_table(conn) == _854_LAST_GOOD
            assert conn in entries
            assert sys.modules["_cctally_cache"]._install_codex_last_good_views(
                conn) is False
            assert conn not in entries
        finally:
            metadata.invalidate_codex_threads_table_resolution(conn)
            conn.close()
        return

    _854_store(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    ro = cache_mod.open_conversations_db_readonly()
    try:
        assert metadata.resolve_codex_threads_table(ro) == _854_LAST_GOOD
        assert ro in entries

        def deny_temp_views(action, *_args):
            if action == sqlite3.SQLITE_CREATE_TEMP_VIEW:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        ro.set_authorizer(deny_temp_views)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                cache_mod._install_codex_last_good_views(ro)
        finally:
            ro.set_authorizer(None)
        assert ro not in entries
    finally:
        metadata.invalidate_codex_threads_table_resolution(ro)
        ro.close()


def test_854_a_failed_maintenance_close_releases_the_connection(
    tmp_path, monkeypatch,
):
    """A read-only open that fails after connecting leaves nothing behind.

    Failure detected: a seeded connection that never reaches its owner. The
    maintenance-file close is the opener's last fallible step; when it raises,
    the caller never receives the connection, so neither request-teardown
    eviction can run and the opener itself must close the connection and
    leave no resolver-cache entry for it.
    """
    import builtins
    import os
    import pathlib

    import _lib_codex_metadata as metadata

    _854_store(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    maintenance = pathlib.Path(
        sys.modules["_cctally_core"].CONVERSATIONS_LOCK_MAINTENANCE_PATH)
    real_open = builtins.open

    class _RaisingClose:
        def __init__(self, fh):
            self._fh = fh

        def fileno(self):
            return self._fh.fileno()

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def close(self):
            self._fh.close()
            raise OSError("854 injected maintenance close failure")

    def opener(file, *args, **kwargs):
        fh = real_open(file, *args, **kwargs)
        if isinstance(file, (str, os.PathLike)) and pathlib.Path(file) == maintenance:
            return _RaisingClose(fh)
        return fh

    created = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    monkeypatch.setattr(cache_mod, "open", opener, raising=False)
    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(OSError, match="854 injected"):
        cache_mod.open_conversations_db_readonly()
    try:
        assert created, "the opener never connected"
        for conn in created:
            assert conn not in metadata._threads_table_resolution_cache
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
    finally:
        for conn in created:
            metadata.invalidate_codex_threads_table_resolution(conn)
            conn.close()


class _854Handler:
    path = "/api/conversations?source=codex"

    def __init__(self):
        self.errors = []
        self.responses = []

    def log_error(self, *args):
        self.errors.append(args)

    def _respond_json(self, status, body):
        self.responses.append((status, body))


def _854_in_resolution() -> bool:
    """Whether the statement being traced was issued by the resolver."""
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        if (code.co_name in ("resolve_codex_threads_table",
                             "_schema_holds_threads_table")
                and code.co_filename.endswith("_lib_codex_metadata.py")):
            return True
        frame = frame.f_back
    return False


def _854_new_run():
    return {"created": [], "resolution": [], "readonly": 0, "full": 0}


def _854_request(kernel, run, *, cancelled=None):
    """One real viewer request lifecycle, traced from ``sqlite3.connect``.

    No connection the request uses is touched before the request: it is
    created inside ``_run_conversation_query_impl`` and recorded here.
    """
    dash = sys.modules["_cctally_dashboard"]
    dconv = sys.modules["_cctally_dashboard_conversation"]
    real_connect = sqlite3.connect
    # #787 / #861: a request reads through one query session; the full
    # opener must never run on its behalf.
    real_readonly = dconv._open_conversation_session
    real_full = dash.open_conversations_db
    caller = threading.get_ident()

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if threading.get_ident() != caller:
            # The patch is process-wide; a stray thread's connection is not
            # this request's.
            return conn
        run["created"].append(conn)

        def trace(sql):
            if _854_in_resolution():
                run["resolution"].append(sql)

        conn.set_trace_callback(trace)
        return conn

    def readonly(*args, **kwargs):
        run["readonly"] += 1
        return real_readonly(*args, **kwargs)

    def full(*args, **kwargs):
        run["full"] += 1
        return real_full(*args, **kwargs)

    handler = _854Handler()
    sqlite3.connect = traced_connect
    dconv._open_conversation_session = readonly
    dash.open_conversations_db = full
    try:
        run["result"] = dconv._run_conversation_query_impl(
            handler, kernel, "test-854", cancelled=cancelled)
    finally:
        sqlite3.connect = real_connect
        dconv._open_conversation_session = real_readonly
        dash.open_conversations_db = real_full
    run["handler"] = handler
    return run


def test_854_viewer_requests_resolve_nothing_and_release_their_connection(
    v850_store,
):
    """Steady-state viewer requests perform no threads-table resolution.

    Failure detected: incorrect viewer output, and request connections kept
    alive in the resolver cache after teardown. Each request opens its own
    read-only connection, serves the same rows and page as an independent
    control query, issues no statement from the resolver (the explicit R2
    contract), and leaves no cache entry once it ends, including when the
    kernel raises and when the request is cancelled before the kernel runs.
    """
    import _lib_codex_conversation_query as q
    import _lib_codex_metadata as metadata

    entries = metadata._threads_table_resolution_cache
    _ns, cache, conversations = v850_store
    project_key = _v850_project_key(conversations, "v850-b")
    _v850_corrupt(cache, "v850-a", "cwd")

    def kernel(conn):
        return q.list_codex_conversations(
            conn, effective_speed="standard", project_key=project_key, limit=2)

    control = kernel(conversations)
    assert control["rows"], control

    for _request in range(2):
        run = _854_request(kernel, _854_new_run())
        ok, body = run["result"]
        assert ok is True, (run["handler"].errors, run["handler"].responses)
        assert body["rows"] == control["rows"]
        assert body["page"] == control["page"]
        assert (run["readonly"], run["full"]) == (1, 0)
        assert run["resolution"] == [], run["resolution"]
        assert all(conn not in entries for conn in run["created"])

    seen = []

    def failing(conn):
        seen.append(conn in entries)
        raise RuntimeError("854 injected kernel failure")

    run = _854_request(failing, _854_new_run())
    assert run["result"] == (False, None)
    assert run["handler"].responses[-1][0] == 500, run["handler"].responses
    assert seen == [True], "the failing request's connection was never seeded"
    assert all(conn not in entries for conn in run["created"])

    run = _854_new_run()
    recorded = []

    def cancelled():
        if not recorded:
            recorded.append(run["created"][-1] in entries)
        return True

    _854_request(kernel, run, cancelled=cancelled)
    assert run["result"] == (None, None)
    assert recorded == [True], "the cancelled request's connection was never seeded"
    assert all(conn not in entries for conn in run["created"])


def test_854_a_failed_bridge_probe_releases_the_seeded_connection(
    v850_store, monkeypatch,
):
    """A request that fails before teardown leaves no seeded connection behind.

    Failure detected: an open conversations connection pinned in the resolver
    cache after its request failed. The read-only opener seeds the connection
    it returns, and the reader probes the legacy bridge before the request's
    teardown owns that connection; the probe swallows ``sqlite3.Error``, so
    only a non-SQLite exception escapes there, and without its own cleanup the
    cache's strong reference would keep the connection open instead of
    leaving it to garbage collection.
    """
    import _lib_codex_metadata as metadata

    entries = metadata._threads_table_resolution_cache
    seen = []

    def failing_probe(conn):
        seen.append((conn, conn in entries))
        raise RuntimeError("854 injected bridge probe failure")

    def kernel(_conn):
        pytest.fail("the kernel must not run after a failed open")

    monkeypatch.setattr(
        sys.modules["_cctally_cache"], "conversation_legacy_bridge_pending",
        failing_probe)
    run = _854_new_run()
    try:
        with pytest.raises(RuntimeError, match="854 injected"):
            _854_request(kernel, run)
        # #787 / #861: the session probes the bridge inside its readiness
        # read, before it seeds anything, so the probed connection is closed
        # unseeded.
        assert len(seen) == 1, seen
        probed = seen[0][0]
        assert probed not in entries
        with pytest.raises(sqlite3.ProgrammingError):
            probed.execute("SELECT 1")
    finally:
        for conn in run["created"]:
            metadata.invalidate_codex_threads_table_resolution(conn)
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


# --------------------------------------------------------------------------
# #1076 — the off-hook Codex read policy
# --------------------------------------------------------------------------


def test_off_hook_policy_defers_every_codex_read(tmp_path, monkeypatch):
    """#1076 D1. Detects an off-hook Codex spend read that syncs or opens the
    cache inside a reconcile's ingest (the unbudgeted sync and the nested
    reconcile that waited 10 s on its own ingest lock), for vendor-wide,
    account-scoped and ``skip_sync`` reads alike."""
    _runtime(tmp_path, monkeypatch)
    cache = sys.modules["_cctally_cache"]
    calls: list = []
    monkeypatch.setattr(cache, "sync_codex_cache",
                        lambda *a, **k: calls.append("sync"))
    monkeypatch.setattr(cache, "open_cache_db",
                        lambda *a, **k: calls.append("open"))
    with cache.off_hook_codex_read_policy() as policy:
        assert cache._active_codex_read_policy() is policy
        for kwargs in ({}, {"account_key": "acct"}, {"skip_sync": True}):
            with pytest.raises(cache.CodexReadDeferred) as deferred:
                cache.get_codex_entries(START, END, **kwargs)
            assert deferred.value.reason == "off_hook_unpinned", kwargs
        policy.record_deferral("codex_budget:*", "off_hook_unpinned")
        assert policy.first_deferral_reason() == "off_hook_unpinned"
        assert (policy.deferred, policy.deferred_reasons) == (
            ["codex_budget:*"], ["off_hook_unpinned"])
        for attr in ("pin", "invalidate", "snapshot", "eligible",
                     "account_eligible"):
            assert not hasattr(policy, attr), attr
    assert calls == [], "an off-hook read synced or opened the cache"
    assert cache._active_codex_read_policy() is None


def test_off_hook_policy_restores_on_exception_and_refuses_to_nest(
    tmp_path, monkeypatch,
):
    """#1076 D1. Detects a leaked off-hook policy (every later read in the
    process would defer), a second policy stacked over an active one (an
    off-hook policy inside the hook would hide its pinned snapshot), and the
    hook-only accessor returning the off-hook policy (the sync's pin site
    would pin it and make it eligible)."""
    _runtime(tmp_path, monkeypatch)
    cache = sys.modules["_cctally_cache"]
    with pytest.raises(ZeroDivisionError):
        with cache.off_hook_codex_read_policy():
            1 / 0
    assert cache._active_codex_read_policy() is None
    with cache.off_hook_codex_read_policy() as off:
        assert cache._active_hook_codex_read_policy() is None
        with pytest.raises(RuntimeError):
            with cache.off_hook_codex_read_policy():
                pass  # pragma: no cover - the entry raises
        assert cache._active_codex_read_policy() is off
    with cache.hook_codex_read_policy() as hook:
        assert cache._active_hook_codex_read_policy() is hook
        assert cache._active_codex_read_policy() is hook
        with pytest.raises(RuntimeError):
            with cache.off_hook_codex_read_policy():
                pass  # pragma: no cover - the entry raises
        assert cache._active_codex_read_policy() is hook
    assert cache._active_codex_read_policy() is None


def _codex_flocks_free(core) -> bool:
    import fcntl
    import os
    for lock in (core.CACHE_LOCK_PATH, core.CACHE_LOCK_CODEX_PATH):
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            return False
        finally:
            os.close(fd)
    return True


def test_off_hook_policy_refuses_a_sync_before_any_work(tmp_path, monkeypatch):
    """#1076 D3. Detects a Codex sync running under the off-hook policy: the
    unbudgeted work this issue removes from a reconcile's ingest, and a sync
    whose pin site could treat the policy as pinnable. The refusal comes
    before the quota-index rebuild and before either writer flock."""
    ns = _runtime(tmp_path, monkeypatch)
    cache = sys.modules["_cctally_cache"]
    import _cctally_core as core
    import _lib_cache_writer_lock as writer_lock
    calls: list = []
    monkeypatch.setattr(cache, "_ensure_quota_index_before_producer_locks",
                        lambda: calls.append("quota_index"))
    real_acquire = writer_lock.acquire_cache_writer_flocks
    monkeypatch.setattr(
        writer_lock, "acquire_cache_writer_flocks",
        lambda *a, **k: calls.append("flocks") or real_acquire(*a, **k))
    conn = ns["open_cache_db"]()
    try:
        with cache.off_hook_codex_read_policy():
            with pytest.raises(RuntimeError, match="off-hook"):
                cache.sync_codex_cache(conn)
            with pytest.raises(RuntimeError, match="off-hook"):
                cache._sync_codex_cache_locked(conn)
        assert calls == [], calls
        assert _codex_flocks_free(core)
        cache.sync_codex_cache(conn, maintenance="caller")
        assert "quota_index" in calls and "flocks" in calls and (
            calls.index("quota_index") < calls.index("flocks")), (
            "positive control: the same sync without the policy does the work",
            calls)
    finally:
        conn.close()


def test_a_sync_inside_a_held_ingest_skips_the_auto_projection(
    tmp_path, monkeypatch,
):
    """#1076 D4 (sync side). Detects the nested reconcile and the stats-side
    signature probe a sync runs from inside a held ingest cycle: the reconcile
    can only wait 10 s for the ingest lock its own context holds. Outside the
    cycle the same stale store reconciles (positive control)."""
    from helpers.quota_projection_dump import (
        ROOT_KEY, append_one_observation_to_live_window,
        build_codex_quota_store, store_as_of)
    ns = _runtime(tmp_path, monkeypatch)
    cache = sys.modules["_cctally_cache"]
    quota = sys.modules["_cctally_quota"]
    store = sys.modules["_cctally_store"]
    build_codex_quota_store(ns, closed_windows=1, observations_per_window=4)
    now = store_as_of(ns, before_reset=dt.timedelta(minutes=10))
    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys={ROOT_KEY})
    append_one_observation_to_live_window(ns)  # the certificate is stale
    calls: list = []
    monkeypatch.setattr(quota, "reconcile_codex_quota_projection",
                        lambda **k: calls.append("reconcile"))
    real_probe = quota._stats_projection_signatures_match
    monkeypatch.setattr(
        quota, "_stats_projection_signatures_match",
        lambda *a, **k: calls.append("probe") or real_probe(*a, **k))
    conn = ns["open_cache_db"]()
    try:
        with store.stats_write_scope("ingest", ingest_lock=True):
            assert store.holds_ingest_lock()
            cache.sync_codex_cache(conn)
        assert calls == [], "a sync inside a held ingest reconciled or probed"
        cache.sync_codex_cache(conn)
        assert calls == ["reconcile"], (
            "positive control: the stale store reconciles outside the cycle")
    finally:
        conn.close()
