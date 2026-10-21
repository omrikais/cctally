"""#901 spec §5.4 / G3v: every operation's durable charge covers its
attributable writes, at fixture scale (non-vacuous).

Per operation kind — a 1-page and a 16-page reclaim chunk, a spill-free
deletion and one in the Q5 fallback mode (threshold injected) — each run
starts from a fully checkpointed WAL (that baseline checkpoint is not
counted), calls the product operation exactly as the visit does, and ends
with a forced `wal_checkpoint(TRUNCATE)`. Attributable bytes are the WAL frames
the operation wrote (page size + 24 each, plus the 32-byte header), the
checkpoint copy of every distinct page they hold, and its temp-file bytes,
counted directly (#940 revision 7j, spec §6.7) by the write accounting VFS
in this process, in the crash child and in the foreign checkpointer. They
must not exceed the ledger's durable charge.

Variants: a reader pins the WAL across the COMMIT and a SECOND process
performs the copy after it releases; the product's process counter is
unavailable (the independent accounting stays mandatory); the operation
crashes immediately after COMMIT, and the child publishes its counters first.
The charge is
identical in every variant. A validity guard requires each operation to
write at least one WAL frame of its own, and a mutation-free control proves
the guard can fail.

The Q8 fixture assumes freed pages are not overwritten, so its builder and
deletion connections set `secure_delete` explicitly and check its effective
value first, with an `ON` companion and mismatch controls (#940 revision 7k,
spec §6.7); each Q8 measurement prints one `Q8-RECORD` line.

A coverage failure prints the measurement. The §5.4 rule then applies: the
failing reservation part is raised to at least 1.25x its measured maximum,
rounded up to a KiB, in the constants block — never left below coverage.
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
NOW = dt.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
OLD = "2025-08-01T12:00:00.000Z"
FRESH = "2026-07-17T08:00:00.000Z"
CUTOFF = "2026-01-18T12:00:00Z"
SQLITE_DBSTATUS_CACHE_WRITE = 9


def _cache_writes(conn) -> int:
    """`SQLITE_DBSTATUS_CACHE_WRITE` for this connection: every page it wrote
    to the WAL, spills and in-place rewrites included. Reaches the `sqlite3*`
    through CPython's connection layout (PyObject_HEAD, then the handle), the
    contract `bin/_lib_sqlite_close.py` already relies on."""
    import ctypes
    import _sqlite3

    real = getattr(conn, "_conn", conn)
    fn = ctypes.CDLL(_sqlite3.__file__).sqlite3_db_status
    fn.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
                   ctypes.POINTER(ctypes.c_int), ctypes.c_int)
    fn.restype = ctypes.c_int
    handle = ctypes.c_void_p.from_address(
        id(real) + 2 * ctypes.sizeof(ctypes.c_void_p)).value
    current, high = ctypes.c_int(), ctypes.c_int()
    assert fn(ctypes.c_void_p(handle), SQLITE_DBSTATUS_CACHE_WRITE,
              ctypes.byref(current), ctypes.byref(high), 0) == 0
    return int(current.value)


@pytest.fixture(scope="module", autouse=True)
def _write_accounting():
    """Active before any builder opens a connection, in this process."""
    with fx.write_accounting():
        yield


def _wal_pages(db_path):
    """Page numbers of the valid frames in the current WAL generation."""
    wal = pathlib.Path(f"{db_path}-wal")
    if not wal.exists():
        return [], 4096
    data = wal.read_bytes()
    if len(data) < 32:
        return [], 4096
    page_size = int.from_bytes(data[8:12], "big")
    salt = data[16:24]
    pages = []
    offset = 32
    while offset + 24 + page_size <= len(data):
        header = data[offset:offset + 24]
        if header[8:16] != salt:
            break
        pages.append(int.from_bytes(header[0:4], "big"))
        offset += 24 + page_size
    return pages, page_size


def _ledger_total(db_path, retention):
    conn = sqlite3.connect(db_path)
    try:
        record = retention.read_reclaim_pending(conn)
    finally:
        conn.close()
    return sum(b["charged"] for b in (record or {}).get("ledger", []))


def _truncate(db_path):
    conn = sqlite3.connect(db_path)
    try:
        fx.require_attributed(conn, "checkpoint writer")
        busy, log, done = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    assert busy == 0 and log == done


#: The second process that performs the copy: it activates the accounting
#: before it connects and publishes its own checked counters.
_FOREIGN_CHECKPOINT = r"""
import sqlite3, sys
sys.path.insert(0, sys.argv[1])
import _sqlite_write_accounting as acct
acct.activate()
interval = acct.measure("foreign checkpoint").begin()
c = sqlite3.connect(sys.argv[2])
attributed = acct.is_attributed(c)
print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(), flush=True)
c.close()
acct.publish(interval.end(), attributed=attributed)
"""


def _foreign_truncate(db_path):
    """The copy by a second process; returns its directly counted writes."""
    child = subprocess.run(
        [sys.executable, "-c", _FOREIGN_CHECKPOINT, str(fx.TESTS_DIR),
         str(db_path)],
        capture_output=True, text=True, timeout=60)
    _record, writes = fx.child_writes(child.stdout, "foreign checkpoint",
                                      stderr=child.stderr)
    assert child.returncode == 0, child.stderr
    assert child.stdout.startswith("(0,"), child.stdout
    return writes


def _store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    conn = ns["open_conversations_db"](attach_cache=False)
    retention = importlib.import_module("_lib_conversation_retention")
    wio = importlib.import_module("_lib_write_io")
    wio.reset_for_tests()
    return ns, conn, retention, wio, pathlib.Path(
        retention._resolve_main_db_path(conn))


def _seed_groups(conn, sizes):
    """Expiring groups `g-<n>` of the given sizes, interleaved row by row
    with a surviving group, so a deletion dirties shared pages."""
    offset = 0
    longest = max(sizes)
    for i in range(longest):
        for key, rows, ts in [(f"g-{n}", n, OLD) for n in sizes] + [
                ("keeper", longest, FRESH)]:
            if i >= rows:
                continue
            offset += 1
            conn.execute(
                "INSERT INTO conversation_messages (session_id, uuid, "
                "source_path, byte_offset, timestamp_utc, entry_type, text, "
                "blocks_json) VALUES (?,?,?,?,?,?,?,'[]')",
                (key, f"u-{offset}", f"/{key}.jsonl", offset, ts, "assistant",
                 " ".join(f"tok{(offset * 13 + j) % 4000}" for j in range(120))))
    conn.commit()


def _interior_freelist(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS g3v_a(payload BLOB)")
    conn.execute("CREATE TABLE IF NOT EXISTS g3v_b(payload BLOB)")
    for _ in range(400):
        conn.execute("INSERT INTO g3v_a VALUES (?)", (os.urandom(3000),))
        conn.execute("INSERT INTO g3v_b VALUES (?)", (os.urandom(3000),))
    conn.commit()
    conn.execute("DELETE FROM g3v_a")
    conn.commit()


def _copy(conn, target):
    dst = sqlite3.connect(target)
    try:
        conn.backup(dst)
    finally:
        dst.close()
    return target


def _run_op(retention, db_path, kind, units):
    """Run one product operation; return it with the frames its own
    connection wrote (spills and in-place rewrites included)."""
    if kind == "reclaim":
        conn = retention.open_reclaim_connection(db_path)
        try:
            fx.require_attributed(conn, "reclaim connection")
            result = retention.reclaim_chunk(conn, now_utc=NOW, pages=units)
            return result, _cache_writes(conn)
        finally:
            conn.close()
    conn = retention.open_deletion_connection(db_path)
    try:
        fx.require_attributed(conn, "deletion connection")
        result = retention.delete_group(
            conn, retention.ExpiredGroup("claude", False, f"g-{units}"),
            now_utc=NOW, cutoff_iso=CUTOFF)
        return result, _cache_writes(conn)
    finally:
        conn.close()


_CRASH_CHILD = r"""
import datetime as dt, json, os, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import _lib_conversation_retention as r
import _retention_fixtures as fx
import _sqlite_write_accounting as acct
db, kind, units = sys.argv[3], sys.argv[4], int(sys.argv[5])
if kind == "fallback":
    r.DELETION_FALLBACK_ROW_THRESHOLD = 10
acct.activate()
interval = acct.measure("crash child").begin()
if kind == "reclaim":
    raw = r.open_reclaim_connection(db)
else:
    raw = r.open_deletion_connection(db)


class Crash:
    def __init__(self, conn):
        # #940 F1 review #16: attribution is checked when the connection is
        # opened, before the operation's first statement, and a bypass is
        # published at once.
        if not acct.is_attributed(conn):
            acct.publish(interval.end(), frames=0, attributed=False)
            os._exit(18)
        self._conn = conn

    def commit(self):
        self._conn.commit()
        acct.publish(interval.end(), frames=fx.cache_writes(self._conn),
                     attributed=acct.is_attributed(self._conn))
        os._exit(17)

    def __getattr__(self, name):
        return getattr(self._conn, name)


now = dt.datetime(2026, 7, 17, 12, 0, tzinfo=dt.timezone.utc)
if kind == "reclaim":
    r.reclaim_chunk(Crash(raw), now_utc=now, pages=units)
else:
    r.delete_group(Crash(raw), r.ExpiredGroup("claude", False, "g-%d" % units),
                   now_utc=now, cutoff_iso="2026-01-18T12:00:00Z")
os._exit(0)
"""


def _measure(retention, db_path, kind, units, variant, monkeypatch, wio):
    _truncate(db_path)                       # the baseline, never counted
    charged_before = _ledger_total(db_path, retention)
    # Keeps any operation-side close from being the LAST close, which would
    # checkpoint and delete the WAL before its frames are counted.
    holder = sqlite3.connect(db_path)
    reader = None
    checkpoint = None
    try:
        holder.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
        if variant == "crash":
            env = {**os.environ, "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
            child = subprocess.run(
                [sys.executable, "-c", _CRASH_CHILD, str(_BIN),
                 str(fx.TESTS_DIR), str(db_path),
                 kind if kind != "delete-fallback" else "fallback",
                 str(units)],
                capture_output=True, text=True, timeout=120, env=env)
            report, writes = fx.child_writes(child.stdout, "crash child",
                                             stderr=child.stderr)
            assert child.returncode == 17, child.stderr
            frames_written = report["frames"]
            assert isinstance(frames_written, int), report
        else:
            if variant == "pinned":
                reader = sqlite3.connect(db_path)
                reader.execute("BEGIN")
                reader.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
            if variant == "no-counter":
                wio.reset_for_tests(
                    counter=wio.ProcessWriteCounter(platform="win32"))
            with fx.measure("operation") as interval:
                result, frames_written = _run_op(
                    retention, db_path,
                    "reclaim" if kind == "reclaim" else "delete", units)
            writes = interval.writes
            assert result.outcome == "ok", result
        pages, page_size = _wal_pages(db_path)
        if reader is not None:
            reader.rollback()
            reader.close()
            reader = None
            checkpoint = _foreign_truncate(db_path)
            holder.close()
        else:
            holder.close()
            _truncate(db_path)
    finally:
        if reader is not None:
            reader.close()
        holder.close()
    wal_bytes = frames_written * (page_size + 24) + (32 if pages else 0)
    copy_bytes = len(set(pages)) * page_size
    checkpoint_temp = 0 if checkpoint is None else checkpoint.temp
    temp_bytes = writes.temp + checkpoint_temp
    return {
        "kind": kind, "units": units, "variant": variant,
        "payload": (None if variant == "crash"
                    else result.as_phase_payload()),
        "frames": frames_written, "distinct_pages": len(set(pages)),
        "wal_bytes": wal_bytes,
        "copy_bytes": copy_bytes, "temp_bytes": temp_bytes,
        "checkpoint_temp": checkpoint_temp,
        "write_classes": writes.summary()["classes"],
        "attributable": wal_bytes + copy_bytes + temp_bytes,
        "charge": _ledger_total(db_path, retention) - charged_before,
    }


def _valid(measurement) -> bool:
    return measurement["frames"] >= 1


VARIANTS = ("base", "pinned", "no-counter", "crash")


@pytest.mark.parametrize("kind, units", [
    ("reclaim", 1), ("reclaim", 16),
    ("delete", 50), ("delete", 300),
    ("delete-fallback", 50), ("delete-fallback", 300)])
def test_the_charge_covers_every_attributable_write(
        tmp_path, monkeypatch, kind, units):
    ns, conn, retention, wio, db_path = _store(tmp_path, monkeypatch)
    try:
        if kind == "reclaim":
            _interior_freelist(conn)
            # §5.4: the record is normalized outside any chunk, before the run.
            conn.commit()
            retention.normalize_reclaim_record(conn, NOW)
        else:
            _seed_groups(conn, [units])
        if kind == "delete-fallback":
            monkeypatch.setattr(retention, "DELETION_FALLBACK_ROW_THRESHOLD",
                                10)
        template = _copy(conn, tmp_path / "g3v-template.db")
    finally:
        conn.close()
    results = []
    for variant in VARIANTS:
        target = tmp_path / f"g3v-{variant}.db"
        src = sqlite3.connect(template)
        try:
            _copy(src, target)
        finally:
            src.close()
        wio.reset_for_tests()
        measurement = _measure(retention, target, kind, units, variant,
                               monkeypatch, wio)
        results.append(measurement)
        assert _valid(measurement), measurement
        assert measurement["attributable"] <= measurement["charge"], (
            "coverage: raise the failing reservation part per §5.4",
            json.dumps(measurement, sort_keys=True))
    charges = {m["charge"] for m in results}
    assert len(charges) == 1, ("the charge is identical in every variant",
                               results)
    if kind == "reclaim":
        # Q9: the charge is the chunk's planned reservation, recomputed from
        # the operation record of the run that kept its result.
        [payload] = [m["payload"] for m in results if m.get("payload")][:1]
        expected = retention.recompute_charge({"phase": "reclaim", **payload})
    else:
        page_size, page_count = _geometry(template)
        expected = retention.deletion_reservation(
            units, page_count=page_count, usable_size=page_size,
            page_size=page_size)
    assert charges == {expected}


def _geometry(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return (int(conn.execute("PRAGMA page_size").fetchone()[0]),
                int(conn.execute("PRAGMA page_count").fetchone()[0]))
    finally:
        conn.close()


# ── Q8: a small, overflow-heavy group over a fragmented file ──────────────

def _scatter_store(tmp_path, monkeypatch, *, secure_delete, builder):
    """The Q8 store, built by `builder` on connections set to
    `secure_delete` and checked before the fixture is built (revision 7k):
    `precreate_store`'s own connection and the product opener's."""
    import _cctally_core

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    precreated = fx.precreate_store(_cctally_core.CONVERSATIONS_DB_PATH,
                                    fx.SCATTER_PAGE_SIZE,
                                    secure_delete=secure_delete)
    conn = ns["open_conversations_db"](attach_cache=False)
    retention = importlib.import_module("_lib_conversation_retention")
    importlib.import_module("_lib_write_io").reset_for_tests()
    try:
        opened = fx.set_secure_delete(conn, secure_delete,
                                      "builder connection")
        geometry = builder(conn, key=_Q8_KEY, ts=OLD, keeper_ts=FRESH)
        db_path = pathlib.Path(retention._resolve_main_db_path(conn))
    finally:
        conn.close()
    return retention, db_path, geometry, {"precreate_store": precreated,
                                          "product_opener": opened}


#: The expiring Q8 group's key (`fx.build_scattered_codex_group`'s default).
_Q8_KEY = "q8-expire"


def _q8_rows_left(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_events "
            "WHERE conversation_key = ?", (_Q8_KEY,)).fetchone()[0])
    finally:
        conn.close()


def _pointer_map_page_bytes(page_size):
    """B: 1.25 x (2P + 24) rounded up to a KiB (spec §5.4), computed here
    independently of the product."""
    return -(-(5 * (2 * page_size + 24)) // (4 * KiB)) * KiB


def _independent_pointer_map_cap(page_count, usable_size, rows, page_size):
    growth = -(-(512 * KiB + 12 * KiB * rows) // page_size)
    per_map = usable_size // 5 + 1
    return -(-(page_count + growth) // per_map) + 1


def _q8(tmp_path, monkeypatch, *, required, deletion_actual=None,
        builder=fx.build_scattered_codex_group):
    """The Q8 body: build the group with `builder` at `secure_delete=
    required`, delete it with the product's `delete_group` on a deletion
    connection set to `deletion_actual` (default `required`) and checked
    against `required` before its first statement, and judge each component
    against the durable charge. `required=0` keeps the dominance guard;
    `required=1`, the companion, keeps every other bound. Prints one
    `Q8-RECORD` line and returns the measurement."""
    if deletion_actual is None:
        deletion_actual = required
    retention, db_path, geometry, built = _scatter_store(
        tmp_path, monkeypatch, secure_delete=required, builder=builder)
    _truncate(db_path)
    page_size, page_count = _geometry(db_path)
    regions = fx.overflow_regions(db_path, page_size)
    charged_before = _ledger_total(db_path, retention)
    holder = sqlite3.connect(db_path)
    reader = sqlite3.connect(db_path)
    try:
        holder.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
        with fx.measure("operation") as interval:
            conn = retention.open_deletion_connection(db_path)
            try:
                fx.set_secure_delete(conn, deletion_actual,
                                     "deletion connection")
                deletion = fx.require_secure_delete(conn, required,
                                                    "deletion connection")
                fx.require_attributed(conn, "deletion connection")
                result = retention.delete_group(
                    conn, retention.ExpiredGroup("codex", False,
                                                 geometry["key"]),
                    now_utc=NOW, cutoff_iso=CUTOFF)
                written = fx.cache_writes(conn)
            finally:
                conn.close()
        _wal_page_size, frames = fx.wal_frames(db_path)
        pages = [pgno for _offset, pgno, _commit in frames]
        reader.rollback()
    finally:
        reader.close()
        holder.close()
    checkpoint = _foreign_truncate(db_path)
    # #940 F1 fix round 3 (R3-2): after the deletion, no overflow chain traces
    # back to the events table; the file is whole after the TRUNCATE above.
    regions_after = fx.overflow_regions(db_path, page_size)
    rows = result.rows
    assert (result.outcome, rows) == ("ok", geometry["events"]), result
    ptrmap, other = fx.classify(pages, page_size, page_size)
    frame_cost = 2 * page_size + 24
    wal_bytes = written * (page_size + 24) + fx.WAL_HEADER_BYTES
    # Temp-file bytes, counted directly in this process and in the foreign
    # checkpointer, are charged to the operation as well (#940 revision 7j).
    temp = interval.writes.temp + checkpoint.temp
    attributable = wal_bytes + len(set(pages)) * page_size + temp
    charge = _ledger_total(db_path, retention) - charged_before
    measurement = {"module": "charge_coverage", "builder": builder.__name__,
                   "required": required, "rows": rows,
                   "pointer_map_pages": len(ptrmap),
                   "other_pages": len(other), "frames": written,
                   "attributable": attributable, "charge": charge,
                   "temp": temp, "page_count": page_count,
                   "overflow_regions": regions,
                   "overflow_regions_after_deletion": regions_after,
                   "secure_delete": {"builder": built,
                                     "deletion": [deletion]}}
    print("Q8-RECORD " + json.dumps(measurement, sort_keys=True), flush=True)
    assert regions_after == 0, (
        "no overflow chain of the events table survives the deletion",
        measurement)
    if required == 0:
        assert len(ptrmap) > len(other), (
            "validity: pointer-map pages outnumber the others", measurement)
    assert written == len(set(pages)), ("spill-free: no page twice",
                                        measurement)
    assert attributable <= charge, (
        "coverage: raise the failing reservation part per §5.4",
        json.dumps(measurement, sort_keys=True))
    m_cap = _independent_pointer_map_cap(page_count, page_size, rows,
                                         page_size)
    assert len(ptrmap) * frame_cost <= _pointer_map_page_bytes(page_size) * m_cap
    assert (len(other) * frame_cost + fx.WAL_HEADER_BYTES + temp
            <= 512 * KiB + 15 * KiB * rows)
    payload = result.as_phase_payload()
    assert (payload["page_count"], payload["usable_size"],
            payload["page_size"], payload["pointer_map_cap"]) == (
        page_count, page_size, page_size, m_cap)
    assert payload["reservation_version"] == retention.RESERVATION_VERSION
    assert retention.recompute_charge({"phase": "delete", **payload}) == charge
    assert payload["charged_bytes"] == charge
    return measurement


def test_q8_a_scattered_overflow_deletion_is_covered_per_component(
        tmp_path, monkeypatch):
    """G3v Q8: the deletion of a small Codex conversation whose freed overflow
    pages lie in many pointer-map regions is covered, per component, by the
    durable charge, and the charge recomputed from the operation record's
    inputs equals it. RED: revision 5's 64 KiB + 12 KiB per row. Its validity
    guard holds only while freed pages are not overwritten
    (`secure_delete=OFF`, revision 7k)."""
    _q8(tmp_path, monkeypatch, required=0)


def test_q8_the_secure_delete_on_companion_is_covered_per_component(
        tmp_path, monkeypatch):
    """Revision 7k: the same scattered workload with `secure_delete=ON` on
    the builder and the deletion connection removes the eight rows, its
    attributable bytes stay within the charge, and every bound except the
    dominance guard holds."""
    _q8(tmp_path, monkeypatch, required=1)


@pytest.mark.parametrize("required, deletion_actual", [
    pytest.param(0, 1, id="off-precondition"),
    pytest.param(1, 0, id="on-companion")])
def test_q8_a_secure_delete_mismatch_is_rejected(
        tmp_path, monkeypatch, required, deletion_actual):
    """Revision 7k: the builder reports the required value while the actual
    deletion connection reports the other; the precondition rejects it
    before the measured deletion runs, so the group's rows remain."""
    import _cctally_core

    with pytest.raises(AssertionError, match="precondition: "):
        _q8(tmp_path, monkeypatch, required=required,
            deletion_actual=deletion_actual)
    assert _q8_rows_left(_cctally_core.CONVERSATIONS_DB_PATH) == \
        fx.SCATTER_EVENTS


def test_missing_child_evidence_carries_the_child_stderr():
    """#940 F1 review #5: a child that publishes no evidence fails with its
    stderr in the message."""
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.stderr.write('q8-child-stderr-marker')"],
        capture_output=True, text=True, timeout=60)
    with pytest.raises(fx.acct.MeasurementInvalid,
                       match="crash child: child write evidence missing.*"
                             "q8-child-stderr-marker"):
        fx.child_writes(child.stdout, "crash child", stderr=child.stderr)


def test_the_validity_guard_can_fail(tmp_path, monkeypatch):
    """A mutation-free operation writes no frame of its own."""
    ns, conn, retention, wio, db_path = _store(tmp_path, monkeypatch)
    try:
        conn.executescript("PRAGMA incremental_vacuum;")
        retention.normalize_reclaim_record(conn, NOW)
    finally:
        conn.close()
    _truncate(db_path)
    rc = retention.open_reclaim_connection(db_path)
    try:
        result = retention.reclaim_chunk(rc, now_utc=NOW, pages=1)
    finally:
        rc.close()
    pages, _ = _wal_pages(db_path)
    assert result.outcome == "no_progress"
    assert not _valid({"frames": len(pages)})
