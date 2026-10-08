"""#901 probe harness: attribute SQLite temp-file (etilqs_*) bytes to SQL statements.

Runs `bin/cctally <args>` in-process with sqlite3.connect patched so every
connection gets a trace callback. At each statement start on a thread, the
bytes the thread wrote to SQLite temp files since its previous statement start
are charged to that previous statement (sorts spill during the first step,
before the next statement on the same thread can start).

env: SQLATTR_OUT=<json path>, SQLATTR_TREE=<repo tree>, SQLATTR_DYLIB=<the wtrace.dylib
also named in DYLD_INSERT_LIBRARIES>. Per statement it records calls, temp bytes, other
bytes, the largest single temp charge (for a statement journal, the high-water of its
file per execution) and the wall time from its start to the next statement start on the
same thread (an upper bound that includes intervening work: an attribution aid, never
latency evidence).

Completeness (#901 spec §6.3 D, 901-PA-004):
* Each thread's FINAL interval is flushed: a thread's last statement is charged when the
  thread finishes (Thread.run is wrapped) and the main thread's at exit, so nothing
  written after a thread's last statement start is lost.
* Totals are complete: every statement is kept (`stmts`, not only a top-200 display),
  and at exit the interposer's own process totals give an explicit `unattributed`
  bucket (temp and other bytes the threads wrote outside any statement interval, or on
  a daemon thread still running at exit), so attributed + unattributed reconciles with
  the interposer's per-process total; workers are their own processes with their own
  interposer traces.
* `SQLATTR_TEMP_STORE=DEFAULT` is the labelled FILE-control (§6.3 statement-journal
  evidence): the enumerated writers keep SQLite's default (file) temp store, so each
  statement journal's extent is visible on disk. Diagnostic only, never a substitute for
  a MEMORY-mode run.
* The output also records this process's lifetime peak physical footprint.
"""
import ctypes, json, os, re, sqlite3, sys, threading, time, atexit


class _StubLib:
    """`SQLATTR_DYLIB=stub`: the interposer's four entry points in Python,
    for the tool's own self-tests (no compiler, no interposer). A test script
    advances the counters through `add_temp` / `add_untracked_temp`."""

    def __init__(self):
        tls = threading.local()
        lock = threading.Lock()
        totals = {"temp": 0, "other": 0}

        def thread_temp():
            return getattr(tls, "temp", 0)

        def thread_other():
            return getattr(tls, "other", 0)

        def dump_to(path):
            with lock:
                snap = {"t": time.time(), "pid": os.getpid(), "dropped": 0,
                        "paths": {"/stub/etilqs_stub": [totals["temp"], 1],
                                  "/stub/other": [totals["other"], 1]}}
            with open(path.decode() if isinstance(path, bytes) else path,
                      "a") as fh:
                fh.write(json.dumps(snap) + "\n")
            return 0

        def add_temp(n):
            tls.temp = getattr(tls, "temp", 0) + n
            with lock:
                totals["temp"] += n

        def add_untracked_temp(n):
            with lock:
                totals["temp"] += n

        self.wtrace_thread_temp = thread_temp
        self.wtrace_thread_other = thread_other
        self.wtrace_dump_to = dump_to
        self.add_temp = add_temp
        self.add_untracked_temp = add_untracked_temp


lib = (_StubLib() if os.environ["SQLATTR_DYLIB"] == "stub"
       else ctypes.CDLL(os.environ["SQLATTR_DYLIB"]))
if os.environ["SQLATTR_DYLIB"] == "stub":
    sys.modules["sqlattr_stub"] = lib   # how a self-test script reaches it
lib.wtrace_thread_temp.restype = ctypes.c_ulonglong
lib.wtrace_thread_other.restype = ctypes.c_ulonglong
lib.wtrace_dump_to.argtypes = [ctypes.c_char_p]
OUT = os.environ["SQLATTR_OUT"]
TREE = os.environ["SQLATTR_TREE"]
BIN = os.path.join(TREE, "bin")
CONTROL = os.environ.get("SQLATTR_TEMP_STORE")

agg = {}  # key -> [count, temp_bytes, other_bytes, max_temp, wall_s]
lock = threading.Lock()
tls = threading.local()
t0 = time.time()
timeline = []  # (t, thread, temp_bytes, key) for statements with >1 MB temp
flushed_threads = [0]


def site():
    f = sys._getframe(2); out = []
    while f is not None and len(out) < 6:
        fn = f.f_code.co_filename
        if fn.startswith(BIN):
            out.append(f"{os.path.basename(fn)}:{f.f_lineno}:{f.f_code.co_name}")
        f = f.f_back
    return " < ".join(out)


def norm(sql):
    s = re.sub(r"\s+", " ", sql).strip()
    s = re.sub(r"'[^']*'", "?", s)
    s = re.sub(r"\b\d+(\.\d+)?\b", "N", s)
    return s[:400]


def close_prev(now_temp, now_other, now):
    prev = getattr(tls, "prev", None)
    if prev is None:
        return
    tls.prev = None
    key, temp0, other0, ts = prev
    dt, do, dw = now_temp - temp0, now_other - other0, now - ts
    with lock:
        a = agg.setdefault(key, [0, 0, 0, 0, 0.0])
        a[0] += 1; a[1] += dt; a[2] += do; a[3] = max(a[3], dt); a[4] += dw
        if dt > 1_000_000:
            timeline.append((round(ts - t0, 2), threading.current_thread().name, dt, key))


def flush_this_thread():
    """Charge the calling thread's final interval (its last statement)."""
    if getattr(tls, "prev", None) is not None:
        close_prev(lib.wtrace_thread_temp(), lib.wtrace_thread_other(), time.time())
        with lock:
            flushed_threads[0] += 1


def cb(sql):
    now_temp = lib.wtrace_thread_temp(); now_other = lib.wtrace_thread_other(); now = time.time()
    close_prev(now_temp, now_other, now)
    tls.prev = (norm(sql) + "  @@  " + site(), now_temp, now_other, now)


_orig_connect = sqlite3.connect


def connect(*a, **k):
    c = _orig_connect(*a, **k)
    try:
        c.set_trace_callback(cb)
    except Exception:
        pass
    return c


sqlite3.connect = connect

_orig_run = threading.Thread.run


def run(self, *a, **k):
    try:
        return _orig_run(self, *a, **k)
    finally:
        flush_this_thread()


threading.Thread.run = run


def footprint_peak():
    if sys.platform != "darwin":
        return None
    names = [f"f{i}" for i in range(27)] + ["logical_writes", "lifetime_max_footprint"] + [
        f"g{i}" for i in range(6)]

    class V4(ctypes.Structure):
        _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in names]

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.proc_pid_rusage.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    libc.proc_pid_rusage.restype = ctypes.c_int
    info = V4()
    if libc.proc_pid_rusage(os.getpid(), 4, ctypes.byref(info)) != 0:
        return None
    return int(info.lifetime_max_footprint)


def interposer_totals():
    """This process's temp and other bytes from the interposer's own table."""
    snap_path = OUT + ".totals.jsonl"
    try:
        os.unlink(snap_path)
    except FileNotFoundError:
        pass
    if lib.wtrace_dump_to(snap_path.encode()) != 0:
        return None
    with open(snap_path) as fh:
        snap = json.loads(fh.read().strip().splitlines()[-1])
    temp = sum(v[0] for k, v in snap["paths"].items()
               if os.path.basename(k).startswith("etilqs_"))
    return {"temp": temp, "other": sum(v[0] for v in snap["paths"].values()) - temp,
            "dropped": snap.get("dropped", 0), "framesDropped": snap.get("framesDropped", 0)}


def dump(final=False):
    with lock:
        rows = sorted(agg.items(), key=lambda kv: -kv[1][1])
        stmts = [[k] + v for k, v in rows]
        attributed_temp = sum(v[1] for _k, v in rows)
        attributed_other = sum(v[2] for _k, v in rows)
        data = {"elapsed": time.time() - t0, "pid": os.getpid(),
                "control": {"tempStore": CONTROL} if CONTROL else None,
                "stmts": stmts, "top": stmts[:200],
                "timeline": timeline[-2000:],
                "attributed": {"temp": attributed_temp, "other": attributed_other},
                "flushedThreads": flushed_threads[0], "final": final}
    if final:
        totals = interposer_totals()
        data["interposer"] = totals
        data["unattributed"] = (None if totals is None else {
            "temp": totals["temp"] - attributed_temp,
            "other": totals["other"] - attributed_other})
        data["footprintPeakBytes"] = footprint_peak()
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, OUT)


def dumper():
    while True:
        time.sleep(10)
        try:
            dump()
        except Exception as e:
            print("sqlattr dump failed", e, file=sys.stderr)


def at_exit():
    flush_this_thread()
    dump(final=True)


threading.Thread(target=dumper, name="sqlattr-dumper", daemon=True).start()
atexit.register(at_exit)

sys.argv = [os.path.join(BIN, "cctally")] + sys.argv[1:]
sys.path.insert(0, BIN)
try:
    if CONTROL:
        # The labelled FILE-control: load the CLI as a module (its siblings,
        # `_cctally_store` included, load with it), set the shared policy
        # helper's temp store, then run its entry point.
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader(
            "cctally", os.path.join(BIN, "cctally"))
        spec = importlib.util.spec_from_loader("cctally", loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["cctally"] = module
        loader.exec_module(module)
        sys.modules["_cctally_store"].WRITER_TEMP_STORE = CONTROL
        raise SystemExit(module.main())
    import runpy
    runpy.run_path(os.path.join(BIN, "cctally"), run_name="__main__")
except SystemExit:
    at_exit()
    atexit.unregister(at_exit)
    raise
