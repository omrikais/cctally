"""Deterministic contracts for the provider-neutral quota interpretation kernel."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_quota as quota  # noqa: E402
from conftest import load_script  # noqa: E402


UTC = dt.timezone.utc
AS_OF = dt.datetime(2026, 7, 15, 8, 30, tzinfo=UTC)


def _identity(
    *,
    root: str = "root-a",
    logical_limit_key: str = "limit-primary",
    observed_slot: str = "primary",
    window_minutes: int = 120,
    limit_id: str | None = "limit-1",
    limit_name: str | None = "Synthetic limit",
) -> quota.QuotaWindowIdentity:
    return quota.QuotaWindowIdentity(
        source="codex",
        source_root_key=root,
        logical_limit_key=logical_limit_key,
        observed_slot=observed_slot,
        window_minutes=window_minutes,
        limit_id=limit_id,
        limit_name=limit_name,
    )


def _observation(
    *,
    identity: quota.QuotaWindowIdentity | None = None,
    captured_at: dt.datetime = AS_OF - dt.timedelta(minutes=30),
    used_percent: float = 10.0,
    resets_at: dt.datetime = AS_OF + dt.timedelta(minutes=30),
    source_path: str = "/synthetic/root-a/rollout.jsonl",
    line_offset: int = 0,
    plan_type: str | None = "pro",
    individual_limit_json: str | None = '{"remaining":90}',
    reached_type: str | None = None,
) -> quota.QuotaObservation:
    return quota.QuotaObservation(
        identity=identity or _identity(),
        captured_at=captured_at,
        used_percent=used_percent,
        resets_at=resets_at,
        source_path=source_path,
        line_offset=line_offset,
        plan_type=plan_type,
        individual_limit_json=individual_limit_json,
        reached_type=reached_type,
    )


def test_identity_is_frozen_and_display_labels_do_not_change_logical_identity():
    original = _identity(limit_name="Original name")
    renamed = _identity(limit_name="Renamed display label")

    assert dataclasses.is_dataclass(original)
    assert original == renamed
    assert hash(original) == hash(renamed)
    with pytest.raises(dataclasses.FrozenInstanceError):
        original.limit_name = "mutated"  # type: ignore[misc]


def test_history_keeps_non_adjacent_exact_values_in_total_physical_order():
    reset = AS_OF + dt.timedelta(minutes=90)
    exact_later = _observation(
        captured_at=AS_OF - dt.timedelta(minutes=20),
        resets_at=reset,
        source_path="/synthetic/z.jsonl",
        line_offset=80,
    )
    exact_earlier = _observation(
        captured_at=AS_OF - dt.timedelta(minutes=20),
        resets_at=reset,
        source_path="/synthetic/a.jsonl",
        line_offset=40,
    )
    metadata_changed = _observation(
        identity=_identity(limit_name="Metadata changed"),
        captured_at=AS_OF - dt.timedelta(minutes=20),
        resets_at=reset,
        source_path="/synthetic/b.jsonl",
        line_offset=20,
    )

    history = quota.build_history((exact_later, metadata_changed, exact_earlier))

    assert len(history) == 1
    assert history[0].physical_observations == (exact_earlier, metadata_changed, exact_later)
    assert history[0].observations == (exact_earlier, metadata_changed, exact_later)
    assert quota.logical_value_tuple(exact_earlier) == quota.logical_value_tuple(exact_later)
    assert quota.logical_value_tuple(exact_earlier) != quota.logical_value_tuple(metadata_changed)


def test_physical_total_order_uses_reset_then_line_offset_tie_breakers_for_duplicates():
    captured_at = AS_OF - dt.timedelta(minutes=20)
    first_reset = AS_OF + dt.timedelta(minutes=60)
    later_reset = first_reset + dt.timedelta(seconds=1)
    reset_history = quota.build_history((
        _observation(
            captured_at=captured_at,
            resets_at=later_reset,
            source_path="/synthetic/same.jsonl",
            line_offset=9,
        ),
        _observation(
            captured_at=captured_at,
            resets_at=first_reset,
            source_path="/synthetic/same.jsonl",
            line_offset=9,
        ),
    ))
    lower_offset = _observation(
        captured_at=captured_at,
        resets_at=first_reset,
        source_path="/synthetic/offset-only.jsonl",
        line_offset=4,
    )
    higher_offset = _observation(
        captured_at=captured_at,
        resets_at=first_reset,
        source_path="/synthetic/offset-only.jsonl",
        line_offset=5,
    )
    offset_history = quota.build_history((higher_offset, lower_offset))

    assert reset_history[0].physical_observations[0].resets_at == first_reset
    assert reset_history[0].physical_observations[1].resets_at == later_reset
    assert offset_history[0].physical_observations == (lower_offset, higher_offset)
    assert offset_history[0].observations == (lower_offset,)


def test_out_of_order_captures_use_capture_time_for_history_and_baseline_selection():
    reset = AS_OF + dt.timedelta(minutes=60)
    earliest_duplicate = _observation(
        captured_at=AS_OF - dt.timedelta(minutes=20),
        used_percent=20,
        resets_at=reset,
        source_path="/synthetic/z-earliest.jsonl",
        line_offset=90,
    )
    later_duplicate = _observation(
        captured_at=AS_OF - dt.timedelta(minutes=10),
        used_percent=20,
        resets_at=reset,
        source_path="/synthetic/a-later.jsonl",
        line_offset=0,
    )
    latest = _observation(
        captured_at=AS_OF - dt.timedelta(minutes=5),
        used_percent=30,
        resets_at=reset,
        source_path="/synthetic/0-latest.jsonl",
        line_offset=0,
    )

    history = quota.build_history((latest, later_duplicate, earliest_duplicate))

    assert history[0].physical_observations == (
        earliest_duplicate,
        later_duplicate,
        latest,
    )
    assert history[0].observations == (earliest_duplicate, latest)
    assert quota.select_baseline(
        history[0].physical_observations,
        AS_OF - dt.timedelta(minutes=6),
    ) == later_duplicate
    assert quota.select_baseline(history[0].observations, AS_OF) == latest


def test_history_never_blends_same_looking_windows_across_roots_or_limits():
    shared_reset = AS_OF + dt.timedelta(minutes=45)
    observations = (
        _observation(resets_at=shared_reset),
        _observation(identity=_identity(root="root-b"), resets_at=shared_reset),
        _observation(
            identity=_identity(logical_limit_key="limit-secondary", observed_slot="secondary"),
            resets_at=shared_reset,
        ),
    )

    history = quota.build_history(observations)
    blocks = quota.build_blocks(observations)

    assert len(history) == 3
    assert len(blocks) == 3
    assert {(block.identity.source_root_key, block.identity.logical_limit_key) for block in blocks} == {
        ("root-a", "limit-primary"),
        ("root-b", "limit-primary"),
        ("root-a", "limit-secondary"),
    }


def test_single_window_selectors_reject_mixed_identity_input_instead_of_blending_it():
    observations = (
        _observation(captured_at=AS_OF - dt.timedelta(minutes=10)),
        _observation(
            identity=_identity(root="root-b"),
            captured_at=AS_OF - dt.timedelta(minutes=5),
        ),
    )

    with pytest.raises(ValueError, match="exactly one quota identity"):
        quota.select_baseline(observations, AS_OF)
    with pytest.raises(ValueError, match="exactly one quota identity"):
        quota.quota_freshness(observations, AS_OF)


def test_reset_blocks_and_percent_milestones_use_first_observation_hwm_and_recovery_rules():
    identity = _identity()
    reset_a = AS_OF + dt.timedelta(minutes=90)
    reset_b = reset_a + dt.timedelta(hours=2)
    observations = (
        _observation(identity=identity, captured_at=AS_OF - dt.timedelta(minutes=30), used_percent=20, resets_at=reset_a),
        _observation(identity=identity, captured_at=AS_OF - dt.timedelta(minutes=20), used_percent=25, resets_at=reset_a, line_offset=1),
        _observation(identity=identity, captured_at=AS_OF - dt.timedelta(minutes=15), used_percent=22, resets_at=reset_a, line_offset=2),
        _observation(identity=identity, captured_at=AS_OF - dt.timedelta(minutes=10), used_percent=25, resets_at=reset_a, line_offset=3),
        _observation(identity=identity, captured_at=AS_OF - dt.timedelta(minutes=5), used_percent=27, resets_at=reset_a, line_offset=4),
        _observation(identity=identity, captured_at=AS_OF, used_percent=7, resets_at=reset_b, line_offset=5),
    )

    blocks = quota.build_blocks(observations)
    first, second = blocks
    milestones = quota.percent_milestones(first)

    assert first.nominal_start_at == reset_a - dt.timedelta(minutes=120)
    assert first.first_percent == 20
    assert first.current_percent == 27
    assert second.first_percent == second.current_percent == 7
    assert [milestone.percent for milestone in milestones] == list(range(21, 28))
    assert all(milestone.captured_at == observations[1].captured_at for milestone in milestones[:5])
    assert all(milestone.captured_at == observations[4].captured_at for milestone in milestones[5:])
    assert quota.percent_milestones(second) == ()


def test_source_path_key_is_prefixed_sha256_of_the_canonical_absolute_path():
    source_path = "/synthetic/root-a/sessions/2026/rollout.jsonl"
    expected = hashlib.sha256(
        b"cctally-source-path-v1\0" + source_path.encode("utf-8")
    ).hexdigest()[:32]

    assert quota.source_path_key(source_path) == expected
    assert quota.source_path_key(source_path) == expected


def test_freshness_uses_latest_physical_capture_and_native_duration_with_future_skew():
    fresh = _observation(captured_at=AS_OF - dt.timedelta(seconds=900))
    stale = _observation(captured_at=AS_OF - dt.timedelta(seconds=901))
    future = _observation(captured_at=AS_OF + dt.timedelta(seconds=301))
    within_skew = _observation(captured_at=AS_OF + dt.timedelta(seconds=60))

    assert quota.stale_after_seconds(120) == 900
    assert quota.quota_freshness((fresh,), AS_OF) == quota.QuotaFreshness(
        state="fresh", captured_at=fresh.captured_at, age_seconds=900, stale_after_seconds=900,
    )
    assert quota.quota_freshness((stale,), AS_OF).state == "stale"
    assert quota.quota_freshness((future,), AS_OF).state == "future"
    assert quota.quota_freshness((within_skew,), AS_OF) == quota.QuotaFreshness(
        state="fresh", captured_at=within_skew.captured_at, age_seconds=-60, stale_after_seconds=900,
    )
    assert quota.quota_freshness((), AS_OF) == quota.QuotaFreshness(
        state="unavailable", captured_at=None, age_seconds=None, stale_after_seconds=None,
    )


@pytest.mark.parametrize(
    ("captures", "expected_confidence", "expected_samples", "expected_span", "expected_projected"),
    (
        (((0, 10), (450, 20), (900, 30), (1350, 40), (1800, 50)), "high", 4, 1800, 100),
        (((0, 10), (480, 20), (900, 30)), "medium", 2, 900, 100),
        (((0, 10), (1800, 20)), "low", 1, 1800, 40),
    ),
)
def test_forecast_confidence_boundaries_and_exact_rate_vectors(
    captures: tuple[tuple[int, float], ...],
    expected_confidence: str,
    expected_samples: int,
    expected_span: int,
    expected_projected: float,
):
    reset = AS_OF + dt.timedelta(hours=1)
    start = AS_OF - dt.timedelta(minutes=30)
    observations = tuple(
        _observation(captured_at=start + dt.timedelta(seconds=seconds), used_percent=percent, resets_at=reset, line_offset=index)
        for index, (seconds, percent) in enumerate(captures)
    )

    result = quota.forecast_quota(observations, AS_OF)

    assert result.status == "ok"
    assert result.current_percent == captures[-1][1]
    assert result.rate_percent_per_hour == pytest.approx((captures[-1][1] - captures[0][1]) / (expected_span / 3600))
    assert result.projected_percent == expected_projected
    assert result.sample_count == expected_samples
    assert result.sample_span_seconds == expected_span
    assert result.confidence == expected_confidence


def test_forecast_ignores_decreases_and_requires_a_usable_positive_interval():
    reset = AS_OF + dt.timedelta(hours=1)
    insufficient = (_observation(captured_at=AS_OF - dt.timedelta(minutes=5), used_percent=40, resets_at=reset),)
    decrease_then_recovery = (
        _observation(captured_at=AS_OF - dt.timedelta(minutes=30), used_percent=50, resets_at=reset),
        _observation(captured_at=AS_OF - dt.timedelta(minutes=20), used_percent=40, resets_at=reset, line_offset=1),
        _observation(captured_at=AS_OF - dt.timedelta(minutes=10), used_percent=45, resets_at=reset, line_offset=2),
    )

    assert quota.forecast_quota(insufficient, AS_OF) == quota.QuotaForecast(
        status="insufficient-history", current_percent=40, rate_percent_per_hour=None,
        projected_percent=None, resets_at=reset, remaining_seconds=3600,
        sample_count=0, sample_span_seconds=0, confidence=None,
    )
    result = quota.forecast_quota(decrease_then_recovery, AS_OF)
    assert result.sample_count == 1
    assert result.sample_span_seconds == 600
    assert result.rate_percent_per_hour == pytest.approx(30.0)
    assert result.confidence == "low"


def test_non_adjacent_recovery_value_remains_history_and_a_forecast_interval():
    reset = AS_OF + dt.timedelta(hours=1)
    observations = tuple(
        _observation(
            captured_at=AS_OF - dt.timedelta(minutes=40 - index * 10),
            used_percent=percent,
            resets_at=reset,
            line_offset=index,
        )
        for index, percent in enumerate((50, 40, 50, 45))
    )

    history = quota.build_history(observations)[0]
    block = quota.build_blocks(observations)[0]
    forecast = quota.forecast_quota(observations, AS_OF)

    assert [point.used_percent for point in history.observations] == [50, 40, 50, 45]
    assert [point.used_percent for point in block.observations] == [50, 40, 50, 45]
    assert block.current_percent == 45
    assert forecast.current_percent == 45
    assert forecast.sample_count == 1
    assert forecast.sample_span_seconds == 600
    assert forecast.rate_percent_per_hour == pytest.approx(60.0)


def test_repeated_value_at_end_remains_the_current_observation_after_a_decrease():
    reset = AS_OF + dt.timedelta(hours=1)
    observations = tuple(
        _observation(
            captured_at=AS_OF - dt.timedelta(minutes=40 - index * 10),
            used_percent=percent,
            resets_at=reset,
            line_offset=index,
        )
        for index, percent in enumerate((20, 25, 22, 25))
    )

    history = quota.build_history(observations)[0]
    block = quota.build_blocks(observations)[0]
    forecast = quota.forecast_quota(observations, AS_OF)

    assert [point.used_percent for point in history.observations] == [20, 25, 22, 25]
    assert block.current_percent == 25
    assert [milestone.percent for milestone in quota.percent_milestones(block)] == [
        21, 22, 23, 24, 25,
    ]
    assert forecast.current_percent == 25
    assert forecast.sample_count == 2
    assert forecast.sample_span_seconds == 1200
    assert forecast.rate_percent_per_hour == pytest.approx(24.0)


def test_forecast_future_and_stale_precedence_preserve_only_eligible_baselines():
    reset = AS_OF + dt.timedelta(hours=1)
    only_future = (_observation(captured_at=AS_OF + dt.timedelta(seconds=301), used_percent=70, resets_at=reset),)
    prior_plus_future = (
        _observation(captured_at=AS_OF - dt.timedelta(minutes=10), used_percent=30, resets_at=reset),
        _observation(captured_at=AS_OF + dt.timedelta(seconds=301), used_percent=70, resets_at=reset, line_offset=1),
    )
    only_within_skew = (_observation(captured_at=AS_OF + dt.timedelta(seconds=60), used_percent=70, resets_at=reset),)
    stale = (_observation(captured_at=AS_OF - dt.timedelta(seconds=901), used_percent=30, resets_at=reset),)

    assert quota.forecast_quota(only_future, AS_OF) == quota.QuotaForecast(
        status="future", current_percent=None, rate_percent_per_hour=None,
        projected_percent=None, resets_at=None, remaining_seconds=None,
        sample_count=0, sample_span_seconds=0, confidence=None,
    )
    prior = quota.forecast_quota(prior_plus_future, AS_OF)
    assert prior.status == "future"
    assert prior.current_percent == 30
    assert prior.resets_at == reset
    assert prior.sample_count == 0
    assert quota.quota_freshness(only_within_skew, AS_OF).state == "fresh"
    assert quota.forecast_quota(only_within_skew, AS_OF).status == "unavailable"
    assert quota.forecast_quota(stale, AS_OF).status == "stale"


def test_resolved_rules_validate_thresholds_and_hash_exact_canonical_bytes_without_labels():
    identity = _identity(limit_name="Initial label")
    override = quota.QuotaRule(
        source="codex", source_root_key="root-a", logical_limit_key="limit-primary",
        actual_thresholds=(80, 90), projected_thresholds=(95,),
    )
    resolved = quota.resolve_quota_rule(
        identity,
        default_actual_thresholds=(90, 95),
        default_projected_thresholds=(),
        rules=(override,),
    )
    expected_bytes = json.dumps(
        {
            "actualThresholds": [80, 90],
            "globalEnabled": True,
            "identity": {
                "logicalLimitKey": "limit-primary",
                "source": "codex",
                "sourceRootKey": "root-a",
            },
            "projectedThresholds": [95],
            "quotaEnabled": True,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    expected = hashlib.sha256(b"cctally-quota-alert-rule-v1\0" + expected_bytes).hexdigest()

    assert resolved.actual_thresholds == (80, 90)
    assert resolved.projected_thresholds == (95,)
    assert quota.quota_rule_fingerprint(identity, resolved, global_enabled=True, quota_enabled=True) == expected
    assert quota.quota_rule_fingerprint(
        _identity(limit_name="Renamed label"), resolved, global_enabled=True, quota_enabled=True,
    ) == expected
    with pytest.raises(ValueError, match="ordered unique"):
        quota.validate_thresholds((95, 90))
    with pytest.raises(ValueError, match="ordered unique"):
        quota.validate_thresholds((90, 90))
    with pytest.raises(ValueError, match="1 through 100"):
        quota.validate_thresholds((0,))


def test_threshold_decisions_are_provider_neutral_and_never_cross_identity_boundaries():
    decisions = quota.quota_threshold_decisions(
        current_percent=94,
        projected_percent=98,
        actual_thresholds=(90, 95),
        projected_thresholds=(95, 97),
    )

    assert decisions == (
        quota.QuotaThresholdDecision(kind="actual", threshold=90),
        quota.QuotaThresholdDecision(kind="projected", threshold=95),
        quota.QuotaThresholdDecision(kind="projected", threshold=97),
    )


def test_cctally_reexports_the_pure_quota_kernel_surface():
    ns = load_script()

    assert ns["QuotaWindowIdentity"] is quota.QuotaWindowIdentity
    assert ns["forecast_quota"] is quota.forecast_quota
