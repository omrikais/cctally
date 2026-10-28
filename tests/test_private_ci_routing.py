"""Route every workflow event to the work spec 4.11 assigns it (test regime reset).

| Event | Work |
|---|---|
| private push to main | `ci.yml`'s `changed-checks` on lan, plus the dashboard build for a frontend change |
| private release-candidate push | `release-candidate.yml`'s `release-verify` only |
| private schedule or dispatch | `pricing-freshness.yml`'s `check`, and the broad run (`broad-run.yml`) on lan |
| public or fork pull request | hosted Linux only; no fork code on either Mac |

Each test interprets the checked-in `on:` blocks, `if:` conditions and
`runs-on` labels against representative GitHub contexts, across EVERY workflow
file. Literal label assertions alone would miss a swapped branch, and a
per-workflow reading would miss a second workflow that a trigger also starts.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
#: The reservation helper the runner supervisor retired, split so the test
#: regime reset's reference sweep (Task 4.10 step 7) finds no reference; the
#: guards below name it only to forbid it.
RETIRED_RESERVATION_HELPER = "_lib_runner_" "reservation"
# The one self-hosted Actions runner that serves this repository, `cctally-mac`
# on lan. Its `ARM64` label is added by the runner itself.
LAN = ["self-hosted", "macOS", "ARM64", "cctally"]
MAC = ["self-hosted", "macOS", "cctally"]
HOSTED = ["ubuntu-latest"]
PRIVATE = "omrikais/cctally-dev"
PUBLIC = "omrikais/cctally"
ZERO = "0" * 40


# --------------------------------------------------------------- the evaluator
#
# GitHub's expression grammar as these workflows use it: `!`, `&&`, `||`, `==`,
# `!=`, parentheses, single-quoted strings, context paths and `cancelled()`.
# `&&` and `||` return an OPERAND, as GitHub's do. A missing context path is
# None, which is falsy and unequal to every string, which is how a missing job
# output behaves.

_TOKEN = re.compile(r"\s*(\(|\)|&&|\|\||!=|==|!|'[^']*'|[A-Za-z0-9_.\-]+)")


def _tokenize(text):
    pos, tokens = 0, []
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise ValueError("cannot tokenize at %r" % text[pos:pos + 20])
        tokens.append(match.group(1))
        pos = match.end()
    return tokens


def _truthy(value):
    if isinstance(value, str):
        return value != ""
    return bool(value)


class _Parser:
    def __init__(self, tokens, context):
        self.tokens, self.i, self.context = tokens, 0, context

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self):
        token = self.peek()
        self.i += 1
        return token

    def parse(self):
        value = self.parse_or()
        assert self.peek() is None, self.tokens[self.i:]
        return value

    def parse_or(self):
        value = self.parse_and()
        while self.peek() == "||":
            self.take()
            right = self.parse_and()
            value = value if _truthy(value) else right
        return value

    def parse_and(self):
        value = self.parse_unary()
        while self.peek() == "&&":
            self.take()
            right = self.parse_unary()
            value = right if _truthy(value) else value
        return value

    def parse_unary(self):
        if self.peek() == "!":
            self.take()
            return not _truthy(self.parse_unary())
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_primary()
        if self.peek() in ("==", "!="):
            op = self.take()
            right = self.parse_primary()
            return (left == right) if op == "==" else (left != right)
        return left

    def parse_primary(self):
        token = self.take()
        if token == "(":
            value = self.parse_or()
            assert self.take() == ")"
            return value
        if token.startswith("'"):
            return token[1:-1]
        if token == "cancelled":
            assert self.take() == "(" and self.take() == ")"
            return False
        node = self.context
        for part in token.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node


def _evaluate(condition, context):
    return _truthy(_Parser(_tokenize(" ".join(condition.split())), context).parse())


def _context(*, repository=PRIVATE, event="push", head_repo=None, outputs=None):
    return {
        "github": {
            "repository": repository,
            "event_name": event,
            "event": {"pull_request": {"head": {"repo": {"full_name": head_repo or repository}}}}
            if event == "pull_request" else {},
        },
        "needs": {job: {"outputs": values} for job, values in (outputs or {}).items()},
    }


# --------------------------------------------------------------- the triggers


def _filter_matches(pattern, path):
    """GitHub's filter-pattern subset in use: `*` stops at `/`, `**` does not."""
    regex = ""
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            regex, index = regex + "(?:.*/)?", index + 3
        elif pattern.startswith("**", index):
            regex, index = regex + ".*", index + 2
        elif pattern[index] == "*":
            regex, index = regex + "[^/]*", index + 1
        else:
            regex, index = regex + re.escape(pattern[index]), index + 1
    return re.fullmatch(regex, path) is not None


def _paths_admit(spec, changed):
    """`paths` (ordered, `!` negates, last match wins) and `paths-ignore`."""
    if "paths-ignore" in spec:
        if all(any(_filter_matches(p, path) for p in spec["paths-ignore"]) for path in changed):
            return False
    if "paths" in spec:
        def included(path):
            verdict = False
            for pattern in spec["paths"]:
                negated = pattern.startswith("!")
                if _filter_matches(pattern.lstrip("!"), path):
                    verdict = not negated
            return verdict
        return any(included(path) for path in changed)
    return True


def _starts(doc, event):
    triggers = doc.get("on", doc.get(True))
    if isinstance(triggers, str):
        triggers = {triggers: {}}
    name = event["event_name"]
    if name not in triggers:
        return False
    spec = triggers[name] or {}
    if name == "push":
        ref = event["ref"]
        if ref.startswith("refs/heads/"):
            kind, short = "branches", ref[len("refs/heads/"):]
        else:
            kind, short = "tags", ref[len("refs/tags/"):]
        if kind not in spec or not any(_filter_matches(p, short) for p in spec[kind]):
            return False
        return _paths_admit(spec, event["changed"])
    if name == "pull_request":
        if "types" in spec and event["action"] not in spec["types"]:
            return False
        return _paths_admit(spec, event["changed"])
    return True


def _route(job, context):
    if "if" in job and not _evaluate(job["if"], context):
        return None
    runs_on = job["runs-on"]
    if isinstance(runs_on, list):
        return runs_on
    assert runs_on == "ubuntu-latest", runs_on
    return HOSTED


def _workflow_docs():
    return {path.name: yaml.safe_load(path.read_text()) for path in sorted(WORKFLOWS.glob("*.yml"))}


def _jobs(workflow):
    return _workflow_docs()[workflow]["jobs"]


def _routed(event, context, failed=()):
    """`{"<workflow>:<job>": labels}` for every job this event runs, in every workflow.

    `failed` names the started jobs that end in failure.
    """
    found = {}
    for workflow, doc in _workflow_docs().items():
        if not _starts(doc, event):
            continue
        for name, job in doc["jobs"].items():
            needs = job.get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            condition = job.get("if", "")
            # A dependent with no status function is skipped when a dependency
            # was skipped or failed.
            unmet = [dep for dep in needs
                     if f"{workflow}:{dep}" not in found or f"{workflow}:{dep}" in failed]
            if unmet and "cancelled()" not in condition:
                continue
            route = _route(job, context)
            if route is not None:
                found[f"{workflow}:{name}"] = route
    return found


def _push(ref="refs/heads/main", changed=("bin/cctally",), before="a" * 40):
    return {"event_name": "push", "ref": ref, "changed": list(changed), "before": before}


def _pull_request(changed=("bin/cctally",)):
    return {"event_name": "pull_request", "action": "opened", "changed": list(changed)}


# ------------------------------------------------- the changed-checks job, executed


def _step(workflow, job, name):
    return next(step for step in _jobs(workflow)[job]["steps"] if step.get("name") == name)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        capture_output=True, text=True, timeout=60, check=True).stdout.strip()


def _repo_with_change(tmp_path, changed):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "bin" / "cctally").write_text("print(1)\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    before = _git(repo, "rev-parse", "HEAD")
    for rel in changed:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return repo, before, _git(repo, "rev-parse", "HEAD")


def _classified_outputs(tmp_path, changed, *, zero_before=False):
    """Run the changed-checks job's path classification over a real range."""
    step = _step("ci.yml", "changed-checks", "Classify the pushed paths")
    repo, before, after = _repo_with_change(tmp_path, changed)
    out = tmp_path / "github-output"
    env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out),
           "BEFORE": ZERO if zero_before else before, "AFTER": after}
    done = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


@pytest.mark.parametrize("changed,dashboard", [
    (("bin/cctally",), False),
    (("docs/guide.md",), False),
    (("dashboard/web/src/App.tsx",), True),
    (("package.json",), True),
    (("dashboard/web/.nvmrc",), True),
    (("dashboard/static/index.html",), True),
])
def test_main_push_runs_only_changed_checks(tmp_path, changed, dashboard):
    outputs = _classified_outputs(tmp_path, changed)
    assert outputs == {"dashboard": "true" if dashboard else "false"}, outputs
    routed = _routed(_push(changed=changed), _context(outputs={"changed-checks": outputs}))
    expected = {"ci.yml:changed-checks": LAN}
    if dashboard:
        expected["ci.yml:dashboard-build-stability"] = LAN
    assert routed == expected

    job = _jobs("ci.yml")["changed-checks"]
    assert job["timeout-minutes"] == 10
    check = _step("ci.yml", "changed-checks", "Check the pushed range")
    assert check["env"] == {"BEFORE": "${{ github.event.before }}", "AFTER": "${{ github.event.after }}",
                            "CCTALLY_FETCH_TOKEN": "${{ github.token }}"}
    assert SUPERVISOR_EXEC + ' local --sha "$GITHUB_SHA"' in re.sub(r"\\\n\s*", "", check["run"])
    assert "--wait 300 --busy-exit 75" in check["run"]
    assert 'bin/cctally-preflight --changed "$BEFORE..$AFTER"' in check["run"]


# Codex Q6 item 1 and E-7: every workflow step that runs the supervisor's
# `local` makes it the step's own process with `exec`, as the step's final
# command, so the runner's cancellation signals reach it; the two listener
# variables are guarded with a message naming the listener environment.
SUPERVISOR_EXEC = ('exec "${CCTALLY_RUNNER_PYTHON:?is not set by the runner listener environment}" -I -B '
                   '"${CCTALLY_RUNNER_TOOL:?is not set by the runner listener environment}"')
LOCAL_STEPS = {("ci.yml", "changed-checks"), ("ci.yml", "dashboard-build-stability"),
               ("broad-run.yml", "broad-run"), ("release-candidate.yml", "release-verify")}


def _local_steps():
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job_name, job in (yaml.safe_load(path.read_text()).get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if " local --sha" in step.get("run", ""):
                    found.append((path.name, job_name, step))
    return found


def _quote_open(text, state):
    """The shell quote (`'`, `"` or None) open after ``text``, starting in ``state``."""
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
        elif state == "'":
            state = None if ch == "'" else state
        elif ch == "\\":
            escaped = True
        elif state == '"':
            state = None if ch == '"' else state
        elif ch in "'\"":
            state = ch
    return state


def _commands(script):
    """The script's logical lines: comments and blank lines dropped, `\\`
    continuations and lines inside an open quote (a multi-line `bash -c`
    payload) joined."""
    lines, current, quote = [], "", None
    for raw in script.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        continued = quote is None and line.endswith("\\")
        piece = line[:-1].strip() if continued else line
        current += (" " if current else "") + piece
        quote = _quote_open(piece + " ", quote)
        if not continued and quote is None:
            lines.append(current)
            current = ""
    return lines


def test_every_local_step_execs_the_supervisor_as_its_final_command():
    steps = _local_steps()
    expected = {pair for pair in LOCAL_STEPS if (WORKFLOWS / pair[0]).exists()}
    assert {(w, j) for w, j, _s in steps} == expected, steps
    for workflow, job, step in steps:
        commands = _commands(step["run"])
        final = commands[-1]
        assert final.startswith(SUPERVISOR_EXEC + " local --sha"), (workflow, job, final)
        # Nothing may follow it or wrap it: no `||`, no pipeline into another
        # command, no status capture or trailing processing in this step.
        assert sum(SUPERVISOR_EXEC in c for c in commands) == 1, (workflow, job)
        assert "set +e" not in step["run"] and "$?" not in step["run"], (workflow, job)
        head = final.split(" -- ", 1)[0]
        assert "||" not in head and "|" not in head.replace("||", ""), (workflow, job, head)
        assert step.get("id") == "execute", (workflow, job)


@pytest.mark.parametrize("missing", ["CCTALLY_RUNNER_PYTHON", "CCTALLY_RUNNER_TOOL"])
def test_a_listener_without_the_supervisor_variables_fails_the_step_by_name(tmp_path, missing):
    check = _step("ci.yml", "changed-checks", "Check the pushed range")
    env, log = _supervisor_env(tmp_path, busy=False, base={"PATH": os.environ["PATH"], "BEFORE": "a",
                                                           "AFTER": "b"})
    env.pop(missing)
    done = subprocess.run(["bash", "-e", "-c", check["run"]], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode != 0
    assert missing in done.stderr and "runner listener environment" in done.stderr, done.stderr
    assert not log.exists()


# The runner supervisor's `local` verb as the steps call it: the arguments the
# workflows pass are checked, a busy host exits --busy-exit having started
# nothing, and otherwise the command after -- runs in the current directory
# (the supervisor's own checkout is exactly the pushed commit). Each call is
# recorded, one JSON line per call.
_FAKE_SUPERVISOR = """import json, os, re, subprocess, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_SUPERVISOR_LOG"], "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
if argv[:1] != ["local"] or "--" not in argv:
    sys.exit(2)
opts = argv[1:argv.index("--")]
def value(name):
    return opts[opts.index(name) + 1]
if not re.match(r"^[0-9a-f]{40}$", value("--sha")) or not value("--deadline-epoch").isdigit():
    sys.exit(2)
if not value("--fetch-url").startswith("https://github.com/") or not os.environ.get("CCTALLY_FETCH_TOKEN"):
    sys.exit(2)
def finish(rc):
    # As the real `local`: its own status goes to GITHUB_OUTPUT when set.
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write("exit=%%d\\n" %% rc)
    sys.exit(rc)
if %(busy)r:
    finish(int(value("--busy-exit")))
finish(subprocess.run(argv[argv.index("--") + 1:]).returncode)
"""


def _supervisor_env(tmp_path, *, busy, base):
    tool = tmp_path / "fake-cctally-runner"
    tool.write_text(_FAKE_SUPERVISOR % {"busy": busy})
    log = tmp_path / "supervisor.jsonl"
    env = dict(base)
    env.update({"CCTALLY_RUNNER_PYTHON": sys.executable, "CCTALLY_RUNNER_TOOL": str(tool),
                "FAKE_SUPERVISOR_LOG": str(log), "CCTALLY_FETCH_TOKEN": "t0ken",
                "GITHUB_SHA": "c" * 40, "GITHUB_REPOSITORY": PRIVATE})
    return env, log


def test_no_workflow_names_the_retired_reservation_helper():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert RETIRED_RESERVATION_HELPER not in path.read_text(), path.name


def _report_step(job):
    """The `if: always()` step after a job's supervisor step, which reports
    it from `steps.execute.outputs.exit` (Codex Q6 item 6)."""
    steps = _jobs("ci.yml")[job]["steps"]
    (at,) = [i for i, step in enumerate(steps) if step.get("id") == "execute"]
    report = steps[at + 1]
    assert report["if"] == "always()", report
    assert report["env"]["SUPERVISOR_EXIT"] == "${{ steps.execute.outputs.exit }}", report
    assert report["env"]["EXECUTE_OUTCOME"] == "${{ steps.execute.outcome }}", report
    return report


def _run_report(job, tmp_path, env, github_output, outcome="failure"):
    exit_line = [line for line in github_output.read_text().splitlines() if line.startswith("exit=")]
    env = dict(env, SUPERVISOR_EXIT=exit_line[-1].split("=", 1)[1] if exit_line else "",
               EXECUTE_OUTCOME=outcome)
    return subprocess.run(["bash", "-e", "-c", _report_step(job)["run"]], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)


def test_a_busy_host_reports_changed_checks_as_not_run(tmp_path):
    """Exit 75 is a busy host: the step fails with it, and the next step
    reports it as not run, never as a pass."""
    check = _step("ci.yml", "changed-checks", "Check the pushed range")
    summary = tmp_path / "summary.md"
    github_output = tmp_path / "github-output"
    github_output.write_text("")
    env, log = _supervisor_env(tmp_path, busy=True, base={
        "PATH": os.environ["PATH"], "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_OUTPUT": str(github_output),
        "GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "1", "BEFORE": "a", "AFTER": "b"})
    done = subprocess.run(["bash", "-e", "-c", check["run"]], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 75, (done.stdout, done.stderr)
    (call,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert call[call.index("--") + 1:] == ["python3", "bin/cctally-preflight", "--changed", "a..b"]
    reported = _run_report("changed-checks", tmp_path, env, github_output)
    assert reported.returncode == 0, (reported.stdout, reported.stderr)
    assert "not run" in summary.read_text()
    assert "::error title=changed-checks not run::" in reported.stdout, reported.stdout
    assert "gh run rerun 7 --failed" in reported.stdout, reported.stdout


@pytest.mark.parametrize("status,outcome", [("0", "success"), ("1", "failure"), ("3", "failure"),
                                            ("", "failure"), ("", "success")])
def test_the_report_step_says_nothing_of_a_busy_host_otherwise(tmp_path, status, outcome):
    for job in ("changed-checks", "dashboard-build-stability"):
        summary = tmp_path / ("%s.md" % job)
        done = subprocess.run(["bash", "-e", "-c", _report_step(job)["run"]], cwd=tmp_path,
                              env={"PATH": os.environ["PATH"], "GITHUB_STEP_SUMMARY": str(summary),
                                   "SUPERVISOR_EXIT": status, "EXECUTE_OUTCOME": outcome, "GITHUB_RUN_ID": "7"},
                              capture_output=True, text=True, timeout=60)
        text = (summary.read_text() if summary.exists() else "") + done.stdout
        assert "host was busy" not in text and "not run" not in text, (job, text)
        # Codex Q6 item 6: an execute step that ran and left no status is an
        # incomplete run, never busy or success, and fails this step (E2-8).
        assert done.returncode == (1 if status == "" else 0), (job, done.stdout, done.stderr)
        assert ("incomplete: no exit status" in text) == (status == ""), (job, status, text)


@pytest.mark.parametrize("outcome", ["skipped", "cancelled", ""])
def test_the_report_step_names_an_execute_step_that_did_not_complete(tmp_path, outcome):
    # E2-8: a skipped execute step (or a job cancelled before or during it)
    # is said so; it is not reported as a supervisor that answered nothing.
    for job in ("changed-checks", "dashboard-build-stability"):
        summary = tmp_path / ("%s.md" % job)
        done = subprocess.run(["bash", "-e", "-c", _report_step(job)["run"]], cwd=tmp_path,
                              env={"PATH": os.environ["PATH"], "GITHUB_STEP_SUMMARY": str(summary),
                                   "SUPERVISOR_EXIT": "", "EXECUTE_OUTCOME": outcome, "GITHUB_RUN_ID": "7"},
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, (job, done.stdout, done.stderr)
        text = (summary.read_text() if summary.exists() else "") + done.stdout
        assert "the execute step did not complete (%s)" % (outcome or "unknown") in text, (job, text)
        assert "no exit status" not in text and "host was busy" not in text, (job, text)


def test_dashboard_build_still_runs_when_changed_checks_fails(tmp_path):
    """`!cancelled()`: a failed pre-flight does not hide a bundle drift."""
    changed = ("dashboard/web/src/App.tsx",)
    outputs = _classified_outputs(tmp_path, changed)
    routed = _routed(_push(changed=changed), _context(outputs={"changed-checks": outputs}),
                     failed={"ci.yml:changed-checks"})
    assert routed == {"ci.yml:changed-checks": LAN, "ci.yml:dashboard-build-stability": LAN}


@pytest.mark.parametrize("busy,drift,status", [(True, False, 75), (False, False, 0), (False, True, 1)])
def test_dashboard_build_runs_under_the_runner_supervisor(tmp_path, busy, drift, status):
    """The build and the byte check run under the supervisor, which provisions
    node_modules itself; a busy host builds nothing and is reported as not run."""
    steps = [step for step in _jobs("ci.yml")["dashboard-build-stability"]["steps"] if " local --sha" in
             step.get("run", "")]
    assert len(steps) == 1, [step.get("name") for step in steps]
    assert all("actions/setup-node" not in str(step.get("uses", ""))
               for step in _jobs("ci.yml")["dashboard-build-stability"]["steps"])
    assert steps[0]["env"] == {"CCTALLY_FETCH_TOKEN": "${{ github.token }}"}
    assert "--wait 300 --busy-exit 75" in steps[0]["run"] and "npm ci" not in steps[0]["run"]
    repo, _before, _after = _repo_with_change(
        tmp_path, ("dashboard/web/package.json", "dashboard/static/index.html"))
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    npm_log = tmp_path / "npm.log"
    (fake / "npm").write_text(
        '#!/bin/sh\necho "$(pwd -P) $*" >> %s\n' % npm_log
        + ('[ "$*" = "run build" ] && echo drift > ../static/index.html\n' if drift else "")
        + "exit 0\n")
    (fake / "npm").chmod(0o755)
    summary = tmp_path / "summary.md"
    github_output = tmp_path / "github-output"
    github_output.write_text("")
    env, _log = _supervisor_env(tmp_path, busy=busy, base={
        "PATH": f"{fake}:{os.environ['PATH']}", "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_OUTPUT": str(github_output), "GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "1"})
    done = subprocess.run(["bash", "-e", "-c", steps[0]["run"]], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == status, (done.stdout, done.stderr)
    reported = _run_report("dashboard-build-stability", tmp_path, env, github_output,
                           "success" if status == 0 else "failure")
    assert reported.returncode == 0, (reported.stdout, reported.stderr)
    if busy:
        assert not npm_log.exists(), npm_log.read_text()
        assert "not run" in summary.read_text()
        assert "::error title=dashboard-build-stability not run::" in reported.stdout, reported.stdout
        assert "gh run rerun 7 --failed" in reported.stdout, reported.stdout
    else:
        web = (repo / "dashboard" / "web").resolve()
        assert npm_log.read_text().splitlines() == [f"{web} run build"]


def test_an_unreadable_diff_after_a_readable_before_counts_as_dashboard(tmp_path):
    step = _step("ci.yml", "changed-checks", "Classify the pushed paths")
    repo, before, _after = _repo_with_change(tmp_path, ("bin/other.py",))
    out = tmp_path / "github-output"
    env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out), "BEFORE": before, "AFTER": "b" * 40}
    done = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert out.read_text() == "dashboard=true\n"


def test_new_branch_push_with_zero_before_checks_the_pushed_tree(tmp_path):
    # The classification treats an unknown range as touching everything.
    assert _classified_outputs(tmp_path / "a", ("bin/cctally",), zero_before=True) == {"dashboard": "true"}
    # And the pre-flight checks the relevant files of the WHOLE pushed tree: a
    # defect the range itself never touched is still found.
    repo, before, after = _repo_with_change(tmp_path / "b", ("bin/new_ok.py",))
    (repo / "bin" / "old_broken.py").write_text("def broken(:\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a defect outside the next range")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "bin" / "later.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "later")
    head = _git(repo, "rev-parse", "HEAD")
    tool = [sys.executable, str(ROOT / "bin" / "cctally-preflight"), "--repo-root", str(repo)]
    ranged = subprocess.run(tool + ["--changed", f"{base}..{head}"],
                            capture_output=True, text=True, timeout=120)
    assert "old_broken.py" not in ranged.stderr, ranged.stderr
    for spec in (f"{ZERO}..{head}", f"..{head}"):
        whole = subprocess.run(tool + ["--changed", spec], capture_output=True, text=True, timeout=120)
        assert whole.returncode == 3, (spec, whole.stdout, whole.stderr)
        assert "old_broken.py" in whole.stderr, (spec, whole.stderr)


@pytest.mark.parametrize("before", [ZERO, "a" * 40])
def test_candidate_push_runs_only_release_verify(before):
    event = _push(ref="refs/heads/release-candidate/1.2.3", changed=("CHANGELOG.md", "package.json"),
                  before=before)
    # release-candidate.yml is mirror-private, so on the public tree no
    # workflow starts on a candidate push at all.
    expected = ({"release-candidate.yml:release-verify": LAN}
                if (WORKFLOWS / "release-candidate.yml").exists() else {})
    assert _routed(event, _context()) == expected


def test_no_private_schedule_or_full_suite_lane():
    ci = _workflow_docs()["ci.yml"]
    triggers = ci.get("on", ci.get(True))
    assert set(triggers) == {"push", "pull_request"}, triggers
    assert triggers["push"]["branches"] == ["main"]
    suite = re.compile(r"bin/cctally-(?:test-all|suite-run|broad-run)\b")
    for workflow, doc in _workflow_docs().items():
        for name, job in doc["jobs"].items():
            text = json.dumps(job.get("steps") or [])
            if not suite.search(text) or f"{workflow}:{name}" in ("release-candidate.yml:release-verify",
                                                                  "broad-run.yml:broad-run"):
                continue
            # Any other full-suite job runs only on the public mirror, on hosted Linux.
            assert "github.repository == 'omrikais/cctally'" in job.get("if", ""), (workflow, name)
            assert job["runs-on"] == "ubuntu-latest", (workflow, name)
    # broad-run.yml is mirror-private, so the public tree schedules only the pricing check.
    expected = {"pricing-freshness.yml:check": MAC}
    if (WORKFLOWS / "broad-run.yml").exists():
        expected["broad-run.yml:broad-run"] = LAN
    for event in ("schedule", "workflow_dispatch"):
        assert _routed({"event_name": event}, _context(event=event)) == expected, event
        # The public mirror runs neither on its own schedule.
        assert "broad-run.yml:broad-run" not in _routed({"event_name": event},
                                                        _context(repository=PUBLIC, event=event)), event


@pytest.mark.parametrize("changed", [("bin/cctally",), ("docs/guide.md",)])
def test_public_pr_runs_hosted_only(changed):
    routed = _routed(_pull_request(changed),
                     _context(repository=PUBLIC, event="pull_request", head_repo="contributor/cctally"))
    assert routed, routed
    assert all(route == HOSTED for route in routed.values()), routed
    if changed == ("bin/cctally",):
        assert routed == {"ci.yml:test-pr": HOSTED}
    else:
        assert routed == {"doc-lint.yml:doc-lint": HOSTED}
    # A private pull request, fork or not, runs nothing on either Mac.
    for head in (PRIVATE, "contributor/cctally-dev"):
        assert _routed(_pull_request(changed), _context(event="pull_request", head_repo=head)) == {}


def test_public_mirror_push_runs_nothing():
    assert _routed(_push(), _context(repository=PUBLIC)) == {}


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_private_pricing_check_uses_mac_and_public_stays_skipped(event):
    job = _jobs("pricing-freshness.yml")["check"]
    assert _route(job, _context(event=event)) == MAC
    assert _route(job, _context(repository=PUBLIC, event=event)) is None


# #940 Part C (C4). The workflows a runner service executes for this
# repository are mirrored, so they refer to the runner's storage only through
# the variable NAMES its listener exports, never to a host, an alias or an
# installed path. Built from fragments so this public file names none of them.
_RUNNER_WORKFLOWS = ("ci.yml", "pricing-freshness.yml", "doc-lint.yml")
_FORBIDDEN_WORKFLOW_TEXT = (
    ("an ssh alias", re.compile(r"\b[A-Za-z0-9.]+-(?:cmd|admin)\b")),
    ("a home directory", re.compile(r"/Users/")),
    ("a mounted volume", re.compile(r"/Volumes/")),
    ("an installed runner path",
     re.compile(r"\.local/(?:libexec|state)/cctally|\.config/cctally-runner")),
    ("a runner-home cache", re.compile(r"(?:\$HOME|~)/\.(?:cache|local)/cctally")),
)


@pytest.mark.parametrize("workflow", _RUNNER_WORKFLOWS)
def test_runner_workflows_name_no_alias_or_installed_path(workflow):
    text = (WORKFLOWS / workflow).read_text()
    found = [
        (label, match.group(0))
        for label, pattern in _FORBIDDEN_WORKFLOW_TEXT
        for match in pattern.finditer(text)
    ]
    assert not found, (workflow, found)


def test_pricing_payload_lives_in_the_job_temp_for_producer_and_consumer(tmp_path):
    check = _step("pricing-freshness.yml", "check",
                  "Run pricing-check (capture rc — exit 1 = drift, not failure)")
    summarize = _step("pricing-freshness.yml", "check", "Summarize the check")
    text = (WORKFLOWS / "pricing-freshness.yml").read_text()
    assert "/tmp/pricing.json" not in text
    assert '"${RUNNER_TEMP:?}/pricing.json"' in summarize["run"]
    # Execute the producer with the expensive command replaced: the payload
    # lands in the job's temp directory, and nowhere without one.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").write_text("#!/bin/sh\necho '{\"ok\": true}'\n")
    (fake_bin / "python3").chmod(0o755)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "GITHUB_OUTPUT": str(tmp_path / "out")}
    # The step prepends brew's Python to PATH; neutralise that one line so the
    # fake interpreter answers instead of a real, networked pricing check.
    run = check["run"].replace("export PATH=", "true ")
    done = subprocess.run(
        ["bash", "-e", "-c", run], env={**env, "RUNNER_TEMP": str(runner_temp)},
        capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert (runner_temp / "pricing.json").read_text() == '{"ok": true}\n'
    refused = subprocess.run(
        ["bash", "-e", "-c", run], env=env, capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0
    assert "RUNNER_TEMP" in refused.stderr


def test_doc_lint_takes_no_runner_admission():
    for step in _jobs("doc-lint.yml")["doc-lint"]["steps"]:
        command = str(step.get("run", ""))
        assert RETIRED_RESERVATION_HELPER not in command, step
        assert "CCTALLY_RUNNER_" not in command, step
