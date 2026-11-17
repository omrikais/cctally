"""#997 T-A5 — a real resident peak in the rebuild record (spec D11, T9).

`peakHeapBytes` is a `tracemalloc` peak, zero unless tracing, and it never saw
the memory that pushed the October 22 rebuild into hours of compression.
`peakResidentBytes` is the measuring process's own resident peak since its
exec: `getrusage(RUSAGE_SELF).ru_maxrss` in bytes on macOS, and on Linux
`VmHWM` from `/proc/self/status` (#1017), because Linux's `ru_maxrss` keeps
the peak of the address space the process replaced at `execve`, which for a
detached rebuild worker is the peak of the process that launched it. When
`VmHWM` is unavailable or malformed, Linux falls back to `ru_maxrss` in KiB.
"""
from __future__ import annotations

import builtins
import importlib
import json
import os
import pathlib
import subprocess
import sys
import types

import pytest

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


# The defect this test guards (#1017): Linux keeps a process's `ru_maxrss`
# across `execve`, so a child launched from a large process reports that
# process's peak as its own (a pytest worker's 0.8 GB showed up as the peak
# of a 100 MB driver on the public matrix, and a small rebuild worker started
# by a large dashboard reports the dashboard's peak). The spawner below is
# that large process, made explicit: it maps and touches more than either
# driver, releases it, records its own peak, and then launches each driver
# with the rebuild worker's launch flags. A reading that includes the
# launcher's peak makes both drivers report the spawner's peak.
_SPAWNER = "DRIVER = " + repr(_DRIVER) + r"""
import json, mmap, subprocess, sys
sys.path.insert(0, sys.argv[1])
import _lib_rebuild_progress as progress
size = int(sys.argv[2]) * 1024 * 1024
block = mmap.mmap(-1, size)
for page in range(0, size, mmap.PAGESIZE):
    block[page] = 1
block.close()
spawner_peak = progress.peak_resident_bytes()
drivers = {}
for mb in sys.argv[3:]:
    out = subprocess.run(
        [sys.executable, "-c", DRIVER, sys.argv[1], mb],
        capture_output=True, text=True, timeout=120,
        start_new_session=True, close_fds=True)
    if out.returncode != 0:
        sys.stderr.write(f"driver {mb} MiB exited {out.returncode}:\n"
                         f"{out.stderr}")
        sys.exit(1)
    drivers[mb] = json.loads(out.stdout)
print(json.dumps({"spawnerPeak": spawner_peak, "drivers": drivers}))
"""


def _drive_from_spawner(spawner_mb: int, driver_mbs: tuple) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", _SPAWNER, str(BIN), str(spawner_mb),
         *(str(mb) for mb in driver_mbs)],
        capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, (
        f"spawner exited {out.returncode}:\n{out.stderr}")
    raw = json.loads(out.stdout)
    return {"spawnerPeak": raw["spawnerPeak"],
            "drivers": {int(mb): reading
                        for mb, reading in raw["drivers"].items()}}


def test_peak_resident_bytes_tracks_allocation():
    """Spec T9, #1017 T2: from a 600 MiB spawner, each driver's peak is at
    least its allocation and at most its allocation plus 1 GiB (interpreter
    baseline and tolerance), the 400 MiB peak exceeds the 100 MiB one by at
    least 250 MiB, the current resident value taken after the release is
    well below the peak, and the spawner's own peak is at least 600 MiB and
    above each driver's. Detects a worker's peak that includes the peak of
    the process that launched it (on Linux both drivers would report the
    spawner's), a constant, a current-RSS substitute and a gross unit error.
    """
    readings = _drive_from_spawner(600, (400, 100))
    print(json.dumps(readings, sort_keys=True))
    spawner = readings["spawnerPeak"]
    big = readings["drivers"][400]
    small = readings["drivers"][100]
    assert big["peak"] >= 400 * MB, big
    assert small["peak"] >= 100 * MB, small
    assert big["peak"] <= 400 * MB + 1024 * MB, big
    assert small["peak"] <= 100 * MB + 1024 * MB, small
    assert big["peak"] - small["peak"] >= 250 * MB, (big, small)
    assert spawner >= 600 * MB, readings
    assert spawner > big["peak"], readings
    assert spawner > small["peak"], readings
    assert big["current"] is not None and small["current"] is not None
    assert big["current"] <= big["peak"] - 250 * MB, big
    assert small["current"] <= small["peak"] - 50 * MB, small


_INHERITED_KIB = 819200  # an 800 MiB spawner peak, as Linux reports it
_FALLBACK = _INHERITED_KIB * 1024
_MISSING = object()


def _status_excerpt(hwm_line: "str | None") -> str:
    """A realistic `/proc/self/status` excerpt around the `VmHWM` line."""
    lines = ["Name:\tpython3", "Umask:\t0022", "State:\tR (running)",
             "Tgid:\t4242", "VmPeak:\t  912340 kB", "VmSize:\t  861200 kB",
             "VmLck:\t       0 kB", "VmPin:\t       0 kB"]
    if hwm_line is not None:
        lines.append(hwm_line)
    lines += ["VmRSS:\t   10240 kB", "RssAnon:\t    4096 kB", "Threads:\t1"]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(("platform", "ru_maxrss", "hwm_line", "expected",
                          "guard_open"), [
    pytest.param("linux", _INHERITED_KIB, "VmHWM:\t   51200 kB", 52428800,
                 False, id="linux-inherited-spawner"),
    pytest.param("linux", 3145728, "VmHWM:\t 3145728 kB", 3221225472,
                 False, id="linux-own-peak-above-spawner"),
    pytest.param("linux", _INHERITED_KIB, "VmHWM:\t       0 kB", 0,
                 False, id="linux-explicit-zero"),
    pytest.param("linux", _INHERITED_KIB, _MISSING, _FALLBACK,
                 False, id="linux-fallback-missing-file"),
    pytest.param("linux", _INHERITED_KIB, None, _FALLBACK,
                 False, id="linux-fallback-no-line"),
    pytest.param("linux", _INHERITED_KIB, "VmHWM:\t  -51200 kB", _FALLBACK,
                 False, id="linux-fallback-negative"),
    pytest.param("linux", _INHERITED_KIB, "VmHWM:\t 51200.5 kB", _FALLBACK,
                 False, id="linux-fallback-fractional"),
    pytest.param("linux", _INHERITED_KIB, "VmHWM:\t   51200", _FALLBACK,
                 False, id="linux-fallback-no-unit"),
    pytest.param("linux", _INHERITED_KIB, "VmHWM:\t   51200 MB", _FALLBACK,
                 False, id="linux-fallback-wrong-unit"),
    pytest.param("linux", _INHERITED_KIB, "VmHWM:\t   51200 kB 7",
                 _FALLBACK, False, id="linux-fallback-four-tokens"),
    pytest.param("linux", _INHERITED_KIB, "VmHWM:\t  0051200 kB", 52428800,
                 False, id="linux-whitespace-leading-zeros"),
    pytest.param("darwin", 1000, "VmHWM:\t   51200 kB", 1000,
                 True, id="darwin-unchanged"),
    pytest.param("freebsd14", 1000, "VmHWM:\t   51200 kB", 1024000,
                 True, id="other-platform-kib"),
])
def test_peak_resident_bytes_platform_readings(
        tmp_path, monkeypatch, platform, ru_maxrss, hwm_line, expected,
        guard_open):
    """#1017 T1, the reader's seam: on Linux a worker whose `ru_maxrss`
    carries its launcher's 800 MiB peak reports its own 50 MiB `VmHWM`.
    Detects an inherited spawner peak recorded as the worker's, a genuine
    larger own peak lost, a missing or malformed reading turned into zero
    (each falls back to `ru_maxrss` KiB), a wrong unit, and the macOS or
    other-platform reading altered or made to read the status file."""
    sys.path.insert(0, str(BIN))
    progress = importlib.import_module("_lib_rebuild_progress")
    status = tmp_path / "status"
    if hwm_line is not _MISSING:
        status.write_text(_status_excerpt(hwm_line))
    # raising=False: `main`'s module has no such constant, so this test
    # reaches its numeric assertion there instead of failing at setup.
    monkeypatch.setattr(progress, "_PROC_SELF_STATUS", str(status),
                        raising=False)
    usage = types.SimpleNamespace(ru_maxrss=ru_maxrss)
    monkeypatch.setattr(progress.resource, "getrusage", lambda who: usage)
    monkeypatch.setattr(progress.sys, "platform", platform)
    if guard_open:
        guarded = {str(status), "/proc/self/status"}
        real_open = builtins.open

        def open_guard(file, *args, **kwargs):
            # pytest.fail, not an OSError: the fallback would swallow that.
            if (isinstance(file, (str, bytes, os.PathLike))
                    and os.fsdecode(file) in guarded):
                pytest.fail(f"{platform} opened the status file {file!r}")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", open_guard)
    assert progress.peak_resident_bytes() == expected


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
