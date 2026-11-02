"""Held-stage process harness for the #1000 exclusion tests (spec 8, T8, T9).

A *held* stage blocks a production path (the probe's live read, recovery's
`confirmed` pause) on a file the test controls. While it is held, the test
starts child processes that each call one production path. Every child writes
``<name>.ack`` immediately before it calls the path under test and
``<name>.done`` (JSON) with its result immediately after, so the test never
synchronizes on a sleep: it waits for those files under fixed deadlines and
fails on a missed one. It never waits unboundedly on a blocked opener.

Children load ``bin/cctally`` the way ``tests/_script_loader.py`` does and
inherit ``PYTEST_CURRENT_TEST`` from the test's environment, so the
pytest-only seams are honored in them.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
CCTALLY = ROOT / "bin" / "cctally"

#: Spec 8 deadlines.
ACK_SECONDS = 10.0
BLOCKED_SECONDS = 1.0
HOLD_SECONDS = 30.0
DONE_AFTER_RELEASE_SECONDS = 20.0
#: How long a holder may take to reach its stage (a full `cache-sync` start).
REACH_SECONDS = 90.0
#: How long a holder may take to finish once released.
HOLDER_EXIT_SECONDS = 120.0

_DRIVER = r'''
import json, os, pathlib, subprocess, sys, time
root, workdir, name, mode, *rest = sys.argv[1:]
sys.path.insert(0, os.path.join(root, "tests"))
sys.path.insert(0, os.path.join(root, "bin"))
workdir = pathlib.Path(workdir)


def publish(suffix, payload):
    target = workdir / f"{name}.{suffix}"
    tmp = workdir / f".{name}.{suffix}.tmp"
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, target)


def ack():
    publish("ack", {"pid": os.getpid(), "at": time.monotonic()})


def wait_for_file(path, seconds):
    deadline = time.monotonic() + seconds
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.02)


if mode == "cli":
    ack()
    result = subprocess.run(
        [sys.executable, os.path.join(root, "bin", "cctally"), *rest],
        capture_output=True, text=True, timeout=120,
    )
    publish("done", {"returncode": result.returncode,
                     "stdout": result.stdout, "stderr": result.stderr})
    raise SystemExit(0)

from _script_loader import load_script_module
mod = load_script_module()
cache = mod._load_sibling("_cctally_cache")
core = sys.modules["_cctally_core"]

if mode == "create":
    conn = mod.open_conversations_db(attach_cache=False)
    conn.close()
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    for suffix in ("-wal", "-shm"):
        pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)
    publish("done", {"path": str(path), "newest": [
        m.name for m in mod._CONVERSATIONS_MIGRATIONS][-1]})
elif mode == "provider-locks":
    ack()
    locks = cache._acquire_conversation_provider_locks(timeout=0.05)
    if locks is not None:
        cache._release_conversation_provider_locks(locks)
    publish("done", {"acquired": locks is not None})
elif mode == "open":
    ack()
    try:
        conn = mod.open_conversations_db(attach_cache=False)
        conn.close()
        publish("done", {"opened": True})
    except Exception as exc:
        publish("done", {"opened": False, "error": str(exc)})
elif mode == "explain":
    import datetime as dt
    import _lib_diagnosis as kernel
    sources = mod._load_sibling("_cctally_diagnosis_sources")
    utc = dt.timezone.utc
    scope = sources.DiagnosisScope(
        source="claude", account_key=None,
        window_start=dt.datetime(2026, 8, 10, tzinfo=utc),
        window_end=dt.datetime(2026, 8, 17, tzinfo=utc),
        effective_speed=None, display_tz="UTC",
    )
    plan = kernel.resolve_policy_plan("claude", transcripts_visible=True)
    bundle = sources.StoreBundle(scope, plan)
    ack()
    try:
        conn = bundle.connection("conversations")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        publish("done", {"opened": True})
    except sources.EstablishmentFailure as exc:
        publish("done", {"opened": False, "code": exc.code,
                         "message": exc.message})
    finally:
        bundle.close()
elif mode == "probe":
    # T9: no keeper-yield fence can be published, as on exFAT (#1020).
    import errno

    def refuse(_path):
        raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))

    cache._cctally_db_sib._claim_keeper_yield_request = refuse
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    ack()
    try:
        trigger = cache._probe_conversation_rebuild(path, lock_timeout=5.0)
        publish("done", {"raised": None, "trigger": (
            None if trigger is None else str(trigger))})
    except Exception as exc:
        publish("done", {"raised": type(exc).__name__, "error": str(exc)})
elif mode == "finalize":
    # T9: the production unkept-store finalizer on an armed store.
    import _lib_wal_checkpoint as wc
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    hold = rest[0] if rest else ""
    # The PASSIVE leaves `<marker>.attempted`, so a completed cycle proves it
    # ran rather than returned early; with a hold it then waits for release.
    marker = hold or str(workdir / f"{name}-cycle")
    real_attempt = wc.WalCheckpointPolicy.attempt

    def recorded_attempt(self, conn):
        result = real_attempt(self, conn)
        pathlib.Path(f"{marker}.attempted").write_text("1")
        if hold:
            wait_for_file(f"{hold}.release", 60)
        return result

    wc.WalCheckpointPolicy.attempt = recorded_attempt
    key = wc._key(path)
    identity = wc._identity(key)
    with wc._KEEPER_LOCK:
        wc._ARMED[key] = identity
    ack()
    with wc._KEEPER_LOCK:
        wc._ARMED.pop(key, None)
        wc._finalize_unkept_locked(key, identity)
    publish("done", {"finalized": True})
else:
    raise SystemExit(f"unknown mode {mode}")
'''


def child_env(tmp_path: pathlib.Path, **extra: str) -> dict:
    """An isolated environment for `bin/cctally` children."""
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    codex = tmp_path / "codex"
    (codex / "sessions").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("CCTALLY_TEST_CONVERSATION_PROBE_COPY", None)
    env.update({
        "HOME": str(home),
        "CODEX_HOME": str(codex),
        "CCTALLY_DATA_DIR": str(tmp_path / "data"),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "TZ": "Etc/UTC",
    })
    env.update(extra)
    return env


class Harness:
    """Children, their acknowledgement files and the family observer."""

    def __init__(self, tmp_path: pathlib.Path, env: dict) -> None:
        self.workdir = tmp_path / "hold"
        self.workdir.mkdir(exist_ok=True)
        self.driver = tmp_path / "hold_driver.py"
        self.driver.write_text(_DRIVER)
        self.env = env
        self.children: dict[str, subprocess.Popen] = {}

    # -- children -----------------------------------------------------------

    def start(self, name: str, mode: str, *args: str,
              env: "dict | None" = None) -> subprocess.Popen:
        log = open(self.workdir / f"{name}.log", "w")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(self.driver), str(ROOT),
                 str(self.workdir), name, mode, *args],
                env=env or self.env, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()
        self.children[name] = proc
        return proc

    def run(self, name: str, mode: str, *args: str,
            env: "dict | None" = None, seconds: float = 120.0) -> dict:
        """Run one child to completion and return its `done` payload."""
        proc = self.start(name, mode, *args, env=env)
        proc.wait(timeout=seconds)
        done = self.done(name)
        assert done is not None, self.log(name)
        return done

    def ack(self, name: str) -> "dict | None":
        return self._read(f"{name}.ack")

    def done(self, name: str) -> "dict | None":
        return self._read(f"{name}.done")

    def log(self, name: str) -> str:
        try:
            return (self.workdir / f"{name}.log").read_text()
        except OSError:
            return ""

    def _read(self, leaf: str) -> "dict | None":
        try:
            return json.loads((self.workdir / leaf).read_text())
        except FileNotFoundError:
            return None

    def wait(self, predicate, seconds: float, what: str) -> None:
        """Wait for ``predicate()`` under a fixed deadline; fail on a miss."""
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(f"missed the {seconds:g} s deadline: {what}")
            time.sleep(0.02)

    def wait_ack(self, name: str) -> float:
        self.wait(lambda: self.ack(name) is not None, ACK_SECONDS,
                  f"{name} ack\n{self.log(name)}")
        return time.monotonic()

    def close(self) -> None:
        """Kill each child's whole process group: a `cli` driver runs
        `cctally` as its own child, and a holder orphaned by a failed test
        would keep the probe's locks on the runner and then rebuild."""
        for proc in self.children.values():
            if proc.returncode is None:
                # Not yet reaped, so its group id cannot have been reused.
                # macOS answers EPERM when the group's only member is the
                # exited, unreaped driver: nothing is left to kill.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            proc.wait(timeout=10)


def family(path: pathlib.Path) -> dict:
    """The family's names, sizes and sha256, journal included."""
    out = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        member = pathlib.Path(f"{path}{suffix}")
        try:
            data = member.read_bytes()
        except FileNotFoundError:
            continue
        out[member.name] = (len(data), hashlib.sha256(data).hexdigest())
    return out


def main_identity(path: pathlib.Path) -> tuple:
    st = os.lstat(path)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest())


def corrupt_second_page(path: pathlib.Path) -> None:
    """Damage a page that opening never reads; only quick_check finds it."""
    raw = bytearray(path.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big")
    if page_size == 1:
        page_size = 65_536
    assert len(raw) >= page_size * 3
    raw[page_size:page_size * 2] = b"\xa5" * page_size
    path.write_bytes(raw)


def held_stage(harness: Harness, *, store: pathlib.Path, reached,
               release, holder: subprocess.Popen, blocking: list[str],
               declining: list[str], start_children) -> dict:
    """Hold a stage while children run against the store (spec 8, T8).

    Waits for ``reached()``, records the family, starts the children through
    ``start_children()``, and keeps the stage held until every ``blocking``
    child has stayed blocked for at least one second after its ack and every
    ``declining`` child has written its decline, within the 30 s hold
    deadline, checking that the family stays byte-identical throughout. Then
    calls ``release()`` and waits for every blocked child's `done` within
    20 s. Returns ``{"family": <family when reached>, "done": {name: done}}``.
    """
    harness.wait(lambda: reached() or holder.poll() is not None,
                 REACH_SECONDS, "the held stage")
    assert reached(), f"the holder exited before its stage: {holder.returncode}"
    at_reach = family(store)
    start_children()
    acked = {name: harness.wait_ack(name) for name in blocking + declining}
    deadline = time.monotonic() + HOLD_SECONDS
    while True:
        assert family(store) == at_reach, "the family changed during the hold"
        now = time.monotonic()
        for name in blocking:
            assert harness.done(name) is None, (
                f"{name} was not excluded: {harness.done(name)}")
        blocked_long_enough = all(
            now - acked[name] >= BLOCKED_SECONDS for name in blocking)
        declined = all(harness.done(name) is not None for name in declining)
        if blocked_long_enough and declined:
            break
        if now >= deadline:
            raise AssertionError(
                "missed the 30 s hold deadline: "
                + ", ".join(f"{name}: {harness.log(name)!r}"
                            for name in declining
                            if harness.done(name) is None))
        time.sleep(0.02)
    assert family(store) == at_reach, "the family changed during the hold"
    results = {name: harness.done(name) for name in declining}
    release()
    for name in blocking:
        harness.wait(lambda name=name: harness.done(name) is not None,
                     DONE_AFTER_RELEASE_SECONDS,
                     f"{name} done after release\n{harness.log(name)}")
        results[name] = harness.done(name)
    return {"family": at_reach, "done": results}
