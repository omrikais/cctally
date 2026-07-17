"""doctor `hooks.statusline_refresh_interval` check (#311 D4).

Pure-function tests over the _lib_doctor kernel. The check reads the
precomputed `statusline_refresh_state` scalar (a defaulted tail field on
DoctorState, default "unavailable") — the I/O-free kernel never touches the
setup classifier. WARN fires ONLY on `missing`; every other state is OK with
its own stable summary.
"""
import dataclasses
import importlib
import os
import sys

import pytest

BIN = os.path.join(os.path.dirname(__file__), "..", "bin")
sys.path.insert(0, BIN)
doctor = importlib.import_module("_lib_doctor")


def _state(**kw):
    fields = {f.name: (f.default if f.default is not dataclasses.MISSING else None)
              for f in dataclasses.fields(doctor.DoctorState)}
    fields.update(kw)
    return doctor.DoctorState(**fields)


def test_missing_warns_with_remediation():
    r = doctor._check_statusline_refresh_interval(_state(statusline_refresh_state="missing"))
    assert r.severity == "warn"
    assert r.id == "hooks.statusline_refresh_interval"
    rem = r.remediation or ""
    assert "cctally setup" in rem
    assert "statusline.md" in rem.lower()


@pytest.mark.parametrize("state", ["present", "absent", "foreign", "unavailable"])
def test_non_missing_states_are_ok(state):
    r = doctor._check_statusline_refresh_interval(_state(statusline_refresh_state=state))
    assert r.severity == "ok"
    assert r.remediation is None
    assert r.id == "hooks.statusline_refresh_interval"


def test_default_field_is_unavailable_and_ok():
    # Defaulted tail field default "unavailable" → the check is OK when a
    # constructor omits the field entirely.
    assert _state().statusline_refresh_state == "unavailable"
    assert doctor._check_statusline_refresh_interval(_state()).severity == "ok"


def test_each_state_has_a_stable_distinct_summary():
    states = ["present", "absent", "foreign", "unavailable", "missing"]
    summaries = {
        st: doctor._check_statusline_refresh_interval(
            _state(statusline_refresh_state=st)).summary
        for st in states
    }
    # A distinct summary per state (not-applicable states say so).
    assert len(set(summaries.values())) == len(states)
    # Stable: same input → same summary (fingerprint stability).
    for st in states:
        again = doctor._check_statusline_refresh_interval(
            _state(statusline_refresh_state=st)).summary
        assert again == summaries[st]


def test_registered_in_hooks_category():
    hooks_specs = next(
        specs for cat_id, _title, specs in doctor._CATEGORY_DEFINITIONS
        if cat_id == "hooks"
    )
    ids = [cid for cid, _fn in hooks_specs]
    assert "hooks.statusline_refresh_interval" in ids


def test_only_missing_flips_the_report_fingerprint():
    # OK<->OK across present/absent/foreign/unavailable shares a fingerprint;
    # crossing into `missing` (WARN) changes it.
    f_present = doctor.fingerprint(doctor.run_checks(_state(statusline_refresh_state="present")))
    f_absent = doctor.fingerprint(doctor.run_checks(_state(statusline_refresh_state="absent")))
    f_missing = doctor.fingerprint(doctor.run_checks(_state(statusline_refresh_state="missing")))
    assert f_present == f_absent
    assert f_present != f_missing
