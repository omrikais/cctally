import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import {
  splitByTerms, splitByRegex, buildSplit, applyMarksToHast,
  splitToReactNodes, markRectClipped, firstLandableMark,
} from './findMark';
import type { Root } from 'hast';

describe('splitByTerms', () => {
  it('splits a value into literal/hit segments (longest term first)', () => {
    expect(splitByTerms('a cache.db b', ['cache', 'cache.db'], false))
      .toEqual([{ s: 'a ', hit: false }, { s: 'cache.db', hit: true }, { s: ' b', hit: false }]);
  });
  it('returns null when nothing matches', () => {
    expect(splitByTerms('nothing here', ['zzz'], false)).toBeNull();
  });
  it('honors caseSensitive', () => {
    expect(splitByTerms('Flock FLOCK', ['Flock'], true))
      .toEqual([{ s: 'Flock', hit: true }, { s: ' FLOCK', hit: false }]);
  });
});

describe('splitByRegex', () => {
  it('marks regex matches', () => {
    expect(splitByRegex('a1 b2', /\d/g))
      .toEqual([{ s: 'a', hit: false }, { s: '1', hit: true }, { s: ' b', hit: false }, { s: '2', hit: true }]);
  });
  it('returns null with no match', () => {
    expect(splitByRegex('abc', /\d/g)).toBeNull();
  });
});

describe('buildSplit', () => {
  it('returns null for null / empty', () => {
    expect(buildSplit(null)).toBeNull();
    expect(buildSplit({ kind: 'terms', terms: [], caseSensitive: false })).toBeNull();
  });
  it('builds a terms splitter', () => {
    const f = buildSplit({ kind: 'terms', terms: ['x'], caseSensitive: false })!;
    expect(f('x')).toEqual([{ s: 'x', hit: true }]);
  });
  it('builds a regex splitter, null on invalid / over-cap', () => {
    expect(buildSplit({ kind: 'regex', source: '\\d', caseSensitive: false })!('a1'))
      .toEqual([{ s: 'a', hit: false }, { s: '1', hit: true }]);
    expect(buildSplit({ kind: 'regex', source: '(', caseSensitive: false })).toBeNull();
    expect(buildSplit({ kind: 'regex', source: 'a'.repeat(1001), caseSensitive: false })).toBeNull();
  });
});

describe('applyMarksToHast', () => {
  const tree = (): Root => ({
    type: 'root',
    children: [
      { type: 'element', tagName: 'p', properties: {}, children: [{ type: 'text', value: 'foo bar' }] },
      { type: 'element', tagName: 'code', properties: {}, children: [{ type: 'text', value: 'foo' }] },
    ],
  });
  const marks = (t: Root): string[] => {
    const out: string[] = [];
    const walk = (n: any) => {
      if (n.type === 'element' && n.tagName === 'mark') out.push(n.children[0].value);
      (n.children ?? []).forEach(walk);
    };
    t.children.forEach(walk);
    return out;
  };
  it('skipCode: true leaves code subtrees untouched', () => {
    const t = tree();
    applyMarksToHast(t, (v) => splitByTerms(v, ['foo'], false), { skipCode: true });
    expect(marks(t)).toEqual(['foo']); // only the <p>, not the <code>
  });
  it('skipCode: false marks inside code subtrees too', () => {
    const t = tree();
    applyMarksToHast(t, (v) => splitByTerms(v, ['foo'], false), { skipCode: false });
    expect(marks(t)).toEqual(['foo', 'foo']); // both
  });
});

describe('splitToReactNodes', () => {
  it('returns the bare string when nothing matches', () => {
    const split = (v: string) => splitByTerms(v, ['zzz'], false);
    const { container } = render(<>{splitToReactNodes('hello', split)}</>);
    expect(container.querySelector('mark')).toBeNull();
    expect(container.textContent).toBe('hello');
  });
  it('wraps hits in <mark>', () => {
    const split = (v: string) => splitByTerms(v, ['ell'], false);
    const { container } = render(<>{splitToReactNodes('hello', split)}</>);
    expect(container.querySelector('mark')?.textContent).toBe('ell');
    expect(container.textContent).toBe('hello');
  });
});

describe('markRectClipped', () => {
  const r = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 100 });
  it('false when the mark is inside every clip', () => {
    expect(markRectClipped(r(50, 60), [r(0, 100)])).toBe(false);
  });
  it('true when the mark is below a clip', () => {
    expect(markRectClipped(r(150, 160), [r(0, 100)])).toBe(true);
  });
});

describe('firstLandableMark', () => {
  // Helper: build a turn with marks at given rects, optionally inside an
  // overflow-clipping wrapper, and stub getBoundingClientRect per element.
  function stubRect(el: HTMLElement, rect: { top: number; bottom: number; left?: number; right?: number }) {
    el.getBoundingClientRect = () =>
      ({ top: rect.top, bottom: rect.bottom, left: rect.left ?? 0, right: rect.right ?? 100,
         width: (rect.right ?? 100) - (rect.left ?? 0), height: rect.bottom - rect.top, x: 0, y: 0, toJSON() {} }) as DOMRect;
  }
  it('returns the first NON-clipped mark, skipping a clipped one', () => {
    const turn = document.createElement('div');
    const clip = document.createElement('div');
    clip.style.overflow = 'auto';
    stubRect(clip, { top: 0, bottom: 100 });
    const m1 = document.createElement('mark'); // inside the clip, scrolled out of view
    clip.appendChild(m1); stubRect(m1, { top: 300, bottom: 320 });
    const m2 = document.createElement('mark'); // normal prose, landable
    stubRect(m2, { top: 50, bottom: 70 });
    turn.appendChild(clip); turn.appendChild(m2);
    document.body.appendChild(turn);
    expect(firstLandableMark(turn)).toBe(m2);
    turn.remove();
  });
  it('returns null when the only mark is clipped', () => {
    const turn = document.createElement('div');
    const clip = document.createElement('div');
    clip.style.overflow = 'hidden';
    stubRect(clip, { top: 0, bottom: 100 });
    const m1 = document.createElement('mark');
    clip.appendChild(m1); stubRect(m1, { top: 300, bottom: 320 });
    turn.appendChild(clip);
    document.body.appendChild(turn);
    expect(firstLandableMark(turn)).toBeNull();
    turn.remove();
  });
});
