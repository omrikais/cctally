"""Which credit epoch governs a capture (#703 + #707 §5.3).

`ORDER BY id DESC` is gone, and it was not merely imprecise but unusable. Row
identifiers are projection-local and a rebuild reassigns them; worse, a rebuild
sorts by fold family before sequence, and after unification a manual credit folds
at op order 5 while an automatic one folds at event order 30. So identifier order
after a rebuild can reverse the real chronology of two credits, and a reader
would then show a different epoch from the one the writer used.

The ordering is the accounting instant first, `credit_order` second and
`credit_key` last. `credit_order` records a fact of the SOURCE record rather than
the fold position of the derived row, because fold orders express dependency and
not occurrence. `credit_key` is the final deterministic fallback for legacy rows
and exact ties.

Selection is by the WEEK, not by a boundary column. A manual credit has both
boundary columns NULL — it moved no boundary and has none to record — so a
resolver keyed on `new_week_end_at` would not see one at all, and a manual credit
opening no milestone epoch is precisely the defect unification removes. The
boundary match is retained only for a row written before `week_start_date`
existed.
"""
from __future__ import annotations

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


WEEK_START_DATE = "2026-08-29"
WEEK_END_AT = "2026-09-05T00:00:00+00:00"
ACCOUNT = "unattributed"


def _insert(conn, *, credit_key, observed=None, effective, credit_order=None,
            account_key=ACCOUNT, week_start_date=WEEK_START_DATE,
            boundaries=True):
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
        " week_start_date, observed_at_utc, observed_post_credit_pct, "
        " credit_key, credit_order) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (effective, effective if boundaries else None,
         WEEK_END_AT if boundaries else None, effective, 40.0, account_key,
         week_start_date, observed, 5.0, credit_key, credit_order))


def _resolve(conn, captured_at, **over):
    import _lib_credit_selection as sel
    kwargs = dict(week_start_date=WEEK_START_DATE, account_key=ACCOUNT,
                  captured_at=captured_at, week_end_at=WEEK_END_AT)
    kwargs.update(over)
    return sel.resolve_weekly_credit_epoch(conn, **kwargs)


def test_the_resolver_follows_chronology_not_row_identifiers(ns):
    """Identifier 2 carries the EARLIER accounting instant."""
    conn = ns["open_db"]()
    try:
        _insert(conn, credit_key="sa:o:later",
                effective="2026-08-30T12:00:00+00:00",
                observed="2026-08-30T12:04:00Z", credit_order=20)
        _insert(conn, credit_key="sa:o:earlier",
                effective="2026-08-30T09:00:00+00:00",
                observed="2026-08-30T09:04:00Z", credit_order=10)
        conn.commit()
        got = _resolve(conn, "2026-08-30T13:00:00Z")
        assert got is not None
        assert got["credit_key"] == "sa:o:later", (
            "`id DESC` chose the earlier event")
    finally:
        conn.close()


def test_the_choice_does_not_depend_on_physical_insert_order(ns, tmp_path,
                                                             monkeypatch):
    """What a rebuild does to identifiers, modelled directly.

    A rebuild materializes the same credits under new identifiers, sorted by
    fold family before sequence. Inserting the same two rows in the opposite
    physical order is that renumbering, and the resolver must be blind to it.
    """
    keys = []
    for order in (("a", "b"), ("b", "a")):
        other = load_script()
        redirect_paths(other, monkeypatch, tmp_path / order[0])
        conn = other["open_db"]()
        try:
            spec = {
                "a": dict(credit_key="sa:o:aaa",
                          effective="2026-08-30T09:00:00+00:00",
                          observed="2026-08-30T09:04:00Z", credit_order=10),
                "b": dict(credit_key="sa:o:bbb",
                          effective="2026-08-30T12:00:00+00:00",
                          observed="2026-08-30T12:04:00Z", credit_order=20),
            }
            for which in order:
                _insert(conn, **spec[which])
            conn.commit()
            got = _resolve(conn, "2026-08-30T13:00:00Z")
            assert got is not None
            keys.append(got["credit_key"])
        finally:
            conn.close()
    assert keys == ["sa:o:bbb", "sa:o:bbb"], keys


def test_a_capture_before_every_credit_resolves_to_none(ns):
    conn = ns["open_db"]()
    try:
        _insert(conn, credit_key="sa:o:one",
                effective="2026-08-30T12:00:00+00:00",
                observed="2026-08-30T12:04:00Z", credit_order=20)
        conn.commit()
        assert _resolve(conn, "2026-08-30T11:00:00Z") is None
    finally:
        conn.close()


def test_a_manual_credit_opens_an_epoch(ns):
    """Both boundary columns NULL, so a `new_week_end_at` predicate finds
    nothing — and a manual credit opening no epoch is the defect unification
    removes."""
    conn = ns["open_db"]()
    try:
        _insert(conn, credit_key="o:manualop",
                effective="2026-08-30T09:00:00+00:00",
                observed="2026-08-30T09:12:00Z", credit_order=10,
                boundaries=False)
        conn.commit()
        got = _resolve(conn, "2026-08-30T13:00:00Z")
        assert got is not None and got["credit_key"] == "o:manualop"
    finally:
        conn.close()


def test_a_legacy_row_without_a_week_is_found_by_its_boundary(ns):
    """A row written before `week_start_date` existed carries only the
    boundary, so that match is retained for exactly those rows."""
    conn = ns["open_db"]()
    try:
        _insert(conn, credit_key=None,
                effective="2026-08-30T09:00:00+00:00", week_start_date=None)
        conn.commit()
        got = _resolve(conn, "2026-08-30T13:00:00Z")
        assert got is not None
        assert got["effective_reset_at_utc"] == "2026-08-30T09:00:00+00:00"
    finally:
        conn.close()


def test_the_accounting_instant_beats_the_display_instant(ns):
    """A credit whose effective instant is EARLIER can still be the later one,
    because the effective instant is hour-floored and display-only."""
    conn = ns["open_db"]()
    try:
        _insert(conn, credit_key="sa:o:floored",
                effective="2026-08-30T12:00:00+00:00", observed=None,
                credit_order=10)
        _insert(conn, credit_key="sa:o:exact",
                effective="2026-08-30T11:00:00+00:00",
                observed="2026-08-30T12:30:00Z", credit_order=20)
        conn.commit()
        got = _resolve(conn, "2026-08-30T13:00:00Z")
        assert got is not None and got["credit_key"] == "sa:o:exact"
    finally:
        conn.close()


def test_a_tie_on_the_instant_falls_through_to_credit_order(ns):
    conn = ns["open_db"]()
    try:
        _insert(conn, credit_key="sa:o:aaa",
                effective="2026-08-30T12:00:00+00:00",
                observed="2026-08-30T12:04:00Z", credit_order=99)
        _insert(conn, credit_key="sa:o:zzz",
                effective="2026-08-30T12:00:00+00:00",
                observed="2026-08-30T12:04:00Z", credit_order=1)
        conn.commit()
        got = _resolve(conn, "2026-08-30T13:00:00Z")
        assert got is not None and got["credit_key"] == "sa:o:aaa"
    finally:
        conn.close()


def test_a_tie_on_both_falls_through_to_the_credit_key(ns):
    """The final deterministic fallback. It is content-hash order rather than
    chronological order, which is one of the two losses spec §5.3 records."""
    conn = ns["open_db"]()
    try:
        for key in ("sa:o:aaa", "sa:o:zzz"):
            _insert(conn, credit_key=key,
                    effective="2026-08-30T12:00:00+00:00",
                    observed="2026-08-30T12:04:00Z", credit_order=7)
        conn.commit()
        got = _resolve(conn, "2026-08-30T13:00:00Z")
        assert got is not None and got["credit_key"] == "sa:o:zzz"
    finally:
        conn.close()


def test_the_resolver_is_account_scoped(ns):
    conn = ns["open_db"]()
    try:
        _insert(conn, credit_key="sa:o:a", account_key="acct-a",
                effective="2026-08-30T12:00:00+00:00",
                observed="2026-08-30T12:04:00Z", credit_order=20)
        conn.commit()
        assert _resolve(conn, "2026-08-30T13:00:00Z",
                        account_key="acct-b") is None
        got = _resolve(conn, "2026-08-30T13:00:00Z", account_key="acct-a")
        assert got is not None and got["credit_key"] == "sa:o:a"
    finally:
        conn.close()


def test_the_ordering_names_no_row_identifier():
    """The contract itself, read out of the module.

    A resolver that ordered by `id` would be correct on the writer's store and
    wrong on every rebuilt one, and nothing in the output would say so.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("bin/_lib_credit_selection.py").read_text())
    docstrings = set()
    for node in ast.walk(tree):
        # `body` is a list on a Module, a function and a class, and a single
        # expression on a conditional expression — so subscripting it blindly
        # raises the moment the module grows one `a if b else c`.
        body = getattr(node, "body", None)
        body = body if isinstance(body, list) else []
        first = body[0] if body else None
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
                and isinstance(first, ast.Expr)
                and isinstance(getattr(first, "value", None), ast.Constant)
                and isinstance(first.value.value, str)):
            docstrings.add(id(first.value))
    sql = " ".join(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ).upper()
    assert "ORDER BY" in sql
    assert " ID DESC" not in sql, sql
    assert " ID ASC" not in sql, sql
