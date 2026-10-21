"""#825/#832 spec 4.7: the bound recording kernel (cases M1, M2, F10).

M1 is the #825 defect itself: a candidate captured while account A was active
was reduced and published as whichever account was active at publication. It
runs only entry points that also exist before the fix (`_candidate_from_input`,
`_write_candidate`, `_statusline_reduce_and_publish`, `cmd_record_usage`), so
the same test is the RED proof against the pre-fix code.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

import pytest

from conftest import load_script, redirect_paths

NOW = 1791417600


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setattr(time, "time", lambda: float(NOW))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(NOW))
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    cctally.open_db().close()
    return cctally


def _identity(app, name: str) -> str:
    import _cctally_core as core
    import _lib_accounts
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": name, "emailAddress": f"{name}@example.test",
        "plan": "max"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _lib_accounts.account_key("claude", name)


def _parsed(app, session: str, *, weekly=20, five=30):
    return app._lib_statusline.parse_statusline_stdin(json.dumps({
        "session_id": session,
        "rate_limits": {
            "seven_day": {"used_percentage": weekly, "resets_at": _iso(NOW + 3 * 86400)},
            "five_hour": {"used_percentage": five, "resets_at": _iso(NOW + 3600)},
        }}).encode())


def _journal(app):
    import _cctally_core as core
    records = []
    for path in sorted(core.JOURNAL_DIR.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _usage_obs(app):
    return [r for r in _journal(app)
            if r.get("t") == "obs" and r.get("provider") == "claude"]


def _snapshots(app):
    conn = app.open_db()
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT account_key, weekly_percent, five_hour_percent "
            "FROM weekly_usage_snapshots ORDER BY account_key, id")]
    finally:
        conn.close()


@pytest.mark.parametrize("first,second", [("A", "B"), ("B", "A")])
def test_m1_capture_switch_publishes_only_as_the_captured_account(app, first, second):
    """Case M1 (#825, RED before the fix): the candidate is captured while
    `first` is active, the identity switches to `second` before the detached
    reducer publishes, and the journal and the index still say `first`."""
    import _cctally_statusline as sl

    captured = _identity(app, first)
    candidate = sl._candidate_from_input(_parsed(app, "peer"), received_at=NOW)
    assert candidate is not None
    sl._write_candidate(candidate)
    other = _identity(app, second)

    sl._statusline_reduce_and_publish(nudge_sink=lambda: None)

    usage = _usage_obs(app)
    assert len(usage) == 1, usage
    assert usage[0].get("account") == captured, (
        f"published as {usage[0].get('account')!r}, captured as {captured!r} (#825)")
    assert usage[0]["payload"]["account_binding"]["accountKey"] == captured
    assert _snapshots(app) == [(captured, 20.0, 30.0)]
    assert all(row[0] != other for row in _snapshots(app))


@pytest.mark.parametrize("first,second", [("A", "B"), ("B", "A")])
def test_m2_post_switch_candidate_publishes_only_in_its_own_partition(app, first, second):
    """Case M2: after the switch, the second account's candidate is reduced and
    published only in its own partition; the first account's live candidate
    is neither merged into it nor republished under it."""
    import _cctally_statusline as sl

    a_key = _identity(app, first)
    sl._write_candidate(sl._candidate_from_input(
        _parsed(app, "peer-a", weekly=70, five=70), received_at=NOW))
    b_key = _identity(app, second)
    sl._write_candidate(sl._candidate_from_input(
        _parsed(app, "peer-b", weekly=20, five=20), received_at=NOW))

    sl._statusline_reduce_and_publish(nudge_sink=lambda: None)

    by_account = {row[0]: row[1:] for row in _snapshots(app)}
    assert by_account.get(b_key) == (20.0, 20.0), by_account
    assert by_account.get(a_key) == (70.0, 70.0), by_account
    for record in _usage_obs(app):
        assert record["payload"]["account_binding"]["accountKey"] == record["account"]


def _record_args(weekly=20, five=30):
    return argparse.Namespace(
        percent=weekly, resets_at=str(NOW + 3 * 86400),
        five_hour_percent=five, five_hour_resets_at=str(NOW + 3600),
        source="statusline")


def test_bound_kernel_never_resolves_the_active_identity(app):
    """The bound kernel records the binding it is given, whatever account is
    active at append time."""
    import _lib_account_binding as binding_lib
    import _cctally_core as core

    a_key = _identity(app, "A")
    binding = binding_lib.capture(core._resolve_active_claude_identity())
    _identity(app, "B")
    assert app.record_usage_bound(
        _record_args(), binding=binding, nudge_dashboard=False) == 0
    usage = _usage_obs(app)
    assert [r["account"] for r in usage] == [a_key]
    assert usage[0]["payload"]["account_binding"] == binding.document()
    observes = [r for r in _journal(app)
                if (r.get("payload") or {}).get("kind") == "account_observe"]
    assert [o["payload"]["account_key"] for o in observes] == [a_key]
    journal = _journal(app)
    assert journal.index(observes[0]) < journal.index(usage[0]), (
        "the account observation must precede the usage observation")


def test_cli_adapter_writes_nothing_on_a_torn_identity(app, monkeypatch):
    import _cctally_core as core

    monkeypatch.setattr(core, "_resolve_active_claude_identity", lambda: {
        "status": "torn", "account_key": "unattributed", "natural_id": None,
        "email": None, "plan_type": None})
    assert app.cmd_record_usage(_record_args(), nudge_dashboard=False) == 2
    assert _usage_obs(app) == []


def test_f10_legacy_account_marker_never_suppresses_current_evidence(app):
    """Case F10: the previous release's `observed-claude-account` marker naming
    A must not suppress the current account observation before A's usage."""
    import _cctally_core as core

    a_key = _identity(app, "A")
    (core.APP_DIR / "observed-claude-account").write_text(a_key + "\n")
    assert app.cmd_record_usage(_record_args(), nudge_dashboard=False) == 0
    observes = [r for r in _journal(app)
                if (r.get("payload") or {}).get("kind") == "account_observe"]
    assert [o["payload"]["account_key"] for o in observes] == [a_key]


def test_f10_dedup_compares_full_binding_evidence(app):
    """Same account key, changed identity evidence (email): a new observation.
    Unchanged evidence: deduplicated."""
    import _cctally_core as core

    a_key = _identity(app, "A")
    assert app.cmd_record_usage(_record_args(), nudge_dashboard=False) == 0
    assert app.cmd_record_usage(_record_args(weekly=21), nudge_dashboard=False) == 0
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": "A", "emailAddress": "renamed@example.test",
        "plan": "max"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    assert app.cmd_record_usage(_record_args(weekly=22), nudge_dashboard=False) == 0
    observes = [r["payload"] for r in _journal(app)
                if (r.get("payload") or {}).get("kind") == "account_observe"]
    assert [(o["account_key"], o.get("email")) for o in observes] == [
        (a_key, "A@example.test"), (a_key, "renamed@example.test")]
