"""#797 T16 — derived consumers across a `retract` correction (spec section 6
T16, section 8, R19; review W2, X1, Y1).

The store is T7's fake-reset storm (`tests/test_weekly_retract_797.py`),
journalled through the real bound recording kernel, plus retained priced
session entries for the account inside the subscription week. The replays
A1, A2 and A3 are retracted through the real `db rederive` command (preview,
then apply with the previewed plan hashes), and the stats index is rebuilt.

What moves with the correction:

* `account show` counts its snapshots and percent milestones from the
  corrected index (`bin/_cctally_account.py`, `_cmd_account_show`);
* the dashboard snapshot-cache reset signature (`bin/_lib_snapshot_cache.py`,
  `_reset_sig`) changes when the fake resets are tombstoned and stays equal
  across a run that changes nothing;
* the quota-model inputs (`_read_stats_component`) carry only the genuine
  reset and no stale replay reading.

What does not (R19): the forecast applies the PERSISTED open calibration
regime to the week's entries and never refits from corrected snapshots, so
the calibration is pinned at three checkpoints with literal, distinguishable
coefficients over a nonzero population:

1. after the correction and the rebuild the sidecar's bytes are unchanged and
   the forecast still applies the retained coefficient;
2. (the control, run before 3) a withheld sweep takes the reducer's
   non-writing branch, so the sidecar and the forecast stay as they were;
3. a trustworthy sweep updates the open regime in place (same
   `effectiveFrom`), the forecast applies the new coefficient, and the closed
   regime and the journalled metering-rate transition are retained.

The sweeps' analyses are constructed values, as in
`tests/test_quota_persistence.py`: the fit kernel is unchanged by this work,
and what is pinned is the persistence and consumption of whatever it fits.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json

import pytest

import _outcome_quarantine_trace as T
import test_weekly_retract_797 as W

UTC = dt.timezone.utc
WEEK_START = dt.datetime(2026, 10, 17, 5, 0, tzinfo=UTC)
WEEK_END = dt.datetime(2026, 10, 24, 5, 0, tzinfo=UTC)
#: Six of the week's seven days have elapsed at the forecast clock.
FORECAST_NOW = dt.datetime(2026, 10, 23, 5, 0, tzinfo=UTC)
SWEEP_NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
ERA_START = dt.datetime(2026, 9, 22, tzinfo=UTC)
SPLIT = dt.date(2026, 10, 10)

#: Literal coefficients (weighted units per meter point), all distinct.
X_CLOSED = 50_000.0
X_OLD = 100_000.0
X_NEW = 400_000.0

#: The week's priced population: two requests, fresh input only, so the
#: weighted units are exactly 560,000 (fresh weighs 1.0 in every era).
ENTRIES = (("2026-10-19T10:00:00Z", 360_000), ("2026-10-21T10:00:00Z", 200_000))
UNITS = 560_000.0


# --- the store -----------------------------------------------------------------


def _seed_entries(app, account_key):
    cache = app.open_cache_db()
    try:
        source = "/retained/A/week.jsonl"
        cache.execute(
            "INSERT OR IGNORE INTO session_files (path, size_bytes, mtime_ns, "
            "last_byte_offset, last_ingested_at, session_id, project_path) "
            "VALUES (?,?,?,?,?,?,?)",
            (source, 100, 1, 100, "2026-10-21T11:00:00Z", "A-week", "/retained/A"))
        for offset, (at, fresh) in enumerate(ENTRIES):
            cache.execute(
                "INSERT INTO session_entries (source_path, line_offset, "
                "timestamp_utc, model, input_tokens, output_tokens, "
                "cache_create_tokens, cache_read_tokens, cache_create_1h_tokens, "
                "account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source, offset, at, "claude-opus-5-5", fresh, 0, 0, 0, 0,
                 account_key))
        cache.commit()
    finally:
        cache.close()


def _store(monkeypatch, tmp_path):
    app = W._app(monkeypatch, tmp_path)
    account = T.identity("A")
    _seed_entries(app, account)
    ids = W._storm(app, monkeypatch)
    return app, account, ids


def _retract(app, capsys, tmp_path, ids):
    path = W._manifest(tmp_path, [(ids[name], "retract")
                                  for name in ("A1", "A2", "A3")])
    W._apply(app, capsys, path)
    T.rebuild(app)


def _account_show(app, capsys) -> dict:
    capsys.readouterr()
    args = app.build_parser().parse_args(
        ["account", "show", "A@example.test", "--json"])
    assert args.func(args) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])["attribution"]


def _reset_sig(app):
    import _lib_snapshot_cache
    conn = app.open_db()
    try:
        return _lib_snapshot_cache._reset_sig(conn)
    finally:
        conn.close()


def _quota_inputs(app, account):
    import _cctally_quota_model as qmg
    conn = app.open_db()
    try:
        snapshots, credits, _diag = qmg._read_stats_component(conn, account, None)
    finally:
        conn.close()
    return ([(s.at.isoformat(), s.percent) for s in snapshots],
            sorted((c.kind, c.at.isoformat()) for c in credits))


# --- calibration -----------------------------------------------------------------


def _composition():
    import _lib_quota_model as qm
    era = qm.era_by_id("opus-5-5")
    records = [qm.EntryRecord(at=dt.datetime.fromisoformat(at.replace("Z", "+00:00")),
                              model="claude-opus-5-5", fresh=fresh, output=0,
                              cache_create_total=0, cache_1h=0, cache_read=0)
               for at, fresh in ENTRIES]
    return qm.aggregate_composition(records, era=era)


def _evidence(qm, value):
    return qm.evidence_available(
        value, qm.Interval(value * 0.95, value * 1.05), qm.Support(12, 2),
        {"days": 12, "segments": 2}, ())


def _analysis(*, fitted=None, baseline=None, watch=None, confirmed=False,
              status=None):
    import _cctally_quota_model as qmg
    qm = qmg.qm
    withheld = qm.evidence_withheld("insufficient-history", {"days": 0, "segments": 0})
    family, klass = _composition()
    detector = qm.DetectorResult(
        split_date=SPLIT if confirmed else None,
        raw_p=0.0004 if confirmed else None,
        holm_p=0.006 if confirmed else None, holm_family_size=13,
        baseline_days=18 if confirmed else 0, watch_days=13 if confirmed else 0,
        longest_run=3 if confirmed else 0, qualified=confirmed,
        input_eligible_days=31, scanned_eligible_days=31,
        truncated_eligible_days=0, scan_start_date=ERA_START.date(),
        max_auto_scan_days=64, history_truncated=False)
    fitted = withheld if fitted is None else fitted
    return qm.QuotaAnalysis(
        status=qm.CalibrationStatus.OK if status is None else status,
        verdict=(qm.Verdict.RATE_CHANGE_DETECTED if confirmed
                 else qm.Verdict.NO_RATE_CHANGE),
        exit_code=0, fitted=fitted, consumption=fitted, projection=fitted,
        headroom=fitted,
        baseline_fit=withheld if baseline is None else baseline,
        watch_fit=withheld if watch is None else watch,
        observed_percent=3, family_shares=dict(family), class_shares=dict(klass),
        current_family_shares={}, current_class_shares={},
        family_radius=0.02, class_radius=0.0105, detector=detector, blocking=(),
        diagnostics={}, detector_input_causes=frozenset(),
        composition_provenance=frozenset())


def _mode(account):
    import _cctally_quota_model as qmg
    return qmg.PersistMode(
        kind="automatic", account_key=account, now=SWEEP_NOW,
        fingerprint=qmg.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
        regime_start=ERA_START, coefficient_era="opus-5-5")


def _sweep(account, analysis):
    """One sweep's persistence, as `cmd_quota` runs it: persist under the
    calibration lock, then journal each fresh transition."""
    import _cctally_quota_model as qmg
    quarantined, transitions, _candidates = qmg.persist_and_detect(
        analysis, _mode(account))
    assert quarantined is None
    for transition in transitions:
        assert qmg.record_rate_change_transition(transition, now=SWEEP_NOW) is not None
    return transitions


def _sidecar() -> bytes:
    import _cctally_quota_model as qmg
    return qmg.calibration_path().read_bytes()


def _regimes(account) -> list:
    import _cctally_quota_model as qmg
    state = json.loads(_sidecar())
    return [(r["effectiveFrom"], r["effectiveUntil"], r["unitsPerPoint"])
            for r in qmg.stored_regimes(state, account)]


def _forecast(account):
    import _cctally_forecast
    detail = _cctally_forecast._calibrated_week_detail(
        FORECAST_NOW, WEEK_START, WEEK_END, account_key=account)
    assert detail.code is None, detail
    return detail.consumption_pct, detail.projection_pct, detail.headroom_pct


def _expected_forecast(x):
    consumed = UNITS / x
    return pytest.approx((consumed, consumed * 7 / 6, 100.0 - consumed))


def _rate_change_events(app):
    return W._stats(app, "SELECT provider, account_key, effective_from, "
                         "previous_units_per_point, new_units_per_point "
                         "FROM meter_rate_change_events ORDER BY effective_from")


def _rate_change_journal():
    import _lib_meter_rate_change as mrc
    return sorted(r["id"] for r in T.journal_records()
                  if r.get("t") == "evt"
                  and (r.get("payload") or {}).get("kind") == mrc.EVT_KIND)


# --- the tests -----------------------------------------------------------------


def test_account_show_reset_signature_and_quota_inputs_follow_the_correction(
        monkeypatch, tmp_path, capsys):
    app, account, ids = _store(monkeypatch, tmp_path)
    T.rebuild(app)
    shown_before = _account_show(app, capsys)
    sig_before = _reset_sig(app)
    snapshots_before, credits_before = _quota_inputs(app, account)
    resets_before = W._stats(
        app, "SELECT origin_observation_id, effective_reset_at_utc "
             "FROM week_reset_events WHERE account_key = ? ORDER BY 2", (account,))
    assert [origin for origin, _at in resets_before] == [
        ids[n] for n in ("L0", "L1", "L2", "L3")]

    _retract(app, capsys, tmp_path, ids)

    shown_after = _account_show(app, capsys)
    sig_after = _reset_sig(app)
    snapshots_after, credits_after = _quota_inputs(app, account)
    [(genuine, genuine_at)] = W._stats(
        app, "SELECT origin_observation_id, effective_reset_at_utc "
             "FROM week_reset_events WHERE account_key = ?", (account,))
    assert genuine == ids["L0"]

    # `account show`: held-inclusive snapshot inventory and the milestones.
    # Before: ten unheld snapshots and nine milestones (the genuine 70-74 and
    # the replays' milestones in the fake epochs). After: A1 and A3 store no
    # snapshot (no five-hour rise), A2 is a held row, and the genuine 70-74
    # plus R0's 1, 2 and 3 remain.
    dump = repr((shown_before, shown_after, sig_before, sig_after,
                 snapshots_before, credits_before, snapshots_after, credits_after))
    assert shown_before == {"usageSnapshots": 10, "percentMilestones": 9}, dump
    assert shown_after == {"usageSnapshots": 8, "percentMilestones": 8}, dump

    # The reset signature moves with the tombstoned resets ...
    assert sig_before[0] == 4 and sig_after[0] == 1
    assert sig_after != sig_before
    # ... and stays equal across a run that changes nothing.
    rc, payload = W._rederive(app, capsys, yes=True)
    assert rc == 0 and W._no_actions(payload), payload
    T.rebuild(app)
    assert _reset_sig(app) == sig_after

    # The quota-model inputs: only the genuine reset is a credit, and no
    # stale 74 is a meter reading after it.
    assert [kind for kind, _at in credits_before] == ["reset"] * 4
    assert credits_after == [("reset", genuine_at.replace("Z", "+00:00"))]
    assert [pct for _at, pct in snapshots_before].count(74.0) == 4
    assert [pct for _at, pct in snapshots_after] == [
        70.0, 74.0, 0.0, 0.0, 1.0, 1.0, 3.0], dump
    assert sorted(snapshots_after) == sorted(
        s for s in snapshots_before if s[0] not in {
            "2026-10-22T09:32:00+00:00", "2026-10-22T09:34:00+00:00",
            "2026-10-22T09:36:00+00:00"})


def test_calibration_checkpoints_across_the_correction(
        monkeypatch, tmp_path, capsys):
    import _cctally_quota_model as qmg
    app, account, ids = _store(monkeypatch, tmp_path)
    qm = qmg.qm

    # The retained history: a confirmed change at SPLIT, with no prior state,
    # persists both sides (closed X_CLOSED, open X_OLD) and journals one
    # metering-rate transition.
    transitions = _sweep(account, _analysis(
        fitted=_evidence(qm, X_OLD), baseline=_evidence(qm, X_CLOSED),
        watch=_evidence(qm, X_OLD), confirmed=True,
        status=qm.CalibrationStatus.OK))
    assert len(transitions) == 1
    closed = (ERA_START.isoformat(), "2026-10-10T00:00:00+00:00", X_CLOSED)
    open_old = ("2026-10-10T00:00:00+00:00", None, X_OLD)
    assert _regimes(account) == [closed, open_old]
    events = _rate_change_events(app)
    assert events == [("claude", account, "2026-10-10T00:00:00+00:00",
                       X_CLOSED, X_OLD)]
    journalled = _rate_change_journal()
    assert len(journalled) == 1
    assert _forecast(account) == _expected_forecast(X_OLD)
    sidecar = _sidecar()

    # Checkpoint 1: the correction and the rebuild leave the sidecar's bytes
    # unchanged, and the forecast still applies X_OLD (5.6 points consumed,
    # 6.5333 projected, 94.4 headroom).
    _retract(app, capsys, tmp_path, ids)
    assert W._resets() == [ids["L0"]]
    assert _sidecar() == sidecar
    assert _forecast(account) == pytest.approx((5.6, 5.6 * 7 / 6, 94.4))
    assert _rate_change_events(app) == events
    assert _rate_change_journal() == journalled

    # Checkpoint 3 (the control): a withheld sweep takes the reducer's
    # non-writing branch — the reduced state IS the stored state — so nothing
    # is written and the forecast keeps applying X_OLD.
    withheld = _analysis(status=qm.CalibrationStatus.INSUFFICIENT_HISTORY)
    loaded = qmg.load_calibrations(now=SWEEP_NOW)
    assert qmg.reduce_state(loaded.state, withheld, _mode(account)) is loaded.state
    assert _sweep(account, withheld) == ()
    assert _sidecar() == sidecar
    assert _forecast(account) == pytest.approx((5.6, 5.6 * 7 / 6, 94.4))

    # Checkpoint 2: a trustworthy sweep updates the open regime in place
    # (same effectiveFrom) to X_NEW; the forecast applies it (1.4 consumed,
    # 1.6333 projected, 98.6 headroom); the closed regime and the journalled
    # transition are retained.
    _sweep(account, _analysis(fitted=_evidence(qm, X_NEW)))
    assert _regimes(account) == [closed, ("2026-10-10T00:00:00+00:00", None, X_NEW)]
    assert _forecast(account) == pytest.approx((1.4, 1.4 * 7 / 6, 98.6))
    assert _forecast(account) == _expected_forecast(X_NEW)
    assert _rate_change_events(app) == events
    assert _rate_change_journal() == journalled
    T.rebuild(app)
    assert _rate_change_events(app) == events
