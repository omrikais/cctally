"""#968 T1 + T7 transitions: verified compression is invisible; changes are not."""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from conftest import (  # noqa: E402
    load_script, redirect_paths)
from _codex_zstd_fixtures import (  # noqa: E402
    conversation_records, snapshot_tables, token_records, write_rollout,
    zstd_bytes)
from test_codex_bounded_ingest import _auth_json  # noqa: E402
from test_codex_prune_drain import _no_detached_drains  # noqa: F401,E402

import _lib_codex_rollout_container as rc  # noqa: E402

ACCOUNTING_TABLES = (
    "codex_session_entries", "quota_window_snapshots", "codex_file_accounts",
    "codex_file_incarnations", "codex_accounting_change_log",
    "quota_window_change_log", "codex_conversation_threads",
)
CONVERSATION_TABLES = (
    "codex_conversation_events", "codex_conversation_messages",
    "codex_conversation_rollups",
)
CURSOR_KEEP = (
    "path", "size_bytes", "last_byte_offset", "last_ingested_at",
    "last_session_id", "last_model", "last_total_tokens", "source_root_key",
    "last_native_thread_id", "last_root_thread_id", "last_parent_thread_id",
    "last_conversation_key", "last_turn_id", "account_key", "ingest_complete",
)


def _name(i):
    return f"rollout-2026-07-20T1{i}-00-00-0199aaaa-bbbb-7ccc-8ddd-eeeeffff01{i:02d}.jsonl"


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: True)
    return ns, provider, day


def _sync_both(ns, *, quota_reconcile="auto"):
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, quota_reconcile=quota_reconcile)
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
    finally:
        conv.close()


def _state(ns):
    conn = ns["open_cache_db"]()
    try:
        tables = [t for t in ACCOUNTING_TABLES if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()]
        snap = snapshot_tables(conn, tables)
        conn.row_factory = sqlite3.Row
        cursors = [{k: dict(r)[k] for k in CURSOR_KEEP} for r in conn.execute(
            "SELECT * FROM codex_session_files ORDER BY path")]
        seq = conn.execute(
            "SELECT value FROM cache_meta WHERE key='codex_physical_mutation_seq'"
        ).fetchall()
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        tables = [t for t in CONVERSATION_TABLES if conv.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()]
        csnap = snapshot_tables(conv, tables)
        conv.row_factory = sqlite3.Row
        ccursors = [{k: dict(r)[k] for k in CURSOR_KEEP if k in r.keys()}
                    for r in conv.execute(
                        "SELECT * FROM codex_conversation_source_files ORDER BY path")]
    finally:
        conv.close()
    return snap, cursors, seq, csnap, ccursors


def _compress(path):
    data = path.read_bytes()
    path.with_name(path.name + ".zst").write_bytes(zstd_bytes(data))
    path.unlink()


def _cursor(ns, path):
    conn = ns["open_cache_db"]()
    try:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute(
            "SELECT * FROM codex_session_files WHERE path=?", (str(path),)).fetchone())
    finally:
        conn.close()


def _drain(ns):
    return sys.modules["_cctally_cache"].run_codex_container_verification_drain(
        wait_for_requests=False)


@pytest.mark.parametrize("order", ["a-then-b", "b-then-a"])
def test_compress_after_ingest_changes_no_derived_row(env, order):
    """T1: anchored and legacy rows, two journal-retained accounts."""
    ns, provider, day = env
    a, b, keeper = day / _name(1), day / _name(2), day / _name(3)
    accounts = {a: ("acct-a", "a@example.com"), b: ("acct-b", "b@example.com")}
    write_rollout(keeper, token_records(1, session_id="keep"))
    for path in ([a, b] if order == "a-then-b" else [b, a]):
        (provider / "auth.json").write_text(_auth_json(*accounts[path]))
        records = token_records(4, session_id=path.stem[-4:])
        records[-1]["payload"]["rate_limits"] = {
            "limit_id": "codex", "primary": {"used_percent": 31.0,
            "window_minutes": 10080, "resets_at": 1784966699}}
        write_rollout(path, records)
        _sync_both(ns, quota_reconcile="defer")
    conn = ns["open_cache_db"]()
    try:   # b becomes a legacy row: what every pre-#968 cursor looks like
        conn.execute("UPDATE codex_session_files SET content_anchor=NULL, "
                     "content_anchor_version=NULL, content_anchor_offset=NULL "
                     "WHERE path=?", (str(b),))
        conn.commit()
    finally:
        conn.close()
    # Both account-bearing paths need quota facts and trigger ledger scopes.
    conn = ns["open_cache_db"]()
    try:
        for path in (a, b):
            facts = conn.execute(
                "SELECT account_key FROM quota_window_snapshots WHERE source_path=?",
                (str(path),)).fetchall()
            assert facts and all(row[0] is not None for row in facts)
            assert conn.execute(
                "SELECT 1 FROM quota_window_change_log WHERE op='insert' "
                "AND (new_source_root_key, new_logical_limit_key, new_observed_slot) "
                "IN (SELECT source_root_key, logical_limit_key, observed_slot "
                "FROM quota_window_snapshots WHERE source_path=?)",
                (str(path),)).fetchone()
    finally:
        conn.close()
    before = _state(ns)
    (provider / "auth.json").write_text(_auth_json("acct-c", "c@example.com"))
    _compress(a)
    _compress(b)
    _sync_both(ns, quota_reconcile="defer")
    # 92e06df85: the compressed rollouts vanish from discovery and the
    # eligible prune deletes their rows here.
    assert _state(ns) == before
    _drain(ns)
    after = _state(ns)
    # The quota-deletion mutant must reach the quota oracle while these
    # independently asserted accounting/cursor/attribution invariants hold.
    assert after[1:] == before[1:]
    for table in ACCOUNTING_TABLES:
        if table not in ("quota_window_snapshots", "quota_window_change_log"):
            assert after[0][table] == before[0][table]
    assert after[0]["quota_window_snapshots"] == before[0]["quota_window_snapshots"]
    assert after[0]["quota_window_change_log"] == before[0]["quota_window_change_log"]
    assert after == before
    for path in (a, b):
        cursor = _cursor(ns, path)
        assert cursor["container_kind"] == rc.ZSTD
        assert cursor["container_state"] == "accepted"
        assert cursor["content_anchor"] is not None   # legacy row now anchored
    calls = {"n": 0}
    real = rc.resolve_zstd_backend

    def counting():
        calls["n"] += 1
        return real()

    sys.modules["_lib_codex_rollout_container"].resolve_zstd_backend = counting
    try:
        _sync_both(ns, quota_reconcile="defer")
    finally:
        sys.modules["_lib_codex_rollout_container"].resolve_zstd_backend = real
    assert calls["n"] == 0                         # unchanged fingerprint: no decode
    assert _state(ns) == before


SHARED = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0199"


def _drop_anchors(ns) -> None:
    """Make every cursor in both stores a legacy (pre-#968, unanchored) row."""
    for opener, table in (("open_cache_db", "codex_session_files"),
                          ("open_conversations_db",
                           "codex_conversation_source_files")):
        conn = ns[opener]()
        try:
            conn.execute(f"UPDATE {table} SET content_anchor=NULL, "
                         "content_anchor_version=NULL, content_anchor_offset=NULL")
            conn.commit()
        finally:
            conn.close()


def test_both_contributors_of_a_two_file_conversation_verify(env):
    """Review P1-2: the thread table holds ONE row per conversation key,
    overwritten by the contributor ingested last, so the other contributor's
    legacy equivalence must compare thread identity per key, not by path."""
    ns, provider, day = env
    first, second = day / _name(4), day / _name(5)
    write_rollout(first, conversation_records(2, session_id=SHARED, label="f"))
    write_rollout(second, conversation_records(2, session_id=SHARED, label="g"))
    _sync_both(ns)
    conn = ns["open_cache_db"]()
    try:
        owners = {r[0] for r in conn.execute(
            "SELECT source_path FROM codex_conversation_threads")}
    finally:
        conn.close()
    assert len(owners) == 1                 # one contributor owns the shared row
    _drop_anchors(ns)
    before = _state(ns)
    _compress(first)
    _compress(second)
    _sync_both(ns)
    _drain(ns)
    for path in (first, second):
        cursor = _cursor(ns, path)
        assert cursor["container_state"] == "accepted", (
            path.name, cursor["container_state_reason"])
    assert _state(ns) == before


def _mutate_same_length(data: bytes) -> bytes:
    marker = b'"output_tokens":40'
    assert marker in data
    return data.replace(marker, b'"output_tokens":41', 1)


def _entries(ns, path):
    conn = ns["open_cache_db"]()
    try:
        return conn.execute(
            "SELECT line_offset, output_tokens FROM codex_session_entries "
            "WHERE source_path=? ORDER BY line_offset", (str(path),)).fetchall()
    finally:
        conn.close()


def _incarnation(ns, path):
    conn = ns["open_cache_db"]()
    try:
        return conn.execute(
            "SELECT MAX(incarnation) FROM codex_file_incarnations").fetchone()[0]
    finally:
        conn.close()


def test_equal_length_mutation_is_detected_on_an_anchored_row(env):
    ns, _provider, day = env
    path, keeper = day / _name(1), day / _name(3)
    write_rollout(keeper, token_records(1, session_id="keep"))
    write_rollout(path, token_records(3, session_id="s1"))
    _sync_both(ns)
    mutated = _mutate_same_length(path.read_bytes())
    path.with_name(path.name + ".zst").write_bytes(zstd_bytes(mutated))
    path.unlink()
    before = _entries(ns, path)
    _sync_both(ns)
    assert _entries(ns, path) == before
    _drain(ns)
    # Never silently adopted: the anchor mismatch takes the off-hook reset.
    assert [o for _off, o in _entries(ns, path)][0] == 41
    assert _cursor(ns, path)["container_state"] == "accepted"


def test_equal_length_mutation_on_a_legacy_row_is_left_unverified(env):
    ns, _provider, day = env
    path, keeper = day / _name(1), day / _name(3)
    write_rollout(keeper, token_records(1, session_id="keep"))
    write_rollout(path, token_records(3, session_id="s1"))
    _sync_both(ns)
    conn = ns["open_cache_db"]()
    try:
        conn.execute("UPDATE codex_session_files SET content_anchor=NULL, "
                     "content_anchor_version=NULL, content_anchor_offset=NULL")
        conn.commit()
    finally:
        conn.close()
    before = _entries(ns, path)
    mutated = _mutate_same_length(path.read_bytes())
    path.with_name(path.name + ".zst").write_bytes(zstd_bytes(mutated))
    path.unlink()
    _sync_both(ns)
    assert _entries(ns, path) == before
    _drain(ns)
    assert _entries(ns, path) == before            # kept, not churned, not pruned
    assert _cursor(ns, path)["container_state"] == "unverified"


def test_a_verified_longer_target_ingests_only_the_tail(env):
    ns, _provider, day = env
    path, keeper = day / _name(1), day / _name(3)
    write_rollout(keeper, token_records(1, session_id="keep"))
    write_rollout(path, token_records(3, session_id="s1"))
    _sync_both(ns)
    conn = ns["open_cache_db"]()
    try:
        ids_before = conn.execute(
            "SELECT id, line_offset FROM codex_session_entries WHERE source_path=? "
            "ORDER BY line_offset", (str(path),)).fetchall()
    finally:
        conn.close()
    incarnation = _incarnation(ns, path)
    longer = day / "longer.tmp"
    write_rollout(longer, token_records(5, session_id="s1"))
    path.with_name(path.name + ".zst").write_bytes(zstd_bytes(longer.read_bytes()))
    longer.unlink()
    path.unlink()
    before = _entries(ns, path)
    _sync_both(ns)
    assert _entries(ns, path) == before
    _drain(ns)
    conn = ns["open_cache_db"]()
    try:
        ids_after = conn.execute(
            "SELECT id, line_offset FROM codex_session_entries WHERE source_path=? "
            "ORDER BY line_offset", (str(path),)).fetchall()
    finally:
        conn.close()
    assert ids_after[:len(ids_before)] == ids_before
    assert len(ids_after) == len(ids_before) + 2
    assert _incarnation(ns, path) == incarnation


def test_a_shorter_target_takes_the_off_hook_reset(env):
    ns, _provider, day = env
    path, keeper = day / _name(1), day / _name(3)
    write_rollout(keeper, token_records(1, session_id="keep"))
    write_rollout(path, token_records(5, session_id="s1"))
    _sync_both(ns)
    incarnation = _incarnation(ns, path)
    shorter = day / "shorter.tmp"
    write_rollout(shorter, token_records(2, session_id="s1"))
    path.with_name(path.name + ".zst").write_bytes(zstd_bytes(shorter.read_bytes()))
    shorter.unlink()
    path.unlink()
    _sync_both(ns)
    assert len(_entries(ns, path)) == 5            # the walk kept everything
    _drain(ns)
    assert len(_entries(ns, path)) == 2
    assert _incarnation(ns, path) == incarnation + 1


def _cumulative_records(totals, *, session_id):
    out = token_records(len(totals), session_id=session_id)
    for record, total in zip(out[2:], totals):
        record["payload"]["info"]["total_token_usage"]["total_tokens"] = total
    return out


def test_legacy_cumulative_watermark_change_is_not_adopted(env):
    """T7: identical per-turn rows, prefix cumulative 1,000 -> 2,000, then 1,500."""
    ns, _provider, day = env
    path, keeper = day / _name(1), day / _name(3)
    write_rollout(keeper, token_records(1, session_id="keep"))
    write_rollout(path, _cumulative_records(
        [100 * (i + 1) for i in range(10)], session_id="s1"))
    _sync_both(ns)
    assert _cursor(ns, path)["last_total_tokens"] == 1000
    conn = ns["open_cache_db"]()
    try:
        conn.execute("UPDATE codex_session_files SET content_anchor=NULL, "
                     "content_anchor_version=NULL, content_anchor_offset=NULL")
        conn.commit()
    finally:
        conn.close()
    before = _entries(ns, path)
    mutated = _cumulative_records(
        [200 * (i + 1) for i in range(10)] + [1500], session_id="s1")
    tmp = day / "mutated.tmp"
    write_rollout(tmp, mutated)
    path.with_name(path.name + ".zst").write_bytes(zstd_bytes(tmp.read_bytes()))
    tmp.unlink()
    path.unlink()
    _sync_both(ns)
    assert _entries(ns, path) == before
    _drain(ns)
    cursor = _cursor(ns, path)
    assert cursor["container_state"] == "unverified"
    assert cursor["last_total_tokens"] == 1000
    assert _entries(ns, path) == before            # the 1,500 event never lands


def _pending_conversation(env):
    ns, _provider, day = env
    path = day / _name(1)
    write_rollout(path, conversation_records(3, session_id=SHARED))
    _sync_both(ns)
    _compress(path)
    _sync_both(ns)
    return ns, path


@pytest.mark.parametrize("failure", ["decoder-unavailable", "decode-error", "unstable-stat"])
def test_inconclusive_container_keeps_both_stores_retryable(env, monkeypatch, failure):
    ns, path = _pending_conversation(env)
    before = _state(ns)
    if failure == "decoder-unavailable":
        monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    elif failure == "decode-error":
        target = path.with_name(path.name + ".zst")
        target.write_bytes(target.read_bytes()[:-4])
    else:
        monkeypatch.setattr(rc, "validate_full_stream", lambda selected:
                            rc.StreamValidation(False, None, None, "unstable-stat"))
    result = _drain(ns)
    assert result.pending == 2
    assert _cursor(ns, path)["container_state"] == "pending"
    assert _state(ns) == before


def test_verification_preparation_holds_no_writer_lock(env, monkeypatch):
    ns, _path = _pending_conversation(env)
    cache = sys.modules["_cctally_cache"]
    real = cache._verify_codex_container
    observed = []

    def prepare(conn, table, snap, selected):
        assert not conn.in_transaction
        take, give = ((cache._cache_store_locks, cache._release_cache_store_locks)
                      if table == "codex_session_files" else
                      (cache._conversation_store_locks,
                       cache._release_conversation_store_locks))
        token = take()
        assert token is not None
        give(token)
        observed.append(table)
        return real(conn, table, snap, selected)

    monkeypatch.setattr(cache, "_verify_codex_container", prepare)
    assert _drain(ns).adopted == 2
    assert observed == ["codex_session_files", "codex_conversation_source_files"]


def test_cursor_changed_after_prepare_cannot_adopt_stale_verdict(env, monkeypatch):
    ns, path = _pending_conversation(env)
    cache = sys.modules["_cctally_cache"]
    real = cache._verify_codex_container

    def race(conn, table, snap, selected):
        verdict = real(conn, table, snap, selected)
        if table == "codex_session_files":
            conn.execute("UPDATE codex_session_files SET last_model='new-generation' "
                         "WHERE path=?", (str(path),))
            conn.commit()
        return verdict

    monkeypatch.setattr(cache, "_verify_codex_container", race)
    result = _drain(ns)
    assert result.pending == 1 and result.adopted == 1
    assert _cursor(ns, path)["container_state"] == "pending"
    assert _cursor(ns, path)["last_model"] == "new-generation"


def test_legacy_conversation_turn_seed_is_part_of_equivalence(env):
    ns, path = _pending_conversation(env)
    _drop_anchors(ns)
    conv = ns["open_conversations_db"]()
    try:
        conv.execute("UPDATE codex_conversation_source_files SET last_turn_id=? "
                     "WHERE path=?", ("different-turn", str(path)))
        conv.commit()
    finally:
        conv.close()
    before = _state(ns)
    _drain(ns)
    conv = ns["open_conversations_db"]()
    try:
        row = conv.execute("SELECT container_state, last_turn_id FROM "
                           "codex_conversation_source_files WHERE path=?",
                           (str(path),)).fetchone()
    finally:
        conv.close()
    assert tuple(row) == ("unverified", "different-turn")
    assert _state(ns) == before


def test_admission_retry_does_not_spend_distinct_identity_limit(env, monkeypatch):
    ns, path = _pending_conversation(env)
    import _lib_codex_drain_admission as adm
    cache = sys.modules["_cctally_cache"]
    real = adm.admit
    calls = []

    def refuse_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return adm.Admission(False, None, None)
        return real(*args, **kwargs)

    monkeypatch.setattr(adm, "admit", refuse_once)
    monkeypatch.setattr(cache, "_drain_backoff", lambda _adm: None)
    result = cache.run_codex_container_verification_drain(
        wait_for_requests=True, max_identities=1)
    assert result.adopted == 1 and result.retried == 1 and result.yielded == 1
    assert _cursor(ns, path)["container_state"] == "accepted"


def test_interrupted_adoption_keeps_pending_history(env, monkeypatch):
    ns, path = _pending_conversation(env)
    before = _state(ns)
    cache = sys.modules["_cctally_cache"]

    class Interrupted(BaseException):
        pass

    def interrupt(table, path):
        raise Interrupted()

    monkeypatch.setattr(cache, "_CODEX_VERIFY_PRECOMMIT_HOOK", interrupt)
    with pytest.raises(Interrupted):
        _drain(ns)
    assert _cursor(ns, path)["container_state"] == "pending"
    assert _state(ns) == before


def test_new_recurring_verification_statements_pass_sql_plan_guard(env):
    ns, _path = _pending_conversation(env)
    _drop_anchors(ns)
    import _sql_plan_guard as guard
    sites = {"_cctally_cache." + name for name in (
        "run_codex_container_verification_drain", "_load_cursor_snapshot",
        "_retained_messages", "_retained_thread_facts", "_retained_prefix_facts",
        "_adopt_codex_container", "_apply_codex_verdict")}
    with guard.capture_sql_plans() as recorder:
        result = _drain(ns)
    assert result.adopted == 2
    statements = [s for s in recorder.statements if s.call_sites
                  and s.call_sites[0] in sites]
    assert statements and any("container_state='pending'" in s.sql for s in statements)
    assert all(not any("<explain failed" in p for p in s.plan) for s in statements)
    findings, stale = guard.classify(statements, allowlist=(), pending=())
    assert not stale
    assert not findings, [(f.statement.sql, f.detail) for f in findings]


def test_verified_prefix_with_removed_torn_tail_retains_incarnation(env):
    """A shorter target is destructive only below the committed cursor.

    Removing never-consumed torn bytes preserves the entire accepted prefix;
    adoption must finish at that prefix rather than provoke a later shrink reset.
    """
    ns, _provider, day = env
    path = day / _name(1)
    write_rollout(path, conversation_records(2, session_id=SHARED))
    complete = path.read_bytes()
    with path.open("ab") as fh:
        fh.write(b'{"type":"event_msg"')
    _sync_both(ns)
    incarnation = _incarnation(ns, path)
    before = _state(ns)
    path.with_name(path.name + ".zst").write_bytes(zstd_bytes(complete))
    path.unlink()
    _sync_both(ns)
    _drain(ns)
    assert _cursor(ns, path)["size_bytes"] == len(complete)
    _sync_both(ns)
    assert _incarnation(ns, path) == incarnation
    after = _state(ns)
    assert after[0] == before[0] and after[3] == before[3]


def test_legacy_metadata_only_cursor_verifies_in_both_stores(env):
    ns, _provider, day = env
    path = day / _name(1)
    write_rollout(path, conversation_records(0, session_id=SHARED))
    _sync_both(ns)
    _drop_anchors(ns)
    before = _state(ns)
    _compress(path)
    _sync_both(ns)
    result = _drain(ns)
    assert result.adopted == 2 and result.unverified == 0
    assert _state(ns) == before


@pytest.mark.parametrize("store", ["cache", "conversations"])
@pytest.mark.parametrize("replace_after_yield", [False, True])
def test_torn_suffix_adoption_is_safe_before_second_admission(
        env, monkeypatch, store, replace_after_yield):
    _assert_torn_suffix_adoption_is_safe_before_second_admission(
        env, monkeypatch, store, replace_after_yield, b"")


@pytest.mark.parametrize("store", ["cache", "conversations"])
@pytest.mark.parametrize("replace_after_yield", [False, True])
def test_partial_torn_suffix_adoption_is_safe_before_second_admission(
        env, monkeypatch, store, replace_after_yield):
    _assert_torn_suffix_adoption_is_safe_before_second_admission(
        env, monkeypatch, store, replace_after_yield, b'{"type')


def _assert_torn_suffix_adoption_is_safe_before_second_admission(
        env, monkeypatch, store, replace_after_yield, retained_suffix):
    """R01: callers between admissions cannot reset a retained verified prefix.

    A subsequent actual shrink must still reset its new physical generation.
    """
    ns, provider, day = env
    path = day / _name(1)
    (provider / "auth.json").write_text(_auth_json("acct-a", "a@example.com"))
    write_rollout(path, conversation_records(2, session_id=SHARED))
    complete = path.read_bytes()
    with path.open("ab") as fh:
        fh.write(b'{"type":"event_msg"')
    _sync_both(ns)
    before = _state(ns)
    incarnation = _incarnation(ns, path)
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    target = path.with_name(path.name + ".zst")
    target.write_bytes(zstd_bytes(complete + retained_suffix))
    path.unlink()
    _sync_both(ns)
    cache = sys.modules["_cctally_cache"]
    import _lib_codex_drain_admission as adm
    table = ("codex_session_files" if store == "cache" else
             "codex_conversation_source_files")
    opener = "open_cache_db" if store == "cache" else "open_conversations_db"
    # Real per-identity executor, isolated from sibling-store iteration.
    conn = ns[opener]()
    try:
        snap = cache._load_cursor_snapshot(conn, table, str(path))
        verdict = cache._verify_codex_container(conn, table, snap,
                                                 rc.select_container(path))
    finally:
        conn.close()
    assert verdict.outcome == "adopt-ingest"
    real_admit = adm.admit
    admissions = []
    interleaved = []

    def admit(*args, **kwargs):
        admissions.append(1)
        if len(admissions) == 2:
            # Adoption has committed and released gate and store locks.
            _sync_both(ns)
            interleaved.append(_state(ns))
            return adm.Admission(False, None, None)
        return real_admit(*args, **kwargs)

    monkeypatch.setattr(adm, "admit", admit)
    counts = dict(adopted=0, ingested=0, reset=0, unverified=0, pending=0,
                  yielded=0, retried=0)
    assert cache._apply_codex_verdict(
        table, str(path), snap, verdict, adm, counts,
        wait_for_requests=False) == "yield-stop"
    assert interleaved == [before]  # IDs, seeds, cursors, attribution and ledgers
    assert _incarnation(ns, path) == incarnation
    monkeypatch.setattr(adm, "admit", real_admit)
    if replace_after_yield:
        short = day / "replacement.tmp"
        write_rollout(short, conversation_records(1, session_id=SHARED))
        target.write_bytes(zstd_bytes(short.read_bytes()))
        short.unlink()
    import _sql_plan_guard as guard
    with guard.capture_sql_plans() as recorder:
        _drain(ns)
    statements = [statement for statement in recorder.statements
                  if statement.call_sites and statement.call_sites[0]
                  == "_cctally_cache._apply_codex_verdict"]
    findings, stale = guard.classify(statements, allowlist=(), pending=())
    assert not stale and not findings
    if replace_after_yield:
        assert len(_entries(ns, path)) == 1
        assert _incarnation(ns, path) == incarnation + 1
    else:
        after = _state(ns)
        assert after[0] == before[0] and after[2:4] == before[2:4], {
            "sequence_before": [(tuple(row), row.keys()) for row in before[2]],
            "sequence_after": [(tuple(row), row.keys()) for row in after[2]]}
        assert _incarnation(ns, path) == incarnation
        assert _cursor(ns, path)["size_bytes"] == len(complete + retained_suffix)
        assert _cursor(ns, path)["last_byte_offset"] == len(complete)
        for slot in (1, 4):  # Both cursor tables: only the scan target changes.
            for current, retained in zip(after[slot], before[slot], strict=True):
                assert {k: v for k, v in current.items() if k != "size_bytes"} == {
                    k: v for k, v in retained.items() if k != "size_bytes"}
        # Validation consumed the target, including any torn suffix; no complete
        # line remains unread. The committed cursor must stay at the last newline.
        assert _cursor(ns, path)["ingest_complete"] == 1
        _sync_both(ns)
        assert _state(ns) == after
        assert _incarnation(ns, path) == incarnation
        assert any("SET size_bytes=?" in statement.sql for statement in statements)


@pytest.mark.parametrize("store", ["cache", "conversations"])
@pytest.mark.parametrize("failure", ["decode-error", "read-error", "unstable-stat"])
def test_later_implicit_sync_retries_verification_after_transient_failure(
        env, monkeypatch, store, failure):
    """R02: recovered debt reaches the real attempt-throttled dispatcher."""
    ns, path = _pending_conversation(env)
    cache = sys.modules["_cctally_cache"]
    before = _state(ns)
    real_validate = rc.validate_full_stream
    monkeypatch.setattr(rc, "validate_full_stream", lambda selected:
                        rc.StreamValidation(False, None, None, failure))
    assert _drain(ns).pending == 2
    assert _state(ns) == before
    monkeypatch.setattr(rc, "validate_full_stream", real_validate)
    spawned = []

    def spawn(command):
        assert command == cache.CODEX_REPLAY_DRAIN_COMMAND
        # Replace only the subprocess boundary; execute the real hidden worker.
        for take, give in ((cache._cache_store_locks,
                            cache._release_cache_store_locks),
                           (cache._conversation_store_locks,
                            cache._release_conversation_store_locks)):
            token = take()
            assert token is not None
            give(token)
        spawned.append(command)
        assert cache.cmd_codex_replay_drain_internal(None) == 0
        return True

    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached", spawn)
    opener = "open_cache_db" if store == "cache" else "open_conversations_db"
    sync = "sync_codex_cache" if store == "cache" else "sync_codex_conversations"

    def implicit():
        conn = ns[opener]()
        try:
            return ns[sync](conn)
        finally:
            conn.close()

    implicit()  # The initial attempt marker must still throttle this request.
    assert not spawned
    marker = cache._cctally_core.APP_DIR / cache.CODEX_REPLAY_DRAIN_MARKER_NAME
    os.utime(marker, (0, 0))
    implicit()  # No manual drain/worker invocation after recovery.
    assert spawned == [cache.CODEX_REPLAY_DRAIN_COMMAND]
    assert _cursor(ns, path)["container_state"] == "accepted"
    conv = ns["open_conversations_db"]()
    try:
        assert conv.execute("SELECT container_state FROM "
                            "codex_conversation_source_files").fetchone()[0] == "accepted"
    finally:
        conv.close()
    assert _state(ns) == before


def test_terminal_unverified_does_not_become_retry_debt(env, monkeypatch):
    ns, path = _pending_conversation(env)
    _drop_anchors(ns)
    conv = ns["open_conversations_db"]()
    try:
        conv.execute("UPDATE codex_conversation_source_files SET last_turn_id='wrong'")
        conv.commit()
    finally:
        conv.close()
    assert _drain(ns).unverified == 1
    cache = sys.modules["_cctally_cache"]
    os.utime(cache._cctally_core.APP_DIR / cache.CODEX_REPLAY_DRAIN_MARKER_NAME,
             (0, 0))
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: pytest.fail("terminal unverified spawned retry"))
    _sync_both(ns)


@pytest.mark.parametrize("store", ["cache", "conversations"])
def test_terminal_unverified_retries_only_a_different_rejected_generation(
        env, monkeypatch, store):
    """A terminal rejection belongs to one candidate, not every future file."""
    ns, _provider, day = env
    path = day / _name(1)
    write_rollout(path, conversation_records(3, session_id=SHARED))
    good = path.read_bytes()
    # Root recognition uses its own bounded source probe. A plain newest
    # sibling proves the root while this control observes zero decoding of
    # the terminal compressed candidate itself.
    write_rollout(day / _name(9), token_records(1, session_id="keeper"))
    _sync_both(ns)
    opener, table, sync = (("open_cache_db", "codex_session_files", "sync_codex_cache")
        if store == "cache" else
        ("open_conversations_db", "codex_conversation_source_files", "sync_codex_conversations"))
    conn = ns[opener]()
    try:
        conn.execute(f"UPDATE {table} SET content_anchor=NULL, "
                     "content_anchor_version=NULL,content_anchor_offset=NULL")
        conn.commit()
    finally:
        conn.close()
    rejected = (_mutate_same_length(good) if store == "cache" else
                good.replace(b"Answer a0", b"Answer z0", 1))
    assert rejected != good and len(rejected) == len(good)
    target = path.with_name(path.name + ".zst")
    target.write_bytes(zstd_bytes(rejected))
    path.unlink()
    def implicit():
        conn = ns[opener]()
        try:
            return ns[sync](conn)
        finally:
            conn.close()
    implicit()
    before = _state(ns)
    assert _drain(ns).unverified == 1
    def row():
        conn = ns[opener]()
        try:
            return conn.execute(f"SELECT container_state,pending_container_fingerprint "
                                f"FROM {table} WHERE path=?", (str(path),)).fetchone()
        finally:
            conn.close()
    terminal = row()
    assert terminal[0] == "unverified"
    assert rc.ContainerFingerprint.from_json(terminal[1]) == rc.select_container(path).fingerprint
    # No decode or detached retry for the exact rejected candidate. This is
    # measured at stream opening/validation, not at the cheap capability probe.
    real_open = rc.open_decoded
    def open_without_terminal(selected, **kwargs):
        assert selected.logical != path, "terminal candidate decoded"
        return real_open(selected, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(rc, "open_decoded", open_without_terminal)
        m.setattr(rc, "validate_full_stream", lambda *_a, **_kw: pytest.fail("terminal candidate validated"))
        m.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                  lambda _command: pytest.fail("terminal candidate spawned retry"))
        implicit()
        implicit()
    assert row() == terminal and _state(ns) == before
    replacement = target.with_name("replacement.tmp")
    replacement.write_bytes(zstd_bytes(good))
    os.replace(replacement, target)
    selected = rc.select_container(path)
    assert selected.fingerprint != rc.ContainerFingerprint.from_json(terminal[1])
    implicit()
    assert row() == ("pending", selected.fingerprint.to_json())
    assert _state(ns) == before  # re-arming itself preserves the retained tuple
    assert _drain(ns).adopted == 1
    assert row()[0] == "accepted" and _state(ns) == before


@pytest.mark.parametrize("table", ["codex_session_files", "codex_conversation_source_files"])
def test_dynamic_container_helpers_write_only_their_store_and_temp_views(
        env, monkeypatch, table):
    """#968 declaration proof: observe all six concrete dynamic write sites.

    A consumed prefix plus an unconsumed torn suffix reaches both adoption
    and metadata-only EOF reconciliation. Both real store connections carry
    an SQLite authorizer; no mocked connection or substitute SQL is used.
    """
    import collections
    import _lib_codex_drain_admission as admission
    from test_stats_writer_surface_386 import STATS_TABLES

    ns, _provider, day = env
    cache = sys.modules["_cctally_cache"]
    path = day / _name(7)
    write_rollout(path, conversation_records(2, session_id="dynamic-writes"))
    complete = path.read_bytes()
    path.write_bytes(complete + b'{"torn":')
    _sync_both(ns)
    path.with_name(path.name + ".zst").write_bytes(zstd_bytes(complete))
    path.unlink()
    selected = rc.select_container(path)
    assert selected is not None
    seen = []
    statements = []
    writes = set(sys.modules["_cctally_store"]._GUARD_MUTATIONS) | {
        sqlite3.SQLITE_CREATE_TEMP_VIEW, sqlite3.SQLITE_DROP_TEMP_VIEW}
    opener_name = "open_cache_db" if table == "codex_session_files" else "open_conversations_db"
    real_open = getattr(cache, opener_name)
    core = sys.modules["_cctally_core"]
    expected_path = core.CACHE_DB_PATH if table == "codex_session_files" else core.CONVERSATIONS_DB_PATH

    def observe(conn):
        databases = {r[1]: r[2] for r in conn.execute("PRAGMA database_list")}
        assert pathlib.Path(databases["main"]) == pathlib.Path(expected_path)
        assert pathlib.Path(databases["main"]) != pathlib.Path(core.DB_PATH)

        def authorizer(action, target, column, schema, origin):
            if action in writes:
                seen.append((action, target, schema, origin))
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        conn.set_trace_callback(statements.append)
        return conn

    def observed_open(*args, **kwargs):
        return observe(real_open(*args, **kwargs))

    monkeypatch.setattr(cache, opener_name, observed_open)
    conn = observed_open()
    try:
        for forbidden in ("quota_window_blocks", "quota_projection_state",
                          "five_hour_blocks", "weekly_usage_snapshots"):
            with pytest.raises(ValueError):
                cache._load_cursor_snapshot(conn, forbidden, str(path))
            with pytest.raises(ValueError):
                cache._mark_codex_containers_pending(conn, forbidden, [
                    (str(path), selected.fingerprint.to_json(), "forbidden")])
        assert seen == []
        assert cache._mark_codex_containers_pending(conn, table, [
            (str(path), selected.fingerprint.to_json(), "container-changed")]) == 1
        snap = cache._load_cursor_snapshot(conn, table, str(path))
        verdict = cache._verify_codex_container(conn, table, snap, selected)
        assert verdict.outcome == "adopt-ingest"
        assert verdict.complete_line_end == snap.last_byte_offset < snap.size_bytes
    finally:
        conn.close()
    counts = collections.defaultdict(int)
    assert cache._apply_codex_verdict(
        table, str(path), snap,
        verdict._replace(outcome="unverified", reason="legacy-inequivalent"),
        admission, counts, wait_for_requests=False) == "done"
    assert counts["unverified"] == 1
    conn = observed_open()
    try:
        assert cache._mark_codex_containers_pending(conn, table, [
            (str(path), selected.fingerprint.to_json(), "container-changed")]) == 1
        snap = cache._load_cursor_snapshot(conn, table, str(path))
    finally:
        conn.close()
    assert cache._apply_codex_verdict(
        table, str(path), snap, verdict, admission, counts,
        wait_for_requests=False) == "done"
    assert counts["adopted"] == counts["ingested"] == 1
    conn = observed_open()
    try:
        after = cache._load_cursor_snapshot(conn, table, str(path))
        assert after.state == "accepted"
        assert after.size_bytes == after.last_byte_offset == len(complete)
    finally:
        conn.close()

    # DROP and CREATE each run against the actual conversations TEMP schema.
    conv = ns["open_conversations_db"]()
    try:
        conv.set_authorizer(lambda action, target, column, schema, origin:
            (seen.append((action, target, schema, origin)) or sqlite3.SQLITE_OK)
            if action in writes else sqlite3.SQLITE_OK)
        conv.set_trace_callback(statements.append)
        assert cache._install_codex_last_good_views(conv)
        assert cache._install_codex_last_good_views(conv)
        assert conv.execute("SELECT COUNT(*) FROM temp.codex_last_good_threads").fetchone()[0] >= 0
    finally:
        conv.close()
    updates = [sql for sql in statements if sql.startswith(f"UPDATE {table} SET ")]
    assert any("container_state='pending'" in sql and "NOT (" in sql for sql in updates)
    assert any("container_kind=" in sql for sql in updates)
    assert any("SET container_state='unverified'" in sql for sql in updates)
    assert any("SET size_bytes=" in sql and "container_state='accepted'" in sql for sql in updates)
    assert (sqlite3.SQLITE_UPDATE, table, "main", None) in seen
    view = cache._barrier.LAST_GOOD_THREADS_VIEW
    assert (sqlite3.SQLITE_CREATE_TEMP_VIEW, view, "temp", None) in seen
    assert (sqlite3.SQLITE_DROP_TEMP_VIEW, view, "temp", None) in seen
    assert not [(action, target, schema, origin) for action, target, schema, origin in seen
                if schema == "main" and target in STATS_TABLES]
    allowed_main = {table, "cache_meta", "codex_barrier_population",
                    "codex_barrier_targets", "codex_barrier_membership",
                    "codex_barrier_certificates", "codex_barrier_repairs",
                    "codex_barrier_entry_threads"}
    assert not [(action, target, schema, origin) for action, target, schema, origin in seen
                if schema == "main" and target not in allowed_main]
    assert {schema for _action, _target, schema, _origin in seen} <= {"main", "temp"}
    assert {target for _action, target, schema, _origin in seen if schema == "temp"} <= {
        "sqlite_temp_master", "codex_conversation_threads", view}
    print(json.dumps({"cursorTable": table, "statsMainWrites": 0,
                      "authorizerTargets": sorted({(schema, target)
                          for _action, target, schema, _origin in seen}),
                      "dynamicUpdateShapes": 4, "dynamicViewShapes": 2}, sort_keys=True))


# ── #970 spec 5.5: location-only adoption and relocation verification ─────
BARRIER_TABLES = (
    "codex_barrier_membership", "codex_barrier_targets",
    "codex_barrier_population", "codex_barrier_entry_threads",
    "codex_barrier_certificates", "codex_barrier_repairs", "codex_prune_debt",
)
CONVERSATION_EXTRA = (
    "codex_conversation_publications", "codex_conversation_published_threads",
    "codex_conversation_prune_debt",
)


def _quota_records(session_id):
    records = conversation_records(2, session_id=session_id)
    records[-1]["payload"]["rate_limits"] = {
        "limit_id": "codex", "primary": {"used_percent": 21.0,
        "window_minutes": 10080, "resets_at": 1784966699}}
    return records


def _full_state(ns):
    """Every derived row (ids included), both change ledgers, the mutation
    sequence, the barrier state and every cursor column but container_path,
    in both stores — plus the two container_path columns on their own."""
    out = {}
    locations = {}
    for opener, cursor_table, tables in (
            ("open_cache_db", "codex_session_files",
             ACCOUNTING_TABLES + BARRIER_TABLES),
            ("open_conversations_db", "codex_conversation_source_files",
             CONVERSATION_TABLES + CONVERSATION_EXTRA)):
        conn = ns[opener]()
        try:
            present = [t for t in tables if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()]
            # Some barrier tables are WITHOUT ROWID: take every column of every
            # row in a stable order instead of rowid order.
            out[opener] = {t: sorted((tuple(r) for r in conn.execute(
                f"SELECT * FROM {t}")), key=repr) for t in present}
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM {cursor_table} ORDER BY path")]
            locations[opener] = [r.pop("container_path") for r in rows]
            out[opener + ":cursors"] = rows
            out[opener + ":seq"] = conn.execute(
                "SELECT value FROM cache_meta "
                "WHERE key='codex_physical_mutation_seq'").fetchall()
        finally:
            conn.close()
    return out, locations


def _place(path, records, compressed):
    write_rollout(path, records)
    if compressed:
        _compress(path)
        return path.with_name(path.name + ".zst")
    return path


@pytest.mark.parametrize("direction", ["archive", "unarchive"])
@pytest.mark.parametrize("compressed", [False, True], ids=["plain", "zstd"])
def test_location_adoption_changes_only_container_path(env, compressed, direction):
    """#970 A3/G6: a same-volume archive or unarchive move is not content
    work. Routed as a transition or a replacement it would delete and
    re-insert history, bump the incarnation or churn the ledgers and the
    publication barrier; it must change only the accepted location, in
    both stores."""
    ns, provider, day = env
    archive = provider / "archived_sessions"
    archive.mkdir()
    write_rollout(day / _name(9), token_records(1, session_id="keep"))
    start = day if direction == "archive" else archive
    container = _place(start / _name(1), _quota_records(SHARED), compressed)
    _sync_both(ns)
    _sync_both(ns)
    before, locations = _full_state(ns)
    dest = (archive if direction == "archive" else day) / container.name
    os.rename(container, dest)
    _sync_both(ns)
    after, moved = _full_state(ns)
    assert after == before
    logical = day / _name(1)
    expected = None if dest == logical else str(dest)
    for opener in ("open_cache_db", "open_conversations_db"):
        index = [r["path"] for r in after[opener + ":cursors"]].index(str(logical))
        assert locations[opener][index] == (
            None if container == logical else str(container))
        assert moved[opener][index] == expected
    assert _cursor(ns, logical)["container_state"] == "accepted"


@pytest.mark.parametrize("compressed", [False, True], ids=["plain", "zstd"])
def test_location_adoption_writes_only_its_cursor_table(env, monkeypatch, compressed):
    """#970 declaration proof for the location-only `UPDATE {table} SET
    container_path=?`. A `{table}` target is invisible to the static write
    census, so a wrong table there could write a stats table unseen. Observe
    the real archive move on both stores with an SQLite authorizer: each
    adoption writes only its own cursor table in its own store, and any
    other table is refused before SQL."""
    from test_stats_writer_surface_386 import STATS_TABLES

    ns, provider, day = env
    cache = sys.modules["_cctally_cache"]
    core = sys.modules["_cctally_core"]
    archive = provider / "archived_sessions"
    archive.mkdir()
    write_rollout(day / _name(9), token_records(1, session_id="keep"))
    container = _place(day / _name(1), _quota_records(SHARED), compressed)
    _sync_both(ns)
    writes = set(sys.modules["_cctally_store"]._GUARD_MUTATIONS)
    stores = {"codex_session_files": pathlib.Path(core.CACHE_DB_PATH),
              "codex_conversation_source_files": pathlib.Path(core.CONVERSATIONS_DB_PATH)}
    calls = []
    real = cache._adopt_codex_container_location

    def observed(conn, table, path, selected, snap):
        main = {r[1]: r[2] for r in conn.execute("PRAGMA database_list")}["main"]
        seen = []

        def authorizer(action, target, column, schema, origin):
            if action in writes:
                seen.append((target, schema))
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        try:
            adopted = real(conn, table, path, selected, snap)
        finally:
            conn.set_authorizer(None)
        calls.append((table, pathlib.Path(main), adopted, seen))
        return adopted

    monkeypatch.setattr(cache, "_adopt_codex_container_location", observed)
    os.rename(container, archive / container.name)
    _sync_both(ns)
    assert sorted(call[0] for call in calls) == sorted(stores)
    for table, main, adopted, seen in calls:
        assert adopted is True
        assert main == stores[table] and main != pathlib.Path(core.DB_PATH)
        assert seen and set(seen) == {(table, "main")}
        assert not [target for target, _schema in seen if target in STATS_TABLES]
    conn = cache.open_cache_db()
    try:
        for forbidden in ("quota_window_blocks", "quota_projection_state",
                          "five_hour_blocks", "weekly_usage_snapshots"):
            with pytest.raises(ValueError):
                real(conn, forbidden, str(day / _name(1)), None, None)
    finally:
        conn.close()


@pytest.mark.parametrize("compressed", [False, True], ids=["plain", "zstd"])
def test_relocation_with_new_inode_is_verified_not_reset(env, compressed):
    """#970 A3/SR-03: a cross-volume move (copy plus delete, so a new inode)
    must go through verification. Read as a plain replacement it would reset
    the rollout to byte zero, delete and re-insert its rows and bump the
    incarnation."""
    import shutil
    ns, provider, day = env
    archive = provider / "archived_sessions"
    archive.mkdir()
    write_rollout(day / _name(9), token_records(1, session_id="keep"))
    container = _place(day / _name(1), _quota_records(SHARED), compressed)
    _sync_both(ns)
    logical = day / _name(1)
    incarnation = _incarnation(ns, logical)
    before = _state(ns)
    dest = archive / container.name
    shutil.copy2(container, dest)
    container.unlink()
    _sync_both(ns)
    assert _cursor(ns, logical)["container_state"] == "pending"
    _drain(ns)
    cursor = _cursor(ns, logical)
    assert cursor["container_state"] == "accepted", cursor["container_state_reason"]
    assert cursor["container_path"] == str(dest)
    assert _incarnation(ns, logical) == incarnation
    assert _state(ns) == before


def test_legacy_plain_cursor_relocation_is_verified(env):
    """#970 SR-03: a legacy plain cursor (no device/inode evidence) cannot
    prove a move, so its relocation is verified, never adopted blindly and
    never reset."""
    ns, provider, day = env
    archive = provider / "archived_sessions"
    archive.mkdir()
    write_rollout(day / _name(9), token_records(1, session_id="keep"))
    logical = _place(day / _name(1), _quota_records(SHARED), False)
    _sync_both(ns)
    for opener, table in (("open_cache_db", "codex_session_files"),
                          ("open_conversations_db",
                           "codex_conversation_source_files")):
        conn = ns[opener]()
        try:
            conn.execute(f"UPDATE {table} SET device_id=NULL, inode=NULL "
                         "WHERE path=?", (str(logical),))
            conn.commit()
        finally:
            conn.close()
    before = _state(ns)
    dest = archive / logical.name
    os.rename(logical, dest)
    _sync_both(ns)
    assert _cursor(ns, logical)["container_state"] == "pending"
    _drain(ns)
    cursor = _cursor(ns, logical)
    assert cursor["container_state"] == "accepted", cursor["container_state_reason"]
    assert cursor["container_path"] == str(dest)
    assert _state(ns) == before


def test_verified_zstd_to_plain_archive_adoption_records_path(env):
    """#970 SR-05/G7: the verifier used to write container_path only for zstd,
    erasing a plain archive location; the next pass would then misread the
    archived container's position and re-route it forever."""
    ns, provider, day = env
    archive = provider / "archived_sessions"
    archive.mkdir()
    write_rollout(day / _name(9), token_records(1, session_id="keep"))
    zst = _place(day / _name(1), _quota_records(SHARED), True)
    logical = day / _name(1)
    _sync_both(ns)
    before = _state(ns)
    data = zst.read_bytes()
    backend = rc.resolve_zstd_backend()
    (archive / logical.name).write_bytes(backend.module.decompress(data))
    zst.unlink()
    _sync_both(ns)
    _drain(ns)
    for opener, table in (("open_cache_db", "codex_session_files"),
                          ("open_conversations_db",
                           "codex_conversation_source_files")):
        conn = ns[opener]()
        try:
            row = conn.execute(
                f"SELECT container_kind, container_path, container_state "
                f"FROM {table} WHERE path=?", (str(logical),)).fetchone()
        finally:
            conn.close()
        assert tuple(row) == (rc.PLAIN, str(archive / logical.name), "accepted")
    assert _state(ns) == before
    # The recorded location makes the next pass quiet: no new transition.
    _sync_both(ns)
    assert _cursor(ns, logical)["container_state"] == "accepted"
    assert _state(ns) == before


def test_move_racing_walk_adopts_or_retries(env, monkeypatch):
    """#970 spec 6: a move landing between the walk's selection and its write
    must never record a location the container no longer has (the next pass
    would then route a phantom move); it retries and converges."""
    ns, provider, day = env
    archive = provider / "archived_sessions"
    archive.mkdir()
    write_rollout(day / _name(9), token_records(1, session_id="keep"))
    logical = _place(day / _name(1), _quota_records(SHARED), False)
    _sync_both(ns)
    before, _ = _full_state(ns)
    dest = archive / logical.name
    os.rename(logical, dest)
    cache_mod = sys.modules["_cctally_cache"]
    raced = []

    def move_back(table, path):
        if not raced:
            raced.append(table)
            os.rename(dest, logical)

    monkeypatch.setattr(cache_mod, "_CODEX_LOCATION_ADOPT_HOOK", move_back)
    _sync_both(ns)
    assert raced
    after, locations = _full_state(ns)
    assert after == before
    assert locations["open_cache_db"] == [None, None]   # nothing phantom recorded
    monkeypatch.setattr(cache_mod, "_CODEX_LOCATION_ADOPT_HOOK", None)
    os.rename(logical, dest)
    _sync_both(ns)
    after, locations = _full_state(ns)
    assert after == before
    for opener in ("open_cache_db", "open_conversations_db"):
        assert str(dest) in locations[opener]


@pytest.mark.parametrize("compressed", [False, True], ids=["plain", "zstd"])
def test_unchanged_second_pass_is_quiet(env, monkeypatch, compressed):
    """#970 A8: after an archive move is adopted, the next pass must be
    quiet — no write, no decode — or every hook would redo the move."""
    ns, provider, day = env
    archive = provider / "archived_sessions"
    archive.mkdir()
    write_rollout(day / _name(9), token_records(1, session_id="keep"))
    container = _place(day / _name(1), _quota_records(SHARED), compressed)
    _sync_both(ns)
    os.rename(container, archive / container.name)
    _sync_both(ns)
    adopted = _full_state(ns)
    calls = {"n": 0}
    real = rc.resolve_zstd_backend

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(sys.modules["_lib_codex_rollout_container"],
                        "resolve_zstd_backend", counting)
    _sync_both(ns)
    assert calls["n"] == 0
    assert _full_state(ns) == adopted


# ── #1090: retention-aware legacy equivalence (conversations side) ─────────

#: A cutoff every fixture row precedes (they are stamped 2026-07-20).
RETENTION_CUTOFF = "2026-08-01T00:00:00Z"
RETENTION_NOW = dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.timezone.utc)
RETRY_PREFIX = "codex_legacy_retry:v1:"


def _session(i):
    return f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff1{i:03d}"


def _conv_cursor(ns, path):
    conv = ns["open_conversations_db"]()
    try:
        conv.row_factory = sqlite3.Row
        return dict(conv.execute(
            "SELECT * FROM codex_conversation_source_files WHERE path=?",
            (str(path),)).fetchone())
    finally:
        conv.close()


def _conv_keys(ns, path):
    conv = ns["open_conversations_db"]()
    try:
        return sorted({r[0] for r in conv.execute(
            "SELECT conversation_key FROM codex_conversation_events "
            "WHERE source_path=? AND conversation_key IS NOT NULL",
            (str(path),))})
    finally:
        conv.close()


def _retry_record(ns, path):
    conv = ns["open_conversations_db"]()
    try:
        row = conv.execute("SELECT value FROM cache_meta WHERE key=?",
                           (RETRY_PREFIX + str(path),)).fetchone()
    finally:
        conv.close()
    return None if row is None else json.loads(row[0])


def _retention_delete_key(key, cutoff=RETENTION_CUTOFF):
    """The real paced deletion of one Codex conversation key."""
    import _cctally_core
    retention = importlib.import_module("_lib_conversation_retention")
    conn = retention.open_deletion_connection(_cctally_core.CONVERSATIONS_DB_PATH)
    try:
        result = retention.delete_group(
            conn, retention.ExpiredGroup("codex", False, key),
            now_utc=RETENTION_NOW, cutoff_iso=cutoff)
    finally:
        conn.close()
    assert result.outcome == "ok", result
    return result


def _alter_first_event(ns, path):
    conv = ns["open_conversations_db"]()
    try:
        cur = conv.execute(
            "UPDATE codex_conversation_events SET payload_json=payload_json||' ' "
            "WHERE id=(SELECT MIN(id) FROM codex_conversation_events "
            "WHERE source_path=?)", (str(path),))
        assert cur.rowcount == 1
        conv.commit()
    finally:
        conv.close()


def test_pruned_proven_legacy_cursor_verifies_and_differing_control_stays_rejected(env):
    """#1090 T1 (A1, A2): a legacy conversations cursor whose only key the
    real `delete_group` removed under a cutoff its rows precede verifies,
    while a cursor with one genuinely differing retained event stays
    rejected. Detects the defect: a pruned rollout stays unverified forever,
    and its converse, a retained difference being adopted."""
    ns, _provider, day = env
    pruned, control = day / _name(1), day / _name(2)
    write_rollout(pruned, conversation_records(2, session_id=_session(1), label="p"))
    write_rollout(control, conversation_records(2, session_id=_session(2), label="c"))
    _sync_both(ns)
    _drop_anchors(ns)
    [key] = _conv_keys(ns, pruned)
    _retention_delete_key(key)
    assert _conv_keys(ns, pruned) == []
    _alter_first_event(ns, control)
    _compress(pruned)
    _compress(control)
    _sync_both(ns)
    assert _conv_cursor(ns, pruned)["container_state"] == "pending"
    _drain(ns)
    a, b = _conv_cursor(ns, pruned), _conv_cursor(ns, control)
    assert (a["container_state"], a["container_state_reason"]) == ("accepted", None)
    assert (b["container_state"], b["container_state_reason"]) == (
        "unverified", "legacy-inequivalent")
    assert _conv_keys(ns, pruned) == [], "no row is recreated at adoption"
    assert _retry_record(ns, pruned) is None
    record = _retry_record(ns, control)
    assert record is not None and record["keys"] == _conv_keys(ns, control)
    assert record["fp"] == b["pending_container_fingerprint"]


def _retention_lib():
    return importlib.import_module("_lib_conversation_retention")


def _with_conv(ns, action):
    conv = ns["open_conversations_db"]()
    try:
        result = action(conv)
        conv.commit()
        return result
    finally:
        conv.close()


def _set_proof(ns, cutoff):
    _with_conv(ns, lambda c: _retention_lib()._merge_applied_cutoff(c, cutoff))


def _prune_key(ns, key):
    """Retention's own deletion kernel for one key (unpaced; proof is set
    separately so each case controls it)."""
    _with_conv(ns, lambda c: _retention_lib()._delete_codex_conversation(c, key))


def _prune_null_path(ns, path):
    _with_conv(ns, lambda c: _retention_lib()._delete_codex_null_path(c, str(path)))


def _root_key(ns, path):
    return _conv_cursor(ns, path)["source_root_key"]


FOREIGN = "/elsewhere/rollout-foreign.jsonl"


def _insert_foreign_event(ns, key, root, *, line_offset=0):
    _with_conv(ns, lambda c: c.execute(
        "INSERT INTO codex_conversation_events (source_path, line_offset, "
        "source_root_key, conversation_key, timestamp_utc, record_type, "
        "payload_json) VALUES (?, ?, ?, ?, '2026-07-20T10:00:00Z', "
        "'event_msg', '{}')", (FOREIGN, line_offset, root, key)))


def _insert_foreign_message(ns, key, root):
    _with_conv(ns, lambda c: c.execute(
        "INSERT INTO codex_conversation_messages (conversation_key, "
        "source_root_key, source_path, line_offset, timestamp_utc, kind, "
        "record_family, content_digest, content_len) VALUES "
        "(?, ?, ?, 0, '2026-07-20T10:00:00Z', 'user', 'message', 'd', 0)",
        (key, root, FOREIGN)))


def _delete_foreign(ns):
    _with_conv(ns, lambda c: c.execute(
        "DELETE FROM codex_conversation_events WHERE source_path=?", (FOREIGN,)))


def _newest_stamp(ns, key):
    def read(conn):
        return max(v for (v,) in conn.execute(
            "SELECT MAX(timestamp_utc) FROM codex_conversation_events "
            "WHERE conversation_key=? UNION ALL SELECT MAX(timestamp_utc) "
            "FROM codex_conversation_messages WHERE conversation_key=?",
            (key, key)) if v is not None)
    return _with_conv(ns, read)


def _null_lead():
    """Two identity-less physical events ahead of the first session_meta."""
    return [{"timestamp": "2026-07-20T09:59:5%d.000Z" % i, "type": "event_msg",
             "payload": {"type": "task_started", "seq": i}} for i in (1, 2)]


def _undated(records):
    return [{k: v for k, v in r.items() if k != "timestamp"} for r in records]


def _null_events(ns, path):
    return _with_conv(ns, lambda c: c.execute(
        "SELECT COUNT(*) FROM codex_conversation_events "
        "WHERE conversation_key IS NULL AND source_path=?",
        (str(path),)).fetchone()[0])


#: case -> (files, expected (state, reason)). Files: "target" plus optional
#: "sibling"; each a records list.
def _t2_files(case):
    one = conversation_records(2, session_id=_session(1), label="t")
    two = one + conversation_records(1, session_id=_session(2), label="u")
    if case in ("mixed-differing", "mixed-matching"):
        return {"target": two}
    if case in ("derived-null-message", "null-group-per-path"):
        files = {"target": _null_lead() + one}
        if case == "null-group-per-path":
            files["sibling"] = _null_lead() + conversation_records(
                1, session_id=_session(3), label="s")
        return files
    if case == "sibling-path":
        return {"target": one, "sibling": conversation_records(
            1, session_id=_session(1), label="s")}
    if case == "all-null-timestamps":
        return {"target": _undated(one)}
    if case == "text-order-spelling":
        records = [dict(r) for r in one]
        records[-1]["timestamp"] = "2026-07-20T10:00:14.500Z"
        return {"target": records}
    return {"target": one}


T2_CASES = {
    "mixed-differing": ("unverified", "legacy-inequivalent"),
    "mixed-matching": ("accepted", None),
    "derived-null-message": ("unverified", "legacy-inequivalent"),
    "events-elsewhere": ("unverified", "legacy-inequivalent"),
    "messages-elsewhere": ("unverified", "legacy-inequivalent"),
    "sibling-path": ("unverified", "legacy-inequivalent"),
    "null-group-per-path": ("accepted", None),
    "all-null-timestamps": ("unverified", "legacy-inequivalent"),
    "newest-equals-cutoff": ("unverified", "legacy-inequivalent"),
    "text-order-spelling": ("accepted", None),
    "stamp-only": ("unverified", "legacy-inequivalent"),
    "retention-off-history": ("accepted", None),
}


def _t2_mutate(ns, monkeypatch, case, target, sibling):
    """Retention's effect on the store, the proof, and the case's twist."""
    keys = _conv_keys(ns, target)
    key = keys[0]
    root = _root_key(ns, target)
    proof = RETENTION_CUTOFF
    if case in ("mixed-differing", "mixed-matching"):
        assert len(keys) == 2
        first = min(keys, key=lambda k: _with_conv(ns, lambda c: c.execute(
            "SELECT MIN(line_offset) FROM codex_conversation_events "
            "WHERE conversation_key=?", (k,)).fetchone()[0]))
        _prune_key(ns, first)
        if case == "mixed-differing":
            _alter_first_event(ns, target)
    elif case in ("derived-null-message", "null-group-per-path"):
        assert _null_events(ns, target) == 2
        _prune_null_path(ns, target)
        if case == "null-group-per-path":
            assert _null_events(ns, sibling) == 2
        else:
            cache = sys.modules["_cctally_cache"]
            real = cache._codex_prefix_facts

            def with_null_message(*args, **kwargs):
                facts = real(*args, **kwargs)
                stray = (-1, None) + facts.messages[0][2:]
                return facts._replace(messages=sorted(facts.messages + [stray]))

            monkeypatch.setattr(cache, "_codex_prefix_facts", with_null_message)
    elif case == "sibling-path":
        _with_conv(ns, lambda c: (
            c.execute("DELETE FROM codex_conversation_messages "
                      "WHERE source_path=?", (str(target),)),
            c.execute("DELETE FROM codex_conversation_events "
                      "WHERE source_path=?", (str(target),))))
        assert _conv_keys(ns, sibling) == [key]
    else:
        if case == "newest-equals-cutoff":
            proof = _newest_stamp(ns, key)
        elif case == "text-order-spelling":
            newest = _newest_stamp(ns, key)
            assert newest == "2026-07-20T10:00:14.500000Z", newest
            proof = "2026-07-20T10:00:14Z"   # chronologically earlier
        _prune_key(ns, key)
        if case == "events-elsewhere":
            _insert_foreign_event(ns, key, root)
        elif case == "messages-elsewhere":
            _insert_foreign_message(ns, key, root)
    if case == "stamp-only":
        _with_conv(ns, lambda c: _retention_lib()._stamp_retention_prune(
            c, RETENTION_NOW))
    else:
        _set_proof(ns, proof)
    if case == "retention-off-history":
        import _cctally_core
        import _retention_fixtures as rfx
        rfx.write_retention(pathlib.Path(_cctally_core.APP_DIR), 0)


@pytest.mark.parametrize("case", sorted(T2_CASES))
def test_retention_absent_group_boundaries(env, monkeypatch, case):
    """#1090 T2: one builder, one case per §6 T2 bullet. Detects false
    acceptance (a group present anywhere, an unproven or undated group, a
    NULL group with a derived NULL-key message) and the likely false
    rejection (a mixed file whose exempt group sits beside a matching
    retained one; SQLite text order of timestamp spellings; proof kept with
    retention off)."""
    ns, _provider, day = env
    files = _t2_files(case)
    target = day / _name(1)
    sibling = day / _name(2) if "sibling" in files else None
    write_rollout(target, files["target"])
    if sibling is not None:
        write_rollout(sibling, files["sibling"])
    _sync_both(ns)
    _drop_anchors(ns)
    _compress(target)
    _sync_both(ns)
    assert _conv_cursor(ns, target)["container_state"] == "pending"
    derived_keys = _conv_keys(ns, target)
    _t2_mutate(ns, monkeypatch, case, target, sibling)
    _drain(ns)
    cursor = _conv_cursor(ns, target)
    assert (cursor["container_state"],
            cursor["container_state_reason"]) == T2_CASES[case], case
    record = _retry_record(ns, target)
    if T2_CASES[case][0] == "accepted":
        assert record is None
        return
    assert record["fp"] == cursor["pending_container_fingerprint"]
    if case == "all-null-timestamps":
        assert record["permanent"] is True, record
    if case in ("newest-equals-cutoff", "stamp-only"):
        assert record["permanent"] is False
        assert record["threshold"] == "2026-07-20T10:00:14Z", record
    if case in ("events-elsewhere", "messages-elsewhere", "sibling-path"):
        # The group still has rows elsewhere: its deletion could matter.
        assert record["keys"] == derived_keys, record
        assert record["threshold"] is None and record["permanent"] is False
    if case == "mixed-differing":
        assert record["keys"] == _conv_keys(ns, target), record
    if case == "derived-null-message":
        # The NULL group can never qualify; the retained key stays a
        # dependency and the path no longer holds NULL-key events.
        assert record["permanent"] is True and record["nullGroup"] is False
        assert record["keys"] == derived_keys, record


def _legacy_single(env, *, label="t"):
    ns, _provider, day = env
    path = day / _name(1)
    write_rollout(path, conversation_records(2, session_id=_session(1),
                                             label=label))
    _sync_both(ns)
    _drop_anchors(ns)
    _compress(path)
    _sync_both(ns)
    assert _conv_cursor(ns, path)["container_state"] == "pending"
    [key] = _conv_keys(ns, path)
    return ns, path, key


@pytest.mark.parametrize("rows", ["retained", "absent"])
def test_watermark_only_resume_difference_adopts_and_keeps_stored_watermark(
        env, rows):
    """#1090 T3 (Q3): a conversations-side legacy cursor whose resume tuple
    differs only in `last_total_tokens` adopts, with its rows retained and
    with them retention-absent, and the stored watermark survives adoption.
    Detects a lost resume field and a rewritten watermark."""
    ns, path, key = _legacy_single(env)
    _with_conv(ns, lambda c: c.execute(
        "UPDATE codex_conversation_source_files SET last_total_tokens=? "
        "WHERE path=?", (4_843_451, str(path))))
    if rows == "absent":
        _prune_key(ns, key)
        _set_proof(ns, RETENTION_CUTOFF)
    _drain(ns)
    cursor = _conv_cursor(ns, path)
    assert (cursor["container_state"], cursor["container_state_reason"]) == (
        "accepted", None)
    assert cursor["last_total_tokens"] == 4_843_451
    assert _retry_record(ns, path) is None


def test_turn_resume_difference_stays_rejected_with_permanent_record(env):
    """#1090 T3: a turn difference is a compared resume field, so the row
    stays `unverified/legacy-inequivalent` with a permanent record that no
    retention transition re-arms. Detects a dropped turn comparison."""
    ns, path, key = _legacy_single(env)
    _with_conv(ns, lambda c: c.execute(
        "UPDATE codex_conversation_source_files SET last_turn_id=? "
        "WHERE path=?", ("different-turn", str(path))))
    _set_proof(ns, RETENTION_CUTOFF)
    _drain(ns)
    cursor = _conv_cursor(ns, path)
    assert (cursor["container_state"], cursor["container_state_reason"]) == (
        "unverified", "legacy-inequivalent")
    assert cursor["last_turn_id"] == "different-turn"
    record = _retry_record(ns, path)
    assert record == {"fp": cursor["pending_container_fingerprint"],
                      "permanent": True, "cacheHeld": False, "keys": [key],
                      "nullGroup": False, "threshold": None}


def _race_seam(monkeypatch, mutate):
    """Run `mutate(n)` after the n-th conversations-side preparation."""
    cache = sys.modules["_cctally_cache"]
    real = cache._verify_codex_container
    calls = []

    def seam(conn, table, snap, selected):
        verdict = real(conn, table, snap, selected)
        if table == "codex_conversation_source_files":
            calls.append(verdict.outcome)
            mutate(len(calls))
        return verdict

    monkeypatch.setattr(cache, "_verify_codex_container", seam)
    return calls


def test_deletion_between_prepare_and_apply_ends_in_adoption(env, monkeypatch):
    """#1090 T5 race (§5.5): a deletion that lands after preparation turns
    the prepared rejection into equality; application sees the change,
    writes nothing and re-prepares, and the row adopts. Detects a stale
    rejection committed against a changed store."""
    ns, path, key = _legacy_single(env)
    _set_proof(ns, RETENTION_CUTOFF)
    _alter_first_event(ns, path)

    def mutate(n):
        if n == 1:
            _prune_key(ns, key)

    calls = _race_seam(monkeypatch, mutate)
    result = _drain(ns)
    assert calls == ["unverified", "adopt"]
    cursor = _conv_cursor(ns, path)
    assert (cursor["container_state"], cursor["container_state_reason"]) == (
        "accepted", None)
    assert result.retried >= 1 and _retry_record(ns, path) is None


def test_sibling_insertion_between_prepare_and_apply_ends_in_rejection_naming_key(
        env, monkeypatch):
    """#1090 T5 race (§5.5): rows for an exempt key inserted on a sibling
    path after preparation turn equality into rejection; the row ends
    rejected, and its record names that key. Detects an adoption committed
    while the exempt group was reappearing."""
    ns, path, key = _legacy_single(env)
    root = _root_key(ns, path)
    _prune_key(ns, key)
    _set_proof(ns, RETENTION_CUTOFF)

    def mutate(n):
        if n == 1:
            _insert_foreign_event(ns, key, root)

    calls = _race_seam(monkeypatch, mutate)
    _drain(ns)
    assert calls == ["adopt", "unverified"]
    cursor = _conv_cursor(ns, path)
    assert (cursor["container_state"], cursor["container_state_reason"]) == (
        "unverified", "legacy-inequivalent")
    record = _retry_record(ns, path)
    assert record["keys"] == [key] and record["permanent"] is False


def test_three_changes_leave_row_pending_and_drain_completes(env, monkeypatch):
    """#1090 T5 race (§5.5): a writer that changes the result after every
    preparation costs at most two retries; the third change leaves the row
    `pending` and the drain still completes. Detects an unbounded retry
    loop that would stall the drain."""
    ns, path, key = _legacy_single(env)
    root = _root_key(ns, path)
    _prune_key(ns, key)
    _set_proof(ns, RETENTION_CUTOFF)

    def mutate(n):
        if n % 2:
            _insert_foreign_event(ns, key, root)
        else:
            _delete_foreign(ns)

    calls = _race_seam(monkeypatch, mutate)
    result = _drain(ns)
    assert calls == ["adopt", "unverified", "adopt"]
    assert result.outcome == "drained"
    assert result.pending >= 1
    cursor = _conv_cursor(ns, path)
    assert cursor["container_state"] == "pending"
    assert _retry_record(ns, path) is None


# ── #1090 T5: the re-arm evaluator and its two entrances (§5.4) ────────────

ENTRANCES = ["pass", "drain"]
REARM = "_reevaluate_codex_legacy_rejections"


def _rearm_spy(monkeypatch):
    """Record every evaluator call: (re-armed, writes it made, rows after).

    `writes` is the connection's `total_changes` delta across the call, so an
    unchanged state is observable at either entrance."""
    cache = sys.modules["_cctally_cache"]
    real = getattr(cache, REARM, None)
    calls = []
    if real is None:                       # RED on a tree without it
        return calls

    def spy(conn):
        before = conn.total_changes
        rearmed = real(conn)
        rows = {r[0]: tuple(r[1:]) for r in conn.execute(
            "SELECT path, container_state, container_state_reason, "
            "pending_container_fingerprint FROM codex_conversation_source_files")}
        calls.append((rearmed, conn.total_changes - before, rows))
        return rearmed

    monkeypatch.setattr(cache, REARM, spy)
    return calls


def _enter(ns, entrance):
    """One evaluator entrance: a full conversation pass, or a drain run."""
    if entrance == "pass":
        conv = ns["open_conversations_db"]()
        try:
            return ns["sync_codex_conversations"](conv, maintenance="caller")
        finally:
            conv.close()
    return _drain(ns)


def _state_of(ns, path):
    cursor = _conv_cursor(ns, path)
    return (cursor["container_state"], cursor["container_state_reason"])


def _assert_rearms_and_adopts(ns, entrance, calls, path, fp):
    """The entrance re-arms ``path`` keeping ``fp``; the drain then adopts."""
    result = _enter(ns, entrance)
    rearmed = [rows[str(path)] for n, _w, rows in calls if n]
    assert rearmed and rearmed[0] == ("pending", "legacy-reverify", fp), calls
    if entrance == "pass":
        assert _state_of(ns, path) == ("pending", "legacy-reverify")
        assert _conv_cursor(ns, path)["pending_container_fingerprint"] == fp
        assert "_codex-replay-drain" in result.drain_requested
        _drain(ns)
    assert _state_of(ns, path) == ("accepted", None)
    assert _retry_record(ns, path) is None


def _proof(ns):
    return _with_conv(ns, lambda c: _retention_lib().retention_cutoff_proof(c))


def _reject_directly(ns, path, *, reason="legacy-inequivalent", fp=None):
    """A rejection as an older binary writes it: no retry record."""
    def write(conn):
        if fp is None:
            conn.execute(
                "UPDATE codex_conversation_source_files SET "
                "container_state='unverified', container_state_reason=? "
                "WHERE path=?", (reason, str(path)))
        else:
            conn.execute(
                "UPDATE codex_conversation_source_files SET "
                "container_state='unverified', container_state_reason=?, "
                "pending_container_fingerprint=? WHERE path=?",
                (reason, fp, str(path)))
    _with_conv(ns, write)
    assert _retry_record(ns, path) is None


def _sibling_dependency(env):
    """A legacy target whose only key K still has rows on a sibling path S:
    rejected, with a non-permanent record whose dependency is K."""
    ns, _provider, day = env
    target, sibling = day / _name(1), day / _name(2)
    write_rollout(target, conversation_records(2, session_id=_session(1),
                                               label="t"))
    write_rollout(sibling, conversation_records(2, session_id=_session(1),
                                                label="s"))
    _sync_both(ns)
    _drop_anchors(ns)
    _compress(target)
    _sync_both(ns)
    [key] = _conv_keys(ns, target)
    assert _conv_keys(ns, sibling) == [key]
    _with_conv(ns, lambda c: (
        c.execute("DELETE FROM codex_conversation_messages WHERE source_path=?",
                  (str(target),)),
        c.execute("DELETE FROM codex_conversation_events WHERE source_path=?",
                  (str(target),))))
    _set_proof(ns, RETENTION_CUTOFF)
    _drain(ns)
    assert _state_of(ns, target) == ("unverified", "legacy-inequivalent")
    record = _retry_record(ns, target)
    assert record["keys"] == [key] and record["permanent"] is False, record
    assert record["threshold"] is None and record["cacheHeld"] is False
    return ns, target, sibling, key


def _leave_population(ns, key):
    """Take ``key`` out of the publication barrier's population (a
    conversation outside it keeps the ordinary walk, #968 residual R4)."""
    conn = ns["open_cache_db"]()
    try:
        for table in ("codex_barrier_population", "codex_barrier_certificates",
                      "codex_barrier_repairs", "codex_barrier_entry_threads"):
            conn.execute(f"DELETE FROM {table} WHERE conversation_key=?", (key,))
        conn.commit()
    finally:
        conn.close()


def _legacy_files(env, count):
    ns, _provider, day = env
    paths = [day / _name(i) for i in range(1, count + 1)]
    for i, path in enumerate(paths, start=1):
        write_rollout(path, conversation_records(2, session_id=_session(i),
                                                 label=f"f{i}"))
    _sync_both(ns)
    _drop_anchors(ns)
    for path in paths:
        _compress(path)
    _sync_both(ns)
    keys = []
    for path in paths:
        assert _state_of(ns, path)[0] == "pending"
        [key] = _conv_keys(ns, path)
        keys.append(key)
    return ns, paths, keys


@pytest.mark.parametrize("entrance", ENTRANCES)
def test_bootstrap_rearms_recordless_prefix_rejection(env, monkeypatch, entrance):
    """#1090 T5 bootstrap (§5.4 (a), §5.6): a pre-fix `legacy-inequivalent`
    row with no retry record, on a store with proof, is re-armed at the next
    entrance with its fingerprint kept, and drains to adoption. Detects the
    pre-fix backlog staying terminal after the upgrade (no migration runs)."""
    ns, [path], [key] = _legacy_files(env, 1)
    _prune_key(ns, key)
    _set_proof(ns, RETENTION_CUTOFF)
    _reject_directly(ns, path)
    fp = _conv_cursor(ns, path)["pending_container_fingerprint"]
    assert fp is not None
    calls = _rearm_spy(monkeypatch)
    _assert_rearms_and_adopts(ns, entrance, calls, path, fp)
    assert _conv_keys(ns, path) == []


@pytest.mark.parametrize("entrance", ENTRANCES)
def test_compressed_rejection_is_not_rearmed_while_the_decoder_is_unavailable(
        env, monkeypatch, entrance):
    """#1090 T5 (§5.4): a rejected compressed row whose trigger fired is left
    `unverified` while no zstd decoder is available, with no writes, and is
    re-armed and adopted once the decoder returns. Re-arming it then would
    leave a `pending` row the drain can never verify: verification debt and a
    drain request on every pass, where `main` keeps the row quiet."""
    ns, [path], [key] = _legacy_files(env, 1)
    _prune_key(ns, key)
    _set_proof(ns, RETENTION_CUTOFF)
    _reject_directly(ns, path)
    fp = _conv_cursor(ns, path)["pending_container_fingerprint"]
    assert rc.ContainerFingerprint.from_json(fp).kind == rc.ZSTD
    before = _conv_cursor(ns, path)
    backend = rc.resolve_zstd_backend
    monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    calls = _rearm_spy(monkeypatch)
    _enter(ns, entrance)
    assert calls and all(n == 0 and w == 0 for n, w, _rows in calls), calls
    assert _conv_cursor(ns, path) == before
    monkeypatch.setattr(rc, "resolve_zstd_backend", backend)
    calls.clear()
    _assert_rearms_and_adopts(ns, entrance, calls, path, fp)


def _stamp_prune(ns):
    """Retention's own history stamp, as its visit writes it after a prune."""
    _with_conv(ns, lambda c: _retention_lib()._stamp_retention_prune(
        c, RETENTION_NOW))


def _prune_stamp(ns):
    rows = _with_conv(ns, lambda c: c.execute(
        "SELECT value FROM cache_meta WHERE key=?",
        (_retention_lib()._RETENTION_LAST_PRUNE_KEY,)).fetchall())
    return rows[0][0] if rows else None


@pytest.mark.parametrize("entrance", ENTRANCES)
def test_recordless_rejection_on_a_never_pruned_store_rearms_without_proof(
        env, monkeypatch, entrance):
    """#1090 T5 (§5.4 (a)): a store with no retention history (no prune
    stamp) and no proof cannot owe a rejection to retention, so a recordless
    pre-fix rejection — here a watermark-only resume difference, which Q3 now
    accepts — is re-armed at the next entrance and adopts, keeping its stored
    watermark. Detects a never-pruned store's backlog staying terminal."""
    ns, path, _key = _legacy_single(env)
    _with_conv(ns, lambda c: c.execute(
        "UPDATE codex_conversation_source_files SET last_total_tokens=? "
        "WHERE path=?", (4_843_451, str(path))))
    _reject_directly(ns, path)
    assert _proof(ns) is None and _prune_stamp(ns) is None
    fp = _conv_cursor(ns, path)["pending_container_fingerprint"]
    assert fp is not None
    calls = _rearm_spy(monkeypatch)
    _assert_rearms_and_adopts(ns, entrance, calls, path, fp)
    assert _conv_cursor(ns, path)["last_total_tokens"] == 4_843_451


@pytest.mark.parametrize("entrance", ENTRANCES)
def test_recordless_rejection_on_a_pruned_store_waits_for_proof(
        env, monkeypatch, entrance):
    """#1090 T5 (§5.4 (a)): on a store that has pruned (a prune stamp) but
    holds no proof yet, a recordless rejection is left as it is, with no
    writes, and is re-armed once an applied cutoff is merged. Detects a
    pruned store re-verifying its backlog twice (before and after proof)."""
    ns, path, key = _legacy_single(env)
    _prune_key(ns, key)
    _stamp_prune(ns)
    _reject_directly(ns, path)
    assert _proof(ns) is None and _prune_stamp(ns) is not None
    before = _conv_cursor(ns, path)
    calls = _rearm_spy(monkeypatch)
    _enter(ns, entrance)
    assert calls and all(n == 0 and w == 0 for n, w, _rows in calls), calls
    assert _conv_cursor(ns, path) == before
    assert _retry_record(ns, path) is None
    _set_proof(ns, RETENTION_CUTOFF)
    calls.clear()
    _assert_rearms_and_adopts(ns, entrance, calls, path,
                              before["pending_container_fingerprint"])
    assert _conv_keys(ns, path) == []


@pytest.mark.parametrize("entrance", ENTRANCES)
@pytest.mark.parametrize("writer", ["delete_group", "orphan_prune",
                                    "sibling_reset"])
def test_dependency_deletion_by_any_writer_rearms(env, monkeypatch, entrance,
                                                  writer):
    """#1090 T5 dependency deletion (§5.4 (b)): the rejected row's dependency
    key loses its last rows elsewhere — by retention's `delete_group`, by the
    orphan-prune worker deleting the sibling path, or by the sibling's file
    reset — with no change to the retention proof; the row is re-armed at the
    next entrance and adopts. Detects a missed retry when a writer other than
    retention removes the blocking rows."""
    ns, target, sibling, key = _sibling_dependency(env)
    proof = _proof(ns)
    fp = _conv_cursor(ns, target)["pending_container_fingerprint"]
    if writer == "delete_group":
        _retention_delete_key(key)
    elif writer == "orphan_prune":
        sibling.unlink()
        _sync_both(ns)
        cache = sys.modules["_cctally_cache"]
        cache.run_codex_prune_drain(wait_for_requests=False)
        result = cache.run_codex_conversation_prune_drain(wait_for_requests=False)
        assert result.identities_deleted == 1, result
    else:
        # Inside the barrier population the walk defers this reset for as
        # long as K's other contributor (the target) is terminal (#1052), so
        # the reset writer can only remove a key outside the population.
        _leave_population(ns, key)
        size = sibling.stat().st_size
        write_rollout(sibling, conversation_records(1, session_id=_session(9),
                                                    label="z"))
        assert sibling.stat().st_size < size
        _sync_both(ns)
    assert _with_conv(ns, lambda c: c.execute(
        "SELECT (SELECT COUNT(*) FROM codex_conversation_events "
        "WHERE conversation_key=?) + (SELECT COUNT(*) FROM "
        "codex_conversation_messages WHERE conversation_key=?)",
        (key, key)).fetchone()[0]) == 0
    assert _proof(ns) == proof, "the writer changed no retention evidence"
    assert _state_of(ns, target) == ("unverified", "legacy-inequivalent")
    calls = _rearm_spy(monkeypatch)
    _assert_rearms_and_adopts(ns, entrance, calls, target, fp)


@pytest.mark.parametrize("entrance", ENTRANCES)
def test_retained_only_key_deletion_rearms(env, monkeypatch, entrance):
    """#1090 T5 (§5.3 `keys`): a key the target path retains but the rollout
    never derives blocks equivalence until it is deleted; its deletion
    re-arms the row, which then adopts. Detects a record that names only
    derived keys."""
    ns, [path], [key] = _legacy_files(env, 1)
    root = _root_key(ns, path)
    stray = "codex:stray-retained-key"
    _with_conv(ns, lambda c: c.execute(
        "INSERT INTO codex_conversation_events (source_path, line_offset, "
        "source_root_key, conversation_key, timestamp_utc, record_type, "
        "payload_json) VALUES (?, 999999, ?, ?, '2026-07-20T10:00:00Z', "
        "'event_msg', '{}')", (str(path), root, stray)))
    _set_proof(ns, RETENTION_CUTOFF)
    _drain(ns)
    assert _state_of(ns, path) == ("unverified", "legacy-inequivalent")
    record = _retry_record(ns, path)
    assert record["keys"] == sorted([key, stray]), record
    fp = _conv_cursor(ns, path)["pending_container_fingerprint"]
    _retention_delete_key(stray)
    calls = _rearm_spy(monkeypatch)
    _assert_rearms_and_adopts(ns, entrance, calls, path, fp)
    assert _conv_keys(ns, path) == [key], "the retained key is untouched"


@pytest.mark.parametrize("entrance", ENTRANCES)
@pytest.mark.parametrize("retention", ["on", "off"])
def test_downgrade_rejection_is_rearmed_at_the_next_entrance(
        env, monkeypatch, entrance, retention):
    """#1090 T5 downgrade (§5.4 (a), no token): after the store has been
    evaluated once, a recordless rejection written as an older binary writes
    it is re-armed at the next entrance when proof exists — with retention on,
    and with retention turned off and only its historical proof. Detects a
    one-time marker or token that would hide a later rejection."""
    ns, [path], [key] = _legacy_files(env, 1)
    _prune_key(ns, key)
    _set_proof(ns, RETENTION_CUTOFF)
    cache = sys.modules["_cctally_cache"]
    assert _with_conv(ns, cache._reevaluate_codex_legacy_rejections) == 0
    _reject_directly(ns, path)
    if retention == "off":
        import _cctally_core
        import _retention_fixtures as rfx
        rfx.write_retention(pathlib.Path(_cctally_core.APP_DIR), 0)
    fp = _conv_cursor(ns, path)["pending_container_fingerprint"]
    calls = _rearm_spy(monkeypatch)
    _assert_rearms_and_adopts(ns, entrance, calls, path, fp)


@pytest.mark.parametrize("entrance", ENTRANCES)
@pytest.mark.parametrize("permanent", [False, True],
                         ids=["threshold", "permanent"])
def test_threshold_crossing_rearms_and_permanent_never_does(
        env, monkeypatch, entrance, permanent):
    """#1090 T5 threshold (§5.4 (c)): an absent group whose newest stamp
    equals the proof is rejected with that stamp as its threshold; once the
    proof passes it, the row re-arms and adopts. A permanent record (here a
    turn difference) whose threshold the proof has passed is never re-armed.
    Detects a missed retry on cutoff advance, and an endless retry of a row
    no retention transition can fix."""
    ns, [path], [key] = _legacy_files(env, 1)
    newest = _newest_stamp(ns, key)
    _prune_key(ns, key)
    _set_proof(ns, newest)
    if permanent:
        _with_conv(ns, lambda c: c.execute(
            "UPDATE codex_conversation_source_files SET last_turn_id=? "
            "WHERE path=?", ("different-turn", str(path))))
    _drain(ns)
    assert _state_of(ns, path) == ("unverified", "legacy-inequivalent")
    record = _retry_record(ns, path)
    assert record["threshold"] == newest and record["permanent"] is permanent
    fp = _conv_cursor(ns, path)["pending_container_fingerprint"]
    _set_proof(ns, RETENTION_CUTOFF)
    calls = _rearm_spy(monkeypatch)
    if not permanent:
        _assert_rearms_and_adopts(ns, entrance, calls, path, fp)
        return
    _enter(ns, entrance)
    assert calls and all(n == 0 and w == 0 for n, w, _rows in calls), calls
    assert _state_of(ns, path) == ("unverified", "legacy-inequivalent")
    assert _retry_record(ns, path) == record


@pytest.mark.parametrize("entrance", ENTRANCES)
def test_unchanged_state_writes_nothing(env, monkeypatch, entrance):
    """#1090 T5 (§5.4 invariants): with a valid non-permanent record whose
    dependency still has rows and no other trigger, the evaluator writes
    nothing (`total_changes` unchanged) and the row stays rejected. Detects
    a retry without relevant progress, or a write on every pass."""
    ns, target, _sibling, _key = _sibling_dependency(env)
    record = _retry_record(ns, target)
    calls = _rearm_spy(monkeypatch)
    _enter(ns, entrance)
    assert calls and all(n == 0 and w == 0 for n, w, _rows in calls), calls
    assert _state_of(ns, target) == ("unverified", "legacy-inequivalent")
    assert _retry_record(ns, target) == record


@pytest.mark.parametrize("entrance", ENTRANCES)
def test_failed_rearm_transaction_rolls_back_and_retries(env, monkeypatch,
                                                         entrance):
    """#1090 T5 rollback (§5.4 writes): an error inside the evaluator's write
    transaction leaves every row `unverified`, and the next entrance re-arms
    them all. Detects a partial re-arm, or evaluator state that suppresses
    the retry."""
    ns, paths, keys = _legacy_files(env, 2)
    for path, key in zip(paths, keys):
        _prune_key(ns, key)
        _reject_directly(ns, path)
    _set_proof(ns, RETENTION_CUTOFF)
    cache = sys.modules["_cctally_cache"]
    raised = []

    def fail(*args):
        raised.append(args)
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(cache, "_CODEX_LEGACY_REARM_PRECOMMIT_HOOK", fail,
                        raising=False)
    calls = _rearm_spy(monkeypatch)
    _enter(ns, entrance)
    assert raised, "the injected failure ran inside the write transaction"
    assert calls and all(n == 0 for n, _w, _rows in calls), calls
    for path in paths:
        assert _state_of(ns, path) == ("unverified", "legacy-inequivalent")
    monkeypatch.setattr(cache, "_CODEX_LEGACY_REARM_PRECOMMIT_HOOK", None)
    calls.clear()
    _enter(ns, entrance)
    assert calls and calls[0][0] == 2, calls
    if entrance == "pass":
        _drain(ns)
    for path in paths:
        assert _state_of(ns, path) == ("accepted", None)


@pytest.mark.parametrize("entrance", ENTRANCES)
def test_cache_side_and_boundary_rows_are_never_rearmed(env, monkeypatch,
                                                        entrance):
    """#1090 T5 (Q4): with proof present and no record, a conversations
    `legacy-boundary` row and a cache-side `unverified` row are left exactly
    as they are. Detects a re-arm outside the reasons the record covers."""
    ns, [boundary, cache_row], keys = _legacy_files(env, 2)
    _prune_key(ns, keys[0])
    _set_proof(ns, RETENTION_CUTOFF)
    _reject_directly(ns, boundary, reason="legacy-boundary")
    _drain(ns)        # the cache side settles; boundary stays as written
    cache_conn = ns["open_cache_db"]()
    try:
        cache_conn.execute(
            "UPDATE codex_session_files SET container_state='unverified', "
            "container_state_reason='legacy-inequivalent', "
            "pending_container_fingerprint='{}' WHERE path=?", (str(cache_row),))
        cache_conn.commit()
    finally:
        cache_conn.close()
    before_conv = _conv_cursor(ns, boundary)
    before_cache = _cursor(ns, cache_row)
    calls = _rearm_spy(monkeypatch)
    _enter(ns, entrance)
    assert calls and all(n == 0 for n, _w, _rows in calls), calls
    assert _conv_cursor(ns, boundary) == before_conv
    assert _cursor(ns, cache_row) == before_cache


def test_pass_entrance_requests_the_drain_for_an_unwalked_rearmed_row(
        env, monkeypatch):
    """#1090 T5 (§5.4 entrance 1): the full pass's own re-arm requests the
    verification drain even when its walk never meets the re-armed row (here
    the file is left out of this pass's discovery, present on disk), so no
    `verification-pending` skip asks for it. Detects a re-armed row left
    `pending` with no drain run to verify it."""
    ns, [path], [key] = _legacy_files(env, 1)
    _prune_key(ns, key)
    _set_proof(ns, RETENTION_CUTOFF)
    _reject_directly(ns, path)
    cache = sys.modules["_cctally_cache"]
    real = cache._discover_codex_files_with_roots

    def discover(*args, **kwargs):
        return [f for f in real(*args, **kwargs)
                if str(f.source_path) != str(path)]

    monkeypatch.setattr(cache, "_discover_codex_files_with_roots", discover)
    result = _enter(ns, "pass")
    assert _state_of(ns, path) == ("pending", "legacy-reverify")
    assert not result.container_skipped.get("verification-pending")
    assert not result.container_pending
    assert "_codex-replay-drain" in result.drain_requested, result


def test_pass_keeps_a_prune_drain_request_beside_pending_verification(env):
    """#1090 F3: a full pass that records prune debt (a retained file is gone)
    and also skips a file as `verification-pending` requests both drains, the
    request order stable. Detects the verification request overwriting the
    prune drain's, which leaves the orphan rows undeleted for a pass."""
    ns, [pending, gone], _keys = _legacy_files(env, 2)
    gone.with_name(gone.name + ".zst").unlink()
    result = _enter(ns, "pass")
    assert result.container_skipped.get("verification-pending") or (
        result.container_pending), result
    assert result.prune_debt_pending, result
    assert result.drain_requested == tuple(sorted(
        {"_codex-replay-drain", "_codex-prune-drain"})), result
    assert _state_of(ns, pending)[0] == "pending"


# ── #1090 T5: the cache-held gate (§5.2, revision 3) ───────────────────────

#: An equal-length accounting mutation the cache side's legacy check rejects
#: (it compares output tokens); the conversations side's exempt group hides
#: it, so only the cache cursor ends `unverified`.
CACHE_MUTATION = (b'"output_tokens":40', b'"output_tokens":41')
TORN = b'{"timestamp":"2026-07-20T10:00:30.000Z","type":"event_msg"'


def _lines(records):
    return b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n"
                    for r in records)


def _replace_zst(zpath, data):
    """A new container at the same name with a new inode."""
    tmp = zpath.with_name("replacement.tmp")
    tmp.write_bytes(zstd_bytes(data))
    os.replace(tmp, zpath)


def _sync_cache(ns):
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()


def _held_compressed(env, *, suffix, cache_rejects, torn_tail=b""):
    """A legacy conversations cursor whose only key retention removed (proof
    set), moved into a compressed container of ``prefix + suffix``; the
    cache side rejects the prefix when ``cache_rejects``. ``torn_tail`` is a
    torn final line the original plain file ended in (the cursor's
    `size_bytes` then lies past its `last_byte_offset`)."""
    ns, _provider, day = env
    path = day / _name(1)
    records = conversation_records(3, session_id=_session(1), label="g")
    write_rollout(path, records[:-4])
    good = path.read_bytes()
    if torn_tail:
        with path.open("ab") as fh:
            fh.write(torn_tail)
    _sync_both(ns)
    _drop_anchors(ns)
    [key] = _conv_keys(ns, path)
    _prune_key(ns, key)
    _set_proof(ns, RETENTION_CUTOFF)
    extra = {"lines": _lines(records[-4:]), "torn": TORN,
             "none": b""}[suffix] if isinstance(suffix, str) else suffix
    prefix = good.replace(*CACHE_MUTATION, 1) if cache_rejects else good
    zpath = path.with_name(path.name + ".zst")
    zpath.write_bytes(zstd_bytes(prefix + extra))
    path.unlink()
    _sync_both(ns)
    assert _state_of(ns, path)[0] == "pending"
    return ns, path, zpath, key, good, extra


def _cache_state(ns, path):
    row = _cursor(ns, path)
    return row["container_state"]


@pytest.mark.parametrize("entrance", ENTRANCES)
@pytest.mark.parametrize("suffix", ["lines", "torn"])
def test_compressed_adoption_is_held_until_its_cache_cursor_is_accepted(
        env, monkeypatch, suffix, entrance):
    """#1090 T5 gate (§5.2 R1-P1-1, R2-P1-1): a compressed legacy container
    that adopts only by the new rules and still has unread bytes (complete
    lines, or a growing torn-only suffix) is held as `legacy-cache-held`
    while its cache cursor is `unverified`; once the cache cursor is
    `accepted`, the next entrance re-arms it by (d) and it adopts. Detects
    the adoption whose deferral the barrier reports `uncovered` forever."""
    ns, path, zpath, _key, good, extra = _held_compressed(
        env, suffix=suffix, cache_rejects=True)
    _drain(ns)
    assert _cache_state(ns, path) == "unverified"
    assert _state_of(ns, path) == ("unverified", "legacy-cache-held")
    cursor = _conv_cursor(ns, path)
    if suffix == "torn":
        assert cursor["size_bytes"] == len(good)   # decoded length above it
    record = _retry_record(ns, path)
    assert record["cacheHeld"] is True, record
    assert record["fp"] == cursor["pending_container_fingerprint"]
    fp = cursor["pending_container_fingerprint"]
    _drain(ns)                                    # still held: no progress
    assert _state_of(ns, path) == ("unverified", "legacy-cache-held")
    _replace_zst(zpath, good + extra)             # the cache side recovers
    _sync_cache(ns)
    cache = sys.modules["_cctally_cache"]
    if entrance == "pass":
        real = cache._reevaluate_codex_legacy_rejections
        monkeypatch.setattr(cache, REARM, lambda conn: 0)
        _drain(ns)                                # the cache side settles
        monkeypatch.setattr(cache, REARM, real)
    else:
        calls = _rearm_spy(monkeypatch)
    if entrance == "pass":
        calls = _rearm_spy(monkeypatch)
        result = _enter(ns, "pass")
        assert "_codex-replay-drain" in result.drain_requested
        assert _cache_state(ns, path) == "accepted"
        _drain(ns)
    else:
        _drain(ns)
        assert _cache_state(ns, path) == "accepted"
    assert calls and calls[0][0] == 1 and calls[0][2][str(path)] == (
        "pending", "legacy-reverify", fp), calls
    assert _state_of(ns, path) == ("accepted", None)
    assert _retry_record(ns, path) is None


@pytest.mark.parametrize("case", ["accepted-cache", "reconcile-only"])
def test_compressed_adoption_the_gate_leaves_alone(env, case):
    """#1090 T5 gate controls: the same compressed file adopts directly when
    its cache cursor is `accepted`, and a reconcile-only verdict (no unread
    complete line, decoded length below the old scan target) adopts even
    against an `unverified` cache cursor. Detects an over-broad hold."""
    if case == "accepted-cache":
        ns, path, _z, _k, _g, _e = _held_compressed(
            env, suffix="lines", cache_rejects=False)
    else:
        ns, path, _z, _k, good, _e = _held_compressed(
            env, suffix=TORN[:7], cache_rejects=True, torn_tail=TORN)
        assert _conv_cursor(ns, path)["size_bytes"] == len(good) + len(TORN)
    _drain(ns)
    assert _cache_state(ns, path) == (
        "accepted" if case == "accepted-cache" else "unverified")
    assert _state_of(ns, path) == ("accepted", None)
    assert _retry_record(ns, path) is None


def _population(ns):
    conn = ns["open_cache_db"]()
    try:
        return {r[0] for r in conn.execute(
            "SELECT conversation_key FROM codex_barrier_population")}
    finally:
        conn.close()


def _plain_with_suffix(env, *, suffix, cache_rejects):
    """A legacy conversations cursor that last read a compressed container
    and now finds the plain rollout with ``suffix`` appended (a kind change,
    so verification runs); its only key retention removed (proof set)."""
    ns, _provider, day = env
    path = day / _name(1)
    write_rollout(path, conversation_records(2, session_id=_session(1),
                                             label="p"))
    good = path.read_bytes()
    _compress(path)
    _sync_both(ns)
    _drain(ns)
    assert _state_of(ns, path) == ("accepted", None)
    _drop_anchors(ns)
    [key] = _conv_keys(ns, path)
    _prune_key(ns, key)
    _set_proof(ns, RETENTION_CUTOFF)
    path.with_name(path.name + ".zst").unlink()
    prefix = good.replace(*CACHE_MUTATION, 1) if cache_rejects else good
    path.write_bytes(prefix + suffix)
    _sync_both(ns)
    assert _state_of(ns, path)[0] == "pending"
    return ns, path, key, good


def _other_population_key(ns, day):
    """A key genuinely in the population: an unrelated compressed rollout."""
    other = day / _name(5)
    write_rollout(other, conversation_records(1, session_id=_session(5),
                                              label="o"))
    _compress(other)
    _sync_both(ns)
    _drain(ns)
    [key] = _conv_keys(ns, other)
    assert key in _population(ns)
    return key


def _set_population(ns, keep):
    conn = ns["open_cache_db"]()
    try:
        marks = ",".join("?" * len(keep))
        conn.execute("DELETE FROM codex_barrier_population" + (
            f" WHERE conversation_key NOT IN ({marks})" if keep else ""),
            tuple(keep))
        conn.commit()
    finally:
        conn.close()
    assert _population(ns) == set(keep)


def _plain_positive_control(env, *, population="none"):
    """The gate's positive control: a plain file with unread complete lines,
    no population hit (none, or only an unrelated key) and no paired cache
    cursor at all. Returns ``(ns, path, key, good)``."""
    ns, _provider, day = env
    other = _other_population_key(ns, day) if population == "unrelated" else None
    records = conversation_records(3, session_id=_session(1), label="p")
    ns, path, key, good = _plain_with_suffix(
        env, suffix=_lines(records[-4:]), cache_rejects=False)
    _set_population(ns, [] if other is None else [other])
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM codex_session_files WHERE path=?", (str(path),))
        conn.commit()
    finally:
        conn.close()
    return ns, path, key, good


def _assert_adopted_and_ingested(ns, path, key, good):
    assert _state_of(ns, path) == ("accepted", None)
    cursor = _conv_cursor(ns, path)
    assert cursor["last_byte_offset"] == path.stat().st_size
    assert _conv_keys(ns, path) == [key], "only the suffix's rows were written"
    assert _with_conv(ns, lambda c: c.execute(
        "SELECT MIN(line_offset) FROM codex_conversation_events "
        "WHERE source_path=?", (str(path),)).fetchone()[0]) >= len(good)


@pytest.mark.parametrize("population", ["none", "unrelated"])
def test_plain_adoption_without_a_population_hit_ingests(env, population):
    """#1090 T5 gate (R2-P2-1): a plain file with an unread suffix whose keys
    the barrier population does not hold (no population, or only an
    unrelated key) is never deferred, so it adopts and ingests even with no
    paired cache cursor at all. Detects a gate that holds on the cache
    cursor alone."""
    ns, path, key, good = _plain_positive_control(env, population=population)
    _drain(ns)
    _assert_adopted_and_ingested(ns, path, key, good)


#: #1090 R4-P1-1: the gate call (1 = preparation, 2 = application, in the
#: first drain run) from which the gate's own inspection fails, and how.
GATE_FAULTS = [
    pytest.param(1, OSError(13, "Permission denied"), id="prepare-oserror"),
    pytest.param(2, sqlite3.OperationalError("disk I/O error"),
                 id="apply-sqlite"),
]


def _gate_fault(monkeypatch, *, start, error):
    """Fail the gate's population read from gate call ``start`` on, only
    inside the gate, while ``state["active"]``. The fingerprint, the file
    and the (absent) cache cursor are untouched, as with a transient EACCES
    or I/O error."""
    cache = sys.modules["_cctally_cache"]
    real_gate = cache._codex_legacy_cache_held
    real_present = cache._codex_conversation_population_present
    state = {"calls": 0, "inside": False, "active": True}

    def gate(*args, **kwargs):
        state["calls"] += 1
        state["inside"] = True
        try:
            return real_gate(*args, **kwargs)
        finally:
            state["inside"] = False

    def present(conn):
        if state["active"] and state["inside"] and state["calls"] >= start:
            raise error
        return real_present(conn)

    monkeypatch.setattr(cache, "_codex_legacy_cache_held", gate)
    monkeypatch.setattr(cache, "_codex_conversation_population_present",
                        present)
    return state


def _paired_cursor_absent(ns, path):
    conn = ns["open_cache_db"]()
    try:
        return conn.execute("SELECT 1 FROM codex_session_files WHERE path=?",
                            (str(path),)).fetchone() is None
    finally:
        conn.close()


@pytest.mark.parametrize("entrance", ENTRANCES)
@pytest.mark.parametrize("start,error", GATE_FAULTS)
def test_undecidable_gate_leaves_the_adoption_pending(
        env, monkeypatch, start, error, entrance):
    """#1090 T5 gate (R4-P1-1): when the gate cannot decide (its inspection
    raises at preparation, or first at application), the positive-control
    adoption stays `pending` with no retry record, rather than committing a
    `legacy-cache-held` that no trigger releases: the fingerprint is
    unchanged and no cache cursor ever becomes `accepted`. Once the fault
    clears, a later drain run adopts and ingests, directly or after a full
    pass requests it. Detects a transient error turned into a permanent
    hold."""
    ns, path, key, good = _plain_positive_control(env)
    stat = path.stat()
    fault = _gate_fault(monkeypatch, start=start, error=error)
    _drain(ns)
    assert fault["calls"] >= start
    assert _state_of(ns, path)[0] == "pending", _state_of(ns, path)
    assert _retry_record(ns, path) is None
    fault["active"] = False
    assert _paired_cursor_absent(ns, path)
    assert (path.stat().st_ino, path.stat().st_size) == (stat.st_ino,
                                                         stat.st_size)
    if entrance == "pass":
        result = _enter(ns, "pass")
        assert "_codex-replay-drain" in result.drain_requested
        assert _paired_cursor_absent(ns, path)
    _drain(ns)
    _assert_adopted_and_ingested(ns, path, key, good)


@pytest.mark.parametrize("start,error", GATE_FAULTS)
def test_persistent_undecidable_gate_neither_holds_nor_burns_retries(
        env, monkeypatch, start, error):
    """#1090 T5 gate (R4-P1-1): a fault that persists across two drain runs
    leaves the row `pending` after each, never held and never charged as a
    `legacy-changed` revalidation retry, and each drain still completes.
    Detects a persistent error that exhausts the retry bound as if the
    state had changed."""
    ns, path, _key, _good = _plain_positive_control(env)
    _gate_fault(monkeypatch, start=start, error=error)
    cache = sys.modules["_cctally_cache"]
    real_apply = cache._apply_codex_verdict
    outcomes = []

    def apply(table, apply_path, *args, **kwargs):
        outcome = real_apply(table, apply_path, *args, **kwargs)
        if apply_path == str(path):
            outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(cache, "_apply_codex_verdict", apply)
    for _run in range(2):
        result = _drain(ns)
        assert result.outcome in ("drained", "yielded"), result
        assert result.retried == 0, result
        assert _state_of(ns, path)[0] == "pending", _state_of(ns, path)
        assert _retry_record(ns, path) is None
    assert outcomes and "legacy-changed" not in outcomes, outcomes


@pytest.mark.parametrize("rule", ["before-read", "after-read"])
def test_plain_adoption_the_barrier_would_defer_is_held(env, rule):
    """#1090 T5 gate (R2-P1-1, R2-P2-1): a plain file with an unread suffix
    and a present population is held as `legacy-cache-held` against an
    `unverified` cache cursor when the targeted sync would defer it: its
    membership keys hit the population (before read), or its unread suffix
    names a population key while its membership keys do not (after read).
    Detects a gate that copies only one of the two deferral rules."""
    ns, _provider, day = env
    if rule == "before-read":
        records = conversation_records(3, session_id=_session(1), label="p")
        ns, path, key, _good = _plain_with_suffix(
            env, suffix=_lines(records[-4:]), cache_rejects=True)
        assert key in _population(ns)
    else:
        other = _other_population_key(ns, day)
        ns, path, key, _good = _plain_with_suffix(
            env, suffix=_lines(conversation_records(
                1, session_id=_session(5), label="x")), cache_rejects=True)
        _set_population(ns, [other])
    _drain(ns)
    assert _cache_state(ns, path) == "unverified"
    assert _state_of(ns, path) == ("unverified", "legacy-cache-held")
    assert _retry_record(ns, path)["cacheHeld"] is True
