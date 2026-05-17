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


class TestMonthlyView:
    def test_empty_entries_returns_empty_view(self, vm):
        view = vm.build_monthly_view([], now_utc=_now(), display_tz=None)
        assert view.rows == ()
        assert view.aggregated == ()
        assert view.total_cost_usd == 0.0
        assert view.total_tokens == 0

    def test_multi_month_delta_linkage(self, vm):
        """Newest-first ordering + delta_cost_pct points at the
        immediately-older row; oldest row has delta None."""
        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        from _lib_aggregators import UsageEntry  # noqa: WPS433

        entries = [
            UsageEntry(
                timestamp=dt.datetime(2026, 3, 15, tzinfo=dt.timezone.utc),
                model="claude-opus-4-5",
                usage={"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=0.10,
            ),
            UsageEntry(
                timestamp=dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc),
                model="claude-opus-4-5",
                usage={"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=0.15,
            ),
        ]
        view = vm.build_monthly_view(entries, now_utc=_now(), display_tz=None)
        # Newest-first: April first, March second.
        assert view.rows[0].label == "2026-04"
        assert view.rows[1].label == "2026-03"
        # delta = (0.15 - 0.10) / 0.10 = 0.50
        assert view.rows[0].delta_cost_pct == pytest.approx(0.5, abs=1e-9)
        # Oldest row has no prior → delta None.
        assert view.rows[1].delta_cost_pct is None

    def test_is_current_marks_now_month(self, vm):
        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        from _lib_aggregators import UsageEntry  # noqa: WPS433

        entries = [
            UsageEntry(
                timestamp=dt.datetime(2026, 5, 5, tzinfo=dt.timezone.utc),
                model="claude-opus-4-5",
                usage={"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=0.05,
            ),
        ]
        # _now() is 2026-05-17 → current month is 2026-05.
        view = vm.build_monthly_view(entries, now_utc=_now(), display_tz=None)
        assert view.rows[0].is_current is True
        assert view.rows[0].label == "2026-05"

    def test_n_truncates_to_trailing_window(self, vm):
        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        from _lib_aggregators import UsageEntry  # noqa: WPS433

        entries = []
        for mo in (1, 2, 3, 4):
            entries.append(UsageEntry(
                timestamp=dt.datetime(2026, mo, 15, tzinfo=dt.timezone.utc),
                model="claude-opus-4-5",
                usage={"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=0.1 * mo,
            ))
        view = vm.build_monthly_view(entries, now_utc=_now(), n=2,
                                      display_tz=None)
        assert len(view.rows) == 2
        # Newest-first; cap to 2 takes 2026-04, 2026-03.
        assert view.rows[0].label == "2026-04"
        assert view.rows[1].label == "2026-03"


class TestWeeklyView:
    """Weekly view requires SubWeek records + an in-memory SQLite seeded
    with `weekly_usage_snapshots` rows for the usage overlay (spec §5.3).
    """

    @staticmethod
    def _seed_db():
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE weekly_usage_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start_date TEXT, week_end_date TEXT,
                week_start_at TEXT, week_end_at TEXT,
                weekly_percent REAL, five_hour_percent REAL,
                captured_at_utc TEXT
            );
        """)
        return conn

    @staticmethod
    def _make_subweek(start_date, end_date):
        """Build a minimal `SubWeek`-like namespace object.

        SubWeek is a frozen dataclass in _lib_subscription_weeks; we
        only need the fields the builder reads.
        """
        import sys as _sys
        if str(BIN_DIR) not in _sys.path:
            _sys.path.insert(0, str(BIN_DIR))
        import _lib_subscription_weeks  # noqa: WPS433
        return _lib_subscription_weeks.SubWeek(
            start_ts=dt.datetime.combine(
                start_date, dt.time.min, tzinfo=dt.timezone.utc,
            ).isoformat(),
            end_ts=dt.datetime.combine(
                end_date, dt.time.min, tzinfo=dt.timezone.utc,
            ).isoformat(),
            start_date=start_date,
            end_date=end_date,
            source="snapshot",
            display_start_date=start_date,
        )

    def test_empty_entries_returns_empty_view(self, vm):
        conn = self._seed_db()
        view = vm.build_weekly_view(
            conn, [], weeks=[], now_utc=_now(), display_tz=None,
        )
        assert view.rows == ()
        assert view.aggregated == ()
        assert view.overlay == ()

    def test_overlay_drives_used_pct_and_dpp(self, vm):
        """With a usage snapshot row, used_pct flows through and
        dollar_per_pct = cost_usd / used_pct."""
        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        from _lib_aggregators import UsageEntry  # noqa: WPS433

        conn = self._seed_db()
        sw_start = dt.date(2026, 5, 11)
        sw_end = dt.date(2026, 5, 18)
        # Seed a usage snapshot at 50% for this week.
        conn.execute(
            "INSERT INTO weekly_usage_snapshots ("
            "  week_start_date, week_end_date, week_start_at, week_end_at, "
            "  weekly_percent, five_hour_percent, captured_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sw_start.isoformat(), sw_end.isoformat(),
                dt.datetime.combine(sw_start, dt.time.min,
                                    tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                dt.datetime.combine(sw_end, dt.time.min,
                                    tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                50.0, 0.0,
                "2026-05-15T12:00:00Z",
            ),
        )
        conn.commit()
        sw = self._make_subweek(sw_start, sw_end)
        entries = [
            UsageEntry(
                timestamp=dt.datetime(2026, 5, 14, 12, tzinfo=dt.timezone.utc),
                model="claude-opus-4-5",
                usage={"input_tokens": 1000, "output_tokens": 500,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=10.0,
            ),
        ]
        view = vm.build_weekly_view(
            conn, entries, weeks=[sw], now_utc=_now(), display_tz=None,
        )
        assert len(view.rows) == 1
        r = view.rows[0]
        assert r.used_pct == pytest.approx(50.0, abs=1e-9)
        # dpp = 10.0 / 50.0 = 0.2
        assert r.dollar_per_pct == pytest.approx(0.2, abs=1e-9)
        # overlay parallel to aggregated.
        assert view.overlay[0] == (pytest.approx(50.0, abs=1e-9),
                                    pytest.approx(0.2, abs=1e-9))

    def test_no_overlay_when_no_snapshot(self, vm):
        """Week without a weekly_usage_snapshot row → used_pct=None,
        dollar_per_pct=None."""
        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        from _lib_aggregators import UsageEntry  # noqa: WPS433

        conn = self._seed_db()
        sw = self._make_subweek(dt.date(2026, 5, 4), dt.date(2026, 5, 11))
        entries = [
            UsageEntry(
                timestamp=dt.datetime(2026, 5, 7, 12, tzinfo=dt.timezone.utc),
                model="claude-opus-4-5",
                usage={"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=1.0,
            ),
        ]
        view = vm.build_weekly_view(
            conn, entries, weeks=[sw], now_utc=_now(), display_tz=None,
        )
        assert view.rows[0].used_pct is None
        assert view.rows[0].dollar_per_pct is None
        assert view.overlay[0] == (None, None)
