"""The estate gate on its PRODUCTION path, with nothing stubbed (#648 D11).

`tests/test_authoritative_test_contract.py` drives the same gate over a stubbed
checker, which is the right shape for asserting the reason-code mapping and the
run-mode contract: it can produce any report on demand. It cannot show that the
real checker produces those reports, and a seam is also the way a check can be
avoided, so this module removes the seam.

Everything here is real. The tree carries the real `bin/_lib_test_estate.py`,
the real `bin/_lib_estate_discovery.py`, the real `bin/_lib-test-contract.sh`
and the real `bin/cctally-test-all`. The estate artifact is DERIVED from the
tree by the same `discover_live` the gate calls, so no case encodes an
assumption about what the collectors produce; each case then mutates that
derived artifact in exactly one way and asserts what the gate says about it.

WHAT IS SYNTHETIC, AND WHY IT IS NOT A BYPASS. Two frontend runner binaries and
the e2e fixture builder are shell fakes, because the real ones need
`dashboard/web/node_modules` and a Vite build that a scratch tree has no way to
carry. They stand in for the RUNNERS, not for any part of the checker: the
kernel still spawns them, still parses their JSON through
`parse_vitest_list` / `parse_playwright_list`, and still refuses everything it
refuses. Removing one of them is how the derivation-failure case is reached.

The tree classifies as the PUBLIC profile: it carries no `.mirror-allowlist`,
so it needs no private overlay and the visibility cross-check does not apply.
The public profile is also the one a public clone runs, which is the profile
this checker most needs to work in.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
RUNNER = BIN / "cctally-test-all"

ARTIFACT = "tests/authoritative-estate.json"
LEDGER = "tests/authoritative-estate-retirements.json"

_VITEST_FAKE = """#!/usr/bin/env bash
cat <<'JSON'
[{"name": "renders", "file": "src/App.test.ts"},
 {"name": "formats a total", "file": "src/fmt.test.ts"}]
JSON
"""

_PLAYWRIGHT_FAKE = """#!/usr/bin/env bash
cat <<'JSON'
{"suites": [{"title": "e2e/dash.spec.ts", "file": "e2e/dash.spec.ts",
             "specs": [{"title": "loads", "file": "e2e/dash.spec.ts",
                        "tests": [{"expectedStatus": "expected",
                                   "projectName": "chromium"}]},
                       {"title": "exports", "file": "e2e/dash.spec.ts",
                        "tests": [{"expectedStatus": "expected",
                                   "projectName": "chromium"}]}]}],
 "errors": []}
JSON
"""

_E2E_BUILDER_FAKE = """#!/usr/bin/env python3
import json
import sys
out = sys.argv[sys.argv.index("--out") + 1]
with open(out + "/manifest.json", "w", encoding="utf-8") as handle:
    json.dump({"fixtures": []}, handle)
"""

_TESTS = {
    "test_alpha.py": "def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n",
    "test_bench.py": "def test_bench():\n    assert True\n",
    "test_gated.py": (
        "import pytest\n\n\n"
        "def test_gated():\n"
        "    if False:\n"
        "        pytest.skip('a recorded suppression')\n"
        "    assert True\n"
    ),
}


def _helper():
    spec = importlib.util.spec_from_file_location(
        "_lib_test_estate_production", BIN / "_lib_test_estate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _write(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def _tree(tmp_path):
    """A tiny repository the real checker can derive a complete estate from."""
    repo = tmp_path / "repo"
    bindir = repo / "bin"
    testsdir = repo / "tests"
    bindir.mkdir(parents=True)
    testsdir.mkdir()

    shutil.copy2(RUNNER, bindir / "cctally-test-all")
    for lib in sorted(BIN.glob("_lib-*.sh")):
        shutil.copy2(lib, bindir / lib.name)
    for kernel in sorted(BIN.glob("_lib_test_*.py")):
        shutil.copy2(kernel, bindir / kernel.name)
    shutil.copy2(BIN / "_lib_harness_durations.py",
                 bindir / "_lib_harness_durations.py")
    # THE REAL discovery kernel, not a stand-in. The checker imports it by path
    # from the tree it is checking, so this is the module under test as much as
    # the helper is.
    shutil.copy2(BIN / "_lib_estate_discovery.py",
                 bindir / "_lib_estate_discovery.py")
    shutil.copy2(REPO / "tests" / "_estate_leg_plugin.py",
                 testsdir / "_estate_leg_plugin.py")
    _write(bindir / "build-e2e-fixtures.py", _E2E_BUILDER_FAKE, 0o755)

    web = repo / "dashboard" / "web"
    _write(web / "node_modules" / ".bin" / "vitest", _VITEST_FAKE, 0o755)
    _write(web / "node_modules" / ".bin" / "playwright", _PLAYWRIGHT_FAKE, 0o755)

    for name, body in _TESTS.items():
        _write(testsdir / name, body)

    for name in ("alpha", "reconcile"):
        _write(bindir / f"cctally-{name}-test",
               "#!/usr/bin/env bash\nprintf 'passed: 1   failed: 0\\n'\n", 0o755)
    _write(testsdir / "authoritative-test-manifest.json", json.dumps({
        "schemaVersion": 1,
        "minHarnessRows": 0,
        "harnesses": [
            {"name": n, "visibility": "public", "minCases": 0,
             "countPolicy": "fixed"} for n in ("alpha", "reconcile")
        ],
        "capabilities": [],
        "forbiddenRegeneration": [],
    }, indent=2) + "\n")

    mod = _helper()
    live = mod.discover_live(repo)
    artifact = dict(live)
    artifact["schemaVersion"] = 1
    artifact["generatedFrom"] = "0" * 40
    artifact["pytestExecution"] = {"legs": [
        {"name": "benchmark", "selectors": ["tests/test_bench.py"]},
        {"name": "pytest", "selectors": [mod.COMPLEMENT_SELECTOR]},
    ]}
    _record(repo, artifact)
    _write(repo / LEDGER, json.dumps(
        {"schemaVersion": 1, "declarations": []}, indent=2) + "\n")

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _record(repo, artifact):
    _write(repo / ARTIFACT, json.dumps(artifact, indent=2, sort_keys=True) + "\n")


def _artifact(repo):
    return json.loads((repo / ARTIFACT).read_text(encoding="utf-8"))


def _drive(repo, args=(), env=None):
    outcome = repo / "outcome.json"
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(repo)),
        "CCTALLY_TEST_ALL_OUTCOME_FILE": str(outcome),
        "CCTALLY_TEST_JOBS": "1",
    }
    environment.update(env or {})
    proc = subprocess.run(
        [str(repo / "bin" / "cctally-test-all"), *args],
        env=environment, capture_output=True, text=True,
        # A HANG bound. A complete run over this tree measures about 3 seconds
        # -- two trivial harnesses, four test functions and the real estate
        # check over a four-file collection -- so 90 is thirtyfold headroom
        # rather than a ceiling. It is not larger because
        # tests/test_timing_budget_guard.py refuses a single budget above 111,
        # being the pytest phase's 120-second per-test cap less the 9 seconds it
        # reserves, and a budget above the cap can never fire: pytest-timeout
        # kills the worker first and takes every test it still holds with it.
        timeout=90,
    )
    proc.outcome_path = outcome  # type: ignore[attr-defined]
    return proc


def _codes(proc):
    path = proc.outcome_path  # type: ignore[attr-defined]
    assert path.exists(), f"no outcome record\n{proc.stdout}\n{proc.stderr}"
    return {x["code"]
            for x in json.loads(path.read_text(encoding="utf-8"))["reasons"]}


# ---------------------------------------------------------------------------
# The control arm
# ---------------------------------------------------------------------------


def test_the_derived_artifact_admits_and_the_run_passes(tmp_path):
    """Without this every case below could pass on a gate that refuses always.

    It is also the only case that proves the real checker AGREES with a real
    derivation: the artifact was written by `discover_live` and is then compared
    against a second, independent call to the same collectors inside the gate.
    """
    repo = _tree(tmp_path)
    proc = _drive(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_derived_artifact_records_all_three_axes(tmp_path):
    """A green control over an EMPTY estate would prove nothing.

    Each axis is asserted non-empty, so the agreement above is agreement about
    rows that exist rather than about three empty lists matching three others.
    """
    repo = _tree(tmp_path)
    doc = _artifact(repo)
    assert len(doc["pytestNodes"]) >= 4, doc["pytestNodes"]
    assert {row["runner"] for row in doc["frontendTests"]} == {
        "vitest", "playwright"}, doc["frontendTests"]
    assert doc["suppressions"], doc["suppressions"]


# ---------------------------------------------------------------------------
# Drift, one axis and one direction at a time
# ---------------------------------------------------------------------------


def test_a_lost_pytest_node_fails_admission(tmp_path):
    """#648 acceptance criterion 4, on the production path.

    The node is removed from the RECORD, so the tree collects an identifier the
    record does not name. That is criterion 4's direction and criterion 4's
    code, whichever side of the comparison the edit was made on. The criterion
    below it covers the other direction.
    """
    repo = _tree(tmp_path)
    doc = _artifact(repo)
    doc["pytestNodes"] = [n for n in doc["pytestNodes"]
                          if not n.endswith("::test_two")]
    _record(repo, doc)
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "manifest-unexpected-pytest-node" in _codes(proc), proc.stderr


def test_a_test_deleted_from_the_tree_fails_admission(tmp_path):
    """#648 acceptance criterion 3 -- the loss this whole mechanism is for.

    The artifact is untouched and the SOURCE is deleted, which is what a merge
    resolution does. The record then names a node the tree does not collect.
    """
    repo = _tree(tmp_path)
    (repo / "tests" / "test_alpha.py").unlink()
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "manifest-missing-pytest-node" in _codes(proc), proc.stderr


def test_a_lost_frontend_test_fails_admission(tmp_path):
    """#648 acceptance criterion 5, frontend axis."""
    repo = _tree(tmp_path)
    doc = _artifact(repo)
    doc["frontendTests"] = [row for row in doc["frontendTests"]
                            if row["runner"] != "vitest"]
    _record(repo, doc)
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "manifest-unexpected-frontend-test" in _codes(proc), proc.stderr


def test_a_playwright_row_flipped_to_skipped_fails_admission(tmp_path):
    """#648 acceptance criterion 9 -- the status is part of the row's identity.

    Flipped in the RECORD rather than in the runner, so the tree still reports
    `expected` and the two disagree. That is the same disagreement a real
    `test.skip(...)` produces, seen from the other side.
    """
    repo = _tree(tmp_path)
    doc = _artifact(repo)
    for row in doc["frontendTests"]:
        if row["runner"] == "playwright":
            row["expectedStatus"] = "skipped"
            break
    _record(repo, doc)
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    codes = _codes(proc)
    assert "manifest-unexpected-frontend-test" in codes, proc.stderr
    assert "manifest-missing-frontend-test" in codes, proc.stderr


def test_a_new_suppression_fails_admission(tmp_path):
    """#648 D3 -- the harmful direction on this axis is ADDITION.

    Added to the TREE, not to the record, which is the shape a suppression
    written under time pressure actually has. Getting this direction backwards
    was the first draft's most serious defect.
    """
    repo = _tree(tmp_path)
    (repo / "tests" / "test_new_skip.py").write_text(
        "import pytest\n\n\n"
        "def test_new():\n"
        "    pytest.skip('added under time pressure')\n",
        encoding="utf-8")
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    codes = _codes(proc)
    assert "manifest-unexpected-suppression" in codes, proc.stderr


def test_a_suppression_removed_from_the_tree_is_still_reported(tmp_path):
    """The record and the tree must AGREE, in both directions.

    Removing a suppression is the free direction for AUTHORIZATION -- no
    declaration is required -- and it is still drift: the record no longer
    describes the tree, and the fix is to regenerate. The two rules are
    independent and this is the case that keeps them apart.
    """
    repo = _tree(tmp_path)
    (repo / "tests" / "test_gated.py").write_text(
        "def test_gated():\n    assert True\n", encoding="utf-8")
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "manifest-missing-suppression" in _codes(proc), proc.stderr


# ---------------------------------------------------------------------------
# Authorization and inability
# ---------------------------------------------------------------------------


def test_an_uncovered_shrink_against_the_committed_parent_fails(tmp_path):
    """#648 acceptance criterion 7 -- authorization, enforced at the GATE.

    Both the source and its artifact row are removed in one change, which is
    exactly the merge resolution D3 exists for: the tree is internally
    consistent, the live comparison is clean, and only the committed parent
    remembers the row.
    """
    repo = _tree(tmp_path)
    doc = _artifact(repo)
    doc["pytestNodes"] = [n for n in doc["pytestNodes"]
                          if not n.startswith("tests/test_alpha.py")]
    _record(repo, doc)
    (repo / "tests" / "test_alpha.py").unlink()
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "estate-shrink-unauthorized" in _codes(proc), proc.stderr
    assert "tests/test_alpha.py::test_one" in proc.stderr


def test_a_declaration_in_the_tree_authorizes_that_same_shrink(tmp_path):
    """#648 D3a -- and the control arm for the case above.

    Without it the refusal could be a gate that refuses every shrink, which
    would be a gate nobody can use.
    """
    mod = _helper()
    repo = _tree(tmp_path)
    previous = _artifact(repo)
    doc = _artifact(repo)
    doc["pytestNodes"] = [n for n in doc["pytestNodes"]
                          if not n.startswith("tests/test_alpha.py")]
    _record(repo, doc)
    (repo / "tests" / "test_alpha.py").unlink()
    _write(repo / LEDGER, json.dumps({"schemaVersion": 1, "declarations": [{
        "id": "R-9001",
        "axis": "pytestNodes",
        "profile": "public",
        "cause": "retired",
        "reason": "the module was deleted with its subject",
        "rows": ["tests/test_alpha.py::test_one",
                 "tests/test_alpha.py::test_two"],
        "predecessorDigest": mod.active_set_digest(previous),
    }]}, indent=2) + "\n")
    proc = _drive(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_replayed_declaration_no_longer_authorizes_anything(tmp_path):
    """#648 acceptance criterion 10 -- a spent declaration is inert.

    The same entry is presented against a LATER predecessor, whose active set
    digests differently, so the binding that made it an authorization the first
    time is what refuses it the second.
    """
    mod = _helper()
    repo = _tree(tmp_path)
    original = _artifact(repo)
    doc = _artifact(repo)
    doc["pytestNodes"] = [n for n in doc["pytestNodes"]
                          if not n.startswith("tests/test_alpha.py")]
    _record(repo, doc)
    (repo / "tests" / "test_alpha.py").unlink()
    declaration = {
        "id": "R-9001",
        "axis": "pytestNodes",
        "profile": "public",
        "cause": "retired",
        "reason": "the module was deleted with its subject",
        "rows": ["tests/test_alpha.py::test_one",
                 "tests/test_alpha.py::test_two",
                 "tests/test_bench.py::test_bench"],
        "predecessorDigest": mod.active_set_digest(original),
    }
    _write(repo / LEDGER, json.dumps(
        {"schemaVersion": 1, "declarations": [declaration]}, indent=2) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "authorized shrink")

    # A second shrink, covered by the SAME entry's rows and refused anyway,
    # because the predecessor it is bound to is no longer the predecessor.
    doc = _artifact(repo)
    doc["pytestNodes"] = [n for n in doc["pytestNodes"]
                          if not n.startswith("tests/test_bench.py")]
    doc["pytestExecution"] = {"legs": [
        {"name": "benchmark", "selectors": ["tests/test_gated.py"]},
        {"name": "pytest", "selectors": [mod.COMPLEMENT_SELECTOR]},
    ]}
    _record(repo, doc)
    (repo / "tests" / "test_bench.py").unlink()
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "estate-shrink-unauthorized" in _codes(proc), proc.stderr


def _merge_that_drops_test_alpha(repo, ledger=None):
    """Build a repository whose HEAD is a MERGE that lost a test and its row.

    Both sides of the merge still record `tests/test_alpha.py`; the resolution
    committed here does not. The working tree is left CLEAN and identical to
    that merge commit, so the HEAD-against-working-tree comparison is empty and
    the only live comparison is the one against the two parents.
    """
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo / "side.txt", "s\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "side")
    _git(repo, "checkout", "-q", "-")
    _write(repo / "main.txt", "m\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "mainline")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    doc = _artifact(repo)
    doc["pytestNodes"] = [n for n in doc["pytestNodes"]
                          if not n.startswith("tests/test_alpha.py")]
    _record(repo, doc)
    (repo / "tests" / "test_alpha.py").unlink()
    if ledger is not None:
        _write(repo / LEDGER, json.dumps(ledger, indent=2) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "merge resolution that drops a test")
    assert _git(repo, "status", "--porcelain").stdout == "", (
        "the working tree must be clean, or the HEAD comparison would fire "
        "instead of the parent comparison this case is about")
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3, parents


def test_a_merge_commit_that_drops_a_row_from_both_parents_is_unauthorized(
    tmp_path,
):
    """#648 acceptance criterion 7, on the production path, at a MERGE.

    Section 4 of the spec bounds the authorization check to two situations, and
    this is the one the design was filed for: a resolution drops a source file
    and its artifact row together, so the merge commit agrees with itself and
    only its parents remember the row. The other production case removes both
    in the WORKING TREE, where HEAD is the predecessor; nothing until now built
    a repository whose HEAD is the merge.

    No drift code may appear. The tree and its record agree exactly -- that is
    what makes this a transition failure and not a comparison failure, and
    asserting their absence is what proves which check fired.
    """
    repo = _tree(tmp_path)
    _merge_that_drops_test_alpha(repo)
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    codes = _codes(proc)
    assert "estate-shrink-unauthorized" in codes, proc.stderr
    assert not {c for c in codes if c.startswith("manifest-")}, codes
    assert "tests/test_alpha.py::test_one" in proc.stderr


def test_a_declaration_committed_in_the_merge_authorizes_that_same_loss(tmp_path):
    """The control arm for the case above.

    Without it the refusal could be a gate that refuses every merge commit,
    which would refuse every merge this repository ever makes. The declaration
    is bound to the parents' active set, which both parents share because
    neither of them touched the artifact.
    """
    mod = _helper()
    repo = _tree(tmp_path)
    parent_digest = mod.active_set_digest(_artifact(repo))
    _merge_that_drops_test_alpha(repo, ledger={
        "schemaVersion": 1,
        "declarations": [{
            "id": "R-9002",
            "axis": "pytestNodes",
            "profile": "public",
            "cause": "retired",
            "reason": "the module was deleted on one side and the resolution "
                      "kept the deletion",
            "rows": ["tests/test_alpha.py::test_one",
                     "tests/test_alpha.py::test_two"],
            "predecessorDigest": parent_digest,
        }],
    })
    proc = _drive(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_derivation_that_cannot_be_completed_is_an_inability(tmp_path):
    """#648 D9 -- an underivable axis is never an empty one.

    The vitest binary is removed, which is the precondition the kernel refuses
    on rather than returning a smaller frontend set. The run reports the
    inability code and not a drift code, because the two demand different
    actions from the operator.
    """
    repo = _tree(tmp_path)
    (repo / "dashboard" / "web" / "node_modules" / ".bin" / "vitest").unlink()
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    codes = _codes(proc)
    assert "estate-discovery-failed" in codes, proc.stderr
    assert not [c for c in codes if c.startswith("manifest-")], codes


def test_an_unreadable_artifact_is_its_own_code(tmp_path):
    """#648 acceptance criterion 6."""
    repo = _tree(tmp_path)
    (repo / ARTIFACT).write_text("{ not json", encoding="utf-8")
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "estate-artifact-unreadable" in _codes(proc), proc.stderr


def test_an_overlay_on_a_public_profile_tree_is_a_partition_failure(tmp_path):
    """#648 acceptance criterion 16, third clause, on the production path."""
    mod = _helper()
    repo = _tree(tmp_path)
    doc = _artifact(repo)
    _write(repo / "tests" / "authoritative-estate.private.json", json.dumps({
        "schemaVersion": 1,
        "publicDigest": mod.active_set_digest(doc),
        "allowlistDigest": "x" * 64,
        "pytestNodes": {"additions": [], "removals": []},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [], "removals": []},
    }, indent=2) + "\n")
    proc = _drive(repo)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "estate-partition-invalid" in _codes(proc), proc.stderr


def test_no_environment_variable_turns_the_production_check_off(tmp_path):
    """#648 D11 -- the seams the fixture families use are not an escape hatch.

    Asserted by SOURCE inspection as well as by behaviour, because a variable
    that disabled the check would show up as a passing run and there is no
    finite set of names to try. The contract shell reads no variable at all on
    this path; the only variable near it is `CCTALLY_AUTHORITATIVE_RUN`, which
    grades an inability and never skips the check.
    """
    text = (BIN / "_lib-test-contract.sh").read_text(encoding="utf-8")
    start = text.index("contract_admit_estate() {")
    end = text.index("\n}\n", start)
    body = text[start:end]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "CCTALLY_" not in stripped or "CCTALLY_ESTATE" in stripped, line
    # And the aggregator's call site is gated only by the mode predicate.
    aggregator = RUNNER.read_text(encoding="utf-8")
    assert ('if contract_estate_check_required "$COVERAGE_MODE" '
            '"$ESTATE_CHECK_OPT_IN"; then\n'
            '    contract_admit_estate "$REPO_ROOT" "$CONTRACT_PROFILE"'
            ) in aggregator


@pytest.mark.parametrize("args", [(), ("--tier-fast", "HEAD")])
def test_the_real_checker_runs_in_every_mode_that_claims_completeness(
        tmp_path, args):
    """The mode contract, over the real checker rather than over a stub.

    `--tier-fast` needs an ownership map, which this tree does not carry, so the
    tier widens to the whole estate -- which is the correct behaviour and leaves
    the estate check exactly where this case wants it.
    """
    repo = _tree(tmp_path)
    doc = _artifact(repo)
    doc["pytestNodes"] = [n for n in doc["pytestNodes"]
                          if not n.endswith("::test_two")]
    _record(repo, doc)
    proc = _drive(repo, args=args)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "manifest-unexpected-pytest-node" in _codes(proc), proc.stderr
