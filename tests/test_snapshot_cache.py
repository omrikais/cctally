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
CREATE TABLE session_files (
    path             TEXT PRIMARY KEY,
    size_bytes       INTEGER NOT NULL DEFAULT 0,
    mtime_ns         INTEGER NOT NULL DEFAULT 0,
    last_byte_offset INTEGER NOT NULL DEFAULT 0,
    last_ingested_at TEXT    NOT NULL DEFAULT '2026-07-04T00:00:00Z',
    session_id       TEXT,
    project_path     TEXT
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


# ===========================================================================
# Task 0.3 — generation counter
# ===========================================================================
def test_generation_counter_monotonic():
    import _lib_snapshot_cache as sc

    start = sc.current_generation()
    n1 = sc.bump_generation()
    assert n1 == start + 1
    assert sc.current_generation() == n1
    n2 = sc.bump_generation()
    assert n2 == n1 + 1
    assert sc.current_generation() == n2


def test_generation_feeds_signature(tmp_cache, tmp_stats):
    """A bump changes the composite signature even with no table change."""
    import _lib_snapshot_cache as sc

    g0 = sc.current_generation()
    s0 = sc.compute_signature(tmp_cache, tmp_stats, generation=g0)
    g1 = sc.bump_generation()
    s1 = sc.compute_signature(tmp_cache, tmp_stats, generation=g1)
    assert s1 != s0 and s1.generation == g1


# ===========================================================================
# Task 0.4 — Group A bucket cache holder
# ===========================================================================
def test_bucket_cache_put_get_roundtrip():
    from _lib_snapshot_cache import BucketCache

    bc = BucketCache()
    assert bc.get("daily", "2026-06-30") is None
    sentinel = object()
    bc.put("daily", "2026-06-30", sentinel)
    assert bc.get("daily", "2026-06-30") is sentinel
    # Distinct builder_key namespaces don't collide on same label.
    assert bc.get("monthly", "2026-06-30") is None
    other = object()
    bc.put("monthly", "2026-06", other)
    assert bc.get("monthly", "2026-06") is other
    assert bc.get("daily", "2026-06-30") is sentinel


def test_bucket_cache_clear_empties_all():
    from _lib_snapshot_cache import BucketCache

    bc = BucketCache()
    bc.put("daily", "2026-06-30", object())
    bc.put("weekly", "2026-06-29", object())
    bc.clear()
    assert bc.get("daily", "2026-06-30") is None
    assert bc.get("weekly", "2026-06-29") is None


def test_bucket_cache_drop_from_predicate_only_matching():
    from _lib_snapshot_cache import BucketCache

    bc = BucketCache()
    for label in ("2026-06-28", "2026-06-29", "2026-06-30"):
        bc.put("daily", label, object())
    bc.put("monthly", "2026-06", object())  # different builder, untouched
    # Drop daily buckets on/after 2026-06-29.
    bc.drop_from("daily", lambda label: label >= "2026-06-29")
    assert bc.get("daily", "2026-06-28") is not None
    assert bc.get("daily", "2026-06-29") is None
    assert bc.get("daily", "2026-06-30") is None
    assert bc.get("monthly", "2026-06") is not None  # other builder untouched


# ===========================================================================
# Task 0.5 — Group B session cache holder
# ===========================================================================
def test_session_cache_put_get_all_roundtrip():
    from _lib_snapshot_cache import SessionCache

    sc = SessionCache()
    assert sc.get_all() == {}
    a, b = object(), object()
    sc.put("sess-a", a)
    sc.put("sess-b", b)
    allrows = sc.get_all()
    assert allrows == {"sess-a": a, "sess-b": b}


def test_session_cache_get_all_returns_copy():
    """get_all() hands back a copy so a caller's sort/truncate can't mutate
    the module-level store (spec §7 immutability discipline)."""
    from _lib_snapshot_cache import SessionCache

    sc = SessionCache()
    sc.put("sess-a", object())
    snapshot = sc.get_all()
    snapshot["injected"] = object()
    del snapshot["sess-a"]
    # Internal store is unaffected.
    assert set(sc.get_all().keys()) == {"sess-a"}


def test_session_cache_drop_subset():
    from _lib_snapshot_cache import SessionCache

    sc = SessionCache()
    for k in ("a", "b", "c"):
        sc.put(k, object())
    sc.drop({"a", "c"})
    assert set(sc.get_all().keys()) == {"b"}
    # Dropping an absent key is a no-op, not an error.
    sc.drop({"zzz"})
    assert set(sc.get_all().keys()) == {"b"}


def test_session_cache_clear():
    from _lib_snapshot_cache import SessionCache

    sc = SessionCache()
    sc.put("a", object())
    sc.clear()
    assert sc.get_all() == {}


# ===========================================================================
# Task 3.1 — changed-session resolution (join + filename-stem fallback)
# ===========================================================================
def _insert_session_file(conn, path, *, session_id=None, project_path=None):
    conn.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?, 0, 0, 0, ?, ?, ?)",
        (path, "2026-07-04T00:00:00Z", session_id, project_path),
    )
    conn.commit()


def _insert_entry_at_path(conn, ts, source, *, model="claude-opus-4-8"):
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model) VALUES (?, 0, ?, ?)",
        (source, ts, model),
    )
    conn.commit()


def test_affected_session_keys_join_and_stem_fallback(tmp_cache):
    """Resolves a session_files.session_id via the join AND the filename-stem
    fallback when the join yields null — keying IDENTICALLY to how
    `_aggregate_claude_sessions` groups (sf.session_id else basename-stem)."""
    from _lib_snapshot_cache import affected_session_keys

    # One session carries an explicit session_files.session_id (join path).
    _insert_session_file(tmp_cache, "/p/known.jsonl", session_id="SID-known")
    # One session_files row has a NULL session_id → filename-stem fallback.
    _insert_session_file(tmp_cache, "/p/orphan.jsonl", session_id=None)
    last = _max_id(tmp_cache)
    _insert_entry_at_path(tmp_cache, "2026-07-04T10:00:00Z", "/p/known.jsonl")
    _insert_entry_at_path(tmp_cache, "2026-07-04T11:00:00Z", "/p/orphan.jsonl")

    keys = affected_session_keys(tmp_cache, last)
    assert keys == {"SID-known", "orphan"}


def test_affected_session_keys_missing_session_files_row(tmp_cache):
    """A source_path with NO session_files row at all (LEFT JOIN → null)
    still resolves via the basename-stem fallback."""
    from _lib_snapshot_cache import affected_session_keys

    last = _max_id(tmp_cache)
    _insert_entry_at_path(tmp_cache, "2026-07-04T10:00:00Z", "/p/no-sf-row.jsonl")
    assert affected_session_keys(tmp_cache, last) == {"no-sf-row"}


def test_affected_session_keys_only_new_ids(tmp_cache):
    """Only entries with id > last_seen contribute; older ids are ignored."""
    from _lib_snapshot_cache import affected_session_keys

    _insert_entry_at_path(tmp_cache, "2026-07-01T10:00:00Z", "/p/old.jsonl")
    last = _max_id(tmp_cache)
    _insert_entry_at_path(tmp_cache, "2026-07-04T10:00:00Z", "/p/new.jsonl")
    assert affected_session_keys(tmp_cache, last) == {"new"}


def test_affected_session_keys_skips_synthetic(tmp_cache):
    """A `<synthetic>`-model entry never forms a session (mirrors the
    aggregator's skip), so it contributes no affected key."""
    from _lib_snapshot_cache import affected_session_keys

    last = _max_id(tmp_cache)
    _insert_entry_at_path(
        tmp_cache, "2026-07-04T10:00:00Z", "/p/syn.jsonl", model="<synthetic>"
    )
    assert affected_session_keys(tmp_cache, last) == set()


def test_affected_session_keys_empty_when_nothing_new(tmp_cache):
    from _lib_snapshot_cache import affected_session_keys

    _insert_entry_at_path(tmp_cache, "2026-07-04T10:00:00Z", "/p/a.jsonl")
    assert affected_session_keys(tmp_cache, _max_id(tmp_cache)) == set()


def test_affected_session_keys_tolerates_missing_tables():
    """Fresh DB with no session_entries table → empty set, no raise."""
    from _lib_snapshot_cache import affected_session_keys

    empty = sqlite3.connect(":memory:")
    try:
        assert affected_session_keys(empty, 0) == set()
    finally:
        empty.close()


# ===========================================================================
# Task 4.2 — doctor payload TTL memo
# ===========================================================================
_T0 = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)


def _make_compute():
    calls = {"n": 0}

    def compute(now, bind):
        calls["n"] += 1
        return {"severity": "ok", "n": calls["n"], "bind": bind}

    return compute, calls


def test_doctor_memo_computes_once_within_ttl():
    import _lib_snapshot_cache as sc

    sc.reset_doctor_memo()
    compute, calls = _make_compute()
    p1 = sc.doctor_payload_memo(_T0, "127.0.0.1", ttl_s=30, compute=compute)
    p2 = sc.doctor_payload_memo(
        _T0 + dt.timedelta(seconds=5), "127.0.0.1", ttl_s=30, compute=compute,
    )
    assert calls["n"] == 1, "back-to-back calls within the TTL must compute ONCE"
    assert p1 is p2  # same cached object


def test_doctor_memo_recomputes_after_ttl():
    import _lib_snapshot_cache as sc

    sc.reset_doctor_memo()
    compute, calls = _make_compute()
    sc.doctor_payload_memo(_T0, "127.0.0.1", ttl_s=30, compute=compute)
    p2 = sc.doctor_payload_memo(
        _T0 + dt.timedelta(seconds=31), "127.0.0.1", ttl_s=30, compute=compute,
    )
    assert calls["n"] == 2 and p2["n"] == 2


def test_doctor_memo_recomputes_on_runtime_bind_change():
    import _lib_snapshot_cache as sc

    sc.reset_doctor_memo()
    compute, calls = _make_compute()
    sc.doctor_payload_memo(_T0, "127.0.0.1", ttl_s=30, compute=compute)
    p2 = sc.doctor_payload_memo(_T0, "0.0.0.0", ttl_s=30, compute=compute)
    assert calls["n"] == 2 and p2["bind"] == "0.0.0.0"


def test_doctor_memo_recomputes_on_clock_regression():
    import _lib_snapshot_cache as sc

    sc.reset_doctor_memo()
    compute, calls = _make_compute()
    sc.doctor_payload_memo(_T0, "127.0.0.1", ttl_s=30, compute=compute)
    # A now_utc EARLIER than the cached compute time must recompute (never
    # serve a "future" cache).
    sc.doctor_payload_memo(
        _T0 - dt.timedelta(seconds=5), "127.0.0.1", ttl_s=30, compute=compute,
    )
    assert calls["n"] == 2


def test_reset_doctor_memo_forces_recompute():
    import _lib_snapshot_cache as sc

    sc.reset_doctor_memo()
    compute, calls = _make_compute()
    sc.doctor_payload_memo(_T0, "127.0.0.1", ttl_s=30, compute=compute)
    sc.reset_doctor_memo()
    sc.doctor_payload_memo(_T0, "127.0.0.1", ttl_s=30, compute=compute)
    assert calls["n"] == 2


# ===========================================================================
# #269 M0.1 — weekref-cost cache state + `_weekref_key` + reset
# ===========================================================================
def test_weekref_key_normalizes_to_utc_iso():
    import _lib_snapshot_cache as sc

    a = sc._weekref_key(
        dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc),
    )
    # A different tz that denotes the same instants keys identically.
    est = dt.timezone(dt.timedelta(hours=-4))
    b = sc._weekref_key(
        dt.datetime(2026, 6, 28, 20, tzinfo=est),
        dt.datetime(2026, 7, 5, 20, tzinfo=est),
    )
    assert a == b == (
        "2026-06-29T00:00:00+00:00",
        "2026-07-06T00:00:00+00:00",
    )


def test_reset_weekref_cost_state_clears():
    import _lib_snapshot_cache as sc

    sc._WEEKREF_COST_CACHE[("s", "e")] = 1.23
    sc._WEEKREF_COST_LAST_SEEN["max_id"] = 5
    sc.reset_weekref_cost_state()
    assert sc._WEEKREF_COST_CACHE == {} and sc._WEEKREF_COST_LAST_SEEN == {}


# ===========================================================================
# #269 M0.2 — `cached_weekref_cost` (open-vs-closed get-or-compute)
# ===========================================================================
def test_cached_weekref_cost_closed_caches_open_never():
    import _lib_snapshot_cache as sc

    sc.reset_weekref_cost_state()
    now = dt.datetime(2026, 7, 5, 12, tzinfo=dt.timezone.utc)
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return 4.0

    ws = dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)
    we = dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc)  # closed (we < now)
    assert sc.cached_weekref_cost(
        week_start_at=ws, week_end_at=we, now_utc=now, compute=compute
    ) == 4.0
    assert sc.cached_weekref_cost(
        week_start_at=ws, week_end_at=we, now_utc=now, compute=compute
    ) == 4.0
    assert calls["n"] == 1  # second call was a cache hit
    # open week (end > now) always recomputes, never stored
    ows = dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc)
    owe = dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)
    c2 = {"n": 0}

    def compute2():
        c2["n"] += 1
        return 9.0

    sc.cached_weekref_cost(
        week_start_at=ows, week_end_at=owe, now_utc=now, compute=compute2
    )
    sc.cached_weekref_cost(
        week_start_at=ows, week_end_at=owe, now_utc=now, compute=compute2
    )
    assert c2["n"] == 2  # never cached
    assert sc._weekref_key(ows, owe) not in sc._WEEKREF_COST_CACHE


def test_cached_weekref_cost_zero_is_a_hit():
    import _lib_snapshot_cache as sc

    sc.reset_weekref_cost_state()
    now = dt.datetime(2026, 7, 5, tzinfo=dt.timezone.utc)
    ws = dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)
    we = dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc)
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return 0.0  # a $0 week

    sc.cached_weekref_cost(
        week_start_at=ws, week_end_at=we, now_utc=now, compute=compute
    )
    sc.cached_weekref_cost(
        week_start_at=ws, week_end_at=we, now_utc=now, compute=compute
    )
    assert calls["n"] == 1  # 0.0 is a hit, not a miss
