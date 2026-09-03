"""A credited week says so on screen (#703 + #707 §6.4).

The week no longer splits, so nothing would otherwise explain a low `Used %`
late in a heavy week: the row shows the whole week's spend beside a counter that
Anthropic reset partway through it. The marker is a `+` prefix in the vocabulary
of the existing `~` heuristic-anchor and `⚡` crossed-reset prefixes, and the
JSON field is additive under the additive-evolution rule, so no `schemaVersion`
moves.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

import pytest

from conftest import load_script, redirect_paths

WEEK_START_DATE = "2026-08-29"
WEEK_END_DATE = "2026-09-05"
WEEK_START_AT = "2026-08-29T05:00:00+00:00"
WEEK_END_AT = "2026-09-05T05:00:00+00:00"
CREDIT_AT = "2026-09-01T17:00:00+00:00"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed(conn, *, with_credit):
    for captured_at, pct in (("2026-08-31T12:00:00Z", 67.0),
                             ("2026-09-02T11:00:00Z", 3.0)):
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json, account_key) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (captured_at, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
             WEEK_END_AT, pct, "statusline", "{}", "unattributed"))
    conn.execute(
        "INSERT INTO weekly_cost_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, cost_usd, mode) VALUES (?,?,?,?,?,?,?)",
        ("2026-09-02T11:00:00Z", WEEK_START_DATE, WEEK_END_DATE,
         WEEK_START_AT, WEEK_END_AT, 500.0, "auto"))
    if with_credit:
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
            " week_start_date, observed_at_utc, confirming_capture_at_utc, "
            " observed_post_credit_pct, credit_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (CREDIT_AT, CREDIT_AT, WEEK_END_AT, CREDIT_AT, 67.0,
             "unattributed", WEEK_START_DATE, CREDIT_AT, CREDIT_AT, 2.0,
             "o:credit"))
    conn.commit()


def _report(ns, monkeypatch, *, as_json):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-09-02T12:00:00Z")
    return ns["cmd_report"](argparse.Namespace(
        weeks=2, sync_current=False, week_start_name=None, mode="auto",
        offline=True, project=None, json=as_json, detail=False, format=None,
        theme=None, reveal_projects=False, no_branding=False, output=None,
        copy=False, open=False, tz=None))


def test_report_json_marks_a_credited_week(ns, monkeypatch, capsys):
    conn = ns["open_db"]()
    try:
        _seed(conn, with_credit=True)
    finally:
        conn.close()
    assert _report(ns, monkeypatch, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(r for r in payload["trend"]
               if r["weekStartDate"] == WEEK_START_DATE)
    assert row["credited"] is True, row


def test_report_json_leaves_an_uncredited_week_unmarked(
        ns, monkeypatch, capsys):
    conn = ns["open_db"]()
    try:
        _seed(conn, with_credit=False)
    finally:
        conn.close()
    assert _report(ns, monkeypatch, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(r for r in payload["trend"]
               if r["weekStartDate"] == WEEK_START_DATE)
    assert row["credited"] is False, row
    assert row["dollarsPerPercentWithheld"] is None, row


def test_the_report_table_marks_the_row_and_prints_a_legend(
        ns, monkeypatch, capsys):
    conn = ns["open_db"]()
    try:
        _seed(conn, with_credit=True)
    finally:
        conn.close()
    assert _report(ns, monkeypatch, as_json=False) == 0
    out = capsys.readouterr().out
    assert "+1" in out, out
    assert "Anthropic credited the counter" in out, out


def test_an_uncredited_report_table_gains_no_legend(ns, monkeypatch, capsys):
    conn = ns["open_db"]()
    try:
        _seed(conn, with_credit=False)
    finally:
        conn.close()
    assert _report(ns, monkeypatch, as_json=False) == 0
    out = capsys.readouterr().out
    assert "Anthropic credited the counter" not in out, out


def _weekly_table_lines(rendered: str) -> list[str]:
    """The bordered table's own lines, excluding the title box.

    A data row carries one `│` per column edge (eleven for the weekly
    table's ten columns); the title box carries exactly two. Rule lines
    are made entirely of box-drawing characters, so they are selected by
    their leading character instead.
    """
    return [
        line for line in rendered.splitlines()
        if line.count("│") >= 11 or line[:1] in ("┌", "├", "└")
    ]


def _weekly_render_fixture(ns, *, credited: bool) -> str:
    """Render a two-week compact table, optionally marking the later week."""
    buckets = [
        ns["BucketUsage"](
            bucket=bucket, input_tokens=500_000, output_tokens=50_000,
            cache_creation_tokens=0, cache_read_tokens=0,
            total_tokens=550_000, cost_usd=3.75,
            models=["claude-opus-4-7-20260115"], model_breakdowns=[])
        for bucket in ("2026-08-22", WEEK_START_DATE)
    ]
    weeks = [
        ns["SubWeek"](
            start_ts=f"{bucket}T05:00:00+00:00",
            end_ts=f"{bucket}T05:00:00+00:00",
            start_date=dt.date.fromisoformat(bucket),
            end_date=dt.date.fromisoformat(bucket) + dt.timedelta(days=6),
            source="snapshot",
            display_start_date=dt.date.fromisoformat(bucket))
        for bucket in ("2026-08-22", WEEK_START_DATE)
    ]
    return ns["_render_weekly_table"](
        buckets, [(60.0, 0.062), (25.0, 0.450)], weeks=weeks,
        compact_split_fn=ns["_daily_compact_split"], compact=True,
        credited_weeks={WEEK_START_DATE} if credited else None)


def test_a_compact_credited_week_keeps_the_tables_column_grid(ns):
    """The `+` marker must not widen the credited row past its column.

    In compact mode the Week column is fixed at ten characters and the
    date cell wraps to two lines. Prefixing the marker before the split
    defeats the `^\\d{4}-\\d{2}-\\d{2}$` match, so the cell stays on one
    eleven-character line and that one row renders a character wider than
    every other row in the table.
    """
    rendered = _weekly_render_fixture(ns, credited=True)
    widths = {len(line) for line in _weekly_table_lines(rendered)}
    assert len(widths) == 1, (
        f"credited table rows have mixed widths {sorted(widths)}\n{rendered}")


def test_a_compact_credited_week_still_wraps_its_date_over_two_lines(ns):
    """The credited row must keep the two-line date every other row has."""
    credited = _weekly_render_fixture(ns, credited=True)
    plain = _weekly_render_fixture(ns, credited=False)
    assert len(credited.splitlines()) == len(plain.splitlines()), (
        f"credited render lost a line\n{credited}\n---\n{plain}")
    marked = [line for line in credited.splitlines() if "+" in line]
    assert marked, credited
    assert any("2026" in line for line in marked), marked


def test_the_weekly_table_prints_the_cause_of_a_withheld_ratio(ns):
    """§6.3: a withheld quantity prints its CAUSE, never a bare em-dash.

    An em-dash in the `$/1%` cell reads as "no usage recorded", which is a
    different and wrong statement about a week that holds a credit whose epoch
    supports no divisor yet.
    """
    weeks = [
        ns["SubWeek"](
            start_ts=f"{WEEK_START_DATE}T05:00:00+00:00",
            end_ts=WEEK_END_AT,
            start_date=dt.date.fromisoformat(WEEK_START_DATE),
            end_date=dt.date.fromisoformat(WEEK_START_DATE)
            + dt.timedelta(days=6),
            source="snapshot",
            display_start_date=dt.date.fromisoformat(WEEK_START_DATE)),
    ]
    buckets = [ns["BucketUsage"](
        bucket=WEEK_START_DATE, input_tokens=1, output_tokens=1,
        cache_creation_tokens=0, cache_read_tokens=0, total_tokens=2,
        cost_usd=500.0, models=["claude-opus-4-7-20260115"],
        model_breakdowns=[])]
    rendered = ns["_render_weekly_table"](
        buckets, [(2.0, None)], weeks=weeks,
        compact_split_fn=ns["_daily_compact_split"], compact=True,
        credited_weeks={WEEK_START_DATE},
        withheld_by_week={WEEK_START_DATE: "no-climb-since-credit"})
    assert "no climb" in rendered, rendered
    widths = {len(line) for line in _weekly_table_lines(rendered)}
    assert len(widths) == 1, (
        f"the withheld cell broke the column grid {sorted(widths)}\n{rendered}")


def test_an_unknown_withheld_cause_still_fits_the_column(ns):
    """A cause the renderer does not recognize must not overflow the cell.

    The compact `$/1%` column is ten characters wide and the renderer does not
    truncate, so pasting an arbitrary-length cause in would widen that one row.
    The exact cause is published by `weekly --json` and by `report` instead.
    """
    weeks = [
        ns["SubWeek"](
            start_ts=f"{WEEK_START_DATE}T05:00:00+00:00",
            end_ts=WEEK_END_AT,
            start_date=dt.date.fromisoformat(WEEK_START_DATE),
            end_date=dt.date.fromisoformat(WEEK_START_DATE)
            + dt.timedelta(days=6),
            source="snapshot",
            display_start_date=dt.date.fromisoformat(WEEK_START_DATE)),
    ]
    buckets = [ns["BucketUsage"](
        bucket=WEEK_START_DATE, input_tokens=1, output_tokens=1,
        cache_creation_tokens=0, cache_read_tokens=0, total_tokens=2,
        cost_usd=500.0, models=["claude-opus-4-7-20260115"],
        model_breakdowns=[])]
    rendered = ns["_render_weekly_table"](
        buckets, [(2.0, None)], weeks=weeks,
        compact_split_fn=ns["_daily_compact_split"], compact=True,
        withheld_by_week={
            WEEK_START_DATE: "a-cause-nobody-has-mapped-yet-and-it-is-long"})
    assert "withheld" in rendered, rendered
    widths = {len(line) for line in _weekly_table_lines(rendered)}
    assert len(widths) == 1, (
        f"an unmapped cause broke the column grid {sorted(widths)}\n{rendered}")


def test_weekly_json_publishes_the_withheld_cause(ns, monkeypatch, capsys):
    """`report` publishes `dollarsPerPercentWithheld`; `weekly` carried the
    `credited` flag with no cause at all, so a machine consumer could not tell a
    withheld ratio from a week with no usage."""
    conn = ns["open_db"]()
    try:
        _seed(conn, with_credit=True)
        # Land the credit at the level the counter currently reads, so the
        # epoch supports no divisor and the ratio is withheld.
        conn.execute(
            "UPDATE week_reset_events SET observed_post_credit_pct = 3.0")
        conn.commit()
    finally:
        conn.close()
    # `weekly` aggregates session entries, so a week with none emits no bucket
    # at all and the wire field would go untested.
    cache = ns["open_cache_db"]()
    try:
        path = "/fake/repos/solo/entry.jsonl"
        cache.execute(
            "INSERT INTO session_files(path, size_bytes, mtime_ns, "
            " last_byte_offset, last_ingested_at, session_id, project_path) "
            "VALUES (?,?,?,?,?,?,?)",
            (path, 0, 0, 0, "2026-09-02T00:00:00Z", "sess-1",
             "/fake/repos/solo"))
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, msg_id, req_id, "
            " input_tokens, output_tokens, cache_create_tokens, "
            " cache_read_tokens, cost_usd_raw, account_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (path, 0, "2026-09-02T09:00:00+00:00", "claude-opus-4-7", "m1",
             "r1", 100_000, 20_000, 0, 0, None, "unattributed"))
        cache.commit()
    finally:
        cache.close()
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-09-02T12:00:00Z")
    assert ns["cmd_weekly"](argparse.Namespace(
        since=None, until=None, json=True, breakdown=False, order="asc",
        mode="auto", offline=True, compact=False, format=None, theme=None,
        reveal_projects=False, no_branding=False, output=None, copy=False,
        open=False, tz=None, account=None, source="claude")) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(r for r in payload["weekly"] if r["week"] == WEEK_START_DATE)
    assert row["credited"] is True, row
    assert row["dollarsPerPercent"] is None, row
    assert row["dollarsPerPercentWithheld"] == "no-climb-since-credit", row


def test_the_weekly_bucket_key_set_is_scoped_to_its_account(ns):
    """One account's credit must not mark another's week (§6.2a)."""
    import types

    conn = ns["open_db"]()
    try:
        _seed(conn, with_credit=True)
        conn.execute(
            "UPDATE week_reset_events SET account_key = 'acct-a'")
        conn.commit()
        week = types.SimpleNamespace(
            start_date=dt.date.fromisoformat(WEEK_START_DATE),
            end_ts=WEEK_END_AT)
        assert ns["_credited_week_keys"](
            conn, [week], account_key="acct-a") == {WEEK_START_DATE}
        assert ns["_credited_week_keys"](
            conn, [week], account_key="acct-b") == set()
    finally:
        conn.close()
