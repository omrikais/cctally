"""#496 S1 — forensics-time WAL evidence, damage scans, and the publication
transaction.

These share one file because they share the kill/drive harness: proving that a
rebuild's published bytes are validated, and that a failed publication is
durable rather than merely raised, both require killing a real process at a
known seam and then observing what a LATER process finds on disk.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import signal
import sqlite3
import struct
import subprocess
import sys
import time

import pytest

from conftest import load_script, redirect_paths

ROOT = pathlib.Path(__file__).resolve().parents[1]
CCTALLY = ROOT / "bin" / "cctally"
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

_W1 = int(dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc).timestamp())


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_live_index():
    """One journaled observation folded into a live stats.db."""
    import _cctally_journal as jr
    import _lib_journal as J

    jr.append_record(
        J.make_obs(
            at="2026-01-04T09:00:00Z",
            src="record-usage",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": _W1,
                "source": "statusline",
                "captured_at": "2026-01-04T09:00:00Z",
            },
        )
    )
    jr.run_stats_ingest(mode="authoritative")
    return jr


def _await_marker(
    marker: pathlib.Path, process: subprocess.Popen, budget_s: float = 30.0
) -> None:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text().strip():
            return
        assert process.poll() is None, (
            f"process exited at rc={process.returncode} before {marker.name}"
        )
        time.sleep(0.01)
    raise AssertionError(f"process never reached pause marker {marker}")


def _test_wal_index_snapshot(db: pathlib.Path) -> dict:
    """Test-only structural view, derived independently from production code."""
    wal_path = pathlib.Path(f"{db}-wal")
    shm_path = pathlib.Path(f"{db}-shm")
    wal = wal_path.read_bytes()
    shm = shm_path.read_bytes()
    native = "<" if sys.byteorder == "little" else ">"
    page_size = struct.unpack_from(">I", wal, 8)[0]
    mx_frame = struct.unpack_from(f"{native}I", shm, 16)[0]
    n_page = struct.unpack_from(f"{native}I", shm, 20)[0]
    n_backfill = struct.unpack_from(f"{native}I", shm, 96)[0]
    n_backfill_attempted = struct.unpack_from(f"{native}I", shm, 128)[0]
    compared = min(mx_frame, (len(wal) - 32) // (24 + page_size), 4062)
    mismatches = []
    for index in range(compared):
        frame_offset = 32 + index * (24 + page_size)
        wal_page = struct.unpack_from(">I", wal, frame_offset)[0]
        shm_page = struct.unpack_from(f"{native}I", shm, 136 + index * 4)[0]
        if wal_page != shm_page:
            mismatches.append(
                {"frame": index + 1, "walPage": wal_page, "shmPage": shm_page}
            )
    return {
        "walSalt": wal[16:24].hex(),
        "shmSalt": shm[32:40].hex(),
        "pageSize": page_size,
        "mxFrame": mx_frame,
        "nPage": n_page,
        "nBackfill": n_backfill,
        "nBackfillAttempted": n_backfill_attempted,
        "comparedFrames": compared,
        "mappingMismatchCount": len(mismatches),
        "mappingMismatchSample": mismatches[:8],
    }


def _strand_committed_wal_family(
    db: pathlib.Path, tmp_path: pathlib.Path
) -> tuple:
    """Leave a committed, non-empty WAL+SHM family as if its writer crashed.

    Modelled on tests/test_stats_rebuild_cutover_388.py, which uses the same
    disable-autocheckpoint-then-SIGKILL shape, with one deliberate difference.
    The schema change is committed and CHECKPOINTED first, and only a plain
    INSERT is left stranded. Measured against SQLite 3.53.4: a commit that
    changes the schema writes page 1 into the WAL, and a page-1 frame then
    MASKS a damaged main-file header completely -- `PRAGMA quick_check` even
    answers `ok`. An INSERT-only commit writes no page-1 frame, so the damage
    this test then applies is actually observable.
    """
    marker = tmp_path / "wal-writer.pid"
    script = """
import os, pathlib, signal, sqlite3, sys
db, marker = sys.argv[1:]
conn = sqlite3.connect(db, timeout=5)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE forensics_wal_probe (value TEXT NOT NULL)")
conn.commit()
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("INSERT INTO forensics_wal_probe VALUES ('committed')")
conn.commit()
pathlib.Path(marker).write_text(f"{os.getpid()}\\n")
os.kill(os.getpid(), signal.SIGSTOP)
"""
    writer = subprocess.Popen(
        [sys.executable, "-c", script, str(db), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _await_marker(marker, writer)
        os.kill(writer.pid, signal.SIGKILL)
        writer.communicate(timeout=30)
    finally:
        if writer.poll() is None:
            os.kill(writer.pid, signal.SIGKILL)
            writer.wait(timeout=30)
    assert writer.returncode == -signal.SIGKILL
    wal = pathlib.Path(f"{db}-wal")
    shm = pathlib.Path(f"{db}-shm")
    wal_bytes = wal.read_bytes()
    shm_bytes = shm.read_bytes()
    assert wal_bytes and shm_bytes
    return wal_bytes, shm_bytes


def _strand_incoherent_wal_index_family(
    db: pathlib.Path, tmp_path: pathlib.Path
) -> dict:
    """Build a valid WAL generation beside a stale SHM generation.

    The child creates and strands a real WAL/SHM pair. The parent then changes
    only bounded SHM structural metadata to reproduce the production
    split-generation shape without retaining any incident payload.
    """
    ready = tmp_path / "wal-index.ready"
    script = """
import os, pathlib, signal, sqlite3, sys
db, ready = sys.argv[1:]
conn = sqlite3.connect(db, timeout=15)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE issue514_generation (value TEXT NOT NULL)")
conn.commit()
assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
conn.execute("INSERT INTO issue514_generation VALUES ('current')")
conn.commit()
pathlib.Path(ready).write_text("ready\\n")
os.kill(os.getpid(), signal.SIGSTOP)
"""
    writer = subprocess.Popen(
        [sys.executable, "-c", script, str(db), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _await_marker(ready, writer)
        os.kill(writer.pid, signal.SIGKILL)
        _stdout, stderr = writer.communicate(timeout=30)
    finally:
        if writer.poll() is None:
            os.kill(writer.pid, signal.SIGKILL)
            writer.wait(timeout=30)
    assert writer.returncode == -signal.SIGKILL, stderr

    wal = pathlib.Path(f"{db}-wal")
    shm = pathlib.Path(f"{db}-shm")
    current = _test_wal_index_snapshot(db)
    stale_bytes = bytearray(shm.read_bytes())
    # Make the stale generation identity different in both header copies; this
    # is a bounded structural fixture, not a recoverable SHM.
    stale_bytes[32] ^= 0x01
    stale_bytes[80] ^= 0x01
    shm.write_bytes(stale_bytes)
    stale = _test_wal_index_snapshot(db)
    assert current["walSalt"] == current["shmSalt"]
    assert stale["walSalt"] != stale["shmSalt"]

    # The production incident also carried stale aPgno[] entries. Make that
    # mismatch explicit without invalidating the older SHM header checksum:
    # aPgno[] is outside the checksummed 48-byte header copies.
    native = "<" if sys.byteorder == "little" else ">"
    wal_page = struct.unpack_from(">I", wal.read_bytes(), 32)[0]
    with shm.open("r+b") as handle:
        handle.seek(136)
        handle.write(struct.pack(f"{native}I", wal_page + 1))
    stale = _test_wal_index_snapshot(db)
    assert stale["mappingMismatchCount"] >= 1
    return stale


def _destroy_header_magic(db: pathlib.Path) -> None:
    """Make every open of the family fail with a CLASSIFIED corruption error."""
    with db.open("r+b") as handle:
        handle.write(b"not a database\x00")


def _page_size_of(db: pathlib.Path) -> int:
    raw = int.from_bytes(db.read_bytes()[16:18], "big")
    return 65536 if raw == 1 else raw


def _zero_main_data_pages(db: pathlib.Path) -> None:
    """Damage every page but page 1.

    The schema then still parses while every table b-tree read raises, which is
    the damage shape `PRAGMA quick_check` detects and the cheap open-time probe
    does not. Page 1 is left intact deliberately: the cutover still has to
    checkpoint the old family, and the file header is where
    `preservedUserVersion` lives.
    """
    page_size = _page_size_of(db)
    total = db.stat().st_size // page_size
    with db.open("r+b") as handle:
        for page in range(2, total + 1):
            handle.seek((page - 1) * page_size)
            handle.write(b"\x00" * page_size)


def _clobber_table_root_page(db: pathlib.Path, table: str) -> int:
    """Invalidate one named table's root page, leaving the schema readable.

    This is the production shape: the retained corpus implicates
    `quota_projection_state` and its automatic index almost every time, not the
    whole file.
    """
    conn = sqlite3.connect(str(db))
    try:
        root = int(
            conn.execute(
                "SELECT rootpage FROM sqlite_schema WHERE name = ?", (table,)
            ).fetchone()[0]
        )
    finally:
        conn.close()
    page_size = _page_size_of(db)
    with db.open("r+b") as handle:
        handle.seek((root - 1) * page_size)
        handle.write(b"\x00" * page_size)
    return root


def _forensics_bundles(log_dir: pathlib.Path) -> list:
    if not log_dir.exists():
        return []
    return sorted(
        p for p in log_dir.iterdir()
        if p.is_file() and "corruption-forensics" in p.name
    )


def _evidence_dirs(log_dir: pathlib.Path) -> list:
    if not log_dir.exists():
        return []
    return sorted(
        p for p in log_dir.iterdir()
        if p.is_dir() and "corruption-forensics" in p.name
    )


def _latest_bundle(log_dir: pathlib.Path) -> dict:
    bundles = _forensics_bundles(log_dir)
    assert bundles, "no forensics bundle was written"
    return json.loads(bundles[-1].read_text())


# --- #496 S3 §6: the heal is DETACHED ------------------------------------
#
# These are S1 regressions about what the HOOK captures and what the REBUILD
# produces. Both halves still happen, in two processes instead of one, so the
# helpers below drive both and each test keeps its own assertions.


def _stats_heal_marker(app_dir):
    return pathlib.Path(app_dir) / "stats-corruption-heal.pending"


def _run_heal_worker():
    import types
    import _cctally_store

    assert _cctally_store.cmd_stats_corruption_heal_internal(
        types.SimpleNamespace()
    ) == 0


def _defer_without_spawning(call):
    """Run ``call``, requiring it to defer, without launching a real process."""
    import _cctally_db
    import _cctally_update

    prior = _cctally_update._spawn_detached
    _cctally_update._spawn_detached = lambda _command: True
    try:
        call()
    except _cctally_db.StatsHealDeferred:
        return
    finally:
        _cctally_update._spawn_detached = prior
    raise AssertionError("the corruption heal did not defer")


def _post_query_heal_then_worker(exc) -> bool:
    import _cctally_tui

    _defer_without_spawning(
        lambda: _cctally_tui._tui_heal_post_query_stats(exc)
    )
    _run_heal_worker()
    return True


def _open_db_through_the_deferred_heal():
    import _cctally_core

    _defer_without_spawning(_cctally_core.open_db)
    _run_heal_worker()
    return _cctally_core.open_db()


# ==========================================================================
# F2 — forensics-time WAL capture
# ==========================================================================

def test_forensics_captures_the_wal_bytes_present_when_the_damage_was_found(
    ns, tmp_path
):
    """The capture is driven through the dashboard/TUI post-query heal.

    That entry point is used rather than an ordinary `open_db` heal for a
    measured reason. `open_db` detects corruption on its own READ-WRITE
    connection and closes it before the heal hook runs; that close is the last
    close of the family, and SQLite removes the WAL as part of it, so nothing
    downstream can capture bytes that no longer exist. The post-query path
    reaches the hook with no read-write handle ever having been opened, and
    both the heal's locked re-check and the bundle's own integrity probe open
    read-only, which cannot checkpoint. The residual gap in the `open_db` path
    is recorded in the spec rather than closed here: closing it means capturing
    at the `open_db` corruption boundary, which is a later session's surface.
    """
    import _cctally_core
    import _cctally_tui

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    wal_bytes, shm_bytes = _strand_committed_wal_family(db, tmp_path)
    _zero_main_data_pages(db)
    assert pathlib.Path(f"{db}-wal").stat().st_size == len(wal_bytes)

    assert _post_query_heal_then_worker(
        sqlite3.DatabaseError("database disk image is malformed")
    ) is True

    conn = _cctally_core.open_db()
    try:
        rows = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [7.0], "the heal must republish a usable index"

    evidence = _evidence_dirs(_cctally_core.LOG_DIR)
    assert len(evidence) == 1, f"expected one evidence directory, got {evidence!r}"
    assert (evidence[0] / "stats.db-wal").read_bytes() == wal_bytes
    assert len((evidence[0] / "stats.db-shm").read_bytes()) == len(shm_bytes)
    assert not (evidence[0] / "stats.db").exists(), (
        "the main file is deliberately not copied at forensics time"
    )

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    assert bundle["walEvidence"]["disposition"] == "captured"
    assert bundle["walEvidence"]["path"] == str(evidence[0])
    assert bundle["walEvidence"]["bytes"]["stats.db-wal"] == len(wal_bytes)


def test_an_empty_wal_records_skipped_empty_and_writes_no_evidence_file(ns):
    import _cctally_core

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    # `run_stats_ingest` closed the family cleanly, so SQLite already folded
    # and removed the WAL — the routine corruption-heal shape.
    assert not pathlib.Path(f"{db}-wal").exists()
    _destroy_header_magic(db)

    healed = _open_db_through_the_deferred_heal()
    healed.close()

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    assert bundle["walEvidence"]["disposition"] == "skipped_empty"
    assert bundle["walEvidence"]["path"] is None
    assert _evidence_dirs(_cctally_core.LOG_DIR) == [], (
        "an empty WAL must not leave a zero-byte file pretending to be evidence"
    )


def test_operator_rebuild_on_a_healthy_index_records_skipped_not_corruption(ns):
    import _cctally_core

    _seed_live_index()
    rc = ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=False))
    assert rc == 0

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    assert bundle["walEvidence"]["disposition"] == "skipped_not_corruption"
    assert _evidence_dirs(_cctally_core.LOG_DIR) == []


# ==========================================================================
# F8 — the damage description, and its inability to break a heal
# ==========================================================================

def test_forensics_bundle_carries_a_structured_damage_description(ns):
    import _cctally_core

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _destroy_header_magic(db)

    healed = _open_db_through_the_deferred_heal()
    healed.close()

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    damage = bundle["damage"]
    assert damage["schemaVersion"] == 1
    assert damage["method"] in ("integrity_rows", "raw_scan", "both", "unavailable")
    assert isinstance(damage["shapeToken"], str) and damage["shapeToken"]


def test_forensics_classifies_a_split_wal_index_generation(ns, tmp_path):
    """The bundle must make the production mechanism queryable without a
    one-off parser, while retaining only bounded structural metadata."""
    import _cctally_core
    import _cctally_db

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    raw = _strand_incoherent_wal_index_family(db, tmp_path)

    bundle_path = _cctally_db.write_corruption_forensics(
        db,
        trigger_origin="test.issue514",
        trigger_exception=sqlite3.DatabaseError(
            "database disk image is malformed"
        ),
    )
    bundle = json.loads(pathlib.Path(bundle_path).read_text())
    evidence = bundle["walIndexEvidence"]

    assert bundle["sqliteRuntimeVersion"] == sqlite3.sqlite_version
    assert evidence["schemaVersion"] == 1
    assert evidence["verdict"] == "wal_index_generation_mismatch"
    assert evidence["captureStable"] is True
    assert evidence["wal"]["saltHex"] == raw["walSalt"]
    assert evidence["shm"]["saltHex"] == raw["shmSalt"]
    assert evidence["wal"]["pageSize"] == raw["pageSize"]
    assert evidence["shm"]["mxFrame"] == raw["mxFrame"]
    assert evidence["shm"]["nPage"] == raw["nPage"]
    assert evidence["shm"]["nBackfill"] == raw["nBackfill"]
    assert evidence["shm"]["nBackfillAttempted"] == raw[
        "nBackfillAttempted"
    ]
    mapping = evidence["frameMapping"]
    assert mapping["mismatchCount"] >= 1
    assert mapping["comparedCount"] >= mapping["mismatchCount"]
    assert 1 <= len(mapping["mismatchSample"]) <= 8
    assert mapping["mismatchSample"][0] == raw["mappingMismatchSample"][0]
    for member in (evidence["wal"], evidence["shm"]):
        assert isinstance(member["inode"], int)
        assert member["sizeBytes"] > 0
        assert member["mtimeNs"] > 0


def test_wal_index_parser_excludes_stale_tail_frames_from_mapping_count(tmp_path):
    """Only the current WAL generation through its last commit is comparable.

    The production change this catches is treating stale physical tail bytes as
    committed frames merely because a stale SHM advertises a larger mxFrame.
    """
    import _lib_stats_wal

    db = tmp_path / "stats.db"
    db.write_bytes(b"fixture")
    page_size = 512
    current_salt = bytes.fromhex("0102030405060708")
    stale_salt = bytes.fromhex("1112131415161718")
    header = bytearray(32)
    struct.pack_into(">I", header, 0, 0x377F0682)
    struct.pack_into(">I", header, 8, page_size)
    header[16:24] = current_salt

    def frame(page, committed_pages, salt):
        raw = bytearray(24 + page_size)
        struct.pack_into(">II", raw, 0, page, committed_pages)
        raw[8:16] = salt
        return raw

    pathlib.Path(f"{db}-wal").write_bytes(
        header
        + frame(7, 10, current_salt)
        + frame(99, 20, stale_salt)
    )
    shm = bytearray(32768)
    native = "<" if sys.byteorder == "little" else ">"
    struct.pack_into(f"{native}H", shm, 14, page_size)
    struct.pack_into(f"{native}I", shm, 16, 1)
    struct.pack_into(f"{native}I", shm, 20, 10)
    shm[32:40] = current_salt
    shm[48:96] = shm[:48]
    struct.pack_into(f"{native}I", shm, 136, 7)
    struct.pack_into(f"{native}I", shm, 140, 42)
    pathlib.Path(f"{db}-shm").write_bytes(shm)

    evidence = _lib_stats_wal.inspect_wal_index_family(db)

    assert evidence["wal"]["physicalFrameCount"] == 2
    assert evidence["wal"]["currentGenerationCommitFrameCount"] == 1
    assert evidence["frameMapping"]["comparedCount"] == 1
    assert evidence["frameMapping"]["mismatchCount"] == 0
    assert evidence["verdict"] == "coherent"


def test_real_restart_stale_tail_is_coherent(tmp_path):
    """A RESTART reuses frame 1 and leaves ordinary old-salt tail bytes."""
    import _lib_stats_wal

    db = tmp_path / "stats.db"
    conn = sqlite3.connect(str(db), timeout=15)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE payloads (value BLOB NOT NULL)")
        conn.executemany(
            "INSERT INTO payloads VALUES (zeroblob(4000))",
            [()] * 128,
        )
        conn.commit()
        assert conn.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()[0] == 0
        conn.execute("INSERT INTO payloads VALUES (zeroblob(4000))")
        conn.commit()

        evidence = _lib_stats_wal.inspect_wal_index_family(db)
    finally:
        conn.close()

    assert evidence["wal"]["physicalFrameCount"] > evidence["shm"]["mxFrame"]
    assert evidence["wal"]["stalePhysicalTailFrameCount"] > 0
    assert evidence["frameMapping"]["mismatchCount"] == 0
    assert evidence["verdict"] == "coherent"


def test_lower_shm_mxframe_and_disagreeing_header_copy_are_incoherent(
    tmp_path,
):
    import _lib_stats_wal

    db = tmp_path / "stats.db"
    db.write_bytes(b"fixture")
    page_size = 512
    salt = bytes.fromhex("0102030405060708")
    wal = bytearray(32 + 2 * (24 + page_size))
    struct.pack_into(">II", wal, 0, 0x377F0682, 3007000)
    struct.pack_into(">I", wal, 8, page_size)
    wal[16:24] = salt
    for index, page in enumerate((7, 8)):
        offset = 32 + index * (24 + page_size)
        struct.pack_into(">II", wal, offset, page, 10)
        wal[offset + 8:offset + 16] = salt
    pathlib.Path(f"{db}-wal").write_bytes(wal)

    native = "<" if sys.byteorder == "little" else ">"
    shm = bytearray(32768)
    struct.pack_into(f"{native}H", shm, 14, page_size)
    struct.pack_into(f"{native}I", shm, 16, 1)
    struct.pack_into(f"{native}I", shm, 20, 10)
    shm[32:40] = salt
    shm[48:96] = shm[:48]
    struct.pack_into(f"{native}I", shm, 136, 7)
    pathlib.Path(f"{db}-shm").write_bytes(shm)

    lower_mx = _lib_stats_wal.inspect_wal_index_family(db)
    assert lower_mx["verdict"] == "wal_index_mapping_mismatch"

    shm[48] ^= 0x01
    pathlib.Path(f"{db}-shm").write_bytes(shm)
    split_copies = _lib_stats_wal.inspect_wal_index_family(db)
    assert split_copies["shm"]["headerCopiesMatch"] is False
    assert split_copies["verdict"] == "wal_index_mapping_mismatch"


@pytest.mark.parametrize(
    ("wal_bytes", "shm_bytes"),
    [
        (b"short", bytes(32768)),
        (bytes(32), bytes(32768)),
    ],
    ids=("truncated-header", "invalid-header"),
)
def test_unproven_wal_shape_is_preserved_without_sqlite_open(
    tmp_path, monkeypatch, wal_bytes, shm_bytes
):
    import _lib_stats_wal

    db = tmp_path / "stats.db"
    db.write_bytes(b"main-family-byte-proof")
    pathlib.Path(f"{db}-wal").write_bytes(wal_bytes)
    pathlib.Path(f"{db}-shm").write_bytes(shm_bytes)
    paths = [db, pathlib.Path(f"{db}-wal"), pathlib.Path(f"{db}-shm")]
    before = {path.name: path.read_bytes() for path in paths}

    def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("unproven WAL-index shape reached sqlite3.connect")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    evidence = _lib_stats_wal.inspect_wal_index_family(db)

    assert evidence["verdict"] not in {"coherent", "wal_absent", "wal_empty"}
    assert {path.name: path.read_bytes() for path in paths} == before


def test_oversized_wal_analysis_is_positionally_bounded(tmp_path, monkeypatch):
    import _lib_stats_wal

    db = tmp_path / "stats.db"
    db.write_bytes(b"fixture")
    page_size = 512
    salt = bytes.fromhex("0102030405060708")
    cap = _lib_stats_wal._FRAME_ANALYSIS_MAX
    wal_path = pathlib.Path(f"{db}-wal")
    with wal_path.open("wb") as handle:
        header = bytearray(32)
        struct.pack_into(">I", header, 0, 0x377F0682)
        struct.pack_into(">I", header, 8, page_size)
        header[16:24] = salt
        handle.write(header)
        for index in range(cap):
            frame = bytearray(24)
            struct.pack_into(">II", frame, 0, index + 1, cap + 1)
            frame[8:16] = salt
            handle.seek(32 + index * (24 + page_size))
            handle.write(frame)
        handle.truncate(32 + (cap * 8) * (24 + page_size))

    native = "<" if sys.byteorder == "little" else ">"
    shm = bytearray(5 * 32768)
    struct.pack_into(f"{native}H", shm, 14, page_size)
    struct.pack_into(f"{native}I", shm, 16, cap + 1)
    struct.pack_into(f"{native}I", shm, 20, cap + 1)
    shm[32:40] = salt
    shm[48:96] = shm[:48]
    for frame_number in range(1, cap + 1):
        index = frame_number - 1
        if index < 4062:
            offset = 136 + index * 4
        else:
            index -= 4062
            region = 1 + index // 4096
            offset = region * 32768 + (index % 4096) * 4
        struct.pack_into(f"{native}I", shm, offset, frame_number)
    pathlib.Path(f"{db}-shm").write_bytes(shm)

    read_bytes = 0
    original = _lib_stats_wal._read_at

    def counted(handle, offset, size):
        nonlocal read_bytes
        raw = original(handle, offset, size)
        read_bytes += len(raw)
        return raw

    monkeypatch.setattr(_lib_stats_wal, "_read_at", counted)
    evidence = _lib_stats_wal.inspect_wal_index_family(db)

    assert evidence["verdict"] == "analysis_truncated"
    assert evidence["frameMapping"]["truncated"] is True
    assert read_bytes < 1024 * 1024


def test_zero_wal_fast_path_rejects_a_refill_before_open(tmp_path, monkeypatch):
    """The opened descriptor, not an earlier pathname stat, owns the verdict."""
    import _lib_stats_wal

    db = tmp_path / "stats.db"
    db.write_bytes(b"fixture")
    wal = pathlib.Path(f"{db}-wal")
    shm = pathlib.Path(f"{db}-shm")
    wal.write_bytes(bytes(32))
    shm.write_bytes(bytes(32768))
    original_stat = pathlib.Path.stat
    first_wal_stat = True

    class StaleZeroStat:
        st_size = 0

    def stale_once(path, *args, **kwargs):
        nonlocal first_wal_stat
        if path == wal and first_wal_stat:
            first_wal_stat = False
            return StaleZeroStat()
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", stale_once)
    evidence = _lib_stats_wal.inspect_wal_index_family(db)

    assert evidence["verdict"] == "capture_raced"
    assert evidence["captureStable"] is False
    assert evidence["wal"]["sizeBytes"] == 32


def test_incoherent_wal_index_is_preserved_without_checkpoint(ns, tmp_path):
    """A fallback checkpoint must not consume a stale SHM page map.

    The production change this catches is opening/checkpointing the old family
    before classifying its raw WAL/SHM generation under the cutover locks.
    """
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _strand_incoherent_wal_index_family(db, tmp_path)
    paths = [db, pathlib.Path(f"{db}-wal"), pathlib.Path(f"{db}-shm")]
    before = {path.name: path.read_bytes() for path in paths}
    incident = jr._preserve_stats_family_for_cutover(
        db,
        context=jr.RebuildContext(
            trigger="test-fixture",
            record_path=str(tmp_path / "rebuild-record.json"),
        ),
    )
    for path in paths:
        assert (incident / path.name).read_bytes() == before[path.name]

    after = {
        path.name: path.read_bytes() if path.exists() else None for path in paths
    }
    assert after == {path.name: None for path in paths}
    manifest = json.loads((incident / "manifest.json").read_text())
    assert manifest["sqliteRuntimeVersion"] == sqlite3.sqlite_version
    assert manifest["cutoverProtocol"] == "cold-quarantine-then-replace-v2"
    assert manifest["damage"]["checkpointOutcome"] == (
        "not_applicable_cold_quarantine"
    )


def test_real_rebuild_records_incoherent_fallback_without_checkpoint(
    ns, tmp_path, monkeypatch
):
    """Exercise the complete fallback, record, replace, and validation path."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _strand_incoherent_wal_index_family(db, tmp_path)
    family = [db, pathlib.Path(f"{db}-wal"), pathlib.Path(f"{db}-shm")]
    before = {path.name: path.read_bytes() for path in family}
    monkeypatch.setattr(
        jr, "_publish_stats_index_in_place", lambda **_kwargs: jr._FALL_BACK
    )
    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture")
    )

    assert result.quarantine_dir is not None
    for path in family:
        assert (result.quarantine_dir / path.name).read_bytes() == before[path.name]
    record = _latest_record(_cctally_core.LOG_DIR)
    assert record["status"] == "ok"
    assert record["publicationMechanism"] == "replace"
    assert record["damageShapeTokens"]["checkpointOutcome"] == (
        "not_applicable_cold_quarantine"
    )
    conn = sqlite3.connect(str(db), timeout=15)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"
        ).fetchall() == [(7.0,)]
    finally:
        conn.close()


def test_inconclusive_legacy_wal_evidence_refuses_replacement(
    ns, tmp_path, monkeypatch
):
    """An inspection limit is not proof that a healthy main is corrupt."""
    import _cctally_core
    import _cctally_journal as jr
    import _lib_stats_wal

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    wal = pathlib.Path(f"{db}-wal")
    shm = pathlib.Path(f"{db}-shm")
    wal.write_bytes(b"legacy-wal-evidence")
    shm.write_bytes(b"legacy-shm-evidence")
    before = {path.name: path.read_bytes() for path in (db, wal, shm)}
    monkeypatch.setattr(
        _lib_stats_wal,
        "inspect_wal_index_family",
        lambda _path: {
            "schemaVersion": 1,
            "verdict": "analysis_truncated",
            "captureStable": True,
            "reason": None,
        },
    )

    with pytest.raises(jr.JournalError, match="could not prove.*safe"):
        jr.rebuild_stats_index(
            context=jr.RebuildContext(trigger="test-fixture")
        )

    assert {path.name: path.read_bytes() for path in (db, wal, shm)} == before
    quarantine = _cctally_core.APP_DIR / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == []


def test_a_failing_damage_scan_never_breaks_the_heal(ns, monkeypatch):
    import _cctally_core
    import _lib_stats_damage

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _destroy_header_magic(db)

    def _boom(**_kwargs):
        raise RuntimeError("injected characterization failure")

    monkeypatch.setattr(_lib_stats_damage, "describe_damage", _boom)

    healed = _open_db_through_the_deferred_heal()
    try:
        rows = healed.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"
        ).fetchall()
    finally:
        healed.close()
    # Assert on the heal's own success, not merely on the absence of an
    # exception: the rebuilt index must carry the journal-covered fact.
    assert [r[0] for r in rows] == [7.0]
    incidents = sorted((_cctally_core.APP_DIR / "quarantine").iterdir())
    assert len(incidents) == 1

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    assert bundle["damage"]["method"] == "unavailable"
    assert "injected characterization failure" in bundle["damage"]["reason"]


def test_damage_description_names_the_implicated_object(ns, tmp_path):
    """F8's core claim, proven non-vacuously.

    The raw scan must name the object whose root page is damaged. In 54 of the
    74 retained production bundles `PRAGMA integrity_check` raised and produced
    no rows at all, so this finding cannot come from the pragma.
    """
    import _cctally_core
    import _cctally_tui

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _clobber_table_root_page(db, "quota_projection_state")

    assert _post_query_heal_then_worker(
        sqlite3.DatabaseError("database disk image is malformed")
    ) is True

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    damage = bundle["damage"]
    assert damage["method"] in ("raw_scan", "both")
    scanned = [f for f in damage["findings"] if f["kind"] == "bad_root_page_type"]
    assert [f["table"] for f in scanned] == ["quota_projection_state"]
    assert damage["shapeToken"] != "none"


# ==========================================================================
# F8 — three scan points, placed so they bracket the explicit checkpoint
# ==========================================================================

def _incident_manifest(app_dir: pathlib.Path) -> dict:
    incidents = sorted((app_dir / "quarantine").iterdir())
    assert len(incidents) == 1, f"expected one incident, got {incidents!r}"
    return json.loads((incidents[0] / "manifest.json").read_text())


def test_corrupt_old_family_records_cold_quarantine(ns):
    import _cctally_core

    _seed_live_index()
    _destroy_header_magic(pathlib.Path(_cctally_core.DB_PATH))

    healed = _open_db_through_the_deferred_heal()
    healed.close()

    damage = _incident_manifest(_cctally_core.APP_DIR)["damage"]
    assert damage["checkpointOutcome"] == "not_applicable_cold_quarantine"
    assert damage["preserved"]["schemaVersion"] == 1
    assert damage["postCheckpoint"] is None


def _fail_in_place_pre_commit(monkeypatch):
    """Force the physical fallback while leaving the destination READABLE.

    #496 S3 publishes a readable destination in place and never preserves, so
    the preservation path is now reached either by a destination SQLite cannot
    open — which is never a healthy family — or by an in-place attempt that
    rolled back on a structural error. Only the second keeps a healthy old
    family, which is what a test about checkpointing one needs.
    """
    import _cctally_journal as jr
    import _lib_stats_publish as sp

    def stub(conn, scratch, **kwargs):
        exc = sqlite3.DatabaseError("database disk image is malformed")
        setattr(exc, "_cctally_publication_phase", sp.PRE_COMMIT)
        raise exc

    monkeypatch.setattr(jr, "_publish_generation_in_place", stub)


def test_healthy_old_family_is_not_replaced_after_precommit_failure(
    ns, monkeypatch,
):
    import _cctally_core

    _seed_live_index()
    _fail_in_place_pre_commit(monkeypatch)
    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=False)) == 3
    quarantine = _cctally_core.APP_DIR / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == []


def test_cold_quarantine_scan_names_the_damaged_object(ns):
    """The retained artifact is described, not just the live one.

    The preserved copy is taken BEFORE the explicit checkpoint and the
    post-checkpoint scan after it, so the pair shows what that checkpoint did.
    """
    import _cctally_core
    import _cctally_tui

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _clobber_table_root_page(db, "quota_projection_state")

    assert _post_query_heal_then_worker(
        sqlite3.DatabaseError("database disk image is malformed")
    ) is True

    damage = _incident_manifest(_cctally_core.APP_DIR)["damage"]
    assert damage["checkpointOutcome"] == "not_applicable_cold_quarantine"
    named = [
        finding
        for finding in damage["preserved"]["findings"]
        if finding["kind"] == "bad_root_page_type"
    ]
    assert [finding["table"] for finding in named] == ["quota_projection_state"]
    assert damage["postCheckpoint"] is None


# ==========================================================================
# F1 — fresh-connection validation at both points
# ==========================================================================

def _main_file_of(conn) -> pathlib.Path:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return pathlib.Path(row[2])
    raise AssertionError("connection has no main database")


def _publication_marker(app_dir: pathlib.Path) -> pathlib.Path:
    return app_dir / "stats.db.publication"


def _rebuild_records(log_dir: pathlib.Path) -> list:
    if not log_dir.exists():
        return []
    return sorted(log_dir.glob("stats-rebuild-*.json"))


def _latest_record(log_dir: pathlib.Path) -> dict:
    records = _rebuild_records(log_dir)
    assert records, "no rebuild record was written"
    return json.loads(records[-1].read_text())


def test_the_publication_stamp_table_is_part_of_the_epoch_contract(ns):
    """#496 S3: the stamp is written INSIDE the publication transaction, so it
    has to be a real table in the epoch's schema.

    `_validate_rebuilt_stats_index` enforces an exact user-table set against a
    separately maintained required list, so the table has to be added to that
    list and to the fingerprint together; a stats schema change is an epoch
    bump, never a migration.
    """
    import _cctally_core
    import _cctally_journal as jr

    assert _cctally_core.STATS_INDEX_EPOCH == 1010
    assert "stats_publication_stamp" in jr._REBUILD_REQUIRED_TABLES

    _seed_live_index()
    conn = _cctally_core.open_db()
    try:
        names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert "stats_publication_stamp" in names
        columns = [
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(stats_publication_stamp)"
            )
        ]
        assert columns == ["record_path", "started_at_utc", "stamped_at_utc"]
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 1010
    finally:
        conn.close()


def test_a_rebuild_still_validates_with_the_stamp_table_present(ns):
    """A table missing from `_REBUILD_REQUIRED_TABLES` is reported as
    `unexpected` and refuses the rebuild, so this fails loudly rather than
    subtly if the contract and the schema drift apart."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))

    conn = sqlite3.connect(f"file:{_cctally_core.DB_PATH}?mode=ro", uri=True)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'stats_publication_stamp'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_a_scratch_damaged_after_the_build_is_refused_before_publication(
    ns, monkeypatch,
):
    """The pre-publication check reads the bytes that will actually be
    published, on a connection that never saw them being written."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    before = pathlib.Path(_cctally_core.DB_PATH).read_bytes()

    real_assert = jr._assert_stats_wal_sidecars_absent
    calls = 0

    def assert_then_damage(path, *, phase):
        nonlocal calls
        real_assert(path, phase=phase)
        calls += 1
        candidate = pathlib.Path(path)
        if calls == 1 and ".rebuilding-" in candidate.name:
            with candidate.open("r+b") as handle:
                handle.write(b"not a database\x00")

    monkeypatch.setattr(jr, "_assert_stats_wal_sidecars_absent", assert_then_damage)
    with pytest.raises(Exception):
        jr.rebuild_stats_index(
            context=jr.RebuildContext(trigger="test-fixture")
        )

    assert pathlib.Path(_cctally_core.DB_PATH).read_bytes() == before, (
        "a pre-publication refusal must leave the old family live"
    )
    quarantine = _cctally_core.APP_DIR / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == [], (
        "a pre-publication refusal must not create a quarantine incident"
    )
    assert not _publication_marker(_cctally_core.APP_DIR).exists()


def test_a_successful_rebuild_records_both_verdicts_and_leaves_no_marker(ns):
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))

    record = _latest_record(_cctally_core.LOG_DIR)
    assert record["schemaVersion"] == 1
    assert record["status"] == "ok"
    assert record["trigger"] == "db-rebuild"
    assert record["binaryEpoch"] == _cctally_core.STATS_INDEX_EPOCH
    assert record["sqliteRuntimeVersion"] == sqlite3.sqlite_version
    assert record["prePublicationValidation"] == {"ok": True, "error": None}
    assert record["postPublicationValidation"] == {"ok": True, "error": None}
    # #496 S3: an in-place publish never preserves, because preservation is a
    # consequence of destroying a file and nothing is destroyed.
    assert record["publicationMechanism"] == "in_place"
    assert record["incidentPath"] is None
    assert not _publication_marker(_cctally_core.APP_DIR).exists()
    quarantine = _cctally_core.APP_DIR / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == []


def test_a_published_family_uses_rollback_journaling_without_wal(ns):
    """A published stats generation is complete in the main file with no WAL."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    db = pathlib.Path(_cctally_core.DB_PATH)
    assert db.exists()
    assert not pathlib.Path(f"{db}-wal").exists()
    assert not pathlib.Path(f"{db}-shm").exists()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_idle_legacy_wal_holder_forces_restart_before_transition(
    ns, tmp_path
):
    """A zero WAL is not permission to convert an idle holder's generation."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    ready = tmp_path / "idle-holder.ready"
    holder_script = """
import pathlib, sqlite3, sys, time
bin_dir, db, ready = sys.argv[1:]
sys.path.insert(0, bin_dir)
from _lib_dashboard_sources import codex_stats_digest
conn = sqlite3.connect(db, timeout=15)
conn.execute("PRAGMA journal_mode=WAL")
digest = codex_stats_digest(conn)
pathlib.Path(ready).write_text(digest + "\\n")
try:
    while True:
        time.sleep(0.1)
finally:
    conn.close()
    """
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(BIN), str(db), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _await_marker(ready, holder)
        checkpoint = sqlite3.connect(str(db), timeout=15)
        try:
            assert checkpoint.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()[0] == 0
        finally:
            checkpoint.close()

        wal = pathlib.Path(f"{db}-wal")
        shm = pathlib.Path(f"{db}-shm")
        assert wal.exists() and wal.stat().st_size == 0
        assert shm.exists() and shm.stat().st_size >= 32768
        before = {"wal": wal.stat().st_ino, "shm": shm.stat().st_ino}

        with pytest.raises(jr.JournalError, match="restart the dashboard"):
            jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="test-fixture")
            )
        after_refusal = {
            "wal": wal.stat().st_ino if wal.exists() else None,
            "shm": shm.stat().st_ino if shm.exists() else None,
        }
        assert after_refusal == before
    finally:
        holder.terminate()
        try:
            holder.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.communicate(timeout=10)

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    probe = sqlite3.connect(str(db), timeout=15)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert probe.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        probe.close()
    assert not wal.exists()
    assert not shm.exists()


def test_a_failed_post_publication_verdict_is_durable_and_refuses_the_next_open(
    ns,
):
    """The verdict has to outlive the process that reached it.

    After `os.replace` no scratch remains, so interrupted recovery cannot fire,
    and an epoch-current index returns from `open_db` without validation. A
    raise alone would therefore leave a known-bad index accepted by every later
    command.
    """
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    real_validate = jr._validate_rebuilt_stats_index

    def fail_on_the_destination(conn, high_water):
        real_validate(conn, high_water)
        if ".rebuilding-" not in _main_file_of(conn).name:
            raise jr.JournalError("injected post-publication validation failure")

    jr._validate_rebuilt_stats_index = fail_on_the_destination
    try:
        with pytest.raises(jr.JournalError, match="injected post-publication"):
            jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="db-rebuild")
            )
    finally:
        jr._validate_rebuilt_stats_index = real_validate

    marker_path = _publication_marker(_cctally_core.APP_DIR)
    marker = json.loads(marker_path.read_text())
    assert marker["status"] == "failed"
    assert marker["mechanism"] == "in_place"
    record_path = marker["recordPath"]
    record = json.loads(pathlib.Path(record_path).read_text())
    assert record["status"] == "failed"
    assert record["postPublicationValidation"]["ok"] is False
    assert "injected post-publication" in record["postPublicationValidation"]["error"]

    # A REAL second open, not an inspection of the marker.
    import _cctally_db

    with pytest.raises(_cctally_db.StatsPublicationFailedError) as caught:
        _cctally_core.open_db()
    message = str(caught.value)
    assert record_path in message
    # An in-place publication preserves nothing, so the message must not send a
    # user whose index is already known bad to a directory that was never
    # created. It must still say what to do.
    quarantine = pathlib.Path(_cctally_core.APP_DIR) / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == []
    assert "quarantine" not in message, message
    assert "db rebuild --db stats" in message


# ==========================================================================
# F1 — the crash-point map, driven through real process kills
# ==========================================================================

def _isolated_env(tmp_path: pathlib.Path) -> dict:
    data = tmp_path / "data"
    home = tmp_path / "home"
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    for path in (data, home, claude / "projects", codex):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(claude),
            "CODEX_HOME": str(codex),
            "TZ": "Etc/UTC",
        }
    )
    return env


def _record_usage(env: dict) -> subprocess.CompletedProcess:
    now = int(time.time())
    return subprocess.run(
        [
            sys.executable, str(CCTALLY), "record-usage",
            "--percent", "7",
            "--resets-at", str(now + 3 * 86400),
            "--five-hour-percent", "11",
            "--five-hour-resets-at", str(now + 3600),
        ],
        env=env, capture_output=True, text=True, timeout=120,
    )


def _seed_cli(env: dict) -> pathlib.Path:
    result = _record_usage(env)
    assert result.returncode == 0, result.stderr
    db = pathlib.Path(env["CCTALLY_DATA_DIR"]) / "stats.db"
    assert db.exists()
    return db


def _kill_rebuild_at(env: dict, tmp_path: pathlib.Path, point: str) -> None:
    marker = tmp_path / f"{point}.pid"
    child_env = dict(env)
    child_env.update(
        {
            "CCTALLY_TEST_STATS_REBUILD_PAUSE_AT": point,
            "CCTALLY_TEST_STATS_REBUILD_MARKER": str(marker),
        }
    )
    rebuild = subprocess.Popen(
        [sys.executable, str(CCTALLY), "db", "rebuild", "--db", "stats"],
        env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _await_marker(marker, rebuild)
        os.kill(rebuild.pid, signal.SIGKILL)
        rebuild.communicate(timeout=30)
    finally:
        if rebuild.poll() is None:
            os.kill(rebuild.pid, signal.SIGKILL)
            rebuild.wait(timeout=30)
    assert rebuild.returncode == -signal.SIGKILL


def _stamp_rows(db: pathlib.Path) -> list:
    """The record paths the destination's publication stamp names."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT record_path FROM stats_publication_stamp"
            )
        ]
    finally:
        conn.close()


def _desynchronize_the_live_cursor(db: pathlib.Path) -> None:
    """Make the live index disagree with the pinned journal high-water.

    Nothing on the ordinary open path notices, because the file carries the
    current epoch and `open_db`'s zero-DDL fast path returns it unvalidated.
    Only a publication verdict that actually validates the destination catches
    it, so this is what separates "the marker was resolved" from "the marker
    was discarded" observably.
    """
    raw = sqlite3.connect(str(db))
    try:
        raw.execute("UPDATE journal_cursor SET offset = offset + 1 WHERE id = 1")
        raw.commit()
    finally:
        raw.close()


def test_a_kill_between_the_commit_and_the_verdict_is_resolved_by_the_next_open(
    tmp_path,
):
    """#496 S3 §5, crash point "after commit before the verdict".

    An in-place publish leaves its scratch on disk until the verdict settles,
    so BOTH artifacts are present: a `.rebuilding-*` family that artifact-first
    recovery classifies first, and a pending marker whose publication is
    already live. The `scratchPath` proxy reads that state as "this run never
    replaced anything" and discards the marker, which accepts the published
    bytes without ever validating them. The stamp names the marker's record, so
    the verdict is still owed and must be rendered.
    """
    env = _isolated_env(tmp_path)
    db = _seed_cli(env)
    _kill_rebuild_at(env, tmp_path, "rebuild_after_publication_replace")

    scratches = sorted(db.parent.glob("stats.db.rebuilding-*"))
    assert scratches, (
        "an in-place publish does not consume its scratch, so the state this "
        "crash point produces carries both artifacts"
    )
    publication = db.parent / "stats.db.publication"
    state = json.loads(publication.read_text())
    assert state["status"] == "pending"
    assert state["mechanism"] == "in_place"
    record = json.loads(pathlib.Path(state["recordPath"]).read_text())
    assert record["status"] == "pending"
    assert _stamp_rows(db) == [state["recordPath"]], (
        "the publication committed, so its stamp is in the live bytes"
    )

    _desynchronize_the_live_cursor(db)

    result = _record_usage(env)
    assert result.returncode != 0, (
        "the owed verdict must be rendered against the live bytes, not "
        "discarded because a scratch happens to survive an in-place publish"
    )
    resolved = json.loads(publication.read_text())
    assert resolved["status"] == "failed"
    settled = json.loads(pathlib.Path(state["recordPath"]).read_text())
    assert settled["status"] == "failed"
    assert settled["postPublicationValidation"]["ok"] is False


def test_a_kill_between_the_commit_and_the_verdict_clears_a_sound_index(
    tmp_path,
):
    """The same crash point, with the published bytes intact: the verdict is
    rendered, passes, and clears the marker and the spent scratch."""
    env = _isolated_env(tmp_path)
    db = _seed_cli(env)
    _kill_rebuild_at(env, tmp_path, "rebuild_after_publication_replace")

    publication = db.parent / "stats.db.publication"
    assert json.loads(publication.read_text())["status"] == "pending"

    result = _record_usage(env)
    assert result.returncode == 0, result.stderr
    assert not publication.exists(), (
        "the next open must validate the destination and clear the marker"
    )
    assert sorted(db.parent.glob("stats.db.rebuilding-*")) == []
    probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        probe.close()


def test_a_kill_before_replace_lets_interrupted_recovery_win(tmp_path):
    """#496 S3 §5, crash point "after the marker before commit".

    A scratch artifact still takes precedence, and clears the stale marker.
    Both discriminators agree here — the scratch exists AND the stamp does not
    name this record — so this is a regression guard rather than the case that
    separates them.
    """
    env = _isolated_env(tmp_path)
    db = _seed_cli(env)
    _kill_rebuild_at(env, tmp_path, "rebuild_before_cutover")

    assert sorted(db.parent.glob("stats.db.rebuilding-*")), (
        "the kill must leave the scratch artifact this branch is gated on"
    )
    publication = db.parent / "stats.db.publication"
    state = json.loads(publication.read_text())
    assert state["status"] == "pending"
    assert state["mechanism"] == "in_place"
    assert state["recordPath"] not in _stamp_rows(db), (
        "the transaction rolled back, so nothing may name this record"
    )

    result = _record_usage(env)
    assert result.returncode == 0, result.stderr
    assert sorted(db.parent.glob("stats.db.rebuilding-*")) == []
    assert not publication.exists()
    probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        probe.close()


def test_a_kill_before_the_marker_leaves_an_orphan_scratch_only(tmp_path):
    """#496 S3 §5, crash point "before the marker".

    No marker exists, so nothing owes a verdict; the destination is untouched
    and the existing interrupted-rebuild recovery reclaims the scratch.
    """
    env = _isolated_env(tmp_path)
    db = _seed_cli(env)
    _kill_rebuild_at(env, tmp_path, "publication_before_marker")

    assert sorted(db.parent.glob("stats.db.rebuilding-*")), (
        "the kill must leave the scratch this branch is gated on"
    )
    publication = db.parent / "stats.db.publication"
    assert not publication.exists()
    assert _stamp_rows(db) == [], "the destination was never written to"

    result = _record_usage(env)
    assert result.returncode == 0, result.stderr
    assert sorted(db.parent.glob("stats.db.rebuilding-*")) == []
    assert not publication.exists()


def test_a_kill_between_the_commit_and_the_detach_still_owes_its_verdict(
    tmp_path,
):
    """#496 S3 §5, crash point "between commit and detach".

    `DETACH` cannot run inside a transaction, so the scratch is still attached
    when the commit lands. The stamp is already durable at that point, which is
    what makes this indistinguishable from the later crash points to a resolver
    that reads it — and unrecoverable to one that reads `scratchPath`.
    """
    env = _isolated_env(tmp_path)
    db = _seed_cli(env)
    _kill_rebuild_at(env, tmp_path, "publication_after_commit_before_detach")

    publication = db.parent / "stats.db.publication"
    state = json.loads(publication.read_text())
    assert state["status"] == "pending"
    assert _stamp_rows(db) == [state["recordPath"]]
    assert sorted(db.parent.glob("stats.db.rebuilding-*"))

    _desynchronize_the_live_cursor(db)

    result = _record_usage(env)
    assert result.returncode != 0, (
        "a committed publication still owes a verdict on the bytes it installed"
    )
    assert json.loads(publication.read_text())["status"] == "failed"


def test_a_kill_after_the_verdict_before_marker_removal_resolves_the_same_way(
    tmp_path,
):
    """#496 S3 §5, crash point "after the verdict before marker removal".

    The record already says `ok`, but the marker slot has not been cleared, so
    a later opener still finds a pending marker over a committed publication.
    """
    env = _isolated_env(tmp_path)
    db = _seed_cli(env)
    _kill_rebuild_at(
        env, tmp_path, "publication_after_verdict_before_marker_removal"
    )

    publication = db.parent / "stats.db.publication"
    state = json.loads(publication.read_text())
    assert state["status"] == "pending"
    assert _stamp_rows(db) == [state["recordPath"]]
    assert json.loads(pathlib.Path(state["recordPath"]).read_text())[
        "status"
    ] == "ok"

    _desynchronize_the_live_cursor(db)

    result = _record_usage(env)
    assert result.returncode != 0
    assert json.loads(publication.read_text())["status"] == "failed"


def test_a_kill_before_scratch_removal_reclaims_it_without_rebuilding(tmp_path):
    """#496 S3 §5, crash point "after commit before scratch removal".

    The marker is already gone and the verdict is settled, so the surviving
    scratch is spent. Artifact-first recovery must reclaim it and must not
    rebuild a healthy index it happens to sit beside.
    """
    env = _isolated_env(tmp_path)
    db = _seed_cli(env)
    _kill_rebuild_at(env, tmp_path, "publication_before_scratch_removal")

    assert sorted(db.parent.glob("stats.db.rebuilding-*"))
    publication = db.parent / "stats.db.publication"
    assert not publication.exists()
    stamped = _stamp_rows(db)
    assert len(stamped) == 1, "the publication committed and stamped itself"

    log_dir = pathlib.Path(env["CCTALLY_DATA_DIR"]) / "logs"
    before = sorted(log_dir.glob("stats-rebuild-*.json"))

    result = _record_usage(env)
    assert result.returncode == 0, result.stderr
    assert sorted(db.parent.glob("stats.db.rebuilding-*")) == []
    assert sorted(log_dir.glob("stats-rebuild-*.json")) == before, (
        "a spent scratch beside a healthy index must not trigger a rebuild"
    )
    assert _stamp_rows(db) == stamped


# ==========================================================================
# F1 — a scratch from a LATER run must not discard an earlier run's verdict
# ==========================================================================

class _SimulatedCrash(Exception):
    """Stops a rebuild exactly where a SIGKILL would, in-process."""


def _die_at(jr, point: str):
    """Replace the rebuild's pause seam with one that raises at ``point``."""
    real_pause = jr._stats_rebuild_test_pause

    def pause(reached: str) -> None:
        if reached == point:
            raise _SimulatedCrash(reached)
        real_pause(reached)

    return real_pause, pause


def test_a_later_scratch_must_not_discard_an_earlier_pending_verdict(ns):
    """#496 S1 F1. A crashed rebuild releases its flock, so a scratch on disk
    can belong to a LATER run than the pending marker beside it.

    Run A dies between `os.replace` and the verdict, leaving a pending marker
    and no scratch. Run B then dies during its fold, leaving a scratch and no
    incident. Discarding A's marker because B's scratch exists accepts A's
    published index without ever validating it, which is exactly the
    silent-acceptance class the publication transaction exists to close.
    """
    import _cctally_core
    import _cctally_db
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)

    real_pause, dying_pause = _die_at(jr, "rebuild_after_publication_replace")
    jr._stats_rebuild_test_pause = dying_pause
    try:
        with pytest.raises(_SimulatedCrash):
            jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="db-rebuild")
            )
    finally:
        jr._stats_rebuild_test_pause = real_pause

    marker_path = _publication_marker(_cctally_core.APP_DIR)
    run_a = json.loads(marker_path.read_text())
    assert run_a["status"] == "pending"
    # #496 S3: an in-place publish attaches its scratch read-only, so run A's
    # own scratch survives its commit too. Removing it here isolates the
    # property under test — a scratch belonging to a STRICTLY LATER run — from
    # the crash point covered by
    # `test_a_kill_between_the_commit_and_the_verdict_is_resolved_by_the_next_open`.
    for stale in sorted(db.parent.glob("stats.db.rebuilding-*")):
        stale.unlink()

    # What run A published is bad. Nothing on the ordinary open path notices:
    # the file carries the current epoch, so `open_db`'s zero-DDL fast path
    # returns it, and no scratch of run A's own remains.
    raw = sqlite3.connect(str(db))
    try:
        raw.execute("UPDATE journal_cursor SET offset = offset + 1 WHERE id = 1")
        raw.commit()
    finally:
        raw.close()

    # Run B: started later, died during its fold, left only a scratch. The
    # stamp is well past run A's incident so it cannot be read as evidence of
    # the legacy quarantine-before-build shape.
    stamp = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
    ).strftime("%Y%m%dT%H%M%S_%f")
    (db.parent / f"stats.db.rebuilding-{stamp}").write_bytes(b"")

    with pytest.raises(_cctally_db.StatsPublicationFailedError) as caught:
        _cctally_core.open_db()

    resolved = json.loads(marker_path.read_text())
    assert resolved["status"] == "failed"
    assert resolved["recordPath"] in str(caught.value)
    record = json.loads(pathlib.Path(resolved["recordPath"]).read_text())
    assert record["status"] == "failed"
    assert record["postPublicationValidation"]["ok"] is False
    assert sorted(db.parent.glob("stats.db.rebuilding-*")) == [], (
        "the stale scratch is still reclaimed"
    )


def _age_quarantine_incidents(app_dir: pathlib.Path, minutes: int) -> None:
    """Restamp every existing incident directory ``minutes`` into the past.

    `_has_completed_stats_quarantine_incident` matches a scratch to an incident
    only when the scratch was created within five minutes AFTER it, which is
    what separates the legacy quarantine-before-build interruption from a Task A
    orphan. Ageing the incidents states the ordinary case — the next rebuild
    starts well after the previous one finished — without which the recovery
    path would rebuild instead of reaching the marker branch under test.
    """
    stamp = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    ).strftime("%Y%m%dT%H%M%S_%f")
    quarantine = app_dir / "quarantine"
    if not quarantine.is_dir():
        # #496 S3: an in-place publish never preserves, so a run that published
        # into a readable destination leaves no incident to restamp.
        return
    for incident in sorted(quarantine.iterdir()):
        incident.rename(incident.with_name(f"stats.db-{stamp}"))


def test_a_later_publication_must_not_destroy_an_earlier_owed_verdict(ns):
    """#496 S1 F1. A pre-commit failure must restore the carried verdict.

    Run A dies after publishing but before its verdict and the live generation
    is then made invalid. Run B settles that owed verdict, writes its own marker,
    and dies before changing the live database. Its pre-commit cleanup may
    reclaim its scratch, but must restore run A's failed marker rather than
    accepting the invalid generation.
    """
    import _cctally_core
    import _cctally_db
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    marker_path = _publication_marker(_cctally_core.APP_DIR)

    real_pause, dying_pause = _die_at(jr, "rebuild_after_publication_replace")
    jr._stats_rebuild_test_pause = dying_pause
    try:
        with pytest.raises(_SimulatedCrash):
            jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="db-rebuild")
            )
    finally:
        jr._stats_rebuild_test_pause = real_pause

    run_a = json.loads(marker_path.read_text())
    assert run_a["status"] == "pending"
    run_a_record = run_a["recordPath"]
    run_a_scratch = pathlib.Path(run_a["scratchPath"])

    # What run A published is invalid. Nothing on the ordinary open path
    # notices: the file carries the current epoch, so `open_db`'s zero-DDL fast
    # path returns it, and `os.replace` consumed run A's scratch.
    raw = sqlite3.connect(str(db))
    try:
        raw.execute("UPDATE journal_cursor SET offset = offset + 1 WHERE id = 1")
        raw.commit()
    finally:
        raw.close()
    _age_quarantine_incidents(_cctally_core.APP_DIR, 10)

    real_pause, dying_pause = _die_at(jr, "rebuild_before_cutover")
    jr._stats_rebuild_test_pause = dying_pause
    try:
        with pytest.raises(_SimulatedCrash):
            jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="db-rebuild")
            )
    finally:
        jr._stats_rebuild_test_pause = real_pause

    assert sorted(db.parent.glob("stats.db.rebuilding-*")) == [run_a_scratch], (
        "run B's scratch must be reclaimed while run A's evidence remains"
    )
    restored = json.loads(marker_path.read_text())
    assert restored["recordPath"] == run_a_record
    assert restored["status"] == "failed"

    with pytest.raises(_cctally_db.StatsPublicationFailedError) as caught:
        _cctally_core.open_db()
    assert run_a_record in str(caught.value), (
        "the verdict run A owed must survive run B, and must name run A's record"
    )
    record = json.loads(pathlib.Path(run_a_record).read_text())
    assert record["status"] == "failed"
    assert record["postPublicationValidation"]["ok"] is False


# ==========================================================================
# #496 S3 §5 — which discriminator, and the three states it resolves to
# ==========================================================================

def _fake_scratch(db: pathlib.Path) -> pathlib.Path:
    scratch = db.with_name(f"{db.name}.rebuilding-20260805T120000_000000")
    scratch.write_bytes(b"")
    return scratch


def test_an_unreadable_stamp_never_discards_a_pending_in_place_marker(
    ns, monkeypatch
):
    """#496 S3 §5. The three states must stay three.

    Collapsing INDETERMINATE into PROVEN_PREDECESSOR discards a verdict owed on
    bytes that may well be live; collapsing it into MATCH condemns an index on
    no evidence. Only a stamp that was READ and names another record discards.
    """
    import _cctally_core
    import _cctally_journal as jr
    import _cctally_store as st

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    record_path = "/nonexistent/stats-rebuild-run-a.json"
    jr._write_publication_marker(
        db, record_path, started_at="2026-08-05T12:00:00Z",
        scratch_path=str(_fake_scratch(db)), mechanism="in_place",
    )

    monkeypatch.setattr(
        jr, "read_publication_stamp",
        lambda *_a, **_k: sqlite3.DatabaseError(
            "database disk image is malformed"
        ),
    )
    assert st._pending_stats_publication_never_replaced(db) is False, (
        "an unreadable stamp proves nothing and must not discard the marker"
    )

    monkeypatch.setattr(jr, "read_publication_stamp", lambda *_a, **_k: None)
    assert st._pending_stats_publication_never_replaced(db) is True

    monkeypatch.setattr(
        jr, "read_publication_stamp",
        lambda *_a, **_k: [{"record_path": record_path}],
    )
    assert st._pending_stats_publication_never_replaced(db) is False


def test_a_replace_marker_keeps_its_scratch_proxy_when_a_stamp_would_disagree(
    ns,
):
    """The marker STATES its mechanism and the opener selects on it. Neither
    discriminator is generalized over the other: the same marker beside the
    same files resolves opposite ways depending only on which protocol it
    declares."""
    import _cctally_core
    import _cctally_journal as jr
    import _cctally_store as st

    _seed_live_index()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))
    db = pathlib.Path(_cctally_core.DB_PATH)
    stamped = _stamp_rows(db)
    assert len(stamped) == 1, "the in-place publish must have stamped itself"
    scratch = str(_fake_scratch(db))

    jr._write_publication_marker(
        db, stamped[0], started_at="2026-08-05T12:00:00Z",
        scratch_path=scratch,
    )
    assert st._pending_stats_publication_never_replaced(db) is True, (
        "a `replace` marker answers with its scratch, whatever the stamp says"
    )

    jr._write_publication_marker(
        db, stamped[0], started_at="2026-08-05T12:00:00Z",
        scratch_path=scratch, mechanism="in_place",
    )
    assert st._pending_stats_publication_never_replaced(db) is False, (
        "an `in_place` marker answers with the stamp, whatever the scratch says"
    )


def test_a_crash_publishing_into_a_pre_stamp_epoch_discards_the_marker(ns):
    """#496 S3 §5, the upgrade path.

    The first 1008 binary publishes into a readable epoch-1007 index, which has
    no `stats_publication_stamp` table at all. Reading that absence as a failed
    query — INDETERMINATE — would resolve the marker instead of discarding it,
    the validation would fail on the epoch, and an interrupted upgrade rebuild
    would condemn a perfectly healthy index. The destination's epoch settles it
    instead: a committed publication always leaves the current epoch behind, so
    any other epoch proves this publication never committed.
    """
    import _cctally_core
    import _cctally_journal as jr
    import _cctally_store as st

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    raw = sqlite3.connect(str(db))
    try:
        raw.execute("DROP TABLE stats_publication_stamp")
        raw.execute("PRAGMA user_version=1007")
        raw.commit()
    finally:
        raw.close()

    jr._write_publication_marker(
        db, "/nonexistent/stats-rebuild-upgrade.json",
        started_at="2026-08-05T12:00:00Z",
        scratch_path=str(_fake_scratch(db)), mechanism="in_place",
    )
    assert jr.read_publication_stamp(db) is None
    assert st._pending_stats_publication_never_replaced(db) is True


def test_post_publication_validation_verifies_the_publication_identity(ns):
    """#496 S3 §5. The pinned high-water answers "is this a correct
    materialization of the journal prefix", which an equally journal-consistent
    generation installed by some other run also satisfies. The stamp is what
    says these bytes are the ones THIS publication installed."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))
    db = pathlib.Path(_cctally_core.DB_PATH)
    stamped = _stamp_rows(db)
    assert len(stamped) == 1
    high_water = jr.journal_high_water()

    assert jr.validate_published_stats_index(db, high_water) is None
    assert jr.validate_published_stats_index(
        db, high_water, expected_record_path=stamped[0]
    ) is None
    error = jr.validate_published_stats_index(
        db, high_water, expected_record_path="/nonexistent/another-record.json"
    )
    assert error is not None
    assert "PROVEN_PREDECESSOR" in error


# ==========================================================================
# #496 S3 §5 — cross-version pending-marker behaviour (Task 8)
# ==========================================================================

def test_an_old_binary_misreads_an_in_place_marker_but_cannot_act_on_it(
    ns, monkeypatch
):
    """#496 S3 §5, cross-version. The misreading is real; it is also inert.

    A pre-1008 binary does not know the `mechanism` field, so it applies the
    `scratchPath` proxy to a pending in-place publication and concludes "never
    replaced" for one that has already committed. The field cannot be hidden
    from it — an in-place publish genuinely leaves its scratch on disk between
    the commit and the verdict, and the physical fallback still needs
    `scratchPath` — so the marker is not made safe by shaping it differently.

    It is safe for a structural reason instead. A committed in-place
    publication always leaves the destination at the publishing binary's
    `STATS_INDEX_EPOCH`, because every scratch eligible for publication was
    validated at that epoch and the publication transaction stamps it. So the
    only window in which the old proxy answers wrongly is one where the
    destination carries an epoch the old binary refuses to use: it rebuilds the
    whole index from the journal rather than accepting the bytes whose verdict
    it discarded. In the window where the old binary WOULD use the destination
    — an epoch it recognizes — the publication provably did not commit and the
    proxy's answer is correct.
    """
    import _cctally_core
    import _cctally_journal as jr
    import _cctally_store as st

    _seed_live_index()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))
    db = pathlib.Path(_cctally_core.DB_PATH)
    stamped = _stamp_rows(db)
    assert len(stamped) == 1

    # Reconstruct the post-commit, pre-verdict state a crash leaves behind.
    jr._write_publication_marker(
        db, stamped[0], started_at="2026-08-05T12:00:00Z",
        scratch_path=str(_fake_scratch(db)), mechanism="in_place",
    )
    marker_path = _publication_marker(_cctally_core.APP_DIR)
    assert st._pending_stats_publication_never_replaced(db) is False, (
        "this binary resolves the committed publication through its stamp"
    )

    legacy = json.loads(marker_path.read_text())
    legacy.pop("mechanism")
    marker_path.write_text(json.dumps(legacy))
    assert st._pending_stats_publication_never_replaced(db) is True, (
        "an older reader really does misread this state; the test exists to "
        "pin that, not to deny it"
    )

    # And that misreading cannot reach the bytes: the destination carries an
    # epoch such a binary refuses to accept.
    assert st.stats_epoch_rebuild_pending(db) is False
    monkeypatch.setattr(_cctally_core, "STATS_INDEX_EPOCH", 1007)
    assert st.stats_epoch_rebuild_pending(db) is True, (
        "a binary that would apply the old proxy must first find the whole "
        "index unusable at its own epoch"
    )


def test_a_marker_holding_valid_json_that_is_not_an_object_is_not_a_traceback(
    ns, monkeypatch
):
    """#496 S1 F1. `json.loads` returns whatever the bytes decode to.

    `_raise_settled_publication_failure` runs inside `_stats_heal_hook`'s
    `except Exception`, so an `AttributeError` from calling `.get(...)` on
    `null` escapes the hook entirely and surfaces as a raw traceback from
    `open_db` instead of the guided corruption error. That is the worst blast
    radius of this shape, because it replaces a diagnosis with a stack trace on
    a user whose stats.db is already corrupt.

    The marker is written from inside the failing rebuild rather than up front:
    `stats_open_guarded` resolves any marker that exists before it connects, so
    a marker present at open time never reaches the heal hook at all.
    """
    import _cctally_core
    import _cctally_db
    import _cctally_journal as jr

    _seed_live_index()
    _destroy_header_magic(pathlib.Path(_cctally_core.DB_PATH))

    real_rebuild = jr.rebuild_stats_index
    refused = []

    def refuse(**kwargs):
        if not refused:
            refused.append(True)
            _publication_marker(_cctally_core.APP_DIR).write_text("null")
            raise RuntimeError("injected rebuild failure")
        return real_rebuild(**kwargs)

    monkeypatch.setattr(jr, "rebuild_stats_index", refuse)

    # S3 moved the rebuild into the worker, so the worker is where a marker
    # holding `null` is first read back. It must survive that and stay
    # retryable, and a later heal must still converge rather than surfacing an
    # `AttributeError` from calling `.get(...)` on `null`.
    _defer_without_spawning(_cctally_core.open_db)
    _run_heal_worker()
    assert refused == [True]

    _stats_heal_marker(_cctally_core.APP_DIR).unlink(missing_ok=True)
    _defer_without_spawning(_cctally_core.open_db)
    _run_heal_worker()
    conn = _cctally_core.open_db()
    try:
        assert [
            r[0] for r in conn.execute(
                "SELECT weekly_percent FROM weekly_usage_snapshots"
            )
        ] == [7.0]
    finally:
        conn.close()


# ==========================================================================
# F1 — the process that CAUSES the failure must not print the false message
# ==========================================================================

def test_a_failed_publication_in_the_worker_reports_publication_guidance(ns):
    """#496 S1 F1, re-homed by S3's detachment.

    Replacement has already occurred, so the ordinary corrupt-stats text —
    "never auto-recreated … run `cctally db repair --db stats --yes`" — is
    false. The process that
    CAUSES the failure is now the detached worker, whose streams are
    `/dev/null`, so it can only record the verdict durably and log it; the
    guidance therefore reaches the user on the next open, from the marker. What
    must NOT happen is either half going missing, or the worker crashing.

    Both halves of that argument are asserted here. The durable half is the ring
    entry the worker settles, not merely the marker: the ring is what a later
    reader consults, and asserting only the marker would leave the durability
    claim untested. The destination's header is destroyed, so publication falls
    back to physical replacement — which is the mechanism whose message really
    does name a preserved predecessor, so this also pins that wording against
    the quarantine directory it promises.
    """
    import _cctally_core
    import _cctally_db
    import _cctally_journal as jr
    import _cctally_store as st

    _seed_live_index()
    _destroy_header_magic(pathlib.Path(_cctally_core.DB_PATH))

    real_validate = jr._validate_rebuilt_stats_index

    def fail_on_the_destination(conn, high_water):
        real_validate(conn, high_water)
        if ".rebuilding-" not in _main_file_of(conn).name:
            raise jr.JournalError("injected post-publication validation failure")

    jr._validate_rebuilt_stats_index = fail_on_the_destination
    try:
        _defer_without_spawning(_cctally_core.open_db)
        _run_heal_worker()
    finally:
        jr._validate_rebuilt_stats_index = real_validate

    marker = json.loads(
        _publication_marker(_cctally_core.APP_DIR).read_text()
    )
    assert marker["status"] == "failed"
    assert marker["mechanism"] == "replace"

    # The durable half: the worker settled the ring entry it owns.
    events = st.read_stats_heal_events()
    assert len(events) == 1, events
    assert events[0]["outcome"] == "failed"
    assert events[0]["error"] == "JournalError"
    assert events[0]["healId"]

    with pytest.raises(_cctally_db.StatsPublicationFailedError) as caught:
        _cctally_core.open_db()
    message = str(caught.value)
    assert marker["recordPath"] in message
    assert "db repair --db stats" not in message, (
        "a settled publication verdict must not send the user down the "
        "pre-cutover path"
    )
    # This mechanism DID preserve the predecessor, so the message says so and
    # the directory it names is really there.
    quarantine = pathlib.Path(_cctally_core.APP_DIR) / "quarantine"
    assert "preserved under quarantine/" in message
    assert sorted(quarantine.glob("stats.db-*")), sorted(
        quarantine.iterdir() if quarantine.exists() else []
    )


# ==========================================================================
# F2 — the WAL capture is scoped to stats, and never breaks a heal
# ==========================================================================

def test_a_classified_cache_corruption_records_skipped_not_stats(ns):
    """#496 S1 F2. The evidence directory is a stats-only artifact.

    Retention for `logs/stats.db-corruption-forensics-<ts>/` is handed to S6;
    nobody owns retention for a cache or conversations family, whose WAL is
    not bounded by stats.db's 16 MiB `journal_size_limit` and has been observed
    above 256 MiB under a multi-agent hook storm (#297).
    """
    import _cctally_core
    import _cctally_db

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    cache = pathlib.Path(_cctally_core.APP_DIR) / "cache.db"
    cache.write_bytes(b"not a database\x00" + b"\x00" * 4080)
    pathlib.Path(f"{cache}-wal").write_bytes(b"\x00" * 4096)

    bundle_path = _cctally_db.write_corruption_forensics(
        cache,
        db_label="cache",
        trigger_origin="cache-open",
        trigger_exception=sqlite3.DatabaseError(
            "database disk image is malformed"
        ),
    )
    bundle = json.loads(pathlib.Path(bundle_path).read_text())

    assert bundle["walEvidence"]["disposition"] == "skipped_not_stats"
    assert bundle["walEvidence"]["path"] is None
    assert _evidence_dirs(_cctally_core.LOG_DIR) == []


def test_a_non_oserror_from_the_wal_capture_never_breaks_the_heal(
    ns, tmp_path, monkeypatch
):
    """#496 S1 F2/F8. Enrichment can never break a heal, and the guard has to
    cover more than `OSError`: an escaping exception propagates out of
    `write_corruption_forensics`, and the cache caller's own `except Exception`
    then DECLINES destructive recovery.
    """
    import _cctally_core
    import _cctally_db
    import _cctally_tui

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _strand_committed_wal_family(db, tmp_path)
    _zero_main_data_pages(db)

    real_copy = _cctally_db._copy_db_family

    def copy_or_boom(src, dst, **kwargs):
        # Scoped to the evidence copy: preservation uses the same helper, and
        # breaking that would test something else entirely.
        if "corruption-forensics" in str(dst):
            raise RuntimeError("injected WAL evidence copy failure")
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(_cctally_db, "_copy_db_family", copy_or_boom)

    assert _post_query_heal_then_worker(
        sqlite3.DatabaseError("database disk image is malformed")
    ) is True

    conn = _cctally_core.open_db()
    try:
        rows = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [7.0]

    evidence = _latest_bundle(_cctally_core.LOG_DIR)["walEvidence"]
    assert evidence["disposition"] == "failed"
    assert "injected WAL evidence copy failure" in evidence["reason"]


def test_the_publisher_refuses_a_scratch_at_the_wrong_index_epoch(ns, tmp_path):
    """`read_publication_stamp`'s short-circuit rests on the claim that a
    committed publication always leaves the destination at THIS binary's epoch.

    The publisher stamped `main.user_version` from `PRAGMA src.user_version`
    on trust. Upstream validation does guarantee they are equal; asserting it
    here converts that argument into an invariant.
    """
    import _cctally_core
    import _cctally_journal as jr

    scratch = tmp_path / "wrong-epoch-scratch.db"
    src = sqlite3.connect(str(scratch))
    try:
        src.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        src.execute(
            f"PRAGMA user_version = {_cctally_core.STATS_INDEX_EPOCH - 1}"
        )
        src.commit()
    finally:
        src.close()

    dest = tmp_path / "destination.db"
    conn = sqlite3.connect(str(dest))
    try:
        conn.execute("PRAGMA foreign_keys = 0")
        with pytest.raises(jr.JournalError, match="index epoch"):
            jr._publish_generation_in_place(
                conn,
                scratch,
                record_path="/nonexistent/record.json",
                started_at="2026-08-05T12:00:00Z",
            )
        assert int(
            conn.execute("PRAGMA main.user_version").fetchone()[0]
        ) == 0, "the refused publication must not have stamped the destination"
    finally:
        conn.close()


def test_the_opener_consults_the_stamp_when_no_scratch_family_survives(ns):
    """A pending `in_place` marker with NO surviving `.rebuilding-*` family.

    `stats_open_guarded` has two marker paths. The artifact-present one asks
    `_pending_stats_publication_never_replaced` and is correct. The one taken
    when nothing survives beside the marker called
    `_resolve_stats_publication_marker` directly, which validates against the
    record's PINNED HIGH-WATER and never asked the stamp anything. For a
    `replace` marker scratch absence proved `os.replace` had run; for an
    `in_place` marker scratch absence proves NOTHING, so a publication that
    provably never committed was validated against a high-water describing an
    index that does not exist, promoted to `failed`, and every ordinary open
    refused until `db rebuild --db stats` — which is exactly the failure this
    epic exists to eliminate.

    Reachable in production: `_recover_completed_correction`'s error path calls
    `_cleanup_new_correction_scratches` when a `COMMIT_UNKNOWN`-phase error
    leaves the marker `pending`, and a kill inside
    `_recover_or_reclaim_interrupted_stats_rebuild` between artifact removal
    and the marker decision lands in the same state.
    """
    import _cctally_core
    import _cctally_journal as jr
    import _lib_stats_publish as sp

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    before = _logical_rows(db)

    record = pathlib.Path(_cctally_core.APP_DIR) / "stats-rebuild-never-live.json"
    record.write_text(json.dumps({"highWater": ["seg-9999.jsonl", 999999]}))
    scratch = db.with_name(f"{db.name}.rebuilding-20260805T120000_000000")
    assert not scratch.exists(), "this test is about the NO-artifact path"
    jr._write_publication_marker(
        db,
        str(record),
        started_at="2026-08-05T12:00:00Z",
        scratch_path=str(scratch),
        mechanism="in_place",
    )
    # The stamp is the evidence: it does not name this record, so the live
    # bytes are the untouched predecessor.
    assert sp.resolve_stamp(
        jr.read_publication_stamp(db), str(record)
    ) == sp.STAMP_PROVEN_PREDECESSOR

    conn = _cctally_core.open_db()
    try:
        assert _logical_rows_from(conn) == before, (
            "the healthy predecessor must still be readable"
        )
    finally:
        conn.close()
    assert not _publication_marker(_cctally_core.APP_DIR).exists(), (
        "a publication that provably never became live must have its marker "
        "discarded, not promoted to `failed`"
    )


def _logical_rows_from(conn):
    return [
        tuple(r) for r in conn.execute(
            "SELECT weekly_percent, journal_id FROM weekly_usage_snapshots "
            "ORDER BY id"
        )
    ]


def _logical_rows(db):
    import _cctally_core

    conn = _cctally_core.open_db()
    try:
        return _logical_rows_from(conn)
    finally:
        conn.close()


def test_an_unreadable_destination_is_not_reported_as_a_failed_check(
    ns, capsys,
):
    """#496 exists partly because corruption gets MISATTRIBUTED.

    On a corrupt destination the stamp read is INDETERMINATE, and the
    high-water validation then fails for the UNRELATED damage — so stating
    that an earlier publication "FAILED that check" accuses it of something
    the evidence does not support.
    """
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    record = pathlib.Path(_cctally_core.APP_DIR) / "stats-rebuild-prior.json"
    record.write_text(json.dumps({"highWater": ["seg-1.jsonl", 4096]}))
    scratch = db.with_name(f"{db.name}.rebuilding-20260805T120000_000000")
    jr._write_publication_marker(
        db,
        str(record),
        started_at="2026-08-05T12:00:00Z",
        scratch_path=str(scratch),
        mechanism="in_place",
    )
    _destroy_header_magic(db)
    capsys.readouterr()

    settled = jr._settle_prior_publication_verdict(db)

    assert settled is not None and settled["status"] == "failed"
    err = capsys.readouterr().err
    assert "could not be read" in err
    assert "not evidence about that publication" in err
    assert "FAILED that check" not in err


def test_a_content_mismatch_is_still_reported_as_a_failed_check(ns, capsys):
    """The softening must be scoped to READ failures, not applied to every
    validation error — a genuine content mismatch is a real failed check."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    record = pathlib.Path(_cctally_core.APP_DIR) / "stats-rebuild-prior.json"
    # A high-water the healthy destination provably does not materialize.
    record.write_text(json.dumps({"highWater": ["seg-9999.jsonl", 999999]}))
    # A physically-replaced publication whose scratch is gone: `os.replace`
    # consumed it, so the verdict on the live bytes is still owed and the
    # high-water check actually runs.
    scratch = db.with_name(f"{db.name}.rebuilding-20260805T120000_000000")
    assert not scratch.exists()
    jr._write_publication_marker(
        db,
        str(record),
        started_at="2026-08-05T12:00:00Z",
        scratch_path=str(scratch),
        mechanism="replace",
    )
    capsys.readouterr()

    settled = jr._settle_prior_publication_verdict(db)

    assert settled is not None and settled["status"] == "failed"
    err = capsys.readouterr().err
    assert "FAILED that check" in err
    assert "could not be read" not in err


def test_a_third_crashed_run_must_not_drop_a_carried_verdict(ns):
    """#496 S1 F1. The carry must be transitive.

    Run A publishes an invalid index and dies before validating it. Run B
    settles A to `failed`, carries that verdict forward in its own marker, and
    dies before its own `os.replace` — so B's scratch is still on disk. Run C
    then reaches `_settle_prior_publication_verdict` and finds a pending marker
    whose OWN scratch still exists, which means B replaced nothing and owes
    nothing about the live bytes.

    Returning `None` there is right about B and wrong about A: it drops the
    verdict B was carrying, and the next opener removes the marker and connects
    to A's never-validated index. The carried block must pass through.
    """
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)

    # Run B's scratch, still on disk because B died before its `os.replace`.
    live_scratch = db.with_name(f"{db.name}.rebuilding-20260805T120000_000000")
    live_scratch.write_bytes(b"")

    run_a_verdict = {
        "schemaVersion": 1,
        "status": "failed",
        "recordPath": "/nonexistent/stats-rebuild-run-a.json",
        "error": "integrity_check reported: database disk image is malformed",
    }
    jr._write_publication_marker(
        db,
        "/nonexistent/stats-rebuild-run-b.json",
        started_at="2026-08-05T12:00:00Z",
        scratch_path=str(live_scratch),
        prior=run_a_verdict,
    )

    carried = jr._settle_prior_publication_verdict(db)

    assert carried is not None, (
        "run A's verdict was dropped: a pending marker whose own scratch "
        "survives still carries an older run's failure"
    )
    assert carried["status"] == "failed"
    assert carried["error"] == run_a_verdict["error"]
    assert carried["recordPath"] == run_a_verdict["recordPath"]
