"""Provider-native Codex dashboard read model contracts for #294 S4."""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import shutil
import sys
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
) -> QuotaObservation:
    return QuotaObservation(
        identity=QuotaWindowIdentity(
            source="codex",
            source_root_key=root,
            logical_limit_key=logical_limit_key,
            observed_slot=observed_slot,
            window_minutes=window_minutes,
            limit_name=limit_name,
        ),
        captured_at=captured_at,
        used_percent=used_percent,
        resets_at=resets_at,
        source_path=f"/private/{root}.jsonl",
        line_offset=1,
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
    assert report["days"][0]["cache_hit_percent"] == pytest.approx(80.0)
    assert report["days"][0]["saved_usd"] == pytest.approx(
        80 * (1.25e-6 - 1.25e-7)
    )
    assert report["days"][0]["net_usd"] == report["days"][0]["saved_usd"]
    assert report["days"][0]["wasted_usd"] == 0.0
    assert report["days"][0]["cache_creation_tokens"] == 0
    assert report["fourteen_day_counterfactual_usd"] == report["days"][0]["saved_usd"]
    assert report["fourteen_day_efficiency_ratio"] == 1.0
    assert report["by_project"][0]["key"] == "cctally-dev"
    assert report["by_model"][0]["key"] == "gpt-5"


def test_codex_cycle_selects_the_active_seven_day_boundary_over_five_hour_limit():
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)

    cycle = source_module._resolve_codex_weekly_cycle((
        _quota_observation(root="root", window_minutes=300, resets_at=NOW + dt.timedelta(hours=4)),
        _quota_observation(root="root", window_minutes=10_080, resets_at=reset),
    ), NOW)

    assert cycle.window_minutes == 10_080
    assert cycle.start_at == reset - dt.timedelta(days=7)
    assert cycle.resets_at == reset


def test_codex_cycle_allows_a_fresh_weekly_boundary_without_a_five_hour_window():
    source_module = sys.modules["_cctally_dashboard_sources"]
    reset = NOW + dt.timedelta(days=2)

    cycle = source_module._resolve_codex_weekly_cycle((
        _quota_observation(root="root", window_minutes=10_080, resets_at=reset),
    ), NOW)

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
    ), NOW)

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


def test_codex_cycle_rejects_stale_weekly_evidence_even_before_its_reset():
    source_module = sys.modules["_cctally_dashboard_sources"]

    with pytest.raises(source_module.CodexCycleUnavailable, match="stale"):
        source_module._resolve_codex_weekly_cycle((
            _quota_observation(
                root="root",
                window_minutes=10_080,
                resets_at=NOW + dt.timedelta(days=2),
                captured_at=NOW - dt.timedelta(hours=2),
            ),
        ), NOW)


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
    ), NOW)

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


def test_stale_weekly_baseline_builds_a_stale_partial_generation_without_a_hero(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        root_key = str(cache.execute(
            "SELECT source_root_key FROM codex_session_entries ORDER BY id LIMIT 1"
        ).fetchone()[0])
        monkeypatch.setattr(
            source_module,
            "load_codex_quota_observations",
            lambda **_kwargs: (
                _quota_observation(
                    root=root_key,
                    window_minutes=10_080,
                    resets_at=NOW + dt.timedelta(days=2),
                    captured_at=NOW - dt.timedelta(hours=2),
                ),
            ),
        )

        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="stale-cycle-v1",
        )

        assert state.availability == "partial"
        assert state.freshness == "stale"
        assert state.data["quota"]["summary"]["freshness"] == "stale"
        assert state.capabilities["hero"].status == "unavailable"
        assert state.data["hero"]["cycle"] is None
        assert state.data["hero"]["total_tokens"] is None
        assert state.warnings[-1].code == "codex_cycle_unavailable"
        assert state.data["periods"]["daily"]["rows"]
    finally:
        cache.close()
        stats.close()


def test_idle_clock_stale_weekly_evidence_withdraws_the_hero_without_a_cache_read(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        root_key = str(cache.execute(
            "SELECT source_root_key FROM codex_session_entries ORDER BY id LIMIT 1"
        ).fetchone()[0])
        _install_active_native_cycle(
            monkeypatch, source_module, reset=NOW + dt.timedelta(days=2), root=root_key,
        )
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="clock-cycle-v1",
        )
        assert state.capabilities["hero"].status == "supported"
        before_rows = cache.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0]

        monkeypatch.setattr(
            source_module,
            "load_codex_quota_observations",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("idle clock must not read cache")),
        )
        refreshed = source_module.refresh_codex_source_clock(
            state, now_utc=NOW + dt.timedelta(hours=2),
        )

        assert cache.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == before_rows
        assert refreshed.availability == "partial"
        assert refreshed.freshness == "stale"
        assert refreshed.capabilities["hero"].status == "unavailable"
        assert refreshed.data["hero"]["cycle"] is None
        assert refreshed.data["hero"]["total_tokens"] is None
        assert any(warning.code == "codex_cycle_unavailable" for warning in refreshed.warnings)
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


def test_codex_session_name_uses_persisted_short_name_not_prompt_title(
    tmp_path, monkeypatch,
):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    provider_root = tmp_path / "provider"
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
            (native_thread_id, "Fix dashboard cycle UI"),
        )
        state_db.commit()
    finally:
        state_db.close()

    try:
        metadata = source_module._codex_conversation_metadata(cache)
        assert "Fix dashboard cycle UI" in {
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

        labels = [row["label"] for row in state.data["sessions"]["rows"]]
        assert "Fix dashboard cycle UI" in labels, (labels, metadata)
        assert "beginning of the user's prompt" not in repr(state.data["sessions"])
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
        )

        assert len(quota["histories"]) <= source_module.SOURCE_HISTORY_LIMIT
        assert len(quota["summary"]["active"]) <= source_module.SOURCE_HISTORY_LIMIT
        assert len(quota["milestones"]) <= source_module.SOURCE_HISTORY_LIMIT
    finally:
        cache.close()
        stats.close()


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
            source_path TEXT, line_offset INTEGER, orphaned_at TEXT
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
                "('codex', 'root-a', 'weekly', 'primary', 10080, ?, ?, ?, ?, ?, NULL)",
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
