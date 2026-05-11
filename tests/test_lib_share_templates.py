"""Tests for the share template registry."""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TPL_PATH = _HERE.parent / "bin" / "_lib_share_templates.py"
_SPEC = importlib.util.spec_from_file_location("_lib_share_templates", _TPL_PATH)
_T = importlib.util.module_from_spec(_SPEC)
sys.modules["_lib_share_templates"] = _T
_SPEC.loader.exec_module(_T)


def test_registry_has_unique_ids():
    ids = [t.id for t in _T.SHARE_TEMPLATES]
    assert len(ids) == len(set(ids)), f"duplicate template ids: {ids}"


def test_registry_covers_all_share_capable_panels():
    if not _T.SHARE_TEMPLATES:
        import pytest
        pytest.skip("registry not yet populated — re-enabled after M1.4")
    panels_in_registry = {t.panel for t in _T.SHARE_TEMPLATES}
    assert panels_in_registry == _T.SHARE_CAPABLE_PANELS, (
        f"panel coverage mismatch — extra: {panels_in_registry - _T.SHARE_CAPABLE_PANELS}, "
        f"missing: {_T.SHARE_CAPABLE_PANELS - panels_in_registry}"
    )


def test_alerts_panel_not_in_share_capable_set():
    assert "alerts" not in _T.SHARE_CAPABLE_PANELS, (
        "alerts panel is a notification stream, not a data view — no share registry"
    )


def test_share_template_dataclass_is_frozen():
    if not _T.SHARE_TEMPLATES:
        import pytest
        pytest.skip("registry not yet populated — re-enabled after M1.4")
    sample = _T.SHARE_TEMPLATES[0]
    import dataclasses
    assert dataclasses.is_dataclass(sample)
    try:
        sample.id = "tampered"
        raise AssertionError("ShareTemplate should be frozen")
    except dataclasses.FrozenInstanceError:
        pass


def _fake_weekly_panel_data():
    """Synthetic panel_data shape returned by bin/cctally's _build_weekly_share_snapshot
    when the source helper is reused."""
    return {
        "weeks": [
            {"start_date": "2026-05-04", "cost_usd": 14.27, "pct_used": 0.71,
             "dollar_per_pct": 0.20, "top_projects": [
                ("project/aa", 5.12), ("project/bb", 3.81), ("project/cc", 2.04)]},
        ],
        "current_week_index": 0,
    }


def test_weekly_recap_builder_emits_snapshot():
    tpl = _T.get_template("weekly-recap")
    snap = tpl.builder(panel_data=_fake_weekly_panel_data(),
                       options={"theme": "light", "reveal_projects": True,
                                "no_branding": False, "top_n": 5,
                                "show_chart": True, "show_table": True,
                                "project_allowlist": None})
    import dataclasses
    assert dataclasses.is_dataclass(snap)
    assert snap.cmd == "weekly"
    assert snap.title and snap.period and snap.generated_at
    # Recap should include both chart and table:
    assert snap.chart is not None
    assert len(snap.rows) > 0


# --- M2.1: Visual + Detail builders coverage ---
#
# 8 panels × 3 archetypes (recap/visual/detail) = 24 registered templates.
# The parameterized archetype-shape test below loops every registered
# template through its builder with a stub panel_data dict and asserts the
# produced ShareSnapshot's chart / rows populated state matches the
# archetype contract documented in spec §9.4.

import pytest


_ARCHETYPE_EXPECTATIONS = {
    # archetype suffix -> (chart_populated, rows_populated, rows_min)
    "recap":  (True,  True,  1),
    "visual": (True,  False, 0),
    "detail": (True,  True,  1),
}


_STUB_PANEL_DATA = {
    "weekly": {
        "weeks": [
            {"start_date": "2026-05-04", "cost_usd": 12.5, "pct_used": 0.45,
             "dollar_per_pct": 0.278, "top_projects": [("/p/a", 8.0), ("/p/b", 4.5)]},
        ],
        "current_week_index": 0,
    },
    "current-week": {
        "kpi_cost_usd": 8.0, "kpi_pct_used": 0.30, "kpi_dollar_per_pct": 0.267,
        "kpi_days_remaining": 3.5,
        "daily_progression": [{"date": "2026-05-04", "cost_usd": 2.0},
                               {"date": "2026-05-05", "cost_usd": 6.0}],
        "top_projects": [("/p/a", 5.0), ("/p/b", 3.0)],
        "week_start_date": "2026-05-04", "display_tz": "Etc/UTC",
    },
    "trend": {
        "weeks": [
            {"start_date": "2026-04-13", "cost_usd": 10.0, "pct_used": 0.40,
             "dollar_per_pct": 0.25},
            {"start_date": "2026-04-20", "cost_usd": 12.0, "pct_used": 0.45,
             "dollar_per_pct": 0.267},
        ],
    },
    "daily": {
        "days": [{"date": "2026-05-04", "cost_usd": 2.0}],
        "top_projects": [("/p/a", 1.5)],
    },
    "monthly": {
        "months": [{"month": "2026-04", "cost_usd": 50.0}],
        "top_projects": [("/p/a", 40.0)],
    },
    "blocks": {
        "current_block": {"start_at": "2026-05-04T10:00:00Z",
                          "end_at":   "2026-05-04T15:00:00Z",
                          "cost_usd": 3.0, "pct_used": 0.12,
                          "tokens_total": 1000},
        "recent_blocks": [{"start_at": "2026-05-04T10:00:00Z",
                            "cost_usd": 3.0}],
        "top_projects":  [("/p/a", 2.0)],
    },
    "forecast": {
        "projected_end_pct": 0.92, "days_to_100pct": 8.0, "days_to_90pct": 6.5,
        "daily_budgets": {"avg": 1.5, "recent_24h": 2.0,
                          "until_90pct": 2.5, "until_100pct": 3.0},
        "projection_curve": [
            {"date": "2026-05-04", "projected_pct_used": 0.45},
        ],
        "confidence": "ok",
    },
    "sessions": {
        "sessions": [{"session_id": "s1", "cost_usd": 5.0,
                       "project_path": "/p/a",
                       "started_at": "2026-05-04T09:00:00Z",
                       "model": "claude-sonnet-4-5"}],
    },
}


def test_template_registry_has_24_entries():
    assert len(_T.SHARE_TEMPLATES) == 24, (
        f"expected 24 templates (8 panels × 3 archetypes), got {len(_T.SHARE_TEMPLATES)}"
    )


# Documented exceptions to the generic archetype contract above.
# Spec §9.5 specifies sessions-recap as "Top-N table (default 15) + total" —
# explicitly no chart. The plan's parameterized archetype-shape table (which
# expects chart populated for every recap) and spec §9.5 disagree on this
# one cell; we honor the spec (and the M1.4 builder that already shipped)
# and carve out sessions-recap as the documented no-chart exception.
_ARCHETYPE_CHART_EXCEPTIONS: frozenset[str] = frozenset({"sessions-recap"})


@pytest.mark.parametrize("template", list(_T.SHARE_TEMPLATES), ids=lambda t: t.id)
def test_template_archetype_shape(template):
    archetype = template.id.rsplit("-", 1)[-1]
    assert archetype in _ARCHETYPE_EXPECTATIONS, (
        f"template id {template.id!r} does not end in a known archetype suffix"
    )
    chart_pop, rows_pop, rows_min = _ARCHETYPE_EXPECTATIONS[archetype]
    panel_data = _STUB_PANEL_DATA[template.panel]
    options = dict(template.default_options)
    options.setdefault("format", "html")
    options.setdefault("theme", "light")
    options.setdefault("reveal_projects", True)
    options.setdefault("no_branding", False)
    options.setdefault("show_chart", True)
    options.setdefault("show_table", archetype != "visual")
    snap = template.builder(panel_data=panel_data, options=options)
    if chart_pop and template.id not in _ARCHETYPE_CHART_EXCEPTIONS:
        assert snap.chart is not None, f"{template.id}: chart expected populated"
    elif not chart_pop:
        assert snap.chart is None, f"{template.id}: chart expected empty"
    # else: documented exception — no assertion on snap.chart shape.
    if rows_pop:
        assert len(snap.rows) >= rows_min, (
            f"{template.id}: rows expected populated (>= {rows_min}), got {len(snap.rows)}"
        )
    else:
        assert snap.rows == (), f"{template.id}: rows expected empty, got {len(snap.rows)}"
