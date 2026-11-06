import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { isSnippetParts, renderSnippet } from './snippet';

function marks(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('mark')).map((m) => m.textContent ?? '');
}

describe('renderSnippet', () => {
  // #1036 — the renderer used to bracket-parse `snippet`, so a transcript's
  // own `[b]` became a highlight and lost its brackets. Without structured
  // parts the snippet is literal text.
  it('renders a legacy-only snippet literally, brackets kept, no mark', () => {
    const { container } = render(<>{renderSnippet('a [b] c')}</>);
    expect(container.textContent).toBe('a [b] c');
    expect(marks(container)).toEqual([]);
  });

  it('marks only the parts the server flagged as FTS matches', () => {
    const parts = [
      { text: '[WARN] ', match: false },
      { text: 'harness', match: true },
      { text: '.claude', match: false },
    ];
    const { container } = render(<>{renderSnippet('[WARN] [harness].claude', parts)}</>);
    expect(container.textContent).toBe('[WARN] harness.claude');
    expect(marks(container)).toEqual(['harness']);
  });

  it('keeps adjacent matched parts as separate marks', () => {
    const parts = [{ text: 'a', match: true }, { text: 'b', match: true }];
    const { container } = render(<>{renderSnippet('[a][b]', parts)}</>);
    expect(marks(container)).toEqual(['a', 'b']);
  });

  it.each([
    ['not an array', 'nope'],
    ['an element without text', [{ match: true }]],
    ['a non-boolean match', [{ text: 'b', match: 'yes' }]],
    ['a non-object element', ['b']],
    ['an empty list', []],
  ])('falls back to the literal snippet for %s', (_label, parts) => {
    const { container } = render(<>{renderSnippet('a [b] c', parts)}</>);
    expect(container.textContent).toBe('a [b] c');
    expect(marks(container)).toEqual([]);
  });

  it('renders nothing for a null or missing snippet without parts', () => {
    expect(render(<>{renderSnippet(null)}</>).container.textContent).toBe('');
    expect(render(<>{renderSnippet(undefined)}</>).container.textContent).toBe('');
  });

  it('does not interpret HTML — angle brackets are inert text, in parts too', () => {
    const { container } = render(<>{renderSnippet('a <script>x</script> [b]', [
      { text: '<img src=x onerror=alert(1)> ', match: false },
      { text: '<b>bold</b>', match: true },
    ])}</>);
    expect(container.querySelector('script, img, b')).toBeNull();
    expect(container.textContent).toBe('<img src=x onerror=alert(1)> <b>bold</b>');
    expect(marks(container)).toEqual(['<b>bold</b>']);
    const legacy = render(<>{renderSnippet('a <script>x</script> [b]')}</>).container;
    expect(legacy.querySelector('script')).toBeNull();
    expect(legacy.textContent).toBe('a <script>x</script> [b]');
  });

  it('isSnippetParts accepts only a list of {text: string, match: boolean}', () => {
    expect(isSnippetParts([{ text: 'a', match: false }])).toBe(true);
    expect(isSnippetParts([])).toBe(true);
    expect(isSnippetParts([{ text: 'a' }])).toBe(false);
    expect(isSnippetParts([null])).toBe(false);
    expect(isSnippetParts({ text: 'a', match: true })).toBe(false);
  });
});
