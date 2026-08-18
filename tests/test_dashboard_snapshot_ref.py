"""#583 S2 — _SnapshotRef as the queue and activity authority.

``_SnapshotRef`` is shared with the TUI, whose ``r`` keypress calls
``request_sync()`` with no arguments (``bin/_cctally_tui.py:5581``) and whose
``_TuiSyncThread`` consumes ``take_sync_request()`` as a test-and-clear boolean
(``:5832``). Those two semantics are pinned here alongside the new counters, so
the dashboard's queue cannot be built by changing the TUI's mechanism.
"""
import importlib

import pytest

from conftest import load_script


@pytest.fixture
def dash():
    load_script()  # sets sys.path so sibling modules import
    return importlib.import_module("_cctally_dashboard")


def _ref(dash):
    return dash._SnapshotRef(dash._empty_dashboard_snapshot())


def test_epoch_is_fixed_length_and_stable(dash):
    ref = _ref(dash)
    assert len(ref.server_epoch) == 16
    assert ref.server_epoch == ref.server_epoch
    assert ref.activity()["server_epoch"] == ref.server_epoch


def test_request_ids_are_monotonic_from_one(dash):
    ref = _ref(dash)
    assert ref.request_sync() == 1
    assert ref.request_sync() == 2
    assert ref.request_sync(refresh=True) == 3


def test_pending_request_is_a_peek_not_a_consume(dash):
    ref = _ref(dash)
    ref.request_sync()
    assert ref.pending_request() is True
    assert ref.pending_request() is True  # still pending — peeking never clears


def test_capture_batch_takes_the_high_water_mark_and_ors_refresh(dash):
    ref = _ref(dash)
    ref.request_sync(refresh=False)
    ref.request_sync(refresh=True)
    ref.request_sync(refresh=False)
    batch_id, refresh = ref.capture_batch()
    assert batch_id == 3
    assert refresh is True
    assert ref.pending_request() is False


def test_capture_clears_refresh_for_the_next_batch(dash):
    ref = _ref(dash)
    ref.request_sync(refresh=True)
    ref.capture_batch()
    ref.request_sync(refresh=False)
    _, refresh = ref.capture_batch()
    assert refresh is False


def test_request_during_a_build_stays_outstanding(dash):
    ref = _ref(dash)
    ref.request_sync()
    batch_id, _ = ref.capture_batch()
    ref.request_sync()                       # arrives mid-build
    ref.settle(batch_id, "ok", ())
    act = ref.activity()
    assert act["settled_id"] == 1
    assert act["requested_id"] == 2
    assert act["rebuilding"] is False


def test_settled_metadata_persists_until_a_newer_batch_settles(dash):
    ref = _ref(dash)
    ref.request_sync()
    b1, _ = ref.capture_batch()
    ref.settle(b1, "ok", ({"code": "rate_limited"},))
    assert ref.activity()["settled_warnings"] == ({"code": "rate_limited"},)
    ref.set(dash._empty_dashboard_snapshot())   # an ordinary later publish
    assert ref.activity()["settled_warnings"] == ({"code": "rate_limited"},)
    ref.request_sync()
    b2, _ = ref.capture_batch()
    ref.settle(b2, "failed", ())
    act = ref.activity()
    assert act["settled_status"] == "failed"
    assert act["settled_warnings"] == ()


def test_rebuilding_flag_tracks_capture_and_settle(dash):
    ref = _ref(dash)
    ref.request_sync()
    assert ref.activity()["rebuilding"] is False
    batch_id, _ = ref.capture_batch()
    assert ref.activity()["rebuilding"] is True
    ref.settle(batch_id, "ok", ())
    assert ref.activity()["rebuilding"] is False


def test_mark_rebuilding_moves_the_flag_alone_and_reports_the_transition(dash):
    """The narrow mutator the batchless tick needs (#583 S2 §6.3).

    It must not touch the settlement counters, and it must report whether the
    value changed so the loop's collaborator publishes exactly one frame per
    transition rather than a duplicate on every requested tick.
    """
    ref = _ref(dash)
    ref.request_sync()
    b1, _ = ref.capture_batch()
    ref.settle(b1, "ok", ({"code": "rate_limited"},))
    before = dict(ref.activity())

    assert ref.mark_rebuilding(True) is True
    assert ref.get().sync_activity["rebuilding"] is True
    assert ref.mark_rebuilding(True) is False       # no transition, no frame
    assert ref.mark_rebuilding(False) is True
    assert ref.mark_rebuilding(False) is False

    after = ref.activity()
    for field in ("requested_id", "started_id", "settled_id",
                  "settled_status", "settled_warnings"):
        assert after[field] == before[field], field


def test_legacy_tui_flag_semantics_are_unchanged(dash):
    ref = _ref(dash)
    ref.request_sync()
    assert ref.take_sync_request() is True
    assert ref.take_sync_request() is False


def test_the_held_snapshot_always_carries_the_current_activity(dash):
    """The reference is the single authority (#583 S2 spec 6.2).

    A2 and the final publish both replace the snapshot wholesale, so a request
    accepted mid-build would have its counter overwritten by the older object
    the builder had already assembled. Every mutator therefore re-stamps the
    held snapshot, and ``set()`` merges the authoritative activity in.
    """
    ref = _ref(dash)
    assert ref.get().sync_activity["requested_id"] == 0
    ref.request_sync()
    assert ref.get().sync_activity["requested_id"] == 1

    stale = dash._empty_dashboard_snapshot()   # built before the next request
    ref.request_sync()
    merged = ref.set(stale)
    assert merged.sync_activity["requested_id"] == 2
    assert ref.get().sync_activity["requested_id"] == 2

    batch_id, _ = ref.capture_batch()
    assert ref.get().sync_activity["rebuilding"] is True
    ref.settle(batch_id, "ok", ())
    assert ref.get().sync_activity["rebuilding"] is False
    assert ref.get().sync_activity["settled_id"] == batch_id
