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
