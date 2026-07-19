"""Pure doctor contracts for #312 Codex project-metadata health."""
from __future__ import annotations

import dataclasses
import importlib
import pathlib
import sys


BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))
doctor = importlib.import_module("_lib_doctor")


def _state(**kwargs):
    fields = {
        field.name: (field.default if field.default is not dataclasses.MISSING else None)
        for field in dataclasses.fields(doctor.DoctorState)
    }
    fields.update(kwargs)
    return doctor.DoctorState(**fields)


def test_codex_project_metadata_is_ok_when_empty_or_all_qualified():
    result = doctor._check_data_codex_project_metadata(_state(
        codex_project_metadata_health={
            "total_rows": 2,
            "qualified_rows": 2,
            "missing_conversation_key_rows": 0,
            "missing_thread_join_rows": 0,
        },
    ))
    assert result.id == "data.codex_project_metadata"
    assert result.severity == "ok"
    assert result.summary == "qualified"
    assert result.remediation is None
    assert result.details == {
        "total_rows": 2,
        "qualified_rows": 2,
        "missing_conversation_key_rows": 0,
        "missing_thread_join_rows": 0,
        "incomplete_rows": 0,
    }


def test_codex_project_metadata_warns_with_all_history_counts_and_safe_details():
    result = doctor._check_data_codex_project_metadata(_state(
        codex_project_metadata_health={
            "total_rows": 10,
            "qualified_rows": 7,
            "missing_conversation_key_rows": 2,
            "missing_thread_join_rows": 1,
        },
    ))
    assert result.id == "data.codex_project_metadata"
    assert result.severity == "warn"
    assert result.details["incomplete_rows"] == 3
    assert set(result.details) == {
        "total_rows", "qualified_rows", "missing_conversation_key_rows",
        "missing_thread_join_rows", "incomplete_rows",
    }
    assert "cache-sync --source codex --rebuild" in (result.remediation or "")


def test_codex_project_metadata_gather_error_is_fail():
    result = doctor._check_data_codex_project_metadata(_state(
        codex_project_metadata_health=None,
        codex_project_metadata_error="DatabaseError",
    ))
    assert result.id == "data.codex_project_metadata"
    assert result.severity == "fail"
    assert result.summary == "metadata health could not be read"
    assert result.details == {"error": "DatabaseError"}


def test_codex_project_metadata_is_registered_after_codex_cache():
    data_checks = next(
        checks for category, _title, checks in doctor._CATEGORY_DEFINITIONS
        if category == "data"
    )
    ids = [check_id for check_id, _fn in data_checks]
    assert ids.index("data.codex_project_metadata") == ids.index("data.codex_cache") + 1
