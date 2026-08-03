import { describe, expect, it } from 'vitest';
import { headingIsVisible } from './headingVisibility';

function build(html: string): HTMLElement {
  const host = document.createElement('div');
  host.innerHTML = html;
  document.body.appendChild(host);
  return host;
}

describe('#463 S2 — a landing means VISIBLE, not merely present in the DOM', () => {
  it('accepts a plain element', () => {
    const host = build('<span data-heading-key="k">A</span>');
    expect(headingIsVisible(host.querySelector('[data-heading-key]')!)).toBe(true);
  });

  it('rejects an element inside a CLOSED disclosure body', () => {
    // HTML keeps a closed <details>'s children in the document, so
    // querySelectorAll resolves them. A step onto one reported success,
    // advanced the cursor and produced no mark and no scroll.
    const host = build(
      '<details><summary>s</summary><span data-heading-key="k">A</span></details>');
    expect(headingIsVisible(host.querySelector('[data-heading-key]')!)).toBe(false);
  });

  it('accepts the same element once the disclosure is open', () => {
    const host = build(
      '<details open><summary>s</summary><span data-heading-key="k">A</span></details>');
    expect(headingIsVisible(host.querySelector('[data-heading-key]')!)).toBe(true);
  });

  it('accepts a heading inside a CLOSED disclosure SUMMARY', () => {
    // The expandable reasoning block renders its headings in the <summary>, which
    // stays on screen while the disclosure is shut. Rejecting those would make
    // every expandable block unreachable.
    const host = build(
      '<details><summary><span data-heading-key="k">A</span></summary><div>body</div></details>');
    expect(headingIsVisible(host.querySelector('[data-heading-key]')!)).toBe(true);
  });

  it('rejects an element nested several levels inside a closed disclosure', () => {
    const host = build(
      '<details><summary>s</summary><div><p><span data-heading-key="k">A</span></p></div></details>');
    expect(headingIsVisible(host.querySelector('[data-heading-key]')!)).toBe(false);
  });

  it('rejects an element under an OPEN disclosure nested in a CLOSED one', () => {
    const host = build(
      '<details><summary>outer</summary>'
      + '<details open><summary>inner</summary><span data-heading-key="k">A</span></details>'
      + '</details>');
    expect(headingIsVisible(host.querySelector('[data-heading-key]')!)).toBe(false);
  });

  it('prefers the browser answer when Element.checkVisibility exists', () => {
    // jsdom 25 implements neither checkVisibility nor layout (offsetParent is
    // null for every element there, so the usual offsetParent test cannot stand
    // in). The fallback below it is the jsdom path; this pins that a real
    // browser's own answer wins when it is available.
    const host = build('<span data-heading-key="k">A</span>');
    const el = host.querySelector('[data-heading-key]')! as Element & { checkVisibility?: () => boolean };
    el.checkVisibility = () => false;
    expect(headingIsVisible(el)).toBe(false);
    el.checkVisibility = () => true;
    expect(headingIsVisible(el)).toBe(true);
  });
});
