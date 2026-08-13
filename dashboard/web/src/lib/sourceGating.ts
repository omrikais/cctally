// Canonical frontend source-state policy.
//
// The fixed ten-card board no longer uses capability gates to decide which
// shells exist. This module therefore owns only the live source-state consumers:
// warning selection for status/panel chrome and Sessions hydration/degradation.

import type {
  AllCombinedLeg,
  AllCombinedQualification,
  AllSourceData,
  SourceEntry,
  SourceFreshness,
  SourceFreshnessDomain,
  SourceName,
  SourceWarning,
} from '../types/envelope';
import {
  isHydratingEntry,
  resolveSourceView,
  type SourceView,
} from '../store/sourceView';

export type GateMode = 'render' | 'hidden' | 'degraded' | 'skeleton';

export interface PanelGate {
  mode: GateMode;
  warning?: SourceWarning | null;
  noSuccessYet: boolean;
}

const CAPABILITY_WARNING_DOMAINS = new Set([
  'hero',
  'daily',
  'weekly',
  'monthly',
  'sessions',
  'projects',
  'quota',
  'budget',
  'forensics',
  'alerts',
]);

function firstWarning(warnings: SourceWarning[] | undefined): SourceWarning | null {
  return warnings != null && warnings.length > 0 ? warnings[0] : null;
}

function isSourceWideWarning(warning: SourceWarning): boolean {
  const domain = warning.domain;
  return domain == null || domain === 'ingest' || domain === 'read_model'
    || !CAPABILITY_WARNING_DOMAINS.has(domain);
}

// A physical-source status chip must surface a source-wide warning first. When
// no such warning exists, it may summarize the first capability warning.
export function warningForSource(
  warnings: SourceWarning[] | undefined,
): SourceWarning | null {
  if (warnings == null) return null;
  return warnings.find(isSourceWideWarning) ?? firstWarning(warnings);
}

// Panel/domain chrome uses one precedence rule: a source-wide warning wins;
// otherwise only a warning for the requested known domain applies. Unrelated
// known capability warnings never leak across panels.
export function warningForDomain(
  warnings: SourceWarning[] | undefined,
  domain: string,
): SourceWarning | null {
  if (warnings == null) return null;
  return warnings.find(isSourceWideWarning)
    ?? warnings.find((warning) => warning.domain === domain)
    ?? null;
}

function rec(value: unknown): Record<string, unknown> | undefined {
  return value != null && typeof value === 'object'
    ? value as Record<string, unknown>
    : undefined;
}

// The one frontend compatibility seam for #396's additive map. Do not scatter
// `?.[domain] ?? freshness` across consumers: older envelopes deterministically
// inherit provider freshness for every domain.
export function sourceDomainFreshness(
  entry: SourceEntry<unknown>,
  domain: SourceFreshnessDomain,
): SourceFreshness {
  const domainValue = entry.domain_freshness?.[domain];
  return domainValue === 'fresh' || domainValue === 'stale'
    ? domainValue
    : entry.freshness;
}

// ---- The combined figure's single authoritative predicate (#556 S1 §4.4) ---
//
// `HeroStrip`, `AllCurrentWeekModal` and `SourceStatusChip` derive combined
// disclosure from THIS and from nothing else. In particular none of them may
// read `sourceDomainFreshness(entry, 'hero')` or `entry.freshness` for that
// purpose: the first is an aggregate over both providers' accounting
// resolvability and the second is a source-wide axis, and pairing either with a
// published number is how "Combined totals are unavailable" came to sit beside
// a figure that was on screen (#556 B3).
//
// It also does NOT fall back to warning-tuple order. All flattens All-local,
// Claude and Codex warnings into one tuple with no provenance field
// (`bin/_lib_dashboard_sources.py`), so warning order cannot carry which
// provider a reason belongs to. The typed `combined_unavailable` does.

export interface CombinedValue {
  costUsd: number;
  totalTokens: number;
}

export interface CombinedPresentation {
  // The figure to print, or `null` when it is withheld.
  value: CombinedValue | null;
  legs: { claude: AllCombinedLeg; codex: AllCombinedLeg } | null;
  // The providers that actually contribute to the sum, in display order. A
  // `current` leg contributes and is named even when it cannot resolve its own
  // period; only an `empty` leg is excluded.
  contributors: SourceName[];
  // Notes qualifying a PUBLISHED figure. Empty unless the figure exists.
  qualifications: AllCombinedQualification[];
  // The named reason a figure is withheld. Non-null only when `value` is null.
  unavailable: { code: string; message: string } | null;
}

const COMBINED_PROVIDERS: readonly SourceName[] = ['claude', 'codex'];

export function combinedPresentation(
  entry: SourceEntry<AllSourceData> | null | undefined,
): CombinedPresentation {
  const data = entry?.data ?? null;
  const combined = data?.combined ?? null;
  if (combined == null) {
    const unavailable = data?.combined_unavailable;
    return {
      value: null,
      legs: null,
      contributors: [],
      qualifications: [],
      // A legacy (pre-v5) envelope carries no typed diagnostic, so the
      // hero-domain warning is the only reason available. That fallback exists
      // for those envelopes and for nothing else — a v5 server always emits
      // `combined_unavailable` beside a null `combined`.
      unavailable: unavailable != null
        ? { code: unavailable.code, message: unavailable.message }
        : legacyCombinedReason(entry),
    };
  }
  const legs = combined.legs;
  return {
    value: { costUsd: combined.cost_usd, totalTokens: combined.total_tokens },
    legs,
    contributors: COMBINED_PROVIDERS.filter(
      (provider) => legs?.[provider]?.state === 'current',
    ),
    qualifications: combined.qualifications ?? [],
    unavailable: null,
  };
}

function legacyCombinedReason(
  entry: SourceEntry<AllSourceData> | null | undefined,
): { code: string; message: string } | null {
  if (entry == null) return null;
  const warning = warningForDomain(entry.warnings, 'hero');
  return warning == null
    ? null
    : { code: warning.code, message: warning.message };
}

// The heading names its actual contributors, so a reader is never told a
// provider is in a total it is not in.
export function combinedHeading(contributors: SourceName[]): string {
  if (contributors.length === 2) return 'COMBINED · CURRENT CYCLES';
  if (contributors.length === 1) {
    return contributors[0] === 'claude'
      ? 'CLAUDE · CURRENT CYCLE'
      : 'CODEX · CURRENT CYCLE';
  }
  return 'CURRENT CYCLES · NO DATA';
}

function gatePhysicalSessions(view: SourceView, source: SourceName): PanelGate {
  const entry = view.entry;

  // A pre-source-bundle Claude envelope remains the supported legacy path.
  // Other missing entries are either still hydrating or genuinely absent.
  if (entry == null) {
    if (source === 'claude' && view.env != null && !view.hydrating) {
      return { mode: 'render', noSuccessYet: false };
    }
    return { mode: view.hydrating ? 'skeleton' : 'hidden', noSuccessYet: false };
  }

  if (isHydratingEntry(entry)) {
    return { mode: 'skeleton', noSuccessYet: false };
  }

  const status = entry.capabilities?.sessions?.status;
  const noSuccessYet = entry.last_success_at == null;
  const warning = warningForDomain(entry.warnings, 'sessions');

  if (status === 'deferred' || status === 'not_applicable') {
    return { mode: 'hidden', noSuccessYet: false };
  }
  if (entry.availability === 'unavailable' || status === 'unavailable') {
    return { mode: 'degraded', warning, noSuccessYet };
  }

  const hasSessions = rec(entry.data)?.sessions !== undefined;
  if (!hasSessions) {
    return { mode: 'hidden', noSuccessYet: false };
  }

  if (entry.availability === 'partial'
      && (sourceDomainFreshness(entry, 'sessions') === 'stale' || warning != null)) {
    return { mode: 'degraded', warning, noSuccessYet };
  }
  if (status !== 'supported' && status !== 'derived') {
    return { mode: 'degraded', warning: null, noSuccessYet };
  }
  return { mode: 'render', noSuccessYet: false };
}

// Sessions is the sole production consumer that still needs a gate: it uses
// the result for loading skeletons and honest degraded states, never board
// visibility or card-order decisions.
export function gateSessions(view: SourceView): PanelGate {
  if (view.selection !== 'all') {
    return gatePhysicalSessions(view, view.selection);
  }

  const children = (['claude', 'codex'] as const).map((source) =>
    gatePhysicalSessions(resolveSourceView(view.env, source), source));
  const visible = children.some((gate) =>
    gate.mode === 'render' || gate.mode === 'degraded');
  if (visible) return { mode: 'render', noSuccessYet: false };
  const hydrating = children.some((gate) => gate.mode === 'skeleton');
  return { mode: hydrating ? 'skeleton' : 'hidden', noSuccessYet: false };
}
