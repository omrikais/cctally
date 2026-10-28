"""#825/#832 spec 5.5 and section 7: cutover establishment by the first
current mutating producer and the pre-publication legacy spool rescan
(case F1, Task 2.7), plus the append-path chokepoint every stamped record
goes through.
"""
from __future__ import annotations

import builtins
import datetime as dt
import json
import os
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
    import _cctally_journal
    monkeypatch.setattr(_cctally_journal, "_ACCOUNT_BOUND_CUTOVER_MEMO", None,
                        raising=False)
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    return cctally


def _identity(name: str) -> str:
    import _cctally_core as core
    import _lib_accounts
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": name, "emailAddress": f"{name}@example.test"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _lib_accounts.account_key("claude", name)


def _parsed(app, session, weekly=20, five=30):
    return app._lib_statusline.parse_statusline_stdin(json.dumps({
        "session_id": session, "rate_limits": {
            "seven_day": {"used_percentage": weekly, "resets_at": _iso(NOW + 3 * 86400)},
            "five_hour": {"used_percentage": five, "resets_at": _iso(NOW + 3600)},
        }}).encode())


def _lines():
    """``(segment, offset, record)`` for every journal line, in order."""
    import _cctally_core as core
    import _cctally_journal
    result = []
    for segment in _cctally_journal.list_segments():
        data = (core.JOURNAL_DIR / segment).read_bytes()
        offset = 0
        for raw in data.splitlines(keepends=True):
            if raw.strip():
                result.append((segment, offset, json.loads(raw)))
            offset += len(raw)
    return result


def _kind(record):
    return (record.get("payload") or {}).get("kind")


def _index(lines, predicate):
    return next(i for i, (_s, _o, r) in enumerate(lines) if predicate(r))


def _legacy_obs_line(at=NOW - 3600):
    """A previous-release (unstamped) Claude usage observation line."""
    import _lib_journal
    record = _lib_journal.make_obs(
        at=_iso(at), src="statusline", provider="claude",
        payload={"weekly_percent": 10.0, "resets_at": NOW + 3 * 86400,
                 "source": "statusline", "captured_at": _iso(at)},
        writer_generation=None)
    return _lib_journal.encode_line(record)


def _append_raw(data: bytes) -> None:
    import _cctally_core as core
    import _lib_journal
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    segment = core.JOURNAL_DIR / _lib_journal.segment_name(
        dt.datetime.fromtimestamp(NOW, dt.timezone.utc))
    with open(segment, "ab") as handle:
        handle.write(data)


# ── F1: the statusline alone establishes the cutover and keeps recording ───


def test_f1_first_statusline_tick_establishes_the_cutover_and_records(app):
    """Case F1 (C3): an upgraded store — legacy history, no cutover, no hooks
    and no manual command — gets its cutover from the detached reducer of
    the first eligible statusline tick, which then publishes."""
    import _cctally_journal
    import _cctally_statusline as sl
    app.open_db().close()
    _append_raw(_legacy_obs_line())
    assert _cctally_journal.find_account_bound_cutover() is None
    key = _identity("A")

    sl._statusline_persist(_parsed(app, "peer"), sync_for_test=True)

    boundary = _cctally_journal.find_account_bound_cutover()
    assert boundary is not None, "the reducer must establish the cutover"
    lines = _lines()
    cutover = _index(lines, lambda r: _kind(r) == "account_bound_cutover")
    usage = _index(lines, lambda r: r.get("t") == "obs" and r.get("account") == key)
    assert cutover < usage
    legacy = _index(lines, lambda r: r.get("t") == "obs" and "wpg" not in r)
    assert legacy < cutover, "pre-boundary history stays historical"
    conn = app.open_db()
    try:
        rows = conn.execute(
            "SELECT account_key, weekly_percent FROM weekly_usage_snapshots "
            "WHERE account_key = ?", (key,)).fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [(key, 20.0)], "non-vacuous recording progress"


def test_render_never_establishes_the_cutover(app, monkeypatch):
    import _cctally_journal
    import _cctally_statusline as sl
    app.open_db().close()
    _identity("A")
    forks = []
    monkeypatch.setattr(sl, "_fork_persist", lambda fd, reservation: (
        forks.append(fd), sl._release_reservation(reservation)))
    sl._statusline_persist(_parsed(app, "peer"))
    assert forks
    assert _cctally_journal.find_account_bound_cutover() is None
    assert _lines() == []


# ── legacy spool rescan (spec 5.5 step 6, section 7) ───────────────────────


def _legacy_candidate(percent):
    import _cctally_core as core
    core.STATUSLINE_CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    (core.STATUSLINE_CANDIDATE_DIR / ("c" * 64 + ".json")).write_text(json.dumps({
        "schemaVersion": 1, "receivedAt": NOW,
        "sevenDay": {"percent": float(percent), "resetsAt": NOW + 3 * 86400}}))


def test_legacy_candidate_between_runs_journals_one_activity_before_publication(app):
    """A legacy candidate written between two reducer runs produces exactly
    one `legacy_spool_activity` op, appended before the next publication."""
    import _cctally_statusline as sl
    app.open_db().close()
    key = _identity("A")
    sl._statusline_persist(_parsed(app, "peer", weekly=20), sync_for_test=True)
    assert not [r for _s, _o, r in _lines() if _kind(r) == "legacy_spool_activity"]

    _legacy_candidate(70)
    sl._write_candidate(sl._candidate_from_input(
        _parsed(app, "peer-2", weekly=30), received_at=NOW))
    sl._statusline_reduce_and_publish(nudge_sink=lambda: None)

    lines = _lines()
    activity = [i for i, (_s, _o, r) in enumerate(lines)
                if _kind(r) == "legacy_spool_activity"]
    assert len(activity) == 1
    record = lines[activity[0]][2]
    assert record["payload"]["reason"] == "changed"
    assert "wpg" in record
    published = [i for i, (_s, _o, r) in enumerate(lines)
                 if r.get("t") == "obs" and r.get("account") == key
                 and r["payload"].get("weekly_percent") == 30.0]
    assert published and activity[0] < published[0]

    sl._statusline_reduce_and_publish(nudge_sink=lambda: None)
    assert len([1 for _s, _o, r in _lines()
                if _kind(r) == "legacy_spool_activity"]) == 1, (
        "an unchanged legacy spool is not a second trigger")


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_unavailable_baseline_is_a_trigger_never_silently_reinitialized(app, damage):
    import _cctally_core as core
    import _cctally_statusline as sl
    app.open_db().close()
    _identity("A")
    sl._statusline_persist(_parsed(app, "peer"), sync_for_test=True)
    path = core.LEGACY_SPOOL_BASELINE_PATH
    if damage == "missing":
        path.unlink()
    else:
        doc = json.loads(path.read_text())
        doc["entries"] = [{"path": "statusline-selected.json", "size": 1,
                           "mtimeNs": 1, "sha256": "0" * 64}]
        path.write_text(json.dumps(doc))
    sl._statusline_reduce_and_publish(nudge_sink=lambda: None)
    reasons = [r["payload"]["reason"] for _s, _o, r in _lines()
               if _kind(r) == "legacy_spool_activity"]
    assert reasons == ["baseline-unavailable"]


# ── the append chokepoint (spec 5.5) ───────────────────────────────────────


def test_stamped_append_without_a_cutover_establishes_it_first(app):
    """A stamped append on a store without the op yields the op at a smaller
    coordinate than the record."""
    import _cctally_journal
    import _lib_journal
    app.open_db().close()
    _append_raw(_legacy_obs_line())
    record = _lib_journal.make_op(at=_iso(NOW), src="test", payload={
        "kind": "account_label", "provider": "claude", "account_key": "x" * 32,
        "label": "work"})
    _cctally_journal.append_record(record)
    lines = _lines()
    cutover = _index(lines, lambda r: _kind(r) == "account_bound_cutover")
    appended = _index(lines, lambda r: r.get("id") == _lib_journal.prepare_record(record)["id"])
    assert cutover < appended
    assert (lines[cutover][0], lines[cutover][1]) < (lines[appended][0], lines[appended][1])


def test_group_append_without_a_cutover_establishes_it_first(app):
    import _cctally_journal
    import _lib_journal
    app.open_db().close()
    records = [_lib_journal.make_op(at=_iso(NOW), src="test", payload={
        "kind": "account_label", "provider": "claude", "account_key": "y" * 32,
        "label": f"l{i}"}) for i in range(2)]
    _cctally_journal.append_records(records)
    kinds = [_kind(r) for _s, _o, r in _lines()]
    # #797/#823/#840 (spec 4.6 B2): on a fresh store the revision-2 op
    # follows the cutover op immediately.
    assert kinds == ["account_bound_cutover", "account_bound_revision",
                     "account_label", "account_label"]


def test_concurrent_legacy_append_is_classified_by_physical_position(app, monkeypatch):
    """A legacy record an old process appends while the cutover is being
    established is classified by where it physically lands: before the op it
    is historical input; after it, excluded."""
    import _cctally_journal
    import _lib_account_protocol
    import _lib_journal
    app.open_db().close()
    seen = []

    def seam(step):
        if step == "after-scan" and not seen:
            seen.append(step)
            _append_raw(_legacy_obs_line(at=NOW - 60))

    monkeypatch.setattr(_cctally_journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", seam)
    _cctally_journal.append_record(_lib_journal.make_op(
        at=_iso(NOW), src="test", payload={
            "kind": "account_label", "provider": "claude",
            "account_key": "z" * 32, "label": "x"}))
    monkeypatch.setattr(_cctally_journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", None)
    _append_raw(_legacy_obs_line(at=NOW + 60))
    boundary = _cctally_journal.find_account_bound_cutover()
    legacy = [(s, o, r) for s, o, r in _lines() if r.get("t") == "obs" and "wpg" not in r]
    assert len(legacy) == 2
    early, late = legacy
    assert _lib_account_protocol.is_historical(
        _lib_account_protocol.Coordinate(early[0], early[1]), boundary)
    assert not _lib_account_protocol.is_historical(
        _lib_account_protocol.Coordinate(late[0], late[1]), boundary)


def test_steady_state_append_does_not_scan_or_reopen_the_journal(app, monkeypatch):
    """After the cutover is known, an append checks it in O(1): it opens only
    its own target segment once and never scans or line-reads the journal."""
    import _cctally_core as core
    import _cctally_journal
    import _lib_journal
    app.open_db().close()

    def op(i):
        return _lib_journal.make_op(at=_iso(NOW + i), src="test", payload={
            "kind": "account_label", "provider": "claude",
            "account_key": "w" * 32, "label": f"l{i}"})

    _cctally_journal.append_record(op(0))
    scans = []
    monkeypatch.setattr(_cctally_journal, "_scan_for_cutover",
                        lambda *a, **k: scans.append(1))
    monkeypatch.setattr(_cctally_journal, "_read_line_at",
                        lambda *a, **k: scans.append(1))
    opened = []
    real_open, real_os_open = builtins.open, os.open
    journal_dir = str(core.JOURNAL_DIR)

    def tracking_open(file, *args, **kwargs):
        if str(file).startswith(journal_dir) and str(file).endswith(".jsonl"):
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def tracking_os_open(path, *args, **kwargs):
        if str(path).startswith(journal_dir) and str(path).endswith(".jsonl"):
            opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    monkeypatch.setattr(os, "open", tracking_os_open)
    for i in range(1, 6):
        _cctally_journal.append_record(op(i))
    assert scans == []
    assert len(opened) == 5, opened
