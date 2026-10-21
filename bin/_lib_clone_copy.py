"""Clone-first file and tree copies (stdlib only; #940 D2, spec §6.7).

On APFS, ``fclonefileat(2)`` gives a copy that shares the source's blocks
until either side is written, so a copy of a large test scaffold writes no
data. This module takes that clone wherever it can and a real copy wherever
it cannot, and otherwise behaves exactly as the stdlib copier it stands in
for:

- :func:`copy2` is ``shutil.copy2`` for the same arguments: a directory
  destination keeps the source's basename, ``follow_symlinks=False`` copies a
  link as a link, the same file raises ``shutil.SameFileError``, and an
  existing destination is overwritten in place (same inode), which a clone
  cannot do, so that case is a real copy. Metadata is ``shutil.copystat``'s,
  applied after the clone as after a copy.
- :func:`copytree` is ``shutil.copytree`` with :func:`copy2` as its file
  copier, so symlink policy, ignore lists, dangling-link errors and
  destination collisions are the stdlib's own code.

A clone is attempted only on Darwin. An unsupported filesystem, a
cross-volume destination, a missing ``fclonefileat`` and any non-Darwin
platform take the real copy; any other error (EIO, ENOSPC, EACCES, …)
propagates and is never retried as a copy. A clone is never a hardlink: the
copy is a new inode with a link count of one.

The optional ``counts`` mapping is incremented under ``"clone"`` or
``"copy"`` per file, so a caller or a measurement can see which path ran.
This module is public and imports nothing outside the standard library.

It binds ``fclonefileat`` itself rather than importing the binding in
``bin/_lib_remote_staging.py`` (#940 D-2 F17): that module is private, so it
is absent from the public tree that this module and its public callers ship
in, and a public module may not import it. The staging module may import
from this one instead (spec §6.7).
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
import sys

# Errors that mean "this filesystem or this pair of volumes cannot clone",
# never "the data could not be written". EEXIST is a destination created
# after the existence check: `shutil.copy2` overwrites it, and so do we.
_FALLBACK_ERRNOS = frozenset(
    code for code in (
        getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None),
        errno.ENOSYS, errno.EXDEV, errno.EINVAL, errno.EPERM, errno.EEXIST)
    if code is not None)

_AT_FDCWD = -2            # Darwin's AT_FDCWD
_CLONE_NOOWNERCOPY = 0x0002  # leave ownership to the caller, as a copy does

_LIBC_CLONE = []  # the resolved fclonefileat, once looked up
# The platform this module decides on; a test sets it here instead of
# patching the interpreter-wide ``sys.platform`` (#940 D-2 F16).
_PLATFORM = sys.platform


def _fclonefileat():
    """The C ``fclonefileat`` on Darwin, else None."""
    if _PLATFORM != "darwin":
        return None
    if not _LIBC_CLONE:
        libc = ctypes.CDLL(None, use_errno=True)
        clone = getattr(libc, "fclonefileat", None)
        if clone is not None:
            clone.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
            clone.restype = ctypes.c_int
        _LIBC_CLONE.append(clone)
    return _LIBC_CLONE[0]


def _clone_fd(source_fd: int, destination: str) -> None:
    """Clone the open regular file `source_fd` to the new path `destination`."""
    clone = _fclonefileat()
    if clone is None:
        raise OSError(errno.ENOTSUP, "fclonefileat is unavailable", destination)
    if clone(source_fd, _AT_FDCWD, os.fsencode(os.path.abspath(destination)),
             _CLONE_NOOWNERCOPY) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination)


def _count(counts, method: str) -> None:
    if counts is not None:
        counts[method] = counts.get(method, 0) + 1


def copy2(src, dst, *, follow_symlinks: bool = True, counts=None):
    """``shutil.copy2(src, dst)``, by clone where the filesystem allows it.

    Returns the destination path, as ``shutil.copy2`` does.
    """
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src))
    if (not follow_symlinks and os.path.islink(src)) or os.path.lexists(dst):
        # A link copied as a link, or a destination to overwrite in place.
        result = shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
        _count(counts, "copy")
        return result
    try:
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        # The stdlib's own error for this source (missing, unreadable, …).
        return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            # A FIFO, a device or a directory: the stdlib decides.
            os.close(fd)
            fd = -1
            return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
        try:
            _clone_fd(fd, dst)
        except OSError as exc:
            if exc.errno not in _FALLBACK_ERRNOS:
                raise
            result = shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
            _count(counts, "copy")
            return result
        shutil.copystat(src, dst, follow_symlinks=follow_symlinks)
        _count(counts, "clone")
        return dst
    finally:
        if fd >= 0:
            os.close(fd)


def copytree(src, dst, symlinks: bool = False, ignore=None,
             ignore_dangling_symlinks: bool = False, dirs_exist_ok: bool = False,
             *, counts=None):
    """``shutil.copytree`` with :func:`copy2` as its file copier."""
    def copier(source, destination):
        return copy2(source, destination, counts=counts)

    return shutil.copytree(src, dst, symlinks=symlinks, ignore=ignore,
                           copy_function=copier,
                           ignore_dangling_symlinks=ignore_dangling_symlinks,
                           dirs_exist_ok=dirs_exist_ok)
