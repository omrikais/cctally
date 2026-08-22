"""#620 S2 E3 — the forecast confidence predicate is kernel-resident.

`_assess_forecast_confidence` lived in `bin/_cctally_forecast.py` and knew
only three of its four triggers; `_load_forecast_inputs` appended the fourth
(`no_sample_ge_24h`) after calling it, so glue could add reasons the
predicate did not know about. E3 moves the whole predicate, including that
fourth trigger, into the pure `bin/_lib_forecast.py` behind a closed
`ForecastConfidenceCause` enum.

Four existing import paths reach the old name — `bin/cctally:1600`,
`bin/_cctally_record.py:427-428`, `bin/_cctally_record.py:1539`, and
`tests/test_620_confidence_wording.py:141` — so the glue alias stays, with
the same three-positional-argument signature and the same
`(confidence, list_of_reason_strings)` return shape.
"""
from __future__ import annotations

# conftest puts bin/ on sys.path.
import _lib_forecast
from conftest import load_script


def test_fourth_trigger_is_kernel_resident():
    """no_sample_ge_24h must come from the kernel, not be appended by glue."""
    out = _lib_forecast.assess_forecast_confidence(
        48.0, 50.0, 10, has_sample_ge_24h=False
    )
    assert out.confidence == "low"
    assert "no_sample_ge_24h" in out.reasons


def test_reason_order_is_preserved():
    out = _lib_forecast.assess_forecast_confidence(
        1.0, 0.0, 0, has_sample_ge_24h=False
    )
    assert out.reasons == (
        "elapsed_hours<24", "percent<2", "snapshots<3", "no_sample_ge_24h",
    )


def test_high_confidence_has_no_reasons():
    out = _lib_forecast.assess_forecast_confidence(
        48.0, 50.0, 10, has_sample_ge_24h=True
    )
    assert out.confidence == "high"
    assert out.reasons == ()


def test_the_cause_enum_is_closed_and_matches_the_wire_codes():
    values = [c.value for c in _lib_forecast.ForecastConfidenceCause]
    assert values == [
        "elapsed_hours<24", "percent<2", "snapshots<3", "no_sample_ge_24h",
    ]


def test_has_sample_ge_24h_is_keyword_only():
    """A positional fourth argument would silently reorder existing calls."""
    import inspect

    sig = inspect.signature(_lib_forecast.assess_forecast_confidence)
    param = sig.parameters["has_sample_ge_24h"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_glue_alias_still_resolves():
    """Four existing import paths depend on the old name."""
    ns = load_script()
    fc = ns["_cctally_forecast"]
    assert callable(fc._assess_forecast_confidence)
    conf, reasons = fc._assess_forecast_confidence(1.0, 0.0, 0)
    assert conf == "low"
    assert "elapsed_hours<24" in reasons


def test_glue_alias_returns_a_list_and_omits_the_fourth_trigger_by_default():
    """tests/test_620_confidence_wording.py:141 asserts list equality over
    exactly the three codes, so the three-argument form must default
    `has_sample_ge_24h` to True and must not return a tuple."""
    ns = load_script()
    fc = ns["_cctally_forecast"]
    conf, reasons = fc._assess_forecast_confidence(1.0, 0.0, 0)
    assert reasons == ["elapsed_hours<24", "percent<2", "snapshots<3"]
    assert conf == "low"


def test_glue_alias_forwards_the_fourth_trigger_when_asked():
    ns = load_script()
    fc = ns["_cctally_forecast"]
    conf, reasons = fc._assess_forecast_confidence(
        48.0, 50.0, 10, has_sample_ge_24h=False
    )
    assert conf == "low"
    assert reasons == ["no_sample_ge_24h"]


def test_the_namespace_alias_is_the_same_object():
    """`bin/cctally:1600` re-exports it; `_cctally_record` shims through the
    namespace. Both must keep resolving to the glue function."""
    ns = load_script()
    assert ns["_assess_forecast_confidence"] is (
        ns["_cctally_forecast"]._assess_forecast_confidence
    )


def test_glue_returns_the_predicates_reasons_unmodified(monkeypatch):
    """The alias forwards and returns; it never edits what it was given.

    Asserting the absence of one exact source literal would pass over a
    trivially reworded append, so the predicate is replaced with a sentinel
    and the glue's whole output is compared against it.
    """
    ns = load_script()
    forecast = ns["_cctally_forecast"]
    sentinel = _lib_forecast.ForecastConfidenceAssessment(
        confidence="high", reasons=("sentinel_reason",),
    )
    seen = {}

    def _fake(elapsed_hours, percent, snapshot_count, *, has_sample_ge_24h):
        seen["args"] = (elapsed_hours, percent, snapshot_count,
                        has_sample_ge_24h)
        return sentinel

    monkeypatch.setattr(forecast, "assess_forecast_confidence", _fake)
    confidence, reasons = forecast._assess_forecast_confidence(
        1.0, 0.0, 0, has_sample_ge_24h=False
    )
    assert seen["args"] == (1.0, 0.0, 0, False)
    assert confidence == "high"
    assert reasons == ["sentinel_reason"]


def test_the_record_call_site_also_routes_through_the_predicate(monkeypatch):
    """`_cctally_record` computed `has_sample_ge_24h` in glue and downgraded
    the confidence itself, so E3's goal — glue can no longer append reasons or
    override a verdict independently — held only at the forecast call site."""
    import datetime as dt

    ns = load_script()
    record = ns["_cctally_record"]
    now = dt.datetime(2026, 8, 17, tzinfo=dt.timezone.utc)
    samples = [(now - dt.timedelta(hours=48), 10.0, None),
               (now - dt.timedelta(hours=2), 40.0, None)]
    monkeypatch.setattr(
        record, "_fetch_current_week_snapshots",
        lambda *_a, **_kw: (now - dt.timedelta(days=3),
                            now + dt.timedelta(days=4), samples),
    )
    monkeypatch.setattr(record, "_apply_midweek_reset_override",
                        lambda *_a, **_kw: (now - dt.timedelta(days=3), samples))
    seen = {}
    real = record._assess_forecast_confidence

    def _record_call(*args, **kwargs):
        seen["kwargs"] = kwargs
        return real(*args, **kwargs)

    monkeypatch.setattr(record, "_assess_forecast_confidence", _record_call)
    record._weekly_pct_week_avg_projection(None, now)
    assert "has_sample_ge_24h" in seen["kwargs"], (
        "the record call site must hand the fourth trigger to the predicate"
    )
    assert seen["kwargs"]["has_sample_ge_24h"] is True
