import { describe, it, expect, vi } from 'vitest';
import { isInteractiveActivationTarget, cardRegionClick } from './cardRegion';

function el(html: string): HTMLElement {
  const host = document.createElement('div');
  host.innerHTML = html;
  return host.firstElementChild as HTMLElement;
}

describe('isInteractiveActivationTarget', () => {
  it('is true for a button and for an SVG glyph inside a button', () => {
    const btn = el('<button><svg><path/></svg></button>');
    expect(isInteractiveActivationTarget(btn)).toBe(true);
    expect(isInteractiveActivationTarget(btn.querySelector('path'))).toBe(true);
  });
  it('is true for a, input, select, textarea, [role=button], and the grip ignore hook', () => {
    for (const h of ['<a href="#">x</a>', '<input/>', '<select></select>',
                     '<textarea></textarea>',
                     '<div role="button">x</div>', '<span data-card-region-ignore></span>']) {
      expect(isInteractiveActivationTarget(el(h))).toBe(true);
    }
  });
  it('is true for an element nested inside the grip ignore hook', () => {
    const grip = el('<span data-card-region-ignore><i>glyph</i></span>');
    expect(isInteractiveActivationTarget(grip.querySelector('i'))).toBe(true);
  });
  it('is false for a bare div/td card body and for null', () => {
    expect(isInteractiveActivationTarget(el('<div>body</div>'))).toBe(false);
    expect(isInteractiveActivationTarget(el('<td>cell</td>'))).toBe(false);
    expect(isInteractiveActivationTarget(null)).toBe(false);
  });
});

describe('cardRegionClick', () => {
  it('fires openModal once for a bare-body target', () => {
    const open = vi.fn();
    cardRegionClick(open)({ target: el('<div>body</div>') } as unknown as React.MouseEvent);
    expect(open).toHaveBeenCalledTimes(1);
  });
  it('does not fire for an interactive/ignore target', () => {
    const open = vi.fn();
    cardRegionClick(open)({ target: el('<button>x</button>') } as unknown as React.MouseEvent);
    cardRegionClick(open)({ target: el('<span data-card-region-ignore></span>') } as unknown as React.MouseEvent);
    expect(open).not.toHaveBeenCalled();
  });
});
