"""The focal day must agree with the bucket keys (#443 S3 F23).

``display_tz=None`` means host-local bucketing on purpose. The focal-day
derivations fell back to UTC instead, so on a non-UTC host the builder
resolved a different current day than the one its rows were keyed by.

Four sites did this. Three assign ``today_iso``
(``_lib_cache_report.classify_and_summarize``,
``_cctally_dashboard_cache_report.build_cache_report_snapshot``,
``_cctally_dashboard_sources._codex_cache_report_wire``); the fourth is
an ENTRY FILTER in the Codex builder, which a ``today_iso =`` search
structurally cannot find, and whose consequence is that the breakdowns
can be drawn from a different entry population than the visible days.

NON-VACUITY: a midday instant has the same calendar date in most zones,
so these tests pin a near-midnight instant and assert the two dates
actually differ before checking anything else. Without that assertion a
fixture drifting back to noon would pass against the old code. The host
zone is Asia/Tokyo rather than a DST-observing zone because
``_resolve_bucket_tz(None)`` snapshots the offset at REAL now — a
DST zone would make the fixture's offset depend on the day the suite runs.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_BIN = ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402

UTC = dt.timezone.utc


@pytest.fixture
def host_tz_tokyo():
    """Asia/Tokyo: UTC+9 year-round, so the fixture offset never drifts."""
    prior = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    yield
    if prior is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = prior
    time.tzset()


def _row(crk, iso, hit_percent):
    """A day row whose ``cache_hit_percent`` is exactly ``hit_percent``."""
    r = crk.CacheRow(date=iso)
    r.input_tokens = 100 - hit_percent
    r.cache_read_tokens = hit_percent
    return r


# 23:30Z on New Year's Eve: UTC says 2025-12-31, Tokyo says 2026-01-01.
NOW_UTC = dt.datetime(2025, 12, 31, 23, 30, tzinfo=UTC)


def test_focal_day_follows_the_bucketing_fallback(host_tz_tokyo):
    """The kernel's ``today_iso`` must resolve through ``_resolve_bucket_tz``.

    ``_CacheReportResult`` does not expose ``today_iso``, so the focal day
    is observable only through what it changes. The rows below carry
    deliberately asymmetric hit percentages, and the two candidate focal
    days admit two different baseline populations:

        UTC focal day  2025-12-31 -> samples [10, 20, 30, 40, 50] -> 30.0
        host focal day 2026-01-01 -> samples [0, 10, 20, 30, 40, 50] -> 25.0

    Asserting a preconstructed bucket key instead would pass without ever
    exercising the faulty derivation.
    """
    import _lib_cache_report as crk

    utc_date = NOW_UTC.astimezone(UTC).strftime("%Y-%m-%d")
    host_date = NOW_UTC.astimezone(
        crk._resolve_bucket_tz(None)
    ).strftime("%Y-%m-%d")
    assert utc_date != host_date, (
        "non-vacuity: this instant must straddle the date boundary, "
        f"got utc={utc_date} host={host_date}"
    )
    assert (utc_date, host_date) == ("2025-12-31", "2026-01-01")

    rows = [
        _row(crk, "2025-12-26", 10),
        _row(crk, "2025-12-27", 20),
        _row(crk, "2025-12-28", 30),
        _row(crk, "2025-12-29", 40),
        _row(crk, "2025-12-30", 50),
        _row(crk, "2025-12-31", 0),
        _row(crk, "2026-01-01", 0),
    ]

    result = crk.classify_and_summarize(
        rows, now_utc=NOW_UTC, window_days=14,
        anomaly_threshold_pp=15, anomaly_window_days=14,
        display_tz=None, mode="day",
    )

    assert result.today_baseline_median == pytest.approx(25.0), (
        "the focal day was resolved in UTC, not in the zone the rows were "
        "bucketed by; 30.0 is the UTC answer"
    )


def _codex_entry(ts, *, project, input_tokens=100, cached=80):
    return SimpleNamespace(
        timestamp=ts,
        source_root_key="root-a",
        source_path="/synthetic/session.jsonl",
        project_label=project,
        model="gpt-5",
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=10,
        reasoning_output_tokens=2,
        total_tokens=input_tokens + 10,
        cost_usd=0.01,
    )


def _codex_report(tmp_path, monkeypatch, entries):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    source_module = sys.modules["_cctally_dashboard_sources"]
    return source_module._codex_cache_report_wire(
        tuple(entries), metadata={}, now_utc=NOW_UTC,
        display_tz_name=None, speed="standard",
    )


def _boundary_fixture():
    """15 host-local day rows plus one entry on the drop boundary.

    Every ``12:00Z`` entry shares its calendar date between UTC and Tokyo,
    so they pin the row set. The two entries that matter are near
    midnight, where the two zones disagree:

      - ``2025-12-31T23:00Z`` -> Tokyo 2026-01-01, UTC 2025-12-31. It is
        what makes a host-local ``today`` row exist at all.
      - ``2025-12-18T23:00Z`` -> Tokyo 2025-12-19, UTC 2025-12-18. Its
        day row (2025-12-19) survives the 14-row cap while 2025-12-18
        does not, so a UTC-keyed entry filter drops spend whose day IS
        on screen.
    """
    entries = [
        _codex_entry(
            dt.datetime(2025, 12, d, 12, 0, tzinfo=UTC), project="main-project",
        )
        for d in range(18, 32)
    ]
    entries.append(_codex_entry(
        dt.datetime(2025, 12, 31, 23, 0, tzinfo=UTC), project="main-project",
    ))
    entries.append(_codex_entry(
        dt.datetime(2025, 12, 18, 23, 0, tzinfo=UTC), project="boundary-project",
    ))
    return entries


def test_codex_spotlight_day_follows_the_bucketing_fallback(
    tmp_path, monkeypatch, host_tz_tokyo,
):
    """The Codex spotlight date must be the day its own rows are keyed by."""
    report = _codex_report(tmp_path, monkeypatch, _boundary_fixture())

    assert report["days"][0]["date"] == "2026-01-01"
    assert report["today"]["date"] == report["days"][0]["date"], (
        "the spotlight resolved its current day in UTC while the day rows "
        "were bucketed host-local"
    )
    assert report["today"]["observed"] is True


def test_codex_breakdowns_are_drawn_from_the_visible_days(
    tmp_path, monkeypatch, host_tz_tokyo,
):
    """The fourth site: the ``kept_entries`` filter, not a ``today_iso``.

    ``boundary-project``'s only entry lands on host-local 2025-12-19,
    which survives the 14-row cap. Filtering by its UTC date (2025-12-18)
    drops it, so the breakdown totals disagree with the days shown beside
    them.
    """
    report = _codex_report(tmp_path, monkeypatch, _boundary_fixture())

    kept_dates = {row["date"] for row in report["days"]}
    assert "2025-12-19" in kept_dates
    assert "2025-12-18" not in kept_dates, (
        "fixture precondition: the 14-row cap must drop 2025-12-18, so the "
        "UTC and host-local filters disagree about the boundary entry"
    )

    keys = {row["key"] for row in report["by_project"]}
    assert "boundary-project" in keys, (
        "an entry whose day row IS on screen was excluded from the "
        "breakdowns because the filter resolved its date in UTC"
    )
