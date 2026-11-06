"""#825/#832 Task 1.7 — calibration sidecar writer generation (spec 5.4).

Claude quota-model calibration recovery (#689) enumerates the transitions the
persisted calibration state holds and journals one an earlier run failed to
record. A transition persisted by a previous-release process must never be
republished as a current-stamped `meter_rate_change`, so every regime the
current binary writes carries the writer generation plus a digest of its own
content, and recovery withholds any transition whose successor regime does not
carry a verifying stamp (a legacy in-place edit of a stamped regime fails the
digest).
"""
import pytest

from conftest import load_script, redirect_paths

BOUNDARY = "2026-08-25T00:00:00+00:00"
DETECTED = "2026-08-29T12:00:00+00:00"


@pytest.fixture
def qm(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_quota_model
    return _cctally_quota_model


def _pair():
    predecessor = {
        "effectiveFrom": "2026-07-25T00:00:00+00:00",
        "effectiveUntil": BOUNDARY,
        "unitsPerPoint": 2_442_620.0,
        "status": "ok",
        "fingerprint": "FP",
    }
    successor = {
        "effectiveFrom": BOUNDARY,
        "effectiveUntil": None,
        "unitsPerPoint": 1_685_000.0,
        "status": "ok",
        "fingerprint": "FP",
        # #1015 spec 5.6: recovery also requires the successor's frozen
        # transition lineage, which the reducer writes at detection.
        "transitionLineage": _frozen_lineage(),
    }
    return predecessor, successor


def _frozen_lineage():
    import _lib_quota_lineage as L
    return L.transition_lineage(
        windows=[L.window(None, DETECTED, until_inclusive=True)], scope="*",
        basis=[], descriptor={
            "provider": "claude", "account_key": "unattributed",
            "effective_from": BOUNDARY, "previous_units_per_point": 2_442_620.0,
            "new_units_per_point": 1_685_000.0, "severity": "alarm",
            "detected_at": DETECTED, "withholding_status": None,
            "detector_input_causes": None, "composition_provenance": None,
            "baseline_withheld_days": None})


def _state(qm, regimes):
    state = qm.empty_calibration_state()
    state["accounts"][qm._state_key(None)] = {"regimes": list(regimes)}
    return state


def test_a_legacy_persisted_transition_is_not_recovered(qm):
    state = _state(qm, _pair())
    assert qm.recovery_candidates(state, None, detected_at=DETECTED) == ()


def test_a_current_persisted_transition_is_recovered(qm):
    predecessor, successor = _pair()
    state = _state(qm, [qm.stamp_regime(predecessor),
                        qm.stamp_regime(successor)])
    candidates = qm.recovery_candidates(state, None, detected_at=DETECTED)
    assert [c.effective_from for c in candidates] == [BOUNDARY]


def test_a_legacy_successor_is_withheld(qm):
    predecessor, successor = _pair()
    state = _state(qm, [qm.stamp_regime(predecessor), successor])
    assert qm.recovery_candidates(state, None, detected_at=DETECTED) == ()


def test_a_current_successor_of_legacy_history_is_recovered(qm):
    """Every regime from before the upgrade is legacy-written; the first
    current change after it is still a recoverable transition."""
    predecessor, successor = _pair()
    state = _state(qm, [predecessor, qm.stamp_regime(successor)])
    candidates = qm.recovery_candidates(state, None, detected_at=DETECTED)
    assert [c.effective_from for c in candidates] == [BOUNDARY]


def test_a_legacy_in_place_edit_of_a_stamped_regime_is_withheld(qm):
    predecessor, successor = _pair()
    edited = qm.stamp_regime(successor)
    edited["unitsPerPoint"] = 1_500_000.0  # the previous release's update()
    state = _state(qm, [qm.stamp_regime(predecessor), edited])
    assert not qm.regime_writer_is_current(edited)
    assert qm.recovery_candidates(state, None, detected_at=DETECTED) == ()


def test_only_regimes_this_run_wrote_are_stamped(qm):
    predecessor, successor = _pair()
    before = _state(qm, [predecessor])
    closed = dict(predecessor)
    after = _state(qm, [closed, successor])
    stamped = qm.stamp_current_writes(before, after, None)
    regimes = qm.stored_regimes(stamped, None)
    # The untouched legacy regime is NOT laundered into a current one.
    assert "wpg" not in regimes[0]
    assert qm.regime_writer_is_current(regimes[1])
    # A regime this run modified is stamped.
    modified = dict(predecessor, status="stale")
    stamped = qm.stamp_current_writes(before, _state(qm, [modified]), None)
    assert qm.regime_writer_is_current(qm.stored_regimes(stamped, None)[0])


# --------------------------------------------------------------------------
# #1015 T5 — an older writer's in-place update is never accepted as current
# lineage (spec 5.2, A13, R2)
# --------------------------------------------------------------------------
_FIT_FROM = "2026-10-01T00:00:00+00:00"
_FIT_AS_OF = "2026-10-20T00:00:00+00:00"
_LATER_AS_OF = "2026-10-25T00:00:00+00:00"


def _stamped_regime(qm):
    import _lib_quota_lineage as L
    regime = {
        "effectiveFrom": _FIT_FROM, "effectiveUntil": None,
        "fingerprint": qm.qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
        "algorithmRevision": qm.qm.QUOTA_MODEL_ALGORITHM_REVISION,
        "coefficientEra": "opus-5-5", "unitsPerPoint": 100_000.0,
        "interval": {"lo": 95_000.0, "hi": 105_000.0},
        "support": {"days": 12, "segments": 2}, "status": "ok",
        "asOf": _FIT_AS_OF, "qualifications": [],
        "familyShares": {"claude-opus-5-5": 1.0},
        "classShares": {"fresh": 1.0, "output": 0.0, "cache_1h": 0.0,
                        "cache_read": 0.0},
        "familyRadius": 0.02, "classRadius": 0.0105,
    }
    regime["fitLineage"] = L.fit_lineage(
        scope="*", basis=[], windows=L.dependency_windows(
            "nosplit", a=_FIT_FROM, t=_FIT_AS_OF),
        estimator=None, regime=regime)
    return qm.stamp_regime(regime)


def test_1015_an_older_writers_update_is_assessed_as_legacy(qm):
    """Detects stale lineage accepted as current: the previous release's
    `update()` rewrites `asOf` (identical coefficients) and keeps the stale
    `fitLineage`, so the digest fails, the fit is assessed as legacy over
    [era start, new asOf], and a correction outside the ORIGINAL window but
    inside that one withholds it."""
    import json
    import sqlite3
    import _cctally_core
    import _cctally_quota_calibration as qcg
    import _lib_quota_lineage as L
    _cctally_core.open_db().close()
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO claude_weekly_observation_index (observation_id, account_key, captured_at_utc) VALUES (?,?,?)",
            ("obs-1", "acct-a", "2026-10-22T09:32:00+00:00"))
        conn.execute(
            "INSERT INTO claude_weekly_correction_history VALUES (?,?,?)",
            ("op-1", "obs-1", "retract"))
        conn.execute(
            "INSERT INTO claude_weekly_correction_ops VALUES (?,?,?)",
            ("op-1", "observations-2026-10.jsonl", 100))
        conn.execute("INSERT INTO correction_batch_lineage VALUES (?,?,?)",
                     ("batch-1", "observations-2026-10.jsonl", 200))
        conn.execute("INSERT INTO correction_batch_footprint VALUES (?,?,?)",
                     ("batch-1", "claude", "acct-a"))
        conn.commit()
    finally:
        conn.close()

    def read(regime):
        qm.calibration_path().write_text(
            json.dumps(_state(qm, [regime])), encoding="utf-8")
        return qcg.read_calibration_file(account_key=None)

    current = _stamped_regime(qm)
    assert L.fit_lineage_verifies(current)
    # Non-vacuity: the correction is outside the stamped fit's window.
    assert read(current).rejection is None

    legacy_update = dict(current)
    legacy_update.update({k: current[k] for k in (
        "unitsPerPoint", "interval", "support", "qualifications")})
    legacy_update["asOf"] = _LATER_AS_OF
    assert not L.fit_lineage_verifies(legacy_update)
    read_after = read(legacy_update)
    assert read_after.regime is None
    assert read_after.rejection.value == "recalibration-needed"
