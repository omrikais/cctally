"""#620 S1 A3 — where no interval resolves, the panel declines to attribute.

The rule the panel now follows is that the numerator, the denominator and the
percentage must all come from ONE half-open interval. Its consequence is a
refusal: a snapshot row that cannot be resolved onto an interval contributes
nothing, and a week whose percentage never resolves publishes `null` rather
than a number computed against some other week's boundaries.

A3 asks for more than "the field is absent". A `null` proves only that
nothing was written; it does not prove that the orphan percentage was not
applied somewhere else. So these tests assert both halves: the affected rows
publish `null` while still reporting their cost, AND the unresolvable
percentage appears nowhere in the envelope, with every other week keeping its
own.

`bin/build-dashboard-fixtures.py` labels a fixture "(#620 S1 A1/A3)" and the
`all-combined` golden carries a `weekly_pct: null`, but a golden records an
output; it does not state which population produced it.
"""
from __future__ import annotations

import datetime as dt
import sys

import pytest

# The store scaffolding (four Thursday-anchored weeks, one of them six days
# long, two projects with cost in every week) is shared with the A2 parity
# module rather than duplicated, so the two acceptance criteria are asserted
# over the same shape.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import test_620_projects_cli_panel_parity as A2  # noqa: E402
from test_620_projects_cli_panel_parity import app  # noqa: F401,E402


_ORPHAN_PCT = 77.0


def _all_percentages(env) -> list:
    """Every percentage the envelope publishes, from all three collections."""
    out = []
    for w in env["trend"]["weeks"]:
        out.append(w["total_pct"])
    for p in env["trend"]["projects"]:
        out.extend(p["weekly_pct"])
    for r in env["current_week"]["rows"]:
        out.append(r["attributed_pct"])
    return out


# --- Half 1: an unresolvable snapshot row contributes nothing --------------

def test_an_unresolvable_snapshot_row_is_discarded_not_reassigned(app):
    """The legacy shape that reaches `if wstart is None: continue`.

    A row with no `week_start_at` and a `week_start_date` that is no
    interval's start date resolves neither by anchor nor by date. Its
    percentage was captured against a boundary this store's cost was never
    bucketed by, so pairing the two would be attribution across mismatched
    populations. The row must be dropped whole.

    The date chosen — 2026-04-03 — sits INSIDE the third week rather than
    outside every week, which is the case that would actually tempt a
    nearest-interval fallback.
    """
    A2._seed_snapshots(app, A2._WEEKS)
    conn = app.open_db()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots("
            " captured_at_utc, week_start_date, week_end_date, week_start_at,"
            " week_end_at, weekly_percent, source, payload_json, account_key)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            # Captured after EVERY well-formed row (the latest of those is
            # 2026-04-15T08:00Z), so a code path that resolved this one would
            # also OVERWRITE the correct percentage for whichever week it
            # landed on — the loop is ordered by `captured_at_utc` and
            # `weekly_pct_by_week[wstart] = ...` is last-write-wins. An
            # earlier capture would be silently corrected by the proper row
            # and the assertions below would hold even against a code path
            # that did the wrong thing.
            ("2026-04-20T00:00:00Z", "2026-04-03", "2026-04-10", None, None,
             _ORPHAN_PCT, "legacy", "{}", "unattributed"),
        )
        conn.commit()
    finally:
        conn.close()
    A2._seed_entries(app, A2._ENTRIES)

    conn = A2._panel_conn(app)
    try:
        import _cctally_dashboard
        # Guard the guard: assert the row really is unresolvable BOTH ways,
        # so a future grid change that starts resolving it fails here rather
        # than silently making the assertions below vacuous.
        grid = _cctally_dashboard._projects_week_grid(
            conn, anchor_utc=A2.NOW_UTC, weeks_back=A2.WEEKS_BACK,
        )
        assert grid is not None
        anchor_free_date = dt.date(2026, 4, 3)
        assert grid.start_for_date(anchor_free_date) is None, (
            "the orphan's date must match no interval start, or the row "
            "resolves by the legacy date path and this test asserts nothing"
        )
        env = A2._panel_envelope(conn)
    finally:
        conn.close()

    published = _all_percentages(env)
    assert _ORPHAN_PCT not in published, (
        f"the unresolvable row's {_ORPHAN_PCT}% reached the envelope: "
        f"{published}"
    )

    # Every week keeps its OWN percentage. This is the mismatched-population
    # statement: had the orphan resolved onto any interval it would have
    # replaced that week's value, because it was captured last.
    got = [w["total_pct"] for w in env["trend"]["weeks"]]
    assert got == [w[2] for w in A2._WEEKS], (
        f"per-week percentages moved: {got} vs {[w[2] for w in A2._WEEKS]}"
    )

    # And attribution still happened for the weeks that DO resolve, so the
    # refusal is scoped to the unresolvable row rather than disabling the
    # whole calculation.
    alpha = next(p for p in env["trend"]["projects"] if p["key"] == "alpha")
    assert all(v is not None for v in alpha["weekly_pct"]), alpha["weekly_pct"]


# --- Half 2: a week with cost and no resolvable percentage ----------------

def test_a_week_without_a_resolvable_percentage_publishes_null(app):
    """Cost is reported; attribution is withheld.

    Only the two most recent weeks carry a snapshot, so the pre-snapshot tail
    is extrapolated backwards from the earliest anchor and those older
    intervals have no percentage at all. Every project with cost in them must
    publish `weekly_pct: null` beside a NON-ZERO `weekly_cost` — a zero cost
    would make the null unremarkable, since the `cost > 0` guard produces one
    too and the test could not tell the two reasons apart.
    """
    A2._seed_snapshots(app, [A2._W3, A2._W4])
    A2._seed_entries(app, A2._ENTRIES)

    conn = A2._panel_conn(app)
    try:
        env = A2._panel_envelope(conn)
    finally:
        conn.close()

    weeks = env["trend"]["weeks"]
    unpriced = [i for i, w in enumerate(weeks) if w["total_pct"] is None]
    assert unpriced, "the store must produce at least one week with no percentage"
    for i in unpriced:
        assert weeks[i]["total_cost_usd"] > 0, (
            f"week {weeks[i]['week_start_date']} must carry cost, or a null "
            "percentage beside it proves nothing"
        )

    for proj in env["trend"]["projects"]:
        for i in unpriced:
            if proj["weekly_cost"][i] > 0:
                assert proj["weekly_pct"][i] is None, (
                    f"{proj['key']} week {weeks[i]['week_start_date']} "
                    f"attributed {proj['weekly_pct'][i]} against a week whose "
                    "own percentage never resolved"
                )

    # The weeks that DO have a percentage still attribute, so the null above
    # is a scoped refusal rather than a dead calculation.
    priced = [i for i, w in enumerate(weeks) if w["total_pct"] is not None]
    assert priced, "the store must also produce a priced week"
    attributed = [
        proj["weekly_pct"][i]
        for proj in env["trend"]["projects"] for i in priced
        if proj["weekly_cost"][i] > 0
    ]
    assert attributed and all(v is not None for v in attributed), attributed


def test_current_week_rows_publish_null_when_nothing_resolves(app):
    """The current-week collection follows the same rule.

    With no snapshot anywhere the panel falls back to Monday anchors, which
    is the documented no-anchor path. Cost is still published per project;
    `attributed_pct` is `null` rather than a percentage borrowed from a week
    the store never measured.
    """
    A2._seed_entries(app, A2._ENTRIES)

    conn = A2._panel_conn(app)
    try:
        env = A2._panel_envelope(conn)
    finally:
        conn.close()

    rows = env["current_week"]["rows"]
    assert rows, "the current week must carry at least one project row"
    for r in rows:
        assert r["cost_usd"] > 0, r
        assert r["attributed_pct"] is None, (
            f"{r['key']} attributed {r['attributed_pct']} with no snapshot "
            "anywhere in the store"
        )
    assert all(
        w["total_pct"] is None for w in env["trend"]["weeks"]
    ), [w["total_pct"] for w in env["trend"]["weeks"]]
    assert env["current_week"]["total_cost_usd"] > 0
