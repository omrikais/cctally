#!/usr/bin/env bash
# Authoritative-gate verdict contract (#529 S1).
#
# Owns the admission checks (regeneration variables, capabilities), harness
# and pytest result classification, exit-code mapping and the run-outcome
# JSON. Sourced by bin/cctally-test-all; the runner client
# (bin/_lib_runner_client.py) reads its regeneration list. Bash 3.2 compatible:
# no associative arrays.
#
# Failure classes: none | product | infrastructure | incomplete.
# Exit band: 0 pass | 1 product | 2 usage | 3 infrastructure or incomplete.
# Never 75 — that is the wrapper's "still running", not an outcome.

# The FTS5 assertion, shared with provisioning so one statement answers the
# question in both places (#529 S6, exception X2). Resolved from THIS file's own
# directory, like every other sibling this library needs, and refused loudly
# when absent: a contract that silently lost its capability probe would admit a
# runner it never asked about.
if [ -r "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib-fts5-probe.sh" ]; then
    # The probe path is computed from BASH_SOURCE at runtime, so ShellCheck
    # cannot resolve it statically; the readability test above is the guard.
    # shellcheck source=/dev/null
    . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib-fts5-probe.sh"
else
    echo "cctally test contract: bin/_lib-fts5-probe.sh is missing or unreadable; refusing to run without the FTS5 assertion" >&2
    exit 3
fi

# The reason-code vocabulary (spec §5). Codes are bare string literals at each
# emission site, so nothing in the shell can catch a typo; this list is the
# authoritative registry and tests/test_authoritative_test_contract.py asserts
# BOTH directions against it — every literal emitted by bin/_lib-test-contract.sh
# and bin/cctally-test-all is a member, and every member
# is emitted somewhere, so a stale entry is caught as well as a mistyped one.
# Adding a code is additive and does not bump schemaVersion.
_CONTRACT_REASON_CODES='
admission-scratch-failed
aggregator-usage-error
capability-missing
deliberate-subset
evidence-init-failed
evidence-kernel-missing
harness-failed
harness-incomplete
harness-killed
pool-machinery-failed
pytest-collected-nothing
pytest-failed
pytest-internal
pytest-no-failing-node-ids
pytest-unavailable
quarantine-list-unreadable
regeneration-enabled
temp-generation-cleanup-failed
'

# Codes the test regime reset retired (Task 3.4: the estate manifest, the
# estate check, case floors, summary verdicts and the merge gate's budget;
# Task 4.9: the old Bash wrapper's job-killed, job-vanished and
# wrapper-failed-before-verdict, which the supervisor's client does not emit;
# Task 4.10: the outcome-record-* and outcome-exit-* codes of the carrier that
# wrapper read the record through, and agentmem-unavailable-local, the external
# incomplete reason only its local branch supplied).
# Nothing emits them, so they are not in the registry above, but retained
# outcome records and ledger events still carry them and real harness lines
# lean on their words (`unexpected`, `executable`), so the scrub vocabulary in
# bin/_lib_test_evidence.py (`build_known_tokens`) keeps reading them.
_CONTRACT_RETIRED_REASON_CODES='
agentmem-unavailable-local
binary-log
case-floor-unmet
estate-artifact-unreadable
estate-discovery-failed
estate-partition-invalid
estate-shrink-unauthorized
exit-summary-mismatch
harness-not-executable
job-killed
job-vanished
manifest-duplicate-key
manifest-invalid-name
manifest-invalid-row
manifest-missing-frontend-test
manifest-missing-harness
manifest-missing-pytest-node
manifest-missing-suppression
manifest-nontrivial
manifest-unexpected-frontend-test
manifest-unexpected-harness
manifest-unexpected-pytest-node
manifest-unexpected-suppression
manifest-unknown-key
manifest-unreadable
merge-runtime-budget-exceeded
outcome-exit-mismatch
outcome-exit-unreadable
outcome-record-malformed
outcome-record-missing
summary-unreadable
visibility-classifier-failed
visibility-classifier-unavailable
visibility-drift
wrapper-failed-before-verdict
'

CONTRACT_CLASS="none"
CONTRACT_REASONS=""       # newline-separated "code<TAB>phase<TAB>subject"
CONTRACT_LAST_CODE=""
CONTRACT_DIAGNOSTICS=""   # newline-separated operator-facing delta lines

# Class precedence, with two causally-prior overrides applied by the caller
# (see contract_classify_harness): incomplete > infrastructure > product.
_contract_rank() {
    case "$1" in
        incomplete)     echo 3 ;;
        infrastructure) echo 2 ;;
        product)        echo 1 ;;
        *)              echo 0 ;;
    esac
}

# One operator-facing line per problem, printed at the terminal path. A reason
# code names what failed; the note says how to fix it.
contract_note() {
    CONTRACT_DIAGNOSTICS="${CONTRACT_DIAGNOSTICS}$1"$'\n'
}

contract_fail() {  # contract_fail <class> <code> [subject] [phase]
    local class=$1 code=$2 subject=${3:-} phase=${4:-admission}
    CONTRACT_REASONS="${CONTRACT_REASONS}${code}"$'\t'"${phase}"$'\t'"${subject}"$'\n'
    # ShellCheck: this sourced-library result is consumed by cctally-test-all.
    # shellcheck disable=SC2034
    CONTRACT_LAST_CODE=$code
    if [ "$(_contract_rank "$class")" -gt "$(_contract_rank "$CONTRACT_CLASS")" ]; then
        CONTRACT_CLASS=$class
    fi
    return 1
}

# ------------------------------------------------------- outcome + exit mapping

CONTRACT_CAPABILITIES=""   # name<TAB>true|false, one per line
CONTRACT_PASSED=0
CONTRACT_FAILED=0
# pytest's own passed-ITEM count, summed across the bulk leg and the serial
# benchmark leg. Kept separate from CONTRACT_PASSED, which counts shell harness
# CASES: the two are different units and only their sum is the metric's
# denominator (#529 S5 §4.6).
CONTRACT_PYTEST_PASSED=0
# 1 once ANY leg reported a count that could not be read. The metric is then
# published as null rather than computed from a denominator missing pytest's
# half — a lost count does not zero that denominator, it halves it, and a
# halved denominator roughly doubles the recorded cost per thousand cases.
CONTRACT_PYTEST_UNPARSED=0

# The run's wall-clock seconds, sampled ONCE. The outcome record and the
# retained run manifest both publish a metric whose numerator is this number
# and they are written at different instants — the record at the verdict, the
# manifest afterwards from the aggregator's EXIT trap — so sampling $SECONDS
# independently in each let one run publish two different
# secondsPerThousandCases values for itself.
CONTRACT_WALL_SECONDS=""

contract_freeze_wall_seconds() {
    [ -n "$CONTRACT_WALL_SECONDS" ] || CONTRACT_WALL_SECONDS=$SECONDS
}

CONTRACT_USAGE=0           # 1 once contract_usage_abort has been called

contract_exit_code() {
    # A usage or configuration error is its own band and outranks the class
    # mapping: the spec assigns it 2, and without this case the aggregator's
    # `exit 2` paths reached the caller as an infrastructure transport failure
    # (the wrapper saw status 2, found no record, and synthesised exitCode 3).
    if [ "$CONTRACT_USAGE" = 1 ]; then
        echo 2
        return 0
    fi
    case "$CONTRACT_CLASS" in
        none)                       echo 0 ;;
        product)                    echo 1 ;;
        infrastructure|incomplete)  echo 3 ;;
        *)                          echo 3 ;;
    esac
}

contract_print_diagnostics() {
    [ -n "$CONTRACT_DIAGNOSTICS" ] || return 0
    printf '%s' "$CONTRACT_DIAGNOSTICS" >&2
}

# The outcome object is written to a dedicated record, atomically by rename, and
# is never streamed as human output: --watch combines both remote streams into
# one spool, so a stdout/stderr split cannot carry it.
contract_emit_outcome() {  # <exit_code>
    local dest=${CCTALLY_TEST_ALL_OUTCOME_FILE:-} rc=$1 tmp
    # Before the early return, so the retained manifest reads the same frozen
    # number whether or not a record was asked for.
    contract_freeze_wall_seconds
    [ -n "$dest" ] || return 0
    # A plan is never an authoritative outcome. bin/cctally-test-all already
    # refuses the two modes together, but the worker-budget check runs before
    # that refusal, so this keeps a usage abort in plan mode from writing one.
    [ "${CCTALLY_TEST_ALL_PLAN:-}" = "1" ] && return 0
    tmp="${dest}.tmp.$$"
    CONTRACT_R="$CONTRACT_REASONS" CONTRACT_C="$CONTRACT_CLASS" \
    CONTRACT_CAPS="$CONTRACT_CAPABILITIES" CONTRACT_RC="$rc" \
    CONTRACT_P="$CONTRACT_PASSED" CONTRACT_F="$CONTRACT_FAILED" \
    CONTRACT_PYP="$CONTRACT_PYTEST_PASSED" \
    CONTRACT_PYP_UNPARSED="$CONTRACT_PYTEST_UNPARSED" \
    CONTRACT_WALL="$CONTRACT_WALL_SECONDS" CONTRACT_OUTER="${OUTER:-0}" \
    CONTRACT_INNER="${INNER:-0}" CONTRACT_PYTEST_JOBS="${PYTEST:-0}" \
    CONTRACT_COV_RESOLVED="${COVERAGE_RESOLVED:-0}" \
    CONTRACT_COV_MODE="${COVERAGE_MODE:-full}" \
    CONTRACT_COV_SELECTED="${COVERAGE_SELECTED:-}" \
    CONTRACT_COV_OMITTED="${COVERAGE_OMITTED:-}" \
    CONTRACT_COV_PYTEST="${COVERAGE_PYTEST:-full}" \
    python3 -c '
import json, os

reasons = []
for line in os.environ.get("CONTRACT_R", "").split("\n"):
    if not line:
        continue
    parts = line.split("\t")
    while len(parts) < 3:
        parts.append("")
    reasons.append({"code": parts[0], "phase": parts[1], "subject": parts[2]})

caps = {}
for line in os.environ.get("CONTRACT_CAPS", "").split("\n"):
    if not line:
        continue
    name, _, value = line.partition("\t")
    caps[name] = value == "true"

cls = os.environ.get("CONTRACT_C", "none")


def _int(name):
    try:
        return int(os.environ.get(name, "0") or 0)
    except ValueError:
        return 0


# Wall-seconds per thousand passed cases. The denominator sums shell harness
# CASES and pytest ITEMS, which is why it is not called assertions. A zero
# denominator records null rather than dividing: 0.0 would be a claim about
# cost, and a run that passed nothing supports no such claim.
#
# A pytest count that could not be READ is a third answer, distinct from both.
# The sum of a known number and an unknown one is unknown, so both the count
# and the sum are published as null and the metric with them — reporting the
# shell half alone would halve the denominator and roughly double the cost this
# run appears to have had.
shell_passed = _int("CONTRACT_P")
unparsed = os.environ.get("CONTRACT_PYP_UNPARSED") == "1"
pytest_passed = None if unparsed else _int("CONTRACT_PYP")
passed_cases = None if unparsed else shell_passed + pytest_passed
wall_seconds = _int("CONTRACT_WALL")
metric = None if not passed_cases else round(wall_seconds / passed_cases * 1000, 1)

doc = {
    "schemaVersion": 1,
    "outcome": "pass" if cls == "none" else "fail",
    "failureClass": cls,
    "exitCode": int(os.environ.get("CONTRACT_RC", "3")),
    "reasons": reasons,
    "capabilities": caps,
    "pytestPassed": pytest_passed,
    "passedCases": passed_cases,
    "wallSeconds": wall_seconds,
    "secondsPerThousandCases": metric,
    "budget": {
        "outer": _int("CONTRACT_OUTER"),
        "inner": _int("CONTRACT_INNER"),
        "pytest": _int("CONTRACT_PYTEST_JOBS"),
    },
    "totals": {
        "passed": shell_passed,
        "failed": _int("CONTRACT_F"),
    },
}

# Additive at schemaVersion 1, and published only from the point a selection
# was resolved. A usage refusal that never parsed one carries no `coverage`
# object at all, rather than an object describing a selection it does not have.
if os.environ.get("CONTRACT_COV_RESOLVED") == "1":
    doc["coverage"] = {
        "mode": os.environ.get("CONTRACT_COV_MODE", "full"),
        "selectedHarnesses": os.environ.get("CONTRACT_COV_SELECTED", "").split(),
        "omittedHarnesses": os.environ.get("CONTRACT_COV_OMITTED", "").split(),
        "pytest": os.environ.get("CONTRACT_COV_PYTEST", "full"),
    }

print(json.dumps(doc, sort_keys=True))
' > "$tmp" && mv -f "$tmp" "$dest"
}

# Terminal path: print every diagnostic, write the record, exit the mapped code.
contract_finish() {
    local rc
    contract_freeze_wall_seconds
    rc=$(contract_exit_code)
    contract_print_diagnostics
    contract_emit_outcome "$rc"
    exit "$rc"
}

# Admission refusal: no verdict was attempted, so the run stops here.
contract_abort() { contract_finish; }

# An aggregator usage or configuration error. It writes a record like every
# other terminal path, so a reader of the record finds the status corroborated:
# a usage refusal that wrote none once reached the caller as an infrastructure
# failure at exit 3. The record is what keeps 2 meaning 2.
contract_usage_abort() {  # <message> [subject]
    local message=$1 subject=${2:-}
    echo "cctally-test-all: $message" >&2
    CONTRACT_USAGE=1
    contract_fail infrastructure aggregator-usage-error "$subject" admission
    contract_finish
}

# ----------------------------------------------------------------- capabilities
#
# CCTALLY_AUTHORITATIVE_RUN=1 marks a run whose green is meant to be believed:
# the runner supervisor exports it for the canonical command. Setting it can
# only make a run STRICTER, and unsetting it degrades a refusal to an
# `incomplete` classification and never to green, so it is safe by construction
# and needs no anti-tamper guard.
contract_is_authoritative() { [ "${CCTALLY_AUTHORITATIVE_RUN:-}" = "1" ]; }

_contract_probe() {  # <probe-spec>
    local spec=$1 mod
    case "$spec" in
        pytest)
            python3 -m pytest --version >/dev/null 2>&1 ;;
        fts5)
            # A runner precondition, not a product requirement: the product
            # deliberately degrades without FTS5 and the 55 pytest.skip sites
            # stay. What changes is only that an authoritative run refuses to
            # start on a runner lacking it, so genuinely skipped coverage
            # surfaces instead of reading as a pass.
            #
            # The statement itself lives in bin/_lib-fts5-probe.sh, which
            # provisioning also calls so the refusal can land before the suite
            # rather than after the round trip (#529 S6, exception X2). PROBE
            # mode is deliberate: the decision below — authoritative refusal
            # versus non-authoritative `incomplete` that continues — stays with
            # this caller, and a helper that printed a refusal diagnostic would
            # be false on the second path.
            fts5_probe python3 ;;
        python-import:*)
            mod=${spec#python-import:}
            python3 -c "import $mod" >/dev/null 2>&1 ;;
        command:*)
            command -v "${spec#command:}" >/dev/null 2>&1 ;;
        node)
            # Executed, not merely resolved: a node on PATH that cannot run is
            # the same outage as no node at all.
            node --version >/dev/null 2>&1 && npm --version >/dev/null 2>&1 ;;
        *)
            return 1 ;;
    esac
}

# The runner capabilities admission probes (they used to live in the estate
# manifest): name<TAB>probe<TAB>hard<TAB>visibility, one per line. `hard` is
# true (refuse an authoritative run, record incomplete otherwise), false (a
# speed knob, recorded only) or policy (hard when
# CCTALLY_AGENTMEM_TEST_POLICY=required). A private row is probed only in the
# private profile. tests/helpers/contract_tree.py rewrites this one assignment
# in synthetic trees, so keep it a single $'...' line.
CONTRACT_CAPABILITY_TABLE=$'pytest\tpytest\ttrue\t\nfts5\tfts5\ttrue\t\npytest-timeout\tpython-import:pytest_timeout\ttrue\t\nrich\tpython-import:rich\ttrue\t\nnode\tnode\ttrue\t\npytest-xdist\tpython-import:xdist\tfalse\t\nagentmem\tcommand:agentmem\tpolicy\tprivate\n'

# Records EVERY probe into CONTRACT_CAPABILITIES, then fails on the hard ones.
# Reads the table from process substitution rather than a pipe so the recorded
# results survive in this shell (a pipeline would run the loop in a subshell).
contract_admit_capabilities() {  # <profile>
    local profile=$1 name probe hard vis ok=0 result
    while IFS=$'\t' read -r name probe hard vis; do
        [ -n "$name" ] || continue
        if [ "$vis" = "private" ] && [ "$profile" != "private" ]; then
            continue
        fi
        if _contract_probe "$probe"; then result=true; else result=false; fi
        CONTRACT_CAPABILITIES="${CONTRACT_CAPABILITIES}${name}"$'\t'"${result}"$'\n'
        [ "$result" = true ] && continue
        case "$hard" in
            true) ;;
            policy)
                # Asserted against the EFFECTIVE policy, not hardcoded:
                # the runner supervisor pins `required` for every supervised
                # execution, while the hosted lanes declare their own boundary.
                [ "${CCTALLY_AGENTMEM_TEST_POLICY:-}" = "required" ] || continue ;;
            *)
                # Soft: recorded in the outcome and never fatal. pytest-xdist is
                # explicitly a speed knob, and failing a run for it would fail
                # for a reason that changes nothing about what was verified.
                continue ;;
        esac
        if contract_is_authoritative; then
            contract_note "admission: required capability '$name' is unavailable (probe: $probe) — an authoritative run refuses to start rather than verify less than it claims"
            contract_fail infrastructure capability-missing "$name"
        else
            contract_note "admission: capability '$name' is unavailable (probe: $probe) — this run is NOT authoritative (CCTALLY_AUTHORITATIVE_RUN is unset), so it is classified incomplete and continues; it can never be green"
            contract_fail incomplete capability-missing "$name"
        fi
        ok=1
    done < <(printf '%s\n' "$CONTRACT_CAPABILITY_TABLE")
    return $ok
}

# ----------------------------------------------------------------- regeneration
#
# Presence is poison, and this is deliberately stricter than each variable's own
# activation semantics: every one of them activates only on `= 1`, so `REGEN=0`
# does nothing today and would become a refusal. That is intended. An exported
# regeneration variable of ANY value signals intent that must not silently ride
# into a run whose green is meant to be believed, and `REGEN=true` — which today
# does nothing while looking like it does — is itself a footgun.
#
# A DIRECT harness invocation is untouched: CCTALLY_DOCTOR_REGENERATE=1
# bin/cctally-doctor-test keeps working exactly as before.
#
# The list is shared with bin/cctally-test-remote's own guard (Codex Q10), so
# the two can never disagree about which variables are poison.
CONTRACT_REGENERATION_VARIABLES='CCTALLY_REGEN_GOLDENS
REGEN
HARNESS_REGEN
CCTALLY_DOCTOR_REGENERATE
CCTALLY_MIGRATIONS_REGENERATE
CCTALLY_SETUP_REGENERATE
CCTALLY_PRICING_CHECK_REGENERATE
CCTALLY_UPDATE_TEST_REGEN'

contract_admit_regeneration() {
    local var ok=0 value
    while IFS= read -r var; do
        [ -n "$var" ] || continue
        if eval "[ \"\${${var}+set}\" = set ]"; then
            eval "value=\${${var}}"
            contract_note "admission: '$var' is present in the environment (value: '${value}') — a golden-regeneration variable converts a comparison into an adoption, so it must never ride into a run. Presence is poison regardless of value; unset it, or invoke the single harness directly, which this guard deliberately does not touch."
            contract_fail infrastructure regeneration-enabled "$var"
            ok=1
        fi
    done < <(printf '%s\n' "$CONTRACT_REGENERATION_VARIABLES")
    return $ok
}

# ------------------------------------------------------ harness classification
#
# The verdict is the exit status alone (Codex Q10): 0 passes, 1 is a product
# failure, any other status is incomplete. The `passed: N failed: M` summary
# feeds the report row and the normalized metric only; case counts never decide
# success. The grammar is explicit because harnesses print two spacing variants
# (bin/cctally-rederive-test prints two spaces between the counters).
_CONTRACT_SUMMARY_RE='^passed:[[:space:]]+([0-9]+)[[:space:]]+failed:[[:space:]]+([0-9]+)[[:space:]]*$'

CONTRACT_LAST_PASSED=0
CONTRACT_LAST_FAILED=0
CONTRACT_LAST_SUMMARY_OK=0
CONTRACT_LAST_HARNESS_OK=0

contract_classify_harness() {  # <name> <logfile> <exitfile>
    local name=$1 log=$2 exitfile=$3 rc line p f
    CONTRACT_LAST_PASSED=0
    CONTRACT_LAST_FAILED=0
    CONTRACT_LAST_SUMMARY_OK=0
    CONTRACT_LAST_HARNESS_OK=0

    # The summary first, as a diagnostic. A log carrying a NUL byte is not
    # searched: BSD and GNU grep answer differently for it.
    if [ -f "$log" ] && LC_ALL=C tr -d '\000' < "$log" | cmp -s - "$log"; then
        line=$(grep -E "$_CONTRACT_SUMMARY_RE" "$log" 2>/dev/null | tail -1)
        if [ -n "$line" ]; then
            p=$(printf '%s\n' "$line" | sed -E "s/$_CONTRACT_SUMMARY_RE/\1/")
            f=$(printf '%s\n' "$line" | sed -E "s/$_CONTRACT_SUMMARY_RE/\2/")
            case "$p$f" in
                ''|*[!0-9]*) ;;
                *)
                    # ShellCheck: this sourced-library result is consumed by cctally-test-all.
                    # shellcheck disable=SC2034
                    CONTRACT_LAST_PASSED=$p
                    # ShellCheck: this sourced-library result is consumed by cctally-test-all.
                    # shellcheck disable=SC2034
                    CONTRACT_LAST_FAILED=$f
                    # ShellCheck: this sourced-library result is consumed by cctally-test-all.
                    # shellcheck disable=SC2034
                    CONTRACT_LAST_SUMMARY_OK=1
                    CONTRACT_PASSED=$((CONTRACT_PASSED + p))
                    CONTRACT_FAILED=$((CONTRACT_FAILED + f)) ;;
            esac
        fi
    fi

    # A process that never left a status, or died on a signal, cannot be
    # judged on what it printed.
    if [ ! -f "$exitfile" ]; then
        contract_fail infrastructure pool-machinery-failed "$name" harness
        return 0
    fi
    rc=$(cat "$exitfile" 2>/dev/null)
    case "$rc" in
        ''|*[!0-9]*)
            contract_fail infrastructure pool-machinery-failed "$name" harness
            return 0 ;;
    esac
    if [ "$rc" -gt 128 ]; then
        contract_fail infrastructure harness-killed "$name" harness
        return 0
    fi
    case "$rc" in
        0)
            # ShellCheck: this sourced-library result is consumed by cctally-test-all.
            # shellcheck disable=SC2034
            CONTRACT_LAST_HARNESS_OK=1 ;;
        1)  contract_fail product harness-failed "$name" harness ;;
        *)  contract_fail incomplete harness-incomplete "$name" harness ;;
    esac
    return 0
}

# The pytest phase's exit code is read for MEANING, not as a boolean. The
# separate wall-clock benchmark leg is classified by the same rule, so
# isolating it did not put it outside the verdict.
#
# The third argument is the leg's passed-ITEM count and is purely additive: it
# is accumulated before the exit-code cases and changes none of them, so a
# count that could not be read degrades the METRIC and never the verdict. It is
# accumulated for EVERY leg including a passing one, which is why the caller
# invokes this unconditionally rather than only on a non-zero status.
#
# `${3-0}`, not `${3:-0}`: an EMPTY third argument is the caller saying it read
# no count at all, and collapsing that to 0 is exactly the confusion this
# sentinel exists to prevent. An omitted argument still means 0, because a
# caller that passes none never tried to measure. The optional fourth argument
# is the caller's node-id parse result for a status-1 leg: `present`, `none`, or
# omitted/`unknown`. A status-1 run that identifies no FAILED/ERROR node is an
# infrastructure failure, not evidence of defective product code. `unknown`
# preserves the historical exit-code-only behavior for other callers.
contract_classify_pytest() {  # <rc> [subject] [passed_items] [node_ids]
    local rc=$1 subject=${2:-pytest} passed=${3-0} node_ids=${4:-unknown}
    case "$passed" in
        ''|*[!0-9]*)
            CONTRACT_PYTEST_UNPARSED=1
            contract_note "pytest: the $subject leg's passed-item count could not be read from its log, so this run's passed-case total and normalized metric are recorded as null, and the verdict is unaffected"
            passed=0 ;;
    esac
    CONTRACT_PYTEST_PASSED=$((CONTRACT_PYTEST_PASSED + passed))
    case "$rc" in
        0)     return 0 ;;
        1)     if [ "$node_ids" = none ]; then
                   contract_fail infrastructure pytest-no-failing-node-ids "$subject" pytest
               else
                   contract_fail product pytest-failed "$subject" pytest
               fi ;;
        5)     contract_fail incomplete pytest-collected-nothing "$subject" pytest ;;
        2|3|4) contract_fail infrastructure pytest-internal "$subject" pytest ;;
        *)     contract_fail infrastructure pytest-internal "$subject" pytest ;;
    esac
    return 0
}
