"""_probe_table_nonempty: shared 'is there data here?' probe (cctally-dev#93)."""
import importlib.util
import pathlib
import sqlite3
import sys

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
