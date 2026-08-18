"""`cctally db checkpoint` — raw-connect WAL drain (#297, Task 4).

The handler opens a RAW existing-file-only connection
(``sqlite3.connect("file:<path>?mode=rw", uri=True)`` guarded by
``path.exists()``) — NOT ``open_cache_db()`` / ``open_db()``, which apply
schema, run the migration dispatcher, can DELETE Codex rows, and create a
missing DB. Tests import ``_cctally_db`` / ``_cctally_core`` directly and
monkeypatch ``CACHE_DB_PATH`` (the handler resolves it via ``_cctally_core``
at call time, no live ``cctally`` module needed).

``_grow`` returns the STILL-OPEN writer connection: when the last connection
on a WAL database closes, SQLite checkpoints and deletes the -wal file, so a
closed writer would leave nothing to drain (verified against SQLite 3.53.3).
An idle (committed, no txn) connection pins no read snapshot, so the handler's
separate raw connection can still TRUNCATE the WAL.
"""
import argparse
import fcntl
import importlib
import json
import os
import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))


def _load(n):
    return importlib.import_module(n)


@pytest.fixture(autouse=True)
def _pin_the_conversations_locks(tmp_path, monkeypatch):
    """Pin the three flocks a conversations checkpoint takes (#583 S4).

    Same reasoning as ``_pin_the_cache_locks`` below: without this the
    conversations tests would open the maintainer's real
    ``~/.local/share/cctally`` lock files.
    """
    core = _load("_cctally_core")
    monkeypatch.setattr(
        core,
        "CONVERSATIONS_LOCK_MAINTENANCE_PATH",
        tmp_path / "conversations.db.maintenance.lock",
    )
    monkeypatch.setattr(
        core, "CONVERSATIONS_LOCK_PATH", tmp_path / "conversations.db.lock"
    )
    monkeypatch.setattr(
        core,
        "CONVERSATIONS_LOCK_CODEX_PATH",
        tmp_path / "conversations.db.codex.lock",
    )
    monkeypatch.setattr(
        core, "CONVERSATIONS_DB_PATH", tmp_path / "absent-conversations.db"
    )
    yield


@pytest.fixture(autouse=True)
def _pin_the_cache_locks(tmp_path, monkeypatch):
    """Pin the two flocks ``cmd_db_checkpoint`` takes besides the database.

    Every test here pins ``CACHE_DB_PATH``, and none of that pins the locks:
    ``acquire_ordered_flocks`` opens ``CACHE_LOCK_MAINTENANCE_PATH`` and
    ``CACHE_LOCK_PATH``, which two of the ten tests set and the other eight left
    resolving to the maintainer's real ``~/.local/share/cctally`` (#529 S4).

    A narrow pin rather than ``isolated_paths``, because these two constants are
    the whole surface this module reaches beyond the database it already pins,
    and re-deriving all of them from a fake HOME points the locks at a
    ``.local/share/cctally`` directory nothing creates, which turns the
    ``O_CREAT`` open into ENOENT.
    """
    core = _load("_cctally_core")
    monkeypatch.setattr(
        core, "CACHE_LOCK_MAINTENANCE_PATH", tmp_path / "cache.db.maintenance.lock"
    )
    monkeypatch.setattr(core, "CACHE_LOCK_PATH", tmp_path / "cache.db.lock")
    yield


def _args(**kw):
    ns = argparse.Namespace(db="cache", json=False, busy_timeout_ms=15000)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _grow(db):
    """Grow the -wal sidecar and return the STILL-OPEN writer connection."""
    c = sqlite3.connect(str(db))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE t(x)")
    c.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(20000)])
    c.commit()
    return c


def test_checkpoint_drains_exit0(tmp_path, monkeypatch, capsys):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "cache.db"
    writer = _grow(db)
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    try:
        assert os.path.getsize(str(db) + "-wal") > 0
        rc = dbmod.cmd_db_checkpoint(_args())
        assert rc == 0
        assert (not os.path.exists(str(db) + "-wal")) or os.path.getsize(str(db) + "-wal") == 0
    finally:
        writer.close()


def test_checkpoint_missing_db_exit0(tmp_path, monkeypatch, capsys):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    monkeypatch.setattr(core, "CACHE_DB_PATH", tmp_path / "nope.db")
    assert dbmod.cmd_db_checkpoint(_args()) == 0
    assert "nothing to drain" in capsys.readouterr().out


def test_checkpoint_busy_exit3(tmp_path, monkeypatch):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "cache.db"
    writer = _grow(db)
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    pin = sqlite3.connect(str(db))
    pin.execute("BEGIN")
    pin.execute("SELECT count(*) FROM t").fetchone()  # pin snapshot with WAL frames
    try:
        rc = dbmod.cmd_db_checkpoint(_args(busy_timeout_ms=100))
        assert rc == 3
        # WAL stayed put — nothing was truncated while the reader pins it.
        assert os.path.getsize(str(db) + "-wal") > 0
    finally:
        pin.close()
        writer.close()


def test_cache_checkpoint_defers_while_global_writer_flock_is_held(
    tmp_path, monkeypatch
):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "cache.db"
    writer = _grow(db)
    lock_path = tmp_path / "cache.db.lock"
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    monkeypatch.setattr(core, "CACHE_LOCK_PATH", lock_path)
    held = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        rc = dbmod.cmd_db_checkpoint(_args(busy_timeout_ms=100))
        assert rc == 3
        assert os.path.getsize(str(db) + "-wal") > 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        writer.close()


def test_cache_checkpoint_defers_during_exclusive_maintenance(
    tmp_path, monkeypatch
):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "cache.db"
    writer = _grow(db)
    maintenance_path = tmp_path / "cache.db.maintenance.lock"
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    monkeypatch.setattr(core, "CACHE_LOCK_MAINTENANCE_PATH", maintenance_path)
    monkeypatch.setattr(core, "CACHE_LOCK_PATH", tmp_path / "cache.db.lock")
    held = os.open(str(maintenance_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        rc = dbmod.cmd_db_checkpoint(_args(busy_timeout_ms=100))
        assert rc == 3
        assert os.path.getsize(str(db) + "-wal") > 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        writer.close()


def test_checkpoint_json_schemaversion_first(tmp_path, monkeypatch, capsys):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "cache.db"
    writer = _grow(db)
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    try:
        dbmod.cmd_db_checkpoint(_args(json=True))
        out = capsys.readouterr().out
        obj = json.loads(out)
        assert list(obj.keys())[0] == "schemaVersion"
        assert obj["schemaVersion"] == 1
        assert set(["db", "walBytesBefore", "walBytesAfter", "framesCheckpointed",
                    "busy", "truncated"]).issubset(obj)
    finally:
        writer.close()


def test_checkpoint_does_not_create_missing_db(tmp_path, monkeypatch):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "nope.db"
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    dbmod.cmd_db_checkpoint(_args())
    assert not db.exists()  # raw connect never created it


def test_checkpoint_missing_db_json_present_false(tmp_path, monkeypatch, capsys):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    monkeypatch.setattr(core, "CACHE_DB_PATH", tmp_path / "nope.db")
    assert dbmod.cmd_db_checkpoint(_args(json=True)) == 0
    obj = json.loads(capsys.readouterr().out)
    assert list(obj.keys())[0] == "schemaVersion"
    assert obj["present"] is False
    assert obj["truncated"] is True  # absent re-derivable cache is not an error


def test_stats_checkpoint_is_retired_before_file_or_sqlite_access(
    tmp_path, monkeypatch, capsys,
):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "must-not-be-read.db")
    monkeypatch.setattr(
        dbmod.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("retired stats checkpoint opened SQLite"),
    )

    assert dbmod.cmd_db_checkpoint(_args(db="stats")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stats.db uses rollback journaling" in captured.err
    assert "no WAL checkpoint is applicable" in captured.err


def test_stats_checkpoint_json_is_stamped_not_applicable(
    tmp_path, monkeypatch, capsys,
):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "must-not-be-read.db")

    assert dbmod.cmd_db_checkpoint(_args(db="stats", json=True)) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert list(payload)[0] == "schemaVersion"
    assert payload == {
        "schemaVersion": 1,
        "db": "stats.db",
        "status": "notApplicable",
        "reason": (
            "stats.db uses rollback journaling; no WAL checkpoint is applicable"
        ),
    }


# --------------------------------------------------------------------------
# `--db conversations` (#583 S4 / O-2)
#
# conversations.db carries the same 128 MiB journal_size_limit as cache.db but
# has TWO independently writable provider domains, so its checkpoint must
# exclude both provider flocks as well as taking maintenance shared.
# --------------------------------------------------------------------------


def _pin_conversations(tmp_path, monkeypatch, *, grow=True):
    """Point CONVERSATIONS_DB_PATH at a WAL-backed DB; return the open writer."""
    core = _load("_cctally_core")
    db = tmp_path / "conversations.db"
    monkeypatch.setattr(core, "CONVERSATIONS_DB_PATH", db)
    return _grow(db) if grow else None


def _wal_size(db):
    wal = str(db) + "-wal"
    return os.path.getsize(wal) if os.path.exists(wal) else 0


def test_conversations_is_an_accepted_db_choice(cctally_module):
    """--db conversations must parse; before #583 S4 argparse exits 2."""
    parser = cctally_module.build_parser()
    args = parser.parse_args(["db", "checkpoint", "--db", "conversations"])
    assert args.db == "conversations"


def test_conversations_checkpoint_holds_all_three_flocks_in_order(
    tmp_path, monkeypatch
):
    """maintenance-SH, Claude-EX and Codex-EX, held SIMULTANEOUSLY, in order.

    Per-lock contention tests alone are insufficient: an implementation that
    acquired and released each lock separately before opening SQLite would pass
    every one of them while holding none during the checkpoint. So this probes
    the real flock state from INSIDE the truncate callback — a fresh open of the
    same path conflicts with a flock held on another descriptor in the same
    process, which is what makes the probe meaningful.
    """
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    cache = _load("_cctally_cache")
    lockmod = _load("_lib_cache_writer_lock")
    writer = _pin_conversations(tmp_path, monkeypatch)

    observed_plan = []
    real_acquire = lockmod.acquire_ordered_flocks

    def spy_acquire(locks, *, timeout=None):
        observed_plan.extend(locks)
        return real_acquire(locks, timeout=timeout)

    monkeypatch.setattr(lockmod, "acquire_ordered_flocks", spy_acquire)

    def _probe(path, mode):
        """True when a fresh ``mode`` flock on ``path`` CONFLICTS (is held)."""
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    held_during_checkpoint = {}
    real_truncate = cache._run_wal_truncate

    def spy_truncate(conn, path, *, db_label):
        held_during_checkpoint["maintenance_shared"] = (
            _probe(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH, fcntl.LOCK_EX)
            and not _probe(
                core.CONVERSATIONS_LOCK_MAINTENANCE_PATH, fcntl.LOCK_SH
            )
        )
        held_during_checkpoint["claude_exclusive"] = _probe(
            core.CONVERSATIONS_LOCK_PATH, fcntl.LOCK_SH
        )
        held_during_checkpoint["codex_exclusive"] = _probe(
            core.CONVERSATIONS_LOCK_CODEX_PATH, fcntl.LOCK_SH
        )
        return real_truncate(conn, path, db_label=db_label)

    monkeypatch.setattr(cache, "_run_wal_truncate", spy_truncate)

    try:
        rc = dbmod.cmd_db_checkpoint(
            _args(db="conversations", busy_timeout_ms=2000)
        )
    finally:
        writer.close()

    assert rc == 0
    assert [str(p) for p, _ in observed_plan] == [
        str(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH),
        str(core.CONVERSATIONS_LOCK_PATH),
        str(core.CONVERSATIONS_LOCK_CODEX_PATH),
    ]
    assert [m for _, m in observed_plan] == [
        fcntl.LOCK_SH,
        fcntl.LOCK_EX,
        fcntl.LOCK_EX,
    ]
    assert held_during_checkpoint == {
        "maintenance_shared": True,
        "claude_exclusive": True,
        "codex_exclusive": True,
    }, "the checkpoint ran without all three flocks still held"


def test_conversations_checkpoint_shrinks_a_real_wal(tmp_path, monkeypatch):
    """Relative shrinkage, never an absolute byte ceiling (D-1)."""
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    writer = _pin_conversations(tmp_path, monkeypatch)
    try:
        before = _wal_size(core.CONVERSATIONS_DB_PATH)
        assert before > 0, "precondition: the fixture must leave a non-empty WAL"
        rc = dbmod.cmd_db_checkpoint(
            _args(db="conversations", busy_timeout_ms=2000)
        )
        assert rc == 0
        assert _wal_size(core.CONVERSATIONS_DB_PATH) < before
    finally:
        writer.close()


def test_absent_conversations_db_exits_zero_and_is_not_created(
    tmp_path, monkeypatch, capsys
):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    _pin_conversations(tmp_path, monkeypatch, grow=False)
    assert not core.CONVERSATIONS_DB_PATH.exists()
    assert dbmod.cmd_db_checkpoint(_args(db="conversations")) == 0
    # Naming the store keeps this from passing against cache's own absent-file
    # early return, which is what the pre-#583-S4 handler would have hit.
    assert "no conversations.db database file present" in capsys.readouterr().out
    assert not core.CONVERSATIONS_DB_PATH.exists(), (
        "a checkpoint must never create the store"
    )


def test_contended_conversations_checkpoint_exits_three_without_truncating(
    tmp_path, monkeypatch
):
    """A held Codex provider flock is enough — cache's plan would miss it."""
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    writer = _pin_conversations(tmp_path, monkeypatch)
    before = _wal_size(core.CONVERSATIONS_DB_PATH)
    held = os.open(
        str(core.CONVERSATIONS_LOCK_CODEX_PATH), os.O_RDWR | os.O_CREAT, 0o600
    )
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        rc = dbmod.cmd_db_checkpoint(
            _args(db="conversations", busy_timeout_ms=100)
        )
        assert rc == 3
        assert _wal_size(core.CONVERSATIONS_DB_PATH) == before
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        writer.close()


def test_conversations_checkpoint_defers_during_exclusive_maintenance(
    tmp_path, monkeypatch
):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    writer = _pin_conversations(tmp_path, monkeypatch)
    held = os.open(
        str(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        rc = dbmod.cmd_db_checkpoint(
            _args(db="conversations", busy_timeout_ms=100)
        )
        assert rc == 3
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        writer.close()


def test_conversations_checkpoint_runs_no_schema_or_migration(
    tmp_path, monkeypatch
):
    """The raw mode=rw connect must bypass open_conversations_db()."""
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    cache = _load("_cctally_cache")
    writer = _pin_conversations(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(
        cache,
        "open_conversations_db",
        lambda *a, **k: called.append("open"),
    )
    monkeypatch.setattr(
        dbmod, "_run_pending_migrations", lambda *a, **k: called.append("migrate")
    )
    try:
        assert dbmod.cmd_db_checkpoint(
            _args(db="conversations", busy_timeout_ms=2000)
        ) == 0
        # Prove the checkpoint actually ran against conversations.db, so this
        # cannot pass vacuously through cache's absent-file early return.
        assert _wal_size(core.CONVERSATIONS_DB_PATH) == 0
    finally:
        writer.close()
    assert called == []


def test_conversations_checkpoint_json_labels_the_store(
    tmp_path, monkeypatch, capsys
):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    writer = _pin_conversations(tmp_path, monkeypatch)
    try:
        assert dbmod.cmd_db_checkpoint(
            _args(db="conversations", json=True, busy_timeout_ms=2000)
        ) == 0
    finally:
        writer.close()
    obj = json.loads(capsys.readouterr().out)
    assert list(obj)[0] == "schemaVersion"
    assert obj["db"] == "conversations.db"
    assert obj["present"] is True
    assert obj["truncated"] is True
