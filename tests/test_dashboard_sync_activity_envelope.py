"""#583 S2 — sync_activity is always published, with a stable shape.

The SSE hub is latest-wins, so the frame that would prove a particular request
finished may never be delivered. A boolean cannot survive that; monotonic
identifiers can, because the client compares its own outstanding identifier
against ``settled_id`` on whichever frame does arrive.
"""
import dataclasses
import datetime as dt

import pytest

from conftest import load_script, redirect_paths

_KEYS = {
    "server_epoch", "rebuilding", "requested_id", "started_id",
    "settled_id", "settled_status", "settled_warnings",
}

_NOW = dt.datetime(2026, 8, 16, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def envelope_for(monkeypatch, tmp_path):
    """Serialize a hand-built DataSnapshot the way the freshness tests do.

    ``snapshot_to_envelope`` opens cache.db / stats.db even for a hand-built
    snapshot, so pin the kernel path constants at ``tmp_path``.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    def _build(**fields):
        snap = dataclasses.replace(
            ns["_empty_dashboard_snapshot"](), generated_at=_NOW, **fields,
        )
        return ns["snapshot_to_envelope"](snap, now_utc=_NOW)

    return _build


def test_sync_activity_is_always_present(envelope_for):
    env = envelope_for(sync_activity=None)
    assert "sync_activity" in env
    act = env["sync_activity"]
    assert set(act) == _KEYS
    assert act["rebuilding"] is False
    assert act["requested_id"] == 0
    assert act["settled_status"] is None
    assert act["settled_warnings"] == []


def test_sync_activity_round_trips_a_populated_state(envelope_for):
    env = envelope_for(sync_activity={
        "server_epoch": "0123456789abcdef",
        "rebuilding": True,
        "requested_id": 7,
        "started_id": 5,
        "settled_id": 4,
        "settled_status": "ok",
        "settled_warnings": ({"code": "rate_limited"},),
    })
    act = env["sync_activity"]
    assert act["server_epoch"] == "0123456789abcdef"
    assert act["rebuilding"] is True
    assert act["requested_id"] == 7
    assert act["started_id"] == 5
    assert act["settled_id"] == 4
    assert act["settled_status"] == "ok"
    assert act["settled_warnings"] == [{"code": "rate_limited"}]


def test_settled_warnings_serializes_as_a_json_array(envelope_for):
    import json
    env = envelope_for(sync_activity={
        "server_epoch": "0123456789abcdef", "rebuilding": False,
        "requested_id": 1, "started_id": 1, "settled_id": 1,
        "settled_status": "ok", "settled_warnings": ({"code": "x"},),
    })
    # Tuples are not JSON — a tuple that reached the encoder unconverted
    # would serialize as an array by luck in json but break browser-strict
    # expectations elsewhere. Assert the Python type explicitly.
    assert isinstance(env["sync_activity"]["settled_warnings"], list)
    json.dumps(env["sync_activity"])


def test_a_non_mapping_warning_is_dropped_rather_than_raising(envelope_for):
    """`dict(w)` raises on a non-mapping, and this serializer runs on EVERY
    published frame — one malformed entry would take down the whole envelope
    instead of costing one warning."""
    env = envelope_for(sync_activity={
        "server_epoch": "0123456789abcdef", "rebuilding": False,
        "requested_id": 1, "started_id": 1, "settled_id": 1,
        "settled_status": "ok",
        "settled_warnings": ("rate_limited", {"code": "x"}, None),
    })
    assert env["sync_activity"]["settled_warnings"] == [{"code": "x"}]


def test_a_non_dict_mapping_warning_is_kept(envelope_for):
    """The guard exists because `dict(w)` raises on a NON-MAPPING. A mapping
    that simply is not a `dict` converts fine, so rejecting it drops a
    perfectly good warning — and the deferred refresh warnings this field
    carries are the only place a queued request's outcome is reported."""
    import types
    env = envelope_for(sync_activity={
        "server_epoch": "0123456789abcdef", "rebuilding": False,
        "requested_id": 1, "started_id": 1, "settled_id": 1,
        "settled_status": "ok",
        "settled_warnings": (types.MappingProxyType({"code": "rate_limited"}),),
    })
    assert env["sync_activity"]["settled_warnings"] == [
        {"code": "rate_limited"}]
