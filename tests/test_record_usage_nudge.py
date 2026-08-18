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
import importlib
import urllib.request

import pytest

from conftest import load_script, redirect_paths


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


def _args(**over):
    base = dict(
        percent=42.0, resets_at="1786000000",
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
    """The high-cadence path, whose lock is owned two frames above the record.

    `_hook_tick_oauth_refresh` holds the lock across its OAuth fetch AND the
    authoritative record, so `_authoritative_record_usage` cannot release it —
    the deferral has to reach the frame that acquired it.
    """
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
        "seven_day": {"utilization": 0.42, "resets_at": "2026-08-20T00:00:00Z"},
    })
    monkeypatch.setattr(
        journal, "run_stats_ingest",
        lambda **_: _result(journal, ran=True, error=None, events_emitted=1))

    status, _payload = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)

    assert status.startswith("ok("), status
    assert events == ["lock", "unlock", "nudge"]


def test_nudge_sends_queue_one_and_the_resolved_bearer_token(mods, monkeypatch):
    ns, _journal = mods
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
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

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
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
