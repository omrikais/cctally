"""#661 S2 Task D2 — the status line's 7d projection and rate-change marker.

Spec section 9. The 7d slot gains the projected end-of-week percent with the
basis it came from, and a marker while section 6.6's predicate holds.

What this module owns is the COST contract as much as the rendering. This
surface runs once per prompt, so it must never take a flock, never open
`cache.db` for quota, and never call `analyse`. Each of those is asserted by
making the forbidden call explode rather than by reading the source for its
name, because a name-channel assertion is blind to a transitive call.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json

import pytest

from conftest import load_script

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
NOW_EPOCH = int(NOW.timestamp())
#: Three days into a seven-day window, so four days remain.
RESETS_AT = NOW_EPOCH + 4 * 24 * 3600
BOUNDARY = "2026-08-25T00:00:00+00:00"


def _sl(ns):
    return ns["_load_sibling"]("_lib_statusline")


def _inputs(sl, *, seven_pct=40.0, seven_resets=RESETS_AT):
    return sl.StatuslineInput(
        rate_limits_5h_pct=None, rate_limits_5h_resets_at=None,
        rate_limits_7d_pct=seven_pct, rate_limits_7d_resets_at=seven_resets)


def _injections(sl, *, regimes=()):
    return sl.StatuslineInjections(
        cctally_session_cost=lambda _sid: None,
        today_cost=lambda _tz, _now: 0.0,
        active_block=lambda _now: None,
        hwm_clamp=lambda _f, _s: (None, None),
        db_latest_rate_limits=lambda: None,
        context_pct=lambda _p, _m: None,
        warn_once=lambda _m: None,
        quota_regimes=lambda: tuple(regimes),
    )


def _segment(ns, *, seven_pct=40.0, seven_resets=RESETS_AT, regimes=()):
    sl = _sl(ns)
    return sl.resolve_cctally_extensions(
        _inputs(sl, seven_pct=seven_pct, seven_resets=seven_resets),
        NOW, _injections(sl, regimes=regimes))


def _regime_pair():
    """A closed predecessor and the open successor that follows it."""
    return [
        {"effectiveFrom": "2026-07-25T00:00:00+00:00",
         "effectiveUntil": BOUNDARY, "unitsPerPoint": 2_442_620.0,
         "status": "ok"},
        {"effectiveFrom": BOUNDARY, "effectiveUntil": None,
         "unitsPerPoint": 1_685_000.0, "status": "insufficient-history"},
    ]


# --------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------
def test_d2_the_seven_day_slot_carries_a_projection_and_its_basis():
    """40% displayed corrects to 39.5, three days into a seven-day window,
    so the pace projects 39.5 * 7/3 = 92.2% by the reset."""
    ns = load_script()
    out = _segment(ns)
    assert "7d 40%" in out
    assert "→ 92% meter" in out, out


def test_d2_basis_is_stated_when_the_meter_is_used():
    """Section 9's acceptance criterion is worded as basis-AWARE, not as
    always model-backed: the token names which measurement produced it."""
    ns = load_script()
    out = _segment(ns)
    assert "meter" in out
    assert "model" not in out


def test_d2_the_meter_word_comes_from_the_shared_basis_copy(monkeypatch):
    """A consumer-local basis map would keep printing `meter` after the
    shared presentation changed, which is the drift #676 closes."""
    ns = load_script()
    copy = ns["_load_sibling"]("_lib_quota_copy")
    monkeypatch.setitem(
        copy._BASIS_FORM["corrected-meter"], "short", "gauge")
    assert "→ 92% gauge" in _segment(ns)


def test_d2_the_projection_measures_from_the_corrected_reading():
    """The discriminating twin of the test above. Projecting from the RAW
    40 would give 93.3% and round to 93; the corrected 39.5 gives 92.2%.
    Without this the slot could publish a corrected rate on a raw base."""
    ns = load_script()
    sl = _sl(ns)
    raw_projection = 40.0 * (7.0 / 3.0)
    assert int(round(raw_projection)) == 93
    assert "→ 92% meter" in _segment(ns)
    assert sl is not None


def test_d2_a_right_censored_reading_withholds_in_the_short_register():
    """A displayed 100 denotes `[99, +inf)` and has no point estimate, so
    there is nothing to project from and none is invented. The cause is
    rendered through the section 8 copy table's short form."""
    ns = load_script()
    out = _segment(ns, seven_pct=100.0)
    assert "→ right censored" in out, out
    assert "%" in out.split("→")[0]


def test_d2_an_unknown_reset_leaves_the_slot_the_shape_it_had():
    """With no reset epoch there is no window to project over, so the slot
    renders no token rather than a withholding on every prompt."""
    ns = load_script()
    out = _segment(ns, seven_resets=None)
    assert out == "7d 40%", out


def test_d2_a_zero_reading_still_projects():
    """Section 3.6: zero is not censored. It denotes `[0, 0.5)`, so it
    projects from the corrected point of 0.25 exactly as any other
    reading."""
    ns = load_script()
    out = _segment(ns, seven_pct=0.0)
    assert "→ 1% meter" in out, out


def test_d2_a_window_too_young_for_a_pace_renders_no_projection():
    """Not a new threshold: `ForecastConfidenceCause.ELAPSED_HOURS` is the
    estate's existing rule for when a week-pace projection is
    low-confidence, and the status line has no room for the `LOW CONF`
    qualifier `forecast` prints beside one. Ten hours into a week a 42%
    reading projects to 697%, which is correct arithmetic and a useless
    figure to put on a per-prompt line."""
    ns = load_script()
    young = NOW_EPOCH + int((7 * 24 - 10) * 3600)
    out = _segment(ns, seven_pct=42.0, seven_resets=young)
    assert out == "7d 42% (6d 14h)", out


def test_d2_a_window_just_past_the_gate_does_project():
    """The discriminating twin. The gate is `elapsed < 24`, so a window
    with exactly 24 hours elapsed projects, and one an hour short does
    not."""
    ns = load_script()
    just_past = NOW_EPOCH + int((7 * 24 - 24) * 3600)
    just_short = NOW_EPOCH + int((7 * 24 - 23) * 3600)
    assert "→" in _segment(ns, seven_pct=42.0, seven_resets=just_past)
    assert "→" not in _segment(ns, seven_pct=42.0, seven_resets=just_short)


def test_d2_a_censored_reading_states_its_cause_even_in_a_young_window():
    """The withholding is a statement about the READING and does not depend
    on how much of the window has elapsed, so it is rendered before the
    confidence gate."""
    ns = load_script()
    young = NOW_EPOCH + int((7 * 24 - 10) * 3600)
    out = _segment(ns, seven_pct=100.0, seven_resets=young)
    assert "→ right censored" in out, out


# --------------------------------------------------------------------------
# The rate-change marker (section 6.6)
# --------------------------------------------------------------------------
def test_d2_the_marker_shows_while_the_successor_regime_is_open():
    ns = load_script()
    out = _segment(ns, regimes=_regime_pair())
    assert "Δrate" in out, out


def test_d2_no_marker_without_a_confirmed_predecessor():
    """A new user's first fit is not a transition, so the marker must not
    appear for every install that has ever run `cctally quota`."""
    ns = load_script()
    out = _segment(ns, regimes=_regime_pair()[1:])
    assert "Δrate" not in out, out


def test_d2_no_marker_once_the_successor_closes():
    """Derived, not latched: the marker clears on its own."""
    ns = load_script()
    closed, successor = _regime_pair()
    successor = dict(successor, effectiveUntil="2026-09-01T00:00:00+00:00")
    out = _segment(ns, regimes=[closed, successor])
    assert "Δrate" not in out, out


def test_d2_a_port_that_raises_never_takes_the_prompt_down():
    ns = load_script()
    sl = _sl(ns)

    def _boom():
        raise OSError("nope")

    inj = sl.StatuslineInjections(
        cctally_session_cost=lambda _sid: None,
        today_cost=lambda _tz, _now: 0.0,
        active_block=lambda _now: None,
        hwm_clamp=lambda _f, _s: (None, None),
        db_latest_rate_limits=lambda: None,
        context_pct=lambda _p, _m: None,
        warn_once=lambda _m: None,
        quota_regimes=_boom,
    )
    out = sl.resolve_cctally_extensions(_inputs(sl), NOW, inj)
    assert "7d 40%" in out
    assert "Δrate" not in out


# --------------------------------------------------------------------------
# The cost contract (section 9)
# --------------------------------------------------------------------------
def test_d2_read_takes_no_lock(monkeypatch, tmp_path):
    """A flock here would let a writer stall a prompt.

    `save_calibrations` writes through `os.replace` in the same directory,
    so a lock-free reader always sees a complete old or new inode.
    """
    ns = load_script()
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    _write_calibration(ns)

    def _boom(*_a, **_k):
        raise AssertionError("the status line took a flock")

    monkeypatch.setattr(fcntl, "flock", _boom)
    port = _build_real_port(ns)
    assert len(port()) == 2


def test_d2_never_opens_cache_db_for_quota(monkeypatch, tmp_path):
    """Section 9 states this outright. Asserted by making the call explode
    rather than by grepping the source for its name."""
    ns = load_script()
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    _write_calibration(ns)

    cache = ns["_load_sibling"]("_cctally_cache")

    def _boom(*_a, **_k):
        raise AssertionError("the status line opened cache.db for quota")

    monkeypatch.setattr(cache, "open_cache_db", _boom)
    port = _build_real_port(ns)
    assert len(port()) == 2


def test_d2_never_calls_analyse(monkeypatch, tmp_path):
    """`analyse_account` runs two unbounded SQL reads plus a detector, and
    section 1 forbids any per-prompt surface from reaching it."""
    ns = load_script()
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    _write_calibration(ns)

    glue = ns["_load_sibling"]("_cctally_quota_model")

    def _boom(*_a, **_k):
        raise AssertionError("the status line called analyse_account")

    monkeypatch.setattr(glue, "analyse_account", _boom)
    port = _build_real_port(ns)
    assert len(port()) == 2


def test_d2_the_read_never_quarantines_a_malformed_file(monkeypatch,
                                                        tmp_path):
    """`load_calibrations` renames a malformed or version-ahead file aside.
    A status line that did that once per prompt would be a writer on the
    hottest path in the product, so this read must not use it."""
    ns = load_script()
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    glue = ns["_load_sibling"]("_cctally_quota_model")
    path = glue.calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    before = sorted(p.name for p in path.parent.iterdir())

    port = _build_real_port(ns)
    assert port() == ()
    assert sorted(p.name for p in path.parent.iterdir()) == before, (
        "the malformed calibration was renamed aside, which makes the "
        "status line a writer")


def test_d2_a_version_ahead_file_renders_no_marker_and_does_not_raise(
        monkeypatch, tmp_path):
    """One of section 13's required calibration-read states, on this
    consumer. Version-ahead, corrupt, absent and quarantined are
    indistinguishable here without scanning sidecars, and all four render
    the same thing."""
    ns = load_script()
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    glue = ns["_load_sibling"]("_cctally_quota_model")
    path = glue.calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schemaVersion": 9_999, "accounts": {}}),
                    encoding="utf-8")
    assert _build_real_port(ns)() == ()


def test_d2_an_absent_file_renders_no_marker(monkeypatch, tmp_path):
    ns = load_script()
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    assert _build_real_port(ns)() == ()


def test_d2_usage_only_keeps_its_two_reading_shape():
    """`--usage-only` is the compact form other tools embed, so it renders
    neither the projection nor the marker."""
    ns = load_script()
    sl = _sl(ns)
    out = sl.resolve_cctally_extensions(
        _inputs(sl), NOW, _injections(sl, regimes=_regime_pair()),
        include_countdowns=False, include_projection=False)
    assert out == "7d 40%", out


# --------------------------------------------------------------------------
# Helpers that touch the real port
# --------------------------------------------------------------------------
def _write_calibration(ns):
    glue = ns["_load_sibling"]("_cctally_quota_model")
    path = glue.calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schemaVersion": glue.CALIBRATION_STATE_SCHEMA_VERSION,
        "accounts": {"*": {"regimes": _regime_pair()}},
    }), encoding="utf-8")


def _build_real_port(ns):
    """The production port, pulled out of the real injections factory."""
    sl_glue = ns["_load_sibling"]("_cctally_statusline")
    return sl_glue._build_statusline_injections(lambda _m: None).quota_regimes


@pytest.mark.parametrize("shown,expected", [(1.0, "→ 2% meter"),
                                            (99.0, "→ 230% meter")])
def test_d2_the_projection_is_not_clamped_to_the_meter_ceiling(shown,
                                                               expected):
    """A projection past 100 is the point: it says the week is on course to
    exhaust the quota. Clamping it would hide exactly the case a user needs
    to see.

    The low case is the twin that keeps the high one honest, and its
    arithmetic is worth writing out because it is not the obvious one: a
    displayed 1 denotes `[0.5, 1.0)`, so the corrected point is 0.75, not
    0.5. Three days into the window that projects to 1.75 and renders 2.
    """
    ns = load_script()
    assert expected in _segment(ns, seven_pct=shown)


# --------------------------------------------------------------------------
# A closed window (#661 S2 review, finding E5)
# --------------------------------------------------------------------------
def test_d2_a_reset_already_in_the_past_renders_no_projection():
    """A stale reset epoch degenerated the projection instead of withholding.

    `remaining` clamps to zero and `elapsed` becomes the whole 168 hours, so
    the projected value equals the corrected reading and the line printed a
    projection over a window that has already closed. It is reachable from a
    DB-latest fallback row and from a long-idle machine, and neither of the
    two documented withholding states — an unknown reset, and a window under
    24 hours old — covers it.

    Withheld exactly the way an unknown reset is: no token at all, because
    there is no window to project over rather than a typed reason not to.
    """
    ns = load_script()
    out = _segment(ns, seven_resets=NOW_EPOCH - 3600)
    assert "\u2192" not in out, out
    assert out.startswith("7d 40%"), out


def test_d2_a_reset_at_this_instant_renders_no_projection():
    """The boundary. A window whose reset is exactly now has closed."""
    ns = load_script()
    out = _segment(ns, seven_resets=NOW_EPOCH)
    assert "\u2192" not in out, out
    assert out.startswith("7d 40%"), out


def test_d2_a_reset_one_second_ahead_is_still_an_open_window():
    """The discriminating twin. A guard written as `remaining <= 0` after the
    clamp, or as `elapsed >= 168`, would swallow this case too. The window is
    open, 167 hours have elapsed, and the slot still projects."""
    ns = load_script()
    out = _segment(ns, seven_resets=NOW_EPOCH + 1)
    assert "→" in out, out
    assert "meter" in out, out


def test_d2_a_censored_reading_in_a_closed_window_renders_no_cause_either():
    """A withholding is a statement about the reading, and it is rendered
    before the confidence gate. A CLOSED window has no reading to make a
    statement about, so the closed-window guard sits above it — the same
    place the unknown-reset guard sits."""
    ns = load_script()
    out = _segment(ns, seven_pct=100.0, seven_resets=NOW_EPOCH - 3600)
    assert "\u2192" not in out, out
    assert "right censored" not in out, out
