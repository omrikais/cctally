"""WARN state: 67.3% used, forecast projects overshoot but not capped.

Encodes the WARN state. The populated forecast carries the ACCELERATING
rate case (r_recent > r_avg),
which exercises the path where the recent 24h projection dominates the
week-avg projection. Verdict resolves to WARN — recent-24h projects 91%,
crossing the 90% threshold but staying under the 100% cap.
`last_sync_at=None` → header displays deterministic "synced -".
"""
import datetime as dt
import importlib.machinery
import importlib.util
import pathlib
import sys

_PATH = pathlib.Path(__file__).resolve().parents[3] / "bin" / "cctally"
# SourceFileLoader handles the extensionless script; sys.modules registration
# is required so dataclass machinery (which looks up cls.__module__) resolves.
_LOADER = importlib.machinery.SourceFileLoader("_ccusage_tui_fixture", str(_PATH))
_SPEC = importlib.util.spec_from_loader("_ccusage_tui_fixture", _LOADER)
m = importlib.util.module_from_spec(_SPEC)
sys.modules["_ccusage_tui_fixture"] = m
_SPEC.loader.exec_module(m)

_UTC = dt.timezone.utc
_NOW = dt.datetime(2026, 4, 20, 9, 0, tzinfo=_UTC)
_WEEK_START = dt.datetime(2026, 4, 14, 0, 0, tzinfo=_UTC)
_WEEK_END = _WEEK_START + dt.timedelta(days=7)
_ELAPSED_H = (_NOW - _WEEK_START).total_seconds() / 3600.0        # 153.0
_REMAINING_H = (_WEEK_END - _NOW).total_seconds() / 3600.0        # 15.0
_REMAINING_D = _REMAINING_H / 24.0                                # 0.625
_P_NOW = 67.3
# Accelerating: r_recent 1.6%/h > r_avg ≈ 0.440%/h. p_final_rec = 91.3% (WARN).
_P_24H = 28.9
_DPP = 0.42

_R_AVG = _P_NOW / _ELAPSED_H                                      # ≈ 0.43987
_R_REC = max(0.0, (_P_NOW - _P_24H) / 24.0)                       # = 1.6
_P_FIN_AVG = _P_NOW + _R_AVG * _REMAINING_H                       # ≈ 73.898
_P_FIN_REC = _P_NOW + _R_REC * _REMAINING_H                       # = 91.3

_FC_INPUTS = m.ForecastInputs(
    now_utc=_NOW,
    week_start_at=_WEEK_START,
    week_end_at=_WEEK_END,
    elapsed_hours=_ELAPSED_H,
    elapsed_fraction=_ELAPSED_H / 168.0,
    remaining_hours=_REMAINING_H,
    remaining_days=_REMAINING_D,
    p_now=_P_NOW,
    five_hour_percent=12.0,
    spent_usd=28.31,
    snapshot_count=27,
    latest_snapshot_at=_NOW - dt.timedelta(seconds=124),
    p_24h_ago=_P_24H,
    t_24h_actual_hours=24.0,
    dollars_per_percent=_DPP,
    dollars_per_percent_source="this_week",
    confidence="high",
    low_confidence_reasons=[],
)
_FC = m.ForecastOutput(
    inputs=_FC_INPUTS,
    r_avg=_R_AVG,
    r_recent=_R_REC,
    final_percent_low=min(_P_FIN_AVG, _P_FIN_REC),
    final_percent_high=max(_P_FIN_AVG, _P_FIN_REC),
    projected_cap=False,
    already_capped=False,
    cap_at=None,
    budgets=[
        m.BudgetRow(
            target_percent=100,
            pct_headroom=100.0 - _P_NOW,
            dollars_per_day=((100.0 - _P_NOW) * _DPP) / _REMAINING_D,
            percent_per_day=(100.0 - _P_NOW) / _REMAINING_D,
        ),
        m.BudgetRow(
            target_percent=90,
            pct_headroom=90.0 - _P_NOW,
            dollars_per_day=((90.0 - _P_NOW) * _DPP) / _REMAINING_D,
            percent_per_day=(90.0 - _P_NOW) / _REMAINING_D,
        ),
    ],
)

_TREND = [
    m.TuiTrendRow("Feb 17", dt.datetime(2026, 2, 17, tzinfo=_UTC),
                  42.1, 0.38, -0.04, 2, False),
    m.TuiTrendRow("Feb 24", dt.datetime(2026, 2, 24, tzinfo=_UTC),
                  51.8, 0.41, +0.03, 3, False),
    m.TuiTrendRow("Mar 03", dt.datetime(2026, 3, 3, tzinfo=_UTC),
                  48.9, 0.40, -0.01, 3, False),
    m.TuiTrendRow("Mar 10", dt.datetime(2026, 3, 10, tzinfo=_UTC),
                  58.4, 0.43, +0.03, 4, False),
    m.TuiTrendRow("Mar 17", dt.datetime(2026, 3, 17, tzinfo=_UTC),
                  72.1, 0.47, +0.04, 6, False),
    m.TuiTrendRow("Mar 24", dt.datetime(2026, 3, 24, tzinfo=_UTC),
                  64.7, 0.44, -0.03, 5, False),
    m.TuiTrendRow("Mar 31", dt.datetime(2026, 3, 31, tzinfo=_UTC),
                  81.2, 0.49, +0.05, 7, False),
    m.TuiTrendRow("Apr 14", _WEEK_START, 67.3, 0.42, -0.07, 8, True),
]

_PERCENT_MILESTONES = [
    m.TuiPercentMilestone(
        percent=p,
        crossed_at=_WEEK_START + dt.timedelta(hours=p * 2.3),  # deterministic stagger
        cumulative_cost_usd=_DPP * p,
        marginal_cost_usd=_DPP,
        five_hour_pct_at_crossing=(8.0 if p % 5 == 0 else 12.0),
    )
    for p in range(1, 68)  # 67 milestones — matches the warn state P_NOW=67.3
]

# Reuse the existing _TREND rows (8) and prepend 4 older weeks for the
# Trend modal's 12-week window.
_HISTORY_OLDER = [
    m.TuiTrendRow(
        week_label=f"{(_WEEK_START - dt.timedelta(weeks=12 - i)).strftime('%b %d')}",
        week_start_at=(_WEEK_START - dt.timedelta(weeks=12 - i)),
        used_pct=(38.0 + i * 1.5),
        dollars_per_percent=(0.38 + i * 0.005),
        delta_dpp=(0.005 if i > 0 else None),
        spark_height=(2 + i),
        is_current=False,
    )
    for i in range(4)  # 4 older weeks: 12w..9w before _WEEK_START
]
_WEEKLY_HISTORY = _HISTORY_OLDER + _TREND

SNAPSHOT = m.DataSnapshot(
    current_week=m.TuiCurrentWeek(
        week_start_at=_WEEK_START,
        week_end_at=_WEEK_END,
        used_pct=_P_NOW,
        five_hour_pct=12.0,
        five_hour_resets_at=_NOW + dt.timedelta(hours=3, minutes=42),
        spent_usd=28.31,
        dollars_per_percent=_DPP,
        latest_snapshot_at=_NOW - dt.timedelta(seconds=124),
    ),
    forecast=_FC,
    trend=_TREND,
    sessions=[
        m.TuiSessionRow(dt.datetime(2026, 4, 20, 14, 38, 2, tzinfo=_UTC),
                        42.0, "sonnet-4.5", 1.84, 67.0, "subscription-stats",
                        "7f3a2b89-4c1e-49a1-a000-000000000001"),
        m.TuiSessionRow(dt.datetime(2026, 4, 20, 13, 12, 44, tzinfo=_UTC),
                        78.0, "sonnet-4.5", 3.21, 71.0, "subscription-stats",
                        "7f3a2b89-4c1e-49a1-a000-000000000002"),
        m.TuiSessionRow(dt.datetime(2026, 4, 20, 11, 4, 21, tzinfo=_UTC),
                        29.0, "haiku-4.5", 0.14, 82.0, "dotfiles",
                        "7f3a2b89-4c1e-49a1-a000-000000000003"),
        m.TuiSessionRow(dt.datetime(2026, 4, 20, 9, 47, 10, tzinfo=_UTC),
                        123.0, "opus-4.5", 6.42, 54.0, "marketing-site",
                        "7f3a2b89-4c1e-49a1-a000-000000000004"),
        m.TuiSessionRow(dt.datetime(2026, 4, 20, 8, 2, 55, tzinfo=_UTC),
                        54.0, "sonnet-4.5", 2.10, 63.0, "subscription-stats",
                        "7f3a2b89-4c1e-49a1-a000-000000000005"),
        m.TuiSessionRow(dt.datetime(2026, 4, 19, 22, 41, 0, tzinfo=_UTC),
                        38.0, "sonnet-4.5", 1.52, 70.0, "subscription-stats",
                        "7f3a2b89-4c1e-49a1-a000-000000000006"),
        m.TuiSessionRow(dt.datetime(2026, 4, 19, 20, 17, 0, tzinfo=_UTC),
                        107.0, "opus-4.5", 5.88, 49.0, "cc-usage-viz",
                        "7f3a2b89-4c1e-49a1-a000-000000000007"),
    ],
    last_sync_at=None,
    last_sync_error=None,
    generated_at=_NOW,
    percent_milestones=_PERCENT_MILESTONES,
    weekly_history=_WEEKLY_HISTORY,
)
