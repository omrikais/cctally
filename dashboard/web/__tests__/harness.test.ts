import { describe, it, expect } from 'vitest';

describe('vitest harness', () => {
  it('runs a trivial assertion', () => {
    expect(1 + 1).toBe(2);
  });

  it('has jsdom', () => {
    const el = document.createElement('div');
    el.textContent = 'hello';
    expect(el.textContent).toBe('hello');
  });

  it('has ResizeObserver polyfill', () => {
    expect(typeof ResizeObserver).toBe('function');
    const ro = new ResizeObserver(() => {});
    expect(ro).toBeDefined();
  });
});
