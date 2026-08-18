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
import os
import pathlib
import sqlite3
import sys

import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"

# Set by `bin/cctally-bench.run_all` itself, not by the generator's `_pin_env`,
# so it is absent from PINNED_ENV_KEYS and has to be named separately here.
# `run_all` exports the corpus clock for the command entry points that read it
# and deliberately does not restore it, which is correct for the real CLI and a
# leak in-process: a sibling test on the same pytest-xdist worker then resolves
# "now" as 2026-01-07T00:00:00Z. Measured on the runner, that made
# `record-usage` reject a `--resets-at` row as outside its plausibility band and
# left `current_week` None in `tests/test_dashboard_api_events.py`.
_RUNNER_ENV_KEYS = ("CCTALLY_AS_OF",)


@pytest.fixture(autouse=True)
def _isolate_bench_env():
    """The generator + runner pin CCTALLY_DATA_DIR, CLAUDE_CONFIG_DIR,
    CODEX_HOME and HOME via os.environ directly (so a freshly-loaded cctally
    targets the scratch dirs), and leave them set. Snapshot + restore them here
    so the mutation can't leak into a sibling test on the same pytest-xdist
    worker — a leaked CCTALLY_DATA_DIR override otherwise wins over that test's
    HOME-based path resolution and points APP_DIR at this test's since-deleted
    tmp dir, and a leaked HOME/CODEX_HOME resolves that test's user state
    through a deleted scratch home.

    The pinned half of the key list is READ from
    `build_bench_fixtures.PINNED_ENV_KEYS` rather than restated. Four
    hand-maintained copies of "the pinned axes" had already drifted to lengths
    5, 5, 4 and 5 with nothing comparing them, which is the drift class that
    constant was introduced to end. `_RUNNER_ENV_KEYS` covers the axes the
    RUNNER sets on its own, which that constant does not describe."""
    keys = tuple(_load_build_bench().PINNED_ENV_KEYS) + _RUNNER_ENV_KEYS
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # Re-resolve the path globals from the restored env so a later test that
    # reuses the cached cctally module sees clean prod-layout paths.
    mod = sys.modules.get("cctally")
    if mod is not None:
        try:
            mod._cctally_core._init_paths_from_env()
        except Exception:
            pass


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
    a = gen.build_fixture_isolated(scale="small", seed=42, root=tmp_path / "a")
    b = gen.build_fixture_isolated(scale="small", seed=42, root=tmp_path / "b")
    ca = gen.open_fixture_db(a)
    cb = gen.open_fixture_db(b)
    try:
        assert gen.semantic_hash(ca) == gen.semantic_hash(cb)
        assert gen.dataset_counts(ca) == gen.dataset_counts(cb)
    finally:
        ca.close()
        cb.close()


def test_corpus_shapes(tmp_path):
    gen = _load_build_bench()
    data = gen.build_fixture_isolated(scale="small", seed=42, root=tmp_path)
    conn = gen.open_fixture_db(data)
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
            "SELECT COUNT(DISTINCT model) FROM cache_db.session_entries"
        ).fetchone()[0]
        assert models >= 2                        # model diversity for reconciles
    finally:
        conn.close()


def test_pinned_env_restores_every_pinned_axis(tmp_path):
    """pinned_env restores every variable it sets, including on exception.

    The key list is READ from `PINNED_ENV_KEYS`. It used to be a fourth
    hand-written copy, and it was short by one: `CCTALLY_DISABLE_DEV_AUTODETECT`
    is set by `_pin_env` via `setdefault` and promised by `pinned_env`'s
    docstring, and nothing asserted its restoration.
    """
    bbf = _load_build_bench()

    keys = bbf.PINNED_ENV_KEYS
    before = {k: os.environ.get(k) for k in keys}

    with bbf.pinned_env(
        tmp_path / "data", tmp_path / "claude",
        tmp_path / "codex", tmp_path / "home",
    ):
        assert os.environ["CCTALLY_DATA_DIR"] == str(tmp_path / "data")
        assert os.environ["CODEX_HOME"] == str(tmp_path / "codex")
        assert os.environ["HOME"] == str(tmp_path / "home")

    assert {k: os.environ.get(k) for k in keys} == before

    try:
        with bbf.pinned_env(
            tmp_path / "d2", tmp_path / "c2", tmp_path / "x2", tmp_path / "h2",
        ):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert {k: os.environ.get(k) for k in keys} == before, (
        "an exception inside the block must not leak the pins")


def test_building_a_fixture_from_a_test_restores_every_pinned_axis(
    tmp_path, monkeypatch
):
    """No test caller of the builder may change the process it runs in.

    `build_fixture` pins five environment keys and deliberately leaves them
    pinned, which is right for `_main` and `bin/cctally-bench` and wrong for a
    gate: the next test on this pytest-xdist worker would resolve HOME through
    a scratch directory that no longer exists. Measured on the runner before
    this fix, via `tests/test_conversation_assembly_perf.py`: HOME became
    that test's per-test scratch home and left replaced after it finished.
    """
    bbf = _load_build_bench()
    # ESTABLISH the absence rather than assuming it: a maintainer with
    # CODEX_HOME exported would otherwise fail this test for a reason that has
    # nothing to do with the property under test.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    before = {k: os.environ.get(k) for k in bbf.PINNED_ENV_KEYS}
    assert before.get("CODEX_HOME") is None, (
        "precondition: CODEX_HOME is ABSENT here, so this test also proves "
        "absence is restored as absence rather than as an empty string")

    bbf.build_fixture_isolated(scale="small", seed=42, root=tmp_path / "iso")

    after = {k: os.environ.get(k) for k in bbf.PINNED_ENV_KEYS}
    assert after == before, (
        "build_fixture_isolated changed the process: "
        + repr({k: (before[k], after[k]) for k in before if before[k] != after[k]}))

    # Non-vacuity: the RAW primitive really does leak, so the wrapper above is
    # not asserting a property the primitive already had. This file's autouse
    # `_isolate_bench_env` restores it at teardown.
    bbf.build_fixture(scale="small", seed=42, root=tmp_path / "raw")
    leaked = {k: (before[k], os.environ.get(k))
              for k in bbf.PINNED_ENV_KEYS if os.environ.get(k) != before[k]}
    assert leaked, (
        "the raw builder no longer leaks, so build_fixture_isolated is a no-op "
        "and this test proves nothing")


def test_marker_params_hash_covers_every_scale(tmp_path):
    """A profile-shape change must bust the cached fixture for any scale."""
    bbf = _load_build_bench()

    with bbf.pinned_env(tmp_path / "d", tmp_path / "c",
                        tmp_path / "x", tmp_path / "h") as cctally:
        for scale in sorted(bbf.SCALES):
            payload = bbf._marker_payload(cctally, seed=42, scale=scale)
            assert "params_hash" in payload, (
                f"{scale} marker cannot detect a profile change")

        base = bbf._marker_payload(cctally, seed=42, scale="small")
        original = dict(bbf.SCALES["small"])
        try:
            bbf.SCALES["small"] = {**original, "sessions": original["sessions"] + 1}
            changed = bbf._marker_payload(cctally, seed=42, scale="small")
        finally:
            bbf.SCALES["small"] = original
        assert changed["params_hash"] != base["params_hash"], (
            "changing a profile's cardinality must change its marker")


# ── Task 2: runner JSON schema ────────────────────────────────────────────

# The 15 registered benchmark families (spec §4.2), asserted here and in the
# bin/cctally-bench-test self-test.
_EXPECTED_BENCHMARKS = {
    "snapshot.cold", "snapshot.warm", "snapshot.idle",
    "sync.noop", "sync.delta",
    "conversations.page1", "conversations.sorted", "conversations.filtered",
    "search.cross_session", "find.in_conversation",
    "payload.assemble", "outline.build", "payload.assemble_memo_hit",
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
    # Exact set equality (Codex F6): an accidental extra default benchmark must
    # fail the test, not slip through as a NEW compare row.
    assert set(result["benchmarks"]) == _EXPECTED_BENCHMARKS
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


@pytest.mark.parametrize("data_dir,claude_dir", [("d", None), (None, "c")])
def test_realism_partial_args_error(tmp_path, data_dir, claude_dir):
    """Passing exactly one of --data-dir / --claude-dir must error loudly, not
    silently fall back to the synthetic fixture (review M2). The XOR guard
    raises before any fixture build, so no real run is needed."""
    bench = _load_bin("cctally-bench")
    dd = str(tmp_path / data_dir) if data_dir else None
    cd = str(tmp_path / claude_dir) if claude_dir else None
    with pytest.raises(ValueError, match="BOTH --data-dir and --claude-dir"):
        bench.run_all(scale="small", seed=1, iterations=1, trace=False,
                      root=tmp_path, data_dir=dd, claude_dir=cd)


# ── Task 4: --assembly-scan structure (Session C / M5) ────────────────────

_EXPECTED_RUNG_KEYS = {
    "turn_count", "msg_count", "item_count",
    "assemble_ms", "detail_tail_ms", "detail_page_ms", "outline_ms",
    "find_hit_ms", "open_pair_ms",
    "assembled_items_bytes", "page_bytes_200", "page_bytes_500",
    "page_bytes_1000", "outline_bytes",
}
# Structural (deterministic) columns — everything else is a machine-variant ms.
_STRUCTURAL_KEYS = {
    "turn_count", "msg_count", "item_count", "assembled_items_bytes",
    "page_bytes_200", "page_bytes_500", "page_bytes_1000", "outline_bytes",
}


def test_assembly_scan_structure_and_determinism(tmp_path):
    bench = _load_bin("cctally-bench")
    gen = _load_build_bench()
    ladder = gen.ASSEMBLY_TURN_LADDER_SMALL

    a = bench.run_assembly_scan(ladder_scale="small", iterations=1,
                                root=tmp_path / "a")
    assert a["schemaVersion"] == 1
    assert a["ladder_scale"] == "small"
    for k in ("cctally_version", "machine_label", "dataset_counts", "rungs",
              "visible_ms"):
        assert k in a, k
    assert len(a["rungs"]) == len(ladder)
    for i, r in enumerate(a["rungs"]):
        assert set(r) == _EXPECTED_RUNG_KEYS, sorted(set(r) ^ _EXPECTED_RUNG_KEYS)
        # ladder shape (Codex F8): turn_count + msg_count == 2 * turns.
        assert r["turn_count"] == ladder[i], (i, r["turn_count"])
        assert r["msg_count"] == 2 * ladder[i], (i, r["msg_count"])
        assert r["item_count"] > 0
        # never assert absolute timings — only ordering sanity (non-negative).
        for msk in ("assemble_ms", "outline_ms", "find_hit_ms", "open_pair_ms"):
            assert r[msk] >= 0.0, msk

    # Structural columns are byte-stable across an independent second build.
    b = bench.run_assembly_scan(ladder_scale="small", iterations=1,
                                root=tmp_path / "b")

    def _structural(res):
        return [{k: r[k] for k in _STRUCTURAL_KEYS} for r in res["rungs"]]

    assert _structural(a) == _structural(b)


def test_assembly_scan_incompatible_with_default_baseline_flags():
    """Codex F7: --assembly-scan + a default-suite baseline flag errors."""
    bench = _load_bin("cctally-bench")
    for flag in ("--compare", "--gate", "--update-baseline"):
        with pytest.raises(SystemExit) as ei:
            bench.main(["--assembly-scan", flag])
        assert ei.value.code == 2   # argparse parser.error exit code


# ── Task 6 (#583 S1): the contract/receipt baseline ───────────────────────


def test_classify_reads_the_contract_shaped_baseline():
    """classify() must not report a contract-shaped baseline as malformed."""
    bench = _load_bin("cctally-bench")
    baseline = {
        "contract": {
            "benchmark_names": ["snapshot.warm"],
            "corpus_fingerprint": "abc123",
            "dataset_counts": {"entries": 10},
        },
        "receipt": {
            "cctally_version": "1.99.0",
            "machine_label": "test-machine",
            "benchmarks": {"snapshot.warm": {"median_ms": 10.0}},
        },
    }
    current = {
        "machine_label": "test-machine",
        "benchmarks": {"snapshot.warm": {"median_ms": 10.5}},
    }
    out = bench.classify(baseline, current, pct=0.2, floor_ms=5.0)
    assert out["_meta"]["malformed"] is False
    assert out["_meta"]["machine_mismatch"] is False
    assert out["snapshot.warm"]["status"] == "OK"


def test_update_baseline_preserves_the_contract_block(tmp_path, monkeypatch):
    """--update-baseline must re-record the receipt, not flatten the file."""
    import json as _json
    bench = _load_bin("cctally-bench")
    target = tmp_path / "backend.json"
    monkeypatch.setattr(bench, "BASELINE_PATH", target)
    bench._write_baseline({
        "cctally_version": "1.99.0",
        "machine_label": "m",
        "benchmarks": {"snapshot.warm": {"median_ms": 1.0}},
        "dataset_counts": {"entries": 1},
        "corpus_fingerprint": "abc123",
        "generator_version": 4,
        "scale": "large",
        "seed": 42,
    })
    written = _json.loads(target.read_text())
    assert "contract" in written and "receipt" in written
    assert "benchmark_names" in written["contract"]
    # The two blocks duplicate these; the harness asserts they agree, so a
    # writer that let them diverge would put the contract out of date silently.
    for key in ("scale", "seed", "dataset_counts", "corpus_fingerprint",
                "generator_version"):
        assert written["contract"][key] == written["receipt"][key], key


def test_write_baseline_refuses_a_run_with_no_corpus_fingerprint(
    tmp_path, monkeypatch
):
    """Realism mode computes no fingerprint, so its baseline cannot satisfy the
    contract the harness asserts. Refuse at WRITE time rather than deferring the
    failure to an unrelated harness run later."""
    bench = _load_bin("cctally-bench")
    target = tmp_path / "backend.json"
    monkeypatch.setattr(bench, "BASELINE_PATH", target)
    with pytest.raises(SystemExit, match="no corpus fingerprint"):
        bench._write_baseline({
            "cctally_version": "1.99.0",
            "machine_label": "m",
            "benchmarks": {"snapshot.warm": {"median_ms": 1.0}},
            "dataset_counts": {"entries": 1},
            "corpus_fingerprint": None,
        })
    assert not target.exists(), "the refusal must not leave a partial file"


def test_the_committed_baseline_is_contract_shaped():
    """The file in the tree must be the shape every reader now expects."""
    import json as _json
    bench = _load_bin("cctally-bench")
    written = _json.loads(bench.BASELINE_PATH.read_text())
    assert set(written) >= {"contract", "receipt"}, sorted(written)
    contract = written["contract"]
    assert set(contract["benchmark_names"]) == _EXPECTED_BENCHMARKS
    assert contract["corpus_fingerprint"]
    assert contract["generator_version"]
    assert contract["scale"] == "large"
    for key in ("entries", "codex_entries", "codex_files", "quota_windows"):
        assert contract["dataset_counts"].get(key), key
    # The receipt is advisory and must never be asserted on value; assert only
    # that it is present and carries the run's identity.
    assert written["receipt"]["cctally_version"]
    assert written["receipt"]["machine_label"]
