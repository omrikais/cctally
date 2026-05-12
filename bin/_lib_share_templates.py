"""Share template registry.

Templates are pure-Python builders that produce ShareSnapshot instances
from panel-data + ShareOptions. The kernel (_lib_share.py) renders the
snapshots; templates own the data-to-snapshot composition.

Each template is identified by `<panel>-<archetype>` (e.g., `weekly-recap`).
Three archetypes per panel: recap, visual, detail.

This module ships in the public npm/brew distribution alongside
`bin/cctally` (promoted to public in .mirror-allowlist as of v1.6.2;
required by both the CLI `--format` surface and the dashboard share
GUI at runtime).

Spec: docs/superpowers/specs/2026-05-11-shareable-reports-v2-design.md §9
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


# --- Panel set ---
#
# Share-capable panels are the 8 data-view panels in the dashboard.
# RecentAlertsPanel ('alerts') is intentionally excluded: it's a
# notification stream, not a data view — shipping share templates over
# alerts would conflate the two concepts (spec §6.1, §9.5).
SHARE_CAPABLE_PANELS: frozenset[str] = frozenset({
    "current-week",
    "trend",
    "weekly",
    "daily",
    "monthly",
    "blocks",
    "forecast",
    "sessions",
})


@dataclass(frozen=True)
class ShareTemplate:
    id: str                    # globally unique: "<panel>-<archetype>"
    panel: str                 # routing key
    label: str                 # gallery tile heading ("Recap" / "Visual" / "Detail")
    description: str           # tile subhead
    default_options: Mapping[str, Any]
    builder: Callable[..., Any]  # (panel_data, share_options) -> ShareSnapshot


# Filled in subsequent tasks (M1.4 adds the 8 Recap templates;
# M2.1 adds the 16 Visual + Detail templates).
SHARE_TEMPLATES: tuple[ShareTemplate, ...] = ()


# --- Import-time invariants ---
#
# These run at module import and fail loudly on registry inconsistencies,
# mirroring the migration-ordering guards in bin/cctally.

def _validate_registry() -> None:
    ids = [t.id for t in SHARE_TEMPLATES]
    if len(ids) != len(set(ids)):
        dups = sorted({i for i in ids if ids.count(i) > 1})
        raise RuntimeError(f"duplicate share template ids: {dups}")
    panels_in = {t.panel for t in SHARE_TEMPLATES}
    unknown = panels_in - SHARE_CAPABLE_PANELS
    if unknown:
        raise RuntimeError(f"share templates reference unknown panels: {sorted(unknown)}")
    # NOTE: do NOT require panels_in == SHARE_CAPABLE_PANELS at import time
    # for the M1 in-progress state (registry being populated task-by-task).
    # The full-coverage assertion fires only once the registry is "complete",
    # gated by ENV var so partial dev builds don't break.
    if os.environ.get("CCTALLY_SHARE_TEMPLATES_REQUIRE_COMPLETE") == "1":
        missing = SHARE_CAPABLE_PANELS - panels_in
        if missing:
            raise RuntimeError(f"share registry missing panels: {sorted(missing)}")


# NOTE: the authoritative `_validate_registry()` import-time call lives at
# the END of this module, after `SHARE_TEMPLATES` has been extended with all
# registered templates. Calling it here (with an empty registry) was harmless
# under M1.3's scaffold-only state but blows up under
# `CCTALLY_SHARE_TEMPLATES_REQUIRE_COMPLETE=1` because the partial registry
# is "missing every panel." Defer the single gate to the bottom of the file.


# --- Lookup helpers (consumed by /api/share/render and /templates) ---

def templates_for_panel(panel: str) -> tuple[ShareTemplate, ...]:
    return tuple(t for t in SHARE_TEMPLATES if t.panel == panel)


def get_template(template_id: str) -> ShareTemplate:
    for t in SHARE_TEMPLATES:
        if t.id == template_id:
            return t
    raise KeyError(template_id)


# --- Shared builder helpers ---

import datetime as _dt


def _import_share_lib():
    """Module-load import, scoped here for testability — no cycle exists today.

    `_lib_share` is a pure stdlib sibling with no imports from this module, so
    a top-level `from _lib_share import ...` would work. Loading via a path
    spec instead keeps `_lib_share_templates` importable from both the in-tree
    test harness (which loads `bin/_lib_share_templates.py` by file path) and
    from `bin/cctally` (which also loads `_lib_share.py` by file path via
    `_share_load_lib`). One module instance is exposed as `_LS`.

    The loaded module MUST be registered in `sys.modules` before
    `exec_module` runs: Python 3.14's `@dataclass` decorator resolves
    `cls.__module__` via `sys.modules.get(...)` while building the field
    type-check, and would AttributeError on `None.__dict__` otherwise.
    """
    from pathlib import Path
    import importlib.util
    import sys
    if "_lib_share" in sys.modules:
        return sys.modules["_lib_share"]
    p = Path(__file__).resolve().parent / "_lib_share.py"
    spec = importlib.util.spec_from_file_location("_lib_share", p)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_lib_share"] = m
    try:
        spec.loader.exec_module(m)
    except Exception:
        sys.modules.pop("_lib_share", None)
        raise
    return m


_LS = _import_share_lib()


def _kpi_strip(*items: tuple[str, str]) -> tuple:
    """Generic KPI strip → tuple of `Totalled`."""
    return tuple(_LS.Totalled(label=lbl, value=val) for lbl, val in items)


def _top_projects_rows(top_projects, cap: int) -> tuple:
    """Build `Row` tuple with ProjectCell + MoneyCell from a list of
    `(project_path, cost_usd)` pairs.

    Anonymization happens later in `_scrub()` — builders always emit real
    names. Accepts both 2-tuples and `(path, cost, ...)` longer tuples;
    only the first two positional elements are used so callers can pass
    enriched rows without copy-coercion.
    """
    rows = []
    for entry in (top_projects or [])[:cap]:
        path = entry[0]
        cost = float(entry[1] or 0.0)
        rows.append(_LS.Row(cells={
            "project": _LS.ProjectCell(label=path),
            "cost":    _LS.MoneyCell(usd=cost),
        }))
    return tuple(rows)


_PROJECT_COLUMNS = (
    _LS.ColumnSpec(key="project", label="Project", align="left"),
    _LS.ColumnSpec(key="cost",    label="$",       align="right", emphasis=True),
)


# --- Cross-tab Detail-template helpers (issue #33, spec §6.1) ---
_CROSS_TAB_OTHER_KEY = "_other"


def _aggregate_breakdowns(
    breakdowns: list[dict[str, float]],
) -> list[tuple[str, float]]:
    """Aggregate per-row breakdowns into window-wide totals.

    Returns (label, total) sorted by total desc, ties broken lex
    ascending. Deterministic for goldens.
    """
    totals: dict[str, float] = {}
    for br in breakdowns:
        for k, v in br.items():
            totals[k] = totals.get(k, 0.0) + float(v or 0.0)
    return sorted(totals.items(), key=lambda p: (-p[1], p[0]))


def _cross_tab_columns(
    row_label_col: "_LS.ColumnSpec",
    members: list[tuple[str, float]],
    top_n: int,
    has_other_residual: bool,
    *,
    kind: str,                  # "project" | "model"
) -> tuple[tuple, tuple[str, ...], bool]:
    """Build (columns, top_k_labels, has_other) for a cross-tab table.

    Column keys are stable synthetic identifiers (m_0..m_K, _other) so
    project paths with awkward characters or post-scrub labels never
    affect renderer `row.cells.get(col.key)` lookups.

    `has_other` is True iff either:
      - len(members) > top_n (overflow case), OR
      - has_other_residual is True (partial-coverage case).

    Caller computes `has_other_residual` via `_detect_residual` over the
    provisional top-K labels. Spec §4.2.
    """
    top = members[:top_n]
    has_other_cap = len(members) > top_n
    has_other = has_other_cap or has_other_residual
    cols: list = [
        row_label_col,
        _LS.ColumnSpec(key="total", label="$", align="right", emphasis=True),
    ]
    for i, (lbl, _total) in enumerate(top):
        cols.append(_LS.ColumnSpec(
            key=f"m_{i}", label=lbl, align="right", kind=kind,
        ))
    if has_other:
        # kind=None: "Other" rollup is never a project name; scrubber skips.
        cols.append(_LS.ColumnSpec(
            key=_CROSS_TAB_OTHER_KEY, label="Other", align="right",
        ))
    return tuple(cols), tuple(t[0] for t in top), has_other


def _cross_tab_row(
    *,
    row_label_key: str,
    row_label_cell,
    row_total: float,
    breakdown: dict[str, float],
    top_k_labels: tuple[str, ...],
    has_other: bool,
):
    """One cross-tab row. Other = clamp(row_total - SUM(top_k cells), 0)."""
    cells: dict = {
        row_label_key: row_label_cell,
        "total": _LS.MoneyCell(usd=row_total),
    }
    other_sum = row_total
    for i, lbl in enumerate(top_k_labels):
        v = float(breakdown.get(lbl, 0.0))
        cells[f"m_{i}"] = _LS.MoneyCell(usd=v)
        other_sum -= v
    if has_other:
        cells[_CROSS_TAB_OTHER_KEY] = _LS.MoneyCell(usd=max(0.0, other_sum))
    return _LS.Row(cells=cells)


def _detect_residual(
    rows_and_breakdowns: list[tuple[float, dict[str, float]]],
    top_k_labels: tuple[str, ...],
    *,
    epsilon: float = 1e-9,
) -> bool:
    """Return True iff any row's residual (row_total - SUM(top-K cells))
    exceeds epsilon. Drives `has_other_residual` in `_cross_tab_columns`.
    """
    for row_total, breakdown in rows_and_breakdowns:
        top_k_sum = sum(float(breakdown.get(lbl, 0.0)) for lbl in top_k_labels)
        if abs(row_total - top_k_sum) > epsilon:
            return True
    return False


def _utc_now() -> _dt.datetime:
    """Override-aware UTC now (per `CCTALLY_AS_OF` env hook for fixture tests)."""
    s = os.environ.get("CCTALLY_AS_OF")
    if s:
        parsed = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed
    return _dt.datetime.now(_dt.timezone.utc)


def _release_version() -> str:
    """Read CHANGELOG-stamped latest version.

    Honors `CCTALLY_TEST_CHANGELOG_PATH` override (the documented test pattern
    at `bin/cctally:86`). Falls back to `"dev"` when CHANGELOG is unreadable
    or has no stamped release entry yet (pre-release dev builds).

    Parallel to `_release_read_latest_release_version` in `bin/cctally` —
    intentionally duplicated so the template module stays free of any
    `bin/cctally` import. If CHANGELOG header format changes, update both.
    """
    from pathlib import Path
    p = os.environ.get("CCTALLY_TEST_CHANGELOG_PATH")
    if p:
        path = Path(p)
    else:
        path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## [") and "Unreleased" not in line:
                # "## [1.5.0] - 2026-05-11" → "1.5.0"
                return line.split("[", 1)[1].split("]", 1)[0]
    except OSError:
        pass
    return "dev"


def _parse_iso_utc(s: str) -> _dt.datetime:
    """Parse an ISO-8601 string into a UTC-aware datetime.

    Accepts both `Z` and `+HH:MM` suffixes. Naive inputs are interpreted as
    UTC (matches the rest of the share kernel — JSON output always emits `Z`).
    """
    parsed = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _period(start, end, *, label: str, display_tz: str):
    return _LS.PeriodSpec(start=start, end=end,
                          display_tz=display_tz, label=label)


def _display_tz(options) -> str:
    return options.get("display_tz", "Etc/UTC")


# --- 8 Recap builders ---


def _build_weekly_recap(*, panel_data, options):
    """Weekly recap — balanced KPI + 8-week cost line + top-N projects.

    Expected panel_data shape (produced by M1.6's `_build_weekly_share_panel_data`):
        {
            "weeks": [
                {"start_date": "YYYY-MM-DD",      # ISO date string
                 "cost_usd":    float,
                 "pct_used":    float,            # fraction 0..1
                 "dollar_per_pct": float,
                 "top_projects":  [(path, cost), ...]},
                ... up to 8 weeks, chronological ...
            ],
            "current_week_index": int,            # index into weeks[]
        }
    """
    weeks = panel_data["weeks"]
    idx = panel_data.get("current_week_index", 0)
    w = weeks[idx]
    start = _parse_iso_utc(w["start_date"])
    end = start + _dt.timedelta(days=6)
    return _LS.ShareSnapshot(
        cmd="weekly",
        title=f"Weekly recap — week of {w['start_date']}",
        subtitle=None,
        period=_period(start, end, label="This week", display_tz=_display_tz(options)),
        columns=_PROJECT_COLUMNS,
        rows=_top_projects_rows(w.get("top_projects") or [], options.get("top_n", 5)),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=w2["start_date"], x_value=float(i),
                               y_value=float(w2["cost_usd"]))
                for i, w2 in enumerate(weeks)
            ),
            y_label="$ / week",
            reference_lines=(),
        ),
        totals=_kpi_strip(
            ("$ spent",      f"${w['cost_usd']:.2f}"),
            ("% used",       f"{w['pct_used']*100:.1f}%"),
            ("$/% rate",     f"${w['dollar_per_pct']:.3f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_current_week_recap(*, panel_data, options):
    """Current-week recap — week-to-date KPI strip + daily line + top-3 projects.

    CurrentWeekPanel has no 1:1 CLI counterpart (spec §9.5); panel_data is
    synthesized in M1.6 from the dashboard envelope.

    Expected panel_data shape:
        {
            "kpi_cost_usd":       float,
            "kpi_pct_used":      float,   # fraction 0..1
            "kpi_dollar_per_pct": float,
            "kpi_days_remaining": float,
            "daily_progression":  [{"date": "YYYY-MM-DD",
                                     "cost_usd": float}, ...],   # ≤7
            "top_projects":       [(path, cost), ...],
            "week_start_date":    "YYYY-MM-DD",
            "display_tz":         "Etc/UTC" | "...",
        }
    """
    progression = panel_data.get("daily_progression") or []
    start = _parse_iso_utc(panel_data["week_start_date"])
    end = start + _dt.timedelta(days=6)
    today_label = progression[-1]["date"] if progression else panel_data["week_start_date"]
    return _LS.ShareSnapshot(
        cmd="current-week",
        title=f"Current week — through {today_label}",
        subtitle=None,
        period=_period(start, end, label="This week", display_tz=_display_tz(options)),
        columns=_PROJECT_COLUMNS,
        rows=_top_projects_rows(panel_data.get("top_projects") or [],
                                options.get("top_n", 3)),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=d["date"], x_value=float(i),
                               y_value=float(d["cost_usd"]))
                for i, d in enumerate(progression)
            ),
            y_label="$ / day",
            reference_lines=(),
        ) if progression else None,
        totals=_kpi_strip(
            ("$ spent",        f"${panel_data['kpi_cost_usd']:.2f}"),
            ("% used",         f"{panel_data['kpi_pct_used']*100:.1f}%"),
            ("$/% rate",       f"${panel_data['kpi_dollar_per_pct']:.3f}"),
            ("Days remaining", f"{panel_data['kpi_days_remaining']:.1f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_trend_recap(*, panel_data, options):
    """Trend recap — $/% line over 8 weeks + 3-week delta KPI.

    Maps to CLI `report` subcommand (dashboard panel: `trend`).

    Expected panel_data shape:
        {
            "weeks": [
                {"start_date": "YYYY-MM-DD",
                 "cost_usd":      float,
                 "pct_used":      float,
                 "dollar_per_pct": float}, ... 8 entries, chronological ...
            ],
            "delta_3_weeks": {
                "dpp_change_pct": float,   # +ve = $/% trending up
                "cost_change_usd": float,
            },
        }
    """
    weeks = panel_data["weeks"]
    start = _parse_iso_utc(weeks[0]["start_date"]) if weeks else _utc_now()
    end_anchor = _parse_iso_utc(weeks[-1]["start_date"]) if weeks else _utc_now()
    end = end_anchor + _dt.timedelta(days=6)
    delta = panel_data.get("delta_3_weeks") or {}
    return _LS.ShareSnapshot(
        cmd="report",
        title="$/% trend — last 8 weeks",
        subtitle=None,
        period=_period(start, end, label="Last 8 weeks", display_tz=_display_tz(options)),
        columns=(
            _LS.ColumnSpec(key="week",  label="Week",   align="left"),
            _LS.ColumnSpec(key="cost",  label="$",      align="right", emphasis=True),
            _LS.ColumnSpec(key="pct",   label="% used", align="right"),
            _LS.ColumnSpec(key="dpp",   label="$/%",    align="right"),
        ),
        rows=tuple(
            _LS.Row(cells={
                "week": _LS.TextCell(w["start_date"]),
                "cost": _LS.MoneyCell(float(w["cost_usd"])),
                "pct":  _LS.PercentCell(float(w["pct_used"]) * 100.0),
                "dpp":  _LS.MoneyCell(float(w["dollar_per_pct"])),
            })
            for w in weeks
        ),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=w["start_date"], x_value=float(i),
                               y_value=float(w["dollar_per_pct"]))
                for i, w in enumerate(weeks)
            ),
            y_label="$ / 1%",
            reference_lines=(),
        ) if weeks else None,
        totals=_kpi_strip(
            ("Δ $/% (3wk)", f"{float(delta.get('dpp_change_pct') or 0.0)*100:+.1f}%"),
            ("Δ $ (3wk)",   f"${float(delta.get('cost_change_usd') or 0.0):+,.2f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_daily_recap(*, panel_data, options):
    """Daily recap — 7-day cost bar + top-5 projects.

    Maps to CLI `daily` (dashboard panel: `daily`).

    Expected panel_data shape:
        {
            "days": [{"date": "YYYY-MM-DD",
                       "cost_usd":      float,
                       "pct_of_period": float,
                       "top_model":     str}, ...],  # 7 entries, chronological
            "top_projects": [(path, cost), ...],
        }
    """
    days = panel_data.get("days") or []
    start = _parse_iso_utc(days[0]["date"]) if days else _utc_now()
    end_anchor = _parse_iso_utc(days[-1]["date"]) if days else start
    end = end_anchor + _dt.timedelta(days=1)
    sum_cost = sum(float(d["cost_usd"]) for d in days)
    return _LS.ShareSnapshot(
        cmd="daily",
        title=f"Daily — last {len(days)} day{'s' if len(days) != 1 else ''}",
        subtitle=None,
        period=_period(start, end, label="Last 7 days", display_tz=_display_tz(options)),
        columns=_PROJECT_COLUMNS,
        rows=_top_projects_rows(panel_data.get("top_projects") or [],
                                options.get("top_n", 5)),
        chart=_LS.BarChart(
            points=tuple(
                _LS.ChartPoint(x_label=d["date"], x_value=float(i),
                               y_value=float(d["cost_usd"]))
                for i, d in enumerate(days)
            ),
            y_label="$ / day",
        ) if days else None,
        totals=_kpi_strip(
            ("Sum",         f"${sum_cost:,.2f}"),
            ("Daily avg",   f"${(sum_cost / len(days) if days else 0.0):,.2f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_monthly_recap(*, panel_data, options):
    """Monthly recap — per-month bar + KPI strip + top-N projects.

    Maps to CLI `monthly` (dashboard panel: `monthly`).

    Expected panel_data shape:
        {
            "months": [{"month": "YYYY-MM",
                         "cost_usd": float,
                         "pct_used": float,        # fraction 0..1; may be 0
                         "top_model": str}, ...],  # chronological
            "top_projects": [(path, cost), ...],
        }
    """
    months = panel_data.get("months") or []

    def _month_start(s):
        return _parse_iso_utc(f"{s}-01")

    start = _month_start(months[0]["month"]) if months else _utc_now()
    if months:
        last = _month_start(months[-1]["month"])
        # End of last month: simple 31-day forward, then truncate to month end.
        end_anchor = last.replace(day=28) + _dt.timedelta(days=4)
        end = end_anchor.replace(day=1) - _dt.timedelta(days=1)
    else:
        end = start
    sum_cost = sum(float(m["cost_usd"]) for m in months)
    return _LS.ShareSnapshot(
        cmd="monthly",
        title=f"Monthly — last {len(months)} month{'s' if len(months) != 1 else ''}",
        subtitle=None,
        period=_period(start, end, label="Recent months",
                       display_tz=_display_tz(options)),
        columns=_PROJECT_COLUMNS,
        rows=_top_projects_rows(panel_data.get("top_projects") or [],
                                options.get("top_n", 5)),
        chart=_LS.BarChart(
            points=tuple(
                _LS.ChartPoint(x_label=m["month"], x_value=float(i),
                               y_value=float(m["cost_usd"]))
                for i, m in enumerate(months)
            ),
            y_label="$ / month",
        ) if months else None,
        totals=_kpi_strip(
            ("Sum",         f"${sum_cost:,.2f}"),
            ("Monthly avg", f"${(sum_cost / len(months) if months else 0.0):,.2f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_blocks_recap(*, panel_data, options):
    """Blocks recap — current 5h block KPI + recent-blocks line + top-3 projects.

    Maps to CLI `five-hour-blocks` (dashboard panel: `blocks`).

    Expected panel_data shape:
        {
            "current_block": {"start_at":    "ISO datetime",
                               "end_at":      "ISO datetime",
                               "cost_usd":     float,
                               "pct_used":     float,        # fraction 0..1
                               "tokens_total": int},
            "recent_blocks": [{"start_at": "ISO", "cost_usd": float}, ...],  # ≤8
            "top_projects":  [(path, cost), ...],
        }
    """
    cb = panel_data.get("current_block") or {}
    recent = panel_data.get("recent_blocks") or []
    start = _parse_iso_utc(cb["start_at"]) if cb.get("start_at") else _utc_now()
    end = _parse_iso_utc(cb["end_at"]) if cb.get("end_at") else start + _dt.timedelta(hours=5)
    return _LS.ShareSnapshot(
        cmd="five-hour-blocks",
        title="Current 5-hour block",
        subtitle=None,
        period=_period(start, end, label="Current block",
                       display_tz=_display_tz(options)),
        columns=_PROJECT_COLUMNS,
        rows=_top_projects_rows(panel_data.get("top_projects") or [],
                                options.get("top_n", 3)),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=b["start_at"], x_value=float(i),
                               y_value=float(b["cost_usd"]))
                for i, b in enumerate(recent)
            ),
            y_label="$ / block",
            reference_lines=(),
        ) if recent else None,
        totals=_kpi_strip(
            ("$ this block", f"${float(cb.get('cost_usd') or 0.0):.2f}"),
            ("% used",       f"{float(cb.get('pct_used') or 0.0)*100:.1f}%"),
            ("Tokens",       f"{int(cb.get('tokens_total') or 0):,}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_forecast_recap(*, panel_data, options):
    """Forecast recap — projection chart + budget table + days-to-ceiling KPIs.

    Maps to CLI `forecast` (dashboard panel: `forecast`).

    Expected panel_data shape:
        {
            "projected_end_pct":  float,   # fraction 0..1+
            "days_to_100pct":     float,
            "days_to_90pct":      float,
            "daily_budgets": {
                "avg":           float,   # $/day to-date
                "recent_24h":    float,
                "until_90pct":   float,
                "until_100pct":  float,
            },
            "projection_curve": [{"date": "YYYY-MM-DD",
                                    "projected_pct_used": float},  # fraction
                                  ...],  # ≤7 entries
            "confidence": "ok" | "LOW CONF",
        }
    """
    curve = panel_data.get("projection_curve") or []
    budgets = panel_data.get("daily_budgets") or {}
    start = _parse_iso_utc(curve[0]["date"]) if curve else _utc_now()
    end_anchor = _parse_iso_utc(curve[-1]["date"]) if curve else start
    end = end_anchor + _dt.timedelta(days=1)
    confidence = panel_data.get("confidence") or "ok"
    notes = ("LOW CONF: insufficient samples",) if confidence == "LOW CONF" else ()
    return _LS.ShareSnapshot(
        cmd="forecast",
        title="Forecast — projection to ceiling",
        subtitle=None,
        period=_period(start, end, label="Next 7 days",
                       display_tz=_display_tz(options)),
        columns=(
            _LS.ColumnSpec(key="metric", label="Metric", align="left"),
            _LS.ColumnSpec(key="value",  label="$/day", align="right", emphasis=True),
        ),
        rows=(
            _LS.Row(cells={"metric": _LS.TextCell("Avg to-date"),
                           "value":  _LS.MoneyCell(float(budgets.get("avg") or 0.0))}),
            _LS.Row(cells={"metric": _LS.TextCell("Recent 24h"),
                           "value":  _LS.MoneyCell(float(budgets.get("recent_24h") or 0.0))}),
            _LS.Row(cells={"metric": _LS.TextCell("Budget to 90%"),
                           "value":  _LS.MoneyCell(float(budgets.get("until_90pct") or 0.0))}),
            _LS.Row(cells={"metric": _LS.TextCell("Budget to 100%"),
                           "value":  _LS.MoneyCell(float(budgets.get("until_100pct") or 0.0))}),
        ),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=p["date"], x_value=float(i),
                               y_value=float(p["projected_pct_used"]) * 100.0)
                for i, p in enumerate(curve)
            ),
            y_label="projected %",
            reference_lines=(
                (90.0,  "90%",  "warn"),
                (100.0, "100%", "alarm"),
            ),
        ) if curve else None,
        totals=_kpi_strip(
            ("Days→90%",  f"{float(panel_data.get('days_to_90pct') or 0.0):.1f}"),
            ("Days→100%", f"{float(panel_data.get('days_to_100pct') or 0.0):.1f}"),
            ("End %",     f"{float(panel_data.get('projected_end_pct') or 0.0)*100:.1f}%"),
        ),
        notes=notes,
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_sessions_recap(*, panel_data, options):
    """Sessions recap — top-N sessions table + total (no chart per spec §9.5).

    Maps to CLI `session` (dashboard panel: `sessions`). Default `top_n` = 15
    per spec §9.6.

    Expected panel_data shape:
        {
            "sessions": [
                {"session_id":  str,
                 "project_path": str,
                 "cost_usd":    float,
                 "started_at":  "ISO datetime",
                 "model":       str},
                ... already sorted desc by cost, length ≤ top_n cap upstream ...
            ],
        }
    """
    sessions = panel_data.get("sessions") or []
    cap = options.get("top_n", 15)
    rows_iter = sessions[:cap]
    sum_cost = sum(float(s.get("cost_usd") or 0.0) for s in rows_iter)
    starts = [_parse_iso_utc(s["started_at"]) for s in rows_iter if s.get("started_at")]
    start = min(starts) if starts else _utc_now()
    end = max(starts) if starts else start
    return _LS.ShareSnapshot(
        cmd="session",
        title=f"Sessions — top {len(rows_iter)}",
        subtitle=None,
        period=_period(start, end, label="Recent sessions",
                       display_tz=_display_tz(options)),
        columns=(
            _LS.ColumnSpec(key="started", label="Started",  align="left"),
            _LS.ColumnSpec(key="project", label="Project",  align="left"),
            _LS.ColumnSpec(key="model",   label="Model",    align="left"),
            _LS.ColumnSpec(key="cost",    label="$",        align="right",
                           emphasis=True),
        ),
        rows=tuple(
            _LS.Row(cells={
                "started": (_LS.DateCell(when=_parse_iso_utc(s["started_at"]))
                            if s.get("started_at")
                            else _LS.TextCell("")),
                "project": _LS.ProjectCell(label=str(s.get("project_path") or "")),
                "model":   _LS.TextCell(str(s.get("model") or "")),
                "cost":    _LS.MoneyCell(float(s.get("cost_usd") or 0.0)),
            })
            for s in rows_iter
        ),
        chart=None,
        totals=_kpi_strip(
            ("Sum",     f"${sum_cost:,.2f}"),
            ("Shown",   f"{len(rows_iter)}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


# --- 16 Visual + Detail builders ---
#
# Archetype contract (spec §9.4):
#   - Visual: chart populated (same density as Recap), `rows=()`, `columns=()`,
#     `top_n=8` (default_options). Visuals drop the table entirely.
#   - Detail: chart populated, full table (`top_n=50`), columns same as Recap.
#
# Each Visual/Detail mirrors its Recap sibling's panel_data indexing,
# period assembly, and chart construction. Only `title`, `columns`/`rows`,
# and (where chart space permits trimming) `totals` differ.


def _build_weekly_visual(*, panel_data, options):
    """Weekly visual — chart-only, table dropped (spec §9.5).

    Same panel_data shape as `_build_weekly_recap`; Visual differs by
    emitting `rows=()` and `columns=()`. Chart density unchanged.
    """
    weeks = panel_data["weeks"]
    idx = panel_data.get("current_week_index", 0)
    w = weeks[idx]
    start = _parse_iso_utc(w["start_date"])
    end = start + _dt.timedelta(days=6)
    return _LS.ShareSnapshot(
        cmd="weekly",
        title=f"Weekly visual — week of {w['start_date']}",
        subtitle=None,
        period=_period(start, end, label="This week", display_tz=_display_tz(options)),
        columns=(),
        rows=(),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=w2["start_date"], x_value=float(i),
                               y_value=float(w2["cost_usd"]))
                for i, w2 in enumerate(weeks)
            ),
            y_label="$ / week",
            reference_lines=(),
        ),
        totals=_kpi_strip(
            ("$ spent",  f"${w['cost_usd']:.2f}"),
            ("% used",   f"{w['pct_used']*100:.1f}%"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_weekly_detail(*, panel_data, options):
    """Weekly detail — per-week × per-model cross-tab (spec §9.5).

    `panel_data["weeks"][i].models: dict[model_name, cost_usd]` carries
    each week's per-model breakdown. Window-wide top-K + `Other` rollup
    is computed at render time via `_aggregate_breakdowns` (spec §4.2).
    """
    weeks = panel_data["weeks"]
    idx = panel_data.get("current_week_index", 0)
    w = weeks[idx]
    start = _parse_iso_utc(w["start_date"])
    end = start + _dt.timedelta(days=6)
    top_n = max(int(options.get("top_n", 5)), 1)

    breakdowns = [dict(week.get("models") or {}) for week in weeks]
    members = _aggregate_breakdowns(breakdowns)
    top_k_labels_provisional = tuple(m[0] for m in members[:top_n])
    rows_and_breakdowns = [
        (float(week["cost_usd"]), dict(week.get("models") or {}))
        for week in weeks
    ]
    has_other_residual = _detect_residual(
        rows_and_breakdowns, top_k_labels_provisional,
    )
    columns, top_k, has_other = _cross_tab_columns(
        _LS.ColumnSpec(key="week", label="Week", align="left"),
        members, top_n, has_other_residual, kind="model",
    )
    rows = tuple(
        _cross_tab_row(
            row_label_key="week",
            row_label_cell=_LS.TextCell(week["start_date"]),
            row_total=float(week["cost_usd"]),
            breakdown=dict(week.get("models") or {}),
            top_k_labels=top_k,
            has_other=has_other,
        )
        for week in weeks
    )
    return _LS.ShareSnapshot(
        cmd="weekly",
        title=f"Weekly detail — week of {w['start_date']}",
        subtitle=None,
        period=_period(start, end, label="This week", display_tz=_display_tz(options)),
        columns=columns,
        rows=rows,
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=w2["start_date"], x_value=float(i),
                               y_value=float(w2["cost_usd"]))
                for i, w2 in enumerate(weeks)
            ),
            y_label="$ / week",
            reference_lines=(),
        ),
        totals=_kpi_strip(
            ("$ spent",      f"${w['cost_usd']:.2f}"),
            ("% used",       f"{w['pct_used']*100:.1f}%"),
            ("$/% rate",     f"${w['dollar_per_pct']:.3f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_current_week_visual(*, panel_data, options):
    """Current-week visual — week-to-date line, rows=() (spec §9.5)."""
    progression = panel_data.get("daily_progression") or []
    start = _parse_iso_utc(panel_data["week_start_date"])
    end = start + _dt.timedelta(days=6)
    today_label = progression[-1]["date"] if progression else panel_data["week_start_date"]
    return _LS.ShareSnapshot(
        cmd="current-week",
        title=f"Current week visual — through {today_label}",
        subtitle=None,
        period=_period(start, end, label="This week", display_tz=_display_tz(options)),
        columns=(),
        rows=(),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=d["date"], x_value=float(i),
                               y_value=float(d["cost_usd"]))
                for i, d in enumerate(progression)
            ),
            y_label="$ / day",
            reference_lines=(),
        ) if progression else None,
        totals=_kpi_strip(
            ("$ spent", f"${panel_data['kpi_cost_usd']:.2f}"),
            ("% used",  f"{panel_data['kpi_pct_used']*100:.1f}%"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_current_week_detail(*, panel_data, options):
    """Current-week detail — per-project full table + chart (spec §9.5)."""
    progression = panel_data.get("daily_progression") or []
    start = _parse_iso_utc(panel_data["week_start_date"])
    end = start + _dt.timedelta(days=6)
    today_label = progression[-1]["date"] if progression else panel_data["week_start_date"]
    top_n = max(int(options.get("top_n", 50)), 1)
    return _LS.ShareSnapshot(
        cmd="current-week",
        title=f"Current week detail — through {today_label}",
        subtitle=None,
        period=_period(start, end, label="This week", display_tz=_display_tz(options)),
        columns=_PROJECT_COLUMNS,
        rows=_top_projects_rows(panel_data.get("top_projects") or [], top_n),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=d["date"], x_value=float(i),
                               y_value=float(d["cost_usd"]))
                for i, d in enumerate(progression)
            ),
            y_label="$ / day",
            reference_lines=(),
        ) if progression else None,
        totals=_kpi_strip(
            ("$ spent",        f"${panel_data['kpi_cost_usd']:.2f}"),
            ("% used",         f"{panel_data['kpi_pct_used']*100:.1f}%"),
            ("$/% rate",       f"${panel_data['kpi_dollar_per_pct']:.3f}"),
            ("Days remaining", f"{panel_data['kpi_days_remaining']:.1f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_trend_visual(*, panel_data, options):
    """Trend visual — $/% trend line over 8 weeks; rows=() (spec §9.5)."""
    weeks = panel_data["weeks"]
    start = _parse_iso_utc(weeks[0]["start_date"]) if weeks else _utc_now()
    end_anchor = _parse_iso_utc(weeks[-1]["start_date"]) if weeks else _utc_now()
    end = end_anchor + _dt.timedelta(days=6)
    delta = panel_data.get("delta_3_weeks") or {}
    return _LS.ShareSnapshot(
        cmd="report",
        title="$/% trend visual — last 8 weeks",
        subtitle=None,
        period=_period(start, end, label="Last 8 weeks", display_tz=_display_tz(options)),
        columns=(),
        rows=(),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=w["start_date"], x_value=float(i),
                               y_value=float(w["dollar_per_pct"]))
                for i, w in enumerate(weeks)
            ),
            y_label="$ / 1%",
            reference_lines=(),
        ) if weeks else None,
        totals=_kpi_strip(
            ("Δ $/% (3wk)", f"{float(delta.get('dpp_change_pct') or 0.0)*100:+.1f}%"),
            ("Δ $ (3wk)",   f"${float(delta.get('cost_change_usd') or 0.0):+,.2f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_trend_detail(*, panel_data, options):
    """Trend detail — full 8-week × $/%/rate table + sparkline (spec §9.5)."""
    weeks = panel_data["weeks"]
    start = _parse_iso_utc(weeks[0]["start_date"]) if weeks else _utc_now()
    end_anchor = _parse_iso_utc(weeks[-1]["start_date"]) if weeks else _utc_now()
    end = end_anchor + _dt.timedelta(days=6)
    delta = panel_data.get("delta_3_weeks") or {}
    return _LS.ShareSnapshot(
        cmd="report",
        title="$/% trend detail — last 8 weeks",
        subtitle=None,
        period=_period(start, end, label="Last 8 weeks", display_tz=_display_tz(options)),
        columns=(
            _LS.ColumnSpec(key="week",  label="Week",   align="left"),
            _LS.ColumnSpec(key="cost",  label="$",      align="right", emphasis=True),
            _LS.ColumnSpec(key="pct",   label="% used", align="right"),
            _LS.ColumnSpec(key="dpp",   label="$/%",    align="right"),
        ),
        rows=tuple(
            _LS.Row(cells={
                "week": _LS.TextCell(w["start_date"]),
                "cost": _LS.MoneyCell(float(w["cost_usd"])),
                "pct":  _LS.PercentCell(float(w["pct_used"]) * 100.0),
                "dpp":  _LS.MoneyCell(float(w["dollar_per_pct"])),
            })
            for w in weeks
        ),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=w["start_date"], x_value=float(i),
                               y_value=float(w["dollar_per_pct"]))
                for i, w in enumerate(weeks)
            ),
            y_label="$ / 1%",
            reference_lines=(),
        ) if weeks else None,
        totals=_kpi_strip(
            ("Δ $/% (3wk)", f"{float(delta.get('dpp_change_pct') or 0.0)*100:+.1f}%"),
            ("Δ $ (3wk)",   f"${float(delta.get('cost_change_usd') or 0.0):+,.2f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_daily_visual(*, panel_data, options):
    """Daily visual — 7-day cost bar, rows=() (spec §9.5)."""
    days = panel_data.get("days") or []
    start = _parse_iso_utc(days[0]["date"]) if days else _utc_now()
    end_anchor = _parse_iso_utc(days[-1]["date"]) if days else start
    end = end_anchor + _dt.timedelta(days=1)
    sum_cost = sum(float(d["cost_usd"]) for d in days)
    return _LS.ShareSnapshot(
        cmd="daily",
        title=f"Daily visual — last {len(days)} day{'s' if len(days) != 1 else ''}",
        subtitle=None,
        period=_period(start, end, label="Last 7 days", display_tz=_display_tz(options)),
        columns=(),
        rows=(),
        chart=_LS.BarChart(
            points=tuple(
                _LS.ChartPoint(x_label=d["date"], x_value=float(i),
                               y_value=float(d["cost_usd"]))
                for i, d in enumerate(days)
            ),
            y_label="$ / day",
        ) if days else None,
        totals=_kpi_strip(
            ("Sum",         f"${sum_cost:,.2f}"),
            ("Daily avg",   f"${(sum_cost / len(days) if days else 0.0):,.2f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_daily_detail(*, panel_data, options):
    """Daily detail — per-day × per-project cross-tab (spec §9.5).

    `panel_data["days"][i].projects: dict[project_path, cost_usd]`
    carries each day's per-project breakdown.
    """
    days = panel_data.get("days") or []
    start = _parse_iso_utc(days[0]["date"]) if days else _utc_now()
    end_anchor = _parse_iso_utc(days[-1]["date"]) if days else start
    end = end_anchor + _dt.timedelta(days=1)
    sum_cost = sum(float(d["cost_usd"]) for d in days)
    top_n = max(int(options.get("top_n", 5)), 1)

    breakdowns = [dict(d.get("projects") or {}) for d in days]
    members = _aggregate_breakdowns(breakdowns)
    top_k_labels_provisional = tuple(m[0] for m in members[:top_n])
    rows_and_breakdowns = [
        (float(d["cost_usd"]), dict(d.get("projects") or {}))
        for d in days
    ]
    has_other_residual = _detect_residual(
        rows_and_breakdowns, top_k_labels_provisional,
    )
    columns, top_k, has_other = _cross_tab_columns(
        _LS.ColumnSpec(key="date", label="Day", align="left"),
        members, top_n, has_other_residual, kind="project",
    )
    rows = tuple(
        _cross_tab_row(
            row_label_key="date",
            row_label_cell=_LS.TextCell(d["date"]),
            row_total=float(d["cost_usd"]),
            breakdown=dict(d.get("projects") or {}),
            top_k_labels=top_k,
            has_other=has_other,
        )
        for d in days
    )
    return _LS.ShareSnapshot(
        cmd="daily",
        title=f"Daily detail — last {len(days)} day{'s' if len(days) != 1 else ''}",
        subtitle=None,
        period=_period(start, end, label="Last 7 days", display_tz=_display_tz(options)),
        columns=columns,
        rows=rows,
        chart=_LS.BarChart(
            points=tuple(
                _LS.ChartPoint(x_label=d["date"], x_value=float(i),
                               y_value=float(d["cost_usd"]))
                for i, d in enumerate(days)
            ),
            y_label="$ / day",
        ) if days else None,
        totals=_kpi_strip(
            ("Sum",         f"${sum_cost:,.2f}"),
            ("Daily avg",   f"${(sum_cost / len(days) if days else 0.0):,.2f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_monthly_visual(*, panel_data, options):
    """Monthly visual — month-over-month bar, rows=() (spec §9.5)."""
    months = panel_data.get("months") or []

    def _month_start(s):
        return _parse_iso_utc(f"{s}-01")

    start = _month_start(months[0]["month"]) if months else _utc_now()
    if months:
        last = _month_start(months[-1]["month"])
        end_anchor = last.replace(day=28) + _dt.timedelta(days=4)
        end = end_anchor.replace(day=1) - _dt.timedelta(days=1)
    else:
        end = start
    sum_cost = sum(float(m["cost_usd"]) for m in months)
    return _LS.ShareSnapshot(
        cmd="monthly",
        title=f"Monthly visual — last {len(months)} month{'s' if len(months) != 1 else ''}",
        subtitle=None,
        period=_period(start, end, label="Recent months",
                       display_tz=_display_tz(options)),
        columns=(),
        rows=(),
        chart=_LS.BarChart(
            points=tuple(
                _LS.ChartPoint(x_label=m["month"], x_value=float(i),
                               y_value=float(m["cost_usd"]))
                for i, m in enumerate(months)
            ),
            y_label="$ / month",
        ) if months else None,
        totals=_kpi_strip(
            ("Sum",         f"${sum_cost:,.2f}"),
            ("Monthly avg", f"${(sum_cost / len(months) if months else 0.0):,.2f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_monthly_detail(*, panel_data, options):
    """Monthly detail — per-month × per-model cross-tab (spec §9.5)."""
    months = panel_data.get("months") or []

    def _month_start(s):
        return _parse_iso_utc(f"{s}-01")

    start = _month_start(months[0]["month"]) if months else _utc_now()
    if months:
        last = _month_start(months[-1]["month"])
        end_anchor = last.replace(day=28) + _dt.timedelta(days=4)
        end = end_anchor.replace(day=1) - _dt.timedelta(days=1)
    else:
        end = start
    sum_cost = sum(float(m["cost_usd"]) for m in months)
    top_n = max(int(options.get("top_n", 5)), 1)

    breakdowns = [dict(m.get("models") or {}) for m in months]
    members = _aggregate_breakdowns(breakdowns)
    top_k_labels_provisional = tuple(m[0] for m in members[:top_n])
    rows_and_breakdowns = [
        (float(m["cost_usd"]), dict(m.get("models") or {}))
        for m in months
    ]
    has_other_residual = _detect_residual(
        rows_and_breakdowns, top_k_labels_provisional,
    )
    columns, top_k, has_other = _cross_tab_columns(
        _LS.ColumnSpec(key="month", label="Month", align="left"),
        members, top_n, has_other_residual, kind="model",
    )
    rows = tuple(
        _cross_tab_row(
            row_label_key="month",
            row_label_cell=_LS.TextCell(m["month"]),
            row_total=float(m["cost_usd"]),
            breakdown=dict(m.get("models") or {}),
            top_k_labels=top_k,
            has_other=has_other,
        )
        for m in months
    )
    return _LS.ShareSnapshot(
        cmd="monthly",
        title=f"Monthly detail — last {len(months)} month{'s' if len(months) != 1 else ''}",
        subtitle=None,
        period=_period(start, end, label="Recent months",
                       display_tz=_display_tz(options)),
        columns=columns,
        rows=rows,
        chart=_LS.BarChart(
            points=tuple(
                _LS.ChartPoint(x_label=m["month"], x_value=float(i),
                               y_value=float(m["cost_usd"]))
                for i, m in enumerate(months)
            ),
            y_label="$ / month",
        ) if months else None,
        totals=_kpi_strip(
            ("Sum",         f"${sum_cost:,.2f}"),
            ("Monthly avg", f"${(sum_cost / len(months) if months else 0.0):,.2f}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_blocks_visual(*, panel_data, options):
    """Blocks visual — recent-blocks line, rows=() (spec §9.5)."""
    cb = panel_data.get("current_block") or {}
    recent = panel_data.get("recent_blocks") or []
    start = _parse_iso_utc(cb["start_at"]) if cb.get("start_at") else _utc_now()
    end = _parse_iso_utc(cb["end_at"]) if cb.get("end_at") else start + _dt.timedelta(hours=5)
    return _LS.ShareSnapshot(
        cmd="five-hour-blocks",
        title="Current 5-hour block — visual",
        subtitle=None,
        period=_period(start, end, label="Current block",
                       display_tz=_display_tz(options)),
        columns=(),
        rows=(),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=b["start_at"], x_value=float(i),
                               y_value=float(b["cost_usd"]))
                for i, b in enumerate(recent)
            ),
            y_label="$ / block",
            reference_lines=(),
        ) if recent else None,
        totals=_kpi_strip(
            ("$ this block", f"${float(cb.get('cost_usd') or 0.0):.2f}"),
            ("% used",       f"{float(cb.get('pct_used') or 0.0)*100:.1f}%"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_blocks_detail(*, panel_data, options):
    """Blocks detail — per-block × per-project cross-tab (spec §9.5)."""
    cb = panel_data.get("current_block") or {}
    recent = panel_data.get("recent_blocks") or []
    start = _parse_iso_utc(cb["start_at"]) if cb.get("start_at") else _utc_now()
    end = _parse_iso_utc(cb["end_at"]) if cb.get("end_at") else start + _dt.timedelta(hours=5)
    top_n = max(int(options.get("top_n", 5)), 1)

    breakdowns = [dict(b.get("projects") or {}) for b in recent]
    members = _aggregate_breakdowns(breakdowns)
    top_k_labels_provisional = tuple(m[0] for m in members[:top_n])
    rows_and_breakdowns = [
        (float(b["cost_usd"]), dict(b.get("projects") or {}))
        for b in recent
    ]
    has_other_residual = _detect_residual(
        rows_and_breakdowns, top_k_labels_provisional,
    )
    columns, top_k, has_other = _cross_tab_columns(
        _LS.ColumnSpec(key="block", label="Block (start)", align="left"),
        members, top_n, has_other_residual, kind="project",
    )
    rows = tuple(
        _cross_tab_row(
            row_label_key="block",
            row_label_cell=_LS.TextCell(b["start_at"]),
            row_total=float(b["cost_usd"]),
            breakdown=dict(b.get("projects") or {}),
            top_k_labels=top_k,
            has_other=has_other,
        )
        for b in recent
    )
    return _LS.ShareSnapshot(
        cmd="five-hour-blocks",
        title="Current 5-hour block — detail",
        subtitle=None,
        period=_period(start, end, label="Current block",
                       display_tz=_display_tz(options)),
        columns=columns,
        rows=rows,
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=b["start_at"], x_value=float(i),
                               y_value=float(b["cost_usd"]))
                for i, b in enumerate(recent)
            ),
            y_label="$ / block",
            reference_lines=(),
        ) if recent else None,
        totals=_kpi_strip(
            ("$ this block", f"${float(cb.get('cost_usd') or 0.0):.2f}"),
            ("% used",       f"{float(cb.get('pct_used') or 0.0)*100:.1f}%"),
            ("Tokens",       f"{int(cb.get('tokens_total') or 0):,}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_forecast_visual(*, panel_data, options):
    """Forecast visual — projection chart with 90/100% ceilings, rows=() (spec §9.5)."""
    curve = panel_data.get("projection_curve") or []
    start = _parse_iso_utc(curve[0]["date"]) if curve else _utc_now()
    end_anchor = _parse_iso_utc(curve[-1]["date"]) if curve else start
    end = end_anchor + _dt.timedelta(days=1)
    confidence = panel_data.get("confidence") or "ok"
    notes = ("LOW CONF: insufficient samples",) if confidence == "LOW CONF" else ()
    return _LS.ShareSnapshot(
        cmd="forecast",
        title="Forecast visual — projection to ceiling",
        subtitle=None,
        period=_period(start, end, label="Next 7 days",
                       display_tz=_display_tz(options)),
        columns=(),
        rows=(),
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=p["date"], x_value=float(i),
                               y_value=float(p["projected_pct_used"]) * 100.0)
                for i, p in enumerate(curve)
            ),
            y_label="projected %",
            reference_lines=(
                (90.0,  "90%",  "warn"),
                (100.0, "100%", "alarm"),
            ),
        ) if curve else None,
        totals=_kpi_strip(
            ("Days→90%",  f"{float(panel_data.get('days_to_90pct') or 0.0):.1f}"),
            ("Days→100%", f"{float(panel_data.get('days_to_100pct') or 0.0):.1f}"),
            ("End %",     f"{float(panel_data.get('projected_end_pct') or 0.0)*100:.1f}%"),
        ),
        notes=notes,
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_forecast_detail(*, panel_data, options):
    """Forecast detail — per-day projection table + chart (spec §9.5).

    Detail's table emits one row per projection-curve day showing the
    cumulative projected % alongside the 4-line budget metric block from
    the Recap. Top_n caps the row count.
    """
    curve = panel_data.get("projection_curve") or []
    budgets = panel_data.get("daily_budgets") or {}
    start = _parse_iso_utc(curve[0]["date"]) if curve else _utc_now()
    end_anchor = _parse_iso_utc(curve[-1]["date"]) if curve else start
    end = end_anchor + _dt.timedelta(days=1)
    confidence = panel_data.get("confidence") or "ok"
    notes = ("LOW CONF: insufficient samples",) if confidence == "LOW CONF" else ()
    top_n = max(int(options.get("top_n", 50)), 1)
    # Budget metric rows + per-day projection rows, capped at top_n total.
    budget_rows = (
        _LS.Row(cells={"metric": _LS.TextCell("Avg to-date"),
                       "value":  _LS.MoneyCell(float(budgets.get("avg") or 0.0))}),
        _LS.Row(cells={"metric": _LS.TextCell("Recent 24h"),
                       "value":  _LS.MoneyCell(float(budgets.get("recent_24h") or 0.0))}),
        _LS.Row(cells={"metric": _LS.TextCell("Budget to 90%"),
                       "value":  _LS.MoneyCell(float(budgets.get("until_90pct") or 0.0))}),
        _LS.Row(cells={"metric": _LS.TextCell("Budget to 100%"),
                       "value":  _LS.MoneyCell(float(budgets.get("until_100pct") or 0.0))}),
    )
    day_rows = tuple(
        _LS.Row(cells={
            "metric": _LS.TextCell(p["date"]),
            "value":  _LS.PercentCell(float(p["projected_pct_used"]) * 100.0),
        })
        for p in curve
    )
    rows = (budget_rows + day_rows)[:top_n]
    return _LS.ShareSnapshot(
        cmd="forecast",
        title="Forecast detail — projection to ceiling",
        subtitle=None,
        period=_period(start, end, label="Next 7 days",
                       display_tz=_display_tz(options)),
        columns=(
            _LS.ColumnSpec(key="metric", label="Metric", align="left"),
            _LS.ColumnSpec(key="value",  label="$/day", align="right", emphasis=True),
        ),
        rows=rows,
        chart=_LS.LineChart(
            points=tuple(
                _LS.ChartPoint(x_label=p["date"], x_value=float(i),
                               y_value=float(p["projected_pct_used"]) * 100.0)
                for i, p in enumerate(curve)
            ),
            y_label="projected %",
            reference_lines=(
                (90.0,  "90%",  "warn"),
                (100.0, "100%", "alarm"),
            ),
        ) if curve else None,
        totals=_kpi_strip(
            ("Days→90%",  f"{float(panel_data.get('days_to_90pct') or 0.0):.1f}"),
            ("Days→100%", f"{float(panel_data.get('days_to_100pct') or 0.0):.1f}"),
            ("End %",     f"{float(panel_data.get('projected_end_pct') or 0.0)*100:.1f}%"),
        ),
        notes=notes,
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_sessions_visual(*, panel_data, options):
    """Sessions visual — horizontal bar of top-N sessions by cost; rows=().

    Spec §9.5. Sessions Recap has no chart (it's a pure table); Visual
    flips that — chart only via `HorizontalBarChart` (top-N capped),
    rows=(). `cap=None` means show all `points` (the builder pre-truncates).
    """
    sessions = panel_data.get("sessions") or []
    cap = int(options.get("top_n", 8))
    rows_iter = sessions[:cap]
    sum_cost = sum(float(s.get("cost_usd") or 0.0) for s in rows_iter)
    starts = [_parse_iso_utc(s["started_at"]) for s in rows_iter if s.get("started_at")]
    start = min(starts) if starts else _utc_now()
    end = max(starts) if starts else start
    return _LS.ShareSnapshot(
        cmd="session",
        title=f"Sessions visual — top {len(rows_iter)}",
        subtitle=None,
        period=_period(start, end, label="Recent sessions",
                       display_tz=_display_tz(options)),
        columns=(),
        rows=(),
        chart=_LS.HorizontalBarChart(
            points=tuple(
                _LS.ChartPoint(
                    x_label=str(s.get("session_id") or ""),
                    x_value=float(i),
                    y_value=float(s.get("cost_usd") or 0.0),
                    project_label=str(s.get("project_path") or "") or None,
                )
                for i, s in enumerate(rows_iter)
            ),
            x_label="$",
            cap=None,
        ) if rows_iter else None,
        totals=_kpi_strip(
            ("Sum",   f"${sum_cost:,.2f}"),
            ("Shown", f"{len(rows_iter)}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


def _build_sessions_detail(*, panel_data, options):
    """Sessions detail — top-50 sessions with full columns + hbar chart (spec §9.5).

    Default `top_n` is 50 (Recap's is 15). Sessions Recap explicitly omits the
    chart (table-first panel); Detail re-introduces a compact horizontal bar
    of the same top-N so the archetype contract (chart populated + rows
    populated) holds uniformly across all 8 panels' Detail siblings.
    """
    sessions = panel_data.get("sessions") or []
    cap = options.get("top_n", 50)
    rows_iter = sessions[:cap]
    sum_cost = sum(float(s.get("cost_usd") or 0.0) for s in rows_iter)
    starts = [_parse_iso_utc(s["started_at"]) for s in rows_iter if s.get("started_at")]
    start = min(starts) if starts else _utc_now()
    end = max(starts) if starts else start
    return _LS.ShareSnapshot(
        cmd="session",
        title=f"Sessions detail — top {len(rows_iter)}",
        subtitle=None,
        period=_period(start, end, label="Recent sessions",
                       display_tz=_display_tz(options)),
        columns=(
            _LS.ColumnSpec(key="started", label="Started",  align="left"),
            _LS.ColumnSpec(key="project", label="Project",  align="left"),
            _LS.ColumnSpec(key="model",   label="Model",    align="left"),
            _LS.ColumnSpec(key="cost",    label="$",        align="right",
                           emphasis=True),
        ),
        rows=tuple(
            _LS.Row(cells={
                "started": (_LS.DateCell(when=_parse_iso_utc(s["started_at"]))
                            if s.get("started_at")
                            else _LS.TextCell("")),
                "project": _LS.ProjectCell(label=str(s.get("project_path") or "")),
                "model":   _LS.TextCell(str(s.get("model") or "")),
                "cost":    _LS.MoneyCell(float(s.get("cost_usd") or 0.0)),
            })
            for s in rows_iter
        ),
        chart=_LS.HorizontalBarChart(
            points=tuple(
                _LS.ChartPoint(
                    x_label=str(s.get("session_id") or ""),
                    x_value=float(i),
                    y_value=float(s.get("cost_usd") or 0.0),
                    project_label=str(s.get("project_path") or "") or None,
                )
                for i, s in enumerate(rows_iter)
            ),
            x_label="$",
            cap=None,
        ) if rows_iter else None,
        totals=_kpi_strip(
            ("Sum",     f"${sum_cost:,.2f}"),
            ("Shown",   f"{len(rows_iter)}"),
        ),
        notes=(),
        generated_at=_utc_now(),
        version=_release_version(),
    )


# --- Register Recap templates ---

_RECAP = (
    ShareTemplate(id="weekly-recap", panel="weekly", label="Recap",
                  description="Balanced KPIs + chart + top projects",
                  default_options={"top_n": 5, "show_chart": True, "show_table": True},
                  builder=_build_weekly_recap),
    ShareTemplate(id="current-week-recap", panel="current-week", label="Recap",
                  description="Week-to-date KPIs + line + top-3 projects",
                  default_options={"top_n": 3, "show_chart": True, "show_table": True},
                  builder=_build_current_week_recap),
    ShareTemplate(id="trend-recap", panel="trend", label="Recap",
                  description="$/% trend over 8 weeks + 3-week delta",
                  default_options={"top_n": 3, "show_chart": True, "show_table": True},
                  builder=_build_trend_recap),
    ShareTemplate(id="daily-recap", panel="daily", label="Recap",
                  description="7-day cost bar + top-5 projects",
                  default_options={"top_n": 5, "show_chart": True, "show_table": True},
                  builder=_build_daily_recap),
    ShareTemplate(id="monthly-recap", panel="monthly", label="Recap",
                  description="Per-month bar + KPI + top projects",
                  default_options={"top_n": 5, "show_chart": True, "show_table": True},
                  builder=_build_monthly_recap),
    ShareTemplate(id="blocks-recap", panel="blocks", label="Recap",
                  description="Current block KPI + recent-blocks line + top-3",
                  default_options={"top_n": 3, "show_chart": True, "show_table": True},
                  builder=_build_blocks_recap),
    ShareTemplate(id="forecast-recap", panel="forecast", label="Recap",
                  description="Projection + budget table + days-to-ceiling",
                  default_options={"top_n": 5, "show_chart": True, "show_table": True},
                  builder=_build_forecast_recap),
    ShareTemplate(id="sessions-recap", panel="sessions", label="Recap",
                  description="Top-N sessions table + total",
                  default_options={"top_n": 15, "show_chart": False, "show_table": True},
                  builder=_build_sessions_recap),
)

SHARE_TEMPLATES = SHARE_TEMPLATES + _RECAP


# --- Register Visual templates ---

_VISUAL = (
    ShareTemplate(id="weekly-visual", panel="weekly", label="Visual",
                  description="Chart-first 8-week cost trend",
                  default_options={"top_n": 8, "show_chart": True, "show_table": False},
                  builder=_build_weekly_visual),
    ShareTemplate(id="current-week-visual", panel="current-week", label="Visual",
                  description="Week-to-date line with KPI overlay",
                  default_options={"top_n": 8, "show_chart": True, "show_table": False},
                  builder=_build_current_week_visual),
    ShareTemplate(id="trend-visual", panel="trend", label="Visual",
                  description="$/% trend line with budget reference",
                  default_options={"top_n": 8, "show_chart": True, "show_table": False},
                  builder=_build_trend_visual),
    ShareTemplate(id="daily-visual", panel="daily", label="Visual",
                  description="Stacked bar by model across 7 days",
                  default_options={"top_n": 8, "show_chart": True, "show_table": False},
                  builder=_build_daily_visual),
    ShareTemplate(id="monthly-visual", panel="monthly", label="Visual",
                  description="Month-over-month line",
                  default_options={"top_n": 8, "show_chart": True, "show_table": False},
                  builder=_build_monthly_visual),
    ShareTemplate(id="blocks-visual", panel="blocks", label="Visual",
                  description="Burndown gauge + recent-blocks stacked bar",
                  default_options={"top_n": 8, "show_chart": True, "show_table": False},
                  builder=_build_blocks_visual),
    ShareTemplate(id="forecast-visual", panel="forecast", label="Visual",
                  description="Projection with 90/100% ceilings",
                  default_options={"top_n": 8, "show_chart": True, "show_table": False},
                  builder=_build_forecast_visual),
    ShareTemplate(id="sessions-visual", panel="sessions", label="Visual",
                  description="Horizontal bar of top-N sessions by cost",
                  default_options={"top_n": 8, "show_chart": True, "show_table": False},
                  builder=_build_sessions_visual),
)


# --- Register Detail templates ---

_DETAIL = (
    ShareTemplate(id="weekly-detail", panel="weekly", label="Detail",
                  description="Per-week × per-model cross-tab",
                  default_options={"top_n": 5, "show_chart": True, "show_table": True},
                  builder=_build_weekly_detail),
    ShareTemplate(id="current-week-detail", panel="current-week", label="Detail",
                  description="Per-project table + sidebar chart",
                  default_options={"top_n": 50, "show_chart": True, "show_table": True},
                  builder=_build_current_week_detail),
    ShareTemplate(id="trend-detail", panel="trend", label="Detail",
                  description="8-week table with $/%/rate columns + sparkline",
                  default_options={"top_n": 50, "show_chart": True, "show_table": True},
                  builder=_build_trend_detail),
    ShareTemplate(id="daily-detail", panel="daily", label="Detail",
                  description="Per-day × per-project cross-tab",
                  default_options={"top_n": 5, "show_chart": True, "show_table": True},
                  builder=_build_daily_detail),
    ShareTemplate(id="monthly-detail", panel="monthly", label="Detail",
                  description="Per-month × per-model cross-tab",
                  default_options={"top_n": 5, "show_chart": True, "show_table": True},
                  builder=_build_monthly_detail),
    ShareTemplate(id="blocks-detail", panel="blocks", label="Detail",
                  description="Per-block × per-project cross-tab",
                  default_options={"top_n": 5, "show_chart": True, "show_table": True},
                  builder=_build_blocks_detail),
    ShareTemplate(id="forecast-detail", panel="forecast", label="Detail",
                  description="Per-day forecast table with $/% budget",
                  default_options={"top_n": 50, "show_chart": True, "show_table": True},
                  builder=_build_forecast_detail),
    ShareTemplate(id="sessions-detail", panel="sessions", label="Detail",
                  description="Top-50 sessions with full columns",
                  default_options={"top_n": 50, "show_chart": False, "show_table": True},
                  builder=_build_sessions_detail),
)

SHARE_TEMPLATES = SHARE_TEMPLATES + _VISUAL + _DETAIL

_validate_registry()
