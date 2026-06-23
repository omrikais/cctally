import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import { OutlineResizer } from './OutlineResizer';
import { _resetForTests, getState } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  registerKeymap,
  _resetForTests as _resetKeymapForTests,
} from '../store/keymap';
import {
  OUTLINE_WIDTH_KEY,
  OUTLINE_WIDTH_MIN,
  OUTLINE_WIDTH_MAX,
} from '../store/outlineWidth';

// #217 S3 E6(b) — the keyboard + a11y surface of the outline resize divider.
// Pointer-drag PIXEL math is verified in the Playwright pass (JSDOM has no
// layout); here we assert the keyboard resize + persistence + a11y roles, which
// JSDOM CAN evaluate.
beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});
afterEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('OutlineResizer (#217 S3 E6(b))', () => {
  it('renders a separator with a vertical orientation + an aria-label', () => {
    const { container } = render(<OutlineResizer />);
    const sep = container.querySelector('[role="separator"]')!;
    expect(sep).toBeTruthy();
    expect(sep.getAttribute('aria-orientation')).toBe('vertical');
    expect(sep.getAttribute('aria-label')).toBeTruthy();
  });

  it('exposes the current width via aria-valuenow / min / max', () => {
    const { container } = render(<OutlineResizer />);
    const sep = container.querySelector('[role="separator"]')!;
    expect(Number(sep.getAttribute('aria-valuemin'))).toBe(OUTLINE_WIDTH_MIN);
    expect(Number(sep.getAttribute('aria-valuemax'))).toBe(OUTLINE_WIDTH_MAX);
    expect(Number(sep.getAttribute('aria-valuenow'))).toBe(getState().convOutlineWidth);
  });

  it('ArrowLeft WIDENS the outline (the column is on the right) and persists', () => {
    const { container } = render(<OutlineResizer />);
    const sep = container.querySelector('[role="separator"]')!;
    const before = getState().convOutlineWidth;
    fireEvent.keyDown(sep, { key: 'ArrowLeft' });
    const after = getState().convOutlineWidth;
    expect(after).toBeGreaterThan(before);
    // Persisted to localStorage.
    expect(Number(localStorage.getItem(OUTLINE_WIDTH_KEY))).toBe(after);
  });

  it('ArrowRight NARROWS the outline and persists', () => {
    const { container } = render(<OutlineResizer />);
    const sep = container.querySelector('[role="separator"]')!;
    const before = getState().convOutlineWidth;
    fireEvent.keyDown(sep, { key: 'ArrowRight' });
    const after = getState().convOutlineWidth;
    expect(after).toBeLessThan(before);
    expect(Number(localStorage.getItem(OUTLINE_WIDTH_KEY))).toBe(after);
  });

  it('clamps at the maximum (many widen presses never exceed MAX)', () => {
    const { container } = render(<OutlineResizer />);
    const sep = container.querySelector('[role="separator"]')!;
    for (let i = 0; i < 200; i++) fireEvent.keyDown(sep, { key: 'ArrowLeft' });
    expect(getState().convOutlineWidth).toBe(OUTLINE_WIDTH_MAX);
  });

  it('clamps at the minimum (many narrow presses never drop below MIN)', () => {
    const { container } = render(<OutlineResizer />);
    const sep = container.querySelector('[role="separator"]')!;
    for (let i = 0; i < 200; i++) fireEvent.keyDown(sep, { key: 'ArrowRight' });
    expect(getState().convOutlineWidth).toBe(OUTLINE_WIDTH_MIN);
  });

  it('Home jumps to MAX, End jumps to MIN', () => {
    const { container } = render(<OutlineResizer />);
    const sep = container.querySelector('[role="separator"]')!;
    fireEvent.keyDown(sep, { key: 'Home' });
    expect(getState().convOutlineWidth).toBe(OUTLINE_WIDTH_MAX);
    fireEvent.keyDown(sep, { key: 'End' });
    expect(getState().convOutlineWidth).toBe(OUTLINE_WIDTH_MIN);
  });

  // #228 S1 (F2) — a mouse click must move keyboard focus to the divider, so a
  // pointer user can immediately Arrow-resize. JSDOM has no pixel layout but it
  // DOES track document.activeElement, so the focus call is testable here.
  it('focuses the resizer on pointer-down (so a mouse click can then keyboard-resize)', () => {
    const { container } = render(<OutlineResizer />);
    const sep = container.querySelector('[role="separator"]')! as HTMLElement;
    expect(sep).not.toHaveFocus();
    fireEvent.pointerDown(sep, { pointerId: 1 });
    expect(sep).toHaveFocus();
  });

  // Cross-branch review P3 — a HANDLED resizer key must NOT also reach the
  // document-level global keymap (a double-fire). The reader binds `End` to
  // jump-to-latest at scope 'global'; pressing `End` while the resizer is focused
  // must resize the outline only. The fix is ev.stopPropagation() on every handled
  // key. NON-VACUITY: removing that stopPropagation lets the keydown bubble to the
  // document handler → globalEnd fires → RED.
  it('P3: handled keys (End/Home/arrows) do NOT bubble to the global keymap', () => {
    _resetKeymapForTests();
    installGlobalKeydown();
    try {
      const globalEnd = vi.fn();
      const globalHome = vi.fn();
      const globalArrow = vi.fn();
      registerKeymap([
        { key: 'End', scope: 'global', view: 'any', when: () => true, action: globalEnd },
        { key: 'Home', scope: 'global', view: 'any', when: () => true, action: globalHome },
        { key: 'ArrowLeft', scope: 'global', view: 'any', when: () => true, action: globalArrow },
      ]);
      const { container } = render(<OutlineResizer />);
      const sep = container.querySelector('[role="separator"]')!;
      fireEvent.keyDown(sep, { key: 'End' });
      fireEvent.keyDown(sep, { key: 'Home' });
      fireEvent.keyDown(sep, { key: 'ArrowLeft' });
      // The resizer handled each (width moved), but none reached the global keymap.
      expect(globalEnd).not.toHaveBeenCalled();
      expect(globalHome).not.toHaveBeenCalled();
      expect(globalArrow).not.toHaveBeenCalled();
    } finally {
      uninstallGlobalKeydown();
      _resetKeymapForTests();
    }
  });
});
