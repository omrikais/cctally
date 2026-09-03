"""No pre-credit tolerance band survives anywhere in `bin/` (#703 + #707).

The 2026-09-01 incident is what a band does when the two quantities it compares
are different things. On the debounced confirmation leg the remembered level is
the armed marker's `baseline_pct` while the rows to remove hold what was
actually written to `weekly_usage_snapshots`. They differed by exactly 1.0, the
strict `< 1.0` excluded every row, and the DELETE did nothing — after which the
surviving row held the reset-aware high-water mark and no genuine reading could
land.

A band cannot be repaired by widening it, because the two quantities have no
bounded relationship. It is replaced by evidence: a bracket closed by two
observations on the automatic path, and the asserted level on the manual one.

Both halves are asserted here. A static assertion that no such predicate remains
in `bin/`, and a behavioural one that every path that selects stale replicas
reaches the shared selectors — the static half alone would pass over a caller
that had quietly rewritten the predicate in a shape the regex does not match.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from conftest import load_script, redirect_paths


BIN = pathlib.Path("bin")

#: A comparison of the WEEKLY percent column against a remembered level within
#: a tolerance. Written to match the shape rather than one spelling: the operand
#: may be a bound parameter or a literal, and the bound may be any number.
#:
#: Scoped to `weekly_percent` on purpose. The 5h in-place credit path carries a
#: band of its own over `five_hour_percent`, and this change does not touch that
#: subsystem — asserting over it would claim a scope the work does not have.
_BAND = re.compile(
    r"ABS\(\s*weekly_percent\s*-\s*[^)]+\)\s*[<>]=?\s*[0-9.]+",
    re.IGNORECASE)


def _sql_strings(path, text):
    """Every string constant in a source that is not a docstring.

    Prose ABOUT the band is expected: several docstrings name the predicate they
    replaced, and a search that could not tell explanation from predicate would
    either fail on the explanation or drive it out of the code. Docstrings are
    excluded by identity — the first statement of a module, function or class.

    A file that does not parse as Python (a shell harness) falls back to a line
    scan with comment lines removed, which is the best a non-Python source
    admits.
    """
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [
            line for line in text.splitlines()
            if not line.lstrip().startswith(("#", "--"))
        ]
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        first = body[0] if body else None
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docstrings.add(id(first.value))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _sources():
    for path in sorted(BIN.iterdir()):
        if path.is_dir():
            continue
        try:
            yield path, path.read_text()
        except (OSError, UnicodeDecodeError):
            continue


def test_no_pre_credit_tolerance_band_remains_in_bin():
    offenders = []
    for path, text in _sources():
        for chunk in _sql_strings(path, text):
            if _BAND.search(chunk):
                offenders.append(f"{path}: {chunk.strip()[:120]}")
    assert offenders == [], (
        "a weekly-percent tolerance band is still present:\n"
        + "\n".join(offenders))


def test_the_static_assertion_would_see_the_predicate_it_forbids():
    """Non-vacuity. The regex has to match the predicate as it was actually
    written, split across adjacent string literals and all — a search that
    matches nothing passes silently and certifies nothing."""
    assert _BAND.search(
        "  AND ABS(weekly_percent - ?) < 1.0")
    assert _BAND.search(
        "WHERE week_start_date = ? AND ABS(weekly_percent - ?) < 1.0 ")
    assert not _BAND.search("AND weekly_percent > ?")


def test_the_weekly_credit_band_is_gone_from_every_write_path():
    """The five sites that carried it, named so a reappearance is legible.

    They were: the automatic capture and the automatic DELETE in
    `_fire_in_place_credit`, the manual inline DELETE and the manual capture in
    `_apply_credit`, and `_count_stale_replays` behind the preview.
    """
    text = (BIN / "_cctally_record.py").read_text()
    assert "ABS(weekly_percent" not in text
    assert text.count("_lib_credit_selection.select_automatic_replicas") >= 1
    assert text.count("_lib_credit_selection.select_manual_replicas") >= 2


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def test_every_stale_replica_path_routes_through_the_shared_selectors(
    ns, monkeypatch
):
    """The behavioural half. Stub both selectors and assert nothing selects a
    replica without them.

    A static search alone cannot see a caller that rewrote the predicate in a
    shape the regex misses, and the two selectors exist precisely because the
    automatic predicate used to be written out twice and could drift.
    """
    import _lib_credit_selection as sel
    import _cctally_record as rec

    seen = {"automatic": 0, "manual": 0}
    real_auto = sel.select_automatic_replicas
    real_manual = sel.select_manual_replicas

    def _auto(*a, **k):
        seen["automatic"] += 1
        return real_auto(*a, **k)

    def _manual(*a, **k):
        seen["manual"] += 1
        return real_manual(*a, **k)

    monkeypatch.setattr(sel, "select_automatic_replicas", _auto)
    monkeypatch.setattr(sel, "select_manual_replicas", _manual)

    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json, account_key) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-08-30T09:00:00Z", "2026-08-29", "2026-09-05",
             "2026-08-29T00:00:00+00:00", "2026-09-05T00:00:00+00:00", 60.0,
             "statusline", "{}", "unattributed"))
        conn.commit()

        from _lib_credit_identity import CreditSource
        import datetime as dt
        rec._fire_in_place_credit(
            conn, "2026-08-29", "2026-09-05T00:00:00+00:00", 20.0,
            observed_pre_credit_pct=60.0,
            effective_dt=dt.datetime(2026, 8, 30, 10, tzinfo=dt.timezone.utc),
            as_of="2026-08-30T10:10:00Z", commit=True, ctx=None,
            credit_source=CreditSource("immediate", "sa:o:auto", 1),
            observed_at_utc="2026-08-30T10:10:00Z",
            confirming_capture_at_utc="2026-08-30T10:10:00Z")

        plan = ns["_build_credit_plan"](
            week_start_date="2026-08-29",
            week_start_at="2026-08-29T00:00:00+00:00",
            week_end_at="2026-09-05T00:00:00+00:00",
            from_pct=20.0, from_source="hwm", to_pct=5.0,
            at_dt=dt.datetime(2026, 8, 30, 12, tzinfo=dt.timezone.utc),
            now=dt.datetime(2026, 8, 30, 13, tzinfo=dt.timezone.utc))
        ns["_apply_credit"](conn, plan, commit=True)
        rec._count_stale_replays(conn, plan)
    finally:
        conn.close()

    assert seen["automatic"] >= 1, (
        "the automatic path selected replicas without the shared selector")
    assert seen["manual"] >= 2, (
        "the manual removal or the preview count selected replicas without the "
        f"shared selector: {seen}")
