"""Doctor reclaimable cache-space hint (#315)."""

import dataclasses
import importlib
import os
import sys


BIN = os.path.join(os.path.dirname(__file__), "..", "bin")
sys.path.insert(0, BIN)
doctor = importlib.import_module("_lib_doctor")


def _state(**kw):
    fields = {
        field.name: (
            field.default if field.default is not dataclasses.MISSING else None
        )
        for field in dataclasses.fields(doctor.DoctorState)
    }
    fields.update(kw)
    return doctor.DoctorState(**fields)


def test_reclaimable_warns_at_twenty_five_percent_free_pages():
    result = doctor._check_db_reclaimable(_state(
        cache_db_page_count=100,
        cache_db_freelist_count=25,
    ))

    assert result.id == "db.reclaimable"
    assert result.severity == "warn"
    assert result.summary == "high — 25.0% of cache.db pages are free"
    assert "cctally db vacuum --db cache" in (result.remediation or "")
    assert result.details == {
        "cache_db_page_count": 100,
        "cache_db_freelist_count": 25,
        "cache_db_free_ratio": 0.25,
        "warn_ratio": 0.25,
    }


def test_reclaimable_stays_ok_below_threshold():
    result = doctor._check_db_reclaimable(_state(
        cache_db_page_count=100,
        cache_db_freelist_count=24,
    ))

    assert result.severity == "ok"
    assert result.summary == "below threshold"
    assert result.remediation is None


def test_reclaimable_degrades_ok_when_page_counts_unavailable_or_empty():
    unavailable = doctor._check_db_reclaimable(_state(
        cache_db_page_count=None,
        cache_db_freelist_count=None,
    ))
    empty = doctor._check_db_reclaimable(_state(
        cache_db_page_count=0,
        cache_db_freelist_count=0,
    ))

    assert unavailable.severity == "ok"
    assert unavailable.details["cache_db_free_ratio"] is None
    assert empty.severity == "ok"
    assert empty.details["cache_db_free_ratio"] is None


def test_reclaimable_check_is_registered_in_database_category():
    database = next(
        specs
        for category_id, _title, specs in doctor._CATEGORY_DEFINITIONS
        if category_id == "db"
    )

    assert ("db.reclaimable", "_check_db_reclaimable") in database
