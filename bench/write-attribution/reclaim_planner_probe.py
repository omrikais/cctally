"""#901 SR-016 prototype: a precommitted per-chunk dirty-page bound for the
product's reclaim chunk (16 x `PRAGMA incremental_vacuum(1)` in one
transaction), computed from page images read WAL-over-main inside the chunk's
BEGIN IMMEDIATE, checked against the pages the chunk actually wrote.

Bound derivation (SQLite 3.37.2 / 3.53.4 / master btree.c, audited):
- every step: page 1 (sqlite3BtreeIncrVacuum + allocateBtreePage).
- free tail page (BTALLOC_EXACT): <= 2 freelist pages besides the page itself
  (leaf case: its trunk; trunk case: new trunk + previous trunk or page 1);
  the page itself is past the new end and dropped by pagerWalFrames.
- in-use tail page (BTALLOC_LE, nearby = nFin of a FULL vacuum, so the
  destination is never a tail page): <= 2 freelist pages, the destination,
  its pointer-map page, the parent (ptrmap parent field), and the pointer-map
  pages of its children and overflow heads (b-tree) or of its next overflow
  page (overflow).
Identified pages (exact page numbers) are deduplicated; anything whose
identity can change inside the chunk counts as one unidentified page.

Variant `simple`: every free step = 2 unidentified.
Variant `hybrid`: free steps before any structural change use the
freelist map (leaf -> its trunk, identified); afterwards 2 unidentified.

usage: reclaim_planner_probe.py SRC_DB WORKDIR TREE K
  WORKDIR must not exist; it receives a clone of SRC_DB (`cp -c`), which is
  removed at the end, and the receipt planner.json. Run it on a scratch volume.
"""
import json, os, sqlite3, struct, subprocess, sys

src, work, tree, K = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
assert not os.path.exists(work), work
sys.path.insert(0, os.path.join(tree, "bin"))
import _lib_conversation_retention as R

os.makedirs(work)
db = os.path.join(work, "conversations.db")
subprocess.check_call(["cp", "-c", src, db])
for side in ("-wal", "-shm"):
    if os.path.exists(src + side):
        subprocess.check_call(["cp", "-c", src + side, db + side])
c = sqlite3.connect(db)
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.close()

# Raw descriptors stay open for the whole run (closing one would drop this
# process's POSIX locks); they are closed only after every connection.
reader = sqlite3.connect(db, isolation_level=None)
reader.execute("BEGIN")
reader.execute("SELECT count(*) FROM cache_meta").fetchone()
conn = R.open_reclaim_connection(db)
conn.isolation_level = None
fdb = os.open(db, os.O_RDONLY)
fwal = os.open(db + "-wal", os.O_RDONLY)

hdr = os.pread(fdb, 100, 0)
P = struct.unpack(">H", hdr[16:18])[0]
P = 65536 if P == 1 else P
U = P - hdr[20]
PER = U // 5 + 1
PENDING = 1073741824 // P + 1
FRAME = 24 + P

wal_index = {}      # pgno -> frame offset (committed)
wal_pos = [32]      # next frame offset to scan
wal_salt = [None]
wal_ck = [None]
wal_be = [True]


def _cksum(data, s0, s1, be):
    n = len(data) // 4
    words = struct.unpack((">" if be else "<") + "%dI" % n, data)
    for i in range(0, n, 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def wal_refresh():
    size = os.fstat(fwal).st_size
    if wal_salt[0] is None:
        if size < 32:
            return
        h = os.pread(fwal, 32, 0)
        magic = struct.unpack(">I", h[:4])[0]
        wal_be[0] = magic == 0x377F0683
        wal_salt[0] = h[16:24]
        wal_ck[0] = _cksum(h[:24], 0, 0, wal_be[0])
        assert wal_ck[0] == struct.unpack(">II", h[24:32]), "wal header checksum"
    pending, s = {}, wal_ck[0]
    off = wal_pos[0]
    while off + FRAME <= size:
        fr = os.pread(fwal, FRAME, off)
        pgno, commit = struct.unpack(">II", fr[:8])
        if fr[8:16] != wal_salt[0]:
            break
        s = _cksum(fr[:8], s[0], s[1], wal_be[0])
        s = _cksum(fr[24:], s[0], s[1], wal_be[0])
        if s != struct.unpack(">II", fr[16:24]):
            raise AssertionError("wal frame checksum at %d" % off)
        pending[pgno] = off
        off += FRAME
        if commit:
            wal_index.update(pending)
            pending = {}
            wal_pos[0] = off
            wal_ck[0] = s


def page(p):
    off = wal_index.get(p)
    if off is not None:
        return os.pread(fwal, P, off + 24)
    return os.pread(fdb, P, (p - 1) * P)


def ptrmap_pageno(p):
    if p < 2:
        return 0
    ret = ((p - 2) // PER) * PER + 2
    return ret + 1 if ret == PENDING else ret


def is_ptrmap(p):
    return p >= 2 and ptrmap_pageno(p) == p


def ptrmap_get(p):
    pm = ptrmap_pageno(p)
    d = page(pm)
    o = 5 * (p - pm - 1)
    return d[o], struct.unpack(">I", d[o + 1:o + 5])[0]


def varint(b, i):
    v = 0
    for k in range(8):
        c_ = b[i + k]
        v = (v << 7) | (c_ & 0x7F)
        if not c_ & 0x80:
            return v, i + k + 1
    return (v << 8) | b[i + 8], i + 9


def btree_refs(d):
    """(children, overflow_heads) of a non-page-1 b-tree page image."""
    t = d[0]
    ncell = struct.unpack(">H", d[3:5])[0]
    interior = t in (2, 5)
    hl = 12 if interior else 8
    children, heads = [], []
    min_local = (U - 12) * 32 // 255 - 23
    max_local = U - 35 if t == 13 else (U - 12) * 64 // 255 - 23
    for i in range(ncell):
        ptr = struct.unpack(">H", d[hl + 2 * i:hl + 2 * i + 2])[0]
        j = ptr
        if interior:
            children.append(struct.unpack(">I", d[j:j + 4])[0])
            j += 4
        if t == 5:
            continue  # table interior: child + rowid, no payload
        n, j = varint(d, j)
        if t == 13:
            _, j = varint(d, j)  # rowid
        if n > max_local:
            local = min_local + (n - min_local) % (U - 4)
            if local > max_local:
                local = min_local
            heads.append(struct.unpack(">I", d[j + local:j + local + 4])[0])
    if interior:
        children.append(struct.unpack(">I", d[8:12])[0])
    return t, children, heads


def freelist_map():
    p1 = page(1)
    trunk = struct.unpack(">I", p1[32:36])[0]
    leaf_of, trunks, prev = {}, {}, 1
    while trunk:
        d = page(trunk)
        nxt, k = struct.unpack(">II", d[:8])
        leaves = struct.unpack(">%dI" % k, d[8:8 + 4 * k])
        trunks[trunk] = (prev, leaves[0] if k else None)
        for lf in leaves:
            leaf_of[lf] = trunk
        prev, trunk = trunk, nxt
    return leaf_of, trunks


def plan(n, use_freelist):
    p1 = page(1)
    npage = struct.unpack(">I", p1[28:32])[0]
    last, steps = npage, []
    for _ in range(n):
        if not is_ptrmap(last) and last != PENDING:
            t, parent = ptrmap_get(last)
            steps.append((last, t, parent))
        last -= 1
        while last == PENDING or is_ptrmap(last):
            last -= 1
    new_end = last
    ident, unid, kinds = {1}, 0, {"free": 0, "overflow": 0, "leaf": 0, "interior": 0}
    leaf_of, trunks = freelist_map() if use_freelist else ({}, {})
    changed = not use_freelist
    maxrefs = 0

    def ref(x):
        nonlocal unid
        if x <= new_end:
            ident.add(ptrmap_pageno(x))
        else:
            unid += 1
    for (p, t, parent) in steps:
        if t == 2:  # PTRMAP_FREEPAGE
            kinds["free"] += 1
            if not changed and p in leaf_of:
                tr = leaf_of[p]
                if tr <= new_end:
                    ident.add(tr)
                else:
                    unid += 1
            elif not changed and p in trunks:
                pv, nt = trunks[p]
                for x in (pv, nt):
                    if x is None:
                        continue
                    if x <= new_end:
                        ident.add(x)
                    else:
                        unid += 1
                changed = True
            else:
                unid += 2
            continue
        if t == 1:
            raise SystemExit("root page in tail: refuse")
        changed = True
        unid += 4  # 2 freelist pages, destination, destination's ptrmap page
        if parent <= new_end:
            ident.add(parent)
        else:
            unid += 1
        d = page(p)
        if t in (3, 4):
            kinds["overflow"] += 1
            nxt = struct.unpack(">I", d[:4])[0]
            if nxt:
                ref(nxt)
        elif t == 5:
            bt, children, heads = btree_refs(d)
            kinds["interior" if bt in (2, 5) else "leaf"] += 1
            maxrefs = max(maxrefs, len(children) + len(heads))
            for x in children + heads:
                ref(x)
        else:
            raise SystemExit("unexpected ptrmap type %d" % t)
    return {"ident": ident, "unid": unid, "bound": len(ident) + unid, "kinds": kinds,
            "steps": len(steps), "newEnd": new_end, "npage": npage, "maxRefs": maxrefs}


chunks = []
for i in range(K):
    conn.execute("BEGIN IMMEDIATE")
    wal_refresh()
    ps = plan(16, False)
    ph = plan(16, True)
    R._run_incremental_vacuum_chunk(conn, 16)
    conn.execute("COMMIT")
    # the commit's frames: everything appended since the last refresh
    before = wal_pos[0]
    wal_refresh()
    written = set()
    off = before
    while off < wal_pos[0]:
        written.add(struct.unpack(">I", os.pread(fwal, 4, off))[0])
        off += FRAME
    rec = {"i": i, "written": len(written), "frames": (wal_pos[0] - before) // FRAME,
           "reclaimed": ps["npage"] - ph["newEnd"], "kinds": ph["kinds"], "maxRefs": ph["maxRefs"]}
    for name, pl in (("simple", ps), ("hybrid", ph)):
        outside = written - pl["ident"]
        rec[name] = {"bound": pl["bound"], "ident": len(pl["ident"]), "unid": pl["unid"],
                     "outsideIdent": len(outside),
                     "ok": len(outside) <= pl["unid"] and len(written) <= pl["bound"]}
    chunks.append(rec)

conn.close()
reader.execute("ROLLBACK")
reader.close()
os.close(fdb)
os.close(fwal)
for f in ("conversations.db", "conversations.db-wal", "conversations.db-shm"):
    try:
        os.remove(os.path.join(work, f))
    except FileNotFoundError:
        pass
B = 2 * P + 24
summ = {"chunks": K, "pageSize": P,
        "repeatedFrames": sum(x["frames"] - x["written"] for x in chunks),
        "writtenPagesTotal": sum(x["written"] for x in chunks),
        "reclaimedPagesTotal": sum(x["reclaimed"] for x in chunks),
        "maxWritten": max(x["written"] for x in chunks)}
for name in ("simple", "hybrid"):
    bt = sum(x[name]["bound"] for x in chunks)
    summ[name] = {"violations": [x["i"] for x in chunks if not x[name]["ok"]],
                  "boundPagesTotal": bt, "maxBound": max(x[name]["bound"] for x in chunks),
                  "meanBoundBytes": bt * B / K,
                  "alphaPageTerm": bt * B / (summ["reclaimedPagesTotal"] * P),
                  "slackPages": bt - summ["writtenPagesTotal"]}
json.dump({"summary": summ, "chunks": chunks}, open(os.path.join(work, "planner.json"), "w"), indent=1)
print(json.dumps(summ, indent=1))
top = sorted(chunks, key=lambda x: -x["written"])[:8]
for x in top:
    print(x["i"], "written", x["written"], "simple", x["simple"]["bound"], "hybrid", x["hybrid"]["bound"],
          "kinds", x["kinds"], "maxRefs", x["maxRefs"])
