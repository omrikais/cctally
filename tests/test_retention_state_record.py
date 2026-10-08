"""#901 spec §5.4 "State write" (901-SR-016), G3q's state-write half: the
#780 record is one fixed-size cell that every writer stores at exactly L
bytes and no writer deletes, so a reclaim chunk's state write overwrites it
in place and dirties exactly one page.

Every case runs against a real conversations store. Lengths are read with
SQL (`length(CAST(value AS BLOB))`), never from the module under test.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import pathlib
import sqlite3
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402
import _retention_fixtures as fx  # noqa: E402

UTC = dt.timezone.utc
KiB = 1024
NOW = dt.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
OLD = "2025-08-01T12:00:00.000Z"
KEY = "conversation_retention_reclaim_pending"


def _env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    conn = ns["open_conversations_db"](attach_cache=False)
    retention = importlib.import_module("_lib_conversation_retention")
    importlib.import_module("_lib_write_io").reset_for_tests()
    return ns, conn, retention, pathlib.Path(retention._resolve_main_db_path(conn))


def _stored(conn):
    row = conn.execute("SELECT length(CAST(value AS BLOB)), value FROM "
                       "cache_meta WHERE key=?", (KEY,)).fetchone()
    return (None, None) if row is None else (int(row[0]), row[1])


def _fill_and_free(conn, rows=600):
    conn.execute("CREATE TABLE IF NOT EXISTS sr_filler(payload BLOB)")
    conn.executemany("INSERT INTO sr_filler VALUES (?)",
                     [(os.urandom(3000),) for _ in range(rows)])
    conn.commit()
    conn.execute("DELETE FROM sr_filler")
    conn.commit()


def _eligible(monkeypatch, retention):
    monkeypatch.setattr(
        retention, "reclaim_episode_active",
        lambda geometry, active: geometry is not None
        and geometry.freelist_count > 0)


def _visit(retention, conn, now, ops):
    return retention._maybe_prune_conversation_retention(
        conn, now_utc=now, retention_days=180,
        record_phase=lambda kind, payload: ops.append((kind, payload)))


def _seed_claude(conn, session_id, rows):
    conn.executemany(
        "INSERT INTO conversation_messages (session_id, uuid, source_path, "
        "byte_offset, timestamp_utc, entry_type, text, blocks_json) "
        "VALUES (?,?,?,?,?,?,?,'[]')",
        [(session_id, f"{session_id}-{i}", f"/{session_id}.jsonl", i, OLD,
          "assistant", "x" * 200) for i in range(rows)])
    conn.commit()


def test_every_writer_stores_the_record_at_one_fixed_length(
        tmp_path, monkeypatch):
    """Deletion, the episode end, a failure record, the daily stamp and the
    reclaim chunk all store exactly L space-padded bytes; nothing deletes
    the row."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    _seed_claude(conn, "old", 3)
    lengths = {}
    ops = []
    _visit(retention, conn, NOW, ops)                 # deletion + stamp
    lengths["deletion_and_stamp"] = _stored(conn)[0]
    retention._record_reclaim_failure(db_path, "sqlite_error", NOW)
    lengths["failure"] = _stored(conn)[0]
    _fill_and_free(conn)
    _eligible(monkeypatch, retention)
    _visit(retention, conn, NOW + dt.timedelta(minutes=2), ops)  # chunks
    assert any(kind == "reclaim" and p["outcome"] == "ok" for kind, p in ops)
    lengths["reclaim"] = _stored(conn)[0]
    retention._write_reclaim_pending(conn, None)      # the old DELETE branch
    conn.commit()
    lengths["ended_episode"], value = _stored(conn)
    assert len(set(lengths.values())) == 1, lengths
    assert value != value.rstrip(" "), "the JSON is space-padded"
    assert json.loads(value)["eligible"] is False
    assert lengths["deletion_and_stamp"] == retention.RECLAIM_RECORD_LENGTH
    conn.close()


def test_the_record_length_has_headroom_and_its_cell_stays_local():
    retention = importlib.import_module("_lib_conversation_retention")
    length = retention.RECLAIM_RECORD_LENGTH
    assert length * 4 >= retention.max_record_length() * 5, "25% headroom"
    payload = retention.record_cell_payload_bytes(length)
    assert payload == 4 + len(KEY) + length
    assert payload <= 4096 - 35, "local on a 4 KiB page"
    assert retention.record_fits_locally(4096)
    assert not retention.record_fits_locally(1024)


def test_a_bounded_record_never_exceeds_the_computed_maximum():
    """Garbage in every field still serializes within the maximum."""
    retention = importlib.import_module("_lib_conversation_retention")
    hostile = {
        "policy_version": 10 ** 30, "balance_bytes": -(10 ** 40),
        "as_of": "x" * 500, "op_seq": 10 ** 30, "eligible": "yes",
        "continuation_cutoff": "2026-01-18T12:00:00Z" + " " * 400,
        "ledger": [{"hour": f"2026-07-{1 + h // 24:02d}T{h % 24:02d}:00:00Z",
                    "charged": 10 ** 30, "largest": 10 ** 30}
                   for h in range(200)],
        "last_failure": {"reason": "R" * 900, "at": "never"},
        "progress": {"freelist_pages": -5, "page_size": 10 ** 25,
                     "observed_at": 3},
        "attempted_at": NOW.isoformat(), "unknown_future_field": "z" * 9000,
    }
    text = retention.serialize_reclaim_record(hostile)
    assert len(text.encode()) == retention.RECLAIM_RECORD_LENGTH
    parsed = json.loads(text)
    assert "unknown_future_field" not in parsed
    assert len(parsed["ledger"]) == 25
    assert parsed["last_failure"]["reason"] == "unknown"
    assert len(json.dumps(parsed, sort_keys=True)) <= \
        retention.max_record_length()


def _pinned_reader(db_path):
    reader = sqlite3.connect(db_path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
    return reader


def test_the_in_chunk_state_write_dirties_exactly_one_page(
        tmp_path, monkeypatch):
    """G3q: on a `cache_meta` leaf full apart from the record's cell, a
    transaction holding only the in-chunk state write writes exactly one WAL
    frame, and the leaf has no room for a second copy of the cell."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    assert retention.normalize_reclaim_record(conn, NOW) is True
    # Small neighbours on either side of the key, so the record shares its
    # leaf with other cells.
    conn.executemany("INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                     [(f"{KEY[:-1]}{chr(97 + i)}", "v" * 8) for i in range(4)]
                     + [(f"{KEY}~{i}", "v" * 8) for i in range(4)])
    conn.commit()
    try:
        unused = [row[0] for row in conn.execute(
            "SELECT unused FROM dbstat WHERE name='cache_meta' "
            "AND pagetype='leaf'")]
    except sqlite3.Error:
        unused = None                       # no dbstat in this build
    if unused is not None:
        cell = retention.record_cell_payload_bytes() + 2
        assert min(unused) < cell, ("the record's leaf is full apart from "
                                    "its own cell", unused)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    reader = _pinned_reader(db_path)
    record = retention.read_reclaim_pending(conn)
    record["balance_bytes"] = -123_456_789
    record["op_seq"] = 999_999
    state_conn = retention.open_reclaim_connection(db_path)
    try:
        state_conn.execute("BEGIN IMMEDIATE")
        retention._write_reclaim_state_in_chunk(state_conn, record)
        state_conn.commit()
    finally:
        state_conn.close()
    _page_size, frames = fx.wal_frames(db_path)
    reader.rollback()
    reader.close()
    assert len(frames) == 1, frames
    assert retention.read_reclaim_pending(conn)["balance_bytes"] == -123_456_789
    assert _stored(conn)[0] == retention.RECLAIM_RECORD_LENGTH
    conn.close()


def test_a_legacy_record_is_normalized_once_before_the_first_chunk(
        tmp_path, monkeypatch):
    """An unpadded legacy record is rewritten at L by one uncharged state-only
    transaction before the attempt's first chunk, and not again after a
    successful chunk."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, ?)", (
        KEY, json.dumps({"freelist_count": 5, "unreclaimed_bytes": 20480,
                         "attempted_at": NOW.isoformat(),
                         "made_progress": True, "deadline_hit": True})))
    retention._stamp_retention_prune(conn, NOW)
    conn.commit()
    assert _stored(conn)[0] != retention.RECLAIM_RECORD_LENGTH
    _fill_and_free(conn, rows=3000)        # not drained by the first visit
    _eligible(monkeypatch, retention)
    events = []
    real_normalize = retention.normalize_reclaim_record
    real_chunk = retention.reclaim_chunk

    def normalize(c, now):
        wrote = real_normalize(c, now)
        events.append(("normalize", wrote, _stored(conn)[0]))
        return wrote

    def chunk(c, **kw):
        result = real_chunk(c, **kw)
        events.append(("chunk", result.outcome, result.charged_bytes))
        return result

    monkeypatch.setattr(retention, "normalize_reclaim_record", normalize)
    monkeypatch.setattr(retention, "reclaim_chunk", chunk)
    ops = []
    _visit(retention, conn, NOW, ops)
    _visit(retention, conn, NOW + dt.timedelta(minutes=1), ops)
    normalizations = [e for e in events if e[0] == "normalize"]
    chunks = [e for e in events if e[0] == "chunk"]
    assert events[0] == ("normalize", True, retention.RECLAIM_RECORD_LENGTH)
    assert [e[1] for e in normalizations] == [True, False], events
    assert chunks and all(e[1] == "ok" for e in chunks), events
    record = retention.read_reclaim_pending(conn)
    assert record["freelist_count"] is not None
    ledger = sum(b["charged"] for b in record["ledger"])
    assert ledger == sum(e[2] for e in chunks), "normalization is uncharged"
    conn.close()


def test_the_old_deleted_row_state_is_normalized_before_the_first_chunk(
        tmp_path, monkeypatch):
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    retention._stamp_retention_prune(conn, NOW)
    conn.commit()
    _fill_and_free(conn)
    assert _stored(conn) == (None, None), "an older binary deleted the row"
    _eligible(monkeypatch, retention)
    ops = []
    _visit(retention, conn, NOW, ops)
    assert any(kind == "reclaim" and p["outcome"] == "ok" for kind, p in ops)
    assert _stored(conn)[0] == retention.RECLAIM_RECORD_LENGTH
    conn.close()


def test_a_chunk_refuses_an_unnormalized_record(tmp_path, monkeypatch):
    """The precondition, read before the plan: the row exists at length L."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                 (KEY, json.dumps({"policy_version": 1})))
    conn.commit()
    _fill_and_free(conn)
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    reclaim = retention.open_reclaim_connection(db_path)
    try:
        result = retention.reclaim_chunk(reclaim, now_utc=NOW, pages=16)
    finally:
        reclaim.close()
    assert (result.outcome, result.skip_reason, result.charged_bytes) == (
        "skipped", "state_record_unnormalized", 0)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free
    conn.close()


def test_a_chunk_whose_record_would_exceed_L_is_refused(tmp_path, monkeypatch):
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    assert retention.normalize_reclaim_record(conn, NOW) is True
    _fill_and_free(conn)
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    before = _stored(conn)[1]
    monkeypatch.setattr(retention, "_record_json",
                        lambda record: "x" * (retention.RECLAIM_RECORD_LENGTH
                                              + 1))
    reclaim = retention.open_reclaim_connection(db_path)
    try:
        result = retention.reclaim_chunk(reclaim, now_utc=NOW, pages=16)
    finally:
        reclaim.close()
    assert (result.outcome, result.skip_reason, result.charged_bytes) == (
        "skipped", "state_record_oversize", 0)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free
    assert _stored(conn)[1] == before, "no vacuum, no charge, no write"
    with pytest.raises(retention.StateRecordOversize):
        retention.serialize_reclaim_record({})
    conn.close()
