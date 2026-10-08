"""#901 Amendment 19 (HR-7): find and reap a runner's whole process family by
the run token every family process inherits.

usage: wa_procs.py scan --token TOKEN
       wa_procs.py reap --token TOKEN --record FILE [--teardown FILE]
                        [--by NAME] [--rounds 5]

Every runner (`_inputs.sh` `wa_guard`) exports a fresh `WA_RUN_TOKEN` before
it starts anything, so every process of its family - the dashboard, the
workers it detaches with `start_new_session=True`, and their own children -
carries the token in the environment it was executed with, whatever session
or process group it moved to and whoever it was reparented to. A process
group kill misses a worker that called setsid; this does not.

`scan` lists the live processes of this user whose exec-time environment
holds `WA_RUN_TOKEN=TOKEN`, plus every descendant of one and every member of
a process group one of them leads (a protected system binary such as
/bin/sleep does not disclose its environment, but its parent and its group
leader do), never this process or one of its ancestors. A group is joined
only through its leader, so a family process can never pull in the group
of a process outside the family.
`reap` records each one (pid, ppid, pgid, start time) in FILE - and, with
`--teardown`, in a frozen family's teardown.jsonl too, so the family judge
reads the kill as the harness's teardown - BEFORE it sends SIGKILL, then
scans again, up to `--rounds` times, until no token process is left. The
environment is read from the kernel (macOS `KERN_PROCARGS2`, Linux
`/proc/PID/environ`); a process whose environment cannot be read is not
matched. Prints a JSON summary; exit 0 when nothing carrying the token is
left alive, 1 otherwise.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time

TOKEN_VAR = "WA_RUN_TOKEN"
_CTL_KERN, _KERN_ARGMAX, _KERN_PROCARGS2 = 1, 8, 49


def process_table() -> "list[dict]":
    """Every process: pid, ppid, pgid, uid and its start time (`ps`)."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,uid=,lstart="],
                         capture_output=True, text=True, timeout=30).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, pgid, uid = (int(x) for x in parts[:4])
        except ValueError:
            continue
        rows.append({"pid": pid, "ppid": ppid, "pgid": pgid, "uid": uid,
                     "start": " ".join(parts[4].split())})
    return rows


def _darwin_environ(pid: int) -> "dict | None":
    libc = ctypes.CDLL(None, use_errno=True)
    argmax = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    mib = (ctypes.c_int * 2)(_CTL_KERN, _KERN_ARGMAX)
    if libc.sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0):
        return None
    buf = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(argmax.value)
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
        return None
    data = buf.raw[:size.value]
    if len(data) < 4:
        return None
    argc = int.from_bytes(data[:4], sys.byteorder)
    rest = data[4:]
    end = rest.find(b"\0")                      # the executable path
    if end < 0:
        return None
    pos = end
    while pos < len(rest) and rest[pos] == 0:
        pos += 1
    strings = rest[pos:].split(b"\0")
    env = {}
    for item in strings[argc:]:
        if not item:
            break
        key, sep, value = item.partition(b"=")
        if sep:
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def environ(pid: int) -> "dict | None":
    """The environment `pid` was executed with, or None when unreadable."""
    if sys.platform == "darwin":
        try:
            return _darwin_environ(pid)
        except (OSError, ValueError):
            return None
    try:
        raw = open(f"/proc/{pid}/environ", "rb").read()
    except OSError:
        return None
    env = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def _ancestors(table: "list[dict]", pid: int) -> "set[int]":
    parent = {row["pid"]: row["ppid"] for row in table}
    seen = set()
    while pid and pid not in seen:
        seen.add(pid)
        pid = parent.get(pid, 0)
    return seen


def token_processes(token: str, *, table=None, exclude=()) -> "list[dict]":
    """The live processes of this user carrying WA_RUN_TOKEN=token, their
    descendants and the other members of their process groups - never this
    process or one of its ancestors (the runner itself). Each row says how
    it matched (`matched`: the token, `descendant` or `group`)."""
    if not token:
        return []
    table = process_table() if table is None else table
    skip = _ancestors(table, os.getpid()) | {int(p) for p in exclude}
    uid = os.getuid()
    rows = [r for r in table
            if r["uid"] == uid and r["pid"] not in skip and r["pid"] > 1]
    found = {}
    for row in rows:
        env = environ(row["pid"])
        if env is not None and env.get(TOKEN_VAR) == token:
            found[row["pid"]] = dict(row, matched=TOKEN_VAR)
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["pid"] in found:
                continue
            how = ("descendant" if row["ppid"] in found else
                   "group" if row["pgid"] in found else None)
            if how:
                found[row["pid"]] = dict(row, matched=how)
                changed = True
    return sorted(found.values(), key=lambda r: r["pid"])


def _append(path, row: dict) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def reap(token: str, record: str, *, teardown=None, by="wa_procs.reap",
         rounds: int = 5, exclude=()) -> dict:
    """Record, then SIGKILL, every process carrying the token; repeat until
    none is left or `rounds` scans ran."""
    killed, survivors = [], []
    for _round in range(max(1, rounds)):
        found = token_processes(token, exclude=exclude)
        if not found:
            survivors = []
            break
        for row in found:
            entry = {"pid": row["pid"], "ppid": row["ppid"], "pgid": row["pgid"],
                     "start": row["start"], "signal": int(signal.SIGKILL),
                     "matched": row["matched"], "t": time.time(), "by": by}
            _append(record, entry)
            _append(teardown, {"pid": row["pid"], "signal": int(signal.SIGKILL),
                               "group": False, "t": entry["t"], "by": by})
            try:
                os.kill(row["pid"], signal.SIGKILL)
                killed.append(row["pid"])
            except OSError:
                pass
        time.sleep(0.2)
        survivors = [r["pid"] for r in token_processes(token, exclude=exclude)]
    return {"token": token, "killed": killed, "survivors": survivors}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--token", required=True)
    r = sub.add_parser("reap")
    r.add_argument("--token", required=True)
    r.add_argument("--record", required=True)
    r.add_argument("--teardown")
    r.add_argument("--by", default="wa_procs.reap")
    r.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args(argv)
    if args.cmd == "scan":
        print(json.dumps(token_processes(args.token)))
        return 0
    result = reap(args.token, args.record, teardown=args.teardown, by=args.by,
                  rounds=args.rounds)
    print(json.dumps(result))
    return 0 if not result["survivors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
