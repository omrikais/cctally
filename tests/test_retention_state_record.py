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


#: #990 (spec D1): the planner helper's real-time floor for the tests whose
#: subject is a chunk's outcome, never the helper's own failure or deadline.
_PLANNER_FLOOR_SECONDS = 30.0


def _floor_planner_budget(monkeypatch, retention) -> None:
    """Give every planner helper of this test 30 s of its own deadline, and
    31 s of subprocess timeout with the 1.0 s start-up slack, whatever the
    visit's remaining deadline or a direct call's 1.5 s default, so a
    multi-second host stall cannot turn the chunk under test into a
    ``helper_timeout`` refusal (#990). The product constants and the tests'
    clocks are unchanged, and a passing run never waits on the floor: a
    helper returns in about 0.05 s."""
    monkeypatch.setattr(retention, "_PLANNER_MIN_DEADLINE_SECONDS",
                        _PLANNER_FLOOR_SECONDS)


def _visit(retention, conn, now, ops, *, clock=None):
    """One visit. ``clock`` (a `_stops_after_chunks` clock) decides when
    its reclaim attempt ends; without it the visit runs on the real clock."""
    def record(kind, payload):
        ops.append((kind, payload))
        if clock is not None and kind == "reclaim":
            clock.reported()

    return retention._maybe_prune_conversation_retention(
        conn, now_utc=now, retention_days=180, record_phase=record,
        **({} if clock is None else {"clock": clock}))


class _stops_after_chunks:
    """A visit clock that stands still and passes the deadline once
    ``chunks`` reclaim chunks have reported, so the test decides the chunk
    count. It does not bound a helper in real time: the tests that use it
    also apply `_floor_planner_budget`, whose 30 s floor plus the 1.0 s
    start-up slack is each helper's real-time bound (#990)."""

    def __init__(self, retention, chunks):
        self._jump = retention.RECLAIM_DEADLINE_SECONDS + 0.5
        self._left = chunks
        self._now = 0.0

    def __call__(self):
        return self._now

    def reported(self):
        self._left -= 1
        if self._left == 0:
            self._now += self._jump


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
    _floor_planner_budget(monkeypatch, retention)
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
    _floor_planner_budget(monkeypatch, retention)
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
    for minute in (0, 1):
        # #990: each visit ends after exactly two chunks, decided here by the
        # clock rather than by the wall clock. With the real clock a loaded
        # runner left the visit's last chunk a sliver of the 2 s deadline,
        # its planner helper refused it, and the "every chunk is ok" check
        # failed on a deadline skip instead of on normalization. The clock
        # decides only the chunk count: each helper's real-time bound is the
        # 30 s floor plus the 1.0 s start-up slack (`_floor_planner_budget`),
        # so a host stall longer than the 2 s deadline cannot refuse one.
        _visit(retention, conn, NOW + dt.timedelta(minutes=minute), ops,
               clock=_stops_after_chunks(retention, 2))
    normalizations = [e for e in events if e[0] == "normalize"]
    chunks = [e for e in events if e[0] == "chunk"]
    assert events[0] == ("normalize", True, retention.RECLAIM_RECORD_LENGTH)
    assert [e[1] for e in normalizations] == [True, False], events
    assert [e[1] for e in chunks] == ["ok"] * 4, events
    record = retention.read_reclaim_pending(conn)
    assert record["freelist_count"] is not None
    ledger = sum(b["charged"] for b in record["ledger"])
    assert ledger == sum(e[2] for e in chunks), "normalization is uncharged"
    conn.close()


def test_the_old_deleted_row_state_is_normalized_before_the_first_chunk(
        tmp_path, monkeypatch):
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    _floor_planner_budget(monkeypatch, retention)
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
    _floor_planner_budget(monkeypatch, retention)
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


# ── #1090 T4: the applied-cutoff evidence (spec §5.1) ─────────────────────

APPLIED = "conversation_retention_applied_cutoff"


def _applied(conn):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?",
                       (APPLIED,)).fetchone()
    return None if row is None else row[0]


def _set_applied(conn, value):
    conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (APPLIED, value))
    conn.commit()


def _set_continuation(conn, retention, cutoff):
    record = retention.read_reclaim_pending(conn) or {}
    retention._write_reclaim_pending(conn, {
        **retention.PacingState.from_record(record, NOW).to_record(),
        "continuation_cutoff": cutoff})
    conn.commit()


def _merge(conn, retention, cutoff):
    conn.execute("BEGIN IMMEDIATE")
    wrote = retention._merge_applied_cutoff(conn, cutoff)
    assert conn.in_transaction, "the merge never commits"
    conn.commit()
    return wrote


def test_applied_cutoff_never_decreases(tmp_path, monkeypatch):
    """A lower or equal cutoff writes nothing (no false entitlement shrink,
    no churn); a higher one advances the key. Through the real deletion too:
    a deletion under a lower cutoff leaves the higher key. Detects lost
    entitlement (a decrease) and a needless state write."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    assert retention.APPLIED_CUTOFF_KEY == APPLIED
    assert _merge(conn, retention, "2026-02-01T00:00:00Z") is True
    assert _applied(conn) == "2026-02-01T00:00:00Z"
    before = conn.total_changes
    assert _merge(conn, retention, "2026-01-01T00:00:00Z") is False
    assert _merge(conn, retention, "2026-02-01T00:00:00Z") is False
    assert conn.total_changes == before, "an unchanged merge writes nothing"
    assert _applied(conn) == "2026-02-01T00:00:00Z"
    assert _merge(conn, retention, "2026-03-01T00:00:00Z") is True
    _seed_claude(conn, "old", 2)
    deletion = retention.open_deletion_connection(db_path)
    try:
        result = retention.delete_group(
            deletion, retention.ExpiredGroup("claude", False, "old"),
            now_utc=NOW, cutoff_iso="2026-01-15T00:00:00Z")
    finally:
        deletion.close()
    assert result.outcome == "ok"
    assert _applied(conn) == "2026-03-01T00:00:00Z"
    assert retention.read_reclaim_pending(conn)["continuation_cutoff"] == \
        "2026-01-15T00:00:00Z"
    assert retention.retention_cutoff_proof(conn) == "2026-03-01T00:00:00Z"
    conn.close()


def test_completion_records_cutoff_before_clearing_continuation(
        tmp_path, monkeypatch):
    """Completion merges the visit's cutoff before it clears
    `continuation_cutoff`, in one transaction, so the proof never lapses
    between the two writes. Detects lost entitlement at episode end."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    cutoff = "2026-01-18T12:00:00Z"
    _set_continuation(conn, retention, cutoff)
    seen = []
    real = retention._write_reclaim_pending

    def write(c, state):
        seen.append((_applied(c), (state or {}).get("continuation_cutoff")))
        return real(c, state)

    monkeypatch.setattr(retention, "_write_reclaim_pending", write)
    retention._stamp_retention_complete(conn, NOW, cutoff)
    assert seen == [(cutoff, None)], "merged before the clear"
    assert _applied(conn) == cutoff
    assert retention.read_reclaim_pending(conn)["continuation_cutoff"] is None
    assert retention.retention_cutoff_proof(conn) == cutoff
    assert not conn.in_transaction
    conn.close()


def test_zero_deletion_completion_records_cutoff(tmp_path, monkeypatch):
    """A completed visit that deleted nothing still records the cutoff it
    applied. Detects a store that never gains entitlement because its
    deletions all happened before the upgrade."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    _floor_planner_budget(monkeypatch, retention)
    ops = []
    stats = _visit(retention, conn, NOW, ops)
    assert stats is not None and stats.complete
    assert not any(kind == "delete" for kind, _payload in ops)
    expected = retention._cutoff_iso(NOW - dt.timedelta(days=180))
    assert _applied(conn) == expected
    assert retention.retention_cutoff_proof(conn) == expected
    conn.close()


@pytest.mark.parametrize("operation", ["deletion", "completion"])
def test_rolled_back_deletion_or_completion_leaves_applied_cutoff(
        tmp_path, monkeypatch, operation):
    """The key is written inside the operation's own transaction, so an
    operation that rolls back leaves no proof behind. Detects false
    entitlement from a deletion that never happened."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    _set_applied(conn, "2025-06-01T00:00:00Z")
    cutoff = "2026-01-18T12:00:00Z"

    class Injected(Exception):
        pass

    def boom(*_args, **_kwargs):
        raise Injected()

    if operation == "deletion":
        _seed_claude(conn, "old", 2)
        monkeypatch.setattr(retention, "_delete_group_rows", boom)
        deletion = retention.open_deletion_connection(db_path)
        try:
            with pytest.raises(Injected):
                retention.delete_group(
                    deletion, retention.ExpiredGroup("claude", False, "old"),
                    now_utc=NOW, cutoff_iso=cutoff)
        finally:
            deletion.close()
        assert conn.execute("SELECT COUNT(*) FROM conversation_messages "
                            "WHERE session_id='old'").fetchone()[0] == 2
    else:
        _set_continuation(conn, retention, cutoff)
        real = retention._write_reclaim_pending

        def clear(c, state):
            if (state or {}).get("continuation_cutoff") is None:
                raise Injected()
            return real(c, state)

        monkeypatch.setattr(retention, "_write_reclaim_pending", clear)
        with pytest.raises(Injected):
            retention._stamp_retention_complete(conn, NOW, cutoff)
        assert retention.read_reclaim_pending(conn)["continuation_cutoff"] \
            == cutoff
    assert _applied(conn) == "2025-06-01T00:00:00Z"
    conn.close()


def test_proof_survives_retention_off(tmp_path, monkeypatch):
    """Turning retention off does not withdraw the historical evidence: the
    proof reads no config, and a disabled visit writes nothing. Detects lost
    entitlement for groups retention already removed."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    _floor_planner_budget(monkeypatch, retention)
    _seed_claude(conn, "old", 2)
    ops = []
    assert _visit(retention, conn, NOW, ops).complete
    expected = retention._cutoff_iso(NOW - dt.timedelta(days=180))
    assert retention.retention_cutoff_proof(conn) == expected
    before = conn.total_changes
    assert retention._maybe_prune_conversation_retention(
        conn, now_utc=NOW + dt.timedelta(days=2), retention_days=0) is None
    assert conn.total_changes == before
    assert retention.retention_cutoff_proof(conn) == expected
    conn.close()


@pytest.mark.parametrize("applied, continuation, expected", [
    pytest.param(None, None, None, id="none"),
    pytest.param("2026-02-01T00:00:00Z", None, "2026-02-01T00:00:00Z",
                 id="applied-only"),
    pytest.param(None, "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z",
                 id="continuation-only"),
    pytest.param("2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z",
                 "2026-03-01T00:00:00Z", id="continuation-higher"),
    pytest.param("2026-04-01T00:00:00Z", "2026-03-01T00:00:00Z",
                 "2026-04-01T00:00:00Z", id="applied-higher"),
    pytest.param("", None, None, id="empty-applied"),
    pytest.param(b"\x00", None, None, id="non-string-applied"),
    pytest.param(None, "", None, id="empty-continuation"),
    pytest.param(None, 5, None, id="non-string-continuation"),
    pytest.param("2026-02-01T00:00:00Z", 5, "2026-02-01T00:00:00Z",
                 id="applied-with-invalid-continuation"),
])
def test_proof_is_max_of_applied_and_continuation(
        tmp_path, monkeypatch, applied, continuation, expected):
    """The proof is the SQLite-order maximum of the two recorded cutoffs,
    ignoring absent, empty and non-string values, and opens no transaction.
    Detects false entitlement from a malformed value."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    if applied is not None:
        _set_applied(conn, applied)
    if continuation is not None:
        # Raw JSON, as a legacy or foreign writer could leave it.
        conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (KEY, json.dumps({"continuation_cutoff": continuation})))
        conn.commit()
    assert retention.retention_cutoff_proof(conn) == expected
    assert not conn.in_transaction
    conn.close()
