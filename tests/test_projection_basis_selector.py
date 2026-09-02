"""#661 S2 Task A4 — one projection selector, two PROJECTED1 invariants.

Spec section 3.3. The forecast and the projected-pace alert twin must publish
the same number when they can reach the same basis. A calibrated alert reuses
the projection its existing week scan produced; when calibration cannot apply,
both consumers fall back to the corrected meter. Each leg carries its own
non-vacuity guard so equality cannot pass on the wrong basis.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

# conftest puts bin/ on sys.path.
import _lib_forecast as fc
import _lib_quota_model as qm
from conftest import load_isolated_cctally_module

UTC = dt.timezone.utc
WEEK_START = dt.datetime(2026, 5, 18, tzinfo=UTC)
WEEK_HOURS = 168


@pytest.fixture
def mod(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _inputs(*, p_now=40.0, elapsed_hours=84.0, remaining_hours=84.0,
            p_24h_ago=None, t_24h_actual_hours=None, **over):
    now = WEEK_START + dt.timedelta(hours=elapsed_hours)
    week_end = now + dt.timedelta(hours=remaining_hours)
    total = elapsed_hours + remaining_hours
    kwargs = dict(
        now_utc=now, week_start_at=WEEK_START, week_end_at=week_end,
        elapsed_hours=elapsed_hours,
        elapsed_fraction=(elapsed_hours / total if total else 0.0),
        remaining_hours=remaining_hours, remaining_days=remaining_hours / 24.0,
        p_now=p_now, five_hour_percent=None, spent_usd=10.0, snapshot_count=6,
        latest_snapshot_at=now, p_24h_ago=p_24h_ago,
        t_24h_actual_hours=t_24h_actual_hours, dollars_per_percent=1.0,
        dollars_per_percent_source="this_week", confidence="high",
        low_confidence_reasons=[],
    )
    kwargs.update(over)
    return fc.ForecastInputs(**kwargs)


# --------------------------------------------------------------------------
# The selector itself
# --------------------------------------------------------------------------
def test_a4_the_calibrated_basis_wins_when_it_is_available():
    out = fc.select_projection_basis(
        _inputs(p_now=40.0, calibrated_projection_pct=61.25))
    assert out.basis is fc.ProjectionBasis.CALIBRATED
    assert out.value == pytest.approx(61.25)
    assert out.code is None


def test_a4_the_corrected_meter_is_the_fallback():
    out = fc.select_projection_basis(_inputs(p_now=40.0))
    assert out.basis is fc.ProjectionBasis.CORRECTED_METER
    # 39.5 + (39.5/84)*84 == 79.0 — the CORRECTED reading, not 80.
    assert out.value == pytest.approx(79.0)
    assert out.code is None


def test_a4_a_right_censored_reading_withholds_with_a_typed_cause():
    out = fc.select_projection_basis(_inputs(p_now=100.0))
    assert out.basis is fc.ProjectionBasis.WITHHELD
    assert out.value is None
    assert out.code == "right-censored"


def test_a4_a_calibrated_value_survives_a_censored_meter():
    """The model reads tokens rather than the censored meter, so a
    trustworthy calibration still supplies a projection at 100%."""
    out = fc.select_projection_basis(
        _inputs(p_now=100.0, calibrated_projection_pct=140.0))
    assert out.basis is fc.ProjectionBasis.CALIBRATED
    assert out.value == pytest.approx(140.0)


def test_a4_a_non_finite_calibrated_value_is_not_adopted():
    for bad in (float("nan"), float("inf")):
        out = fc.select_projection_basis(
            _inputs(p_now=40.0, calibrated_projection_pct=bad))
        assert out.basis is fc.ProjectionBasis.CORRECTED_METER, bad


def test_a4_every_withheld_code_is_a_member_of_the_closed_union():
    for inputs in (_inputs(p_now=100.0),):
        out = fc.select_projection_basis(inputs)
        assert out.code in qm.EVIDENCE_CODES, out.code


def test_a4_compute_forecast_publishes_the_selected_basis_and_value():
    out = fc._compute_forecast(_inputs(p_now=40.0), [100])
    assert out.projection_basis == fc.ProjectionBasis.CORRECTED_METER.value
    assert out.week_avg_projection_pct == pytest.approx(79.0)

    out = fc._compute_forecast(
        _inputs(p_now=40.0, calibrated_projection_pct=61.25), [100])
    assert out.projection_basis == fc.ProjectionBasis.CALIBRATED.value
    assert out.week_avg_projection_pct == pytest.approx(61.25)


def test_a4_compute_forecast_reports_the_withheld_code_when_censored():
    out = fc._compute_forecast(_inputs(p_now=100.0), [100])
    assert out.projection_basis == fc.ProjectionBasis.WITHHELD.value
    assert out.projection_code == "right-censored"
    assert out.week_avg_projection_pct is None


# --------------------------------------------------------------------------
# Store helpers for the two PROJECTED1 legs
# --------------------------------------------------------------------------
def _seed_snapshots(conn, percents, *, week_start=WEEK_START):
    from _fixture_builders import seed_weekly_usage_snapshot

    week_end = week_start + dt.timedelta(hours=WEEK_HOURS)
    for offset_hours, pct in percents:
        captured = week_start + dt.timedelta(hours=offset_hours)
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=captured.isoformat(),
            week_start_date=week_start.date().isoformat(),
            week_end_date=week_end.date().isoformat(),
            week_start_at=week_start.isoformat(),
            week_end_at=week_end.isoformat(),
            weekly_percent=pct, source="statusline")
    conn.commit()


def _seed_entries(mod, rows):
    """`rows` is a list of (offset_hours, input_tokens)."""
    cache = mod._load_sibling("_cctally_cache").open_cache_db()
    try:
        for index, (offset_hours, tokens) in enumerate(rows):
            at = WEEK_START + dt.timedelta(hours=offset_hours)
            cache.execute(
                "INSERT INTO session_entries(source_path, line_offset,"
                " timestamp_utc, model, input_tokens, output_tokens,"
                " cache_create_tokens, cache_create_1h_tokens,"
                " cache_read_tokens) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"/fx/a4-{index}.jsonl", index, at.isoformat(),
                 "claude-opus-5", int(tokens), 0, 0, 0, 0))
        cache.commit()
    finally:
        cache.close()


def _write_calibration(mod, *, units_per_point=2_000_000.0):
    """A regime this binary will validate, matching the seeded entries."""
    import _cctally_core

    payload = {
        "schemaVersion": 1,
        "accounts": {"*": {"regimes": [{
            "effectiveFrom": (WEEK_START - dt.timedelta(days=30)).isoformat(),
            "effectiveUntil": None,
            "fingerprint": qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
            "algorithmRevision": qm.QUOTA_MODEL_ALGORITHM_REVISION,
            "unitsPerPoint": units_per_point,
            "interval": {"lo": units_per_point * 0.9,
                         "hi": units_per_point * 1.1},
            "support": {"days": 26, "segments": 4},
            "status": "ok",
            "asOf": WEEK_START.isoformat(),
            "qualifications": [],
            "familyShares": {"claude-opus-5": 1.0},
            "classShares": {"fresh": 1.0, "output": 0.0,
                            "cache_1h": 0.0, "cache_read": 0.0},
            "familyRadius": 0.02,
            "classRadius": 0.05,
        }]}},
    }
    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def store(mod):
    import _cctally_core

    conn = _cctally_core.open_db()
    cache = mod._load_sibling("_cctally_cache").open_cache_db()
    cache.close()
    yield mod, conn
    conn.close()


def _forecast_selection(mod, conn, now, *, account_key=None):
    inputs = mod._load_forecast_inputs(
        conn, now, skip_sync=True, account_key=account_key)
    assert inputs is not None
    return fc.select_projection_basis(inputs)


# --------------------------------------------------------------------------
# PROJECTED1-a — equality when both consumers reach the same basis
# --------------------------------------------------------------------------
def test_a4_projected1a_equality_when_both_reach_the_same_basis(
        store, monkeypatch):
    """The emitted week-average forecast and the value the projected-alert
    path fires on stay equal within 1e-9."""
    mod, conn = store
    # Recent 24h is deliberately much hotter than the week average, so the
    # displayed high end diverges from the week-average projection.
    _seed_snapshots(conn, [(0, 2.0), (48, 8.0), (76, 12.0), (84, 40.0)])
    monkeypatch.setattr(mod, "_sum_cost_for_range", lambda *a, **k: 20.0)
    now = WEEK_START + dt.timedelta(hours=84)

    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    out = mod._compute_forecast(inputs, [100, 90])
    emitted = out.week_avg_projection_pct
    detector = mod._weekly_pct_week_avg_projection(conn, now)
    assert detector is not None, "the detector must reach a projection here"
    assert abs(emitted - detector[0]) < 1e-9

    # Non-vacuity: an accidental comparison against the displayed high end
    # must still fail.
    assert abs(out.final_percent_high - emitted) > 1.0, "PROJECTED1-a VACUOUS"


def test_a4_projected1a_both_sides_are_on_the_meter_basis(store, monkeypatch):
    """Guard the guard: the equality above is only meaningful while both
    consumers are on the SAME basis."""
    mod, conn = store
    _seed_snapshots(conn, [(0, 2.0), (48, 8.0), (76, 12.0), (84, 40.0)])
    monkeypatch.setattr(mod, "_sum_cost_for_range", lambda *a, **k: 20.0)
    now = WEEK_START + dt.timedelta(hours=84)
    assert _forecast_selection(mod, conn, now).basis is \
        fc.ProjectionBasis.CORRECTED_METER


# --------------------------------------------------------------------------
# PROJECTED1-b — retain the calibrated basis the alert path already computed
# --------------------------------------------------------------------------
def _calibrated_store(mod, conn, monkeypatch):
    _seed_snapshots(conn, [(0, 2.0), (48, 8.0), (76, 12.0), (84, 40.0)])
    _seed_entries(mod, [(10, 20_000_000), (50, 20_000_000)])
    _write_calibration(mod)
    monkeypatch.setattr(mod, "_sum_cost_for_range", lambda *a, **k: 20.0)
    return WEEK_START + dt.timedelta(hours=84)


def test_a4_projected1b_alert_uses_the_reachable_calibrated_basis(
        store, monkeypatch):
    """The alert path already performs the calibrated week scan, so it must
    retain that projection rather than discard it and silently abstain."""
    mod, conn = store
    now = _calibrated_store(mod, conn, monkeypatch)
    selected = _forecast_selection(mod, conn, now)
    assert selected.basis is fc.ProjectionBasis.CALIBRATED
    projected = mod._weekly_pct_week_avg_projection(conn, now)
    assert projected is not None
    assert projected[0] == pytest.approx(selected.value)


def test_historical_forecast_ignores_reset_until_its_detection_instant(
        store, monkeypatch):
    """A replay sees only reset events detected by that instant, comparing
    mixed-offset timestamps as instants. At the inclusive detection boundary
    the same reset becomes eligible and re-anchors the forecast window."""
    mod, conn = store
    _seed_snapshots(conn, [(0, 2.0), (48, 8.0), (84, 12.0)])
    monkeypatch.setattr(mod, "_sum_cost_for_range", lambda *a, **k: 20.0)

    effective = WEEK_START + dt.timedelta(hours=72)
    detected = WEEK_START + dt.timedelta(hours=96)
    detected_offset = detected.astimezone(
        dt.timezone(dt.timedelta(hours=-4))).isoformat()
    end_iso = mod._normalize_week_boundary_dt(WEEK_START + dt.timedelta(
        hours=WEEK_HOURS)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
        (detected_offset,
         (WEEK_START + dt.timedelta(hours=144)).isoformat(),
         end_iso,
         effective.astimezone(
             dt.timezone(dt.timedelta(hours=5))).isoformat()),
    )
    conn.commit()

    before = mod._load_forecast_inputs(
        conn, WEEK_START + dt.timedelta(hours=60), skip_sync=True)
    assert before is not None
    assert before.week_start_at == WEEK_START

    at_detection = mod._load_forecast_inputs(
        conn, detected, skip_sync=True)
    assert at_detection is not None
    assert at_detection.week_start_at == effective


def test_a4_projected1b_is_not_vacuous(store, monkeypatch):
    """Guard the guard: if the forecast were also on the meter basis, the
    calibrated equality assertion would pass for the wrong reason."""
    mod, conn = store
    now = _calibrated_store(mod, conn, monkeypatch)
    assert _forecast_selection(mod, conn, now).basis is not \
        fc.ProjectionBasis.CORRECTED_METER


def test_a4_the_twin_still_fires_without_a_calibration(store, monkeypatch):
    """The same store without a calibration remains on the meter basis."""
    mod, conn = store
    now = _calibrated_store(mod, conn, monkeypatch)
    import _cctally_core

    (_cctally_core.APP_DIR / "quota-calibrations.json").unlink()
    assert mod._weekly_pct_week_avg_projection(conn, now) is not None
    assert _forecast_selection(mod, conn, now).basis is \
        fc.ProjectionBasis.CORRECTED_METER


def test_a4_an_unsupported_population_does_not_reach_the_calibrated_basis(
        store, monkeypatch):
    """The stored `status: "ok"` is not evidence about THIS week's
    composition, so a population outside the recorded radii falls back."""
    mod, conn = store
    _seed_snapshots(conn, [(0, 2.0), (48, 8.0), (76, 12.0), (84, 40.0)])
    _write_calibration(mod)
    cache = mod._load_sibling("_cctally_cache").open_cache_db()
    try:
        cache.execute(
            "INSERT INTO session_entries(source_path, line_offset,"
            " timestamp_utc, model, input_tokens, output_tokens,"
            " cache_create_tokens, cache_create_1h_tokens, cache_read_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("/fx/a4-haiku.jsonl", 0,
             (WEEK_START + dt.timedelta(hours=10)).isoformat(),
             "claude-haiku-4-5", 20_000_000, 0, 0, 0, 0))
        cache.commit()
    finally:
        cache.close()
    monkeypatch.setattr(mod, "_sum_cost_for_range", lambda *a, **k: 20.0)
    now = WEEK_START + dt.timedelta(hours=84)
    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    assert inputs.calibrated_projection_pct is None
    assert inputs.calibrated_withheld_code == "unsupported-composition"
    assert fc.select_projection_basis(inputs).basis is \
        fc.ProjectionBasis.CORRECTED_METER


def test_a4_a_stale_calibration_reports_its_cause_and_falls_back(
        store, monkeypatch):
    mod, conn = store
    now = _calibrated_store(mod, conn, monkeypatch)
    import _cctally_core

    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["accounts"]["*"]["regimes"][0]["fingerprint"] = "EARLIER"
    path.write_text(json.dumps(payload), encoding="utf-8")
    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    assert inputs.calibrated_projection_pct is None
    assert inputs.calibrated_withheld_code == "stale"


def test_a4_a_detection_only_calibration_is_refused_and_states_its_cause(
        store, monkeypatch):
    """S1 marks a successor regime `detection-only` while its fit is below the
    prediction gate, which is S1 saying it must not be used to predict. The
    forecast falls back to the corrected meter and names the regime's own
    blocking status (spec §1.1)."""
    mod, conn = store
    now = _calibrated_store(mod, conn, monkeypatch)
    import _cctally_core

    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    regime = payload["accounts"]["*"]["regimes"][0]
    regime["qualifications"] = ["successor-regime", "detection-only"]
    regime["status"] = "insufficient-history"
    path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    assert inputs.calibrated_projection_pct is None
    assert inputs.calibrated_withheld_code == "insufficient-history"
    assert fc.select_projection_basis(inputs).basis is \
        fc.ProjectionBasis.CORRECTED_METER


def test_a4_the_detection_only_cause_is_the_regimes_own_status(
        store, monkeypatch):
    """Guard the guard above: the code is read from the store rather than
    hardcoded, so a differently-blocked fit states a different cause."""
    mod, conn = store
    now = _calibrated_store(mod, conn, monkeypatch)
    import _cctally_core

    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    regime = payload["accounts"]["*"]["regimes"][0]
    regime["qualifications"] = ["detection-only"]
    regime["status"] = "unstable-fit"
    path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    assert inputs.calibrated_withheld_code == "unstable-fit"
    assert inputs.calibrated_withheld_code in qm.EVIDENCE_CODES


def test_a4_the_json_states_why_the_calibrated_basis_was_not_reached(
        store, monkeypatch):
    """A surface that falls back without naming a cause is silently falling
    back, which is what section 1.1 forbids."""
    mod, conn = store
    now = _calibrated_store(mod, conn, monkeypatch)
    import _cctally_core

    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["accounts"]["*"]["regimes"][0]["qualifications"] = [
        "successor-regime", "detection-only"]
    payload["accounts"]["*"]["regimes"][0]["status"] = "insufficient-history"
    path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    out = mod._compute_forecast(inputs, [100, 90])
    body = mod._build_forecast_json_payload(out)
    assert body["forecast"]["projection_basis"] == "corrected-meter"
    assert body["forecast"]["calibration_code"] == "insufficient-history"


def test_a4_the_json_calibration_code_is_null_on_the_calibrated_basis(
        store, monkeypatch):
    mod, conn = store
    now = _calibrated_store(mod, conn, monkeypatch)
    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    out = mod._compute_forecast(inputs, [100, 90])
    body = mod._build_forecast_json_payload(out)
    assert body["forecast"]["projection_basis"] == "calibrated"
    assert body["forecast"]["calibration_code"] is None


# --------------------------------------------------------------------------
# PROJECTED1-b selects the BASIS, not mere calibration readability
# --------------------------------------------------------------------------
def test_a4_the_twin_fires_when_the_forecast_falls_back_to_the_meter(
        store, monkeypatch):
    """Basis selection depends on what this week's population supports, not
    merely on whether a stored regime validates.

    Here a calibration validates but this week's population is outside its
    recorded radii, so the forecast is on `corrected-meter` — a basis the
    alert path reaches perfectly well. Withholding anyway would silently
    disable the weekly 90%/100% projected alert, and `EMPTY_POPULATION` makes
    that reachable at the start of every week.
    """
    mod, conn = store
    _seed_snapshots(conn, [(0, 2.0), (48, 8.0), (76, 12.0), (84, 40.0)])
    _write_calibration(mod)
    cache = mod._load_sibling("_cctally_cache").open_cache_db()
    try:
        cache.execute(
            "INSERT INTO session_entries(source_path, line_offset,"
            " timestamp_utc, model, input_tokens, output_tokens,"
            " cache_create_tokens, cache_create_1h_tokens, cache_read_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("/fx/a4-haiku-twin.jsonl", 0,
             (WEEK_START + dt.timedelta(hours=10)).isoformat(),
             "claude-haiku-4-5", 20_000_000, 0, 0, 0, 0))
        cache.commit()
    finally:
        cache.close()
    monkeypatch.setattr(mod, "_sum_cost_for_range", lambda *a, **k: 20.0)
    now = WEEK_START + dt.timedelta(hours=84)

    assert _forecast_selection(mod, conn, now).basis is \
        fc.ProjectionBasis.CORRECTED_METER, "non-vacuity: the forecast must "\
        "be on the basis the twin can reach, or this proves nothing"
    proj = mod._weekly_pct_week_avg_projection(conn, now)
    assert proj is not None, (
        "the twin withheld a basis it can reach; the projected weekly "
        "alert would never fire")


def test_a4_the_twin_fires_on_an_empty_population(store, monkeypatch):
    """`EMPTY_POPULATION` is the start of every week: a calibration validates
    and the week has no entries yet."""
    mod, conn = store
    _seed_snapshots(conn, [(0, 2.0), (48, 8.0), (76, 12.0), (84, 40.0)])
    _write_calibration(mod)
    monkeypatch.setattr(mod, "_sum_cost_for_range", lambda *a, **k: 20.0)
    now = WEEK_START + dt.timedelta(hours=84)

    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    assert inputs.calibrated_withheld_code == "no-local-history"
    assert mod._weekly_pct_week_avg_projection(conn, now) is not None


def test_a4_the_twin_emits_on_a_reachable_calibrated_basis(
        store, monkeypatch):
    """A reachable calibrated basis produces the same alert value."""
    mod, conn = store
    now = _calibrated_store(mod, conn, monkeypatch)
    selected = _forecast_selection(mod, conn, now)
    assert selected.basis is fc.ProjectionBasis.CALIBRATED
    projected = mod._weekly_pct_week_avg_projection(conn, now)
    assert projected is not None
    assert projected[0] == pytest.approx(selected.value)


def test_a4_the_twin_reads_the_calibration_for_the_account_it_was_given(
        store, monkeypatch):
    """The readability probe always read the MERGED bucket while the forecast
    used its own `account_key`. On a decorated multi-account install that let
    the forecast publish a calibrated projection while the twin saw no merged
    regime and fired on the meter — the exact PROJECTED1-b violation."""
    mod, conn = store
    now = _calibrated_store(mod, conn, monkeypatch)
    import _cctally_core

    # Move the regime out of the merged bucket and into a real account.
    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["accounts"] = {"acct-a": payload["accounts"]["*"]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    conn.execute(
        "UPDATE weekly_usage_snapshots SET account_key = 'acct-a'")
    conn.commit()
    cache = mod._load_sibling("_cctally_cache").open_cache_db()
    try:
        cache.execute("UPDATE session_entries SET account_key = 'acct-a'")
        cache.commit()
    finally:
        cache.close()

    # Merged: no regime, so the forecast is on the meter and so is the twin.
    assert mod._weekly_pct_week_avg_projection(conn, now) is not None
    # The account that owns the regime: the alert uses that account's own
    # calibrated projection rather than the merged meter fallback.
    selected = _forecast_selection(mod, conn, now, account_key="acct-a")
    account_projected = mod._weekly_pct_week_avg_projection(
        conn, now, account_key="acct-a")
    assert account_projected is not None
    assert selected.basis is fc.ProjectionBasis.CALIBRATED
    assert account_projected[0] == pytest.approx(selected.value)
