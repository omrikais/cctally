// Canonical frontend source-state policy.
//
// The fixed ten-card board no longer uses capability gates to decide which
// shells exist. This module therefore owns only the live source-state consumers:
// warning selection for status/panel chrome and Sessions hydration/degradation.

import type { SourceName, SourceWarning } from '../types/envelope';
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
      && (entry.freshness === 'stale' || warning != null)) {
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
