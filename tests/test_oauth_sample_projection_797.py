"""#797/#823/#840 T14 — the provider-sample projection (spec 4.3 D8, D9;
section 6 T14).

`claude_oauth_samples` (epoch 1019) holds one row per admitted api
observation carrying a valid `oauth_request_started_at`, materialized from
the RAW journal record: independently of the snapshot fold (equal readings
deduplicate there and still each produce a sample), of the five-hour
MAX adjustment, and of credit suppression and retirement. One projection
implementation serves live ingest, ordinary rebuild, plain rederive scratch,
completion scratch and correction rebuilds, so all of them agree row for row
(compared as `collections.Counter` multisets, never by membership).

The read is exact-window: the same account, equal canonical five-hour window
keys or equal weekly resets after nearest-hour normalization, `q` strictly
after the caller's instant, and the response captured before the window's
reset. A reviewed weekly `hold` makes only the weekly sample ineligible and a
later `accept` restores it. An unavailable or uncovered projection is never
"no proof needed".
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sqlite3
import sys
import time

import pytest

import _outcome_quarantine_trace as T
from conftest import load_script, redirect_paths

NOW = 1791417600
WEEK_END = NOW + 3 * 86400
FIVE_END = NOW + 3600
KEY = "oauth_request_started_at"
EARLY = "1970-01-01T00:00:00.000000Z"
COLUMNS = ("account_key, observation_id, requested_at_utc, captured_at_utc, "
           "weekly_percent, weekly_resets_at_utc, five_hour_percent, "
           "five_hour_resets_at_utc, five_hour_window_key")
INDEX_SQL = ("SELECT observation_id, account_key, captured_at_utc"
             " FROM claude_weekly_observation_index")
HISTORY_SQL = ("SELECT decision_op_id, observation_id, disposition"
               " FROM claude_weekly_correction_history")


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _q(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _set_clock(monkeypatch, epoch: float) -> None:
    monkeypatch.setattr(time, "time", lambda: float(epoch))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(epoch))


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _set_clock(monkeypatch, NOW)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    monkeypatch.setattr(cctally, "_bust_statusline_cache", lambda: "skipped")
    monkeypatch.setattr(cctally, "_resolve_oauth_token", lambda: "credential")
    cctally.open_db().close()
    # `db rederive` re-derives from retained session input.
    for name in ("A", "B"):
        T.retain_session_input(cctally, name)
    T.identity("A")
    return cctally


def _key(name):
    import _lib_accounts
    return _lib_accounts.account_key("claude", name)


def _refresh(app, monkeypatch, *, at, weekly, five=None, account="A",
             lead=2, week_end=WEEK_END, five_end=FIVE_END):
    """One forced OAuth refresh whose request starts ``lead`` seconds before
    the response is captured at ``at``."""
    T.identity(account)
    _set_clock(monkeypatch, at - lead)
    response = {"seven_day": {"utilization": weekly,
                              "resets_at": _iso(week_end)}}
    if five is not None:
        response["five_hour"] = {"utilization": five,
                                 "resets_at": _iso(five_end)}

    def fetch(**_kwargs):
        _set_clock(monkeypatch, at)
        return response

    monkeypatch.setattr(app, "_fetch_oauth_usage", fetch)
    result = app._refresh_usage_inproc()
    assert result.status == "ok", result


def _rows(app, sql, args=()):
    conn = app.open_db()
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _samples(app):
    return _rows(app, f"SELECT {COLUMNS} FROM claude_oauth_samples")


def _counter(rows):
    return collections.Counter(rows)


def _read(app, *, account="A", axis, window, after=EARLY):
    import _cctally_oauth_samples as S
    conn = app.open_db()
    try:
        return S.read_samples(conn, account_key=_key(account), axis=axis,
                              window=window, requested_after_utc=after)
    finally:
        conn.close()


def _obs_id(app, captured):
    for record in T.journal_records():
        if (record.get("t") == "obs"
                and (record.get("payload") or {}).get("captured_at")
                == captured):
            return record["id"]
    raise AssertionError(captured)


def _five_key(app, observation_id):
    [(key,)] = _rows(app, "SELECT five_hour_window_key FROM claude_oauth_samples"
                          " WHERE observation_id = ?", (observation_id,))
    return key


# --------------------------------------------------------------------------
# the projection is raw and independent of the fold
# --------------------------------------------------------------------------

def test_equal_readings_each_project_a_sample(app, monkeypatch):
    for i in range(3):
        _refresh(app, monkeypatch, at=NOW + 60 * i, weekly=40, five=30)
    snapshots = _rows(app, "SELECT weekly_percent FROM weekly_usage_snapshots")
    assert len(snapshots) == 1, "the fold deduplicates the equal readings"
    rows = _samples(app)
    assert len(rows) == 3, rows
    assert sorted(r[2] for r in rows) == [_q(NOW + 60 * i - 2)
                                          for i in range(3)]
    assert {r[4] for r in rows} == {40.0} and {r[6] for r in rows} == {30.0}
    assert len({r[1] for r in rows}) == 3


def test_clamped_five_hour_value_never_substitutes(app, monkeypatch):
    _refresh(app, monkeypatch, at=NOW, weekly=40, five=60)
    _refresh(app, monkeypatch, at=NOW + 60, weekly=39, five=55)
    stored = _rows(app, "SELECT MAX(weekly_percent), MAX(five_hour_percent)"
                        " FROM weekly_usage_snapshots")
    assert stored == [(40.0, 60.0)]
    second = _obs_id(app, _iso(NOW + 60))
    [row] = _rows(app, "SELECT weekly_percent, five_hour_percent"
                       " FROM claude_oauth_samples WHERE observation_id = ?",
                  (second,))
    assert row == (39.0, 55.0), "the raw readings, never the clamped ones"


def test_suppression_and_retirement_leave_samples(app, monkeypatch):
    trace = ((30, 70), (30, 20), (30, 20), (31, 21))
    for i, (weekly, five) in enumerate(trace):
        _refresh(app, monkeypatch, at=NOW + 600 * i, weekly=weekly, five=five)
    # Non-vacuity: a five-hour credit was minted (its pivot retires the
    # window's stale replicas), and the stored rows are not the raw readings.
    assert _rows(app, "SELECT COUNT(*) FROM five_hour_reset_events") == [(1,)]
    before = _counter(_samples(app))
    assert sorted(r[6] for r in before) == [20.0, 20.0, 21.0, 70.0]
    stored = sorted(r[0] for r in _rows(
        app, "SELECT five_hour_percent FROM weekly_usage_snapshots"
             " WHERE five_hour_percent IS NOT NULL"))
    assert stored != [20.0, 20.0, 21.0, 70.0], stored
    # A manual weekly credit and its stale-replica cleanup.
    T.set_clock(monkeypatch, _iso(NOW + 2400))
    T.identity("A")
    args = app.build_parser().parse_args(
        ["record-credit", "--to", "5", "--yes"])
    assert args.func(args) == 0
    assert _rows(app, "SELECT COUNT(*) FROM weekly_credit_floors") == [(1,)]
    assert _counter(_samples(app)) == before
    T.rebuild(app)
    assert _counter(_samples(app)) == before


def test_account_isolation(app, monkeypatch):
    _refresh(app, monkeypatch, at=NOW, weekly=50, five=40, account="A")
    _refresh(app, monkeypatch, at=NOW + 60, weekly=10, five=5, account="B")
    for account, weekly, five in (("A", 50.0, 40.0), ("B", 10.0, 5.0)):
        weekly_rows = _read(app, account=account, axis="weekly",
                            window=WEEK_END)
        assert [s.weekly_percent for s in weekly_rows] == [weekly]
        assert {s.account_key for s in weekly_rows} == {_key(account)}
        key = weekly_rows[0].five_hour_window_key
        five_rows = _read(app, account=account, axis="five_hour", window=key)
        assert [s.five_hour_percent for s in five_rows] == [five]


# --------------------------------------------------------------------------
# reviewed weekly decisions
# --------------------------------------------------------------------------

def _reviewed(app, tmp_path, capsys, decisions, *, name):
    """Apply one reviewed weekly decision manifest through `db rederive`
    (preview, pin, apply), exactly as an operator does."""
    import _cctally_journal as jr
    high_water = jr.journal_high_water()
    manifest = {
        "schemaVersion": 2,
        "journalHighWater": {"segment": high_water[0],
                             "offset": high_water[1]},
        "journalPrefixHash": jr.journal_prefix_hash(high_water),
        "reviewedAt": _iso(time.time()),
        "reason": "T14 reviewed weekly decision",
        "weeklyAxisDecisions": [
            {"observationId": obs, "disposition": disposition}
            for obs, disposition in decisions],
        "snapshotIdentityDecisions": [],
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(manifest))
    parser = app.build_parser()
    capsys.readouterr()
    base = ["db", "rederive", "--family", "claude-usage",
            "--reviewed-weekly-decisions", str(path), "--json"]
    assert app.cmd_db_rederive(parser.parse_args(base)) == 0
    preview = json.loads(capsys.readouterr().out)
    manifest.update(expectedBaselinePlanHash=preview["baselinePlanHash"],
                    expectedDecisionPlanHash=preview["decisionPlanHash"])
    path.write_text(json.dumps(manifest))
    rc = app.cmd_db_rederive(parser.parse_args(base + ["--yes"]))
    out = capsys.readouterr().out
    assert rc == 0, out


def test_weekly_hold_excludes_only_the_weekly_sample_and_accept_restores(
        app, monkeypatch, tmp_path, capsys):
    for i, (weekly, five) in enumerate(((30, 10), (45, 11), (46, 12))):
        _refresh(app, monkeypatch, at=NOW + 600 * i, weekly=weekly, five=five)
    held = _obs_id(app, _iso(NOW + 600))
    key = _five_key(app, held)

    def ids(axis, window):
        return {s.observation_id for s in _read(app, axis=axis,
                                                window=window)}

    assert held in ids("weekly", WEEK_END)
    _set_clock(monkeypatch, NOW + 2000)
    _reviewed(app, tmp_path, capsys, [(held, "hold")], name="hold")
    assert held not in ids("weekly", WEEK_END)
    assert len(ids("weekly", WEEK_END)) == 2
    assert held in ids("five_hour", key), "the five-hour sample stays"
    assert len(_samples(app)) == 3, "the row itself is retained"
    _set_clock(monkeypatch, NOW + 2100)
    _reviewed(app, tmp_path, capsys, [(held, "accept")], name="accept")
    assert held in ids("weekly", WEEK_END)
    T.rebuild(app)
    assert held in ids("weekly", WEEK_END) and held in ids("five_hour", key)


# --------------------------------------------------------------------------
# every derivation path agrees, row for row
# --------------------------------------------------------------------------

def test_live_rebuild_rederive_completion_and_correction_rebuild_agree_row_for_row(
        app, monkeypatch, tmp_path, capsys):
    import _cctally_core as core
    import _cctally_rederive
    trace = ((30, 70), (30, 70), (30, 20), (30, 20), (31, 21))
    for i, (weekly, five) in enumerate(trace):
        _refresh(app, monkeypatch, at=NOW + 600 * i, weekly=weekly, five=five)
        _refresh(app, monkeypatch, at=NOW + 600 * i + 30, weekly=weekly + 5,
                 five=five, account="B")
    live = _counter(_samples(app))
    assert sum(live.values()) == 10 and len(live) == 10

    # #1015 spec 5.1: the observation index, one row per admitted reading.
    live_index = _counter(_rows(app, INDEX_SQL))
    assert sum(live_index.values()) == 10 and len(live_index) == 10
    assert {r[1] for r in live_index} == {_key("A"), _key("B")}

    scratch: list = []
    original = _cctally_rederive.replay_scratch_records

    def capturing(conn, records, *args, **kwargs):
        events = original(conn, records, *args, **kwargs)
        scratch.append({
            "samples": _counter(tuple(r) for r in conn.execute(
                f"SELECT {COLUMNS} FROM claude_oauth_samples")),
            "index": _counter(tuple(r) for r in conn.execute(INDEX_SQL)),
            "history": _counter(tuple(r) for r in conn.execute(HISTORY_SQL)),
            "ops": conn.execute(
                "SELECT COUNT(*) FROM claude_weekly_correction_ops"
            ).fetchone()[0],
        })
        return events

    monkeypatch.setattr(_cctally_rederive, "replay_scratch_records", capturing)

    def published():
        return {"samples": _counter(_samples(app)),
                "index": _counter(_rows(app, INDEX_SQL)),
                "history": _counter(_rows(app, HISTORY_SQL))}

    def scratch_agrees(expected, label):
        assert scratch, f"{label} replayed no scratch"
        for found in scratch:
            assert found["samples"] == expected["samples"], label
            assert found["index"] == expected["index"], label
            # A counterfactual baseline replay (the journal without a retained
            # reviewed op, for the baseline plan) holds a subset; the replay
            # of the journal itself holds exactly the published history.
            assert not found["history"] - expected["history"], label
            assert found["ops"] == 0, f"{label} wrote a journal coordinate"
        assert any(found["history"] == expected["history"]
                   for found in scratch), (
            label, [found["history"] for found in scratch])

    def drop_index():
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = core.DB_PATH.with_name(core.DB_PATH.name + suffix)
            if path.exists():
                path.unlink()

    expected = {"samples": live, "index": live_index,
                "history": collections.Counter()}
    assert published() == expected
    T.rebuild(app)
    assert published() == expected, "ordinary rebuild"

    scratch.clear()
    T.rederive(app)
    scratch_agrees(expected, "rederive scratch")
    assert published() == expected, "after plain rederive"

    # Completion: with no trusted predecessor the completing rebuild replays
    # every admitted input after the cutover through its scratch.
    scratch.clear()
    drop_index()
    T.rebuild(app)
    scratch_agrees(expected, "completion scratch")
    assert published() == expected, "after the completing rebuild"

    # A correction rebuild: a reviewed hold journals a correction and
    # republishes; the samples and the index are untouched by it, and the
    # history gains the hold on every path.
    held = _obs_id(app, _iso(NOW + 600))
    _set_clock(monkeypatch, NOW + 4000)
    _reviewed(app, tmp_path, capsys, [(held, "hold")], name="hold")
    [op] = [r["id"] for r in T.journal_records() if r.get("t") == "op"
            and (r.get("payload") or {}).get("kind", "").startswith(
                "claude_weekly_observation_decision")]
    expected["history"] = collections.Counter({(op, held, "hold"): 1})
    assert published() == expected, "correction rebuild"
    ops = _rows(app, "SELECT decision_op_id FROM claude_weekly_correction_ops")
    assert ops == [(op,)]
    T.rebuild(app)
    assert published() == expected, "ordinary rebuild after the hold"
    scratch.clear()
    T.rederive(app)
    scratch_agrees(expected, "rederive scratch after the hold")
    scratch.clear()
    drop_index()
    T.rebuild(app)
    scratch_agrees(expected, "completion scratch after the hold")
    assert published() == expected, "completing rebuild after the hold"
    assert _rows(app, "SELECT decision_op_id FROM claude_weekly_correction_ops"
                 ) == ops


# --------------------------------------------------------------------------
# window identity and proof bounds
# --------------------------------------------------------------------------

def test_window_identity_by_canonical_key_and_normalized_reset(app,
                                                                monkeypatch):
    # Weekly resets 2 minutes early and 10 minutes late normalize to the same
    # hour; one an hour later is a different window.
    _refresh(app, monkeypatch, at=NOW, weekly=20, five=10,
             week_end=WEEK_END - 120, five_end=FIVE_END)
    _refresh(app, monkeypatch, at=NOW + 60, weekly=21, five=11,
             week_end=WEEK_END + 600, five_end=FIVE_END + 7)
    _refresh(app, monkeypatch, at=NOW + 120, weekly=22, five=12,
             week_end=WEEK_END + 3600, five_end=FIVE_END + 2 * 3600)
    by_epoch = _read(app, axis="weekly", window=WEEK_END)
    by_text = _read(app, axis="weekly", window=_iso(WEEK_END))
    assert [s.weekly_percent for s in by_epoch] == [20.0, 21.0]
    assert by_text == by_epoch
    assert {s.weekly_resets_at_utc for s in by_epoch} == {_iso(WEEK_END)}
    assert [s.weekly_percent for s in _read(
        app, axis="weekly", window=WEEK_END + 3600)] == [22.0]
    # The five-hour key is the authoritative one the snapshot carries; the
    # 7-second jitter keeps the window, the next window is distinct.
    snapshot_keys = {r[0] for r in _rows(
        app, "SELECT five_hour_window_key FROM weekly_usage_snapshots")}
    first = _read(app, axis="five_hour", window=by_epoch[0].five_hour_window_key)
    assert [s.five_hour_percent for s in first] == [10.0, 11.0]
    assert {s.five_hour_window_key for s in first} <= snapshot_keys
    assert _read(app, axis="five_hour",
                 window=by_epoch[0].five_hour_window_key + 600) == ()
    # `q` must be STRICTLY after the caller's instant.
    assert [s.weekly_percent for s in _read(
        app, axis="weekly", window=WEEK_END, after=_q(NOW - 2))] == [21.0]


def _append_api(app, monkeypatch, *, at, weekly, resets_at, q, five=None,
                five_resets=None, planned=None, account="A",
                binding_account=None):
    """An api observation appended exactly as the recording kernel shapes
    one (stamped, bound), with chosen values, then ingested."""
    import _cctally_journal as jr
    import _cctally_statusline as sl
    import _lib_journal as J
    _set_clock(monkeypatch, at)
    T.identity(binding_account or account)
    binding = sl._capture_binding()
    T.identity(account)
    raw = {"weekly_percent": float(weekly), "resets_at": int(resets_at),
           "source": "api", "captured_at": _iso(at),
           "account_binding": binding.document(), "fold_policy": 1}
    if q is not None:
        raw[KEY] = q
    if five is not None:
        raw["five_hour_percent"] = float(five)
        raw["five_hour_resets_at"] = dt.datetime.fromtimestamp(
            five_resets, dt.timezone.utc).isoformat(timespec="seconds")
        if planned is not None:
            raw["five_hour_window_key"] = planned
    record = J.prepare_record(J.make_obs(
        at=_iso(at), src="record-usage", provider="claude", payload=raw,
        account=_key(account)))
    jr.append_record(record)
    T.ingest(app)
    return record


def test_no_proof_from_sample_completed_after_advertised_reset_or_conflicting_identity(
        app, monkeypatch):
    early = NOW - 86400   # an earlier week, already reset at capture
    # Requested before the weekly reset, captured after it.
    late = _append_api(app, monkeypatch, at=early + 1, weekly=5,
                       resets_at=early, q=_q(early - 1))
    # Captured after the five-hour reset of its own window.
    five_late = _append_api(app, monkeypatch, at=FIVE_END + 60, weekly=6,
                            resets_at=WEEK_END, q=_q(FIVE_END + 50),
                            five=3, five_resets=FIVE_END,
                            planned=FIVE_END)
    # A planned key that contradicts its own raw reset: conflicting identity.
    conflicting = _append_api(app, monkeypatch, at=NOW + 10, weekly=7,
                              resets_at=WEEK_END, q=_q(NOW + 9), five=4,
                              five_resets=FIVE_END,
                              planned=FIVE_END + 4 * 3600)
    # A binding naming another account than the record's.
    misbound = _append_api(app, monkeypatch, at=NOW + 20, weekly=8,
                           resets_at=WEEK_END, q=_q(NOW + 19),
                           binding_account="B")
    rows = {r[1]: r for r in _samples(app)}
    assert late["id"] in rows and five_late["id"] in rows
    assert misbound["id"] not in rows, "no sample without the account's binding"
    assert rows[conflicting["id"]][8] is None, "conflicting window identity"
    assert _read(app, axis="weekly", window=early) == ()
    assert five_late["id"] not in {
        s.observation_id for s in _read(app, axis="five_hour",
                                        window=FIVE_END)}
    assert five_late["id"] in {
        s.observation_id for s in _read(app, axis="weekly", window=WEEK_END)}
    for window in (FIVE_END, FIVE_END + 4 * 3600):
        assert conflicting["id"] not in {
            s.observation_id for s in _read(app, axis="five_hour",
                                            window=window)}


def test_requested_after_is_bounded_by_q_not_the_capture_instant(
        app, monkeypatch):
    """Mutation control (spec 6 T5/T14): with `q` 30 s before the capture, a
    caller's instant between them must exclude the sample. A projection that
    used the capture instant as `q` would include it, and this guard fails."""
    import _lib_oauth_samples as P

    def guard():
        found = _read(app, axis="weekly", window=WEEK_END, after=_q(NOW - 10))
        assert found == (), found

    _refresh(app, monkeypatch, at=NOW, weekly=50, five=10, lead=30)
    guard()
    original = P.sample_from_observation

    def capture_as_q(payload, **kwargs):
        sample = original(payload, **kwargs)
        if sample is None:
            return None
        import dataclasses
        return dataclasses.replace(
            sample, requested_at_utc=P.canonical_instant(
                sample.captured_at_utc))

    monkeypatch.setattr(P, "sample_from_observation", capture_as_q)
    T.rebuild(app)
    with pytest.raises(AssertionError):
        guard()


def test_uncovered_projection_reports_unavailable(app, tmp_path):
    import _cctally_core as core
    import _cctally_oauth_samples as S
    conn = app.open_db()
    try:
        assert S.projection_covered(conn, account_key=_key("A"))
    finally:
        conn.close()
    bare = sqlite3.connect(":memory:")
    try:
        assert not S.projection_covered(bare, account_key=_key("A"))
        with pytest.raises(S.ProjectionUnavailable):
            S.read_samples(bare, account_key=_key("A"), axis="weekly",
                           window=WEEK_END, requested_after_utc=EARLY)
    finally:
        bare.close()
    copy = tmp_path / "older.db"
    source = sqlite3.connect(str(core.DB_PATH))
    target = sqlite3.connect(str(copy))
    try:
        source.backup(target)
        target.execute(f"PRAGMA user_version = {core.STATS_INDEX_EPOCH - 1}")
        assert not S.projection_covered(target, account_key=_key("A"))
        target.execute(f"PRAGMA user_version = {core.STATS_INDEX_EPOCH}")
        target.execute("DROP TABLE claude_oauth_samples")
        assert not S.projection_covered(target, account_key=_key("A"))
        with pytest.raises(S.ProjectionUnavailable):
            S.read_samples(target, account_key=_key("A"), axis="five_hour",
                           window=FIVE_END, requested_after_utc=EARLY)
    finally:
        source.close()
        target.close()


# --------------------------------------------------------------------------
# the pure kernel
# --------------------------------------------------------------------------

def test_weekly_normalization_matches_the_week_boundary_code():
    import _cctally_core as core
    import _lib_oauth_samples as P
    for offset in range(-3600, 3600, 37):
        epoch = WEEK_END + offset
        expected = core._normalize_week_boundary_dt(
            dt.datetime.fromtimestamp(epoch, dt.timezone.utc))
        assert P.normalize_weekly_reset(epoch) == expected.strftime(
            "%Y-%m-%dT%H:%M:%SZ")


def test_sample_from_observation_rejects_what_carries_no_valid_key():
    import _lib_oauth_samples as P
    binding = {"accountKey": "k", "status": "ok"}
    base = {"weekly_percent": 10.0, "resets_at": WEEK_END, "source": "api",
            "captured_at": _iso(NOW), "account_binding": binding,
            KEY: _q(NOW - 1)}

    def sample(**changes):
        payload = dict(base, **changes)
        for name, value in list(payload.items()):
            if value is None:
                payload.pop(name)
        return P.sample_from_observation(
            payload, account_key="k", observation_id="o:1",
            captured_at_utc=payload.get("captured_at"))

    assert sample() is not None
    assert sample().requested_at_utc == _q(NOW - 1)
    assert sample(source="statusline") is None
    assert sample(**{KEY: None}) is None
    for bad in ("yesterday", 12, True, "2026-10-08T00:00:00", ""):
        assert sample(**{KEY: bad}) is None, bad
    assert sample(account_binding=None) is None
    assert sample(account_binding={"accountKey": "other"}) is None
    # A request instant after the capture is no lower bound at all.
    assert sample(**{KEY: _q(NOW + 5)}) is None


# --------------------------------------------------------------------------
# #1015 spec 5.1: the observation index's attribution
# --------------------------------------------------------------------------

_INDEX_RESETS = int(dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc).timestamp())


def _legacy_reading(J, at, **extra):
    """A pre-boundary Claude reading the previous writers produced."""
    payload = {"weekly_percent": 1.0, "source": "statusline",
               "resets_at": _INDEX_RESETS, "captured_at": at}
    payload.update(extra.pop("payload", {}))
    return J.make_obs(at=at, src="record-usage", provider="claude",
                      payload=payload, writer_generation=None, **extra)


def test_observation_index_attribution_is_live_ingests_on_every_path(
        monkeypatch, tmp_path, capsys):
    """#1015 (R2-P1-1, R3-P2-1), detects a correction attributed to the wrong
    account because the index read an account live ingest does not: an
    explicit stamp keeps it (an explicit `unattributed` included), an
    account-aware stampless reading (current generation, or a validated
    `unattributed` binding) is `unattributed`, and only an unstamped legacy
    reading takes the cutover op's account — before or after the op, and
    when a pinned rebuild finds the op only in the unvisited suffix. The
    rebuild leaves no staging table behind."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core as core
    import _cctally_journal as jr
    import _cctally_store
    import _lib_account_binding
    import _lib_accounts
    import _lib_journal as J
    account_a = _lib_accounts.account_key("claude", "index-a")
    account_b = _lib_accounts.account_key("claude", "index-b")
    ns["open_db"]().close()
    binding = _lib_account_binding.capture({
        "status": _lib_account_binding.STATUS_STABLY_ABSENT,
        "account_key": _lib_accounts.UNATTRIBUTED, "natural_id": None})
    before = _legacy_reading(J, "2026-01-04T09:00:00Z")
    cutover = J.make_op(
        at="2026-01-05T00:00:00Z", src="accounts-cutover",
        payload={"kind": "accounts_cutover",
                 "claude_legacy_account": account_a},
        writer_generation=None)
    cutover["id"] = jr.CUTOVER_OP_ID
    after = _legacy_reading(J, "2026-01-06T09:00:00Z")
    stamped = _legacy_reading(J, "2026-01-06T10:00:00Z", account=account_b)
    explicit = _legacy_reading(J, "2026-01-06T11:00:00Z",
                               account=_lib_accounts.UNATTRIBUTED)
    bound = _legacy_reading(J, "2026-01-06T12:00:00Z", payload={
        "account_binding": binding.document()})
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (core.JOURNAL_DIR / "observations-2026-01.jsonl").write_bytes(b"".join(
        J.encode_line(r) for r in (before, cutover, after, stamped, explicit,
                                   bound)))
    current = J.prepare_record(J.make_obs(
        at="2026-01-12T09:00:00Z", src="record-usage", provider="claude",
        account=None, payload={
            "weekly_percent": 2.0, "source": "statusline",
            "resets_at": _INDEX_RESETS, "captured_at": "2026-01-12T09:00:00Z",
            "account_binding": binding.document()}))
    assert "account" not in current
    jr.append_record(current)
    jr.run_stats_ingest(mode="authoritative")

    def index(path=None):
        conn = sqlite3.connect(str(path or core.DB_PATH))
        try:
            rows = {r[0]: (r[1], r[2]) for r in conn.execute(INDEX_SQL)}
            staging = conn.execute(
                "SELECT name FROM sqlite_master WHERE name ="
                " 'rebuild_weekly_observation_unstamped'").fetchall()
        finally:
            conn.close()
        assert staging == [], "the staging table reached a published index"
        return rows

    unattributed = _lib_accounts.UNATTRIBUTED
    live = index()
    assert {k: v[0] for k, v in live.items()} == {
        before["id"]: account_a, after["id"]: account_a,
        stamped["id"]: account_b, explicit["id"]: unattributed,
        bound["id"]: unattributed, current["id"]: unattributed}
    assert live[before["id"]][1] == "2026-01-04T09:00:00.000000Z"
    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True)) == 0
    capsys.readouterr()
    assert index() == live, "rebuild"

    # A rebuild pinned before the op finds it only in the unvisited suffix.
    lines = list(jr.iter_range(None, jr.journal_high_water()))
    pinned = (lines[0][0], lines[0][1] + len(lines[0][2]) + 1)
    dest = tmp_path / "pinned.db"
    core.open_db(_target_path=str(dest)).close()
    with _cctally_store.stats_write_scope("maintenance-rebuild"):
        jr.rebuild_stats_index(
            context=jr.RebuildContext(trigger="test-fixture"),
            target_path=str(dest), high_water=pinned,
            update_quota_cache=False)
    assert index(dest) == {before["id"]: live[before["id"]]}
