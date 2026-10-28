"""#997 T-A6 — progress for the stats index's full replays (spec D9).

The October 22 epoch rebuild ran 4.4 hours and died without a word: nothing
it did between start and death reached a file. `_lib_rebuild_progress` is the
seam a worker's state file hangs off: `phase()` always reaches the installed
sink, `tick()` at most every ten seconds, a sink failure never fails a
rebuild, and with no sink installed every call is a no-op.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib

import pytest

from conftest import load_script, redirect_paths


def _progress():
    return importlib.import_module("_lib_rebuild_progress")


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def progress(monkeypatch):
    module = _progress()
    clock = _Clock()
    monkeypatch.setattr(module.time, "monotonic", clock)
    writes: list = []
    module.install(lambda name, counters, forced: writes.append(
        (name, dict(counters), forced)))
    yield module, clock, writes
    module.uninstall()


def test_tick_throttle(progress):
    """A tick writes only when ten seconds have passed since the last write
    of any kind; the counters it carries accumulate in between."""
    module, clock, writes = progress
    module.phase("rebuild:read", lines=0)
    clock.now += 4.0
    module.tick(lines=4096)
    clock.now += 5.9
    module.tick(lines=8192)
    assert writes == [("rebuild:read", {"lines": 0}, True)]
    clock.now += 0.2
    module.tick(bytes=99)
    assert writes[-1] == ("rebuild:read", {"lines": 8192, "bytes": 99},
                          False)
    clock.now += 9.0
    module.tick(lines=12288)
    assert len(writes) == 2


def test_phase_always_writes(progress):
    """`phase()` is forced: two in the same instant both reach the sink, and
    a new phase starts with only its own counters."""
    module, _clock, writes = progress
    module.phase("completion:read", lines=7)
    module.phase("completion:replay-all")
    assert writes == [("completion:read", {"lines": 7}, True),
                      ("completion:replay-all", {}, True)]


def test_waiting_restores_the_phase(progress):
    """A lock wait is its own phase while it lasts, and the phase it
    interrupted, with its counters, is written back when it ends."""
    module, _clock, writes = progress
    module.phase("rebuild:quota-leg", chunks=2)
    with module.waiting("quota-flocks"):
        pass
    assert [w[0] for w in writes] == [
        "rebuild:quota-leg", "waiting:quota-flocks", "rebuild:quota-leg"]
    assert writes[-1] == ("rebuild:quota-leg", {"chunks": 2}, True)


def test_sink_exception_swallowed(monkeypatch):
    """Progress never fails a rebuild: a sink that raises is ignored, and
    with no sink installed every call is a no-op."""
    module = _progress()

    def broken(name, counters, forced):
        raise OSError("disk full")

    module.install(broken)
    try:
        module.phase("rebuild:read")
        module.tick(lines=1)
        with module.waiting("ingest-lock"):
            pass
    finally:
        module.uninstall()
    calls: list = []
    module.phase("rebuild:read")
    module.tick(lines=1)
    with module.waiting("ingest-lock"):
        calls.append("body")
    assert calls == ["body"]


# --------------------------------------------------------------------------
# A completion's phases
# --------------------------------------------------------------------------

_W1 = int(dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc).timestamp())

#: Every phase name the replays emit; a sink must never see another.
VOCABULARY = {
    "completion:predecessor", "completion:owed-outputs", "completion:read",
    "completion:replay-all", "completion:replay-without",
    "completion:replay-with", "completion:append",
    "rebuild:read", "rebuild:selection", "rebuild:quota-leg",
    "rebuild:fold", "rebuild:open-blocks", "rebuild:stats-fold",
    "rebuild:validate", "rebuild:publish",
    "waiting:maintenance-lock", "waiting:ingest-lock",
    "waiting:quota-flocks",
}


def _obs(J, at, pct):
    return J.make_obs(at=at, src="record-usage", provider="claude", payload={
        "weekly_percent": pct, "resets_at": _W1, "source": "statusline",
        "captured_at": at})


def _is_subsequence(wanted, seen) -> bool:
    it = iter(seen)
    return all(any(item == name for item in it) for name in wanted)


def test_completion_emits_phases_in_order(monkeypatch, tmp_path):
    """Spec D9: a completion that journals a missing decision reports its
    predecessor build, its window read, its three replays and its append, and
    then the final rebuild's phases, in that order."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_journal as jr
    import _lib_journal as J
    module = _progress()

    jr.append_record(_obs(J, "2026-01-04T09:00:00Z", 7.0))
    jr.run_stats_ingest(mode="authoritative")
    # A reading appended and never ingested: the rebuild must derive it.
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))

    writes: list = []
    module.install(lambda name, counters, forced: writes.append(
        (name, forced)))
    try:
        assert ns["cmd_db_rebuild"](
            argparse.Namespace(db="stats", json=True)) == 0
    finally:
        module.uninstall()
    names = [name for name, _forced in writes]
    assert names, "no phase reached the sink"
    assert set(names) <= VOCABULARY, set(names) - VOCABULARY
    assert _is_subsequence([
        "completion:predecessor", "completion:read",
        "completion:replay-all", "completion:replay-without",
        "completion:replay-with", "completion:append",
        "rebuild:read", "rebuild:selection", "rebuild:fold",
        "rebuild:open-blocks", "rebuild:stats-fold", "rebuild:validate",
        "rebuild:publish",
    ], names), names
    final = [n for n in names if not n.startswith("waiting:")]
    assert final[-1] == "rebuild:publish", final
