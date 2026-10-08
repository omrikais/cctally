#!/usr/bin/env python3
"""Runner-local CPU admission, separate from transport/workdir leases.

The persistent inode is never unlinked or replaced. flock belongs to the open
file description and is inherited by the foreground workload. Owner metadata
also guards surviving workload groups after a killed supervisor loses its FD.
Raw observations remain owner-only files, outside the sanitized export tree.
"""

import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import uuid


DEFAULT_ROOT = Path.home() / ".cache/cctally-runner-reservation/v1"
FD_ENV = "_CCTALLY_RUNNER_RESERVATION_FD"
TOKEN_ENV = "_CCTALLY_RUNNER_RESERVATION_TOKEN"


def process_table():
    """Retain process-start identity; unreadable tables are never idle."""
    proc = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart=,args="],
        capture_output=True, text=True, check=True, timeout=10,
        env={**os.environ, "LC_ALL": "C", "TZ": "Etc/UTC"},
    )
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 9)
        if len(parts) != 10:
            raise ValueError("incomplete process identity")
        rows.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                     "pgid": int(parts[2]), "state": parts[3],
                     "start": " ".join(parts[4:9]), "argv": parts[9]})
    if not rows:
        raise ValueError("empty process table")
    return rows


def ancestors(rows, pid=None):
    by_pid = {row["pid"]: row for row in rows}
    result = set()
    pid = os.getpid() if pid is None else pid
    while pid and pid not in result:
        result.add(pid)
        row = by_pid.get(pid)
        if row is None:
            break
        pid = row["ppid"]
    return result


def legacy_blockers(rows, excluded):
    """Known legacy suites and opaque live wrapper jobs, conservatively.

    Only executable/script positions are matched; arbitrary source text in a
    Python -c argument must not turn the observer itself into a legacy suite.
    A Runner.Worker outside our ancestry is occupied even before its suite
    starts, covering old CI code that does not yet participate in flock.
    """
    result = []
    coordinated_parents = set()
    for row in rows:
        try:
            head = shlex.split(row["argv"])[:3]
        except ValueError:
            continue
        if any(Path(arg).name == "_lib_runner_reservation.py" for arg in head) and "run" in head:
            coordinated_parents.add(row["ppid"])
    for row in rows:
        if row["pid"] in excluded or row["state"].startswith("Z"):
            continue
        try:
            args = shlex.split(row["argv"])
        except ValueError:
            args = row["argv"].split()
        if not args:
            continue
        names = [Path(arg).name for arg in args[:3]]
        known = any(re.fullmatch(r"cctally-(?:test-all|.+-test)", n) for n in names)
        known |= "pytest" in names or "py.test" in names
        known |= names[0] in {"Runner.Worker", "vitest", "playwright"}
        # Old detached wrappers carry a script as bash -c. Their prologue is
        # stronger evidence of wrapper occupancy than a generic python3 name.
        if names[0] in {"bash", "sh"} and len(args) >= 3 and args[1] == "-c":
            if row["pid"] not in coordinated_parents:
                known |= "CCTALLY_REMOTE_EXEC=1" in args[2]
                known |= bool(re.search(r"(?:^|[\s;/])bin/cctally-test-all(?:\s|$)", args[2]))
        if known:
            result.append(row)
    return result


def snapshot():
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        rows = process_table()
        return {"timestamp": stamp, "hostname": socket.gethostname(),
                "cpuCount": os.cpu_count(), "load": list(os.getloadavg()),
                "processState": "observed", "processes": rows}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"timestamp": stamp, "hostname": socket.gethostname(),
                "cpuCount": os.cpu_count(), "load": None,
                "processState": "unknown", "processes": []}


def read_owner(root):
    try:
        doc = json.loads((root / "owner.json").read_text())
        if doc["schemaVersion"] != 1 or not isinstance(doc["token"], str):
            return None, "unknown"
        if doc.get("state") not in {"admitted", "active", "finished"}:
            return None, "unknown"
        if type(doc.get("pid")) is not int or doc["pid"] <= 0 or not isinstance(doc.get("start"), str):
            return None, "unknown"
        if not isinstance(doc.get("groups", []), list):
            return None, "unknown"
        if any(type(group) is not int or group <= 0 for group in doc.get("groups", [])):
            return None, "unknown"
        if not isinstance(doc.get("groupStarts", {}), dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in doc.get("groupStarts", {}).items()):
            return None, "unknown"
        return doc, "observed"
    except FileNotFoundError:
        return None, "absent"
    except (OSError, ValueError, KeyError, TypeError):
        return None, "unknown"


def write_owner(root, owner):
    # Atomic publication is metadata only. The lock inode stays fixed.
    temp = root / ("owner." + owner["token"] + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(owner, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, root / "owner.json")


def remaining_groups(owner, rows):
    if not owner or owner.get("state") == "finished":
        return []
    groups = owner.get("groups", [])
    # A PID can become a new process-group leader only after the original
    # group is gone. A different leader start proves that stale group number
    # no longer owns its current members. An absent leader proves no such thing.
    starts = owner.get("groupStarts", {})
    reused = {row["pid"] for row in rows
              if row["pid"] == row["pgid"] and starts.get(str(row["pid"]))
              and starts[str(row["pid"])] != row["start"]}
    groups = [group for group in groups if group not in reused]
    return [row for row in rows if row["pgid"] in groups and not row["state"].startswith("Z")]


def owned_descendants(rows, root_pid, groups, root_start):
    """Separate test-created process sessions from unrelated peer workloads."""
    owned = {row["pid"] for row in rows
             if (row["pid"] == root_pid and row["start"] == root_start) or row["pgid"] in groups}
    while True:
        expanded = owned | {row["pid"] for row in rows if row["ppid"] in owned}
        if expanded == owned:
            return owned
        owned = expanded


def open_lock(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return os.open(root / "reservation.lock", os.O_RDWR | os.O_CREAT, 0o600)


def try_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def occupancy(root, fd, sample, caller="wrapper"):
    if not try_lock(fd):
        return "busy", []
    owner, owner_state = read_owner(root)
    rows = sample["processes"]
    if sample["processState"] != "observed" or owner_state == "unknown":
        return "unknown", []
    survivors = remaining_groups(owner, rows)
    blockers = legacy_blockers(rows, ancestors(rows))
    if survivors or blockers:
        return "busy", survivors + blockers
    return "idle", []


def inherited_fd(root):
    """A nested invocation may use its ancestor's real open description."""
    try:
        owner, state = read_owner(root)
        if state != "observed" or owner["token"] != os.environ.get(TOKEN_ENV):
            return None
        rows = process_table()
        if owner["pid"] not in ancestors(rows) or not any(
                row["pid"] == owner["pid"] and row["start"] == owner.get("start") for row in rows):
            return None
        try:
            fd = int(os.environ[FD_ENV])
            lock_stat = (root / "reservation.lock").stat()
            fd_stat = os.fstat(fd)
            if (lock_stat.st_dev, lock_stat.st_ino) == (fd_stat.st_dev, fd_stat.st_ino):
                return fd
        except (KeyError, OSError, ValueError):
            pass
        # Python subprocess defaults close inherited descriptors. A nested
        # helper in the verified live supervisor's ancestry may still borrow
        # that ancestor's reservation, provided the inode really is locked.
        probe_fd = open_lock(root)
        try:
            return None if try_lock(probe_fd) else -1
        finally:
            os.close(probe_fd)
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        return None


def observe(stream, run_id, phase, sample, **extra):
    # The private record may contain argv and paths; never print or export it.
    stream.write(json.dumps({"schemaVersion": 1, "runId": run_id,
                             "phase": phase, **sample, **extra}, sort_keys=True) + "\n")
    stream.flush()


def run(args):
    inherited = inherited_fd(args.root)
    if inherited is not None:
        # No LOCK_UN and no owner rewrite: only the outer supervisor releases.
        proc = subprocess.Popen(args.command, pass_fds=(inherited,) if inherited >= 0 else ())
        result = proc.wait()
        return 128 - result if result < 0 else result
    fd = open_lock(args.root)
    started = time.monotonic()
    evidence_fd = os.open(args.evidence, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    token = uuid.uuid4().hex
    with os.fdopen(evidence_fd, "a") as evidence:
        while True:
            sample = snapshot()
            status, blockers = occupancy(args.root, fd, sample)
            observe(evidence, args.run_id, "admission", sample,
                    reservationState=status, blockers=blockers)
            # Deadline applies before and after the claim, including an idle
            # admission. It never changes the command's own time allowance.
            expired = args.deadline is not None and time.time() >= args.deadline
            if status == "idle" and not expired:
                break
            fcntl.flock(fd, fcntl.LOCK_UN)
            if expired or time.monotonic() - started >= args.wait:
                print("runner reservation: acquisition deadline reached" if expired else
                      "runner reservation: host occupied or ownership unknown; no workload started",
                      file=sys.stderr)
                os.close(fd)
                return 75 if expired else args.busy_exit
            pause = min(args.sample_seconds, max(0, args.wait - (time.monotonic() - started)))
            if args.deadline is not None:
                pause = min(pause, max(0, args.deadline - time.time()))
            time.sleep(pause)
        owner = {"schemaVersion": 1, "token": token, "pid": os.getpid(),
                 "start": next(row["start"] for row in sample["processes"] if row["pid"] == os.getpid()),
                 "runId": args.run_id, "state": "admitted", "groups": []}
        write_owner(args.root, owner)
        env = {**os.environ, FD_ENV: str(fd), TOKEN_ENV: token}
        os.set_inheritable(fd, True)
        proc = None
        barrier_read = barrier_write = None
        received = []

        def forward_cancel(signum):
            if proc is None or not owner.get("groupStarts"):
                return
            try:
                rows = process_table()
            except (OSError, ValueError, subprocess.SubprocessError):
                # Keep the reservation and retry on the next observation;
                # stale metadata alone is never permission to signal a group.
                return
            for group in {row["pgid"] for row in remaining_groups(owner, rows)}:
                try:
                    os.killpg(group, signum)
                except (ProcessLookupError, PermissionError):
                    # A missing or unsignalable group is not proof that the
                    # workload finished. Keep its reservation, then recheck
                    # identity and retry cancellation on the next observation.
                    pass

        def cancel(signum, _frame):
            received.append(signum)
            forward_cancel(signum)

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, cancel)
        try:
            # The command cannot execute in the crash window between fork and
            # publishing its group. A killed supervisor closes the barrier;
            # EOF then exits the launcher without ever starting the workload.
            barrier_read, barrier_write = os.pipe()
            launcher = ("import os,sys; fd=int(sys.argv[1]); "
                        "ready=os.read(fd,1); os.close(fd); "
                        "sys.exit(3) if ready != b'1' else None; "
                        "os.execvpe(sys.argv[2],sys.argv[2:],os.environ)")
            proc = subprocess.Popen([sys.executable, "-c", launcher,
                                     str(barrier_read), *args.command],
                                    start_new_session=True,
                                    pass_fds=(fd, barrier_read), env=env)
            os.close(barrier_read)
            barrier_read = None
            owner.update(state="active", groups=[proc.pid])
            child_identity = next((row for row in process_table() if row["pid"] == proc.pid), None)
            if child_identity is None:
                raise ValueError("workload group identity unavailable before admission")
            owner["groupStarts"] = {str(proc.pid): child_identity["start"]}
            write_owner(args.root, owner)
            if received:
                forward_cancel(received[0])
            else:
                os.write(barrier_write, b"1")
            os.close(barrier_write)
            barrier_write = None
            while True:
                if received:
                    forward_cancel(received[0])
                sample = snapshot()
                rows = sample["processes"]
                live_groups = {row["pgid"] for row in remaining_groups(owner, rows)}
                owned = owned_descendants(rows, proc.pid, live_groups,
                                          owner["groupStarts"].get(str(proc.pid)))
                groups = sorted(set(owner["groups"]) | {r["pgid"] for r in rows if r["pid"] in owned})
                group_starts = dict(owner["groupStarts"])
                for row in rows:
                    if row["pid"] == row["pgid"] and row["pid"] in owned:
                        group_starts[str(row["pgid"])] = row["start"]
                if groups != owner["groups"] or group_starts != owner["groupStarts"]:
                    owner["groups"] = groups
                    owner["groupStarts"] = group_starts
                    write_owner(args.root, owner)
                peers = legacy_blockers(rows, ancestors(rows) | owned)
                observe(evidence, args.run_id, "execution", sample,
                        reservationState="owned", workloadPgid=proc.pid,
                        ownedProcessIds=sorted(owned),
                        peerWorkloads=peers, overlapObserved=bool(peers))
                result = proc.poll()
                # A shell exiting before a background child is not completion.
                # Unknown process visibility retains the reservation.
                if result is not None and sample["processState"] == "observed" \
                        and not remaining_groups(owner, rows):
                    break
                if result is None:
                    try:
                        proc.wait(timeout=args.sample_seconds)
                    except subprocess.TimeoutExpired:
                        pass
                else:
                    time.sleep(args.sample_seconds)
            owner["state"] = "finished"
            write_owner(args.root, owner)
            observe(evidence, args.run_id, "finished", snapshot(), exitCode=result)
            return 128 + received[0] if received else (128 - result if result < 0 else result)
        finally:
            # Inherited FDs and active owner metadata still protect survivors
            # after an abnormal supervisor exit. Never unlink the lock inode.
            os.close(fd)
            for pipe_fd in (barrier_read, barrier_write):
                if pipe_fd is not None:
                    os.close(pipe_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "probe"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--run-id", default="unknown")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--wait", type=float, default=1800)
    parser.add_argument("--busy-exit", type=int, default=3)
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--sample-seconds", type=float, default=30)
    args, command = parser.parse_known_args(argv)
    args.command = command[1:] if command[:1] == ["--"] else command
    if args.operation == "probe":
        # Probes never create directories, an inode or owner metadata.
        sample = snapshot()
        lock_path = args.root / "reservation.lock"
        if not lock_path.exists() and not (args.root / "owner.json").exists():
            status = "unknown" if sample["processState"] == "unknown" else (
                "busy" if legacy_blockers(sample["processes"], ancestors(sample["processes"])) else "idle")
        else:
            try:
                fd = os.open(lock_path, os.O_RDONLY)
                try:
                    status, _ = occupancy(args.root, fd, sample)
                finally:
                    os.close(fd)
            except OSError:
                status = "unknown"
        print(status)
        return 0
    if not args.command or args.evidence is None or args.wait < 0 or args.sample_seconds <= 0:
        parser.error("run requires a command, evidence file and nonnegative wait")
    args.evidence.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        return run(args)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("runner reservation: coordination or observation unavailable; no admission guarantee", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
