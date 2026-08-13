"""#529 S7 E2 — the vitest estate has one declared execution per push.

Named TOPOLOGY, not executions: a static test cannot prove a runtime
execution completed. What it proves is that the declared graph — parsed
workflow nodes times lane admission, plus the aggregator's own resolved
plan — contains exactly one unscoped estate invocation per admitted event.

Text scanning is deliberately avoided. `bin/cctally-frontend-test` carries
comment references to unscoped vitest (line 28) and a SCOPED invocation
whose file argument sits on a continuation line (line 205). A scan would
count the first and, once leg 2 is deleted, misclassify the second.
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_YML = ROOT / ".github/workflows/ci.yml"
MATRIX_YML = ROOT / ".github/workflows/ci-linux-matrix.yml"
FRONTEND = ROOT / "bin/cctally-frontend-test"
AGGREGATOR = "bin/cctally-test-all"

# Every admitted event, and the estate count its declared topology must show.
# Zero is correct and deliberate in four of these; a flat "exactly one"
# assertion would redden a correct workflow.
EXPECTED = {
    "private-push-nonstamp": 1,
    "private-push-stamp": 0,
    "markdown-only-push": 0,
    "private-same-repo-pr": 1,
    "private-fork-pr": 0,
    "public-mirror-pr": 1,
    "public-mirror-push": 0,
    "tag-or-cron-or-dispatch": 3,
}


def _strip_line_comment(line: str) -> str:
    """Drop a trailing `#` comment, tracking quote state as the shell does.

    A negative lookahead for "a quote appears somewhere after the `#`" was
    tried first and suppresses far more than it should: it leaves
    `echo hi  # "note": npx vitest run` intact, so a comment reads as a
    command. Scanning quote state is the only way to tell a comment
    introducer from a `#` inside a string, and it is a dozen lines.
    """
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line


def _strip_shell_comments(script: str) -> str:
    """Remove whole-line and trailing `#` comments, then join continuations.

    A continued command must be reassembled BEFORE classification, or the
    scoped reader-golden invocation reads as unscoped once its argument
    line is separated from it.
    """
    out = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(_strip_line_comment(line))
    joined = "\n".join(out)
    return re.sub(r"\\\n\s*", " ", joined)


def _unscoped_vitest_commands(script: str) -> list[str]:
    """`npx vitest run` invocations with no file argument.

    `--maxWorkers=N` is a flag, not a file, so the bundle's capped leg 2
    still counts as unscoped. A trailing path argument does not.

    `>` terminates the command for the same reason `;`, `)`, `&` and `|` do:
    without it, `npx vitest run > log` reads its redirection target as a file
    argument, classifies an unscoped run as scoped, and drops it from the
    total. Both live invocations sit inside `( … )` today, so the omission
    changed no current count — it would have undercounted the first redirected
    one written.
    """
    found = []
    for raw in re.findall(r"npx vitest run[^\n;)&|>]*", _strip_shell_comments(script)):
        args = raw.split()[3:]
        positional = [a for a in args if not a.startswith("-")]
        if not positional:
            found.append(raw.strip())
    return found


def test_the_frontend_harness_has_exactly_one_unscoped_estate_command():
    found = _unscoped_vitest_commands(FRONTEND.read_text())
    assert len(found) == 1, found


def test_the_scoped_reader_golden_command_is_not_counted():
    """Non-vacuity for the parser itself: the scoped invocation exists in
    this file, and the classifier above must not have counted it."""
    text = FRONTEND.read_text()
    assert text.count("npx vitest run") >= 2, "the fixture for this test is gone"
    assert len(_unscoped_vitest_commands(text)) == 1


def test_comments_mentioning_vitest_are_not_counted():
    salted = FRONTEND.read_text() + "\n# npx vitest run\n"
    assert len(_unscoped_vitest_commands(salted)) == 1


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        ("npx vitest run > log", 1),
        ("npx vitest run >log 2>&1", 1),
        ("npx vitest run src/one.test.ts > log", 0),
        ("npx vitest run --maxWorkers=2 > log", 1),
    ),
)
def test_a_redirected_estate_command_is_still_unscoped(command, expected):
    """A redirection is not a file argument. Reading it as one classifies an
    unscoped run as scoped, which removes it from the total — the one direction
    this count must never fail in. Neither live invocation is redirected, so
    nothing else here can observe the difference."""
    assert len(_unscoped_vitest_commands(command)) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    (
        ('echo hi  # "note": npx vitest run', "echo hi"),
        ("echo hi # npx vitest run", "echo hi"),
        ("echo '# not a comment'", "echo '# not a comment'"),
        ('echo "a # b" # tail', 'echo "a # b"'),
        ("echo no-comment-here", "echo no-comment-here"),
        ("echo a#b", "echo a#b"),
    ),
)
def test_the_comment_stripper_reads_quote_state(line, expected):
    """The first case is the one the earlier lookahead got wrong: a quote
    anywhere after the `#` suppressed stripping entirely, so a comment
    mentioning an unscoped vitest run survived and counted as a command."""
    assert _strip_line_comment(line) == expected


def _workflow_run_steps(path: Path) -> dict[str, list[str]]:
    doc = yaml.safe_load(path.read_text())
    return {
        name: [s["run"] for s in (body.get("steps") or []) if isinstance(s.get("run"), str)]
        for name, body in doc["jobs"].items()
    }


def test_no_workflow_job_runs_the_estate_directly():
    """After E2 the ONLY unscoped estate invocation is inside the bundle."""
    offenders = {}
    for path in (CI_YML, MATRIX_YML):
        for job, runs in _workflow_run_steps(path).items():
            hits = [c for run in runs for c in _unscoped_vitest_commands(run)]
            if hits:
                offenders[f"{path.name}:{job}"] = hits
    assert offenders == {}, offenders


def _resolved_plan() -> dict[str, str]:
    """`CCTALLY_TEST_ALL_PLAN=1` emits `key=value` lines and exits before any
    side effect (`bin/cctally-test-all:336-358`). The harness list arrives on
    one `harnesses=` line, space separated, in on-disk execution order."""
    result = subprocess.run(
        ["bash", AGGREGATOR],
        cwd=str(ROOT), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "CCTALLY_TEST_ALL_PLAN": "1", "HOME": str(ROOT)},
    )
    assert result.returncode == 0, result.stderr
    plan = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        plan[key] = value
    return plan


def test_the_aggregator_plan_contains_the_frontend_harness_exactly_once():
    plan = _resolved_plan()
    names = plan.get("harnesses", "").split()
    assert names, plan
    assert names.count("frontend") == 1, names


def _jobs_running_the_bundle(path: Path) -> set[str]:
    return {
        job for job, runs in _workflow_run_steps(path).items()
        if any(AGGREGATOR in _strip_shell_comments(run) for run in runs)
    }


def test_the_bundle_lanes_are_exactly_the_expected_three():
    assert _jobs_running_the_bundle(CI_YML) == {"test-macos", "test-pr"}
    assert _jobs_running_the_bundle(MATRIX_YML) == {"test-linux"}


def test_the_matrix_lane_declares_three_python_legs():
    doc = yaml.safe_load(MATRIX_YML.read_text())
    versions = doc["jobs"]["test-linux"]["strategy"]["matrix"]["python-version"]
    assert len(versions) == EXPECTED["tag-or-cron-or-dispatch"], versions


# ------------------------------------------------------ the multiplication
#
# Everything above establishes the FACTORS: how many unscoped estate commands
# one frontend harness carries, how many times the resolved plan runs that
# harness, which jobs invoke the bundle, and how many matrix legs each job has.
# What follows multiplies those factors by LANE ADMISSION — each job's parsed
# `if:` and `needs:` evaluated against a declared event context — and compares
# the product against the D2 table.
#
# Admission that cannot be decided from the declared context raises
# `Undecidable`. It is never assumed either way: assuming admitted would
# overcount a suppressed lane and assuming skipped would let a lane that runs
# the estate twice report zero, and both replace one false green with another.


class Undecidable(Exception):
    """A workflow construct this evaluator cannot decide from the context.

    Raised rather than defaulted, and it names the construct. Every unknown
    operator, function, context path and trigger form lands here.
    """


class _Absent:
    """A context path that this event genuinely does not carry.

    GitHub evaluates a property of a missing object to null rather than
    erroring — `github.event.pull_request.head.repo.full_name` on a push is
    null, and comparing it to a string is false, not a failure. Modelling
    that needs a value distinct from every string, because two absent paths
    must not compare equal to each other either.

    This is DECLARED per event, never a fallback. An undeclared path still
    raises `Undecidable`, so the fail-loud property survives: saying a field
    is absent is a statement about the event, and this is how it is made.
    """

    def __eq__(self, other):
        return other is self

    def __hash__(self):
        return id(self)

    def __repr__(self):
        return "<absent>"

    def __bool__(self):
        return False


ABSENT = _Absent()


_TOKENS = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')"
    r"|(?P<op>&&|\|\||==|!=|!|\(|\))"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*))"
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(expr):
        match = _TOKENS.match(expr, pos)
        if match is None:
            raise Undecidable(f"unparsable at {expr[pos:pos + 40]!r} in {expr!r}")
        pos = match.end()
        for kind in ("str", "op", "name"):
            if match.group(kind) is not None:
                tokens.append((kind, match.group(kind)))
                break
    return tokens


class _Evaluator:
    """Recursive descent over the GitHub Actions expression subset in use.

    Deliberately narrow. `success()`, `failure()`, comparison against a
    number, `contains()` and every other construct this repository's
    workflows do not use today raise `Undecidable`, so the first workflow
    that adopts one fails this test loudly instead of being guessed at.
    """

    def __init__(self, tokens, context):
        self.tokens, self.pos, self.context = tokens, 0, context

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def _take(self):
        token = self._peek()
        self.pos += 1
        return token

    def parse(self):
        value = self._or()
        if self.pos != len(self.tokens):
            raise Undecidable(f"trailing tokens from {self.tokens[self.pos:]!r}")
        if not isinstance(value, bool):
            raise Undecidable(f"expression is not a boolean: {value!r}")
        return value

    def _or(self):
        value = self._and()
        while self._peek() == ("op", "||"):
            self._take()
            # BOTH operands are bound and coerced before the connective runs.
            # Written as `self._as_bool(right) or self._as_bool(value)`, Python's
            # own short-circuit skips the left coercion whenever the right one
            # answers True, so `s || T` with a string on the left ANSWERS where
            # `T || s`, `s && T` and `T && s` all refuse. The answer is not
            # wrong; the refusal is what must not depend on operand order.
            right = self._and()
            left, right = self._as_bool(value), self._as_bool(right)
            value = left or right
        return value

    def _and(self):
        value = self._compare()
        while self._peek() == ("op", "&&"):
            self._take()
            right = self._compare()
            left, right = self._as_bool(value), self._as_bool(right)
            value = left and right
        return value

    def _compare(self):
        left = self._unary()
        kind, text = self._peek()
        if kind == "op" and text in ("==", "!="):
            self._take()
            right = self._unary()
            return (left == right) if text == "==" else (left != right)
        return left

    def _unary(self):
        if self._peek() == ("op", "!"):
            self._take()
            return not self._as_bool(self._unary())
        return self._primary()

    def _primary(self):
        kind, text = self._take()
        if kind == "op" and text == "(":
            value = self._or()
            if self._take() != ("op", ")"):
                raise Undecidable("unbalanced parentheses")
            return value
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "name":
            if self._peek() == ("op", "("):
                self._take()
                if self._take() != ("op", ")"):
                    raise Undecidable(f"{text}() takes no arguments here")
                if text != "cancelled":
                    raise Undecidable(
                        f"status function {text}() is not modelled; decide it "
                        "explicitly before relying on this count")
                return self._lookup("cancelled()")
            return self._lookup(text)
        raise Undecidable(f"unexpected token {(kind, text)!r}")

    def _lookup(self, path):
        if path not in self.context:
            raise Undecidable(
                f"context {path!r} is not declared for this event; the count "
                "cannot be derived without it")
        return self.context[path]

    @staticmethod
    def _as_bool(value):
        if isinstance(value, bool):
            return value
        if value is ABSENT:
            return False
        raise Undecidable(f"value {value!r} used as a boolean")


def _evaluate(expr: str, context: dict) -> bool:
    return _Evaluator(_tokenize(expr), context).parse()


def _glob_to_regex(pattern: str):
    """The subset of GitHub filter-pattern syntax the workflows use."""
    if set(pattern) & set("[]+!"):
        raise Undecidable(f"filter pattern {pattern!r} uses unmodelled syntax")
    out, index = "", 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out, index = out + "(?:.*/)?", index + 3
        elif pattern.startswith("**", index):
            out, index = out + ".*", index + 2
        elif pattern[index] == "*":
            out, index = out + "[^/]*", index + 1
        elif pattern[index] == "?":
            out, index = out + "[^/]", index + 1
        else:
            out, index = out + re.escape(pattern[index]), index + 1
    return re.compile(f"^{out}$")


def _workflow_starts(doc: dict, event: dict) -> bool:
    """Whether the event starts this workflow at all, from its own `on:` block.

    `markdown-only-push` is decided HERE and not by any job condition: it is
    the `paths-ignore` filter, and modelling it as a job predicate would put
    the answer in the test rather than in the workflow.
    """
    # PyYAML resolves the bare key `on` to the boolean True.
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict):
        raise Undecidable(f"unmodelled `on:` block: {triggers!r}")
    name = event["event_name"]
    if name not in triggers:
        return False
    spec = triggers[name] or {}
    if name in ("schedule", "workflow_dispatch"):
        return True
    if name == "push":
        # Three constructs whose absence from this evaluator would UNDERCOUNT
        # silently, which is the one direction the module note above forbids.
        for key in ("branches-ignore", "tags-ignore"):
            if key in spec:
                raise Undecidable(f"`{key}:` filters are not modelled")
        if "branches" not in spec and "tags" not in spec:
            raise Undecidable(
                "an unfiltered `push:` starts on every branch and tag, so "
                "reading its absent filter as no-trigger would report zero "
                "runs for a workflow that runs on all of them")
        ref = event["ref"]
        if ref.startswith("refs/heads/"):
            if "branches" not in spec:
                return False
            # Branch filters take the same glob syntax as tag filters, so they
            # go through the same compiler. Exact string membership would read
            # `branches: ['release/*']` as matching nothing.
            if not any(_glob_to_regex(p).match(ref[len("refs/heads/"):])
                       for p in spec["branches"]):
                return False
        elif ref.startswith("refs/tags/"):
            if "tags" not in spec:
                return False
            if not any(_glob_to_regex(p).match(ref[len("refs/tags/"):])
                       for p in spec["tags"]):
                return False
        else:
            raise Undecidable(f"unmodelled ref {ref!r}")
    elif name == "pull_request":
        types = spec.get("types")
        if types is not None and event["action"] not in types:
            return False
    else:
        raise Undecidable(f"unmodelled trigger {name!r}")
    ignore = spec.get("paths-ignore")
    if ignore is not None:
        paths = event["changed_paths"]
        if not paths:
            raise Undecidable(
                "the event declares no changed paths, so `paths-ignore` "
                "cannot be evaluated")
        patterns = [_glob_to_regex(p) for p in ignore]
        if all(any(p.match(path) for p in patterns) for path in paths):
            return False
    if "paths" in spec:
        raise Undecidable("`paths:` filters are not modelled")
    return True


def _admitted_jobs(doc: dict, event: dict) -> set[str]:
    """Job names GitHub would run for this event, `if:` and `needs:` included."""
    if not _workflow_starts(doc, event):
        return set()
    jobs = doc["jobs"]
    admitted, order, guard = set(), list(jobs), 0
    pending = list(order)
    while pending:
        guard += 1
        if guard > len(order) ** 2 + len(order):
            raise Undecidable(f"job graph does not settle: {pending!r}")
        name = pending.pop(0)
        body = jobs[name]
        needs = body.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        condition = body.get("if")
        # GitHub skips a dependent when a dependency did not succeed, UNLESS
        # the dependent's `if` carries a status function. That override is the
        # whole reason the gate's conditions are written with `!cancelled()`.
        overrides = condition is not None and "cancelled()" in condition
        statuses = event["needs"]
        for dependency in needs:
            if dependency not in statuses:
                raise Undecidable(
                    f"job {name!r} needs {dependency!r}, whose status this "
                    "event does not declare")
            if statuses[dependency] != "success" and not overrides:
                break
        else:
            if condition is None or _evaluate(
                " ".join(condition.split()), event["context"]):
                admitted.add(name)
    return admitted


def _matrix_legs(body: dict) -> int:
    strategy = body.get("strategy")
    if strategy is None:
        return 1
    matrix = strategy.get("matrix")
    if matrix is None:
        raise Undecidable("a strategy with no matrix is not modelled")
    if set(matrix) - {"python-version"}:
        raise Undecidable(f"unmodelled matrix axes: {sorted(matrix)}")
    return len(matrix["python-version"])


def _estate_executions(event: dict) -> int:
    """The declared number of unscoped estate executions for one event.

    The product of the factors the tests above established, and nothing is
    hard-coded here: the per-bundle contribution is read from the resolved
    plan and from the harness itself.
    """
    per_bundle = (
        _resolved_plan()["harnesses"].split().count("frontend")
        * len(_unscoped_vitest_commands(FRONTEND.read_text()))
    )
    total = 0
    for path in (CI_YML, MATRIX_YML):
        doc = yaml.safe_load(path.read_text())
        admitted = _admitted_jobs(doc, event)
        for job in sorted(admitted):
            body = doc["jobs"][job]
            runs = [s["run"] for s in (body.get("steps") or [])
                    if isinstance(s.get("run"), str)]
            scripts = [_strip_shell_comments(run) for run in runs]
            bundle_calls = sum(s.count(AGGREGATOR) for s in scripts)
            direct = sum(len(_unscoped_vitest_commands(run)) for run in runs)
            total += _matrix_legs(body) * (bundle_calls * per_bundle + direct)
    return total


def _push(repository, *, ref="refs/heads/main", changed=("bin/cctally",),
          skip_heavy="false"):
    return {
        "event_name": "push", "ref": ref, "action": None,
        "changed_paths": list(changed),
        "needs": {"release-stamp-gate": "success"},
        "context": {
            "cancelled()": False,
            "github.event_name": "push",
            "github.repository": repository,
            # A push carries no pull_request object. Declared absent rather
            # than omitted, because omitting it would make the count
            # underivable and this test would refuse instead of answering.
            "github.event.pull_request.head.repo.full_name": ABSENT,
            "needs.release-stamp-gate.result": "success",
            "needs.release-stamp-gate.outputs.skipHeavy": skip_heavy,
        },
    }


def _pull_request(repository, head_repository):
    return {
        "event_name": "pull_request", "ref": "refs/heads/topic",
        "action": "opened", "changed_paths": ["bin/cctally"],
        "needs": {"release-stamp-gate": "success"},
        "context": {
            "cancelled()": False,
            "github.event_name": "pull_request",
            "github.repository": repository,
            "github.event.pull_request.head.repo.full_name": head_repository,
            # A pull_request event is not a push, so the classifier refuses on
            # its first condition and the gate's output is the literal 'false'.
            "needs.release-stamp-gate.result": "success",
            "needs.release-stamp-gate.outputs.skipHeavy": "false",
        },
    }


PRIVATE = "omrikais/cctally-dev"
PUBLIC = "omrikais/cctally"

# One or more CONCRETE contexts per D2 row. A row covering several concrete
# events lists all of them and every one must produce the row's count, so a
# row cannot be satisfied by whichever member happens to agree with it.
EVENT_CONTEXTS = {
    "private-push-nonstamp": [
        _push(PRIVATE),
        # A MIXED code-and-markdown push, and the only input on which
        # `paths-ignore`'s "every changed path matches" differs from "any
        # changed path matches". `.github/workflows/ci.yml:6-9` states the
        # semantics this context pins; without it, relaxing the quantifier
        # would send this push to the markdown-only row and count zero.
        _push(PRIVATE, changed=("CHANGELOG.md", "bin/cctally")),
    ],
    "private-push-stamp": [_push(PRIVATE, skip_heavy="true")],
    "markdown-only-push": [
        _push(PRIVATE, changed=("CHANGELOG.md",)),
        _push(PRIVATE, changed=("CHANGELOG.md", "docs/commands/blocks.md")),
    ],
    "private-same-repo-pr": [_pull_request(PRIVATE, PRIVATE)],
    "private-fork-pr": [_pull_request(PRIVATE, "contributor/cctally-dev")],
    "public-mirror-pr": [_pull_request(PUBLIC, "contributor/cctally")],
    "public-mirror-push": [_push(PUBLIC)],
    # `ci-linux-matrix` admits on the public repository for a tag push and the
    # weekly cron, and on the private repository for a manual dispatch. Those
    # are the three concrete events behind this row. A PRIVATE tag push is NOT
    # one of them and is asserted separately below, because the row's own
    # wording does not cover it.
    "tag-or-cron-or-dispatch": [
        _push(PUBLIC, ref="refs/tags/v1.95.6", changed=("package.json",)),
        {"event_name": "schedule", "ref": "refs/heads/main", "action": None,
         "changed_paths": ["bin/cctally"], "needs": {},
         "context": {"cancelled()": False, "github.event_name": "schedule",
                     "github.repository": PUBLIC}},
        {"event_name": "workflow_dispatch", "ref": "refs/heads/main",
         "action": None, "changed_paths": ["bin/cctally"], "needs": {},
         "context": {"cancelled()": False,
                     "github.event_name": "workflow_dispatch",
                     "github.repository": PRIVATE}},
    ],
}


def test_every_declared_event_has_at_least_one_concrete_context():
    assert set(EVENT_CONTEXTS) == set(EXPECTED), (
        sorted(EVENT_CONTEXTS), sorted(EXPECTED))
    assert len(EXPECTED) == 8, sorted(EXPECTED)
    for event, contexts in EVENT_CONTEXTS.items():
        assert contexts, event


@pytest.mark.parametrize("event", sorted(EXPECTED))
def test_the_declared_topology_multiplies_out_to_the_d2_table(event):
    """Lane admission times matrix legs times per-bundle estate count, against
    the reviewed table. `private-push-stamp: 0` is the row D1 exists to
    produce, and it is decided here by evaluating the gate's real condition
    against a stamp context rather than by restating the table."""
    for context in EVENT_CONTEXTS[event]:
        assert _estate_executions(context) == EXPECTED[event], (
            event, context["event_name"], context["context"].get("github.repository"))


def test_a_failed_classifier_still_admits_the_gated_lane():
    """D1's central fail-safe, evaluated rather than assumed.

    Every context in the table above declares the classifier SUCCEEDED, so the
    two `needs:` branches in `_admitted_jobs` cannot change any count there —
    the skip branch is never reached and the override that guards it is never
    load-bearing. This context declares a FAILED classifier, which is exactly
    the case D1 says must still run the heavy lane.
    """
    event = _push(PRIVATE)
    event["needs"] = {"release-stamp-gate": "failure"}
    event["context"]["needs.release-stamp-gate.result"] = "failure"
    assert _estate_executions(event) == 1


def test_a_dependent_without_a_status_function_is_skipped_when_its_dependency_fails():
    """The other half of the `needs:` model, on a synthetic workflow.

    GitHub skips a dependent whose dependency did not succeed UNLESS the
    dependent's `if` carries a status function. No job in this repository is
    written the first way — all three gated jobs carry `!cancelled()` — so an
    evaluator that hard-coded the override to admitted would change no real
    count. The rule is therefore pinned on a workflow written here.
    """
    doc = {
        "on": {"push": {"branches": ["main"]}},
        "jobs": {
            "gate": {"runs-on": "ubuntu-latest", "steps": []},
            "dependent": {"runs-on": "ubuntu-latest", "needs": "gate",
                          "if": "github.event_name == 'push'", "steps": []},
            "overriding": {"runs-on": "ubuntu-latest", "needs": "gate",
                           "if": "!cancelled() && github.event_name == 'push'",
                           "steps": []},
        },
    }
    event = _push(PRIVATE)
    event["needs"] = {"gate": "failure"}
    assert _admitted_jobs(doc, event) == {"gate", "overriding"}
    event["needs"] = {"gate": "success"}
    assert _admitted_jobs(doc, event) == {"gate", "dependent", "overriding"}


# A context for the operator-algebra pins below. `T`, `F` and `A` are not
# workflow paths; they exist so an expression can be written whose value depends
# on nothing but the parser's own precedence and coercion rules. Every real
# condition in both workflows is fully parenthesized, applies `!` only to
# `cancelled()`, and compares an absent path only with `==`, so no real
# condition can tell a correct parser from several plausibly wrong ones.
_ALGEBRA = {"T": True, "F": False, "A": ABSENT, "S": "push"}


@pytest.mark.parametrize(
    ("expression", "expected"),
    (
        # `&&` binds tighter than `||`. Both operand patterns are listed because
        # each is decided by one direction of the precedence: `F || T && F` is
        # False under either reading and pins nothing.
        ("T || T && F", True),
        ("F && F || T", True),
        # `!` binds tighter than `&&`. Read the other way this is `!(F && F)`,
        # which is True.
        ("!F && F", False),
        # `!` applies to a whole parenthesized group.
        ("!(T || F)", False),
        # An absent path compares unequal to every string.
        ("A == 'push'", False),
        ("A != 'push'", True),
        # An absent path is falsy where a boolean is wanted, which is the
        # coercion `_as_bool` performs and no real condition exercises.
        ("A && T", False),
        ("A || T", True),
    ),
)
def test_the_expression_algebra_binds_the_way_github_does(expression, expected):
    assert _evaluate(expression, _ALGEBRA) is expected


@pytest.mark.parametrize(
    "expression",
    ("S || T", "T || S", "S && F", "F && S", "S && T", "T && S"),
)
def test_a_string_used_as_a_boolean_refuses_in_either_position(expression):
    """A string operand is a construct this evaluator does not model, so it must
    refuse wherever it appears. Coercing only the operand Python's short-circuit
    happens to reach made the refusal depend on operand order: `S || T` answered
    True and `S && F` answered False, while the same operands the other way
    round refused."""
    with pytest.raises(Undecidable):
        _evaluate(expression, _ALGEBRA)


def _push_trigger_doc(spec):
    return {"on": {"push": spec}, "jobs": {}}


@pytest.mark.parametrize(
    ("branches", "ref", "starts"),
    (
        (["main"], "refs/heads/main", True),
        (["main"], "refs/heads/topic", False),
        (["release/*"], "refs/heads/release/1.0", True),
        (["release/*"], "refs/heads/main", False),
        (["release/**"], "refs/heads/release/1/0", True),
    ),
)
def test_a_branch_filter_is_a_glob_not_a_literal(branches, ref, starts):
    """`branches:` takes the same filter-pattern syntax as `tags:`, and both
    workflows happen to list one literal branch, so exact string membership was
    indistinguishable from matching. It is not: `branches: ['release/*']` would
    have matched nothing and reported zero runs for every push to that branch."""
    assert _workflow_starts(
        _push_trigger_doc({"branches": branches}), _push(PRIVATE, ref=ref)) is starts


@pytest.mark.parametrize(
    ("label", "spec"),
    (
        ("an unfiltered push", {}),
        ("a branches-ignore filter", {"branches-ignore": ["docs/**"]}),
        ("a tags-ignore filter", {"tags": ["v*"], "tags-ignore": ["v0.*"]}),
    ),
)
def test_an_unmodelled_push_filter_refuses_rather_than_undercounting(label, spec):
    """Each of these three would have answered "this workflow does not start" —
    an undercount, and the one direction the note above this section forbids,
    because it lets a lane that runs the estate report zero."""
    with pytest.raises(Undecidable):
        _workflow_starts(_push_trigger_doc(spec), _push(PRIVATE))


PRIVATE_ZERO_CONTEXTS = {
    "tag push": _push(PRIVATE, ref="refs/tags/v1.95.6", changed=("package.json",)),
    "weekly cron": {
        "event_name": "schedule", "ref": "refs/heads/main", "action": None,
        "changed_paths": ["bin/cctally"], "needs": {},
        "context": {"cancelled()": False, "github.event_name": "schedule",
                    "github.repository": PRIVATE}},
}


@pytest.mark.parametrize("label", sorted(PRIVATE_ZERO_CONTEXTS))
def test_a_private_tag_push_or_cron_admits_no_lane(label):
    """Recorded rather than folded into the table above. The D2 row groups tag
    push, cron and dispatch at three legs, and that is true of the contexts
    where `ci-linux-matrix` admits — which are the PUBLIC tag push, the PUBLIC
    cron, and a dispatch on either repository. The two private events are not
    among them: `ci.yml` triggers only on `push: branches: [main]`, and
    `test-linux` admits the private repository only for `workflow_dispatch`.

    Both matter concretely. The release tool's own Phase 2 tag push is private,
    so the event this repository actually produces runs the estate zero times,
    and the private weekly cron does the same for the same reason.
    """
    assert _estate_executions(PRIVATE_ZERO_CONTEXTS[label]) == 0


@pytest.mark.parametrize(
    ("label", "condition"),
    (
        ("an unknown context path", "github.actor == 'someone'"),
        ("an unmodelled status function", "success() && github.event_name == 'push'"),
        ("an unmodelled operator", "github.run_number > 3"),
    ),
)
def test_an_undecidable_condition_fails_loud(label, condition):
    """Non-vacuity for the fail-loud requirement: admission that cannot be
    decided from the declared context must raise and name the construct, never
    silently resolve to admitted or skipped."""
    context = _push(PRIVATE)["context"]
    with pytest.raises(Undecidable):
        _evaluate(condition, context)


def test_the_evaluator_reproduces_the_real_gate_condition():
    """Non-vacuity for the evaluator: the exact condition from the workflow,
    evaluated against a stamp context and a non-stamp context, must disagree.
    An evaluator that returned a constant would pass every count above by
    accident on the rows that happen to share a value."""
    condition = " ".join(
        yaml.safe_load(CI_YML.read_text())["jobs"]["test-macos"]["if"].split())
    assert _evaluate(condition, _push(PRIVATE)["context"]) is True
    assert _evaluate(condition, _push(PRIVATE, skip_heavy="true")["context"]) is False
