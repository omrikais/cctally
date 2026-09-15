"""Pure diagnosis kernel for `cctally explain` (#620 S2).

Every type, constant, predicate and the ranking live here. The module opens
no store, renders nothing and reads no clock, so each classification rule is
directly unit-testable without a fixture. `bin/_cctally_diagnosis_sources.py`
is the only component that opens databases; `bin/_cctally_diagnosis.py` is
the only one that renders.

The evidence contract, in one sentence: every measured quantity is an
`EvidenceField` — either available with a value, its population and zero or
more qualifications, or withheld with a typed cause and still its
population. The three cause layers never mix. `EstablishmentError` ends the
request. `WithheldCause` is field-level. `BaselineOutcome` is its own axis,
because a thin baseline must not suppress a measured observed-cost answer.

Spec: docs/superpowers/specs/2026-08-19-620-s2-on-demand-diagnosis.md
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


class FrozenDict(dict):
    """A small pickle-safe immutable mapping for cross-process results."""

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("frozen mapping")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __reduce__(self):
        return FrozenDict, (dict(self),)


# --- versioned constants ------------------------------------------------
#
# These are published in the JSON envelope and stated on the terminal
# surface, so a reader can see the rule that produced the verdict rather
# than inferring it. Changing one is a contract change.

DIAGNOSIS_CONTRACT_VERSION: int = 1
DIAGNOSIS_CONTRIBUTOR_SHARE_FLOOR: float = 0.20
DIAGNOSIS_TIE_EPSILON_USD: float = 1e-9
# The slack the share floor is compared with, and the reason it is PUBLISHED
# rather than private: it decides the verdict. A share is a ratio of two
# independently accumulated float sums, so a subject genuinely at one fifth of
# the denominator computes as 0.19999999999999998, and a client applying a bare
# `share >= floor` to the published share would compute `no_contributor` where
# the server published `contributor`. The rule both sides apply is
# `share >= floor - epsilon`, and both halves of it are on the wire.
DIAGNOSIS_SHARE_FLOOR_EPSILON: float = 1e-9
CONFIDENCE_HIGH_MIN_SUPPORT: int = 20
CONFIDENCE_MEDIUM_MIN_SUPPORT: int = 5
CONFIDENCE_MEDIUM_MIN_COVERAGE: float = 0.80
WITHHOLD_MIN_COVERAGE: float = 0.50
WITHHOLD_MIN_SUPPORT: int = 2

# Float comparison slack for the COVERAGE thresholds only, so a coverage
# computed as a ratio of sums does not fall below its own threshold on the last
# bit. The share floor has its own constant above, because that one is part of
# the published rule and this one is not.
#
# Exported without the underscore because the source adapter applies it to
# RETENTION coverage, which is the same kind of ratio against the same
# `WITHHOLD_MIN_COVERAGE` constant. Two copies of one slack value drift, and a
# retention computing as 0.49999999999999994 for a genuinely half-retained
# window withheld the whole provider as `stale_evidence`.
COVERAGE_EPSILON: float = 1e-9

# --- S3 versioned constants (#620 S3) -----------------------------------
#
# Published with the rest, because each one decides a verdict and a reader
# who cannot see the threshold cannot reproduce the rule that produced it.

# A conversation is "short" at or below this many canonical human turns.
DIAGNOSIS_SHORT_CONVERSATION_MAX_HUMAN_TURNS: int = 3
# The fraction of a request's OWN model context window that makes it large.
DIAGNOSIS_LARGE_CONTEXT_MIN_WINDOW_FRACTION: float = 0.80
# How many distinct subagent buckets a parent needs before it fans out.
DIAGNOSIS_MIN_SUBAGENT_BUCKETS: int = 2

# The three scan budgets. Each bounds a read path that is capped rather than
# structurally bounded, and each is allocated per subject rather than
# globally, so no one session, conversation or source file can starve another
# and the result never depends on which one happened to be read first.
DIAGNOSIS_SEED_SCAN_BUDGET_ROWS: int = 50_000
DIAGNOSIS_CONVERSATION_NORMALIZE_BUDGET_ROWS: int = 2_000
# #632 measured the inference-relevant production population at max=2,262
# rows/file (p99=853). The global ceiling keeps arbitrarily wide windows
# bounded; the per-file ceiling prevents a narrow window's long tail from
# consuming the whole allowance.
DIAGNOSIS_CODEX_EVENT_SCAN_BUDGET_ROWS: int = 1_048_576
DIAGNOSIS_CODEX_EVENT_SCAN_PER_FILE_ROWS: int = 4_096

# Gap codes, NOT withheld causes: a gap describes rows the class could not
# decide, while a cause describes why a whole field was not published.
#
# The vocabulary is closed on the server and every member is declared here,
# because a bare literal at a call site is a code no reader can enumerate and
# no test can name. Spec revision 4 added the last two: Task 2 emitted
# `unresolved_subagent_attribution` as an undeclared literal and reported an
# unreadable Codex origin as `unknown_context_window`, where the context
# window is not what is unknown. A typed cause that misdescribes itself is
# worse than none.
GAP_UNKNOWN_CONTEXT_WINDOW: str = "unknown_context_window"
GAP_SCAN_BUDGET_EXHAUSTED: str = "scan_budget_exhausted"
GAP_UNRESOLVED_SUBAGENT_ATTRIBUTION: str = "unresolved_subagent_attribution"
GAP_AMBIGUOUS_ORIGIN_CATEGORY: str = "ambiguous_origin_category"
# #834 S2 (#800). A conversation the accounting rows name for which the
# transcript store retains no candidate row at all. The predicate needs a human
# turn count and there is no evidence to count, so the conversation is NOT
# decided — it leaves the evaluated population and lowers
# `evaluabilityCoverage` instead of being reported as a non-contributor with
# zero turns. Its own code, because none of the four above describes it: the
# context window is known, the budget was not exhausted, the origin is
# readable, and nothing about a subagent is unresolved.
GAP_NO_RETAINED_TRANSCRIPT: str = "no_retained_transcript"

# The closed set, so a test can enumerate it and a published code that no
# constant defines fails rather than shipping.
GAP_CODES: frozenset[str] = frozenset({
    GAP_UNKNOWN_CONTEXT_WINDOW,
    GAP_SCAN_BUDGET_EXHAUSTED,
    GAP_UNRESOLVED_SUBAGENT_ATTRIBUTION,
    GAP_AMBIGUOUS_ORIGIN_CATEGORY,
    GAP_NO_RETAINED_TRANSCRIPT,
})

# Qualifications an S3 evidence field can carry.
QUALIFICATION_IDENTIFIABLE_SUBSET: str = "identifiable_subset_unknown_completeness"
QUALIFICATION_PARTIAL_ATTRIBUTION: str = "partial_attribution"
QUALIFICATION_SESSION_LEVEL_CAPACITY: str = "session_level_context_window"

# Every S3 class contributes ONE aggregate subject, and its key, kind and
# label are constants of the class rather than anything derived from a
# member. `_display_key` aliases only project and session subjects and emits
# every other key verbatim, so a member-derived key would reach the wire and
# the screen unaliased.
SUBJECT_KIND_QUALIFYING_SET: str = "qualifying_set"
SUBJECT_CACHE_CHURN: str = "qualifying-set/cache-churn"
SUBJECT_SHORT_HIGH_CONTEXT: str = "qualifying-set/short-high-context"
SUBJECT_SUBAGENT_FANOUT: str = "qualifying-set/subagent-fanout"

# The accounting mode a next-step command is told to apply, and the providers
# whose target subcommands accept `-m/--mode` at all.
#
# `explain` reprices every Claude accounting entry at read time
# (`_calculate_entry_cost(..., mode="calculate")`), but `cctally claude daily`
# and `cctally claude session` default to `-m auto`, which returns a non-null
# stored `session_entries.cost_usd_raw` verbatim. On a store holding stored
# costs, a reader following the next step would see different dollars from the
# figure that sent them there, and a next step that contradicts its own
# evidence is the one thing D4 says a next step must not be.
#
# The Codex subcommands register no `-m` at all, because Codex cost is always
# computed at read time from `CODEX_MODEL_PRICING`, so there is no divergence
# to close and no flag to pass. `cctally project` and
# `cctally five-hour-breakdown` register none either, and a flag they reject
# would turn the next step into an argument error; `docs/commands/explain.md`
# states that limitation for the reader.
NEXT_STEP_ACCOUNTING_MODE: str = "calculate"
NEXT_STEP_MODE_SOURCES: frozenset[str] = frozenset({"claude"})


def next_step_mode_flag(source: str) -> str:
    """The `-m/--mode` fragment a next-step command carries, or empty."""
    if source in NEXT_STEP_MODE_SOURCES:
        return f" -m {NEXT_STEP_ACCOUNTING_MODE}"
    return ""


# Which support minimum a class failed, when it was withheld as
# `insufficient_population`. The cause alone reads as a contradiction beside a
# support count that looks ample: `insufficient_population (support 60 units)`
# is exactly what a class with 60 entries under a single model produces.
SUPPORT_SHORTFALL_DISTINCT_SUBJECTS: str = "min_distinct_subjects"
SUPPORT_SHORTFALL_PRICED_ENTRIES: str = "min_priced_entries"
SUPPORT_SHORTFALL_USD_COVERAGE: str = "min_usd_coverage"
SUPPORT_SHORTFALL_NO_PRICED_DOLLARS: str = "no_priced_dollars"

# The header sentences both surfaces state verbatim. They are constants
# rather than render-site strings because B11 binds them on both surfaces.
DIAGNOSIS_SCOPE_SENTENCE: str = (
    "This explains locally retained cost and tokens, not quota percentage."
)
DIAGNOSIS_OVERLAP_SENTENCE: str = (
    "The classes overlap — one session is also a project and a model — "
    "so shares do not sum to 100 percent."
)

# The published turn predicate, in one sentence, for the two turn-based
# classes. It is a constant rather than render-site prose because F8 requires
# the FULL predicate on the surface: `conversation_sessions.msg_count` groups
# `conversation_messages` with no `is_sidechain` predicate while subagent files
# carry the parent's `session_id`, so a reader shown a bare turn count would
# read a sidechain-inclusive physical count as a human-turn count.
DIAGNOSIS_TURN_DEFINITION_SENTENCE: str = (
    "Turn definition: main-thread human messages only — non-sidechain, "
    "non-subagent, after command and meta normalisation; assistant replies "
    "associate with the nearest preceding human."
)

# The asymmetry of spec 5.4, stated where it happens: on a denied dashboard
# the same class name renders two verdicts in one report, because Claude
# subagent structure lives in `conversations.db` and the Codex signal lives in
# `cache.db`. A reader who sees one withheld and one measured must be told why
# rather than left to infer a bug.
DIAGNOSIS_TRANSCRIPT_ASYMMETRY_SENTENCE: str = (
    "A class is withheld where this dashboard is not authorized to read "
    "transcripts. Codex subagent attribution is derived from accounting "
    "metadata and needs no transcript access."
)


# --- the three cause layers ---------------------------------------------

class EstablishmentError(str, Enum):
    """Request-ending failures. Never an evidence field.

    CLI exit 3. Dashboard 503 for `store_unavailable` and
    `generation_incoherent`, 400 for `range_unresolved` and
    `account_unresolved`.
    """

    RANGE_UNRESOLVED = "range_unresolved"
    ACCOUNT_UNRESOLVED = "account_unresolved"
    STORE_UNAVAILABLE = "store_unavailable"
    GENERATION_INCOHERENT = "generation_incoherent"


class WithheldCause(str, Enum):
    """Field-level causes, closed on the server, in precedence order.

    S3 adds `signal_unavailable` and `transcripts_not_visible` additively.
    The client types every code as a bare string with a required fallback
    branch, so an added cause cannot break an older client.
    """

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    # An authorization denial, decided in plan stage 1 before any store is
    # opened, so it outranks every availability cause: the forbidden store is
    # never probed and `signal_unavailable` — which only means an AUTHORIZED
    # open failed — cannot arise for a denied class.
    TRANSCRIPTS_NOT_VISIBLE = "transcripts_not_visible"
    RETAINED_RANGE_MISMATCH = "retained_range_mismatch"
    PRICING_UNAVAILABLE = "pricing_unavailable"
    UNATTRIBUTED_EVIDENCE = "unattributed_evidence"
    STALE_EVIDENCE = "stale_evidence"
    # The signal could not be established at all, which is more fundamental
    # than establishing it with too little support and less informative than a
    # pruned store, which explains WHY it could not be established.
    SIGNAL_UNAVAILABLE = "signal_unavailable"
    INSUFFICIENT_POPULATION = "insufficient_population"
    CALCULATION_FAILED = "calculation_failed"


WITHHELD_CAUSE_PRECEDENCE: tuple[str, ...] = tuple(
    c.value for c in WithheldCause
)


class BaselineOutcome(str, Enum):
    """The baseline axis. A subject missing from an otherwise fully measured
    baseline is the value zero, not a cause."""

    BASELINE_INSUFFICIENT = "baseline_insufficient"


class VerdictState(str, Enum):
    CONTRIBUTOR = "contributor"
    NO_CONTRIBUTOR = "no_contributor"
    WITHHELD = "withheld"
    NOT_APPLICABLE = "not_applicable"


class OverallVerdict(str, Enum):
    CONTRIBUTOR_DETECTED = "contributor_detected"
    NO_CONTRIBUTOR_DETECTED = "no_contributor_detected"
    WITHHELD = "withheld"


class EvidenceState(str, Enum):
    AVAILABLE = "available"
    WITHHELD = "withheld"


class EstablishmentFailure(Exception):
    """Raised by the adapter when the report cannot be established at all.

    Defined here rather than in the adapter so the code it carries is
    validated against the same closed enum the wire publishes.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# --- coverage and evidence ----------------------------------------------

@dataclass(frozen=True)
class PopulationCoverage:
    """Named coverage dimensions rather than one ambiguous fraction.

    An unmeasurable dimension is absent (`None`) rather than zero, because
    zero is a measurement and absence is not. `gap_codes` names why.
    Confidence is computed on `usd_coverage`, because a diagnosis ranked by
    dollars must gate on dollar coverage rather than row counts.
    """

    requested_start: str
    requested_end: str
    observed_start: str | None = None
    observed_end: str | None = None
    count_coverage: float | None = None
    usd_coverage: float | None = None
    identity_coverage: float | None = None
    retention_coverage: float | None = None
    pricing_coverage: float | None = None
    # `None` when no population was ever attributed — a class the provider
    # preempted never shaped its subjects, and zero would assert that it
    # shaped them and found none.
    support_units: int | None = 0
    gap_codes: tuple[str, ...] = ()
    # `evaluated_count / candidate_count`, and the ONLY dimension that falls
    # when a row could not be decided — an unresolvable subagent identity, a
    # parent matching zero or several threads, an ambiguous Codex origin, an
    # unknown context window, an exhausted scan budget. `identity_coverage`
    # cannot also fall for those rows: a population that excludes them has
    # perfect identity coverage by construction. S2 coverage objects leave it
    # `None`, and `_coverage_to_wire` omits a null, so no S2 block moves.
    evaluability_coverage: float | None = None
    # Per dimension, its numerator and denominator in words, so a reader never
    # has to infer which population a figure was computed over. Published by
    # S3 coverage objects only; S2's are byte-frozen.
    dimensions: Mapping[str, str] | None = None


@dataclass(frozen=True)
class EvidenceField:
    state: str                                  # EvidenceState value
    value: Any | None
    population: PopulationCoverage
    qualifications: tuple[str, ...] = ()
    code: str | None = None


def available(value: Any, population: PopulationCoverage,
              qualifications: Sequence[str] = ()) -> EvidenceField:
    return EvidenceField(
        state=EvidenceState.AVAILABLE.value, value=value,
        population=population, qualifications=tuple(qualifications),
    )


def withheld(code: str, population: PopulationCoverage,
             qualifications: Sequence[str] = ()) -> EvidenceField:
    return EvidenceField(
        state=EvidenceState.WITHHELD.value, value=None,
        population=population, qualifications=tuple(qualifications),
        code=code,
    )


@dataclass(frozen=True)
class Denominator:
    """The named object every share divides by.

    `identity` is always `totalExplainedRetainedCost`. A row attributed over
    only part of the population divides by its own disclosed explained
    population and says so through its coverage.

    `usd` is an `EvidenceField`, not a bare float, and it is withheld under
    the same provider-wide cause ladder every class is withheld under. A
    denominator rendered as `$0.00` beside four classes withheld as
    `pricing_unavailable` states a figure the data does not support: the true
    retained cost there is unknown, not zero. `usd_value` is the arithmetic
    view — zero when the field is withheld — and exists so the classification
    rules divide by a number without ever printing one.
    """

    identity: str
    source: str
    account_key: str | None
    window_start: str
    window_end: str
    population_digest: str
    usd: EvidenceField

    @property
    def usd_value(self) -> float:
        value = self.usd.value
        return float(value) if isinstance(value, (int, float)) else 0.0

    @property
    def usd_is_available(self) -> bool:
        return self.usd.state == EvidenceState.AVAILABLE.value


# --- windows, subjects, rows, classes -----------------------------------

@dataclass(frozen=True)
class DiagnosisWindow:
    """A half-open `[start_at, end_at)` window that states its IANA zone."""

    start_at: str
    end_at: str
    tz: str
    label: str = ""


@dataclass(frozen=True)
class SubjectFacts:
    """One subject's priced facts, as the adapter hands them to the kernel.

    `priced_entry_count` is the support unit for every class: the count of
    priced accounting entries attributed to this subject.
    """

    subject_key: str
    subject_label: str
    observed_usd: float
    priced_entry_count: int = 0
    is_fallback_pricing: bool = False
    qualifications: tuple[str, ...] = ()
    baseline_share: float | None = None
    baseline_code: str | None = None
    next_step: str | None = None


@dataclass(frozen=True)
class ContributorRow:
    contributor_class: str
    subject_kind: str
    subject_key: str
    subject_label: str
    rank: int | None
    observed_usd: EvidenceField
    share: EvidenceField
    baseline: EvidenceField
    confidence: str                             # "high" | "medium" | "low"
    is_fallback_pricing: bool
    next_step: str
    # Class-specific evidence, keyed by camelCase wire name. Declared with a
    # `default_factory` because `dataclasses` rejects a mutable default
    # outright, and because a shared default would leak one row's evidence
    # into the next. `_row_to_wire` emits the key only when the mapping is
    # non-empty, so every S2 row stays byte-identical.
    evidence: "Mapping[str, EvidenceField]" = field(default_factory=dict)


@dataclass(frozen=True)
class ClassResult:
    contributor_class: str
    verdict: str                                # VerdictState value
    code: str | None
    rows: tuple[ContributorRow, ...]
    population: PopulationCoverage
    # Set only when `code` is `insufficient_population`: WHICH minimum was not
    # met. A class withheld under a provider-wide cause never shaped its
    # subjects, so it has no shortfall to name.
    support_shortfall: str | None = None
    # The confidence of the measurement this class made, and `None` when it
    # made none. `None` is NOT `low`: confidence is a statement about a
    # measurement. Carried on the result rather than recomputed per renderer,
    # so the terminal and the modal state one assessment.
    confidence: str | None = None


@dataclass(frozen=True)
class ClassRule:
    """The published predicate.

    `sentence` is what a person reads on the terminal and in the modal;
    `parameters` is the machine-readable form, so a consumer can reproduce the
    rule rather than parse English.
    """

    sentence: str
    parameters: "Mapping[str, Any]"


@dataclass(frozen=True)
class ContributorSpec:
    kind: str
    subject_kind: str
    min_distinct_subjects: int
    min_priced_entries: int
    # `None` means this class publishes no generic next step, because its next
    # step names the subject and only the code that built the subject knows
    # how to name it. See `_render_next_step`.
    next_step_template: str | None
    label: str = ""
    # The published predicate, or `None` for the four accounting classes,
    # whose wire shape is byte-frozen. Last field, so existing positional
    # construction is unaffected.
    rule: "ClassRule | None" = None


# Fixed order. Registry order is the tie-break after observed USD and the
# display order for non-ranked rows, so it is part of the contract.
CONTRIBUTOR_REGISTRY: tuple[ContributorSpec, ...] = (
    ContributorSpec(
        kind="model_mix",
        subject_kind="model",
        min_distinct_subjects=2,
        min_priced_entries=20,
        # The SUBGROUP form, not `--source`: `cctally daily` registers no
        # `--source` flag at all, so `cctally daily --source claude` is an
        # argument error rather than a report. `cctally claude daily` and
        # `cctally codex daily` route to the same command with the provider
        # pinned, which is what the flat form was reaching for.
        next_step_template=(
            "cctally {source} daily{mode_flag} "
            "--since {window_start_date} --until {window_end_date}"
        ),
        label="Expensive model mix",
    ),
    ContributorSpec(
        kind="project_concentration",
        subject_kind="project",
        min_distinct_subjects=2,
        min_priced_entries=20,
        next_step_template="cctally project --source {source}",
        label="One project dominating",
    ),
    ContributorSpec(
        kind="session_concentration",
        subject_kind="session",
        min_distinct_subjects=2,
        min_priced_entries=20,
        # The subgroup form, for the same reason `model_mix` uses it.
        next_step_template=(
            "cctally {source} session{mode_flag} "
            "--since {window_start_date} --until {window_end_date}"
        ),
        label="One session dominating",
    ),
    ContributorSpec(
        kind="five_hour_bursts",
        subject_kind="block",
        min_distinct_subjects=2,
        min_priced_entries=20,
        # No generic template. This class's next step names ONE block, and a
        # block subject key is the composite `<root>|<pool>|<instant>` that
        # `_native_block_key` builds — so interpolating it rendered
        # `cctally five-hour-breakdown --block-start claude|standard|<iso>`,
        # which the command rejects and which a shell splits at the unquoted
        # `|` into two piped commands. Both block builders set a per-subject
        # override on every block they produce, which is the only form that
        # can be correct, so the hazardous template is deleted rather than
        # rewritten.
        next_step_template=None,
        label="Concentrated 5-hour bursts",
    ),
    # --- the three S3 conversation-derived classes, APPENDED ---------------
    #
    # Appending is what keeps every S2 tie-break where it was: `REGISTRY_ORDER`
    # is the tie-break after observed USD, so inserting anywhere else would
    # re-rank rows that did not change. Each takes `min_distinct_subjects=1`
    # because it contributes exactly ONE aggregate subject — the qualifying
    # set — whose members appear only as evidence fields.
    ContributorSpec(
        kind="cache_churn",
        subject_kind=SUBJECT_KIND_QUALIFYING_SET,
        min_distinct_subjects=1,
        min_priced_entries=20,
        # Per-subject override; the loader names the window this class
        # measured rather than any member of it.
        next_step_template=None,
        label="Prompt-cache churn",
        rule=ClassRule(
            sentence=("Turns whose prompt cache was rebuilt: the cached prefix "
                      "collapsed and was re-created in the same turn."),
            parameters={"collapseFraction": 0.5, "recreateFraction": 0.75,
                        "cacheFloorTokens": 20_000, "createFloorTokens": 20_000},
        ),
    ),
    ContributorSpec(
        kind="short_high_context",
        subject_kind=SUBJECT_KIND_QUALIFYING_SET,
        min_distinct_subjects=1,
        min_priced_entries=20,
        next_step_template=None,
        label="Short conversations carrying large context",
        rule=ClassRule(
            sentence=("Conversations with at most 3 human turns where at least "
                      "one request used at least 80% of that request's model "
                      "context window. With several replies the largest single "
                      "request fraction is used, never their sum."),
            parameters={
                "maxHumanTurns": DIAGNOSIS_SHORT_CONVERSATION_MAX_HUMAN_TURNS,
                "minWindowFraction": DIAGNOSIS_LARGE_CONTEXT_MIN_WINDOW_FRACTION,
            },
        ),
    ),
    ContributorSpec(
        kind="subagent_fanout",
        subject_kind=SUBJECT_KIND_QUALIFYING_SET,
        min_distinct_subjects=1,
        min_priced_entries=20,
        next_step_template=None,
        label="Subagent fan-out",
        rule=ClassRule(
            sentence=("Cost attributable to delegated subagent work, grouped by "
                      "parent. On Codex this is an identifiable subset of "
                      "unknown completeness."),
            parameters={"minBuckets": DIAGNOSIS_MIN_SUBAGENT_BUCKETS},
        ),
    ),
)

REGISTRY_ORDER: Mapping[str, int] = {
    spec.kind: index for index, spec in enumerate(CONTRIBUTOR_REGISTRY)
}


def spec_for(kind: str) -> ContributorSpec:
    for spec in CONTRIBUTOR_REGISTRY:
        if spec.kind == kind:
            return spec
    raise KeyError(kind)


# --- the execution plan (#620 S3 §3) ------------------------------------
#
# The route behaviour, the withheld causes, the stores opened, the cache
# projection and the generation hash are all derived from ONE immutable
# provider-local plan, so they cannot state four different things.
#
# Resolution is two-stage, because readability cannot be an input to a
# decision taken before anything is opened. Stage 1 is decided from
# authorization and provider capability alone. Only permitted stores are then
# opened, and stage 2 folds those outcomes into final modes. Stage 2's plan is
# the one that is hashed, so the identifier describes what actually ran.


class ClassMode(str, Enum):
    MEASURE = "measure"
    WITHHOLD = "withhold"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class ClassPlan:
    """One class's mode, and what that mode requires of the stores."""

    kind: str
    mode: str                                   # ClassMode value
    cause: str | None                           # WithheldCause value on WITHHOLD
    requires_conversations: bool
    requires_s3_projection: bool


@dataclass(frozen=True)
class ExecutionPlan:
    source: str
    # Registry order, always all seven. A plan that omitted a class would make
    # "which classes ran" depend on reading the list rather than on the mode.
    classes: tuple[ClassPlan, ...]

    def requires_conversations(self) -> bool:
        return any(c.requires_conversations for c in self.classes)

    def requires_s3_projection(self) -> bool:
        return any(c.requires_s3_projection for c in self.classes)

    def mode_for(self, kind: str) -> ClassPlan | None:
        for c in self.classes:
            if c.kind == kind:
                return c
        return None

    def plan_token(self) -> str:
        """A deterministic token over the source and every class's decision.

        EVERY plan tokenizes — there is no legacy exemption for the
        four-class shape. A seven-class report with three withheld results is
        a different report from a four-class one and must not share an
        identifier with it.
        """
        digest = hashlib.sha256()
        digest.update(self.source.encode())
        for c in self.classes:
            digest.update(b"\x1e")
            digest.update(f"{c.kind}={c.mode}:{c.cause or ''}".encode())
        return digest.hexdigest()


# The classes whose predicate is defined over transcript rows, so they read
# `conversations.db` on BOTH providers.
_CONVERSATION_CLASSES: frozenset[str] = frozenset({
    "cache_churn", "short_high_context",
})
# The S3 class whose store depends on the provider: Claude reads normalized
# subagent structure out of `conversations.db`, while Codex derives the same
# signal from `codex_conversation_threads` and `codex_session_entries`, which
# both live in `cache.db` — so it needs no transcript authorization at all.
_PROVIDER_DEPENDENT_S3_CLASSES: frozenset[str] = frozenset({"subagent_fanout"})


def resolve_policy_plan(source: str, *,
                        transcripts_visible: bool) -> ExecutionPlan:
    """Stage 1, decided without touching a store.

    A class denied here is SETTLED: the store it would have needed is never
    opened, probed or digested.
    """
    classes: list[ClassPlan] = []
    for spec in CONTRIBUTOR_REGISTRY:
        kind = spec.kind
        if (kind not in _CONVERSATION_CLASSES
                and kind not in _PROVIDER_DEPENDENT_S3_CLASSES):
            # The four accounting-native classes. They read `cache.db` and
            # `stats.db` only, which every plan opens.
            classes.append(ClassPlan(kind, ClassMode.MEASURE.value, None,
                                     False, False))
            continue
        if kind == "cache_churn" and source == "codex":
            # Capability, not availability. Codex retains a cached-input ratio
            # and no loss predicate, so this is never measurable there and is
            # excluded from completeness rather than withheld.
            classes.append(ClassPlan(kind, ClassMode.NOT_APPLICABLE.value, None,
                                     False, False))
            continue
        needs_conversations = (
            kind in _CONVERSATION_CLASSES
            or (kind in _PROVIDER_DEPENDENT_S3_CLASSES and source == "claude")
        )
        if needs_conversations and not transcripts_visible:
            classes.append(ClassPlan(kind, ClassMode.WITHHOLD.value,
                                     WithheldCause.TRANSCRIPTS_NOT_VISIBLE.value,
                                     False, False))
            continue
        classes.append(ClassPlan(kind, ClassMode.MEASURE.value, None,
                                 needs_conversations, True))
    return ExecutionPlan(source=source, classes=tuple(classes))


def establish_plan(policy: ExecutionPlan, *, conversations_available: bool,
                   provider_cause: str | None) -> ExecutionPlan:
    """Stage 2, folding in what opening the permitted stores revealed.

    `provider_cause` is the accounting-store overlay. A provider whose
    accounting store cannot be read has no denominator at all, so it preempts
    every cell — including the ones that measure — except `not_applicable`,
    which is a capability statement rather than an availability one and stays
    true whatever the store did.
    """
    out: list[ClassPlan] = []
    for c in policy.classes:
        if c.mode == ClassMode.NOT_APPLICABLE.value:
            out.append(c)
            continue
        if provider_cause is not None:
            out.append(ClassPlan(c.kind, ClassMode.WITHHOLD.value,
                                 provider_cause, False, False))
            continue
        if (c.mode == ClassMode.MEASURE.value and c.requires_conversations
                and not conversations_available):
            out.append(ClassPlan(c.kind, ClassMode.WITHHOLD.value,
                                 WithheldCause.SIGNAL_UNAVAILABLE.value,
                                 False, False))
            continue
        out.append(c)
    return ExecutionPlan(source=policy.source, classes=tuple(out))


# --- confidence ---------------------------------------------------------

def assess_confidence(coverage: PopulationCoverage) -> str:
    """Three-valued confidence over `usd_coverage`.

    `high` requires complete coverage and at least
    `CONFIDENCE_HIGH_MIN_SUPPORT` support units. `medium` requires at least
    `CONFIDENCE_MEDIUM_MIN_COVERAGE` and `CONFIDENCE_MEDIUM_MIN_SUPPORT`.
    Anything else is `low`. Absent coverage is `low`, not `high`: we do not
    know that it is complete.
    """
    usd = coverage.usd_coverage
    if usd is None:
        return "low"
    support = coverage.support_units or 0
    if usd >= 1.0 - COVERAGE_EPSILON and support >= CONFIDENCE_HIGH_MIN_SUPPORT:
        return "high"
    if (usd >= CONFIDENCE_MEDIUM_MIN_COVERAGE - COVERAGE_EPSILON
            and support >= CONFIDENCE_MEDIUM_MIN_SUPPORT):
        return "medium"
    return "low"


def baseline_is_comparable(confidence: str) -> bool:
    """A baseline comparison requires at least `medium` confidence.

    Below that only the baseline field is withheld; the observed-cost result
    still renders. That separation is what keeps a thin baseline from
    suppressing a measured answer.
    """
    return confidence in ("high", "medium")


# --- classification -----------------------------------------------------

def _derive_population(subjects: Sequence[SubjectFacts],
                       denominator: Denominator) -> PopulationCoverage:
    """A complete coverage over exactly the handed-in subjects.

    Used only when the caller supplies none, which is the pure-unit-test
    case: the adapter always measures and passes its own.
    """
    return PopulationCoverage(
        requested_start=denominator.window_start,
        requested_end=denominator.window_end,
        observed_start=denominator.window_start,
        observed_end=denominator.window_end,
        count_coverage=1.0,
        usd_coverage=1.0,
        support_units=sum(s.priced_entry_count for s in subjects),
    )


class _BlankDefaults(dict):
    """A format mapping that renders an unsupplied key as nothing at all."""

    def __missing__(self, key: str) -> str:
        return ""


# The next step a class with no generic template falls back to. It navigates
# to the WINDOW rather than to the subject, and interpolates no subject key,
# so it cannot emit a string a shell would re-interpret. Nothing in production
# reaches it — every block subject carries its own override — and it exists so
# that a hand-built subject still yields a runnable command instead of raising
# into the report or printing an empty `-> Run`.
_SUBJECTLESS_NEXT_STEP = (
    "cctally {source} daily{mode_flag} "
    "--since {window_start_date} --until {window_end_date}"
)


def _render_next_step(spec: ContributorSpec, subject: SubjectFacts,
                      context: Mapping[str, Any]) -> str:
    if subject.next_step:
        return subject.next_step
    template = spec.next_step_template or _SUBJECTLESS_NEXT_STEP
    values = dict(context)
    values.setdefault("source", "claude")
    values.setdefault("mode_flag", "")
    values.setdefault("window_start_date", "")
    values.setdefault("window_end_date", "")
    values["subject_key"] = subject.subject_key
    values["subject_label"] = subject.subject_label
    try:
        return template.format(**values)
    except KeyError:
        # A template naming a key the caller did not supply must still produce
        # a runnable command rather than raising into the report. Substituting
        # blanks keeps the whole command; leaving a raw `{placeholder}` in text
        # a person is told to run would not.
        rendered = template.format_map(_BlankDefaults(values))
        return " ".join(rendered.split())


def _build_row(spec: ContributorSpec, subject: SubjectFacts,
               denominator: Denominator, population: PopulationCoverage,
               confidence: str, context: Mapping[str, Any],
               evidence: Mapping[str, EvidenceField] | None = None
               ) -> ContributorRow:
    # The subject's qualifications are attached ONCE, to the observed cost.
    # Attaching the same tuple to the share too shipped `block_precedes_window`
    # twice per row, which reads as two separate qualifications of two separate
    # figures rather than one statement about the subject.
    observed = available(subject.observed_usd, population,
                         subject.qualifications)
    # `classify_class` is the single gate on the denominator: it returns
    # before building any row when the denominator is withheld or not
    # positive, so there is no second guard here to fall out of step with it.
    share = available(subject.observed_usd / denominator.usd_value, population)

    if subject.baseline_code is not None:
        baseline = withheld(subject.baseline_code, population)
    elif not baseline_is_comparable(confidence):
        baseline = withheld(BaselineOutcome.BASELINE_INSUFFICIENT.value,
                            population)
    elif subject.baseline_share is None:
        baseline = withheld(BaselineOutcome.BASELINE_INSUFFICIENT.value,
                            population)
    else:
        baseline = available(subject.baseline_share, population)

    return ContributorRow(
        contributor_class=spec.kind,
        subject_kind=spec.subject_kind,
        subject_key=subject.subject_key,
        subject_label=subject.subject_label,
        rank=None,
        observed_usd=observed,
        share=share,
        baseline=baseline,
        confidence=confidence,
        is_fallback_pricing=subject.is_fallback_pricing,
        next_step=_render_next_step(spec, subject, context),
        # An immutable view, so the mapping a caller receives cannot be
        # mutated through the row. Empty for every S2 class, which is what
        # keeps `_row_to_wire` from emitting the key at all.
        evidence=(FrozenDict(evidence) if evidence else {}),
    )


def support_shortfall(spec: ContributorSpec,
                      subjects: Sequence[SubjectFacts],
                      population: PopulationCoverage,
                      *, predicate_evaluated: bool = False) -> str | None:
    """Which support minimum this class failed, or `None` when support is met.

    The cause `insufficient_population` is one word for four different
    shortfalls, and the count a surface prints beside it belongs to only one of
    them. A class with sixty priced entries under a single model fails the
    distinct-subject minimum, and printing `support 60 units` beside the cause
    without naming that reads as a contradiction.
    """
    # An evaluated predicate that matched nothing has no subjects BY RESULT,
    # not by absence of data, so the distinct-subject minimum does not apply to
    # it. Every other minimum still does: a class that looked at four entries
    # and found nothing has not established a healthy window.
    if not (predicate_evaluated and not subjects):
        if len(subjects) < spec.min_distinct_subjects:
            return SUPPORT_SHORTFALL_DISTINCT_SUBJECTS
    units = population.support_units or 0
    if units < max(spec.min_priced_entries, WITHHOLD_MIN_SUPPORT):
        return SUPPORT_SHORTFALL_PRICED_ENTRIES
    usd = population.usd_coverage
    if usd is not None and usd < WITHHOLD_MIN_COVERAGE - COVERAGE_EPSILON:
        return SUPPORT_SHORTFALL_USD_COVERAGE
    return None


def classify_class(
    spec: ContributorSpec,
    subjects: Sequence[SubjectFacts],
    denominator: Denominator,
    *,
    population: PopulationCoverage | None = None,
    preempting_cause: str | None = None,
    not_applicable: bool = False,
    next_step_context: Mapping[str, Any] | None = None,
    predicate_evaluated: bool = False,
    evidence: Mapping[str, EvidenceField] | None = None,
) -> ClassResult:
    """Turn one class's subjects into a verdict and its reported rows.

    `contributor` when the top subject's share of the class denominator is at
    least `DIAGNOSIS_CONTRIBUTOR_SHARE_FLOOR` and support is met.
    `no_contributor` when support is met and the top share falls below the
    floor — a measured, available answer, not a withheld one. `withheld` with
    `insufficient_population` when support is not met. `not_applicable` when
    the provider structurally cannot support the class.

    Ties are resolved by reporting every subject within
    `DIAGNOSIS_TIE_EPSILON_USD` of the top, ordered by subject key. The floor
    boundary is inclusive: a share of exactly 0.20 is a contributor.
    """
    context = dict(next_step_context or {})
    pop = population if population is not None else _derive_population(
        subjects, denominator
    )

    if not_applicable:
        return ClassResult(spec.kind, VerdictState.NOT_APPLICABLE.value,
                           None, (), pop)

    if preempting_cause is not None:
        return ClassResult(spec.kind, VerdictState.WITHHELD.value,
                           preempting_cause, (), pop)

    shortfall = support_shortfall(spec, subjects, pop,
                                  predicate_evaluated=predicate_evaluated)
    if shortfall is not None:
        return ClassResult(spec.kind, VerdictState.WITHHELD.value,
                           WithheldCause.INSUFFICIENT_POPULATION.value, (), pop,
                           support_shortfall=shortfall)

    if not denominator.usd_is_available:
        # The denominator itself is withheld, so no share can be divided by
        # it. The class carries the denominator's own cause rather than
        # inventing a thinner one.
        return ClassResult(spec.kind, VerdictState.WITHHELD.value,
                           denominator.usd.code
                           or WithheldCause.CALCULATION_FAILED.value, (), pop)

    if denominator.usd_value <= 0.0:
        # There is a population but no dollars to divide by, so no share can
        # be stated. That is unmeasurable, not healthy.
        return ClassResult(spec.kind, VerdictState.WITHHELD.value,
                           WithheldCause.INSUFFICIENT_POPULATION.value, (), pop,
                           support_shortfall=SUPPORT_SHORTFALL_NO_PRICED_DOLLARS)

    confidence = assess_confidence(pop)

    # PLACEMENT IS LOAD-BEARING. This sits after BOTH denominator guards and
    # after confidence, and before `ordered[0]`. Placed earlier — the obvious
    # reading of "before indexing" — a class whose denominator was withheld or
    # zero reports healthy, which is the exact defect those two guards exist
    # to prevent. An evaluated predicate that matched nothing has measured
    # something: the healthy inverse, and the class counts as applicable
    # rather than withheld.
    if predicate_evaluated and not subjects:
        return ClassResult(spec.kind, VerdictState.NO_CONTRIBUTOR.value,
                           None, (), pop, confidence=confidence)

    ordered = sorted(subjects, key=lambda s: (-s.observed_usd, s.subject_key))
    top_usd = ordered[0].observed_usd
    top_share = top_usd / denominator.usd_value

    # `top_share` is a ratio of two independently accumulated float sums, so a
    # subject genuinely at one fifth of the denominator can compute as
    # 0.19999999999999998. The same slack this repository applies before
    # `math.floor` on a percent-like float applies here, or a measured
    # contributor flips to a healthy `no_contributor` on the last bit. The
    # slack is PUBLISHED (`DIAGNOSIS_SHARE_FLOOR_EPSILON`) because it decides
    # this verdict, and a client applying a bare `share >= floor` to the
    # published share would disagree with the server on exactly that row.
    if top_share < (DIAGNOSIS_CONTRIBUTOR_SHARE_FLOOR
                    - DIAGNOSIS_SHARE_FLOOR_EPSILON):
        return ClassResult(spec.kind, VerdictState.NO_CONTRIBUTOR.value,
                           None, (), pop, confidence=confidence)

    tied = [s for s in ordered
            if abs(s.observed_usd - top_usd) <= DIAGNOSIS_TIE_EPSILON_USD]
    tied.sort(key=lambda s: s.subject_key)
    rows = tuple(
        _build_row(spec, s, denominator, pop, confidence, context, evidence)
        for s in tied
    )
    return ClassResult(spec.kind, VerdictState.CONTRIBUTOR.value, None,
                       rows, pop, confidence=confidence)


# --- ranking ------------------------------------------------------------

def _row_usd(row: ContributorRow) -> float:
    value = row.observed_usd.value
    return float(value) if isinstance(value, (int, float)) else 0.0


def rank_rows(rows: Iterable[ContributorRow]) -> tuple[ContributorRow, ...]:
    """Rank heterogeneous contributor rows by `(-observedUsd, registryOrder,
    subject.key)` and stamp a one-based rank onto each."""
    ordered = sorted(
        rows,
        key=lambda r: (
            -_row_usd(r),
            REGISTRY_ORDER.get(r.contributor_class, len(CONTRIBUTOR_REGISTRY)),
            r.subject_key,
        ),
    )
    return tuple(
        replace(row, rank=index) for index, row in enumerate(ordered, start=1)
    )


# --- the overall verdict ------------------------------------------------

def count_applicable(results: Sequence[ClassResult]) -> int:
    return sum(1 for r in results
               if r.verdict != VerdictState.NOT_APPLICABLE.value)


def count_withheld(results: Sequence[ClassResult]) -> int:
    return sum(1 for r in results
               if r.verdict == VerdictState.WITHHELD.value)


def overall_verdict(results: Sequence[ClassResult]) -> tuple[str, str | None]:
    """`no_contributor_detected` only when every applicable class is
    available and none reports a contributor.

    **The two overall verdicts are deliberately asymmetric.**
    `no_contributor_detected` is a claim about the whole population — nothing
    in this window reached the floor — so it requires every applicable class
    to be available. `contributor_detected` claims only that a contributor was
    found, which stays true beside a withheld sibling class, so a measured
    contributor is not erased by an unmeasurable sibling. Because
    `contributor_detected` is therefore not a complete account, every surface
    states how many applicable classes were withheld alongside it; see
    `count_withheld`.

    `not_applicable` classes are excluded from the completeness test. Without
    that exclusion a provider with a permanently unsupported class could never
    render as healthy.
    """
    applicable = [r for r in results
                  if r.verdict != VerdictState.NOT_APPLICABLE.value]
    if any(r.verdict == VerdictState.CONTRIBUTOR.value for r in applicable):
        return OverallVerdict.CONTRIBUTOR_DETECTED.value, None

    withheld_codes = [r.code for r in applicable
                      if r.verdict == VerdictState.WITHHELD.value]
    if withheld_codes:
        for cause in WITHHELD_CAUSE_PRECEDENCE:
            if cause in withheld_codes:
                return OverallVerdict.WITHHELD.value, cause
        return OverallVerdict.WITHHELD.value, withheld_codes[0]

    return OverallVerdict.NO_CONTRIBUTOR_DETECTED.value, None


# --- the assembled report -----------------------------------------------

@dataclass(frozen=True)
class ProviderResult:
    """One provider's whole answer: its own denominator, classes, ranked
    contributors and verdict. Nothing is ranked across providers and no
    denominator spans them."""

    source: str
    account_key: str | None
    effective_speed: str | None
    denominator: Denominator
    classes: tuple[ClassResult, ...]
    contributors: tuple[ContributorRow, ...]
    verdict: str
    code: str | None
    applicable_class_count: int = 0
    withheld_class_count: int = 0
    generation: Any | None = None
    coverage: PopulationCoverage | None = None
    # The ESTABLISHED `ExecutionPlan` this result ran under. Threaded
    # explicitly rather than hung off the scope, because `_scope_for`
    # reconstructs scopes and would discard a field merely attached to one.
    plan: "ExecutionPlan | None" = None


def build_provider_result(
    source: str,
    account_key: str | None,
    effective_speed: str | None,
    denominator: Denominator,
    classes: Sequence[ClassResult],
    *,
    generation: Any | None = None,
    coverage: PopulationCoverage | None = None,
    plan: "ExecutionPlan | None" = None,
) -> ProviderResult:
    verdict, code = overall_verdict(classes)
    ranked = rank_rows([row for result in classes for row in result.rows])
    # The same row appears in the ranked `contributors` view and in its own
    # class, so both carry the rank. Publishing `rank: null` on the class row
    # and a number on the contributor row is two representations of one row
    # disagreeing about it.
    ranks = {(row.contributor_class, row.subject_key): row.rank
             for row in ranked}
    classes = [
        replace(result, rows=tuple(
            replace(row, rank=ranks.get((row.contributor_class,
                                         row.subject_key)))
            for row in result.rows
        ))
        for result in classes
    ]
    return ProviderResult(
        source=source,
        account_key=account_key,
        effective_speed=effective_speed,
        denominator=denominator,
        classes=tuple(classes),
        contributors=ranked,
        verdict=verdict,
        code=code,
        applicable_class_count=count_applicable(classes),
        withheld_class_count=count_withheld(classes),
        generation=generation,
        coverage=coverage,
        plan=plan,
    )


@dataclass(frozen=True)
class DiagnosisReport:
    contract_version: int
    measured_at: str
    window: DiagnosisWindow
    results: tuple[ProviderResult, ...]
    overall_verdict: str
    overall_code: str | None
    applicable_class_count: int = 0
    withheld_class_count: int = 0


def build_report(measured_at: str, window: DiagnosisWindow,
                 results: Sequence[ProviderResult]) -> DiagnosisReport:
    """Fold provider verdicts into one report verdict.

    A withheld provider withholds the report, and a contributor anywhere is
    a positive finding, which is the same precedence `overall_verdict`
    applies within a provider.
    """
    verdicts = [r.verdict for r in results]
    if OverallVerdict.CONTRIBUTOR_DETECTED.value in verdicts:
        overall, code = OverallVerdict.CONTRIBUTOR_DETECTED.value, None
    elif OverallVerdict.WITHHELD.value in verdicts:
        codes = [r.code for r in results
                 if r.verdict == OverallVerdict.WITHHELD.value]
        code = next((c for c in WITHHELD_CAUSE_PRECEDENCE if c in codes), None)
        overall = OverallVerdict.WITHHELD.value
    elif verdicts:
        overall, code = OverallVerdict.NO_CONTRIBUTOR_DETECTED.value, None
    else:
        # UNREACHABLE from the product, and kept deliberately.
        # `build_diagnosis` always passes at least one `ProviderResult` — one
        # per physical provider the request named, and `--source all` names
        # two — so `results` is never empty there. It would become reachable if
        # a caller ever built a report from a filtered result list, and a
        # report of nothing must then say `withheld`, never
        # `no_contributor_detected`: claiming no contributor was detected over
        # zero providers asserts a measurement that was never attempted.
        overall, code = OverallVerdict.WITHHELD.value, \
            WithheldCause.PROVIDER_UNAVAILABLE.value
    return DiagnosisReport(
        contract_version=DIAGNOSIS_CONTRACT_VERSION,
        measured_at=measured_at,
        window=window,
        results=tuple(results),
        overall_verdict=overall,
        overall_code=code,
        applicable_class_count=sum(r.applicable_class_count for r in results),
        withheld_class_count=sum(r.withheld_class_count for r in results),
    )


def unreadable_store_is_terminal(report: DiagnosisReport) -> bool:
    """Whether a requested provider's store could not be read AND nothing else
    answered.

    A store that cannot be read is an infrastructure failure, not an answer
    about data availability, so the surfaces report it as a failure: the CLI
    prints the withheld report and exits 3, and the route publishes the same
    report with 503. Every other withheld cause —
    `retained_range_mismatch`, `stale_evidence`, `pricing_unavailable`,
    `insufficient_population` — is a legitimate answer about what the store
    holds, and exits 0.

    The second half of the predicate is what keeps `--source all` honest: one
    provider unreadable while the other reports is a report, so it exits 0.

    **"Answered" is a CLASS-level question, not an overall-verdict one.** A
    provider whose classes measured three `no_contributor` results and withheld
    the fourth produced a measurement, but `overall_verdict` returns `withheld`
    for it, because that verdict is a claim about the whole population and a
    withheld sibling class makes the population claim incomplete. Reading the
    overall verdict here therefore counted a complete answer as nothing having
    answered: a Claude install whose status-line hook was never wired holds no
    `five_hour_blocks`, so `five_hour_bursts` is withheld while the other three
    classes measure, and `--source all` beside an unreadable Codex store then
    exited 3 and served 503 for a report that had measured three classes.
    `not_applicable` is not a measurement either — it says the class does not
    apply to this provider, which is not an answer about the window.
    """
    # Both `any()` calls return `False` over an empty `results`, so a
    # zero-provider report exits 0 rather than 3. That is unreachable for the
    # same reason the `else` branch of `build_report` is — `build_diagnosis`
    # always passes at least one provider result — and it is the right answer
    # anyway: with no provider requested, nothing was unreadable. A caller that
    # ever builds a report from a filtered result list must decide whether an
    # empty list is a failure BEFORE reaching here, because this predicate
    # cannot tell "nothing was asked" from "nothing failed".
    unreadable = any(
        result.verdict == OverallVerdict.WITHHELD.value
        and result.code == WithheldCause.PROVIDER_UNAVAILABLE.value
        for result in report.results
    )
    answered = any(
        class_result.verdict not in (VerdictState.WITHHELD.value,
                                     VerdictState.NOT_APPLICABLE.value)
        for result in report.results
        for class_result in result.classes
    )
    return unreadable and not answered
