"""Clone-first copies (#940 D2, spec §6.7).

`bin/_lib_clone_copy.py` copies a file or a tree by APFS clone where it can
and by a real copy where it cannot, with the results `shutil.copy2` and
`shutil.copytree` give for the same inputs. These tests hold it to three
things: the clone is actually taken on a same-volume APFS copy (a spy on the
clone call and the module's own counts), it is never a hardlink, and every
observable result — contents, modes, timestamps, symlink policy, ignore lists
and what happens when the destination already exists — equals the stdlib's.
"""

import errno
import importlib.util
import os
import pathlib
import shutil
import stat
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "_lib_clone_copy_under_test", str(ROOT / "bin" / "_lib_clone_copy.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CC = _load()

# The runners' scratch is APFS (the internal disk, or the enrolled root image),
# so a Darwin run clones; any other platform takes the real copy.
EXPECT_CLONE = sys.platform == "darwin"


def _source_tree(base):
    src = base / "src"
    (src / "sub" / "deep").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"alpha\n" * 1000)
    (src / "exec.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    os.chmod(str(src / "exec.sh"), 0o755)
    (src / "private").write_bytes(b"secret")
    os.chmod(str(src / "private"), 0o600)
    (src / "sub" / "b.bin").write_bytes(bytes(range(256)) * 64)
    (src / "sub" / "deep" / "c.txt").write_text("gamma\n", encoding="utf-8")
    (src / "sub" / "skip.pyc").write_bytes(b"compiled")
    (src / "empty").write_bytes(b"")
    os.symlink("a.txt", str(src / "link-to-file"))
    os.symlink("sub", str(src / "link-to-dir"))
    old = 1_500_000_000_123_456_789
    os.utime(str(src / "a.txt"), ns=(old, old))
    os.utime(str(src / "sub" / "deep"), ns=(old + 7, old + 7))
    os.chmod(str(src / "sub" / "deep"), 0o750)
    return src


def _snapshot(root):
    """Every observable property of a copied tree, keyed by relative path."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in sorted(dirnames + filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, str(root))
            st = os.lstat(path)
            entry = {"mode": stat.S_IMODE(st.st_mode), "type": stat.S_IFMT(st.st_mode)}
            if stat.S_ISLNK(st.st_mode):
                entry["target"] = os.readlink(path)
            else:
                entry["mtime_ns"] = st.st_mtime_ns
            if stat.S_ISREG(st.st_mode):
                with open(path, "rb") as fh:
                    entry["data"] = fh.read()
            out[rel] = entry
    return out


@pytest.mark.parametrize("symlinks", [pytest.param(False, id="follow"),
                                      pytest.param(True, id="symlinks")])
@pytest.mark.parametrize("ignored", [pytest.param(False, id="all"),
                                     pytest.param(True, id="ignore")])
def test_a_tree_copy_equals_shutil_copytree(tmp_path, symlinks, ignored):
    src = _source_tree(tmp_path)
    ignore = shutil.ignore_patterns("*.pyc") if ignored else None
    shutil.copytree(str(src), str(tmp_path / "oracle"), symlinks=symlinks, ignore=ignore)
    counts = {}
    CC.copytree(str(src), str(tmp_path / "clone"), symlinks=symlinks, ignore=ignore,
                counts=counts)
    assert _snapshot(tmp_path / "clone") == _snapshot(tmp_path / "oracle")
    if ignored:
        assert not (tmp_path / "clone" / "sub" / "skip.pyc").exists()
    files = sum(1 for e in _snapshot(tmp_path / "clone").values()
                if e["type"] == stat.S_IFREG)
    assert sum(counts.values()) == files, counts
    assert counts.get("clone" if EXPECT_CLONE else "copy") == files, counts


def test_a_same_volume_copy_calls_the_clone_and_is_never_a_hardlink(tmp_path, monkeypatch):
    src = tmp_path / "f.bin"
    src.write_bytes(os.urandom(1 << 16))
    calls = []
    real = CC._clone_fd

    def spy(fd, destination):
        calls.append(destination)
        return real(fd, destination)

    monkeypatch.setattr(CC, "_clone_fd", spy)
    counts = {}
    dst = tmp_path / "g.bin"
    assert CC.copy2(str(src), str(dst), counts=counts) == str(dst)
    assert len(calls) == 1, "the clone path was not taken"
    assert counts == {("clone" if EXPECT_CLONE else "copy"): 1}
    s, d = os.stat(str(src)), os.stat(str(dst))
    assert s.st_ino != d.st_ino
    assert s.st_nlink == 1 and d.st_nlink == 1
    assert dst.read_bytes() == src.read_bytes()
    # Independence: writing the copy leaves the source untouched.
    with open(str(dst), "r+b") as fh:
        fh.write(b"changed")
    assert src.read_bytes() != dst.read_bytes()


@pytest.mark.parametrize("code", [pytest.param(errno.EXDEV, id="cross-volume"),
                                  pytest.param(errno.ENOTSUP, id="unsupported")])
def test_an_unsupported_or_cross_volume_clone_falls_back_to_a_real_copy(
        tmp_path, monkeypatch, code):
    def refuse(fd, destination):
        raise OSError(code, os.strerror(code), destination)

    monkeypatch.setattr(CC, "_clone_fd", refuse)
    src = _source_tree(tmp_path)
    shutil.copytree(str(src), str(tmp_path / "oracle"))
    counts = {}
    CC.copytree(str(src), str(tmp_path / "copy"), counts=counts)
    assert _snapshot(tmp_path / "copy") == _snapshot(tmp_path / "oracle")
    assert "clone" not in counts and counts["copy"] > 0, counts


def test_linux_takes_the_real_copy(tmp_path, monkeypatch):
    # The module's own platform lookup, never the interpreter-wide
    # `sys.platform`, which every other module reads too (#940 D-2 F16).
    monkeypatch.setattr(CC, "_PLATFORM", "linux")
    src = tmp_path / "f"
    src.write_bytes(b"x" * 4096)
    counts = {}
    CC.copy2(str(src), str(tmp_path / "g"), counts=counts)
    assert counts == {"copy": 1}
    assert (tmp_path / "g").read_bytes() == src.read_bytes()


@pytest.mark.parametrize("code", [pytest.param(errno.EIO, id="eio"),
                                  pytest.param(errno.ENOSPC, id="enospc"),
                                  pytest.param(errno.EACCES, id="eacces")])
def test_a_genuine_io_error_propagates(tmp_path, monkeypatch, code):
    def fail(fd, destination):
        raise OSError(code, os.strerror(code), destination)

    monkeypatch.setattr(CC, "_clone_fd", fail)
    src = tmp_path / "f"
    src.write_bytes(b"data")
    with pytest.raises(OSError) as info:
        CC.copy2(str(src), str(tmp_path / "g"))
    assert info.value.errno == code
    assert not (tmp_path / "g").exists(), "a genuine failure fell back to a copy"


def test_file_level_behaviour_equals_shutil_copy2(tmp_path):
    src = _source_tree(tmp_path)
    for name in ("into-dir-oracle", "into-dir-clone"):
        (tmp_path / name).mkdir()
    # Into a directory: the basename is kept.
    assert CC.copy2(str(src / "a.txt"), str(tmp_path / "into-dir-clone")) == \
        os.path.join(str(tmp_path / "into-dir-clone"), "a.txt")
    shutil.copy2(str(src / "a.txt"), str(tmp_path / "into-dir-oracle"))
    assert _snapshot(tmp_path / "into-dir-clone") == _snapshot(tmp_path / "into-dir-oracle")
    # follow_symlinks=False copies the link itself.
    CC.copy2(str(src / "link-to-file"), str(tmp_path / "l-clone"), follow_symlinks=False)
    shutil.copy2(str(src / "link-to-file"), str(tmp_path / "l-oracle"), follow_symlinks=False)
    assert os.readlink(str(tmp_path / "l-clone")) == os.readlink(str(tmp_path / "l-oracle"))
    # The same file is refused the same way.
    with pytest.raises(shutil.SameFileError):
        CC.copy2(str(src / "a.txt"), str(src / "a.txt"))
    # A missing source fails the same way.
    with pytest.raises(FileNotFoundError):
        CC.copy2(str(src / "absent"), str(tmp_path / "absent-copy"))


def test_an_existing_destination_file_is_overwritten_in_place_as_copy2_does(tmp_path):
    src = tmp_path / "src.txt"
    src.write_bytes(b"new contents\n")
    results = {}
    for name, copier in (("oracle", shutil.copy2), ("clone", CC.copy2)):
        dst = tmp_path / ("dst-" + name)
        dst.write_bytes(b"old contents that are longer\n")
        inode = os.stat(str(dst)).st_ino
        copier(str(src), str(dst))
        results[name] = (dst.read_bytes(), os.stat(str(dst)).st_ino == inode,
                         os.stat(str(dst)).st_mtime_ns)
    assert results["clone"] == results["oracle"]


def test_destination_collisions_behave_as_copytree(tmp_path):
    src = _source_tree(tmp_path)
    for name in ("oracle", "clone"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.txt").write_bytes(b"pre-existing")
    with pytest.raises(FileExistsError):
        shutil.copytree(str(src), str(tmp_path / "oracle"))
    with pytest.raises(FileExistsError):
        CC.copytree(str(src), str(tmp_path / "clone"))
    shutil.copytree(str(src), str(tmp_path / "oracle"), dirs_exist_ok=True)
    CC.copytree(str(src), str(tmp_path / "clone"), dirs_exist_ok=True)
    assert _snapshot(tmp_path / "clone") == _snapshot(tmp_path / "oracle")


def test_a_dangling_symlink_behaves_as_copytree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "real").write_bytes(b"r")
    os.symlink("missing-target", str(src / "dangling"))
    with pytest.raises(shutil.Error):
        shutil.copytree(str(src), str(tmp_path / "oracle"))
    with pytest.raises(shutil.Error):
        CC.copytree(str(src), str(tmp_path / "clone"))
    shutil.copytree(str(src), str(tmp_path / "oracle2"), ignore_dangling_symlinks=True)
    CC.copytree(str(src), str(tmp_path / "clone2"), ignore_dangling_symlinks=True)
    assert _snapshot(tmp_path / "clone2") == _snapshot(tmp_path / "oracle2")
    shutil.copytree(str(src), str(tmp_path / "oracle3"), symlinks=True)
    CC.copytree(str(src), str(tmp_path / "clone3"), symlinks=True)
    assert _snapshot(tmp_path / "clone3") == _snapshot(tmp_path / "oracle3")


def test_the_module_is_public_and_imports_nothing_private():
    source = (ROOT / "bin" / "_lib_clone_copy.py").read_text(encoding="utf-8")
    import ast
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
    ast.parse(source, feature_version=(3, 9))
    # Public, so public tests can import it (spec §6.7): `bin/_lib_*` is not a
    # public glob, so the module needs its own allowlist entry. The allowlist
    # is itself mirror-private, so the public mirror checks only the module.
    if (ROOT / ".mirror-allowlist").is_file():
        allowlist = (ROOT / ".mirror-allowlist").read_text(
            encoding="utf-8").splitlines()
        assert "bin/_lib_clone_copy.py" in allowlist
        assert "!bin/_lib_clone_copy.py" not in allowlist


def test_the_shared_corpus_copier_clones_under_its_shared_lock_and_restamps(
        tmp_path, monkeypatch):
    # copy_shared_corpus takes every file through the clone path, with the
    # scale's build lock held SHARED for the whole walk, still excludes the
    # SQLite sidecars, and still re-stamps the copy's Codex trust afterwards.
    import fcntl

    import _shared_corpus as SC
    scale = tmp_path / "corpus" / "small"
    data = scale / "data"
    data.mkdir(parents=True)
    (data / "stats.db").write_bytes(b"s" * 8192)
    (data / "stats.db-wal").write_bytes(b"w")
    (scale / "claude").mkdir()
    (scale / "claude" / "a.jsonl").write_text("{}\n", encoding="utf-8")
    module = SC._load_clone_copy()
    lock = SC.corpus_lock_path(scale.parent, scale.name)
    real = module._clone_fd
    calls, held = [], []

    def spy(fd, destination):
        with open(str(lock), "a") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                held.append(False)
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        calls.append(os.path.basename(destination))
        return real(fd, destination)

    monkeypatch.setattr(module, "_clone_fd", spy)
    restamped = []
    monkeypatch.setattr(SC, "restamp_codex_hook_trust", lambda root: restamped.append(root))
    result = SC.copy_shared_corpus(data, tmp_path / "copy")
    assert result == tmp_path / "copy" / "data"
    assert sorted(calls) == ["a.jsonl", "stats.db"], calls
    assert held == [True, True], "the shared build lock was not held during the copy"
    assert not (result / "stats.db-wal").exists()
    assert (result / "stats.db").read_bytes() == b"s" * 8192
    assert restamped == [tmp_path / "copy"]
    assert os.stat(str(result / "stats.db")).st_ino != os.stat(str(data / "stats.db")).st_ino



# --- clone_regular_file: the strict clone (#1000 spec 5.1, T2) ---------------
#
# The transcript probe must never write a byte copy of the store. Its clone is
# therefore this strict variant: any failure raises, with the syscall errno, and
# nothing is ever copied, hardlinked or overwritten in its place.


def _as_darwin(monkeypatch):
    """Make the strict clone reach its clone call on any host."""
    monkeypatch.setattr(CC, "_PLATFORM", "darwin")
    monkeypatch.setattr(CC, "_fclonefileat", lambda: (lambda *_a: 0))


_STRICT_ERRNOS = ("ENOTSUP", "EOPNOTSUPP", "EXDEV", "EINVAL", "EPERM",
                  "EEXIST", "EIO", "ENOSPC")


@pytest.mark.parametrize("name", [n for n in _STRICT_ERRNOS if hasattr(errno, n)])
def test_the_strict_clone_raises_every_clone_errno_and_copies_nothing(
        tmp_path, monkeypatch, name):
    """Stored-data safety: a clone the filesystem refuses (HFS+/exFAT ENOTSUP,
    a cross-volume EXDEV) or fails (EIO, ENOSPC, ...) must raise that errno and
    never fall back to a byte copy, which on a 4 GB transcript store fills the
    volume (#1000)."""
    code = getattr(errno, name)
    _as_darwin(monkeypatch)
    attempts = []

    def refuse(fd, destination):
        attempts.append(destination)
        raise OSError(code, os.strerror(code), destination)

    monkeypatch.setattr(CC, "_clone_fd", refuse)
    src = tmp_path / "f"
    src.write_bytes(b"store bytes")
    with pytest.raises(OSError) as info:
        CC.clone_regular_file(str(src), str(tmp_path / "g"), counts={})
    assert info.value.errno == code
    assert attempts, "the clone call was never attempted"
    assert not (tmp_path / "g").exists(), "a refused clone created a destination"


@pytest.mark.parametrize("case", ["no-symbol", "not-darwin"])
def test_the_strict_clone_without_fclonefileat_is_unsupported(
        tmp_path, monkeypatch, case):
    """Machine safety: a host that cannot clone at all reports ENOTSUP, the
    refusal the probe maps to "unavailable", rather than copying the store."""
    if case == "no-symbol":
        monkeypatch.setattr(CC, "_PLATFORM", "darwin")
        monkeypatch.setattr(CC, "_fclonefileat", lambda: None)
    else:
        monkeypatch.setattr(CC, "_PLATFORM", "linux")
    src = tmp_path / "f"
    src.write_bytes(b"x")
    with pytest.raises(OSError) as info:
        CC.clone_regular_file(str(src), str(tmp_path / "g"))
    assert info.value.errno == errno.ENOTSUP
    assert not (tmp_path / "g").exists()


def test_the_strict_clone_refuses_a_fifo_without_blocking(tmp_path, monkeypatch):
    """Machine safety: a FIFO planted where the store should be must fail with
    EINVAL at once; a blocking open would hang the rebuild under its locks."""
    import threading

    _as_darwin(monkeypatch)

    def unexpected(fd, destination):
        raise AssertionError("a non-regular source reached the clone call")

    monkeypatch.setattr(CC, "_clone_fd", unexpected)
    fifo = tmp_path / "fifo"
    os.mkfifo(str(fifo))
    outcome = []

    def run():
        try:
            CC.clone_regular_file(str(fifo), str(tmp_path / "g"))
        except OSError as exc:
            outcome.append(exc.errno)
        else:
            outcome.append(None)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "opening a FIFO source blocked"
    assert outcome == [errno.EINVAL]
    assert not (tmp_path / "g").exists()


def test_the_strict_clone_never_follows_a_symlinked_source(tmp_path, monkeypatch):
    """Stored-data safety: a symlinked store member must fail (ELOOP) rather
    than clone whatever the link points at."""
    _as_darwin(monkeypatch)
    monkeypatch.setattr(CC, "_clone_fd", lambda fd, destination: None)
    target = tmp_path / "target"
    target.write_bytes(b"elsewhere")
    os.symlink(str(target), str(tmp_path / "link"))
    with pytest.raises(OSError) as info:
        CC.clone_regular_file(str(tmp_path / "link"), str(tmp_path / "g"))
    assert info.value.errno == errno.ELOOP
    assert not (tmp_path / "g").exists()


def test_the_strict_clone_counts_one_clone_and_never_a_copy(tmp_path, monkeypatch):
    """Measurement accuracy: a successful strict clone is reported as a clone,
    so the counts can never hide a copy."""
    _as_darwin(monkeypatch)

    def clone(fd, destination):
        with open(destination, "xb") as fh:
            fh.write(os.pread(fd, 1 << 16, 0))

    monkeypatch.setattr(CC, "_clone_fd", clone)
    src = tmp_path / "f"
    src.write_bytes(b"cloned")
    counts = {}
    dst = str(tmp_path / "g")
    assert CC.clone_regular_file(str(src), dst, counts=counts) == dst
    assert counts == {"clone": 1}
    assert (tmp_path / "g").read_bytes() == b"cloned"


def test_the_strict_clone_takes_a_real_apfs_clone(tmp_path):
    """Stored-data accuracy: on the runner's APFS scratch, with no seam, the
    strict clone produces a destination with the source's bytes on a new inode;
    elsewhere it reports ENOTSUP and creates nothing."""
    src = tmp_path / "f.bin"
    src.write_bytes(os.urandom(1 << 16))
    dst = tmp_path / "g.bin"
    counts = {}
    if not EXPECT_CLONE:
        with pytest.raises(OSError) as info:
            CC.clone_regular_file(str(src), str(dst), counts=counts)
        assert info.value.errno == errno.ENOTSUP
        assert not dst.exists() and counts == {}
        return
    CC.clone_regular_file(str(src), str(dst), counts=counts)
    assert counts == {"clone": 1}
    assert dst.read_bytes() == src.read_bytes()
    assert os.stat(str(dst)).st_ino != os.stat(str(src)).st_ino
    assert os.stat(str(dst)).st_nlink == 1
