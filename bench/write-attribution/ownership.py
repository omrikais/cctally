#!/usr/bin/env python3
"""Per-occurrence page ownership of a measured SQLite store (#939 spec 2.2).

Used in-process by `conversation_catchup.py run --ownership LOG`: one
`Tracker` follows every database it names, from files only (it never opens a
SQLite connection, so it can neither take a lock nor limit a checkpoint).

`observe()` runs between transactions (the driver calls it from each traced
connection's statement trace whenever that connection is not in a
transaction, after each phase, and once more at exit). Per store it reads the
wal-index header in `-shm` (both copies, which must agree: `mxFrame`, the
salts, `nBackfill`), and only when that header, the WAL's or the main file's
size or mtime changed since the last observation does it read the WAL frames
committed since then (`(old mxFrame, mxFrame]` in the same generation, else
`(0, mxFrame]`). It keeps the latest committed frame of every page in the
current generation, so a page's current image is that frame's data or, when
the page has none, the main file's page (a WAL restart requires a complete
backfill, so the main file then holds every earlier frame).

Ownership follows the pointer map (`auto_vacuum=INCREMENTAL` or FULL): every
page has a 5-byte entry (type, parent) on its pointer-map page, and the owner
is found by following parents to a root page (`PTRMAP_ROOTPAGE`), whose
`sqlite_schema` row names the object. A free page is `<free>`, a pointer-map
page `<ptrmap>`, page 1 `sqlite_schema`, the pending-byte page `<pending>`.
`sqlite_schema` is parsed from the page images (a table b-tree rooted at page
1, overflow chains followed) whenever the schema cookie changes.

The log (JSONL, one file for every store): a `seed` record per store
(`ptrmap`: whether the store has pointer maps; without them nothing more is
logged for it and `catchup_attrib.py` falls back to endpoint ownership), then
an `obs` record per changed observation: `db`, `t0`/`t1` (wall-clock seconds,
the interposer's clock, read start and end), `salt`, `mxFrame`, `nBackfill`,
`restart` (a new WAL generation), `walPresent`, `names` (object ids first
used here: id -> [name, type, tbl_name]) and `frames`, the committed frames
read: [frame, pgno, owner id], the owner resolved after the whole range was
applied (the page's owner after that commit). A final observation after the
driver closed its connections reports a removed or emptied WAL as
`walPresent: false`. The log's own bytes count in the process's physical
writes; `catchup_attrib.py` reports its size.
"""
from __future__ import annotations

import atexit
import json
import os
import struct
import time

WAL_HEADER = 32
FRAME_HEADER = 24
PENDING_BYTE = 0x40000000
PTRMAP_ROOTPAGE, PTRMAP_FREEPAGE, PTRMAP_OVERFLOW1, PTRMAP_OVERFLOW2, \
    PTRMAP_BTREE = 1, 2, 3, 4, 5
SCHEMA = "sqlite_schema"


# ── pure page arithmetic ──────────────────────────────────────────────────

def ptrmap_page(pgno: int, usable: int, page_size: int) -> int:
    """SQLite's ptrmapPageno: the pointer-map page holding `pgno`'s entry."""
    if pgno < 2:
        return 0
    per = usable // 5 + 1
    ret = ((pgno - 2) // per) * per + 2
    if ret == PENDING_BYTE // page_size + 1:
        ret += 1
    return ret


def _varint(buf: bytes, i: int) -> "tuple[int, int]":
    v = 0
    for k in range(8):
        b = buf[i + k]
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, i + k + 1
    return (v << 8) | buf[i + 8], i + 9


def _record(payload: bytes) -> list:
    """The values of one SQLite record (NULL, integers, text; blobs as bytes)."""
    hlen, i = _varint(payload, 0)
    types = []
    while i < hlen:
        t, i = _varint(payload, i)
        types.append(t)
    out, j = [], hlen
    widths = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}
    for t in types:
        if t == 0:
            out.append(None)
        elif t in widths:
            w = widths[t]
            out.append(int.from_bytes(payload[j:j + w], "big", signed=True))
            j += w
        elif t == 7:
            out.append(struct.unpack(">d", payload[j:j + 8])[0])
            j += 8
        elif t in (8, 9):
            out.append(t - 8)
        elif t >= 12:
            n = (t - 12) // 2
            raw = payload[j:j + n]
            out.append(raw.decode("utf-8", "replace") if t % 2 else raw)
            j += n
        else:
            out.append(None)
    return out


def parse_schema(page_image, page_size: int, usable: int) -> dict:
    """{rootpage: (name, type, tbl_name)} from the sqlite_schema table b-tree
    (page 1), read through `page_image(pgno) -> bytes`."""
    out, stack, seen = {}, [1], set()
    max_local = usable - 35
    min_local = ((usable - 12) * 32 // 255) - 23
    while stack:
        pgno = stack.pop()
        if pgno in seen or pgno < 1:
            continue
        seen.add(pgno)
        page = page_image(pgno)
        base = 100 if pgno == 1 else 0
        kind = page[base]
        ncells = int.from_bytes(page[base + 3:base + 5], "big")
        if kind == 0x05:
            ptrs = base + 12
            stack.append(int.from_bytes(page[base + 8:base + 12], "big"))
            for k in range(ncells):
                off = int.from_bytes(page[ptrs + 2 * k:ptrs + 2 * k + 2], "big")
                stack.append(int.from_bytes(page[off:off + 4], "big"))
            continue
        if kind != 0x0D:
            continue
        ptrs = base + 8
        for k in range(ncells):
            off = int.from_bytes(page[ptrs + 2 * k:ptrs + 2 * k + 2], "big")
            size, i = _varint(page, off)
            _rowid, i = _varint(page, i)
            if size <= max_local:
                payload = bytes(page[i:i + size])
            else:
                local = min_local + (size - min_local) % (usable - 4)
                if local > max_local:
                    local = min_local
                payload = bytearray(page[i:i + local])
                nxt = int.from_bytes(page[i + local:i + local + 4], "big")
                guard = 0
                while nxt and len(payload) < size and guard < 100000:
                    ov = page_image(nxt)
                    payload += ov[4:usable]
                    nxt = int.from_bytes(ov[:4], "big")
                    guard += 1
                payload = bytes(payload[:size])
            try:
                values = _record(payload)
            except (IndexError, struct.error):
                continue
            if len(values) >= 4 and isinstance(values[3], int) and values[3] > 0:
                out[values[3]] = (str(values[1]), str(values[0]),
                                  str(values[2]))
    return out


# ── one store ─────────────────────────────────────────────────────────────

class Store:
    def __init__(self, name: str, path: str):
        self.name, self.path = name, path
        self.wal, self.shm = path + "-wal", path + "-shm"
        self.page_size = self.usable = None
        self.ptrmap = None
        self.salt = None
        self.mx = 0
        self.latest = {}          # pgno -> latest committed frame (this generation)
        self.cache = {}           # pgno -> image (ptrmap and schema pages)
        self.cookie = None
        self.schema = {}
        self.signature = None
        self.names = {}           # owner name -> id
        self.kinds = {}           # owner name -> (type, tbl_name)

    # file access
    def _read(self, path, off, n) -> bytes:
        try:
            with open(path, "rb") as fh:
                fh.seek(off)
                return fh.read(n)
        except OSError:
            return b""

    def _stat(self, path):
        try:
            st = os.stat(path)
            return (st.st_size, st.st_mtime_ns, st.st_ino)
        except OSError:
            return None

    def wal_index(self):
        """(mxFrame, salt, nBackfill) from -shm, or None (no consistent
        header: absent, uninitialized or mid-update)."""
        for _ in range(5):
            raw = self._read(self.shm, 0, 136)
            if len(raw) < 136:
                return None
            a, b = raw[0:48], raw[48:96]
            if a == b and a[12]:
                mx = struct.unpack_from("<I", a, 16)[0]
                # aSalt holds the WAL header's salt bytes as written (memcpy),
                # so it is read like a frame header's, big-endian.
                salt = list(struct.unpack_from(">II", a, 32))
                backfill = struct.unpack_from("<I", raw, 96)[0]
                return mx, salt, backfill
        return None

    def _geometry(self) -> bool:
        if self.page_size:
            return True
        head = self._read(self.path, 0, 100)
        if len(head) == 100 and head[:16] == b"SQLite format 3\x00":
            ps = int.from_bytes(head[16:18], "big")
            self.page_size = 65536 if ps == 1 else ps
            self.usable = self.page_size - head[20]
            self.ptrmap = int.from_bytes(head[52:56], "big") != 0
            return True
        whead = self._read(self.wal, 0, WAL_HEADER)
        if len(whead) == WAL_HEADER:
            self.page_size = int.from_bytes(whead[8:12], "big")
            self.usable = self.page_size     # refined once page 1 exists
            return True
        return False

    def image(self, pgno: int) -> bytes:
        frame = self.latest.get(pgno)
        if frame is not None:
            off = WAL_HEADER + (frame - 1) * (FRAME_HEADER + self.page_size) \
                + FRAME_HEADER
            data = self._read(self.wal, off, self.page_size)
        else:
            data = self._read(self.path, (pgno - 1) * self.page_size,
                              self.page_size)
        return data.ljust(self.page_size, b"\0")

    def _cached(self, pgno: int) -> bytes:
        img = self.cache.get(pgno)
        if img is None:
            img = self.cache[pgno] = self.image(pgno)
        return img

    def _refresh_header(self) -> None:
        page1 = self._cached(1)
        if page1[:16] == b"SQLite format 3\x00":
            self.usable = self.page_size - page1[20]
            if self.ptrmap is None or not self.ptrmap:
                self.ptrmap = int.from_bytes(page1[52:56], "big") != 0
            cookie = int.from_bytes(page1[40:44], "big")
            if cookie != self.cookie:
                self.cookie = cookie
                self.schema = parse_schema(self._cached, self.page_size,
                                           self.usable)

    def owner(self, pgno: int) -> str:
        if pgno == 1:
            return SCHEMA
        if pgno == PENDING_BYTE // self.page_size + 1:
            return "<pending>"
        page = pgno
        for _ in range(256):
            pm = ptrmap_page(page, self.usable, self.page_size)
            if pm == page:
                return "<ptrmap>"
            off = 5 * (page - pm - 1)
            entry = self._cached(pm)[off:off + 5]
            kind = entry[0] if entry else 0
            parent = int.from_bytes(entry[1:5], "big") if entry else 0
            if kind == PTRMAP_ROOTPAGE:
                found = self.schema.get(page)
                if found is None:
                    return f"<root {page}>"
                return found[0]
            if kind == PTRMAP_FREEPAGE:
                return "<free>"
            if kind in (PTRMAP_OVERFLOW1, PTRMAP_OVERFLOW2, PTRMAP_BTREE) \
                    and parent:
                if parent == 1:
                    return SCHEMA
                page = parent
                continue
            return "<unmapped>"
        return "<unmapped>"

    def _id(self, name: str, new: dict) -> int:
        oid = self.names.get(name)
        if oid is None:
            oid = self.names[name] = len(self.names)
            kind = next(((t, tbl) for n, t, tbl in self.schema.values()
                         if n == name), (None, None))
            new[str(oid)] = [name, kind[0], kind[1]]
        return oid

    def observe(self) -> "dict | None":
        """One observation, or None when nothing changed."""
        t0 = time.time()
        index = self.wal_index()
        signature = (index, self._stat(self.wal), self._stat(self.path))
        if signature == self.signature:
            return None
        self.signature = signature
        if not self._geometry():
            return None
        if self.ptrmap is False:
            return None
        wal_present = bool((self._stat(self.wal) or (0,))[0])
        if index is None or not wal_present:
            mx, salt, backfill = (0, self.salt, 0)
            restart = False
            frames_read = []
        else:
            mx, salt, backfill = index
            restart = salt != self.salt
            start = 0 if restart else self.mx
            if restart:
                self.latest.clear()
                self.cache.clear()     # the main file now holds every frame
            elif mx < self.mx:
                start = 0
            frames_read = []
            size = FRAME_HEADER + self.page_size
            for f in range(start + 1, mx + 1):
                head = self._read(self.wal, WAL_HEADER + (f - 1) * size,
                                  FRAME_HEADER)
                if len(head) < FRAME_HEADER:
                    break
                pgno = int.from_bytes(head[0:4], "big")
                if list(struct.unpack(">II", head[8:16])) != salt:
                    continue
                self.latest[pgno] = f
                self.cache.pop(pgno, None)
                frames_read.append((f, pgno))
        if not wal_present:
            self.latest.clear()
            self.cache.clear()
        self._refresh_header()
        new = {}
        frames = [[f, pgno, self._id(self.owner(pgno), new)]
                  for f, pgno in frames_read]
        self.salt, self.mx = salt, mx
        return {"kind": "obs", "db": self.name, "t0": round(t0, 6),
                "t1": round(time.time(), 6), "salt": salt, "mxFrame": mx,
                "nBackfill": backfill, "restart": restart,
                "walPresent": wal_present, "names": new, "frames": frames}


class Tracker:
    """Every named store, one log; `observe()` is cheap when nothing moved."""

    def __init__(self, stores: dict, log_path: str):
        self.stores = [Store(name, path) for name, path in stores.items()]
        self.log = open(log_path, "a", buffering=1 << 16)
        self.observations = 0
        for store in self.stores:
            self._seed(store, initial=True)
        atexit.register(self.close)

    def _seed(self, store: Store, *, initial: bool) -> None:
        """A store that exists when the tracker starts is seeded with its
        state before the run (`base`: its committed frames are not the
        run's); one the run creates is seeded at its first observation,
        which is an ordinary `obs`."""
        record = store.observe()
        store.seeded = bool(store.page_size and store.ptrmap is not None)
        if store.seeded:
            self._write({"kind": "seed", "db": store.name, "t": time.time(),
                         "ptrmap": bool(store.ptrmap),
                         "pageSize": store.page_size})
        if record is not None:
            if initial:
                record["kind"] = "base"
            else:
                self.observations += 1
            self._write(record)

    def _write(self, record: dict) -> None:
        if self.log is not None:
            self.log.write(json.dumps(record, separators=(",", ":")) + "\n")

    def observe(self) -> None:
        for store in self.stores:
            if not getattr(store, "seeded", False):
                self._seed(store, initial=False)
                continue
            record = store.observe()
            if record is not None:
                self.observations += 1
                self._write(record)

    def close(self) -> None:
        if self.log is None:
            return
        try:
            self.observe()
        finally:
            self.log.close()
            self.log = None


def observer(conn, tracker: Tracker, then=None):
    """A trace callback: observe whenever `conn` is not in a transaction (the
    statement starting is the first after a COMMIT or ROLLBACK, or an
    autocommit statement), then hand the statement on."""
    def callback(statement):
        if not conn.in_transaction:
            tracker.observe()
        if then is not None:
            then(statement)
    return callback


# ── reading the log (catchup_attrib.py) ───────────────────────────────────

def load(path: str) -> dict:
    """{db: {"ptrmap", "pageSize", "names": {id: [name, type, tbl]},
    "observations": [...], "bytes": log size}}."""
    out = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            store = out.setdefault(rec["db"], {"ptrmap": None, "pageSize": None,
                                               "names": {}, "observations": []})
            if rec["kind"] == "seed":
                store["ptrmap"], store["pageSize"] = rec["ptrmap"], rec["pageSize"]
                continue
            store["names"].update(rec.get("names") or {})
            if rec["kind"] in ("obs", "base"):
                store["observations"].append(rec)
    size = os.path.getsize(path)
    for store in out.values():
        store["observations"].sort(key=lambda r: (r["kind"] != "base", r["t0"]))
        store["bytes"] = size
    return out
