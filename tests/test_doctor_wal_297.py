"""doctor `cache.db WAL size` check (#297, Task 5).

Pure-function tests over the _lib_doctor kernel. The fingerprint regression
is adapted to the REAL `_identity_slice`, which operates on a `DoctorReport`
(hashing `[check_id, severity]` per check + counts + overall_severity, and
EXCLUDING summary/remediation/details). So the exact WAL byte count lives
only in the fingerprint-excluded `details` block: two different
below-threshold byte counts must share a report fingerprint, while crossing
256 MB (OK->WARN) must change it. We go through `run_checks` + `fingerprint`
(the public entrypoints) rather than the plan's original
`_identity_slice(_check_db_wal_size(...))`, which mistakenly treated the
slice as per-check.
"""
import dataclasses
import importlib
import os
import sys

BIN = os.path.join(os.path.dirname(__file__), "..", "bin")
sys.path.insert(0, BIN)
doctor = importlib.import_module("_lib_doctor")


def _state(**kw):
    """Build a DoctorState with only the field(s) under test set; every other
    field falls back to its dataclass default or None."""
    fields = {f.name: (f.default if f.default is not dataclasses.MISSING else None)
              for f in dataclasses.fields(doctor.DoctorState)}
    fields.update(kw)
    return doctor.DoctorState(**fields)


def test_wal_check_warn_above_threshold():
    r = doctor._check_db_wal_size(_state(cache_db_wal_bytes=300 * 1024 * 1024))
    assert r.severity == "warn"
    assert "db checkpoint" in (r.remediation or "")


def test_wal_check_ok_below_threshold():
    assert doctor._check_db_wal_size(_state(cache_db_wal_bytes=10 * 1024 * 1024)).severity == "ok"


def test_wal_check_ok_when_none():
    assert doctor._check_db_wal_size(_state(cache_db_wal_bytes=None)).severity == "ok"


def test_wal_check_byte_count_only_in_details():
    # The exact byte count must NOT appear in the (fingerprint-hashed) summary;
    # it lives only in details. Two below-threshold counts share a summary.
    r1 = doctor._check_db_wal_size(_state(cache_db_wal_bytes=10 * 1024 * 1024))
    r2 = doctor._check_db_wal_size(_state(cache_db_wal_bytes=50 * 1024 * 1024))
    assert r1.summary == r2.summary
    assert r1.details["cache_db_wal_bytes"] != r2.details["cache_db_wal_bytes"]


def test_wal_below_threshold_shares_report_fingerprint():
    # Report-level fingerprint regression (the spec's actual invariant): byte
    # drift below the threshold does not flip the fingerprint; crossing does.
    f1 = doctor.fingerprint(doctor.run_checks(_state(cache_db_wal_bytes=10 * 1024 * 1024)))
    f2 = doctor.fingerprint(doctor.run_checks(_state(cache_db_wal_bytes=50 * 1024 * 1024)))
    f3 = doctor.fingerprint(doctor.run_checks(_state(cache_db_wal_bytes=300 * 1024 * 1024)))
    assert f1 == f2      # byte drift below threshold -> same fingerprint
    assert f1 != f3      # crossing 256 MB -> different


def test_wal_size_check_registered_in_db_category():
    ids = [cid for _cat_id, _title, specs in doctor._CATEGORY_DEFINITIONS
           for cid, _fn in specs]
    assert "db.wal_size" in ids
