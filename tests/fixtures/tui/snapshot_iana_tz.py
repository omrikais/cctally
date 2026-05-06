"""F4 regression: TUI session rows under explicit IANA --tz.

Mirrors the OK snapshot's shape but pins ``generated_at`` and the four
session ``started_at`` values to instants whose UTC hour differs from
their America/New_York hour, so the localized "today HH:MM:SS" format
visibly diverges from the raw-UTC strftime that the pre-F4 helper
emitted. Driven by the harness with ``--tz America/New_York``; the
golden under ``golden/iana_tz_conventional_120x36.txt`` captures the
localized clock face. ``last_sync_at=None`` → header displays the
deterministic "synced -" sentinel.
"""
import datetime as dt
import importlib.machinery
import importlib.util
import pathlib
import sys

_PATH = pathlib.Path(__file__).resolve().parents[3] / "bin" / "cctally"
_LOADER = importlib.machinery.SourceFileLoader("_ccusage_tui_iana", str(_PATH))
_SPEC = importlib.util.spec_from_loader("_ccusage_tui_iana", _LOADER)
m = importlib.util.module_from_spec(_SPEC)
sys.modules["_ccusage_tui_iana"] = m
_SPEC.loader.exec_module(m)

_UTC = dt.timezone.utc
# 2026-04-20T15:00 UTC = 2026-04-20 11:00 NY (DST → UTC-04:00). Sessions
# started earlier the same NY day; the formatter's "today" branch fires
# (date matches), so each row renders HH:MM:SS in NY local hours.
_NOW = dt.datetime(2026, 4, 20, 15, 0, tzinfo=_UTC)
_WEEK_START = dt.datetime(2026, 4, 14, 0, 0, tzinfo=_UTC)
_WEEK_END = _WEEK_START + dt.timedelta(days=7)
_ELAPSED_H = (_NOW - _WEEK_START).total_seconds() / 3600.0
_REMAINING_H = (_WEEK_END - _NOW).total_seconds() / 3600.0
_REMAINING_D = _REMAINING_H / 24.0
_P_NOW = 34.2
_P_24H = 31.0
_DPP = 0.42

_R_AVG = _P_NOW / _ELAPSED_H
_R_REC = max(0.0, (_P_NOW - _P_24H) / 24.0)
_P_FIN_AVG = _P_NOW + _R_AVG * _REMAINING_H
_P_FIN_REC = _P_NOW + _R_REC * _REMAINING_H

_FC_INPUTS = m.ForecastInputs(
    now_utc=_NOW,
    week_start_at=_WEEK_START,
    week_end_at=_WEEK_END,
    elapsed_hours=_ELAPSED_H,
    elapsed_fraction=_ELAPSED_H / 168.0,
    remaining_hours=_REMAINING_H,
    remaining_days=_REMAINING_D,
    p_now=_P_NOW,
    five_hour_percent=4.0,
    spent_usd=14.36,
    snapshot_count=12,
    latest_snapshot_at=_NOW - dt.timedelta(seconds=44),
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

# Session starts: all on 2026-04-20 (UTC), so they map to either the
# late-night-prior or early-morning of that NY day. Rows render as
# NY-local HH:MM:SS — UTC and NY would diverge (UTC=hr, NY=hr-4).
SNAPSHOT = m.DataSnapshot(
    current_week=m.TuiCurrentWeek(
        week_start_at=_WEEK_START,
        week_end_at=_WEEK_END,
        used_pct=_P_NOW,
        five_hour_pct=4.0,
        five_hour_resets_at=_NOW + dt.timedelta(hours=2, minutes=15),
        spent_usd=14.36,
        dollars_per_percent=_DPP,
        latest_snapshot_at=_NOW - dt.timedelta(seconds=44),
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
        # week_label localized via display_tz: 2026-04-14T00:00Z = 2026-04-13 20:00 NY
        m.TuiTrendRow("Apr 13", _WEEK_START, 34.2, 0.42, -0.07, 4, True),
    ],
    sessions=[
        # Started 2026-04-20T14:38:02 UTC → NY 10:38:02 (same day)
        m.TuiSessionRow(dt.datetime(2026, 4, 20, 14, 38, 2, tzinfo=_UTC),
                        42.0, "sonnet-4.5", 1.84, 67.0, "subscription-stats",
                        "7f3a2b89-4c1e-49a1-a000-000000000001"),
        # Started 2026-04-20T13:12:44 UTC → NY 09:12:44 (same day)
        m.TuiSessionRow(dt.datetime(2026, 4, 20, 13, 12, 44, tzinfo=_UTC),
                        78.0, "sonnet-4.5", 3.21, 71.0, "subscription-stats",
                        "7f3a2b89-4c1e-49a1-a000-000000000002"),
        # Started 2026-04-20T11:04:21 UTC → NY 07:04:21 (same day)
        m.TuiSessionRow(dt.datetime(2026, 4, 20, 11, 4, 21, tzinfo=_UTC),
                        29.0, "haiku-4.5", 0.14, 82.0, "dotfiles",
                        "7f3a2b89-4c1e-49a1-a000-000000000003"),
        # Started 2026-04-19T22:00:00 UTC → NY Apr 19 18:00 (yesterday in NY)
        # Falls into the "Mon DD HH:MM" branch.
        m.TuiSessionRow(dt.datetime(2026, 4, 19, 22, 0, 0, tzinfo=_UTC),
                        123.0, "opus-4.5", 6.42, 54.0, "marketing-site",
                        "7f3a2b89-4c1e-49a1-a000-000000000004"),
    ],
    last_sync_at=None,
    last_sync_error=None,
    generated_at=_NOW,
)
