#!/usr/bin/env bash
# Authoritative-gate verdict contract (#529 S1).
#
# Owns the estate manifest, admission checks, harness-result classification,
# exit-code mapping and the run-outcome JSON. Sourced by bin/cctally-test-all
# and by bin/cctally-test-remote. Bash 3.2 compatible: no associative arrays.
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
# BOTH directions against it — every literal emitted by bin/_lib-test-contract.sh,
# bin/cctally-test-all and bin/cctally-test-remote is a member, and every member
# is emitted somewhere, so a stale entry is caught as well as a mistyped one.
# Adding a code is additive and does not bump schemaVersion.
_CONTRACT_REASON_CODES='
admission-scratch-failed
agentmem-unavailable-local
aggregator-usage-error
binary-log
capability-missing
case-floor-unmet
deliberate-subset
estate-artifact-unreadable
estate-discovery-failed
estate-partition-invalid
estate-shrink-unauthorized
evidence-init-failed
evidence-kernel-missing
exit-summary-mismatch
harness-failed
harness-killed
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
outcome-exit-mismatch
outcome-exit-unreadable
outcome-record-malformed
outcome-record-missing
pool-machinery-failed
pytest-collected-nothing
pytest-failed
pytest-internal
pytest-no-failing-node-ids
pytest-unavailable
regeneration-enabled
merge-runtime-budget-exceeded
runtime-budget-exceeded
runtime-budget-unavailable
summary-unreadable
visibility-classifier-failed
visibility-classifier-unavailable
visibility-drift
wrapper-failed-before-verdict
'

CONTRACT_CLASS="none"
CONTRACT_REASONS=""       # newline-separated "code<TAB>phase<TAB>subject"
CONTRACT_LAST_CODE=""
CONTRACT_MANIFEST_JSON=""
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

# One operator-facing line per delta. The checker prints EVERY delta, not just
# the first, and each line states the alternatives — for an unexpected harness
# and a missing one, editing the manifest and fixing the filesystem are each
# potentially correct, and a reason code alone cannot resolve that.
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

_CONTRACT_TOP_KEYS='schemaVersion seededAt seededFrom minHarnessRows harnesses capabilities forbiddenRegeneration'
_CONTRACT_HARNESS_KEYS='name visibility minCases countPolicy countAxis'
_CONTRACT_CAPABILITY_KEYS='name probe hard visibility'

contract_manifest_load() {  # contract_manifest_load <path>
    local path=$1 out rc code subject
    out=$(CONTRACT_TOP_KEYS="$_CONTRACT_TOP_KEYS" \
          CONTRACT_HARNESS_KEYS="$_CONTRACT_HARNESS_KEYS" \
          CONTRACT_CAPABILITY_KEYS="$_CONTRACT_CAPABILITY_KEYS" \
          python3 - "$path" <<'PY'
import json, os, re, sys

path = sys.argv[1]


def no_dupes(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise ValueError("manifest-duplicate-key:%s" % k)
        seen.add(k)
    return dict(pairs)


try:
    with open(path, "rb") as handle:
        raw = handle.read().decode("utf-8")
except OSError as exc:
    print("manifest-unreadable:%s" % (exc.strerror or "unreadable",))
    sys.exit(1)
# UnicodeDecodeError is a ValueError, not an OSError. Read as bytes and decode
# explicitly so a non-UTF-8 manifest is refused with a cause the operator can
# act on, rather than escaping the handler and printing an empty cause beside a
# raw traceback. contract_budget_load carries the identical shape, which is what
# makes docs/remote-testing.md's equal-strictness claim true of the shipped code.
except UnicodeDecodeError:
    print("manifest-unreadable:not UTF-8")
    sys.exit(1)

try:
    doc = json.loads(raw, object_pairs_hook=no_dupes)
except ValueError as exc:
    msg = str(exc)
    print(msg if msg.startswith("manifest-duplicate-key") else "manifest-unreadable:")
    sys.exit(1)

if not isinstance(doc, dict):
    print("manifest-unreadable:")
    sys.exit(1)


def check(obj, allowed, code):
    if not isinstance(obj, dict):
        print("%s:%r" % (code, obj))
        sys.exit(1)
    for k in obj:
        if k not in allowed:
            print("%s:%s" % (code, k))
            sys.exit(1)


check(doc, os.environ["CONTRACT_TOP_KEYS"].split(), "manifest-unknown-key")
if doc.get("schemaVersion") != 1:
    print("manifest-unreadable:schemaVersion=%r" % (doc.get("schemaVersion"),))
    sys.exit(1)
for row in doc.get("harnesses", []):
    check(row, os.environ["CONTRACT_HARNESS_KEYS"].split(), "manifest-unknown-key")
    # A floor that is not an integer is not a floor: `[ N -lt "abc" ]` is a
    # shell arithmetic error, which is non-fatal, so a mistyped value would
    # disable the check for that row without failing anything.
    name = row.get("name")
    if not isinstance(name, str) or not name:
        print("manifest-invalid-row:name=%r" % (name,))
        sys.exit(1)
    # THE NAME GRAMMAR. A harness name is serialized into a tab-and-newline
    # separated table, read back through shell word splitting, and counted by
    # word splitting again in the tier partition. Whitespace makes those three
    # readings disagree, and a glob metacharacter is expanded by any word-split
    # path that has not disabled pathname expansion — so a whitespace-only rule
    # would leave half the divergence open. The grammar is the basename shape
    # every harness on disk already has, because the name is the middle of
    # `bin/cctally-<name>-test`.
    if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", name):
        print("manifest-invalid-name:%s" % (name,))
        sys.exit(1)
    floor_value = row.get("minCases")
    if not isinstance(floor_value, int) or isinstance(floor_value, bool) or floor_value < 0:
        print("manifest-invalid-row:%s minCases=%r" % (name, floor_value))
        sys.exit(1)
    # countPolicy/countAxis are advisory to the reader — the floor itself is
    # one number — but a `variable` row with no axis records no evidence for
    # WHY its count varies, so it is refused rather than accepted silently.
    if row.get("countPolicy") == "variable" and not row.get("countAxis"):
        print("manifest-invalid-row:%s countPolicy=variable without countAxis" % (name,))
        sys.exit(1)
for row in doc.get("capabilities", []):
    check(row, os.environ["CONTRACT_CAPABILITY_KEYS"].split(), "manifest-unknown-key")

# Non-triviality floor: a truncated manifest must not pass by having nothing
# to disagree with.
floor = doc.get("minHarnessRows", 0)
if len(doc.get("harnesses", [])) < floor:
    print("manifest-nontrivial:%d<%d" % (len(doc.get("harnesses", [])), floor))
    sys.exit(1)

json.dump(doc, sys.stdout, sort_keys=True)
PY
    ) ; rc=$?
    if [ $rc -ne 0 ]; then
        code=${out%%:*}
        subject=${out#*:}
        [ "$subject" = "$out" ] && subject=""
        case "$code" in
            manifest-duplicate-key|manifest-unknown-key|manifest-invalid-row|manifest-invalid-name|manifest-nontrivial|manifest-unreadable) ;;
            *) code=manifest-unreadable; subject="" ;;
        esac
        # The grammar refusal states the RULE, not only the offending name. A
        # bare "manifest-invalid-name (alpha bravo)" tells an operator that a
        # name was rejected without telling them which shape is accepted, and
        # the shape is the whole content of this check.
        if [ "$code" = manifest-invalid-name ]; then
            contract_note "manifest: harness name '$subject' is outside the accepted grammar ^[a-z0-9]+(-[a-z0-9]+)*\$ — a name is word-split on three separate paths, so whitespace and glob metacharacters are unsafe in one; rename the row and bin/cctally-<name>-test together"
        else
            contract_note "manifest: $code${subject:+ ($subject)} — fix tests/authoritative-test-manifest.json"
        fi
        contract_fail infrastructure "$code" "$subject"
        return 1
    fi
    CONTRACT_MANIFEST_JSON=$out
    if ! _contract_index_manifest; then
        contract_note "manifest: could not index the parsed manifest"
        contract_fail infrastructure manifest-unreadable "index"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------- manifest index
# One python call materialises three lookup tables, so no later shell path pays
# a subprocess per harness. Bash 3.2 has no associative arrays; these are
# tab-separated line tables read with awk.
CONTRACT_HARNESS_TABLE=""      # name<TAB>visibility<TAB>minCases<TAB>countPolicy<TAB>countAxis
CONTRACT_CAPABILITY_TABLE=""   # name<TAB>probe<TAB>hard<TAB>visibility
CONTRACT_FORBIDDEN_LIST=""     # one variable name per line

_contract_index_manifest() {
    local out
    out=$(printf '%s' "$CONTRACT_MANIFEST_JSON" | python3 -c '
import json, sys

doc = json.load(sys.stdin)
print("---harnesses---")
for row in doc.get("harnesses", []):
    print("\t".join([
        str(row.get("name", "")),
        str(row.get("visibility", "public")),
        str(row.get("minCases", 0)),
        str(row.get("countPolicy", "fixed")),
        str(row.get("countAxis", "")),
    ]))
print("---capabilities---")
for row in doc.get("capabilities", []):
    print("\t".join([
        str(row.get("name", "")),
        str(row.get("probe", "")),
        json.dumps(row.get("hard", False)),
        str(row.get("visibility", "")),
    ]))
print("---forbidden---")
for var in doc.get("forbiddenRegeneration", []):
    print(var)
') || return 1
    CONTRACT_HARNESS_TABLE=$(printf '%s\n' "$out" | awk '/^---harnesses---$/{f=1;next} /^---capabilities---$/{f=0} f')
    CONTRACT_CAPABILITY_TABLE=$(printf '%s\n' "$out" | awk '/^---capabilities---$/{f=1;next} /^---forbidden---$/{f=0} f')
    CONTRACT_FORBIDDEN_LIST=$(printf '%s\n' "$out" | awk '/^---forbidden---$/{f=1;next} f')
    return 0
}

# Names the given profile REQUIRES. The private checkout requires the union;
# the public tree requires only the public rows, because the mirror ships no
# private harness and demanding one there fails CI before a harness runs (#131).
contract_manifest_harness_names() {  # <profile>
    printf '%s\n' "$CONTRACT_HARNESS_TABLE" \
        | awk -F'\t' -v p="$1" '$1!="" && (p=="private" || $2=="public") {print $1}'
}

contract_min_cases() {  # <name>
    local v
    v=$(printf '%s\n' "$CONTRACT_HARNESS_TABLE" \
        | awk -F'\t' -v n="$1" '$1==n {print $3; exit}')
    printf '%s' "${v:-0}"
}

contract_declared_visibility() {  # <name>
    printf '%s\n' "$CONTRACT_HARNESS_TABLE" \
        | awk -F'\t' -v n="$1" '$1==n {print $2; exit}'
}

# ------------------------------------------------- the tier partition's counts
# The arithmetic behind bin/cctally-test-all's widen-to-the-whole-estate
# backstop, EXTRACTED so it can be driven directly (#650 item 3).
#
# It is extracted rather than left inline because the branch it feeds is
# unreachable through any manifest contract_manifest_load accepts: the manifest
# count counts LINES while the two other counts count IFS-split WORDS, and the
# name grammar above is exactly what makes those three readings agree for every
# accepted name. The branch stays — it is the backstop for a future divergence
# and it fails CLOSED into the full estate rather than refusing a run — and this
# seam is what lets a test reach it with a name the loader would now refuse,
# instead of pretending a valid manifest reaches it.
#
# Pathname expansion is disabled around both word-split walks and restored only
# if this function turned it off, so a caller that had already disabled it is
# not silently handed it back on.
CONTRACT_PARTITION_MANIFEST_COUNT=0
CONTRACT_PARTITION_SELECTED_COUNT=0
CONTRACT_PARTITION_OMITTED_COUNT=0

contract_partition_counts_agree() {  # <names, newline-sep> <selected> <omitted>
    local names=$1 selected=$2 omitted=$3 n restore_glob=0
    CONTRACT_PARTITION_MANIFEST_COUNT=0
    CONTRACT_PARTITION_SELECTED_COUNT=0
    CONTRACT_PARTITION_OMITTED_COUNT=0
    while IFS= read -r n; do
        [ -n "$n" ] || continue
        CONTRACT_PARTITION_MANIFEST_COUNT=$((CONTRACT_PARTITION_MANIFEST_COUNT + 1))
    done <<EOF
$names
EOF
    case $- in *f*) ;; *) restore_glob=1; set -f ;; esac
    for n in $selected; do
        CONTRACT_PARTITION_SELECTED_COUNT=$((CONTRACT_PARTITION_SELECTED_COUNT + 1))
    done
    for n in $omitted; do
        CONTRACT_PARTITION_OMITTED_COUNT=$((CONTRACT_PARTITION_OMITTED_COUNT + 1))
    done
    if [ "$restore_glob" = 1 ]; then set +f; fi
    [ "$((CONTRACT_PARTITION_SELECTED_COUNT + CONTRACT_PARTITION_OMITTED_COUNT))" \
        -eq "$CONTRACT_PARTITION_MANIFEST_COUNT" ]
}

# ------------------------------------------------------------------- admission

contract_admit_harnesses() {  # contract_admit_harnesses <repo_root> <profile>
    local root=$1 profile=$2 path base name ok=0 n exp_file act_file

    # The `.XXXXXX` suffix is required, not cosmetic. GNU coreutils rejects a
    # template carrying fewer than three consecutive X's and exits 1 BEFORE it
    # interprets -t, while BSD mktemp treats the argument as a prefix and
    # appends its own randomness — so a bare prefix works on macOS and fails on
    # every Linux lane. This matches bin/cctally-test-all's own LOGDIR template.
    #
    # Both allocations are fail-closed. Admission that cannot allocate its
    # scratch space must REFUSE, because the alternative is what this session
    # exists to close: an empty variable makes both redirections and both `comm`
    # invocations fail, the executable-bit loop read nothing, and the whole of
    # admission return 0 while verifying nothing at all.
    exp_file=$(mktemp -t cctally-contract-exp.XXXXXX) || exp_file=""
    if [ -z "$exp_file" ]; then
        contract_note "admission: could not allocate a scratch file for the manifest comparison (mktemp failed; check TMPDIR) — admission refuses rather than skip the comparison"
        contract_fail infrastructure admission-scratch-failed "" admission
        return 1
    fi
    act_file=$(mktemp -t cctally-contract-act.XXXXXX) || act_file=""
    if [ -z "$act_file" ]; then
        rm -f "$exp_file"
        contract_note "admission: could not allocate a scratch file for the on-disk harness listing (mktemp failed; check TMPDIR) — admission refuses rather than skip the comparison"
        contract_fail infrastructure admission-scratch-failed "" admission
        return 1
    fi

    # F27: enumerate EVERY matching file first. bin/cctally-test-all's old
    # discovery filtered on -x before recording, so a lost executable bit was
    # indistinguishable from a harness that was never there.
    for path in "$root"/bin/cctally-*-test; do
        [ -e "$path" ] || continue
        base=$(basename "$path")
        [ "$base" = "cctally-test-all" ] && continue
        name=${base#cctally-}; name=${name%-test}
        printf '%s\n' "$name"
    done | LC_ALL=C sort > "$act_file"
    contract_manifest_harness_names "$profile" | LC_ALL=C sort > "$exp_file"

    for n in $(comm -13 "$exp_file" "$act_file"); do
        contract_note "admission: harness '$n' is on disk but has no manifest row — add a row to tests/authoritative-test-manifest.json if it is intentional, or remove bin/cctally-${n}-test if it is accidental (the remote test wrapper materialises UNCOMMITTED working-tree files, so an unstaged harness is a routine cause)"
        contract_fail infrastructure manifest-unexpected-harness "$n"; ok=1
    done
    for n in $(comm -23 "$exp_file" "$act_file"); do
        contract_note "admission: manifest row '$n' has no bin/cctally-${n}-test — restore the file, or remove the row if the harness was deliberately retired"
        contract_fail infrastructure manifest-missing-harness "$n"; ok=1
    done

    # Only files that ARE expected are mode-checked, so a lost bit reports as a
    # mode problem rather than as an absence.
    while IFS= read -r n; do
        [ -n "$n" ] || continue
        path="$root/bin/cctally-${n}-test"
        if [ -e "$path" ] && [ ! -x "$path" ]; then
            contract_note "admission: bin/cctally-${n}-test exists but is not executable — chmod +x it (a lost mode bit is NOT an absent harness)"
            contract_fail infrastructure harness-not-executable "$n"; ok=1
        fi
    done < "$exp_file"

    rm -f "$exp_file" "$act_file"
    contract_admit_visibility "$root" "$profile" || ok=1
    return $ok
}

# Cross-check each declared row against the allowlist classifier, so a row that
# claims to be public while .mirror-allowlist excludes it (or the reverse) is
# caught here rather than at the next release snapshot. Private profile only:
# the public tree ships neither .mirror-allowlist nor .githooks/_match.py.
contract_admit_visibility() {  # <repo_root> <profile>
    local root=$1 profile=$2 ok=0 out line name declared actual
    [ "$profile" = "private" ] || return 0
    # The profile is ALREADY private here, which means .mirror-allowlist is
    # present — that file's presence is what selects the profile, so a third
    # guard on it would be unreachable and is deliberately absent. The tree
    # therefore claims to be private, so an absent classifier means a declared
    # check cannot run, and that is a refusal rather than a silent success.
    if [ ! -f "$root/.githooks/_match.py" ]; then
        contract_note "admission: this tree is the PRIVATE profile (.mirror-allowlist is present) but the allowlist classifier $root/.githooks/_match.py is absent, so the declared visibility of every manifest row cannot be checked — restore .githooks/_match.py, or remove .mirror-allowlist if this tree is meant to be the public subset"
        contract_fail infrastructure visibility-classifier-unavailable ""
        return 1
    fi

    out=$(CONTRACT_TABLE="$CONTRACT_HARNESS_TABLE" python3 - "$root" <<'PY'
import os
import sys

root = sys.argv[1]
sys.path.insert(0, os.path.join(root, ".githooks"))
import _match  # noqa: E402

rows = []
for line in os.environ.get("CONTRACT_TABLE", "").splitlines():
    if not line.strip():
        continue
    parts = line.split("\t")
    rows.append((parts[0], parts[1] if len(parts) > 1 else "public"))

paths = ["bin/cctally-%s-test" % name for name, _ in rows]
with open(os.path.join(root, ".mirror-allowlist"), encoding="utf-8") as fh:
    text = fh.read()
public = set(_match.classify(paths, allowlist_text=text)["public"])
for (name, declared), path in zip(rows, paths):
    actual = "public" if path in public else "private"
    if actual != declared:
        print("%s\t%s\t%s" % (name, declared, actual))
PY
    ) || {
        contract_note "admission: could not run the allowlist classifier at $root/.githooks/_match.py"
        contract_fail infrastructure visibility-classifier-failed ""
        return 1
    }

    while IFS=$'\t' read -r name declared actual; do
        [ -n "$name" ] || continue
        contract_note "admission: manifest row '$name' declares visibility '$declared' but .githooks/_match.py classifies bin/cctally-${name}-test as '$actual' — fix the row or fix .mirror-allowlist"
        contract_fail infrastructure visibility-drift "$name"; ok=1
    done <<EOF
$out
EOF
    return $ok
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

# ---------------------------------------------------- the normalized budget
#
# #630 S7. The metric already existed: contract_emit_outcome publishes
# `wallSeconds`, `passedCases` and their quotient. What did not exist was a
# threshold, a check or a classification.
#
# The threshold is COMMITTED, in tests/authoritative-runtime-budget.json, so a
# maintainer raising it does so in a commit a reviewer can see. That is the
# mechanism that stops ordinary estate growth from silently breaching it: the
# metric is normalized per case, so growing the estate does not move it and a
# breach means the tree became more expensive PER CASE.
#
# One fleet-wide number, with no host lookup at check time and no influence on
# scheduling. The cost of that choice is recorded rather than hidden: the fast
# host observes about 56 s per 1,000 cases, so 120 is nearly silent there. The
# budget's purpose is catching a step change, and a step change large enough to
# matter appears on both hosts.
CONTRACT_BUDGET_MAX=""     # the committed maximum, as a decimal string

contract_budget_load() {  # contract_budget_load <path>
    local path=$1 out rc
    out=$(python3 - "$path" <<'PY'
import json, math, sys


def no_dupes(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise ValueError("duplicate-key:%s" % k)
        seen.add(k)
    return dict(pairs)


try:
    with open(sys.argv[1], "rb") as handle:
        raw = handle.read().decode("utf-8")
except OSError as exc:
    print("unreadable:%s" % (exc.strerror or "unreadable",))
    sys.exit(1)
# UnicodeDecodeError is a ValueError, not an OSError. Read as bytes and decode
# explicitly so a non-UTF-8 budget file is refused with a cause the operator can
# act on, rather than escaping the handler and printing an empty cause beside a
# raw traceback.
except UnicodeDecodeError:
    print("unreadable:not UTF-8")
    sys.exit(1)

try:
    doc = json.loads(raw, object_pairs_hook=no_dupes)
except ValueError as exc:
    msg = str(exc)
    print(msg if msg.startswith("duplicate-key") else "unreadable:not JSON")
    sys.exit(1)

if not isinstance(doc, dict):
    print("unreadable:not an object")
    sys.exit(1)

allowed = {"schemaVersion", "metric", "maxSecondsPerThousandCases"}
extra = sorted(set(doc) - allowed)
if extra:
    print("unknown-key:%s" % extra[0])
    sys.exit(1)
missing = sorted(allowed - set(doc))
if missing:
    print("missing-key:%s" % missing[0])
    sys.exit(1)
if doc["schemaVersion"] != 1:
    print("schemaVersion:%r" % (doc["schemaVersion"],))
    sys.exit(1)
if doc["metric"] != "secondsPerThousandCases":
    print("metric:%r" % (doc["metric"],))
    sys.exit(1)
value = doc["maxSecondsPerThousandCases"]
# bool is an int subclass, so True would otherwise pass as 1.
if isinstance(value, bool) or not isinstance(value, (int, float)):
    print("maxSecondsPerThousandCases:%r" % (value,))
    sys.exit(1)
# FINITENESS IS TESTED BEFORE THE SIGN, and it is not defensive. Python's json
# parses the bare `NaN` and `Infinity` literals by default, and both survive a
# sign test: every comparison against a NaN is false, and infinity is not below
# zero. Either value then makes `raw > cap` false for every run that can exist,
# so no run could ever breach the budget — the mechanism would be silently
# disabled rather than loudly wrong. contract_budget_format would also raise
# OverflowError on int(inf) while rendering the breach that never comes.
if not math.isfinite(value) or value <= 0:
    print("maxSecondsPerThousandCases:%r" % (value,))
    sys.exit(1)
print(repr(float(value)))
PY
    ) ; rc=$?
    if [ $rc -ne 0 ]; then
        contract_note "runtime budget: $out — fix tests/authoritative-runtime-budget.json"
        contract_fail infrastructure runtime-budget-unavailable "${out%%:*}" admission
        return 1
    fi
    CONTRACT_BUDGET_MAX=$out
    return 0
}

# The ONE eligibility predicate, read twice: at admission, so a malformed or
# missing budget refuses the run in seconds rather than after twenty minutes,
# and again at the verdict, when CONTRACT_CLASS is final. Stated once so the two
# readings cannot drift apart.
#
# Keyed on the AUTHORITATIVE marker and deliberately NOT on CCTALLY_REMOTE_EXEC.
# .github/workflows/ci.yml's test-macos job sets CCTALLY_AUTHORITATIVE_RUN=1 and
# executes bin/cctally-test-all DIRECTLY, never through the wrapper, so a
# remote-marker predicate would leave the budget unenforced on exactly the run
# that has no reusable receipt to fall back on.
contract_budget_is_eligible() {
    contract_is_authoritative || return 1
    # `full` only: a --harness subset, the tier and linux-matrix each ran a
    # different estate, so their quotients answer a different question.
    [ "${COVERAGE_MODE:-}" = "full" ] || return 1
    # One failure must not produce two reason codes. A red run has fewer passed
    # cases, so the quotient inflates precisely when it carries no information.
    [ "${CONTRACT_CLASS:-none}" = "none" ] || return 1
    # The local escape hatch, refused HERE rather than by inheritance.
    # bin/cctally-test-remote's CCTALLY_TEST_LOCAL=1 path execs the command with
    # the caller's environment intact and unsets nothing, so a caller who
    # exported the authoritative marker alongside it would otherwise satisfy the
    # three tests above on a full local run. The wrapper also sanitizes the
    # markers it owns before that exec, so the two defences are independent.
    [ "${CCTALLY_TEST_LOCAL:-}" != "1" ] || return 1
    # The PRIVATE profile only. Two real invocation paths run an authoritative
    # `full` coverage mode over a PUBLIC tree, and the first four conditions
    # admitted both. bin/cctally-test-linux-matrix --profile public sets
    # CCTALLY_LINUX_MATRIX_RUN only for the private profile, so its public arm
    # resolves to the ordinary `full` mode while exporting the authoritative
    # marker unconditionally, and the release runbook invokes it at every cut.
    # .github/workflows/ci.yml's `test-pr` job sets the same marker, runs
    # bin/cctally-test-all with no arguments on ubuntu-latest, and is gated to
    # the PUBLIC MIRROR, so it is active on contributor and fork pull requests.
    # Both run a SMALLER estate on hardware this threshold never measured, so
    # the comparison is meaningless in both directions — and a breach on the
    # second would fail a contributor's pull request with a `product` verdict
    # blaming their tree for a hosted runner's slowness.
    #
    # CONTRACT_PROFILE is the right discriminator because it already means
    # exactly this: bin/cctally-test-all resolves it to `private` if and only if
    # .mirror-allowlist is present, and that file is unmatched by its own
    # patterns, so it is absent from every published public tree.
    [ "${CONTRACT_PROFILE:-}" = "private" ] || return 1
    return 0
}

# Renders operator-facing numbers readably: at most two decimals, with trailing
# zeros trimmed, so a breach reads `120.04 > 120` rather than `120.040000>120.0`.
#
# Two decimals is not unconditional. A threshold small enough to round to zero
# there — a fixture budget of 0.001, which every scratch estate in the suite
# uses — would render as `0`, and `454.55 > 0` states something that is not
# true of the file. Such a value falls back to Python's shortest round-trip
# repr, which is `0.001` rather than `0.001000`.
#
# DISPLAY ONLY. The comparison itself stays on the raw quotient computed by
# contract_budget_verdict, which is the whole point of that function; formatting
# here must never feed a decision.
contract_budget_format() {  # contract_budget_format <number>...
    CB_NUMS="$*" python3 -c '
import os

out = []
for token in os.environ["CB_NUMS"].split():
    try:
        value = float(token)
    except ValueError:
        out.append(token)
        continue
    if value == int(value):
        text = str(int(value))
    else:
        text = "%.2f" % value
        if float(text) == 0.0:
            text = repr(value)
        else:
            text = text.rstrip("0").rstrip(".")
    out.append(text or "0")
print(" ".join(out))
'
}

# Prints "<raw> ok" or "<raw> over", and fails loudly on an argument that is not
# a number rather than reporting a verdict it did not compute.
#
# The RAW quotient, not the `secondsPerThousandCases` the record publishes:
# that value is rounded to one decimal for display, and a threshold decided by a
# display artifact is one nobody can reason about — 120.04 rounds to 120.0 and
# would pass a cap of 120 it genuinely exceeds. STRICTLY greater, so a run
# landing exactly on the budget passes: the budget exists to catch a step
# change, and failing an exactly-conforming run buys nothing.
contract_budget_verdict() {  # contract_budget_verdict <wall> <cases> <max>
    CB_WALL="$1" CB_CASES="$2" CB_MAX="$3" python3 -c '
import os, sys

try:
    wall = float(os.environ["CB_WALL"])
    cases = float(os.environ["CB_CASES"])
    cap = float(os.environ["CB_MAX"])
except (KeyError, ValueError):
    sys.exit(1)
if cases <= 0 or cap <= 0:
    sys.exit(1)
raw = wall / cases * 1000.0
print("%.6f %s" % (raw, "over" if raw > cap else "ok"))
'
}

# Called once the class is final and before the verdict is printed, so the
# printed verdict and the exit code agree. Never changes an existing verdict:
# the eligibility predicate above refuses to speak unless the class is still
# `none`.
contract_check_runtime_budget() {
    local cases verdict shown raw_shown max_shown
    contract_budget_is_eligible || return 0
    contract_freeze_wall_seconds
    if [ -z "$CONTRACT_BUDGET_MAX" ]; then
        contract_note "runtime budget: no committed threshold was loaded for this run, so the budget could not be evaluated; a budget that could not be evaluated is never recorded as one that was satisfied"
        contract_fail infrastructure runtime-budget-unavailable "threshold" budget
        return 0
    fi
    if [ "${CONTRACT_PYTEST_UNPARSED:-0}" = "1" ]; then
        contract_note "runtime budget: pytest's passed-item count was unreadable, so the denominator could not be assembled; the budget is recorded as unavailable rather than satisfied"
        contract_fail infrastructure runtime-budget-unavailable "denominator" budget
        return 0
    fi
    cases=$((CONTRACT_PASSED + CONTRACT_PYTEST_PASSED))
    if [ "$cases" -le 0 ]; then
        contract_note "runtime budget: the run passed no cases, so it supports no claim about cost per case"
        contract_fail infrastructure runtime-budget-unavailable "denominator" budget
        return 0
    fi
    verdict=$(contract_budget_verdict "$CONTRACT_WALL_SECONDS" "$cases" "$CONTRACT_BUDGET_MAX") || {
        contract_note "runtime budget: the normalized metric could not be computed from wallSeconds=$CONTRACT_WALL_SECONDS passedCases=$cases max=$CONTRACT_BUDGET_MAX"
        contract_fail infrastructure runtime-budget-unavailable "metric" budget
        return 0
    }
    case "$verdict" in
        *" over")
            # Display only, and computed AFTER the verdict: the comparison above
            # ran on the raw quotient, and these two numbers exist so the
            # operator reads `120.04 > 120` instead of `120.040000>120.0`.
            shown=$(contract_budget_format "${verdict%% *}" "$CONTRACT_BUDGET_MAX")
            raw_shown=${shown%% *}
            max_shown=${shown##* }
            contract_note "runtime budget: $raw_shown seconds per 1,000 cases exceeds the committed maximum of $max_shown (tests/authoritative-runtime-budget.json). This is a NORMALIZED measure, so ordinary estate growth does not move it — a breach means the tree became more expensive PER CASE. Raise the committed budget deliberately if the cost is intended."
            contract_fail product runtime-budget-exceeded "$raw_shown > $max_shown" budget ;;
    esac
    return 0
}

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
    CONTRACT_MERGE_RECORD="${MERGE_RECORD:-}" \
    CONTRACT_COV_RESOLVED="${COVERAGE_RESOLVED:-0}" \
    CONTRACT_COV_MODE="${COVERAGE_MODE:-full}" \
    CONTRACT_COV_SELECTED="${COVERAGE_SELECTED:-}" \
    CONTRACT_COV_OMITTED="${COVERAGE_OMITTED:-}" \
    CONTRACT_COV_PYTEST="${COVERAGE_PYTEST:-full}" \
    CONTRACT_COV_TIER_BASE="${COVERAGE_TIER_BASE_OID:-}" \
    CONTRACT_COV_TIER_HEAD="${COVERAGE_TIER_HEAD_OID:-}" \
    CONTRACT_COV_TIER_COMMITTED="${COVERAGE_TIER_COMMITTED_PATHS:-0}" \
    CONTRACT_COV_TIER_WORKTREE="${COVERAGE_TIER_WORKTREE_PATHS:-0}" \
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
    # The resolved tier basis, so the selection is reproducible from the RECORD
    # rather than only from the shell history of whoever invoked it. Present in
    # tier mode alone: no other mode has a basis, and an object of empty strings
    # would describe one that does not exist.
    if doc["coverage"]["mode"] == "tier-fast":
        doc["coverage"]["tierBasis"] = {
            "baseOid": os.environ.get("CONTRACT_COV_TIER_BASE", ""),
            "headOid": os.environ.get("CONTRACT_COV_TIER_HEAD", ""),
            "committedPaths": _int("CONTRACT_COV_TIER_COMMITTED"),
            "worktreePaths": _int("CONTRACT_COV_TIER_WORKTREE"),
        }

if os.environ.get("CONTRACT_MERGE_RECORD"):
    doc["coverage"]["mergeSelection"] = json.loads(os.environ["CONTRACT_MERGE_RECORD"])

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
# other terminal path, because the carrier requires the observed status and the
# record to corroborate each other: without one the wrapper saw status 2, found
# nothing to read, and rewrote a configuration error into a `transport`
# infrastructure failure at exit 3. The record is what keeps 2 meaning 2 to the
# caller, and the corroboration rule stays intact rather than gaining a
# status-2 exemption that would let an unverified 2 read as a verdict.
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
# bin/cctally-test-remote exports it for the canonical command. Setting it can
# only make a run STRICTER, and unsetting it degrades a refusal to an
# `incomplete` classification and never to green, so it is safe by construction
# and needs no anti-tamper guard.
contract_is_authoritative() { [ "${CCTALLY_AUTHORITATIVE_RUN:-}" = "1" ]; }

# --- externally supplied incomplete reason (#529 S6, exception X1) -----------
#
# The classification schema is consumed by both the aggregator and the wrapper
# rather than duplicated in each, so a caller that KNOWS it degraded the run
# says so in this vocabulary instead of inventing a second taxonomy. Today
# exactly one caller does: bin/cctally-test-remote's CCTALLY_TEST_LOCAL=1 branch,
# when the machine genuinely has no `agentmem` and the run therefore cannot
# execute the gated tests the remote path executes.
#
# The allowlist is STRICT, and the empty value is rejected explicitly rather
# than falling through as "nothing set". An unguarded empty read is the selector
# failure class this repository has already paid for twice — an empty `--harness`
# value selected nobody and reported the same incompleteness an all-passing
# subset reports, and an empty pattern elsewhere matched everything. The kernel
# now reads a reason CODE from the environment, so both mistakes are refused at
# admission rather than recorded as a degradation nobody chose.
# The accepted set is the CASE ARMS below, not this string. This exists only so
# the refusal can list what it would have accepted. Emitting the reason through
# a variable would satisfy the allowlist while defeating the registry guard,
# which asserts that every declared code appears as a bare literal at some
# emission site — a variable is exactly the shape that lets a typo through.
_CONTRACT_EXTERNAL_REASONS='agentmem-unavailable-local'

contract_admit_external_reason() {
    local reason
    # Unset is the ordinary case and says nothing. `+set` distinguishes it from
    # a variable that is set to the empty string, which is the case below.
    [ -n "${CCTALLY_TEST_EXTERNAL_INCOMPLETE+set}" ] || return 0
    reason=$CCTALLY_TEST_EXTERNAL_INCOMPLETE
    case "$reason" in
        agentmem-unavailable-local)
            # Recorded, NOT fatal. The run continues and completes; what changes
            # is that it can never be green, exactly as a deliberate subset can
            # never be green.
            contract_fail incomplete agentmem-unavailable-local "" admission ;;
        '')
            contract_usage_abort "CCTALLY_TEST_EXTERNAL_INCOMPLETE is set to the empty value; an empty reason names no degradation and must not read as one" CCTALLY_TEST_EXTERNAL_INCOMPLETE ;;
        *)
            contract_usage_abort "CCTALLY_TEST_EXTERNAL_INCOMPLETE='$reason' is not an accepted external incomplete reason (accepted: $_CONTRACT_EXTERNAL_REASONS)" CCTALLY_TEST_EXTERNAL_INCOMPLETE ;;
    esac
    return 0
}

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

# Records EVERY probe into CONTRACT_CAPABILITIES, then fails on the hard ones.
# Reads the table from a heredoc rather than a pipe so the recorded results
# survive in this shell (a pipeline would run the loop in a subshell).
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
            '"policy"')
                # Asserted against the EFFECTIVE policy, not hardcoded:
                # bin/cctally-test-remote pins `required` for every remote
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
    done <<EOF
$CONTRACT_CAPABILITY_TABLE
EOF
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
contract_admit_regeneration() {
    local var ok=0 value
    while IFS= read -r var; do
        [ -n "$var" ] || continue
        case "$var" in
            *[!A-Za-z0-9_]*)
                contract_note "manifest: forbiddenRegeneration entry '$var' is not a valid environment variable name"
                contract_fail infrastructure manifest-unknown-key "$var"
                ok=1; continue ;;
        esac
        if eval "[ \"\${${var}+set}\" = set ]"; then
            eval "value=\${${var}}"
            contract_note "admission: '$var' is present in the environment (value: '${value}') — a golden-regeneration variable converts a comparison into an adoption, so it must never ride into a run. Presence is poison regardless of value; unset it, or invoke the single harness directly, which this guard deliberately does not touch."
            contract_fail infrastructure regeneration-enabled "$var"
            ok=1
        fi
    done <<EOF
$CONTRACT_FORBIDDEN_LIST
EOF
    return $ok
}

# #648 D12. WHICH RUN MODES RUN THE COMPLETE ESTATE CHECK.
#
# A predicate rather than an inline condition, for two reasons. It is a contract
# rule and belongs beside the other admission rules; and `CCTALLY_LINUX_MATRIX_RUN`
# refuses on `uname -s` outside plan mode, so a condition written inline in the
# aggregator could only ever be exercised on one platform.
#
# FAIL CLOSED on the mode axis: everything requires the check except the one
# mode that has argued for an exemption. `--harness` is already unconditionally
# `deliberate-subset` and exits 3 whatever happens, so the complete check adds
# no protection against a green result there, and it would materially slow the
# targeted test-driven loop this repository runs constantly. It is available on
# that path as an explicit diagnostic. A mode added later and not taught to this
# predicate inherits the checking behaviour, never the exemption.
#
# Plan mode is absent because it never reaches here: it exits before admission.
contract_estate_check_required() {  # <coverage_mode> <opt_in>
    case "$1" in
        subset) [ "${2:-0}" = 1 ] ;;
        *)      return 0 ;;
    esac
}

# #648 D7. THE PYTEST EXECUTION DECLARATION, VALIDATED AT ADMISSION.
#
# The artifact's `pytestExecution` block is the single place the two legs are
# declared, and the helper proves it partitions the recorded estate: every
# selector matches at least one node, every required leg's share is non-empty,
# the shares are disjoint and their union is the whole estate. A selector that
# matches nothing is therefore an ADMISSION failure (acceptance criterion 17)
# rather than a leg that silently runs less, which is what the retired
# `[ -f ... ]` guards in the aggregator produced.
#
# `--validate-legs` and not `--plan-legs`, because admission precedes `LOGDIR`
# and has nowhere to write the per-leg node files. Creating a directory here
# would leave one behind on every refusal.
#
# The selectors are captured into CONTRACT_ESTATE_SELECTORS as `kind<TAB>value`
# lines. The aggregator builds BOTH legs' argv from them: a `file` selector
# becomes `--ignore=` on the bulk leg, a `node` selector becomes `--deselect=`,
# and every selector is a target of the serial leg.
CONTRACT_ESTATE_SELECTORS=""

contract_admit_estate_plan() {  # contract_admit_estate_plan <repo_root> <profile>
    local root=$1 profile=$2 out rc tab=$'\t' f1 f2 f3 f4
    CONTRACT_ESTATE_SELECTORS=""
    out=$(python3 "$root/bin/_lib_test_estate.py" \
        --repo "$root" --profile "$profile" --validate-legs 2>&1)
    rc=$?
    if [ "$rc" -ne 0 ] || ! printf '%s\n' "$out" | grep -q "^end${tab}ok$"; then
        contract_note "admission: the pytest execution declaration does not expand against the recorded estate (exit $rc) — $(printf '%s' "$out" | tail -n 3 | tr '\n' ' ')"
        contract_fail infrastructure estate-partition-invalid pytestExecution admission
        return 1
    fi
    while IFS="$tab" read -r f1 f2 f3 f4; do
        [ "$f1" = selector ] || continue
        CONTRACT_ESTATE_SELECTORS="${CONTRACT_ESTATE_SELECTORS}${f3}${tab}${f4}"$'\n'
    done <<EOF
$out
EOF
    return 0
}

# ---------------------------------------------------------------- the estate
#
# #648 D6. The committed estate artifact is compared against this tree's own
# live derivation, and every artifact transition against committed history is
# checked for authorization. The comparison itself lives in the public helper
# `bin/_lib_test_estate.py`, which returns TAGS and renders no verdict; this
# function is where a tag becomes one of the ten reason codes registered above.
#
# Every literal is written out at a bare `contract_fail` call rather than
# assembled into a variable, because `test_every_emitted_reason_code_is_in_the_-
# registry` and its dead-code twin scan these sources with a regular expression.
# A `contract_fail "$class" "$code"` would emit correctly and be invisible to
# both directions of that scan, which is the failure mode the scan exists to
# prevent. The repetition is the point.
#
# RETURN VALUE, three-valued rather than boolean:
#   0  the record and the tree agree, and every transition is authorized
#   1  drift or an unauthorized transition — the caller aborts
#   2  an inability on a NON-authoritative run — recorded incomplete, and the
#      run continues and can never be green (D12)
# An inability on an authoritative run returns 1: such a run refuses to start
# rather than verify less than it claims.
contract_admit_estate() {  # contract_admit_estate <repo_root> <profile>
    local root=$1 profile=$2 report errfile rc ok=0 saw_end=0
    # The terminator's PAYLOAD and the record kinds are both read, because a
    # reader that ignored either would fail OPEN on a report kind it does not
    # know yet: `end problems` beside no recognised problem record, or a future
    # record kind falling through a `case` with no default arm. Neither is
    # producible by today's `render_report`, which is exactly why neither would
    # be noticed the day it becomes producible.
    local end_state="" saw_problem=0 saw_unknown=0
    # FIVE names, not six. The longest record this loop acts on carries exactly
    # five fields; the `uncovered` record carries six and is deliberately not
    # acted on, so its sixth field lands in `f5` alongside the fifth and nothing
    # reads either. A sixth name would be assigned and never used.
    local f1 f2 f3 f4 f5

    report=$(mktemp -t cctally-estate-report.XXXXXX) || report=""
    errfile=$(mktemp -t cctally-estate-stderr.XXXXXX) || errfile=""
    if [ -z "$report" ] || [ -z "$errfile" ]; then
        rm -f "$report" "$errfile"
        contract_note "admission: could not allocate a scratch file for the estate check (mktemp failed; check TMPDIR) — admission refuses rather than skip the comparison"
        contract_fail infrastructure admission-scratch-failed "" admission
        return 1
    fi

    python3 "$root/bin/_lib_test_estate.py" \
        --repo "$root" --profile "$profile" --check \
        > "$report" 2> "$errfile"
    rc=$?

    # The `end` line is load-bearing in the FAIL-CLOSED direction. A checker
    # killed halfway prints a well-formed prefix and no terminator, and a reader
    # that judged only on the records it saw would read a truncated report as
    # agreement. Absence of the terminator is therefore an inability in its own
    # right, independent of the exit status.
    if [ "$rc" -ne 0 ]; then
        contract_note "admission: the estate checker exited $rc — $(tail -n 3 "$errfile" 2>/dev/null | tr '\n' ' ')"
    fi

    while IFS=$'\t' read -r f1 f2 f3 f4 f5; do
        case "$f1" in
        end)
            saw_end=1
            end_state=$f2
            ;;
        note)
            contract_note "$f2"
            ;;
        row|uncovered|version|profile|leg|selector)
            : # diagnostics the operator reads in the report, not verdict input
            ;;
        inability)
            saw_problem=1
            case "$f2" in
            artifact-unreadable)
                contract_note "admission: the estate record could not be read — $f3"
                if contract_is_authoritative; then
                    contract_fail infrastructure estate-artifact-unreadable "$f2" admission
                    ok=1
                else
                    contract_fail incomplete estate-artifact-unreadable "$f2" admission
                    if [ "$ok" -eq 0 ]; then ok=2; fi
                fi
                ;;
            partition-invalid)
                contract_note "admission: the public/private partition does not describe this tree — $f3"
                if contract_is_authoritative; then
                    contract_fail infrastructure estate-partition-invalid "$f2" admission
                    ok=1
                else
                    contract_fail incomplete estate-partition-invalid "$f2" admission
                    if [ "$ok" -eq 0 ]; then ok=2; fi
                fi
                ;;
            *)
                # Every other inability, discovery included. A tag this shell
                # does not recognise lands here rather than being ignored: an
                # unknown inability is still an inability.
                contract_note "admission: the live estate could not be derived — $f3"
                if contract_is_authoritative; then
                    contract_fail infrastructure estate-discovery-failed "$f2" admission
                    ok=1
                else
                    contract_fail incomplete estate-discovery-failed "$f2" admission
                    if [ "$ok" -eq 0 ]; then ok=2; fi
                fi
                ;;
            esac
            ;;
        unauthorized)
            saw_problem=1
            contract_note "admission: $f2 harmful estate transition(s) are not covered by a declaration, beginning with $f4 '$f5' in the $f3 profile — add an entry to the matching tests/authoritative-estate-retirements*.json naming the rows and the predecessor digest, or restore what was removed"
            contract_fail infrastructure estate-shrink-unauthorized "$f5" admission
            ok=1
            ;;
        finding)
            saw_problem=1
            # $f2 direction, $f3 axis, $f4 count, $f5 the first row.
            case "$f2/$f3" in
            unexpected/pytestNodes)
                contract_note "admission: $f4 pytest node identifier(s) are collected here and absent from the estate record, beginning with '$f5' — regenerate the artifacts, or find out what added them"
                contract_fail infrastructure manifest-unexpected-pytest-node "$f5" admission
                ;;
            missing/pytestNodes)
                contract_note "admission: $f4 pytest node identifier(s) are in the estate record and are NOT collected here, beginning with '$f5' — a lost test is exactly what this record exists to catch"
                contract_fail infrastructure manifest-missing-pytest-node "$f5" admission
                ;;
            unexpected/frontendTests)
                contract_note "admission: $f4 frontend test(s) are collected here and absent from the estate record, beginning with '$f5'"
                contract_fail infrastructure manifest-unexpected-frontend-test "$f5" admission
                ;;
            missing/frontendTests)
                contract_note "admission: $f4 frontend test(s) are in the estate record and are NOT collected here, beginning with '$f5'"
                contract_fail infrastructure manifest-missing-frontend-test "$f5" admission
                ;;
            unexpected/suppressions)
                contract_note "admission: $f4 suppression(s) are in this tree and absent from the estate record, beginning with '$f5' — a new suppression is the harmful direction on this axis"
                contract_fail infrastructure manifest-unexpected-suppression "$f5" admission
                ;;
            missing/suppressions)
                contract_note "admission: $f4 suppression(s) are in the estate record and are absent from this tree, beginning with '$f5' — removing one is free, so regenerate the artifacts"
                contract_fail infrastructure manifest-missing-suppression "$f5" admission
                ;;
            *)
                contract_note "admission: the estate checker reported a finding this contract does not recognise ($f2 on $f3) — treated as an inability rather than ignored"
                contract_fail infrastructure estate-discovery-failed "$f3" admission
                ;;
            esac
            ok=1
            ;;
        *)
            # No default arm at all is how a future record kind gets DROPPED.
            # Reported once however many such records arrive, because the
            # operator needs the first one named and not a hundred copies of
            # the same reason.
            if [ "$saw_unknown" -eq 0 ]; then
                saw_unknown=1
                contract_note "admission: the estate checker emitted a record kind this contract does not recognise ('$f1') — an unknown record is an inability, never something to ignore"
                if contract_is_authoritative; then
                    contract_fail infrastructure estate-discovery-failed "unknown-record" admission
                    ok=1
                else
                    contract_fail incomplete estate-discovery-failed "unknown-record" admission
                    if [ "$ok" -eq 0 ]; then ok=2; fi
                fi
            fi
            ;;
        esac
    done < "$report"

    if [ "$saw_end" -ne 1 ]; then
        contract_note "admission: the estate checker produced no terminated report (exit $rc); a truncated report is an inability, never an agreement"
        if contract_is_authoritative; then
            contract_fail infrastructure estate-discovery-failed "no-report" admission
            ok=1
        else
            contract_fail incomplete estate-discovery-failed "no-report" admission
            if [ "$ok" -eq 0 ]; then ok=2; fi
        fi
    elif [ "$end_state" = problems ] && [ "$saw_problem" -ne 1 ]; then
        contract_note "admission: the estate checker terminated its report with 'problems' and named none this contract recognises (exit $rc); the terminator's payload is read rather than assumed, so a problem this shell cannot name is an inability"
        if contract_is_authoritative; then
            contract_fail infrastructure estate-discovery-failed "unnamed-problems" admission
            ok=1
        else
            contract_fail incomplete estate-discovery-failed "unnamed-problems" admission
            if [ "$ok" -eq 0 ]; then ok=2; fi
        fi
    fi

    rm -f "$report" "$errfile"
    return $ok
}

# ------------------------------------------------------ harness classification
#
# The summary grammar is EXPLICIT because the estate already has two spacing
# variants: most harnesses print three spaces between the counters and
# bin/cctally-rederive-test:553 prints two. A literal `passed: N   failed: M`
# parser would break the second, which would contradict "no harness body is
# edited".
_CONTRACT_SUMMARY_RE='^passed:[[:space:]]+([0-9]+)[[:space:]]+failed:[[:space:]]+([0-9]+)[[:space:]]*$'

CONTRACT_LAST_PASSED=0
CONTRACT_LAST_FAILED=0
CONTRACT_LAST_SUMMARY_OK=0

# A harness passed only when all four hold: exit 0, a parseable final summary,
# `failed == 0`, and `passed + failed >= minCases`.
contract_classify_harness() {  # <name> <logfile> <exitfile> <min_cases>
    local name=$1 log=$2 exitfile=$3 min=${4:-0} rc line p f
    CONTRACT_LAST_PASSED=0
    CONTRACT_LAST_FAILED=0
    CONTRACT_LAST_SUMMARY_OK=0
    [ -n "$min" ] || min=0

    # Two causally-prior overrides, ahead of the ordinary class precedence: a
    # process that never finished cannot be judged on what it printed. Without
    # them a killed harness matches BOTH the signal row and the missing-summary
    # row, and precedence resolves it to `incomplete` when the true cause is
    # that the process died.
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

    line=""
    if [ -f "$log" ]; then
        # Detect the binary shape explicitly before asking grep to find the
        # summary. BSD grep emits the matching NUL-bearing line while GNU grep
        # may replace it with a binary-file diagnostic (or no stdout at all),
        # which made the same log classify differently across the remote Mac
        # gate and Linux CI. `tr | cmp` is byte-oriented under C locale and is
        # portable across both estates: removing a NUL changes the stream iff
        # the log carries one.
        if ! LC_ALL=C tr -d '\000' < "$log" | cmp -s - "$log"; then
            contract_fail infrastructure binary-log "$name" harness
            return 0
        fi
        line=$(grep -E "$_CONTRACT_SUMMARY_RE" "$log" 2>/dev/null | tail -1)
    fi
    if [ -z "$line" ]; then
        contract_fail incomplete summary-unreadable "$name" harness
        return 0
    fi
    p=$(printf '%s\n' "$line" | sed -E "s/$_CONTRACT_SUMMARY_RE/\1/")
    f=$(printf '%s\n' "$line" | sed -E "s/$_CONTRACT_SUMMARY_RE/\2/")
    case "$p" in
        ''|*[!0-9]*)
            contract_fail infrastructure binary-log "$name" harness
            return 0 ;;
    esac
    case "$f" in
        ''|*[!0-9]*)
            contract_fail infrastructure binary-log "$name" harness
            return 0 ;;
    esac
    # ShellCheck: these sourced-library results are consumed by cctally-test-all.
    # shellcheck disable=SC2034
    CONTRACT_LAST_PASSED=$p
    # ShellCheck: these sourced-library results are consumed by cctally-test-all.
    # shellcheck disable=SC2034
    CONTRACT_LAST_FAILED=$f
    # ShellCheck: these sourced-library results are consumed by cctally-test-all.
    # shellcheck disable=SC2034
    CONTRACT_LAST_SUMMARY_OK=1
    CONTRACT_PASSED=$((CONTRACT_PASSED + p))
    CONTRACT_FAILED=$((CONTRACT_FAILED + f))

    if [ "$f" -gt 0 ]; then
        contract_fail product harness-failed "$name" harness
        # Exit 0 while reporting failures is a mismatch on top of the product
        # cause; BOTH are retained, and precedence resolves the class.
        if [ "$rc" -eq 0 ]; then
            contract_fail incomplete exit-summary-mismatch "$name" harness
        fi
    elif [ "$rc" -ne 0 ]; then
        # No harness in the estate has a legitimate nonzero-success contract,
        # so this row carries no false positives.
        contract_fail incomplete exit-summary-mismatch "$name" harness
    fi
    if [ $((p + f)) -lt "$min" ]; then
        contract_fail incomplete case-floor-unmet "$name" harness
    fi
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
            contract_note "pytest: the $subject leg's passed-item count could not be read from its log, so this run's passed-case total and normalized metric are recorded as null; on a run the runtime budget is eligible to judge that missing denominator is itself runtime-budget-unavailable (infrastructure, exit 3), and on every other run the verdict is unaffected"
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

# --------------------------------------------------------------- the carrier
#
# bin/cctally-test-remote reads the aggregator's record and turns it into the
# ONE object the caller sees on stdout. When there is no usable record the
# wrapper must state that itself rather than let a damaged carrier read as a
# verdict — a `transport`-phase infrastructure outcome.
CONTRACT_CARRIER_EMITTED=0

contract_carrier_synth() {  # <code> [detail]
    CONTRACT_SYNTH_CODE="$1" CONTRACT_SYNTH_DETAIL="${2:-}" python3 -c '
import json, os
print(json.dumps({
    "schemaVersion": 1,
    "outcome": "fail",
    "failureClass": "infrastructure",
    "exitCode": 3,
    "reasons": [{
        "code": os.environ["CONTRACT_SYNTH_CODE"],
        "phase": "transport",
        "subject": os.environ.get("CONTRACT_SYNTH_DETAIL", ""),
    }],
    "capabilities": {},
    "totals": {"passed": 0, "failed": 0},
}, sort_keys=True))
'
    # ShellCheck: the remote wrapper reads this sourced-library state.
    # shellcheck disable=SC2034
    # ShellCheck: the remote wrapper reads this sourced-library state.
    # shellcheck disable=SC2034
    CONTRACT_CARRIER_EMITTED=1
    return 3
}

# Prints exactly one object on stdout and returns the exit code the caller
# should propagate. A record that parses but disagrees with the observed exit
# status is rejected too: the two must corroborate, or neither is trustworthy.
contract_carrier_emit() {  # <observed_exit> <local_record_path>
    local rc
    CONTRACT_OBSERVED_RC="$1" CONTRACT_RECORD="$2" python3 -c '
import json, os, sys

VALID = {"none", "product", "infrastructure", "incomplete"}
# CLOSED, and extended deliberately. A reason whose phase is not listed here is
# rewritten to outcome-record-malformed — infrastructure at exit 3 — so a new
# phase must be admitted here before anything emits it, or the reason silently
# loses both its class and its exit code at the wrapper boundary (#630 S7).
PHASES = {"admission", "harness", "pytest", "transport", "budget"}


def synth(code, detail=""):
    print(json.dumps({
        "schemaVersion": 1,
        "outcome": "fail",
        "failureClass": "infrastructure",
        "exitCode": 3,
        "reasons": [{"code": code, "phase": "transport", "subject": detail}],
        "capabilities": {},
        "totals": {"passed": 0, "failed": 0},
    }, sort_keys=True))
    sys.exit(3)


try:
    observed = int(os.environ.get("CONTRACT_OBSERVED_RC", ""))
except ValueError:
    synth("outcome-exit-unreadable", os.environ.get("CONTRACT_OBSERVED_RC", ""))

try:
    raw = open(os.environ.get("CONTRACT_RECORD", ""), encoding="utf-8").read()
except OSError:
    synth("outcome-record-missing")

if not raw.strip():
    synth("outcome-record-missing")

try:
    doc = json.loads(raw)
except ValueError:
    synth("outcome-record-malformed", "unparseable")

if not isinstance(doc, dict):
    synth("outcome-record-malformed", "not-an-object")
if doc.get("schemaVersion") != 1:
    synth("outcome-record-malformed", "schemaVersion=%r" % (doc.get("schemaVersion"),))
if doc.get("failureClass") not in VALID:
    synth("outcome-record-malformed", "failureClass=%r" % (doc.get("failureClass"),))
reasons = doc.get("reasons")
if not isinstance(reasons, list):
    synth("outcome-record-malformed", "reasons")
for row in reasons:
    if not isinstance(row, dict) or row.get("phase") not in PHASES:
        synth("outcome-record-malformed", "reason-phase")
if doc.get("exitCode") != observed:
    synth("outcome-exit-mismatch",
          "record=%r observed=%d" % (doc.get("exitCode"), observed))

print(json.dumps(doc, sort_keys=True))
sys.exit(observed if 0 <= observed <= 255 else 3)
'
    rc=$?
    # ShellCheck: the remote wrapper reads this sourced-library state.
    # shellcheck disable=SC2034
    CONTRACT_CARRIER_EMITTED=1
    return $rc
}
