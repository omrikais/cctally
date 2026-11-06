"""#997 T-A5 — a real resident peak in the rebuild record (spec D11, T9).

`peakHeapBytes` is a `tracemalloc` peak, zero unless tracing, and it never saw
the memory that pushed the October 22 rebuild into hours of compression.
`peakResidentBytes` is `getrusage(RUSAGE_SELF).ru_maxrss`, the process
lifetime peak: bytes on macOS, KiB on Linux.
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import subprocess
import sys
import types

from conftest import load_script, redirect_paths

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
MB = 1024 * 1024

_DRIVER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import mmap
import _lib_rebuild_progress as progress
size = int(sys.argv[2]) * 1024 * 1024
# An anonymous mapping, so the release is an munmap the kernel accounts at
# once (macOS malloc keeps a freed large block resident until pressure).
block = mmap.mmap(-1, size)
for page in range(0, size, mmap.PAGESIZE):
    block[page] = 1
block.close()
print(json.dumps({"peak": progress.peak_resident_bytes(),
                  "current": progress.current_resident_bytes()}))
"""


# Linux keeps a process's `ru_maxrss` across `execve`, and a child forked or
# vforked from this test process starts from this process's resident peak (a
# pytest worker reported 0.8 GB as the peak of a 100 MB driver on the public
# matrix). A small intermediate interpreter launches the driver, so the
# driver's peak starts from that interpreter's, not from the test runner's.
_LAUNCHER = r"""
import subprocess, sys
sys.exit(subprocess.run(sys.argv[1:]).returncode)
"""


def _drive(megabytes: int) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", _LAUNCHER,
         sys.executable, "-c", _DRIVER, str(BIN), str(megabytes)],
        check=True, capture_output=True, text=True, timeout=120)
    return json.loads(out.stdout)


def test_peak_resident_bytes_tracks_allocation():
    """Spec T9: each peak is at least its allocation and at most its
    allocation plus 1 GiB (interpreter baseline and tolerance), the 400 MB
    peak exceeds the 100 MB one by at least 250 MB, and the current resident value taken
    after the release is well below the peak. A constant, a current-RSS
    substitute or a wrong unit fails one of these."""
    big = _drive(400)
    small = _drive(100)
    assert big["peak"] >= 400 * MB, big
    assert small["peak"] >= 100 * MB, small
    assert big["peak"] <= 400 * MB + 1024 * MB, big
    assert small["peak"] <= 100 * MB + 1024 * MB, small
    assert big["peak"] - small["peak"] >= 250 * MB, (big, small)
    assert big["current"] is not None and small["current"] is not None
    assert big["current"] <= big["peak"] - 250 * MB, big
    assert small["current"] <= small["peak"] - 50 * MB, small


def test_peak_resident_bytes_linux_units(monkeypatch):
    """Spec D11: `ru_maxrss` is KiB on Linux and bytes on macOS."""
    sys.path.insert(0, str(BIN))
    progress = importlib.import_module("_lib_rebuild_progress")
    usage = types.SimpleNamespace(ru_maxrss=1000)
    monkeypatch.setattr(progress.resource, "getrusage", lambda who: usage)
    monkeypatch.setattr(progress.sys, "platform", "linux")
    assert progress.peak_resident_bytes() == 1000 * 1024
    monkeypatch.setattr(progress.sys, "platform", "darwin")
    assert progress.peak_resident_bytes() == 1000


def test_rebuild_record_carries_peak_resident_bytes(tmp_path, monkeypatch):
    """Spec D11: the rebuild record gains `peakResidentBytes` beside
    `peakHeapBytes`, which keeps its meaning."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr = importlib.import_module("_cctally_journal")
    core = importlib.import_module("_cctally_core")
    progress = importlib.import_module("_lib_rebuild_progress")
    monkeypatch.setattr(progress, "peak_resident_bytes", lambda: 123_456_789)
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    paths = sorted(pathlib.Path(core.LOG_DIR).glob("stats-rebuild-*.json"))
    assert paths, "no rebuild record was written"
    record = json.loads(paths[-1].read_text())
    assert record["peakResidentBytes"] == 123_456_789
    assert "peakHeapBytes" in record


# --------------------------------------------------------------------------
# #1004 T5 — the rebuild retains coordinates, not decoded records
# --------------------------------------------------------------------------


def _journal_of_ops(count: int) -> None:
    """``count`` `account_observe` ops (retained, streamed, and kept by no
    downstream structure before the quota leg) and one Codex quota
    observation (a `None` slot) per ten ops, in two segments."""
    import journal_fixture_496_s5b as F
    import _lib_journal as J
    core = importlib.import_module("_cctally_core")
    records = []
    for n in range(count):
        at = f"2026-07-{1 + n // 1440 % 28:02d}T{n // 60 % 24:02d}:{n % 60:02d}:00Z"
        records.append(J.make_account_observe(
            at=at, account_key="claude:acct-1004", provider="claude",
            label=f"probe-{n}"))
        if not n % 10:
            records.append(F.quota_obs(at, n))
    half = len(records) // 2
    F._append(pathlib.Path(core.JOURNAL_DIR), "observations-2026-07.jsonl",
              records[:half])
    F._append(pathlib.Path(core.JOURNAL_DIR), "observations-2026-08.jsonl",
              records[half:])


def test_rebuild_quota_leg_entry_retains_coordinates(tmp_path, monkeypatch):
    """#1004 T5 (the rebuild reader): at the quota leg's entry the read pass
    retains under 64 bytes per retained position (streamed records, `None`
    slots and quota coordinates alike: an 8-byte slot, 12 bytes of
    coordinates and an 8-byte digest), where it retained each decoded
    record (several hundred bytes) before. Measured as the traced-heap
    difference between a 2,000- and a 12,000-op journal, so a rebuild's
    fixed cost cancels, with #997 D15's interning off (it only makes equal
    short strings one object, and the interned-string table's resizing
    otherwise moves a delta by megabytes) and after a warm-up rebuild."""
    import gc
    import tracemalloc
    readings = {}
    for label, count in (("small", 2_000), ("large", 12_000)):
        ns = load_script()
        redirect_paths(ns, monkeypatch, tmp_path / label)
        jr = importlib.import_module("_cctally_journal")
        jl = importlib.import_module("_lib_journal")
        monkeypatch.setattr(jl, "compact_retained", lambda value: value)
        _journal_of_ops(count)
        seen: list = []
        real = jr._rebuild_quota_cache_leg_raw

        def measuring(quota_raw, decoded, *args, _real=real, _seen=seen,
                      **kwargs):
            gc.collect()
            streamed = sum(1 for i in range(len(decoded))
                           if getattr(decoded, "is_streamed",
                                      lambda _i: False)(i))
            _seen.append((tracemalloc.get_traced_memory()[0], len(decoded),
                          streamed))
            return _real(quota_raw, decoded, *args, **kwargs)

        monkeypatch.setattr(jr, "_rebuild_quota_cache_leg_raw", measuring)
        context = jr.RebuildContext(trigger="test-fixture")
        jr.rebuild_stats_index(context=context)  # the warm-up
        gc.collect()
        tracemalloc.start()
        try:
            jr.rebuild_stats_index(context=context)
        finally:
            tracemalloc.stop()
        readings[label] = seen[-1]
    traced = readings["large"][0] - readings["small"][0]
    positions = readings["large"][1] - readings["small"][1]
    streamed = readings["large"][2] - readings["small"][2]
    per_position = traced / positions
    print(f"T5 rebuild quota-leg entry: {per_position:.1f} bytes per "
          f"position, {traced / max(streamed, 1):.1f} per streamed record "
          f"({positions} positions, {streamed} streamed)")
    assert streamed >= 10_000, readings
    assert per_position < 64, (
        f"the rebuild's read pass retains {per_position:.1f} bytes per "
        "position at the quota leg's entry")
