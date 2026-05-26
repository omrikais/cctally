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
                source_path="/tmp/synth.jsonl",
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
                source_path="/tmp/synth.jsonl",
            ),
            UsageEntry(
                timestamp=ts2, model="claude-opus-4-5",
                usage={"input_tokens": 200, "output_tokens": 100,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=0.02,
                source_path="/tmp/synth.jsonl",
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
                source_path="/tmp/synth.jsonl",
            ),
        ]
        view = vm.build_daily_view(entries, now_utc=_now(), display_tz=None)
        assert all(r.label == "" for r in view.rows)
        assert all(r.intensity_bucket == 0 for r in view.rows)


# Session C (T1.7) — the view builders must thread `mode` through to the
# per-entry cost kernel. Two entries on the same day: one with a recorded
# costUSD deliberately != its computed cost, one without any recorded cost.
# auto/calculate/display must diverge accordingly. Default `auto` is the
# pre-Session-C behavior.
def _mode_entries(vm):
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    from _lib_aggregators import UsageEntry  # noqa: WPS433

    ts = dt.datetime(2026, 4, 20, 9, 0, tzinfo=dt.timezone.utc)
    usage = {"input_tokens": 1000, "output_tokens": 500,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    # Entry A: recorded costUSD deliberately != computed-from-pricing.
    a = UsageEntry(timestamp=ts, model="claude-opus-4-7", usage=usage,
                   cost_usd=9.99, source_path="/tmp/a.jsonl")
    # Entry B: no recorded costUSD.
    b = UsageEntry(timestamp=ts, model="claude-opus-4-7", usage=usage,
                   cost_usd=None, source_path="/tmp/b.jsonl")
    return [a, b]


def test_build_daily_view_threads_mode(vm):
    now = dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc)
    entries = _mode_entries(vm)

    def total(mode):
        v = vm.build_daily_view(entries, now_utc=now, mode=mode)
        return sum(r.cost_usd for r in v.aggregated)

    auto = total("auto")
    calc = total("calculate")
    disp = total("display")
    # display: A contributes its recorded 9.99, B contributes 0.
    assert abs(disp - 9.99) < 1e-9, disp
    # auto: A uses recorded 9.99; B computes from pricing (> 0).
    assert auto > 9.99, auto
    # calculate: both compute from pricing, ignoring A's recorded value.
    assert abs(calc - 2 * (auto - 9.99)) < 1e-6, (calc, auto)
    # auto and calculate differ because A's recorded != A's computed.
    assert abs(auto - calc) > 1e-9, (auto, calc)


def test_build_monthly_view_threads_mode(vm):
    now = dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc)
    entries = _mode_entries(vm)

    def total(mode):
        # n large so the boundary-spillover bucket is not dropped.
        v = vm.build_monthly_view(entries, now_utc=now, n=10 ** 6, mode=mode)
        return sum(r.cost_usd for r in v.aggregated)

    auto = total("auto")
    calc = total("calculate")
    disp = total("display")
    assert abs(disp - 9.99) < 1e-9, disp
    assert auto > 9.99, auto
    assert abs(calc - 2 * (auto - 9.99)) < 1e-6, (calc, auto)
    assert abs(auto - calc) > 1e-9, (auto, calc)


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
                source_path="/tmp/synth.jsonl",
            ),
            UsageEntry(
                timestamp=dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc),
                model="claude-opus-4-5",
                usage={"input_tokens": 100, "output_tokens": 50,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0},
                cost_usd=0.15,
                source_path="/tmp/synth.jsonl",
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
                source_path="/tmp/synth.jsonl",
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
                source_path="/tmp/synth.jsonl",
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
                source_path="/tmp/synth.jsonl",
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
                source_path="/tmp/synth.jsonl",
            ),
        ]
        view = vm.build_weekly_view(
            conn, entries, weeks=[sw], now_utc=_now(), display_tz=None,
        )
        assert view.rows[0].used_pct is None
        assert view.rows[0].dollar_per_pct is None
        assert view.overlay[0] == (None, None)


class TestTrendView:
    """Trend view tests — requires in-memory SQLite seeded with
    weekly_usage_snapshots + weekly_cost_snapshots."""

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
            CREATE TABLE weekly_cost_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start_date TEXT, week_end_date TEXT,
                week_start_at TEXT, week_end_at TEXT,
                cost_usd REAL,
                range_start_iso TEXT, range_end_iso TEXT,
                captured_at_utc TEXT
            );
            CREATE TABLE week_reset_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                old_week_end_at TEXT, new_week_end_at TEXT,
                effective_reset_at_utc TEXT
            );
            CREATE TABLE percent_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start_date TEXT,
                week_start_at TEXT,
                percent_threshold INTEGER,
                cumulative_cost_usd REAL,
                marginal_cost_usd REAL,
                five_hour_percent_at_crossing REAL,
                captured_at_utc TEXT,
                alerted_at TEXT
            );
        """)
        return conn

    def test_empty_returns_none_avg(self, vm):
        conn = self._seed_db()
        view = vm.build_trend_view(conn, now_utc=_now(), n=8,
                                    display_tz=None)
        assert view.rows == ()
        assert view.avg_dollars_per_pct is None

    def test_fewer_than_3_samples_avg_is_none(self, vm):
        """Seed 2 weeks with valid usage% + cost — avg should be None
        per the 3-sample rule (spec §4.3)."""
        conn = self._seed_db()
        # Two weeks: 2026-04-27 (week of) and 2026-05-04 (week of).
        for ws_d, we_d, pct, cost in [
            ("2026-04-27", "2026-05-04", 50.0, 25.0),
            ("2026-05-04", "2026-05-11", 80.0, 40.0),
        ]:
            ws_at = ws_d + "T00:00:00Z"
            we_at = we_d + "T00:00:00Z"
            cap = ws_d + "T12:00:00Z"
            conn.execute(
                "INSERT INTO weekly_usage_snapshots ("
                " week_start_date, week_end_date, week_start_at, "
                " week_end_at, weekly_percent, five_hour_percent, "
                " captured_at_utc) VALUES (?,?,?,?,?,?,?)",
                (ws_d, we_d, ws_at, we_at, pct, 0.0, cap),
            )
            conn.execute(
                "INSERT INTO weekly_cost_snapshots ("
                " week_start_date, week_end_date, week_start_at, "
                " week_end_at, cost_usd, range_start_iso, "
                " range_end_iso, captured_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ws_d, we_d, ws_at, we_at, cost, ws_at, we_at, cap),
            )
        conn.commit()
        view = vm.build_trend_view(conn, now_utc=_now(), n=8,
                                    display_tz=None)
        assert len(view.rows) == 2
        # 2 valid samples → avg None (3-sample rule).
        assert view.avg_dollars_per_pct is None

    def test_at_least_3_samples_avg_computed(self, vm):
        conn = self._seed_db()
        for ws_d, we_d, pct, cost in [
            ("2026-04-13", "2026-04-20", 30.0, 15.0),   # dpp = 0.5
            ("2026-04-20", "2026-04-27", 40.0, 24.0),   # dpp = 0.6
            ("2026-04-27", "2026-05-04", 50.0, 30.0),   # dpp = 0.6
        ]:
            ws_at = ws_d + "T00:00:00Z"
            we_at = we_d + "T00:00:00Z"
            cap = ws_d + "T12:00:00Z"
            conn.execute(
                "INSERT INTO weekly_usage_snapshots ("
                " week_start_date, week_end_date, week_start_at, "
                " week_end_at, weekly_percent, five_hour_percent, "
                " captured_at_utc) VALUES (?,?,?,?,?,?,?)",
                (ws_d, we_d, ws_at, we_at, pct, 0.0, cap),
            )
            conn.execute(
                "INSERT INTO weekly_cost_snapshots ("
                " week_start_date, week_end_date, week_start_at, "
                " week_end_at, cost_usd, range_start_iso, "
                " range_end_iso, captured_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ws_d, we_d, ws_at, we_at, cost, ws_at, we_at, cap),
            )
        conn.commit()
        view = vm.build_trend_view(conn, now_utc=_now(), n=8,
                                    display_tz=None)
        assert len(view.rows) == 3
        # avg = (0.5 + 0.6 + 0.6) / 3 = 0.5666…
        assert view.avg_dollars_per_pct == pytest.approx(
            (0.5 + 0.6 + 0.6) / 3.0, abs=1e-9,
        )

    def test_extended_fields_populated(self, vm):
        """TuiTrendRow's 10 extended nullable fields must be populated
        when the trend builder owns the row construction (spec §4.1).
        """
        conn = self._seed_db()
        ws_d, we_d = "2026-04-27", "2026-05-04"
        ws_at = ws_d + "T00:00:00Z"
        we_at = we_d + "T00:00:00Z"
        cap = ws_d + "T12:00:00Z"
        conn.execute(
            "INSERT INTO weekly_usage_snapshots ("
            " week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, five_hour_percent, "
            " captured_at_utc) VALUES (?,?,?,?,?,?,?)",
            (ws_d, we_d, ws_at, we_at, 50.0, 0.0, cap),
        )
        conn.execute(
            "INSERT INTO weekly_cost_snapshots ("
            " week_start_date, week_end_date, week_start_at, "
            " week_end_at, cost_usd, range_start_iso, "
            " range_end_iso, captured_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ws_d, we_d, ws_at, we_at, 25.0, ws_at, we_at, cap),
        )
        conn.commit()
        view = vm.build_trend_view(conn, now_utc=_now(), n=8,
                                    display_tz=None)
        assert len(view.rows) == 1
        r = view.rows[0]
        assert r.week_start_date == dt.date(2026, 4, 27)
        assert r.week_end_date == dt.date(2026, 5, 4)
        assert r.weekly_cost_usd == pytest.approx(25.0, abs=1e-9)
        assert r.usage_captured_at == cap
        assert r.cost_captured_at == cap
        assert r.range_start_iso == ws_at
        assert r.range_end_iso == we_at
        # as_of = max(usage_captured, cost_captured) — both equal `cap`
        # here. _parse_iso_datetime_optional rebases to host-local tz
        # before isoformat; compare by parsing back to UTC for a tz-
        # agnostic instant check.
        assert r.as_of is not None
        as_of_dt = dt.datetime.fromisoformat(r.as_of).astimezone(dt.timezone.utc)
        assert as_of_dt == dt.datetime(2026, 4, 27, 12, 0, 0, tzinfo=dt.timezone.utc)


class TestSessionsView:
    """Sessions view tests — exercises the dual-shape (`rows` +
    `aggregated`) contract from spec §6.5, the merge-resumed-sessions
    invariant, and the limit-truncation behaviour.

    Uses inline `_JoinedClaudeEntry` builders so the tests stay isolated
    from the cache.db / session_files lazy-population pathway. The
    aggregator (`_aggregate_claude_sessions`) consumes joined entries
    directly — no DB-state mocking needed.
    """

    @staticmethod
    def _entry(*, ts, sid, src, project, model="claude-opus-4-5",
               input_tokens=100, output_tokens=50,
               cache_creation_tokens=0, cache_read_tokens=0):
        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        # `_JoinedClaudeEntry` lives in `_cctally_cache.py` (the cache-
        # tier sibling); `_lib_aggregators._aggregate_claude_sessions`
        # consumes it via a forward-string annotation. We import it
        # directly so the row builder gets the canonical dataclass
        # identity used by production code.
        from _cctally_cache import _JoinedClaudeEntry  # noqa: WPS433
        return _JoinedClaudeEntry(
            timestamp=ts,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            source_path=src,
            session_id=sid,
            project_path=project,
        )

    def test_empty_entries_returns_empty_view(self, vm):
        view = vm.build_sessions_view(
            [], now_utc=_now(), limit=100, display_tz=None,
        )
        assert view.rows == ()
        assert view.aggregated == ()
        assert view.total_sessions == 0
        assert view.total_cost_usd == 0.0
        assert view.display_tz_label  # non-empty string

    def test_single_session_one_row(self, vm):
        sid = "11111111-2222-3333-4444-555555555555"
        ts = dt.datetime(2026, 5, 16, 14, 0, 0, tzinfo=dt.timezone.utc)
        entries = [
            self._entry(
                ts=ts, sid=sid,
                src="/tmp/sess.jsonl",
                project="/Users/me/work/proj",
            ),
        ]
        view = vm.build_sessions_view(
            entries, now_utc=_now(), limit=100, display_tz=None,
        )
        assert view.total_sessions == 1
        assert len(view.rows) == 1
        assert len(view.aggregated) == 1
        # rows[i] and aggregated[i] describe the same merged sessionId.
        assert view.rows[0].session_id == sid
        assert view.aggregated[0].session_id == sid
        assert view.rows[0].model_primary == "claude-opus-4-5"
        assert view.rows[0].project_label == "proj"
        # total_cost_usd matches the aggregator's computed cost; we only
        # assert it's a positive float (the exact value depends on
        # CLAUDE_MODEL_PRICING which evolves).
        assert view.total_cost_usd > 0.0
        assert view.aggregated[0].cost_usd == pytest.approx(
            view.total_cost_usd, abs=1e-9,
        )

    def test_resumed_session_merges_across_files(self, vm):
        """A session_id that appears in TWO source files (resume scenario)
        collapses into ONE row in BOTH `rows` and `aggregated` (CLAUDE.md
        gotcha: 'session merges resumed sessions'). `source_paths` on
        the aggregated entry preserves the file set.
        """
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        ts1 = dt.datetime(2026, 5, 16, 14, 0, 0, tzinfo=dt.timezone.utc)
        ts2 = dt.datetime(2026, 5, 16, 14, 30, 0, tzinfo=dt.timezone.utc)
        entries = [
            self._entry(
                ts=ts1, sid=sid,
                src="/tmp/sess-a.jsonl",
                project="/Users/me/work/proj",
            ),
            self._entry(
                ts=ts2, sid=sid,
                src="/tmp/sess-b.jsonl",
                project="/Users/me/work/proj",
            ),
        ]
        view = vm.build_sessions_view(
            entries, now_utc=_now(), limit=100, display_tz=None,
        )
        assert view.total_sessions == 1
        assert len(view.rows) == 1
        assert len(view.aggregated) == 1
        # source_paths on aggregated preserves the file set (multi-JSONL
        # resume merge invariant).
        assert set(view.aggregated[0].source_paths) == {
            "/tmp/sess-a.jsonl", "/tmp/sess-b.jsonl",
        }

    def test_session_without_models_has_em_dash_primary(self, vm):
        """Defensive: a session whose aggregator output lists no models
        should render `model_primary='—'`. In practice this is hard to
        trigger via the public aggregator (entries imply a model), but
        the builder branch must stay defensive.
        """
        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        from _lib_aggregators import ClaudeSessionUsage  # noqa: WPS433

        sid = "cccccccc-0000-0000-0000-000000000000"
        ts = dt.datetime(2026, 5, 16, 14, 0, 0, tzinfo=dt.timezone.utc)
        # Construct a fake aggregator output by monkey-patching the
        # builder's _aggregate_claude_sessions call via direct
        # ClaudeSessionUsage list and calling the row-build loop
        # indirectly. The cleanest path: stub the aggregator on
        # _lib_aggregators for one call.
        import _lib_aggregators as _agg  # noqa: WPS433
        orig = _agg._aggregate_claude_sessions
        fake = ClaudeSessionUsage(
            session_id=sid,
            project_path="/Users/me/work/proj",
            source_paths=["/tmp/sess.jsonl"],
            first_activity=ts,
            last_activity=ts,
            input_tokens=0, cache_creation_tokens=0, cache_read_tokens=0,
            output_tokens=0, total_tokens=0,
            cost_usd=0.0,
            models=[],
            model_breakdowns=[],
        )
        _agg._aggregate_claude_sessions = lambda _entries, mode="auto": [fake]
        try:
            view = vm.build_sessions_view(
                [], now_utc=_now(), limit=100, display_tz=None,
            )
        finally:
            _agg._aggregate_claude_sessions = orig
        assert view.rows[0].model_primary == "—"
        # cache_hit_pct stays None when the I/O denominator is zero.
        assert view.rows[0].cache_hit_pct is None

    def test_limit_truncates_both_parallel_tuples(self, vm):
        """`limit=N` truncates rows AND aggregated to N — preserving
        the spec §4.3 invariant `total_sessions == len(rows) ==
        len(aggregated)`. The aggregator returns descending-by-
        last_activity, so the leading N are the most recent.
        """
        entries = []
        # 5 distinct sessions, descending in activity time. Use day i.
        for i in range(5):
            sid = f"{i:08x}-0000-0000-0000-000000000000"
            ts = dt.datetime(2026, 5, 10 + i, 14, 0, 0,
                             tzinfo=dt.timezone.utc)
            entries.append(self._entry(
                ts=ts, sid=sid,
                src=f"/tmp/sess-{i}.jsonl",
                project="/Users/me/work/proj",
            ))
        view = vm.build_sessions_view(
            entries, now_utc=_now(), limit=3, display_tz=None,
        )
        assert len(view.rows) == 3
        assert len(view.aggregated) == 3
        assert view.total_sessions == 3
        # Aggregator sort is desc by last_activity → leading 3 are the
        # newest. i=4 (May 14) first, i=3, i=2.
        assert view.rows[0].session_id == "00000004-0000-0000-0000-000000000000"
        assert view.rows[1].session_id == "00000003-0000-0000-0000-000000000000"
        assert view.rows[2].session_id == "00000002-0000-0000-0000-000000000000"

    def test_limit_none_keeps_all_sessions(self, vm):
        """`limit=None` is the CLI use case (`cctally session` emits
        every session in the date range). Builder must NOT truncate.
        """
        entries = []
        for i in range(5):
            sid = f"{i:08x}-0000-0000-0000-000000000000"
            ts = dt.datetime(2026, 5, 10 + i, 14, 0, 0,
                             tzinfo=dt.timezone.utc)
            entries.append(self._entry(
                ts=ts, sid=sid,
                src=f"/tmp/sess-{i}.jsonl",
                project="/Users/me/work/proj",
            ))
        view = vm.build_sessions_view(
            entries, now_utc=_now(), limit=None, display_tz=None,
        )
        assert len(view.rows) == 5
        assert len(view.aggregated) == 5
        assert view.total_sessions == 5
