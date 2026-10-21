"""#968 T1 + T7 transitions: verified compression is invisible; changes are not."""
from __future__ import annotations

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
