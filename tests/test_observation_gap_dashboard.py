"""The milestone modal names an observation gap (#750 S4 §5).

`_shape_weekly_milestone` emitted five fields and dropped the cause, so the
dashboard rendered a bare em dash for a marginal the CLI names. The route now
adds an optional `marginal_usd_withheld_cause` to a null marginal the existing
pure kernel `classify_observation_gaps` identified, plus an additive
`observation_gap_runs` array carrying what the NOTE above the table is about —
a run's first threshold and the previous crossing, which the per-row cause
cannot state.

The client composes the sentence from those fields through `lib/fmt.ts`, the
browser's display-timezone chokepoint. The server ships no rendered sentence,
because that would put a presentation decision inside a data contract and take
the viewer's timezone from the server's idea of it.
"""
from __future__ import annotations

import sys

import pytest

from conftest import load_script, redirect_paths

WEEK_START_DATE = "2026-05-11"
WEEK_END_DATE = "2026-05-18"
WEEK_START_AT = "2026-05-11T00:00:00+00:00"
WEEK_END_AT = "2026-05-18T00:00:00+00:00"

# The whole run shares ONE capture instant and ONE originating observation,
# which is what the catch-up writer produces and what the classifier keys on.
RUN_CAPTURE = "2026-05-14T09:00:00+00:00"
PRIOR_CAPTURE = "2026-05-13T08:00:00+00:00"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    namespace = load_script()
    redirect_paths(namespace, monkeypatch, tmp_path)
    return namespace


def _seed(conn):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (RUN_CAPTURE, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
         WEEK_END_AT, 15.0, "test", "{}"),
    )
    rows = [
        # (threshold, captured, marginal, usage_snapshot_id)
        (11, PRIOR_CAPTURE, 1.5, 41),
        (12, RUN_CAPTURE, 3.0, 42),
        (13, RUN_CAPTURE, None, 42),
        (14, RUN_CAPTURE, None, 42),
        (15, RUN_CAPTURE, None, 42),
    ]
    for threshold, captured, marginal, snapshot_id in rows:
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, percent_threshold, cumulative_cost_usd, "
            " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id, "
            " five_hour_percent_at_crossing, reset_event_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (captured, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
             WEEK_END_AT, threshold, 10.0 + threshold, marginal,
             snapshot_id, 1, None, 0),
        )
    conn.commit()


def _detail(ns):
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        _seed(conn)
        index = mh.build_claude_week_index(conn)
        entry = next(e for e in index
                     if (e["start_at_utc"] or "").startswith("2026-05-11"))
        return entry, mh.build_claude_week_detail(conn, entry["key"])
    finally:
        conn.close()


def test_a_backfilled_row_carries_its_typed_cause_on_the_wire(ns):
    """`_shape_weekly_milestone` emits five fields and dropped the cause, so
    the modal rendered a bare em dash while the CLI names it."""
    _entry, detail = _detail(ns)
    milestones = detail["segments"][0]["milestones"]
    by_percent = {m["percent"]: m for m in milestones}
    for withheld in (13, 14, 15):
        assert by_percent[withheld]["marginal_usd"] is None
        assert by_percent[withheld]["marginal_usd_withheld_cause"] == (
            "observation_gap")
    # The run's FIRST row keeps its accumulated marginal, and a row outside
    # the run carries no cause at all — the field appears only where it is
    # true, so every other payload byte is unchanged.
    assert "marginal_usd_withheld_cause" not in by_percent[12]
    assert "marginal_usd_withheld_cause" not in by_percent[11]


def test_the_route_publishes_one_run_entry_per_backfilled_run(ns):
    """Ordered ascending by threshold; each entry carries first and last
    threshold, the single observation's instant, and the previous crossing's
    instant, which is null when the run opens the ladder."""
    _entry, detail = _detail(ns)
    runs = detail["observation_gap_runs"]
    assert len(runs) == 1, runs
    (run,) = runs
    assert run["first_percent"] == 12
    assert run["last_percent"] == 15
    assert run["observed_at_utc"] == "2026-05-14T09:00:00Z"
    assert run["previous_crossed_at_utc"] == "2026-05-13T08:00:00Z"


def test_a_week_without_a_gap_publishes_no_runs(ns):
    """Absent or empty means no gap, so an ordinary week's payload is
    unchanged apart from the empty array."""
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (PRIOR_CAPTURE, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
             WEEK_END_AT, 11.0, "test", "{}"),
        )
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, percent_threshold, cumulative_cost_usd, "
            " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id, "
            " five_hour_percent_at_crossing, reset_event_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (PRIOR_CAPTURE, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
             WEEK_END_AT, 11, 21.0, 1.5, 41, 1, None, 0),
        )
        conn.commit()
        index = mh.build_claude_week_index(conn)
        entry = next(e for e in index
                     if (e["start_at_utc"] or "").startswith("2026-05-11"))
        detail = mh.build_claude_week_detail(conn, entry["key"])
    finally:
        conn.close()
    assert detail["observation_gap_runs"] == []
    assert entry["has_observation_gap"] is False
    milestone = detail["segments"][0]["milestones"][0]
    assert "marginal_usd_withheld_cause" not in milestone


def test_the_week_index_hints_at_a_gap(ns):
    """§5.3. A CURRENT single-segment cycle neither fetches its detail nor
    uses one that arrives, so a gap in the live cycle would stay invisible.
    The hint was written only after the client probe demonstrated the need —
    `renders the disclosure for a CURRENT single-segment cycle` in
    `dashboard/web/src/modals/currentWeekHistory.test.tsx` failed without it.

    The index and the detail cannot disagree, because both classify through
    the same pure kernel over the same rows.
    """
    entry, detail = _detail(ns)
    assert entry["has_observation_gap"] is True
    assert bool(detail["observation_gap_runs"]) is True


def test_the_cli_bytes_do_not_move(ns):
    """`marginalCostWithheldCause` stays camelCase on the CLI's own JSON.

    The dashboard wire shape is one of the frozen legacy SNAKE surfaces
    `docs/cli-contract.md` describes, so the two spellings are correct on
    their own surfaces and neither follows the other.
    """
    import _cctally_percent_breakdown as pb
    assert pb.OBSERVATION_GAP_CAUSE == "observation_gap"
    source = (
        __import__("pathlib").Path(pb.__file__).read_text(encoding="utf-8"))
    assert 'entry["marginalCostWithheldCause"]' in source, (
        "the CLI's camelCase key must not have been renamed to the "
        "dashboard's snake_case one"
    )
    assert "marginal_usd_withheld_cause" not in source
    assert sys.modules is not None
