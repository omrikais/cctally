import { describe, expect, it } from 'vitest';
import { render, act } from '@testing-library/react';
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
  it('renders documents as a badge with an open link, never an img', () => {
    const doc = { kind: 'document' as const, media_type: 'application/pdf', bytes: 4000, index: 0 };
    const { container } = renderWith(<MediaFigure media={doc} uuid="u1" context="attached" />);
    expect(container.querySelector('img')).toBeNull();
    const a = container.querySelector('a')!;
    expect(a.getAttribute('href')).toBe('/api/conversation/s1/media?uuid=u1&index=0');
    expect(container.textContent).toContain('open');
  });
});
