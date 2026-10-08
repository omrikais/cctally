"""proc_pid_rusage(RUSAGE_INFO_V4) sampler for the #901 write-attribution probe.

usage: rusage.py PID...   -> one JSON line per pid:
  {"t", "pid", "rc", "errno", "dwrite", "dread", "logical_writes", "resident",
   "footprint", "footprint_peak", "cpu_abs"}

A failed read (rc != 0) emits rc/errno and null counters, never zeros (#901 SR-004);
analyze.py refuses a window whose endpoints or coverage rest on failed samples.
dwrite/dread are ri_diskio_byteswritten/bytesread (kernel-accounted process I/O);
logical_writes is ri_logical_writes; resident/footprint are the current
ri_resident_size/ri_phys_footprint; footprint_peak is ri_lifetime_max_phys_footprint, a
true high-water mark that includes transients between samples (#901 SR-005); cpu_abs is
user+system time in Mach absolute-time units (not nanoseconds on Apple silicon).
"""
import ctypes, json, sys, time

libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
libc.proc_pid_rusage.restype = ctypes.c_int
RUSAGE_INFO_V4 = 4
_V2 = ('user', 'sys', 'idle_wk', 'int_wk', 'pageins', 'wired', 'resident', 'footprint', 'start', 'exit',
       'c_user', 'c_sys', 'c_idle', 'c_int', 'c_pageins', 'c_elapsed', 'dread', 'dwrite')
_V3 = ('qos_default', 'qos_maintenance', 'qos_background', 'qos_utility', 'qos_legacy',
       'qos_user_initiated', 'qos_user_interactive', 'billed_system', 'serviced_system')
_V4 = ('logical_writes', 'lifetime_max_footprint', 'instructions', 'cycles', 'billed_energy',
       'serviced_energy', 'interval_max_footprint', 'runnable_time')


class RU(ctypes.Structure):
    _fields_ = [('uuid', ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in _V2 + _V3 + _V4]


for pid in map(int, sys.argv[1:]):
    ru = RU()
    rc = libc.proc_pid_rusage(pid, RUSAGE_INFO_V4, ctypes.byref(ru))
    ok = rc == 0
    rec = {"t": time.time(), "pid": pid, "rc": rc, "errno": ctypes.get_errno() if rc else 0}
    for out, field in (("dwrite", "dwrite"), ("dread", "dread"), ("logical_writes", "logical_writes"),
                       ("resident", "resident"), ("footprint", "footprint"),
                       ("footprint_peak", "lifetime_max_footprint")):
        rec[out] = getattr(ru, field) if ok else None
    rec["cpu_abs"] = (ru.user + ru.sys) if ok else None
    print(json.dumps(rec))
