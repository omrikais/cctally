"""Doctor guidance for live and stale cache recovery ownership."""
from __future__ import annotations

import dataclasses
import importlib
import os
import sys


BIN = os.path.join(os.path.dirname(__file__), "..", "bin")
sys.path.insert(0, BIN)
doctor = importlib.import_module("_lib_doctor")


def _state(marker):
    fields = {
        field.name: (
            field.default if field.default is not dataclasses.MISSING else None
        )
        for field in dataclasses.fields(doctor.DoctorState)
    }
    fields["cache_db_status"] = {
        "path": "/private/cache.db",
        "user_version": 1,
        "registry_size": 1,
    }
    fields["cache_repair_marker"] = marker
    return doctor.DoctorState(**fields)


def test_doctor_reports_live_cache_repair_without_destructive_advice():
    result = doctor._check_db_cache_file(
        _state({"exists": True, "live": True, "reason": "live owner"})
    )

    assert result.severity == "warn"
    assert result.summary == "repair in progress"
    assert result.remediation == "Wait for the active cache repair to finish."


def test_doctor_points_stale_cache_repair_at_proven_rebuild_command():
    result = doctor._check_db_cache_file(
        _state({"exists": True, "live": False, "reason": "dead owner"})
    )

    assert result.severity == "warn"
    assert result.summary == "stale repair owner"
    assert result.remediation == "Run `cctally cache-sync --rebuild`"


def test_doctor_cache_file_is_unchanged_without_repair_marker():
    result = doctor._check_db_cache_file(
        _state({"exists": False, "live": None, "reason": None})
    )

    assert result.severity == "ok"
