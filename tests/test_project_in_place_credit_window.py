"""`cctally project` over a week that was credited in place.

An in-place credit ends one billing cycle and begins another inside the week,
so `_compute_subscription_weeks` returns TWO intervals for it. `cmd_project`
counts intervals, which moves three reported quantities at once:

  * `weeksInRange` counts SEGMENTS, so the credited week takes two slots and
    `--weeks N` spans a shorter calendar range than N times seven days. That
    is the decision: under the operator's rule a segment IS a billing cycle,
    and it is what `report` already renders.
  * the internal `weeks_missing_snapshot` set gains the POST-credit segment,
    whose `start_ts` is the credit instant and therefore matches no snapshot's
    `week_start_at`; that surfaces as
    `totals.weeklyAttributionAvailable == false`.
  * `attributedUsedPercent` MOVES UPWARD, because the PRE-credit segment's
    `start_ts` is the week's original `week_start_at` and now matches a
    snapshot again. Before the split existed, the credited week's only
    interval started at the credit instant, `_load_week_snapshots` never
    matched it, and the whole week contributed nothing.

The remaining mismatch is deliberate and out of scope here: the percentage
the credited week contributes is the POST-credit 12.0, not the pre-credit
peak, because `_load_week_snapshots` applies `_reset_aware_floor` per
`week_start_date` rather than per segment. `weekly` reports the two segments
separately (71.0 and 12.0). Filed as a follow-up, not fixed here.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import pytest

from conftest import load_script, redirect_paths


AS_OF = "2026-06-12T12:00:00Z"

WK1_START = dt.datetime(2026, 5, 29, 15, tzinfo=dt.timezone.utc)
WK1_END = dt.datetime(2026, 6, 5, 15, tzinfo=dt.timezone.utc)
WK2_START = WK1_END
WK2_END = dt.datetime(2026, 6, 12, 15, tzinfo=dt.timezone.utc)
EFFECTIVE = dt.datetime(2026, 6, 10, 9, tzinfo=dt.timezone.utc)


def _z(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canon(d: dt.datetime) -> str:
    """The `+00:00` spelling `_backfill_week_reset_events` writes. Seeding the
    event in this spelling is what lets `UNIQUE(old_week_end_at,
    new_week_end_at)` recognize the backfill's own attempt as a duplicate —
    its `already` pre-check compares `account_key = NULL`, which never matches
    in SQL, so the UNIQUE constraint is the only thing that dedups."""
    return d.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CCTALLY_AS_OF", AS_OF)
    return sys.modules["cctally"]


def _seed(app):
    conn = app.open_db()
    try:
        for captured, start, end, pct in [
            (WK1_START + dt.timedelta(days=3), WK1_START, WK1_END, 40.0),
            # Pre-credit peak, then the post-credit capture that reveals the
            # credit. The capture sits strictly AFTER `EFFECTIVE` so the
            # pre-credit segment's `captured_at_utc <= effective` lookup can
            # still resolve to the peak.
            (dt.datetime(2026, 6, 10, 8, tzinfo=dt.timezone.utc),
             WK2_START, WK2_END, 71.0),
            (dt.datetime(2026, 6, 10, 9, 30, tzinfo=dt.timezone.utc),
             WK2_START, WK2_END, 12.0),
        ]:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots("
                "  captured_at_utc, week_start_date, week_end_date, "
                "  week_start_at, week_end_at, weekly_percent, source, "
                "  payload_json) VALUES (?,?,?,?,?,?,?,?)",
                (_z(captured), start.date().isoformat(),
                 end.date().isoformat(), _z(start), _z(end), pct,
                 "fixture", json.dumps({"fixture": True})),
            )
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?,?,?,?)",
            (_canon(EFFECTIVE), _canon(EFFECTIVE), _canon(WK2_END),
             _canon(EFFECTIVE)),
        )
        conn.commit()
    finally:
        conn.close()

    conn = app.open_cache_db()
    try:
        # One entry per segment: week-1, pre-credit, post-credit.
        for i, ts in enumerate([
            dt.datetime(2026, 6, 1, 12, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 7, 12, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 11, 10, tzinfo=dt.timezone.utc),
        ]):
            path = "/fake/repos/solo/s.jsonl"
            if i == 0:
                conn.execute(
                    "INSERT INTO session_files(path, size_bytes, mtime_ns, "
                    " last_byte_offset, last_ingested_at, session_id, "
                    " project_path) VALUES (?,?,?,?,?,?,?)",
                    (path, 0, 0, 0, "2026-06-12T00:00:00Z", "sess-solo",
                     "/fake/repos/solo"),
                )
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, msg_id, "
                " req_id, input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cost_usd_raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (path, i, ts.isoformat(), "claude-opus-4-7", f"m{i}",
                 f"r{i}", 100_000, 20_000, 0, 0, None),
            )
        conn.commit()
    finally:
        conn.close()


def test_credited_week_contributes_two_intervals(app):
    """Guards the guard: without the split there is one interval for the
    credited week and every assertion below is vacuous."""
    _seed(app)
    conn = app.open_db()
    try:
        weeks = app._compute_subscription_weeks(
            conn,
            dt.datetime(2026, 5, 25, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 12, 12, tzinfo=dt.timezone.utc),
            account_key=None,
        )
    finally:
        conn.close()
    credited = [w for w in weeks if w.start_date == dt.date(2026, 6, 5)]
    assert len(credited) == 2, [(w.start_ts, w.end_ts) for w in weeks]
    assert credited[0].end_ts == credited[1].start_ts, "segments must abut"


def test_project_counts_segments_and_attributes_the_pre_credit_one(
    app, capsys,
):
    _seed(app)
    rc = app.main(["project", "--weeks", "3", "--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    payload = json.loads(out)

    # Three intervals: week-1, the pre-credit segment, the post-credit
    # segment. Under the old one-interval-per-week count the same `--weeks 3`
    # reached a week further back.
    assert payload["weeksInRange"] == 3, payload["weeksInRange"]

    # The post-credit segment starts at the credit instant, which is not any
    # snapshot's `week_start_at`, so at least one interval reports no snapshot
    # and the whole-window attribution flag goes false.
    assert payload["totals"]["weeklyAttributionAvailable"] is False, payload

    row = payload["projects"][0]
    # week-1 at 40.0 plus the pre-credit segment, which matches the credited
    # week's snapshot row again now that its `start_ts` is the original
    # `week_start_at`. `_load_week_snapshots` floors that week's MAX to the
    # credit instant, so the percentage it contributes is the post-credit
    # 12.0 — see the module docstring.
    assert row["attributedUsedPercent"] == pytest.approx(52.0, abs=1e-6), (
        f"expected 40.0 + 12.0; got {row['attributedUsedPercent']}"
    )
