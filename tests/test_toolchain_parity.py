"""Every authoritative gate installs the SAME test toolchain (#529 S6, F20).

`tests/requirements-dev.txt` is the canonical closure. Before S6 the remote
wrapper carried its own exact pin literal and installed it ``--no-deps``, while
all three CI lanes ran an unpinned ``pip install --upgrade pip pytest
pytest-xdist pytest-timeout PyYAML rich`` and the pin file itself listed only a
few packages, mostly unpinned. A suite result could therefore depend on which
GATE ran it, which is the same class of defect the fleet-constant job budget
closed for which RUNNER ran it.

Enforcement takes TWO tests, because one cannot reach both sides of the mirror
boundary. This is the PUBLIC half: it checks the pin file and the three
authoritative CI jobs, all of which are mirrored. The wrapper is mirror-excluded
and ``tests/test_public_test_dep_closure.py`` rejects an ungated public
reference to a private file, so the assertion that the WRAPPER still installs
this same file lives in ``bin/cctally-test-remote-test`` instead. Without that
private half this test would keep passing while the wrapper reverted to a
private literal, which is exactly the divergence F20 describes.

The workflows are PARSED, never scanned. A text scan over these files has
already reported three healthy artifact uploads over three files GitHub Actions
could not compile (see ``_parse_workflow``'s own docstring), and a scan that
reads comments derives facts from prose — every one of these lanes carries a
comment naming ``pip install`` and ``pytest-xdist``. Shell comments are stripped
from each ``run`` script before any command assertion is made.

The same pass also enforces M2's FTS5 assertion, because it is part of the same
mechanism and the same three jobs: FTS5 is the one hard capability no
provisioning step installs, so each authoritative lane asks for it in
PROVISIONING mode before the suite starts. Nothing enforced that until this
file did — deleting the step from either workflow reddened nothing.
"""
from __future__ import annotations

import pathlib
import re
import shutil

import pytest

# The structural workflow reader S2 shipped for exactly this purpose, reused
# rather than reimplemented so the two files cannot disagree about what a lane
# says. It is PyYAML-backed since #541 replaced the permissive reader with a
# real parser, which is sound here only because PyYAML is itself one of the pins
# below: a parity gate may depend on it precisely because the closure it is
# checking is what installs it, in every lane, fail-closed.
from test_test_all_observability import _parse_workflow  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO / ".github" / "workflows"
PIN_FILE = REPO / "tests" / "requirements-dev.txt"
PIN_FILE_REL = "tests/requirements-dev.txt"

EXPECTED_PIN_COUNT = 14
EXPECTED_AUTHORITATIVE_JOBS = {
    "ci.yml:test-macos",
    "ci.yml:test-pr",
    "ci-linux-matrix.yml:test-linux",
}

# The six packages a lane used to name inline. Any of them appearing as a bare
# argument of a pip install is the inline list this session removed. PyYAML
# arrived with the workflow-contract gate and shellcheck-py with the ShellCheck
# gate; both were added to every lane's inline list before this file became the
# one source, so both belong here.
_TEST_PACKAGES = (
    "pytest",
    "pytest-xdist",
    "pytest-timeout",
    "pyyaml",
    "rich",
    "shellcheck-py",
)

_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.*+!_-]*$")


# --------------------------------------------------------------- the pin file


def parse_pins(text: str) -> list[str]:
    """The requirement lines, comments and blank lines removed.

    Mirrors what the wrapper's ``_venv_pin_spec`` does to build its venv cache
    key, so the two cannot disagree about what this file says.
    """
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def test_pin_file_is_exactly_the_expected_count_of_exact_pins():
    pins = parse_pins(PIN_FILE.read_text(encoding="utf-8"))
    # The non-vacuity anchor. A parser bug that found nothing would otherwise
    # satisfy every per-entry assertion below by iterating over an empty list.
    assert len(pins) == EXPECTED_PIN_COUNT, pins
    names = [p.split("==", 1)[0].lower() for p in pins]
    assert len(set(names)) == EXPECTED_PIN_COUNT, names
    for pin in pins:
        assert _PIN.match(pin), f"not an exact name==version pin: {pin!r}"
        assert ";" not in pin, f"environment markers are not allowed: {pin!r}"
        assert "@" not in pin and "://" not in pin, f"URLs are not allowed: {pin!r}"
        assert "[" not in pin, f"extras are not allowed: {pin!r}"


def test_pin_file_carries_the_packages_every_gate_needs():
    """The closure must actually contain what each gate detects or executes.

    The right count of the wrong packages would satisfy the shape test above
    while leaving a lane without xdist, which is what made one lane's pytest
    phase run single-process for 914s. PyYAML and shellcheck-py are named here
    for the same reason and a stronger one: each backs a FAIL-CLOSED gate, so a
    closure that lost either would not degrade, it would refuse.
    """
    names = {
        p.split("==", 1)[0].lower()
        for p in parse_pins(PIN_FILE.read_text(encoding="utf-8"))
    }
    for required in (
        "pip",
        "pytest",
        "pytest-xdist",
        "pytest-timeout",
        "pyyaml",
        "rich",
        "shellcheck-py",
    ):
        assert required in names, f"{required} missing from {PIN_FILE_REL}"


# ------------------------------------------------------------- the CI lanes


def _strip_shell_comments(script: str) -> str:
    """Drop shell comments, so no assertion below is derived from prose.

    Every one of these lanes documents the toolchain in a comment that names
    ``pip install`` and the package list. A scan that read those comments would
    report an inline package list in a lane that has none, and would keep
    reporting one after the lane was fixed.
    """
    out = []
    for raw in script.splitlines():
        stripped = re.sub(r"(^|\s)#.*$", "", raw)
        if stripped.strip():
            out.append(stripped)
    return "\n".join(out)


def _scalar(value) -> str:
    """The VALUE of a parsed YAML scalar, without its quotes or trailing comment.

    The lightweight parser keeps the raw right-hand side, so this file's
    ``CCTALLY_AUTHORITATIVE_RUN: "1"`` parses to the four characters ``"1"`` and
    several sibling entries carry an aligned trailing comment. Comparing the raw
    text against ``1`` selected no job at all, which would have made every
    per-lane assertion below pass over an empty set — the exact failure the
    enumeration anchor exists to catch, and it caught it.
    """
    text = str(value).strip()
    for quote in ('"', "'"):
        if text.startswith(quote):
            closing = text.find(quote, 1)
            if closing > 0:
                return text[1:closing]
    return re.sub(r"\s+#.*$", "", text).strip()


def _job_env(job, steps) -> dict:
    """Every environment mapping a job can set, job level plus step level.

    Read from the PARSED mappings. ``test-macos`` declares the authoritative
    marker in the suite step's ``env`` while the other two declare it at job
    level, so a selector that looked at only one of the two would find one job
    where there are three.
    """
    merged = {}
    env = job.get("env")
    if isinstance(env, dict):
        merged.update(env)
    for step in steps:
        if not isinstance(step, dict):
            continue
        env = step.get("env")
        if isinstance(env, dict):
            merged.update(env)
    return merged


def authoritative_jobs(workflow_dir: pathlib.Path) -> dict:
    """`{"<file>:<job>": (job, steps)}` for every job declaring the marker.

    S1 added ``CCTALLY_AUTHORITATIVE_RUN: "1"`` to exactly the jobs whose green
    is meant to be believed, so it is the selector rather than a hand-kept list
    of job names.
    """
    found = {}
    paths = sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml"))
    for path in paths:
        for name, (job, steps) in _parse_workflow(path).items():
            if _scalar(_job_env(job, steps).get("CCTALLY_AUTHORITATIVE_RUN", "")) == "1":
                found[f"{path.name}:{name}"] = (job, steps)
    return found


def _command_lines(steps) -> list[str]:
    """Every executable line of a job's ``run`` scripts, in execution order.

    Flattened across steps because the two lane shapes differ: ``test-macos``
    installs and runs the suite inside ONE step, while ``test-pr`` and
    ``test-linux`` install in a step of their own. Ordering assertions have to
    hold for both, so the unit is the line rather than the step.
    """
    lines = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if not isinstance(run, str):
            continue
        lines.extend(_strip_shell_comments(run).splitlines())
    return lines


def _first_index(lines, predicate) -> int:
    for index, line in enumerate(lines):
        if predicate(line):
            return index
    return -1


def _is_pip_install(line: str) -> bool:
    return "pip" in line and re.search(r"\bpip\b[^|;&]*\binstall\b", line) is not None


# The PROVISIONING mode of the one FTS5 assertion (#529 S6, M2). Probe mode is
# deliberately not accepted here: it returns a status and owns no output, so a
# lane calling it asserts nothing — the step exits either way and the suite runs
# on an interpreter whose SQLite may lack the capability. Only `require` prints
# the diagnostic and exits 3 before the suite starts.
_FTS5_REQUIRE = re.compile(r"_lib-fts5-probe\.sh\s+require\b")


def _is_fts5_require(line: str) -> bool:
    return _FTS5_REQUIRE.search(line) is not None


def lane_problems(label: str, steps) -> list[str]:
    """Every way one lane departs from the canonical install, named."""
    problems = []
    lines = _command_lines(steps)
    if not lines:
        return [f"{label}: the job declares no run script at all"]

    install = _first_index(
        lines, lambda l: _is_pip_install(l) and PIN_FILE_REL in l
    )
    check = _first_index(lines, lambda l: re.search(r"\bpip\b[^|;&]*\bcheck\b", l))
    suite = _first_index(lines, lambda l: "bin/cctally-test-all" in l)
    fts5 = _first_index(lines, _is_fts5_require)

    if install < 0:
        problems.append(
            f"{label}: no `pip install ... -r {PIN_FILE_REL}` command"
        )
    elif "--no-deps" not in lines[install]:
        problems.append(
            f"{label}: the pin-file install drops --no-deps, so pip resolves "
            f"beyond the pins: {lines[install].strip()!r}"
        )
    if check < 0:
        problems.append(f"{label}: no `pip check` command validates the closure")
    if suite < 0:
        problems.append(f"{label}: the job never invokes bin/cctally-test-all")
    if fts5 < 0:
        problems.append(
            f"{label}: no `bash bin/_lib-fts5-probe.sh require <interpreter>` "
            f"step asserts FTS5 before the suite"
        )
    elif suite >= 0 and suite < fts5:
        problems.append(
            f"{label}: the suite runs before FTS5 is asserted, so a runner "
            f"lacking it is discovered mid-suite instead of before it"
        )
    if install >= 0 and check >= 0 and check < install:
        problems.append(f"{label}: `pip check` runs before the install")
    if install >= 0 and suite >= 0 and suite < install:
        problems.append(f"{label}: the suite runs before the toolchain is installed")
    if check >= 0 and suite >= 0 and suite < check:
        problems.append(f"{label}: the suite runs before `pip check` validates it")

    for line in lines:
        if not _is_pip_install(line) or PIN_FILE_REL in line:
            continue
        # An install of something else entirely is fine; an install naming one
        # of the four test packages as a bare argument is the inline list.
        for token in re.split(r"\s+", line.strip()):
            if token.split("==", 1)[0].split("[", 1)[0].lower() in _TEST_PACKAGES:
                problems.append(
                    f"{label}: carries an inline test-package list "
                    f"({token!r} in {line.strip()!r})"
                )
                break
    return problems


def test_exactly_the_three_known_authoritative_jobs_are_selected():
    """The non-vacuity anchor for the lane enumeration.

    Without this an enumeration bug that selected nothing would make every
    per-lane assertion below pass over an empty set.
    """
    found = set(authoritative_jobs(WORKFLOW_DIR))
    assert found == EXPECTED_AUTHORITATIVE_JOBS, found
    assert len(found) == 3


@pytest.mark.parametrize("label", sorted(EXPECTED_AUTHORITATIVE_JOBS))
def test_each_authoritative_lane_installs_the_canonical_pin_file(label):
    jobs = authoritative_jobs(WORKFLOW_DIR)
    assert label in jobs, sorted(jobs)
    _job, steps = jobs[label]
    assert lane_problems(label, steps) == []


# ------------------------------------------------------------- mutation cases
#
# Both operate on COPIES in tmp_path. Nothing here rewrites a real workflow.


def _mutated_workflows(tmp_path, filename, old, new):
    out = tmp_path / "workflows"
    out.mkdir(exist_ok=True)
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        shutil.copy(path, out / path.name)
    target = out / filename
    text = target.read_text(encoding="utf-8")
    assert old in text, f"the mutation anchor is stale: {old!r} not in {filename}"
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    return out


def test_restoring_an_inline_list_in_one_lane_reddens_and_names_that_lane(tmp_path):
    out = _mutated_workflows(
        tmp_path,
        "ci-linux-matrix.yml",
        f"--no-deps -r {PIN_FILE_REL}",
        "--upgrade pip pytest pytest-xdist pytest-timeout rich",
    )
    jobs = authoritative_jobs(out)
    assert set(jobs) == EXPECTED_AUTHORITATIVE_JOBS, sorted(jobs)
    problems = {
        label: lane_problems(label, steps) for label, steps in
        ((label, steps) for label, (_job, steps) in jobs.items())
    }
    assert problems["ci-linux-matrix.yml:test-linux"], problems
    assert any(
        "inline test-package list" in p
        for p in problems["ci-linux-matrix.yml:test-linux"]
    ), problems["ci-linux-matrix.yml:test-linux"]
    # And ONLY that lane: a mutation that reddened everything would not prove
    # the failure names the lane a maintainer has to fix.
    assert problems["ci.yml:test-pr"] == [], problems["ci.yml:test-pr"]
    assert problems["ci.yml:test-macos"] == [], problems["ci.yml:test-macos"]


def test_deleting_the_fts5_assertion_reddens_and_names_that_lane(tmp_path):
    """M2's provisioning mode is part of the mechanism, so deleting it must redden.

    FTS5 is the one hard capability no workflow step installs, because it comes
    from the SQLite the interpreter links. Each authoritative lane therefore asks
    for it in provisioning mode before the suite starts. Nothing enforced that
    until this case: removing the step from either workflow reddened nothing at
    all, and the lane went back to assuming the capability.
    """
    out = _mutated_workflows(
        tmp_path,
        "ci-linux-matrix.yml",
        "      - name: Assert FTS5 before the suite\n"
        "        run: bash bin/_lib-fts5-probe.sh require python\n",
        "",
    )
    jobs = authoritative_jobs(out)
    assert set(jobs) == EXPECTED_AUTHORITATIVE_JOBS, sorted(jobs)
    problems = {
        label: lane_problems(label, steps) for label, (_job, steps) in jobs.items()
    }
    assert any(
        "FTS5" in p for p in problems["ci-linux-matrix.yml:test-linux"]
    ), problems["ci-linux-matrix.yml:test-linux"]
    # And ONLY that lane, so the failure names the file a maintainer has to fix.
    assert problems["ci.yml:test-pr"] == [], problems["ci.yml:test-pr"]
    assert problems["ci.yml:test-macos"] == [], problems["ci.yml:test-macos"]


def test_downgrading_the_fts5_assertion_to_probe_mode_reddens(tmp_path):
    """Probe mode returns a status and owns no output, so a lane calling it
    asserts nothing: the step exits 0 or 1 and the job carries on either way.
    Only the provisioning mode refuses before the suite."""
    out = _mutated_workflows(
        tmp_path,
        "ci.yml",
        "bash bin/_lib-fts5-probe.sh require python3",
        "bash bin/_lib-fts5-probe.sh probe python3",
    )
    jobs = authoritative_jobs(out)
    problems = lane_problems("ci.yml:test-macos", jobs["ci.yml:test-macos"][1])
    assert any("FTS5" in p for p in problems), problems


def test_dropping_no_deps_reddens(tmp_path):
    out = _mutated_workflows(
        tmp_path,
        "ci.yml",
        f"install --no-deps -r {PIN_FILE_REL}",
        f"install -r {PIN_FILE_REL}",
    )
    jobs = authoritative_jobs(out)
    reddened = [
        label for label, (_job, steps) in jobs.items() if lane_problems(label, steps)
    ]
    assert reddened, "dropping --no-deps must redden a lane"
    for label in reddened:
        assert any("--no-deps" in p for p in lane_problems(label, jobs[label][1]))
