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
    }
    return predecessor, successor


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
