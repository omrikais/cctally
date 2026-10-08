"""#901 spec §5.4: paced retention maintenance (G3, G3c, G3r).

Every case runs against a real conversations store. Clocks are injected
through ``now_utc`` (wall) and ``clock`` (monotonic); the allowance, the
ledger and the reservations are read back from the durable record.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import importlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402

UTC = dt.timezone.utc
KiB = 1024
MiB = 1024 * KiB
NOW = dt.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
OLD = "2025-08-01T12:00:00.000Z"
FRESH = "2026-07-17T08:00:00.000Z"
DAYS = 180
_OFFSET = [0]


def _env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    conn = ns["open_conversations_db"](attach_cache=False)
    retention = importlib.import_module("_lib_conversation_retention")
    try:
        importlib.import_module("_lib_write_io").reset_for_tests()
    except ModuleNotFoundError:
        pass  # the 56e66f07a RED run of the crash case predates the module
    return ns, conn, retention, retention._resolve_main_db_path(conn)


def _seed_claude(conn, session_id, rows, ts=OLD, *, text="x" * 200):
    batch = []
    for _ in range(rows):
        _OFFSET[0] += 1
        batch.append((session_id, f"u-{_OFFSET[0]}", f"/{session_id}.jsonl",
                      _OFFSET[0], ts, "assistant", text))
    conn.executemany(
        "INSERT INTO conversation_messages (session_id, uuid, source_path, "
        "byte_offset, timestamp_utc, entry_type, text, blocks_json) "
        "VALUES (?,?,?,?,?,?,?,'[]')", batch)
    conn.commit()


def _seed_codex(conn, key, rows, ts=OLD):
    batch = []
    for _ in range(rows):
        _OFFSET[0] += 1
        batch.append((f"/{key}.jsonl", _OFFSET[0], "root-a", key, ts))
    conn.executemany(
        "INSERT INTO codex_conversation_events (source_path, line_offset, "
        "source_root_key, conversation_key, timestamp_utc, payload_json) "
        "VALUES (?,?,?,?,?,'{}')", batch)
    conn.commit()


def _claude_rows(conn, session_id):
    return conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE session_id=?",
        (session_id,)).fetchone()[0]


def _raw_record(conn, retention):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?",
                       (retention.RECLAIM_PENDING_KEY,)).fetchone()
    return None if row is None else row[0]


def _ledger_total(record):
    return sum(b["charged"] for b in (record or {}).get("ledger", []))


def _stamp(conn):
    row = conn.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='conversation_retention_last_prune_at'").fetchone()
    return None if row is None else row[0]


def _visit(retention, conn, now, ops=None, *, force=False, clock=time.monotonic):
    record = None if ops is None else (
        lambda kind, payload: ops.append((kind, payload)))
    return retention._maybe_prune_conversation_retention(
        conn, now_utc=now, retention_days=DAYS, force=force,
        record_phase=record, clock=clock)


def _fill_and_free(conn, rows=600):
    conn.execute("CREATE TABLE IF NOT EXISTS g3_filler(payload BLOB)")
    conn.executemany("INSERT INTO g3_filler VALUES (?)",
                     [(os.urandom(3000),) for _ in range(rows)])
    conn.commit()
    conn.execute("DELETE FROM g3_filler")
    conn.commit()
    # §5.4: the fixed-size record exists before any chunk (a first write of
    # it allocates its leaf from the freelist, which would blur the counts).
    importlib.import_module("_lib_conversation_retention") \
        .normalize_reclaim_record(conn, NOW)
    return int(conn.execute("PRAGMA freelist_count").fetchone()[0])


def _eligible(monkeypatch, retention):
    """Treat any non-empty freelist as an eligible episode; the real
    thresholds are proven separately and would need a multi-GiB file."""
    monkeypatch.setattr(
        retention, "reclaim_episode_active",
        lambda geometry, active: geometry is not None
        and geometry.freelist_count > 0)


def _geometry(conn):
    return (int(conn.execute("PRAGMA page_count").fetchone()[0]),
            int(conn.execute("PRAGMA page_size").fetchone()[0]))


def _reservation_rows(conn, rows):
    """Q8: F + A x rows + B x M_cap from the store's geometry right now."""
    retention = importlib.import_module("_lib_conversation_retention")
    page_count, page_size = _geometry(conn)
    return retention.deletion_reservation(
        rows, page_count=page_count, usable_size=page_size,
        page_size=page_size)


def _recomputed(retention, kind, payload):
    return retention.recompute_charge({"phase": kind, **payload})


# ── the allowance, the reservations and the record (pure) ────────────────

def test_the_allowance_accrues_by_wall_clock_and_caps_at_one_minute():
    retention = importlib.import_module("_lib_conversation_retention")
    state = retention.PacingState(balance_bytes=0, as_of=NOW)
    assert state.available(NOW) == 0
    assert state.available(NOW + dt.timedelta(seconds=30)) == 2 * MiB
    assert state.available(NOW + dt.timedelta(minutes=10)) == 4 * MiB
    debt = retention.PacingState(balance_bytes=-6 * MiB, as_of=NOW)
    assert debt.available(NOW + dt.timedelta(minutes=1)) == -2 * MiB
    assert debt.available(NOW - dt.timedelta(minutes=5)) == -6 * MiB, (
        "a clock step backwards grants nothing")


def test_a_charge_is_the_reservation_in_its_start_hour():
    retention = importlib.import_module("_lib_conversation_retention")
    at = NOW + dt.timedelta(minutes=59)
    reservation = retention.deletion_reservation(
        100, page_count=10_000, usable_size=4096, page_size=4096)
    charged = retention.PacingState(4 * MiB, NOW).charge(reservation, at)
    assert charged.balance_bytes == 4 * MiB - reservation
    assert charged.as_of == at
    assert charged.op_seq == 1
    assert charged.ledger == ({"hour": "2026-07-17T12:00:00Z",
                               "charged": reservation,
                               "largest": reservation},)


def test_the_pointer_map_cap_follows_the_file_geometry():
    """Q8: M_cap = ceil(N_max / (floor(U/5) + 1)) + 1 with N_max the page
    count plus ceil((512 KiB + 12 KiB x rows) / P)."""
    retention = importlib.import_module("_lib_conversation_retention")
    # Today's store (spec §1.4): 4,532,015 pages of 4 KiB.
    assert retention.pointer_map_cap(4_532_015, 4096, 0, 4096) == 5529
    assert retention.pointer_map_cap(4_532_015, 4096, 27_395, 4096) == \
        -(-(4_532_015 + -(-(512 * KiB + 12 * KiB * 27_395) // 4096))
          // 820) + 1
    # The usable size, not the page size, sets the entries per map page.
    assert retention.pointer_map_cap(820 * 10, 4096, 0, 4096) == 12
    assert retention.pointer_map_cap(820 * 10, 4000, 0, 4096) == \
        -(-(820 * 10 + 128) // 801) + 1
    assert retention.pointer_map_cap(1, 1024, 0, 1024) == \
        -(-(1 + 512) // 205) + 1


def test_the_pointer_map_coefficient_is_computed_from_the_page_size():
    """B = ceil(1.25 x (2P + 24) / 1024) KiB: 11 KiB at 4 KiB pages."""
    retention = importlib.import_module("_lib_conversation_retention")
    assert retention.pointer_map_page_bytes(4096) == 11 * KiB
    assert retention.pointer_map_page_bytes(1024) == 3 * KiB
    assert retention.pointer_map_page_bytes(65536) == 161 * KiB


def test_the_reservations_follow_the_candidate_constants():
    retention = importlib.import_module("_lib_conversation_retention")
    geometry = dict(page_count=4_532_015, usable_size=4096, page_size=4096)
    b_term = 11 * KiB * retention.pointer_map_cap(4_532_015, 4096, 0, 4096)
    assert retention.deletion_reservation(0, **geometry) == 512 * KiB + b_term
    for rows, per_row in ((60_000, 15 * KiB), (60_001, 50 * KiB)):
        cap = retention.pointer_map_cap(4_532_015, 4096, rows, 4096)
        assert retention.deletion_reservation(rows, **geometry) == (
            512 * KiB + per_row * rows + 11 * KiB * cap)
    # About 59 MiB of pointer-map term on today's store (spec §5.4).
    assert 59 * MiB < b_term < 60 * MiB


def test_a_deletion_charge_is_recomputable_from_its_record():
    retention = importlib.import_module("_lib_conversation_retention")
    payload = {"phase": "delete", "rows": 493, "page_count": 4_532_015,
               "usable_size": 4096, "page_size": 4096,
               "reservation_version": retention.RESERVATION_VERSION}
    expected = retention.deletion_reservation(
        493, page_count=4_532_015, usable_size=4096, page_size=4096)
    assert retention.recompute_charge(payload) == expected
    assert retention.recompute_charge({**payload, "reservation_version": 1}) \
        is None, "an unknown formula version is not recomputed"
    assert retention.recompute_charge({**payload, "page_count": None}) is None


def test_a_legacy_record_reads_as_full_credit_and_no_charges():
    retention = importlib.import_module("_lib_conversation_retention")
    legacy = {"freelist_count": 5, "unreclaimed_bytes": 20480,
              "attempted_at": NOW.isoformat(), "made_progress": True,
              "deadline_hit": True}
    state = retention.PacingState.from_record(legacy, NOW)
    assert (state.balance_bytes, state.ledger, state.eligible) == (
        4 * MiB, (), False)
    record = state.to_record()
    assert record["policy_version"] == 1
    assert record["freelist_count"] == 5, "legacy fields are carried"


def test_a_malformed_balance_never_grants_more_than_the_cap():
    retention = importlib.import_module("_lib_conversation_retention")
    record = {"policy_version": 1, "balance_bytes": 10 ** 12,
              "as_of": "2026-07-17T12:00:00Z"}
    assert retention.PacingState.from_record(record, NOW).available(NOW) == 4 * MiB
    garbled = {"policy_version": 1, "balance_bytes": "lots", "as_of": 7}
    assert retention.PacingState.from_record(garbled, NOW).available(NOW) == 4 * MiB


def test_a_correct_pacer_never_exceeds_the_limit_and_a_broken_one_does():
    """G3l's pacer half: the full allowance for 25 hours."""
    retention = importlib.import_module("_lib_conversation_retention")
    budget = importlib.import_module("_lib_write_budget")
    reservation = retention.deletion_reservation(            # above the credit
        700, page_count=10_000, usable_size=4096, page_size=4096)
    state = retention.PacingState(4 * MiB, NOW)
    broken = []
    now = NOW
    for minute in range(25 * 60):
        now = NOW + dt.timedelta(minutes=minute)
        if state.available(now) >= 0:
            state = state.charge(reservation, now)
        broken = budget.ledger_add(broken, at_utc=now,
                                   charge_bytes=reservation)
        assert budget.maintenance_statistic(
            list(state.ledger), now).verdict == "ok", minute
    assert budget.maintenance_statistic(broken, now).verdict == "over"


@pytest.mark.parametrize("free_pages, page_count, active, expected", [
    (524_288, 2_000_000, False, False),   # exactly 2 GiB does not start
    (524_289, 2_000_000, False, True),    # just above 2 GiB, 26% free
    (600_000, 3_000_000, False, False),   # exactly 20% does not start
    (600_001, 3_000_000, False, True),
    (400_000, 2_000_000, False, False),   # between thresholds: no start ...
    (400_000, 2_000_000, True, True),     # ... but an episode continues
    (262_145, 2_000_000, True, True),     # just above 1 GiB
    (262_144, 2_000_000, True, False),    # reaching 1 GiB stops
    (300_000, 3_000_000, True, False),    # reaching 10% stops
    (300_001, 3_000_000, True, True),
])
def test_reclaim_starts_and_stops_at_its_boundaries(
        free_pages, page_count, active, expected):
    retention = importlib.import_module("_lib_conversation_retention")
    geometry = retention.StoreGeometry(page_count, free_pages, 4096)
    assert retention.reclaim_episode_active(geometry, active) is expected


def test_connection_settings_mirror_the_store_policy(tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    policy = ns["_cctally_cache"]._cctally_store.STORE_POLICY["conversations"]
    assert retention._CONNECTION_BUSY_TIMEOUT_MS == policy.busy_timeout
    assert retention._CONNECTION_SYNCHRONOUS == policy.synchronous
    assert retention._CONNECTION_JOURNAL_SIZE_LIMIT == policy.journal_size_limit
    conn.close()


# ── deletion pacing ───────────────────────────────────────────────────────

def test_paced_deletion_finishes_across_visits_and_keeps_small_slack(
        tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    for index in range(120):
        _seed_claude(conn, f"old-{index:03d}", 1)
    _seed_claude(conn, "fresh", 1, FRESH)
    ops = []
    visits = []
    for minute in range(60):
        stats = _visit(retention, conn, NOW + dt.timedelta(minutes=minute), ops)
        visits.append(stats)
        if stats is not None and stats.complete:
            break
    assert visits[0] is not None and visits[0].complete is False
    assert visits[-1] is not None and visits[-1].complete is True
    assert sum(s.claude_sessions for s in visits if s) == 120
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages"
                        ).fetchone()[0] == 1
    assert _stamp(conn) is not None
    assert all(kind == "delete" for kind, _ in ops), "small slack: no reclaim"
    assert all(p["charged_bytes"] == _recomputed(retention, "delete", p)
               for _, p in ops if p["outcome"] == "ok")
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 0
    conn.close()


def test_a_group_larger_than_the_allowance_runs_once_then_waits(
        tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    _seed_claude(conn, "a-big", 400)
    _seed_claude(conn, "b-small", 1)
    reservation = _reservation_rows(conn, 400)
    first = _visit(retention, conn, NOW)
    assert (first.claude_sessions, first.complete) == (1, False)
    assert _claude_rows(conn, "a-big") == 0
    assert _claude_rows(conn, "b-small") == 1
    record = retention.read_reclaim_pending(conn)
    assert record["balance_bytes"] == 4 * MiB - reservation < 0
    assert record["continuation_cutoff"] == "2026-01-18T12:00:00Z"
    assert _stamp(conn) is None
    raw = _raw_record(conn, retention)
    assert _visit(retention, conn, NOW + dt.timedelta(seconds=10)) is None
    assert _raw_record(conn, retention) == raw, "a gated visit writes nothing"
    assert _claude_rows(conn, "b-small") == 1
    last = _visit(retention, conn, NOW + dt.timedelta(seconds=40))
    assert (last.claude_sessions, last.complete) == (1, True)
    assert _claude_rows(conn, "b-small") == 0
    assert retention.read_reclaim_pending(conn)["continuation_cutoff"] is None
    assert _stamp(conn) is not None
    conn.close()


def test_restart_does_not_refill_credit(tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    _seed_claude(conn, "a-big", 400)
    _visit(retention, conn, NOW)
    stored = retention.read_reclaim_pending(conn)["balance_bytes"]
    # A restart is a fresh read of the durable record by a new process: the
    # pacing state holds nothing in memory that could refill the credit.
    state = retention.PacingState.from_record(
        retention.read_reclaim_pending(conn), NOW)
    assert state.available(NOW) == stored < 0
    assert state.available(NOW + dt.timedelta(seconds=15)) == stored + MiB
    conn.close()


def test_the_continuation_cutoff_is_stable_and_the_stamp_waits_for_both(
        tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    _seed_claude(conn, "a-big", 400)
    _seed_codex(conn, "codex-old", 3)
    # Last active one minute after the first visit's cutoff: expired by the
    # second visit's own clock, but NOT by the persisted continuation cutoff.
    _seed_claude(conn, "borderline", 1, "2026-01-18T12:01:00.000Z")
    first = _visit(retention, conn, NOW)
    assert first.complete is False
    assert conn.execute("SELECT COUNT(*) FROM codex_conversation_events"
                        ).fetchone()[0] == 3
    assert _stamp(conn) is None
    second = _visit(retention, conn, NOW + dt.timedelta(days=1))
    assert (second.codex_conversations, second.complete) == (1, True)
    assert _claude_rows(conn, "borderline") == 1
    assert _stamp(conn).startswith("2026-07-18T12:00:00")
    conn.close()


def test_contention_on_a_provider_flock_skips_without_writing(
        tmp_path, monkeypatch):
    import _cctally_core
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    _seed_claude(conn, "old", 2)
    held = open(_cctally_core.CONVERSATIONS_LOCK_PATH, "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        assert _visit(retention, conn, NOW) is None
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
    assert _claude_rows(conn, "old") == 2
    assert _raw_record(conn, retention) is None
    conn.close()


def test_a_legacy_record_is_reread_and_rewritten_additively(
        tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    retention._write_reclaim_pending(conn, {
        "freelist_count": 999_999, "unreclaimed_bytes": 999_999 * 4096,
        "attempted_at": NOW.isoformat(), "made_progress": True,
        "deadline_hit": True})
    conn.commit()
    _seed_claude(conn, "old", 1)
    ops = []
    _visit(retention, conn, NOW, ops)
    assert [kind for kind, _ in ops] == ["delete"], (
        "the legacy backlog is re-evaluated from page counts: no reclaim")
    record = retention.read_reclaim_pending(conn)
    assert record["policy_version"] == 1
    assert record["freelist_count"] == 999_999
    [(_kind, payload)] = ops
    assert _ledger_total(record) == payload["charged_bytes"] == _recomputed(
        retention, "delete", payload)
    conn.close()


def test_the_counterless_charge_is_the_reservation(tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    wio = importlib.import_module("_lib_write_io")
    wio.reset_for_tests(counter=wio.ProcessWriteCounter(platform="win32"))
    _seed_claude(conn, "old", 7)
    ops = []
    _visit(retention, conn, NOW, ops)
    [(kind, payload)] = ops
    assert payload["charged_bytes"] == _recomputed(retention, "delete", payload)
    assert payload["process_write_bytes"] is None
    assert payload["write_status"] == "unsupported_platform"
    assert _ledger_total(retention.read_reclaim_pending(conn)) == \
        payload["charged_bytes"]
    conn.close()


# ── reclaim pacing ────────────────────────────────────────────────────────

def test_a_reclaim_chunk_frees_exactly_its_pages_in_one_transaction(
        tmp_path, monkeypatch):
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    before = _fill_and_free(conn)
    assert before > 40
    reclaim = retention.open_reclaim_connection(db_path)
    retention.normalize_reclaim_record(reclaim, NOW)
    try:
        result = retention.reclaim_chunk(reclaim, now_utc=NOW, pages=40)
    finally:
        reclaim.close()
    after = conn.execute("PRAGMA freelist_count").fetchone()[0]
    payload = result.as_phase_payload()
    assert (result.outcome, payload["steps"]) == ("ok", 16), "clamped to 16"
    assert before - after == result.pages == payload["freelist_reduction"] == 16
    assert result.charged_bytes == _recomputed(retention, "reclaim", payload)
    assert _ledger_total(retention.read_reclaim_pending(conn)) == \
        result.charged_bytes
    conn.close()


def test_an_empty_freelist_chunk_is_no_progress_and_uncharged(
        tmp_path, monkeypatch):
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    conn.executescript("PRAGMA incremental_vacuum;")
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    reclaim = retention.open_reclaim_connection(db_path)
    retention.normalize_reclaim_record(reclaim, NOW)
    try:
        result = retention.reclaim_chunk(reclaim, now_utc=NOW)
    finally:
        reclaim.close()
    assert (result.outcome, result.charged_bytes) == ("no_progress", 0)
    assert _ledger_total(retention.read_reclaim_pending(conn)) == 0
    conn.close()


def test_retained_slack_below_the_start_threshold_is_never_reclaimed(
        tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    free = _fill_and_free(conn)
    retention._stamp_retention_prune(conn, NOW)
    conn.commit()
    ops = []
    assert _visit(retention, conn, NOW + dt.timedelta(hours=1), ops) is None
    assert ops == []
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free
    conn.close()


def test_reclaim_is_attempted_at_most_once_a_minute_in_small_chunks(
        tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    # Enough free pages that one minute's allowance (or the 2 s deadline)
    # cannot drain them: a planned free-page chunk costs about 88 KiB.
    _fill_and_free(conn, rows=3000)
    retention._stamp_retention_prune(conn, NOW)
    conn.commit()
    _eligible(monkeypatch, retention)
    ops = []
    assert _visit(retention, conn, NOW, ops) is None, "no deletion was due"
    chunks = [p for kind, p in ops if kind == "reclaim" and p["outcome"] == "ok"]
    assert chunks and all(p["pages_reclaimed"] <= 16 for p in chunks)
    assert all(p["charged_bytes"] == _recomputed(retention, "reclaim", p)
               for p in chunks)
    assert all(p["balance_before_bytes"] >= 0 for p in chunks)
    assert retention.read_reclaim_pending(conn)["next_attempt_at"] == \
        "2026-07-17T12:01:00Z"
    raw = _raw_record(conn, retention)
    ops.clear()
    _visit(retention, conn, NOW + dt.timedelta(seconds=30), ops)
    assert ops == [] and _raw_record(conn, retention) == raw
    _visit(retention, conn, NOW + dt.timedelta(seconds=60), ops)
    assert any(kind == "reclaim" for kind, _ in ops)
    conn.close()


# 901-RW-001: the record's instants are whole seconds, so a fractional
# visit clock must never let a reload of the durable record accrue again at
# the same instant, nor store a reclaim deadline earlier than the cadence.
FRACTIONAL = NOW + dt.timedelta(microseconds=900_000)


def test_reloading_the_record_at_a_fractional_instant_accrues_nothing(
        tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    for index in range(20):
        _seed_claude(conn, f"old-{index:03d}", 1)
    ops = []
    stats = _visit(retention, conn, FRACTIONAL, ops)
    deletes = [p for kind, p in ops if kind == "delete" and p["outcome"] == "ok"]
    assert stats is not None and stats.complete is False
    assert len(deletes) >= 2
    assert deletes[0]["balance_before_bytes"] == 4 * MiB
    # Every delete_group reloads the stored record at the same visit instant:
    # its opening balance must be exactly the previous operation's closing one.
    chain = [(a["balance_after_bytes"], b["balance_before_bytes"])
             for a, b in zip(deletes, deletes[1:])]
    assert all(after == before for after, before in chain), (
        f"a reload at an unchanged instant accrued again: {chain}")
    # The one durable allowance: the run stops at the first negative balance,
    # exactly where accounting with no elapsed time stops it.
    balance, expected = 4 * MiB, 0
    for p in deletes:
        if balance < 0:
            break
        balance -= p["charged_bytes"]
        expected += 1
    assert len(deletes) == expected
    stored = retention.read_reclaim_pending(conn)
    assert retention.PacingState.from_record(stored, FRACTIONAL).available(
        FRACTIONAL) == deletes[-1]["balance_after_bytes"]
    assert len(_raw_record(conn, retention).encode()) == \
        retention.RECLAIM_RECORD_LENGTH
    conn.close()


def test_a_charge_lands_in_the_hour_its_operation_started(
        tmp_path, monkeypatch):
    """PR-13 (a). Every operation of a visit charged the ledger bucket of the
    VISIT instant, so a visit that ran past the hour filed its later
    operations' charges under the earlier hour. The bucket is now the
    operation's own start (the visit instant plus the visit clock's elapsed
    time); the allowance still accrues at the visit instant, so every reload
    chains without accrual, and the record keeps its fixed length."""
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    for index in range(20):
        _seed_claude(conn, f"old-{index:03d}", 1)
    visit = NOW + dt.timedelta(minutes=59, seconds=30)
    ticks = {"n": 0.0}

    def clock():
        ticks["n"] += 10.0
        return ticks["n"]

    ops = []
    _visit(retention, conn, visit, ops, clock=clock)
    deletes = [p for kind, p in ops if kind == "delete" and p["outcome"] == "ok"]
    assert len(deletes) >= 3, "non-vacuity: the visit must run past the hour"
    stored = retention.read_reclaim_pending(conn)
    ledger = {bucket["hour"]: bucket["charged"] for bucket in stored["ledger"]}
    total = sum(p["charged_bytes"] for p in deletes)
    assert sum(ledger.values()) == total
    assert ledger.get("2026-07-17T13:00:00Z", 0) > 0, (
        "a charge of an operation that started after 13:00 was filed under "
        "the visit's hour", ledger)
    assert 0 < ledger.get("2026-07-17T12:00:00Z", 0) < total, ledger
    chain = [(a["balance_after_bytes"], b["balance_before_bytes"])
             for a, b in zip(deletes, deletes[1:])]
    assert all(after == before for after, before in chain), chain
    assert len(_raw_record(conn, retention).encode()) == \
        retention.RECLAIM_RECORD_LENGTH
    conn.close()


def test_a_fractional_reclaim_deadline_is_never_earlier_than_the_cadence(
        tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    _fill_and_free(conn, rows=3000)
    retention._stamp_retention_prune(conn, NOW)
    conn.commit()
    _eligible(monkeypatch, retention)
    ops = []
    assert _visit(retention, conn, FRACTIONAL, ops) is None
    chunks = [p for kind, p in ops if kind == "reclaim" and p["outcome"] == "ok"]
    assert len(chunks) >= 2
    stored = retention.read_reclaim_pending(conn)
    due = retention._parse_utc(stored["next_attempt_at"])
    cadence = FRACTIONAL + dt.timedelta(
        seconds=retention.RECLAIM_ATTEMPT_INTERVAL_SECONDS)
    assert due >= cadence, (
        f"next_attempt_at {stored['next_attempt_at']} precedes {cadence}")
    chain = [(a["balance_after_bytes"], b["balance_before_bytes"])
             for a, b in zip(chunks, chunks[1:])]
    assert all(after == before for after, before in chain), (
        f"a chunk's reload at an unchanged instant accrued again: {chain}")
    assert len(_raw_record(conn, retention).encode()) == \
        retention.RECLAIM_RECORD_LENGTH
    conn.close()


def test_a_reclaim_attempt_stops_at_its_deadline(tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    free = _fill_and_free(conn)
    retention._stamp_retention_prune(conn, NOW)
    conn.commit()
    _eligible(monkeypatch, retention)
    ticks = {"n": 0.0}

    def clock():
        ticks["n"] += 1.0
        return ticks["n"]

    ops = []
    _visit(retention, conn, NOW, ops, clock=clock)
    chunks = [p for kind, p in ops if kind == "reclaim" and p["outcome"] == "ok"]
    assert len(chunks) == 1, "the 2 s deadline is checked between chunks"
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free - 16
    conn.close()


def test_a_reclaim_visit_releases_its_flocks_within_the_deadline_plus_a_chunk(
        tmp_path, monkeypatch):
    import _cctally_core
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    _fill_and_free(conn, rows=2000)
    retention._stamp_retention_prune(conn, NOW)
    conn.commit()
    _eligible(monkeypatch, retention)
    ops = []
    started = time.monotonic()
    _visit(retention, conn, NOW, ops)
    elapsed = time.monotonic() - started
    longest = max(p["duration_s"] for kind, p in ops if kind == "reclaim")
    assert elapsed <= retention.RECLAIM_DEADLINE_SECONDS + longest + 1.0
    for path in (_cctally_core.CONVERSATIONS_LOCK_MAINTENANCE_PATH,
                 _cctally_core.CONVERSATIONS_LOCK_PATH,
                 _cctally_core.CONVERSATIONS_LOCK_CODEX_PATH):
        with open(path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
    conn.close()


def test_the_ceiling_never_accelerates_reclaim(tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    _fill_and_free(conn)
    retention._stamp_retention_prune(conn, NOW)
    conn.commit()
    _eligible(monkeypatch, retention)
    monkeypatch.setattr(retention, "observed_reclaim_backlog_bytes",
                        lambda c: 20 * 1024 * MiB)
    assert retention.observed_backlog_over_ceiling(conn)
    ops = []
    _visit(retention, conn, NOW, ops)
    _visit(retention, conn, NOW + dt.timedelta(seconds=30), ops)
    chunk_minutes = {p["started_at"] for kind, p in ops if kind == "reclaim"}
    assert chunk_minutes == {"2026-07-17T12:00:00Z"}, "still once a minute"
    assert all(p["pages_reclaimed"] <= 16 for _, p in ops)
    conn.close()


# ── G3r: reclaim never checkpoints ────────────────────────────────────────

def _sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def _wal_frames(db_path):
    wal = pathlib.Path(f"{db_path}-wal")
    if not wal.exists() or wal.stat().st_size < 32:
        return 0
    page_size = int.from_bytes(wal.read_bytes()[8:12], "big")
    return (wal.stat().st_size - 32) // (page_size + 24)


def _backlog_with_pinning_reader(conn, db_path):
    """A freelist to reclaim, then another writer's uncopied WAL backlog held
    by a reader pinned before it. Returns the reader."""
    _fill_and_free(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    reader = sqlite3.connect(db_path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.executemany("INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                       [(f"backlog-{i}", "v" * 200) for i in range(3000)])
    writer.commit()
    writer.close()
    return reader


def test_reclaim_never_checkpoints_even_as_the_last_connection(
        tmp_path, monkeypatch):
    import _lib_sqlite_close
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    reader = _backlog_with_pinning_reader(conn, db_path)
    main_before = _sha(db_path)
    backlog_frames = _wal_frames(db_path)
    assert backlog_frames > 0, "non-vacuity: the WAL holds a backlog"
    reclaim = retention.open_reclaim_connection(db_path)
    retention.normalize_reclaim_record(reclaim, NOW)
    assert reclaim.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
    if sys.version_info >= (3, 12):
        assert _lib_sqlite_close.no_checkpoint_on_close_enabled(reclaim) is True
    first = retention.reclaim_chunk(reclaim, now_utc=NOW, pages=16)
    reader.commit()
    reader.close()                      # the reclaim connection is now last
    second = retention.reclaim_chunk(
        reclaim, now_utc=NOW + dt.timedelta(minutes=1), pages=16)
    reclaim.close()
    assert (first.outcome, second.outcome) == ("ok", "ok")
    assert _sha(db_path) == main_before, "nothing was copied into the file"
    assert _wal_frames(db_path) > backlog_frames
    check = sqlite3.connect(db_path)
    try:
        record = retention.read_reclaim_pending(check)
    finally:
        check.close()
    assert _ledger_total(record) == first.charged_bytes + second.charged_bytes
    assert first.charged_bytes == _recomputed(
        retention, "reclaim", first.as_phase_payload())


def test_autocheckpoint_off_alone_still_copies_at_the_last_close(
        tmp_path, monkeypatch):
    """The validity witness: without no-checkpoint-on-close, the same
    scenario DOES change the file, so the case above can fail."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    reader = _backlog_with_pinning_reader(conn, db_path)
    main_before = _sha(db_path)
    plain = sqlite3.connect(db_path)
    plain.execute("PRAGMA wal_autocheckpoint = 0")
    plain.execute("INSERT INTO cache_meta(key, value) VALUES ('witness', '1')")
    plain.commit()
    reader.commit()
    reader.close()
    plain.close()
    assert _sha(db_path) != main_before


def test_reclaim_that_cannot_disable_close_checkpoints_runs_nothing(
        tmp_path, monkeypatch):
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    free = _fill_and_free(conn)
    retention._stamp_retention_prune(conn, NOW)
    conn.commit()
    _eligible(monkeypatch, retention)

    def refuse(*_args, **_kwargs):
        raise sqlite3.NotSupportedError("no db_config here")

    monkeypatch.setattr(retention._lib_sqlite_close,
                        "set_no_checkpoint_on_close", refuse)
    ops = []
    _visit(retention, conn, NOW, ops)
    assert [(kind, p["outcome"]) for kind, p in ops] == [("reclaim", "skipped")]
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free
    record = retention.read_reclaim_pending(conn)
    assert _ledger_total(record) == 0, "no reclaim, no charge"
    assert record["last_failure"]["reason"] == \
        "no_checkpoint_on_close_unavailable"
    raw = _raw_record(conn, retention)
    _visit(retention, conn, NOW + dt.timedelta(seconds=10), ops)
    assert _raw_record(conn, retention) == raw, "a repeated skip rewrites nothing"
    conn.close()


@pytest.mark.parametrize("failure", ["unavailable", "sqlite_error"])
def test_a_durable_reclaim_failure_is_retried_at_most_once_a_minute(
        tmp_path, monkeypatch, failure):
    """#901 Amendment 19 PR-4 (spec §5.4: reclaim is "attempted at most once
    per minute"). A durable failure — the reclaim connection refused
    (``ReclaimUnavailable``) or a SQLite error inside the attempt — writes no
    ``next_attempt_at``, so without a hold every visit retried it. It now holds
    the same one-minute cadence as a refusal, and reclaim resumes once the
    cause clears."""
    ns, conn, retention, _ = _env(tmp_path, monkeypatch)
    _fill_and_free(conn, rows=3000)
    retention._stamp_retention_prune(conn, NOW)
    conn.commit()
    _eligible(monkeypatch, retention)
    attempts = []
    real_open = retention._open_reclaim_connection
    real_normalize = retention.normalize_reclaim_record

    def refused_open(db_path):
        attempts.append(db_path)
        raise retention.ReclaimUnavailable("connection_settings_unavailable")

    def failing_normalize(conn_, now_):
        attempts.append(now_)
        raise sqlite3.OperationalError("disk I/O error")

    if failure == "unavailable":
        monkeypatch.setattr(retention, "_open_reclaim_connection", refused_open)
        reason = "connection_settings_unavailable"
    else:
        monkeypatch.setattr(retention, "normalize_reclaim_record",
                            failing_normalize)
        reason = "sqlite_error"
    for step in range(13):  # a visit every 10 s for two minutes
        _visit(retention, conn, NOW + dt.timedelta(seconds=10 * step))
    assert len(attempts) == 3, (
        "attempted at 0 s, 60 s and 120 s only", len(attempts))
    assert retention.read_reclaim_pending(conn)["last_failure"]["reason"] == \
        reason
    # Recovery: the cause clears, and the next due visit reclaims again.
    monkeypatch.setattr(retention, "_open_reclaim_connection", real_open)
    monkeypatch.setattr(retention, "normalize_reclaim_record", real_normalize)
    ops = []
    _visit(retention, conn, NOW + dt.timedelta(seconds=150), ops)
    assert ops == [], "still inside the hold the 120 s failure set"
    _visit(retention, conn, NOW + dt.timedelta(seconds=180), ops)
    assert any(kind == "reclaim" and payload["outcome"] == "ok"
               for kind, payload in ops), ops
    conn.close()


# ── G3c: crash points ─────────────────────────────────────────────────────

_CRASH_CHILD = r"""
import datetime as dt, os, sqlite3, sys
sys.path.insert(0, sys.argv[1])
import _lib_conversation_retention as r
db, op, point = sys.argv[2], sys.argv[3], sys.argv[4]


class Crash:
    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        if point == "before_commit":
            os._exit(17)
        self._conn.commit()
        os._exit(17)

    def __getattr__(self, name):
        return getattr(self._conn, name)


now = dt.datetime(2026, 7, 17, 12, 0, tzinfo=dt.timezone.utc)
if op == "delete":
    if hasattr(r, "_open_deletion_connection"):
        real = r._open_deletion_connection
        r._open_deletion_connection = lambda path: Crash(real(path))
        conn = sqlite3.connect(db)
    else:
        conn = Crash(sqlite3.connect(db))
    r._maybe_prune_conversation_retention(
        conn, now_utc=now, retention_days=180, force=True)
else:
    import json
    real_plan = r._request_plan

    def plan(*a, **k):
        p = real_plan(*a, **k)
        print(json.dumps({"reservation": p.reservation_bytes,
                          "steps": p.steps}), flush=True)
        return p

    r._request_plan = plan
    raw = r.open_reclaim_connection(db)
    r.normalize_reclaim_record(raw, now)
    conn = Crash(raw)
    r.reclaim_chunk(conn, now_utc=now, pages=16)
os._exit(0)
"""


@pytest.mark.parametrize("op, point", [
    ("delete", "before_commit"), ("delete", "after_commit"),
    ("reclaim", "before_commit"), ("reclaim", "after_commit")])
def test_a_crash_at_each_commit_point(tmp_path, monkeypatch, op, point):
    import _cctally_core
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    if op == "delete":
        _seed_claude(conn, "crash-me", 400)
        delete_reservation = _reservation_rows(conn, 400)
    else:
        _fill_and_free(conn)
    free_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.close()
    env = {**os.environ, "HOME": str(tmp_path),
           "CCTALLY_DATA_DIR": str(_cctally_core.APP_DIR),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    child = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD, str(_BIN), str(db_path), op, point],
        env=env, capture_output=True, text=True, timeout=120)
    assert child.returncode == 17, child.stderr
    conn = sqlite3.connect(db_path)
    try:
        record = retention.read_reclaim_pending(conn)
        if op == "delete":
            rows = _claude_rows(conn, "crash-me")
            if point == "before_commit":
                assert rows == 400 and _ledger_total(record) == 0
                return
            assert rows == 0
            reservation = delete_reservation
            assert record is not None and record.get("ledger") == [{
                "hour": "2026-07-17T12:00:00Z", "charged": reservation,
                "largest": reservation}], (
                "a committed deletion must carry its durable charge")
        else:
            free_after = conn.execute("PRAGMA freelist_count").fetchone()[0]
            if point == "before_commit":
                assert free_after == free_before and _ledger_total(record) == 0
                return
            planned = json.loads(child.stdout.strip().splitlines()[0])
            assert free_before - free_after == planned["steps"] == 16
            reservation = planned["reservation"]
            assert _ledger_total(record) == reservation
        state = retention.PacingState.from_record(record, NOW)
        assert state.available(NOW) == 4 * MiB - reservation
    finally:
        conn.close()
    if op == "delete":
        conn = ns["open_conversations_db"](attach_cache=False)
        _seed_claude(conn, "next", 1)
        assert _visit(retention, conn, NOW) is None, "no op while in debt"
        assert _claude_rows(conn, "next") == 1
        conn.close()


# ── explicit `cache-sync --prune-conversations` (primary's resolution 1) ──

def _cache_sync_prune(ns, capsys):
    import argparse

    args = argparse.Namespace(source="all", rebuild=False, prune_orphans=False,
                              prune_conversations=True)
    code = ns["cmd_cache_sync"](args)
    return code, capsys.readouterr().err


def test_an_explicit_prune_says_when_the_allowance_stops_it_early(
        tmp_path, monkeypatch, capsys):
    """I4: the explicit command is paced too. A first group whose reservation
    overdraws the one minute of credit gates the second, and the command says
    so beside its counts line, still exiting 0."""
    ns, conn, retention, _db = _env(tmp_path, monkeypatch)
    _seed_claude(conn, "a-big", 400)     # 64 KiB + 400 x 12 KiB > 4 MiB
    _seed_claude(conn, "b-small", 1)
    conn.close()
    code, err = _cache_sync_prune(ns, capsys)
    assert code == 0, err
    assert "pruned transcripts older than" in err
    assert "claude 1 session(s) / 400 message(s)" in err
    assert "[cache-sync] stopped early: the transcript maintenance allowance " \
        "(4 MiB/min) is spent; the remaining expired transcripts are pruned " \
        "on later runs or by a running dashboard." in err
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert _claude_rows(conn, "b-small") == 1, "the gated group survives"
    finally:
        conn.close()


def test_an_explicit_prune_that_starts_in_debt_says_so_not_lock_contention(
        tmp_path, monkeypatch, capsys):
    """An explicit prune that starts while the allowance is already in debt
    deletes nothing; it must report the allowance, not a lock held by another
    process, and exit 0 (the visit returned None for both, so the command
    printed the lock-contention message and exited 1)."""
    ns, conn, retention, _db = _env(tmp_path, monkeypatch)
    _seed_claude(conn, "a-big", 400)     # overdraws the one minute of credit
    _seed_claude(conn, "b-small", 1)
    conn.close()
    code, _err = _cache_sync_prune(ns, capsys)
    assert code == 0, _err
    code, err = _cache_sync_prune(ns, capsys)   # starts in debt
    assert code == 0, err
    assert "another process holds" not in err
    assert "claude 0 session(s) / 0 message(s)" in err
    assert "[cache-sync] stopped early: the transcript maintenance allowance " \
        "(4 MiB/min) is spent" in err
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert _claude_rows(conn, "b-small") == 1, "nothing deleted while in debt"
    finally:
        conn.close()


def test_an_explicit_prune_with_nothing_gated_does_not_say_stopped_early(
        tmp_path, monkeypatch, capsys):
    ns, conn, retention, _db = _env(tmp_path, monkeypatch)
    _seed_claude(conn, "only", 3)
    conn.close()
    code, err = _cache_sync_prune(ns, capsys)
    assert code == 0, err
    assert "claude 1 session(s) / 3 message(s)" in err
    assert "stopped early" not in err
