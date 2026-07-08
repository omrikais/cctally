"""Structural + determinism unit tests for the backend benchmark suite
(issue #276, M3 / Session B).

These are the pytest half of the M3 test plan (spec §7). They assert the
GENERATOR's semantic determinism + corpus adequacy (Task 1), the RUNNER's
JSON schema (Task 2), and the compare/gate status taxonomy on synthetic
numbers (Task 3). They NEVER assert wall-clock timings — the bench self-test
harness (bin/cctally-bench-test) and this module both stay timing-free; the
only committed timings live in bench/baselines/backend.json as advisory data.

The two bin scripts under test have no ``.py`` extension / carry a hyphen, so
they are path-loaded via importlib rather than imported by name.
"""
import importlib.machinery
import importlib.util
import pathlib
import sqlite3

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"


def _load_path(mod_name, file_name):
    """Path-load a bin/ script (hyphenated / extensionless) as a module."""
    path = BIN / file_name
    loader = importlib.machinery.SourceFileLoader(mod_name, str(path))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _load_build_bench():
    return _load_path("build_bench_fixtures", "build-bench-fixtures.py")


def _load_bin(name):
    """Path-load an executable bin/cctally-* script (e.g. ``cctally-bench``)."""
    return _load_path(name.replace("-", "_"), name)


# ── Task 1: generator determinism + corpus shape ──────────────────────────

def test_generator_deterministic(tmp_path):
    gen = _load_build_bench()
    a = gen.build_fixture(scale="small", seed=42, root=tmp_path / "a")
    b = gen.build_fixture(scale="small", seed=42, root=tmp_path / "b")
    ca = sqlite3.connect(a / "cache.db")
    cb = sqlite3.connect(b / "cache.db")
    try:
        assert gen.semantic_hash(ca) == gen.semantic_hash(cb)
        assert gen.dataset_counts(ca) == gen.dataset_counts(cb)
    finally:
        ca.close()
        cb.close()


def test_corpus_shapes(tmp_path):
    gen = _load_build_bench()
    data = gen.build_fixture(scale="small", seed=42, root=tmp_path)
    conn = sqlite3.connect(data / "cache.db")
    try:
        counts = gen.dataset_counts(conn)
        assert counts["sessions"] >= 5           # many sessions for the rail
        assert counts["messages"] >= 50          # searchable text
        # >=1 large session above the "large" threshold
        big = conn.execute(
            "SELECT MAX(c) FROM "
            "(SELECT COUNT(*) c FROM conversation_messages GROUP BY session_id)"
        ).fetchone()[0]
        assert big >= gen.SCALES["small"]["large_session_turns"]
        models = conn.execute(
            "SELECT COUNT(DISTINCT model) FROM session_entries"
        ).fetchone()[0]
        assert models >= 2                        # model diversity for reconciles
    finally:
        conn.close()


# ── Task 2: runner JSON schema ────────────────────────────────────────────

# The 14 registered benchmark families (spec §4.2), asserted here and in the
# bin/cctally-bench-test self-test.
_EXPECTED_BENCHMARKS = {
    "snapshot.cold", "snapshot.warm", "snapshot.idle",
    "sync.noop", "sync.delta",
    "conversations.page1", "conversations.sorted", "conversations.filtered",
    "search.cross_session", "find.in_conversation",
    "payload.assemble", "outline.build",
    "reconcile.cache_report", "reconcile.projects_env",
}


def test_run_json_schema(tmp_path):
    bench = _load_bin("cctally-bench")
    result = bench.run_all(scale="small", seed=42, iterations=2, trace=False,
                           root=tmp_path)
    assert result["schemaVersion"] == 1
    for k in ("cctally_version", "machine_label", "scale", "seed",
              "dataset_counts", "benchmarks"):
        assert k in result, k
    assert _EXPECTED_BENCHMARKS <= set(result["benchmarks"])
    for name, b in result["benchmarks"].items():
        assert b["median_ms"] >= 0, name
        assert b["min_ms"] <= b["median_ms"] <= b["max_ms"], name
        # every entry carries the documented (possibly-None) meta keys
        for k in ("count", "bytes", "phases"):
            assert k in b, (name, k)


# ── Task 3: compare / gate taxonomy (pure functions, no timing) ───────────

def _bl(benches, label="darwin-arm64"):
    return {"schemaVersion": 1, "machine_label": label, "benchmarks": benches}


def test_compare_status_taxonomy():
    bench = _load_bin("cctally-bench")
    base = _bl({"a": {"median_ms": 100.0}, "b": {"median_ms": 10.0},
                "gone": {"median_ms": 5.0}})
    cur = _bl({"a": {"median_ms": 100.0}, "b": {"median_ms": 40.0},
               "new": {"median_ms": 1.0}})
    res = bench.classify(base, cur, pct=0.15, floor_ms=15.0)
    assert res["a"]["status"] == "OK"          # unchanged
    assert res["b"]["status"] == "REGRESSED"   # +30 > max(1.5, 15)
    assert res["gone"]["status"] == "MISSING"
    assert res["new"]["status"] == "NEW"


def test_gate_exit_codes():
    bench = _load_bin("cctally-bench")
    base = _bl({"a": {"median_ms": 100.0}})
    ok = _bl({"a": {"median_ms": 101.0}})
    reg = _bl({"a": {"median_ms": 200.0}})
    miss = _bl({"b": {"median_ms": 1.0}})
    assert bench.gate_exit(bench.classify(base, ok, pct=0.15, floor_ms=15.0)) == 0
    assert bench.gate_exit(bench.classify(base, reg, pct=0.15, floor_ms=15.0)) != 0
    assert bench.gate_exit(bench.classify(base, miss, pct=0.15, floor_ms=15.0)) != 0


def test_zero_baseline_uses_floor():
    bench = _load_bin("cctally-bench")
    base = _bl({"idle": {"median_ms": 0.0}})
    cur = _bl({"idle": {"median_ms": 10.0}})
    assert bench.classify(base, cur, pct=0.15, floor_ms=15.0)["idle"]["status"] == "OK"


def test_malformed_baseline_gate_fails():
    bench = _load_bin("cctally-bench")
    cur = _bl({"a": {"median_ms": 1.0}})
    res = bench.classify(None, cur, pct=0.15, floor_ms=15.0)
    assert res["_meta"]["malformed"] is True
    assert bench.gate_exit(res) != 0


def test_machine_mismatch_flagged_not_gated():
    bench = _load_bin("cctally-bench")
    base = _bl({"a": {"median_ms": 100.0}}, label="linux-x86_64")
    cur = _bl({"a": {"median_ms": 300.0}}, label="darwin-arm64")
    res = bench.classify(base, cur, pct=0.15, floor_ms=15.0)
    assert res["_meta"]["machine_mismatch"] is True
    # regression present, but cross-machine → not gated on that alone
    assert bench.gate_exit(res, allow_cross_machine=True) == 0
