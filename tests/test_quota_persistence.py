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
              detector_input_causes=None, composition_provenance=None):
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
                                else composition_provenance))


def _mode(glue, kind="automatic", account_key=None):
    return glue.PersistMode(
        kind=kind, account_key=account_key, now=NOW,
        fingerprint=glue.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
        regime_start=REGIME_START)


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
    assert all(c.withholding_status is None for c in candidates), (
        "the enumeration must not carry the disclosure; it sees only stored "
        "regimes")


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
