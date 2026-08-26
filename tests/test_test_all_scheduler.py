"""Assertions for bin/cctally-test-all's worker model and phase isolation.

Most tests invoke the runner's side-effect-free CCTALLY_TEST_ALL_PLAN=1 dry-run
(which exits before any harness/pytest launch) with
CCTALLY_TEST_ALL_FAKE_NCPU pinning the core count. The execution test uses a
synthetic checkout and fake Python boundary to observe pytest phase isolation.

WARNING: calls through ``_plan`` must keep plan mode enabled. The one execution
test builds a synthetic checkout whose harnesses and Python boundary are fakes,
so it cannot recurse into the real suite. Short timeouts backstop both paths.
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
RUNNER = BIN / "cctally-test-all"
CONTRACT_LIB = BIN / "_lib-test-contract.sh"
# .mirror-allowlist excludes itself from the mirror, so its presence is exactly the
# marker bin/cctally-test-all uses to require the maintainer-local test-remote harness.
PRIVATE_TREE = (REPO / ".mirror-allowlist").exists()


def _plan(env_overrides, fake_ncpu="16", runner=RUNNER, args=()):
    env = {
        "PATH": __import__("os").environ["PATH"],
        "CCTALLY_TEST_ALL_PLAN": "1",
        "CCTALLY_TEST_ALL_FAKE_NCPU": fake_ncpu,
    }
    env.update(env_overrides)
    proc = subprocess.run(
        [str(runner), *args], env=env, capture_output=True, text=True, timeout=30
    )
    return proc


def _kv(stdout):
    out = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_unset_autotunes_outer_below_ncpu_16():
    p = _plan({}, fake_ncpu="16")
    assert p.returncode == 0, p.stderr
    kv = _kv(p.stdout)
    assert kv["ncpu"] == "16"
    assert kv["outer"] == "7"     # round(16*0.45)
    assert kv["inner"] == "4"     # min(4, outer)
    assert kv["pytest"] == "16"   # solo pytest keeps the full machine
    assert kv["reconcile_in_pool"] == "1"


def test_unset_autotune_ncpu_10():
    kv = _kv(_plan({}, fake_ncpu="10").stdout)
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("5", "4", "10")


def test_explicit_budget_4_preserves_today():
    kv = _kv(_plan({"CCTALLY_TEST_JOBS": "4"}, fake_ncpu="16").stdout)
    # Explicit budget = outer = pytest (today's meaning); inner capped at 4.
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("4", "4", "4")


def test_explicit_budget_2_preserves_today():
    kv = _kv(_plan({"CCTALLY_TEST_JOBS": "2"}, fake_ncpu="16").stdout)
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("2", "2", "2")


def test_serial_budget_1_is_fully_serial():
    kv = _kv(_plan({"CCTALLY_TEST_JOBS": "1"}, fake_ncpu="16").stdout)
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("1", "1", "1")


def test_explicit_role_overrides():
    kv = _kv(
        _plan(
            {
                "CCTALLY_OUTER_JOBS": "9",
                "CCTALLY_INNER_JOBS": "3",
                "CCTALLY_PYTEST_JOBS": "6",
            },
            fake_ncpu="16",
        ).stdout
    )
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("9", "3", "6")


def test_outer_only_override_keeps_full_machine_for_solo_pytest():
    kv = _kv(_plan({"CCTALLY_OUTER_JOBS": "2"}, fake_ncpu="4").stdout)
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("2", "2", "4")


def test_inner_override_independent_of_outer_default():
    kv = _kv(_plan({"CCTALLY_INNER_JOBS": "2"}, fake_ncpu="16").stdout)
    assert kv["outer"] == "7" and kv["inner"] == "2"


def test_rejects_zero():
    p = _plan({"CCTALLY_TEST_JOBS": "0"}, fake_ncpu="16")
    assert p.returncode == 2


def test_rejects_non_numeric():
    p = _plan({"CCTALLY_OUTER_JOBS": "abc"}, fake_ncpu="16")
    assert p.returncode == 2


@pytest.mark.skipif(
    not PRIVATE_TREE,
    reason="test-remote is maintainer-local; the public mirror ships no such harness",
)
def test_plan_explicitly_includes_test_remote_harness():
    p = _plan({})
    assert p.returncode == 0, p.stderr
    assert "test-remote" in _kv(p.stdout)["harnesses"].split()


@pytest.mark.skipif(
    not PRIVATE_TREE,
    reason="the Linux profile's one omitted harness is private-only",
)
def test_linux_matrix_plan_excludes_only_the_mac_remote_harness():
    full = _kv(_plan({}).stdout)
    linux = _kv(_plan({"CCTALLY_LINUX_MATRIX_RUN": "1"}).stdout)
    assert linux["mode"] == "linux-matrix"
    assert set(full["harnesses"].split()) - set(linux["harnesses"].split()) == {
        "test-remote"
    }
    assert linux["pytest_disposition"] == "full"


def _copy_python_kernels(bindir, *, validator_body=None):
    """The aggregator's python dependencies, and only those.

    `_lib_test_*.py` is matched as a CLASS, because the evidence bridge loads
    whichever of those kernels a tree carries and naming them would break the
    moment it grows another. The duration-table validator is named, because it
    is exactly one file. This was briefly the wider `_lib_*.py`, which copied
    every kernel in the repository — 95 files and 3.2 MB into each synthetic
    tree instead of 3 files and 248 KB — for one module it actually needed.

    ``validator_body`` writes that one file's text instead of copying it, which
    is how the scheduler's ImportError branch is reached. DELETING the file
    does not reach it, and that is not an oversight: under pytest,
    tests/isolation_bootstrap/sitecustomize.py appends the REAL repository's
    bin/ to every child process's sys.path, so a subprocess imports
    `_lib_harness_durations` successfully no matter what the tree under test
    carries. A module whose body raises is immune to that, because the
    scheduler's own `sys.path.insert(0, <root>/bin)` puts this file ahead of
    the appended directory.
    """
    for kernel in sorted(BIN.glob("_lib_test_*.py")):
        shutil.copy2(kernel, bindir / kernel.name)
    target = bindir / "_lib_harness_durations.py"
    if validator_body is None:
        shutil.copy2(BIN / "_lib_harness_durations.py", target)
    else:
        target.write_text(validator_body, encoding="utf-8")


def _expected_names(private, extra=()):
    """Manifest rows for the synthetic checkout. test-remote is declared in the
    private profile whether or not the file is present, because its absence is
    itself one of the conditions under test."""
    names = [
        ("codex-quota", "public"),
        ("source-aware", "public"),
        *extra,
        # `reconcile` is the summary-ordering device bin/cctally-test-all pins
        # as the last report row. The synthetic estate carries it so a duration
        # table over this tree can be an exact cover of the manifest, which is
        # what the scheduler validates before it sorts.
        ("reconcile", "public"),
    ]
    if private:
        names.append(("test-remote", "private"))
    return names


def _tree(
    tmp_path,
    *,
    private,
    test_remote_mode,
    durations=None,
    validator_body=None,
    extra_harnesses=(),
    extra_manifest_harnesses=(),
    harness_bodies=None,
    ownership=False,
    git=False,
):
    """A synthetic checkout. ``private`` writes .mirror-allowlist, which is
    itself unmirrored and so marks the private tree; ``test_remote_mode`` is
    None to omit the maintainer-local harness entirely (the public shape).
    ``durations`` writes tests/authoritative-harness-durations.tsv from an
    iterable of (name, seconds) pairs; None omits the file entirely, which is
    itself one of the conditions under test. ``validator_body`` replaces
    bin/_lib_harness_durations.py with the given text, which is how the
    scheduler's ImportError branch is reached (see _copy_python_kernels).
    ``extra_harnesses`` puts harness FILES on disk that the manifest and the
    table do not declare, which is how a discovered harness can be absent from
    the dispatch order."""
    repo = tmp_path / "repo"
    bindir = repo / "bin"
    bindir.mkdir(parents=True)
    runner = bindir / "cctally-test-all"
    shutil.copy2(RUNNER, runner)
    # The aggregator sources the contract library and reads the estate manifest
    # at admission (#529 S1), so a synthetic checkout must carry both or every
    # non-plan-mode case in this module fails on the fixture rather than on the
    # property it asserts.
    # Matched as a CLASS: bin/_lib-test-contract.sh sources bin/_lib-fts5-probe.sh
    # and refuses without it (#529 S6, exception X2), so an estate that copied
    # one library by name refuses to start the moment the contract grows a
    # second. The failure is silent-looking rather than obvious — the estate
    # aborts with the contract's own diagnostic, and every assertion about what
    # the aggregator SHOULD have said then reads as a behaviour change.
    for lib in sorted(BIN.glob("_lib-*.sh")):
        shutil.copy2(lib, bindir / lib.name)
    # bin/cctally-test-all imports the evidence kernels (#529 S2) and, since
    # #630 S3, the duration-table validator. See _copy_python_kernels.
    _copy_python_kernels(bindir, validator_body=validator_body)

    harness_bodies = harness_bodies or {}
    manifest_extra = [n for n, _ in extra_manifest_harnesses]
    for name in (
        "codex-quota", "source-aware", "reconcile",
        *manifest_extra, *extra_harnesses,
    ):
        harness = bindir / f"cctally-{name}-test"
        harness.write_text(
            harness_bodies.get(name, "#!/usr/bin/env bash\nexit 0\n")
        )
        harness.chmod(0o755)

    if private:
        # The allowlist has to agree with the declared visibilities below, or
        # admission reports visibility-drift instead of the property under
        # test: every harness is public except the maintainer-local
        # test-remote one.
        (repo / ".mirror-allowlist").write_text(
            "bin/cctally-test-all\nbin/_lib-*\ntests/**\nbin/cctally-*\n"
            "!bin/cctally-test-remote-test\n"
        )
        # A private tree with no classifier is now an admission refusal in its
        # own right (visibility-classifier-unavailable), so the scratch tree
        # must carry the classifier exactly as tests/test_authoritative_test_
        # contract.py::_estate does. The classifier is maintainer-local, so the
        # public mirror's own suite skips instead of failing on a file it never
        # received.
        if not (REPO / ".githooks").exists() or not (
            REPO / ".githooks" / "_match.py"
        ).exists():
            pytest.skip(
                "the allowlist classifier .githooks/_match.py is maintainer-local"
            )
        githooks = repo / ".githooks"
        githooks.mkdir(exist_ok=True)
        shutil.copy2(REPO / ".githooks" / "_match.py", githooks / "_match.py")
    if test_remote_mode is not None:
        test_remote = bindir / "cctally-test-remote-test"
        test_remote.write_text("#!/usr/bin/env bash\nexit 0\n")
        test_remote.chmod(test_remote_mode)

    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "authoritative-test-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "minHarnessRows": 0,
                "harnesses": [
                    {
                        "name": n,
                        "visibility": v,
                        "minCases": 0,
                        "countPolicy": "fixed",
                    }
                    for n, v in _expected_names(private, extra_manifest_harnesses)
                ],
                "capabilities": [],
                "forbiddenRegeneration": [],
            },
            indent=2,
        )
        + "\n"
    )

    # #630 S7. A real estate carries a committed runtime budget and an
    # authoritative full run refuses to start without one, so every scratch
    # estate carries one too. The maximum is deliberately enormous rather than
    # the committed 120: a scratch estate passes a handful of cases in a few
    # seconds, which is a per-case cost orders of magnitude worse than the real
    # estate's, and a fixture pinned at the real threshold would breach it by
    # construction on every case in this file.
    (tests_dir / "authoritative-runtime-budget.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "metric": "secondsPerThousandCases",
                "maxSecondsPerThousandCases": 1000000,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if durations is not None:
        (tests_dir / "authoritative-harness-durations.tsv").write_text(
            "".join("%s\t%s\n" % (name, secs) for name, secs in durations)
        )

    # #630 S7. The tier attributes its change set through bin/cctally-test-owners
    # and runs that tool's own verification before accepting any narrow result,
    # so a tier estate carries the tool, the ownership rows DERIVED from each
    # harness's text, and the fixture directories those rows declare. Derived
    # rather than hand-written: a hand-built row encodes the very declaration the
    # verification is supposed to check.
    if ownership:
        shutil.copy2(BIN / "cctally-test-owners", bindir / "cctally-test-owners")
        (bindir / "cctally-test-owners").chmod(0o755)
        owners = _load_owners()
        public_rows, private_rows = {}, {}
        visibility = dict(_expected_names(private, extra_manifest_harnesses))
        for name, vis in sorted(visibility.items()):
            harness = bindir / f"cctally-{name}-test"
            if not harness.is_file():
                continue
            scrape = owners.scrape_fixture_edges(
                harness.read_text(encoding="utf-8")
            )
            row = {"fixturePaths": sorted(scrape.edges), "sourcePaths": []}
            (private_rows if vis != "public" else public_rows)[name] = row
            for edge in scrape.edges:
                target = repo / edge
                if edge.startswith("tests/fixtures/"):
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "seed.json").write_text("{}\n", encoding="utf-8")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        (tests_dir / "harness-ownership.json").write_text(
            json.dumps({"schemaVersion": 1, "harnesses": public_rows}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        if private:
            (tests_dir / "harness-ownership.private.json").write_text(
                json.dumps(
                    {"schemaVersion": 1, "harnesses": private_rows}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )

    if git:
        _git_commit_all(repo, "seed")
    return runner


def _load_owners():
    """bin/cctally-test-owners is EXTENSIONLESS, so spec_from_file_location
    returns None for it; load it by hand the way tests/test_harness_ownership.py
    does."""
    loader = importlib.machinery.SourceFileLoader(
        "_cctally_test_owners_sched", str(BIN / "cctally-test-owners")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check, timeout=60,
    )


def _git_commit_all(repo, message):
    if not (repo / ".git").exists():
        _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(
        repo, "-c", "user.email=t@e", "-c", "user.name=t",
        "commit", "-q", "--allow-empty", "-m", message,
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _admit(runner, env_overrides=None):
    """Drive the runner past plan mode so admission actually runs. Admission
    precedes LOGDIR creation, so a refusal never reaches the harness pool."""
    env = {"PATH": os.environ["PATH"], "CCTALLY_TEST_JOBS": "1"}
    env.update(env_overrides or {})
    return subprocess.run(
        [str(runner)], env=env, capture_output=True, text=True, timeout=60
    )


def test_non_executable_test_remote_is_a_manifest_admission_error(tmp_path):
    """The manifest, not the retired `required_harnesses` list, produces this.
    A lost mode bit reports as a mode problem, never as an absent harness."""
    runner = _tree(tmp_path, private=True, test_remote_mode=0o644)
    p = _admit(runner)
    assert p.returncode == 3, p.stdout + p.stderr
    assert "not executable" in p.stderr
    assert "test-remote" in p.stderr
    assert "manifest row 'test-remote' has no" not in p.stderr


def test_missing_test_remote_is_a_manifest_admission_error_in_private_tree(
        tmp_path):
    """Deleting it outright is as bad as losing its executable bit (#446),
    but the two are now distinct reason codes."""
    runner = _tree(tmp_path, private=True, test_remote_mode=None)
    p = _admit(runner)
    assert p.returncode == 3, p.stdout + p.stderr
    assert "manifest row 'test-remote' has no" in p.stderr


def test_plan_mode_still_exits_before_admission(tmp_path):
    """Plan mode must stay side-effect-free: it exits before the manifest is
    even parsed, so a tree that admission would refuse still plans."""
    runner = _tree(tmp_path, private=True, test_remote_mode=None)
    p = _plan({}, runner=runner)
    assert p.returncode == 0, p.stderr
    assert "harnesses=" in p.stdout


def test_public_subset_tree_does_not_require_the_private_test_remote_harness(
        tmp_path):
    """The public mirror ships no bin/cctally-test-remote-test — .mirror-allowlist
    excludes both it and the wrapper as maintainer-local tooling, and excludes
    itself. Requiring it there hard-fails the public CI matrix before a single
    harness runs (issue #131: the public matrix runs the shipped subset only)."""
    runner = _tree(tmp_path, private=False, test_remote_mode=None)
    p = _plan({}, runner=runner)
    assert p.returncode == 0, p.stderr
    assert "test-remote" not in _kv(p.stdout)["harnesses"].split()


def test_wall_clock_benchmarks_run_outside_xdist(tmp_path):
    """Wall-clock SLAs must not compete with the parallel pytest estate."""
    repo = tmp_path / "repo"
    bindir = repo / "bin"
    tests_dir = repo / "tests"
    fake_bin = tmp_path / "fake-bin"
    bindir.mkdir(parents=True)
    tests_dir.mkdir()
    fake_bin.mkdir()
    runner = bindir / "cctally-test-all"
    shutil.copy2(RUNNER, runner)
    # Matched as a CLASS: bin/_lib-test-contract.sh sources bin/_lib-fts5-probe.sh
    # and refuses without it (#529 S6, exception X2), so an estate that copied
    # one library by name refuses to start the moment the contract grows a
    # second. The failure is silent-looking rather than obvious — the estate
    # aborts with the contract's own diagnostic, and every assertion about what
    # the aggregator SHOULD have said then reads as a behaviour change.
    for lib in sorted(BIN.glob("_lib-*.sh")):
        shutil.copy2(lib, bindir / lib.name)
    # bin/cctally-test-all imports the evidence kernels (#529 S2) and, since
    # #630 S3, the duration-table validator. See _copy_python_kernels.
    _copy_python_kernels(bindir)

    for name in ("codex-quota", "source-aware", "reconcile"):
        harness = bindir / f"cctally-{name}-test"
        harness.write_text(
            "#!/usr/bin/env bash\nprintf 'passed: 1   failed: 0\\n'\n",
            encoding="utf-8",
        )
        harness.chmod(0o755)

    (tests_dir / "authoritative-test-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "minHarnessRows": 0,
                "harnesses": [
                    {
                        "name": n,
                        "visibility": "public",
                        "minCases": 0,
                        "countPolicy": "fixed",
                    }
                    for n in ("codex-quota", "source-aware", "reconcile")
                ],
                "capabilities": [],
                "forbiddenRegeneration": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # #630 S7. A real estate carries a committed runtime budget and an
    # authoritative full run refuses to start without one, so every scratch
    # estate carries one too. The maximum is deliberately enormous rather than
    # the committed 120: a scratch estate passes a handful of cases in a few
    # seconds, which is a per-case cost orders of magnitude worse than the real
    # estate's, and a fixture pinned at the real threshold would breach it by
    # construction on every case in this file.
    (tests_dir / "authoritative-runtime-budget.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "metric": "secondsPerThousandCases",
                "maxSecondsPerThousandCases": 1000000,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (tests_dir / "test_rebuild_benchmark.py").write_text(
        "# execution is represented by the fake pytest below\n",
        encoding="utf-8",
    )
    (tests_dir / "test_stats_writer_storm_386.py").write_text(
        "# execution is represented by the fake pytest below\n",
        encoding="utf-8",
    )
    calls = tmp_path / "pytest-calls"
    fake_python = fake_bin / "python3"
    # The pytest boundary is faked; everything else DELEGATES to the real
    # interpreter, because the contract library parses the manifest and encodes
    # the outcome object with stdlib json (`python3 -` / `python3 -c`). A stub
    # that answered 2 to every other form would fail the fixture, not the
    # property under test. The two optional-plugin probes keep their fixed
    # answers so this case stays independent of what is pip-installed.
    fake_python.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
    if [ "${3:-}" = "--version" ]; then
        exit 0
    fi
    printf '%s\\n' "$*" >> "$CCTALLY_FAKE_PYTHON_LOG"
    exit 0
fi
if [ "$1" = "-c" ]; then
    case "$2" in
        "import xdist"|"import pytest_timeout") exit 0 ;;
    esac
fi
exec "$CCTALLY_REAL_PYTHON3" "$@"
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "CCTALLY_FAKE_PYTHON_LOG": str(calls),
        "CCTALLY_REAL_PYTHON3": sys.executable,
        "CCTALLY_TEST_JOBS": "2",
    }
    proc = subprocess.run(
        [str(runner)], env=env, capture_output=True, text=True, timeout=30
    )

    assert proc.returncode == 0, proc.stderr
    invocations = calls.read_text(encoding="utf-8").splitlines()
    assert len(invocations) == 2, invocations
    bulk, benchmark = invocations
    assert "tests/" in bulk
    assert "--ignore=tests/test_rebuild_benchmark.py" in bulk
    assert (
        "--deselect=tests/test_stats_writer_storm_386.py::"
        "test_h1_multiwriter_baseline_stays_intact"
    ) in bulk
    assert (
        "--deselect=tests/test_stats_writer_storm_386.py::"
        "test_dashboard_source_reader_releases_rollback_snapshot"
    ) in bulk
    assert " -n 2" in bulk
    assert "tests/test_rebuild_benchmark.py" in benchmark
    assert (
        "tests/test_stats_writer_storm_386.py::"
        "test_h1_multiwriter_baseline_stays_intact"
    ) in benchmark
    assert (
        "tests/test_stats_writer_storm_386.py::"
        "test_dashboard_source_reader_releases_rollback_snapshot"
    ) in benchmark
    assert " -n " not in benchmark


# ------------------------------------------- the effective-configuration plan
#
# With all three roles explicitly exported, the internal BUDGET intermediate no
# longer influences any worker count (#529 S5 section 2.3). Left reporting the
# host's core count, plan output would carry a host-dependent number that
# decides nothing, and two runners would print different plans for identical
# effective configurations.


def test_plan_budget_reports_the_effective_configuration_not_the_core_count():
    env = {
        "CCTALLY_OUTER_JOBS": "4",
        "CCTALLY_INNER_JOBS": "2",
        "CCTALLY_PYTEST_JOBS": "10",
    }
    small = _plan(env, fake_ncpu="8")
    large = _plan(env, fake_ncpu="16")
    assert small.returncode == 0, small.stderr
    assert large.returncode == 0, large.stderr
    assert _kv(small.stdout)["budget"] == "4/2/10", small.stdout
    assert _kv(large.stdout)["budget"] == "4/2/10", large.stdout
    # Every plan line except `ncpu=` is identical across the two hosts. The
    # `ncpu=` line necessarily still differs, because it reports the seam's own
    # value and test_fake_ncpu_inside_plan_mode_is_still_honoured asserts that.
    def _without_ncpu(text):
        return [line for line in text.splitlines() if not line.startswith("ncpu=")]

    assert _without_ncpu(small.stdout) == _without_ncpu(large.stdout)


def test_plan_budget_still_reflects_a_combined_knob():
    kv = _kv(_plan({"CCTALLY_TEST_JOBS": "4"}, fake_ncpu="16").stdout)
    assert kv["budget"] == "4/4/4"


# ------------------------------------------------- plan mode and subset runs


def test_plan_mode_reports_the_subset_selection(tmp_path):
    runner = _tree(tmp_path, private=False, test_remote_mode=None)
    p = _plan({}, runner=runner, args=("--harness", "source-aware"))
    assert p.returncode == 0, p.stdout + p.stderr
    kv = _kv(p.stdout)
    assert kv["mode"] == "subset"
    assert kv["pytest_disposition"] == "skipped"
    # `harnesses=` is the single carrier of the selection. A second key
    # printing the same variable under a different name used to sit beside it,
    # inviting a reader to equate it with the record's manifest-ordered
    # `coverage.selectedHarnesses`, which it is not.
    assert kv["harnesses"] == "source-aware"
    assert "selected" not in kv, kv


def test_plan_mode_reports_a_full_run_as_full(tmp_path):
    subset = _kv(
        _plan(
            {},
            runner=_tree(tmp_path, private=False, test_remote_mode=None),
            args=("--harness", "source-aware"),
        ).stdout
    )
    full = _kv(_plan({}).stdout)
    assert full["mode"] == "full"
    assert full["pytest_disposition"] == "full"
    # The real content of "full": the plan names the whole discovered estate,
    # ordered so `reconcile` stays the last row, and it is strictly wider than
    # a subset plan for the same key.
    names = full["harnesses"].split()
    assert len(names) > len(subset["harnesses"].split())
    assert names[-1] == "reconcile", names


def test_plan_mode_with_a_subset_stays_side_effect_free(tmp_path):
    runner = _tree(tmp_path, private=False, test_remote_mode=None)
    evidence = tmp_path / "ev"
    p = _plan(
        {"CCTALLY_TEST_EVIDENCE_ROOT": str(evidence)},
        runner=runner,
        args=("--harness", "source-aware"),
    )
    assert p.returncode == 0, p.stdout + p.stderr
    assert not evidence.exists(), "plan mode must remain side-effect free"


def test_plan_mode_still_rejects_an_unknown_harness(tmp_path):
    """Selection is validated even in plan mode: a plan for a set that cannot
    be run is not a plan."""
    runner = _tree(tmp_path, private=False, test_remote_mode=None)
    p = _plan({}, runner=runner, args=("--harness", "no-such-harness"))
    assert p.returncode == 2, p.stdout + p.stderr
    assert "no-such-harness" in p.stderr


def test_plan_mode_with_a_subset_publishes_no_outcome_record(tmp_path):
    """A plan is never an authoritative outcome, and a subset plan is not one
    either — the two modes stay mutually exclusive rather than precedence
    ordered."""
    runner = _tree(tmp_path, private=False, test_remote_mode=None)
    outcome = tmp_path / "outcome.json"
    p = _plan(
        {"CCTALLY_TEST_ALL_OUTCOME_FILE": str(outcome)},
        runner=runner,
        args=("--harness", "source-aware"),
    )
    assert p.returncode == 2, p.stdout + p.stderr
    assert not outcome.exists()


# ------------------------------------------- dispatch order vs report order
#
# preserve-item 29: the printed harness row order is deterministic and
# independent of dispatch order. Before #630 S3 that held only because the two
# orders were the same shell array. The split below makes it structural.


def test_plan_reports_dispatch_order_and_scheduler_mode_separately(tmp_path):
    """The two orders are separately named even when identical. Without a
    duration table the scheduler must say so rather than silently defaulting."""
    runner = _tree(tmp_path, private=False, test_remote_mode=None)
    proc = _plan({}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert "harnesses" in lines and "dispatch_harnesses" in lines
    assert lines["scheduler"] == "fallback"
    # Identical here by construction; Task 2 makes them differ.
    assert lines["dispatch_harnesses"] == lines["harnesses"]


def test_dispatch_is_longest_first_and_ties_break_by_report_position(tmp_path):
    """Four unequal durations plus a deliberate tie. Asserting the COMPLETE
    sequence: checking only the first element, or set equality, would pass an
    implementation that merely moved the longest harness to the front."""
    runner = _tree(
        tmp_path,
        private=False,
        test_remote_mode=None,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 40)],
    )
    proc = _plan({}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "lpt"
    # report order is discovery order with reconcile appended last
    assert lines["harnesses"] == "codex-quota source-aware reconcile"
    # 40 and 40 tie, so report position decides: source-aware precedes reconcile.
    assert lines["dispatch_harnesses"] == "source-aware reconcile codex-quota"
    # Non-vacuity: the orders must actually differ, or this asserts nothing.
    assert lines["dispatch_harnesses"] != lines["harnesses"]


def test_plan_mode_writes_no_bytecode_into_the_tree_it_reads(tmp_path):
    """Preserve-item 25: plan mode stays side-effect-free.

    The scheduler imports bin/_lib_harness_durations with <root>/bin on
    sys.path, and an ordinary import has CPython write
    bin/__pycache__/_lib_harness_durations.<tag>.pyc beside the source. The
    remote wrapper's environment redirects that write and CI's does not, so
    what keeps the item true is `python3 -B` rather than an ambient variable.
    `_plan` passes a minimal environment carrying neither
    PYTHONDONTWRITEBYTECODE nor PYTHONPYCACHEPREFIX, so a green result here
    cannot have been produced by the caller's own environment.
    """
    runner = _tree(
        tmp_path,
        private=False,
        test_remote_mode=None,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 5)],
    )
    proc = _plan({}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    # Non-vacuity: the run really did import the module. Without this a tree
    # whose scheduler never ran would satisfy the assertion below trivially.
    assert _kv(proc.stdout)["scheduler"] == "lpt", proc.stdout + proc.stderr
    repo = runner.parent.parent
    caches = sorted(str(p.relative_to(repo)) for p in repo.rglob("__pycache__"))
    assert caches == [], caches


@pytest.mark.parametrize(
    "mutation,expected_reason",
    [
        ("missing", "no duration table"),
        ("malformed", "malformed row"),
        ("omits-a-harness", "does not cover"),
        ("unknown-name", "unknown harness"),
        ("validator-unimportable", "no duration table validator"),
        ("undispatchable-harness", "would drop"),
    ],
)
def test_a_bad_duration_table_falls_back_to_report_order(
    tmp_path, mutation, expected_reason
):
    """Each case asserts the DIAGNOSTIC and the resulting order. Asserting only
    exit 0 would pass an implementation that ignored the table entirely.

    Two of the six are not about the table's bytes. `validator-unimportable`
    gives the tree a bin/_lib_harness_durations.py that raises on import, which
    is the scheduler's ImportError branch. Deleting the file instead does NOT
    reach it — see _copy_python_kernels for the measured reason.
    `undispatchable-harness` puts a harness on disk that the manifest
    and the table do not declare: the table is then a valid exact cover, the
    sort silently omits the extra name, and dispatching that shorter list would
    report and count a harness the pool never ran.
    """
    rows = [("codex-quota", 10), ("source-aware", 40), ("reconcile", 5)]
    kwargs = {}
    if mutation == "missing":
        rows = None
    elif mutation == "malformed":
        rows = [("codex-quota", 10), ("source-aware", "not-a-number"), ("reconcile", 5)]
    elif mutation == "omits-a-harness":
        rows = [("codex-quota", 10), ("reconcile", 5)]
    elif mutation == "unknown-name":
        rows = rows + [("no-such-harness", 7)]
    elif mutation == "validator-unimportable":
        kwargs["validator_body"] = (
            'raise ImportError("synthetic: this validator cannot be loaded")\n'
        )
    elif mutation == "undispatchable-harness":
        kwargs["extra_harnesses"] = ("orphan",)
    runner = _tree(
        tmp_path, private=False, test_remote_mode=None, durations=rows, **kwargs
    )
    proc = _plan({}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "fallback"
    assert lines["dispatch_harnesses"] == lines["harnesses"]
    assert expected_reason in proc.stderr, proc.stderr


def test_a_fallback_diagnostic_never_reaches_the_dispatch_list(tmp_path):
    """The capture takes stdout alone.

    It used to be `2>&1`, and the shell read only the first line of the merged
    stream — so anything the interpreter printed to stderr on a ZERO exit
    became the dispatch list. PYTHONVERBOSE reproduces that exactly and
    deterministically: the first merged line is `import _frozen_importlib #
    frozen`, which is what the pool would then have been asked to run. The
    table here is perfectly good, so the assertion is that dispatch is still
    the real longest-first sequence.
    """
    runner = _tree(
        tmp_path,
        private=False,
        test_remote_mode=None,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 5)],
    )
    proc = _plan({"PYTHONVERBOSE": "1"}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "lpt", proc.stdout + proc.stderr
    assert lines["dispatch_harnesses"] == "source-aware codex-quota reconcile", (
        proc.stdout + proc.stderr
    )


def test_a_good_table_produces_no_diagnostic_at_all(tmp_path):
    """The quiet path is asserted, not assumed.

    The fallback branch used to echo the captured text unconditionally, so a
    run whose capture was empty printed a bare blank line to stderr — a
    diagnostic reporting nothing, on a run where nothing was wrong. Asserting
    the absence of a substring would not catch that, because the defect emitted
    no substring; the blank line IS the symptom, so the blank line is what is
    asserted against.

    `stderr == ""` was stricter than the property and carried no diagnostic
    value: any byte from any interpreter in the chain — a DeprecationWarning, a
    PYTHONWARNINGS setting, a locale complaint — reddened this case while
    saying nothing about the scheduler."""
    runner = _tree(
        tmp_path,
        private=False,
        test_remote_mode=None,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 5)],
    )
    proc = _plan({}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    assert _kv(proc.stdout)["scheduler"] == "lpt", proc.stdout
    blank = [line for line in proc.stderr.splitlines() if not line.strip()]
    assert not blank, repr(proc.stderr)
    assert "scheduler:" not in proc.stderr, proc.stderr


def test_the_dispatch_override_forces_report_order_on_an_identical_tree(tmp_path):
    """#630 S3's decision rule compares longest-first dispatch against report
    order. Without this override the only way to reach report order is to remove
    the duration table, which faults pre-flight and reds the preflight harness's
    real-tree case — so the two arms could not be measured on the same tree, and
    a paired difference over two different trees is not a paired difference."""
    runner = _tree(
        tmp_path,
        private=False,
        test_remote_mode=None,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 40)],
    )
    baseline = _kv(_plan({}, runner=runner).stdout)
    proc = _plan({"CCTALLY_TEST_ALL_DISPATCH": "report"}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "report-forced"
    assert lines["dispatch_harnesses"] == lines["harnesses"]
    # Non-vacuity: the override is only meaningful if the unforced tree really
    # would have dispatched in a different order.
    assert baseline["scheduler"] == "lpt"
    assert baseline["dispatch_harnesses"] != baseline["harnesses"]
    # The estate itself must be untouched — only the order may differ.
    assert lines["harnesses"] == baseline["harnesses"]


def test_the_dispatch_override_accepts_the_shipped_arm_explicitly(tmp_path):
    """`lpt` names the default rather than changing it, so a measurement script
    can state both arms rather than expressing one of them as an absence."""
    runner = _tree(
        tmp_path,
        private=False,
        test_remote_mode=None,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 40)],
    )
    proc = _plan({"CCTALLY_TEST_ALL_DISPATCH": "lpt"}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "lpt"
    assert lines["dispatch_harnesses"] == "source-aware reconcile codex-quota"


def test_an_unrecognized_dispatch_override_refuses_rather_than_guessing(tmp_path):
    """Fail closed. A typo that fell through to the default would produce a run
    labelled as the treatment arm while the operator believed it was the
    control, and the whole measurement would be silently wrong."""
    runner = _tree(
        tmp_path,
        private=False,
        test_remote_mode=None,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 40)],
    )
    proc = _plan({"CCTALLY_TEST_ALL_DISPATCH": "reoprt"}, runner=runner)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "CCTALLY_TEST_ALL_DISPATCH" in proc.stderr
    assert "reoprt" in proc.stderr


# ------------------------------------------------------ the tiered mode (#630 S7)
#
# `--tier-fast <BASE-REV>` takes a BASE REVISION, never a revision range, and
# derives its change set from the merge base UNIONED with working-tree dirt. The
# distinction is what makes the tier citable while `--harness` cannot be:
# `--harness` takes a set the caller chose, so nothing in the tree can prove the
# caller chose correctly, and preserve-item 19 records that exiting 0 for it "was
# considered and rejected". A tier's set is a function of the tree.
#
# Revision 1 accepted an arbitrary range and passed it to `git diff`. The wrapper
# certifies the complete MATERIALIZED working tree, uncommitted and untracked
# inputs included (preserve-item 10), so a caller could have supplied an unrelated
# non-empty range that selected fewer harnesses than the tree actually required
# and received a citable receipt for it.

_TIER_HARNESS = (
    "#!/usr/bin/env bash\n"
    'cp "$REPO_ROOT/tests/fixtures/{name}/seed.json" .\n'
    "exit 0\n"
)


def _tier_tree(tmp_path, *, private=True, durations=None):
    """A synthetic checkout the tier can actually attribute over: a git repo,
    ownership rows derived from each harness's own text, `frontend` (public, and
    force-included in both profiles) and `test-remote` (private, and force-
    included only where the active profile declares it)."""
    if private and (
        not (REPO / ".githooks").exists()
        or not (REPO / ".githooks" / "_match.py").exists()
    ):
        pytest.skip("the allowlist classifier .githooks/_match.py is maintainer-local")
    return _tree(
        tmp_path,
        private=private,
        test_remote_mode=0o755 if private else None,
        durations=durations,
        extra_manifest_harnesses=(("frontend", "public"),),
        harness_bodies={
            "codex-quota": _TIER_HARNESS.format(name="codex-quota"),
            "source-aware": _TIER_HARNESS.format(name="source-aware"),
        },
        ownership=True,
        git=True,
    )


def _tier_plan(runner, base, env_overrides=None, extra_args=()):
    return _plan(
        env_overrides or {}, runner=runner, args=("--tier-fast", base, *extra_args)
    )


def _head(runner):
    return _git(runner.parent.parent, "rev-parse", "HEAD").stdout.strip()


def _manifest_names(runner, profile):
    doc = json.loads(
        (runner.parent.parent / "tests" / "authoritative-test-manifest.json")
        .read_text(encoding="utf-8")
    )
    return [
        row["name"]
        for row in doc["harnesses"]
        if profile == "private" or row["visibility"] == "public"
    ]


def test_the_tier_basis_includes_working_tree_dirt(tmp_path):
    """#630 S7 / pre-plan review P1. The wrapper certifies the MATERIALIZED
    tree, uncommitted and untracked inputs included, so a basis computed from
    commits alone lets an unrelated base select fewer harnesses than the tree
    needs and still mint a citable receipt. Because the working-tree half is
    unioned in, a dirty tree can only WIDEN the selection, never narrow it."""
    runner = _tier_tree(tmp_path)
    repo = runner.parent.parent
    base = _head(runner)

    # One tracked modification and one untracked addition. Neither is in the
    # committed diff against the merge base, which is empty here by construction.
    (repo / "bin" / "cctally-source-aware-test").write_text(
        _TIER_HARNESS.format(name="source-aware") + "# touched\n", encoding="utf-8"
    )
    (repo / "tests" / "fixtures" / "codex-quota" / "new.json").write_text(
        "{}\n", encoding="utf-8"
    )

    plan = _kv(_tier_plan(runner, base).stdout)
    selected = plan["tier_selected"].split()
    assert "source-aware" in selected, plan
    assert "codex-quota" in selected, plan
    assert plan["tier_committed_paths"] == "0", plan
    assert plan["tier_worktree_paths"] == "2", plan
    # Non-vacuity: the tier really narrowed, so "everything is selected" cannot
    # be what made the two assertions above true.
    assert plan["tier_omitted"].split() == ["reconcile"], plan


def test_the_tier_basis_counts_a_committed_change_separately(tmp_path):
    """The other half of the union, and the control for the case above: the same
    two paths, committed rather than left dirty, move from `worktreePaths` to
    `committedPaths` and select the same harnesses."""
    runner = _tier_tree(tmp_path)
    repo = runner.parent.parent
    base = _head(runner)
    (repo / "bin" / "cctally-source-aware-test").write_text(
        _TIER_HARNESS.format(name="source-aware") + "# touched\n", encoding="utf-8"
    )
    (repo / "tests" / "fixtures" / "codex-quota" / "new.json").write_text(
        "{}\n", encoding="utf-8"
    )
    _git_commit_all(repo, "work")

    plan = _kv(_tier_plan(runner, base).stdout)
    assert plan["tier_committed_paths"] == "2", plan
    assert plan["tier_worktree_paths"] == "0", plan
    selected = plan["tier_selected"].split()
    assert "source-aware" in selected and "codex-quota" in selected, plan
    assert plan["tier_omitted"].split() == ["reconcile"], plan


def test_the_tier_basis_is_recorded_so_selection_is_reproducible(tmp_path):
    """The resolved basis is recorded rather than left in the caller's shell
    history, so the selection can be reproduced from the record."""
    runner = _tier_tree(tmp_path)
    plan = _kv(_tier_plan(runner, _head(runner)).stdout)
    assert len(plan["tier_base_oid"]) == 40, plan
    assert len(plan["tier_head_oid"]) == 40, plan
    assert plan["tier_base_oid"] == plan["tier_head_oid"], plan
    assert plan["tier_committed_paths"].isdigit(), plan
    assert plan["tier_worktree_paths"].isdigit(), plan


def test_an_unresolvable_base_is_a_usage_error(tmp_path):
    runner = _tier_tree(tmp_path)
    p = _tier_plan(runner, "not-a-rev")
    assert p.returncode == 2, p.stdout + p.stderr
    assert "not-a-rev" in p.stderr, p.stderr


def test_the_tier_refuses_a_tree_that_is_not_a_repository(tmp_path):
    """The basis is a git computation, so a tree with no repository cannot
    produce one. Refusing is the fail-closed answer; a tier that silently
    proceeded would certify a selection it never derived.

    #630 S7 / Task 2 review: made hermetic. `git -C <dir>` walks UP from that
    directory, so "this tree is not a repository" was a claim about the ambient
    filesystem rather than about the fixture — under a tmp root that happened to
    sit inside a checkout, git would resolve an enclosing work tree and the case
    would assert the opposite of what it names. GIT_CEILING_DIRECTORIES stops
    the walk, and the precondition below proves it stopped."""
    runner = _tree(
        tmp_path, private=False, test_remote_mode=None,
        extra_manifest_harnesses=(("frontend", "public"),), ownership=True,
    )
    repo = runner.parent.parent
    ceiling = {"GIT_CEILING_DIRECTORIES": str(repo.parent)}
    # Asserted at call time rather than assumed: if some future tmp layout let
    # git find a work tree here anyway, this fails as a broken fixture instead
    # of quietly turning the case below into a test of something else.
    probe = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
        env={**os.environ, **ceiling}, timeout=60,
    )
    assert probe.returncode != 0, (
        "fixture precondition: the synthetic tree must resolve NO enclosing "
        f"git work tree, and it resolved {probe.stdout.strip()!r}"
    )

    p = _tier_plan(runner, "HEAD", env_overrides=ceiling)
    assert p.returncode == 2, p.stdout + p.stderr
    # Named, because before the flag existed this case passed on
    # "unrecognised argument '--tier-fast'" — an exit 2 for a different reason.
    assert "git" in p.stderr.lower(), p.stderr


# Each row is (argv after the runner, the stderr sentence unique to that
# refusal). Four separate mistakes, and a caller can only fix the one they
# made — which is exactly why `--harness` carries the same four and says so in
# its own comment.
#
# Non-vacuity is what the cross-exclusion loop below buys, and it is not
# theoretical here. Observed with the four refusals removed from
# bin/cctally-test-all: `--tier-fast` alone crashes on `$2: unbound variable`
# at exit 1, while the other three all reach the basis program and produce ONE
# shared sentence — `--tier-fast could not derive a change set (base revision
# '...')`. Three distinct mistakes collapsing into one message is the
# precise failure this table exists to catch, and an assertion checking only
# "exit 2 and --tier-fast appears" would pass against every one of them.
TIER_ARGUMENT_REFUSALS = {
    "no_value": (
        ("--tier-fast",),
        "--tier-fast requires a base revision and none followed it",
    ),
    "empty_value": (
        ("--tier-fast", ""),
        "was given an empty base revision",
    ),
    "option_as_value": (
        ("--tier-fast", "--with-pytest"),
        "instead of a base revision, so its value is missing",
    ),
    "repeated_flag": (
        ("--tier-fast", "a", "--tier-fast", "b"),
        "--tier-fast may be given at most once",
    ),
}


@pytest.mark.parametrize("case", sorted(TIER_ARGUMENT_REFUSALS))
def test_a_malformed_tier_argument_names_its_own_mistake(tmp_path, case):
    """#630 S7 / Task 2 review. The four refusals shipped correct and pinned by
    nothing: no test named any of their sentences, so a future edit collapsing
    them into one catch-all would have been invisible."""
    args, fragment = TIER_ARGUMENT_REFUSALS[case]
    runner = _tree(tmp_path, private=False, test_remote_mode=None)
    p = _plan({}, runner=runner, args=args)
    assert p.returncode == 2, (case, p.stdout, p.stderr)
    assert fragment in p.stderr, (case, p.stderr)
    # No OTHER refusal's sentence, and not the generic unrecognised-argument
    # catch-all either — that message names `--tier-fast BASE-REV` in its own
    # text, so a token-level assertion would accept it.
    for other, (_, other_fragment) in TIER_ARGUMENT_REFUSALS.items():
        if other != case:
            assert other_fragment not in p.stderr, (case, other, p.stderr)
    assert "unrecognised argument" not in p.stderr, (case, p.stderr)
    assert "could not derive a change set" not in p.stderr, (case, p.stderr)


def test_the_tier_is_mutually_exclusive_with_harness(tmp_path):
    """Preserve-item 19. A caller-chosen subset stays uncitable, so the two
    request surfaces may not be combined into one run."""
    runner = _tier_tree(tmp_path)
    p = _tier_plan(runner, _head(runner), extra_args=("--harness", "source-aware"))
    assert p.returncode == 2, p.stdout + p.stderr
    # BOTH tokens, because the refusal has to name the conflict rather than one
    # argument: before the flag existed this case passed on "unrecognised
    # argument '--tier-fast'", which is an exit 2 for an unrelated reason.
    assert "--tier-fast cannot accompany --harness" in p.stderr, p.stderr


def test_the_tier_is_mutually_exclusive_with_with_pytest(tmp_path):
    """The tier narrows the harness axis alone; pytest always runs in full, so
    a flag that re-enables it means the caller misunderstood the mode."""
    runner = _tier_tree(tmp_path)
    p = _tier_plan(runner, _head(runner), extra_args=("--with-pytest",))
    assert p.returncode == 2, p.stdout + p.stderr
    assert "--tier-fast cannot accompany --with-pytest" in p.stderr, p.stderr


def test_the_tier_is_mutually_exclusive_with_the_linux_matrix(tmp_path):
    runner = _tier_tree(tmp_path)
    p = _tier_plan(
        runner, _head(runner), env_overrides={"CCTALLY_LINUX_MATRIX_RUN": "1"}
    )
    assert p.returncode == 2, p.stdout + p.stderr
    assert (
        "--tier-fast cannot accompany CCTALLY_LINUX_MATRIX_RUN" in p.stderr
    ), p.stderr


def test_test_remote_is_forced_only_within_the_active_profile(tmp_path):
    """#630 S7 / pre-plan review P1. Preserve-item 27 keeps test-remote in the
    full Gate 0, so the tier must not become the second thing that drops it —
    but it carries `visibility: private`, it is absent from the mirror, and
    contract_manifest_harness_names deliberately returns only public rows for
    the public profile. Forcing it unconditionally would put a name in
    `selected` that the public manifest does not contain, so the partition
    assertion could never hold on a public tree. Revision 1 made the tier
    unimplementable on the public profile in exactly this way."""
    private = _kv(_tier_plan(_tier_tree(tmp_path / "priv"), "HEAD").stdout)
    assert "test-remote" in private["tier_selected"].split(), private

    public_runner = _tier_tree(tmp_path / "pub", private=False)
    public = _kv(_tier_plan(public_runner, "HEAD").stdout)
    assert "test-remote" not in public["tier_selected"].split(), public
    assert "test-remote" not in public["tier_omitted"].split(), public


def test_frontend_is_forced_in_both_profiles(tmp_path):
    """`frontend` carries `visibility: public`, so it belongs to both profiles.
    The tier reasons about the harness axis alone and has no view of the
    frontend estate, so omitting the harness that executes it would be a
    narrowing the tier cannot justify."""
    for label, private in (("priv", True), ("pub", False)):
        runner = _tier_tree(tmp_path / label, private=private)
        plan = _kv(_tier_plan(runner, "HEAD").stdout)
        assert "frontend" in plan["tier_selected"].split(), (label, plan)


def test_selected_and_omitted_are_an_exact_partition(tmp_path):
    runner = _tier_tree(tmp_path)
    repo = runner.parent.parent
    (repo / "tests" / "fixtures" / "source-aware" / "extra.json").write_text(
        "{}\n", encoding="utf-8"
    )
    plan = _kv(_tier_plan(runner, _head(runner)).stdout)
    selected = plan["tier_selected"].split()
    omitted = plan["tier_omitted"].split()
    manifest = _manifest_names(runner, "private")
    # #630 S7 / Task 2 review. The anchor comes FIRST, because without it every
    # assertion below is satisfied by a fully widened tier: `sorted(selected +
    # omitted) == sorted(manifest)` holds when `omitted` is empty, and both
    # order assertions are trivially true of any subsequence. This fixture
    # dirties one fixture directory, which is a direct ownership edge, so a
    # narrow result is what should be observed here.
    assert omitted, plan
    assert sorted(selected + omitted) == sorted(manifest), plan
    assert not (set(selected) & set(omitted)), plan
    # Manifest order, so two invocations over the same change produce
    # byte-identical records.
    assert selected == [n for n in manifest if n in set(selected)], plan
    assert omitted == [n for n in manifest if n in set(omitted)], plan


def test_a_basis_attributing_to_nothing_runs_the_mandatory_set(tmp_path):
    """#630 S7 / pre-plan review P2. Revision 1 refused an empty selection. That
    rule is either wrong — checked BEFORE the force-includes it rejects a
    legitimate no-op basis, which bin/cctally-test-owners already supports as a
    successful query — or dead, because checked after them it can never fire.
    The effective selection is safe UNION mandatory and is never empty."""
    runner = _tier_tree(tmp_path)
    plan = _kv(_tier_plan(runner, _head(runner)).stdout)
    selected = plan["tier_selected"].split()
    assert selected == ["frontend", "test-remote"], plan
    assert plan["tier_omitted"].split(), plan


def test_an_ownership_failure_widens_to_the_full_estate(tmp_path):
    """#630 S7 / pre-plan review P1 and ordering hazard 5. Estate equality
    proves the estate's NAMES; it says nothing about whether the ownership
    declarations used to narrow are true. Verification runs BEFORE a narrow
    result is accepted, and a failure widens rather than trusting it."""
    runner = _tier_tree(tmp_path)
    repo = runner.parent.parent
    path = repo / "tests" / "harness-ownership.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["harnesses"]["reconcile"]["fixturePaths"] = ["tests/fixtures/source-aware"]
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    plan = _kv(_tier_plan(runner, _head(runner)).stdout)
    assert plan["tier_omitted"] == "", plan
    assert plan["tier_selected"].split() == _manifest_names(runner, "private"), plan
    assert plan["mode"] == "tier-fast", plan


def test_a_missing_owners_tool_widens_rather_than_narrowing(tmp_path):
    """A tier that cannot establish ownership must select everything, never
    nothing.

    #630 S7 / Task 2 review corrects the justification this case shipped with.
    It claimed the published public tree carries no bin/cctally-test-owners.
    That is false: `.mirror-allowlist` line 8 is the positive glob
    `bin/cctally-*`, no negation covers the tool, and .githooks/_match.py
    classifies it `public`. Only the private overlay
    tests/harness-ownership.private.json is withheld, and the tool discovers it
    by existence and operates on public rows alone when it is absent — so a
    tier on a published public tree attributes normally. The branch remains,
    because a tree that has lost the tool for any reason must still fail
    closed; only the reason for it was wrong."""
    runner = _tier_tree(tmp_path)
    (runner.parent / "cctally-test-owners").unlink()
    plan = _kv(_tier_plan(runner, _head(runner)).stdout)
    assert plan["tier_omitted"] == "", plan
    assert plan["tier_selected"].split() == _manifest_names(runner, "private"), plan


def test_a_tier_whose_set_equals_the_estate_still_records_tier_fast(tmp_path):
    """#630 S7 / pre-plan review P1. `coverage.mode` states INVOCATION and
    SELECTION semantics, never the size of the result. Deciding it by result
    would let a widened tier mint the releasable full-suite gate — and given
    F39's measurement that attribution widens on 119 of 119 real merges, the
    widened tier is the NORMAL case rather than the corner one."""
    runner = _tier_tree(tmp_path)
    repo = runner.parent.parent
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "note.md").write_text("unattributed\n", encoding="utf-8")
    plan = _kv(_tier_plan(runner, _head(runner)).stdout)
    assert plan["tier_omitted"] == "", plan
    assert plan["mode"] == "tier-fast", plan
    assert plan["pytest_disposition"] == "full", plan


def test_the_tier_never_narrows_the_pytest_axis(tmp_path):
    """Spec §3.2. The tier narrows the harness axis and nothing else: the
    aggregator has no Playwright execution axis to narrow, because
    bin/cctally-frontend-test runs Vitest and its Playwright involvement is lint
    and type-checking rather than suite execution."""
    runner = _tier_tree(tmp_path)
    plan = _kv(_tier_plan(runner, _head(runner)).stdout)
    assert plan["pytest_disposition"] == "full", plan


def test_a_tier_plan_publishes_no_outcome_record(tmp_path):
    """Pins the plan-plus-outcome-file refusal, and nothing more than that.

    #630 S7 / Task 2 review: this case says nothing about the tier. The refusal
    fires before argv is parsed, so the same exit 2 and the same empty outcome
    path are produced with or without `--tier-fast` — the case would pass
    unchanged against a build that had no tier at all. It is kept because the
    refusal is worth pinning; preserve-item 25 for tier mode specifically is
    proved by test_a_tier_plan_stays_side_effect_free and by
    test_a_tier_plan_writes_no_bytecode_into_the_tree_it_reads, both of which
    reach code the tier alone runs."""
    runner = _tier_tree(tmp_path)
    outcome = tmp_path / "outcome.json"
    p = _tier_plan(
        runner, _head(runner),
        env_overrides={"CCTALLY_TEST_ALL_OUTCOME_FILE": str(outcome)},
    )
    assert p.returncode == 2, p.stdout + p.stderr
    assert "CCTALLY_TEST_ALL_PLAN" in p.stderr, p.stderr
    assert not outcome.exists()


def test_a_tier_plan_stays_side_effect_free(tmp_path):
    runner = _tier_tree(tmp_path)
    evidence = tmp_path / "ev"
    p = _tier_plan(
        runner, _head(runner),
        env_overrides={"CCTALLY_TEST_EVIDENCE_ROOT": str(evidence)},
    )
    assert p.returncode == 0, p.stdout + p.stderr
    assert not evidence.exists(), "plan mode must remain side-effect free"


def test_the_tier_dispatches_only_what_it_selected(tmp_path):
    """The selection reaches the pool, not merely the record. `harnesses=` is
    the report order of what will execute and `dispatch_harnesses=` is what the
    pool receives; neither may name a harness the tier omitted."""
    runner = _tier_tree(tmp_path)
    plan = _kv(_tier_plan(runner, _head(runner)).stdout)
    omitted = set(plan["tier_omitted"].split())
    assert omitted, plan
    assert not (set(plan["harnesses"].split()) & omitted), plan
    assert not (set(plan["dispatch_harnesses"].split()) & omitted), plan


def test_a_tier_plan_writes_no_bytecode_into_the_tree_it_reads(tmp_path):
    """Preserve-item 25 again, over the path the tier adds. The tier runs a
    basis program and bin/cctally-test-owners before plan mode, and `_plan`
    passes a minimal environment carrying neither PYTHONDONTWRITEBYTECODE nor
    PYTHONPYCACHEPREFIX, so a green result here cannot have been produced by
    the caller's own environment."""
    runner = _tier_tree(tmp_path)
    repo = runner.parent.parent
    (repo / "tests" / "fixtures" / "source-aware" / "extra.json").write_text(
        "{}\n", encoding="utf-8"
    )
    p = _tier_plan(runner, _head(runner))
    assert p.returncode == 0, p.stdout + p.stderr
    # Non-vacuity: the tier really resolved a selection rather than bailing out
    # before it ever ran the tools this case is about.
    assert _kv(p.stdout)["tier_omitted"].split(), p.stdout
    caches = sorted(str(x.relative_to(repo)) for x in repo.rglob("__pycache__"))
    assert caches == [], caches
