import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { _resetForTests, dispatch, getState, type Action } from './store';
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
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
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

// The doctor key's composite guard (spec §6.4, Codex M5): `d` must stay inert
// under a panel modal, the update modal and either text-input mode, not only
// under a chrome overlay — otherwise the doctor modal opens on top of another
// modal or swallows a `d` typed into the filter/search box.
describe('doctor (d) composite guard', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
    registerKeymap(buildGlobalKeyBindings());
  });
  afterEach(() => { uninstallGlobalKeydown(); });

  const pressD = (): void => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'd', bubbles: true, cancelable: true }));
  };
  const doctorGuard = () => buildGlobalKeyBindings().find((b) => b.key === 'd')!.when!;

  it('opens the doctor modal when nothing else owns the keyboard', () => {
    expect(doctorGuard()()).toBe(true);
    pressD();
    expect(getState().doctorModalOpen).toBe(true);
  });

  it.each<[string, Action, Action]>([
    ['an open panel modal', { type: 'OPEN_MODAL', kind: 'daily' }, { type: 'CLOSE_MODAL' }],
    ['the update modal', { type: 'OPEN_UPDATE_MODAL' }, { type: 'CLOSE_UPDATE_MODAL' }],
    ['filter input mode', { type: 'SET_INPUT_MODE', mode: 'filter' }, { type: 'SET_INPUT_MODE', mode: null }],
    ['search input mode', { type: 'SET_INPUT_MODE', mode: 'search' }, { type: 'SET_INPUT_MODE', mode: null }],
  ])('is suppressed under %s and fires again once it closes', (_label, open, close) => {
    dispatch(open);
    expect(doctorGuard()()).toBe(false);
    pressD();
    expect(getState().doctorModalOpen).toBe(false);
    dispatch(close);
    expect(doctorGuard()()).toBe(true);
    pressD();
    expect(getState().doctorModalOpen).toBe(true);
  });
});
