"""`cctally account` subcommand (#341 Task 3, spec §3): list / show / label,
the JSON envelope, ref-resolution exit codes, the R8 decoration helpers, and
the durable-label round-trip through the journal.

Seeds the accounts registry the production way (append `account_observe` ops +
rebuild the stats index), then drives `cmd_account` through argparse namespaces.
Isolation via load_isolated_cctally_module (APP_DIR + CLAUDE_JSON_PATH -> tmp).
"""
from __future__ import annotations

import argparse
import json

import pytest

import _cctally_core
from conftest import load_isolated_cctally_module

CLAUDE_A = None  # filled per-test from account_key("claude", ...)


@pytest.fixture
def cc(tmp_path, monkeypatch):
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    return mod


def _acc_key(provider, natural):
    import _lib_accounts
    return _lib_accounts.account_key(provider, natural)


def _seed(observes):
    """Append account_observe ops then rebuild the stats index."""
    import _cctally_journal as jr
    import _lib_journal as lj
    for kw in observes:
        jr.append_record(lj.make_account_observe(**kw))
    jr.rebuild_stats_index()


def _ns_list(emit_json=False):
    return argparse.Namespace(account_action="list", emit_json=emit_json)


def _ns_show(ref, emit_json=False):
    return argparse.Namespace(account_action="show", ref=ref, emit_json=emit_json)


def _ns_label(ref, label):
    return argparse.Namespace(account_action="label", ref=ref, label=label)


def _two_claude():
    ka = _acc_key("claude", "uuid-aaa")
    kb = _acc_key("claude", "uuid-bbb")
    _seed([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             natural_id="uuid-aaa", email="alice@example.com", plan_type="max",
             label="alice", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=kb, provider="claude",
             natural_id="uuid-bbb", email="bob@example.com", plan_type="pro",
             label="bob", label_source="auto"),
    ])
    return ka, kb


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def test_list_empty(cc, capsys):
    assert cc.cmd_account(_ns_list()) == 0
    out = capsys.readouterr().out
    assert "No accounts observed yet." in out


def test_list_table_shows_accounts(cc, capsys):
    _two_claude()
    assert cc.cmd_account(_ns_list()) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "bob" in out
    assert "alice@example.com" in out
    assert "claude" in out
    assert "PROVIDER" in out and "ACTIVE" in out


def test_list_json_envelope_camelcase(cc, capsys):
    ka, kb = _two_claude()
    assert cc.cmd_account(_ns_list(emit_json=True)) == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["schemaVersion"] == 1
    keys = {a["accountKey"] for a in obj["accounts"]}
    assert keys == {ka, kb}
    a0 = obj["accounts"][0]
    for k in ("accountKey", "provider", "label", "email", "planType",
              "labelSource", "firstSeenUtc", "lastSeenUtc", "active"):
        assert k in a0


def test_list_active_marker_from_claude_identity(cc, capsys, monkeypatch):
    ka, kb = _two_claude()
    monkeypatch.setattr(_cctally_core, "_resolve_active_claude_account",
                        lambda: ka)
    assert cc.cmd_account(_ns_list(emit_json=True)) == 0
    obj = json.loads(capsys.readouterr().out)
    active = {a["accountKey"]: a["active"] for a in obj["accounts"]}
    assert active[ka] is True
    assert active[kb] is False


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------

def test_show_by_label(cc, capsys):
    ka, _kb = _two_claude()
    assert cc.cmd_account(_ns_show("alice")) == 0
    out = capsys.readouterr().out
    assert ka in out
    assert "alice@example.com" in out


def test_show_json_attribution_block(cc, capsys):
    ka, _kb = _two_claude()
    assert cc.cmd_account(_ns_show("alice", emit_json=True)) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["schemaVersion"] == 1
    assert obj["accountKey"] == ka
    assert "attribution" in obj
    assert "usageSnapshots" in obj["attribution"]


def test_show_unknown_ref_exits_2_with_candidates(cc, capsys):
    _two_claude()
    assert cc.cmd_account(_ns_show("nope@nowhere")) == 2
    err = capsys.readouterr().err
    assert "ambiguous or unknown" in err


def test_show_unattributed_ref_accepted(cc, capsys):
    _two_claude()
    assert cc.cmd_account(_ns_show("unattributed")) == 0


# --------------------------------------------------------------------------
# label (durable rename)
# --------------------------------------------------------------------------

def test_label_sets_durable_user_label(cc, capsys):
    ka, _kb = _two_claude()
    assert cc.cmd_account(_ns_label("alice", "primary")) == 0
    capsys.readouterr()
    # a fresh show reflects the user label
    assert cc.cmd_account(_ns_show(ka[:8])) == 0
    out = capsys.readouterr().out
    assert "primary" in out


def test_label_survives_rebuild(cc, capsys):
    ka, _kb = _two_claude()
    assert cc.cmd_account(_ns_label("alice", "primary")) == 0
    import _cctally_journal as jr
    jr.rebuild_stats_index()
    capsys.readouterr()
    assert cc.cmd_account(_ns_show(ka[:8], emit_json=True)) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["label"] == "primary"
    assert obj["labelSource"] == "user"


# --------------------------------------------------------------------------
# R8 decoration helpers
# --------------------------------------------------------------------------

def test_real_account_count_and_decoration_gate(cc):
    ka, kb = _two_claude()
    conn = cc.open_db()
    try:
        assert cc.real_account_count(conn, "claude") == 2
        assert cc.provider_is_decorated(conn, "claude") is True
        assert cc.provider_is_decorated(conn, "codex") is False
    finally:
        conn.close()


def test_unattributed_alone_never_decorates(cc):
    # one real account + a legacy unattributed bucket -> NOT decorated (R8).
    import _lib_accounts
    ka = _acc_key("claude", "uuid-only")
    _seed([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             natural_id="uuid-only", email="solo@example.com", plan_type="max",
             label_source="auto"),
        dict(at="2026-07-01T00:00:00Z",
             account_key=_lib_accounts.UNATTRIBUTED, provider="claude",
             label_source="auto"),
    ])
    conn = cc.open_db()
    try:
        assert cc.real_account_count(conn, "claude") == 1
        assert cc.provider_is_decorated(conn, "claude") is False
    finally:
        conn.close()


# --------------------------------------------------------------------------
# `--account <ref>` render-filter resolution (shared plumbing, spec §3)
# --------------------------------------------------------------------------

def _ns_account(ref):
    return argparse.Namespace(account=ref)


def test_resolve_account_filter_no_flag_is_merged(cc):
    # No --account -> (None, None): the merged view, byte-identical to today (R8).
    assert cc.resolve_account_filter(_ns_account(None), "claude") == (None, None)


def test_resolve_account_filter_resolves_ref(cc):
    ka, _kb = _two_claude()
    key, code = cc.resolve_account_filter(_ns_account("alice"), "claude")
    assert code is None and key == ka
    # email + unique key-prefix resolve too.
    assert cc.resolve_account_filter(_ns_account("bob@example.com"), "claude")[0] == _kb_of(cc)
    assert cc.resolve_account_filter(_ns_account(ka[:8]), "claude") == (ka, None)


def _kb_of(cc):
    return _acc_key("claude", "uuid-bbb")


def test_resolve_account_filter_unattributed_literal(cc):
    import _lib_accounts
    key, code = cc.resolve_account_filter(_ns_account("unattributed"), "claude")
    assert code is None and key == _lib_accounts.UNATTRIBUTED


def test_resolve_account_filter_unknown_ref_exits_2(cc, capsys):
    _two_claude()
    key, code = cc.resolve_account_filter(_ns_account("nobody"), "claude")
    assert key is None and code == 2
    assert "ambiguous or unknown" in capsys.readouterr().err


def test_resolve_account_filter_cache_unavailable_exits_3(cc, capsys, monkeypatch):
    ka, _kb = _two_claude()
    import _cctally_cache

    def _boom():
        raise OSError("cache down")
    monkeypatch.setattr(_cctally_cache, "open_cache_db", _boom)
    key, code = cc.resolve_account_filter(
        _ns_account("alice"), "claude", needs_cache=True)
    assert key is None and code == 3
    assert "attribution unavailable" in capsys.readouterr().err
