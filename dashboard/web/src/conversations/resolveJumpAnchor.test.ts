import { describe, expect, it } from 'vitest';
import { BLOCK_ANCHOR_CLASS, resolveJumpAnchor } from './resolveJumpAnchor';

function item(html: string): HTMLElement {
  const el = document.createElement('div');
  el.innerHTML = html;
  return el;
}

describe('#463 S4 F-A — resolveJumpAnchor', () => {
  it('returns null when no key was asked for', () => {
    expect(resolveJumpAnchor(item('<span data-block-key="b1"></span>'), null)).toBeNull();
    expect(resolveJumpAnchor(item('<span data-block-key="b1"></span>'), '')).toBeNull();
  });

  it('returns null when the key names nothing in this item', () => {
    expect(resolveJumpAnchor(item('<span data-block-key="b1"></span>'), 'b2')).toBeNull();
  });

  it('resolves a plain block key to the element that carries it', () => {
    const el = item('<p id="a"></p><section id="b" data-block-key="cbk1.x"></section>');
    expect(resolveJumpAnchor(el, 'cbk1.x')!.id).toBe('b');
  });

  it('resolves a reasoning heading key, which the block key does not name', () => {
    const el = item(
      '<div data-block-key="cbk1.r"><span id="h0" data-heading-key="cbk1.r#0"></span>'
      + '<span id="h1" data-heading-key="cbk1.r#1"></span></div>',
    );
    expect(resolveJumpAnchor(el, 'cbk1.r#1')!.id).toBe('h1');
    expect(resolveJumpAnchor(el, 'cbk1.r')!.getAttribute('data-block-key')).toBe('cbk1.r');
  });

  it('descends through the box-less anchor wrapper to the card it names', () => {
    // The wrapper is `display: contents`; measured in a real browser it reports
    // a 0x0 rect at the origin, so aligning it would scroll to the top of the
    // document rather than to the card.
    const el = item(
      `<div class="${BLOCK_ANCHOR_CLASS}" data-block-key="cbk1.e">`
      + '<details id="card"></details></div>',
    );
    expect(resolveJumpAnchor(el, 'cbk1.e')!.id).toBe('card');
  });

  it('reports no anchor rather than the wrapper when the wrapper is empty', () => {
    const el = item(`<div class="${BLOCK_ANCHOR_CLASS}" data-block-key="cbk1.e"></div>`);
    expect(resolveJumpAnchor(el, 'cbk1.e')).toBeNull();
  });

  it('escapes a key whose punctuation is CSS syntax', () => {
    const el = item('<span id="x" data-heading-key="cbk1.r#3"></span>');
    expect(resolveJumpAnchor(el, 'cbk1.r#3')!.id).toBe('x');
  });

  // C-5 — the guard used to name one class. The condition it stood for is that
  // the element generates no box, so it has no rect to align; `display: contents`
  // is one instance of that condition and the class is one way of reaching it.
  it('descends through a box-less wrapper that does not carry the class', () => {
    const el = item(
      '<div id="wrap" style="display: contents" data-block-key="cbk1.e">'
      + '<details id="card"></details></div>',
    );
    expect(resolveJumpAnchor(el, 'cbk1.e')!.id).toBe('card');
  });

  it('keeps an element that DOES generate a box, even with a child', () => {
    const el = item('<div id="wrap" data-block-key="cbk1.e"><span id="kid"></span></div>');
    expect(resolveJumpAnchor(el, 'cbk1.e')!.id).toBe('wrap');
  });

  it('descends through a chain of box-less wrappers', () => {
    const el = item(
      `<div class="${BLOCK_ANCHOR_CLASS}" data-block-key="cbk1.e">`
      + '<div style="display: contents"><details id="card"></details></div></div>',
    );
    expect(resolveJumpAnchor(el, 'cbk1.e')!.id).toBe('card');
  });
});
