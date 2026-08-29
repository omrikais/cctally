"""#661 S2 §4.2 — the candidate support decision, over a REAL population.

`tests/test_forecast_dpp_candidates.py` covers the plumbing and the two date
predicates, but its `_pin_calibration` stubs `read_calibration_file`,
`apply_regime` AND the entry read together, so the decision §4.2 actually
turns on — `aggregate_composition` plus `is_supported` over a candidate week's
own entries — is never executed on that path by any test.

It is also inert on every install without a prediction-ready calibration,
including the maintainer's store, because the validated reader refuses the
`detection-only` open regime. So without a fixture that carries a
prediction-ready calibration AND real entries, §4.2's substance ships
unexercised.

This module builds that fixture. It seeds a store with five prior weeks of
snapshots and of `session_entries`, writes a real `quota-calibrations.json`
whose composition centres are computed from one of those weeks, and drives
the shipped `_select_dollars_per_percent` through it. Only the per-week cost
lookup is stubbed, so the run stays deterministic; the reader, the entry read,
the composition aggregation and the support test are all the shipped ones.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 6, tzinfo=UTC)
CURRENT_WEEK_START = dt.datetime(2026, 6, 29, tzinfo=UTC)
#: Five prior weeks, most recent first — the loop's own order.
CANDIDATES = [CURRENT_WEEK_START - dt.timedelta(days=7 * n)
              for n in range(1, 6)]

#: Distinct per-week costs so the median moves when a week leaves the pool.
#: Every week realizes 40 points of movement, so these are also the per-point
#: rates: 6, 5, 4, 3, 2 dollars from most recent to oldest.
COST_BY_WEEK = {CANDIDATES[0]: 240.0, CANDIDATES[1]: 200.0,
                CANDIDATES[2]: 160.0, CANDIDATES[3]: 120.0,
                CANDIDATES[4]: 80.0}

SUPPORTED_MODEL = "claude-opus-4-5"
#: A different family, so `aggregate_composition` puts the whole week's
#: weighted units on another share and `tv_distance` reaches 1.0.
DRIFTED_MODEL = "claude-sonnet-4-5"


def _seed_week(conn, cache, start, *, model):
    end = start + dt.timedelta(days=7)
    for hours, pct in ((24, 20.0), (120, 40.0)):
        conn.execute(
            "INSERT INTO weekly_usage_snapshots("
            " captured_at_utc, week_start_date, week_end_date, week_start_at,"
            " week_end_at, weekly_percent, source, payload_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ((start + dt.timedelta(hours=hours)).isoformat().replace(
                "+00:00", "Z"),
             start.date().isoformat(), end.date().isoformat(),
             start.isoformat().replace("+00:00", "Z"),
             end.isoformat().replace("+00:00", "Z"),
             pct, "fixture", "{}"))
    for index, day in enumerate((1, 3, 5)):
        at = start + dt.timedelta(days=day)
        tag = f"{start.date().isoformat()}-{index}"
        path = f"/fake/repos/alpha/{tag}.jsonl"
        cache.execute(
            "INSERT INTO session_files(path, size_bytes, mtime_ns,"
            " last_byte_offset, last_ingested_at, session_id, project_path)"
            " VALUES (?,?,?,?,?,?,?)",
            (path, 0, 0, 0, "2026-07-06T00:00:00Z", f"s-{tag}",
             "/fake/repos/alpha"))
        cache.execute(
            "INSERT INTO session_entries(source_path, line_offset,"
            " timestamp_utc, model, msg_id, req_id, input_tokens,"
            " output_tokens, cache_create_tokens, cache_create_1h_tokens,"
            " cache_read_tokens, cost_usd_raw)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (path, 0, at.isoformat(), model, f"m-{tag}", f"r-{tag}",
             200_000, 40_000, 0, 0, 0, None))


def _write_calibration(entries_model=SUPPORTED_MODEL):
    """A prediction-ready regime centred on `entries_model`'s composition."""
    import _cctally_core
    import _lib_quota_model as qm

    probe = [qm.EntryRecord(at=NOW, model=entries_model, fresh=200_000,
                            output=40_000, cache_create_total=0, cache_1h=0,
                            cache_read=0)]
    family_shares, class_shares = qm.aggregate_composition(probe)
    units = qm.weighted_units(probe[0]) * 3.0
    per_point = units / 40.0
    payload = {
        "schemaVersion": 1,
        "accounts": {"*": {"regimes": [{
            "effectiveFrom": (
                CANDIDATES[-1] - dt.timedelta(days=30)).isoformat(),
            "effectiveUntil": None,
            "fingerprint": qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
            "algorithmRevision": qm.QUOTA_MODEL_ALGORITHM_REVISION,
            "unitsPerPoint": per_point,
            "interval": {"lo": per_point * 0.95, "hi": per_point * 1.05},
            "support": {"days": 26, "segments": 4},
            "status": "ok",
            "asOf": CANDIDATES[-1].isoformat(),
            "qualifications": [],
            "familyShares": dict(family_shares),
            "classShares": dict(class_shares),
            # Tight enough that a different model FAMILY fails, wide enough
            # that the identical composition passes.
            "familyRadius": 0.05,
            "classRadius": 0.05,
        }]}},
    }
    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def store(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    return ns


def _build(ns, *, drifted_weeks=()):
    app = sys.modules["cctally"]
    conn = app.open_db()
    cache = app.open_cache_db()
    try:
        for start in CANDIDATES:
            _seed_week(conn, cache, start,
                       model=(DRIFTED_MODEL if start in drifted_weeks
                              else SUPPORTED_MODEL))
        conn.commit()
        cache.commit()
    finally:
        cache.close()
        conn.close()
    _write_calibration()


def _select(ns):
    """Run the shipped selector with only the per-week COST lookup stubbed."""
    app = sys.modules["cctally"]
    conn = app.open_db()
    original = ns["_sum_cost_for_range"]
    ns["_sum_cost_for_range"] = (
        lambda ws, _we, mode="auto", skip_sync=False, **_k:
            COST_BY_WEEK[ws])
    try:
        return ns["_select_dollars_per_percent"](
            conn, NOW, CURRENT_WEEK_START, 5.0, 50.0, skip_sync=True)
    finally:
        ns["_sum_cost_for_range"] = original
        conn.close()


def test_the_reader_publishes_a_prediction_ready_regime(store):
    """Guards the guard. Every assertion below is vacuous if the validated
    reader refuses this fixture's calibration, which is the state every
    other fixture in the estate is in."""
    _build(store)
    qcg = store["_load_sibling"]("_cctally_quota_calibration")
    read = qcg.read_calibration_file(account_key=None)
    assert read.rejection is None, read.rejection
    assert read.regime is not None


def test_a_comparable_population_passes_the_real_support_test(store):
    """The shipped `aggregate_composition` and `is_supported` run over the
    candidate weeks' own `session_entries`, with nothing stubbed but cost.

    Five weeks at $6, $5, $4, $3 and $2 per point, most recent first. The
    four most recent are selected, so the median is (5 + 4) / 2 = $4.50.
    """
    _build(store)
    dpp, source = _select(store)
    assert source == "trailing_4wk_median", source
    assert dpp == pytest.approx(4.5), dpp


def test_a_drifted_week_is_dropped_by_the_real_support_test(store):
    """The discriminating twin, and the point of the whole module: the ONLY
    difference is the model family recorded in the most recent candidate
    week's entries. Its composition sits outside the regime's recorded
    radii, so the shipped support test refuses it, the week leaves the pool,
    and the four survivors are $5, $4, $3 and $2 — median $3.50.
    """
    _build(store, drifted_weeks={CANDIDATES[0]})
    dpp, source = _select(store)
    assert source == "trailing_4wk_median_drifted", source
    assert dpp == pytest.approx(3.5), (
        "the drifted week was still priced into the median, so the support "
        f"test did not actually run over its population: {dpp}")


def test_two_drifted_weeks_leave_too_few_candidates(store):
    """§4.2: with fewer than four eligible candidates the existing fallback
    runs rather than admitting an incomparable week."""
    _build(store, drifted_weeks={CANDIDATES[0], CANDIDATES[1]})
    _dpp, source = _select(store)
    assert source == "this_week_sparse", source


def test_the_regimes_rate_never_reaches_the_published_dollar_rate(
        store, monkeypatch):
    """`_dpp_candidate_regime(account_key=None)` reads the MERGED `*` bucket,
    which spec §5.3 declares invalid as a source of published quota on a
    decorated install, and this path carries no decoration gate.

    The reason it needs none is this: the regime is used ONLY to decide
    which prior weeks are comparable, and its `unitsPerPoint` never enters
    the returned rate, which is `week_cost / realized_points` end to end. So
    two calibrations differing only in that figure must publish the same
    dollar rate.
    """
    import _cctally_core

    _build(store)
    baseline = _select(store)
    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    regime = payload["accounts"]["*"]["regimes"][0]
    regime["unitsPerPoint"] *= 7.0
    regime["interval"] = {"lo": regime["unitsPerPoint"] * 0.95,
                          "hi": regime["unitsPerPoint"] * 1.05}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    assert _select(store) == baseline, (
        "the regime's units-per-point moved the published dollar rate, so "
        "this path really is publishing a merged quota quantity")


def test_the_source_label_has_human_copy_for_every_member(store):
    """`trailing_4wk_median_drifted` reached users as a bare
    `replace("_", " ")` at three render sites, with no explanatory copy and
    no test pinning any rendering."""
    ns = store
    fc = ns["_load_sibling"]("_lib_forecast")
    for code, copy in fc.DOLLARS_PER_PERCENT_SOURCES.items():
        assert fc.dollars_per_percent_source_label(code) == copy
        assert copy and "_" not in copy, (code, copy)
    assert fc.DOLLARS_PER_PERCENT_SOURCES[
        "trailing_4wk_median_drifted"].endswith("(drift-reduced confidence)")
    assert fc.DOLLARS_PER_PERCENT_SOURCES[
        "trailing_4wk_median_unverified"].endswith(
            "(comparability unverified)")
    # An unknown code degrades rather than raising or printing an empty cell.
    assert fc.dollars_per_percent_source_label("some_future_code") == (
        "some future code")


def test_the_terminal_footer_renders_the_source_copy(store):
    """The rendering itself, pinned. The footer used to print the raw code
    with its underscores replaced, which said nothing about what `drifted`
    meant."""
    ns = store
    fc = ns["_load_sibling"]("_lib_forecast")
    forecast = ns["_cctally_forecast"]
    import inspect
    body = inspect.getsource(forecast._render_forecast_terminal)
    assert "dollars_per_percent_source_label" in body
    assert "dollars_per_percent_source.replace" not in body
    assert fc.dollars_per_percent_source_label("trailing_4wk_median") == (
        "trailing 4wk median"), (
        "the existing label's copy moved, which would move a golden")
