"""The two stale-replica selectors (#703 + #707 §5.1).

There are TWO rules, not one, because the automatic and manual paths hold
different evidence, and a single shared predicate would destroy real data.

The automatic rule is a bracket closed at both ends. Detection follows the
credit within a tick, so the bracket is seconds wide, and a row inside it
reading above the credited level contradicts both of the observations that bound
it. Both ends are inclusive: timestamps here are second-precision by design, so
an exclusive end would miss a replica stamped in the same second as an endpoint,
and inclusivity cannot delete either endpoint because the percent comparison is
strictly above the recorded landing level.

The manual rule targets the asserted pre-credit level instead. `record-credit
--at` accepts any past instant inside the week, so a credit recorded hours after
it happened has genuine climb between the two; applying the automatic rule there
would delete every climbed row as a replica. There is no upper bracket end to
prevent that, because a retroactive assertion has no confirming observation.
What distinguishes a replay from a climb is the LEVEL: a replay still reads the
old value.

Neither rule contains a tolerance band. The band they replace —
`ABS(weekly_percent - observed_pre_credit_pct) < 1.0` — is what failed in the
2026-09-01 incident: the marker's baseline was 14.0 while the rows to remove
held 13.0, the two differed by exactly 1.0, the strict band excluded them, and
the DELETE did nothing.
"""
from __future__ import annotations

import pathlib

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


WEEK_START_DATE = "2026-08-29"
WEEK_START_AT = "2026-08-29T00:00:00+00:00"
WEEK_END_AT = "2026-09-05T00:00:00+00:00"
ACCOUNT = "unattributed"


def _insert(conn, captured_at, percent, *, account_key=ACCOUNT,
            journal_id=None):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, page_url, source, payload_json, "
        " account_key, journal_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (captured_at, WEEK_START_DATE, "2026-09-05", WEEK_START_AT,
         WEEK_END_AT, percent, None, "statusline", "{}", account_key,
         journal_id))


@pytest.fixture
def incident_store(ns):
    """The 2026-09-01 incident's snapshot shape.

    The 13.0 at 17:33:38 is genuine pre-credit history captured BEFORE the
    credit was observed. The three 13.0 rows from 17:59:41 onward are replays of
    it that the external status line pushed after the counter had already been
    zeroed.
    """
    conn = ns["open_db"]()
    try:
        _insert(conn, "2026-09-01T17:33:38Z", 13.0, journal_id="sa:o:genuine")
        _insert(conn, "2026-09-01T17:59:41Z", 13.0, journal_id="sa:o:lower")
        _insert(conn, "2026-09-01T17:59:44Z", 13.0, journal_id="sa:o:middle")
        _insert(conn, "2026-09-01T17:59:47Z", 13.0, journal_id="sa:o:upper")
        _insert(conn, "2026-09-01T17:59:41Z", 0.0, journal_id="sa:o:firstzero")
        _insert(conn, "2026-09-01T17:59:47Z", 0.0, journal_id="sa:o:confirm")
        _insert(conn, "2026-09-01T18:18:50Z", 1.0, journal_id="sa:o:climb1")
        conn.commit()
        yield conn
    finally:
        conn.close()


def _automatic(conn, **over):
    import _lib_credit_selection as sel
    kwargs = dict(
        week_start_date=WEEK_START_DATE, account_key=ACCOUNT,
        observed_at="2026-09-01T17:59:41Z",
        confirming_capture_at="2026-09-01T17:59:47Z",
        post_credit_pct=0.0)
    kwargs.update(over)
    return sel.select_automatic_replicas(conn, **kwargs)


def _ids(rows):
    return {r["journal_id"] for r in rows}


def test_the_incident_row_is_kept_because_it_precedes_the_observation(
    incident_store
):
    """Spec §5.1: the 17:33:38 capture is real pre-credit history, and deleting
    it would destroy a true observation. What stops it pinning every surface at
    13% is §5.3's floor, not a deletion."""
    assert "sa:o:genuine" not in _ids(_automatic(incident_store))


def test_a_replica_inside_the_bracket_is_selected(incident_store):
    assert "sa:o:middle" in _ids(_automatic(incident_store))


def test_both_bracket_ends_are_inclusive(incident_store):
    """A replica stamped in the same second as either endpoint must be
    selected. Timestamps here are second-precision by design, so an exclusive
    end would silently leave it behind."""
    selected = _ids(_automatic(incident_store))
    assert {"sa:o:lower", "sa:o:upper"} <= selected, selected


def test_the_two_bounding_observations_are_never_selected(incident_store):
    """Inclusivity is safe precisely because the percent comparison is STRICTLY
    above the recorded landing level, and both endpoints read at that level."""
    selected = _ids(_automatic(incident_store))
    assert "sa:o:firstzero" not in selected
    assert "sa:o:confirm" not in selected


def test_a_later_genuine_climb_is_outside_the_bracket(incident_store):
    assert "sa:o:climb1" not in _ids(_automatic(incident_store))


def test_the_selector_returns_the_physical_id_and_the_logical_one(
    incident_store
):
    """Every caller consumes ONE result. The suppression emitter needs the
    logical id and the unjournaled inline delete needs the physical one."""
    row = next(r for r in _automatic(incident_store)
               if r["journal_id"] == "sa:o:middle")
    assert isinstance(row["id"], int)
    assert row["weekly_percent"] == 13.0
    assert row["captured_at_utc"] == "2026-09-01T17:59:44Z"


def test_another_accounts_replica_is_not_selected(ns):
    conn = ns["open_db"]()
    try:
        _insert(conn, "2026-09-01T17:59:44Z", 13.0, account_key="acct-b",
                journal_id="sa:o:other")
        conn.commit()
        assert _automatic(conn) == []
    finally:
        conn.close()


# ── the manual rule ─────────────────────────────────────────────────────

@pytest.fixture
def retroactive_store(ns):
    """A credit of 46 -> 31 at 10:00, recorded at 14:00, with genuine climb.

    This is the data-loss case, and the reason §5.1 has two rules. Applying the
    automatic bracket here would delete 32, 33 and 35 as replicas.
    """
    conn = ns["open_db"]()
    try:
        _insert(conn, "2026-08-30T09:40:00Z", 46.0, journal_id="sa:o:pre")
        _insert(conn, "2026-08-30T10:05:00Z", 46.0, journal_id="sa:o:replay")
        _insert(conn, "2026-08-30T11:00:00Z", 32.0, journal_id="sa:o:c32")
        _insert(conn, "2026-08-30T12:00:00Z", 33.0, journal_id="sa:o:c33")
        _insert(conn, "2026-08-30T13:00:00Z", 35.0, journal_id="sa:o:c35")
        conn.commit()
        yield conn
    finally:
        conn.close()


def _manual(conn, **over):
    import _lib_credit_selection as sel
    kwargs = dict(
        week_start_date=WEEK_START_DATE, account_key=ACCOUNT,
        observed_at="2026-08-30T10:00:00Z", from_pct=46.0)
    kwargs.update(over)
    return sel.select_manual_replicas(conn, **kwargs)


def test_a_retroactive_manual_credit_keeps_genuine_climb(retroactive_store):
    kept = {"sa:o:c32", "sa:o:c33", "sa:o:c35"}
    assert kept.isdisjoint(_ids(_manual(retroactive_store))), (
        "genuine post-credit climb was selected for deletion")


def test_a_manual_replay_at_the_asserted_level_is_selected(retroactive_store):
    assert "sa:o:replay" in _ids(_manual(retroactive_store))


def test_a_manual_credits_own_history_before_the_instant_is_kept(
    retroactive_store
):
    assert "sa:o:pre" not in _ids(_manual(retroactive_store))


def test_the_manual_rule_is_at_or_above_not_a_band(ns):
    """`>= from_pct`, so a replay that reads slightly HIGHER than the asserted
    mark is still a replay. The band it replaces would have excluded it once the
    difference passed 1.0."""
    conn = ns["open_db"]()
    try:
        _insert(conn, "2026-08-30T10:05:00Z", 47.5, journal_id="sa:o:above")
        _insert(conn, "2026-08-30T10:06:00Z", 44.5, journal_id="sa:o:below")
        conn.commit()
        selected = _ids(_manual(conn))
    finally:
        conn.close()
    assert "sa:o:above" in selected
    assert "sa:o:below" not in selected


def test_the_incident_band_would_have_missed_the_replicas(incident_store):
    """The regression this replaces, stated as a measurement rather than prose.

    On the debounced leg `observed_pre_credit_pct` is the armed marker's
    baseline (14.0) while the rows to remove hold what was actually written
    (13.0). The old predicate banded on that difference and selected nothing.
    """
    banded = incident_store.execute(
        "SELECT journal_id FROM weekly_usage_snapshots "
        "WHERE week_start_date = ? AND account_key = ? "
        "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
        "  AND ABS(weekly_percent - ?) < 1.0",
        (WEEK_START_DATE, ACCOUNT, "2026-09-01T17:00:00+00:00", 14.0),
    ).fetchall()
    assert [r["journal_id"] for r in banded] == []
    assert _ids(_automatic(incident_store)) == {
        "sa:o:lower", "sa:o:middle", "sa:o:upper"}


def test_neither_selector_contains_a_tolerance_band():
    """Read the STRINGS the module builds SQL from, not its prose.

    The module's own docstring names the band it replaces, so a plain text
    search over the file would match the explanation rather than a predicate.
    Docstrings are excluded by identity — the first statement of the module and
    of each definition — and every remaining string constant is checked.
    """
    import ast

    tree = ast.parse(pathlib.Path("bin/_lib_credit_selection.py").read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None) or []
            first = body[0] if body else None
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    offenders = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings and "ABS(" in node.value.upper()
    ]
    assert offenders == [], (
        f"a tolerance band reappeared in the selectors: {offenders!r}")
