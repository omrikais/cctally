"""Direct, file-attributed SQLite write accounting for the retention
instrument (#940 spec revision 7j, section 6.7).

One stdlib `ctypes` implementation of a wrapping SQLite VFS. Activated, it is
the process's default VFS: a copy of the original default whose `xOpen` is a
Python callback that opens the file through the original (inner) VFS,
classifies it by its open flags, and swaps in a copy of the inner I/O-method
table whose `xWrite` is a Python callback. That callback adds the bytes it
passes to the inner `xWrite`, repeated writes at one offset included, to the
file's class. Every other method is the inner C function itself. What it
measures is logical SQLite write traffic, not physical SSD traffic.

TEMP is the union, counted once per write, of the TEMP_DB, TEMP_JOURNAL,
SUBJOURNAL and TRANSIENT_DB classes and of unnamed and delete-on-close files.
MAIN_DB, WAL, MAIN_JOURNAL and SUPER_JOURNAL are not temp.

Contract:

- Importing this module has no side effect. `activate()` registers the
  wrapper once per process (idempotent, never stacked on itself or on another
  wrapper, and refusing a VFS already registered under its name) and makes
  it the default; `deactivate()` restores the previous default once every
  activation is released, and fails if SQLite refuses the restoration.
  Callbacks and method tables live for the rest of the process, so a
  connection opened while the wrapper was the default keeps being counted
  after restoration.
- Before registering, it validates the CPython connection layout, the SQLite
  symbols and version, the VFS and I/O-method versions and every required
  pointer. An unsupported runtime raises `AccountingUnavailable`; nothing
  ever falls back to reading zero.
- A callback never lets an exception reach `ctypes`: it latches the error,
  which invalidates the measurement it falls in, and returns a defined SQLite
  error code (`SQLITE_CANTOPEN` from `xOpen`, `SQLITE_IOERR_WRITE` from
  `xWrite`).
- Measured intervals are single-threaded: a callback on another thread while
  an interval is open invalidates it. So do an underlying I/O failure, a
  failed open, an unclassified write and a MAIN_JOURNAL or SUPER_JOURNAL write
  (the measured stores run in WAL mode).
- Every interval starts with a process-local positive control: a known
  delete-on-close TEMP_DB file written through the installed default VFS must
  be counted at exactly its size.
- `require_attributed` checks `SQLITE_FCNTL_VFS_POINTER` against the
  wrapper: a connection opened before activation, or with an explicit inner
  VFS, is rejected.
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import sys
import threading

WRAPPER_NAME = "cctally-write-accounting"

SQLITE_OK = 0
SQLITE_CANTOPEN = 14
SQLITE_IOERR_WRITE = 10 | (3 << 8)
SQLITE_FCNTL_VFS_POINTER = 27

OPEN_READONLY = 0x00000001
OPEN_READWRITE = 0x00000002
OPEN_CREATE = 0x00000004
OPEN_DELETEONCLOSE = 0x00000008
OPEN_EXCLUSIVE = 0x00000010

#: The file-type bit `xOpen` receives for each class.
CLASS_FLAG = {
    "MAIN_DB": 0x00000100,
    "TEMP_DB": 0x00000200,
    "TRANSIENT_DB": 0x00000400,
    "MAIN_JOURNAL": 0x00000800,
    "TEMP_JOURNAL": 0x00001000,
    "SUBJOURNAL": 0x00002000,
    "SUPER_JOURNAL": 0x00004000,
    "WAL": 0x00080000,
}
CLASSES = tuple(CLASS_FLAG)
TEMP_CLASSES = frozenset(("TEMP_DB", "TEMP_JOURNAL", "SUBJOURNAL",
                          "TRANSIENT_DB"))
#: Journal classes a measured interval over a WAL store never writes.
UNEXPECTED_CLASSES = ("MAIN_JOURNAL", "SUPER_JOURNAL")
#: No class bit, or more than one.
UNTYPED = "UNTYPED"
_COUNTERS = CLASSES + (UNTYPED, "TEMP", "UNCLASSIFIED", "WRITES", "ERRORS",
                       "FOREIGN", "IO_ERRORS", "OPEN_FAILURES")

#: The positive control: sequential writes to one delete-on-close TEMP_DB
#: file, each counted once.
CONTROL_WRITES = ((0, 4096), (4096, 4096), (8192, 4096))
EVIDENCE_PREFIX = "sqlite-write-accounting-evidence "
EVIDENCE_SCHEMA = 1

# The validated runtime contract; tests replace these to prove a mismatch
# fails activation.
_HANDLE_WORDS = 2          # PyObject_HEAD, then `sqlite3 *db`
_MIN_SQLITE = 3015000      # SQLITE_FCNTL_VFS_POINTER
_SUPPORTED_VFS_VERSIONS = (1, 2, 3)
_SUPPORTED_IO_VERSIONS = (1, 2, 3)
_REQUIRED_SYMBOLS = ("sqlite3_vfs_find", "sqlite3_vfs_register",
                     "sqlite3_vfs_unregister", "sqlite3_file_control",
                     "sqlite3_libversion_number", "sqlite3_total_changes")
_REQUIRED_VFS_METHODS = ("xOpen", "xDelete", "xAccess", "xFullPathname")
_REQUIRED_IO_METHODS = ("xClose", "xRead", "xWrite", "xTruncate", "xSync",
                        "xFileSize", "xLock", "xUnlock", "xCheckReservedLock",
                        "xFileControl", "xSectorSize", "xDeviceCharacteristics")


class AccountingError(AssertionError):
    """Base of every accounting failure: a measurement that cannot be
    trusted fails, it never reads as zero."""


class AccountingUnavailable(AccountingError):
    def __init__(self, detail):
        super().__init__("write accounting unavailable: %s" % detail)


class MeasurementInvalid(AccountingError):
    pass


class NotAttributed(AccountingError):
    pass


# -- SQLite structures (sqlite3.h) -------------------------------------------

class _File(ctypes.Structure):
    _fields_ = [("pMethods", ctypes.c_void_p)]


class _Vfs(ctypes.Structure):
    pass


_VFS_V1_METHODS = ("xOpen", "xDelete", "xAccess", "xFullPathname", "xDlOpen",
                   "xDlError", "xDlSym", "xDlClose", "xRandomness", "xSleep",
                   "xCurrentTime", "xGetLastError")
_Vfs._fields_ = [
    ("iVersion", ctypes.c_int), ("szOsFile", ctypes.c_int),
    ("mxPathname", ctypes.c_int), ("pNext", ctypes.c_void_p),
    ("zName", ctypes.c_char_p), ("pAppData", ctypes.c_void_p),
] + [(name, ctypes.c_void_p) for name in _VFS_V1_METHODS + (
    "xCurrentTimeInt64", "xSetSystemCall", "xGetSystemCall",
    "xNextSystemCall")]
#: The last field of each VFS version.
_VFS_LAST = {1: "xGetLastError", 2: "xCurrentTimeInt64", 3: "xNextSystemCall"}


class _IoMethods(ctypes.Structure):
    _fields_ = [("iVersion", ctypes.c_int)] + [
        (name, ctypes.c_void_p) for name in _REQUIRED_IO_METHODS + (
            "xShmMap", "xShmLock", "xShmBarrier", "xShmUnmap", "xFetch",
            "xUnfetch")]


_IO_LAST = {1: "xDeviceCharacteristics", 2: "xShmUnmap", 3: "xUnfetch"}

_XOPEN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_int,
                          ctypes.POINTER(ctypes.c_int))
_XWRITE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_int, ctypes.c_int64)
_XCLOSE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
_XFILESIZE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_int64))


def _copy_size(struct, last_field):
    return getattr(struct, last_field).offset + ctypes.sizeof(ctypes.c_void_p)


# -- the process-wide state ----------------------------------------------------

_STATE = None
_LOCK = threading.Lock()


class _State:
    """The registered wrapper. Created once per process and never freed: the
    VFS copy, the method-table copies and both callbacks must outlive every
    file opened through them."""

    def __init__(self, lib, inner):
        self.lib = lib
        self.inner = inner
        self.inner_addr = ctypes.addressof(inner.contents)
        self.inner_name = inner.contents.zName.decode()
        self._inner_open = _XOPEN(inner.contents.xOpen)
        version = inner.contents.iVersion
        self.vfs = _Vfs()
        ctypes.memmove(ctypes.byref(self.vfs), self.inner_addr,
                       _copy_size(_Vfs, _VFS_LAST[version]))
        self.vfs.iVersion = version
        # ctypes keeps the assigned bytes object alive with the structure.
        self._name = WRAPPER_NAME.encode()
        self.vfs.zName = self._name
        self.vfs.pNext = None
        self._open_cb = _XOPEN(self._open)
        self._write_cb = _XWRITE(self._write)
        self.vfs.xOpen = ctypes.cast(self._open_cb, ctypes.c_void_p).value
        self.addr = ctypes.addressof(self.vfs)
        self.methods = {}   # inner io_methods address -> (copy, inner xWrite)
        self.files = {}     # sqlite3_file address -> (class, temp, inner xWrite)
        self.counts = dict.fromkeys(_COUNTERS, 0)
        self.errors = []
        self.owner = None   # thread ident of the open interval
        self.depth = 0
        self.disarmed = False

    # Bookkeeping, replaceable by the instrument tests.
    def _classify(self, flags, zname):
        bits = [name for name, bit in CLASS_FLAG.items() if flags & bit]
        cls = bits[0] if len(bits) == 1 else None
        temp = (cls in TEMP_CLASSES or not zname
                or bool(flags & OPEN_DELETEONCLOSE))
        return cls, temp

    def _count(self, pfile, record, n, offset):
        cls, temp, _inner = record
        counts = self.counts
        counts["WRITES"] += 1
        counts[cls or UNTYPED] += n
        if temp:
            counts["TEMP"] += n
        elif cls is None:
            counts["UNCLASSIFIED"] += n

    def _latch(self, where, exc):
        self.counts["ERRORS"] += 1
        self.errors.append("%s: %s: %s" % (where, type(exc).__name__, exc))

    def _note_thread(self):
        owner = self.owner
        if owner is not None and threading.get_ident() != owner:
            self.counts["FOREIGN"] += 1

    def _methods_for(self, inner_methods):
        entry = self.methods.get(inner_methods)
        if entry is not None:
            return entry
        src = _IoMethods.from_address(inner_methods)
        if src.iVersion not in _SUPPORTED_IO_VERSIONS:
            raise AccountingUnavailable(
                "unsupported I/O-method version %d" % src.iVersion)
        for name in _REQUIRED_IO_METHODS:
            if not getattr(src, name):
                raise AccountingUnavailable("I/O method %s is missing" % name)
        copy = _IoMethods()
        ctypes.memmove(ctypes.byref(copy), inner_methods,
                       _copy_size(_IoMethods, _IO_LAST[src.iVersion]))
        copy.iVersion = src.iVersion
        inner_write = _XWRITE(src.xWrite)
        copy.xWrite = ctypes.cast(self._write_cb, ctypes.c_void_p).value
        entry = (copy, inner_write)
        self.methods[inner_methods] = entry
        return entry

    def _open(self, _vfs, zname, pfile, flags, pout):
        try:
            rc = self._inner_open(self.inner_addr, zname, pfile, flags, pout)
        except BaseException as exc:  # noqa: BLE001 - never reach ctypes
            self._latch("xOpen", exc)
            return SQLITE_CANTOPEN
        try:
            self._note_thread()
            if rc != SQLITE_OK:
                self.counts["OPEN_FAILURES"] += 1
                return rc
            handle = _File.from_address(pfile)
            if not handle.pMethods:
                return rc
            copy, inner_write = self._methods_for(handle.pMethods)
            cls, temp = self._classify(flags, zname)
            self.files[pfile] = (cls, temp, inner_write)
            handle.pMethods = ctypes.addressof(copy)
            return rc
        except BaseException as exc:  # noqa: BLE001 - never reach ctypes
            # pMethods still names the inner table, so SQLite closes the file.
            self._latch("xOpen", exc)
            return SQLITE_CANTOPEN

    def _write(self, pfile, buf, n, offset):
        try:
            self._note_thread()
            record = self.files[pfile]
            self._count(pfile, record, n, offset)
            rc = record[2](pfile, buf, n, offset)
        except BaseException as exc:  # noqa: BLE001 - never reach ctypes
            self._latch("xWrite", exc)
            return SQLITE_IOERR_WRITE
        if rc != SQLITE_OK:
            self.counts["IO_ERRORS"] += 1
        return rc

    def snapshot(self):
        return dict(self.counts), len(self.errors)

    def disarm(self):
        """At interpreter exit only: route both callbacks back to the inner
        C functions so nothing calls into a finalizing interpreter."""
        self.disarmed = True
        self.vfs.xOpen = ctypes.cast(self._inner_open, ctypes.c_void_p).value
        for copy, inner_write in self.methods.values():
            copy.xWrite = ctypes.cast(inner_write, ctypes.c_void_p).value


def _library():
    if sys.implementation.name != "cpython":
        raise AccountingUnavailable("needs CPython, not %s"
                                    % sys.implementation.name)
    try:
        import _sqlite3
        lib = ctypes.CDLL(_sqlite3.__file__)
    except (ImportError, AttributeError, OSError) as exc:
        raise AccountingUnavailable("cannot load the sqlite3 module's "
                                    "library: %s" % exc) from None
    for name in _REQUIRED_SYMBOLS:
        if not hasattr(lib, name):
            raise AccountingUnavailable("SQLite symbol %s is missing" % name)
    lib.sqlite3_vfs_find.restype = ctypes.POINTER(_Vfs)
    lib.sqlite3_vfs_find.argtypes = (ctypes.c_char_p,)
    lib.sqlite3_vfs_register.argtypes = (ctypes.c_void_p, ctypes.c_int)
    lib.sqlite3_vfs_register.restype = ctypes.c_int
    lib.sqlite3_vfs_unregister.argtypes = (ctypes.c_void_p,)
    lib.sqlite3_vfs_unregister.restype = ctypes.c_int
    lib.sqlite3_file_control.argtypes = (ctypes.c_void_p, ctypes.c_char_p,
                                         ctypes.c_int, ctypes.c_void_p)
    lib.sqlite3_file_control.restype = ctypes.c_int
    lib.sqlite3_libversion_number.restype = ctypes.c_int
    lib.sqlite3_total_changes.argtypes = (ctypes.c_void_p,)
    lib.sqlite3_total_changes.restype = ctypes.c_int
    import sqlite3
    major, minor, patch = sqlite3.sqlite_version_info
    expected = major * 1000000 + minor * 1000 + patch
    found = lib.sqlite3_libversion_number()
    if found != expected:
        raise AccountingUnavailable(
            "the library ctypes reached (%d) is not the sqlite3 module's "
            "(%d)" % (found, expected))
    if found < _MIN_SQLITE:
        raise AccountingUnavailable("SQLite %d predates "
                                    "SQLITE_FCNTL_VFS_POINTER" % found)
    return lib


def _candidate_handle(conn):
    return ctypes.c_void_p.from_address(
        id(conn) + _HANDLE_WORDS * ctypes.sizeof(ctypes.c_void_p)).value


def _validate_layout(lib, inner_addr):
    """The connection's `sqlite3*` sits `_HANDLE_WORDS` words into the
    CPython object: its first field (`sqlite3.pVfs`) is the VFS the
    connection opened with, and SQLite's total-changes count for it matches
    the module's. The candidate is dereferenced only when it looks like a
    pointer."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    try:
        if type(conn) is not sqlite3.Connection:
            raise AccountingUnavailable("unexpected connection type")
        conn.execute("CREATE TABLE layout_probe(x)")
        conn.executemany("INSERT INTO layout_probe VALUES (?)",
                         [(i,) for i in range(7)])
        handle = _candidate_handle(conn)
        word = ctypes.sizeof(ctypes.c_void_p)
        if not handle or handle % word or handle < 65536:
            raise AccountingUnavailable("unsupported CPython connection layout")
        if ctypes.c_void_p.from_address(handle).value != inner_addr:
            raise AccountingUnavailable("unsupported CPython connection layout")
        if lib.sqlite3_total_changes(handle) != 7 or conn.total_changes != 7:
            raise AccountingUnavailable("unsupported CPython connection layout")
    finally:
        conn.close()


def _register():
    global _STATE
    lib = _library()
    inner = lib.sqlite3_vfs_find(None)
    if not inner:
        raise AccountingUnavailable("SQLite has no default VFS")
    vfs = inner.contents
    if vfs.zName and vfs.zName.decode() == WRAPPER_NAME:
        raise AccountingUnavailable("the default VFS is already wrapped")
    if lib.sqlite3_vfs_find(WRAPPER_NAME.encode()):
        # Registration happens once per process, so any VFS by this name
        # here is not this module's own (#940 F1 review #19).
        raise AccountingUnavailable(
            "a VFS named %s is already registered and is not this module's"
            % WRAPPER_NAME)
    if vfs.iVersion not in _SUPPORTED_VFS_VERSIONS:
        raise AccountingUnavailable("unsupported VFS version %d" % vfs.iVersion)
    if vfs.szOsFile <= 0 or vfs.mxPathname <= 0 or not vfs.zName:
        raise AccountingUnavailable("the default VFS is malformed")
    for name in _REQUIRED_VFS_METHODS:
        if not getattr(vfs, name):
            raise AccountingUnavailable("VFS method %s is missing" % name)
    _validate_layout(lib, ctypes.addressof(vfs))
    state = _State(lib, inner)
    if lib.sqlite3_vfs_register(state.addr, 0) != SQLITE_OK:
        raise AccountingUnavailable("sqlite3_vfs_register failed")
    import atexit
    atexit.register(state.disarm)
    _STATE = state
    return state


def _default_addr(state):
    found = state.lib.sqlite3_vfs_find(None)
    return ctypes.addressof(found.contents) if found else None


def activate():
    """Register once and make the wrapper the default VFS. Nested calls only
    count; a default that is neither the wrapped VFS nor the wrapper refuses
    (the wrapper never stacks)."""
    with _LOCK:
        state = _STATE if _STATE is not None else _register()
        if state.disarmed:
            raise AccountingUnavailable("the interpreter is finalizing")
        current = _default_addr(state)
        if current == state.addr:
            state.depth += 1
            return
        if current != state.inner_addr:
            raise AccountingUnavailable(
                "the default VFS changed under the accounting")
        if state.lib.sqlite3_vfs_register(state.addr, 1) != SQLITE_OK:
            raise AccountingUnavailable("sqlite3_vfs_register failed")
        state.depth = 1


def deactivate():
    """Release one activation; the last restores the previous default. Files
    already opened through the wrapper keep being counted."""
    with _LOCK:
        state = _STATE
        if state is None or state.depth == 0:
            raise AccountingError("write accounting is not active")
        state.depth -= 1
        if state.depth:
            return
        state.owner = None
        if _default_addr(state) != state.addr:
            raise AccountingError(
                "the default VFS changed under the accounting")
        rc = state.lib.sqlite3_vfs_register(state.inner_addr, 1)
        if rc != SQLITE_OK:
            # Fails closed (#940 F1 review #18): the wrapper stays the
            # default, which the next activation adopts.
            raise AccountingError(
                "restoring the inner VFS as the default failed with SQLite "
                "error %d" % rc)


@contextlib.contextmanager
def activated():
    activate()
    try:
        yield
    finally:
        deactivate()


def is_active():
    return (_STATE is not None and _STATE.depth > 0
            and _default_addr(_STATE) == _STATE.addr)


def _require_state(what):
    if _STATE is None:
        raise MeasurementInvalid("%s: write accounting is not active" % what)
    return _STATE


_QUERY_LIB = None


def _query_lib():
    """The validated library for read-only queries; registers nothing."""
    global _QUERY_LIB
    if _STATE is not None:
        return _STATE.lib
    if _QUERY_LIB is None:
        _QUERY_LIB = _library()
    return _QUERY_LIB


def default_vfs_name():
    found = _query_lib().sqlite3_vfs_find(None)
    return found.contents.zName.decode()


def inner_vfs_name():
    return _require_state("inner_vfs_name").inner_name


def registered_vfs_names():
    state = _require_state("registered_vfs_names")
    names = []
    node = ctypes.addressof(state.lib.sqlite3_vfs_find(None).contents)
    while node:
        vfs = _Vfs.from_address(node)
        names.append(vfs.zName.decode())
        node = vfs.pNext
    return names


def counters():
    """A copy of the process's cumulative counters."""
    return _require_state("counters").snapshot()[0]


# -- attribution ------------------------------------------------------------------

def _unwrap(conn):
    import sqlite3

    real = conn
    for _ in range(16):
        if isinstance(real, sqlite3.Connection):
            return real
        real = getattr(real, "_conn", None)
    raise NotAttributed("%r is not an sqlite3 connection" % (conn,))


def _db_handle(conn):
    return _candidate_handle(_unwrap(conn))


def _connection_vfs(conn, schema):
    state = _require_state("attribution")
    handle = _db_handle(conn)
    if not handle:
        return None, "the connection is closed"
    out = ctypes.c_void_p()
    rc = state.lib.sqlite3_file_control(handle, schema.encode(),
                                        SQLITE_FCNTL_VFS_POINTER,
                                        ctypes.byref(out))
    if rc != SQLITE_OK:
        return None, "schema %r has no file (rc=%d)" % (schema, rc)
    return out.value, None


def is_attributed(conn, schema="main"):
    """True when `schema` of `conn` was opened through the wrapper."""
    vfs, _why = _connection_vfs(conn, schema)
    return vfs is not None and vfs == _STATE.addr


def require_attributed(conn, label, schema="main"):
    vfs, why = _connection_vfs(conn, schema)
    if vfs is None:
        raise NotAttributed("%s: %s" % (label, why))
    if vfs != _STATE.addr:
        raise NotAttributed("%s: the connection bypasses the write accounting "
                            "VFS" % label)


# -- raw files through the installed default VFS ---------------------------------

class RawFile:
    """A file opened through the installed default VFS's own `xOpen`, written
    through its own `xWrite`: the positive control and the cumulative test."""

    def __init__(self, state, flags, path=None):
        found = state.lib.sqlite3_vfs_find(None)
        if not found or ctypes.addressof(found.contents) != state.addr:
            raise MeasurementInvalid("the write accounting VFS is not the "
                                     "default")
        self._buf = ctypes.create_string_buffer(state.vfs.szOsFile)
        self._name = (None if path is None else ctypes.create_string_buffer(
            str(path).encode() + b"\0\0\0"))
        out = ctypes.c_int()
        rc = _XOPEN(found.contents.xOpen)(
            state.addr,
            None if self._name is None else ctypes.addressof(self._name),
            ctypes.addressof(self._buf), flags, ctypes.byref(out))
        methods = _File.from_buffer(self._buf).pMethods
        self._methods = None if not methods else _IoMethods.from_address(methods)
        if rc != SQLITE_OK:
            self.close()
            raise OSError("xOpen failed with SQLite error %d" % rc)

    @property
    def _pfile(self):
        return ctypes.addressof(self._buf)

    def write(self, data, offset):
        """The inner write's SQLite result code."""
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        return _XWRITE(self._methods.xWrite)(self._pfile, buf, len(data),
                                             offset)

    def size(self):
        out = ctypes.c_int64()
        rc = _XFILESIZE(self._methods.xFileSize)(self._pfile,
                                                 ctypes.byref(out))
        if rc != SQLITE_OK:
            raise OSError("xFileSize failed with SQLite error %d" % rc)
        return out.value

    def close(self):
        if self._methods is not None:
            _XCLOSE(self._methods.xClose)(self._pfile)
            self._methods = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def raw_file(flags, path=None):
    return RawFile(_require_state("raw_file"), flags, path)


def _positive_control(state, label):
    flags = (CLASS_FLAG["TEMP_DB"] | OPEN_DELETEONCLOSE | OPEN_CREATE
             | OPEN_READWRITE | OPEN_EXCLUSIVE)
    expected = sum(n for _offset, n in CONTROL_WRITES)
    before, errors = state.snapshot()
    try:
        with RawFile(state, flags) as control:
            for offset, n in CONTROL_WRITES:
                if control.write(b"\xa5" * n, offset) != SQLITE_OK:
                    raise OSError("the control write failed")
    except (OSError, AccountingError) as exc:
        raise MeasurementInvalid("%s: write accounting positive control "
                                 "failed: %s" % (label, exc)) from None
    after, errors_after = state.snapshot()
    got = {k: after[k] - before[k] for k in ("TEMP", "TEMP_DB")}
    if got != {"TEMP": expected, "TEMP_DB": expected} or errors_after != errors:
        raise MeasurementInvalid(
            "%s: write accounting positive control failed: a %d-byte TEMP_DB "
            "file counted as %r" % (label, expected, got))


# -- measurements -------------------------------------------------------------------

class Writes:
    """One interval's directly counted writes, checked before use."""

    def __init__(self, label, by_class, temp, unclassified, writes, problems):
        self.label = label
        self.by_class = dict(by_class)
        self._temp = temp
        self.unclassified = unclassified
        self.writes = writes
        self.problems = list(problems)

    @classmethod
    def empty(cls, label):
        return cls(label, dict.fromkeys(CLASSES + (UNTYPED,), 0), 0, 0, 0, [])

    @classmethod
    def from_delta(cls, label, before, after):
        (b, b_errors), (a, a_errors) = before, after
        d = {k: a[k] - b[k] for k in _COUNTERS}
        problems = []
        if a_errors > b_errors:
            problems.append("callback error: " + "; ".join(
                _STATE.errors[b_errors:a_errors][:3]))
        if d["FOREIGN"]:
            problems.append("SQLite activity on another thread during the "
                            "measurement (%d callbacks)" % d["FOREIGN"])
        if d["IO_ERRORS"]:
            problems.append("an underlying I/O failure (%d writes)"
                            % d["IO_ERRORS"])
        if d["OPEN_FAILURES"]:
            problems.append("a file open failed (%d)" % d["OPEN_FAILURES"])
        if d["UNCLASSIFIED"]:
            problems.append("an unclassified write (%d bytes)"
                            % d["UNCLASSIFIED"])
        for name in UNEXPECTED_CLASSES:
            if d[name]:
                problems.append("an unexpected journal class: %s (%d bytes)"
                                % (name, d[name]))
        return cls(label, {k: d[k] for k in CLASSES + (UNTYPED,)}, d["TEMP"],
                   d["UNCLASSIFIED"], d["WRITES"], problems)

    @property
    def valid(self):
        return not self.problems

    def check(self):
        if self.problems:
            raise MeasurementInvalid("%s: write measurement invalid: %s"
                                     % (self.label, "; ".join(self.problems)))
        return self

    @property
    def temp(self):
        """Directly counted TEMP bytes; raises when the measurement is not
        valid."""
        return self.check()._temp

    def summary(self):
        nonzero = {k: v for k, v in self.by_class.items() if v}
        return {"label": self.label, "temp": self._temp, "classes": nonzero,
                "writes": self.writes, "problems": self.problems}

    def as_evidence(self):
        return {"schema": EVIDENCE_SCHEMA, "label": self.label,
                "temp": self._temp, "byClass": dict(self.by_class),
                "unclassified": self.unclassified, "writes": self.writes,
                "problems": list(self.problems)}

    @classmethod
    def from_evidence(cls, obj, label):
        def bad(why):
            return MeasurementInvalid("%s: child write evidence malformed: %s"
                                      % (label, why))

        def count(value):
            return (isinstance(value, int) and not isinstance(value, bool)
                    and value >= 0)

        keys = {"schema", "label", "temp", "byClass", "unclassified",
                "writes", "problems"}
        if not isinstance(obj, dict) or set(obj) != keys:
            raise bad("keys %r" % (sorted(obj) if isinstance(obj, dict)
                                   else type(obj).__name__))
        if obj["schema"] != EVIDENCE_SCHEMA:
            raise bad("schema %r" % (obj["schema"],))
        by_class = obj["byClass"]
        if (not isinstance(by_class, dict)
                or set(by_class) != set(CLASSES + (UNTYPED,))
                or not all(count(v) for v in by_class.values())):
            raise bad("byClass")
        for key in ("temp", "unclassified", "writes"):
            if not count(obj[key]):
                raise bad(key)
        if (not isinstance(obj["problems"], list)
                or not all(isinstance(p, str) for p in obj["problems"])
                or not isinstance(obj["label"], str)):
            raise bad("problems or label")
        return cls(obj["label"], by_class, obj["temp"], obj["unclassified"],
                   obj["writes"], obj["problems"])


class Interval:
    """One measured interval: `begin()` runs the positive control and takes
    ownership for the calling thread; `end()` returns its `Writes`."""

    def __init__(self, label):
        self.label = label
        self.writes = None
        self._before = None

    def begin(self):
        state = _STATE
        if state is None or not is_active():
            raise MeasurementInvalid("%s: write accounting is not active"
                                     % self.label)
        if state.owner is not None:
            raise MeasurementInvalid("%s: another measurement is open"
                                     % self.label)
        _positive_control(state, self.label)
        state.owner = threading.get_ident()
        self._before = state.snapshot()
        return self

    def end(self):
        if self._before is None:
            raise MeasurementInvalid("%s: the measurement never began"
                                     % self.label)
        state = _STATE
        after = state.snapshot()
        state.owner = None
        self.writes = Writes.from_delta(self.label, self._before, after)
        self._before = None
        return self.writes

    def __enter__(self):
        return self.begin()

    def __exit__(self, *exc):
        self.end()
        return False


def measure(label):
    return Interval(label)


# -- child processes ------------------------------------------------------------------

def publish(writes, **extra):
    """Print one evidence line a parent reads with `read_evidence`."""
    record = dict(extra)
    record["writes"] = writes.as_evidence()
    print(EVIDENCE_PREFIX + json.dumps(record, sort_keys=True), flush=True)


def read_evidence(text, label):
    lines = [line for line in (text or "").splitlines()
             if line.startswith(EVIDENCE_PREFIX)]
    if not lines:
        raise MeasurementInvalid("%s: child write evidence missing" % label)
    try:
        record = json.loads(lines[-1][len(EVIDENCE_PREFIX):])
    except ValueError as exc:
        raise MeasurementInvalid("%s: child write evidence malformed: %s"
                                 % (label, exc)) from None
    if not isinstance(record, dict):
        raise MeasurementInvalid("%s: child write evidence malformed: not an "
                                 "object" % label)
    return record


def child_writes(record, label):
    """The child's checked `Writes`; a child whose measured connection
    bypassed the wrapper fails attribution."""
    if (not isinstance(record, dict)
            or not isinstance(record.get("attributed"), bool)
            or "writes" not in record):
        raise MeasurementInvalid("%s: child write evidence malformed: %r"
                                 % (label, sorted(record) if isinstance(
                                     record, dict) else record))
    writes = Writes.from_evidence(record["writes"], label)
    if not record["attributed"]:
        raise NotAttributed("%s: the connection bypasses the write accounting "
                            "VFS" % label)
    return writes
