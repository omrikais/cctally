"""Self-test for analyze.py (#901 SR-004): failed, reset and missing samples invalidate a window;
revision 15 (Q16): so do a missing input-mode receipt and, for a frozen run, absent sandbox
enforcement, a missing child activation receipt, a changed freeze or mismatched identities;
Amendment 19 (HR-3): and a family receipt with no evidence of how its process ended.

usage: analyze_selftest.py SCRATCH_DIR   -> exit 0 and `analyze-selftest: PASS`, else exit 1.
Builds synthetic run directories and checks analyze.py's verdict on each.
"""
import json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
root = sys.argv[1]
PID = 4242


SEAL = {"freeze": "/f", "sealSha256": "s" * 64, "manifestSha256": "m" * 64,
        "rootmapSha256": "r" * 64, "profileSha256": "p" * 64}
LIB = "/x/rootmap.dylib"


def frozen_inputs(**over):
    """A synthetic frozen-mode inputs.json (spec §6.3 revision 15) and the
    activation receipts of its family; `over` breaks one property."""
    inputs = {"schema": "write-attribution-inputs/1", "mode": "frozen",
              "runtime": {"python": "/opt/homebrew/bin/python3",
                          "version": "3.14.7"},
              "libraries": {"wtrace": {"path": "/x/wtrace.dylib",
                                       "sha256": "w" * 64},
                            "rootmap": {"path": LIB, "sha256": "l" * 64}},
              "freeze": dict(SEAL),
              "frozenSelftest": {"passed": True, "rootmapSha256": "l" * 64,
                                 "wtraceSha256": "w" * 64, "python": "3.14.7"},
              "verifyBefore": {"valid": True, "seal": dict(SEAL)},
              "verifyAfter": {"valid": True, "seal": dict(SEAL)}}
    activation = {"event": "activate", "schema": "rootmap/1", "image": "load",
                  "pid": PID, "ok": True, "manifestSha256": "r" * 64,
                  "library": LIB,
                  "enforced": [{"root": "/h/.codex", "denied": True}]}
    extra = []
    if over.get("unenforced"):
        activation["enforced"] = [{"root": "/h/.codex", "denied": False}]
    if over.get("lost_child"):
        extra.append({"event": "exec", "pid": PID,
                      "target": "/opt/homebrew/bin/python3"})
    if over.get("changed_freeze"):
        inputs["verifyAfter"]["seal"] = dict(SEAL, sealSha256="t" * 64)
    if over.get("mismatched"):
        inputs["frozenSelftest"]["rootmapSha256"] = "z" * 64
    if not over.get("unterminated") and not over.get("lost_child"):
        # HR-3: how the process ended - its own exit line
        extra.append({"event": "exit", "pid": PID, "how": "exit",
                      "counters": {"eventsDropped": 0}})
    return inputs, [activation] + extra


def make(name, rusage, traces=None, selftest="selftest: PASS\n",
         inputs=({"schema": "write-attribution-inputs/1", "mode": "live"}, None)):
    d = os.path.join(root, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    open(os.path.join(d, "pid"), "w").write(str(PID))
    if selftest is not None:
        open(os.path.join(d, "selftest.txt"), "w").write(selftest)
    if inputs is not None:
        receipt, lines = inputs
        open(os.path.join(d, "inputs.json"), "w").write(json.dumps(receipt))
        if lines is not None:
            os.makedirs(os.path.join(d, "rootmap"))
            with open(os.path.join(d, "rootmap", f"{PID}.1.2.jsonl"), "w") as fh:
                for line in lines:
                    fh.write(json.dumps(line) + "\n")
    if traces is None:
        traces = [{"t": 1000.0 + 5 * i, "pid": PID, "dropped": 0,
                   "paths": {"/s/cache.db": [1000 * i, i]}} for i in range(0, 80)]
    with open(os.path.join(d, f"wtrace.{PID}"), "w") as fh:
        for r in traces:
            fh.write(json.dumps(r) + "\n")
    with open(os.path.join(d, "rusage.jsonl"), "w") as fh:
        for r in rusage:
            fh.write(json.dumps(r) + "\n")
    return d


def ok_sample(t, w):
    return {"t": t, "pid": PID, "rc": 0, "errno": 0, "dwrite": w, "dread": 0,
            "resident": 1 << 20, "footprint": 1 << 20, "footprint_peak": 1 << 21, "logical_writes": 0, "cpu_abs": 0}


def bad_sample(t):
    return {"t": t, "pid": PID, "rc": -1, "errno": 3, "dwrite": None, "dread": None,
            "resident": None, "footprint": None, "footprint_peak": None, "logical_writes": None, "cpu_abs": None}


good = [ok_sample(1000.0 + 15 * i, 10000 * i) for i in range(0, 27)]
cases = {
    "valid": (make("valid", good), 0),
    "failed-endpoints": (make("failed-endpoints", [bad_sample(s["t"]) if i in (0, 1, 2, 24, 25, 26) else s
                                                   for i, s in enumerate(good)]), 2),
    "all-failed": (make("all-failed", [bad_sample(s["t"]) for s in good]), 2),
    "counter-reset": (make("counter-reset", good[:13] + [ok_sample(s["t"], s["dwrite"] - 200000) for s in good[13:]]), 2),
    "sparse-coverage": (make("sparse-coverage", [s if i in (0, 13, 26) else bad_sample(s["t"]) for i, s in enumerate(good)]), 2),
    "no-selftest": (make("no-selftest", good, selftest=None), 2),
    "failed-selftest": (make("failed-selftest", good, selftest="selftest: FAIL\n"), 2),
    "trace-decrease": (make("trace-decrease", good, traces=[
        {"t": 1000.0 + 5 * i, "pid": PID, "dropped": 0, "paths": {"/s/cache.db": [1000 * i if i != 60 else 5, i]}}
        for i in range(0, 80)]), 2),
    "trace-dropped": (make("trace-dropped", good, traces=[
        {"t": 1000.0 + 5 * i, "pid": PID, "dropped": 7 if i == 70 else 0, "paths": {"/s/cache.db": [1000 * i, i]}}
        for i in range(0, 80)]), 2),
    # revision 15 (Q16): the input mode and the frozen family's receipts
    "no-inputs": (make("no-inputs", good, inputs=None), 2),
    "frozen-valid": (make("frozen-valid", good, inputs=frozen_inputs()), 0),
    "frozen-absent-enforcement": (make("frozen-absent-enforcement", good,
                                       inputs=frozen_inputs(unenforced=True)), 2),
    "frozen-missing-child-receipt": (make("frozen-missing-child-receipt", good,
                                          inputs=frozen_inputs(lost_child=True)), 2),
    "frozen-changed-freeze": (make("frozen-changed-freeze", good,
                                   inputs=frozen_inputs(changed_freeze=True)), 2),
    "frozen-mismatched-identities": (make("frozen-mismatched-identities", good,
                                          inputs=frozen_inputs(mismatched=True)), 2),
    # Amendment 19 HR-3: a receipt whose process's end is nowhere on record
    "frozen-unterminated": (make("frozen-unterminated", good,
                                 inputs=frozen_inputs(unterminated=True)), 2),
}
fail = []
for name, (d, want) in cases.items():
    r = subprocess.run([sys.executable, os.path.join(HERE, "analyze.py"), d, "--skip", "10"],
                       capture_output=True, text=True)
    if r.returncode != want:
        fail.append(f"{name}: exit {r.returncode}, want {want}: {(r.stdout + r.stderr).strip()[:300]}")
for f in fail:
    print("analyze-selftest: FAIL", f)
print("analyze-selftest:", "PASS" if not fail else "FAIL", f"({len(cases)} cases)")
sys.exit(1 if fail else 0)
