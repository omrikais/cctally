"""Self-test for wtrace.dylib (#901 SR-003): lossless accounting under concurrent snapshots.

Run under the interposer it tests, with Homebrew Python (not a SIP-protected launcher):

    WTRACE_TEST_PAUSE_US=300000 DYLD_INSERT_LIBRARIES=$PWD/wtrace.dylib \
      WTRACE_DYLIB=$PWD/wtrace.dylib /opt/homebrew/bin/python3 selftest.py SCRATCH_DIR

Exit 0 and a final `selftest: PASS` line, or exit 1 naming the failed check. The
production-scale acceptance (spec §6.3) runs it before every measurement session and
records the result in the receipt; a failed or missing self-test invalidates the evidence.

1. Deterministic race: a snapshot is held open (WTRACE_TEST_PAUSE_US) while another
   thread closes a written SQLite-temp-named file. The closed file's bytes must appear
   in the next snapshot. The pre-fix interposer swapped its live table for the
   snapshot copy without the lock, so this close was folded into the copy and lost.
2. Stress: several threads write and close many temp-named files while snapshots are
   taken continuously; the final snapshot must equal the bytes actually written and
   every path's cumulative counter must be non-decreasing across snapshots.
3. WAL frame log (#901 SR-014): a WAL-mode SQLite database commits known pages; the
   flushed frame log must name exactly the frames the WAL file holds (frame index,
   page number, commit field and salt, in order), plus the WAL header record.
4. A full frame ring: with room for four records, a longer commit is counted in the
   snapshot's "framesDropped" and "dropped", never silently lost.
5. Fork safety: a child forked while a snapshot holds the trace mutex must still exec.
   Python's subprocess (close_fds) calls the interposed close() between fork and exec;
   without fork handlers the child inherits the held mutex, deadlocks before exec, and
   Popen blocks on its exec-error pipe past any timeout (#901 R-cov (i) stalled so).
"""
import ctypes, json, os, sqlite3, struct, subprocess, sys, threading

lib = ctypes.CDLL(os.environ["WTRACE_DYLIB"])
lib.wtrace_dump_to.argtypes = [ctypes.c_char_p]
lib.wtrace_frames_to.argtypes = [ctypes.c_char_p]
lib.wtrace_set_frame_ring.argtypes = [ctypes.c_size_t]
lib.wtrace_in_dump.restype = ctypes.c_int
scratch = sys.argv[1]
os.makedirs(scratch, exist_ok=True)
fail = []


def snapshots(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def total_for(snap, prefix):
    return sum(v[0] for k, v in snap["paths"].items() if os.path.basename(k).startswith(prefix))


# 1. Deterministic close-during-snapshot.
race_file = os.path.join(scratch, "etilqs_race_%d" % os.getpid())
race_out = os.path.join(scratch, "race.jsonl")
for p in (race_file, race_out):
    if os.path.exists(p):
        os.unlink(p)
payload = b"x" * 1_000_000
fd = os.open(race_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, payload)
dumper = threading.Thread(target=lib.wtrace_dump_to, args=(race_out.encode(),))
dumper.start()
for _ in range(2000):  # wait until the snapshot is inside its critical window
    if lib.wtrace_in_dump():
        break
    threading.Event().wait(0.001)
else:
    fail.append("race: snapshot never entered its window (WTRACE_TEST_PAUSE_US unset?)")
os.close(fd)  # folds this descriptor while the snapshot is in progress
dumper.join()
lib.wtrace_dump_to(race_out.encode())
race_snaps = snapshots(race_out)
got = total_for(race_snaps[-1], "etilqs_race_")
if got != len(payload):
    fail.append(f"race: closed file shows {got} bytes after the snapshot, wrote {len(payload)}")

# 2. Stress.
stress_out = os.path.join(scratch, "stress.jsonl")
if os.path.exists(stress_out):
    os.unlink(stress_out)
os.environ.pop("WTRACE_TEST_PAUSE_US", None)
written = [0] * 4
stop = threading.Event()


def worker(i):
    for j in range(300):
        p = os.path.join(scratch, f"etilqs_stress_{i}_{j % 7}")
        f = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        for _ in range(3):
            written[i] += os.write(f, b"y" * (4096 + j))
        os.close(f)


def snapper():
    while not stop.is_set():
        lib.wtrace_dump_to(stress_out.encode())


s = threading.Thread(target=snapper)
s.start()
ws = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
for w in ws:
    w.start()
for w in ws:
    w.join()
stop.set()
s.join()
lib.wtrace_dump_to(stress_out.encode())
snaps = snapshots(stress_out)
final = total_for(snaps[-1], "etilqs_stress_")
if final != sum(written):
    fail.append(f"stress: final snapshot {final} bytes, wrote {sum(written)}")
prev = {}
for n, snap in enumerate(snaps):
    if snap.get("dropped", 0):
        fail.append(f"stress: snapshot {n} dropped {snap['dropped']} bytes")
        break
    for k, prior in prev.items():
        if k not in snap["paths"] or snap["paths"][k][0] < prior:
            fail.append(f"stress: snapshot {n} lost or decreased {os.path.basename(k)}")
            break
    prev = {k: v[0] for k, v in snap["paths"].items()}

# 3. WAL frame log.
def wal_frames(path):
    data = open(path, "rb").read()
    page = struct.unpack(">I", data[8:12])[0]
    salt = data[16:24]
    out, off, k = [], 32, 1
    while off + 24 + page <= len(data):
        if data[off + 8:off + 16] != salt:
            break
        pgno, commit, salt1 = struct.unpack(">III", data[off:off + 12])
        out.append({"frame": k, "pgno": pgno, "commit": commit, "salt": salt1})
        off += 24 + page
        k += 1
    return page, out


def fresh(name):
    path = os.path.join(scratch, name)
    for side in ("", "-wal", "-shm"):
        if os.path.exists(path + side):
            os.unlink(path + side)
    return path


lib.wtrace_set_frame_ring(100000)
db = fresh("frames.db")
conn = sqlite3.connect(db)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE t(x)")
conn.executemany("INSERT INTO t VALUES (?)", [(os.urandom(3000),) for _ in range(40)])
conn.commit()
frames_out = os.path.join(scratch, "frames.jsonl")
if os.path.exists(frames_out):
    os.unlink(frames_out)
lib.wtrace_frames_to(frames_out.encode())
page, actual = wal_frames(db + "-wal")
conn.close()
logged = [json.loads(line) for line in open(frames_out) if line.strip()]
logged = [r for r in logged if r["path"].endswith("frames.db-wal")]
headers = [r for r in logged if r["frame"] == 0]
records = [{k: r[k] for k in ("frame", "pgno", "commit", "salt")} for r in logged if r["frame"]]
if not headers or headers[-1]["pgno"] != page:
    fail.append(f"frames: no WAL header record naming page size {page}")
if not actual or records[-len(actual):] != actual:
    fail.append(f"frames: logged {len(records)} frame records, the WAL holds {len(actual)} frames")

# 4. A full ring.
lib.wtrace_set_frame_ring(4)
db = fresh("frames-full.db")
conn = sqlite3.connect(db)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE t(x)")
conn.executemany("INSERT INTO t VALUES (?)", [(os.urandom(3000),) for _ in range(20)])
conn.commit()
conn.close()
full_out = os.path.join(scratch, "frames-full.jsonl")
if os.path.exists(full_out):
    os.unlink(full_out)
lib.wtrace_dump_to(full_out.encode())
snap = snapshots(full_out)[-1]
if not snap.get("framesDropped") or not snap.get("dropped"):
    fail.append("frames: a full ring did not count its dropped records")
if not isinstance(snap.get("footprintPeak"), int) or snap["footprintPeak"] <= 0:
    fail.append("footprint: the snapshot carries no lifetime peak footprint")
lib.wtrace_set_frame_ring(1 << 20)

# 5. Fork safety: fork (via subprocess, close_fds) while a snapshot holds the mutex.
fork_out = os.path.join(scratch, "fork.jsonl")
if os.path.exists(fork_out):
    os.unlink(fork_out)
dumper = threading.Thread(target=lib.wtrace_dump_to, args=(fork_out.encode(),))
dumper.start()
for _ in range(2000):
    if lib.wtrace_in_dump():
        break
    threading.Event().wait(0.001)
else:
    fail.append("fork: snapshot never entered its window (WTRACE_TEST_PAUSE_US unset?)")
spawned = {}


def spawn():
    child = subprocess.Popen([sys.executable, "-c", "pass"], close_fds=True)
    spawned["rc"] = child.wait()


forker = threading.Thread(target=spawn, daemon=True)
forker.start()
dumper.join()
forker.join(10)
if forker.is_alive():
    fail.append("fork: a child forked during a snapshot never exec'd (trace mutexes not fork-safe)")
    subprocess.run(["pkill", "-9", "-P", str(os.getpid())])
elif spawned.get("rc") != 0:
    fail.append(f"fork: the child exited {spawned.get('rc')}")

print(f"selftest: race {got}/{len(payload)} bytes; stress {final}/{sum(written)} bytes over {len(snaps)} snapshots; "
      f"frames {len(actual)} logged; full ring dropped {snap.get('framesDropped')}")
for f in fail:
    print("selftest: FAIL", f)
print("selftest:", "PASS" if not fail else "FAIL")
sys.exit(1 if fail else 0)
