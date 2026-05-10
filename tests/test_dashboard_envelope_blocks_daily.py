"""Golden-file freeze of the SSE envelope shape with blocks/daily keys."""
import datetime as dt
import json
import pathlib

import pytest
from conftest import load_script

GOLDEN = pathlib.Path(__file__).parent / "golden" / "dashboard_envelope_with_blocks_daily.json"


@pytest.fixture(autouse=True)
def _pin_tz_etc_utc(monkeypatch):
    """Pin TZ=Etc/UTC for every test in this module so the envelope's
    `display` block (offset_label / offset_seconds / resolved_tz) matches
    the golden regardless of host timezone. Per CLAUDE.md: use Etc/UTC,
    never bare UTC, because _local_tz_name() falls back to host-local for
    non-IANA strings.

    Also pin the envelope's update-mirror block to a deterministic shape:
    the production loaders read ~/.local/share/cctally/update-state.json
    (real path), which on a developer machine carries live wall-clock
    state. Patching here keeps the full-envelope golden stable."""
    monkeypatch.setenv("TZ", "Etc/UTC")
    import time as _time
    _time.tzset()


def _pin_update_envelope_loaders(ns):
    """Replace ``_load_update_state`` / ``_load_update_suppress`` in the
    per-test namespace with deterministic stubs. Production reads from
    ~/.local/share/cctally/update-state.json (live wall-clock state on
    a developer machine), which would leak into the full-envelope
    golden assertion otherwise. Each ``load_script()`` returns a fresh
    namespace, so the override has to be done per test rather than via
    a module-scoped autouse fixture."""
    ns["_load_update_state"] = lambda: None
    ns["_load_update_suppress"] = lambda: {
        "skipped_versions": [],
        "remind_after": None,
    }


def _make_snapshot(ns):
    _pin_update_envelope_loaders(ns)
    DataSnapshot = ns["DataSnapshot"]
    BlocksPanelRow = ns["BlocksPanelRow"]
    DailyPanelRow = ns["DailyPanelRow"]
    return DataSnapshot(
        current_week=None,
        forecast=None,
        trend=[],
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=dt.datetime(2026, 4, 26, 12, 0, tzinfo=dt.timezone.utc),
        percent_milestones=[],
        weekly_history=[],
        weekly_periods=[],
        monthly_periods=[],
        blocks_panel=[
            BlocksPanelRow(
                start_at="2026-04-26T14:00:00+00:00",
                end_at="2026-04-26T19:00:00+00:00",
                anchor="recorded",
                is_active=True,
                cost_usd=4.21,
                models=[
                    {"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
                     "chip": "opus", "cost_usd": 3.28, "cost_pct": 78.0},
                ],
                label="14:00 Apr 26",
            ),
        ],
        daily_panel=[
            DailyPanelRow(
                date="2026-04-26",
                label="04-26",
                cost_usd=8.40,
                is_today=True,
                # Pre-set value matches what _compute_intensity_buckets emits
                # for a single non-zero day: dedup → [8.40], bisect_right → 1.
                # The envelope re-runs the helper on snap.daily_panel for
                # threshold-vs-bucket consistency, so this stays in sync
                # with the post-mutation row state.
                intensity_bucket=1,
                models=[
                    {"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
                     "chip": "opus", "cost_usd": 5.20, "cost_pct": 62.0},
                ],
                # v2.3 fields
                input_tokens=412_000,
                output_tokens=38_400,
                cache_creation_tokens=1_200_000,
                cache_read_tokens=8_300_000,
                total_tokens=9_950_400,
                cache_hit_pct=87.3,
            ),
            DailyPanelRow(
                date="2026-04-25",
                label="04-25",
                cost_usd=0.0,
                is_today=False,
                intensity_bucket=0,
                models=[],
                # v2.3: zero-day tokens default to 0; cache_hit_pct null
            ),
        ],
    )


def test_envelope_blocks_daily_keys_match_golden():
    ns = load_script()
    snap = _make_snapshot(ns)
    env = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 4, 26, 12, 0, tzinfo=dt.timezone.utc),
        monotonic_now=None,
    )
    assert "blocks" in env
    assert "daily" in env
    # Inline expected shape (minus quantile_thresholds — see test_envelope_quantile_thresholds_consistent_with_helper).
    assert env["blocks"] == {"rows": [
        {
            "start_at": "2026-04-26T14:00:00+00:00",
            "end_at":   "2026-04-26T19:00:00+00:00",
            "anchor":   "recorded",
            "is_active": True,
            "cost_usd": 4.21,
            "models": [
                {"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
                 "chip": "opus", "cost_usd": 3.28, "cost_pct": 78.0},
            ],
            "label": "14:00 Apr 26",
        },
    ]}
    assert env["daily"]["rows"] == [
        {
            "date": "2026-04-26",
            "label": "04-26",
            "cost_usd": 8.40,
            "is_today": True,
            # See _make_snapshot comment: helper collapses single non-zero
            # day to bucket 1 (not bucket 5).
            "intensity_bucket": 1,
            "models": [
                {"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
                 "chip": "opus", "cost_usd": 5.20, "cost_pct": 62.0},
            ],
            # v2.3 token + cache rollup
            "input_tokens": 412_000,
            "output_tokens": 38_400,
            "cache_creation_tokens": 1_200_000,
            "cache_read_tokens": 8_300_000,
            "total_tokens": 9_950_400,
            "cache_hit_pct": 87.3,
        },
        {
            "date": "2026-04-25",
            "label": "04-25",
            "cost_usd": 0.0,
            "is_today": False,
            "intensity_bucket": 0,
            "models": [],
            # v2.3: zero-day tokens are zero; cache_hit_pct null
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
            "cache_hit_pct": None,
        },
    ]
    assert env["daily"]["peak"] == {"date": "2026-04-26", "cost_usd": 8.40}
    # quantile_thresholds: 1 non-zero day, so the helper-shipped algorithm
    # returns a 5-element list (raw, possibly duplicated).
    assert isinstance(env["daily"]["quantile_thresholds"], list)
    assert len(env["daily"]["quantile_thresholds"]) == 5


def test_envelope_quantile_thresholds_consistent_with_helper():
    """Envelope thresholds MUST equal the helper's return — not an
    independent re-derivation. Otherwise dedup vs raw thresholds diverge."""
    ns = load_script()
    snap = _make_snapshot(ns)
    env = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 4, 26, 12, 0, tzinfo=dt.timezone.utc),
        monotonic_now=None,
    )
    expected_thresholds = ns["_compute_intensity_buckets"](list(snap.daily_panel))
    assert env["daily"]["quantile_thresholds"] == expected_thresholds


def test_envelope_blocks_daily_full_golden_diff():
    """Full envelope-shape freeze. Update the golden JSON by hand when
    adding fields intentionally; CI diff will surface unintended drift."""
    ns = load_script()
    snap = _make_snapshot(ns)
    env = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 4, 26, 12, 0, tzinfo=dt.timezone.utc),
        monotonic_now=None,
    )
    expected = json.loads(GOLDEN.read_text())
    assert env == expected, (
        f"Envelope drift detected. If intentional, update {GOLDEN}.\n"
        f"Got:      {json.dumps(env, indent=2, sort_keys=True)}\n"
        f"Expected: {json.dumps(expected, indent=2, sort_keys=True)}"
    )
