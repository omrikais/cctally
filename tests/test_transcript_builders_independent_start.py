"""#965 T7b (965-SPEC-003): every standalone transcript builder sets the test
retention policy itself. Each starts with the switch ABSENT, no parent
preamble and no persisted retention setting, declared as an independent-start
control so the launch guard admits it."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

TESTS = pathlib.Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import _lib_test_isolation as iso  # noqa: E402
from _retention_entry_points import SWITCH, TRANSCRIPT_BUILDERS  # noqa: E402
from _retention_fixtures import BUILDER_TIMEOUT_S  # noqa: E402

REPO_ROOT = TESTS.parent
BIN = REPO_ROOT / "bin"
READER_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex-reader"
WIRE = ("wire-detail.json", "wire-detail-segmented.json",
        "wire-detail-tool-legibility.json", "wire-outline-modern-full.json",
        "wire-outline-segmented-turn.json", "wire-outline-tool-legibility.json")
COVERED = {"build-codex-reader-fixtures.py", "build-bench-fixtures.py"}


def _independent(tmp_path, argv):
    env = {k: v for k, v in os.environ.items() if k != SWITCH}
    env["HOME"] = str(tmp_path / "home")
    env.pop("CCTALLY_DATA_DIR", None)
    with iso.retention_launch_control(
            "independent-start", reason="#965 T7b: the builder must set the switch itself"):
        # timing-budget: #965 hang bound for one transcript-builder run, BUILDER_TIMEOUT_S (100 s) from tests/_retention_fixtures.py, under the 120 s cap
        return subprocess.run([sys.executable, *map(str, argv)], env=env, cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=BUILDER_TIMEOUT_S)


def test_every_transcript_builder_has_an_independent_start_test():
    """Compares two written-down sets, so on its own it proves nothing about the
    builders on disk. It is sound only together with
    tests/test_harness_dev_autodetect_coverage.py::
    test_965_the_builder_population_is_derived_not_listed, which ties
    TRANSCRIPT_BUILDERS to the set derived from every bin/build-*.py's AST: a new
    ingest-driving builder fails that test until it is listed, and then fails
    this one until it gets an independent-start test below."""
    assert set(TRANSCRIPT_BUILDERS) == COVERED


def test_the_reader_builder_sets_the_policy_itself(tmp_path):
    out = tmp_path / "wire"
    proc = _independent(tmp_path, [BIN / "build-codex-reader-fixtures.py", "--out-dir", out])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for name in WIRE:
        assert (out / name).read_bytes() == (READER_FIXTURES / name).read_bytes(), (
            f"{name} differs from the committed wire fixture")


def test_the_bench_builder_sets_the_policy_itself(tmp_path):
    """PRESERVATION (AC6), not a defect reproduction: build_fixture persists
    `retention_days: 0` (bin/build-bench-fixtures.py:1698-1699) before it
    ingests, and an explicit 0 wins on every code version, so this passes on the
    baseline too. It proves the builder still meets its own success criterion
    started alone; its switch assignment is proved by the static AST check
    (test_965_every_transcript_builder_assigns_the_switch_before_loading_cctally)."""
    root = tmp_path / "bench"
    proc = _independent(tmp_path, [BIN / "build-bench-fixtures.py", "--scale", "tiny",
                                   "--out", root])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(l for l in proc.stdout.splitlines() if l.startswith("dataset_counts: "))
    dataset = json.loads(line.split(": ", 1)[1])
    assert dataset["messages"] > 0 and dataset["codex_conversation_messages"] > 0, dataset
    config = json.loads((root / "data" / "config.json").read_text())
    assert config["conversation"]["retention_days"] == 0, (
        "the bench corpus lost its persisted retention policy (AC6)")
    # AC6's other half: the corpus was produced by GENERATOR_VERSION 10. The
    # marker folds the version into params_hash (bin/build-bench-fixtures.py,
    # _marker_payload), so the persisted hash is recomputed here with the
    # literal 10 rather than read back from the module under test.
    from _shared_corpus import _load_bench_generator
    generator = _load_bench_generator()
    assert generator.GENERATOR_VERSION == 10, generator.GENERATOR_VERSION
    marker = json.loads((root / "data" / ".bench-fixture.json").read_text())
    shape = json.dumps({"params": generator.SCALES["tiny"], "generator": 10,
                        "identity_root": None}, sort_keys=True)
    assert marker["params_hash"] == hashlib.sha256(shape.encode()).hexdigest()[:16], (
        "the bench corpus was not produced by GENERATOR_VERSION 10 (AC6)")
