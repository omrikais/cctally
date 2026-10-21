"""#901 spec §5.4 / G3s: whole-group deletion is spill-free (non-vacuous).

The product deletion path (the dashboard's retention visit) must write at
most 1.05x the WAL frames of a control that deletes the same group on an
identical copy with an unbounded cache (the group's distinct dirty pages),
and no temp file. A validity guard requires the default-cache control to
write at least twice the unbounded control's frames, so the fixture cannot
shrink into vacuity.

Frames WRITTEN are counted with `SQLITE_DBSTATUS_CACHE_WRITE` on the
connection that deletes: SQLite rewrites a frame in place when a page is
spilled twice inside one transaction, so the WAL file's size cannot show the
re-spill this test exists to catch. The rows carry random keys in several
indexed columns (as production rows do), which is what makes the default
cache re-dirty pages it already spilled. A reader is pinned during every
measurement so no checkpoint copy lands in it.

Temp-file bytes are counted directly (#940 revision 7j, spec §6.7): the
write accounting VFS sums every `xWrite` to a temp-class, unnamed or
delete-on-close file, and every measured connection must be opened through
it. A measurement that cannot be trusted fails; it never reads as zero.

The Q8 fixtures assume freed pages are not overwritten, so every Q8
connection that builds the store or performs the measured deletion sets
`secure_delete` explicitly and checks its effective value first (#940
revision 7k, spec §6.7); each Q8 measurement prints one `Q8-RECORD` line.

This module was once the RED proof on 56e66f07a, when its spill-free cases
used only names that also existed there. Revision 7j's direct write
accounting (`tests/_sqlite_write_accounting.py`) does not exist on that
commit, so the module no longer runs there unchanged; `_instrument` still
reports a missing deletion seam rather than failing on it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import os
import pathlib
import random
import shutil
import sqlite3
import sys
import tempfile
import time

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402
import _retention_fixtures as fx  # noqa: E402

UTC = dt.timezone.utc
KiB = 1024
NOW = dt.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
GROUP_ROWS = 6000
#: The Codex tables' indexes are narrower than Claude's, so a 6,000-row group
#: fits the default cache and never re-spills (measured: 15,646 default vs
#: 13,603 unbounded frames, ratio 1.15; 20,000 rows gave 2.10). The fixture is
#: sized so the validity guard below holds with margin (24,000 rows: 2.29).
CODEX_GROUP_ROWS = 24000
TEMP_SLACK = 64 * KiB
SQLITE_DBSTATUS_CACHE_WRITE = 9

_CLAUDE_DELETES = (
    "DELETE FROM conversation_file_touches WHERE session_id = ?",
    "DELETE FROM conversation_messages WHERE session_id = ?",
    "DELETE FROM conversation_ai_titles WHERE session_id = ?",
    "DELETE FROM conversation_sessions WHERE session_id = ?",
)
_CODEX_DELETES = (
    "DELETE FROM codex_conversation_file_touches WHERE conversation_key = ?",
    "DELETE FROM codex_conversation_messages WHERE conversation_key = ?",
    "DELETE FROM codex_conversation_rollups WHERE conversation_key = ?",
    "DELETE FROM codex_conversation_events WHERE conversation_key = ?",
)


def _cache_writes(conn) -> int:
    """`SQLITE_DBSTATUS_CACHE_WRITE` for this connection: every page it wrote
    to the WAL, spills and in-place rewrites included. Reaches the `sqlite3*`
    through CPython's connection layout (PyObject_HEAD, then the handle), the
    contract `bin/_lib_sqlite_close.py` already relies on."""
    import ctypes
    import _sqlite3

    real = getattr(conn, "_conn", conn)
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


@pytest.fixture(scope="module", autouse=True)
def _write_accounting():
    """Active before any builder opens a connection, in this process."""
    with fx.write_accounting():
        yield


def _text(i):
    return f"{i:08d} " + " ".join(f"tok{(i * 7 + j) % 5000}" for j in range(150))


def _stamp(rng, base):
    return (base + dt.timedelta(seconds=rng.randrange(86_400 * 20))
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _build(tmp_path, monkeypatch, provider, *, report=None):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        return _build_on(conn, ns, provider, report=report)
    except BaseException:
        # #940 F1 fix round 2: the caller closes the connection only once
        # `_build` returns it, so a failure here (the attribution check
        # first) closes it.
        conn.close()
        raise


def _build_on(conn, ns, provider, *, report=None):
    # #940 F1 review #1: the fixture connection reads during the measured
    # product interval, so it is attributed before it builds anything.
    fx.require_attributed(conn, "fixture connection")
    retention = importlib.import_module("_lib_conversation_retention")
    try:
        importlib.import_module("_lib_write_io").reset_for_tests()
    except ModuleNotFoundError:
        pass
    rng = random.Random(901)
    old = dt.datetime(2025, 8, 1, tzinfo=UTC)
    fresh = dt.datetime(2026, 6, 20, tzinfo=UTC)
    groups = (("expire-me", old), ("keep-1", fresh), ("keep-2", fresh))
    offset = 0
    rows = GROUP_ROWS if provider == "claude" else CODEX_GROUP_ROWS
    for i in range(rows):
        for key, base in groups:
            offset += 1
            path = f"/{rng.getrandbits(64):016x}.jsonl"
            if provider == "claude":
                conn.execute(
                    "INSERT INTO conversation_messages (session_id, uuid, "
                    "source_path, byte_offset, timestamp_utc, entry_type, "
                    "text, blocks_json, model, msg_id, req_id, cwd) "
                    "VALUES (?,?,?,?,?,?,?,'[]',?,?,?,?)",
                    (key, f"u-{offset}", path, offset, _stamp(rng, base),
                     "assistant", _text(offset),
                     f"model-{rng.randrange(1000)}",
                     f"{rng.getrandbits(96):024x}",
                     f"{rng.getrandbits(64):016x}",
                     f"/work/{rng.getrandbits(48):012x}"))
            else:
                ts = _stamp(rng, base)
                conn.execute(
                    "INSERT INTO codex_conversation_events (source_path, "
                    "line_offset, source_root_key, conversation_key, "
                    "timestamp_utc, payload_json) VALUES (?,?,?,?,?,?)",
                    (path, offset, "root-a", key, ts,
                     '{"t":"' + _text(offset) + '"}'))
                conn.execute(
                    "INSERT INTO codex_conversation_messages "
                    "(conversation_key, source_root_key, source_path, "
                    "line_offset, timestamp_utc, turn_id, call_id, kind, "
                    "event_type, record_family, model, text, content_digest, "
                    "content_len, detail_json, search_tool, search_thinking) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, "root-a", path, offset, ts, "turn-a", None,
                     "assistant", None, "response_item", "gpt-x",
                     _text(offset), "d" * 32, len(_text(offset)), None, "", ""))
        if i % 500 == 499:
            conn.commit()
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db_path = pathlib.Path(retention._resolve_main_db_path(conn))
    copies = {}
    for name in ("default", "unbounded"):
        target = db_path.parent / f"g3s-{name}.db"
        dst = sqlite3.connect(target)
        try:
            conn.backup(dst)
        finally:
            dst.close()
        copies[name] = target
    if report is not None:
        started = time.monotonic()
        report["starting"] = _observe_control_start(conn, provider)
        report["observationSeconds"] += time.monotonic() - started
    return ns, conn, retention, db_path, copies


class _Pinned:
    """A reader pinned at a truncated WAL, plus the interval's directly
    counted SQLite writes (`writes`)."""

    def __init__(self, db_path):
        self.db_path = db_path

    def __enter__(self):
        c = sqlite3.connect(self.db_path)
        try:
            fx.require_attributed(c, "checkpoint writer")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
        self.reader = sqlite3.connect(self.db_path)
        try:
            # It stays open through the measured interval, so it is measured.
            fx.require_attributed(self.reader, "pinned reader")
            self.reader.execute("BEGIN")
            self.reader.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
            self.interval = fx.measure(str(self.db_path)).begin()
        except BaseException:
            self.reader.close()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self.writes = self.interval.end()
        finally:
            self.reader.rollback()
            self.reader.close()
        return False


def _control(path, provider, *, unbounded, report=None):
    conn = sqlite3.connect(path)
    try:
        fx.require_attributed(conn, "control")
        if unbounded:
            conn.execute("PRAGMA cache_size = -1048576")
            conn.execute("PRAGMA temp_store = MEMORY")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        for sql in (_CLAUDE_DELETES if provider == "claude"
                    else _CODEX_DELETES):
            conn.execute(sql, ("expire-me",))
        conn.commit()
        frames = _cache_writes(conn)
        # 968-MG04-V01: observe only AFTER the original commit and counter
        # snapshot. Never scan schema/counts on this fresh measured writer.
        if report is not None:
            started = time.monotonic()
            settings = {name: conn.execute("PRAGMA " + name).fetchone()[0]
                        for name in ("cache_size", "temp_store", "secure_delete",
                                     "journal_mode", "cache_spill", "synchronous",
                                     "locking_mode")}
            report["controls"]["unbounded" if unbounded else "default"] = {
                "frames": frames, "pageSize": page_size,
                "observationPoint": "deletion-connection-after-commit-and-frame-snapshot",
                "effectiveSettings": settings}
            report["observationSeconds"] = report.get("observationSeconds", 0) + (
                time.monotonic() - started)
    finally:
        conn.close()
    return frames, page_size


def _observe_control_start(conn, provider):
    """Builder-only facts, before either original pinned control interval.

    This adds reads to the builder after its existing physical copies have
    been made. It never opens or warms a fresh measured control connection.
    """
    schema = [{"type": kind, "name": name, "table": table,
               "rootpage": rootpage, "sql": sql}
              for kind, name, table, rootpage, sql in conn.execute(
                  "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_schema "
                  "ORDER BY name")]
    names = {row["name"] for row in schema if row["type"] == "table"}
    tables = (("conversation_messages", "session_id"),) if provider == "claude" else (
        ("codex_conversation_messages", "conversation_key"),
        ("codex_conversation_events", "conversation_key"))
    counts = {table: dict(conn.execute(
        f"SELECT {key},COUNT(*) FROM {table} GROUP BY {key}"))
        for table, key in tables if table in names}
    return {"observationPoint": "builder-after-copies-before-controls",
            "schema": schema, "seededCounts": counts,
            "sqliteVersion": sqlite3.sqlite_version,
            "compileOptions": [row[0] for row in conn.execute("PRAGMA compile_options")]}


def _preserve_control_families(report, families):
    """Copy physical bytes only, after both intervals. No SQLite opens,
    checkpoint, backup or connection-lifetime changes for preservation.

    These are task-created synthetic stores. Capture all present family files
    and explicitly record absent sidecars; compare the complete source family
    before/after copying and verify every destination hash. This establishes
    byte stability, not a claim of an SQLite logical integrity check.
    """
    target = pathlib.Path(report["evidenceDirectory"])
    report["failureArtifact"] = str(target)
    suffixes = ("", "-wal", "-shm", "-journal")

    def snapshot(path):
        files = {}
        for suffix in suffixes:
            source = pathlib.Path(str(path) + suffix)
            if source.exists():
                stat = source.stat()
                with source.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                files[suffix] = {"sha256": digest, "size": stat.st_size,
                                 "inode": stat.st_ino, "mtimeNs": stat.st_mtime_ns}
        return files

    before = {name: snapshot(path) for name, path in families.items()}
    rows = []
    for name, path in families.items():
        row = {"name": name, "source": str(path), "files": [],
               "absentSuffixes": [s for s in suffixes if s not in before[name]]}
        for suffix, info in before[name].items():
            dest = target / (name + ".db" + suffix)
            shutil.copyfile(str(path) + suffix, dest)
            with dest.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            row["files"].append({"suffix": suffix, "copy": dest.name,
                                 "sha256": digest, "size": dest.stat().st_size,
                                 "sourceSha256Before": info["sha256"],
                                 "sourceStatBefore": info})
        rows.append(row)
    after = {name: snapshot(path) for name, path in families.items()}
    consistent = before == after
    for row in rows:
        for item in row["files"]:
            info = after[row["name"]].get(item["suffix"], {})
            item["sourceSha256After"] = info.get("sha256")
            item["sourceStatAfter"] = info
            consistent = consistent and item["sha256"] == item["sourceSha256Before"]
    manifest = {"schemaVersion": 1, "process": report["process"],
                "consistency": ("physical-files-stable-during-copy" if consistent
                                else "physical-files-changed-during-copy"),
                "sqliteOpenedForPreservation": False, "families": rows}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return consistent


def _emit_control_report(report, default, unbounded, *, families=None):
    """Only called after both `_Pinned.__exit__` operations completed.
    Observation overhead is charged to the unchanged case runtime. The
    post-interval seconds include directories/copy/hashing/manifest work,
    excluding final JSON serialization, controls.json write and print.
    Successful cases persist counters too: pytest may suppress passed stdout.
    """
    started = time.monotonic()
    report["controls"]["default"]["writes"] = default.writes.summary()
    report["controls"]["unbounded"]["writes"] = unbounded.writes.summary()
    root = pathlib.Path(os.environ.get("CCTALLY_TEST_EVIDENCE_ROOT") or
                        (pathlib.Path(tempfile.gettempdir()) / "cctally-test-evidence"))
    # The suite owns its run layout. Targeted wrapper runs have no suite
    # directory; keep their unique report under the supplied evidence root.
    remote_dir = os.environ.get("CCTALLY_REMOTE_DIR")
    run_id = os.environ.get("CCTALLY_TEST_RUN_ID")
    if remote_dir and run_id and (root / remote_dir / run_id).is_dir():
        root = root / remote_dir / run_id
    root = root / "raw" / "retention-controls"
    root.mkdir(parents=True, exist_ok=True)
    target = pathlib.Path(tempfile.mkdtemp(prefix=report["provider"] + "-", dir=root))
    report["evidenceDirectory"] = str(target)
    try:
        if families is not None:
            report["physicalFamilyStable"] = _preserve_control_families(report, families)
    except Exception as error:
        report["artifactError"] = repr(error)
        raise
    finally:
        report["postIntervalSeconds"] = time.monotonic() - started
        serialized = json.dumps(report, sort_keys=True)
        (target / "controls.json").write_text(serialized + "\n")
        print("G3S-CONTROL " + serialized, flush=True)


def _attribute_seams(monkeypatch, retention):
    """#940 F1 review #1: the reclaim and state connections a visit opens
    (`_run_reclaim_attempt`, and `_update_record` for an episode's end or a
    failure) are measured too, so each must be opened through the write
    accounting VFS; a bypassing one fails the measurement when opened."""
    for seam, label in (("_open_reclaim_connection", "reclaim connection"),
                        ("_open_state_connection", "state connection")):
        real = getattr(retention, seam)

        def checked(path, _real=real, _label=label):
            conn = _real(path)
            try:
                fx.require_attributed(conn, _label)
            except BaseException:
                conn.close()
                raise
            return conn

        monkeypatch.setattr(retention, seam, checked)


def _instrument(monkeypatch, retention, seen):
    """On the candidate, wrap the per-visit deletion connection to record its
    frames written by the group's commit and its (temp_store, cache_size).
    Returns False on 56e66f07a, where deletion runs on the caller's
    connection and no seam exists."""
    if not hasattr(retention, "_open_deletion_connection"):
        return False
    _attribute_seams(monkeypatch, retention)
    real_open = retention._open_deletion_connection

    class Spy:
        def __init__(self, conn):
            # #940 F1 review #16: checked when the visit opens it, before any
            # measured work, not at its commit. A connection that fails the
            # check is closed, not leaked (#940 F1 fix round 3, R3-8).
            try:
                fx.require_attributed(conn, "deletion connection")
            except BaseException:
                conn.close()
                raise
            self._conn = conn

        def commit(self):
            result = self._conn.commit()
            seen.append({
                "frames": _cache_writes(self._conn),
                "attributed": fx.is_attributed(self._conn),
                "temp_store": int(self._conn.execute(
                    "PRAGMA temp_store").fetchone()[0]),
                "cache_size": int(self._conn.execute(
                    "PRAGMA cache_size").fetchone()[0]),
            })
            return result

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(retention, "_open_deletion_connection",
                        lambda path: Spy(real_open(path)))
    return True


#: The attribution assertion's text. The deletion connection is checked when
#: the visit opens it (`Spy.__init__`, #940 F1 review #16), which fails the
#: measurement there; each commit also records `is_attributed`, and that
#: record is asserted afterwards as a second reading.
_BYPASS = "deletion connection: the connection bypasses the write accounting VFS"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_spill_free_deletion_writes_each_dirty_page_once(
        tmp_path, monkeypatch, provider):
    report = {"schemaVersion": 1, "provider": provider, "seed": 901,
              "rowsPerGroup": GROUP_ROWS if provider == "claude" else CODEX_GROUP_ROWS,
              "process": {"pid": os.getpid(), "workerId": os.environ.get("PYTEST_XDIST_WORKER"),
                          "pythonVersion": sys.version, "hostname": os.uname().nodename,
                          "runId": os.environ.get("CCTALLY_TEST_RUN_ID"),
                          "remoteDir": os.environ.get("CCTALLY_REMOTE_DIR")},
              "controls": {}, "observationSeconds": 0.0}
    ns, conn, retention, db_path, copies = _build(tmp_path, monkeypatch,
                                                  provider, report=report)
    try:
        with _Pinned(copies["default"]) as default:
            default_frames, page_size = _control(copies["default"], provider,
                                                 unbounded=False, report=report)
        with _Pinned(copies["unbounded"]) as unbounded:
            unbounded_frames, _ = _control(copies["unbounded"], provider,
                                           unbounded=True, report=report)
        try:
            assert default_frames >= 2 * unbounded_frames, (
                "validity: the group must make the default cache re-spill",
                default_frames, unbounded_frames)
        except AssertionError as failure:
            try:
                _emit_control_report(report, default, unbounded,
                                     families={"product": db_path, **copies})
            except Exception as reporting_error:
                # Ancillary evidence failure must never replace this original
                # validity assertion (including its exact argument tuple).
                failure.add_note("control reporting failed: " + repr(reporting_error))
            raise
        _emit_control_report(report, default, unbounded)

        seen = []
        instrumented = _instrument(monkeypatch, retention, seen)
        with _Pinned(db_path) as product:
            before = 0 if instrumented else _cache_writes(conn)
            stats = retention._maybe_prune_conversation_retention(
                conn, now_utc=NOW, retention_days=180, force=True)
            product_frames = (seen[0]["frames"] if instrumented
                              else _cache_writes(conn) - before)
        assert stats is not None
        assert product_frames <= 1.05 * unbounded_frames, (
            "the product deletion re-spilled dirty pages to the WAL",
            product_frames, unbounded_frames)
        assert (seen[0]["temp_store"], seen[0]["cache_size"]) == (
            2, -262144), "temp_store=MEMORY under the 256 MiB cache cap"
        assert all(s["attributed"] for s in seen), (_BYPASS, seen)
        # #940 revision 7j: temp-file bytes are counted directly, per file
        # class, so no frame or baseline is subtracted.
        assert default.writes.temp > TEMP_SLACK, (
            "validity: the default control's statement journal reaches a "
            "temp file", default.writes.summary())
        assert unbounded.writes.temp == 0, (
            "validity: the temp_store=MEMORY control writes no temp file",
            unbounded.writes.summary())
        assert product.writes.temp <= TEMP_SLACK, (
            "the product deletion wrote a temp file", product.writes.summary())
        # #901 §5.3a (Q11): a fresh ordinary writer keeps SQLite's default
        # cache size and holds its statement journals in memory.
        fresh = ns["open_conversations_db"](attach_cache=False)
        try:
            assert fresh.execute("PRAGMA cache_size").fetchone()[0] == -2000
            assert fresh.execute("PRAGMA temp_store").fetchone()[0] == 2
        finally:
            fresh.close()
    finally:
        conn.close()


def test_spill_free_connection_configuration(tmp_path, monkeypatch):
    """The deterministic half of the spill-free property, with no write
    measured (test regime reset, session 1; spec 4.3). The WAL-frame and
    temp-byte measurement above is quarantined (#942) and runs on the
    scheduled run; this keeps the machine-safety configuration it depends on
    checked on the merge path: the retention visit's deletion connection runs
    with an in-memory temp store under the 256 MiB cache cap, and a newly
    opened ordinary writer keeps SQLite's default cache size with in-memory
    statement journals (#901 spec 5.3a, Q11)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    fresh = ns["open_conversations_db"](attach_cache=False)
    try:
        db_path = next(row[2] for row in fresh.execute("PRAGMA database_list")
                       if row[1] == "main")
        assert fresh.execute("PRAGMA cache_size").fetchone()[0] == -2000
        assert fresh.execute("PRAGMA temp_store").fetchone()[0] == 2
    finally:
        fresh.close()
    retention = importlib.import_module("_lib_conversation_retention")
    # The visit opens its deletion connection through this seam, which is the
    # one the measuring test instruments.
    deletion = retention._open_deletion_connection(db_path)
    try:
        assert (int(deletion.execute("PRAGMA temp_store").fetchone()[0]),
                int(deletion.execute("PRAGMA cache_size").fetchone()[0])) == (
            2, -262144), "temp_store=MEMORY under the 256 MiB cache cap"
    finally:
        deletion.close()


def test_a_group_above_the_row_threshold_takes_the_bounded_fallback(
        tmp_path, monkeypatch):
    ns, conn, retention, db_path, _copies = _build(tmp_path, monkeypatch,
                                                   "claude")
    try:
        monkeypatch.setattr(retention, "DELETION_FALLBACK_ROW_THRESHOLD", 100)
        seen = []
        assert _instrument(monkeypatch, retention, seen)
        ops = []
        with _Pinned(db_path) as product:
            retention._maybe_prune_conversation_retention(
                conn, now_utc=NOW, retention_days=180, force=True,
                record_phase=lambda kind, payload: ops.append(payload))
        [op] = ops
        assert op["mode"] == "fallback"
        assert op["charged_bytes"] == retention.recompute_charge(
            {"phase": "delete", **op})
        assert (seen[0]["temp_store"], seen[0]["cache_size"]) == (
            1, -262144), (
            "the file temp store for the statement journal, the cache cap "
            "kept")
        assert all(s["attributed"] for s in seen), (_BYPASS, seen)
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        wal_bytes = seen[0]["frames"] * (page_size + 24)
        copy_bytes = seen[0]["frames"] * page_size    # the eventual checkpoint
        temp_bytes = product.writes.temp
        assert wal_bytes + copy_bytes + temp_bytes <= 40 * KiB * GROUP_ROWS
    finally:
        conn.close()


#: Injected below the 6,000-row group's working set (about 7,900 distinct
#: pages, 31 MiB), so the fallback's cache actually spills (spec G3s, Q8;
#: `dc4` T3's NEED: injecting the row threshold alone does not prove the
#: fallback's costs). Half the working set, as the production 256 MiB cap is
#: to a group of twice the threshold. A cap of 64 pages (1/123 of the working
#: set) rewrote each page 6.7 times and broke the 40 KiB-per-row bound
#: (281 MB for 6,000 rows): that bound assumes a cap of the same order as the
#: working set, which R-cov (vi) measures at production scale.
_SPILLING_CACHE_KIB = 16 * 1024


def test_the_fallback_actually_spills_and_stays_within_its_bound(
        tmp_path, monkeypatch):
    """Q5 with both injections: some page reaches the WAL more than once (the
    validity guard), the statement journal uses a temp file, the injected
    cache cap holds, and the whole operation stays within 40 KiB per deleted
    row and within its charge."""
    ns, conn, retention, db_path, _copies = _build(tmp_path, monkeypatch,
                                                   "claude")
    try:
        monkeypatch.setattr(retention, "DELETION_FALLBACK_ROW_THRESHOLD", 100)
        monkeypatch.setattr(retention, "DELETION_CACHE_SIZE_KIB",
                            _SPILLING_CACHE_KIB)
        seen = []
        assert _instrument(monkeypatch, retention, seen)
        ops = []
        with _Pinned(db_path) as product:
            retention._maybe_prune_conversation_retention(
                conn, now_utc=NOW, retention_days=180, force=True,
                record_phase=lambda kind, payload: ops.append(payload))
            _page_size, frames = fx.wal_frames(db_path)
        [op] = ops
        assert op["mode"] == "fallback"
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        distinct = len({pgno for _o, pgno, _c in frames})
        written = seen[0]["frames"]
        measurement = {"written": written, "distinct": distinct,
                       "writes": product.writes.summary(), "rows": op["rows"],
                       "charge": op["charged_bytes"]}
        assert written > distinct, ("validity: some page repeats", measurement)
        assert (seen[0]["temp_store"], seen[0]["cache_size"]) == (
            1, -_SPILLING_CACHE_KIB), ("the file temp store, the cap held",
                                       measurement)
        assert all(s["attributed"] for s in seen), (_BYPASS, seen)
        wal_bytes = written * (page_size + 24) + fx.WAL_HEADER_BYTES
        temp_bytes = product.writes.temp
        assert temp_bytes > TEMP_SLACK, (
            "validity: the statement journal reaches a temp file", measurement)
        total = wal_bytes + distinct * page_size + temp_bytes
        assert total <= 40 * KiB * op["rows"], measurement
        assert total <= op["charged_bytes"], (
            "the fallback's writes exceed its charge", measurement)
        assert op["charged_bytes"] == retention.recompute_charge(
            {"phase": "delete", **op})
    finally:
        conn.close()


#: The expiring Q8 group's key (`fx.build_scattered_codex_group`'s default).
_Q8_KEY = "q8-expire"


def _q8_rows_left(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_events "
            "WHERE conversation_key = ?", (_Q8_KEY,)).fetchone()[0])
    finally:
        conn.close()


def _q8(tmp_path, monkeypatch, *, required, deletion_actual=None,
        builder=fx.build_scattered_codex_group, negative=False,
        measurements=None, check_clustered=True):
    """The Q8 body: build the group with `builder` on a store whose builder
    connections run at `secure_delete=required`, delete it through the
    product's retention visit on a deletion connection set to
    `deletion_actual` (default `required`) and checked against `required`
    before its first statement, and judge the measured WAL page set.

    `required=0` is the precondition of revision 6's fixture and keeps the
    dominance guard; `required=1` is the companion, which keeps every other
    bound. `negative=True` judges the clustered fixture instead: the deletion
    removes the group, its WAL page set is nonempty, its overflow pages are
    clustered and the dominance predicate is false. `check_clustered=False`
    exists only for the negative fixture's substitution test. Prints one
    `Q8-RECORD` line and returns the measurement (also appended to
    `measurements`, when given, before anything is judged)."""
    import _cctally_core

    if deletion_actual is None:
        deletion_actual = required
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    precreated = fx.precreate_store(_cctally_core.CONVERSATIONS_DB_PATH,
                                    fx.SCATTER_PAGE_SIZE,
                                    secure_delete=required)
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        fx.require_attributed(conn, "fixture connection")
        opened = fx.set_secure_delete(conn, required, "builder connection")
        retention = importlib.import_module("_lib_conversation_retention")
        importlib.import_module("_lib_write_io").reset_for_tests()
        geometry = builder(
            conn, key=_Q8_KEY, ts="2025-08-01T12:00:00.000Z",
            keeper_ts="2026-07-17T08:00:00.000Z")
        db_path = pathlib.Path(retention._resolve_main_db_path(conn))
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        regions = fx.overflow_regions(db_path, page_size)
        seen = []
        deletion = []
        _attribute_seams(monkeypatch, retention)
        real_open = retention._open_deletion_connection

        class Spy:
            def __init__(self, inner):
                fx.require_attributed(inner, "deletion connection")
                self._conn = inner

            def commit(self):
                result = self._conn.commit()
                if not seen:
                    seen.append({"frames": fx.cache_writes(self._conn),
                                 "wal": fx.wal_frames(db_path)[1]})
                return result

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def open_deletion(path):
            # Revision 7k: set and checked on the real connection before the
            # visit's first statement on it.
            inner = real_open(path)
            try:
                fx.set_secure_delete(inner, deletion_actual,
                                     "deletion connection")
                deletion.append(fx.require_secure_delete(
                    inner, required, "deletion connection"))
                return Spy(inner)
            except BaseException:
                inner.close()
                raise

        monkeypatch.setattr(retention, "_open_deletion_connection",
                            open_deletion)
        ops = []
        builder_before = fx.cache_writes(conn)
        with _Pinned(db_path):
            retention._maybe_prune_conversation_retention(
                conn, now_utc=NOW, retention_days=180, force=True,
                record_phase=lambda kind, payload: ops.append(payload))
        builder_during = fx.cache_writes(conn) - builder_before
        # #940 F1 fix round 3 (R3-2): after the deletion, no overflow chain
        # traces back to the events table. Read after a TRUNCATE checkpoint,
        # once the pinned reader has gone and the WAL pages are recorded.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        regions_after = fx.overflow_regions(db_path, page_size)
        [op] = [p for p in ops if p["outcome"] == "ok"]
        rows = op["rows"]
        pages = [pgno for _o, pgno, _c in seen[0]["wal"]]
        ptrmap, other = fx.classify(pages, page_size, page_size)
        per_map = page_size // 5 + 1
        growth = -(-(512 * KiB + 12 * KiB * rows) // page_size)
        m_cap = -(-(page_count + growth) // per_map) + 1
        measurement = {
            "module": "spill_free", "builder": builder.__name__,
            "required": required, "rows": rows, "M": len(ptrmap),
            "D": len(other), "frames": seen[0]["frames"], "m_cap": m_cap,
            "overflow_regions": regions,
            "overflow_regions_after_deletion": regions_after,
            "builder_writes_during_deletion": builder_during,
            "secure_delete": {
                "builder": {"precreate_store": precreated,
                            "product_opener": opened},
                "deletion": deletion}}
        print("Q8-RECORD " + json.dumps(measurement, sort_keys=True),
              flush=True)
        if measurements is not None:
            measurements.append(measurement)
        assert regions_after == 0, (
            "no overflow chain of the events table survives the deletion",
            measurement)
        if negative:
            assert rows == geometry["events"], (
                "negative: the deletion removes the group", measurement)
            assert pages, ("negative: the measured WAL page set is nonempty",
                           measurement)
            if check_clustered:
                # #940 F2-b: bounded below too, so the clustering evidence
                # cannot pass vacuously at zero regions.
                assert 1 <= regions <= fx.CLUSTER_MAX_REGIONS, (
                    "negative: overflow pages are clustered", measurement)
                # F2-c: the derivation's premise, as the builder measured it.
                assert geometry.get("free_before_insert") == 0, (
                    "negative: overflow pages are clustered only from an empty "
                    "freelist", measurement)
            assert not (len(ptrmap) > len(other)), (
                "negative: the dominance predicate is false", measurement)
            return measurement
        assert rows == geometry["events"], measurement
        if required == 0:
            assert len(ptrmap) > len(other), (
                "validity: pointer-map pages outnumber the others",
                measurement)
        assert seen[0]["frames"] == len(set(pages)), (
            "no page written twice", measurement)
        assert len(ptrmap) <= m_cap, measurement
        assert (2 * page_size + 24) * len(other) <= 512 * KiB + 12 * KiB * rows, \
            measurement
        assert op["pointer_map_cap"] == m_cap
        return measurement
    finally:
        conn.close()


def test_q8_a_scattered_overflow_group_is_bounded_per_page(
        tmp_path, monkeypatch):
    """G3s second fixture (Q8): a small, overflow-heavy Codex conversation
    over a fragmented file. Its spill-free deletion writes no page twice, its
    pointer-map pages are at most M_cap, and its other pages cost at most
    512 KiB + 12 KiB per deleted row; the validity guard requires its
    pointer-map pages to outnumber its other pages, which holds only while
    freed pages are not overwritten (`secure_delete=OFF`, revision 7k)."""
    _q8(tmp_path, monkeypatch, required=0)


def test_q8_the_secure_delete_on_companion_is_bounded_per_page(
        tmp_path, monkeypatch):
    """Revision 7k: the same scattered workload with `secure_delete=ON` on
    the builder and the deletion connection removes the eight rows and keeps
    every bound except the dominance guard."""
    _q8(tmp_path, monkeypatch, required=1)


@pytest.mark.parametrize("required, deletion_actual", [
    pytest.param(0, 1, id="off-precondition"),
    pytest.param(1, 0, id="on-companion")])
def test_q8_a_secure_delete_mismatch_is_rejected(
        tmp_path, monkeypatch, required, deletion_actual):
    """Revision 7k: the builder reports the required value while the actual
    deletion connection reports the other; the precondition rejects it
    before the measured deletion runs, so the group's rows remain."""
    import _cctally_core

    with pytest.raises(AssertionError, match="precondition: "):
        _q8(tmp_path, monkeypatch, required=required,
            deletion_actual=deletion_actual)
    assert _q8_rows_left(_cctally_core.CONVERSATIONS_DB_PATH) == \
        fx.SCATTER_EVENTS


def test_q8_the_dominance_guard_fails_on_a_clustered_group(
        tmp_path, monkeypatch):
    """Revision 7k's negative fixture: the same overflow workload with its
    chains allocated contiguously, under its own asserted `secure_delete=OFF`
    on the builder and the deletion connection. The deletion removes the
    group, its WAL page set is nonempty, its overflow pages lie in at most
    `CLUSTER_MAX_REGIONS` pointer-map regions (read from the file), and the
    unchanged dominance predicate is false: the guard can fail."""
    _q8(tmp_path, monkeypatch, required=0,
        builder=fx.build_clustered_codex_group, negative=True)


def test_q8_the_negative_fixture_rejects_the_scattered_group(
        tmp_path, monkeypatch):
    """Substituting the scattered fixture at `secure_delete=OFF` into the
    negative test makes it fail: at the clustering check, and, with that one
    check disabled, at the dominance predicate."""
    measurements = []
    with monkeypatch.context() as mp, pytest.raises(
            AssertionError, match="negative: overflow pages are clustered"):
        _q8(tmp_path / "clustering", mp, required=0, negative=True,
            measurements=measurements)
    [scattered] = measurements
    assert scattered["overflow_regions"] > fx.CLUSTER_MAX_REGIONS, scattered
    with monkeypatch.context() as mp, pytest.raises(
            AssertionError, match="negative: the dominance predicate is false"):
        _q8(tmp_path / "dominance", mp, required=0, negative=True,
            check_clustered=False)


def test_overflow_regions_counts_only_the_named_tables_chains(tmp_path):
    """#940 F1 fix round 3 (R3-2): `overflow_regions` counts the regions of
    the overflow chains that trace back, through the pointer map, to the named
    table's root, not every overflow entry in the file. Another table's
    chains spanning several regions do not count, and a table without an
    overflowing row counts zero."""
    page_size = fx.SCATTER_PAGE_SIZE
    per_region = page_size // 5 + 1
    db = tmp_path / "regions.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(f"PRAGMA page_size={page_size}")
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE other(payload BLOB)")
        conn.execute("CREATE TABLE named(payload BLOB)")
        conn.execute("CREATE TABLE small(payload BLOB)")
        # About 14 overflow pages per row, so these chains cover more than
        # three regions' worth of consecutive pages.
        conn.executemany("INSERT INTO other VALUES (?)",
                         ((b"\x01" * 14_000,)
                          for _ in range(3 * per_region // 14 + 2)))
        # One overflow page, so exactly one region.
        conn.execute("INSERT INTO named VALUES (?)", (b"\x02" * 1_500,))
        conn.execute("INSERT INTO small VALUES (?)", (b"\x03" * 10,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert fx.overflow_regions(db, page_size, table="named") == 1
        assert fx.overflow_regions(db, page_size, table="small") == 0
        assert fx.overflow_regions(db, page_size, table="other") >= 3
    finally:
        conn.close()


def test_the_secure_delete_precondition_rejects_a_mismatched_connection(
        tmp_path):
    """Revision 7k: `require_secure_delete` reads every schema of the
    connection and rejects a value other than the expected one."""
    conn = sqlite3.connect(tmp_path / "sd.db")
    try:
        conn.execute("PRAGMA secure_delete=1")
        with pytest.raises(AssertionError, match="precondition: probe has"):
            fx.require_secure_delete(conn, 0, "probe")
        assert fx.require_secure_delete(conn, 1, "probe") == {"main": 1}
        assert fx.set_secure_delete(conn, 0, "probe") == {"main": 0}
    finally:
        conn.close()


@pytest.mark.parametrize("seam, label", [
    pytest.param("_open_reclaim_connection", "reclaim connection",
                 id="reclaim"),
    pytest.param("_open_state_connection", "state connection", id="state")])
def test_a_reclaim_or_state_connection_outside_the_wrapper_fails(
        tmp_path, monkeypatch, seam, label):
    """#940 F1 review #1: a reclaim or state connection that a visit opens
    outside the write accounting VFS fails the measurement."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        retention = importlib.import_module("_lib_conversation_retention")
        db_path = pathlib.Path(retention._resolve_main_db_path(conn))
    finally:
        conn.close()
    real = getattr(retention, seam)

    def opened_outside(path):
        fx.acct.deactivate()
        try:
            return real(path)
        finally:
            fx.acct.activate()

    monkeypatch.setattr(retention, seam, opened_outside)
    assert _instrument(monkeypatch, retention, [])
    with pytest.raises(fx.acct.NotAttributed,
                       match=label + ": the connection bypasses"):
        if seam == "_open_state_connection":
            retention._end_reclaim_episode(db_path)
        else:
            retention._run_reclaim_attempt(db_path, now=NOW,
                                           record_phase=None,
                                           clock=time.monotonic)


def test_build_closes_its_connection_when_attribution_fails(
        tmp_path, monkeypatch):
    """#940 F1 fix round 2 (R2-7): a fixture connection that fails its
    attribution check is closed by `_build` rather than leaked."""
    refused = []

    def refuse(conn, label, schema="main"):
        refused.append(conn)
        raise fx.acct.NotAttributed("%s: refused by the test" % label)

    monkeypatch.setattr(fx, "require_attributed", refuse)
    with pytest.raises(fx.acct.NotAttributed, match="fixture connection"):
        _build(tmp_path, monkeypatch, "claude")
    [conn] = refused
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_instrument_closes_the_deletion_connection_when_attribution_fails(
        tmp_path, monkeypatch):
    """#940 F1 fix round 3 (R3-8): a deletion connection that fails its
    attribution check when the visit opens it is closed by `_instrument`'s
    opener rather than leaked."""
    load_script()
    retention = importlib.import_module("_lib_conversation_retention")
    opened = []

    def real_open(path):
        conn = sqlite3.connect(path)
        opened.append(conn)
        return conn

    def refuse(conn, label, schema="main"):
        raise fx.acct.NotAttributed("%s: refused by the test" % label)

    monkeypatch.setattr(retention, "_open_deletion_connection", real_open)
    monkeypatch.setattr(fx, "require_attributed", refuse)
    assert _instrument(monkeypatch, retention, [])
    with pytest.raises(fx.acct.NotAttributed, match="deletion connection"):
        retention._open_deletion_connection(str(tmp_path / "deletion.db"))
    [conn] = opened
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_a_pinned_reader_outside_the_wrapper_fails(tmp_path, monkeypatch):
    """#940 F1 fix round 2 (R2-8): `_Pinned`'s reader stays open through the
    measured interval, so it is attributed like every other measured
    connection; one opened outside the write accounting VFS fails, closed."""
    db = tmp_path / "pinned.db"
    setup = sqlite3.connect(db)
    try:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE cache_meta(k)")
        setup.commit()
    finally:
        setup.close()
    real_connect = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        if opened:
            fx.acct.deactivate()
            try:
                conn = real_connect(*args, **kwargs)
            finally:
                fx.acct.activate()
        else:
            conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(fx.acct.NotAttributed,
                       match="pinned reader: the connection bypasses"):
        with _Pinned(db):
            pass
    monkeypatch.undo()
    assert len(opened) == 2, opened
    with pytest.raises(sqlite3.ProgrammingError):
        opened[1].execute("SELECT 1")


@pytest.mark.parametrize("valid", [False, True], ids=["failure", "success"])
def test_control_reporting_preserves_intervals_failure_and_physical_families(
        tmp_path, monkeypatch, capsys, valid):
    """Missing/early reporting, a replaced validity error, or a logical copy
    loses the evidence this control is intended to retain. Only the costly
    fixture and measured operations are doubled; reporting/copying is real."""
    import types

    events = []
    evidence = tmp_path / "evidence"
    monkeypatch.setenv("CCTALLY_TEST_EVIDENCE_ROOT", str(evidence))
    monkeypatch.delenv("EV_EVIDENCE_DIR", raising=False)
    paths = {name: tmp_path / (name + ".db")
             for name in ("product", "default", "unbounded")}
    originals = {}
    for name, path in paths.items():
        for suffix in ("", "-wal", "-shm"):
            payload = (name + suffix).encode() + b"\x00physical-family"
            pathlib.Path(str(path) + suffix).write_bytes(payload)
            originals[(name, suffix)] = payload

    class Connection:
        def close(self):
            events.append("builder-close")

    class Fresh(Connection):
        def execute(self, sql):
            return types.SimpleNamespace(fetchone=lambda: (
                -2000 if "cache_size" in sql else 2,))

    def build(*args, **kwargs):
        if kwargs.get("report") is not None:
            kwargs["report"]["starting"] = {"observationPoint": "builder"}
        return ({"open_conversations_db": lambda **kw: Fresh()}, Connection(),
                types.SimpleNamespace(_maybe_prune_conversation_retention=
                                      lambda *a, **kw: {}),
                paths["product"], {k: paths[k] for k in ("default", "unbounded")})

    class Pinned:
        def __init__(self, path):
            self.name = pathlib.Path(path).stem
            self.writes = fx.acct.Writes.empty(self.name)
            if self.name == "default":
                self.writes._temp = TEMP_SLACK + 1

        def __enter__(self):
            events.append(self.name + "-begin")
            return self

        def __exit__(self, *exc):
            events.append(self.name + "-end")

    def control(path, provider, *, unbounded, **kwargs):
        frames = 5 if unbounded else (10 if valid else 9)
        if kwargs.get("report") is not None:
            kwargs["report"]["controls"]["unbounded" if unbounded else "default"] = {
                "frames": frames, "pageSize": 4096}
        return frames, 4096

    def instrument(mp, retention, seen):
        seen.append({"frames": 5, "temp_store": 2, "cache_size": -262144,
                     "attributed": True})
        return True

    monkeypatch.setattr(sys.modules[__name__], "_build", build)
    monkeypatch.setattr(sys.modules[__name__], "_Pinned", Pinned)
    monkeypatch.setattr(sys.modules[__name__], "_control", control)
    monkeypatch.setattr(sys.modules[__name__], "_instrument", instrument)
    real_print = print

    def print_after_intervals(*args, **kwargs):
        assert events[:4] == ["default-begin", "default-end",
                             "unbounded-begin", "unbounded-end"]
        return real_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", print_after_intervals)
    real_copy = shutil.copyfile

    def copy_after_intervals(*args, **kwargs):
        assert events == ["default-begin", "default-end",
                          "unbounded-begin", "unbounded-end"]
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", copy_after_intervals)

    def no_preservation_open(*args, **kwargs):
        raise AssertionError("preservation must not reopen SQLite")

    monkeypatch.setattr(sqlite3, "connect", no_preservation_open)
    original_failures = []
    real_emit = _emit_control_report

    def emit_with_original_failure(*args, **kwargs):
        original_failures.append(sys.exception())
        return real_emit(*args, **kwargs)

    monkeypatch.setattr(sys.modules[__name__], "_emit_control_report",
                        emit_with_original_failure)
    if valid:
        test_spill_free_deletion_writes_each_dirty_page_once(
            tmp_path, monkeypatch, "claude")
    else:
        with pytest.raises(AssertionError) as caught:
            test_spill_free_deletion_writes_each_dirty_page_once(
                tmp_path, monkeypatch, "claude")
        assert caught.value is original_failures[0]
        assert "validity: the group must make the default cache re-spill" in str(caught.value)
    output = capsys.readouterr().out
    records = [json.loads(line.removeprefix("G3S-CONTROL "))
               for line in output.splitlines() if line.startswith("G3S-CONTROL ")]
    assert len(records) == 1, "both control counters must be emitted after intervals"
    [record] = records
    assert record["controls"]["default"]["frames"] == (10 if valid else 9)
    assert record["controls"]["unbounded"]["frames"] == 5
    assert record["controls"]["default"]["writes"]["temp"] == TEMP_SLACK + 1
    assert record["controls"]["unbounded"]["writes"]["temp"] == 0
    assert record["process"]["pid"] > 0
    assert events[-1] == "builder-close"
    report_dir = pathlib.Path(record["evidenceDirectory"])
    assert json.loads((report_dir / "controls.json").read_text()) == record
    if valid:
        assert sorted(p.name for p in report_dir.iterdir()) == ["controls.json"]
        return
    manifest_path = pathlib.Path(record["failureArtifact"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["schemaVersion"] == 1
    assert manifest["consistency"] == "physical-files-stable-during-copy"
    assert manifest["sqliteOpenedForPreservation"] is False
    assert {f["name"] for f in manifest["families"]} == {
        "product", "default", "unbounded"}
    for family in manifest["families"]:
        assert len(family["files"]) == 3
        assert family["absentSuffixes"] == ["-journal"]
        for item in family["files"]:
            payload = (manifest_path.parent / item["copy"]).read_bytes()
            assert payload == originals[(family["name"], item["suffix"])]
            assert item["sha256"] == hashlib.sha256(payload).hexdigest()
            assert item["size"] == len(payload)
            assert item["sourceSha256Before"] == item["sourceSha256After"] == item["sha256"]


def test_control_reporting_observes_effective_settings_after_frame_snapshot(
        tmp_path, monkeypatch):
    """A settings scan before deletion can warm the cache and invalidate the
    original control. The observer must use the actual deletion connection,
    after commit and the existing frame snapshot, without resetting it."""
    events = []

    class Connection:
        def execute(self, sql, *args):
            events.append(sql)
            values = {"page_size": 4096, "cache_size": -2000,
                      "temp_store": 1, "secure_delete": 1,
                      "journal_mode": "wal", "cache_spill": 483,
                      "synchronous": 2, "locking_mode": "normal"}
            return type("Rows", (), {"fetchone": lambda self: (
                values.get(sql.removeprefix("PRAGMA "), 0),)})()

        def commit(self):
            events.append("commit")

        def close(self):
            events.append("close")

    monkeypatch.setattr(sqlite3, "connect", lambda path: Connection())
    monkeypatch.setattr(fx, "require_attributed", lambda *args: None)

    def frames(conn):
        events.append("frames")
        return 123

    monkeypatch.setattr(sys.modules[__name__], "_cache_writes", frames)
    report = {"controls": {}}
    assert _control(tmp_path / "default.db", "claude", unbounded=False,
                    report=report) == (123, 4096)
    assert events[:7] == ["PRAGMA page_size", "BEGIN IMMEDIATE",
                          *_CLAUDE_DELETES, "commit"]
    assert events[7] == "frames"
    assert events[-1] == "close"
    entry = report["controls"]["default"]
    assert entry["observationPoint"] == "deletion-connection-after-commit-and-frame-snapshot"
    assert entry["effectiveSettings"]["cache_size"] == -2000
    assert entry["effectiveSettings"]["temp_store"] == 1
    assert entry["effectiveSettings"]["secure_delete"] == 1
    assert entry["frames"] == 123


def test_control_reporting_builder_metadata_uses_separate_observation(tmp_path):
    """Starting counts/rootpages and runtime compile facts must be captured
    on the builder, never by scanning a fresh measured control connection."""
    conn = sqlite3.connect(tmp_path / "builder.db")
    try:
        conn.execute("CREATE TABLE conversation_messages(session_id TEXT)")
        conn.executemany("INSERT INTO conversation_messages VALUES (?)",
                         [("expire-me",), ("keep-1",), ("keep-2",)])
        conn.commit()
        observe = globals().get("_observe_control_start")
        assert callable(observe), "builder metadata observer is missing"
        data = observe(conn, "claude")
        assert data["observationPoint"] == "builder-after-copies-before-controls"
        assert data["seededCounts"]["conversation_messages"] == {
            "expire-me": 1, "keep-1": 1, "keep-2": 1}
        assert data["schema"][0]["name"] == "conversation_messages"
        assert data["schema"][0]["rootpage"] == 2
        assert data["sqliteVersion"] == sqlite3.sqlite_version
        assert isinstance(data["compileOptions"], list) and data["compileOptions"]
    finally:
        conn.close()
