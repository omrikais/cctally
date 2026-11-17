"""#620 S1 — the account-scoped project reads degrade instead of raising.

`_load_week_snapshots` grew an `AND account_key = ?` predicate. The
structurally identical predicate in `_projects_week_grid` is wrapped in a
`try/except sqlite3.OperationalError` that returns `None`, because a stats.db
predating the column is a real shape: an installation whose migrations have
not run, or a dev binary refused the prod forward-migration (#142). Without
the same guard here, that store makes `cctally project --account <ref>` raise
instead of degrade.

Withholding is the correct degradation rather than dropping the predicate.
Retrying the query unscoped would sum another account's weekly percentage
into an account-filtered total, which is the exact defect #620 S1 removed —
a figure describing neither account.
"""
from __future__ import annotations

import contextlib
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    return ns


class _NoAccountColumnConn:
    """A stats connection that answers every query except one carrying the
    `account_key` predicate, the way SQLite answers a store whose column
    does not exist yet."""

    def __init__(self, real):
        self._real = real
        self.saw_unscoped_retry = False

    def execute(self, sql, params=()):
        if "account_key" in sql:
            raise sqlite3.OperationalError(
                "no such column: account_key"
            )
        if "weekly_usage_snapshots" in sql and "SELECT" in sql:
            self.saw_unscoped_retry = True
        return self._real.execute(sql, params)

    def close(self):
        """A no-op, because the TEST's reader seam owns the wrapped reader's
        lifetime: it closes each real statement reader when the `with` block
        `_load_week_snapshots` opens it in ends, and hands back this one
        wrapper every time so the flag below spans both calls.
        """

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_account_scoped_snapshot_read_degrades_on_a_missing_column(
    app, monkeypatch, tmp_path,
):
    """`_load_week_snapshots(account_key=...)` returns an empty mapping
    rather than propagating `sqlite3.OperationalError`."""
    import datetime as dt
    import _cctally_core
    import _cctally_project

    # #787 / #861 AD21: the read left `open_db` for a query-session statement
    # reader, so the wrapper sits on the real reader, at the command module's
    # own `_store_access` binding. The owner open first creates the store the
    # reader admits, as every installation has one.
    _cctally_core.open_db().close()
    wrapper = _NoAccountColumnConn(None)
    real_access = _cctally_project._store_access

    class _Access:
        def __getattr__(self, name):
            return getattr(real_access, name)

        @staticmethod
        @contextlib.contextmanager
        def statement_reader(store, *args, **kwargs):
            with real_access.statement_reader(store, *args, **kwargs) as reader:
                if store != "stats":
                    yield reader
                    return
                wrapper._real = reader
                yield wrapper

    monkeypatch.setattr(_cctally_project, "_store_access", _Access())

    # #750 S4 §2.1: the reducer takes the emitted SEGMENTS rather than a date
    # range, because the segment is the identity a percentage belongs to.
    import _lib_subscription_weeks
    segment = _lib_subscription_weeks.SubWeek(
        start_ts="2026-04-06T00:00:00+00:00",
        end_ts="2026-04-13T00:00:00+00:00",
        start_date=dt.date(2026, 4, 6),
        end_date=dt.date(2026, 4, 12),
        source="snapshot",
        display_start_date=dt.date(2026, 4, 6),
    )

    # Guard the guard: the merged read must NOT hit the wrapper's raise, or
    # the assertion below could pass for the wrong reason.
    merged = _cctally_project._load_week_snapshots([segment])
    assert merged == {}, merged
    assert wrapper.saw_unscoped_retry, (
        "the merged read must reach the unscoped query, or the wrapper is "
        "not intercepting what this test thinks it is"
    )
    wrapper.saw_unscoped_retry = False   # only the scoped call counts below

    got = _cctally_project._load_week_snapshots(
        [segment], account_key="claude:abc123",
    )
    assert got == {}, got
    assert not wrapper.saw_unscoped_retry, (
        "the scoped read must not fall back to an unscoped query — that "
        "sums another account's percentage into an account-filtered total"
    )


# --- Error precedence: a usage error still outranks an environment error ---
#
# Task 4 moved `resolve_account_filter` ahead of the range parse, because
# `_compute_subscription_weeks` needs the account context to build the
# default window. That reordering also moved which error a user sees first.
# Exit codes are contract (`docs/cli-contract.md`): a malformed `--since` is
# a native-usage error (2), an unavailable entry cache is a staged
# environment failure (3), and the usage error is the one the user can act
# on. The account context still has to be resolved before the interval is
# constructed; only the argument VALIDATION moves back in front.


def _project_exit(app, monkeypatch, argv, *, cache_down: bool):
    if cache_down:
        import _cctally_cache

        def _boom(*a, **k):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(_cctally_cache, "open_cache_db", _boom)
    return sys.modules["cctally"].main(argv)


def test_a_malformed_since_outranks_an_unavailable_cache(
    app, monkeypatch, capsys,
):
    """Both failures at once must report the malformed date, exit 2."""
    rc = _project_exit(
        app, monkeypatch,
        ["project", "--account", "unattributed", "--since", "not-a-date"],
        cache_down=True,
    )
    capsys.readouterr()
    assert rc == 2, (
        f"a malformed --since must exit 2 even when the entry cache is also "
        f"unavailable; got {rc}"
    )


def test_an_unavailable_cache_still_exits_3_on_its_own(
    app, monkeypatch, capsys,
):
    """Guard the guard: with valid arguments the cache failure is still the
    reported one, so the test above is about precedence rather than about
    having disabled the cache check."""
    rc = _project_exit(
        app, monkeypatch,
        ["project", "--account", "unattributed"],
        cache_down=True,
    )
    capsys.readouterr()
    assert rc == 3, rc
