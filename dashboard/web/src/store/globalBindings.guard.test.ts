import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { _resetForTests, dispatch } from './store';
import {
  _resetForTests as _resetKeymap,
  installGlobalKeydown,
  uninstallGlobalKeydown,
  registerKeymap,
} from './keymap';
import { buildGlobalKeyBindings } from './globalBindings';

describe('D2: globals are inert while a chrome overlay is open', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
    registerKeymap(buildGlobalKeyBindings());
  });
  afterEach(() => { uninstallGlobalKeydown(); });

  it('q does not fire tryQuit under chromeOverlayOpen', () => {
    const guard = buildGlobalKeyBindings().find((b) => b.key === 'q')!.when!;
    expect(guard()).toBe(true);                       // baseline: fires
    dispatch({ type: 'INCREMENT_CHROME_OVERLAY' });
    expect(guard()).toBe(false);                      // suppressed
  });
  it('q also suppressed under an open panel modal / input mode', () => {
    const guard = buildGlobalKeyBindings().find((b) => b.key === 'q')!.when!;
    dispatch({ type: 'OPEN_MODAL', kind: 'history' });
    expect(guard()).toBe(false);
  });
  it('doctor (d) is suppressed under chromeOverlayOpen', () => {
    const guard = buildGlobalKeyBindings().find((b) => b.key === 'd')!.when!;
    expect(guard()).toBe(true);
    dispatch({ type: 'INCREMENT_CHROME_OVERLAY' });
    expect(guard()).toBe(false);
  });
  it('collapse (c) is suppressed under chromeOverlayOpen', () => {
    const guard = buildGlobalKeyBindings().find((b) => b.key === 'c')!.when!;
    expect(guard()).toBe(true);
    dispatch({ type: 'INCREMENT_CHROME_OVERLAY' });
    expect(guard()).toBe(false);
  });
});
