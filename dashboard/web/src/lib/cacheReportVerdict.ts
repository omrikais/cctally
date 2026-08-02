// The single definition of every Cache Report verdict derivation.
//
// Before #443 S1 the insufficient-baseline predicate was re-derived in
// CacheReportPanel, CacheReportModal and CacheReportSpotlight, kept in sync
// only by comments that had themselves drifted to wrong line numbers. Every
// surface now calls into here, so a disagreement is impossible rather than
// merely discouraged.
import type {
  CacheAnomalyReason, CacheReportEnvelope,
} from '../types/envelope';
import { CACHE_REPORT_MIN_BASELINE_DAYS } from './cache-report-constants';

// Mirrors CACHE_ANOMALY_PREDICATES in bin/_lib_cache_report.py. Enforcing the
// correspondence is #443 F19, owned by session S3.
export const CACHE_ANOMALY_PREDICATES: readonly CacheAnomalyReason[] = [
  'net_negative', 'cache_drop',
];

export type CacheRowVerdictState =
  | 'anomalous' | 'clean' | 'partial' | 'unevaluated';

// Satisfied structurally by both CacheReportDailyRow and the today spotlight,
// so one function serves rows and today alike.
export interface CacheVerdictInput {
  anomaly_triggered: boolean;
  anomaly_reasons: CacheAnomalyReason[];
  anomaly_unevaluated?: CacheAnomalyReason[];
  observed?: boolean;
}

export interface CacheRowVerdict {
  state: CacheRowVerdictState;
  triggered: boolean;
  unevaluated: CacheAnomalyReason[];
  observed: boolean;
}

/**
 * #443 S2 — `predicates` is the set the PROVIDER can evaluate at all.
 *
 * "Every predicate unevaluated" is a per-provider question. Codex can only
 * ever evaluate cache_drop, so a Codex row carrying it alone is fully
 * unevaluated; resolving it against the Claude pair calls it `partial`, which
 * is a claim that some other predicate ran — and on Codex no other predicate
 * exists to run. Not applicable is not unevaluated (spec §2).
 *
 * The default is the Claude pair, so a pre-S2 envelope — which publishes no
 * `anomaly_predicates` — resolves exactly as it does today. That default is
 * pinned against the Python kernel by tests/test_cache_report_constant_parity.py.
 */
export function cacheRowVerdict(
  input: CacheVerdictInput,
  predicates: readonly CacheAnomalyReason[] = CACHE_ANOMALY_PREDICATES,
): CacheRowVerdict {
  const unevaluated = input.anomaly_unevaluated ?? [];
  // Absent means observed — an older envelope must render as it does today.
  const observed = input.observed !== false;
  // Triggered wins first. A thin-baseline net_negative is a real verdict even
  // though cache_drop beside it could not be evaluated, and the epic requires
  // the unevaluated presentation never to swallow it.
  let state: CacheRowVerdictState;
  if (input.anomaly_triggered) {
    state = 'anomalous';
  } else if (unevaluated.length === 0) {
    state = 'clean';
  } else if (unevaluated.length >= predicates.length) {
    state = 'unevaluated';
  } else {
    state = 'partial';
  }
  return { state, triggered: input.anomaly_triggered, unevaluated, observed };
}

export function cacheRowFlagGlyph(state: CacheRowVerdictState): string {
  if (state === 'anomalous') return '⚠';
  if (state === 'clean') return '✓';
  return '·';
}

// Lives beside the glyph rather than in the modal: a new state would otherwise
// need two edits in two files, and only the glyph half is watched by the drift
// scan. The class is returned separately from the glyph because the desktop
// table hangs it on the <td> and the mobile card on a <span> — different
// elements, different CSS selectors, one derivation.
export function cacheRowFlagClass(state: CacheRowVerdictState): string {
  if (state === 'anomalous') return 'flag-warn';
  if (state === 'clean') return 'flag-ok';
  return 'flag-none';
}

// `reasonLabel` translates a raw predicate name into the provider's wording.
// It defaults to identity, which is EXACTLY Claude's mapping, so the Claude
// label is byte-identical whether the caller passes one or not.
//
// #443 S2 QA P2: this label is rendered into both `title` and `aria-label` on
// the daily-row flag, so leaving it untranslated put the raw `cache_drop`
// identifier on a Codex surface — where the contract says "reuse drop" — and
// put it somewhere only a screen-reader user or a hovering mouse would find.
// The vocabulary map already had `reasonLabel` for precisely this; it just
// had one call site.
export function cacheRowFlagLabel(
  state: CacheRowVerdictState,
  unevaluated: CacheAnomalyReason[],
  observed = true,
  reasonLabel: (predicate: CacheAnomalyReason) => string = (p) => p,
): string {
  if (state === 'anomalous') return 'anomaly';
  if (state === 'clean') return 'evaluated, no anomaly';
  if (!observed) return 'no activity — nothing measured to evaluate';
  const names = unevaluated.map(reasonLabel).join(' and ');
  return `not evaluated — ${names} could not be evaluated for this day`;
}

export interface CacheReportVerdict {
  insufficient: boolean;
  chromeAmber: boolean;
  todayObserved: boolean;
  today: CacheRowVerdict;
}

/** The report's own applicable predicate set; absent means the Claude pair. */
export function cacheReportPredicates(
  cr: CacheReportEnvelope,
): readonly CacheAnomalyReason[] {
  return cr.anomaly_predicates ?? CACHE_ANOMALY_PREDICATES;
}

export function cacheReportVerdict(cr: CacheReportEnvelope): CacheReportVerdict {
  const insufficient =
    cr.today.baseline_daily_row_count < CACHE_REPORT_MIN_BASELINE_DAYS;
  // During the first 1-4 captured days a net_negative today already sets
  // anomaly_triggered, but the watchdog reads as neutral "Building baseline"
  // until the floor exists. This gate is pre-existing deliberate behavior.
  const chromeAmber = cr.today.anomaly_triggered && !insufficient;
  // Forwarding the report's set is what makes S2 land: deriving today's
  // verdict here against the module default would leave an ordinary Codex
  // today reading `partial` no matter what cacheRowVerdict learned.
  const today = cacheRowVerdict(cr.today, cacheReportPredicates(cr));
  return { insufficient, chromeAmber, todayObserved: today.observed, today };
}
