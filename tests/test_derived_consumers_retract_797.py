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

The calibration (#1015, closing R19): a regime fitted before the reviewed
correction to its inputs stops steering what the user sees, immediately and
across rebuilds, without any automatic live recalibration. The checkpoints
pin literal, distinguishable coefficients over a nonzero population:

1. after the correction and the rebuild, with the sidecar bytes unchanged,
   the forecast states `recalibration-needed`, the metering-rate transition
   is effectively superseded and its marker is hidden;
2. a withheld sweep annotates the tainted fit and records the attempt, and
   journals the explicit supersession event (no notification);
3. a trustworthy refit archives the tainted fit and steers again.

Every sweep runs through the production `sweep_account` over a real
`load_population`, so the basis and the correction facts are read by
production code. Further cases cover a closed predecessor fitted across the
corrected day, stamped fits outside the window or account, a legacy regime,
and a sentinel-scoped transition with no lineage and no sidecar.

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
              status=None, split=SPLIT):
    import _cctally_quota_model as qmg
    qm = qmg.qm
    withheld = qm.evidence_withheld("insufficient-history", {"days": 0, "segments": 0})
    family, klass = _composition()
    detector = qm.DetectorResult(
        split_date=split if confirmed else None,
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


def _result(account, analysis, now=SWEEP_NOW):
    """The `AccountResult` `cmd_quota` would build for ``analysis``, over a
    REAL `load_population` of the scratch store: the correction facts and the
    basis are read by production code, never supplied by the test."""
    import _cctally_quota_model as qmg
    era = qmg.qm.era_by_id("opus-5-5")
    load = qmg.load_population(account, since=None, now=now, era=era)
    assert load.correction_facts is not None, load
    return qmg.AccountResult(
        account_key=account, label=None, analysis=analysis, load=load,
        week=None, recorded=None, stale_prior=False, mode_kind="automatic",
        clean=analysis, era=era)


def _mode_from_load(account):
    """The `PersistMode` the production sweep builds (for the reducer-level
    identity check only)."""
    import _cctally_quota_model as qmg
    return qmg.persist_mode(_result(account, None), now=SWEEP_NOW)


def _sweep(account, analysis, now=SWEEP_NOW):
    """One account's sweep through the production `sweep_account`:
    persistence, supersession reconciliation, recording, recovery and
    settlement exactly as `cmd_quota` runs them."""
    import _cctally_quota_model as qmg
    outcome = qmg.sweep_account(_result(account, analysis, now), now=now,
                                delivery=qmg.seed_delivery_state())
    assert outcome.quarantined is None
    return outcome.transitions


def _cli_quota(app, monkeypatch, capsys, analysis) -> dict:
    """`cctally quota --account A@example.test --json` through the CLI
    parser. Only the kernel's `qm.analyse` is substituted, so
    `analyse_account`, `load_population` and `sweep_account` run as
    production code."""
    import _cctally_quota_model as qmg
    capsys.readouterr()
    with monkeypatch.context() as m:
        m.setenv("CCTALLY_AS_OF", SWEEP_NOW.isoformat())
        m.setattr(qmg.qm, "analyse", lambda *_a, **_k: analysis)
        args = app.build_parser().parse_args(
            ["quota", "--account", "A@example.test", "--json"])
        rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == analysis.exit_code, out
    return json.loads(out)


def _capture_notifications(monkeypatch) -> list:
    """Rate-change notifications ON, with every dispatched payload of this
    family captured."""
    import _cctally_journal as jr
    import _cctally_quota_model as qmg
    captured: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", lambda alerts: captured.extend(
        a for a in alerts if a.get("axis") == "meter_rate_change"))
    monkeypatch.setattr(qmg, "rate_change_notifications_enabled",
                        lambda: True)
    return captured


def _sidecar() -> bytes:
    import _cctally_quota_model as qmg
    return qmg.calibration_path().read_bytes()


def _regimes(account) -> list:
    import _cctally_quota_model as qmg
    state = json.loads(_sidecar())
    return [(r["effectiveFrom"], r["effectiveUntil"], r["unitsPerPoint"])
            for r in qmg.stored_regimes(state, account)]


def _detail(account):
    import _cctally_forecast
    return _cctally_forecast._calibrated_week_detail(
        FORECAST_NOW, WEEK_START, WEEK_END, account_key=account)


def _forecast(account):
    detail = _detail(account)
    assert detail.code is None, detail
    return detail.consumption_pct, detail.projection_pct, detail.headroom_pct


def _bucket(account) -> dict:
    return json.loads(_sidecar())["accounts"][account]


def _regime_at(account, index) -> dict:
    return _bucket(account)["regimes"][index]


def _open_regime(account) -> dict:
    [regime] = [r for r in _bucket(account)["regimes"]
                if r.get("effectiveUntil") is None]
    return regime


def _facts(app, account):
    import _cctally_quota_calibration as qcg
    conn = app.open_db()
    try:
        return qcg.read_correction_facts(conn, account_scope=account)
    finally:
        conn.close()


def _basis(app) -> set:
    return {b for (b,) in W._stats(
        app, "SELECT batch_id FROM correction_batch_lineage")}


def _assessments(app, account) -> tuple:
    """Each stored regime's lineage assessment, oldest first."""
    import _cctally_quota_calibration as qcg
    facts = _facts(app, account)
    return tuple(qcg.assess_regime(r, facts, scope=account).state
                 for r in _bucket(account)["regimes"])


def _superseded(app) -> dict:
    """Effective supersession per transition identity (explicit row, or
    derived from the transition's frozen lineage)."""
    import _cctally_quota_calibration as qcg
    conn = app.open_db()
    try:
        state = qcg.read_rate_change_supersessions(conn)
    finally:
        conn.close()
    assert state.available
    return dict(state.by_identity)


def _marker(account):
    import _cctally_quota_calibration as qcg
    return qcg.clean_active_rate_change(account_key=account)


def _supersession_journal():
    return sorted(r["id"] for r in T.journal_records()
                  if r.get("t") == "evt"
                  and (r.get("payload") or {}).get("kind")
                  == "meter_rate_change_supersession")


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
    """#1015 (R19 closed): a calibration fitted before a reviewed correction
    to its inputs stops steering what the user sees, immediately and across
    rebuilds, with no automatic live recalibration (spec 7, A1-A4, A9).

    Production wiring: every sweep is `sweep_account` over a real
    `load_population` (`_sweep`), and the withheld sweep is the CLI command
    path with only `qm.analyse` substituted, so the basis and the correction
    facts are always read by production code. The sweep clock (2026-10-23)
    is BEHIND the reviewed op's journal time, so every outcome below also
    shows ordering by basis rather than by clock (A9)."""
    app, account, ids = _store(monkeypatch, tmp_path)
    import _cctally_quota_model as qmg
    qm = qmg.qm
    dispatched = _capture_notifications(monkeypatch)

    # The retained history: a confirmed change at SPLIT, with no prior state,
    # persists both sides (closed X_CLOSED, open X_OLD) and journals one
    # metering-rate transition, which notifies.
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
    identity = ("claude", account, "2026-10-10T00:00:00+00:00")
    journalled = _rate_change_journal()
    assert len(journalled) == 1
    assert len(dispatched) == 1
    assert _forecast(account) == _expected_forecast(X_OLD)
    assert _marker(account) is not None
    assert _superseded(app) == {}
    transition_lineage = _open_regime(account)["transitionLineage"]
    sidecar = _sidecar()

    # Checkpoint 1 (A1): immediately after the correction and the rebuild,
    # before any sweep, the poisoned open regime no longer steers: the
    # forecast states `recalibration-needed` with no calibrated consumption,
    # projection or headroom (it falls back to the corrected meter). Reads
    # are non-mutating, so the sidecar bytes are unchanged. The transition
    # row is retained and effectively superseded (derived, no event yet), and
    # the marker is hidden.
    _retract(app, capsys, tmp_path, ids)
    assert W._resets() == [ids["L0"]]
    assert _sidecar() == sidecar
    detail = _detail(account)
    assert detail.code == "recalibration-needed", detail
    assert (detail.consumption_pct, detail.projection_pct,
            detail.headroom_pct) == (None, None, None)
    assert _rate_change_events(app) == events
    assert _rate_change_journal() == journalled
    derived = _superseded(app)
    assert set(derived) == {identity}
    assert derived[identity].superseded_at is None
    assert derived[identity].corrections
    assert _marker(account) is None
    # The closed baseline [ERA_START, 2026-10-10) is the negative control:
    # the October 22 retractions fall outside it (A5).
    baseline, poisoned = _assessments(app, account)
    assert (baseline, poisoned) == ("clean", "tainted")
    # Rebuild survival before the explicit event (A4).
    T.rebuild(app)
    assert set(_superseded(app)) == {identity}
    assert _assessments(app, account) == ("clean", "tainted")
    assert _sidecar() == sidecar

    # The withheld checkpoint (A2), through the CLI: the sweep writes the
    # annotation and the recalibration attempt, the forecast states the
    # attempt's status, no transition is recovered or notified, and the
    # explicit supersession event is journalled.
    withheld = _analysis(status=qm.CalibrationStatus.INSUFFICIENT_HISTORY)
    loaded = qmg.load_calibrations(now=SWEEP_NOW)
    assert qmg.reduce_state(
        loaded.state, withheld, _mode_from_load(account)) is not loaded.state
    _cli_quota(app, monkeypatch, capsys, withheld)
    assert _sidecar() != sidecar
    poisoned_record = _open_regime(account)
    assert poisoned_record["unitsPerPoint"] == X_OLD
    annotation = poisoned_record["supersededByCorrection"]
    assert annotation["fitId"] == poisoned_record["fitLineage"]["fitId"]
    assert [c["opId"] for c in annotation["corrections"]] == list(
        derived[identity].corrections)
    attempt = poisoned_record["recalibrationAttempt"]
    assert attempt["status"] == "insufficient-history"
    assert attempt["basis"] == sorted(_basis(app))
    assert poisoned_record["transitionLineage"] == transition_lineage
    assert "supersededByCorrection" not in _regime_at(account, 0)
    assert _detail(account).code == "insufficient-history"
    assert _rate_change_events(app) == events
    assert _rate_change_journal() == journalled
    assert len(dispatched) == 1
    explicit = _supersession_journal()
    assert len(explicit) == 1
    # A second withheld sweep changes nothing and journals nothing more.
    annotated = _sidecar()
    _cli_quota(app, monkeypatch, capsys, withheld)
    assert _sidecar() == annotated
    assert len(_supersession_journal()) == 1

    # Explicit projection (A4): after a rebuild the event is a durable row
    # with its reason and op ids; effective supersession alone would mask a
    # missing applier.
    T.rebuild(app)
    [(reason, corrections)] = W._stats(
        app, "SELECT reason, corrections FROM meter_rate_change_supersessions "
             "WHERE provider = ? AND account_key = ? AND effective_from = ?",
        identity)
    assert reason == "reviewed-correction"
    assert [c["opId"] for c in json.loads(corrections)] == list(
        derived[identity].corrections)
    assert _superseded(app)[identity].superseded_at == SWEEP_NOW.isoformat()
    assert _assessments(app, account) == ("clean", "tainted")

    # The qualifying refit (A3): a trustworthy no-change fit replaces the
    # tainted open regime at the same effectiveFrom; the old fit is archived
    # with its supersession; the forecast applies X_NEW (1.4 consumed, 1.6333
    # projected, 98.6 headroom); the transition stays superseded and
    # un-notified, and its frozen lineage is preserved byte for byte.
    _sweep(account, _analysis(fitted=_evidence(qm, X_NEW)))
    assert _regimes(account) == [closed,
                                 ("2026-10-10T00:00:00+00:00", None, X_NEW)]
    assert _forecast(account) == pytest.approx((1.4, 1.4 * 7 / 6, 98.6))
    assert _forecast(account) == _expected_forecast(X_NEW)
    refitted = _open_regime(account)
    assert "supersededByCorrection" not in refitted
    assert "recalibrationAttempt" not in refitted
    assert refitted["transitionLineage"] == transition_lineage
    assert refitted["fitLineage"]["basis"] == sorted(_basis(app))
    [archived] = _bucket(account)["archivedFits"]
    assert archived["regime"]["unitsPerPoint"] == X_OLD
    assert archived["regime"]["supersededByCorrection"] == annotation
    assert archived["archivedAt"] == SWEEP_NOW.isoformat()
    assert _assessments(app, account) == ("clean", "clean")
    assert _rate_change_events(app) == events
    assert _rate_change_journal() == journalled
    assert len(dispatched) == 1
    assert _marker(account) is None
    assert set(_superseded(app)) == {identity}

    # Rebuild survival after the explicit event (A4).
    T.rebuild(app)
    assert _rate_change_events(app) == events
    assert _superseded(app)[identity].superseded_at == SWEEP_NOW.isoformat()
    assert _assessments(app, account) == ("clean", "clean")
    assert _forecast(account) == _expected_forecast(X_NEW)


def test_a_predecessor_fitted_across_the_correction_and_closed_is_superseded(
        monkeypatch, tmp_path, capsys):
    """Detects a closed regime escaping the lineage check: a predecessor
    fitted across October 22 and closed by a confirmed change BEFORE the
    correction is tainted after it (closing is not a fit, so its lineage
    still names those days), as is the transition it formed, and both
    survive a rebuild (spec 7, A4)."""
    app, account, _ids = _store(monkeypatch, tmp_path)
    import _cctally_quota_model as qmg
    qm = qmg.qm
    _sweep(account, _analysis(fitted=_evidence(qm, X_OLD)))
    late_split = dt.date(2026, 10, 23)
    [transition] = _sweep(account, _analysis(
        baseline=_evidence(qm, X_OLD), watch=_evidence(qm, X_NEW),
        confirmed=True, split=late_split))
    assert _assessments(app, account) == ("clean", "clean")
    _retract(app, capsys, tmp_path, _ids)
    assert _assessments(app, account) == ("tainted", "tainted")
    assert set(_superseded(app)) == {transition.identity()}
    T.rebuild(app)
    assert _assessments(app, account) == ("tainted", "tainted")
    assert set(_superseded(app)) == {transition.identity()}


def test_a_stamped_fit_outside_the_window_or_account_keeps_steering(
        monkeypatch, tmp_path, capsys):
    """Detects lineage that is not window- and account-precise for stamped
    fits (A5): a fit whose windows end before October 22, and a fit for
    another account, stay clean across the correction."""
    app, account, ids = _store(monkeypatch, tmp_path)
    import _cctally_quota_calibration as qcg
    import _cctally_quota_model as qmg
    qm = qmg.qm
    early = dt.datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    _sweep(account, _analysis(fitted=_evidence(qm, X_OLD)), now=early)
    other = "acct-other"
    _sweep(other, _analysis(fitted=_evidence(qm, X_NEW)))
    _retract(app, capsys, tmp_path, ids)
    assert _assessments(app, account) == ("clean",)
    assert _forecast(account) == _expected_forecast(X_OLD)
    [regime] = _bucket(other)["regimes"]
    assert qcg.assess_regime(regime, _facts(app, other),
                             scope=other).state == "clean"
    # Non-vacuity: the same account's fit over the corrected days is tainted.
    covering = qmg.reduce_state(
        json.loads(_sidecar()), _analysis(fitted=_evidence(qm, X_OLD)),
        dataclasses.replace(_mode_from_load(account),
                            correction_basis=frozenset()))
    [stale_basis] = qmg.stored_regimes(covering, account)
    assert qcg.assess_regime(stale_basis, _facts(app, account),
                             scope=account).state == "tainted"


def test_a_legacy_regime_is_withheld_only_under_an_overlapping_correction(
        monkeypatch, tmp_path, capsys):
    """Detects a legacy (unstamped) regime treated as current: it is clean
    with no correction and withheld under an overlapping one (R1)."""
    app, account, ids = _store(monkeypatch, tmp_path)
    import _cctally_quota_model as qmg
    _sweep(account, _analysis(fitted=_evidence(qmg.qm, X_OLD)))
    state = json.loads(_sidecar())
    legacy = {k: v for k, v in qmg.stored_regimes(state, account)[0].items()
              if k not in ("fitLineage", "wpg", "wpgDigest", "wpr",
                           "wprDigest", "wpx", "wpxDigest")}
    state["accounts"][account]["regimes"] = [legacy]
    qmg.save_calibrations(state)
    assert _forecast(account) == _expected_forecast(X_OLD)
    _retract(app, capsys, tmp_path, ids)
    assert _detail(account).code == "recalibration-needed"


def test_a_sentinel_transition_with_no_lineage_is_superseded_without_a_sidecar(
        monkeypatch, tmp_path, capsys):
    """Detects R1-P2-2: a null-lineage transition recorded under the
    `unattributed` sentinel (a merged fit on a single-real-account install)
    is assessed over EVERY account, so a correction for the real account
    supersedes it even with the sidecar gone: history labelled, the owed
    notification settled silently, and both survive a rebuild."""
    app, account, ids = _store(monkeypatch, tmp_path)
    import _cctally_quota_model as qmg
    import _lib_meter_rate_change as mrc
    dispatched = _capture_notifications(monkeypatch)
    delivery = qmg.seed_delivery_state()
    sentinel = mrc.RateChangeTransition(
        provider="claude", account_key="unattributed",
        effective_from="2026-10-10T00:00:00+00:00",
        previous_units_per_point=X_CLOSED, new_units_per_point=X_OLD,
        severity=mrc.transition_severity(X_CLOSED, X_OLD),
        detected_at=SWEEP_NOW.isoformat())
    monkeypatch.setattr(qmg, "rate_change_notifications_enabled",
                        lambda: False)
    assert qmg.record_rate_change_transition(sentinel, now=SWEEP_NOW)
    monkeypatch.setattr(qmg, "rate_change_notifications_enabled",
                        lambda: True)
    assert not qmg.calibration_path().exists()
    assert [t.identity() for t in qmg._owed_rate_change_transitions(
        ["unattributed"], delivery, now=SWEEP_NOW)] == [sentinel.identity()]
    _retract(app, capsys, tmp_path, ids)
    derived = _superseded(app)
    assert set(derived) == {sentinel.identity()}
    assert derived[sentinel.identity()].superseded_at is None
    assert qmg._owed_rate_change_transitions(
        ["unattributed"], delivery, now=SWEEP_NOW) == ()
    T.rebuild(app)
    assert set(_superseded(app)) == {sentinel.identity()}
    # The sweep journals the explicit event and notifies nothing.
    _cli_quota(app, monkeypatch, capsys, _analysis(
        status=qmg.qm.CalibrationStatus.INSUFFICIENT_HISTORY))
    assert dispatched == []
    assert len(_supersession_journal()) == 1
    T.rebuild(app)
    assert _superseded(app)[sentinel.identity()].superseded_at == \
        SWEEP_NOW.isoformat()



def _confirmed_change(qm, new=X_OLD):
    return _analysis(
        fitted=_evidence(qm, new), baseline=_evidence(qm, X_CLOSED),
        watch=_evidence(qm, new), confirmed=True,
        status=qm.CalibrationStatus.OK)


def _rate_change_payloads():
    import _lib_meter_rate_change as mrc
    return [r["payload"] for r in T.journal_records()
            if r.get("t") == "evt"
            and (r.get("payload") or {}).get("kind") == mrc.EVT_KIND]


def test_the_marker_hides_when_the_correction_facts_are_unreadable(
        monkeypatch, tmp_path):
    """Detects an unverifiable rate change asserted to the user (spec 5.6,
    A6): with the stats store unreadable, the status line, TUI and doctor
    marker predicate reports `unavailable` and shows nothing."""
    app, account, _ids = _store(monkeypatch, tmp_path)
    import _cctally_quota_calibration as qcg
    import _cctally_quota_model as qmg
    import _cctally_store
    _sweep(account, _confirmed_change(qmg.qm))
    assert _marker(account) is not None  # non-vacuity
    monkeypatch.setattr(_cctally_store, "stats_open_readonly_failfast",
                        lambda *_a, **_k: None)
    state, active = qcg.assess_active_rate_change(account_key=account)
    assert (state, active) == (qcg.MARKER_UNAVAILABLE, None)
    assert _marker(account) is None
    assert qcg.marker_regimes(account) == ()


@pytest.mark.parametrize("correction", ["none", "after-refit",
                                        "before-refit"])
def test_recovery_trusts_only_the_frozen_transition_lineage(
        monkeypatch, tmp_path, capsys, correction):
    """Detects recovery confusing fit and transition lineage (R1-P2-3,
    A13): a transition whose recording failed, followed by a no-change refit
    and a correction inside its detection-time window, is never recorded or
    notified by recovery. With the correction BEFORE the refit, the refit's
    fit lineage already contains the op while the transition's frozen basis
    does not, so only the frozen lineage can refuse it. Without a
    correction (the control) recovery records it."""
    app, account, ids = _store(monkeypatch, tmp_path)
    import _cctally_quota_model as qmg
    qm = qmg.qm
    dispatched = _capture_notifications(monkeypatch)
    later = SWEEP_NOW + dt.timedelta(hours=1)
    with monkeypatch.context() as m:
        m.setattr(qmg, "record_rate_change_transition",
                  lambda *_a, **_k: None)
        assert len(_sweep(account, _confirmed_change(qm))) == 1
        frozen = _open_regime(account)["transitionLineage"]
        if correction == "before-refit":
            _retract(app, capsys, tmp_path, ids)
        _sweep(account, _confirmed_change(qm), now=later)
    assert _open_regime(account)["transitionLineage"] == frozen
    if correction == "before-refit":
        refit = _open_regime(account)["fitLineage"]["basis"]
        assert set(refit) - set(frozen["basis"])
    if correction == "after-refit":
        _retract(app, capsys, tmp_path, ids)
    assert _rate_change_events(app) == []
    _sweep(account, _analysis(status=qm.CalibrationStatus.INSUFFICIENT_HISTORY),
           now=later + dt.timedelta(hours=1))
    if correction == "none":
        assert _rate_change_events(app) == [
            ("claude", account, "2026-10-10T00:00:00+00:00", X_CLOSED, X_OLD)]
        assert len(dispatched) == 1
    else:
        assert _rate_change_events(app) == []
        assert _rate_change_journal() == []
        assert dispatched == []


def test_recovery_emits_the_frozen_values_not_a_later_refit(
        monkeypatch, tmp_path, capsys):
    """Detects recovered values differing from the frozen lineage (R2-P1-5,
    A13): detected at T1, recording failed, refitted at T2 to different
    successor values, then recovered — the journalled event carries T1's
    values and T1's lineage. A correction after recovery, outside T1's
    windows but inside T2's fit window, leaves the transition unsuperseded
    and withholds the refitted regime."""
    app, account, ids = _store(monkeypatch, tmp_path)
    import _cctally_quota_model as qmg
    import _lib_quota_lineage as L
    qm = qmg.qm
    t1 = dt.datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    with monkeypatch.context() as m:
        m.setattr(qmg, "record_rate_change_transition",
                  lambda *_a, **_k: None)
        _sweep(account, _confirmed_change(qm), now=t1)
    frozen = _open_regime(account)["transitionLineage"]
    assert frozen["descriptor"]["new_units_per_point"] == X_OLD
    assert _rate_change_events(app) == []
    assert _sweep(account, _confirmed_change(qm, X_NEW)) == ()
    refitted = _open_regime(account)
    assert refitted["unitsPerPoint"] == X_NEW
    assert L.fit_lineage_verifies(refitted)
    assert refitted["transitionLineage"] == frozen
    assert _rate_change_events(app) == [
        ("claude", account, "2026-10-10T00:00:00+00:00", X_CLOSED, X_OLD)]
    [payload] = _rate_change_payloads()
    assert payload["fit_lineage"] == frozen
    assert payload["fit_lineage"]["digest"] != refitted["fitLineage"]["digest"]
    _retract(app, capsys, tmp_path, ids)
    assert _superseded(app) == {}
    assert _detail(account).code == "recalibration-needed"


def _strip_transition_lineage(account):
    """Rewrite the open successor as a writer-verified regime with NO
    `transitionLineage`, exactly what a #1019 release (current writer
    stamps, no #1015 lineage) persists, through the production stamper."""
    import _cctally_quota_model as qmg
    stored = qmg.load_calibrations().state
    reduced = json.loads(json.dumps(stored))
    for regime in reduced["accounts"][account]["regimes"]:
        if regime.get("effectiveUntil") is None:
            assert regime.pop("transitionLineage")
    reduced = qmg.stamp_current_writes(stored, reduced, account)
    qmg.save_calibrations(reduced)
    successor = _open_regime(account)
    assert "transitionLineage" not in successor
    assert qmg.regime_writer_is_current(successor)


@pytest.mark.parametrize("correction", ["none", "before-refit"])
def test_recovery_of_a_writer_verified_successor_without_lineage(
        monkeypatch, tmp_path, capsys, correction):
    """Detects F8: a writer-verified successor a #1019 release wrote, with no
    `transitionLineage`, refused recovery permanently. It recovers as a
    legacy transition (basis [], window ending at the recovery run's now)
    with its values from the regime pair; any earlier correction inside
    that window (here one landed before a refit that re-cleaned the
    successor's own fit) supersedes it, so it is neither recorded nor
    notified."""
    app, account, ids = _store(monkeypatch, tmp_path)
    import _cctally_quota_model as qmg
    qm = qmg.qm
    dispatched = _capture_notifications(monkeypatch)
    later = SWEEP_NOW + dt.timedelta(hours=1)
    with monkeypatch.context() as m:
        m.setattr(qmg, "record_rate_change_transition",
                  lambda *_a, **_k: None)
        assert len(_sweep(account, _confirmed_change(qm))) == 1
        if correction == "before-refit":
            _retract(app, capsys, tmp_path, ids)
            _sweep(account, _confirmed_change(qm), now=later)
        _strip_transition_lineage(account)
    # Non-vacuity: the pair itself is clean (the marker shows it), so only
    # the legacy transition lineage can refuse the recovery.
    assert _marker(account) is not None
    assert _rate_change_events(app) == []
    _sweep(account, _analysis(status=qm.CalibrationStatus.INSUFFICIENT_HISTORY),
           now=later + dt.timedelta(hours=1))
    if correction == "none":
        assert _rate_change_events(app) == [
            ("claude", account, "2026-10-10T00:00:00+00:00", X_CLOSED, X_OLD)]
        assert len(dispatched) == 1
    else:
        assert _rate_change_events(app) == []
        assert _rate_change_journal() == []
        assert dispatched == []
