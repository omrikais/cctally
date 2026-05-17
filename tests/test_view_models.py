"""View-model unit tests.

Per spec §8.1 — one TestClass per domain. Fixtures use in-line
UsageEntry / ClaudeSessionUsage builders; weekly/trend tests use
in-memory SQLite.

Spec: docs/superpowers/specs/2026-05-17-view-model-unification-design.md
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def _load_view_models():
    """Pre-load cctally + late-load _lib_view_models via importlib.

    The dashboard sibling's ``_model_breakdowns_to_models`` (called
    transitively by ``build_daily_view`` / ``build_monthly_view`` /
    ``build_weekly_view``) routes through ``sys.modules['cctally']``
    for ``_short_model_name`` / ``_chip_for_model``. Under pytest we
    must pre-load cctally so the shim resolves cleanly. ``load_script``
    is the canonical helper.
    """
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    from conftest import load_script  # noqa: WPS433
    load_script()
    # Drop any cached _lib_view_models so the late-import resolves
    # cleanly against the fresh sibling graph.
    sys.modules.pop("_lib_view_models", None)
    import _lib_view_models  # noqa: WPS433
    return _lib_view_models


@pytest.fixture
def vm():
    return _load_view_models()


def _now():
    return dt.datetime(2026, 5, 17, 12, 0, 0, tzinfo=dt.timezone.utc)


class TestDailyView:
    def test_empty_entries_returns_empty_view(self, vm):
        view = vm.build_daily_view([], now_utc=_now(), display_tz=None)
        assert view.rows == ()
        assert view.aggregated == ()
        assert view.total_cost_usd == 0.0
        assert view.total_tokens == 0
        assert view.display_tz_label  # non-empty string

    def test_single_day_aggregates_cost_and_tokens(self, vm):
        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        from _lib_aggregators import UsageEntry  # noqa: WPS433

        ts = dt.datetime(2026, 5, 16, 14, 0, 0, tzinfo=dt.timezone.utc)
        entries = [
            UsageEntry(
                timestamp=ts,
                model="claude-opus-4-5",
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 200,
                },
                cost_usd=0.05,
            ),
        ]
        view = vm.build_daily_view(entries, now_utc=_now(), display_tz=None)
        assert len(view.rows) == 1
        assert view.rows[0].date == "2026-05-16"
        assert view.total_cost_usd == pytest.approx(0.05, abs=1e-9)
        # is_today=False because now_utc is 2026-05-17 and the entry is 2026-05-16
        assert view.rows[0].is_today is False
        # aggregated parallel: BucketUsage's `bucket` matches the date string
        assert view.aggregated[0].bucket == "2026-05-16"

    def test_does_not_materialize_gap_days(self, vm):
        """Per spec §5.1: builder is gap-free. Gap-fill is the dashboard
        envelope adapter's job."""
        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        from _lib_aggregators import UsageEntry  # noqa: WPS433

        ts1 = dt.datetime(2026, 5, 10, 12, 0, 0, tzinfo=dt.timezone.utc)
        ts2 = dt.datetime(2026, 5, 16, 12, 0, 0, tzinfo=dt.timezone.utc)
        entries = [
            UsageEntry(
                timestamp=ts1, model="claude-opus-4-5",
                usage={"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=0.01,
            ),
            UsageEntry(
                timestamp=ts2, model="claude-opus-4-5",
                usage={"input_tokens": 200, "output_tokens": 100,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=0.02,
            ),
        ]
        view = vm.build_daily_view(entries, now_utc=_now(), display_tz=None)
        # Two non-empty days; 5 gap days between them MUST NOT appear.
        assert len(view.rows) == 2
        assert view.rows[0].date == "2026-05-16"
        assert view.rows[1].date == "2026-05-10"

    def test_presentation_fields_left_at_defaults(self, vm):
        """Per spec §4.4: builder leaves `label` and `intensity_bucket`
        at dataclass defaults; the dashboard envelope adapter fills them.
        """
        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        from _lib_aggregators import UsageEntry  # noqa: WPS433

        ts = dt.datetime(2026, 5, 16, 12, 0, 0, tzinfo=dt.timezone.utc)
        entries = [
            UsageEntry(
                timestamp=ts, model="claude-opus-4-5",
                usage={"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=0.01,
            ),
        ]
        view = vm.build_daily_view(entries, now_utc=_now(), display_tz=None)
        assert all(r.label == "" for r in view.rows)
        assert all(r.intensity_bucket == 0 for r in view.rows)
