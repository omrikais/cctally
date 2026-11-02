"""Crash-safe cache.db corruption recovery.

Regression for the 2026-07-23 dashboard SIGBUS: the legacy opener unlinked
cache.db in place while another process still held a WAL reader.  The live
reader then faulted in sqlite3.walFindFrame after the mapped file was shortened.
"""
from __future__ import annotations
import types

import ast
import errno
import hashlib
import json
import os
import pathlib
import signal
import sqlite3
import struct
import subprocess
import sys
import threading
import time

import pytest

from conftest import load_script, redirect_paths
from tests._support_http import PRESENCE_BACKSTOP_SECONDS


ROOT = pathlib.Path(__file__).resolve().parents[1]

POST_OPEN_RECOVERY_ORIGINS = (
    "cache_sync.cli.claude",
    "cache_sync.cli.codex",
    "claude.entries.sync",
    "claude.session_entries.sync",
    "codex.entries.sync",
    "codex.quota.sync",
    "dashboard.conversation.codex_sync",
    "dashboard.refresh.claude_sync",
    "dashboard.refresh.codex_sync",
    "dashboard.refresh.frontier_commit",
    "hook.claude.sync",
    "hook.codex_quota.sync",
    "setup.bootstrap.claude_sync",
    "source.analytics.codex_sync",
    "source.report.codex_sync",
    "view_model.claude.sync",
    "view_model.codex.sync",
)


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return (
        ns,
        sys.modules["_cctally_core"],
        sys.modules["_cctally_store"],
    )


def _family_snapshot(path: pathlib.Path) -> dict[str, tuple[int, int, str]]:
    snapshot = {}
    for suffix in ("", "-wal", "-shm"):
        member = pathlib.Path(f"{path}{suffix}")
        if member.exists():
            stat = member.stat()
            snapshot[member.name] = (
                stat.st_ino,
                stat.st_size,
                hashlib.sha256(member.read_bytes()).hexdigest(),
            )
    return snapshot


def test_corrupt_open_preserves_family_while_live_reader_exists(
    tmp_path, monkeypatch,
):
    ns, core, store = _load(tmp_path, monkeypatch)
    live = ns["open_cache_db"]()
    path = pathlib.Path(core.CACHE_DB_PATH)
    inode = path.stat().st_ino

    def corrupt_open(_store):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(store, "open_index", corrupt_open)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="still open"):
            ns["open_cache_db"]()

        assert path.exists()
        assert path.stat().st_ino == inode
        assert live.execute("PRAGMA schema_version").fetchone() is not None
        assert not path.with_name("cache.db.repairing").exists()
    finally:
        live.close()


def test_classified_open_trigger_preserves_healthy_populated_cache(
    tmp_path, monkeypatch, capsys,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
            ("issue-404-healthy-row", "preserve-me"),
        )
        conn.commit()
    finally:
        conn.close()

    before_inode = path.stat().st_ino
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    real_open = cache_mod._cache_open_guarded
    attempts = 0

    def classified_failure_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_open()

    monkeypatch.setattr(cache_mod, "_cache_open_guarded", classified_failure_once)
    observed = None
    try:
        reopened = ns["open_cache_db"]()
    except sqlite3.DatabaseError as exc:
        observed = exc
    else:
        reopened.close()

    bundles = sorted(
        core.LOG_DIR.glob("cache.db-corruption-forensics-*.json")
    )
    assert len(bundles) == 1
    bundle = json.loads(bundles[0].read_text())
    assert bundle["integrityCheck"] == ["ok"]
    assert bundle["probeDisposition"] == "unconfirmed"
    assert bundle["probeReason"] == "integrity_check_ok"
    assert bundle["trigger"]["origin"] == "cache.open"
    assert bundle["trigger"]["exceptionType"].endswith(".DatabaseError")
    assert bundle["trigger"]["message"] == "database disk image is malformed"
    assert bundle["trigger"]["sqliteErrorCode"] is None
    assert bundle["trigger"]["sqliteErrorName"] is None
    assert bundle["trigger"]["tracebackCallSite"]["function"] == (
        "classified_failure_once"
    )
    assert path.stat().st_ino == before_inode
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert observed is not None
    assert str(observed) == "database disk image is malformed"
    check = sqlite3.connect(path)
    try:
        assert check.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            ("issue-404-healthy-row",),
        ).fetchone() == ("preserve-me",)
    finally:
        check.close()
    assert not list((path.parent / "quarantine").glob("cache.db-*"))
    assert not path.with_name("cache.db.quarantine-pending.json").exists()
    assert not path.with_name("cache.db.repairing").exists()
    diagnostic = capsys.readouterr().err
    assert "destructive recovery declined" in diagnostic
    assert "cache.open" in diagnostic
    assert str(bundles[0]) in diagnostic


@pytest.mark.parametrize("origin", POST_OPEN_RECOVERY_ORIGINS)
def test_each_post_open_origin_preserves_live_wal_family_byte_for_byte(
    tmp_path, monkeypatch, origin,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    conn = ns["open_cache_db"]()
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
        ("issue-404-live-wal", "preserve-all-members"),
    )
    conn.commit()
    before = _family_snapshot(path)
    assert set(before) == {"cache.db", "cache.db-wal", "cache.db-shm"}

    trigger = sqlite3.DatabaseError("database disk image is malformed")

    def classified_ingest_failure(_active):
        raise trigger

    with pytest.raises(
        sqlite3.DatabaseError, match="database disk image is malformed",
    ) as caught:
        cache_mod._run_cache_operation_with_recovery(
            conn,
            classified_ingest_failure,
            origin=origin,
        )

    assert caught.value is trigger
    bundle_path = next(
        core.LOG_DIR.glob("cache.db-corruption-forensics-*.json")
    )
    bundle = json.loads(bundle_path.read_text())
    assert bundle["integrityCheck"] == ["ok"]
    assert bundle["probeDisposition"] == "unconfirmed"
    assert bundle["trigger"]["origin"] == origin
    assert bundle["trigger"]["tracebackCallSite"]["function"] == (
        "classified_ingest_failure"
    )
    assert _family_snapshot(path) == before
    check = sqlite3.connect(path)
    try:
        assert check.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            ("issue-404-live-wal",),
        ).fetchone() == ("preserve-all-members",)
    finally:
        check.close()
    assert not list((path.parent / "quarantine").glob("cache.db-*"))
    assert not path.with_name("cache.db.quarantine-pending.json").exists()
    assert not path.with_name("cache.db.repairing").exists()


def test_writer_before_marker_is_retained_without_stale_shm_restore(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    conn = ns["open_cache_db"]()
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
        ("issue-404-trigger-writer", "original"),
    )
    conn.commit()
    real_claim = db_mod._claim_repair_marker
    expected = None

    def writer_then_claim(db_path):
        nonlocal expected
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sqlite3,sys;"
                    "c=sqlite3.connect(sys.argv[1]);"
                    "c.execute("
                    "\"INSERT OR REPLACE INTO cache_meta(key,value) "
                    "VALUES('issue-404-racing-writer','retained')\""
                    ");c.commit();c.close()"
                ),
                str(path),
            ],
            check=True,
            timeout=10,
        )
        expected = _family_snapshot(path)
        return real_claim(db_path)

    trigger = sqlite3.DatabaseError("database disk image is malformed")

    def classified_failure(_active):
        raise trigger

    monkeypatch.setattr(db_mod, "_claim_repair_marker", writer_then_claim)
    with pytest.raises(sqlite3.DatabaseError) as caught:
        cache_mod._run_cache_operation_with_recovery(
            conn,
            classified_failure,
            origin="claude.entries.sync",
        )

    assert caught.value is trigger
    assert expected is not None
    assert _family_snapshot(path) == expected
    check = sqlite3.connect(path)
    try:
        assert check.execute(
            "SELECT value FROM cache_meta WHERE key='issue-404-racing-writer'"
        ).fetchone() == ("retained",)
    finally:
        check.close()
    assert not list((path.parent / "quarantine").glob("cache.db-*"))
    assert not path.with_name("cache.db.repairing").exists()


def test_recovery_call_sites_use_the_complete_origin_vocabulary():
    helpers = {
        "_recover_corrupt_cache",
        "_run_cache_operation_with_recovery",
        "_run_cache_plan_with_recovery",
    }
    observed = set()
    for source_path in (ROOT / "bin").glob("_cctally*.py"):
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id in {"plan_origins", "operation_origins"}
                    for target in node.targets
                )
                and isinstance(node.value, ast.List)
            ):
                observed.update(
                    item.value for item in node.value.elts
                    if isinstance(item, ast.Constant)
                )
                continue
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"plan_origins", "operation_origins"}
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
            ):
                observed.add(node.args[0].value)
                continue
            if isinstance(node.func, ast.Attribute):
                helper = node.func.attr
            elif isinstance(node.func, ast.Name):
                helper = node.func.id
            else:
                continue
            if helper not in helpers:
                continue
            for keyword in node.keywords:
                if keyword.arg == "origin" and isinstance(
                    keyword.value, ast.Constant,
                ):
                    observed.add(keyword.value.value)
                elif keyword.arg == "origins" and isinstance(
                    keyword.value, ast.Tuple,
                ):
                    observed.update(
                        item.value for item in keyword.value.elts
                        if isinstance(item, ast.Constant)
                    )

    assert observed == {"cache.open", *POST_OPEN_RECOVERY_ORIGINS}


def test_python_311_close_policy_compat_preserves_live_wal_family(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    path = pathlib.Path(core.CACHE_DB_PATH)
    conn = ns["open_cache_db"]()
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
        ("issue-404-python-311", "preserve"),
    )
    conn.commit()
    before = _family_snapshot(path)

    cache_mod = sys.modules["_cctally_cache"]
    cache_mod._set_cache_no_checkpoint_on_close_cpython(conn, True)
    conn.close()

    assert _family_snapshot(path) == before


def test_ordinary_healthy_open_does_not_require_close_policy_adapter(
    tmp_path, monkeypatch,
):
    ns, _core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]

    def unavailable_adapter(*_args, **_kwargs):
        raise sqlite3.NotSupportedError("alternate Python 3.11 runtime")

    monkeypatch.setattr(
        cache_mod, "_set_cache_no_checkpoint_on_close", unavailable_adapter,
    )
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_alternate_python_keeper_fallback_preserves_live_wal_family(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    conn = ns["open_cache_db"]()
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
        ("issue-404-keeper-fallback", "preserve"),
    )
    conn.commit()
    before = _family_snapshot(path)

    def unavailable_adapter(*_args, **_kwargs):
        raise sqlite3.NotSupportedError("alternate Python 3.11 runtime")

    trigger = sqlite3.DatabaseError("database disk image is malformed")

    def classified_failure(_active):
        raise trigger

    monkeypatch.setattr(
        cache_mod, "_set_cache_no_checkpoint_on_close", unavailable_adapter,
    )
    with pytest.raises(sqlite3.DatabaseError) as caught:
        cache_mod._run_cache_operation_with_recovery(
            conn,
            classified_failure,
            origin="claude.entries.sync",
        )

    assert caught.value is trigger
    assert _family_snapshot(path) == before
    assert not list((path.parent / "quarantine").glob("cache.db-*"))
    assert not path.with_name("cache.db.repairing").exists()


def test_non_ok_integrity_row_confirms_forensics_probe(tmp_path, monkeypatch):
    _ns, core, _store = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"forensics fixture")

    class FakeIntegrityConnection:
        def execute(self, _sql):
            return self

        def fetchall(self):
            return [("*** in database main *** page 2 is malformed",)]

        def close(self):
            return None

    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_sqlite3 = types.SimpleNamespace(**vars(db_mod.sqlite3))
    _iso_sqlite3.connect = lambda *_args, **_kwargs: FakeIntegrityConnection()
    monkeypatch.setattr(db_mod, "sqlite3", _iso_sqlite3)
    result = db_mod.write_corruption_forensics(
        path,
        db_label="cache",
        trigger_origin="test.non_ok_probe",
        trigger_exception=sqlite3.DatabaseError(
            "database disk image is malformed"
        ),
        return_result=True,
    )

    assert isinstance(result, db_mod.CorruptionForensicsResult)
    assert result.disposition is db_mod.CorruptionProbeDisposition.CONFIRMED
    assert result.reason == "integrity_check_non_ok"
    assert result.integrity_check == (
        "*** in database main *** page 2 is malformed",
    )
    bundle = json.loads(result.path.read_text())
    assert bundle["probeDisposition"] == "confirmed"
    assert bundle["probeReason"] == "integrity_check_non_ok"


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [("ok",), ("ok",)],
    ],
)
def test_empty_or_multiple_ok_integrity_rows_are_unconfirmed(
    tmp_path, monkeypatch, rows,
):
    _ns, core, _store = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"forensics fixture")

    class FakeIntegrityConnection:
        def execute(self, _sql):
            return self

        def fetchall(self):
            return rows

        def close(self):
            return None

    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_sqlite3 = types.SimpleNamespace(**vars(db_mod.sqlite3))
    _iso_sqlite3.connect = lambda *_args, **_kwargs: FakeIntegrityConnection()
    monkeypatch.setattr(db_mod, "sqlite3", _iso_sqlite3)
    result = db_mod.write_corruption_forensics(
        path,
        db_label="cache",
        trigger_origin="test.inconclusive_probe",
        trigger_exception=sqlite3.DatabaseError(
            "database disk image is malformed"
        ),
        return_result=True,
    )

    assert result.disposition is db_mod.CorruptionProbeDisposition.UNCONFIRMED
    assert result.reason == "integrity_check_inconclusive"
    assert result.integrity_check == tuple(row[0] for row in rows)
    bundle = json.loads(result.path.read_text())
    assert bundle["probeDisposition"] == "unconfirmed"
    assert bundle["probeReason"] == "integrity_check_inconclusive"


def test_trigger_metadata_is_bounded_and_keeps_sqlite_identity(
    tmp_path, monkeypatch,
):
    _ns, core, _store = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    broken = pathlib.Path(core.CACHE_DB_PATH)
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError) as caught:
        conn = sqlite3.connect(f"file:{broken}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()

    record = db_mod._corruption_trigger_record(
        "o" * 500,
        caught.value,
    )
    assert len(record["origin"]) == db_mod._FORENSICS_ORIGIN_MAX
    assert record["origin"].endswith("…")
    assert record["sqliteErrorCode"] in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
    }
    assert record["sqliteErrorName"] in {
        "SQLITE_CORRUPT",
        "SQLITE_NOTADB",
    }
    assert record["tracebackCallSite"]["file"] == (
        "test_cache_corruption_recovery.py"
    )
    assert record["tracebackCallSite"]["function"] == (
        "test_trigger_metadata_is_bounded_and_keeps_sqlite_identity"
    )

    long_record = db_mod._corruption_trigger_record(
        "test.long_message",
        sqlite3.DatabaseError("x" * 1000),
    )
    assert len(long_record["message"]) == (
        db_mod._FORENSICS_EXCEPTION_MESSAGE_MAX
    )
    assert long_record["message"].endswith("…")


def test_forensics_default_return_remains_path_compatible(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    path = pathlib.Path(core.CACHE_DB_PATH)
    ns["open_cache_db"]().close()
    db_mod = sys.modules["_cctally_db"]

    result = db_mod.write_corruption_forensics(path, db_label="stats")

    assert isinstance(result, pathlib.Path)
    assert result.is_file()
    assert json.loads(result.read_text())["probeDisposition"] == "unconfirmed"


def test_unrelated_probe_failure_declines_quarantine(tmp_path, monkeypatch):
    _ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"preserve unrelated probe failure")
    before_inode = path.stat().st_ino
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    def unrelated_probe_failure(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_sqlite3 = types.SimpleNamespace(**vars(db_mod.sqlite3))
    _iso_sqlite3.connect = unrelated_probe_failure
    monkeypatch.setattr(db_mod, "sqlite3", _iso_sqlite3)
    assert cache_mod._recover_corrupt_cache(
        sqlite3.DatabaseError("database disk image is malformed"),
        origin="test.unrelated_probe_failure",
    ) is False

    bundle_path = next(
        core.LOG_DIR.glob("cache.db-corruption-forensics-*.json")
    )
    bundle = json.loads(bundle_path.read_text())
    assert bundle["integrityCheck"] == "error: database is locked"
    assert bundle["probeDisposition"] == "unconfirmed"
    assert bundle["probeReason"] == "integrity_check_unavailable"
    assert path.stat().st_ino == before_inode
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert not list((path.parent / "quarantine").glob("cache.db-*"))
    assert not path.with_name("cache.db.repairing").exists()


def test_forensics_write_failure_overrides_confirmed_probe(
    tmp_path, monkeypatch,
):
    _ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    before_inode = path.stat().st_ino
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    real_publish = db_mod._atomic_write_private_json

    def fail_forensics_write(output, payload):
        if "corruption-forensics" in pathlib.Path(output).name:
            raise OSError("injected forensics write failure")
        return real_publish(output, payload)

    monkeypatch.setattr(
        db_mod, "_atomic_write_private_json", fail_forensics_write,
    )
    assert cache_mod._recover_corrupt_cache(
        sqlite3.DatabaseError("file is not a database"),
        origin="test.forensics_write_failure",
    ) is False

    assert not list(core.LOG_DIR.glob("cache.db-corruption-forensics-*.json"))
    assert path.stat().st_ino == before_inode
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert not list((path.parent / "quarantine").glob("cache.db-*"))
    assert not path.with_name("cache.db.quarantine-pending.json").exists()
    assert not path.with_name("cache.db.repairing").exists()


def test_unavailable_forensics_preserves_original_exception_and_family(
    tmp_path, monkeypatch, capsys,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    conn = ns["open_cache_db"]()
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
        ("issue-404-forensics-unavailable", "preserve"),
    )
    conn.commit()
    before = _family_snapshot(path)
    trigger = sqlite3.DatabaseError("database disk image is malformed")

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("injected forensics outage")

    def classified_failure(_active):
        raise trigger

    monkeypatch.setattr(db_mod, "write_corruption_forensics", unavailable)
    with pytest.raises(sqlite3.DatabaseError) as caught:
        cache_mod._run_cache_operation_with_recovery(
            conn,
            classified_failure,
            origin="claude.entries.sync",
        )

    assert caught.value is trigger
    assert _family_snapshot(path) == before
    assert not list(core.LOG_DIR.glob("cache.db-corruption-forensics-*.json"))
    assert not list((path.parent / "quarantine").glob("cache.db-*"))
    assert not path.with_name("cache.db.quarantine-pending.json").exists()
    assert not path.with_name("cache.db.repairing").exists()
    diagnostic = capsys.readouterr().err
    assert "forensics was unavailable" in diagnostic
    assert "claude.entries.sync" in diagnostic


def test_idle_corrupt_cache_quarantines_whole_family_then_recreates(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    pathlib.Path(str(path) + "-wal").write_bytes(b"forensic wal")
    pathlib.Path(str(path) + "-shm").write_bytes(b"forensic shm")

    # Inject the already-observed corruption result before SQLite gets a chance
    # to discard deliberately synthetic sidecars as invalid. The retry uses the
    # real guarded opener against the freshly quarantined path.
    real_open = cache_mod._cache_open_guarded
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_open()

    monkeypatch.setattr(cache_mod, "_cache_open_guarded", fail_once)
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    incidents = sorted((path.parent / "quarantine").glob("cache.db-*"))
    assert len(incidents) == 1
    assert {
        item.name for item in incidents[0].iterdir()
    } >= {"cache.db", "cache.db-wal", "cache.db-shm", "manifest.json"}
    assert not path.with_name("cache.db.repairing").exists()


def test_partial_family_quarantine_fails_closed_then_resumes(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    pathlib.Path(f"{path}-wal").write_bytes(b"forensic wal")
    pathlib.Path(f"{path}-shm").write_bytes(b"forensic shm")
    real_replace = db_mod.os.replace
    failed = False

    def fail_shm_once(src, dst):
        nonlocal failed
        if not failed and str(src).endswith("cache.db-shm"):
            failed = True
            raise OSError("injected sidecar move failure")
        return real_replace(src, dst)

    # #630 S2: patch the IMPORTER's reference, never the shared stdlib
    # module object, which every other importer and every concurrent
    # thread resolves through. The later line flips behaviour on the
    # copy already installed, so the restore does not need a second
    # setattr.
    _iso_os = types.SimpleNamespace(**vars(db_mod.os))
    _iso_os.replace = fail_shm_once
    monkeypatch.setattr(db_mod, "os", _iso_os)
    with pytest.raises(
        sqlite3.DatabaseError, match="could not complete whole-family quarantine"
    ):
        cache_mod._recover_corrupt_cache(
            sqlite3.DatabaseError("database disk image is malformed"),
            origin="test.partial_quarantine",
        )

    pending = db_mod._quarantine_pending_path(path)
    assert pending.exists()
    assert path.exists(), "main DB must not be recreated after a partial move"

    _iso_os.replace = real_replace
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert not pending.exists()
    incidents = list((path.parent / "quarantine").glob("cache.db-*"))
    assert len(incidents) == 1
    assert {
        item.name for item in incidents[0].iterdir()
    } >= {"cache.db", "cache.db-wal", "cache.db-shm", "manifest.json"}


def test_cache_repair_marker_blocks_new_open(tmp_path, monkeypatch):
    ns, core, _store = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    path = pathlib.Path(core.CACHE_DB_PATH)
    inode = path.stat().st_ino
    marker = path.with_name("cache.db.repairing")
    marker.write_text(f"{os.getpid()}\n")

    with pytest.raises(sqlite3.DatabaseError, match="maintenance"):
        ns["open_cache_db"]()

    assert path.stat().st_ino == inode


@pytest.mark.parametrize("marker_form", ["file", "directory"])
def test_dead_cache_repair_marker_is_reclaimed_before_open(
    tmp_path, monkeypatch, marker_form,
):
    """A dead owner's marker, in either #1020 form, never wedges the opener."""
    ns, core, _store = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    path = pathlib.Path(core.CACHE_DB_PATH)
    inode = path.stat().st_ino
    marker = path.with_name("cache.db.repairing")
    _write_owner_marker(marker, "999999999\n", marker_form)

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    assert path.stat().st_ino == inode
    assert not os.path.lexists(marker)
    assert _owner_record_debris(marker) == []


def test_malformed_cache_repair_marker_is_reclaimed_before_open(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    path = pathlib.Path(core.CACHE_DB_PATH)
    marker = path.with_name("cache.db.repairing")
    marker.write_text("{truncated-owner-record")

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    assert not marker.exists()


def test_cache_repair_marker_rejects_reused_pid_identity(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    ns["open_cache_db"]().close()
    path = pathlib.Path(core.CACHE_DB_PATH)
    marker = path.with_name("cache.db.repairing")
    current_identity = db_mod._process_start_identity(os.getpid())
    assert current_identity
    marker.write_text(
        db_mod._encode_repair_owner(
            pid=os.getpid(),
            process_start=current_identity + "-reused",
            claim_id="old-claim",
        )
    )

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    assert not marker.exists()


def test_cache_repair_owner_fallback_identity_is_caller_timezone_independent(
    tmp_path, monkeypatch,
):
    """macOS has no /proc, so its live-owner decision always uses this fallback."""
    _ns, _core, _store = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    real_read_text = pathlib.Path.read_text

    def without_proc(self, *args, **kwargs):
        if str(self).startswith("/proc/") and self.name == "stat":
            raise OSError("force the macOS ps fallback")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", without_proc)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    tokyo = db_mod._process_start_identity(os.getpid())
    monkeypatch.setenv("TZ", "America/New_York")
    new_york = db_mod._process_start_identity(os.getpid())

    assert tokyo, "the fallback must identify this live pytest process"
    assert tokyo == new_york


# --- #1020: owner records on a volume without hard links -------------------


def _no_hard_links(monkeypatch, db_mod, code=errno.ENOTSUP):
    """Rebind ``_cctally_db``'s ``os`` to a copy whose ``link`` fails as a
    volume without hard links (exFAT) fails; the module-local seam of #630
    S2, never the shared stdlib object."""
    iso = types.SimpleNamespace(**vars(db_mod.os))

    def link(src, dst, *args, **kwargs):
        raise OSError(code, os.strerror(code), str(src), None, str(dst))

    iso.link = link
    monkeypatch.setattr(db_mod, "os", iso)
    return iso


def _write_owner_marker(marker: pathlib.Path, record: str, form: str) -> None:
    """Publish ``record`` at ``marker`` as a file or as a directory holding
    ``owner`` (the #1020 directory form)."""
    if form == "file":
        marker.write_text(record)
    else:
        marker.mkdir()
        (marker / "owner").write_text(record)


def _owner_record_debris(marker: pathlib.Path) -> list:
    """Private ``.d`` directories, ``.tmp`` files and ``.removed`` tombs left
    beside ``marker``."""
    return sorted(
        entry.name for entry in marker.parent.iterdir()
        if entry.name.startswith(f".{marker.name}.")
    )


def _owner_store(tmp_path, monkeypatch):
    _ns, core, _store = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    return db_mod, path, db_mod._repair_marker_path(path)


def _live_record(db_mod, claim_id="live-claim") -> str:
    identity = db_mod._process_start_identity(os.getpid())
    assert identity
    return db_mod._encode_repair_owner(
        pid=os.getpid(), process_start=identity, claim_id=claim_id,
    )


@pytest.mark.parametrize("form", ["hard_link", "no_hard_link"])
def test_owner_record_claim_read_live_and_release_in_each_form(
    tmp_path, monkeypatch, form,
):
    """#1020 T1. Detects: the exFAT claim failure that blocks every recovery;
    a hard-link volume regressing to the directory form; a release that
    removes another owner's marker."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    if form == "no_hard_link":
        _no_hard_links(monkeypatch, db_mod)

    claim, reason = db_mod._claim_repair_marker(path)

    assert isinstance(claim, db_mod.RepairMarkerClaim), reason
    if form == "hard_link":
        assert marker.is_file() and not marker.is_symlink()
    else:
        assert marker.is_dir() and not marker.is_symlink()
        assert sorted(item.name for item in marker.iterdir()) == ["owner"]
        record = json.loads((marker / "owner").read_text())
        assert record["claim_id"] == claim.claim_id
    assert db_mod._read_repair_owner(marker)["claim_id"] == claim.claim_id
    assert db_mod._repair_marker_is_live(marker)[0] is True
    second, refusal = db_mod._claim_repair_marker(path)
    assert second is None
    assert str(marker) in refusal and "another cache.db repair" in refusal
    stranger = db_mod.RepairMarkerClaim(marker=marker, claim_id="not-mine")
    with pytest.raises(OSError, match="ownership changed"):
        db_mod._release_repair_marker(path, stranger)
    assert db_mod._read_repair_owner(marker)["claim_id"] == claim.claim_id

    db_mod._release_repair_marker(path, claim)

    assert not os.path.lexists(marker)
    assert _owner_record_debris(marker) == []


@pytest.mark.parametrize("form", ["hard_link", "no_hard_link"])
def test_owner_record_release_needs_no_new_directory_entry(
    tmp_path, monkeypatch, form,
):
    """#1020 review. Detects: a release on a full disk that cannot create its
    removal-lock file and so leaves this process's own live record fencing
    the store (the base's plain unlink needed no new directory entry)."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    if form == "no_hard_link":
        iso = _no_hard_links(monkeypatch, db_mod)
    else:
        iso = types.SimpleNamespace(**vars(db_mod.os))
        monkeypatch.setattr(db_mod, "os", iso)
    claim, reason = db_mod._claim_repair_marker(path)
    assert claim is not None, reason
    real_open = iso.open

    def full_disk_open(file, flags, *args, **kwargs):
        if flags & os.O_CREAT and not os.path.lexists(file):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(file))
        return real_open(file, flags, *args, **kwargs)

    iso.open = full_disk_open

    db_mod._release_repair_marker(path, claim)

    assert not os.path.lexists(marker)
    assert _owner_record_debris(marker) == []


def test_owner_record_directory_is_complete_before_its_name_exists(
    tmp_path, monkeypatch,
):
    """#1020 T2. Detects: a partial record visible at the marker name, which
    an opener would read as malformed and reclaim under a running recovery."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    iso = _no_hard_links(monkeypatch, db_mod)
    real_rename = os.rename
    seen = []

    def observe_rename(src, dst, *args, **kwargs):
        if pathlib.Path(dst) == marker:
            src = pathlib.Path(src)
            seen.append({
                "marker_exists": os.path.lexists(marker),
                "private": src.name,
                "entries": sorted(item.name for item in src.iterdir()),
                "record": json.loads((src / "owner").read_text()),
            })
        return real_rename(src, dst, *args, **kwargs)

    iso.rename = observe_rename

    claim, reason = db_mod._claim_repair_marker(path)

    assert claim is not None, reason
    assert len(seen) == 1
    assert seen[0]["marker_exists"] is False
    assert seen[0]["private"] == f".{marker.name}.{claim.claim_id}.d"
    assert seen[0]["entries"] == ["owner"]
    assert seen[0]["record"]["claim_id"] == claim.claim_id
    assert seen[0]["record"]["pid"] == os.getpid()
    db_mod._release_repair_marker(path, claim)


@pytest.mark.parametrize("code,platform", [
    (errno.EACCES, "darwin"),
    (errno.EIO, "darwin"),
    (errno.EPERM, "darwin"),
], ids=["EACCES", "EIO", "darwin-EPERM"])
def test_owner_record_link_failures_that_are_not_missing_hard_links_propagate(
    tmp_path, monkeypatch, code, platform,
):
    """#1020 T2. Detects: a fallback that masks a real permission or I/O
    failure behind a directory-form publication."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    _no_hard_links(monkeypatch, db_mod, code)
    monkeypatch.setattr(sys, "platform", platform)

    with pytest.raises(OSError) as raised:
        db_mod._claim_repair_marker(path)

    assert raised.value.errno == code
    assert not os.path.lexists(marker)
    assert _owner_record_debris(marker) == []


def test_owner_record_linux_eperm_publishes_the_directory_form(
    tmp_path, monkeypatch,
):
    """#1020 T2. Detects: Linux vfat/exfat (link(2) EPERM) left unable to
    claim."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    _no_hard_links(monkeypatch, db_mod, errno.EPERM)
    monkeypatch.setattr(sys, "platform", "linux")

    claim, reason = db_mod._claim_repair_marker(path)

    assert claim is not None, reason
    assert marker.is_dir()
    assert db_mod._read_repair_owner(marker)["claim_id"] == claim.claim_id


def test_owner_record_rename_failure_that_is_not_contention_propagates(
    tmp_path, monkeypatch,
):
    """#1020 T2. Detects: an I/O failure publishing the directory form
    reported as contention, or its private directory left behind."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    iso = _no_hard_links(monkeypatch, db_mod)
    real_rename = os.rename

    def failing_rename(src, dst, *args, **kwargs):
        if pathlib.Path(dst) == marker:
            raise OSError(errno.EIO, os.strerror(errno.EIO), str(src))
        return real_rename(src, dst, *args, **kwargs)

    iso.rename = failing_rename

    with pytest.raises(OSError) as raised:
        db_mod._claim_repair_marker(path)

    assert raised.value.errno == errno.EIO
    assert not os.path.lexists(marker)
    assert _owner_record_debris(marker) == []


def _dead_identity_record(db_mod) -> str:
    return db_mod._encode_repair_owner(
        pid=999_999_999, process_start="ps:dead", claim_id="dead-claim",
    )


def _reused_pid_record(db_mod) -> str:
    identity = db_mod._process_start_identity(os.getpid())
    assert identity
    return db_mod._encode_repair_owner(
        pid=os.getpid(), process_start=identity + "-reused",
        claim_id="reused-claim",
    )


@pytest.mark.parametrize("claimant", ["hard_link", "no_hard_link"])
@pytest.mark.parametrize("marker_form", ["file", "directory"])
@pytest.mark.parametrize("record_kind", [
    "dead-pid", "reused-pid", "malformed", "directory-without-owner",
])
def test_stale_owner_records_are_reclaimed_in_either_form(
    tmp_path, monkeypatch, record_kind, marker_form, claimant,
):
    """#1020 T3. Detects: a stale owner record (in either form) that a
    claimant of either form can never reclaim, wedging every recovery."""
    if record_kind == "directory-without-owner" and marker_form == "file":
        pytest.skip("a file marker always carries its own record")
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    if record_kind == "directory-without-owner":
        marker.mkdir()
        (marker / "._owner").write_bytes(b"apple-double companion")
    else:
        record = {
            "dead-pid": _dead_identity_record,
            "reused-pid": _reused_pid_record,
            "malformed": lambda _db: "{truncated-owner-record",
        }[record_kind](db_mod)
        _write_owner_marker(marker, record, marker_form)
    if claimant == "no_hard_link":
        _no_hard_links(monkeypatch, db_mod)

    claim, reason = db_mod._claim_repair_marker(path)

    assert claim is not None, reason
    assert db_mod._read_repair_owner(marker)["claim_id"] == claim.claim_id
    db_mod._release_repair_marker(path, claim)
    assert not os.path.lexists(marker)
    assert _owner_record_debris(marker) == []


@pytest.mark.parametrize("claimant", ["hard_link", "no_hard_link"])
@pytest.mark.parametrize("marker_form", ["file", "directory"])
def test_a_live_owner_record_in_either_form_is_refused_untouched(
    tmp_path, monkeypatch, marker_form, claimant,
):
    """#1020 T3. Detects: a live directory-form owner read as malformed and
    reclaimed under the recovery that holds it."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    record = _live_record(db_mod)
    _write_owner_marker(marker, record, marker_form)
    record_file = marker if marker_form == "file" else marker / "owner"
    if claimant == "no_hard_link":
        _no_hard_links(monkeypatch, db_mod)

    claim, reason = db_mod._claim_repair_marker(path)

    assert claim is None
    assert "another cache.db repair owns" in reason
    assert record_file.read_text() == record
    assert _owner_record_debris(marker) == []


@pytest.mark.parametrize("case", ["dead", "live-in-lease", "live-past-lease"])
def test_legacy_pid_only_owner_records_keep_their_lease(
    tmp_path, monkeypatch, case,
):
    """#1020 T3. Detects: a lease semantics change for pre-identity (legacy
    pid-only) records."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    pid = 999_999_999 if case == "dead" else os.getpid()
    marker.write_text(f"{pid}\n")
    if case == "live-past-lease":
        old = time.time() - db_mod._REPAIR_OWNER_UNKNOWN_IDENTITY_LEASE_SECONDS - 60
        os.utime(marker, (old, old))

    claim, reason = db_mod._claim_repair_marker(path)

    if case == "live-in-lease":
        assert claim is None
        assert "bounded lease" in reason
        assert marker.read_text() == f"{pid}\n"
    else:
        assert claim is not None, reason
        db_mod._release_repair_marker(path, claim)


@pytest.mark.parametrize("owner_age", ["fresh", "expired"])
def test_directory_marker_lease_is_measured_on_its_owner_file(
    tmp_path, monkeypatch, owner_age,
):
    """#1020 T3. Detects: a lease read from the directory's mtime (which an
    AppleDouble companion moves), keeping a dead lease live or expiring a
    live one."""
    db_mod, _path, marker = _owner_store(tmp_path, monkeypatch)
    _write_owner_marker(marker, _live_record(db_mod), "directory")
    monkeypatch.setattr(db_mod, "_process_start_identity", lambda _pid: None)
    old = time.time() - db_mod._REPAIR_OWNER_UNKNOWN_IDENTITY_LEASE_SECONDS - 60
    now = time.time()
    owner_time, directory_time = (now, old) if owner_age == "fresh" else (old, now)
    os.utime(marker / "owner", (owner_time, owner_time))
    os.utime(marker, (directory_time, directory_time))

    live, reason = db_mod._repair_marker_is_live(marker)

    if owner_age == "fresh":
        assert live is True and "bounded lease" in reason
    else:
        assert live is False and "lease expired" in reason


@pytest.mark.parametrize("direction", ["file-to-directory", "directory-to-file"])
def test_an_owner_record_changing_form_mid_read_is_read_again(
    tmp_path, monkeypatch, direction,
):
    """#1020 T3. Detects: a record released in one form and republished in
    the other between the resolver's lstat and its read, judged malformed
    and reclaimed although its owner is live."""
    db_mod, _path, marker = _owner_store(tmp_path, monkeypatch)
    before, after = direction.split("-to-")
    record = _live_record(db_mod, claim_id="swapped-claim")
    _write_owner_marker(marker, record, before)
    real_resolve = db_mod._owner_record_file
    calls = []

    def resolve_then_swap(target):
        resolved = real_resolve(target)
        if not calls:
            if before == "file":
                marker.unlink()
            else:
                (marker / "owner").unlink()
                marker.rmdir()
            _write_owner_marker(marker, record, after)
        calls.append(resolved)
        return resolved

    monkeypatch.setattr(db_mod, "_owner_record_file", resolve_then_swap)

    owner = db_mod._read_repair_owner(marker)

    assert owner is not None and owner["claim_id"] == "swapped-claim"
    assert len(calls) == 2
    assert db_mod._repair_marker_is_live(marker)[0] is True


def test_a_failed_tomb_cleanup_leaves_the_marker_name_to_its_next_owner(
    tmp_path, monkeypatch,
):
    """#1020 T3. Detects: tomb debris that blocks or is mistaken for the next
    owner's directory marker."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    _no_hard_links(monkeypatch, db_mod)
    monkeypatch.setattr(db_mod, "shutil", types.SimpleNamespace(
        **{**vars(db_mod.shutil), "rmtree": lambda *_a, **_k: None}
    ))
    claim, reason = db_mod._claim_repair_marker(path)
    assert claim is not None, reason

    db_mod._release_repair_marker(path, claim)

    assert not os.path.lexists(marker)
    tombs = [name for name in _owner_record_debris(marker)
             if name.endswith(".removed")]
    assert len(tombs) == 1, _owner_record_debris(marker)
    successor, reason = db_mod._claim_repair_marker(path)
    assert successor is not None, reason
    assert db_mod._read_repair_owner(marker)["claim_id"] == successor.claim_id


def test_a_directory_removal_whose_barrier_fails_is_not_reported_complete(
    tmp_path, monkeypatch,
):
    """#1020 T4b. Detects: a removal reported complete before its parent
    fsync, or a tomb deleted before the barrier."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    _write_owner_marker(marker, _dead_identity_record(db_mod), "directory")
    real_fsync_directory = db_mod._fsync_directory

    def failing_parent_fsync(target):
        if pathlib.Path(target) == marker.parent:
            raise OSError(errno.EIO, "injected parent fsync failure")
        return real_fsync_directory(target)

    monkeypatch.setattr(db_mod, "_fsync_directory", failing_parent_fsync)

    with pytest.raises(OSError, match="injected parent fsync failure"):
        db_mod._remove_owner_record(marker)

    assert not os.path.lexists(marker)
    tombs = [name for name in _owner_record_debris(marker)
             if name.endswith(".removed")]
    assert len(tombs) == 1
    assert (marker.parent / tombs[0] / "owner").is_file()


@pytest.mark.parametrize("marker_form,claimant", [
    ("file", "hard_link"), ("directory", "no_hard_link"),
])
def test_two_claimants_over_one_stale_record_yield_exactly_one_owner(
    tmp_path, monkeypatch, marker_form, claimant,
):
    """#1020 T4 (deterministic). Detects: double ownership through stale
    reclaim, where a delayed reclaimer removes the record a faster claimant
    just published and both run a destructive recovery."""
    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    _write_owner_marker(marker, _dead_identity_record(db_mod), marker_form)
    if claimant == "no_hard_link":
        _no_hard_links(monkeypatch, db_mod)
    real_live = db_mod._repair_marker_is_live
    real_remove_stale = db_mod._remove_stale_repair_marker
    b_judged = threading.Event()
    b_resume = threading.Event()
    a_reclaiming = threading.Event()

    def observed_remove_stale(target):
        # A's reclaim has begun (its identity lookup and link attempt are
        # behind it), so the window below observes A against B's held
        # judgment rather than A still starting up on a loaded runner.
        if threading.current_thread().name == "claimant-A":
            a_reclaiming.set()
        return real_remove_stale(target)

    def held_judgment(target):
        verdict = real_live(target)
        if (
            threading.current_thread().name == "claimant-B"
            and not b_judged.is_set()
        ):
            b_judged.set()
            b_resume.wait(PRESENCE_BACKSTOP_SECONDS)
        return verdict

    monkeypatch.setattr(db_mod, "_repair_marker_is_live", held_judgment)
    monkeypatch.setattr(
        db_mod, "_remove_stale_repair_marker", observed_remove_stale,
    )
    results = {}

    def claim(name):
        try:
            results[name] = db_mod._claim_repair_marker(path)
        except BaseException as exc:  # noqa: BLE001 — reported below
            results[name] = exc

    b = threading.Thread(
        target=claim, args=("B",), name="claimant-B", daemon=True,
    )
    a = threading.Thread(
        target=claim, args=("A",), name="claimant-A", daemon=True,
    )
    b.start()
    try:
        assert b_judged.wait(PRESENCE_BACKSTOP_SECONDS), "B never judged"
        a.start()
        assert a_reclaiming.wait(PRESENCE_BACKSTOP_SECONDS), (
            "A never reached its stale reclaim"
        )
        a.join(1.0)
        a_returned_while_b_held = not a.is_alive()
        holder_while_b_held = (
            db_mod._read_repair_owner(marker) or {}
        ).get("claim_id")
    finally:
        b_resume.set()
        b.join(PRESENCE_BACKSTOP_SECONDS)
        if a.ident is not None:
            a.join(PRESENCE_BACKSTOP_SECONDS)
    assert not a.is_alive() and not b.is_alive()
    claims = [
        result[0] for result in results.values()
        if isinstance(result, tuple) and result[0] is not None
    ]
    assert len(claims) == 1, results
    refused = [
        result for result in results.values()
        if isinstance(result, tuple) and result[0] is None
    ]
    assert len(refused) == 1, results
    assert "another cache.db repair owns" in refused[0][1]
    assert db_mod._read_repair_owner(marker)["claim_id"] == claims[0].claim_id
    assert not a_returned_while_b_held
    assert holder_while_b_held == "dead-claim"
    db_mod._release_repair_marker(path, claims[0])


_RACE_CASES = [
    (claimant, seed)
    for claimant in ("hard_link", "no_hard_link")
    for seed in ("absent", "stale-file", "stale-directory")
]


def _owner_race_child(bin_dir, store, cases, rounds, start, done, cleared,
                      results):
    sys.path.insert(0, bin_dir)
    import _cctally_db as db

    # One consistent start identity per pid, without a `ps` per claim.
    db._process_start_identity = lambda pid: f"stub:{pid}"
    real_os = db.os
    no_links = types.SimpleNamespace(**vars(real_os))

    def link(src, dst, *args, **kwargs):
        raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP), str(src))

    no_links.link = link
    path = pathlib.Path(store)
    for claimant, _seed in cases:
        db.os = no_links if claimant == "no_hard_link" else real_os
        for _round in range(rounds):
            start.wait()
            claim = None
            try:
                claim, reason = db._claim_repair_marker(path)
                results.put(claim.claim_id if claim else None)
            except OSError as exc:
                results.put(f"error: {exc}")
            done.wait()
            if claim is not None:
                db._release_repair_marker(path, claim)
            cleared.wait()


def test_racing_claim_processes_produce_exactly_one_owner_per_round(
    tmp_path, monkeypatch,
):
    """#1020 T4 (statistical, regression evidence only). Detects: double or
    missing ownership when several processes claim one store at once, over
    an absent marker and a stale record in either form, with or without hard
    links."""
    import multiprocessing as mp

    db_mod, path, marker = _owner_store(tmp_path, monkeypatch)
    processes, rounds = 6, 20
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    stale = db_mod._encode_repair_owner(
        pid=dead.pid, process_start="stub:dead", claim_id="stale-claim",
    )
    ctx = mp.get_context("spawn")
    start, done, cleared = (
        ctx.Barrier(processes + 1, timeout=PRESENCE_BACKSTOP_SECONDS)
        for _ in range(3)
    )
    results = ctx.Queue()
    children = [
        ctx.Process(
            target=_owner_race_child,
            args=(str(ROOT / "bin"), str(path), _RACE_CASES, rounds,
                  start, done, cleared, results),
            daemon=True,
        )
        for _ in range(processes)
    ]
    for child in children:
        child.start()
    failures = []
    try:
        for claimant, seed in _RACE_CASES:
            for number in range(rounds):
                db_mod._remove_owner_record(marker)
                if seed != "absent":
                    _write_owner_marker(
                        marker, stale, seed.split("-", 1)[1],
                    )
                start.wait()
                got = [
                    results.get(timeout=PRESENCE_BACKSTOP_SECONDS)
                    for _ in range(processes)
                ]
                winners = [
                    item for item in got
                    if item and not item.startswith("error")
                ]
                holder = (db_mod._read_repair_owner(marker) or {}).get(
                    "claim_id"
                )
                if len(winners) != 1 or holder != winners[0]:
                    failures.append((claimant, seed, number, got, holder))
                done.wait()
                cleared.wait()
    except BaseException:
        # Release children parked at a barrier at once instead of letting
        # each wait out its timeout before the join below.
        for barrier in (start, done, cleared):
            barrier.abort()
        raise
    finally:
        for child in children:
            child.join(PRESENCE_BACKSTOP_SECONDS)
            if child.is_alive():
                child.kill()
        results.close()
        results.join_thread()
    assert failures == [], failures[:5]
    assert not os.path.lexists(marker)
    assert _owner_record_debris(marker) == []


_RELEASE_KINDS = {
    "repair": (
        "_claim_repair_marker", "_release_repair_marker",
        "_repair_marker_path",
    ),
    "keeper-yield": (
        "_claim_keeper_yield_request", "_release_keeper_yield_request",
        "_keeper_yield_request_path",
    ),
}


def _release_case(db_mod, kind, path):
    claim_name, release_name, path_name = _RELEASE_KINDS[kind]
    return (
        getattr(db_mod, claim_name),
        getattr(db_mod, release_name),
        getattr(db_mod, path_name)(path),
    )


def _module_os(monkeypatch, db_mod, claimant):
    if claimant == "no_hard_link":
        return _no_hard_links(monkeypatch, db_mod)
    iso = types.SimpleNamespace(**vars(db_mod.os))
    monkeypatch.setattr(db_mod, "os", iso)
    return iso


@pytest.mark.parametrize("claimant", ["hard_link", "no_hard_link"])
@pytest.mark.parametrize("kind", sorted(_RELEASE_KINDS))
def test_a_release_after_its_record_was_reclaimed_spares_the_new_owner(
    tmp_path, monkeypatch, kind, claimant,
):
    """#1020 T4c. Detects: a delayed release (its record already reclaimed)
    deleting the record a newer owner published under the same name."""
    db_mod, path, _marker = _owner_store(tmp_path, monkeypatch)
    iso = _module_os(monkeypatch, db_mod, claimant)
    claim_fn, release_fn, marker = _release_case(db_mod, kind, path)
    claim, reason = claim_fn(path)
    assert claim is not None, reason
    db_mod._remove_owner_record(marker)  # as a stale reclaim would
    fresh = []
    calls = {"read": 0, "remove": 0}
    state = {"in_read": False}

    def publish_fresh_once():
        if not fresh:
            successor, why = claim_fn(path)
            assert successor is not None, why
            fresh.append(successor)

    real_lstat = iso.lstat

    def lstat_then_publish(target, *args, **kwargs):
        try:
            return real_lstat(target, *args, **kwargs)
        except FileNotFoundError:
            if pathlib.Path(target) == marker and not state["in_read"]:
                publish_fresh_once()
            raise

    real_read = db_mod._read_repair_owner

    def read_then_publish(target):
        calls["read"] += 1
        state["in_read"] = True
        try:
            owner = real_read(target)
        finally:
            state["in_read"] = False
        if owner is None and not os.path.lexists(target):
            publish_fresh_once()
        return owner

    real_remove = db_mod._remove_owner_record

    def counted_remove(target):
        calls["remove"] += 1
        return real_remove(target)

    iso.lstat = lstat_then_publish
    monkeypatch.setattr(db_mod, "_read_repair_owner", read_then_publish)
    monkeypatch.setattr(db_mod, "_remove_owner_record", counted_remove)

    release_fn(path, claim)

    assert fresh, "the release never observed the name absent"
    assert real_read(marker) is not None
    assert real_read(marker)["claim_id"] == fresh[0].claim_id
    assert calls == {"read": 0, "remove": 0}


@pytest.mark.parametrize("claimant", ["hard_link", "no_hard_link"])
@pytest.mark.parametrize("record", ["matching", "malformed", "mismatched"])
@pytest.mark.parametrize("kind", sorted(_RELEASE_KINDS))
def test_a_release_over_a_present_record_keeps_its_claim_check(
    tmp_path, monkeypatch, kind, record, claimant,
):
    """#1020 T4c. Detects: a release that removes another owner's record, or
    leaves its own (matching or malformed) behind."""
    db_mod, path, _marker = _owner_store(tmp_path, monkeypatch)
    _module_os(monkeypatch, db_mod, claimant)
    claim_fn, release_fn, marker = _release_case(db_mod, kind, path)
    claim, reason = claim_fn(path)
    assert claim is not None, reason
    record_file = db_mod._owner_record_file(marker)
    if record == "malformed":
        record_file.write_text("{truncated-owner-record")
    elif record == "mismatched":
        release_claim = db_mod.RepairMarkerClaim(marker=marker, claim_id="gone")
    if record != "mismatched":
        release_claim = claim

    if record == "mismatched" and kind == "repair":
        with pytest.raises(OSError, match="ownership changed"):
            release_fn(path, release_claim)
    else:
        release_fn(path, release_claim)

    if record == "mismatched":
        assert db_mod._read_repair_owner(marker)["claim_id"] == claim.claim_id
    else:
        assert not os.path.lexists(marker)
        assert _owner_record_debris(marker) == []


@pytest.mark.parametrize("marker_form", ["file", "directory"])
@pytest.mark.parametrize("remover", [
    "stale-repair", "stale-keeper-yield", "release-repair",
    "release-keeper-yield",
])
def test_a_failed_marker_lookup_is_never_taken_for_absence(
    tmp_path, monkeypatch, remover, marker_form,
):
    """#1020 T4c. Detects: an unreadable marker reported as already removed
    (admitting an opener under a running recovery) or removed blind."""
    db_mod, path, _marker = _owner_store(tmp_path, monkeypatch)
    kind = remover.split("-", 1)[1]
    _claim_fn, release_fn, marker = _release_case(db_mod, kind, path)
    record = _dead_identity_record(db_mod)
    _write_owner_marker(marker, record, marker_form)
    record_file = marker if marker_form == "file" else marker / "owner"
    iso = _module_os(monkeypatch, db_mod, "hard_link")
    real_lstat = iso.lstat

    def denied_lstat(target, *args, **kwargs):
        if pathlib.Path(target) == marker:
            raise PermissionError(errno.EACCES, "injected lookup failure",
                                  str(target))
        return real_lstat(target, *args, **kwargs)

    iso.lstat = denied_lstat
    claim = db_mod.RepairMarkerClaim(marker=marker, claim_id="dead-claim")

    if remover == "stale-repair":
        removed, reason = db_mod._remove_stale_repair_marker(path)
        assert removed is False
        assert reason.startswith(f"could not remove stale repair marker {marker}: ")
    elif remover == "stale-keeper-yield":
        removed, reason = db_mod._remove_stale_keeper_yield_request(path)
        assert removed is False
        assert reason.startswith(
            f"could not remove stale keeper-yield request {marker}: "
        )
    elif remover == "release-repair":
        with pytest.raises(OSError, match="injected lookup failure"):
            release_fn(path, claim)
    else:
        release_fn(path, claim)

    assert record_file.read_text() == record


def _write_recovery_sources(
    claude_dir: pathlib.Path, codex_home: pathlib.Path,
) -> None:
    claude_project = claude_dir / "projects" / "-tmp-recovery"
    claude_project.mkdir(parents=True)
    (claude_project / "recovery-session.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-07-24T08:00:00Z",
                "requestId": "req-recovery",
                "message": {
                    "id": "msg-recovery",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                },
            }
        )
        + "\n"
    )
    codex_sessions = codex_home / "sessions" / "2026" / "07" / "24"
    codex_sessions.mkdir(parents=True)
    (codex_sessions / "rollout-recovery.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-07-24T08:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "recovery-codex-session"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-24T08:00:00Z",
                        "type": "turn_context",
                        "payload": {"model": "gpt-5"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-24T08:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 100,
                                    "output_tokens": 10,
                                    "cached_input_tokens": 20,
                                    "reasoning_output_tokens": 5,
                                    "total_tokens": 135,
                                },
                                "total_token_usage": {"total_tokens": 135},
                            },
                        },
                    }
                ),
            ]
        )
        + "\n"
    )


@pytest.mark.parametrize(
    ("source", "claude_rows", "codex_rows"),
    [
        ("claude", 1, 0),
        ("codex", 0, 1),
        ("all", 1, 1),
    ],
)
def test_corrupt_cache_rebuild_preserves_requested_source_scope(
    tmp_path, source, claude_rows, codex_rows,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    _write_recovery_sources(claude_dir, codex_home)
    cache_path = data_dir / "cache.db"
    cache_path.write_bytes(b"not a sqlite database")
    pathlib.Path(f"{cache_path}-wal").write_bytes(b"forensic wal")
    pathlib.Path(f"{cache_path}-shm").write_bytes(b"forensic shm")
    env = os.environ.copy()
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data_dir),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "CODEX_HOME": str(codex_home),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "cctally"),
            "cache-sync",
            "--source",
            source,
            "--rebuild",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(cache_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM session_entries"
        ).fetchone()[0] == claude_rows
        assert conn.execute(
            "SELECT count(*) FROM codex_session_entries"
        ).fetchone()[0] == codex_rows
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    incidents = list((data_dir / "quarantine").glob("cache.db-*"))
    assert len(incidents) == 1
    manifest = json.loads((incidents[0] / "manifest.json").read_text())
    assert manifest["complete"] is True
    moved = set(manifest["movedFiles"])
    assert "cache.db" in moved
    assert moved == {
        item.name for item in incidents[0].iterdir()
        if item.name != "manifest.json"
    }
    bundles = list(
        (data_dir / "logs").glob("cache.db-corruption-forensics-*.json")
    )
    assert len(bundles) == 1
    bundle = json.loads(bundles[0].read_text())
    assert bundle["probeDisposition"] == "confirmed"
    assert bundle["probeReason"] == "integrity_check_corruption_error"
    assert bundle["trigger"]["origin"] == "cache.open"
    assert bundle["trigger"]["exceptionType"].endswith(".DatabaseError")
    assert bundle["trigger"]["sqliteErrorCode"] in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
    }
    assert "quarantined its file family" in result.stderr
    assert not cache_path.with_name("cache.db.repairing").exists()
    assert not cache_path.with_name(
        "cache.db.quarantine-pending.json"
    ).exists()


@pytest.mark.parametrize("form", ["hard_link", "no_hard_link"])
def test_corrupt_cache_with_dead_owner_marker_rebuilds_without_surgery(
    tmp_path, form,
):
    """A corrupt cache.db behind a dead owner's marker rebuilds; #1020 T7:
    also where the volume has no hard links (child-side link seam), where
    the recovery's repair marker must publish as a directory. Detects: a
    fallback wired only into the conversations recovery."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    _write_recovery_sources(claude_dir, codex_home)
    cache_path = data_dir / "cache.db"
    cache_path.write_bytes(b"not a sqlite database")
    cache_path.with_name("cache.db.repairing").write_text("999999999\n")
    env = os.environ.copy()
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data_dir),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "CODEX_HOME": str(codex_home),
        }
    )
    trace = tmp_path / "owner-record-trace.txt"
    env["CCTALLY_TEST_OWNER_RECORD_TRACE"] = str(trace)
    if form == "no_hard_link":
        env["CCTALLY_TEST_OWNER_RECORD_LINK_ERRNO"] = "ENOTSUP"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "cctally"),
            "cache-sync",
            "--source",
            "all",
            "--rebuild",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    expected = "directory" if form == "no_hard_link" else "file"
    published = trace.read_text().splitlines() if trace.exists() else []
    assert f"cache.db.repairing {expected}" in published, published
    marker = cache_path.with_name("cache.db.repairing")
    assert not os.path.lexists(marker)
    assert _owner_record_debris(marker) == []
    conn = sqlite3.connect(cache_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM session_entries"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM codex_session_entries"
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("pause_at", "source", "claude_rows", "codex_rows"),
    [
        ("cache_repair_claimed", "all", 1, 1),
        ("cache_repair_forensics", "all", 1, 1),
        ("cache_repair_quarantined", "all", 1, 1),
        ("cache_repair_recreated", "all", 1, 1),
        # The final three cases kill the recovery owner inside the real
        # provider transaction after recreation.  They prove provider-scoped
        # and all-provider restart semantics rather than only pre-ingest phase
        # boundaries.
        ("claude_precommit", "claude", 1, 0),
        ("codex_precommit", "codex", 0, 1),
        ("codex_precommit", "all", 1, 1),
    ],
)
def test_killed_cache_repair_converges_on_next_rebuild(
    tmp_path, pause_at, source, claude_rows, codex_rows,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    _write_recovery_sources(claude_dir, codex_home)
    cache_path = data_dir / "cache.db"
    cache_path.write_bytes(b"not a sqlite database")
    pathlib.Path(f"{cache_path}-wal").write_bytes(b"forensic wal")
    pathlib.Path(f"{cache_path}-shm").write_bytes(b"forensic shm")
    pause_marker = tmp_path / f"{pause_at}.marker"

    base_env = os.environ.copy()
    base_env.update(
        {
            "CCTALLY_DATA_DIR": str(data_dir),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "CODEX_HOME": str(codex_home),
        }
    )
    victim_env = base_env | {
        "CCTALLY_TEST_CACHE_STORM_PAUSE_AT": pause_at,
        "CCTALLY_TEST_CACHE_STORM_MARKER": str(pause_marker),
    }
    victim = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "bin" / "cctally"),
            "cache-sync",
            "--source",
            source,
        ],
        env=victim_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pause_marker.exists():
            if victim.poll() is not None:
                break
            time.sleep(0.02)
        if not pause_marker.exists():
            stdout, stderr = victim.communicate(timeout=PRESENCE_BACKSTOP_SECONDS)
            pytest.fail(
                f"victim never reached {pause_at}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        os.kill(victim.pid, signal.SIGKILL)
        victim.communicate(timeout=PRESENCE_BACKSTOP_SECONDS)
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.communicate(timeout=PRESENCE_BACKSTOP_SECONDS)

    survivor = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "cctally"),
            "cache-sync",
            "--source",
            source,
            "--rebuild",
        ],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert survivor.returncode == 0, survivor.stderr

    conn = sqlite3.connect(cache_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM session_entries"
        ).fetchone()[0] == claude_rows
        assert conn.execute(
            "SELECT count(*) FROM codex_session_entries"
        ).fetchone()[0] == codex_rows
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert not cache_path.with_name("cache.db.repairing").exists()
    assert len(list((data_dir / "quarantine").glob("cache.db-*"))) == 1


def test_guarded_open_detects_schema_readable_session_tree_corruption(
    tmp_path, monkeypatch,
):
    _ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA page_size=512")
        conn.execute(
            "CREATE TABLE session_entries "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO session_entries(payload) VALUES (?)",
            [("x" * 200,) for _ in range(300)],
        )
        conn.commit()
        root_page = conn.execute(
            "SELECT rootpage FROM sqlite_schema "
            "WHERE type='table' AND name='session_entries'"
        ).fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    finally:
        conn.close()

    # Interior table B-tree pages store their right-most child at header +8.
    # Point it past EOF: schema_version and the left edge remain readable, while
    # the exact descending-rowid probe needed for the production failure raises
    # SQLITE_CORRUPT ("invalid page number").
    with path.open("r+b") as fh:
        header = (root_page - 1) * page_size
        fh.seek(header)
        assert fh.read(1) == b"\x05", "fixture root must be an interior table page"
        fh.seek(header + 8)
        fh.write(struct.pack(">I", page_count + 100))

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA schema_version").fetchone() is not None
        assert raw.execute(
            "SELECT rowid FROM session_entries ORDER BY rowid LIMIT 1"
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            raw.execute(
                "SELECT rowid FROM session_entries ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
    finally:
        raw.close()

    with pytest.raises(sqlite3.DatabaseError, match="malformed") as caught:
        cache_mod._cache_open_guarded()
    trigger_conn = getattr(
        caught.value, "_cctally_cache_connection", None,
    )
    assert trigger_conn is not None
    cache_mod._close_cache_trigger_connection_best_effort(trigger_conn)
