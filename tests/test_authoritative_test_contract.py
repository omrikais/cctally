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

from helpers.authoritative_runtime_budget import write_budget
from tests import _estate_stub

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
    # POPPED, not merely defaulted. The local escape hatch exports this variable
    # into every child when `agentmem` is absent — the exact path M4 exists to
    # support — so a suite run through CCTALLY_TEST_LOCAL=1 on such a machine
    # inherited it here and turned the control case below into an environmental
    # failure. `_run_nested_pytest` pops CCTALLY_AGENTMEM_TEST_POLICY and the
    # leaked xdist variables for the same reason.
    e.pop("CCTALLY_TEST_EXTERNAL_INCOMPLETE", None)
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


# ------------------------------------------- the loader's two remaining holes
#
# #650 items 2 and 3. Both are about a manifest the loader cannot read or must
# not accept, and both were reachable while the run still failed closed — which
# is why neither was caught by the exit-code matrix above. What was wrong was
# the CAUSE the operator reads, not the verdict.


def test_a_non_utf8_manifest_is_refused_with_a_named_cause(tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError.

    `open(path, encoding="utf-8").read()` inside `except OSError` therefore let
    a non-UTF-8 manifest escape the handler: the run still failed closed, but
    the operator read an empty cause beside a raw Python traceback.
    """
    m = tmp_path / "manifest.json"
    m.write_bytes(b'{"schemaVersion": 1, "harnesses": [], "\xff\xfe": 2}')
    r = _run_lib(
        tmp_path,
        f'contract_manifest_load "{m}"; '
        'echo "$CONTRACT_CLASS $CONTRACT_LAST_CODE"; '
        'contract_print_diagnostics',
    )
    assert "infrastructure manifest-unreadable" in r.stdout
    assert "not UTF-8" in r.stderr, r.stderr
    assert "Traceback" not in r.stderr, r.stderr


def test_an_absent_manifest_reports_the_operating_system_cause(tmp_path):
    """The manifest loader adopts contract_budget_load's cause reporting.

    The budget loader already reported `unreadable:%s` with `exc.strerror`
    while this one printed a bare `manifest-unreadable:` that named nothing, so
    a missing file and an unreadable directory read identically.
    """
    r = _run_lib(
        tmp_path,
        f'contract_manifest_load "{tmp_path}/absent.json"; contract_print_diagnostics',
    )
    assert "manifest-unreadable" in r.stderr
    assert "No such file or directory" in r.stderr, r.stderr


# The harness-name grammar. Names are serialized into tab-and-newline separated
# tables, consumed through shell word splitting, and counted by word splitting
# in the tier partition, so whitespace and glob metacharacters are each unsafe
# on a word-split path.
GRAMMAR_REJECTS = [
    ("whitespace", "alpha bravo"),
    ("tab", "alpha\tbravo"),
    ("glob-star", "alpha*"),
    ("glob-question", "alph?"),
    ("glob-class", "alpha[ab]"),
    ("uppercase", "Alpha"),
    ("underscore", "alpha_bravo"),
    ("leading-dash", "-alpha"),
    ("trailing-dash", "alpha-"),
    ("double-dash", "alpha--bravo"),
    ("dot", "alpha.bravo"),
    ("slash", "alpha/bravo"),
    ("newline", "alpha\nbravo"),
]


@pytest.mark.parametrize("label,name", GRAMMAR_REJECTS, ids=[c[0] for c in GRAMMAR_REJECTS])
def test_a_harness_name_outside_the_grammar_is_refused(tmp_path, label, name):
    obj = dict(MINIMAL)
    obj["harnesses"] = [dict(MINIMAL["harnesses"][0], name=name)]
    m = _manifest(tmp_path, obj)
    r = _run_lib(
        tmp_path,
        f'contract_manifest_load "{m}"; echo "$CONTRACT_CLASS $CONTRACT_LAST_CODE"',
    )
    assert "infrastructure manifest-invalid-name" in r.stdout, r.stdout + r.stderr


@pytest.mark.parametrize("name", ["a", "alpha", "alpha-bravo", "five-hour-blocks", "s3", "a1-b2-c3"])
def test_a_conforming_harness_name_is_accepted(tmp_path, name):
    """The green control. A refusal that fired on everything would satisfy the
    matrix above while refusing the committed manifest."""
    obj = dict(MINIMAL)
    obj["harnesses"] = [dict(MINIMAL["harnesses"][0], name=name)]
    m = _manifest(tmp_path, obj)
    r = _run_lib(tmp_path, f'contract_manifest_load "{m}" && echo OK')
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


def test_every_committed_harness_name_conforms_to_the_grammar():
    """Stated over the committed file directly, not only through the loader.

    `test_committed_manifest_parses` would also redden if a committed name
    stopped conforming, but it would redden for `manifest-invalid-name` with no
    statement of which name or which rule — and this is the file a maintainer
    edits.
    """
    doc = json.loads(
        (REPO / "tests" / "authoritative-test-manifest.json").read_text(encoding="utf-8")
    )
    bad = [
        row["name"] for row in doc["harnesses"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", row["name"])
    ]
    assert not bad, bad


# ------------------------------------------ the tier partition's count backstop
#
# `bin/cctally-test-all` fails closed into the whole estate when its selected
# and omitted lists are not an exact partition of the manifest. Once
# contract_manifest_load enforces the name grammar above, NO manifest the loader
# accepts can reach that branch: the manifest count counts LINES and the two
# other counts count IFS-split WORDS, and the grammar is exactly what makes
# those three agree for every accepted name.
#
# The branch stays, because it is the backstop for a future divergence and
# because it fails closed into the full estate rather than refusing a run. It is
# driven HERE through the pure helper the aggregator calls, with a name the
# loader would now refuse, so the test is honest about why the branch is
# otherwise unreachable rather than pretending a valid manifest reaches it.


def _partition(tmp_path, names, selected, omitted):
    r = _run_lib(
        tmp_path,
        "if contract_partition_counts_agree "
        f"{shlex.quote(names)} {shlex.quote(selected)} {shlex.quote(omitted)}; "
        'then echo "AGREE"; else echo "DISAGREE"; fi; '
        'echo "$CONTRACT_PARTITION_MANIFEST_COUNT '
        '$CONTRACT_PARTITION_SELECTED_COUNT $CONTRACT_PARTITION_OMITTED_COUNT"',
    )
    assert r.returncode == 0, r.stdout + r.stderr
    verdict, counts = r.stdout.split()[0], r.stdout.split()[1:]
    return verdict, [int(c) for c in counts]


def test_the_partition_helper_agrees_on_an_exact_partition(tmp_path):
    verdict, counts = _partition(
        tmp_path, "alpha\nbravo\ncharlie", "alpha bravo ", "charlie "
    )
    assert verdict == "AGREE"
    assert counts == [3, 2, 1]


def test_the_partition_helper_disagrees_on_a_word_split_name(tmp_path):
    """The one input that reaches the aggregator's widen-to-the-whole-estate
    backstop, and the reason it is driven through the helper rather than
    through a manifest: `contract_manifest_load` now refuses this name."""
    verdict, counts = _partition(tmp_path, "alpha bravo\ncharlie", "alpha bravo charlie ", "")
    assert verdict == "DISAGREE"
    assert counts == [2, 3, 0]


def test_the_partition_helper_disagrees_on_a_dropped_name(tmp_path):
    verdict, counts = _partition(tmp_path, "alpha\nbravo\ncharlie", "alpha ", "bravo ")
    assert verdict == "DISAGREE"
    assert counts == [3, 1, 1]


def test_the_partition_helper_leaves_pathname_expansion_off(tmp_path):
    """A `*` in a list must not expand against the working directory. The
    aggregator disables globbing around its own walks for exactly this reason,
    and a helper that re-enabled it would count whatever the scratch directory
    happens to contain."""
    (tmp_path / "decoy-one").write_text("x")
    (tmp_path / "decoy-two").write_text("x")
    verdict, counts = _partition(tmp_path, "decoy*", "decoy* ", "")
    assert verdict == "AGREE"
    assert counts == [1, 1, 0]


def test_the_aggregator_calls_the_partition_helper():
    """The seam is load-bearing only if the aggregator uses it.

    Without this, the helper above could be a second, parallel spelling of the
    arithmetic and the cases driving it would prove nothing about the branch
    they claim to reach.
    """
    text = (BIN / "cctally-test-all").read_text(encoding="utf-8")
    assert "contract_partition_counts_agree" in text


# --------------------------------- contract_check_runtime_budget's two guards
#
# Both are correct defensive code that the shipped wiring cannot reach.
# Eligibility at check time implies eligibility at admission, where a missing
# budget already refused the run, and an eligible run that passed no cases at
# all does not reach a green class. They are driven directly so that the
# classification and the SUBJECT each guard reports are pinned: the two share a
# reason code and are told apart only by the subject.

_BUDGET_ELIGIBLE = {
    "CCTALLY_AUTHORITATIVE_RUN": "1",
    "CCTALLY_TEST_LOCAL": "",
}

_BUDGET_PRELUDE = (
    "COVERAGE_MODE=full\n"
    "CONTRACT_PROFILE=private\n"
    "CONTRACT_CLASS=none\n"
    "CONTRACT_PYTEST_UNPARSED=0\n"
)


def _budget_check(tmp_path, snippet):
    r = _run_lib(
        tmp_path,
        _BUDGET_PRELUDE + snippet + "\n"
        "contract_check_runtime_budget\n"
        'printf "%s" "$CONTRACT_REASONS"',
        env=_BUDGET_ELIGIBLE,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    rows = [ln.split("\t") for ln in r.stdout.splitlines() if ln.strip()]
    return r, rows


def test_an_unloaded_threshold_is_recorded_as_unavailable_not_satisfied(tmp_path):
    r, rows = _budget_check(
        tmp_path,
        'CONTRACT_BUDGET_MAX=""\nCONTRACT_PASSED=10\nCONTRACT_PYTEST_PASSED=10',
    )
    assert rows == [["runtime-budget-unavailable", "budget", "threshold"]], r.stdout


def test_a_run_that_passed_no_cases_is_recorded_as_unavailable(tmp_path):
    r, rows = _budget_check(
        tmp_path,
        "CONTRACT_BUDGET_MAX=120\nCONTRACT_PASSED=0\nCONTRACT_PYTEST_PASSED=0",
    )
    assert rows == [["runtime-budget-unavailable", "budget", "denominator"]], r.stdout


def test_the_two_guards_are_green_when_both_inputs_are_present(tmp_path):
    """The control. A check that classified everything as unavailable would
    satisfy both cases above."""
    r, rows = _budget_check(
        tmp_path,
        "CONTRACT_BUDGET_MAX=1000000\nCONTRACT_PASSED=10\nCONTRACT_PYTEST_PASSED=10",
    )
    assert rows == [], r.stdout


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
    sentinel=False,
    harness_sleep=None,
    budget=None,
    tier=False,
    estate_report=None,
    estate_exit=None,
    estate_plan_exit=None,
):
    """A known-green scratch estate; each case mutates exactly one property."""
    repo = tmp_path / "estate"
    bindir = repo / "bin"
    testsdir = repo / "tests"
    bindir.mkdir(parents=True)
    testsdir.mkdir()

    shutil.copy2(RUNNER, bindir / "cctally-test-all")
    # Matched as a CLASS, like the kernels below: bin/_lib-test-contract.sh
    # sources bin/_lib-fts5-probe.sh and refuses without it (#529 S6, exception
    # X2), so an estate that copied only the contract by name would refuse to
    # start the moment the contract grew a second shared library.
    for lib in sorted(BIN.glob("_lib-*.sh")):
        shutil.copy2(lib, bindir / lib.name)
    # bin/cctally-test-all imports the evidence kernels, so a scratch estate
    # that omitted them would refuse to start (#529 S2). Matched as a CLASS
    # rather than listed one at a time: the vocabulary producer is
    # maintainer-local, so naming it here would break the mirrored suite.
    for kernel in sorted(BIN.glob("_lib_test_*.py")):
        shutil.copy2(kernel, bindir / kernel.name)
    # #648 D10. The class glob above acquires the estate checker automatically,
    # and this estate records no artifact for it to compare against, so 56 cases
    # here failed at admission on the fixture instead of on their own property.
    # `estate_report` is how a case asks for a specific report instead of the
    # clean one, which is what exercises the real contract mapping.
    _estate_stub.install(bindir, report=estate_report, exit_code=estate_exit,
                         plan_exit_code=estate_plan_exit)
    # #648 D7. Both pytest legs load `-p tests._estate_leg_plugin`, so a scratch
    # estate that omitted it would fail on a missing module. It is copied rather
    # than made conditional on existing: a file-existence guard around the proof
    # is exactly the shape D7 removed from the leg construction.
    shutil.copy2(REPO / "tests" / "_estate_leg_plugin.py",
                 testsdir / "_estate_leg_plugin.py")

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
        if tier:
            # ONE literal tests/fixtures/<name> reference, which is the scrape
            # form tests/test_harness_ownership.py::
            # test_scraper_finds_a_literal_fixture_reference pins. That is what
            # lets the ownership row below be written directly without encoding
            # a convention this file does not otherwise test: a row that
            # disagreed with the scrape fails bin/cctally-test-owners --verify,
            # and the tier then WIDENS, so a wrong row shows up as a visibly
            # widened selection rather than as a silently passing one.
            body.append(f': "$REPO_ROOT/tests/fixtures/{name}/seed.json"')
        if name in harness_sleep:
            # Wall time a case can rely on. The normalized budget's numerator is
            # `$SECONDS`, an integer, so a run that finishes inside one second
            # can record a zero numerator and satisfy any positive threshold —
            # which would make a breach case pass for the wrong reason.
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

    # #630 S7, one spelling since #650 item 5. `budget` is a number to pin the
    # maximum, a string to write the file verbatim, or False to omit it.
    write_budget(testsdir, budget)

    if tier:
        # #630 S7. The tier attributes its change set through
        # bin/cctally-test-owners and runs that tool ITSELF before accepting any
        # narrow result, so a tier estate carries the tool, the ownership rows,
        # the fixture directories those rows declare, and a git repository —
        # the basis is a merge-base computation unioned with working-tree dirt.
        shutil.copy2(BIN / "cctally-test-owners", bindir / "cctally-test-owners")
        (bindir / "cctally-test-owners").chmod(0o755)
        public_rows, private_rows = {}, {}
        for name in names:
            row = {
                "fixturePaths": [f"tests/fixtures/{name}"],
                "sourcePaths": [],
            }
            if manifest_visibility.get(name, "public") == "public":
                public_rows[name] = row
            else:
                private_rows[name] = row
            fixture = testsdir / "fixtures" / name
            fixture.mkdir(parents=True, exist_ok=True)
            (fixture / "seed.json").write_text("{}\n", encoding="utf-8")
        (testsdir / "harness-ownership.json").write_text(
            json.dumps({"schemaVersion": 1, "harnesses": public_rows}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        if private:
            (testsdir / "harness-ownership.private.json").write_text(
                json.dumps(
                    {"schemaVersion": 1, "harnesses": private_rows}, indent=2
                )
                + "\n",
                encoding="utf-8",
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
    if tier:
        # LAST, so every file above is committed and the working tree starts
        # clean. A case then dirties exactly what it means to attribute.
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t",
             "commit", "-q", "-m", "seed"],
            check=True,
        )
    return repo


def _drive(est, env=None, args=(), timeout=180, interpreter=None):
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


def test_binary_harness_log_is_classified_and_outcome_is_emitted(tmp_path):
    """A NUL-bearing log must fail closed before numeric parsing can abort bash."""
    est = _estate(tmp_path)
    alpha = est / "bin" / "cctally-alpha-test"
    alpha.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'passed: 5   failed: 0\\0\\n'\n",
        encoding="utf-8",
    )
    alpha.chmod(0o755)
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure", out
    assert out["outcome"] == "fail", out
    assert out["exitCode"] == 3, out
    assert any(
        reason == {"code": "binary-log", "phase": "harness", "subject": "alpha"}
        for reason in out["reasons"]
    ), out["reasons"]
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
    est = _estate(tmp_path, harnesses=["alpha"], manifest_names=["alpha", "beta"])
    r = _drive(est)
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
    est = _estate(tmp_path, harnesses=["alpha"], manifest_names=["alpha", "beta"])
    r = _drive(est)
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
VALID_PHASES = {"admission", "harness", "pytest", "transport", "budget"}


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


# --- the normalized runtime budget (#630 S7) --------------------------------
#
# The metric already existed: the outcome record publishes `wallSeconds`,
# `passedCases` and their quotient. What did not exist was a threshold, a check
# or a classification. The threshold is COMMITTED, in
# tests/authoritative-runtime-budget.json, so a maintainer raising it does so in
# a commit a reviewer can see — which is the mechanism that stops ordinary
# estate growth from silently breaching it.

COMMITTED_BUDGET = REPO / "tests" / "authoritative-runtime-budget.json"


def _budget_load(tmp_path, payload=None, path=None):
    """Call the REAL contract_budget_load and report everything it recorded."""
    if path is None:
        path = tmp_path / "budget.json"
        path.write_text(payload, encoding="utf-8")
    r = _run_lib(
        tmp_path,
        f'contract_budget_load "{path}"\n'
        'rc=$?\n'
        'printf "rc=%s\\n" "$rc"\n'
        'printf "class=%s\\n" "$CONTRACT_CLASS"\n'
        'printf "max=%s\\n" "$CONTRACT_BUDGET_MAX"\n'
        'printf "note=%s" "$CONTRACT_DIAGNOSTICS"\n'
        'printf "\\nreasons=%s" "$CONTRACT_REASONS"\n',
    )
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    fields = {}
    for line in r.stdout.split("\n"):
        key, _, value = line.partition("=")
        if key in {"rc", "class", "max", "note"}:
            fields[key] = value
    reasons = []
    for row in r.stdout.split("reasons=", 1)[1].split("\n"):
        if row.strip():
            reasons.append(row.split("\t"))
    fields["codes"] = [row[0] for row in reasons]
    fields["phases"] = [row[1] for row in reasons if len(row) > 1]
    # The loader's own stderr, so a case can assert that a refusal reached the
    # operator as a sentence rather than as a Python traceback.
    fields["stderr"] = r.stderr
    return fields


@pytest.mark.parametrize(
    "payload,cause",
    [
        ('{"schemaVersion": 2, "metric": "secondsPerThousandCases",'
         ' "maxSecondsPerThousandCases": 120}', "schemaVersion"),
        ('{"schemaVersion": 1, "metric": "wrong",'
         ' "maxSecondsPerThousandCases": 120}', "metric"),
        ('{"schemaVersion": 1, "metric": "secondsPerThousandCases"}', "max"),
        ('{"schemaVersion": 1, "metric": "secondsPerThousandCases",'
         ' "maxSecondsPerThousandCases": "120"}', "max"),
        ('{"schemaVersion": 1, "metric": "secondsPerThousandCases",'
         ' "maxSecondsPerThousandCases": 0}', "max"),
        ('{"schemaVersion": 1, "metric": "secondsPerThousandCases",'
         ' "maxSecondsPerThousandCases": -5}', "max"),
        ('{"schemaVersion": 1, "metric": "secondsPerThousandCases",'
         ' "maxSecondsPerThousandCases": true}', "max"),
        ('{"schemaVersion": 1, "metric": "secondsPerThousandCases",'
         ' "maxSecondsPerThousandCases": 120, "extra": 1}', "extra"),
        ('{"schemaVersion": 1, "schemaVersion": 1,'
         ' "metric": "secondsPerThousandCases",'
         ' "maxSecondsPerThousandCases": 120}', "schemaVersion"),
        # A real cause fragment on both of these, not "". `assert cause in
        # got["note"]` is trivially true of the empty string, so a row passing
        # `""` asserts nothing about what the operator was told.
        ("not json at all", "not JSON"),
        ("[1, 2, 3]", "not an object"),
        # #630 S7 / Task 2 review. Python's json parses the bare `NaN` and
        # `Infinity` literals by default, and BOTH pass a `value <= 0` guard,
        # because every comparison against a NaN is false and infinity is not
        # below zero. A budget of either then makes `raw > cap` false for every
        # run that can exist, so no run can ever breach it — a silent disabling
        # of the whole mechanism, in a design whose entire point is failing
        # closed. `-Infinity` is a THIRD finiteness case, not a sign control:
        # the loader tests finiteness before the sign, so `not
        # math.isfinite(value) or value <= 0` short-circuits and the sign test
        # never sees it. The sign controls are the `0` and `-5` rows above.
        ('{"schemaVersion": 1, "metric": "secondsPerThousandCases",'
         ' "maxSecondsPerThousandCases": NaN}', "max"),
        ('{"schemaVersion": 1, "metric": "secondsPerThousandCases",'
         ' "maxSecondsPerThousandCases": Infinity}', "max"),
        ('{"schemaVersion": 1, "metric": "secondsPerThousandCases",'
         ' "maxSecondsPerThousandCases": -Infinity}', "max"),
    ],
)
def test_a_malformed_budget_is_refused(tmp_path, payload, cause):
    """#630 S7. A budget that cannot be evaluated must never read as a budget
    that was satisfied, so an unreadable file refuses the run in seconds rather
    than being skipped after twenty minutes. The strictness matches what
    contract_manifest_load already applies to the estate manifest: unknown
    keys, duplicate keys, missing keys, non-numeric and non-positive values."""
    got = _budget_load(tmp_path, payload)
    assert got["rc"] == "1", got
    assert got["class"] == "infrastructure", got
    assert got["codes"] == ["runtime-budget-unavailable"], got
    assert got["phases"] == ["admission"], got
    assert got["max"] == "", "a refused budget must leave no threshold behind"
    assert cause in got["note"], got


def test_a_valid_budget_yields_its_maximum(tmp_path):
    """The green control. A guard that only ever sees red cannot be told apart
    from one that refuses everything."""
    got = _budget_load(
        tmp_path,
        '{"schemaVersion": 1, "metric": "secondsPerThousandCases",'
        ' "maxSecondsPerThousandCases": 120}',
    )
    assert got["rc"] == "0", got
    assert got["class"] == "none", got
    assert got["codes"] == [], got
    assert float(got["max"]) == 120.0, got


def test_a_non_utf8_budget_names_its_cause_instead_of_tracing(tmp_path):
    """#630 S7 / Task 1 review. UnicodeDecodeError is a ValueError, not an
    OSError, so a non-UTF-8 file used to escape the loader's handler: the run
    was still refused, but the operator got `runtime budget:  — fix ...` with an
    EMPTY cause beside a raw Python traceback. The refusal must name what is
    wrong with the file."""
    path = tmp_path / "budget.json"
    path.write_bytes(b'{"schemaVersion": 1, "metric": "\xff\xfe"}\n')
    got = _budget_load(tmp_path, path=path)
    assert got["rc"] == "1", got
    assert got["class"] == "infrastructure", got
    assert got["codes"] == ["runtime-budget-unavailable"], got
    assert got["phases"] == ["admission"], got
    assert "UTF-8" in got["note"], got
    # The second half, in the opposite direction. The escaping handler also
    # refused, so only the two together separate "refused with a cause" from
    # "refused with an empty cause beside 211 bytes of traceback".
    assert "Traceback" not in got["stderr"], got["stderr"]


def test_an_absent_budget_file_is_refused(tmp_path):
    got = _budget_load(tmp_path, path=tmp_path / "there-is-no-such-file.json")
    assert got["rc"] == "1", got
    assert got["codes"] == ["runtime-budget-unavailable"], got


def test_the_committed_budget_parses(tmp_path):
    """The file that ships must satisfy its own loader."""
    got = _budget_load(tmp_path, path=COMMITTED_BUDGET)
    assert got["rc"] == "0", got
    assert got["codes"] == [], got
    assert float(got["max"]) > 0, got


def test_the_committed_budget_is_the_number_the_spec_derived():
    """Hard-coded on purpose and deliberately NOT read from the file it guards.

    120 comes from docs/superpowers/audits/2026-08-23-630-s3-runs.tsv: the worst
    contended shipped observation on either host is 87.3 s per 1,000 cases and
    the worst degraded control is 103.2, so 120 clears both with headroom while
    no healthy run approaches it. Raising it is a deliberate act in a reviewable
    commit; this assertion is what makes the raise visible rather than silent.
    """
    doc = json.loads(COMMITTED_BUDGET.read_text(encoding="utf-8"))
    assert doc["schemaVersion"] == 1, doc
    assert doc["metric"] == "secondsPerThousandCases", doc
    assert doc["maxSecondsPerThousandCases"] == 120, doc


# The eligibility predicate. Keyed on the AUTHORITATIVE marker rather than on
# CCTALLY_REMOTE_EXEC: .github/workflows/ci.yml's test-macos job sets
# CCTALLY_AUTHORITATIVE_RUN=1 and executes bin/cctally-test-all DIRECTLY, never
# through the wrapper, so a remote-marker predicate would leave the budget
# unenforced on exactly the run that has no receipt to fall back on.


@pytest.mark.parametrize(
    "authoritative,mode,cls,local,profile,expected",
    [
        (True, "full", "none", False, "private", True),
        (False, "full", "none", False, "private", False),         # not authoritative
        (True, "subset", "none", False, "private", False),        # a deliberate subset
        (True, "tier-fast", "none", False, "private", False),     # a tier is not a full run
        (True, "linux-matrix", "none", False, "private", False),
        (True, "full", "product", False, "private", False),       # already red
        (True, "full", "infrastructure", False, "private", False),
        (True, "full", "incomplete", False, "private", False),
        (True, "full", "none", True, "private", False),           # the local hatch
        (True, "full", "none", False, "public", False),           # a public tree
    ],
)
def test_budget_eligibility(
    tmp_path, authoritative, mode, cls, local, profile, expected
):
    """The five conditions, one case per row.

    Every negative row differs from the positive row in exactly ONE axis, so
    deleting any single condition from the predicate fails at least one row.

    The class condition is what stops one failure from producing two reason
    codes: a red run has fewer passed cases, so the quotient inflates precisely
    when it carries no information. The profile condition (#630 S7 revision 3)
    is what keeps the threshold off a PUBLIC tree: `bin/cctally-test-linux-matrix
    --profile public` and ci.yml's `test-pr` job on the public mirror both set
    the authoritative marker and resolve to `full`, and both run a smaller
    estate on hardware the 120 never measured.
    """
    r = _run_lib(
        tmp_path,
        f"COVERAGE_MODE={mode}\n"
        f"CONTRACT_CLASS={cls}\n"
        f"CONTRACT_PROFILE={profile}\n"
        "if contract_budget_is_eligible; then echo ELIGIBLE; else echo NO; fi\n",
        env={
            # Set EXPLICITLY in both directions rather than defaulted. Under the
            # full suite the aggregator itself runs with CCTALLY_AUTHORITATIVE_RUN=1
            # and every pytest child inherits it, so a case that merely omitted
            # the variable would read the outer run's marker and pass standalone
            # while failing inside the suite.
            "CCTALLY_AUTHORITATIVE_RUN": "1" if authoritative else "",
            "CCTALLY_TEST_LOCAL": "1" if local else "",
        },
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ("ELIGIBLE" if expected else "NO"), (r.stdout, r.stderr)


# The comparison itself. The RAW quotient, not the record's published
# `secondsPerThousandCases`, which is rounded to one decimal for display — a
# threshold decided by a display artifact is one nobody can reason about.


@pytest.mark.parametrize(
    "wall,cases,over",
    [
        (11994, 100_000, False),   # 119.94 raw; under the cap either way
        (12000, 100_000, False),   # exactly 120.0; strictly-greater PASSES it
        (12004, 100_000, True),    # 120.04 raw, which ROUNDS DOWN to 120.0
        (14270, 100_000, True),
    ],
)
def test_the_breach_boundary_uses_the_raw_quotient(tmp_path, wall, cases, over):
    r = _run_lib(tmp_path, f"contract_budget_verdict {wall} {cases} 120.0\n")
    assert r.returncode == 0, r.stderr
    raw, _, state = r.stdout.strip().partition(" ")
    assert state == ("over" if over else "ok"), (r.stdout, r.stderr)
    assert abs(float(raw) - wall / cases * 1000) < 1e-6, r.stdout


def test_the_rounded_metric_would_not_have_caught_the_boundary_breach(tmp_path):
    """The discriminating anchor for the case above, and the whole reason the
    basis is specified. 120.04 rounds to 120.0, which is not greater than
    120.0, so a check reading the record's published metric would pass this run
    while the raw quotient fails it. Without this assertion the boundary matrix
    above would be satisfied under either basis."""
    assert round(12004 / 100_000 * 1000, 1) == 120.0
    r = _run_lib(tmp_path, "contract_budget_verdict 12004 100000 120.0\n")
    assert r.stdout.split()[1] == "over", r.stdout


# The check, driven end to end through a real aggregator run.


def _budget_reasons(proc):
    return [x for x in _outcome(proc)["reasons"] if x["phase"] == "budget"]


def test_an_eligible_run_within_the_budget_records_no_budget_reason(tmp_path):
    """The green control for every red case below.

    Every driven budget case in this section is a PRIVATE estate, because the
    fifth eligibility condition is `CONTRACT_PROFILE == private` and a public
    scratch estate would be ineligible for a second, unrelated reason — which
    would leave each negative case below passing for the wrong one.
    """
    est = _estate(tmp_path, private=True)
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _budget_reasons(r) == [], _outcome(r)


def test_an_eligible_run_over_the_budget_is_product_at_exit_one(tmp_path):
    """#630 S7. The class is `product` rather than `infrastructure` because the
    instrument is working correctly and reporting a true fact about the tree;
    `infrastructure` means the instrument itself broke."""
    est = _estate(tmp_path, private=True, budget=0.001, harness_sleep={"alpha": 1})
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    out = _outcome(r)
    assert out["failureClass"] == "product", out
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert out["exitCode"] == 1, out
    rows = _budget_reasons(r)
    assert [x["code"] for x in rows] == ["runtime-budget-exceeded"], out
    assert ">" in rows[0]["subject"], rows
    # The operator is told what to do about it, not merely that it happened.
    assert "per 1,000 cases" in r.stderr, r.stderr


def test_the_breach_states_both_numbers_readably(tmp_path):
    """#630 S7 / Task 1 review. The comparison runs on the raw quotient, which
    is the whole point of contract_budget_verdict — but the operator was then
    shown that raw value verbatim, so a breach read
    `runtime-budget-exceeded(120.040000>120.0)`. Formatting is DISPLAY only and
    must not touch the comparison, which
    test_the_rounded_metric_would_not_have_caught_the_boundary_breach pins."""
    est = _estate(tmp_path, private=True, budget=0.001, harness_sleep={"alpha": 1})
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    subject = _budget_reasons(r)[0]["subject"]
    assert re.fullmatch(r"\d+(\.\d{1,2})? > 0\.001", subject), subject
    # The threshold reads as the number a maintainer typed into the committed
    # file, not as its float repr.
    assert "0.001000" not in r.stderr, r.stderr


def test_a_non_authoritative_run_over_the_budget_is_still_green(tmp_path):
    """The eligibility predicate is not decorative: the SAME estate that fails
    above must pass with the marker absent."""
    est = _estate(tmp_path, private=True, budget=0.001, harness_sleep={"alpha": 1})
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _budget_reasons(r) == [], _outcome(r)


def test_a_public_tree_over_the_budget_is_still_green(tmp_path):
    """#630 S7 revision 3. The SAME breaching estate, authoritative, `full`, and
    not the local hatch — differing from the failing case in the profile alone.

    Two real invocation paths reach exactly this state: the release runbook's
    `bin/cctally-test-linux-matrix --profile public` at every cut, and ci.yml's
    `test-pr` job, which runs on the PUBLIC MIRROR for contributor and fork pull
    requests. Both run a smaller estate on hardware the committed 120 never
    measured, so a breach there would blame a contributor's tree for a hosted
    runner's slowness."""
    est = _estate(tmp_path, private=False, budget=0.001, harness_sleep={"alpha": 1})
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _budget_reasons(r) == [], _outcome(r)
    assert _outcome(r)["coverage"]["mode"] == "full", _outcome(r)


def test_a_subset_over_the_budget_gains_no_budget_reason(tmp_path):
    """A deliberate subset is excluded by `coverage.mode`, and it could not be
    authoritative anyway — the aggregator refuses that combination outright."""
    est = _estate(tmp_path, private=True, harnesses=["alpha", "beta"], budget=0.001,
                  harness_sleep={"alpha": 1})
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert _budget_reasons(r) == [], _outcome(r)


def test_a_red_run_gains_no_budget_reason(tmp_path):
    """One failure must not produce two reason codes: a red run has fewer
    passed cases, so the quotient inflates exactly when it is meaningless.

    Non-vacuous by construction — the estate is the one
    test_an_eligible_run_over_the_budget_is_product_at_exit_one proves does
    breach, mutated only by making a harness fail."""
    est = _estate(
        tmp_path,
        private=True,
        budget=0.001,
        harness_sleep={"alpha": 1},
        harness_output={"alpha": "passed: 1   failed: 1\n"},
        harness_exit={"alpha": 1},
    )
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 1, r.stdout + r.stderr
    codes = _codes(r)
    assert "harness-failed" in codes, codes
    assert "runtime-budget-exceeded" not in codes, codes


def test_a_null_denominator_is_unavailable_not_a_breach(tmp_path):
    """passedCases is null when pytest's passed-item count was unreadable, which
    is a different answer from zero. An otherwise-green eligible run that cannot
    establish its denominator is infrastructure, never product — a budget that
    could not be evaluated must never read as a budget that was satisfied."""
    est = _estate(tmp_path, private=True)
    env = dict(_pytest_output_shim(est, r'b"no summary line here at all\n"'))
    env["CCTALLY_AUTHORITATIVE_RUN"] = "1"
    r = _drive(est, env=env)
    out = _outcome(r)
    assert out["passedCases"] is None, out
    assert [x["code"] for x in out["reasons"]] == ["runtime-budget-unavailable"], out
    assert out["failureClass"] == "infrastructure", out
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert out["exitCode"] == 3, out


def test_a_missing_budget_refuses_an_eligible_run_before_anything_runs(tmp_path):
    """Validated at ADMISSION, so a missing budget refuses the run in seconds
    rather than after twenty minutes of work whose verdict it cannot judge."""
    est = _estate(tmp_path, private=True, budget=False, sentinel=True)
    ran = tmp_path / "ran"
    ran.mkdir()
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1",
                         "SENTINEL_DIR": str(ran)})
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure", out
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert "runtime-budget-unavailable" in {x["code"] for x in out["reasons"]}, out
    assert _executed(ran) == set(), (
        "the run was refused at admission, so no harness may have executed"
    )


def test_a_malformed_budget_refuses_an_eligible_run(tmp_path):
    est = _estate(tmp_path, private=True, budget='{"schemaVersion": 1}\n')
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure", out
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert "runtime-budget-unavailable" in {x["code"] for x in out["reasons"]}, out


def test_an_ineligible_run_is_not_refused_for_a_budget_it_never_reads(tmp_path):
    """The admission load is gated by the SAME predicate the check is, so a run
    that will never compare itself against the budget is not refused for one it
    does not have."""
    est = _estate(tmp_path, private=True, budget=False)
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "runtime-budget-unavailable" not in _codes(r), _outcome(r)


def test_both_budget_reasons_are_registered():
    """A code absent from _CONTRACT_REASON_CODES is not a code: the emission
    sites are bare string literals, so nothing in the shell catches a typo."""
    codes = _registry()
    assert "runtime-budget-exceeded" in codes, sorted(codes)
    assert "runtime-budget-unavailable" in codes, sorted(codes)


# --- the estate check, driven end to end (#648 D6, D11, D12) ----------------
#
# The estate helper in these scratch trees is a stub (tests/_estate_stub.py):
# a scratch estate records no committed artifact, so the comparison itself has
# nothing to compare. Everything downstream of the report is real -- the
# aggregator's mode predicate, `contract_admit_estate`, the reason-code
# mapping, the failure class and the exit code.


def test_a_full_run_runs_the_estate_check(tmp_path):
    """#648 D12 -- a full run is citable, so it checks."""
    est = _estate(tmp_path)
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = _estate_stub.invocations(est)
    assert any("--check" in call for call in calls), calls


def test_a_harness_subset_does_not_run_the_estate_check(tmp_path):
    """#648 D12 -- POSITIVE evidence, through the stub's own invocation log.

    A subset already records `deliberate-subset` and exits 3 whatever happens,
    so the complete check adds no protection there and would slow the targeted
    loop this repository runs constantly. Asserted on a file that must not
    exist rather than on the absence of a diagnostic, because a diagnostic can
    be absent for reasons that have nothing to do with the check running.
    """
    est = _estate(tmp_path, harnesses=["alpha"])
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert _codes(r) == {"deliberate-subset"}, _outcome(r)
    assert _estate_stub.invocations(est) == []


def test_a_harness_subset_runs_the_estate_check_under_the_opt_in(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha"])
    r = _drive(est, args=("--harness", "alpha", "--with-estate-check"))
    assert r.returncode == 3, r.stdout + r.stderr
    calls = _estate_stub.invocations(est)
    assert any("--check" in call for call in calls), calls


def test_the_opt_in_flag_is_refused_without_a_harness(tmp_path):
    """It would change nothing on any other mode, so accepting it would accept
    a word the caller meant as a request."""
    est = _estate(tmp_path)
    r = _drive(est, args=("--with-estate-check",))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "requires at least one --harness" in r.stderr


def test_plan_mode_runs_no_estate_check(tmp_path):
    """Plan mode exits before admission, so it can yield no estate verdict.

    Driven without `_drive`, which pins `CCTALLY_TEST_ALL_OUTCOME_FILE`; the
    aggregator refuses that variable in plan mode, because a plan is never an
    authoritative outcome.
    """
    est = _estate(tmp_path)
    r = subprocess.run(
        [str(est / "bin" / "cctally-test-all")],
        env={"PATH": os.environ["PATH"], "CCTALLY_TEST_ALL_PLAN": "1",
             "CCTALLY_TEST_JOBS": "1"},
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert _estate_stub.invocations(est) == []


@pytest.mark.parametrize(
    "direction,axis,code",
    [
        ("missing", "pytestNodes", "manifest-missing-pytest-node"),
        ("unexpected", "pytestNodes", "manifest-unexpected-pytest-node"),
        ("missing", "frontendTests", "manifest-missing-frontend-test"),
        ("unexpected", "frontendTests", "manifest-unexpected-frontend-test"),
        ("missing", "suppressions", "manifest-missing-suppression"),
        ("unexpected", "suppressions", "manifest-unexpected-suppression"),
    ],
)
def test_each_axis_reports_its_own_directional_code(tmp_path, direction, axis,
                                                    code):
    """#648 D5, acceptance criteria 3 to 5 -- six codes, not one generic one."""
    est = _estate(
        tmp_path,
        estate_report=_estate_stub.drift_report(direction, axis, "row-under-test"),
    )
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert code in _codes(r), _outcome(r)
    assert _outcome(r)["failureClass"] == "infrastructure"


@pytest.mark.parametrize(
    "tag,code",
    [
        ("artifact-unreadable", "estate-artifact-unreadable"),
        ("discovery-failed", "estate-discovery-failed"),
        ("partition-invalid", "estate-partition-invalid"),
    ],
)
def test_each_inability_reports_its_own_code(tmp_path, tag, code):
    """#648 acceptance criterion 6 -- never a generic one."""
    est = _estate(
        tmp_path, estate_report=_estate_stub.inability_report(tag, "the reason"),
    )
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert code in _codes(r), _outcome(r)


def test_an_unauthorized_transition_reports_its_own_code(tmp_path):
    """#648 acceptance criteria 7 to 9."""
    est = _estate(
        tmp_path,
        estate_report=_estate_stub.unauthorized_report(
            "pytestNodes", "tests/test_a.py::test_gone"),
    )
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "estate-shrink-unauthorized" in _codes(r), _outcome(r)
    assert "tests/test_a.py::test_gone" in r.stderr


def test_an_inability_is_recorded_incomplete_on_a_non_authoritative_run(tmp_path):
    """#648 D12 -- a non-authoritative run records it and can never be green.

    It does NOT abort: the run continues and every harness still executes, so
    the operator gets the results alongside the reason the run is uncitable.
    Proven by the harness sentinel rather than by the exit code, which an abort
    would produce identically.
    """
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    est = _estate(
        tmp_path, sentinel=True,
        estate_report=_estate_stub.inability_report(
            "discovery-failed", "vitest is not installed"),
    )
    r = _drive(est, env={"SENTINEL_DIR": str(sentinels)})
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "incomplete", out
    assert "estate-discovery-failed" in _codes(r), out
    assert (sentinels / "alpha.ran").exists(), sorted(sentinels.iterdir())


def test_drift_aborts_even_on_a_non_authoritative_run(tmp_path):
    """#648 D12 -- "proven drift aborts" in every mode, authoritative or not.

    Drift is a fact about the tree, not about how much this run claims, so it
    is fatal wherever the check runs. Only INABILITY is graded by
    authoritativeness.
    """
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    est = _estate(
        tmp_path, sentinel=True,
        estate_report=_estate_stub.drift_report(
            "missing", "pytestNodes", "tests/test_a.py::test_gone"),
    )
    r = _drive(est, env={"SENTINEL_DIR": str(sentinels)})
    assert r.returncode == 3, r.stdout + r.stderr
    assert not (sentinels / "alpha.ran").exists(), sorted(sentinels.iterdir())


def test_a_truncated_report_is_an_inability_and_not_an_agreement(tmp_path):
    """FAIL CLOSED on the terminator.

    A checker killed halfway prints a well-formed prefix and no `end` line. A
    reader that judged only on the records it had seen would read that as
    agreement, which is the one reading this whole mechanism cannot afford.
    """
    est = _estate(
        tmp_path,
        estate_report="version\t1\nprofile\tpublic\n",
        estate_exit=0,
    )
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "estate-discovery-failed" in _codes(r), _outcome(r)


def test_a_checker_that_cannot_run_at_all_is_an_inability(tmp_path):
    est = _estate(tmp_path, estate_report="", estate_exit=3)
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "estate-discovery-failed" in _codes(r), _outcome(r)


def test_a_report_terminated_problems_that_names_none_is_an_inability(tmp_path):
    """FAIL CLOSED on the terminator's PAYLOAD, not merely on its presence.

    The reader matched `end` and never read the field beside it, so a report
    that says `problems` while carrying no record this shell recognises was
    read as agreement. Nothing produces that today, because `render_report`
    writes `end problems` only alongside a recognised problem record. A future
    record kind would produce exactly it, and the failure would be silent and
    green -- which is the one reading this mechanism cannot afford.
    """
    est = _estate(
        tmp_path,
        estate_report="version\t1\nprofile\tpublic\nnote\tsomething\nend\tproblems\n",
    )
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "estate-discovery-failed" in _codes(r), _outcome(r)


def test_a_record_kind_this_contract_does_not_recognise_is_an_inability(tmp_path):
    """The `case` had no default arm, so an unknown record kind was DROPPED.

    An unrecognised inability tag already lands in that arm's `*)` and is
    treated as an inability; the record kind one level above it had no such
    arm at all. A future problem record would therefore be discarded and the
    run would end green on a report that named a problem.
    """
    est = _estate(
        tmp_path,
        estate_report=(
            "version\t1\nprofile\tpublic\n"
            "future-problem\tpytestNodes\ta-row\n"
            "end\tok\n"
        ),
    )
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "estate-discovery-failed" in _codes(r), _outcome(r)


def _declare_one_leg(est, nodes):
    """Re-install the stub so the plan declares exactly these node identifiers."""
    _estate_stub.install(
        est / "bin",
        legs=[{"name": "pytest", "selectors": [], "nodes": list(nodes)}],
    )


def test_a_leg_that_collected_less_than_it_declared_fails_the_run(tmp_path):
    """#648 D7, acceptance criterion 18 -- what pytest ASKED for is not enough.

    Deriving argv from the declaration proves what pytest was asked to collect.
    It cannot prove what pytest DID collect, and M13 measured the gap: a
    narrowing that reaches the leg leaves admission observing the complete
    estate and the run exiting 0. The narrowing here is a conftest hook rather
    than `PYTEST_ADDOPTS`, because the sanitizer now removes that variable
    before the leg starts -- so the environment route can no longer produce the
    condition, and the proof needs a route that still can.
    """
    est = _estate(tmp_path)
    (est / "tests" / "test_narrowed.py").write_text(
        "def test_narrowed():\n    assert True\n", encoding="utf-8")
    (est / "tests" / "conftest.py").write_text(
        "def pytest_collection_modifyitems(config, items):\n"
        "    items[:] = [i for i in items if 'test_narrowed' not in i.nodeid]\n",
        encoding="utf-8")
    _declare_one_leg(est, ["tests/test_scratch_smoke.py::test_ok",
                           "tests/test_narrowed.py::test_narrowed"])
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "manifest-missing-pytest-node" in _codes(r), _outcome(r)
    assert "tests/test_narrowed.py::test_narrowed" in r.stderr


def test_a_leg_that_collected_its_whole_declared_share_passes(tmp_path):
    """The control arm. Without it the case above would pass on a comparison
    that failed for every run, which is not a comparison at all."""
    est = _estate(tmp_path)
    (est / "tests" / "test_narrowed.py").write_text(
        "def test_narrowed():\n    assert True\n", encoding="utf-8")
    _declare_one_leg(est, ["tests/test_scratch_smoke.py::test_ok",
                           "tests/test_narrowed.py::test_narrowed"])
    r = _drive(est)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_leg_with_no_observed_record_at_all_fails_closed(tmp_path):
    """A comparison whose observed half is missing is a comparison that did not
    happen, and it must never read as agreement."""
    est = _estate(tmp_path)
    _declare_one_leg(est, ["tests/test_scratch_smoke.py::test_ok"])
    (est / "tests" / "_estate_leg_plugin.py").write_text(
        "# the plugin this tree carries records nothing\n", encoding="utf-8")
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "manifest-missing-pytest-node" in _codes(r), _outcome(r)


def test_an_unexpandable_execution_declaration_fails_admission(tmp_path):
    """#648 acceptance criterion 17 -- a missing target is an admission failure.

    The retired shell literals were wrapped in `[ -f ... ]` guards, so a
    benchmark file that vanished produced a run that silently omitted the serial
    leg and still exited 0. The declaration is validated at admission instead,
    and a selector matching nothing refuses the run before anything launches.
    """
    est = _estate(tmp_path, estate_plan_exit=3)
    r = _drive(est)
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert "estate-partition-invalid" in _codes(r), out
    assert {x["subject"] for x in out["reasons"]
            if x["code"] == "estate-partition-invalid"} == {"pytestExecution"}, out
    assert "does not expand against the recorded estate" in r.stderr


def test_a_subset_with_pytest_still_validates_the_execution_declaration(tmp_path):
    """The complete check is exempt on a subset (D12); the declaration is not.

    The legs are built from it on every path that RUNS them, so a declaration
    that no longer expands must refuse on every such path. Proven by driving
    the exemption and the requirement in one run: no `--check` invocation, and
    an admission refusal anyway.
    """
    est = _estate(tmp_path, harnesses=["alpha"], estate_plan_exit=3)
    r = _drive(est, args=("--harness", "alpha", "--with-pytest"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "estate-partition-invalid" in _codes(r), _outcome(r)
    calls = _estate_stub.invocations(est)
    assert not any("--check" in call for call in calls), calls


def test_a_subset_without_pytest_validates_no_execution_declaration(tmp_path):
    """Nothing runs the legs, so nothing needs them to expand."""
    est = _estate(tmp_path, harnesses=["alpha"], estate_plan_exit=3)
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert _codes(r) == {"deliberate-subset"}, _outcome(r)


def test_the_estate_check_is_required_in_every_citable_mode(tmp_path):
    """#648 D12 -- the run-mode contract, stated rather than inherited.

    Every mode that can produce a green or citable result runs the complete
    structural check: a full run, `--tier-fast`, and the Linux matrix profile.
    The Linux profile cannot be driven end to end from a macOS runner, because
    a non-plan `CCTALLY_LINUX_MATRIX_RUN` refuses on `uname -s`, so the mode
    rule lives in a predicate that can be exercised directly instead of being
    an inline condition only one platform can reach.
    """
    for mode in ("full", "tier-fast", "linux-matrix"):
        r = _run_lib(
            tmp_path,
            f'if contract_estate_check_required {mode} 0; then echo YES; '
            f'else echo NO; fi',
        )
        assert r.stdout.strip() == "YES", (mode, r.stdout, r.stderr)


def test_a_harness_subset_runs_the_estate_check_only_on_the_opt_in(tmp_path):
    """#648 D12 -- a subset is already unconditionally uncitable.

    `--harness` records `deliberate-subset` and exits 3 whatever happens, so
    the complete check adds no protection against a green result there and
    would materially slow the targeted loop this repository runs constantly. It
    is available as an explicit diagnostic instead.
    """
    off = _run_lib(
        tmp_path,
        'if contract_estate_check_required subset 0; then echo YES; else echo NO; fi',
    )
    assert off.stdout.strip() == "NO", off.stdout + off.stderr
    on = _run_lib(
        tmp_path,
        'if contract_estate_check_required subset 1; then echo YES; else echo NO; fi',
    )
    assert on.stdout.strip() == "YES", on.stdout + on.stderr


def test_an_unknown_coverage_mode_still_requires_the_estate_check(tmp_path):
    """Fail closed on the mode axis too.

    A mode added later and not taught to this predicate must inherit the
    checking behaviour, never the exemption: the exemption is the thing that
    has to be argued for, and `subset` is the only mode that has argued for it.
    """
    r = _run_lib(
        tmp_path,
        'if contract_estate_check_required something-new 0; then echo YES; '
        'else echo NO; fi',
    )
    assert r.stdout.strip() == "YES", r.stdout + r.stderr


def test_every_estate_reason_code_is_registered():
    """#648 D5 -- ten codes, and every literal lives in the contract shell.

    Six directional codes, one pair per axis, plus four inability and
    authorization codes. The emission sites are bare string literals in shell,
    so nothing catches a typo; registration is the only check there is.
    """
    codes = _registry()
    expected = {
        "estate-artifact-unreadable",
        "estate-discovery-failed",
        "estate-partition-invalid",
        "estate-shrink-unauthorized",
        "manifest-missing-frontend-test",
        "manifest-missing-pytest-node",
        "manifest-missing-suppression",
        "manifest-unexpected-frontend-test",
        "manifest-unexpected-pytest-node",
        "manifest-unexpected-suppression",
    }
    assert expected <= codes, sorted(expected - codes)


def test_the_estate_codes_are_emitted_from_the_contract_shell_alone():
    """#648 D5 -- `_CODE_SOURCES` is not widened, so every literal is here.

    The helper returns TAGS and the shell maps them, which is what keeps the
    registration scan able to see all ten at their single emission site. A code
    emitted from the Python helper instead would be invisible to the scan and
    would fail the dead-code direction of the registration test.
    """
    import re

    text = (BIN / "_lib-test-contract.sh").read_text(encoding="utf-8")
    emitted = set(re.findall(r"contract_fail\s+\w+\s+([a-z][a-z0-9-]+)", text))
    for code in (
        "estate-artifact-unreadable", "estate-discovery-failed",
        "estate-partition-invalid", "estate-shrink-unauthorized",
        "manifest-missing-frontend-test", "manifest-missing-pytest-node",
        "manifest-missing-suppression", "manifest-unexpected-frontend-test",
        "manifest-unexpected-pytest-node", "manifest-unexpected-suppression",
    ):
        assert code in emitted, code


# --- the carrier's phase set is CLOSED (#630 S7) ----------------------------
#
# bin/cctally-test-remote reads the aggregator's record through
# contract_carrier_emit, which validates every reason row against its own phase
# set and rewrites an unlisted phase to `outcome-record-malformed` —
# infrastructure at exit 3. A reason whose phase the carrier does not know
# therefore loses BOTH its class and its exit code on the way to the caller,
# silently, and the record the caller sees names a transport fault rather than
# the thing that actually happened.


def _carrier_record(**overrides):
    doc = {
        "schemaVersion": 1,
        "outcome": "fail",
        "failureClass": "product",
        "exitCode": 1,
        "reasons": [],
        "capabilities": {},
        "totals": {"passed": 0, "failed": 0},
    }
    doc.update(overrides)
    return doc


def _carry(tmp_path, record, rc):
    """Run the real carrier over a record; return its object and exit code."""
    path = tmp_path / "carried-record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    r = _run_lib(tmp_path, f'contract_carrier_emit {rc} "{path}"')
    assert r.stdout.strip(), f"the carrier printed nothing\nstderr:\n{r.stderr}"
    return json.loads(r.stdout), r.returncode


@pytest.mark.parametrize(
    "code,cls,exit_code,subject",
    [
        ("runtime-budget-exceeded", "product", 1, "142.7 > 120"),
        # BOTH codes, because §2.5 asks for a carrier-level case for each and
        # the two travel different class and exit-code paths. The `budget` phase
        # is what the carrier validates, and it validates it per reason row.
        ("runtime-budget-unavailable", "infrastructure", 3, "denominator"),
    ],
)
def test_the_carrier_accepts_the_budget_phase(
    tmp_path, code, cls, exit_code, subject
):
    """#630 S7. The carrier's phase set is CLOSED: a reason carrying an
    unlisted phase is rewritten to outcome-record-malformed, which is
    infrastructure and exits 3. That would strip runtime-budget-exceeded of
    the product class and the exit 1 the whole design rests on, silently — and
    it would make runtime-budget-unavailable indistinguishable from a transport
    fault, which is the one thing it is not."""
    doc, rc = _carry(
        tmp_path,
        _carrier_record(
            failureClass=cls,
            exitCode=exit_code,
            reasons=[{"code": code, "phase": "budget", "subject": subject}],
        ),
        rc=exit_code,
    )
    assert doc["failureClass"] == cls, doc
    assert doc["exitCode"] == exit_code, doc
    assert rc == exit_code, (rc, doc)
    assert [r["code"] for r in doc["reasons"]] == [code], doc


def test_the_carrier_still_refuses_a_phase_outside_the_set(tmp_path):
    """The non-vacuity control. Opening the set to `budget` must not open it
    to everything: a set that accepts any string is not a closed set, and the
    rewrite above would then be unreachable for every real malformed record."""
    doc, rc = _carry(
        tmp_path,
        _carrier_record(
            failureClass="product",
            exitCode=1,
            reasons=[{"code": "harness-failed", "phase": "invented",
                      "subject": "x"}],
        ),
        rc=1,
    )
    assert doc["failureClass"] == "infrastructure", doc
    assert rc == 3, (rc, doc)
    assert [r["code"] for r in doc["reasons"]] == ["outcome-record-malformed"], doc


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
    row that names no axis records no evidence for its own claim. The variable
    rows are asserted BY NAME, so a future one cannot be added without a
    deliberate edit here.

    `alerts` varies on the platform, because several of its cases need macOS
    `osascript`. `preflight` varies on whether the host has a bash 3.x
    interpreter (#641): two of its cases parse a fixture against the declared
    bash 3.2 floor, and a Linux container legitimately has none, so they state a
    skip rather than passing silently. It prints 58 cases where one exists and
    56 where none does, and `minCases` is the 56 that holds on every lane. A
    single floor cannot protect the bash-3 axis, because a macOS run that
    silently stopped executing both gated cases would report 56 and still clear
    it; the harness asserts its own exact tally per axis for that reason
    (#655)."""
    rows = _committed_manifest()["harnesses"]
    variable = {r["name"]: r.get("countAxis") for r in rows
                if r.get("countPolicy") == "variable"}
    assert variable == {
        "alerts": "uname-s",
        "preflight": "bash3-availability",
    }, variable
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


def test_the_selected_list_is_in_manifest_order_not_caller_order(tmp_path):
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


def test_admission_still_catches_an_unexpected_harness_under_a_subset(tmp_path):
    """Admission compares the manifest against the harnesses on disk: it checks
    the ESTATE, not the executed set. Narrowing what executes must not narrow
    what is checked for existence."""
    est = _estate(tmp_path, harnesses=["alpha", "beta"], manifest_names=["alpha"])
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "manifest-unexpected-harness" in _codes(r)


def test_admission_still_catches_a_missing_harness_under_a_subset(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha"], manifest_names=["alpha", "beta"])
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "manifest-missing-harness" in _codes(r)


def test_admission_still_catches_a_lost_executable_bit_under_a_subset(tmp_path):
    est = _estate(
        tmp_path, harnesses=["alpha", "beta"], modes={"beta": 0o644}
    )
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "harness-not-executable" in _codes(r)


def test_a_selected_harness_keeps_its_case_floor(tmp_path):
    est = _estate(tmp_path, harnesses=["alpha", "beta"], manifest_min={"alpha": 999})
    r = _drive(est, args=("--harness", "alpha"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "case-floor-unmet" in _codes(r)


def test_an_omitted_harness_floor_is_not_evaluated(tmp_path):
    """A floor for a harness that did not run must not be recorded as met, and
    equally must not be recorded as breached."""
    est = _estate(tmp_path, harnesses=["alpha", "beta"], manifest_min={"beta": 999})
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
# the verdict contract cannot describe, because the carrier reads a status with
# nothing to corroborate it. Both LAN runners are Macs, so this is the real
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


# Every array a run can leave empty. The subset feature makes `all_harnesses`
# and `rows` emptiable, `parallel_harnesses` is empty for an estate carrying no
# pool harness, `subset_harnesses` is empty while it is being built, and
# `pytest_timeout_args` is empty whenever pytest-timeout is not installed.
EMPTIABLE_ARRAYS = (
    "all_harnesses",
    "parallel_harnesses",
    "pytest_timeout_args",
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


# --- #529 S6 M4 / exception X1: the external incomplete reason --------------
#
# The local escape hatch (bin/cctally-test-remote CCTALLY_TEST_LOCAL=1) is the
# one caller that supplies a reason code from outside. It runs the SAME contract
# the remote path runs, and when `agentmem` is genuinely absent it says so in
# this vocabulary rather than exiting 0 over tests that silently skipped.


def test_external_reason_accepts_the_one_allowlisted_literal(tmp_path):
    r = _run_lib(
        tmp_path,
        'contract_admit_external_reason\n'
        'printf "class=%s\\n" "$CONTRACT_CLASS"\n'
        'printf "reasons=%s" "$CONTRACT_REASONS"\n',
        env={"CCTALLY_TEST_EXTERNAL_INCOMPLETE": "agentmem-unavailable-local"},
    )
    assert r.returncode == 0, r.stderr
    assert "class=incomplete" in r.stdout, r.stdout
    assert "agentmem-unavailable-local" in r.stdout, r.stdout


def test_external_reason_is_absent_when_the_variable_is_unset(tmp_path):
    """The non-vacuity control: the reason must not appear on its own."""
    r = _run_lib(
        tmp_path,
        'contract_admit_external_reason\n'
        'printf "class=%s\\n" "$CONTRACT_CLASS"\n',
    )
    assert r.returncode == 0, r.stderr
    assert "class=none" in r.stdout, r.stdout


def test_external_reason_rejects_an_unknown_value(tmp_path):
    r = _run_lib(
        tmp_path,
        "contract_admit_external_reason\n",
        env={"CCTALLY_TEST_EXTERNAL_INCOMPLETE": "whatever-i-felt-like"},
    )
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "not an accepted external incomplete reason" in r.stderr, r.stderr


def test_external_reason_rejects_the_empty_value(tmp_path):
    """An empty selector is the failure class this estate has paid for twice.

    Set-but-empty must not fall through as "nothing set": the kernel reads a
    reason CODE from the environment, and an empty one names no degradation
    while looking like it names one.
    """
    r = _run_lib(
        tmp_path,
        "contract_admit_external_reason\n",
        env={"CCTALLY_TEST_EXTERNAL_INCOMPLETE": ""},
    )
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "empty value" in r.stderr, r.stderr


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
    passing standalone) and is the shape `tests/test_timing_budget_guard.py`
    refuses.
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


# --- the tiered coverage mode in the RECORD (#630 S7) ------------------------
#
# tests/test_test_all_scheduler.py exercises the SELECTION — the basis, the
# force-includes, the partition and every fail-closed leg — through plan mode.
# What lives here is the contract the carrier and the wrapper read: the outcome
# record a real tier run publishes, and the verdict it carries.


def _tier_estate(tmp_path, **kwargs):
    kwargs.setdefault("harnesses", ["alpha", "beta"])
    return _estate(tmp_path, private=True, tier=True, **kwargs)


def _tier_touch(est, name):
    """Dirty one harness's fixture directory, which is a DIRECT ownership edge
    and therefore the only class of evidence that narrows."""
    (est / "tests" / "fixtures" / name / "seed.json").write_text(
        '{"touched": true}\n', encoding="utf-8"
    )


def test_a_green_tier_run_is_class_none_at_exit_zero(tmp_path):
    """§3.6. A tier run is a real verdict about what it RAN.
    `contract_is_authoritative` is an execution-strength marker, not a
    completeness predicate; completeness is carried separately by coverage.mode
    and by the gate identity, which is what stops this being read as a verdict
    about the whole estate."""
    est = _tier_estate(tmp_path)
    _tier_touch(est, "alpha")
    r = _drive(est, args=("--tier-fast", "HEAD"),
               env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    out = _outcome(r)
    assert r.returncode == 0, r.stdout + r.stderr
    assert out["failureClass"] == "none", out
    assert out["outcome"] == "pass", out
    # Preserve-item 19. The tier is a NEW named mode, never a relaxation of the
    # subset's uncitable classification.
    assert "deliberate-subset" not in _codes(r), out


def test_a_tier_run_runs_the_estate_check(tmp_path):
    """#648 D12 -- `--tier-fast` carries its own gate identity and is citable,
    so it runs the complete check like a full run does."""
    est = _tier_estate(tmp_path)
    _tier_touch(est, "alpha")
    r = _drive(est, args=("--tier-fast", "HEAD"),
               env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    calls = _estate_stub.invocations(est)
    assert any("--check" in call for call in calls), calls


def test_an_estate_inability_aborts_an_authoritative_run(tmp_path):
    """#648 D12 -- an authoritative run refuses to START rather than verify
    less than it claims.

    The mirror image of the non-authoritative case: there the run continues and
    is recorded incomplete, here nothing executes at all. Proven by the harness
    sentinel, because both dispositions exit 3 and the exit code alone cannot
    tell them apart.
    """
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    est = _tier_estate(
        tmp_path, sentinel=True,
        estate_report=_estate_stub.inability_report(
            "discovery-failed", "the e2e fixture runtime could not be built"),
    )
    _tier_touch(est, "alpha")
    r = _drive(est, args=("--tier-fast", "HEAD"),
               env={"CCTALLY_AUTHORITATIVE_RUN": "1",
                    "SENTINEL_DIR": str(sentinels)})
    assert r.returncode == 3, r.stdout + r.stderr
    out = _outcome(r)
    assert out["failureClass"] == "infrastructure", out
    assert "estate-discovery-failed" in _codes(r), out
    assert not list(sentinels.iterdir()), sorted(sentinels.iterdir())


def test_a_green_tier_verdict_names_the_mode_and_the_omitted_count(tmp_path):
    """#630 S7 / Task 2 review. A green tier printed `Verdict: PASS (exit 0)`
    and nothing else, because the two sentences that state a selection are
    gated on `COVERAGE_MODE = subset`. A tier that omitted most of the estate
    therefore read in scrollback exactly like a full run, and the record — the
    only place the mode appears — is written to a file the operator is not
    looking at.

    The line deliberately does NOT reuse the subset path's "NOT a gate"
    wording: a subset is uncitable by design, while a tier is citable under its
    own gate identity, so borrowing that sentence would state the opposite of
    what the mode means."""
    est = _tier_estate(tmp_path)
    _tier_touch(est, "alpha")
    r = _drive(est, args=("--tier-fast", "HEAD"),
               env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    text = r.stdout + r.stderr
    assert "Verdict: PASS" in text, text
    assert "coverage: tier-fast" in text, text
    # The count itself, not merely the word. A line naming the mode while
    # reporting nothing about the size of the selection leaves the operator
    # exactly where the finding found them.
    assert "2 of 3 harness(es) omitted" in text, text
    assert "NOT a gate" not in text, text

    # Non-vacuity, in the direction that matters: the same estate run in full
    # prints no such line, so a defect that printed it unconditionally would
    # not satisfy this case.
    full = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert full.returncode == 0, full.stdout + full.stderr
    assert "coverage: tier-fast" not in (full.stdout + full.stderr), full.stdout


def test_the_tier_record_carries_its_mode_partition_and_basis(tmp_path):
    est = _tier_estate(tmp_path)
    _tier_touch(est, "alpha")
    r = _drive(est, args=("--tier-fast", "HEAD"),
               env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    cov = _outcome(r)["coverage"]
    assert cov["mode"] == "tier-fast", cov
    assert cov["pytest"] == "full", cov
    assert cov["selectedHarnesses"] == ["alpha"], cov
    assert cov["omittedHarnesses"] == ["beta", "reconcile"], cov
    assert set(cov["tierBasis"]) == {
        "baseOid", "headOid", "committedPaths", "worktreePaths"
    }, cov
    assert len(cov["tierBasis"]["baseOid"]) == 40, cov
    assert len(cov["tierBasis"]["headOid"]) == 40, cov
    assert cov["tierBasis"]["committedPaths"] == 0, cov
    assert cov["tierBasis"]["worktreePaths"] == 1, cov


def test_a_full_run_record_carries_no_tier_basis(tmp_path):
    """The basis is present in tier mode alone. An object of empty strings on
    every other mode would describe a basis that does not exist, and the
    wrapper's coverage validation reads this block."""
    est = _tier_estate(tmp_path)
    r = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    cov = _outcome(r)["coverage"]
    assert cov["mode"] == "full", cov
    assert "tierBasis" not in cov, cov


def test_the_tier_executes_only_what_it_selected(tmp_path):
    """The selection reaches the POOL, not merely the record. Positive evidence
    of execution, so this cannot be satisfied by a run that executed nothing."""
    est = _tier_estate(tmp_path, sentinel=True)
    _tier_touch(est, "alpha")
    ran = tmp_path / "ran"
    ran.mkdir()
    r = _drive(est, args=("--tier-fast", "HEAD"),
               env={"CCTALLY_AUTHORITATIVE_RUN": "1", "SENTINEL_DIR": str(ran)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _executed(ran) == {"alpha"}, _executed(ran)


def test_the_tier_is_excluded_from_the_runtime_budget(tmp_path):
    """Eligibility condition 2. The tier ran a DIFFERENT estate, so its quotient
    answers a different question from the one the committed threshold was
    derived for. Non-vacuous by construction: the same estate, the same marker
    and the same 0.001 budget produce runtime-budget-exceeded on a full run."""
    est = _tier_estate(tmp_path, budget=0.001, harness_sleep={"alpha": 1})
    _tier_touch(est, "alpha")
    full = _drive(est, env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert [x["code"] for x in _budget_reasons(full)] == [
        "runtime-budget-exceeded"
    ], _outcome(full)

    tiered = _drive(est, args=("--tier-fast", "HEAD"),
                    env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    assert tiered.returncode == 0, tiered.stdout + tiered.stderr
    assert _budget_reasons(tiered) == [], _outcome(tiered)


def test_the_tier_and_a_subset_cannot_be_combined(tmp_path):
    """Preserve-item 19 again, at the aggregator boundary rather than in plan
    mode: a caller-chosen subset stays uncitable, so the two request surfaces
    may not be merged into one run."""
    est = _tier_estate(tmp_path)
    r = _drive(est, args=("--tier-fast", "HEAD", "--harness", "alpha"))
    assert r.returncode == 2, r.stdout + r.stderr
    # The exact phrase, not merely both tokens: the pre-tier message
    # "unrecognised argument '--tier-fast'; the only arguments are --harness
    # NAME (repeatable) and --with-pytest" contains both tokens too, so a
    # membership test alone was satisfied by an exit 2 for an unrelated reason.
    assert "--tier-fast cannot accompany --harness" in r.stderr, r.stderr


def test_a_tier_over_a_broken_ownership_map_widens_and_stays_tier_fast(tmp_path):
    """§3.5 legs 2 and 4 together, in the record. Ownership verification runs
    before a narrow result is accepted, and a failure widens — but the MODE is
    decided by how the run was invoked and how its set was derived, never by
    whether the set turned out to be everything. Deciding it by result would let
    a widened tier mint the releasable full-suite gate."""
    est = _tier_estate(tmp_path)
    _tier_touch(est, "alpha")
    path = est / "tests" / "harness-ownership.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["harnesses"]["beta"]["fixturePaths"] = ["tests/fixtures/alpha"]
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    r = _drive(est, args=("--tier-fast", "HEAD"),
               env={"CCTALLY_AUTHORITATIVE_RUN": "1"})
    cov = _outcome(r)["coverage"]
    assert cov["mode"] == "tier-fast", cov
    assert cov["omittedHarnesses"] == [], cov
    assert cov["selectedHarnesses"] == ["alpha", "beta", "reconcile"], cov
