import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import { OutlineResizer } from './OutlineResizer';
import { _resetForTests, getState } from '../store/store';
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
});
