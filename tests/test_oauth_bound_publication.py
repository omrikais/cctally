"""#825/#832 spec 4.6: bound OAuth capture and revalidation on every entry
point (cases M10, M11, M12, F11).

The selected credential and its AccountBinding are captured together BEFORE
the fetch; after the response is parsed and the selected-state lock is held
again, the identity and credential are re-read, and publication proceeds only
when both still match. Everything after that gate — fences, recording,
commit, repair, control and freshness — uses the captured binding.
"""
from __future__ import annotations

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
    monkeypatch.setattr(cctally, "_bust_statusline_cache", lambda: "skipped")
    cctally.open_db().close()
    return cctally


def _identity(name: str) -> str:
    import _cctally_core as core
    import _lib_accounts
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": name, "emailAddress": f"{name}@example.test",
        "plan": "max"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _lib_accounts.account_key("claude", name)


def _torn():
    import _cctally_core as core
    core.CLAUDE_JSON_PATH.write_text("{torn")
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)


def _response(weekly=60, five=65):
    return {"seven_day": {"utilization": weekly, "resets_at": _iso(NOW + 3 * 86400)},
            "five_hour": {"utilization": five, "resets_at": _iso(NOW + 3600)}}


def _journal():
    import _cctally_core as core
    records = []
    for path in sorted(core.JOURNAL_DIR.glob("*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text().splitlines()
                       if line.strip())
    return records


def _claude_records():
    return [r for r in _journal()
            if (r.get("t") == "obs" and r.get("provider") == "claude")
            or (r.get("payload") or {}).get("kind") == "account_observe"]


def _snapshots(app):
    conn = app.open_db()
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT account_key, weekly_percent, five_hour_percent "
            "FROM weekly_usage_snapshots ORDER BY account_key, id")]
    finally:
        conn.close()


def _account_state(account_key):
    """Every current-protocol file of one account partition."""
    import _cctally_core as core
    root = core.STATUSLINE_ACCOUNT_BOUND_DIR / "accounts" / account_key
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _fences(account_key):
    return [p for p in _account_state(account_key) if p.startswith("tombstones/")]


def _run(app, entry):
    """Drive one OAuth entry point; return True when it reported success."""
    import _cctally_dashboard
    import _cctally_statusline as sl
    if entry == "forced":
        return app._refresh_usage_inproc().status == "ok"
    if entry == "dashboard":
        return _cctally_dashboard._refresh_usage_inproc().status == "ok"
    if entry == "hook":
        return app._hook_tick_oauth_refresh(throttle_seconds=0)[0].startswith("ok")
    if entry == "setup":
        return app._hook_tick_oauth_refresh()[0].startswith("ok")
    if entry == "statusline":
        sl._statusline_oauth_tick(sync_for_test=True)
        return bool(_snapshots(app))
    raise AssertionError(entry)


ENTRIES = ["forced", "dashboard", "hook", "setup", "statusline"]


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("change", ["switch", "torn"])
def test_m10_m11_identity_change_during_fetch_discards_before_mutation(
        app, monkeypatch, entry, change):
    """Cases M10/M11: an A->B switch or a torn identity during the fetch
    discards the response before any fence, control, journal or freshness
    mutation, through every entry point."""
    a = _identity("A")
    monkeypatch.setattr(app, "_resolve_oauth_token", lambda: "credential-A")

    def fetch(**kwargs):
        assert kwargs["token"] == "credential-A"
        if change == "switch":
            _identity("B")
        else:
            _torn()
        return _response()

    monkeypatch.setattr(app, "_fetch_oauth_usage", fetch)
    assert not _run(app, entry)
    assert _claude_records() == [], "a discarded response must not append"
    assert _snapshots(app) == []
    import _lib_accounts
    for key in (a, _lib_accounts.account_key("claude", "B"), "unattributed"):
        assert _account_state(key) == [], key


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_m12_oauth_for_a_reconciles_only_a(app, monkeypatch, order):
    """Case M12: an OAuth publication for A that overlaps live A and B
    candidates fences and reconciles only A's exact axis/window."""
    import _cctally_statusline as sl
    keys = {}
    for name in order:
        keys[name] = _identity(name)
        candidate = sl._candidate_from_input(app._lib_statusline.parse_statusline_stdin(
            json.dumps({"session_id": name, "rate_limits": {
                "seven_day": {"used_percentage": 20, "resets_at": _iso(NOW + 3 * 86400)},
                "five_hour": {"used_percentage": 30, "resets_at": _iso(NOW + 3600)},
            }}).encode()), received_at=NOW)
        sl._write_candidate(candidate)
    _identity("A")
    monkeypatch.setattr(app, "_resolve_oauth_token", lambda: "credential-A")
    monkeypatch.setattr(app, "_fetch_oauth_usage", lambda **k: _response(60, 65))
    assert app._refresh_usage_inproc().status == "ok"
    assert {p.split("-")[0] for p in _fences(keys["A"])} == {
        "tombstones/fiveHour", "tombstones/sevenDay"}
    assert _fences(keys["B"]) == []
    assert "transition.json" not in _account_state(keys["B"])
    sl._statusline_reduce_and_publish(nudge_sink=lambda: None)
    by_account = {row[0]: row[1:] for row in _snapshots(app)}
    assert by_account == {keys["A"]: (60.0, 65.0), keys["B"]: (20.0, 30.0)}


def _publication_accounts():
    return [r.get("account") for r in _journal()
            if r.get("t") == "obs" and r.get("provider") == "claude"]


@pytest.mark.parametrize("entry", ["forced", "hook"])
@pytest.mark.parametrize("switch_at", ["after_begin", "after_record"])
@pytest.mark.parametrize("change", ["switch", "torn"])
def test_f11_identity_change_after_the_gate_stays_bound_to_a(
        app, monkeypatch, entry, switch_at, change):
    """Case F11: a switch or torn read AFTER authority begin or AFTER recording
    changes nothing about attribution: the journal says A, and only A's fence,
    control and freshness change."""
    import _lib_accounts
    a = _identity("A")
    b = _lib_accounts.account_key("claude", "B")
    monkeypatch.setattr(app, "_resolve_oauth_token", lambda: "credential-A")
    monkeypatch.setattr(app, "_fetch_oauth_usage", lambda **k: _response())

    def flip():
        _identity("B") if change == "switch" else _torn()

    if switch_at == "after_begin":
        record = app.cmd_record_usage

        def switch_then_record(*args, **kwargs):
            flip()
            return record(*args, **kwargs)

        monkeypatch.setattr(app, "cmd_record_usage", switch_then_record)
    else:
        monkeypatch.setattr(app, "_after_authoritative_record", flip)
    assert _run(app, entry)
    assert _publication_accounts() == [a]
    assert _snapshots(app) == [(a, 60.0, 65.0)]
    assert _fences(a) and _account_state(b) == []
    assert "observe.last" in _account_state(a)


@pytest.mark.parametrize("entry", ["forced", "hook"])
@pytest.mark.parametrize("failure", ["before_append", "after_append"])
def test_f11_failure_leaves_only_a_fence_inflight(app, monkeypatch, entry, failure):
    """Case F11: a failure before or after the durable observation append,
    with the identity switched to B, leaves only A's bound fence inflight, and
    the repair it requires stays in A's namespace."""
    import _cctally_statusline as sl
    import _lib_accounts
    a = _identity("A")
    a_binding = sl._capture_binding()
    b = _lib_accounts.account_key("claude", "B")
    monkeypatch.setattr(app, "_resolve_oauth_token", lambda: "credential-A")
    monkeypatch.setattr(app, "_fetch_oauth_usage", lambda **k: _response())
    if failure == "before_append":
        def fail(*args, **kwargs):
            _identity("B")
            raise RuntimeError("stopped before append")
        monkeypatch.setattr(app, "cmd_record_usage", fail)
    else:
        def fail_after():
            _identity("B")
            raise RuntimeError("stopped after append")
        monkeypatch.setattr(app, "_after_authoritative_record", fail_after)
    assert not _run(app, entry)
    assert _publication_accounts() == ([] if failure == "before_append" else [a])
    states = {}
    for path in _fences(a):
        states[path] = json.loads((sl._account_dir(a) / path).read_text())["state"]
    assert states and set(states.values()) == {"inflight"}
    assert _account_state(b) == []
    b_binding = sl._capture_binding()
    assert b_binding.account_key == b
    assert sl._authoritative_repair_required(now_epoch=NOW, binding=a_binding)
    assert not sl._authoritative_repair_required(now_epoch=NOW, binding=b_binding)


@pytest.mark.parametrize("entry", ["forced", "hook"])
def test_f11_a_b_a_with_the_captured_credential_publishes_as_a(app, monkeypatch, entry):
    """A->B->A during the fetch is safe when the response came from A's
    captured credential: it is published as A."""
    a = _identity("A")
    monkeypatch.setattr(app, "_resolve_oauth_token", lambda: "credential-A")

    def fetch(**kwargs):
        assert kwargs["token"] == "credential-A"
        _identity("B")
        _identity("A")
        return _response()

    monkeypatch.setattr(app, "_fetch_oauth_usage", fetch)
    assert _run(app, entry)
    assert _publication_accounts() == [a]


@pytest.mark.parametrize("entry", ["forced", "hook"])
def test_f11_a_b_a_with_a_changed_credential_is_discarded(app, monkeypatch, entry):
    """A->B->A where the selected credential changed during the fetch: the
    response was not fetched with the credential A now selects, so it is
    discarded before any mutation."""
    import _cctally_statusline as sl
    a = _identity("A")
    tokens = iter(["credential-A1"])
    monkeypatch.setattr(app, "_resolve_oauth_token",
                        lambda: next(tokens, "credential-A2"))

    def fetch(**kwargs):
        assert kwargs["token"] == "credential-A1"
        _identity("B")
        _identity("A")
        return _response()

    monkeypatch.setattr(app, "_fetch_oauth_usage", fetch)
    assert not _run(app, entry)
    assert _claude_records() == []
    assert _account_state(a) == []
    assert sl._capture_binding().account_key == a


def test_hook_freshness_of_another_account_does_not_suppress_b(app, monkeypatch):
    """Selected freshness is per account: A's fresh marker never withholds a
    genuine B response."""
    import _lib_accounts
    _identity("A")
    monkeypatch.setattr(app, "_resolve_oauth_token", lambda: "credential")
    monkeypatch.setattr(app, "_fetch_oauth_usage", lambda **k: _response())
    assert app._refresh_usage_inproc().status == "ok"
    _identity("B")
    status, _ = app._hook_tick_oauth_refresh(throttle_seconds=0)
    assert status.startswith("ok"), status
    assert _publication_accounts()[-1] == _lib_accounts.account_key("claude", "B")
