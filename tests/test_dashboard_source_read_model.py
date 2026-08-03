"""Provider-native Codex dashboard read model contracts for #294 S4."""
from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import sqlite3
import shutil
import sys
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from _cctally_dashboard_sources import (
    DashboardReadContext,
    build_codex_source_state,
    codex_projection_coherence,
    refresh_codex_source_clock,
    resolve_dashboard_source_semantics,
)
from conftest import load_script, redirect_paths
from _lib_dashboard_sources import SOURCE_SCHEMA_VERSION
from _lib_quota import QuotaObservation, QuotaWindowIdentity


UTC = dt.timezone.utc
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
START = dt.datetime(2026, 7, 1, tzinfo=UTC)
NOW = dt.datetime(2026, 7, 20, tzinfo=UTC)


def _quota_observation(
    *,
    root: str,
    window_minutes: int,
    resets_at: dt.datetime,
    captured_at: dt.datetime = NOW - dt.timedelta(minutes=10),
    limit_name: str | None = None,
    logical_limit_key: str = "limit",
    observed_slot: str = "primary",
    used_percent: float = 25.0,
    account_key: str | None = None,
    line_offset: int = 1,
) -> QuotaObservation:
    return QuotaObservation(
        identity=QuotaWindowIdentity(
            source="codex",
            source_root_key=root,
            logical_limit_key=logical_limit_key,
            observed_slot=observed_slot,
            window_minutes=window_minutes,
            limit_name=limit_name,
            **({"account_key": account_key} if account_key is not None else {}),
        ),
        captured_at=captured_at,
        used_percent=used_percent,
        resets_at=resets_at,
        source_path=f"/private/{root}.jsonl",
        line_offset=line_offset,
    )


def test_native_quota_labels_derive_familiar_names_from_duration():
    source_module = sys.modules["_cctally_dashboard_sources"]

    assert source_module._native_limit_label("  five-hour quota  ", 300) == "five-hour quota"
    assert source_module._native_limit_label(" Weekly limit ", 10_080) == "Weekly limit"
    assert source_module._native_limit_label(None, 90) == "90-minute limit"


def test_codex_blocks_wire_uses_current_cycle_five_hour_activity_and_models(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    source_module = sys.modules["_cctally_dashboard_sources"]
    cycle = source_module.CodexCycleBoundary(
        window_minutes=10_080,
        start_at=dt.datetime(2026, 7, 13, tzinfo=UTC),
        resets_at=dt.datetime(2026, 7, 20, tzinfo=UTC),
        source_root_keys=("root-a",),
    )
    try:
        stats.executemany(
            "INSERT INTO quota_window_blocks "
            "(source, source_root_key, logical_limit_key, observed_slot, "
            "window_minutes, limit_name, resets_at_utc, nominal_start_at_utc, "
            "first_observed_at_utc, last_observed_at_utc, first_percent, "
            "current_percent, last_source_path, last_line_offset, generation) "
            "VALUES ('codex', 'root-a', ?, 'primary', ?, ?, ?, ?, ?, ?, 1, 2, ?, 1, 'g')",
            (
                (
                    "five-hour", 300, "5-hour limit",
                    "2026-07-18T15:00:00+00:00", "2026-07-18T10:00:00+00:00",
                    "2026-07-18T10:05:00+00:00", "2026-07-18T14:00:00+00:00",
                    "/private/five-hour.jsonl",
                ),
                (
                    "weekly", 10_080, "7-day limit",
                    "2026-07-20T00:00:00+00:00", "2026-07-13T00:00:00+00:00",
                    "2026-07-13T00:05:00+00:00", "2026-07-18T14:00:00+00:00",
                    "/private/weekly.jsonl",
                ),
            ),
        )
        entries = (
            SimpleNamespace(
                timestamp=dt.datetime(2026, 7, 18, 11, tzinfo=UTC),
                source_root_key="root-a", model="gpt-5.6-sol", cost_usd=7.0,
                input_tokens=100, cached_input_tokens=80, output_tokens=10,
                reasoning_output_tokens=2, total_tokens=110,
            ),
            SimpleNamespace(
                timestamp=dt.datetime(2026, 7, 18, 12, tzinfo=UTC),
                source_root_key="root-a", model="gpt-5.6-terra", cost_usd=3.0,
                input_tokens=50, cached_input_tokens=20, output_tokens=5,
                reasoning_output_tokens=1, total_tokens=55,
            ),
        )

        rows = source_module._quota_wire(
            stats,
            accounting_entries=entries,
            cycle=cycle,
            now_utc=dt.datetime(2026, 7, 18, 13, tzinfo=UTC),
            display_tz_name="UTC",
        )

        assert len(rows) == 1
        assert rows[0]["window_minutes"] == 300
        assert rows[0]["cost_usd"] == 10.0
        assert [row["modelName"] for row in rows[0]["model_breakdowns"]] == [
            "gpt-5.6-sol", "gpt-5.6-terra",
        ]
        assert rows[0]["model_breakdowns"][0]["inputTokens"] == 100

        stats.execute("DELETE FROM quota_window_blocks WHERE window_minutes=300")
        assert source_module._quota_wire(
            stats,
            accounting_entries=entries,
            cycle=cycle,
            now_utc=dt.datetime(2026, 7, 18, 13, tzinfo=UTC),
            display_tz_name="UTC",
        ) == ()
    finally:
        stats.close()


def test_codex_cache_report_computes_savings_and_breakdowns_from_native_counters(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    source_module = sys.modules["_cctally_dashboard_sources"]
    entry = SimpleNamespace(
        timestamp=NOW - dt.timedelta(hours=1),
        source_root_key="root-a",
        source_path="/private/session.jsonl",
        project_label="cctally-dev",
        model="gpt-5",
        input_tokens=100,
        cached_input_tokens=80,
        output_tokens=10,
        reasoning_output_tokens=2,
        total_tokens=110,
        cost_usd=0.01,
    )

    report = source_module._codex_cache_report_wire(
        (entry,), metadata={}, now_utc=NOW,
        display_tz_name="UTC", speed="standard",
    )

    assert report["is_empty"] is False
    # NOW is midnight UTC, so this entry is dated YESTERDAY and #443 S2
    # inserts an unobserved synthetic row for today at position 0. The
    # measured row is therefore the observed one, not days[0].
    measured = next(row for row in report["days"] if row["observed"])
    assert "cache_hit_percent" not in measured
    assert measured["cached_input_percent"] == pytest.approx(80.0)
    assert measured["saved_usd"] == pytest.approx(
        80 * (1.25e-6 - 1.25e-7)
    )
    assert measured["net_usd"] == measured["saved_usd"]
    assert measured["wasted_usd"] is None
    assert measured["cache_creation_tokens"] == 0
    assert report["fourteen_day_counterfactual_usd"] == measured["saved_usd"]
    assert report["fourteen_day_efficiency_ratio"] is None
    assert report["by_project"][0]["key"] == "cctally-dev"
    assert report["by_model"][0]["key"] == "gpt-5"


def test_codex_empty_report_carries_codex_vocabulary(tmp_path, monkeypatch):
    """#443 S2 F18 — the empty early return goes through the shared builder.

    Before S2 it was a hand-built literal that omitted anomaly_unevaluated
    and observed entirely, so the empty and populated Codex returns had
    different shapes.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    source_module = sys.modules["_cctally_dashboard_sources"]

    report = source_module._codex_cache_report_wire(
        (), metadata={}, now_utc=NOW,
        display_tz_name="UTC", speed="standard",
    )

    assert report["is_empty"] is True
    assert report["today"]["cached_input_percent"] == 0.0
    assert "cache_hit_percent" not in report["today"]
    assert report["today"]["wasted_usd"] is None
    assert report["fourteen_day_efficiency_ratio"] is None
    assert report["anomaly_predicates"] == ["cache_drop"]
    assert "wasted_usd" in report["not_applicable"]
    # An empty store measured nothing, so every applicable predicate is
    # unevaluated and today is unobserved.
    assert report["today"]["observed"] is False
    assert report["today"]["anomaly_unevaluated"] == ["cache_drop"]


# === #443 S2 — the synthetic unobserved Codex today row ==================
# NOW is midnight UTC, so day 0 lands exactly on it: dated today, and not
# after now_utc. Nudging it forward would place the entry outside the
# qualified reader's half-open window, making the observed-today tests
# assert a state production cannot produce.

def _codex_entry(days_ago, *, input_tokens=100, cached=80):
    return SimpleNamespace(
        timestamp=NOW - dt.timedelta(days=days_ago),
        source_root_key="root-a", source_path="/private/session.jsonl",
        project_label="cctally-dev", model="gpt-5",
        input_tokens=input_tokens, cached_input_tokens=cached,
        output_tokens=10, reasoning_output_tokens=2,
        total_tokens=input_tokens + 10, cost_usd=0.01,
    )


def _wire(entries, source_module):
    return source_module._codex_cache_report_wire(
        tuple(entries), metadata={}, now_utc=NOW,
        display_tz_name="UTC", speed="standard",
    )


def test_codex_idle_today_gets_an_unobserved_synthetic_row(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    source_module = sys.modules["_cctally_dashboard_sources"]
    # Newest real entry is YESTERDAY.
    report = _wire([_codex_entry(d) for d in range(1, 10)], source_module)

    today_iso = NOW.strftime("%Y-%m-%d")
    assert report["days"][0]["date"] == today_iso
    assert report["days"][0]["observed"] is False
    assert report["days"][0]["cached_input_percent"] == 0.0
    assert report["today"]["observed"] is False
    assert sorted(report["days"][0]["anomaly_unevaluated"]) == ["cache_drop"]


def test_synthetic_row_does_not_inflate_the_baseline_count(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    source_module = sys.modules["_cctally_dashboard_sources"]
    report = _wire([_codex_entry(d) for d in range(1, 10)], source_module)
    # Nine real non-today rows. The synthetic row must not become a tenth,
    # or it would push a thin history over the five-sample baseline floor.
    assert report["today"]["baseline_daily_row_count"] == 9


def test_codex_active_today_row_is_observed(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    source_module = sys.modules["_cctally_dashboard_sources"]
    report = _wire([_codex_entry(0), _codex_entry(1)], source_module)
    assert report["days"][0]["date"] == NOW.strftime("%Y-%m-%d")
    assert report["days"][0]["observed"] is True
    assert report["today"]["observed"] is True


def test_codex_cycle_selects_the_active_seven_day_boundary_over_five_hour_limit():
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)

    cycle = source_module._resolve_codex_weekly_cycle((
        _quota_observation(root="root", window_minutes=300, resets_at=NOW + dt.timedelta(hours=4)),
        _quota_observation(root="root", window_minutes=10_080, resets_at=reset),
    ), NOW)[0]  # #341: per-account list — single-account scenario -> 1 element

    assert cycle.window_minutes == 10_080
    assert cycle.start_at == reset - dt.timedelta(days=7)
    assert cycle.resets_at == reset


def test_codex_cycle_allows_a_fresh_weekly_boundary_without_a_five_hour_window():
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)

    cycle = source_module._resolve_codex_weekly_cycle((
        _quota_observation(root="root", window_minutes=10_080, resets_at=reset),
    ), NOW)[0]  # #341: per-account list — single-account scenario -> 1 element

    assert cycle.resets_at == reset


def test_codex_cycle_ignores_a_concurrent_model_scoped_spark_week():
    source_module = sys.modules["_cctally_dashboard_sources"]
    standard_reset = NOW + dt.timedelta(days=5)
    spark_reset = NOW + dt.timedelta(days=7)
    spark_key = '{"modelPool":"gpt-5.3-codex-spark"}'

    cycle = source_module._resolve_codex_weekly_cycle((
        _quota_observation(
            root="root", window_minutes=10_080, resets_at=standard_reset,
            logical_limit_key="standard-limit",
        ),
        _quota_observation(
            root="root", window_minutes=10_080, resets_at=spark_reset,
            logical_limit_key=spark_key,
        ),
    ), NOW)[0]  # #341: per-account list — single-account scenario -> 1 element

    assert cycle.resets_at == standard_reset


def test_codex_weekly_rows_follow_native_reset_reanchors_not_calendar_weeks(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = "root-native-weekly"
    try:
        stats.executemany(
            "INSERT INTO quota_window_blocks "
            "(source, source_root_key, logical_limit_key, observed_slot, "
            "window_minutes, limit_name, resets_at_utc, nominal_start_at_utc, "
            "first_observed_at_utc, last_observed_at_utc, first_percent, "
            "current_percent, last_source_path, last_line_offset, generation) "
            "VALUES ('codex', ?, ?, 'primary', 10080, '7-day limit', ?, ?, ?, ?, "
            "0, 10, ?, 1, 'g')",
            (
                (
                    root, "weekly-a", "2026-07-08T00:00:00+00:00",
                    "2026-07-01T00:00:00+00:00", "2026-07-01T00:05:00+00:00",
                    "2026-07-02T23:00:00+00:00", "/private/a.jsonl",
                ),
                (
                    root, "weekly-b", "2026-07-10T00:00:00+00:00",
                    "2026-07-03T00:00:00+00:00", "2026-07-03T00:05:00+00:00",
                    "2026-07-09T23:00:00+00:00", "/private/b.jsonl",
                ),
                (
                    root, "weekly-b-jitter", "2026-07-10T00:00:30+00:00",
                    "2026-07-03T00:00:30+00:00", "2026-07-03T00:05:30+00:00",
                    "2026-07-09T23:00:30+00:00", "/private/b-jitter.jsonl",
                ),
                (
                    root, '{"modelPool":"gpt-5.3-codex-spark"}',
                    "2026-07-12T00:00:00+00:00", "2026-07-05T00:00:00+00:00",
                    "2026-07-05T00:00:05+00:00", "2026-07-05T00:01:00+00:00",
                    "/private/spark.jsonl",
                ),
            ),
        )
        entries = (
            SimpleNamespace(
                timestamp=dt.datetime(2026, 7, 2, 12, tzinfo=UTC),
                source_root_key=root, source_path="/private/first.jsonl", session_id="first",
                model="gpt-5", input_tokens=100, cached_input_tokens=0,
                output_tokens=10, reasoning_output_tokens=0, total_tokens=110,
            ),
            SimpleNamespace(
                timestamp=dt.datetime(2026, 7, 3, 0, tzinfo=UTC),
                source_root_key=root, source_path="/private/boundary.jsonl", session_id="boundary",
                model="gpt-5", input_tokens=200, cached_input_tokens=0,
                output_tokens=20, reasoning_output_tokens=0, total_tokens=220,
            ),
            SimpleNamespace(
                timestamp=dt.datetime(2026, 7, 4, 0, tzinfo=UTC),
                source_root_key=root, source_path="/private/spark.jsonl", session_id="spark",
                model="gpt-5.3-codex-spark", input_tokens=400, cached_input_tokens=0,
                output_tokens=40, reasoning_output_tokens=0, total_tokens=440,
            ),
            SimpleNamespace(
                timestamp=dt.datetime(2026, 7, 8, 12, tzinfo=UTC),
                source_root_key=root, source_path="/private/second.jsonl", session_id="second",
                model="gpt-5", input_tokens=300, cached_input_tokens=0,
                output_tokens=30, reasoning_output_tokens=0, total_tokens=330,
            ),
        )

        periods = source_module._codex_weekly_periods(
            stats, source_root_keys=(root,), active_cycle=None,
        )
        view = source_module._build_codex_native_weekly_view(
            stats, entries, source_root_keys=(root,), active_cycle=None,
            now_utc=dt.datetime(2026, 7, 9, tzinfo=UTC),
            display_tz_name="UTC", speed="standard",
        )

        assert [(row.start_at, row.end_at) for row in periods] == [
            (
                dt.datetime(2026, 7, 1, tzinfo=UTC),
                dt.datetime(2026, 7, 3, tzinfo=UTC),
            ),
            (
                dt.datetime(2026, 7, 3, tzinfo=UTC),
                dt.datetime(2026, 7, 10, 0, 0, 30, tzinfo=UTC),
            ),
        ]
        assert [row.bucket for row in view.rows] == [
            "07-01 00:00", "07-03 00:00",
        ]
        assert [row.input_tokens for row in view.rows] == [100, 500]
        assert [row.used_pct for row in view.rows] == [10, 10]
        assert all(row.dollar_per_pct == pytest.approx(row.cost_usd / 10) for row in view.rows)
        assert [row.period_start_at for row in view.rows] == [
            dt.datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
            dt.datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
        ]
        assert [row.period_end_at for row in view.rows] == [
            dt.datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
            dt.datetime(2026, 7, 10, 0, 0, 30, tzinfo=UTC),
        ]
        assert view.total_tokens == 660
    finally:
        stats.close()


# =========================================================================
# #350 — build-time weekly-cycle ranking (spec §3.2, §5.2).
#
# "Projections blank. Actuals stay." A weekly quota observation goes STALE after
# exactly one idle hour (`stale_after_seconds(10_080) == 3600`), and Codex has no
# background poll, so a lone stale-but-future boundary must still bound the hero's
# backward-looking actuals. Ranking is FRESH-FIRST: one fresh boundary wins; else
# one stale boundary wins and the cycle is marked stale. Only the EXACT `"stale"`
# freshness state is eligible — `"future"` and `"unavailable"` stay invalid.
# =========================================================================


def _stale_weekly_observation(
    *, root: str = "root", resets_at: dt.datetime | None = None,
    logical_limit_key: str = "limit", account_key: str | None = None,
    used_percent: float = 25.0, now_utc: dt.datetime = NOW,
) -> QuotaObservation:
    """One weekly observation whose capture is older than its 3600s stale bound."""
    return _quota_observation(
        root=root,
        window_minutes=10_080,
        resets_at=resets_at if resets_at is not None else now_utc + dt.timedelta(days=2),
        captured_at=now_utc - dt.timedelta(hours=2),
        logical_limit_key=logical_limit_key,
        account_key=account_key,
        used_percent=used_percent,
    )


def test_lone_stale_future_weekly_boundary_yields_a_live_cycle():
    """#350: the headline. A stale-but-future weekly boundary keeps the hero's
    actuals bounded instead of raising `CodexCycleUnavailable("stale")`."""
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)

    cycles = source_module._resolve_codex_weekly_cycle(
        (_stale_weekly_observation(resets_at=reset),), NOW,
    )

    assert len(cycles) == 1
    assert cycles[0].resets_at == reset
    assert cycles[0].resets_at > NOW
    assert cycles[0].start_at == reset - dt.timedelta(days=7)
    assert cycles[0].used_percent == 25.0
    assert cycles[0].evidence_stale is True


def test_one_fresh_plus_one_stale_boundary_prefers_the_fresh_one():
    """Regression guard for the ranking rule: a FLAT count would call this
    `conflicting`. It resolves valid TODAY and must keep doing so — fresh-first."""
    source_module = sys.modules["_cctally_dashboard_sources"]
    fresh_reset = NOW + dt.timedelta(days=2)
    stale_reset = NOW + dt.timedelta(days=5)

    cycles = source_module._resolve_codex_weekly_cycle((
        _quota_observation(
            root="root-fresh", window_minutes=10_080, resets_at=fresh_reset,
            captured_at=NOW - dt.timedelta(minutes=10), logical_limit_key="limit-fresh",
        ),
        _stale_weekly_observation(
            root="root-stale", resets_at=stale_reset, logical_limit_key="limit-stale",
        ),
    ), NOW)

    assert len(cycles) == 1
    assert cycles[0].resets_at == fresh_reset
    assert cycles[0].evidence_stale is False


def test_two_stale_boundaries_with_no_fresh_is_conflicting():
    source_module = sys.modules["_cctally_dashboard_sources"]

    with pytest.raises(source_module.CodexCycleUnavailable, match="conflicting"):
        source_module._resolve_codex_weekly_cycle((
            _stale_weekly_observation(
                root="root-a", resets_at=NOW + dt.timedelta(days=1),
                logical_limit_key="limit-a",
            ),
            _stale_weekly_observation(
                root="root-b", resets_at=NOW + dt.timedelta(days=2),
                logical_limit_key="limit-b",
            ),
        ), NOW)


def test_future_dated_weekly_evidence_is_never_a_stale_fallback():
    """`"future"` freshness must stay INVALID. The identity carries a live
    baseline (an already-captured point) plus a future-dated latest physical
    capture, so `quota_freshness` reports exactly `"future"` — not `"stale"`."""
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)

    with pytest.raises(source_module.CodexCycleUnavailable):
        source_module._resolve_codex_weekly_cycle((
            _quota_observation(
                root="root", window_minutes=10_080, resets_at=reset,
                captured_at=NOW - dt.timedelta(minutes=10), used_percent=25.0,
            ),
            _quota_observation(
                root="root", window_minutes=10_080, resets_at=reset,
                captured_at=NOW + dt.timedelta(minutes=10), used_percent=30.0,
                line_offset=2,
            ),
        ), NOW)


def test_lone_future_dated_weekly_observation_is_missing_not_stale():
    """A capture entirely ahead of ``now`` is not baseline-eligible at all, so it
    never reaches the stale branch."""
    source_module = sys.modules["_cctally_dashboard_sources"]

    with pytest.raises(source_module.CodexCycleUnavailable, match="missing"):
        source_module._resolve_codex_weekly_cycle((
            _quota_observation(
                root="root", window_minutes=10_080,
                resets_at=NOW + dt.timedelta(days=2),
                captured_at=NOW + dt.timedelta(minutes=10),
            ),
        ), NOW)


def test_model_scoped_stale_weekly_identity_stays_excluded():
    """The spark-week exclusion is unchanged: a model-scoped stale weekly
    identity is not a candidate, so the standard stale boundary wins alone."""
    source_module = sys.modules["_cctally_dashboard_sources"]
    standard_reset = NOW + dt.timedelta(days=5)

    cycles = source_module._resolve_codex_weekly_cycle((
        _stale_weekly_observation(
            resets_at=standard_reset, logical_limit_key="standard-limit",
        ),
        _stale_weekly_observation(
            resets_at=NOW + dt.timedelta(days=7),
            logical_limit_key='{"modelPool":"gpt-5.3-codex-spark"}',
        ),
    ), NOW)

    assert len(cycles) == 1
    assert cycles[0].resets_at == standard_reset
    assert cycles[0].evidence_stale is True


def test_stale_ranking_without_account_key_reduces_to_one_bucket():
    """R8 byte-stability: with no real accounts every row falls into the single
    reserved bucket, so two distinct stale boundaries still conflict."""
    source_module = sys.modules["_cctally_dashboard_sources"]

    with pytest.raises(source_module.CodexCycleUnavailable, match="conflicting"):
        source_module._resolve_codex_weekly_cycle((
            _stale_weekly_observation(
                root="root-a", resets_at=NOW + dt.timedelta(days=1),
                logical_limit_key="limit-a",
            ),
            _stale_weekly_observation(
                root="root-b", resets_at=NOW + dt.timedelta(days=2),
                logical_limit_key="limit-b",
            ),
        ), NOW)


def test_stale_ranking_is_scoped_per_account():
    """Two real accounts each with ONE stale boundary each yield a live cycle —
    the same per-account scoping the fresh path already has."""
    source_module = sys.modules["_cctally_dashboard_sources"]
    a_reset = NOW + dt.timedelta(days=1)
    b_reset = NOW + dt.timedelta(days=3)

    cycles = source_module._resolve_codex_weekly_cycle((
        _stale_weekly_observation(
            root="root-a", resets_at=a_reset, logical_limit_key="limit-a",
            account_key="acct-a",
        ),
        _stale_weekly_observation(
            root="root-b", resets_at=b_reset, logical_limit_key="limit-b",
            account_key="acct-b",
        ),
    ), NOW)

    assert [cycle.resets_at for cycle in cycles] == [a_reset, b_reset]
    assert all(cycle.evidence_stale for cycle in cycles)


def test_one_account_fresh_another_stale_keeps_both_cycles():
    source_module = sys.modules["_cctally_dashboard_sources"]
    a_reset = NOW + dt.timedelta(days=1)
    b_reset = NOW + dt.timedelta(days=3)

    cycles = source_module._resolve_codex_weekly_cycle((
        _quota_observation(
            root="root-a", window_minutes=10_080, resets_at=a_reset,
            captured_at=NOW - dt.timedelta(minutes=10),
            logical_limit_key="limit-a", account_key="acct-a",
        ),
        _stale_weekly_observation(
            root="root-b", resets_at=b_reset, logical_limit_key="limit-b",
            account_key="acct-b",
        ),
    ), NOW)

    assert [(cycle.resets_at, cycle.evidence_stale) for cycle in cycles] == [
        (a_reset, False), (b_reset, True),
    ]


# =========================================================================
# #350 — the cycle decision deadline (spec §3.3, §5.3).
#
# Cycle validity is TIME-DEPENDENT even on frozen evidence (spec §2.2), and the
# idle clock's public-history view is lossy (§2.3), so the clock may neither
# re-resolve nor trust an old verdict forever. Build time therefore records the
# earliest future instant at which any time-dependent resolution input flips;
# the tick rebuilds authoritatively when it passes. One rebuild per crossing —
# a handful per weekly cycle — not one per tick.
# =========================================================================


def _next_decision_at(observations, now_utc=NOW):
    """Resolve the cycle list the way the builder does, then compute the deadline."""
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        cycles = source_module._resolve_codex_weekly_cycle(observations, now_utc)
    except source_module.CodexCycleUnavailable:
        cycles = []
    return source_module._codex_next_decision_at(observations, cycles, now_utc)


def test_weekly_stale_after_seconds_is_3600():
    """Guards the `max(900, min(window*60//10, 3600))` arithmetic the whole
    design rests on: an idle weekly observation goes stale after ONE hour."""
    from _lib_quota import stale_after_seconds

    assert stale_after_seconds(10_080) == 3600


def test_deadline_is_the_reset_when_nothing_flips_sooner():
    reset = NOW + dt.timedelta(days=2)

    # Already stale: its fresh->stale flip is in the past, so only expiry remains.
    assert _next_decision_at((_stale_weekly_observation(resets_at=reset),)) == reset


def test_deadline_is_capture_plus_stale_after_when_that_is_sooner():
    reset = NOW + dt.timedelta(days=2)
    captured_at = NOW - dt.timedelta(minutes=10)

    deadline = _next_decision_at((
        _quota_observation(
            root="root", window_minutes=10_080, resets_at=reset,
            captured_at=captured_at,
        ),
    ))

    assert deadline == captured_at + dt.timedelta(seconds=3600)
    assert deadline < reset


def test_deadline_is_a_future_dated_capture_when_that_is_soonest():
    """A future-dated capture becomes baseline-eligible purely because time
    passed, which can switch the selected reset (spec §2.2)."""
    reset = NOW + dt.timedelta(days=2)
    future_capture = NOW + dt.timedelta(minutes=5)

    deadline = _next_decision_at((
        _quota_observation(
            root="root", window_minutes=10_080, resets_at=reset,
            captured_at=NOW - dt.timedelta(minutes=10), used_percent=25.0,
        ),
        _quota_observation(
            root="root", window_minutes=10_080, resets_at=reset,
            captured_at=future_capture, used_percent=30.0, line_offset=2,
        ),
    ))

    assert deadline == future_capture


def test_deadline_is_the_min_across_all_three_input_kinds():
    a_reset = NOW + dt.timedelta(days=3)
    b_reset = NOW + dt.timedelta(minutes=20)
    future_capture = NOW + dt.timedelta(minutes=45)

    deadline = _next_decision_at((
        # (2) fresh -> stale at NOW+30m; (1) expiry at NOW+3d
        _quota_observation(
            root="root-a", window_minutes=10_080, resets_at=a_reset,
            captured_at=NOW - dt.timedelta(minutes=30),
            logical_limit_key="limit-a", account_key="acct-a",
        ),
        # (1) expiry at NOW+20m — the soonest of every candidate
        _quota_observation(
            root="root-b", window_minutes=10_080, resets_at=b_reset,
            captured_at=NOW - dt.timedelta(minutes=10),
            logical_limit_key="limit-b", account_key="acct-b",
        ),
        # (3) a future-dated capture at NOW+45m
        _quota_observation(
            root="root-c", window_minutes=10_080, resets_at=NOW + dt.timedelta(days=2),
            captured_at=NOW - dt.timedelta(minutes=10),
            logical_limit_key="limit-c", account_key="acct-c",
        ),
        _quota_observation(
            root="root-c", window_minutes=10_080, resets_at=NOW + dt.timedelta(days=2),
            captured_at=future_capture, used_percent=30.0,
            logical_limit_key="limit-c", account_key="acct-c", line_offset=2,
        ),
    ))

    assert deadline == b_reset
    assert deadline < future_capture


def test_deadline_is_none_when_no_weekly_evidence_can_flip():
    assert _next_decision_at((
        _quota_observation(
            root="root", window_minutes=300, resets_at=NOW + dt.timedelta(hours=4),
        ),
    )) is None


def test_deadline_is_none_for_an_expired_stale_weekly_boundary():
    """Nothing left to flip: the boundary already reset and its evidence is
    already stale, so no future instant changes the resolution."""
    assert _next_decision_at((
        _quota_observation(
            root="root", window_minutes=10_080, resets_at=NOW - dt.timedelta(hours=1),
            captured_at=NOW - dt.timedelta(hours=2),
        ),
    )) is None


def test_deadline_is_never_at_or_before_now():
    deadline = _next_decision_at((
        _stale_weekly_observation(resets_at=NOW + dt.timedelta(days=2)),
    ))

    assert deadline is not None and deadline > NOW


def test_codex_cycle_selects_one_full_identity_for_one_boundary():
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)

    cycle = source_module._resolve_codex_weekly_cycle((
        _quota_observation(
            root="root-a", window_minutes=10_080, resets_at=reset,
            logical_limit_key="limit-a", used_percent=25.0,
        ),
        _quota_observation(
            root="root-b", window_minutes=10_080, resets_at=reset,
            logical_limit_key="limit-b", observed_slot="secondary",
            used_percent=61.0,
        ),
    ), NOW)[0]  # #341: per-account list — single-account scenario -> 1 element

    assert cycle.resets_at == reset
    assert cycle.source_root_keys == ("root-b",)
    assert cycle.used_percent == 61.0
    assert cycle.quota_identity == QuotaWindowIdentity(
        source="codex", source_root_key="root-b",
        logical_limit_key="limit-b", observed_slot="secondary",
        window_minutes=10_080,
    )


@pytest.mark.parametrize(
    "observations, reason",
    (
        ((
            _quota_observation(root="root", window_minutes=300, resets_at=NOW + dt.timedelta(hours=4)),
        ), "missing"),
        ((
            _quota_observation(root="root-a", window_minutes=10_080, resets_at=NOW + dt.timedelta(days=1)),
            _quota_observation(root="root-b", window_minutes=10_080, resets_at=NOW + dt.timedelta(days=2)),
        ), "conflicting"),
        ((
            _quota_observation(root="root", window_minutes=10_080, resets_at=NOW),
        ), "missing"),
    ),
    ids=("five-hour-only", "conflicting-weekly-boundaries", "expired-weekly-boundary"),
)
def test_codex_cycle_rejects_missing_conflicting_or_expired_weekly_boundaries(
    observations: tuple[QuotaObservation, ...], reason: str,
):
    source_module = sys.modules["_cctally_dashboard_sources"]

    with pytest.raises(source_module.CodexCycleUnavailable, match=reason):
        source_module._resolve_codex_weekly_cycle(observations, NOW)


def _install_active_native_cycle(
    monkeypatch,
    source_module,
    *,
    reset: dt.datetime,
    now_utc: dt.datetime = NOW,
    root: str = "root",
) -> None:
    observations = (
        _quota_observation(
            root=root,
            window_minutes=300,
            resets_at=now_utc + dt.timedelta(hours=4),
            captured_at=now_utc - dt.timedelta(minutes=10),
        ),
        _quota_observation(
            root=root,
            window_minutes=10_080,
            resets_at=reset,
            captured_at=now_utc - dt.timedelta(minutes=10),
        ),
    )
    monkeypatch.setattr(
        source_module,
        "load_codex_quota_observations",
        lambda **_kwargs: observations,
    )


def test_codex_cycle_hero_uses_only_native_boundary_accounting_rows(tmp_path, monkeypatch):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)
    cycle_start = reset - dt.timedelta(days=7)
    try:
        for offset, line_offset in (
            (-dt.timedelta(microseconds=1), 20_001),
            (dt.timedelta(), 20_002),
            (NOW - cycle_start, 20_003),
            (NOW - cycle_start + dt.timedelta(microseconds=1), 20_004),
            (reset - cycle_start, 20_005),
        ):
            _insert_incomplete_accounting_row(
                cache,
                source_path=f"/cached/cycle-boundary-{line_offset}.jsonl",
                line_offset=line_offset,
                session_id=f"cycle-boundary-{line_offset}",
                timestamp=cycle_start + offset,
            )
        cache.commit()
        _install_active_native_cycle(
            monkeypatch, source_module, reset=reset, root=_cache_root_key(cache),
        )

        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="cycle-boundary-v1",
        )

        hero = state.data["hero"]
        assert hero["cycle"] == {
            "window_minutes": 10_080,
            "start_at": cycle_start.isoformat(),
            "resets_at": reset.isoformat(),
        }
        assert hero["total_tokens"] == 3_200
        assert hero["input_tokens"] == 2_400
        assert hero["cached_input_tokens"] == 600
        assert hero["output_tokens"] == 800
        assert hero["reasoning_output_tokens"] == 200
        assert state.capabilities["projects"].status == "supported"
        assert state.capabilities["projects"].semantics == "conversation-metadata-partial"
        assert state.data["periods"]["daily"]["rows"]
    finally:
        cache.close()
        stats.close()


def test_codex_cycle_failure_replaces_a_prior_generation_without_retained_hero_totals(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_active_native_cycle(
            monkeypatch, source_module, reset=NOW + dt.timedelta(days=2), root=_cache_root_key(cache),
        )
        coherent = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="cycle-coherent-v1",
        )
        assert coherent.capabilities["hero"].semantics == "native-reset-cycle"

        monkeypatch.setattr(
            source_module, "load_codex_quota_observations", lambda **_kwargs: (),
        )
        failed = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="cycle-failure-v2",
        )

        assert failed.availability == "partial"
        assert failed.freshness == "fresh"
        assert failed.data_version != coherent.data_version
        assert failed.capabilities["hero"].status == "unavailable"
        assert failed.capabilities["hero"].semantics == "missing-or-conflicting-native-cycle"
        assert failed.warnings[-1].code == "codex_cycle_unavailable"
        assert failed.warnings[-1].domain == "hero"
        assert failed.data["hero"]["cycle"] is None
        assert failed.data["hero"]["cost_usd"] is None
        assert failed.data["hero"]["input_tokens"] is None
        assert failed.data["hero"]["cached_input_tokens"] is None
        assert failed.data["hero"]["output_tokens"] is None
        assert failed.data["hero"]["reasoning_output_tokens"] is None
        assert failed.data["hero"]["total_tokens"] is None
        assert failed.data["periods"]["daily"]["rows"]
    finally:
        cache.close()
        stats.close()


_HERO_WIRE_KEYS = (
    "cost_usd", "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "cycle", "quota", "budget",
    "alerts",
)


def _install_weekly_only_cycle(
    monkeypatch, source_module, *, reset: dt.datetime, root: str,
    captured_at: dt.datetime,
) -> None:
    """Freeze exactly one weekly observation so its freshness is the only variable."""
    monkeypatch.setattr(
        source_module,
        "load_codex_quota_observations",
        lambda **_kwargs: (
            _quota_observation(
                root=root,
                window_minutes=10_080,
                resets_at=reset,
                captured_at=captured_at,
            ),
        ),
    )


def _copy_accounting_row_at(cache: sqlite3.Connection, *, timestamp: dt.datetime,
                            source_path: str) -> None:
    """Clone a COMPLETE cached accounting row to a new instant (metadata stays healthy)."""
    row_id = cache.execute(
        "SELECT id FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()[0]
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
        "input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, "
        "total_tokens, source_root_key, conversation_key) "
        "SELECT ?, ?, ?, session_id, model, input_tokens, cached_input_tokens, "
        "output_tokens, reasoning_output_tokens, total_tokens, source_root_key, "
        "conversation_key FROM codex_session_entries WHERE id=?",
        (source_path, 1, timestamp.isoformat(), row_id),
    )
    cache.commit()


def test_stale_weekly_baseline_retains_the_hero_and_stamps_cycle_freshness(
    tmp_path, monkeypatch,
):
    """#350 build-time contract (spec §3.2, §3.4, §5.5). The stale-but-future
    boundary keeps every backward-looking hero field, provider metadata is
    untouched, and staleness rides the hero-local freshness fields."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)
    try:
        root_key = _cache_root_key(cache)
        _copy_accounting_row_at(
            cache, timestamp=NOW - dt.timedelta(days=1),
            source_path="/private/350-in-cycle.jsonl",
        )
        context = DashboardReadContext(
            cache_conn=cache, stats_conn=stats, range_start=START,
            now_utc=NOW, display_tz_name="UTC",
        )
        _install_weekly_only_cycle(
            monkeypatch, source_module, reset=reset, root=root_key,
            captured_at=NOW - dt.timedelta(hours=2),
        )
        state = source_module.build_codex_source_state(
            context, data_version="stale-cycle-v1",
        )

        # Provider metadata remains coherent; the hero/quota axes move.
        assert state.availability == "ok"
        assert state.freshness == "fresh"
        assert dict(state.domain_freshness) == {
            "hero": "stale",
            "quota": "stale",
            "sessions": "fresh",
        }
        assert state.warnings == ()
        assert state.capabilities["hero"].status == "supported"
        assert state.capabilities["hero"].semantics == "native-reset-cycle"

        hero = state.data["hero"]
        assert hero["cycle_freshness"] == "stale"
        assert hero["cost_usd"] > 0
        assert hero["cycle"] == {
            "window_minutes": 10_080,
            "start_at": (reset - dt.timedelta(days=7)).isoformat(),
            "resets_at": reset.isoformat(),
        }
        for field in (
            "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens",
        ):
            assert hero[field], field
        # The quota domain keeps reporting the real (stale) evidence age; the
        # client's Snapshot chip derives its marker from that, not the envelope.
        assert state.data["quota"]["summary"]["freshness"] == "stale"
        # Undecorated (<=1 real account) stays byte-stable: no per-account wire.
        assert "cycles" not in hero
        assert "accounts" not in state.data
        assert state.data["periods"]["daily"]["rows"]

        # §5.5 non-hero pins. The selected cycle also feeds native weekly
        # periods, the 5h quota-block wire, the cycle index and the per-account
        # wires, so pin the stale build against a FRESH build at the same reset:
        # evidence age must change nothing but the hero's disclosure field.
        _install_weekly_only_cycle(
            monkeypatch, source_module, reset=reset, root=root_key,
            captured_at=NOW - dt.timedelta(minutes=10),
        )
        fresh = source_module.build_codex_source_state(
            context, data_version="stale-cycle-v1",
        )
        assert state.data["periods"]["weekly"] == fresh.data["periods"]["weekly"]
        assert state.data["quota"]["blocks"] == fresh.data["quota"]["blocks"]
        assert state.data["quota"]["cycle_index"] == fresh.data["quota"]["cycle_index"]
        assert state.data["hero"]["cycle"] == fresh.data["hero"]["cycle"]
        assert state.data["hero"]["cost_usd"] == fresh.data["hero"]["cost_usd"]
        assert "cycle_freshness" not in fresh.data["hero"]
        # This fixture has no durable 300-minute projection, so both wires are
        # empty; pin the exact value so a future emission trips this test.
        assert state.data["quota"]["blocks"] == ()
        assert state.data["quota"]["cycle_index"] == ()

        # Quota histories / milestones / alerts are untouched by this change:
        # compare against the pre-#350 behavior (resolver refuses the boundary).
        _install_weekly_only_cycle(
            monkeypatch, source_module, reset=reset, root=root_key,
            captured_at=NOW - dt.timedelta(hours=2),
        )
        monkeypatch.setattr(
            source_module,
            "_resolve_codex_weekly_cycle",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                source_module.CodexCycleUnavailable("stale")
            ),
        )
        blanked = source_module.build_codex_source_state(
            context, data_version="stale-cycle-v1",
        )
        assert state.data["quota"]["histories"] == blanked.data["quota"]["histories"]
        assert state.data["quota"]["milestones"] == blanked.data["quota"]["milestones"]
        assert state.data["alerts"] == blanked.data["alerts"]
        # Non-vacuity: the reference build IS the old full-blank behavior, and
        # the cycle-driven weekly periods genuinely differ without a live cycle.
        assert blanked.data["hero"]["cost_usd"] is None
        assert blanked.availability == "partial"
        assert blanked.freshness == "fresh"
        assert dict(blanked.domain_freshness) == {
            "hero": "stale",
            "quota": "stale",
            "sessions": "fresh",
        }
        assert state.data["periods"]["weekly"] != blanked.data["periods"]["weekly"]
    finally:
        cache.close()
        stats.close()


def test_fresh_live_cycle_omits_cycle_freshness_and_keeps_the_old_hero_wire(
    tmp_path, monkeypatch,
):
    """No golden exercises a live Codex cycle (all nine publish `empty`), so the
    fresh hero-local shape and additive map are asserted directly."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_active_native_cycle(
            monkeypatch, source_module, reset=NOW + dt.timedelta(days=2),
            root=_cache_root_key(cache),
        )
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="fresh-cycle-v1",
        )

        hero = state.data["hero"]
        assert "cycle_freshness" not in hero
        assert tuple(hero) == _HERO_WIRE_KEYS
        assert state.availability == "ok"
        assert state.freshness == "fresh"
        assert state.warnings == ()
        assert state.capabilities["hero"].status == "supported"
        wire = sys.modules["_cctally_dashboard_envelope"]._source_state_to_wire(state)
        assert "cycle_freshness" not in wire["data"]["hero"]
        assert wire["domain_freshness"] == {
            "hero": "fresh",
            "quota": "fresh",
            "sessions": "fresh",
        }
    finally:
        cache.close()
        stats.close()


def test_build_records_the_cycle_decision_deadline_in_private_clock_data(
    tmp_path, monkeypatch,
):
    """The deadline is server-only: it drives the tick, never the public wire."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)
    captured_at = NOW - dt.timedelta(minutes=10)
    try:
        _install_weekly_only_cycle(
            monkeypatch, source_module, reset=reset, root=_cache_root_key(cache),
            captured_at=captured_at,
        )
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="deadline-v1",
        )

        # A fresh boundary goes stale before it resets, so that is the deadline.
        assert state.clock_data["codex_next_decision_at"] == (
            captured_at + dt.timedelta(seconds=3600)
        )
        assert "codex_budget_cost_events" in state.clock_data
        wire = sys.modules["_cctally_dashboard_envelope"]._source_state_to_wire(state)
        assert "clock_data" not in wire
        assert "codex_next_decision_at" not in repr(wire)
    finally:
        cache.close()
        stats.close()


def test_source_wire_legacy_state_falls_back_to_provider_freshness():
    legacy = SimpleNamespace(
        availability="partial",
        freshness="stale",
        warnings=(),
        data_version="legacy-v1",
        last_success_at=None,
        capabilities={},
        data={"sessions": {"rows": ()}},
    )

    wire = sys.modules["_cctally_dashboard_envelope"]._source_state_to_wire(legacy)

    assert wire["domain_freshness"] == {
        "hero": "stale",
        "quota": "stale",
        "sessions": "stale",
    }


def _codex_state_with_in_cycle_spend(
    cache, stats, source_module, monkeypatch, *, reset: dt.datetime,
    captured_at: dt.datetime, data_version: str,
):
    """Build a Codex source state that has real in-cycle spend to retain."""
    _copy_accounting_row_at(
        cache, timestamp=NOW - dt.timedelta(days=1),
        source_path=f"/private/350-{data_version}.jsonl",
    )
    _install_weekly_only_cycle(
        monkeypatch, source_module, reset=reset, root=_cache_root_key(cache),
        captured_at=captured_at,
    )
    return source_module.build_codex_source_state(
        DashboardReadContext(
            cache_conn=cache, stats_conn=stats, range_start=START,
            now_utc=NOW, display_tz_name="UTC",
        ),
        data_version=data_version,
    )


def test_idle_clock_retains_actuals_for_a_stale_but_future_cycle(
    tmp_path, monkeypatch,
):
    """#350 (spec §3.1, §3.3, §5.5). The idle clock no longer re-derives cycle
    validity — its public-history view is lossy, so it CANNOT resolve correctly
    (§2.3). It keeps only a cheap expiry guard, so a stale-but-future cycle
    retains every backward-looking hero field and touches no provider metadata."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)
    try:
        state = _codex_state_with_in_cycle_spend(
            cache, stats, source_module, monkeypatch, reset=reset,
            captured_at=NOW - dt.timedelta(hours=2), data_version="clock-stale-v1",
        )
        assert state.capabilities["hero"].status == "supported"
        assert state.data["hero"]["cycle_freshness"] == "stale"
        expected_cost = state.data["hero"]["cost_usd"]
        assert expected_cost > 0
        before_rows = cache.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0]

        monkeypatch.setattr(
            source_module,
            "load_codex_quota_observations",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("idle clock must not read cache")),
        )
        refreshed = source_module.refresh_codex_source_clock(
            state, now_utc=NOW + dt.timedelta(hours=4),
        )

        assert cache.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == before_rows
        hero = refreshed.data["hero"]
        assert hero["cost_usd"] == pytest.approx(expected_cost)
        assert hero["cycle"] == state.data["hero"]["cycle"]
        assert hero["cycle_freshness"] == "stale"
        for field in (
            "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens",
        ):
            assert hero[field] == state.data["hero"][field], field
        assert refreshed.capabilities["hero"].status == "supported"
        # Envelope metadata untouched on the retain path (§3.4, §5.5).
        assert refreshed.availability == state.availability
        assert refreshed.freshness == state.freshness
        assert refreshed.warnings == state.warnings
        assert not any(
            warning.code == "codex_cycle_unavailable" for warning in refreshed.warnings
        )
        # Forward-looking projections still pause: the forecast stamps its OWN
        # status, which is what blanks `Forecast @ reset` on the client.
        weekly_row = next(
            row for row in refreshed.data["quota"]["histories"]
            if row["window_minutes"] == 10_080
        )
        assert weekly_row["freshness"] == "stale"
        assert weekly_row["forecast"]["status"] == "stale"
    finally:
        cache.close()
        stats.close()


def test_idle_clock_retains_a_cycle_that_was_fresh_at_build(tmp_path, monkeypatch):
    """A cycle built FRESH that goes stale while idle keeps its actuals. The
    clock deliberately does NOT stamp `cycle_freshness` — only the build-time
    resolver does, which is exactly why the §3.3 decision deadline exists."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)
    try:
        state = _codex_state_with_in_cycle_spend(
            cache, stats, source_module, monkeypatch, reset=reset,
            captured_at=NOW - dt.timedelta(minutes=10), data_version="clock-fresh-v1",
        )
        assert "cycle_freshness" not in state.data["hero"]

        refreshed = source_module.refresh_codex_source_clock(
            state, now_utc=NOW + dt.timedelta(hours=2),
        )

        hero = refreshed.data["hero"]
        assert hero["cost_usd"] == pytest.approx(state.data["hero"]["cost_usd"])
        assert hero["cycle"] == state.data["hero"]["cycle"]
        assert refreshed.capabilities["hero"].status == "supported"
        assert "cycle_freshness" not in hero
        assert refreshed.availability == state.availability
        assert refreshed.warnings == state.warnings
    finally:
        cache.close()
        stats.close()


def test_envelope_freshness_is_not_taken_from_the_last_retained_history_row(
    tmp_path, monkeypatch,
):
    """Spec §3.9. The envelope-level `freshness` was assigned `state.freshness`
    and then SHADOWED by the per-row loop variable, so after the loop it held
    the LAST retained history row's freshness. With a single weekly history that
    row is the active weekly one, so an idle stale crossing silently marked the
    whole provider stale and tripped idle eligibility on its own."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        state = _codex_state_with_in_cycle_spend(
            cache, stats, source_module, monkeypatch,
            reset=NOW + dt.timedelta(days=2),
            captured_at=NOW - dt.timedelta(minutes=10), data_version="shadow-v1",
        )
        assert state.freshness == "fresh"
        assert len(state.data["quota"]["histories"]) == 1

        refreshed = source_module.refresh_codex_source_clock(
            state, now_utc=NOW + dt.timedelta(hours=2),
        )

        # Non-vacuity: the trailing (only) retained row really did go stale.
        assert refreshed.data["quota"]["histories"][-1]["freshness"] == "stale"
        assert refreshed.freshness == "fresh"
        assert dict(refreshed.domain_freshness) == {
            "hero": "fresh",
            "quota": "stale",
            "sessions": "fresh",
        }
    finally:
        cache.close()
        stats.close()


def test_claude_idle_clock_advances_only_weekly_domain_freshness(monkeypatch):
    ns = load_script()
    tui = ns["_cctally_tui"]
    state = tui.SourceDashboardState(
        source="claude",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="claude-clock-v1",
        last_success_at=NOW,
        capabilities={
            "hero": tui.CapabilityRecord("supported", "subscription-week"),
            "quota": tui.CapabilityRecord("supported", "subscription-week"),
            "sessions": tui.CapabilityRecord("supported", "legacy-session-rollup"),
        },
        data={"hero": {}, "quota": {}, "sessions": {"rows": ()}},
        domain_freshness={"hero": "fresh", "quota": "fresh", "sessions": "fresh"},
    )
    current_week = SimpleNamespace(latest_snapshot_at=NOW)
    monkeypatch.setattr(tui, "_get_oauth_usage_config", lambda _config: {})
    monkeypatch.setattr(
        tui,
        "_freshness_label",
        lambda age, _config: "stale" if age > 3600 else "fresh",
    )

    same = tui._refresh_claude_source_clock(
        state, current_week=current_week, now_utc=NOW, raw_config={},
    )
    stale = tui._refresh_claude_source_clock(
        state,
        current_week=current_week,
        now_utc=NOW + dt.timedelta(hours=2),
        raw_config={},
    )

    assert same is state
    assert stale.freshness == "fresh"
    assert dict(stale.domain_freshness) == {
        "hero": "stale",
        "quota": "stale",
        "sessions": "fresh",
    }


def test_idle_clock_expiry_degrade_is_byte_identical_to_the_build_degrade(
    tmp_path, monkeypatch,
):
    """The retained expiry guard must reproduce today's degrade exactly (§3.3)."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        state = _codex_state_with_in_cycle_spend(
            cache, stats, source_module, monkeypatch,
            reset=NOW + dt.timedelta(minutes=10),
            captured_at=NOW - dt.timedelta(minutes=10), data_version="expiry-v1",
        )
        assert state.capabilities["hero"].status == "supported"

        refreshed = source_module.refresh_codex_source_clock(
            state, now_utc=NOW + dt.timedelta(minutes=20),
        )

        hero = refreshed.data["hero"]
        for field in (
            "cost_usd", "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens", "cycle",
        ):
            assert hero[field] is None, field
        assert refreshed.capabilities["hero"].status == "unavailable"
        assert refreshed.capabilities["hero"].semantics == "missing-or-conflicting-native-cycle"
        assert refreshed.availability == "partial"
        assert [warning.code for warning in refreshed.warnings].count(
            "codex_cycle_unavailable"
        ) == 1
        expiry_warning = next(
            warning for warning in refreshed.warnings
            if warning.code == "codex_cycle_unavailable"
        )
        assert expiry_warning.domain == "hero"
        assert expiry_warning.message == "Codex native reset cycle is unavailable."
        # Clocking again must not duplicate the warning.
        twice = source_module.refresh_codex_source_clock(
            refreshed, now_utc=NOW + dt.timedelta(minutes=25),
        )
        assert [warning.code for warning in twice.warnings].count(
            "codex_cycle_unavailable"
        ) == 1
    finally:
        cache.close()
        stats.close()


def test_same_instant_codex_clocking_is_idempotent(tmp_path, monkeypatch):
    """No 'does not republish' assertion: `remaining_seconds` advances `data`
    every tick, so a NEW object is expected whenever time moves (spec §2.5).
    What must hold is that clocking twice at an IDENTICAL instant agrees."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    idle_now = NOW + dt.timedelta(hours=4)
    try:
        state = _codex_state_with_in_cycle_spend(
            cache, stats, source_module, monkeypatch,
            reset=NOW + dt.timedelta(days=2),
            captured_at=NOW - dt.timedelta(hours=2), data_version="idempotent-v1",
        )

        once = source_module.refresh_codex_source_clock(state, now_utc=idle_now)
        twice = source_module.refresh_codex_source_clock(once, now_utc=idle_now)

        assert twice.data == once.data
        assert twice.availability == once.availability
        assert twice.freshness == once.freshness
        assert twice.warnings == once.warnings
    finally:
        cache.close()
        stats.close()


def test_idle_clock_crossing_a_native_reset_withdraws_the_hero_without_a_cache_read(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(minutes=10)
    try:
        root_key = str(cache.execute(
            "SELECT source_root_key FROM codex_session_entries ORDER BY id LIMIT 1"
        ).fetchone()[0])
        _install_active_native_cycle(monkeypatch, source_module, reset=reset, root=root_key)
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="clock-reset-v1",
        )
        assert state.capabilities["hero"].status == "supported"

        refreshed = source_module.refresh_codex_source_clock(
            state, now_utc=NOW + dt.timedelta(minutes=20),
        )

        assert refreshed.availability == "partial"
        assert refreshed.freshness == "fresh"
        assert refreshed.data["quota"]["summary"]["active_window_count"] == 1
        assert refreshed.data["quota"]["summary"]["freshness"] == "fresh"
        assert refreshed.capabilities["hero"].status == "unavailable"
        assert refreshed.data["hero"]["cycle"] is None
        assert refreshed.data["hero"]["total_tokens"] is None
    finally:
        cache.close()
        stats.close()


def test_cycle_accounting_excludes_a_non_supporting_root(tmp_path, monkeypatch):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)
    cycle_start = reset - dt.timedelta(days=7)
    try:
        root_a = str(cache.execute(
            "SELECT source_root_key FROM codex_session_entries ORDER BY id LIMIT 1"
        ).fetchone()[0])
        cache.execute(
            "UPDATE codex_session_entries SET timestamp_utc=?",
            ((cycle_start + dt.timedelta(hours=1)).isoformat(),),
        )
        _insert_incomplete_accounting_row(
            cache,
            source_path="/cached/root-b/outside-cycle-proof.jsonl",
            line_offset=31_001,
            session_id="root-b-outside-cycle-proof",
            source_root_key="root-b-without-boundary",
            timestamp=cycle_start + dt.timedelta(hours=2),
        )
        cache.commit()
        expected = cache.execute(
            "SELECT SUM(total_tokens) FROM codex_session_entries WHERE source_root_key=?",
            (root_a,),
        ).fetchone()[0]
        _install_active_native_cycle(monkeypatch, source_module, reset=reset, root=root_a)

        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="root-qualified-v1",
        )

        assert state.data["hero"]["total_tokens"] == expected
        assert "root-b-without-boundary" not in repr(state.data)
    finally:
        cache.close()
        stats.close()


def test_cycle_accounting_uses_only_the_selected_full_identity_root(tmp_path, monkeypatch):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)
    cycle_start = reset - dt.timedelta(days=7)
    try:
        root_a = str(cache.execute(
            "SELECT source_root_key FROM codex_session_entries ORDER BY id LIMIT 1"
        ).fetchone()[0])
        root_b = "root-b-supporting-boundary"
        root_c = "root-c-without-boundary"
        cache.execute(
            "UPDATE codex_session_entries SET timestamp_utc=?",
            ((cycle_start + dt.timedelta(hours=1)).isoformat(),),
        )
        for root_key, line_offset in ((root_b, 31_002), (root_c, 31_003)):
            _insert_incomplete_accounting_row(
                cache,
                source_path=f"/cached/{root_key}.jsonl",
                line_offset=line_offset,
                session_id=root_key,
                source_root_key=root_key,
                timestamp=cycle_start + dt.timedelta(hours=2),
            )
        cache.commit()
        expected = cache.execute(
            "SELECT SUM(total_tokens) FROM codex_session_entries WHERE source_root_key=?",
            (root_b,),
        ).fetchone()[0]
        monkeypatch.setattr(
            source_module,
            "load_codex_quota_observations",
            lambda **_kwargs: (
                _quota_observation(
                    root=root_a, window_minutes=10_080, resets_at=reset,
                    used_percent=25.0,
                ),
                _quota_observation(
                    root=root_b, window_minutes=10_080, resets_at=reset,
                    used_percent=61.0,
                ),
            ),
        )

        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="duplicate-root-cycle-v1",
        )

        assert state.data["hero"]["total_tokens"] == expected
        assert root_a not in repr(state.data)
        assert root_b not in repr(state.data)
        assert root_c not in repr(state.data)
    finally:
        cache.close()
        stats.close()


def test_missing_cycle_fails_closed_for_accounting_outside_the_visible_range(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        cache.execute(
            "UPDATE codex_session_entries SET timestamp_utc=?",
            ((NOW - dt.timedelta(days=60)).isoformat(),),
        )
        cache.commit()

        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats,
                range_start=NOW - dt.timedelta(days=1), now_utc=NOW,
                display_tz_name="UTC",
            ),
            data_version="outside-range-cycle-v1",
        )

        assert state.availability == "partial"
        assert state.capabilities["hero"].status == "unavailable"
        assert state.data["hero"]["cycle"] is None
        assert state.data["hero"]["total_tokens"] is None
    finally:
        cache.close()
        stats.close()


def test_codex_cycle_only_metadata_is_excluded_from_project_health_but_not_hero(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)
    cycle_start = reset - dt.timedelta(days=7)
    try:
        _insert_incomplete_accounting_row(
            cache,
            source_path="/cached/cycle-only-missing-project.jsonl",
            line_offset=20_006,
            session_id="cycle-only-missing-project",
            timestamp=cycle_start,
        )
        cache.commit()
        _install_active_native_cycle(
            monkeypatch, source_module, reset=reset, root=_cache_root_key(cache),
        )

        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=NOW - dt.timedelta(days=1),
                now_utc=NOW,
                display_tz_name="UTC",
            ),
            data_version="cycle-only-metadata-v1",
        )

        assert state.capabilities["projects"].status == "supported"
        assert state.data["hero"]["total_tokens"] == 1_600
    finally:
        cache.close()
        stats.close()


def _seeded_context(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    root = tmp_path / "provider"
    rollout = root / "sessions" / "2026" / "07" / "16" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(root))
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    ns["sync_codex_cache"](cache)
    conversations = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conversations)
    finally:
        conversations.close()
    return ns, cache, stats


def test_codex_session_name_stays_private_normalized_and_out_of_source_rows(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    provider_root = tmp_path / "provider"
    raw_title = "\x1b[31m  Fix   dashboard " + ("x" * 130) + "\x1b[0m"
    expected_title = "Fix dashboard " + ("x" * 106) + "…"
    native_thread_id = cache.execute(
        "SELECT native_thread_id FROM codex_conversation_threads LIMIT 1"
    ).fetchone()[0]
    cache.execute(
        "UPDATE codex_conversation_rollups SET title=?",
        ("This is the beginning of the user's prompt, not the task name",),
    )
    cache.commit()
    state_db = sqlite3.connect(provider_root / "state_5.sqlite")
    try:
        state_db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
        state_db.execute(
            "INSERT INTO threads(id, title) VALUES (?, ?)",
            (native_thread_id, raw_title),
        )
        state_db.commit()
    finally:
        state_db.close()

    try:
        metadata = source_module._codex_conversation_metadata(cache)
        assert expected_title in {
            row["title"] for row in metadata.values()
        }, metadata
        _install_active_native_cycle(
            monkeypatch, source_module,
            reset=NOW + dt.timedelta(days=2),
            root=_cache_root_key(cache),
        )
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="short-name-v1",
        )

        rows = state.data["sessions"]["rows"]
        assert rows
        assert all("label" not in row for row in rows)
        assert expected_title in set(
            getattr(state, "private_session_labels", {}).values()
        )
        assert "beginning of the user's prompt" not in repr(state.data["sessions"])
    finally:
        cache.close()
        stats.close()


def test_codex_session_start_comes_from_accounting_not_rebuild_observation(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root_key = _cache_root_key(cache)
    try:
        source_path, expected_started_at = cache.execute(
            "SELECT source_path, MIN(timestamp_utc) "
            "FROM codex_session_entries WHERE source_root_key=?",
            (root_key,),
        ).fetchone()
        rebuild_at = NOW.isoformat()
        cache.execute(
            "UPDATE codex_conversation_threads "
            "SET first_seen_utc=?, last_seen_utc=? "
            "WHERE source_root_key=? AND source_path=?",
            (rebuild_at, rebuild_at, root_key, source_path),
        )

        mcp_path = "/cached/mcp-rollout.jsonl"
        mcp_started_at = (NOW - dt.timedelta(hours=2)).isoformat()
        cache.execute(
            "INSERT INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            "last_session_id, source_root_key, last_native_thread_id) "
            "VALUES (?, 1, 1, 1, ?, ?, ?, ?)",
            (
                mcp_path, rebuild_at, "mcp-accounting-session", root_key,
                "mcp-native-thread",
            ),
        )
        _insert_incomplete_accounting_row(
            cache,
            source_path=mcp_path,
            line_offset=11_000,
            session_id="mcp-accounting-session",
            conversation_key=None,
            source_root_key=root_key,
            timestamp=NOW - dt.timedelta(hours=2),
        )
        cache.commit()

        metadata = source_module._codex_conversation_metadata(cache)

        assert metadata[(root_key, source_path)]["started_at"] == expected_started_at
        assert metadata[(root_key, mcp_path)]["started_at"] == mcp_started_at
    finally:
        cache.close()
        stats.close()


def test_codex_subagent_accounting_inherits_root_task_and_project_metadata(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    provider_root = tmp_path / "provider"
    root_row = cache.execute(
        "SELECT source_root_key, native_thread_id, cwd, conversation_key "
        "FROM codex_conversation_threads LIMIT 1"
    ).fetchone()
    assert root_row is not None
    root_key, native_thread_id, _cwd, _conversation_key = root_row
    child_path = "/cached/subagent-rollout.jsonl"
    child_session_id = "native-subagent-session"
    child_conversation_key = "v1.child-without-own-thread-row"
    cache.execute(
        "INSERT INTO codex_session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        "last_session_id, source_root_key, last_native_thread_id, "
        "last_root_thread_id, last_conversation_key) "
        "VALUES (?, 1, 1, 1, ?, ?, ?, ?, ?, ?)",
        (
            child_path, NOW.isoformat(), child_session_id, root_key,
            native_thread_id, native_thread_id, child_conversation_key,
        ),
    )
    _insert_incomplete_accounting_row(
        cache,
        source_path=child_path,
        line_offset=11_001,
        session_id=child_session_id,
        conversation_key=child_conversation_key,
        source_root_key=str(root_key),
    )
    cache.commit()
    state_db = sqlite3.connect(provider_root / "state_5.sqlite")
    try:
        state_db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
        state_db.execute(
            "INSERT INTO threads(id, title) VALUES (?, ?)",
            (native_thread_id, "Inherited root task name"),
        )
        state_db.commit()
    finally:
        state_db.close()

    try:
        health = sys.modules["_cctally_source_analytics"].load_codex_project_metadata_health(
            cache_conn=cache, start=START, end=NOW + dt.timedelta(microseconds=1),
        )
        assert health.incomplete_rows == 0
        metadata = source_module._codex_conversation_metadata(cache)
        inherited = metadata[(str(root_key), child_path)]
        assert inherited["title"] == "Inherited root task name"
        assert inherited["project_label"] == "project-red"
    finally:
        cache.close()
        stats.close()


def _cache_root_key(cache: sqlite3.Connection) -> str:
    row = cache.execute(
        "SELECT source_root_key FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_codex_nonconversation_panels_never_open_conversation_store(
    tmp_path, monkeypatch,
):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    _insert_incomplete_accounting_row(
        cache,
        source_path="/cached/active-cycle.jsonl",
        line_offset=12_001,
        session_id="active-cycle",
        timestamp=NOW - dt.timedelta(hours=1),
    )
    cache.commit()
    conversation_path = ns["_cctally_core"].CONVERSATIONS_DB_PATH
    for suffix in ("", "-wal", "-shm"):
        pathlib.Path(str(conversation_path) + suffix).unlink(missing_ok=True)
    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        if "conversations.db" in str(database):
            raise AssertionError("non-conversation source model opened transcript store")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    try:
        _install_active_native_cycle(
            monkeypatch,
            source_module,
            reset=NOW + dt.timedelta(days=2),
            root=_cache_root_key(cache),
        )
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=START,
                now_utc=NOW,
                display_tz_name="UTC",
            ),
            data_version="conversation-store-missing-v1",
        )
        assert state.data["hero"]["total_tokens"] > 0
        assert state.data["periods"]["weekly"]["rows"]
        assert state.data["projects"]["rows"]
        assert state.data["sessions"]["rows"]
    finally:
        cache.close()
        stats.close()


def _insert_incomplete_accounting_row(
    cache: sqlite3.Connection,
    *,
    source_path: str,
    line_offset: int,
    session_id: str,
    conversation_key: str | None = None,
    source_root_key: str | None = None,
    timestamp: dt.datetime | None = None,
) -> tuple[str, str]:
    """Clone known-good accounting while withholding only project metadata."""
    row = cache.execute(
        "SELECT source_root_key, model, input_tokens, cached_input_tokens, "
        "output_tokens, reasoning_output_tokens, total_tokens "
        "FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    root_key = source_root_key or str(row[0])
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
        "input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, "
        "total_tokens, source_root_key, conversation_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_path, line_offset,
            (timestamp or (NOW - dt.timedelta(hours=1))).isoformat(),
            session_id, row[1], row[2], row[3], row[4], row[5], row[6],
            root_key, conversation_key,
        ),
    )
    return root_key, str(row[1])


def _mixed_metadata_context(tmp_path, monkeypatch):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    _insert_incomplete_accounting_row(
        cache,
        source_path="/cached/root-a/missing-project-metadata.jsonl",
        line_offset=10_001,
        session_id="native-missing-project-metadata",
    )
    cache.commit()
    return ns, cache, stats, DashboardReadContext(
        cache_conn=cache,
        stats_conn=stats,
        range_start=START,
        now_utc=NOW,
        display_tz_name="UTC",
    )


def test_mixed_codex_metadata_preserves_accounting_and_keeps_qualified_projects(
    tmp_path, monkeypatch,
):
    ns, cache, stats, context = _mixed_metadata_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_active_native_cycle(
            monkeypatch, source_module, reset=NOW + dt.timedelta(days=2), root=_cache_root_key(cache),
        )
        state = source_module.build_codex_source_state(context, data_version="mixed-v1")

        assert state.availability == "partial"
        assert state.freshness == "fresh"
        assert state.warnings[0].code == "codex_metadata_incomplete"
        assert state.warnings[0].domain == "projects"
        assert state.capabilities["projects"].status == "supported"
        assert state.capabilities["projects"].semantics == "conversation-metadata-partial"
        assert state.data["hero"]["total_tokens"] > 0
        assert state.data["sessions"]["total_sessions"] == 2
        assert [row["label"] for row in state.data["projects"]["rows"]] == ["project-red"]
        assert any(row["project"] == "project-red" for row in state.data["sessions"]["rows"])
        assert all("label" not in row for row in state.data["sessions"]["rows"])
        assert ns["iter_codex_entries"](cache, START, NOW)
    finally:
        cache.close()
        stats.close()


def test_partial_projects_disambiguate_duplicate_labels_without_identity_leaks():
    source_module = sys.modules["_cctally_dashboard_sources"]
    entries = (
        SimpleNamespace(
            timestamp=NOW - dt.timedelta(hours=2), source_root_key="root-secret-b",
            source_path="/Users/secret/work/repo/rollout-b.jsonl", session_id="native-b",
            model="gpt-5", cost_usd=2.0, input_tokens=20, cached_input_tokens=5,
            output_tokens=8, reasoning_output_tokens=2, total_tokens=28,
        ),
        SimpleNamespace(
            timestamp=NOW - dt.timedelta(hours=1), source_root_key="root-secret-a",
            source_path="/Users/secret/personal/repo/rollout-a.jsonl", session_id="native-a",
            model="gpt-5", cost_usd=3.0, input_tokens=30, cached_input_tokens=7,
            output_tokens=12, reasoning_output_tokens=3, total_tokens=42,
        ),
    )
    metadata = {
        ("root-secret-a", "/Users/secret/personal/repo/rollout-a.jsonl"): {
            "project_key": "project:" + "a" * 24, "project_label": "repo", "title": "A",
        },
        ("root-secret-b", "/Users/secret/work/repo/rollout-b.jsonl"): {
            "project_key": "project:" + "b" * 24, "project_label": "repo", "title": "B",
        },
    }

    first = source_module._partial_projects_wire(entries, metadata)
    second = source_module._partial_projects_wire(reversed(entries), metadata)

    assert {row["label"] for row in first["rows"]} == {"repo (1)", "repo (2)"}
    assert [(row["key"], row["label"]) for row in first["rows"]] == [
        (row["key"], row["label"]) for row in second["rows"]
    ]
    assert len({row["key"] for row in first["rows"]}) == 2
    public = repr(first)
    assert {
        session["label"]
        for row in first["rows"]
        for session in row["sessions"]
    } == {"Session"}
    assert "'A'" not in public and "'B'" not in public
    for secret in (
        "root-secret-a", "root-secret-b", "/Users/secret", "rollout-a.jsonl",
        "rollout-b.jsonl", "project:" + "a" * 24, "project:" + "b" * 24,
    ):
        assert secret not in public


@pytest.mark.parametrize("metadata_kind", ("all-unqualified", "missing-join", "wrong-root-join"))
def test_incomplete_codex_metadata_keeps_nonproject_dashboard_data(
    tmp_path, monkeypatch, metadata_kind,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        if metadata_kind == "all-unqualified":
            cache.execute("UPDATE codex_session_entries SET conversation_key=NULL")
        else:
            key = f"{metadata_kind}-key"
            root_key, _model = _insert_incomplete_accounting_row(
                cache,
                source_path=f"/cached/{metadata_kind}.jsonl",
                line_offset=10_002,
                session_id=f"native-{metadata_kind}",
                conversation_key=key,
            )
            if metadata_kind == "wrong-root-join":
                other_root = root_key + "-other"
                cache.execute(
                    "INSERT INTO codex_conversation_threads "
                    "(conversation_key, source_root_key, native_thread_id, root_thread_id, source_path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (key, other_root, "native", "root", "/cached/other.jsonl"),
                )
        cache.commit()
        _install_active_native_cycle(
            monkeypatch, source_module, reset=NOW + dt.timedelta(days=2), root=_cache_root_key(cache),
        )
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version=f"{metadata_kind}-v1",
        )

        assert state.availability == "partial"
        assert state.freshness == "fresh"
        assert state.data["hero"]["cycle"]["window_minutes"] == 10_080
        assert state.data["hero"]["total_tokens"] >= 0
        assert [row["label"] for row in state.data["projects"]["rows"]] == ["project-red"]
    finally:
        cache.close()
        stats.close()


def test_budget_only_incomplete_metadata_makes_the_visible_source_partial(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    now = dt.datetime(2026, 7, 31, 12, tzinfo=UTC)
    visible_start = now - dt.timedelta(days=2)
    try:
        _insert_incomplete_accounting_row(
            cache,
            source_path="/cached/budget-only-incomplete.jsonl",
            line_offset=10_003,
            session_id="native-budget-only-incomplete",
            timestamp=now - dt.timedelta(days=20),
        )
        cache.commit()
        state = build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=visible_start,
                now_utc=now, display_tz_name="UTC", codex_budget={
                    "amount_usd": 10.0, "period": "calendar-month", "alert_thresholds": (80, 100),
                },
            ),
            data_version="budget-only-incomplete-v1",
        )

        assert state.availability == "partial"
        assert state.data["projects"]["rows"] == ()
    finally:
        cache.close()
        stats.close()


@pytest.mark.parametrize("name", ("_codex_session_roots", "_codex_home_roots"))
def test_rooted_fallback_sessions_never_discover_filesystem(tmp_path, monkeypatch, name):
    ns, cache, stats, context = _mixed_metadata_context(tmp_path, monkeypatch)
    try:
        cache.execute(
            "UPDATE codex_session_entries SET session_id=? WHERE session_id=?",
            ("same-native-session", "native-missing-project-metadata"),
        )
        _insert_incomplete_accounting_row(
            cache,
            source_path="/cached/root-a/second-session-file.jsonl",
            line_offset=10_004,
            session_id="same-native-session",
        )
        cache.commit()
        monkeypatch.setitem(
            ns, name, lambda: (_ for _ in ()).throw(AssertionError(name)),
        )
        monkeypatch.setattr(
            pathlib.Path, "is_dir", lambda *_: (_ for _ in ()).throw(AssertionError("is_dir")),
        )

        state = build_codex_source_state(context, data_version=f"rooted-{name}")

        assert state.data["sessions"]["total_sessions"] == 3
        assert len({row["key"] for row in state.data["sessions"]["rows"]}) == 3
    finally:
        cache.close()
        stats.close()


def test_rooted_fallback_keeps_same_file_and_native_id_separate_across_roots(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    try:
        original = cache.execute(
            "SELECT id, source_root_key FROM codex_session_entries ORDER BY id LIMIT 1"
        ).fetchone()
        assert original is not None
        original_id, first_root = original
        cache.execute("DELETE FROM codex_session_entries WHERE id != ?", (original_id,))
        cache.execute(
            "UPDATE codex_session_entries SET source_path=?, session_id=?, "
            "conversation_key=NULL, input_tokens=?, cached_input_tokens=?, "
            "output_tokens=?, reasoning_output_tokens=?, total_tokens=? WHERE id=?",
            (
                "/cached/shared/rollout.jsonl", "same-native-session",
                10, 2, 4, 0, 14, original_id,
            ),
        )
        second_root = f"{first_root}-second"
        _insert_incomplete_accounting_row(
            cache,
            source_path="/cached/shared/rollout.jsonl",
            line_offset=10_005,
            session_id="same-native-session",
            source_root_key=second_root,
        )
        cache.execute(
            "UPDATE codex_session_entries SET input_tokens=?, cached_input_tokens=?, "
            "output_tokens=?, reasoning_output_tokens=?, total_tokens=? "
            "WHERE source_root_key=?",
            (20, 3, 7, 1, 27, second_root),
        )
        cache.commit()

        state = build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="rooted-cross-root-v1",
        )

        rows = state.data["sessions"]["rows"]
        assert state.availability == "partial"
        assert state.data["sessions"]["total_sessions"] == 2
        assert {row["total_tokens"] for row in rows} == {14, 27}
        assert len({row["key"] for row in rows}) == 2
        assert all(row["key"].startswith("session:") for row in rows)
        assert all("/cached/shared/rollout.jsonl" not in row["key"] for row in rows)
        assert all("same-native-session" not in row["key"] for row in rows)
    finally:
        cache.close()
        stats.close()


def test_complete_metadata_defensively_falls_back_once_when_qualified_read_fails(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        monkeypatch.setattr(
            source_module,
            "load_qualified_codex_entries",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sys.modules["_cctally_source_analytics"].QualifiedMetadataUnavailable("race")
            ),
        )
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="defensive-fallback-v1",
        )

        assert state.availability == "partial"
        assert state.freshness == "fresh"
        assert state.warnings[0].code == "codex_metadata_incomplete"
        assert state.warnings[0].message == (
            "Codex project metadata could not be read; "
            "run `cctally cache-sync --source codex --rebuild`."
        )
        assert "0 Codex accounting row(s)" not in state.warnings[0].message
        assert [row["label"] for row in state.data["projects"]["rows"]] == ["project-red"]
    finally:
        cache.close()
        stats.close()


def test_metadata_health_and_accounting_share_one_read_snapshot(tmp_path, monkeypatch):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    try:
        root_key, _model = _insert_incomplete_accounting_row(
            cache,
            source_path="/cached/snapshot-race.jsonl",
            line_offset=10_006,
            session_id="native-snapshot-race",
            conversation_key="snapshot-race-key",
        )
        cache.commit()
        cache.execute("BEGIN")
        before = sys.modules["_cctally_source_analytics"].load_codex_project_metadata_health(
            cache_conn=cache, start=START, end=NOW + dt.timedelta(microseconds=1),
        )
        writer = ns["open_cache_db"]()
        try:
            writer.execute(
                "INSERT INTO codex_conversation_threads "
                "(conversation_key, source_root_key, native_thread_id, root_thread_id, source_path) "
                "VALUES (?, ?, ?, ?, ?)",
                ("snapshot-race-key", root_key, "native", "root", "/cached/snapshot-race.jsonl"),
            )
            writer.commit()
            current = build_codex_source_state(
                DashboardReadContext(
                    cache_conn=cache, stats_conn=stats, range_start=START,
                    now_utc=NOW, display_tz_name="UTC",
                ),
                data_version="snapshot-race-current",
            )
        finally:
            writer.close()
        cache.rollback()
        next_generation = build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="snapshot-race-next",
        )

        assert before.incomplete_rows == 1
        assert current.availability == "partial"
        assert next_generation.capabilities["projects"].status == "supported"
    finally:
        cache.close()
        stats.close()


def test_codex_read_model_reuses_shipped_view_kernels_with_safe_native_vocabulary(
    tmp_path, monkeypatch,
):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        context = DashboardReadContext(
            cache_conn=cache,
            stats_conn=stats,
            range_start=START,
            now_utc=NOW,
            display_tz_name="UTC",
            week_start_idx=0,
            speed="standard",
        )
        _install_active_native_cycle(
            monkeypatch, source_module, reset=NOW + dt.timedelta(days=2), root=_cache_root_key(cache),
        )
        state = source_module.build_codex_source_state(context, data_version="codex-v1")
        entries = ns["iter_codex_entries"](cache, START, NOW)
        expected_daily = ns["build_codex_daily_view"](entries, now_utc=NOW, tz_name="UTC")
        expected_monthly = ns["build_codex_monthly_view"](entries, now_utc=NOW, tz_name="UTC")
        expected_sessions = ns["build_codex_session_view"](entries, now_utc=NOW, tz_name="UTC")

        assert state.source == "codex"
        assert state.availability == "ok"
        assert state.data["hero"]["cycle"]["window_minutes"] == 10_080
        assert state.data["hero"]["cost_usd"] == 0.0
        assert state.data["hero"]["total_tokens"] == 0
        assert state.data["periods"]["daily"]["total_cost_usd"] == expected_daily.total_cost_usd
        assert state.data["periods"]["monthly"]["total_tokens"] == expected_monthly.total_tokens
        assert state.data["periods"]["weekly"]["total_cost_usd"] == 0.0
        assert state.capabilities["weekly"].status == "derived"
        assert state.capabilities["weekly"].semantics == "native-reset-cycles"
        assert state.data["periods"]["daily"]["rows"][0]["model_breakdowns"] == tuple(
            dict(row) for row in expected_daily.rows[0].model_breakdowns
        )
        assert state.data["sessions"]["total_cost_usd"] == expected_sessions.total_cost_usd
        assert state.data["sessions"]["total_tokens"] == expected_sessions.total_tokens
        assert state.capabilities["projects"].status == "supported"
        assert state.data["projects"]["rows"]
        assert all(row["key"].startswith("project:") for row in state.data["projects"]["rows"])
        qualified = sys.modules["_cctally_dashboard_sources"].load_qualified_codex_entries(
            START, NOW, speed="standard", sync=False,
        )
        assert qualified
        assert {
            row["key"] for row in state.data["projects"]["rows"]
        }.isdisjoint({entry.project_key for entry in qualified})
        assert {"summary", "blocks", "histories", "milestones"} <= set(state.data["quota"])
        assert {"milestones", "projected"} <= set(state.data["budget"])
        assert state.data["alerts"]["rows"] == ()
        assert {"quota", "budget", "alerts"} <= set(state.data["hero"])
        assert state.capabilities["forensics"].semantics == "inclusive-input-token-reuse"
        assert "cache_hit_pct" not in state.data["hero"]
        assert all(row["key"].startswith("session:") for row in state.data["sessions"]["rows"])
        assert all(row["source"] == "codex" for row in state.data["sessions"]["rows"])
        assert all(row["source"] == "codex" for row in state.data["projects"]["rows"])
        assert all(row["source"] == "codex" for row in state.data["quota"]["blocks"])
        assert all(row["source"] == "codex" for row in state.data["quota"]["histories"])
        assert all(row["source"] == "codex" for row in state.data["quota"]["milestones"])

        raw_root = cache.execute("SELECT source_root_key FROM codex_source_roots").fetchone()[0]
        raw_session = cache.execute("SELECT session_id FROM codex_session_entries").fetchone()[0]
        public = repr(state.data)
        assert raw_root not in public
        assert raw_session not in public
    finally:
        cache.close()
        stats.close()


def test_dashboard_source_semantics_use_the_canonical_fast_tier_and_week_start(
    tmp_path, monkeypatch,
):
    ns, _cache, _stats = _seeded_context(tmp_path, monkeypatch)
    try:
        (tmp_path / "provider" / "config.toml").write_text(
            'service_tier = "fast"\n', encoding="utf-8",
        )

        semantics = resolve_dashboard_source_semantics(
            {"collector": {"week_start": "sunday"}},
            display_tz_name="UTC",
        )

        assert semantics.speed == ns["_resolve_codex_speed"]("auto") == "fast"
        assert semantics.week_start_idx == ns["WEEKDAY_MAP"]["sunday"]
        assert semantics.week_start_name == "sunday"
    finally:
        _cache.close()
        _stats.close()


def test_codex_hero_budget_uses_configured_calendar_status_and_pace_kernels(
    tmp_path, monkeypatch,
):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    config = {
        "collector": {"week_start": "sunday"},
        "budget": {
            "codex": {
                "amount_usd": 10.0,
                "period": "calendar-month",
                "alert_thresholds": [80, 100],
            },
        },
    }
    try:
        budget_cfg = ns["_get_budget_config"](config)["codex"]
        context = DashboardReadContext(
            cache_conn=cache,
            stats_conn=stats,
            range_start=START,
            now_utc=NOW,
            display_tz_name="UTC",
            week_start_idx=6,
            week_start_name="sunday",
            codex_budget=budget_cfg,
        )
        state = build_codex_source_state(context, data_version="budget-v1")
        expected_inputs = ns["_build_vendor_budget_inputs"](
            vendor="codex",
            period="calendar-month",
            target_usd=10.0,
            alert_thresholds=(80, 100),
            now_utc=NOW,
            config=config,
            tz=dt.timezone.utc,
            skip_sync=True,
        )
        expected = ns["compute_budget_status"](expected_inputs)

        budget = state.data["hero"]["budget"]
        assert budget["period"] == "calendar-month"
        assert budget["spent_usd"] == pytest.approx(expected.spent_usd)
        assert budget["verdict"] == expected.verdict
        assert budget["pace"]["daily_usd"] == pytest.approx(expected.daily_pace_usd)
        refreshed = refresh_codex_source_clock(
            state, now_utc=NOW + dt.timedelta(hours=6),
        )
        assert refreshed.data_version == state.data_version
        assert refreshed.last_success_at == state.last_success_at
        assert refreshed.data["hero"]["budget"]["spent_usd"] == budget["spent_usd"]
        assert refreshed.data["hero"]["budget"]["pace"] != budget["pace"]
    finally:
        cache.close()
        stats.close()


def test_idle_budget_refresh_recomputes_trailing_24_hour_pace_from_frozen_cost_events(
    tmp_path, monkeypatch,
):
    """An entry that ages out on idle must match a fresh canonical build."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    config = {
        "collector": {"week_start": "sunday"},
        "budget": {
            "codex": {
                "amount_usd": 10.0,
                "period": "calendar-month",
                "alert_thresholds": [80, 100],
            },
        },
    }
    initial_now = NOW
    idle_now = NOW + dt.timedelta(hours=2)
    try:
        # The first row is in the original 24h numerator but is outside it at
        # the idle instant.  A second row remains, so the expected value is
        # non-zero and this cannot pass by merely zeroing the pace.
        row_id = cache.execute(
            "SELECT id FROM codex_session_entries ORDER BY id LIMIT 1"
        ).fetchone()[0]
        cache.execute(
            "UPDATE codex_session_entries SET timestamp_utc=? WHERE id=?",
            ((initial_now - dt.timedelta(hours=23)).isoformat(), row_id),
        )
        cache.execute(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            "input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, "
            "total_tokens, source_root_key, conversation_key) "
            "SELECT ?, ?, ?, session_id, model, input_tokens, cached_input_tokens, "
            "output_tokens, reasoning_output_tokens, total_tokens, source_root_key, "
            "conversation_key FROM codex_session_entries WHERE id=?",
            ("/private/idle-recent.jsonl", 2,
             (initial_now - dt.timedelta(hours=1)).isoformat(), row_id),
        )
        cache.commit()
        budget_cfg = ns["_get_budget_config"](config)["codex"]

        def build(now):
            return build_codex_source_state(
                DashboardReadContext(
                    cache_conn=cache,
                    stats_conn=stats,
                    range_start=START,
                    now_utc=now,
                    display_tz_name="UTC",
                    week_start_idx=6,
                    week_start_name="sunday",
                    codex_budget=budget_cfg,
                ),
                data_version="budget-clock-v1",
            )

        initial = build(initial_now)
        refreshed = refresh_codex_source_clock(initial, now_utc=idle_now)
        expected = build(idle_now)
        initial_budget = initial.data["hero"]["budget"]
        refreshed_budget = refreshed.data["hero"]["budget"]
        expected_budget = expected.data["hero"]["budget"]

        assert initial_budget["recent_24h_usd"] > expected_budget["recent_24h_usd"] > 0
        assert refreshed_budget["recent_24h_usd"] == pytest.approx(
            expected_budget["recent_24h_usd"], abs=1e-12,
        )
        assert refreshed_budget["pace"] == pytest.approx(expected_budget["pace"])
        assert refreshed_budget["verdict"] == expected_budget["verdict"]
        assert refreshed.data_version == initial.data_version
    finally:
        cache.close()
        stats.close()


def test_retained_codex_source_keeps_private_clock_data_for_idle_budget_refresh(
    tmp_path, monkeypatch,
):
    """A contention-retained source keeps its clock kernel without publishing it."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    config = {
        "collector": {"week_start": "sunday"},
        "budget": {
            "codex": {
                "amount_usd": 10.0,
                "period": "calendar-month",
                "alert_thresholds": [80, 100],
            },
        },
    }
    initial_now = NOW
    idle_now = NOW + dt.timedelta(hours=2)
    try:
        # One cost is in the original trailing-24h window only; the second
        # remains at the idle instant.  This makes stale clock retention
        # observably wrong without relying on an all-zero outcome.
        row_id = cache.execute(
            "SELECT id FROM codex_session_entries ORDER BY id LIMIT 1"
        ).fetchone()[0]
        cache.execute(
            "UPDATE codex_session_entries SET timestamp_utc=? WHERE id=?",
            ((initial_now - dt.timedelta(hours=23)).isoformat(), row_id),
        )
        cache.execute(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            "input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, "
            "total_tokens, source_root_key, conversation_key) "
            "SELECT ?, ?, ?, session_id, model, input_tokens, cached_input_tokens, "
            "output_tokens, reasoning_output_tokens, total_tokens, source_root_key, "
            "conversation_key FROM codex_session_entries WHERE id=?",
            ("/private/retained-idle-recent.jsonl", 2,
             (initial_now - dt.timedelta(hours=1)).isoformat(), row_id),
        )
        cache.commit()
        _install_active_native_cycle(
            monkeypatch,
            source_module,
            reset=initial_now + dt.timedelta(days=2),
            now_utc=initial_now,
            root=_cache_root_key(cache),
        )
        tui = ns["_cctally_tui"]
        initial_bundle = tui._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=initial_now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            raw_config=config,
        )
        initial = initial_bundle.sources["codex"]
        assert initial.availability == "ok"
        assert initial.data["hero"]["budget"]["recent_24h_usd"] > 0
        assert initial.clock_data is not None

        # Exercise the production retained-source path, rather than a hand
        # constructed partial state.
        degraded_bundle = tui._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=initial_now,
            display_tz_name="UTC",
            codex_ingest_contended=True,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            prior_bundle=initial_bundle,
            raw_config=config,
        )
        degraded = degraded_bundle.sources["codex"]
        assert degraded.availability == "partial"
        assert degraded.freshness == "stale"
        assert degraded.data_version == initial.data_version

        budget_cfg = ns["_get_budget_config"](config)["codex"]
        expected = build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=idle_now - dt.timedelta(days=30),
                now_utc=idle_now,
                display_tz_name="UTC",
                week_start_idx=6,
                week_start_name="sunday",
                codex_budget=budget_cfg,
            ),
            data_version=degraded.data_version,
        )
        refreshed = refresh_codex_source_clock(degraded, now_utc=idle_now)
        refreshed_budget = refreshed.data["hero"]["budget"]
        expected_budget = expected.data["hero"]["budget"]

        assert refreshed_budget["recent_24h_usd"] == pytest.approx(
            expected_budget["recent_24h_usd"], abs=1e-12,
        )
        assert refreshed_budget["pace"] == pytest.approx(expected_budget["pace"])
        assert refreshed_budget["verdict"] == expected_budget["verdict"]
        assert refreshed.data_version == degraded.data_version

        wire = sys.modules["_cctally_dashboard_envelope"]._source_state_to_wire(degraded)
        assert "clock_data" not in wire
        assert "/private/retained-idle-recent.jsonl" not in repr(wire)
    finally:
        cache.close()
        stats.close()


def test_calendar_month_budget_reads_the_exact_31_day_window_without_widening_visible_rows(
    tmp_path, monkeypatch,
):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    now = dt.datetime(2026, 7, 31, 23, 30, tzinfo=UTC)
    visible_start = now - dt.timedelta(days=30)
    config = {
        "collector": {"week_start": "monday"},
        "budget": {
            "codex": {
                "amount_usd": 10.0,
                "period": "calendar-month",
                "alert_thresholds": [80, 100],
            },
        },
    }
    try:
        cache.execute(
            "UPDATE codex_session_entries SET timestamp_utc=?",
            (dt.datetime(2026, 7, 1, 1, tzinfo=UTC).isoformat(),),
        )
        cache.commit()
        budget_cfg = ns["_get_budget_config"](config)["codex"]
        _install_active_native_cycle(
            monkeypatch,
            source_module,
            reset=now + dt.timedelta(days=2),
            now_utc=now,
            root=_cache_root_key(cache),
        )
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=visible_start,
                now_utc=now,
                display_tz_name="UTC",
                week_start_idx=0,
                week_start_name="monday",
                codex_budget=budget_cfg,
            ),
            data_version="calendar-month-31d",
        )
        expected_inputs = ns["_build_vendor_budget_inputs"](
            vendor="codex",
            period="calendar-month",
            target_usd=10.0,
            alert_thresholds=(80, 100),
            now_utc=now,
            config=config,
            tz=dt.timezone.utc,
            skip_sync=True,
        )
        expected = ns["compute_budget_status"](expected_inputs)

        budget = state.data["hero"]["budget"]
        assert budget["spent_usd"] == pytest.approx(expected.spent_usd)
        assert budget["pace"]["daily_usd"] == pytest.approx(expected.daily_pace_usd)
        assert budget["verdict"] == expected.verdict
        assert budget["spent_usd"] > 0
        assert state.data["hero"]["cost_usd"] == 0.0
        assert state.data["periods"]["daily"]["rows"] == ()
    finally:
        cache.close()
        stats.close()


def test_dashboard_quota_loader_bounds_history_but_retains_active_boundary_evidence(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    quota_module = sys.modules["_cctally_quota"]
    cache = ns["open_cache_db"]()
    now = dt.datetime(2026, 7, 31, 12, tzinfo=UTC)
    old_active = now - dt.timedelta(days=20)
    recent_cutoff = now - dt.timedelta(days=2)
    rows = []
    for index in range(600):
        captured = now - dt.timedelta(days=40) + dt.timedelta(hours=index)
        rows.append((
            "codex", "root-history", "/private/history.jsonl", index,
            captured.isoformat(), "primary", "limit-history", "History", 10080,
            float(index % 100), (captured + dt.timedelta(days=7)).isoformat(),
        ))
    rows.append((
        "codex", "root-active", "/private/active.jsonl", 9999,
        old_active.isoformat(), "primary", "limit-active", "Active", 10080,
        42.0, (now + dt.timedelta(days=1)).isoformat(),
    ))
    try:
        cache.executemany(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, captured_at_utc, "
            "observed_slot, logical_limit_key, limit_name, window_minutes, "
            "used_percent, resets_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        cache.commit()

        unbounded = quota_module.load_codex_quota_observations(cache_conn=cache)
        signatures = {}
        traced_sql = []
        cache.set_trace_callback(traced_sql.append)
        bounded = quota_module.load_codex_quota_observations(
            source_root_keys=("root-history", "root-active"),
            cache_conn=cache,
            captured_at_or_after=recent_cutoff,
            active_at=now,
            max_rows=25,
            physical_signatures=signatures,
        )
        cache.set_trace_callback(None)

        assert len(unbounded) == 601
        assert len(bounded) <= 25
        assert any(row.identity.source_root_key == "root-active" for row in bounded)
        assert all(
            row.captured_at >= recent_cutoff or row.resets_at > now
            for row in bounded
        )
        assert signatures == {
            root_key: quota_module._signature(unbounded, root_key)
            for root_key in ("root-history", "root-active")
        }
        assert any(
            "LIMIT 25" in statement and "unixepoch(captured_at_utc)" in statement
            for statement in traced_sql
        )
    finally:
        cache.close()


def test_codex_source_build_bounds_quota_reads_with_retained_history_and_exact_projection(
    tmp_path, monkeypatch,
):
    """A real source build must not materialize stale quota history to validate it."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    root_key = cache.execute(
        "SELECT source_root_key FROM codex_source_roots ORDER BY source_root_key LIMIT 1"
    ).fetchone()[0]
    stale = NOW - dt.timedelta(days=730)
    try:
        cache.executemany(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, captured_at_utc, "
            "observed_slot, logical_limit_key, limit_name, window_minutes, "
            "used_percent, resets_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "codex", root_key, f"/private/retained/{index}.jsonl", index,
                    stale.isoformat(), f"stale-{index}", f"stale-limit-{index}",
                    "Retained quota", 300, 10.0,
                    (stale + dt.timedelta(hours=5)).isoformat(),
                )
                for index in range(5_000)
            ),
        )
        cache.commit()
        # Reconciliation is the production post-ingest writer; the dashboard
        # source build itself is the bounded reader under test.
        ns["reconcile_codex_quota_projection"](now=NOW)
        sql: list[str] = []
        cache.set_trace_callback(sql.append)
        state = build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=START,
                now_utc=NOW,
                display_tz_name="UTC",
            ),
            data_version="quota-scale-v1",
        )
        cache.set_trace_callback(None)

        quota_queries = [
            " ".join(statement.split()) for statement in sql
            if "FROM quota_window_snapshots" in statement
        ]
        assert quota_queries
        assert all("unixepoch(captured_at_utc) >=" in statement for statement in quota_queries)
        assert all("OR unixepoch(resets_at_utc) >" in statement for statement in quota_queries)
        assert all("LIMIT 1000" in statement for statement in quota_queries)
        assert state.data["quota"]["summary"]["active_window_count"] > 0
        assert len(state.data["quota"]["histories"]) <= 250
    finally:
        cache.set_trace_callback(None)
        cache.close()
        stats.close()


def test_codex_session_resource_keys_include_the_root_qualified_grouping_id():
    source_module = sys.modules["_cctally_dashboard_sources"]
    shared = {
        "session_id": "same-inner-session",
        "session_id_path": "2026/07/16/rollout-shared",
        "session_file": "rollout-shared",
        "directory": "2026/07/16",
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
        "total_tokens": 2,
        "cost_usd": 0.1,
        "models": ("gpt-5",),
        "last_activity": NOW,
    }
    view = SimpleNamespace(
        rows=(
            SimpleNamespace(**shared, codex_root="/private/root-a"),
            SimpleNamespace(**shared, codex_root="/private/root-b"),
        ),
        total_sessions=2,
        total_cost_usd=0.2,
        total_tokens=4,
    )

    wire = source_module._session_wire(view)

    keys = [row["key"] for row in wire["rows"]]
    assert len(set(keys)) == 2
    assert "/private/root-a" not in repr(wire)
    assert "/private/root-b" not in repr(wire)


def test_quota_hero_summary_uses_the_active_baseline_not_a_historical_high_watermark():
    source_module = sys.modules["_cctally_dashboard_sources"]
    cache = sqlite3.connect(":memory:")
    stats = sqlite3.connect(":memory:")
    now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
    identity = QuotaWindowIdentity(
        source="codex", source_root_key="root", logical_limit_key="limit",
        observed_slot="primary", window_minutes=300,
    )
    observations = (
        QuotaObservation(
            identity=identity, captured_at=now - dt.timedelta(hours=6),
            used_percent=98.0, resets_at=now - dt.timedelta(hours=1),
            source_path="/private/historical.jsonl", line_offset=1,
        ),
        QuotaObservation(
            identity=identity, captured_at=now - dt.timedelta(minutes=10),
            used_percent=20.0, resets_at=now + dt.timedelta(hours=4),
            source_path="/private/active.jsonl", line_offset=2,
        ),
    )
    try:
        quota = source_module._quota_read_model(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=now, display_tz_name="UTC",
            ),
            observations,
            decorated=False,
        )

        assert quota["summary"]["latest_percent"] == 20.0
        assert quota["summary"]["freshness"] == "fresh"
    finally:
        cache.close()
        stats.close()


def test_dashboard_quota_read_model_caps_histories_active_rows_and_milestones():
    source_module = sys.modules["_cctally_dashboard_sources"]
    cache = sqlite3.connect(":memory:")
    stats = sqlite3.connect(":memory:")
    now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
    observations = []
    for index in range(300):
        identity = QuotaWindowIdentity(
            source="codex", source_root_key=f"root-{index}",
            logical_limit_key=f"limit-{index}", observed_slot="primary",
            window_minutes=300,
        )
        observations.extend((
            QuotaObservation(
                identity=identity, captured_at=now - dt.timedelta(minutes=20),
                used_percent=10.0, resets_at=now + dt.timedelta(hours=4),
                source_path=f"/private/{index}.jsonl", line_offset=1,
            ),
            QuotaObservation(
                identity=identity, captured_at=now - dt.timedelta(minutes=5),
                used_percent=11.0, resets_at=now + dt.timedelta(hours=4),
                source_path=f"/private/{index}.jsonl", line_offset=2,
            ),
        ))
    try:
        quota = source_module._quota_read_model(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=now, display_tz_name="UTC",
            ),
            observations,
            decorated=False,
        )

        assert len(quota["histories"]) <= source_module.SOURCE_HISTORY_LIMIT
        assert len(quota["summary"]["active"]) <= source_module.SOURCE_HISTORY_LIMIT
        assert len(quota["milestones"]) <= source_module.SOURCE_HISTORY_LIMIT
    finally:
        cache.close()
        stats.close()


def test_dashboard_quota_cap_reserves_freshest_model_scoped_history(monkeypatch):
    """After active account rows, capped history retains the most recently
    captured independent-pool fact.  Resource-key order is deliberately made
    adverse so the asserted rule is recency, not an incidental digest order."""
    source_module = sys.modules["_cctally_dashboard_sources"]
    monkeypatch.setattr(source_module, "SOURCE_HISTORY_LIMIT", 4)
    cache = sqlite3.connect(":memory:")
    stats = sqlite3.connect(":memory:")
    now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)

    active = [
        _quota_observation(
            root=f"active-{index}",
            window_minutes=10_080,
            resets_at=now + dt.timedelta(days=7),
            captured_at=now - dt.timedelta(minutes=index + 1),
            logical_limit_key=f"active-limit-{index}",
            used_percent=20.0 + index,
        )
        for index in range(3)
    ]
    inactive_standard = [
        _quota_observation(
            root=f"inactive-{index}",
            window_minutes=10_080,
            resets_at=now - dt.timedelta(days=1),
            captured_at=now - dt.timedelta(days=index + 1),
            logical_limit_key=f"inactive-limit-{index}",
        )
        for index in range(2)
    ]

    model_specs = []
    for index in range(4):
        root = f"pool-{index}"
        logical_limit_key = f"pool-limit-{index}"
        key = source_module.dashboard_resource_key(
            "quota", "codex", root, logical_limit_key, "primary", 10_080,
        )
        model_specs.append((key, root, logical_limit_key))
    model_specs.sort()
    model_scoped = [
        _quota_observation(
            root=root,
            window_minutes=10_080,
            resets_at=now + dt.timedelta(days=3),
            # The old digest-order cap chooses model_specs[0], which is oldest.
            captured_at=now - dt.timedelta(hours=len(model_specs) - index),
            limit_name="GPT-5.3-Codex-Spark",
            logical_limit_key=logical_limit_key,
            used_percent=90.0 + index,
        )
        for index, (_key, root, logical_limit_key) in enumerate(model_specs)
    ]
    freshest_at = max(row.captured_at for row in model_scoped)

    try:
        quota = source_module._quota_read_model(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=START,
                now_utc=now,
                display_tz_name="UTC",
            ),
            (*active, *inactive_standard, *model_scoped),
            decorated=False,
        )
    finally:
        cache.close()
        stats.close()

    assert len(quota["histories"]) == 4
    assert quota["summary"]["active_window_count"] == 3
    assert quota["summary"]["latest_percent"] == 22.0
    retained_pools = [
        row for row in quota["histories"] if row.get("model_scoped")
    ]
    assert len(retained_pools) == 1
    assert retained_pools[0]["captured_at"] == freshest_at.isoformat()
    assert retained_pools[0]["current_percent"] > quota["summary"]["latest_percent"]

    from _lib_dashboard_sources import SourceDashboardState
    refreshed = source_module.refresh_codex_source_clock(
        SourceDashboardState(
            source="codex",
            availability="ok",
            freshness="fresh",
            warnings=(),
            data_version="issue-412-cap",
            last_success_at=now,
            capabilities={},
            data={"quota": quota},
        ),
        now_utc=now + dt.timedelta(minutes=5),
    )
    refreshed_quota = refreshed.data["quota"]
    assert [
        (row["key"], row.get("model_scoped"), row["captured_at"])
        for row in refreshed_quota["histories"]
    ] == [
        (row["key"], row.get("model_scoped"), row["captured_at"])
        for row in quota["histories"]
    ]
    assert refreshed_quota["summary"]["active_window_count"] == 3
    assert refreshed_quota["summary"]["latest_percent"] == 22.0
    assert refreshed_quota["summary"]["freshness"] == "fresh"


def test_dashboard_quota_milestones_include_native_window_and_accounting_costs():
    source_module = sys.modules["_cctally_dashboard_sources"]
    cache = sqlite3.connect(":memory:")
    stats = sqlite3.connect(":memory:")
    now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
    reset = now + dt.timedelta(days=2)
    identity = QuotaWindowIdentity(
        source="codex", source_root_key="root-a", logical_limit_key="weekly",
        observed_slot="primary", window_minutes=10_080,
    )
    observations = (
        QuotaObservation(
            identity=identity, captured_at=now - dt.timedelta(hours=2),
            used_percent=5.0, resets_at=reset,
            source_path="/private/a.jsonl", line_offset=1,
        ),
        QuotaObservation(
            identity=identity, captured_at=now - dt.timedelta(hours=1),
            used_percent=6.0, resets_at=reset,
            source_path="/private/a.jsonl", line_offset=2,
        ),
    )
    accounting_entries = (
        SimpleNamespace(
            source_root_key="root-a", timestamp=now - dt.timedelta(hours=3),
            cost_usd=1.25,
        ),
        SimpleNamespace(
            source_root_key="root-a", timestamp=now - dt.timedelta(hours=1),
            cost_usd=2.75,
        ),
    )
    try:
        quota = source_module._quota_read_model(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=now, display_tz_name="UTC",
            ),
            observations,
            accounting_entries=accounting_entries,
            decorated=False,
        )

        milestone = quota["milestones"][0]
        assert milestone["quota_key"] == quota["histories"][0]["key"]
        assert milestone["window_minutes"] == 10_080
        assert milestone["resets_at"] == reset.isoformat()
        assert milestone["cumulative_usd"] == pytest.approx(4.0)
        assert milestone["marginal_usd"] == pytest.approx(4.0)
    finally:
        cache.close()
        stats.close()


def test_dashboard_current_cycle_uses_complete_durable_quota_breakdown():
    """The hero modal must not rebuild milestones from its capped read tail.

    The dashboard observation slice starts at 5%, while the durable projection
    retains the complete 1-6% block derived from the rollout JSONLs.  The modal
    contract is the canonical durable breakdown, not only the late 6% crossing.
    """
    load_script()
    source_module = sys.modules["_cctally_dashboard_sources"]
    cache = sqlite3.connect(":memory:")
    stats = sqlite3.connect(":memory:")
    now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
    reset = now + dt.timedelta(days=2)
    identity = QuotaWindowIdentity(
        source="codex", source_root_key="root-a", logical_limit_key="weekly",
        observed_slot="primary", window_minutes=10_080,
    )
    cache.executescript("""
        CREATE TABLE quota_window_snapshots (
            source TEXT, source_root_key TEXT, source_path TEXT,
            line_offset INTEGER, captured_at_utc TEXT, observed_slot TEXT,
            logical_limit_key TEXT, limit_id TEXT, limit_name TEXT,
            window_minutes INTEGER, used_percent REAL, resets_at_utc TEXT,
            plan_type TEXT, individual_limit_json TEXT, reached_type TEXT
        );
        CREATE TABLE codex_session_entries (
            timestamp_utc TEXT, source_path TEXT, line_offset INTEGER,
            model TEXT, input_tokens INTEGER, cached_input_tokens INTEGER,
            output_tokens INTEGER, reasoning_output_tokens INTEGER,
            total_tokens INTEGER, source_root_key TEXT
        );
    """)
    stats.executescript("""
        CREATE TABLE quota_percent_milestones (
            source TEXT, source_root_key TEXT, logical_limit_key TEXT,
            observed_slot TEXT, window_minutes INTEGER, resets_at_utc TEXT,
            percent_threshold INTEGER, captured_at_utc TEXT,
            source_path TEXT, line_offset INTEGER, orphaned_at TEXT,
            account_key TEXT
        );
    """)
    path = "/private/a.jsonl"
    for percent in range(7):
        captured = now - dt.timedelta(hours=7 - percent)
        cache.execute(
            "INSERT INTO quota_window_snapshots VALUES "
            "('codex', 'root-a', ?, ?, ?, 'primary', 'weekly', NULL, NULL, "
            "10080, ?, ?, NULL, NULL, NULL)",
            (path, percent, captured.isoformat(), float(percent), reset.isoformat()),
        )
        cache.execute(
            "INSERT INTO quota_window_snapshots VALUES "
            "('codex', 'root-a', ?, ?, ?, 'primary', 'five-hour', NULL, NULL, "
            "300, ?, ?, NULL, NULL, NULL)",
            (
                path, 100 + percent, captured.isoformat(), float(percent * 2),
                (captured + dt.timedelta(hours=4)).isoformat(),
            ),
        )
        if percent:
            stats.execute(
                "INSERT INTO quota_percent_milestones VALUES "
                "('codex', 'root-a', 'weekly', 'primary', 10080, ?, ?, ?, ?, ?, NULL, "
                "'unattributed')",
                (reset.isoformat(), percent, captured.isoformat(), path, percent),
            )
            cache.execute(
                "INSERT INTO codex_session_entries VALUES "
                "(?, ?, ?, 'gpt-5', 1000, 500, 100, 25, 1100, 'root-a')",
                (captured.isoformat(), path, percent),
            )
    cache.commit()
    stats.commit()
    # Simulate the dashboard's capped tail: the complete 1-4% crossings are
    # absent here but remain available in the durable projection above.
    observations = (
        QuotaObservation(
            identity=identity, captured_at=now - dt.timedelta(hours=2),
            used_percent=5.0, resets_at=reset,
            source_path=path, line_offset=5,
        ),
        QuotaObservation(
            identity=identity, captured_at=now - dt.timedelta(hours=1),
            used_percent=6.0, resets_at=reset,
            source_path=path, line_offset=6,
        ),
    )
    try:
        quota = source_module._quota_read_model(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=now, display_tz_name="UTC", speed="standard",
            ),
            observations,
            decorated=False,
        )

        weekly = [
            row for row in quota["milestones"]
            if row["window_minutes"] == 10_080
        ]
        assert [row["percent"] for row in weekly] == [6, 5, 4, 3, 2, 1]
        assert weekly[0]["cumulative_usd"] > weekly[-1]["cumulative_usd"]
        assert all(row["marginal_usd"] > 0 for row in weekly)
        assert [row["five_hour_percent"] for row in weekly] == [12, 10, 8, 6, 4, 2]
    finally:
        cache.close()
        stats.close()


def test_source_retained_history_wires_are_bounded_newest_first():
    source_module = sys.modules["_cctally_dashboard_sources"]
    stats = sqlite3.connect(":memory:")
    try:
        stats.executescript("""
            CREATE TABLE quota_window_blocks (
                source TEXT, source_root_key TEXT, logical_limit_key TEXT,
                observed_slot TEXT, window_minutes INTEGER, limit_name TEXT,
                resets_at_utc TEXT, current_percent REAL, orphaned_at TEXT
            );
            CREATE TABLE budget_milestones (
                vendor TEXT, period_start_at TEXT, period TEXT, threshold INTEGER,
                budget_usd REAL, spent_usd REAL, consumption_pct REAL,
                crossed_at_utc TEXT, alerted_at TEXT
            );
            CREATE TABLE projected_milestones (
                metric TEXT, period TEXT, threshold INTEGER, projected_value REAL,
                denominator REAL, crossed_at_utc TEXT, alerted_at TEXT
            );
            CREATE TABLE quota_threshold_events (
                source TEXT, source_root_key TEXT, logical_limit_key TEXT,
                observed_slot TEXT, window_minutes INTEGER, resets_at_utc TEXT,
                threshold INTEGER, severity TEXT, created_at_utc TEXT,
                disposition TEXT, orphaned_at TEXT
            );
        """)
        for index in range(251):
            stamp = f"2026-07-20T{index:04d}Z"
            stats.execute(
                "INSERT INTO quota_window_blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("codex", "root", "limit", "slot", 300, "Quota", stamp, index, None),
            )
            stats.execute(
                "INSERT INTO budget_milestones VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("codex", stamp, "calendar-week", index, 100, index, index, stamp, stamp),
            )
            stats.execute(
                "INSERT INTO projected_milestones VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("codex_budget_usd", "calendar-week", index, index, 100, stamp, stamp),
            )
            stats.execute(
                "INSERT INTO quota_threshold_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("codex", "root", "limit", "slot", 300, stamp, index, "warn", stamp, "alerted", None),
            )

        assert len(source_module._quota_wire(stats)) <= 250
        assert len(source_module._budget_wire(stats)) <= 250
        assert len(source_module._projected_budget_wire(stats)) <= 250
        assert len(source_module._alerts_wire(stats)) <= 250
    finally:
        stats.close()


def test_codex_source_builder_loads_quota_and_projects_once_from_context(
    tmp_path, monkeypatch,
):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    quota_loader = source_module.load_codex_quota_observations
    observations = quota_loader()
    calls: dict[str, list[object]] = {"quota": [], "projects": []}

    def quota_from_context(
        *, source_root_keys=None, cache_conn=None, captured_at_or_after=None,
        active_at=None, max_rows=None, physical_signatures=None,
    ):
        calls["quota"].append(cache_conn)
        assert source_root_keys
        assert captured_at_or_after == NOW - dt.timedelta(days=35)
        assert active_at == NOW
        assert max_rows == source_module.DASHBOARD_QUOTA_OBSERVATION_LIMIT
        assert physical_signatures is None
        return observations

    def projects_from_context(start, end, *, speed, sync, group="git-root", cache_conn=None):
        assert start == START
        assert end == NOW + dt.timedelta(microseconds=1)
        assert speed == "standard"
        assert sync is False
        calls["projects"].append(cache_conn)
        return ()

    monkeypatch.setattr(source_module, "load_codex_quota_observations", quota_from_context)
    monkeypatch.setattr(source_module, "load_qualified_codex_entries", projects_from_context)

    assert not hasattr(source_module, "iter_codex_entries")
    try:
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=START,
                now_utc=NOW,
                display_tz_name="UTC",
            ),
            data_version="context-v1",
        )

        assert state.source == "codex"
        assert calls == {"quota": [cache], "projects": [cache]}
    finally:
        cache.close()
        stats.close()


def test_codex_source_builder_never_opens_an_independent_cache_connection(
    tmp_path, monkeypatch,
):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    quota_module = sys.modules["_cctally_quota"]
    source_module = sys.modules["_cctally_dashboard_sources"]

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("source builder must use DashboardReadContext.cache_conn")

    monkeypatch.setattr(quota_module, "_cache_connection", forbidden_open)
    monkeypatch.setitem(ns, "open_cache_db", forbidden_open)
    try:
        _install_active_native_cycle(
            monkeypatch, source_module, reset=NOW + dt.timedelta(days=2), root=_cache_root_key(cache),
        )
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=START,
                now_utc=NOW,
                display_tz_name="UTC",
            ),
            data_version="context-v1",
        )

        assert state.availability == "ok"
        assert cache.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] > 0
    finally:
        cache.close()
        stats.close()


def test_empty_codex_read_model_is_available_empty_data_not_unavailable(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=START,
                now_utc=NOW,
                display_tz_name="UTC",
                week_start_idx=0,
            ),
            data_version="empty-v1",
        )

        assert state.availability == "empty"
        assert state.freshness == "fresh"
        assert state.data is not None
        assert state.capabilities["hero"].status == "supported"
        assert state.data["hero"]["cycle"] is None
        assert state.data["hero"]["total_tokens"] == 0
        assert state.data["sessions"]["rows"] == ()
        assert state.data["periods"]["daily"]["rows"] == ()
    finally:
        cache.close()
        stats.close()


def test_codex_projection_coherence_requires_each_active_root_state(tmp_path, monkeypatch):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    try:
        context = DashboardReadContext(
            cache_conn=cache,
            stats_conn=stats,
            range_start=START,
            now_utc=NOW,
            display_tz_name="UTC",
        )

        coherence = codex_projection_coherence(context)

        assert coherence.coherent is True, coherence.reason
    finally:
        cache.close()
        stats.close()


def test_codex_projection_incoherence_is_scoped_to_the_current_hero_generation(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        stats.execute("UPDATE quota_projection_state SET physical_signature='not-the-cache'")
        _install_active_native_cycle(
            monkeypatch,
            source_module,
            reset=NOW + dt.timedelta(days=2),
            root=_cache_root_key(cache),
        )
        context = DashboardReadContext(
            cache_conn=cache,
            stats_conn=stats,
            range_start=START,
            now_utc=NOW,
            display_tz_name="UTC",
        )

        state = source_module.build_codex_source_state(context, data_version="codex-v1")

        assert state.availability == "partial"
        assert state.freshness == "fresh"
        assert state.capabilities["hero"].status == "unavailable"
        assert state.capabilities["hero"].semantics == "projection-incoherent"
        assert state.data["hero"]["cycle"] is None
        assert state.data["hero"]["total_tokens"] is None
        assert any(
            warning.code == "codex_projection_incoherent" and warning.domain == "hero"
            for warning in state.warnings
        )
        assert state.data["periods"]["daily"]["rows"]
        assert state.data["sessions"]["rows"]
        assert state.data["quota"]["histories"]
        assert "root" not in repr(state.data["hero"])
    finally:
        cache.close()
        stats.close()


# =========================================================================
# #350 — the tick honors the decision deadline (spec §3.3, §5.4).
#
# A clock-only fix survives exactly one tick: `_tui_source_bundle_can_idle`
# requires `availability in ("ok","empty")` AND `freshness == "fresh"`, so any
# degraded result forces the full source-bundle path — which then re-nulls
# through the build-time site. And `reuse_coherent_source_state` returns the
# EXACT prior object for a coherent provider, bypassing the clock entirely
# (§2.5). Both holes are closed here: a passed deadline forces an authoritative
# rebuild, and every path is clocked before `compose_all_state`.
# =========================================================================

_TICK_CONFIG = {"collector": {"week_start": "sunday"}}


def _install_observations(monkeypatch, source_module, observations):
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_kwargs: observations,
    )


def _build_tick_bundle(tui, stats, *, now_utc, prior_bundle=None):
    return tui._tui_build_source_bundle(
        stats_conn=stats,
        now_utc=now_utc,
        display_tz_name="UTC",
        codex_ingest_contended=False,
        claude_cost_usd=0.0,
        claude_total_tokens=0,
        common_range_start=now_utc - dt.timedelta(days=30),
        prior_bundle=prior_bundle,
        raw_config=_TICK_CONFIG,
    )


def test_a_passed_deadline_forces_an_authoritative_codex_rebuild(tmp_path, monkeypatch):
    """A fresh cycle that goes stale while idle acquires the marker at the
    crossing. Without the deadline, `reuse_coherent_source_state` would hand back
    the EXACT prior object and the hero would never learn its evidence aged."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    tui = ns["_cctally_tui"]
    reset = NOW + dt.timedelta(days=2)
    captured_at = NOW - dt.timedelta(minutes=10)
    try:
        _copy_accounting_row_at(
            cache, timestamp=NOW - dt.timedelta(days=1),
            source_path="/private/350-tick-deadline.jsonl",
        )
        _install_observations(monkeypatch, source_module, (
            _quota_observation(
                root=_cache_root_key(cache), window_minutes=10_080,
                resets_at=reset, captured_at=captured_at,
            ),
            _quota_observation(
                root=_cache_root_key(cache), window_minutes=300,
                resets_at=NOW + dt.timedelta(hours=4), captured_at=captured_at,
                logical_limit_key="five-hour-limit", line_offset=2,
            ),
        ))
        initial_bundle = _build_tick_bundle(tui, stats, now_utc=NOW)
        initial = initial_bundle.sources["codex"]
        assert initial.capabilities["hero"].status == "supported"
        assert "cycle_freshness" not in initial.data["hero"]
        assert dict(initial.domain_freshness) == {
            "hero": "fresh",
            "quota": "fresh",
            "sessions": "fresh",
        }
        assert initial.clock_data["codex_next_decision_at"] == (
            captured_at + dt.timedelta(seconds=3600)
        )

        # Before the deadline: the coherent prior is reused verbatim (only the
        # clock's time-derived fields move), so no marker yet.
        #
        # The guard is what makes this half of the test discriminating. Every
        # assertion below (`data_version`, absent marker, equal `cost_usd`) also
        # holds under a REBUILD, so without it the test would still pass if the
        # deadline check regressed into rebuilding on every tick — which is
        # precisely the per-tick rebuild storm AC5 forbids.
        with monkeypatch.context() as no_rebuild:
            no_rebuild.setattr(
                tui, "build_codex_source_state",
                lambda *_a, **_k: pytest.fail(
                    "a pre-deadline tick must reuse the prior state, not rebuild"
                ),
            )
            before_bundle = _build_tick_bundle(
                tui, stats, now_utc=NOW + dt.timedelta(minutes=30),
                prior_bundle=initial_bundle,
            )
        before = before_bundle.sources["codex"]
        assert before.data_version == initial.data_version
        assert "cycle_freshness" not in before.data["hero"]
        assert before.data["hero"]["cost_usd"] == initial.data["hero"]["cost_usd"]
        assert before.freshness == "fresh"
        assert dict(before.domain_freshness) == {
            "hero": "fresh",
            "quota": "stale",
            "sessions": "fresh",
        }
        assert before_bundle.sources["all"].data["combined"] is not None
        assert before_bundle.sources["all"].warnings == ()
        assert dict(before_bundle.sources["all"].domain_freshness) == {
            "hero": "fresh",
            "quota": "stale",
            "sessions": "fresh",
        }
        envelope_module = sys.modules["_cctally_dashboard_envelope"]
        before_envelope = envelope_module._source_bundle_to_envelope(before_bundle)
        before_wire = before_envelope["sources"]["codex"]
        assert before_wire["freshness"] == "fresh"
        assert before_wire["domain_freshness"] == {
            "hero": "fresh",
            "quota": "stale",
            "sessions": "fresh",
        }
        assert "clock_data" not in before_wire
        assert before_envelope["sources"]["all"]["data"]["combined"] is not None
        assert set(
            before_envelope["sources"]["all"]["data"]["providers"]
        ) == {"claude", "codex"}

        # After the deadline: an authoritative rebuild stamps the marker while
        # every backward-looking actual survives.
        after_bundle = _build_tick_bundle(
            tui, stats, now_utc=NOW + dt.timedelta(hours=2),
            prior_bundle=initial_bundle,
        )
        after = after_bundle.sources["codex"]
        assert after.data["hero"]["cycle_freshness"] == "stale"
        assert after.data["hero"]["cost_usd"] == initial.data["hero"]["cost_usd"]
        assert after.data["hero"]["cycle"] == initial.data["hero"]["cycle"]
        assert after.capabilities["hero"].status == "supported"
        assert after.availability == "ok"
        assert after.freshness == "fresh"
        assert dict(after.domain_freshness) == {
            "hero": "stale",
            "quota": "stale",
            "sessions": "fresh",
        }

        # #359: All retains the compatible backward-looking actuals and states
        # that their quota boundary is stale, without degrading Codex itself.
        all_after = after_bundle.sources["all"]
        assert all_after.data["combined"] is not None
        assert [warning.code for warning in all_after.warnings] == [
            "combined_totals_stale",
        ]
        assert all_after.warnings[0].domain == "hero"
        assert all_after.warnings[0].message == (
            "Codex quota evidence is stale; combined totals use retained actuals."
        )
        assert (all_after.availability, all_after.freshness) == ("partial", "fresh")
        assert dict(all_after.domain_freshness) == {
            "hero": "stale",
            "quota": "stale",
            "sessions": "fresh",
        }
        after_envelope = envelope_module._source_bundle_to_envelope(after_bundle)
        assert after_envelope["sources"]["codex"]["freshness"] == "fresh"
        assert after_envelope["sources"]["codex"]["domain_freshness"] == {
            "hero": "stale",
            "quota": "stale",
            "sessions": "fresh",
        }
        assert after_envelope["sources"]["all"]["data"]["combined"] is not None
        assert set(
            after_envelope["sources"]["all"]["data"]["providers"]
        ) == {"claude", "codex"}
    finally:
        cache.close()
        stats.close()


def test_reused_codex_state_is_clocked_before_all_composition(tmp_path, monkeypatch):
    """Spec §2.5's hole: `reuse_coherent_source_state` returns the EXACT prior
    object when `_coherent_provider` holds, so without unconditional clocking an
    expired cycle would be carried forward with no re-check at all."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    tui = ns["_cctally_tui"]
    reset = NOW + dt.timedelta(minutes=10)
    try:
        _copy_accounting_row_at(
            cache, timestamp=NOW - dt.timedelta(days=1),
            source_path="/private/350-tick-reuse.jsonl",
        )
        _install_observations(monkeypatch, source_module, (
            _quota_observation(
                root=_cache_root_key(cache), window_minutes=10_080,
                resets_at=reset, captured_at=NOW - dt.timedelta(minutes=10),
            ),
        ))
        initial_bundle = _build_tick_bundle(tui, stats, now_utc=NOW)
        initial = initial_bundle.sources["codex"]
        assert initial.capabilities["hero"].status == "supported"

        # Strip the deadline so ONLY the reuse path can be under test — this is
        # the shape any state without a recorded deadline presents.
        no_deadline = dataclasses.replace(
            initial, clock_data={"codex_budget_cost_events": ()},
        )
        prior_claude = initial_bundle.sources["claude"]
        prior_bundle = tui.SourceDashboardBundle(
            source_schema_version=SOURCE_SCHEMA_VERSION,
            default_source="claude",
            source_order=("claude", "codex", "all"),
            sources={
                "claude": prior_claude,
                "codex": no_deadline,
                "all": tui.compose_all_state(prior_claude, no_deadline),
            },
        )
        monkeypatch.setattr(
            tui, "build_codex_source_state",
            lambda *_a, **_k: pytest.fail("the reuse path must not rebuild here"),
        )

        rebuilt = _build_tick_bundle(
            tui, stats, now_utc=NOW + dt.timedelta(minutes=20),
            prior_bundle=prior_bundle,
        ).sources["codex"]

        assert rebuilt.data["hero"]["cost_usd"] is None
        assert rebuilt.data["hero"]["cycle"] is None
        assert rebuilt.capabilities["hero"].status == "unavailable"
        assert rebuilt.availability == "partial"
        assert any(
            warning.code == "codex_cycle_unavailable" for warning in rebuilt.warnings
        )
    finally:
        cache.close()
        stats.close()


def test_a_baseline_switch_is_picked_up_at_its_deadline(tmp_path, monkeypatch):
    """Spec §2.2: a future-dated capture becomes baseline-eligible purely because
    time passed, switching the selected reset on IDENTICAL frozen evidence."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    tui = ns["_cctally_tui"]
    first_reset = NOW + dt.timedelta(days=2)
    second_reset = NOW + dt.timedelta(days=5)
    # Within the 300s clock-skew tolerance, so freshness is "fresh", not "future".
    future_capture = NOW + dt.timedelta(seconds=200)
    try:
        root_key = _cache_root_key(cache)
        _install_observations(monkeypatch, source_module, (
            _quota_observation(
                root=root_key, window_minutes=10_080, resets_at=first_reset,
                captured_at=NOW - dt.timedelta(minutes=10), used_percent=25.0,
            ),
            _quota_observation(
                root=root_key, window_minutes=10_080, resets_at=second_reset,
                captured_at=future_capture, used_percent=30.0, line_offset=2,
            ),
        ))
        initial_bundle = _build_tick_bundle(tui, stats, now_utc=NOW)
        initial = initial_bundle.sources["codex"]
        assert initial.data["hero"]["cycle"]["resets_at"] == first_reset.isoformat()
        assert initial.clock_data["codex_next_decision_at"] == future_capture

        switched = _build_tick_bundle(
            tui, stats, now_utc=NOW + dt.timedelta(seconds=250),
            prior_bundle=initial_bundle,
        ).sources["codex"]

        assert switched.data["hero"]["cycle"]["resets_at"] == second_reset.isoformat()
        assert switched.capabilities["hero"].status == "supported"
    finally:
        cache.close()
        stats.close()


def test_drift_to_two_stale_boundaries_resolves_conflicting(tmp_path, monkeypatch):
    """Spec §2.2's counterexample: one fresh plus one stale boundary resolves
    FRESH now and `conflicting` an hour later on the very same rows."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    tui = ns["_cctally_tui"]
    fresh_reset = NOW + dt.timedelta(days=2)
    captured_at = NOW - dt.timedelta(minutes=10)
    try:
        _install_observations(monkeypatch, source_module, (
            _quota_observation(
                root="root-a", window_minutes=10_080, resets_at=fresh_reset,
                captured_at=captured_at, logical_limit_key="limit-a",
            ),
            _quota_observation(
                root="root-b", window_minutes=10_080,
                resets_at=NOW + dt.timedelta(days=5),
                captured_at=NOW - dt.timedelta(hours=2), logical_limit_key="limit-b",
            ),
        ))
        initial_bundle = _build_tick_bundle(tui, stats, now_utc=NOW)
        initial = initial_bundle.sources["codex"]
        assert initial.data["hero"]["cycle"]["resets_at"] == fresh_reset.isoformat()
        assert initial.clock_data["codex_next_decision_at"] == (
            captured_at + dt.timedelta(seconds=3600)
        )

        drifted = _build_tick_bundle(
            tui, stats, now_utc=NOW + dt.timedelta(hours=1),
            prior_bundle=initial_bundle,
        ).sources["codex"]

        assert drifted.data["hero"]["cycle"] is None
        assert drifted.data["hero"]["cost_usd"] is None
        assert drifted.capabilities["hero"].status == "unavailable"
        assert drifted.availability == "partial"
    finally:
        cache.close()
        stats.close()


def test_account_a_expiry_repins_the_hero_to_a_live_sibling(tmp_path, monkeypatch):
    """#341 at-least-one-live-cycle behavior across the crossing: when the
    selected account's cycle expires, a rebuilt dashboard re-pins to the live
    sibling — so an idle one must too, or the two disagree."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    tui = ns["_cctally_tui"]
    a_reset = NOW + dt.timedelta(minutes=30)
    b_reset = NOW + dt.timedelta(days=3)
    try:
        _install_observations(monkeypatch, source_module, (
            _quota_observation(
                root="root-a", window_minutes=10_080, resets_at=a_reset,
                captured_at=NOW - dt.timedelta(minutes=10),
                logical_limit_key="limit-a", account_key="acct-a",
            ),
            _quota_observation(
                root="root-b", window_minutes=10_080, resets_at=b_reset,
                captured_at=NOW - dt.timedelta(minutes=10),
                logical_limit_key="limit-b", account_key="acct-b",
            ),
        ))
        initial_bundle = _build_tick_bundle(tui, stats, now_utc=NOW)
        initial = initial_bundle.sources["codex"]
        # Two distinct live cycles resolve; the aggregate hero pins the first.
        assert initial.data["hero"]["cycle"]["resets_at"] == a_reset.isoformat()
        assert initial.clock_data["codex_next_decision_at"] == a_reset
        assert len(
            source_module._resolve_codex_weekly_cycle(
                source_module.load_codex_quota_observations(), NOW,
            )
        ) == 2

        # Both survive idle ticks before the crossing.
        idle = _build_tick_bundle(
            tui, stats, now_utc=NOW + dt.timedelta(minutes=10),
            prior_bundle=initial_bundle,
        ).sources["codex"]
        assert idle.data["hero"]["cycle"]["resets_at"] == a_reset.isoformat()
        assert idle.capabilities["hero"].status == "supported"

        repinned = _build_tick_bundle(
            tui, stats, now_utc=NOW + dt.timedelta(minutes=40),
            prior_bundle=initial_bundle,
        ).sources["codex"]

        assert repinned.data["hero"]["cycle"]["resets_at"] == b_reset.isoformat()
        assert repinned.capabilities["hero"].status == "supported"
        assert repinned.availability == "ok"
        # Undecorated (<=1 REAL registered account): no per-account wire appears.
        assert "accounts" not in repinned.data
        assert "cycles" not in repinned.data["hero"]
    finally:
        cache.close()
        stats.close()


# =========================================================================
# #429 — one quota identity resolves to one `captured_at`, one `freshness`
# and one `stale_after_seconds` everywhere in a single envelope.
# =========================================================================

_429_NOW = dt.datetime(2026, 4, 24, 13, 0, tzinfo=UTC)


def _quota_observations_with_repeated_value(
    now: dt.datetime = _429_NOW,
    *,
    root: str = "429-root",
    logical_limit_key: str = "429-limit",
    account_key: str | None = None,
) -> tuple[QuotaObservation, ...]:
    """One weekly identity whose newest PHYSICAL capture repeats the value.

    `build_history` collapses consecutive equal `logical_value_tuple`
    observations, so the retained INTERPRETED baseline is the first capture
    while `quota_freshness` reads the last PHYSICAL one — the exact idle case
    where the build's `baseline.captured_at` and the row's own evidence
    timestamp disagree. Three captures: a distinct earlier value so the block
    has a real interpreted run, then a repeated value across two captures.
    """
    reset = now + dt.timedelta(days=3)
    return (
        _quota_observation(
            root=root, window_minutes=10_080, resets_at=reset,
            captured_at=now - dt.timedelta(minutes=50),
            logical_limit_key=logical_limit_key, used_percent=9.0,
            line_offset=1, account_key=account_key,
        ),
        _quota_observation(
            root=root, window_minutes=10_080, resets_at=reset,
            captured_at=now - dt.timedelta(minutes=40),
            logical_limit_key=logical_limit_key, used_percent=12.0,
            line_offset=2, account_key=account_key,
        ),
        _quota_observation(
            root=root, window_minutes=10_080, resets_at=reset,
            captured_at=now - dt.timedelta(minutes=5),
            logical_limit_key=logical_limit_key, used_percent=12.0,
            line_offset=3, account_key=account_key,
        ),
    )


def test_forecast_baseline_matches_build_baseline():
    """#429: the shared active-row helper reads forecast.current_percent, which
    re-derives its baseline from physical observations. If that ever diverged
    from the build's own select_baseline, active rows would change value
    silently — not just their timestamp."""
    from _lib_quota import build_history, forecast_quota, select_baseline

    now = _429_NOW
    observations = _quota_observations_with_repeated_value(now)
    histories = build_history(observations)
    assert len(histories) == 1
    history = histories[0]
    baseline = select_baseline(history.observations, now)
    forecast = forecast_quota(history.physical_observations, now)
    assert baseline is not None
    assert forecast.current_percent == baseline.used_percent
    assert forecast.resets_at == baseline.canonical_resets_at


def _build_codex_state_from(
    observations,
    *,
    now_utc,
    tmp_path,
    monkeypatch,
    data_version="429-v1",
):
    """Build a real Codex source state over exactly ``observations``.

    Uses the production builder rather than a hand-assembled state, because the
    #429 defect lives in the disagreement between that builder and the idle
    clock — a hand-built state would beg the question.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    source_module = sys.modules["_cctally_dashboard_sources"]
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations",
        lambda **_kwargs: tuple(observations),
    )
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        return source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats,
                range_start=now_utc - dt.timedelta(days=30),
                now_utc=now_utc, display_tz_name="UTC", week_start_idx=0,
            ),
            data_version=data_version,
        )
    finally:
        cache.close()
        stats.close()


def test_clock_at_build_instant_is_a_noop(tmp_path, monkeypatch):
    """#429 §5.1: refresh_codex_source_clock claims same-instant identity
    (_cctally_tui.py:2799). It is false today — the build fills
    active[].captured_at from the interpreted baseline while the clock refills
    it from the latest physical observation."""
    from _lib_quota import build_history, quota_freshness, select_baseline

    now = _429_NOW
    # Precondition, asserted rather than assumed: repeated logical value, so the
    # newest PHYSICAL capture is newer than the newest INTERPRETED one.
    observations = _quota_observations_with_repeated_value(now)
    history = build_history(observations)[0]
    baseline = select_baseline(history.observations, now)
    freshness = quota_freshness(history.physical_observations, now)
    assert baseline.captured_at < freshness.captured_at, (
        "fixture does not exercise the defect: baseline and latest physical "
        "resolve to the same capture"
    )

    state = _build_codex_state_from(
        observations, now_utc=now, tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    clocked = refresh_codex_source_clock(state, now_utc=now)
    assert clocked == state


def _three_hundred_active_identities(
    now: dt.datetime = _429_NOW, *, count: int = 300, peak_on_discarded: bool = False,
) -> tuple[QuotaObservation, ...]:
    """``count`` live 5h identities, deliberately above ``SOURCE_HISTORY_LIMIT``.

    With ``peak_on_discarded`` the highest ``current_percent`` is placed on an
    identity the retention cap DISCARDS, so a summary scalar computed before the
    cap reports a percentage that appears nowhere in the published active set.
    """
    source_module = sys.modules["_cctally_dashboard_sources"]
    specs = []
    for index in range(count):
        root = f"429-root-{index:03d}"
        logical_limit_key = f"429-limit-{index:03d}"
        specs.append((
            source_module.dashboard_resource_key(
                "quota", "codex", root, logical_limit_key, "primary", 300,
            ),
            root,
            logical_limit_key,
        ))
    peak_index: int | None = None
    if peak_on_discarded:
        # Every identity is active, so the retention key ties on its first two
        # components and the cap keeps the SOURCE_HISTORY_LIMIT lowest keys.
        discarded = sorted(specs)[source_module.SOURCE_HISTORY_LIMIT:]
        assert discarded, "precondition: the fixture must exceed the cap"
        peak_index = next(
            index for index, spec in enumerate(specs) if spec[0] == discarded[0][0]
        )
    observations: list[QuotaObservation] = []
    for index, (_key, root, logical_limit_key) in enumerate(specs):
        used = 99.0 if index == peak_index else 10.0 + (index % 7)
        for offset, (minutes, percent) in enumerate(
            ((20, used - 1.0), (5, used)), start=1,
        ):
            observations.append(_quota_observation(
                root=root, window_minutes=300,
                resets_at=now + dt.timedelta(hours=4),
                captured_at=now - dt.timedelta(minutes=minutes),
                logical_limit_key=logical_limit_key, used_percent=percent,
                line_offset=offset,
            ))
    return tuple(observations)


def test_retained_active_rows_all_have_retained_histories(tmp_path, monkeypatch):
    """#429 §4.2: histories and active rows were capped independently and in
    different orders, so above SOURCE_HISTORY_LIMIT the clock — which sees only
    retained histories — could not reproduce the build."""
    source_module = sys.modules["_cctally_dashboard_sources"]
    now = _429_NOW
    observations = _three_hundred_active_identities(now)
    state = _build_codex_state_from(
        observations, now_utc=now, tmp_path=tmp_path, monkeypatch=monkeypatch,
        data_version="429-retention-v1",
    )
    quota = state.data["quota"]
    history_keys = {str(row["key"]) for row in quota["histories"]}
    active_keys = [str(row["key"]) for row in quota["summary"]["active"]]

    assert active_keys, "precondition: the fixture publishes active rows"
    assert len(active_keys) <= source_module.SOURCE_HISTORY_LIMIT
    assert set(active_keys) <= history_keys

    clocked = refresh_codex_source_clock(state, now_utc=now)
    clocked_active = [
        str(row["key"]) for row in clocked.data["quota"]["summary"]["active"]
    ]
    assert clocked_active == active_keys      # membership AND order
    assert clocked == state


def test_summary_scalars_derive_from_retained_active_rows(tmp_path, monkeypatch):
    """#429 §4.2 step 6: latest_percent and freshness were computed pre-cap."""
    now = _429_NOW
    # Highest percentage sits on an identity that the cap discards.
    observations = _three_hundred_active_identities(now, peak_on_discarded=True)
    summary = _build_codex_state_from(
        observations, now_utc=now, tmp_path=tmp_path, monkeypatch=monkeypatch,
        data_version="429-retention-v2",
    ).data["quota"]["summary"]
    retained = summary["active"]
    assert summary["active_window_count"] == len(retained)
    assert summary["latest_percent"] == max(
        float(row["current_percent"]) for row in retained)
    assert summary["latest_percent"] != 99.0, (
        "the discarded identity's peak must not survive as a summary scalar")
    assert summary["freshness"] == "fresh"


def _build_codex_state_with_expiring_cycle_and_budget(
    tmp_path, monkeypatch, *, now_utc=NOW, data_version="429-composite-v1",
):
    """A state whose quota, hero cycle and budget all move on the same tick."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    config = {
        "collector": {"week_start": "sunday"},
        "budget": {
            "codex": {
                "amount_usd": 10.0,
                "period": "calendar-month",
                "alert_thresholds": [80, 100],
            },
        },
    }
    _install_weekly_only_cycle(
        monkeypatch, source_module,
        reset=now_utc + dt.timedelta(hours=1),
        root=_cache_root_key(cache),
        captured_at=now_utc - dt.timedelta(minutes=10),
    )
    try:
        return source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=now_utc, display_tz_name="UTC", week_start_idx=6,
                week_start_name="sunday",
                codex_budget=ns["_get_budget_config"](config)["codex"],
            ),
            data_version=data_version,
        )
    finally:
        cache.close()
        stats.close()


def test_hero_quota_tracks_the_clocked_top_level_summary(tmp_path, monkeypatch):
    """#429 §4.5: hero.quota and quota.summary are separate frozen mappings
    after publication; the clock replaced only one of them."""
    now = _429_NOW
    state = _build_codex_state_from(
        _quota_observations_with_repeated_value(now), now_utc=now,
        tmp_path=tmp_path, monkeypatch=monkeypatch, data_version="429-hero-v1",
    )
    assert state.data["hero"]["quota"]["freshness"] == "fresh", (
        "precondition: the build publishes a fresh summary to age")
    later = now + dt.timedelta(hours=2)
    clocked = refresh_codex_source_clock(state, now_utc=later)
    assert clocked.data["hero"]["quota"] == clocked.data["quota"]["summary"]
    assert clocked.data["hero"]["quota"]["freshness"] == "stale"


def test_one_tick_applies_quota_expiry_and_budget_together(tmp_path, monkeypatch):
    """#429 §4.5: three hero mutations compose on ONE hero copy; three
    independent copies would let the last write win."""
    state = _build_codex_state_with_expiring_cycle_and_budget(tmp_path, monkeypatch)
    assert state.capabilities["hero"].status == "supported"
    assert isinstance(state.data["hero"]["cycle"], Mapping)
    assert state.data["hero"]["budget"] is not None

    later = NOW + dt.timedelta(hours=2)     # past freshness AND cycle reset
    clocked = refresh_codex_source_clock(state, now_utc=later)
    hero = clocked.data["hero"]
    assert hero["cycle"] is None                  # expiry cleared it
    assert hero["cost_usd"] is None
    assert hero["budget"] == clocked.data["budget"]["status"]   # budget applied
    assert hero["quota"] == clocked.data["quota"]["summary"]    # quota applied
    assert clocked.capabilities["hero"].status == "unavailable"
    assert any(w.code == "codex_cycle_unavailable" for w in clocked.warnings)


# -------------------------------------------------------------------------
# #429 §5.6 — the non-regression oracle. Regenerated goldens accept whatever
# bytes they are given, and no single run can execute both the pre- and
# post-change code, so the comparison is against a COMMITTED pre-change
# artifact captured with `bin/` reverted to the branch point.
# -------------------------------------------------------------------------

# Lives under `tests/golden/`, NOT beside the dashboard harness fixtures: that
# directory is rebuilt IN PLACE by `bin/build-dashboard-fixtures.py` during the
# shell pool, so a pytest reader of it would race the rebuild under the #296
# overlap (`tests/test_test_all_overlap_safety.py` enforces this).
_429_PRE_CHANGE_ENVELOPE = (
    REPO_ROOT / "tests" / "golden" / "429-pre-change-source-envelope.json"
)
ALLOWED_DELTAS = {
    "captured_at", "account_key", "source_schema_version",
}
# #443 S2 and #465 Codex cache-report vocabulary changes. These deltas are
# CODEX-ONLY: the authoritative percent key, retired alias, null inapplicable
# values, metadata, and row-observation fields. Claude remains pinned.
#
# Gated on the PATH, not the leaf name: `observed` and `anomaly_unevaluated`
# ALSO exist on the Claude cache report, so allowing them by bare name would
# blind this oracle to a Claude-side change under
# `sources.all.data.providers.claude.cache_report` — which
# `test_claude_source_state_is_unchanged` does NOT cover, since it pins only
# `after["sources"]["claude"]`.
CODEX_SCOPED_DELTAS = {
    "cached_input_percent", "cache_hit_percent", "wasted_usd",
    "fourteen_day_efficiency_ratio", "not_applicable", "anomaly_predicates",
    "anomaly_unevaluated", "observed",
}


def _is_allowed_delta(path):
    segments = path.split(".")
    if segments[-1] in ALLOWED_DELTAS:
        return True
    return segments[-1] in CODEX_SCOPED_DELTAS and "codex" in segments
# NOT a wire semantic: `data_version` embeds `current_generation()`, a
# process-global counter that advances as other tests run, so the same envelope
# built twice in one pytest session carries two different tokens. It is a
# cache-invalidation key, never rendered, and is excluded from BOTH comparisons.
VOLATILE_FIELDS = {"data_version"}


def _capture_undecorated_codex_envelope(tmp_path, monkeypatch):
    """The full, undecorated source envelope over the #429 repeated-value fixture.

    The fixture must REPEAT a logical value, otherwise the interpreted baseline
    and the latest physical capture coincide and the artifact cannot witness the
    change at all.

    Deliberately does NOT sync the Codex corpus. Every Codex resource key is
    derived from `source_root_key`, which `_seeded_context` roots at the
    per-test `tmp_path` — so a corpus-backed capture re-salts every
    `quota:` / `session:` / `project:` key and its `data_version` on each run,
    and could never be committed as a stable oracle. The observations carry a
    literal root instead, which keeps every emitted key reproducible.
    """
    import json as _json

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    # `redirect_paths` moves HOME (so `~/.codex` is covered) but leaves
    # $CODEX_HOME, and `resolve_dashboard_source_semantics` calls
    # `_resolve_codex_speed("auto")`, which walks $CODEX_HOME roots' config.toml
    # and can flip `speed` to `fast` — which feeds cost. A committed oracle must
    # not depend on the developer's environment.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    tui = sys.modules["_cctally_tui"]
    source_module = sys.modules["_cctally_dashboard_sources"]
    _install_observations(
        monkeypatch, source_module, _quota_observations_with_repeated_value(NOW),
    )
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        bundle = _build_tick_bundle(tui, stats, now_utc=NOW)
        envelope = sys.modules[
            "_cctally_dashboard_envelope"
        ]._source_bundle_to_envelope(bundle)
        return _json.loads(_json.dumps(envelope, sort_keys=True, default=str))
    finally:
        cache.close()
        stats.close()


def _deep_diff(before, after, path=""):
    """Yield ``(path, old, new)`` for every leaf that differs."""
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}" if path else str(key)
            if key not in before:
                yield (child, None, after[key])
            elif key not in after:
                yield (child, before[key], None)
            else:
                yield from _deep_diff(before[key], after[key], child)
    elif isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child = f"{path}[{index}]"
            if index >= len(before):
                yield (child, None, after[index])
            elif index >= len(after):
                yield (child, before[index], None)
            else:
                yield from _deep_diff(before[index], after[index], child)
    elif before != after:
        yield (path, before, after)


def test_undecorated_envelope_changes_only_in_allowed_fields(tmp_path, monkeypatch):
    """#429 §5.6: goldens accept whatever bytes they are given. This compares
    against a committed PRE-change artifact and allows exactly three fields."""
    import json as _json

    before = _json.loads(_429_PRE_CHANGE_ENVELOPE.read_text())
    after = _capture_undecorated_codex_envelope(tmp_path, monkeypatch)
    diffs = [
        (path, old, new) for path, old, new in _deep_diff(before, after)
        if path.split(".")[-1] not in VOLATILE_FIELDS
    ]
    unexpected = [
        (path, old, new) for path, old, new in diffs
        if not _is_allowed_delta(path)
    ]
    assert unexpected == [], f"unintended envelope changes: {unexpected}"
    assert any(p.endswith("captured_at") for p, _, _ in diffs), (
        "fixture does not exercise the change at all")
    assert not any(p.endswith("account_key") for p, _, _ in diffs), (
        "R8: an undecorated install must gain no account_key")


def test_claude_source_state_is_unchanged(tmp_path, monkeypatch):
    """#429 AC4: the Claude side is not in scope and must be bit-identical."""
    import json as _json

    before = _json.loads(_429_PRE_CHANGE_ENVELOPE.read_text())
    after = _capture_undecorated_codex_envelope(tmp_path, monkeypatch)
    strip = lambda entry: {
        key: value for key, value in entry.items() if key not in VOLATILE_FIELDS
    }
    assert strip(after["sources"]["claude"]) == strip(before["sources"]["claude"])
