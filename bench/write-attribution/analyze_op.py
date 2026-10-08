"""#901 spec §6.3 C-op / R-cov verdicts over one `run-op.sh` run.

usage: analyze_op.py RUN_DIR --mode {c-op,r-cov} [--control CONTROL_RUN]
                     [--require-chunk-validity]
       analyze_op.py --aggregate SEGMENT_RUN_DIR... [--min-chunks 5000]
         segmented R-cov (i) (spec §6.3, revision 11): parses each segment's
         verdict.txt (this tool's r-cov output: JSON, then a verdict line),
         requires every segment to PASS with its chunks, and reconciles chunk
         counts, failures, relocation validity over all chunks, the longest
         chunk, the largest fixed residual, Σ attributable against Σ charged
         (with the worst segment's ratio) and temp bytes; a segment that
         cannot be parsed makes the aggregate INVALID.

Reads the interposer's exit snapshots (`wtrace.<pid>.exit` for the operation
process, `copier.<pid>.exit` when a foreign process did the final checkpoint;
`baseline-ckpt.<pid>.exit` is reported, never counted) and `op.json`.
Bytes are classified by path: the database file, its `-wal`, and SQLite temp
files (`etilqs_*`).

  c-op : per run (one deletion operation): no temp bytes; WAL bytes at most
         1.05x the bytes copied into the database file; database + WAL + temp
         at most 12 KiB per deleted row.
  r-cov: database + WAL + temp of the operation process (plus the foreign
         copier) at most the durable charges the ledger recorded.

Revision 15 (Q16): the run (and its --control) must carry the input-mode
receipt (inputs.json); a frozen run needs the freeze's seal before and after
and complete, enforced activation receipts for its whole family, and a
control must carry the same evidence (one freeze, or both live). The result
names it under "inputs".

Fails closed with exit 2 and `INVALID: <reason>`: no passing self-test
receipt, a missing or dropped exit snapshot, a missing `op.json`, no
operation, or an operation that did not complete. PASS exits 0, FAIL 1.
"""
from __future__ import annotations

import glob
import json
import os
import sys

KiB = 1024
C_OP_WAL_FACTOR = 1.05
C_OP_BYTES_PER_ROW = 12 * KiB

# ── deletion receipts (spec §6.3 "Deletion receipts", 901-SR-014) ─────────
#
# Every deletion a C-op, R-cov or C run measures gets FOUR separate verdicts:
# validity (the evidence is complete and bound to the run), I4 (the write
# bounds), coverage (the bytes against the durable charge) and calibration
# (the reservation parts against 1.25 x this operation's components). A
# deletion that is covered but violates I4 fails, and no coefficient change
# alters an I4 verdict: I4's limits below are the spec's, never calibrated.
RECEIPT_VERSION = 1
I4_FIXED_BYTES = 512 * KiB
I4_PER_ROW_BYTES = 12 * KiB
I4_FALLBACK_PER_ROW_BYTES = 40 * KiB
I4_FALLBACK_ROW_THRESHOLD = 60_000
CALIBRATION_FACTOR = 1.25
WAL_HEADER_BYTES = 32
FRAME_HEADER_BYTES = 24


def ptrmap_pageno(page: int, usable: int, page_size: int) -> int:
    """SQLite's `ptrmapPageno`, written here independently of the product."""
    if page < 2:
        return 0
    per = usable // 5 + 1
    pending = 0x40000000 // page_size + 1
    base = ((page - 2) // per) * per + 2
    return base + 1 if base == pending else base


def independent_m_cap(page_count: int, usable: int, rows: int,
                      page_size: int) -> int:
    growth = -(-(I4_FIXED_BYTES + I4_PER_ROW_BYTES * rows) // page_size)
    return -(-(page_count + growth) // (usable // 5 + 1)) + 1


def independent_n_max(page_count: int, rows: int, page_size: int) -> int:
    return page_count + -(-(I4_FIXED_BYTES + I4_PER_ROW_BYTES * rows)
                          // page_size)


def independent_charge(rows: int, geometry: dict, coefficients: dict) -> int:
    """F + A x rows + B x M_cap with the coefficients the run recorded from
    the candidate tree, recomputed by this tool's own formula."""
    page_size, usable = geometry["pageSize"], geometry["usableSize"]
    per_row = (coefficients["perRowFallback"]
               if rows > I4_FALLBACK_ROW_THRESHOLD else coefficients["perRow"])
    b = -(-(5 * (2 * page_size + 24)) // (4 * KiB)) * KiB
    return (coefficients["fixed"] + per_row * rows
            + b * independent_m_cap(geometry["pageCount"], usable, rows,
                                    page_size))


def classify_frames(frames, usable: int, page_size: int) -> dict:
    """Frame identities -> distinct pointer-map pages M, distinct other pages
    D and repeated frames of each class. `frames`: page numbers in write
    order (a page rewritten in place appears again)."""
    seen = {}
    for pgno in frames:
        seen[pgno] = seen.get(pgno, 0) + 1
    ptrmap = {p for p in seen if p >= 2 and ptrmap_pageno(p, usable, page_size) == p}
    other = set(seen) - ptrmap
    return {"frames": len(frames), "M": len(ptrmap), "D": len(other),
            "repeatedPointerMap": sum(seen[p] - 1 for p in ptrmap),
            "repeatedOther": sum(seen[p] - 1 for p in other)}


def deletion_receipt(*, op: dict, frames, frame_source: str, geometry,
                     coefficients, wal_header_bytes: int, temp_bytes,
                     copy_bytes, identities: dict, committed: bool,
                     evidence_dropped: int = 0) -> dict:
    """One deletion's receipt and its four verdicts. `frames` is None when
    no frame-identity evidence exists (no frame log, no pinned WAL)."""
    invalid = []
    if frames is None:
        invalid.append(f"no frame identities ({frame_source})")
    elif not frames:
        invalid.append("the operation wrote no WAL frame of its own")
    if not geometry or any(geometry.get(k) is None for k in (
            "pageSize", "usableSize", "pageCount")):
        invalid.append("no independent pre-operation geometry")
    if op.get("outcome") != "ok":
        invalid.append(f"the operation did not complete ({op.get('outcome')})")
    if not committed:
        invalid.append("the operation is not in the committed operation set")
    if evidence_dropped:
        invalid.append(f"the interposer dropped {evidence_dropped} records")
    if temp_bytes is None or copy_bytes is None:
        invalid.append("missing temp or checkpoint-copy bytes")
    rows = int(op.get("rows") or 0)
    receipt = {
        "receiptVersion": RECEIPT_VERSION, "opId": op.get("op_id"),
        "provider": op.get("provider"), "mode": op.get("mode"), "rows": rows,
        "transaction": {k: op.get(k) for k in ("began", "ended")},
        "identities": identities, "frameSource": frame_source,
        "geometry": geometry, "reservationVersion": op.get(
            "reservation_version"),
        "coefficients": coefficients,
        "recordedInputs": {k: op.get(k) for k in (
            "page_count", "usable_size", "page_size", "pointer_map_cap")},
        "recordedCharge": op.get("charged_bytes"),
    }
    if invalid:
        receipt.update(validity={"pass": False, "reasons": invalid},
                       i4=None, coverage=None, calibration=None,
                       verdict="INVALID")
        return receipt
    page_size, usable = geometry["pageSize"], geometry["usableSize"]
    frame_cost = 2 * page_size + 24
    classes = classify_frames(frames, usable, page_size)
    m_cap = independent_m_cap(geometry["pageCount"], usable, rows, page_size)
    recomputed = independent_charge(rows, geometry, coefficients)
    wal_bytes = wal_header_bytes + classes["frames"] * (page_size
                                                        + FRAME_HEADER_BYTES)
    attributable = wal_bytes + copy_bytes + temp_bytes
    other_bytes = classes["D"] * frame_cost + wal_header_bytes + temp_bytes
    receipt.update(
        frames=classes,
        nMax=independent_n_max(geometry["pageCount"], rows, page_size),
        mCap=m_cap,
        walBytes=wal_bytes, walHeaderBytes=wal_header_bytes,
        framingBytes=classes["frames"] * FRAME_HEADER_BYTES,
        tempBytes=temp_bytes, copyBytes=copy_bytes,
        attributableBytes=attributable, recomputedCharge=recomputed,
        validity={"pass": True, "reasons": []})
    fallback = op.get("mode") == "fallback"
    i4 = []
    if fallback:
        ceiling = I4_FALLBACK_PER_ROW_BYTES * rows
        if attributable > ceiling:
            i4.append(f"fallback wrote {attributable} > 40 KiB x {rows} rows "
                      f"({ceiling}) through the final checkpoint")
    else:
        repeated = classes["repeatedPointerMap"] + classes["repeatedOther"]
        if repeated:
            i4.append(f"{repeated} repeated frames")
        if temp_bytes:
            i4.append(f"{temp_bytes} temp bytes")
        limit = I4_FIXED_BYTES + I4_PER_ROW_BYTES * rows
        if classes["D"] * frame_cost > limit:
            i4.append(f"(2P + 24) x D = {classes['D'] * frame_cost} > {limit}")
        if classes["M"] > m_cap:
            i4.append(f"M = {classes['M']} > M_cap = {m_cap}")
    coverage = []
    if op.get("charged_bytes") != recomputed:
        coverage.append(f"recorded charge {op.get('charged_bytes')} != "
                        f"recomputed {recomputed}")
    if attributable > (op.get("charged_bytes") or 0):
        coverage.append(f"attributable {attributable} > charged "
                        f"{op.get('charged_bytes')}")
    calibration = []
    per_row = (coefficients["perRowFallback"] if fallback
               else coefficients["perRow"])
    b = -(-(5 * frame_cost) // (4 * KiB)) * KiB
    fixed_and_rows = coefficients["fixed"] + per_row * rows
    if fallback:
        if CALIBRATION_FACTOR * attributable > fixed_and_rows + b * m_cap:
            calibration.append("1.25 x the fallback's bytes exceed its "
                               "reservation")
    else:
        if CALIBRATION_FACTOR * other_bytes > fixed_and_rows:
            calibration.append(f"1.25 x other bytes {other_bytes} > F + A x "
                               f"rows {fixed_and_rows}")
        if CALIBRATION_FACTOR * classes["M"] * frame_cost > b * m_cap:
            calibration.append("1.25 x pointer-map bytes > B x M_cap")
    receipt.update(
        i4={"pass": not i4, "reasons": i4},
        coverage={"pass": not coverage, "reasons": coverage},
        calibration={"pass": not calibration, "reasons": calibration,
                     "otherBytes": other_bytes,
                     "pointerMapBytes": classes["M"] * frame_cost},
        verdict="PASS" if not i4 and not coverage else "FAIL")
    return receipt


def frames_from_log(path, *, wal_name: str, began: float, ended: float):
    """Page numbers of the frame records a process logged for `wal_name`
    between `began` and `ended`, plus the WAL header bytes written then;
    None when the frame log is missing."""
    if not path or not os.path.exists(path):
        return None, 0
    pages, header = [], 0
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if (os.path.basename(row["path"]) != wal_name
                    or not began <= row["t"] <= ended):
                continue
            if row["frame"] == 0:
                header += WAL_HEADER_BYTES
            else:
                pages.append(row["pgno"])
    return pages, header


class Invalid(Exception):
    pass


def classify(paths: dict, db_name: str) -> dict:
    """{db, wal, temp, other} bytes from an exit snapshot's `paths`."""
    out = {"db": 0, "wal": 0, "temp": 0, "other": 0}
    for path, (written, _calls) in paths.items():
        base = os.path.basename(path)
        if base == db_name:
            out["db"] += written
        elif base == f"{db_name}-wal":
            out["wal"] += written
        elif base.startswith("etilqs_"):
            out["temp"] += written
        else:
            out["other"] += written
    return out


def merge(*parts: dict) -> dict:
    total = {"db": 0, "wal": 0, "temp": 0, "other": 0}
    for part in parts:
        for key in total:
            total[key] += part[key]
    return total


def verdict_c_op(bytes_: dict, rows: int) -> "tuple[bool, list[str]]":
    """C-op's whole-run conditions (spec §6.3): no temp bytes and WAL bytes
    at most 1.05 x the bytes copied into the database. Its I4 bound is the
    deletion receipt's own verdict (revision 6 replaced the 12 KiB-per-row
    total)."""
    problems = []
    if bytes_["temp"] != 0:
        problems.append(f"temp bytes {bytes_['temp']} (must be 0)")
    if bytes_["wal"] > C_OP_WAL_FACTOR * bytes_["db"]:
        problems.append(f"WAL {bytes_['wal']} > 1.05 x copied {bytes_['db']}")
    if rows <= 0:
        problems.append("no deleted row")
    return not problems, problems


def verdict_r_cov(bytes_: dict, charged: int) -> "tuple[bool, list[str]]":
    total = bytes_["db"] + bytes_["wal"] + bytes_["temp"]
    if total > charged:
        return False, [f"attributable {total} > charged {charged}"]
    return True, []


#: R-cov (i)'s validity evidence (spec §6.3), each required.
INTERIOR_MIN_CHILDREN = 298
INTERIOR_MIN_REGIONS = 250
#: R-cov (vi)'s production cache cap: an unbounded control must dirty more.
FALLBACK_CACHE_BYTES = 256 * 1024 * 1024


def certify_chunk(op: dict, independent: dict, written, *, repeated: int,
                  temp_bytes: int, wal_header_bytes: int) -> dict:
    """One reclaim chunk on its own complete transaction (spec §6.3 R-cov
    (i), G3v): no repeated frame, no temp bytes, |W| <= |ID| + UNID + 1 and
    |W \\ ID| <= UNID + 1 against its recorded plan, that plan equal to the
    independent recomputation, and attributable bytes <= its own charge,
    which equals the reservation the record's inputs imply."""
    plan = op.get("plan") or {}
    page_size = op.get("page_size") or 0
    problems = []
    if not plan:
        return {"opId": op.get("op_id"), "pass": False, "invalid": True,
                "problems": ["no recorded plan"]}
    identified = set(plan["identified"])
    unid = plan["unidentified"]
    w = set(written)
    if repeated:
        problems.append(f"{repeated} repeated frames")
    if temp_bytes:
        problems.append(f"{temp_bytes} temp bytes")
    if len(w) > len(identified) + unid + 1:
        problems.append(f"|W| {len(w)} > |ID| + UNID + 1 = "
                        f"{len(identified) + unid + 1}")
    if len(w - identified) > unid + 1:
        problems.append(f"|W \\ ID| {len(w - identified)} > UNID + 1 = {unid + 1}")
    if (sorted(identified), unid) != (independent["identified"],
                                      independent["unidentified"]):
        problems.append("the recorded plan differs from its independent "
                        "recomputation")
    frame_cost = 2 * page_size + 24
    expected = ((len(identified) + unid + 1) * frame_cost
                + (op.get("fixed_bytes") or 0))
    if op.get("charged_bytes") != expected:
        problems.append(f"charge {op.get('charged_bytes')} != planned "
                        f"reservation {expected}")
    attributable = len(w) * frame_cost + wal_header_bytes
    if attributable > (op.get("charged_bytes") or 0):
        problems.append(f"attributable {attributable} > charge "
                        f"{op.get('charged_bytes')}")
    fixed_residual = attributable - (len(identified) + unid + 1) * frame_cost
    return {"opId": op.get("op_id"), "pass": not problems,
            "problems": problems, "W": len(w), "ID": len(identified),
            "UNID": unid, "outside": len(w - identified),
            "attributable": attributable, "charge": op.get("charged_bytes"),
            "fixedResidual": max(0, fixed_residual),
            "durationMs": round(1000 * (op["ended"] - op["began"]), 1),
            "inspectionMs": op.get("inspection_ms"),
            "freelistReduction": op.get("freelist_reduction"),
            "detail": independent["detail"]}


def chunk_validity(chunks) -> "list[str]":
    """R-cov (i): the run must relocate an interior page with >= 298
    children over >= 250 pointer-map pages, leaves with overflow heads and a
    non-terminal overflow page; anything missing makes the run INVALID."""
    steps = [d for c in chunks for d in c.get("detail") or ()]
    missing = []
    if not any(d["kind"] == "interior"
               and d.get("children", 0) >= INTERIOR_MIN_CHILDREN
               and d.get("regions", 0) >= INTERIOR_MIN_REGIONS for d in steps):
        missing.append("no relocated interior page with >= 298 children over "
                       ">= 250 pointer-map pages")
    if not any(d["kind"] == "leaf" and d.get("heads") for d in steps):
        missing.append("no relocated leaf with overflow heads")
    if not any(d["kind"] == "overflow" and d.get("nonTerminal") for d in steps):
        missing.append("no relocated non-terminal overflow page")
    return missing


def fallback_validity(receipt: dict, *, control_distinct_pages, page_size,
                      cache_bytes) -> "list[str]":
    """R-cov (vi) (901-SR-015): the production cap with the file journal,
    an unbounded control whose distinct dirty pages exceed it, repeated
    pointer-map frames and statement-journal temp bytes."""
    missing = []
    if receipt.get("mode") != "fallback":
        missing.append("the deletion did not take the fallback")
    if cache_bytes != FALLBACK_CACHE_BYTES:
        missing.append(f"cache cap {cache_bytes} is not the production 256 MiB")
    if (control_distinct_pages is None
            or control_distinct_pages * page_size <= FALLBACK_CACHE_BYTES):
        missing.append("no unbounded control whose distinct dirty pages exceed "
                       "256 MiB")
    frames = receipt.get("frames") or {}
    if not frames.get("repeatedPointerMap"):
        missing.append("no repeated pointer-map frame")
    if not receipt.get("tempBytes"):
        missing.append("no statement-journal temp bytes")
    return missing


def _exit_snapshot(run: str, prefix: str, pid=None) -> "tuple[int, dict, dict] | None":
    pattern = os.path.join(run, f"{prefix}.{pid if pid else '*'}.exit")
    matches = sorted(glob.glob(pattern))
    if not matches:
        return None
    if len(matches) > 1:
        raise Invalid(f"more than one {prefix} exit snapshot")
    with open(matches[0]) as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    if not lines:
        raise Invalid(f"empty {prefix} exit snapshot")
    snap = lines[-1]
    if snap.get("dropped", 0) or snap.get("framesDropped", 0):
        raise Invalid(f"{prefix} snapshot dropped {snap.get('dropped')} bytes "
                      f"and {snap.get('framesDropped', 0)} frame records")
    return snap["pid"], snap["paths"], snap


def _pinned_pages(run_db: str, op: dict, page_size: int) -> "list[int] | None":
    wal = f"{run_db}.wal.pinned"
    if not os.path.exists(wal):
        return None
    import independent_plan
    snaps = independent_plan.PinnedSnapshots(f"{run_db}.start", wal, page_size)
    try:
        return snaps.frames_between(op["walFrom"], op["walTo"])
    finally:
        snaps.close()


def analyze(run: str, mode: str, *, control: "str | None" = None,
            require_chunk_validity: bool = False) -> dict:
    receipt_path = os.path.join(run, "selftest.txt")
    if (not os.path.exists(receipt_path)
            or not open(receipt_path).read().rstrip().endswith("selftest: PASS")):
        raise Invalid("no passing interposer self-test receipt (selftest.txt)")
    # Revision 15 (Q16): the input mode, and for a frozen run the freeze's
    # seal and complete enforced activation receipts for every process of
    # the family (frozen_roots.check_inputs); a control must carry the same
    # evidence (one freeze, or both live).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import frozen_roots
    inputs = frozen_roots.check_inputs(run)
    if inputs:
        raise Invalid("inputs: " + "; ".join(inputs[:5]))
    label = frozen_roots.evidence_label(run)
    if control is not None:
        control_inputs = frozen_roots.check_inputs(control)
        if control_inputs:
            raise Invalid("control inputs: " + "; ".join(control_inputs[:5]))
        if frozen_roots.evidence_label(control) != label:
            raise Invalid("mixed input evidence in one comparison: run "
                          f"{label}, control {frozen_roots.evidence_label(control)}")
    op_path = os.path.join(run, "op.json")
    if not os.path.exists(op_path):
        raise Invalid("no op.json")
    report = json.load(open(op_path))
    ops = report.get("ops") or []
    if not ops:
        raise Invalid("no operation ran")
    if any(op.get("outcome") != "ok" for op in ops):
        raise Invalid("an operation did not complete")
    measured = _exit_snapshot(run, "wtrace", report["pid"])
    if measured is None:
        raise Invalid(f"no exit snapshot for the operation pid {report['pid']}")
    db_name = os.path.basename(report["db"])
    parts = [classify(measured[1], db_name)]
    copier = _exit_snapshot(run, "copier")
    if copier is not None:
        parts.append(classify(copier[1], db_name))
    baseline = _exit_snapshot(run, "baseline-ckpt")
    bytes_ = merge(*parts)
    units = report.get("rows") or report.get("pages") or 0
    charges = sum(int(op.get("charged_bytes") or 0) for op in ops)
    committed = charges == int(report.get("chargedBytes") or 0)
    frames_log = os.path.join(run, f"wtrace.{report['pid']}.frames")
    wal_name = f"{db_name}-wal"
    single = len(ops) == 1
    receipts, chunks = [], []
    # The 56e66f07a RED proof (`--baseline`) and R-cov (vi)'s control
    # (`--control-unbounded`) run the shipped statements, which carry no
    # charge or geometry: they are judged on the whole run only.
    shipped = bool(report.get("baseline") or report.get("control"))
    deletions = [op for op in ops if op.get("kind") == "delete"
                 and not shipped]
    reclaims = [op for op in ops if op.get("kind") == "reclaim"]
    for op in deletions:
        frames, header = frames_from_log(
            frames_log, wal_name=wal_name, began=op["began"], ended=op["ended"])
        page_size = (op.get("geometry") or {}).get("pageSize") or 4096
        if report.get("pinned") and frames is not None:
            pinned = _pinned_pages(report["db"], op, page_size)
            if pinned is None or set(pinned) != set(frames):
                frames = None                    # the two sources disagree
        copy = bytes_["db"] if single else (
            None if frames is None else len(set(frames)) * page_size)
        receipts.append(deletion_receipt(
            op=op, frames=frames,
            frame_source="frame log" + (" + pinned WAL" if report.get("pinned")
                                        else ""),
            geometry=op.get("geometry"),
            coefficients=report.get("coefficients") or {},
            wal_header_bytes=header, temp_bytes=bytes_["temp"],
            copy_bytes=copy, identities=report.get("identities") or {},
            committed=committed))
    if reclaims:
        if not report.get("pinned"):
            raise Invalid("reclaim chunks are certified only from a pinned run")
        import independent_plan
        page_size = reclaims[0].get("page_size") or 4096
        snaps = independent_plan.PinnedSnapshots(
            f"{report['db']}.start", f"{report['db']}.wal.pinned", page_size)
        cache = independent_plan.FreelistCache()
        try:
            for op in reclaims:
                snaps.advance(op["walFrom"])
                steps = (op.get("plan") or {}).get("steps") or 0
                independent = independent_plan.recompute(snaps, steps, cache)
                logged, header = frames_from_log(
                    frames_log, wal_name=wal_name, began=op["began"],
                    ended=op["ended"])
                pinned = snaps.frames_between(op["walFrom"], op["walTo"])
                if logged is None:
                    raise Invalid("no frame log for the reclaim chunks")
                repeated = len(logged) - len(set(logged))
                chunks.append(certify_chunk(
                    op, independent, pinned, repeated=repeated,
                    temp_bytes=0, wal_header_bytes=header))
        finally:
            snaps.close()
    problems = []
    if mode == "c-op":
        ok, problems = verdict_c_op(bytes_, int(report.get("rows") or 0))
    else:
        ok, problems = verdict_r_cov(bytes_, int(report.get("chargedBytes") or 0))
        if bytes_["temp"] and reclaims:
            problems.append(f"{bytes_['temp']} temp bytes during reclaim")
    invalid = [r for r in receipts if r["verdict"] == "INVALID"]
    if invalid:
        raise Invalid("; ".join(invalid[0]["validity"]["reasons"]))
    for r in receipts:
        failing = [] if mode == "r-cov" else (r["i4"]["reasons"])
        if mode == "r-cov":
            failing = r["i4"]["reasons"] + r["coverage"]["reasons"]
        problems.extend(f"op {r['opId']}: {p}" for p in failing)
    if any(c.get("invalid") for c in chunks):
        raise Invalid("a reclaim chunk has no recorded plan")
    for c in chunks:
        problems.extend(f"chunk {c['opId']}: {p}" for p in c["problems"])
    validity = []
    if require_chunk_validity:
        validity += chunk_validity(chunks)
    if control is not None:
        control_report = json.load(open(os.path.join(control, "op.json")))
        control_pages = None
        if control_report.get("ops"):
            cop = control_report["ops"][0]
            cframes, _h = frames_from_log(
                os.path.join(control, f"wtrace.{control_report['pid']}.frames"),
                wal_name=wal_name, began=cop["began"], ended=cop["ended"])
            control_pages = None if cframes is None else len(set(cframes))
        for r in receipts:
            validity += fallback_validity(
                r, control_distinct_pages=control_pages,
                page_size=(r.get("geometry") or {}).get("pageSize") or 4096,
                cache_bytes=report.get("deletionCacheBytes"))
    if validity:
        raise Invalid("; ".join(validity))
    attributable = bytes_["db"] + bytes_["wal"] + bytes_["temp"]
    reduction = sum(int(c.get("freelistReduction") or 0) for c in chunks)
    return {
        "mode": mode, "pass": not problems, "problems": problems,
        "inputs": label,
        "bytes": bytes_, "attributable": attributable,
        "chargedBytes": report.get("chargedBytes"),
        "operations": len(ops), "units": units,
        "perOperation": attributable / len(ops),
        "perUnit": (attributable / units) if units else None,
        "foreignCopier": copier is not None,
        "baselineCheckpoint": (None if baseline is None
                               else classify(baseline[1], db_name)),
        "footprintPeakBytes": report.get("footprintPeakBytes"),
        "receipts": receipts, "chunks": chunks,
        "reservationPerReclaimedByte": (
            sum(c["charge"] or 0 for c in chunks) / (reduction * page_size)
            if chunks and reduction else None),
        "maxChunkFixedResidual": max((c["fixedResidual"] for c in chunks),
                                     default=None),
        "maxChunkDurationMs": max((c["durationMs"] for c in chunks),
                                  default=None),
    }


#: Spec §6.3 R-cov (i): the consecutive product reclaim chunks a run needs.
R_COV_I_MIN_CHUNKS = 5000


def parse_verdict(text: str) -> "tuple[dict, str]":
    """A segment's `verdict.txt` is this tool's stdout: the JSON result
    followed by one verdict line (`PASS` or `FAIL: ...`), or a lone
    `INVALID: ...` line. Raises Invalid on anything else."""
    lines = text.rstrip().splitlines()
    if not lines:
        raise Invalid("empty verdict")
    verdict = lines[-1].strip()
    if verdict.startswith("INVALID"):
        raise Invalid(verdict)
    if not (verdict == "PASS" or verdict.startswith("FAIL")):
        raise Invalid(f"no verdict line (ends with {verdict[:60]!r})")
    try:
        result = json.loads("\n".join(lines[:-1]))
    except ValueError as exc:
        raise Invalid(f"unparsable result JSON: {exc}")
    if not isinstance(result, dict):
        raise Invalid("the result is not a JSON object")
    return result, verdict


def chain_problems(dirs) -> "list[str]":
    """Amendment 19 HR-21: segmented R-cov (i) is one clone's consecutive
    chunks. Each segment after the first must continue the one before it,
    proven from the operation records rather than assumed: the same tree,
    the same input evidence, operation ids that continue without a gap,
    and the page count and freelist the previous segment's last chunk left
    (its record's values less its reductions); a segment whose run
    recorded its cloned copy (source.json) must have cloned the previous
    segment's database."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import frozen_roots
    problems, previous = [], None
    for directory in dirs:
        try:
            with open(os.path.join(directory, "op.json")) as fh:
                op = json.load(fh)
        except (OSError, ValueError):
            problems.append(f"{directory}: no operation record (op.json)")
            previous = None
            continue
        ops = sorted((o for o in op.get("ops") or [] if o.get("op_id")),
                     key=lambda o: o["op_id"])
        current = {"dir": directory, "ops": ops,
                   "rev": (op.get("identities") or {}).get("gitRev"),
                   "label": frozen_roots.evidence_label(directory),
                   "db": os.path.realpath(os.path.join(directory,
                                                       "conversations.db"))}
        if not ops:
            problems.append(f"{directory}: no operations in op.json")
        if previous is not None and ops and previous["ops"]:
            last, first = previous["ops"][-1], ops[0]
            where = f"{directory} after {previous['dir']}"
            if current["rev"] != previous["rev"] or not current["rev"]:
                problems.append(f"{where}: another tree ({previous['rev']} -> "
                                f"{current['rev']})")
            if current["label"] != previous["label"]:
                problems.append(f"{where}: other input evidence "
                                f"({previous['label']} -> {current['label']})")
            if first["op_id"] != last["op_id"] + 1:
                problems.append(f"{where}: operation ids do not continue "
                                f"({last['op_id']} -> {first['op_id']})")
            for field, reduction in (("page_count", "page_count_reduction"),
                                     ("freelist_count", "freelist_reduction")):
                try:
                    expect = int(last[field]) - int(last[reduction])
                    got = int(first[field])
                except (KeyError, TypeError, ValueError):
                    problems.append(f"{where}: no {field} to chain")
                    continue
                if got != expect:
                    problems.append(f"{where}: {field} {got} does not continue "
                                    f"the previous segment's {expect}")
            source = None
            try:
                with open(os.path.join(directory, "source.json")) as fh:
                    source = json.load(fh).get("src")
            except (OSError, ValueError):
                source = None
            if source is not None and os.path.realpath(source) != previous["db"]:
                problems.append(f"{where}: cloned {source}, not the previous "
                                f"segment's database {previous['db']}")
        previous = current
    return problems


def aggregate(dirs, *, min_chunks: int = R_COV_I_MIN_CHUNKS) -> dict:
    """Spec §6.3 segmented R-cov (i): consecutive segments chained on one
    clone, each with its own verdict. Every segment must be an r-cov PASS with
    its chunks; the aggregate reconciles chunk counts, failures, run (i)'s
    relocation validity over ALL chunks, the longest chunk, the largest fixed
    residual, the summed attributable bytes against the summed charges (and
    the worst segment's ratio) and the temp bytes. Evidence that is missing is
    never inferred from passing chunks: an unparsable segment, a segment
    without chunks, a repeated operation or too few chunks is INVALID."""
    segments, chunks, invalid, problems = [], [], [], []
    for directory in dirs:
        path = os.path.join(directory, "verdict.txt")
        try:
            result, verdict = parse_verdict(open(path).read())
        except OSError as exc:
            raise Invalid(f"{directory}: no verdict.txt ({exc.strerror})")
        except Invalid as exc:
            raise Invalid(f"{directory}: {exc}")
        seg_chunks = result.get("chunks") or []
        if result.get("mode") != "r-cov":
            invalid.append(f"{directory}: mode {result.get('mode')}, not r-cov")
        if not seg_chunks:
            invalid.append(f"{directory}: no reclaim chunks")
        elif len(seg_chunks) != result.get("operations"):
            invalid.append(f"{directory}: {len(seg_chunks)} chunks for "
                           f"{result.get('operations')} operations")
        if verdict != "PASS" or not result.get("pass"):
            problems.append(f"{directory}: {verdict}")
        attributable = result.get("attributable")
        charged = result.get("chargedBytes")
        temp = (result.get("bytes") or {}).get("temp")
        if attributable is None or charged is None or temp is None:
            invalid.append(f"{directory}: missing attributable, charged or "
                           "temp bytes")
        segments.append({"dir": directory, "verdict": verdict,
                         "chunks": len(seg_chunks), "attributable": attributable,
                         "charged": charged, "temp": temp,
                         "ratio": (attributable / charged)
                         if attributable is not None and charged else None})
        chunks.extend(seg_chunks)
    if not segments:
        raise Invalid("no segments")
    ids = [c.get("opId") for c in chunks]
    if len(set(ids)) != len(ids):
        invalid.append("an operation appears in more than one chunk")
    if len(chunks) < min_chunks:
        invalid.append(f"{len(chunks)} chunks (need {min_chunks})")
    validity = chunk_validity(chunks)
    invalid.extend(validity)
    invalid.extend(chain_problems(dirs))
    if invalid:
        raise Invalid("; ".join(invalid))
    failed = [c for c in chunks if not c.get("pass")]
    if failed:
        problems.append(f"{len(failed)} failed chunks")
    temp = sum(seg["temp"] for seg in segments)
    if temp:
        problems.append(f"{temp} temp bytes over the segments")
    attributable = sum(seg["attributable"] for seg in segments)
    charged = sum(seg["charged"] for seg in segments)
    if attributable > charged:
        problems.append(f"attributable {attributable} > charged {charged}")
    return {
        "mode": "r-cov-aggregate", "pass": not problems, "problems": problems,
        "segments": segments, "chunks": len(chunks), "failedChunks": len(failed),
        "chunkValidity": {"complete": not validity, "missing": validity},
        "maxChunkDurationMs": max((c.get("durationMs") or 0) for c in chunks),
        "maxChunkFixedResidual": max((c.get("fixedResidual") or 0)
                                     for c in chunks),
        "attributable": attributable, "charged": charged,
        "worstSegmentRatio": max((seg["ratio"] for seg in segments
                                  if seg["ratio"] is not None), default=None),
        "tempBytes": temp,
    }


def main(argv=None) -> int:
    import argparse

    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--aggregate":
        parser = argparse.ArgumentParser(description=aggregate.__doc__)
        parser.add_argument("--aggregate", nargs="+", required=True,
                            metavar="SEGMENT_RUN_DIR")
        parser.add_argument("--min-chunks", type=int, default=R_COV_I_MIN_CHUNKS)
        args = parser.parse_args(argv)
        try:
            result = aggregate(args.aggregate, min_chunks=args.min_chunks)
        except Invalid as exc:
            print(f"INVALID: {exc}")
            return 2
        print(json.dumps(result, indent=2, sort_keys=True))
        print("PASS" if result["pass"] else "FAIL: " + "; ".join(result["problems"]))
        return 0 if result["pass"] else 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run")
    parser.add_argument("--mode", choices=("c-op", "r-cov"), required=True)
    parser.add_argument("--control", help="R-cov (vi): the unbounded-cache "
                        "control run directory")
    parser.add_argument("--require-chunk-validity", action="store_true",
                        help="R-cov (i): require the interior, leaf-head and "
                        "non-terminal overflow relocations")
    args = parser.parse_args(argv)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        result = analyze(args.run, args.mode, control=args.control,
                         require_chunk_validity=args.require_chunk_validity)
    except Invalid as exc:
        print(f"INVALID: {exc}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    print("PASS" if result["pass"] else "FAIL: " + "; ".join(result["problems"]))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
