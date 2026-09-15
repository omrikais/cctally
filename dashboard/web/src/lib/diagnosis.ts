// #620 S2 — the client half of the diagnosis contract.
//
// `GET /api/diagnosis` publishes exactly what `diagnosis_to_wire` produces, so
// these types mirror that adapter. Three properties of the wire decide how they
// are written here, and each one has bitten this repository before:
//
//   * **Every measured quantity is an `EvidenceField`**, including the
//     denominator. A withheld field carries `value: null` and a `code`, and it
//     must never render as `$0.00` — the retained cost of a population nothing
//     could price is unknown, not zero.
//   * **Every cause code is a bare `string` with a REQUIRED fallback branch.**
//     The union is closed on the server and deliberately open here, because the
//     in-place update path lets an old client meet a newer server without
//     reloading the JavaScript. S3 adds two causes additively.
//   * **An unmeasurable coverage dimension is ABSENT, not zero**, and
//     `supportUnits` may be `null`, meaning not measured. Rendering an absent
//     dimension as `0` asserts a measurement nobody made.

export type EvidenceState = 'available' | 'withheld';

export interface PopulationCoverage {
  requestedStart: string;
  requestedEnd: string;
  observedStart: string | null;
  observedEnd: string | null;
  /** `null` means not measured. Zero would mean measured and empty. */
  supportUnits: number | null;
  gapCodes: string[];
  // Absent when the dimension could not be measured — never zero.
  countCoverage?: number;
  usdCoverage?: number;
  identityCoverage?: number;
  retentionCoverage?: number;
  pricingCoverage?: number;
  /** #620 S3. `evaluated / candidate`, and the ONLY dimension that falls when
   *  a row could not be decided. Published by the three conversation-derived
   *  classes only, so it is absent on every S2 coverage block. */
  evaluabilityCoverage?: number;
  /** #620 S3. Per dimension, its numerator and denominator in words, so a
   *  reader never has to infer which population a figure was computed over.
   *  Published by S3 coverage objects only; S2's are byte-frozen. */
  dimensions?: Record<string, string>;
}

export interface EvidenceField<T = number> {
  state: EvidenceState;
  value: T | null;
  population: PopulationCoverage | null;
  qualifications: string[];
  code?: string;
}

export interface DiagnosisRow {
  contributorClass: string;
  subjectKind: string;
  subjectKey: string;
  subjectLabel: string;
  rank: number | null;
  observedUsd: EvidenceField;
  share: EvidenceField;
  baseline: EvidenceField;
  confidence: string;
  isFallbackPricing: boolean;
  nextStep: string;
  /** #620 S3. Class-specific evidence, keyed by its published camelCase name.
   *  Absent on every S2 row, and each member is a full `EvidenceField`, so one
   *  figure can be withheld while the row still reports its cost. */
  evidence?: Record<string, EvidenceField>;
}

export interface DiagnosisClass {
  contributorClass: string;
  verdict: string;
  code: string | null;
  supportShortfall: string | null;
  /** `null` when the class measured nothing, which is NOT `low`: confidence
   *  is a statement about a measurement. The server assesses it; nothing here
   *  recomputes that rule from the published constants. */
  confidence: string | null;
  population: PopulationCoverage | null;
  rows: DiagnosisRow[];
}

export interface DiagnosisDenominator {
  identity: string;
  source: string;
  accountKey: string | null;
  windowStart: string;
  windowEnd: string;
  populationDigest: string;
  usd: EvidenceField;
}

export interface DiagnosisResult {
  source: string;
  accountKey: string | null;
  effectiveSpeed: string | null;
  generation: { stats: string; cache: string; configuration: string;
    /** #620 S3. Present only when the plan actually read conversation bytes,
     *  so a denied request publishes no such component at all. */
    conversations?: string;
    generationId?: string } | null;
  denominator: DiagnosisDenominator;
  coverage: PopulationCoverage | null;
  verdict: string;
  code: string | null;
  applicableClassCount: number;
  withheldClassCount: number;
  contributors: DiagnosisRow[];
  classes: DiagnosisClass[];
}

/** The published predicate of one class. `sentence` is what a person reads;
 *  `parameters` is the machine-readable form, so a consumer can reproduce the
 *  rule rather than parse English. Absent for the four accounting classes,
 *  whose registry shape is byte-frozen. */
export interface DiagnosisClassRule {
  sentence: string;
  parameters: Record<string, unknown>;
}

export interface DiagnosisRegistryEntry {
  contributorClass: string;
  subjectKind: string;
  label: string;
  minDistinctSubjects: number;
  minPricedEntries: number;
  rule?: DiagnosisClassRule;
}

export interface DiagnosisConstants {
  contributorShareFloor: number;
  shareFloorEpsilon: number;
  tieEpsilonUsd: number;
  confidenceHighMinSupport: number;
  confidenceMediumMinSupport: number;
  confidenceMediumMinCoverage: number;
  withholdMinCoverage: number;
  withholdMinSupport: number;
  // #620 S3. Each decides a verdict, so each is published beside the rule that
  // consumes it. Optional, because an older server publishes none of them and
  // the in-place update path lets an old server meet a new client too.
  shortConversationMaxHumanTurns?: number;
  largeContextMinWindowFraction?: number;
  minSubagentBuckets?: number;
  seedScanBudgetRows?: number;
  conversationNormalizeBudgetRows?: number;
  codexEventScanBudgetRows?: number;
  /** The closed-on-the-server gap-code vocabulary. Published so a client can
   *  tell a code from a NEWER server apart from one it should have handled —
   *  which is the whole reason `gapCodeMessage` still has a fallback branch. */
  gapCodes?: string[];
  /** The turn predicate the two turn-based classes are defined over. */
  turnDefinition?: string;
  registry: DiagnosisRegistryEntry[];
}

export interface DiagnosisReport {
  schemaVersion: number;
  contractVersion: number;
  measuredAt: string;
  window: { startAt: string; endAt: string; tz: string; label: string };
  constants: DiagnosisConstants;
  notes: { scope: string; overlap: string };
  overallVerdict: string;
  overallCode: string | null;
  applicableClassCount: number;
  withheldClassCount: number;
  results: DiagnosisResult[];
}

/** What the modal is being asked about. All four scope fields may be null,
 *  which asks about the current window for the active provider. */
export interface DiagnosisRequest {
  source: string;
  accountKey: string | null;
  windowStartAt: string | null;
  windowEndAt: string | null;
}

export function diagnosisUrl(request: DiagnosisRequest): string {
  const params = new URLSearchParams({ source: request.source });
  if (request.accountKey != null && request.accountKey !== '') {
    params.set('account', request.accountKey);
  }
  if (request.windowStartAt != null && request.windowEndAt != null) {
    // Explicit half-open bounds, because a followed warning names instants
    // rather than calendar days — a five-hour block start is not a date.
    params.set('start_at', request.windowStartAt);
    params.set('end_at', request.windowEndAt);
  }
  return `/api/diagnosis?${params.toString()}`;
}

/**
 * Whether a share reaches the classification floor, by the PUBLISHED rule.
 *
 * The rule is `share >= floor - epsilon`, and both halves are on the wire for
 * exactly this reason: a share is a ratio of two independently accumulated
 * float sums, so a subject genuinely at one fifth of the denominator publishes
 * as 0.19999999999999998. A client applying a bare `share >= 0.2` to that
 * computes `no_contributor` where the server published `contributor`, and the
 * badge then contradicts the verdict beside it on that exact row.
 */
export function reachesContributorFloor(
  share: number | null | undefined,
  constants: Pick<DiagnosisConstants, 'contributorShareFloor' | 'shareFloorEpsilon'>,
): boolean {
  if (typeof share !== 'number' || !Number.isFinite(share)) return false;
  return share >= constants.contributorShareFloor - constants.shareFloorEpsilon;
}

/**
 * Copy for one withheld evidence cause.
 *
 * The switch has a REQUIRED fallback branch, for the same reason
 * `withheldMessage` in `withheldCopy.ts` does: the code union is closed on the
 * server and open here, so an unheard-of code must render honest generic copy
 * naming the code rather than nothing at all.
 */
export function diagnosisWithheldMessage(code: string | null | undefined): string {
  switch (code) {
    case 'provider_unavailable':
      return 'This provider’s store could not be read, so nothing here was measured.';
    case 'retained_range_mismatch':
      return 'The store does not retain this whole window, so it was not measured.';
    case 'pricing_unavailable':
      return 'Nothing in this window could be priced, so there are no dollars to divide.';
    case 'unattributed_evidence':
      return 'Nothing in this window carried the identity this class groups by.';
    case 'stale_evidence':
      return 'Part of this window was pruned from the store, so it cannot be measured.';
    case 'insufficient_population':
      return 'There is too little in this window to measure this class.';
    case 'transcripts_not_visible':
      // #620 S3 spec 5.4. This says only what is true of EVERY class denied
      // for this cause. The asymmetry sentence that used to end this string
      // is `TRANSCRIPT_ASYMMETRY_NOTE`, and it moved out because this mapper
      // cannot see whether the asymmetry it describes is in the report —
      // which put a sentence about Codex subagent attribution under
      // `cache_churn` on a report with no Codex result in it at all.
      return 'This dashboard is not authorized to read transcripts, so this '
        + 'class was not measured.';
    case 'signal_unavailable':
      return 'The local transcript store could not answer this class, so the '
        + 'signal could not be established at all.';
    case 'calculation_failed':
      return 'This measurement could not be computed.';
    case 'baseline_insufficient':
      return 'No comparable earlier window could be established.';
    case null:
    case undefined:
    case '':
      return 'This measurement is unavailable.';
    default:
      // An unknown cause from a newer server. Say what is true — it is
      // unavailable — and carry the code so a bug report can name it.
      return `This measurement is unavailable (${code}).`;
  }
}

/** The one class whose two providers read different stores, so it is the only
 *  class the transcript asymmetry is a statement about: Claude subagent
 *  structure lives in `conversations.db` and the Codex signal lives in
 *  `cache.db`. The terminal names the same class literally, for the same
 *  reason — the server holds the set as `_PROVIDER_DEPENDENT_S3_CLASSES` and
 *  does not publish it. */
export const TRANSCRIPT_ASYMMETRY_CLASS = 'subagent_fanout';

/** Why one provider measured a class this provider was denied.
 *
 *  This was the tail of `diagnosisWithheldMessage('transcripts_not_visible')`,
 *  so it rendered under every class denied for that cause on every report. It
 *  explains a contrast, and a report has to contain both halves of one for the
 *  sentence to be true: a Claude-only report has no Codex row to contrast
 *  with, and `cache_churn` is not a class the sentence says anything about.
 *  `transcriptAsymmetryPresent` decides whether the contrast is there.
 *
 *  The wording is shared verbatim with the second sentence of
 *  `DIAGNOSIS_TRANSCRIPT_ASYMMETRY_SENTENCE` in `bin/_lib_diagnosis.py` and
 *  with `explain --help`;
 *  `test_the_shared_transcript_asymmetry_clause_is_stated_in_the_same_words`
 *  is what keeps the three from drifting apart. */
export const TRANSCRIPT_ASYMMETRY_NOTE = 'Codex subagent attribution is '
  + 'derived from accounting metadata and needs no transcript access.';

/** Whether ANOTHER source in this report measured the class named here.
 *
 *  A class counts as measured when it produced a contributor row, or when its
 *  class entry carries a verdict other than `withheld` and `not_applicable` —
 *  `no_contributor` is a measurement that found nothing, which is exactly the
 *  contrast the note describes, while `not_applicable` says the provider
 *  structurally cannot support the class and so is no contrast at all. */
export function transcriptAsymmetryPresent(
  report: DiagnosisReport, deniedSource: string,
): boolean {
  return report.results.some(
    (result) => result.source !== deniedSource
      && classIsMeasured(result, TRANSCRIPT_ASYMMETRY_CLASS),
  );
}

function classIsMeasured(result: DiagnosisResult, kind: string): boolean {
  if (result.contributors.some((row) => row.contributorClass === kind)) {
    return true;
  }
  return result.classes.some(
    (entry) => entry.contributorClass === kind
      && entry.verdict !== 'withheld' && entry.verdict !== 'not_applicable',
  );
}

/**
 * Copy for one withheld evidence FIGURE, scoped to the figure.
 *
 * `diagnosisWithheldMessage` is the CLASS vocabulary: its sentences say what
 * the class could not measure. One of them, "There is too little in this
 * window to measure this class.", was rendered under a single withheld member
 * of a row that reported its cost, its share and its two other figures, so
 * that sentence contradicted the row it sat on. A member can be withheld
 * while its class is measured, so it needs its own words.
 *
 * The terminal states the same fact as `median human turns withheld
 * (insufficient_population)`, which is already scoped to the figure; this is
 * that statement in English.
 *
 * Required fallback branch for the same reason every other cause mapper here
 * has one: the cause vocabulary is closed on the server and open in the
 * client, so a code from a newer server must say something honest and name
 * itself rather than render as nothing.
 */
export function evidenceWithheldMessage(code: string | null | undefined): string {
  switch (code) {
    case 'insufficient_population':
      return 'not measured: nothing in this class supplied this figure';
    case null:
    case undefined:
    case '':
      return 'not measured';
    default:
      return `not measured (${code})`;
  }
}

/** Which support minimum an `insufficient_population` class missed. Also has a
 *  required fallback branch: the shortfall vocabulary is closed on the server
 *  and open here for the same reason the causes are. */
export function supportShortfallMessage(
  code: string | null | undefined,
  spec: DiagnosisRegistryEntry | undefined,
): string {
  switch (code) {
    case 'min_distinct_subjects':
      return `fewer than ${spec?.minDistinctSubjects ?? 2} distinct `
        + `${spec?.subjectKind ?? 'subject'}s`;
    case 'min_priced_entries':
      return `fewer than ${spec?.minPricedEntries ?? 20} priced entries`;
    case 'min_usd_coverage':
      return 'too little of the window’s cost could be measured';
    case 'no_priced_dollars':
      return 'no priced dollars to divide by';
    case null:
    case undefined:
    case '':
      return '';
    default:
      return code;
  }
}

/** The human label for a contributor class, taken from the PUBLISHED registry
 *  rather than a client-side copy of it — a second copy would drift the moment
 *  a class is added, and would leave an S3 class rendering as its raw key. */
export function classLabel(
  contributorClass: string,
  constants: DiagnosisConstants | undefined,
): string {
  const entry = constants?.registry?.find(
    (r) => r.contributorClass === contributorClass,
  );
  return entry?.label ?? contributorClass;
}

export function registryEntry(
  contributorClass: string,
  constants: DiagnosisConstants | undefined,
): DiagnosisRegistryEntry | undefined {
  return constants?.registry?.find(
    (r) => r.contributorClass === contributorClass,
  );
}

/**
 * Whether this class states its undecided rows on this surface.
 *
 * The same derivation `_publishes_gaps` makes in `bin/_cctally_diagnosis.py`,
 * and it must stay the same one: a spec carries a `rule` exactly when it is
 * one of the three conversation-derived classes, which are the ones whose
 * predicate can fail to decide a row. The four accounting classes publish gap
 * codes of their own — `unmatched_pool_window`, `unresolved_project_identity`
 * — that are NOT in `constants.gapCodes` and were never meant to be read as
 * undecided rows, and the modal was rendering them as such because it had no
 * equivalent of this guard.
 *
 * THE DERIVATION IS AN INFERENCE. Giving an accounting spec a `rule` would
 * silently add an `undecided:` line to that class here as well as on the
 * terminal. It is left as an inference rather than a second list of class
 * names because a hand-maintained list is the failure this avoids, and
 * because the two surfaces then cannot disagree about which classes publish
 * gap lines.
 */
export function publishesGaps(
  contributorClass: string,
  constants: DiagnosisConstants | undefined,
): boolean {
  return registryEntry(contributorClass, constants)?.rule != null;
}


/**
 * Copy for one gap code — a row the class could not DECIDE, which is a
 * different statement from a field it could not publish.
 *
 * It has the same required fallback branch the cause messages have, and for
 * the same reason: the vocabulary is closed on the server and open here, so a
 * code from a newer server must render honest generic copy naming the code
 * rather than nothing at all. `constants.gapCodes` publishes the set the
 * server knows, which is what lets a reader tell those two apart.
 *
 * Each branch returns the PHRASE only. The `undecided:` lead-in is written
 * once by the caller, over the joined list, exactly as the terminal writes it
 * once over its joined list of codes. Writing it per branch left the
 * recognised codes as the only gap messages missing the word, which was the
 * one non-colour cue separating the gap line from the coverage line directly
 * above it (#620 S3, browser round 1).
 */
export function gapCodeMessage(code: string | null | undefined): string {
  switch (code) {
    case 'unknown_context_window':
      return 'some requests had no known model context window';
    case 'scan_budget_exhausted':
      return 'some conversations held more history than the scan budget allows';
    case 'unresolved_subagent_attribution':
      return 'some subagent spend could not be joined to exactly one parent';
    case 'ambiguous_origin_category':
      return 'some threads carried an origin category this build cannot read';
    // #834 S2 (#800). The accounting rows name a conversation the transcript
    // store retains no candidate row for, so there are no turns to count and
    // the conversation is not DECIDED — it leaves the evaluated population
    // rather than being reported as a non-contributor with zero turns.
    case 'no_retained_transcript':
      return 'some conversations retained no transcript to count turns in';
    case null:
    case undefined:
    case '':
      return '';
    default:
      // The code itself, then the one thing that is actually missing. The
      // earlier wording — "a cause this build cannot name (<code>)" — named
      // the code in the same breath as claiming it could not, and it did so
      // on a screen that had already printed the code's meaning in English
      // one line above. The terminal prints the bare code here, so leading
      // with the code is also what makes the two surfaces read alike.
      return `${code} (no description in this build)`;
  }
}

/** The human label for one evidence figure.
 *
 *  A required fallback branch again: the evidence keys are a server-side
 *  vocabulary, and a key from a newer server must render under its own name
 *  rather than vanish from the row.
 *
 *  These strings are the SAME ones `_EVIDENCE_LABELS` renders in
 *  `bin/_cctally_diagnosis.py`, so the terminal and the modal name every
 *  figure alike; `test_the_terminal_and_the_modal_name_every_evidence_field_alike`
 *  is the gate. No label may contain a comma, because the terminal joins the
 *  figures with `, `. */
export function evidenceLabel(name: string): string {
  switch (name) {
    case 'flaggedTurnCount':
      return 'turns that rebuilt their cache';
    case 'affectedConversationCount':
      return 'conversations affected';
    case 'estWastedUsd':
      return 'estimated wasted cost';
    case 'conversationCount':
      return 'qualifying conversations';
    case 'medianHumanTurns':
      return 'median human turns';
    case 'maxContextWindowFraction':
      return 'largest request as a share of its context window';
    case 'identifiedSubagentCount':
      return 'identified subagents';
    case 'largestSubagentShare':
      return 'largest subagent as a share of this class';
    case 'unallocatedUsd':
      return 'unallocated cost';
    default:
      return name;
  }
}

const EVIDENCE_USD = new Set(['estWastedUsd', 'unallocatedUsd']);
const EVIDENCE_SHARE = new Set(['largestSubagentShare',
  'maxContextWindowFraction']);

/** One evidence figure, rendered in the unit the FIGURE carries.
 *
 *  The unit is a property of the name, not of the type: a number called
 *  `estWastedUsd` is dollars and one called `largestSubagentShare` is a
 *  proportion, and rendering either as a bare number makes the reader guess.
 *  Returns `null` when the field is withheld, so the caller renders the cause
 *  rather than a figure — printing nothing there would read as zero. */
export function evidenceValue(
  name: string, field: EvidenceField | undefined,
): string | null {
  if (field == null || field.state !== 'available'
      || typeof field.value !== 'number') {
    return null;
  }
  if (EVIDENCE_USD.has(name)) {
    return `$${field.value.toFixed(2)}`;
  }
  if (EVIDENCE_SHARE.has(name)) {
    return `${(field.value * 100).toFixed(1)}%`;
  }
  return String(field.value);
}
