#!/usr/bin/env python3
"""#778 cost-measurement campaign driver (session artifact, not shipped).

Measures what `cached_statements=0` costs on three workloads, comparing the
FIXED arm (the production constant) against a BASELINE arm that restores the
pre-#778 construction by emptying `_cctally_store._STATS_CONNECT_KWARGS`.

Every measured run happens in a FRESH subprocess over its OWN copied store, so
no arm inherits a warmed page cache from the other and no fixture build lands
inside a measured region. Rounds alternate ABBA, so a monotone drift in machine
load contributes equally to both arms.

Run remotely, pinned to one runner:

    CCTALLY_REMOTE_HOST=<runner-alias> bin/cctally-test-remote \\
        python3 bench/measure-778-statement-cache.py campaign --rounds 12 --workdir /tmp/bench778

Results are printed to stdout as JSON, because the remote wrapper copies no
files back.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import pathlib
import resource
import shutil
import statistics
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
ARMS = ("fixed", "baseline")
WORKLOADS = ("ingest", "rebuild", "dashboard", "aa")


# ---------------------------------------------------------------------------
# child side
# ---------------------------------------------------------------------------


def _load_cli():
    """Bind `sys.modules["cctally"]`, which `_cctally_core._cctally()` reads."""
    bin_dir = str(REPO / "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    if "cctally" not in sys.modules:
        import importlib.util
        from importlib.machinery import SourceFileLoader

        loader = SourceFileLoader("cctally", str(REPO / "bin" / "cctally"))
        spec = importlib.util.spec_from_loader("cctally", loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["cctally"] = module
        loader.exec_module(module)
    return sys.modules["cctally"]


def _apply_arm(arm: str):
    """Bind the store's connect kwargs to the arm under test.

    Importing `_cctally_store` first and mutating the constant is the whole
    mechanism: the opener reads it at call time, so every guarded connection
    built afterwards in this process carries (or lacks) `cached_statements=0`.
    Nothing else differs between the arms.
    """
    _load_cli()
    import _cctally_store as store

    if arm == "baseline":
        store._STATS_CONNECT_KWARGS = {}
    elif arm != "fixed":
        raise SystemExit(f"unknown arm {arm!r}")
    return store


def _cpu_seconds() -> float:
    me = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (me.ru_utime + me.ru_stime + kids.ru_utime + kids.ru_stime)


class _Timer:
    def __enter__(self):
        self.wall0 = time.perf_counter()
        self.cpu0 = _cpu_seconds()
        return self

    def __exit__(self, *exc):
        self.wall = time.perf_counter() - self.wall0
        self.cpu = _cpu_seconds() - self.cpu0
        return False


def _point_env(store_dir: pathlib.Path):
    os.environ["HOME"] = str(store_dir / "home")
    os.environ["CCTALLY_DATA_DIR"] = str(store_dir / "share")
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    os.environ["CCTALLY_DISABLE_TELEMETRY"] = "1"
    os.environ.setdefault("TZ", "Etc/UTC")
    os.environ.pop("CLAUDE_CONFIG_DIR", None)


def child_ingest(arm: str, store_dir: pathlib.Path) -> dict:
    """The real stats-ingest hot path, driven through `cctally record-usage`.

    Each tick appends one raw observation and runs one single-flight ingest
    cycle, which is where every guarded stats connection and every stats DML
    statement in steady state actually lives. The percent ladder crosses 96
    integer weekly milestones, the five-hour axis cycles so blocks
    close and five-hour milestones cross, and one mid-run weekly reset to zero
    exercises the reset-and-credit path. The tail repeats the final reading, so
    the caught-up cycles that dominate production are measured too.
    """
    _point_env(store_dir)
    cli = _load_cli()
    _apply_arm(arm)

    week_a = 1788584400
    week_b = week_a + 7 * 24 * 3600
    fh_base = 1788560400
    ticks = 160
    calls = 0

    with _Timer() as t:
        for i in range(ticks):
            # Percent climbs, resets to zero at the halfway mark under a new
            # weekly anchor, then climbs again.
            if i < ticks // 2:
                week, pct = week_a, round(0.6 * (i + 1), 1)
            else:
                week, pct = week_b, round(0.6 * (i - ticks // 2 + 1), 1)
            block = i // 12
            cli.main([
                "record-usage",
                "--percent", str(pct),
                "--resets-at", str(week),
                "--five-hour-percent", str(round((i % 12) * 8.0, 1)),
                "--five-hour-resets-at", str(fh_base + block * 5 * 3600),
            ])
            calls += 1
        # Caught-up cycles: the same reading again, which is what a status-line
        # tick does on an idle machine.
        for _ in range(10):
            cli.main([
                "record-usage",
                "--percent", str(round(0.6 * (ticks - ticks // 2), 1)),
                "--resets-at", str(week_b),
                "--five-hour-percent", "88.0",
                "--five-hour-resets-at", str(fh_base + ((ticks - 1) // 12) * 5 * 3600),
            ])
            calls += 1
    return {"wall": t.wall, "cpu": t.cpu, "calls": calls}


def child_rebuild(arm: str, store_dir: pathlib.Path) -> dict:
    """One `rebuild_stats_index` plus its in-place publication.

    The journal is already built and the destination already exists, so the
    measured region is the rebuild and the publish, not the fixture.
    """
    _point_env(store_dir)
    store = _apply_arm(arm)
    import _cctally_core
    import _cctally_journal as jr

    dest = pathlib.Path(os.environ["CCTALLY_DATA_DIR"]) / "rebuilt.db"
    _cctally_core.open_db(_target_path=str(dest)).close()
    high_water = jr.journal_high_water()

    with _Timer() as t:
        with store.stats_write_scope("maintenance-rebuild"):
            result = jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="test-fixture"),
                target_path=str(dest),
                high_water=high_water,
                update_quota_cache=True,
            )
    return {
        "wall": t.wall, "cpu": t.cpu,
        "lines_folded": result.lines_folded,
        "segments_read": result.segments_read,
    }


def child_dashboard(arm: str, store_dir: pathlib.Path) -> dict:
    """The backend bench's snapshot spine: cold, warm-invalidated and idle.

    The reported numbers are the bench's OWN per-body medians rather than this
    process's wall clock, so corpus construction and fixture reuse stay outside
    the measurement exactly as `bin/cctally-bench` intends.
    """
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    os.environ["CCTALLY_DISABLE_TELEMETRY"] = "1"
    os.environ.setdefault("TZ", "Etc/UTC")
    _apply_arm(arm)
    import runpy

    root = store_dir / "bench-root"
    argv = sys.argv[:]
    sys.argv = [
        "cctally-bench", "--scale", "large", "--json",
        "--iterations", "5", "--root", str(root),
    ]
    buffer = io.StringIO()
    with _Timer() as t:
        try:
            with contextlib.redirect_stdout(buffer):
                runpy.run_path(str(REPO / "bin" / "cctally-bench"),
                               run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (0, None):
                sys.argv = argv
                raise
    sys.argv = argv
    payload = json.loads(buffer.getvalue())
    benches = payload["benchmarks"]
    families = {
        name: benches[name]["median_ms"]
        for name in benches
        if name.startswith("snapshot")
    }
    return {
        "wall": t.wall, "cpu": t.cpu,
        "families": families,
        "snapshot_total_ms": sum(families.values()),
        # The named workload is the snapshot spine, not the whole bench run.
        # `wall` also covers the other thirteen benchmarks and the fixture
        # verification, so the budget is applied to this.
        "snapshot_total_s": sum(families.values()) / 1000.0,
    }


def child_aa(arm: str, store_dir: pathlib.Path) -> dict:
    """The A/A control: the ingest workload with the arm label IGNORED.

    Both labels run the production constant, so any separation this control
    reports is measurement noise rather than the mechanism. Without it a small
    real difference cannot be told from a small procedural one.
    """
    return child_ingest("fixed", store_dir)


CHILDREN = {
    "ingest": child_ingest,
    "rebuild": child_rebuild,
    "dashboard": child_dashboard,
    "aa": child_aa,
}


# ---------------------------------------------------------------------------
# parent side
# ---------------------------------------------------------------------------


def build_master(kind: str, master: pathlib.Path, lines: int) -> dict:
    """Build one fixture ONCE, outside every measured region."""
    if master.exists():
        shutil.rmtree(master)
    (master / "home").mkdir(parents=True)
    (master / "share").mkdir(parents=True)
    env = dict(os.environ)
    env.update({
        "HOME": str(master / "home"),
        "CCTALLY_DATA_DIR": str(master / "share"),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "TZ": "Etc/UTC",
    })
    env.pop("CLAUDE_CONFIG_DIR", None)
    argv = ([sys.executable, str(pathlib.Path(__file__).resolve()), "build", "--kind", kind]
            + (["--lines", str(lines)] if kind == "rebuild" else []))
    out = subprocess.run(
        argv, cwd=str(REPO), env=env, capture_output=True, text=True,
        timeout=3600,
    )
    if out.returncode != 0:
        raise SystemExit(
            f"fixture build for {kind} failed:\n{out.stdout}\n{out.stderr}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def cmd_build(args) -> int:
    """Initialize one master store. Never inside a measured region."""
    if args.kind == "ingest":
        cli = _load_cli()
        cli.open_db().close()
        print(json.dumps({"kind": "ingest", "initialized": True}))
        return 0

    _load_cli()
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_journal_benchmark_fixture",
        str(REPO / "bin" / "build-journal-benchmark-fixture.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._load_cctally()
    shape = module.build(target_lines=args.lines, seed_cache=True)
    print(json.dumps({"kind": "rebuild",
                      "total_lines": shape["total_lines"],
                      "total_bytes": shape["total_bytes"]}))
    return 0


def run_child(workload: str, arm: str, master: pathlib.Path,
              scratch: pathlib.Path, timeout: float) -> dict:
    """One measured run in a fresh process over its OWN copy of the store."""
    if scratch.exists():
        shutil.rmtree(scratch)
    shutil.copytree(master, scratch)
    out = subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).resolve()), "child",
         "--workload", workload, "--arm", arm, "--store", str(scratch)],
        cwd=str(REPO), capture_output=True, text=True, timeout=timeout,
    )
    shutil.rmtree(scratch, ignore_errors=True)
    if out.returncode != 0:
        raise SystemExit(
            f"{workload}/{arm} child failed (exit {out.returncode})\n"
            f"{out.stdout}\n{out.stderr}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def warm_master(workload: str, master: pathlib.Path, timeout: float) -> None:
    """Build a workload's reusable fixture INSIDE the master, once.

    `run_child` deliberately copies the master and destroys the copy, so it
    cannot be used to warm anything. Anything a child caches under the store
    it is given survives here and is then copied into every measured scratch.
    """
    out = subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).resolve()), "child",
         "--workload", workload, "--arm", "fixed", "--store", str(master)],
        cwd=str(REPO), capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0:
        raise SystemExit(
            f"{workload} warm-up failed (exit {out.returncode})\n"
            f"{out.stdout}\n{out.stderr}")


def cmd_child(args) -> int:
    result = CHILDREN[args.workload](args.arm, pathlib.Path(args.store))
    print(json.dumps(result))
    return 0


def _ci95(values):
    """Student-t 95% interval on the mean, with the t table inlined.

    stdlib only, and the sample sizes here are small enough that the normal
    approximation would overstate the precision.

    The table is keyed by SAMPLE SIZE, not by degrees of freedom: key 2 holds
    t(0.975, df=1). Looking it up by `n - 1` reads one row too tight at every
    size and misses the table entirely at n = 2, where the 1.984 fallback is
    six times narrower than the 12.706 the row actually holds.
    """
    n = len(values)
    if n < 2:
        return (float("nan"), float("nan"))
    t = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447,
         8: 2.365, 9: 2.306, 10: 2.262, 11: 2.228, 12: 2.201, 13: 2.179,
         14: 2.160, 15: 2.145, 16: 2.131, 17: 2.120, 18: 2.110, 19: 2.101,
         20: 2.093}.get(n, 1.984)
    mean = statistics.fmean(values)
    half = t * statistics.stdev(values) / math.sqrt(n)
    return (mean - half, mean + half)


def _percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def summarize(workload: str, pairs, key: str) -> dict:
    """Paired statistics. The pairing is what removes between-round drift."""
    fixed = [p["fixed"][key] for p in pairs]
    base = [p["baseline"][key] for p in pairs]
    deltas = [f - b for f, b in zip(fixed, base)]
    base_median = statistics.median(base)
    lo, hi = _ci95(deltas)
    return {
        "workload": workload,
        "metric": key,
        "n_pairs": len(pairs),
        "baseline_median": base_median,
        "fixed_median": statistics.median(fixed),
        "baseline_p90": _percentile(base, 0.9),
        "fixed_p90": _percentile(fixed, 0.9),
        "baseline_stdev": statistics.stdev(base) if len(base) > 1 else 0.0,
        "fixed_stdev": statistics.stdev(fixed) if len(fixed) > 1 else 0.0,
        "delta_median": statistics.median(deltas),
        "delta_mean": statistics.fmean(deltas),
        "delta_ci95_lo": lo,
        "delta_ci95_hi": hi,
        "delta_pct_median": (
            100.0 * statistics.median(deltas) / base_median
            if base_median else float("nan")),
        "delta_pct_ci95_lo": 100.0 * lo / base_median if base_median else float("nan"),
        "delta_pct_ci95_hi": 100.0 * hi / base_median if base_median else float("nan"),
    }


def verdict(summary: dict, *, pct_budget=15.0, floor_ms=15.0) -> dict:
    """Apply D6: 15% or 15 ms, whichever is the more generous.

    A breach must be STATISTICALLY ESTABLISHED, so the whole confidence
    interval has to sit beyond the budget. An interval that spans the budget is
    inconclusive and forces a rerun rather than passing.
    """
    base = summary["baseline_median"]
    budget_ms = max(floor_ms / 1000.0, base * pct_budget / 100.0)
    lo, hi = summary["delta_ci95_lo"], summary["delta_ci95_hi"]
    if hi <= budget_ms:
        state = "within budget"
    elif lo > budget_ms:
        state = "BREACH"
    else:
        state = "inconclusive"
    return {
        "budget_seconds": budget_ms,
        "budget_basis": ("15% of baseline median"
                         if base * pct_budget / 100.0 >= floor_ms / 1000.0
                         else "15 ms absolute floor"),
        "delta_ci95": [lo, hi],
        "state": state,
    }


def cmd_campaign(args) -> int:
    work = pathlib.Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    report: dict = {"rounds": args.rounds, "fixtures": {}, "raw": {},
                    "summaries": [], "verdicts": []}

    masters = {}
    if "ingest" in args.workloads or "aa" in args.workloads:
        masters["ingest"] = work / "master-ingest"
        report["fixtures"]["ingest"] = build_master(
            "ingest", masters["ingest"], 0)
        masters["aa"] = masters["ingest"]
    if "rebuild" in args.workloads:
        masters["rebuild"] = work / "master-rebuild"
        report["fixtures"]["rebuild"] = build_master(
            "rebuild", masters["rebuild"], args.rebuild_lines)
    if "dashboard" in args.workloads:
        masters["dashboard"] = work / "master-dashboard"
        if masters["dashboard"].exists():
            shutil.rmtree(masters["dashboard"])
        (masters["dashboard"] / "home").mkdir(parents=True)
        (masters["dashboard"] / "share").mkdir(parents=True)
        (masters["dashboard"] / "bench-root").mkdir(parents=True)
        # Warm the bench corpus once, outside the measured rounds. This must
        # run against the MASTER: `run_child` builds in a scratch copy and
        # deletes it, which left the master empty and put a full `--scale
        # large` corpus build inside every measured run's timer.
        warm_master("dashboard", masters["dashboard"], args.timeout)
        report["fixtures"]["dashboard"] = {"warmed": True}

    for workload in args.workloads:
        rounds = args.rebuild_rounds if workload == "rebuild" else args.rounds
        pairs = []
        for i in range(rounds):
            # ABBA: the arm order flips every round, so a monotone drift in
            # machine load lands on both arms equally.
            order = ARMS if i % 2 == 0 else tuple(reversed(ARMS))
            got = {}
            for arm in order:
                got[arm] = run_child(
                    workload, arm, masters[workload],
                    work / f"run-{workload}-{i}-{arm}", args.timeout)
            pairs.append(got)
            print(f"[{workload}] round {i + 1}/{rounds} "
                  f"fixed={got['fixed']['wall']:.4f}s "
                  f"baseline={got['baseline']['wall']:.4f}s",
                  file=sys.stderr, flush=True)
        report["raw"][workload] = pairs
        budget_metric = ("snapshot_total_s" if workload == "dashboard"
                         else "wall")
        metrics = ("wall", "cpu")
        if workload == "dashboard":
            metrics = ("wall", "cpu", "snapshot_total_s")
        for metric in metrics:
            summary = summarize(workload, pairs, metric)
            report["summaries"].append(summary)
            if metric == budget_metric:
                report["verdicts"].append(
                    {"workload": workload, "metric": metric,
                     **verdict(summary)})

    print(json.dumps(report, indent=2, default=str))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--kind", choices=("ingest", "rebuild"), default="rebuild")
    b.add_argument("--lines", type=int, default=40_000)
    b.set_defaults(func=cmd_build)

    c = sub.add_parser("child")
    c.add_argument("--workload", choices=WORKLOADS, required=True)
    c.add_argument("--arm", choices=ARMS, required=True)
    c.add_argument("--store", required=True)
    c.set_defaults(func=cmd_child)

    r = sub.add_parser("campaign")
    r.add_argument("--rounds", type=int, default=12)
    r.add_argument("--rebuild-rounds", type=int, default=4)
    r.add_argument("--rebuild-lines", type=int, default=1_000_000)
    r.add_argument("--workdir", default="/tmp/bench778")
    r.add_argument("--timeout", type=float, default=3600.0)
    r.add_argument("--workloads", nargs="+", default=list(WORKLOADS))
    r.set_defaults(func=cmd_campaign)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
