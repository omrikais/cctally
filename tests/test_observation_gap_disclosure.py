"""A back-filled milestone run says so instead of printing a column of `n/a`.

#750 S3, Unit C (issue #738). When observation stops for long enough that the
next reading crosses several integer percents at once, the catch-up writer
assigns ONE `usage_snapshot_id` and ONE `captured_at_utc` to every threshold it
fills in, and puts the whole accumulated marginal on the first inserted row. On
week 2026-08-29 that produced sixteen consecutive rows whose marginal rendered
as a bare `n/a`, which reads as "this crossing cost nothing measurable" rather
than "these crossings were never observed separately".

The classification is at READ time, so no journal truth changes and no column
is added. The predicate is the shared truthy `usage_snapshot_id` — null and the
`0` "no snapshot row" sentinel both fail it — and the shared `captured_at_utc`,
over a maximal run of at least two consecutive thresholds inside one
`(account_key, week_start_date, reset_event_id)`. It is deliberately
NOT `marginal_cost_usd IS NULL`: a null marginal also means no prior milestone
exists, which is the ordinary shape of an epoch's first crossing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths


BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))


WEEK_START_DATE = "2026-08-29"
WEEK_END_DATE = "2026-09-05"
WEEK_START_AT = "2026-08-29T00:00:00+00:00"
WEEK_END_AT = "2026-09-05T00:00:00+00:00"
BEFORE_GAP = "2026-09-03T10:40:06Z"
AFTER_GAP = "2026-09-04T04:03:18Z"


def _row(
    threshold: int,
    *,
    snapshot: "int | None" = 7,
    captured: str = AFTER_GAP,
    marginal: "float | None" = None,
    account: str = "unattributed",
    reset_event_id: int = 0,
    week: str = WEEK_START_DATE,
) -> dict:
    """One `percent_milestones` row, as the mapping the classifier reads.

    A plain dict rather than a `sqlite3.Row`, because the classifier is a pure
    function over row mappings and both callers hand it whatever
    `get_milestones_for_week` returned. The command-level cases below drive the
    real rows through the real query.
    """
    return {
        "percent_threshold": threshold,
        "usage_snapshot_id": snapshot,
        "captured_at_utc": captured,
        "marginal_cost_usd": marginal,
        "account_key": account,
        "reset_event_id": reset_event_id,
        "week_start_date": week,
    }


# ── the classifier ─────────────────────────────────────────────────────


def test_a_back_filled_run_is_classified_and_keeps_its_first_marginal():
    ns = load_script()
    rows = [
        _row(18, snapshot=6, captured=BEFORE_GAP, marginal=12.0),
        _row(19, marginal=685.96),
        _row(20),
        _row(21),
    ]

    disclosure = ns["classify_observation_gaps"](rows)

    assert len(disclosure.runs) == 1, disclosure.runs
    run = disclosure.runs[0]
    assert (run.first_threshold, run.last_threshold) == (19, 21)
    assert run.captured_at_utc == AFTER_GAP
    assert run.previous_captured_at_utc == BEFORE_GAP
    assert run.indexes == (1, 2, 3)
    # The run's first row carries the whole accumulated marginal and keeps it;
    # only the nulls after it are withheld. The 18% row is outside the run.
    assert disclosure.withheld_indexes == frozenset({2, 3})


def test_a_run_whose_first_row_is_null_still_keeps_that_row_measured():
    """The first row of a run is never withheld, even when its marginal is null.

    An epoch's first crossing has no predecessor to subtract from, so its null
    means "no prior milestone", not "not separable". Withholding it would put
    the typed cause on a row the gap does not explain.
    """
    ns = load_script()
    disclosure = ns["classify_observation_gaps"]([_row(1), _row(2), _row(3)])
    assert disclosure.withheld_indexes == frozenset({1, 2})


def test_a_single_threshold_is_not_a_run():
    ns = load_script()
    assert ns["classify_observation_gaps"]([_row(19)]).runs == ()


def test_non_consecutive_thresholds_are_not_a_run():
    ns = load_script()
    disclosure = ns["classify_observation_gaps"]([_row(19), _row(21)])
    assert disclosure.runs == () and disclosure.withheld_indexes == frozenset()


def test_rows_carrying_different_snapshot_ids_are_not_a_run():
    ns = load_script()
    rows = [_row(19, snapshot=7), _row(20, snapshot=8)]
    assert ns["classify_observation_gaps"](rows).runs == ()


def test_rows_carrying_different_capture_instants_are_not_a_run():
    ns = load_script()
    rows = [_row(19, captured=AFTER_GAP),
            _row(20, captured="2026-09-04T04:03:19Z")]
    assert ns["classify_observation_gaps"](rows).runs == ()


def test_a_null_snapshot_id_never_classifies():
    """The predicate needs a shared NON-NULL snapshot id.

    Two rows that both fail to name their originating observation agree on
    nothing; treating `NULL == NULL` as a match would classify unrelated
    legacy rows as one back-filled run.
    """
    ns = load_script()
    rows = [_row(19, snapshot=None), _row(20, snapshot=None)]
    assert ns["classify_observation_gaps"](rows).runs == ()


def test_a_zero_snapshot_id_never_classifies():
    """`0` is this repository's "no snapshot row" sentinel, not an observation.

    `bin/_cctally_record.py` writes `cost_snapshot_id = 0` when there is no
    cost snapshot to anchor against, and `bin/build-dashboard-fixtures.py`
    seeds `0` into both id columns on every milestone it writes. Rows sharing
    it agree on nothing, exactly as rows sharing a null do, so a fixture
    seeding it with one `captured_at_utc` must not read as a back-filled run.
    """
    ns = load_script()
    rows = [_row(19, snapshot=0), _row(20, snapshot=0)]
    assert ns["classify_observation_gaps"](rows).runs == ()


def test_rows_in_different_accounts_are_not_a_run():
    ns = load_script()
    rows = [_row(19, account="acct-a"), _row(20, account="acct-b")]
    assert ns["classify_observation_gaps"](rows).runs == ()


def test_rows_in_different_reset_epochs_are_not_a_run():
    ns = load_script()
    rows = [_row(19, reset_event_id=0), _row(20, reset_event_id=4)]
    assert ns["classify_observation_gaps"](rows).runs == ()


def test_rows_in_different_weeks_are_not_a_run():
    """`week_start_date` is the third part of the grouping key.

    Both current callers fix the week in their query, so this is not reachable
    from either one today. It is asserted because a caller that hands two
    weeks' rows to one classification must not have to know that.
    """
    ns = load_script()
    rows = [_row(19, week="2026-08-29"), _row(20, week="2026-09-05")]
    assert ns["classify_observation_gaps"](rows).runs == ()


def test_a_run_with_nothing_to_withhold_is_not_reported():
    """The note claims the marginals are not separable, so a run whose later
    rows all carry numbers must not print it above a table showing every one.

    The production writer puts the whole accumulated marginal on the run's
    first row and leaves the rest null, so this shape is unreachable through
    it. The classifier decides the run and the withheld set together anyway,
    which is what keeps a future writer from reaching it either.
    """
    ns = load_script()
    rows = [_row(19, marginal=1.0), _row(20, marginal=2.0)]
    disclosure = ns["classify_observation_gaps"](rows)
    assert disclosure.runs == ()
    assert disclosure.withheld_indexes == frozenset()


def test_two_runs_in_one_week_are_reported_separately():
    """A week with several runs gets one line per run."""
    ns = load_script()
    rows = [
        _row(1, snapshot=1, captured="2026-08-29T01:00:00Z", marginal=1.0),
        _row(2, snapshot=2, captured="2026-08-30T01:00:00Z", marginal=2.0),
        _row(3, snapshot=2, captured="2026-08-30T01:00:00Z"),
        _row(4, snapshot=3, captured="2026-09-01T01:00:00Z", marginal=4.0),
        _row(5, snapshot=4, captured="2026-09-02T01:00:00Z", marginal=5.0),
        _row(6, snapshot=4, captured="2026-09-02T01:00:00Z"),
    ]
    disclosure = ns["classify_observation_gaps"](rows)
    assert [(r.first_threshold, r.last_threshold) for r in disclosure.runs] == [
        (2, 3), (5, 6)]
    assert disclosure.withheld_indexes == frozenset({2, 5})
    notes = ns["observation_gap_notes"](disclosure, tz=None)
    assert len(notes) == 2, notes


def test_the_note_names_the_range_the_instant_and_the_span():
    ns = load_script()
    disclosure = ns["classify_observation_gaps"]([
        _row(18, snapshot=6, captured=BEFORE_GAP, marginal=12.0),
        _row(19, marginal=685.96),
        _row(20),
    ])
    note = ns["observation_gap_notes"](disclosure, tz=None)[0]
    assert "19%" in note and "20%" in note
    assert "2026-09-04 04:03" in note
    # 2026-09-03T10:40:06Z -> 2026-09-04T04:03:18Z is 17.386 hours.
    assert "17.4 hours" in note
    assert ns["OBSERVATION_GAP_CAUSE"] in note


@pytest.mark.parametrize(("gap_seconds", "expected"), (
    (1, "1 second"),
    (30, "30 seconds"),
    (60, "1 minute"),
    (3599, "59 minutes"),
    (3600, "1.0 hours"),
))
def test_a_span_below_an_hour_reads_in_seconds_or_whole_minutes(
        gap_seconds: int, expected: str) -> None:
    """The status-line hook ticks about every 30 seconds, so a sub-hour span
    between two crossings is an ordinary shape rather than a corner case.

    Rounding the span to minutes rendered 30 seconds as "0 minutes", 60
    seconds as "1 minutes", and 3599 seconds as "60 minutes" directly above
    the "1.0 hours" that 3600 seconds gives.
    """
    ns = load_script()
    end = dt.datetime.fromisoformat(AFTER_GAP.replace("Z", "+00:00"))
    previous = (end - dt.timedelta(seconds=gap_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    disclosure = ns["classify_observation_gaps"]([
        _row(18, snapshot=6, captured=previous, marginal=12.0),
        _row(19),
        _row(20),
    ])
    note = ns["observation_gap_notes"](disclosure, tz=None)[0]
    assert f", {expected} after the previous crossing" in note, note


def test_the_note_omits_the_span_when_the_run_opens_the_ladder():
    """No predecessor means no elapsed span exists to state.

    Printing one anyway would have to invent an origin instant, and the run
    that opens an epoch's ladder genuinely has none.
    """
    ns = load_script()
    disclosure = ns["classify_observation_gaps"]([_row(19), _row(20)])
    note = ns["observation_gap_notes"](disclosure, tz=None)[0]
    assert "hours" not in note and "minutes" not in note
    assert "2026-09-04 04:03" in note


# ── the rendered surfaces ──────────────────────────────────────────────


def _seed(path: Path, rows) -> None:
    """A stats.db carrying one week's snapshots and the given milestones.

    Each entry is `(threshold, usage_snapshot_id, captured_at, marginal)`.
    """
    import _fixture_builders as fixtures

    fixtures.create_stats_db(path)
    conn = sqlite3.connect(path)
    try:
        fixtures.seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=AFTER_GAP,
            week_start_date=WEEK_START_DATE,
            week_end_date=WEEK_END_DATE,
            week_start_at=WEEK_START_AT,
            week_end_at=WEEK_END_AT,
            weekly_percent=21.0,
        )
        fixtures.seed_weekly_cost_snapshot(
            conn,
            captured_at_utc=AFTER_GAP,
            week_start_date=WEEK_START_DATE,
            week_end_date=WEEK_END_DATE,
            week_start_at=WEEK_START_AT,
            week_end_at=WEEK_END_AT,
            cost_usd=1000.0,
        )
        for threshold, snapshot, captured, marginal in rows:
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id, "
                " account_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'unattributed')",
                (captured, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
                 WEEK_END_AT, threshold, 100.0 * threshold, marginal,
                 snapshot, 1),
            )
        conn.commit()
    finally:
        conn.close()


GAP_MILESTONES = (
    (18, 6, BEFORE_GAP, 12.0),
    (19, 7, AFTER_GAP, 685.96),
    (20, 7, AFTER_GAP, None),
    (21, 7, AFTER_GAP, None),
)


@pytest.fixture()
def gap_stats(tmp_path: Path) -> Path:
    path = tmp_path / "stats.db"
    _seed(path, GAP_MILESTONES)
    return path


@pytest.fixture()
def opened_conns():
    """An `open_db` stand-in that closes every connection it handed out."""
    opened: "list[sqlite3.Connection]" = []

    def _factory(path: Path):
        def _open(*_args, **_kwargs) -> sqlite3.Connection:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            opened.append(conn)
            return conn
        return _open

    yield _factory
    for conn in opened:
        conn.close()


def _column(rendered: str, index: int) -> list:
    """One column of a `_boxed_table`'s data rows, stripped, in order."""
    cells = []
    for line in rendered.splitlines():
        if not line.startswith("│ "):
            continue
        fields = [field.strip() for field in line.split("│")[1:-1]]
        if fields[0] == "#":
            continue
        cells.append(fields[index])
    return cells


def _breakdown(ns, monkeypatch, opened_conns, gap_stats, capsys, *, as_json):
    import _cctally_core
    import _cctally_percent_breakdown as pb

    monkeypatch.setattr(pb, "open_db", opened_conns(gap_stats))
    monkeypatch.setattr(_cctally_core, "open_db", opened_conns(gap_stats))
    monkeypatch.setitem(ns, "load_config", lambda *a, **k: {})
    args = argparse.Namespace(
        week_start=None, week_start_name=None, json=as_json, tz="utc",
        account=None,
    )
    assert ns["cmd_percent_breakdown"](args) == 0
    return capsys.readouterr().out


def test_percent_breakdown_names_the_gap_and_withholds_the_marginals(
    gap_stats: Path, monkeypatch: pytest.MonkeyPatch, capsys, opened_conns,
) -> None:
    ns = load_script()
    out = _breakdown(ns, monkeypatch, opened_conns, gap_stats, capsys,
                     as_json=False)

    cause = ns["OBSERVATION_GAP_CAUSE"]
    note = [line for line in out.splitlines() if line.startswith("Observation gap")]
    assert len(note) == 1, out
    assert "19%" in note[0] and "21%" in note[0] and "17.4 hours" in note[0]
    # The note sits above the table, not inside it.
    assert out.index(note[0]) < out.index("Percent breakdown:")

    # Asserted per CELL, not per line: the `5h at crossing` column of this
    # fixture is itself `n/a`, so a whole-line check would pass while the
    # marginal column still said `n/a`.
    marginals = _column(out, 3)
    assert len(marginals) == 4, marginals
    # 18% and 19% keep their measured marginals; 20% and 21% state the cause.
    assert marginals[:2] == ["$12.000000", "$685.960000"]
    assert marginals[2:] == [cause, cause]


def test_percent_breakdown_json_carries_the_additive_cause(
    gap_stats: Path, monkeypatch: pytest.MonkeyPatch, capsys, opened_conns,
) -> None:
    """`marginalCostUSD` stays numeric-or-null; the cause is a sibling.

    `docs/cli-contract.md` permits an additive optional field without a
    `schemaVersion` bump, and the stamped-first rule is unchanged.
    """
    ns = load_script()
    payload = json.loads(_breakdown(
        ns, monkeypatch, opened_conns, gap_stats, capsys, as_json=True))

    assert list(payload)[0] == "schemaVersion"
    assert payload["schemaVersion"] == 1
    cause = ns["OBSERVATION_GAP_CAUSE"]
    by_threshold = {m["percentThreshold"]: m for m in payload["milestones"]}
    assert by_threshold[18]["marginalCostUSD"] == 12.0
    assert by_threshold[19]["marginalCostUSD"] == 685.96
    assert by_threshold[20]["marginalCostUSD"] is None
    assert "marginalCostWithheldCause" not in by_threshold[18]
    assert "marginalCostWithheldCause" not in by_threshold[19]
    assert by_threshold[20]["marginalCostWithheldCause"] == cause
    assert by_threshold[21]["marginalCostWithheldCause"] == cause


def test_an_ungapped_week_renders_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, opened_conns,
) -> None:
    """No run, no note, and a genuine null marginal still prints `n/a`.

    This is the byte-stability half: the disclosure must not reach a week
    that has no back-filled run, and it must not rewrite the null that means
    "no prior milestone".
    """
    ns = load_script()
    path = tmp_path / "stats.db"
    _seed(path, (
        (1, 1, "2026-08-29T01:00:00Z", None),
        (2, 2, "2026-08-30T01:00:00Z", 5.0),
    ))
    out = _breakdown(ns, monkeypatch, opened_conns, path, capsys,
                     as_json=False)
    assert "Observation gap" not in out
    assert ns["OBSERVATION_GAP_CAUSE"] not in out
    # Asserted per CELL, not per line, for the reason the gapped sibling above
    # gives: this fixture leaves `five_hour_percent_at_crossing` null too, so
    # its `5h at crossing` column already prints `n/a` on both rows and an
    # `"n/a" in out` check would stay green however the marginal cell renders.
    marginals = _column(out, 3)
    assert marginals == ["n/a", "$5.000000"], marginals


CODEX_SHAPED_RENDER = "\n".join((
    "Week: 2026-08-29 00:00 -> 2026-09-05 00:00",
    "Percent breakdown:",
    "",
    "┌───┬───────────┬─────────────────┬───────────────┬────────────────┐",
    "│ # │ Threshold │ Cumulative Cost │ Marginal Cost │ 5h at crossing │",
    "├───┼───────────┼─────────────────┼───────────────┼────────────────┤",
    "│ 1 │       11% │       $1.500000 │     $0.500000 │            25% │",
    "├───┼───────────┼─────────────────┼───────────────┼────────────────┤",
    "│ 2 │       12% │       $2.250000 │     $0.750000 │            30% │",
    "└───┴───────────┴─────────────────┴───────────────┴────────────────┘",
))


def test_codex_quota_rendering_is_byte_identical() -> None:
    """`bin/_cctally_quota.py` is a second caller of the shared renderer.

    It passes exactly the keywords below and must keep rendering the same
    bytes, so the disclosure arrives through an optional keyword and no
    required argument is added. The expected block was captured from the
    renderer BEFORE this change, so it is a pre-change byte record rather
    than a restatement of the new code.
    """
    import inspect

    ns = load_script()
    render = ns["_render_percent_breakdown_terminal"]
    assert render(
        week_start_date="2026-08-29",
        week_end_date="2026-09-05",
        display_start_iso="2026-08-29T00:00:00Z",
        display_end_iso="2026-09-05T00:00:00Z",
        milestone_list=[
            {"percentThreshold": 11, "cumulativeCostUSD": 1.5,
             "marginalCostUSD": 0.5, "capturedAt": "2026-08-30T10:00:00Z",
             "fiveHourPercentAtCrossing": 25.0},
            {"percentThreshold": 12, "cumulativeCostUSD": 2.25,
             "marginalCostUSD": 0.75, "capturedAt": "2026-08-30T12:00:00Z",
             "fiveHourPercentAtCrossing": 30.0},
        ],
        tz=None,
    ) == CODEX_SHAPED_RENDER

    # Every parameter is keyword-only with a default except the ones the
    # Codex caller already passes, so a new required argument fails here
    # rather than at that call site.
    required = {
        name for name, p in inspect.signature(render).parameters.items()
        if p.default is inspect.Parameter.empty
    }
    assert required == {
        "week_start_date", "week_end_date", "display_start_iso",
        "display_end_iso", "milestone_list", "tz",
    }, required


def test_report_detail_names_the_gap_on_its_own_milestone_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """`report --detail` builds its own table and had the same defect.

    Shipping the disclosure on only one of the two surfaces would leave #738
    half-fixed, so the table reuses C1's classifier rather than repeating the
    predicate.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_into(ns, GAP_MILESTONES)
    monkeypatch.setitem(ns, "load_config", lambda *a, **k: {"display": {"tz": "utc"}})

    args = ns["build_parser"]().parse_args(["report", "--detail", "--tz", "utc"])
    assert ns["cmd_report"](args) == 0
    out = capsys.readouterr().out

    cause = ns["OBSERVATION_GAP_CAUSE"]
    note = [line for line in out.splitlines() if line.startswith("Observation gap")]
    assert len(note) == 1, out
    assert "19%" in note[0] and "21%" in note[0]
    detail = out[out.index("Percent breakdown (current week):"):]
    marginals = _column(detail, 3)
    assert marginals == ["$12.000000", "$685.960000", cause, cause], marginals
    # The note is above this table, and only one table carries it.
    assert detail.index(note[0]) < detail.index("Marginal Cost")


def _seed_into(ns, rows) -> None:
    """Seed the redirected app-dir stats.db with the same week and rows."""
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, 'test', '{}')",
            (AFTER_GAP, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
             WEEK_END_AT, 21.0),
        )
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, cost_usd, mode) VALUES (?, ?, ?, ?, ?, ?, 'auto')",
            (AFTER_GAP, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
             WEEK_END_AT, 1000.0),
        )
        for threshold, snapshot, captured, marginal in rows:
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id, "
                " account_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'unattributed')",
                (captured, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
                 WEEK_END_AT, threshold, 100.0 * threshold, marginal,
                 snapshot, 1),
            )
        conn.commit()
    finally:
        conn.close()
