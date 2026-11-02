"""#661 S1 — the calibration sidecar and the pure state reducer (spec 7, 20).

The fitted calibration lives in neither database, which is what satisfies the
durability requirement by construction. These tests cover the reducer as a
pure function of `(stored, analysis, mode)` and the write protocol as a
protocol: modes, `O_EXCL`, the parent-directory fsync, the quarantine of a
file this binary cannot own, and the lock that serializes the whole
read-modify-write.
"""
from __future__ import annotations

import datetime as dt
import json
import multiprocessing
import os
import stat

import pytest

import _cctally_core
from tests._script_loader import load_script_module

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
REGIME_START = dt.datetime(2026, 7, 25, tzinfo=UTC)


@pytest.fixture()
def glue(tmp_path, monkeypatch):
    share = tmp_path / "data"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CCTALLY_DATA_DIR", str(share))
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    ns = load_script_module()
    _cctally_core._init_paths_from_env()
    return ns._load_sibling("_cctally_quota_model")


# --------------------------------------------------------------------------
# Analysis builders. `QuotaAnalysis` is a plain frozen dataclass, so the
# reducer is exercised against constructed values rather than against a fit
# the test would have to reproduce.
# --------------------------------------------------------------------------
def _evidence(glue, value, *, days=8, segments=1, qualifications=()):
    qm = glue.qm
    return qm.evidence_available(
        value, qm.Interval(value * 0.95, value * 1.05),
        qm.Support(days, segments), {"days": days, "segments": segments},
        qualifications)


def _withheld(glue, code="insufficient-history"):
    return glue.qm.evidence_withheld(code, {"days": 0, "segments": 0})


def _detector(glue, *, split=None, qualified=False):
    return glue.qm.DetectorResult(
        split_date=split, raw_p=0.0004 if qualified else None,
        holm_p=0.006 if qualified else None, holm_family_size=13,
        baseline_days=26 if qualified else 0, watch_days=3 if qualified else 0,
        longest_run=3 if qualified else 0, qualified=qualified,
        input_eligible_days=29, scanned_eligible_days=29,
        truncated_eligible_days=0, scan_start_date=dt.date(2026, 7, 25),
        max_auto_scan_days=64, history_truncated=False)


def _analysis(glue, *, status=None, verdict=None, exit_code=0, fitted=None,
              baseline=None, watch=None, detector=None, blocking=(),
              detector_input_causes=None, composition_provenance=None,
              fit_family_shares=None, baseline_fit_family_shares=None):
    """One constructed analysis.

    #688: the two typed fields are populated EXPLICITLY and default to the
    supported-and-clean values, so an existing test cannot accidentally start
    permitting the exceptional persistence path. A test that wants the
    permitted shape passes `composition_provenance` deliberately.
    """
    qm = glue.qm
    status = status if status is not None else qm.CalibrationStatus.OK
    verdict = verdict if verdict is not None else qm.Verdict.NO_RATE_CHANGE
    fitted = fitted if fitted is not None else _evidence(glue, 2_000_000.0)
    return qm.QuotaAnalysis(
        status=status, verdict=verdict, exit_code=exit_code, fitted=fitted,
        consumption=fitted, projection=fitted, headroom=fitted,
        baseline_fit=baseline if baseline is not None else _withheld(glue),
        watch_fit=watch if watch is not None else _withheld(glue),
        observed_percent=40, family_shares={"claude-opus-5": 1.0},
        class_shares={"fresh": 1.0}, current_family_shares={},
        current_class_shares={}, family_radius=0.02, class_radius=0.0105,
        detector=detector if detector is not None else _detector(glue),
        blocking=tuple(blocking), diagnostics={},
        detector_input_causes=(frozenset() if detector_input_causes is None
                               else detector_input_causes),
        composition_provenance=(frozenset() if composition_provenance is None
                                else composition_provenance),
        fit_family_shares=fit_family_shares,
        baseline_fit_family_shares=baseline_fit_family_shares)


def _mode(glue, kind="automatic", account_key=None, coefficient_era="opus-5",
          now=NOW, regime_start=REGIME_START):
    return glue.PersistMode(
        kind=kind, account_key=account_key, now=now,
        fingerprint=glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
        regime_start=regime_start, coefficient_era=coefficient_era)


def _regimes(state, glue, account_key=None):
    return glue.stored_regimes(state, account_key)


# --------------------------------------------------------------------------
# The reducer.
# --------------------------------------------------------------------------
def test_an_initial_trustworthy_fit_writes_one_open_regime(glue):
    state = glue.reduce_state(glue.empty_calibration_state(),
                              _analysis(glue), _mode(glue))
    regimes = _regimes(state, glue)
    assert len(regimes) == 1
    assert regimes[0]["effectiveUntil"] is None
    assert regimes[0]["unitsPerPoint"] == 2_000_000.0
    assert regimes[0]["fingerprint"] == \
        glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT


def test_a_no_change_run_updates_the_open_regime_in_place(glue):
    first = glue.reduce_state(glue.empty_calibration_state(),
                              _analysis(glue), _mode(glue))
    second = glue.reduce_state(
        first, _analysis(glue, fitted=_evidence(glue, 2_100_000.0)),
        _mode(glue))
    regimes = _regimes(second, glue)
    assert len(regimes) == 1, "an in-place update must not append"
    assert regimes[0]["unitsPerPoint"] == 2_100_000.0
    assert regimes[0]["effectiveFrom"] == \
        _regimes(first, glue)[0]["effectiveFrom"]


def test_a_confirmed_change_closes_the_open_regime_and_persists_both_sides(
        glue):
    first = glue.reduce_state(glue.empty_calibration_state(),
                              _analysis(glue), _mode(glue))
    split = dt.date(2026, 8, 25)
    changed = _analysis(
        glue, status=glue.qm.CalibrationStatus.INSUFFICIENT_HISTORY,
        verdict=glue.qm.Verdict.RATE_CHANGE_DETECTED, exit_code=1,
        fitted=_withheld(glue),
        baseline=_evidence(glue, 2_050_000.0, days=26,
                           qualifications=("superseded",)),
        watch=_evidence(glue, 1_700_000.0, days=3,
                        qualifications=("successor-regime", "detection-only")),
        detector=_detector(glue, split=split, qualified=True))
    state = glue.reduce_state(first, changed, _mode(glue))
    regimes = _regimes(state, glue)
    assert len(regimes) == 2
    boundary = dt.datetime.combine(split, dt.time(0), tzinfo=UTC).isoformat()
    assert regimes[0]["effectiveUntil"] == boundary
    assert regimes[1]["effectiveFrom"] == boundary
    assert regimes[1]["effectiveUntil"] is None
    assert regimes[1]["unitsPerPoint"] == 1_700_000.0


def test_a_confirmed_change_with_no_prior_state_still_persists_both_sides(
        glue):
    """Spec section 20: the FIRST run that discovers a split persists both."""
    split = dt.date(2026, 8, 25)
    changed = _analysis(
        glue, status=glue.qm.CalibrationStatus.INSUFFICIENT_HISTORY,
        verdict=glue.qm.Verdict.RATE_CHANGE_DETECTED, exit_code=1,
        fitted=_withheld(glue),
        baseline=_evidence(glue, 2_050_000.0, days=26),
        watch=_evidence(glue, 1_700_000.0, days=3),
        detector=_detector(glue, split=split, qualified=True))
    state = glue.reduce_state(glue.empty_calibration_state(), changed,
                              _mode(glue))
    regimes = _regimes(state, glue)
    assert [r["unitsPerPoint"] for r in regimes] == [2_050_000.0, 1_700_000.0]
    assert regimes[0]["effectiveUntil"] is not None


def test_rediscovering_the_same_split_does_not_append_a_duplicate(glue):
    split = dt.date(2026, 8, 25)
    changed = _analysis(
        glue, status=glue.qm.CalibrationStatus.INSUFFICIENT_HISTORY,
        verdict=glue.qm.Verdict.RATE_CHANGE_DETECTED, exit_code=1,
        fitted=_withheld(glue),
        baseline=_evidence(glue, 2_050_000.0, days=26),
        watch=_evidence(glue, 1_700_000.0, days=3),
        detector=_detector(glue, split=split, qualified=True))
    once = glue.reduce_state(glue.empty_calibration_state(), changed,
                             _mode(glue))
    twice = glue.reduce_state(once, changed, _mode(glue))
    assert len(_regimes(twice, glue)) == 2


def test_a_fingerprint_mismatch_marks_stale_and_appends_without_rewriting(
        glue):
    stale_state = {
        "schemaVersion": 1,
        "accounts": {"*": {"regimes": [{
            "effectiveFrom": "2026-06-01T00:00:00+00:00",
            "effectiveUntil": None, "fingerprint": "old-fingerprint",
            "algorithmRevision": 1, "unitsPerPoint": 1_500_000.0,
            "interval": {"lo": 1.4e6, "hi": 1.6e6},
            "support": {"days": 20, "segments": 2}, "status": "ok",
            "asOf": "2026-06-10T00:00:00+00:00", "qualifications": []}]}},
    }
    state = glue.reduce_state(stale_state, _analysis(glue), _mode(glue))
    regimes = _regimes(state, glue)
    assert len(regimes) == 2, "the new fit is appended, never merged"
    assert regimes[0]["unitsPerPoint"] == 1_500_000.0, \
        "the old value was true under its own constants and is never rewritten"
    assert regimes[0]["status"] == "stale"
    assert regimes[0]["effectiveUntil"] is not None
    assert regimes[1]["fingerprint"] == \
        glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT
    assert regimes[1]["unitsPerPoint"] == 2_000_000.0
    # The input document is not mutated: the reducer is pure.
    assert stale_state["accounts"]["*"]["regimes"][0]["status"] == "ok"


def test_a_non_trustworthy_analysis_writes_nothing(glue):
    stored = glue.reduce_state(glue.empty_calibration_state(),
                               _analysis(glue), _mode(glue))
    thin = _analysis(
        glue, status=glue.qm.CalibrationStatus.INSUFFICIENT_HISTORY,
        verdict=glue.qm.Verdict.WITHHELD, exit_code=4, fitted=_withheld(glue))
    assert glue.reduce_state(stored, thin, _mode(glue)) is stored


def test_an_override_run_writes_nothing(glue):
    stored = glue.reduce_state(glue.empty_calibration_state(),
                               _analysis(glue), _mode(glue))
    later = _analysis(glue, fitted=_evidence(glue, 9_000_000.0))
    assert glue.reduce_state(stored, later,
                             _mode(glue, kind="diagnostic")) is stored


def test_each_account_keeps_its_own_regimes(glue):
    state = glue.reduce_state(glue.empty_calibration_state(),
                              _analysis(glue), _mode(glue, account_key="a"))
    state = glue.reduce_state(
        state, _analysis(glue, fitted=_evidence(glue, 3_000_000.0)),
        _mode(glue, account_key="b"))
    assert _regimes(state, glue, "a")[0]["unitsPerPoint"] == 2_000_000.0
    assert _regimes(state, glue, "b")[0]["unitsPerPoint"] == 3_000_000.0
    assert _regimes(state, glue) == []


# --------------------------------------------------------------------------
# The file: modes, atomicity, quarantine, reset.
# --------------------------------------------------------------------------
def test_the_state_file_and_its_lock_are_mode_0600(glue):
    glue.persist(_analysis(glue), _mode(glue))
    for path in (glue.calibration_path(), glue.calibration_lock_path()):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_the_write_fsyncs_the_parent_directory(glue, monkeypatch):
    """`save_config` does not, and a crash after the rename then loses the
    rename itself. This protocol is deliberately stronger."""
    synced_dirs = []
    real = os.fsync

    def spy(fd):
        try:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                synced_dirs.append(fd)
        except OSError:
            pass
        return real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    glue.persist(_analysis(glue), _mode(glue))
    assert synced_dirs, "the parent directory was never fsynced"


def test_a_temporary_file_left_by_a_crashed_writer_is_removed(glue):
    path = glue.calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    orphan = path.with_name(f"{path.name}.tmp.99999.0")
    orphan.write_text("{}")
    glue.persist(_analysis(glue), _mode(glue))
    assert not orphan.exists()
    assert path.exists()


def test_a_malformed_file_is_quarantined_not_overwritten(glue):
    path = glue.calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json")
    quarantined = glue.persist(_analysis(glue), _mode(glue))
    assert quarantined is not None
    from pathlib import Path
    assert Path(quarantined).read_text() == "this is not json"
    assert json.loads(path.read_text())["accounts"]


def test_a_version_ahead_file_is_preserved(glue):
    path = glue.calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ahead = json.dumps({"schemaVersion": 99, "accounts": {"*": {"regimes": [
        {"unitsPerPoint": 1.0}]}}})
    path.write_text(ahead)
    loaded = glue.load_calibrations(now=NOW)
    assert loaded.quarantined is not None
    from pathlib import Path
    assert json.loads(Path(loaded.quarantined).read_text())[
        "schemaVersion"] == 99
    assert loaded.state == glue.empty_calibration_state()


def test_two_quarantines_in_the_same_second_do_not_collide(glue):
    path = glue.calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    paths = []
    for _ in range(2):
        path.write_text("nope")
        paths.append(glue.load_calibrations(now=NOW).quarantined)
    assert len(set(paths)) == 2
    from pathlib import Path
    assert all(Path(p).exists() for p in paths)


def test_reset_is_account_scoped_and_idempotent(glue):
    state = glue.reduce_state(glue.empty_calibration_state(),
                              _analysis(glue), _mode(glue, account_key="a"))
    state = glue.reduce_state(state, _analysis(glue),
                              _mode(glue, account_key="b"))
    with glue.calibration_lock():
        glue.save_calibrations(state)
    assert glue.reset_calibration("a", now=NOW) is True
    assert glue.reset_calibration("a", now=NOW) is False
    after = glue.load_calibrations(now=NOW).state
    assert _regimes(after, glue, "a") == []
    assert _regimes(after, glue, "b")[0]["unitsPerPoint"] == 2_000_000.0


def _writer(share, repo_root, value, count):
    import sys
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    os.environ["CCTALLY_DATA_DIR"] = str(share)
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    from tests._script_loader import load_script_module as load
    import _cctally_core as core
    ns = load()
    core._init_paths_from_env()
    mod = ns._load_sibling("_cctally_quota_model")
    for index in range(count):
        with mod.calibration_lock():
            state = mod.load_calibrations(now=NOW).state
            accounts = dict(state.get("accounts") or {})
            bucket = dict(accounts.get(value) or {"regimes": []})
            bucket["regimes"] = list(bucket["regimes"]) + [
                {"effectiveFrom": f"2026-07-{index + 1:02d}T00:00:00+00:00",
                 "effectiveUntil": None, "fingerprint": "f",
                 "unitsPerPoint": float(index)}]
            accounts[value] = bucket
            state["accounts"] = accounts
            mod.save_calibrations(state)


def test_concurrent_writers_do_not_lose_each_others_updates(glue, tmp_path):
    """The atomic rename protects readers; the lock protects the
    read-modify-write, which is what `config set` learned the hard way."""
    share = _cctally_core.APP_DIR
    ctx = multiprocessing.get_context("spawn")
    import pathlib
    repo_root = str(pathlib.Path(__file__).resolve().parents[1])
    procs = [ctx.Process(target=_writer, args=(share, repo_root, name, 6))
             for name in ("acct-a", "acct-b")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=120)
        assert proc.exitcode == 0
    state = glue.load_calibrations(now=NOW).state
    assert len(state["accounts"]["acct-a"]["regimes"]) == 6
    assert len(state["accounts"]["acct-b"]["regimes"]) == 6


def test_the_calibration_lock_requests_exclusive_flock(glue, monkeypatch):
    """The concurrency contract must not depend on scheduler overlap.

    Two spawned writers can run one after the other even when the lock is
    accidentally shared, making the end-state test pass by luck. This pins the
    operating-system request the context manager makes while still exercising
    its enter/yield/exit behavior.
    """
    operations = []

    def record(_fd, operation):
        operations.append(operation)

    monkeypatch.setattr(glue.fcntl, "flock", record)
    with glue.calibration_lock():
        assert operations == [glue.fcntl.LOCK_EX]
    assert operations == [glue.fcntl.LOCK_EX, glue.fcntl.LOCK_UN]


def test_a_confirmed_change_whose_successor_cannot_be_fitted_writes_nothing(
        glue):
    """The guard `reduce_state` opens with is load-bearing for its own
    contract, not redundant with the trailing branch.

    `analyse` cannot currently produce this combination: spec section 33 makes
    an uncomputable fit `unstable-fit`, which `resolve_outcome` withholds, so
    the verdict is never `rate-change-detected` with a withheld successor.
    `reduce_state` is a public pure function over any `QuotaAnalysis` its
    caller builds, and without the guard this input closes the predecessor and
    appends a baseline while opening no successor — leaving the account with
    no open regime, so the next run re-opens one from the analysis start and
    the split is lost.
    """
    first = glue.reduce_state(glue.empty_calibration_state(),
                              _analysis(glue), _mode(glue))
    hollow = _analysis(
        glue, status=glue.qm.CalibrationStatus.INSUFFICIENT_HISTORY,
        verdict=glue.qm.Verdict.RATE_CHANGE_DETECTED, exit_code=1,
        fitted=_withheld(glue), baseline=_withheld(glue),
        watch=_withheld(glue),
        detector=_detector(glue, split=dt.date(2026, 8, 25), qualified=True))
    assert glue.reduce_state(first, hollow, _mode(glue)) is first


# --------------------------------------------------------------------------
# #688 — the second admission path
#
# `reduce_state` derives `confirmed` from the VERDICT, and `resolve_outcome`
# withholds the verdict for every blocking status. The durable history
# therefore depended on a trustworthy fit while `docs/commands/quota.md`
# promised it from the first detection. These pin the exception and, more
# importantly, everything it must still refuse.
# --------------------------------------------------------------------------
SPLIT = dt.date(2026, 8, 25)
SPLIT_ISO = dt.datetime.combine(SPLIT, dt.time(0), tzinfo=UTC).isoformat()


def _permitted_analysis(glue, **kw):
    """The section 2 occurrence: a qualified detector, a withheld verdict,
    both fits available, and a forecast-aggregate composition finding."""
    qm = glue.qm
    args = dict(
        status=qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX,
        verdict=qm.Verdict.WITHHELD, exit_code=3,
        fitted=_withheld(glue, "unsupported-model-mix"),
        baseline=_evidence(glue, 2_442_620.0, days=26,
                           qualifications=("superseded",)),
        watch=_evidence(glue, 1_665_096.0, days=4,
                        qualifications=("successor-regime", "detection-only")),
        detector=_detector(glue, split=SPLIT, qualified=True),
        composition_provenance=frozenset(
            {qm.CompositionProvenance.FORECAST_AGGREGATE}))
    args.update(kw)
    return _analysis(glue, **args)


def _ordinary_confirmed_analysis(glue):
    qm = glue.qm
    return _analysis(
        glue, status=qm.CalibrationStatus.INSUFFICIENT_HISTORY,
        verdict=qm.Verdict.RATE_CHANGE_DETECTED, exit_code=1,
        fitted=_withheld(glue),
        baseline=_evidence(glue, 2_050_000.0, days=26),
        watch=_evidence(glue, 1_700_000.0, days=3),
        detector=_detector(glue, split=SPLIT, qualified=True))


def test_688_the_permitted_withheld_shape_persists_both_regimes(glue):
    state = glue.reduce_state(glue.empty_calibration_state(),
                              _permitted_analysis(glue), _mode(glue))
    regimes = _regimes(state, glue)
    assert len(regimes) == 2
    baseline, successor = regimes
    # The baseline is stamped `ok`: `baseline_fit` describes the pre-split
    # population independently, and a support failure in the successor does
    # not retroactively degrade it.
    assert baseline["status"] == "ok"
    assert baseline["effectiveUntil"] == SPLIT_ISO
    assert successor["status"] == "unsupported-model-mix"
    assert successor["effectiveFrom"] == SPLIT_ISO
    assert successor["effectiveUntil"] is None
    assert "detection-only" in successor["qualifications"]


def test_688_the_permitted_shape_yields_exactly_one_descriptor(glue):
    # Cold store: no adjacent pair before, one after.
    _quarantined, fresh, candidates = glue.persist_and_detect(
        _permitted_analysis(glue), _mode(glue))
    assert len(fresh) == 1
    assert fresh[0].effective_from == SPLIT_ISO
    assert fresh[0].withholding_status == "unsupported-model-mix"
    # #689: the persisted enumeration is a separate collection, and on this
    # run it names the same pair the freshness comparison just produced.
    assert [c.effective_from for c in candidates] == [SPLIT_ISO]
    # #1015 spec 5.6/5.7: recovery emits the transition's FROZEN descriptor,
    # so a recovered event carries the detection's own disclosure rather
    # than nulls rebuilt from the stored regimes.
    assert [c.withholding_status for c in candidates] == [
        "unsupported-model-mix"]
    assert candidates[0].fit_lineage == fresh[0].fit_lineage


def test_688_a_second_run_yields_no_further_descriptor(glue):
    glue.persist_and_detect(_permitted_analysis(glue), _mode(glue))
    _quarantined, fresh, candidates = glue.persist_and_detect(
        _permitted_analysis(glue), _mode(glue))
    assert fresh == ()
    # #689: the run that writes nothing is exactly the run recovery depends
    # on, so the third element must still name the persisted pair.
    assert [c.effective_from for c in candidates] == [SPLIT_ISO]


def test_688_a_successor_status_flip_yields_no_second_descriptor(glue):
    glue.persist_and_detect(_permitted_analysis(glue), _mode(glue))
    healed = _permitted_analysis(
        glue, status=glue.qm.CalibrationStatus.OK,
        verdict=glue.qm.Verdict.RATE_CHANGE_DETECTED, exit_code=1)
    _quarantined, fresh, candidates = glue.persist_and_detect(
        healed, _mode(glue))
    assert fresh == ()
    assert [c.effective_from for c in candidates] == [SPLIT_ISO]


def test_688_an_ordinary_confirmed_transition_carries_no_withholding_status(
        glue):
    _quarantined, fresh, _candidates = glue.persist_and_detect(
        _ordinary_confirmed_analysis(glue), _mode(glue))
    assert len(fresh) == 1
    assert fresh[0].withholding_status is None


@pytest.mark.parametrize("status_name", [
    "UNAVAILABLE", "FUTURE", "STALE", "TOKEN_SPLIT_UNKNOWN",
    "LOCAL_HISTORY_INCOMPLETE", "UNVALIDATED_COEFFICIENT_ERA",
    "FRAGMENTED_HISTORY", "UNSTABLE_FIT",
])
def test_688_each_refusing_status_writes_nothing(glue, status_name):
    """Every other admission prerequisite is satisfied, so each of these
    refuses for its own status rather than incidentally."""
    stored = glue.empty_calibration_state()
    analysis = _permitted_analysis(
        glue, status=getattr(glue.qm.CalibrationStatus, status_name))
    assert glue.reduce_state(stored, analysis, _mode(glue)) is stored


def test_688_a_blocking_reason_with_a_permitted_status_writes_nothing(glue):
    # `resolve_outcome` withholds on a non-empty reason tuple independently
    # of the status, so this combination satisfies every other condition and
    # testing the two axes separately does not cover it.
    stored = glue.empty_calibration_state()
    analysis = _permitted_analysis(
        glue,
        blocking=(glue.qm.BlockingReason.SPARSE_DAY_IN_DECISIVE_RUN,))
    assert glue.reduce_state(stored, analysis, _mode(glue)) is stored


def test_688_a_decisive_day_provenance_writes_nothing(glue):
    # The decisive-day probe guards DETECTION, so a composition finding it
    # raised says the step itself may be an artefact.
    stored = glue.empty_calibration_state()
    analysis = _permitted_analysis(
        glue, composition_provenance=frozenset(
            {glue.qm.CompositionProvenance.DECISIVE_DAY}))
    assert glue.reduce_state(stored, analysis, _mode(glue)) is stored


def test_688_an_unsupported_composition_cause_writes_nothing(glue):
    # A withheld day is excluded from the detector's rank-sum population, so
    # this origin removes evidence from detection itself.
    stored = glue.empty_calibration_state()
    analysis = _permitted_analysis(
        glue, detector_input_causes=frozenset(
            {glue.qm.WithholdingCause.UNSUPPORTED_COMPOSITION}))
    assert glue.reduce_state(stored, analysis, _mode(glue)) is stored


def test_688_a_withheld_successor_fit_writes_nothing(glue):
    """The permitted path still needs a successor to persist.

    Without the `watch_fit` limb this input closes the predecessor and
    appends a baseline while opening no successor, which is the same defect
    `test_a_confirmed_change_whose_successor_cannot_be_fitted_writes_nothing`
    pins on the verdict-derived path.
    """
    stored = glue.empty_calibration_state()
    analysis = _permitted_analysis(glue, watch=_withheld(glue))
    assert glue.reduce_state(stored, analysis, _mode(glue)) is stored


def test_688_a_diagnostic_run_still_writes_nothing_on_the_permitted_shape(
        glue):
    stored = glue.empty_calibration_state()
    assert glue.reduce_state(stored, _permitted_analysis(glue),
                             _mode(glue, kind="diagnostic")) is stored


def test_688_a_foreign_analysis_object_refuses_rather_than_raising(glue):
    """`persist_and_detect` is public glue and its `analysis` argument is not
    type-checked, so the permit predicate has to be total over any input."""
    assert glue.qm.transition_persistence_permitted(object()) is False


# --------------------------------------------------------------------------
# #691 — the admission condition has exactly one spelling
# --------------------------------------------------------------------------
def test_691_the_admission_condition_has_exactly_one_spelling(glue):
    """Both call sites must consult the same helper.

    This is a STRUCTURAL assertion, not a behavioural one, and deliberately
    so. The two conditions cannot disagree today:
    `transition_persistence_permitted` requires a WITHHELD verdict under a
    blocking status, which is mutually exclusive with the `fitted_ok` limb
    that is the only other way `reduce_state` reaches a save — so the stamp
    site is unreachable with the permit true and the successor fit
    unavailable. The test fails when a future edit widens one site and not
    the other, the direction of that divergence being `permitting`, which is
    the harmful one on this axis.

    The assertion is that no site OUTSIDE the helper calls the bare permit
    predicate. It is deliberately not "`watch_fit.state` appears nowhere":
    `reduce_state` also spells that limb in `successor_ok`, which is the
    ORDINARY confirmed-change path and has nothing to do with #688 admission.
    Asserting its absence would demand deleting a correct, unrelated
    condition.
    """
    import inspect

    src_reduce = inspect.getsource(glue.reduce_state)
    # #1015: the descriptor's disclosure is stamped in one helper, which both
    # `persist_and_detect` and the reducer's transition lineage go through.
    src_stamp = inspect.getsource(glue._stamp_disclosure)
    for name, src in (("reduce_state", src_reduce),
                      ("_stamp_disclosure", src_stamp)):
        assert "_transition_persistence_admitted" in src, (
            f"{name} does not consult the shared admission helper")
        assert "qm.transition_persistence_permitted(" not in src, (
            f"{name} still calls the bare permit predicate, which is the "
            "admission condition with its successor-fit limb missing")
    helper = inspect.getsource(glue._transition_persistence_admitted)
    assert "qm.transition_persistence_permitted(" in helper, (
        "the helper does not consult the permit predicate")
    assert 'watch_fit.state == "available"' in helper, (
        "the helper does not carry the complete conjunction")


def test_691_the_helper_is_the_conjunction_it_claims_to_be(glue):
    """The structural test above cannot see a helper whose text is right and
    whose value is wrong, so the truth table is pinned beside it."""
    permitted = _permitted_analysis(glue)
    assert glue.qm.transition_persistence_permitted(permitted) is True
    assert permitted.watch_fit.state == "available"
    assert glue._transition_persistence_admitted(permitted) is True

    withheld_successor = _permitted_analysis(glue, watch=_withheld(glue))
    assert glue.qm.transition_persistence_permitted(
        withheld_successor) is True, (
        "the fixture no longer isolates the successor-fit limb")
    assert glue._transition_persistence_admitted(withheld_successor) is False

    assert glue._transition_persistence_admitted(object()) is False, (
        "the helper must stay total over any input, as the predicate it "
        "wraps is")


# --------------------------------------------------------------------------
# #978 spec 4.5 — an era change rolls the calibration over; it is never a
# metering-rate transition.
# --------------------------------------------------------------------------
LEGACY_FINGERPRINT = (
    "0cd932a7f8caa5d9bec7010ed5d800935e5c78332ae060c1142650943512a24a")
NEW_ERA_NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
NEW_ERA_START = dt.datetime(2026, 9, 22, tzinfo=UTC)


def _open_prior(*, fingerprint, era=None, value=2_400_000.0):
    regime = {
        "effectiveFrom": "2026-07-25T00:00:00+00:00",
        "effectiveUntil": None, "fingerprint": fingerprint,
        "algorithmRevision": 3, "unitsPerPoint": value,
        "interval": {"lo": value * 0.95, "hi": value * 1.05},
        "support": {"days": 30, "segments": 4}, "status": "ok",
        "asOf": "2026-09-01T00:00:00+00:00", "qualifications": []}
    if era is not None:
        regime["coefficientEra"] = era
    return regime


def _state(*regimes, key="*"):
    return {"schemaVersion": 1,
            "accounts": {key: {"regimes": [dict(r) for r in regimes]}}}


def _new_era_mode(glue, account_key=None):
    return _mode(glue, account_key=account_key, coefficient_era="opus-5-5",
                 now=NEW_ERA_NOW, regime_start=NEW_ERA_START)


def _assert_rolled_over(glue, regimes, prior_value):
    mrc = glue._cctally()._load_sibling("_lib_meter_rate_change")
    assert len(regimes) == 2, regimes
    assert regimes[0]["unitsPerPoint"] == prior_value
    assert regimes[0]["status"] == "stale"
    assert regimes[0]["effectiveUntil"] == NEW_ERA_NOW.isoformat()
    assert regimes[1]["coefficientEra"] == "opus-5-5"
    assert regimes[1]["effectiveUntil"] is None
    assert regimes[1]["unitsPerPoint"] == 1_000_000.0
    assert mrc.enumerate_transitions(
        regimes, provider="claude", account_key="unattributed",
        detected_at=NEW_ERA_NOW.isoformat()) == ()
    assert mrc.active_rate_change(regimes) is None


@pytest.mark.parametrize("prior", [
    _open_prior(fingerprint=LEGACY_FINGERPRINT),
    None,  # the current fingerprint, under the other era (set below)
], ids=["legacy-fingerprint", "same-fingerprint-other-era"])
def test_legacy_fingerprint_prior_rolls_over_without_transition(glue, prior):
    """RED test 4. Both an old-build prior (no `coefficientEra`) and a
    current-build `opus-5` prior roll over on a trustworthy new-era fit: the
    old regime is closed as stale at the clock and one `opus-5-5` regime is
    appended, with no rate transition, no recovery candidate and no marker."""
    if prior is None:
        prior = _open_prior(
            fingerprint=glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
            era="opus-5")
    state = _state(prior)
    _assert_rolled_over(
        glue, _regimes(glue.reduce_state(
            state, _analysis(glue, fitted=_evidence(glue, 1_000_000.0)),
            _new_era_mode(glue)), glue), 2_400_000.0)
    assert state["accounts"]["*"]["regimes"][0]["status"] == "ok", \
        "the reducer is pure"

    glue.save_calibrations(state)
    _quarantined, fresh, candidates = glue.persist_and_detect(
        _analysis(glue, fitted=_evidence(glue, 1_000_000.0)),
        _new_era_mode(glue))
    assert fresh == ()
    assert candidates == ()
    stored = glue.load_calibrations(now=NEW_ERA_NOW).state
    _assert_rolled_over(glue, _regimes(stored, glue), 2_400_000.0)


def test_same_fingerprint_other_era_prior_rolls_over(glue):
    """The fingerprint matches, so before #978 this regime would have been
    updated IN PLACE with an Opus 5.5 budget under its Opus 5 history."""
    prior = _open_prior(fingerprint=glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
                        era="opus-5")
    state = glue.reduce_state(
        _state(prior), _analysis(glue, fitted=_evidence(glue, 1_000_000.0)),
        _new_era_mode(glue))
    _assert_rolled_over(glue, _regimes(state, glue), 2_400_000.0)


def test_a_same_era_prior_still_updates_in_place(glue):
    """The control: under a matching fingerprint AND era nothing rolls over."""
    prior = _open_prior(fingerprint=glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
                        era="opus-5-5")
    regimes = _regimes(glue.reduce_state(
        _state(prior), _analysis(glue, fitted=_evidence(glue, 1_000_000.0)),
        _new_era_mode(glue)), glue)
    assert len(regimes) == 1
    assert regimes[0]["unitsPerPoint"] == 1_000_000.0
    assert regimes[0]["coefficientEra"] == "opus-5-5"


def test_every_written_regime_names_its_era(glue):
    regimes = _regimes(glue.reduce_state(
        glue.empty_calibration_state(), _analysis(glue), _mode(glue)), glue)
    assert [r["coefficientEra"] for r in regimes] == ["opus-5"]


def test_two_accounts_rollover_is_order_independent(glue):
    a = _open_prior(fingerprint=LEGACY_FINGERPRINT, value=2_400_000.0)
    b = _open_prior(fingerprint=glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
                    era="opus-5", value=2_200_000.0)
    fit = _analysis(glue, fitted=_evidence(glue, 1_000_000.0))

    def run(order):
        state = {"schemaVersion": 1, "accounts": {}}
        for key, regime in order:
            state["accounts"][key] = {"regimes": [dict(regime)]}
        for key in ("a", "b"):
            state = glue.reduce_state(state, fit,
                                      _new_era_mode(glue, account_key=key))
        return {key: json.dumps(_regimes(state, glue, key), sort_keys=True)
                for key in ("a", "b")}

    forward = run([("a", a), ("b", b)])
    reverse = run([("b", b), ("a", a)])
    assert forward == reverse
    for key, prior_value in (("a", 2_400_000.0), ("b", 2_200_000.0)):
        regimes = json.loads(forward[key])
        assert [r["status"] for r in regimes] == ["stale", "ok"]
        assert regimes[0]["unitsPerPoint"] == prior_value


def test_recovery_and_active_marker_ignore_era_rollover(glue):
    prior = _open_prior(fingerprint=glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
                        era="opus-5")
    glue.save_calibrations(_state(prior))
    glue.persist_and_detect(
        _analysis(glue, fitted=_evidence(glue, 1_000_000.0)),
        _new_era_mode(glue))
    stored = glue.load_calibrations(now=NEW_ERA_NOW).state
    assert glue.recovery_candidates(
        stored, None, detected_at=NEW_ERA_NOW.isoformat()) == ()
    delivery = glue.seed_delivery_state()
    assert glue._owed_rate_change_transitions(
        ["unattributed"], delivery, now=NEW_ERA_NOW) == ()
    mrc = glue._cctally()._load_sibling("_lib_meter_rate_change")
    assert mrc.active_rate_change(_regimes(stored, glue)) is None


def test_historical_clock_never_overwrites_later_era(glue):
    """A `CCTALLY_AS_OF` inside the Opus 5 era against a stored `opus-5-5`
    calibration persists nothing, however trustworthy its own fit."""
    stored = _state(_open_prior(
        fingerprint=glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT, era="opus-5-5",
        value=1_000_000.0))
    assert glue.reduce_state(
        stored, _analysis(glue, fitted=_evidence(glue, 2_400_000.0)),
        _mode(glue, coefficient_era="opus-5")) is stored


def test_a_run_with_no_selected_era_persists_nothing(glue):
    stored = glue.empty_calibration_state()
    assert glue.reduce_state(stored, _analysis(glue),
                             _mode(glue, coefficient_era=None)) is stored


@pytest.mark.parametrize("recorded, era_id, stale", [
    ({"fingerprint": "current", "coefficientEra": "opus-5-5"}, "opus-5-5",
     False),
    ({"fingerprint": "current", "coefficientEra": "opus-5"}, "opus-5-5",
     True),
    ({"fingerprint": "old"}, "opus-5-5", True),
    (None, "opus-5-5", False),
])
def test_a_prior_from_another_era_is_reported_stale(glue, recorded, era_id,
                                                    stale):
    """Spec 4.3's status table: a stored prior under other constants or
    another era is the `stale` overlay, as today."""
    if recorded is not None and recorded["fingerprint"] == "current":
        recorded = dict(recorded,
                        fingerprint=glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT)
    era = glue.qm.era_by_id(era_id)
    assert glue.recorded_prior_is_stale(recorded, era) is stale


# --------------------------------------------------------------------------
# #991 spec 4.5 and section 5 test 4: each regime stores the fit-weighted
# family composition of the fit it records, and a predecessor never takes on
# its successor's blend.
# --------------------------------------------------------------------------
_BASELINE_Q = {"claude-opus-5": 0.97, "claude-sonnet-5": 0.03}
_SUCCESSOR_Q = {"claude-opus-5": 0.9, "claude-sonnet-5": 0.1}
_PRIOR_Q = {"claude-opus-5": 0.99, "claude-sonnet-5": 0.01}


def _split_analysis(glue, *, successor_q=_SUCCESSOR_Q, baseline_q=_BASELINE_Q,
                    watch_value=1_700_000.0):
    return _analysis(
        glue, status=glue.qm.CalibrationStatus.INSUFFICIENT_HISTORY,
        verdict=glue.qm.Verdict.RATE_CHANGE_DETECTED, exit_code=1,
        fitted=_withheld(glue),
        baseline=_evidence(glue, 2_050_000.0, days=26),
        watch=_evidence(glue, watch_value, days=3),
        detector=_detector(glue, split=dt.date(2026, 8, 25), qualified=True),
        fit_family_shares=successor_q, baseline_fit_family_shares=baseline_q)


def test_991_a_split_run_stores_each_regimes_own_fit_family_shares(glue):
    """The baseline regime records the baseline q, the successor its own.

    A successor blend written onto the baseline would let a closed regime
    admit consumer populations its own fit never saw.
    """
    state = glue.reduce_state(glue.empty_calibration_state(),
                              _split_analysis(glue), _mode(glue))
    baseline, successor = _regimes(state, glue)
    assert baseline["effectiveUntil"] is not None
    assert baseline["fitFamilyShares"] == _BASELINE_Q
    assert successor["effectiveUntil"] is None
    assert successor["fitFamilyShares"] == _SUCCESSOR_Q


def test_991_a_no_split_fit_stores_its_fit_family_shares_or_null(glue):
    state = glue.reduce_state(
        glue.empty_calibration_state(),
        _analysis(glue, fit_family_shares=_SUCCESSOR_Q), _mode(glue))
    assert _regimes(state, glue)[0]["fitFamilyShares"] == _SUCCESSOR_Q
    state = glue.reduce_state(glue.empty_calibration_state(),
                              _analysis(glue), _mode(glue))
    assert _regimes(state, glue)[0]["fitFamilyShares"] is None


def test_991_a_later_run_never_rewrites_the_predecessors_fit_family_shares(
        glue):
    """The predecessor closed at a split keeps what IT stored, across the
    run that closes it and every run that re-discovers the same split."""
    first = glue.reduce_state(
        glue.empty_calibration_state(),
        _analysis(glue, fit_family_shares=_PRIOR_Q), _mode(glue))
    closed = glue.reduce_state(first, _split_analysis(glue), _mode(glue))
    predecessor, successor = _regimes(closed, glue)
    assert predecessor["fitFamilyShares"] == _PRIOR_Q
    assert successor["fitFamilyShares"] == _SUCCESSOR_Q
    moved = {"claude-opus-5": 0.85, "claude-sonnet-5": 0.15}
    again = glue.reduce_state(
        closed, _split_analysis(glue, successor_q=moved,
                                watch_value=1_710_000.0), _mode(glue))
    predecessor, successor = _regimes(again, glue)
    assert len(_regimes(again, glue)) == 2
    assert predecessor["fitFamilyShares"] == _PRIOR_Q
    assert successor["fitFamilyShares"] == moved


#: The fingerprint every regime written before #991 carries.
PRE_991_FINGERPRINT = (
    "5835b430978b2fc556c577d44c9e62d5d7807e62533b32819ed63d37e3af671a")


def test_991_a_pre_991_prior_rolls_over_to_stale_without_a_transition(glue):
    """PRESERVATION. The #991 fingerprint change is a constants rollover,
    never a metering-rate transition: the open prior is closed as stale and
    the next fit is appended, exactly as the #978 rollover did."""
    prior = _open_prior(fingerprint=PRE_991_FINGERPRINT, era="opus-5-5")
    state = glue.reduce_state(
        _state(prior), _analysis(glue, fitted=_evidence(glue, 1_000_000.0)),
        _new_era_mode(glue))
    _assert_rolled_over(glue, _regimes(state, glue), 2_400_000.0)


# --------------------------------------------------------------------------
# #1015 T4 — the lineage kernel (spec 5.2, 5.3). Each case names the failure
# it detects: a fit published across a correction to its inputs that keeps
# steering the forecast (accuracy), or a refusal that fires for a correction
# that cannot have changed the fit (a needless withheld forecast).
# --------------------------------------------------------------------------
_SEG = "observations-2026-10.jsonl"
_A = dt.datetime(2026, 10, 1, tzinfo=UTC)
_S = dt.datetime(2026, 10, 10, tzinfo=UTC)
_T = dt.datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _lin():
    import _lib_quota_lineage as L
    return L


def _facts(rows, batches):
    L = _lin()
    corrections = tuple(
        L.CorrectionRow(op, obs, disp, acct, at, (_SEG, op_off))
        for op, obs, disp, acct, at, op_off in rows)
    facts = tuple(L.BatchFact(bid, (_SEG, off), frozenset(fp))
                  for bid, off, fp in batches)
    return L.CorrectionFacts(corrections, facts,
                             frozenset(b.batch_id for b in facts))


def _lineage(kind="nosplit", *, scope="A", basis=()):
    L = _lin()
    windows = L.dependency_windows(kind, a=_A, s=_S, t=_T)
    return {"basis": list(basis), "accountScope": scope,
            "dependencyWindows": list(windows)}


@pytest.mark.parametrize("captured,kind,state", [
    # A correction exactly at S belongs to the successor, not the baseline.
    (_S, "successor", "tainted"),
    (_S, "baseline", "clean"),
    # One instant before S is the successor's pre-split dependency.
    (_S - dt.timedelta(seconds=1), "baseline", "tainted"),
    (_S - dt.timedelta(seconds=1), "successor", "tainted"),
    # A correction exactly at T is included; one after it is not.
    (_T, "nosplit", "tainted"),
    (_T + dt.timedelta(seconds=1), "nosplit", "clean"),
    (_A - dt.timedelta(seconds=1), "nosplit", "clean"),
])
def test_1015_window_endpoints_at_s_and_t(captured, kind, state):
    L = _lin()
    facts = _facts([("o:1", "obs1", "hold", "A", captured.isoformat(), 10)],
                   [("b:1", 20, {("claude", "A")})])
    assert L.assess(_lineage(kind), facts).state == state


def test_1015_scope_merged_and_account_precise():
    L = _lin()
    facts = _facts([("o:1", "obs1", "retract", "B", _S.isoformat(), 10)],
                   [("b:1", 20, {("claude", "B")})])
    # A stamped fit for A ignores B's correction; a merged fit does not.
    assert L.assess(_lineage(scope="A"), facts).state == "clean"
    assert L.assess(_lineage(scope="*"), facts) == L.Assessment(
        "tainted", (("o:1", 1),))


def test_1015_batch_footprint_basis_and_order_decide_materialization():
    L = _lin()
    row = [("o:1", "obs1", "hold", "A", _S.isoformat(), 10)]
    # Another account's batch, a Codex-only batch and an empty batch never
    # invalidate.
    for fp in ({("claude", "B")}, {("codex", "A")}, set()):
        assert L.assess(_lineage(), _facts(row, [("b", 20, fp)])).state \
            == "clean", fp
    # An unknowable owner and an account-blind Claude batch do.
    for fp in ({("*", "*")}, {("claude", "*")}):
        assert L.assess(_lineage(), _facts(row, [("b", 20, fp)])).state \
            == "tainted", fp
    # A batch committed before the op cannot materialize it.
    assert L.assess(_lineage(), _facts(
        row, [("b", 5, {("claude", "A")})])).state == "clean"
    # A batch already in the fit's basis is reflected in its inputs.
    assert L.assess(_lineage(basis=["b"]), _facts(
        row, [("b", 20, {("claude", "A")})])).state == "clean"
    # The basis holding every batch is clean without a probe, whatever the
    # corrections say.
    assert L.assess(_lineage(basis=["b"]), _facts(
        [("o:1", "x", "hold", None, None, 10)],
        [("b", 20, {("*", "*")})])).state == "clean"


def test_1015_an_accept_is_a_correction():
    L = _lin()
    facts = _facts([("o:1", "obs1", "accept", "A", _S.isoformat(), 10)],
                   [("b:1", 20, {("claude", "A")})])
    assert L.assess(_lineage(), facts).state == "tainted"


def test_1015_an_unresolved_reference_is_unavailable_never_clean():
    L = _lin()
    facts = _facts([("o:1", "obs1", "hold", None, None, 10)],
                   [("b:1", 20, {("claude", "B")})])
    assert L.assess(_lineage(), facts).state == "unavailable"
    # No later batch: it materializes nothing yet.
    assert L.assess(_lineage(), _facts(
        [("o:1", "obs1", "hold", None, None, 30)],
        [("b:1", 20, {("claude", "B")})])).state == "clean"
    assert L.assess(_lineage(), None).state == "unavailable"


def _fitted_regime():
    return {"effectiveFrom": _A.isoformat(), "effectiveUntil": None,
            "fingerprint": "f", "algorithmRevision": 3,
            "coefficientEra": "opus-5-5", "unitsPerPoint": 100_000.0,
            "interval": {"lo": 95_000.0, "hi": 105_000.0},
            "support": {"days": 12, "segments": 2}, "status": "ok",
            "asOf": _T.isoformat(), "qualifications": [],
            "familyShares": {"claude-opus-5-5": 1.0}}


def test_1015_digest_excludes_lifecycle_fields_and_binds_as_of():
    L = _lin()
    regime = _fitted_regime()
    regime["fitLineage"] = L.fit_lineage(
        scope="A", basis=["b:1"], windows=L.dependency_windows(
            "nosplit", a=_A, t=_T), estimator=None, regime=regime)
    assert L.fit_lineage_verifies(regime)
    # Closing, stale-marking, the annotations and the writer stamps are not
    # fits, so the lineage still verifies after each.
    lifecycle = dict(regime, effectiveUntil=_S.isoformat(), status="stale",
                     supersededByCorrection={"fitId": "x"},
                     recalibrationAttempt={"status": "s"},
                     transitionLineage={"digest": "d"}, wpg=3, wpgDigest="z",
                     wpr=2, wprDigest="z", wpx=3, wpxDigest="z")
    assert L.fit_lineage_verifies(lifecycle)
    # A previous release's in-place update writes a new `asOf` even with
    # identical coefficients: the digest no longer verifies, so the fit is
    # assessed as legacy over [era start, new asOf].
    later = dict(regime, asOf=(_T + dt.timedelta(days=2)).isoformat())
    assert not L.fit_lineage_verifies(later)
    era_start = dt.datetime(2026, 9, 22, tzinfo=UTC)
    legacy = L.regime_lineage(later, scope="A", era_start=era_start)
    assert legacy["legacy"] is True and legacy["basis"] == []
    assert legacy["dependencyWindows"] == [L.window(
        era_start, later["asOf"], until_inclusive=True)]
    for changed in ({"unitsPerPoint": 1.0}, {"effectiveFrom": "x"},
                    {"fingerprint": "g"}):
        assert not L.fit_lineage_verifies(dict(regime, **changed)), changed


def test_1015_transition_lineage_digest_is_frozen():
    L = _lin()
    tl = L.transition_lineage(
        windows=L.dependency_windows("successor", a=_A, s=_S, t=_T),
        scope="A", basis=["b:1"], descriptor={"new_units_per_point": 2.0})
    assert L.transition_lineage_verifies(tl)
    edited = dict(tl, descriptor={"new_units_per_point": 3.0})
    assert not L.transition_lineage_verifies(edited)
    legacy = L.legacy_transition_lineage(
        account_key="unattributed", detected_at=_T.isoformat())
    assert legacy["accountScope"] == "*"
    assert legacy["dependencyWindows"][0]["from"] is None


# --------------------------------------------------------------------------
# #1015 T6 — the sweep assesses the locked state: annotation, attempt,
# archive, pairing over clean regimes only (spec 5.5, 5.6)
# --------------------------------------------------------------------------
_IN_SUCCESSOR = "2026-08-26T09:00:00+00:00"   # inside [SPLIT, NOW]
_IN_BASELINE = "2026-08-01T09:00:00+00:00"    # inside [REGIME_START, SPLIT)


def _no_facts():
    return _facts((), ())


def _tainting(captured=_IN_SUCCESSOR, account="acct-a"):
    """One retract of one observation, then its completed batch."""
    return _facts(
        [("op-1", "obs-1", "retract", account, captured, 100)],
        [("batch-1", 200, {("claude", account)})])


def _lmode(glue, facts, **kw):
    import dataclasses
    return dataclasses.replace(_mode(glue, **kw), correction_facts=facts,
                               correction_basis=facts.basis)


def _split_change(glue, *, baseline=2_050_000.0, watch=1_700_000.0):
    qm = glue.qm
    return _analysis(
        glue, status=qm.CalibrationStatus.INSUFFICIENT_HISTORY,
        verdict=qm.Verdict.RATE_CHANGE_DETECTED, exit_code=1,
        fitted=_withheld(glue),
        baseline=(_evidence(glue, baseline, days=26) if baseline is not None
                  else _withheld(glue)),
        watch=_evidence(glue, watch, days=3),
        detector=_detector(glue, split=SPLIT, qualified=True))


def _withheld_analysis(glue):
    return _analysis(glue, status=glue.qm.CalibrationStatus.INSUFFICIENT_HISTORY,
                     fitted=_withheld(glue))


def _assess(glue, regime, facts, scope="*"):
    import _cctally_quota_calibration as qcg
    return qcg.assess_regime(regime, facts, scope=scope).state


def test_1015_every_fit_carries_its_lineage_and_a_closed_one_keeps_it(glue):
    """Detects a fit published with no lineage, or with the wrong windows:
    the predicate could then never see the corrections its inputs depend on
    (spec 5.2)."""
    L = _lin()
    first = glue.reduce_state(glue.empty_calibration_state(), _analysis(glue),
                              _lmode(glue, _no_facts()))
    [nosplit] = _regimes(first, glue)
    assert L.fit_lineage_verifies(nosplit)
    assert nosplit["fitLineage"]["dependencyWindows"] == list(
        L.dependency_windows("nosplit", a=REGIME_START, t=NOW))
    assert nosplit["fitLineage"]["accountScope"] == "*"
    split = glue.reduce_state(first, _split_change(glue),
                              _lmode(glue, _no_facts()))
    closed, successor = _regimes(split, glue)
    # Closing is not a fit: the predecessor keeps its lineage byte for byte.
    assert closed["fitLineage"] == nosplit["fitLineage"]
    assert L.fit_lineage_verifies(closed)
    assert successor["fitLineage"]["dependencyWindows"] == list(
        L.dependency_windows("successor", a=REGIME_START, s=SPLIT_ISO, t=NOW))
    cold = glue.reduce_state(glue.empty_calibration_state(),
                             _split_change(glue), _lmode(glue, _no_facts()))
    baseline, _succ = _regimes(cold, glue)
    assert baseline["fitLineage"]["dependencyWindows"] == list(
        L.dependency_windows("baseline", a=REGIME_START, s=SPLIT_ISO))


def test_1015_a_withheld_sweep_annotates_and_records_the_attempt(glue):
    """Detects A2: a withheld sweep that leaves a tainted fit unannotated
    and records no attempt, so the refusal could never state the status."""
    stored = glue.reduce_state(glue.empty_calibration_state(),
                               _analysis(glue), _lmode(glue, _no_facts()))
    facts = _tainting()
    [before] = _regimes(stored, glue)
    assert _assess(glue, before, facts) == "tainted"
    later = NOW + dt.timedelta(days=1)
    reduced = glue.reduce_state(stored, _withheld_analysis(glue),
                                _lmode(glue, facts, now=later))
    [after] = _regimes(reduced, glue)
    assert after["unitsPerPoint"] == before["unitsPerPoint"]
    assert after["fitLineage"] == before["fitLineage"]
    assert after["supersededByCorrection"] == {
        "fitId": before["fitLineage"]["fitId"],
        "corrections": [{"opId": "op-1", "observations": 1}],
        "assessedAt": later.isoformat()}
    assert after["recalibrationAttempt"] == {
        "status": "insufficient-history", "basis": ["batch-1"],
        "asOf": later.isoformat()}
    # The annotation restamps the writer provenance without a new fitId.
    stamped = glue.stamp_current_writes(stored, reduced, None)
    [restamped] = _regimes(stamped, glue)
    assert glue.regime_writer_is_current(restamped)
    assert restamped["fitLineage"]["fitId"] == before["fitLineage"]["fitId"]
    # A second withheld sweep over the same basis owes nothing.
    again = glue.reduce_state(
        reduced, _withheld_analysis(glue),
        _lmode(glue, facts, now=later + dt.timedelta(hours=1)))
    assert again is reduced


def test_1015_nothing_owed_keeps_the_stored_identity(glue):
    """Detects a sweep that rewrites the sidecar on every run although no
    correction touches the fit (the common install, spec 5.8)."""
    stored = glue.reduce_state(glue.empty_calibration_state(),
                               _analysis(glue), _lmode(glue, _no_facts()))
    other = _facts([("op-1", "obs-1", "retract", "acct-b", _IN_SUCCESSOR, 1)],
                   [("batch-1", 2, {("codex", "acct-b")})])
    for facts in (_no_facts(), other):
        assert glue.reduce_state(stored, _withheld_analysis(glue),
                                 _lmode(glue, facts)) is stored


def test_1015_a_trustworthy_refit_archives_and_preserves_the_bucket(glue):
    """Detects A3: a refit over a tainted open regime that loses the old fit,
    its transition lineage, or another bucket key."""
    split = glue.reduce_state(glue.empty_calibration_state(),
                              _split_change(glue), _lmode(glue, _no_facts()))
    split["accounts"]["*"]["futureKey"] = {"kept": True}
    facts = _tainting()
    annotated = glue.reduce_state(split, _withheld_analysis(glue),
                                  _lmode(glue, facts))
    tainted = _regimes(annotated, glue)[1]
    frozen = tainted["transitionLineage"]
    later = NOW + dt.timedelta(days=2)
    refit = glue.reduce_state(
        annotated, _analysis(glue, fitted=_evidence(glue, 1_400_000.0)),
        _lmode(glue, facts, now=later))
    bucket = refit["accounts"]["*"]
    assert bucket["futureKey"] == {"kept": True}
    assert bucket["archivedFits"] == [
        {"regime": tainted, "archivedAt": later.isoformat()}]
    closed, fresh = _regimes(refit, glue)
    assert fresh["effectiveFrom"] == tainted["effectiveFrom"]
    assert fresh["unitsPerPoint"] == 1_400_000.0
    assert "supersededByCorrection" not in fresh
    assert "recalibrationAttempt" not in fresh
    assert fresh["transitionLineage"] == frozen
    assert fresh["fitLineage"]["basis"] == ["batch-1"]
    assert fresh["fitLineage"]["fitId"] != tainted["fitLineage"]["fitId"]
    assert _assess(glue, fresh, facts) == "clean"
    # The baseline the correction did not touch keeps its lineage (A5).
    assert _assess(glue, closed, facts) == "clean"


@pytest.mark.parametrize("builder", ["ordinary", "permitted"])
def test_1015_a_tainted_open_regime_is_never_a_predecessor(glue, builder):
    """Detects a transition derived from a rate fitted before a correction:
    the tainted regime is closed with its status unchanged and excluded from
    pairing, and this run's own clean baseline is the predecessor. The #688
    gate itself is unchanged (spec 5.5 items 4-5)."""
    first = glue.reduce_state(
        glue.empty_calibration_state(),
        _analysis(glue, fitted=_evidence(glue, 9_000_000.0)),
        _lmode(glue, _no_facts()))
    glue.save_calibrations(glue.stamp_current_writes(
        glue.empty_calibration_state(), first, None))
    facts = _tainting(captured=_IN_BASELINE)
    analysis = (_split_change(glue) if builder == "ordinary"
                else _permitted_analysis(glue))
    _q, fresh, _c = glue.persist_and_detect(analysis, _lmode(glue, facts))
    regimes = glue.stored_regimes(glue.load_calibrations(now=NOW).state, None)
    poisoned = next(r for r in regimes if r["unitsPerPoint"] == 9_000_000.0)
    assert poisoned["effectiveUntil"] == SPLIT_ISO
    assert poisoned["status"] == "ok"
    assert poisoned["supersededByCorrection"]["corrections"] == [
        {"opId": "op-1", "observations": 1}]
    assert [t.previous_units_per_point for t in fresh] == [
        analysis.baseline_fit.value]
    assert all(t.previous_units_per_point != 9_000_000.0 for t in fresh)


def test_1015_with_no_clean_baseline_no_transition_is_derived(glue):
    first = glue.reduce_state(
        glue.empty_calibration_state(),
        _analysis(glue, fitted=_evidence(glue, 9_000_000.0)),
        _lmode(glue, _no_facts()))
    glue.save_calibrations(glue.stamp_current_writes(
        glue.empty_calibration_state(), first, None))
    _q, fresh, candidates = glue.persist_and_detect(
        _split_change(glue, baseline=None),
        _lmode(glue, _tainting(captured=_IN_BASELINE)))
    assert fresh == () and candidates == ()


def test_1015_the_assessment_reads_the_state_under_the_lock(glue):
    """Detects an assessment carried from the unlocked read: a regime another
    process refitted (clean, current basis) between that read and this
    run's locked reduction must be assessed as refitted, so nothing is
    annotated and the file is not rewritten."""
    facts = _tainting()
    stale_view = glue.reduce_state(glue.empty_calibration_state(),
                                   _analysis(glue), _lmode(glue, _no_facts()))
    assert _assess(glue, _regimes(stale_view, glue)[0], facts) == "tainted"
    refitted = glue.reduce_state(
        glue.empty_calibration_state(), _analysis(glue),
        _lmode(glue, facts))
    glue.save_calibrations(glue.stamp_current_writes(
        glue.empty_calibration_state(), refitted, None))
    before = glue.calibration_path().read_bytes()
    glue.persist_and_detect(_withheld_analysis(glue), _lmode(glue, facts))
    assert glue.calibration_path().read_bytes() == before
