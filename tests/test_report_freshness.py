"""report --json freshness field per row.

Adapts the plan's test to the actual `cmd_report` JSON shape, which is
camelCase (`weeklyPercent`, `usageCapturedAt`) and emits a top-level
`{current, trend, ...}` object rather than a flat list. The invariant
asserted is identical: a row whose data is recent (< 24h) carries a
`freshness` envelope; older rows omit the key.
"""
import argparse
import datetime as dt_mod
import json as _json
import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns_with_paths(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _report_args(**overrides):
    base = dict(
        json=True,
        sync_current=False,
        weeks=8,
        week_start_name=None,
        mode="auto",
        offline=False,
        project=None,
        detail=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_report_json_carries_freshness_for_recent_rows(
    ns_with_paths, capsys
):
    """Recent (now-24h) rows carry freshness; older rows do not."""
    ns = ns_with_paths
    now = dt_mod.datetime.now(dt_mod.timezone.utc)
    recent_iso = (now - dt_mod.timedelta(minutes=10)).isoformat()
    old_iso = (now - dt_mod.timedelta(days=3)).isoformat()

    conn = ns["open_db"]()
    try:
        # Recent row (10 minutes old).
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            "week_end_at, weekly_percent, source, payload_json, "
            "five_hour_percent, five_hour_resets_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (recent_iso, "2026-04-26", "2026-05-02",
             "2026-04-26T12:00:00+00:00", "2026-05-02T12:00:00+00:00",
             57.0, "test", "{}", 5.0, "2026-04-30T17:00:00+00:00"),
        )
        # Old row (3 days ago) on a different week.
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            "week_end_at, weekly_percent, source, payload_json, "
            "five_hour_percent, five_hour_resets_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (old_iso, "2026-04-19", "2026-04-25",
             "2026-04-19T12:00:00+00:00", "2026-04-26T12:00:00+00:00",
             90.0, "test", "{}", 0.0, "2026-04-27T17:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    rc = ns["cmd_report"](_report_args())
    out = capsys.readouterr().out
    assert rc == 0
    parsed = _json.loads(out)
    trend = parsed["trend"]
    # Find recent row (weeklyPercent == 57.0).
    recent = next(
        (r for r in trend if r.get("weeklyPercent") == 57.0),
        None,
    )
    assert recent is not None
    assert "freshness" in recent
    assert recent["freshness"]["label"] in ("fresh", "aging", "stale")
    assert recent["freshness"]["captured_at"] == recent_iso
    assert isinstance(recent["freshness"]["age_seconds"], int)
    # Old row must NOT have freshness key (older than 24h).
    old = next(
        (r for r in trend if r.get("weeklyPercent") == 90.0),
        None,
    )
    assert old is not None
    assert "freshness" not in old
