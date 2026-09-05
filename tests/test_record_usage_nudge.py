"""#583 S2 — nudge only on material change, and authenticate.

`cmd_record_usage` fires at Claude Code's status-line cadence. Nudging on
every tick would queue a rebuild for work that changed nothing the dashboard
shows, so the nudge is gated on the `IngestResult` the call currently
discards. `consumed` is the wrong signal, because unchanged observations
advance ingestion without changing anything displayed; `alerts` is the wrong
signal, because it covers only a subset of material events — a new 5-hour
window changes the dashboard without necessarily firing an alert.
"""
import argparse
import datetime as dt
import sys
import time
import types
import importlib
import urllib.request

import pytest

from conftest import load_script, redirect_paths

# `cmd_record_usage` rejects a reset outside the plausibility band
# [now-30d, now+8d] and writes no row, so a literal epoch here is a fixture
# with an expiry date. One expired: `1786000000` sat inside the band when this
# module was written and crossed the 30-day floor on 2026-09-05, failing eight
# cases in this file for a reason none of them is about, on a branch that had
# not touched this subsystem. Two ISO literals of `2026-08-20T00:00:00Z` were
# 16 days from the same fate. Derive the instant from the clock the band is
# measured against instead; three days ahead is inside the +8d ceiling with
# room to spare, and no assertion in this module reads the value.
_RESETS_AT_EPOCH = int(time.time()) + 3 * 86400


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""


@pytest.fixture
def mods(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    journal = importlib.import_module("_cctally_journal")
    return ns, journal


def _result(journal, *, ran, error, events_emitted):
    return journal.IngestResult(
        ran=ran, consumed=1, malformed=0,
        events_emitted=events_emitted, alerts=[], error=error,
    )


def _plausible_resets_at() -> str:
    """A `--resets-at` inside `cmd_record_usage`'s plausibility band.

    Reads `_RESETS_AT_EPOCH`, which is resolved once at import, so every call
    in one run names the same instant.
    """
    return str(_RESETS_AT_EPOCH)


def _plausible_resets_at_iso() -> str:
    """The same instant, in the ISO form an OAuth `resets_at` field carries.

    `_hook_tick_parse_oauth_payload` converts this string to the epoch it
    hands `cmd_record_usage`, so the payload stubs below are subject to the
    same expiring-literal failure and need the same wall-clock derivation.
    """
    return dt.datetime.fromtimestamp(
        _RESETS_AT_EPOCH, tz=dt.timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _args(**over):
    base = dict(
        percent=42.0, resets_at=_plausible_resets_at(),
        five_hour_percent=None, five_hour_resets_at=None, source="statusline",
    )
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def record_usage(mods, monkeypatch):
    """Run `cmd_record_usage` with a stubbed ingest and a counted nudge."""
    ns, journal = mods
    nudges = []
    monkeypatch.setitem(
        ns, "_nudge_dashboard_repaint", lambda *a, **kw: nudges.append(1))

    def _run(ingest, **kw):
        monkeypatch.setattr(journal, "run_stats_ingest", lambda **_: ingest)
        rc = ns["cmd_record_usage"](_args(), **kw)
        assert rc == 0
        return nudges

    _run.nudges = nudges
    _run.journal = journal
    return _run


def test_nudges_when_events_were_emitted(record_usage):
    journal = record_usage.journal
    assert record_usage(
        _result(journal, ran=True, error=None, events_emitted=3)) == [1]


def test_does_not_nudge_when_nothing_changed(record_usage):
    journal = record_usage.journal
    assert record_usage(
        _result(journal, ran=True, error=None, events_emitted=0)) == []


def test_does_not_nudge_on_an_ingest_error(record_usage):
    journal = record_usage.journal
    assert record_usage(
        _result(journal, ran=True, error="boom", events_emitted=5)) == []


def test_does_not_nudge_when_ingest_did_not_run(record_usage):
    journal = record_usage.journal
    assert record_usage(
        _result(journal, ran=False, error=None, events_emitted=5)) == []


def test_the_flag_suppresses_an_otherwise_material_nudge(record_usage):
    journal = record_usage.journal
    assert record_usage(
        _result(journal, ran=True, error=None, events_emitted=3),
        nudge_dashboard=False) == []


def test_statusline_path_nudges_by_default(mods, monkeypatch):
    """`_authoritative_record_usage` threads the flag and defaults it on."""
    ns, journal = mods
    nudges = []
    monkeypatch.setitem(
        ns, "_nudge_dashboard_repaint", lambda *a, **kw: nudges.append(1))
    monkeypatch.setattr(
        journal, "run_stats_ingest",
        lambda **_: _result(journal, ran=True, error=None, events_emitted=1))
    result = ns["_authoritative_record_usage"](_args(source="api"), {"sevenDay"})
    assert result.status == "ok", result.reason
    assert nudges == [1]


def test_refresh_origin_record_suppresses_the_inner_nudge(mods, monkeypatch):
    """`cmd_refresh_usage` nudges once itself, and the dashboard's own
    `refresh=1` runs this inside its `sync_lock`, so an inner nudge would
    enqueue a second rebuild for work already underway."""
    ns, journal = mods
    nudges = []
    monkeypatch.setitem(
        ns, "_nudge_dashboard_repaint", lambda *a, **kw: nudges.append(1))
    monkeypatch.setattr(
        journal, "run_stats_ingest",
        lambda **_: _result(journal, ran=True, error=None, events_emitted=1))
    result = ns["_authoritative_record_usage"](
        _args(source="api"), {"sevenDay"}, nudge_dashboard=False)
    assert result.status == "ok", result.reason
    assert nudges == []


class _RecordingLock:
    """Stand-in for `_SelectedStateLock`, recording its own critical section."""

    def __init__(self, events):
        self._events = events

    def __enter__(self):
        self._events.append("lock")
        return self

    def __exit__(self, *exc):
        self._events.append("unlock")
        return False


def test_the_nudge_fires_after_the_selected_state_lock_is_released(
        mods, monkeypatch):
    """#583 S2 — no network call inside the selected-state critical section.

    `_selected_state_lock` is an `fcntl.flock` every cctally process contends
    on. The nudge is a loopback POST with a multi-second timeout, so something
    accepting on 127.0.0.1:8789 without answering would stall that lock for
    every other process — exactly the wrong place for a network call under the
    multi-agent hook storm #297 documents.
    """
    ns, journal = mods
    statusline = importlib.import_module("_cctally_statusline")
    events = []
    monkeypatch.setattr(statusline, "_selected_state_lock",
                        lambda: _RecordingLock(events))
    monkeypatch.setitem(ns, "_nudge_dashboard_repaint",
                        lambda *a, **kw: events.append("nudge"))
    monkeypatch.setattr(
        journal, "run_stats_ingest",
        lambda **_: _result(journal, ran=True, error=None, events_emitted=1))

    result = ns["_authoritative_record_usage"](_args(source="api"), {"sevenDay"})

    assert result.status == "ok", result.reason
    assert events == ["lock", "unlock", "nudge"]


def test_the_hook_tick_nudge_fires_after_its_own_lock_is_released(
        mods, monkeypatch):
    """The high-cadence path defers after both lock-bounded phases."""
    ns, journal = mods
    events = []
    monkeypatch.setitem(ns, "load_config", lambda: {})
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "_newest_snapshot_age_seconds", lambda: None)
    monkeypatch.setitem(ns, "_statusline_observe_age_seconds", lambda: 10_000.0)
    monkeypatch.setitem(ns, "_selected_state_lock",
                        lambda: _RecordingLock(events))
    monkeypatch.setitem(ns, "_nudge_dashboard_repaint",
                        lambda *a, **kw: events.append("nudge"))
    monkeypatch.setitem(ns, "_fetch_oauth_usage", lambda **kw: {
        "seven_day": {"utilization": 0.42,
                      "resets_at": _plausible_resets_at_iso()},
    })
    monkeypatch.setattr(
        journal, "run_stats_ingest",
        lambda **_: _result(journal, ran=True, error=None, events_emitted=1))

    status, _payload = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)

    assert status.startswith("ok("), status
    assert events == ["lock", "unlock", "lock", "unlock", "nudge"]


def test_hook_tick_oauth_fetch_does_not_hold_the_selected_state_lock(
        mods, monkeypatch):
    """#605 item 5: the five-second network fetch is outside the flock."""
    ns, journal = mods
    held = {"value": False}

    class SelectedLock:
        def __enter__(self):
            assert held["value"] is False
            held["value"] = True
            return self

        def __exit__(self, *exc):
            held["value"] = False
            return False

    monkeypatch.setitem(ns, "load_config", lambda: {})
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "_newest_snapshot_age_seconds", lambda: None)
    monkeypatch.setitem(ns, "_statusline_observe_age_seconds", lambda: 10_000.0)
    monkeypatch.setitem(ns, "_selected_state_lock", SelectedLock)

    def fetch(**_kwargs):
        assert held["value"] is False, "OAuth network I/O held selected-state flock"
        return {
            "seven_day": {
                "utilization": 42.0,
                "resets_at": _plausible_resets_at_iso(),
            },
        }

    monkeypatch.setitem(ns, "_fetch_oauth_usage", fetch)
    monkeypatch.setattr(
        journal, "run_stats_ingest",
        lambda **_: _result(journal, ran=True, error=None, events_emitted=0),
    )

    status, _ = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)

    assert status.startswith("ok("), status
    assert held["value"] is False


def test_lock_held_authoritative_record_requires_a_deferred_nudge_sink(
        mods, monkeypatch):
    """#605 item 6: a future lock owner cannot nudge inside its section."""
    ns, _journal = mods
    called = []
    monkeypatch.setitem(ns, "cmd_record_usage", lambda *a, **kw: called.append(kw) or 0)

    result = ns["_authoritative_record_usage"](
        _args(source="api"), {"sevenDay"}, lock_held=True,
    )

    assert result.status == "record_failed"
    assert "nudge_sink" in (result.reason or "")
    assert called == [], "the fail-closed guard must run before any write"


def test_nudge_sends_queue_one_and_the_resolved_bearer_token(mods, monkeypatch):
    ns, _journal = mods
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _Resp()

    # #630 S2: rebind the module name on the IMPORTING module.
    # Patching the shared `urllib.request.urlopen` rebound stdlib urlopen for
    # the whole process; `_cctally_refresh` is the module that resolves it.
    _iso_request = types.SimpleNamespace(
        **vars(sys.modules["_cctally_refresh"].urllib.request))
    _iso_request.urlopen = _fake_urlopen
    _iso_urllib = types.SimpleNamespace(
        **vars(sys.modules["_cctally_refresh"].urllib))
    _iso_urllib.request = _iso_request
    monkeypatch.setattr(
        sys.modules["_cctally_refresh"], "urllib", _iso_urllib)
    monkeypatch.setitem(ns, "_resolve_dashboard_api_token", lambda: "s3cret")
    ns["_nudge_dashboard_repaint"](port=8789)
    req = captured["req"]
    assert "queue=1" in req.full_url
    assert "refresh=0" in req.full_url
    assert req.get_header("Authorization") == "Bearer s3cret"


def test_nudge_omits_the_header_when_no_token_is_resolvable(mods, monkeypatch):
    ns, _journal = mods
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _Resp()

    # #630 S2: rebind the module name on the IMPORTING module.
    # Patching the shared `urllib.request.urlopen` rebound stdlib urlopen for
    # the whole process; `_cctally_refresh` is the module that resolves it.
    _iso_request = types.SimpleNamespace(
        **vars(sys.modules["_cctally_refresh"].urllib.request))
    _iso_request.urlopen = _fake_urlopen
    _iso_urllib = types.SimpleNamespace(
        **vars(sys.modules["_cctally_refresh"].urllib))
    _iso_urllib.request = _iso_request
    monkeypatch.setattr(
        sys.modules["_cctally_refresh"], "urllib", _iso_urllib)
    monkeypatch.setitem(ns, "_resolve_dashboard_api_token", lambda: None)
    ns["_nudge_dashboard_repaint"](port=8789)
    assert captured["req"].get_header("Authorization") is None


def test_the_token_resolver_reads_the_documented_environment_variable(
        mods, monkeypatch):
    ns, _journal = mods
    monkeypatch.delenv("CCTALLY_DASHBOARD_API_TOKEN", raising=False)
    assert ns["_resolve_dashboard_api_token"]() is None
    monkeypatch.setenv("CCTALLY_DASHBOARD_API_TOKEN", "  s3cret  ")
    assert ns["_resolve_dashboard_api_token"]() == "s3cret"
    monkeypatch.setenv("CCTALLY_DASHBOARD_API_TOKEN", "   ")
    assert ns["_resolve_dashboard_api_token"]() is None
