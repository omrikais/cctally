// #503 S4 §2 — the cross-document Escape bridge.
//
// These tests deliberately drive the `load` event by hand rather than relying
// on jsdom's `srcDoc` navigation behavior: the design must not depend on
// whether jsdom emits `load` for a srcdoc assignment. Real `srcdoc` loads are
// exercised at the browser gate.
import { render, act } from '@testing-library/react';
import { useState } from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { useIframeKeymapBridge } from './useIframeKeymapBridge';
import { registerKeymap, _resetForTests } from '../store/keymap';

function Harness({ onEl }: { onEl: (el: HTMLIFrameElement | null) => void }) {
  const [el, setEl] = useState<HTMLIFrameElement | null>(null);
  useIframeKeymapBridge(el);
  return <iframe title="t" ref={(n) => { setEl(n); onEl(n); }} />;
}

describe('useIframeKeymapBridge', () => {
  beforeEach(() => _resetForTests());

  it('forwards Escape from the child document into the dispatcher', () => {
    const fired: string[] = [];
    registerKeymap([{ key: 'Escape', scope: 'overlay', layer: 200, action: () => fired.push('esc') }]);
    let el: HTMLIFrameElement | null = null;
    render(<Harness onEl={(n) => { el = n; }} />);
    act(() => { el!.dispatchEvent(new Event('load')); });
    const doc = el!.contentDocument!;
    act(() => { doc.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); });
    expect(fired).toEqual(['esc']);
  });

  it('does not forward printable keys', () => {
    const fired: string[] = [];
    registerKeymap([{ key: 's', scope: 'global', action: () => fired.push('s') }]);
    let el: HTMLIFrameElement | null = null;
    render(<Harness onEl={(n) => { el = n; }} />);
    act(() => { el!.dispatchEvent(new Event('load')); });
    const doc = el!.contentDocument!;
    act(() => { doc.dispatchEvent(new KeyboardEvent('keydown', { key: 's', bubbles: true })); });
    expect(fired).toEqual([]);
  });

  // THE LEAK TEST. Asserting on the replacement document proves only that the
  // NEW document has one listener; a listener still attached to the OLD one is
  // invisible to that assertion. So dispatch on the retained old document and
  // require ZERO.
  it('detaches from the previous document when the frame reloads', () => {
    const fired: string[] = [];
    registerKeymap([{ key: 'Escape', scope: 'overlay', layer: 200, action: () => fired.push('esc') }]);
    let el: HTMLIFrameElement | null = null;
    render(<Harness onEl={(n) => { el = n; }} />);
    act(() => { el!.dispatchEvent(new Event('load')); });
    const oldDoc = el!.contentDocument!;

    // Simulate a srcdoc navigation producing a fresh document.
    const replacement = document.implementation.createHTMLDocument('next');
    Object.defineProperty(el!, 'contentDocument', { value: replacement, configurable: true });
    act(() => { el!.dispatchEvent(new Event('load')); });

    act(() => { oldDoc.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); });
    expect(fired).toEqual([]);                                  // old document is dead

    act(() => { replacement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); });
    expect(fired).toEqual(['esc']);                             // current document fires exactly once
  });

  it('detaches on unmount', () => {
    const fired: string[] = [];
    registerKeymap([{ key: 'Escape', scope: 'overlay', layer: 200, action: () => fired.push('esc') }]);
    let el: HTMLIFrameElement | null = null;
    const { unmount } = render(<Harness onEl={(n) => { el = n; }} />);
    act(() => { el!.dispatchEvent(new Event('load')); });
    const doc = el!.contentDocument!;
    unmount();
    act(() => { doc.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); });
    expect(fired).toEqual([]);
  });
});
