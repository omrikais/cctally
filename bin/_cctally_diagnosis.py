"""`cctally explain` — CLI parsing, terminal rendering and the wire adapter.

`diagnosis_to_wire` is the ONE serializer. The CLI and `GET /api/diagnosis`
both call it, which is what turns "the two surfaces agree" from a promise
into a byte comparison over `canonical_projection`.

Anonymization lives here rather than in either surface, for the same reason:
a project alias assigned at render time would differ between the terminal and
the route, and the equivalence check would then be comparing two different
privacy decisions.

Spec: docs/superpowers/specs/2026-08-19-620-s2-on-demand-diagnosis.md §4
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import sqlite3
import sys
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import _lib_diagnosis as kernel
from _cctally_core import _command_as_of
from _lib_json_envelope import stamp_schema_version

EXPLAIN_SCHEMA_VERSION = 1

# Paths the canonical projection drops. `measuredAt` is the instant of the
# read and legitimately differs between two surfaces reading the same facts;
# the rest are presentation-only.
CANONICAL_EXCLUDED_PATHS: tuple[str, ...] = (
    "measuredAt",
    "notes",
    "window.label",
    "results[].contributors[].subjectLabel",
    "results[].classes[].rows[].subjectLabel",
)

# The projection is driven by the published constant itself, so the two cannot
# disagree. Dropping every key NAMED `label` at every depth also removed
# `constants.registry[].label`, which is a fact of the contract and not a
# presentation detail, and Task 13 proves byte-equality through this
# projection.
_EXCLUDED_PATHS = frozenset(CANONICAL_EXCLUDED_PATHS)


def _cctally():
    return sys.modules["cctally"]


def _sources():
    return _cctally()._load_sibling("_cctally_diagnosis_sources")


# --- anonymization ------------------------------------------------------

def _alias_map(rows: Sequence[Any], prefix: str) -> dict[str, str]:
    """Deterministic response-local aliases, by descending USD then key.

    Response-local means the alias means nothing outside this one report,
    which is the property that makes it safe to paste. Ordering by cost keeps
    `project-1` the largest contributor in every rendering of the same facts.
    """
    ranked = sorted(
        {(r.subject_key, _usd(r)) for r in rows},
        key=lambda pair: (-pair[1], pair[0]),
    )
    return {key: f"{prefix}-{index}"
            for index, (key, _usd_value) in enumerate(ranked, start=1)}


def _usd(row: Any) -> float:
    value = row.observed_usd.value
    return float(value) if isinstance(value, (int, float)) else 0.0


def _build_alias_maps(result: Any) -> dict[str, dict[str, str]]:
    every_row = list(result.contributors)
    for class_result in result.classes:
        for row in class_result.rows:
            if row not in every_row:
                every_row.append(row)
    return {
        "project": _alias_map(
            [r for r in every_row if r.subject_kind == "project"], "project"),
        "session": _alias_map(
            [r for r in every_row if r.subject_kind == "session"], "session"),
    }


def _display_label(row: Any, aliases: Mapping[str, Mapping[str, str]],
                   *, reveal_projects: bool) -> str:
    """The label a person reads.

    Projects are aliased by default. `--reveal-projects` exposes the derived
    display label only — never a filesystem path, which is why the basename
    is taken here rather than the raw key. Sessions stay opaque in EVERY
    mode: a session identifier is not a name a person chose, and revealing
    it buys nothing.
    """
    if row.subject_kind == "project":
        if not reveal_projects:
            return aliases["project"].get(row.subject_key, row.subject_key)
        raw = row.subject_label or row.subject_key
        return raw.rstrip("/").rsplit("/", 1)[-1] or raw
    if row.subject_kind == "session":
        return aliases["session"].get(row.subject_key, row.subject_key)
    return row.subject_label or row.subject_key


def _display_key(row: Any, aliases: Mapping[str, Mapping[str, str]]) -> str:
    """The key published on the wire.

    A project bucket path and a session identity are both filesystem- or
    account-revealing, so the published key is the alias in every mode; the
    label is the only thing `--reveal-projects` widens.
    """
    if row.subject_kind in ("project", "session"):
        return aliases[row.subject_kind].get(row.subject_key, row.subject_key)
    return row.subject_key


# --- the wire adapter ---------------------------------------------------

def _coverage_to_wire(coverage: Any) -> dict[str, Any] | None:
    if coverage is None:
        return None
    payload: dict[str, Any] = {
        "requestedStart": coverage.requested_start,
        "requestedEnd": coverage.requested_end,
        "observedStart": coverage.observed_start,
        "observedEnd": coverage.observed_end,
        "supportUnits": coverage.support_units,
        "gapCodes": list(coverage.gap_codes),
    }
    # An unmeasurable dimension is ABSENT rather than zero. Zero is a
    # measurement; absence is the statement that we do not know.
    for wire, attribute in (
        ("countCoverage", "count_coverage"),
        ("usdCoverage", "usd_coverage"),
        ("identityCoverage", "identity_coverage"),
        ("retentionCoverage", "retention_coverage"),
        ("pricingCoverage", "pricing_coverage"),
        # #620 S3. Both default to `None` and both are omitted when null, so
        # every S2 coverage block stays byte-identical.
        ("evaluabilityCoverage", "evaluability_coverage"),
    ):
        value = getattr(coverage, attribute)
        if value is not None:
            payload[wire] = value
    dimensions = getattr(coverage, "dimensions", None)
    if dimensions:
        payload["dimensions"] = dict(dimensions)
    return payload


def _evidence_to_wire(field: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": field.state,
        "value": field.value,
        "population": _coverage_to_wire(field.population),
        "qualifications": list(field.qualifications),
    }
    if field.code is not None:
        payload["code"] = field.code
    return payload


def _row_to_wire(row: Any, aliases: Mapping[str, Mapping[str, str]], *,
                 reveal_projects: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contributorClass": row.contributor_class,
        "subjectKind": row.subject_kind,
        "subjectKey": _display_key(row, aliases),
        "subjectLabel": _display_label(row, aliases,
                                       reveal_projects=reveal_projects),
        "rank": row.rank,
        "observedUsd": _evidence_to_wire(row.observed_usd),
        "share": _evidence_to_wire(row.share),
        "baseline": _evidence_to_wire(row.baseline),
        "confidence": row.confidence,
        "isFallbackPricing": row.is_fallback_pricing,
        "nextStep": row.next_step,
    }
    # #620 S3. Emitted ONLY when non-empty. Publishing the key unconditionally
    # would move every S2 row golden for no reason.
    evidence = getattr(row, "evidence", None)
    if evidence:
        payload["evidence"] = {
            name: _evidence_to_wire(fieldval)
            for name, fieldval in evidence.items()
        }
    return payload


def _class_to_wire(class_result: Any, aliases, *, reveal_projects: bool):
    return {
        "contributorClass": class_result.contributor_class,
        "verdict": class_result.verdict,
        "code": class_result.code,
        # Which minimum an `insufficient_population` class failed. `null` for
        # every other verdict and for a class a provider-wide cause preempted,
        # which never shaped subjects and so has no minimum to have failed.
        "supportShortfall": class_result.support_shortfall,
        # `null` for a class that measured nothing, which is NOT `low`:
        # confidence is a statement about a measurement, and a withheld class
        # made none. The client renders it rather than recomputing a server
        # rule from the published constants.
        "confidence": class_result.confidence,
        "population": _coverage_to_wire(class_result.population),
        "rows": [_row_to_wire(r, aliases, reveal_projects=reveal_projects)
                 for r in class_result.rows],
    }


def _generation_to_wire(result: Any, scope: Any) -> dict[str, Any] | None:
    generation = result.generation
    if generation is None:
        return None
    payload = dict(generation.as_dict())
    # The plan is part of the identity (#620 S3 §3): without it a seven-class
    # report with three withheld results could share an id with a four-class
    # one. It is threaded on the result rather than hung off the scope,
    # because `_scope_for` reconstructs scopes and would discard it.
    plan = getattr(result, "plan", None)
    if scope is not None and plan is not None:
        payload["generationId"] = generation.generation_id(scope, plan)
    return payload


def _result_to_wire(result: Any, scope: Any, *,
                    reveal_projects: bool) -> dict[str, Any]:
    aliases = _build_alias_maps(result)
    return {
        "source": result.source,
        "accountKey": result.account_key,
        "effectiveSpeed": result.effective_speed,
        "generation": _generation_to_wire(result, scope),
        "denominator": {
            "identity": result.denominator.identity,
            "source": result.denominator.source,
            "accountKey": result.denominator.account_key,
            "windowStart": result.denominator.window_start,
            "windowEnd": result.denominator.window_end,
            "populationDigest": result.denominator.population_digest,
            # An `EvidenceField`, not a bare float: a denominator withheld
            # under the provider-wide cause ladder must say so on the wire
            # too, or a client renders the same definite `$0.00` the terminal
            # used to.
            "usd": _evidence_to_wire(result.denominator.usd),
        },
        "coverage": _coverage_to_wire(result.coverage),
        "verdict": result.verdict,
        "code": result.code,
        "applicableClassCount": result.applicable_class_count,
        "withheldClassCount": result.withheld_class_count,
        "contributors": [
            _row_to_wire(r, aliases, reveal_projects=reveal_projects)
            for r in result.contributors
        ],
        "classes": [
            _class_to_wire(c, aliases, reveal_projects=reveal_projects)
            for c in result.classes
        ],
    }


def _constants_to_wire() -> dict[str, Any]:
    return {
        "contributorShareFloor": kernel.DIAGNOSIS_CONTRIBUTOR_SHARE_FLOOR,
        # The floor alone does not reproduce the verdict. A share is a ratio of
        # two float sums, so a subject at exactly one fifth publishes as
        # 0.19999999999999998, and a client applying `share >= floor` to that
        # would compute `no_contributor` where the server published
        # `contributor`. The published rule is `share >= floor - epsilon`, so
        # both halves are published.
        "shareFloorEpsilon": kernel.DIAGNOSIS_SHARE_FLOOR_EPSILON,
        "tieEpsilonUsd": kernel.DIAGNOSIS_TIE_EPSILON_USD,
        "confidenceHighMinSupport": kernel.CONFIDENCE_HIGH_MIN_SUPPORT,
        "confidenceMediumMinSupport": kernel.CONFIDENCE_MEDIUM_MIN_SUPPORT,
        "confidenceMediumMinCoverage": kernel.CONFIDENCE_MEDIUM_MIN_COVERAGE,
        "withholdMinCoverage": kernel.WITHHOLD_MIN_COVERAGE,
        "withholdMinSupport": kernel.WITHHOLD_MIN_SUPPORT,
        # #620 S3 constants. Each decides a verdict, so each is published
        # beside the rule that consumes it.
        "shortConversationMaxHumanTurns":
            kernel.DIAGNOSIS_SHORT_CONVERSATION_MAX_HUMAN_TURNS,
        "largeContextMinWindowFraction":
            kernel.DIAGNOSIS_LARGE_CONTEXT_MIN_WINDOW_FRACTION,
        "minSubagentBuckets": kernel.DIAGNOSIS_MIN_SUBAGENT_BUCKETS,
        "seedScanBudgetRows": kernel.DIAGNOSIS_SEED_SCAN_BUDGET_ROWS,
        "conversationNormalizeBudgetRows":
            kernel.DIAGNOSIS_CONVERSATION_NORMALIZE_BUDGET_ROWS,
        "codexEventScanBudgetRows":
            kernel.DIAGNOSIS_CODEX_EVENT_SCAN_BUDGET_ROWS,
        "codexEventScanPerFileRows":
            kernel.DIAGNOSIS_CODEX_EVENT_SCAN_PER_FILE_ROWS,
        # The gap-code vocabulary, published the way `registry` publishes the
        # class vocabulary. Without it a consumer receives `gapCodes` as bare
        # strings with no way to tell a code from a newer server apart from
        # one it should have handled, which makes the contract's claim that
        # the set is closed on the server a claim no client can act on.
        # Adding a member is additive evolution of a published enum, which
        # `docs/cli-contract.md` permits.
        "gapCodes": sorted(kernel.GAP_CODES),
        # The turn predicate itself, so the two turn-based classes' rules are
        # reproducible rather than merely readable.
        "turnDefinition": kernel.DIAGNOSIS_TURN_DEFINITION_SENTENCE,
        "registry": [_spec_to_wire(spec)
                     for spec in kernel.CONTRIBUTOR_REGISTRY],
    }


def _spec_to_wire(spec: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contributorClass": spec.kind,
        "subjectKind": spec.subject_kind,
        "label": spec.label,
        "minDistinctSubjects": spec.min_distinct_subjects,
        "minPricedEntries": spec.min_priced_entries,
    }
    # The published predicate. Emitted only when the spec carries one, so the
    # four accounting classes keep their byte-frozen registry shape.
    rule = getattr(spec, "rule", None)
    if rule is not None:
        payload["rule"] = {
            "sentence": rule.sentence,
            "parameters": dict(rule.parameters),
        }
    return payload


def diagnosis_to_wire(report: Any, *, scopes: Mapping[str, Any] | None = None,
                      reveal_projects: bool = False) -> dict[str, Any]:
    """Serialize one report, stamped-first camelCase.

    `scopes` maps a provider name to the `DiagnosisScope` that produced it, so
    each result can publish its own `generationId`. It is optional because the
    kernel-only tests build reports without a store.
    """
    payload = {
        "contractVersion": report.contract_version,
        "measuredAt": report.measured_at,
        "window": {
            "startAt": report.window.start_at,
            "endAt": report.window.end_at,
            "tz": report.window.tz,
            "label": report.window.label,
        },
        "constants": _constants_to_wire(),
        "notes": {
            "scope": kernel.DIAGNOSIS_SCOPE_SENTENCE,
            "overlap": kernel.DIAGNOSIS_OVERLAP_SENTENCE,
        },
        "overallVerdict": report.overall_verdict,
        "overallCode": report.overall_code,
        # `contributor_detected` claims only that a contributor was found, so
        # it can stand beside a withheld sibling class. These two make the
        # incompleteness explicit, so the verdict can never be read as a
        # complete account of the window.
        "applicableClassCount": report.applicable_class_count,
        "withheldClassCount": report.withheld_class_count,
        "results": [
            _result_to_wire(result, (scopes or {}).get(result.source),
                            reveal_projects=reveal_projects)
            for result in report.results
        ],
    }
    return stamp_schema_version(payload, version=EXPLAIN_SCHEMA_VERSION)


def _project(value: Any, path: str = "") -> Any:
    if isinstance(value, dict):
        projected = {}
        for k, v in sorted(value.items()):
            child = f"{path}.{k}" if path else k
            if child in _EXCLUDED_PATHS:
                continue
            projected[k] = _project(v, child)
        return projected
    if isinstance(value, list):
        return [_project(v, f"{path}[]") for v in value]
    return value


def canonical_projection(payload: Mapping[str, Any]) -> bytes:
    """The byte form the CLI and the route must agree on.

    Equality is defined over this projection rather than over terminal text
    against HTTP bytes. It names its exclusions — `measuredAt`, the header
    notes and every presentation-only label — and fixes key ordering, so a
    difference in the projection is a difference in the facts.
    """
    return json.dumps(_project(dict(payload)), sort_keys=True,
                      separators=(",", ":")).encode()


# --- terminal rendering -------------------------------------------------

def _pct(value: float, places: int = 0) -> str:
    """A percentage rendered the way the client renders the same number.

    Python's format spec rounds a tie to even and JavaScript's `toFixed`
    rounds a tie away from zero, so one published `identityCoverage` of
    exactly 0.125 printed `12%` here and `13%` in the modal. `Decimal(float)`
    takes the exact binary value the client also holds, so ROUND_HALF_UP over
    it reproduces `toFixed` for every non-negative percentage — including the
    doubles that only look like ties, such as `0.145 * 100`, which both
    surfaces render as 14.

    This is rounding, not flooring, so the `math.floor(pct + 1e-9)` snap rule
    does not apply: that rule exists to stop a fraction-times-100 losing a
    whole percent to the last bit, and snapping here would round 12.4999… up.
    """
    quantum = decimal.Decimal(1).scaleb(-places)
    return str(decimal.Decimal(value * 100).quantize(
        quantum, rounding=decimal.ROUND_HALF_UP))


def _fmt_usd(value: Any) -> str:
    return f"${value:,.2f}" if isinstance(value, (int, float)) else "—"


def _fmt_share(field: Any) -> str:
    # The withheld branch is a deliberate renderer-side safety net, not live
    # behaviour: `classify_class` gates the denominator before it builds any
    # row, so no row S2 produces can carry a withheld share. It stays because
    # a class added later that withholds one must print its cause rather than
    # raise inside the report.
    if field.state != "available" or not isinstance(field.value, (int, float)):
        return f"withheld ({field.code})" if field.code else "withheld"
    return f"{_pct(field.value, 1)}%"


def _fmt_baseline(field: Any) -> str:
    if field.state != "available" or not isinstance(field.value, (int, float)):
        return f"withheld ({field.code})" if field.code else "withheld"
    return f"{_pct(field.value, 1)}% previously"


# How one evidence figure is rendered. Keyed by the published camelCase name,
# because the unit is a property of the FIGURE and not of its type: a float
# named `estWastedUsd` is dollars and a float named `largestSubagentShare` is
# a proportion, and rendering either as a bare repr makes the reader guess.
_EVIDENCE_USD_FIELDS = frozenset({"estWastedUsd", "unallocatedUsd"})
_EVIDENCE_SHARE_FIELDS = frozenset({"largestSubagentShare",
                                    "maxContextWindowFraction"})

# The human label for one evidence figure. The published camelCase name is a
# WIRE vocabulary, and printing it on the terminal made this the one line in
# the report written in a different language from every other: the coverage
# line beside it says `cost coverage 100%` and the modal says `conversations
# affected`, while the terminal said `affectedConversationCount 1`.
#
# These strings are the same ones `evidenceLabel` renders in
# `dashboard/web/src/lib/diagnosis.ts`, so the two surfaces state one
# vocabulary. A key with no label here renders under its own published name
# rather than vanishing — the fallback is required, because a client reading a
# newer server must show the figure it cannot name.
#
# No label may contain a comma: the terminal joins these figures with `, `, so
# a comma inside a label makes the list ambiguous to read. The modal renders
# each figure in its own column and would not have shown the problem.
_EVIDENCE_LABELS: Mapping[str, str] = MappingProxyType({
    "flaggedTurnCount": "turns that rebuilt their cache",
    "affectedConversationCount": "conversations affected",
    "estWastedUsd": "estimated wasted cost",
    "conversationCount": "qualifying conversations",
    "medianHumanTurns": "median human turns",
    "maxContextWindowFraction":
        "largest request as a share of its context window",
    "identifiedSubagentCount": "identified subagents",
    "largestSubagentShare": "largest subagent as a share of this class",
    "unallocatedUsd": "unallocated cost",
})


def _evidence_label(name: str) -> str:
    return _EVIDENCE_LABELS.get(name, name)


def _fmt_evidence_value(name: str, value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    if name in _EVIDENCE_USD_FIELDS:
        return _fmt_usd(value)
    if name in _EVIDENCE_SHARE_FIELDS:
        return f"{_pct(value, 1)}%"
    return f"{value:,}" if isinstance(value, int) else repr(value)


def _fmt_evidence_field(name: str, field: Any, *,
                        exclude: Sequence[str] = ()) -> str:
    """One evidence figure, VISIBLE rather than behind a hover.

    A withheld member prints its cause, exactly as a withheld denominator
    does: a row may report its cost while one member figure could not be
    established, and printing nothing for that member would read as zero.

    `exclude` drops the qualifications the ROW has already stated. A
    qualification printed twice in one row reads as two separate facts about
    two different figures; `identifiable_subset_unknown_completeness` appeared
    once in the row's own parenthetical and again under `identified
    subagents`, with the class rule beneath them stating the same thing in
    English (#620 S3, browser round 1).
    """
    if field.state != "available" or field.value is None:
        rendered = f"withheld ({field.code})" if field.code else "withheld"
    else:
        rendered = _fmt_evidence_value(name, field.value)
    stated = set(exclude)
    qualifications = [mark for mark in getattr(field, "qualifications", ()) or ()
                      if mark not in stated]
    if qualifications:
        rendered += f" [{', '.join(qualifications)}]"
    return f"{_evidence_label(name)} {rendered}"


def _evidence_phrase(row: Any) -> str:
    """The class-specific evidence beneath one row, in published-name order.

    Ordered by the PUBLISHED name and rendered under the human label, so the
    line reads in the same order as the JSON and in the same words as the
    modal. Ordering by the label instead would make a wording change reorder
    the line and move a golden for a reason that is not the figure's.

    The row's own qualifications are excluded from every member, because the
    row prints them itself one line above — see `_fmt_evidence_field`. The
    exclusion is derived from the row rather than passed in, so the terminal
    and the modal cannot state the rule differently: the modal applies the
    same one, over `observedUsd.qualifications`.
    """
    evidence = getattr(row, "evidence", None) or {}
    stated = getattr(getattr(row, "observed_usd", None), "qualifications",
                     ()) or ()
    return ", ".join(_fmt_evidence_field(name, evidence[name], exclude=stated)
                     for name in sorted(evidence))


def _rule_lines() -> list[str]:
    """One line per class that publishes a rule, plus the turn predicate.

    Rendered from `CONTRIBUTOR_REGISTRY` rather than written out, so a class
    whose rule changes cannot leave the terminal stating the old one. The four
    accounting classes carry no rule and contribute no line, which is what
    keeps the S2 header byte-identical when nothing else changes it.
    """
    lines: list[str] = []
    for spec in kernel.CONTRIBUTOR_REGISTRY:
        rule = getattr(spec, "rule", None)
        if rule is None:
            continue
        lines.append(f"  {spec.label}: {rule.sentence}")
    if lines:
        lines.append(f"  {kernel.DIAGNOSIS_TURN_DEFINITION_SENTENCE}")
        lines.insert(0, "Rules in force, by class:")
    return lines


def _class_label(kind: str) -> str:
    try:
        return kernel.spec_for(kind).label
    except KeyError:
        return kind


def _fmt_denominator(field: Any) -> str:
    """The denominator line, which must never print a figure it does not have.

    A withheld denominator prints its cause. The true retained cost of a
    population nothing could price is unknown, not zero.
    """
    if field.state != "available" or not isinstance(field.value, (int, float)):
        cause = f"withheld ({field.code})" if field.code else "withheld"
        # A withheld denominator may carry the failure's own message — the
        # Codex quota projection names its remedy — and dropping it left the
        # user with a cause and no next step.
        detail = ", ".join(field.qualifications)
        return f"{cause}: {detail}" if detail else cause
    return f"{_fmt_usd(field.value)} of locally retained cost"


def _support_phrase(population: Any) -> str:
    """How many priced accounting entries a figure rests on.

    `support_units` is `None` when the class never shaped a population at all,
    which is not the same as shaping one and finding it empty, so the two
    render differently.
    """
    units = getattr(population, "support_units", 0)
    if units is None:
        return "support not measured"
    return f"support {units} unit" if units == 1 else f"support {units} units"


# The coverage dimensions a figure states, in the order they are read, each
# with the wording the dashboard's `CoverageNote` uses. One vocabulary across
# the two surfaces, so a reader moving between them is reading the same words
# about the same measurement.
_COVERAGE_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("usd_coverage", "cost coverage"),
    ("identity_coverage", "identity coverage"),
    ("pricing_coverage", "priced without fallback"),
    ("retention_coverage", "window retained"),
)


def _gap_phrase(population: Any, exclude: Sequence[str] = ()) -> str:
    """WHY rows could not be decided, which is not why a field was withheld.

    A class withheld because every spending session exhausted its seed share
    printed its cause and nothing else, so the one fact that explains the
    withholding was dropped exactly where the reader needs it. The cause
    itself is excluded, because a preempted class carries it as its own first
    gap code and printing it twice reads as two separate statements.
    """
    codes = [code for code in getattr(population, "gap_codes", ()) or ()
             if code not in set(exclude)]
    return f"undecided: {', '.join(codes)}" if codes else ""


def _publishes_gaps(kind: str) -> bool:
    """Whether this class states its undecided rows on this surface.

    Derived from the published registry rather than from a second list of
    class names: a spec carries a `rule` exactly when it is one of the three
    conversation-derived classes, which are the ones whose predicate can fail
    to decide a row. The four accounting classes keep their S2 wording, so no
    S2 line on this surface moves for a change that is not theirs.

    THE DERIVATION IS AN INFERENCE, and a reader changing the registry should
    know which one. This function answers "does this class state its undecided
    rows on the terminal" by asking "does this class publish a rule", and the
    two are the same set only by today's construction. Giving an accounting
    spec a `rule` — a reasonable thing to want, since the four accounting
    predicates are perfectly statable — would silently add an `undecided:` line
    to that class as well. That change is loud in the goldens, because it moves
    a dozen of them; it is surprising in the source, because nothing at the
    call site names gap lines. Left as an inference rather than a second list
    on purpose: a hand-maintained list of class names is the failure this was
    derived to avoid, and a wrong list fails silently where this fails in the
    goldens.
    """
    try:
        return kernel.spec_for(kind).rule is not None
    except KeyError:
        return False


def _coverage_phrase(population: Any, *, gaps: bool = False) -> str:
    """The population one measured figure rests on.

    Each dimension is rendered ONLY when it is present. An absent dimension is
    "not measured", and printing `0%` for it would state a measurement nobody
    made — a figure rendered over a population that did not support it.
    """
    parts = [_support_phrase(population)]
    for attribute, label in _COVERAGE_DIMENSIONS:
        value = getattr(population, attribute, None)
        if value is not None:
            parts.append(f"{label} {_pct(value)}%")
    # The only dimension that falls when a row could not be DECIDED, which is
    # a different statement from an identity that did not resolve. Published
    # by the three conversation-derived classes only, so no S2 line moves.
    evaluability = getattr(population, "evaluability_coverage", None)
    if evaluability is not None:
        parts.append(f"decidable {_pct(evaluability)}%")
    gap_phrase = _gap_phrase(population) if gaps else ""
    if gap_phrase:
        parts.append(gap_phrase)
    return ", ".join(parts)


def _class_support(class_result: Any) -> str:
    """The population and the confidence of one MEASURED class.

    A class admitted at 0.50 dollar coverage can report `no_contributor` at
    `low` confidence, and a bare class label said nothing about either.

    Both facts, in the wording the dashboard's `CoverageNote` uses, because
    the terminal previously stated confidence and no coverage dimensions
    while the modal stated the dimensions and no confidence: a reader moving
    between the two surfaces saw each fact on only one of them.
    """
    confidence = class_result.confidence or kernel.assess_confidence(
        class_result.population)
    phrase = _coverage_phrase(
        class_result.population,
        gaps=_publishes_gaps(class_result.contributor_class))
    return f"{phrase}, confidence {confidence}"


def _shortfall_phrase(class_result: Any) -> str:
    """Which support minimum an `insufficient_population` class failed."""
    code = getattr(class_result, "support_shortfall", None)
    if code is None:
        return ""
    try:
        spec = kernel.spec_for(class_result.contributor_class)
    except KeyError:
        return code
    if code == kernel.SUPPORT_SHORTFALL_DISTINCT_SUBJECTS:
        return (f"fewer than {spec.min_distinct_subjects} distinct "
                f"{spec.subject_kind}s")
    if code == kernel.SUPPORT_SHORTFALL_PRICED_ENTRIES:
        return f"fewer than {spec.min_priced_entries} priced entries"
    if code == kernel.SUPPORT_SHORTFALL_USD_COVERAGE:
        return f"dollar coverage below {kernel.WITHHOLD_MIN_COVERAGE:.2f}"
    if code == kernel.SUPPORT_SHORTFALL_NO_PRICED_DOLLARS:
        return "no priced dollars to divide by"
    return code


def _withheld_note(class_result: Any) -> str:
    """What a class with no measurement can honestly say about its population.

    Confidence is a statement ABOUT a measurement, and a withheld class made
    none, so it is not printed here: `insufficient_population (support 60
    units, confidence high)` claimed high confidence in an answer that does
    not exist. What is printed instead is the shortfall that actually applies,
    because `insufficient_population` is one cause for four different
    shortfalls and the support count belongs to only one of them.
    """
    parts = [_support_phrase(class_result.population)]
    shortfall = _shortfall_phrase(class_result)
    if shortfall:
        parts.append(shortfall)
    # A class withheld because every spending session exhausted its seed share
    # must still say `scan_budget_exhausted` beside the cause, or the reader is
    # left with a cause and no reason. The cause itself is excluded: a
    # preempted class carries it as its own first gap code.
    if _publishes_gaps(class_result.contributor_class):
        gaps = _gap_phrase(class_result.population,
                           exclude=(class_result.code,) if class_result.code
                           else ())
        if gaps:
            parts.append(gaps)
    return ", ".join(parts)


def render_terminal(report: Any, *, reveal_projects: bool = False) -> str:
    floor = kernel.DIAGNOSIS_CONTRIBUTOR_SHARE_FLOOR
    lines: list[str] = []
    lines.append(
        f"Window {report.window.start_at} .. {report.window.end_at} "
        f"[{report.window.tz}] (half-open: the start is included, the end is "
        f"not)"
    )
    lines.append(f"Measured at {report.measured_at}")
    lines.append(kernel.DIAGNOSIS_SCOPE_SENTENCE)
    lines.append(kernel.DIAGNOSIS_OVERLAP_SENTENCE)
    lines.append(
        f"A subject is reported as a contributor at a share of {floor:.2f} "
        f"or more of its named denominator."
    )
    # The published predicate of every class that has one. A verdict a reader
    # cannot reproduce is a verdict they have to trust.
    rule_lines = _rule_lines()
    if rule_lines:
        lines.append("")
        lines.extend(rule_lines)

    for result in report.results:
        aliases = _build_alias_maps(result)
        account = result.account_key or "all accounts"
        lines.append("")
        lines.append(f"== {result.source} · {account} ==")
        lines.append(
            f"Denominator {result.denominator.identity}: "
            f"{_fmt_denominator(result.denominator.usd)}"
        )
        if result.effective_speed:
            lines.append(f"Effective speed: {result.effective_speed}")

        if result.contributors:
            lines.append("")
            lines.append("Reported contributors, ranked by observed cost:")
            for row in result.contributors:
                label = _display_label(row, aliases,
                                       reveal_projects=reveal_projects)
                notes = [f"confidence {row.confidence}"]
                if row.is_fallback_pricing:
                    notes.append("fallback pricing")
                notes.extend(row.observed_usd.qualifications)
                lines.append(
                    f"  {row.rank}. [{_class_label(row.contributor_class)}] "
                    f"{label} — {_fmt_usd(row.observed_usd.value)}, "
                    f"{_fmt_share(row.share)} of the denominator "
                    f"({', '.join(notes)})"
                )
                lines.append(f"       baseline: {_fmt_baseline(row.baseline)}")
                # The population this row's figures rest on. Every class line
                # below states its own; a contributor row that stated none was
                # the one figure on this surface a reader could not weigh.
                lines.append(
                    f"       coverage: "
                    f"{_coverage_phrase(row.observed_usd.population, gaps=_publishes_gaps(row.contributor_class))}"
                )
                # Class-specific evidence, VISIBLE on the row rather than
                # behind a hover (R9). Absent for every S2 class, whose
                # evidence mapping is empty, so no S2 row gains a line.
                evidence = _evidence_phrase(row)
                if evidence:
                    lines.append(f"       evidence: {evidence}")
                lines.append(f"       -> Run {row.next_step}")

        clean = [c for c in result.classes
                 if c.verdict == kernel.VerdictState.NO_CONTRIBUTOR.value]
        if clean:
            lines.append("")
            lines.append("Classes reporting no contributor:")
            for class_result in clean:
                lines.append(
                    f"  {_class_label(class_result.contributor_class)} "
                    f"({_class_support(class_result)})"
                )

        withheld = [c for c in result.classes
                    if c.verdict == kernel.VerdictState.WITHHELD.value]
        if withheld:
            lines.append("")
            lines.append("Classes with no measurement to report:")
            for class_result in withheld:
                cause = class_result.code or class_result.verdict
                lines.append(
                    f"  {_class_label(class_result.contributor_class)} — {cause}"
                    f" ({_withheld_note(class_result)})"
                )
                # Spec 5.4. On a denied dashboard the same class name renders
                # two verdicts in one report, and a reader who sees one
                # withheld and one measured must be told why rather than left
                # to infer a bug. Unreachable from this surface — the CLI
                # reads its own stores and is always authorized — and stated
                # here so both surfaces carry one sentence rather than two.
                if (class_result.contributor_class == "subagent_fanout"
                        and cause == kernel.WithheldCause
                        .TRANSCRIPTS_NOT_VISIBLE.value):
                    lines.append(
                        f"    {kernel.DIAGNOSIS_TRANSCRIPT_ASYMMETRY_SENTENCE}"
                    )

        # `not_applicable` is NOT a withheld measurement: it says the provider
        # structurally cannot support the class, and it is excluded from the
        # completeness test for that reason. Filing it under the withheld
        # heading contradicts the distinction the contract rests on. No S2
        # class reaches this, and S3's cache-churn class does.
        inapplicable = [c for c in result.classes
                        if c.verdict == kernel.VerdictState.NOT_APPLICABLE.value]
        if inapplicable:
            lines.append("")
            lines.append("Classes this provider does not support:")
            for class_result in inapplicable:
                lines.append(
                    f"  {_class_label(class_result.contributor_class)} — "
                    f"not applicable to {result.source}"
                )

        lines.append("")
        verdict = result.verdict + (f" ({result.code})" if result.code else "")
        lines.append(
            f"Verdict for {result.source}: {verdict} "
            f"[{result.withheld_class_count} of "
            f"{result.applicable_class_count} applicable classes withheld]"
        )

    lines.append("")
    overall = report.overall_verdict + (
        f" ({report.overall_code})" if report.overall_code else ""
    )
    # A `contributor_detected` verdict claims only that a contributor was
    # found, so it must never be read as a complete account of the window.
    lines.append(
        f"Overall: {overall} "
        f"[{report.withheld_class_count} of "
        f"{report.applicable_class_count} applicable classes withheld]"
    )
    return "\n".join(lines) + "\n"


# --- the command --------------------------------------------------------

@dataclass(frozen=True)
class _ExactWindow:
    """The parsed form of `--start-at` / `--end-at`.

    It carries the same three attributes `_parse_diff_window` returns, so the
    caller reads one shape whichever grammar produced it.
    """

    start_utc: dt.datetime
    end_utc: dt.datetime
    label: str = ""


def _parse_exact_bound(raw: str, flag: str) -> dt.datetime:
    """One exact instant, in UTC.

    A DATE-ONLY value is refused rather than read as midnight somewhere. The
    same refusal governs `five-hour-breakdown --block-start`, and for the same
    reason: a date is not an instant, and choosing a zone on the user's behalf
    measures a window nobody asked for. A full-ISO form carries its own offset
    and is timezone-independent; a naive datetime is read as UTC, which is the
    convention `--block-start` already sets.
    """
    text = (raw or "").strip()
    if "T" not in text and " " not in text:
        raise ValueError(
            f"{flag} must be an ISO instant such as 2026-08-10T00:00:00Z, "
            f"not the date {text!r}"
        )
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{flag} must be an ISO instant: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _resolve_window(args, now_utc: dt.datetime, tz_name: str):
    """Resolve the window token through the shared kernel grammar.

    `_parse_diff_window` already sits in `bin/_lib_diff_kernel.py` and is
    already re-exported, so `explain` reuses it in place rather than moving
    it (E4 was dropped for exactly that reason).

    Exact bounds bypass that grammar entirely. They are already validated as
    a pair by `cmd_explain`, so reaching here with only one of them is not
    possible.
    """
    c = _cctally()
    dk = c._load_sibling("_lib_diff_kernel")
    start_raw = getattr(args, "start_at", None)
    end_raw = getattr(args, "end_at", None)
    if start_raw or end_raw:
        start_at = _parse_exact_bound(start_raw, "--start-at")
        end_at = _parse_exact_bound(end_raw, "--end-at")
        if end_at <= start_at:
            # A selector error, so exit 2 — the same code a malformed
            # `--window` token gets. `DiagnosisScope` would raise
            # `range_unresolved` for it instead, which is exit 3 and would
            # report a wrong argument as an infrastructure failure.
            raise ValueError("--end-at must be after --start-at")
        return _ExactWindow(start_utc=start_at, end_utc=end_at)
    token = getattr(args, "window", None) or "this-week"
    try:
        # The anchor read goes through the ORDINARY stats opener, which
        # migrates, repairs and prints. An absolute window does not need it,
        # so it is resolved only when the grammar says the token cannot be
        # resolved without it — which is exactly what NoAnchorError reports.
        return dk._parse_diff_window(
            token, now_utc=now_utc, anchor_resets_at=None,
            anchor_week_start=None, tz_name=tz_name,
        )
    except dk.NoAnchorError:
        pass
    try:
        anchor_week_start, anchor_resets_at = dk._diff_resolve_anchor(now_utc)
    except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
        # The anchor read is a STORE read, so its failures belong to the
        # establishment taxonomy, not to selector validation. Reporting a
        # malformed stats.db as `explain: database disk image is malformed`
        # with exit 2 told the user to fix a `--window` token that was never
        # wrong, and hid an infrastructure failure inside the argument rules.
        #
        # The tuple is deliberately wider than the two SQLite-shaped causes,
        # and the two type errors are store signals rather than code bugs.
        # `_diff_resolve_anchor` calls `str.replace` on the stored timestamp
        # with no type guard, and stats.db is a non-STRICT SQLite database, so
        # the declared TEXT type is an affinity rather than a constraint: a
        # BLOB in that column is returned as `bytes` and raises `TypeError`.
        # (An INTEGER cannot reach it — TEXT affinity converts one to text on
        # insert — so `AttributeError` is named for the same class of store
        # damage rather than because this schema can produce it.) Neither was
        # caught, so the exception escaped into the CLI's top-level handler
        # and printed `Error: a bytes-like object is required, not 'str'` at
        # exit 1, which spec §4 does not use. `open_db()` failures never reach
        # here — the anchor helper catches those itself and returns
        # `(None, None)`. The widening stays bounded to this one call: a
        # `TypeError` from anywhere else in the command still propagates,
        # because reporting a code bug as an infrastructure failure tells the
        # user to fix a store that is not broken.
        raise kernel.EstablishmentFailure(
            kernel.EstablishmentError.STORE_UNAVAILABLE.value,
            f"the subscription-week anchor could not be read: {exc}",
        ) from exc
    return dk._parse_diff_window(
        token, now_utc=now_utc,
        anchor_resets_at=anchor_resets_at,
        anchor_week_start=anchor_week_start,
        tz_name=tz_name,
    )


def resolve_tz_name(args, config) -> str:
    """The display zone both surfaces resolve, in one place.

    `display_tz` is part of the scope the generation id is computed over and is
    published as `window.tz`, which the canonical projection keeps. So the CLI
    and the route agreeing about it is not a nicety: two surfaces reading the
    same facts under two spellings of the same zone publish two different
    `generationId` values, and the equivalence check then compares two
    different questions.
    """
    c = _cctally()
    tz_obj = c.resolve_display_tz(args, config)
    return tz_obj.key if tz_obj is not None else c._local_tz_name()


def _resolve_account_key(sources, args, provider: str):
    """Resolve `--account` over the diagnosis's own read-only open path.

    `resolve_account_filter` reaches stats.db through the ordinary opener,
    which migrates, repairs, imports and replays — the same class of problem
    the week anchor already avoids. `explain` reads and never writes, so it
    resolves the ref itself over `mode=ro`, and only when a ref was actually
    supplied.
    """
    ref = getattr(args, "account", None)
    if ref is None:
        return None, None
    import _lib_accounts as accounts

    c = _cctally()
    try:
        conn = sources.open_read_only("stats")
    except kernel.EstablishmentFailure:
        # Selector validation is exit 2, and a ref that cannot be resolved is
        # unresolvable whatever the reason: with no account registry on the
        # machine there is no account this ref could name. Letting the failure
        # through turned an exit-2 selector error into an exit-3 establishment
        # error on any install without a stats.db.
        print(f"account: --account {ref!r} is ambiguous or unknown "
              "(this machine holds no account registry)", file=sys.stderr)
        return None, 2
    try:
        try:
            return accounts.resolve_account_ref(conn, ref, provider), None
        except accounts.AccountRefError as exc:
            print(f"account: --account {ref!r} is ambiguous or unknown",
                  file=sys.stderr)
            c._load_sibling("_cctally_account").print_ref_candidates(
                conn, exc.candidates
            )
            return None, 2
    finally:
        conn.close()


def cmd_explain(args) -> int:
    c = _cctally()
    sources = _sources()
    source = getattr(args, "source", "claude") or "claude"
    account = getattr(args, "account", None)

    # Account keys are provider-scoped, so one selector cannot address both
    # providers. This mirrors the established behaviour recorded at
    # docs/commands/account.md:94.
    if account is not None and source == "all":
        print("explain: --account cannot be combined with --source all "
              "(account keys are provider-scoped)", file=sys.stderr)
        return 2

    # The two window grammars are mutually exclusive, and both bounds of the
    # exact one are required together. Validated HERE rather than through an
    # argparse mutually-exclusive group, because `--window` carries a default
    # and argparse cannot tell a default apart from an explicit value.
    window_token = getattr(args, "window", None)
    start_raw = getattr(args, "start_at", None)
    end_raw = getattr(args, "end_at", None)
    if (start_raw or end_raw) and window_token is not None:
        print("explain: --start-at/--end-at cannot be combined with --window "
              "(a window token names calendar days; exact bounds name "
              "instants)", file=sys.stderr)
        return 2
    if bool(start_raw) != bool(end_raw):
        print("explain: --start-at and --end-at must be given together",
              file=sys.stderr)
        return 2

    now_utc = _command_as_of()
    config = c.load_config()
    tz_name = resolve_tz_name(args, config)

    dk = c._load_sibling("_lib_diff_kernel")
    try:
        parsed = _resolve_window(args, now_utc, tz_name)
    except (ValueError, dk.NoAnchorError) as exc:
        # Exactly the two argument-shaped failures: a token the grammar cannot
        # parse, and one that needs a week anchor this machine cannot supply.
        # A bare `except Exception` here also swallowed store failures raised
        # by the anchor read and reported them as malformed selectors.
        print(f"explain: {exc}", file=sys.stderr)
        return 2
    except kernel.EstablishmentFailure as exc:
        print(f"explain: {exc.code}: {exc.message}", file=sys.stderr)
        return 3

    try:
        account_key, account_exit = _resolve_account_key(
            sources, args, source if source in ("claude", "codex") else "claude",
        )
    except kernel.EstablishmentFailure as exc:
        print(f"explain: {exc.code}: {exc.message}", file=sys.stderr)
        return 3
    if account_exit is not None:
        return account_exit

    speed = getattr(args, "speed", None)
    if speed == "auto":
        speed = None

    try:
        scope = sources.DiagnosisScope(
            source=source,
            account_key=account_key,
            window_start=parsed.start_utc,
            window_end=parsed.end_utc,
            effective_speed=speed,
            display_tz=tz_name,
            label=parsed.label,
        )
        # The CLI reads this machine's own stores directly, so there is no
        # authorization layer to compose and transcripts are always visible.
        # Stated rather than defaulted: `build_diagnosis` carries no default,
        # because the one call site that forgot it was the dashboard route.
        report = sources.build_diagnosis(scope, measured_at=now_utc,
                                         transcripts_visible=True)
    except kernel.EstablishmentFailure as exc:
        # An `EstablishmentFailure` is always exit 3. Argument-shaped
        # validation — a malformed `--window` token, an unresolvable account
        # ref — is exit 2 and fails at argument-parse time above, never by
        # becoming an establishment failure here.
        print(f"explain: {exc.code}: {exc.message}", file=sys.stderr)
        return 3

    reveal = bool(getattr(args, "reveal_projects", False))
    scopes = {result.source: _scope_for(sources, scope, result.source)
              for result in report.results}
    # A store that could not be read is an infrastructure failure rather than
    # an answer about data availability, so when no requested provider
    # answered, the report is still PRINTED — a person needs to see the typed
    # cause — and the command still exits 3, so a script sees the failure. A
    # report withheld for any other cause exits 0, because "the store does not
    # hold that window" is a correct answer.
    exit_code = 3 if kernel.unreadable_store_is_terminal(report) else 0
    if getattr(args, "emit_json", False) or getattr(args, "json", False):
        payload = diagnosis_to_wire(report, scopes=scopes,
                                    reveal_projects=reveal)
        print(json.dumps(payload, indent=2))
        return exit_code

    sys.stdout.write(render_terminal(report, reveal_projects=reveal))
    return exit_code


def _scope_for(sources, scope, source: str):
    """The per-provider scope `build_diagnosis` used for one result."""
    if scope.source == source:
        return scope
    return sources.DiagnosisScope(
        source=source,
        account_key=scope.account_key,
        window_start=scope.window_start,
        window_end=scope.window_end,
        effective_speed=scope.effective_speed if source == "codex" else None,
        display_tz=scope.display_tz,
        label=scope.label,
    )
