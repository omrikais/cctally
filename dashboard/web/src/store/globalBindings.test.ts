import { describe, it, expect } from 'vitest';
import { buildGlobalKeyBindings } from './globalBindings';
import { registeredBindings, registerKeymap, _resetForTests } from './keymap';

describe('buildGlobalKeyBindings', () => {
  it('returns the always-on globals incl. r/q/n/N/d and digit openers', () => {
    const keys = buildGlobalKeyBindings().map((b) => b.key);
    for (const k of ['1', '0', 'r', 'q', 'n', 'N', 'd', 'c']) expect(keys).toContain(k);
  });
});

describe('registeredBindings introspection', () => {
  it('reflects what was registered', () => {
    _resetForTests();
    registerKeymap([{ key: 'X', scope: 'global', action: () => {} }]);
    expect(registeredBindings().some((b) => b.key === 'X' && b.scope === 'global')).toBe(true);
  });
});
