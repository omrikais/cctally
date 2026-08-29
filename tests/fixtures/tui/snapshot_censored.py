"""CENSORED state: the weekly meter reads at its cap, so nothing is projected.

#661 S2 spec section 3.2, and section 13's "the 100% right-censored path on
EVERY consumer". A displayed reading of 100 or more denotes `[99, +inf)` and
has no point estimate, so the forecast kernel withholds the projection band,
the ETA, the daily budgets and the week-average rate. The nearest existing
fixture (`over`, 92.4%) never reaches the cap, so every TUI render path
covering this state was uncovered: the Variant A panel, the Variant B ribbon
and the Forecast explain modal each printed a projection derived from the
censored reading.

The forecast here is the KERNEL's own output rather than a hand-assembled
`ForecastOutput`, so the fixture cannot encode a censored state the kernel
does not actually produce.
`last_sync_at=None` -> header displays deterministic "synced -".
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
# The meter reads past its cap. `true_percent_point` returns None at every
# reading of 100 or more, which is what puts the kernel in the censored state.
_P_NOW = 103.0
# A 24-hour-prior sample IS present, so the explain modal cannot blame the
# missing recent rate on a missing sample: the cause is the censoring.
_P_24H = 88.0
_DPP = 0.45

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
    spent_usd=46.35,
    snapshot_count=38,
    latest_snapshot_at=_NOW - dt.timedelta(seconds=312),
    p_24h_ago=_P_24H,
    t_24h_actual_hours=24.0,
    dollars_per_percent=_DPP,
    dollars_per_percent_source="this_week",
    confidence="high",
    low_confidence_reasons=[],
)
# The kernel decides what a censored reading publishes; the fixture does not.
_FC = m._compute_forecast(_FC_INPUTS, [100, 90])

SNAPSHOT = m.DataSnapshot(
    current_week=m.TuiCurrentWeek(
        week_start_at=_WEEK_START,
        week_end_at=_WEEK_END,
        used_pct=_P_NOW,
        five_hour_pct=38.0,
        five_hour_resets_at=_NOW + dt.timedelta(hours=1, minutes=12),
        spent_usd=46.35,
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
        m.TuiTrendRow("Apr 14", _WEEK_START, 103.0, 0.45, +0.03, 8, True),
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
