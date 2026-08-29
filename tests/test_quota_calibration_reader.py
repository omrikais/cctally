"""#661 S2 Task A1 — the validated, non-mutating calibration reader (spec §1.1).

`_cctally_quota_model.load_calibrations` validates only the outer envelope and
QUARANTINES a file it cannot own by renaming it aside. Every S2 consumer —
`doctor`, the status line, the dashboard — is documented read-only, so none of
them may reach that path. These tests cover the two halves of the replacement:
the pure `validate_regime` predicate in `bin/_lib_quota_calibration.py`, and
the non-mutating file read in `bin/_cctally_quota_calibration.py`.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

# conftest puts bin/ on sys.path.
import _cctally_quota_calibration as qcg
import _lib_quota_calibration as qc

UTC = dt.timezone.utc


def _good(**over):
    base = {
        "effectiveFrom": "2026-07-25T05:00:00+00:00",
        "effectiveUntil": None,
        "fingerprint": "FP",
        "algorithmRevision": 3,
        "unitsPerPoint": 2442620.0,
        "interval": {"lo": 2385463.0, "hi": 2499777.0},
        "support": {"days": 26, "segments": 4},
        "status": "ok",
        "asOf": "2026-08-28T00:00:00+00:00",
        "qualifications": [],
        "familyShares": {"claude-opus-5": 1.0},
        "classShares": {"fresh": 1.0, "output": 0.0,
                        "cache_1h": 0.0, "cache_read": 0.0},
        "familyRadius": 0.02,
        "classRadius": 0.05,
    }
    base.update(over)
    return base


def _validate(raw, **over):
    kwargs = {"account_key": None, "expected_fingerprint": "FP",
              "expected_revision": 3}
    kwargs.update(over)
    return qc.validate_regime(raw, **kwargs)


def _state(regimes, *, version=1, key="*"):
    return {"schemaVersion": version, "accounts": {key: {"regimes": regimes}}}


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# validate_regime — the pure predicate
# --------------------------------------------------------------------------
def test_a1_a_good_regime_validates_to_aware_instants():
    out = _validate(_good())
    assert isinstance(out, qc.ValidatedRegime)
    assert out.effective_from == dt.datetime(2026, 7, 25, 5, tzinfo=UTC)
    assert out.effective_until is None
    assert out.as_of == dt.datetime(2026, 8, 28, tzinfo=UTC)
    assert out.units_per_point == 2442620.0
    assert out.interval_lo == 2385463.0
    assert out.interval_hi == 2499777.0
    assert out.family_shares == {"claude-opus-5": 1.0}
    assert out.family_radius == 0.02
    assert out.qualifications == ()


def test_a1_fingerprint_mismatch_is_rejected_not_repaired():
    out = _validate(_good(fingerprint="OTHER"))
    assert out is qc.RegimeRejection.FINGERPRINT


def test_a1_algorithm_revision_mismatch_is_rejected():
    out = _validate(_good(algorithmRevision=2))
    assert out is qc.RegimeRejection.REVISION


def test_a1_non_finite_units_per_point_is_rejected():
    # NaN and Infinity both pass a naive `> 0` guard; the validator must not.
    for bad in (float("nan"), float("inf")):
        assert _validate(_good(unitsPerPoint=bad)) is \
            qc.RegimeRejection.MALFORMED, bad


def test_a1_non_positive_units_per_point_is_rejected():
    for bad in (0.0, -1.0):
        assert _validate(_good(unitsPerPoint=bad)) is \
            qc.RegimeRejection.MALFORMED, bad


def test_a1_non_finite_interval_bound_is_rejected():
    for bad in (float("nan"), float("inf")):
        assert _validate(_good(interval={"lo": bad, "hi": 2499777.0})) is \
            qc.RegimeRejection.MALFORMED, bad
        assert _validate(_good(interval={"lo": 2385463.0, "hi": bad})) is \
            qc.RegimeRejection.MALFORMED, bad


def test_a1_interval_must_bracket_the_point_estimate():
    # Wholly above the point.
    assert _validate(_good(interval={"lo": 3e6, "hi": 4e6})) is \
        qc.RegimeRejection.MALFORMED
    # Wholly below it.
    assert _validate(_good(interval={"lo": 1e6, "hi": 2e6})) is \
        qc.RegimeRejection.MALFORMED


def test_a1_an_absent_interval_is_malformed_not_a_partial_record():
    assert _validate(_good(interval=None)) is qc.RegimeRejection.MALFORMED


def test_a1_an_unbounded_upper_interval_is_accepted():
    out = _validate(_good(interval={"lo": 2385463.0, "hi": None}))
    assert isinstance(out, qc.ValidatedRegime)
    assert out.interval_hi is None


def test_a1_a_naive_instant_is_rejected():
    """The estate stores aware ISO instants. A naive one has no zone, and
    reading it as host-local is how a stored UTC stamp silently shifts."""
    assert _validate(_good(effectiveFrom="2026-07-25T05:00:00")) is \
        qc.RegimeRejection.MALFORMED
    assert _validate(_good(asOf="2026-08-28T00:00:00")) is \
        qc.RegimeRejection.MALFORMED


def test_a1_a_non_dict_regime_is_malformed():
    for bad in (None, [], "regime", 3):
        assert _validate(bad) is qc.RegimeRejection.MALFORMED, bad


def test_a1_a_non_finite_radius_is_rejected():
    assert _validate(_good(familyRadius=float("nan"))) is \
        qc.RegimeRejection.MALFORMED
    assert _validate(_good(classRadius=float("inf"))) is \
        qc.RegimeRejection.MALFORMED


def test_a1_an_absent_radius_validates_and_stays_none():
    """A radius the fit could not measure is None, not zero. Support then
    fails closed in the apply adapter rather than admitting everything."""
    out = _validate(_good(familyRadius=None, classRadius=None))
    assert isinstance(out, qc.ValidatedRegime)
    assert out.family_radius is None
    assert out.class_radius is None


def test_a1_the_account_key_travels_onto_the_record():
    out = _validate(_good(), account_key="acct-1")
    assert out.account_key == "acct-1"


# --------------------------------------------------------------------------
# read_calibration_file — the non-mutating file read
# --------------------------------------------------------------------------
def test_a1_absent_file_reports_absent_and_never_raises(tmp_path):
    read = qcg.read_calibration_file(account_key=None,
                                     path=tmp_path / "nope.json")
    assert read.regime is None
    assert read.rejection is qcg.RegimeRejection.ABSENT


def test_a1_reader_never_renames_the_file(tmp_path):
    # The mutating loader quarantines a malformed file by renaming it.
    # This reader must leave the directory byte-identical.
    p = tmp_path / "quota-calibrations.json"
    p.write_text("{ not json", encoding="utf-8")
    before = sorted(q.name for q in tmp_path.iterdir())
    read = qcg.read_calibration_file(account_key=None, path=p)
    assert read.rejection is qcg.RegimeRejection.MALFORMED
    assert read.regime is None
    assert sorted(q.name for q in tmp_path.iterdir()) == before


def test_a1_reader_never_renames_a_version_ahead_file(tmp_path):
    """`load_calibrations` quarantines this shape too. The reader reports it."""
    p = _write(tmp_path / "quota-calibrations.json",
               _state([_good()], version=99))
    before = sorted(q.name for q in tmp_path.iterdir())
    read = qcg.read_calibration_file(account_key=None, path=p)
    assert read.rejection is qcg.RegimeRejection.SCHEMA_VERSION
    assert sorted(q.name for q in tmp_path.iterdir()) == before


def test_a1_reader_returns_the_open_regime_for_the_merged_key(tmp_path):
    closed = _good(effectiveUntil="2026-08-01T00:00:00+00:00",
                   unitsPerPoint=2000000.0,
                   interval={"lo": 1900000.0, "hi": 2100000.0})
    p = _write(tmp_path / "quota-calibrations.json", _state([closed, _good()]))
    read = qcg.read_calibration_file(
        account_key=None, path=p, expected_fingerprint="FP",
        expected_revision=3)
    assert read.rejection is None
    assert read.regime.units_per_point == 2442620.0
    assert read.regime.effective_until is None


def test_a1_reader_scopes_by_account_key(tmp_path):
    mine = _good(unitsPerPoint=2442620.0)
    theirs = _good(unitsPerPoint=1000000.0,
                   interval={"lo": 900000.0, "hi": 1100000.0})
    payload = {"schemaVersion": 1,
               "accounts": {"*": {"regimes": [theirs]},
                            "acct-1": {"regimes": [mine]}}}
    p = _write(tmp_path / "quota-calibrations.json", payload)
    read = qcg.read_calibration_file(
        account_key="acct-1", path=p, expected_fingerprint="FP",
        expected_revision=3)
    assert read.regime.units_per_point == 2442620.0
    assert read.regime.account_key == "acct-1"


def test_a1_an_account_with_no_open_regime_is_absent(tmp_path):
    closed = _good(effectiveUntil="2026-08-01T00:00:00+00:00")
    p = _write(tmp_path / "quota-calibrations.json", _state([closed]))
    read = qcg.read_calibration_file(
        account_key=None, path=p, expected_fingerprint="FP",
        expected_revision=3)
    assert read.regime is None
    assert read.rejection is qcg.RegimeRejection.ABSENT


def test_a1_a_stale_open_regime_reports_the_fingerprint_cause(tmp_path):
    p = _write(tmp_path / "quota-calibrations.json",
               _state([_good(fingerprint="EARLIER")]))
    read = qcg.read_calibration_file(
        account_key=None, path=p, expected_fingerprint="FP",
        expected_revision=3)
    assert read.regime is None
    assert read.rejection is qcg.RegimeRejection.FINGERPRINT


def test_a1_an_unreadable_file_reports_unreadable(tmp_path, monkeypatch):
    p = _write(tmp_path / "quota-calibrations.json", _state([_good()]))

    def _boom(*_a, **_k):
        raise PermissionError("nope")

    monkeypatch.setattr(qcg, "_read_text", _boom)
    read = qcg.read_calibration_file(account_key=None, path=p)
    assert read.regime is None
    assert read.rejection is qcg.RegimeRejection.UNREADABLE


def test_a1_the_reader_performs_no_existence_check_before_opening(monkeypatch,
                                                                  tmp_path):
    """A concurrent quarantine rename can remove the primary name between a
    check and an open, so `Path.exists` must not be on this path at all."""
    p = tmp_path / "quota-calibrations.json"

    def _forbidden(*_a, **_k):
        raise AssertionError("read_calibration_file pre-checked existence")

    monkeypatch.setattr(type(p), "exists", _forbidden)
    read = qcg.read_calibration_file(account_key=None, path=p)
    assert read.rejection is qcg.RegimeRejection.ABSENT


def test_a1_the_reader_never_reaches_the_quarantining_loader(monkeypatch,
                                                             tmp_path):
    qmg = qcg._cctally_quota_model

    def _forbidden(*_a, **_k):
        raise AssertionError("read_calibration_file called load_calibrations")

    monkeypatch.setattr(qmg, "load_calibrations", _forbidden)
    monkeypatch.setattr(qmg, "_quarantine", _forbidden)
    p = _write(tmp_path / "quota-calibrations.json", _state([_good()]))
    qcg.read_calibration_file(account_key=None, path=p,
                             expected_fingerprint="FP", expected_revision=3)
    bad = tmp_path / "broken.json"
    bad.write_text("{ nope", encoding="utf-8")
    qcg.read_calibration_file(account_key=None, path=bad)


def test_a1_the_default_path_is_the_calibration_sidecar(monkeypatch, tmp_path):
    # Patch the module object the reader itself holds. A fresh
    # `import _cctally_quota_model` can resolve to a DIFFERENT instance,
    # because `load_script_module` evicts every `_cctally_*` sibling, and
    # patching that instance would leave the reader reading the real path.
    monkeypatch.setattr(qcg._cctally_quota_model, "calibration_path",
                        lambda: tmp_path / "cal.json")
    _write(tmp_path / "cal.json", _state([_good()]))
    read = qcg.read_calibration_file(account_key=None,
                                     expected_fingerprint="FP",
                                     expected_revision=3)
    assert read.regime is not None


def test_a1_default_expectations_come_from_the_shipped_constants(tmp_path):
    """A caller that names no fingerprint gets THIS binary's, so a stored
    regime from other constants is rejected rather than silently applied."""
    import _lib_quota_model as qm

    p = _write(tmp_path / "quota-calibrations.json", _state([_good(
        fingerprint=qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
        algorithmRevision=qm.QUOTA_MODEL_ALGORITHM_REVISION)]))
    assert qcg.read_calibration_file(
        account_key=None, path=p).regime is not None
    stale = _write(tmp_path / "stale.json", _state([_good()]))
    assert qcg.read_calibration_file(account_key=None, path=stale).rejection \
        is qcg.RegimeRejection.FINGERPRINT


@pytest.mark.parametrize("payload", [
    [], "state", 3, {"schemaVersion": 1},
    {"schemaVersion": 1, "accounts": []},
])
def test_a1_a_malformed_envelope_is_reported_not_repaired(tmp_path, payload):
    p = _write(tmp_path / "quota-calibrations.json", payload)
    read = qcg.read_calibration_file(account_key=None, path=p)
    assert read.regime is None
    assert read.rejection in {qcg.RegimeRejection.MALFORMED,
                              qcg.RegimeRejection.SCHEMA_VERSION}


def test_a1_a_non_integer_schema_version_is_a_schema_version_rejection(
        tmp_path):
    p = _write(tmp_path / "quota-calibrations.json",
               {"schemaVersion": "1", "accounts": {}})
    assert qcg.read_calibration_file(account_key=None, path=p).rejection \
        is qcg.RegimeRejection.SCHEMA_VERSION


# --------------------------------------------------------------------------
# The prediction gate S1 already computed (spec §1.1, R1)
# --------------------------------------------------------------------------
# S1 fits a successor regime as soon as it detects a rate change and marks it
# `detection-only` while that fit stays below its prediction gate. That mark is
# S1 stating the regime must not be used to predict, so the validated reader
# refuses it with its own typed cause. Support re-testing does not subsume the
# gate: a three-day fit sits well inside its own recorded radii, because a
# narrow population produces narrow radii.
def test_a1_a_detection_only_regime_is_refused_for_projection():
    out = _validate(_good(status="insufficient-history",
                          qualifications=["successor-regime",
                                          "detection-only"]))
    assert out is qc.RegimeRejection.DETECTION_ONLY


def test_a1_the_detection_only_refusal_is_not_a_shape_complaint():
    """The regime is structurally perfect; only its qualification refuses it.

    A `MALFORMED` here would tell the user to repair a file that is fine.
    """
    raw = _good(qualifications=["successor-regime", "detection-only"])
    assert isinstance(_validate(_good(qualifications=["successor-regime"])),
                      qc.ValidatedRegime)
    assert _validate(raw) is qc.RegimeRejection.DETECTION_ONLY


def test_a1_a_detection_grade_regime_stays_readable_for_detection():
    """Section 6's rate-change alert reads the same regime pair deliberately,
    because detection is exactly what a detection-grade fit is for. The gate
    makes the regime unusable for PROJECTION, not unreadable."""
    raw = _good(qualifications=["successor-regime", "detection-only"])
    out = _validate(raw, require_prediction_ready=False)
    assert isinstance(out, qc.ValidatedRegime)
    assert out.qualifications == ("successor-regime", "detection-only")


def test_a1_identity_checks_still_outrank_the_prediction_gate():
    """A regime written under other constants is reported as a fingerprint
    mismatch even when it is also detection-only — that is the cause a user
    can act on."""
    raw = _good(fingerprint="OTHER",
                qualifications=["successor-regime", "detection-only"])
    assert _validate(raw) is qc.RegimeRejection.FINGERPRINT


def test_a1_the_reader_refuses_a_detection_only_open_regime(tmp_path):
    p = _write(tmp_path / "quota-calibrations.json", _state([
        _good(qualifications=["successor-regime", "detection-only"],
              status="insufficient-history"),
    ]))
    read = qcg.read_calibration_file(
        account_key=None, path=p, expected_fingerprint="FP",
        expected_revision=3)
    assert read.regime is None
    assert read.rejection is qcg.RegimeRejection.DETECTION_ONLY


def test_a1_the_reader_reports_the_refused_regimes_own_status(tmp_path):
    """The regime says WHY S1 held it below the gate, and a surface that
    states `insufficient-history` when the store says `unstable-fit` is
    naming a cause the user cannot reconcile with `cctally quota`."""
    p = _write(tmp_path / "quota-calibrations.json", _state([
        _good(qualifications=["detection-only"], status="unstable-fit"),
    ]))
    read = qcg.read_calibration_file(
        account_key=None, path=p, expected_fingerprint="FP",
        expected_revision=3)
    assert read.rejection is qcg.RegimeRejection.DETECTION_ONLY
    assert read.regime_status == "unstable-fit"


def test_a1_regime_status_is_none_when_nothing_was_refused(tmp_path):
    p = _write(tmp_path / "quota-calibrations.json", _state([
        _good(),
    ]))
    read = qcg.read_calibration_file(
        account_key=None, path=p, expected_fingerprint="FP",
        expected_revision=3)
    assert read.regime is not None
    assert read.regime_status is None


def test_a1_the_reader_can_be_asked_for_the_detection_grade_regime(tmp_path):
    p = _write(tmp_path / "quota-calibrations.json", _state([
        _good(qualifications=["successor-regime", "detection-only"]),
    ]))
    read = qcg.read_calibration_file(
        account_key=None, path=p, expected_fingerprint="FP",
        expected_revision=3, require_prediction_ready=False)
    assert read.rejection is None
    assert read.regime is not None
    assert "detection-only" in read.regime.qualifications
