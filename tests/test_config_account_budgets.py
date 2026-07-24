"""Per-account budget config keys (#341 Task 3, spec §6): `budget.accounts`
(Claude) and `budget.codex.accounts` (inside `budget.codex`), with WRITE-TIME
ref normalization so a later `account label` rename never retargets the budget.

Covers set/get/unset round-trips, the label->key normalization + rename safety,
Codex per-account budgets valid WITHOUT a vendor-wide amount_usd, value
validation, and the reserved-bucket / unknown-ref rejections.
"""
from __future__ import annotations

import argparse
import json

import pytest

import _cctally_core
from conftest import load_isolated_cctally_module


@pytest.fixture
def cc(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _acc(provider, natural):
    import _lib_accounts
    return _lib_accounts.account_key(provider, natural)


def _seed(observes):
    import _cctally_journal as jr
    import _lib_journal as lj
    for kw in observes:
        jr.append_record(lj.make_account_observe(**kw))
    jr.rebuild_stats_index()


def _set(cc, key, value):
    return cc._cmd_config_set(
        argparse.Namespace(action="set", key=key, value=value, emit_json=False))


def _unset(cc, key):
    return cc._cmd_config_unset(argparse.Namespace(action="unset", key=key))


def _stored_budget(cc):
    return json.loads(_cctally_core.CONFIG_PATH.read_text()).get("budget", {})


# --------------------------------------------------------------------------
# allowlist
# --------------------------------------------------------------------------

def test_keys_in_allowlist(cc):
    assert "budget.accounts" in cc.ALLOWED_CONFIG_KEYS
    assert "budget.codex.accounts" in cc.ALLOWED_CONFIG_KEYS


# --------------------------------------------------------------------------
# claude budget.accounts
# --------------------------------------------------------------------------

def test_set_by_label_normalizes_to_key(cc, capsys):
    ka = _acc("claude", "uuid-a")
    _seed([dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
                email="a@x.com", label="alice", label_source="auto")])
    assert _set(cc, "budget.accounts", '{"alice": 50}') == 0
    stored = _stored_budget(cc)
    assert stored["accounts"] == {ka: 50.0}      # ref normalized to the key


def test_rename_does_not_retarget_budget(cc):
    ka = _acc("claude", "uuid-a")
    _seed([dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
                email="a@x.com", label="alice", label_source="auto")])
    assert _set(cc, "budget.accounts", '{"alice": 50}') == 0
    # rename the label via the account subcommand
    import _cctally_journal as jr
    import _lib_journal as lj
    jr.append_record(lj.make_account_label(
        at="2026-07-03T00:00:00Z", account_key=ka, label="renamed",
        provider="claude"))
    jr.run_stats_ingest(mode="authoritative")
    # the stored budget is STILL keyed by the immutable account_key
    assert _stored_budget(cc)["accounts"] == {ka: 50.0}


def test_raw_key_accepted_verbatim_without_registry(cc):
    key = "0123456789abcdef0123456789abcdef"  # 32-hex, never observed
    assert _set(cc, "budget.accounts", json.dumps({key: 25})) == 0
    assert _stored_budget(cc)["accounts"] == {key: 25.0}


def test_unknown_ref_exits_2(cc, capsys):
    # seed one real account so the registry exists -> an unresolvable ref hits
    # the AccountRefError path (not the "no accounts observed yet" path)
    ka = _acc("claude", "uuid-a")
    _seed([dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
                email="a@x.com", label="alice", label_source="auto")])
    assert _set(cc, "budget.accounts", '{"nobody": 10}') == 2
    err = capsys.readouterr().err
    assert "unknown or ambiguous account ref" in err


def test_unresolvable_ref_without_registry_exits_2(cc, capsys):
    # no accounts observed yet + a non-key ref -> clean exit 2 (never a crash)
    assert _set(cc, "budget.accounts", '{"nobody": 10}') == 2
    assert "no accounts observed yet" in capsys.readouterr().err


def test_reserved_bucket_rejected(cc, capsys):
    assert _set(cc, "budget.accounts", '{"unattributed": 10}') == 2
    assert "reserved" in capsys.readouterr().err
    assert _set(cc, "budget.accounts", '{"*": 10}') == 2


def test_negative_value_rejected(cc):
    key = "0123456789abcdef0123456789abcdef"
    assert _set(cc, "budget.accounts", json.dumps({key: -5})) == 2


def test_get_round_trips_json(cc, capsys):
    key = "0123456789abcdef0123456789abcdef"
    assert _set(cc, "budget.accounts", json.dumps({key: 40})) == 0
    capsys.readouterr()
    rc = cc._cmd_config_get(
        argparse.Namespace(key="budget.accounts", emit_json=False),
        cc.load_config())
    assert rc == 0
    out = capsys.readouterr().out.strip()
    # `budget.accounts={"<key>": 40.0}` -> the value round-trips through set
    payload = json.loads(out.split("=", 1)[1])
    assert payload == {key: 40.0}


def test_unset_removes_map(cc):
    key = "0123456789abcdef0123456789abcdef"
    assert _set(cc, "budget.accounts", json.dumps({key: 40})) == 0
    assert _unset(cc, "budget.accounts") == 0
    assert "accounts" not in _stored_budget(cc)


def test_get_budget_config_surfaces_accounts(cc):
    key = "0123456789abcdef0123456789abcdef"
    cfg = cc._get_budget_config({"budget": {"accounts": {key: 12.5}}})
    assert cfg["accounts"] == {key: 12.5}


# --------------------------------------------------------------------------
# codex budget.codex.accounts (valid without vendor-wide amount_usd)
# --------------------------------------------------------------------------

def test_codex_accounts_without_amount_usd(cc):
    key = "abcdef0123456789abcdef0123456789"
    assert _set(cc, "budget.codex.accounts", json.dumps({key: 30})) == 0
    codex = _stored_budget(cc)["codex"]
    assert codex["accounts"] == {key: 30.0}
    assert codex.get("amount_usd") in (None,)     # per-account-only block


def test_codex_block_requires_amount_or_accounts(cc):
    # a codex block with neither amount_usd nor accounts is rejected
    with pytest.raises(cc._BudgetConfigError):
        cc._get_budget_config({"budget": {"codex": {"period": "calendar-month"}}})


def test_codex_accounts_get_and_unset(cc, capsys):
    key = "abcdef0123456789abcdef0123456789"
    assert _set(cc, "budget.codex.accounts", json.dumps({key: 30})) == 0
    capsys.readouterr()
    rc = cc._cmd_config_get(
        argparse.Namespace(key="budget.codex.accounts", emit_json=False),
        cc.load_config())
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().split("=", 1)[1])
    assert payload == {key: 30.0}
    # unsetting the per-account-only block's accounts drops the whole codex block
    assert _unset(cc, "budget.codex.accounts") == 0
    assert "codex" not in _stored_budget(cc)


def test_codex_accounts_coexist_with_amount(cc):
    key = "abcdef0123456789abcdef0123456789"
    assert _set(cc, "budget.codex.amount_usd", "100") == 0
    assert _set(cc, "budget.codex.accounts", json.dumps({key: 30})) == 0
    codex = _stored_budget(cc)["codex"]
    assert abs(codex["amount_usd"] - 100.0) < 1e-9
    assert codex["accounts"] == {key: 30.0}
