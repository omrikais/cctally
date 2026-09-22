"""Aggregator observability behaviour (#529 S2).

Every case drives the REAL `bin/cctally-test-all` against a scratch repository
built the way `tests/test_authoritative_test_contract.py` builds its estate:
the aggregator derives `REPO_ROOT` from its own location, so copying it, the
contract library, the evidence kernels, a manifest and fake harnesses into a
temporary tree exercises the true entry point without recursing into the real
suite. There is deliberately NO "point the aggregator at another estate"
environment seam — a bypass variable sitting next to the admission it would
bypass is the hazard this session exists to close.
"""
from __future__ import annotations

import concurrent.futures
import gzip
import importlib.util
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import time

import pytest
import yaml

from helpers.authoritative_runtime_budget import write_budget
from tests import _estate_stub
from tests._support_http import PRESENCE_BACKSTOP_SECONDS, remaining

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
RUNNER = BIN / "cctally-test-all"
CONTRACT_LIB = BIN / "_lib-test-contract.sh"
EVIDENCE_KERNEL = BIN / "_lib_test_evidence.py"
PRIVATE_TEST_REMOTE_HARNESS = BIN / "cctally-test-remote-test"

DEFAULT_SUMMARY = "passed: 5   failed: 0"


def _kernels():
    """Every `bin/_lib_test_*.py` this tree carries, matched as a CLASS.

    The aggregator's contract is "use whichever kernels this tree carries",
    and the vocabulary producer is maintainer-local, so naming it here would
    both break the mirrored public suite and misstate the contract.
    """
    return sorted(BIN.glob("_lib_test_*.py"))


def _has_vocabulary_producer(path):
    """Whether a carried kernel provides the disclosure-vocabulary capability.

    Match the capability rather than assuming every non-evidence kernel is the
    maintainer-local producer. The public tree also carries the independent
    isolation kernel, which made that filename-count proxy true while no
    vocabulary existed and caused public-only assertions to run against the
    intended redact-everything fallback.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r"^def build_known_tokens\(", text, re.MULTILINE))


# Without the maintainer-local producer the transformer has nothing to vouch
# for a word with and redacts every detail by design, so cases that assert a
# diagnostic SURVIVES are skipped rather than weakened on a public tree.
VOCABULARY_AVAILABLE = any(_has_vocabulary_producer(path) for path in _kernels())


def _production_constant(name):
    """One integer constant, read from the aggregator's own source.

    Re-declaring a value here makes a case agree with itself rather than with
    production. `ORPHAN_GRACE_SECONDS` is the worked example: raising the real
    window to 1,200 seconds would have been caught by the backdating far below,
    but lowering it to 1 second would not, because every case backdated past
    both.

    Reading the value is not the same as pinning it. A case that both derives
    its bound from here AND needs the declared value to stay put must assert
    that value separately, or a change to the runner moves the case with it.
    """
    match = re.search(
        r"^%s\s*=\s*(\d+)\s*$" % re.escape(name),
        RUNNER.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, "%s is not defined in %s" % (name, RUNNER)
    return int(match.group(1))


#: The cadence an authoritative run reports at, whatever the seam is set to.
AUTHORITATIVE_PROGRESS_INTERVAL = _production_constant(
    "AUTHORITATIVE_PROGRESS_INTERVAL"
)


def _estate(tmp_path, harnesses=None, exits=None, smoke=True, manifest_min=None):
    """A scratch repository whose harnesses print exactly what a case needs."""
    harnesses = dict(harnesses or {"alpha": [DEFAULT_SUMMARY]})
    exits = dict(exits or {})
    manifest_min = dict(manifest_min or {})
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
    for kernel in _kernels():
        shutil.copy2(kernel, bindir / kernel.name)
    # #648 D10. The class glob above copies the estate checker too, and a
    # scratch estate carries no committed artifact for it to check against, so
    # every case here failed at admission on the fixture rather than on the
    # property it asserts (51 of them, measured). The stub answers with a clean
    # report; the aggregator's own handling of that report is not stubbed.
    _estate_stub.install(bindir)
    # #648 D7. Both pytest legs load `-p tests._estate_leg_plugin`, so a scratch
    # estate that omitted it would fail on a missing module. Copied rather than
    # guarded by an existence check: a guard around the proof is exactly the
    # shape D7 removed from the leg construction.
    shutil.copy2(REPO / "tests" / "_estate_leg_plugin.py",
                 testsdir / "_estate_leg_plugin.py")

    # `reconcile` stays in the aggregator's final_harnesses as an ordering
    # device, so every estate must carry it or the pool runs a harness that is
    # not on disk.
    names = list(harnesses) + ["reconcile"]
    harnesses.setdefault("reconcile", [DEFAULT_SUMMARY])
    for name in names:
        path = bindir / f"cctally-{name}-test"
        body = ["#!/usr/bin/env bash"]
        for line in harnesses[name]:
            body.append(f"printf '%s\\n' {_sq(line)}")
        body.append(f"exit {exits.get(name, 0)}")
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        path.chmod(0o755)

    # The durations plugin is loaded with `-p tests._pytest_durations_plugin`
    # on an authoritative evidence run, so an estate without it would fail to
    # start pytest at all (#630 S1).
    shutil.copy2(
        REPO / "tests" / "_pytest_durations_plugin.py",
        testsdir / "_pytest_durations_plugin.py",
    )
    if smoke:
        (testsdir / "test_scratch_smoke.py").write_text(
            "def test_ok():\n    assert True\n"
            "def test_known():\n    assert True\n",
            encoding="utf-8",
        )
    (testsdir / "authoritative-test-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "minHarnessRows": 0,
                "harnesses": [
                    {
                        "name": n,
                        "visibility": "public",
                        "minCases": manifest_min.get(n, 0),
                        "countPolicy": "fixed",
                    }
                    for n in names
                ],
                "capabilities": [],
                "forbiddenRegeneration": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # #630 S7, one spelling since #650 item 5.
    write_budget(testsdir)
    return repo


def _sq(text):
    """Single-quote one shell word."""
    return "'" + text.replace("'", "'\\''") + "'"


def _env(tmp_path, extra=None):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "TZ": "Etc/UTC",
        "CCTALLY_TEST_JOBS": "1",
    }
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    env.update(extra or {})
    return env


def _drive(est, tmp_path, extra=None, timeout=240):
    return subprocess.run(
        [str(est / "bin" / "cctally-test-all")],
        env=_env(tmp_path, extra),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_dirs(root):
    return sorted(p.parent for p in pathlib.Path(root).rglob("manifest.json"))


def _embedded_pid_start(pid, zone):
    """Run the aggregator's exact embedded process-identity producer."""
    source = RUNNER.read_text(encoding="utf-8")
    marker = "python3 - \"$EVIDENCE_KERNEL\" \"$EVIDENCE_PRIVATE\" \"$@\" <<'EVPY'\n"
    bridge = source.split(marker, 1)[1].split("\nEVPY\n", 1)[0]
    # `pid-start` needs only the public evidence kernel. Feed the optional
    # private-kernel slot a portable non-module so this public test keeps the
    # same dependency closure as the public mirror.
    private = pathlib.Path(os.devnull)
    env = dict(os.environ, LC_ALL="C", TZ=zone)
    result = subprocess.run(
        [
            sys.executable,
            "-",
            str(EVIDENCE_KERNEL),
            str(private),
            "pid-start",
            str(pid),
        ],
        input=bridge,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# ------------------------------------------------------------- plan-mode boundary


def test_plan_mode_creates_no_evidence_directory(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    baseline = {
        line.split(None, 1)[0]
        for line in _descendants_mentioning(str(est / "bin" / "cctally-test-all"))
    }
    res = _drive(
        est,
        tmp_path,
        {"CCTALLY_TEST_ALL_PLAN": "1", "CCTALLY_TEST_EVIDENCE_ROOT": str(root)},
    )
    assert res.returncode == 0, res.stderr
    assert not root.exists(), "plan mode must remain side-effect free"
    assert "harnesses=" in res.stdout
    survivors = {
        line.split(None, 1)[0]
        for line in _descendants_mentioning(str(est / "bin" / "cctally-test-all"))
    }
    assert not survivors - baseline, (
        "plan mode left processes that were absent from the pre-run baseline: "
        f"{sorted(survivors - baseline)}"
    )


def test_an_invalid_run_id_is_not_even_looked_at_in_plan_mode(tmp_path):
    est = _estate(tmp_path)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_RUN_ID": "../escape",
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_TEST_ALL_PLAN": "1",
        },
    )
    assert res.returncode == 0, "run identity must not be resolved before plan mode"
    assert not (tmp_path / "ev").exists()


def test_aggregator_process_start_identity_is_caller_timezone_independent():
    """A retained live run must not become evictable when the reader's TZ changes."""
    tokyo = _embedded_pid_start(os.getpid(), "Asia/Tokyo")
    new_york = _embedded_pid_start(os.getpid(), "America/New_York")

    assert tokyo, "the producer must identify this live pytest process"
    assert tokyo == new_york


# --------------------------------------------------------------- run identity


def test_an_invalid_explicit_run_id_refuses_a_real_run(tmp_path):
    est = _estate(tmp_path)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_RUN_ID": "../escape",
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
        },
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "CCTALLY_TEST_RUN_ID" in res.stderr
    assert not (tmp_path / "ev").exists()


def test_an_existing_evidence_directory_is_refused_not_reused(tmp_path):
    """Spec section 2: two runs writing one evidence directory corrupt both.

    The kernel cannot enforce this — it imports only `re` and performs no I/O
    — so the aggregator must, and the pre-existing directory must come back
    untouched rather than merged with.
    """
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    occupied = root / "local" / "taken"
    (occupied / "logs").mkdir(parents=True)
    keeper = occupied / "logs" / "earlier.log"
    keeper.write_text("evidence from the earlier run\n", encoding="utf-8")

    res = _drive(
        est,
        tmp_path,
        {"CCTALLY_TEST_RUN_ID": "taken", "CCTALLY_TEST_EVIDENCE_ROOT": str(root)},
    )
    assert res.returncode == 2, res.stdout + res.stderr
    # DISCRIMINATING, not merely present. `taken` is the run id and appears in
    # the opening banner too, so asserting it alone let the whole pre-existing
    # directory check be replaced by `if false` with the case still green: the
    # bare `mkdir` kept the run safe, and the operator was then told the id had
    # been "claimed concurrently" by a run that does not exist. This case owns
    # the check, so it asserts which of the two refusals fired.
    assert "an evidence directory already existed" in res.stderr, res.stderr
    assert "concurrently" not in res.stderr, res.stderr
    # Non-vacuity from the other side: the refusal really left the occupant
    # alone, so the assertion is about refusing rather than about failing.
    assert keeper.read_text(encoding="utf-8") == "evidence from the earlier run\n"
    assert not (occupied / "manifest.json").exists()


def _kernel_refusal(tmp_path, mangle):
    """Drive an estate whose evidence kernel `mangle` has broken, and read
    the outcome record the admission refusal writes."""
    est = _estate(tmp_path)
    mangle(est / "bin" / "_lib_test_evidence.py")
    record = tmp_path / "outcome.json"
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_TEST_ALL_OUTCOME_FILE": str(record),
        },
    )
    return res, json.loads(record.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name,mangle",
    [
        ("absent", lambda p: p.unlink()),
        ("unreadable", lambda p: p.chmod(0o000)),
        # The likeliest case in practice, and the one the readability probe
        # cannot see: the file is there and readable, and importing it raises.
        ("unimportable", lambda p: p.write_text("def (\n", encoding="utf-8")),
        # A kernel that imports but is not the kernel — a truncated copy, or a
        # rename that left the module without the entry point.
        ("incomplete", lambda p: p.write_text("VERSION = 1\n", encoding="utf-8")),
    ],
)
def test_an_unusable_evidence_kernel_is_named_as_such(tmp_path, name, mangle):
    """One diagnosis for the whole class, not one for the readable half.

    The readability probe answers only "can this path be opened", so a kernel
    that is present and readable but raises on import fell through to the
    run-id call, whose non-zero status was attributed to the run-id grammar:
    the aggregator reported `exit 2`, `aggregator-usage-error` and
    "CCTALLY_TEST_RUN_ID must be a safe single path component", none of which
    is true and none of which any change to CCTALLY_TEST_RUN_ID can fix.
    """
    res, record = _kernel_refusal(tmp_path, mangle)
    assert res.returncode == 3, res.stdout + res.stderr
    assert record["failureClass"] == "infrastructure", record
    assert [r["code"] for r in record["reasons"]] == ["evidence-kernel-missing"], (
        record
    )
    assert "CCTALLY_TEST_RUN_ID" not in res.stderr, res.stderr
    assert "_lib_test_evidence.py" in res.stderr, res.stderr


def test_a_generated_run_id_is_used_when_none_is_supplied(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 0, res.stdout + res.stderr
    runs = _run_dirs(root)
    assert len(runs) == 1, runs
    # `<UTC stamp>-<pid>-<random>`; the stamp is the only part a reader can pin.
    assert runs[0].name[:8].isdigit() and "T" in runs[0].name


# --------------------------------------------------------- the evidence layout


def test_the_evidence_layout_has_the_documented_shape_and_modes(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 0, res.stdout + res.stderr
    run = _run_dirs(root)[0]
    for sub in ("logs", "timings", "export"):
        assert (run / sub).is_dir(), sub
        mode = stat.S_IMODE((run / sub).stat().st_mode)
        assert mode == 0o700, (sub, oct(mode))
    # export/ is a SIBLING of logs/, never a parent, so a careless recursive
    # upload of the export directory cannot capture a raw log.
    assert (run / "export").resolve().parent == run.resolve()
    assert not list((run / "export").rglob("*.log"))
    assert (run / "logs" / "alpha.log").is_file()
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["runId"] == run.name
    assert manifest["remoteDir"] == "local"


def test_the_outcome_record_lands_in_the_export_directory(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 0, res.stdout + res.stderr
    run = _run_dirs(root)[0]
    outcome = json.loads((run / "export" / "outcome.json").read_text())
    assert outcome["schemaVersion"] == 1
    assert outcome["outcome"] == "pass"
    assert outcome["exitCode"] == 0


def test_an_explicit_outcome_file_still_wins(tmp_path):
    """The wrapper pins the record's path; evidence supplies only a default."""
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    pinned = tmp_path / "pinned-outcome.json"
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
            "CCTALLY_TEST_ALL_OUTCOME_FILE": str(pinned),
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads(pinned.read_text())["outcome"] == "pass"


def test_no_evidence_root_leaves_no_persistent_directory(tmp_path):
    """The bare local case: a temporary log directory, deleted on exit."""
    est = _estate(tmp_path)
    res = _drive(est, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (tmp_path / "ev").exists()
    assert not list(tmp_path.glob("**/manifest.json"))


def test_an_unsafe_remote_dir_component_is_a_usage_error(tmp_path):
    est = _estate(tmp_path)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_REMOTE_DIR": "../escape",
        },
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "CCTALLY_REMOTE_DIR" in res.stderr


# ------------------------------------------------------------ progress output


# `[  7/56] PASS  diff                      340 cases     48s`. The counter is
# space-padded, so the shape is matched rather than split on whitespace.
COMPLETION_RE = re.compile(
    r"^\[\s*(?P<index>\d+)/(?P<total>\d+)\]\s+(?P<verdict>PASS|FAIL)\s+"
    r"(?P<name>\S+)\s*(?P<rest>.*)$"
)


def _progress_lines(stderr):
    return [
        line
        for line in stderr.splitlines()
        if line.startswith("[cctally-test-all]") or _is_completion_line(line)
    ]


def _is_completion_line(line):
    return COMPLETION_RE.match(line) is not None


def _completions(stderr):
    return [
        COMPLETION_RE.match(line)
        for line in stderr.splitlines()
        if COMPLETION_RE.match(line)
    ]


#: How long the authoritative-cadence case makes its pool last. Long enough
#: that a run honouring the one-second seam must print intermediate ticks, and
#: short enough to stay a rounding error against the estate it runs in.
SEAM_EVIDENCE_SECONDS = 3


#: One pattern per `printf` in the reporter loop, named for the phase each
#: one prints for. Kept separate rather than folded into one alternation with
#: a fallback label, so a third shape added later has to be given its phase
#: here instead of arriving silently as a pool line.
POOL_HEARTBEAT_RE = re.compile(
    r"^\[cctally-test-all\] (?P<elapsed>\d+)s — \d+/\d+ done, .*queued$"
)
PHASE_HEARTBEAT_RE = re.compile(
    r"^\[cctally-test-all\] (?P<elapsed>\d+)s — (?P<phase>pytest|benchmark) running$"
)
#: The aggregator's own measurement of how long the shell pool ran, which is
#: exactly how long the pool reporter was alive to print cadence lines.
POOL_WALL_RE = re.compile(
    r"^\[cctally-test-all\] shell pool finished — (?P<seconds>\d+)s; classifying "
)


def _heartbeats(stderr):
    """`(elapsed_seconds, phase)` for every cadence line the reporter wrote."""
    marks = []
    for line in stderr.splitlines():
        match = PHASE_HEARTBEAT_RE.match(line)
        if match:
            marks.append((int(match.group("elapsed")), match.group("phase")))
            continue
        match = POOL_HEARTBEAT_RE.match(line)
        if match:
            marks.append((int(match.group("elapsed")), "pool"))
    return marks


def _pool_wall_seconds(stderr):
    """How long the shell pool ran, as the aggregator itself reported it."""
    matches = [
        match for match in map(POOL_WALL_RE.match, stderr.splitlines()) if match
    ]
    assert len(matches) == 1, stderr
    return int(matches[0].group("seconds"))


def _cadence_violations(marks, interval):
    """Consecutive same-phase heartbeats written closer together than `interval`.

    Only same-phase pairs are governed by the cadence, because a phase change
    emits its own immediate pulse. Judging every adjacent pair instead would
    call the pool-to-pytest pulse a violation on any estate whose pool
    finishes quickly, and judging the count instead would make the verdict a
    function of how fast the machine happened to be.
    """
    return [
        (previous, current)
        for previous, current in zip(marks, marks[1:])
        if previous[1] == current[1] and current[0] - previous[0] < interval
    ]


def _seed_slow_harness(est, name, seconds):
    """Give one harness a body that sleeps before it reports success.

    The file `_estate` already wrote for `name` is replaced wholesale, so an
    `exits=` entry for the same harness has no effect once this has run.
    """
    path = est / "bin" / ("cctally-%s-test" % name)
    path.write_text(
        "#!/usr/bin/env bash\nsleep %d\nprintf '%%s\\n' %s\n"
        % (seconds, _sq(DEFAULT_SUMMARY)),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _blocks(stderr):
    """`(opener, body)` for every `---- … ----` diagnostic block on stderr.

    The aggregator now closes each block with `---- end <subject> ----`, which
    is what makes "no progress line inside a block" a decidable property
    rather than a guess about where a block stopped.
    """
    blocks = []
    current = None
    for line in stderr.splitlines():
        if line.startswith("---- end ") and line.endswith(" ----"):
            assert current is not None, f"a block closed that never opened: {line}"
            blocks.append(current)
            current = None
            continue
        if line.startswith("---- ") and line.endswith(" ----"):
            assert current is None, f"a block opened inside another: {line}"
            current = (line, [])
            continue
        if current is not None:
            current[1].append(line)
    assert current is None, f"an unterminated diagnostic block: {current}"
    return blocks


def test_the_banner_names_the_run_before_admission_can_refuse_it(tmp_path):
    """A run refused by an admission delta must not be silent.

    The estate carries a harness with no manifest row, which admission refuses
    before a single harness executes.
    """
    est = _estate(tmp_path)
    stray = est / "bin" / "cctally-stray-test"
    stray.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    stray.chmod(0o755)
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev")})
    assert res.returncode == 3, res.stdout + res.stderr
    assert "manifest row" in res.stderr or "no manifest row" in res.stderr
    banner = [
        line for line in res.stderr.splitlines()
        if line.startswith("[cctally-test-all] run ")
    ]
    assert banner, res.stderr
    assert "harnesses, outer=" in banner[0]


def test_progress_output_never_reaches_stdout(tmp_path):
    """The deterministic aggregated block on stdout is what the contract suite
    parses, so every progress byte goes to stderr."""
    est = _estate(tmp_path, harnesses={"alpha": [DEFAULT_SUMMARY]})
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_PROGRESS_INTERVAL": "1",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    # Non-vacuity: progress really was produced, so its absence from stdout is
    # a routing property rather than an empty reporter.
    assert _progress_lines(res.stderr), res.stderr
    for line in res.stdout.splitlines():
        assert not line.startswith("[cctally-test-all]"), line
        assert not _is_completion_line(line), line
    assert "Verdict: PASS (exit 0)" in res.stdout


def test_one_completion_line_is_emitted_per_harness(tmp_path):
    """The counter is monotonic in COMPLETION order, not the scan position.

    A fixture cannot observe that at `CCTALLY_TEST_JOBS=1`: serial execution
    makes completion order and estate order the same sequence, so a
    scan-position counter and a monotonic one print the identical `1,2,3,4`
    and the case stays green against the defect it names. This one runs four
    workers and staggers the harnesses so the two orders are reversed —
    `reconcile` is last in the estate and finishes first, `alpha` is first and
    finishes last. Reverting to `_report_completion "$h" "$done_n"` then
    prints `1` four times, because the running count is taken at the reporting
    harness's own position among the harnesses done so far.
    """
    est = _estate(
        tmp_path,
        harnesses={
            "alpha": [DEFAULT_SUMMARY],
            "beta": [DEFAULT_SUMMARY],
            "gamma": [DEFAULT_SUMMARY],
        },
    )
    # Estate order is alpha, beta, gamma, then reconcile (kept last as the
    # summary-ordering device). Each harness WAITS for its predecessor's
    # marker and only then spends its second, so the completion order is the
    # exact reverse and is fixed by the chain rather than by a wall clock.
    # Staggered sleeps alone were not enough: measured under a saturated
    # runner, `xargs -P 4` launched the four harnesses about two seconds
    # apart, and a harness sleeping 3s that started late finished after one
    # sleeping 4s that started early. The spacing between links still has to
    # exceed the reporter's one-second scan, or two completions land in one
    # scan and are emitted in estate order.
    chain = tmp_path / "chain"
    chain.mkdir()
    for name, predecessor in (
        ("reconcile", None),
        ("gamma", "reconcile"),
        ("beta", "gamma"),
        ("alpha", "beta"),
    ):
        wait = (
            ""
            if predecessor is None
            else (
                f'for _ in $(seq 1 600); do [ -f "$CHAIN/{predecessor}" ] '
                "&& break; sleep 0.1; done\n"
            )
        )
        harness = est / "bin" / f"cctally-{name}-test"
        harness.write_text(
            "#!/usr/bin/env bash\n"
            + wait
            + "sleep 1.5\n"
            + f"printf '%s\\n' {_sq(DEFAULT_SUMMARY)}\n"
            + f': > "$CHAIN/{name}"\n',
            encoding="utf-8",
        )
        harness.chmod(0o755)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_TEST_JOBS": "4",
            "CHAIN": str(chain),
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    completions = _completions(res.stderr)
    named = [m.group("name") for m in completions]
    assert sorted(named) == ["alpha", "beta", "gamma", "reconcile"], res.stderr
    assert all(m.group("verdict") == "PASS" for m in completions), res.stderr
    # The fixture can observe the interaction: the harness the estate lists
    # LAST is reported first, and the one it lists first is reported last.
    # Both ends are fixed by the chain, so no amount of scheduler noise can
    # make this case pass without the inversion being present. The middle
    # pair is deliberately not asserted — two completions can share one scan,
    # and within a scan the reporter walks the estate.
    assert named[0] == "reconcile", res.stderr
    assert named[-1] == "alpha", res.stderr
    assert [m.group("index") for m in completions] == ["1", "2", "3", "4"], (
        res.stderr
    )
    assert {m.group("total") for m in completions} == {"4"}, res.stderr


def test_completion_line_verdicts_agree_with_the_authoritative_classification(
    tmp_path,
):
    """Acceptance criterion 2, across four registered classifier classes.

    The reporter classifies with its own copy of the contract globals while
    the parent reclassifies in deterministic order afterwards, so this asserts
    the preview and the authoritative record cannot disagree.
    """
    est = _estate(
        tmp_path,
        harnesses={
            "alpha": [DEFAULT_SUMMARY],
            "beta": ["passed: 3   failed: 2"],          # product
            "gamma": ["no summary at all"],             # incomplete
            "delta": ["kill -TERM $$", "sleep 5"],      # killed
            "epsilon": [DEFAULT_SUMMARY],               # floor unmet
        },
        exits={"beta": 1},
        manifest_min={"epsilon": 99},
    )
    # `delta`'s body is shell, not printf output, so rewrite it directly.
    (est / "bin" / "cctally-delta-test").write_text(
        "#!/usr/bin/env bash\nkill -TERM $$\nsleep 5\n", encoding="utf-8"
    )
    (est / "bin" / "cctally-delta-test").chmod(0o755)

    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 3, res.stdout + res.stderr

    reported = {}
    for match in _completions(res.stderr):
        rest = match.group("rest").split()
        # A FAIL line reads `<class> <reason-code> <duration>`.
        reported[match.group("name")] = (
            match.group("verdict"),
            rest[0] if rest else "",
            rest[1] if len(rest) > 1 else "",
        )

    outcome = json.loads(
        (_run_dirs(root)[0] / "export" / "outcome.json").read_text()
    )
    authoritative = {}
    for reason in outcome["reasons"]:
        if reason["phase"] == "harness":
            authoritative.setdefault(reason["subject"], set()).add(reason["code"])

    assert reported["alpha"][0] == "PASS", reported
    classes = {
        "beta": "product", "gamma": "incomplete",
        "delta": "infrastructure", "epsilon": "incomplete",
    }
    for name, failure_class in classes.items():
        assert reported[name][0] == "FAIL", (name, reported)
        assert reported[name][1] == failure_class, (name, reported[name])
        assert reported[name][2] in authoritative[name], (
            name, reported[name], authoritative[name],
        )
    # Non-vacuity: every class the case set out to exercise really appeared.
    assert authoritative["beta"] == {"harness-failed"}
    assert authoritative["gamma"] == {"summary-unreadable"}
    assert authoritative["delta"] == {"harness-killed"}
    assert authoritative["epsilon"] == {"case-floor-unmet"}


def test_a_heartbeat_never_lands_inside_a_multiline_diagnostic(tmp_path):
    """F8's defect, reintroduced from the other side, is what this forbids.

    The reporter is joined before the parent writes any multiline diagnostic,
    so a heartbeat cannot split a block the operator is reading. Four failing
    harnesses with large logs make the aggregation loop outlast several
    one-second heartbeat intervals, so an unjoined reporter demonstrably fires
    inside a block — verified by removing the join and watching this case go red.
    """
    names = [f"h{i}" for i in range(4)]
    est = _estate(
        tmp_path,
        harnesses={name: [f"FAIL {name}: stdout diverged"] for name in names},
        exits={name: 1 for name in names},
    )
    # Each failing harness leaves a LARGE log, so classifying and dumping the
    # four of them outlasts several one-second heartbeat intervals. Without
    # that the aggregation loop finishes inside one tick and the case cannot
    # observe the defect it exists for: removing the join before the loop left
    # it green.
    for name in names:
        path = est / "bin" / f"cctally-{name}-test"
        path.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' 'FAIL {name}: stdout diverged'\n"
            "awk 'BEGIN{for (i = 0; i < 400000; i++) print \"context line\", i}'\n"
            "printf '%s\\n' 'passed: 1   failed: 1'\n"
            "exit 1\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_PROGRESS_INTERVAL": "1",
        },
    )
    assert res.returncode == 1, res.stdout + res.stderr
    blocks = _blocks(res.stderr)
    # Non-vacuity, from both sides: blocks really were emitted with bodies in
    # them, and the reporter really was running at a one-second cadence.
    assert len(blocks) >= 4, [b[0] for b in blocks]
    assert all(body for _, body in blocks), [b[0] for b in blocks if not b[1]]
    assert _heartbeats(res.stderr), res.stderr
    for opener, body in blocks:
        for line in body:
            assert not line.startswith("[cctally-test-all]"), (opener, line)
            assert not _is_completion_line(line), (opener, line)


def test_a_hung_harness_is_named_by_the_heartbeat(tmp_path):
    """Telling queued from running is what lets the heartbeat NAME a stuck
    harness instead of leaving it as a line that never arrives."""
    est = _estate(
        tmp_path,
        harnesses={"alpha": [DEFAULT_SUMMARY], "slow": [DEFAULT_SUMMARY]},
    )
    _seed_slow_harness(est, "slow", 4)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_PROGRESS_INTERVAL": "1",
            "CCTALLY_TEST_JOBS": "2",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    naming = [
        line for line in res.stderr.splitlines()
        if line.startswith("[cctally-test-all]") and "running: slow" in line
    ]
    assert naming, res.stderr
    assert "done," in naming[0] and "queued" in naming[0]


def test_the_progress_cadence_cannot_be_loosened_on_an_authoritative_run(tmp_path):
    """The seam exists for tests. No caller may make a run whose green is
    meant to be believed quieter than the fixed cadence."""
    est = _estate(tmp_path, harnesses={"slow": [DEFAULT_SUMMARY]})
    # The pool is made to last, because a pool that finishes inside one second
    # emits its transition pulse and nothing else NO MATTER which interval is
    # in force, and a case that cannot tell the two apart is not a gate. The
    # sleep is a floor rather than a race: a slower machine lengthens the span
    # the assertions below require and can never shorten it.
    _seed_slow_harness(est, "slow", SEAM_EVIDENCE_SECONDS + 2)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_PROGRESS_INTERVAL": "1",
            "CCTALLY_AUTHORITATIVE_RUN": "1",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    marks = _heartbeats(res.stderr)
    phases = [phase for _, phase in marks]
    assert phases[:1] == ["pool"], res.stderr
    assert "pytest" in phases, res.stderr
    # Non-vacuity, asserted rather than assumed: the pool reporter really was
    # alive long enough that honouring the one-second seam would have had to
    # print intermediate pool ticks. Without this the case passes on a fast
    # runner whichever interval governs, which is what the replaced exact
    # count hid. The aggregator's own pool figure is the right window: the
    # span to the next phase pulse also covers classification, which happens
    # after the pool reporter has been joined and can print nothing.
    assert _pool_wall_seconds(res.stderr) >= SEAM_EVIDENCE_SECONDS, res.stderr
    # The property itself, stated against the cadence production uses rather
    # than against how long this estate happened to take. A run that honoured
    # the seam puts same-phase heartbeats a second apart; a contended runner
    # that reaches the next fixed tick does not, which is why counting them
    # reddened this case with no defect present (#642).
    assert not _cadence_violations(marks, AUTHORITATIVE_PROGRESS_INTERVAL), marks
    assert _completions(res.stderr), res.stderr


#: The stderr of the contended round that reddened the case above while the
#: aggregator was behaving correctly (#642). It is recorded
#: rather than reproduced: producing a third tick for real means waiting out
#: the whole authoritative interval and then trusting the machine to have been
#: slow enough, which is the load dependence being removed here.
CONTENDED_AUTHORITATIVE_STDERR = """\
[cctally-test-all] 1s — 0/2 done, 0 running: none; 2 queued
[cctally-test-all] 8s — pytest running
[cctally-test-all] 38s — pytest running
"""


def test_the_authoritative_cadence_is_the_pinned_thirty_seconds():
    """The cases here measure against whatever the runner declares, so the
    declaration itself needs pinning or it moves them with it.

    Raising it makes a run whose green is meant to be believed quieter than
    the contract promises. Lowering it makes the test-only seam
    indistinguishable from the fixed cadence, which is the whole property the
    case below exists to prove. Either is a deliberate two-file change.
    """
    assert AUTHORITATIVE_PROGRESS_INTERVAL == 30


def test_a_slow_authoritative_run_is_not_a_loosened_cadence():
    """#642: a third heartbeat means the run was slow, not that it was quiet.

    The count of heartbeats is a function of how long the estate took. The
    spacing between them is not, so the spacing is what the gate asserts.
    """
    marks = _heartbeats(CONTENDED_AUTHORITATIVE_STDERR)
    assert marks == [(1, "pool"), (8, "pytest"), (38, "pytest")], marks
    # The fact that made the replaced assertion fail: three heartbeats, not
    # two. Pinned here so this case keeps covering the run it was filed for.
    assert len(marks) > 2, marks
    assert not _cadence_violations(marks, AUTHORITATIVE_PROGRESS_INTERVAL), marks


def test_a_seam_honouring_run_is_still_caught_as_a_loosened_cadence():
    """Non-vacuity for the case above: the property can still fail.

    A run that honoured `CCTALLY_PROGRESS_INTERVAL=1` across the same
    thirty-eight seconds emits a heartbeat a second, which is exactly what an
    authoritative run may not do and what the replaced count assertion was
    there to catch.
    """
    loosened = "".join(
        "[cctally-test-all] %ss — pytest running\n" % second
        for second in range(8, 39)
    )
    marks = _heartbeats(loosened)
    assert len(marks) == 31, marks
    violations = _cadence_violations(marks, AUTHORITATIVE_PROGRESS_INTERVAL)
    assert len(violations) == len(marks) - 1, violations


def test_each_pytest_phase_change_emits_an_immediate_reporter_line(tmp_path):
    """A new phase cannot wait one full cadence before saying it is alive.

    The 60-second cadence makes the fixture finish before an ordinary tick, so
    both reporter lines below can only be phase-transition pulses. This is the
    regression for the measured 33-second pool-to-pytest silence in #541.
    """
    est = _estate(tmp_path)
    (est / "tests" / "test_rebuild_benchmark.py").write_text(
        "def test_benchmark():\n    assert True\n", encoding="utf-8"
    )
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_PROGRESS_INTERVAL": "60",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert re.search(r"^\[cctally-test-all\] \d+s — pytest running$", res.stderr, re.M), (
        res.stderr
    )
    assert re.search(
        r"^\[cctally-test-all\] \d+s — benchmark running$", res.stderr, re.M
    ), res.stderr
    assert "[cctally-test-all] pytest phase started" in res.stderr
    assert "[cctally-test-all] benchmark phase started" in res.stderr


def test_pytest_and_benchmark_phase_lines_have_authoritative_failure_reasons(tmp_path):
    """Criterion 2 covers the two Python legs as well as shell harnesses."""
    est = _estate(tmp_path)
    (est / "tests" / "test_scratch_smoke.py").write_text(
        "def test_bad():\n    assert False\n", encoding="utf-8"
    )
    (est / "tests" / "test_rebuild_benchmark.py").write_text(
        "def test_benchmark():\n    assert False\n", encoding="utf-8"
    )
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr
    assert "[cctally-test-all] pytest phase started" in res.stderr
    assert "[cctally-test-all] benchmark phase started" in res.stderr
    outcome = json.loads(
        (_run_dirs(root)[0] / "export" / "outcome.json").read_text()
    )
    reasons = {
        (reason["phase"], reason["subject"], reason["code"])
        for reason in outcome["reasons"]
    }
    assert ("pytest", "pytest", "pytest-failed") in reasons
    assert ("pytest", "benchmark", "pytest-failed") in reasons


# ------------------------------------------------------------ ordered teardown


#: One budget for a signalled run to exit AND for its pool to be reaped. Three
#: presence backstops, because both halves are waits on a real `cctally-test-
#: all` process tree rather than on an in-process object, and the reap cannot
#: begin until the exit has happened.
_ABORT_AND_REAP_BUDGET_S = 3 * PRESENCE_BACKSTOP_SECONDS


def _wait_for(predicate, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# A command started asynchronously by a shell without job control inherits
# SIGINT set to SIG_IGN, and a signal ignored on entry to a shell CANNOT be
# trapped or reset. Under `bin/cctally-test-remote --watch` the remote job is
# detached, so `trap -p INT` in every descendant reads `trap -- '' SIGINT` and
# no process in the tree can observe SIGINT at all. Asserting an INT path from
# inside that tree measures the environment rather than the aggregator, and it
# is why this case passed in a foreground run and timed out under --watch.
#
# The shim restores the default disposition and then execs, so the aggregator
# is the process the test signals. It is a separate single-threaded process
# because `preexec_fn` is unsafe from a threaded pytest-xdist worker.
_SIGINT_RESET_SHIM = (
    "import os, signal, sys\n"
    "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
    "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)


def test_the_signal_shim_restores_a_default_disposition():
    """Non-vacuity for the interrupted-run cases below.

    Without this, an environment that silently re-ignored SIGINT would make
    those cases untestable again and nothing would say so.
    """
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            _SIGINT_RESET_SHIM,
            sys.executable,
            "-c",
            "import signal; print(int(signal.getsignal(signal.SIGINT) is "
            "signal.SIG_IGN))",
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "0", probe.stdout


def _descendants_mentioning(needle):
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=", "-o", "command="],
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if needle in line]


def test_the_manifest_reads_back_the_validator_redaction_sidecar(tmp_path):
    """`exportRedactions`, end to end through `_read_json`.

    The two kernel-level cases read `validator-redactions.json` directly, so
    the read-back — the part that can silently break, because an unreadable
    sidecar degrades to `null` rather than raising — was asserted nowhere.
    """
    est, _canary = _canary_estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr

    run = _run_dirs(root)[0]
    sidecar = json.loads((run / "validator-redactions.json").read_text())
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["exportRedactions"] == sidecar, (manifest, sidecar)
    assert sidecar["schemaVersion"] == 1, sidecar
    assert sidecar["refused"] is False, sidecar
    assert sidecar["refusal"] is None, sidecar
    assert sidecar["redacted"] == 0, sidecar
    assert sidecar["reasons"] == [], sidecar
    # A measured zero rather than an assumed one: the export really had lines
    # for the validator to judge.
    assert sidecar["total"] > 0, sidecar
    assert (run / "export" / "failure-context.txt").exists()


def test_a_run_that_publishes_no_extract_records_a_null_not_a_zero(tmp_path):
    """The other reading of the same field.

    A passing run never reaches the export mode, so no sidecar exists and the
    manifest states `null`. That is a different answer from a published
    extract the validator refused nothing in, and the two must not collapse.
    """
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 0, res.stdout + res.stderr

    run = _run_dirs(root)[0]
    assert not (run / "validator-redactions.json").exists()
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["exportRedactions"] is None, manifest


def test_a_normal_run_is_recorded_completed(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 0, res.stdout + res.stderr
    manifest = json.loads((_run_dirs(root)[0] / "manifest.json").read_text())
    assert manifest["state"] == "completed"
    assert manifest["outcome"] == "pass"
    assert manifest["exitCode"] == 0
    assert manifest["finishedEpoch"] >= manifest["startedEpoch"]
    assert [row["name"] for row in manifest["harnesses"]] == ["alpha", "reconcile"]


def test_a_failing_run_is_recorded_completed_and_failed(tmp_path):
    est = _estate(
        tmp_path,
        harnesses={"alpha": ["FAIL alpha: stdout diverged", "passed: 1   failed: 1"]},
        exits={"alpha": 1},
    )
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr
    manifest = json.loads((_run_dirs(root)[0] / "manifest.json").read_text())
    assert manifest["state"] == "completed"
    assert manifest["outcome"] == "fail"
    assert manifest["failureClass"] == "product"


@pytest.mark.parametrize("signal_name,expected_rc", [("TERM", 143), ("INT", 130)])
def test_an_interrupted_run_is_recorded_aborted_and_reaps_its_pool(
    tmp_path, signal_name, expected_rc
):
    """Marking an interrupted run `completed` would be false, and a surviving
    worker would keep writing into a directory teardown is about to remove."""
    import signal as _signal

    est = _estate(tmp_path, harnesses={"alpha": [DEFAULT_SUMMARY]})
    (est / "bin" / "cctally-alpha-test").write_text(
        "#!/usr/bin/env bash\nsleep 120\n", encoding="utf-8"
    )
    (est / "bin" / "cctally-alpha-test").chmod(0o755)
    root = tmp_path / "ev"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SIGINT_RESET_SHIM,
            str(est / "bin" / "cctally-test-all"),
        ],
        env=_env(tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)}),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # ONE budget for the abort and the reap that follows it. Ninety seconds
    # for the run to exit plus thirty for the pool to be reaped summed to the
    # whole 120-second pytest cap, so a run that never aborted spent the cap
    # and pytest-timeout killed the worker before the reap assertion below
    # could name the descendants it was still seeing.
    deadline = time.monotonic() + _ABORT_AND_REAP_BUDGET_S
    try:
        assert _wait_for(lambda: list(root.rglob("logs/alpha.started"))), (
            "the pool never started"
        )
        proc.send_signal(getattr(_signal, f"SIG{signal_name}"))
        rc = proc.wait(timeout=remaining(deadline))
    finally:
        if proc.poll() is None:                      # pragma: no cover - safety
            proc.kill()
            proc.wait(timeout=30)
    assert rc == expected_rc, rc
    manifest = json.loads((_run_dirs(root)[0] / "manifest.json").read_text())
    assert manifest["state"] == "aborted", manifest
    # An unfinished run HAS no outcome, and the reason is the eviction pass:
    # `plan_evidence_evictions` ranks `outcome == "pass"` first for cap
    # eviction, so a run killed mid-estate recorded as a pass is discarded
    # ahead of a genuine failure. The same sentence the spec gives for
    # `active` applies verbatim here.
    assert manifest["outcome"] is None, manifest
    assert manifest["failureClass"] is None, manifest
    assert manifest["exitCode"] == expected_rc, manifest
    # Non-vacuity: the sleeper really was running, so its absence now is the
    # reap rather than a harness that never started.
    assert (_run_dirs(root)[0] / "logs" / "alpha.started").exists()
    assert _wait_for(
        lambda: not _descendants_mentioning(str(est / "bin" / "cctally-alpha-test")),
        timeout=remaining(deadline),
    ), _descendants_mentioning(str(est / "bin" / "cctally-alpha-test"))


def _scheduler_record(root):
    text = (_run_dirs(root)[0] / "scheduler.tsv").read_text(encoding="utf-8")
    return dict(
        line.split("\t", 1) for line in text.splitlines() if "\t" in line
    )


def test_a_run_records_the_dispatch_order_it_actually_used(tmp_path):
    """#630 S3. `scheduler=` is printed by plan mode and the fallback reason is
    a stderr line, and an authoritative run emits neither — so without this
    sidecar a measurement run carries no record of the treatment condition that
    produced its timings, and twenty-eight of them would be uninterpretable.

    Both arms are asserted in the pair of tests below rather than one, because
    a sidecar hard-coded to `fallback` would satisfy either one alone.
    """
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 0, res.stdout + res.stderr
    # This estate carries no duration table, so the scheduler falls back — and
    # the record says so rather than staying silent. The digest field states a
    # CAUSE rather than a hash when there is nothing to hash, and which cause
    # applies is deliberately not pinned here: under pytest,
    # tests/isolation_bootstrap/sitecustomize.py appends the real repository's
    # bin/ to every child process's sys.path, so the validator imports even
    # though this estate does not carry it and the cause recorded is the
    # table's own absence rather than the validator's.
    record = _scheduler_record(root)
    assert record["mode"] == "fallback", record
    assert not record["table_digest"].startswith("sha256:"), record


def test_the_scheduler_record_names_the_table_it_dispatched_from(tmp_path):
    """The other arm. The digest is compared against the bytes on disk, so a
    record that reported a constant, or hashed the wrong file, fails here."""
    import hashlib

    est = _estate(tmp_path)
    # Not what makes this case work: under pytest,
    # tests/isolation_bootstrap/sitecustomize.py appends the real repository's
    # bin/ to every child process's sys.path, so the scheduler would import the
    # validator with or without this copy. The copy is kept so the estate is
    # self-sufficient rather than resting on that leak, and so the module the
    # scheduler imports is the one this estate declares — sys.path.insert puts
    # the estate's own bin/ first.
    shutil.copy2(
        BIN / "_lib_harness_durations.py", est / "bin" / "_lib_harness_durations.py"
    )
    table = est / "tests" / "authoritative-harness-durations.tsv"
    table.write_text("alpha\t7\nreconcile\t3\n", encoding="utf-8")
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 0, res.stdout + res.stderr
    record = _scheduler_record(root)
    assert record["mode"] == "lpt", record
    assert record["table"] == "tests/authoritative-harness-durations.tsv", record
    assert record["table_digest"] == (
        "sha256:" + hashlib.sha256(table.read_bytes()).hexdigest()
    ), record


def test_the_recorded_digest_is_the_one_the_dispatch_decision_was_taken_from(
    tmp_path,
):
    """The record must describe the dispatch decision, not the tree at teardown.

    `SCHEDULER_MODE` is decided before the pool starts and the digest used to be
    recomputed after it finished, so the two fields could describe different
    bytes. A run leases its workdir for the whole sixteen minutes, which is the
    window in which a tree can change under it. This case makes that window
    deterministic: the `alpha` harness rewrites the duration table while the
    pool is running, and the record must still name the bytes the scheduler
    read.
    """
    import hashlib

    est = _estate(tmp_path)
    # Same reason as the sibling case above: sitecustomize would supply the
    # validator anyway, and resting the only proof of the dispatch-time
    # digest on that leak is exactly the trap this estate documents.
    shutil.copy2(
        BIN / "_lib_harness_durations.py", est / "bin" / "_lib_harness_durations.py"
    )
    table = est / "tests" / "authoritative-harness-durations.tsv"
    dispatched = "alpha\t7\nreconcile\t3\n"
    table.write_text(dispatched, encoding="utf-8")
    (est / "bin" / "cctally-alpha-test").write_text(
        "#!/usr/bin/env bash\n"
        "printf 'alpha\\t99\\nreconcile\\t1\\n' > %s\n"
        "printf '%%s\\n' %s\n" % (_sq(str(table)), _sq(DEFAULT_SUMMARY)),
        encoding="utf-8",
    )
    (est / "bin" / "cctally-alpha-test").chmod(0o755)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 0, res.stdout + res.stderr
    # Non-vacuity: without the mid-run rewrite the two digests are equal and
    # this case cannot tell a dispatch-time record from a teardown-time one.
    assert table.read_text(encoding="utf-8") != dispatched, table.read_text(
        encoding="utf-8"
    )
    record = _scheduler_record(root)
    assert record["mode"] == "lpt", record
    assert record["table_digest"] == (
        "sha256:" + hashlib.sha256(dispatched.encode("utf-8")).hexdigest()
    ), record


def test_a_completed_run_leaves_no_reporter_behind(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
            "CCTALLY_PROGRESS_INTERVAL": "1",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    survivors = _descendants_mentioning(str(est / "bin" / "cctally-test-all"))
    assert not survivors, survivors
    # Reporter state is removed; the worker sidecars that ARE evidence stay.
    run = _run_dirs(root)[0]
    assert not (run / "logs" / ".progress").exists()
    assert (run / "logs" / "alpha.done").exists()
    assert (run / "timings" / "alpha.seconds").is_file()


def test_a_serial_run_still_reports_progress(tmp_path):
    """Everything must work at OUTER=1, the fully serial CCTALLY_TEST_JOBS=1
    mode the remote wrapper and CI both use for reproducibility."""
    est = _estate(
        tmp_path, harnesses={"alpha": [DEFAULT_SUMMARY], "beta": [DEFAULT_SUMMARY]}
    )
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_TEST_JOBS": "1",
            "CCTALLY_PROGRESS_INTERVAL": "1",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    completions = _completions(res.stderr)
    assert len(completions) == 3, res.stderr
    assert "outer=1 inner=1 pytest=1" in res.stdout


# --------------------------------------------- sanitized console and extract
#
# In CI stderr IS the GitHub Actions log, which leaves the runner exactly as an
# artifact does, so sanitizing only the uploaded file would leave the larger
# channel open. Sanitization therefore applies wherever an evidence root
# exists. With no root — a bare local run — stderr stays raw, because nothing
# leaves the machine that produced it and the temporary log is the only copy.

CANARY_TAIL = ".claude/projects/x/secret.jsonl"
# The line `bin/_lib-golden-diff.sh:58` emits from the chokepoint every fixture
# harness compares through. If the sanitizer redacts this, it produces no
# usable artifact on the commonest real failure there is.
CANONICAL_FAILURE = "FAIL alpha: stdout diverged"
# Printed BEFORE the marker, which is the case the window rewrite exists for:
# the forward-only awk rule discarded it and filled the window with the next
# case's output instead.
PRECEDING_DIAGNOSTIC = "expected 5 cases, actual 3"


def _canary_estate(tmp_path, extra_lines=()):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    canary = f"{home}/{CANARY_TAIL}"
    lines = [
        "passed: 0   failed: 0",
        PRECEDING_DIAGNOSTIC,
        f"reading {canary}",
        CANONICAL_FAILURE,
        "    ",
        *extra_lines,
        "passed: 1   failed: 1",
    ]
    est = _estate(tmp_path, harnesses={"alpha": lines}, exits={"alpha": 1})
    return est, canary


@pytest.mark.skipif(
    not VOCABULARY_AVAILABLE,
    reason="the disclosure vocabulary producer is maintainer-local; without it "
    "the transformer redacts every detail by design",
)
def test_the_console_is_sanitized_when_an_evidence_root_exists(tmp_path):
    est, canary = _canary_estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr

    run = _run_dirs(root)[0]
    raw = (run / "logs" / "alpha.log").read_text()
    # Non-vacuity, asserted against the carrier line at its exact position:
    # the canary really was in the raw log, on its own line, so its absence
    # from stderr is the sanitizer and not a harness that never printed it.
    assert f"reading {canary}" in raw.splitlines(), raw
    assert canary not in res.stderr, "a raw production path reached the console"
    assert str(tmp_path / "home") not in res.stderr

    # And the console is still diagnosable. Verified by execution rather than
    # by reading: a sanitizer that redacts the commonest real failure line
    # produces no usable artifact at all.
    assert CANONICAL_FAILURE in res.stderr, res.stderr
    assert PRECEDING_DIAGNOSTIC in res.stderr, res.stderr


# The console block's own glue — `scrub-log` — had no test of any kind while
# its behaviour changed from "drop the whole block" to "replace the offending
# line, publish the rest, state what went". The two branches are only
# reachable when the validator flags an emitted line, which by design happens
# only when the transformer is broken, so the kernel is loaded through a shim
# that adds one violation. The shim re-exports the real kernel unchanged and
# leaves `apply_validation_redactions` and its internal re-validation exactly
# as shipped: the glue is what is under test, not the kernel.
_VALIDATOR_SHIM = '''
import importlib.util as _ilu
import os as _os
import sys as _sys

_spec = _ilu.spec_from_file_location(
    "_real_evidence_kernel", _os.environ["EV_SHIM_REAL_KERNEL"]
)
_real = _ilu.module_from_spec(_spec)
_sys.modules["_real_evidence_kernel"] = _real
_spec.loader.exec_module(_real)
for _name in dir(_real):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_real, _name)

_REASON = _os.environ["EV_SHIM_REASON"]


def validate_export(lines, roots=None):
    """The real verdict, plus one violation on the LAST line.

    Addressed by position rather than by content so the case does not depend
    on which shapes the transformer happened to retain.
    """
    violations = list(_real.validate_export(lines, roots))
    if _REASON and lines and not any(
        v["index"] == len(lines) - 1 for v in violations
    ):
        violations.append(
            {
                "index": len(lines) - 1,
                "reason": _REASON,
                "excerpt": str(lines[-1])[:120],
            }
        )
    violations.sort(key=lambda v: v["index"])
    return violations
'''


def _scrub_log(tmp_path, log_lines, reason, kernel=EVIDENCE_KERNEL):
    """Run the aggregator's embedded `scrub-log` mode over one subject log.

    `kernel` is the module the shim re-exports, so a case that needs a broken
    transformer rather than an injected violation supplies a mangled copy.
    """
    source = RUNNER.read_text(encoding="utf-8")
    marker = "python3 - \"$EVIDENCE_KERNEL\" \"$EVIDENCE_PRIVATE\" \"$@\" <<'EVPY'\n"
    bridge = source.split(marker, 1)[1].split("\nEVPY\n", 1)[0]

    shim = tmp_path / "_lib_test_evidence_shim.py"
    shim.write_text(_VALIDATOR_SHIM, encoding="utf-8")
    logfile = tmp_path / "alpha.log"
    logfile.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    env = dict(
        os.environ,
        LC_ALL="C",
        TZ="Etc/UTC",
        HOME=str(tmp_path / "home"),
        EV_REPO_ROOT=str(tmp_path / "repo"),
        EV_SHIM_REAL_KERNEL=str(kernel),
        EV_SHIM_REASON=reason,
    )
    return subprocess.run(
        [
            sys.executable,
            "-",
            str(shim),
            # The private disclosure producer is deliberately absent, which is
            # the fail-closed context the public tree runs under.
            os.devnull,
            "scrub-log",
            "alpha",
            str(logfile),
        ],
        input=bridge,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


_SCRUB_LOG_LINES = (
    "passed: 0   failed: 0",
    PRECEDING_DIAGNOSTIC,
    CANONICAL_FAILURE,
    "Verdict: FAIL",
    "passed: 1   failed: 1",
)


def test_the_console_replaces_a_structurally_refused_line_and_keeps_the_block(
    tmp_path,
):
    """Per LINE on the console, exactly as in the export file.

    The two used to differ: the console dropped the whole subject block on any
    violation, which is the same over-redaction the sanitizer itself was
    faulted for, one level up.
    """
    res = _scrub_log(tmp_path, _SCRUB_LOG_LINES, "unknown-counter-word")
    assert res.returncode == 0, res.stdout + res.stderr
    lines = res.stdout.splitlines()
    # The offending line's own bytes are gone and the leg that refused it is
    # named, so an operator can tell a sanitizer fault from a missing failure.
    assert (
        "[REDACTED: line refused by the validator: unknown-counter-word]"
        in lines
    ), lines
    # The rest of the block really did survive.
    assert "passed: 0 [REDACTED: unclassified detail]" in lines, lines
    assert len(lines) == len(_SCRUB_LOG_LINES) + 1, lines
    # Never silent: the count and the distinct reasons close the block.
    assert lines[-1] == (
        "[REDACTED: 1 of 5 lines were refused by the validator and replaced; "
        "reasons: unknown-counter-word]"
    ), lines
    assert "context refused by the validator" not in res.stdout, res.stdout


def test_the_console_withholds_the_block_when_a_content_leg_fires(tmp_path):
    """The other branch, which names the CAUSE of the refusal.

    A content violation means the transformer leaked rather than a leg
    over-firing, so nothing around it can be trusted and the block is
    withheld whole.
    """
    res = _scrub_log(tmp_path, _SCRUB_LOG_LINES, "email")
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout == (
        "[REDACTED: alpha context refused by the validator, 1 violations, "
        "cause: email]\n"
    ), res.stdout
    assert "passed: 0" not in res.stdout, res.stdout


def test_the_console_scrub_shim_does_not_itself_manufacture_the_outcome(
    tmp_path,
):
    """Non-vacuity for the two cases above.

    Without the injected violation the same log publishes its whole block and
    no notice at all, so both outcomes above are the glue reacting to the
    violation rather than to the shim being present.
    """
    res = _scrub_log(tmp_path, _SCRUB_LOG_LINES, "")
    assert res.returncode == 0, res.stdout + res.stderr
    lines = res.stdout.splitlines()
    assert len(lines) == len(_SCRUB_LOG_LINES), lines
    assert not any("refused by the validator" in line for line in lines), lines


def test_the_console_stays_raw_without_an_evidence_root(tmp_path):
    """Nothing leaves the machine, and the temporary log is the only copy."""
    est, canary = _canary_estate(tmp_path)
    res = _drive(est, tmp_path)
    assert res.returncode == 1, res.stdout + res.stderr
    assert canary in res.stderr, "a bare local run must keep its raw context"


def test_ci_alone_does_not_turn_sanitization_on(tmp_path):
    """Sanitization is gated on an explicit root, never inferred from CI.

    Sanitizing is only information-preserving because the raw log is retained
    beside the sanitized copy. Deriving a root from `GITHUB_ACTIONS` and
    `RUNNER_TEMP` broke that: `$RUNNER_TEMP` is destroyed when the job ends and
    no workflow uploads the directory, so the sanitized console was the only
    surviving copy and CI failure diagnosis got worse, not better. The gate is
    therefore `CCTALLY_TEST_EVIDENCE_ROOT` and nothing else; a workflow turns
    the whole subsystem on by setting it and uploading `export/` in the same
    change, so the two can never be separated again.
    """
    est, canary = _canary_estate(tmp_path)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    res = _drive(
        est,
        tmp_path,
        {"GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(runner_temp)},
    )
    assert res.returncode == 1, res.stdout + res.stderr
    # Nothing was retained anywhere under the runner's temporary directory ...
    assert not list(runner_temp.rglob("manifest.json")), sorted(
        str(p) for p in runner_temp.rglob("*")
    )
    # ... so the console keeps the raw context that is now the only copy.
    assert canary in res.stderr, res.stderr


@pytest.mark.skipif(not VOCABULARY_AVAILABLE, reason="maintainer-local producer")
def test_the_export_preserves_the_diagnostic_that_precedes_the_marker(tmp_path):
    """Acceptance criterion 6."""
    est, canary = _canary_estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr
    export = (_run_dirs(root)[0] / "export" / "failure-context.txt").read_text()
    assert canary not in export, export
    assert CANONICAL_FAILURE in export, export
    assert PRECEDING_DIAGNOSTIC in export, export
    # The forward-only rule this replaces started AT the marker, so the
    # explanation had to be discarded. Assert the ordering the fix produces.
    assert export.index(PRECEDING_DIAGNOSTIC) < export.index(CANONICAL_FAILURE)


@pytest.mark.skipif(not VOCABULARY_AVAILABLE, reason="maintainer-local producer")
def test_the_real_aggregator_threads_source_classified_case_ids_into_the_export(
    tmp_path,
):
    """The integration seam for issue #579's classified minimum field."""
    if not PRIVATE_TEST_REMOTE_HARNESS.exists():
        pytest.skip("private test-remote harness absent on the public tree")
    est = _estate(
        tmp_path,
        harnesses={"test-remote": ["passed: 1   failed: 1"]},
        exits={"test-remote": 1},
    )
    harness = est / "bin" / "cctally-test-remote-test"
    harness.write_text(
        """#!/usr/bin/env bash
case_source_classified_identity(){ :; }
printf '%s\n' 'CASE: test-remote/case_source_classified_identity line 3'
printf '%s\n' 'CASE: test-remote/case_acme_holdings_invoice line 4'
printf '%s\n' 'FAIL: private detail ops@example.com'
printf '%s\n' 'passed: 1   failed: 1'
exit 1
""",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(est)], check=True)
    subprocess.run(
        ["git", "-C", str(est), "add", "bin/cctally-test-remote-test"],
        check=True,
    )

    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr
    run = _run_dirs(root)[0]
    raw = (run / "logs" / "test-remote.log").read_text()
    export = (run / "export" / "failure-context.txt").read_text()

    known = "CASE: test-remote/case_source_classified_identity line 3"
    forged = "CASE: test-remote/case_acme_holdings_invoice line 4"
    private = "FAIL: private detail ops@example.com"
    for carrier in (known, forged, private):
        assert carrier in raw, raw
    assert known in export, export
    assert forged not in export, export
    assert "acme" not in export and "invoice" not in export, export
    assert "ops@example.com" not in export, export
    assert "CASE: test-remote/[REDACTED: unclassified detail]" in export, export


@pytest.mark.skipif(not VOCABULARY_AVAILABLE, reason="maintainer-local producer")
def test_indentation_counters_and_progress_families_survive_the_sanitizer(
    tmp_path,
):
    est, canary = _canary_estate(
        tmp_path,
        extra_lines=[
            "Timing: total=1054s  shell-pool=548s  pytest=506s",
            "[ 38/56] PASS  diff  340 cases  48s",
            "        ",
        ],
    )
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr
    export = (
        _run_dirs(root)[0] / "export" / "failure-context.txt"
    ).read_text().splitlines()
    for kept in (
        "Timing: total=1054s  shell-pool=548s  pytest=506s",
        "[ 38/56] PASS  diff  340 cases  48s",
        "passed: 1   failed: 1",
        "        ",
    ):
        assert kept in export, (kept, export)


@pytest.mark.skipif(not VOCABULARY_AVAILABLE, reason="maintainer-local producer")
def test_an_unreadable_summary_dump_is_sanitized_too(tmp_path):
    """The second of the four raw stderr paths: the whole-log dump for a
    harness that finished without a parseable summary."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    canary = f"{home}/{CANARY_TAIL}"
    est = _estate(
        tmp_path,
        harnesses={"alpha": [f"reading {canary}", "no summary here"]},
        exits={"alpha": 0},
    )
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 3, res.stdout + res.stderr
    raw = (_run_dirs(root)[0] / "logs" / "alpha.log").read_text()
    assert f"reading {canary}" in raw.splitlines()
    assert "---- alpha output" in res.stderr, res.stderr
    assert canary not in res.stderr


@pytest.mark.skipif(not VOCABULARY_AVAILABLE, reason="maintainer-local producer")
def test_the_pytest_dump_is_sanitized(tmp_path):
    """The third and fourth raw stderr paths."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    canary = f"{home}/{CANARY_TAIL}"
    est = _estate(tmp_path)
    (est / "tests" / "test_scratch_smoke.py").write_text(
        "def test_boom():\n"
        f"    assert 2 + 2 == 5, {canary!r}\n",
        encoding="utf-8",
    )
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr
    raw = (_run_dirs(root)[0] / "logs" / "pytest.log").read_text()
    assert canary in raw, "the raw pytest log must retain it"
    assert "---- pytest FAIL details ----" in res.stderr
    assert canary not in res.stderr
    export = (_run_dirs(root)[0] / "export" / "failure-context.txt").read_text()
    assert ">       assert 2 + 2 == 5" in export, export
    assert "<path>:2: AssertionError" in export, export


@pytest.mark.skipif(not VOCABULARY_AVAILABLE, reason="maintainer-local producer")
def test_the_export_names_the_failing_node_and_its_exception_class(tmp_path):
    """#630 S1 F1's exit criterion, asserted on the real retained artifact.

    A kernel test can pass while the wiring is broken, so this drives the real
    aggregator over a real pytest failure and reads the file an operator would
    read. The published extract for the v1.101.0 failure was 95% repetitions of
    `[REDACTED: unclassified line]`; what must survive now is the failing node
    id, the exception class, the `E ` gutter and pytest's counters line — and
    what must not survive is the exception message.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    canary = f"{home}/{CANARY_TAIL}"
    est = _estate(tmp_path)
    (est / "tests" / "test_scratch_smoke.py").write_text(
        "def test_boom():\n"
        f"    raise TimeoutError({canary!r})\n",
        encoding="utf-8",
    )
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr
    raw = (_run_dirs(root)[0] / "logs" / "pytest.log").read_text()
    # Non-vacuity: the raw log really carries both the node id and the message,
    # so the assertions below measure the sanitizer rather than an empty input.
    assert "test_scratch_smoke.py::test_boom" in raw, raw
    assert canary in raw, raw
    export = (_run_dirs(root)[0] / "export" / "failure-context.txt").read_text()
    assert "FAILED <path>::test_boom - TimeoutError: " in export, export
    assert re.search(
        r"^E\s+TimeoutError: \[REDACTED: exception message\]$", export, re.M
    ), export
    assert "<path>:2: TimeoutError" in export, export
    assert re.search(r"^=*\s*\d+ failed in [\d.]+s", export, re.M), export
    assert canary not in export, export
    assert CANARY_TAIL not in export, export


def test_an_undecodable_log_leaves_no_export_at_all(tmp_path):
    """Spec section 3: a parse error, undecodable input or failed validation
    leaves no export file at all.

    Reading with `errors="replace"` broke that promise quietly — undecodable
    bytes became U+FFFD and an export was published over content nothing had
    decoded. Refusing is the fail-closed half of the same rule the validator
    enforces, and it costs nothing, because the complete raw log stays under
    `logs/` and the caller says so.
    """
    est = _estate(
        tmp_path,
        harnesses={"alpha": ["FAIL alpha: stdout diverged", "passed: 1   failed: 1"]},
        exits={"alpha": 1},
    )
    harness = est / "bin" / "cctally-alpha-test"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' 'FAIL alpha: stdout diverged'\n"
        # One byte that is not valid UTF-8 anywhere in the stream.
        "printf '\\xff\\n'\n"
        "printf '%s\\n' 'passed: 1   failed: 1'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr
    run = _run_dirs(root)[0]
    assert not (run / "export" / "failure-context.txt").exists()
    # The verdict is untouched, the raw bytes are retained, and the reader is
    # told where they are — an observability refusal must never become an
    # outage or a silence.
    assert (run / "logs" / "alpha.log").read_bytes().count(b"\xff") == 1
    assert "the complete unsanitized logs are retained" in res.stderr, res.stderr


def _embedded_export(tmp_path, log_lines, extra_env=None,
                     kernel=EVIDENCE_KERNEL):
    """The aggregator's OWN export mode, over a log this test controls.

    `EV_COVERAGE_NOTE` reaches the extract HEADER unscrubbed — it is the
    aggregator's own sentence — and is validated rather than transformed, so
    it is the one place a test can put a line the validator refuses without
    also mutating the transformer.
    """
    source = RUNNER.read_text(encoding="utf-8")
    marker = "python3 - \"$EVIDENCE_KERNEL\" \"$EVIDENCE_PRIVATE\" \"$@\" <<'EVPY'\n"
    bridge = source.split(marker, 1)[1].split("\nEVPY\n", 1)[0]
    evidence = tmp_path / "run"
    (evidence / "export").mkdir(parents=True)
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "alpha.log"
    log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    listing = tmp_path / "failed-subjects"
    listing.write_text("alpha\t%s\n" % log, encoding="utf-8")
    env = dict(os.environ, LC_ALL="C", TZ="Etc/UTC")
    env.update({
        "EV_FAILED_LIST": str(listing),
        "EV_RUN_ID": "r-1",
        "EV_REPO_ROOT": str(tmp_path),
        "EV_COVERAGE_NOTE": "",
    })
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, "-", str(kernel), os.devnull,
         "export", str(evidence)],
        input=bridge, env=env, capture_output=True, text=True, check=False,
    )
    return evidence, result


# Refused by the validator's counters leg, and NOT produced by the
# transformer: it reaches the extract through the header, which the aggregator
# writes itself.
REFUSED_HEADER_NOTE = "1 failed, 100 sprocketed in 45.67s"


def test_a_refused_extract_line_is_replaced_and_the_rest_is_published(tmp_path):
    """A validator violation degrades the extract per LINE, not to zero bytes.

    Refusing the whole file meant one false positive anywhere in the retained
    window cost the operator every byte of diagnostic evidence — the same
    failure class as over-redaction, at the file level. Three validator legs
    prescribed during this session alone would have triggered exactly that.
    """
    evidence, result = _embedded_export(
        tmp_path,
        ["FAIL alpha: stdout diverged"] + ["passed: 1   failed: 1"] * 12,
        {"EV_COVERAGE_NOTE": REFUSED_HEADER_NOTE},
    )
    assert result.returncode == 0, result.stderr
    export = evidence / "export" / "failure-context.txt"
    assert export.exists(), result.stderr
    text = export.read_text(encoding="utf-8")
    # Fail-closed for the offending line: its bytes never reached the file.
    assert "sprocketed" not in text, text
    # The rest of the extract survived.
    assert "cctally-test-all] run r-1" in text, text
    # Never silent — in the file, on stderr, and in the machine record.
    assert "refused by the validator" in text, text
    assert "unknown-counter-word" in text, text
    assert "the validator refused 1 of" in result.stderr, result.stderr
    record = json.loads(
        (evidence / "validator-redactions.json").read_text(encoding="utf-8"))
    assert record["redacted"] == 1, record
    assert record["reasons"] == ["unknown-counter-word"], record
    # The sidecar is a SIBLING of export/, because only two files may leave
    # the runner and the workflow gate is written against exactly that pair.
    assert not (evidence / "export" / "validator-redactions.json").exists()


def test_a_clean_extract_records_a_measured_zero_rather_than_nothing(tmp_path):
    evidence, result = _embedded_export(
        tmp_path, ["FAIL alpha: stdout diverged", "passed: 1   failed: 1"])
    assert result.returncode == 0, result.stderr
    record = json.loads(
        (evidence / "validator-redactions.json").read_text(encoding="utf-8"))
    assert record["redacted"] == 0 and record["reasons"] == [], record
    assert "refused by the validator" not in (
        evidence / "export" / "failure-context.txt").read_text(encoding="utf-8")


# ------------------------- #769 S7: the transformer-health canary's own glue
#
# The kernel's canary refuses a whole publication BEFORE the validator reaches
# any verdict, so that refusal carries no violation and names no leg. Both
# operator messages interpolated `len(violations)` — which is zero here — and
# both attributed the refusal to the validator, so in the one case where the
# operator loses every byte of failure evidence the two lines reported that
# nothing had been refused and pointed at the wrong subsystem. Nothing else
# corrects them: the export step is deliberately non-deciding, its failure
# becomes a `contract_note`, and neither the verdict nor the exit code changes.

#: The cause the kernel records for a failed canary, and the cause both messages
#: must name. Declared here rather than read from the kernel, because a case
#: that read the constant would agree with whatever string the kernel carried,
#: including one that no longer states what happened.
TRANSFORMER_HEALTH_REFUSAL = "transformer-health-check-failed"

#: The transformer entry the canary's verdict depends on, as the kernel spells
#: it. Deleting this entry is the mutation the kernel's own
#: `test_the_canary_is_claimed_only_by_the_token_entry` performs: the canary line
#: is then reduced by nothing, which is the single condition the health check
#: reports, and every validator leg is left exactly as shipped.
#:
#: TWO SOURCE LINES, and the constant must carry BOTH. #820 added four prefixed
#: spellings to this entry and the result no longer fits one line. A constant
#: that matched only the first of them would delete a fragment of a tuple
#: element and leave the copied kernel unparseable, which reaches the surfaces
#: under test as a kernel that cannot load rather than as the unreduced canary
#: line this mangle stages. The two are reported identically from here, so the
#: helper below compiles the result rather than trusting this comment.
TRANSFORMER_TOKEN_ENTRY_SOURCE = (
    '    (re.compile(r"(?i)\\b(?:token|(?:access|refresh|id|bearer)_token)\\b"\n'
    '                r"\\s*[:=]\\s*\\S+"), "<credential>"),\n'
)

#: Thirteen lines no leg refuses, so an export over them is refused for the
#: reason the case injects and for nothing else. The same log the per-line
#: export case above uses.
_EXPORT_LOG_LINES = (
    ["FAIL alpha: stdout diverged"] + ["passed: 1   failed: 1"] * 12
)


def _kernel_with_a_failing_canary(tmp_path, name):
    """A copy of the evidence kernel whose transformer fails its own canary.

    Mangled as a FILE, because both surfaces load the kernel in a subprocess.
    The canary constants are left alone: a failing transformer is the condition
    the branch exists to report, and mangling the expected placeholder instead
    would exercise a broken check rather than a broken transformer.
    """
    target = tmp_path / name / EVIDENCE_KERNEL.name
    target.parent.mkdir(parents=True, exist_ok=True)
    source = EVIDENCE_KERNEL.read_text(encoding="utf-8")
    assert source.count(TRANSFORMER_TOKEN_ENTRY_SOURCE) == 1, (
        "the transformer's token entry is no longer the source this mangle "
        "deletes, so the mangle would leave the transformer healthy and this "
        "case would assert a refusal that never happened"
    )
    mangled = source.replace(TRANSFORMER_TOKEN_ENTRY_SOURCE, "", 1)
    # A partial match would delete a fragment of the entry, and a kernel that
    # cannot be parsed fails the surfaces under test for a reason this case
    # does not name. Compiling here separates the two.
    compile(mangled, str(target), "exec")
    target.write_text(mangled, encoding="utf-8")
    return target


def test_a_failed_canary_names_the_transformer_on_both_operator_surfaces(
    tmp_path,
):
    """#769 S7. The console line and the export line, in their canary form.

    Both counts are compared against what the run actually withheld — the line
    count a healthy run over the same input publishes, and for the export the
    sidecar record as well — rather than against a literal typed here. Reporting
    zero was the defect, and zero is exactly what a count taken from the
    violations prints on this path, so a case that pinned only the sentence
    would keep passing with the count still wrong.
    """
    broken = _kernel_with_a_failing_canary(tmp_path, "broken")

    healthy_console = _scrub_log(tmp_path, _SCRUB_LOG_LINES, "")
    assert healthy_console.returncode == 0, healthy_console.stderr
    published = healthy_console.stdout.splitlines()
    assert len(published) == len(_SCRUB_LOG_LINES), published
    console = _scrub_log(tmp_path, _SCRUB_LOG_LINES, "", kernel=broken)
    assert console.returncode == 0, console.stdout + console.stderr
    assert console.stdout == (
        "[REDACTED: alpha context withheld whole: the transformer failed its "
        "health canary, so all %d lines were withheld; cause: %s]\n"
        % (len(published), TRANSFORMER_HEALTH_REFUSAL)
    ), console.stdout
    # The count the message states is neither zero nor a number this case
    # supplied: it is every line the healthy run published from the same log.
    assert len(published) != 0, published
    # Fail-closed all the same — the withheld block's own bytes are gone.
    assert CANONICAL_FAILURE not in console.stdout, console.stdout

    healthy_dir, healthy = _embedded_export(
        tmp_path / "healthy", _EXPORT_LOG_LINES
    )
    assert healthy.returncode == 0, healthy.stderr
    extract = (
        (healthy_dir / "export" / "failure-context.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(extract) != 0, extract
    evidence, res = _embedded_export(
        tmp_path / "refused", _EXPORT_LOG_LINES, kernel=broken
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert not (evidence / "export" / "failure-context.txt").exists()
    assert (
        "cctally-test-all: the transformer failed its health canary, so all %d "
        "lines of the sanitized extract were withheld (cause: %s); no export "
        "file was written" % (len(extract), TRANSFORMER_HEALTH_REFUSAL)
    ) in res.stderr.splitlines(), res.stderr
    sidecar = json.loads(
        (evidence / "validator-redactions.json").read_text(encoding="utf-8")
    )
    assert sidecar["refused"] is True, sidecar
    assert sidecar["refusal"] == TRANSFORMER_HEALTH_REFUSAL, sidecar
    # The machine-readable record and the healthy publication agree on the
    # withheld count, and the message states that number rather than the
    # violation count the old wording used.
    assert sidecar["total"] == len(extract), (sidecar, extract)
    assert sidecar["redacted"] == 0, sidecar


def test_an_ordinary_content_refusal_still_names_the_validator_on_both_surfaces(
    tmp_path,
):
    """#769 S7. The other arm of the same branch, byte for byte.

    A repair that improved the canary wording by respelling both paths would
    leave the commoner refusal saying the wrong thing, and the two cannot share
    one sentence: this count is the number of lines the validator refused,
    while the canary's is the number withheld. The console arm restates the
    sentence the `email` case above pins, under the reason the export arm uses,
    so one command covers both surfaces for one refusal cause.
    """
    console = _scrub_log(tmp_path, _SCRUB_LOG_LINES, "unsubstituted-root")
    assert console.returncode == 0, console.stdout + console.stderr
    assert console.stdout == (
        "[REDACTED: alpha context refused by the validator, 1 violations, "
        "cause: unsubstituted-root]\n"
    ), console.stdout
    assert "health canary" not in console.stdout, console.stdout

    # An unsubstituted root, reached the way the per-line export case reaches
    # its violation: the coverage note is the aggregator's own sentence, so it
    # is validated without being transformed and the transformer stays healthy.
    root = tmp_path / "refused"
    evidence, res = _embedded_export(
        root,
        _EXPORT_LOG_LINES,
        {"EV_COVERAGE_NOTE": "the retained artifact is %s/logs" % root},
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert not (evidence / "export" / "failure-context.txt").exists()
    sidecar = json.loads(
        (evidence / "validator-redactions.json").read_text(encoding="utf-8")
    )
    assert sidecar["refusal"] == "unsubstituted-root", sidecar
    assert sidecar["redacted"] == 1, sidecar
    assert (
        "cctally-test-all: the sanitized extract was refused by the validator "
        "(%d of %d lines, cause: unsubstituted-root); no export file was "
        "written" % (sidecar["redacted"], sidecar["total"])
    ) in res.stderr.splitlines(), res.stderr
    assert "health canary" not in res.stderr, res.stderr


def test_no_extract_is_written_for_a_passing_run(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 0, res.stdout + res.stderr
    run = _run_dirs(root)[0]
    assert not (run / "export" / "failure-context.txt").exists()
    assert (run / "export" / "outcome.json").exists()


def test_the_export_directory_holds_only_the_two_publishable_files(tmp_path):
    """`export/` is a sibling of `logs/` and never a parent, so a careless
    recursive upload of the export directory cannot capture a raw log."""
    est, _canary = _canary_estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr
    export = _run_dirs(root)[0] / "export"
    assert sorted(p.name for p in export.rglob("*")) == [
        "failure-context.txt",
        "outcome.json",
    ]
    for path in export.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


@pytest.mark.skipif(not VOCABULARY_AVAILABLE, reason="maintainer-local producer")
def test_the_export_states_its_run_and_where_the_full_logs_are(tmp_path):
    est, _canary = _canary_estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 1, res.stdout + res.stderr
    run = _run_dirs(root)[0]
    head = (run / "export" / "failure-context.txt").read_text().splitlines()[:2]
    assert run.name in head[0], head
    assert head[0].startswith("[cctally-test-all]"), head
    # The second line points at the retained raw copy, which is the whole
    # justification for handing the reader a sanitized one. Pinned in full
    # because it once read "the 1 complete unsanitized log set" — a stray
    # numeral in the artifact's second line, with no assertion over it.
    assert head[1] == (
        "[cctally-test-all] the complete unsanitized logs are retained "
        "under logs/ on the runner"
    ), head


# ------------------------------------------------------ retention and eviction


def _seed_run(
    root,
    run_id,
    remote_dir="cctally-dev",
    state="completed",
    outcome="pass",
    started=1,
    size=600_000,
    pid=None,
    pid_start="",
):
    run = pathlib.Path(root) / remote_dir / run_id
    (run / "logs").mkdir(parents=True)
    (run / "export").mkdir(parents=True)
    (run / "logs" / "big.log").write_text("x" * size, encoding="utf-8")
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runId": run_id,
                "remoteDir": remote_dir,
                "state": state,
                "outcome": outcome,
                "failureClass": "none" if outcome == "pass" else "product",
                "exitCode": 0 if outcome == "pass" else 1,
                "pid": pid,
                "pidStart": pid_start,
                "startedEpoch": started,
                "finishedEpoch": started + 60,
            }
        ),
        encoding="utf-8",
    )
    return run


def _retention(root):
    return json.loads((pathlib.Path(root) / ".retention.json").read_text())


def _embedded_retention(root, current_run, extra_env=None):
    """The aggregator's OWN retention pass, run exactly as the aggregator runs
    it — the same extraction idiom `_embedded_pid_start` uses."""
    source = RUNNER.read_text(encoding="utf-8")
    marker = "python3 - \"$EVIDENCE_KERNEL\" \"$EVIDENCE_PRIVATE\" \"$@\" <<'EVPY'\n"
    bridge = source.split(marker, 1)[1].split("\nEVPY\n", 1)[0]
    env = dict(os.environ, LC_ALL="C", TZ="Etc/UTC")
    env.update(extra_env or {})
    result = subprocess.run(
        [
            sys.executable, "-",
            str(EVIDENCE_KERNEL), os.devnull,
            "retention", str(root), current_run,
        ],
        input=bridge,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads((pathlib.Path(root) / ".retention.json").read_text()), result


def _tree_bytes(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            total += os.lstat(os.path.join(base, name)).st_size
    return total


def _store_bytes(root):
    """Every run directory's bytes — what `bytesAfter` accounts for.

    The record's own file and lock live at the root and are deliberately not
    counted: `.retention.json` is written after the number is computed, so
    including it would make the figure describe a store that did not yet exist.
    """
    total = 0
    for remote in sorted(pathlib.Path(root).iterdir()):
        if not remote.is_dir():
            continue
        for run in sorted(remote.iterdir()):
            if run.is_dir():
                total += _tree_bytes(run)
    return total


def test_record_is_rebuilt_from_disk_when_a_deletion_fails(tmp_path):
    """A failed `rmtree` must not leave the record claiming the bytes are gone.

    `plan["evict"] = removed` already made the evicted-run count honest, but
    `bytesAfter`, `overCap`, `retainedRuns` and `gaps` still came from the
    PLANNED set, so a deletion that failed left the record understating what
    is actually on disk — and the byte cap is enforced against that number.
    """
    root = tmp_path / "ev"
    now = int(time.time())
    _seed_run(root, "doomed", started=now - 3600, size=400_000)
    _seed_run(root, "kept", started=now - 60, size=400_000)
    # `rmtree` unlinks each entry from the directory that holds it, so every
    # directory ALONG the doomed run has to be read-only for the deletion to
    # fail outright. Making only the top one read-only lets rmtree strip the
    # contents and fail on the final rmdir, which is the partial-delete case
    # the next test covers rather than this one.
    parent = root / "cctally-dev"
    doomed = parent / "doomed"
    locked = [parent, doomed] + [d for d in doomed.iterdir() if d.is_dir()]
    originals = [(d, stat.S_IMODE(d.stat().st_mode)) for d in locked]
    for directory, _mode in reversed(originals):
        os.chmod(directory, 0o555)
    try:
        record, result = _embedded_retention(
            root, "kept", {"EV_MAX_BYTES": "1000", "EV_MAX_AGE_DAYS": "3650"}
        )
    finally:
        for directory, mode in originals:
            os.chmod(directory, mode)
    # Non-vacuity: the deletion really did fail, and the run is really still
    # on disk. Without this the test would pass on a successful eviction.
    assert "could not evict" in result.stderr, result.stderr
    assert (doomed / "manifest.json").exists()
    assert record["bytesAfter"] == _store_bytes(root), (
        record["bytesAfter"], _store_bytes(root))
    assert record["retainedRuns"] == 2, record
    assert record["orphanDirs"] == 0, record
    # The eviction was PLANNED, so the pre-#630 record reported the run's
    # bytes as reclaimed and a hole where its evidence still sits.
    assert record["lastEvictedRuns"] == 0, record
    assert record["gaps"] == [], record


# A directory younger than this is a run still starting up, not an orphan.
ORPHAN_GRACE_SECONDS = _production_constant("ORPHAN_GRACE_SECONDS")
# Wide enough that the seconds a retention pass spends between `os.utime` here
# and its `os.stat` in the subprocess cannot move a directory across the
# boundary, and narrow enough that both brackets stay on their own side of it
# for any plausible production value.
ORPHAN_BOUNDARY_MARGIN_SECONDS = 15


def _age_directory(path, seconds):
    """Backdate a directory's mtime past the orphan grace window."""
    when = time.time() - seconds
    os.utime(path, (when, when))


def test_a_partially_deleted_run_is_counted_as_an_orphan(tmp_path):
    """manifest.json removed, other bytes left.

    `_load_runs` skips a directory with no readable manifest, so without
    orphan handling those bytes are invisible to both the retained-run count
    and the cap they are supposed to be measured against.
    """
    root = tmp_path / "ev"
    now = int(time.time())
    _seed_run(root, "healthy", started=now - 60, size=100_000)
    orphan = _seed_run(root, "halfgone", started=now - 120, size=300_000)
    (orphan / "manifest.json").unlink()
    # Older than the grace window, which is what separates a half-deleted run
    # from one that has not written its first manifest yet.
    _age_directory(orphan, ORPHAN_GRACE_SECONDS * 10)
    record, _ = _embedded_retention(
        root, "healthy", {"EV_MAX_BYTES": "100000000", "EV_MAX_AGE_DAYS": "3650"}
    )
    orphan_bytes = _tree_bytes(orphan)
    assert orphan_bytes > 0
    assert record["bytesAfter"] >= orphan_bytes, (record, orphan_bytes)
    assert record["bytesAfter"] == _store_bytes(root), record
    assert record["orphanDirs"] == 1, record
    # An orphan is not healthy retained evidence, so its interval is a hole.
    assert record["coverage"] == "degraded", record


def test_a_run_that_has_not_written_its_manifest_yet_is_not_an_orphan(tmp_path):
    """A concurrent run caught between `mkdir` and `_write_manifest active`.

    Counting it as an orphan flipped this pass's coverage to `degraded` — a
    false alarm on a signal whose only value is that an operator believes it.
    Its bytes are still on disk and are still counted.
    """
    root = tmp_path / "ev"
    now = int(time.time())
    _seed_run(root, "healthy", started=now - 60, size=100_000)
    starting = pathlib.Path(root) / "cctally-dev" / "just-started"
    (starting / "logs").mkdir(parents=True)
    (starting / "logs" / "partial.log").write_text("x" * 5_000, encoding="utf-8")
    record, result = _embedded_retention(
        root, "healthy", {"EV_MAX_BYTES": "100000000", "EV_MAX_AGE_DAYS": "3650"}
    )
    assert record["orphanDirs"] == 0, record
    assert record["coverage"] == "complete", record
    assert "coverage is degraded" not in result.stderr, result.stderr
    # Its bytes are NOT invisible: understating `bytesAfter` is the class the
    # rescan exists to close, and the cap is enforced against that number.
    assert record["bytesAfter"] == _store_bytes(root), record
    assert record["bytesAfter"] >= _tree_bytes(starting) > 0, record


def test_the_orphan_grace_window_is_the_production_constant(tmp_path):
    """Both sides of the REAL boundary, in one pass.

    Every other orphan test backdates far past the window, so raising the
    production constant to 1,200 seconds would have been caught and lowering
    it to 1 second would not. These two directories sit fifteen seconds either
    side of whatever `bin/cctally-test-all` actually declares, so a change in
    either direction moves one of them across and fails here.
    """
    assert ORPHAN_GRACE_SECONDS > 2 * ORPHAN_BOUNDARY_MARGIN_SECONDS, (
        "the brackets below would overlap zero")
    root = tmp_path / "ev"
    now = int(time.time())
    _seed_run(root, "healthy", started=now - 60, size=100_000)
    base = pathlib.Path(root) / "cctally-dev"
    inside = base / "inside-grace"
    outside = base / "outside-grace"
    for directory in (inside, outside):
        (directory / "logs").mkdir(parents=True)
        (directory / "logs" / "partial.log").write_text("x" * 5_000, encoding="utf-8")
    _age_directory(inside, ORPHAN_GRACE_SECONDS - ORPHAN_BOUNDARY_MARGIN_SECONDS)
    _age_directory(outside, ORPHAN_GRACE_SECONDS + ORPHAN_BOUNDARY_MARGIN_SECONDS)
    record, _ = _embedded_retention(
        root, "healthy", {"EV_MAX_BYTES": "100000000", "EV_MAX_AGE_DAYS": "3650"}
    )
    # Exactly one of the two is condemned, and it is the older one.
    assert record["orphanDirs"] == 1, record
    assert record["orphanBytes"] == _tree_bytes(outside), record
    # Neither one's bytes are invisible, whichever side of the line it is on.
    assert record["bytesAfter"] == _store_bytes(root), record


def test_a_manifest_with_an_unrecognised_state_still_has_its_bytes_counted(tmp_path):
    """`_load_runs` deliberately leaves it alone and `_orphan_dirs` does not
    condemn it, so before this its bytes were counted NOWHERE."""
    root = tmp_path / "ev"
    now = int(time.time())
    _seed_run(root, "healthy", started=now - 60, size=100_000)
    strange = _seed_run(root, "future-state", started=now - 120, size=300_000)
    doc = json.loads((strange / "manifest.json").read_text())
    doc["state"] = "quiesced-by-a-later-binary"
    (strange / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")
    record, _ = _embedded_retention(
        root, "healthy", {"EV_MAX_BYTES": "100000000", "EV_MAX_AGE_DAYS": "3650"}
    )
    # Not an orphan and not a retained run — but its bytes are real.
    assert record["orphanDirs"] == 0, record
    assert record["retainedRuns"] == 1, record
    assert record["bytesAfter"] == _store_bytes(root), record
    assert record["bytesAfter"] >= _tree_bytes(strange) > 0, record
    assert (strange / "manifest.json").exists(), "never evicted on an unknown state"


def test_an_orphan_alone_prints_the_operator_notice(tmp_path):
    """A pass that degrades ONLY because of an orphan wrote the degraded
    record and printed nothing, beside this file's own rule that eviction is
    never silent."""
    root = tmp_path / "ev"
    now = int(time.time())
    _seed_run(root, "healthy", started=now - 60, size=100_000)
    orphan = _seed_run(root, "halfgone", started=now - 120, size=300_000)
    (orphan / "manifest.json").unlink()
    _age_directory(orphan, ORPHAN_GRACE_SECONDS * 10)
    record, result = _embedded_retention(
        root, "healthy", {"EV_MAX_BYTES": "100000000", "EV_MAX_AGE_DAYS": "3650"}
    )
    # Non-vacuity: nothing was evicted, so there is no gap and the notice can
    # only be firing for the orphan.
    assert record["gaps"] == [], record
    assert record["orphanDirs"] == 1, record
    assert record["coverage"] == "degraded", record
    assert "coverage is degraded" in result.stderr, result.stderr
    assert "1 orphan directory with no readable manifest" in result.stderr, (
        result.stderr)
    assert "coverage is degraded" in result.stdout, result.stdout


def test_the_record_carries_the_exact_eviction_bytes_and_reasons(tmp_path):
    root = tmp_path / "ev"
    now = int(time.time())
    sizes = {}
    for name in ("a", "b"):
        run = _seed_run(root, name, started=now - 40 * 86400, size=200_000)
        sizes[name] = _tree_bytes(run)
    _seed_run(root, "current", started=now, size=1000)
    record, _ = _embedded_retention(
        root, "current", {"EV_MAX_AGE_DAYS": "7", "EV_MAX_BYTES": "100000000"}
    )
    assert record["lastEvictedRuns"] == 2, record
    assert record["lastEvictionReasons"] == ["age"], record
    assert record["lastEvictedBytes"] == sizes["a"] + sizes["b"], record
    assert record["lastEvictionByReason"]["age"] == {
        "runs": 2, "bytes": sizes["a"] + sizes["b"]}, record


def test_a_pass_that_evicts_nothing_records_zero_rather_than_omitting_it(tmp_path):
    root = tmp_path / "ev"
    _seed_run(root, "current", started=int(time.time()), size=1000)
    record, _ = _embedded_retention(
        root, "current", {"EV_MAX_AGE_DAYS": "3650", "EV_MAX_BYTES": "100000000"}
    )
    assert record["lastEvictedRuns"] == 0
    assert record["lastEvictedBytes"] == 0
    assert record["lastEvictionByReason"] == {"age": {"runs": 0, "bytes": 0},
                                              "cap": {"runs": 0, "bytes": 0}}


def test_cap_eviction_is_reported_and_recorded(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    # Recent, so the age window cannot do the cap's work. Seeded at epoch 1
    # these were all age-evicted and the case passed while exercising nothing
    # it claimed to.
    now = int(time.time())
    for i in range(3):
        _seed_run(root, f"old{i}", started=now - 3600 * (3 - i))
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
            "CCTALLY_TEST_EVIDENCE_MAX_BYTES": "1000000",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "EVIDENCE EVICTED" in res.stderr, res.stderr
    survivors = {p.name for p in (root / "cctally-dev").iterdir()}
    assert len(survivors) < 3, survivors
    record = _retention(root)
    assert record["lastEvictionReasons"] == ["cap"], record
    assert record["coverage"] == "degraded", record
    assert record["gaps"], record
    assert stat.S_IMODE((root / ".retention.json").stat().st_mode) == 0o600


def test_the_producer_writes_keyed_gaps_and_can_emit_an_open_ended_one(tmp_path):
    """The gap shape the read surfaces must be written against (#630 S1, F2).

    Both `cctally-test-remote --status` and `--report` indexed gaps
    positionally and crashed with `KeyError: 0` on the live store, because the
    only fixtures covering them hand-built `[[from, to]]` — a shape
    `_merge_intervals` has never emitted. This pins the producer's real output
    so a fixture can no longer disagree with it silently, and it pins the
    open-ended case, which a naive key fix turns into a silently dropped gap.
    """
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    now = int(time.time())
    # Dated ahead of the current run, so evicting it leaves the store's newest
    # interval unbounded. That is the only way the producer emits `toEpoch`
    # null.
    _seed_run(root, "ahead", started=now + 86400)
    _seed_run(root, "behind", started=now - 3600)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
            "CCTALLY_TEST_EVIDENCE_MAX_BYTES": "1000",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    record = _retention(root)
    assert record["gaps"], record
    for gap in record["gaps"]:
        assert isinstance(gap, dict), record
        assert set(gap) == {"fromEpoch", "toEpoch"}, record
        assert isinstance(gap["fromEpoch"], int), record
        assert gap["toEpoch"] is None or isinstance(gap["toEpoch"], int), record
    assert any(gap["toEpoch"] is None for gap in record["gaps"]), record


def test_concurrent_runs_do_not_lose_each_other_s_retention_state(tmp_path):
    """Acceptance criterion 12 under the concurrency the store exists for.

    The root is machine-wide precisely so that several worktrees can run on
    one runner at once, and `.retention.json` was a lock-free
    read-modify-write over it. Three concurrent aggregators each printed
    EVIDENCE EVICTED and the record then said `passes: 1`: a pass whose gaps
    were overwritten by a pass that evicted nothing reports `complete` over a
    holed store, and the runs it removed can never be rediscovered.
    """
    workers = 4
    root = tmp_path / "ev"
    now = int(time.time())
    for i in range(12):
        _seed_run(root, f"old{i}", started=now - 60 * (12 - i))
    current_runs = [f"concurrent-{i}" for i in range(workers)]
    for i, run_id in enumerate(current_runs):
        _seed_run(root, run_id, started=now + i)

    # Exercise the aggregator's exact embedded retention bridge concurrently,
    # without wrapping every contender in another complete shell + pytest run.
    # The bridge owns the retention lock and state merge; unrelated nested-suite
    # startup made this proof collide with the outer xdist timeout under load.
    def retain(run_id):
        return _embedded_retention(
            root,
            run_id,
            {"EV_MAX_BYTES": "1000000", "EV_MAX_AGE_DAYS": "3650"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(retain, current_runs))

    record = _retention(root)
    # Every pass is counted exactly once. Without the lock the last writer
    # wins and the count collapses to the number of passes that happened to
    # serialize.
    assert record["passes"] == workers, record
    # And the coverage the store reports is the UNION over its whole life, so
    # a later pass that evicted nothing cannot report a holed store complete.
    assert record["coverage"] == "degraded", record
    assert record["gaps"], record
    assert stat.S_IMODE((root / ".retention.lock").stat().st_mode) == 0o600


def test_age_eviction_removes_runs_past_the_window(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    now = int(time.time())
    _seed_run(root, "ancient", started=now - 30 * 86400, size=10)
    _seed_run(root, "recent", started=now - 3600, size=10)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
            "CCTALLY_TEST_EVIDENCE_MAX_AGE_DAYS": "7",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (root / "cctally-dev" / "ancient").exists()
    assert (root / "cctally-dev" / "recent").exists()
    assert "EVIDENCE EVICTED" in res.stderr
    assert _retention(root)["lastEvictionReasons"] == ["age"], _retention(root)


def test_a_passing_run_is_evicted_before_a_failing_one(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    now = int(time.time())
    _seed_run(root, "pass-old", outcome="pass", started=now - 7200)
    _seed_run(root, "fail-old", outcome="fail", started=now - 3600)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
            "CCTALLY_TEST_EVIDENCE_MAX_BYTES": "700000",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (root / "cctally-dev" / "pass-old").exists()
    assert (root / "cctally-dev" / "fail-old").exists(), (
        "a failing run carries the evidence the store exists for"
    )
    assert _retention(root)["lastEvictionReasons"] == ["cap"], _retention(root)


def test_the_current_run_is_never_evicted_even_over_cap(tmp_path):
    """If the current run alone exceeds the cap, the cap is exceeded rather
    than the run truncated, and the next pass reclaims it."""
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
            "CCTALLY_TEST_EVIDENCE_MAX_BYTES": "1",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    runs = _run_dirs(root)
    assert len(runs) == 1, runs
    assert (runs[0] / "logs" / "alpha.log").exists()
    assert _retention(root)["overCap"] is True


def test_an_active_run_whose_process_is_gone_becomes_evictable(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    # A pid that cannot be live: recorded with a start identity nothing can
    # match, so the reconciliation has something to observe.
    _seed_run(
        root,
        "stranded",
        state="active",
        outcome="pass",
        started=int(time.time()) - 3600,
        pid=999999,
        pid_start="Sat Jan  1 00:00:00 2000",
    )
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
            "CCTALLY_TEST_EVIDENCE_MAX_BYTES": "1000",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (root / "cctally-dev" / "stranded").exists(), (
        "an unreconciled active run is protected from eviction for good"
    )


def test_degraded_coverage_survives_a_restart(tmp_path):
    """Acceptance criterion 12.

    `plan_evidence_evictions` reports the gaps of ONE pass. The second run
    evicts nothing, so a surface that rendered a single pass's intervals would
    present a store full of holes as complete. The record therefore persists
    the intervals and reports their union.
    """
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    now = int(time.time())
    for i in range(3):
        _seed_run(root, f"old{i}", started=now - 3600 * (3 - i))
    env = {
        "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
        "CCTALLY_TEST_EVIDENCE_MAX_BYTES": "1000000",
    }
    first = _drive(est, tmp_path, env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "EVIDENCE EVICTED" in first.stderr
    after_first = _retention(root)
    assert after_first["gaps"], after_first

    # Room to spare on the second pass, so it evicts nothing at all.
    second = _drive(
        est, tmp_path, dict(env, CCTALLY_TEST_EVIDENCE_MAX_BYTES="100000000")
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "EVIDENCE EVICTED" not in second.stderr, second.stderr
    after_second = _retention(root)
    # Non-vacuity: the second pass really ran and really recorded itself, so
    # the surviving gaps are the union rather than an untouched file.
    assert after_second["updatedAt"] >= after_first["updatedAt"]
    assert after_second["passes"] == after_first["passes"] + 1
    assert after_second["coverage"] == "degraded", after_second
    assert after_second["gaps"] == after_first["gaps"], (
        after_first["gaps"], after_second["gaps"],
    )
    assert "degraded" in second.stderr, second.stderr


@pytest.mark.skipif(not VOCABULARY_AVAILABLE, reason="maintainer-local producer")
def test_the_extract_states_the_degraded_coverage(tmp_path):
    """A CI artifact consumer sees the gap too, in the file they are handed."""
    est, _canary = _canary_estate(tmp_path)
    root = tmp_path / "ev"
    now = int(time.time())
    for i in range(3):
        _seed_run(root, f"old{i}", started=now - 3600 * (3 - i))
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
            "CCTALLY_TEST_EVIDENCE_MAX_BYTES": "1000000",
        },
    )
    assert res.returncode == 1, res.stdout + res.stderr
    head = (
        _run_dirs(root)[-1] / "export" / "failure-context.txt"
    ).read_text().splitlines()
    coverage = [line for line in head[:5] if "degraded" in line]
    assert coverage, head[:5]


def test_a_run_with_no_evidence_root_writes_no_retention_record(tmp_path):
    est = _estate(tmp_path)
    res = _drive(est, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert not list(tmp_path.glob("**/.retention.json"))


# ------------------------------------------------------ the workflow contract
#
# Sanitization and retention are ONE change, and this is what keeps them one.
# The rule is stated over every job that sets an evidence root, not over a list
# of three job names: a fourth lane that turned sanitization on tomorrow
# without an upload would be caught, where a hardcoded list would not notice it.

WORKFLOW_DIR = REPO / ".github" / "workflows"


def _parse_workflow(path):
    """Load one workflow with the real parser provisioned in every test lane."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        return {}
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        return {}
    out = {}
    for name, body in jobs.items():
        if not isinstance(body, dict):
            continue
        steps = body.get("steps")
        out[name] = (body, steps if isinstance(steps, list) else [])
    return out


def _all_workflows():
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


@pytest.mark.parametrize(
    "body",
    [
        "jobs:\n  test:\n    steps:\n      - name: one\n       run: echo bad\n",
        "jobs:\n\ttest:\n    steps: []\n",
    ],
)
def test_workflow_parser_refuses_yaml_that_github_cannot_compile(tmp_path, body):
    path = tmp_path / "invalid.yml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        _parse_workflow(path)


def _unterminated_heredocs(script):
    """Return shell heredoc delimiters whose terminating line is absent."""
    pending = []
    for line in str(script).splitlines():
        if pending:
            delimiter, strip_tabs = pending[0]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == delimiter:
                pending.pop(0)
            continue
        if line.lstrip().startswith("#"):
            continue
        for match in re.finditer(
            r"<<(?P<dash>-?)(?:'(?P<single>[^']+)'|\"(?P<double>[^\"]+)\"|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))",
            line,
        ):
            pending.append(
                (
                    match.group("single") or match.group("double") or match.group("plain"),
                    bool(match.group("dash")),
                )
            )
    return [delimiter for delimiter, _strip_tabs in pending]


def test_workflow_run_blocks_terminate_every_heredoc():
    seen = 0
    for path in _all_workflows():
        for name, (_body, steps) in _parse_workflow(path).items():
            for index, step in enumerate(steps):
                if not isinstance(step, dict) or "run" not in step:
                    continue
                seen += 1
                assert not _unterminated_heredocs(step["run"]), (
                    f"{path.name}:{name} step {index} leaves shell heredocs "
                    f"unterminated: {_unterminated_heredocs(step['run'])}"
                )
    assert seen > 15, f"only {seen} workflow run blocks were checked"


def test_heredoc_gate_detects_a_terminator_merged_into_the_next_command():
    assert _unterminated_heredocs("python3 - <<'PY'\nprint('ok')\nPY  echo next\n") == [
        "PY"
    ]


_PIN_FILE_REL = "tests/requirements-dev.txt"


def _closure_pins_pyyaml() -> bool:
    """Whether the canonical closure file actually pins PyYAML."""
    text = (REPO / _PIN_FILE_REL).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and line.split("==", 1)[0].strip().lower() == "pyyaml":
            return True
    return False


def test_every_authoritative_ci_lane_installs_the_real_yaml_parser():
    """Each authoritative lane must provision PyYAML, by whichever route.

    The lanes used to name it inline. #529 S6 replaced every inline list with an
    install of the ONE canonical closure, so the literal no longer appears in a
    lane at all. Accepting the `-r` on its own would weaken this to "the lane
    installs a file", which a file that had dropped PyYAML would still satisfy —
    so the closure is read here and required to pin it. Either route is
    accepted; neither route present is the failure this test exists for, because
    without a real parser malformed workflow YAML passes as an unvalidated skip.
    """
    via_closure_available = _closure_pins_pyyaml()
    jobs = _evidence_jobs()
    for label in ("ci.yml:test-macos", "ci.yml:test-pr", "ci-linux-matrix.yml:test-linux"):
        _body, steps, _text = jobs[label]
        installs = [
            str(step.get("run", ""))
            for step in steps
            if isinstance(step, dict) and "pip install" in str(step.get("run", ""))
        ]
        assert installs, f"{label} runs no pip install at all"
        inline = any("PyYAML" in command for command in installs)
        via_closure = via_closure_available and any(
            _PIN_FILE_REL in command for command in installs
        )
        assert inline or via_closure, (
            f"{label} must install PyYAML so workflow validation cannot skip: it "
            f"names no inline PyYAML, and no install of {_PIN_FILE_REL} that "
            f"pins it (closure pins PyYAML: {via_closure_available})"
        )


def _job_text(path, name):
    """The raw text of one job, for the few genuinely lexical assertions."""
    text = path.read_text(encoding="utf-8")
    if "\njobs:\n" not in text:
        return ""
    body = text.split("\njobs:\n", 1)[1]
    marks = list(re.finditer(r"^  ([A-Za-z0-9_.-]+):$", body, re.M))
    for index, mark in enumerate(marks):
        if mark.group(1) != name:
            continue
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        return body[mark.start():end]
    return ""


def _evidence_jobs():
    """`{label: (job mapping, steps, job text)}` for every evidence-enabled job."""
    found = {}
    for path in _all_workflows():
        for name, (body, steps) in _parse_workflow(path).items():
            text = _job_text(path, name)
            if "CCTALLY_TEST_EVIDENCE_ROOT" in text:
                found[f"{path.name}:{name}"] = (body, steps, text)
    return found


def _upload_steps(steps):
    """Every well-formed upload step that names the evidence tree.

    Scoped to the evidence tree rather than to every upload, because a lane may
    legitimately upload unrelated artifacts (the PR lane uploads a Playwright
    report). Scoping this way still catches the defect the rule exists for: a
    step that uploaded `logs/` would name a path under the evidence root and
    would therefore be inside this set.

    A step carrying a `run:` is excluded even when it also carries a `uses:`,
    because such a step is not an upload step — GitHub's step schema forbids
    the pair, so the workflow does not compile and nothing in it runs.
    """
    out = []
    for step in steps:
        if not isinstance(step, dict) or "run" in step:
            continue
        if not str(step.get("uses", "")).startswith("actions/upload-artifact"):
            continue
        if "cctally-test-evidence" not in str(step.get("with", {}).get("path", "")):
            continue
        out.append(step)
    return out


def _run_id_value(body, steps):
    """The explicit run identifier a job declares, wherever it declares it.

    Read from the parsed `env` mappings rather than by grepping the job text.
    A text scan returns the FIRST line mentioning the name, which is a COMMENT
    the moment anyone documents the variable above it — and a comment cannot
    carry the matrix expression the rule is about.
    """
    for scope in [body] + [s for s in steps if isinstance(s, dict)]:
        env = scope.get("env")
        if isinstance(env, dict) and env.get("CCTALLY_TEST_RUN_ID"):
            return str(env["CCTALLY_TEST_RUN_ID"])
    return ""


def _upload_paths(step):
    block = str(step.get("with", {}).get("path", ""))
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_no_workflow_step_declares_both_run_and_uses():
    """A step may script or invoke an action, never both.

    Non-vacuous by construction: the parser is proven to see real steps by the
    estate-wide count asserted here, and this exact assertion goes red on the
    merged-terminator defect, which is how that defect is now detected rather
    than reported healthy.
    """
    seen = 0
    for path in _all_workflows():
        for name, (_body, steps) in _parse_workflow(path).items():
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                seen += 1
                assert not ("run" in step and "uses" in step), (
                    f"{path.name}:{name} step {index} "
                    f"({step.get('name', step.get('uses'))!r}) declares both "
                    f"`run` and `uses`. GitHub's step schema forbids the pair, "
                    f"so the workflow does not compile and NO job in the file "
                    f"runs. The usual cause is a heredoc terminator sharing a "
                    f"line with the next step's header inside a `run: |` block."
                )
    assert seen > 40, f"the workflow parser found only {seen} steps; it broke"


def test_job_level_env_does_not_use_the_runner_context():
    """Runner-derived paths are step-scoped, after a runner exists.

    GitHub evaluates ``jobs.<job_id>.env`` before it assigns a runner, so the
    ``runner`` context is unavailable there and the whole workflow is rejected
    before it creates a single job.  Step-level ``env`` mappings may use the
    context and are deliberately outside this assertion.
    """
    violations = []
    seen_jobs = 0
    for path in _all_workflows():
        for name, (body, _steps) in _parse_workflow(path).items():
            seen_jobs += 1
            env = body.get("env")
            if not isinstance(env, dict):
                continue
            for key, value in env.items():
                if "${{ runner." in str(value):
                    violations.append(f"{path.name}:{name}:env.{key}")

    assert seen_jobs >= 5, (
        f"the workflow parser found only {seen_jobs} jobs; it broke"
    )
    assert not violations, (
        "GitHub does not expose the `runner` context in `jobs.<job_id>.env`; "
        "move these values to the consuming step's `env`: "
        + ", ".join(violations)
    )


def test_every_evidence_enabled_job_uploads_its_sanitized_extract():
    jobs = _evidence_jobs()
    # Non-vacuity: the three aggregator jobs are known to set a root, so an
    # empty or shrunken set means the discovery broke, not that the estate is
    # clean.
    assert set(jobs) >= {
        "ci.yml:test-macos",
        "ci.yml:test-pr",
        "ci-linux-matrix.yml:test-linux",
    }, sorted(jobs)
    for label, (body, steps, _text) in sorted(jobs.items()):
        assert _run_id_value(body, steps), (
            f"{label} turns evidence on without stating a run identifier; "
            f"GITHUB_RUN_ID plus the attempt and job name do not distinguish a "
            f"matrix leg, so two legs would share one evidence directory."
        )
        uploads = _upload_steps(steps)
        assert uploads, (
            f"{label} sets CCTALLY_TEST_EVIDENCE_ROOT, which also turns console "
            f"sanitization on, but uploads nothing. $RUNNER_TEMP is deleted "
            f"when the job ends, so the sanitized console would be the only "
            f"surviving copy of what it redacted — which is worse than not "
            f"sanitizing at all. Add the failure-only upload in the same change."
        )


def test_every_evidence_upload_is_failure_only_and_names_only_the_export_files():
    for label, (_body, steps, _text) in sorted(_evidence_jobs().items()):
        for step in _upload_steps(steps):
            assert step.get("if") == "failure() || cancelled()", (
                f"{label}: the extract upload must be failure-only and must "
                f"also run on cancellation, so a timed-out job still surfaces "
                f"what it had. Its own condition reads {step.get('if')!r}."
            )
            entries = _upload_paths(step)
            assert entries, f"{label}: the upload step names no path at all"
            for entry in entries:
                assert entry.endswith(
                    ("export/failure-context.txt", "export/outcome.json")
                ), (
                    f"{label}: uploads {entry!r}. Only the two export files may "
                    f"leave the runner. `export/` is a sibling of `logs/` and "
                    f"never a parent precisely so a careless recursive upload "
                    f"cannot capture a raw log."
                )
                assert "logs" not in entry, f"{label}: uploads a log path: {entry!r}"


def test_matrix_evidence_identity_carries_the_matrix_leg():
    for label, (body, steps, _text) in sorted(_evidence_jobs().items()):
        if not isinstance(body.get("strategy"), dict) or "matrix" not in body["strategy"]:
            continue
        run_id = _run_id_value(body, steps)
        assert run_id and "matrix." in run_id, (
            f"{label} is a matrix job whose run identifier does not carry the "
            f"matrix leg, so all legs would share one evidence directory and "
            f"the second would be refused. It reads {run_id!r}."
        )
        for step in _upload_steps(steps):
            name = str(step.get("with", {}).get("name", ""))
            assert name.startswith("failure-context") and "matrix." in name, (
                f"{label}: the artifact name must carry the matrix leg, or the "
                f"legs collide on one artifact. It reads {name!r}."
            )


def test_every_workflow_loads_as_a_yaml_mapping_with_jobs():
    """The real parser is a fail-closed gate, not optional corroboration."""
    for path in _all_workflows():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict), path.name
        assert isinstance(loaded.get("jobs"), dict), path.name


# ------------------------------------------------ per-test durations (#630 S1, F3)

DURATIONS_ARTIFACT = ("timings", "pytest-tests.jsonl.gz")


def _load_durations_plugin():
    path = REPO / "tests" / "_pytest_durations_plugin.py"
    spec = importlib.util.spec_from_file_location("_durations_plugin", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_jsonl_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class _FakeConfig:
    """Only what `pytest_configure` reaches for."""

    def __init__(self, workerinput=None):
        if workerinput is not None:
            self.workerinput = workerinput
        self.registered = []
        self.pluginmanager = self

    def register(self, plugin, name=None):
        self.registered.append((plugin, name))


def _authoritative_env(root, extra=None):
    env = {
        "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
        "CCTALLY_AUTHORITATIVE_RUN": "1",
    }
    env.update(extra or {})
    return env


def _durations_path(root):
    return _run_dirs(root)[0].joinpath(*DURATIONS_ARTIFACT)


def test_authoritative_run_writes_a_durations_artifact(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, _authoritative_env(root))
    assert res.returncode == 0, res.stdout + res.stderr
    path = _durations_path(root)
    assert path.exists(), sorted(p.name for p in path.parent.iterdir())
    records = _read_jsonl_gz(path)
    assert any(r.get("phase") == "call" for r in records), records


def test_non_authoritative_run_writes_no_durations_artifact(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(root)})
    assert res.returncode == 0, res.stdout + res.stderr
    assert not _durations_path(root).exists()


def test_every_phase_is_recorded_for_a_known_test(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, _authoritative_env(root))
    assert res.returncode == 0, res.stdout + res.stderr
    records = _read_jsonl_gz(_durations_path(root))
    phases = {
        r["phase"] for r in records
        if r.get("nodeId", "").endswith("::test_known")
    }
    # Dropping setup and teardown would remove exactly the attribution a
    # retirement or a tier decision needs.
    assert phases == {"setup", "call", "teardown"}, records


def test_both_legs_survive_the_merge(tmp_path):
    est = _estate(tmp_path)
    # A serial benchmark target makes the aggregator run its SECOND pytest
    # process; the two are separate processes, so one shared output path would
    # let the second overwrite the first.
    (est / "tests" / "test_rebuild_benchmark.py").write_text(
        "def test_bench():\n    assert True\n", encoding="utf-8"
    )
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, _authoritative_env(root))
    assert res.returncode == 0, res.stdout + res.stderr
    records = _read_jsonl_gz(_durations_path(root))
    assert {r["leg"] for r in records if "leg" in r} >= {"pytest", "benchmark"}


def test_merged_artifact_is_deterministically_ordered(tmp_path):
    est = _estate(tmp_path)
    (est / "tests" / "test_rebuild_benchmark.py").write_text(
        "def test_bench():\n    assert True\n", encoding="utf-8"
    )
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, _authoritative_env(root))
    assert res.returncode == 0, res.stdout + res.stderr
    records = [r for r in _read_jsonl_gz(_durations_path(root)) if "nodeId" in r]
    # Non-vacuity: an empty list is trivially sorted, so the ordering claim
    # would hold over an artifact the plugin never wrote into.
    assert len(records) > 1, records
    keys = [(r["leg"], r["nodeId"], r["phase"]) for r in records]
    assert keys == sorted(keys), keys


def test_the_merged_artifact_states_its_own_completeness(tmp_path):
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, _authoritative_env(root))
    assert res.returncode == 0, res.stdout + res.stderr
    footers = [r for r in _read_jsonl_gz(_durations_path(root)) if r.get("footer")]
    assert len(footers) == 1, footers
    assert footers[0]["complete"] is True, footers


def test_an_incomplete_durations_artifact_is_reported_on_the_run(tmp_path):
    # The completeness state used to exist ONLY inside the artifact. The merge
    # was invoked with `>/dev/null` and its note fired on a non-zero exit,
    # while an incomplete merge exits 0 — publishing a partial artifact is the
    # correct outcome — so nothing in the manifest, the outcome record, the
    # extract or the operator's console ever said so.
    #
    # The estate raises inside `pytest_collection_modifyitems`, which
    # `wrap_session` turns into ExitCode.INTERNAL_ERROR while still calling
    # `pytest_sessionfinish` from its `finally` block. That is the exact path
    # the plugin's comment used to deny, so this drives it end to end.
    est = _estate(tmp_path)
    (est / "tests" / "conftest.py").write_text(
        "def pytest_collection_modifyitems(config, items):\n"
        "    raise RuntimeError('simulated internal error')\n",
        encoding="utf-8",
    )
    root = tmp_path / "ev"
    res = _drive(est, tmp_path, _authoritative_env(root))
    assert res.returncode != 0, res.stdout + res.stderr
    assert "durations artifact for run" in res.stderr, res.stderr
    assert "INCOMPLETE" in res.stderr, res.stderr
    records = _read_jsonl_gz(_durations_path(root))
    footer = [r for r in records if r.get("footer")][0]
    assert footer["complete"] is False, footer
    # The mechanism, not just the outcome: the hook DID run on the internal
    # error path, so the footer exists and only its exit status separates it
    # from an ordinary run.
    legs = {leg["leg"]: leg for leg in footer["legs"]}
    main_leg = legs.get("pytest") or next(iter(legs.values()))
    assert main_leg["sessionFinished"] is True, footer
    assert main_leg["exitStatus"] not in (0, 1, 5), footer


def test_a_worker_never_opens_the_output(tmp_path):
    # xdist propagates `-p` to every worker, so without this guard each worker
    # would open the controller's output path and interleave into it.
    plugin = _load_durations_plugin()
    out = tmp_path / "durations.jsonl.gz"
    os.environ["CCTALLY_DURATIONS_PATH"] = str(out)
    try:
        worker = _FakeConfig(workerinput={"workerid": "gw0"})
        assert plugin._writer_is_disabled(worker)
        plugin.pytest_configure(worker)
        assert worker.registered == []
        assert not out.exists()
        # Non-vacuity: the controller, with the same environment, DOES open it.
        controller = _FakeConfig()
        assert not plugin._writer_is_disabled(controller)
        plugin.pytest_configure(controller)
        assert len(controller.registered) == 1
        controller.registered[0][0].pytest_sessionfinish(None, 0)
        assert out.exists()
    finally:
        os.environ.pop("CCTALLY_DURATIONS_PATH", None)


def test_the_writer_is_disabled_without_an_output_path():
    plugin = _load_durations_plugin()
    # Restored in a `finally`: an in-process environment mutation with no
    # restore leaks for the whole xdist worker, which has contaminated a run
    # in this repository before.
    previous = os.environ.pop("CCTALLY_DURATIONS_PATH", None)
    try:
        assert plugin._writer_is_disabled(_FakeConfig())
    finally:
        if previous is not None:
            os.environ["CCTALLY_DURATIONS_PATH"] = previous


@pytest.mark.parametrize("status", [0, 1, 2, 3, 4, 5])
def test_the_footer_records_the_real_exit_status(tmp_path, status):
    # `pytest_sessionfinish` used to write `"sessionFinished": True`
    # unconditionally and discard the `exitstatus` it was handed, so a session
    # that ended in an INTERNALERROR published `complete: true` over a
    # truncated population. `_pytest.main.wrap_session` calls this hook from
    # its `finally` block on every path where `initstate >= 2`, so the hook
    # DOES run on those paths and the status is the only fact that separates
    # them.
    plugin = _load_durations_plugin()
    out = tmp_path / "durations.jsonl.gz"
    previous = os.environ.get("CCTALLY_DURATIONS_PATH")
    os.environ["CCTALLY_DURATIONS_PATH"] = str(out)
    try:
        config = _FakeConfig()
        plugin.pytest_configure(config)
        config.registered[0][0].pytest_sessionfinish(None, status)
    finally:
        if previous is None:
            os.environ.pop("CCTALLY_DURATIONS_PATH", None)
        else:
            os.environ["CCTALLY_DURATIONS_PATH"] = previous
    footer = _read_jsonl_gz(out)[-1]
    assert footer["footer"] is True, footer
    assert footer["exitStatus"] == status, footer



def test_a_forced_control_arm_records_itself_as_its_own_mode(tmp_path):
    """The measurement programme's only per-run proof of treatment condition.

    A control arm recorded as `fallback` would be indistinguishable from a run
    whose scheduler could not read its table, and a control arm recorded as
    `lpt` would be indistinguishable from the treatment arm. Either confusion
    invalidates the pair it belongs to, so the forced arm carries its own value
    and still names the same table and digest as the treatment arm.
    """
    import hashlib

    est = _estate(tmp_path)
    shutil.copy2(
        BIN / "_lib_harness_durations.py", est / "bin" / "_lib_harness_durations.py"
    )
    table = est / "tests" / "authoritative-harness-durations.tsv"
    table.write_text("alpha\t7\nreconcile\t3\n", encoding="utf-8")
    root = tmp_path / "ev"
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
            "CCTALLY_TEST_ALL_DISPATCH": "report",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    record = _scheduler_record(root)
    assert record["mode"] == "report-forced", record
    # The provenance must be identical to the treatment arm's, or the pair
    # differs in more than the one variable under test.
    assert record["table"] == "tests/authoritative-harness-durations.tsv", record
    assert record["table_digest"] == (
        "sha256:" + hashlib.sha256(table.read_bytes()).hexdigest()
    ), record
