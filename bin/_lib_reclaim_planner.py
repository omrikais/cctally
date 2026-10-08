"""#901 Q9: the reclaim chunk planner and its WAL-snapshot read helper.

A reclaim chunk (spec §5.4) is one `BEGIN IMMEDIATE` transaction on the
reclaim connection: plan, then the planned `PRAGMA incremental_vacuum(1)`
steps, then the frozen reservation's state write, then `COMMIT`. This module
computes the plan: the pages the chunk's steps may write, bounded BEFORE the
chunk mutates anything, from the original page images.

The bound (`dc5`, `dc6`, audited against SQLite `btree.c`, `pager.c` and
`wal.c` at 3.37.2, 3.53.4 and 3.54.0, where `finalDbSize`,
`sqlite3BtreeIncrVacuum`, `allocateBtreePage`, `setChildPtrmaps`,
`ptrmapPutOvflPtr`, `ptrmapPageno`, `btreeGetUnusedPage` and
`autoVacuumCommit` are identical and `incrVacuumStep` / `modifyPagePointer`
differ only by corruption checks):

* Each step removes the file's last page, skipping pointer-map and
  pending-byte pages, exactly as `incrVacuumStep` does.
* ID (identified pages) starts as {page 1}. A free tail page adds 2 to UNID
  (its trunk, or a promoted trunk and its predecessor), except that until the
  chunk's first trunk removal or relocation a free LEAF adds its own trunk to
  ID (the hybrid refinement, `dc6` F2).
* A relocated page (overflow or b-tree) adds 4 to UNID (up to two freelist
  pages, the destination and the destination's pointer-map page), adds its
  pointer-map parent to ID when the parent lies at or below the chunk's new
  end (else 1 to UNID), and for each child, overflow-chain head or next
  overflow page x adds x's pointer-map page to ID when x lies at or below the
  new end (else 1 to UNID). The destination is allocated at or below the
  full-vacuum end (`finalDbSize`), which the preconditions keep below every
  tail page; a commit drops every page past its new end (`pagerWalFrames`).
* The original images suffice: an earlier step changes pointer values but
  never a tail page's type, cell count or overflow-ness, and every reference
  whose identity an earlier step can change lies past the new end, counted in
  UNID (`dc6` F1).

ID and UNID are recomputed for every candidate prefix against that prefix's
own new end (`dc6` F3). The chunk grows one step at a time, up to 16 steps,
while the whole reservation (|ID| + UNID + 1) x (2P + 24) + F_r stays at most
the budget (4 MiB); a single step over the budget runs alone.

The read path (`WalSnapshotReader`, `dc6` F4) runs ONLY in a fresh helper
subprocess (`python3 _lib_reclaim_planner.py --plan`, JSON in on stdin,
JSON out on stdout), never in the dashboard process: closing a raw
descriptor of `conversations.db` or its `-shm` there would release the POSIX
locks SQLite holds on them. It validates both wal-index header copies, scans
and checksums the WAL frames through exactly `mxFrame` (ignoring any frame
past it, even a checksum-valid commit), reads each page from its latest frame
at or below `mxFrame` and otherwise from the main file, which a concurrent
PASSIVE checkpoint never changes for such a page, and rechecks the header and
the file identities before returning the plan. Every read counts against a
byte and time budget inside the 2 s secondary deadline.

Any inspection failure is a `ReclaimPlanRefused(reason)` with a typed reason;
the caller rolls back with no vacuum and no charge. Stdlib only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
import struct
import sys
import time

PLANNER_VERSION = 1
#: The SQLite releases whose incremental-vacuum sources were audited (spec
#: §1.4, G3q). A runtime outside the range refuses reclaim until G3q passes on
#: that version and a source review widens it.
AUDITED_SQLITE_MIN = (3, 37, 2)
AUDITED_SQLITE_MAX = (3, 54, 0)

WAL_INDEX_VERSION = 3007000
WAL_FORMAT_VERSION = 3007000
WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683
WAL_HEADER_BYTES = 32
FRAME_HEADER_BYTES = 24
WAL_INDEX_HEADER_BYTES = 48
PENDING_BYTE = 0x40000000

PTRMAP_ROOTPAGE = 1
PTRMAP_FREEPAGE = 2
PTRMAP_OVERFLOW1 = 3
PTRMAP_OVERFLOW2 = 4
PTRMAP_BTREE = 5

#: Every typed refusal the planner raises (the caller adds the connection,
#: record and helper reasons of spec §5.4 "Refusal").
REFUSAL_REASONS = (
    "wal_index_unreadable",     # missing, inconsistent or unsupported wal-index
    "wal_checksum_mismatch",    # a WAL header or frame checksum or salt
    "wal_endpoint_mismatch",    # the WAL's committed end disagrees with the index
    "read_budget_exhausted",    # the byte or time budget ran out
    "unsupported_geometry",     # auto-vacuum, reserved bytes, free pages, root page
    "malformed_page",           # an unexpected page type or a malformed cell
    "cyclic_freelist",          # a trunk chain that loops or miscounts
    "identity_changed",         # the snapshot or a file changed under the plan
    "sqlite_unaudited",         # a runtime outside the audited range
)

_U32 = 0xFFFFFFFF


class ReclaimPlanRefused(Exception):
    """The planner could not bound the chunk; carries a typed reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def sqlite_audited(version_info) -> bool:
    return AUDITED_SQLITE_MIN <= tuple(version_info)[:3] <= AUDITED_SQLITE_MAX


# ── pure geometry (mirrors btree.c) ───────────────────────────────────────

def pending_byte_page(page_size: int) -> int:
    return PENDING_BYTE // int(page_size) + 1


def ptrmap_pageno(page: int, usable_size: int, page_size: int) -> int:
    """`ptrmapPageno`: the pointer-map page holding `page`'s entry."""
    if page < 2:
        return 0
    per = usable_size // 5 + 1
    ret = ((page - 2) // per) * per + 2
    if ret == pending_byte_page(page_size):
        ret += 1
    return ret


def is_ptrmap(page: int, usable_size: int, page_size: int) -> bool:
    return page >= 2 and ptrmap_pageno(page, usable_size, page_size) == page


def final_db_size(n_orig: int, n_free: int, usable_size: int,
                  page_size: int) -> int:
    """`finalDbSize`: the file's end after a full incremental vacuum."""
    entries = usable_size // 5
    n_ptrmap = ((n_free - n_orig + ptrmap_pageno(n_orig, usable_size,
                                                  page_size) + entries)
                // entries)
    fin = n_orig - n_free - n_ptrmap
    pending = pending_byte_page(page_size)
    if n_orig > pending and fin < pending:
        fin -= 1
    while is_ptrmap(fin, usable_size, page_size) or fin == pending:
        fin -= 1
    return fin


@dataclasses.dataclass(frozen=True)
class TailStep:
    """One `incremental_vacuum(1)` step: the page it moves (None when the
    file's last page is a pointer-map or pending-byte page) and the file's
    end after it."""
    page: "int | None"
    n_fin: int
    new_end: int


def tail_steps(page_count: int, freelist_count: int, max_steps: int, *,
               usable_size: int, page_size: int) -> "list[TailStep]":
    """The steps `incrVacuumStep` takes from the file's end, one page per
    step, skipping pointer-map and pending-byte pages; stops when the
    freelist would be empty (SQLITE_DONE)."""
    pending = pending_byte_page(page_size)
    last, free, out = int(page_count), int(freelist_count), []
    for _ in range(int(max_steps)):
        if free <= 0 or last <= 1:
            break
        if free >= last:
            raise ReclaimPlanRefused("unsupported_geometry",
                                     "freelist count >= page count")
        n_fin = final_db_size(last, free, usable_size, page_size)
        if last > n_fin and not (is_ptrmap(last, usable_size, page_size)
                                 or last == pending):
            page = last
            free -= 1
        elif last <= n_fin:
            break
        else:
            page = None
        last -= 1
        while last == pending or is_ptrmap(last, usable_size, page_size):
            last -= 1
        out.append(TailStep(page, n_fin, last))
    return out


def _varint(image: bytes, offset: int) -> "tuple[int, int]":
    value = 0
    for k in range(8):
        byte = image[offset + k]
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset + k + 1
    return (value << 8) | image[offset + 8], offset + 9


def btree_refs(image: bytes, usable_size: int, page_count: int):
    """`(kind, children, overflow_heads)` of a non-page-1 b-tree page image,
    by the file format's local-payload rule. Raises `malformed_page` on an
    unknown page type or a cell outside the page."""
    try:
        kind = image[0]
        if kind not in (2, 5, 10, 13):
            raise ReclaimPlanRefused("malformed_page", f"page type {kind}")
        interior = kind in (2, 5)
        header = 12 if interior else 8
        ncell = struct.unpack(">H", image[3:5])[0]
        if header + 2 * ncell > usable_size:
            raise ReclaimPlanRefused("malformed_page", "cell count")
        min_local = (usable_size - 12) * 32 // 255 - 23
        max_local = (usable_size - 35 if kind == 13
                     else (usable_size - 12) * 64 // 255 - 23)
        children, heads = [], []
        for i in range(ncell):
            ptr = struct.unpack(">H", image[header + 2 * i:header + 2 * i + 2])[0]
            if not header + 2 * ncell <= ptr < usable_size:
                raise ReclaimPlanRefused("malformed_page", "cell pointer")
            cursor = ptr
            if interior:
                children.append(struct.unpack(">I", image[cursor:cursor + 4])[0])
                cursor += 4
            if kind == 5:
                continue
            payload, cursor = _varint(image, cursor)
            if kind == 13:
                _rowid, cursor = _varint(image, cursor)
            if payload > max_local:
                local = min_local + (payload - min_local) % (usable_size - 4)
                if local > max_local:
                    local = min_local
                end = cursor + local
                if end + 4 > usable_size:
                    raise ReclaimPlanRefused("malformed_page", "cell overflow")
                heads.append(struct.unpack(">I", image[end:end + 4])[0])
            elif cursor + payload > usable_size:
                raise ReclaimPlanRefused("malformed_page", "cell payload")
        if interior:
            children.append(struct.unpack(">I", image[8:12])[0])
    except (IndexError, struct.error) as exc:
        raise ReclaimPlanRefused("malformed_page", "truncated cell") from exc
    for ref in children + heads:
        if not 2 <= ref <= page_count:
            raise ReclaimPlanRefused("malformed_page", f"reference {ref}")
    return kind, children, heads


# ── the plan ──────────────────────────────────────────────────────────────

def page_frame_bytes(page_size: int) -> int:
    return 2 * int(page_size) + 24


def reservation_bytes(identified: int, unidentified: int, page_size: int,
                      fixed_bytes: int) -> int:
    """(|ID| + UNID + 1) x (2P + 24) + F_r: the 1 is the state write's one
    page; F_r covers the WAL header and framing (spec §5.4)."""
    return ((int(identified) + int(unidentified) + 1)
            * page_frame_bytes(page_size) + int(fixed_bytes))


@dataclasses.dataclass(frozen=True)
class ReclaimPlan:
    steps: int
    identified: "tuple[int, ...]"
    unidentified: int
    new_end: int
    page_size: int
    usable_size: int
    page_count: int
    freelist_count: int
    fixed_bytes: int
    reservation_bytes: int
    kinds: "dict"
    digest: str
    inspected_bytes: int = 0
    inspection_ms: int = 0
    reader_status: str = "ok"
    detail: "tuple" = ()

    def as_wire(self) -> dict:
        wire = dataclasses.asdict(self)
        wire["identified"] = list(self.identified)
        wire["detail"] = [dict(d) for d in self.detail]
        return wire

    @classmethod
    def from_wire(cls, wire: dict) -> "ReclaimPlan":
        if not isinstance(wire, dict):
            raise ValueError("plan")
        ints = ("steps", "unidentified", "new_end", "page_size",
                "usable_size", "page_count", "freelist_count", "fixed_bytes",
                "reservation_bytes", "inspected_bytes", "inspection_ms")
        for key in ints:
            value = wire.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(key)
        identified = wire.get("identified")
        if (not isinstance(identified, list)
                or not all(isinstance(p, int) and not isinstance(p, bool)
                           and p >= 1 for p in identified)):
            raise ValueError("identified")
        kinds = wire.get("kinds")
        if (not isinstance(kinds, dict)
                or set(kinds) != {"free", "overflow", "leaf", "interior"}
                or not all(isinstance(v, int) and v >= 0
                           for v in kinds.values())):
            raise ValueError("kinds")
        if not isinstance(wire.get("digest"), str):
            raise ValueError("digest")
        plan = cls(**{key: wire[key] for key in ints},
                   identified=tuple(identified), kinds=dict(kinds),
                   digest=wire["digest"][:64],
                   reader_status=str(wire.get("reader_status") or "ok")[:40],
                   detail=tuple(wire.get("detail") or ()))
        if plan.reservation_bytes != reservation_bytes(
                len(plan.identified), plan.unidentified, plan.page_size,
                plan.fixed_bytes) or plan.steps < 1:
            raise ValueError("reservation")
        return plan


def _ptrmap_entry(read_page, page: int, usable_size: int, page_size: int):
    map_page = ptrmap_pageno(page, usable_size, page_size)
    image = read_page(map_page)
    offset = 5 * (page - map_page - 1)
    if offset < 0 or offset + 5 > usable_size:
        raise ReclaimPlanRefused("malformed_page", "pointer-map offset")
    return image[offset], struct.unpack(">I", image[offset + 1:offset + 5])[0]


def scan_freelist(read_page, first_trunk: int, freelist_count: int,
                  page_count: int, usable_size: int, tail: "set[int]"):
    """The hybrid refinement's freelist map, keeping only the tail pages'
    entries (`dc6` F2): `({leaf: trunk}, {trunk: (predecessor, k)})`. The
    whole chain is walked to verify it: a loop, a page outside the file or a
    count that disagrees with page 1 refuses as `cyclic_freelist`."""
    leaf_of, trunks = {}, {}
    seen, counted = set(), 0
    trunk, previous = first_trunk, 1
    max_leaves = usable_size // 4 - 2
    while trunk:
        if trunk in seen or not 2 <= trunk <= page_count:
            raise ReclaimPlanRefused("cyclic_freelist", f"trunk {trunk}")
        seen.add(trunk)
        image = read_page(trunk)
        following, count = struct.unpack(">II", image[:8])
        if count > max_leaves:
            raise ReclaimPlanRefused("cyclic_freelist", "trunk leaf count")
        counted += 1 + count
        if counted > freelist_count:
            raise ReclaimPlanRefused("cyclic_freelist", "count overflow")
        if trunk in tail:
            trunks[trunk] = (previous, count)
        for i in range(count):
            leaf = struct.unpack(">I", image[8 + 4 * i:12 + 4 * i])[0]
            if not 2 <= leaf <= page_count:
                raise ReclaimPlanRefused("cyclic_freelist", f"leaf {leaf}")
            if leaf in tail:
                leaf_of[leaf] = trunk
        previous, trunk = trunk, following
    if counted != freelist_count:
        raise ReclaimPlanRefused("cyclic_freelist",
                                 f"{counted} pages on the chain, "
                                 f"{freelist_count} counted")
    return leaf_of, trunks


def plan_chunk(read_page, *, page_size: int, max_steps: int,
               budget_bytes: int, fixed_bytes: int) -> ReclaimPlan:
    """Plan one chunk from page images (`read_page(pgno) -> bytes`) of one
    consistent snapshot. Pure apart from `read_page`."""
    page1 = read_page(1)
    if page1[:16] != b"SQLite format 3\x00":
        raise ReclaimPlanRefused("malformed_page", "page 1 header")
    header_size = struct.unpack(">H", page1[16:18])[0]
    header_size = 65536 if header_size == 1 else header_size
    if header_size != page_size:
        raise ReclaimPlanRefused("unsupported_geometry", "page size")
    if page1[20] != 0:
        raise ReclaimPlanRefused("unsupported_geometry", "reserved bytes")
    usable_size = page_size - page1[20]
    largest_root = struct.unpack(">I", page1[52:56])[0]
    incremental = struct.unpack(">I", page1[64:68])[0]
    if largest_root == 0 or incremental != 1:
        raise ReclaimPlanRefused("unsupported_geometry",
                                 "auto_vacuum is not INCREMENTAL")
    page_count = struct.unpack(">I", page1[28:32])[0]
    first_trunk = struct.unpack(">I", page1[32:36])[0]
    freelist_count = struct.unpack(">I", page1[36:40])[0]
    steps = tail_steps(page_count, freelist_count, max_steps,
                       usable_size=usable_size, page_size=page_size)
    if not steps:
        raise ReclaimPlanRefused("unsupported_geometry",
                                 "no free page below the file's end")
    tail = {s.page for s in steps if s.page is not None}
    entries = {}
    for page in sorted(tail, reverse=True):
        kind, parent = _ptrmap_entry(read_page, page, usable_size, page_size)
        if kind not in (PTRMAP_ROOTPAGE, PTRMAP_FREEPAGE, PTRMAP_OVERFLOW1,
                        PTRMAP_OVERFLOW2, PTRMAP_BTREE):
            raise ReclaimPlanRefused("malformed_page", f"pointer-map type {kind}")
        if kind != PTRMAP_FREEPAGE and kind != PTRMAP_ROOTPAGE and not (
                1 <= parent <= page_count):
            raise ReclaimPlanRefused("malformed_page", "pointer-map parent")
        entries[page] = (kind, parent)
    leaf_of, trunks = scan_freelist(read_page, first_trunk, freelist_count,
                                    page_count, usable_size, tail)
    refs = {}
    for page, (kind, _parent) in entries.items():
        if kind == PTRMAP_FREEPAGE:
            if page not in leaf_of and page not in trunks:
                raise ReclaimPlanRefused("malformed_page",
                                         f"free page {page} not on the freelist")
        elif kind in (PTRMAP_OVERFLOW1, PTRMAP_OVERFLOW2):
            following = struct.unpack(">I", read_page(page)[:4])[0]
            if following and not 2 <= following <= page_count:
                raise ReclaimPlanRefused("malformed_page", "overflow link")
            refs[page] = ("overflow", [following] if following else [], [])
        elif kind == PTRMAP_BTREE:
            btype, children, heads = btree_refs(read_page(page), usable_size,
                                                page_count)
            refs[page] = ("interior" if btype in (2, 5) else "leaf",
                          children, heads)
    best = None
    for k in range(1, len(steps) + 1):
        prefix = steps[:k]
        new_end = prefix[-1].new_end
        roots = [s for s in prefix if s.page is not None
                 and entries[s.page][0] == PTRMAP_ROOTPAGE]
        below = all(s.n_fin <= new_end for s in prefix)
        if roots or not below:
            if k == 1:
                raise ReclaimPlanRefused(
                    "unsupported_geometry",
                    "root page in the tail" if roots
                    else "the full-vacuum end reaches the tail")
            break
        candidate = _prefix_plan(prefix, new_end, entries, refs, leaf_of,
                                 trunks, usable_size, page_size)
        identified, unidentified, kinds, detail = candidate
        reservation = reservation_bytes(len(identified), unidentified,
                                        page_size, fixed_bytes)
        if k > 1 and reservation > budget_bytes:
            break
        best = (k, new_end, identified, unidentified, kinds, detail,
                reservation)
    k, new_end, identified, unidentified, kinds, detail, reservation = best
    digest = hashlib.sha256(json.dumps({
        "v": PLANNER_VERSION, "pageCount": page_count,
        "freelist": freelist_count, "newEnd": new_end,
        "steps": [[s.page, *(entries.get(s.page) or (0, 0))]
                  for s in steps[:k]],
        "id": sorted(identified), "unid": unidentified,
    }, separators=(",", ":")).encode()).hexdigest()[:16]
    return ReclaimPlan(
        steps=k, identified=tuple(sorted(identified)),
        unidentified=unidentified, new_end=new_end, page_size=page_size,
        usable_size=usable_size, page_count=page_count,
        freelist_count=freelist_count, fixed_bytes=int(fixed_bytes),
        reservation_bytes=reservation, kinds=kinds, digest=digest,
        detail=tuple(detail))


def _prefix_plan(prefix, new_end, entries, refs, leaf_of, trunks,
                 usable_size, page_size):
    identified, unidentified = {1}, 0
    kinds = {"free": 0, "overflow": 0, "leaf": 0, "interior": 0}
    detail = []
    changed = False

    def reference(page):
        nonlocal unidentified
        if page <= new_end:
            identified.add(ptrmap_pageno(page, usable_size, page_size))
        else:
            unidentified += 1

    for step in prefix:
        if step.page is None:
            detail.append({"page": None})
            continue
        kind, parent = entries[step.page]
        if kind == PTRMAP_FREEPAGE:
            kinds["free"] += 1
            if not changed and step.page in leaf_of:
                identified.add(leaf_of[step.page])
                detail.append({"page": step.page, "kind": "free_leaf",
                               "trunk": leaf_of[step.page]})
            else:
                unidentified += 2
                if step.page in trunks:
                    previous, count = trunks[step.page]
                    detail.append({"page": step.page, "kind": "free_trunk",
                                   "leaves": count,
                                   "predecessor": previous != 1})
                else:
                    detail.append({"page": step.page, "kind": "free_leaf",
                                   "trunk": None})
                changed = changed or step.page in trunks
            continue
        changed = True
        unidentified += 4
        if parent <= new_end:
            identified.add(parent)
        else:
            unidentified += 1
        label, children, heads = refs[step.page]
        kinds[label] += 1
        for page in children + heads:
            reference(page)
        regions = {ptrmap_pageno(p, usable_size, page_size)
                   for p in children + heads}
        detail.append({"page": step.page, "kind": label,
                       "children": len(children), "heads": len(heads),
                       "regions": len(regions),
                       "parentInTail": parent > new_end})
    return identified, unidentified, kinds, detail


# ── the snapshot reader (helper subprocess only) ──────────────────────────

def _checksum(data: bytes, s0: int, s1: int, big_endian: bool):
    words = struct.unpack((">" if big_endian else "<") + "%dI" % (len(data) // 4),
                          data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & _U32
        s1 = (s1 + words[i + 1] + s0) & _U32
    return s0, s1


def _identity(st) -> tuple:
    return (st.st_dev, st.st_ino)


@dataclasses.dataclass(frozen=True)
class WalIndexHeader:
    change: int
    page_size: int
    max_frame: int
    page_count: int
    frame_checksum: "tuple[int, int]"
    salt: bytes
    big_endian_checksum: bool
    raw: bytes


def parse_wal_index_header(raw: bytes, *, byteorder: str = sys.byteorder
                           ) -> WalIndexHeader:
    """Validate the two 48-byte wal-index header copies at the start of the
    `-shm` (native byte order, as SQLite maps it): equal, initialized, the
    supported version, and their checksum (`walIndexTryHdr`)."""
    if len(raw) < 2 * WAL_INDEX_HEADER_BYTES:
        raise ReclaimPlanRefused("wal_index_unreadable", "short -shm")
    first, second = raw[:48], raw[48:96]
    if first != second:
        raise ReclaimPlanRefused("wal_index_unreadable", "header copies differ")
    order = "<" if byteorder == "little" else ">"
    version, _unused, change = struct.unpack(order + "III", first[:12])
    is_init, big_end = first[12], first[13]
    size = struct.unpack(order + "H", first[14:16])[0]
    max_frame, n_page = struct.unpack(order + "II", first[16:24])
    frame_ck = struct.unpack(order + "II", first[24:32])
    salt = first[32:40]
    stored = struct.unpack(order + "II", first[40:48])
    if not is_init or version != WAL_INDEX_VERSION:
        raise ReclaimPlanRefused("wal_index_unreadable", "uninitialized or version")
    if _checksum(first[:40], 0, 0, order == ">") != stored:
        raise ReclaimPlanRefused("wal_index_unreadable", "header checksum")
    page_size = (size & 0xFF00) | ((size & 0x0001) << 16)
    return WalIndexHeader(change, page_size, max_frame, n_page, frame_ck,
                          salt, bool(big_end), first)


class WalSnapshotReader:
    """Page images of the committed snapshot the wal-index publishes, read
    WAL-over-main through exactly `mxFrame`. Used only inside the helper."""

    def __init__(self, db_path, *, page_size: int, read_budget_bytes: int,
                 deadline: float, clock=time.monotonic, hooks=None):
        self.db_path = str(db_path)
        self.page_size = int(page_size)
        self.budget = int(read_budget_bytes)
        self.deadline = float(deadline)
        self.clock = clock
        self.hooks = hooks or {}
        self.read_bytes = 0
        self.frames = {}
        self.fds = {}
        self.identities = {}
        self.header = None
        self._pages = {}

    # ── budget ────────────────────────────────────────────────────────────
    def _charge(self, nbytes: int) -> None:
        self.read_bytes += nbytes
        if self.read_bytes > self.budget:
            raise ReclaimPlanRefused("read_budget_exhausted",
                                     f"{self.read_bytes} bytes")
        if self.clock() >= self.deadline:
            raise ReclaimPlanRefused("read_budget_exhausted", "deadline")

    def _pread(self, which: str, size: int, offset: int) -> bytes:
        self._charge(size)
        data = os.pread(self.fds[which], size, offset)
        if len(data) != size:
            raise ReclaimPlanRefused(
                "wal_endpoint_mismatch" if which == "wal" else "malformed_page",
                f"short read of {which} at {offset}")
        return data

    # ── lifecycle ─────────────────────────────────────────────────────────
    _SUFFIX = {"db": "", "wal": "-wal", "shm": "-shm"}

    def _open(self, which: str) -> None:
        path = self.db_path + self._SUFFIX[which]
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as exc:
            raise ReclaimPlanRefused("wal_index_unreadable",
                                     f"cannot open {which}") from exc
        self.fds[which] = fd
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ReclaimPlanRefused("identity_changed", f"{which} type")
        self.identities[which] = _identity(st)

    def open(self) -> "WalSnapshotReader":
        self._open("db")
        self._open("shm")
        self.header = parse_wal_index_header(
            self._pread("shm", 2 * WAL_INDEX_HEADER_BYTES, 0))
        # An index recovered from an empty WAL has learnt no page size (0).
        if self.header.max_frame and self.header.page_size != self.page_size:
            raise ReclaimPlanRefused("wal_index_unreadable", "page size")
        if self.header.max_frame:
            # An empty generation (mxFrame 0) reads the main file only and
            # never revives a stale frame; otherwise the WAL is required.
            self._open("wal")
            self._scan()
        if "after_scan" in self.hooks:
            self.hooks["after_scan"](self)
        return self

    def _scan(self) -> None:
        header = self._pread("wal", WAL_HEADER_BYTES, 0)
        magic, version, size = struct.unpack(">III", header[:12])
        if magic not in (WAL_MAGIC_LE, WAL_MAGIC_BE) or version != WAL_FORMAT_VERSION:
            raise ReclaimPlanRefused("wal_checksum_mismatch", "WAL header")
        if size != self.page_size:
            raise ReclaimPlanRefused("wal_checksum_mismatch", "WAL page size")
        big_endian = magic == WAL_MAGIC_BE
        checksum = _checksum(header[:24], 0, 0, big_endian)
        if checksum != struct.unpack(">II", header[24:32]):
            raise ReclaimPlanRefused("wal_checksum_mismatch", "WAL header checksum")
        salt = header[16:24]
        if salt != self.header.salt:
            raise ReclaimPlanRefused("wal_checksum_mismatch",
                                     "WAL salt differs from the wal-index")
        frame_size = FRAME_HEADER_BYTES + self.page_size
        wal_size = os.fstat(self.fds["wal"]).st_size
        if WAL_HEADER_BYTES + self.header.max_frame * frame_size > wal_size:
            raise ReclaimPlanRefused("wal_endpoint_mismatch", "WAL too short")
        frames, last_commit = {}, None
        for index in range(1, self.header.max_frame + 1):
            offset = WAL_HEADER_BYTES + (index - 1) * frame_size
            frame = self._pread("wal", frame_size, offset)
            if frame[8:16] != salt:
                raise ReclaimPlanRefused("wal_checksum_mismatch",
                                         f"frame {index} salt")
            checksum = _checksum(frame[:8], *checksum, big_endian)
            checksum = _checksum(frame[24:], *checksum, big_endian)
            if checksum != struct.unpack(">II", frame[16:24]):
                raise ReclaimPlanRefused("wal_checksum_mismatch",
                                         f"frame {index} checksum")
            pgno, commit = struct.unpack(">II", frame[:8])
            frames[pgno] = offset
            last_commit = commit
        if (not last_commit or last_commit != self.header.page_count
                or checksum != self.header.frame_checksum):
            raise ReclaimPlanRefused("wal_endpoint_mismatch",
                                     "the frame at mxFrame is not the index's commit")
        self.frames = frames

    def page(self, pgno: int) -> bytes:
        image = self._pages.get(pgno)
        if image is not None:
            return image
        if pgno < 1:
            raise ReclaimPlanRefused("malformed_page", f"page {pgno}")
        offset = self.frames.get(pgno)
        if offset is not None:
            image = self._pread("wal", self.page_size,
                                offset + FRAME_HEADER_BYTES)
        else:
            image = self._pread("db", self.page_size,
                                (pgno - 1) * self.page_size)
        self._pages[pgno] = image
        return image

    def recheck(self) -> None:
        """The snapshot and every file identity are unchanged."""
        raw = self._pread("shm", 2 * WAL_INDEX_HEADER_BYTES, 0)
        try:
            again = parse_wal_index_header(raw)
        except ReclaimPlanRefused as exc:
            raise ReclaimPlanRefused("identity_changed", exc.detail) from exc
        if (again.change, again.max_frame, again.page_count, again.salt) != (
                self.header.change, self.header.max_frame,
                self.header.page_count, self.header.salt):
            raise ReclaimPlanRefused("identity_changed", "wal-index generation")
        for which, fd in self.fds.items():
            path = self.db_path + self._SUFFIX[which]
            try:
                current = _identity(os.stat(path))
            except OSError as exc:
                raise ReclaimPlanRefused("identity_changed", which) from exc
            if (_identity(os.fstat(fd)) != self.identities[which]
                    or current != self.identities[which]):
                raise ReclaimPlanRefused("identity_changed", which)

    def close(self) -> None:
        for fd in self.fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = {}


def plan_from_store(request: dict, *, clock=time.monotonic,
                    hooks=None) -> dict:
    """The helper's whole job: read the snapshot, plan, recheck. Returns the
    JSON answer `{"ok": true, "plan": {...}}` or `{"ok": false, "reason"}`."""
    started = clock()
    deadline = started + max(0.0, float(request["deadlineMs"])) / 1000.0
    reader = WalSnapshotReader(
        request["db"], page_size=int(request["pageSize"]),
        read_budget_bytes=int(request["readBudgetBytes"]), deadline=deadline,
        clock=clock, hooks=hooks)
    try:
        reader.open()
        plan = plan_chunk(reader.page, page_size=int(request["pageSize"]),
                          max_steps=int(request["maxSteps"]),
                          budget_bytes=int(request["budgetBytes"]),
                          fixed_bytes=int(request["fixedBytes"]))
        if "before_recheck" in (hooks or {}):
            hooks["before_recheck"](reader)
        reader.recheck()
        if (plan.page_count, plan.freelist_count) != (
                int(request["pageCount"]), int(request["freelistCount"])):
            raise ReclaimPlanRefused(
                "wal_endpoint_mismatch",
                "the snapshot disagrees with the connection's geometry")
        if reader.header.max_frame and plan.page_count != reader.header.page_count:
            raise ReclaimPlanRefused("wal_endpoint_mismatch", "nPage")
        plan = dataclasses.replace(
            plan, inspected_bytes=reader.read_bytes,
            inspection_ms=int(round((clock() - started) * 1000)))
        return {"ok": True, "plan": plan.as_wire()}
    except ReclaimPlanRefused as exc:
        return {"ok": False, "reason": exc.reason, "detail": exc.detail[:200],
                "inspectedBytes": reader.read_bytes,
                "inspectionMs": int(round((clock() - started) * 1000))}
    finally:
        reader.close()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["--plan"]:
        print("usage: _lib_reclaim_planner.py --plan  (JSON request on stdin)",
              file=sys.stderr)
        return 2
    try:
        request = json.loads(sys.stdin.read())
        answer = plan_from_store(request)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"bad request: {type(exc).__name__}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(answer, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
