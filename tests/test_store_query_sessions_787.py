"""#787 / #861 Task B groups 1 and 3 — query sessions.

Group 1: per-store query admission and readiness. A query that creates,
migrates, recovers or waits on maintenance is a persistent write or a hang on
a read path (spec 4.3 D3/D8/D9); each case asserts the typed reason and the
file effects (spec 5.4: cache/conversations may gain only ``-shm`` and a
0-byte ``-wal``; stats nothing).

Group 3: TEMP, ATTACH and identity under the composed authorizer and the
5.13 snapshot protocol.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import errno
import fcntl
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback

import pytest

from conftest import load_script, redirect_paths

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated stores created by the owner openers under `process`, in a
    private (0700) data directory."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = sys.modules["_cctally_core"]
    cache = sys.modules["_cctally_cache"]
    app = pathlib.Path(core.APP_DIR)
    app.mkdir(parents=True, exist_ok=True)
    os.chmod(app, 0o700)
    with core.establish_authority("process"):
        core.open_db().close()
        cache.open_cache_db().close()
        cache.open_conversations_db().close()
    import _cctally_store_access as access
    return {
        "ns": ns, "core": core, "cache": cache, "db": sys.modules["_cctally_db"],
        "store": sys.modules["_cctally_store"], "access": access,
        "root": tmp_path, "app": app,
    }


def _path(env, store):
    core = env["core"]
    return pathlib.Path({"stats": core.DB_PATH, "cache": core.CACHE_DB_PATH,
                         "conversations": core.CONVERSATIONS_DB_PATH}[store])


def _lock(env, store):
    core = env["core"]
    return pathlib.Path({
        "stats": core.STATS_LOCK_MAINTENANCE_PATH,
        "cache": core.CACHE_LOCK_MAINTENANCE_PATH,
        "conversations": core.CONVERSATIONS_LOCK_MAINTENANCE_PATH}[store])


def _head(env, store):
    if store == "stats":
        return env["core"].STATS_INDEX_EPOCH
    db = env["db"]
    return len(db._CACHE_MIGRATIONS if store == "cache"
               else db._CONVERSATIONS_MIGRATIONS)


def _listing(root):
    """Every entry's identity; a directory's size/mtime is excluded because
    an allowed new sidecar legitimately changes them."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        if p.is_dir():
            out[str(p.relative_to(root))] = ("dir", st.st_mode)
        else:
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns,
                                             st.st_mode)
    return out


def _assert_only_allowed_effects(env, store, before):
    """Spec 5.4: nothing changes except, for cache/conversations, a new
    ``-shm`` and a new 0-byte ``-wal`` beside the store."""
    after = _listing(env["root"])
    db_rel = str(_path(env, store).relative_to(env["root"]))
    allowed = set()
    if store != "stats":
        allowed = {db_rel + "-shm", db_rel + "-wal"}
    for name in set(after) | set(before):
        if name in allowed:
            if name.endswith("-wal") and name in after and name not in before:
                assert after[name][0] == 0, f"{name} gained frames"
            continue
        if name.endswith("-shm") or name.endswith("-wal"):
            # Existing sidecars of OTHER stores are never touched.
            assert before.get(name) == after.get(name), name
            continue
        assert before.get(name) == after.get(name), f"{name} changed"


def _stamp(path, version):
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _seed(env, store, state):
    path = _path(env, store)
    if state == "absent":
        path.unlink()
        return "absent"
    if state == "missing_lock":
        _lock(env, store).unlink()
        return "absent"
    if state == "behind":
        _stamp(path, _head(env, store) - 1)
        return "schema_behind"
    if state == "ahead":
        _stamp(path, _head(env, store) + 1)
        return "schema_ahead"
    if state == "corrupt":
        for suffix in ("-wal", "-shm"):
            pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)
        path.write_bytes(b"definitely not sqlite" * 256)
        return "corrupt"
    if state == "recovery":
        if store == "stats":
            path.with_name(path.name + ".rebuilding-20260101T000000_000000"
                           ).write_bytes(b"")
            return "recovery_pending"
        if store == "cache":
            env["db"]._quarantine_pending_path(path).write_text("{}")
            return "recovery_pending"
        env["cache"]._conversation_recovery_state_path().write_text("{}")
        return "maintenance"
    if state == "fence":
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, 'x')",
                     (env["db"]._MIGRATION_FENCE_PREFIX + "t",))
        conn.commit()
        conn.close()
        return "recovery_pending"
    if state == "non_private":
        os.chmod(env["app"], 0o755)
        return "unavailable"
    raise AssertionError(state)


_STATES = ["absent", "missing_lock", "behind", "ahead", "corrupt",
           "recovery", "non_private"]


@pytest.mark.parametrize("store", ["stats", "cache", "conversations"])
@pytest.mark.parametrize("state", _STATES + ["fence"])
def test_query_admission_typed_states(env, store, state):
    """Detects a query that creates, migrates or recovers a store it cannot
    read (D3, D8, D9), or that reports an untyped error."""
    if state == "fence" and store != "cache":
        pytest.skip("migration fences are a cache.db state")
    expected = _seed(env, store, state)
    before = _listing(env["root"])
    with pytest.raises(env["core"].StoreUnavailable) as info:
        with env["access"].QueryService.session(store, policy="command"):
            pass  # pragma: no cover
    assert info.value.reason == expected
    _assert_only_allowed_effects(env, store, before)


@pytest.mark.parametrize("store", ["stats", "cache", "conversations"])
def test_maintenance_held_is_refused_within_interactive_budget(env, store):
    """Detects the D9 hang: a held maintenance lock must refuse an
    interactive query in under 500 ms, never wait it out."""
    fh = open(_lock(env, store), "r")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        before = _listing(env["root"])
        started = time.monotonic()
        with pytest.raises(env["core"].StoreUnavailable) as info:
            with env["access"].QueryService.session(store):
                pass  # pragma: no cover
        elapsed = time.monotonic() - started
    finally:
        fh.close()
    assert info.value.reason == "maintenance"
    assert elapsed < 0.5
    _assert_only_allowed_effects(env, store, before)


@pytest.mark.parametrize("store", ["stats", "cache", "conversations"])
def test_current_store_is_readable_without_owner_effects(env, store):
    """Detects a query on a healthy store that still writes (journal_mode,
    chmod, close-time WAL removal, content-revision install)."""
    before = _listing(env["root"])
    with env["access"].QueryService.session(store) as s:
        assert s.readiness["primary"].readable
        assert isinstance(s.conn, env["access"]._QueryConnection)
        s.conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    _assert_only_allowed_effects(env, store, before)


def test_stats_wal_header_without_sidecars_refused(env):
    """Spec 5.6 mandatory obligation: a current-epoch stats.db whose header
    declares WAL with no -wal/-shm must be refused without a sidecar."""
    path = _path(env, "stats")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    assert not pathlib.Path(f"{path}-wal").exists()
    assert path.read_bytes()[18:20] == b"\x02\x02"
    before = _listing(env["root"])
    with pytest.raises(env["core"].StoreUnavailable) as info:
        with env["access"].QueryService.session("stats"):
            pass  # pragma: no cover
    assert info.value.reason == "unavailable"
    assert str(info.value) == "stats.db header is not a rollback-journal index"
    assert _listing(env["root"]) == before


class _FakeProbe:
    """A stand-in for the AD19 child that has already written `out`."""

    def __init__(self, out=b"", returncode=0):
        self._out = out
        self.returncode = None
        self._rc = returncode
        self.stdout = None

    def communicate(self, timeout=None):
        self.returncode = self._rc
        return self._out, None

    def kill(self):
        pass

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def poll(self):
        return self.returncode

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _probe_kwargs(sp):
    return {"shell": False, "env": {}, "stdin": sp.DEVNULL,
            "stdout": sp.PIPE, "stderr": sp.DEVNULL, "close_fds": True}


@pytest.mark.parametrize("head_usable", [True, False],
                         ids=["usr_bin_head", "interpreter_fallback"])
def test_stats_header_probe_argv_is_fixed(env, monkeypatch, head_usable):
    """AD19 (spec revisions 11-12): the header probe child never resolves
    through PATH, so a PATH-planted ``head`` cannot decide whether a WAL-mode
    stats.db is refused (5.6). The launch is pinned by intercepting it: argv
    is exactly one of the two AD19 shapes (the fallback with ``-B``, so
    interpreter startup writes no bytecode), and the descriptor options are
    exactly no shell, an empty environment, DEVNULL stdin/stderr, a stdout
    pipe and close_fds. The fallback is chosen once per process."""
    access = env["access"]
    path = _path(env, "stats")
    calls = []
    real_popen = access.subprocess.Popen

    def spy(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        if argv[0] == sys.executable:
            # The suite's isolation guard refuses a real -I/-S child, so the
            # fallback child is simulated with the bytes it would write (the
            # real child runs in test_header_probe_fallback_writes_nothing).
            return _FakeProbe(pathlib.Path(argv[-1]).read_bytes()[:20])
        return real_popen(argv, **kwargs)

    checks = []
    real_usable = access._usr_bin_head_usable

    def fake_usable():
        checks.append(1)
        return head_usable

    monkeypatch.setattr(access, "_usr_bin_head_usable", fake_usable)
    monkeypatch.setattr(access, "_HEAD_ARGV0", None)
    monkeypatch.setattr(access.subprocess, "Popen", spy)
    monkeypatch.setenv("PATH", str(env["root"]))
    access._HEADER_CACHE.clear()
    assert access._stats_header_kind(path) == "rollback"
    access._HEADER_CACHE.clear()
    assert access._stats_header_kind(path) == "rollback"
    assert len(checks) == 1          # checked once per process
    assert len(calls) == 2
    argv, kwargs = calls[0]
    if head_usable:
        assert argv == ["/usr/bin/head", "-c", "20", str(path)]
    else:
        assert argv == [sys.executable, "-I", "-S", "-B", "-c",
                        "import sys;sys.stdout.buffer.write("
                        "open(sys.argv[1],'rb').read(20))", str(path)]
    assert kwargs == _probe_kwargs(access.subprocess)
    assert real_usable() in (True, False)


def test_stats_header_probe_failure_refuses_unavailable(env, monkeypatch):
    """AD19 fail-closed: a spawn error, a non-zero exit or short output
    refuses as unavailable, never as a readable rollback index. Its message
    is "stats.db could not be read", not the WAL-header sentence: detects a
    healthy rollback store whose probe child timed out under load being
    reported as a WAL-mode index (the header was never read)."""
    access = env["access"]
    path = _path(env, "stats")

    def spawn_error(argv, **kwargs):
        raise OSError("no such program")

    for probe in (spawn_error,
                  lambda argv, **kw: _FakeProbe(b"", returncode=1),
                  lambda argv, **kw: _FakeProbe(b"SQLite")):
        access._HEADER_CACHE.clear()
        monkeypatch.setattr(access.subprocess, "Popen", probe)
        assert access._stats_header_kind(path) is None
        with pytest.raises(env["core"].StoreUnavailable) as info:
            with access.QueryService.session("stats"):
                pass  # pragma: no cover
        assert info.value.reason == "unavailable"
        assert str(info.value) == "stats.db could not be read"
    access._HEADER_CACHE.clear()


#: Scheduling slack for "total elapsed within the deadline": the measured end
#: is taken after the probe returned, so it includes the caller's own frames.
_DEADLINE_SLACK_S = 0.03


def _real_child_spy(access, launched, *, argv=("/bin/sleep", "30"),
                    launch_delay=0.0, kill_delay=0.0, kill_noop=False,
                    on_unbounded_wait=None):
    """A `Popen` spy that launches `argv` (a real child that stalls) in place
    of the probe, optionally delaying the launch or the kill, or stubbing
    the kill out so the child survives it."""
    real_popen = access.subprocess.Popen

    def spy(probe_argv, **kwargs):
        if launch_delay:
            time.sleep(launch_delay)
        proc = real_popen(list(argv), **kwargs)
        real_kill, real_wait = proc.kill, proc.wait

        def kill():
            if kill_delay:
                time.sleep(kill_delay)
            if not kill_noop:
                real_kill()

        def wait(timeout=None):
            if timeout is None and on_unbounded_wait is not None:
                on_unbounded_wait()
            return real_wait(timeout=timeout)

        proc.kill = kill
        proc.wait = wait
        launched.append(proc)
        return proc

    return spy


def _leave_remaining(core, seconds):
    remaining = core.remaining_deadline()
    time.sleep(max(0.0, remaining - seconds))
    return time.monotonic() + core.remaining_deadline()


@pytest.mark.parametrize("admission", ["nearly_exhausted", "nested_tighter"])
@pytest.mark.parametrize("delay", ["stalled_child", "delayed_launch",
                                   "delayed_cleanup"])
def test_header_probe_one_deadline_through_cleanup(env, monkeypatch,
                                                   admission, delay):
    """AD19 (revision 13): launch, communication and cleanup share ONE
    absolute deadline, min(now + 2 s, the operation's), with the 50 ms
    cleanup reserve inside it. Detects a probe that waits its own 2 s for a
    stalled child, restarts the allowance after a slow launch, or kills and
    reaps after the deadline: total elapsed time, with nothing subtracted
    for reaping, stays within the deadline, and the child is reaped."""
    access, core = env["access"], env["core"]
    launched = []
    monkeypatch.setattr(access.subprocess, "Popen", _real_child_spy(
        access, launched,
        launch_delay=0.08 if delay == "delayed_launch" else 0.0,
        kill_delay=0.03 if delay == "delayed_cleanup" else 0.0))
    outer = (core.operation_deadline("command") if admission ==
             "nested_tighter" else contextlib.nullcontext())
    with outer:
        with core.operation_deadline("interactive"):
            if admission == "nested_tighter":
                assert core.remaining_deadline() <= 0.5
            access._HEADER_CACHE.clear()
            deadline = _leave_remaining(core, 0.15)
            kind = access._stats_header_kind(_path(env, "stats"))
            ended = time.monotonic()
    assert kind is None
    assert ended <= deadline + _DEADLINE_SLACK_S, ended - deadline
    assert len(launched) == 1
    assert launched[0].returncode is not None     # reaped, not abandoned


def test_header_probe_exhausted_deadline_spawns_nothing(env, monkeypatch):
    """AD19: with less than the cleanup reserve left, the probe refuses
    without spawning, so it can never start a child it has no time to
    reap."""
    access, core = env["access"], env["core"]
    launched = []
    monkeypatch.setattr(access.subprocess, "Popen",
                        lambda *a, **k: launched.append(a))
    with core.operation_deadline("interactive"):
        access._HEADER_CACHE.clear()
        _leave_remaining(core, getattr(
            access, "_HEADER_PROBE_CLEANUP_RESERVE_S", 0.05) / 2)
        assert access._stats_header_kind(_path(env, "stats")) is None
    assert launched == []


def test_header_probe_never_returns_with_a_live_child(env, monkeypatch):
    """AD19 (revision 14): a child that survives SIGKILL past the deadline is
    waited for until it exits, still under the stats store's shared
    maintenance admission, and the overrun is recorded on the session. The
    kill is stubbed out so termination outlasts the reserve. Detects a probe
    that releases the admission (and so lets #386's drain proceed) while a
    child may still hold a stats.db descriptor."""
    access = env["access"]
    lock = _lock(env, "stats")
    held_during_wait = []

    def probe_lock():
        with open(lock, "r") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held_during_wait.append(True)
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)
                held_during_wait.append(False)

    launched = []
    monkeypatch.setattr(access.subprocess, "Popen", _real_child_spy(
        access, launched, argv=("/bin/sleep", "0.8"), kill_noop=True,
        on_unbounded_wait=probe_lock))
    access._HEADER_CACHE.clear()
    started = time.monotonic()
    with pytest.raises(env["core"].StoreUnavailable) as info:
        with access.QueryService.session("stats"):
            pass  # pragma: no cover
    elapsed = time.monotonic() - started
    access._HEADER_CACHE.clear()
    assert info.value.reason == "unavailable"
    assert len(launched) == 1
    assert launched[0].returncode == 0       # it exited on its own, reaped
    assert elapsed >= 0.75
    assert held_during_wait == [True]
    overruns = info.value.admission_overruns
    assert [what for what, _s in overruns] == ["stats_header_probe"]
    assert overruns[0][1] > 0.1


def test_header_probe_fallback_writes_nothing(env, monkeypatch, tmp_path):
    """AD19: the interpreter fallback child (``-I -S -B``) reads the header
    and writes nothing: no bytecode, no site processing, no file anywhere in
    the test tree. The real child runs through a link whose name the suite's
    isolation guard does not take for an interpreter (that guard refuses a
    ``-I``/``-S`` Python child because it cannot carry the write detector;
    the directory diff below is this child's write detector instead)."""
    access = env["access"]
    path = _path(env, "stats")
    link_dir = tmp_path / "probe-bin"
    link_dir.mkdir()
    link = link_dir / "probe-interp"
    link.symlink_to(os.path.realpath(sys.executable))
    monkeypatch.setattr(access, "_HEAD_ARGV0", False)
    monkeypatch.setattr(access.sys, "executable", str(link))
    access._HEADER_CACHE.clear()
    before = _listing(env["root"])
    assert access._stats_header_kind(path) == "rollback"
    access._HEADER_CACHE.clear()
    assert _listing(env["root"]) == before
    assert not list(env["root"].rglob("__pycache__"))
    assert not list(env["root"].rglob("*.pyc"))


def _mode_ignoring_chmod(env, monkeypatch):
    """Simulate an exFAT/FAT/SMB data folder: a chmod of the data directory
    succeeds and changes nothing (the filesystem reports synthetic modes)."""
    core = env["core"]
    real = core.os.chmod
    app = str(env["app"])

    def chmod(target, mode, *a, **kw):
        if os.fspath(target) == app:
            return None
        return real(target, mode, *a, **kw)

    monkeypatch.setattr(core.os, "chmod", chmod)


def _write_access_record(env, **override):
    st = os.stat(env["app"])
    payload = {"st_dev": st.st_dev, "st_ino": st.st_ino,
               "st_mode": st.st_mode, "st_uid": st.st_uid}
    payload.update(override)
    record = env["app"] / "data-dir-access.json"
    record.write_text(json.dumps(payload))
    os.chmod(record, 0o600)
    return record


def _foreign_record_owner(env, monkeypatch):
    """Report the access record's OPENED descriptor as owned by another user,
    as when another user replaced it (a test cannot chown without root). Only
    the descriptor's ``fstat`` lies: a check that trusts a path ``lstat``
    taken before the open would still accept the record."""
    core = env["core"]
    ino = os.stat(env["app"] / "data-dir-access.json").st_ino
    real = core.os.fstat

    def fstat(fd):
        st = real(fd)
        if st.st_ino == ino:
            fields = list(st)
            fields[4] = st.st_uid + 1          # st_uid
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(core.os, "fstat", fstat)


@pytest.mark.parametrize("case", [
    "permissive_no_record", "sidecar_no_record", "record_other_inode",
    "record_other_mode", "record_foreign_owner", "record_symlink"])
def test_data_dir_access_policy_refuses(env, monkeypatch, case):
    """AD20 controls: a permissive data directory is accepted only with the
    owner's matching, user-owned access record, so a 0755 directory on an
    enforcing filesystem (where 0644 sidecars would be world-readable) never
    passes, and a stale record (another inode or mode, as after a remount),
    one another user replaced, or a symlink to a matching record elsewhere
    does not either. The record is judged through its opened descriptor
    (``O_NOFOLLOW``, ``fstat``). Each refuses `unavailable` without a write
    (detects a check that infers unenforced modes or trusts any record)."""
    os.chmod(env["app"], 0o755)
    store = "stats"
    if case == "sidecar_no_record":
        store = "cache"
        holder = sqlite3.connect(_path(env, "cache"))
        holder.execute("SELECT count(*) FROM sqlite_master").fetchone()
        assert pathlib.Path(f"{_path(env, 'cache')}-shm").exists()
    else:
        holder = None
    if case == "record_other_inode":
        _write_access_record(env, st_ino=os.stat(env["app"]).st_ino + 1)
    elif case == "record_other_mode":
        _write_access_record(env, st_mode=os.stat(env["app"]).st_mode | 0o2)
    elif case == "record_foreign_owner":
        _write_access_record(env)
        _foreign_record_owner(env, monkeypatch)
    elif case == "record_symlink":
        target = _write_access_record(env)
        elsewhere = env["root"] / "planted-access.json"
        target.rename(elsewhere)
        target.symlink_to(elsewhere)
    try:
        before = _listing(env["root"])
        with pytest.raises(env["core"].StoreUnavailable) as info:
            with env["access"].QueryService.session(store, policy="command"):
                pass  # pragma: no cover
        assert info.value.reason == "unavailable"
        assert _listing(env["root"]) == before
    finally:
        if holder is not None:
            holder.close()


def test_data_dir_access_record_written_by_owner_on_mode_ignoring_fs(
        env, monkeypatch):
    """AD20: on a filesystem whose chmod does not take, the owner start
    (`ensure_dirs()`, reached by the AD2 CLI startup phase) records the
    directory's identity and mode, and the query then accepts the directory.
    Detects every query refusing (and every facade falling back to JSONL)
    on a supported exFAT/FAT/SMB data folder."""
    access, core = env["access"], env["core"]
    os.chmod(env["app"], 0o755)
    _mode_ignoring_chmod(env, monkeypatch)
    record = env["app"] / "data-dir-access.json"
    assert not record.exists()
    with pytest.raises(core.StoreUnavailable):
        with access.QueryService.session("stats", policy="command"):
            pass  # pragma: no cover
    with core.establish_authority("process"):
        with access.command_session("stats") as s:
            assert s.readiness["primary"].readable
    st = os.stat(env["app"])
    data = json.loads(record.read_text())
    assert data == {"st_dev": st.st_dev, "st_ino": st.st_ino,
                    "st_mode": st.st_mode, "st_uid": st.st_uid}
    rst = os.stat(record)
    assert rst.st_uid == os.geteuid() and (rst.st_mode & 0o777) == 0o600
    # The next query passes with no owner start.
    with access.QueryService.session("cache", policy="command") as s:
        assert s.readiness["primary"].readable


@pytest.mark.parametrize("err, mints", [
    ("EIO", False), ("EACCES", False),
    ("EPERM", True), ("ENOTSUP", True), ("EOPNOTSUPP", True)])
def test_data_dir_access_record_on_chmod_error(env, monkeypatch, err, mints):
    """AD20 (revision 20): a ``chmod`` of the user-owned data directory that
    raises ``EPERM``, ``ENOTSUP`` or ``EOPNOTSUPP`` is how Linux ``vfat`` and
    ``exfat`` refuse mode changes, so the owner mints the access record and
    the query then accepts the directory (detects every query refusing, and
    every facade falling back to JSONL, on such a mount). Any other error
    (``EIO``, ``EACCES``) is an ordinary hardening failure: no record, and
    the query keeps refusing (detects an owner that records any permissive
    directory it failed to harden)."""
    access, core = env["access"], env["core"]
    os.chmod(env["app"], 0o755)
    real = core.os.chmod
    app = os.fspath(env["app"])
    code = getattr(errno, err)

    def chmod(target, mode, *a, **kw):
        if os.fspath(target) == app:
            raise OSError(code, os.strerror(code), app)
        return real(target, mode, *a, **kw)

    monkeypatch.setattr(core.os, "chmod", chmod)
    with core.establish_authority("process"):
        core.ensure_dirs()
    record = env["app"] / "data-dir-access.json"
    assert record.exists() is mints
    if mints:
        assert stat_mode(record) == 0o600
        with access.QueryService.session("stats", policy="command") as s:
            s.conn.execute("SELECT 1").fetchall()
        return
    with pytest.raises(core.StoreUnavailable) as info:
        with access.QueryService.session("stats", policy="command"):
            pass  # pragma: no cover
    assert info.value.reason == "unavailable"


@pytest.mark.parametrize("owner, err, mints", [
    ("cache", "EIO", False), ("conversations", "EIO", False),
    ("cache", "EPERM", True)])
def test_store_opener_chmod_error_follows_the_minting_rule(
        env, monkeypatch, owner, err, mints):
    """AD20 (revision 20): the cache and conversations owner openers harden
    the data directory themselves, and their record decision follows the
    same rule as `ensure_dirs()` (detects an opener that mints the record
    after any failed ``chmod``, so an ``EIO``/``EACCES`` on a loosened
    directory of an enforcing filesystem would let every query accept it)."""
    core, cache = env["core"], env["cache"]
    os.chmod(env["app"], 0o755)
    real = core.os.chmod
    app = os.fspath(env["app"])
    code = getattr(errno, err)

    def chmod(target, mode, *a, **kw):
        if os.fspath(target) == app:
            raise OSError(code, os.strerror(code), app)
        return real(target, mode, *a, **kw)

    monkeypatch.setattr(core.os, "chmod", chmod)
    opener = (cache.open_cache_db if owner == "cache"
              else cache.open_conversations_db)
    with core.establish_authority("process"):
        opener().close()
    assert (env["app"] / "data-dir-access.json").exists() is mints


def stat_mode(p):
    return os.stat(p).st_mode & 0o777


def test_data_dir_access_record_removed_when_private_again(env):
    """AD20: a directory that becomes private again has its record removed
    at the next owner start, so a later loss of the mode is never accepted
    on a stale record."""
    core = env["core"]
    os.chmod(env["app"], 0o755)
    record = _write_access_record(env)
    with core.establish_authority("process"):
        core.ensure_dirs()
    assert (os.stat(env["app"]).st_mode & 0o077) == 0
    assert not record.exists()


def test_failfast_missing_lock_not_created(env):
    """Detects the 4.5 probe finding: the fail-fast stats reader created its
    maintenance lock (and directory) on a read (X13, spec 5.4)."""
    lock = _lock(env, "stats")
    lock.unlink()
    assert env["store"].stats_open_readonly_failfast() is None
    assert not lock.exists()


def test_legacy_stats_readable_with_obligation(env):
    """Spec 5.6 / AD8: a legacy store a deferred migration keeps below the
    cutover baseline stays readable, carrying legacy_migration_pending."""
    core = env["core"]
    fixture = (REPO / "tests/fixtures/migrations/per-migration/"
               "009_recompute_five_hour_blocks_dedup_fix")
    path = _path(env, "stats")
    for suffix in ("", "-wal", "-shm", "-journal"):
        pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(fixture / "pre.sqlite", path)
    shutil.copyfile(fixture / "pre-cache.sqlite", _path(env, "cache"))
    for suffix in ("-wal", "-shm"):
        pathlib.Path(f"{_path(env, 'cache')}{suffix}").unlink(missing_ok=True)
    with core.establish_authority("process"):
        try:
            core.open_db().close()
        except BaseException:
            pass
    version = sqlite3.connect(path).execute(
        "PRAGMA user_version").fetchone()[0]
    assert version <= core.LEGACY_STATS_HEAD
    with env["access"].QueryService.session("stats") as s:
        r = s.readiness["primary"]
        assert r.readable
        assert "legacy_migration_pending" in r.obligations


def _write_stats(env, sql, params=()):
    conn = sqlite3.connect(_path(env, "stats"))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("obligation", [
    "quota_projection_incomplete", "content_revision_missing",
    "legacy_migration_pending", "migration_error_sentinel",
    "codex_maintenance_debt"])
def test_obligation_predicates(env, obligation):
    """Detects a readable store whose outstanding owner work is invisible to
    the owner (spec 5.5), so nothing would ever drain it."""
    core, db = env["core"], env["db"]
    store = "cache" if obligation in ("migration_error_sentinel",
                                      "codex_maintenance_debt") else "stats"
    if obligation == "quota_projection_incomplete":
        _write_stats(env, "INSERT OR REPLACE INTO "
                          "stats_quota_projection_state(id, incomplete) "
                          "VALUES (1, 1)")
    elif obligation == "content_revision_missing":
        conn = sqlite3.connect(_path(env, "stats"))
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_content_revision_%'")]
        for name in names:
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP TABLE stats_content_revision_scopes")
        conn.execute("DROP TABLE stats_content_revision")
        conn.commit()
        conn.close()
    elif obligation == "legacy_migration_pending":
        _stamp(_path(env, "stats"), core.LEGACY_STATS_HEAD)
    elif obligation == "migration_error_sentinel":
        first = db._CACHE_MIGRATIONS[0].name
        pathlib.Path(core.MIGRATION_ERROR_LOG_PATH).write_text(
            f"[2026-01-01T00:00:00Z] cache.db:{first}\nboom\n\n")
    else:
        conn = sqlite3.connect(_path(env, "cache"))
        conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                     (env["cache"].CODEX_FINALIZATION_DEBT_KEY, "x"))
        conn.commit()
        conn.close()
    with env["access"].QueryService.session(store) as s:
        r = s.readiness["primary"]
    assert r.readable
    assert obligation in r.obligations


def test_malformed_content_revision_stays_readable(env):
    """Detects a query refusing a stats store whose content-revision singleton
    is malformed: the owner opener reads such a store and never repairs it,
    and its readers treat the revision as untrusted (#1050), so refusing the
    whole store would leave the dashboard less available than the owner path.
    The session admits it with no reusable revision and no obligation the
    owner could drain."""
    import _lib_stats_content_revision as rev
    _write_stats(env, f"DELETE FROM {rev.REVISION_TABLE}")
    with env["access"].QueryService.session("stats") as s:
        r = s.readiness["primary"]
        identity = s.identity.stores["primary"]
        with pytest.raises(rev.StatsContentRevisionUnreadable):
            rev.read_claude_content_revision(s.conn)
    assert r.readable
    assert identity.content_revision is None
    assert "content_revision_missing" not in r.obligations


@pytest.mark.parametrize("store", ["stats", "cache", "conversations"])
@pytest.mark.parametrize("artifact", ["marker", "pending"])
def test_diagnostic_admission_refuses_claimed_recovery(env, store, artifact,
                                                       monkeypatch):
    """N2: a repair marker or pending quarantine refuses a diagnostic session
    AND the metadata reader with `maintenance` before any connection exists:
    a destructive recovery that claimed its marker must see no new handle."""
    path = _path(env, store)
    target = ((env["store"]._stats_repair_marker(path) if store == "stats"
               else env["db"]._repair_marker_path(path))
              if artifact == "marker"
              else env["db"]._quarantine_pending_path(path))
    target.write_text("{}")
    connects = []
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **k: connects.append(a) or real(*a, **k))
    access = env["access"]
    for opener in (lambda: access.QueryService.session(store,
                                                       policy="diagnostic"),
                   lambda: access.QueryService.metadata(store)):
        with pytest.raises(env["core"].StoreUnavailable) as info:
            with opener():
                pass  # pragma: no cover
        assert info.value.reason == "maintenance"
    assert connects == []


def _dead_pid() -> int:
    """A PID that names no process: a child that has exited and been reaped."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def _write_owner_record(marker, owner):
    """Write ``marker`` as the repair owner records it: ``live`` (this
    process, its real start identity), ``reused`` (this live PID with another
    process's start identity), ``dead`` (a reaped PID), ``overflow`` (a
    legacy or corrupt PID beyond a C int) or ``unreadable`` (a record that
    does not decode)."""
    db = sys.modules["_cctally_db"]
    if owner == "unreadable":
        marker.write_text("{}")
        return
    pid = {"dead": _dead_pid(), "overflow": 2 ** 40}.get(owner, os.getpid())
    start = (db._process_start_identity(os.getpid()) if owner == "live"
             else "proc:1")
    assert start, "premise: this platform reports a process start identity"
    marker.write_text(json.dumps({
        "version": db._REPAIR_OWNER_VERSION, "pid": pid,
        "process_start": start, "claim_id": f"test-{owner}"}))


@pytest.mark.parametrize(("owner", "query_reason"), [
    ("live", "maintenance"),
    # A query never reads the start identity (it would spawn `ps` on
    # macOS), so a reused live PID reads as live; only the owner open, with
    # the full check, recovers it.
    ("reused", "maintenance"),
    ("dead", "recovery_pending"),
    # F9: `os.kill` raises OverflowError for a PID beyond a C int; such an
    # owner is not alive, not an untyped exception out of every session.
    ("overflow", "recovery_pending"),
    ("unreadable", "recovery_pending"),
    ("pending", "recovery_pending"),
])
def test_query_classifies_a_cache_repair_marker_by_its_owner(
        env, monkeypatch, owner, query_reason):
    """R32-1 (#787 / #861 revision 32): a cache.db repair marker whose owner
    record decodes and names a live PID is a repair in progress, refused as
    `maintenance` as today's owner open reported it, so the facades keep
    their direct-JSONL fallback; a marker with no decodable record or a dead
    owner, and a pending quarantine without a marker, stay
    `recovery_pending` (the owner's to recover). Diagnostic sessions keep
    `maintenance` for every marker. Detects a query that reports a live
    repair as an owner-resolved state (the dashboard then kept an empty
    frame where `main` read the transcripts), one that reports a dead
    owner's marker as maintenance (nothing ever recovers it), and a query
    scope that spawns a process to read the owner's start identity."""
    path = _path(env, "cache")
    db, access = env["db"], env["access"]
    if owner == "pending":
        db._quarantine_pending_path(path).write_text("{}")
    else:
        _write_owner_record(db._repair_marker_path(path), owner)

    def no_process(*_a, **_k):
        raise AssertionError("a query scope created a process")

    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    for policy, expected in (("interactive", query_reason),
                             ("command", query_reason),
                             ("diagnostic", "maintenance")):
        with pytest.raises(env["core"].StoreUnavailable) as info:
            with access.QueryService.session("cache", policy=policy):
                pass  # pragma: no cover
        assert (info.value.reason, policy) == (expected, policy)
        text = str(info.value)
        assert (("maintenance is in progress" in text)
                is (expected == "maintenance")), text


@pytest.mark.parametrize("store", ["stats", "conversations"])
def test_a_live_stats_or_conversations_marker_stays_maintenance(env, store):
    """R32-1 changes only cache.db: a stats or conversations repair marker
    refuses a query as `maintenance` whatever its owner record says."""
    path = _path(env, store)
    marker = (env["store"]._stats_repair_marker(path) if store == "stats"
              else env["db"]._repair_marker_path(path))
    for owner in ("live", "dead"):
        _write_owner_record(marker, owner)
        with pytest.raises(env["core"].StoreUnavailable) as info:
            with env["access"].QueryService.session(store):
                pass  # pragma: no cover
        assert info.value.reason == "maintenance", owner


def test_an_owner_conversations_open_refused_at_cache_closes_its_connection(
        env, monkeypatch):
    """The owner's conversations open attaches cache.db; while another
    process repairs cache.db that attach is refused, and the writer
    connection it had opened must close before the refusal propagates.
    Detects the leak the Task C recorder caught (R32-V5 live-owner state
    under `dashboard --no-sync`): the abandoned connection was closed later
    by the garbage collector, inside a request's query scope, and that
    close checkpointed and deleted conversations.db-wal/-shm, owner work
    a query scope must never do."""
    store, cache = env["store"], env["cache"]
    opened = []
    real_open_index = store.open_index

    def recording_open_index(name, *args, **kwargs):
        conn = real_open_index(name, *args, **kwargs)
        if name == "conversations":
            opened.append(conn)
        return conn

    monkeypatch.setattr(store, "open_index", recording_open_index)
    _write_owner_record(env["db"]._repair_marker_path(_path(env, "cache")),
                        "live")
    with env["core"].establish_authority("process"):
        with pytest.raises(sqlite3.DatabaseError,
                           match="maintenance is in progress"):
            cache.open_conversations_db()
    assert opened, "premise: the owner opened a conversations connection"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


@pytest.mark.parametrize("artifact", ["rebuild", "publication"])
def test_diagnostic_admits_stats_recovery_artifacts_without_recovering(
        env, artifact):
    """N2: stats interrupted-rebuild artifacts and a publication marker are
    admitted read-only to a diagnostic session, and recovery does not run."""
    path = _path(env, "stats")
    target = (path.with_name(path.name + ".rebuilding-20260101T000000_000000")
              if artifact == "rebuild"
              else env["store"]._stats_publication_marker(path))
    target.write_text("{}" if artifact == "publication" else "")
    with env["access"].QueryService.session("stats",
                                            policy="diagnostic") as s:
        assert s.readiness["primary"].reason == "recovery_pending"
        s.conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    assert target.exists()
    with pytest.raises(env["core"].StoreUnavailable) as info:
        with env["access"].QueryService.session("stats"):
            pass  # pragma: no cover
    assert info.value.reason == "recovery_pending"


def test_diagnostic_admits_fenced_cache(env):
    """N2: a fenced cache is admitted read-only to a diagnostic session with
    readiness recovery_pending, and the fence stays."""
    _seed(env, "cache", "fence")
    with env["access"].QueryService.session("cache",
                                            policy="diagnostic") as s:
        assert s.readiness["primary"].reason == "recovery_pending"
    with pytest.raises(env["core"].StoreUnavailable):
        with env["access"].QueryService.session("cache"):
            pass  # pragma: no cover


# ── group 3: TEMP, ATTACH and identity ───────────────────────────────────


def _conv_session(env, **kw):
    return env["access"].QueryService.session(
        "conversations", attach=("cache",), **kw)


def test_in_session_scope_never_commits(env, monkeypatch):
    """Detects account TEMP scope that commits or runs executescript inside
    a session (`_cctally_cache.py` executescript/commit), which ends the
    snapshot the identity was read from."""
    access = env["access"]
    commits = []
    monkeypatch.setattr(access._QueryConnection, "commit",
                        lambda self: commits.append(1), raising=False)
    with _conv_session(env, account="unattributed") as s:
        assert s.conn.in_transaction
        assert s.scope == "unattributed"
        assert s.conn.execute(
            "SELECT count(*) FROM conversation_messages").fetchone()[0] == 0
        assert s.conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE name = "
            "'_conversation_account_scope'").fetchone()
    assert commits == []


def test_attached_cache_readonly_and_admitted(env):
    """Detects an attached cache that is writable or not admitted: a write to
    it is denied and latched, and cache maintenance refuses the session."""
    core = env["core"]
    with pytest.raises(core.QueryAuthorityViolation):
        with _conv_session(env) as s:
            try:
                s.conn.execute("INSERT INTO cache_db.cache_meta(key, value) "
                               "VALUES ('k', 'v')")
            except sqlite3.DatabaseError:
                pass  # swallowed: the session exit still refuses
    fh = open(_lock(env, "cache"), "r")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        with pytest.raises(core.StoreUnavailable) as info:
            with _conv_session(env):
                pass  # pragma: no cover
    finally:
        fh.close()
    assert info.value.reason == "maintenance"


def test_qualified_unqualified_equivalence(env):
    """Detects a session whose qualified (account TEMP scope) or unqualified
    reads differ from today's reader on the same store."""
    cache = env["cache"]
    legacy = cache.open_conversations_db_readonly()
    try:
        legacy.execute("BEGIN")
        cache.scope_conversations_db_to_account(legacy, "unattributed")
        expected = legacy.execute(
            "SELECT name, type FROM sqlite_temp_master ORDER BY name"
        ).fetchall()
    finally:
        import _lib_codex_metadata
        _lib_codex_metadata.invalidate_codex_threads_table_resolution(legacy)
        legacy.close()
    with _conv_session(env, account="unattributed") as s:
        got = s.conn.execute(
            "SELECT name, type FROM sqlite_temp_master ORDER BY name"
        ).fetchall()
    assert got == expected
    with _conv_session(env) as s:
        assert s.conn.execute(
            "SELECT count(*) FROM conversation_messages").fetchone() == (0,)


def _commit_conversations_meta(env, key):
    conn = sqlite3.connect(_path(env, "conversations"), timeout=5)
    conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, 'v')", (key,))
    conn.commit()
    conn.close()


def test_interleaving_commit_between_identity_and_temp(env, monkeypatch):
    """Detects a split snapshot: a writer commit after the identity reads and
    before the TEMP build must not be visible to the session."""
    cache = env["cache"]
    real = cache.scope_conversations_db_to_account

    def interleaved(conn, account, **kw):
        _commit_conversations_meta(env, "interleave-a")
        return real(conn, account, **kw)

    monkeypatch.setattr(cache, "scope_conversations_db_to_account",
                        interleaved)
    with _conv_session(env, account="unattributed") as s:
        assert s.conn.execute("SELECT 1 FROM main.cache_meta WHERE key = "
                              "'interleave-a'").fetchone() is None


def test_interleaving_commit_between_temp_and_consume(env):
    """Detects a session that re-reads after its TEMP build: a commit then
    must not be visible to the consumer reads."""
    with _conv_session(env, account="unattributed") as s:
        _commit_conversations_meta(env, "interleave-b")
        assert s.conn.execute("SELECT 1 FROM main.cache_meta WHERE key = "
                              "'interleave-b'").fetchone() is None


def _replace_same_content(path: pathlib.Path):
    tmp = path.with_name(path.name + ".replacement")
    shutil.copyfile(path, tmp)
    os.replace(tmp, path)


@pytest.mark.parametrize("point", ["pre_open", "post_read"])
def test_replaced_file_fails_binding(env, monkeypatch, point):
    """Detects an identity bound to a file other than the one read: a
    same-content (repeated incarnation) replacement between the pre-open
    stat and the open, or between the open and the post-read stat, retries
    once and then raises busy, never yielding an identity."""
    access = env["access"]
    path = _path(env, "cache")
    replaced = []
    if point == "pre_open":
        real = access._connect_ro

        def connect(store, p, policy, tag):
            _replace_same_content(path)
            replaced.append(1)
            return real(store, p, policy, tag)
        monkeypatch.setattr(access, "_connect_ro", connect)
    else:
        real = access._read_store

        def read(conn, store, schema, **kw):
            out = real(conn, store, schema, **kw)
            _replace_same_content(path)
            replaced.append(1)
            return out
        monkeypatch.setattr(access, "_read_store", read)
    yielded = []
    with pytest.raises(env["core"].StoreUnavailable) as info:
        with access.QueryService.session("cache") as s:
            yielded.append(s.identity)  # pragma: no cover
    assert info.value.reason == "busy"
    assert replaced == [1, 1]
    assert yielded == []


def test_none_identity_never_equal(env):
    """Detects identity reuse across a missing leg: a None in any compared
    field makes two identities unequal."""
    access = env["access"]
    base = dict(file=(1, 2), user_version=5, incarnation="i", protocol=None,
                content_revision=None, migration_state="m", rollup_epoch=None,
                store="cache")
    a = access.StoreIdentity(**base)
    assert access.SessionIdentity({"primary": a}) == access.SessionIdentity(
        {"primary": access.StoreIdentity(**base)})
    missing = access.StoreIdentity(**{**base, "incarnation": None})
    assert access.SessionIdentity({"primary": missing}) != \
        access.SessionIdentity({"primary": missing})
    # A conversations session without the cache attachment has no rollup
    # epoch, so two of them never compare equal.
    with access.QueryService.session("conversations") as s1:
        i1 = s1.identity
    with access.QueryService.session("conversations") as s2:
        i2 = s2.identity
    assert i1 != i2


def test_query_authorizer_not_reused_by_owner(env):
    """#778 shape: the query denial holds for the connection's lifetime,
    also when the same statement is re-executed under an owner scope (a
    cached compiled statement would skip the authorizer)."""
    core, access = env["core"], env["access"]
    sql = "INSERT INTO weekly_usage_snapshots(captured_at_utc) VALUES ('x')"
    with pytest.raises(core.QueryAuthorityViolation):
        with access.QueryService.session("stats") as s:
            for _ in range(2):
                with pytest.raises(sqlite3.DatabaseError):
                    s.conn.execute(sql)
            with core.establish_authority("process"):
                with pytest.raises(sqlite3.DatabaseError):
                    s.conn.execute(sql)
    # An owner connection is unaffected by the query authorizer.
    owner = sqlite3.connect(_path(env, "stats"))
    owner.execute("CREATE TEMP TABLE probe(x)")
    owner.close()


def test_attached_cache_admission_order(env, monkeypatch):
    """Detects attached-cache admission in the wrong lock order (conversations
    maintenance, then cache maintenance — the full opener's order)."""
    access = env["access"]
    order = []
    real = access._admit
    monkeypatch.setattr(access, "_admit",
                        lambda store, policy: order.append(store)
                        or real(store, policy))
    with _conv_session(env):
        pass
    assert order == ["conversations", "cache"]
    for held in ("conversations", "cache"):
        order.clear()
        fh = open(_lock(env, held), "r")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
            with pytest.raises(env["core"].StoreUnavailable) as info:
                with _conv_session(env):
                    pass  # pragma: no cover
        finally:
            fh.close()
        assert info.value.reason == "maintenance"
        assert order[-1] == held


# ── QueryService entry readers (spec 5.11) ───────────────────────────────

_READERS = ["entries", "claude_session_entries", "codex_entries"]
_FACADES = {"entries": "get_entries",
            "claude_session_entries": "get_claude_session_entries",
            "codex_entries": "get_codex_entries"}


def _seed_claude_rows(env):
    conn = sqlite3.connect(_path(env, "cache"))
    try:
        for i, ts in enumerate(("2026-11-01T10:00:00+00:00",
                                "2026-11-02T11:00:00+00:00")):
            conn.execute(
                "INSERT INTO session_entries(source_path, line_offset, "
                "timestamp_utc, model, input_tokens, output_tokens) "
                "VALUES (?, ?, ?, 'claude-opus-4-1', 10, 20)",
                (f"/p/projects/-x/{i}.jsonl", i, ts))
        conn.commit()
    finally:
        conn.close()


def _forbid_owner_reach(env, monkeypatch):
    cache = env["cache"]
    for name in ("sync_cache", "sync_codex_cache", "open_cache_db",
                 "_collect_entries_direct", "_collect_codex_entries_direct",
                 "_direct_parse_claude_session_entries"):
        monkeypatch.setattr(cache, name, lambda *a, _n=name, **k: pytest.fail(
            f"query reader reached {_n}"))


@pytest.mark.parametrize("reader", _READERS)
def test_query_entry_readers_match_facade_without_sync(env, monkeypatch,
                                                       reader):
    """Detects a pure query reader that syncs, opens the full opener, falls
    back to direct JSONL or leaves an owner effect, or that reads different
    rows than the facade's kernel over the same retained cache."""
    import datetime as dt
    core, access = env["core"], env["access"]
    _seed_claude_rows(env)
    start = dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc)
    with core.establish_authority("process"):
        expected = getattr(env["cache"], _FACADES[reader])(
            start, end, skip_sync=True)
    if reader != "codex_entries":
        assert len(expected) == 2
    _forbid_owner_reach(env, monkeypatch)
    before = _listing(env["root"])
    with core.establish_authority("query"):
        got = getattr(access.QueryService, reader)(start, end)
    assert got == expected
    _assert_only_allowed_effects(env, "cache", before)


@pytest.mark.parametrize("reader", _READERS)
def test_query_entry_readers_raise_typed_unavailability(env, monkeypatch,
                                                        reader):
    """Detects a query reader that resolves an absent cache inline (create,
    migrate) or degrades it to direct JSONL instead of raising the typed
    reason its caller translates (spec 5.11)."""
    import datetime as dt
    core, access = env["core"], env["access"]
    base = _path(env, "cache")
    for suffix in ("", "-wal", "-shm"):
        pathlib.Path(str(base) + suffix).unlink(missing_ok=True)
    _forbid_owner_reach(env, monkeypatch)
    now = dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc)
    with core.establish_authority("query"):
        with pytest.raises(core.StoreUnavailable) as info:
            getattr(access.QueryService, reader)(now, now)
    assert info.value.reason == "absent"
    assert not base.exists()


def _install_legacy_fixture(env):
    fixture = (REPO / "tests/fixtures/migrations/per-migration/"
               "009_recompute_five_hour_blocks_dedup_fix")
    for store, name in (("stats", "pre.sqlite"), ("cache", "pre-cache.sqlite")):
        path = _path(env, store)
        for suffix in ("", "-wal", "-shm", "-journal"):
            pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)
        shutil.copyfile(fixture / name, path)


def _legacy_facade_read(env, monkeypatch):
    """Today's facade read: the entries read on the owner connection."""
    cache = env["cache"]

    def owner_read(read, *, account_key, fallback):
        conn = cache.open_cache_db()
        try:
            return read(conn)
        finally:
            conn.close()

    monkeypatch.setattr(cache, "_facade_session_read", owner_read)


@pytest.mark.parametrize("argv", [["report", "--json"],
                                  ["weekly", "--json"]])
def test_cli_legacy_store_output_unchanged(env, monkeypatch, capsys, argv):
    """AD8 / 5.11: on a deferred-legacy store (user_version <= 13 behind the
    cutover baseline) `report` and `weekly` print byte-for-byte what they
    printed when their reads ran on the owner connections (detects a query
    phase that refuses, re-reads or re-buckets a legacy store)."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-01T00:00:00Z")
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    main = env["ns"]["main"]
    _install_legacy_fixture(env)
    with monkeypatch.context() as m:
        _legacy_facade_read(env, m)
        rc_owner = main(list(argv))
    owner = capsys.readouterr().out
    _install_legacy_fixture(env)
    rc_session = main(list(argv))
    session = capsys.readouterr().out
    assert (rc_session, session) == (rc_owner, owner)


def test_command_session_drains_a_cli_obligation_through_the_owner_opener(
        env, monkeypatch):
    """AD2 / 5.11: a readable store carrying a CLI-drained obligation runs
    the existing full opener once, at the command's `process` authority,
    before the query phase (detects a startup decision taken inside the
    already-narrowed session, which never drains a legacy migration, a
    missing content revision or a migration-error sentinel on a CLI run;
    bin/cctally-5h-canonical-test scenario F)."""
    access = env["access"]
    core = env["core"]
    # A readable rollback-journal store at the legacy head (scenario F's
    # shape): a pre-journal upgrade DB, readable with the obligation.
    _write_stats(env, "DELETE FROM stats_open_fixups")
    _write_stats(env, f"PRAGMA user_version = {core.LEGACY_STATS_HEAD}")
    with access.QueryService.session("stats") as s:
        assert s.readiness["primary"].readable
        assert ("legacy_migration_pending"
                in s.readiness["primary"].obligations)
    ensured = []
    real = access.StartupService.ensure

    def spy(store):
        ensured.append((store, core.current_authority().kind))
        return real(store)

    monkeypatch.setattr(access.StartupService, "ensure", staticmethod(spy))
    with core.establish_authority("process"):
        with access.command_session("stats") as s:
            s.conn.execute("SELECT 1").fetchone()
    assert ensured == [("stats", "process")]


def _observe(env):
    events = []
    env["core"].CAPABILITY_OBSERVERS.append(events.append)
    return events


_CODEX_QUOTA_NO_SYNC = [["history"], ["statusline"], ["forecast"], ["blocks"],
                        ["breakdown", "--reset-at", "2026-11-01T00:00:00Z"]]


@pytest.mark.parametrize("sub", _CODEX_QUOTA_NO_SYNC, ids=lambda s: s[0])
def test_codex_quota_no_sync_still_reconciles(env, monkeypatch, capsys, sub):
    """Spec 5.11: `--no-sync` suppresses exactly today's ingestion, and
    `codex quota` keeps its projection reconcile under `--no-sync` (detects a
    refresh phase that drops the reconcile with the ingest)."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    events = _observe(env)
    try:
        env["ns"]["main"](["codex", "quota", *sub, "--no-sync"])
    finally:
        env["core"].CAPABILITY_OBSERVERS.remove(events.append)
    capsys.readouterr()
    entries = [e.entry for e in events]
    assert "reconcile_codex_quota_projection" in entries
    assert not [e for e in entries if e.startswith("sync_")], entries


@pytest.mark.parametrize("argv", [["forecast", "--no-sync"]] + [
    ["codex", "quota", *sub, "--no-sync"] for sub in _CODEX_QUOTA_NO_SYNC])
def test_no_sync_suppresses_only_ingest(env, monkeypatch, capsys, argv):
    """`--no-sync` suppresses only the ingest: no `sync_*` owner entry runs,
    while the startup phase's owner opens still do (create, migrate, recover
    as today). The stats store is absent, so the startup phase has to run
    (AD2 reads a ready store without the owner opener)."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    _path(env, "stats").unlink()
    events = _observe(env)
    try:
        env["ns"]["main"](list(argv))
    finally:
        env["core"].CAPABILITY_OBSERVERS.remove(events.append)
    capsys.readouterr()
    entries = [e.entry for e in events]
    assert not [e for e in entries if e.startswith("sync_")], entries
    assert {"open_db", "open_cache_db"} & set(entries), entries
    assert all(e.authority in ("process", "query", "diagnostic")
               for e in events)


def _stats_write_now(env) -> "str | None":
    """One stats.db write that must not wait: ``timeout=0`` makes a SHARED
    lock still held by a query session fail the commit at once. Returns
    None on success, else the SQLite error."""
    conn = sqlite3.connect(str(_path(env, "stats")), timeout=0,
                           isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t787_writer VALUES (1)")
        conn.execute("COMMIT")
        return None
    except sqlite3.OperationalError as exc:
        return str(exc)
    finally:
        conn.close()


def _arm_stats_writer(env):
    conn = sqlite3.connect(str(_path(env, "stats")))
    try:
        conn.execute("CREATE TABLE t787_writer (x INTEGER)")
        conn.commit()
    finally:
        conn.close()


# Each command's step right after one of its stats reads returned and
# before the command continues: the forecast inputs read and the project
# command's subscription-week walk.
_COMPUTE_HOOKS = {
    "forecast": (["forecast", "--no-sync", "--json"],
                 "_load_forecast_inputs", "after"),
    "project": (["project", "--json"], "_compute_subscription_weeks",
                "after"),
}


@pytest.mark.parametrize("command", sorted(_COMPUTE_HOOKS))
def test_stats_writer_commits_while_cli_command_computes(
        env, monkeypatch, capsys, command):
    """AD21: a CLI command reads stats.db in short read units, so a
    `record-usage` style stats write commits immediately while the command
    computes between its reads, and a ready store's reads never take the
    owner opener (detects a session or owner connection held across the
    command's computation, which on a rollback-journal stats.db holds
    SHARED and makes every status-line write wait or fail)."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-01T00:00:00Z")
    argv, hook, when = _COMPUTE_HOOKS[command]
    _arm_stats_writer(env)
    ns = env["ns"]
    original = ns[hook]
    outcomes = []

    def computing(*args, **kwargs):
        if when == "before":
            outcomes.append(_stats_write_now(env))
        result = original(*args, **kwargs)
        if when == "after":
            outcomes.append(_stats_write_now(env))
        return result

    monkeypatch.setitem(ns, hook, computing)
    events = _observe(env)
    try:
        rc = ns["main"](list(argv))
    finally:
        env["core"].CAPABILITY_OBSERVERS.remove(events.append)
    capsys.readouterr()
    assert rc == 0
    assert outcomes and all(o is None for o in outcomes), outcomes
    entries = [e.entry for e in events]
    assert "open_db" not in entries, entries


def test_stats_writer_commits_while_dashboard_computes(env, monkeypatch):
    """AD21, the dashboard twin of the test above (#787 / #861 I3b): while
    the dashboard's snapshot build computes between its captures (after the
    weekly and daily panel reads return), a `record-usage` style stats write
    commits at once. Detects a session or transaction held across the
    dashboard build, which on a rollback-journal stats.db holds SHARED and
    makes every status-line write wait or fail for the build's duration.
    The source build's post-capture Codex fold is the third point (I5b),
    and inside the Codex capture group, after its quota observations, the
    fourth (AD29): the group reads stats per statement, as base did
    (`_cctally_tui.py:4544-4555`), so no stats session is held across the
    capture either. Detects a stats session held for the capture group,
    which on a large install stalls `record-usage` and the status line's
    fail-fast reader behind a capture of several seconds."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    _arm_stats_writer(env)
    ns, core = env["ns"], env["core"]
    cctally = sys.modules["cctally"]
    tui = sys.modules["_cctally_tui"]
    sources = sys.modules["_cctally_dashboard_sources"]
    outcomes = []
    # (module, hook, write before the call): the panels write after their
    # reads return, the fold before it runs (its capture just rolled back),
    # the capture group after its quota observations (it is mid-capture).
    for module, hook, before in (
            (cctally, "_dashboard_build_weekly_periods", False),
            (cctally, "_dashboard_build_daily_panel", False),
            (tui, "build_codex_source_state_from_capture", True),
            (sources, "_cached_codex_quota_observations", False)):
        original = getattr(module, hook)

        def computing(*args, _original=original, _before=before, **kwargs):
            if _before:
                outcomes.append(_stats_write_now(env))
            result = _original(*args, **kwargs)
            if not _before:
                outcomes.append(_stats_write_now(env))
            return result

        monkeypatch.setattr(module, hook, computing)
    now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    with core.establish_authority("process"):
        with core.operation_boundary("snapshot-build"):
            ns["_tui_build_snapshot"](
                now_utc=now, skip_sync=True, precompute_envelope=True,
                runtime_bind="127.0.0.1")
    assert len(outcomes) >= 4, "non-vacuity: a hooked build step did not run"
    assert all(o is None for o in outcomes), outcomes


def _observe_build(env, monkeypatch):
    """One ordered log, across threads, of every query session's ENCLOSING
    authority (``_session`` narrows on entry, so the authority inside it is
    always its own) and every owner-entry capability event."""
    access, core = env["access"], env["core"]
    log: list = []
    guard = threading.Lock()
    real_session = access._session

    @contextlib.contextmanager
    def observed(primary, **kwargs):
        a = core.current_authority()
        with guard:
            log.append(("session", primary, a.kind if a else None,
                        kwargs.get("policy")))
        with real_session(primary, **kwargs) as s:
            yield s

    # AD28: the Doctor precompute's gather is owner work at `process`; its
    # window is marked so its diagnostic sessions are judged as such.
    cctally = sys.modules["cctally"]
    real_gather = cctally.doctor_gather_state

    def gather(*a, **k):
        auth = core.current_authority()
        with guard:
            log.append(("doctor", "begin", auth.kind if auth else None))
        try:
            return real_gather(*a, **k)
        finally:
            with guard:
                log.append(("doctor", "end", None))

    monkeypatch.setitem(cctally.__dict__, "doctor_gather_state", gather)

    def observer(ev):
        with guard:
            log.append(("entry", ev.entry, ev.authority, ev.permitted))

    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        a = core.current_authority()
        factory = kwargs.get("factory")
        with guard:
            log.append(("connect", str(database),
                        getattr(factory, "__name__", None),
                        a.kind if a else None))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(access, "_session", observed)
    monkeypatch.setattr(sqlite3, "connect", connect)
    core.CAPABILITY_OBSERVERS.append(observer)
    return log, lambda: core.CAPABILITY_OBSERVERS.remove(observer)


def _doctor_window(log):
    """Indices of the log entries inside a Doctor gather (AD28)."""
    inside, depth = set(), 0
    for i, e in enumerate(log):
        if e[:2] == ("doctor", "begin"):
            depth += 1
        elif e[:2] == ("doctor", "end"):
            depth -= 1
        elif depth:
            inside.add(i)
    return inside


def _assert_build_reads_under_query(log, app_dir):
    sessions = [e for e in log if e[0] == "session"]
    assert sessions, "non-vacuity: the build opened no query session"
    doctor = _doctor_window(log)
    # The build's reads narrow to query. The Doctor precompute's gather runs
    # at `process` outside the narrowed reads (AD28), and its database legs
    # open diagnostic sessions there.
    gathers = [e for e in log if e[:2] == ("doctor", "begin")]
    assert {e[2] for e in gathers} <= {"process"}, gathers
    build_reads = [e for i, e in enumerate(log)
                   if e[0] == "session" and i not in doctor]
    doctor_reads = [e for i, e in enumerate(log)
                    if e[0] == "session" and i in doctor]
    assert {e[2] for e in build_reads} == {"query"}, build_reads
    assert all(e[2] == "process" and e[3] == "diagnostic"
               for e in doctor_reads), doctor_reads
    refused = [e for e in log if e[0] == "entry" and not e[3]]
    assert not refused, refused
    first = log.index(sessions[0])
    owner_inside = [e for i, e in enumerate(log[first:], start=first)
                    if e[0] == "entry" and e[2] == "process"
                    and i not in doctor]
    assert not owner_inside, (
        "an owner entry ran inside the snapshot build, after its first "
        f"query read: {owner_inside}")
    # Spec 5.4 / X16: under a query scope only session-factory connections,
    # `:memory:` and read-only files outside the data directory open.
    apps = {str(pathlib.Path(app_dir)), str(pathlib.Path(app_dir).resolve())}
    stray = [e for e in log if e[0] == "connect" and e[3] == "query"
             and e[2] != "_QueryConnection" and e[1] != ":memory:"
             and any(a in e[1] for a in apps)]
    assert not stray, stray


def test_dashboard_snapshot_build_reads_under_query(env, monkeypatch):
    """#787 / #861 spec 5.12, AD21: the dashboard's shared snapshot build
    narrows to `query(interactive)`. The owner opens (create, migrate,
    epoch deferral, worker launch) run at `process` before it, every store
    read inside it is a query session, and no owner entry is reached inside
    it. Detects a build that reads through owner openers or facades at
    `process`, which can create, migrate, recover or launch from a read."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    ns, core = env["ns"], env["core"]
    log, done = _observe_build(env, monkeypatch)
    now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    try:
        with core.establish_authority("process"):
            with core.operation_boundary("snapshot-build") as latch:
                snap = ns["_tui_build_snapshot"](
                    now_utc=now, skip_sync=True, precompute_envelope=True,
                    runtime_bind="127.0.0.1")
    finally:
        done()
    assert not latch.violated, latch.events
    assert snap.last_sync_error is None, snap.last_sync_error
    _assert_build_reads_under_query(log, env["app"])


def test_dashboard_build_never_enters_the_owner_qualified_codex_read(
        env, monkeypatch):
    """Census rule A (Task C recorder, every dashboard trial on cf5f332e3):
    `load_qualified_codex_entries` is an owner function (it may open the
    owner cache connection and sync), so the snapshot build's narrowed Codex
    accounting read must use the query variant over its own cache session.
    Detects the owner function entered under `query`."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    ns, core = env["ns"], env["core"]
    analytics = sys.modules["_cctally_source_analytics"]
    watched = {analytics.load_qualified_codex_entries.__code__: "owner"}
    query_variant = getattr(analytics, "query_qualified_codex_entries", None)
    if query_variant is not None:
        watched[query_variant.__code__] = "query-variant"
    calls: list = []

    def profile(frame, event, _arg):
        if event == "call" and frame.f_code in watched:
            a = core.current_authority()
            calls.append((watched[frame.f_code], a.kind if a else None))

    now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    with core.establish_authority("process"):
        sys.setprofile(profile)
        try:
            ns["_tui_build_snapshot"](
                now_utc=now, skip_sync=True, precompute_envelope=True,
                runtime_bind="127.0.0.1")
        finally:
            sys.setprofile(None)
    assert ("owner", "query") not in calls, calls
    assert ("query-variant", "query") in calls, (
        "non-vacuity: the build issued no qualified Codex read", calls)


def test_tui_sync_thread_build_reads_under_query(env, monkeypatch):
    """#787 / #861 spec 5.12: the TUI sync thread establishes `process`,
    refreshes (the ingest) at `process`, then builds its snapshot narrowed to
    `query`. Detects a TUI thread with no authority (every owner entry then
    raises once enforcement is final) and a build that reads through owner
    openers instead of query sessions."""
    ns = env["ns"]
    tui = sys.modules["_cctally_tui"]
    cctally = sys.modules["cctally"]
    log, done = _observe_build(env, monkeypatch)
    now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    outcomes: list = []
    built = threading.Event()
    real_build = cctally._tui_build_snapshot

    def build(**kwargs):
        try:
            outcomes.append(real_build(**kwargs))
        except BaseException as exc:  # noqa: BLE001 — recorded, re-raised
            outcomes.append(exc)
            raise
        finally:
            built.set()

    monkeypatch.setattr(cctally, "_tui_build_snapshot", build)
    ref = ns["_SnapshotRef"](tui._tui_empty_snapshot(now))
    thread = tui._TuiSyncThread(ref, 60.0, skip_sync=False, now_utc=now)
    thread.start()
    try:
        assert built.wait(60), "the TUI sync thread never built"
    finally:
        thread.stop()
        done()
    assert len(outcomes) == 1 and not isinstance(outcomes[0], BaseException), (
        outcomes)
    assert outcomes[0].last_sync_error is None, outcomes[0].last_sync_error
    assert [e for e in log if e[0] == "entry" and e[1] == "sync_cache"], (
        "non-vacuity: the refresh did not ingest")
    _assert_build_reads_under_query(log, env["app"])


def _touch_stats_key(env):
    """Move stats.db's stat key without changing its content, as any stats
    commit does: the next query open must probe the header again (AD19)."""
    path = _path(env, "stats")
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def _slow_codex_fold(env, monkeypatch, delay=0.6):
    """Make the source build's post-capture Codex fold (pure computation over
    the captured generation) outlast the 500 ms interactive deadline, and
    commit-like move the stats stat key inside it. Returns the call list."""
    tui = sys.modules["_cctally_tui"]
    real = tui.build_codex_source_state_from_capture
    calls: list = []

    def slow(*args, **kwargs):
        calls.append(1)
        time.sleep(delay)
        _touch_stats_key(env)
        return real(*args, **kwargs)

    monkeypatch.setattr(tui, "build_codex_source_state_from_capture", slow)
    return calls


def _slow_codex_capture(env, monkeypatch, delay=0.6):
    """Make the Codex capture group's reads before its projection-coherence
    read (the quota observations, AD21's existing atomic group inside the
    cache-backed capture) outlast the 500 ms interactive deadline, and
    commit-like move the stats stat key inside that window. Returns the call
    list."""
    sources = sys.modules["_cctally_dashboard_sources"]
    real = sources._cached_codex_quota_observations
    calls: list = []

    def slow(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append(1)
        time.sleep(delay)
        _touch_stats_key(env)
        return result

    monkeypatch.setattr(sources, "_cached_codex_quota_observations", slow)
    return calls


_SLOW_POINTS = {
    "post_capture_fold": _slow_codex_fold,
    "codex_capture_group": _slow_codex_capture,
}


def _build_dashboard_snapshot(env):
    core = env["core"]
    now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    with core.establish_authority("process"):
        with core.operation_boundary("snapshot-build") as latch:
            snap = env["ns"]["_tui_build_snapshot"](
                now_utc=now, skip_sync=True, precompute_envelope=True,
                runtime_bind="127.0.0.1")
    assert not latch.violated, latch.events
    return snap


def test_source_build_survives_computation_past_the_capture_deadline(
        env, monkeypatch):
    """#787 / #861 F1 (AD21, 5.3): the dashboard source build's cache
    capture session ends at its rollback, so a post-capture fold that runs
    longer than the 500 ms interactive deadline, during which a stats commit
    moves stats.db's stat key, leaves the later stats reads (the account
    wire, the post-build reconciliation) their own outermost sessions and
    deadlines. Base returned the bundle. Detects the capture session held
    across that computation: every later stats read is then nested under a
    spent deadline, its header probe refuses without spawning, and the build
    raises and keeps the prior bundle with a sync error."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    calls = _slow_codex_fold(env, monkeypatch)
    snap = _build_dashboard_snapshot(env)
    assert calls, "non-vacuity: the build ran no post-capture Codex fold"
    assert snap.last_sync_error is None, snap.last_sync_error
    assert snap.source_bundle is not None
    assert set(snap.source_bundle.sources) >= {"claude", "codex"}


def _codex_result(snap):
    state = snap.source_bundle.sources["codex"]
    return (state.availability, state.freshness, state.warnings, state.data)


def test_codex_capture_group_survives_reads_past_its_deadline(
        env, monkeypatch):
    """#787 / #861 I5b, AD29 (AD21, 5.3): the Codex capture group reads
    stats per statement inside its cache session, as base did, and each of
    those reads is a nested session admitted against the operation's
    admission budget, which the group's long cache reads never spend. When
    the group's earlier reads outlast 500 ms and a stats commit moves
    stats.db's stat key in that window, the capture still succeeds and the
    published Codex source equals the one an unhurried build publishes, as
    base returned it. Detects a wall-clock deadline that computation spends:
    the late nested session then finds it spent, its header probe refuses
    without spawning, and the capture fails, keeping the prior state with a
    sync error on a real-size install."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    reference = _build_dashboard_snapshot(env)
    assert reference.last_sync_error is None, reference.last_sync_error
    # A cold second build, so it captures Codex again instead of reusing the
    # reference's generation.
    sys.modules["_lib_snapshot_cache"].reset_dispatch_state()
    calls = _slow_codex_capture(env, monkeypatch)
    snap = _build_dashboard_snapshot(env)
    assert calls, "non-vacuity: the build ran no Codex capture group"
    assert snap.last_sync_error is None, snap.last_sync_error
    assert snap.source_bundle is not None
    assert _codex_result(snap) == _codex_result(reference)


@pytest.mark.parametrize("slow_point", sorted(_SLOW_POINTS))
def test_no_session_is_refused_because_computation_elapsed(
        env, monkeypatch, slow_point):
    """#787 / #861 AD29 guard (5.3, AD21): only admission draws on an
    operation's admission budget, so no session is refused for budget
    exhaustion merely because computation elapsed, nested or not. Records
    every query session the dashboard snapshot build enters (and whether it
    is nested in another), drives the build with a stretch longer than the
    500 ms interactive budget at two points (the post-capture computation,
    and inside the Codex capture group before its later per-statement stats
    reads) while a stats commit moves the stat key, and asserts that every
    session was admitted. Replaces I5's wall-clock rule (no nested session
    after the outer deadline), which forced atomic groups to hold sessions
    across their reads. Detects a deadline that computation spends: the
    late nested stats session's header probe refuses without spawning, or
    its busy timeout is zero, on a store another process is writing."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    access, core = env["access"], env["core"]
    real_session = access._session
    depth = threading.local()
    nested: list = []
    refused: list = []

    @contextlib.contextmanager
    def observed(primary, **kwargs):
        level = getattr(depth, "n", 0)
        if level:
            nested.append(primary)
        depth.n = level + 1
        try:
            try:
                cm = real_session(primary, **kwargs)
                s = cm.__enter__()
            except core.StoreUnavailable as exc:
                refused.append((primary, level > 0, exc.reason, str(exc), [
                    f"{pathlib.Path(f.filename).name}:{f.lineno}:{f.name}"
                    for f in traceback.extract_stack()
                    if f"{os.sep}bin{os.sep}" in f.filename][-4:]))
                raise
            try:
                yield s
            except BaseException:
                if not cm.__exit__(*sys.exc_info()):
                    raise
            else:
                cm.__exit__(None, None, None)
        finally:
            depth.n = level

    monkeypatch.setattr(access, "_session", observed)
    calls = _SLOW_POINTS[slow_point](env, monkeypatch)
    snap = _build_dashboard_snapshot(env)
    assert calls, f"non-vacuity: the build never reached {slow_point}"
    assert nested, "non-vacuity: no read unit nested a session"
    assert not refused, refused[:3]
    assert snap.last_sync_error is None, snap.last_sync_error


class _ControlledClock:
    """A `time.monotonic` that moves only when the test advances it. Every
    admission deadline, budget debit and measured span then reads the same
    controlled time, so an admission's elapsed time is exactly the delay the
    test injected however loaded the machine is, and the AD29 arithmetic is
    asserted exactly instead of within a wall-clock slack (I5h-2: a span of
    0.548 s under parallel load overran the 0.5 s budget)."""

    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


#: Float rounding only: under `_ControlledClock` the debits and the spans
#: are the same sums of injected delays.
_CLOCK_EXACT_S = 1e-9


def _controlled_clock(monkeypatch) -> _ControlledClock:
    """Replace `time.monotonic` (read by `_cctally_core`'s deadlines and
    budget, `_cctally_store_access` and `_time_admissions`) for the test.
    Real sleeps still pass (a bounded interactive lock poll still ends);
    they just cost the controlled clock nothing."""
    clock = _ControlledClock(time.monotonic())
    monkeypatch.setattr(time, "monotonic", clock)
    return clock


def _slow_admission(env, monkeypatch, seconds, stores=("stats",),
                    inside=None, clock=None):
    """Inject an admission delay of up to `seconds` into each `stores`
    admission, bounded by that admission's own deadline (as a lock poll
    is): a real sleep, or under CLOCK an advance of the controlled clock.
    `inside(store)` runs within the admission after the delay. Returns the
    (store, absolute admission deadline, seconds left at entry) list."""
    access, core = env["access"], env["core"]
    real_admit = access._admit
    seen: list = []

    def slow_admit(store, policy):
        if store in stores:
            remaining = core.remaining_deadline()
            seen.append((store, core.admission_deadline(), remaining))
            delay = max(0.0, min(seconds, remaining or 0.0))
            if clock is not None:
                clock.advance(delay)
            else:
                time.sleep(delay)
            if inside is not None:
                inside(store)
        return real_admit(store, policy)

    monkeypatch.setattr(access, "_admit", slow_admit)
    return seen


def _time_admissions(env, monkeypatch):
    """Wall time of every outermost `_build_session` call (the body of one
    admission), successful or not."""
    access = env["access"]
    real_build = access._build_session
    depth = threading.local()
    spans: list = []

    def timed(*args, **kwargs):
        level = getattr(depth, "n", 0)
        depth.n = level + 1
        started = time.monotonic()
        try:
            return real_build(*args, **kwargs)
        finally:
            depth.n = level
            if level == 0:
                spans.append(time.monotonic() - started)

    monkeypatch.setattr(access, "_build_session", timed)
    return spans


def _spent(budget) -> float:
    return budget.total_s - budget.remaining_s


def test_failed_admission_debits_the_read_unit_budget(env, monkeypatch):
    """5.3 / AD29: an admission debits its elapsed time once, in `finally`,
    on failure as on success. A nested stats session is refused (its
    maintenance lock is held) after an injected 100 ms admission delay, and
    the unit's budget is left with exactly its total less the two
    admissions' elapsed times. Detects a charge made only on success (a
    refused store would cost the unit nothing, so a route retrying a held
    store could wait past 500 ms) and a double charge."""
    access, core = env["access"], env["core"]
    clock = _controlled_clock(monkeypatch)
    spans = _time_admissions(env, monkeypatch)
    _slow_admission(env, monkeypatch, 0.1, clock=clock)
    fh = open(_lock(env, "stats"), "r")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        with core.establish_authority("process"):
            with access.QueryService.session("cache"):
                budget = core.admission_budget()
                assert budget is not None and budget.total_s == 0.5
                after_outer = budget.remaining_s
                with pytest.raises(core.StoreUnavailable) as info:
                    with access.QueryService.session("stats"):
                        pass  # pragma: no cover
                after_failed = budget.remaining_s
    finally:
        fh.close()
    assert info.value.reason == "maintenance", info.value
    assert len(spans) == 2 and spans[1] >= 0.1 - _CLOCK_EXACT_S, spans
    assert abs((budget.total_s - after_outer) - spans[0]) <= \
        _CLOCK_EXACT_S, (after_outer, spans)
    assert abs((after_outer - after_failed) - spans[1]) <= \
        _CLOCK_EXACT_S, (after_outer, after_failed, spans)


def test_attached_store_admission_debits_once(env, monkeypatch):
    """5.3 / 5.7 / AD29: an attached store's admission is part of the
    session's one admission, so the unit's budget is debited exactly that
    admission's elapsed time, once. Both stores' admissions are slowed by
    80 ms, and the real `_admit` of the attached cache spends about 100 ms
    polling its maintenance lock, which another descriptor holds
    exclusively until a timer releases it (command policy, which polls until
    its deadline). Detects a per-store debit inside `_admit` on top of the
    session's (the budget spends that 100 ms twice) and an omitted one."""
    access, core = env["access"], env["core"]
    spans = _time_admissions(env, monkeypatch)
    holder = open(_lock(env, "cache"), "r")
    released: list = []

    def release():
        released.append(time.monotonic())
        fcntl.flock(holder, fcntl.LOCK_UN)

    timers: list = []

    def hold_cache(store):
        if store == "cache" and not timers:
            fcntl.flock(holder, fcntl.LOCK_EX)
            timers.append(threading.Timer(0.1, release))
            timers[0].start()

    real_admit = access._admit
    admit_spans: list = []

    def timed_admit(store, policy):
        started = time.monotonic()
        try:
            return real_admit(store, policy)
        finally:
            admit_spans.append((store, time.monotonic() - started))

    monkeypatch.setattr(access, "_admit", timed_admit)
    seen = _slow_admission(env, monkeypatch, 0.08,
                           stores=("conversations", "cache"),
                           inside=hold_cache)
    try:
        with core.establish_authority("process"):
            with access.QueryService.session(
                    "conversations", attach=("cache",), policy="command"):
                budget = core.admission_budget()
                remaining = budget.remaining_s
    finally:
        for t in timers:
            t.join()
        holder.close()
    assert released, "non-vacuity: the cache lock was never held"
    # Non-vacuity: the real cache admission itself spent the hold.
    cache_admit = [s for store, s in admit_spans if store == "cache"]
    assert cache_admit and cache_admit[0] >= 0.08, admit_spans
    assert [s for s, _d, _r in seen] == ["conversations", "cache"], seen
    assert len({d for _s, d, _r in seen}) == 1, seen
    assert len(spans) == 1 and spans[0] >= 0.24, spans
    assert abs((budget.total_s - remaining) - spans[0]) <= \
        _DEADLINE_SLACK_S, (remaining, spans)


def test_admission_nested_inside_an_admission_debits_nothing(
        env, monkeypatch):
    """5.3 / AD29: an admission nested inside another (here a stats session
    opened while a conversations session admits its attached cache) uses the
    enclosing admission's absolute deadline and debits nothing, so the
    budget is debited exactly the enclosing admission's elapsed time.
    Detects a nested admission charged on top of the enclosing one, or
    given a deadline of its own that could outlast the enclosing one."""
    access, core = env["access"], env["core"]
    opened: list = []

    def nested_session(store):
        if store == "cache" and not opened:
            opened.append(True)
            with access.QueryService.session("stats") as s:
                s.conn.execute("SELECT 1").fetchone()

    clock = _controlled_clock(monkeypatch)
    seen = _slow_admission(
        env, monkeypatch, 0.08, stores=("conversations", "cache", "stats"),
        inside=nested_session, clock=clock)
    spans = _time_admissions(env, monkeypatch)
    with core.establish_authority("process"):
        with access.QueryService.session(
                "conversations", attach=("cache",)):
            budget = core.admission_budget()
            remaining = budget.remaining_s
    assert opened, "non-vacuity: no admission nested a session"
    assert [s for s, _d, _r in seen] == ["conversations", "cache", "stats"]
    assert len({d for _s, d, _r in seen}) == 1, seen
    assert len(spans) == 1 and spans[0] >= 0.24 - _CLOCK_EXACT_S, spans
    assert abs((budget.total_s - remaining) - spans[0]) <= \
        _CLOCK_EXACT_S, (remaining, spans)


def test_sequential_read_units_start_fresh_budgets(env, monkeypatch):
    """5.3 / AD29: the budget belongs to one read unit, so two sequential
    outermost sessions each create their own, and each is debited only its
    own admission. Detects a budget carried from one read unit to the next
    (a snapshot build's per-statement reads would then share one 500 ms
    budget and late reads would be refused again, R26-1)."""
    access, core = env["access"], env["core"]
    clock = _controlled_clock(monkeypatch)
    spans = _time_admissions(env, monkeypatch)
    _slow_admission(env, monkeypatch, 0.1, clock=clock)
    units = []
    with core.establish_authority("process"):
        for _ in range(2):
            assert core.admission_budget() is None
            with access.QueryService.session("stats"):
                budget = core.admission_budget()
                units.append((budget, budget.remaining_s))
            assert core.admission_budget() is None
    (first, r1), (second, r2) = units
    assert first is not second
    assert len(spans) == 2, spans
    assert abs((0.5 - r1) - spans[0]) <= _CLOCK_EXACT_S, (r1, spans)
    assert abs((0.5 - r2) - spans[1]) <= _CLOCK_EXACT_S, (r2, spans)


def test_admission_is_admitted_while_budget_remains_and_refused_after(
        env, monkeypatch):
    """5.3 / AD29: within one read unit, sessions are admitted while its
    budget remains, however long the computation between them (0.3 s of
    controlled time before each, so past 500 ms by the second), and refused once
    admission alone has spent it. Each nested stats admission is slowed by
    up to 150 ms and must run the AD19 probe (the stat key moved), which
    declines to spawn with less than its cleanup reserve left. Asserts the
    exact remaining budget after the refusal. Detects a wall-clock deadline
    (the second session is refused), a budget reset inside the unit or an
    omitted charge (no session is ever refused, and more than 500 ms of
    admission is spent), and a double charge (the remainder falls short of
    the total less the admissions' elapsed times)."""
    access, core = env["access"], env["core"]
    reserve = access._HEADER_PROBE_CLEANUP_RESERVE_S
    clock = _controlled_clock(monkeypatch)
    seen = _slow_admission(env, monkeypatch, 0.15, clock=clock)
    spans = _time_admissions(env, monkeypatch)
    admitted: list = []
    refusal = None
    with core.establish_authority("process"):
        with access.QueryService.session("cache"):
            budget = core.admission_budget()
            for _ in range(10):
                clock.advance(0.3)      # computation between sessions
                _touch_stats_key(env)
                entry = budget.remaining_s
                try:
                    with access.QueryService.session("stats") as s:
                        s.conn.execute("SELECT 1").fetchone()
                        admitted.append(entry)
                except core.StoreUnavailable as exc:
                    refusal = (entry, exc)
                    break
            left = budget.remaining_s
    assert len(admitted) >= 2, (admitted, refusal)
    assert refusal is not None, ("the budget was never spent", spans)
    entry, exc = refusal
    assert exc.reason == "unavailable", exc
    assert entry <= 0.15 + reserve + _CLOCK_EXACT_S, (entry, admitted)
    assert len(seen) == len(admitted) + 1, seen
    assert abs((budget.total_s - left) - sum(spans)) <= \
        _CLOCK_EXACT_S, (left, spans)
    assert sum(spans) <= 0.5 + _CLOCK_EXACT_S, spans


def _commit_projection_row(env, root_key):
    """One relevant stats commit: a Codex projection row the source build's
    stats digests read (`codex_stats_digest`)."""
    conn = sqlite3.connect(str(_path(env, "stats")), timeout=5)
    try:
        conn.execute(
            "INSERT INTO quota_projection_state (source_root_key, "
            "physical_signature, generation, completed_at_utc) "
            "VALUES (?, 'signature', 'generation', ?)",
            (root_key, "2026-06-01T00:00:00+00:00"))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("frame", ["cold", "warm"])
def test_stats_change_during_the_capture_never_publishes_a_mixed_bundle(
        env, monkeypatch, frame):
    """AD29 / AD21 / 5.13: with the capture group's stats reads per
    statement, a relevant stats commit inside the capture (after the quota
    observations, before the capture's later stats reads) must lead to the
    build's publication being rejected: on a cold build no bundle is
    published, and on a warm one the prior frame is retained. Detects a
    build that publishes a bundle mixing the generations before and after
    the commit, the risk per-statement reads carry without the fresh
    publication validation."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    tui = sys.modules["_cctally_tui"]
    sources = sys.modules["_cctally_dashboard_sources"]
    if frame == "warm":
        reference = _build_dashboard_snapshot(env)
        assert reference.source_bundle is not None
        # Move the dispatch signature, so the next tick rebuilds.
        _commit_projection_row(env, "root-before-build")
    real_observations = sources._cached_codex_quota_observations
    commits: list = []

    def committing(*args, **kwargs):
        result = real_observations(*args, **kwargs)
        if not commits:
            _commit_projection_row(env, "root-mid-capture")
            commits.append(True)
        return result

    monkeypatch.setattr(sources, "_cached_codex_quota_observations",
                        committing)
    real_build = tui._tui_build_source_bundle
    outcomes: list = []

    def observed(*args, **kwargs):
        prior = kwargs.get("prior_bundle")
        try:
            result = real_build(*args, **kwargs)
        except Exception as exc:
            outcomes.append(("raised", prior, exc))
            raise
        outcomes.append(("returned", prior, result))
        return result

    monkeypatch.setattr(tui, "_tui_build_source_bundle", observed)
    real_withdraw = tui._tui_withdraw_codex_quota_dependency
    withdrawn: list = []

    def withdraw(bundle):
        out = real_withdraw(bundle)
        withdrawn.append((bundle, out))
        return out

    monkeypatch.setattr(tui, "_tui_withdraw_codex_quota_dependency",
                        withdraw)
    snap = _build_dashboard_snapshot(env)
    assert commits, "non-vacuity: the build ran no Codex capture group"
    assert len(outcomes) == 1, outcomes
    kind, prior, result = outcomes[0]
    if frame == "cold":
        assert prior is None
        assert kind == "raised", result
        assert snap.source_bundle is None, "a mixed bundle was published"
    else:
        assert prior is reference.source_bundle
        retained = [prior] + [out for src, out in withdrawn if src is prior]
        assert any(snap.source_bundle is r for r in retained), (
            "the build published a new bundle across a moved stats "
            "generation instead of retaining the prior frame", kind)


def test_stats_writer_waits_on_a_held_session_control(env):
    """Control for the writer-completion test above: the same write fails
    at once while a query session holds its read unit open, so a passing
    run of that test is evidence that no session was held."""
    _arm_stats_writer(env)
    access = env["access"]
    with env["core"].establish_authority("process"):
        with access.read_unit("stats") as session:
            session.conn.execute("SELECT 1 FROM t787_writer").fetchall()
            held = _stats_write_now(env)
    assert held is not None and "locked" in held
    assert _stats_write_now(env) is None


def test_stats_session_waits_out_a_live_writer_journal(env):
    """stats.db is `journal_mode=DELETE`, so every writer transaction holds
    a `-journal` until it commits. A query session waits that transient
    journal out within its deadline instead of refusing (detects `report`
    and every other CLI stats read failing with "stats.db has a -journal
    side file" whenever a hook or `record-usage` write is in flight), and
    a journal that outlives the deadline is still refused (5.4)."""
    _arm_stats_writer(env)
    access = env["access"]
    journal = pathlib.Path(f"{_path(env, 'stats')}-journal")
    opened = threading.Event()
    errors = []

    def writer():
        conn = sqlite3.connect(str(_path(env, "stats")), timeout=5,
                               isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO t787_writer VALUES (2)")
            opened.set()
            time.sleep(0.3)
            conn.execute("COMMIT")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
            opened.set()
        finally:
            conn.close()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert opened.wait(5)
    assert journal.exists()
    with env["core"].establish_authority("process"):
        with access.QueryService.session("stats", policy="command") as s:
            rows = s.conn.execute("SELECT x FROM t787_writer").fetchall()
    thread.join(5)
    assert not errors, errors
    assert rows == [(2,)]
    assert not journal.exists()

    journal.write_bytes(b"\0" * 512)
    with env["core"].establish_authority("process"):
        with pytest.raises(env["core"].StoreUnavailable) as refused:
            with access.QueryService.session("stats",
                                             policy="interactive"):
                pass
    assert refused.value.reason == "unavailable"
    assert "-journal" in str(refused.value)


def test_stats_hot_journal_after_precheck_is_unavailable(env, monkeypatch):
    """AD23: a `-journal` that turns hot after the precheck (a writer that
    died between the check and the connect) surfaces from the ``mode=ro``
    connection as SQLite's read-only rollback error. It is the precheck's
    typed refusal, never an unmapped ``DatabaseError`` (which a caller
    renders as a raw error or, in Doctor, an open failure), and the journal
    is never rolled back by the reader."""
    access, core = env["access"], env["core"]
    path = _path(env, "stats")
    journal = pathlib.Path(f"{path}-journal")
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        # synchronous=OFF writes the journal header's magic at once (a
        # syncing writer zeroes it until commit), so the captured bytes are
        # a hot journal: a crashed writer's, waiting for rollback.
        writer.execute("PRAGMA synchronous=OFF")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("CREATE TABLE t787_hot_probe (x)")
        hot = journal.read_bytes()
        writer.execute("ROLLBACK")
    finally:
        writer.close()
    assert hot and not journal.exists()
    real = access._stats_journal_cleared

    def cleared_then_hot(p, policy):
        cleared = real(p, policy)
        journal.write_bytes(hot)
        return cleared

    monkeypatch.setattr(access, "_stats_journal_cleared", cleared_then_hot)
    try:
        with pytest.raises(core.StoreUnavailable) as info:
            with access.QueryService.session("stats", policy="command"):
                pass  # pragma: no cover
        assert info.value.store == "stats"
        assert info.value.reason == "unavailable"
        assert str(info.value) == "stats.db has a -journal side file"
        assert journal.read_bytes() == hot
    finally:
        journal.unlink(missing_ok=True)


def test_codex_quota_observations_surface_a_refused_cache(env):
    """A cache.db the query session refuses is unavailability, never "no
    quota observations": the loader raises the typed refusal for its
    caller's degraded path instead of returning ``()``, which every CLI
    surface would render as an empty quota history."""
    import _cctally_quota
    os.chmod(env["app"], 0o755)
    with pytest.raises(env["core"].StoreUnavailable) as info:
        _cctally_quota.load_codex_quota_observations()
    assert info.value.reason == "unavailable"


def test_conversation_ro_guarded_never_creates_lock(env):
    """Spec 5.4 / AD22: the diagnostic conversations admission opens its
    maintenance lock ``O_RDONLY`` and never creates it; a missing lock is
    the typed `absent` refusal (detects a Doctor / explain read that creates
    the lock file and then reads a store no owner established)."""
    lock = _lock(env, "conversations")
    lock.unlink()
    before = _listing(env["root"])
    with pytest.raises(env["core"].StoreUnavailable) as info:
        with env["cache"].conversation_ro_guarded(timeout=0.0):
            pass  # pragma: no cover
    assert info.value.reason == "absent"
    assert info.value.store == "conversations"
    assert not lock.exists()
    assert _listing(env["root"]) == before


def _seed_two_claude_accounts(env):
    conn = sqlite3.connect(_path(env, "stats"))
    try:
        for order, (key, label) in enumerate((("a" * 32, "work"),
                                              ("b" * 32, "home"))):
            conn.execute(
                "INSERT INTO accounts (account_key, provider, natural_id, "
                "email, label, plan_type, label_source, first_seen_utc, "
                "last_seen_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, "claude", f"n{order}", f"{label}@example.com", label,
                 None, "user", f"2026-07-0{order + 1}T00:00:00Z",
                 "2026-07-10T00:00:00Z"))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("obligation", [
    None, "quota_projection_incomplete", "content_revision_missing",
    "legacy_migration_pending", "migration_error_sentinel"])
def test_alert_and_config_account_reads_match_base_on_obligation(
        env, obligation):
    """Item 6: `alerts`' label prefix and test-alert account and `config`'s
    account-budget normalization read stats.db without a startup phase.
    On a store carrying each stats readiness obligation they still read
    what today's guarded ``mode=ro`` open read (computed here from a raw
    read-only connection), so an obligation never degrades the label or the
    ref resolution."""
    import _cctally_account
    import _cctally_alerts
    import _cctally_config
    core, db = env["core"], env["db"]
    _seed_two_claude_accounts(env)
    if obligation == "quota_projection_incomplete":
        _write_stats(env, "INSERT OR REPLACE INTO "
                          "stats_quota_projection_state(id, incomplete) "
                          "VALUES (1, 1)")
    elif obligation == "content_revision_missing":
        conn = sqlite3.connect(_path(env, "stats"))
        for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_content_revision_%'").fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP TABLE stats_content_revision_scopes")
        conn.execute("DROP TABLE stats_content_revision")
        conn.commit()
        conn.close()
    elif obligation == "legacy_migration_pending":
        _stamp(_path(env, "stats"), core.LEGACY_STATS_HEAD)
    elif obligation == "migration_error_sentinel":
        first = db._STATS_MIGRATIONS[0].name
        pathlib.Path(core.MIGRATION_ERROR_LOG_PATH).write_text(
            f"[2026-01-01T00:00:00Z] stats.db:{first}\nboom\n\n")
    if obligation is not None:
        with env["access"].QueryService.session("stats") as s:
            assert obligation in s.readiness["primary"].obligations
    raw = sqlite3.connect(f"file:{_path(env, 'stats')}?mode=ro", uri=True)
    try:
        expected_label = _cctally_account.display_account_label(raw, "a" * 32)
    finally:
        raw.close()
    with core.establish_authority("process"):
        prefix = _cctally_alerts._alert_label_prefix(
            "weekly", "a" * 32, "claude")
        test_key = _cctally_alerts._test_alert_account_key("claude")
        refs = _cctally_config._normalize_account_budget_refs(
            {"work": 5}, "claude", "budget.accounts")
    assert prefix == f"[{expected_label}] "
    assert test_key == "a" * 32
    assert refs == {"a" * 32: 5}


@pytest.mark.parametrize("state", ["wal_header", "recovery", "behind"])
def test_alert_and_config_account_reads_match_base_on_unreadable_store(
        env, monkeypatch, state):
    """Item 6 (AD2): `alerts`' label prefix and test-alert account and
    `config`'s account-budget ref resolution on a stats.db the query layer
    cannot read but base's guarded open read through: a not-yet-converted
    WAL-header store and an interrupted rebuild (the owner opener converts or
    recovers them, as base's open did), and a schema-behind store (read
    read-only, as base's ``mode=ro`` open read it, without starting the
    epoch rebuild). Detects `alerts test` and `config set budget.accounts`
    silently dropping the account label or refusing a label ref that base
    resolved."""
    import _cctally_account
    import _cctally_alerts
    import _cctally_config
    core = env["core"]
    _seed_two_claude_accounts(env)
    path = _path(env, "stats")
    raw = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        expected_label = _cctally_account.display_account_label(raw, "a" * 32)
    finally:
        raw.close()
    if state == "wal_header":
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        assert path.read_bytes()[18:20] == b"\x02\x02"
    else:
        _seed(env, "stats", state)
    workers = []
    monkeypatch.setattr(env["store"], "defer_stats_epoch_rebuild",
                        lambda *a, **k: workers.append(a) or "started")
    seen = {}

    def rehearsal(_args):
        seen["prefix"] = _cctally_alerts._alert_label_prefix(
            "weekly", "a" * 32, "claude")
        seen["test_key"] = _cctally_alerts._test_alert_account_key("claude")
        return 0

    monkeypatch.setattr(_cctally_alerts, "_cmd_alerts_test", rehearsal)
    with core.establish_authority("process"):
        assert _cctally_alerts.cmd_alerts_test(None) == 0
        refs = _cctally_config._normalize_account_budget_refs(
            {"work": 5}, "claude", "budget.accounts")
    assert seen["prefix"] == f"[{expected_label}] "
    assert seen["test_key"] == "a" * 32
    assert refs == {"a" * 32: 5}
    assert workers == []


def test_corrupt_stats_refusal_keeps_sqlites_text(env):
    """Section 12: a stats.db whose header is not SQLite's is refused as
    `corrupt` before any connection (the AD19 header probe). Its message is
    the text today's open reported for the same file, SQLite's own "file is
    not a database", so `doctor`'s "could not open: …" and `db status`'s
    "(not readable: …)" print what they printed before, not a new
    sentence."""
    _seed(env, "stats", "corrupt")
    with pytest.raises(env["core"].StoreUnavailable) as info:
        with env["access"].QueryService.session("stats", policy="command"):
            pass  # pragma: no cover
    assert info.value.reason == "corrupt"
    assert str(info.value) == "file is not a database"


def test_stats_noncontention_expiry_message(env, monkeypatch, capsys):
    """AD18 / O10: when the owner's bounded stats maintenance wait expires
    because flock kept failing with an OS error (no holder), the error is
    still `StatsDbMaintenanceError` (exit 3) and still wakes nobody
    (`blocked_by.contended` False), but its message names the OS error
    instead of claiming maintenance is in progress (detects telling the user
    to wait for a maintenance command that does not exist while the real
    cause, an I/O failure on the lock, is hidden)."""
    store, core, db = env["store"], env["core"], env["db"]
    lock_name = str(_lock(env, "stats"))
    eio = OSError(errno.EIO, os.strerror(errno.EIO))
    real_flock = fcntl.flock

    def failing_flock(fh, op):
        if getattr(fh, "name", None) == lock_name and op & fcntl.LOCK_NB:
            raise eio
        return real_flock(fh, op)

    monkeypatch.setattr(fcntl, "flock", failing_flock)
    monkeypatch.setattr(store, "_STATS_OPEN_MAINTENANCE_WAIT_S", 0.05)
    with core.establish_authority("process"):
        with pytest.raises(db.StatsDbMaintenanceError) as info:
            store.stats_open_guarded()
    exc = info.value
    assert str(exc) == f"stats.db maintenance lock could not be acquired: {eio}"
    assert exc.blocked_by.lock == "maintenance"
    assert exc.blocked_by.contended is False

    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    capsys.readouterr()
    rc = env["ns"]["main"](["sync-week"])
    err = capsys.readouterr().err
    assert rc == 3
    assert (f"cctally: stats.db maintenance lock could not be acquired: "
            f"{eio}") in err
    assert "maintenance is in progress" not in err


#: The query layer's internal refusal details (Q-W1): none may reach a
#: human-readable surface.
_INTERNAL_DETAILS = (
    "not private", "lock is absent", "rollback-journal index", "side file",
    "is not readable (", "lock unreadable", "not a regular file",
    "changed during the open", "recovery is pending")


def _cli(env, capsys, argv):
    capsys.readouterr()
    rc = env["ns"]["main"](list(argv))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


@pytest.mark.parametrize("store, state", [
    ("stats", "wal_header"), ("stats", "missing_lock"),
    ("cache", "missing_lock"), ("conversations", "missing_lock"),
    (None, "non_private")])
def test_db_status_refusal_renders_the_existing_sentence(
        env, monkeypatch, capsys, store, state):
    """Q-W1: `db status` shows a refused store as `(not readable: <store>.db
    could not be read)`, never the query layer's internal reason (detects
    new sentences such as "stats.db header is not a rollback-journal index"
    or "the cctally data directory is not private" reaching users); the
    reason and detail appear only in `--json`, as the additive
    `openRefusal` key."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    if state == "wal_header":
        conn = sqlite3.connect(_path(env, "stats"))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
    else:
        _seed(env, store or "stats", state)
    refused = [store] if store else ["stats", "cache"]
    rc, out, err = _cli(env, capsys, ["db", "status"])
    assert rc == 0
    for name in refused:
        assert f"(not readable: {name}.db could not be read)" in out
    assert not any(d in out + err for d in _INTERNAL_DETAILS), out + err
    rc, out, _err = _cli(env, capsys, ["db", "status", "--json"])
    payload = json.loads(out)
    for name in refused:
        leg = payload["databases"][f"{name}.db"]
        assert leg["_open_error"] == f"{name}.db could not be read"
        assert leg["openRefusal"]["store"] == name
        assert leg["openRefusal"]["reason"] in ("unavailable", "absent")
        assert any(d in leg["openRefusal"]["detail"]
                   for d in _INTERNAL_DETAILS)


def test_cli_refusal_error_renders_the_existing_sentence(
        env, monkeypatch, capsys):
    """Q-W1 / AD20 revision 20: on a non-private data directory whose chmod
    fails with EIO (no access record), a CLI command's stats query still
    refuses. Its `Error:` line and the cache facade's fallback note name
    only `<store>.db could not be read` (detects "Error: the cctally data
    directory is not private")."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    core = env["core"]
    os.chmod(env["app"], 0o755)
    real = core.os.chmod
    app = os.fspath(env["app"])

    def chmod(target, mode, *a, **kw):
        if os.fspath(target) == app:
            raise OSError(errno.EIO, os.strerror(errno.EIO), app)
        return real(target, mode, *a, **kw)

    monkeypatch.setattr(core.os, "chmod", chmod)
    rc, _out, err = _cli(env, capsys, ["report"])
    assert rc == 1
    assert "Error: stats.db could not be read" in err
    rc, _out, err = _cli(env, capsys, ["daily"])
    assert "[cache] unavailable (cache.db could not be read)" in err
    assert not any(d in err for d in _INTERNAL_DETAILS), err


@pytest.mark.parametrize("store", ["stats", "cache"])
@pytest.mark.parametrize("failure", ["eio", "eintr_once"])
def test_query_admission_lock_os_error_is_typed(env, monkeypatch, store,
                                                failure):
    """I2f item 3: an `OSError` other than contention from the query
    admission's shared `flock` (an I/O error on the lock file) is the typed
    `unavailable` refusal chained to the OS error, so every surface renders
    the existing `<store>.db could not be read` sentence (detects a raw
    `OSError` traceback from a read command). An `InterruptedError` is
    retried within the deadline, as `admission_flock` retries it."""
    access, core = env["access"], env["core"]
    eio = OSError(errno.EIO, os.strerror(errno.EIO))
    real_flock = fcntl.flock
    calls = []

    def flock(fd, op):
        if isinstance(fd, int) and op == fcntl.LOCK_SH | fcntl.LOCK_NB:
            calls.append(op)
            if failure == "eio":
                raise eio
            if len(calls) == 1:
                raise InterruptedError(errno.EINTR, "interrupted")
        return real_flock(fd, op)

    monkeypatch.setattr(access.fcntl, "flock", flock)
    if failure == "eintr_once":
        with access.QueryService.session(store, policy="command") as s:
            assert s.readiness["primary"].readable
        assert len(calls) >= 2
        return
    with pytest.raises(core.StoreUnavailable) as info:
        with access.QueryService.session(store, policy="command"):
            pass  # pragma: no cover
    assert info.value.store == store
    assert info.value.reason == "unavailable"
    assert info.value.__cause__ is eio
    assert core.store_refusal_text(info.value) == f"{store}.db could not be read"


def _crash_stats_writer(env, *, hot: bool):
    """A stats writer that dies mid-transaction (``os._exit``), as a crash
    leaves it. ``hot``: its pages spilled into the main file after the
    journal header was written, so the journal is hot and the main file is
    inconsistent until SQLite rolls it back. Otherwise a ``synchronous=FULL``
    writer (the stats policy) died before its first sync: the journal's
    header is still zero, so it is not hot and SQLite leaves it in place."""
    path = _path(env, "stats")
    pragmas = ("PRAGMA synchronous=OFF", "PRAGMA cache_size=5") if hot else (
        "PRAGMA synchronous=FULL",)
    rows = 400 if hot else 1
    code = "\n".join([
        "import os, sqlite3",
        f"c = sqlite3.connect({str(path)!r}, isolation_level=None)",
        *(f"c.execute({p!r})" for p in pragmas),
        "c.execute('BEGIN IMMEDIATE')",
        "c.execute('CREATE TABLE i2f_crash (x)')",
        ("c.execute(\"WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 "
         f"FROM n WHERE i<{rows}) INSERT INTO i2f_crash SELECT "
         "randomblob(3000) FROM n\")"),
        "os._exit(0)",
    ])
    size = path.stat().st_size
    subprocess.run([sys.executable, "-c", code], check=True)
    journal = pathlib.Path(f"{path}-journal")
    assert journal.stat().st_size > 0
    assert (journal.read_bytes()[:8] != b"\0" * 8) is hot
    assert (path.stat().st_size > size) is hot


def _leave_stats_sidecar(env, state):
    path = _path(env, "stats")
    if state == "hot_journal":
        _crash_stats_writer(env, hot=True)
    elif state == "cold_journal":
        _crash_stats_writer(env, hot=False)
    elif state == "empty_wal":
        pathlib.Path(f"{path}-wal").write_bytes(b"")
    elif state == "shm_only":
        pathlib.Path(f"{path}-shm").write_bytes(b"\0" * 32768)
    else:
        raise AssertionError(state)


@pytest.mark.parametrize("state", ["hot_journal", "cold_journal",
                                   "empty_wal", "shm_only"])
def test_cli_reads_match_base_beside_crash_left_stats_sidecars(
        env, monkeypatch, capsys, state):
    """I2f item 2: a crash can leave a `-journal` beside the rollback-journal
    stats.db (hot, or with a still-zero header), and a stray zero-byte
    `-wal` or a lone `-shm` can survive the WAL era. SQLite's unix VFS
    treats the zero-byte `-wal` as absent and ignores `-shm` outside WAL
    mode, so base's read-write owner open read all four (rolling the hot
    journal back) and `report` / `project` / `weekly` printed what the
    pristine store printed (verified against base 858945466 on the runner).
    Detects those commands exiting 1 with `Error: stats.db could not be
    read`: the startup phase's owner opener settles the sidecars, and the
    query then reads (it never recovers anything itself, 5.4)."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    path = _path(env, "stats")
    for argv in (["report"], ["project"], ["weekly"]):
        expected = _cli(env, capsys, argv)
        assert expected[0] == 0, expected
        _leave_stats_sidecar(env, state)
        assert _cli(env, capsys, argv) == expected, argv
        for suffix in ("-journal", "-wal", "-shm"):
            assert not pathlib.Path(f"{path}{suffix}").exists(), suffix


_IMMEDIATE_PROBE = "\n".join([
    "import sqlite3, sys",
    "c = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)",
    "try:",
    "    c.execute('BEGIN IMMEDIATE')",
    "    print('acquired')",
    "except sqlite3.OperationalError as exc:",
    "    print('blocked', exc)",
])


def _immediate_from_another_process(path) -> str:
    return subprocess.run(
        [sys.executable, "-c", _IMMEDIATE_PROBE, str(path)],
        capture_output=True, text=True, timeout=60, check=True).stdout.strip()


@pytest.mark.parametrize("state", ["empty_wal", "shm_only"])
def test_sidecar_settlement_holds_the_exclusive_lock_throughout(
        env, monkeypatch, state):
    """#787 / #861 F2 (AD26, AD19): the stats startup phase removes inert
    sidecars only while SQLite's EXCLUSIVE lock on stats.db is held, so
    another process's `BEGIN IMMEDIATE` stays blocked until the settlement
    ends. Detects an in-process descriptor opened and closed on stats.db
    under that lock: on POSIX its close drops every fcntl lock this process
    holds on the inode, SQLite's included, and the sidecars are then
    unlinked while another process may already be writing the store."""
    access, core = env["access"], env["core"]
    path = _path(env, "stats")
    _leave_stats_sidecar(env, state)
    sidecars = {f"{path}{s}" for s in ("-journal", "-wal", "-shm")}
    seen: list = []
    real_unlink = pathlib.Path.unlink

    def unlink(self, *args, **kwargs):
        if (str(self) in sidecars and not seen and sys._getframe(1)
                .f_code.co_name == "_settle_inert_stats_sidecars"):
            seen.append(_immediate_from_another_process(path))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    with core.establish_authority("process"):
        access.StartupService.ensure("stats")
    assert seen, "non-vacuity: the settlement removed no sidecar"
    assert seen[0].startswith("blocked"), seen
    for name in sidecars:
        assert not pathlib.Path(name).exists(), name
    assert _immediate_from_another_process(path) == "acquired"


def _set_stats_wal_header(env):
    conn = sqlite3.connect(_path(env, "stats"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    assert _path(env, "stats").read_bytes()[18:20] == b"\x02\x02"


@pytest.mark.parametrize("source", ["claude", "all"])
@pytest.mark.parametrize("store, state", [
    ("cache", "missing_lock"), ("cache", "behind"), ("stats", "wal_header")])
def test_explain_matches_base_on_fixable_store_state(
        env, monkeypatch, capsys, store, state, source):
    """I2f item 1 (AD2): base's `explain` read cache.db and stats.db through
    a raw ``mode=ro`` open, so it measured a cache.db whose maintenance lock
    is missing, a schema-behind cache.db and a not-yet-converted WAL-header
    stats.db exactly as the pristine store (verified against base
    858945466 on the runner). Detects `explain` withholding every class and
    exiting 3 there: the fixable states run the owner opener first, the
    schema-behind store is read under the diagnostic policy, and no epoch
    rebuild worker is launched. Revision 31 (R30-2): `--source all` too,
    whose forked Codex provider reads the same stores; its startup runs
    once, in the parent, before the fork (a provider child converting
    stats.db beside the parent's Claude read made one half unreadable)."""
    from test_620_s2_diagnosis_sources import (_seed_claude,
                                               _seed_claude_blocks)
    core = env["core"]
    with core.establish_authority("process"):
        _seed_claude(env["ns"], models=(("claude-opus-4-20250514", 50),
                                        ("claude-haiku-4-20250514", 10)))
        _seed_claude_blocks(env["ns"])
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-08-17T00:00:00Z")
    argv = ["explain", "--window", "2026-08-10..2026-08-16",
            "--source", source]
    expected = _cli(env, capsys, argv)
    assert expected[0] == 0, expected
    if state == "wal_header":
        _set_stats_wal_header(env)
    else:
        _seed(env, store, state)
    workers = []
    monkeypatch.setattr(env["store"], "defer_stats_epoch_rebuild",
                        lambda *a, **k: workers.append(a) or "started")
    rc, out, err = _cli(env, capsys, argv)
    # The pricing warning is one-shot per process, so only the first run
    # in this process prints it.
    one_shot = "[cost] unknown model"
    assert (rc, out) == expected[:2]
    assert [ln for ln in err.splitlines() if one_shot not in ln] == [
        ln for ln in expected[2].splitlines() if one_shot not in ln]
    assert workers == []


@pytest.mark.parametrize("source", ["claude", "all"])
def test_explain_builds_in_a_query_phase_after_its_startup(
        env, monkeypatch, capsys, source):
    """#787 AD31 / AD32 (revision 31, R30-2). CLI `explain` runs its owner
    work (the startup phase that converts a WAL-header stats.db) at the
    command's `process` authority BEFORE its build, and the build reads
    only under `query` authority at the `command` policy: every store
    session the build opens, the held conversations reader included, and
    the `source=all` forked provider's handoff, so the provider child reads
    as a query and can do no owner work. Detects the build running at
    `process`, where the provider child converted stats.db beside the
    parent's Claude read and one half came back unreadable (exit 3)."""
    core, access = env["core"], env["access"]
    sources = env["ns"]["_load_sibling"]("_cctally_diagnosis_sources")
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-08-17T00:00:00Z")
    _set_stats_wal_header(env)
    events: list = []

    def _kind():
        a = core.current_authority()
        return (a.kind, a.policy) if a is not None else None

    real_ensure = access.StartupService.ensure
    monkeypatch.setattr(access.StartupService, "ensure", staticmethod(
        lambda store: (events.append(("ensure", store, _kind())),
                       real_ensure(store))[1]))
    real_build = sources.build_diagnosis

    def _build(*args, **kwargs):
        events.append(("build", None, _kind()))
        return real_build(*args, **kwargs)

    monkeypatch.setattr(sources, "build_diagnosis", _build)
    real_session = access._session

    @contextlib.contextmanager
    def _session(primary, **kwargs):
        events.append(("session", primary, _kind()))
        with real_session(primary, **kwargs) as s:
            yield s

    monkeypatch.setattr(access, "_session", _session)
    real_held = access.held_query_connection

    def _held(store, path, policy):
        events.append(("held", store, _kind()))
        return real_held(store, path, policy)

    monkeypatch.setattr(access, "held_query_connection", _held)
    real_start = sources._start_forked_provider

    def _start(args):
        events.append(("fork", None, core.authority_handoff()[:2]))
        return real_start(args)

    monkeypatch.setattr(sources, "_start_forked_provider", _start)
    rc, out, err = _cli(env, capsys, [
        "explain", "--window", "2026-08-10..2026-08-16", "--source", source,
        "--json"])
    assert rc == 0, (out, err)
    assert _path(env, "stats").read_bytes()[18:20] == b"\x01\x01"
    build = next(i for i, e in enumerate(events) if e[0] == "build")
    # The owner work: before the build, at `process`.
    assert ("ensure", "stats", ("process", None)) in events[:build]
    after = events[build + 1:]
    assert [e for e in after if e[0] == "ensure"] == []
    reads = [e for e in after if e[0] in ("session", "held")]
    assert {e[1] for e in reads} >= {"stats", "cache", "conversations"}
    assert {e[2] for e in reads} == {("query", "command")}, reads
    forks = [e for e in after if e[0] == "fork"]
    assert forks == ([("fork", None, ("query", "command"))]
                     if source == "all" else [])


def test_explain_conversations_read_refuses_a_non_private_data_dir(env):
    """Spec 5.4: a reader-created sidecar is acceptable only inside a data
    directory an owner established as private, so the `explain` reader (CLI
    and GET /api/diagnosis; `conversation_ro_guarded`, which keeps #1000
    5.10's held admission) declines with the existing sentence before any
    connection. Detects that reader opening conversations.db `mode=ro` in a
    directory no owner made private, creating `-wal`/`-shm` there."""
    from test_620_s3_generation import _scope, _visible_plan
    sources = env["ns"]["_load_sibling"]("_cctally_diagnosis_sources")

    def _read():
        bundle = sources.StoreBundle(_scope(), _visible_plan())
        try:
            conn = bundle.connection("conversations")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            bundle.close()

    _read()  # non-vacuity: the private directory's store is read
    os.chmod(env["app"], 0o755)
    before = _listing(env["root"])
    lock = os.path.realpath(_lock(env, "conversations"))
    opened = []
    real_open = os.open

    def _spy(file, flags, *args, **kwargs):
        if os.path.realpath(os.fsdecode(file)) == lock:
            opened.append(flags)
        return real_open(file, flags, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "open", _spy)
        with pytest.raises(sources.EstablishmentFailure) as info:
            _read()
    assert info.value.code == "store_unavailable"
    assert info.value.message == (
        "conversations store could not be opened read-only: "
        "conversations.db could not be read")
    assert _listing(env["root"]) == before
    # AD31: refused before the maintenance lock is even opened.
    assert opened == []


def _explain_bundle(env):
    from test_620_s3_generation import _scope, _visible_plan
    sources = env["ns"]["_load_sibling"]("_cctally_diagnosis_sources")
    return sources, sources.StoreBundle(_scope(), _visible_plan())


@pytest.mark.parametrize("statement", ["attach", "swallowed_write"])
def test_explain_conversations_reader_latches_a_forbidden_statement(
        env, statement):
    """#787 AD31 (revision 29, R28-2). The `explain` conversations reader
    keeps #1000's held admission, and its connection carries every query
    protection: the composed authorizer denies a forbidden statement (an
    ATTACH, a write) and records it in the violation latch, so the reader's
    close refuses even when the caller swallowed the denial, and the
    enclosing operation's latch refuses publication. Detects a raw `mode=ro`
    connection on a read path, which ATTACHes anything and latches nothing:
    a swallowed denial would publish."""
    core, access = env["core"], env["access"]
    sources, bundle = _explain_bundle(env)
    with core.operation_boundary("ad31-test") as outer:
        conn = bundle.connection("conversations")
        assert isinstance(conn, access._QueryConnection)
        assert conn.session_tag.startswith("held-session:conversations:")
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY
        if statement == "attach":
            uri = _path(env, "cache").resolve().as_uri() + "?mode=ro"
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                conn.execute(f"ATTACH DATABASE '{uri}' AS tcr_attached")
        else:
            try:
                conn.execute("CREATE TABLE main.tcr_ad31(x)")
            except sqlite3.DatabaseError:
                pass                    # swallowed, as a careless caller does
        assert outer.violated
        with pytest.raises(core.QueryAuthorityViolation):
            bundle.close()
    # The held admission is released on the refusing close too.
    with open(_lock(env, "conversations"), "a+") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_explain_conversations_reader_reads_at_query_authority(env):
    """#787 AD31 (revision 31, R30-2). Started at the CLI's `process`
    authority, the held conversations reader runs its reads under `query`
    authority for the connection's whole life: a capability check inside
    them refuses and latches, and the caller's `process` authority returns
    once the read unit closes. Detects a reader whose reads keep the
    caller's owner capabilities, so a forbidden owner entry reached from a
    conversations read in CLI `explain` runs unrefused."""
    core = env["core"]
    sources, bundle = _explain_bundle(env)
    with core.establish_authority("process"), \
            core.operation_boundary("ad31-authority") as outer:
        try:
            conn = bundle.connection("conversations")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            inside = core.current_authority()
            assert (inside.kind, inside.policy) == ("query", "command")
            with pytest.raises(core.QueryAuthorityViolation):
                core.require_capability("WRITE",
                                        entry="tcr_ad31_owner_entry")
            assert outer.violated
        except BaseException:
            with contextlib.suppress(core.QueryAuthorityViolation):
                bundle.close()
            raise
        with pytest.raises(core.QueryAuthorityViolation):
            bundle.close()
        assert core.current_authority().kind == "process"


@pytest.mark.parametrize("policy, failed_s, held_s", [
    # interactive: 0.5 s budget; 0.1 + 0.25 spent leaves 0.15, under the
    # 250 ms cap. command: 5 s budget and no cap; 1.0 + 3.5 leaves 0.5.
    ("interactive", 0.1, 0.25), ("command", 1.0, 3.5)])
def test_explain_conversations_admission_draws_on_the_read_unit(
        env, monkeypatch, policy, failed_s, held_s):
    """#787 AD31 (revision 29, R28-4; revision 31, R30-6). The reader's lock
    attempt, connect and readiness reads are one admission of the active
    read unit's AD29 budget, debited exactly once, a failed admission
    included; its busy timeout is min(policy cap, max(0, effective
    admission deadline - now)) instead of the fixed 4 s, so with less left
    than the 250 ms interactive cap it is exactly the remainder; and a cache
    session admitted later in the same unit sees only what is left. A
    controlled clock makes every span exactly the injected delay. Detects a
    held reader that waits up to 4 s on a busy store while the unit's budget
    says 500 ms, a double or missing charge, and a fixed busy timeout."""
    core, access = env["core"], env["access"]
    sources, bundle = _explain_bundle(env)
    clock = _controlled_clock(monkeypatch)
    total = core._QUERY_ADMISSION_BUDGET_S[policy]
    seen = []
    real_connect_ro = access._connect_ro
    fail = [True]

    def _slow_connect_ro(store, path, policy_, tag, **kwargs):
        seen.append((store, policy_, core.remaining_deadline()))
        if store == "conversations":
            clock.advance(failed_s if fail[0] else held_s)
            if fail[0]:
                fail[0] = False
                raise sqlite3.OperationalError("database is locked")
        return real_connect_ro(store, path, policy_, tag, **kwargs)

    timeouts = {}
    real_connect = sqlite3.connect

    def _connect(database, *args, **kwargs):
        timeouts[str(database).split("?")[0].rsplit("/", 1)[-1]] = (
            kwargs.get("timeout"), kwargs.get("cached_statements"))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(access, "_connect_ro", _slow_connect_ro)
    monkeypatch.setattr(sqlite3, "connect", _connect)
    # The dashboard worker's authority (the route's query(interactive)), or
    # CLI `explain`'s query phase (query(command)).
    with core.authority_scope("query", policy=policy), \
            core.operation_budget(policy) as budget:
        try:
            with pytest.raises(sources.EstablishmentFailure):
                bundle.connection("conversations")
            after_failure = budget.remaining_s
            bundle.connection("conversations")
            after_conversations = budget.remaining_s
            bundle.connection("cache")
        finally:
            bundle.close()
    assert [s[:2] for s in seen] == [("conversations", policy),
                                     ("conversations", policy),
                                     ("cache", policy)]
    # Each admission debits exactly its own span, the failed one included.
    assert abs(after_failure - (total - failed_s)) <= _CLOCK_EXACT_S
    assert abs(seen[1][2] - (total - failed_s)) <= _CLOCK_EXACT_S
    left = total - failed_s - held_s
    assert abs(after_conversations - left) <= _CLOCK_EXACT_S
    # The cache admission's deadline is the remainder, not a fresh budget.
    assert abs(seen[2][2] - left) <= _CLOCK_EXACT_S
    busy, uncached = timeouts["conversations.db"]
    # Below the interactive cap, and under the uncapped command policy, the
    # busy timeout is exactly what the admission has left.
    assert left < access._INTERACTIVE_BUSY_CAP_S or policy == "command"
    assert abs(busy - left) <= _CLOCK_EXACT_S
    assert uncached == 0


# ── N2: a snapshot build reads at the interactive policy (spec 5.3) ───────


def _record_session_policies(env, monkeypatch):
    """Every `_session` opened, as (store, policy, authority kind, authority
    policy) at the moment it opens."""
    access, core = env["access"], env["core"]
    real = access._session
    seen: list = []

    @contextlib.contextmanager
    def observed(primary, **kwargs):
        a = core.current_authority()
        seen.append((primary, kwargs.get("policy"),
                     a.kind if a else None, a.policy if a else None))
        with real(primary, **kwargs) as s:
            yield s

    monkeypatch.setattr(access, "_session", observed)
    return seen


_N2_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)


def test_snapshot_build_reads_at_the_interactive_policy(env, monkeypatch):
    """5.1 / 5.3: a snapshot build's narrowed reads run under
    `query(interactive)`, so every session they open, the per-statement stats
    reader's included, admits at the interactive policy: 500 ms, three
    non-blocking lock attempts, a 250 ms busy cap. Detects a reader that
    defaults to the command policy whenever any authority is established
    (5 s, polling admission, an uncapped busy timeout on a dashboard or TUI
    build), which held a build about 5 s per refused stats read."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    core = env["core"]
    seen = _record_session_policies(env, monkeypatch)
    with core.establish_authority("process"):
        with core.operation_boundary("snapshot-build"):
            env["ns"]["_tui_build_snapshot"](
                now_utc=_N2_NOW, skip_sync=True, precompute_envelope=True,
                runtime_bind="127.0.0.1")
    narrowed = [s for s in seen if s[2] == "query"]
    # Non-vacuity: the build read stats per statement inside the narrowing.
    assert sum(1 for s in narrowed if s[0] == "stats") >= 10, seen
    assert {(s[2], s[3]) for s in narrowed} == {("query", "interactive")}
    assert {s[1] for s in narrowed} == {"interactive"}, sorted(
        {s for s in narrowed if s[1] != "interactive"})


def test_build_under_held_stats_maintenance_keeps_the_prior_frame_promptly(
        env, monkeypatch):
    """5.3 / #932: stats maintenance taken after a build's owner phase (a
    rebuild worker starting between the owner open and the narrowed reads)
    refuses the narrowed stats reads fast, as `maintenance`, and the build
    publishes nothing of its own: the publisher keeps the prior frame's
    data, as base's build did when its owner stats open met maintenance.
    Detects a build that waits about 5 s per refused stats read (the command
    policy), and one that records each refused read as a leg failure and
    publishes a frame mixing fresh cache panels with emptied stats ones."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    ns, core = env["ns"], env["core"]
    tui = sys.modules["_cctally_tui"]

    class Hub:
        def __init__(self):
            self.published = []

        def publish(self, snapshot):
            self.published.append(snapshot)

    with core.establish_authority("process"):
        prior = ns["_tui_build_snapshot"](
            now_utc=_N2_NOW, skip_sync=True, precompute_envelope=True,
            runtime_bind="127.0.0.1")
    ref = ns["_SnapshotRef"](prior)
    hub = Hub()
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=_N2_NOW, display_tz_pref_override=None,
        runtime_bind="127.0.0.1")
    holder = open(_lock(env, "stats"), "r")
    real_owner = tui._tui_build_owner_phase
    held: list = []

    def owner_then_maintenance(*, skip_sync):
        out = real_owner(skip_sync=skip_sync)
        fcntl.flock(holder, fcntl.LOCK_EX)
        held.append(True)
        return out

    monkeypatch.setattr(tui, "_tui_build_owner_phase",
                        owner_then_maintenance)
    outcome: dict = {}

    def run():
        try:
            with core.establish_authority("process"):
                with core.operation_boundary("snapshot-build"):
                    started = time.monotonic()
                    locked(skip_sync=True)
                    outcome["elapsed"] = time.monotonic() - started
        except BaseException as exc:  # noqa: BLE001 — handed back
            outcome["raised"] = exc

    t = threading.Thread(target=run)
    t.start()
    try:
        t.join(timeout=240)
    finally:
        holder.close()
        t.join(timeout=60)
    assert not t.is_alive()
    assert held, "non-vacuity: maintenance was never held during the build"
    assert "raised" not in outcome, outcome
    assert outcome["elapsed"] < 2.5, outcome
    published = hub.published[-1]
    assert "maintenance" in (published.last_sync_error or ""), (
        published.last_sync_error)
    # The prior frame's data, not a rebuilt (mixed) one.
    for field in ("current_week", "forecast", "trend", "sessions",
                  "weekly_periods", "monthly_periods", "blocks_panel",
                  "daily_panel", "doctor_payload"):
        assert getattr(published, field) is getattr(prior, field), field


# ── 5.11: an owner-resolved cache state keeps the last coherent frame ──────

_KEEP_NOW = dt.datetime(2026, 6, 1, 12, tzinfo=dt.timezone.utc)
#: The frame fields a dashboard or TUI build derives from cache.db.
_CACHE_PANELS = ("current_week", "sessions", "daily_panel", "weekly_periods",
                 "monthly_periods", "blocks_panel")
_DASHBOARD_CACHE_PANELS = _CACHE_PANELS + (
    "projects_envelope", "cache_report", "source_bundle")


def _write_claude_turns(env, session_id, stamps):
    """Priced Claude turns in one transcript the sync ingest reads."""
    # Under the fake HOME `redirect_paths` pins, which the ingest walks.
    transcript = (pathlib.Path(env["root"]) / ".claude" / "projects"
                  / "-work-alpha" / f"{session_id}.jsonl")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with transcript.open("a") as fh:
        for n, stamp in enumerate(stamps):
            fh.write(json.dumps({
                "type": "assistant", "sessionId": session_id,
                "timestamp": stamp, "requestId": f"req_{session_id}_{n}",
                "cwd": "/work/alpha",
                "message": {
                    "id": f"msg_{session_id}_{n}", "model": "claude-opus-4-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 1000, "output_tokens": 500,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
            }) + "\n")


def _dashboard_publisher(env, monkeypatch):
    """A dashboard publisher over its sync-mode seed frame, as
    `cmd_dashboard` wires it; returns ``tick(skip_sync=...)`` -> the frame it
    published."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    ns, core = env["ns"], env["core"]
    tui = sys.modules["_cctally_tui"]

    class Hub:
        def __init__(self):
            self.published = []

        def publish(self, snapshot):
            self.published.append(snapshot)

    seed = tui._tui_empty_snapshot(_KEEP_NOW)
    ref = ns["_SnapshotRef"](
        __import__("dataclasses").replace(seed, hydrating=True))
    hub = Hub()
    lockeds: dict = {}

    def tick(*, skip_sync, now=_KEEP_NOW):
        # One publisher per pinned clock, all over the same frame and hub, so
        # a later tick can run at a later instant.
        if now not in lockeds:
            lockeds[now] = ns["_make_run_sync_now_locked"](
                ref=ref, hub=hub, pinned_now=now,
                display_tz_pref_override=None, runtime_bind="127.0.0.1")
        locked = lockeds[now]
        before = len(hub.published)
        with core.establish_authority("process"):
            with core.operation_boundary("snapshot-build"):
                locked(skip_sync=skip_sync)
        assert len(hub.published) > before, "the tick published nothing"
        return hub.published[-1]

    return tick


def _hear_owner_attention(env, monkeypatch):
    core = env["core"]
    heard: list = []
    monkeypatch.setattr(core, "_OWNER_ATTENTION_COALESCE_S", 0.0)
    monkeypatch.setattr(core, "_OWNER_ATTENTION_CALLBACKS", [
        lambda store, r: heard.append((store, r.readable, r.reason))])
    return heard


def _assert_coherent(snap):
    """Non-vacuity: the frame holds the transcript's cache-derived data."""
    assert snap.last_sync_error is None, snap.last_sync_error
    assert snap.sessions, "the build read no sessions"
    assert any(row.cost_usd > 0 for row in snap.daily_panel), snap.daily_panel
    assert snap.projects_envelope is not None
    assert snap.cache_report is not None


def _seed_current_week(env):
    """A Claude usage snapshot whose subscription week holds `_KEEP_NOW`,
    so the dashboard builds its current-week panels (Blocks among them)."""
    import _cctally_journal as journal
    import _lib_journal as wire
    resets_at = int(dt.datetime(2026, 6, 3, tzinfo=dt.timezone.utc)
                    .timestamp())
    journal.append_record(wire.make_obs(
        at="2026-05-31T11:00:00Z", src="statusline", provider="claude",
        payload={"weekly_percent": 10.0, "resets_at": resets_at,
                 "source": "statusline",
                 "captured_at": "2026-05-31T11:00:00Z"}))
    journal.run_stats_ingest(mode="authoritative")


def _without_zone_text(field, value):
    """``value`` with the zone-formatted text a kept frame redraws (R32-V3)
    blanked, for comparing the panels' data."""
    if field != "blocks_panel":
        return value
    import dataclasses
    return [dataclasses.replace(r, label="") for r in value]


def _refuse_cache_as_pending(env, monkeypatch):
    """Refuse every non-diagnostic cache session as `recovery_pending`, a
    state the owner resolves (a stale repair marker, a pending quarantine, a
    fence), without the owner open the sync tick runs recovering it."""
    access = env["access"]
    real = access._precheck

    def refuse(store, policy):
        if store == "cache" and policy != "diagnostic":
            raise access._unavailable("cache", "recovery_pending",
                                      "cache.db recovery is pending")
        return real(store, policy)

    monkeypatch.setattr(access, "_precheck", refuse)


def _record_cache_connections(env, monkeypatch):
    """Every connection to cache.db (or ATTACH of it) made from here on, by
    the query layer or by a raw `sqlite3.connect`, as a list of the calls."""
    access = env["access"]
    name = _path(env, "cache").name
    opened: list = []
    real_uri, real_connect_ro = access._ro_uri, access._connect_ro
    real_connect = sqlite3.connect

    def _uri(path):
        if pathlib.Path(path).name == name:
            opened.append(("ro_uri", str(path)))
        return real_uri(path)

    def _connect_ro(store, path, *a, **k):
        if pathlib.Path(path).name == name:
            opened.append(("connect_ro", str(path)))
        return real_connect_ro(store, path, *a, **k)

    def _connect(database, *a, **k):
        if name in str(database):
            opened.append(("connect", str(database)))
        conn = real_connect(database, *a, **k)
        conn.set_trace_callback(
            lambda sql: opened.append(("attach", sql))
            if "ATTACH" in sql.upper() and name in sql else None)
        return conn

    monkeypatch.setattr(access, "_ro_uri", _uri)
    monkeypatch.setattr(access, "_connect_ro", _connect_ro)
    monkeypatch.setattr(sqlite3, "connect", _connect)
    return opened


@pytest.mark.parametrize("skip_sync", [False, True],
                         ids=["ingesting", "settings"])
def test_live_cache_repair_reads_the_transcripts_like_main(
        env, monkeypatch, skip_sync):
    """R32-1 (#787 / #861 revision 32, browser finding B1): a live cache
    repair claimed after the dashboard is up refuses the tick's cache reads
    as `maintenance`, as `main`'s owner open did, so the facades take their
    direct-JSONL fallback and the frame shows the transcripts' newer data on
    the ingesting tick and on the no-ingest rebuild POST /api/settings and
    `--no-sync` run. Detects the regression the browser pass found: a
    dashboard started or running during a live repair showed "—" and empty
    or frozen panels, because the query reported the repair as an
    owner-resolved state and the kept-frame path was taken.

    R35-1 (revision 35): the projects leg, which only cache.db can answer,
    carries the previous published frame's Projects envelope forward, with
    no connection to cache.db. Detects the Projects panel of a running
    dashboard going to "Projects data unavailable — restart the dashboard."
    for a repair that finishes by itself, and any read of cache.db while the
    repair holds its marker (N2)."""
    envelope = sys.modules["_cctally_dashboard_envelope"]
    _write_claude_turns(env, "live-a", ("2026-05-31T10:00:00.000Z",))
    tick = _dashboard_publisher(env, monkeypatch)
    first = tick(skip_sync=False)
    _assert_coherent(first)
    _write_claude_turns(env, "live-b", ("2026-05-31T11:00:00.000Z",))
    heard = _hear_owner_attention(env, monkeypatch)
    db, cache_path = env["db"], _path(env, "cache")
    claim, why = db._claim_repair_marker(cache_path)
    assert claim is not None, why
    try:
        with monkeypatch.context() as patched:
            opened = _record_cache_connections(env, patched)
            held = tick(skip_sync=skip_sync)
    finally:
        db._release_repair_marker(cache_path, claim)
    assert "recovery is pending" not in (held.last_sync_error or ""), (
        held.last_sync_error)
    ids = {getattr(row, "session_id", None) for row in held.sessions}
    assert held.sessions != first.sessions and "live-b" in str(ids), (
        "the live repair kept the last frame instead of reading the "
        "transcripts", ids)
    assert ("cache", False, "recovery_pending") not in heard, heard
    assert held.projects_envelope is not None, (
        "the running dashboard lost its Projects data", held.last_sync_error)
    assert held.projects_envelope == first.projects_envelope
    assert opened == [], opened
    published = envelope.snapshot_to_envelope(held, now_utc=_KEEP_NOW)
    assert published["projects"] == envelope.snapshot_to_envelope(
        first, now_utc=_KEEP_NOW)["projects"]
    assert published["sync_failure"]["kind"] == "maintenance_active", (
        published["sync_failure"])


@pytest.mark.parametrize("change", ["week_rollover", "zone"])
def test_a_live_cache_repair_carries_no_projects_across_a_period_or_zone(
        env, monkeypatch, change):
    """F3 (R35-1's guards): the Projects carry-forward during a live cache
    repair reuses the last frame's envelope only on the idle path's terms,
    so a subscription week that rolled over, or a display zone (config) that
    changed, since that frame carries nothing: the panel then shows its
    loading skeleton under `maintenance_active` rather than the previous
    week's projects or rows bucketed in the old zone. Detects the
    carry-forward republishing a stale week's or zone's Projects envelope."""
    envelope = sys.modules["_cctally_dashboard_envelope"]
    config = sys.modules["_cctally_config"]
    _seed_current_week(env)
    _write_claude_turns(env, "live-a", ("2026-05-31T10:00:00.000Z",))
    tick = _dashboard_publisher(env, monkeypatch)
    first = tick(skip_sync=False)
    _assert_coherent(first)
    week_end = first.current_week.week_end_at
    assert week_end is not None and week_end > _KEEP_NOW, "premise"
    now = _KEEP_NOW
    if change == "week_rollover":
        now = week_end + dt.timedelta(hours=1)
    else:
        # Same calendar day in both zones, so only the zone changes.
        config.save_config({**config.load_config(),
                            "display": {"tz": "Asia/Tokyo"}})
    db, cache_path = env["db"], _path(env, "cache")
    claim, why = db._claim_repair_marker(cache_path)
    assert claim is not None, why
    try:
        with monkeypatch.context() as patched:
            opened = _record_cache_connections(env, patched)
            held = tick(skip_sync=True, now=now)
    finally:
        db._release_repair_marker(cache_path, claim)
    assert held.projects_envelope is None, change
    assert opened == [], opened
    published = envelope.snapshot_to_envelope(held, now_utc=now)
    assert published["projects"] is None
    assert published["sync_failure"]["kind"] == "maintenance_active", (
        published["sync_failure"])


@pytest.mark.parametrize("skip_sync", [False, True],
                         ids=["ingesting", "no-sync"])
def test_a_dashboard_started_during_a_live_cache_repair_has_no_projects(
        env, monkeypatch, skip_sync):
    """R35-1 (revision 35): a dashboard whose first build runs during a live
    cache repair has no earlier Projects envelope to carry, so the leg stays
    `None` (only cache.db can answer it, and N2 forbids reading it), while
    the published sync status names the repair, the signal the Projects
    panel reads to show its loading skeleton instead of "restart the
    dashboard". Detects a carry-forward that invents Projects data from
    nothing, a cache.db read during the repair, and a frame that loses the
    repair signal the panel depends on."""
    envelope = sys.modules["_cctally_dashboard_envelope"]
    _write_claude_turns(env, "live-a", ("2026-05-31T10:00:00.000Z",))
    tick = _dashboard_publisher(env, monkeypatch)
    db, cache_path = env["db"], _path(env, "cache")
    claim, why = db._claim_repair_marker(cache_path)
    assert claim is not None, why
    try:
        with monkeypatch.context() as patched:
            opened = _record_cache_connections(env, patched)
            held = tick(skip_sync=skip_sync)
    finally:
        db._release_repair_marker(cache_path, claim)
    assert held.sessions, "non-vacuity: the fallback read the transcript"
    assert held.projects_envelope is None
    assert opened == [], opened
    published = envelope.snapshot_to_envelope(held, now_utc=_KEEP_NOW)
    assert published["projects"] is None
    assert published["sync_failure"]["kind"] == "maintenance_active", (
        published["sync_failure"])


@pytest.mark.parametrize("skip_sync", [False, True],
                         ids=["ingesting", "settings"])
def test_owner_resolved_cache_refusal_keeps_the_last_coherent_dashboard_frame(
        env, monkeypatch, skip_sync):
    """5.11 (#787 / #861 Task C acceptance P2): a cache refusal the owner
    resolves (`recovery_pending`) on the ingesting tick and on the no-ingest
    rebuild POST /api/settings and `--no-sync` run publishes the last
    coherent frame's cache-derived panels, marks it pending through the
    existing indicators (the sync-failure chip reads "cache repair in
    progress", the sync age is not refreshed), tells the owner, and rebuilds
    once the refusal ends. Detects every cache panel published empty ("No
    sessions yet.", "Projects data unavailable") while the owner resolves the
    store. Also detects a kept frame that republishes the settings it was
    built under (a `display.tz` changed meanwhile reverts in the browser on
    the next frame), and, on the no-ingest path, a chip that tells the user
    to rebuild the cache ("cache recovery needed", `cctally cache-sync
    --rebuild`) while the owner resolves the pending recovery itself. And
    (R32-V3, revision 33) a kept frame whose Blocks labels stay in the zone
    it was built under after `display.tz` changed: the rows carry their UTC
    `start_at`, so the label is redrawn in the new zone with no store
    read."""
    from zoneinfo import ZoneInfo
    envelope = sys.modules["_cctally_dashboard_envelope"]
    config = sys.modules["_cctally_config"]
    _seed_current_week(env)
    _write_claude_turns(env, "keep-787", ("2026-05-31T10:00:00.000Z",
                                          "2026-05-31T10:20:00.000Z"))
    tick = _dashboard_publisher(env, monkeypatch)
    first = tick(skip_sync=False)
    _assert_coherent(first)
    assert first.blocks_panel, "premise: the first build has Blocks rows"
    assert envelope.snapshot_to_envelope(
        first, now_utc=_KEEP_NOW)["display"]["tz"] != "Asia/Tokyo"
    # The user changes a setting while the refusal lasts (POST
    # /api/settings writes config.json, then rebuilds).
    config.save_config({**config.load_config(),
                        "display": {"tz": "Asia/Tokyo"}})
    heard = _hear_owner_attention(env, monkeypatch)
    with monkeypatch.context() as patched:
        _refuse_cache_as_pending(env, patched)
        held = tick(skip_sync=skip_sync)
    for field in _DASHBOARD_CACHE_PANELS:
        assert (_without_zone_text(field, getattr(held, field))
                == _without_zone_text(field, getattr(first, field))), field
    ns = env["ns"]
    tokyo = ZoneInfo("Asia/Tokyo")
    expected = [ns["format_display_dt"](
        ns["_round_to_ten_minutes"](dt.datetime.fromisoformat(r.start_at)),
        tokyo, fmt="%H:%M %b %d", suffix=True) for r in held.blocks_panel]
    assert [r.label for r in held.blocks_panel] == expected, (
        "the kept frame kept its Blocks labels in the old zone")
    assert expected != [r.label for r in first.blocks_panel], (
        "non-vacuity: the zone change moves the labels")
    assert "cache.db recovery is pending" in (held.last_sync_error or "")
    chip = envelope._sync_failure_envelope(held.last_sync_error,
                                           held.sync_failures)
    assert chip is not None and chip["kind"] == "maintenance_active", chip
    if not skip_sync:
        assert held.last_sync_at == first.last_sync_at
    assert not held.hydrating
    assert ("cache", False, "recovery_pending") in heard, heard
    assert envelope.snapshot_to_envelope(
        held, now_utc=_KEEP_NOW)["display"]["tz"] == "Asia/Tokyo"
    released = tick(skip_sync=False)
    assert released.last_sync_error is None, released.last_sync_error
    for field in ("sessions", "daily_panel"):
        assert getattr(released, field) == getattr(first, field), field


@pytest.mark.parametrize(("reason", "prior_built", "kept"), [
    ("recovery_pending", True, True),
    ("schema_behind", True, True),
    ("schema_ahead", True, True),
    ("absent", True, True),
    # A reason the facade raised keeps its exception path (the direct-JSONL
    # fallback reads the newer transcript), never the last frame.
    ("maintenance", True, False),
    # The very first build has no coherent frame to keep: it records its
    # failed legs, as before.
    ("recovery_pending", False, False),
])
def test_owner_resolved_cache_refusal_keeps_the_last_coherent_frame(
        env, monkeypatch, reason, prior_built, kept):
    """5.11: a reason the full opener resolved inline keeps the last coherent
    frame, marks it pending and tells the owner; a reason the facade raised,
    and a refusal on the very first build, keep their existing paths. The
    transcript grows between the two builds, so a kept frame is the only
    one still showing the first build's sessions. Detects a classifier that
    keeps a frame for a facade-raised reason (a dashboard frozen on old data
    while the direct-JSONL fallback could serve), one that misses an
    owner-resolved reason (empty panels), and a first build that raises or
    keeps the empty seed instead of recording its legs."""
    access = env["access"]
    _write_claude_turns(env, "keep-a", ("2026-05-31T10:00:00.000Z",))
    tick = _dashboard_publisher(env, monkeypatch)
    real = access._precheck

    def refuse(store, policy):
        if store == "cache" and policy != "diagnostic":
            raise access._unavailable(
                "cache", reason, f"cache.db is not readable ({reason})")
        return real(store, policy)

    first = None
    if prior_built:
        first = tick(skip_sync=False)
        _assert_coherent(first)
        _write_claude_turns(env, "keep-b", ("2026-05-31T11:00:00.000Z",))
    heard = _hear_owner_attention(env, monkeypatch)
    monkeypatch.setattr(access, "_precheck", refuse)
    published = tick(skip_sync=not (first is None))
    refusal = f"cache.db is not readable ({reason})"
    assert refusal in (published.last_sync_error or ""), (
        published.last_sync_error)
    if kept:
        for field in _DASHBOARD_CACHE_PANELS:
            assert getattr(published, field) == getattr(first, field), field
        assert ("cache", False, reason) in heard, heard
    elif first is not None:
        assert published.sessions != first.sessions
    else:
        assert published.sessions == [], published.sessions


@pytest.mark.parametrize("state", ["pending", "live_repair"])
def test_tui_sync_thread_keeps_the_last_coherent_frame_on_a_pending_cache(
        env, monkeypatch, state):
    """5.11 / 5.12 "TUI sync thread": the TUI's rebuild under a cache refusal
    the owner resolves keeps the last coherent frame's panels, marks it (the
    error banner, the sync age kept) and tells the owner, then rebuilds once
    the refusal ends. Detects the dashboard defect's TUI twin: a rebuild that
    replaces every cache-derived panel with an empty one while the owner
    resolves the store; and, with the phase trace armed, a kept build that
    leaves its "snapshot" phase open, so the thread's next traced phases
    nest under a build that already ended. Under a live repair (R32-1) the
    rebuild instead reads the transcripts, as `main`'s facades did after the
    owner open reported maintenance: a kept frame there hides newer data."""
    import _lib_perf
    ns, core = env["ns"], env["core"]
    tui = sys.modules["_cctally_tui"]
    monkeypatch.setattr(_lib_perf, "_ENABLED", True)
    _write_claude_turns(env, "keep-tui", ("2026-05-31T10:00:00.000Z",))
    ref = ns["_SnapshotRef"](tui._tui_empty_snapshot(_KEEP_NOW))
    thread = tui._TuiSyncThread(ref, 60.0, skip_sync=False, now_utc=_KEEP_NOW)

    def rebuild():
        with core.establish_authority("process"):
            with core.operation_boundary("snapshot-build"):
                thread._rebuild_once()
        return ref.get()

    first = rebuild()
    assert first.last_sync_error is None, first.last_sync_error
    assert first.sessions, "non-vacuity: the build read no sessions"
    heard = _hear_owner_attention(env, monkeypatch)
    if state == "live_repair":
        _write_claude_turns(env, "keep-tui-b", ("2026-05-31T11:00:00.000Z",))
        db, cache_path = env["db"], _path(env, "cache")
        claim, why = db._claim_repair_marker(cache_path)
        assert claim is not None, why
        try:
            held = rebuild()
        finally:
            db._release_repair_marker(cache_path, claim)
        assert "recovery is pending" not in (held.last_sync_error or "")
        assert held.sessions != first.sessions, (
            "the live repair kept the last frame instead of reading the "
            "transcripts")
        assert ("cache", False, "recovery_pending") not in heard, heard
        assert not _lib_perf.in_phase(), "the build left a phase open"
        return
    with monkeypatch.context() as patched:
        _refuse_cache_as_pending(env, patched)
        held = rebuild()
    for field in _CACHE_PANELS:
        assert getattr(held, field) == getattr(first, field), field
    assert "cache.db recovery is pending" in (held.last_sync_error or "")
    assert held.last_sync_at == first.last_sync_at
    assert ("cache", False, "recovery_pending") in heard, heard
    assert not _lib_perf.in_phase(), "the kept build left a phase open"
    released = rebuild()
    assert released.last_sync_error is None, released.last_sync_error
    assert released.sessions == first.sessions


def _refuse_monthly_leg(env, monkeypatch):
    """Refuse only the monthly panel's cache read, as a query session refuses
    a store its owner resolves; every other leg reads normally."""
    access = env["access"]

    def refused(*_a, **_k):
        raise access._unavailable("cache", "recovery_pending",
                                  "cache.db recovery is pending")

    monkeypatch.setattr(sys.modules["cctally"],
                        "_dashboard_build_monthly_periods", refused)


def test_a_tui_launched_under_a_cache_repair_does_not_keep_its_launch_frame(
        env, monkeypatch):
    """5.11: only a build that completed without a refused or failed store
    read is the last coherent frame. `cctally tui` launched while the owner
    resolves a refused cache.db (a pending recovery) builds a launch frame
    whose cache legs were refused (no sessions), and that build stamps
    `last_sync_at` like any other; treating it as coherent made every later
    rebuild that met a refusal republish it, so the sessions the cache holds
    once the recovery ends never appeared while any leg stayed refused. Detects a coherence
    test that accepts any completed build as the frame to keep."""
    ns, core = env["ns"], env["core"]
    tui = sys.modules["_cctally_tui"]
    _write_claude_turns(env, "launch-a", ("2026-05-31T10:00:00.000Z",))
    with monkeypatch.context() as patched:
        _refuse_cache_as_pending(env, patched)
        # As `cmd_tui` builds its initial frame, then hands it to the sync
        # thread.
        with core.establish_authority("process"):
            with core.operation_boundary("snapshot-build"):
                launched = tui._tui_build_snapshot(now_utc=_KEEP_NOW,
                                                   skip_sync=True)
    assert launched.sessions == [], "premise: the launch build was refused"
    assert "cache.db recovery is pending" in (launched.last_sync_error or "")
    ref = ns["_SnapshotRef"](launched)
    thread = tui._TuiSyncThread(ref, 60.0, skip_sync=False, now_utc=_KEEP_NOW)
    _refuse_monthly_leg(env, monkeypatch)
    with core.establish_authority("process"):
        with core.operation_boundary("snapshot-build"):
            thread._rebuild_once()
    published = ref.get()
    assert published.sessions, (
        "the launch frame was kept as coherent", published.last_sync_error)


def test_a_dashboard_launch_build_with_a_refused_leg_is_not_kept(
        env, monkeypatch):
    """5.11, the dashboard twin: a launch build (the first tick over the
    seed) that recorded a refused store leg still carries a real source
    bundle when the refusal spares the bundle's own reads, and treating it as
    coherent made every later tick that met the refusal republish it, so the
    sessions a later transcript adds never appeared. The transcript grows
    between the ticks, so only a kept launch frame still lacks them. Detects
    a coherence test that accepts any frame with a real source bundle."""
    tui = sys.modules["_cctally_tui"]
    _write_claude_turns(env, "launch-a", ("2026-05-31T10:00:00.000Z",))
    tick = _dashboard_publisher(env, monkeypatch)
    _refuse_monthly_leg(env, monkeypatch)
    launched = tick(skip_sync=False)
    assert launched.sessions, "non-vacuity: the launch build read sessions"
    assert (launched.source_bundle is not None
            and not tui._tui_is_hydrating_source_bundle(
                launched.source_bundle)), (
        "premise: the launch frame carries a real source bundle")
    assert "cache.db recovery is pending" in (launched.last_sync_error or "")
    _write_claude_turns(env, "launch-b", ("2026-05-31T11:00:00.000Z",))
    published = tick(skip_sync=False)
    assert published.sessions != launched.sessions, (
        "the launch frame was kept as coherent", published.last_sync_error)


def test_a_refusal_a_leg_records_inside_its_own_try_keeps_the_frame(
        env, monkeypatch):
    """5.11: the dispatch read records its content revision's refusal from
    inside the leg's `try`, whose generic handler records whatever the leg
    raised. The build's pending signal (`_TuiStoreRefusalPending`) raised
    there was caught and recorded as an ordinary "dispatch-signature"
    failure, so the build went on and published a frame the refused store
    could not certify instead of keeping the last coherent one. The
    transcript grows between the builds, so only a kept frame still shows
    the first build's sessions."""
    access = env["access"]
    tui = sys.modules["_cctally_tui"]
    _write_claude_turns(env, "leg-a", ("2026-05-31T10:00:00.000Z",))
    tick = _dashboard_publisher(env, monkeypatch)
    first = tick(skip_sync=False)
    _assert_coherent(first)
    _write_claude_turns(env, "leg-b", ("2026-05-31T11:00:00.000Z",))
    real = tui._tui_compute_dispatch_signature

    def refused_revision(conn, **kwargs):
        out = real(conn, **kwargs)
        sink = kwargs.get("revision_sink")
        if sink is not None:
            sink[:] = [(None, access._unavailable(
                "cache", "recovery_pending", "cache.db recovery is pending"))]
        return out

    monkeypatch.setattr(tui, "_tui_compute_dispatch_signature",
                        refused_revision)
    published = tick(skip_sync=False)
    assert "cache.db recovery is pending" in (published.last_sync_error or "")
    for field in _DASHBOARD_CACHE_PANELS:
        assert getattr(published, field) == getattr(first, field), field


def _cache_user_version(cache_path) -> int:
    conn = sqlite3.connect(cache_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_no_sync_owner_heals_a_cache_that_went_version_ahead(
        env, monkeypatch):
    """5.11 / AD12: a version-ahead cache.db is a state today's full opener
    resolved inline (the owner cache open trims it back to the known head), so
    a query refusal of it is owner work, and under `--no-sync` the obligation
    thread heals it. Detects an owner-attention filter that treats a
    version-ahead cache as nobody's work: every detail route then answered 503
    until a manual rebuild, where base's request opener healed the store at
    once."""
    core, access = env["core"], env["access"]
    dash = sys.modules["_cctally_dashboard"]
    _write_claude_turns(env, "ahead-a", ("2026-05-31T10:00:00.000Z",))
    tick = _dashboard_publisher(env, monkeypatch)
    _assert_coherent(tick(skip_sync=False))
    attention = threading.Event()
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_EVENT", attention)
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_NO_SYNC", True)
    monkeypatch.setattr(core, "_OWNER_ATTENTION_COALESCE_S", 0.0)
    monkeypatch.setattr(core, "_OWNER_ATTENTION_CALLBACKS",
                        [dash._dashboard_owner_attention])
    passes: list = []

    def drain():
        passes.append(time.monotonic())
        dash._dashboard_obligation_drain()

    stop = threading.Event()
    thread = threading.Thread(target=dash._owner_thread_target(
        lambda: dash._dashboard_obligation_loop(
            stop=stop, attention=attention, interval=0.05, drain=drain,
            outstanding=lambda: dash._obligation_outstanding(
                dash._obligation_readiness))), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not passes and time.monotonic() < deadline:
            time.sleep(0.02)
        assert passes, "the obligation thread never ran its startup pass"
        cache_path = _path(env, "cache")
        head = _head(env, "cache")
        _stamp(cache_path, head + 1)
        with pytest.raises(core.StoreUnavailable) as refused:
            with core.establish_authority("process"):
                with access.QueryService.session("cache",
                                                 policy="interactive"):
                    pass
        assert refused.value.reason == "schema_ahead"
        deadline = time.monotonic() + 8
        while (_cache_user_version(cache_path) != head
               and time.monotonic() < deadline):
            time.sleep(0.05)
        assert _cache_user_version(cache_path) == head, (
            "the --no-sync owner left a version-ahead cache.db unhealed")
    finally:
        stop.set()
        attention.set()
        thread.join(5)


def _start_no_sync_owner(env, monkeypatch, *, drain=None, interval=0.05):
    """The `--no-sync` obligation thread (AD12) as the dashboard wires it,
    on a short interval: the real loop, its owner opens and its
    outstanding-work probe. Returns the pass times, the attention event and
    a stop function."""
    core = env["core"]
    dash = sys.modules["_cctally_dashboard"]
    attention = threading.Event()
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_EVENT", attention)
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_NO_SYNC", True)
    monkeypatch.setattr(core, "_OWNER_ATTENTION_CALLBACKS",
                        [dash._dashboard_owner_attention])
    passes: list = []
    marker_passes: list = []

    def counted():
        passes.append(time.monotonic())
        (drain or dash._dashboard_obligation_drain)()

    marker_drain = dash._make_dashboard_marker_drain()

    def counted_marker():
        marker_passes.append(time.monotonic())
        marker_drain()

    stop = threading.Event()
    thread = threading.Thread(target=dash._owner_thread_target(
        lambda: dash._dashboard_obligation_loop(
            stop=stop, attention=attention, interval=interval,
            drain=counted,
            outstanding=lambda: dash._obligation_outstanding(
                dash._obligation_readiness),
            marker_present=dash._cache_repair_marker_present,
            marker_drain=counted_marker)),
        daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not passes and time.monotonic() < deadline:
        time.sleep(0.02)
    assert passes, "the obligation thread never ran its startup pass"

    def halt():
        stop.set()
        attention.set()
        thread.join(5)

    halt.marker_passes = marker_passes
    return passes, attention, halt


def _settle(passes, quiet=0.4, limit=10.0):
    """Wait until the thread has run no pass for ``quiet`` seconds."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if passes and time.monotonic() - passes[-1] >= quiet:
            return
        time.sleep(0.05)
    pytest.fail(f"the obligation thread never went idle ({len(passes)} passes)")


def test_no_sync_owner_recovers_a_stale_cache_marker_naming_a_reused_pid(
        env, monkeypatch):
    """R32-V1 (#787 / #861 revision 33): a stale cache.db repair marker whose
    record names a live but reused PID reads as `maintenance` to a query
    (R32-1 never checks the start identity), and maintenance is nobody's
    owner work, so nothing woke the idle `--no-sync` owner and the marker
    blocked every cache read until a restart. While a marker exists the
    thread runs the owner cache open once per interval, whose full identity
    check recovers it. Detects a `--no-sync` dashboard whose panels read the
    transcripts forever behind a dead repair."""
    _write_claude_turns(env, "stale-a", ("2026-05-31T10:00:00.000Z",))
    tick = _dashboard_publisher(env, monkeypatch)
    _assert_coherent(tick(skip_sync=False))
    passes, _attention, halt = _start_no_sync_owner(env, monkeypatch)
    try:
        _settle(passes)
        marker = env["db"]._repair_marker_path(_path(env, "cache"))
        _write_owner_record(marker, "reused")
        with pytest.raises(env["core"].StoreUnavailable) as refused:
            with env["access"].QueryService.session("cache"):
                pass  # pragma: no cover
        assert refused.value.reason == "maintenance", "premise"
        deadline = time.monotonic() + 5
        while marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not marker.exists(), (
            "the --no-sync owner never recovered a reused PID's marker")
        with env["access"].QueryService.session("cache"):
            pass
    finally:
        halt()


def test_no_sync_owner_leaves_a_live_owners_cache_marker_in_place(
        env, monkeypatch, capsys):
    """R32-V1: the owner cache open the `--no-sync` thread runs while a
    marker exists applies the full owner check, so a live repair's marker
    (this process, its real start identity) is refused, never recovered.
    Detects an owner pass that removes a live repair's marker, which would
    let a query read the file the repair is rewriting.

    F5 (round 9): while only the marker is outstanding the thread runs only
    that owner cache open, once per interval, and logs its refusal once per
    marker. Detects the whole drain (owner `open_db()`, conversations
    readiness, Doctor recovery) running every interval for as long as a
    repair lasts, forever for a hung one, and a stderr line on every pass."""
    db, cache_path = env["db"], _path(env, "cache")
    passes, _attention, halt = _start_no_sync_owner(env, monkeypatch)
    try:
        _settle(passes)
        capsys.readouterr()
        claim, why = db._claim_repair_marker(cache_path)
        assert claim is not None, why
        try:
            before = len(passes)
            marker_before = len(halt.marker_passes)
            time.sleep(0.6)
            marker = db._repair_marker_path(cache_path)
            assert marker.exists(), "the owner removed a live repair's marker"
            owner, _record = db._read_repair_owner_record(marker)
            assert owner is not None and owner["claim_id"] == claim.claim_id
            assert len(halt.marker_passes) - marker_before >= 3, (
                "non-vacuity: the marker's owner cache open did not run "
                "once per interval while the marker existed")
            assert len(passes) == before, (
                "the whole obligation drain ran while only a marker was "
                "outstanding", len(passes) - before)
            logged = capsys.readouterr().err.count(
                "obligation pass: cache open failed")
            assert logged == 1, logged
        finally:
            db._release_repair_marker(cache_path, claim)
    finally:
        halt()


def _seed_usage_journal(env):
    import _cctally_journal as journal
    import _lib_journal as wire
    resets_at = int(dt.datetime(2026, 8, 6, tzinfo=dt.timezone.utc)
                    .timestamp())
    journal.append_record(wire.make_obs(
        at="2026-08-02T12:00:00Z", src="statusline", provider="claude",
        payload={"weekly_percent": 12.0, "resets_at": resets_at,
                 "source": "statusline",
                 "captured_at": "2026-08-02T12:00:00Z"}))
    journal.run_stats_ingest(mode="authoritative")


def _epoch_worker_in_thread(env, monkeypatch):
    """Stand in for the detached epoch worker AD12 lets the owner `open_db`
    launch: the real worker body, on a thread of this process."""
    import types
    store = env["store"]
    ran: list = []

    def spawn(_command, _launch_id):
        def body():
            with env["core"].establish_authority("process"):
                ran.append(store.cmd_stats_epoch_rebuild_internal(
                    types.SimpleNamespace()))
        threading.Thread(target=body, daemon=True).start()
        return os.getpid()

    monkeypatch.setattr(store, "_spawn_rebuild_worker", spawn)
    return ran


def test_no_sync_owner_heals_a_stats_index_that_went_version_ahead(
        env, monkeypatch):
    """R32-2 (#787 / #861 revision 32, browser finding B3): a version-ahead
    stats.db is owner work, because the owner `open_db` defers it to the
    epoch rebuild worker, which rebuilds the disposable index from the
    retained journal. Detects an owner-attention filter that treats it as
    nobody's work: under `--no-sync` every stats panel and Explain stayed
    refused, where `main`'s request opener rebuilt the index."""
    core, access = env["core"], env["access"]
    _seed_usage_journal(env)
    ran = _epoch_worker_in_thread(env, monkeypatch)
    passes, _attention, halt = _start_no_sync_owner(env, monkeypatch)
    try:
        _settle(passes)
        stats_path = _path(env, "stats")
        _stamp(stats_path, _head(env, "stats") + 1)
        with pytest.raises(core.StoreUnavailable) as refused:
            with access.QueryService.session("stats"):
                pass  # pragma: no cover
        assert refused.value.reason == "schema_ahead", "premise"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if access.QueryService.readiness("stats").readable:
                break
            time.sleep(0.1)
        assert access.QueryService.readiness("stats").readable, (
            "the --no-sync owner left a version-ahead stats.db unhealed",
            ran)
    finally:
        halt()


@pytest.mark.parametrize("store", ["stats", "cache"])
def test_no_sync_probe_never_wakes_the_owner_on_an_unhealable_store(
        env, monkeypatch, store):
    """R32-V2 (#787 / #861 revision 33): the `--no-sync` thread's
    outstanding-work probe read readiness through the notifying path, so a
    version-ahead store it observed woke the thread itself, reset its
    backoff and retried a state the owner cannot heal (a stats index with no
    retained journal, a cache trim that keeps failing) at the coalescing
    rate for the dashboard's lifetime. Detects a probe that notifies: the
    owner passes over a fixed window must stay within the backoff's
    schedule, and the probe must set no attention."""
    dash = sys.modules["_cctally_dashboard"]
    _stamp(_path(env, store), _head(env, store) + 1)
    if store == "stats":
        # No retained journal: the epoch worker cannot rebuild it.
        monkeypatch.setattr(env["store"], "_spawn_rebuild_worker",
                            lambda *_a: None)
    else:
        def failing_owner(*_a, **_k):
            raise sqlite3.DatabaseError("cache trim failed")

        monkeypatch.setattr(dash, "open_cache_db_owner", failing_owner)
    wakes: list = []
    monkeypatch.setattr(dash, "_wake_schema_writer",
                        lambda *a, **k: wakes.append(k))
    passes, attention, halt = _start_no_sync_owner(env, monkeypatch)
    try:
        time.sleep(3.0)
        window = [t for t in passes if t - passes[0] <= 3.0]
        woken = len(wakes)
    finally:
        halt()
    # Doubling from 0.05 s: passes at about 0, 0.05, 0.15, 0.35, 0.75, 1.55
    # and 3.15 s. A cache notification wakes the schema writer, whose full
    # conversations open runs the owner cache open too: one more attempt.
    assert len(window) + woken <= 7, (
        f"{len(window)} owner passes and {woken} schema-writer wakes in 3 s: "
        "the probe woke the owner")
    attention.clear()
    assert dash._obligation_outstanding(dash._obligation_readiness) is True
    assert not attention.is_set(), "the outstanding-work probe notified"


@pytest.mark.parametrize("store, outstanding", [
    ("cache", True), ("stats", True), ("conversations", False)])
def test_only_a_version_ahead_cache_or_stats_is_obligation_work(
        env, store, outstanding):
    """R32-2: a conversations store ahead of this binary stays refused, as
    today (this binary cannot read a newer conversations schema), while a
    version-ahead cache.db is trimmed by the owner cache open and a
    version-ahead stats index is rebuilt from the journal by the epoch
    worker the owner `open_db` launches. Detects an obligation cadence that
    spins on an unhealable ahead store, or one that stops before a healable
    cache or stats index is healed."""
    import types
    dash = sys.modules["_cctally_dashboard"]

    def readiness_of(name):
        if name == store:
            return types.SimpleNamespace(
                readable=False, reason="schema_ahead", obligations=())
        return types.SimpleNamespace(
            readable=True, reason=None, obligations=())

    assert dash._obligation_outstanding(readiness_of) is outstanding
