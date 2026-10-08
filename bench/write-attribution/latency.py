"""#901 spec §6.3: latency of each W1–W3 and PA-001 call site, and of the
sync and ingest passes (revision 9), baseline vs candidate.

usage: latency.py --tree TREE --out OUT.json [--calls 5] [--source SRC_ROOT]
       latency.py --tree TREE --out OUT.json --sync-passes N --root CLONE_ROOT
                  [--source SRC_ROOT]
       latency.py pair CANDIDATE_RUN BASELINE_RUN
  (run with CCTALLY_DATA_DIR / HOME / CODEX_HOME pointed at a CLONE of a
   backup-API copy, and TMPDIR on the external drive: `run-latency.sh` does
   this, as a family process of the run, Amendment 12)

Each site is the PRODUCT function that executes the statement and consumes
its whole result, timed with `perf_counter` around exactly that call and
nothing else. One untimed warm-up call per site runs first; on the candidate
it also applies any pending migration of that tree to the clone (a write to
the clone, never to a live store). Five timed calls follow; the median and
maximum are reported. `sqlattr.py`'s start-to-next-start intervals are
attribution aids, never latency evidence.

`--sync-passes N` (revision 9 latency evidence): times complete
conversation-sync (Claude and Codex), Claude cache-sync, Codex cache-sync and
stats-ingest calls directly - each product call wrapped with `perf_counter`
around exactly that call, its consumption and commit included - over three
labelled populations: the first `cache-sync --source all` pass (catch-up), N
no-change passes, and N small-delta passes, each after ~2 KiB appended to the
clone's scratch roots (`--root`; B/D's seeded scratch, never a real root). Per
call kind and population it reports the sample count, input bytes and rows,
p50, p95 and maximum; the matched baseline/candidate comparison applies the
1.2x ingest factor to p50/p95.

Input mode (spec §6.3 revision 15, Q16): under WRITE_ATTRIBUTION_INPUTS=
frozen:FREEZE the process refuses (exit 2) unless that freeze's namespace is
loaded in it (`frozen_roots.require_namespace`), and unless --source names the
store copy the clone came from with a qualification receipt
(SRC_ROOT/frozen-qualification.json) naming this freeze and this copy: the
first sync pass is a frozen catch-up (README "Frozen inputs" step 2). The
receipt records {mode, freeze seal, source} under `inputs`.

`pair CANDIDATE_RUN BASELINE_RUN` is the recipe's comparison of two
`run-latency.sh` runs (RUN/latency.json beside RUN/inputs.json). INVALID
(exit 2) when either run lacks valid input evidence
(`frozen_roots.check_inputs`), the two carry mixed input evidence (two
freezes, or frozen against live, as `workload.py b-pair`), a receipt is
missing, or the receipts are not matched (sites against sync passes, other
call or pass counts, a site or a population present on one side only). FAIL
(exit 1) when a W1-W3 site's candidate median exceeds the baseline median,
or a matched sync population's candidate p50 or p95 exceeds 1.2 x the
baseline's (spec §6.3). The PA-001 sites are measured and reported with
their ratio; the spec states no limit for them. Otherwise PASS (exit 0).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.machinery
import importlib.util
import json
import pathlib
import statistics
import subprocess
import sys
import os
import time

UTC = dt.timezone.utc
HERE = pathlib.Path(__file__).resolve().parent


def _frozen_roots():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import frozen_roots
    return frozen_roots


def load_tree(tree: pathlib.Path):
    """Load TREE/bin/cctally as `cctally`, exactly as the product runs."""
    sys.path.insert(0, str(tree / "bin"))
    loader = importlib.machinery.SourceFileLoader(
        "cctally", str(tree / "bin" / "cctally"))
    spec = importlib.util.spec_from_loader("cctally", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = module
    loader.exec_module(module)
    return module


def summarize(samples):
    return {"samplesS": [round(s, 6) for s in samples],
            "medianS": round(statistics.median(samples), 6),
            "maxS": round(max(samples), 6)}


def call_sites(c, now):
    """(name, module, function, args, thunk) for every W1–W3 call site."""
    quota = c._load_sibling("_cctally_quota")
    doctor = c._load_sibling("_cctally_doctor")
    sources = c._load_sibling("_cctally_dashboard_sources")
    lib_sources = c._load_sibling("_lib_dashboard_sources")
    forecast = c._load_sibling("_cctally_forecast")
    pricing = c._load_sibling("_cctally_pricing_check")
    conv_query = c._load_sibling("_lib_conversation_query")
    cache = c.open_cache_db()
    stats = c.open_db()
    conv = c.open_conversations_db(attach_cache=False)
    long_sessions = [row[0] for row in conv.execute(
        "SELECT session_id FROM conversation_messages WHERE session_id IS "
        "NOT NULL GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 20")]
    roots = sorted({row[0] for row in cache.execute(
        "SELECT DISTINCT source_root_key FROM codex_session_files "
        "WHERE source_root_key IS NOT NULL")})
    week = dt.timedelta(days=7)
    adoption_bounds = (now - week - dt.timedelta(hours=1), now + week)
    recent = now - dt.timedelta(days=sources.DASHBOARD_QUOTA_RECENT_DAYS)
    return [
        ("W1.doctor_latest_quota", "_cctally_doctor",
         "_load_codex_quota_observations_for_doctor", {"force_cold": True},
         lambda: tuple(doctor._load_codex_quota_observations_for_doctor(
             force_cold=True))),
        ("W2.codex_conversation_metadata", "_cctally_dashboard_sources",
         "_codex_conversation_metadata", {"cache_conn": "cache.db"},
         lambda: sources._codex_conversation_metadata(cache)),
        ("W3a.adoption_quota_read", "_cctally_quota",
         "load_codex_quota_observations",
         {"source_root_keys": "all", "canonical_resets_between":
          [b.isoformat() for b in adoption_bounds]},
         lambda: tuple(quota.load_codex_quota_observations(
             source_root_keys=set(roots), cache_conn=cache,
             canonical_resets_between=adoption_bounds))),
        ("W3b.dashboard_quota_read", "_cctally_quota",
         "load_codex_quota_observations",
         {"captured_at_or_after": recent.isoformat(), "active_at": "now",
          "max_rows": sources.DASHBOARD_QUOTA_OBSERVATION_LIMIT},
         lambda: tuple(quota.load_codex_quota_observations(
             source_root_keys=set(roots), cache_conn=cache,
             captured_at_or_after=recent, active_at=now,
             max_rows=sources.DASHBOARD_QUOTA_OBSERVATION_LIMIT))),
        ("W3b.breakdown_quota_read", "_cctally_quota",
         "load_codex_quota_observations",
         {"per_root": True, "captured_at_or_after": (now - week).isoformat()},
         lambda: [tuple(quota.load_codex_quota_observations(
             source_root_keys={root}, cache_conn=cache,
             captured_at_or_after=now - week)) for root in roots]),
        ("W3b.claude_stats_digest", "_lib_dashboard_sources",
         "_stats_relations_digest", {"relations": "claude", "memo": None},
         lambda: lib_sources._stats_relations_digest(
             stats, lib_sources._CLAUDE_STATS_DIGEST_RELATIONS)),
        ("W3b.codex_stats_digest", "_lib_dashboard_sources",
         "_stats_relations_digest", {"relations": "codex", "memo": None},
         lambda: lib_sources._stats_relations_digest(
             stats, lib_sources._CODEX_STATS_DIGEST_RELATIONS)),
        ("W3b.current_week_snapshots", "_cctally_forecast",
         "_fetch_current_week_snapshots", {},
         lambda: forecast._fetch_current_week_snapshots(stats, now)),
        ("W3b.pricing_models_30d", "_cctally_pricing_check",
         "_pricing_observed_models", {"since": "default"},
         lambda: pricing._pricing_observed_models(now)),
        ("W3b.pricing_models_all", "_cctally_pricing_check",
         "_pricing_observed_models", {"since": None},
         lambda: pricing._pricing_observed_models(now, since=None)),
        # Revision 9 (901-PA-001): the two remaining history-sized sorts.
        ("PA001a.reconcile_full_pass", "_cctally_quota",
         "reconcile_codex_quota_projection", {"force_full": True},
         lambda: quota.reconcile_codex_quota_projection(
             now=now, force_full=True)),
        ("PA001b.session_latest_meta", "_lib_conversation_query",
         "_session_latest_meta_map", {"sessions": "20 longest"},
         lambda: conv_query._session_latest_meta_map(conv, long_sessions)),
    ]


def percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    return values[max(0, min(len(values) - 1, -(-int(q * 100) * len(values)
                                                // 100) - 1))]


SYNC_KINDS = (("sync_claude_conversations", "conversationSyncClaude"),
              ("sync_codex_conversations", "conversationSyncCodex"),
              ("sync_cache", "claudeCacheSync"),
              ("sync_codex_cache", "codexCacheSync"))


def _rows_of(stats) -> dict:
    out = {}
    for name in ("entries_inserted", "rows_inserted", "messages_inserted",
                 "events_inserted", "files_processed", "bytes_read",
                 "files_total"):
        value = getattr(stats, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            out[name] = value
    return out


def sync_passes(c, passes: int, root: pathlib.Path) -> dict:
    """Time every sync and ingest call over the three populations."""
    import argparse as _argparse

    cache = c._load_sibling("_cctally_cache")
    journal = c._load_sibling("_cctally_journal")
    samples = []
    label = {"value": "catchUp"}

    def wrap(name, kind):
        real = getattr(cache, name)

        def timed(*a, **k):
            started = time.perf_counter()
            result = real(*a, **k)
            samples.append({"kind": kind, "population": label["value"],
                            "seconds": time.perf_counter() - started,
                            "input": _rows_of(result)})
            return result
        setattr(cache, name, timed)

    for name, kind in SYNC_KINDS:
        wrap(name, kind)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import workload

    args = _argparse.Namespace(source="all", rebuild=False,
                               prune_orphans=False, prune_conversations=False)

    def one_pass(population, appended):
        label["value"] = population
        c.cmd_cache_sync(args)
        started = time.perf_counter()
        result = journal.run_stats_ingest(mode="authoritative", timeout_s=60.0)
        samples.append({"kind": "statsIngest", "population": population,
                        "seconds": time.perf_counter() - started,
                        "input": {"consumed": result.consumed,
                                  "appendedBytes": appended}})

    one_pass("catchUp", 0)
    for _ in range(passes):
        one_pass("noChange", 0)
    for _ in range(passes):
        appended = workload.append(root, 0.1, 20, scratch=True)["bytes"]
        one_pass("smallDelta", appended)
    report = {}
    for kind in [k for _n, k in SYNC_KINDS] + ["statsIngest"]:
        for population in ("catchUp", "noChange", "smallDelta"):
            rows = [s for s in samples if s["kind"] == kind
                    and s["population"] == population]
            seconds = [s["seconds"] for s in rows]
            report.setdefault(kind, {})[population] = {
                "n": len(rows), "p50S": percentile(seconds, 0.50),
                "p95S": percentile(seconds, 0.95),
                "maxS": max(seconds, default=None),
                "inputs": [s["input"] for s in rows]}
    return report


# ── input mode (spec §6.3 revision 15, Amendment 12) ─────────────────────

def qualification_problems(qualification, seal, store_root) -> "list[str]":
    """Why a frozen latency run may not use this store copy ([] when it may):
    the copy's qualification receipt must exist, be qualified, and name this
    freeze's seal and this copy (a realpath), as a frozen catch-up requires
    (catchup.frozen_identity_problems). Pure."""
    if not isinstance(qualification, dict):
        return ["no qualification receipt for this store copy and freeze "
                "(frozen_roots.py qualify --store SRC_ROOT --freeze FREEZE)"]
    problems = []
    if qualification.get("qualified") is not True:
        problems.append("the store copy and freeze are not qualified: "
                        + "; ".join((qualification.get("problems") or [])[:3]))
    named_seal = (qualification.get("freeze") or {}).get("sealSha256")
    if named_seal != (seal or {}).get("sealSha256"):
        problems.append(f"the qualification names another freeze ({named_seal})")
    named = (qualification.get("store") or {}).get("root")
    if named != store_root:
        problems.append(f"the qualification names another store copy ({named}), "
                        f"not {store_root} (frozen_roots.py qualify --store "
                        f"{store_root})")
    return problems


def source_problems(source, freeze) -> "list[str]":
    """qualification_problems for SRC_ROOT's receipt against FREEZE."""
    if not source:
        return ["a frozen latency run names the store copy its clone came from "
                "(--source SRC_ROOT), whose qualification it needs"]
    root = os.path.realpath(source)
    path = pathlib.Path(root) / "frozen-qualification.json"
    try:
        qualification = json.loads(path.read_text())
    except FileNotFoundError:
        qualification = None
    except (OSError, ValueError) as exc:
        return [f"unreadable qualification receipt {path}: {exc}"]
    return qualification_problems(qualification,
                                  _frozen_roots().seal_identity(freeze), root)


# ── pair: the candidate against the baseline (spec §6.3) ──────────────────

SITE_LIMIT = "<= baseline median"
SYNC_FACTOR = 1.2
SYNC_LIMIT = "<= 1.2 x baseline p50 and p95"
POPULATIONS = ("catchUp", "noChange", "smallDelta")


def _ratio(a, b):
    return round(a / b, 6) if a is not None and b else None


def compare(candidate: dict, baseline: dict) -> "tuple[list, list, dict]":
    """(invalid, problems, detail) for two latency receipts. Pure. W1-W3
    sites: candidate median <= baseline median. PA-001 sites: reported, no
    limit (the spec states none). Sync passes: candidate p50 and p95 <=
    1.2 x the baseline's per matched kind and population."""
    invalid, problems, detail = [], [], {}
    if "sites" in candidate and "sites" in baseline:
        if candidate.get("calls") != baseline.get("calls"):
            invalid.append(f"unmatched calls per site: candidate "
                           f"{candidate.get('calls')}, baseline {baseline.get('calls')}")
        sites = {}
        for name in sorted(set(candidate["sites"]) | set(baseline["sites"])):
            c, b = candidate["sites"].get(name), baseline["sites"].get(name)
            if c is None or b is None:
                side = "candidate" if c is None else "baseline"
                invalid.append(f"site {name} is missing from the {side} receipt")
                continue
            cm, bm = c.get("medianS"), b.get("medianS")
            if not isinstance(cm, (int, float)) or not isinstance(bm, (int, float)):
                invalid.append(f"site {name} has no median on both sides")
                continue
            limited = name.startswith("W")
            sites[name] = {"candidateMedianS": cm, "baselineMedianS": bm,
                           "candidateMaxS": c.get("maxS"),
                           "baselineMaxS": b.get("maxS"),
                           "ratio": _ratio(cm, bm),
                           "limit": SITE_LIMIT if limited else None}
            if limited and cm > bm:
                problems.append(f"{name}: candidate median {cm} s > baseline "
                                f"median {bm} s")
        detail["sites"] = sites
    elif "syncPasses" in candidate and "syncPasses" in baseline:
        if candidate.get("passes") != baseline.get("passes"):
            invalid.append(f"unmatched passes: candidate {candidate.get('passes')}, "
                           f"baseline {baseline.get('passes')}")
        cs_all, bs_all = candidate["syncPasses"], baseline["syncPasses"]
        sync = {}
        for kind in sorted(set(cs_all) | set(bs_all)):
            for population in POPULATIONS:
                cs = (cs_all.get(kind) or {}).get(population) or {}
                bs = (bs_all.get(kind) or {}).get(population) or {}
                cn, bn = int(cs.get("n") or 0), int(bs.get("n") or 0)
                row = {side: {k: data.get(k) for k in ("n", "p50S", "p95S", "maxS")}
                       for side, data in (("candidate", cs), ("baseline", bs))}
                row["inputs"] = {"candidate": cs.get("inputs"),
                                 "baseline": bs.get("inputs")}
                sync.setdefault(kind, {})[population] = row
                if cn == 0 and bn == 0:
                    row.update({"applicable": False, "limit": None})
                    continue
                if cn == 0 or bn == 0:
                    invalid.append(f"{kind} {population}: unmatched population "
                                   f"(candidate n={cn}, baseline n={bn})")
                    continue
                row.update({"applicable": True, "limit": SYNC_LIMIT})
                for q in ("p50S", "p95S"):
                    row[q[:3] + "Ratio"] = _ratio(cs.get(q), bs.get(q))
                    limit = SYNC_FACTOR * bs[q]
                    if cs[q] > limit:
                        problems.append(f"{kind} {population}: candidate {q[:3]} "
                                        f"{cs[q]} s > 1.2 x baseline {bs[q]} s "
                                        f"= {limit:.6f} s")
        detail["syncPasses"] = sync
    else:
        invalid.append("the receipts are not the same kind: compare call sites "
                       "with call sites (latency.py) and sync passes with sync "
                       "passes (latency.py --sync-passes)")
    return invalid, problems, detail


def _emit(result, code) -> int:
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    print({0: "PASS", 1: "FAIL", 2: "INVALID"}[code])
    return code


def pair(candidate_run, baseline_run) -> int:
    """The recipe's candidate/baseline comparison of two run-latency.sh runs:
    input evidence first (each run's inputs.json, one freeze or both live,
    as `workload.py b-pair`), then `compare` over RUN/latency.json."""
    fr = _frozen_roots()
    invalid, labels, receipts, sides = [], {}, {}, {}
    for name, run in (("candidate", candidate_run), ("baseline", baseline_run)):
        labels[name] = fr.evidence_label(run)
        invalid.extend(f"{name}: inputs: {p}" for p in fr.check_inputs(run))
        path = pathlib.Path(run) / "latency.json"
        try:
            receipts[name] = json.loads(path.read_text())
        except (OSError, ValueError):
            receipts[name] = None
            invalid.append(f"{name}: no latency receipt ({path})")
        receipt = receipts[name] or {}
        sides[name] = {"run": str(run), "inputs": labels[name],
                       "tree": receipt.get("tree"), "gitRev": receipt.get("gitRev")}
    if labels["candidate"] != labels["baseline"]:
        invalid.append("mixed input evidence in one comparison: candidate "
                       f"{labels['candidate']}, baseline {labels['baseline']} "
                       "(revision 15: one freeze, or both live)")
    # Amendment 19 HR-9: the baseline is 56e66f07a and the candidate is not.
    sys.path.insert(0, str(HERE))
    import workload
    invalid.extend(workload.tree_identity_problems(sides["candidate"]["gitRev"],
                                                   sides["baseline"]["gitRev"]))
    problems, detail = [], {}
    if receipts["candidate"] is not None and receipts["baseline"] is not None:
        unmatched, problems, detail = compare(receipts["candidate"],
                                              receipts["baseline"])
        invalid.extend(unmatched)
    if invalid:
        return _emit({"valid": False, "problems": invalid, **sides, **detail}, 2)
    return _emit({"valid": True, "problems": problems, **sides, **detail},
                 1 if problems else 0)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["pair"]:
        p = argparse.ArgumentParser(prog="latency.py pair")
        p.add_argument("candidate")
        p.add_argument("baseline")
        a = p.parse_args(argv[1:])
        return pair(a.candidate, a.baseline)
    fr = _frozen_roots()
    fr.require_namespace("latency.py")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--calls", type=int, default=5)
    parser.add_argument("--sync-passes", type=int, default=0)
    parser.add_argument("--root", type=pathlib.Path)
    parser.add_argument("--source", help="SRC_ROOT the clone was made from "
                        "(required in frozen mode: its qualification)")
    args = parser.parse_args(argv)
    freeze = fr.frozen_freeze()
    raw = os.environ.get("WRITE_ATTRIBUTION_INPUTS", "")
    inputs = {"mode": "frozen" if freeze else ("live" if raw == "live" else None),
              "freeze": None,
              "source": os.path.realpath(args.source) if args.source else None}
    if freeze:
        problems = source_problems(args.source, freeze)
        if problems:
            print("latency.py: refusing: " + "; ".join(problems), file=sys.stderr)
            return 2
        inputs["freeze"] = fr.seal_identity(freeze)["sealSha256"]
    if args.sync_passes and args.root is None:
        parser.error("--sync-passes needs --root (the clone with scratch roots)")
    tree = args.tree.resolve()
    rev = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    c = load_tree(tree)
    now = dt.datetime.now(UTC)
    if args.sync_passes:
        report = {"tree": str(tree), "gitRev": rev or None,
                  "measuredAt": now.isoformat(), "passes": args.sync_passes,
                  "inputs": inputs,
                  "syncPasses": sync_passes(c, args.sync_passes, args.root)}
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True,
                                       default=str) + "\n")
        return 0
    report = {"tree": str(tree), "gitRev": rev or None,
              "measuredAt": now.isoformat(), "calls": args.calls,
              "inputs": inputs, "sites": {}}
    for name, module, function, call_args, thunk in call_sites(c, now):
        thunk()                                   # warm-up, untimed
        samples = []
        for _ in range(args.calls):
            started = time.perf_counter()
            thunk()
            samples.append(time.perf_counter() - started)
        report["sites"][name] = {"module": module, "function": function,
                                 "args": call_args, **summarize(samples)}
        print(f"{name:34s} median {report['sites'][name]['medianS']:.4f}s "
              f"max {report['sites'][name]['maxS']:.4f}s", flush=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
