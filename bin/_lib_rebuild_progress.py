"""Progress and resident memory for the stats index's full replays (#997).

The 2026-10-22 epoch rebuild ran 4.4 hours and died without a word, because
nothing it did between start and death reached a file. This module is the seam
a detached worker's state file hangs off (spec D9):

* ``install(sink)`` / ``uninstall()``: one process-wide sink, called as
  ``sink(phase, counters, forced)``. Only a worker holding its flock installs
  one; everywhere else every call below is a no-op.
* ``phase(name, **counters)``: where the replay is now. Always written
  (``forced``); the counters start over with the ones given.
* ``tick(**counters)``: merges counters into the current phase's and writes
  them at most every ``THROTTLE_SECONDS`` (``time.monotonic``), so a call
  site can tick on every batch without turning progress into I/O.
* ``waiting(lock)``: a context for a blocking lock wait, written as the phase
  ``waiting:<lock>`` and replaced by the interrupted phase when it ends.

A sink that raises is ignored: progress never fails a rebuild.

``peak_resident_bytes`` is the process-lifetime peak the rebuild record and the
worker state report as ``peakResidentBytes`` (spec D11): this process's own
resident peak since its exec. macOS reports ``ru_maxrss`` in bytes. Linux
reads ``VmHWM`` from ``/proc/self/status`` (#1017), the peak of the address
space created at ``execve``, because Linux's ``ru_maxrss`` also keeps the
peak of the address space the exec replaced, so a detached worker would
report the peak of the process that launched it. Without a usable ``VmHWM``,
Linux falls back to ``ru_maxrss`` in KiB, which can still include an
ancestor's peak. ``peakHeapBytes``, a ``tracemalloc`` peak that is zero unless
tracing, never saw the memory that pushed that rebuild into hours of
compression.

Stdlib only, and importable on its own.
"""
from __future__ import annotations

import contextlib
import os
import resource
import subprocess
import sys
import time

#: The longest a ticking replay goes without a write (spec D9).
THROTTLE_SECONDS = 10.0

_sink = None
_phase: "str | None" = None
_counters: dict = {}
_last_write: "float | None" = None


def install(sink) -> None:
    """Route progress to ``sink(phase, counters, forced)`` from now on."""
    global _sink, _phase, _counters, _last_write
    _sink = sink
    _phase = None
    _counters = {}
    _last_write = None


def uninstall() -> None:
    """Stop routing progress; every call becomes a no-op again."""
    global _sink, _phase, _counters, _last_write
    _sink = None
    _phase = None
    _counters = {}
    _last_write = None


def _emit(forced: bool) -> None:
    global _last_write
    _last_write = time.monotonic()
    sink = _sink
    if sink is None:
        return
    try:
        sink(_phase, dict(_counters), forced)
    except Exception:  # progress never fails a rebuild
        pass


def phase(name: str, **counters) -> None:
    """Enter phase ``name`` and write it now."""
    global _phase, _counters
    if _sink is None:
        return
    _phase = name
    _counters = dict(counters)
    _emit(True)


def tick(**counters) -> None:
    """Record ``counters`` and write them if the throttle allows."""
    if _sink is None:
        return
    _counters.update(counters)
    if (_last_write is not None
            and time.monotonic() - _last_write < THROTTLE_SECONDS):
        return
    _emit(False)


@contextlib.contextmanager
def waiting(lock: str):
    """Report a blocking wait on ``lock`` for as long as the body runs."""
    global _phase, _counters
    if _sink is None:
        yield
        return
    previous, saved = _phase, dict(_counters)
    phase(f"waiting:{lock}")
    try:
        yield
    finally:
        if _sink is not None:
            _phase, _counters = previous, saved
            _emit(True)


#: Where Linux reports this process's ``VmHWM`` (a constant so a test can
#: point it at an excerpt).
_PROC_SELF_STATUS = "/proc/self/status"


def _vm_hwm_bytes() -> "int | None":
    """``VmHWM`` from ``_PROC_SELF_STATUS`` in bytes, or None when unusable.

    The line must read ``VmHWM: <unsigned decimal> kB`` (three tokens; any
    spacing and leading zeros). A missing line or any other shape answers
    None, so missing data never becomes zero; ``0`` itself is a reading.
    """
    try:
        with open(_PROC_SELF_STATUS, "rb") as handle:
            for line in handle:
                fields = line.split()
                if not fields or fields[0] != b"VmHWM:":
                    continue
                if (len(fields) != 3 or not fields[1].isdigit()
                        or fields[2] != b"kB"):
                    return None
                return int(fields[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def peak_resident_bytes() -> int:
    """This process's own resident peak since its exec, in bytes.

    macOS: ``getrusage(RUSAGE_SELF).ru_maxrss``, which is in bytes. Linux:
    ``VmHWM``, the high-water mark of the address space created at
    ``execve``; ``ru_maxrss`` would also carry the pre-exec peak, which for a
    detached rebuild worker is its launcher's (#1017). When ``VmHWM`` is
    unavailable or malformed, Linux falls back to ``ru_maxrss`` in KiB, which
    can still include an ancestor's peak. Elsewhere ``ru_maxrss`` is taken as
    KiB. The platform is decided per call from ``sys.platform`` so a test can
    exercise every branch.
    """
    if sys.platform.startswith("linux"):
        hwm = _vm_hwm_bytes()
        if hwm is not None:
            return hwm
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return peak
    return peak * 1024


def current_resident_bytes() -> "int | None":
    """This process's current resident set in bytes, or None when unknown.

    Best effort, because no stdlib call reports it on macOS: Linux reads
    ``/proc/self/statm``; elsewhere ``ps -o rss=`` (KiB) is asked under
    ``LC_ALL=C``. Any failure answers None rather than raising, since a
    progress reading must never fail a rebuild.
    """
    try:
        with open("/proc/self/statm", "rb") as handle:
            fields = handle.read().split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "LC_ALL": "C"}, check=False)
        return int(out.stdout.strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
