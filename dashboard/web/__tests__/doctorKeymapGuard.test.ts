// Tests the composite guard semantics the `d` keymap binding in
// main.tsx attaches via registerKeymap. The binding is reconstructed
// here verbatim so the guard predicate can be exercised without
// re-importing main.tsx (which boots SSE and registers every other
// global as a side effect). The point of this test is to lock down
// the spec §6.4 / Codex M5 invariant: the `d` keymap MUST gate on
// openModal AND update.modalOpen AND inputMode — not just one.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  registerKeymap,
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../src/store/keymap';
import {
  dispatch,
  getState,
  _resetForTests as _resetStore,
} from '../src/store/store';

function keyDown(key: string): void {
  const ev = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
  document.dispatchEvent(ev);
}

// Mirror of the binding registered in main.tsx. Keep in sync.
function registerDoctorBinding(spy: () => void): void {
  const _doctorOpenGuard = (): boolean => {
    const s = getState();
    if (s.openModal !== null) return false;
    if (s.update.modalOpen) return false;
    if (s.inputMode !== null) return false;
    return true;
  };
  registerKeymap([
    { key: 'd', scope: 'global', when: _doctorOpenGuard, action: spy },
  ]);
}

describe('doctor `d` keymap composite guard', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetStore();
    _resetKeymap();
    installGlobalKeydown();
  });
  afterEach(() => {
    uninstallGlobalKeydown();
  });

  it('fires `d` when no modal and no input mode', () => {
    const spy = vi.fn();
    registerDoctorBinding(spy);
    keyDown('d');
    expect(spy).toHaveBeenCalled();
  });

  it('suppresses `d` while a panel modal is open (openModal !== null)', () => {
    const spy = vi.fn();
    registerDoctorBinding(spy);
    dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
    keyDown('d');
    expect(spy).not.toHaveBeenCalled();
  });

  it('suppresses `d` while the update modal is open', () => {
    const spy = vi.fn();
    registerDoctorBinding(spy);
    dispatch({ type: 'OPEN_UPDATE_MODAL' });
    keyDown('d');
    expect(spy).not.toHaveBeenCalled();
  });

  it('suppresses `d` while inputMode is filter', () => {
    const spy = vi.fn();
    registerDoctorBinding(spy);
    dispatch({ type: 'SET_INPUT_MODE', mode: 'filter' });
    keyDown('d');
    expect(spy).not.toHaveBeenCalled();
  });

  it('suppresses `d` while inputMode is search', () => {
    const spy = vi.fn();
    registerDoctorBinding(spy);
    dispatch({ type: 'SET_INPUT_MODE', mode: 'search' });
    keyDown('d');
    expect(spy).not.toHaveBeenCalled();
  });

  it('fires `d` after closing the panel modal that suppressed it', () => {
    const spy = vi.fn();
    registerDoctorBinding(spy);
    dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
    keyDown('d');
    expect(spy).not.toHaveBeenCalled();
    dispatch({ type: 'CLOSE_MODAL' });
    keyDown('d');
    expect(spy).toHaveBeenCalled();
  });

  it('the registered action opens the doctor modal', () => {
    const action = (): void => dispatch({ type: 'OPEN_DOCTOR_MODAL' });
    registerKeymap([{ key: 'd', scope: 'global', action }]);
    expect(getState().doctorModalOpen).toBe(false);
    keyDown('d');
    expect(getState().doctorModalOpen).toBe(true);
  });
});
