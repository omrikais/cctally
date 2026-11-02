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


@pytest.fixture(autouse=True)
def _published_stats_index(request):
    """#1015: a regime that validates is assessed against the published
    correction facts, so every case runs over a scratch home holding a
    current stats index with no corrections (the `stats_home` fixture
    below). A case that needs no index never reaches it."""
    request.getfixturevalue("stats_home")


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
        "coefficientEra": "opus-5",
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


def test_unknown_era_id_is_malformed():
    # #978 spec 4.5: under the current fingerprint a regime must name a known
    # coefficient era. The fingerprint is checked first, so a regime from an
    # older build is still reported as a fingerprint mismatch.
    assert _validate(_good()).coefficient_era == "opus-5"
    assert _validate(_good(coefficientEra="opus-5-5")).coefficient_era == \
        "opus-5-5"
    missing = _good()
    del missing["coefficientEra"]
    assert _validate(missing) is qc.RegimeRejection.MALFORMED
    for bad in (None, "opus-6", 5, True, ""):
        assert _validate(_good(coefficientEra=bad)) is \
            qc.RegimeRejection.MALFORMED, bad
    legacy = _good(fingerprint="OLD")
    del legacy["coefficientEra"]
    assert _validate(legacy) is qc.RegimeRejection.FINGERPRINT


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


# --------------------------------------------------------------------------
# #991 spec 4.5: `fitFamilyShares`, the materiality reference, has its own
# strict validator. A malformed one disables materiality admission for the
# regime and is never a rejection, because the fit and its TV references
# stay usable.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", [
    "missing", None, {}, {"claude-opus-5": 1.2, "claude-sonnet-5": -0.2},
    {"claude-opus-5": float("nan")}, {"claude-opus-5": float("inf")},
    {"claude-opus-5": "1.0"}, {"claude-opus-5": True}, {"claude-opus-5": 0.2},
    {1: 1.0}, [["claude-opus-5", 1.0]],
    {"claude-opus-5": 1e308, "claude-sonnet-5": 1e308},
], ids=["missing", "null", "empty", "negative", "nan", "inf", "string",
        "bool", "sums-to-0.2", "non-string-key", "list", "overflowing-sum"])
def test_991_a_malformed_fit_reference_is_none_not_a_rejection(value):
    raw = _good()
    if value != "missing":
        raw["fitFamilyShares"] = value
    out = _validate(raw)
    assert isinstance(out, qc.ValidatedRegime), out
    assert out.fit_family_shares is None
    assert out.units_per_point == 2442620.0


def test_991_a_valid_fit_reference_is_returned_unrenormalized():
    shares = {"claude-opus-5": 0.7, "claude-sonnet-5": 0.3000000004}
    out = _validate(_good(fitFamilyShares=dict(shares)))
    assert out.fit_family_shares == shares
    assert out.fit_family_shares["claude-sonnet-5"] == 0.3000000004


def test_991_the_tv_references_still_parse_through_shares():
    """PRESERVATION. `familyShares` keeps its looser `_shares` rule: a
    non-unit sum still validates as before, while the same mapping is not
    accepted as a materiality reference."""
    loose = {"claude-opus-5": 0.5}
    out = _validate(_good(familyShares=dict(loose),
                          fitFamilyShares=dict(loose)))
    assert isinstance(out, qc.ValidatedRegime)
    assert out.family_shares == loose
    assert out.fit_family_shares is None


# --- #1015 T2: the fail-fast stats reader and the correction facts -----------
#
# A forecast or the status line assesses a calibration fit against the
# published correction facts. That read must never repair, migrate, recover,
# rebuild or wait on stats.db (spec 5.3): any of those would turn a per-prompt
# reader into a writer, or stall a prompt behind maintenance.

import fcntl
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import _lib_quota_lineage as ql

ACCT_A = "acct-a"
ACCT_B = "acct-b"
_BATCH_1 = "batch-1"
_BATCH_EMPTY = "batch-empty"


@pytest.fixture
def stats_home(tmp_path, monkeypatch):
    """A scratch home holding a published stats index at the current epoch,
    closed (no side files)."""
    from conftest import load_script, redirect_paths
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core as core
    ns["open_db"]().close()
    db = pathlib.Path(core.DB_PATH)
    assert db.exists()
    for suffix in ("-journal", "-wal", "-shm"):
        assert not pathlib.Path(str(db) + suffix).exists(), suffix
    return db


def _family(db: pathlib.Path) -> dict:
    """Every stats family member and marker beside the index, with its bytes
    and mtime (the maintenance lock file is not part of the family)."""
    return {p.name: (p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(db.parent.iterdir())
            if p.name.startswith(db.name)
            and not p.name.endswith(".maintenance.lock")}


@pytest.fixture
def no_recovery(monkeypatch):
    """Fail the test when the fail-fast path reaches any opener, recovery,
    migration or rebuild entry point."""
    import _cctally_core
    import _cctally_db
    import _cctally_journal
    import _cctally_store
    calls = []

    def spy(name):
        def _called(*a, **k):
            calls.append(name)
            raise AssertionError(f"fail-fast reader reached {name}")
        return _called

    for module, name in (
            (_cctally_store, "_resolve_stats_publication_marker"),
            (_cctally_store, "_resume_pending_quarantine"),
            (_cctally_store, "_recover_or_reclaim_interrupted_stats_rebuild"),
            (_cctally_store, "stats_open_guarded"),
            (_cctally_core, "open_db"),
            (_cctally_journal, "rebuild_stats_index"),
            (_cctally_db, "_run_pending_migrations")):
        monkeypatch.setattr(module, name, spy(name), raising=True)
    return calls


def _seed_facts(db: pathlib.Path) -> None:
    """One reviewed op over three observations (A held, B retracted, one with
    no index row), a completed batch touching A, and an empty batch."""
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            "INSERT INTO claude_weekly_observation_index "
            "(observation_id, account_key, captured_at_utc) VALUES (?,?,?)",
            [("obs-a", ACCT_A, "2026-10-22T09:32:00Z"),
             ("obs-b", ACCT_B, "2026-10-22T09:34:00Z")])
        conn.executemany(
            "INSERT INTO claude_weekly_correction_history "
            "(decision_op_id, observation_id, disposition) VALUES (?,?,?)",
            [("op-1", "obs-a", "hold"), ("op-1", "obs-b", "retract"),
             ("op-1", "obs-missing", "accept")])
        conn.execute(
            "INSERT INTO claude_weekly_correction_ops "
            "(decision_op_id, op_segment, op_offset) VALUES (?,?,?)",
            ("op-1", "2026-10.jsonl", 4096))
        conn.executemany(
            "INSERT INTO correction_batch_lineage "
            "(batch_id, commit_segment, commit_offset) VALUES (?,?,?)",
            [(_BATCH_1, "2026-10.jsonl", 8192),
             (_BATCH_EMPTY, "2026-10.jsonl", 9000)])
        conn.executemany(
            "INSERT INTO correction_batch_footprint "
            "(batch_id, provider, account_key) VALUES (?,?,?)",
            [(_BATCH_1, "claude", ACCT_A), (_BATCH_1, "codex", "cx")])
        conn.commit()
    finally:
        conn.close()


def test_1015_failfast_opens_the_published_index_read_only(
        stats_home, no_recovery):
    """Detects a fail-fast reader that writes or migrates the live index: the
    connection is read-only at the current epoch and the family is
    byte-identical afterwards."""
    import _cctally_core
    import _cctally_store
    before = _family(stats_home)
    conn = _cctally_store.stats_open_readonly_failfast()
    assert conn is not None
    try:
        assert (conn.execute("PRAGMA user_version").fetchone()[0]
                == _cctally_core.STATS_INDEX_EPOCH)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE intruder (x)")
    finally:
        conn.close()
    assert _family(stats_home) == before
    assert no_recovery == []


def _repair_marker(db):
    import _cctally_store
    _cctally_store._stats_repair_marker(db).write_text("repairing\n")


def _quarantine_pending(db):
    import _cctally_db
    _cctally_db._quarantine_pending_path(db).write_text("{}\n")


def _rebuild_artifact(db):
    (db.parent / f"{db.name}.rebuilding-20261031T000000_000000").write_bytes(
        b"")


def _publication(status):
    def make(db):
        import _cctally_store
        _cctally_store._stats_publication_marker(db).write_text(
            json.dumps({"status": status}))
    return make


def _side_file(suffix):
    def make(db):
        pathlib.Path(str(db) + suffix).write_bytes(b"")
    return make


def _wrong_epoch(db):
    import _cctally_core
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            f"PRAGMA user_version = {_cctally_core.STATS_INDEX_EPOCH - 1}")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("make", [
    _repair_marker, _quarantine_pending, _rebuild_artifact,
    _publication("pending"), _publication("failed"),
    _side_file("-wal"), _side_file("-shm"), _wrong_epoch,
], ids=["repair-marker", "quarantine-pending", "rebuild-artifact",
        "publication-pending", "publication-failed", "wal", "shm",
        "wrong-epoch"])
def test_1015_failfast_refuses_an_unpublished_family_and_touches_nothing(
        stats_home, no_recovery, make):
    """Detects a per-prompt reader that resumes a quarantine, resolves a
    publication, reclaims a rebuild, adopts side files or reads a stale epoch:
    it returns None, calls no recovery path, and leaves the family exactly as
    it found it."""
    import _cctally_store
    _seed_facts(stats_home)
    make(stats_home)
    before = _family(stats_home)
    assert _cctally_store.stats_open_readonly_failfast() is None
    assert qcg.read_correction_facts(account_scope=ACCT_A) is None
    assert _family(stats_home) == before
    assert no_recovery == []


def test_1015_failfast_returns_at_once_under_maintenance(
        stats_home, no_recovery):
    """Detects a status line that stalls behind a maintenance owner: with the
    maintenance flock held exclusively elsewhere the reader returns None
    within 100 ms."""
    import _cctally_core
    import _cctally_store
    _seed_facts(stats_home)
    lock_path = pathlib.Path(_cctally_core.STATS_LOCK_MAINTENANCE_PATH)
    with open(lock_path, "a+") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        started = time.monotonic()
        assert _cctally_store.stats_open_readonly_failfast() is None
        assert qcg.read_correction_facts(account_scope=ACCT_A) is None
        assert time.monotonic() - started < 0.1
    assert no_recovery == []


def test_1015_failfast_leaves_a_pre_538_wal_family_unconverted(
        stats_home, no_recovery):
    """Detects the reader converting or recovering an old-release store (R10):
    a WAL header with no side files at an old epoch is refused and the main
    file is byte-identical. The spec allows empty side files here; a plain
    read-only WAL open creates a 32 KiB ``-shm``, so the epoch probe refuses
    before it and no side file appears at all."""
    import _cctally_core
    import _cctally_store
    conn = sqlite3.connect(stats_home)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            f"PRAGMA user_version = {_cctally_core.STATS_INDEX_EPOCH - 1}")
        conn.commit()
    finally:
        conn.close()
    assert _cctally_store._stats_header_declares_wal(stats_home)
    main_before = stats_home.read_bytes()
    assert _cctally_store.stats_open_readonly_failfast() is None
    assert stats_home.read_bytes() == main_before
    for suffix in ("-wal", "-shm", "-journal"):
        assert not pathlib.Path(str(stats_home) + suffix).exists(), suffix
    assert no_recovery == []


def test_1015_reader_keeps_a_writers_lock_and_drains(stats_home, no_recovery):
    """Detects a reader that drops this process's SQLite locks (by closing a
    raw descriptor on stats.db) or leaves a handle the maintenance drain sees:
    a writer's RESERVED lock still excludes another process after the read,
    and the family is drained once the writer closes."""
    import _cctally_store
    _seed_facts(stats_home)
    writer = sqlite3.connect(stats_home, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        facts = qcg.read_correction_facts(account_scope="*")
        assert facts is not None and facts.basis
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sqlite3, sys\n"
             "c = sqlite3.connect(sys.argv[1], timeout=0,"
             " isolation_level=None)\n"
             "try:\n"
             "    c.execute('BEGIN IMMEDIATE')\n"
             "except sqlite3.OperationalError as e:\n"
             "    print('busy', e)\n"
             "    sys.exit(0)\n"
             "print('acquired')\n"
             "sys.exit(1)\n",
             str(stats_home)],
            capture_output=True, text=True, timeout=30)
        assert probe.returncode == 0, probe.stdout + probe.stderr
        writer.execute("ROLLBACK")
    finally:
        writer.close()
    assert _cctally_store._stats_family_drained(stats_home) is None
    assert no_recovery == []


def test_1015_reader_returns_the_published_correction_facts(
        stats_home, no_recovery):
    """Detects lost or misattributed correction facts (a fit over corrected
    inputs would keep steering): rows in the account scope are joined to the
    observation index and the op coordinate, an unresolved reference is kept
    in every scope with no account, every completed batch is returned with its
    footprint (empty for an empty batch), and the basis is the batch set."""
    _seed_facts(stats_home)
    op_coord = ("2026-10.jsonl", 4096)
    unresolved = ql.CorrectionRow("op-1", "obs-missing", "accept", None, None,
                                  op_coord)
    row_a = ql.CorrectionRow("op-1", "obs-a", "hold", ACCT_A,
                             "2026-10-22T09:32:00Z", op_coord)
    row_b = ql.CorrectionRow("op-1", "obs-b", "retract", ACCT_B,
                             "2026-10-22T09:34:00Z", op_coord)
    batches = (
        ql.BatchFact(_BATCH_1, ("2026-10.jsonl", 8192),
                     frozenset({("claude", ACCT_A), ("codex", "cx")})),
        ql.BatchFact(_BATCH_EMPTY, ("2026-10.jsonl", 9000), frozenset()),
    )
    facts = qcg.read_correction_facts(account_scope=ACCT_A)
    assert facts == ql.CorrectionFacts(
        (row_a, unresolved), batches, frozenset({_BATCH_1, _BATCH_EMPTY}))
    assert set(qcg.read_correction_facts(account_scope="*").corrections) == {
        row_a, row_b, unresolved}
    conn = sqlite3.connect(stats_home)
    try:
        scoped = qcg.read_correction_facts(conn, account_scope=ACCT_B)
        assert scoped.corrections == (row_b, unresolved)
        assert scoped.batches == batches
        conn.execute("SELECT 1")    # the caller's connection stays open
    finally:
        conn.close()
    assert no_recovery == []


def test_1015_reader_is_unavailable_when_a_correction_has_no_coordinate(
        stats_home, no_recovery):
    """Detects a correction silently dropped from the assessment: a history
    row whose op coordinate is missing makes the facts unavailable (None)
    rather than incomplete."""
    _seed_facts(stats_home)
    conn = sqlite3.connect(stats_home)
    try:
        conn.execute("DELETE FROM claude_weekly_correction_ops")
        conn.commit()
    finally:
        conn.close()
    assert qcg.read_correction_facts(account_scope=ACCT_A) is None
    assert no_recovery == []


# --------------------------------------------------------------------------
# #1015 T5 — the read-time refusal (spec 5.4)
# --------------------------------------------------------------------------
_FIT_ASOF = "2026-10-23T00:00:00+00:00"


def _fitted(*, basis=None, **over):
    """An ACCT_A regime over [2026-10-01, asOf]; with ``basis`` it carries a
    verifying `fitLineage`, without it it is a legacy (unstamped) record."""
    regime = _good(effectiveFrom="2026-10-01T00:00:00+00:00",
                   asOf=_FIT_ASOF, **over)
    if basis is not None:
        regime["fitLineage"] = ql.fit_lineage(
            scope=ACCT_A, basis=basis, windows=ql.dependency_windows(
                "nosplit", a=regime["effectiveFrom"], t=_FIT_ASOF),
            estimator=None, regime=regime)
    return regime


def _read_a(tmp_path, regime, **kw):
    path = _write(tmp_path / "quota-calibrations.json",
                  _state([regime], key=ACCT_A))
    return qcg.read_calibration_file(
        account_key=ACCT_A, path=path, expected_fingerprint="FP",
        expected_revision=3, **kw)


def test_1015_a_fit_whose_basis_holds_every_batch_is_returned(
        stats_home, no_recovery, tmp_path):
    """Non-vacuity: a fit published after the correction's batch keeps
    steering the forecast."""
    _seed_facts(stats_home)
    read = _read_a(tmp_path, _fitted(basis=[_BATCH_1, _BATCH_EMPTY]))
    assert read.rejection is None and read.regime is not None


@pytest.mark.parametrize("basis", [None, []],
                         ids=["legacy", "stamped-before-the-batch"])
def test_1015_a_fit_before_a_correction_to_its_inputs_is_refused(
        stats_home, no_recovery, tmp_path, basis):
    """Detects the #1015 defect: a fit over an observation a later batch
    corrected keeps steering the forecast. It is refused immediately, with
    no sweep, and the sidecar bytes are untouched."""
    _seed_facts(stats_home)
    path = tmp_path / "quota-calibrations.json"
    read = _read_a(tmp_path, _fitted(basis=basis))
    before = path.read_bytes()
    assert read.regime is None
    assert read.rejection is qc.RegimeRejection.RECALIBRATION_NEEDED
    assert read.rejection.value == "recalibration-needed"
    assert read.regime_status is None
    assert path.read_bytes() == before


def test_1015_the_attempt_status_is_stated_only_for_the_current_basis(
        stats_home, no_recovery, tmp_path):
    """Detects a stale attempt status: the sweep's withheld status is stated
    only while its basis is the current completed basis."""
    _seed_facts(stats_home)
    current = sorted([_BATCH_1, _BATCH_EMPTY])
    attempt = {"status": "insufficient-history", "basis": current,
               "asOf": _FIT_ASOF}
    read = _read_a(tmp_path, _fitted(basis=[], recalibrationAttempt=attempt))
    assert read.rejection is qc.RegimeRejection.RECALIBRATION_NEEDED
    assert read.regime_status == "insufficient-history"
    stale = dict(attempt, basis=[_BATCH_1])
    read = _read_a(tmp_path, _fitted(basis=[], recalibrationAttempt=stale))
    assert read.regime_status is None


def test_1015_an_annotated_regime_is_refused_whatever_its_lineage(
        stats_home, no_recovery, tmp_path):
    _seed_facts(stats_home)
    regime = _fitted(basis=[_BATCH_1, _BATCH_EMPTY],
                     supersededByCorrection={"fitId": "fit:x"})
    assert _read_a(tmp_path, regime).rejection \
        is qc.RegimeRejection.RECALIBRATION_NEEDED


def test_1015_missing_lineage_projection_is_unavailable_not_calibrated(
        stats_home, no_recovery, tmp_path):
    """Detects an unverified calibrated number on an index without the
    lineage projections: the reader refuses with
    `calibration-lineage-unavailable`."""
    conn = sqlite3.connect(stats_home)
    try:
        conn.execute("DROP TABLE correction_batch_lineage")
        conn.commit()
    finally:
        conn.close()
    read = _read_a(tmp_path, _fitted(basis=[]))
    assert read.rejection is qc.RegimeRejection.LINEAGE_UNAVAILABLE
    assert read.rejection.value == "calibration-lineage-unavailable"


def test_1015_the_reader_uses_the_callers_stats_connection(
        stats_home, no_recovery, tmp_path, monkeypatch):
    """A consumer holding a current stats connection passes it, and the
    reader opens nothing of its own."""
    import _cctally_store
    _seed_facts(stats_home)
    monkeypatch.setattr(
        _cctally_store, "stats_open_readonly_failfast",
        lambda: (_ for _ in ()).throw(AssertionError("opened its own")))
    conn = sqlite3.connect(stats_home)
    try:
        read = _read_a(tmp_path, _fitted(basis=[]), stats_conn=conn)
    finally:
        conn.close()
    assert read.rejection is qc.RegimeRejection.RECALIBRATION_NEEDED


def test_1015_no_calibration_reads_no_stats(tmp_path, monkeypatch):
    """The common install pays one file read: an absent sidecar never opens
    the stats index."""
    import _cctally_store
    monkeypatch.setattr(
        _cctally_store, "stats_open_readonly_failfast",
        lambda: (_ for _ in ()).throw(AssertionError("opened stats")))
    read = qcg.read_calibration_file(
        account_key=ACCT_A, path=tmp_path / "absent.json",
        expected_fingerprint="FP", expected_revision=3)
    assert read.rejection is qc.RegimeRejection.ABSENT


# --------------------------------------------------------------------------
# #1015 R6 — the facts are read only as far as the assessed lineages need
# --------------------------------------------------------------------------
_SEG = "2026-10.jsonl"
_S = "2026-10-10T00:00:00+00:00"
_T = "2026-10-23T00:00:00+00:00"
_A = "2026-09-22T00:00:00+00:00"
#: (observation, account, captured_at_utc) — every stored text form a reader
#: may meet, endpoints exactly at S and T, a naive and a calendar-invalid
#: value the kernel reads as unresolved, and one observation with no row.
_R6_INDEX = (
    ("o-s", ACCT_A, "2026-10-10T00:00:00.000000Z"),
    ("o-t", ACCT_A, "2026-10-23T00:00:00Z"),
    ("o-before-s", ACCT_A, "2026-10-09T23:59:59.999999Z"),
    ("o-after-t", ACCT_A, "2026-10-23T00:00:00.000001Z"),
    ("o-b", ACCT_B, "2026-10-15T00:00:00+00:00"),
    ("o-old", ACCT_A, "2026-08-01T00:00:00.123Z"),
    ("o-naive", ACCT_A, "2026-10-15T00:00:00"),
    ("o-badcal", ACCT_A, "2026-02-30T00:00:00Z"),
)
_R6_OPS = (("op-early", 100), ("op-mid", 500), ("op-late", 2000))
_R6_HISTORY = (
    ("op-early", "o-s", "hold"), ("op-early", "o-t", "retract"),
    ("op-early", "o-before-s", "retract"),
    ("op-mid", "o-after-t", "accept"), ("op-mid", "o-b", "retract"),
    ("op-mid", "o-old", "retract"), ("op-mid", "o-missing", "hold"),
    ("op-late", "o-naive", "hold"), ("op-late", "o-badcal", "retract"),
    ("op-late", "o-t", "accept"),
)
#: (batch, commit offset, footprint): an account batch, the account-blind
#: `('claude','*')` and `('*','*')` footprints, a Codex-only batch after
#: every op and an empty batch.
_R6_BATCHES = (
    ("b-a", 300, (("claude", ACCT_A),)),
    ("b-claude-any", 800, (("claude", "*"),)),
    ("b-unknown", 1500, (("*", "*"),)),
    ("b-b", 1200, (("claude", ACCT_B),)),
    ("b-codex", 2500, (("codex", "cx"),)),
    ("b-empty", 2600, ()),
    ("b-a-late", 2700, (("claude", ACCT_A),)),
)


def _seed_r6(db: pathlib.Path) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            "INSERT INTO claude_weekly_observation_index VALUES (?,?,?)",
            _R6_INDEX)
        conn.executemany(
            "INSERT INTO claude_weekly_correction_ops VALUES (?,?,?)",
            [(op, _SEG, off) for op, off in _R6_OPS])
        conn.executemany(
            "INSERT INTO claude_weekly_correction_history VALUES (?,?,?)",
            _R6_HISTORY)
        for batch, off, footprint in _R6_BATCHES:
            conn.execute("INSERT INTO correction_batch_lineage VALUES (?,?,?)",
                         (batch, _SEG, off))
            conn.executemany(
                "INSERT INTO correction_batch_footprint VALUES (?,?,?)",
                [(batch, p, a) for p, a in footprint])
        conn.commit()
    finally:
        conn.close()


def _r6_lineages() -> list:
    """Every lineage shape the readers assess: no-split, successor and
    baseline windows, a legacy open-start window, no window at all, each
    scope, and bases from none to every batch."""
    every = [b for b, _o, _f in _R6_BATCHES]
    windows = {
        "nosplit": ql.dependency_windows("nosplit", a=_A, t=_T),
        "successor": ql.dependency_windows("successor", a=_A, s=_S, t=_T),
        "baseline": ql.dependency_windows("baseline", a=_A, s=_S),
        "open-start": (ql.window(None, _T, until_inclusive=True),),
        "none": (),
    }
    bases = ([], ["b-a"], ["b-a", "b-claude-any"],
             ["b-a", "b-claude-any", "b-unknown", "b-b"],
             [b for b in every if b != "b-a-late"], every)
    out = []
    for scope in (ACCT_A, ACCT_B, "*"):
        for name, w in windows.items():
            for basis in bases:
                out.append({"version": 1, "fitId": f"fit:{scope}:{name}",
                            "accountScope": scope, "basis": list(basis),
                            "estimator": None, "dependencyWindows": list(w),
                            "legacy": False})
    return out


def _disposition_reads(conn) -> list:
    """Every statement prepared on ``conn`` that reads a correction row's
    disposition — the correction rows themselves, not the batches, the op
    coordinates or the projection probes."""
    reads: list = []
    conn.set_authorizer(_authorizer_into(reads))
    return reads


def test_1015_reduced_facts_assess_exactly_like_the_full_facts(
        stats_home, no_recovery):
    """Detects a reduced read that changes a verdict (a tainted fit kept
    steering, or a clean one refused): for every lineage, the facts read for
    any set of lineages containing it assess exactly like the full facts —
    across unresolved references, a naive or calendar-invalid capture,
    endpoints exactly at S and T, out-of-scope accounts, batches inside the
    basis, and the account-blind footprints."""
    _seed_r6(stats_home)
    lineages = _r6_lineages()
    key = qcg._coordinate_order
    conn = sqlite3.connect(stats_home)
    try:
        states = set()
        reduced_any = False
        for scope in ("*", ACCT_A):
            full = qcg.read_correction_facts(conn, account_scope=scope)
            assert full is not None and len(full.corrections) > 0
            for i, lineage in enumerate(lineages):
                expected = ql.assess(lineage, full, coord_key=key)
                states.add(expected.state)
                for group in ([lineage], [lineage, lineages[-1 - i]],
                              [lineages[(i * 7) % len(lineages)], lineage]):
                    reduced = qcg.read_correction_facts(
                        conn, account_scope=scope, lineages=group)
                    assert reduced.batches == full.batches
                    assert reduced.basis == full.basis
                    assert set(reduced.corrections) <= set(full.corrections)
                    reduced_any |= len(reduced.corrections) < len(
                        full.corrections)
                    assert ql.assess(lineage, reduced, coord_key=key) \
                        == expected, (scope, lineage, group)
        # Non-vacuity: every verdict occurs and the read really narrows.
        assert states == {"clean", "tainted", "unavailable"}
        assert reduced_any
        # D3 survives the narrowing: a decision with no op coordinate makes
        # the facts unavailable even for a lineage that needs no row.
        conn.execute("DELETE FROM claude_weekly_correction_ops "
                     "WHERE decision_op_id = 'op-mid'")
        conn.commit()
        every = dict(lineages[-1], basis=[b for b, _o, _f in _R6_BATCHES])
        assert qcg.read_correction_facts(conn, account_scope="*") is None
        assert qcg.read_correction_facts(
            conn, account_scope="*", lineages=[every]) is None
    finally:
        conn.close()
    assert no_recovery == []


def test_1015_a_basis_holding_every_claude_batch_reads_no_correction_row(
        stats_home, no_recovery, tmp_path):
    """Detects the R6 status-line regression: a fit whose basis already
    holds every batch that could taint it is clean without reading a single
    correction row, on the facts reader and on the forecast's calibration
    read; a fit that needs the rows still reads them."""
    _seed_r6(stats_home)
    every = [b for b, _o, _f in _R6_BATCHES]
    claude = [b for b in every if b not in ("b-codex", "b-empty")]
    lineage = dict(_r6_lineages()[0], basis=claude)
    conn = sqlite3.connect(stats_home)
    try:
        reads = _disposition_reads(conn)
        facts = qcg.read_correction_facts(conn, account_scope=ACCT_A,
                                          lineages=[lineage])
        assert facts.corrections == () and reads == []
        read = _read_a(tmp_path, _fitted(basis=every), stats_conn=conn)
        assert read.rejection is None and reads == []
        read = _read_a(tmp_path, _fitted(basis=[]), stats_conn=conn)
        assert read.rejection is qc.RegimeRejection.RECALIBRATION_NEEDED
        assert reads != []
    finally:
        conn.close()


def _marker_regime(start, until, *, basis, units, fingerprint="FP"):
    regime = _good(effectiveFrom=start, effectiveUntil=until,
                   unitsPerPoint=units, fingerprint=fingerprint,
                   interval={"lo": units * 0.9, "hi": units * 1.1},
                   asOf=until or _T)
    if basis is not None:
        regime["fitLineage"] = ql.fit_lineage(
            scope="*", basis=basis, windows=ql.dependency_windows(
                "nosplit", a=start, t=until or _T),
            estimator=None, regime=regime)
    return regime


def _reference_marker(regimes, conn):
    """Spec 5.6 stated directly: pair the clean regimes, suppress a
    superseded identity, refuse on unreadable facts."""
    import _cctally_quota_model as qmg
    import _lib_meter_rate_change as mrc
    facts = qcg.read_correction_facts(conn, account_scope="*")
    if facts is None:
        return qcg.MARKER_UNAVAILABLE, None
    active = mrc.active_rate_change(qmg.clean_regimes(regimes, facts, "*"))
    if active is None:
        return qcg.MARKER_NONE, None
    if qcg.rate_change_supersession(
            conn, ("claude", qmg._alert_account_key(None),
                   active["effective_from"]), facts=facts) is not None:
        return qcg.MARKER_NONE, None
    return qcg.MARKER_ACTIVE, active


def test_1015_the_marker_assesses_newest_first_and_matches_clean_pairing(
        stats_home, no_recovery, monkeypatch):
    """Detects a marker that differs from "pair the clean regimes, suppress
    superseded identities" (a hidden or a false Δrate), and the R6 cost of
    assessing history it does not need: an old closed legacy regime is never
    assessed when the newest pair is clean under a full basis, so no
    correction row is read; a tainted newest predecessor falls through to the
    next clean one exactly as the clean pairing does."""
    import _cctally_store
    _seed_r6(stats_home)
    every = [b for b, _o, _f in _R6_BATCHES]
    real_open = _cctally_store.stats_open_readonly_failfast
    reads: list = []

    def traced_open():
        conn = real_open()
        if conn is not None:
            conn.set_authorizer(_authorizer_into(reads))
        return conn

    monkeypatch.setattr(_cctally_store, "stats_open_readonly_failfast",
                        traced_open)
    legacy = _marker_regime("2026-07-25T05:00:00+00:00", _A, basis=None,
                            units=90_000.0)
    pred = _marker_regime(_A, _S, basis=every, units=100_000.0)
    succ = _marker_regime(_S, None, basis=every, units=400_000.0)
    tainted_pred = _marker_regime(_A, _S, basis=[], units=120_000.0)
    other_fp = _marker_regime(_A, _S, basis=every, units=110_000.0,
                              fingerprint="OTHER")
    cases = {
        "clean newest pair": ([legacy, pred, succ], False),
        "tainted first predecessor": ([legacy, tainted_pred, pred, succ],
                                      True),
        "tainted successor": ([legacy, pred, dict(succ, fitLineage=None)],
                              True),
        "mismatched first predecessor": ([other_fp, pred, succ], False),
    }
    for name, (regimes, needs_rows) in cases.items():
        reads.clear()
        state, active = qcg.assess_active_rate_change(None, regimes=regimes)
        assert (reads != []) is needs_rows, name
        check = sqlite3.connect(stats_home)
        try:
            assert (state, active) == _reference_marker(regimes, check), name
        finally:
            check.close()
        clean_pair = qcg.marker_regimes(None, regimes=regimes)
        import _lib_meter_rate_change as mrc
        assert (mrc.active_rate_change(clean_pair) == active), name
    assert no_recovery == []


def _authorizer_into(reads):
    def authorizer(action, arg1, arg2, _db, _trigger):
        if action == sqlite3.SQLITE_READ and arg1 == \
                "claude_weekly_correction_history" and arg2 == "disposition":
            reads.append(arg2)
        return sqlite3.SQLITE_OK
    return authorizer
