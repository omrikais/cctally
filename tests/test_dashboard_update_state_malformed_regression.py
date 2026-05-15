"""Regression: malformed update-state.json must NOT TypeError in the
SSE envelope build path.

Root cause (caught at Phase F #22 code review, 2026-05-15): the moved
dashboard code in ``bin/_cctally_dashboard.py`` had three ``except
UpdateError:`` clauses (snapshot envelope + ``/api/update/status``
handler). ``UpdateError`` was defined as a module-level callable shim
(``def UpdateError(*args, **kwargs): return sys.modules['cctally']
.UpdateError(*args, **kwargs)``) for cross-module monkeypatch
propagation. But Python's ``except`` clause requires the class object
itself, not a callable that returns one — using a non-class in
``except`` raises ``TypeError: catching classes that do not inherit
from BaseException is not allowed``.

The defensive "client falls back to null-state shape" path (per the
comment at ``_cctally_dashboard.py:2806``) was broken: any user with
corrupted ``~/.local/share/cctally/update-state.json`` would crash
the entire envelope build instead of receiving the intended ``_error``
sentinel.

The fix uses ``except sys.modules["cctally"].UpdateError:`` at all
three sites — same pattern as the ``_AlertsConfigError`` catches
(Phase D #18 precedent for cross-module exception classes).

This test exercises both recovery paths: the snapshot-envelope build
and the /api/update/status handler.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script


@pytest.fixture
def ns():
    return load_script()


def _make_empty_snapshot(ns):
    """Build a minimal DataSnapshot. Fields mirror the precedent in
    tests/test_dashboard_envelope_blocks_daily.py (which is also a
    snapshot_to_envelope consumer)."""
    DataSnapshot = ns["DataSnapshot"]
    return DataSnapshot(
        current_week=None,
        forecast=None,
        trend=[],
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.timezone.utc),
        percent_milestones=[],
        weekly_history=[],
        weekly_periods=[],
        monthly_periods=[],
        blocks_panel=[],
        daily_panel=[],
    )


def test_snapshot_to_envelope_catches_malformed_update_state(ns, monkeypatch):
    """When _load_update_state raises UpdateError (malformed JSON on
    disk), the envelope must still build and surface an _error sentinel
    instead of crashing with TypeError.

    Pre-fix (``except UpdateError:`` where UpdateError was a function shim):
        TypeError: catching classes that do not inherit from BaseException
        is not allowed

    Post-fix (``except sys.modules["cctally"].UpdateError:``):
        envelope.updateState == {"_error": "update-state.json invalid"}
    """
    UpdateError = ns["UpdateError"]

    def _raise_malformed():
        raise UpdateError("update-state.json: invalid JSON at line 3")

    monkeypatch.setitem(ns, "_load_update_state", _raise_malformed)
    monkeypatch.setitem(ns, "_load_update_suppress", lambda: {
        "skipped_versions": [],
        "remind_after": None,
    })

    def _raise_doctor(**_kw):
        raise RuntimeError("pinned: doctor disabled for this regression test")
    monkeypatch.setitem(ns, "doctor_gather_state", _raise_doctor)

    snap = _make_empty_snapshot(ns)
    envelope = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.timezone.utc),
        monotonic_now=None,
    )

    update_block = envelope.get("update")
    assert update_block is not None, (
        "snapshot_to_envelope must surface an update block even "
        "when _load_update_state raises"
    )
    update_state = update_block.get("state")
    assert isinstance(update_state, dict) and "_error" in update_state, (
        f"expected _error sentinel on malformed-state recovery; got "
        f"{update_state!r}. Pre-fix this path raised TypeError because "
        f"UpdateError was a function shim, not a class."
    )


def test_handle_get_update_status_catches_malformed_state_and_suppress(ns, monkeypatch):
    """The /api/update/status handler has TWO additional ``except
    UpdateError`` sites (one for state, one for suppress). Both must
    catch cleanly when the corresponding loader raises UpdateError.

    We override ``_respond_json`` to capture the response payload without
    needing a full HTTP socket stack.
    """
    UpdateError = ns["UpdateError"]
    DashboardHTTPHandler = ns["DashboardHTTPHandler"]

    def _raise_state():
        raise UpdateError("state json corrupted")

    def _raise_suppress():
        raise UpdateError("suppress json corrupted")

    monkeypatch.setitem(ns, "_load_update_state", _raise_state)
    monkeypatch.setitem(ns, "_load_update_suppress", _raise_suppress)

    captured: dict = {}

    class _MockHandler(DashboardHTTPHandler):
        def __init__(self_inner):  # skip BaseHTTPRequestHandler socket init
            pass

        def _respond_json(self_inner, status, body):
            captured["status"] = status
            captured["body"] = body

    handler = _MockHandler()
    handler._handle_get_update_status()

    assert captured.get("status") == 200, (
        f"handler should return 200 even on malformed state/suppress; "
        f"got status={captured.get('status')!r}. Pre-fix this method raised "
        f"TypeError because UpdateError was a function shim."
    )
    body = captured["body"]
    assert isinstance(body["state"], dict) and "_error" in body["state"], (
        f"expected state._error sentinel; got {body['state']!r}"
    )
    assert isinstance(body["suppress"], dict) and "_error" in body["suppress"], (
        f"expected suppress._error sentinel; got {body['suppress']!r}"
    )
