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


# --------------------------------------------------------------------------
# conversations.db WAL size (#583 S4 / F39)
#
# An exact sibling of the cache leg above, sharing DOCTOR_WAL_WARN_BYTES
# because STORE_POLICY gives conversations.db the same 128 MiB
# journal_size_limit — so the same "this only fires when containment has
# genuinely failed" reasoning transfers unchanged.
# --------------------------------------------------------------------------


def test_conversations_wal_ok_below_threshold():
    r = doctor._check_db_conversations_wal_size(
        _state(conversations_db_wal_bytes=1024)
    )
    assert r.id == "db.conversations_wal_size"
    assert r.title == "conversations.db WAL size"
    assert r.severity == "ok"
    assert r.summary == "within limit"
    assert r.remediation is None
    assert r.details["conversations_db_wal_bytes"] == 1024


def test_conversations_wal_warns_above_threshold():
    r = doctor._check_db_conversations_wal_size(
        _state(conversations_db_wal_bytes=doctor.DOCTOR_WAL_WARN_BYTES + 1)
    )
    assert r.severity == "warn"
    assert r.summary == "oversized — conversations.db WAL far above its cap"
    assert r.remediation == (
        "Run `cctally db checkpoint --db conversations` to drain the WAL."
    )


def test_conversations_wal_absent_is_ok():
    assert doctor._check_db_conversations_wal_size(
        _state(conversations_db_wal_bytes=0)
    ).severity == "ok"


def test_conversations_wal_stat_failure_is_ok_not_warn():
    """None means doctor could not read it. Doctor never blocks or raises, and
    an unreadable sidecar is not evidence of a wedge."""
    assert doctor._check_db_conversations_wal_size(
        _state(conversations_db_wal_bytes=None)
    ).severity == "ok"


def test_conversations_wal_summary_carries_no_byte_count():
    """The exact count lives ONLY in fingerprint-excluded details, so a byte
    count drifting below the threshold cannot flip the doctor fingerprint."""
    a = doctor._check_db_conversations_wal_size(
        _state(conversations_db_wal_bytes=1024)
    )
    b = doctor._check_db_conversations_wal_size(
        _state(conversations_db_wal_bytes=999_999)
    )
    assert a.summary == b.summary
    assert "1024" not in a.summary and "999999" not in b.summary
    assert a.details["conversations_db_wal_bytes"] != (
        b.details["conversations_db_wal_bytes"]
    )


def test_conversations_wal_fingerprint_ignores_drift_moves_on_crossing():
    """Directly asserts the property the summary rule exists to protect, over
    the REAL report builder so `_identity_slice` is what is exercised."""
    f_small = doctor.fingerprint(
        doctor.run_checks(_state(conversations_db_wal_bytes=1024))
    )
    f_other_small = doctor.fingerprint(
        doctor.run_checks(_state(conversations_db_wal_bytes=999_999))
    )
    f_oversized = doctor.fingerprint(
        doctor.run_checks(
            _state(conversations_db_wal_bytes=doctor.DOCTOR_WAL_WARN_BYTES + 1)
        )
    )
    assert f_small == f_other_small
    assert f_small != f_oversized


def test_conversations_wal_check_registered_immediately_after_cache_wal():
    ids = [cid for _cat_id, _title, specs in doctor._CATEGORY_DEFINITIONS
           for cid, _fn in specs]
    assert "db.conversations_wal_size" in ids
    assert ids.index("db.conversations_wal_size") == ids.index("db.wal_size") + 1


def test_conversations_wal_is_independent_of_the_cache_leg():
    """An oversized cache WAL must not warn the conversations leg, and vice
    versa — otherwise one gathered field could be reported as the other."""
    only_cache = doctor.run_checks(
        _state(
            cache_db_wal_bytes=doctor.DOCTOR_WAL_WARN_BYTES + 1,
            conversations_db_wal_bytes=1024,
        )
    )
    by_id = {c.id: c for cat in only_cache.categories for c in cat.checks}
    assert by_id["db.wal_size"].severity == "warn"
    assert by_id["db.conversations_wal_size"].severity == "ok"

    only_conv = doctor.run_checks(
        _state(
            cache_db_wal_bytes=1024,
            conversations_db_wal_bytes=doctor.DOCTOR_WAL_WARN_BYTES + 1,
        )
    )
    by_id = {c.id: c for cat in only_conv.categories for c in cat.checks}
    assert by_id["db.wal_size"].severity == "ok"
    assert by_id["db.conversations_wal_size"].severity == "warn"
