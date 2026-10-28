"""Release and remove an owned scratch tree without following links (#963, spec 5.1).

One traversal, three operations: ``inspect`` reports what would block deletion
and changes nothing; ``release`` makes the tree deletable without deleting it;
``remove`` deletes it.

The rules (spec 5.1, J3). ``lstat`` only, and a symlink is never followed:
removal unlinks the link itself. Directories are repaired top-down to
``mode | 0o700``; a regular file's mode is never changed, because a mode, like
a flag, belongs to the inode and a hard link shares it with a checkout. A
device crossing is reported before it is entered or modified. Only
UF_IMMUTABLE, UF_APPEND, SF_IMMUTABLE and SF_APPEND are cleared and every other
flag bit is kept; a flag is cleared on a non-directory only when it is singly
linked and owned by this user. Every mutation (flag, mode, unlink, rmdir) is
preceded by a fresh ``lstat`` that must show the same device, inode and file
type. A privileged or
unsupported flag fails visibly; there is no sudo and no fallback.

Installed with the runner supervisor (``bin/_lib_runner_client.py`` INSTALL_SET),
so it parses on Python 3.9 and imports nothing outside the standard library.
"""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field

UF_IMMUTABLE = 0x00000002
UF_APPEND = 0x00000004
SF_IMMUTABLE = 0x00020000
SF_APPEND = 0x00040000
BLOCKING_FLAGS = (("UF_IMMUTABLE", UF_IMMUTABLE), ("UF_APPEND", UF_APPEND),
                  ("SF_IMMUTABLE", SF_IMMUTABLE), ("SF_APPEND", SF_APPEND))
BLOCKING_MASK = UF_IMMUTABLE | UF_APPEND | SF_IMMUTABLE | SF_APPEND
REFUSAL_KINDS = frozenset({"device", "nlink", "owner", "identity", "privileged", "error"})


@dataclass(frozen=True)
class Finding:
    path: str
    kind: str      # mode | flag | device | nlink | owner | identity | privileged | error
    detail: str


@dataclass
class Result:
    findings: list = field(default_factory=list)
    left: list = field(default_factory=list)   # entries refused or not removable
    removed: bool = False                      # remove(): the root is gone

    @property
    def refusals(self):
        return [f for f in self.findings if f.kind in REFUSAL_KINDS]

    @property
    def ok(self):
        return not self.refusals and not self.left


# The real operations, bound once at import. A test that monkeypatches
# ``os.lstat`` (tests/test_write_attribution_tools.py does, to fake an
# unreadable probe) may still have it patched when a fixture releases its tree,
# because the monkeypatch fixture can be torn down after that fixture; the
# kernel must see the real filesystem regardless.
_LSTAT = os.lstat
_LISTDIR = os.listdir
_CHMOD = os.chmod
_CHFLAGS = getattr(os, "chflags", None)
_UNLINK = os.unlink
_RMDIR = os.rmdir
_GETEUID = os.geteuid


class RealOps(object):
    """The filesystem seam. Tests substitute facts the host cannot produce."""

    def lstat(self, path):
        return _LSTAT(path)

    def listdir(self, path):
        return _LISTDIR(path)

    def chmod_dir(self, path, mode):
        try:
            _CHMOD(path, mode, follow_symlinks=False)
        except NotImplementedError:
            # No lchmod on Linux; the caller has just verified by lstat that
            # this name is a directory, never a link.
            _CHMOD(path, mode)

    def set_flags(self, path, flags):
        if _CHFLAGS is None:
            raise AttributeError("os.chflags is not available on this platform")
        _CHFLAGS(path, flags, follow_symlinks=False)

    def unlink(self, path):
        _UNLINK(path)

    def rmdir(self, path):
        _RMDIR(path)

    def uid(self):
        return _GETEUID()


def _flags(st):
    return getattr(st, "st_flags", 0) or 0


def _why(exc):
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


def _entry_findings(path, st, root_dev):
    if st.st_dev != root_dev:
        return [Finding(path, "device", "device crossing")]
    out = []
    if stat.S_ISDIR(st.st_mode) and st.st_mode & 0o700 != 0o700:
        out.append(Finding(path, "mode", "directory missing owner rwx (mode 0o%03o)"
                           % stat.S_IMODE(st.st_mode)))
    for name, bit in BLOCKING_FLAGS:
        if _flags(st) & bit:
            out.append(Finding(path, "flag", "deletion-blocking flag " + name))
    return out


def _top(root, ops, res):
    try:
        return ops.lstat(root)
    except FileNotFoundError:
        return None
    except OSError as exc:
        res.findings.append(Finding(root, "error", "inspection error: " + _why(exc)))
        return None


def _children(path, ops, res):
    """(name, lstat) pairs in sorted order, or None when the directory cannot be listed.

    A directory that vanished after its parent was listed has no children: a
    concurrent writer (git's detached auto-gc pruning objects/<xx>) removed it,
    and an absent entry cannot block deletion."""
    try:
        names = sorted(ops.listdir(path))
    except FileNotFoundError:
        return []
    except OSError as exc:
        res.findings.append(Finding(path, "error", "inspection error: " + _why(exc)))
        return None
    out = []
    for name in names:
        child = os.path.join(path, name)
        try:
            out.append((child, ops.lstat(child)))
        except FileNotFoundError:
            continue
        except OSError as exc:
            res.findings.append(Finding(child, "error", "inspection error: " + _why(exc)))
    return out


# _recheck's answer for an entry that no longer exists: nothing to mutate and
# nothing left behind, so neither a finding nor a refusal.
_GONE = object()


def _recheck(path, st, ops, res):
    """A fresh lstat of ``path`` that still shows the device, inode and file
    type the walk saw, else None with the refusal recorded, or _GONE when the
    entry has vanished. Every mutation (flag, mode, unlink, rmdir) is preceded
    by one (spec 5.1, J3)."""
    try:
        now = ops.lstat(path)
    except FileNotFoundError:
        return _GONE
    except OSError as exc:
        res.findings.append(Finding(path, "error", "repair refused: " + _why(exc)))
        return None
    if (now.st_dev, now.st_ino, stat.S_IFMT(now.st_mode)) != \
            (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode)):
        res.findings.append(Finding(path, "identity", "repair refused: entry changed during traversal"))
        return None
    return now


def _repair(path, st, ops, uid, res):
    """Make one entry deletable. False means refused: never descend into it.
    None means the entry vanished: there is nothing to repair or descend into."""
    blocking = _flags(st) & BLOCKING_MASK
    is_dir = stat.S_ISDIR(st.st_mode)
    needs_mode = is_dir and st.st_mode & 0o700 != 0o700
    if not blocking and not needs_mode:
        return True
    now = _recheck(path, st, ops, res)
    if now is _GONE:
        return None
    if now is None:
        return False
    if blocking:
        if now.st_uid != uid:
            res.findings.append(Finding(path, "owner", "repair refused: owned by uid %d" % now.st_uid))
            return False
        if not is_dir and now.st_nlink != 1:
            res.findings.append(Finding(path, "nlink", "repair refused: flagged inode has %d links"
                                        % now.st_nlink))
            return False
        try:
            ops.set_flags(path, _flags(now) & ~BLOCKING_MASK)
        except (OSError, AttributeError) as exc:   # AttributeError: no chflags here
            names = ", ".join(n for n, b in BLOCKING_FLAGS if blocking & b)
            res.findings.append(Finding(path, "privileged", "repair refused: cannot clear %s (%s)"
                                        % (names, _why(exc))))
            return False
    if needs_mode:
        try:
            ops.chmod_dir(path, stat.S_IMODE(now.st_mode) | 0o700)
        except OSError as exc:
            res.findings.append(Finding(path, "error", "repair refused: " + _why(exc)))
            return False
    return True


def inspect(root, *, ops=None):
    """Every finding under ``root``; changes nothing."""
    ops = ops or RealOps()
    root = os.fspath(root)
    res = Result()
    top = _top(root, ops, res)
    if top is None:
        return res
    stack = [(root, top)]
    while stack:
        path, st = stack.pop()
        found = _entry_findings(path, st, top.st_dev)
        res.findings.extend(found)
        if not stat.S_ISDIR(st.st_mode) or any(f.kind == "device" for f in found):
            continue
        kids = _children(path, ops, res)
        if kids:
            stack.extend(reversed(kids))
    return res


def _walk(root, ops, delete):
    ops = ops or RealOps()
    root = os.fspath(root)
    res = Result()
    top = _top(root, ops, res)
    if top is None:
        res.removed = delete and not res.findings
        return res
    uid = ops.uid()
    blocked = set()   # directories with something left below them

    def block_parent(path):
        if path != root:
            blocked.add(os.path.dirname(path))

    # (path, lstat, exiting): a directory is visited on entry (record, repair,
    # descend) and, for remove, again on exit (rmdir when nothing is left below).
    stack = [(root, top, False)]
    while stack:
        path, st, exiting = stack.pop()
        if exiting:
            if path in blocked:
                block_parent(path)
                continue
            now = _recheck(path, st, ops, res)
            if now is _GONE:
                continue
            if now is None:
                res.left.append(path)
                block_parent(path)
                continue
            try:
                ops.rmdir(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                res.findings.append(Finding(path, "error", "repair refused: " + _why(exc)))
                res.left.append(path)
                block_parent(path)
            continue
        found = _entry_findings(path, st, top.st_dev)
        res.findings.extend(found)
        repaired = False if any(f.kind == "device" for f in found) else _repair(path, st, ops, uid, res)
        if repaired is None:
            continue
        if not repaired:
            res.left.append(path)
            block_parent(path)
            continue
        if stat.S_ISDIR(st.st_mode):
            if delete:
                stack.append((path, st, True))
            kids = _children(path, ops, res)
            if kids is None:
                res.left.append(path)
                blocked.add(path)
                continue
            for child, cst in reversed(kids):
                stack.append((child, cst, False))
        elif delete:
            now = _recheck(path, st, ops, res)
            if now is _GONE:
                continue
            if now is None:
                res.left.append(path)
                block_parent(path)
                continue
            try:
                ops.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                res.findings.append(Finding(path, "error", "repair refused: " + _why(exc)))
                res.left.append(path)
                block_parent(path)
    if delete:
        try:
            ops.lstat(root)
        except FileNotFoundError:
            res.removed = True
        except OSError:
            res.removed = False
    return res


def release(root, *, ops=None):
    """Make ``root`` deletable without deleting it. Findings are recorded before
    each repair, so a caller can fail on what it found (spec J2)."""
    return _walk(root, ops, False)


def remove(root, *, ops=None):
    """Release and delete ``root`` in one walk; ``left`` names what stayed."""
    return _walk(root, ops, True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in ("inspect", "release", "remove"):
        sys.stderr.write("usage: _lib_test_temp.py {inspect|release|remove} PATH\n")
        return 2
    op, path = argv
    res = {"inspect": inspect, "release": release, "remove": remove}[op](path)
    shown = res.findings if op == "inspect" else res.refusals
    for f in shown:
        sys.stderr.write("%s: %s\n" % (f.detail, f.path))
    if op == "inspect":
        return 1 if res.findings else 0
    if op == "release":
        return 1 if res.refusals else 0
    return 0 if res.removed else 1


if __name__ == "__main__":
    sys.exit(main())
