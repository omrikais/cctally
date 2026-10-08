"""#901 Q8/Q9 shared fixtures for the retention write-bound tests.

Pure helpers that read a store's WAL and classify its frames, plus the
builder of revision 6's G3s/G3v second fixture: a small, overflow-heavy Codex
conversation whose freed overflow pages lie scattered over many pointer-map
regions of a file with free pages, so a spill-free deletion writes more
pointer-map pages than any other kind (§1.4, Q8).

The fixture uses 1,024-byte pages so a pointer-map region is 205 pages and a
few dozen MiB span a hundred regions; at the product's 4 KiB pages the same
mechanism needs gigabytes. The reservation and the bounds are computed from
the store's own page size, so the smaller page exercises the formula, not a
special case.
"""
from __future__ import annotations

import ctypes
import os
import pathlib
import sqlite3
import struct

SQLITE_DBSTATUS_CACHE_WRITE = 9
WAL_HEADER_BYTES = 32
FRAME_HEADER_BYTES = 24

#: Revision 6's second fixture: eight events of ~14 KiB, each overflow chain
#: allocated one page per region from a freelist that holds exactly one free
#: page in each of `SCATTER_REGIONS` regions.
SCATTER_PAGE_SIZE = 1024
SCATTER_EVENTS = 8
SCATTER_PAYLOAD_BYTES = 14_000
SCATTER_REGIONS = 128


def cache_writes(conn) -> int:
    """`SQLITE_DBSTATUS_CACHE_WRITE` for this connection: every page it wrote
    to the WAL, spills and in-place rewrites included."""
    import _sqlite3

    real = conn
    while not isinstance(real, sqlite3.Connection):
        real = real._conn
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


def wal_frames(db_path):
    """`(page_size, [(offset, pgno, commit)])` for the valid frames of the
    current WAL generation (header salt, in order)."""
    wal = pathlib.Path(f"{db_path}-wal")
    if not wal.exists():
        return None, []
    data = wal.read_bytes()
    if len(data) < WAL_HEADER_BYTES:
        return None, []
    page_size = int.from_bytes(data[8:12], "big")
    salt = data[16:24]
    out = []
    offset = WAL_HEADER_BYTES
    while offset + FRAME_HEADER_BYTES + page_size <= len(data):
        header = data[offset:offset + FRAME_HEADER_BYTES]
        if header[8:16] != salt:
            break
        out.append((offset, int.from_bytes(header[0:4], "big"),
                    int.from_bytes(header[4:8], "big")))
        offset += FRAME_HEADER_BYTES + page_size
    return page_size, out


def ptrmap_pageno(page: int, usable_size: int, page_size: int) -> int:
    """SQLite's `ptrmapPageno`, pending-byte exception included."""
    if page < 2:
        return 0
    per = usable_size // 5 + 1
    pending = 0x40000000 // page_size + 1
    ret = ((page - 2) // per) * per + 2
    return ret + 1 if ret == pending else ret


def is_ptrmap(page: int, usable_size: int, page_size: int) -> bool:
    return page >= 2 and ptrmap_pageno(page, usable_size, page_size) == page


def classify(pages, usable_size: int, page_size: int):
    """Split distinct page numbers into (pointer-map pages, other pages)."""
    distinct = set(pages)
    ptrmap = {p for p in distinct if is_ptrmap(p, usable_size, page_size)}
    return ptrmap, distinct - ptrmap


def logical_write_bytes():
    """Bytes this process passed to write(): macOS `ri_logical_writes`,
    Linux `/proc/self/io` `wchar`; None elsewhere."""
    import sys

    if sys.platform == "darwin":
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


def precreate_store(db_path, page_size: int) -> None:
    """An empty conversations store with the given page size, incremental
    auto-vacuum and WAL, before the product opener applies its schema."""
    db_path = pathlib.Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"PRAGMA page_size={int(page_size)}")
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE q8_seed(x)")
        conn.execute("DROP TABLE q8_seed")
        conn.commit()
    finally:
        conn.close()


def build_scattered_codex_group(conn, *, key="q8-expire", ts, keeper_ts,
                                events=SCATTER_EVENTS,
                                payload_bytes=SCATTER_PAYLOAD_BYTES,
                                regions=SCATTER_REGIONS) -> dict:
    """Seed `conn` (a conversations store with `SCATTER_PAGE_SIZE` pages) with
    a filler spanning `regions` pointer-map regions, free exactly one page per
    region, then insert the expiring conversation `key`: each event's overflow
    chain takes one free page per region. A keeper conversation survives.

    Returns the geometry the tests judge against."""
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    assert page_size == SCATTER_PAGE_SIZE, page_size
    per_region = page_size // 5 + 1
    conn.execute("CREATE TABLE IF NOT EXISTS q8_filler(payload BLOB)")
    filler_rows = regions * per_region
    conn.executemany("INSERT INTO q8_filler(payload) VALUES (?)",
                     ((os.urandom(page_size - 140),)
                      for _ in range(filler_rows)))
    conn.commit()
    conn.executemany("DELETE FROM q8_filler WHERE rowid = ?",
                     ((rowid,) for rowid in range(per_region // 2,
                                                   filler_rows, per_region)))
    conn.commit()
    free_before = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    offset = 0
    for i in range(events):
        offset += 1
        payload = '{"t":"' + os.urandom(payload_bytes // 2).hex() + '"}'
        conn.execute(
            "INSERT INTO codex_conversation_events (source_path, line_offset, "
            "source_root_key, conversation_key, timestamp_utc, payload_json) "
            "VALUES (?,?,?,?,?,?)",
            (f"/{key}.jsonl", offset, "root-a", key, ts, payload))
    conn.execute(
        "INSERT INTO codex_conversation_events (source_path, line_offset, "
        "source_root_key, conversation_key, timestamp_utc, payload_json) "
        "VALUES (?,?,?,?,?,?)",
        ("/q8-keeper.jsonl", 1, "root-a", "q8-keeper", keeper_ts, "{}"))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"page_size": page_size, "regions": regions,
            "free_before_insert": free_before,
            "free_after_insert": int(
                conn.execute("PRAGMA freelist_count").fetchone()[0]),
            "page_count": int(conn.execute("PRAGMA page_count").fetchone()[0]),
            "events": events, "key": key}


def read_frame_pages(db_path, frames):
    """Page numbers of the given `(offset, pgno, commit)` frames."""
    return [pgno for _offset, pgno, _commit in frames]


def header_page_count(db_path) -> int:
    with open(db_path, "rb") as fh:
        return struct.unpack(">I", fh.read(100)[28:32])[0]


# ── Q9 reclaim fixtures: fragmented files that force relocations ──────────

def page_count(conn) -> int:
    return int(conn.execute("PRAGMA page_count").fetchone()[0])


def build_interior_tail(conn, *, gap_pages=20, max_rows=4000) -> dict:
    """A table whose root interior page deepens LAST, so the file's final
    pages are a new leaf and two interior pages whose ~450 children are
    spread over the whole file (one leaf every `gap_pages` + 1 pages, so the
    children span many pointer-map regions); then the gap rows are deleted,
    leaving a large freelist below them.

    One row per leaf (a ~3.8 KiB payload at 4 KiB pages). The root leaf's
    deepening grows the file by two pages and is not counted; the root
    INTERIOR page's deepening - an append's new leaf plus the root's copy and
    its split sibling, three pages - moves the full root (~500 children)
    into two new interior pages at the end of the file."""
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    conn.execute("CREATE TABLE g3v_interior(id INTEGER PRIMARY KEY, pad BLOB)")
    conn.execute("CREATE TABLE g3v_gap(pad BLOB)")
    deepenings = 0
    rows = 0
    while deepenings < 1:
        if rows >= max_rows:
            raise AssertionError("the interior fixture's root never deepened")
        conn.executemany("INSERT INTO g3v_gap(pad) VALUES (?)",
                         ((os.urandom(page_size - 200),)
                          for _ in range(gap_pages)))
        before = page_count(conn)
        conn.execute("INSERT INTO g3v_interior(id, pad) VALUES (?, ?)",
                     (rows, os.urandom(page_size - 300)))
        rows += 1
        if page_count(conn) - before >= 3:
            deepenings += 1
    conn.commit()
    tail_page_count = page_count(conn)
    conn.execute("DELETE FROM g3v_gap")
    conn.commit()
    return {"rows": rows, "page_count": tail_page_count,
            "freelist": int(conn.execute("PRAGMA freelist_count").fetchone()[0])}


def build_overflow_tail(conn, *, rows=160, payload_bytes=9000, gap_pages=8,
                        normalize=None) -> dict:
    """Rows whose payload spills into a two-page overflow chain (the first
    page non-terminal), interleaved with gap pages, plus an index b-tree, so
    the tail holds table leaves with overflow heads, first and second
    overflow pages, index pages and free pages. `normalize(conn)` runs last
    while the freelist is still empty, so the #780 record's own leaf is
    allocated at the end of the file (the first chunk relocates it).

    The gaps are freed top first: the first page freed becomes a trunk near
    the end of the file, and once it holds a full page of leaves a later
    freed page becomes the new first trunk, so the tail also holds a trunk
    with leaves and a predecessor."""
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    conn.execute("CREATE TABLE g3v_overflow(id INTEGER PRIMARY KEY, "
                 "tag TEXT, pad BLOB)")
    conn.execute("CREATE INDEX g3v_overflow_tag ON g3v_overflow(tag)")
    conn.execute("CREATE TABLE g3v_gap2(pad BLOB)")
    for i in range(rows):
        conn.executemany("INSERT INTO g3v_gap2(pad) VALUES (?)",
                         ((os.urandom(page_size - 200),)
                          for _ in range(gap_pages)))
        conn.execute("INSERT INTO g3v_overflow(id, tag, pad) VALUES (?, ?, ?)",
                     (i, os.urandom(40).hex(), os.urandom(payload_bytes)))
    conn.commit()
    if normalize is not None:
        normalize(conn)
    top = int(conn.execute("SELECT MAX(rowid) FROM g3v_gap2").fetchone()[0])
    conn.execute("DELETE FROM g3v_gap2 WHERE rowid > ?", (top - 30,))
    conn.commit()
    conn.execute("DELETE FROM g3v_gap2")
    conn.commit()
    return {"page_count": page_count(conn),
            "freelist": int(conn.execute("PRAGMA freelist_count").fetchone()[0])}


def frames_between(db_path, start_offset: int):
    """`(new_offset, [pgno, ...])`: the frames of the current generation
    appended at or after `start_offset`."""
    _page_size, frames = wal_frames(db_path)
    pages = [pgno for offset, pgno, _c in frames if offset >= start_offset]
    end = frames[-1][0] + FRAME_HEADER_BYTES + _page_size if frames else WAL_HEADER_BYTES
    return end, pages
