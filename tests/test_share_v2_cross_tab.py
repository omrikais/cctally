"""Regression tests for share-v2 cross-tab Detail templates (issue #33).

Covers:
- Window-wide top-K aggregation determinism (lex tie-break)
- Per-row reconciliation across 4 affected Detail panels
- Project-column anonymization for daily-detail and blocks-detail
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EPSILON = 1e-9


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def share_lib():
    return _load("_lib_share", REPO_ROOT / "bin" / "_lib_share.py")


@pytest.fixture(scope="module")
def templates(share_lib):
    # _lib_share_templates imports _lib_share from the same directory via its
    # own loader; share_lib is loaded first so the module is in sys.modules.
    return _load("_lib_share_templates", REPO_ROOT / "bin" / "_lib_share_templates.py")


def _fixture_weekly_panel_data():
    """Synthetic weekly panel_data with 4 weeks and a per-week per-model breakdown."""
    return {
        "weeks": [
            {"start_date": "2026-03-23", "cost_usd": 50.0, "pct_used": 0.3, "dollar_per_pct": 1.67,
             "top_projects": [], "models": {"opus": 30.0, "sonnet": 15.0, "haiku": 5.0}},
            {"start_date": "2026-03-30", "cost_usd": 80.0, "pct_used": 0.5, "dollar_per_pct": 1.60,
             "top_projects": [], "models": {"opus": 60.0, "sonnet": 20.0}},
            {"start_date": "2026-04-06", "cost_usd": 100.0, "pct_used": 0.6, "dollar_per_pct": 1.67,
             "top_projects": [], "models": {"opus": 70.0, "sonnet": 25.0, "haiku": 5.0}},
            {"start_date": "2026-04-13", "cost_usd": 120.0, "pct_used": 0.7, "dollar_per_pct": 1.71,
             "top_projects": [], "models": {"opus": 100.0, "sonnet": 20.0}},
        ],
        "current_week_index": 3,
    }


def _fixture_daily_panel_data():
    """Synthetic daily panel_data with 3 days and a per-day per-project breakdown.

    Includes two real-looking project paths so anonymization can be verified.
    """
    return {
        "days": [
            {"date": "2026-04-10", "cost_usd": 12.0, "pct_of_period": 0.20, "top_model": "opus",
             "projects": {"/home/me/secret-project": 10.0, "/home/me/other-project": 2.0}},
            {"date": "2026-04-11", "cost_usd": 18.0, "pct_of_period": 0.30, "top_model": "opus",
             "projects": {"/home/me/secret-project": 15.0, "/home/me/other-project": 3.0}},
            {"date": "2026-04-12", "cost_usd": 30.0, "pct_of_period": 0.50, "top_model": "opus",
             "projects": {"/home/me/secret-project": 25.0, "/home/me/other-project": 5.0}},
        ],
        "top_projects": [
            ("/home/me/secret-project", 50.0),
            ("/home/me/other-project", 10.0),
        ],
    }


def _fixture_monthly_panel_data():
    return {
        "months": [
            {"month": "2026-02", "cost_usd": 100.0, "pct_used": 0.0, "top_model": "opus",
             "models": {"opus": 70.0, "sonnet": 25.0, "haiku": 5.0}},
            {"month": "2026-03", "cost_usd": 200.0, "pct_used": 0.0, "top_model": "opus",
             "models": {"opus": 150.0, "sonnet": 40.0, "haiku": 10.0}},
            {"month": "2026-04", "cost_usd": 250.0, "pct_used": 0.0, "top_model": "opus",
             "models": {"opus": 200.0, "sonnet": 40.0, "haiku": 10.0}},
        ],
        "top_projects": [],
    }


def _fixture_blocks_panel_data():
    return {
        "current_block": {
            "start_at": "2026-05-11T09:00:00+00:00",
            "end_at": "2026-05-11T14:00:00+00:00",
            "cost_usd": 12.0,
            "pct_used": 0.30,
            "tokens_total": 100000,
        },
        "recent_blocks": [
            {"start_at": "2026-05-11T04:00:00+00:00", "cost_usd": 8.0,
             "projects": {"/home/me/secret-project": 6.0, "/home/me/other-project": 2.0}},
            {"start_at": "2026-05-11T09:00:00+00:00", "cost_usd": 12.0,
             "projects": {"/home/me/secret-project": 9.0, "/home/me/other-project": 3.0}},
        ],
        "top_projects": [
            ("/home/me/secret-project", 15.0),
            ("/home/me/other-project", 5.0),
        ],
    }


def test_aggregate_breakdowns_lex_tie_break(templates):
    breakdowns = [
        {"b-model": 10.0, "a-model": 10.0, "c-model": 5.0},
        {"a-model": 10.0, "b-model": 10.0},
    ]
    result = templates._aggregate_breakdowns(breakdowns)
    # Ties broken lex ascending → ("a-model", 20.0) before ("b-model", 20.0).
    assert [m[0] for m in result] == ["a-model", "b-model", "c-model"]
    assert result[0][1] == pytest.approx(20.0)
    assert result[1][1] == pytest.approx(20.0)
    assert result[2][1] == pytest.approx(5.0)


def _row_matrix_sum(row):
    """Sum every m_* and _other cell in a row."""
    return sum(
        cell.usd for k, cell in row.cells.items()
        if k.startswith("m_") or k == "_other"
    )


def test_weekly_detail_row_reconciles(templates):
    snap = templates._build_weekly_detail(
        panel_data=_fixture_weekly_panel_data(),
        options={"top_n": 5, "display_tz": "Etc/UTC"},
    )
    for row in snap.rows:
        total = row.cells["total"].usd
        assert abs(total - _row_matrix_sum(row)) < EPSILON


def test_daily_detail_row_reconciles(templates):
    snap = templates._build_daily_detail(
        panel_data=_fixture_daily_panel_data(),
        options={"top_n": 5, "display_tz": "Etc/UTC"},
    )
    for row in snap.rows:
        total = row.cells["total"].usd
        assert abs(total - _row_matrix_sum(row)) < EPSILON


def test_monthly_detail_row_reconciles(templates):
    snap = templates._build_monthly_detail(
        panel_data=_fixture_monthly_panel_data(),
        options={"top_n": 5, "display_tz": "Etc/UTC"},
    )
    for row in snap.rows:
        total = row.cells["total"].usd
        assert abs(total - _row_matrix_sum(row)) < EPSILON


def test_blocks_detail_row_reconciles(templates):
    snap = templates._build_blocks_detail(
        panel_data=_fixture_blocks_panel_data(),
        options={"top_n": 5, "display_tz": "Etc/UTC"},
    )
    for row in snap.rows:
        total = row.cells["total"].usd
        assert abs(total - _row_matrix_sum(row)) < EPSILON


def _render_all_formats(share_lib, snap):
    """Return md + html + svg rendered bodies for the snapshot."""
    palette = share_lib.PALETTE_LIGHT
    md_body = share_lib._render_md_fragment(snap, branding=False)
    html_body = share_lib._render_html_fragment(snap, palette=palette, branding=False)
    svg_inner, _w, _h = share_lib._render_svg_fragment(
        snap, palette=palette, branding=False,
    )
    return (md_body, html_body, svg_inner)


def test_daily_detail_anonymizes_project_columns(share_lib, templates):
    snap = templates._build_daily_detail(
        panel_data=_fixture_daily_panel_data(),
        options={"top_n": 5, "display_tz": "Etc/UTC"},
    )
    scrubbed = share_lib._scrub(snap, reveal_projects=False)
    # Every kind='project' column must be anonymized or sentinel.
    for col in scrubbed.columns:
        if col.kind == "project":
            assert col.label.startswith("project-") or col.label == "(unknown)", \
                f"unanonymized project column: {col.label!r}"
    # No real project path in any rendered body.
    for body in _render_all_formats(share_lib, scrubbed):
        assert "/home/me/secret-project" not in body, "raw project path leaked"
        assert "/home/me/other-project" not in body, "raw project path leaked"


def test_blocks_detail_anonymizes_project_columns(share_lib, templates):
    snap = templates._build_blocks_detail(
        panel_data=_fixture_blocks_panel_data(),
        options={"top_n": 5, "display_tz": "Etc/UTC"},
    )
    scrubbed = share_lib._scrub(snap, reveal_projects=False)
    for col in scrubbed.columns:
        if col.kind == "project":
            assert col.label.startswith("project-") or col.label == "(unknown)"
    for body in _render_all_formats(share_lib, scrubbed):
        assert "/home/me/secret-project" not in body
        assert "/home/me/other-project" not in body
