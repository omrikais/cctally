"""Crash-safe cache.db corruption recovery.

Regression for the 2026-07-23 dashboard SIGBUS: the legacy opener unlinked
cache.db in place while another process still held a WAL reader.  The live
reader then faulted in sqlite3.walFindFrame after the mapped file was shortened.
"""
from __future__ import annotations

import ast
import hashlib
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

    monkeypatch.setattr(
        db_mod.sqlite3, "connect",
        lambda *_args, **_kwargs: FakeIntegrityConnection(),
    )
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

    monkeypatch.setattr(
        db_mod.sqlite3, "connect",
        lambda *_args, **_kwargs: FakeIntegrityConnection(),
    )
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

    monkeypatch.setattr(db_mod.sqlite3, "connect", unrelated_probe_failure)
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
    real_write_text = pathlib.Path.write_text

    def fail_forensics_write(self, *args, **kwargs):
        if "corruption-forensics" in self.name:
            raise OSError("injected forensics write failure")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", fail_forensics_write)
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

    monkeypatch.setattr(db_mod.os, "replace", fail_shm_once)
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

    monkeypatch.setattr(db_mod.os, "replace", real_replace)
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


def test_dead_cache_repair_marker_is_reclaimed_before_open(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    path = pathlib.Path(core.CACHE_DB_PATH)
    inode = path.stat().st_ino
    marker = path.with_name("cache.db.repairing")
    marker.write_text("999999999\n")

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    assert path.stat().st_ino == inode
    assert not marker.exists()


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


def test_corrupt_cache_with_dead_owner_marker_rebuilds_without_surgery(tmp_path):
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
    assert not cache_path.with_name("cache.db.repairing").exists()
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
            stdout, stderr = victim.communicate(timeout=5)
            pytest.fail(
                f"victim never reached {pause_at}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        os.kill(victim.pid, signal.SIGKILL)
        victim.communicate(timeout=5)
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.communicate(timeout=5)

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
