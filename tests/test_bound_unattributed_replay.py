"""#825/#832 fix round C, 825-832-CD2-004 — a current binding's explicit
`unattributed` survives every replay.

A current `record-usage` observation whose account binding is
`stably_absent` (no `~/.claude.json` account, api-key mode) binds the
`unattributed` sentinel deliberately, and the writer omits the observation's
top-level `account` for it. Live ingest reads that as `unattributed`. Legacy
normalization exists for pre-#341 lines that never named an account; it must
not read a current binding's omission as "legacy, never named" and re-label the
reading with the historical `accounts_cutover` mapping, or `db rederive` and the
derivation-completion step replace the live decision under another account.
"""
from __future__ import annotations

import argparse
import datetime as dt

import pytest

from conftest import load_script, redirect_paths

_W2 = int(dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc).timestamp())


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _mods():
    import _cctally_core
    import _cctally_journal
    import _lib_journal
    return _cctally_journal, _lib_journal, _cctally_core


def _account_a():
    import _lib_accounts
    return _lib_accounts.account_key("claude", "natural-account-a")


def _seed_historical_cutover():
    """A pre-boundary journal: a legacy reading and the #341 accounts cutover
    that maps every legacy Claude line to account A."""
    jr, J, core = _mods()
    legacy_reading = J.make_obs(
        at="2026-01-04T09:00:00Z", src="record-usage", provider="claude",
        payload={"weekly_percent": 0.0, "source": "statusline",
                 "resets_at": _W2 - 7 * 86400,
                 "captured_at": "2026-01-04T09:00:00Z"},
        writer_generation=None)
    cutover = J.make_op(
        at="2026-01-05T00:00:00Z", src="accounts-cutover",
        payload={"kind": "accounts_cutover",
                 "claude_legacy_account": _account_a()},
        writer_generation=None)
    cutover["id"] = jr.CUTOVER_OP_ID
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (core.JOURNAL_DIR / "observations-2026-01.jsonl").write_bytes(
        J.encode_line(legacy_reading) + J.encode_line(cutover))


def _bound_unattributed_reading():
    """What `record-usage` writes for a `stably_absent` binding: the binding
    in the payload, and no top-level account."""
    _jr, J, _core = _mods()
    import _lib_account_binding
    import _lib_accounts
    binding = _lib_account_binding.capture({
        "status": _lib_account_binding.STATUS_STABLY_ABSENT,
        "account_key": _lib_accounts.UNATTRIBUTED, "natural_id": None})
    assert binding is not None
    at = "2026-01-12T09:00:00Z"
    return J.prepare_record(J.make_obs(
        at=at, src="record-usage", provider="claude", account=None,
        payload={"weekly_percent": 0.0, "source": "statusline",
                 "resets_at": _W2, "captured_at": at,
                 "account_binding": binding.document()}))


def _snapshot_accounts():
    _jr, _J, core = _mods()
    conn = core.open_db()
    try:
        return sorted(tuple(row) for row in conn.execute(
            "SELECT captured_at_utc, account_key FROM weekly_usage_snapshots"))
    finally:
        conn.close()


def test_rederive_keeps_a_bound_unattributed_reading_unattributed(ns):
    jr, J, core = _mods()
    import _cctally_rederive
    ns["open_cache_db"]().close()
    _seed_historical_cutover()
    reading = _bound_unattributed_reading()
    assert "account" not in reading
    jr.append_record(reading)
    jr.run_stats_ingest(mode="authoritative")
    live = dict(_snapshot_accounts())
    assert live["2026-01-12T09:00:00Z"] == "unattributed"

    preview = _cctally_rederive.preview_db_rederive("claude-usage")
    touched = [(a.event_id, a.disposition) for a in preview.plan.actions
               if a.event_id == "sa:" + reading["id"]]
    assert touched == [], touched


def test_completion_derives_a_bound_unattributed_reading_unattributed(
        ns, capsys):
    jr, J, core = _mods()
    _seed_historical_cutover()
    jr.run_stats_ingest(mode="authoritative")
    # The reading's own ingest never runs (an interrupted tick); the
    # completion step of `db rebuild` derives it.
    jr.append_record(_bound_unattributed_reading())
    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True)) == 0
    capsys.readouterr()
    assert dict(_snapshot_accounts())["2026-01-12T09:00:00Z"] == "unattributed"


def test_only_a_stampless_unbound_observation_is_legacy(ns):
    """The classifier's rule, case by case: the writer-generation stamp, or a
    validating binding naming `unattributed`, is an explicit account
    statement; a stampless line with no binding, a binding that fails
    validation, or one naming a real account stays under the legacy rule."""
    jr, J, _core = _mods()
    import _lib_account_binding
    reading = _bound_unattributed_reading()
    assert jr.classify_legacy_provider(reading) is None
    assert jr.legacy_account_key(reading, _account_a()) is None

    stampless = J.unstamped_record(reading)
    assert jr.classify_legacy_provider(stampless) is None

    unbound = J.unstamped_record(reading)
    unbound["payload"] = {k: v for k, v in reading["payload"].items()
                          if k != "account_binding"}
    assert jr.classify_legacy_provider(unbound) == "claude"

    forged = J.unstamped_record(reading)
    forged["payload"] = dict(reading["payload"])
    forged["payload"]["account_binding"] = dict(
        reading["payload"]["account_binding"], identityEvidence="0" * 64)
    assert jr.classify_legacy_provider(forged) == "claude"

    real = _lib_account_binding.capture({
        "status": _lib_account_binding.STATUS_IDENTIFIED,
        "account_key": _account_a(), "natural_id": "natural-account-a"})
    named = J.unstamped_record(reading)
    named["payload"] = dict(reading["payload"],
                            account_binding=real.document())
    assert jr.classify_legacy_provider(named) == "claude"
