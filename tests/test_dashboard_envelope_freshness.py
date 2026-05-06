"""Dashboard envelope carries current_week.freshness derived from
TuiCurrentWeek.latest_snapshot_at."""
import datetime as dt
import pytest
from conftest import load_script


@pytest.fixture(scope="module")
def ns():
    return load_script()


def _build_snap(ns, latest_at):
    """Build a minimal DataSnapshot for the test.

    Schema verified at bin/cctally:17057 (TuiCurrentWeek) and :17227
    (DataSnapshot). `latest_snapshot_at` is a `datetime`, not an ISO
    string. DataSnapshot's optional list fields default-construct,
    so we only pass the non-default required ones.
    """
    cw = ns["TuiCurrentWeek"](
        week_start_at=dt.datetime(2026, 4, 26, 12, 0, tzinfo=dt.timezone.utc),
        week_end_at=dt.datetime(2026, 5, 2, 12, 0, tzinfo=dt.timezone.utc),
        used_pct=57.0,
        five_hour_pct=None,
        five_hour_resets_at=None,
        spent_usd=10.0,
        dollars_per_percent=0.18,
        latest_snapshot_at=latest_at,
    )
    return ns["DataSnapshot"](
        current_week=cw,
        forecast=None,
        trend=[],
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=dt.timezone.utc),
    )


def test_envelope_freshness_fresh(ns):
    now = dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
    latest = dt.datetime(2026, 4, 30, 11, 59, 50, tzinfo=dt.timezone.utc)
    env = ns["snapshot_to_envelope"](_build_snap(ns, latest), now_utc=now)
    assert env["current_week"]["freshness"]["label"] == "fresh"


def test_envelope_freshness_stale(ns):
    now = dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
    latest = dt.datetime(2026, 4, 30, 11, 55, 0, tzinfo=dt.timezone.utc)  # 5 min
    env = ns["snapshot_to_envelope"](_build_snap(ns, latest), now_utc=now)
    assert env["current_week"]["freshness"]["label"] == "stale"


def test_envelope_freshness_missing_when_no_snapshot_ts(ns):
    """latest_snapshot_at is non-Optional in TuiCurrentWeek, but the
    envelope should defensively handle a falsy value (None) if a future
    refactor makes it Optional. For now, this test passes a plausible
    'no-data' state via cw=None on DataSnapshot."""
    now = dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
    snap = ns["DataSnapshot"](
        current_week=None,
        forecast=None,
        trend=[],
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=now,
    )
    env = ns["snapshot_to_envelope"](snap, now_utc=now)
    # current_week serializes to None when cw is None.
    assert env["current_week"] is None
