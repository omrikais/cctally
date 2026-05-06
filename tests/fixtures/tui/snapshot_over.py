"""OVER state: 92.4% used, throttle immediately.

Encodes the OVER state. The populated forecast carries the ACCELERATING
rate case (r_recent > r_avg)
with the projected cap reached before week end. Verdict resolves to OVER;
cap_at is populated with a pinned timestamp so the renderer's "projected
cap at" surface is exercised by the golden.
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
_P_NOW = 92.4
# Accelerating: r_recent 0.8%/h > r_avg ≈ 0.604%/h.
_P_24H = 73.2
_DPP = 0.45

_R_AVG = _P_NOW / _ELAPSED_H                                      # ≈ 0.60392
_R_REC = max(0.0, (_P_NOW - _P_24H) / 24.0)                       # = 0.8
_P_FIN_AVG = _P_NOW + _R_AVG * _REMAINING_H                       # ≈ 101.459
_P_FIN_REC = _P_NOW + _R_REC * _REMAINING_H                       # = 104.4
# Cap forecast: r_pessimistic = max(r_avg, r_recent) = 0.8
# hours_to_cap = (100 - 92.4) / 0.8 = 9.5 → cap_at = _NOW + 9h30m
_CAP_AT = _NOW + dt.timedelta(hours=9, minutes=30)

_FC_INPUTS = m.ForecastInputs(
    now_utc=_NOW,
    week_start_at=_WEEK_START,
    week_end_at=_WEEK_END,
    elapsed_hours=_ELAPSED_H,
    elapsed_fraction=_ELAPSED_H / 168.0,
    remaining_hours=_REMAINING_H,
    remaining_days=_REMAINING_D,
    p_now=_P_NOW,
    five_hour_percent=38.0,
    spent_usd=41.18,
    snapshot_count=38,
    latest_snapshot_at=_NOW - dt.timedelta(seconds=312),
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
    projected_cap=True,
    already_capped=False,
    cap_at=_CAP_AT,
    budgets=[
        # target=100: headroom > 0 → populated row.
        m.BudgetRow(
            target_percent=100,
            pct_headroom=100.0 - _P_NOW,
            dollars_per_day=((100.0 - _P_NOW) * _DPP) / _REMAINING_D,
            percent_per_day=(100.0 - _P_NOW) / _REMAINING_D,
        ),
        # target=90: p_now (92.4) already past 90 → headroom <= 0, all-None row
        # per _compute_forecast's budget-row invariant.
        m.BudgetRow(target_percent=90, pct_headroom=None,
                    dollars_per_day=None, percent_per_day=None),
    ],
)

SNAPSHOT = m.DataSnapshot(
    current_week=m.TuiCurrentWeek(
        week_start_at=_WEEK_START,
        week_end_at=_WEEK_END,
        used_pct=_P_NOW,
        five_hour_pct=38.0,
        five_hour_resets_at=_NOW + dt.timedelta(hours=1, minutes=12),
        spent_usd=41.18,
        dollars_per_percent=_DPP,
        latest_snapshot_at=_NOW - dt.timedelta(seconds=312),
    ),
    forecast=_FC,
    trend=[
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
        m.TuiTrendRow("Apr 14", _WEEK_START, 92.4, 0.45, +0.03, 8, True),
    ],
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
)
