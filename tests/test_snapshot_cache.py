"""Unit tests for `bin/_lib_snapshot_cache.py` (#268 dashboard rebuild perf).

M0 foundation: the composite data-version signature, the new-entry
timestamp watermark, the module-level generation counter, and the two
cache holders (`BucketCache`, `SessionCache`).

These are pure-module tests: they build minimal temp `cache.db` /
`stats.db` schemas and pass explicit `sqlite3.Connection` objects into
`_lib_snapshot_cache`, so no fake HOME / `redirect_paths` machinery is
needed and there is zero risk of touching a real prod DB (the functions
under test never open a DB themselves; they read the conns handed in).
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys

import pytest

# bin/ is placed on sys.path by tests/conftest.py; import the module under
# test directly.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))


# --- Minimal schema builders (column-compatible with production DDL) -------
#
# Only the columns the signature / watermark read are load-bearing; the rest
# mirror production so inserts key identically. `id INTEGER PRIMARY KEY
# AUTOINCREMENT` matches bin/_cctally_db.py; the reset tables' `rowid`
# aliases `id` (plan's `MAX(rowid)` leg).
_CACHE_SCHEMA = """
CREATE TABLE session_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path         TEXT    NOT NULL,
    line_offset         INTEGER NOT NULL,
    timestamp_utc       TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_create_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE codex_session_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path   TEXT    NOT NULL,
    line_offset   INTEGER NOT NULL,
    timestamp_utc TEXT    NOT NULL,
    session_id    TEXT    NOT NULL,
    model         TEXT    NOT NULL
);
"""

_STATS_SCHEMA = """
CREATE TABLE weekly_usage_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    week_start_date TEXT NOT NULL,
    week_end_date   TEXT NOT NULL,
    weekly_percent  REAL NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE weekly_cost_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    week_start_date TEXT NOT NULL,
    week_end_date   TEXT NOT NULL,
    cost_usd        REAL NOT NULL
);
CREATE TABLE week_reset_events (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at_utc        TEXT NOT NULL,
    old_week_end_at        TEXT NOT NULL,
    new_week_end_at        TEXT NOT NULL,
    effective_reset_at_utc TEXT NOT NULL,
    UNIQUE(old_week_end_at, new_week_end_at)
);
CREATE TABLE weekly_credit_floors (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start_date         TEXT NOT NULL,
    effective_at_utc        TEXT NOT NULL,
    observed_pre_credit_pct REAL NOT NULL,
    applied_at_utc          TEXT NOT NULL,
    UNIQUE(week_start_date, effective_at_utc)
);
"""


@pytest.fixture
def tmp_cache(tmp_path):
    conn = sqlite3.connect(tmp_path / "cache.db")
    conn.executescript(_CACHE_SCHEMA)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def tmp_stats(tmp_path):
    conn = sqlite3.connect(tmp_path / "stats.db")
    conn.executescript(_STATS_SCHEMA)
    conn.commit()
    yield conn
    conn.close()


# --- Row-insert helpers ----------------------------------------------------
def _insert_session_entry(conn, ts, *, model="claude-opus-4-8", source="/p/a.jsonl"):
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model) VALUES (?, ?, ?, ?)",
        (source, 0, ts, model),
    )
    conn.commit()


def _insert_codex_entry(conn, ts, *, source="/c/a.jsonl"):
    conn.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, 0, ts, "sess", "gpt-5"),
    )
    conn.commit()


def _insert_weekly_usage_snapshot(conn):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, weekly_percent) "
        "VALUES ('2026-07-01T00:00:00Z', '2026-06-29', '2026-07-06', 12.0)"
    )
    conn.commit()


def _insert_weekly_cost_snapshot(conn):
    conn.execute(
        "INSERT INTO weekly_cost_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, cost_usd) "
        "VALUES ('2026-07-01T00:00:00Z', '2026-06-29', '2026-07-06', 4.2)"
    )
    conn.commit()


def _insert_reset_event(conn):
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, effective_reset_at_utc) "
        "VALUES ('2026-07-02T10:00:00Z', '2026-07-06T00:00:00Z', "
        "'2026-07-08T00:00:00Z', '2026-07-02T10:00:00Z')"
    )
    conn.commit()


def _insert_credit_floor(conn):
    conn.execute(
        "INSERT INTO weekly_credit_floors "
        "(week_start_date, effective_at_utc, observed_pre_credit_pct, applied_at_utc) "
        "VALUES ('2026-06-29', '2026-07-02T10:00:00Z', 46.0, '2026-07-02T10:05:00Z')"
    )
    conn.commit()


def _max_id(conn):
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM session_entries").fetchone()[0])


# ===========================================================================
# Task 0.1 — composite data-version signature
# ===========================================================================
def test_signature_advances_per_table(tmp_cache, tmp_stats):
    from _lib_snapshot_cache import compute_signature

    s0 = compute_signature(tmp_cache, tmp_stats, generation=0)

    _insert_session_entry(tmp_cache, "2026-07-04T10:00:00Z")
    s1 = compute_signature(tmp_cache, tmp_stats, generation=0)
    assert s1 != s0 and s1.max_entry_id > s0.max_entry_id

    _insert_weekly_usage_snapshot(tmp_stats)  # no session_entries row
    s2 = compute_signature(tmp_cache, tmp_stats, generation=0)
    assert s2 != s1 and s2.max_wus_id > s1.max_wus_id

    _insert_weekly_cost_snapshot(tmp_stats)  # no session_entries row
    s2b = compute_signature(tmp_cache, tmp_stats, generation=0)
    assert s2b != s2 and s2b.max_wcs_id > s2.max_wcs_id

    _insert_reset_event(tmp_stats)
    s3 = compute_signature(tmp_cache, tmp_stats, generation=0)
    assert s3 != s2b and s3.reset_sig != s2b.reset_sig

    _insert_credit_floor(tmp_stats)  # credit floors also feed reset_sig
    s3b = compute_signature(tmp_cache, tmp_stats, generation=0)
    assert s3b != s3 and s3b.reset_sig != s3.reset_sig

    _insert_codex_entry(tmp_cache, "2026-07-04T11:00:00Z")
    s3c = compute_signature(tmp_cache, tmp_stats, generation=0)
    assert s3c != s3b and s3c.max_codex_id > s3b.max_codex_id

    s4 = compute_signature(tmp_cache, tmp_stats, generation=7)
    assert s4.generation == 7 and s4 != s3c  # generation is part of identity


def test_signature_stable_when_nothing_changes(tmp_cache, tmp_stats):
    from _lib_snapshot_cache import compute_signature

    _insert_session_entry(tmp_cache, "2026-07-04T10:00:00Z")
    _insert_weekly_usage_snapshot(tmp_stats)
    a = compute_signature(tmp_cache, tmp_stats, generation=3)
    b = compute_signature(tmp_cache, tmp_stats, generation=3)
    assert a == b  # NamedTuple value-equality


def test_signature_tolerates_missing_tables():
    """A partially-migrated / fresh DB with no cache tables returns 0 legs, no raise."""
    from _lib_snapshot_cache import compute_signature

    empty_cache = sqlite3.connect(":memory:")
    empty_stats = sqlite3.connect(":memory:")
    try:
        sig = compute_signature(empty_cache, empty_stats, generation=0)
        assert sig.max_entry_id == 0
        assert sig.max_wus_id == 0
        assert sig.max_wcs_id == 0
        assert sig.reset_sig == (0, 0)
        assert sig.max_codex_id == 0
        assert sig.generation == 0
    finally:
        empty_cache.close()
        empty_stats.close()


# ===========================================================================
# Task 0.2 — new-entry timestamp watermark
# ===========================================================================
def test_watermark_reaches_back_for_late_ingest(tmp_cache):
    """A late-ingested row (new id, OLD timestamp) makes the watermark reach
    back to the OLD event time, not "now" — because session_entries.id is
    ingest order, not event time (spec §3 / Codex F1)."""
    from _lib_snapshot_cache import new_min_timestamp

    last = _max_id(tmp_cache)
    _insert_session_entry(tmp_cache, "2026-06-27T18:00:00Z")  # id > last, past ts
    wm = new_min_timestamp(tmp_cache, last)
    assert wm == dt.datetime(2026, 6, 27, 18, 0, tzinfo=dt.timezone.utc)
    # No genuinely-new rows past the current max → None.
    assert new_min_timestamp(tmp_cache, _max_id(tmp_cache)) is None


def test_watermark_is_min_over_new_rows_only(tmp_cache):
    """Watermark is the EARLIEST event time among rows with id > last_seen,
    ignoring older ids and unaffected by insertion order."""
    from _lib_snapshot_cache import new_min_timestamp

    _insert_session_entry(tmp_cache, "2026-07-04T12:00:00Z")  # "already seen"
    last = _max_id(tmp_cache)
    # Two new rows; the SECOND-inserted carries the EARLIER event time.
    _insert_session_entry(tmp_cache, "2026-07-04T20:00:00Z")
    _insert_session_entry(tmp_cache, "2026-07-01T06:00:00Z")
    wm = new_min_timestamp(tmp_cache, last)
    assert wm == dt.datetime(2026, 7, 1, 6, 0, tzinfo=dt.timezone.utc)


def test_watermark_returns_aware_utc(tmp_cache):
    """Returned datetime is aware and normalized to UTC (spec §3)."""
    from _lib_snapshot_cache import new_min_timestamp

    _insert_session_entry(tmp_cache, "2026-06-27T18:00:00Z")
    wm = new_min_timestamp(tmp_cache, 0)
    assert wm is not None
    assert wm.tzinfo is not None
    assert wm.utcoffset() == dt.timedelta(0)


def test_watermark_none_on_empty(tmp_cache):
    from _lib_snapshot_cache import new_min_timestamp

    assert new_min_timestamp(tmp_cache, 0) is None
