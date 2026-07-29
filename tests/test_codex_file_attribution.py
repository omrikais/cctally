"""Durable Codex file attribution (#416 Slice 1).

Spec: ``docs/superpowers/specs/2026-07-28-416-codex-multi-account-design.md``
(sections 3.2-3.6). The Codex account is currently re-derived from the live
``~/.codex/auth.json`` on every ingest cycle, so a ``cache-sync --rebuild``
re-reads every rollout from offset 0 and re-stamps the whole history with
whichever account happens to be logged in. This module pins the replacement:
attribution is decided ONCE at first ingest, journaled as a ``codex_file_account``
op, and thereafter only replayed.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

import _cctally_core  # preserved across load_script(), safe at module top
from conftest import load_script, redirect_paths

KEY_A = "a" * 32
KEY_B = "b" * 32


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _siblings():
    import _cctally_journal
    import _lib_journal
    import _lib_rederive
    return _cctally_journal, _lib_journal, _lib_rederive


@pytest.fixture
def cache_conn(ns, tmp_path):
    """A real cache.db carrying the production schema."""
    import _cctally_db
    conn = sqlite3.connect(tmp_path / "cache.db")
    conn.execute("PRAGMA journal_mode=WAL")
    _cctally_db._apply_cache_schema(conn)
    conn.commit()
    yield conn
    conn.close()


def _discovered(*, spelling: str, physical: str, root_key: str):
    import _cctally_cache
    return _cctally_cache.CodexDiscoveredFile(
        source_path=pathlib.Path(spelling),
        physical_path=pathlib.Path(physical),
        provider_root=pathlib.Path("/roots/provider"),
        walk_root=pathlib.Path("/roots/provider/sessions"),
        source_root_key=root_key,
    )


# --------------------------------------------------------------------------
# Task 1 — the `codex_file_account` op and its classifier registration
# --------------------------------------------------------------------------

def test_codex_file_account_op_omits_key_for_sentinel(ns):
    """Two-shaped stamp (docs/accounts-gotchas.md): an op carries a real account
    on ``payload.account_key``, and the reserved sentinel is expressed by
    OMITTING the field — never by writing the literal ``"unattributed"``. A
    single-account/no-auth install must therefore produce a byte-identical
    payload shape to one that never knew about accounts."""
    _jr, J, _rd = _siblings()
    rec = J.make_codex_file_account(
        at="2026-07-28T00:00:00Z", root_scope="rk1", file_identity="fid1",
        incarnation=1, from_offset=0, account_key=None,
    )
    assert rec["t"] == "op"
    assert rec["payload"]["kind"] == "codex_file_account"
    assert "account_key" not in rec["payload"]
    assert rec["payload"]["root_scope"] == "rk1"
    assert rec["payload"]["file_identity"] == "fid1"
    assert rec["payload"]["incarnation"] == 1
    assert rec["payload"]["from_offset"] == 0


def test_codex_file_account_op_carries_a_real_account_key(ns):
    _jr, J, _rd = _siblings()
    rec = J.make_codex_file_account(
        at="2026-07-28T00:00:00Z", root_scope="rk1", file_identity="fid1",
        incarnation=2, from_offset=4096, account_key="a" * 32,
    )
    assert rec["payload"]["account_key"] == "a" * 32
    assert rec["payload"]["incarnation"] == 2
    assert rec["payload"]["from_offset"] == 4096
    # The op round-trips through the wire encoding unchanged.
    decoded = J.decode_line(J.encode_line(rec))
    assert decoded["payload"] == rec["payload"]


def test_codex_file_account_op_ids_separate_distinct_decisions(ns):
    """Two decisions differing only in the account must be distinct records —
    otherwise a corrective range decision would collide with the original."""
    _jr, J, _rd = _siblings()
    common = dict(at="2026-07-28T00:00:00Z", root_scope="rk1",
                  file_identity="fid1", incarnation=1, from_offset=0)
    a = J.make_codex_file_account(**common, account_key="a" * 32)
    b = J.make_codex_file_account(**common, account_key="b" * 32)
    sentinel = J.make_codex_file_account(**common, account_key=None)
    assert len({a["id"], b["id"], sentinel["id"]}) == 3


def test_codex_file_account_kind_is_registered_machinery(ns):
    """The kind must be registered as accounts machinery so it is never
    classified as legacy data and retro-stamped by
    ``_normalize_legacy_account_stamp`` (spec section 3.3)."""
    jr, _J, _rd = _siblings()
    assert "codex_file_account" in jr._ACCOUNTS_MACHINERY_KINDS


def test_codex_file_account_kind_has_a_rederive_classification(ns):
    """The protocol audit: ``_cctally_rederive.plan_claude_usage`` feeds
    ``_ACCOUNTS_MACHINERY_KINDS`` into ``validate_family_registry`` and raises
    ``RederiveConflict`` on any unclassified op kind. Registering the kind in one
    table but not the other therefore bricks the re-derive planner — this test
    is exactly what catches that (it fails when the kind is machinery-registered
    but absent from ``_lib_rederive._OP_CLASSIFICATIONS``)."""
    jr, _J, rd = _siblings()
    report = rd.validate_family_registry(
        evt_kinds=set(jr._EVT_SPECS),
        op_kinds=(
            set(jr.FOLD_APPLIERS)
            | set(jr._ACCOUNTS_MACHINERY_KINDS)
            | {"sync_week"}
        ),
    )
    assert report.unclassified_op_kinds == ()
    assert "codex_file_account" in report.op


def test_codex_file_account_op_is_never_legacy_classified(ns):
    """Neither shape of the op may be classified as legacy Codex data: the
    sentinel form omits ``account_key`` entirely, which is precisely the shape
    the legacy classifier keys on."""
    jr, J, _rd = _siblings()
    sentinel = J.make_codex_file_account(
        at="2026-07-28T00:00:00Z", root_scope="rk1", file_identity="fid1",
        incarnation=1, from_offset=0, account_key=None,
    )
    stamped = J.make_codex_file_account(
        at="2026-07-28T00:00:00Z", root_scope="rk1", file_identity="fid1",
        incarnation=1, from_offset=0, account_key="a" * 32,
    )
    assert jr.classify_legacy_provider(sentinel) is None
    assert jr.classify_legacy_provider(stamped) is None
    # And normalisation must leave the sentinel's omission intact.
    jr._normalize_legacy_account_stamp(sentinel, "cutover-claude")
    assert "account_key" not in sentinel["payload"]


# --------------------------------------------------------------------------
# Task 2 — file incarnation identity + the durable decision map
# --------------------------------------------------------------------------

def test_identity_is_stable_across_root_reordering(ns):
    """Spec section 3.2 / review F12: ``source_path`` is NOT a valid durable key.
    Discovery deduplicates on the canonical physical path but persists the FIRST
    configured candidate spelling, so reordering ``$CODEX_HOME`` roots or
    respelling a symlink yields a different ``source_path`` for the same physical
    file. The identity must ignore the spelling entirely."""
    import _cctally_cache as cc
    a = _discovered(spelling="/roots/a/sessions/r.jsonl",
                    physical="/real/r.jsonl", root_key="rk")
    b = _discovered(spelling="/roots/b/sessions/r.jsonl",
                    physical="/real/r.jsonl", root_key="rk")
    assert cc.codex_file_identity(a) == cc.codex_file_identity(b)


def test_identity_is_scoped_to_the_source_root(ns):
    """The same physical file reached under a DIFFERENT provider root is a
    different durable identity. This is what makes root requalification safe
    without an incarnation bump: the requalified file simply has no prior
    decision, so a fresh one is taken."""
    import _cctally_cache as cc
    a = _discovered(spelling="/roots/a/sessions/r.jsonl",
                    physical="/real/r.jsonl", root_key="rk-one")
    b = _discovered(spelling="/roots/a/sessions/r.jsonl",
                    physical="/real/r.jsonl", root_key="rk-two")
    assert cc.codex_file_identity(a) != cc.codex_file_identity(b)


def test_identity_is_an_opaque_stable_token(ns):
    """Non-reversible + fixed width, matching ``source_root_key``'s convention,
    so the durable key never embeds an operator path."""
    import _cctally_cache as cc
    ident = cc.codex_file_identity(
        _discovered(spelling="/roots/a/sessions/r.jsonl",
                    physical="/real/secret-project/r.jsonl", root_key="rk"))
    assert len(ident) == 32
    assert all(ch in "0123456789abcdef" for ch in ident)
    assert "secret-project" not in ident


def test_the_incarnation_high_water_is_max_set_and_per_identity(ns, cache_conn):
    """The walk resolves the next incarnation itself and persists it through the
    MAX-set upsert, INSIDE the per-file batch transaction the ingest may roll
    back and retry — so the write must be absolute, never an increment (a replay
    would double-bump). Replaying and going backwards must both converge."""
    import _cctally_cache as cc
    assert cc.codex_file_incarnation(cache_conn, "fid1") == 1
    cc.set_codex_file_incarnation(cache_conn, "fid1", 2)
    assert cc.codex_file_incarnation(cache_conn, "fid1") == 2
    cc.set_codex_file_incarnation(cache_conn, "fid1", 2)  # replay
    assert cc.codex_file_incarnation(cache_conn, "fid1") == 2
    cc.set_codex_file_incarnation(cache_conn, "fid1", 1)  # never regresses
    assert cc.codex_file_incarnation(cache_conn, "fid1") == 2
    cc.set_codex_file_incarnation(cache_conn, "fid1", 3)
    assert cc.codex_file_incarnation(cache_conn, "fid1") == 3
    # Scoped per identity — another file is unaffected.
    assert cc.codex_file_incarnation(cache_conn, "fid2") == 1


def test_old_incarnation_range_never_stamps_new_bytes(ns, cache_conn):
    """Spec section 3.2: a truncation resets the file to offset zero and deletes
    its derived rows, so a permanent ``(path, offset)`` interval would overlap
    newly reused offsets and stamp a REPLACEMENT file with the previous
    account. Intervals are therefore incarnation-qualified."""
    import _cctally_cache as cc
    cc.record_codex_file_account(
        cache_conn, file_identity="fid1", incarnation=1, from_offset=0,
        root_scope="rk", account_key=KEY_A, decided_at_utc="2026-07-28T00:00:00Z")
    assert cc.resolve_codex_file_account(
        cache_conn, "fid1", incarnation=1, offset=0).account_key == KEY_A
    cc.set_codex_file_incarnation(cache_conn, "fid1", 2)
    assert cc.resolve_codex_file_account(
        cache_conn, "fid1", incarnation=2, offset=0) is None


def test_narrowest_containing_interval_wins(ns, cache_conn):
    """Spec section 3.2 interval precedence: within one incarnation the narrowest
    containing interval wins, so a mid-file account change is expressed as a
    second range-qualified decision and the first is never rewritten."""
    import _cctally_cache as cc
    cc.record_codex_file_account(
        cache_conn, file_identity="fid1", incarnation=1, from_offset=0,
        root_scope="rk", account_key=KEY_A, decided_at_utc="2026-07-28T00:00:00Z")
    cc.record_codex_file_account(
        cache_conn, file_identity="fid1", incarnation=1, from_offset=4096,
        root_scope="rk", account_key=KEY_B, decided_at_utc="2026-07-28T01:00:00Z")
    assert cc.resolve_codex_file_account(
        cache_conn, "fid1", incarnation=1, offset=0).account_key == KEY_A
    assert cc.resolve_codex_file_account(
        cache_conn, "fid1", incarnation=1, offset=4095).account_key == KEY_A
    assert cc.resolve_codex_file_account(
        cache_conn, "fid1", incarnation=1, offset=4096).account_key == KEY_B
    assert cc.resolve_codex_file_account(
        cache_conn, "fid1", incarnation=1, offset=99999).account_key == KEY_B


def test_sentinel_decision_is_distinguishable_from_no_decision(ns, cache_conn):
    """Spec section 3.6 has THREE outcomes, and two of them must not collapse:
    a stably-absent identity is an explicit sentinel DECISION (account NULL),
    whereas a torn read is NO decision at all. If ``resolve`` returned a bare
    ``None`` for both, the ingest could not tell "decided: no account" from
    "undecided" and would re-consult auth.json for an already-decided file."""
    import _cctally_cache as cc
    cc.record_codex_file_account(
        cache_conn, file_identity="fid1", incarnation=1, from_offset=0,
        root_scope="rk", account_key=None, decided_at_utc="2026-07-28T00:00:00Z")
    decided = cc.resolve_codex_file_account(cache_conn, "fid1", incarnation=1, offset=0)
    assert decided is not None
    assert decided.account_key is None
    assert cc.resolve_codex_file_account(
        cache_conn, "fid-never-seen", incarnation=1, offset=0) is None


def test_recording_a_decision_is_idempotent(ns, cache_conn):
    """Crash-replay safety: re-appending the same decision must converge, never
    raise and never duplicate."""
    import _cctally_cache as cc
    for _ in range(3):
        cc.record_codex_file_account(
            cache_conn, file_identity="fid1", incarnation=1, from_offset=0,
            root_scope="rk", account_key=KEY_A,
            decided_at_utc="2026-07-28T00:00:00Z")
    assert cache_conn.execute(
        "SELECT COUNT(*) FROM codex_file_accounts").fetchone()[0] == 1
    assert cc.resolve_codex_file_account(
        cache_conn, "fid1", incarnation=1, offset=0).account_key == KEY_A


# --------------------------------------------------------------------------
# Task 5 — THE decisive regression barrier (spec acceptance criterion 4)
#
# Real subprocess under the FULL three-variable isolation (HOME, CODEX_HOME,
# CCTALLY_DATA_DIR). With only two of them the operator's real ~/.codex/auth.json
# stays reachable and this test silently reads the live identity, passing for
# entirely the wrong reason.
# --------------------------------------------------------------------------

import base64
import json
import os
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CCTALLY_BIN = REPO_ROOT / "bin" / "cctally"
ROLLOUT_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
    / "modern-full.jsonl"
)


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")


def _auth_json(account_id: str, email: str) -> str:
    payload = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
        },
    }
    token = f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(payload)}.sig"
    return json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {"id_token": token, "access_token": "a", "refresh_token": "r"},
        "last_refresh": "2026-07-20T00:00:00Z",
    })


def _account_key_for(account_id: str, email: str) -> str:
    import _lib_accounts
    return _lib_accounts.account_key("codex", account_id + "\0" + email)


def _isolated_codex_world(tmp_path):
    """HOME + CODEX_HOME + CCTALLY_DATA_DIR, all inside tmp_path."""
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    data = tmp_path / "data"
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(ROLLOUT_FIXTURE, rollout)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "CCTALLY_DATA_DIR": str(data),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "TZ": "Etc/UTC",
    })
    return env, data, codex_home, rollout


def _cache_sync(env, *extra):
    return subprocess.run(
        [sys.executable, str(CCTALLY_BIN), "cache-sync", "--source", "codex",
         *extra],
        env=env, capture_output=True, text=True, timeout=180,
    )


def _accounts_of_rows(data: pathlib.Path, table: str) -> set:
    conn = sqlite3.connect(data / "cache.db")
    try:
        return {
            r[0] for r in conn.execute(f"SELECT DISTINCT account_key FROM {table}")
        }
    finally:
        conn.close()


def test_rebuild_does_not_restamp_history(ns, tmp_path):
    """Ingest under account A, switch auth.json to account B, rebuild.
    A's rows must still say A.

    This is the production defect (#416 spec section 1.1) in miniature: the
    Codex account is resolved from the LIVE auth.json once per sync and applied
    to every byte that cycle reads, and cache.db is by design fully
    re-derivable, so `cache-sync --rebuild` re-reads every rollout from offset 0
    and re-stamps the entire history with whoever happens to be logged in.
    """
    env, data, _codex_home, _rollout = _isolated_codex_world(tmp_path)
    codex_home = pathlib.Path(env["CODEX_HOME"])
    key_a = _account_key_for("acct-alpha", "alpha@example.com")
    key_b = _account_key_for("acct-beta", "beta@example.com")
    assert key_a != key_b

    (codex_home / "auth.json").write_text(_auth_json("acct-alpha", "alpha@example.com"))
    first = _cache_sync(env)
    assert first.returncode == 0, first.stderr
    assert _accounts_of_rows(data, "codex_session_entries") == {key_a}, (
        "precondition: account A ingested")
    assert _accounts_of_rows(data, "quota_window_snapshots") == {key_a}

    # The operator switches Codex accounts, then rebuilds the cache.
    (codex_home / "auth.json").write_text(_auth_json("acct-beta", "beta@example.com"))
    rebuilt = _cache_sync(env, "--rebuild")
    assert rebuilt.returncode == 0, rebuilt.stderr

    entries_after = _accounts_of_rows(data, "codex_session_entries")
    quota_after = _accounts_of_rows(data, "quota_window_snapshots")
    assert entries_after == {key_a}, (
        f"rebuild re-stamped accounting history: {entries_after} (expected {key_a})")
    assert quota_after == {key_a}, (
        f"rebuild re-stamped quota history: {quota_after} (expected {key_a})")


# --------------------------------------------------------------------------
# Task 3 — fail-closed attribution at ingest (spec section 3.6)
# --------------------------------------------------------------------------

def _codex_root(tmp_path, rollout_name="modern-full.jsonl"):
    provider_root = tmp_path / "codex-provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(
        REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
        / rollout_name, rollout)
    return provider_root, rollout


def _journal_ops(ns, kind: str) -> list:
    journal_dir = ns["_cctally_core"].JOURNAL_DIR
    out = []
    if not journal_dir.exists():
        return out
    for seg in sorted(journal_dir.glob("*.jsonl")):
        for line in seg.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if (rec.get("payload") or {}).get("kind") == kind:
                out.append(rec)
    return out


def _map_rows(conn) -> list:
    return conn.execute(
        "SELECT file_identity, incarnation, from_offset, root_scope, account_key "
        "FROM codex_file_accounts ORDER BY file_identity, incarnation, from_offset"
    ).fetchall()


def test_first_ingest_records_a_durable_decision(ns, monkeypatch, tmp_path):
    """The decision is journaled AND materialized into the cache map, so a later
    rebuild has something to replay instead of re-reading auth.json."""
    provider_root, _rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    expected = _account_key_for("acct-alpha", "alpha@example.com")

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
        assert stats.files_processed == 1
        rows = _map_rows(cache)
        assert len(rows) == 1, f"expected exactly one decision, got {rows}"
        _ident, incarnation, from_offset, _root, account_key = rows[0]
        assert incarnation == 1
        assert from_offset == 0
        assert account_key == expected
    finally:
        cache.close()

    ops = _journal_ops(ns, "codex_file_account")
    assert len(ops) == 1, f"expected exactly one journaled decision, got {ops}"
    assert ops[0]["payload"]["account_key"] == expected
    assert ops[0]["payload"]["from_offset"] == 0
    assert ops[0]["payload"]["incarnation"] == 1


def test_stably_absent_ingest_records_a_sentinel_decision(ns, monkeypatch, tmp_path):
    """No auth.json (api-key mode) is an explicit sentinel DECISION: the map row
    carries NULL and the journal op OMITS ``account_key`` entirely. It is not
    "undecided" — a rebuild must replay it rather than re-reading auth."""
    provider_root, _rollout = _codex_root(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
        assert stats.files_processed == 1
        rows = _map_rows(cache)
        assert len(rows) == 1
        assert rows[0][4] is None
    finally:
        cache.close()

    ops = _journal_ops(ns, "codex_file_account")
    assert len(ops) == 1
    assert "account_key" not in ops[0]["payload"], (
        "the sentinel is expressed by OMITTING the field, never by writing "
        "the literal 'unattributed'")


def test_torn_auth_records_no_decision_and_no_op(ns, monkeypatch, tmp_path):
    """Spec section 3.6: torn is NO decision and NO op — not an ``unattributed``
    decision. The rev-1 spec was wrong to list it as one."""
    provider_root, rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    import _cctally_cache as cc
    monkeypatch.setattr(
        cc, "_resolve_codex_account_for_root",
        lambda _root: cc._CodexRootAccount("torn", None))

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
        assert stats.files_deferred_torn == 1
        assert stats.files_processed == 0
        assert _map_rows(cache) == []
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_files WHERE path=?",
            (str(rollout),)).fetchone() == (0,)
    finally:
        cache.close()
    assert _journal_ops(ns, "codex_file_account") == []


def test_journal_append_failure_defers_the_file_and_commits_no_dml(
        ns, monkeypatch, tmp_path):
    """Spec section 3.6 / review F3: the quota-obs append is deliberately
    best-effort (it swallows every exception and continues). That is safe for an
    OBSERVATION and unsafe for a "decided once" map — accounting rows and the
    file watermark would commit with no durable decision, and the next rebuild
    would then have nothing to replay. The decision append must therefore FAIL
    CLOSED."""
    provider_root, rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    import _cctally_journal as jr
    real_append = jr.append_record

    def failing(record, *args, **kwargs):
        if (record.get("payload") or {}).get("kind") == "codex_file_account":
            raise OSError("journal is full")
        return real_append(record, *args, **kwargs)

    monkeypatch.setattr(jr, "append_record", failing)

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
        assert stats.files_processed == 0
        assert stats.files_failed >= 1
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone() == (0,)
        assert cache.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone() == (0,)
        assert _map_rows(cache) == []
        # Watermark must NOT advance, so the next sync re-reads the same bytes.
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_files WHERE path=?",
            (str(rollout),)).fetchone() == (0,)
    finally:
        cache.close()


def test_a_decided_file_never_re_reads_auth_on_a_rebuild(ns, monkeypatch, tmp_path):
    """The mechanism behind acceptance criterion 4, asserted directly: once a
    file carries a durable decision, the rebuild walk must not consult
    auth.json for it at all."""
    provider_root, _rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    expected = _account_key_for("acct-alpha", "alpha@example.com")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        import _cctally_cache as cc
        calls = {"n": 0}
        real = cc._resolve_codex_account_for_root

        def counting(root):
            calls["n"] += 1
            return real(root)

        monkeypatch.setattr(cc, "_resolve_codex_account_for_root", counting)
        ns["sync_codex_cache"](cache, rebuild=True)
        assert calls["n"] == 0, (
            "the rebuild walk consulted auth.json for an already-decided file")
        assert {r[0] for r in cache.execute(
            "SELECT DISTINCT account_key FROM codex_session_entries")} == {expected}
    finally:
        cache.close()


def test_truncation_opens_a_fresh_decision_from_live_auth(ns, monkeypatch, tmp_path):
    """A truncation resets the file to offset zero, so the previous
    incarnation's interval must not cover the replacement bytes: the new
    incarnation has no decision and takes a fresh one."""
    provider_root, rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")
    key_b = _account_key_for("acct-beta", "beta@example.com")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        import _cctally_cache as cc
        discovered = cc._discover_codex_files_with_roots()
        assert len(discovered) == 1
        ident = cc.codex_file_identity(discovered[0])
        assert cc.codex_file_incarnation(cache, ident) == 1

        # The file is replaced by a shorter one under a different account.
        original = rollout.read_text().splitlines(keepends=True)
        rollout.write_text("".join(original[:3]))
        (provider_root / "auth.json").write_text(
            _auth_json("acct-beta", "beta@example.com"))
        ns["sync_codex_cache"](cache)

        assert cc.codex_file_incarnation(cache, ident) == 2
        decisions = {
            (r[1], r[4]) for r in _map_rows(cache)
        }
        assert (1, key_a) in decisions
        assert (2, key_b) in decisions
        assert {r[0] for r in cache.execute(
            "SELECT DISTINCT account_key FROM codex_session_entries")} == {key_b}
    finally:
        cache.close()


# --------------------------------------------------------------------------
# Task 4 — ONE composite cache leg (spec section 3.4 / review F1, F2)
# --------------------------------------------------------------------------

def _quota_obs(jl, *, root="root-a", path="/codex/root-a/r.jsonl", offset=10,
               account=None):
    return jl.make_obs(
        at="2026-07-28T10:00:00Z", src="codex-quota", provider="codex",
        account=account,
        payload={
            "kind": "quota_window_snapshot", "source": "codex",
            "source_root_key": root, "source_path": path,
            "line_offset": offset, "captured_at_utc": "2026-07-28T10:00:00Z",
            "observed_slot": "primary",
            "logical_limit_key": '{"limitId":"native-primary"}',
            "limit_id": "native-primary", "limit_name": "Primary",
            "window_minutes": 300, "used_percent": 42.0,
            "resets_at_utc": "2026-07-28T15:00:00+00:00", "plan_type": "pro",
            "individual_limit_json": None, "reached_type": None,
            "observed_model": "gpt-5.3-codex",
        })


def _file_account_op(jl, *, account_key=KEY_A, incarnation=1, from_offset=0):
    return jl.make_codex_file_account(
        at="2026-07-28T10:00:01Z", root_scope="root-a", file_identity="fid-1",
        incarnation=incarnation, from_offset=from_offset, account_key=account_key)


def _drop_table(ns, table: str) -> None:
    conn = sqlite3.connect(str(ns["_cctally_core"].CACHE_DB_PATH))
    try:
        conn.execute(f"DROP TABLE {table}")
        conn.commit()
    finally:
        conn.close()


def test_cache_applier_materializes_both_families(ns):
    """One leg, both families. The Codex file-account ops must reach cache.db
    through the SAME leg as the quota obs — a second, independently
    prefix-stopping applier could not share the step-3 seam (review F1)."""
    import _cctally_journal as jr
    import _lib_journal as jl
    ns["open_cache_db"]().close()

    decoded = [(_quota_obs(jl), "seg", 0), (_file_account_op(jl), "seg", 100)]
    assert jr._cache_applier(decoded) is None

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source='codex'").fetchone()[0] == 1
        assert _map_rows(conn) == [("fid-1", 1, 0, "root-a", KEY_A)]
    finally:
        conn.close()


def test_cache_applier_is_all_or_nothing_across_families(ns):
    """Spec section 3.4: the leg commits BOTH families or neither, and returns
    the EARLIEST stop across them.

    Why this matters: `_quota_applier` commits every record it handles and only
    then returns a stop index, while `run_stats_ingest` invokes ONE applier and
    truncates the batch afterwards. Two independent prefix-stopping appliers
    could therefore let one commit the whole batch while the other stopped
    earlier, exposing suffix effects beyond the retained prefix and violating
    the scalar-cursor rule (docs/journal-gotchas.md)."""
    import _cctally_journal as jr
    import _lib_journal as jl
    ns["open_cache_db"]().close()
    _drop_table(ns, "codex_file_accounts")  # the op's insert will raise

    decoded = [(_quota_obs(jl), "seg", 0), (_file_account_op(jl), "seg", 100)]
    stop = jr._cache_applier(decoded)
    assert stop == 0, "stop must be the EARLIEST index across both families"

    conn = sqlite3.connect(str(ns["_cctally_core"].CACHE_DB_PATH))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == 0, (
            "the quota family must roll back with the file-account family")
    finally:
        conn.close()


def test_cache_applier_is_a_no_op_without_either_family(ns):
    import _cctally_journal as jr
    import _lib_journal as jl
    ns["open_cache_db"]().close()
    unrelated = jl.make_obs(
        at="2026-07-28T10:00:00Z", src="statusline", provider="claude",
        payload={"week_start_date": "2026-07-28", "weekly_percent": 1.0})
    assert jr._cache_applier([(unrelated, "seg", 0)]) is None


def test_cache_applier_is_wired_to_the_ingest_seam(ns):
    """End-to-end through `run_stats_ingest`, which is the only path the live
    cycle uses."""
    import _cctally_journal as jr
    import _lib_journal as jl
    ns["open_cache_db"]().close()
    jr.append_record(_file_account_op(jl))
    res = jr.run_stats_ingest(mode="authoritative")
    assert res.ran
    conn = ns["open_cache_db"]()
    try:
        assert _map_rows(conn) == [("fid-1", 1, 0, "root-a", KEY_A)]
    finally:
        conn.close()


def test_rebuild_cache_leg_materializes_file_account_ops(ns):
    """Spec section 3.4: `_rebuild_quota_cache_leg` must materialize the map
    too, or the rebuild rehydration has nothing to rehydrate FROM."""
    import _cctally_journal as jr
    import _lib_journal as jl
    ns["open_cache_db"]().close()
    records = [_quota_obs(jl), _file_account_op(jl)]
    jr._rebuild_quota_cache_leg(records)
    conn = ns["open_cache_db"]()
    try:
        assert _map_rows(conn) == [("fid-1", 1, 0, "root-a", KEY_A)]
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == 1
    finally:
        conn.close()


def test_sentinel_file_account_op_replays_as_null(ns):
    """An op whose payload OMITS `account_key` is the stably-absent sentinel and
    must materialize as NULL — never as the literal string."""
    import _cctally_journal as jr
    import _lib_journal as jl
    ns["open_cache_db"]().close()
    jr._cache_applier([(_file_account_op(jl, account_key=None), "seg", 0)])
    conn = ns["open_cache_db"]()
    try:
        assert _map_rows(conn) == [("fid-1", 1, 0, "root-a", None)]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Task 5 — the rebuild rehydration phase + robustness (spec section 3.4)
# --------------------------------------------------------------------------

def _wipe_map(conn) -> None:
    """Simulate a cache.db that was recreated (corruption recovery, a manual
    `rm cache.db`) while the append-only journal kept every decision."""
    conn.execute("DELETE FROM codex_file_accounts")
    conn.execute("DELETE FROM codex_file_incarnations")
    conn.commit()


def test_rebuild_rehydrates_the_map_from_the_journal(ns, monkeypatch, tmp_path):
    """Spec section 3.4 / review F2: the journal-to-cache replay only runs inside
    `rebuild_stats_index`, whereas `cache-sync --rebuild` independently clears
    the Codex rows and starts the rollout walk with NO applier in front of it.
    A recreated cache.db therefore has an empty map, and without an explicit
    rehydration phase the walk falls back to the live auth.json for every file.
    """
    provider_root, _rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        assert _map_rows(cache), "precondition: a decision was recorded"
        _wipe_map(cache)
        # The journal still holds it.
        assert len(_journal_ops(ns, "codex_file_account")) == 1

        (provider_root / "auth.json").write_text(
            _auth_json("acct-beta", "beta@example.com"))
        ns["sync_codex_cache"](cache, rebuild=True)

        assert {r[0] for r in cache.execute(
            "SELECT DISTINCT account_key FROM codex_session_entries")} == {key_a}
        assert _map_rows(cache), "the map must be rehydrated, not left empty"
    finally:
        cache.close()


def test_clear_codex_derived_rows_preserves_the_map(ns, monkeypatch, tmp_path):
    """The rebuild clear must not take the attribution map with it — it is the
    one Codex family that is NOT re-derivable from the rollout bytes."""
    import _cctally_cache as cc
    provider_root, _rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        before = _map_rows(cache)
        assert before
        cc._clear_codex_derived_rows(cache)
        cache.commit()
        assert _map_rows(cache) == before
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone() == (0,)
    finally:
        cache.close()


def test_root_reordering_still_hits_the_decision(ns, monkeypatch, tmp_path):
    """Review F12: discovery persists the FIRST configured candidate spelling,
    so adding a second $CODEX_HOME entry that reaches the same physical rollout
    changes `source_path`. A path-keyed map would miss; an identity-keyed one
    must not."""
    provider_root, rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        identity_before = {r[0] for r in _map_rows(cache)}

        # A symlinked alias of the SAME provider root, listed first.
        alias = tmp_path / "codex-alias"
        alias.symlink_to(provider_root, target_is_directory=True)
        monkeypatch.setenv("CODEX_HOME", f"{alias},{provider_root}")
        (provider_root / "auth.json").write_text(
            _auth_json("acct-beta", "beta@example.com"))
        ns["sync_codex_cache"](cache, rebuild=True)

        assert {r[0] for r in _map_rows(cache)} == identity_before, (
            "a respelled root must not mint a second identity")
        assert {r[0] for r in cache.execute(
            "SELECT DISTINCT account_key FROM codex_session_entries")} == {key_a}
    finally:
        cache.close()


def test_requalification_takes_a_fresh_decision(ns, monkeypatch, tmp_path):
    """A rollout that moves under a genuinely different provider root is a
    different durable identity, so it carries no prior decision and takes a
    fresh one — strictly stronger than an incarnation bump."""
    provider_root, rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")
    key_b = _account_key_for("acct-beta", "beta@example.com")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        assert len(_map_rows(cache)) == 1

        # Same rollout bytes, genuinely different provider root.
        other_root = tmp_path / "codex-other"
        other_rollout = other_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
        other_rollout.parent.mkdir(parents=True)
        shutil.copyfile(rollout, other_rollout)
        (other_root / "auth.json").write_text(
            _auth_json("acct-beta", "beta@example.com"))
        monkeypatch.setenv("CODEX_HOME", str(other_root))
        ns["sync_codex_cache"](cache)

        identities = {r[0] for r in _map_rows(cache)}
        assert len(identities) == 2, "a different root is a different identity"
        accounts = {r[4] for r in _map_rows(cache)}
        assert accounts == {key_a, key_b}
    finally:
        cache.close()


# --------------------------------------------------------------------------
# Task 6 — precedence: the file decision beats the observation stamp
# (spec section 3.5 / review F5)
# --------------------------------------------------------------------------

def _seed_decision(conn, *, path: pathlib.Path, root="root-a",
                   account_key=KEY_A, incarnation=1, from_offset=0):
    import _cctally_cache as cc
    from _lib_source_identity import codex_file_key
    identity = codex_file_key(root, str(cc._canonical_codex_path(path)))
    cc.record_codex_file_account(
        conn, file_identity=identity, incarnation=incarnation,
        from_offset=from_offset, root_scope=root, account_key=account_key,
        decided_at_utc="2026-07-28T00:00:00Z")
    cc.set_codex_file_incarnation(conn, identity, incarnation)
    conn.commit()
    return identity


def _quota_account_in_cache(ns, path: pathlib.Path, offset: int):
    conn = sqlite3.connect(str(ns["_cctally_core"].CACHE_DB_PATH))
    try:
        row = conn.execute(
            "SELECT account_key FROM quota_window_snapshots "
            "WHERE source='codex' AND source_path=? AND line_offset=?",
            (str(path), offset)).fetchone()
        return row[0] if row else "<missing>"
    finally:
        conn.close()


def test_file_decision_beats_a_stale_observation_stamp(ns, tmp_path):
    """Spec section 3.5 / review F5: "the journal always wins" picks the WRONG
    authority. Journaled quota obs are deduplicated on a natural key that
    EXCLUDES the account and later records for that key are discarded, so the
    retained observation is first-stamp-wins and may preserve the known
    late-ingest guess (bytes written under one login but ingested after a
    switch). The durable file/range decision is authoritative; the observation
    stamp only covers bytes no decision reaches."""
    import _lib_journal as jl
    import _cctally_journal as jr
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    cache = ns["open_cache_db"]()
    try:
        _seed_decision(cache, path=rollout, account_key=KEY_A)
    finally:
        cache.close()

    obs = _quota_obs(jl, root="root-a", path=str(rollout), offset=10,
                     account=KEY_B)
    assert jr._cache_applier([(obs, "seg", 0)]) is None
    assert _quota_account_in_cache(ns, rollout, 10) == KEY_A


def test_attribution_disagreement_is_reported_not_silent(ns, tmp_path, capfd):
    """A disagreement is reported rather than silently applied — a genuine
    correction is expressed as a new range decision, never by mutating
    history."""
    import _lib_journal as jl
    import _cctally_journal as jr
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    cache = ns["open_cache_db"]()
    try:
        _seed_decision(cache, path=rollout, account_key=KEY_A)
    finally:
        cache.close()
    capfd.readouterr()
    jr._cache_applier([(_quota_obs(jl, root="root-a", path=str(rollout),
                                   offset=10, account=KEY_B), "seg", 0)])
    err = capfd.readouterr().err
    assert "attribution conflict" in err, err


def test_obs_stamp_is_used_where_no_decision_covers_the_bytes(ns, tmp_path):
    """The observation stamp remains the fallback for undecided bytes — the
    precedence rule narrows it, it does not remove it."""
    import _lib_journal as jl
    import _cctally_journal as jr
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    ns["open_cache_db"]().close()
    jr._cache_applier([(_quota_obs(jl, root="root-a", path=str(rollout),
                                   offset=10, account=KEY_B), "seg", 0)])
    assert _quota_account_in_cache(ns, rollout, 10) == KEY_B


def test_decision_converges_a_previously_unstamped_quota_row(ns, tmp_path):
    """The upsert must CONVERGE, not merely ignore: a row already materialized
    without an account (a legacy replay, or a rollout that evaporated before
    #416) is corrected to the decision. Repeating it is idempotent, which is
    what preserves crash-replay."""
    import _lib_journal as jl
    import _cctally_journal as jr
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    ns["open_cache_db"]().close()
    unstamped = _quota_obs(jl, root="root-a", path=str(rollout), offset=10,
                           account=None)
    jr._cache_applier([(unstamped, "seg", 0)])
    assert _quota_account_in_cache(ns, rollout, 10) is None

    cache = ns["open_cache_db"]()
    try:
        _seed_decision(cache, path=rollout, account_key=KEY_A)
    finally:
        cache.close()
    for _ in range(2):
        jr._cache_applier([(unstamped, "seg", 0)])
        assert _quota_account_in_cache(ns, rollout, 10) == KEY_A


def test_precedence_honours_the_narrowest_range(ns, tmp_path):
    """Two ranges in one incarnation: each obs takes the decision covering its
    own byte offset."""
    import _lib_journal as jl
    import _cctally_journal as jr
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    cache = ns["open_cache_db"]()
    try:
        _seed_decision(cache, path=rollout, account_key=KEY_A, from_offset=0)
        _seed_decision(cache, path=rollout, account_key=KEY_B, from_offset=100)
    finally:
        cache.close()
    jr._cache_applier([
        (_quota_obs(jl, root="root-a", path=str(rollout), offset=10,
                    account=None), "seg", 0),
        (_quota_obs(jl, root="root-a", path=str(rollout), offset=150,
                    account=None), "seg", 100),
    ])
    assert _quota_account_in_cache(ns, rollout, 10) == KEY_A
    assert _quota_account_in_cache(ns, rollout, 150) == KEY_B


# --------------------------------------------------------------------------
# Slice 1 review round — P1-1 / P1-2
#
# The rehydration phase shipped wired ONLY into the `rebuild=True` branch, but
# its own docstring names the scenario it must cover: a recreated cache.db from
# corruption recovery or a manual `rm cache.db`. Every production Codex call
# site passes `rebuild=False`, and the corruption auto-heal recreates the family
# then re-runs the same ORDINARY sync — so the map starts empty and the walk
# falls straight back to the live auth.json.
#
# And once a cache map row diverges from the journal, the additive
# `DO NOTHING` replay silently no-ops over it (the #374 fold-applier defect
# class), so the documented remedy — `cache-sync --rebuild` — cannot heal it.
# --------------------------------------------------------------------------

def _remove_cache_db_family(data: pathlib.Path) -> None:
    """Delete cache.db exactly the way corruption recovery / `rm cache.db` do."""
    for suffix in ("", "-wal", "-shm"):
        target = pathlib.Path(str(data / "cache.db") + suffix)
        if target.exists():
            target.unlink()


def _file_account_ops(data: pathlib.Path) -> list:
    journal_dir = data / "journal"
    out = []
    if not journal_dir.exists():
        return out
    for seg in sorted(journal_dir.glob("*.jsonl")):
        for line in seg.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if (rec.get("payload") or {}).get("kind") == "codex_file_account":
                out.append(rec)
    return out


def test_recreated_cache_db_plain_sync_does_not_restamp(ns, tmp_path):
    """A recreated cache.db + an ORDINARY sync must replay the journalled
    decision, not re-derive it from the live auth.json.

    This is the production path: corruption recovery and `rm cache.db` both
    leave an empty attribution map, and every production Codex call site syncs
    with ``rebuild=False``. Nothing in the ordinary sync rehydrated the map, so
    the walk re-decided every file from whoever happened to be logged in — the
    same defect ``--rebuild`` was fixed for, reached by a shorter road.
    """
    env, data, _codex_home, _rollout = _isolated_codex_world(tmp_path)
    codex_home = pathlib.Path(env["CODEX_HOME"])
    key_a = _account_key_for("acct-alpha", "alpha@example.com")
    key_b = _account_key_for("acct-beta", "beta@example.com")

    (codex_home / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    first = _cache_sync(env)
    assert first.returncode == 0, first.stderr
    assert _accounts_of_rows(data, "codex_session_entries") == {key_a}
    assert len(_file_account_ops(data)) == 1, "precondition: one decision journalled"

    _remove_cache_db_family(data)
    (codex_home / "auth.json").write_text(
        _auth_json("acct-beta", "beta@example.com"))
    again = _cache_sync(env)
    assert again.returncode == 0, again.stderr

    entries_after = _accounts_of_rows(data, "codex_session_entries")
    assert entries_after == {key_a}, (
        f"a recreated cache.db re-stamped history from the live auth.json: "
        f"{entries_after} (expected {key_a})")
    ops = _file_account_ops(data)
    assert len(ops) == 1, (
        "a second, contradictory decision was journalled for the same "
        f"(file_identity, incarnation, from_offset): "
        f"{[(o['payload'].get('from_offset'), o['payload'].get('account_key')) for o in ops]}")


def test_rebuild_heals_a_divergent_map_row_from_the_journal(ns, tmp_path):
    """`cache-sync --rebuild` is the documented remedy, so it must be able to
    CONVERGE the map onto the journal — not merely add missing rows.

    The applier is an inserter (`ON CONFLICT ... DO NOTHING`), and
    ``_clear_codex_derived_rows`` deliberately preserves the map, so a divergent
    row survives the clear and the replay refuses to correct it. That leaves the
    install permanently wrong with no supported repair.
    """
    env, data, _codex_home, _rollout = _isolated_codex_world(tmp_path)
    codex_home = pathlib.Path(env["CODEX_HOME"])
    key_a = _account_key_for("acct-alpha", "alpha@example.com")
    key_b = _account_key_for("acct-beta", "beta@example.com")

    (codex_home / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    assert _cache_sync(env).returncode == 0
    assert _accounts_of_rows(data, "codex_session_entries") == {key_a}

    # However it got there (the recreated-cache.db path above is one way), the
    # cache map now disagrees with the journal.
    conn = sqlite3.connect(data / "cache.db")
    try:
        conn.execute("UPDATE codex_file_accounts SET account_key = ?", (key_b,))
        conn.commit()
        assert {r[0] for r in conn.execute(
            "SELECT account_key FROM codex_file_accounts")} == {key_b}
    finally:
        conn.close()

    rebuilt = _cache_sync(env, "--rebuild")
    assert rebuilt.returncode == 0, rebuilt.stderr

    conn = sqlite3.connect(data / "cache.db")
    try:
        map_keys = {r[0] for r in conn.execute(
            "SELECT account_key FROM codex_file_accounts")}
    finally:
        conn.close()
    assert map_keys == {key_a}, (
        f"--rebuild could not converge the map onto the journal: {map_keys}")
    entries_after = _accounts_of_rows(data, "codex_session_entries")
    assert entries_after == {key_a}, (
        f"--rebuild left the derived rows on the divergent account: {entries_after}")


# --------------------------------------------------------------------------
# Slice 1 review round — P2-2
#
# `codex_account_for_offset` breaks on the first `from_offset > offset`, so it
# is only correct for an ASCENDING list. `load_codex_file_account_ranges`
# returns ascending, but the walk appended the pending decision at
# `start_offset` unconditionally — and reaching that append requires every
# loaded range to have `from_offset > start_offset`, so whenever the loaded list
# was non-empty the concatenation was unsorted.
#
# The trigger is the operator's real upgrade path: a live rollout already
# ingested to offset M BEFORE #416 existed mints its first decision at
# `from_offset = M`; cache.db is later recreated, so the cursor and the derived
# rows are gone while the journal-backed map survives. The walk then re-reads
# from offset 0, finds itself uncovered, and mints `(0, LIVE)` — which both
# re-stamps bytes at offset >= M with the wrong account AND infers an account
# for the undecided pre-#416 prefix, which is exactly what D1 forbids.
# --------------------------------------------------------------------------

def test_prefix_of_a_partly_decided_file_is_never_inferred(ns, monkeypatch, tmp_path):
    """The pre-#416 upgrade path, built the way it actually arises.

    The setup used to hand-edit ``codex_file_accounts.from_offset`` after a
    normal sync, which manufactured a map the JOURNAL contradicted — a state the
    B1 journal-cursor replay now (correctly) converges away. So the partly
    decided file is produced for real instead: the first ingest runs with the
    #416 decision machinery disabled (exactly what a pre-#416 binary did —
    watermark and derived rows, no decision anywhere), and the decision is then
    minted genuinely at that watermark by the next delta append.
    """
    provider_root, rollout, head, tail = _split_growing_rollout(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")
    key_b = _account_key_for("acct-beta", "beta@example.com")
    mid = len(head.encode())

    import _cctally_cache as cc
    cache = ns["open_cache_db"]()
    try:
        # A pre-#416 ingest: the watermark advances, nothing is decided.
        with monkeypatch.context() as pre416:
            pre416.setattr(cc, "_append_codex_file_account_decision",
                           lambda **_kw: None)
            pre416.setattr(cc, "record_codex_file_account", lambda *_a, **_k: None)
            ns["sync_codex_cache"](cache)
        assert _map_rows(cache) == [], "precondition: no decision exists yet"
        assert _journal_ops(ns, "codex_file_account") == []
        assert cache.execute(
            "SELECT last_byte_offset FROM codex_session_files WHERE path=?",
            (str(rollout),)).fetchone()[0] == mid

        # #416 ships; the file grows, so the FIRST decision is minted at the
        # existing watermark rather than at byte zero.
        rollout.write_text(head + tail)
        ns["sync_codex_cache"](cache)
        assert [(r[2], r[4]) for r in _map_rows(cache)] == [(mid, key_a)]

        # cache.db is afterwards recreated: cursor and derived rows gone, the
        # journal-backed map intact.
        cache.execute("DELETE FROM codex_session_entries")
        cache.execute("DELETE FROM quota_window_snapshots WHERE source = 'codex'")
        cache.execute("DELETE FROM codex_session_files")
        cache.commit()

        (provider_root / "auth.json").write_text(
            _auth_json("acct-beta", "beta@example.com"))
        ns["sync_codex_cache"](cache)

        rows = {
            int(r[0]): r[1] for r in cache.execute(
                "SELECT line_offset, account_key FROM codex_session_entries")
        }
        assert rows, "the re-read must have re-ingested the file"
        decided = {v for k, v in rows.items() if k >= mid}
        undecided = {v for k, v in rows.items() if k < mid}
        assert decided and undecided, "the split must straddle the decision"
        assert decided == {key_a}, (
            "bytes covered by the durable decision resolved through an "
            f"out-of-order range list: {decided} (expected {key_a})")
        assert undecided == {None}, (
            "the undecided pre-#416 prefix was inferred from the live "
            f"auth.json: {undecided} (D1 forbids inference; expected NULL)")
        assert {r[0] for r in cache.execute(
            "SELECT DISTINCT account_key FROM codex_file_accounts")} == {key_a}, (
            "a second decision was minted over already-decided history")
        assert key_b not in {
            r[0] for r in cache.execute(
                "SELECT DISTINCT account_key FROM codex_session_entries")}
    finally:
        cache.close()


# --------------------------------------------------------------------------
# Slice 1 review round — P2-4
#
# The D1 "never re-infer pre-#416 history" guard snapshots
# `SELECT path FROM codex_session_files` and compares it against
# `str(discovered.source_path)` — the first CONFIGURED candidate spelling, which
# review finding F12 already established is unstable across `$CODEX_HOME`
# reordering and symlink respelling. A respelling between the last pre-#416
# ingest and the remedial `cache-sync --rebuild` therefore drops the file out of
# the set, sends it to the auth.json branch, and re-stamps undecided history
# with the current account — the exact violation the branch exists to prevent.
# --------------------------------------------------------------------------

def test_d1_history_guard_survives_a_root_respelling(ns, monkeypatch, tmp_path):
    real_root = tmp_path / "real"
    rollout = real_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(ROLLOUT_FIXTURE, rollout)
    (real_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    link_root = tmp_path / "link"
    link_root.symlink_to(real_root, target_is_directory=True)
    key_b = _account_key_for("acct-beta", "beta@example.com")

    cache = ns["open_cache_db"]()
    try:
        # A pre-#416 install: the cursor advanced under the LINK spelling and no
        # attribution decision was ever journalled for these bytes.
        monkeypatch.setenv("CODEX_HOME", str(link_root))
        ns["sync_codex_cache"](cache)
        stored = {r[0] for r in cache.execute(
            "SELECT path FROM codex_session_files")}
        assert stored == {
            str(link_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl")}, (
            "precondition: the cursor is keyed on the configured link spelling")
        shutil.rmtree(ns["_cctally_core"].JOURNAL_DIR)
        cache.execute("DELETE FROM codex_file_accounts")
        cache.execute("UPDATE codex_session_entries SET account_key = NULL")
        cache.execute(
            "UPDATE quota_window_snapshots SET account_key = NULL "
            "WHERE source = 'codex'")
        cache.commit()

        # The operator respells $CODEX_HOME, then runs the remedial rebuild
        # under a different account.
        monkeypatch.setenv("CODEX_HOME", str(real_root))
        (real_root / "auth.json").write_text(
            _auth_json("acct-beta", "beta@example.com"))
        ns["sync_codex_cache"](cache, rebuild=True)

        after = {r[0] for r in cache.execute(
            "SELECT DISTINCT account_key FROM codex_session_entries")}
        assert after == {None}, (
            "a root respelling dropped the file out of the D1 history snapshot "
            f"and re-stamped never-decided history: {after} (expected NULL)")
        assert key_b not in after
        assert _map_rows(cache) == [], (
            "a decision was minted over history that was never durably stamped")
    finally:
        cache.close()


# --------------------------------------------------------------------------
# Slice 1 review round — P2-3
#
# `_QUOTA_SNAPSHOT_UPSERT` was built by REPLACING `INSERT OR IGNORE` with a
# plain `INSERT`, which loses the conflict tolerance the uncovered path still
# has. `quota_window_snapshots` carries several CHECK and NOT NULL constraints,
# so a violating record that was previously dropped silently now raises
# `IntegrityError`, which `_cache_applier` catches as `sqlite3.Error` ->
# rollback -> prefix-stop. The cursor never advances past the offending record,
# so ONE permanently-violating record wedges the entire journal ingest cycle
# forever — not just Codex.
# --------------------------------------------------------------------------

def _bad_quota_obs(jl, *, path, offset, account=None, **overrides):
    obs = _quota_obs(jl, root="root-a", path=str(path), offset=offset,
                     account=account)
    obs["payload"].update(overrides)
    return obs


@pytest.mark.parametrize("field,value", [
    ("used_percent", 250.0),      # CHECK(used_percent BETWEEN 0 AND 100)
    ("window_minutes", 0),        # CHECK(window_minutes > 0)
    ("logical_limit_key", None),  # NOT NULL
    ("captured_at_utc", None),    # NOT NULL
])
def test_covered_quota_obs_tolerates_a_constraint_violation(
        ns, tmp_path, field, value):
    """A violating record on the COVERED path must be dropped exactly as the
    uncovered `INSERT OR IGNORE` path drops it — never raise, never prefix-stop,
    and never wedge the cursor. The following well-formed record must still land.
    """
    import _lib_journal as jl
    import _cctally_journal as jr
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    cache = ns["open_cache_db"]()
    try:
        _seed_decision(cache, path=rollout, account_key=KEY_A)
    finally:
        cache.close()

    bad = _bad_quota_obs(jl, path=rollout, offset=10, account=KEY_B,
                         **{field: value})
    good = _quota_obs(jl, root="root-a", path=str(rollout), offset=20,
                      account=KEY_B)
    stop = jr._cache_applier([(bad, "seg", 0), (good, "seg", 100)])
    assert stop is None, (
        f"a record violating {field} prefix-stopped the whole cache leg at "
        f"index {stop}; the journal cursor can never advance past it")
    assert _quota_account_in_cache(ns, rollout, 10) == "<missing>"
    assert _quota_account_in_cache(ns, rollout, 20) == KEY_A


def test_covered_quota_obs_upsert_still_converges(ns, tmp_path):
    """Non-vacuity guard for the fix above: restoring `INSERT OR IGNORE` must
    NOT disarm the targeted-conflict `DO UPDATE`. SQLite gives the upsert clause
    precedence for the conflict it names, so an existing unstamped row is still
    converged onto the decision."""
    import _lib_journal as jl
    import _cctally_journal as jr
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    ns["open_cache_db"]().close()
    # An unstamped row lands first (no decision covers it yet).
    assert jr._cache_applier([(_quota_obs(
        jl, root="root-a", path=str(rollout), offset=10, account=None),
        "seg", 0)]) is None
    assert _quota_account_in_cache(ns, rollout, 10) is None
    cache = ns["open_cache_db"]()
    try:
        _seed_decision(cache, path=rollout, account_key=KEY_A)
    finally:
        cache.close()
    assert jr._cache_applier([(_quota_obs(
        jl, root="root-a", path=str(rollout), offset=10, account=None),
        "seg", 0)]) is None
    assert _quota_account_in_cache(ns, rollout, 10) == KEY_A


# --------------------------------------------------------------------------
# Slice 1 review round — P2-1: spec section 3.3's mid-file second range decision
#
# "A mid-file account change appends a second range-qualified op; the first is
# never rewritten." The data model and both read paths already supported ranges;
# only the MINTING was absent, so a decision at `from_offset = 0` covered every
# future byte and a rollout that outlived an account switch inherited the old
# account forever. The claimed mitigation ("a switch starts a new session, so a
# new rollout, so a new identity") covers the common restart path but NOT a
# long-running session whose file keeps growing after `codex login`.
#
# The two cases below are the whole point of the fix and must be read together:
# minting a range for genuinely NEW bytes is the feature, and re-deciding bytes
# that are already covered is the original defect.
# --------------------------------------------------------------------------

TURN_CONTRACT_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
    / "session-a-turn-contract.jsonl"
)


def _split_growing_rollout(tmp_path):
    """A rollout delivered in two appends, split between token_count events."""
    lines = TURN_CONTRACT_FIXTURE.read_text().splitlines(keepends=True)
    head, tail = "".join(lines[:28]), "".join(lines[28:])
    provider_root = tmp_path / "codex-provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(head)
    return provider_root, rollout, head, tail


def test_account_switch_mid_file_mints_a_second_range(ns, monkeypatch, tmp_path):
    """New bytes appended after `codex login` carry the NEW account, while the
    bytes decided under the previous login keep theirs."""
    provider_root, rollout, head, tail = _split_growing_rollout(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")
    key_b = _account_key_for("acct-beta", "beta@example.com")
    boundary = len(head.encode())

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        first = {int(r[0]): r[1] for r in cache.execute(
            "SELECT line_offset, account_key FROM codex_session_entries")}
        assert first and set(first.values()) == {key_a}
        assert max(first) < boundary

        # The operator runs `codex login` as another account; the SAME rollout
        # keeps growing.
        (provider_root / "auth.json").write_text(
            _auth_json("acct-beta", "beta@example.com"))
        rollout.write_text(head + tail)
        ns["sync_codex_cache"](cache)

        rows = {int(r[0]): r[1] for r in cache.execute(
            "SELECT line_offset, account_key FROM codex_session_entries")}
        old = {v for k, v in rows.items() if k < boundary}
        new = {v for k, v in rows.items() if k >= boundary}
        assert old == {key_a}, f"already-decided bytes were re-stamped: {old}"
        assert new == {key_b}, (
            "bytes appended after the account switch inherited the previous "
            f"account: {new} (expected {key_b})")
        # Bind the identity independently instead of reading it back out of the
        # rows under test (which compared two columns against themselves and
        # raised IndexError on an empty map).
        from _lib_source_identity import codex_file_key, source_root_key
        root_key = source_root_key(str(provider_root))
        identity = codex_file_key(root_key, str(rollout.resolve()))
        map_rows = _map_rows(cache)
        assert map_rows == [
            (identity, 1, 0, root_key, key_a),
            (identity, 1, boundary, root_key, key_b),
        ], map_rows
        ops = _journal_ops(ns, "codex_file_account")
        assert [(o["payload"]["from_offset"], o["payload"].get("account_key"))
                for o in ops] == [(0, key_a), (boundary, key_b)], ops
    finally:
        cache.close()


def test_unchanged_account_mints_no_second_range(ns, monkeypatch, tmp_path):
    """Non-vacuity partner: a growing file under the SAME login must not append
    a redundant op on every delta sync."""
    provider_root, rollout, head, tail = _split_growing_rollout(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        rollout.write_text(head + tail)
        ns["sync_codex_cache"](cache)
        assert len(_map_rows(cache)) == 1, _map_rows(cache)
        assert len(_journal_ops(ns, "codex_file_account")) == 1
        assert {r[0] for r in cache.execute(
            "SELECT DISTINCT account_key FROM codex_session_entries")} == {key_a}
    finally:
        cache.close()


def test_a_re_read_never_mints_a_range_over_decided_bytes(ns, monkeypatch, tmp_path):
    """The counter-case that the mint must NOT reintroduce: re-reading a decided
    file from offset zero under a different login is the ORIGINAL defect. The
    live auth.json may only mint a range starting at the current append offset,
    never re-decide bytes a decision already covers."""
    provider_root, rollout, head, tail = _split_growing_rollout(tmp_path)
    rollout.write_text(head + tail)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        # cache.db loses its cursor (recreation / rebuild), the operator is now
        # signed in as someone else, and the whole file is re-read.
        cache.execute("DELETE FROM codex_session_files")
        cache.execute("DELETE FROM codex_session_entries")
        cache.commit()
        (provider_root / "auth.json").write_text(
            _auth_json("acct-beta", "beta@example.com"))
        ns["sync_codex_cache"](cache)

        assert {r[0] for r in cache.execute(
            "SELECT DISTINCT account_key FROM codex_session_entries")} == {key_a}
        assert len(_map_rows(cache)) == 1, _map_rows(cache)
        assert len(_journal_ops(ns, "codex_file_account")) == 1
    finally:
        cache.close()


def test_attribution_conflict_warning_is_one_line_per_file(ns, tmp_path, capfd):
    """Once §3.3 minting exists, a mid-file switch legitimately produces a RUN
    of observations whose first-stamp-wins account disagrees with the range
    decision now governing those bytes. The warning stays (the condition has a
    remedy and is worth reporting) but must not emit one line per row."""
    import _lib_journal as jl
    import _cctally_journal as jr
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    cache = ns["open_cache_db"]()
    try:
        _seed_decision(cache, path=rollout, account_key=KEY_A)
    finally:
        cache.close()
    capfd.readouterr()
    jr._cache_applier([
        (_quota_obs(jl, root="root-a", path=str(rollout), offset=off,
                    account=KEY_B), "seg", off)
        for off in (10, 20, 30, 40, 50)
    ])
    err = capfd.readouterr().err
    assert err.count("attribution conflict") == 1, err


# --------------------------------------------------------------------------
# Slice 1 fix-round review — B1 / B2 / B3
#
# B1: the fix round made the journal-to-map replay AUTHORITATIVE under
# `--rebuild`, which exposed a latent divergence the additive replay had been
# silently masking. The rehydration was gated on a one-shot
# `codex_attribution_rehydrated_at` marker, so a file whose cache write FAILED
# after its decision was already journaled left a journaled-but-unapplied
# decision that the retry sync never replayed — the retry re-decided from the
# live `auth.json` instead, and a later `cache-sync --rebuild` then FLIPPED the
# attribution back to the journaled decision. Spec §3.6 requires the opposite:
# "a crash after append but before the cache-map commit is recovered by
# replaying pending journal state under the same locked operation BEFORE
# auth.json is consulted on retry".
# --------------------------------------------------------------------------


def _decisions(ns) -> list:
    return [(o["payload"]["from_offset"], o["payload"].get("account_key"))
            for o in _journal_ops(ns, "codex_file_account")]


def _entry_accounts(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT account_key FROM codex_session_entries")}


def test_a_failed_cache_write_is_replayed_before_auth_is_consulted(
        ns, monkeypatch, tmp_path):
    """Three-step reproduction of the fix-round B1 defect.

    1. Sync under account A; the per-file batch write fails on both attempts, so
       the decision is journaled and NOTHING is materialized.
    2. The operator switches to account B and syncs again. The retry must replay
       the pending journalled decision (A) instead of re-deciding from the live
       auth.json — otherwise the journal gains a SECOND, contradictory op at the
       same `(file_identity, incarnation, from_offset)`.
    3. `cache-sync --rebuild` must not change attribution (acceptance
       criterion 4). The authoritative replay is clear-then-replay with a
       first-wins conflict clause, so a step-2 regression that mints a SECOND,
       contradictory op at the same `(file_identity, incarnation, from_offset)`
       cannot cement itself: the rebuild lands the original decision. Step 2's
       own assertion is what proves the second op was never minted; this step
       proves the remedy stays correct even if it had been.
    """
    provider_root, _rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")
    key_b = _account_key_for("acct-beta", "beta@example.com")

    import _cctally_cache as cc
    real_write = cc._write_codex_file_batch

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    cache = ns["open_cache_db"]()
    try:
        # Step 1 — the decision is journalled, the cache write fails.
        monkeypatch.setattr(cc, "_write_codex_file_batch", locked)
        first = ns["sync_codex_cache"](cache)
        assert first.files_failed == 1
        assert first.files_processed == 0
        assert _map_rows(cache) == [], "precondition: nothing materialized"
        assert _decisions(ns) == [(0, key_a)], "precondition: decision journalled"

        # Step 2 — the operator switches accounts; the write now succeeds.
        monkeypatch.setattr(cc, "_write_codex_file_batch", real_write)
        (provider_root / "auth.json").write_text(
            _auth_json("acct-beta", "beta@example.com"))
        second = ns["sync_codex_cache"](cache)
        assert second.files_processed == 1

        assert _decisions(ns) == [(0, key_a)], (
            "the retry re-decided from the live auth.json instead of replaying "
            "the pending journalled decision")
        assert [(r[2], r[4]) for r in _map_rows(cache)] == [(0, key_a)]
        assert _entry_accounts(cache) == {key_a}

        # Step 3 — acceptance criterion 4.
        ns["sync_codex_cache"](cache, rebuild=True)
        assert [(r[2], r[4]) for r in _map_rows(cache)] == [(0, key_a)]
        assert _entry_accounts(cache) == {key_a}, (
            "cache-sync --rebuild changed account attribution")
    finally:
        cache.close()


def test_pending_journal_state_survives_a_hard_crash_between_append_and_commit(
        ns, monkeypatch, tmp_path):
    """The same recovery, reached the way spec §3.6 actually words it: a CRASH
    between the journal append and the cache commit. Nothing gets a chance to
    clean up a marker on the way out, so the recovery cannot depend on the
    failing sync noticing its own failure."""
    provider_root, _rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")

    import _cctally_cache as cc
    real_write = cc._write_codex_file_batch

    class _Crash(BaseException):
        """Not an Exception — nothing in the walk may catch it."""

    def crash(*_a, **_k):
        raise _Crash()

    cache = ns["open_cache_db"]()
    try:
        monkeypatch.setattr(cc, "_write_codex_file_batch", crash)
        with pytest.raises(_Crash):
            ns["sync_codex_cache"](cache)
        assert _map_rows(cache) == []
        assert _decisions(ns) == [(0, key_a)]
    finally:
        cache.close()

    # A fresh process, a different login, the same cache.db.
    monkeypatch.setattr(cc, "_write_codex_file_batch", real_write)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-beta", "beta@example.com"))
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        assert _decisions(ns) == [(0, key_a)], (
            "the post-crash sync re-decided from the live auth.json")
        assert _entry_accounts(cache) == {key_a}
    finally:
        cache.close()


# --------------------------------------------------------------------------
# B2 — the rehydration is now on the hot path (every first sync of every
# cache.db, including hook-tick and the corruption auto-heal), so it must not
# materialize the whole journal. Both properties are pinned: it must not build
# a full-range list, and its allocation must stay bounded well below the
# journal's size.
# --------------------------------------------------------------------------

def _seed_journal_segment(ns, *, filler_bytes: int, ops: list) -> None:
    """Write one real journal segment directly: `filler_bytes` of decodable
    non-Codex records, then the given `codex_file_account` ops."""
    import _lib_journal as jl
    journal_dir = ns["_cctally_core"].JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    seg = journal_dir / "observations-2026-07.jsonl"
    written = 0
    with open(seg, "wb") as fh:
        i = 0
        while written < filler_bytes:
            line = jl.encode_line({
                "t": "obs", "provider": "claude",
                "at": "2026-07-20T00:00:00Z",
                "payload": {"kind": "filler", "i": i, "blob": "x" * 900},
            })
            fh.write(line)
            written += len(line)
            i += 1
        for op in ops:
            fh.write(jl.encode_line(op))


def _decision_op(*, file_identity, from_offset, account_key):
    payload = {"kind": "codex_file_account", "file_identity": file_identity,
               "incarnation": 1, "from_offset": from_offset, "root_scope": "rk"}
    if account_key is not None:
        payload["account_key"] = account_key
    return {"t": "op", "at": "2026-07-20T01:00:00Z", "payload": payload}


def test_rehydration_never_materializes_the_whole_journal(ns, cache_conn):
    """`_read_range` returns a Python list of every decoded line in the range.
    Calling it from the rehydration puts a several-hundred-MB transient on the
    hot path of every first sync. The rehydration must stream instead."""
    import _cctally_journal as jr
    _seed_journal_segment(ns, filler_bytes=200_000, ops=[
        _decision_op(file_identity="fid1", from_offset=0, account_key=KEY_A)])

    def forbidden(*_a, **_k):
        raise AssertionError("rehydration built a full-journal list")

    saved = jr._read_range
    jr._read_range = forbidden
    try:
        applied, _hw, _declined = jr.rehydrate_codex_file_accounts(cache_conn)
    finally:
        jr._read_range = saved
    cache_conn.commit()
    assert applied == 1
    assert [(r[2], r[4]) for r in _map_rows(cache_conn)] == [(0, KEY_A)]


def test_rehydration_allocation_stays_bounded(ns, cache_conn):
    """Measurement partner: the transient must not scale with the journal."""
    import tracemalloc
    import _cctally_journal as jr
    filler = 8 << 20
    _seed_journal_segment(ns, filler_bytes=filler, ops=[
        _decision_op(file_identity="fid1", from_offset=0, account_key=KEY_A)])

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        applied, _hw, _declined = jr.rehydrate_codex_file_accounts(cache_conn)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    cache_conn.commit()
    assert applied == 1
    assert peak < 4 << 20, (
        f"rehydrating an {filler >> 20} MiB journal allocated {peak >> 20} MiB")


# --------------------------------------------------------------------------
# B3 — the mint boundary is `>`, not `>=`. `record_codex_file_account` keeps the
# existing row on conflict, so minting AT an already-decided offset leaves the
# map on the old account while the journal gains a contradictory op at the same
# primary key: B1's divergence class with no write failure at all.
# --------------------------------------------------------------------------

def test_the_mint_boundary_excludes_an_already_decided_offset(
        ns, monkeypatch, tmp_path):
    provider_root, rollout, head, tail = _split_growing_rollout(tmp_path)
    (provider_root / "auth.json").write_text(
        _auth_json("acct-alpha", "alpha@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    key_a = _account_key_for("acct-alpha", "alpha@example.com")
    key_b = _account_key_for("acct-beta", "beta@example.com")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        watermark = cache.execute(
            "SELECT last_byte_offset FROM codex_session_files WHERE path=?",
            (str(rollout),)).fetchone()[0]
        assert watermark > 0
        identity = _map_rows(cache)[0][0]

        # A decision already covers the file's current watermark, so the next
        # delta append starts exactly AT a decided offset.
        import _cctally_cache as cc
        cc.record_codex_file_account(
            cache, file_identity=identity, incarnation=1,
            from_offset=watermark, root_scope=_map_rows(cache)[0][3],
            account_key=key_a, decided_at_utc="2026-07-28T00:00:00Z")
        cache.commit()

        (provider_root / "auth.json").write_text(
            _auth_json("acct-beta", "beta@example.com"))
        rollout.write_text(head + tail)
        ns["sync_codex_cache"](cache)

        assert [(r[2], r[4]) for r in _map_rows(cache)] == [
            (0, key_a), (watermark, key_a)], _map_rows(cache)
        assert _decisions(ns) == [(0, key_a)], (
            "a second, contradictory decision was journalled at an offset a "
            "decision already covers")
    finally:
        cache.close()


# --------------------------------------------------------------------------
# B4 — a persistently torn `auth.json` (a truncated or half-written file, not
# just a mid-write race) now defers EVERY growing rollout under that root, not
# only the never-decided ones. `files_deferred_torn` was counted and rendered
# nowhere, so Codex spend and quota silently stopped updating while `cache-sync`
# exited 0. The defer is correct; the silence is not.
#
# This is a health signal, not account decoration, so it is NOT R8-gated — it
# names no account and adds no per-account column, exactly like the `alerts.log`
# runtime-state carve-out in `docs/accounts-gotchas.md`.
# --------------------------------------------------------------------------

TORN_MARKER_KEY = "codex_torn_auth_deferred"


def _cache_meta_value(conn, key):
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def test_a_torn_root_records_a_durable_deferral_marker(ns, monkeypatch, tmp_path):
    provider_root, rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text("{ half-written")
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
        assert stats.files_deferred_torn == 1
        assert stats.files_processed == 0
        raw = _cache_meta_value(cache, TORN_MARKER_KEY)
        assert raw is not None, (
            "a persistently torn auth.json halted Codex ingest with no durable "
            "signal for `doctor` to report")
        assert json.loads(raw)["files"] == 1

        # A clean read clears it — the marker must not outlive the condition.
        (provider_root / "auth.json").write_text(
            _auth_json("acct-alpha", "alpha@example.com"))
        ns["sync_codex_cache"](cache)
        assert _cache_meta_value(cache, TORN_MARKER_KEY) is None
    finally:
        cache.close()


def test_a_targeted_sync_never_clears_the_torn_marker(ns, monkeypatch, tmp_path):
    """A targeted (`only_paths`) sync sees a handful of files, so its zero
    deferral count says nothing about the rest of the tree."""
    provider_root, rollout = _codex_root(tmp_path)
    (provider_root / "auth.json").write_text("{ half-written")
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        assert _cache_meta_value(cache, TORN_MARKER_KEY) is not None
        ns["sync_codex_cache"](cache, only_paths={str(tmp_path / "absent.jsonl")})
        assert _cache_meta_value(cache, TORN_MARKER_KEY) is not None
    finally:
        cache.close()


def test_cache_sync_reports_a_torn_deferral_on_stderr(ns, tmp_path):
    """End to end, under the full three-variable isolation: the operator must
    not have to read `--json` to learn that ingest stopped."""
    env, data, _codex_home, _rollout = _isolated_codex_world(tmp_path)
    codex_home = pathlib.Path(env["CODEX_HOME"])
    (codex_home / "auth.json").write_text("{ half-written")

    run = _cache_sync(env)
    assert run.returncode == 0, run.stderr
    assert "torn" in run.stderr, run.stderr
    conn = sqlite3.connect(data / "cache.db")
    try:
        assert _cache_meta_value(conn, TORN_MARKER_KEY) is not None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Slice 1 closeout review — C1/C2/C3.
#
# C1: the replay's duplicate-primary-key policy. Spec section 3.3 says "the
# first is never rewritten" and section 3.5 says "a genuine correction is
# expressed as an explicit new range decision, not by mutating history", so the
# replay must be FIRST-WINS. Last-op-wins inverts acceptance criterion 4 on the
# one path where duplicates are reachable (a failed rehydration lets the walk
# re-decide from the live auth.json, minting a second op at the same key):
# `cache-sync --rebuild` would then cement the NEWER live-auth-derived value
# instead of restoring the original.
#
# C3: the `prior is None` probe runs BEFORE the insert, so a record dropped by a
# non-primary-key constraint still counts as restored AND still raises the
# incarnation high-water. An inflated incarnation is the DANGEROUS direction —
# ranges resolve at exactly the walk's current incarnation, so the range list
# comes back empty, `covered` is False, and a plain sync falls through to the
# live auth.json.
# --------------------------------------------------------------------------

def _raw_decision_op(**payload_overrides) -> dict:
    """A `codex_file_account` op whose payload is built member by member, so a
    test can OMIT a member the schema requires."""
    payload = {"kind": "codex_file_account"}
    payload.update(payload_overrides)
    return {"t": "op", "at": "2026-07-20T01:00:00Z", "payload": payload}


def _incarnation_rows(conn) -> list:
    return conn.execute(
        "SELECT file_identity, incarnation FROM codex_file_incarnations "
        "ORDER BY file_identity").fetchall()


def test_additive_replay_keeps_the_first_decision_at_a_contended_key(
        ns, cache_conn):
    """Two ops at one `(file_identity, incarnation, from_offset)`: the FIRST is
    authoritative and the second is declined (spec section 3.3)."""
    import _cctally_journal as jr
    _seed_journal_segment(ns, filler_bytes=0, ops=[
        _decision_op(file_identity="fid1", from_offset=0, account_key=KEY_A),
        _decision_op(file_identity="fid1", from_offset=0, account_key=KEY_B),
    ])
    jr.rehydrate_codex_file_accounts(cache_conn)
    cache_conn.commit()
    assert [(r[2], r[4]) for r in _map_rows(cache_conn)] == [(0, KEY_A)], (
        "the replay rewrote the first decision with a later contradicting one")


def test_authoritative_replay_keeps_the_first_decision_at_a_contended_key(
        ns, cache_conn):
    """`cache-sync --rebuild` must RESTORE the original decision, not cement the
    newer live-auth-derived one — acceptance criterion 4."""
    import _cctally_journal as jr
    _seed_journal_segment(ns, filler_bytes=0, ops=[
        _decision_op(file_identity="fid1", from_offset=0, account_key=KEY_A),
        _decision_op(file_identity="fid1", from_offset=0, account_key=KEY_B),
    ])
    jr.rehydrate_codex_file_accounts(cache_conn, authoritative=True)
    cache_conn.commit()
    assert [(r[2], r[4]) for r in _map_rows(cache_conn)] == [(0, KEY_A)], (
        "--rebuild cemented the newer decision instead of restoring the first")


def test_authoritative_replay_still_repairs_a_drifted_row(ns, cache_conn):
    """GUARD-RAIL, not new coverage: it passes before and after C1.

    It exists because C1's safety argument depends on it — first-wins is only
    safe on the authoritative path because the `DELETE FROM codex_file_accounts`
    runs first, so `DO NOTHING` is first-WINS rather than a no-op. Deleting that
    clear (or "simplifying" the authoritative branch away) turns this red.
    """
    import _cctally_journal as jr
    _seed_journal_segment(ns, filler_bytes=0, ops=[
        _decision_op(file_identity="fid1", from_offset=0, account_key=KEY_A)])
    jr.rehydrate_codex_file_accounts(cache_conn)
    cache_conn.execute("UPDATE codex_file_accounts SET account_key = ?", (KEY_B,))
    cache_conn.commit()
    jr.rehydrate_codex_file_accounts(cache_conn, authoritative=True)
    cache_conn.commit()
    assert [(r[2], r[4]) for r in _map_rows(cache_conn)] == [(0, KEY_A)]


def test_a_dropped_decision_never_raises_the_incarnation_high_water(
        ns, cache_conn):
    """C3. `root_scope` is NOT NULL on `codex_file_accounts` and absent from
    `codex_file_incarnations`, so an op omitting it is silently dropped by the
    map insert while the incarnation insert still lands. The counter would then
    sit ABOVE any committed batch, the walk's range lookup would come back
    empty, and a plain sync would re-decide from the live auth.json."""
    import _cctally_journal as jr
    _seed_journal_segment(ns, filler_bytes=0, ops=[
        _raw_decision_op(file_identity="fid1", incarnation=7, from_offset=0,
                         account_key=KEY_A)])
    applied, _hw, _declined = jr.rehydrate_codex_file_accounts(cache_conn)
    cache_conn.commit()
    assert _map_rows(cache_conn) == [], "precondition: the map insert was dropped"
    assert _incarnation_rows(cache_conn) == [], (
        "a dropped decision raised the incarnation high-water")
    assert applied == 0, (
        f"a dropped decision was counted as a restored decision ({applied})")


def test_the_incarnation_insert_tolerates_a_constraint_violation(ns, cache_conn):
    """C2, pinned at the STATEMENT level.

    `_FILE_ACCOUNT_INSERT` carries `OR IGNORE` and says why in its own comment:
    an `IntegrityError` inside `_apply_file_account_records` prefix-stops
    `_cache_applier`, and the scalar cursor can never advance past the record,
    wedging the journal ingest cycle for EVERY provider. Its sibling
    `_FILE_INCARNATION_INSERT` lacked it. C3's landed-check makes the end-to-end
    path unreachable (the incarnation table's NOT NULLs are a strict subset of
    the map's), so the asymmetry is pinned where it is observable rather than
    through a scenario that no longer exists.
    """
    import _cctally_journal as jr
    cache_conn.execute(jr._FILE_INCARNATION_INSERT, (None, 1, "2026-07-20T00:00:00Z"))
    cache_conn.rollback()
