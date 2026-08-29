"""#661 S2 — every renderer publishes the kernel's SELECTED projection.

Spec section 3.2 forbids fabricating a projection from a censored meter "in
every renderer", and section 3.3 puts the selection in one place. Six sites
re-derived `p_now + r_avg * remaining` for themselves instead of reading
`ForecastOutput.week_avg_projection_pct`, and a committed dashboard golden
proved the cost: the `over` envelope published `week_avg_projection_pct: 126.0`
and `verdict: "capped"` while its own nested `forecast.explain` block said
`right_censored: true`, `projection_basis: "withheld"`,
`week_avg_projection_pct: null`. One envelope answered the same question two
contradictory ways, and the outer answer was invented from a censored meter.

These tests pin the CONSUMERS rather than the kernel: the view model
(`_forecast_projection_pcts`, which the dashboard envelope's legacy branch and
the TUI both call), the dashboard share panel builder, the share template,
the TUI's three render paths, `forecast --json`, the status line and the
terminal.

An earlier revision of this docstring claimed the dashboard envelope's legacy
branch was pinned here. No test in this module calls `snapshot_to_envelope`.
What is pinned is the helper that branch reads — `_forecast_projection_pcts`
at `_cctally_dashboard_envelope.py:1265` — plus the committed `over` dashboard
golden, which is what caught the contradiction in the first place.
"""
from __future__ import annotations

import datetime as dt

import pytest

# conftest puts bin/ on sys.path.
import _lib_forecast as fc
import _lib_view_models as vm

UTC = dt.timezone.utc
WEEK_START = dt.datetime(2026, 5, 18, tzinfo=UTC)


def _inputs(*, p_now, elapsed_hours=120.0, remaining_hours=48.0,
            p_24h_ago=None, t_24h_actual_hours=None,
            dollars_per_percent=1.0, **over):
    now = WEEK_START + dt.timedelta(hours=elapsed_hours)
    week_end = now + dt.timedelta(hours=remaining_hours)
    total = elapsed_hours + remaining_hours
    kwargs = dict(
        now_utc=now, week_start_at=WEEK_START, week_end_at=week_end,
        elapsed_hours=elapsed_hours,
        elapsed_fraction=(elapsed_hours / total if total else 0.0),
        remaining_hours=remaining_hours, remaining_days=remaining_hours / 24.0,
        p_now=p_now, five_hour_percent=None, spent_usd=42.0, snapshot_count=6,
        latest_snapshot_at=now, p_24h_ago=p_24h_ago,
        t_24h_actual_hours=t_24h_actual_hours,
        dollars_per_percent=dollars_per_percent,
        dollars_per_percent_source="this_week", confidence="high",
        low_confidence_reasons=[],
    )
    kwargs.update(over)
    return fc.ForecastInputs(**kwargs)


def _censored():
    return fc._compute_forecast(_inputs(p_now=105.0), [100, 90])


def _healthy():
    return fc._compute_forecast(
        _inputs(p_now=40.0, p_24h_ago=30.0, t_24h_actual_hours=24.0), [100, 90])


# --------------------------------------------------------------------------
# The view model — the header the dashboard publishes
# --------------------------------------------------------------------------
def test_the_view_model_publishes_the_kernels_selected_projection():
    out = _healthy()
    week_avg, _recent = vm._forecast_projection_pcts(out)
    assert week_avg == pytest.approx(out.week_avg_projection_pct)


def test_the_view_model_withholds_the_projection_at_a_censored_reading():
    """The committed `over` golden's contradiction, asserted directly."""
    week_avg, recent = vm._forecast_projection_pcts(_censored())
    assert week_avg is None
    assert recent is None


def test_the_header_projection_is_absent_at_a_censored_reading():
    out = _censored()
    week_avg, recent = vm._forecast_projection_pcts(out)
    verdict = vm._forecast_dashboard_verdict_of(out)
    assert verdict == "capped"
    assert vm._forecast_header_projection_pct(week_avg, recent,
                                              verdict) is None


def test_the_view_model_still_publishes_a_recent_24h_projection():
    """Guard the guard: the withholding above is caused by the censoring, not
    by the second method being dead."""
    _week_avg, recent = vm._forecast_projection_pcts(_healthy())
    assert recent is not None


def test_the_view_model_adopts_a_calibrated_projection_verbatim():
    """A calibrated projection reads tokens rather than the meter, so it is
    not re-derivable from `p_now` and `r_avg` at all. A consumer that
    re-derives publishes the meter's answer under the model's label."""
    out = fc._compute_forecast(
        _inputs(p_now=40.0, calibrated_projection_pct=61.25), [100])
    week_avg, _recent = vm._forecast_projection_pcts(out)
    assert week_avg == pytest.approx(61.25)


# --------------------------------------------------------------------------
# The dashboard share panel builder (`_cctally_dashboard_share`)
# --------------------------------------------------------------------------
class _Snap:
    def __init__(self, forecast):
        self.forecast = forecast
        self.forecast_view = None


def _panel(mod, output):
    return mod._build_forecast_share_panel_data({}, _Snap(output))


@pytest.fixture
def share_mod(tmp_path, monkeypatch):
    from conftest import load_isolated_cctally_module

    load_isolated_cctally_module(tmp_path, monkeypatch)
    import importlib

    return importlib.import_module("_cctally_dashboard_share")


def test_the_share_panel_projects_from_the_corrected_base(share_mod):
    """The site paired a RAW `p_now` with the corrected rate the kernel
    publishes, which is neither operand's answer."""
    out = _healthy()
    panel = _panel(share_mod, out)
    assert panel["projected_end_pct"] == pytest.approx(
        out.week_avg_projection_pct / 100.0)


def test_the_share_panel_withholds_everything_meter_derived_when_censored(
        share_mod):
    panel = _panel(share_mod, _censored())
    assert panel["projected_end_pct"] is None
    assert panel["projection_curve"] == []
    assert panel["days_to_100pct"] is None
    assert panel["days_to_90pct"] is None


def test_the_share_panel_curve_starts_at_the_corrected_base(share_mod):
    out = _healthy()
    panel = _panel(share_mod, out)
    first = panel["projection_curve"][0]["projected_pct_used"]
    assert first == pytest.approx(fc.projection_base(out.inputs) / 100.0)


def test_the_share_panel_still_reaches_a_ceiling_distance(share_mod):
    """Guard the guard: the withholding above is the censoring, not a dead
    branch."""
    panel = _panel(share_mod, _healthy())
    assert panel["days_to_100pct"] is not None


def test_the_share_template_renders_a_withheld_end_percent():
    import _lib_share_templates as tpl

    assert tpl._optional_fraction_percent(None) == "n/a"
    assert tpl._optional_fraction_percent(0.865) == "86.5%"


# --------------------------------------------------------------------------
# The TUI's three render paths (spec section 13's censored consumer)
# --------------------------------------------------------------------------
@pytest.fixture
def tui(tmp_path, monkeypatch):
    from conftest import load_isolated_cctally_module

    load_isolated_cctally_module(tmp_path, monkeypatch)
    import importlib

    return importlib.import_module("_cctally_tui")


def _runtime(tui_mod):
    return tui_mod.RuntimeState(
        variant="conventional", focus_index=0, session_scroll=0,
        show_help=False, toast=None, color_enabled=False, tz="utc")


def _snapshot(tui_mod, output):
    return tui_mod.DataSnapshot(
        current_week=None, forecast=output, trend=[], sessions=[],
        generated_at=WEEK_START + dt.timedelta(hours=120),
        last_sync_at=None, last_sync_error=None,
    )


def test_the_tui_panel_states_the_withholding_rather_than_a_projection(tui):
    body = "\n".join(tui._tui_panel_forecast(
        _snapshot(tui, _censored()), _runtime(tui), 100))
    assert "Projection by week-avg" not in body
    assert "meter at its cap" in body


def test_the_tui_panel_still_renders_both_projections_when_it_can(tui):
    body = "\n".join(tui._tui_panel_forecast(
        _snapshot(tui, _healthy()), _runtime(tui), 100))
    assert "Projection by week-avg" in body
    assert "Projection by recent 24h" in body


def test_the_tui_modal_names_censoring_rather_than_a_missing_sample(tui):
    """The modal printed `r_recent unavailable — no 24h-prior sample` at a
    censored reading whose fixture HAS a 24-hour-prior sample. The stated
    cause was false."""
    output = fc._compute_forecast(
        _inputs(p_now=105.0, p_24h_ago=80.0, t_24h_actual_hours=24.0),
        [100, 90])
    _title, lines = tui._tui_modal_forecast(
        _snapshot(tui, output), _runtime(tui), 100)
    body = "\n".join(lines)
    assert "no 24h-prior sample" not in body
    assert "meter at its cap" in body


def test_the_tui_modal_labels_its_rate_operands_as_corrected(tui):
    """The hero band prints the DISPLAYED readings and the rate lines divide
    the CORRECTED ones, and nothing said so."""
    _title, lines = tui._tui_modal_forecast(
        _snapshot(tui, _healthy()), _runtime(tui), 100)
    body = "\n".join(lines)
    assert "Used now" in body
    assert "ceiling-corrected" in body


def test_the_marketing_demo_uses_one_projection_operand(tui):
    """The README demo hand-computed its projections from the raw reading
    while `_tui_panel_forecast` reads the corrected one, so the demo's panel
    and header could disagree by a point."""
    snap = tui.DataSnapshot.synthesize_for_marketing(
        as_of_iso="2026-05-07T13:00:00Z")
    output = snap.forecast
    base = fc.projection_base(output.inputs)
    assert output.week_avg_projection_pct == pytest.approx(
        base + output.r_avg * output.inputs.remaining_hours)
    assert output.final_percent_high == pytest.approx(
        base + output.r_avg * output.inputs.remaining_hours)
    assert output.budgets[0].pct_headroom == pytest.approx(100.0 - base)


# --------------------------------------------------------------------------
# The `forecast --json` schema bump (spec section 3.5)
# --------------------------------------------------------------------------
def test_forecast_json_carries_schema_version_two(tmp_path, monkeypatch):
    """`final_percent_low`, `final_percent_high` and `week_avg_projection_pct`
    changed from always-number to nullable AND changed meaning from
    raw-derived to corrected-derived. `docs/cli-contract.md` classifies both
    as breaking."""
    import json

    from conftest import load_isolated_cctally_module

    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    payload = json.loads(mod._emit_forecast_json(_healthy()))
    assert list(payload)[0] == "schemaVersion"
    assert payload["schemaVersion"] == 2


def _withheld_uncapped():
    """A withheld projection with the meter NOT at its cap.

    Spec section 3.6 removed the zero-reading withholding, so this state is
    now reached only through `unavailable`: the window supplies no span to
    project along. `_load_forecast_inputs` never produces it, which is the
    point — the kernel's contract is wider than the loader's current output,
    and the renderers below round `final_percent_low/high` unconditionally
    once they get past their verdict arms.
    """
    inputs = _inputs(p_now=40.0, elapsed_hours=78.0, remaining_hours=90.0,
                     dollars_per_percent=None)
    inputs.remaining_hours = None
    return fc._compute_forecast(inputs, [100, 90])


def test_the_status_line_renders_both_withheld_states(tmp_path, monkeypatch):
    """`_render_forecast_status_line` rounds `final_percent_low/high` below
    its verdict arms, so both withheld states must be caught first: censoring
    by the CAPPED arm, and a withheld-but-uncapped projection by its own
    arm."""
    from conftest import load_isolated_cctally_module

    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    assert "CAPPED" in mod._render_forecast_status_line(_censored(), False)

    empty = _withheld_uncapped()
    assert empty.projection_code == "unavailable"
    assert empty.final_percent_high is None
    assert empty.already_capped is False
    assert empty.inputs.confidence == "high", (
        "non-vacuity: this output must NOT be caught by the LOW CONF arm, or "
        "the withheld arm below it would never be exercised")
    assert "tracking" in mod._render_forecast_status_line(empty, False)


def test_the_terminal_renders_both_withheld_states(tmp_path, monkeypatch):
    """`_render_forecast_terminal` rounds `final_percent_low/high` in its
    forecast line, so both withheld states must be caught before it: the
    censored one by the CAPPED arm, and a withheld-but-uncapped projection by
    its own arm. The second is reachable for any caller-built inputs."""
    import argparse

    from conftest import load_isolated_cctally_module

    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    args = argparse.Namespace(color="never", explain=False, _resolved_tz=None,
                              targets="100,90")
    censored = mod._render_forecast_terminal(_censored(), args, False)
    assert "CAPPED" in censored

    empty = _withheld_uncapped()
    assert empty.inputs.confidence == "high", (
        "non-vacuity: this output must NOT be caught by the LOW CONF arm")
    body = mod._render_forecast_terminal(empty, args, False)
    assert "Forecast withheld" in body
    # A named sentence, not the generic fallback: every code the kernel can
    # emit has its own wording.
    assert "the week's window supplies no pace to project along" in body
    assert "the projection is unavailable" not in body
