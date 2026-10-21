"""R1 (#963 spec 9.1): the release kernel over injected filesystem facts.

Real Darwin flags run in tests/test_session_isolation_native.py. This module
drives the portable branches through an ops seam whose lstat reports flags,
link counts, devices and identity swaps the host cannot produce.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

import _lib_test_temp as T

REPO = Path(__file__).resolve().parent.parent
KERNEL = REPO / "bin" / "_lib_test_temp.py"
UF_NODUMP = 0x1
UF_HIDDEN = 0x8000


class FakeOps(T.RealOps):
    """Real files, plus injected facts the host filesystem cannot carry."""

    def __init__(self, flags=None, nlink=None, dev=None, swap=(), refuse=()):
        self.flags = {str(k): v for k, v in (flags or {}).items()}
        self.nlink = {str(k): v for k, v in (nlink or {}).items()}
        self.dev = {str(k): v for k, v in (dev or {}).items()}
        self.swap = {str(k) for k in swap}
        self.refuse = set(refuse)
        self.seen = {}
        self.calls = []

    def lstat(self, path):
        path = str(path)
        st = os.lstat(path)
        count = self.seen.get(path, 0)
        self.seen[path] = count + 1
        # A swapped entry shows a new inode from its second lstat on: the
        # walk saw one inode, and something else now sits at the same name.
        ino = st.st_ino + (10 ** 9 if path in self.swap and count >= 1 else 0)
        return types.SimpleNamespace(
            st_mode=st.st_mode, st_ino=ino, st_dev=self.dev.get(path, st.st_dev),
            st_nlink=self.nlink.get(path, st.st_nlink), st_uid=st.st_uid,
            st_flags=self.flags.get(path, 0))

    def listdir(self, path):
        self.calls.append(("listdir", str(path)))
        return os.listdir(path)

    def chmod_dir(self, path, mode):
        self.calls.append(("chmod_dir", str(path), mode))
        os.chmod(path, mode)

    def set_flags(self, path, flags):
        path = str(path)
        if self.flags.get(path, 0) & ~flags & sum(self.refuse):
            raise PermissionError(1, "Operation not permitted", path)
        self.calls.append(("set_flags", path, flags))
        self.flags[path] = flags

    def unlink(self, path):
        if self.flags.get(str(path), 0) & T.BLOCKING_MASK:
            raise PermissionError(1, "Operation not permitted", str(path))
        os.unlink(path)

    def rmdir(self, path):
        if self.flags.get(str(path), 0) & T.BLOCKING_MASK:
            raise PermissionError(1, "Operation not permitted", str(path))
        os.rmdir(path)



class VanishingOps(FakeOps):
    """A concurrent writer removes ``victim`` after the walk listed its parent
    and before the walk lists ``victim`` itself: git's detached auto-gc pruning
    a loose-object fan-out directory (``.git/objects/6f``) while the guard
    inspects the tree of the test whose commit started it (#963 census)."""

    def __init__(self, victim):
        super().__init__()
        self.victim = str(victim)

    def listdir(self, path):
        if str(path) == self.victim and os.path.isdir(self.victim):
            for name in os.listdir(self.victim):
                os.unlink(os.path.join(self.victim, name))
            os.rmdir(self.victim)
        return super().listdir(path)

@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    r.mkdir()
    yield r
    # The test's own cleanup, through real operations: a refusal test leaves
    # real (unflagged) entries behind, and the hygiene guard checks tmp_path.
    for dirpath, dirnames, _files in os.walk(r):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if not os.path.islink(p):
                os.chmod(p, 0o700)
    leftover = T.remove(r)
    assert leftover.removed, leftover.findings


def _ro(path, mode=0o555):
    os.chmod(path, mode)


def test_remove_deletes_the_abort_shape(root, tmp_path):
    """Spec 1.1's remnant: read-only directories whose only entries are a
    symlink, or an empty read-only directory. pytest's handler aborts on both."""
    target = tmp_path / "outside-target"
    target.mkdir()
    one = root / "freeze" / "roots" / "1"
    (one / "-p").mkdir(parents=True)
    os.symlink(str(target / "memory"), str(one / "-p" / "memory"))
    (root / "chain" / "inner").mkdir(parents=True)
    for d in (one / "-p", one, root / "chain" / "inner", root / "chain"):
        _ro(d)
    res = T.remove(root)
    assert res.removed and not res.refusals, res.findings
    assert target.is_dir() and list(target.iterdir()) == []


def test_a_hard_link_and_a_symlink_target_are_never_changed(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    shared = outside / "shared.txt"
    shared.write_text("checkout bytes")
    os.chmod(shared, 0o444)
    d = root / "d"
    d.mkdir()
    os.link(str(shared), str(d / "alias"))
    os.symlink(str(outside), str(d / "link"))
    _ro(d, 0o500)
    res = T.remove(root)
    assert res.removed and not res.refusals, res.findings
    assert shared.read_text() == "checkout bytes"
    assert stat.S_IMODE(os.lstat(shared).st_mode) == 0o444, "a hard-linked file's mode changed"
    assert os.lstat(shared).st_nlink == 1
    assert sorted(p.name for p in outside.iterdir()) == ["shared.txt"]


def test_release_clears_only_the_blocking_flags(root):
    f = root / "f"
    f.write_text("x")
    d = root / "d"
    d.mkdir()
    ops = FakeOps(flags={f: T.UF_IMMUTABLE | UF_NODUMP, d: T.UF_APPEND | UF_HIDDEN})
    res = T.release(root, ops=ops)
    assert not res.refusals, res.findings
    assert ops.flags[str(f)] == UF_NODUMP, "a blocking flag survived release, or an unrelated flag was cleared"
    assert ops.flags[str(d)] == UF_HIDDEN, "a blocking flag survived release, or an unrelated flag was cleared"
    assert {(x.path, x.detail) for x in res.findings if x.kind == "flag"} == {
        (str(f), "deletion-blocking flag UF_IMMUTABLE"),
        (str(d), "deletion-blocking flag UF_APPEND"),
    }
    assert T.remove(root, ops=ops).removed


def test_a_flagged_multiply_linked_inode_is_refused(root, tmp_path):
    alias = tmp_path / "alias"
    alias.write_text("x")
    linked = root / "linked"
    os.link(str(alias), str(linked))
    ops = FakeOps(flags={linked: T.UF_IMMUTABLE})
    res = T.remove(root, ops=ops)
    assert [(x.path, x.kind) for x in res.refusals] == [(str(linked), "nlink")], res.findings
    assert not any(c[0] == "set_flags" for c in ops.calls), "cleared a flag on a multiply linked inode"
    assert res.left == [str(linked)] and linked.exists() and not res.removed


def test_an_identity_swap_is_refused_before_any_mutation(root):
    sealed = root / "sealed"
    sealed.mkdir()
    (sealed / "f").write_text("x")
    _ro(sealed, 0o500)
    ops = FakeOps(swap={sealed})
    res = T.release(root, ops=ops)
    assert not any(c[0] == "chmod_dir" and c[1] == str(sealed) for c in ops.calls), (
        "repaired an entry whose identity changed")
    assert [(x.path, x.kind) for x in res.refusals] == [(str(sealed), "identity")], res.findings


@pytest.mark.parametrize("kind", ["file", "empty-dir"])
def test_an_identity_swap_is_refused_before_unlink_and_rmdir(root, kind):
    """963-CD-K6: the rechecks before unlink and rmdir are their own. A writable,
    unflagged file and an empty owner-rwx directory need no repair, so the only
    identity check between the walk's lstat and the deletion is the one right
    before it; a replacement at the same name must survive, refused and left."""
    entry = root / "victim"
    if kind == "file":
        entry.write_text("replacement")
    else:
        entry.mkdir()
    ops = FakeOps(swap={entry})
    res = T.remove(root, ops=ops)
    assert [(x.path, x.kind) for x in res.refusals] == [(str(entry), "identity")], res.findings
    assert os.path.lexists(entry), "deleted an entry whose identity changed"
    if kind == "file":
        assert entry.read_text() == "replacement"
    assert str(entry) in res.left and not res.removed, (res.left, res.removed)
    assert root.is_dir(), "removed the parent of a refused entry"


def test_a_privileged_flag_is_refused_visibly(root):
    f = root / "f"
    f.write_text("x")
    ops = FakeOps(flags={f: T.SF_IMMUTABLE}, refuse={T.SF_IMMUTABLE})
    res = T.release(root, ops=ops)
    assert [(x.path, x.kind) for x in res.refusals] == [(str(f), "privileged")], (
        "no visible refusal for a flag this user cannot clear: %r" % (res.findings,))
    assert "SF_IMMUTABLE" in res.refusals[0].detail


def test_a_device_crossing_is_reported_and_never_entered(root):
    mnt = root / "mnt"
    (mnt / "inner").mkdir(parents=True)
    _ro(mnt / "inner", 0o500)
    ops = FakeOps(dev={mnt: os.lstat(root).st_dev + 1})
    res = T.remove(root, ops=ops)
    assert res.left == [str(mnt)]
    assert [(x.path, x.detail) for x in res.findings if x.kind == "device"] == [(str(mnt), "device crossing")]
    assert ("listdir", str(mnt)) not in ops.calls
    assert stat.S_IMODE(os.lstat(mnt / "inner").st_mode) == 0o500


def test_inspect_reports_and_mutates_nothing(root):
    (root / "a" / "b").mkdir(parents=True)
    _ro(root / "a" / "b")
    _ro(root / "a")
    res = T.inspect(root)
    assert sorted((x.path, x.detail) for x in res.findings) == sorted([
        (str(root / "a"), "directory missing owner rwx (mode 0o555)"),
        (str(root / "a" / "b"), "directory missing owner rwx (mode 0o555)"),
    ]), "inspection found nothing: %r" % (res.findings,)
    assert stat.S_IMODE(os.lstat(root / "a").st_mode) == 0o555


def test_the_cli_removes_refuses_and_rejects_usage(tmp_path):
    victim = tmp_path / "v"
    (victim / "x").mkdir(parents=True)
    _ro(victim / "x")
    r = subprocess.run([sys.executable, "-B", str(KERNEL), "remove", str(victim)],
                       capture_output=True, text=True, timeout=45)
    assert r.returncode == 0 and not victim.exists(), r.stderr
    r = subprocess.run([sys.executable, "-B", str(KERNEL), "bogus", str(victim)],
                       capture_output=True, text=True, timeout=45)
    assert r.returncode == 2, r.stderr


@pytest.mark.parametrize("op", ["inspect", "release", "remove"])
def test_an_entry_a_concurrent_writer_removes_is_not_a_finding(root, op):
    """A vanished entry cannot block deletion, so it is neither a finding nor a
    refusal, and a removal still completes (#963 census: a guard false positive
    on a git repository whose detached auto-gc pruned objects/<xx>)."""
    gone = root / ".git" / "objects" / "6f"
    gone.mkdir(parents=True)
    (gone / "obj").write_text("x")
    res = getattr(T, op)(root, ops=VanishingOps(gone))
    assert res.findings == [], "a vanished entry was reported: %r" % (res.findings,)
    assert not gone.exists()
    if op == "remove":
        assert res.removed and res.left == [], res
