#!/usr/bin/env python3
"""The shell holder: the parent and only reaper of one piece of shell work (#1087).

Bash reaps a background job asynchronously in its SIGCHLD handler, so a shell
script can neither hold a child unreaped nor make a check-then-signal sound:
between `kill -0 "$pid"` and `kill -TERM "$pid"` the job can be reaped and its
number handed to an unrelated process. A script therefore never signals a
process number. Every background process it later stops or resumes runs under
this holder instead, which is that work's parent and holds it unreaped until it
has finished signalling it:

    _lib_hold.py --control DIR --parent PID --scope child|group
                 [--grace S] [--kill-wait S] -- ARGV...

Standard library only, and it imports nothing from the runner modules or any
other private module, so it runs unchanged on Darwin, on the Linux CI lanes and
in the public tree. `bin/_lib-hold.sh` is its shell interface.

THE HOLD. While a parent holds its child `W` unreaped (alive or a zombie), pid
`W` is in use, so no process other than `W` can lead a process group `W`, and
the only group `W` that can exist is the one `W` created. Every signal this
holder sends names `W` while it still holds it: `kill(W, ...)` and, with group
scope, `killpg(W, ...)`. It observes `W`'s exit WITHOUT reaping it (kqueue
`EVFILT_PROC`/`NOTE_EXIT` on Darwin, `os.waitid(..., WNOWAIT)` on Linux) and
reaps it exactly once, after its last signal. A platform with neither refuses
to start the work (exit 4) rather than fall back to a reaping wait. The holder
never calls `poll()`, `wait()` or `communicate()` on the work's handle; the
only other children it starts are `ps` listings, waited by their own pid.

SCOPES. With `--scope child` the work stays in the holder's group and only `W`
itself is signalled: SIGTERM, up to `--grace` seconds, SIGKILL, up to
`--kill-wait` seconds. With `--scope group` the work calls `setpgid(0, 0)`
before exec and leads a new group `W` in the launching shell's session, and
the settlement, after `W`'s exit or on a stop, always signals the whole group:
SIGTERM to the group and (while it runs) to `W` by pid, a grace that ends early
once the emptiness check below passes, then SIGKILL rounds 0.1 s apart until
that check passes after a round or the kill wait expires. Group scope is used
only by `bin/cctally-test-all` (its harness pool and its pytest legs); every
other caller uses child scope, so no group-scope holder runs inside another's
work.

EMPTINESS. An ordinary process listing never decides that a group is empty: a
member can fork into an already-scanned pid range and exit while the scan
runs. The check, once `W` has exited:

* Darwin: `killpg(W, 0)`. ESRCH means group `W` no longer exists, and it
  cannot be re-created while `W` is held except by `W`, which has exited:
  complete (the leader that moved itself to another group is the ordinary
  case). EPERM means the group holds only zombies, or a live member this
  holder may not signal: complete only when a `ps` listing of the group shows
  no non-zombie member, otherwise incomplete with those members as survivors.
  Success means a member it can signal is alive: another round.
* Linux, where `killpg` reports success for a zombie-only group: a `/proc`
  census. A member of group `W` other than `W` is dead only when every one of
  its `/proc/<pid>/task/*/stat` lines shows `Z` or `X` (a zombie thread-group
  leader can keep a live thread); a vanished `/proc/<pid>` is gone; anything
  unreadable is present. The census decides only after a SIGKILL round, which
  freezes membership (Linux refuses a fork while a group-wide signal is
  pending); before the first round it only shortens the grace.

`ps` runs only to name survivors and for the Darwin EPERM case, always as this
holder's own child, in the holder's group and never in `W`.

CONTROL. Every 0.1 s the holder checks `W`'s exit, `DIR/stop`, its own SIGTERM,
SIGINT and SIGHUP (caught with a handler that only sets a flag, so the work
gets the default dispositions back at exec), its parent still being `PID` (a
launcher that is gone is a stop), and `DIR/cont`, on which it sends SIGCONT to
`W` (and its group with group scope) and removes the file. If its parent is not
`PID` at start it starts nothing and exits 3.

STATUS. `DIR/status`, written through a temporary file and a rename, one key
per line, keys exactly: `started, pid, reaped, status_code, rc, signal,
stopped, termed, killed, complete, survivors`. `status_code` is the work's
status as bash reports it (the exit code, or 128 + the signal), null when `W`
was not reaped; the holder exits with it, or 137 when `W` was sent SIGKILL and
not reaped, 3 when it started nothing because its launcher was gone, 4 when it
could not start the work.

`CCTALLY_HOLD_TRACE`, a private test variable, names a file to which the holder
appends one JSON line per signal call: `[call, target, signal, result]`.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import select
import signal
import subprocess
import sys
import time

POLL_S = 0.1
KILL_ROUND_S = 0.1
DEFAULT_GRACE_S = 5.0
DEFAULT_KILL_WAIT_S = 5.0
PS_TIMEOUT_S = 5.0

EXIT_NOT_STARTED = 3
EXIT_HOLDER_FAILED = 4
EXIT_UNREAPED = 137

STATUS_KEYS = (
    "started", "pid", "reaped", "status_code", "rc", "signal",
    "stopped", "termed", "killed", "complete", "survivors",
)
TRACE_ENV = "CCTALLY_HOLD_TRACE"


# ---- signal calls ----------------------------------------------------------

def tracer(path):
    """A trace callable appending `[call, target, signal, result]` lines to
    ``path``, or None when ``path`` is empty."""
    if not path:
        return None

    def trace(call, target, sig, result):
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps([call, int(target), int(sig), result]) + "\n")
        except OSError:
            pass
    return trace


def send(fn, call, target, sig, trace=None):
    """Make one signal call and return its result: ``"ok"`` or the errno name."""
    try:
        fn(target, sig)
    except OSError as exc:
        result = errno.errorcode.get(exc.errno, str(exc.errno))
    else:
        result = "ok"
    if trace is not None:
        trace(call, target, sig, result)
    return result


# ---- non-reaping exit waits ------------------------------------------------

class KqueueWaiter:
    """Darwin: `EVFILT_PROC`/`NOTE_EXIT` on ``pid``. Registration on a child
    that has already exited (an unreaped zombie) reports ESRCH, which is an
    exit too. Never reaps."""

    def __init__(self, pid, kqueue=None, kevent=None):
        kqueue = kqueue or select.kqueue
        kevent = kevent or select.kevent
        self._exited = False
        self._kq = kqueue()
        event = kevent(pid, filter=select.KQ_FILTER_PROC,
                       flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                       fflags=select.KQ_NOTE_EXIT)
        try:
            self._kq.control([event], 0, 0)
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                self._kq.close()
                raise
            self._exited = True

    def exited(self, timeout=0.0):
        if not self._exited:
            for event in self._kq.control(None, 1, timeout):
                if event.fflags & select.KQ_NOTE_EXIT:
                    self._exited = True
        return self._exited

    def close(self):
        self._kq.close()


class WaitidWaiter:
    """Linux: `waitid(P_PID, pid, WEXITED | WNOWAIT | WNOHANG)`. WNOWAIT leaves
    the child waitable, so this never reaps. A thread-group leader is reported
    only once its whole thread group has exited."""

    def __init__(self, pid, waitid=None, sleep=time.sleep):
        self._pid = pid
        self._waitid = waitid or os.waitid
        self._sleep = sleep
        self._exited = False

    def _check(self):
        if not self._exited:
            result = self._waitid(os.P_PID, self._pid,
                                  os.WEXITED | os.WNOWAIT | os.WNOHANG)
            if result is not None and getattr(result, "si_pid", 0):
                self._exited = True
        return self._exited

    def exited(self, timeout=0.0):
        if self._check() or not timeout:
            return self._exited
        self._sleep(timeout)
        return self._check()

    def close(self):
        pass


def _system(system=None):
    return system or platform.system()


def exit_wait_supported(system=None):
    """Whether this platform has a non-reaping exit wait."""
    system = _system(system)
    if system == "Darwin":
        return hasattr(select, "kqueue") and hasattr(select, "KQ_NOTE_EXIT")
    if system == "Linux":
        return hasattr(os, "waitid") and hasattr(os, "WNOWAIT")
    return False


def exit_wait(pid, *, system=None, kqueue=None, kevent=None, waitid=None, sleep=time.sleep):
    """The platform's non-reaping exit waiter for ``pid``, or None (exit 4)."""
    system = _system(system)
    if system == "Darwin" and (kqueue is not None or exit_wait_supported(system)):
        return KqueueWaiter(pid, kqueue=kqueue, kevent=kevent)
    if system == "Linux" and (waitid is not None or exit_wait_supported(system)):
        return WaitidWaiter(pid, waitid=waitid, sleep=sleep)
    return None


# ---- group membership ------------------------------------------------------

def _ps_path():
    return "/bin/ps" if os.path.exists("/bin/ps") else "ps"


def ps_listing(group, *, run=subprocess.run):
    """Pids of the non-zombie processes in ``group``, from one `ps` run as this
    process's own child; None when the listing cannot be taken."""
    try:
        proc = run([_ps_path(), "-A", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   stdin=subprocess.DEVNULL, timeout=PS_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    members = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        if int(fields[1]) == group and "Z" not in fields[2]:
            members.append(int(fields[0]))
    return sorted(members)


def _read_text(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def _stat_fields(text):
    """`(state, pgrp)` from a `/proc/.../stat` line, or None when unparsable.
    The command name is parenthesized and may hold spaces or parentheses, so
    the fields are read after the LAST `)`."""
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 1:].split()
    if len(fields) < 3 or not fields[2].lstrip("-").isdigit():
        return None
    return fields[0], int(fields[2])


_GONE = (errno.ENOENT, errno.ESRCH)


def linux_census(group, *, exclude=None, proc="/proc", listdir=os.listdir, read=_read_text):
    """The live members of ``group`` other than ``exclude``, from `/proc`; None
    when `/proc` itself cannot be listed.

    A member is dead only when every `/proc/<pid>/task/*/stat` shows `Z` or
    `X`: a thread-group leader that exited (`Z` in its own stat) can keep a
    live thread. A vanished pid directory is gone; anything unreadable or
    unparsable counts as present, including a process whose group cannot be
    read at all.
    """
    try:
        names = listdir(proc)
    except OSError:
        return None
    live = set()
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == exclude:
            continue
        base = "%s/%s" % (proc, name)
        try:
            fields = _stat_fields(read(base + "/stat"))
        except OSError as exc:
            if exc.errno not in _GONE:
                live.add(pid)
            continue
        if fields is None:
            live.add(pid)
            continue
        if fields[1] != group:
            continue
        try:
            tids = listdir(base + "/task")
        except OSError as exc:
            if exc.errno not in _GONE:
                live.add(pid)
            continue
        for tid in tids:
            try:
                task = _stat_fields(read("%s/task/%s/stat" % (base, tid)))
            except OSError as exc:
                if exc.errno in _GONE:
                    continue
                live.add(pid)
                break
            if task is None or task[0] not in ("Z", "X", "x"):
                live.add(pid)
                break
    return live


# ---- settlement ------------------------------------------------------------

def settle_group(w, *, exited, killpg, kill, census, clock, sleep, grace, kill_wait,
                 trace=None, system=None, listing=None, poll=POLL_S, round_s=KILL_ROUND_S):
    """Settle held group ``w``; return the status fields it decides:
    ``termed``, ``killed``, ``complete`` and ``survivors``.

    ``exited()`` reports (without reaping) whether ``w`` has exited;
    ``killpg``/``kill`` make the signal calls; ``census()`` is the Linux
    `/proc` census (a set of live members other than ``w``, None when
    unreadable); ``listing()`` is the `ps` listing of the group's non-zombie
    members (None when it fails). Every call names ``w``.
    """
    system = _system(system)
    out = {"termed": False, "killed": False, "complete": False, "survivors": []}

    def deliver(sig):
        send(killpg, "killpg", w, sig, trace)
        if not exited():
            send(kill, "kill", w, sig, trace)

    def empty():
        if not exited():
            return False
        if system == "Darwin":
            result = send(killpg, "killpg", w, 0, trace)
            if result == "ESRCH":
                return True
            if result == "EPERM":
                members = listing() if listing is not None else None
                if members:
                    out["survivors"] = sorted(members)
                return members is not None and not members
            return False
        live = census()
        return live is not None and not live

    deliver(signal.SIGTERM)
    out["termed"] = True
    end = clock() + grace
    while clock() < end and not empty():
        sleep(poll)
    end = clock() + kill_wait
    while True:
        deliver(signal.SIGKILL)
        out["killed"] = True
        if empty():
            out["complete"] = True
            out["survivors"] = []
            break
        if clock() >= end:
            break
        sleep(round_s)
    if not out["complete"]:
        members = listing() if listing is not None else None
        if members is not None:
            out["survivors"] = sorted(members)
    return out


def settle_child(w, *, exited, kill, clock, sleep, grace, kill_wait, trace=None, poll=POLL_S):
    """Stop held child ``w`` (it alone): SIGTERM, up to ``grace``, SIGKILL, up
    to ``kill_wait``. ``complete`` is whether ``w`` exited."""
    out = {"termed": False, "killed": False, "complete": False, "survivors": []}
    if not exited():
        send(kill, "kill", w, signal.SIGTERM, trace)
        out["termed"] = True
        end = clock() + grace
        while not exited() and clock() < end:
            sleep(poll)
    if not exited():
        send(kill, "kill", w, signal.SIGKILL, trace)
        out["killed"] = True
        end = clock() + kill_wait
        while not exited() and clock() < end:
            sleep(poll)
    out["complete"] = bool(exited())
    return out


# ---- status ----------------------------------------------------------------

def blank_status():
    return {
        "started": False, "pid": None, "reaped": False, "status_code": None,
        "rc": None, "signal": None, "stopped": False, "termed": False,
        "killed": False, "complete": False, "survivors": [],
    }


def status_text(status):
    """One key per line, so `bin/_lib-hold.sh` reads it without a parser."""
    lines = ['"%s": %s' % (key, json.dumps(status[key])) for key in STATUS_KEYS]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def publish(control, status):
    target = os.path.join(control, "status")
    tmp = os.path.join(control, ".status.%d.tmp" % os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(status_text(status))
        os.replace(tmp, target)
    except OSError as exc:
        sys.stderr.write("_lib_hold: could not publish %s: %s\n" % (target, exc.strerror))


def decode_wait_status(status):
    """`(rc, signal, status_code)` with bash's normalization."""
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        return None, sig, 128 + sig
    if os.WIFEXITED(status):
        rc = os.WEXITSTATUS(status)
        return rc, None, rc
    return None, None, None


def exit_code(status):
    if not status["started"]:
        return EXIT_NOT_STARTED
    if status["status_code"] is not None:
        return status["status_code"]
    return EXIT_UNREAPED


# ---- the holder ------------------------------------------------------------

def _parse(argv):
    parser = argparse.ArgumentParser(prog="_lib_hold.py", add_help=True)
    parser.add_argument("--control", required=True)
    parser.add_argument("--parent", required=True, type=int)
    parser.add_argument("--scope", required=True, choices=("child", "group"))
    parser.add_argument("--grace", type=float, default=DEFAULT_GRACE_S)
    parser.add_argument("--kill-wait", type=float, default=DEFAULT_KILL_WAIT_S)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.argv and args.argv[0] == "--":
        args.argv = args.argv[1:]
    if not args.argv:
        parser.error("no work to run after --")
    return args


def _consume(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    except OSError:
        return os.path.exists(path)
    return True


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    flagged = []

    def _flag(signum, _frame):
        flagged.append(signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, _flag)

    control = args.control
    status = blank_status()
    if os.getppid() != args.parent:
        status["complete"] = True
        publish(control, status)
        return EXIT_NOT_STARTED
    if not exit_wait_supported():
        sys.stderr.write("_lib_hold: no non-reaping exit wait on %s; not starting\n"
                         % platform.system())
        status["complete"] = True
        publish(control, status)
        return EXIT_HOLDER_FAILED

    group = args.scope == "group"
    # An ignored SIGCHLD (inherited through exec on Linux) makes the kernel
    # reap the work on its own: the reap below would fail, and the number it
    # held could be another process's by the time anything signals it.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        work = subprocess.Popen(args.argv, close_fds=True,
                                preexec_fn=(lambda: os.setpgid(0, 0)) if group else None)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("_lib_hold: could not start %s: %s\n" % (args.argv[0], exc))
        status["complete"] = True
        publish(control, status)
        return EXIT_HOLDER_FAILED

    # `work` stays referenced until the reap below: an unreferenced handle with
    # no return code joins subprocess's `_active` list, which the next Popen
    # (a `ps` listing) would poll, and that poll would reap `W` behind the hold.
    w = work.pid
    status["started"] = True
    status["pid"] = w
    trace = tracer(os.environ.get(TRACE_ENV, ""))
    waiter = None
    reaped = False
    try:
        waiter = exit_wait(w)

        def exited():
            return waiter.exited()

        def wait_or_sleep(seconds):
            if waiter.exited():
                time.sleep(seconds)
            else:
                waiter.exited(timeout=seconds)

        stop_file = os.path.join(control, "stop")
        cont_file = os.path.join(control, "cont")
        while not waiter.exited():
            if flagged or os.path.exists(stop_file) or os.getppid() != args.parent:
                status["stopped"] = True
                break
            if _consume(cont_file):
                send(os.kill, "kill", w, signal.SIGCONT, trace)
                if group:
                    send(os.killpg, "killpg", w, signal.SIGCONT, trace)
            waiter.exited(timeout=POLL_S)

        if group:
            settled = settle_group(
                w, exited=exited, killpg=os.killpg, kill=os.kill,
                census=lambda: linux_census(w, exclude=w),
                listing=lambda: ps_listing(w),
                clock=time.monotonic, sleep=wait_or_sleep,
                grace=args.grace, kill_wait=args.kill_wait, trace=trace)
        elif status["stopped"]:
            settled = settle_child(
                w, exited=exited, kill=os.kill, clock=time.monotonic,
                sleep=wait_or_sleep, grace=args.grace, kill_wait=args.kill_wait,
                trace=trace)
        else:
            settled = {"termed": False, "killed": False, "complete": True, "survivors": []}
        status.update(settled)

        if waiter.exited():
            _, wait_status = os.waitpid(w, 0)
            reaped = True
            work.returncode = os.WEXITSTATUS(wait_status) if os.WIFEXITED(wait_status) else -1
            status["reaped"] = True
            status["rc"], status["signal"], status["status_code"] = decode_wait_status(wait_status)
        else:
            status["complete"] = False
    except BaseException:
        if not reaped:
            # Still held, so the number is still the work's.
            send(os.kill, "kill", w, signal.SIGKILL, trace)
        raise
    finally:
        if waiter is not None:
            waiter.close()
    publish(control, status)
    return exit_code(status)


if __name__ == "__main__":
    sys.exit(main())
