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


# ===========================================================================
# #269 M0.3 — `reconcile_weekref_cache` (signature-driven invalidation)
# ===========================================================================
def test_reconcile_weekref_evicts_late_ingest_week(tmp_cache):
    import _lib_snapshot_cache as sc

    sc.reset_weekref_cost_state()
    # Two cached closed weeks.
    wk_old = (
        dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc),
    )
    wk_new = (
        dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc),
    )
    sc._WEEKREF_COST_CACHE[sc._weekref_key(*wk_old)] = 1.0
    sc._WEEKREF_COST_CACHE[sc._weekref_key(*wk_new)] = 2.0
    last = _max_id(tmp_cache)
    sc._WEEKREF_COST_LAST_SEEN.update(max_id=last, reset_sig=(0, 0))
    # Late-ingest a row (new id) with an OLD timestamp inside wk_new.
    _insert_session_entry(tmp_cache, ts="2026-06-25T10:00:00Z")
    sc.reconcile_weekref_cache(
        tmp_cache, max_entry_id=_max_id(tmp_cache), reset_sig=(0, 0)
    )
    assert sc._weekref_key(*wk_new) not in sc._WEEKREF_COST_CACHE  # evicted
    assert sc._weekref_key(*wk_old) in sc._WEEKREF_COST_CACHE  # untouched


def test_reconcile_weekref_full_clear_on_reset_sig(tmp_cache):
    import _lib_snapshot_cache as sc

    sc.reset_weekref_cost_state()
    sc._WEEKREF_COST_CACHE[("s", "e")] = 3.0
    sc._WEEKREF_COST_LAST_SEEN.update(max_id=_max_id(tmp_cache), reset_sig=(1, 1))
    sc.reconcile_weekref_cache(
        tmp_cache, max_entry_id=_max_id(tmp_cache), reset_sig=(2, 2)
    )
    assert sc._WEEKREF_COST_CACHE == {}  # reset/credit reshaped a past week


def test_reconcile_weekref_full_clear_on_maxid_regression(tmp_cache):
    import _lib_snapshot_cache as sc

    sc.reset_weekref_cost_state()
    sc._WEEKREF_COST_CACHE[("s", "e")] = 3.0
    sc._WEEKREF_COST_LAST_SEEN.update(max_id=100, reset_sig=(0, 0))
    sc.reconcile_weekref_cache(tmp_cache, max_entry_id=5, reset_sig=(0, 0))  # rebuilt
    assert sc._WEEKREF_COST_CACHE == {}


def test_reconcile_weekref_boundary_equal_is_evicted(tmp_cache):
    """Codex-1: inclusive [start,end] means an entry AT week_end belongs to
    the week, so eviction is >= not >."""
    import _lib_snapshot_cache as sc

    sc.reset_weekref_cost_state()
    we = dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc)
    wk = (dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc), we)
    sc._WEEKREF_COST_CACHE[sc._weekref_key(*wk)] = 2.0
    last = _max_id(tmp_cache)
    sc._WEEKREF_COST_LAST_SEEN.update(max_id=last, reset_sig=(0, 0))
    _insert_session_entry(tmp_cache, ts="2026-06-29T00:00:00Z")  # exactly week_end
    sc.reconcile_weekref_cache(
        tmp_cache, max_entry_id=_max_id(tmp_cache), reset_sig=(0, 0)
    )
    assert sc._weekref_key(*wk) not in sc._WEEKREF_COST_CACHE  # >= evicts it


def test_reconcile_weekref_cold_records_last_seen(tmp_cache):
    """First (cold) call just records last-seen and touches no cache entry."""
    import _lib_snapshot_cache as sc

    sc.reset_weekref_cost_state()
    sc._WEEKREF_COST_CACHE[("s", "e")] = 5.0  # pre-seeded, must survive the cold call
    sc.reconcile_weekref_cache(tmp_cache, max_entry_id=7, reset_sig=(3, 4))
    assert sc._WEEKREF_COST_LAST_SEEN == {"max_id": 7, "reset_sig": (3, 4)}
    assert sc._WEEKREF_COST_CACHE == {("s", "e"): 5.0}


def test_reconcile_weekref_idempotent_within_tick(tmp_cache):
    """After the first reconcile updates last-seen, a second call with the same
    signature sees no delta and evicts nothing (and never re-queries)."""
    import _lib_snapshot_cache as sc

    sc.reset_weekref_cost_state()
    wk = (
        dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc),
    )
    sc._WEEKREF_COST_CACHE[sc._weekref_key(*wk)] = 2.0
    last = _max_id(tmp_cache)
    sc._WEEKREF_COST_LAST_SEEN.update(max_id=last, reset_sig=(0, 0))
    _insert_session_entry(tmp_cache, ts="2026-07-04T10:00:00Z")  # in the future, keeps wk
    new_max = _max_id(tmp_cache)
    sc.reconcile_weekref_cache(tmp_cache, max_entry_id=new_max, reset_sig=(0, 0))
    assert sc._weekref_key(*wk) in sc._WEEKREF_COST_CACHE  # 2026-06-29 < 2026-07-04
    # Re-populate a would-be-dirty entry, then reconcile again with SAME sig:
    # no watermark re-query fires, so the entry survives (idempotent no-op).
    sc._WEEKREF_COST_CACHE[sc._weekref_key(*wk)] = 2.0
    sc.reconcile_weekref_cache(tmp_cache, max_entry_id=new_max, reset_sig=(0, 0))
    assert sc._weekref_key(*wk) in sc._WEEKREF_COST_CACHE


# ===========================================================================
# #269 M4.2 — projects-envelope per-(project, week) cache infra
# (spec §14 Win 2). Mirrors the M0 weekref shape: bare module dicts (no lock —
# only the sync thread mutates), a per-builder last-seen, a `>=` watermark
# eviction, plus a `session_files_sig` attribution-backfill signal and a
# `max_wus_id` attribution-denominator full-clear (Codex-M4 P2).
# ===========================================================================
def _insert_session_file(conn, path, *, session_id="s", project_path="/p"):
    conn.execute(
        "INSERT OR REPLACE INTO session_files "
        "(path, session_id, project_path) VALUES (?, ?, ?)",
        (path, session_id, project_path),
    )
    conn.commit()


def test_reset_projects_env_state_clears():
    import _lib_snapshot_cache as sc

    sc._PROJECTS_ENV_WEEK_CACHE[("/p", "wk")] = ("agg",)
    sc._PROJECTS_ENV_WEEK_TOTALS["wk"] = 1.0
    sc._PROJECTS_ENV_LAST_SEEN["max_id"] = 5
    sc.reset_projects_env_state()
    assert sc._PROJECTS_ENV_WEEK_CACHE == {}
    assert sc._PROJECTS_ENV_WEEK_TOTALS == {}
    assert sc._PROJECTS_ENV_LAST_SEEN == {}


def test_session_files_sig_moves_on_row_add(tmp_cache):
    import _lib_snapshot_cache as sc

    s0 = sc.session_files_sig(tmp_cache)
    _insert_session_file(tmp_cache, "/f/a.jsonl")
    s1 = sc.session_files_sig(tmp_cache)
    assert s1 != s0 and s1[0] == s0[0] + 1
    # An in-place attribution backfill (UPDATE project_path, same row) moves the
    # count-signal not at all but keeps MAX(rowid) stable — the belt is the
    # count; add a second row to prove monotonic growth of both legs.
    _insert_session_file(tmp_cache, "/f/b.jsonl")
    s2 = sc.session_files_sig(tmp_cache)
    assert s2 != s1 and s2[1] >= s1[1]


def test_projects_env_week_put_get_roundtrip():
    import _lib_snapshot_cache as sc

    sc.reset_projects_env_state()
    wk = sc.projects_env_week_key(dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc))
    assert sc.projects_env_week_get(wk) is None  # miss before put
    sc.projects_env_week_put(wk, {"/p/a": ("agg-a",), "/p/b": ("agg-b",)}, 7.5)
    by_bp, total = sc.projects_env_week_get(wk)
    assert total == 7.5
    assert by_bp == {"/p/a": ("agg-a",), "/p/b": ("agg-b",)}
    # An empty computed week is a HIT (registry presence), not a miss.
    wk2 = sc.projects_env_week_key(dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc))
    sc.projects_env_week_put(wk2, {}, 0.0)
    by_bp2, total2 = sc.projects_env_week_get(wk2)
    assert by_bp2 == {} and total2 == 0.0


def test_reconcile_projects_env_cold_records_last_seen(tmp_cache):
    import _lib_snapshot_cache as sc

    sc.reset_projects_env_state()
    wk = sc.projects_env_week_key(dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc))
    sc.projects_env_week_put(wk, {"/p": ("a",)}, 1.0)  # survives the cold call
    sc.reconcile_projects_env_cache(
        tmp_cache, max_entry_id=7, max_wus_id=3, sf_sig=(2, 9),
    )
    assert sc._PROJECTS_ENV_LAST_SEEN == {"max_id": 7, "max_wus_id": 3, "sf_sig": (2, 9)}
    assert sc.projects_env_week_get(wk) is not None  # untouched by cold call


def test_reconcile_projects_env_keeps_cache_on_wus_change(tmp_cache):
    """#271 §9a: a `max_wus_id` bump (a `record-usage` write) must NOT full-clear
    the per-(project,week) cost cache (layer 2). Attribution stays fresh via the
    whole-envelope memo (layer 1, `_PROJECTS_ENV_MEMO`), which still keys on
    `max_wus_id` and misses on the bump; layer 2's `session_entries`-only cost
    aggregates are byte-safe to reuse across a WUS bump. (Before the clause drop
    this full-cleared — RED.)"""
    import _lib_snapshot_cache as sc

    sc.reset_projects_env_state()
    wk = sc.projects_env_week_key(dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc))
    mid = _max_id(tmp_cache)
    sc._PROJECTS_ENV_LAST_SEEN.update(max_id=mid, max_wus_id=3, sf_sig=(0, 0))
    sc.projects_env_week_put(wk, {"/p": ("a",)}, 1.0)
    # WUS bumps, entries/sf unchanged → the cost cache MUST survive.
    sc.reconcile_projects_env_cache(
        tmp_cache, max_entry_id=mid, max_wus_id=4, sf_sig=(0, 0),
    )
    assert sc.projects_env_week_get(wk) is not None


def test_projects_env_memo_key_includes_max_wus_id():
    """#271 §9 Codex-4 invariant: the whole-envelope memo (`_PROJECTS_ENV_MEMO`)
    MUST keep `max_wus_id` in its key, so a `record-usage` write busts the memo
    and attribution is recomputed fresh — even after Item 3 dropped `max_wus_id`
    from layer 2's (`reconcile_projects_env_cache`) full-clear. A regression that
    drops `max_wus_id` from the memo key goes RED here."""
    src = (
        pathlib.Path(__file__).resolve().parent.parent
        / "bin" / "_cctally_dashboard.py"
    ).read_text()
    assert "memo_key = (max_id, max_wus_id, cw_key, weeks_back)" in src


def test_reconcile_projects_env_full_clear_on_sf_sig(tmp_cache):
    import _lib_snapshot_cache as sc

    sc.reset_projects_env_state()
    wk = sc.projects_env_week_key(dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc))
    sc.projects_env_week_put(wk, {"/p": ("a",)}, 1.0)
    sc._PROJECTS_ENV_LAST_SEEN.update(max_id=_max_id(tmp_cache), max_wus_id=3, sf_sig=(1, 1))
    # session_files attribution backfill moved rows WITHOUT a new entry/WUS row.
    sc.reconcile_projects_env_cache(
        tmp_cache, max_entry_id=_max_id(tmp_cache), max_wus_id=3, sf_sig=(2, 5),
    )
    assert sc._PROJECTS_ENV_WEEK_CACHE == {} and sc._PROJECTS_ENV_WEEK_TOTALS == {}


def test_reconcile_projects_env_full_clear_on_maxid_regression(tmp_cache):
    import _lib_snapshot_cache as sc

    sc.reset_projects_env_state()
    wk = sc.projects_env_week_key(dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc))
    sc.projects_env_week_put(wk, {"/p": ("a",)}, 1.0)
    sc._PROJECTS_ENV_LAST_SEEN.update(max_id=100, max_wus_id=3, sf_sig=(0, 0))
    sc.reconcile_projects_env_cache(
        tmp_cache, max_entry_id=5, max_wus_id=3, sf_sig=(0, 0),  # cache.db rebuilt
    )
    assert sc._PROJECTS_ENV_WEEK_CACHE == {} and sc._PROJECTS_ENV_WEEK_TOTALS == {}


def test_reconcile_projects_env_evicts_late_ingest_week(tmp_cache):
    import _lib_snapshot_cache as sc

    sc.reset_projects_env_state()
    wk_old = sc.projects_env_week_key(dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc))
    wk_new = sc.projects_env_week_key(dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc))
    sc.projects_env_week_put(wk_old, {"/p": ("old",)}, 1.0)
    sc.projects_env_week_put(wk_new, {"/p": ("new",)}, 2.0)
    last = _max_id(tmp_cache)
    sc._PROJECTS_ENV_LAST_SEEN.update(max_id=last, max_wus_id=0, sf_sig=(0, 0))
    # Late-ingest a row with an OLD timestamp inside wk_new (2026-06-22..06-29).
    _insert_session_entry(tmp_cache, ts="2026-06-25T10:00:00Z")
    sc.reconcile_projects_env_cache(
        tmp_cache, max_entry_id=_max_id(tmp_cache), max_wus_id=0, sf_sig=(0, 0),
    )
    assert sc.projects_env_week_get(wk_new) is None      # week_end 06-29 >= wm
    assert sc.projects_env_week_get(wk_old) is not None   # week_end 06-22 < wm
    # The bucket rows for the evicted week are gone too.
    assert ("/p", wk_new) not in sc._PROJECTS_ENV_WEEK_CACHE
    assert ("/p", wk_old) in sc._PROJECTS_ENV_WEEK_CACHE


def test_reconcile_projects_env_boundary_equal_evicted(tmp_cache):
    """Codex-1 `>=` rule: a week whose end equals the watermark is over-evicted
    (byte-safe — forces a harmless recompute)."""
    import _lib_snapshot_cache as sc

    sc.reset_projects_env_state()
    wk = sc.projects_env_week_key(dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc))
    sc.projects_env_week_put(wk, {"/p": ("x",)}, 2.0)  # week_end = 2026-06-29
    last = _max_id(tmp_cache)
    sc._PROJECTS_ENV_LAST_SEEN.update(max_id=last, max_wus_id=0, sf_sig=(0, 0))
    _insert_session_entry(tmp_cache, ts="2026-06-29T00:00:00Z")  # exactly week_end
    sc.reconcile_projects_env_cache(
        tmp_cache, max_entry_id=_max_id(tmp_cache), max_wus_id=0, sf_sig=(0, 0),
    )
    assert sc.projects_env_week_get(wk) is None  # >= evicts it


def test_reconcile_projects_env_idempotent_within_tick(tmp_cache, monkeypatch):
    import _lib_snapshot_cache as sc

    sc.reset_projects_env_state()
    wk = sc.projects_env_week_key(dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc))
    sc.projects_env_week_put(wk, {"/p": ("x",)}, 2.0)
    last = _max_id(tmp_cache)
    sc._PROJECTS_ENV_LAST_SEEN.update(max_id=last, max_wus_id=0, sf_sig=(0, 0))
    _insert_session_entry(tmp_cache, ts="2026-07-04T10:00:00Z")  # future — keeps wk
    new_max = _max_id(tmp_cache)
    real_nmt = sc.new_min_timestamp
    calls = {"n": 0}

    def spy(conn, last_id):
        calls["n"] += 1
        return real_nmt(conn, last_id)

    monkeypatch.setattr(sc, "new_min_timestamp", spy)
    sc.reconcile_projects_env_cache(tmp_cache, max_entry_id=new_max, max_wus_id=0, sf_sig=(0, 0))
    assert calls["n"] == 1 and sc.projects_env_week_get(wk) is not None  # 06-29 < 07-04
    # Second call, SAME signature: no watermark re-query, entry survives.
    sc.reconcile_projects_env_cache(tmp_cache, max_entry_id=new_max, max_wus_id=0, sf_sig=(0, 0))
    assert calls["n"] == 1


# ===========================================================================
# #271 M1 — incremental current-bucket floor
#
# Shared helpers: a FULL-production-schema cache.db (so ``iter_entries`` and
# its delta sibling run against the real column/index shape, incl.
# ``idx_entries_timestamp`` and the ``speed``/``cost_usd_raw`` columns), plus
# a plain ISO→aware-UTC parser and a ``(id, UsageEntry)`` row builder for the
# pure accumulator tests (no DB).
# ===========================================================================
def _make_cache_db(tmp_path, name="cache271.db"):
    """A cache.db carrying the real production ``session_entries`` schema.

    Uses ``_cctally_db._apply_cache_schema`` (the single schema source) so the
    table has ``id INTEGER PRIMARY KEY AUTOINCREMENT``, ``speed`` /
    ``cost_usd_raw``, and ``idx_entries_timestamp`` — everything
    ``iter_entries`` / ``iter_entries_with_id`` read and the query-plan test
    depends on.
    """
    import _cctally_db as _db

    conn = sqlite3.connect(tmp_path / name)
    _db._apply_cache_schema(conn)
    conn.commit()
    return conn


def _insert_entry(conn, *, ts, model="claude-opus-4-8", input=0, output=0,
                  cache_create=0, cache_read=0, speed=None, cost_usd=None,
                  source="/p/a.jsonl"):
    """Insert one ``session_entries`` row (auto-incrementing ``line_offset``).

    ``timestamp_utc`` is stored in the SAME normalized form ``sync_cache`` uses
    (``.astimezone(utc).isoformat()`` → the ``+00:00`` suffix, not ``Z``), so
    the lexical ``timestamp_utc > ?`` / ``BETWEEN`` string comparisons behave
    identically to production.
    """
    off = conn.execute(
        "SELECT COALESCE(MAX(line_offset), -1) + 1 FROM session_entries"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, speed, cost_usd_raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, off, _dt(ts).isoformat(), model, input, output, cache_create,
         cache_read, speed, cost_usd),
    )
    conn.commit()


def _dt(iso):
    """Parse an ISO-8601 string (``…Z`` or offset) to an aware-UTC datetime."""
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(
        dt.timezone.utc
    )


def _entry(ts, model="claude-opus-4-8", *, input=0, output=0, cache_create=0,
           cache_read=0, speed=None, cost_usd=None, source="/p/a.jsonl"):
    """Build one in-memory ``UsageEntry`` (no DB) for the pure fold/accumulator
    tests. ``ts`` is any ISO string; the ``speed`` key is added only when set,
    mirroring ``iter_entries``' materialization."""
    from _lib_jsonl import UsageEntry

    usage = {
        "input_tokens": input,
        "output_tokens": output,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read,
    }
    if speed is not None:
        usage["speed"] = speed
    return UsageEntry(timestamp=_dt(ts), model=model, usage=usage,
                      cost_usd=cost_usd, source_path=source)


def _entries(specs):
    """Build a list of ``UsageEntry`` from ``(ts, model, input, output, cc, cr,
    speed, cost_usd)`` tuples."""
    return [
        _entry(ts, model, input=i, output=o, cache_create=cc, cache_read=cr,
               speed=sp, cost_usd=cu)
        for (ts, model, i, o, cc, cr, sp, cu) in specs
    ]


# --- Task 2: shared per-entry fold primitive -------------------------------
def test_fold_primitive_matches_aggregate_buckets():
    """A hand-built entry list folded via ``_new_bucket_acc``/``_fold_entry``/
    ``_finalize_bucket`` equals ``_aggregate_buckets`` output byte-for-byte
    (exact cost float, models first-seen order, model_breakdowns) (#271 §6)."""
    import _lib_aggregators as agg

    entries = _entries([  # (ts, model, input, output, cc, cr, speed, cost_usd)
        ("2026-07-01T00:00:00Z", "claude-opus-4-8", 10, 5, 0, 0, None, None),
        ("2026-07-01T01:00:00Z", "claude-sonnet-4-5", 3, 1, 0, 0, "fast", None),
        ("2026-07-01T02:00:00Z", "claude-opus-4-8", 7, 2, 0, 0, None, None),
        ("2026-07-01T03:00:00Z", "<synthetic>", 99, 99, 0, 0, None, None),  # skipped
    ])
    full = agg._aggregate_buckets(entries, key_fn=lambda e: "2026-07-01", mode="auto")
    acc = agg._new_bucket_acc()
    for e in entries:
        agg._fold_entry(acc, e, "auto")
    got = agg._finalize_bucket("2026-07-01", acc)
    assert got == full[0]  # exact dataclass equality


# --- Task 3: CurrentBucketAccumulator + the pure tick algorithm ------------
def _rows(*specs):
    """Build ``(id, UsageEntry)`` rows from ``(id, ts_iso, model, input)``."""
    return [(i, _entry(ts, model, input=inp)) for (i, ts, model, inp) in specs]


def _acc_call(prior, *, label, now, max_id, all_rows, delta_rows,
              member=lambda e: True):
    import _lib_snapshot_cache as sc

    return sc.accumulate_current_bucket(
        prior, current_label=label, cur_now=_dt(now), cur_max_id=max_id,
        fetch_all=lambda: all_rows,
        fetch_delta=lambda aid, ats: [
            (i, e) for (i, e) in delta_rows if i > aid or e.timestamp > ats
        ],
        membership=member, mode="auto")


def test_accumulate_cold_folds_all():
    rows = _rows((1, "2026-07-01T00:00:00Z", "claude-opus-4-8", 10),
                 (2, "2026-07-01T01:00:00Z", "claude-opus-4-8", 5))
    bucket, acc = _acc_call(None, label="2026-07-01", now="2026-07-01T02:00:00Z",
                            max_id=2, all_rows=rows, delta_rows=[])
    assert bucket.input_tokens == 15
    assert acc.tail == (rows[1][1].timestamp, 2)
    assert acc.last_seen_id == 2


def test_accumulate_empty_delta_reuses():
    rows = _rows((1, "2026-07-01T00:00:00Z", "claude-opus-4-8", 10))
    _, prior = _acc_call(None, label="2026-07-01", now="2026-07-01T01:00:00Z",
                         max_id=1, all_rows=rows, delta_rows=[])
    bucket, acc = _acc_call(prior, label="2026-07-01", now="2026-07-01T02:00:00Z",
                            max_id=1, all_rows=rows, delta_rows=[])
    assert bucket.input_tokens == 10  # unchanged, from cached acc


def test_accumulate_append_after_tail():
    r1 = _rows((1, "2026-07-01T00:00:00Z", "claude-opus-4-8", 10))
    _, prior = _acc_call(None, label="2026-07-01", now="2026-07-01T00:30:00Z",
                         max_id=1, all_rows=r1, delta_rows=[])
    new = _rows((2, "2026-07-01T01:00:00Z", "claude-opus-4-8", 5))
    bucket, acc = _acc_call(prior, label="2026-07-01", now="2026-07-01T01:30:00Z",
                            max_id=2, all_rows=r1 + new, delta_rows=new)
    assert bucket.input_tokens == 15
    assert acc.tail == (new[0][1].timestamp, 2)


def test_accumulate_mid_bucket_late_ingest_falls_back():
    r1 = _rows((1, "2026-07-01T02:00:00Z", "claude-opus-4-8", 10))
    _, prior = _acc_call(None, label="2026-07-01", now="2026-07-01T02:30:00Z",
                         max_id=1, all_rows=r1, delta_rows=[])
    late = _rows((2, "2026-07-01T01:00:00Z", "claude-opus-4-8", 5))  # ts BEFORE tail
    allrows = _rows((2, "2026-07-01T01:00:00Z", "claude-opus-4-8", 5),
                    (1, "2026-07-01T02:00:00Z", "claude-opus-4-8", 10))  # (ts,id) order
    bucket, acc = _acc_call(prior, label="2026-07-01", now="2026-07-01T02:40:00Z",
                            max_id=2, all_rows=allrows, delta_rows=late)
    assert bucket.input_tokens == 15  # full recompute, correct total
    assert acc.tail == (r1[0][1].timestamp, 1)  # tail = max (ts,id) = the 02:00 row


def test_accumulate_now_advances_past_ingested_row():
    # A row already ingested (id=1) but with ts AFTER prior now; MAX(id) unchanged.
    r1 = _rows((1, "2026-07-01T05:00:00Z", "claude-opus-4-8", 10))
    _, prior = _acc_call(None, label="2026-07-01", now="2026-07-01T04:00:00Z",
                         max_id=1, all_rows=[], delta_rows=[])  # not yet reached: empty
    bucket, acc = _acc_call(prior, label="2026-07-01", now="2026-07-01T06:00:00Z",
                            max_id=1, all_rows=r1, delta_rows=r1)  # ts>last_now leg catches it
    assert bucket is not None and bucket.input_tokens == 10


def test_accumulate_synthetic_skipped_does_not_advance_tail():
    r1 = _rows((1, "2026-07-01T00:00:00Z", "claude-opus-4-8", 10))
    _, prior = _acc_call(None, label="2026-07-01", now="2026-07-01T00:30:00Z",
                         max_id=1, all_rows=r1, delta_rows=[])
    syn = _rows((2, "2026-07-01T01:00:00Z", "<synthetic>", 99))
    bucket, acc = _acc_call(prior, label="2026-07-01", now="2026-07-01T01:30:00Z",
                            max_id=2, all_rows=r1 + syn, delta_rows=syn)
    assert bucket.input_tokens == 10
    assert acc.tail == (r1[0][1].timestamp, 1)  # unchanged; synthetic never folded


def test_accumulate_backward_now_cold_refolds():
    """#271 M1 review (Minor 1): a backward wall-clock step (`cur_now <
    prior.last_now`, e.g. an NTP adjustment) must force a cold refold. The
    current-bucket window is clamped to `now`, so reusing the larger prior fold
    set over an empty delta would OVER-count rows now beyond the shrunken
    `[start, cur_now]` window. A cold refold matches from-scratch byte-for-byte.

    Non-vacuous: without the `cur_now < prior.last_now` trigger the empty-delta
    fast path reuses `prior.acc` → input 15 (over-count) → RED."""
    a = _rows((1, "2026-07-01T00:30:00Z", "claude-opus-4-8", 10))  # ts <= T1
    b = _rows((2, "2026-07-01T01:30:00Z", "claude-opus-4-8", 5))   # ts in (T1, T2]
    # Warm at now=T2 (02:00): both rows folded → input 15, tail = the 01:30 row.
    warm, prior = _acc_call(None, label="2026-07-01", now="2026-07-01T02:00:00Z",
                            max_id=2, all_rows=a + b, delta_rows=[])
    assert warm.input_tokens == 15
    # Backward tick: now=T1 (01:00 < 02:00). fetch_all over [start, T1] returns
    # only row A (row B's 01:30 ts is now beyond the window); no new delta.
    bucket, acc = _acc_call(prior, label="2026-07-01", now="2026-07-01T01:00:00Z",
                            max_id=2, all_rows=a, delta_rows=[])
    assert bucket.input_tokens == 10  # excludes row B — matches from-scratch [start, T1]
    assert acc.tail == (a[0][1].timestamp, 1)
    assert acc.last_now == _dt("2026-07-01T01:00:00Z")


def test_accumulate_published_bucket_not_mutated_by_later_append():
    """#271 F7 (spec §10): a published current-bucket `BucketUsage` must not be
    mutated by a later appending tick. `_finalize_bucket` copies `models` /
    `model_breakdowns`, so the earlier row stays byte-frozen even though the
    accumulator keeps folding into the same `acc` dict in place.

    Non-vacuous: if `_finalize_bucket` shared `acc["models_order"]` instead of
    copying it, the append (a new distinct model) would grow the published row's
    `models` list → the captured snapshot would differ → RED."""
    import copy

    r1 = _rows((1, "2026-07-01T00:00:00Z", "claude-opus-4-8", 10))
    published, prior = _acc_call(None, label="2026-07-01", now="2026-07-01T00:30:00Z",
                                 max_id=1, all_rows=r1, delta_rows=[])
    snapshot = copy.deepcopy(published)
    # Append a second row (a DIFFERENT model) in a later tick — mutates prior.acc
    # in place, growing acc["models_order"] to two entries.
    new = _rows((2, "2026-07-01T01:00:00Z", "claude-sonnet-4-5", 5))
    bucket2, _ = _acc_call(prior, label="2026-07-01", now="2026-07-01T01:30:00Z",
                           max_id=2, all_rows=r1 + new, delta_rows=new)
    assert bucket2.input_tokens == 15                 # the new tick grew
    assert bucket2.models == ["claude-opus-4-8", "claude-sonnet-4-5"]
    assert published == snapshot                      # earlier published row is byte-unchanged
    assert published.models == ["claude-opus-4-8"]    # not torn by the append


# --- Task 4: the (id, UsageEntry) delta fetch sibling ----------------------
def test_iter_entries_with_id_delta(tmp_path):
    """``iter_entries_with_id`` yields ``(id, UsageEntry)`` and restricts to the
    ``(id > after_id OR timestamp_utc > after_ts)`` delta (#271 §7d)."""
    import _cctally_cache as cache

    conn = _make_cache_db(tmp_path)
    try:
        _insert_entry(conn, ts="2026-07-01T00:00:00Z", input=1)  # id 1
        _insert_entry(conn, ts="2026-07-01T01:00:00Z", input=2)  # id 2
        _insert_entry(conn, ts="2026-07-01T02:00:00Z", input=3)  # id 3
        lo, hi = _dt("2026-07-01T00:00:00Z"), _dt("2026-07-01T23:00:00Z")
        # after_id=1, after_ts far-future → only id>1 (the id leg).
        got = cache.iter_entries_with_id(
            conn, lo, hi, after_id=1, after_ts=_dt("2100-01-01T00:00:00Z")
        )
        assert [i for i, _ in got] == [2, 3]
        # after_id huge, after_ts=01:00 → only ts>01:00 (the ts leg, id 3).
        got2 = cache.iter_entries_with_id(
            conn, lo, hi, after_id=10**9, after_ts=_dt("2026-07-01T01:00:00Z")
        )
        assert [i for i, _ in got2] == [3]
        # No after-predicate → the whole window in (timestamp_utc, id) order.
        allrows = cache.iter_entries_with_id(conn, lo, hi)
        assert [i for i, _ in allrows] == [1, 2, 3]
        # The UsageEntry side is fully built (tokens + timestamp).
        assert allrows[0][1].usage["input_tokens"] == 1
        assert allrows[2][1].timestamp == _dt("2026-07-01T02:00:00Z")
    finally:
        conn.close()


def test_iter_entries_with_id_query_plan_is_indexed(tmp_path):
    """The delta predicate stays index-driven — not a bare full-table scan
    (#271 §7d / Codex-2)."""
    import _cctally_cache as cache  # noqa: F401 (schema/module parity)

    conn = _make_cache_db(tmp_path)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT id, timestamp_utc FROM session_entries "
            "WHERE timestamp_utc >= ? AND timestamp_utc <= ? "
            "AND (id > ? OR timestamp_utc > ?) "
            "ORDER BY timestamp_utc ASC, id ASC",
            ("2026-07-01T00:00:00+00:00", "2026-07-01T23:00:00+00:00", 1,
             "2026-07-01T01:00:00+00:00"),
        ).fetchall()
        text = " ".join(str(r) for r in plan).upper()
        assert "SCAN SESSION_ENTRIES" not in text or "USING INDEX" in text
    finally:
        conn.close()


# --- Task 5: current_override hook + accumulator wiring --------------------
def _bucket(label, *, input=0):
    """A minimal ``BucketUsage`` for override/wiring tests."""
    from _lib_aggregators import BucketUsage

    return BucketUsage(
        bucket=label, input_tokens=input, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0, total_tokens=input,
        cost_usd=0.0, models=[], model_breakdowns=[])


def _daily_end_of(label):
    d = dt.date.fromisoformat(label)
    return (dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
            + dt.timedelta(days=2))


def test_cached_buckets_current_override():
    """When ``current_override`` is supplied, the current label is served from
    it and ``aggregate_one`` is NOT called for it (#271 §8a)."""
    import _lib_snapshot_cache as sc

    calls = []

    def agg_one(label, entries):
        calls.append(label)
        return _bucket(label, input=1)

    out = sc.cached_buckets(
        "daily", cache=sc.BucketCache(),
        all_bucket_labels=["2026-06-30", "2026-07-01"], current_label="2026-07-01",
        dirty_predicate=lambda l: False,
        fetch_bucket_entries=lambda l: [], aggregate_one=agg_one,
        current_override=lambda: _bucket("2026-07-01", input=42))
    assert "2026-07-01" not in calls           # override used, not aggregate_one
    assert "2026-06-30" in calls               # past bucket still via aggregate_one
    assert out[-1].input_tokens == 42          # current comes from the override


def test_build_cached_group_a_accumulator_off_by_default(tmp_path):
    """The default path (``use_current_accumulator=False``) never engages the
    accumulator — the current label is computed via ``aggregate_one`` and the
    ``_GROUP_A_CURRENT`` holder stays empty (byte-identical to today)."""
    import _lib_snapshot_cache as sc

    sc.reset_group_a_state()
    conn = _make_cache_db(tmp_path)
    try:
        _insert_entry(conn, ts="2026-07-04T09:00:00Z", input=3)
        labels = ["2026-07-03", "2026-07-04"]
        calls = []

        def agg_one(label, entries):
            calls.append(label)
            return _bucket(label, input=len(entries))

        out = sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=_daily_end_of,
            fetch_bucket_entries=lambda l: [1] if l == "2026-07-04" else [],
            aggregate_one=agg_one, extra_signature=("daily", "local"),
            use_current_accumulator=False)
        assert "2026-07-04" in calls          # current computed via aggregate_one
        assert sc._GROUP_A_CURRENT == {}       # accumulator holder untouched
        assert out[-1].input_tokens == 1
    finally:
        conn.close()


# --- Task 1: iter_entries fold-order tie-break -----------------------------
def test_iter_entries_order_is_timestamp_then_id(tmp_path):
    """``iter_entries`` returns rows in ``(timestamp_utc, id)`` ascending order,
    equal-timestamp ties broken by ``id`` (#271 §5 / Codex-3)."""
    import _cctally_cache as cache

    conn = _make_cache_db(tmp_path)
    try:
        # Two rows with the SAME timestamp, inserted in id order 1 then 2, plus
        # an earlier-timestamp row inserted LAST (id=3).
        _insert_entry(conn, ts="2026-07-01T00:00:00Z", input=1)   # id=1
        _insert_entry(conn, ts="2026-07-01T00:00:00Z", input=2)   # id=2
        _insert_entry(conn, ts="2026-06-30T23:59:59Z", input=3)   # id=3 (earlier ts)
        rows = cache.iter_entries(
            conn, _dt("2026-06-01T00:00:00Z"), _dt("2026-07-31T00:00:00Z")
        )
        # Ascending by timestamp; the equal-ts pair ordered by id (1 then 2).
        assert [e.usage["input_tokens"] for e in rows] == [3, 1, 2]
    finally:
        conn.close()
