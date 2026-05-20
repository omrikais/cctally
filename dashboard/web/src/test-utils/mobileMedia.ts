// Shared matchMedia stub for tests that exercise useIsMobile-driven
// branches. Codifies the inline `vi.stubGlobal('matchMedia', …)`
// recipe already used in ComposerModal.test.tsx, ShareManualSmoke
// .test.tsx, and a11y.test.tsx. Call inside `beforeEach`; `afterEach`
// should run `vi.restoreAllMocks()` (standard project pattern) to
// undo the stub.
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
