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

function keyDown(key: string, target: EventTarget = document.body): void {
  const ev = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
  Object.defineProperty(ev, 'target', { value: target, configurable: true });
  document.dispatchEvent(ev);
}

beforeEach(() => {
  localStorage.clear();
  _resetStore();
  _resetKeymap();
  installGlobalKeydown();
});
afterEach(() => {
  uninstallGlobalKeydown();
  document.body.innerHTML = '';
});

describe('keymap dispatcher', () => {
  it('fires a global binding', () => {
    const spy = vi.fn();
    registerKeymap([{ key: '1', scope: 'global', action: spy }]);
    keyDown('1');
    expect(spy).toHaveBeenCalled();
  });

  it('modal scope wins over global for the same key', () => {
    const g = vi.fn(); const m = vi.fn();
    registerKeymap([{ key: 'Escape', scope: 'global', action: g }]);
    registerKeymap([{ key: 'Escape', scope: 'modal', action: m }]);
    keyDown('Escape');
    expect(m).toHaveBeenCalled();
    expect(g).not.toHaveBeenCalled();
  });

  it('sessions scope wins over global', () => {
    const g = vi.fn(); const s = vi.fn();
    registerKeymap([{ key: 'f', scope: 'global', action: g }]);
    registerKeymap([{ key: 'f', scope: 'sessions', action: s }]);
    keyDown('f');
    expect(s).toHaveBeenCalled();
    expect(g).not.toHaveBeenCalled();
  });

  it('modal wins over sessions', () => {
    const m = vi.fn(); const s = vi.fn();
    registerKeymap([{ key: 'Escape', scope: 'modal', action: m }]);
    registerKeymap([{ key: 'Escape', scope: 'sessions', action: s }]);
    keyDown('Escape');
    expect(m).toHaveBeenCalled();
    expect(s).not.toHaveBeenCalled();
  });

  it('when predicate gates a binding', () => {
    const spy = vi.fn();
    registerKeymap([{ key: 'x', scope: 'sessions', action: spy, when: () => false }]);
    keyDown('x');
    expect(spy).not.toHaveBeenCalled();
  });

  it('input-mode suppression: single-char keys are swallowed when focus is in INPUT', () => {
    const spy = vi.fn();
    registerKeymap([{ key: '?', scope: 'global', action: spy }]);
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    keyDown('?', input);
    expect(spy).not.toHaveBeenCalled();
  });

  it('input-mode does NOT suppress Escape (multi-char key)', () => {
    const spy = vi.fn();
    registerKeymap([{ key: 'Escape', scope: 'global', action: spy }]);
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    keyDown('Escape', input);
    expect(spy).toHaveBeenCalled();
  });

  it('unregister removes the binding', () => {
    const spy = vi.fn();
    const unreg = registerKeymap([{ key: 'x', scope: 'global', action: spy }]);
    unreg();
    keyDown('x');
    expect(spy).not.toHaveBeenCalled();
  });

  it('only the first matching binding in precedence fires', () => {
    const a = vi.fn(); const b = vi.fn();
    registerKeymap([{ key: '1', scope: 'global', action: a }]);
    registerKeymap([{ key: '1', scope: 'global', action: b }]);
    keyDown('1');
    expect(a.mock.calls.length + b.mock.calls.length).toBe(1);
  });

  it('bails on Ctrl-modifier keystrokes (no dispatch)', () => {
    const spy = vi.fn();
    registerKeymap([{ key: '1', scope: 'global', action: spy }]);
    const ev = new KeyboardEvent('keydown', { key: '1', ctrlKey: true, bubbles: true, cancelable: true });
    document.dispatchEvent(ev);
    expect(spy).not.toHaveBeenCalled();
  });

  it('bails on Cmd-modifier keystrokes (no dispatch)', () => {
    const spy = vi.fn();
    registerKeymap([{ key: 'f', scope: 'global', action: spy }]);
    const ev = new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true, cancelable: true });
    document.dispatchEvent(ev);
    expect(spy).not.toHaveBeenCalled();
  });

  it('bails on Alt-modifier keystrokes (no dispatch)', () => {
    const spy = vi.fn();
    registerKeymap([{ key: 'r', scope: 'global', action: spy }]);
    const ev = new KeyboardEvent('keydown', { key: 'r', altKey: true, bubbles: true, cancelable: true });
    document.dispatchEvent(ev);
    expect(spy).not.toHaveBeenCalled();
  });

  it('preventDefault is called on dispatched bindings', () => {
    const spy = vi.fn();
    registerKeymap([{ key: '/', scope: 'global', action: spy }]);
    const ev = new KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true });
    document.dispatchEvent(ev);
    expect(spy).toHaveBeenCalled();
    expect(ev.defaultPrevented).toBe(true);
  });

  it('preventDefault is NOT called when no binding matches', () => {
    const ev = new KeyboardEvent('keydown', { key: 'z', bubbles: true, cancelable: true });
    document.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(false);
  });

  it('preventDefault is NOT called when binding is suppressed by when()', () => {
    registerKeymap([{ key: 'x', scope: 'global', action: vi.fn(), when: () => false }]);
    const ev = new KeyboardEvent('keydown', { key: 'x', bubbles: true, cancelable: true });
    document.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(false);
  });

  it('contenteditable elements also suppress single-char keys', () => {
    const spy = vi.fn();
    registerKeymap([{ key: 'a', scope: 'global', action: spy }]);
    const div = document.createElement('div');
    div.setAttribute('contenteditable', 'true');
    document.body.appendChild(div);
    div.focus();
    const ev = new KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true });
    Object.defineProperty(ev, 'target', { value: div, configurable: true });
    document.dispatchEvent(ev);
    expect(spy).not.toHaveBeenCalled();
  });

  it('installGlobalKeydown is idempotent (second install is a no-op)', () => {
    // Already installed via beforeEach; calling again shouldn't double-register.
    // Use a single binding and assert exactly one call per keydown.
    const spy = vi.fn();
    registerKeymap([{ key: 'q', scope: 'global', action: spy }]);
    // Call install a second time — it should early-return.
    installGlobalKeydown();
    keyDown('q');
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("'c' action toggles prefs.sessionsCollapsed via SAVE_PREFS", () => {
    // Replicate the action that main.tsx registers for { key: 'c', scope: 'sessions' }.
    const action = (): void => {
      const cur = getState().prefs.sessionsCollapsed;
      dispatch({ type: 'SAVE_PREFS', patch: { sessionsCollapsed: !cur } });
    };
    expect(getState().prefs.sessionsCollapsed).toBe(true);
    action();
    expect(getState().prefs.sessionsCollapsed).toBe(false);
    action();
    expect(getState().prefs.sessionsCollapsed).toBe(true);
  });

  it('"5" opens the weekly modal', () => {
    registerKeymap([
      { key: '5', scope: 'global', action: () => dispatch({ type: 'OPEN_MODAL', kind: 'weekly' }) },
    ]);
    keyDown('5');
    expect(getState().openModal).toBe('weekly');
  });

  it('"6" opens the monthly modal', () => {
    registerKeymap([
      { key: '6', scope: 'global', action: () => dispatch({ type: 'OPEN_MODAL', kind: 'monthly' }) },
    ]);
    keyDown('6');
    expect(getState().openModal).toBe('monthly');
  });

  it('pressing 8 dispatches OPEN_MODAL { kind: "daily" }', () => {
    registerKeymap([
      { key: '8', scope: 'global', action: () => dispatch({ type: 'OPEN_MODAL', kind: 'daily' }) },
    ]);
    keyDown('8');
    expect(getState().openModal).toBe('daily');
    expect(getState().openDailyDate).toBeNull();
  });

  it('UpdateModal modal-scope Esc is gated by update.modalOpen', () => {
    // Regression: UpdateModal is mounted for the app's lifetime, so its
    // modal-scope Escape binding is always registered. Without the
    // `when: () => getState().update.modalOpen` guard, it would win
    // over Settings/Help (global scope) Escape and swallow the event,
    // leaving those overlays unable to close on Esc.
    const updateClose = vi.fn();
    const settingsClose = vi.fn();
    registerKeymap([
      {
        key: 'Escape',
        scope: 'modal',
        action: updateClose,
        when: () => getState().update.modalOpen,
      },
    ]);
    registerKeymap([{ key: 'Escape', scope: 'global', action: settingsClose }]);

    // Update modal closed → modal-scope binding inert; global fires.
    expect(getState().update.modalOpen).toBe(false);
    keyDown('Escape');
    expect(updateClose).not.toHaveBeenCalled();
    expect(settingsClose).toHaveBeenCalledTimes(1);

    // Open the update modal → modal-scope binding wins.
    dispatch({ type: 'OPEN_UPDATE_MODAL' });
    keyDown('Escape');
    expect(updateClose).toHaveBeenCalledTimes(1);
    // Global Settings Esc must NOT fire again — modal wins.
    expect(settingsClose).toHaveBeenCalledTimes(1);
  });

  it("'c' binding is suppressed when a modal is open (when guard)", () => {
    const spy = vi.fn();
    const guardedAction = (): void => {
      // Replicate main.tsx's c binding (action + when guard).
      if (getState().openModal) return;
      spy();
    };
    // No modal open → action runs.
    expect(getState().openModal).toBeNull();
    guardedAction();
    expect(spy).toHaveBeenCalledTimes(1);

    // Modal open → action suppressed.
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    guardedAction();
    expect(spy).toHaveBeenCalledTimes(1); // not called again
  });
});
