// #294 S5 — the §5.5 capability-gating model (three layers). Pure functions
// only; no store imports.
//
// Layer 1 — the panel gating table (PANEL_GATING): each panel's capability key,
//   per-provider source-data path, and permitted legacy fallback (cache-report
//   only). A panel renders for a provider iff the capability record is
//   supported/derived AND its mapped data path resolves (or its legacy fallback
//   exists). A capability without a resolvable path/fallback hides (D2), which
//   is what keeps Codex `forensics: supported` (hero counters only) and Codex
//   forecast/trend from rendering ghost panels.
// Layer 2 — selection-level gating for `all`: the `all` entry's own capabilities
//   govern only the combined hero tiles and the alerts union; every other panel
//   in All mode renders provider-labeled sections from each provider child
//   (providerSections), evaluated through the same Layer-1 table.
// Layer 3 — availability/hydration precedence: hydrating → skeleton; unavailable
//   → degraded (warning-derived when present); partial/stale → degraded with
//   retained data; ok/empty → normal. Independently, a degraded state with a
//   null last_success_at also reports noSuccessYet.

import type { Envelope, SourceName, SourceWarning } from '../types/envelope';
import type { PanelId } from './panelIds';
import {
  isHydratingEntry,
  resolveSourceView,
  type SourceView,
} from '../store/sourceView';

export type GateMode = 'render' | 'hidden' | 'degraded' | 'skeleton';

export interface PanelGate {
  mode: GateMode;
  // The governing warning for a degraded/unavailable state (entry.warnings[0]),
  // or null when degraded generically (unknown status) or not degraded.
  warning?: SourceWarning | null;
  // §5.5 Layer 3 — a degraded state whose last_success_at is null renders "no
  // successful snapshot yet" in addition to any warning chip.
  noSuccessYet: boolean;
}

export interface ProviderSection {
  source: SourceName;
  data: unknown | null;
  gate: PanelGate;
}

type PathFn = (data: unknown) => unknown | undefined;

export interface PanelGateSpec {
  capability: string;
  // A per-source data path, or `null` when the source's gating row has NO data
  // path for the panel (e.g. Codex publishes no forecast/trend; neither provider
  // has a cache-report path — Claude renders it via `legacyFallback`). A `null`
  // path + no applicable fallback means the source can NEVER render the panel, so
  // it hides UNCONDITIONALLY (ahead of availability/hydration layering) — see
  // `sourceHasPanelPath` / `gateSingleSource`.
  path: Record<SourceName, PathFn | null>;
  legacyFallback?: (env: Envelope) => unknown | undefined;
}

function rec(v: unknown): Record<string, unknown> | undefined {
  return v != null && typeof v === 'object' ? (v as Record<string, unknown>) : undefined;
}

// §5.5 Layer 1 — the panel gating table, verbatim. `current-week` is the hero
// (strip + modal). Forecast/trend map to the `hero` capability but their Codex
// path is intentionally absent (Codex hero publishes no forecast/trend →
// hidden). Blocks map to the `quota` capability. Cache-report has NO source path
// under either provider; Claude renders it only via the top-level `cache_report`
// legacy fallback.
export const PANEL_GATING: Record<PanelId, PanelGateSpec> = {
  'current-week': {
    capability: 'hero',
    path: {
      claude: (d) => rec(d)?.hero,
      codex: (d) => rec(d)?.hero,
    },
  },
  forecast: {
    capability: 'hero',
    path: {
      claude: (d) => rec(rec(d)?.hero)?.forecast,
      // Codex hero publishes no forecast → no path → hidden unconditionally.
      codex: null,
    },
  },
  trend: {
    capability: 'hero',
    path: {
      claude: (d) => rec(rec(d)?.hero)?.trend,
      // Codex hero publishes no trend → no path → hidden unconditionally.
      codex: null,
    },
  },
  daily: {
    capability: 'daily',
    path: {
      claude: (d) => rec(rec(d)?.periods)?.daily,
      codex: (d) => rec(rec(d)?.periods)?.daily,
    },
  },
  weekly: {
    capability: 'weekly',
    path: {
      claude: (d) => rec(rec(d)?.periods)?.weekly,
      codex: (d) => rec(rec(d)?.periods)?.weekly,
    },
  },
  monthly: {
    capability: 'monthly',
    path: {
      claude: (d) => rec(rec(d)?.periods)?.monthly,
      codex: (d) => rec(rec(d)?.periods)?.monthly,
    },
  },
  sessions: {
    capability: 'sessions',
    path: {
      claude: (d) => rec(d)?.sessions,
      codex: (d) => rec(d)?.sessions,
    },
  },
  projects: {
    capability: 'projects',
    path: {
      claude: (d) => rec(d)?.projects,
      codex: (d) => rec(d)?.projects,
    },
  },
  blocks: {
    capability: 'quota',
    path: {
      claude: (d) => rec(rec(d)?.quota)?.blocks,
      codex: (d) => rec(d)?.quota,
    },
  },
  'cache-report': {
    capability: 'forensics',
    path: {
      // Neither provider publishes a cache-report path; Claude renders it ONLY
      // via the top-level `cache_report` legacy fallback (below). Codex has no
      // path and no fallback → hidden unconditionally.
      claude: null,
      codex: null,
    },
    legacyFallback: (env) => env.cache_report ?? undefined,
  },
  alerts: {
    capability: 'alerts',
    path: {
      claude: (d) => rec(d)?.alerts,
      codex: (d) => rec(d)?.alerts,
    },
  },
};

// The two selection-level surfaces the `all` entry's OWN capabilities govern
// (§5.5 Layer 2): the combined hero and the alerts union. Every other panel in
// All mode is a provider-child (providerSections).
const ALL_COMBINED_PANELS: ReadonlySet<PanelId> = new Set<PanelId>(['current-week', 'alerts']);

function firstWarning(warnings: SourceWarning[] | undefined): SourceWarning | null {
  return warnings != null && warnings.length > 0 ? warnings[0] : null;
}

// §5.5 — whether the panel's gating row gives THIS source a way to produce data:
// a non-null mapped path, or (Claude only) a legacy fallback with an envelope to
// read from. A source with neither can NEVER render the panel, so it hides
// unconditionally — independent of availability, freshness, or hydration. This
// is what keeps Codex forecast/trend/cache-report hidden even when the Codex
// entry is wholly `unavailable` (the availability layer would otherwise
// `degrade` them into a visible slot and mount a Claude panel under the Codex
// label).
function sourceHasPanelPath(
  spec: PanelGateSpec,
  source: SourceName,
  hasEnv: boolean,
): boolean {
  if (spec.path[source] != null) return true;
  return source === 'claude' && spec.legacyFallback != null && hasEnv;
}

// Resolve the mapped data path (or the Claude legacy fallback) for a
// single-source view. Returns `undefined` when the source does not publish the
// path AND no fallback applies — the distinction between `undefined` (not
// published → hide) and a present `null` (published-but-empty → honest empty).
export function resolvePanelData(view: SourceView, panel: PanelId): unknown | undefined {
  const spec = PANEL_GATING[panel];
  if (view.selection === 'all') return undefined; // use providerSections instead
  const source = view.selection;
  const entry = view.entry;
  const pathFn = spec.path[source];
  if (pathFn != null && entry?.data != null) {
    const resolved = pathFn(entry.data);
    if (resolved !== undefined) return resolved;
  }
  if (source === 'claude' && spec.legacyFallback != null && view.env != null) {
    const fb = spec.legacyFallback(view.env);
    if (fb !== undefined) return fb;
  }
  return undefined;
}

// Gate a panel for a single physical source (claude | codex).
function gateSingleSource(view: SourceView, panel: PanelId, source: SourceName): PanelGate {
  const spec = PANEL_GATING[panel];
  const entry = view.entry;

  // Pre-S4 / no-entry defensive path. Claude with a legacy envelope is a
  // legacy-compatible view: render (the cache-report fallback resolves too);
  // any other no-entry case is hidden unless hydrating.
  if (entry == null) {
    if (source === 'claude' && view.env != null && !view.hydrating) {
      // cache-report renders from its legacy fallback. When that fallback has
      // not (re)built yet — cold start or a transient sub-build failure — the
      // panel keeps a cold-start SKELETON placeholder rather than un-mounting
      // (which would reflow the §6.11 digit-shortcut map). See the same
      // resilience in the main-path `!hasData` branch below.
      if (spec.legacyFallback != null) {
        return {
          mode: spec.legacyFallback(view.env) !== undefined ? 'render' : 'skeleton',
          noSuccessYet: false,
        };
      }
      return { mode: 'render', noSuccessYet: false };
    }
    return { mode: view.hydrating ? 'skeleton' : 'hidden', noSuccessYet: false };
  }

  // Layer 1 (precedence) — a panel whose gating row gives THIS source no data
  // path AND no applicable legacy fallback can NEVER render for the source, so
  // `hidden` wins UNCONDITIONALLY here, ahead of every availability/hydration
  // layer below. Availability/freshness only modulate panels the source can
  // actually have; without this a wholly-`unavailable` Codex entry fell into the
  // availability→`degraded` branch and left forecast/trend/cache-report mounted
  // (leaking the Claude panel under the Codex label). See §5.5.
  if (!sourceHasPanelPath(spec, source, view.env != null)) {
    return { mode: 'hidden', noSuccessYet: false };
  }

  // Layer 3 — hydration precedence.
  if (isHydratingEntry(entry)) return { mode: 'skeleton', noSuccessYet: false };

  const cap = entry.capabilities?.[spec.capability];
  const status = cap?.status;
  const noSuccessYet = entry.last_success_at == null;

  // Layer 1 — a capability the source explicitly does not offer hides.
  if (status === 'deferred' || status === 'not_applicable') {
    return { mode: 'hidden', noSuccessYet: false };
  }

  // Layer 3 — an entry-level unavailable renders an explicit unavailable state.
  if (entry.availability === 'unavailable') {
    return { mode: 'degraded', warning: firstWarning(entry.warnings), noSuccessYet };
  }

  // Resolve the mapped data path (or Claude legacy fallback). `pathFn` may be
  // null only for a source that also HAS a legacy fallback (a null-path source
  // without one was already hidden above), so a null `pathFn` here just skips to
  // the fallback.
  const pathFn = spec.path[source];
  const resolved =
    pathFn != null && entry.data != null ? pathFn(entry.data) : undefined;
  const fallback =
    source === 'claude' && spec.legacyFallback != null && view.env != null
      ? spec.legacyFallback(view.env)
      : undefined;
  const hasData = resolved !== undefined || fallback !== undefined;

  // Layer 1 — a runtime-unavailable capability renders an explicit degraded
  // state (D2), distinct from the entry-level availability above.
  if (status === 'unavailable') {
    return { mode: 'degraded', warning: firstWarning(entry.warnings), noSuccessYet };
  }

  // Layer 1 — supported/derived (or unknown) with no resolvable path/fallback
  // hides (ghost-panel prevention: Codex forensics, Codex forecast/trend).
  if (!hasData) {
    // Transient-null resilience for the Claude cache-report legacy fallback: an
    // otherwise-healthy Claude entry (not unavailable, not deferred/
    // not_applicable, not capability-unavailable — all handled above) whose
    // top-level `cache_report` object hasn't been (re)built yet keeps a
    // cold-start SKELETON placeholder instead of un-mounting the panel and
    // reflowing the §6.11 digit-shortcut map. Only Claude + a legacy-fallback
    // panel (cache-report) reaches here; Codex forecast/trend/forensics still
    // hide (source !== 'claude' or no legacyFallback).
    if (source === 'claude' && spec.legacyFallback != null) {
      return { mode: 'skeleton', noSuccessYet: false };
    }
    return { mode: 'hidden', noSuccessYet: false };
  }

  // Layer 3 — partial/stale renders retained data with a degraded chip.
  if (entry.availability === 'partial') {
    return { mode: 'degraded', warning: firstWarning(entry.warnings), noSuccessYet };
  }

  // Unknown capability status but data present → degrade generically (never
  // throw, never render as fully-supported). A recognized supported/derived
  // status renders normally.
  if (status !== 'supported' && status !== 'derived') {
    return { mode: 'degraded', warning: null, noSuccessYet };
  }

  return { mode: 'render', noSuccessYet: false };
}

// Provider-labeled sections for an `all`-mode provider-child panel (§5.5 Layer
// 2). Each provider is gated through the SAME single-source logic (so Codex
// forecast/trend/forensics still hide, and the Claude cache-report legacy
// fallback still applies). Returns [] when the selection is not `all`.
export function providerSections(view: SourceView, panel: PanelId): ProviderSection[] {
  if (view.selection !== 'all') return [];
  const sources: SourceName[] = ['claude', 'codex'];
  return sources.map((source) => {
    const provView = resolveSourceView(view.env, source);
    const gate = gateSingleSource(provView, panel, source);
    const data = resolvePanelData(provView, panel);
    return { source, data: data === undefined ? null : data, gate };
  });
}

// Gate the combined selection-level surfaces (`all` entry own capabilities).
// Both surfaces (combined hero + alerts union) gate identically on the `all`
// entry, so the specific panel is irrelevant here.
function gateAllCombined(view: SourceView): PanelGate {
  const entry = view.entry;
  if (entry == null) return { mode: view.hydrating ? 'skeleton' : 'hidden', noSuccessYet: false };
  if (isHydratingEntry(entry)) return { mode: 'skeleton', noSuccessYet: false };
  const noSuccessYet = entry.last_success_at == null;
  if (entry.availability === 'unavailable') {
    return { mode: 'degraded', warning: firstWarning(entry.warnings), noSuccessYet };
  }
  if (entry.availability === 'partial') {
    return { mode: 'degraded', warning: firstWarning(entry.warnings), noSuccessYet };
  }
  // The combined hero always renders (even a null `combined` shows an explicit
  // "combined unavailable" state — a render decision inside the hero, not a
  // hidden gate). The alerts union likewise always renders.
  return { mode: 'render', noSuccessYet: false };
}

// The single gating authority for a panel under the active view. For `all`, the
// combined surfaces gate on the `all` entry; other panels aggregate their
// provider sections so `mode !== 'hidden'` is a faithful visibility signal.
export function gatePanel(view: SourceView, panel: PanelId): PanelGate {
  if (view.selection !== 'all') return gateSingleSource(view, panel, view.selection);
  if (ALL_COMBINED_PANELS.has(panel)) return gateAllCombined(view);
  // Provider-child panel: aggregate the sections. Visible when any provider
  // renders/degrades; skeleton when any is still hydrating and none renders;
  // hidden only when every provider is hidden.
  const sections = providerSections(view, panel);
  const anyVisible = sections.some((s) => s.gate.mode === 'render' || s.gate.mode === 'degraded');
  if (anyVisible) return { mode: 'render', noSuccessYet: false };
  const anySkeleton = sections.some((s) => s.gate.mode === 'skeleton');
  return { mode: anySkeleton ? 'skeleton' : 'hidden', noSuccessYet: false };
}

// True when the panel occupies a visible slot for the active view (drives the
// derived visible-panel order, digit shortcuts, Help listing, DnD, share).
export function isPanelVisible(view: SourceView, panel: PanelId): boolean {
  return gatePanel(view, panel).mode !== 'hidden';
}
