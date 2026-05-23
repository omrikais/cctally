"""_probe_table_nonempty: shared 'is there data here?' probe (cctally-dev#93)."""
import importlib.util
import pathlib
import sqlite3
import sys

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parents[1] / "bin"
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))
_spec = importlib.util.spec_from_file_location("_cctally_db", _BIN_DIR / "_cctally_db.py")
_db = importlib.util.module_from_spec(_spec)
sys.modules["_cctally_db"] = _db
_spec.loader.exec_module(_db)


def test_missing_table_is_false():
    conn = sqlite3.connect(":memory:")
    assert _db._probe_table_nonempty(conn, "nope") is False


def test_empty_table_is_false():
    conn = sqlite3.connect(":memory:"); conn.execute("CREATE TABLE t(x)")
    assert _db._probe_table_nonempty(conn, "t") is False


def test_nonempty_table_is_true():
    conn = sqlite3.connect(":memory:"); conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    assert _db._probe_table_nonempty(conn, "t") is True


class _TransientExecConn:
    """Minimal connection stand-in whose ``.execute`` always raises a
    transient-style ``sqlite3.OperationalError`` ("database is locked").

    Used to pin the contract that ``_probe_table_nonempty`` swallows ONLY
    ``no such table`` and re-raises every other ``OperationalError`` —
    including the transient BUSY/LOCKED case. The gate shell's inline
    ``cache_has_entries`` read deliberately does NOT use this helper for
    exactly that reason: it must catch a transient lock and flip
    ``marker_state_readable=False`` rather than let it propagate. If the
    helper ever started swallowing all ``OperationalError``s, that gate
    decision would silently turn a transient lock into ``cache_has_entries
    is False`` instead of a row-1 DEFER. This test fails closed on that.
    """

    def execute(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")


def test_transient_operational_error_propagates():
    # A non-``no such table`` OperationalError (e.g. BUSY/LOCKED) must be
    # re-raised, not turned into False. Contract the gate shell depends on.
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _db._probe_table_nonempty(_TransientExecConn(), "t")
