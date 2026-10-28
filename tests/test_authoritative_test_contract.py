"""Mutation matrix for the authoritative-gate verdict contract (#529 S1).

Every case mutates ONE property of a known-green scratch estate and asserts
the exact exit code, failureClass and reason code. Each guard also has a
green control, because a test that only ever sees red cannot distinguish a
working guard from one that fails on everything.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_test_generation
import _temp_hygiene as _hyg
from tests.helpers import contract_tree as _ct

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
RUNNER = BIN / "cctally-test-all"
CONTRACT_LIB = BIN / "_lib-test-contract.sh"

DEFAULT_SUMMARY = "passed: 5   failed: 0\n"


def _run_lib(tmp_path, snippet, env=None):
    """Source the contract library in a scratch dir and run one snippet."""
    script = tmp_path / "drive.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        f'source "{CONTRACT_LIB}"\n' + snippet + "\n"
    )
    script.chmod(0o755)
    e = dict(os.environ)
    # #966(c), spec 4.3: the outer run's owner-only variables (its outcome
    # record, run id and evidence store) never reach the child, which would
    # otherwise write the PARENT run's outcome. Applied before `env`, so a test
    # may still name a child-local outcome destination.
    _lib_test_generation.scrub_owner_env(e)
    e.update(env or {})
    return subprocess.run(
        [str(script)], capture_output=True, text=True, env=e, timeout=60
    )


# --------------------------------------------------------------- scratch estate
#
# No capability-override environment seam exists on purpose: a bypass variable
# sitting next to the check it tests is exactly the hazard this session closes.
# Instead every case injects a scratch repository. REPO_ROOT derives from the
# aggregator's own location (bin/cctally-test-all), so copying the aggregator,
# the contract library and fake executable harnesses into a temp tree
# exercises the true entry point.


def _capability_rows(capabilities):
    """Capability dicts as rows of the library's capability table."""
    rows = []
    for row in capabilities or ():
        hard = row["hard"]
        hard = hard if isinstance(hard, str) else ("true" if hard else "false")
        rows.append((row["name"], row["probe"], hard, row.get("visibility", "")))
    return rows


def _estate(
    tmp_path,
    harnesses=("alpha",),
    modes=None,
    harness_output=None,
    harness_exit=None,
    harness_kill=None,
    harness_breaks_sidecar=(),
    capabilities=None,
    smoke_test=True,
    sentinel=False,
    harness_sleep=None,
):
    """A known-green scratch estate; each case mutates exactly one property.

    Since the test regime reset (Task 3.4) the aggregator DISCOVERS its
    harnesses, so the estate is the set of executable bin/cctally-*-test files
    and no manifest is written. The capability table is the library's own
    assignment, rewritten to ``capabilities`` (none by default) so no case
    depends on what the runner has installed.
    """
    repo = tmp_path / "estate"
    bindir = repo / "bin"
    testsdir = repo / "tests"
    testsdir.mkdir(parents=True)
    _ct.copy_aggregator(bindir, capabilities=_capability_rows(capabilities))

    # `reconcile` is the aggregator's last report row, so every estate carries
    # it as a real harness does.
    disk = list(harnesses) + ["reconcile"]

    modes = modes or {}
    harness_output = harness_output or {}
    harness_exit = harness_exit or {}
    harness_kill = harness_kill or {}
    harness_sleep = harness_sleep or {}

    for name in disk:
        path = bindir / f"cctally-{name}-test"
        body = ["#!/usr/bin/env bash"]
        if sentinel:
            # POSITIVE evidence of execution. Only a harness that really ran
            # creates its marker, so a subset assertion built on these markers
            # cannot be satisfied by a run that executed nothing at all —
            # which is precisely what an absence-only assertion would allow.
            body.append(
                f'[ -n "${{SENTINEL_DIR:-}}" ] && : > "$SENTINEL_DIR/{name}.ran"'
            )
        if name in harness_breaks_sidecar:
            # Occupy the sidecar path with a DIRECTORY so run_one's
            # `echo "$rc" > "$LOGDIR/<name>.exit"` cannot create a readable
            # file. That is a real pool-machinery failure: the status was
            # never recorded, so the harness cannot be judged on what it
            # printed.
            body.append(f'mkdir -p "$LOGDIR/{name}.exit"')
        if name in harness_sleep:
            # Wall time a case can rely on: `$SECONDS` is an integer, so a run
            # that finishes inside one second records a zero numerator.
            body.append(f"sleep {harness_sleep[name]}")
        if name in harness_kill:
            body.append(f"kill -{harness_kill[name]} $$")
            body.append("sleep 5")
        else:
            out = harness_output.get(name, DEFAULT_SUMMARY)
            body.append("cat <<'SUMMARY'")
            body.append(out.rstrip("\n"))
            body.append("SUMMARY")
            body.append(f"exit {harness_exit.get(name, 0)}")
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        path.chmod(modes.get(name, 0o755))

    # A trivial passing pytest estate: the aggregator's bulk leg runs
    # `python3 -m pytest tests/` in the scratch root, and an empty tests/ dir
    # would exit 5 (nothing collected) on every case. `smoke_test=False` is how
    # a case reproduces exactly that exit-5 shape.
    if smoke_test:
        (testsdir / "test_scratch_smoke.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )
    return repo


def _drive(est, env=None, args=(), timeout=180, interpreter=None, reclaim=True):
    outcome = est / "outcome.json"
    if outcome.exists():
        outcome.unlink()
    # #963 J4: the child's TMPDIR lies inside this test's allocation, so its
    # suite directory lies inside the outer run's; any the child could not
    # remove is reclaimed below once the results are read.
    # The child-local outcome destination is an explicit override, applied
    # after the builder's owner-only scrub (spec 4.3).
    e = _hyg.child_aggregator_env({
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(est)),
        "CCTALLY_TEST_JOBS": "1",
    }, est.parent)
    e["CCTALLY_TEST_ALL_OUTCOME_FILE"] = str(outcome)
    e.update(env or {})
    argv = [str(est / "bin" / "cctally-test-all"), *args]
    # `interpreter` runs the aggregator under a named bash rather than the one
    # its shebang resolves to, which is how a bash-3.2-only defect is observed
    # on a host whose PATH bash is 5.x.
    if interpreter is not None:
        argv.insert(0, interpreter)
    proc = subprocess.run(
        argv,
        env=e,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    proc.outcome_path = outcome  # type: ignore[attr-defined]
    if reclaim:
        _hyg.reclaim_child_suite_dirs(est.parent)
    return proc


def _outcome(proc):
    path = proc.outcome_path  # type: ignore[attr-defined]
    assert path.exists(), (
        "no outcome record was written\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _codes(proc):
    return {x["code"] for x in _outcome(proc)["reasons"]}


# ------------------------------------------------------------------- admission


def test_a_harness_that_lost_its_executable_bit_is_a_usage_error(tmp_path):
    """F27, under discovery (Codex Q10): a lost mode bit must never look like
    an absent harness, so a matching file that is not executable refuses the
    run as usage before any harness executes."""
    est = _estate(tmp_path, harnesses=["alpha", "beta"], modes={"alpha": 0o644},
                  sentinel=True)
    ran = tmp_path / "ran"
    ran.mkdir()
    r = _drive(est, env={"SENTINEL_DIR": str(ran)})
    assert r.returncode == 2, r.stdout + r.stderr
    usage = [x for x in _outcome(r)["reasons"] if x["code"] == "aggregator-usage-error"]
    assert [x["subject"] for x in usage] == ["cctally-alpha-test"], usage
    assert "not executable" in r.stderr, r.stderr
    assert not list(ran.iterdir())


def test_an_admission_refusal_launches_nothing_and_keeps_no_suite_dir(tmp_path):
    """#963 R31: a refusal launches no harness and creates no log directory.
    The suite directory made before admission is removed at exit too (test
    regime reset, session 4): it recorded no process group, so its reap
    succeeds and nothing is kept."""
    est = _estate(tmp_path, sentinel=True)
    ran = tmp_path / "ran"
    ran.mkdir()
    r = _drive(est, env={"REGEN": "1", "SENTINEL_DIR": str(ran)}, reclaim=False)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "regeneration-enabled" in _codes(r)
    assert not list(ran.iterdir())
    suites = sorted(p for p in (tmp_path / "child-tmp").iterdir() if p.name.startswith("cs."))
    assert suites == [], suites


def test_unmutated_estate_is_green(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha"])
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "none"
    assert out["outcome"] == "pass"
    assert out["schemaVersion"] == 1
    assert out["exitCode"] == 0
    assert out["reasons"] == []


# ----------------------------------------------------------------- capabilities
#
# The authoritative marker CCTALLY_AUTHORITATIVE_RUN=1 is exported by the
# runner supervisor (bin/_lib_runner_host.py) for the canonical command. Setting it can only make a
# run STRICTER; unsetting it degrades a refusal to an `incomplete`
# classification and never to green, so it is safe by construction and needs no
# anti-tamper guard.

REAL_CAPABILITIES = [
    {"name": "pytest", "probe": "pytest", "hard": True},
    {"name": "fts5", "probe": "fts5", "hard": True},
    {"name": "pytest-timeout", "probe": "python-import:pytest_timeout",
     "hard": True},
    {"name": "rich", "probe": "python-import:rich", "hard": True},
    {"name": "node", "probe": "node", "hard": True},
    {"name": "pytest-xdist", "probe": "python-import:xdist", "hard": False},
]

CAP_BY_NAME = {c["name"]: c for c in REAL_CAPABILITIES}


def _shim(est, break_capability):
    """A PATH shim that breaks exactly ONE probe and delegates everything else.

    The contract library itself runs `python3 -c` to encode the outcome
    record, so a shim that failed every form would break the fixture rather
    than the probe under test.
    """
    shim = est / "shim"
    shim.mkdir(exist_ok=True)
    py = shim / "python3"
    py.write_text(
        "#!/usr/bin/env bash\n"
        f'BREAK={break_capability}\n'
        'case "${1:-}" in\n'
        '  -m) if [ "${2:-}" = "pytest" ] && [ "$BREAK" = "pytest" ]; then exit 1; fi ;;\n'
        '  -c) case "${2:-}" in\n'
        '        *fts5*) if [ "$BREAK" = "fts5" ]; then exit 1; fi ;;\n'
        '        "import pytest_timeout") if [ "$BREAK" = "pytest_timeout" ]; then exit 1; fi ;;\n'
        '        "import rich") if [ "$BREAK" = "rich" ]; then exit 1; fi ;;\n'
        '        "import xdist") if [ "$BREAK" = "xdist" ]; then exit 1; fi ;;\n'
        '      esac ;;\n'
        'esac\n'
        f'exec {shutil.which("python3")} "$@"\n',
        encoding="utf-8",
    )
    py.chmod(0o755)
    if break_capability == "node":
        for tool in ("node", "npm"):
            p = shim / tool
            p.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            p.chmod(0o755)
    return shim


def _drive_with_shim(est, break_capability, env=None):
    shim = _shim(est, break_capability)
    e = {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}
    e.update(env or {})
    return _drive(est, env=e)


@pytest.mark.parametrize(
    "cap,shim",
    [
        ("pytest", "pytest"),
        ("fts5", "fts5"),
        ("pytest-timeout", "pytest_timeout"),
        ("rich", "rich"),
        ("node", "node"),
    ],
)
def test_hard_capability_missing_refuses_an_authoritative_run(tmp_path, cap, shim):
    est = _estate(tmp_path, capabilities=[CAP_BY_NAME[cap]])
    r = _drive_with_shim(est, shim, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure"
    assert any(
        x["code"] == "capability-missing" and x["subject"] == cap
        for x in out["reasons"]
    ), out["reasons"]
    assert out["capabilities"][cap] is False


@pytest.mark.parametrize(
    "cap,shim",
    [("pytest", "pytest"), ("fts5", "fts5"), ("pytest-timeout", "pytest_timeout")],
)
def test_hard_capability_missing_is_incomplete_when_not_authoritative(
    tmp_path, cap, shim
):
    est = _estate(tmp_path, capabilities=[CAP_BY_NAME[cap]])
    r = _drive_with_shim(est, shim, env={"CCTALLY_AUTHORITATIVE_RUN": ""})
    assert r.returncode == 3, r.stdout + r.stderr
    # Never green, so "no waiver is a pass" holds — but not an infrastructure
    # refusal either, because tests/requirements-dev.txt documents
    # pytest-timeout as optional-by-detection for local runs.
    assert _outcome(r)["failureClass"] == "incomplete"


def test_xdist_absence_is_recorded_and_not_fatal(tmp_path):
    est = _estate(tmp_path, capabilities=[CAP_BY_NAME["pytest-xdist"]])
    r = _drive_with_shim(est, "xdist", env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _outcome(r)["capabilities"]["pytest-xdist"] is False


def test_every_capability_present_is_green_and_recorded(tmp_path):
    """The green control: the same probes, unbroken, must pass and be recorded
    true — a guard that only ever sees red cannot be distinguished from one
    that fails on everything."""
    est = _estate(tmp_path, capabilities=REAL_CAPABILITIES)
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    caps = _outcome(r)["capabilities"]
    for row in REAL_CAPABILITIES:
        assert caps[row["name"]] is True, (row["name"], caps)


def test_agentmem_is_asserted_only_under_the_required_policy(tmp_path):
    """`agentmem` is asserted against the EFFECTIVE policy, not hardcoded.
    The runner supervisor pins `required` for every remote execution."""
    row = {
        "name": "agentmem",
        "probe": "command:definitely-not-a-real-binary-529",
        "hard": "policy",
    }
    est = _estate(tmp_path, capabilities=[row])
    lenient = _drive(
        est,
        env={
            "CCTALLY_AUTHORITATIVE_RUN": "1",
            "CCTALLY_AGENTMEM_TEST_POLICY": "hosted-private-unavailable",
        },
    )
    assert lenient.returncode == 0, lenient.stdout + lenient.stderr
    assert _outcome(lenient)["capabilities"]["agentmem"] is False

    strict = _drive(
        est,
        env={
            "CCTALLY_AUTHORITATIVE_RUN": "1",
            "CCTALLY_AGENTMEM_TEST_POLICY": "required",
        },
    )
    assert strict.returncode == 3, strict.stdout + strict.stderr
    assert "capability-missing" in _codes(strict)


# ------------------------------------------------------------------ regeneration

FORBIDDEN = [
    "CCTALLY_REGEN_GOLDENS",
    "REGEN",
    "HARNESS_REGEN",
    "CCTALLY_DOCTOR_REGENERATE",
    "CCTALLY_MIGRATIONS_REGENERATE",
    "CCTALLY_SETUP_REGENERATE",
    "CCTALLY_PRICING_CHECK_REGENERATE",
    "CCTALLY_UPDATE_TEST_REGEN",
]


def _lib_constant(tmp_path, name):
    r = _run_lib(tmp_path, f'printf "%s" "${name}"')
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_the_library_declares_every_regeneration_variable(tmp_path):
    """Hard-coded here rather than read back, so the tripwire cannot be
    satisfied by editing the thing it guards. The runner client's own guard
    (bin/_lib_runner_client.py) reads the same list (Codex Q10)."""
    declared = _lib_constant(tmp_path, "CONTRACT_REGENERATION_VARIABLES").split()
    assert sorted(declared) == sorted(FORBIDDEN)


@pytest.mark.parametrize("var, value", [
    *((var, "1") for var in FORBIDDEN),
    *(("REGEN", value) for value in ("0", "true", "")),
])
def test_any_regeneration_variable_refuses_the_run(tmp_path, var, value):
    """Presence is poison. Deliberately stricter than each variable's own
    `= 1` activation predicate: REGEN=0 does nothing today, and REGEN=true
    looks like it does something while doing nothing. Every declared
    variable is refused when set; the values that do not activate it (and the
    empty string, which a `-n` test would miss) run on one variable, because
    the guard tests presence with one loop over the declared list."""
    est = _estate(tmp_path)
    r = _drive(est, env={var: value, "CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure"
    assert any(
        x["code"] == "regeneration-enabled" and x["subject"] == var
        for x in out["reasons"]
    ), out["reasons"]
    assert var in r.stderr


HARD_CAPABILITIES = {
    "pytest": True,
    "fts5": True,
    "pytest-timeout": True,
    "rich": True,
    "node": True,
}


def test_the_library_declares_every_hard_capability(tmp_path):
    """An emptied capability table would disable every probe in silence,
    exactly as an emptied regeneration list would. The names and their
    hardness are hard-coded here rather than read from the library, so the
    tripwire cannot be satisfied by editing the thing it guards."""
    table = _lib_constant(tmp_path, "CONTRACT_CAPABILITY_TABLE")
    declared = {}
    for line in table.splitlines():
        if line:
            name, _probe, hard, *_rest = line.split("\t")
            declared[name] = hard
    for name, hard in HARD_CAPABILITIES.items():
        assert declared.get(name) == ("true" if hard else "false"), (name, declared)
    # pytest-xdist is recorded and never required: it is a speed knob and
    # changes nothing about what was verified.
    assert declared.get("pytest-xdist") == "false", declared
    assert declared.get("agentmem") == "policy", declared


def test_a_clean_environment_is_the_green_control(tmp_path):
    """The same eight variables declared, none set: the guard must pass."""
    est = _estate(tmp_path)
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "regeneration-enabled" not in _codes(r)


def test_regeneration_is_refused_even_without_the_authoritative_marker(tmp_path):
    """The guard is not scoped to the authoritative profile: an adopted golden
    is a wrong artifact on any lane."""
    est = _estate(tmp_path)
    r = _drive(est, env={"REGEN": "1"})
    assert r.returncode == 3, r.stdout + r.stderr
    assert _outcome(r)["failureClass"] == "infrastructure"


def test_bench_update_baseline_refuses_under_the_authoritative_marker(tmp_path):
    """`--update-baseline` is a flag, not an inheritable variable, so it is
    handled separately: it refuses inside an authoritative run and ordinary
    direct operator use stays valid."""
    env = dict(os.environ)
    env["CCTALLY_AUTHORITATIVE_RUN"] = "1"
    r = subprocess.run(
        [str(BIN / "cctally-bench"), "--update-baseline"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert "--update-baseline" in r.stderr


# ------------------------------------------------------- harness classification
#
# The exit status alone decides (Codex Q10): 0 passes, 1 is a product failure,
# a signal is infrastructure and any other status is incomplete. The summary
# line feeds the report row and the metric only.


def _pytest_rc_shim(est, rc, output=""):
    """A python3 on PATH whose BULK pytest run exits with `rc`. `--version`
    still succeeds, so the capability probe is unaffected and the run reaches
    phase 3."""
    shim = est / "shim"
    shim.mkdir(exist_ok=True)
    py = shim / "python3"
    py.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pytest" ]; then\n'
        '  if [ "${3:-}" = "--version" ]; then exit 0; fi\n'
        f"  printf '%s\\n' {output!r}\n"
        f"  exit {rc}\n"
        "fi\n"
        f'exec {shutil.which("python3")} "$@"\n',
        encoding="utf-8",
    )
    py.chmod(0o755)
    return {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}


def test_three_space_summary_variant_is_accepted(tmp_path):
    est = _estate(tmp_path, harness_output={"alpha": "passed: 5   failed: 0\n"})
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _outcome(r)["totals"]["passed"] >= 5


def test_two_space_summary_variant_is_accepted(tmp_path):
    """bin/cctally-rederive-test:553 prints two spaces; the rest print three.
    A literal wire format would break it, contradicting "no harness edited"."""
    est = _estate(tmp_path, harness_output={"alpha": "passed: 5  failed: 0\n"})
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _outcome(r)["failureClass"] == "none"


def test_killed_harness_classifies_as_killed_not_as_missing_summary(tmp_path):
    est = _estate(tmp_path, harness_kill={"alpha": 9})
    r = _drive(est)
    out = _outcome(r)
    codes = {x["code"] for x in out["reasons"]}
    assert codes == {"harness-killed"}, out["reasons"]  # causally prior override
    assert out["failureClass"] == "infrastructure"
    assert r.returncode == 3


def test_absent_exit_sidecar_is_pool_machinery_not_a_summary_problem(tmp_path):
    est = _estate(tmp_path, harness_breaks_sidecar=("alpha",))
    r = _drive(est)
    out = _outcome(r)
    codes = {x["code"] for x in out["reasons"]}
    assert codes == {"pool-machinery-failed"}, out["reasons"]
    assert out["failureClass"] == "infrastructure"
    assert r.returncode == 3


def test_a_zero_exit_passes_whatever_the_summary_says(tmp_path):
    """The summary is a diagnostic, never a verdict: a harness that exits 0
    while printing failures passes, and its counts still reach the totals."""
    est = _estate(
        tmp_path,
        harness_output={"alpha": "passed: 3   failed: 2\n"},
        harness_exit={"alpha": 0},
    )
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "none", out["reasons"]
    assert out["totals"]["failed"] == 2, out["totals"]


@pytest.mark.parametrize("output", [
    "passed: 4   failed: 0\n",   # a clean summary
    "passed: 0   failed: 0\n",   # bin/cctally-doc-lint-test's self_check_fail shape
    "",                          # no summary at all
])
def test_a_status_other_than_zero_or_one_is_incomplete(tmp_path, output):
    est = _estate(tmp_path, harness_output={"alpha": output},
                  harness_exit={"alpha": 2})
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "incomplete"
    assert {(x["code"], x["subject"]) for x in out["reasons"]} == {
        ("harness-incomplete", "alpha")}, out["reasons"]


@pytest.mark.parametrize(
    "output", ["", "passed: x   failed: y\n", "passed:   failed:\n", "all good\n"]
)
def test_a_missing_or_malformed_summary_does_not_decide(tmp_path, output):
    est = _estate(tmp_path, harness_output={"alpha": output})
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _outcome(r)["failureClass"] == "none"


def test_a_binary_harness_log_is_judged_by_its_status(tmp_path):
    """A NUL-bearing log is not searched for a summary (BSD and GNU grep answer
    differently for it), and must not abort bash's arithmetic either: the
    status decides and the outcome is emitted."""
    est = _estate(tmp_path)
    alpha = est / "bin" / "cctally-alpha-test"
    alpha.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'passed: 5   failed: 0\\0\\n'\n",
        encoding="utf-8",
    )
    alpha.chmod(0o755)
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "none", out
    assert out["exitCode"] == 0, out
    assert "arithmetic syntax error" not in r.stderr


def test_a_genuine_test_failure_is_product_and_exits_one(tmp_path):
    est = _estate(
        tmp_path,
        harness_output={"alpha": "passed: 3   failed: 2\n"},
        harness_exit={"alpha": 1},
    )
    r = _drive(est)
    assert r.returncode == 1, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "product"
    assert any(
        x["code"] == "harness-failed" and x["subject"] == "alpha"
        for x in out["reasons"]
    ), out["reasons"]


def test_multi_cause_run_retains_every_reason(tmp_path):
    est = _estate(
        tmp_path,
        harnesses=["alpha", "beta"],
        harness_output={"alpha": "passed: 3   failed: 1\n"},
        harness_exit={"alpha": 1, "beta": 2},
    )
    r = _drive(est)
    out = _outcome(r)
    assert out["failureClass"] == "incomplete"  # precedence
    codes = {x["code"] for x in out["reasons"]}
    assert codes == {"harness-failed", "harness-incomplete"}, out["reasons"]
    assert r.returncode == 3


# ----------------------------------------------------------------- pytest phase


def test_pytest_failure_with_a_node_id_is_product(tmp_path):
    est = _estate(tmp_path)
    (est / "tests" / "test_scratch_smoke.py").write_text(
        "def test_bad():\n    assert False\n", encoding="utf-8"
    )
    r = _drive(est)
    assert r.returncode == 1, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "product"
    assert "pytest-failed" in {x["code"] for x in out["reasons"]}


def test_pytest_status_one_without_a_node_id_is_infrastructure(tmp_path):
    """An xdist worker crash can exit 1 while reporting no failed test node.

    Calling that a product failure sends the maintainer looking for a test that
    the run never identified and hides the retry-worthy infrastructure shape.
    """
    est = _estate(tmp_path)
    r = _drive(est, env=_pytest_rc_shim(est, 1, "FAILED worker::crashed"))
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure"
    codes = {x["code"] for x in out["reasons"]}
    assert "pytest-no-failing-node-ids" in codes
    assert "pytest-failed" not in codes


def test_pytest_collecting_nothing_is_incomplete(tmp_path):
    """Exit 5 means the estate was not verified at all — the exact shape of a
    silently empty run."""
    est = _estate(tmp_path, smoke_test=False)
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "incomplete"
    assert "pytest-collected-nothing" in {x["code"] for x in out["reasons"]}


@pytest.mark.parametrize("rc", [2, 3, 4])
def test_pytest_internal_statuses_are_infrastructure(tmp_path, rc):
    est = _estate(tmp_path)
    r = _drive(est, env=_pytest_rc_shim(est, rc))
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure"
    assert "pytest-internal" in {x["code"] for x in out["reasons"]}


def test_the_benchmark_leg_is_inside_the_verdict(tmp_path):
    """Merge 6dffa5283 split the wall-clock rebuild benchmark into its own
    pytest invocation. Isolating it must not put it outside the verdict."""
    est = _estate(tmp_path)
    (est / "tests" / "test_rebuild_benchmark.py").write_text(
        "def test_benchmark():\n    assert False\n", encoding="utf-8"
    )
    r = _drive(est)
    assert r.returncode == 1, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "product"
    assert any(
        x["code"] == "pytest-failed" and x["subject"] == "benchmark"
        for x in out["reasons"]
    ), out["reasons"]


# ------------------------------------------------ the pytest passed-item count
#
# The count is the denominator half of the normalized metric (#529 S5 §4.6).
# It is additive: the passed-item count changes no classification. The
# independent node-id shape can refine status 1 into an infrastructure failure.


def test_the_pytest_passed_item_count_is_recorded(tmp_path):
    """The scratch estate carries exactly one passing pytest item."""
    est = _estate(tmp_path)
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    out = _outcome(r)
    assert out["pytestPassed"] == 1, out


def test_the_pytest_passed_item_count_is_recorded_on_a_failing_run(tmp_path):
    """A product failure in the shell pool must not suppress the count: the
    metric is recorded on failed runs too."""
    est = _estate(
        tmp_path,
        harness_output={"alpha": "passed: 3   failed: 2\n"},
        harness_exit={"alpha": 1},
    )
    r = _drive(est)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _outcome(r)["pytestPassed"] == 1, _outcome(r)


def test_the_pytest_passed_count_defaults_to_zero_when_pytest_never_ran(tmp_path):
    """An admission refusal never reaches the pytest phase, so the count is 0
    rather than absent — a null denominator must be a number, not a hole."""
    est = _estate(tmp_path)
    r = _drive(est, env={"REGEN": "1"})
    assert r.returncode == 3, r.stdout + r.stderr
    assert _outcome(r)["pytestPassed"] == 0


# ---------------------------------------------- a count that could not be read
#
# "Not parsed" and "parsed zero" are different answers and must not collapse.
# A lost pytest count does not zero the denominator — the shell half is non-zero
# on any real run — it HALVES it, and a halved denominator roughly doubles the
# recorded metric while the record still looks well formed. So a count that
# could not be read records null, and a pytest run that legitimately passed
# nothing still records 0.


def _pytest_output_shim(est, payload_expr, rc=0):
    """A python3 on PATH whose BULK pytest run writes `payload_expr` (a Python
    bytes expression) to stdout and exits `rc`. `--version` still succeeds, so
    the capability probe is unaffected and the run reaches phase 3."""
    shim = est / "shim"
    shim.mkdir(exist_ok=True)
    real = shutil.which("python3")
    py = shim / "python3"
    py.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pytest" ]; then\n'
        '  if [ "${3:-}" = "--version" ]; then exit 0; fi\n'
        f"  {real} -c 'import sys; sys.stdout.buffer.write({payload_expr})'\n"
        f"  exit {rc}\n"
        "fi\n"
        f'exec {real} "$@"\n',
        encoding="utf-8",
    )
    py.chmod(0o755)
    return {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}


def test_a_binary_pytest_log_still_yields_its_real_passed_count(tmp_path):
    """A log carrying a NUL byte is the shape `grep` answers with `Binary file
    … matches` — a non-numeric answer that used to flatten to 0 and silently
    halve the denominator. The count is read from the summary line regardless."""
    est = _estate(tmp_path)
    r = _drive(
        est,
        env=_pytest_output_shim(
            est, r'b"noise\x00noise\n5 passed in 0.01s\n"'
        ),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    out = _outcome(r)
    assert out["pytestPassed"] == 5, out
    assert out["passedCases"] == 15, out


def test_a_pytest_log_with_no_summary_records_null_not_zero(tmp_path):
    """pytest closes every run it reached the end of with a summary line. A log
    without one — an internal error, a truncated write — carries no count to
    read, and the record must say so rather than report a metric computed from
    a denominator missing pytest's half."""
    est = _estate(tmp_path)
    r = _drive(
        est,
        env=_pytest_output_shim(
            est, r'b"INTERNALERROR> RuntimeError: boom\n"', rc=3
        ),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["pytestPassed"] is None, out
    assert out["passedCases"] is None, out
    assert out["secondsPerThousandCases"] is None, out
    # The degradation is announced, not silent.
    assert "passed-item count could not be read" in r.stderr, r.stderr
    # The classification is untouched by the count: rc 3 is still internal.
    assert "pytest-internal" in _codes(r)


def test_a_pytest_run_that_legitimately_passed_nothing_records_zero(tmp_path):
    """The discriminating control for the case above. An all-skipped run has a
    summary line with no `passed` in it at all, which is a genuine zero and must
    not be recorded as an unreadable count."""
    est = _estate(tmp_path)
    (est / "tests" / "test_scratch_smoke.py").write_text(
        "import pytest\n\n\ndef test_skipped():\n    pytest.skip('deliberate')\n",
        encoding="utf-8",
    )
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    out = _outcome(r)
    assert out["pytestPassed"] == 0, out
    assert out["passedCases"] == 10, out
    assert out["secondsPerThousandCases"] is not None, out
    assert "passed-item count could not be read" not in r.stderr, r.stderr


# ------------------------------------------------------- the normalized metric
#
# Wall-seconds per thousand passed cases (#529 S5 §4.6). The denominator is the
# sum of every shell harness's passed count and pytest's passed-item count. It
# is deliberately NOT called assertions: shell values are harness cases and
# pytest's are items, so "assertions" would mislabel both.


def test_the_metric_is_wall_seconds_per_thousand_passed_cases(tmp_path):
    est = _estate(tmp_path)
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    out = _outcome(r)
    assert out["passedCases"] == out["totals"]["passed"] + out["pytestPassed"]
    assert out["passedCases"] > 0, out
    assert out["secondsPerThousandCases"] == round(
        out["wallSeconds"] / out["passedCases"] * 1000, 1
    ), out


def test_the_metric_is_null_not_zero_on_a_zero_denominator(tmp_path):
    """An admission refusal passes nothing at all. A zero denominator records a
    null metric rather than dividing, and null is not the same claim as 0.0."""
    est = _estate(tmp_path)
    r = _drive(est, env={"REGEN": "1"})
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["passedCases"] == 0, out
    assert out["secondsPerThousandCases"] is None, out


def test_the_metric_is_recorded_on_a_failed_run(tmp_path):
    """A run is not exempt from measurement because it went red — comparing a
    failed run's cost is exactly what the A/B needs."""
    est = _estate(
        tmp_path,
        harness_output={"alpha": "passed: 3   failed: 2\n"},
        harness_exit={"alpha": 1},
    )
    r = _drive(est)
    assert r.returncode == 1, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "product"
    assert out["passedCases"] > 0, out
    assert out["secondsPerThousandCases"] == round(
        out["wallSeconds"] / out["passedCases"] * 1000, 1
    ), out


def test_the_record_carries_the_effective_budget(tmp_path):
    """The A/B compares two runs that differ only in the pytest worker count,
    so the tuple a record describes has to be readable from the record."""
    est = _estate(tmp_path)
    r = _drive(
        est,
        env={
            "CCTALLY_OUTER_JOBS": "3",
            "CCTALLY_INNER_JOBS": "2",
            "CCTALLY_PYTEST_JOBS": "5",
        },
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert _outcome(r)["budget"] == {"outer": 3, "inner": 2, "pytest": 5}


def test_the_retained_manifest_carries_the_metric_and_its_terms(tmp_path):
    """§4.6's second record extension. The retained manifest already recorded
    the budget and the per-phase wall times; the metric joins them so one
    artifact carries numerator and denominator together."""
    root = tmp_path / "ev"
    est = _estate(tmp_path)
    r = _drive(est, env=_evidence_env(root, "metric-manifest"))
    assert r.returncode == 0, r.stdout + r.stderr
    manifest = json.loads(
        (root / "local" / "metric-manifest" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    outcome = _outcome(r)
    # The scratch estate is fully determined, so the denominator has ONE
    # correct value: two harnesses (alpha and reconcile) each printing
    # DEFAULT_SUMMARY's `passed: 5`, plus the single passing item in
    # tests/test_scratch_smoke.py. Asserting the exact number is what lets this
    # test see a denominator that dropped pytest's half; recomputing the metric
    # from the manifest's own two terms agrees with itself either way.
    assert manifest["passedCases"] == 11, manifest
    # The two artifacts constrain each other rather than each being internally
    # consistent about a different number.
    assert manifest["passedCases"] == outcome["passedCases"], (manifest, outcome)
    assert manifest["secondsPerThousandCases"] == round(
        manifest["phaseWallSeconds"]["total"] / manifest["passedCases"] * 1000, 1
    ), manifest
    assert manifest["budget"] == {"outer": 1, "inner": 1, "pytest": 1}, manifest


def test_both_artifacts_report_one_run_as_one_wall_time(tmp_path):
    """The outcome record samples the run's wall clock at the verdict and the
    retained manifest is written afterwards from the EXIT trap. Sampling
    independently let one run publish two different metrics for itself."""
    root = tmp_path / "ev"
    est = _estate(tmp_path)
    r = _drive(est, env=_evidence_env(root, "wall-agreement"))
    assert r.returncode == 0, r.stdout + r.stderr
    manifest = json.loads(
        (root / "local" / "wall-agreement" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    outcome = _outcome(r)
    assert manifest["phaseWallSeconds"]["total"] == outcome["wallSeconds"], (
        manifest,
        outcome,
    )
    assert (
        manifest["secondsPerThousandCases"] == outcome["secondsPerThousandCases"]
    ), (manifest, outcome)


# --------------------------------------------------- outcome schema + seams

VALID_CLASSES = {"none", "product", "infrastructure", "incomplete"}
# The aggregator's phase set is CLOSED. `transport` appears only in objects the
# retired remote wrapper's carrier synthesized (removed in session 4, Task 4.10).
VALID_PHASES = {"admission", "harness", "pytest"}


def test_every_contract_fail_phase_is_a_declared_phase():
    """Every reason bin/cctally-test-all emits through contract_fail names a
    phase of the closed set (#963 PX1, the twin of G1): a reader of the record
    classifies by phase, so a new phase is a schema change, not a new value."""
    phases = VALID_PHASES
    calls = []
    for number, line in enumerate((BIN / "cctally-test-all").read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for _cls, code, _subject, phase in re.findall(
                r'contract_fail\s+(\S+)\s+(\S+)\s+("[^"]*"|\S+)\s+([^\s;]+)', line):
            calls.append((number, code, phase))
    assert len(calls) >= 10, calls
    outside = [call for call in calls if call[2] not in phases]
    assert not outside, outside


def test_outcome_object_shape(tmp_path):
    est = _estate(tmp_path)
    r = _drive(est)
    out = _outcome(r)
    assert out["schemaVersion"] == 1
    assert set(out) >= {
        "schemaVersion", "outcome", "failureClass", "exitCode", "reasons"
    }
    assert out["outcome"] in {"pass", "fail"}
    assert out["failureClass"] in VALID_CLASSES
    assert out["exitCode"] == r.returncode
    assert isinstance(out["reasons"], list)


@pytest.mark.parametrize(
    "kwargs,expect_class,expect_code",
    [
        ({}, "none", 0),
        (
            {
                "harness_output": {"alpha": "passed: 1   failed: 1\n"},
                "harness_exit": {"alpha": 1},
            },
            "product",
            1,
        ),
        ({"harness_exit": {"alpha": 2}}, "incomplete", 3),
        ({"harness_kill": {"alpha": 9}}, "infrastructure", 3),
    ],
)
def test_the_exit_band_matches_the_recorded_class(
    tmp_path, kwargs, expect_class, expect_code
):
    est = _estate(tmp_path, **kwargs)
    r = _drive(est)
    out = _outcome(r)
    assert out["failureClass"] == expect_class, out["reasons"]
    assert r.returncode == expect_code
    assert out["exitCode"] == r.returncode
    # The aggregator never emits 75 — that is the wrapper's "still running".
    assert r.returncode != 75


def test_every_reason_uses_the_declared_vocabulary(tmp_path):
    est = _estate(
        tmp_path,
        harnesses=["alpha", "beta"],
        harness_output={"alpha": "passed: 1   failed: 1\n"},
        harness_exit={"alpha": 1, "beta": 2},
    )
    out = _outcome(_drive(est))
    for reason in out["reasons"]:
        assert reason["phase"] in VALID_PHASES, reason
        assert set(reason) == {"code", "phase", "subject"}, reason
    # `invalid` was Codex's name for the third class; the maintainer chose
    # `incomplete`. The vocabulary must never regress to it.
    assert out["failureClass"] != "invalid"


# ------------------------------------------------------- the reason-code registry
#
# Codes are bare string literals at each emission site, so nothing in the shell
# can catch a typo. The registry in bin/_lib-test-contract.sh is the declared
# vocabulary and this pair of assertions makes it load-bearing in both
# directions.

_EMISSION_PATTERNS = (
    r"contract_fail\s+\w+\s+([a-z][a-z0-9-]+)",
)

_CODE_SOURCES = (
    "bin/_lib-test-contract.sh",
    "bin/cctally-test-all",
    # Plus every kernel the aggregator and the wrapper delegate to, matched as
    # a CLASS rather than listed one at a time (#529 S2, spec section 7).
    # Neither #529 S2 kernel emits a reason code today, so this has no live
    # effect — which is exactly why it has to be here now: the two-directional
    # assertion below goes blind the moment emission moves into them, and a
    # blind assertion looks identical to a passing one.
) + tuple(
    sorted(f"bin/{path.name}" for path in BIN.glob("_lib_test_*.py"))
)
if (REPO / "bin" / "cctally-test-remote").exists():
    _CODE_SOURCES += ("bin/cctally-test-remote",)


def _registry():
    import re

    text = (BIN / "_lib-test-contract.sh").read_text(encoding="utf-8")
    body = re.search(r"_CONTRACT_REASON_CODES='\n(.*?)\n'", text, re.S)
    assert body, "bin/_lib-test-contract.sh declares no _CONTRACT_REASON_CODES"
    return set(body.group(1).split())


def _emitted_codes():
    import re

    found = set()
    for rel in _CODE_SOURCES:
        path = REPO / rel
        if not path.exists():
            continue  # bin/cctally-test-remote is maintainer-local
        text = path.read_text(encoding="utf-8")
        for pattern in _EMISSION_PATTERNS:
            found.update(re.findall(pattern, text))
    return found


def test_every_emitted_reason_code_is_in_the_registry():
    unknown = _emitted_codes() - _registry()
    assert not unknown, (
        f"these reason codes are emitted but not declared: {sorted(unknown)}. "
        "A typo at an emission site otherwise passes every test."
    )


@pytest.mark.skipif(
    not (REPO / "bin" / "cctally-test-remote").exists(),
    reason="bin/cctally-test-remote is maintainer-local; the mirror ships none",
)
def test_the_registry_carries_no_dead_reason_codes():
    dead = _registry() - _emitted_codes()
    assert not dead, f"declared but never emitted: {sorted(dead)}"


def test_a_driven_run_only_uses_registered_reason_codes(tmp_path):
    est = _estate(
        tmp_path,
        harnesses=["alpha", "beta"],
        harness_output={"alpha": "passed: 1   failed: 1\n"},
        harness_exit={"alpha": 1, "beta": 2},
    )
    registry = _registry()
    for reason in _outcome(_drive(est))["reasons"]:
        assert reason["code"] in registry, reason


# ------------------------------------------------------------- the usage band
#
# The aggregator's usage refusals used to write no record at all, so the
# wrapper saw status 2, found nothing to read, and synthesised
# `outcome-record-missing` at exit 3 — a configuration error reaching the
# caller as an infrastructure transport failure.


@pytest.mark.parametrize(
    "var,value",
    [
        ("CCTALLY_TEST_JOBS", "0"),
        ("CCTALLY_OUTER_JOBS", "abc"),
        ("CCTALLY_TEST_ALL_FAKE_NCPU", "16"),
    ],
)
def test_a_usage_error_records_its_own_outcome_at_exit_two(tmp_path, var, value):
    est = _estate(tmp_path)
    r = _drive(est, env={var: value})
    assert r.returncode == 2, r.stdout + r.stderr
    out = _outcome(r)
    assert out["exitCode"] == 2
    assert out["failureClass"] == "infrastructure"
    assert any(
        x["code"] == "aggregator-usage-error" for x in out["reasons"]
    ), out["reasons"]
    assert var in r.stderr


def test_the_plan_mode_conflict_still_writes_no_record(tmp_path):
    """The one usage path that must stay recordless: a plan is never an
    authoritative outcome, so its refusal is not one either."""
    est = _estate(tmp_path)
    r = _drive(est, env={"CCTALLY_TEST_ALL_PLAN": "1"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (est / "outcome.json").exists()


def test_a_usage_error_in_plan_mode_writes_no_record(tmp_path):
    """The worker-budget check runs before the plan/outcome refusal, so this is
    the ordering that could otherwise have published a plan-mode record."""
    est = _estate(tmp_path)
    r = _drive(
        est,
        env={"CCTALLY_TEST_ALL_PLAN": "1", "CCTALLY_TEST_JOBS": "0"},
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (est / "outcome.json").exists()


def test_no_temporary_record_is_left_behind(tmp_path):
    """The record is written atomically by rename, so no .tmp sibling
    survives a completed run."""
    est = _estate(tmp_path)
    _drive(est)
    assert not list(est.glob("outcome.json.tmp*"))


def test_the_human_summary_states_the_verdict_on_an_incomplete_harness(tmp_path):
    """`Total:` counts only summary-line failures, and a harness that exited 2
    over a clean summary has none — the scrollback would otherwise print
    `failed=0` directly above an exit 3."""
    est = _estate(tmp_path, harness_exit={"alpha": 2})
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "failed=0" in r.stdout, r.stdout
    assert "Verdict: FAIL" in r.stdout, r.stdout
    assert "incomplete" in r.stdout, r.stdout
    assert "harness-incomplete" in r.stdout, r.stdout


def test_the_human_summary_states_the_verdict_on_a_pass(tmp_path):
    est = _estate(tmp_path)
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verdict: PASS (exit 0)" in r.stdout, r.stdout


def test_fake_ncpu_outside_plan_mode_is_a_usage_error(tmp_path):
    """CCTALLY_TEST_ALL_FAKE_NCPU is a PLAN-MODE seam. A real run must never
    take its core count from an environment variable."""
    est = _estate(tmp_path)
    r = _drive(est, env={"CCTALLY_TEST_ALL_FAKE_NCPU": "16"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "plan-mode" in r.stderr or "plan mode" in r.stderr


def test_fake_ncpu_inside_plan_mode_is_still_honoured(tmp_path):
    est = _estate(tmp_path)
    e = dict(_hyg.child_aggregator_env({"PATH": os.environ["PATH"]}, est.parent),
             CCTALLY_TEST_ALL_PLAN="1", CCTALLY_TEST_ALL_FAKE_NCPU="16")
    r = subprocess.run(
        [str(est / "bin" / "cctally-test-all")],
        env=e, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "ncpu=16" in r.stdout


def test_plan_mode_cannot_coexist_with_machine_outcome_mode(tmp_path):
    """A plan can never be an authoritative pass, so the two modes are
    mutually exclusive rather than silently precedence-ordered."""
    est = _estate(tmp_path)
    r = _drive(est, env={"CCTALLY_TEST_ALL_PLAN": "1"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (est / "outcome.json").exists()


def test_plan_mode_emits_no_outcome_even_with_a_capability_absent(tmp_path):
    """Plan mode exits before any probe, so a deliberately unprovisioned
    machine can still ask what the runner would do — and gets no outcome
    record for it."""
    est = _estate(tmp_path, capabilities=REAL_CAPABILITIES)
    shim = _shim(est, "pytest")
    r = subprocess.run(
        [str(est / "bin" / "cctally-test-all")],
        env=dict(_hyg.child_aggregator_env({
            "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
            "CCTALLY_AUTHORITATIVE_RUN": "1",
        }, est.parent), CCTALLY_TEST_ALL_PLAN="1"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "harnesses=" in r.stdout
    assert not (est / "outcome.json").exists()


# ------------------------------------------------------- the verdict freeze point
#
# Spec section 3 states the rule by FREEZE POINT rather than by wall-clock
# ordering, and acceptance criterion 9 requires both sides of it. The two sides
# are:
#
#   * A verdict-SUPPORTING artifact — a per-harness log, an exit sidecar, the
#     evidence tree the pool writes into, the authoritative outcome record —
#     remains deciding infrastructure. A storage failure that prevents one of
#     them from being written MAY classify `infrastructure`.
#   * Everything the run does with a settled verdict — the retention pass,
#     eviction, the run manifest, the sanitized extract — is non-deciding. A
#     storage failure there is reported through a diagnostic and the artifact's
#     own absence, and changes neither the class nor the exit code.
#
# The distinction is realized by WHICH function the failing path calls
# (`contract_fail` versus `contract_note`), not by where it sits in the file,
# so both sides need their own observation.


def _evidence_env(root, run_id):
    return {
        "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
        "CCTALLY_TEST_RUN_ID": run_id,
    }


def test_a_storage_failure_before_the_verdict_may_classify_infrastructure(tmp_path):
    """A mid-pool storage failure is DECIDING, and says so in its own words.

    Non-vacuous on two counts. The sabotage is real rather than simulated: the
    exit-sidecar path is occupied by a DIRECTORY, so `run_one`'s
    `echo "$rc" > "$LOGDIR/alpha.exit"` genuinely cannot create a readable
    file while the pool is running. And the harness itself still prints a
    clean passing summary and exits 0, so nothing about the harness's own
    behaviour could produce this verdict — only the unwritable sidecar can.
    """
    root = tmp_path / "ev"
    est = _estate(tmp_path, harness_breaks_sidecar=("alpha",))
    r = _drive(est, env=_evidence_env(root, "freeze-before"))
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure", out
    assert {x["code"] for x in out["reasons"]} == {"pool-machinery-failed"}, out["reasons"]
    assert r.returncode == 3, r.stdout + r.stderr


def test_a_storage_failure_before_the_verdict_can_refuse_the_whole_run(tmp_path):
    """The evidence tree is verdict-supporting while the pool writes into it.

    An unwritable evidence root is a storage failure the run cannot survive,
    because `LOGDIR` lives inside it, and it is therefore allowed to decide.
    Non-vacuous: the identical estate with a WRITABLE root is asserted green
    immediately below, so the class comes from the sabotage and not from the
    estate.
    """
    root = tmp_path / "readonly-root"
    root.mkdir()
    root.chmod(0o500)
    try:
        est = _estate(tmp_path)
        r = _drive(est, env=_evidence_env(root, "freeze-unwritable"))
        assert r.returncode != 0, r.stdout + r.stderr
        assert "evidence" in (r.stdout + r.stderr).lower()
    finally:
        root.chmod(0o700)

    control_root = tmp_path / "ev-control"
    est2 = _estate(tmp_path / "second")
    (tmp_path / "second").mkdir(exist_ok=True)
    r2 = _drive(est2, env=_evidence_env(control_root, "freeze-control"))
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert _outcome(r2)["failureClass"] == "none"


@pytest.mark.parametrize(
    "harness_exit,summary,want_class,want_code",
    [
        (0, DEFAULT_SUMMARY, "none", 0),
        (1, "passed: 3   failed: 2", "product", 1),
    ],
)
def test_a_storage_failure_after_the_verdict_changes_neither_class_nor_code(
    tmp_path, harness_exit, summary, want_class, want_code
):
    """The retention pass is NON-deciding, on a passing run and a failing one.

    The sabotage occupies `<root>/.retention.json` with a DIRECTORY, so the
    retention pass's atomic write genuinely cannot land. That pass runs with
    the verdict already settled, so the rule is that it may report and must not
    decide — and letting it decide would turn an observability subsystem into
    an outage on the merge gate.

    Non-vacuous three ways. The sabotage is proven EFFECTIVE, because the
    obstruction is asserted to have survived the run: had the pass written the
    record, the directory would be gone. The failing leg proves the rule does
    not merely mean "everything stays green", because a `product` failure must
    still come out as `product` and exit 1. And the passing leg proves the
    reverse — that the sabotage cannot silently upgrade a green run to
    infrastructure.
    """
    root = tmp_path / "ev"
    root.mkdir()
    (root / ".retention.json").mkdir()

    est = _estate(
        tmp_path,
        harness_output={"alpha": summary},
        harness_exit={"alpha": harness_exit},
    )
    r = _drive(est, env=_evidence_env(root, f"freeze-after-{want_code}"))
    out = _outcome(r)
    assert out["failureClass"] == want_class, out
    assert r.returncode == want_code, r.stdout + r.stderr
    assert "evidence-" not in " ".join(x["code"] for x in out["reasons"]), out["reasons"]
    # The obstruction is still a directory, which is the proof that the
    # retention write really was refused rather than quietly succeeding.
    assert (root / ".retention.json").is_dir()


# --------------------------------------------------------- subset invocation
#
# `--harness NAME` gives bin/cctally-test-all its first command-line arguments
# (#529 S5 §4.1-§4.4). Any run carrying it is deliberately incomplete, exits 3
# even when every selected harness passes, and can never be cited as a gate.


# Each case carries the argv, the `subject` its reason must record, and a
# stderr fragment unique to its own refusal. Asserting only "exit 2 with an
# aggregator-usage-error" would pass against an implementation that blanket
# rejected every argument with one message, so it could not tell six distinct
# rejections from one — least of all for `duplicate_name` and
# `with_pytest_twice`, the two the parser actually has to reason about. The
# subject separates most of them; the stderr fragment separates the pairs that
# share one (`--with-pytest` twice over, and the three malformed `--harness`
# values).
SUBSET_USAGE_CASES = {
    "duplicate_name": (
        ("--harness", "alpha", "--harness", "alpha"),
        "alpha",
        "was given more than once",
    ),
    "no_value": (
        ("--harness",),
        "--harness",
        "and none followed it",
    ),
    "empty_value": (
        ("--harness", ""),
        "--harness",
        "was given an empty value",
    ),
    "option_as_value": (
        ("--harness", "--with-pytest"),
        "--harness",
        "instead of a harness name",
    ),
    "glob_name": (
        ("--harness", "*"),
        "*",
        "unknown harness '*'",
    ),
    "unknown_name": (
        ("--harness", "nope"),
        "nope",
        "unknown harness 'nope'",
    ),
    "unrecognised_option": (
        ("--nonsense",),
        "--nonsense",
        "unrecognised argument '--nonsense'",
    ),
    "with_pytest_alone": (
        ("--with-pytest",),
        "--with-pytest",
        "requires at least one --harness",
    ),
    "with_pytest_twice": (
        ("--harness", "alpha", "--with-pytest", "--with-pytest"),
        "--with-pytest",
        "at most once",
    ),
}


@pytest.mark.parametrize("case", sorted(SUBSET_USAGE_CASES))
def test_subset_usage_rejection_exits_two(tmp_path, case):
    """Each of §4.1's rejections, individually. An unknown harness name is an
    error rather than a silent no-op, because running nothing and reporting
    success is the failure class S1 closed; `--with-pytest` alone is rejected
    so that no argument-bearing invocation can produce an ordinary exit 0; and
    an empty `--harness` value is rejected because it selects nobody, which is
    the same silent no-op wearing a different shape."""
    args, subject, fragment = SUBSET_USAGE_CASES[case]
    est = _estate(tmp_path)
    r = _drive(est, args=args)
    assert r.returncode == 2, (case, r.stdout, r.stderr)
    out = _outcome(r)
    assert out["exitCode"] == 2
    usage = [x for x in out["reasons"] if x["code"] == "aggregator-usage-error"]
    assert usage, out["reasons"]
    assert [x["subject"] for x in usage] == [subject], (case, out["reasons"])
    assert fragment in r.stderr, (case, r.stderr)
    # No OTHER case's fragment may appear: that is what fails against one
    # blanket message covering every rejection.
    for other, (_, _, other_fragment) in SUBSET_USAGE_CASES.items():
        if other != case:
            assert other_fragment not in r.stderr, (case, other, r.stderr)


def test_a_name_carrying_the_record_delimiter_is_not_a_duplicate(tmp_path):
    """The duplicate test compares whole names, never the elements of a
    delimited record. Its first form split the accumulated names on spaces, so
    `--harness 'a b'` followed by `--harness a` reported a duplicate; moving
    the record to newlines moved the same defect to a name containing a
    newline rather than closing it. Neither input names a real duplicate, and
    reporting one names a mistake the caller did not make."""
    est = _estate(tmp_path, harnesses=["alpha", "beta"])
    r = _drive(est, args=("--harness", "alpha\nnope", "--harness", "alpha"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "was given more than once" not in r.stderr, r.stderr
    assert "unknown harness 'nope'" in r.stderr, r.stderr
    usage = [x for x in _outcome(r)["reasons"] if x["code"] == "aggregator-usage-error"]
    assert [x["subject"] for x in usage] == ["nope"], usage


def test_a_usage_refusal_before_selection_carries_no_coverage(tmp_path):
    """A refusal that never parsed a selection must not describe one."""
    est = _estate(tmp_path)
    r = _drive(est, env={"CCTALLY_TEST_JOBS": "0"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "coverage" not in _outcome(r)


def _executed(sentinel_dir):
    return {p.name[: -len(".ran")] for p in sentinel_dir.glob("*.ran")}


def test_the_execution_sentinel_records_every_harness_on_a_full_run(tmp_path):
    """The control for the subset case below: the marker mechanism really does
    observe execution, so an empty marker set means nothing ran rather than
    that the mechanism is inert."""
    est = _estate(tmp_path, harnesses=["alpha", "beta", "gamma"], sentinel=True)
    ran = tmp_path / "ran"
    ran.mkdir()
    r = _drive(est, env={"SENTINEL_DIR": str(ran)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _executed(ran) == {"alpha", "beta", "gamma", "reconcile"}


def test_a_subset_executes_the_selected_harnesses_and_only_those(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha", "beta", "gamma"], sentinel=True)
    ran = tmp_path / "ran"
    ran.mkdir()
    r = _drive(est, env={"SENTINEL_DIR": str(ran)}, args=("--harness", "beta"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert _executed(ran) == {"beta"}


def test_a_subset_exits_three_even_when_every_selected_harness_passes(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha", "beta"])
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["outcome"] == "fail"
    assert out["failureClass"] == "incomplete"
    assert any(
        x["code"] == "deliberate-subset" for x in out["reasons"]
    ), out["reasons"]
    assert out["coverage"]["mode"] == "subset"
    assert out["coverage"]["pytest"] == "skipped"


def test_the_selected_list_is_in_report_order_not_caller_order(tmp_path):
    """Two invocations naming the same set must produce byte-identical
    records."""
    est = _estate(tmp_path, harnesses=["alpha", "beta", "gamma"])
    forward = _outcome(_drive(est, args=("--harness", "alpha", "--harness", "gamma")))
    reverse = _outcome(_drive(est, args=("--harness", "gamma", "--harness", "alpha")))
    assert forward["coverage"] == reverse["coverage"]
    assert forward["coverage"]["selectedHarnesses"] == ["alpha", "gamma"]
    assert forward["coverage"]["omittedHarnesses"] == ["beta", "reconcile"]


def test_a_subset_skips_pytest_without_claiming_it_was_unavailable(tmp_path):
    est = _estate(tmp_path)
    out = _outcome(_drive(est, args=("--harness", "alpha")))
    assert {x["code"] for x in out["reasons"]} == {"deliberate-subset"}, out["reasons"]
    assert out["pytestPassed"] == 0


def test_with_pytest_re_enables_the_whole_pytest_phase(tmp_path):
    est = _estate(tmp_path)
    r = _drive(est, args=("--harness", "alpha", "--with-pytest"))
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["coverage"]["pytest"] == "full"
    # The count is the proof the phase really ran, not merely that a field
    # says so.
    assert out["pytestPassed"] == 1, out


def test_naming_every_harness_is_still_a_subset(tmp_path):
    """The mode is set by the PRESENCE of --harness, not by how many names
    follow it; without that rule the exit-3 contract has an obvious loophole."""
    est = _estate(tmp_path, harnesses=["alpha", "beta"])
    r = _drive(
        est,
        args=(
            "--harness", "alpha", "--harness", "beta", "--harness", "reconcile",
        ),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["coverage"]["mode"] == "subset"
    assert out["coverage"]["omittedHarnesses"] == []
    assert any(x["code"] == "deliberate-subset" for x in out["reasons"])


def test_an_authoritative_subset_refuses_before_any_harness_executes(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha", "beta"], sentinel=True)
    ran = tmp_path / "ran"
    ran.mkdir()
    r = _drive(
        est,
        env={"SENTINEL_DIR": str(ran), "CCTALLY_AUTHORITATIVE_RUN": "1"},
        args=("--harness", "alpha"),
    )
    assert r.returncode == 2, r.stdout + r.stderr
    # The refusal precedes execution, observed positively: no harness left a
    # marker behind.
    assert _executed(ran) == set()
    out = _outcome(r)
    assert any(
        x["code"] == "aggregator-usage-error" for x in out["reasons"]
    ), out["reasons"]
    # The record still describes the selection the run was refused for.
    assert out["coverage"]["mode"] == "subset"


def test_discovery_still_refuses_a_lost_executable_bit_under_a_subset(tmp_path):
    """Discovery checks the TREE, not the executed set: narrowing what executes
    must not narrow what is checked."""
    est = _estate(tmp_path, harnesses=["alpha", "beta"], modes={"beta": 0o644})
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 2, r.stdout + r.stderr
    usage = [x for x in _outcome(r)["reasons"] if x["code"] == "aggregator-usage-error"]
    assert [x["subject"] for x in usage] == ["cctally-beta-test"], usage


def test_an_omitted_harness_is_not_judged(tmp_path):
    """A harness that did not run must not be recorded as passed, and equally
    must not be recorded as failed."""
    est = _estate(tmp_path, harnesses=["alpha", "beta"], harness_exit={"beta": 1})
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert {x["code"] for x in _outcome(r)["reasons"]} == {"deliberate-subset"}


def test_the_subset_console_says_plainly_that_it_is_not_a_gate(tmp_path):
    """Exiting 3 on an all-passing targeted run is deliberately surprising, so
    the console must not let a reader mistake it for a failure of the selected
    harnesses."""
    est = _estate(tmp_path, harnesses=["alpha", "beta"])
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "PASSED" in r.stdout, r.stdout
    assert "deliberately incomplete" in r.stdout, r.stdout
    assert "NOT a gate" in r.stdout, r.stdout
    assert "Verdict: FAIL" not in r.stdout, r.stdout


def test_a_subset_with_a_real_failure_still_reads_as_a_failure(tmp_path):
    """The reassuring wording is reserved for the case where the ONLY reason is
    the deliberate subset."""
    est = _estate(
        tmp_path,
        harnesses=["alpha", "beta"],
        harness_output={"alpha": "passed: 1   failed: 1\n"},
        harness_exit={"alpha": 1},
    )
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "Verdict: FAIL" in r.stdout, r.stdout
    assert "harness-failed" in _codes(r)


def test_a_full_run_records_coverage_mode_full(tmp_path):
    est = _estate(tmp_path)
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    coverage = _outcome(r)["coverage"]
    assert coverage["mode"] == "full"
    assert coverage["pytest"] == "full"
    assert coverage["omittedHarnesses"] == []
    assert coverage["selectedHarnesses"] == ["alpha", "reconcile"]


# ------------------------------------------- empty arrays under bash 3.2
#
# macOS system bash is 3.2, where `"${a[@]}"` on an EMPTY array is an
# `unbound variable` error under `set -u`. The aggregator then dies at exit 1
# from the middle of a run, writing no outcome record — the one failure shape
# the verdict contract cannot describe, because a caller then reads a status
# with nothing to corroborate it. Both LAN runners are Macs, so this is the real
# interpreter, not a hypothetical one.


def _legacy_bash():
    """Return a path to a bash older than 4.4, or None."""
    for candidate in ("/bin/bash", "/usr/bin/bash"):
        if not os.path.exists(candidate):
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "printf %s.%s ${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except OSError:
            continue
        try:
            major, minor = (int(x) for x in probe.stdout.strip().split("."))
        except ValueError:
            continue
        if (major, minor) < (4, 4):
            return candidate
    return None


def test_an_estate_with_no_parallel_harnesses_survives_a_legacy_bash(tmp_path):
    """`reconcile` alone leaves `parallel_harnesses` empty, which is reachable
    today and does not need the subset feature to get there."""
    bash = _legacy_bash()
    if bash is None:
        pytest.skip("no bash older than 4.4 is installed on this host")
    est = _estate(tmp_path, harnesses=[])
    r = _drive(est, interpreter=bash)
    assert "unbound variable" not in r.stderr, r.stderr
    assert r.returncode == 0, r.stdout + r.stderr
    assert _outcome(r)["failureClass"] == "none"


# Every array a run can leave empty. The subset feature makes
# `report_harnesses` and `rows` emptiable, `parallel_harnesses` is empty for an
# estate carrying no pool harness, `final_harnesses` for one without
# `reconcile`, `subset_harnesses` while it is being built, and the pytest
# argument arrays whenever their optional plugins are absent.
EMPTIABLE_ARRAYS = (
    "final_harnesses",
    "parallel_harnesses",
    "pytest_common",
    "pytest_durations_legs",
    "report_harnesses",
    "rows",
    "subset_harnesses",
)


def test_every_emptiable_array_expansion_carries_the_bash_3_2_guard():
    """The guard cannot be observed at runtime for the sites a fixed parser now
    keeps unreachable, so it is asserted on the source shape instead. The one
    accepted form is `${a[@]+"${a[@]}"}`, which expands to nothing at all when
    the array is unset or empty."""
    text = RUNNER.read_text(encoding="utf-8")
    offenders = []
    for name in EMPTIABLE_ARRAYS:
        guarded = re.compile(
            r"\$\{%s\[([@*])\]\+\s*\"?\$\{%s\[\1\]\}\"?\s*\}" % (name, name)
        )
        bare = re.compile(r"\$\{%s\[[@*]\]\}" % name)
        for number, line in enumerate(text.splitlines(), 1):
            if bare.search(guarded.sub("", line)):
                offenders.append(f"{number}: {line.strip()}")
    assert not offenders, (
        "unguarded expansions of an array that can be empty (bash 3.2 exits 1 "
        "with `unbound variable` and writes no outcome record):\n"
        + "\n".join(offenders)
    )


# --- #966(c), spec 4.3: a nested contract run never writes the parent's outcome --


# Any refusal that records an outcome at exit 2 reproduces E1.
_E1_USAGE_REFUSAL = 'contract_usage_abort "an E1 usage refusal" CCTALLY_TEST_JOBS\n'


def test_a_nested_contract_run_cannot_write_the_parent_outcome(tmp_path, monkeypatch):
    """E1. A usage refusal, run as a test inside an aggregator's pytest leg
    would run it: CCTALLY_TEST_ALL_OUTCOME_FILE names
    the PARENT run's record. The child must exit 2 and leave that record's bytes
    and metadata exactly as they were, checked before the parent could rewrite it."""
    sentinel = tmp_path / "parent-outcome.json"
    sentinel.write_bytes(b'{"sentinel": "the parent run\'s record"}\n')
    os.chmod(sentinel, 0o640)
    before = os.lstat(sentinel)
    content = sentinel.read_bytes()
    monkeypatch.setenv("CCTALLY_TEST_ALL_OUTCOME_FILE", str(sentinel))
    r = _run_lib(tmp_path, _E1_USAGE_REFUSAL)
    after = os.lstat(sentinel)
    assert sentinel.read_bytes() == content, "the nested run wrote the parent run's outcome"
    assert (after.st_ino, after.st_size, after.st_mode, after.st_mtime_ns) == (
        before.st_ino, before.st_size, before.st_mode, before.st_mtime_ns), (
        "the nested run touched the parent run's outcome")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)


def test_an_explicit_child_outcome_destination_still_works(tmp_path):
    """E1's control: a test may still give its child an explicit, child-local
    outcome destination (spec 4.3)."""
    child = tmp_path / "child-outcome.json"
    r = _run_lib(tmp_path, _E1_USAGE_REFUSAL,
                 env={"CCTALLY_TEST_ALL_OUTCOME_FILE": str(child)})
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert json.loads(child.read_text(encoding="utf-8"))["exitCode"] == 2


_E1_OWNER = {
    "CCTALLY_TEST_RUN_ID": "e1-run",
    "CCTALLY_TEST_EVIDENCE_MAX_AGE_DAYS": "7",
    "CCTALLY_TEST_EVIDENCE_MAX_BYTES": "1000000000",
    "CCTALLY_REMOTE_DIR": "e1-remote",
    "CCTALLY_TEST_ALL_E1_PROBE": "owner-only by prefix",
    "CCTALLY_TEST_EVIDENCE_E1_PROBE": "owner-only by prefix",
}
_E1_PRESERVED = {
    "CCTALLY_PROGRESS_INTERVAL": "30",
    # Outside the owner family by name, so it is inherited like any other.
    "_CCTALLY_E1_INHERITED_PROBE": "e1-token",
}
_E1_DUMP_TEST = (
    "import json, os\n"
    "def test_dumps_its_environment():\n"
    "    with open(os.environ['E1_PYTEST_DUMP'], 'w') as fh:\n"
    "        json.dump(sorted(os.environ), fh)\n"
)


def _e1_owner_names(names):
    return sorted(n for n in names if n.startswith(("CCTALLY_TEST_ALL_", "CCTALLY_TEST_EVIDENCE_"))
                  or n in {"CCTALLY_TEST_RUN_ID", "CCTALLY_REMOTE_DIR", "_CCTALLY_HARNESS_SCRUB"})


def test_owner_only_variables_stop_at_the_harness_and_pytest_boundaries(tmp_path):
    """E1 at the shell-harness and pytest launch boundaries of bin/cctally-test-all.
    The aggregator is given every owner-only family (the outcome file it needs
    itself, the run id, the evidence family, the index and remote-dir flags, and
    one synthetic name per prefix); neither a harness nor a pytest leg may see
    any of them, and the deliberately inherited classes still arrive."""
    est = _estate(tmp_path)
    harness_dump, pytest_dump = tmp_path / "harness-env.json", tmp_path / "pytest-env.json"
    alpha = est / "bin" / "cctally-alpha-test"
    alpha.write_text(
        "#!/usr/bin/env bash\n"
        "python3 -c 'import json, os, sys; json.dump(sorted(os.environ), open(sys.argv[1], \"w\"))' "
        "\"$E1_HARNESS_DUMP\"\n"
        "printf '%s\\n' " + shlex.quote(DEFAULT_SUMMARY.rstrip("\n")) + "\n"
        "exit 0\n", encoding="utf-8")
    alpha.chmod(0o755)
    (est / "tests" / "test_e1_dump.py").write_text(_E1_DUMP_TEST, encoding="utf-8")
    env = dict(_E1_OWNER, **_E1_PRESERVED)
    env.update({"CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
                "E1_HARNESS_DUMP": str(harness_dump), "E1_PYTEST_DUMP": str(pytest_dump)})
    r = _drive(est, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    harness = json.loads(harness_dump.read_text(encoding="utf-8"))
    leg = json.loads(pytest_dump.read_text(encoding="utf-8"))
    assert _e1_owner_names(harness) == [], (
        "owner-only variables reached the harness boundary: %s" % _e1_owner_names(harness))
    assert _e1_owner_names(leg) == [], (
        "owner-only variables reached the pytest boundary: %s" % _e1_owner_names(leg))
    # #965's test retention switch is deliberately inherited (ledger P4): the
    # aggregator exports it, and both boundaries must keep handing it on.
    for name in ("CCTALLY_TEST_JOBS", "CCTALLY_DISABLE_DEV_AUTODETECT",
                 "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF", *_E1_PRESERVED):
        assert name in harness and name in leg, name


# --- #529 S6 M4: the degradation is announced ONCE, with a real count -------


def _agentmem_gated_modules():
    """Every test module that imports the ONE shared gate, found on disk.

    Enumerated as a CLASS rather than named, for two reasons. Both modules that
    carry agentmem-gated tests are mirror-EXCLUDED, so naming one would make
    this public file reference a private path at run time — which is exactly
    what `tests/test_public_test_dep_closure.py` refuses, and it refused this
    file's first draft. And on the public tree the answer is legitimately empty,
    where a hard-coded name would be a missing file rather than an absent case.
    """
    found = []
    for path in sorted((REPO / "tests").glob("test_*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Match the actual dependency edge, not this helper's prose mentioning
        # the gate. On the public tree both gated modules are mirror-excluded;
        # the broad substring search selected this module itself, launched a
        # nested collection containing zero gated tests, then violated its own
        # non-vacuity assertion instead of taking the intended no-module skip.
        if re.search(r"^\s*from\s+_agentmem_gate\s+import\s+", text, re.MULTILINE):
            found.append(path.relative_to(REPO).as_posix())
    return found


def _run_nested_pytest(tmp_path, extra_args, scrub_agentmem: bool):
    """COLLECT a real pytest run, optionally without agentmem on PATH.

    A nested run of the real suite rather than a synthetic estate, because the
    announcement lives in the real `tests/conftest.py` and a scratch conftest
    would be a different file asserting nothing about the one that ships.

    `--collect-only` is deliberate and is what keeps this inside the phase-3
    per-test budget. Everything under test happens at collection and at session
    finish — the per-worker count is taken in `pytest_collection_modifyitems`
    and the announcement is emitted in `pytest_sessionfinish` — so executing the
    selected tests would add load-sensitive wall time and verify nothing extra.
    The first draft ran them under `timeout=600`, which both exceeded the 120s
    pytest-timeout the suite imposes (so it died under the full suite while
    passing standalone).
    """
    modules = _agentmem_gated_modules()
    if not modules:
        pytest.skip("this tree carries no agentmem-gated module to collect")
    env = dict(os.environ)
    env.pop("CCTALLY_AGENTMEM_TEST_POLICY", None)
    # A nested run is its OWN session, and every xdist variable in this
    # environment describes the OUTER one. `PYTEST_XDIST_WORKER` in particular is
    # inherited by every descendant, so under the full suite — which runs phase 3
    # with `-n 10` — the nested controller read it, concluded it was a worker,
    # and stayed silent. These three tests passed standalone and failed only
    # inside the suite for exactly that reason.
    for leaked in (
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
        "PYTEST_CURRENT_TEST",
    ):
        env.pop(leaked, None)
    if scrub_agentmem:
        # An empty directory in FRONT is not enough — `shutil.which` walks the
        # whole PATH. Replace it outright, and use the absolute interpreter so
        # the run does not need PATH to find itself.
        env["PATH"] = str(tmp_path / "empty-path")
        (tmp_path / "empty-path").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *modules, "-q", "--collect-only", *extra_args],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        timeout=110,
    )


def test_absent_agentmem_announces_the_contract_once_with_a_nonzero_count(tmp_path):
    pytest.importorskip("xdist")
    r = _run_nested_pytest(tmp_path, ["-n", "4"], scrub_agentmem=True)
    lines = [l for l in r.stdout.splitlines() if "agentmem contract:" in l]
    # Exactly one, from the controller. Four workers all reach sessionfinish,
    # and announcing from each would print the line once per worker.
    assert len(lines) == 1, (lines, r.stdout[-3000:])
    assert "ABSENT" in lines[0], lines[0]
    # The non-vacuity anchor. A count of zero is indistinguishable from no
    # degradation at all, which is exactly what this anchor exists to catch.
    match = re.search(r"so (\d+) agentmem-gated test", lines[0])
    assert match, lines[0]
    assert int(match.group(1)) > 0, lines[0]


def test_the_announced_count_does_not_multiply_by_the_worker_count(tmp_path):
    """Every worker collects the WHOLE estate, so the aggregate is a maximum.

    Summing the per-worker counts announced four times the real figure under
    `-n 4` — a number that is not merely imprecise but false, and false in a way
    that grows with the worker count.
    """
    pytest.importorskip("xdist")
    serial = _run_nested_pytest(tmp_path / "serial", [], scrub_agentmem=True)
    parallel = _run_nested_pytest(tmp_path / "par", ["-n", "4"], scrub_agentmem=True)

    def _count(result):
        line = next(
            l for l in result.stdout.splitlines() if "agentmem contract:" in l
        )
        return int(re.search(r"so (\d+) agentmem-gated test", line).group(1))

    assert _count(serial) == _count(parallel), (serial.stdout[-1500:], parallel.stdout[-1500:])


def test_present_agentmem_announces_that_the_gated_tests_ran(tmp_path):
    if shutil.which("agentmem") is None:
        pytest.skip("agentmem not installed; the present-branch cannot be observed here")
    r = _run_nested_pytest(tmp_path, [], scrub_agentmem=False)
    lines = [l for l in r.stdout.splitlines() if "agentmem contract:" in l]
    assert len(lines) == 1, (lines, r.stdout[-3000:])
    assert "agentmem is present" in lines[0], lines[0]
