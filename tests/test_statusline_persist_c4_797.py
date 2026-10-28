"""#797 T10: the persistence leg stays database- and journal-free (C4 of #825)
with provider-activity capture, and its cost is measured against the #825
baseline.

`test_persistence_leg_opens_no_journal_or_database` is the structural guard:
for usage-only, unchanged and scan-bound renders the real `_statusline_persist`
(with the detached reducer stubbed exactly where a render forks it) opens no
journal file and no SQLite database. `test_persistence_leg_median_t10` is the
measurement; it runs only when ``CCTALLY_T10_MEASURE=1`` (timings are not a
suite gate) and prints the per-class medians with and without capture.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import pathlib
import sqlite3
import statistics
import sys
import time

import pytest

from conftest import load_script, redirect_paths


def _assistant(ts, request_id="req_01"):
    return json.dumps({
        "parentUuid": None, "isSidechain": False, "type": "assistant",
        "sessionId": "s1", "cwd": "/Users/u/proj", "requestId": request_id,
        "message": {"id": "msg_01", "model": "claude-opus-5-5", "role": "assistant",
                    "content": [{"type": "text", "text": "ok" * 400}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}},
        "uuid": "u-1", "timestamp": ts,
    })


def _progress(ts):
    return json.dumps({"type": "progress", "timestamp": ts,
                       "data": {"content": "p" * 900}})


def _append(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for line in lines:
            fh.write(line + "\n")


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    app = sys.modules["cctally"]
    monkeypatch.setattr(app, "_nudge_dashboard_repaint", lambda: None)
    monkeypatch.setattr(app, "_statusline_oauth_tick", lambda *a, **k: None)
    return app


class _Clock:
    def __init__(self, monkeypatch):
        self.value = float(int(time.time()))
        monkeypatch.setattr(time, "time", lambda: self.value)


def _render_classes(tmp_path):
    """Three transcript layouts: usage-only (a session that grows by one
    response per render), unchanged (nothing moved since the last render) and
    scan-bound (the main tail holds no response inside the 1 MiB bound and
    twelve subagent files grow each render, past the eight-file budget)."""
    usage = tmp_path / "proj" / "usage.jsonl"
    _append(usage, [_assistant(f"2026-10-26T09:{i % 60:02d}:00Z") for i in range(200)])
    for k in range(3):
        _append(usage.with_suffix("") / "subagents" / f"agent-{k}.jsonl",
                [_assistant("2026-10-26T09:00:00Z")] * 20)
    unchanged = tmp_path / "proj" / "unchanged.jsonl"
    _append(unchanged, [_assistant(f"2026-10-26T09:{i % 60:02d}:00Z") for i in range(200)])
    bound = tmp_path / "proj" / "bound.jsonl"
    _append(bound, [_assistant("2026-10-26T08:00:00Z")]
            + [_progress("2026-10-26T08:00:01Z")] * 1200)
    for k in range(12):
        _append(bound.with_suffix("") / "subagents" / f"agent-{k:02d}.jsonl",
                [_assistant("2026-10-26T08:00:00Z")] * 20)

    def grow_usage(i):
        _append(usage, [_assistant(f"2026-10-26T10:{i % 60:02d}:{i % 60:02d}Z")])

    def grow_bound(i):
        _append(bound, [_progress("2026-10-26T10:00:00Z")])
        for k in range(12):
            _append(bound.with_suffix("") / "subagents" / f"agent-{k:02d}.jsonl",
                    [_progress("2026-10-26T10:00:00Z")])

    return {
        "usage-only": (usage, grow_usage, True),
        "unchanged": (unchanged, lambda i: None, False),
        "scan-bound": (bound, grow_bound, False),
    }


def _parsed(app, transcript, *, percent, session, now):
    payload = {"session_id": session, "transcript_path": str(transcript),
               "rate_limits": {"seven_day": {"used_percentage": percent,
                                             "resets_at": _iso(now + 3 * 86400)}}}
    return app._lib_statusline.parse_statusline_stdin(json.dumps(payload).encode())


def test_persistence_leg_opens_no_journal_or_database(app, tmp_path, monkeypatch):
    import _cctally_core as core
    import _cctally_journal as journal
    sl = sys.modules["_cctally_statusline"]
    pa = sys.modules["_lib_provider_activity"]
    clock = _Clock(monkeypatch)
    classes = _render_classes(tmp_path)
    # Prime the store through one real, synchronous reduction per class so
    # the measured renders are steady-state ones.
    for name, (transcript, _grow, _rising) in classes.items():
        sl._statusline_persist(_parsed(app, transcript, percent=20, session=name,
                                       now=int(clock.value)), sync_for_test=True)
    forks = []
    monkeypatch.setattr(sl, "_fork_persist", lambda fd, reservation: (
        forks.append(fd), sl._release_reservation(reservation)))
    forbidden = []

    def refuse(label):
        def _raise(*args, **kwargs):
            forbidden.append(label)
            raise AssertionError(label)
        return _raise

    monkeypatch.setattr(sqlite3, "connect", refuse("sqlite3.connect"))
    for name in ("ensure_account_bound_cutover", "find_account_bound_cutover",
                 "append_record", "append_records", "run_stats_ingest"):
        monkeypatch.setattr(journal, name, refuse(name))
    journal_dir = str(core.JOURNAL_DIR)
    databases = tuple(str(p) for p in (core.DB_PATH, core.CACHE_DB_PATH))
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def guarded(real):
        def _open(file, *args, **kwargs):
            text = os.fspath(file) if not isinstance(file, int) else ""
            if text.startswith(journal_dir) or text.startswith(databases):
                forbidden.append(text)
                raise AssertionError(f"render opened {text}")
            return real(file, *args, **kwargs)
        return _open

    monkeypatch.setattr(builtins, "open", guarded(real_open))
    monkeypatch.setattr(io, "open", guarded(real_io_open))
    monkeypatch.setattr(os, "open", guarded(real_os_open))
    captured = []
    real_capture = pa.capture_activity
    monkeypatch.setattr(pa, "capture_activity", lambda *a, **k: (
        lambda w: (captured.append(w), w)[1])(real_capture(*a, **k)))
    for i in range(3):
        clock.value += 1
        for name, (transcript, grow, rising) in classes.items():
            grow(i)
            sl._statusline_persist(_parsed(
                app, transcript, percent=21 + i if rising else 20, session=name,
                now=int(clock.value)))
    assert forbidden == []
    assert len(captured) == 9
    statuses = {w.status for w in captured}
    assert "known" in statuses and "unknown" in statuses


def _median_ms(samples):
    return statistics.median(samples) * 1000.0


@pytest.mark.skipif(os.environ.get("CCTALLY_T10_MEASURE") != "1",
                    reason="timing measurement; set CCTALLY_T10_MEASURE=1")
def test_persistence_leg_median_t10(app, tmp_path, monkeypatch):
    """The #825 method (commit 700ae6268): 200 renders per run, three runs per
    variant, the same session's renders with capture replaced by a constant
    witness as the control. Each render advances the clock one second, so
    every render appends a distinct evaluation."""
    sl = sys.modules["_cctally_statusline"]
    pa = sys.modules["_lib_provider_activity"]
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(sl, "_fork_persist", lambda fd, reservation: sl._release_reservation(
        reservation))
    real_capture = pa.capture_activity
    constant = real_capture(None, cache_path=tmp_path / "unused.json")
    results = {}
    for variant in ("control", "capture"):
        monkeypatch.setattr(pa, "capture_activity", real_capture if variant == "capture"
                            else (lambda *a, **k: constant))
        for run in range(3):
            root = tmp_path / f"{variant}-{run}"
            classes = _render_classes(root)
            for name, (transcript, grow, rising) in classes.items():
                session = f"{variant}-{run}-{name}"
                sl._statusline_persist(_parsed(app, transcript, percent=20, session=session,
                                               now=int(clock.value)), sync_for_test=True)
                samples = []
                for i in range(200):
                    clock.value += 1
                    grow(i)
                    parsed = _parsed(app, transcript, percent=20 + (i % 2 if rising else 0),
                                     session=session, now=int(clock.value))
                    start = time.perf_counter()
                    sl._statusline_persist(parsed)
                    samples.append(time.perf_counter() - start)
                results.setdefault((name, variant), []).append(_median_ms(samples))
    lines = []
    for name in ("usage-only", "unchanged", "scan-bound"):
        control = statistics.median(results[(name, "control")])
        capture = statistics.median(results[(name, "capture")])
        lines.append(f"T10 {name}: control {control:.3f} ms, capture {capture:.3f} ms, "
                     f"delta {capture - control:+.3f} ms; runs control "
                     f"{[round(x, 3) for x in results[(name, 'control')]]} capture "
                     f"{[round(x, 3) for x in results[(name, 'capture')]]}")
    # The cold case, informational: the first capture of a scan-bound session
    # (no cache yet: the 1 MiB main scan plus eight full subagent scans).
    bound = _render_classes(tmp_path / "cold")["scan-bound"][0]
    cold = []
    for i in range(20):
        start = time.perf_counter()
        real_capture(str(bound), cache_path=tmp_path / "cold-cache" / f"{i}.json")
        cold.append(time.perf_counter() - start)
    lines.append(f"T10 cold scan-bound capture: median {_median_ms(cold):.3f} ms")
    print("\n".join(lines))
    for name in ("usage-only", "unchanged", "scan-bound"):
        delta = (statistics.median(results[(name, "capture")])
                 - statistics.median(results[(name, "control")]))
        assert delta <= 2.0, lines
