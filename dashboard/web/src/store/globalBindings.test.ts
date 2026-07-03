import { afterEach, describe, it, expect, vi } from 'vitest';
import { buildGlobalKeyBindings } from './globalBindings';
import { registeredBindings, registerKeymap, _resetForTests } from './keymap';
import { _resetForTests as _resetStore } from './store';

describe('buildGlobalKeyBindings', () => {
  it('returns the always-on globals incl. r/q/n/N/d and digit openers', () => {
    const keys = buildGlobalKeyBindings().map((b) => b.key);
    for (const k of ['1', '0', 'r', 'q', 'n', 'N', 'd', 'c']) expect(keys).toContain(k);
  });
});

// #264 S4 (A3/Codex-P2): the `c` collapse keymap must be inert in the desktop
// bento (>=900px), where the collapse chevron is hidden — a desktop `c` press
// would otherwise silently flip sessionsCollapsed, a pref that still governs the
// <900px stacked view. The gate reads matchMedia(BENTO_MEDIA_QUERY).
describe('c collapse keymap viewport gate', () => {
  afterEach(() => vi.unstubAllGlobals());
  function stubMatchMedia(matches: boolean) {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches, media: q, onchange: null,
      addEventListener() {}, removeEventListener() {},
      addListener() {}, removeListener() {}, dispatchEvent: () => false,
    }));
  }
  it('is inert in the desktop bento (>=900px)', () => {
    _resetStore();
    stubMatchMedia(true);
    const c = buildGlobalKeyBindings().find((b) => b.key === 'c')!;
    expect(c.when?.()).toBe(false);
  });
  it('is active below 900px', () => {
    _resetStore();
    stubMatchMedia(false);
    const c = buildGlobalKeyBindings().find((b) => b.key === 'c')!;
    expect(c.when?.()).toBe(true);
  });
});

describe('registeredBindings introspection', () => {
  it('reflects what was registered', () => {
    _resetForTests();
    registerKeymap([{ key: 'X', scope: 'global', action: () => {} }]);
    expect(registeredBindings().some((b) => b.key === 'X' && b.scope === 'global')).toBe(true);
  });
});
