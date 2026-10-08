"""#901 Q9: the reclaim planner's pure stepping and its read path (G3p).

The read path runs only in a helper subprocess in the product. Cases that
must observe the parent's locks drive the product chunk; cases that corrupt
or race the files run the reader on COPIES of a store (or in a subprocess
with a hook), never on a file this process holds SQLite locks on.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import pathlib
import shutil
import sqlite3
import struct
import subprocess
import sys
import textwrap

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402
import _retention_fixtures as fx  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
P4K = 4096


def _planner():
    return importlib.import_module("_lib_reclaim_planner")


# ── pure stepping (injected geometry) ─────────────────────────────────────

def test_the_pointer_map_page_skips_the_pending_byte_page():
    planner = _planner()
    pending = planner.pending_byte_page(P4K)
    assert pending == 262_145
    per = P4K // 5 + 1
    for page in range(pending - 3 * per, pending + 3 * per, 97):
        base = ((page - 2) // per) * per + 2
        expected = base + 1 if base == pending else base
        assert planner.ptrmap_pageno(page, P4K, P4K) == expected
    # With 1 KiB pages a map page lands exactly on the pending-byte page
    # (1,048,577 = 2 + 205 x 5,115), so it moves up by one.
    pending_1k = planner.pending_byte_page(1024)
    assert (pending_1k - 2) % 205 == 0
    assert planner.ptrmap_pageno(pending_1k + 5, 1024, 1024) == pending_1k + 1
    assert planner.is_ptrmap(pending_1k + 1, 1024, 1024)


def test_the_stepping_crosses_the_pending_byte_and_a_pointer_map_page():
    """`incrVacuumStep` skips pointer-map and pending-byte pages; the file
    needs more than 1 GiB, so the geometry is injected."""
    planner = _planner()
    pending = planner.pending_byte_page(P4K)
    page_count = pending + 3
    steps = planner.tail_steps(page_count, 200_000, 6,
                               usable_size=P4K, page_size=P4K)
    moved = [s.page for s in steps]
    assert pending not in moved
    assert all(not planner.is_ptrmap(p, P4K, P4K) for p in moved if p)
    ends = [s.new_end for s in steps]
    assert all(e != pending and not planner.is_ptrmap(e, P4K, P4K)
               for e in ends)
    assert moved[:3] == [pending + 3, pending + 2, pending + 1]
    assert ends[2] == pending - 1, "the pending-byte page is skipped"
    # A pointer-map page at the new end is skipped too: 8,202 = 2 + 10 x 820.
    steps = planner.tail_steps(8203, 5000, 2, usable_size=P4K, page_size=P4K)
    assert [(s.page, s.new_end) for s in steps] == [(8203, 8201), (8201, 8200)]


def test_the_full_vacuum_end_mirrors_final_db_size():
    planner = _planner()
    # finalDbSize(10,000, 3,000): nPtrmap = (3,000 - 10,000 + 9,842 + 819)
    # / 819 = 4 map pages freed, so the end is 10,000 - 3,000 - 4.
    assert planner.final_db_size(10_000, 3_000, P4K, P4K) == 6_996
    assert planner.final_db_size(10_000, 0, P4K, P4K) == 10_000
    # Enough free pages to empty the last map page's region frees that map
    # page (6,562 = 2 + 8 x 820) as well: one page fewer at the end.
    assert planner.final_db_size(6_600, 37, P4K, P4K) == 6_563
    assert planner.final_db_size(6_600, 38, P4K, P4K) == 6_561


def test_a_freelist_count_at_or_above_the_page_count_refuses():
    planner = _planner()
    with pytest.raises(planner.ReclaimPlanRefused) as exc:
        planner.tail_steps(100, 100, 4, usable_size=P4K, page_size=P4K)
    assert exc.value.reason == "unsupported_geometry"


def _leaf(cells, kind=13, usable=P4K):
    """A b-tree page image with the given (offset -> bytes) cells."""
    image = bytearray(usable)
    image[0] = kind
    header = 12 if kind in (2, 5) else 8
    struct.pack_into(">H", image, 3, len(cells))
    for i, (offset, body) in enumerate(cells):
        struct.pack_into(">H", image, header + 2 * i, offset)
        image[offset:offset + len(body)] = body
    if kind in (2, 5):
        struct.pack_into(">I", image, 8, 77)
    return bytes(image)


def test_cells_yield_children_and_overflow_heads_by_the_local_payload_rule():
    planner = _planner()
    # Table leaf: payload 5000 > U - 35, so the cell keeps
    # minLocal + (5000 - minLocal) % (U - 4) local bytes and a head pointer.
    min_local = (P4K - 12) * 32 // 255 - 23
    local = min_local + (5000 - min_local) % (P4K - 4)
    body = bytes([0xA7, 0x08, 0x05]) + b"x" * local + struct.pack(">I", 99)
    kind, children, heads = planner.btree_refs(
        _leaf([(1000, body)]), P4K, 1000)
    assert (kind, children, heads) == (13, [], [99])
    # Table interior: a child pointer and a rowid, no payload.
    interior = _leaf([(2000, struct.pack(">I", 42) + b"\x05")], kind=5)
    assert planner.btree_refs(interior, P4K, 1000) == (5, [42, 77], [])


@pytest.mark.parametrize("image, why", [
    (bytes([9]) + bytes(P4K - 1), "page type"),
    (_leaf([(5000, b"\x01")]), "cell pointer"),
])
def test_a_malformed_page_refuses(image, why):
    planner = _planner()
    with pytest.raises(planner.ReclaimPlanRefused) as exc:
        planner.btree_refs(image, P4K, 1000)
    assert exc.value.reason == "malformed_page", why


def test_a_looping_trunk_chain_refuses():
    planner = _planner()
    trunk = bytearray(P4K)
    struct.pack_into(">II", trunk, 0, 7, 0)        # trunk 7 -> 7
    pages = {7: bytes(trunk)}
    with pytest.raises(planner.ReclaimPlanRefused) as exc:
        planner.scan_freelist(pages.__getitem__, 7, 10, 100, P4K, set())
    assert exc.value.reason == "cyclic_freelist"


def test_the_audited_range_is_a_constant_beside_g3q():
    planner = _planner()
    assert (planner.AUDITED_SQLITE_MIN, planner.AUDITED_SQLITE_MAX) == (
        (3, 37, 2), (3, 54, 0))
    assert planner.sqlite_audited((3, 53, 4))
    assert not planner.sqlite_audited((3, 54, 1))
    assert not planner.sqlite_audited((3, 37, 1))


# ── the read path (G3p) ───────────────────────────────────────────────────

def _env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    conn = ns["open_conversations_db"](attach_cache=False)
    retention = importlib.import_module("_lib_conversation_retention")
    importlib.import_module("_lib_write_io").reset_for_tests()
    return ns, conn, retention, pathlib.Path(retention._resolve_main_db_path(conn))


def _fill_and_free(conn, rows=400):
    conn.execute("CREATE TABLE IF NOT EXISTS rp_filler(payload BLOB)")
    conn.executemany("INSERT INTO rp_filler VALUES (?)",
                     [(os.urandom(3000),) for _ in range(rows)])
    conn.commit()
    conn.execute("DELETE FROM rp_filler WHERE rowid % 2 = 0")
    conn.commit()


def _store_with_wal(tmp_path, monkeypatch):
    """A store whose current pages live in WAL frames (no checkpoint since
    the last writes), with a normalized record and a freelist."""
    ns, conn, retention, db_path = _env(tmp_path, monkeypatch)
    _fill_and_free(conn)
    retention.normalize_reclaim_record(conn, NOW)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("UPDATE cache_meta SET value = value WHERE key = ?",
                 (retention.RECLAIM_PENDING_KEY,))
    conn.execute("DELETE FROM rp_filler WHERE rowid % 5 = 0")
    conn.commit()
    return ns, conn, retention, db_path


def _request(db_path, conn, **over):
    page_size, page_count, freelist = (
        int(conn.execute(f"PRAGMA {p}").fetchone()[0])
        for p in ("page_size", "page_count", "freelist_count"))
    request = {"db": str(db_path), "pageSize": page_size,
               "pageCount": page_count, "freelistCount": freelist,
               "maxSteps": 16, "budgetBytes": 4 * 1024 * 1024,
               "fixedBytes": 64 * 1024, "readBudgetBytes": 256 * 1024 * 1024,
               "deadlineMs": 10_000}
    request.update(over)
    return request


def _copy_store(db_path, target_dir):
    """Byte copies of the database, its WAL and its wal-index while a
    connection keeps them, for reader cases that corrupt or extend them."""
    target_dir.mkdir()
    target = target_dir / "copy.db"
    for side in ("", "-wal", "-shm"):
        shutil.copyfile(f"{db_path}{side}", f"{target}{side}")
    return target


def _plan_in_subprocess(request, hook_source=""):
    """`plan_from_store` in a fresh interpreter, with an optional hook."""
    script = "\n".join([
        "import json, sqlite3, sys",
        f"sys.path.insert(0, {str(_BIN)!r})",
        "import _lib_reclaim_planner as planner",
        "hooks = {}",
        "observed = {}",
        textwrap.dedent(hook_source),
        "answer = planner.plan_from_store(json.loads(sys.stdin.read()), "
        "hooks=hooks)",
        "answer['observed'] = observed",
        "print(json.dumps(answer))",
    ])
    child = subprocess.run([sys.executable, "-c", script],
                           input=json.dumps(request), capture_output=True,
                           text=True, timeout=120)
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout)


def _same_plan(a, b):
    keys = ("steps", "identified", "unidentified", "new_end", "page_count",
            "freelist_count", "reservation_bytes", "kinds", "digest")
    return {k: a[k] for k in keys} == {k: b[k] for k in keys}


def test_a_concurrent_passive_checkpoint_does_not_change_the_plan(
        tmp_path, monkeypatch):
    """While the parent holds the chunk's transaction, another connection's
    PASSIVE checkpoint copies the WAL into the file DURING the plan; the
    plan equals one made without it (pages with a frame are read from the
    frame, the others are never written by the checkpoint)."""
    ns, conn, retention, db_path = _store_with_wal(tmp_path, monkeypatch)
    request = _request(db_path, conn)
    # `conn` stays open (idle) so its close never checkpoints the WAL away.
    parent = retention.open_reclaim_connection(db_path)
    try:
        parent.execute("BEGIN IMMEDIATE")
        parent.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
        quiet = _plan_in_subprocess(request)
        raced = _plan_in_subprocess(request, textwrap.dedent("""
            def after_scan(reader):
                c = sqlite3.connect(reader.db_path)
                observed["checkpoint"] = list(
                    c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
                c.close()
            hooks["after_scan"] = after_scan
        """))
    finally:
        parent.rollback()
        parent.close()
    assert quiet["ok"] and raced["ok"], (quiet, raced)
    busy, log, copied = raced["observed"]["checkpoint"]
    assert busy == 0 and copied > 0, "validity: the checkpoint really copied"
    assert _same_plan(quiet["plan"], raced["plan"])
    conn.close()


def test_a_wal_reset_between_attempts_uses_no_stale_frame(
        tmp_path, monkeypatch):
    """After a chunk, the WAL is restarted and a new generation overwrites
    its start; the next plan equals one made from a WAL-free copy."""
    ns, conn, retention, db_path = _store_with_wal(tmp_path, monkeypatch)
    rc = retention.open_reclaim_connection(db_path)
    try:
        first = retention.reclaim_chunk(rc, now_utc=NOW, pages=4)
    finally:
        rc.close()
    assert first.outcome == "ok", first
    restart = conn.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()
    assert restart[0] == 0
    conn.execute("INSERT INTO cache_meta(key, value) VALUES ('g3p-gen', 'x')")
    conn.commit()
    request = _request(db_path, conn)
    parent = retention.open_reclaim_connection(db_path)
    try:
        parent.execute("BEGIN IMMEDIATE")
        parent.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
        live = _plan_in_subprocess(request)
    finally:
        parent.rollback()
        parent.close()
    reference = tmp_path / "reference.db"
    dst = sqlite3.connect(reference)
    conn.backup(dst)
    dst.execute("PRAGMA journal_mode=WAL")
    dst.close()
    holder = sqlite3.connect(reference)
    holder.execute("SELECT 1").fetchone()
    ref = _plan_in_subprocess({**request, "db": str(reference)})
    holder.close()
    assert live["ok"] and ref["ok"], (live, ref)
    assert _same_plan(live["plan"], ref["plan"])
    conn.close()


def test_a_checksum_valid_commit_past_mxframe_is_ignored(
        tmp_path, monkeypatch):
    """Frames physically present past the wal-index's mxFrame - here a
    complete, checksum-valid later commit - are never read."""
    ns, conn, retention, db_path = _store_with_wal(tmp_path, monkeypatch)
    request = _request(db_path, conn)
    s1 = _copy_store(db_path, tmp_path / "s1")
    before = _planner().plan_from_store({**request, "db": str(s1)})
    # A later commit that changes page 1 (the freelist) and the tail.
    conn.execute("CREATE TABLE IF NOT EXISTS rp_late(payload BLOB)")
    conn.executemany("INSERT INTO rp_late VALUES (?)",
                     [(os.urandom(3000),) for _ in range(5)])
    conn.commit()
    wal = pathlib.Path(f"{db_path}-wal").read_bytes()
    copied = pathlib.Path(f"{s1}-wal").read_bytes()
    assert len(wal) > len(copied)
    pathlib.Path(f"{s1}-wal").write_bytes(copied + wal[len(copied):])
    _ps, frames = fx.wal_frames(s1)
    assert any(commit for _o, _p, commit in frames[-3:]), (
        "validity: a complete, checksum-valid commit lies past mxFrame")
    after = _planner().plan_from_store({**request, "db": str(s1)})
    assert before["ok"] and after["ok"], (before, after)
    assert _same_plan(before["plan"], after["plan"])
    conn.close()


def _corrupt(path, offset, delta=1):
    data = bytearray(pathlib.Path(path).read_bytes())
    data[offset] = (data[offset] + delta) % 256
    pathlib.Path(path).write_bytes(bytes(data))


@pytest.mark.parametrize("damage, reason", [
    ("index-copy", "wal_index_unreadable"),
    ("frame-salt", "wal_checksum_mismatch"),
    ("frame-page", "wal_checksum_mismatch"),
    ("budget", "read_budget_exhausted"),
    ("deadline", "read_budget_exhausted"),
    ("identity", "identity_changed"),
])
def test_an_unsafe_read_refuses_with_its_reason(tmp_path, monkeypatch,
                                                damage, reason):
    ns, conn, retention, db_path = _store_with_wal(tmp_path, monkeypatch)
    request = _request(db_path, conn)
    copy = _copy_store(db_path, tmp_path / "copy")
    request["db"] = str(copy)
    hooks = None
    page_size = request["pageSize"]
    if damage == "index-copy":
        _corrupt(f"{copy}-shm", 48 + 16)            # the second copy's mxFrame
    elif damage == "frame-salt":
        _corrupt(f"{copy}-wal", 32 + 8)
    elif damage == "frame-page":
        _corrupt(f"{copy}-wal", 32 + 24 + 100)
    elif damage == "budget":
        request["readBudgetBytes"] = page_size
    elif damage == "deadline":
        request["deadlineMs"] = 0
    else:
        def swap(reader):
            replacement = pathlib.Path(f"{copy}-wal.new")
            shutil.copyfile(f"{copy}-wal", replacement)
            os.replace(replacement, f"{copy}-wal")
        hooks = {"before_recheck": swap}
    answer = _planner().plan_from_store(request, hooks=hooks)
    assert (answer["ok"], answer["reason"]) == (False, reason), answer
    conn.close()


def _chunk_with_helper(retention, db_path, command, **kw):
    rc = retention.open_reclaim_connection(db_path)
    try:
        return retention.reclaim_chunk(rc, now_utc=NOW, pages=16, **kw)
    finally:
        rc.close()


@pytest.mark.parametrize("script, reason", [
    ("import sys; sys.exit(3)", "helper_failed"),
    ("import sys; print('not json')", "helper_failed"),
    ("import time; time.sleep(60)", "helper_timeout"),
])
def test_a_failing_helper_refuses_with_no_vacuum_and_no_charge(
        tmp_path, monkeypatch, script, reason):
    ns, conn, retention, db_path = _store_with_wal(tmp_path, monkeypatch)
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    stored = conn.execute("SELECT value FROM cache_meta WHERE key=?",
                          (retention.RECLAIM_PENDING_KEY,)).fetchone()[0]
    monkeypatch.setattr(retention, "_PLANNER_COMMAND",
                        [sys.executable, "-c", script])
    monkeypatch.setattr(retention, "_PLANNER_DEADLINE_SECONDS", 1.0)
    result = _chunk_with_helper(retention, db_path, script)
    assert (result.outcome, result.skip_reason, result.charged_bytes) == (
        "skipped", reason, 0), result
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free
    assert conn.execute("SELECT value FROM cache_meta WHERE key=?",
                        (retention.RECLAIM_PENDING_KEY,)).fetchone()[0] == stored
    conn.close()


def test_the_parent_opens_no_raw_descriptor_and_keeps_its_locks(
        tmp_path, monkeypatch):
    """The dashboard process never opens `conversations.db`, its WAL or its
    `-shm` with a raw descriptor (closing one would drop its POSIX locks):
    a reader pinned in this process still blocks another process's TRUNCATE
    checkpoint after the chunk."""
    ns, conn, retention, db_path = _store_with_wal(tmp_path, monkeypatch)
    conn.close()
    reader = sqlite3.connect(db_path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
    opened = []
    real_open = os.open
    names = {str(db_path), f"{db_path}-wal", f"{db_path}-shm"}

    def spy_open(path, *args, **kwargs):
        if os.fspath(path) in names:
            opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)
    rc = retention.open_reclaim_connection(db_path)
    try:
        result = retention.reclaim_chunk(rc, now_utc=NOW, pages=16)
    finally:
        rc.close()
    monkeypatch.setattr(os, "open", real_open)
    assert result.outcome == "ok", result
    assert opened == []
    child = subprocess.run(
        [sys.executable, "-c",
         "import sqlite3, sys; c = sqlite3.connect(sys.argv[1], timeout=0); "
         "print(list(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()))",
         str(db_path)], capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr
    busy = json.loads(child.stdout)[0]
    reader.rollback()
    reader.close()
    assert busy == 1, "the pinned reader's locks survived the chunk"
