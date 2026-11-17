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
import posixpath
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


# #923: a persistent evidence path grows a self-hosted runner's disk without
# bound — lan's internal disk for a home path, the runner store for a path
# outside the job temp — until the disk fills or the supervisor's free-space
# refusal stops every run. Two fail-closed static checks over every self-hosted
# job (spec 2026-11-10-923-s1 §5). They read YAML and strings only and run no
# workflow command; they are no shell interpreter, so a form they do not
# recognize fails rather than being interpreted. Kept apart from the privacy
# check above, which covers only the mirrored runner workflows.


def _all_workflow_docs():
    """Every workflow, `.yml` and `.yaml` (`_workflow_docs` stays `.yml`-only)."""
    paths = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    return {path.name: yaml.safe_load(path.read_text()) for path in paths}


def _maybe_self_hosted(runs_on):
    """True when `runs-on` names `self-hosted` or is not a literal label: an
    expression (`${{ matrix.os }}`), a mapping (a runner group), or a list with
    a non-string or expression element may all select a self-hosted runner."""
    if runs_on is None:
        return False
    labels = [runs_on] if isinstance(runs_on, str) else runs_on if isinstance(runs_on, list) else None
    return labels is None or any(not isinstance(label, str) or label == "self-hosted" or "${{" in label
                                 for label in labels)


def _self_hosted_jobs(docs=None):
    """``(workflow file, job name, workflow doc, job)`` for every job whose
    `runs-on` may select a self-hosted runner (`_maybe_self_hosted`)."""
    docs = _all_workflow_docs() if docs is None else docs
    return [(workflow, name, doc, job)
            for workflow, doc in sorted(docs.items())
            for name, job in ((doc or {}).get("jobs") or {}).items()
            if isinstance(job, dict) and _maybe_self_hosted(job.get("runs-on"))]


def _step_label(index, step):
    return repr(str(step.get("name") or step.get("uses") or f"step {index + 1}"))


def _strings(mapping):
    return [(key, value) for key, value in (mapping or {}).items() if isinstance(value, str)]


def _text_units(workflow, job):
    """``(where, text, kind, step index)`` for every text the checks read:
    workflow and job `env` values, and each step's `run`, `env` and `with`
    values. ``kind`` is "run" for shell text and "yaml" otherwise."""
    units = [(f"workflow env {key}", value, "yaml", None) for key, value in _strings(workflow.get("env"))]
    units += [(f"job env {key}", value, "yaml", None) for key, value in _strings(job.get("env"))]
    for index, step in enumerate(job.get("steps") or []):
        label = _step_label(index, step)
        if isinstance(step.get("run"), str):
            units.append((f"step {label} run", step["run"], "run", index))
        for part in ("env", "with"):
            units += [(f"step {label} {part} {key}", value, "yaml", index)
                      for key, value in _strings(step.get(part))]
    return units


def _line_at(text, offset):
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return text[start:end if end >= 0 else len(text)].strip()


# 5.1: any HOME expansion (`$HOME`, `"${HOME}"`, `${HOME:-…}`); a `~` that
# starts a word (not after a word character, `~` or `/`) and is followed by
# `/`, whitespace, a quote or the end, except `=~ ` (the regex operator); or
# a literal `/Users/` path.
_HOME_PATH = re.compile(r"""\$\{?HOME\b|(?<![\w~/=])~(?=[/\s"']|\Z)|(?<==)~/|/Users/""")


def _home_path_findings(workflow, job):
    units = [(where, text) for where, text, _kind, _index in _text_units(workflow, job)]
    for scope, owner in (("workflow", workflow), ("job", job)):
        directory = (((owner.get("defaults") or {}).get("run")) or {}).get("working-directory")
        if isinstance(directory, str):
            units.append((f"{scope} defaults working-directory", directory))
    for index, step in enumerate(job.get("steps") or []):
        if isinstance(step.get("working-directory"), str):
            units.append((f"step {_step_label(index, step)} working-directory", step["working-directory"]))
    return [f"{where}: home path {match.group(0)!r} in {_line_at(text, match.start())!r}"
            for where, text in units
            for match in _HOME_PATH.finditer(text)]


# 5.2. The job-temp and run-directory roots and the opaque segment are
# placeholders no literal can spell, so only a recognized read produces them.
_JOB_TEMP, _RUN_DIR, _OPAQUE = "/\0job-temp", "/\0run-dir", "\0opaque"
_EVIDENCE_ROOT_NAMES = ("BROAD_OUT", "CCTALLY_SUITE_OUT", "CCTALLY_TEST_EVIDENCE_ROOT",
                        "CCTALLY_E2E_EVIDENCE_DIR", "CCTALLY_E2E_RUNTIME_DIR")
_ROOT_NAMES = {"RUNNER_TEMP": _JOB_TEMP, "CCTALLY_RUNNER_RUN_DIR": _RUN_DIR}
_OPAQUE_NAMES = ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")
_RUNNER_PROVIDED = {*_ROOT_NAMES, *_OPAQUE_NAMES, "GITHUB_ENV"}
_EXPRESSION_VALUES = {"runner.temp": _JOB_TEMP, "github.run_id": _OPAQUE, "github.run_attempt": _OPAQUE}
_ALLOWED_ACTION = re.compile(r"(?:actions/checkout|actions/upload-artifact)(?:@\S*)?")
# A whole option: `--export-failure` is not a sink.
_SINK = re.compile(r"(?<![\w-])--(export|out)(?=[\s=])")
_SINK_VALUE = re.compile(r"""[ \t]+(?:"([^"\\`]*)"|([^\s"'\\`;&|()<>]+))(?=\s|$|[;&|)'])""")
# A recognized supervisor invocation: `"${CCTALLY_RUNNER_TOOL:?…}" local` or
# `"$CCTALLY_RUNNER_TOOL" local`; group 1 is the exempt `local` subcommand.
_SUPERVISOR_INVOCATION = re.compile(
    r'"\$(?:\{CCTALLY_RUNNER_TOOL(?::\?[^}"]*)?\}|CCTALLY_RUNNER_TOOL)"[ \t]+(local)(?![\w-])')
_TEST_REMOTE_INVOCATION = re.compile(r"(?<![\w./-])(?:\./)?bin/cctally-test-remote(?![\w-])")
_SEPARATOR = re.compile(r"(?<=\s)--(?=\s|$)")
# The one recognized env-file write; group 1 is the name, 2 the value, 3 the file.
_ENV_FILE_WRITE = re.compile(
    r"""^[ \t]*printf '([A-Za-z_][A-Za-z0-9_]*)=%s\\n' "([^"\\`\n]*)" >> "\$(GITHUB_ENV)"[ \t]*$""",
    re.MULTILINE)
_NAME = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*(?![A-Za-z0-9_])")
_REFERENCE = re.compile(r"\$\{\{\s*env\.([A-Za-z_]\w*)\s*\}\}|\$\{?([A-Za-z_][A-Za-z0-9_]*)")
# Commands that can change a variable without its name in the text. A word is
# bounded by characters other than letters, digits, `_` and `-`.
_MUTATION_WORD = re.compile(
    r"(?<![A-Za-z0-9_-])(?:eval|source|declare|typeset|export|readonly|local|read|mapfile|readarray"
    r"|printf[ \t]+-v)(?![A-Za-z0-9_-])")
# A lone `.` word at a command position: line start, after `;&|({`, or after
# a word that starts a command (`then`, `do`, `else`, `elif`, `!`, `time`).
_DOT_COMMAND = re.compile(
    r"(?:^|[;&|({]|(?<![A-Za-z0-9_-])(?:then|do|else|elif|time|!)(?=[ \t]))[ \t]*\.(?=[ \t]|$)")
# The parts of a value the resolver recognizes; any other `$` is unresolved.
_VALUE_PART = re.compile(
    r"\$\{\{\s*([^}]*?)\s*\}\}|\$\{([A-Za-z_][A-Za-z0-9_]*)(?::\?[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)|\$")
_SAFE_LITERAL = re.compile(r"[A-Za-z0-9_./=:@%+,-]*")


def _is_read(text, start, end):
    before, after = text[:start], text[end:]
    if before.endswith("$") and not before.endswith(("\\$", "$$")):
        return True
    if (before.endswith("${") and not before.endswith("\\${")
            and re.match(r"\}|:[-?+][^}]*\}", after)):
        return True
    return bool(re.search(r"\$\{\{\s*env\.$", before) and re.match(r"\s*\}\}", after))


class _Evidence:
    """One self-hosted job's evidence check (spec 5.2)."""

    def __init__(self, workflow, job):
        self.steps = job.get("steps") or []
        self.units = _text_units(workflow, job)
        self.findings = []
        self.census = {"sinks": 0, "names": 0}
        # name -> [(raw value, shell?, first step it applies to, last, where)]
        self.candidates = {}
        last = len(self.steps) - 1
        for scope, mapping in (("workflow env", workflow.get("env")), ("job env", job.get("env"))):
            for key, value in (mapping or {}).items():
                self._candidate(key, str(value), False, 0, last, f"{scope} {key}")
        for index, step in enumerate(self.steps):
            label = _step_label(index, step)
            for key, value in (step.get("env") or {}).items():
                self._candidate(key, str(value), False, index, index, f"step {label} env {key}")
            run = step.get("run")
            if isinstance(run, str):
                for match in _ENV_FILE_WRITE.finditer(run):
                    # Applies from the next step on; the value is evaluated here.
                    self._candidate(match.group(1), match.group(2), True, index + 1, last,
                                    f"step {label} run", context=index)

    def _candidate(self, name, value, shell, first, last, where, context=None):
        if name in _RUNNER_PROVIDED:
            self.findings.append(f"{where}: redefines the runner-provided {name}")
        self.candidates.setdefault(name, []).append(
            (value, shell, first, last, where, first if context is None else context))

    def values_at(self, name, index):
        return [c for c in self.candidates.get(name, []) if c[2] <= index <= c[3]]

    def resolve(self, value, index, shell, depth=0):
        """Every value ``value`` may take at step ``index``, or None when any
        part of it is unresolved."""
        if depth > 8:
            return None
        results, pos = [""], 0
        for match in _VALUE_PART.finditer(value):
            literal = value[pos:match.start()]
            if not _SAFE_LITERAL.fullmatch(literal):
                return None
            pos = match.end()
            expression, name = match.group(1), match.group(2) or match.group(3)
            if expression is not None:
                if expression in _EXPRESSION_VALUES:
                    options = [_EXPRESSION_VALUES[expression]]
                elif re.fullmatch(r"env\.[A-Za-z_]\w*", expression):
                    # The `env` context holds only workflow, job, step and
                    # env-file values: `${{ env.RUNNER_TEMP }}` evaluates empty.
                    if expression[4:] in _ROOT_NAMES or expression[4:] in _OPAQUE_NAMES:
                        return None
                    options = self._name_values(expression[4:], index, depth)
                else:
                    return None
            elif name is not None and shell:
                options = self._name_values(name, index, depth)
            else:
                return None
            if options is None:
                return None
            results = [done + literal + option for done in results for option in options]
        tail = value[pos:]
        if not _SAFE_LITERAL.fullmatch(tail):
            return None
        return [done + tail for done in results]

    def _name_values(self, name, index, depth):
        if name in _ROOT_NAMES:
            return [_ROOT_NAMES[name]]
        if name in _OPAQUE_NAMES:
            return [_OPAQUE]
        found = self.values_at(name, index)
        if not found:
            return None
        options = []
        for value, shell, _first, _last, _where, context in found:
            resolved = self.resolve(value, context, shell, depth + 1)
            if resolved is None:
                return None
            options += resolved
        return options

    def run(self):
        self.sink_steps = set()
        sinks = self._sinks()
        self._evidence_roots()
        watched = self._watched(sinks)
        for where, text, kind, _index in self.units:
            self._name_census(where, text, watched, kind == "run")
        for index, step in enumerate(self.steps):
            label = _step_label(index, step)
            if "uses" in step and not _ALLOWED_ACTION.fullmatch(str(step["uses"])):
                self.findings.append(f"step {label}: action {step['uses']!r} is not on the allowlist")
            run = step.get("run")
            if isinstance(run, str) and (index in self.sink_steps or _ENV_FILE_WRITE.search(run)):
                self._mutation_words(label, run)
        return self.findings, self.census

    def _sinks(self):
        """Check every sink occurrence; return ``(step index, value)`` pairs."""
        sinks = []
        for where, text, kind, index in self.units:
            commands = _commands(text) if kind == "run" else [text]
            for command in commands:
                invocation = (_SUPERVISOR_INVOCATION.search(command)
                              or _TEST_REMOTE_INVOCATION.search(command))
                separator = None
                if invocation:
                    found = _SEPARATOR.search(command, invocation.end())
                    separator = found.start() if found else len(command)
                for match in _SINK.finditer(command):
                    self.census["sinks"] += 1
                    if kind == "run":
                        self.sink_steps.add(index)
                    option = match.group(1)
                    argument = invocation and invocation.end() <= match.start() < separator
                    payload = invocation and match.start() >= separator
                    value = _SINK_VALUE.match(command, match.end())
                    shown = command[match.start():match.start() + 120]
                    if not value or (option == "export" and not argument):
                        self.findings.append(f"{where}: unrecognized sink {shown!r}")
                        continue
                    value = value.group(1) if value.group(1) is not None else value.group(2)
                    sinks.append((index, value))
                    self._sink(where, option, value, index, kind == "run", bool(payload), shown)
        return sinks

    def _sink(self, where, option, value, index, shell, payload, shown):
        if option == "export":
            source, equals, dest = value.partition("=")
            if not equals:
                self.findings.append(f"{where}: export without SRC=DEST {shown!r}")
                return
            sources = self.resolve(source, index, shell)
            if sources is None or not all(map(self._contained, sources)):
                self.findings.append(f"{where}: export source is not relative and contained {shown!r}")
            value = dest
        resolved = self.resolve(value, index, shell)
        if resolved is None:
            self.findings.append(f"{where}: unresolved sink value {shown!r}")
        elif payload:
            if not all(self._beneath(path, _RUN_DIR) or self._contained(path) for path in resolved):
                self.findings.append(f"{where}: payload --out outside the run directory {shown!r}")
        elif not all(self._beneath(path, _JOB_TEMP) for path in resolved):
            self.findings.append(f"{where}: --{option} outside the job temp {shown!r}")

    @staticmethod
    def _beneath(path, root):
        return posixpath.normpath(path).startswith(root + "/")

    @staticmethod
    def _contained(path):
        normal = posixpath.normpath(path)
        return not (normal.startswith("/") or normal == ".." or normal.startswith("../")
                    or _JOB_TEMP[1:] in normal or _RUN_DIR[1:] in normal)

    def _evidence_roots(self):
        for name in _EVIDENCE_ROOT_NAMES:
            for value, shell, _first, _last, where, context in self.candidates.get(name, []):
                resolved = self.resolve(value, context, shell)
                if resolved is None or not all(self._beneath(path, _JOB_TEMP) for path in resolved):
                    self.findings.append(f"{where}: {name} is not beneath the job temp ({value!r})")

    def _watched(self, sinks):
        """The watched names: the fixed ones, then the closure over every name a
        sink value or a watched name's candidate value references."""
        watched = {*_EVIDENCE_ROOT_NAMES, *_RUNNER_PROVIDED}
        values = [value for _index, value in sinks]
        while True:
            values += [candidate[0] for name in watched for candidate in self.candidates.get(name, [])]
            found = {match.group(1) or match.group(2) for value in values for match in _REFERENCE.finditer(value)}
            if found <= watched:
                return watched
            watched |= found

    def _name_census(self, where, text, watched, shell):
        claimed = set()
        for match in _ENV_FILE_WRITE.finditer(text):
            claimed |= {match.span(1), match.span(3)}
        for match in _NAME.finditer(text):
            name = match.group(0)
            if name not in watched:
                continue
            # A full-line shell comment, as the mutation rule skips.
            if shell and text[text.rfind("\n", 0, match.start()) + 1:].lstrip(" \t").startswith("#"):
                continue
            if match.span() in claimed or (name != "GITHUB_ENV" and _is_read(text, *match.span())):
                self.census["names"] += 1
                continue
            self.findings.append(
                f"{where}: {name} outside a read or a recognized write in {_line_at(text, match.start())!r}")

    def _mutation_words(self, label, run):
        exempt = {match.span(1) for match in _SUPERVISOR_INVOCATION.finditer(run)}
        offset = 0
        for line in run.splitlines(keepends=True):
            if not line.lstrip().startswith("#"):
                for pattern in (_MUTATION_WORD, _DOT_COMMAND):
                    for match in pattern.finditer(line.rstrip("\n")):
                        span = (offset + match.start(), offset + match.end())
                        if pattern is _MUTATION_WORD and span in exempt:
                            continue
                        self.findings.append(
                            f"step {label} run: {match.group(0).strip()!r} can change a variable"
                            f" without naming it, in {line.strip()!r}")
            offset += len(line)


def _evidence_findings(workflow, job):
    """Spec 5.2's findings for one self-hosted job, and its census counts."""
    return _Evidence(workflow, job).run()


def test_self_hosted_jobs_name_no_runner_home_path():
    """A persistent evidence path grows a self-hosted runner's disk without
    bound: a runner-home path fills lan's internal disk (#923)."""
    jobs = _self_hosted_jobs()
    assert jobs, "no self-hosted job found; the check would be vacuous"
    found = {(workflow, name): _home_path_findings(doc, job) for workflow, name, doc, job in jobs}
    assert not any(found.values()), found


def test_self_hosted_evidence_and_exports_stay_under_runner_temp():
    """A persistent evidence path grows a self-hosted runner's disk without
    bound: an export or evidence root outside the job temp fills the runner
    store until the supervisor's free-space refusal stops every run (#923)."""
    # Both exporting workflows are mirror-private (.githooks/_match.py), so the
    # public tree expects none; every self-hosted job there must still pass.
    expected = {(workflow, name) for workflow, name in (("broad-run.yml", "broad-run"),
                                                        ("release-candidate.yml", "release-verify"))
                if (WORKFLOWS / workflow).exists()}
    exporting = []
    for workflow, name, doc, job in _self_hosted_jobs():
        findings, census = _evidence_findings(doc, job)
        assert not findings, (workflow, name, findings)
        runs = [step["run"] for step in job.get("steps") or [] if isinstance(step.get("run"), str)]
        if any(_SUPERVISOR_INVOCATION.search(run) and re.search(r"--export\s", run) for run in runs):
            exporting.append((workflow, name))
            # Non-vacuity from the raw census, not from parser matches.
            assert census["sinks"] > 0 and census["names"] > 0, (workflow, name, census)
    assert set(exporting) == expected, (exporting, expected)


_SAFE_WRITE = """printf 'BROAD_OUT=%s\\n' "$RUNNER_TEMP/broad-run-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT" >> "$GITHUB_ENV"\n"""
_LOCAL = ('exec "${CCTALLY_RUNNER_PYTHON:?unset}" -I -B "${CCTALLY_RUNNER_TOOL:?unset}" local --sha "$GITHUB_SHA" \\\n'
          '  --export %s \\\n  -- true\n')
_SAFE_EXPORT = _LOCAL % '"out/broad=$BROAD_OUT"'


def _synthetic(*steps, runs_on=("self-hosted", "macOS")):
    """A one-job self-hosted workflow: a safe BROAD_OUT write, then ``steps``
    (each a run text, or a step mapping), every one named ``control``."""
    named = [step if isinstance(step, dict) else {"name": "control", "run": step} for step in steps]
    job = {"runs-on": list(runs_on), "steps": [{"uses": "actions/checkout@v5"},
                                               {"name": "setup", "run": _SAFE_WRITE}, *named]}
    return {"synthetic.yml": {"jobs": {"job": job}}}


def _non_literal_runs_on(runs_on):
    """A `.yaml` job whose `runs-on` names no literal label, with an unsafe export."""
    doc = _synthetic({"name": "control", "run": _LOCAL % '"out/x=/Volumes/x/persistent/x"'})["synthetic.yml"]
    doc["jobs"]["job"]["runs-on"] = runs_on
    return {"synthetic.yaml": doc}


def _real_broad_run_with_an_unsafe_export():
    path = WORKFLOWS / "broad-run.yml"
    if not path.exists():
        pytest.skip("broad-run.yml is not in this tree")
    doc = yaml.safe_load(path.read_text())
    step = next(s for s in doc["jobs"]["broad-run"]["steps"]
                if _SUPERVISOR_INVOCATION.search(str(s.get("run", ""))))
    invocation = _SUPERVISOR_INVOCATION.search(step["run"])
    unsafe, count = re.subn(r"[ \t]--(?=[ \t\n])", r' --export "out/broad=/Volumes/x/persistent/x" --',
                            step["run"][invocation.end():], count=1)
    assert count == 1, step["run"]
    step["run"] = step["run"][:invocation.end()] + unsafe
    return {"broad-run.yml": doc}, step["name"]


_EVIDENCE_CONTROLS = [
    ("export to home", lambda: _synthetic(_LOCAL % '"out/x=$HOME/x"')),
    ("export escaping the job temp", lambda: _synthetic(_LOCAL % '"out/x=$RUNNER_TEMP/../x"')),
    ("export through an unknown variable", lambda: _synthetic(_LOCAL % '"out/x=$SOMEWHERE/x"')),
    ("export with an absolute source", lambda: _synthetic(_LOCAL % '"/etc=$BROAD_OUT"')),
    ("export with a source outside the run", lambda: _synthetic(_LOCAL % '"../x=$BROAD_OUT"')),
    ("evidence root written outside the job temp", lambda: _synthetic(
        """printf 'BROAD_OUT=%s\\n' "/Volumes/x/persistent/$GITHUB_RUN_ID" >> "$GITHUB_ENV"\n""",
        {"name": "execute", "run": _SAFE_EXPORT})),
    ("plain reassignment before the export", lambda: _synthetic(
        "BROAD_OUT=/Volumes/x/$GITHUB_RUN_ID\n" + _SAFE_EXPORT)),
    ("read before the export", lambda: _synthetic(
        "read -r BROAD_OUT <<< '/Volumes/x/persistent/x'\n" + _SAFE_EXPORT)),
    ("runner temp reassigned", lambda: _synthetic("RUNNER_TEMP=/Volumes/x\ntrue\n")),
    ("echo to the env file", lambda: _synthetic('echo "BROAD_OUT=$RUNNER_TEMP/x" >> "$GITHUB_ENV"\n')),
    ("a mention in a string", lambda: _synthetic('echo "::warning::BROAD_OUT=${BROAD_OUT}"\n')),
    ("cat to the env file", lambda: _synthetic('cat evidence.env >> "$GITHUB_ENV"\n')),
    ("tee to the env file", lambda: _synthetic("""printf 'X\\n' | tee -a "$GITHUB_ENV"\n""")),
    ("source", lambda: _synthetic("source ./env.sh\n" + _SAFE_EXPORT)),
    ("dot", lambda: _synthetic(". ./env.sh\n" + _SAFE_EXPORT)),
    ("dot after then", lambda: _synthetic("if [ -f env.sh ]; then . ./env.sh; fi\n" + _SAFE_EXPORT)),
    ("dot after do", lambda: _synthetic('for f in env.sh; do . "./$f"; done\n' + _SAFE_EXPORT)),
    ("export through env.RUNNER_TEMP, which GitHub evaluates empty",
     lambda: _synthetic(_LOCAL % '"out/x=${{ env.RUNNER_TEMP }}/x"')),
    ("matrix runs-on", lambda: _non_literal_runs_on("${{ matrix.os }}")),
    ("mapping runs-on", lambda: _non_literal_runs_on({"group": "large"})),
    ("runs-on list with an expression", lambda: _non_literal_runs_on(["macOS", "${{ matrix.label }}"])),
    ("eval", lambda: _synthetic('eval "$x"\n' + _SAFE_EXPORT)),
    ("declare", lambda: _synthetic('declare "$n=/Volumes/x"\n' + _SAFE_EXPORT)),
    ("unlisted action", lambda: _synthetic({"name": "control", "uses": "some-org/some-action@v1"})),
    ("test-remote export outside the job temp", lambda: _synthetic(
        "bin/cctally-test-remote --export out/x=/Volumes/x -- true\n")),
    ("real broad-run with an unsafe export", _real_broad_run_with_an_unsafe_export),
]


@pytest.mark.parametrize("make", [make for _label, make in _EVIDENCE_CONTROLS],
                         ids=[label for label, _make in _EVIDENCE_CONTROLS])
def test_the_evidence_check_refuses_each_unsafe_form(make):
    """A persistent evidence path grows a self-hosted runner's disk without
    bound: each form here would let an export or evidence root leave the job
    temp unseen (#923)."""
    made = make()
    docs, step = made if isinstance(made, tuple) else (made, "control")
    jobs = _self_hosted_jobs(docs)
    assert jobs, docs
    findings = [finding for _w, _n, doc, job in jobs for finding in _evidence_findings(doc, job)[0]]
    assert any(repr(step) in finding for finding in findings), findings


@pytest.mark.parametrize("run", [_SAFE_EXPORT, "# BROAD_OUT is set by setup\n" + _SAFE_EXPORT],
                         ids=["base", "a full-line comment naming BROAD_OUT"])
def test_the_evidence_check_accepts_the_safe_synthetic_base(run):
    """The negative controls differ from this base only in their unsafe form."""
    jobs = _self_hosted_jobs(_synthetic({"name": "execute", "run": run}))
    assert jobs
    for _w, _n, doc, job in jobs:
        findings, census = _evidence_findings(doc, job)
        assert not findings and census["sinks"] > 0, (findings, census)


def _home_job(text=None, *, job_directory=None, step_directory=None):
    step = {"name": "control", "run": "true" if text is None else text}
    job = {"runs-on": "self-hosted", "steps": [step]}
    if job_directory is not None:
        job["defaults"] = {"run": {"working-directory": job_directory}}
    if step_directory is not None:
        step["working-directory"] = step_directory
    return job


@pytest.mark.parametrize("job,refused", [
    (_home_job("cp a $HOME/x"), True), (_home_job("cp a ${HOME}/x"), True), (_home_job("cp a ~/x"), True),
    (_home_job('cp a "$HOME"/.cache/x'), True), (_home_job('cp a "${HOME}"/.cache/x'), True),
    (_home_job("cp a ${HOME:-/tmp}/x"), True), (_home_job('cd "$HOME" && cp -r out .cache/x'), True),
    (_home_job("cd ~"), True), (_home_job("cd ~ && cp -r out .cache/x"), True), (_home_job('cp a "~/x"'), True),
    (_home_job("cp a /Users/runner/.cache/x"), True), (_home_job("D=~/x; cp a $D"), True),
    (_home_job('[[ "$r" =~ ^a ]]'), False),
    (_home_job(step_directory="~/work"), True), (_home_job(step_directory="$HOME/work"), True),
    (_home_job(job_directory="/Users/runner/work"), True),
    (_home_job("cp a x~/y"), False), (_home_job("cp a a/~/b"), False), (_home_job("echo $HOMEBREW_PREFIX"), False),
    (_home_job(job_directory="dashboard/web", step_directory="dashboard/web"), False),
])
def test_the_home_path_check_refuses_each_home_form(job, refused):
    """A persistent evidence path grows a self-hosted runner's disk without
    bound: a runner-home path fills lan's internal disk (#923)."""
    jobs = _self_hosted_jobs({"h.yaml": {"jobs": {"job": job}}})
    assert len(jobs) == 1
    findings = _home_path_findings(jobs[0][2], jobs[0][3])
    assert bool(findings) is refused, findings
    where = "job defaults" if "defaults" in job else "'control'"
    assert all(where in finding for finding in findings), findings
