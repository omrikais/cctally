"""The retention instrument's direct write accounting (#940 spec revision 7j,
section 6.7).

`tests/_sqlite_write_accounting.py` wraps SQLite's default VFS through stdlib
`ctypes` and counts the bytes every file's `xWrite` passes to the inner VFS,
by the file class its `xOpen` flags name. These tests cover what applying
the A5s patches cannot prove: classification, overlapping flags, callback
errors, the activation lifecycle, bypass detection, the positive control,
the child evidence and cumulative accounting (every write summed, never the
file's extent).

Tests that need the wrapper installed request `accounting`; the few that
must run without it (import side effects, unsupported layouts, a subprocess
writer) start their own child process, so a failed activation in one test
can never leave this process half-registered.
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading

import pytest

TESTS = pathlib.Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import _sqlite_write_accounting as acct  # noqa: E402

KiB = 1024
TEMP_DOC = acct.OPEN_DELETEONCLOSE | acct.OPEN_CREATE | acct.OPEN_READWRITE \
    | acct.OPEN_EXCLUSIVE


@pytest.fixture
def accounting():
    with acct.activated():
        yield acct


def _child(source, *args, timeout=120):
    """Run `source` in a fresh interpreter with this directory importable."""
    prelude = ("import sys\nsys.path.insert(0, %r)\n" % str(TESTS))
    return subprocess.run([sys.executable, "-c", prelude + source, *args],
                          capture_output=True, text=True, timeout=timeout)


def _delta(before, after):
    return {k: after[k] - before[k] for k in after if after[k] != before[k]}


# -- registration and lifecycle ---------------------------------------------

def test_importing_the_module_registers_nothing():
    child = _child(
        "import ctypes, json, _sqlite3\n"
        "lib = ctypes.CDLL(_sqlite3.__file__)\n"
        "lib.sqlite3_vfs_find.restype = ctypes.c_void_p\n"
        "lib.sqlite3_vfs_find.argtypes = (ctypes.c_char_p,)\n"
        "before = lib.sqlite3_vfs_find(None)\n"
        "import _sqlite_write_accounting as a\n"
        "print(json.dumps({'same_default': lib.sqlite3_vfs_find(None) == before,\n"
        "  'wrapper': lib.sqlite3_vfs_find(a.WRAPPER_NAME.encode()),\n"
        "  'state': a._STATE is None}))\n")
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == {"same_default": True, "wrapper": None,
                                        "state": True}


def test_activation_is_idempotent_non_stacking_and_restores_the_default():
    child = _child(
        "import json, sqlite3, _sqlite_write_accounting as a\n"
        "out = {'first': a.default_vfs_name()}\n"
        "a.activate(); a.activate()\n"
        "out['active'] = a.default_vfs_name()\n"
        "out['inner'] = a.inner_vfs_name()\n"
        "out['names'] = a.registered_vfs_names()\n"
        "a.deactivate()\n"
        "out['after_one'] = a.default_vfs_name()\n"
        "a.deactivate()\n"
        "out['restored'] = a.default_vfs_name()\n"
        "a.activate()\n"
        "out['again'] = a.registered_vfs_names()\n"
        "a.deactivate()\n"
        "try:\n"
        "    a.deactivate()\n"
        "except a.AccountingError as exc:\n"
        "    out['extra'] = str(exc)\n"
        "print(json.dumps(out))\n")
    assert child.returncode == 0, child.stderr
    out = json.loads(child.stdout)
    assert out["active"] == acct.WRAPPER_NAME
    assert out["inner"] == out["first"] != acct.WRAPPER_NAME
    assert out["names"].count(acct.WRAPPER_NAME) == 1, out["names"]
    assert out["after_one"] == acct.WRAPPER_NAME
    assert out["restored"] == out["first"]
    assert out["again"].count(acct.WRAPPER_NAME) == 1, out["again"]
    assert "not active" in out["extra"]


def test_a_changed_default_vfs_refuses_to_stack():
    """Registration wraps one inner VFS once; a default that changed under it
    is refused rather than wrapped a second time."""
    child = _child(
        "import ctypes, _sqlite_write_accounting as a\n"
        "a.activate(); a.deactivate()\n"
        "st = a._STATE\n"
        "other = a._Vfs()\n"
        "ctypes.memmove(ctypes.byref(other), st.inner_addr, ctypes.sizeof(a._Vfs))\n"
        "other.zName = b'some-other-default'\n"
        "other.pNext = None\n"
        "assert st.lib.sqlite3_vfs_register(ctypes.addressof(other), 1) == 0\n"
        "try:\n"
        "    a.activate()\n"
        "except a.AccountingUnavailable as exc:\n"
        "    print('refused:', exc)\n"
        "st.lib.sqlite3_vfs_unregister(ctypes.addressof(other))\n")
    assert child.returncode == 0, child.stderr
    assert "refused:" in child.stdout
    assert "default VFS changed" in child.stdout


def test_registration_refuses_a_foreign_vfs_under_the_wrapper_name():
    """#940 F1 review #19: a VFS already registered under the wrapper's name
    that is not this module's own is refused, never shadowed or stacked."""
    child = _child(
        "import ctypes, _sqlite_write_accounting as a\n"
        "lib = a._library()\n"
        "inner = lib.sqlite3_vfs_find(None)\n"
        "other = a._Vfs()\n"
        "ctypes.memmove(ctypes.byref(other), ctypes.addressof(inner.contents),\n"
        "               ctypes.sizeof(a._Vfs))\n"
        "name = a.WRAPPER_NAME.encode()\n"
        "other.zName = name\n"
        "other.pNext = None\n"
        "assert lib.sqlite3_vfs_register(ctypes.addressof(other), 0) == 0\n"
        "try:\n"
        "    a.activate()\n"
        "except a.AccountingUnavailable as exc:\n"
        "    print('refused:', exc)\n"
        "else:\n"
        "    print('accepted')\n"
        "print('state:', a._STATE is None)\n"
        "lib.sqlite3_vfs_unregister(ctypes.addressof(other))\n")
    assert child.returncode == 0, child.stderr
    assert "refused:" in child.stdout, child.stdout
    assert "already registered" in child.stdout, child.stdout
    assert "state: True" in child.stdout, child.stdout


def test_a_failed_restoration_of_the_inner_vfs_fails_closed():
    """#940 F1 review #18: `deactivate()` checks the return code of making
    the inner VFS the default again, and raises when it fails."""
    child = _child(
        "import _sqlite_write_accounting as a\n"
        "a.activate()\n"
        "st = a._STATE\n"
        "real = st.lib\n"
        "class Lib:\n"
        "    def __getattr__(self, name):\n"
        "        return getattr(real, name)\n"
        "    def sqlite3_vfs_register(self, addr, make_default):\n"
        "        if addr == st.inner_addr:\n"
        "            return 1\n"
        "        return real.sqlite3_vfs_register(addr, make_default)\n"
        "st.lib = Lib()\n"
        "try:\n"
        "    a.deactivate()\n"
        "except a.AccountingError as exc:\n"
        "    print('refused:', exc)\n"
        "else:\n"
        "    print('restored silently')\n"
        "st.lib = real\n")
    assert child.returncode == 0, child.stderr
    assert "refused:" in child.stdout, child.stdout
    assert "restoring the inner VFS" in child.stdout, child.stdout


def test_wrapped_files_stay_counted_after_restoration(accounting, tmp_path):
    """Restoring the previous default never disables accounting underneath a
    live wrapped connection; a connection opened after restoration is not
    attributed."""
    wrapped = sqlite3.connect(tmp_path / "wrapped.db")
    try:
        assert acct.is_attributed(wrapped)
        acct.deactivate()
        try:
            late = sqlite3.connect(tmp_path / "late.db")
            try:
                assert not acct.is_attributed(late)
            finally:
                late.close()
            before = acct.counters()
            wrapped.execute("PRAGMA temp_store = FILE")
            wrapped.execute("PRAGMA temp.cache_size = 20")
            wrapped.execute("CREATE TEMP TABLE t(x)")
            wrapped.execute(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n "
                "WHERE i < 1000) INSERT INTO t SELECT randomblob(1000) FROM n")
            wrapped.commit()
            assert acct.counters()["TEMP"] - before["TEMP"] > 100 * KiB
        finally:
            acct.activate()
    finally:
        wrapped.close()


# -- classification -----------------------------------------------------------

@pytest.mark.parametrize("cls", ["TEMP_DB", "TEMP_JOURNAL", "TRANSIENT_DB",
                                 "SUBJOURNAL"])
def test_each_temp_class_counts_its_writes_once(accounting, cls):
    before = acct.counters()
    with acct.raw_file(acct.CLASS_FLAG[cls] | TEMP_DOC) as f:
        f.write(b"a" * 3000, 0)
        f.write(b"b" * 1000, 3000)
    delta = _delta(before, acct.counters())
    assert delta == {cls: 4000, "TEMP": 4000, "WRITES": 2}, delta


def test_a_named_main_database_write_is_not_temp(accounting, tmp_path):
    before = acct.counters()
    with acct.raw_file(acct.CLASS_FLAG["MAIN_DB"] | acct.OPEN_CREATE
                       | acct.OPEN_READWRITE, tmp_path / "main.db") as f:
        f.write(b"m" * 4096, 0)
    delta = _delta(before, acct.counters())
    assert delta == {"MAIN_DB": 4096, "WRITES": 1}, delta


def test_real_sqlite_traffic_lands_in_its_classes(accounting, tmp_path):
    """WAL frames and checkpoint copies are not temp; a rollback journal and
    a super-journal are their own classes; a FILE temp table and a statement
    journal are temp."""
    wal = sqlite3.connect(tmp_path / "wal.db", isolation_level=None)
    rollback = sqlite3.connect(tmp_path / "a.db", isolation_level=None)
    try:
        wal.execute("PRAGMA journal_mode=WAL")
        wal.execute("PRAGMA wal_autocheckpoint=0")
        wal.execute("CREATE TABLE w(x)")
        before = acct.counters()
        wal.execute("INSERT INTO w SELECT randomblob(700) FROM "
                    "(WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 "
                    "FROM n WHERE i < 600) SELECT i FROM n)")
        wal.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        delta = _delta(before, acct.counters())
        assert delta["WAL"] > 200 * KiB and delta["MAIN_DB"] > 200 * KiB, delta
        assert "TEMP" not in delta, delta

        rollback.execute("ATTACH ? AS b", (str(tmp_path / "b.db"),))
        rollback.execute("CREATE TABLE main.t(x)")
        rollback.execute("CREATE TABLE b.t(x)")
        before = acct.counters()
        rollback.execute("BEGIN")
        rollback.execute("INSERT INTO main.t VALUES (randomblob(5000))")
        rollback.execute("INSERT INTO b.t VALUES (randomblob(5000))")
        rollback.execute("COMMIT")
        delta = _delta(before, acct.counters())
        assert delta["MAIN_JOURNAL"] > 0 and delta["SUPER_JOURNAL"] > 0, delta
        assert "TEMP" not in delta, delta

        rollback.execute("PRAGMA temp_store = FILE")
        rollback.execute("PRAGMA temp.cache_size = 20")
        before = acct.counters()
        rollback.execute("CREATE TEMP TABLE s(x)")
        rollback.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n "
            "WHERE i < 1000) INSERT INTO s SELECT randomblob(1000) FROM n")
        delta = _delta(before, acct.counters())
        assert delta["TEMP_DB"] > 500 * KiB, delta
        assert delta["TEMP"] == delta["TEMP_DB"], delta
    finally:
        wal.close()
        rollback.close()


def test_overlapping_flags_are_counted_once(accounting, tmp_path):
    """A temp class that is also unnamed and delete-on-close is one write; a
    named delete-on-close main file is temp and keeps its class; two class
    bits are untyped (temp only when unnamed or delete-on-close); a named
    file with no class bit and no delete-on-close is unclassified."""
    base = acct.OPEN_CREATE | acct.OPEN_READWRITE
    cases = [
        (acct.CLASS_FLAG["TEMP_DB"] | TEMP_DOC, None,
         {"TEMP_DB": 100, "TEMP": 100}),
        (acct.CLASS_FLAG["MAIN_DB"] | base | acct.OPEN_DELETEONCLOSE,
         tmp_path / "doc.db", {"MAIN_DB": 100, "TEMP": 100}),
        (acct.CLASS_FLAG["TEMP_DB"] | acct.CLASS_FLAG["SUBJOURNAL"] | TEMP_DOC,
         None, {"UNTYPED": 100, "TEMP": 100}),
        (TEMP_DOC, None, {"UNTYPED": 100, "TEMP": 100}),
        (base, tmp_path / "plain.bin", {"UNTYPED": 100, "UNCLASSIFIED": 100}),
    ]
    for flags, path, expected in cases:
        before = acct.counters()
        with acct.raw_file(flags, path) as f:
            f.write(b"x" * 100, 0)
        delta = _delta(before, acct.counters())
        assert delta == {**expected, "WRITES": 1}, (hex(flags), delta)


# -- cumulative accounting (R7j-F1) -------------------------------------------

def test_cumulative_accounting_counts_every_write_not_the_file_extent(
        accounting):
    """Through the installed VFS's own xOpen of a TEMP_DB-class delete-on-close
    file and that file's xWrite: repeated and overlapping writes at the same
    offsets sum to more than the final size, and TEMP is exactly that sum."""
    writes = [(0, 4096), (0, 4096), (1024, 2048), (2048, 4096), (0, 512)]
    expected = sum(n for _offset, n in writes)
    assert acct.default_vfs_name() == acct.WRAPPER_NAME
    before = acct.counters()
    with acct.raw_file(acct.CLASS_FLAG["TEMP_DB"] | TEMP_DOC) as f:
        for offset, n in writes:
            f.write(os.urandom(n), offset)
        final_size = f.size()
    temp = acct.counters()["TEMP"] - before["TEMP"]
    assert final_size == 6144 < expected == 14848
    assert temp == expected and temp > final_size, (
        "cumulative accounting: TEMP must be the sum of every write",
        {"temp": temp, "sum": expected, "final_size": final_size})


# -- measurement validity -----------------------------------------------------

def test_a_measurement_counts_temp_and_passes_a_positive_control(
        accounting, tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    try:
        acct.require_attributed(conn, "measured connection")
        conn.execute("PRAGMA temp_store = FILE")
        conn.execute("PRAGMA temp.cache_size = 20")
        with acct.measure("temp table") as m:
            conn.execute("CREATE TEMP TABLE t(x)")
            conn.execute(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n "
                "WHERE i < 1000) INSERT INTO t SELECT randomblob(1000) FROM n")
            conn.commit()
        assert m.writes.valid, m.writes.problems
        assert m.writes.temp > 500 * KiB
        assert m.writes.temp == m.writes.by_class["TEMP_DB"]
    finally:
        conn.close()


def test_a_measurement_requires_the_wrapper_to_be_active(accounting):
    acct.deactivate()
    try:
        with pytest.raises(acct.MeasurementInvalid, match="not active"):
            acct.measure("inactive").begin()
    finally:
        acct.activate()


def test_blind_accounting_fails_the_positive_control(accounting, monkeypatch):
    monkeypatch.setattr(acct._STATE, "_count",
                        lambda pfile, record, n, offset: None)
    with pytest.raises(acct.MeasurementInvalid, match="positive control"):
        acct.measure("blind").begin()


def test_a_callback_exception_latches_and_returns_a_defined_error(
        accounting, tmp_path, monkeypatch, capfd):
    """xOpen and xWrite never let an exception reach ctypes: the measurement
    latches invalid and SQLite sees an ordinary error."""
    def boom(*_args):
        raise RuntimeError("injected bookkeeping failure")

    interval = acct.measure("open error")
    interval.begin()
    with monkeypatch.context() as m:
        m.setattr(acct._STATE, "_classify", boom)
        with pytest.raises(sqlite3.Error):
            sqlite3.connect(tmp_path / "e.db").execute("CREATE TABLE t(x)")
    writes = interval.end()
    assert not writes.valid and "callback error" in writes.problems[0], \
        writes.problems
    with pytest.raises(acct.MeasurementInvalid, match="callback error"):
        writes.temp

    conn = sqlite3.connect(tmp_path / "w.db")
    try:
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        interval = acct.measure("write error")
        interval.begin()
        with monkeypatch.context() as m:
            m.setattr(acct._STATE, "_count", boom)
            with pytest.raises(sqlite3.Error):
                conn.execute("INSERT INTO t VALUES (randomblob(9000))")
                conn.commit()
        writes = interval.end()
    finally:
        conn.close()
    assert not writes.valid and "callback error" in " ".join(writes.problems)
    assert "Exception ignored" not in capfd.readouterr().err

    with acct.measure("after the errors") as later:
        pass
    assert later.writes.valid, later.writes.problems


def test_an_io_failure_invalidates_the_measurement(accounting, tmp_path):
    path = tmp_path / "ro.db"
    path.write_bytes(b"")
    with acct.measure("io failure") as m:
        with acct.raw_file(acct.CLASS_FLAG["MAIN_DB"] | acct.OPEN_READONLY,
                           path) as f:
            assert f.write(b"z" * 512, 0) != 0
    assert not m.writes.valid
    assert any("I/O" in p for p in m.writes.problems), m.writes.problems


def test_unclassified_and_unexpected_journal_writes_invalidate(
        accounting, tmp_path):
    with acct.measure("unclassified") as m:
        with acct.raw_file(acct.OPEN_CREATE | acct.OPEN_READWRITE,
                           tmp_path / "u.bin") as f:
            f.write(b"u" * 64, 0)
    assert any("unclassified" in p for p in m.writes.problems), \
        m.writes.problems

    conn = sqlite3.connect(tmp_path / "j.db", isolation_level=None)
    try:
        conn.execute("CREATE TABLE t(x)")
        with acct.measure("rollback journal") as m:
            conn.execute("INSERT INTO t VALUES (randomblob(4000))")
    finally:
        conn.close()
    assert any("unexpected journal class" in p for p in m.writes.problems), \
        m.writes.problems


def test_concurrent_sqlite_activity_invalidates_the_measurement(
        accounting, tmp_path):
    def write():
        c = sqlite3.connect(tmp_path / "thread.db")
        try:
            c.execute("CREATE TABLE t(x)")
            c.execute("INSERT INTO t VALUES (randomblob(9000))")
            c.commit()
        finally:
            c.close()

    with acct.measure("threaded") as m:
        worker = threading.Thread(target=write)
        worker.start()
        worker.join()
    assert any("another thread" in p for p in m.writes.problems), \
        m.writes.problems


# -- attribution ----------------------------------------------------------------

def test_a_connection_opened_before_installation_is_not_attributed(
        accounting, tmp_path):
    acct.deactivate()
    try:
        early = sqlite3.connect(tmp_path / "early.db")
    finally:
        acct.activate()
    try:
        assert not acct.is_attributed(early)
        with pytest.raises(acct.NotAttributed,
                           match="early: the connection bypasses the write "
                                 "accounting VFS"):
            acct.require_attributed(early, "early")
        before = acct.counters()
        early.execute("PRAGMA temp_store = FILE")
        early.execute("PRAGMA temp.cache_size = 20")
        early.execute("CREATE TEMP TABLE t(x)")
        early.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n "
            "WHERE i < 1000) INSERT INTO t SELECT randomblob(1000) FROM n")
        early.commit()
        assert acct.counters()["TEMP"] == before["TEMP"], (
            "a bypassing connection's spill is invisible: only attribution "
            "can catch it")
    finally:
        early.close()


def test_an_explicit_inner_vfs_and_attached_databases_are_checked(
        accounting, tmp_path):
    uri = "file:%s?vfs=%s" % (tmp_path / "inner.db", acct.inner_vfs_name())
    inner = sqlite3.connect(uri, uri=True)
    wrapped = sqlite3.connect("file:%s" % (tmp_path / "wrapped.db"), uri=True)
    try:
        assert not acct.is_attributed(inner)
        assert acct.is_attributed(wrapped)
        wrapped.execute("ATTACH ? AS aux", (str(tmp_path / "aux.db"),))
        wrapped.execute("ATTACH ? AS other",
                        ("file:%s?vfs=%s" % (tmp_path / "other.db",
                                             acct.inner_vfs_name()),))
        assert acct.is_attributed(wrapped, schema="aux")
        assert not acct.is_attributed(wrapped, schema="other")
        assert not acct.is_attributed(wrapped, schema="missing")
    finally:
        inner.close()
        wrapped.close()
    with pytest.raises(acct.NotAttributed, match="closed"):
        acct.require_attributed(wrapped, "closed")


def test_a_wrapper_object_is_unwrapped_for_attribution(accounting, tmp_path):
    class Spy:
        def __init__(self, conn):
            self._conn = conn

    conn = sqlite3.connect(tmp_path / "spy.db")
    try:
        assert acct.is_attributed(Spy(Spy(conn)))
    finally:
        conn.close()


# -- unsupported runtimes, child evidence ---------------------------------------

@pytest.mark.parametrize("mutation, text", [
    pytest.param("a._HANDLE_WORDS = 1", "CPython connection layout",
                 id="layout"),
    pytest.param("a._SUPPORTED_VFS_VERSIONS = ()", "VFS version",
                 id="vfs-version"),
    pytest.param("a._REQUIRED_SYMBOLS = a._REQUIRED_SYMBOLS + "
                 "('sqlite3_no_such_symbol',)", "sqlite3_no_such_symbol",
                 id="symbol"),
])
def test_an_unsupported_runtime_fails_activation(mutation, text):
    child = _child(
        "import _sqlite_write_accounting as a\n"
        + mutation + "\n"
        "try:\n"
        "    a.activate()\n"
        "except a.AccountingUnavailable as exc:\n"
        "    print('UNAVAILABLE', exc)\n"
        "else:\n"
        "    print('ACTIVATED')\n")
    assert child.returncode == 0, child.stderr
    assert child.stdout.startswith("UNAVAILABLE write accounting unavailable"), \
        child.stdout
    assert text in child.stdout


def test_evidence_round_trips_and_malformed_evidence_fails(accounting):
    with acct.measure("evidence") as m:
        pass
    evidence = m.writes.as_evidence()
    again = acct.Writes.from_evidence(json.loads(json.dumps(evidence)), "x")
    assert again.as_evidence() == evidence
    for broken in ({}, {**evidence, "temp": "0"},
                   {**evidence, "byClass": {}},
                   {**evidence, "schema": 99}):
        with pytest.raises(acct.MeasurementInvalid,
                           match="child write evidence malformed"):
            acct.Writes.from_evidence(broken, "child")
    with pytest.raises(acct.MeasurementInvalid,
                       match="child write evidence missing"):
        acct.read_evidence("no evidence here\n", "child")


def test_a_subprocess_writer_publishes_validated_counters(tmp_path):
    """The child path the consumers' crash and checkpoint children use: a
    fresh process activates, measures, and publishes evidence the parent
    validates. Needs no wrapper in this process."""
    child = _child(
        "import sqlite3, _sqlite_write_accounting as a\n"
        "a.activate()\n"
        "m = a.measure('child temp table')\n"
        "m.begin()\n"
        "c = sqlite3.connect(sys.argv[1])\n"
        "c.execute('PRAGMA temp_store = FILE')\n"
        "c.execute('PRAGMA temp.cache_size = 20')\n"
        "c.execute('CREATE TEMP TABLE t(x)')\n"
        "c.execute('WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 '\n"
        "          'FROM n WHERE i < 1000) INSERT INTO t '\n"
        "          'SELECT randomblob(1000) FROM n')\n"
        "c.commit()\n"
        "a.publish(m.end(), attributed=a.is_attributed(c))\n"
        "c.close()\n",
        str(tmp_path / "child.db"))
    record = acct.read_evidence(child.stdout + child.stderr, "child writer")
    writes = acct.child_writes(record, "child writer")
    assert child.returncode == 0, child.stderr
    assert writes.valid and writes.temp > 500 * KiB, writes.summary()


def test_a_child_that_bypasses_the_wrapper_fails_through_its_evidence():
    record = {"attributed": False,
              "writes": acct.Writes.empty("bypass").as_evidence()}
    with pytest.raises(acct.NotAttributed,
                       match="bypass child: the connection bypasses the write "
                             "accounting VFS"):
        acct.child_writes(record, "bypass child")
    with pytest.raises(acct.MeasurementInvalid,
                       match="child write evidence malformed"):
        acct.child_writes({"writes": {}}, "broken child")


def test_the_handle_layout_matches_the_connection(accounting, tmp_path):
    """The validated CPython layout reaches the same sqlite3* SQLite reports
    for the connection (total changes agree)."""
    conn = sqlite3.connect(tmp_path / "layout.db")
    try:
        conn.execute("CREATE TABLE t(x)")
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(7)])
        handle = acct._db_handle(conn)
        fn = acct._STATE.lib.sqlite3_total_changes
        assert fn(ctypes.c_void_p(handle)) == conn.total_changes == 7
    finally:
        conn.close()
