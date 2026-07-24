"""`--account <ref>` render filter wiring (#341 Task 3 slice C, spec §3).

The stamped-entry analytics family (daily/monthly/session/project/diff/
range-cost/cache-report) accepts `--account`, resolves it through the landed
`resolve_account_filter` chokepoint, threads the key into the entry reads, and
decorates the JSON (R8, under an explicit account-aware invocation). Source-aware
commands reject `--account` with `--source {codex,all}` (exit 2); the whole
family fails closed (exit 3) when the entry cache is unavailable.
"""
from __future__ import annotations

import json
import sys

import pytest

import _cctally_core
from conftest import load_script, redirect_paths


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # main() fires the detached update-check / telemetry-beat workers post-command;
    # disable both so the CLI-driving tests don't spawn subprocesses (ResourceWarnings
    # that could leak into a later test's capsys).
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    return sys.modules["cctally"]


def _key(uuid):
    import _lib_accounts
    return _lib_accounts.account_key("claude", uuid)


def _seed_registry(observes):
    import _cctally_journal as jr
    import _lib_journal as lj
    for kw in observes:
        jr.append_record(lj.make_account_observe(**kw))
    jr.rebuild_stats_index()


def _two_accounts():
    ka, kb = _key("uuid-A"), _key("uuid-B")
    _seed_registry([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             natural_id="uuid-A", email="alice@x.com", plan_type="max",
             label="alice", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=kb, provider="claude",
             natural_id="uuid-B", email="bob@x.com", plan_type="pro",
             label="bob", label_source="auto"),
    ])
    return ka, kb


def _seed_entries(app, rows):
    """rows = [(msg_id, cost_usd_raw, account_key)] at a fixed in-range time."""
    conn = app.open_cache_db()
    try:
        for msg_id, cost, account_key in rows:
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, msg_id, req_id, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cost_usd_raw, account_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"/p/{msg_id}.jsonl", 0, "2026-05-22T12:00:00Z",
                 "claude-opus-4-7", msg_id, "r-" + msg_id,
                 10, 20, 0, 0, cost, account_key),
            )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# parser presence
# --------------------------------------------------------------------------

def test_all_stamped_entry_commands_accept_account(app):
    p = app.build_parser()
    for argv in (
        ["daily", "--account", "alice"],
        ["monthly", "--account", "alice"],
        ["session", "--account", "alice"],
        ["project", "--account", "alice"],
        ["range-cost", "--start", "2026-05-22", "--account", "alice"],
        ["cache-report", "--account", "alice"],
        ["diff", "--a", "today", "--b", "yesterday", "--account", "alice"],
    ):
        ns = p.parse_args(argv)
        assert ns.account == "alice", argv
    # Flat and subgroup forms carry the same flag.
    assert p.parse_args(["claude", "daily", "--account", "alice"]).account == "alice"


def test_stats_family_commands_accept_account(app):
    """#341 slice D — the Claude usage/milestone stats family gains `--account`."""
    p = app.build_parser()
    for argv in (
        ["report", "--account", "alice"],
        ["forecast", "--account", "alice"],
        ["weekly", "--account", "alice"],
        ["percent-breakdown", "--account", "alice"],
        ["five-hour-blocks", "--account", "alice"],
        ["five-hour-breakdown", "--account", "alice"],
    ):
        ns = p.parse_args(argv)
        assert ns.account == "alice", argv
    # Flat and subgroup forms carry the same flag (build-once, register-twice).
    assert p.parse_args(
        ["claude", "weekly", "--account", "alice"]).account == "alice"


def test_codex_quota_views_accept_account(app):
    """#341 slice D — all 5 `codex quota` leaves gain `--account` (provider=codex)."""
    p = app.build_parser()
    for leaf, extra in (
        ("history", []),
        ("statusline", []),
        ("forecast", []),
        ("blocks", []),
        ("breakdown", ["--reset-at", "2026-05-22T12:00:00Z"]),
    ):
        ns = p.parse_args(["codex", "quota", leaf, "--account", "alice"] + extra)
        assert ns.account == "alice", leaf


# --------------------------------------------------------------------------
# resolve_account_filter chokepoint
# --------------------------------------------------------------------------

def test_bad_ref_exits_2(app, capsys):
    _two_accounts()
    import argparse
    args = argparse.Namespace(account="nope-nobody")
    key, code = app.resolve_account_filter(args, "claude", needs_cache=True)
    assert key is None and code == 2
    assert "ambiguous or unknown" in capsys.readouterr().err


def test_no_flag_is_merged(app):
    import argparse
    args = argparse.Namespace(account=None)
    assert app.resolve_account_filter(args, "claude", needs_cache=True) == (None, None)


def test_needs_cache_unavailable_exits_3(app, monkeypatch, capsys):
    ka, _kb = _two_accounts()
    import argparse
    import _cctally_cache

    def boom():
        raise OSError("cache gone")
    monkeypatch.setattr(_cctally_cache, "open_cache_db", boom)
    args = argparse.Namespace(account="alice")
    key, code = app.resolve_account_filter(args, "claude", needs_cache=True)
    assert key is None and code == 3
    assert "cache required" in capsys.readouterr().err


# --------------------------------------------------------------------------
# end-to-end filter (range-cost --json) + R8 decoration
# --------------------------------------------------------------------------

def test_range_cost_account_filter_scopes_cost(app, capsys):
    ka, kb = _two_accounts()
    _seed_entries(app, [("mA", 1.0, ka), ("mB", 2.0, kb)])

    # Merged: A+B = $3.
    rc = app.main(["range-cost", "--start", "2026-05-22", "--end", "2026-05-23",
                   "--json"])
    assert rc == 0
    merged = json.loads(capsys.readouterr().out)
    assert abs(merged["totalCostUSD"] - 3.0) < 1e-9
    assert "accountKey" not in merged  # R8: no decoration without --account

    # Scoped to alice: A only = $1, with R8 decoration.
    rc = app.main(["range-cost", "--start", "2026-05-22", "--end", "2026-05-23",
                   "--json", "--account", "alice"])
    assert rc == 0
    scoped = json.loads(capsys.readouterr().out)
    assert abs(scoped["totalCostUSD"] - 1.0) < 1e-9
    assert scoped["accountKey"] == ka
    assert scoped["accountLabel"] == "alice"


def test_source_codex_with_account_rejected(app, capsys):
    _two_accounts()
    rc = app.main(["range-cost", "--start", "2026-05-22", "--source", "codex",
                   "--account", "alice"])
    assert rc == 2
    assert "--account is only valid with --source claude" in capsys.readouterr().err


# --------------------------------------------------------------------------
# fail-closed on a QUERY-TIME cache degrade under --account (slice D, item 4)
# --------------------------------------------------------------------------

def test_lock_contended_under_account_fails_closed(app, monkeypatch, capsys):
    """A query-time ingest-lock contention makes ``get_entries`` fall back to a
    direct-JSONL parse that carries NO account identity. Under ``--account`` that
    would silently return unfiltered/merged (or empty) data mislabeled as the
    selected account — so it must FAIL CLOSED (exit 3), never degrade. (The
    cache-OPEN failure case is covered by resolve_account_filter's needs_cache
    gate; this is the distinct AFTER-the-gate contention window.)"""
    from types import SimpleNamespace
    ka, kb = _two_accounts()
    _seed_entries(app, [("mA", 1.0, ka), ("mB", 2.0, kb)])
    import _cctally_cache
    # Force the query-time contention branch inside get_entries / sync_cache.
    monkeypatch.setattr(_cctally_cache, "sync_cache",
                        lambda conn: SimpleNamespace(lock_contended=True))
    rc = app.main(["range-cost", "--start", "2026-05-22", "--end", "2026-05-23",
                   "--account", "alice", "--json"])
    assert rc == 3, rc
    assert "attribution unavailable" in capsys.readouterr().err


def test_lock_contended_without_account_still_degrades(app, monkeypatch, capsys):
    """Non-vacuity companion: WITHOUT --account the same contention keeps its
    existing correctness-degrade (direct-JSONL parse, exit 0) — the fail-closed
    is exclusive to the account-scoped read, so a merged read stays byte-stable."""
    from types import SimpleNamespace
    _two_accounts()
    import _cctally_cache
    monkeypatch.setattr(_cctally_cache, "sync_cache",
                        lambda conn: SimpleNamespace(lock_contended=True))
    rc = app.main(["range-cost", "--start", "2026-05-22", "--end", "2026-05-23",
                   "--json"])
    assert rc == 0, rc


# --------------------------------------------------------------------------
# Codex fail-closed symmetry (#341 Task 4, P2 load-bearing). `get_codex_entries`
# must raise ``AccountAttributionUnavailable`` on a cache degrade when the caller
# passed a real ``account_key`` — exactly like its Claude siblings
# (``get_entries`` / ``get_claude_session_entries``). The only account-scoped
# caller is ``_sum_codex_cost_for_range`` -> the Codex budget eval; a silent
# degrade there returns ALL Codex entries (the identity-less direct-JSONL parse)
# mislabeled as the selected account's spend.
# --------------------------------------------------------------------------

def _codex_range():
    import datetime as dt
    return (dt.datetime(2026, 5, 22, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 5, 23, tzinfo=dt.timezone.utc))


def test_codex_get_entries_cache_open_fail_under_account_fails_closed(app, monkeypatch):
    import sqlite3
    import _cctally_cache

    def _boom():
        raise sqlite3.DatabaseError("boom")

    monkeypatch.setattr(_cctally_cache, "open_cache_db", _boom)
    start, end = _codex_range()
    with pytest.raises(_cctally_cache.AccountAttributionUnavailable):
        _cctally_cache.get_codex_entries(start, end, account_key="acct-key")


def test_codex_get_entries_lock_contended_under_account_fails_closed(app, monkeypatch):
    from types import SimpleNamespace
    import _cctally_cache
    monkeypatch.setattr(_cctally_cache, "sync_codex_cache",
                        lambda conn: SimpleNamespace(lock_contended=True))
    start, end = _codex_range()
    with pytest.raises(_cctally_cache.AccountAttributionUnavailable):
        _cctally_cache.get_codex_entries(start, end, account_key="acct-key")


def test_codex_get_entries_cache_degrade_without_account_still_degrades(app, monkeypatch):
    """Non-vacuity: WITHOUT an ``account_key`` the same cache-open failure keeps
    the correctness-degrade (direct-JSONL parse) — the fail-closed is exclusive to
    the account-scoped read, so a merged Codex read stays byte-stable."""
    import sqlite3
    import _cctally_cache

    def _boom():
        raise sqlite3.DatabaseError("boom")

    monkeypatch.setattr(_cctally_cache, "open_cache_db", _boom)
    monkeypatch.setattr(_cctally_cache, "_collect_codex_entries_direct",
                        lambda s, e: ["SENTINEL"])
    start, end = _codex_range()
    assert _cctally_cache.get_codex_entries(start, end) == ["SENTINEL"]


# --------------------------------------------------------------------------
# codex quota --account (provider="codex") — the account filter is a pure
# history-subset over the loaded quota identities. A harness golden would need a
# codex-account registry + quota_window_snapshots + a freshness-vs-now projection
# reconcile (timestamp-fragile); this exercises the actual wiring deterministically.
# --------------------------------------------------------------------------

def _codex_key(pair):
    import _lib_accounts
    return _lib_accounts.account_key("codex", pair)


def _seed_codex_accounts():
    ka = _codex_key("acct-carol\x00carol@x.com")
    kb = _codex_key("acct-dave\x00dave@x.com")
    _seed_registry([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="codex",
             natural_id="acct-carol\x00carol@x.com", email="carol@x.com",
             plan_type="plus", label="carol", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=kb, provider="codex",
             natural_id="acct-dave\x00dave@x.com", email="dave@x.com",
             plan_type="pro", label="dave", label_source="auto"),
    ])
    return ka, kb


def test_codex_quota_account_ref_resolves_provider_codex(app, capsys):
    """`--account carol` on `codex quota` resolves against the CODEX registry
    (provider-scoped), not Claude."""
    import argparse
    ka, _kb = _seed_codex_accounts()
    key, code = app.resolve_account_filter(
        argparse.Namespace(account="carol"), "codex", needs_cache=False)
    assert code is None
    assert key == ka


def test_codex_quota_resolve_and_scope_filters_histories(app, capsys):
    """`_resolve_account_and_scope` keeps only the selected account's quota
    identities and reports the resolved key; a bad ref exits 2; no flag merges."""
    from types import SimpleNamespace
    import argparse
    import _cctally_quota as q
    ka, kb = _seed_codex_accounts()
    histories = (
        SimpleNamespace(identity=SimpleNamespace(account_key=ka)),
        SimpleNamespace(identity=SimpleNamespace(account_key=kb)),
    )
    # Resolved ref → only carol's identity survives, key returned.
    key, code, scoped = q._resolve_account_and_scope(
        argparse.Namespace(account="carol"), histories)
    assert code is None and key == ka
    assert [h.identity.account_key for h in scoped] == [ka]
    # No flag → merged (both identities, key None).
    key, code, scoped = q._resolve_account_and_scope(
        argparse.Namespace(account=None), histories)
    assert code is None and key is None
    assert len(scoped) == 2
    # Bad ref → exit 2, histories untouched.
    key, code, scoped = q._resolve_account_and_scope(
        argparse.Namespace(account="nobody-xyz"), histories)
    assert code == 2 and key is None
    assert "ambiguous or unknown" in capsys.readouterr().err


def test_codex_quota_r8_decoration_only_under_account(app):
    """R8: a payload is decorated with accountKey/accountLabel only when a real
    account is selected; the merged (None) path stays byte-identical."""
    import _cctally_quota as q
    ka, _kb = _seed_codex_accounts()
    assert q._decorate_account({"source": "codex"}, None) == {"source": "codex"}
    decorated = q._decorate_account({"source": "codex"}, ka)
    assert decorated["accountKey"] == ka
    assert decorated["accountLabel"] == "carol"
