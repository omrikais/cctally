"""#630 S4 — a push whose tree an authoritative receipt covers skips the estate.

Two halves. The STRUCTURE half parses `.github/workflows/ci.yml` and asserts the
new job, the conjunct and the concurrency block are exactly what the spec
states. The SEMANTIC half evaluates `test-macos`'s condition against real
workflow contexts, because the guarantee the spec makes — a `receipt-gate` that
fails, is skipped, or emits a missing or malformed output leaves the estate
RUNNING — is a claim about what the expression evaluates to, and no substring
assertion can establish it.

The two halves are not interchangeable. An exact-string assertion cannot say
what the string means, and an evaluator over a condition nobody pinned would
happily certify a rewritten one.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_YML = ROOT / ".github/workflows/ci.yml"
GATE_JOB = "receipt-gate"
STAMP_JOB = "release-stamp-gate"
SELF_HOSTED_JOBS = ("test-macos", "dashboard-build-stability", "e2e-reader")
PRIVATE_REPO = "omrikais/cctally-dev"

# The exact clause, as ONE contiguous literal. `tests/test_ci_release_stamp_gate.py`
# records why this is not four substring assertions: `&&`-joining the two halves
# instead of `||`-joining them inside parentheses contains every token and
# inverts the meaning — the job would then run only when the classifier did NOT
# succeed, which is never, on a healthy push.
RECEIPT_CLAUSE = (
    "(needs.receipt-gate.result != 'success' || "
    "needs.receipt-gate.outputs.discharged != 'true')"
)

TEST_MACOS_CONDITION = (
    "!cancelled() && "
    "(needs.release-stamp-gate.result != 'success' || "
    "needs.release-stamp-gate.outputs.skipHeavy != 'true') && "
    "(needs.receipt-gate.result != 'success' || "
    "needs.receipt-gate.outputs.discharged != 'true') && "
    "github.repository == 'omrikais/cctally-dev' && "
    "(github.event_name == 'push' || "
    "(github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name == github.repository))"
)

# Preserve-item 1. These are the fork/self-hosted safety boundary, and this file
# asserts them byte for byte so that nothing here can widen them by accident.
FORK_GUARD = (
    "github.repository == 'omrikais/cctally-dev' && "
    "(github.event_name == 'push' || "
    "(github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name == github.repository))"
)
PUSH_ONLY_GUARD = (
    "github.event_name == 'push' && github.repository == 'omrikais/cctally-dev'"
)

CONCURRENCY_GROUP = (
    "ci-${{ github.event_name }}-"
    "${{ github.event.pull_request.number || github.ref }}"
)


def _ci():
    return yaml.safe_load(CI_YML.read_text())


def _condition(job):
    return " ".join(_ci()["jobs"][job]["if"].split())


# --------------------------------------------------------------- the evaluator
#
# A GitHub expression evaluator over exactly the grammar these conditions use:
# `!`, `&&`, `||`, `==`, `!=`, parentheses, single-quoted strings, context paths
# and `cancelled()`. A missing context path is `None`, which is falsy and
# compares unequal to every string — which is precisely how a missing job output
# has to behave for the fail-safe to hold.

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


class _Parser:
    def __init__(self, tokens, context):
        self.tokens = tokens
        self.i = 0
        self.context = context

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
            assert self.take() == "("
            assert self.take() == ")"
            return bool(self.context.get("cancelled"))
        return _lookup(self.context, token)


def _lookup(context, path):
    node = context
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value != ""
    return bool(value)


def _evaluate(condition, context):
    return _truthy(_Parser(_tokenize(condition), context).parse())


def _context(*, repository=PRIVATE_REPO, event="push", head_repo=None,
             cancelled=False, stamp_result="success", skip_heavy="false",
             receipt_result="success", discharged="false",
             receipt_outputs_present=True):
    receipt_outputs = {}
    if receipt_outputs_present and discharged is not None:
        receipt_outputs["discharged"] = discharged
    return {
        "cancelled": cancelled,
        "github": {
            "repository": repository,
            "event_name": event,
            "event": {"pull_request": {
                "head": {"repo": {"full_name": head_repo or repository}}}},
        },
        "needs": {
            "release-stamp-gate": {
                "result": stamp_result,
                "outputs": {"skipHeavy": skip_heavy},
            },
            GATE_JOB: {"result": receipt_result, "outputs": receipt_outputs},
        },
    }


def test_the_evaluator_reads_this_grammar_correctly():
    """Non-vacuity for the evaluator itself. Every semantic test below rests on
    it, so an evaluator that returned True for everything would certify an
    inverted condition as correct."""
    ctx = {"a": {"b": "x"}, "cancelled": True}
    assert _evaluate("a.b == 'x'", ctx) is True
    assert _evaluate("a.b == 'y'", ctx) is False
    assert _evaluate("a.b != 'y'", ctx) is True
    assert _evaluate("a.missing != 'y'", ctx) is True
    assert _evaluate("a.missing == 'y'", ctx) is False
    assert _evaluate("!cancelled()", ctx) is False
    assert _evaluate("!cancelled()", {"cancelled": False}) is True
    assert _evaluate("(a.b == 'x' || a.b == 'y') && !cancelled()", ctx) is False
    assert _evaluate("a.b == 'x' && a.b == 'x'", ctx) is True
    assert _evaluate("a.b == 'q' || a.b == 'x'", ctx) is True


# ---------------------------------------------------------------- structure
def test_the_receipt_gate_job_exists_and_is_hosted():
    jobs = _ci()["jobs"]
    assert GATE_JOB in jobs, sorted(jobs)
    assert jobs[GATE_JOB]["runs-on"] == "ubuntu-latest"


def test_the_receipt_gate_guard_is_exactly_its_consumers_guard():
    """Two independent reasons, one condition.

    The repository equality alone stops it running on every mirror push, which
    would waste hosted minutes and change the operator-selected public-push
    outcome: a public push must continue to run the release classifier and
    nothing else.

    The event half is what keeps `secrets.GITHUB_TOKEN` away from fork code.
    The classify step puts the token in the environment of a script that comes
    from the checkout, and on a `pull_request` event the checkout is the MERGE
    ref — so on a fork PR that script is fork-authored. The guard therefore
    mirrors `test-macos`, this job's only consumer, which already refuses fork
    PRs: a context that could never act on the discharge cannot reach the token
    either. Same-repository PR discharge is preserved, which a push-only guard
    would have given up.

    Asserted as ONE contiguous string for the reason `RECEIPT_CLAUSE` records:
    four independent substring assertions cannot distinguish this from an
    inverted condition containing all four tokens.
    """
    assert _condition(GATE_JOB) == FORK_GUARD


def test_a_fork_pull_request_never_runs_the_receipt_gate():
    """Evaluated, not matched. This is the leg that keeps the token out of
    fork-authored code, so it gets the same treatment as preserve-item 1."""
    assert _evaluate(_condition(GATE_JOB), _context(
        event="pull_request", head_repo="someone/cctally-dev")) is False


def test_the_public_mirror_never_runs_the_receipt_gate():
    assert _evaluate(_condition(GATE_JOB),
                     _context(repository="omrikais/cctally")) is False


@pytest.mark.parametrize("label,kwargs", [
    ("a push to main", {}),
    ("a same-repository pull request", {"event": "pull_request"}),
])
def test_the_receipt_gate_still_runs_wherever_its_consumer_can(label, kwargs):
    """The narrowing must cost nothing that is reachable. Both contexts in
    which `test-macos` can run must still be able to discharge it, or the guard
    has traded a real capability for the token fix rather than adding it for
    free."""
    assert _evaluate(_condition(GATE_JOB), _context(**kwargs)) is True, label


def test_the_receipt_gate_and_test_macos_admit_the_same_contexts():
    """Stated as a relationship rather than as two copies of a string, because
    the reason the guard is written this way is that it mirrors its consumer.
    Anything `test-macos` refuses on repository or event grounds, this refuses
    too — and nothing more."""
    for kwargs in ({}, {"event": "pull_request"},
                   {"event": "pull_request", "head_repo": "someone/cctally-dev"},
                   {"repository": "omrikais/cctally"}):
        gate = _evaluate(_condition(GATE_JOB), _context(**kwargs))
        macos = _evaluate(FORK_GUARD, _context(**kwargs))
        assert gate is macos, kwargs


def test_the_release_classifier_stays_deliberately_unguarded():
    """F29's operator decision, pinned rather than argued. A public push runs
    the release classifier and NOTHING else, so its green result is not estate
    evidence — and guarding this job would change that operator-selected
    outcome. Adding a guard here is a decision, not a tidy-up."""
    assert "if" not in _ci()["jobs"][STAMP_JOB], _ci()["jobs"][STAMP_JOB].get("if")


def test_the_receipt_gate_is_read_only_and_does_not_persist_credentials():
    body = _ci()["jobs"][GATE_JOB]
    assert body["permissions"] == {"contents": "read"}
    checkout = [s for s in body["steps"] if str(s.get("uses", "")).startswith(
        "actions/checkout")]
    assert len(checkout) == 1, body["steps"]
    assert checkout[0]["with"]["persist-credentials"] is False


def test_the_receipt_gate_publishes_a_discharged_output():
    assert "discharged" in (_ci()["jobs"][GATE_JOB].get("outputs") or {})


def test_the_receipt_gate_runs_the_public_classifier():
    body = yaml.safe_dump(_ci()["jobs"][GATE_JOB])
    assert ".github/scripts/classify_receipt.py" in body, body


def test_only_test_macos_carries_the_receipt_conjunct():
    """The receipt certifies `bin/cctally-test-all`. It does not build the
    dashboard bundle and it does not run Playwright, so the other two
    self-hosted jobs keep exactly their current guards."""
    assert RECEIPT_CLAUSE in _condition("test-macos")
    for job in ("dashboard-build-stability", "e2e-reader"):
        condition = _condition(job)
        assert RECEIPT_CLAUSE not in condition, condition
        needs = _ci()["jobs"][job]["needs"]
        needs = [needs] if isinstance(needs, str) else needs
        assert GATE_JOB not in needs, needs


def test_test_macos_declares_both_gates_in_needs():
    needs = _ci()["jobs"]["test-macos"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert needs == [STAMP_JOB, GATE_JOB], needs


def test_the_test_macos_condition_is_exactly_the_specified_string():
    assert _condition("test-macos") == TEST_MACOS_CONDITION


def test_the_three_fork_guards_are_byte_unchanged():
    """Preserve-item 1's regression test. Nothing in #630 S4 touches the
    repository or head-repository equalities, and this is what says so."""
    assert _condition("test-macos").endswith(FORK_GUARD)
    for job in ("dashboard-build-stability", "e2e-reader"):
        assert PUSH_ONLY_GUARD in _condition(job), _condition(job)


def test_the_workflow_level_concurrency_group_is_exactly_as_specified():
    concurrency = _ci()["concurrency"]
    assert concurrency["group"] == CONCURRENCY_GROUP
    assert concurrency["cancel-in-progress"] is True


def test_the_pull_request_lane_keeps_a_distinct_concurrency_scope():
    """`test-pr`'s job-level group must stay a different NAME, or the two scopes
    could cancel each other."""
    job_group = _ci()["jobs"]["test-pr"]["concurrency"]["group"]
    assert job_group != CONCURRENCY_GROUP, job_group


# ---------------------------------------------------------------- semantics
def test_a_verified_discharge_skips_the_estate():
    assert _evaluate(_condition("test-macos"),
                     _context(discharged="true")) is False


def test_no_discharge_runs_the_estate():
    assert _evaluate(_condition("test-macos"),
                     _context(discharged="false")) is True


@pytest.mark.parametrize("label,kwargs", [
    ("failed", {"receipt_result": "failure", "receipt_outputs_present": False}),
    ("skipped", {"receipt_result": "skipped", "receipt_outputs_present": False}),
    ("missing output", {"receipt_outputs_present": False}),
    ("malformed output", {"discharged": "maybe"}),
    ("empty output", {"discharged": ""}),
    ("cancelled dependency", {"receipt_result": "cancelled",
                              "receipt_outputs_present": False}),
])
def test_a_broken_receipt_gate_leaves_the_estate_running(label, kwargs):
    """The guarantee, stated exactly: while the workflow remains active, a
    `receipt-gate` that fails, is skipped, or emits a missing or malformed
    output leaves `result != 'success'` or `discharged != 'true'` true, so the
    estate RUNS. Only an explicit successful `discharged == 'true'` skips it."""
    assert _evaluate(_condition("test-macos"), _context(**kwargs)) is True, label


def test_a_cancelled_run_does_not_run_the_job_and_that_is_not_the_fail_safe():
    """A cancelled RUN makes the leading `!cancelled()` false, so the job does
    not run. That is correct behaviour for a cancelled run and it is NOT the
    estate running — the guarantee above deliberately does not cover it.

    The context sets `cancelled` TRUE. A fixture that modelled cancellation by
    setting `cancelled()` false would assert the opposite of the condition
    under test.
    """
    assert _evaluate(_condition("test-macos"),
                     _context(cancelled=True, discharged="false")) is False


def test_a_fork_pull_request_never_runs_the_self_hosted_job(): 
    """Preserve-item 1, evaluated rather than matched. A discharge cannot admit
    fork code, and neither can a non-discharge."""
    for discharged in ("true", "false"):
        assert _evaluate(_condition("test-macos"), _context(
            event="pull_request", head_repo="someone/cctally-dev",
            discharged=discharged)) is False


def test_the_public_mirror_never_runs_the_self_hosted_job():
    assert _evaluate(_condition("test-macos"),
                     _context(repository="omrikais/cctally",
                              discharged="false")) is False


def test_a_release_stamp_still_suppresses_the_estate_on_its_own():
    """Adding a second `needs` entry must not change the first gate's meaning."""
    assert _evaluate(_condition("test-macos"),
                     _context(skip_heavy="true", discharged="false")) is False


def test_a_broken_release_classifier_still_runs_the_estate():
    assert _evaluate(_condition("test-macos"), _context(
        stamp_result="failure", skip_heavy="true", discharged="false")) is True


def test_a_trusted_same_repository_pull_request_still_runs_the_estate():
    assert _evaluate(_condition("test-macos"),
                     _context(event="pull_request", discharged="false")) is True
