"""#968 T6: the conversations prune and the budgeted reset writer (I8)."""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import threading

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from conftest import (  # noqa: E402
    load_script, redirect_paths)
from _codex_zstd_fixtures import (  # noqa: E402
    conversation_records, snapshot_tables, write_rollout)

import _lib_ingest_frontier as frontier  # noqa: E402

_base_conversation_records = conversation_records


def conversation_records(*args, **kwargs):
    records = _base_conversation_records(*args, **kwargs)
    for record in records:
        payload = record.get("payload", {})
        if payload.get("type") == "token_count":
            payload["info"]["rate_limits"] = {"limit_id": "codex", "limit_name": "codex",
                "primary": {"used_percent": 25, "window_minutes": 10080,
                            "resets_at": 1785150000}}
    records.append({"timestamp": "2026-07-20T10:59:00.000Z", "type": "response_item",
        "payload": {"type": "custom_tool_call", "call_id": "patch-1",
                    "name": "apply_patch", "input": "*** Begin Patch\n*** Add File: twins.txt\n+hello\n*** End Patch"}})
    records.append({"timestamp": "2026-07-20T10:59:01.000Z", "type": "event_msg",
        "payload": {"type": "patch_apply_end", "call_id": "patch-1",
                    "status": "completed", "changes": [{"path": "twins.txt"}],
                    "success": True}})
    return records


CONV_TABLES = ("codex_conversation_messages", "codex_conversation_events",
               "codex_conversation_file_touches",
               "codex_conversation_source_files")
#: Spec T6 "derived rows byte-identical" (review S-P3 item 2): accounting,
#: cursors, identity and account registries, quota facts, the cache-side
#: thread rows, both change ledgers and the partition-latest summary.
DURABLE_CACHE_TABLES = ("codex_session_entries", "codex_session_files",
                        "codex_file_incarnations", "codex_file_accounts",
                        "quota_window_snapshots", "codex_conversation_threads",
                        "codex_accounting_change_log",
                        "quota_window_change_log")
#: WITHOUT ROWID, so `snapshot_tables` (ORDER BY rowid) cannot read it.
PARTITION_LATEST = "codex_quota_partition_latest"
#: The physical mutation sequence and the journal coverage certificate.
DURABLE_META_KEYS = ("codex_physical_mutation_seq",
                     "codex_journal_coverage_certificate",
                     "codex_journal_coverage_progress")


def _sid(i: int) -> str:
    return f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff03{i:02d}"


def _name(i: int) -> str:
    return f"rollout-2026-07-20T10-{i:02d}-00-{_sid(i)}.jsonl"


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
def env(tmp_path, monkeypatch):
    ns = load_script()
    # The module runs conversations passes over July-2026 fixtures: the
    # forced retention prune must not expire them (Global Constraints).
    redirect_paths(
        ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    payload = {"email": "twins@example.test", "https://api.openai.com/auth": {
        "chatgpt_account_id": "twins-account", "chatgpt_plan_type": "pro"}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    (provider / "auth.json").write_text(json.dumps({"tokens": {
        "id_token": f"header.{encoded}.sig", "access_token": "a",
        "refresh_token": "r"}}))
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: True)
    return ns, day


def _cache():
    return sys.modules["_cctally_cache"]


def _call_supported(fn, conn, **kwargs):
    """Call ``fn`` with the keywords its signature accepts.

    On 92e06df85 the executor keywords do not exist, so the call degrades to
    the ordinary targeted sync and the test fails on its BEHAVIOR (the whole
    call is declined), never on a TypeError.
    """
    params = inspect.signature(fn).parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return fn(conn, **kwargs)
    return fn(conn, **{k: v for k, v in kwargs.items() if k in params})


def _sync_both(ns):
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, quota_reconcile="defer")
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
    finally:
        conv.close()


def _rows_for(conv, path: str) -> dict:
    out = {}
    for table in CONV_TABLES:
        column = "path" if table == "codex_conversation_source_files" else "source_path"
        out[table] = conv.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (path,)).fetchone()[0]
    return out


def _identity_rows(conv, path):
    return {table: [tuple(r) for r in conv.execute(
        f"SELECT * FROM {table} WHERE "
        + ("path" if table == "codex_conversation_source_files" else "source_path")
        + "=? ORDER BY rowid", (path,))] for table in CONV_TABLES}


def _assert_derived_fixed_point(conv):
    """Rollups and the find projection are a pure function of the survivors:
    recomputing them over every key changes nothing (no row, no generation)."""
    import _lib_codex_conversation_query as query
    keys = sorted({r[0] for r in conv.execute(
        "SELECT conversation_key FROM codex_conversation_rollups "
        "UNION SELECT conversation_key FROM codex_conversation_messages") if r[0]})
    tables = ("codex_conversation_rollups", "codex_find_projection")
    gen_sql = ("SELECT value FROM cache_meta "
               "WHERE key='codex_find_projection_generation'")
    before, gen_before = snapshot_tables(conv, tables), conv.execute(gen_sql).fetchone()
    conv.execute("SAVEPOINT fixed_point")
    try:
        _cache()._recompute_codex_rollups(
            conv, keys, advance_render_revision=False, inputs_changed=False)
        query.materialize_codex_find_projection(conv, keys, cascaded=False)
        after, gen_after = snapshot_tables(conv, tables), conv.execute(gen_sql).fetchone()
    finally:
        conv.execute("ROLLBACK TO SAVEPOINT fixed_point")
        conv.execute("RELEASE SAVEPOINT fixed_point")
    assert after == before and gen_after == gen_before
    assert conv.execute(
        "SELECT COUNT(*) FROM codex_conversation_rollups r WHERE NOT EXISTS ("
        "SELECT 1 FROM codex_conversation_messages m "
        "WHERE m.conversation_key = r.conversation_key)").fetchone()[0] == 0


def test_the_conversations_prune_drains_per_identity(env, monkeypatch):
    ns, day = env
    cache_mod = _cache()
    paths = []
    for i in range(4):
        path = day / _name(i)
        write_rollout(path, conversation_records(3, session_id=_sid(i),
                                                 label=chr(97 + i)))
        paths.append(path)
    _sync_both(ns)
    keys = [str(p.resolve()) for p in paths]
    for path in paths[:3]:
        path.unlink()
    conv = ns["open_conversations_db"]()
    try:
        before = {k: _rows_for(conv, k) for k in keys}
        complete_before = {k: _identity_rows(conv, k) for k in keys}
        assert all(v > 0 for k in keys for v in before[k].values())
        stats = ns["sync_codex_conversations"](conv)
        # the walk deleted nothing; it recorded debt and requested the worker
        assert {k: _rows_for(conv, k) for k in keys} == before
        assert stats.prune_debt_pending == 3
        assert cache_mod.CODEX_PRUNE_DRAIN_COMMAND in stats.drain_requested
        # spec 5.7 (review S-P3 item 5): no completion certificate while
        # prune debt is pending, in the conversations frontier too
        assert not frontier.conversation_sync_certifiable("full", stats)
    finally:
        conv.close()

    calls: list[str] = []

    def killed_on_second(path, _root_key):
        calls.append(path)
        if len(calls) == 2:
            raise KeyboardInterrupt("killed inside the second identity")

    monkeypatch.setattr(cache_mod, "_CODEX_CONVERSATION_PRUNE_PRECOMMIT_HOOK",
                        killed_on_second)
    with pytest.raises(KeyboardInterrupt):
        cache_mod.run_codex_conversation_prune_drain(wait_for_requests=False)
    first, second = calls
    conv = ns["open_conversations_db"]()
    try:
        assert all(v == 0 for v in _rows_for(conv, first).values())
        assert _rows_for(conv, second) == before[second]
        assert _identity_rows(conv, second) == complete_before[second]
        assert _identity_rows(conv, keys[3]) == complete_before[keys[3]]
        debt = {r[0] for r in conv.execute(
            "SELECT source_path FROM codex_conversation_prune_debt")}
        assert first not in debt and second in debt
        _assert_derived_fixed_point(conv)
    finally:
        conv.close()

    monkeypatch.setattr(cache_mod, "_CODEX_CONVERSATION_PRUNE_PRECOMMIT_HOOK", None)
    while True:
        result = cache_mod.run_codex_conversation_prune_drain(
            wait_for_requests=False, max_identities=1)
        conv = ns["open_conversations_db"]()
        try:
            _assert_derived_fixed_point(conv)   # after every committed identity
        finally:
            conv.close()
        if result.remaining == 0:
            break
    conv = ns["open_conversations_db"]()
    try:
        for key in keys[:3]:
            assert all(v == 0 for v in _rows_for(conv, key).values())
        assert _rows_for(conv, keys[3]) == before[keys[3]]
        # debt drained: the next full pass certifies again
        assert frontier.conversation_sync_certifiable(
            "full", ns["sync_codex_conversations"](conv))
    finally:
        conv.close()


def _durable_state(ns):
    journal = sys.modules["_cctally_core"].JOURNAL_DIR
    files = {}
    if journal.exists():
        for item in sorted(journal.rglob("*")):
            if item.is_file():
                files[str(item.relative_to(journal))] = hashlib.sha256(
                    item.read_bytes()).hexdigest()
    conn = ns["open_cache_db"]()
    try:
        tables = snapshot_tables(conn, DURABLE_CACHE_TABLES)
        tables[PARTITION_LATEST] = sorted(
            tuple(r) for r in conn.execute(f"SELECT * FROM {PARTITION_LATEST}"))
        meta = {key: conn.execute("SELECT value FROM cache_meta WHERE key=?",
                                  (key,)).fetchone()
                for key in DURABLE_META_KEYS}
    finally:
        conn.close()
    return files, tables, meta


@pytest.mark.parametrize("change",
                         ["truncation", "replacement", "requalification"])
def test_a_budgeted_sync_defers_a_genuine_reset_with_no_durable_effect(
        env, monkeypatch, change):
    ns, day = env
    cache_mod = _cache()
    path = day / _name(5)
    write_rollout(path, conversation_records(4, session_id=_sid(5), label="r"))
    _sync_both(ns)
    key = str(path.resolve())

    def discovered_item():
        found = [item for item in cache_mod._discover_codex_files_with_roots()
                 if os.path.realpath(str(item.source_path)) == key]
        assert len(found) == 1
        return found[0]

    identity = cache_mod.codex_file_identity(discovered_item())
    conn = ns["open_cache_db"]()
    try:
        # `codex_file_incarnation` defaults to 1 for an identity with no
        # registry row (`bin/_cctally_cache.py:1162-1168`).
        incarnation = cache_mod.codex_file_incarnation(conn, identity)
    finally:
        conn.close()
    before_stat = path.stat()
    before_size, before_ino = before_stat.st_size, before_stat.st_ino
    # Three branches, each establishing the precondition its assertions
    # below rely on: a same-inode shrink, a new inode at the same name, and
    # the same physical file under a different first matching root.
    if change == "truncation":
        shorter = b"".join(
            json.dumps(r, separators=(",", ":")).encode("utf-8") + b"\n"
            for r in conversation_records(2, session_id=_sid(5), label="r"))
        with open(path, "r+b") as fh:          # same inode, shorter content
            fh.seek(0)
            fh.write(shorter)
            fh.truncate()
        assert path.stat().st_size < before_size        # a genuine shrink
    elif change == "replacement":
        staged = path.with_name(path.name + ".staged")
        write_rollout(staged, conversation_records(4, session_id=_sid(5), label="s"))
        os.replace(staged, path)              # new inode, possibly equal size
        assert path.stat().st_ino != before_ino         # a genuine replacement
    else:
        # Same physical file, a different first matching provider root: the
        # direct sessions root wins, exactly as in
        # tests/test_codex_fused_ingest.py's requalification test.
        monkeypatch.setenv("CODEX_HOME", str(day.parents[2]))
        assert cache_mod.codex_file_identity(discovered_item()) != identity
    conn = ns["open_cache_db"]()
    try:
        for marker in DURABLE_META_KEYS[1:]:
            conn.execute("INSERT OR REPLACE INTO cache_meta VALUES (?,?)",
                         (marker, "retained-evidence"))
        conn.commit()
    finally:
        conn.close()
    before = _durable_state(ns)
    assert all(before[2][key] is not None for key in DURABLE_META_KEYS)
    assert before[0], "journal content must be nonempty"
    for table in DURABLE_CACHE_TABLES:
        assert before[1][table], f"non-vacuous reset family: {table}"
    assert before[1][PARTITION_LATEST]
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](conn, lock_timeout=0, budget_seconds=60.0,
                                       quota_reconcile="defer")
    finally:
        conn.close()
    # journal high-water and content, incarnations, account ranges, quota
    # facts and ledgers, partition-latest, thread rows, cursors, derived
    # rows, the mutation sequence and the coverage certificate: all
    # byte-identical. (92e06df85: the budgeted walk resets in place.)
    assert _durable_state(ns) == before
    assert stats.files_deferred_reset == 1
    assert cache_mod.CODEX_REPLAY_DRAIN_COMMAND in stats.drain_requested

    # Review S-P1: the worker's targeted sync is not declined by the
    # whole-call truncation/requalification preflight; it performs the reset.
    result = cache_mod.run_codex_reset_drain(wait_for_requests=False)
    assert result.reset == 1
    now = discovered_item()
    conn = ns["open_cache_db"]()
    try:
        size, root_key = conn.execute(
            "SELECT size_bytes, source_root_key FROM codex_session_files "
            "WHERE path=?", (key,)).fetchone()
        assert size == path.stat().st_size
        assert root_key == now.source_root_key
        if change == "requalification":
            # a different root is a different durable identity (#416 3.2)
            assert cache_mod.codex_file_identity(now) != identity
        else:
            assert (cache_mod.codex_file_incarnation(conn, identity)
                    == incarnation + 1)
    finally:
        conn.close()


@pytest.mark.parametrize("change", ["truncation", "requalification"])
def test_an_executor_conversation_reset_passes_the_whole_call_preflight(
        env, monkeypatch, change):
    """Review S-P1, the conversations twin (HEAD 14239-14253): a targeted
    executor sync that was handed a reset reaches the per-file reset instead
    of declining the whole call."""
    ns, day = env
    path = day / _name(9)
    write_rollout(path, conversation_records(4, session_id=_sid(9), label="q"))
    _sync_both(ns)
    key = str(path.resolve())
    if change == "truncation":
        write_rollout(path, conversation_records(2, session_id=_sid(9), label="q"))
    else:
        monkeypatch.setenv("CODEX_HOME", str(day.parents[2]))
    conv = ns["open_conversations_db"]()
    try:
        stats = _call_supported(
            ns["sync_codex_conversations"], conv, maintenance="caller",
            only_paths={key}, executor_paths=frozenset({key}),
            executor_resets=frozenset({key}))
        assert stats.deferred_reason is None
        assert stats.files_reset_truncated == 1
        assert conv.execute(
            "SELECT size_bytes FROM codex_conversation_source_files "
            "WHERE path=?", (key,)).fetchone()[0] == path.stat().st_size
    finally:
        conv.close()


def test_an_unbudgeted_replacement_still_resets_atomically(env):
    ns, day = env
    path = day / _name(6)
    write_rollout(path, conversation_records(4, session_id=_sid(6), label="u"))
    _sync_both(ns)
    staged = path.with_name(path.name + ".staged")
    write_rollout(staged, conversation_records(2, session_id=_sid(6), label="v"))
    os.replace(staged, path)
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](conn, quota_reconcile="defer")
        assert conn.execute("SELECT size_bytes FROM codex_session_files "
                            "WHERE path=?", (str(path.resolve()),)).fetchone()[0] \
            == path.stat().st_size
        assert stats.files_reset_truncated == 1
        # The field is declared by this task; this pin must also pass on
        # 92e06df85, where it does not exist yet.
        assert getattr(stats, "files_deferred_reset", 0) == 0
    finally:
        conn.close()


def test_the_conversations_prune_cannot_commit_under_a_paused_writer(env, monkeypatch):
    """Spec 5.12 and I8 (review S-P2c, S-P2d). The ordinary writer is paused
    after it has read the victim's new records and normalized them into
    memory, and before its batch's first SQL write: the window in which a
    prune that committed would be undone by the buffered tuples. The prune
    executor cannot be admitted while the writer holds the Codex
    conversations provider lock; after the writer finishes, the drain deletes
    the identity and an ordinary pass resurrects nothing."""
    ns, day = env
    cache_mod = _cache()
    victim, keeper = day / _name(7), day / _name(8)
    write_rollout(victim, conversation_records(2, session_id=_sid(7), label="y"))
    write_rollout(keeper, conversation_records(2, session_id=_sid(8), label="k"))
    _sync_both(ns)
    victim_key, keeper_key = str(victim.resolve()), str(keeper.resolve())
    victim.unlink()
    conv = ns["open_conversations_db"]()
    try:
        held = _rows_for(conv, victim_key)
        assert all(v > 0 for v in held.values())
        ns["sync_codex_conversations"](conv)
        # 92e06df85 RED: today's walk deletes the orphan inline, under its own
        # provider lock, so no separate prune exists for a writer to race.
        assert _rows_for(conv, victim_key) == held, "the walk deleted the orphan"
        keeper_rows = _rows_for(conv, keeper_key)
    finally:
        conv.close()
    # The victim comes back longer while its debt row is still recorded (debt
    # is not permission): the next ordinary writer buffers new tuples for it.
    write_rollout(victim, conversation_records(4, session_id=_sid(7), label="y"))
    conv_lib = sys.modules["_lib_codex_conversation"]
    original = conv_lib.normalize_codex_events
    prepared, release = threading.Event(), threading.Event()

    def paused(events, *args, **kwargs):
        result = original(events, *args, **kwargs)
        if not prepared.is_set() and any(
                os.path.realpath(str(e.source_path)) == victim_key
                for e in events):
            # Read and normalized into memory; no SQL write yet. The writer
            # holds the provider lock and the buffered tuples.
            prepared.set()
            assert release.wait(30)
        return result

    monkeypatch.setattr(conv_lib, "normalize_codex_events", paused)
    errors: list[BaseException] = []

    def writer():
        conv_w = ns["open_conversations_db"]()
        try:
            ns["sync_codex_conversations"](conv_w)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            conv_w.close()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert prepared.wait(30)
    victim.unlink()                   # gone again while the tuples are buffered
    conv = ns["open_conversations_db"]()
    try:
        held = _rows_for(conv, victim_key)
    finally:
        conv.close()
    # The writer holds the Codex conversations provider lock: no admission.
    blocked = cache_mod.run_codex_conversation_prune_drain(wait_for_requests=False)
    assert blocked.identities_deleted == 0 and blocked.yielded >= 1
    conv = ns["open_conversations_db"]()
    try:
        assert _rows_for(conv, victim_key) == held
    finally:
        conv.close()
    release.set()
    thread.join(30)
    assert not thread.is_alive() and errors == []
    drained = cache_mod.run_codex_conversation_prune_drain(wait_for_requests=False)
    assert drained.identities_deleted == 1 and drained.remaining == 0
    _sync_both(ns)   # an ordinary pass afterwards resurrects nothing
    conv = ns["open_conversations_db"]()
    try:
        assert all(v == 0 for v in _rows_for(conv, victim_key).values())
        assert _rows_for(conv, keeper_key) == keeper_rows
    finally:
        conv.close()


def test_debt_only_conversation_passes_are_held_until_the_debt_clears(
        env, monkeypatch):
    """Review N3: the conversations twin of Task 4's hold. A pass clean except
    for pending conversations prune debt leaves a token, and the frontier
    walks such a store at most once per FRONTIER_DEBT_HOLD_SECONDS, at once
    when a Codex ticket reaches the shared activity marker (no hook writes
    conversations.db) but never on a Claude ticket (review r3 F2), and at
    once when the debt reads empty."""
    ns, day = env
    cache_mod = _cache()
    paths = []
    for i in range(3):
        path = day / _name(i)
        write_rollout(path, conversation_records(3, session_id=_sid(i),
                                                 label=chr(97 + i)))
        paths.append(path)
    _sync_both(ns)
    victim = str(paths[0].resolve())
    paths[0].unlink()
    now = [2_000_000.0]
    monkeypatch.setattr(frontier, "_now", lambda: now[0])
    roots = tuple(r.walk_root for r in cache_mod._codex_provider_roots())
    app_dir = ns["APP_DIR"] / "frontier-n3"
    app_dir.mkdir(parents=True, exist_ok=True)
    state = frontier.ConversationSyncFrontier(app_dir)
    walks = []

    def tick(advance=10.0):
        now[0] += advance
        conv = ns["open_conversations_db"]()
        try:
            plan = state.plan_provider("codex", conv, roots=roots)
            if plan.mode == "full":
                stats = _call_supported(ns["sync_codex_conversations"], conv,
                                        maintenance="caller")
                walks.append(stats)
                if frontier.conversation_sync_certifiable("full", stats):
                    assert state.seed_provider("codex", conv, roots=roots), (
                        state.last_seed_failure)
            return plan
        finally:
            conv.close()

    plans = [tick() for _ in range(4)]
    conv = ns["open_conversations_db"]()
    try:
        assert _rows_for(conv, victim)["codex_conversation_source_files"] == 1, (
            "the walk deleted the orphan")
    finally:
        conv.close()
    assert [p.mode for p in plans] == ["full"] + ["caught_up"] * 3, plans
    assert {p.reason for p in plans[1:]} == {"debt_hold"}
    assert len(walks) == 1 and walks[0].prune_debt_pending == 1
    # Review r3 F2: the Claude hook appends to the same marker, and Claude
    # activity is nothing a Codex conversation walk publishes.
    assert frontier.record_activity(app_dir, "claude", "/tmp/claude-968.jsonl")
    assert tick().reason == "debt_hold" and len(walks) == 1
    # Codex hook activity is transcript content only a walk can publish.
    assert frontier.record_activity(app_dir, "codex", str(paths[1].resolve()))
    assert tick().mode == "full" and len(walks) == 2
    assert tick().reason == "debt_hold"
    # The hold lasts no longer than a certificate may.
    assert tick(frontier.FRONTIER_DEBT_HOLD_SECONDS).mode == "full"
    assert len(walks) == 3
    assert cache_mod.run_codex_conversation_prune_drain(
        wait_for_requests=False).remaining == 0
    # An empty debt table ends the hold at once, and that pass certifies.
    assert tick().mode == "full" and len(walks) == 4
    assert frontier.conversation_sync_certifiable("full", walks[-1])
    assert tick().mode != "full" and len(walks) == 4


def test_a_stale_maintenance_marker_never_hangs_the_conversations_prune(env, monkeypatch):
    """Review S-P2b. The executor opens conversations.db BEFORE admission
    takes the Codex conversations provider lock. A stale repair marker makes
    the opener take the maintenance lock exclusively and then wait for both
    provider locks (`_conversations_open_guarded`,
    `bin/_cctally_cache.py:11412-11426`); an executor that opened after
    admission would wait forever on the lock it already holds."""
    ns, day = env
    cache_mod = _cache()
    victim, keeper = day / _name(10), day / _name(11)
    write_rollout(victim, conversation_records(2, session_id=_sid(10), label="m"))
    write_rollout(keeper, conversation_records(2, session_id=_sid(11), label="n"))
    _sync_both(ns)
    victim_key = str(victim.resolve())
    victim.unlink()
    conv = ns["open_conversations_db"]()
    try:
        held = _rows_for(conv, victim_key)
        ns["sync_codex_conversations"](conv)
        # 92e06df85 RED: today's walk deletes the orphan inline.
        assert _rows_for(conv, victim_key) == held, "the walk deleted the orphan"
    finally:
        conv.close()
    import _lib_codex_drain_admission as adm
    marker = sys.modules["_cctally_db"]._repair_marker_path(
        pathlib.Path(sys.modules["_cctally_core"].CONVERSATIONS_DB_PATH))
    real_admit = adm.admit
    planted: list[bool] = []

    def admit_after_a_crashed_repair(*args, **kwargs):
        # A crashed repair's marker with a malformed owner record, which
        # `_repair_marker_is_live` reports as not live, so the opener treats
        # it as stale. It appears after the debt row is read and just before
        # admission takes the provider lock.
        if not planted:
            marker.write_text("{}")
            planted.append(True)
        return real_admit(*args, **kwargs)

    monkeypatch.setattr(adm, "admit", admit_after_a_crashed_repair)
    outcome: list[object] = []
    worker = threading.Thread(
        target=lambda: outcome.append(cache_mod.run_codex_conversation_prune_drain(
            wait_for_requests=False)), daemon=True)
    worker.start()
    worker.join(60)
    assert not worker.is_alive(), "the prune executor hung on a stale marker"
    assert planted and outcome and outcome[0].identities_deleted == 1
    conv = ns["open_conversations_db"]()     # the stale marker was cleared
    try:
        assert all(v == 0 for v in _rows_for(conv, victim_key).values())
    finally:
        conv.close()
    assert not marker.exists()


def test_a_stale_conversation_debt_never_deletes_a_requalified_identity(env, monkeypatch):
    ns, day = env
    victim, keeper = day / _name(12), day / _name(13)
    records = conversation_records(2, session_id=_sid(12), label="stale")
    write_rollout(victim, records)
    write_rollout(keeper, conversation_records(2, session_id=_sid(13)))
    _sync_both(ns)
    key = str(victim.resolve())
    victim.unlink()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv, maintenance="caller")
        assert conv.execute("SELECT COUNT(*) FROM codex_conversation_prune_debt").fetchone()[0] == 1
        write_rollout(victim, records)
        monkeypatch.setenv("CODEX_HOME", str(day.parents[2]))
        stats = ns["sync_codex_conversations"](conv, maintenance="caller")
        assert stats.files_reset_truncated == 2
        before = snapshot_tables(conv, (*CONV_TABLES, "codex_conversation_rollups", "codex_find_projection"))
    finally:
        conv.close()
    victim.unlink()
    result = _cache().run_codex_conversation_prune_drain(wait_for_requests=False)
    conv = ns["open_conversations_db"]()
    try:
        assert snapshot_tables(conv, (*CONV_TABLES, "codex_conversation_rollups", "codex_find_projection")) == before
        assert result.identities_dropped == 1 and result.identities_deleted == 0
    finally:
        conv.close()


@pytest.mark.parametrize("change", ["replacement", "requalification"])
def test_reset_drain_does_not_infer_equivalence_from_a_partial_cursor(
        env, monkeypatch, tmp_path, change):
    """T7-COMMIT-R01: a shorter replacement is not a verified prefix.

    Compare every retained accounting/quota/identity/cursor/ledger value with
    the ordinary unbudgeted reset of an identical pre-worker database.
    """
    ns, day = env
    cache = _cache()
    original_datetime = cache.dt.datetime
    fixed = original_datetime(2026, 7, 20, 12, tzinfo=cache.dt.timezone.utc)

    class FrozenDatetime(original_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(cache.dt, "datetime", FrozenDatetime)
    path = day / _name(20)
    write_rollout(path, conversation_records(30, session_id=_sid(20), label="old"))
    real_clock = cache._walk_clock
    ticks = iter(i / 10 for i in range(1000))
    monkeypatch.setattr(cache, "_walk_clock", lambda: next(ticks))
    conn = ns["open_cache_db"]()
    try:
        partial = ns["sync_codex_cache"](
            conn, maintenance="caller", budget_seconds=2, quota_reconcile="defer")
        row = conn.execute("SELECT size_bytes,last_byte_offset,ingest_complete "
                           "FROM codex_session_files WHERE path=?", (str(path),)).fetchone()
        assert partial.budget_exhausted and row[2] == 0
        assert 0 < row[1] < row[0]
        for table in DURABLE_CACHE_TABLES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0, table
        assert conn.execute(f"SELECT COUNT(*) FROM {PARTITION_LATEST}").fetchone()[0] > 0
    finally:
        conn.close()
    monkeypatch.setattr(cache, "_walk_clock", real_clock)
    original_inode = path.stat().st_ino
    staged = path.with_name(path.name + ".replacement")
    write_rollout(staged, conversation_records(20, session_id=_sid(21), label="new"))
    assert row[1] <= staged.stat().st_size < row[0], (row, staged.stat().st_size)
    if change == "replacement":
        os.replace(staged, path)
        assert path.stat().st_ino != original_inode
    else:
        path.write_bytes(staged.read_bytes())
        monkeypatch.setenv("CODEX_HOME", str(day.parents[2]))
        assert path.stat().st_ino == original_inode
    core = sys.modules["_cctally_core"]
    control_journal = tmp_path / "control-journal"
    shutil.copytree(core.JOURNAL_DIR, control_journal)
    conn = ns["open_cache_db"]()
    control = sqlite3.connect(tmp_path / "control.sqlite")
    try:
        conn.backup(control)
    finally:
        conn.close()
    try:
        result = cache.run_codex_reset_drain(wait_for_requests=False)
        # Each execution sees the identical journal as well as the identical
        # SQLite input: the worker must not seed the control with its decisions.
        with monkeypatch.context() as isolated:
            isolated.setattr(core, "JOURNAL_DIR", control_journal)
            isolated.setattr(core, "JOURNAL_LOCK_PATH", tmp_path / "control-journal.lock")
            ordinary = ns["sync_codex_cache"](
                control, maintenance="caller", quota_reconcile="defer")
        assert ordinary.files_reset_truncated == 1
        assert control.execute(
            "SELECT COUNT(*),COUNT(DISTINCT session_id),MIN(session_id) "
            "FROM codex_session_entries").fetchone() == (20, 1, _sid(21))
        expected = snapshot_tables(control, DURABLE_CACHE_TABLES)
        expected[PARTITION_LATEST] = sorted(tuple(r) for r in control.execute(
            f"SELECT * FROM {PARTITION_LATEST}"))
        actual_conn = ns["open_cache_db"]()
        try:
            actual = snapshot_tables(actual_conn, DURABLE_CACHE_TABLES)
            actual[PARTITION_LATEST] = sorted(tuple(r) for r in actual_conn.execute(
                f"SELECT * FROM {PARTITION_LATEST}"))
            # The replacement has a new session id and twenty accounting and
            # quota facts: no old prefix, no suffix-only re-read.
            assert actual_conn.execute(
                "SELECT COUNT(*),COUNT(DISTINCT session_id),MIN(session_id) "
                "FROM codex_session_entries").fetchone() == (20, 1, _sid(21))
        finally:
            actual_conn.close()
        assert result.reset == 1
        assert actual == expected
    finally:
        control.close()


def _retained_reset_identity(ns, path):
    """Exact, nonempty protected families; omit no cursor or attribution field."""
    cache = _cache()
    item = next(f for f in cache._discover_codex_files_with_roots()
                if str(f.source_path) == str(path))
    identity = cache.codex_file_identity(item)
    conn = ns["open_cache_db"]()
    try:
        rows = {}
        for table in DURABLE_CACHE_TABLES:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if table == "quota_window_change_log":
                rows[table] = [tuple(r) for r in conn.execute(
                    f"SELECT * FROM {table} WHERE old_source_root_key=? "
                    "OR new_source_root_key=? ORDER BY rowid",
                    (item.source_root_key, item.source_root_key))]
                assert rows[table], table
                continue
            column, value = (("path", str(path)) if "path" in cols else
                             ("source_path", str(path)) if "source_path" in cols else
                             ("file_identity", identity))
            rows[table] = [tuple(r) for r in conn.execute(
                f"SELECT * FROM {table} WHERE {column}=? ORDER BY rowid", (value,))]
            assert rows[table], table
        rows[PARTITION_LATEST] = sorted(tuple(r) for r in conn.execute(
            f"SELECT * FROM {PARTITION_LATEST}"))
        assert rows[PARTITION_LATEST]
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        rows.update(_identity_rows(conv, str(path)))
        for table in CONV_TABLES:
            assert rows[table], table
    finally:
        conv.close()
    return rows


@pytest.mark.parametrize("becomes_unknown", ["foreground", "admission"])
def test_replay_worker_cannot_reset_a_protected_sibling_root(
        env, monkeypatch, tmp_path, becomes_unknown):
    """T7-COMMIT-R02: unrelated verification cannot authorize a reset.

    The admission variant changes recognition after reset-candidate discovery,
    immediately before admission, rejecting a cached/foreground-only verdict.
    """
    from _codex_zstd_fixtures import compress_in_place
    import _lib_codex_drain_admission as adm

    ns, day = env
    cache = _cache()
    root_a = day.parents[3]
    root_b = tmp_path / "codex-b"
    day_b = root_b / "sessions" / "2026" / "07" / "20"
    monkeypatch.setenv("CODEX_HOME", ",".join((str(root_a), str(root_b))))
    path_a, path_b = day / _name(22), day_b / _name(23)
    write_rollout(path_a, conversation_records(5, session_id=_sid(22), label="keep"))
    write_rollout(path_b, conversation_records(3, session_id=_sid(23), label="verify"))
    _sync_both(ns)
    before = _retained_reset_identity(ns, path_a)
    auth_reads = []
    real_account = cache._resolve_codex_account_for_root

    def account(root):
        auth_reads.append(str(root))
        return real_account(root)

    monkeypatch.setattr(cache, "_resolve_codex_account_for_root", account)
    compress_in_place(path_b)  # Independent verification starts the real worker.
    unknown = b'{"type":"future-unrecognized-envelope","payload":{}}\n'
    if becomes_unknown == "foreground":
        path_a.write_bytes(unknown)
    else:
        write_rollout(path_a, conversation_records(1, session_id=_sid(22)))
    requested = []
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: requested.append(command) or True)
    conn = ns["open_cache_db"]()
    try:
        foreground = ns["sync_codex_cache"](
            conn, lock_timeout=0, budget_seconds=60, quota_reconcile="defer")
        assert foreground.container_pending == 1
        assert foreground.prune_refused == (becomes_unknown == "foreground")
    finally:
        conn.close()
    assert cache.CODEX_REPLAY_DRAIN_COMMAND in requested
    assert _retained_reset_identity(ns, path_a) == before
    # Capture the reset executor phase, then change the root's sole recognizable
    # file between candidate discovery and its actual store-lock admission.
    real_reset = cache.run_codex_reset_drain
    real_admit = adm.admit
    in_reset = []
    races = []

    def reset(**kwargs):
        in_reset.append(True)
        try:
            return real_reset(**kwargs)
        finally:
            in_reset.pop()

    def admit(*args, **kwargs):
        if in_reset and becomes_unknown == "admission" and not races:
            path_a.write_bytes(unknown)
            races.append(True)
        return real_admit(*args, **kwargs)

    monkeypatch.setattr(cache, "run_codex_reset_drain", reset)
    monkeypatch.setattr(adm, "admit", admit)
    # Exact hidden command entry point, in-process, with real verification,
    # gate, reset scan, cache locks and logging; no uncontrolled subprocess.
    assert cache.cmd_codex_replay_drain_internal(None) == 0
    assert _retained_reset_identity(ns, path_a) == before
    assert bool(races) == (becomes_unknown == "admission")
    assert str(root_a) not in auth_reads, auth_reads
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("SELECT container_kind,container_state FROM "
                            "codex_session_files WHERE path=?", (str(path_b),)).fetchone() \
            == ("zstd", "accepted")
    finally:
        conn.close()


def test_stale_reset_candidate_cannot_authorize_a_container_transition(env, monkeypatch):
    """A scan's reset candidate may become verification work before admission."""
    from _codex_zstd_fixtures import zstd_bytes

    ns, day = env
    cache = _cache()
    path = day / _name(24)
    write_rollout(path, conversation_records(4, session_id=_sid(24)))
    _sync_both(ns)
    complete = path.read_bytes()
    before = _durable_state(ns)
    write_rollout(path, conversation_records(2, session_id=_sid(24)))
    real_candidates = cache._codex_reset_candidates
    scanned = []

    def scan_then_compress():
        candidates = real_candidates()
        assert candidates == [str(path)]
        # The old candidate now names an equal-content transition; only the
        # verification executor may prove equivalence and adopt this container.
        path.with_name(path.name + ".zst").write_bytes(zstd_bytes(complete))
        path.unlink()
        scanned.append(True)
        return candidates

    monkeypatch.setattr(cache, "_codex_reset_candidates", scan_then_compress)
    result = cache.run_codex_reset_drain(wait_for_requests=False)
    assert scanned
    assert _durable_state(ns) == before
    assert result.reset == 0 and result.unchanged == 1
