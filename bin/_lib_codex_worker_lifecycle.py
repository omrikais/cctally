"""Attempt records for the detached Codex workers (#939 D5).

The Codex syncs hand work to detached workers (`_codex-replay-drain`,
`_codex-prune-drain`, `_codex-inventory-bootstrap`). A worker runs several
stages without an enclosing lock, its admission gate is released between
identities, and a detached spawn returns before the child takes any lock, so a
momentarily free gate or lock is no evidence that none is running. writeIo's
startup admission hold needs that evidence before it may judge two passes
settled, so every worker request keeps its own record:

* the spawner writes ``codex-workers/<command>.<attempt>.json`` with
  ``requestedAt`` immediately before it spawns, passes the attempt id to the
  child in ``ATTEMPT_ENV`` (a throttled request writes nothing), and adds the
  child's ``spawnPid`` right after the spawn (`note_spawned`);
* the worker's first action (`mark_started`) opens
  ``codex-workers/<command>.<attempt>.lock``, takes ``flock(LOCK_EX)`` and
  keeps that descriptor open for its whole life, and only then adds
  ``startedAt`` and its pid; its ``finally`` (`end_attempt`) removes the
  record, then the lock file, then closes the descriptor;
* a started record is live exactly while its lock is held: a separate open
  and ``flock(LOCK_SH | LOCK_NB)`` that is refused means live, one that is
  granted (released at once) or a missing lock file means dead, and any other
  error falls back to the recorded pid (conservative). The probe never creates
  the lock file. The kernel drops the lock with the worker's last descriptor,
  so a worker killed without its ``finally`` (SIGKILL, logout, reboot) is dead
  at once, whatever process later reuses its pid;
* a record without ``startedAt`` is live only while it is younger than
  ``HANDOFF_SECONDS`` and its ``spawnPid`` is absent or alive, so a child that
  died before marking its start stops counting at once instead of keeping a
  young record alive on every re-spawn;
* one attempt's end never retires another, and the next spawner of a command
  removes that command's dead records and their lock files.

Every filesystem error is swallowed and logged: a record is diagnostic
evidence for the hold, never a reason for a spawner or a worker to fail. A
worker that could not open its lock records ``lock: false`` and is judged by
its pid, the pre-lock rule, whose only risk (pid reuse) delays a release, the
conservative direction.
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import secrets
import sys
import threading

WORKER_COMMANDS = (
    "_codex-replay-drain",
    "_codex-prune-drain",
    "_codex-inventory-bootstrap",
)
HANDOFF_SECONDS = 120
ATTEMPT_ENV = "CCTALLY_WORKER_ATTEMPT"
RECORD_DIRNAME = "codex-workers"

#: This process's own detached worker launches, attempt id -> its
#: ``subprocess.Popen`` handle (#939 R8-04): the one ownership-safe way to see
#: that a child this process launched has exited, on every Python (macOS
#: Python 3.11 and 3.12 have no ``os.waitid``). Keyed by attempt, never by
#: pid alone: a handle answers only for the record of its own launch, so a
#: reused pid in another process's record never borrows it (R9-02). Finished
#: entries are dropped at the next launch.
_LAUNCHES: dict = {}
_LAUNCH_LOCK = threading.Lock()


def remember_launch(handle, attempt=None) -> None:
    """The detached spawner, right after a launch: keep a worker attempt's
    process handle so its record's liveness asks the owner's own handle;
    drop finished ones. A launch without an attempt is not kept."""
    if not attempt:
        return
    with _LAUNCH_LOCK:
        for known, proc in list(_LAUNCHES.items()):
            try:
                finished = proc.poll() is not None
            except Exception:  # noqa: BLE001
                finished = True
            if finished:
                _LAUNCHES.pop(known, None)
        _LAUNCHES[str(attempt)] = handle


def _launch_alive(attempt, pid: int) -> "bool | None":
    """Whether this process's own launch of ``attempt``, with pid ``pid``,
    still runs, from its retained handle (polling it reaps the owner's own
    child and keeps its status on the handle); None when this process did
    not launch that attempt with that pid."""
    if not attempt:
        return None
    with _LAUNCH_LOCK:
        proc = _LAUNCHES.get(str(attempt))
    if proc is None or getattr(proc, "pid", None) != pid:
        return None
    try:
        return proc.poll() is None
    except Exception:  # noqa: BLE001
        return None


def _record_pid_alive(record, pid: int, pid_alive) -> bool:
    """A record's pid: through its own retained launch handle when this
    process launched it, else ``pid_alive``."""
    launched = _launch_alive((record or {}).get("attempt"), pid)
    return launched if launched is not None else bool(pid_alive(pid))


def _log(message: str) -> None:
    try:
        print(f"[codex-workers] {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — logging must never fail a worker
        pass


def _pid_alive(pid) -> bool:
    """Whether ``pid`` runs. An exited child is a zombie until it is reaped,
    and ``kill(pid, 0)`` answers for a zombie, so a child of this process is
    recognized by a NON-consuming ``waitid(..., WNOWAIT)`` where the platform
    has it: a recorded pid reused by another child keeps its exit status for
    its real owner (#939 R7-03). This process's own worker launches are asked
    through their retained handles before this (`_record_pid_alive`, R8-04).
    Anything else falls back to ``kill(pid, 0)``."""
    try:
        if int(pid) <= 0:       # P_PID needs a real pid; 0/-n name groups
            return False
    except (ValueError, TypeError):
        return False
    waitid = getattr(os, "waitid", None)
    if waitid is not None:
        try:
            exited = waitid(os.P_PID, int(pid),
                            os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except (ChildProcessError, OSError, ValueError, TypeError):
            pass                # not this process's child
        else:
            return exited is None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def _record_dir(app_dir) -> pathlib.Path:
    return pathlib.Path(app_dir) / RECORD_DIRNAME


def _record_path(app_dir, command: str, attempt: str) -> pathlib.Path:
    return _record_dir(app_dir) / f"{command}.{attempt}.json"


def _lock_path_for(record_path: pathlib.Path) -> pathlib.Path:
    return record_path.with_suffix(".lock")


def _unlink_quietly(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log(f"could not remove {path.name}: {exc}")


def _lock_held(lock_path: pathlib.Path) -> "bool | None":
    """Whether a worker holds ``lock_path``: ``True`` held, ``False`` free or
    missing, ``None`` when the probe itself failed. Never creates the file."""
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _write_record(path: pathlib.Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, sort_keys=True)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_records(app_dir, command: str) -> "list[tuple[pathlib.Path, dict | None]]":
    try:
        paths = sorted(_record_dir(app_dir).glob(f"{command}.*.json"))
    except OSError:
        return []
    out = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                record = None
        except (OSError, ValueError):
            record = None
        out.append((path, record))
    return out


def _record_age_origin(path: pathlib.Path, record: "dict | None") -> "float | None":
    """When the record's handoff started: ``requestedAt``, or the file's mtime
    for a record that cannot be parsed."""
    if record is not None:
        value = record.get("requestedAt")
        if isinstance(value, (int, float)):
            return float(value)
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _valid_pid(value) -> "int | None":
    return value if isinstance(value, int) and value > 0 else None


def _live(path, record, *, now: float, pid_alive) -> bool:
    if record is not None and record.get("startedAt") is not None:
        held = (_lock_held(_lock_path_for(path))
                if record.get("lock", True) else None)
        if held is not None:
            return held
        pid = _valid_pid(record.get("pid"))
        return pid is not None and _record_pid_alive(record, pid, pid_alive)
    origin = _record_age_origin(path, record)
    if origin is None or now - origin >= HANDOFF_SECONDS:
        return False
    spawn_pid = None if record is None else _valid_pid(record.get("spawnPid"))
    return spawn_pid is None or _record_pid_alive(record, spawn_pid, pid_alive)


def begin_attempt(app_dir, command: str, now: float) -> "str | None":
    """Write a requested attempt's record and return its id (``None`` when
    the record could not be written). Removes the command's dead records and
    their lock files."""
    try:
        for path, record in _read_records(app_dir, command):
            if not _live(path, record, now=now, pid_alive=_pid_alive):
                _unlink_quietly(path)
                _unlink_quietly(_lock_path_for(path))
        attempt = secrets.token_hex(8)
        _write_record(_record_path(app_dir, command, attempt), {
            "command": command, "attempt": attempt,
            "requestedAt": float(now), "startedAt": None, "pid": None})
        return attempt
    except OSError as exc:
        _log(f"could not record a {command} request: {exc}")
        return None


def note_spawned(app_dir, command: str, attempt: str, *, pid: int) -> None:
    """The spawner, right after the spawn: add the child's ``spawnPid``.

    Serialized against the child's `mark_started` through the attempt's lock:
    a lock the child already holds means it started (its record is not
    touched); otherwise the record is updated while the lock is held, so the
    child's own update cannot be lost. A record already gone (the attempt
    ended) is never recreated."""
    path = _record_path(app_dir, command, attempt)
    lock_path = _lock_path_for(path)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        _log(f"could not record a {command} spawn: {exc}")
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return                              # the child holds it: started
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            record = None
        except ValueError:
            return
        if not isinstance(record, dict):
            _unlink_quietly(lock_path)          # the attempt already ended
            return
        if record.get("startedAt") is not None:
            return
        record["spawnPid"] = int(pid)
        _write_record(path, record)
    except OSError as exc:
        _log(f"could not record a {command} spawn: {exc}")
    finally:
        os.close(fd)


def mark_started(app_dir, command: str, attempt: str, *, pid: int,
                 now: float) -> "int | None":
    """The worker's first action: take the attempt's lifetime lock, then add
    ``startedAt`` and its pid (creating the record when the spawner's was
    already removed). Returns the lock descriptor, which the worker keeps open
    until `end_attempt`; ``None`` when the lock could not be taken (the record
    then says ``lock: false`` and is judged by its pid)."""
    path = _record_path(app_dir, command, attempt)
    lock_fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(_lock_path_for(path), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as exc:
        _log(f"could not take a {command} lifetime lock: {exc}")
        if lock_fd is not None:
            os.close(lock_fd)
            lock_fd = None
    try:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                record = {}
        except (OSError, ValueError):
            record = {}
        record.update({"command": command, "attempt": attempt,
                       "startedAt": float(now), "pid": int(pid),
                       "lock": lock_fd is not None})
        record.setdefault("requestedAt", float(now))
        _write_record(path, record)
    except OSError as exc:
        _log(f"could not record a {command} start: {exc}")
    return lock_fd


def end_attempt(app_dir, command: str, attempt: str, lock=None) -> None:
    """Remove this attempt's own record, then its lock file, then release the
    lock (``lock`` is `mark_started`'s descriptor); never another's."""
    path = _record_path(app_dir, command, attempt)
    try:
        _unlink_quietly(path)
        _unlink_quietly(_lock_path_for(path))
    finally:
        if lock is not None:
            try:
                os.close(lock)
            except OSError as exc:
                _log(f"could not release a {command} lifetime lock: {exc}")


def in_progress(app_dir, command: str, *, now: float, pid_alive=None) -> bool:
    """Whether any attempt of ``command`` is requested or running."""
    alive = _pid_alive if pid_alive is None else pid_alive
    return any(_live(path, record, now=now, pid_alive=alive)
               for path, record in _read_records(app_dir, command))


def any_in_progress(app_dir, *, now: float, pid_alive=None) -> bool:
    return any(in_progress(app_dir, command, now=now, pid_alive=pid_alive)
               for command in WORKER_COMMANDS)
