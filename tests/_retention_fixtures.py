"""#901 Q8/Q9 shared fixtures for the retention write-bound tests.

Pure helpers that read a store's WAL and classify its frames, plus the
builder of revision 6's G3s/G3v second fixture: a small, overflow-heavy Codex
conversation whose freed overflow pages lie scattered over many pointer-map
regions of a file with free pages, so a spill-free deletion writes more
pointer-map pages than any other kind (§1.4, Q8). That holds only while freed
pages are not overwritten, so #940 revision 7k makes `secure_delete` an
explicit, checked setting of every Q8 builder and deletion connection, and
adds a clustered negative fixture with an independent, file-read measure of
its overflow pages' pointer-map regions.

The fixture uses 1,024-byte pages so a pointer-map region is 205 pages and a
few dozen MiB span a hundred regions; at the product's 4 KiB pages the same
mechanism needs gigabytes. The reservation and the bounds are computed from
the store's own page size, so the smaller page exercises the formula, not a
special case.

#965 adds a second, independent section at the end of this module: a healthy
isolated store with expired Claude and Codex transcripts, the copied install
layouts and the hang bounds the test-retention-default tests share.
"""
from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import datetime as dt
import json
import os
import pathlib
import shutil
import sqlite3
import struct

import _sqlite_write_accounting as acct

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
#: Revision 7k's negative fixture: the same overflow workload allocated
#: contiguously, so the group's overflow pages lie in at most this many
#: pointer-map regions (`overflow_regions`, which counts only the chains that
#: trace back to the events table's root). Derived, not measured: each of the
#: `SCATTER_EVENTS` (8) payloads of about `SCATTER_PAYLOAD_BYTES` (14,000)
#: spills into a chain of about 14 overflow pages of 1,020 bytes at
#: `SCATTER_PAGE_SIZE` (1,024), so the eight chains, allocated contiguously at
#: the end of the file with the few table and index pages their inserts add,
#: occupy roughly 120 consecutive pages. That is fewer than one region's
#: `page_size // 5 + 1` (205) pages, and a run of consecutive pages shorter
#: than a region crosses at most one region boundary, so the group's own
#: chains span at most two regions. Other tables' chains (the schema's in
#: region 0, a `cache_meta` row's) are not counted (#940 F1 fix round 3, R3-2).
CLUSTER_MAX_REGIONS = 2
#: SQLite's pointer-map entry types for an overflow page (first, later).
PTRMAP_OVERFLOW_TYPES = (3, 4)


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


# ── #940 revision 7j: direct, file-attributed write accounting ───────────
#
# The temp-byte checks read `tests/_sqlite_write_accounting.py`'s checked
# intervals instead of subtracting WAL frames and another interval's
# baseline from the process's whole logical write counter (spec §6.7). The
# accounting is activated by each consumer module's fixture, never on
# import, and independently in every subprocess writer.

TESTS_DIR = pathlib.Path(__file__).resolve().parent
measure = acct.measure
is_attributed = acct.is_attributed
require_attributed = acct.require_attributed


@contextlib.contextmanager
def write_accounting():
    """The wrapper is the default VFS for the fixture's lifetime: activated
    before the fixture's builders open a connection (per process, so per
    xdist worker), and the previous default restored after the fixture's
    own connections close. Fails, never skips, when unavailable."""
    acct.activate()
    try:
        yield acct
    finally:
        acct.deactivate()


def child_writes(stdout, label, *, stderr=None):
    """`(record, writes)` from a subprocess writer's published evidence:
    missing or malformed evidence, an invalid measurement or a bypassing
    connection fails. Missing or malformed evidence carries the child's
    `stderr`, when given (#940 F1 review #5)."""
    try:
        record = acct.read_evidence(stdout, label)
    except acct.MeasurementInvalid as exc:
        if stderr is None:
            raise
        raise acct.MeasurementInvalid(
            "%s; child stderr: %s" % (exc, stderr.strip() or "(empty)")
        ) from None
    return record, acct.child_writes(record, label)


# ── #940 revision 7k: the Q8 fixtures' `secure_delete` precondition ───────

def require_secure_delete(conn, expected: int, label: str) -> dict:
    '''Every schema of `conn` (`PRAGMA database_list`) reports `expected`
    as its effective `secure_delete`; returns {schema: value}.'''
    values = {}
    for _seq, schema, _file in conn.execute("PRAGMA database_list").fetchall():
        values[schema] = int(conn.execute(
            f'PRAGMA "{schema}".secure_delete').fetchone()[0])
    assert values and all(v == expected for v in values.values()), (
        f"precondition: {label} has secure_delete={values}, "
        f"expected {expected}")
    return values


def set_secure_delete(conn, value: int, label: str) -> dict:
    conn.execute(f"PRAGMA secure_delete={int(value)}")
    return require_secure_delete(conn, value, label)


def precreate_store(db_path, page_size: int, *, secure_delete: int) -> dict:
    """An empty conversations store with the given page size, incremental
    auto-vacuum and WAL, before the product opener applies its schema. Its
    own connection runs at `secure_delete`, set and checked before it frees a
    page; returns that connection's checked values."""
    db_path = pathlib.Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        values = set_secure_delete(conn, secure_delete,
                                   "precreate_store connection")
        conn.execute(f"PRAGMA page_size={int(page_size)}")
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE q8_seed(x)")
        conn.execute("DROP TABLE q8_seed")
        conn.commit()
    finally:
        conn.close()
    return values


def build_scattered_codex_group(conn, *, key="q8-expire", ts, keeper_ts,
                                events=SCATTER_EVENTS,
                                payload_bytes=SCATTER_PAYLOAD_BYTES,
                                regions=SCATTER_REGIONS) -> dict:
    """Seed `conn` (a conversations store with `SCATTER_PAGE_SIZE` pages) with
    a filler spanning `regions` pointer-map regions, free exactly one page per
    region, then insert the expiring conversation `key`: each event's overflow
    chain takes one free page per region. A keeper conversation survives.

    Returns the geometry the tests judge against."""
    return _build_codex_group(conn, key=key, ts=ts, keeper_ts=keeper_ts,
                              events=events, payload_bytes=payload_bytes,
                              regions=regions, scatter=True)


def build_clustered_codex_group(conn, *, key="q8-expire", ts, keeper_ts,
                                events=SCATTER_EVENTS,
                                payload_bytes=SCATTER_PAYLOAD_BYTES,
                                regions=SCATTER_REGIONS) -> dict:
    """Revision 7k's negative fixture: `build_scattered_codex_group` without
    the per-region freeing, so the freelist holds no scattered page and each
    event's overflow chain is allocated contiguously at the end of the file.
    The filler, the events, the payloads and the keeper are the same."""
    return _build_codex_group(conn, key=key, ts=ts, keeper_ts=keeper_ts,
                              events=events, payload_bytes=payload_bytes,
                              regions=regions, scatter=False)


def _build_codex_group(conn, *, key, ts, keeper_ts, events, payload_bytes,
                       regions, scatter) -> dict:
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    assert page_size == SCATTER_PAGE_SIZE, page_size
    per_region = page_size // 5 + 1
    conn.execute("CREATE TABLE IF NOT EXISTS q8_filler(payload BLOB)")
    filler_rows = regions * per_region
    conn.executemany("INSERT INTO q8_filler(payload) VALUES (?)",
                     ((os.urandom(page_size - 140),)
                      for _ in range(filler_rows)))
    conn.commit()
    if scatter:
        conn.executemany("DELETE FROM q8_filler WHERE rowid = ?",
                         ((rowid,) for rowid in range(per_region // 2,
                                                       filler_rows,
                                                       per_region)))
        conn.commit()
    free_before = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    if not scatter:
        # #940 F2-c: the premise of `CLUSTER_MAX_REGIONS`'s derivation. With
        # no free page to reuse, every overflow chain is allocated
        # contiguously at the end of the file.
        assert free_before == 0, (
            "clustered: the freelist must be empty before the insert", free_before)
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


def _table_root(db_path, table: str) -> int:
    """`table`'s root page from `sqlite_schema`, read on a connection of its
    own (closed before returning)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT rootpage FROM sqlite_schema WHERE type = 'table' "
            "AND name = ?", (table,)).fetchone()
    finally:
        conn.close()
    assert row is not None, ("overflow_regions: no such table", table)
    return int(row[0])


def overflow_regions(db_path, page_size: int, *,
                     table: str = "codex_conversation_events") -> int:
    """The number of distinct pointer-map regions holding an overflow page of
    a chain that belongs to `table`, parsed from the file's own bytes; the
    product is not used. Read after a TRUNCATE checkpoint, so the WAL must be
    empty and the file is the whole store.

    Ownership follows the pointer map's parent entries (#940 F1 fix round 3,
    R3-2): a later overflow page (type 4) points at the page before it, so the
    walk reaches the chain's first page (type 3); that page's parent is the
    b-tree page holding the cell, and b-tree pages (type 5) point at their
    parents up to a root (type 1). The chain belongs to `table` when that root
    is the table's `rootpage` in `sqlite_schema`. Chains of other tables, of
    `sqlite_schema` itself (rooted at page 1, which has no map entry) and of
    indexes do not count.

    The map pages are located with this measure's own arithmetic rather than
    `is_ptrmap`/`ptrmap_pageno`, which `classify` uses, so the region count
    the negative fixture is judged by does not share the arithmetic of the
    page classification it is compared with. A region is `usable // 5 + 1`
    pages starting at page 2, its first page is its map page, and SQLite moves
    a map page that would land on the pending-byte page to the next page."""
    db_path = pathlib.Path(db_path)
    wal = pathlib.Path(f"{db_path}-wal")
    assert not wal.exists() or wal.stat().st_size == 0, (
        "overflow_regions reads the file after a TRUNCATE checkpoint",
        wal.stat().st_size)
    root = _table_root(db_path, table)
    data = db_path.read_bytes()
    assert int.from_bytes(data[16:18], "big") % 65536 == page_size % 65536, \
        "the file's page size is not the expected one"
    usable = page_size - data[20]
    entries = usable // 5
    per_region = entries + 1
    pending_page = 0x40000000 // page_size + 1
    pages = len(data) // page_size

    def map_page_of(page):
        first = ((page - 2) // per_region) * per_region + 2
        return first + 1 if first == pending_page else first

    def entry(page):
        """(type, parent) of `page`'s pointer-map entry."""
        offset = (map_page_of(page) - 1) * page_size + 5 * (
            page - map_page_of(page) - 1)
        return data[offset], int.from_bytes(data[offset + 1:offset + 5], "big")

    def owner_root(page):
        kind, parent = entry(page)
        for _ in range(pages):
            if kind != 4:
                break
            page = parent
            kind, parent = entry(page)
        assert kind == 3, ("overflow_regions: a chain without a first page",
                           page, kind)
        node = parent
        for _ in range(pages):
            if node == 1:
                return 1
            kind, parent = entry(node)
            if kind != 5:
                break
            node = parent
        assert kind == 1, ("overflow_regions: a b-tree page without a root",
                           node, kind)
        return node

    regions = set()
    for first in range(2, pages + 1, per_region):
        map_page = first + 1 if first == pending_page else first
        if map_page > pages:
            break
        base = (map_page - 1) * page_size
        for i in range(min(entries, pages - map_page)):
            if (data[base + 5 * i] in PTRMAP_OVERFLOW_TYPES
                    and owner_root(map_page + 1 + i) == root):
                regions.add(map_page)
                break
    return len(regions)


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


# --- #965: test environments default conversation retention off ----------
#
# Shared #965 fixtures: a healthy isolated store with expired transcripts.
#
# EXPIRED timestamps are older than 90 days for any wall clock after 2026-04-10,
# so they stay expired; the RECENT session is dated at call time. Stores are
# healthy (never corruption recovery), so the recovery marker of spec 5.6 cannot
# mask a prune. Layout copies are physical copies of bin/, never symlinks into
# the checkout and never hard links (a later recursive chmod of pytest scratch
# would follow a hard link into the repository).
#
# Non-vacuity: no keep or prune observation is made on a store that has not
# first been shown to hold expired Claude AND Codex transcripts (``seed`` or
# ``fork`` followed by ``assert_holds_expired``), so a launch that reads no
# Codex root cannot pass a prune control by observing nothing.

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"
SWITCH = "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF"
EXPIRED_TS = "2026-01-10T12:00:00.000Z"
EXPIRED_SESSION = "965-expired-claude"
RECENT_SESSION = "965-recent-claude"
CODEX_CORPUS = (REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1"
                / "rollouts" / "modern-full.jsonl")
_IGNORE = shutil.ignore_patterns("__pycache__", "cctally-*-test")
#: Hang bounds, all under pytest's 120 s per-test timeout (bin/cctally-test-all,
#: CCTALLY_PYTEST_TIMEOUT) with margin, module-fixture setup included: no node
#: makes more than two sequential cache-sync launches.
LAUNCH_TIMEOUT_S = 45
QUICK_TIMEOUT_S = 30
BUILDER_TIMEOUT_S = 100


def _user_line(session_id: str, uuid: str, ts: str) -> str:
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": session_id, "timestamp": ts,
        "message": {"role": "user", "content": f"hello {uuid}"},
    }) + "\n"


@dataclasses.dataclass(frozen=True)
class Staged:
    env: dict
    data: pathlib.Path


def stage(root: pathlib.Path) -> Staged:
    """An isolated HOME, Claude config, Codex home and data dir under ``root``.

    The returned environment is a COPY of this process's environment, so it
    carries whatever the switch is here (conftest assigns "1").

    The Codex tree lives at ``home/.codex``, the location cctally resolves when
    ``CODEX_HOME`` is unset (``_codex_home_roots``: ``Path.home() / ".codex"``),
    and ``CODEX_HOME`` names that same directory. bin/_lib-harness-env.sh runs
    ``unset CODEX_HOME`` and the preamble-sourcing Codex harnesses populate
    ``$HOME/.codex`` under their fake HOME (bin/cctally-explain-test), so a
    preamble launch (T5) and an explicit-CODEX_HOME launch (T2, T4, T6) read
    one tree.
    """
    home = root / "home"
    claude = home / ".claude"
    project = claude / "projects" / "-retention-965"
    project.mkdir(parents=True)
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    (project / f"{EXPIRED_SESSION}.jsonl").write_text(
        _user_line(EXPIRED_SESSION, "e-1", EXPIRED_TS)
        + _user_line(EXPIRED_SESSION, "e-2", EXPIRED_TS), encoding="utf-8")
    (project / f"{RECENT_SESSION}.jsonl").write_text(
        _user_line(RECENT_SESSION, "r-1", recent), encoding="utf-8")
    codex = home / ".codex"
    rollout = codex / "sessions" / "2024" / "01" / "10" / "rollout-965-expired.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        CODEX_CORPUS.read_text(encoding="utf-8").replace("2026-07-14", "2024-01-10"),
        encoding="utf-8")
    data = root / "data"
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude),
        "CODEX_HOME": str(codex),
        "CCTALLY_DATA_DIR": str(data),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "TZ": "Etc/UTC",
    })
    return Staged(env=env, data=data)


def write_retention(data: pathlib.Path, days) -> None:
    """Persist an explicit ``conversation.retention_days``; ``None`` removes the
    config file, so the next cctally run recreates it with no retention key."""
    data.mkdir(parents=True, exist_ok=True)
    path = data / "config.json"
    if days is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps({"conversation": {"retention_days": days}}) + "\n",
                    encoding="utf-8")


def counts(data: pathlib.Path) -> dict:
    conn = sqlite3.connect(str(data / "conversations.db"))
    try:
        def one(sql, *args):
            return conn.execute(sql, args).fetchone()[0]
        return {
            "expired_claude": one(
                "SELECT COUNT(*) FROM conversation_messages WHERE session_id=?",
                EXPIRED_SESSION),
            "recent_claude": one(
                "SELECT COUNT(*) FROM conversation_messages WHERE session_id=?",
                RECENT_SESSION),
            "expired_codex": one("SELECT COUNT(*) FROM codex_conversation_messages"),
        }
    finally:
        conn.close()


def assert_holds_expired(data: pathlib.Path, *, when: str) -> dict:
    """Non-vacuity: the store holds expired Claude AND Codex transcripts (and
    the recent Claude survivor) before a keep or prune observation is made."""
    got = counts(data)
    assert (got["expired_claude"] > 0 and got["expired_codex"] > 0
            and got["recent_claude"] > 0), (
        f"non-vacuity ({when}): the store must hold expired Claude and Codex "
        f"transcripts before any keep or prune observation: {got}")
    return got


def seed(data: pathlib.Path, launch) -> dict:
    """Ingest through ``launch`` with retention explicitly off, then prove the
    store holds expired transcripts. ``launch(*args)`` must run ``cctally
    cache-sync <args>`` through the observation's OWN launch path, so a path
    that reads no Codex root fails here instead of passing a prune control.
    An explicit 0 disables retention on every code version under every switch
    value, so the seed behaves the same on the baseline and the candidate."""
    write_retention(data, 0)
    launch("--source", "all")
    return assert_holds_expired(data, when="after seeding")


def fork(template: Staged, root: pathlib.Path) -> Staged:
    """A private copy of a seeded template's data directory under ``root``.

    The copy shares the template's read-only source tree (HOME, Claude config,
    Codex home), so its stored source paths stay valid; only
    ``CCTALLY_DATA_DIR`` moves. Copy only while no process holds the template
    (its seeding launch has exited), the established pattern of
    tests/_shared_corpus.py."""
    data = root / "data"
    shutil.copytree(template.data, data)
    env = dict(template.env)
    env["CCTALLY_DATA_DIR"] = str(data)
    return Staged(env=env, data=data)


def copy_layout(kind: str, root: pathlib.Path) -> pathlib.Path:
    """A physical copy of bin/ shaped as ``kind``; returns the path to launch.

    ``packaged``: a bare copy (no .git). ``npm``: the npm package layout; the
    launch is the command bin/cctally-npm-shim.js spawns (`python3
    <package>/bin/cctally`), because the pytest leg has no guaranteed node.
    ``brew``: a Cellar libexec copy reached through a bin/ symlink, as the
    formula installs it. ``checkout`` / ``worktree``: checkout-form layouts with
    a .git directory / a .git file; these stand for a checkout, not for a copied
    install (spec I8 governs copied installs, which stay production-like).
    """
    if kind == "packaged":
        shutil.copytree(BIN, root / "bin", ignore=_IGNORE)
        return root / "bin" / "cctally"
    if kind == "npm":
        package = root / "lib" / "node_modules" / "cctally"
        shutil.copytree(BIN, package / "bin", ignore=_IGNORE)
        return package / "bin" / "cctally"
    if kind == "brew":
        libexec = root / "Cellar" / "cctally" / "0.0.0" / "libexec"
        shutil.copytree(BIN, libexec / "bin", ignore=_IGNORE)
        link = root / "bin" / "cctally"
        link.parent.mkdir(parents=True)
        link.symlink_to(libexec / "bin" / "cctally")
        return link
    if kind == "checkout":
        shutil.copytree(BIN, root / "bin", ignore=_IGNORE)
        (root / ".git").mkdir()
        return root / "bin" / "cctally"
    if kind == "worktree":
        shutil.copytree(BIN, root / "bin", ignore=_IGNORE)
        (root / ".git").write_text("gitdir: /nonexistent/.git/worktrees/965\n",
                                   encoding="utf-8")
        return root / "bin" / "cctally"
    raise ValueError(f"unknown layout {kind!r}")
