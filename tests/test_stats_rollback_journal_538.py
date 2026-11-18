"""Issue #538 Task A: rollback-journal stats index contract."""

from __future__ import annotations

import pathlib
import os
import sqlite3
import subprocess
import sys

import pytest

from conftest import load_script, redirect_paths


ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))


def test_stats_opens_delete_full_without_wal_or_shm(tmp_path, monkeypatch):
    """A stats policy regression to WAL recreates the proven failure plane."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    conn = ns["open_db"]()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == (
            "delete"
        )
        assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 2
    finally:
        conn.close()

    db = pathlib.Path(ns["DB_PATH"])
    assert not pathlib.Path(f"{db}-wal").exists()
    assert not pathlib.Path(f"{db}-shm").exists()


def test_cache_and_conversations_keep_wal_normal(tmp_path, monkeypatch):
    """The stats prevention must not silently change either sibling store."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    for opener in (ns["open_cache_db"], ns["open_conversations_db"]):
        conn = opener()
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == (
                "wal"
            )
            assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 1
        finally:
            conn.close()


def test_epoch_transition_refuses_an_old_wal_holder_before_mutation(
    tmp_path, monkeypatch,
):
    """A pre-transition dashboard handle must yield restart guidance."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core as core
    import _cctally_db as dbmod
    import _cctally_journal as journal
    import _cctally_store as store
    import _lib_journal as wire

    journal.append_record(
        wire.make_obs(
            at="2026-08-09T00:00:00Z",
            src="statusline",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": 1786320000,
                "source": "statusline",
                "captured_at": "2026-08-09T00:00:00Z",
            },
        )
    )
    journal.run_stats_ingest(mode="authoritative")
    db = pathlib.Path(core.DB_PATH)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute(f"PRAGMA user_version = {core.STATS_INDEX_EPOCH - 1}")
        conn.commit()
    finally:
        conn.close()

    parity_tables = (
        "journal_protocol_violations",
        "weekly_usage_snapshots",
        "weekly_cost_snapshots",
        "percent_milestones",
        "five_hour_blocks",
        "five_hour_milestones",
        "accounts",
        "quota_window_blocks",
        "quota_percent_milestones",
        "quota_projection_state",
    )

    def logical_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
        width = len(conn.execute(f"PRAGMA table_info({table})").fetchall())
        order = ", ".join(str(index) for index in range(1, width + 1))
        return [
            tuple(row)
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        ]

    parity = sqlite3.connect(str(db))
    try:
        before_effective = parity.execute(
            "SELECT event_id, rev, status, content_hash, batch_id, event_json "
            "FROM journal_effective_events ORDER BY event_id"
        ).fetchall()
        before_rows = {
            table: logical_rows(parity, table)
            for table in parity_tables
        }
    finally:
        parity.close()

    ready_r, ready_w = os.pipe()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys,time\n"
            "c=sqlite3.connect(sys.argv[1])\n"
            "c.execute('PRAGMA schema_version').fetchone()\n"
            "os.write(int(sys.argv[2]), b'1')\n"
            "time.sleep(120)\n",
            str(db),
            str(ready_w),
        ],
        pass_fds=(ready_w,),
    )
    os.close(ready_w)
    try:
        assert os.read(ready_r, 1) == b"1"
        family_before = {
            suffix: pathlib.Path(f"{db}{suffix}").read_bytes()
            for suffix in ("", "-wal", "-shm")
            if pathlib.Path(f"{db}{suffix}").exists()
        }
        with pytest.raises(dbmod.StatsEpochMismatchError) as raised:
            store.resolve_stats_epoch_mismatch()
        message = str(raised.value).lower()
        assert "restart" in message and "dashboard" in message
        family_after = {
            suffix: pathlib.Path(f"{db}{suffix}").read_bytes()
            for suffix in family_before
        }
        assert family_after == family_before
    finally:
        os.close(ready_r)
        holder.kill()
        holder.wait(timeout=30)

    rebuilt = store.resolve_stats_epoch_mismatch()
    try:
        assert rebuilt.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert int(rebuilt.execute("PRAGMA user_version").fetchone()[0]) == (
            core.STATS_INDEX_EPOCH
        )
        cursor = rebuilt.execute(
            "SELECT segment, offset, applied_segment, applied_offset "
            "FROM journal_cursor WHERE id = 1"
        ).fetchone()
        expected_high_water = journal._journal_rebuild_snapshot()[0]
        assert tuple(cursor) == (
            expected_high_water[0], expected_high_water[1],
            expected_high_water[0], expected_high_water[1],
        )
        assert [
            tuple(row)
            for row in rebuilt.execute(
                "SELECT event_id, rev, status, content_hash, batch_id, "
                "event_json FROM journal_effective_events ORDER BY event_id"
            ).fetchall()
        ] == before_effective
        assert {
            table: logical_rows(rebuilt, table)
            for table in parity_tables
        } == before_rows
    finally:
        rebuilt.close()


def test_rollback_rebuild_never_calls_a_wal_checkpoint(
    tmp_path, monkeypatch,
):
    """A future checkpoint call would restore live dependence on WAL state."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_journal as journal

    ns["open_db"]().close()
    assert not hasattr(journal, "_checkpoint_after_publication")

    journal.rebuild_stats_index(
        context=journal.RebuildContext(trigger="db-rebuild")
    )


def test_rollback_recovery_preparation_does_not_open_or_checkpoint(
    tmp_path, monkeypatch,
):
    """A DELETE-family recovery must not route through a WAL helper."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_store as store

    db = tmp_path / "rollback.db"
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == (
            "delete"
        )
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
    finally:
        conn.close()

    assert not store._stats_legacy_wal_family_present(db)


# --------------------------------------------------------------------------
# #1100: the stats header probes must keep this process's SQLite locks
# --------------------------------------------------------------------------
#
# On POSIX, closing ANY descriptor on a file releases every fcntl lock the
# process holds on that inode, including the locks SQLite holds for another
# connection in the same process. SQLite's unix VFS defers its own closes for
# that reason; a raw Python `open()`/`close()` of stats.db bypasses it. The
# failure each case below detects is stored-data corruption: once the lock is
# gone, a second process can write stats.db (or roll back a hot journal) while
# this process still believes it holds a read or write lock.

_CONTENDER = (
    "import sqlite3,sys\n"
    "c=sqlite3.connect(sys.argv[1],timeout=0,isolation_level=None)\n"
    "try:\n"
    "    c.execute('BEGIN EXCLUSIVE')\n"
    "except sqlite3.OperationalError as e:\n"
    "    print('locked' if 'locked' in str(e) else 'error: '+str(e))\n"
    "else:\n"
    "    c.execute('ROLLBACK')\n"
    "    print('acquired')\n"
)


def _other_process_exclusive(db: pathlib.Path) -> str:
    """Whether a second process can take stats.db EXCLUSIVE right now."""
    done = subprocess.run(
        [sys.executable, "-c", _CONTENDER, str(db)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return done.stdout.strip() or f"error: {done.stderr.strip()}"


def _hold(db: pathlib.Path, lock: str) -> sqlite3.Connection:
    """A same-process connection holding SHARED (an open read transaction)
    or RESERVED (an open write transaction) on `db`."""
    holder = sqlite3.connect(str(db), isolation_level=None)
    if lock == "reserved":
        holder.execute("BEGIN IMMEDIATE")
    else:
        holder.execute("BEGIN")
        holder.execute("SELECT count(*) FROM sqlite_master").fetchall()
    return holder


def _probe_wal_family(ns, store, db):
    assert store._stats_legacy_wal_family_present(db) is False


def _probe_wal_cold(ns, store, db):
    assert store._require_stats_legacy_wal_cold(db) is None


def _probe_epoch_pending(ns, store, db):
    assert store.stats_epoch_rebuild_pending(db) is False


def _probe_user_version(ns, store, db):
    import _cctally_core as core
    assert store._raw_user_version(db) == core.STATS_INDEX_EPOCH


def _probe_user_version_fallback(ns, store, db):
    """The 64-byte child fallback, taken when SQLite rejects the file: the
    immutable read is replaced by one that raises, so the header bytes come
    from the AD19 child while the holder's lock is live."""
    import _cctally_core as core
    import _cctally_store_access as access_mod
    lengths = []
    real_probe = access_mod._run_header_probe

    def rejected(_path):
        raise sqlite3.DatabaseError("file is not a database")

    def counting(path, *args, **kwargs):
        lengths.append(kwargs.get("length", args[0] if args else 20))
        return real_probe(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store, "_immutable_user_version", rejected)
        mp.setattr(access_mod, "_run_header_probe", counting)
        assert store._raw_user_version(db) == core.STATS_INDEX_EPOCH
    assert lengths == [64], "the fallback child did not read the header"


def _probe_open_db(ns, store, db):
    ns["open_db"]().close()


def _probe_vacuum(ns, store, db):
    import argparse
    # The holder's lock makes VACUUM refuse "in use"; what must survive is
    # the holder's lock itself.
    assert ns["cmd_db_vacuum"](argparse.Namespace(db="stats")) == 3


@pytest.mark.parametrize(
    "lock, probe",
    [
        # The shared helper of the vacuum and rebuild-publication callers.
        ("reserved", _probe_wal_family),
        # The epoch resolver's cold-family check (the WAL-transition helper).
        ("reserved", _probe_wal_cold),
        # The deferred-epoch probe every ordinary stats open runs first.
        ("reserved", _probe_epoch_pending),
        ("reserved", _probe_user_version),
        # The same hint when SQLite rejects the file: the 64-byte child.
        ("reserved", _probe_user_version_fallback),
        # A stats owner open beside a live reader (a dashboard owner thread
        # beside a request thread's read unit).
        ("shared", _probe_open_db),
        # The `db vacuum --db stats` entry, end to end. RESERVED: a SHARED
        # holder's lock is re-taken by VACUUM's own failed escalation, which
        # would hide the release window from an end-state check.
        ("reserved", _probe_vacuum),
    ],
    ids=["wal-family", "wal-cold", "epoch-pending", "user-version",
         "user-version-fallback", "open-db", "vacuum"],
)
def test_stats_header_probes_keep_same_process_sqlite_locks(
    tmp_path, monkeypatch, lock, probe,
):
    """A stats header read must never drop another connection's SQLite lock
    in this process (#1100)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core as core
    import _cctally_store as store

    ns["open_db"]().close()
    db = pathlib.Path(core.DB_PATH)
    holder = _hold(db, lock)
    try:
        assert _other_process_exclusive(db) == "locked"
        probe(ns, store, db)
        assert _other_process_exclusive(db) == "locked", (
            "the header read released this process's SQLite lock: a second "
            "process took stats.db EXCLUSIVE while the holder's "
            f"{lock.upper()} transaction was still open"
        )
    finally:
        holder.close()
    # Control: with the holder gone the contender does acquire, so the two
    # assertions above distinguish a held lock from a contender that always
    # reports "locked".
    assert _other_process_exclusive(db) == "acquired"


# --------------------------------------------------------------------------
# #1100: the AD19 fresh-read mode and the WAL-header check through the child
# --------------------------------------------------------------------------

_SQLITE_MAGIC = b"SQLite format 3\x00"


@pytest.fixture
def access(tmp_path, monkeypatch):
    """The store-access module with an empty AD19 cache of its own, so a
    verdict another test probed can never decide one of these cases."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_store_access as access_mod

    monkeypatch.setattr(access_mod, "_HEADER_CACHE", {})
    monkeypatch.setattr(access_mod, "_HEADER_PROBED", {})
    return access_mod


def _rollback_db(path: pathlib.Path) -> pathlib.Path:
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == (
            "delete")
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
    finally:
        conn.close()
    header = path.read_bytes()[:20]
    assert header[:16] == _SQLITE_MAGIC and header[18] == header[19] == 1
    return path


def _wal_header_db(path: pathlib.Path) -> pathlib.Path:
    """A database whose main header declares WAL, with no -wal or -shm (the
    last close checkpoints and removes both)."""
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
    finally:
        conn.close()
    header = path.read_bytes()[:20]
    assert header[:16] == _SQLITE_MAGIC and header[18] == header[19] == 2
    assert not pathlib.Path(f"{path}-wal").exists()
    assert not pathlib.Path(f"{path}-shm").exists()
    return path


def _stat_key(access_mod, path) -> tuple:
    return access_mod._header_stat_key(os.stat(path))


def test_fresh_header_read_ignores_a_stale_cached_verdict(access, tmp_path):
    """A cached ``rollback`` verdict whose stat key a WAL-header file still
    matches must not hide the WAL header from the maintenance callers.
    Detects: weakened legacy-WAL detection through a stale AD19 verdict,
    which would let vacuum or publication convert a live legacy family."""
    import _cctally_store as store

    db = _wal_header_db(tmp_path / "stats.db")
    access.seed_header_verdicts([(_stat_key(access, db), "rollback")])
    assert access._stats_header_kind(db) == "rollback"      # cached mode
    assert access._stats_header_kind(db, fresh=True) == "wal"
    assert store._stats_legacy_wal_family_present(db) is True


def test_fresh_header_read_rejects_a_capture_that_straddled_a_change(
    access, tmp_path, monkeypatch,
):
    """A capture whose stat identity moved while the child read certifies
    nothing: fresh mode returns None and caches and exports nothing for
    either identity. Detects: a missing second-stat comparison, or a
    rejected capture handed to another process through AD30."""
    import _cctally_core as core
    import _cctally_store as store

    db = _rollback_db(tmp_path / "stats.db")
    keys = [_stat_key(access, db)]
    real_probe = access._run_header_probe

    def probe_then_rewrite(path, *args, **kwargs):
        out = real_probe(path, *args, **kwargs)
        data = bytearray(pathlib.Path(path).read_bytes())
        data[18] = data[19] = 2
        pathlib.Path(path).write_bytes(bytes(data) + b"\0" * 512)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        keys.append(_stat_key(access, path))
        return out

    monkeypatch.setattr(access, "_run_header_probe", probe_then_rewrite)
    assert access._stats_header_kind(db, fresh=True) is None
    assert keys[0] != keys[-1]
    probed = dict(access.probed_header_verdicts())
    for key in keys:
        assert key not in access._HEADER_CACHE
        assert key not in probed
    assert access.header_verdict_handoff([db]) == ()

    with pytest.raises(core.StoreUnavailable) as info:
        store._stats_legacy_wal_family_present(db)
    assert core.store_refusal_text(info.value) == "stats.db could not be read"


@pytest.mark.parametrize("head_usable", [True, False],
                         ids=["head", "interpreter"])
def test_header_probe_reads_the_requested_length(
    access, tmp_path, monkeypatch, head_usable,
):
    """The 64-byte user-version fallback must read 64 bytes on both AD19
    backends, while the 20-byte program stays byte-identical. Detects: the
    interpreter fallback's hardcoded ``read(20)`` returning a short header to
    the 64-byte caller, which would misclassify every damaged stats.db as
    not pending."""
    db = _rollback_db(tmp_path / "stats.db")
    assert access._HEADER_PROBE_SCRIPT == access._header_probe_script(20)
    monkeypatch.setattr(access, "_HEAD_ARGV0", head_usable)
    if head_usable:
        assert access._header_probe_argv(db, 64)[:3] == [
            "/usr/bin/head", "-c", "64"]
        assert access._header_probe_argv(db) == [
            "/usr/bin/head", "-c", "20", str(db)]
    else:
        # The suite's isolation guard refuses a real -I/-S interpreter
        # child under its own name; a link it does not recognize runs it.
        link_dir = tmp_path / "probe-bin"
        link_dir.mkdir()
        link = link_dir / "probe-interp"
        link.symlink_to(os.path.realpath(sys.executable))
        monkeypatch.setattr(access.sys, "executable", str(link))
        argv = access._header_probe_argv(db, 64)
        assert argv == [str(link), "-I", "-S", "-B", "-c",
                        access._header_probe_script(64), str(db)]
    raw = db.read_bytes()
    assert access._run_header_probe(db, length=64) == raw[:64]
    assert access._run_header_probe(db) == raw[:20]


@pytest.mark.parametrize(
    "header, sidecars, expected",
    [
        ("wal", (), True),
        ("rollback", ("-wal",), True),
        ("rollback", ("-shm",), True),
        ("rollback", (), False),
        (None, (), False),
    ],
    ids=["header-only-wal", "wal-sidecar-only", "shm-sidecar-only",
         "rollback", "missing"],
)
def test_legacy_wal_family_keeps_every_detection_leg(
    access, tmp_path, monkeypatch, header, sidecars, expected,
):
    """Each leg of the legacy-WAL family check detects on its own, and a
    sidecar already present decides without spawning the header child.
    Detects: a leg lost when the sidecar checks moved before the header read,
    which would let vacuum, publication or the epoch transition treat a live
    legacy WAL family as a rollback index."""
    import _cctally_store as store

    db = tmp_path / "stats.db"
    if header == "wal":
        _wal_header_db(db)
    elif header == "rollback":
        _rollback_db(db)
    for suffix in sidecars:
        pathlib.Path(f"{db}{suffix}").write_bytes(b"")
    calls = []
    real_probe = access._run_header_probe

    def counting(path, *args, **kwargs):
        calls.append(str(path))
        return real_probe(path, *args, **kwargs)

    monkeypatch.setattr(access, "_run_header_probe", counting)
    assert store._stats_legacy_wal_family_present(db) is expected
    assert len(calls) == (1 if header is not None and not sidecars else 0)


@pytest.mark.parametrize("cause", ["probe-none", "stat-error"])
def test_legacy_wal_check_refuses_when_the_header_cannot_be_read(
    access, tmp_path, monkeypatch, cause,
):
    """A header the child could not read, or a main file that cannot be
    stat'ed, is not proof of a rollback index: the check refuses with AD19's
    typed refusal, never answers False, and the refusal is not corruption.
    Detects: fail-open legacy detection (a maintenance caller converting a
    family it could not classify), or a heal triggered by a probe that
    merely could not run."""
    import _cctally_core as core
    import _cctally_db as db_mod
    import _cctally_store as store

    db = _rollback_db(tmp_path / "stats.db")
    if cause == "probe-none":
        monkeypatch.setattr(access, "_run_header_probe",
                            lambda path, *a, **kw: None)
    else:
        real_stat = os.stat

        def stat(target, *args, **kwargs):
            if os.fspath(target) == str(db):
                raise PermissionError(13, "Permission denied", str(target))
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(store.os, "stat", stat)
    with pytest.raises(core.StoreUnavailable) as info:
        store._stats_legacy_wal_family_present(db)
    assert info.value.store == "stats"
    assert info.value.reason == "unavailable"
    assert str(info.value) == "stats.db could not be read"
    assert core.store_refusal_text(info.value) == "stats.db could not be read"
    assert not db_mod._is_sqlite_corruption_error(info.value)


def test_epoch_resolver_refuses_an_unreadable_header_then_converges(
    access, monkeypatch,
):
    """The epoch resolver's cold-family check cannot read the header while
    the AD19 child is unavailable: it refuses with the typed refusal,
    releases maintenance and leaves the family untouched; once the child
    runs again the same resolver converges. Detects: an epoch transition
    that converts a family it could not classify, or a refusal that leaves
    the transition unretryable (maintenance or the in-flight guard held)."""
    import _cctally_core as core
    import _cctally_journal as journal
    import _cctally_store as store
    import _lib_journal as wire

    journal.append_record(
        wire.make_obs(
            at="2026-08-09T00:00:00Z",
            src="statusline",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": 1786320000,
                "source": "statusline",
                "captured_at": "2026-08-09T00:00:00Z",
            },
        )
    )
    journal.run_stats_ingest(mode="authoritative")
    db = pathlib.Path(core.DB_PATH)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(f"PRAGMA user_version = {core.STATS_INDEX_EPOCH - 1}")
        conn.commit()
    finally:
        conn.close()
    family_before = {
        suffix: pathlib.Path(f"{db}{suffix}").read_bytes()
        for suffix in ("", "-journal", "-wal", "-shm")
        if pathlib.Path(f"{db}{suffix}").exists()
    }
    real_probe = access._run_header_probe
    monkeypatch.setattr(access, "_run_header_probe",
                        lambda path, *a, **kw: None)
    with pytest.raises(core.StoreUnavailable) as info:
        store.resolve_stats_epoch_mismatch()
    assert core.store_refusal_text(info.value) == "stats.db could not be read"
    # In one process a leaked hold would let the retry below converge anyway
    # (maintenance is reentrant), so the release is asserted directly.
    assert not core.holds_stats_maintenance()
    assert {
        suffix: pathlib.Path(f"{db}{suffix}").read_bytes()
        for suffix in ("", "-journal", "-wal", "-shm")
        if pathlib.Path(f"{db}{suffix}").exists()
    } == family_before

    monkeypatch.setattr(access, "_run_header_probe", real_probe)
    rebuilt = store.resolve_stats_epoch_mismatch()
    try:
        assert int(rebuilt.execute("PRAGMA user_version").fetchone()[0]) == (
            core.STATS_INDEX_EPOCH)
    finally:
        rebuilt.close()


def test_epoch_resolver_never_transitions_a_file_whose_epoch_is_unknown(
    access, monkeypatch,
):
    """The locked re-check after a cold family check reads the epoch hint;
    an unknown hint (SQLite rejected the file and the child could not read
    it) refuses with the typed refusal and releases maintenance, and never
    runs the epoch transition. Detects: `None != EPOCH` taken as a mismatch,
    which would rebuild and publish over a file nobody could read (#1100)."""
    import _cctally_core as core
    import _cctally_journal as journal
    import _cctally_store as store
    import _lib_journal as wire

    # A current, cold family with a journal behind it, so only the unknown
    # hint can refuse (a missing journal would refuse for its own reason).
    journal.append_record(
        wire.make_obs(
            at="2026-08-09T00:00:00Z",
            src="statusline",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": 1786320000,
                "source": "statusline",
                "captured_at": "2026-08-09T00:00:00Z",
            },
        )
    )
    journal.run_stats_ingest(mode="authoritative")
    db = pathlib.Path(core.DB_PATH)
    assert store._raw_user_version(db) == core.STATS_INDEX_EPOCH
    before = db.read_bytes()
    monkeypatch.setattr(store, "_stats_user_version_hint", lambda path: None)

    def transition(*a, **kw):
        raise AssertionError("epoch transition ran on an unknown epoch")

    monkeypatch.setattr(journal, "run_epoch_transition", transition)
    with pytest.raises(core.StoreUnavailable) as info:
        store.resolve_stats_epoch_mismatch()
    assert core.store_refusal_text(info.value) == "stats.db could not be read"
    assert not core.holds_stats_maintenance()
    assert db.read_bytes() == before
