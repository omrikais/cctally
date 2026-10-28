"""`bin/cctally-preflight-test`'s parallel case-group dispatch (#940 F1).

`CCTALLY_PREFLIGHT_TEST_JOBS` runs the harness's case groups several at a time.
Two ways that dispatch used to hang rather than finish:

- a value too large for the shell's integers passed validation, because the
  failing comparison read as "not less than one", and every later comparison
  against it failed too, so no slot ever freed;
- a group killed by a signal never ran its EXIT trap, so its completion marker
  never appeared and its slot stayed taken for the rest of the run.

The second is exercised on the scheduler itself, cut out of the harness
verbatim, so a group can be killed deterministically without paying for the
harness's real cases.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]
HARNESS = REPO / "bin" / "cctally-preflight-test"


def test_a_jobs_value_too_large_for_the_shell_is_refused(tmp_path):
    env = dict(os.environ, CCTALLY_PREFLIGHT_TEST_JOBS="9" * 20,
               TMPDIR=str(tmp_path))
    proc = subprocess.run(
        [str(HARNESS)], env=env, capture_output=True, text=True,
        timeout=30,  # the refusal happens before any case runs
    )
    assert proc.returncode == 1, (proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
    assert "CCTALLY_PREFLIGHT_TEST_JOBS must be a positive integer" in proc.stderr
    assert "PASS " not in proc.stdout


def test_the_shared_job_budget_sets_the_group_count_when_the_own_one_is_unset(tmp_path):
    """#940 fix round 2: `CCTALLY_TEST_JOBS`, the budget bin/cctally-test-all
    and bin/cctally-test-remote give every harness, sets how many case groups
    run at once when `CCTALLY_PREFLIGHT_TEST_JOBS` is unset. It is validated
    under its own name, and the harness's own variable still wins."""
    env = {k: v for k, v in os.environ.items()
           if k != "CCTALLY_PREFLIGHT_TEST_JOBS"}
    env.update(CCTALLY_TEST_JOBS="9" * 20, TMPDIR=str(tmp_path))
    proc = subprocess.run(
        [str(HARNESS)], env=env, capture_output=True, text=True,
        timeout=30,  # the refusal happens before any case runs
    )
    assert proc.returncode == 1, (proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
    assert "CCTALLY_TEST_JOBS must be a positive integer" in proc.stderr
    assert "CCTALLY_PREFLIGHT_TEST_JOBS" not in proc.stderr
    assert "PASS " not in proc.stdout

    env.update(CCTALLY_PREFLIGHT_TEST_JOBS="x", CCTALLY_TEST_JOBS="2")
    proc = subprocess.run(
        [str(HARNESS)], env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 1, (proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
    assert "CCTALLY_PREFLIGHT_TEST_JOBS must be a positive integer, got x" in proc.stderr
    assert "PASS " not in proc.stdout


def _scheduler_source() -> str:
    """`report`, `case_group` and `run_case_groups`, verbatim from the harness."""
    lines = HARNESS.read_text(encoding="utf-8").splitlines()
    start = lines.index("report () {")
    runner = lines.index("run_case_groups () {")
    end = lines.index("}", runner)
    return "\n".join(lines[start:end + 1]) + "\n"


def _interpreters() -> list[str]:
    found: dict[str, str] = {}
    for candidate in (shutil.which("bash"), "/bin/bash"):
        if candidate and os.path.exists(candidate):
            found.setdefault(os.path.realpath(candidate), candidate)
    return sorted(found.values())


_GROUPS = """\
killed_a () { echo "started killed_a"; sh -c 'kill -9 "$PPID"'; report killed_a PASS; }
killed_b () { echo "started killed_b"; sh -c 'kill -9 "$PPID"'; report killed_b PASS; }
ok_c () { report ok_c PASS; }
ok_d () { report ok_d PASS; }
case_group killed_a
case_group killed_b
case_group ok_c
case_group ok_d
run_case_groups
echo "tally $pass_count $fail_count"
"""


def test_a_group_killed_by_a_signal_frees_its_slot_and_is_reported(tmp_path):
    """Two jobs, and both of the first two groups die by SIGKILL: before the fix
    neither slot ever freed and the run never finished. Run under every
    distinct bash available (on macOS that includes /bin/bash, the 3.2 floor),
    inside one node so the node set does not depend on the host."""
    interpreters = _interpreters()
    assert interpreters, "no bash interpreter found"
    for index, bash in enumerate(interpreters):
        _run_killed_groups(tmp_path / f"bash-{index}", bash)


def _run_killed_groups(work: pathlib.Path, bash: str, prelude: str = "") -> None:
    scratch = work / "scratch"
    scratch.mkdir(parents=True)
    script = work / "scheduler.sh"
    script.write_text(
        "set -uo pipefail\n"
        f"SCRATCH_DIR='{scratch}'\n"
        "PREFLIGHT_TEST_JOBS=2\n"
        "pass_count=0\n"
        "fail_count=0\n"
        + prelude + _scheduler_source() + _GROUPS,
        encoding="utf-8",
    )
    proc = subprocess.run(
        [bash, str(script)], capture_output=True, text=True,
        timeout=30,  # four trivial groups; a hang is the defect
    )
    assert proc.returncode == 0, (bash, proc.returncode, proc.stdout, proc.stderr)
    out = proc.stdout.splitlines()
    assert out[0] == "started killed_a", out
    assert out[1].startswith("FAIL killed_a: the case group did not complete"), out
    assert out[2] == "started killed_b", out
    assert out[3].startswith("FAIL killed_b: the case group did not complete"), out
    assert out[4:] == ["PASS ok_c", "PASS ok_d", "tally 2 2"], out


def test_a_killed_groups_reused_pid_does_not_hold_its_slot(tmp_path):
    """#940 F2-d: a killed group's PID may be reused by an unrelated process,
    so `kill -0` alone can report it alive for ever. A slot counts as running
    only while its PID is also one of this shell's own jobs (`jobs -p`).
    Simulated by a `kill -0` that always succeeds: before the fix both slots
    stayed taken and the run never finished."""
    interpreters = _interpreters()
    assert interpreters, "no bash interpreter found"
    reuse = 'kill () { if [ "$1" = -0 ]; then return 0; fi; command kill "$@"; }\n'
    for index, bash in enumerate(interpreters):
        _run_killed_groups(tmp_path / f"bash-{index}", bash, prelude=reuse)


def test_the_scheduler_header_states_that_a_killed_groups_children_can_outlive_it():
    """#940 F2-e: the choice is recorded where the scheduler is described."""
    text = HARNESS.read_text(encoding="utf-8")
    assert "can outlive the harness" in text


def _loader_probe_commands(text: str) -> list[tuple[str, bool]]:
    """Each here-document of the harness whose body loads a module with
    `SourceFileLoader`: its command (backslash continuations joined) and
    whether that command runs `python3 -B`."""
    import re
    lines = text.splitlines()
    found = []
    opener = re.compile(r"<<-?\s*['\"]?([A-Za-z_][\w-]*)['\"]?")
    i = 0
    while i < len(lines):
        m = opener.search(lines[i])
        if not m or lines[i].lstrip().startswith("#"):
            i += 1
            continue
        tag = m.group(1)
        start = i
        while start > 0 and lines[start - 1].rstrip().endswith("\\"):
            start -= 1
        command = " ".join(ln.rstrip("\\").strip() for ln in lines[start:i + 1])
        end = i + 1
        while end < len(lines) and lines[end].strip() != tag:
            end += 1
        body = "\n".join(lines[i + 1:end])
        if "SourceFileLoader" in body:
            found.append((command, bool(re.search(r"\bpython3 -B\b", command))))
        i = end + 1
    return found


def test_every_source_file_loader_probe_runs_python_without_bytecode():
    """#940 F2-g: a probe that loads `bin/cctally-preflight` (or any tool)
    through `SourceFileLoader` runs `python3 -B`, so it writes no bytecode
    beside the tool it loads."""
    probes = _loader_probe_commands(HARNESS.read_text(encoding="utf-8"))
    assert len(probes) >= 2, probes  # the two known probes, at least
    assert [c for c, ok in probes if not ok] == []
    # The rule bites on a probe without -B, continued over two lines.
    synthetic = ("if python3 - \"$x\" \\\n        > log <<'P'\n"
                 "loader = importlib.machinery.SourceFileLoader('m', t)\nP\n")
    assert _loader_probe_commands(synthetic) == [
        ('if python3 - "$x" > log <<\'P\'', False)]
