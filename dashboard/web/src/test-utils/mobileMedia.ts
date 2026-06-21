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
