"""#901 spec §5.4 / G3s: whole-group deletion is spill-free (non-vacuous).

The product deletion path (the dashboard's retention visit) must write at
most 1.05x the WAL frames of a control that deletes the same group on an
identical copy with an unbounded cache (the group's distinct dirty pages),
and no temp file. A validity guard requires the default-cache control to
write at least twice the unbounded control's frames, so the fixture cannot
shrink into vacuity.

Frames WRITTEN are counted with `SQLITE_DBSTATUS_CACHE_WRITE` on the
connection that deletes: SQLite rewrites a frame in place when a page is
spilled twice inside one transaction, so the WAL file's size cannot show the
re-spill this test exists to catch. The rows carry random keys in several
indexed columns (as production rows do), which is what makes the default
cache re-dirty pages it already spilled. A reader is pinned during every
measurement so no checkpoint copy lands in the logical write count.

The spill-free cases use only names that also exist on 56e66f07a, so the
same module is the RED proof there.
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import pathlib
import random
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
GROUP_ROWS = 6000
#: The Codex tables' indexes are narrower than Claude's, so a 6,000-row group
#: fits the default cache and never re-spills (measured: 15,646 default vs
#: 13,603 unbounded frames, ratio 1.15; 20,000 rows gave 2.10). The fixture is
#: sized so the validity guard below holds with margin (24,000 rows: 2.29).
CODEX_GROUP_ROWS = 24000
TEMP_SLACK = 64 * KiB
SQLITE_DBSTATUS_CACHE_WRITE = 9

_CLAUDE_DELETES = (
    "DELETE FROM conversation_file_touches WHERE session_id = ?",
    "DELETE FROM conversation_messages WHERE session_id = ?",
    "DELETE FROM conversation_ai_titles WHERE session_id = ?",
    "DELETE FROM conversation_sessions WHERE session_id = ?",
)
_CODEX_DELETES = (
    "DELETE FROM codex_conversation_file_touches WHERE conversation_key = ?",
    "DELETE FROM codex_conversation_messages WHERE conversation_key = ?",
    "DELETE FROM codex_conversation_rollups WHERE conversation_key = ?",
    "DELETE FROM codex_conversation_events WHERE conversation_key = ?",
)


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


def _logical_write_bytes():
    """Bytes this process passed to write(): macOS `ri_logical_writes`
    (RUSAGE_INFO_V4), Linux `/proc/self/io` `wchar`; None elsewhere."""
    if sys.platform == "darwin":
        import ctypes

        names = [f"f{i}" for i in range(27)] + ["logical_writes"] + [
            f"g{i}" for i in range(7)]

        class V4(ctypes.Structure):
            _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
                (n, ctypes.c_uint64) for n in names]

        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        lib.proc_pid_rusage.argtypes = (ctypes.c_int, ctypes.c_int,
                                        ctypes.c_void_p)
        lib.proc_pid_rusage.restype = ctypes.c_int
        info = V4()
        if lib.proc_pid_rusage(os.getpid(), 4, ctypes.byref(info)) != 0:
            return None
        return int(info.logical_writes)
    try:
        with open("/proc/self/io", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("wchar:"):
                    return int(line.split(":")[1])
    except OSError:
        return None
    return None


def _text(i):
    return f"{i:08d} " + " ".join(f"tok{(i * 7 + j) % 5000}" for j in range(150))


def _stamp(rng, base):
    return (base + dt.timedelta(seconds=rng.randrange(86_400 * 20))
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _build(tmp_path, monkeypatch, provider):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    conn = ns["open_conversations_db"](attach_cache=False)
    retention = importlib.import_module("_lib_conversation_retention")
    try:
        importlib.import_module("_lib_write_io").reset_for_tests()
    except ModuleNotFoundError:
        pass
    rng = random.Random(901)
    old = dt.datetime(2025, 8, 1, tzinfo=UTC)
    fresh = dt.datetime(2026, 6, 20, tzinfo=UTC)
    groups = (("expire-me", old), ("keep-1", fresh), ("keep-2", fresh))
    offset = 0
    rows = GROUP_ROWS if provider == "claude" else CODEX_GROUP_ROWS
    for i in range(rows):
        for key, base in groups:
            offset += 1
            path = f"/{rng.getrandbits(64):016x}.jsonl"
            if provider == "claude":
                conn.execute(
                    "INSERT INTO conversation_messages (session_id, uuid, "
                    "source_path, byte_offset, timestamp_utc, entry_type, "
                    "text, blocks_json, model, msg_id, req_id, cwd) "
                    "VALUES (?,?,?,?,?,?,?,'[]',?,?,?,?)",
                    (key, f"u-{offset}", path, offset, _stamp(rng, base),
                     "assistant", _text(offset),
                     f"model-{rng.randrange(1000)}",
                     f"{rng.getrandbits(96):024x}",
                     f"{rng.getrandbits(64):016x}",
                     f"/work/{rng.getrandbits(48):012x}"))
            else:
                ts = _stamp(rng, base)
                conn.execute(
                    "INSERT INTO codex_conversation_events (source_path, "
                    "line_offset, source_root_key, conversation_key, "
                    "timestamp_utc, payload_json) VALUES (?,?,?,?,?,?)",
                    (path, offset, "root-a", key, ts,
                     '{"t":"' + _text(offset) + '"}'))
                conn.execute(
                    "INSERT INTO codex_conversation_messages "
                    "(conversation_key, source_root_key, source_path, "
                    "line_offset, timestamp_utc, turn_id, call_id, kind, "
                    "event_type, record_family, model, text, content_digest, "
                    "content_len, detail_json, search_tool, search_thinking) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, "root-a", path, offset, ts, "turn-a", None,
                     "assistant", None, "response_item", "gpt-x",
                     _text(offset), "d" * 32, len(_text(offset)), None, "", ""))
        if i % 500 == 499:
            conn.commit()
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db_path = pathlib.Path(retention._resolve_main_db_path(conn))
    copies = {}
    for name in ("default", "unbounded"):
        target = db_path.parent / f"g3s-{name}.db"
        dst = sqlite3.connect(target)
        conn.backup(dst)
        dst.close()
        copies[name] = target
    return ns, conn, retention, db_path, copies


class _Pinned:
    """A reader pinned at a truncated WAL, plus logical write bytes."""

    def __init__(self, db_path):
        self.db_path = db_path

    def __enter__(self):
        c = sqlite3.connect(self.db_path)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        self.reader = sqlite3.connect(self.db_path)
        self.reader.execute("BEGIN")
        self.reader.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
        self.before = _logical_write_bytes()
        return self

    def __exit__(self, *exc):
        after = _logical_write_bytes()
        self.logical = (None if after is None or self.before is None
                        else after - self.before)
        self.reader.rollback()
        self.reader.close()
        return False


def _control(path, provider, *, unbounded):
    conn = sqlite3.connect(path)
    if unbounded:
        conn.execute("PRAGMA cache_size = -1048576")
        conn.execute("PRAGMA temp_store = MEMORY")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    for sql in (_CLAUDE_DELETES if provider == "claude" else _CODEX_DELETES):
        conn.execute(sql, ("expire-me",))
    conn.commit()
    frames = _cache_writes(conn)
    conn.close()
    return frames, page_size


def _instrument(monkeypatch, retention, seen):
    """On the candidate, wrap the per-visit deletion connection to record its
    frames written by the group's commit and its (temp_store, cache_size).
    Returns False on 56e66f07a, where deletion runs on the caller's
    connection and no seam exists."""
    if not hasattr(retention, "_open_deletion_connection"):
        return False
    real_open = retention._open_deletion_connection

    class Spy:
        def __init__(self, conn):
            self._conn = conn

        def commit(self):
            result = self._conn.commit()
            seen.append({
                "frames": _cache_writes(self._conn),
                "temp_store": int(self._conn.execute(
                    "PRAGMA temp_store").fetchone()[0]),
                "cache_size": int(self._conn.execute(
                    "PRAGMA cache_size").fetchone()[0]),
            })
            return result

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(retention, "_open_deletion_connection",
                        lambda path: Spy(real_open(path)))
    return True


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_spill_free_deletion_writes_each_dirty_page_once(
        tmp_path, monkeypatch, provider):
    ns, conn, retention, db_path, copies = _build(tmp_path, monkeypatch,
                                                  provider)
    with _Pinned(copies["default"]) as default:
        default_frames, page_size = _control(copies["default"], provider,
                                             unbounded=False)
    with _Pinned(copies["unbounded"]) as unbounded:
        unbounded_frames, _ = _control(copies["unbounded"], provider,
                                       unbounded=True)
    assert default_frames >= 2 * unbounded_frames, (
        "validity: the group must make the default cache re-spill",
        default_frames, unbounded_frames)

    seen = []
    instrumented = _instrument(monkeypatch, retention, seen)
    with _Pinned(db_path) as product:
        before = 0 if instrumented else _cache_writes(conn)
        stats = retention._maybe_prune_conversation_retention(
            conn, now_utc=NOW, retention_days=180, force=True)
        product_frames = (seen[0]["frames"] if instrumented
                          else _cache_writes(conn) - before)
    assert stats is not None
    assert product_frames <= 1.05 * unbounded_frames, (
        "the product deletion re-spilled dirty pages to the WAL",
        product_frames, unbounded_frames)
    assert (seen[0]["temp_store"], seen[0]["cache_size"]) == (2, -262144), (
        "temp_store=MEMORY under the 256 MiB cache cap")
    if default.logical is not None:
        # Logical writes beyond the WAL frames also include SQLite's own
        # wal-index and header traffic, which grows with the frame count
        # (measured: 0.18 MiB for the 6,000-row Claude group's unbounded
        # control, 1.4 MiB for the 24,000-row Codex one). The unbounded
        # control keeps its statement journal in memory by construction, so
        # its excess is that baseline; a temp file is what exceeds it.
        frame_bytes = page_size + 24
        baseline = unbounded.logical - unbounded_frames * frame_bytes
        assert (default.logical - default_frames * frame_bytes
                - baseline) > TEMP_SLACK, (
            "validity: the default control's statement journal reaches a "
            "temp file the logical counter sees")
        written = seen[-1]["frames"] * frame_bytes   # cumulative per conn
        assert product.logical - written - baseline <= TEMP_SLACK, (
            "the product deletion wrote a temp file",
            product.logical - written, baseline)
    # #901 §5.3a (Q11): a fresh ordinary writer keeps SQLite's default cache
    # size and holds its statement journals in memory.
    fresh = ns["open_conversations_db"](attach_cache=False)
    try:
        assert fresh.execute("PRAGMA cache_size").fetchone()[0] == -2000
        assert fresh.execute("PRAGMA temp_store").fetchone()[0] == 2
    finally:
        fresh.close()
    conn.close()


def test_a_group_above_the_row_threshold_takes_the_bounded_fallback(
        tmp_path, monkeypatch):
    ns, conn, retention, db_path, _copies = _build(tmp_path, monkeypatch,
                                                   "claude")
    monkeypatch.setattr(retention, "DELETION_FALLBACK_ROW_THRESHOLD", 100)
    seen = []
    assert _instrument(monkeypatch, retention, seen)
    ops = []
    with _Pinned(db_path) as product:
        retention._maybe_prune_conversation_retention(
            conn, now_utc=NOW, retention_days=180, force=True,
            record_phase=lambda kind, payload: ops.append(payload))
    [op] = ops
    assert op["mode"] == "fallback"
    assert op["charged_bytes"] == retention.recompute_charge(
        {"phase": "delete", **op})
    assert (seen[0]["temp_store"], seen[0]["cache_size"]) == (1, -262144), (
        "the file temp store for the statement journal, the cache cap kept")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    wal_bytes = seen[0]["frames"] * (page_size + 24)
    copy_bytes = seen[0]["frames"] * page_size     # the eventual checkpoint
    temp_bytes = max(0, (product.logical or 0) - wal_bytes)
    assert wal_bytes + copy_bytes + temp_bytes <= 40 * KiB * GROUP_ROWS
    conn.close()


#: Injected below the 6,000-row group's working set (about 7,900 distinct
#: pages, 31 MiB), so the fallback's cache actually spills (spec G3s, Q8;
#: `dc4` T3's NEED: injecting the row threshold alone does not prove the
#: fallback's costs). Half the working set, as the production 256 MiB cap is
#: to a group of twice the threshold. A cap of 64 pages (1/123 of the working
#: set) rewrote each page 6.7 times and broke the 40 KiB-per-row bound
#: (281 MB for 6,000 rows): that bound assumes a cap of the same order as the
#: working set, which R-cov (vi) measures at production scale.
_SPILLING_CACHE_KIB = 16 * 1024


def test_the_fallback_actually_spills_and_stays_within_its_bound(
        tmp_path, monkeypatch):
    """Q5 with both injections: some page reaches the WAL more than once (the
    validity guard), the statement journal uses a temp file, the injected
    cache cap holds, and the whole operation stays within 40 KiB per deleted
    row and within its charge."""
    ns, conn, retention, db_path, _copies = _build(tmp_path, monkeypatch,
                                                   "claude")
    monkeypatch.setattr(retention, "DELETION_FALLBACK_ROW_THRESHOLD", 100)
    monkeypatch.setattr(retention, "DELETION_CACHE_SIZE_KIB",
                        _SPILLING_CACHE_KIB)
    seen = []
    assert _instrument(monkeypatch, retention, seen)
    ops = []
    with _Pinned(db_path) as product:
        retention._maybe_prune_conversation_retention(
            conn, now_utc=NOW, retention_days=180, force=True,
            record_phase=lambda kind, payload: ops.append(payload))
        _page_size, frames = fx.wal_frames(db_path)
    [op] = ops
    assert op["mode"] == "fallback"
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    distinct = len({pgno for _o, pgno, _c in frames})
    written = seen[0]["frames"]
    measurement = {"written": written, "distinct": distinct,
                   "logical": product.logical, "rows": op["rows"],
                   "charge": op["charged_bytes"]}
    assert written > distinct, ("validity: some page repeats", measurement)
    assert (seen[0]["temp_store"], seen[0]["cache_size"]) == (
        1, -_SPILLING_CACHE_KIB), ("the file temp store, the cap held",
                                   measurement)
    wal_bytes = written * (page_size + 24) + fx.WAL_HEADER_BYTES
    temp_bytes = max(0, (product.logical or 0) - wal_bytes)
    if product.logical is not None:
        assert temp_bytes > TEMP_SLACK, (
            "validity: the statement journal reaches a temp file", measurement)
    total = wal_bytes + distinct * page_size + temp_bytes
    assert total <= 40 * KiB * op["rows"], measurement
    assert total <= op["charged_bytes"], measurement
    assert op["charged_bytes"] == retention.recompute_charge(
        {"phase": "delete", **op})
    conn.close()


def test_q8_a_scattered_overflow_group_is_bounded_per_page(
        tmp_path, monkeypatch):
    """G3s second fixture (Q8): a small, overflow-heavy Codex conversation
    over a fragmented file. Its spill-free deletion writes no page twice, its
    pointer-map pages are at most M_cap, and its other pages cost at most
    512 KiB + 12 KiB per deleted row; the validity guard requires its
    pointer-map pages to outnumber its other pages."""
    import _cctally_core

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    fx.precreate_store(_cctally_core.CONVERSATIONS_DB_PATH,
                       fx.SCATTER_PAGE_SIZE)
    conn = ns["open_conversations_db"](attach_cache=False)
    retention = importlib.import_module("_lib_conversation_retention")
    importlib.import_module("_lib_write_io").reset_for_tests()
    geometry = fx.build_scattered_codex_group(
        conn, ts="2025-08-01T12:00:00.000Z",
        keeper_ts="2026-07-17T08:00:00.000Z")
    db_path = pathlib.Path(retention._resolve_main_db_path(conn))
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    seen = []
    real_open = retention._open_deletion_connection

    class Spy:
        def __init__(self, inner):
            self._conn = inner

        def commit(self):
            result = self._conn.commit()
            if not seen:
                seen.append({"frames": fx.cache_writes(self._conn),
                             "wal": fx.wal_frames(db_path)[1]})
            return result

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(retention, "_open_deletion_connection",
                        lambda path: Spy(real_open(path)))
    ops = []
    with _Pinned(db_path):
        retention._maybe_prune_conversation_retention(
            conn, now_utc=NOW, retention_days=180, force=True,
            record_phase=lambda kind, payload: ops.append(payload))
    [op] = [p for p in ops if p["outcome"] == "ok"]
    rows = op["rows"]
    pages = [pgno for _o, pgno, _c in seen[0]["wal"]]
    ptrmap, other = fx.classify(pages, page_size, page_size)
    per_map = page_size // 5 + 1
    growth = -(-(512 * KiB + 12 * KiB * rows) // page_size)
    m_cap = -(-(page_count + growth) // per_map) + 1
    measurement = {"rows": rows, "M": len(ptrmap), "D": len(other),
                   "frames": seen[0]["frames"], "m_cap": m_cap}
    assert rows == geometry["events"], measurement
    assert len(ptrmap) > len(other), (
        "validity: pointer-map pages outnumber the others", measurement)
    assert seen[0]["frames"] == len(set(pages)), (
        "no page written twice", measurement)
    assert len(ptrmap) <= m_cap, measurement
    assert (2 * page_size + 24) * len(other) <= 512 * KiB + 12 * KiB * rows, \
        measurement
    assert op["pointer_map_cap"] == m_cap
    conn.close()
