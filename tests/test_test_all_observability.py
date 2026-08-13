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

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
RUNNER = BIN / "cctally-test-all"
CONTRACT_LIB = BIN / "_lib-test-contract.sh"
EVIDENCE_KERNEL = BIN / "_lib_test_evidence.py"

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

    if smoke:
        (testsdir / "test_scratch_smoke.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
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
    heartbeats = [
        line for line in res.stderr.splitlines()
        if line.startswith("[cctally-test-all] ") and line.split()[1].endswith("s")
    ]
    assert heartbeats, res.stderr
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
    (est / "bin" / "cctally-slow-test").write_text(
        "#!/usr/bin/env bash\nsleep 4\nprintf '%s\\n' 'passed: 1   failed: 0'\n",
        encoding="utf-8",
    )
    (est / "bin" / "cctally-slow-test").chmod(0o755)
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
    est = _estate(tmp_path)
    res = _drive(
        est,
        tmp_path,
        {
            "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
            "CCTALLY_PROGRESS_INTERVAL": "1",
            "CCTALLY_AUTHORITATIVE_RUN": "1",
        },
    )
    # Every phase now emits one immediate pulse. The estate finishes well
    # inside thirty seconds, so honouring the one-second seam would produce
    # additional cadence ticks; pinning the authoritative cadence produces
    # only the transition pulse.
    heartbeats = [
        line for line in res.stderr.splitlines()
        if line.startswith("[cctally-test-all] ") and line.split()[1].endswith("s")
    ]
    assert len(heartbeats) == 2, heartbeats
    assert "queued" in heartbeats[0], heartbeats
    assert heartbeats[1].endswith("pytest running"), heartbeats
    assert _completions(res.stderr), res.stderr


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
    try:
        assert _wait_for(lambda: list(root.rglob("logs/alpha.started"))), (
            "the pool never started"
        )
        proc.send_signal(getattr(_signal, f"SIG{signal_name}"))
        rc = proc.wait(timeout=90)
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
        timeout=30,
    ), _descendants_mentioning(str(est / "bin" / "cctally-alpha-test"))


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
    est = _estate(tmp_path)
    root = tmp_path / "ev"
    now = int(time.time())
    for i in range(12):
        _seed_run(root, f"old{i}", started=now - 60 * (12 - i))
    procs = [
        subprocess.Popen(
            [str(est / "bin" / "cctally-test-all")],
            env=_env(
                tmp_path,
                {
                    "CCTALLY_TEST_EVIDENCE_ROOT": str(root),
                    "CCTALLY_TEST_RUN_ID": f"concurrent-{i}",
                    "CCTALLY_TEST_EVIDENCE_MAX_BYTES": "1000000",
                },
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(workers)
    ]
    results = [(p.wait(timeout=110), p.communicate()) for p in procs]
    for rc, (out, err) in results:
        assert rc == 0, out + err

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
