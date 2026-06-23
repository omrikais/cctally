// Shared matchMedia stub for tests that exercise useIsMobile-driven
// branches. Codifies the inline `vi.stubGlobal('matchMedia', …)`
// recipe already used in ComposerModal.test.tsx, ShareManualSmoke
// .test.tsx, and a11y.test.tsx. Call inside `beforeEach`.
//
// IMPORTANT: `vi.stubGlobal` is undone ONLY by `vi.unstubAllGlobals()` —
// NOT by `vi.restoreAllMocks()`. The project pins `test.unstubGlobals: true`
// in vite.config.ts so this stub is auto-cleared between tests; a file that
// relied on restoreAllMocks alone would leak this matchMedia stub into every
// later test, leaving `useIsMobile()` stuck and flaking layout/outline
// assertions under test reordering (#221).
import { vi } from 'vitest';

export function stubMobileMedia(matches: boolean): void {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches,
    media: q,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

// #228 S3 F1 — a per-query matchMedia stub. `stubMobileMedia` returns the SAME
// `matches` for every query, which can't model the 641–1100 tablet band where
// useIsMobile (max-width:640) is FALSE while useIsWide (min-width:1101) is also
// false. `resolveMatch(query)` is consulted per call so a single render can see
// both hooks resolve distinctly. Like stubMobileMedia it is auto-cleared by the
// project's `test.unstubGlobals: true` (vite.config.ts) — never relied on
// restoreAllMocks. The listener callbacks are captured so a test can fire a
// viewport change by re-stubbing then dispatching the captured `change` handler.
type MqlListener = (e: { matches: boolean }) => void;

export interface ResponsiveMediaController {
  /** Re-resolve all live MediaQueryLists and fire their change listeners. */
  set(resolveMatch: (query: string) => boolean): void;
}

export function stubResponsiveMedia(
  resolveMatch: (query: string) => boolean,
): ResponsiveMediaController {
  // Track every MQL we hand out so `set` can re-resolve + notify them (mirrors a
  // real viewport resize firing `change` on every registered MediaQueryList).
  const live: { query: string; listeners: Set<MqlListener>; matches: boolean }[] = [];
  let resolver = resolveMatch;
  vi.stubGlobal('matchMedia', (q: string) => {
    const entry = { query: q, listeners: new Set<MqlListener>(), matches: resolver(q) };
    live.push(entry);
    return {
      get matches() {
        return entry.matches;
      },
      media: q,
      onchange: null,
      addEventListener: (_type: string, cb: MqlListener) => entry.listeners.add(cb),
      removeEventListener: (_type: string, cb: MqlListener) => entry.listeners.delete(cb),
      addListener: (cb: MqlListener) => entry.listeners.add(cb),
      removeListener: (cb: MqlListener) => entry.listeners.delete(cb),
      dispatchEvent: () => false,
    };
  });
  return {
    set(next: (query: string) => boolean) {
      resolver = next;
      for (const entry of live) {
        const m = resolver(entry.query);
        if (m === entry.matches) continue;
        entry.matches = m;
        for (const cb of entry.listeners) cb({ matches: m });
      }
    },
  };
}
