// The Codex cache vocabulary contract, client side (#443 S2 §2, §4.3, §4.4).
//
// Two things live here because they answer the same question — "what does this
// provider call its cache figure?" — from opposite ends:
//
//   cachePercent    reads the VALUE, preferring Codex's authoritative
//                   `cached_input_percent` and tolerating its absence.
//   cacheVocabulary reads the LABEL for that value and everything derived
//                   from it.
//
// The governing rule is stronger than the enumeration below: no Codex surface
// uses Claude cache vocabulary. A site the map does not yet cover is still in
// scope — add the entry here rather than writing a per-site literal, which is
// how the pre-S2 UI ended up labelling OpenAI token reuse a "cache hit".
//
// The CLI is the reference implementation, not this file: `cachedInputPercent`,
// the "Cached Input" column and the "Token Reuse Report" title already ship in
// bin/_lib_source_analytics.py, and the wire contract in
// bin/_lib_cache_report_wire.py is what publishes the field these names read.
import type { CacheAnomalyReason } from '../types/envelope';
import { fmt } from './fmt';

/** Anything row-shaped enough to carry a cache percentage. */
export interface CachePercentRow {
  cached_input_percent?: number | null;
  cache_hit_percent?: number | null;
}

/**
 * The percentage a row publishes, or null when it publishes none.
 *
 * `??` and not `||`: a measured zero-reuse day is a real observation and must
 * not collapse into the missing case. Null is returned only when BOTH keys are
 * absent — the shape a future server emits once the transitional
 * `cache_hit_percent` alias is dropped, and the shape that produced `NaN`
 * geometry in the sparkline before this accessor existed.
 */
export function cachePercent(row: CachePercentRow): number | null {
  return row.cached_input_percent ?? row.cache_hit_percent ?? null;
}

/**
 * The same percentage as display text, or null when the row publishes none.
 *
 * One definition rather than `cachePercent(...) === null ? … : …` at every
 * textual site: a null percentage is not a zero and must never render as
 * `0%` or `NaN%`. `fmt.pctFloor` stays a FLOOR, per the epic's preserve list.
 */
export function cachePercentText(row: CachePercentRow): string | null {
  const pct = cachePercent(row);
  return pct === null ? null : `${fmt.pctFloor(pct)}%`;
}

/** Fields the wire may mark structurally absent for a provider. */
export type CacheNotApplicableField = 'wasted_usd' | 'fourteen_day_efficiency_ratio';

/** Anything carrying the wire's optional not-applicable map. */
export interface CacheNotApplicableCarrier {
  not_applicable?: Record<string, string> | null;
}

/**
 * The provider's own reason a figure is not applicable, or null.
 *
 * The map is the authoritative signal, NOT the value: Codex keeps publishing
 * `wasted_usd` and `fourteen_day_efficiency_ratio` as numbers through the
 * transition release (a pre-S2 tab calls `.toFixed` on the first and
 * `Math.round` on the second), so a client that inferred applicability from
 * nullness would read a structural zero as a measurement. The copy comes from
 * the wire so the reason cannot drift from the provider that owns it.
 */
export function notApplicableReason(
  carrier: CacheNotApplicableCarrier,
  field: CacheNotApplicableField,
): string | null {
  return carrier.not_applicable?.[field] ?? null;
}

export interface CacheVocabulary {
  /** Inline stat / KPI key: "Cache hit" vs "Cached input". */
  percentLabel: string;
  /**
   * The same name mid-sentence, where a capital would be wrong.
   *
   * A separate entry rather than `percentLabel.toLowerCase()` at four call
   * sites: the transformation is a property of the copy, not of the site, and
   * a vocabulary whose inline form is not simply the lowercased label would
   * silently render wrong everywhere at once.
   */
  percentLabelInline: string;
  /** Daily-table column header, desktop and mobile card alike. */
  percentColumnHeader: string;
  /** Timeline section heading prefix, without the day count. */
  timelineHeading: string;
  /** Accessible name prefix for the sparkline. */
  sparklineLabel: string;
  /** Net-bars section heading. */
  netBarsHeading: string;
  /** Net-bars meta caption. */
  netBarsCaption: string;
  /** Opening phrase of the counterfactual callout. */
  counterfactualLead: string;
  /** Name of the efficiency figure in that callout. */
  efficiencyLabel: string;
  /** One anomaly predicate as user copy. */
  reasonLabel(predicate: CacheAnomalyReason): string;
  /** The legend printed after the reasons list, given the live threshold. */
  thresholdLegend(thresholdPp: number): string;
}

const CLAUDE: CacheVocabulary = Object.freeze({
  percentLabel: 'Cache hit',
  percentLabelInline: 'cache hit',
  percentColumnHeader: 'Cache %',
  timelineHeading: 'Cache hit %',
  sparklineLabel: 'Cache hit % timeline',
  netBarsHeading: 'Net $ per day · saved (green) − wasted (red)',
  netBarsCaption: 'positive bars = caching helped',
  counterfactualLead: "Without caching, you'd have paid",
  efficiencyLabel: 'cache efficiency',
  // Claude has printed the raw predicate name since the first release and the
  // epic's preserve list keeps it: renaming it here would move Claude copy for
  // a session whose Claude-side safety property is that nothing moves.
  reasonLabel: (predicate: CacheAnomalyReason) => predicate,
  thresholdLegend: (thresholdPp: number) => `${thresholdPp}pp drop, net < 0`,
});

const CODEX: CacheVocabulary = Object.freeze({
  percentLabel: 'Cached input',
  percentLabelInline: 'cached input',
  percentColumnHeader: 'Reuse %',
  timelineHeading: 'Cached input %',
  sparklineLabel: 'Cached input % timeline',
  // No red segment is promised: OpenAI charges no cache-write premium, so a
  // Codex day has nothing wasted to stack on top of the green bar.
  netBarsHeading: 'Net $ per day · reuse savings (green)',
  netBarsCaption: 'bars = input reuse savings',
  counterfactualLead: "Without input reuse, you'd have paid",
  efficiencyLabel: 'reuse efficiency',
  reasonLabel: (predicate: CacheAnomalyReason) =>
    predicate === 'cache_drop' ? 'reuse drop' : predicate,
  // `net < 0` describes a predicate that is not applicable to Codex, so the
  // legend names only the one threshold that governs there.
  thresholdLegend: (thresholdPp: number) => `${thresholdPp}pp reuse drop`,
});

/**
 * The vocabulary one source renders with.
 *
 * `all` resolves to the Claude/neutral copy and never appears in practice: the
 * all-mode composition renders one section per provider and passes each
 * section's OWN source, so the merged selection has no single vocabulary to
 * pick. Resolving it keeps the function total rather than leaving a caller to
 * invent a narrowing at every site.
 */
export function cacheVocabulary(source: string): CacheVocabulary {
  return source === 'codex' ? CODEX : CLAUDE;
}
