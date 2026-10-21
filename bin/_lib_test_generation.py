"""The suite generation: one owned, marked temp root per aggregator run (#963, spec 5.4-5.5).

bin/cctally-test-all claims a generation ``ctg-<8 hex>`` beneath the TMPDIR it
inherits, before admission, and every pytest allocation of the run lives in it:
the main leg's ``--basetemp m``, the benchmark leg's ``--basetemp b``, the
``PYTEST_DEBUG_TEMPROOT`` ``t`` that implicit nested sessions use, and the
temporary log directory. The marker ``owner.json`` binds the generation to its
directory (device and inode), its UID and token, its owner process and that
process's start identity, the runner reservation where one exists, every
process group the run started, and a dedicated random descendant token
``cctg.<32 hex>`` that the aggregator appends to the inherited
``CCTALLY_SUITE_GENERATIONS``, so every descendant carries its own run's token
and every enclosing run's.

On success the aggregator removes the generation, through bin/_lib_test_temp.py,
before the runtime budget freezes the wall (J9), and only once ``reap`` has
established every recorded group gone. On failure, TERM or INT it reaps and
removes the generation too, once every recorded group is gone and
``descendants`` finds no other process carrying its token; otherwise it
releases obstructive metadata, records the terminal state and keeps the
generation for RETENTION_SECONDS (U5). A run killed with SIGKILL records
nothing: a later run's sweep records when it first established that ownership
ended, and the retention runs from then.

The sweep never trusts a bare PID, an age or an unlocked marker. A generation
is eligible only when its owner is absent or its start identity differs, no
recorded process group survives, no visible live process carries its
descendant token, and its reservation, where recorded, is no longer current;
an unknown answer keeps it. Descendants are tracked three ways: by the
recorded process groups, by the runner reservation where one exists (it stays
current while an OBSERVED detached session lives), and by the inherited token
in the environments of this user's processes, scanned once per sweep (``ps
-E`` on macOS, ``/proc/<pid>/environ`` on Linux). A scanner that cannot read
even its own environment preserves every generation that has a token. The
operator-accepted residual (963-CD-G3, esc-db01a377fca6) is a detached
descendant whose environment macOS hides (measured on macOS 27: Apple system
binaries such as /bin/sh and /usr/libexec/*, and argv-rewriting processes;
only 45 of 617 same-user processes showed an environment, the venv python3
among them) and that also escaped the recorded
groups and the reservation's 30-second ancestry sampling.

It reads only ``ctg-<8 hex>`` directories of this user whose marker matches
them, and never touches legacy ``pytest-*`` or ``garbage-*`` directories or
anything unmarked. Claims and sweeps are serialized by an exclusive flock on
the namespace directory itself, so nothing but generations is ever created
beneath TMPDIR.

Stdlib only and Python 3.9-parseable, like the kernel it calls.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time

import _lib_test_temp as T

NAME_RE = re.compile(r"^ctg-[0-9a-f]{8}$")
MARKER = "owner.json"
RETENTION_SECONDS = 86400
TERMINAL_STATES = ("failed", "interrupted")
SCHEMA_VERSION = 1
RESERVATION_ROOT_ENV = "CCTALLY_RUNNER_RESERVATION_ROOT"
RESERVATION_TOKEN_ENV = "_CCTALLY_RUNNER_RESERVATION_TOKEN"
#: The inherited, space-separated descendant tokens of every enclosing run.
DESCENDANT_ENV = "CCTALLY_SUITE_GENERATIONS"
#: Set only in the scanner's own environment: seeing it proves the scan reads environments.
PROBE_ENV = "CCTALLY_SUITE_ENV_PROBE"
DESCENDANT_RE = re.compile(r"cctg\.[0-9a-f]{32}")
SCAN_TIMEOUT_SECONDS = 20
PREFIX = "cctally-test-all: "


def _iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class Facts(object):
    """What may be known about processes. Each answer is definite or None:
    an inability to see processes preserves the root (spec 5.5)."""

    def process_start(self, pid):
        """The start identity of ``pid``; "" when it is absent; None when unknown."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ""
        except PermissionError:
            pass
        except OSError:
            return None
        env = dict(os.environ, LC_ALL="C", TZ="UTC")
        try:
            proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], env=env,
                                  capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        text = " ".join(proc.stdout.split())
        if proc.returncode == 0 and text:
            return text
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ""
        except OSError:
            return None
        return None

    def group_alive(self, pgid):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True

    def reservation_current(self, root, token):
        try:
            import pathlib
            import _lib_runner_reservation as R
        except ImportError:
            return None
        try:
            doc, status = R.read_owner(pathlib.Path(root))
        except Exception:
            return None
        if status == "absent":
            return False
        if status != "observed" or not isinstance(doc, dict):
            return None
        if doc.get("token") != token:
            return False
        return doc.get("state") in ("admitted", "active")

    def descendant_tokens(self):
        """Every descendant token in the visible environment of a live process
        of this user; None when the scanner cannot read even its own."""
        probe = os.urandom(16).hex()
        if sys.platform == "darwin":
            return _ps_tokens(probe)
        if sys.platform.startswith("linux"):
            return _linux_tokens(probe)
        return None

    def descendant_table(self):
        """``{pid: (ppid, tokens)}`` for every visible process, with the
        descendant tokens in this user's visible environments; None when the
        scanner cannot read even its own (the same scan, per process)."""
        probe = os.urandom(16).hex()
        if sys.platform == "darwin":
            return _ps_table(probe)
        if sys.platform.startswith("linux"):
            return _linux_table(probe)
        return None


_LISTING_ROW = re.compile(r"^\s*(\d+)\s+(\d+)(?:\s(.*))?$")


def _table_in_listing(text, probe):
    """``{pid: (ppid, tokens)}`` from a ``ps -E -o pid=,ppid=,command=``
    listing, or None unless it shows the scanner's own probe (as
    _tokens_in_listing). A line that does not start a row continues the
    previous one (an argument or value with a newline in it); one before any
    row is attributed to pid 0, which no caller excludes."""
    if "%s=%s" % (PROBE_ENV, probe) not in text:
        return None
    table, current = {}, 0
    for line in text.splitlines():
        row = _LISTING_ROW.match(line)
        if row:
            current = int(row.group(1))
            table[current] = (int(row.group(2)), set())
            rest = row.group(3) or ""
        else:
            table.setdefault(current, (0, set()))
            rest = line
        table[current][1].update(DESCENDANT_RE.findall(rest))
    return table


def _ps_table(probe):
    env = {"PATH": os.environ.get("PATH") or "/bin:/usr/bin", "LC_ALL": "C", PROBE_ENV: probe}
    try:
        proc = subprocess.run(["ps", "-E", "-ww", "-A", "-o", "pid=,ppid=,command="], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=SCAN_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _table_in_listing(proc.stdout.decode("utf-8", "replace"), probe)


def _proc_table(root, uid, probe_pid, probe):
    """``{pid: (ppid, tokens)}`` from ``<root>/<pid>/stat`` (every process) and
    ``<root>/<pid>/environ`` (only those ``uid`` owns), or None unless the probe
    process's environment shows ``probe``. An unreadable stat gives ppid 0; a
    process that vanishes or refuses its environment carries no token."""
    pattern = re.compile(DESCENDANT_RE.pattern.encode("ascii"))
    try:
        with open(os.path.join(root, str(probe_pid), "environ"), "rb") as fh:
            if ("%s=%s" % (PROBE_ENV, probe)).encode("ascii") not in fh.read():
                return None
        names = os.listdir(root)
    except OSError:
        return None
    table = {}
    for name in names:
        if not name.isdigit():
            continue
        base = os.path.join(root, name)
        ppid = 0
        try:
            with open(os.path.join(base, "stat")) as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            pass
        tokens = set()
        try:
            if os.stat(base).st_uid == uid:
                with open(os.path.join(base, "environ"), "rb") as fh:
                    tokens = set(m.decode("ascii") for m in pattern.findall(fh.read()))
        except OSError:
            pass
        table[int(name)] = (ppid, tokens)
    return table


def _linux_table(probe):
    """_linux_tokens' probe child, read through _proc_table."""
    try:
        child = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 env={"PATH": os.environ.get("PATH") or "/bin:/usr/bin",
                                      PROBE_ENV: probe})
    except OSError:
        return None
    try:
        return _proc_table("/proc", os.geteuid(), child.pid, probe)
    finally:
        try:
            child.stdin.close()
            child.wait(timeout=SCAN_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            child.kill()
            child.wait()


def _tokens_in_listing(text, probe):
    """The tokens in a ``ps -E`` listing, or None unless the listing shows the
    scanner's own probe: a sandbox that hides every environment must not read
    as "no descendant". Processes whose environment macOS hides are simply
    absent (the accepted residual)."""
    if "%s=%s" % (PROBE_ENV, probe) not in text:
        return None
    return set(DESCENDANT_RE.findall(text))


def _ps_tokens(probe):
    env = {"PATH": os.environ.get("PATH") or "/bin:/usr/bin", "LC_ALL": "C", PROBE_ENV: probe}
    try:
        proc = subprocess.run(["ps", "-E", "-ww", "-A", "-o", "pid=,command="], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=SCAN_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _tokens_in_listing(proc.stdout.decode("utf-8", "replace"), probe)


def _proc_tokens(root, uid, probe_pid, probe):
    """The tokens in ``<root>/<pid>/environ`` of every process ``uid`` owns,
    or None unless the probe process's environment shows ``probe``. A process
    that vanishes or refuses the read is skipped."""
    def environ(pid):
        with open(os.path.join(root, str(pid), "environ"), "rb") as fh:
            return fh.read()

    pattern = re.compile(DESCENDANT_RE.pattern.encode("ascii"))
    try:
        if ("%s=%s" % (PROBE_ENV, probe)).encode("ascii") not in environ(probe_pid):
            return None
        names = os.listdir(root)
    except OSError:
        return None
    found = set()
    for name in names:
        if not name.isdigit():
            continue
        try:
            if os.stat(os.path.join(root, name)).st_uid != uid:
                continue
            data = environ(name)
        except OSError:
            continue
        found.update(m.decode("ascii") for m in pattern.findall(data))
    return found


def _linux_tokens(probe):
    """/proc/<pid>/environ is the environment a process was EXECUTED with, so
    the probe lives in a child started for it, never in os.environ."""
    try:
        child = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 env={"PATH": os.environ.get("PATH") or "/bin:/usr/bin",
                                      PROBE_ENV: probe})
    except OSError:
        return None
    try:
        return _proc_tokens("/proc", os.geteuid(), child.pid, probe)
    finally:
        try:
            child.stdin.close()
            child.wait(timeout=SCAN_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            child.kill()
            child.wait()


class _Namespace(object):
    """An exclusive flock on the namespace directory itself; no lock file is created."""

    def __init__(self, tmpdir):
        self.fd = os.open(tmpdir, os.O_RDONLY)

    def __enter__(self):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(self.fd)
            raise
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
        return False


def _well_formed(doc):
    groups, starts, res = doc.get("groups"), doc.get("groupStarts"), doc.get("reservation")
    return (type(doc.get("ownerPid")) is int and doc["ownerPid"] > 0
            and isinstance(doc.get("ownerStart"), str)
            and isinstance(doc.get("state"), str)
            and isinstance(groups, list) and all(type(g) is int and g > 1 for g in groups)
            and isinstance(starts, dict)
            and (res is None or isinstance(res, dict))
            and (doc.get("descendantToken") is None or isinstance(doc["descendantToken"], str))
            and all(doc.get(k) is None or isinstance(doc.get(k), (int, float))
                    for k in ("terminalAt", "ownershipEndedAt")))


def _read_marker(gen_dir):
    fd = os.open(os.path.join(gen_dir, MARKER), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict) or not _well_formed(doc):
        raise ValueError("malformed ownership marker")
    return doc


def _write_marker(gen_dir, doc):
    tmp = os.path.join(gen_dir, ".%s.%d.tmp" % (MARKER, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, os.path.join(gen_dir, MARKER))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _matches(doc, name, st, uid):
    return (doc.get("schemaVersion") == SCHEMA_VERSION and doc.get("token") == name[4:]
            and doc.get("uid") == uid and doc.get("dev") == st.st_dev
            and doc.get("ino") == st.st_ino)


def claim(tmpdir, owner_pid, *, facts=None, now=None, environ=None):
    """Create and mark a new generation beneath ``tmpdir``; return its path."""
    facts = facts or Facts()
    environ = os.environ if environ is None else environ
    tmpdir = os.path.abspath(tmpdir)
    start = facts.process_start(owner_pid)
    if not start:
        raise OSError("cannot establish the start identity of process %d" % owner_pid)
    root = environ.get(RESERVATION_ROOT_ENV) or ""
    token = environ.get(RESERVATION_TOKEN_ENV) or ""
    reservation = {"root": root, "token": token} if root and token else None
    with _Namespace(tmpdir):
        while True:
            gen_dir = os.path.join(tmpdir, "ctg-" + os.urandom(4).hex())
            try:
                os.mkdir(gen_dir, 0o700)
                break
            except FileExistsError:
                continue
        try:
            os.mkdir(os.path.join(gen_dir, "t"), 0o700)
            st = os.lstat(gen_dir)
            _write_marker(gen_dir, {
                "schemaVersion": SCHEMA_VERSION, "token": os.path.basename(gen_dir)[4:],
                "uid": os.geteuid(), "dev": st.st_dev, "ino": st.st_ino,
                "ownerPid": owner_pid, "ownerStart": start, "reservation": reservation,
                "groups": [], "groupStarts": {}, "state": "claimed",
                "claimedAt": time.time() if now is None else now,
                "terminalAt": None, "ownershipEndedAt": None, "reclaimer": None,
                "descendantToken": "cctg." + os.urandom(16).hex()})
        except BaseException:
            T.remove(gen_dir)
            raise
    return gen_dir


def add_group(gen_dir, pgid, *, facts=None):
    """Record a process group the run started, with its leader's start identity
    ("" when the leader had already exited, None when unknown)."""
    if type(pgid) is not int or pgid <= 1:
        raise ValueError("not a child process group: %r" % (pgid,))
    facts = facts or Facts()
    start = facts.process_start(pgid)
    gen_dir = os.path.abspath(gen_dir)
    with _Namespace(os.path.dirname(gen_dir)):
        doc = _read_marker(gen_dir)
        if pgid not in doc["groups"]:
            doc["groups"].append(pgid)
        doc["groupStarts"][str(pgid)] = start
        _write_marker(gen_dir, doc)


def _owner_state(facts, doc):
    """True: the owner runs. False: its ownership ended. None: unknown."""
    now = facts.process_start(doc["ownerPid"])
    if now is None:
        return None
    if now == "":
        return False
    return now == doc["ownerStart"]


def _group_state(facts, pgid, recorded):
    """True while the recorded group has members; False once it is gone (its
    number names another process now, or nothing answers); None when unknown.
    A group's number cannot be reissued while any member lives, so a leader
    that is absent while the group answers means the run's own survivors."""
    now = facts.process_start(pgid)
    if now is None:
        return None
    if now:
        if recorded is None:
            return None
        return now == recorded
    return facts.group_alive(pgid)


#: reap_groups' waits: SIGTERM's grace, then how long SIGKILL's effect is
#: awaited before a group still answering is reported (spec 5.4).
REAP_GRACE_SECONDS = 5.0
REAP_SETTLE_SECONDS = 5.0


def reap_groups(gen_dir, *, facts=None, kill=None, grace=None, settle=None):
    """Stop and reap the run's recorded groups (spec 5.4): TERM every one that
    is still this run's, KILL whatever outlives ``grace`` seconds, then wait up
    to ``settle`` seconds for each to be established gone.

    Returns ``[(pgid, why)]``, in recorded order, for every recorded group whose
    termination could not be established: one that still answers after
    SIGKILL, one whose state is unknown (never signalled, since its number may
    name another process now), and one that could not be signalled. Only an
    empty answer permits removing the generation. A group whose number another
    process now holds is not this run's and is ignored, as is the caller's own
    group, which ends only when the caller does."""
    facts = facts or Facts()
    kill = os.killpg if kill is None else kill
    grace = REAP_GRACE_SECONDS if grace is None else grace
    settle = REAP_SETTLE_SECONDS if settle is None else settle
    doc = _read_marker(os.path.abspath(gen_dir))
    own = os.getpgrp()
    order = [pg for pg in doc["groups"] if pg != own]
    unproven = {}

    def state(pg):
        return _group_state(facts, pg, doc["groupStarts"].get(str(pg)))

    def send(pgs, sig):
        sent = []
        for pg in pgs:
            try:
                kill(pg, sig)
            except ProcessLookupError:
                continue
            except OSError as exc:
                unproven[pg] = "it could not be signalled (%s)" % (exc.strerror or exc)
                continue
            sent.append(pg)
        return sent

    def wait(pgs, seconds):
        deadline = time.monotonic() + seconds
        while True:
            pgs = [pg for pg in pgs if state(pg) is not False]
            if not pgs or time.monotonic() >= deadline:
                return pgs
            time.sleep(0.1)

    live = []
    for pg in order:
        now = state(pg)
        if now is None:
            unproven[pg] = "its state is unknown"
        elif now:
            live.append(pg)
    remaining = wait(send(live, signal.SIGTERM), grace)
    for pg in wait(send(remaining, signal.SIGKILL), settle):
        unproven[pg] = "it survives SIGKILL" if state(pg) else "its state is unknown"
    return [(pg, unproven[pg]) for pg in order if pg in unproven]


def _remove_tree(gen_dir, ops=None):
    """Remove every entry but the marker through the kernel, then the marker,
    then the directory. The marker goes last, so a partial removal stays
    recognisable and retained.

    The generation's device is the reference for every child: each kernel walk
    takes its own root's device, so a filesystem mounted directly at
    <generation>/<child> would otherwise be walked and emptied."""
    res = T.Result()
    lstat = (ops or T.RealOps()).lstat
    try:
        gen_dev = lstat(gen_dir).st_dev
        names = sorted(os.listdir(gen_dir))
    except OSError as exc:
        res.findings.append(T.Finding(gen_dir, "error", "inspection error: %s" % (exc.strerror or exc)))
        res.left.append(gen_dir)
        return res
    for name in names:
        if name == MARKER:
            continue
        path = os.path.join(gen_dir, name)
        try:
            crossed = lstat(path).st_dev != gen_dev
        except FileNotFoundError:
            continue
        except OSError as exc:
            res.findings.append(T.Finding(path, "error", "inspection error: %s" % (exc.strerror or exc)))
            res.left.append(path)
            continue
        if crossed:
            res.findings.append(T.Finding(path, "device", "device crossing"))
            res.left.append(path)
            continue
        part = T.remove(path, ops=ops)
        res.findings.extend(part.findings)
        res.left.extend(part.left)
        if not part.removed and path not in part.left:
            res.left.append(path)
    if res.left or res.refusals:
        return res
    for step, target in ((os.unlink, os.path.join(gen_dir, MARKER)), (os.rmdir, gen_dir)):
        try:
            step(target)
        except FileNotFoundError:
            pass
        except OSError as exc:
            res.findings.append(T.Finding(target, "error", "repair refused: %s" % (exc.strerror or exc)))
            res.left.append(target)
            return res
    res.removed = not os.path.lexists(gen_dir)
    return res


def _shown(res):
    return res.refusals or [T.Finding(p, "error", "not removed") for p in res.left]


def finish(gen_dir, state, *, now=None, ops=None):
    """Release obstructive metadata, then record the terminal state (spec 5.4,
    failure steps 2 and 3). The generation stays."""
    if state not in TERMINAL_STATES:
        raise ValueError("state must be one of: %s" % ", ".join(TERMINAL_STATES))
    gen_dir = os.path.abspath(gen_dir)
    res = T.release(gen_dir, ops=ops)
    with _Namespace(os.path.dirname(gen_dir)):
        doc = _read_marker(gen_dir)
        doc["state"] = state
        doc["terminalAt"] = time.time() if now is None else now
        _write_marker(gen_dir, doc)
    return res


def remove_generation(gen_dir, *, ops=None, now=None, out=None):
    """The success path: remove this run's own generation. A refusal keeps it,
    marked failed, so U5's retention applies, and prints each refusal."""
    out = sys.stderr if out is None else out
    gen_dir = os.path.abspath(gen_dir)
    name = os.path.basename(gen_dir)
    with _Namespace(os.path.dirname(gen_dir)):
        try:
            doc = _read_marker(gen_dir)
            st = os.lstat(gen_dir)
        except (OSError, ValueError) as exc:
            out.write("%ssuite generation %s: unreadable ownership marker (%s)\n"
                      % (PREFIX, name[4:], exc))
            return False
        if not NAME_RE.match(name) or not _matches(doc, name, st, os.geteuid()):
            out.write("%ssuite generation %s: the ownership marker does not match the directory\n"
                      % (PREFIX, name[4:]))
            return False
        doc["state"] = "removing"
        _write_marker(gen_dir, doc)
    res = _remove_tree(gen_dir, ops=ops)
    if res.removed:
        return True
    for f in _shown(res):
        out.write("%ssuite generation %s: %s: %s\n" % (PREFIX, name[4:], f.detail, f.path))
    if os.path.isdir(gen_dir):
        with _Namespace(os.path.dirname(gen_dir)):
            doc["state"] = "failed"
            doc["terminalAt"] = time.time() if now is None else now
            _write_marker(gen_dir, doc)
    return False


def surviving_descendants(gen_dir, *, facts=None, self_pid=None):
    """The exiting run's check before it removes its own generation (test
    regime reset fix round, Review Focus 4): the pids, sorted, of every visible
    process other than the caller and its ancestors whose environment carries
    the generation's descendant token, the same token the sweep's scan looks
    for; [] when the marker records no token; None when environments cannot be
    inspected, which keeps the generation exactly as the sweep keeps it. The
    caller and its ancestors carry the token themselves and never count."""
    facts = facts or Facts()
    token = _read_marker(os.path.abspath(gen_dir)).get("descendantToken")
    if not token:
        return []
    table = facts.descendant_table()
    if table is None:
        return None
    pid = os.getpid() if self_pid is None else self_pid
    excluded = set()
    while pid > 0 and pid not in excluded:
        excluded.add(pid)
        pid = table.get(pid, (0,))[0]
    return sorted(p for p, (_, tokens) in table.items() if token in tokens and p not in excluded)


def _assess(gen_dir, name, facts, uid, stamp, descendants=None):
    """None when the entry vanished, else (verdict, reason, doc, clock, changed)
    with verdict "kept" or "eligible". ``descendants`` answers the sweep's one
    cached descendant scan."""
    descendants = descendants or facts.descendant_tokens
    try:
        st = os.lstat(gen_dir)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return ("kept", "no ownership marker", None, None, False)
    if st.st_uid != uid:
        return ("kept", "another user's generation", None, None, False)
    try:
        doc = _read_marker(gen_dir)
    except FileNotFoundError:
        return ("kept", "no ownership marker", None, None, False)
    except (OSError, ValueError):
        return ("kept", "unreadable ownership marker", None, None, False)
    if not _matches(doc, name, st, uid):
        return ("kept", "the ownership marker does not match the directory", None, None, False)
    if doc["state"] == "reclaiming":
        reclaimer = doc.get("reclaimer") or {}
        pid = reclaimer.get("pid")
        running = facts.process_start(pid) if type(pid) is int and pid > 0 else ""
        if running is None or (running and running == reclaimer.get("start")):
            return ("kept", "being reclaimed by a running process", doc, None, False)
    owner = _owner_state(facts, doc)
    if owner is None:
        return ("kept", "its owner's state is unknown", doc, None, False)
    if owner:
        return ("kept", "its owner is still running", doc, None, False)
    for pg in doc["groups"]:
        alive = _group_state(facts, pg, doc["groupStarts"].get(str(pg)))
        if alive is None:
            return ("kept", "a recorded process group's state is unknown", doc, None, False)
        if alive:
            return ("kept", "a recorded process group survives", doc, None, False)
    # 963-CD-G3: a detached descendant no group or reservation sample saw.
    token = doc.get("descendantToken")
    if token:
        seen = descendants()
        if seen is None:
            return ("kept", "descendant environments cannot be inspected", doc, None, False)
        if token in seen:
            return ("kept", "a descendant carrying its token survives", doc, None, False)
    reservation = doc.get("reservation")
    if reservation:
        current = facts.reservation_current(reservation.get("root", ""),
                                            reservation.get("token", ""))
        if current is None:
            return ("kept", "its runner reservation's state is unknown", doc, None, False)
        if current:
            return ("kept", "its runner reservation is still current", doc, None, False)
    changed = False
    if doc.get("terminalAt") is not None:
        # A terminal clock stands even when a reclaimer died mid-removal and
        # left the marker `reclaiming`: restamping would add another 24 hours.
        clock = doc["terminalAt"]
        shown = doc["state"] if doc["state"] in TERMINAL_STATES \
            else "reclamation interrupted, terminal"
        label = "%s at %s" % (shown, _iso(clock))
    else:
        if doc.get("ownershipEndedAt") is None:
            doc["ownershipEndedAt"] = stamp
            changed = True
        clock = doc["ownershipEndedAt"]
        label = "ownership ended at %s" % _iso(clock)
    if stamp < clock + RETENTION_SECONDS:
        return ("kept", "%s; retained until %s" % (label, _iso(clock + RETENTION_SECONDS)),
                doc, clock, changed)
    return ("eligible", None, doc, clock, changed)


def sweep(tmpdir, *, keep=None, reclaim=False, facts=None, now=None, out=None, uid=None):
    """Inspect the marked namespace beneath ``tmpdir``.

    Always records when a generation's ownership is first seen to have ended.
    With ``reclaim``, removes each eligible generation outside the lock and
    reports every generation it kept or reclaimed; without it, prints nothing.
    """
    facts = facts or Facts()
    out = sys.stderr if out is None else out
    stamp = time.time() if now is None else now
    uid = os.geteuid() if uid is None else uid
    tmpdir = os.path.abspath(tmpdir)
    keep = os.path.abspath(keep) if keep else None
    lines, claimed, scan = [], [], []

    def descendants():
        if not scan:                      # one process listing per sweep
            scan.append(facts.descendant_tokens())
        return scan[0]

    with _Namespace(tmpdir):
        for name in sorted(os.listdir(tmpdir)):
            gen_dir = os.path.join(tmpdir, name)
            if not NAME_RE.match(name) or gen_dir == keep:
                continue
            found = _assess(gen_dir, name, facts, uid, stamp, descendants)
            if found is None:
                continue
            verdict, reason, doc, clock, changed = found
            if verdict == "eligible" and reclaim:
                previous = dict(doc)
                doc["state"] = "reclaiming"
                doc["reclaimer"] = {"pid": os.getpid(),
                                    "start": facts.process_start(os.getpid())}
                _write_marker(gen_dir, doc)
                claimed.append((gen_dir, name[4:], clock, previous))
                continue
            if changed:
                _write_marker(gen_dir, doc)
            if verdict == "kept":
                lines.append("%skept suite generation %s: %s" % (PREFIX, name[4:], reason))
    for gen_dir, token, clock, previous in claimed:
        res = _remove_tree(gen_dir)
        if res.removed:
            lines.append("%sreclaimed suite generation %s (ended %s)" % (PREFIX, token, _iso(clock)))
            continue
        lines.append("%skept suite generation %s: removal refused: %s" % (
            PREFIX, token, "; ".join("%s: %s" % (f.detail, f.path) for f in _shown(res))))
        if os.path.isdir(gen_dir):
            with _Namespace(tmpdir):
                _write_marker(gen_dir, previous)
    if not reclaim:
        return []
    for line in lines:
        out.write(line + "\n")
    return lines


# --- the aggregator-owner environment family (#966(c), spec 4.3, J7) --------
#: Owner-only: they describe ONE bin/cctally-test-all invocation (its outcome
#: record, its run id, its evidence store and flags) and are removed at every
#: boundary that could hand them to another run.
OWNER_ONLY_PREFIXES = ("CCTALLY_TEST_ALL_", "CCTALLY_TEST_EVIDENCE_")
OWNER_ONLY_NAMES = frozenset({
    "CCTALLY_TEST_RUN_ID", "CCTALLY_TEST_EXTERNAL_INCOMPLETE",
    "CCTALLY_RUNNER_EVIDENCE_INDEX", "CCTALLY_REMOTE_DIR",
    "_CCTALLY_HARNESS_SCRUB",          # the scrub list itself
})
#: Deliberately inherited, by spec 4.3's preserved classes; the readers of each
#: are in the plan's population (c). A new control variable is added to one of
#: the two sets, and tests/test_owner_environment.py fails until it is.
DELIBERATELY_INHERITED = frozenset({
    # execution policy
    "CCTALLY_AGENTMEM_TEST_POLICY", "CCTALLY_AUTHORITATIVE_RUN", "CCTALLY_DOCTOR_REGENERATE",
    "CCTALLY_LINUX_MATRIX_RUN", "CCTALLY_PROGRESS_INTERVAL", "CCTALLY_PYTEST_DURATIONS",
    "CCTALLY_PYTEST_TIMEOUT", "CCTALLY_REMOTE_EXEC", "CCTALLY_TEST_LOCAL",
    # #965's test retention switch (bin/_cctally_config.py, bin/_lib_test_isolation.py):
    # the aggregator and every harness export it, and the isolation layer
    # refuses a covered launch that lacks it, so every child must inherit it.
    "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF",
    # parallelism
    "CCTALLY_TEST_JOBS", "CCTALLY_RELEASE_TEST_JOBS", "CCTALLY_OUTER_JOBS",
    "CCTALLY_INNER_JOBS", "CCTALLY_PYTEST_JOBS",
    # confinement and reservation
    "CCTALLY_RUNNER_STORE_ID", "CCTALLY_RUNNER_RESERVATION_ROOT",
    "_CCTALLY_RUNNER_RESERVATION_TOKEN", "_CCTALLY_RUNNER_RESERVATION_FD",
    # isolation
    "CCTALLY_DISABLE_DEV_AUTODETECT",
    # 963-CD-G3: every descendant must carry each enclosing run's generation token
    "CCTALLY_SUITE_GENERATIONS",
    # leg-owned output channels
    "CCTALLY_DURATIONS_PATH", "CCTALLY_DURATIONS_LEG", "CCTALLY_ESTATE_OBSERVED_DIR",
})


def is_owner_only(name):
    return name in OWNER_ONLY_NAMES or name.startswith(OWNER_ONLY_PREFIXES)


def owner_only_names(environ=None):
    """Every fixed owner-only name, plus every prefixed one present, sorted."""
    environ = os.environ if environ is None else environ
    return sorted(OWNER_ONLY_NAMES | set(n for n in environ if is_owner_only(n)))


def scrub_owner_env(env):
    """Remove every owner-only variable from ``env`` in place (``os.environ``
    included) and return it. Apply BEFORE a caller's explicit overrides."""
    for name in [n for n in env if is_owner_only(n)]:
        del env[name]
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(prog="_lib_test_generation.py")
    sub = parser.add_subparsers(dest="verb")
    p = sub.add_parser("claim")
    p.add_argument("--tmpdir", required=True)
    p.add_argument("--owner-pid", type=int, required=True)
    p = sub.add_parser("add-group")
    p.add_argument("--dir", required=True)
    p.add_argument("--pgid", type=int, required=True)
    p = sub.add_parser("reap")
    p.add_argument("--dir", required=True)
    p = sub.add_parser("finish")
    p.add_argument("--dir", required=True)
    p.add_argument("--state", choices=TERMINAL_STATES, required=True)
    p = sub.add_parser("remove")
    p.add_argument("--dir", required=True)
    p = sub.add_parser("descendants")
    p.add_argument("--dir", required=True)
    p = sub.add_parser("sweep")
    p.add_argument("--tmpdir", required=True)
    p.add_argument("--keep")
    p.add_argument("--reclaim", action="store_true")
    sub.add_parser("owner-env-names")
    args = parser.parse_args(argv)          # a usage error exits 2 here
    if args.verb is None:
        parser.print_usage(sys.stderr)
        return 2
    try:
        if args.verb == "claim":
            # The directory, then the descendant token the aggregator exports.
            gen_dir = claim(args.tmpdir, args.owner_pid)
            print(gen_dir)
            print(_read_marker(gen_dir)["descendantToken"])
        elif args.verb == "add-group":
            add_group(args.dir, args.pgid)
        elif args.verb == "reap":
            token = os.path.basename(os.path.abspath(args.dir))[4:]
            left = reap_groups(args.dir)
            for pg, why in left:
                sys.stderr.write("%ssuite generation %s: process group %d was not established "
                                 "as terminated: %s\n" % (PREFIX, token, pg, why))
            return 1 if left else 0
        elif args.verb == "finish":
            res = finish(args.dir, args.state)
            for f in res.refusals:
                sys.stderr.write("%ssuite generation: %s: %s\n" % (PREFIX, f.detail, f.path))
            return 1 if res.refusals else 0
        elif args.verb == "remove":
            return 0 if remove_generation(args.dir) else 1
        elif args.verb == "descendants":
            token = os.path.basename(os.path.abspath(args.dir))[4:]
            left = surviving_descendants(args.dir)
            if left is None:
                sys.stderr.write("%ssuite generation %s: descendant environments cannot be inspected\n"
                                 % (PREFIX, token))
                return 1
            for pid in left:
                sys.stderr.write("%ssuite generation %s: process %d outside the recorded process groups "
                                 "still carries its descendant token\n" % (PREFIX, token, pid))
            return 1 if left else 0
        elif args.verb == "owner-env-names":
            print(" ".join(owner_only_names()))
        else:
            sweep(args.tmpdir, keep=args.keep, reclaim=args.reclaim)
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write("%ssuite generation: %s failed: %s\n" % (PREFIX, args.verb, exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
