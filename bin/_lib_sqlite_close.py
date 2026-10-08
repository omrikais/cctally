"""Per-connection SQLite checkpoint-on-close control (#901 spec §5.4).

The compatibility helper that `_cctally_cache._set_cache_no_checkpoint_on_close`
introduced for cache recovery, generalized to any connection. Python 3.12+
reaches `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE` through `Connection.setconfig`;
CPython 3.11 reaches the same SQLite API through its supported
`pysqlite_Connection` layout and the stdlib extension's own linked
`sqlite3_db_config`, so a different SQLite instance is never bound.
Stdlib-only leaf.
"""
from __future__ import annotations

import sqlite3
import sys

#: `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE` from sqlite3.h.
SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE = 1006


def set_no_checkpoint_on_close(conn: sqlite3.Connection, disabled: bool, *,
                               purpose: str = "connection") -> None:
    """Turn SQLite's checkpoint at the last close off (``disabled=True``) or
    back on; raise `sqlite3.NotSupportedError` when it cannot be applied."""
    option = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
    setconfig = getattr(conn, "setconfig", None)
    if option is not None and setconfig is not None:
        setconfig(option, bool(disabled))
        getconfig = getattr(conn, "getconfig", None)
        if getconfig is not None and bool(getconfig(option)) != bool(disabled):
            raise sqlite3.NotSupportedError(
                f"{purpose} could not configure SQLite close checkpointing")
        return
    set_no_checkpoint_on_close_cpython(conn, disabled, purpose=purpose)


def set_no_checkpoint_on_close_cpython(conn: sqlite3.Connection,
                                       disabled: bool, *,
                                       purpose: str = "connection") -> None:
    """Python 3.11 compatibility adapter for sqlite3_db_config()."""
    if sys.implementation.name != "cpython":
        raise sqlite3.NotSupportedError(
            f"{purpose} requires SQLite no-checkpoint-on-close support")

    # CPython 3.11's public sqlite3 module does not expose db_config(), but its
    # connection layout begins with PyObject_HEAD followed by ``sqlite3 *db``.
    # Load sqlite3_db_config from the same extension so we never bind a
    # different SQLite instance.
    import _sqlite3
    import ctypes

    sqlite_lib = ctypes.CDLL(_sqlite3.__file__)
    db_config = sqlite_lib.sqlite3_db_config
    db_config.argtypes = (ctypes.c_void_p, ctypes.c_int)
    db_config.restype = ctypes.c_int
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    db_pointer = ctypes.c_void_p.from_address(
        id(conn) + (2 * pointer_size)
    ).value
    if not db_pointer:
        raise sqlite3.NotSupportedError(
            f"{purpose} could not resolve the SQLite connection handle")
    current = ctypes.c_int()
    rc = db_config(
        ctypes.c_void_p(db_pointer),
        SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,
        ctypes.c_int(1 if disabled else 0),
        ctypes.byref(current),
    )
    if rc != sqlite3.SQLITE_OK or current.value != int(bool(disabled)):
        raise sqlite3.NotSupportedError(
            f"{purpose} could not configure SQLite close checkpointing")


def no_checkpoint_on_close_enabled(conn: sqlite3.Connection) -> "bool | None":
    """The setting as SQLite reports it, or None where it cannot be read
    (CPython 3.11, whose adapter verifies at set time instead)."""
    option = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
    getconfig = getattr(conn, "getconfig", None)
    if option is None or getconfig is None:
        return None
    return bool(getconfig(option))
