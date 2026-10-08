"""#901 Q9: every reclaim chunk is bounded by its own plan (G3v reclaim),
the runtime SQLite passes the behavior gate (G3q), and a crash at each point
of the chunk's transaction leaves neither or both of the mutation and its
charge (G3c).

Each chunk is judged on its own complete transaction, exactly as the product
runs it (plan, steps, state write, COMMIT): its frames repeat no page, it
writes no temp file, its distinct pages W satisfy |W| <= |ID| + UNID + 1 and
|W \\ ID| <= UNID + 1 (both, so a missed page and an unused reserved page
cannot cancel), and its attributable bytes are at most its own charge, which
equals the reservation recomputed from its record. No chunk is exempt and no
total stands in for one.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402
import _retention_fixtures as fx  # noqa: E402

UTC = dt.timezone.utc
KiB = 1024
MiB = 1024 * KiB
NOW = dt.datetime(2026, 7, 17, 11, 58, tzinfo=UTC)
KEY = "conversation_retention_reclaim_pending"
#: Logical bytes a chunk may write beyond its WAL frames: SQLite's own
#: wal-index bookkeeping, never a temp file (a statement journal is far
#: larger than this and reclaim keeps it in memory).
TEMP_SLACK = 64 * KiB


def _env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    conn = ns["open_conversations_db"](attach_cache=False)
    retention = importlib.import_module("_lib_conversation_retention")
    importlib.import_module("_lib_write_io").reset_for_tests()
    return ns, conn, retention, pathlib.Path(retention._resolve_main_db_path(conn))


def _ledger_total(db_path, retention):
    conn = sqlite3.connect(db_path)
    try:
        record = retention.read_reclaim_pending(conn)
    finally:
        conn.close()
    return sum(b["charged"] for b in (record or {}).get("ledger", []))


def _capture_plans(monkeypatch, retention, plans):
    """Wrap the product's plan request (absent before Q9) to keep each
    chunk's full plan, ID set included (receipt-only detail)."""
    if not hasattr(retention, "_request_plan"):
        return
    real = retention._request_plan

    def capture(*args, **kwargs):
        plan = real(*args, **kwargs)
        plans.append(plan)
        return plan

    monkeypatch.setattr(retention, "_request_plan", capture)


def _run_chunks(retention, db_path, *, count, max_steps=16, start=NOW,
                stop_on_refusal=True):
    """Run product chunks one minute apart (so each starts funded) on a
    reclaim connection, with a reader pinned before the first, and measure
    each chunk's own transaction from the WAL it appended."""
    reader = sqlite3.connect(db_path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
    rc = retention.open_reclaim_connection(db_path)
    out = []
    try:
        offset, _pages = fx.frames_between(db_path, 1 << 62)
        for index in range(count):
            charged_before = _ledger_total(db_path, retention)
            before = fx.logical_write_bytes()
            result = retention.reclaim_chunk(
                rc, now_utc=start + dt.timedelta(minutes=index),
                pages=max_steps)
            after = fx.logical_write_bytes()
            new_offset, pages = fx.frames_between(db_path, offset)
            out.append({"result": result, "pages": pages,
                        "restart": offset == fx.WAL_HEADER_BYTES,
                        "first_offset": offset,
                        "logical": None if before is None or after is None
                        else after - before,
                        "ledger": _ledger_total(db_path, retention)
                        - charged_before})
            if result.outcome != "ok":
                if stop_on_refusal:
                    break
                continue
            offset = new_offset
    finally:
        rc.close()
        reader.rollback()
        reader.close()
    return out


def _judge(retention, chunk, plan, page_size):
    """The per-chunk G3v reclaim assertions; returns the measurement."""
    result = chunk["result"]
    pages = chunk["pages"]
    written = set(pages)
    frame_cost = 2 * page_size + 24
    attributable = len(written) * frame_cost + (
        fx.WAL_HEADER_BYTES if chunk["restart"] else 0)
    measurement = {"op": result.op_id, "W": len(written),
                   "frames": len(pages), "charge": result.charged_bytes,
                   "attributable": attributable, "ledger": chunk["ledger"],
                   "logical": chunk["logical"]}
    assert len(pages) == len(written), ("a page reached the WAL twice",
                                        measurement)
    if chunk["logical"] is not None:
        temp = chunk["logical"] - len(pages) * (page_size + 24)
        assert temp <= TEMP_SLACK, ("a temp file was written", measurement)
    assert attributable <= result.charged_bytes, (
        "coverage: the chunk wrote more than its charge", measurement)
    assert chunk["ledger"] == result.charged_bytes
    if plan is not None:
        identified = set(plan.identified)
        measurement.update(ID=len(identified), UNID=plan.unidentified,
                           outside=len(written - identified))
        assert len(written) <= len(identified) + plan.unidentified + 1, \
            measurement
        assert len(written - identified) <= plan.unidentified + 1, \
            measurement
        payload = result.as_phase_payload()
        assert retention.recompute_charge(
            {"phase": "reclaim", **payload}) == result.charged_bytes
        assert payload["identified_pages"] == len(identified)
        assert payload["unidentified_pages"] == plan.unidentified
        assert payload["plan_digest"] == plan.digest
    return measurement


# ── the interior-relocation fixture (RED: revision 6's reservation) ──────

@pytest.fixture(scope="module")
def interior_template(tmp_path_factory):
    """Built once: a store whose final pages are two interior pages with
    ~450 children spread over the whole file, above a large freelist."""
    root = tmp_path_factory.mktemp("interior")
    mp = pytest.MonkeyPatch()
    try:
        ns, conn, retention, db_path = _env(root, mp)
        geometry = fx.build_interior_tail(conn)
        retention_mod = importlib.import_module("_lib_conversation_retention")
        if hasattr(retention_mod, "normalize_reclaim_record"):
            retention_mod.normalize_reclaim_record(conn, NOW)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        template = root / "interior-template.db"
        dst = sqlite3.connect(template)
        conn.backup(dst)
        dst.close()
        conn.close()
    finally:
        mp.undo()
    return template, geometry


def _store_from(template, tmp_path, monkeypatch):
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    conn.close()
    src = sqlite3.connect(template)
    dst = sqlite3.connect(db_path)
    src.backup(dst)
    dst.close()
    src.close()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    conn.close()
    return ns, retention, db_path, page_size


def test_an_interior_relocation_chunk_is_covered_by_its_own_charge(
        interior_template, tmp_path, monkeypatch):
    """One step that relocates an interior page whose children span several
    pointer-map regions. RED: revision 6's 64 KiB + 12 KiB per page charged
    77,824 bytes for it."""
    template, geometry = interior_template
    ns, retention, db_path, page_size = _store_from(template, tmp_path,
                                                    monkeypatch)
    plans = []
    _capture_plans(monkeypatch, retention, plans)
    [chunk] = _run_chunks(retention, db_path, count=1, max_steps=1)
    assert chunk["result"].outcome == "ok", chunk["result"]
    pointer_maps, _other = fx.classify(chunk["pages"], page_size, page_size)
    assert len(pointer_maps) >= 5, (
        "validity: the relocated interior's children span many regions",
        sorted(pointer_maps))
    measurement = _judge(retention, chunk, plans[0] if plans else None,
                         page_size)
    plan = plans[0]
    assert plan.kinds["interior"] == 1 and plan.steps == 1, plan.kinds
    [step] = plan.detail
    assert step["kind"] == "interior" and step["regions"] >= 5, step
    assert measurement["W"] >= 10


def test_a_sixteen_step_chunk_over_the_interior_tail_is_bounded(
        interior_template, tmp_path, monkeypatch):
    template, _geometry = interior_template
    ns, retention, db_path, page_size = _store_from(template, tmp_path,
                                                    monkeypatch)
    plans = []
    _capture_plans(monkeypatch, retention, plans)
    chunks = _run_chunks(retention, db_path, count=6)
    assert all(c["result"].outcome == "ok" for c in chunks), [
        (c["result"].outcome, c["result"].skip_reason) for c in chunks]
    for chunk, plan in zip(chunks, plans):
        _judge(retention, chunk, plan, page_size)
        assert plan.reservation_bytes <= 4 * MiB or plan.steps == 1
    kinds = {k for plan in plans for k, n in plan.kinds.items() if n}
    assert {"interior", "leaf", "free"} <= kinds, kinds


# ── the overflow drain: every chunk on its own ───────────────────────────

def test_every_chunk_of_a_fragmented_drain_is_bounded_on_its_own(
        tmp_path, monkeypatch):
    """Table leaves with overflow heads, first (non-terminal) and second
    overflow pages, index pages, free leaves and trunks, a chunk whose tail
    holds the #780 record's own leaf (relocated, after which the state write
    lands on its destination), chunks across an hour boundary that roll the
    ledger, and pointer-map pages inside the tail."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    marks = {}

    def normalize(c):
        marks["before"] = fx.page_count(c)
        retention.normalize_reclaim_record(c, NOW)
        marks["after"] = fx.page_count(c)

    fx.build_overflow_tail(conn, normalize=normalize)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    plans = []
    _capture_plans(monkeypatch, retention, plans)
    chunks = _run_chunks(retention, db_path, count=40)
    assert len(chunks) >= 20, len(chunks)
    ok = [(c, p) for c, p in zip([c for c in chunks
                                  if c["result"].outcome == "ok"], plans)]
    for chunk, plan in ok:
        _judge(retention, chunk, plan, page_size)
    details = [d for plan in plans for d in plan.detail if d.get("page")]
    kinds = {d["kind"] for d in details}
    assert {"leaf", "overflow", "free_leaf", "free_trunk"} <= kinds, kinds
    assert any(d["kind"] == "overflow" and d["children"] == 1
               for d in details), "a non-terminal overflow page"
    assert any(d["kind"] == "leaf" and d["heads"] for d in details), (
        "a leaf with overflow heads")
    assert any(d["kind"] == "free_trunk" and d["leaves"] for d in details), (
        "a non-empty trunk removal")
    record_leaf = range(marks["before"] + 1, marks["after"] + 1)
    assert any(d["page"] in record_leaf and d["kind"] == "leaf"
               for d in plans[0].detail), (
        "the first chunk relocates the record's own leaf", plans[0].detail,
        marks)
    planner = importlib.import_module("_lib_reclaim_planner")
    assert any(planner.is_ptrmap(p, page_size, page_size)
               for plan in plans
               for p in range(plan.new_end + 1, plan.page_count + 1)), (
        "a chunk crosses a pointer-map page")
    record = retention.read_reclaim_pending(sqlite3.connect(db_path))
    hours = {b["hour"] for b in record["ledger"]}
    assert len(hours) >= 2, "the chunks rolled the hourly ledger"


def test_the_first_chunk_after_a_legacy_record_is_normalized_separately(
        interior_template, tmp_path, monkeypatch):
    """A legacy-length record is normalized by its own uncharged transaction
    (measured outside the chunk); the chunk after it is bounded."""
    template, _geometry = interior_template
    ns, retention, db_path, page_size = _store_from(template, tmp_path,
                                                    monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE cache_meta SET value=? WHERE key=?",
                 (json.dumps({"freelist_count": 7, "made_progress": True}),
                  KEY))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    reader = sqlite3.connect(db_path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
    rc = retention.open_reclaim_connection(db_path)
    try:
        assert retention.normalize_reclaim_record(rc, NOW) is True
    finally:
        rc.close()
    _end, normalization = fx.frames_between(db_path, fx.WAL_HEADER_BYTES)
    reader.rollback()
    reader.close()
    assert 1 <= len(normalization) <= 4, normalization
    assert _ledger_total(db_path, retention) == 0, "uncharged"
    plans = []
    _capture_plans(monkeypatch, retention, plans)
    [chunk] = _run_chunks(retention, db_path, count=1)
    _judge(retention, chunk, plans[0], page_size)
    conn.close()


# ── G3q: the SQLite behavior gate ────────────────────────────────────────

def test_the_runtime_sqlite_is_audited_and_recorded(tmp_path, monkeypatch):
    """The gate on this runtime: it lies inside the audited range (else
    reclaim refuses until a source review widens it), and every chunk
    records `sqlite_source_id()`; the compile options are recorded here."""
    planner = importlib.import_module("_lib_reclaim_planner")
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    runtime = sqlite3.sqlite_version_info
    source_id = conn.execute("SELECT sqlite_source_id()").fetchone()[0]
    options = [row[0] for row in conn.execute("PRAGMA compile_options")]
    print(json.dumps({"sqlite": sqlite3.sqlite_version,
                      "sourceId": source_id, "compileOptions": options}))
    assert planner.sqlite_audited(runtime), (
        "this runtime's SQLite is outside the audited range; run G3q on it "
        "and review its incremental-vacuum sources before widening",
        sqlite3.sqlite_version)
    fx_rows = conn.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
    assert fx_rows is not None
    conn.execute("CREATE TABLE q_fill(payload BLOB)")
    conn.executemany("INSERT INTO q_fill VALUES (?)",
                     [(os.urandom(3000),) for _ in range(300)])
    conn.commit()
    conn.execute("DELETE FROM q_fill")
    conn.commit()
    retention.normalize_reclaim_record(conn, NOW)
    rc = retention.open_reclaim_connection(db_path)
    try:
        result = retention.reclaim_chunk(rc, now_utc=NOW, pages=16)
    finally:
        rc.close()
    assert result.outcome == "ok", result
    assert result.as_phase_payload()["sqlite_source_id"] == source_id
    conn.close()


def test_an_unaudited_sqlite_refuses_reclaim(tmp_path, monkeypatch):
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    conn.execute("CREATE TABLE q_fill(payload BLOB)")
    conn.executemany("INSERT INTO q_fill VALUES (?)",
                     [(os.urandom(3000),) for _ in range(100)])
    conn.commit()
    conn.execute("DELETE FROM q_fill")
    conn.commit()
    retention.normalize_reclaim_record(conn, NOW)
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    monkeypatch.setattr(retention, "_sqlite_version_info", lambda: (3, 55, 0))
    rc = retention.open_reclaim_connection(db_path)
    try:
        result = retention.reclaim_chunk(rc, now_utc=NOW, pages=16)
    finally:
        rc.close()
    assert (result.outcome, result.skip_reason, result.charged_bytes) == (
        "skipped", "sqlite_unaudited", 0)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free
    assert _ledger_total(db_path, retention) == 0
    conn.close()


# ── G3c: crash points inside the chunk's transaction ─────────────────────

_CRASH_CHILD = r"""
import datetime as dt, json, os, sys
sys.path.insert(0, sys.argv[1])
import _lib_conversation_retention as r
db, point = sys.argv[2], sys.argv[3]
real_write = r._write_reclaim_state_in_chunk
real_plan = r._request_plan


def plan(*a, **k):
    p = real_plan(*a, **k)
    print(json.dumps({"reservation": p.reservation_bytes,
                      "steps": p.steps}), flush=True)
    return p


def write(conn, record):
    if point == "after_steps":
        os._exit(17)
    real_write(conn, record)
    if point == "after_state_write":
        os._exit(17)


class Crash:
    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        self._conn.commit()
        if point == "after_commit":
            os._exit(17)

    def __getattr__(self, name):
        return getattr(self._conn, name)


r._request_plan = plan
r._write_reclaim_state_in_chunk = write
now = dt.datetime(2026, 7, 17, 12, 0, tzinfo=dt.timezone.utc)
raw = r.open_reclaim_connection(db)
r.reclaim_chunk(Crash(raw), now_utc=now, pages=16)
os._exit(0)
"""


@pytest.mark.parametrize("point", ["after_steps", "after_state_write",
                                   "after_commit"])
def test_a_crash_at_each_point_of_the_chunk(tmp_path, monkeypatch, point):
    """Between the steps and the state write, and between the state write
    and COMMIT, neither the mutation nor the reservation is durable; after
    COMMIT both are, and the balance is lower by exactly the reservation."""
    import _cctally_core

    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    conn.execute("CREATE TABLE c_fill(payload BLOB)")
    conn.executemany("INSERT INTO c_fill VALUES (?)",
                     [(os.urandom(3000),) for _ in range(300)])
    conn.commit()
    conn.execute("DELETE FROM c_fill")
    conn.commit()
    retention.normalize_reclaim_record(conn, NOW)
    free_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.close()
    env = {**os.environ, "HOME": str(tmp_path),
           "CCTALLY_DATA_DIR": str(_cctally_core.APP_DIR),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    child = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD, str(_BIN), str(db_path), point],
        env=env, capture_output=True, text=True, timeout=120)
    assert child.returncode == 17, child.stderr
    planned = json.loads(child.stdout.strip().splitlines()[0])
    conn = sqlite3.connect(db_path)
    try:
        free_after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        total = sum(b["charged"] for b in (
            retention.read_reclaim_pending(conn) or {}).get("ledger", []))
        if point != "after_commit":
            assert (free_after, total) == (free_before, 0)
            return
        assert free_before - free_after == planned["steps"]
        assert total == planned["reservation"]
        state = retention.PacingState.from_record(
            retention.read_reclaim_pending(conn), NOW)
        assert state.balance_bytes == 4 * MiB - planned["reservation"]
    finally:
        conn.close()
