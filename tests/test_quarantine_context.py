"""#496 S6 Task 2 — `QuarantineContext` and additive v2 incident manifests.

Spec §4.1 / §4.2. Every quarantine call site is `strict=True`; two of the five
are direct producers that know what triggered the recovery, and three are
resume paths that must read that context back from the durable pending record
rather than inventing one.

A manifest that carries `schemaVersion: 2` **and** a truthy `trigger`
classifies itself (§3.3), which is what lets the retention planner ever select
the incident. Anything less stays unclassified, and therefore protected — the
safe direction.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sqlite3
import stat
import subprocess
import sys

import pytest

from conftest import load_script, redirect_paths


V1_MANIFEST_KEYS = ("quarantinedAtUtc", "originalPath", "movedFiles", "complete")

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"

#: §4.1's verified inventory: enclosing function -> supplies its own context.
#: The two direct producers observed the corruption and are the only callers
#: that can describe it; the three resume callers read it back from the
#: durable pending record and must supply none.
STRICT_QUARANTINE_SITES = {
    ("_cctally_cache.py", "_recover_corrupt_cache"): True,
    ("_cctally_cache.py", "_recover_corrupt_conversations"): True,
    ("_cctally_cache.py", "_cache_open_guarded"): False,
    ("_cctally_cache.py", "_conversations_open_guarded"): False,
    ("_cctally_store.py", "_resume_pending_quarantine"): False,
}


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", "1")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns, sys.modules["_cctally_core"], sys.modules["_cctally_cache"]


def _corrupt_cache_family(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    pathlib.Path(f"{path}-wal").write_bytes(b"forensic wal")
    pathlib.Path(f"{path}-shm").write_bytes(b"forensic shm")


def _drive_idle_cache_recovery(ns, cache_mod, monkeypatch, *, message=None):
    """Run the real direct cache recovery once, then reopen normally."""
    real_open = cache_mod._cache_open_guarded
    attempts = 0
    text = message or "database disk image is malformed"

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.DatabaseError(text)
        return real_open()

    monkeypatch.setattr(cache_mod, "_cache_open_guarded", fail_once)
    conn = ns["open_cache_db"]()
    conn.close()


def _only_incident(path: pathlib.Path, prefix: str) -> pathlib.Path:
    incidents = sorted((path.parent / "quarantine").glob(f"{prefix}-*"))
    assert len(incidents) == 1, incidents
    return incidents[0]


def _manifest(incident: pathlib.Path) -> dict:
    return json.loads((incident / "manifest.json").read_text())


# --------------------------------------------------------------------------
# Direct producers — the two sites that know the trigger
# --------------------------------------------------------------------------


def test_direct_cache_recovery_writes_an_additive_v2_manifest(
    tmp_path, monkeypatch,
):
    ns, core, cache_mod = _load(tmp_path, monkeypatch)
    path = pathlib.Path(core.CACHE_DB_PATH)
    _corrupt_cache_family(path)
    _drive_idle_cache_recovery(ns, cache_mod, monkeypatch)

    m = _manifest(_only_incident(path, "cache.db"))
    assert m["schemaVersion"] == 2
    assert m["trigger"]
    assert m["complete"] is True
    assert m["forensicsPath"]
    assert m["binaryVersion"]
    # Additive: every v1 key survives with its v1 name and meaning.
    for key in V1_MANIFEST_KEYS:
        assert key in m


def test_direct_conversations_recovery_writes_an_additive_v2_manifest(
    tmp_path, monkeypatch,
):
    ns, core, cache_mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(cache_mod.shutil, "which", lambda _name: "/usr/bin/cp")

    real_run = subprocess.run

    def reject_clone(command, **kwargs):
        if command[0] == "/usr/bin/cp":
            return subprocess.CompletedProcess(
                command, 1, "", "Operation not supported",
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(
        cache_mod.subprocess, "run", reject_clone,
    )
    ns["open_conversations_db"](attach_cache=False).close()
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    raw = bytearray(path.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big") or 4096
    if page_size == 1:
        page_size = 65_536
    assert len(raw) >= page_size * 3
    raw[page_size:page_size * 2] = b"\xa5" * page_size
    path.write_bytes(bytes(raw))

    recovered = cache_mod._recover_corrupt_conversations(
        sqlite3.DatabaseError("database disk image is malformed"),
        origin="cache_sync.cli.conversations.open",
        providers=("claude", "codex"),
        lock_timeout=5.0,
    )
    assert recovered is True

    m = _manifest(_only_incident(path, "conversations.db"))
    assert m["schemaVersion"] == 2
    assert m["trigger"] == "cache_sync.cli.conversations.open"
    assert m["forensicsPath"]
    assert m["binaryVersion"]
    for key in V1_MANIFEST_KEYS:
        assert key in m


def test_the_manifest_is_written_atomically(tmp_path, monkeypatch):
    ns, core, cache_mod = _load(tmp_path, monkeypatch)
    path = pathlib.Path(core.CACHE_DB_PATH)
    _corrupt_cache_family(path)
    _drive_idle_cache_recovery(ns, cache_mod, monkeypatch)

    manifest = _only_incident(path, "cache.db") / "manifest.json"
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_trigger_error_is_bounded(tmp_path, monkeypatch):
    ns, core, cache_mod = _load(tmp_path, monkeypatch)
    path = pathlib.Path(core.CACHE_DB_PATH)
    _corrupt_cache_family(path)
    _drive_idle_cache_recovery(
        ns, cache_mod, monkeypatch,
        message="database disk image is malformed " + "x" * 5000,
    )

    m = _manifest(_only_incident(path, "cache.db"))
    assert m["triggerError"]
    assert len(m["triggerError"]) <= 512


# --------------------------------------------------------------------------
# Resume paths — the three sites that must read the context back
# --------------------------------------------------------------------------


def test_a_resume_reads_context_from_the_pending_record(tmp_path, monkeypatch):
    ns, core, cache_mod = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    _corrupt_cache_family(path)

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
    record = json.loads(pending.read_text())
    assert record["context"]["trigger"] == "test.partial_quarantine"

    monkeypatch.setattr(db_mod.os, "replace", real_replace)
    ns["open_cache_db"]().close()
    assert not pending.exists()

    m = _manifest(_only_incident(path, "cache.db"))
    assert m["schemaVersion"] == 2
    # Read back from the pending record, not invented by the resuming caller.
    assert m["trigger"] == "test.partial_quarantine"


def test_the_conversations_resume_reads_context_from_the_pending_record(
    tmp_path, monkeypatch,
):
    """§4.1's third family. Two earlier inventories got the assignments wrong.

    The direct producer at `_recover_corrupt_conversations` is the process that
    observed the corruption; the resume at `_conversations_open_guarded` must
    finalize the SAME incident from the record rather than invent a context of
    its own.
    """
    ns, core, cache_mod = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]

    ns["open_conversations_db"](attach_cache=False).close()
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    raw = bytearray(path.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big") or 4096
    if page_size == 1:
        page_size = 65_536
    raw[page_size:page_size * 2] = b"\xa5" * page_size
    path.write_bytes(bytes(raw))

    real_replace = db_mod.os.replace
    failed = False

    def fail_main_once(src, dst):
        nonlocal failed
        if not failed and str(src).endswith("/conversations.db"):
            failed = True
            raise OSError("injected main-file move failure")
        return real_replace(src, dst)

    monkeypatch.setattr(db_mod.os, "replace", fail_main_once)
    with pytest.raises(
        sqlite3.DatabaseError, match="could not complete whole-family quarantine"
    ):
        cache_mod._recover_corrupt_conversations(
            sqlite3.DatabaseError("database disk image is malformed"),
            origin="test.conversations_partial_quarantine",
            providers=("claude", "codex"),
            lock_timeout=5.0,
        )
    pending = db_mod._quarantine_pending_path(path)
    assert pending.exists()
    record = json.loads(pending.read_text())
    assert record["context"]["trigger"] == "test.conversations_partial_quarantine"

    monkeypatch.setattr(db_mod.os, "replace", real_replace)
    # The failed quarantine also left a durable recovery state, so the next
    # open is the recovery re-entry rather than an ordinary one.
    cache_mod._open_conversations_db_for_recovery(attach_cache=False).close()
    assert not pending.exists()

    m = _manifest(_only_incident(path, "conversations.db"))
    assert m["schemaVersion"] == 2
    assert m["trigger"] == "test.conversations_partial_quarantine"
    assert m["complete"] is True


def test_the_stats_resume_reads_context_from_the_pending_record(
    tmp_path, monkeypatch,
):
    """§4.1's fifth call site. Revisions 1 and 2 both missed stats entirely.

    A stats pending record is finalized by `_resume_pending_quarantine`, which
    supplies no context of its own; the manifest must still describe what the
    process that observed the corruption knew.
    """
    _ns, core, _cache_mod = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    store = sys.modules["_cctally_store"]

    path = pathlib.Path(core.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    pathlib.Path(f"{path}-wal").write_bytes(b"forensic wal")

    incident = core.APP_DIR / "quarantine" / "stats.db-20260101T000000Z"
    incident.mkdir(parents=True)
    db_mod._atomic_write_private_json(
        db_mod._quarantine_pending_path(path),
        {
            "schemaVersion": 1,
            "originalPath": str(path),
            "incidentPath": str(incident),
            "members": ["stats.db-wal", "stats.db"],
            "createdAtUtc": "2026-01-01T00:00:00Z",
            "context": db_mod.quarantine_context(
                trigger="test.stats_pending",
                trigger_error=sqlite3.DatabaseError(
                    "database disk image is malformed"
                ),
                forensics_path=core.LOG_DIR / "stats.db-corruption-forensics-x.json",
            ).to_record(),
        },
    )

    store._resume_pending_quarantine(path)
    assert not db_mod._quarantine_pending_path(path).exists()

    m = _manifest(incident)
    assert m["schemaVersion"] == 2
    assert m["trigger"] == "test.stats_pending"
    assert m["triggerError"]
    assert m["forensicsPath"]
    assert m["complete"] is True
    assert sorted(m["movedFiles"]) == ["stats.db", "stats.db-wal"]


def test_strict_stats_quarantine_preserves_a_hot_rollback_journal(
    tmp_path, monkeypatch,
):
    """#538: destructive cold recovery preserves every rollback byte."""
    _ns, core, _cache_mod = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"damaged main")
    rollback = pathlib.Path(f"{path}-journal")
    rollback.write_bytes(b"hot rollback journal")

    incident = db_mod.quarantine_db_family(
        path,
        strict=True,
        context=db_mod.quarantine_context(trigger="test.stats_rollback"),
    )

    assert not path.exists()
    assert not rollback.exists()
    assert (incident / path.name).read_bytes() == b"damaged main"
    assert (incident / rollback.name).read_bytes() == b"hot rollback journal"
    manifest = _manifest(incident)
    assert manifest["complete"] is True
    assert manifest["movedFiles"] == [rollback.name, path.name]


def test_a_legacy_pending_record_without_context_finalizes_as_v1(
    tmp_path, monkeypatch,
):
    ns, core, cache_mod = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    _corrupt_cache_family(path)

    incident = core.APP_DIR / "quarantine" / "cache.db-20260101T000000Z"
    incident.mkdir(parents=True)
    db_mod._atomic_write_private_json(
        db_mod._quarantine_pending_path(path),
        {
            "schemaVersion": 1,
            "originalPath": str(path),
            "incidentPath": str(incident),
            "members": ["cache.db-wal", "cache.db-shm", "cache.db"],
            "createdAtUtc": "2026-01-01T00:00:00Z",
        },
    )

    ns["open_cache_db"]().close()

    m = _manifest(incident)
    assert m["schemaVersion"] == 1
    assert "trigger" not in m


def test_a_resume_refuses_a_second_context(tmp_path, monkeypatch):
    """The persisted record is the only authority on a resume (§4.2)."""
    ns, core, _cache_mod = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    _corrupt_cache_family(path)

    incident = core.APP_DIR / "quarantine" / "cache.db-20260101T000000Z"
    incident.mkdir(parents=True)
    db_mod._atomic_write_private_json(
        db_mod._quarantine_pending_path(path),
        {
            "schemaVersion": 1,
            "originalPath": str(path),
            "incidentPath": str(incident),
            "members": ["cache.db"],
            "createdAtUtc": "2026-01-01T00:00:00Z",
        },
    )

    with pytest.raises(ValueError, match="resume"):
        db_mod.quarantine_db_family(
            path,
            strict=True,
            context=db_mod.QuarantineContext(
                trigger="test.second_context",
                trigger_error=None,
                forensics_path=None,
                binary_version=None,
            ),
        )


# --------------------------------------------------------------------------
# A creation that supplies no context degrades loudly (§4.2)
# --------------------------------------------------------------------------


def _strict_quarantine_calls(module: str) -> "list[tuple[str, bool]]":
    """(enclosing function, supplies `context=`) per `strict=True` call site."""
    tree = ast.parse((BIN / module).read_text(encoding="utf-8"))
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found: "list[tuple[str, bool]]" = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "quarantine_db_family":
            continue
        keywords = {kw.arg for kw in node.keywords}
        strict = any(
            kw.arg == "strict"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in node.keywords
        )
        if not strict:
            continue
        enclosing = None
        parent = node
        while parent in parents:
            parent = parents[parent]
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                enclosing = parent.name
                break
        found.append((enclosing, "context" in keywords))
    return found


def test_every_strict_creation_site_supplies_a_context():
    """§4.1's five call sites, pinned by which of them describe the trigger.

    A new producer that forgets its context must fail here rather than write a
    permanently unclassifiable v1 incident in production. A new resume caller
    that invents one must fail here too.
    """
    observed: dict = {}
    for module in ("_cctally_cache.py", "_cctally_store.py"):
        for enclosing, supplies in _strict_quarantine_calls(module):
            observed[(module, enclosing)] = supplies
    assert observed == STRICT_QUARANTINE_SITES


def test_a_creation_without_a_context_warns_once(tmp_path, monkeypatch, capsys):
    """The safe direction is kept, but it stops being silent.

    Raising would turn a metadata defect into a failed corruption recovery, so
    the v1 manifest is still finalized. The two adjacent programming errors — a
    context on a resume, a context with `strict=False` — both raise, and this
    one degraded with nothing said at all.
    """
    ns, core, _cache_mod = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]

    for stamp in ("20260101T000000Z", "20260102T000000Z"):
        path = pathlib.Path(core.CACHE_DB_PATH)
        _corrupt_cache_family(path)
        incident = db_mod.quarantine_db_family(path, strict=True, ts=stamp)
        assert _manifest(incident)["schemaVersion"] == 1

    err = capsys.readouterr().err
    assert err.count("without a QuarantineContext") == 1, err


def test_a_creation_with_a_context_stays_silent(tmp_path, monkeypatch, capsys):
    ns, core, cache_mod = _load(tmp_path, monkeypatch)
    path = pathlib.Path(core.CACHE_DB_PATH)
    _corrupt_cache_family(path)
    _drive_idle_cache_recovery(ns, cache_mod, monkeypatch)

    assert _manifest(_only_incident(path, "cache.db"))["schemaVersion"] == 2
    assert "without a QuarantineContext" not in capsys.readouterr().err


def test_family_drain_detects_a_rollback_journal_only_holder(
    tmp_path, monkeypatch,
):
    """The #538 cold-drain family includes `-journal`, even without main."""
    _ns, _core, _cache_mod = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    db = tmp_path / "stats.db"
    journal = pathlib.Path(f"{db}-journal")
    journal.write_bytes(b"rollback evidence")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time\nf=open(sys.argv[1], 'rb')\n"
            "print('ready', flush=True)\ntime.sleep(120)\n",
            str(journal),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        pids = db_mod._db_family_open_pids(db)
        assert pids is not None and holder.pid in pids
    finally:
        holder.kill()
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()


# --------------------------------------------------------------------------
# The non-strict branch has no production caller, but must not regress
# --------------------------------------------------------------------------


def test_the_non_strict_branch_writes_its_manifest_privately(
    tmp_path, monkeypatch,
):
    ns, core, _cache_mod = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    _corrupt_cache_family(path)

    incident = db_mod.quarantine_db_family(path, strict=False)
    manifest = incident / "manifest.json"
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert json.loads(manifest.read_text())["schemaVersion"] == 1
