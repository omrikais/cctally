"""#778 — the stats authorizer must be consulted at every write EXECUTION.

`sqlite3.Connection.set_authorizer` installs a callback SQLite invokes while it
COMPILES a statement, not while it runs one. Python's `sqlite3` module keeps a
per-connection cache of compiled statements (128 entries by default), so the
second `execute()` of a byte-identical SQL string re-uses the statement compiled
by the first and never calls the authorizer again.

`bin/_cctally_store.py`'s guard decides authorization from a `ContextVar` that is
only true inside `stats_write_scope`. Combining the two means a statement first
prepared inside a sanctioned scope carries that sanction for the rest of the
connection's life: the same SQL executed after the scope has exited, or from
another thread whose `ContextVar` was never set, writes without ever consulting
the guard.

Measured on this tree before the fix (Python 3.14.7 / SQLite 3.53.4): with a
default statement cache the post-scope INSERT committed and the row landed; with
`cached_statements=0` the same INSERT raised `sqlite3.DatabaseError: not
authorized` and no row landed.

WHY THESE CASES OPEN THROUGH `stats_open_guarded` RATHER THAN `sqlite3.connect`.
The fix is a connection CONSTRUCTION option, so a test that builds its own raw
connection cannot observe it and would stay red after the fix. Every connection
here therefore comes from the production opener, which is the single place that
arms the authorizer and now the single place that disables the statement cache.
`test_the_mechanism_is_the_construction_option_not_the_arming` is the one
deliberate raw-connection case, and it exists to pin WHY the option is what
enforces: the identical arming with a default cache still leaks.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import _cctally_core  # noqa: E402
import _cctally_journal as journal  # noqa: E402
import _cctally_store as store  # noqa: E402

#: Opt out of `tests/conftest.py::_stats_write_sanction`. That autouse fixture
#: declares the whole pytest process a sanctioned stats writer; this module's
#: entire subject is what the guard does with NO scope held, so it must run with
#: the guard live. `tests/test_stats_writer_guard_386.py` opts out for the same
#: reason.
CCTALLY_STATS_GUARD_LIVE = True

#: SQLite's own wording for an authorizer DENY. Matching on it is mandatory
#: rather than stylistic: `sqlite3.ProgrammingError` (raised when a connection
#: built with `check_same_thread=True` is used from another thread) is also a
#: `DatabaseError`, so a bare `pytest.raises(sqlite3.DatabaseError)` can pass
#: with the guard entirely disarmed. `tests/test_stats_writer_guard_386.py`
#: records that exact false positive.
_DENIED = "not authorized"

_TABLE = "weekly_usage_snapshots"


def _seed(path: pathlib.Path, ddl: str = f"CREATE TABLE {_TABLE} (v TEXT)"):
    """Create the schema on an UNARMED connection, then close it.

    `CREATE TABLE` is itself a guarded mutation, so seeding through the armed
    opener would need a write scope and would prove nothing about the subject.
    Production creates this schema under `stats_open_time_guard`; the point here
    is only that the table exists before the guarded connection opens.
    """
    raw = sqlite3.connect(str(path))
    try:
        raw.executescript(ddl)
        raw.commit()
    finally:
        raw.close()


def _guarded(tmp_path, *, name="stats.db", cross_thread=False, ddl=None):
    """A guarded stats connection built the way production builds one."""
    path = tmp_path / name
    _seed(path, ddl) if ddl is not None else _seed(path)
    if not cross_thread:
        return store.stats_open_guarded(path)
    return store.stats_open_guarded(
        path,
        connect=lambda p, **kw: sqlite3.connect(
            str(p), check_same_thread=False, **kw),
    )


def _rows(conn, table=_TABLE):
    return [r[0] for r in conn.execute(f"SELECT v FROM {table}")]


# ---------------------------------------------------------------------------
# The two reuse reproductions (#778). RED before the fix.
# ---------------------------------------------------------------------------


def test_cached_statement_does_not_carry_the_sanction_out_of_scope(tmp_path):
    """Identical SQL executed after the scope exits must be denied afresh."""
    conn = _guarded(tmp_path)
    try:
        with store.stats_write_scope("test-sanctioned"):
            conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("sanctioned",))
            conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match=_DENIED):
            conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("unsanctioned",))

        assert _rows(conn) == ["sanctioned"]
    finally:
        conn.close()


def test_cached_statement_does_not_cross_a_thread_boundary(tmp_path):
    """A thread that never entered the scope must be denied.

    `ContextVar` values do not propagate into a `threading.Thread`, so the
    guard's own answer for this thread is "unsanctioned". Only the statement
    cache can make the write succeed.
    """
    conn = _guarded(tmp_path, cross_thread=True)
    try:
        with store.stats_write_scope("test-sanctioned"):
            conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("sanctioned",))
            conn.commit()

        outcome: dict = {}

        def _from_another_thread():
            assert store.in_stats_write_scope() is False
            try:
                conn.execute(
                    f"INSERT INTO {_TABLE} VALUES (?)", ("other-thread",))
                outcome["raised"] = None
            except sqlite3.DatabaseError as exc:
                outcome["raised"] = str(exc)

        thread = threading.Thread(target=_from_another_thread)
        thread.start()
        thread.join()

        assert outcome["raised"] is not None, (
            "the write from another thread was not denied — the statement "
            "prepared inside the scope carried its sanction across the thread "
            "boundary"
        )
        assert _DENIED in outcome["raised"]
        assert _rows(conn) == ["sanctioned"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Controls. GREEN before AND after the fix.
# ---------------------------------------------------------------------------


def test_a_fresh_statement_outside_scope_is_still_denied(tmp_path):
    """The pre-#778 behaviour that already worked, restated as a control."""
    conn = _guarded(tmp_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match=_DENIED):
            conn.execute(f"INSERT INTO {_TABLE} VALUES ('never-prepared')")
        assert _rows(conn) == []
    finally:
        conn.close()


def test_a_sanctioned_write_is_still_allowed(tmp_path):
    conn = _guarded(tmp_path)
    try:
        with store.stats_write_scope("test-sanctioned"):
            conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("ok",))
            conn.commit()
        assert _rows(conn) == ["ok"]
    finally:
        conn.close()


def test_repeated_sanctioned_writes_are_all_allowed(tmp_path):
    """Disabling the cache must not turn a hot sanctioned loop into a denial."""
    conn = _guarded(tmp_path)
    try:
        with store.stats_write_scope("test-sanctioned"):
            for i in range(50):
                conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", (str(i),))
            conn.commit()
        assert len(_rows(conn)) == 50
    finally:
        conn.close()


def test_reads_and_temp_schema_are_unaffected(tmp_path):
    conn = _guarded(tmp_path)
    try:
        conn.execute(f"SELECT * FROM {_TABLE}").fetchall()
        conn.execute("CREATE TEMP TABLE scratch (v TEXT)")
        conn.execute("INSERT INTO scratch VALUES ('t')")
        assert _rows(conn, "scratch") == ["t"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Installed mode: the guard logs instead of raising, and must still SEE the
# reuse. A silent inheritance leaves the operator with no record at all.
# ---------------------------------------------------------------------------


def _installed_mode(tmp_path, monkeypatch) -> pathlib.Path:
    """Put the guard in its installed-build regime and return its log path.

    `_is_dev_checkout` — deliberately NOT `DEV_MODE`, which CLAUDE.md forbids
    collapsing into it — plus the absence of `PYTEST_CURRENT_TEST` are the two
    inputs to `_guard_should_raise`. This is the same setup
    `tests/test_stats_writer_guard_386.py::test_installed_build_logs_instead_of_raising`
    uses; it is reused rather than reinvented.
    """
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    log = tmp_path / "stats-writer-guard.log"
    monkeypatch.setattr(store, "_guard_log_path", lambda: log)
    monkeypatch.setattr(store, "_guard_last_logged", 0.0)
    return log


def test_installed_mode_reaches_the_throttled_diagnostic_on_reuse(
        tmp_path, monkeypatch):
    """Reuse outside the scope logs one throttled line and still writes.

    An installed build must never break a user's command, so the row lands.
    What #778 changes is that the guard is CONSULTED, which is what puts the
    incident in `logs/stats-writer-guard.log` and therefore in front of the
    doctor leg that reads it.
    """
    log = _installed_mode(tmp_path, monkeypatch)
    conn = _guarded(tmp_path)
    try:
        with store.stats_write_scope("test-sanctioned"):
            conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("sanctioned",))
            conn.commit()
        conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("logged",))
        conn.commit()
        assert _rows(conn) == ["sanctioned", "logged"]
    finally:
        conn.close()

    lines = log.read_text().splitlines()
    assert len(lines) == 1, lines
    assert "unsanctioned stats write" in lines[0]


def test_installed_mode_diagnostic_stays_throttled_under_reuse(
        tmp_path, monkeypatch):
    """Re-preparing every statement must not turn the log into a firehose.

    Before #778 a looping unsanctioned writer produced at most one line
    because the authorizer ran once. Now it runs on every execution, so the
    throttle is the only thing bounding the file — and this is the regression
    that says so.
    """
    log = _installed_mode(tmp_path, monkeypatch)
    conn = _guarded(tmp_path)
    try:
        with store.stats_write_scope("test-sanctioned"):
            conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("sanctioned",))
            conn.commit()
        for i in range(25):
            conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", (str(i),))
        conn.commit()
    finally:
        conn.close()

    assert len(log.read_text().splitlines()) == 1


def test_without_the_cache_option_installed_mode_never_sees_the_reuse(
        tmp_path, monkeypatch):
    """The pre-#778 counterfactual, kept executable rather than described.

    One difference from
    `test_installed_mode_reaches_the_throttled_diagnostic_on_reuse`: the
    opener is asked to build the connection the way it did before #778. The
    write still lands, and nothing is logged, because the cached statement is
    re-used and the authorizer is never called. That silence is what made the
    defect invisible on installed builds, and pinning it here is what stops a
    future reader from concluding the keyword is redundant.
    """
    monkeypatch.setattr(store, "_STATS_CONNECT_KWARGS", {})
    log = _installed_mode(tmp_path, monkeypatch)
    conn = _guarded(tmp_path)
    try:
        with store.stats_write_scope("test-sanctioned"):
            conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("sanctioned",))
            conn.commit()
        conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("unlogged",))
        conn.commit()
        assert _rows(conn) == ["sanctioned", "unlogged"]
    finally:
        conn.close()

    assert not log.exists(), log.read_text()


# ---------------------------------------------------------------------------
# #768 — the same enforcement, over `meter_rate_change_events`.
#
# Epoch 1011 created that table and epoch 1012 widened it, but it never joined
# `tests/test_stats_writer_surface_386.py::STATS_TABLES`, so its three write
# sites were outside the lexical freeze. These cases exercise the two DML sites
# through their real production helpers rather than through hand-written SQL,
# because a hand-written statement would prove that the AUTHORIZER works and
# nothing about whether this table's writers reach it.
# ---------------------------------------------------------------------------

_MRC_KEY = ("claude", "acct-778", "2026-09-01T00:00:00+00:00")


def _mrc_evt(*, effective_from=_MRC_KEY[2], with_evidence=False):
    payload = {
        "provider": _MRC_KEY[0],
        "account_key": _MRC_KEY[1],
        "effective_from": effective_from,
        "previous_units_per_point": 1.0,
        "new_units_per_point": 2.0,
        "severity": "info",
        "detected_at_utc": "2026-09-01T00:00:00+00:00",
        "created_at_utc": "2026-09-01T00:00:00+00:00",
    }
    if with_evidence:
        payload.update({
            "withholding_status": "none",
            "detector_input_causes": "[]",
            "composition_provenance": "[]",
            "baseline_withheld_days": 0,
        })
    return {"payload": payload, "at": "2026-09-01T00:00:00+00:00"}


def _mrc_guarded(tmp_path, *, cross_thread=False):
    """A guarded connection over the REAL `meter_rate_change_events` schema."""
    path = tmp_path / "mrc.db"
    raw = sqlite3.connect(str(path))
    try:
        _cctally_core._apply_quota_projection_schema(raw)
        raw.commit()
    finally:
        raw.close()
    if not cross_thread:
        return store.stats_open_guarded(path)
    return store.stats_open_guarded(
        path,
        connect=lambda p, **kw: sqlite3.connect(
            str(p), check_same_thread=False, **kw),
    )


def _mrc_count(conn):
    return conn.execute(
        "SELECT count(*) FROM meter_rate_change_events").fetchone()[0]


def test_meter_rate_change_fold_is_the_sanctioned_control(tmp_path):
    """The control: the real fold applier writes the row inside the scope."""
    conn = _mrc_guarded(tmp_path)
    try:
        with store.stats_write_scope("test-sanctioned"):
            journal._apply_meter_rate_change(conn, _mrc_evt())
            conn.commit()
        assert _mrc_count(conn) == 1
    finally:
        conn.close()


def test_meter_rate_change_insert_is_denied_after_a_sanctioned_one(tmp_path):
    """The parameterized INSERT re-prepares, so the second call is judged."""
    conn = _mrc_guarded(tmp_path)
    try:
        with store.stats_write_scope("test-sanctioned"):
            assert journal._insert_meter_rate_change(conn, _mrc_evt()) is True
            conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match=_DENIED):
            journal._insert_meter_rate_change(
                conn, _mrc_evt(effective_from="2026-09-08T00:00:00+00:00"))
        conn.rollback()
        assert _mrc_count(conn) == 1
    finally:
        conn.close()


def test_meter_rate_change_evidence_backfill_is_denied_after_a_sanctioned_one(
        tmp_path):
    """The COALESCE UPDATE is a second statement and gets judged separately.

    It is driven directly rather than through a duplicate insert, because the
    `INSERT OR IGNORE` would be denied first and the UPDATE would never be
    reached — which would leave this site untested while looking covered.
    """
    conn = _mrc_guarded(tmp_path)
    try:
        with store.stats_write_scope("test-sanctioned"):
            journal._insert_meter_rate_change(conn, _mrc_evt())
            # A second record for the same natural key: the insert is ignored
            # and the evidence backfill runs, preparing the UPDATE.
            assert journal._insert_meter_rate_change(
                conn, _mrc_evt(with_evidence=True)) is False
            conn.commit()
            row = journal._meter_rate_change_row(_mrc_evt(with_evidence=True))

        with pytest.raises(sqlite3.DatabaseError, match=_DENIED):
            journal._backfill_meter_rate_change_evidence(conn, row)
        conn.rollback()
    finally:
        conn.close()


def test_meter_rate_change_insert_is_denied_across_a_thread_boundary(tmp_path):
    conn = _mrc_guarded(tmp_path, cross_thread=True)
    try:
        with store.stats_write_scope("test-sanctioned"):
            journal._insert_meter_rate_change(conn, _mrc_evt())
            conn.commit()

        outcome: dict = {}

        def _from_another_thread():
            try:
                journal._insert_meter_rate_change(
                    conn, _mrc_evt(effective_from="2026-09-08T00:00:00+00:00"))
                outcome["raised"] = None
            except sqlite3.DatabaseError as exc:
                outcome["raised"] = str(exc)

        thread = threading.Thread(target=_from_another_thread)
        thread.start()
        thread.join()

        assert outcome["raised"] is not None, (
            "the metering-table write from another thread was not denied")
        assert _DENIED in outcome["raised"]
        conn.rollback()
        assert _mrc_count(conn) == 1
    finally:
        conn.close()


def test_meter_rate_change_reuse_reaches_the_installed_mode_diagnostic(
        tmp_path, monkeypatch):
    log = _installed_mode(tmp_path, monkeypatch)
    conn = _mrc_guarded(tmp_path)
    try:
        with store.stats_write_scope("test-sanctioned"):
            journal._insert_meter_rate_change(conn, _mrc_evt())
            conn.commit()
        journal._insert_meter_rate_change(
            conn, _mrc_evt(effective_from="2026-09-08T00:00:00+00:00"))
        conn.commit()
        assert _mrc_count(conn) == 2
    finally:
        conn.close()

    lines = log.read_text().splitlines()
    assert len(lines) == 1, lines
    assert "unsanctioned stats write" in lines[0]


def test_the_mechanism_is_the_construction_option_not_the_arming(tmp_path):
    """Arming alone does not enforce per execution; the cache option does.

    Two raw connections, identically armed, differing only in
    `cached_statements`. This is the executable form of the #778 diagnosis, so
    a future reader who removes the keyword from the opener as "redundant with
    the authorizer" gets a failing test rather than a silently leaking guard.
    """
    path = tmp_path / "raw.db"
    _seed(path)

    default_cache = sqlite3.connect(str(path))
    no_cache = sqlite3.connect(str(path), cached_statements=0)
    try:
        for conn in (default_cache, no_cache):
            store.arm_stats_authorizer(conn)
            with store.stats_write_scope("test-sanctioned"):
                conn.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("s",))
                conn.commit()

        # The cached statement is re-used and never re-authorized.
        default_cache.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("leak",))
        default_cache.commit()

        with pytest.raises(sqlite3.DatabaseError, match=_DENIED):
            no_cache.execute(f"INSERT INTO {_TABLE} VALUES (?)", ("denied",))
    finally:
        default_cache.close()
        no_cache.close()
