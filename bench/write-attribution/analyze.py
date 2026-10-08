"""#901 probe analyzer: per-path write bytes over a steady-state window, per tick.

usage: analyze.py RUN_DIR [--origin EPOCH] [--skip SECONDS] [--until SECONDS]

--skip and --until are offsets from --origin (Amendment 19 HR-21: the
verdicts pass the run's `window.json` traceStart, the instant their own
offsets are measured from); without --origin the origin is the dashboard's
first interposer snapshot minus 5 s, as before.

Fails closed (#901 SR-003/SR-004): exit 2 with `INVALID: <reason>` when the evidence
cannot support a number -- a missing trace, a snapshot that dropped bytes, a path whose
cumulative counter decreased or vanished, a failed rusage sample at a window endpoint,
a decreasing process counter, or valid rusage coverage below 90% of the window. A
missing or failed self-test receipt (`selftest.txt` in RUN_DIR ending in
`selftest: PASS`) is also invalid, and so is a run whose input-mode receipt
(`inputs.json`, spec §6.3 revision 15) is missing or does not hold: a frozen
run needs the namespace self-test, the freeze verified before and after with
one seal, and complete, enforced activation receipts for its whole family.
Valid runs exit 0 and name their evidence (frozen and its seal, or live).
"""
import glob, json, os, re, sys, collections

run = sys.argv[1]
skip = float(sys.argv[sys.argv.index("--skip") + 1]) if "--skip" in sys.argv else 240.0
until = float(sys.argv[sys.argv.index("--until") + 1]) if "--until" in sys.argv else 1e18
origin = float(sys.argv[sys.argv.index("--origin") + 1]) if "--origin" in sys.argv else None
RUSAGE_PERIOD_S = 15.0


def invalid(reason):
    print(f"INVALID: {reason}")
    sys.exit(2)


st = os.path.join(run, "selftest.txt")
if not os.path.exists(st) or not open(st).read().rstrip().endswith("selftest: PASS"):
    invalid("no passing interposer self-test receipt (selftest.txt)")

# Revision 15 (Q16): the input mode and, for a frozen run, the namespace
# self-test, the freeze's seal before and after, and complete enforced
# activation receipts for every process of the family (frozen_roots.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frozen_roots  # noqa: E402

input_problems = frozen_roots.check_inputs(run)
if input_problems:
    invalid("inputs: " + "; ".join(input_problems[:5]))

pid = int(open(os.path.join(run, "pid")).read())
traces = {}
for f in glob.glob(os.path.join(run, "wtrace.*")):
    suffix = f.rsplit(".", 1)[1]
    if not suffix.isdigit():
        continue
    traces[int(suffix)] = [json.loads(l) for l in open(f) if l.strip()]
if pid not in traces or not traces[pid]:
    invalid(f"no interposer trace for the dashboard pid {pid}")
main = traces[pid]
prev = {}
for n, snap in enumerate(main):
    if snap.get("dropped", 0):
        invalid(f"snapshot {n} dropped {snap['dropped']} bytes (path table full)")
    for k, prior in prev.items():
        if k not in snap["paths"]:
            invalid(f"snapshot {n} lost path {k}")
        if snap["paths"][k][0] < prior:
            invalid(f"snapshot {n} decreased {k}")
    prev = {k: v[0] for k, v in snap["paths"].items()}
t0 = main[0]["t"] - 5 if origin is None else origin
lo = [r for r in main if r["t"] - t0 >= skip]
hi = [r for r in main if r["t"] - t0 <= until]
if not lo or not hi or lo[0]["t"] >= hi[-1]["t"]:
    invalid("no snapshots inside the requested window")
a, b = lo[0], hi[-1]
span = b["t"] - a["t"]


def norm(path):
    path = re.sub(r"^.*/store-[a-z]+/", "<store>/", path)
    path = re.sub(r"^/Users/[^/]+/", "~/", path)
    return path


delta = collections.Counter(); calls = collections.Counter()
for k, (by, ca) in b["paths"].items():
    pa = a["paths"].get(k, [0, 0])
    if by - pa[0] > 0:
        delta[norm(k)] += by - pa[0]; calls[norm(k)] += ca - pa[1]
tot = sum(delta.values())

# ticks in window from perf snapshots (cumulative dispatch counts)
perfs = []
for f in sorted(glob.glob(os.path.join(run, "perf-*.json"))):
    try:
        d = json.load(open(f))["diagnostic"]
        if not d or not d.get("tick"):
            continue
    except Exception:
        continue
    perfs.append((os.path.getmtime(f), d["tick"]["dispatch_counts"], d))


def counts_at(t):
    best = None
    for mt, c, _ in perfs:
        if mt <= t:
            best = c
    return best or {"idle": 0, "full": 0, "degraded": 0}


ca_, cb_ = counts_at(a["t"]), counts_at(b["t"])
full = cb_["full"] - ca_["full"]; idle = cb_["idle"] - ca_["idle"]

# rusage window: endpoints and coverage must rest on successful reads
ru_all = [json.loads(l) for l in open(os.path.join(run, "rusage.jsonl"))]
ru = [r for r in ru_all if r.get("pid") == pid]
inwin = [r for r in ru if a["t"] - RUSAGE_PERIOD_S <= r["t"] <= b["t"] + RUSAGE_PERIOD_S]
valid = [r for r in inwin if r.get("rc") == 0 and r.get("dwrite") is not None and r.get("footprint_peak") is not None]
if len(valid) < 2:
    invalid("fewer than two successful rusage samples in the window")
ra = min(valid, key=lambda r: abs(r["t"] - a["t"])); rb = min(valid, key=lambda r: abs(r["t"] - b["t"]))
for end, r in (("start", ra), ("end", rb)):
    if abs(r["t"] - (a if end == "start" else b)["t"]) > RUSAGE_PERIOD_S:
        invalid(f"no successful rusage sample within {RUSAGE_PERIOD_S:.0f}s of the window {end}")
seq = sorted(valid, key=lambda r: r["t"])
for x, y in zip(seq, seq[1:]):
    if y["dwrite"] < x["dwrite"]:
        invalid("process write counter decreased (reset or wrong pid)")
covered = sum(min(y["t"] - x["t"], 2 * RUSAGE_PERIOD_S) for x, y in zip(seq, seq[1:]))
if covered < 0.9 * (rb["t"] - ra["t"]) or (rb["t"] - ra["t"]) < 0.9 * span:
    invalid(f"successful rusage samples cover {covered:.0f}s of a {span:.0f}s window")
rdw = rb["dwrite"] - ra["dwrite"]; rspan = rb["t"] - ra["t"]
peak_res = max(r["resident"] for r in valid)
# footprint_peak is the kernel's lifetime high-water mark, so it includes transients between
# samples; the window raised it only if the end value exceeds the start value.
peak_fp = rb["footprint_peak"]; peak_fp_before = ra["footprint_peak"]

label = frozen_roots.evidence_label(run)
print(f"inputs: {label['mode']}" + (f" (freeze seal {label['freeze']})" if label["freeze"] else ""))
print(f"window {a['t']-t0:.0f}s..{b['t']-t0:.0f}s ({span:.0f}s); ticks full={full} idle={idle}")
print(f"interposer total {tot/1e6:.1f} MB = {tot/1e6/span*60:.0f} MB/min; per full tick {tot/1e6/max(full,1):.1f} MB")
print(f"rusage dwrite {rdw/1e6:.1f} MB over {rspan:.0f}s = {rdw/1e6/max(rspan,1)*60:.0f} MB/min; "
      f"{len(valid)} valid of {len(inwin)} samples")
print(f"sampled peak resident {peak_res/2**20:.0f} MiB; lifetime peak footprint {peak_fp/2**20:.0f} MiB "
      f"({'raised in this window' if peak_fp > peak_fp_before else 'set before this window'}; {peak_fp_before/2**20:.0f} MiB at its start)")
for k, v in delta.most_common(25):
    print(f"  {v/1e6:10.2f} MB {calls[k]:8d} calls  {v/1e6/max(full,1):8.2f} MB/full-tick  {k}")
for p, rows in traces.items():
    # A child that exited before its first snapshot left an empty trace (Task X H-1).
    if p != pid and rows:
        last = rows[-1]["paths"]
        print(f"child pid {p}: {sum(v[0] for v in last.values())/1e6:.1f} MB lifetime", [norm(k) for k in sorted(last, key=lambda k: -last[k][0])[:5]])
