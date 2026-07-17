import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, act } from '@testing-library/react';
import { useLayoutEffect } from 'react';
import { useReaderControlsDensity, COMPACT_READER_CONTROLS_PX } from './useReaderControlsDensity';

// #304 S3 — this is an ELEMENT-width resolver (ResizeObserver + a single
// getBoundingClientRect().width metric), NOT a matchMedia hook, so the tests
// stub ResizeObserver and a fake element's rect — never matchMedia (per the
// #304 S1 stubResponsiveMedia lesson: don't stub the wrong axis).

interface FakeRO {
  cb: () => void;
  observed: Element[];
  disconnect: ReturnType<typeof vi.fn>;
}
let ros: FakeRO[];

class FakeResizeObserver {
  cb: () => void;
  observed: Element[] = [];
  disconnect = vi.fn();
  constructor(cb: () => void) {
    this.cb = cb;
    ros.push(this);
  }
  observe(el: Element) {
    this.observed.push(el);
  }
  unobserve() {}
}

function makeEl(width: number): { el: HTMLElement; setWidth: (w: number) => void } {
  let w = width;
  const el = document.createElement('div');
  el.getBoundingClientRect = () =>
    ({ width: w, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
  return { el, setWidth: (nw: number) => { w = nw; } };
}

function Probe({ el }: { el: HTMLElement | null }) {
  const { density, readerRef } = useReaderControlsDensity();
  useLayoutEffect(() => {
    readerRef(el);
    return () => readerRef(null);
  }, [el, readerRef]);
  return <output>{density}</output>;
}

beforeEach(() => {
  ros = [];
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('useReaderControlsDensity (#304 S3)', () => {
  it('exposes the normative 720 threshold', () => {
    expect(COMPACT_READER_CONTROLS_PX).toBe(720);
  });

  it('(a) measures synchronously at mount: width 719 → compact (before any observer fire)', () => {
    const { el } = makeEl(719);
    const { getByText } = render(<Probe el={el} />);
    expect(getByText('compact')).toBeTruthy();
  });

  it('(b) width 720 → full (boundary is <, not <=)', () => {
    const { el } = makeEl(720);
    const { getByText } = render(<Probe el={el} />);
    expect(getByText('full')).toBeTruthy();
  });

  it('(c) width 721 → full', () => {
    const { el } = makeEl(721);
    const { getByText } = render(<Probe el={el} />);
    expect(getByText('full')).toBeTruthy();
  });

  it('(d) width 0 / JSDOM-unmeasurable → full', () => {
    const { el } = makeEl(0);
    const { getByText } = render(<Probe el={el} />);
    expect(getByText('full')).toBeTruthy();
  });

  it('(e) re-measures on observer fire, ignoring entry rects: 800→600 → compact', () => {
    const { el, setWidth } = makeEl(800);
    const { getByText } = render(<Probe el={el} />);
    expect(getByText('full')).toBeTruthy();
    setWidth(600);
    // Entries are signals ONLY — fire the callback with no args; the hook must
    // re-read getBoundingClientRect itself, not trust any entry content-box.
    act(() => { ros[ros.length - 1].cb(); });
    expect(getByText('compact')).toBeTruthy();
  });

  it('(f) disconnects the observer on unmount', () => {
    const { el } = makeEl(900);
    const { unmount } = render(<Probe el={el} />);
    const ro = ros[ros.length - 1];
    unmount();
    expect(ro.disconnect).toHaveBeenCalled();
  });

  it('(g) readerRef(null) disconnects without crashing', () => {
    const { el } = makeEl(900);
    const { rerender } = render(<Probe el={el} />);
    const ro = ros[ros.length - 1];
    expect(() => rerender(<Probe el={null} />)).not.toThrow();
    expect(ro.disconnect).toHaveBeenCalled();
  });
});
