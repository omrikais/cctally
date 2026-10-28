"""#797/#823/#840 Task 1 — revision 2 of the account-bound fence (spec 4.6,
B1–B7; section 6 T6 lives in `test_mixed_schedule_revision2_797.py`).

B1: every newly authored record carries `wpr: 2` beside the unchanged
`wpg: "account-bound-v1"`; retained records are never restamped. B2: the
durable `account_bound_revision` op follows the original cutover and is
established before every authoritative decision. B3: after it, a Claude-scoped
record without `wpr == 2` is excluded and quarantines automatic outcomes. B4:
the v2 spool namespace; the v1 coordination state is legacy spool input. B5:
the v3 repair acknowledgement and revision-specific calibration provenance.
B6: epoch 1019. Q8's counterexample is the acceptance trace.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import time

import pytest

from conftest import load_script, redirect_paths
import _outcome_quarantine_trace as T

NOW = 1791417600
WEEK_END = "2026-10-12T00:00:00Z"


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
    monkeypatch.setattr(_cctally_journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", None,
                        raising=False)
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


def _lines():
    """``(segment, offset, record)`` for every journal line, in order."""
    import _cctally_core as core
    import _cctally_journal
    import _lib_journal
    result = []
    for segment in sorted(_cctally_journal.list_segments(),
                          key=_lib_journal.segment_sort_key):
        data = (core.JOURNAL_DIR / segment).read_bytes()
        offset = 0
        for raw in data.splitlines(keepends=True):
            if raw.strip():
                result.append((segment, offset, json.loads(raw)))
            offset += len(raw)
    return result


def _kind(record):
    return (record.get("payload") or {}).get("kind")


def _journal_bytes() -> dict:
    import _cctally_core as core
    return {path.name: path.read_bytes()
            for path in sorted(core.JOURNAL_DIR.glob("*.jsonl"))}


def _without_revision(record: dict) -> dict:
    """``record`` as a revision-1 writer (the previous release, which stamps
    `wpg` but not `wpr`) wrote it: the stamp dropped and a content id
    recomputed over the remaining core. Built by hand so the RED run against
    the previous code exercises the same bytes."""
    import _lib_journal as J
    out = {k: v for k, v in record.items() if k != "wpr"}
    rid = record.get("id")
    if isinstance(rid, str) and rid.startswith("o:"):
        core = {k: v for k, v in out.items() if k not in ("v", "id")}
        out["id"] = J.content_id(core)
    return out


def _original_cutover_only():
    """Establish ONLY the #825 cutover, as the previous release left a
    store: the boundary op present, no revision op yet."""
    import _cctally_journal
    ensure = getattr(_cctally_journal, "_ensure_original_cutover",
                     _cctally_journal.ensure_account_bound_cutover)
    return ensure()


def _revision_ops():
    import _lib_account_protocol as P
    kind = getattr(P, "REVISION_OP_KIND", "account_bound_revision")
    return [(s, o, r) for s, o, r in _lines() if _kind(r) == kind]


# ── B1: the revision stamp ─────────────────────────────────────────────────


def test_new_records_carry_wpr_2_and_keep_wpg_v1(app):
    import _cctally_journal
    import _lib_account_protocol as P
    import _lib_journal as J
    assert P.REVISION_KEY == "wpr"
    assert P.PROTOCOL_REVISION == 2
    assert P.PROTOCOL_GENERATION == "account-bound-v1"
    assert P.REVISION_OP_KIND == "account_bound_revision"
    assert P.REVISION_OP_ID == "account-bound-revision-2"

    obs = J.make_obs(at=_iso(NOW), src="test", provider="claude",
                     payload={"weekly_percent": 1.0})
    evt = J.make_evt(kind="percent_milestone", id="e:x", at=_iso(NOW),
                     payload={"n": 1})
    batch = J.make_correction_batch(batch_id="b:x", family="f", at=_iso(NOW),
                                    actions=[])
    for record in (obs, evt, *batch):
        assert record["wpg"] == "account-bound-v1"
        assert record["wpr"] == 2 and type(record["wpr"]) is int
        assert P.is_stamped(record) and P.has_current_revision(record)
    # A content-id record is identified over its stamped core.
    core = {k: v for k, v in obs.items() if k not in ("v", "id")}
    assert obs["id"] == J.content_id(core)

    # The shared preparation stamps both before content identity: an
    # unstamped content-id record is re-identified, a semantic id kept.
    bare = J.make_obs(at=_iso(NOW), src="test", provider="claude",
                      payload={"weekly_percent": 2.0}, writer_generation=None)
    prepared = J.prepare_record(bare)
    assert prepared["wpg"] == "account-bound-v1" and prepared["wpr"] == 2
    assert prepared["id"] == J.content_id(
        {k: v for k, v in prepared.items() if k not in ("v", "id")})
    revision1 = _without_revision(obs)
    assert J.prepare_record(revision1)["wpr"] == 2
    assert J.prepare_record(revision1)["id"] == obs["id"]
    assert J.prepare_record(obs) is obs

    # Every appended line carries both stamps.
    _cctally_journal.append_record(J.make_op(
        at=_iso(NOW), src="test", payload={
            "kind": "account_label", "provider": "claude",
            "account_key": "a" * 32, "label": "work"}))
    appended = [r for _s, _o, r in _lines() if _kind(r) == "account_label"]
    assert len(appended) == 1
    assert appended[0]["wpg"] == "account-bound-v1" and appended[0]["wpr"] == 2
    # The journal line format version is unchanged.
    assert all(r["v"] == 1 for _s, _o, r in _lines())


def test_retained_records_are_never_restamped(app):
    import _cctally_core as core
    import _cctally_journal
    import _lib_journal as J
    legacy = J.unstamped_record(J.make_obs(
        at=_iso(NOW - 7200), src="record-usage", provider="claude",
        payload={"weekly_percent": 5.0, "resets_at": T.epoch(WEEK_END),
                 "source": "api", "captured_at": _iso(NOW - 7200)}))
    revision1 = _without_revision(J.make_obs(
        at=_iso(NOW - 3600), src="record-usage", provider="claude",
        payload={"weekly_percent": 6.0, "resets_at": T.epoch(WEEK_END),
                 "source": "api", "captured_at": _iso(NOW - 3600)}))
    T.raw_append([legacy, revision1])
    before = _journal_bytes()

    _cctally_journal.ensure_account_bound_cutover()
    T.ingest(app)
    T.rebuild(app)

    after = _journal_bytes()
    for name, data in before.items():
        assert after[name][:len(data)] == data, "a retained line was rewritten"
    # The retained revision-1 record is still exactly its own bytes, and no
    # second, restamped copy of it was appended.
    ids = [r.get("id") for _s, _o, r in _lines()]
    assert ids.count(revision1["id"]) == 1
    assert J.prepare_record(revision1)["id"] not in ids or (
        J.prepare_record(revision1)["id"] == revision1["id"])
    assert core.JOURNAL_DIR.exists()


def test_metadata_only_duplicate_guard_ignores_wpr(app):
    import _cctally_journal
    import _lib_account_protocol as P
    import _lib_journal as J
    current = J.make_evt(kind="percent_milestone", id="e:dup", at=_iso(NOW),
                         payload={"n": 7})
    retained = _without_revision(current)
    assert "wpr" not in retained and retained["wpg"] == "account-bound-v1"
    # Transport metadata: stripped for semantic comparison.
    assert P.strip_stamp(current) == P.strip_stamp(retained)
    assert "wpr" not in P.strip_stamp(current)
    assert J.event_content_hash(current) == J.event_content_hash(retained)

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE journal_effective_events "
                 "(event_id TEXT PRIMARY KEY, event_json TEXT)")
    conn.execute("INSERT INTO journal_effective_events VALUES (?, ?)",
                 ("e:dup", J.encode_line(retained).decode("utf-8").strip()))
    # A current emission differing from the retained revision-1 event only by
    # `wpr` is a metadata-only duplicate: never appended as a second line.
    assert _cctally_journal._is_metadata_only_duplicate(conn, current)
    conn.execute("UPDATE journal_effective_events SET event_json = ? "
                 "WHERE event_id = 'e:dup'",
                 (J.encode_line(current).decode("utf-8").strip(),))
    # A byte-identical duplicate still appends (crash-replay convergence).
    assert not _cctally_journal._is_metadata_only_duplicate(conn, current)
    conn.close()


def test_revision_check_accepts_exactly_int_2(app):
    import _lib_account_protocol as P
    assert P.has_current_revision({"wpr": 2})
    for value in (3, 1, "2", True, 2.0, None, [2]):
        assert not P.has_current_revision({"wpr": value}), value
    assert not P.has_current_revision({})
    assert not P.has_current_revision(None)
    # `is_stamped` keeps its meaning: the generation stamp alone.
    assert P.is_stamped({"wpg": "account-bound-v1"})
    stamped = P.stamp({"t": "obs"})
    assert stamped["wpg"] == "account-bound-v1" and stamped["wpr"] == 2
    assert P.strip_stamp(stamped) == {"t": "obs"}


# ── B2: the revision boundary op ───────────────────────────────────────────


def _usage_args(weekly):
    import argparse
    return argparse.Namespace(percent=weekly, resets_at=str(T.epoch(WEEK_END)),
                              five_hour_percent=None, five_hour_resets_at=None,
                              source="api")


def _entry_ingest(app, monkeypatch):
    T.ingest(app)


def _entry_refresh(app, monkeypatch):
    monkeypatch.setattr(app, "_resolve_oauth_token", lambda: "credential-A")
    monkeypatch.setattr(app, "_fetch_oauth_usage", lambda **kw: {
        "seven_day": {"utilization": 40, "resets_at": _iso(NOW + 3 * 86400)},
        "five_hour": {"utilization": 45, "resets_at": _iso(NOW + 3600)}})
    assert app._refresh_usage_inproc().status == "ok"


def _entry_manual(app, monkeypatch):
    import _cctally_statusline as sl
    assert app.record_usage_bound(
        _usage_args(30), binding=sl._capture_binding(),
        nudge_dashboard=False) == 0


def _entry_maintenance(app, monkeypatch):
    T.retain_session_input(app, "A")
    T.rederive(app)


def _entry_recovery(app, monkeypatch):
    T.rebuild(app)


def _entry_statusline(app, monkeypatch):
    import _cctally_statusline as sl
    parsed = app._lib_statusline.parse_statusline_stdin(json.dumps({
        "session_id": "peer", "rate_limits": {
            "seven_day": {"used_percentage": 25,
                          "resets_at": _iso(NOW + 3 * 86400)},
            "five_hour": {"used_percentage": 30,
                          "resets_at": _iso(NOW + 3600)}}}).encode())
    sl._statusline_persist(parsed, sync_for_test=True)


ENTRY_POINTS = {
    "ingest": _entry_ingest,
    "refresh": _entry_refresh,
    "manual-recording": _entry_manual,
    "maintenance-planning": _entry_maintenance,
    "recovery": _entry_recovery,
    "statusline-detached-child": _entry_statusline,
}


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_revision_boundary_precedes_every_authoritative_decision(
        app, monkeypatch, entry):
    """A store the previous release left (original cutover, no revision op)
    gets the revision op from whichever authoritative producer runs first,
    before anything that producer journals."""
    import _cctally_journal
    _identity("A")
    _original_cutover_only()
    assert _revision_ops() == []
    before = len(_lines())

    ENTRY_POINTS[entry](app, monkeypatch)

    lines = _lines()
    revisions = [i for i, (_s, _o, r) in enumerate(lines)
                 if _kind(r) == "account_bound_revision"]
    assert len(revisions) == 1, f"{entry} did not establish the revision"
    cutover = next(i for i, (_s, _o, r) in enumerate(lines)
                   if _kind(r) == "account_bound_cutover")
    assert cutover < revisions[0]
    # Everything the entry point journalled lands after the boundary; only
    # the establishment's own reconciliation may precede it.
    for _s, _o, record in lines[before:revisions[0]]:
        assert _kind(record) == "legacy_spool_activity"
    boundary = _cctally_journal.find_account_bound_cutover()
    assert boundary.revision is not None


def test_render_never_establishes_the_revision(app, monkeypatch):
    import _cctally_statusline as sl
    _identity("A")
    _original_cutover_only()
    before = _journal_bytes()
    forks = []
    monkeypatch.setattr(sl, "_fork_persist", lambda fd, reservation: (
        forks.append(fd), sl._release_reservation(reservation)))
    parsed = app._lib_statusline.parse_statusline_stdin(json.dumps({
        "session_id": "peer", "rate_limits": {
            "seven_day": {"used_percentage": 25,
                          "resets_at": _iso(NOW + 3 * 86400)}}}).encode())
    sl._statusline_persist(parsed)
    assert forks
    assert _journal_bytes() == before
    assert _revision_ops() == []


def test_fresh_install_appends_revision_op_after_cutover_op(app):
    import _cctally_journal
    import _lib_account_protocol as P
    import _lib_journal as J
    records = [J.make_op(at=_iso(NOW), src="test", payload={
        "kind": "account_label", "provider": "claude", "account_key": "y" * 32,
        "label": f"l{i}"}) for i in range(2)]
    _cctally_journal.append_records(records)
    lines = _lines()
    assert [_kind(r) for _s, _o, r in lines] == [
        "account_bound_cutover", "account_bound_revision",
        "account_label", "account_label"]
    (c_seg, c_off, cutover), (r_seg, r_off, revision) = lines[0], lines[1]
    assert revision["id"] == P.REVISION_OP_ID
    assert P.is_stamped(revision) and P.has_current_revision(revision)
    decoded = P.revision_boundary_from_op(revision)
    assert (decoded.cutover_segment, decoded.cutover_offset) == (c_seg, c_off)
    assert (decoded.segment, decoded.end_offset) == (r_seg, r_off)
    assert decoded.frozen_extents == {c_seg: r_off}
    payload = P.revision_boundary_payload(decoded)
    assert payload == {k: v for k, v in revision["payload"].items()
                       if k != "kind"}
    boundary = _cctally_journal.find_account_bound_cutover()
    assert boundary.revision == decoded
    # Idempotent.
    _cctally_journal.ensure_account_bound_cutover()
    assert len(_revision_ops()) == 1
    assert cutover["id"] == P.CUTOVER_OP_ID


class _Crash(RuntimeError):
    pass


@pytest.mark.parametrize("step", [
    "revision-after-scan", "revision-after-baseline-capture",
    "revision-after-append", "revision-after-fsync",
    "revision-after-baseline-install",
])
def test_crash_at_each_durability_step_recovers_one_boundary(
        app, monkeypatch, step):
    import _cctally_core as core
    import _cctally_journal
    _original_cutover_only()
    v1 = core.STATUSLINE_ACCOUNT_BOUND_V1_DIR
    (v1 / "accounts" / "k" / "candidates").mkdir(parents=True)
    (v1 / "accounts" / "k" / "candidates" / "c.json").write_text("{}")

    def seam(name):
        if name == step:
            raise _Crash(name)

    monkeypatch.setattr(_cctally_journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", seam)
    with pytest.raises(_Crash):
        _cctally_journal.ensure_account_bound_cutover()
    monkeypatch.setattr(_cctally_journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", None)

    boundary = _cctally_journal.ensure_account_bound_cutover()
    again = _cctally_journal.ensure_account_bound_cutover()
    assert boundary.revision is not None
    assert again.revision == boundary.revision
    assert len(_revision_ops()) == 1
    assert len([1 for _s, _o, r in _lines()
                if _kind(r) == "account_bound_cutover"]) == 1
    assert _cctally_journal.find_account_bound_cutover().revision == (
        boundary.revision)
    baseline = _cctally_journal.read_anchored_legacy_spool_baseline()
    if step in ("revision-after-append", "revision-after-fsync"):
        # The install was lost with the crashed producer: the reducer reports
        # baseline-unavailable, never a silent re-initialization.
        assert baseline is None
    else:
        assert baseline is not None
        assert baseline.digest() == boundary.revision.legacy_spool_baseline_digest
        assert any("statusline-account-bound-v1/accounts" in entry[0]
                   for entry in baseline.entries)


# ── B3: composed admission (Q8's counterexample) ───────────────────────────


def test_q8_counterexample_old_stale_30_is_excluded_and_reset_withheld(
        app, monkeypatch):
    """Credited baseline 0; an old (revision-1) reducer's stale 30 lands
    after the boundary without `wpr`; API 20, then API 0. Mixed, the clamped
    baseline of 30 minted a reset neither uniform run has. Under the
    composed fence the 30 is excluded, every Claude account is quarantined,
    and no reset is minted."""
    import _lib_journal as J
    T.current(app, monkeypatch, at="2026-10-08T10:00:00Z", weekly=0,
              week_end=WEEK_END)
    import _lib_accounts
    stale = _without_revision(J.make_obs(
        at="2026-10-08T10:05:00Z", src="record-usage", provider="claude",
        account=_lib_accounts.account_key("claude", "A"),
        payload={"weekly_percent": 30.0, "resets_at": T.epoch(WEEK_END),
                 "source": "api", "captured_at": "2026-10-08T10:05:00Z"}))
    assert stale["wpg"] == "account-bound-v1" and "wpr" not in stale
    T.raw_append([stale])
    T.current(app, monkeypatch, at="2026-10-08T10:10:00Z", weekly=20,
              week_end=WEEK_END)
    T.current(app, monkeypatch, at="2026-10-08T10:15:00Z", weekly=0,
              week_end=WEEK_END)

    assert T.outcome_count(T.outcomes(app)) == 0, T.outcomes(app)
    assert T.account_quarantined(app, "A")
    assert T.account_quarantined(app, "B")
    assert T.account_quarantined(app, "unattributed")
    conn = app.open_db()
    try:
        weekly = [row[0] for row in conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots")]
    finally:
        conn.close()
    assert 30.0 not in weekly, "the stale 30 must be excluded input"
    # The same answer after a real rebuild from the journal.
    T.rebuild(app)
    assert T.outcome_count(T.outcomes(app)) == 0
    assert T.account_quarantined(app, "A")


def test_ingest_admission_boundary_notices_new_revision_op(app, monkeypatch):
    import _cctally_journal
    import _lib_account_protocol as P
    import _lib_accounts
    import _lib_journal as J
    _original_cutover_only()
    monkeypatch.setattr(_cctally_journal, "establish_revision_before_decision",
                        lambda: None, raising=False)
    T.ingest(app)
    conn = app.open_db()
    try:
        summary = _cctally_journal._read_admission_summary(conn)
    finally:
        conn.close()
    assert summary is not None and summary.boundary is not None
    assert getattr(summary, "revision_id", None) is None

    _cctally_journal.ensure_account_bound_cutover()
    stale = _without_revision(J.make_obs(
        at=_iso(NOW - 60), src="record-usage", provider="claude",
        account=_lib_accounts.account_key("claude", "A"),
        payload={"weekly_percent": 44.0, "resets_at": T.epoch(WEEK_END),
                 "source": "api", "captured_at": _iso(NOW - 60)}))
    T.raw_append([stale])
    T.ingest(app)

    conn = app.open_db()
    try:
        summary = _cctally_journal._read_admission_summary(conn)
        weekly = [row[0] for row in conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots")]
    finally:
        conn.close()
    assert summary.revision_id == P.REVISION_OP_ID
    assert summary.boundary.revision is not None
    assert summary.excluded_total == 1
    assert 44.0 not in weekly


def _outcome_multiset(app):
    """Every C5 outcome and every stored weekly reading, as one multiset."""
    import collections
    conn = app.open_db()
    try:
        weekly = [("weekly", row[0]) for row in conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots")]
    finally:
        conn.close()
    return collections.Counter(
        [(kind, item) for kind, items in T.outcomes(app).items()
         for item in items] + weekly)


def test_ingest_tick_establishes_the_revision_on_a_cutover_cache_miss(
        app, monkeypatch):
    """Review RV-1 (spec 4.6 B2): an upgraded store whose original cutover
    exists while its v2 cutover cache does not (the v2 directory starts
    empty). The ingest tick establishes the revision boundary from the
    boundary its index already holds, without scanning the journal, so an
    old-release write after the tick is post-boundary and excluded — rather
    than history the first new producer's boundary lands after. Live,
    rebuild and rederive agree."""
    import _cctally_core as core
    import _cctally_journal
    import _lib_accounts
    import _lib_journal as J
    for name in ("A", "B", "unattributed"):
        T.retain_session_input(app, name)
    _identity("A")
    _original_cutover_only()
    account = _lib_accounts.account_key("claude", "A")

    def reading(at, weekly):
        return _without_revision(J.make_obs(
            at=at, src="record-usage", provider="claude", account=account,
            payload={"weekly_percent": float(weekly),
                     "resets_at": T.epoch(WEEK_END), "source": "api",
                     "captured_at": at}))

    # The previous release's history, ingested by the previous release
    # (whose appends establish only the original cutover).
    T.raw_append([reading("2026-10-08T10:00:00Z", 0)])
    with monkeypatch.context() as previous:
        previous.setattr(_cctally_journal, "establish_revision_before_decision",
                         lambda **kw: None)
        previous.setattr(_cctally_journal, "ensure_account_bound_cutover",
                         lambda **kw: _cctally_journal._ensure_original_cutover(
                             **kw))
        T.ingest(app)
    assert _revision_ops() == []
    # The upgrade: an empty v2 directory, so no verified cutover cache.
    core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.unlink(missing_ok=True)
    core.ACCOUNT_BOUND_REVISION_CACHE_PATH.unlink(missing_ok=True)
    scans = []
    real_scan = _cctally_journal._scan_for_cutover_traversed
    monkeypatch.setattr(_cctally_journal, "_scan_for_cutover_traversed",
                        lambda *a, **kw: (scans.append(1), real_scan(*a, **kw))[1])

    T.ingest(app)
    assert len(_revision_ops()) == 1, "the ingest tick deferred the boundary"
    assert scans == [], "the tick scanned the journal for the cutover"

    # An old-release write after the tick, then the first new producer.
    T.raw_append([reading("2026-10-08T10:05:00Z", 30)])
    T.current(app, monkeypatch, at="2026-10-08T10:10:00Z", weekly=20,
              week_end=WEEK_END)
    T.current(app, monkeypatch, at="2026-10-08T10:15:00Z", weekly=0,
              week_end=WEEK_END)
    lines = _lines()
    revision = next(i for i, (_s, _o, r) in enumerate(lines)
                    if _kind(r) == "account_bound_revision")
    stale = next(i for i, (_s, _o, r) in enumerate(lines)
                 if (r.get("payload") or {}).get("weekly_percent") == 30.0)
    assert revision < stale, "the old write landed before the boundary"
    live = _outcome_multiset(app)
    assert ("weekly", 30.0) not in live, "the old write was admitted"
    assert T.account_quarantined(app, "A")
    T.rebuild(app)
    assert _outcome_multiset(app) == live
    T.rederive(app)
    assert _outcome_multiset(app) == live


def test_a_revision_cache_miss_is_scanned_at_most_once(app, monkeypatch):
    """Review RV-2: with the cutover cache present and the revision cache
    missing, the tick that finds the revision op by a scan publishes it, so
    the next tick reads no consumed segment for it."""
    import _cctally_core as core
    import _cctally_journal
    _identity("A")
    _cctally_journal.ensure_account_bound_cutover()
    T.ingest(app)
    core.ACCOUNT_BOUND_REVISION_CACHE_PATH.unlink()
    scans = []
    real = _cctally_journal._scan_for_revision_traversed
    monkeypatch.setattr(_cctally_journal, "_scan_for_revision_traversed",
                        lambda *a, **kw: (scans.append(1), real(*a, **kw))[1])
    T.ingest(app)
    assert scans, "the first tick must find the revision by a scan"
    assert core.ACCOUNT_BOUND_REVISION_CACHE_PATH.exists()
    scans.clear()
    T.ingest(app)
    assert scans == [], "the revision was scanned for again"


def test_codex_only_exemption_survives_the_revision(app):
    import _cctally_journal
    import _lib_account_protocol as P
    import _lib_journal as J
    boundary = _cctally_journal.ensure_account_bound_cutover()
    revision = boundary.revision
    after = P.Coordinate(revision.segment, revision.end_offset + 10_000)
    codex = _without_revision(J.make_obs(
        at=_iso(NOW), src="codex", provider="codex",
        payload={"kind": "quota_window_snapshot"}))
    claude = _without_revision(J.make_obs(
        at=_iso(NOW), src="record-usage", provider="claude",
        payload={"weekly_percent": 1.0}))
    assert P.classify_admission(codex, after, boundary).admitted
    verdict = P.classify_admission(claude, after, boundary)
    assert not verdict.admitted and verdict.reason == "revision-unstamped"
    assert "revision-unstamped" in P.ADMISSION_REASONS
    # The revision never re-admits what the original cutover excludes.
    unstamped = J.unstamped_record(claude)
    assert P.classify_admission(unstamped, after, boundary).reason == "excluded"
    # Before the revision boundary a revision-1 record stays admitted.
    cut_after = P.Coordinate(revision.segment, revision.end_offset - 1)
    if P.is_historical(cut_after, revision):
        assert P.classify_admission(claude, cut_after, boundary).admitted


# ── B4: the v2 spool namespace ─────────────────────────────────────────────


def test_v1_coordination_state_joins_legacy_inventory(app):
    import _cctally_core as core
    import _cctally_journal
    import _cctally_statusline as sl
    assert core.STATUSLINE_ACCOUNT_BOUND_DIR.name == "statusline-account-bound-v2"
    assert core.STATUSLINE_ACCOUNT_BOUND_V1_DIR.name == (
        "statusline-account-bound-v1")
    v1 = core.STATUSLINE_ACCOUNT_BOUND_V1_DIR
    (v1 / "accounts" / "k" / "candidates").mkdir(parents=True)
    (v1 / "accounts" / "k" / "candidates" / "c.json").write_text('{"p": 1}')
    (v1 / "transport.last").write_text("1")
    for diagnostic in ("cutover-coordinate.json", "legacy-spool-baseline.json",
                       "legacy-writer-evidence.json"):
        (v1 / diagnostic).write_text("{}")

    paths = {str(p) for p in _cctally_journal._legacy_spool_paths()}
    assert str(v1 / "accounts") in paths
    assert str(v1 / "transport.last") in paths
    assert not any("cutover-coordinate" in p or "legacy-writer-evidence" in p
                   or p.endswith("statusline-account-bound-v1/"
                                 "legacy-spool-baseline.json") for p in paths)
    state = _cctally_journal.capture_legacy_spool_state()
    rels = {entry[0] for entry in state.entries}
    assert "statusline-account-bound-v1/transport.last" in rels
    assert "statusline-account-bound-v1/accounts/k/candidates/c.json" in rels
    assert not any("cutover-coordinate" in rel for rel in rels)

    _identity("A")
    _original_cutover_only()
    _cctally_journal.ensure_account_bound_cutover()
    snapshot = {p: p.read_bytes() for p in v1.rglob("*") if p.is_file()}
    # Quiet pre-boundary v1 files never manufacture a trigger.
    assert not sl._rescan_legacy_spool()
    assert not [r for _s, _o, r in _lines()
                if _kind(r) == "legacy_spool_activity"]
    # A v1 diagnostic refresh is not spool activity.
    (v1 / "cutover-coordinate.json").write_text('{"refreshed": true}')
    assert not sl._rescan_legacy_spool()
    # A v1 render after the boundary is trigger (b).
    (v1 / "transport.last").write_text("2")
    assert sl._rescan_legacy_spool()
    activity = [r for _s, _o, r in _lines()
                if _kind(r) == "legacy_spool_activity"]
    assert len(activity) == 1
    assert activity[0]["payload"]["changed"] == [
        "statusline-account-bound-v1/transport.last"]
    snapshot[v1 / "cutover-coordinate.json"] = b'{"refreshed": true}'
    snapshot[v1 / "transport.last"] = b"2"
    # A full current tick never writes, prunes or deletes under v1.
    _entry_statusline(app, None)
    assert {p: p.read_bytes() for p in v1.rglob("*") if p.is_file()} == snapshot
    assert (core.STATUSLINE_ACCOUNT_BOUND_DIR / "accounts").exists()


# ── B5: version-checked administrative shapes ──────────────────────────────


def test_resolution_ack_uses_v3_kind(app):
    import _lib_account_protocol as P
    import _lib_journal as J
    violation = J._protocol_violation("b:x", "marker_conflict", phase="begin",
                                      firstRecordHash="sha256:" + "0" * 64,
                                      conflictingRecordHash="sha256:" + "1" * 64)
    ack = J.make_protocol_resolution(
        at=_iso(NOW), violations=[violation],
        journal_high_water=("observations-2026-10.jsonl", 10),
        journal_prefix_hash="sha256:" + "a" * 64)
    assert J.PROTOCOL_RESOLUTION_V3_KIND == "journal_protocol_resolution_v3"
    assert P.PROTOCOL_RESOLUTION_V3_KIND == J.PROTOCOL_RESOLUTION_V3_KIND
    assert ack["payload"]["kind"] == "journal_protocol_resolution_v3"
    assert ack["wpg"] == "account-bound-v1" and ack["wpr"] == 2
    assert J.PROTOCOL_RESOLUTION_V3_KIND in J.PROTOCOL_RESOLUTION_KINDS
    J._validate_protocol_resolution(ack)
    # A retained revision-1 v2 acknowledgement still validates.
    v2 = _without_revision({**ack, "payload": {
        **ack["payload"], "kind": "journal_protocol_resolution_v2"}})
    v2 = {**v2, "id": J.content_id(
        {k: v for k, v in v2.items() if k not in ("v", "id")})}
    J._validate_protocol_resolution(v2)
    assert P.classify_admission(
        ack, P.Coordinate("x", 0), None).family == "administrative"


def test_calibration_recovery_requires_revision_digest(app):
    import _cctally_quota_model as Q
    regime = {"start": "2026-10-01T00:00:00Z", "rate": 0.0148}
    stamped = Q.stamp_regime(regime)
    assert stamped["wpr"] == 2 and "wprDigest" in stamped
    assert Q.regime_writer_is_current(stamped)

    # The previous release's stamp: `wpg` and its digest only. It verifies
    # under that release's rule, so it alone proves nothing about revision 2.
    revision1 = {**regime, "wpg": "account-bound-v1"}
    revision1["wpgDigest"] = Q._regime_content_digest(revision1)
    assert not Q.regime_writer_is_current(revision1)

    # A previous-release in-place update keeps the v2 keys and recomputes
    # `wpgDigest` the old way; the revision digest no longer verifies.
    edited = {**stamped, "rate": 0.02}
    edited["wpgDigest"] = Q._regime_content_digest(edited)
    assert not Q.regime_writer_is_current(edited)
    # A bool or string revision is not revision 2.
    for value in (True, "2", 3):
        forged = {**stamped, "wpr": value}
        assert not Q.regime_writer_is_current(forged)
    # The previous release still verifies this binary's `wpg` stamp.
    assert stamped["wpgDigest"] == Q._regime_content_digest(stamped)


# ── B6: epoch 1019 ─────────────────────────────────────────────────────────


def _stamp_epoch(path, epoch):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"PRAGMA user_version = {int(epoch)}")
        conn.commit()
    finally:
        conn.close()


def test_epoch_1019_rejects_1018_completion_proofs(app, monkeypatch):
    import _cctally_core as core
    import _cctally_journal
    assert core.STATS_INDEX_EPOCH == 1019
    assert _cctally_journal.DERIVATION_FRONTIER_EPOCH == 1019
    T.current(app, monkeypatch, at="2026-10-08T10:00:00Z", weekly=5,
              week_end=WEEK_END)
    T.rebuild(app)
    assert _cctally_journal.read_trusted_derivation_state(core.DB_PATH) is not None
    _stamp_epoch(core.DB_PATH, 1018)
    assert _cctally_journal.read_trusted_derivation_state(core.DB_PATH) is None


def test_validate_stats_identity_covers_owned_supplied_and_reused_connections(app):
    import _cctally_core as core
    import _cctally_store
    path = core.DB_PATH
    # Reused: a connection validated at the current epoch, then the index is
    # republished at 1018 by another process.
    reused = sqlite3.connect(str(path))
    with _cctally_store.current_stats_snapshot(reused):
        pass
    _stamp_epoch(path, 1018)
    try:
        with pytest.raises(_cctally_store.StatsIndexNotCurrent):
            with _cctally_store.current_stats_snapshot(reused):
                pass
        # Owned: the snapshot opens its own read transaction.
        owned = sqlite3.connect(str(path))
        try:
            with pytest.raises(_cctally_store.StatsIndexNotCurrent):
                with _cctally_store.current_stats_snapshot(owned):
                    pass
            # Supplied: the caller's own transaction is joined.
            owned.execute("BEGIN")
            with pytest.raises(_cctally_store.StatsIndexNotCurrent):
                with _cctally_store.current_stats_snapshot(owned):
                    pass
            with pytest.raises(_cctally_store.StatsIndexNotCurrent):
                _cctally_store.require_current_stats_identity(owned)
            owned.rollback()
        finally:
            owned.close()
    finally:
        reused.close()
    _stamp_epoch(path, 1019)
    fresh = sqlite3.connect(str(path))
    try:
        with _cctally_store.current_stats_snapshot(fresh):
            pass
    finally:
        fresh.close()
    assert os.path.exists(path)
