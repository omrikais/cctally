"""#825/#832 spec 4.2–4.4 and section 7: the current spool namespace, the
account-partitioned reducer, the per-account transition and account-local
five-hour anchors (cases M3–M9, M14–M18).

Every case runs real spool writes, the real detached-reducer body
(`_statusline_reduce_and_publish`) and the real recording kernel against an
isolated store. Account orders and peer insertion orders are both exercised
where the row names them.
"""
from __future__ import annotations

import argparse
import dataclasses
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


def _tick(monkeypatch, at: int) -> None:
    monkeypatch.setattr(time, "time", lambda: float(NOW + at))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(NOW + at))


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _tick(monkeypatch, 0)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
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


def _parsed(app, session, *, weekly=20, five=30, five_reset=None, at=0,
            seven=True):
    limits = {"five_hour": {"used_percentage": five,
                            "resets_at": _iso(five_reset or NOW + 3600)}}
    if seven:
        limits["seven_day"] = {"used_percentage": weekly,
                               "resets_at": _iso(NOW + 3 * 86400)}
    return app._lib_statusline.parse_statusline_stdin(json.dumps({
        "session_id": session, "rate_limits": limits}).encode())


def _spool(app, name, session, *, at=0, **values):
    import _cctally_statusline as sl
    _identity(name)
    candidate = sl._candidate_from_input(
        _parsed(app, session, **values), received_at=NOW + at)
    assert candidate is not None
    sl._write_candidate(candidate)
    return candidate


def _reduce():
    import _cctally_statusline as sl
    return sl._statusline_reduce_and_publish(nudge_sink=lambda: None)


def _journal():
    import _cctally_core as core
    records = []
    for path in sorted(core.JOURNAL_DIR.glob("*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text().splitlines()
                       if line.strip())
    return records


def _usage_obs():
    return [r for r in _journal()
            if r.get("t") == "obs" and r.get("provider") == "claude"]


def _snapshots(app):
    conn = app.open_db()
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT account_key, weekly_percent, five_hour_percent "
            "FROM weekly_usage_snapshots ORDER BY account_key, id")]
    finally:
        conn.close()


def _transition(account_key, at=0):
    import _cctally_statusline as sl
    return sl._read_transition(account_key, now_epoch=NOW + at)


def _binding(name):
    import _cctally_statusline as sl
    _identity(name)
    return sl._capture_binding()


def _legacy_paths():
    import _cctally_core as core
    return [core.STATUSLINE_CANDIDATE_DIR, core.STATUSLINE_SELECTED_PATH,
            core.STATUSLINE_AUTHORITATIVE_5H_PATH, core.STATUSLINE_AUTHORITATIVE_7D_PATH,
            core.STATUSLINE_OBSERVE_MARKER_PATH, core.STATUSLINE_TRANSPORT_MARKER_PATH,
            core.STATUSLINE_PERSIST_LOCK_PATH]


# ── M3 / M4 / legacy spool ─────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["torn", "unreadable", "inconsistent"])
def test_m3_unstable_identity_writes_nothing(app, monkeypatch, state):
    """Case M3: no candidate, no control mutation, no retry consumption."""
    import _cctally_core as core
    import _cctally_statusline as sl
    monkeypatch.setattr(core, "_resolve_active_claude_identity", lambda: {
        "status": state, "account_key": "unattributed", "natural_id": None,
        "email": None, "plan_type": None})
    assert sl._candidate_from_input(_parsed(app, "peer"), received_at=NOW) is None
    sl._statusline_persist(_parsed(app, "peer"), sync_for_test=True)
    assert not (core.STATUSLINE_ACCOUNT_BOUND_DIR / "accounts").exists()
    assert not core.STATUSLINE_CANDIDATE_DIR.exists()
    assert _usage_obs() == []


def test_m4_stably_absent_binds_unattributed_in_its_own_partition(app):
    """Case M4: an absent identity binds `unattributed` deliberately and is
    never merged with a real account."""
    import _cctally_core as core
    import _cctally_statusline as sl
    real = _spool(app, "A", "real", weekly=70, five=70)
    core.CLAUDE_JSON_PATH.unlink()
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    absent = sl._candidate_from_input(_parsed(app, "absent", weekly=20, five=20),
                                      received_at=NOW)
    assert absent.binding.status == "stably_absent"
    assert absent.binding.account_key == "unattributed"
    sl._write_candidate(absent)
    _reduce()
    assert dict((r[0], r[1:]) for r in _snapshots(app)) == {
        real.binding.account_key: (70.0, 70.0), "unattributed": (20.0, 20.0)}


def test_current_render_and_reduce_never_touch_the_legacy_spool(app):
    """Section 7: the legacy paths are neither written nor read."""
    import _cctally_statusline as sl
    _identity("A")
    sl._statusline_persist(_parsed(app, "peer"), sync_for_test=True)
    assert len(_snapshots(app)) == 1
    for path in _legacy_paths():
        assert not path.exists(), path


def test_legacy_candidates_are_never_reduced(app):
    """A previous-release candidate in the legacy directory is not input."""
    import _cctally_core as core
    core.STATUSLINE_CANDIDATE_DIR.mkdir(parents=True)
    (core.STATUSLINE_CANDIDATE_DIR / ("a" * 64 + ".json")).write_text(json.dumps({
        "schemaVersion": 1, "receivedAt": NOW,
        "sevenDay": {"percent": 90.0, "resetsAt": NOW + 3 * 86400}}))
    assert _reduce() == ()
    assert _snapshots(app) == []


# ── M5 / M6 / M14 / M15 ────────────────────────────────────────────────────


def test_m5_same_account_peers_reduce_normally(app):
    """Case M5: two contributors of one account publish the maximum once."""
    a = _spool(app, "A", "peer-1", weekly=20, five=30)
    _spool(app, "A", "peer-2", weekly=25, five=35)
    results = _reduce()
    assert [(r.account_key, r.status) for r in results] == [(a.binding.account_key, "ok")]
    assert _snapshots(app) == [(a.binding.account_key, 25.0, 35.0)]


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_m6_identical_values_stay_independent(app, order):
    """Case M6: identical values and resets in A and B are two partitions with
    independent candidates, transitions, publications and retry namespaces."""
    keys = {}
    for name in order:
        keys[name] = _spool(app, name, "peer", weekly=40, five=40).binding.account_key
    results = _reduce()
    assert [r.account_key for r in results] == sorted(
        keys.values(), key=lambda k: k.encode())
    assert sorted(_snapshots(app)) == sorted(
        [(keys["A"], 40.0, 40.0), (keys["B"], 40.0, 40.0)])
    for name in order:
        transition = _transition(keys[name])
        assert transition.binding.account_key == keys[name]
        assert transition.control.db_projection.account_key == keys[name]
    # A drop in A alone arms A's pending state only.
    _spool(app, "A", "peer", at=2, weekly=10, five=10)
    _tick_results = _reduce()
    a_pending = _transition(keys["A"], 2).control.pending_drops
    b_pending = _transition(keys["B"], 2).control.pending_drops
    assert any(a_pending.values())
    assert not any(b_pending.values())
    for pending in a_pending.values():
        if pending is not None and pending.retry_signature is not None:
            assert pending.retry_signature.account_key == keys["A"]


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_m14_all_eligible_partitions_attempted_in_bytewise_order(app, order):
    """Case M14: both partitions are attempted in one invocation, in bytewise
    account-key order."""
    for name in order:
        _spool(app, name, name, weekly=20, five=30)
    results = _reduce()
    assert len(results) == 2
    assert [r.account_key for r in results] == sorted(
        (r.account_key for r in results), key=lambda k: k.encode())
    assert [r.status for r in results] == ["ok", "ok"]
    assert len(_snapshots(app)) == 2


def test_m15_one_partition_failure_does_not_starve_the_other(app, monkeypatch):
    """Case M15: an injected failure in the first partition leaves it
    retryable; the second is still attempted and cannot consume or clear the
    first's transition."""
    keys = sorted((_spool(app, n, n).binding.account_key for n in ("A", "B")),
                  key=lambda k: k.encode())
    first = keys[0]
    real = app.cmd_record_usage

    def fail_first(args, **kwargs):
        if kwargs["binding"].account_key == first:
            raise RuntimeError("account-local failure")
        return real(args, **kwargs)

    monkeypatch.setattr(app, "cmd_record_usage", fail_first)
    results = _reduce()
    assert [(r.account_key, r.status) for r in results] == [
        (first, "record_failed"), (keys[1], "ok")]
    assert [row[0] for row in _snapshots(app)] == [keys[1]]
    assert _transition(first).publication is not None, "A's plan stays pending"
    monkeypatch.setattr(app, "cmd_record_usage", real)
    _identity("B")
    results = _reduce()
    assert {r.account_key: r.status for r in results}[first] == "recovered"
    assert sorted(row[0] for row in _snapshots(app)) == keys


def test_corrupt_partition_is_left_untouched_and_others_publish(app):
    import _cctally_statusline as sl
    a = _spool(app, "A", "A").binding.account_key
    b = _spool(app, "B", "B").binding.account_key
    path = sl._transition_path(a)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    results = {r.account_key: r.status for r in _reduce()}
    assert results == {a: "record_failed", b: "ok"}
    assert path.read_text() == "{}", "fail-closed state cannot regain a retry budget"


# ── M7 account-local anchors ───────────────────────────────────────────────


@pytest.mark.parametrize("account_order", [("A", "B"), ("B", "A")])
@pytest.mark.parametrize("peer_order", [("low", "high"), ("high", "low")])
def test_m7_jitter_shares_one_account_local_window(app, account_order, peer_order):
    """Case M7: raw resets b−1 and b+0 share one window under the rolling
    anchor; A and B anchors stay independent; insertion order is irrelevant."""
    boundary = ((NOW + 3600) // 600) * 600
    keys = {}
    for name in account_order:
        offset = 0 if name == "A" else 600
        for peer in peer_order:
            raw = boundary + offset - (1 if peer == "low" else 0)
            keys[name] = _spool(app, name, name + peer, five_reset=raw,
                                five=20 if peer == "low" else 30).binding.account_key
    assert all(r.status == "ok" for r in _reduce())
    for name in account_order:
        offset = 0 if name == "A" else 600
        anchor = _transition(keys[name]).five_hour_anchor
        assert anchor == (boundary + offset - 1, boundary + offset - 600)
        import _cctally_statusline as sl
        projection = sl._read_db_projection_once(account_key=keys[name])
        assert projection.five_hour.canonical_key == boundary + offset - 600
        assert projection.five_hour.percent == 30.0


# ── M8 / M9 provenance ─────────────────────────────────────────────────────


def _record_bound(app, name, *, weekly, five, at=0):
    binding = _binding(name)
    args = argparse.Namespace(
        percent=weekly, resets_at=str(NOW + 3 * 86400), five_hour_percent=five,
        five_hour_resets_at=str(NOW + 3600), source="statusline")
    assert app.record_usage_bound(args, binding=binding, nudge_dashboard=False) == 0
    return binding


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_m8_projection_fill_uses_the_requested_account(app, order):
    """Case M8: a five-hour-only candidate is filled from ITS account's weekly
    projection, and the plan records projection provenance for that account."""
    bindings = {name: _record_bound(app, name, weekly=w, five=w)
                for name, w in (("A", 40), ("B", 70))}
    for name in order:
        _spool(app, name, "five-only-" + name, five=80, seven=False)
    assert all(r.status == "ok" for r in _reduce())
    published = [r for r in _usage_obs() if r["payload"].get("publication_id")]
    assert len(published) == 2
    for record in published:
        provenance = record["payload"]["publication"]
        assert provenance["sevenDay"]["kind"] == "projection"
        assert provenance["sevenDay"]["account"] == record["account"]
        assert provenance["fiveHour"]["kind"] == "candidate"
        expected = 40 if record["account"] == bindings["A"].account_key else 70
        assert record["payload"]["weekly_percent"] == expected


@pytest.mark.parametrize("axis", ["sevenDay", "fiveHour"])
def test_m9_retained_low_expiry_keeps_arming_account_and_deadline(app, monkeypatch, axis):
    """Case M9: a pending drop armed in A is decided at A's ORIGINAL deadline
    from A's retained evidence even after the identity switches to B."""
    import _lib_statusline_candidates as lib
    high = _spool(app, "A", "peer", weekly=70, five=70)
    _reduce()
    _tick(monkeypatch, 2)
    _spool(app, "A", "peer", at=2, weekly=20, five=20)
    _reduce()
    pending = _transition(high.binding.account_key, 2).control.pending_drops[axis]
    assert pending.deadline_at == NOW + 2 + lib.PENDING_DROP_DEADLINE_SECONDS
    _identity("B")
    _tick(monkeypatch, pending.deadline_at - NOW)
    results = _reduce()
    assert [r.account_key for r in results] == [high.binding.account_key]
    last = [r for r in _usage_obs() if r["payload"].get("publication_id")][-1]
    assert last["account"] == high.binding.account_key
    assert last["payload"]["publication"][axis]["kind"] == "retained_low"
    after = _transition(high.binding.account_key, pending.deadline_at - NOW)
    still = after.control.pending_drops[axis]
    assert still is None or still.deadline_at == pending.deadline_at


# ── M16 / M17 recovery ─────────────────────────────────────────────────────


def _publication_obs():
    return [r for r in _usage_obs() if r["payload"].get("publication_id")]


@pytest.mark.parametrize("boundary", ["before_append", "after_append", "before_commit"])
def test_m16_stopped_publication_recovers_the_same_bound_plan(app, monkeypatch, boundary):
    """Case M16: a stop at each durable boundary recovers the SAME plan with
    its original binding (never the now-active account) and appends at most
    one observation."""
    import _cctally_journal as journal
    import _cctally_statusline as sl
    candidate = _spool(app, "A", "peer")
    account = candidate.binding.account_key
    if boundary == "before_append":
        owner, name = app, "cmd_record_usage"
    elif boundary == "after_append":
        owner, name = journal, "run_stats_ingest"
    else:
        owner, name = sl, "_commit_publication"
    real = getattr(owner, name)

    def stop(*args, **kwargs):
        # #825/#832 spec 4.3 revision 1: classification ingests BEFORE the
        # plan exists, so the after-append stop is the first ingest that
        # follows the observation append.
        if boundary == "after_append" and not _publication_obs():
            return real(*args, **kwargs)
        raise RuntimeError("process stop")

    monkeypatch.setattr(owner, name, stop)
    assert _reduce()[0].status == "record_failed"
    monkeypatch.setattr(owner, name, real)
    plan = _transition(account).publication
    assert plan is not None
    assert len(_publication_obs()) == (0 if boundary == "before_append" else 1)
    _identity("B")
    _tick(monkeypatch, 100)
    results = _reduce()
    assert [(r.account_key, r.status) for r in results] == [(account, "recovered")]
    records = _publication_obs()
    assert len(records) == 1
    assert records[0]["account"] == account
    assert records[0]["payload"]["publication_id"] == plan["publicationId"]
    assert records[0]["at"] == _iso(NOW)
    assert _snapshots(app) == [(account, 20.0, 30.0)]
    assert _transition(account, 100).publication is None


def test_m17_repeating_after_commit_is_a_no_op(app):
    """Case M17: after commit, another reducer run appends nothing."""
    _spool(app, "A", "peer")
    assert _reduce()[0].status == "ok"
    before = _journal()
    results = _reduce()
    assert all(r.status == "noop" for r in results)
    assert _journal() == before


def test_superseded_plan_is_dropped_by_a_same_account_authoritative_fence(app, monkeypatch):
    """A plan stopped before append is superseded by a later authoritative
    publication for the same account and window."""
    import _cctally_statusline as sl
    candidate = _spool(app, "A", "peer")
    account = candidate.binding.account_key
    real = app.cmd_record_usage
    monkeypatch.setattr(app, "cmd_record_usage", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("stopped before append")))
    _reduce()
    monkeypatch.setattr(app, "cmd_record_usage", real)
    plan = _transition(account).publication
    window = plan["sevenDay"]["canonicalKey"]
    sl._write_tombstone("sevenDay", sl._candidates.Tombstone(
        "sevenDay", "committed", block_received_at_through=NOW + 5),
        account_key=account, window=window)
    assert [r.status for r in _reduce()] == ["superseded"]
    assert _publication_obs() == []


# ── M18 expiry ─────────────────────────────────────────────────────────────


def test_m18_expiry_cleans_only_the_exact_account_namespace(app, monkeypatch):
    """Case M18: expiring A's closed-window fence and A's expired candidates
    never touches B's fences or candidates."""
    import _cctally_statusline as sl
    a = _binding("A")
    b = _binding("B")
    closed, live = NOW - 7200, NOW + 3 * 86400
    for binding in (a, b):
        for window in (closed, live):
            sl._write_tombstone("sevenDay", sl._candidates.Tombstone(
                "sevenDay", "committed", block_received_at_through=NOW - 10),
                account_key=binding.account_key, window=window)
    _spool(app, "B", "b-old", at=-200)
    _spool(app, "A", "a-now")
    _reduce()
    assert not sl._tombstone_path("sevenDay", account_key=a.account_key, window=closed).exists()
    assert sl._tombstone_path("sevenDay", account_key=a.account_key, window=live).exists()
    assert sl._tombstone_path("sevenDay", account_key=b.account_key, window=closed).exists()
    assert sl._tombstone_path("sevenDay", account_key=b.account_key, window=live).exists()


# ── #832 retry reopening through the reducer (Task 2.4) ────────────────────


def test_new_own_event_reopens_spent_retry_but_foreign_event_does_not(app, monkeypatch):
    """A spent attempt stays spent across a foreign account's new reset event;
    the account's own genuinely new event reopens it."""
    def record(name, five, at):
        _tick(monkeypatch, at)
        return _record_bound(app, name, weekly=20, five=five, at=at)

    record("A", 70, 0)
    record("A", 20, 2)
    binding = record("A", 20, 4)
    account = binding.account_key
    for at in (580, 582, 584):
        _tick(monkeypatch, at)
        _spool(app, "A", "peer", at=at, weekly=20, five=19)
        _reduce()
    spent = _transition(account, 584).control.pending_drops["fiveHour"]
    assert spent.kernel_stage == "suppressed" and spent.attempts == 2
    record("B", 70, 600)
    record("B", 20, 602)
    record("B", 20, 604)
    _tick(monkeypatch, 606)
    _spool(app, "A", "peer", at=606, weekly=20, five=19)
    _reduce()
    foreign = _transition(account, 606).control.pending_drops["fiveHour"]
    assert foreign.attempts == 2 and foreign.retry_signature == spent.retry_signature
    record("A", 80, 608)
    record("A", 20, 610)
    record("A", 20, 612)
    _tick(monkeypatch, 614)
    _spool(app, "A", "peer", at=614, weekly=20, five=19)
    _reduce()
    reopened = _transition(account, 614).control.pending_drops["fiveHour"]
    assert reopened is None or reopened.retry_signature != spent.retry_signature
