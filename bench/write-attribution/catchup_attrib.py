#!/usr/bin/env python3
"""Per-object write attribution of one `run-catchup.sh` run (#939).

  catchup_attrib.py RUN [--db conversations.db] [--json OUT.json]

RUN holds the driver receipt (driver.json), the interposer's files
(w.<pid>, w.<pid>.exit, w.<pid>.frames, w.<pid>.dbpages; WTRACE_DBPAGES=1) and
two page maps per database (pagemap.py): pre-<db> of the store copy the clone
came from and post-<db> of the clone after the pass.

WAL bytes are charged by record kind (`wtrace.c`): the first header of a frame
in a WAL generation is a new frame, 24 bytes; each page-data record
(`"data": 1`, WTRACE_DBPAGES=1) is one page, charged to the page its frame's
header named; a later header for the same frame in the same generation is a
checksum rewrite (`walRewriteChecksums`, after a transaction that outgrew the
page cache overwrote earlier frames in place), 24 bytes. A generation starts at
each 32-byte WAL header write (SQLite writes one whenever it restarts the log at
frame 1), never at a salt: while a rewrite is pending SQLite writes new frame
headers with a zero salt and checksum (`walEncodeFrame`) and fixes them at
commit. A run whose log has no data
records (an older interposer) charges page + 24 per new frame and reports
`dataRecords: false`; its overwrites are then unattributed and show in the
reconciliation. Every main-database page write (the checkpoint copies) is one
page. Ownership is the post-pass map, then the pre-pass map for a page the pass
freed, else `<unowned>`. Reconciliation proves totals, not ownership. The
interposer records page numbers, not page images, and conversations.db runs
`auto_vacuum=INCREMENTAL`, so a page freed and reused within the run (A->B->A,
or free->A->B) is invisible at both endpoints. Object figures are therefore
endpoint attributions. The bytes on pages whose endpoint owners differ, or that
are unowned at either endpoint, are reported as `endpointOwnerChanged`: a lower
bound on reassignment, never a bound on misattribution (#939 R1-07).
Totals are reconciled with the
interposer's per-path byte table (exit snapshots, every pid), so an
attribution that does not add up is reported, never hidden. Each object also
gets its distinct pages and its rewrite factor (frames / distinct pages), and
objects are grouped into classes: FTS (a table whose name contains `_fts`),
index (an index or an autoindex) and table. Physical bytes are the driver
process's ri_diskio_bytes_written delta from the driver's start sample to the
interposer's exit snapshot (`diskioWritten`, after the interpreter's exit
handlers, so the same end as the logical totals); a run without that field
falls back to the driver's own end sample (`physicalBoundary: "driver"`).

Per-occurrence ownership (#939 spec 2.2): when RUN holds an ownership log
(ownership.jsonl, `conversation_catchup.py run --ownership`, `ownership.py`)
for a store with pointer maps, ownership is resolved per write occurrence
instead (`ownership: "per-occurrence"`). Interposer records and the log's
observations are merged on their shared clock (a record at or before an
observation's read start precedes it). Every WAL occurrence waits on its slot
(interposer generation, frame) until an observation reports the slot
committed: then the slot's last header and last page-data write are charged
to the page's owner after that commit, and every earlier occurrence of the
slot (written when a transaction outgrew the page cache, then superseded, or
abandoned by a rollback whose positions a later transaction reused) is
unresolved. A slot never committed (a rollback, a WAL restart, or a run's end
before an observation) is unresolved. A checkpoint page write is charged at
the next observation to the owner of the page's latest committed frame in
that observation's WAL generation (else the previous one's); when that
observation shows `nBackfill` below `mxFrame` (a reader-limited checkpoint)
it is unresolved. `unresolvedBytes` (WAL, main file, by reason) sits beside
the object table and is part of the reconciliation; `ownershipLogBytes` is
the log's own size, which the physical total includes. A store without a log
or without pointer maps (`auto_vacuum=NONE`) keeps the endpoint attribution
above, labelled `ownership: "endpoint"`.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ownership  # noqa: E402
import pagemap  # noqa: E402


def _jsonl(path):
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def exit_paths(run) -> "dict[str, int]":
    totals: "dict[str, int]" = collections.Counter()
    exits = glob.glob(os.path.join(run, "w.*.exit"))
    for path in exits:
        last = None
        for rec in _jsonl(path):
            last = rec
        if last:
            for p, (b, _c) in last["paths"].items():
                totals[p] += b
    return dict(totals), len(exits)


def classify(name, meta) -> str:
    if name.startswith("<"):        # <unowned>, <orphan-data>
        return "unowned"
    if "_fts" in name:
        return "fts"
    kind = meta["types"].get(name)
    if kind == "index" or name.startswith("sqlite_autoindex_"):
        return "index"
    return "table"


def phase_of(windows):
    """The driver's phase (provider) whose [start, end] holds a record's time;
    `between` for a record outside every window (open, close, a checkpoint
    that a later statement triggered)."""
    spans = sorted((v[0], v[1], k) for k, v in (windows or {}).items())

    def label(t):
        for a, b, k in spans:
            if a <= t <= b:
                return k
        return "between"
    return label


def _log_meta(store) -> dict:
    """A page-map-shaped meta from the ownership log's object names."""
    types, tables = {}, {}
    for name, kind, tbl in store["names"].values():
        if kind:
            types[name], tables[name] = kind, tbl
    return {"types": types, "tables": tables, "pages": {}, "pageCount": None}


def per_occurrence(records, dbpages, store, page, phase, has_data) -> dict:
    """Spec 2.2's per-occurrence charging (pure). `records`: the store's WAL
    frame records in write order; `dbpages`: its main-file page records;
    `store`: its `ownership.load` entry. Returns the charged bytes per object
    and the unresolved bytes by reason."""
    names = {int(k): v[0] for k, v in store["names"].items()}
    events = [(r["t"], 0, i, "wal", r) for i, r in enumerate(records)]
    events += [(r["t"], 0, i, "db", r) for i, r in enumerate(
        sorted(dbpages, key=lambda r: r["t"]))]
    events += [(-1.0 if o["kind"] == "base" else o["t0"], 1, i, "obs", o)
               for i, o in enumerate(store["observations"])]
    events.sort(key=lambda e: e[:3])
    wal, wal_new, wal_over, wal_rck = (collections.Counter() for _ in range(4))
    wal_pages = collections.defaultdict(set)
    ckpt, ckpt_pages = collections.Counter(), collections.defaultdict(set)
    by_phase = collections.defaultdict(collections.Counter)
    unresolved = {"wal": collections.Counter(), "db": collections.Counter()}
    pending = {}              # (igen, frame) -> [pgno, [header occ], [data occ]]
    queued = []               # main-file page writes awaiting an observation
    owners = {}               # salt -> {pgno: owner id}
    salt = prev_salt = None
    igen = headers = commits = non_page = frames = 0
    head_bytes = 24 if has_data else page + 24
    # A commit frame's header is written again by walRewriteChecksums after a
    # spill; it is still one commit, so a frame counts once per generation.
    commit_frames = set()

    def lose(slot, reason):
        unresolved["wal"][reason] += sum(n for _t, n in slot[1] + slot[2])

    for _t, _k, _i, kind, rec in events:
        if kind == "wal":
            if rec.get("data"):
                slot = pending.get((igen, rec["frame"]))
                if slot is None:
                    unresolved["wal"]["orphan-data"] += page
                else:
                    slot[2].append((rec["t"], page))
            elif rec["frame"] == 0:
                headers += 1
                igen += 1
                commit_frames.clear()
                for key in [k for k in pending if k[0] < igen]:
                    lose(pending.pop(key), "abandoned")
            else:
                if rec.get("commit") and rec["frame"] not in commit_frames:
                    commit_frames.add(rec["frame"])
                    commits += 1
                slot = pending.setdefault((igen, rec["frame"]),
                                          [rec["pgno"], [], []])
                slot[0] = rec["pgno"]
                slot[1].append((rec["t"], head_bytes))
            continue
        if kind == "db":
            if rec["pgno"] == 0:
                non_page += rec["n"]
            else:
                queued.append(rec)
            continue
        obs = rec
        osalt = tuple(obs["salt"]) if obs.get("salt") else None
        if obs.get("restart") or osalt != salt:
            prev_salt, salt = salt, osalt
        table = owners.setdefault(salt, {})
        for frame, pgno, oid in obs["frames"]:
            table[pgno] = oid
            if obs["kind"] == "base":
                continue
            slot = pending.pop((igen, frame), None)
            if slot is None:
                continue
            if slot[0] != pgno:
                lose(slot, "page-mismatch")
                continue
            name = names[oid]
            (th, nh), rest_h = slot[1][-1] if slot[1] else (None, 0), slot[1][:-1]
            last_d, rest_d = (slot[2][-1] if slot[2] else None), slot[2][:-1]
            unresolved["wal"]["superseded"] += sum(n for _t, n in rest_h + rest_d)
            frames += 1
            wal_new[name] += 1
            wal_over[name] += len(rest_d)
            wal_rck[name] += len(rest_h)
            wal_pages[name].add(pgno)
            if th is not None:
                wal[name] += nh
                by_phase[phase(th)][name] += nh
            if last_d is not None:
                wal[name] += last_d[1]
                by_phase[phase(last_d[0])][name] += last_d[1]
        limited = obs.get("walPresent") and obs["nBackfill"] < obs["mxFrame"]
        for rec_d in queued:
            if limited:
                unresolved["db"]["reader-limited"] += page
                continue
            oid = owners.get(salt, {}).get(rec_d["pgno"])
            if oid is None:
                oid = owners.get(prev_salt, {}).get(rec_d["pgno"])
            if oid is None:
                unresolved["db"]["no-committed-frame"] += page
                continue
            name = names[oid]
            ckpt[name] += 1
            ckpt_pages[name].add(rec_d["pgno"])
            by_phase[phase(rec_d["t"])][name] += page
        queued = []
    for slot in pending.values():
        lose(slot, "never-committed")
    unresolved["db"]["unobserved"] += page * len(queued)
    return {"wal": wal, "walNew": wal_new, "walOver": wal_over,
            "walRck": wal_rck, "walPages": wal_pages, "ckpt": ckpt,
            "ckptPages": ckpt_pages, "byPhase": by_phase,
            "unresolved": {k: {r: n for r, n in v.items() if n}
                           for k, v in unresolved.items()},
            "headers": headers, "commits": commits, "nonPage": non_page,
            "frames": frames}


def attribute(run, db) -> dict:
    log_path = os.path.join(run, "ownership.jsonl")
    store = (ownership.load(log_path).get(db)
             if os.path.exists(log_path) else None)
    if store is not None and store["ptrmap"]:
        return attribute_per_occurrence(run, db, store)
    report = attribute_endpoint(run, db)
    report["ownership"] = "endpoint"
    report["ownershipFallback"] = ("no ownership log" if store is None
                                   else "the store has no pointer maps")
    return report


def _maps(run, db):
    out = []
    for side in ("pre", "post"):
        prefix = os.path.join(run, f"{side}-{db}")
        out.append(pagemap.load(prefix)[0] if os.path.exists(prefix + ".json")
                   else None)
    return out


def attribute_per_occurrence(run, db, store) -> dict:
    pre_meta, post_meta = _maps(run, db)
    log_meta = _log_meta(store)
    page = store["pageSize"]
    driver = json.loads(pathlib.Path(run, "driver.json").read_text())
    phase = phase_of(driver.get("windows"))
    paths, nexits = exit_paths(run)
    wal_path = next((p for p in paths if p.endswith("/" + db + "-wal")), None)
    db_path = next((p for p in paths if p.endswith("/" + db)), None)
    records = []
    for fp in sorted(glob.glob(os.path.join(run, "w.*.frames"))):
        records.extend(r for r in _jsonl(fp) if r["path"].endswith("/" + db + "-wal"))
    dbpages = []
    for fp in sorted(glob.glob(os.path.join(run, "w.*.dbpages"))):
        dbpages.extend(r for r in _jsonl(fp) if r["path"].endswith("/" + db))
    has_data = any(r.get("data") for r in records)
    got = per_occurrence(records, dbpages, store, page, phase, has_data)
    meta_for = lambda name: next((m for m in (post_meta, pre_meta, log_meta)
                                  if m and name in m["types"]), log_meta)
    objects = []
    for name in set(got["wal"]) | set(got["ckpt"]):
        meta = meta_for(name)
        w_bytes, c_bytes = got["wal"][name], got["ckpt"][name] * page
        distinct = len(got["walPages"][name])
        objects.append({
            "object": name, "class": classify(name, meta),
            "table": meta.get("tables", {}).get(name, name),
            "walFrames": got["walNew"][name],
            "walOverwrites": got["walOver"][name],
            "walChecksumRewrites": got["walRck"][name], "walBytes": w_bytes,
            "ckptPages": got["ckpt"][name], "ckptBytes": c_bytes,
            "totalBytes": w_bytes + c_bytes, "distinctPages": distinct,
            "rewriteFactor": round(got["walNew"][name] / distinct, 2) if distinct else None,
            "pagesPre": (pre_meta or {}).get("pages", {}).get(name, 0),
            "pagesPost": (post_meta or {}).get("pages", {}).get(name, 0),
        })
    objects.sort(key=lambda o: -o["totalBytes"])
    classes = collections.defaultdict(lambda: collections.Counter())
    for o in objects:
        for k in ("walBytes", "ckptBytes", "totalBytes"):
            classes[o["class"]][k] += o[k]
    un_wal = sum(got["unresolved"]["wal"].values())
    un_db = sum(got["unresolved"]["db"].values())
    attributed_wal = sum(got["wal"].values()) + un_wal + got["headers"] * 32
    attributed_db = sum(got["ckpt"].values()) * page + un_db + got["nonPage"]
    measured_wal = paths.get(wal_path, 0) if wal_path else 0
    measured_db = paths.get(db_path, 0) if db_path else 0
    total = attributed_wal + attributed_db
    return {
        "db": db, "pageSize": page, "exitSnapshots": nexits,
        "ownership": "per-occurrence",
        "ownershipObservations": sum(1 for o in store["observations"]
                                     if o["kind"] == "obs"),
        "ownershipLogBytes": store["bytes"],
        "measured": {"walBytes": measured_wal, "dbBytes": measured_db,
                     "total": measured_wal + measured_db},
        "attributed": {"walBytes": attributed_wal, "dbBytes": attributed_db,
                       "walFrames": got["frames"], "walHeaders": got["headers"],
                       "commitFrames": got["commits"],
                       "nonPageDbBytes": got["nonPage"],
                       "dataRecords": has_data,
                       "walOverwrites": sum(got["walOver"].values()),
                       "walChecksumRewrites": sum(got["walRck"].values()),
                       "orphanDataRecords": got["unresolved"]["wal"].get(
                           "orphan-data", 0) // page},
        "unresolvedBytes": {
            "wal": un_wal, "db": un_db, "total": un_wal + un_db,
            "share": round((un_wal + un_db) / total, 4) if total else None,
            "reasons": got["unresolved"]},
        "reconciled": {
            "wal": round(attributed_wal / measured_wal, 4) if measured_wal else None,
            "db": round(attributed_db / measured_db, 4) if measured_db else None},
        "walToCheckpoint": round(attributed_wal / attributed_db, 3) if attributed_db else None,
        "pageCountPre": (pre_meta or {}).get("pageCount"),
        "pageCountPost": (post_meta or {}).get("pageCount"),
        "classes": {k: dict(v) for k, v in classes.items()},
        "phases": {ph: {"totalBytes": sum(c.values()),
                        "classes": dict(_class_totals(c, post_meta or log_meta,
                                                      pre_meta or log_meta)),
                        "top": c.most_common(12)}
                   for ph, c in sorted(got["byPhase"].items())},
        "objects": objects,
    }


def attribute_endpoint(run, db) -> dict:
    pre_meta, pre = pagemap.load(os.path.join(run, f"pre-{db}"))
    post_meta, post = pagemap.load(os.path.join(run, f"post-{db}"))
    page = post_meta["pageSize"]
    names_pre, names_post = pre_meta["names"], post_meta["names"]

    def owner(pgno: int) -> str:
        if pgno < len(post) and post[pgno]:
            return names_post[post[pgno]]
        if pgno < len(pre) and pre[pgno]:
            return names_pre[pre[pgno]]
        return "<unowned>"

    def klass(pgno: int) -> str:
        """Endpoint stability of a page: `stable` (same owner at both
        endpoints), `changed` (owners differ, or freed by the run),
        `allocated` (unowned before the run: new, or taken from the free list)
        or `unowned` (at both). Reuse inside the run is invisible, so only
        `changed` and `allocated` pages are known to be unstable, and even
        `stable` pages may have been reused in between."""
        before = names_pre[pre[pgno]] if pgno < len(pre) and pre[pgno] else None
        after = names_post[post[pgno]] if pgno < len(post) and post[pgno] else None
        if before is None:
            return "unowned" if after is None else "allocated"
        return "changed" if after != before else "stable"

    stab = collections.Counter()         # bytes by endpoint stability class

    def charge(pgno: int, name: str, n: int) -> None:
        k = klass(pgno)
        stab[k] += n
        if k in ("changed", "allocated"):
            amb[name] += n

    amb = collections.Counter()          # bytes on endpoint-unstable pages
    driver = json.loads(pathlib.Path(run, "driver.json").read_text())
    phase = phase_of(driver.get("windows"))
    by_phase = collections.defaultdict(collections.Counter)   # phase -> name -> bytes
    paths, nexits = exit_paths(run)
    wal_path = next((p for p in paths if p.endswith("/" + db + "-wal")), None)
    db_path = next((p for p in paths if p.endswith("/" + db)), None)
    wal = collections.Counter()          # bytes per object
    wal_new = collections.Counter()      # new frames per object
    wal_over = collections.Counter()     # in-place page overwrites per object
    wal_rck = collections.Counter()      # checksum-rewrite headers per object
    wal_pages = collections.defaultdict(set)
    ckpt = collections.Counter()
    ckpt_pages = collections.defaultdict(set)
    headers = commits = frames = data_records = rck = over = orphan = 0
    non_page = 0
    records = []
    for fp in glob.glob(os.path.join(run, "w.*.frames")):
        records.extend(r for r in _jsonl(fp) if r["path"].endswith("/" + db + "-wal"))
    has_data = any(r.get("data") for r in records)
    seen = {}                            # (generation, frame) -> [pgno, data writes]
    gen = 0
    for rec in records:                  # one writer: file order is write order
        if rec.get("data"):
            data_records += 1
            slot = seen.get((gen, rec["frame"]))
            if slot is None:
                orphan += 1
                wal["<orphan-data>"] += page
                continue
            name = owner(slot[0])
            wal[name] += page
            by_phase[phase(rec["t"])][name] += page
            charge(slot[0], name, page)
            slot[1] += 1
            if slot[1] > 1:
                over += 1
                wal_over[name] += 1
            continue
        if rec["frame"] == 0:
            headers += 1
            gen += 1
            continue
        key = (gen, rec["frame"])
        name = owner(rec["pgno"])
        if key in seen:
            rck += 1
            wal_rck[name] += 1
            wal[name] += 24
            by_phase[phase(rec["t"])][name] += 24
            charge(rec["pgno"], name, 24)
            continue
        seen[key] = [rec["pgno"], 0]
        frames += 1
        wal_new[name] += 1
        wal_pages[name].add(rec["pgno"])
        wal[name] += 24 if has_data else page + 24
        by_phase[phase(rec["t"])][name] += 24 if has_data else page + 24
        charge(rec["pgno"], name, 24 if has_data else page + 24)
        if rec["commit"]:
            commits += 1
    for fp in glob.glob(os.path.join(run, "w.*.dbpages")):
        for rec in _jsonl(fp):
            if not rec["path"].endswith("/" + db):
                continue
            if rec["pgno"] == 0:
                non_page += rec["n"]
                continue
            name = owner(rec["pgno"])
            ckpt[name] += 1
            ckpt_pages[name].add(rec["pgno"])
            by_phase[phase(rec["t"])][name] += page
            charge(rec["pgno"], name, page)
    objects = []
    for name in set(wal) | set(ckpt):
        meta = post_meta if name in post_meta["types"] else pre_meta
        w_bytes = wal[name]
        c_bytes = ckpt[name] * page
        distinct = len(wal_pages[name])
        objects.append({
            "object": name, "class": classify(name, meta),
            "table": meta.get("tables", {}).get(name, name),
            "walFrames": wal_new[name], "walOverwrites": wal_over[name],
            "walChecksumRewrites": wal_rck[name], "walBytes": w_bytes,
            "ckptPages": ckpt[name], "ckptBytes": c_bytes,
            "totalBytes": w_bytes + c_bytes, "distinctPages": distinct,
            "rewriteFactor": round(wal_new[name] / distinct, 2) if distinct else None,
            "pagesPre": pre_meta["pages"].get(name, 0),
            "pagesPost": post_meta["pages"].get(name, 0),
        })
    objects.sort(key=lambda o: -o["totalBytes"])
    classes = collections.defaultdict(lambda: collections.Counter())
    for o in objects:
        for k in ("walBytes", "ckptBytes", "totalBytes"):
            classes[o["class"]][k] += o[k]
    attributed_wal = sum(wal.values()) + headers * 32
    attributed_db = sum(ckpt.values()) * page + non_page
    measured_wal = paths.get(wal_path, 0) if wal_path else 0
    measured_db = paths.get(db_path, 0) if db_path else 0
    return {
        "db": db, "pageSize": page, "exitSnapshots": nexits,
        "measured": {"walBytes": measured_wal, "dbBytes": measured_db,
                     "total": measured_wal + measured_db},
        "attributed": {"walBytes": attributed_wal, "dbBytes": attributed_db,
                       "walFrames": frames, "walHeaders": headers,
                       "commitFrames": commits, "nonPageDbBytes": non_page,
                       "dataRecords": has_data, "walOverwrites": over,
                       "walChecksumRewrites": rck, "orphanDataRecords": orphan},
        "reconciled": {
            "wal": round(attributed_wal / measured_wal, 4) if measured_wal else None,
            "db": round(attributed_db / measured_db, 4) if measured_db else None},
        "walToCheckpoint": round(attributed_wal / attributed_db, 3) if attributed_db else None,
        "pageCountPre": pre_meta["pageCount"], "pageCountPost": post_meta["pageCount"],
        "classes": {k: dict(v) for k, v in classes.items()},
        "phases": {ph: {"totalBytes": sum(c.values()),
                        "classes": dict(_class_totals(c, post_meta, pre_meta)),
                        "top": c.most_common(12)}
                   for ph, c in sorted(by_phase.items())},
        "objects": objects,
        "endpointStability": {k: stab[k] for k in ("stable", "changed", "allocated", "unowned")},
        "endpointOwnerChanged": {
            "bytes": sum(amb.values()),
            "share": round(sum(amb.values()) / (attributed_wal + attributed_db), 4)
            if (attributed_wal + attributed_db) else None,
            "top": amb.most_common(12)},
    }


def _class_totals(counter, post_meta, pre_meta):
    out = collections.Counter()
    for name, n in counter.items():
        meta = post_meta if name in post_meta["types"] else pre_meta
        out[classify(name, meta)] += n
    return out


def summary(run) -> dict:
    driver = json.loads(pathlib.Path(run, "driver.json").read_text())
    rs, re_ = driver.get("rusageStart") or {}, driver.get("rusageEnd") or {}
    paths, _n = exit_paths(run)
    at_driver_end = (re_.get("dwrite", 0) - rs.get("dwrite", 0)) if rs and re_ else None
    at_exit = None
    exit_file = pathlib.Path(run, f"w.{driver.get('pid')}.exit")
    if rs and exit_file.exists():
        last = None
        for rec in _jsonl(exit_file):
            last = rec
        if last and isinstance(last.get("diskioWritten"), int) and last["diskioWritten"] >= 0:
            at_exit = last["diskioWritten"] - rs.get("dwrite", 0)
    return {
        "tree": driver.get("tree"), "mode": driver.get("mode"),
        "phases": driver.get("phases"),
        # #939 R1-08: the physical total ends where the logical per-path totals
        # end, at the interposer's exit snapshot (after the interpreter's exit
        # handlers, a store's final checkpoint among them). Older runs, whose
        # snapshots carry no counter, keep the driver's own end sample.
        "physicalBytes": at_exit if at_exit is not None else at_driver_end,
        "physicalBoundary": "exit" if at_exit is not None else "driver",
        "physicalBytesAtDriverEnd": at_driver_end,
        "logicalWritesRusage": (re_.get("logical_writes", 0) - rs.get("logical_writes", 0)) if rs and re_ else None,
        "interposerTotal": sum(paths.values()),
        "byPath": dict(sorted(paths.items(), key=lambda kv: -kv[1])[:12]),
        "claude": driver.get("claude"), "codex": driver.get("codex"),
    }


def _gb(n):
    return f"{n / 1e9:8.3f}"


def render(report) -> str:
    out = []
    s = report["summary"]
    out.append(f"tree {s['tree']}  mode {s['mode']}  phases {s['phases']}")
    phys = s["physicalBytes"]
    out.append(f"physical {_gb(phys) if phys is not None else '   n/a'} GB "
               f"(boundary {s.get('physicalBoundary', 'driver')})  "
               f"interposer {_gb(s['interposerTotal'])} GB")
    for a in report["dbs"]:
        m, t = a["measured"], a["attributed"]
        out.append(f"\n{a['db']}: WAL {_gb(m['walBytes'])} GB  checkpoint/db {_gb(m['dbBytes'])} GB  "
                   f"(reconciled wal {a['reconciled']['wal']}, db {a['reconciled']['db']}; "
                   f"commits {t['commitFrames']}, WAL resets {t['walHeaders']}, "
                   f"frames {t['walFrames']}, overwrites {t['walOverwrites']}, "
                   f"checksum rewrites {t['walChecksumRewrites']}, "
                   f"pages {a['pageCountPre']}->{a['pageCountPost']})")
        out.append(f"  ownership {a.get('ownership', 'endpoint')}"
                   + (f" ({a['ownershipFallback']})" if a.get("ownershipFallback") else ""))
        un = a.get("unresolvedBytes")
        if un is not None:
            out.append(f"  unresolved {_gb(un['total']).strip()} GB (share {un['share']}; "
                       f"wal {un['reasons']['wal']}, db {un['reasons']['db']}); "
                       f"ownership log {a['ownershipLogBytes']} bytes")
        oc = a.get("endpointOwnerChanged")
        if oc is not None:
            out.append(f"  endpoint owner changed or unowned: {_gb(oc['bytes']).strip()} GB "
                       f"(share {oc['share']}; a lower bound on reassignment, not a "
                       f"misattribution bound); top {oc['top'][:4]}")
        for cls, v in sorted(a["classes"].items(), key=lambda kv: -kv[1]["totalBytes"]):
            out.append(f"  class {cls:8s} total {_gb(v['totalBytes'])}  wal {_gb(v['walBytes'])}  ckpt {_gb(v['ckptBytes'])}")
        for ph, v in a.get("phases", {}).items():
            cls = "  ".join(f"{k} {_gb(n).strip()}" for k, n in
                            sorted(v["classes"].items(), key=lambda kv: -kv[1]))
            out.append(f"  phase {ph:12s} total {_gb(v['totalBytes'])}  ({cls})")
        out.append(f"  {'object':52s} {'total GB':>8s} {'wal GB':>8s} {'ckpt GB':>8s} {'distinct':>9s} {'rewrite':>7s}")
        for o in a["objects"][:25]:
            out.append(f"  {o['object'][:52]:52s} {_gb(o['totalBytes'])} {_gb(o['walBytes'])} "
                       f"{_gb(o['ckptBytes'])} {o['distinctPages']:9d} {o['rewriteFactor'] or 0:7.1f}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run")
    ap.add_argument("--db", action="append")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    dbs = args.db or [p[len(os.path.join(args.run, "post-")):-len(".json")]
                      for p in sorted(glob.glob(os.path.join(args.run, "post-*.json")))]
    report = {"summary": summary(args.run), "dbs": [attribute(args.run, d) for d in dbs]}
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, indent=1) + "\n")
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
