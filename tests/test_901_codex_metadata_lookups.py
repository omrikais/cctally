"""#901 W2 (spec §5.2, T2, G2): the Codex file-alias and conversation-metadata
reads become indexed correlated lookups with identical rows.

``LEGACY_ALIAS_SQL`` and ``LEGACY_METADATA_CORE_SQL`` are the two statements
frozen verbatim from ``56e66f07a``. In the fixture, file ``a.jsonl``'s first
accounting row (minimum ``id``) is not its earliest (minimum ``timestamp_utc``),
so the accounting session and ``started_at`` must come from different rows; a
file and a thread with no accounting rows must still yield NULL; an entry under
another root must never join; equal sort keys exercise both tie-breakers.
"""
from __future__ import annotations

import sqlite3

import pytest

import _sql_plan_guard as guard
from conftest import load_script

LEGACY_ALIAS_SQL = (
    "SELECT f.source_root_key, f.path, f.last_native_thread_id, "
    "f.last_session_id, MIN(e.timestamp_utc) "
    "FROM codex_session_files AS f "
    "LEFT JOIN codex_session_entries AS e "
    "ON e.source_root_key=f.source_root_key AND e.source_path=f.path "
    "WHERE f.last_native_thread_id IS NOT NULL AND f.last_native_thread_id != '' "
    "GROUP BY f.source_root_key, f.path, f.last_native_thread_id, f.last_session_id "
    "ORDER BY f.last_ingested_at DESC, f.path DESC"
)
LEGACY_METADATA_CORE_SQL = (
    "WITH accounting AS ("
    " SELECT source_root_key, source_path, MIN(id) AS first_id,"
    " MIN(timestamp_utc) AS started_at"
    " FROM codex_session_entries"
    " GROUP BY source_root_key, source_path"
    ") "
    "SELECT t.source_root_key, t.source_path, t.native_thread_id, "
    "e.session_id AS accounting_session_id, "
    "CAST(t.cwd AS BLOB) AS cwd_blob, "
    "CAST(t.git_json AS BLOB) AS git_json_blob, "
    "a.started_at, t.last_seen_utc "
    "FROM codex_conversation_threads AS t "
    "LEFT JOIN accounting AS a "
    "ON a.source_root_key=t.source_root_key AND a.source_path=t.source_path "
    "LEFT JOIN codex_session_entries AS e ON e.id=a.first_id "
    "ORDER BY t.last_seen_utc DESC, t.conversation_key DESC"
)
ROOT = "r" * 32
OTHER_ROOT = "o" * 32
BASE = "/synthetic/w2"


@pytest.fixture
def cache_conn(tmp_path):
    load_script()
    import _cctally_db

    conn = sqlite3.connect(tmp_path / "cache.db")
    _cctally_db._apply_cache_schema(conn)
    conn.commit()
    _seed(conn)
    try:
        yield conn
    finally:
        conn.close()


def _seed(conn):
    conn.execute(
        "INSERT INTO codex_source_roots (source_root_key, canonical_root_path,"
        " first_seen_utc, last_seen_utc) VALUES (?,?,?,?)",
        (ROOT, BASE, "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"))
    files = (
        (f"{BASE}/a.jsonl", ROOT, "thread-a", "session-a",
         "2026-07-20T10:00:00Z"),
        (f"{BASE}/b.jsonl", ROOT, "thread-b", "session-b",
         "2026-07-20T11:00:00Z"),
        # The same instant as b.jsonl: path DESC breaks the tie.
        (f"{BASE}/c.jsonl", ROOT, "thread-c", "session-c",
         "2026-07-20T11:00:00Z"),
        (f"{BASE}/no-entries.jsonl", ROOT, "thread-d", "session-d",
         "2026-07-20T09:00:00Z"),
        (f"{BASE}/blank.jsonl", ROOT, "", "session-e", "2026-07-20T12:00:00Z"),
        (f"{BASE}/null.jsonl", ROOT, None, "session-f", "2026-07-20T12:30:00Z"),
        (f"{BASE}/other-root.jsonl", OTHER_ROOT, "thread-g", "session-g",
         "2026-07-20T08:00:00Z"),
    )
    conn.executemany(
        "INSERT INTO codex_session_files (path, size_bytes, mtime_ns,"
        " last_byte_offset, last_ingested_at, source_root_key,"
        " last_native_thread_id, last_session_id) VALUES (?,1,1,1,?,?,?,?)",
        [(path, ingested, root, thread, session)
         for path, root, thread, session, ingested in files])
    conn.executemany(
        "INSERT INTO codex_session_entries (source_path, line_offset,"
        " timestamp_utc, session_id, model, source_root_key)"
        " VALUES (?,?,?,?,'gpt-5',?)",
        (
            # a.jsonl: the first-inserted row is NOT the earliest one.
            (f"{BASE}/a.jsonl", 0, "2026-07-20T09:30:00Z", "acct-a1", ROOT),
            (f"{BASE}/a.jsonl", 1, "2026-07-20T09:00:00Z", "acct-a2", ROOT),
            (f"{BASE}/b.jsonl", 0, "2026-07-20T10:30:00Z", "acct-b", ROOT),
            (f"{BASE}/c.jsonl", 0, "2026-07-20T10:45:00Z", "acct-c", ROOT),
            # An entry on a.jsonl's path under another root never joins ROOT.
            (f"{BASE}/a.jsonl", 2, "2026-07-20T08:00:00Z", "acct-x",
             OTHER_ROOT),
            (f"{BASE}/other-root.jsonl", 0, "2026-07-20T07:00:00Z",
             "acct-g", OTHER_ROOT),
        ))
    conn.executemany(
        "INSERT INTO codex_conversation_threads (conversation_key,"
        " source_root_key, native_thread_id, root_thread_id, source_path,"
        " cwd, git_json, last_seen_utc) VALUES (?,?,?,?,?,?,?,?)",
        (
            ("conv-a", ROOT, "thread-a", "thread-a", f"{BASE}/a.jsonl",
             "/repo/a", None, "2026-07-20T10:00:00Z"),
            ("conv-b", ROOT, "thread-b", "thread-b", f"{BASE}/b.jsonl",
             "/repo/b", '{"branch":"main"}', "2026-07-20T11:00:00Z"),
            # Equal last_seen_utc: conversation_key DESC breaks the tie.
            ("conv-c", ROOT, "thread-c", "thread-c", f"{BASE}/c.jsonl",
             None, None, "2026-07-20T11:00:00Z"),
            ("conv-n", ROOT, "thread-n", "thread-n", f"{BASE}/no-entries.jsonl",
             "/repo/n", None, None),
            ("conv-g", OTHER_ROOT, "thread-g", "thread-g",
             f"{BASE}/other-root.jsonl", "/repo/g", "{not json",
             "2026-07-20T08:00:00Z"),
        ))
    conn.commit()


def test_alias_rows_are_identical_to_the_legacy_join(cache_conn):
    import _cctally_dashboard_sources as ds

    new = list(cache_conn.execute(ds._CODEX_FILE_ALIAS_SQL))
    assert new == list(cache_conn.execute(LEGACY_ALIAS_SQL))
    by_path = {row[1]: row for row in new}
    assert by_path[f"{BASE}/a.jsonl"][4] == "2026-07-20T09:00:00Z"
    assert by_path[f"{BASE}/no-entries.jsonl"][4] is None
    assert f"{BASE}/blank.jsonl" not in by_path
    assert f"{BASE}/null.jsonl" not in by_path
    assert [row[1] for row in new][:2] == [f"{BASE}/c.jsonl", f"{BASE}/b.jsonl"]


def test_metadata_rows_are_identical_to_the_legacy_cte(cache_conn):
    import _cctally_dashboard_sources as ds

    new = list(cache_conn.execute(ds._CODEX_CONVERSATION_METADATA_CORE_SQL))
    assert new == list(cache_conn.execute(LEGACY_METADATA_CORE_SQL))
    row_a = next(row for row in new if row[1] == f"{BASE}/a.jsonl")
    assert row_a[3] == "acct-a1", "accounting session = minimum id"
    assert row_a[6] == "2026-07-20T09:00:00Z", "started_at = minimum timestamp"
    row_n = next(row for row in new if row[1] == f"{BASE}/no-entries.jsonl")
    assert row_n[3] is None and row_n[6] is None
    assert [row[1] for row in new][:2] == [f"{BASE}/c.jsonl", f"{BASE}/b.jsonl"]


@pytest.mark.parametrize(
    "name", ["_CODEX_FILE_ALIAS_SQL", "_CODEX_CONVERSATION_METADATA_CORE_SQL"])
def test_both_reads_build_no_temp_structure(cache_conn, name):
    import _cctally_dashboard_sources as ds

    plan = [str(row[3]) for row in cache_conn.execute(
        "EXPLAIN QUERY PLAN " + getattr(ds, name))]
    assert not any(marker in detail for detail in plan
                   for marker in guard.TEMP_MARKERS), plan
    assert any("idx_codex_entries_root_path_time" in detail
               for detail in plan), plan


def test_the_metadata_read_is_identical_with_the_legacy_sql(
    cache_conn, monkeypatch,
):
    import _cctally_dashboard_sources as ds

    new = ds._codex_conversation_metadata(cache_conn)
    monkeypatch.setattr(ds, "_CODEX_FILE_ALIAS_SQL", LEGACY_ALIAS_SQL)
    monkeypatch.setattr(
        ds, "_CODEX_CONVERSATION_METADATA_CORE_SQL", LEGACY_METADATA_CORE_SQL)
    old = ds._codex_conversation_metadata(cache_conn)
    assert new.error is None and old.error is None
    assert new.metadata == old.metadata
    assert new.metadata, "non-vacuity: the fixture yields metadata"
