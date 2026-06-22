import { describe, expect, it } from 'vitest';
import { render, act, fireEvent } from '@testing-library/react';
import { MediaFigure } from './MediaFigure';
import { TranscriptContext } from './TranscriptContext';

const IMG = { kind: 'image' as const, media_type: 'image/png', bytes: 140000, index: 0 };

function renderWith(el: React.ReactElement, sessionId: string | null = 's1') {
  return render(
    <TranscriptContext.Provider value={{ sessionId }}>{el}</TranscriptContext.Provider>,
  );
}

describe('MediaFigure', () => {
  it('renders a lazy img with the tool_use_id route URL + caption', () => {
    const { container } = renderWith(<MediaFigure media={IMG} toolUseId="tu_1" context="Bash" />);
    const img = container.querySelector('img')!;
    expect(img.getAttribute('src')).toBe('/api/conversation/s1/media?tool_use_id=tu_1&index=0');
    expect(img.getAttribute('loading')).toBe('lazy');
    expect(img.getAttribute('decoding')).toBe('async');
    expect(container.textContent).toContain('image/png');
    expect(container.textContent).toContain('~103 KB'); // 140000 * 3/4 = 105000 B
    const open = container.querySelector('.conv-media-caption a')!;
    expect(open.getAttribute('target')).toBe('_blank');
    expect(open.getAttribute('rel')).toBe('noopener noreferrer');
  });
  it('uses the uuid mode URL for user-content media', () => {
    const { container } = renderWith(<MediaFigure media={IMG} uuid="u-9" context="attached" />);
    expect(container.querySelector('img')!.getAttribute('src'))
      .toBe('/api/conversation/s1/media?uuid=u-9&index=0');
  });
  it('degrades to the badge when unaddressable (no key / no index / no session)', () => {
    const { container: c1 } = renderWith(<MediaFigure media={IMG} context="x" />);
    expect(c1.querySelector('img')).toBeNull();
    expect(c1.textContent).toContain('image/png');
    const { container: c2 } = renderWith(
      <MediaFigure media={{ ...IMG, index: -1 }} toolUseId="t" context="x" />);
    expect(c2.querySelector('img')).toBeNull();
    const { container: c3 } = renderWith(<MediaFigure media={IMG} toolUseId="t" context="x" />, null);
    expect(c3.querySelector('img')).toBeNull();
  });
  it('falls back to the badge + hint on img error (410/404/413 path)', () => {
    const { container } = renderWith(<MediaFigure media={IMG} toolUseId="t1" context="x" />);
    // Native error event flushed through act() so the synchronous re-render
    // lands before we assert (React's onError sets state; act flushes it).
    act(() => {
      container.querySelector('img')!.dispatchEvent(new Event('error'));
    });
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('source no longer available');
  });
  it('PDF document: badge + view-inline toggle; expands to an <object>, keeps open ↗', () => {
    const doc = { kind: 'document' as const, media_type: 'application/pdf', bytes: 4000, index: 0 };
    const { container } = renderWith(<MediaFigure media={doc} uuid="u1" context="attached" />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('object')).toBeNull(); // collapsed by default
    const toggle = container.querySelector('.conv-pdf-toggle') as HTMLButtonElement;
    expect(toggle).toBeTruthy();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    const obj = container.querySelector('object') as HTMLObjectElement;
    expect(obj.getAttribute('data')).toBe('/api/conversation/s1/media?uuid=u1&index=0');
    expect(obj.getAttribute('type')).toBe('application/pdf');
    expect(obj.querySelector('a')!.getAttribute('href')).toBe('/api/conversation/s1/media?uuid=u1&index=0'); // fallback child
    expect(container.querySelector('.conv-pdf-toggle')!.getAttribute('aria-expanded')).toBe('true');
    // collapse removes the <object>
    fireEvent.click(container.querySelector('.conv-pdf-toggle')!);
    expect(container.querySelector('object')).toBeNull();
  });
  it('non-PDF document stays a plain badge with no inline toggle', () => {
    const doc = { kind: 'document' as const, media_type: 'text/plain', bytes: 100, index: 0 };
    const { container } = renderWith(<MediaFigure media={doc} uuid="u1" context="attached" />);
    expect(container.querySelector('.conv-pdf-toggle')).toBeNull();
    expect(container.querySelector('a')!.getAttribute('href')).toBe('/api/conversation/s1/media?uuid=u1&index=0');
  });
  it('a failed PDF still renders only the badge (no inline toggle)', () => {
    // An unaddressable PDF (no session) falls to the badge branch — no toggle.
    const doc = { kind: 'document' as const, media_type: 'application/pdf', bytes: 4000, index: 0 };
    const { container } = renderWith(<MediaFigure media={doc} uuid="u1" context="x" />, null);
    expect(container.querySelector('.conv-pdf-toggle')).toBeNull();
    expect(container.querySelector('object')).toBeNull();
  });
  it('builds a session-scoped media URL (cross-session)', () => {
    const doc = { kind: 'document' as const, media_type: 'application/pdf', bytes: 4000, index: 0 };
    const { container: c1 } = renderWith(<MediaFigure media={doc} uuid="u1" context="x" />, 's1');
    const { container: c2 } = renderWith(<MediaFigure media={doc} uuid="u1" context="x" />, 's2');
    // open-↗ link reflects each session id
    expect(c1.querySelector('a')!.getAttribute('href')).toContain('/conversation/s1/');
    expect(c2.querySelector('a')!.getAttribute('href')).toContain('/conversation/s2/');
  });
});
