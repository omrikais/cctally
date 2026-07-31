"""The `data.codex_ingest_backlog` doctor leg (public #5 spec §5).

The hook's Codex ingest leg is bounded in wall clock, so it can legitimately
leave work for the next tick — that is the mechanism working, not a fault.
What is worth an operator's attention is a backlog that never drains: a store
whose per-tick budget is smaller than its per-tick growth, or a walk that has
stopped making progress. The leg therefore distinguishes DRAINING from STUCK on
the `since` stamp rather than on the counts.

Covers the pure check's three states, the cache-side write that feeds it, and
the gather that carries it across.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402

NOW = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def D():
    load_script()
    import _lib_doctor
    return _lib_doctor


def _state(record=None):
    return types.SimpleNamespace(codex_ingest_backlog=record, now_utc=NOW)


def _record(*, files, byte_count=4096, minutes_ago=5):
    since = NOW - dt.timedelta(minutes=minutes_ago)
    return {
        "files": files, "bytes": byte_count,
        "since": since.isoformat().replace("+00:00", "Z"),
    }


# ── the pure check ─────────────────────────────────────────────────────────

def test_ok_when_there_is_no_backlog(D):
    """The absent record IS the zero state.

    A drained walk deletes the row rather than zeroing it, so `None` and
    "nothing owed" are the same thing by construction.
    """
    result = D._check_data_codex_ingest_backlog(_state())
    assert result.severity == "ok"
    assert result.summary == "no Codex ingest backlog"
    assert result.remediation is None


def test_ok_while_a_fresh_backlog_is_draining(D):
    """A heavy burst legitimately leaves work for the next few ticks.

    Warning here would fire on every large Codex session and train the operator
    to ignore the leg.
    """
    result = D._check_data_codex_ingest_backlog(
        _state(_record(files=12, minutes_ago=5)))
    assert result.severity == "ok"
    assert "12 Codex rollout(s)" in result.summary
    assert "draining" in result.summary


def test_warns_once_it_has_been_stuck_for_over_an_hour(D):
    result = D._check_data_codex_ingest_backlog(
        _state(_record(files=9, byte_count=1234, minutes_ago=125)))
    assert result.severity == "warn"
    assert "9 Codex rollout(s)" in result.summary
    assert "1234 byte(s)" in result.summary
    assert result.remediation == "Run `cctally cache-sync --source codex`"
    assert result.details["age_s"] == 125 * 60


def test_the_boundary_is_strictly_over_the_interval(D):
    """Exactly one hour is still draining; a second past it is stuck.

    Pinned because an off-by-one here is invisible: both spellings look
    reasonable and neither fails anything else.
    """
    interval = D.CODEX_INGEST_BACKLOG_STUCK_SECONDS
    at = _state({"files": 1, "bytes": 1,
                 "since": (NOW - dt.timedelta(seconds=interval)).isoformat()
                 .replace("+00:00", "Z")})
    past = _state({"files": 1, "bytes": 1,
                   "since": (NOW - dt.timedelta(seconds=interval + 1))
                   .isoformat().replace("+00:00", "Z")})
    assert D._check_data_codex_ingest_backlog(at).severity == "ok"
    assert D._check_data_codex_ingest_backlog(past).severity == "warn"


def test_a_missing_or_junk_since_never_warns(D):
    """Fail toward silence on a malformed record.

    The record is a health signal, not evidence; a hand-edited or truncated one
    must not manufacture a WARN the operator cannot act on.
    """
    for record in (
        {"files": 3, "bytes": 10},
        {"files": 3, "bytes": 10, "since": "not-a-timestamp"},
        {"files": "many", "bytes": None, "since": None},
    ):
        result = D._check_data_codex_ingest_backlog(_state(record))
        assert result.severity == "ok"


def test_the_leg_is_registered_in_the_data_category(D):
    """Registration is what makes the leg reachable at all.

    A check function nobody lists evaluates to nothing and fails nothing.
    """
    registered = {
        check_id
        for _group, _title, checks in D._CATEGORY_DEFINITIONS
        for check_id, _fn in checks
    }
    assert "data.codex_ingest_backlog" in registered


def test_a_stuck_backlog_never_reaches_fail(D):
    """Per the CLI contract a WARN alone does not change `doctor`'s exit code,
    which is exactly why this leg may warn at all."""
    result = D._check_data_codex_ingest_backlog(
        _state(_record(files=4, minutes_ago=999)))
    assert result.severity == "warn"
    assert result.severity != "fail"


# ── the cache write and the gather ─────────────────────────────────────────

def test_the_gather_carries_the_record_across(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    import _cctally_doctor

    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES (?, ?)",
            ("codex_ingest_backlog", json.dumps(
                {"files": 5, "bytes": 900, "since": "2026-07-31T09:00:00Z"})))
        conn.commit()
    finally:
        conn.close()

    state = _cctally_doctor.doctor_gather_state(
        now_utc=NOW, deep=False)
    assert state.codex_ingest_backlog == {
        "files": 5, "bytes": 900, "since": "2026-07-31T09:00:00Z"}


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
