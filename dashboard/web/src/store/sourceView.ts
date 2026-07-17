// #294 S5 — the source-view resolution seam (§5.2).
//
// One pure function resolves the active `SourceDashboardState` out of the
// already-delivered S4 `sources` bundle. Panels consume THIS instead of reaching
// into the legacy top-level envelope, with the single exception of the
// explicitly enumerated legacy-fallback fields (§5.2): the top-level
// `alerts_settings` Codex-only flags and the top-level `cache_report` object
// backing the Claude cache-report panel. That list is the named constant below
// with a test asserting its exact contents.
//
// No store imports here — types only. Switching is a pure client re-selection;
// the store never reconciles the choice against later envelopes (§5.1).

import type {
  DashboardSelection,
  Envelope,
  SourceEntry,
} from '../types/envelope';

// §5.2 — the ONLY legacy top-level fields a source entry intentionally omits, so
// the seam falls back to the legacy envelope for them. Exact-contents asserted.
export const CLAUDE_LEGACY_FALLBACK_FIELDS = ['alerts_settings', 'cache_report'] as const;

export interface SourceView {
  selection: DashboardSelection;
  // The active source entry, or null when the envelope pre-dates S4 (no
  // `sources` bundle) or is absent entirely (pre-first-tick).
  entry: SourceEntry<unknown> | null;
  // §5.2 bootstrap/hydrating state — render the existing loading skeletons.
  hydrating: boolean;
  // The legacy envelope, retained for the enumerated fallback fields ONLY.
  env: Envelope | null;
}

// §5.2 hydration detection: an entry whose `capabilities` is `{}` with
// `data: null`, empty `warnings`, and null `last_success_at`. Note this keys off
// the ENTRY SHAPE, not `availability` (the server publishes the hydrating seed
// as `partial`). A null entry is NOT itself hydrating — the caller decides
// (pre-S4 Claude is a legacy-compatible view, not a skeleton).
export function isHydratingEntry(entry: SourceEntry<unknown> | null): boolean {
  if (entry == null) return false;
  return (
    entry.data == null &&
    entry.last_success_at == null &&
    Array.isArray(entry.warnings) &&
    entry.warnings.length === 0 &&
    entry.capabilities != null &&
    Object.keys(entry.capabilities).length === 0
  );
}

// Resolve the active source view over an already-delivered envelope.
//
//  - env == null (pre-first-tick): every selection is hydrating (skeletons).
//  - env present, no `sources` map (pre-S4 server / bare fixture):
//      * Claude → a legacy-compatible view (entry: null, hydrating: false) —
//        panels + the cache-report legacy fallback read the top-level envelope.
//      * Codex / All → hydrating-like absence (entry: null, hydrating: true) —
//        there is no provider data to show, but nothing crashes.
//  - env present WITH `sources`: entry = env.sources[active] (the FLAT map);
//    hydrating is detected from the entry shape.
export function resolveSourceView(
  env: Envelope | null,
  active: DashboardSelection,
): SourceView {
  if (env == null) {
    return { selection: active, entry: null, hydrating: true, env: null };
  }
  const sources = env.sources;
  if (sources == null) {
    // Pre-S4 envelope (no `sources` map): Claude reads legacy fields directly;
    // Codex/All have no provider data yet.
    return {
      selection: active,
      entry: null,
      hydrating: active !== 'claude',
      env,
    };
  }
  const entry = (sources[active] ?? null) as SourceEntry<unknown> | null;
  return {
    selection: active,
    entry,
    hydrating: isHydratingEntry(entry),
    env,
  };
}
