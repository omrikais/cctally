"""Doctor reclaimable cache-space hint (#315)."""

import dataclasses
import importlib
import os
import sys

import pytest


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
    assert (
        "db.conversations_reclaimable",
        "_check_db_conversations_reclaimable",
    ) in database


GiB = 1024 ** 3
PAGE = 4096
EPISODE = {"policy_version": 1, "eligible": True}


@pytest.mark.parametrize("free, total, record, severity, summary", [
    (300, 1000, None, "ok", "below threshold"),
    (2 * GiB // PAGE + 1, 4 * GiB // PAGE, None, "warn",
     "Reclaiming 2.0 GiB of free transcript space at a paced rate."),
    (int(1.5 * GiB) // PAGE, 4 * GiB // PAGE, None, "ok", "below threshold"),
    (int(1.5 * GiB) // PAGE, 4 * GiB // PAGE, EPISODE, "warn",
     "Reclaiming 1.5 GiB of free transcript space at a paced rate."),
    (GiB // PAGE, 4 * GiB // PAGE, EPISODE, "ok", "below threshold"),
    (16 * GiB // PAGE, 32 * GiB // PAGE, None, "fail",
     "reclaim backlog 16.0 GiB is at or over the 16.0 GiB ceiling; "
     "transcript rebuilds are refused"),
    (None, None, None, "ok", "below threshold"),
])
def test_conversation_reclaimable_remap_table(free, total, record, severity,
                                              summary):
    result = doctor._check_db_conversations_reclaimable(_state(
        conversations_db_page_count=total,
        conversations_db_freelist_count=free,
        conversations_db_page_size=None if total is None else PAGE,
        conversations_reclaim_pending=record))
    assert result.id == "db.conversations_reclaimable"
    assert (result.severity, result.summary) == (severity, summary)
    if severity == "ok":
        assert result.remediation is None
    else:
        assert "cctally db vacuum --db conversations" in result.remediation
    # #901 §5.4 (Q9): a paced WARN also says why it waits and names a
    # planner refusal by reason, so only the WARN rows carry those two keys.
    paced = {"reclaim_wait", "reclaim_refusal"} if severity == "warn" else set()
    assert set(result.details) == {
        "conversations_db_page_count", "conversations_db_freelist_count",
        "conversations_db_page_size", "conversations_db_free_ratio",
        "reclaimable_bytes", "reclaim_start_bytes", "reclaim_start_ratio",
        "reclaim_stop_bytes", "reclaim_stop_ratio", "reclaim_ceiling_bytes",
        "reclaim_episode_active", "reclaim_failure"} | paced
    if severity == "warn":
        # No running dashboard reports a refusal and no failure is recorded,
        # so the wait is the paced one.
        assert result.details["reclaim_wait"] == "paced"
        assert result.details["reclaim_refusal"] is None
    assert result.details["reclaim_ceiling_bytes"] == 16 * GiB


def test_a_failing_eligible_reclaim_warns_with_its_reason():
    result = doctor._check_db_conversations_reclaimable(_state(
        conversations_db_page_count=4 * GiB // PAGE,
        conversations_db_freelist_count=int(1.5 * GiB) // PAGE,
        conversations_db_page_size=PAGE,
        conversations_reclaim_pending={
            **EPISODE, "last_failure": {
                "reason": "no_checkpoint_on_close_unavailable",
                "at": "2026-10-03T12:00:00Z"}}))
    assert result.severity == "warn"
    assert result.details["reclaim_failure"]["reason"] == \
        "no_checkpoint_on_close_unavailable"
