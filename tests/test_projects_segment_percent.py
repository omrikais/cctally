"""`project` resolves each cycle's percentage in its own interval (#750 S4).

Spec §2.1, §2.4, §2.5. `_load_week_snapshots` returned
``{week_start_utc -> max(weekly_percent)}`` keyed on
`weekly_usage_snapshots.week_start_at` while `cmd_project` looked each week up
with a `SubWeek.start_ts` instant. On a credited week those are different
quantities: the post-credit segment matched nothing and landed in
`weeks_missing_snapshot`, and the pre-credit segment received the post-credit
reading. That is #731.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
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
    return d.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CCTALLY_AS_OF", AS_OF)
    return sys.modules["cctally"]


def _seed(app, *, snapshots, credit_floor=None):
    conn = app.open_db()
    try:
        for captured, start, end, pct in snapshots:
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
        if credit_floor is not None:
            conn.execute(
                "INSERT INTO weekly_credit_floors "
                "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
                " applied_at_utc) VALUES (?,?,?,?)",
                (WK2_START.date().isoformat(), _z(credit_floor), 71.0,
                 _z(credit_floor)),
            )
        conn.commit()
    finally:
        conn.close()

    conn = app.open_cache_db()
    try:
        path = "/fake/repos/solo/s.jsonl"
        conn.execute(
            "INSERT INTO session_files(path, size_bytes, mtime_ns, "
            " last_byte_offset, last_ingested_at, session_id, "
            " project_path) VALUES (?,?,?,?,?,?,?)",
            (path, 0, 0, 0, "2026-06-12T00:00:00Z", "sess-solo",
             "/fake/repos/solo"),
        )
        for i, ts in enumerate([
            dt.datetime(2026, 6, 1, 12, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 7, 12, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 11, 10, tzinfo=dt.timezone.utc),
        ]):
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, msg_id, "
                " req_id, input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cost_usd_raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (path, i, ts.isoformat(), "claude-opus-4-7", f"m{i}",
                 f"r{i}", 100_000, 20_000, 0, 0, None),
            )
        conn.commit()
    finally:
        conn.close()


FULL_SNAPSHOTS = [
    (WK1_START + dt.timedelta(days=3), WK1_START, WK1_END, 40.0),
    (dt.datetime(2026, 6, 10, 8, tzinfo=dt.timezone.utc),
     WK2_START, WK2_END, 71.0),
    (dt.datetime(2026, 6, 10, 9, 30, tzinfo=dt.timezone.utc),
     WK2_START, WK2_END, 12.0),
]


def _segments(app):
    conn = app.open_db()
    try:
        return app._compute_subscription_weeks(
            conn, dt.datetime(2026, 5, 25, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 12, 12, tzinfo=dt.timezone.utc),
            account_key=None,
        )
    finally:
        conn.close()


def test_project_reports_the_same_per_segment_percents_as_weekly(app):
    """#731. `weekly` reads 71.0 and 12.0 on the fixture week's two segments;
    `project` attributed 12.0 to the pre-credit one and nothing to the
    post-credit one."""
    _seed(app, snapshots=FULL_SNAPSHOTS)
    segments = _segments(app)
    credited = [w for w in segments if w.start_date == dt.date(2026, 6, 5)]
    assert len(credited) == 2, [(w.start_ts, w.end_ts) for w in segments]

    by_segment = app._load_week_snapshots(segments, account_key=None)
    assert by_segment[
        app.parse_iso_datetime(credited[0].start_ts, "seg").astimezone(
            dt.timezone.utc)] == 71.0
    assert by_segment[
        app.parse_iso_datetime(credited[1].start_ts, "seg").astimezone(
            dt.timezone.utc)] == 12.0
    assert by_segment[WK1_START] == 40.0


def test_weeks_missing_snapshot_holds_segment_identities(app, capsys):
    """N cuts can report up to N+1 independently missing observations. Here
    the post-credit cycle has none of its own, and the reducer must report it
    as missing rather than hand it the pre-credit reading."""
    _seed(app, snapshots=FULL_SNAPSHOTS[:2])  # nothing after the credit
    segments = _segments(app)
    credited = [w for w in segments if w.start_date == dt.date(2026, 6, 5)]
    by_segment = app._load_week_snapshots(segments, account_key=None)
    post_key = app.parse_iso_datetime(
        credited[1].start_ts, "seg").astimezone(dt.timezone.utc)
    pre_key = app.parse_iso_datetime(
        credited[0].start_ts, "seg").astimezone(dt.timezone.utc)
    assert pre_key in by_segment and by_segment[pre_key] == 71.0
    assert post_key not in by_segment, (
        "a credited cycle with no observation of its own must be MISSING, "
        "not resolved to the previous cycle's reading"
    )

    rc = app.main(["project", "--weeks", "3", "--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["totals"]["weeklyAttributionAvailable"] is False, payload


def test_the_manual_credit_floor_still_raises_a_segments_lower_bound(app):
    """§2.1. The automatic-reset leg of `_reset_aware_floor` is replaced by the
    segment's own bound; the manual `weekly_credit_floors` leg stays, because
    `record-credit` writes a same-window floor that creates no segment."""
    floor_at = dt.datetime(2026, 6, 10, 10, tzinfo=dt.timezone.utc)
    _seed(app, snapshots=FULL_SNAPSHOTS + [
        (dt.datetime(2026, 6, 10, 11, tzinfo=dt.timezone.utc),
         WK2_START, WK2_END, 3.0),
    ], credit_floor=floor_at)
    segments = _segments(app)
    credited = [w for w in segments if w.start_date == dt.date(2026, 6, 5)]
    conn = app.open_db()
    try:
        bounds = app.segment_capture_bounds(
            conn, segments, account_key=None)
    finally:
        conn.close()
    post = credited[1]
    since, _before = bounds[post.segment_key]
    assert since == "2026-06-10T10:00:00Z", (
        "the manual floor sits inside the post-credit cycle and raises its "
        f"lower bound above the segment start; got {since!r}"
    )
    by_segment = app._load_week_snapshots(segments, account_key=None)
    post_key = app.parse_iso_datetime(
        post.start_ts, "seg").astimezone(dt.timezone.utc)
    assert by_segment[post_key] == 3.0, (
        "the 09:30 capture is below the manual floor and must be dropped"
    )


def test_modelled_attribution_receives_the_segments_real_end(app, capsys,
                                                             monkeypatch):
    """§2.5. `resolve_week_attribution(..., week_end=ws + 7d)` is not the
    cycle end once `ws` is a credit cut; the emitted end is threaded through
    instead."""
    _seed(app, snapshots=FULL_SNAPSHOTS)
    seen: list = []
    real = sys.modules["_cctally_project"].resolve_week_attribution

    def _spy(*args, **kwargs):
        seen.append((kwargs.get("week_start"), kwargs.get("week_end")))
        return real(*args, **kwargs)

    monkeypatch.setattr(
        sys.modules["_cctally_project"], "resolve_week_attribution", _spy)
    rc = app.main(["project", "--weeks", "3", "--json"])
    assert rc == 0, capsys.readouterr().out
    ends = dict(seen)
    assert ends[WK2_START] == EFFECTIVE, (
        f"the pre-credit cycle ends at the credit instant; got {ends}"
    )
    assert ends[EFFECTIVE] == WK2_END, (
        f"the post-credit cycle ends at the week's end; got {ends}"
    )
    assert all(
        end != start + dt.timedelta(days=7)
        for start, end in seen if start in (WK2_START, EFFECTIVE)
    ), seen


def test_a_missing_snapshot_table_withholds_rather_than_guessing(app):
    """A stats.db with no `weekly_usage_snapshots` yields one `None` per
    segment, never a partial map that would read as "these cycles have no
    usage"."""
    ns = load_script()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        segs = [ns["SubWeek"](
            start_ts=_z(WK1_START), end_ts=_z(WK1_END),
            start_date=WK1_START.date(), end_date=WK1_END.date(),
            source="snapshot", display_start_date=WK1_START.date(),
        )]
        out = ns["latest_usage_by_segment"](conn, segs, account_key=None)
        assert out == {segs[0].segment_key: None}
    finally:
        conn.close()
