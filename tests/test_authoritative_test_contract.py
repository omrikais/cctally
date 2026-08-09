"""Mutation matrix for the authoritative-gate verdict contract (#529 S1).

Every case mutates ONE property of a known-green scratch estate and asserts
the exact exit code, failureClass and reason code. Each guard also has a
green control, because a test that only ever sees red cannot distinguish a
working guard from one that fails on everything.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

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
    e.update(env or {})
    return subprocess.run(
        [str(script)], capture_output=True, text=True, env=e, timeout=60
    )


MINIMAL = {
    "schemaVersion": 1,
    "minHarnessRows": 1,
    "harnesses": [
        {"name": "alpha", "visibility": "public", "minCases": 1,
         "countPolicy": "fixed"}
    ],
    "capabilities": [],
    "forbiddenRegeneration": [],
}


def _manifest(tmp_path, obj=None, raw=None):
    p = tmp_path / "manifest.json"
    p.write_text(raw if raw is not None else json.dumps(obj or MINIMAL))
    return p


def test_valid_manifest_loads(tmp_path):
    m = _manifest(tmp_path)
    r = _run_lib(tmp_path, f'contract_manifest_load "{m}" && echo OK')
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_duplicate_key_is_rejected(tmp_path):
    m = _manifest(tmp_path, raw='{"schemaVersion": 1, "schemaVersion": 2}')
    r = _run_lib(
        tmp_path,
        f'contract_manifest_load "{m}"; echo "$CONTRACT_CLASS $CONTRACT_LAST_CODE"',
    )
    assert "infrastructure manifest-duplicate-key" in r.stdout


def test_unknown_key_is_rejected(tmp_path):
    obj = dict(MINIMAL)
    obj["harnesses"] = [dict(MINIMAL["harnesses"][0], nonsense=1)]
    m = _manifest(tmp_path, obj)
    r = _run_lib(
        tmp_path,
        f'contract_manifest_load "{m}"; echo "$CONTRACT_CLASS $CONTRACT_LAST_CODE"',
    )
    assert "infrastructure manifest-unknown-key" in r.stdout


def test_truncated_manifest_fails_the_non_triviality_floor(tmp_path):
    obj = dict(MINIMAL, harnesses=[], minHarnessRows=1)
    m = _manifest(tmp_path, obj)
    r = _run_lib(
        tmp_path,
        f'contract_manifest_load "{m}"; echo "$CONTRACT_CLASS $CONTRACT_LAST_CODE"',
    )
    assert "infrastructure manifest-nontrivial" in r.stdout


def test_unreadable_manifest_is_its_own_reason(tmp_path):
    r = _run_lib(
        tmp_path,
        f'contract_manifest_load "{tmp_path}/absent.json"; '
        'echo "$CONTRACT_CLASS $CONTRACT_LAST_CODE"',
    )
    assert "infrastructure manifest-unreadable" in r.stdout


def test_committed_manifest_parses(tmp_path):
    """The real committed manifest must satisfy its own parser."""
    committed = REPO / "tests" / "authoritative-test-manifest.json"
    r = _run_lib(tmp_path, f'contract_manifest_load "{committed}" && echo OK')
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


# --------------------------------------------------------------- scratch estate
#
# No capability-override environment seam exists on purpose: a bypass variable
# sitting next to the check it tests is exactly the hazard this session closes.
# Instead every case injects a scratch repository. REPO_ROOT derives from the
# aggregator's own location (bin/cctally-test-all), so copying the aggregator,
# the contract library, a manifest and fake executable harnesses into a temp
# tree exercises the true entry point.


def _estate(
    tmp_path,
    harnesses=("alpha",),
    manifest_names=None,
    modes=None,
    harness_output=None,
    harness_exit=None,
    harness_kill=None,
    harness_breaks_sidecar=(),
    manifest_min=None,
    manifest_visibility=None,
    private=False,
    capabilities=None,
    forbidden=None,
    min_harness_rows=0,
    smoke_test=True,
):
    """A known-green scratch estate; each case mutates exactly one property."""
    repo = tmp_path / "estate"
    bindir = repo / "bin"
    testsdir = repo / "tests"
    bindir.mkdir(parents=True)
    testsdir.mkdir()

    shutil.copy2(RUNNER, bindir / "cctally-test-all")
    shutil.copy2(CONTRACT_LIB, bindir / "_lib-test-contract.sh")

    # bin/cctally-test-all keeps `reconcile` in final_harnesses as a summary
    # ordering device, so every estate must carry it or the pool runs a harness
    # that is not on disk.
    disk = list(harnesses) + ["reconcile"]
    names = list(harnesses if manifest_names is None else manifest_names)
    names = names + ["reconcile"]

    modes = modes or {}
    harness_output = harness_output or {}
    harness_exit = harness_exit or {}
    harness_kill = harness_kill or {}
    manifest_min = manifest_min or {}
    manifest_visibility = manifest_visibility or {}

    for name in disk:
        path = bindir / f"cctally-{name}-test"
        body = ["#!/usr/bin/env bash"]
        if name in harness_breaks_sidecar:
            # Occupy the sidecar path with a DIRECTORY so run_one's
            # `echo "$rc" > "$LOGDIR/<name>.exit"` cannot create a readable
            # file. That is a real pool-machinery failure: the status was
            # never recorded, so the harness cannot be judged on what it
            # printed.
            body.append(f'mkdir -p "$LOGDIR/{name}.exit"')
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

    # A trivial passing pytest estate: the aggregator's phase 3 runs
    # `python3 -m pytest tests/` in the scratch root, and an empty tests/ dir
    # would exit 5 (nothing collected) on every case. `smoke_test=False` is how
    # a case reproduces exactly that exit-5 shape.
    if smoke_test:
        (testsdir / "test_scratch_smoke.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )

    doc = {
        "schemaVersion": 1,
        "minHarnessRows": min_harness_rows,
        "harnesses": [
            {
                "name": n,
                "visibility": manifest_visibility.get(n, "public"),
                "minCases": manifest_min.get(n, 0),
                "countPolicy": "fixed",
            }
            for n in names
        ],
        "capabilities": list(capabilities or []),
        "forbiddenRegeneration": list(
            forbidden if forbidden is not None else []
        ),
    }
    (testsdir / "authoritative-test-manifest.json").write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8"
    )

    if private:
        # .mirror-allowlist excludes itself, so its presence is the private
        # discriminator bin/cctally-test-all already uses.
        (repo / ".mirror-allowlist").write_text(
            "bin/cctally-test-all\nbin/_lib-*\ntests/**\nbin/cctally-*\n"
            "!bin/cctally-secret-test\n",
            encoding="utf-8",
        )
        # The allowlist classifier is maintainer-local: the public mirror ships
        # no .githooks/, so this reference must be existence-gated or the
        # mirrored public suite fails on a file it never received.
        if not (REPO / ".githooks").exists() or not (
            REPO / ".githooks" / "_match.py"
        ).exists():
            pytest.skip(
                "the allowlist classifier .githooks/_match.py is maintainer-local"
            )
        githooks = repo / ".githooks"
        githooks.mkdir(exist_ok=True)
        shutil.copy2(REPO / ".githooks" / "_match.py", githooks / "_match.py")
    return repo


def _drive(est, env=None, args=(), timeout=180):
    outcome = est / "outcome.json"
    if outcome.exists():
        outcome.unlink()
    e = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(est)),
        "CCTALLY_TEST_ALL_OUTCOME_FILE": str(outcome),
        "CCTALLY_TEST_JOBS": "1",
    }
    e.update(env or {})
    proc = subprocess.run(
        [str(est / "bin" / "cctally-test-all"), *args],
        env=e,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    proc.outcome_path = outcome  # type: ignore[attr-defined]
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


def test_admission_harness_on_disk_but_not_in_manifest(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha", "beta"], manifest_names=["alpha"])
    r = _drive(est)
    assert r.returncode == 3, r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure"
    assert any(
        x["code"] == "manifest-unexpected-harness" and x["subject"] == "beta"
        for x in out["reasons"]
    ), out["reasons"]
    # The diagnostic must state both alternatives, not merely the reason code.
    assert "beta" in r.stderr and "manifest" in r.stderr


def test_admission_harness_in_manifest_but_absent_on_disk(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha"], manifest_names=["alpha", "beta"])
    r = _drive(est)
    assert r.returncode == 3, r.stderr
    assert "manifest-missing-harness" in _codes(r)


def test_admission_executable_bit_cleared_is_its_own_reason(tmp_path):
    est = _estate(
        tmp_path, harnesses=["alpha"], manifest_names=["alpha"], modes={"alpha": 0o644}
    )
    r = _drive(est)
    assert r.returncode == 3, r.stderr
    reasons = _codes(r)
    assert "harness-not-executable" in reasons
    # F27's whole point: a lost mode bit must NOT look like an absent harness.
    assert "manifest-missing-harness" not in reasons


def test_admission_every_delta_is_reported_not_only_the_first(tmp_path):
    est = _estate(
        tmp_path,
        harnesses=["alpha", "beta", "gamma"],
        manifest_names=["alpha", "delta"],
    )
    r = _drive(est)
    assert r.returncode == 3, r.stderr
    subjects = {
        x["subject"]
        for x in _outcome(r)["reasons"]
        if x["code"] in ("manifest-unexpected-harness", "manifest-missing-harness")
    }
    assert subjects == {"beta", "gamma", "delta"}, subjects


def _gnu_mktemp_shim(est):
    """A `mktemp` that rejects a template with fewer than three trailing X's.

    GNU coreutils exits 1 on such a template, and it does so before it even
    interprets `-t`. BSD `mktemp` treats the argument as a prefix and appends
    its own randomness instead, so a macOS-only run cannot observe the
    difference. Everything else delegates to the real binary.
    """
    shim = est / "shim"
    shim.mkdir(exist_ok=True)
    p = shim / "mktemp"
    p.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in -*) continue ;; esac\n'
        '  case "$arg" in\n'
        "    *XXX) ;;\n"
        "    *) printf \"mktemp: too few X's in template '%s'\\n\" \"$arg\" >&2\n"
        "       exit 1 ;;\n"
        "  esac\n"
        "done\n"
        f'exec {shutil.which("mktemp")} "$@"\n',
        encoding="utf-8",
    )
    p.chmod(0o755)
    return {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}


def test_admission_is_not_a_no_op_under_a_gnu_style_mktemp(tmp_path):
    """Every admission delta must still be reported when scratch allocation
    behaves the way it does on Linux.

    A template without `XXXXXX` makes GNU `mktemp` exit 1, so both scratch
    variables become empty strings, every redirection and every `comm` fails,
    and the whole of admission — set equality in both directions and the F27
    executable-bit check — silently reports nothing on every Linux lane.
    """
    est = _estate(
        tmp_path,
        harnesses=["alpha", "beta"],
        manifest_names=["alpha", "delta"],
        modes={"alpha": 0o644},
    )
    r = _drive(est, env=_gnu_mktemp_shim(est))
    out = _outcome(r)
    assert r.returncode == 3, (
        "admission became a silent no-op under a GNU-style mktemp: "
        f"exit={r.returncode} reasons={out['reasons']}\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    codes = {(x["code"], x["subject"]) for x in out["reasons"]}
    assert ("manifest-unexpected-harness", "beta") in codes, codes
    assert ("manifest-missing-harness", "delta") in codes, codes
    assert ("harness-not-executable", "alpha") in codes, codes


def test_admission_refuses_when_scratch_allocation_fails(tmp_path):
    """An inability to perform a declared check is itself an infrastructure
    refusal, so a future failure here can never turn admission into a no-op
    again."""
    est = _estate(tmp_path, harnesses=["alpha"], manifest_names=["alpha"])
    shim = est / "shim"
    shim.mkdir(exist_ok=True)
    p = shim / "mktemp"
    # -d still works, because bin/cctally-test-all needs its LOGDIR; only the
    # file form fails, which is exactly what admission allocates.
    p.write_text(
        "#!/usr/bin/env bash\n"
        'case " $* " in *" -d "*) exec %s "$@" ;; esac\n'
        "exit 1\n" % shutil.which("mktemp"),
        encoding="utf-8",
    )
    p.chmod(0o755)
    r = _drive(est, env={"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"})
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure"
    assert "admission-scratch-failed" in {x["code"] for x in out["reasons"]}


def test_admission_truncated_manifest_cannot_pass(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha"], min_harness_rows=99)
    r = _drive(est)
    assert r.returncode == 3, r.stderr
    assert "manifest-nontrivial" in _codes(r)


def test_admission_visibility_drift_is_caught_in_the_private_profile(tmp_path):
    est = _estate(
        tmp_path,
        harnesses=["alpha", "secret"],
        private=True,
        manifest_visibility={"secret": "public"},
    )
    r = _drive(est)
    assert r.returncode == 3, r.stderr
    out = _outcome(r)
    assert any(
        x["code"] == "visibility-drift" and x["subject"] == "secret"
        for x in out["reasons"]
    ), out["reasons"]


def test_admission_private_profile_is_green_when_visibility_agrees(tmp_path):
    est = _estate(
        tmp_path,
        harnesses=["alpha", "secret"],
        private=True,
        manifest_visibility={"secret": "private"},
    )
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _outcome(r)["failureClass"] == "none"


def test_admission_public_profile_ignores_private_rows(tmp_path):
    """The public tree ships no private harness; requiring one would fail its
    CI before a single harness runs (#131)."""
    est = _estate(
        tmp_path,
        harnesses=["alpha"],
        manifest_names=["alpha", "secret"],
        manifest_visibility={"secret": "private"},
    )
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _outcome(r)["failureClass"] == "none"


def test_unmutated_estate_is_green(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha"], manifest_names=["alpha"])
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
# The authoritative marker CCTALLY_AUTHORITATIVE_RUN=1 is exported by
# bin/cctally-test-remote for the canonical command. Setting it can only make a
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

    The contract library itself runs `python3 -` and `python3 -c` to parse the
    manifest and encode the outcome, so a shim that failed every form would
    break the fixture rather than the probe under test.
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
    bin/cctally-test-remote pins `required` for every remote execution."""
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


def test_the_committed_manifest_declares_every_regeneration_variable():
    doc = json.loads(
        (REPO / "tests" / "authoritative-test-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert sorted(doc["forbiddenRegeneration"]) == sorted(FORBIDDEN)


@pytest.mark.parametrize("var", FORBIDDEN)
@pytest.mark.parametrize("value", ["1", "0", "true", ""])
def test_any_regeneration_variable_refuses_the_run(tmp_path, var, value):
    """Presence is poison. Deliberately stricter than each variable's own
    `= 1` activation predicate: REGEN=0 does nothing today, and REGEN=true
    looks like it does something while doing nothing."""
    est = _estate(tmp_path, forbidden=FORBIDDEN)
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


def test_the_committed_manifest_declares_every_hard_capability():
    """An emptied `capabilities` array would disable every probe in silence,
    exactly as an emptied `forbiddenRegeneration` array would. The names and
    their hardness are hard-coded here rather than read from the manifest, so
    the tripwire cannot be satisfied by editing the thing it guards."""
    doc = json.loads(
        (REPO / "tests" / "authoritative-test-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    declared = {row["name"]: row.get("hard") for row in doc["capabilities"]}
    for name, hard in HARD_CAPABILITIES.items():
        assert declared.get(name) == hard, (name, declared)
    # pytest-xdist is recorded and never required: it is a speed knob and
    # changes nothing about what was verified.
    assert declared.get("pytest-xdist") is False, declared


def test_a_clean_environment_is_the_green_control(tmp_path):
    """The same eight variables declared, none set: the guard must pass."""
    est = _estate(tmp_path, forbidden=FORBIDDEN)
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "regeneration-enabled" not in _codes(r)


def test_regeneration_is_refused_even_without_the_authoritative_marker(tmp_path):
    """The guard is not scoped to the authoritative profile: an adopted golden
    is a wrong artifact on any lane."""
    est = _estate(tmp_path, forbidden=FORBIDDEN)
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
# A harness passed only when all four hold: exit 0, a parseable final summary,
# `failed == 0`, and `passed + failed >= minCases`.


def _pytest_rc_shim(est, rc):
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


def test_floor_unmet_on_a_clean_exit_is_incomplete(tmp_path):
    est = _estate(
        tmp_path,
        harness_output={"alpha": "passed: 0   failed: 0\n"},
        manifest_min={"alpha": 1},
    )
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "incomplete"
    assert any(
        x["code"] == "case-floor-unmet" and x["subject"] == "alpha"
        for x in out["reasons"]
    ), out["reasons"]


def test_zero_exit_with_failures_in_the_summary_is_a_mismatch(tmp_path):
    est = _estate(
        tmp_path,
        harness_output={"alpha": "passed: 3   failed: 2\n"},
        harness_exit={"alpha": 0},
    )
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "incomplete"
    codes = {x["code"] for x in out["reasons"]}
    # The product cause is RETAINED alongside the mismatch.
    assert codes == {"exit-summary-mismatch", "harness-failed"}, out["reasons"]


def test_nonzero_exit_with_a_clean_summary_is_a_mismatch(tmp_path):
    est = _estate(
        tmp_path,
        harness_output={"alpha": "passed: 4   failed: 0\n"},
        harness_exit={"alpha": 2},
    )
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "incomplete"
    assert "exit-summary-mismatch" in {x["code"] for x in out["reasons"]}


def test_the_doc_lint_self_check_shape_is_caught_twice(tmp_path):
    """bin/cctally-doc-lint-test::self_check_fail emits `passed: 0  failed: 0`
    and exits 2. Today that lands as a clean pass and the self-check that
    exists to prevent a vacuous doc-lint run cannot fail the bundle."""
    est = _estate(
        tmp_path,
        harness_output={"alpha": "passed: 0   failed: 0\n"},
        harness_exit={"alpha": 2},
        manifest_min={"alpha": 1},
    )
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    codes = {x["code"] for x in _outcome(r)["reasons"]}
    assert codes == {"exit-summary-mismatch", "case-floor-unmet"}, codes


@pytest.mark.parametrize(
    "output", ["", "passed: x   failed: y\n", "passed:   failed:\n", "all good\n"]
)
def test_missing_or_malformed_summary_is_unreadable(tmp_path, output):
    est = _estate(tmp_path, harness_output={"alpha": output})
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "incomplete"
    assert "summary-unreadable" in {x["code"] for x in out["reasons"]}


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
        harness_exit={"alpha": 1},
        manifest_min={"beta": 99},
    )
    r = _drive(est)
    out = _outcome(r)
    assert out["failureClass"] == "incomplete"  # precedence
    codes = {x["code"] for x in out["reasons"]}
    assert codes == {"harness-failed", "case-floor-unmet"}, out["reasons"]
    assert r.returncode == 3


# ----------------------------------------------------------------- pytest phase


def test_pytest_failure_is_product(tmp_path):
    est = _estate(tmp_path)
    r = _drive(est, env=_pytest_rc_shim(est, 1))
    assert r.returncode == 1, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "product"
    assert "pytest-failed" in {x["code"] for x in out["reasons"]}


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


# --------------------------------------------------- outcome schema + seams

VALID_CLASSES = {"none", "product", "infrastructure", "incomplete"}
VALID_PHASES = {"admission", "harness", "pytest", "transport"}


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
        ({"manifest_min": {"alpha": 999}}, "incomplete", 3),
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
        harness_exit={"alpha": 1},
        manifest_min={"beta": 99},
    )
    out = _outcome(_drive(est))
    for reason in out["reasons"]:
        assert reason["phase"] in VALID_PHASES, reason
        assert set(reason) == {"code", "phase", "subject"}, reason
    # `invalid` was Codex's name for the third class; the maintainer chose
    # `incomplete`. The vocabulary must never regress to it.
    assert out["failureClass"] != "invalid"


# ------------------------------------------------------ committed-manifest floors
#
# `minHarnessRows` protects the manifest against a truncated row list, but
# nothing protected the floors themselves. An edit or a bad merge zeroing every
# `minCases` would disable the whole case-floor mechanism and every run would
# still report green.

# Hard-coded on purpose, and deliberately NOT derived from the manifest: a
# tripwire that reads the value it guards is satisfied by editing that value.
# The measured sum at the time of writing is 2,383 across 56 rows. 1,800 sits
# about a quarter below it, which is far more headroom than any legitimate
# reduction has ever needed while still catching a mass zeroing (which lands at
# 0) or a broad silent erosion. Raise it deliberately when the estate grows.
MIN_TOTAL_CASE_FLOOR = 1800
MIN_COMMITTED_HARNESS_ROWS = 50


def _committed_manifest():
    return json.loads(
        (REPO / "tests" / "authoritative-test-manifest.json").read_text(
            encoding="utf-8"
        )
    )


def test_no_committed_case_floor_can_be_zeroed():
    rows = _committed_manifest()["harnesses"]
    zeroed = [r["name"] for r in rows if not isinstance(r["minCases"], int)
              or r["minCases"] < 1]
    assert not zeroed, f"these rows carry no usable floor: {zeroed}"


def test_the_committed_case_floors_sum_above_a_hard_coded_bound():
    rows = _committed_manifest()["harnesses"]
    assert len(rows) >= MIN_COMMITTED_HARNESS_ROWS, len(rows)
    total = sum(r["minCases"] for r in rows)
    assert total >= MIN_TOTAL_CASE_FLOOR, (
        f"the committed case floors now sum to {total}, below the hard-coded "
        f"bound of {MIN_TOTAL_CASE_FLOOR}. Either the estate genuinely shrank "
        "and this literal must be lowered in the same reviewed commit, or the "
        "floors were zeroed."
    )


def test_only_a_variable_row_carries_a_count_axis():
    """`countPolicy` / `countAxis` are advisory to the reader, but a `variable`
    row that names no axis records no evidence for its own claim. `alerts` is
    currently the only variable row, asserted by name so a future one cannot be
    added without a deliberate edit here."""
    rows = _committed_manifest()["harnesses"]
    variable = {r["name"]: r.get("countAxis") for r in rows
                if r.get("countPolicy") == "variable"}
    assert variable == {"alerts": "uname-s"}, variable
    for row in rows:
        if row.get("countPolicy") != "variable":
            assert row.get("countPolicy") == "fixed", row
            assert "countAxis" not in row, row


def test_a_variable_row_without_an_axis_is_refused(tmp_path):
    obj = dict(MINIMAL)
    obj["harnesses"] = [
        dict(MINIMAL["harnesses"][0], countPolicy="variable")
    ]
    m = _manifest(tmp_path, obj)
    r = _run_lib(
        tmp_path,
        f'contract_manifest_load "{m}"; echo "$CONTRACT_CLASS $CONTRACT_LAST_CODE"',
    )
    assert "infrastructure manifest-invalid-row" in r.stdout, r.stdout + r.stderr


@pytest.mark.parametrize("bad", ["nine", None, -1, True])
def test_a_non_integer_floor_is_refused(tmp_path, bad):
    """`[ N -lt "nine" ]` is a shell arithmetic error, which is non-fatal — a
    mistyped floor would disable that row's check without failing anything."""
    obj = dict(MINIMAL)
    obj["harnesses"] = [dict(MINIMAL["harnesses"][0], minCases=bad)]
    m = _manifest(tmp_path, obj)
    r = _run_lib(
        tmp_path,
        f'contract_manifest_load "{m}"; echo "$CONTRACT_CLASS $CONTRACT_LAST_CODE"',
    )
    assert "infrastructure manifest-invalid-row" in r.stdout, r.stdout + r.stderr


# ------------------------------------------------------- the reason-code registry
#
# Codes are bare string literals at each emission site, so nothing in the shell
# can catch a typo. The registry in bin/_lib-test-contract.sh is the declared
# vocabulary and this pair of assertions makes it load-bearing in both
# directions.

_EMISSION_PATTERNS = (
    r"contract_fail\s+\w+\s+([a-z][a-z0-9-]+)",
    r"contract_carrier_synth\s+([a-z][a-z0-9-]+)",
    r'synth\("([a-z][a-z0-9-]+)"',
    r'print\("(manifest-[a-z0-9-]+)',
    r'"(manifest-[a-z0-9-]+)"\)',
)

_CODE_SOURCES = (
    "bin/_lib-test-contract.sh",
    "bin/cctally-test-all",
    "bin/cctally-test-remote",
)


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
        harness_exit={"alpha": 1},
        manifest_min={"beta": 99},
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


def test_the_human_summary_states_the_verdict_on_a_floor_breach(tmp_path):
    """`Total:` counts only summary-line failures, and a run refused for
    `case-floor-unmet` has none — the scrollback would otherwise print
    `failed=0` directly above an exit 3."""
    est = _estate(tmp_path, manifest_min={"alpha": 999})
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "failed=0" in r.stdout, r.stdout
    assert "Verdict: FAIL" in r.stdout, r.stdout
    assert "incomplete" in r.stdout, r.stdout
    assert "case-floor-unmet" in r.stdout, r.stdout


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
    e = {"PATH": os.environ["PATH"], "CCTALLY_TEST_ALL_PLAN": "1",
         "CCTALLY_TEST_ALL_FAKE_NCPU": "16"}
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
        env={
            "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
            "CCTALLY_TEST_ALL_PLAN": "1",
            "CCTALLY_AUTHORITATIVE_RUN": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "harnesses=" in r.stdout
    assert not (est / "outcome.json").exists()
