"""#901 spec §6.3 R-cov (i): recompute a reclaim chunk's plan INDEPENDENTLY of
the product, from the clone's own pages at the chunk's start.

`maintenance_op.py --pin` keeps two files: `db.start` (an APFS clone of the
database after the baseline checkpoint) and `wal.pinned` (the WAL every
operation appended to, copied before the pinning reader released it). The
snapshot at a chunk's start is `db.start` overlaid with the frames of
`wal.pinned` that precede the chunk's first byte. This module rebuilds that
snapshot and applies spec §5.4's rule - the same rule, written separately
from `bin/_lib_reclaim_planner.py` (it descends from the measured prototype
`reclaim_planner_probe.py`):

* ID starts as {page 1}; a free tail page adds 2 to UNID, except that a free
  leaf before the chunk's first trunk removal or relocation adds its own
  trunk to ID;
* a relocated page adds 4 to UNID, its pointer-map parent to ID when at or
  below the new end (else 1 to UNID), and for each child, overflow head or
  next overflow page x, x's pointer-map page to ID when x is at or below the
  new end (else 1 to UNID).

It also reports, per step, what the R-cov validity rules need: the relocated
page's kind, its children and overflow heads and the pointer-map regions they
span. Pure reads; stdlib only.
"""
from __future__ import annotations

import os
import struct

WAL_HEADER = 32
FRAME_HEADER = 24


class PinnedSnapshots:
    """Page images of `db.start` + the first N bytes of `wal.pinned`, for
    successive increasing N. Trunk images are cached by (page, frame) so a
    long run re-reads only the trunks a chunk changed."""

    def __init__(self, db_start, wal_pinned, page_size: int):
        self.page_size = page_size
        self.db = open(db_start, "rb")
        self.wal = open(wal_pinned, "rb") if os.path.exists(wal_pinned) else None
        self.frames = []          # (offset, pgno) of every valid frame
        if self.wal is not None:
            header = self.wal.read(WAL_HEADER)
            salt = header[16:24] if len(header) == WAL_HEADER else None
            offset = WAL_HEADER
            size = os.fstat(self.wal.fileno()).st_size
            while salt and offset + FRAME_HEADER + page_size <= size:
                self.wal.seek(offset)
                frame = self.wal.read(FRAME_HEADER)
                if frame[8:16] != salt:
                    break
                self.frames.append((offset, struct.unpack(">I", frame[:4])[0]))
                offset += FRAME_HEADER + page_size
        self.index = {}
        self.applied = 0
        self.limit = 0

    def advance(self, wal_offset: int) -> None:
        """The snapshot just before `wal_offset` (monotonic)."""
        while (self.applied < len(self.frames)
               and self.frames[self.applied][0] < wal_offset):
            offset, pgno = self.frames[self.applied]
            self.index[pgno] = offset
            self.applied += 1
        self.limit = wal_offset

    def page(self, pgno: int) -> bytes:
        offset = self.index.get(pgno)
        if offset is not None:
            self.wal.seek(offset + FRAME_HEADER)
            return self.wal.read(self.page_size)
        self.db.seek((pgno - 1) * self.page_size)
        return self.db.read(self.page_size)

    def version(self, pgno: int):
        return self.index.get(pgno)

    def frames_between(self, start: int, end: int) -> "list[int]":
        return [pgno for offset, pgno in self.frames if start <= offset < end]

    def close(self) -> None:
        self.db.close()
        if self.wal is not None:
            self.wal.close()


def ptrmap_page(page: int, usable: int, page_size: int) -> int:
    if page < 2:
        return 0
    per = usable // 5 + 1
    pending = 0x40000000 // page_size + 1
    base = ((page - 2) // per) * per + 2
    return base + 1 if base == pending else base


def _varint(b, i):
    v = 0
    for k in range(8):
        c = b[i + k]
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            return v, i + k + 1
    return (v << 8) | b[i + 8], i + 9


def _refs(image: bytes, usable: int):
    kind = image[0]
    ncell = struct.unpack(">H", image[3:5])[0]
    interior = kind in (2, 5)
    hl = 12 if interior else 8
    min_local = (usable - 12) * 32 // 255 - 23
    max_local = usable - 35 if kind == 13 else (usable - 12) * 64 // 255 - 23
    children, heads = [], []
    for i in range(ncell):
        j = struct.unpack(">H", image[hl + 2 * i:hl + 2 * i + 2])[0]
        if interior:
            children.append(struct.unpack(">I", image[j:j + 4])[0])
            j += 4
        if kind == 5:
            continue
        n, j = _varint(image, j)
        if kind == 13:
            _, j = _varint(image, j)
        if n > max_local:
            local = min_local + (n - min_local) % (usable - 4)
            if local > max_local:
                local = min_local
            heads.append(struct.unpack(">I", image[j + local:j + local + 4])[0])
    if interior:
        children.append(struct.unpack(">I", image[8:12])[0])
    return kind, children, heads


class FreelistCache:
    """Each trunk's (next, leaves) keyed by its current frame version."""

    def __init__(self):
        self.trunks = {}

    def map(self, snap: PinnedSnapshots, tail: set):
        p1 = snap.page(1)
        trunk = struct.unpack(">I", p1[32:36])[0]
        leaf_of, trunks, previous, seen = {}, {}, 1, set()
        while trunk and trunk not in seen:
            seen.add(trunk)
            key = (trunk, snap.version(trunk))
            cached = self.trunks.get(key)
            if cached is None:
                image = snap.page(trunk)
                following, count = struct.unpack(">II", image[:8])
                cached = (following,
                          struct.unpack(">%dI" % count, image[8:8 + 4 * count]))
                self.trunks[key] = cached
            following, leaves = cached
            if trunk in tail:
                trunks[trunk] = (previous, len(leaves))
            for leaf in leaves:
                if leaf in tail:
                    leaf_of[leaf] = trunk
            previous, trunk = trunk, following
        return leaf_of, trunks


def recompute(snap: PinnedSnapshots, steps: int, freelists: FreelistCache):
    """The plan of a chunk of `steps` steps from the current snapshot."""
    page_size = snap.page_size
    p1 = snap.page(1)
    usable = page_size - p1[20]
    npage = struct.unpack(">I", p1[28:32])[0]
    pending = 0x40000000 // page_size + 1

    def skip(page):
        return page == pending or ptrmap_page(page, usable, page_size) == page

    last, tail_steps = npage, []
    for _ in range(steps):
        if not skip(last):
            image = snap.page(ptrmap_page(last, usable, page_size))
            off = 5 * (last - ptrmap_page(last, usable, page_size) - 1)
            tail_steps.append((last, image[off], struct.unpack(
                ">I", image[off + 1:off + 5])[0]))
        last -= 1
        while skip(last):
            last -= 1
    new_end = last
    tail = {p for p, _t, _parent in tail_steps}
    leaf_of, trunks = freelists.map(snap, tail)
    ident, unid, changed = {1}, 0, False
    detail = []

    def ref(x):
        nonlocal unid
        if x <= new_end:
            ident.add(ptrmap_page(x, usable, page_size))
        else:
            unid += 1

    for page, kind, parent in tail_steps:
        if kind == 2:
            if not changed and page in leaf_of:
                ident.add(leaf_of[page])
                detail.append({"page": page, "kind": "free_leaf"})
            else:
                unid += 2
                if page in trunks:
                    changed = True
                detail.append({"page": page, "kind": "free_trunk"
                               if page in trunks else "free_leaf"})
            continue
        changed = True
        unid += 4
        if parent <= new_end:
            ident.add(parent)
        else:
            unid += 1
        image = snap.page(page)
        if kind in (3, 4):
            following = struct.unpack(">I", image[:4])[0]
            if following:
                ref(following)
            detail.append({"page": page, "kind": "overflow",
                           "nonTerminal": bool(following)})
        else:
            btype, children, heads = _refs(image, usable)
            for x in children + heads:
                ref(x)
            regions = {ptrmap_page(x, usable, page_size)
                       for x in children + heads}
            detail.append({"page": page,
                           "kind": "interior" if btype in (2, 5) else "leaf",
                           "children": len(children), "heads": len(heads),
                           "regions": len(regions)})
    return {"identified": sorted(ident), "unidentified": unid,
            "newEnd": new_end, "pageCount": npage, "usable": usable,
            "detail": detail}
