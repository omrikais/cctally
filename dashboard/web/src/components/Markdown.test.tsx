import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown } from './Markdown';
import { HighlightContext } from '../conversations/HighlightContext';

describe('Markdown', () => {
  it('renders gfm tables', () => {
    const md = '| a | b |\n| - | - |\n| 1 | 2 |';
    const { container } = render(<Markdown>{md}</Markdown>);
    expect(container.querySelector('table')).not.toBeNull();
    expect(container.querySelectorAll('td')).toHaveLength(2);
  });

  it('renders gfm strikethrough', () => {
    const { container } = render(<Markdown>{'~~gone~~'}</Markdown>);
    expect(container.querySelector('del')).not.toBeNull();
  });

  it('escapes raw HTML (no rehype-raw)', () => {
    const { container } = render(<Markdown>{'<script>alert(1)</script> and <b>x</b>'}</Markdown>);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toContain('<script>alert(1)</script>');
  });

  it('opens links in a new tab with safe rel', () => {
    const { container } = render(<Markdown>{'[x](https://example.com)'}</Markdown>);
    const a = container.querySelector('a')!;
    expect(a.getAttribute('target')).toBe('_blank');
    expect(a.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('renders a no-language fence as a plain <pre><code> block (no codeblock chrome)', () => {
    const { container } = render(<Markdown>{'```\nconst x = 1;\n```'}</Markdown>);
    expect(container.querySelector('pre code')).not.toBeNull();
    expect(container.querySelector('.codeblock')).toBeNull();
    expect(container.querySelectorAll('pre').length).toBe(1); // no <pre><pre> nesting
    expect(container.textContent).toContain('const x = 1;');
  });

  it('renders a registered-language fence through CodeBlock chrome', () => {
    const { container } = render(<Markdown>{'```ts\nconst x = 1;\n```'}</Markdown>);
    expect(container.querySelector('.codeblock')).not.toBeNull();
    expect(container.querySelectorAll('pre').length).toBe(1); // no <pre><pre> nesting
  });

  // ---- #177 S6: find-term <mark> highlighting via HighlightContext ----

  function renderWithTerms(terms: string[] | null, md: string, caseSensitive = false) {
    return render(
      <HighlightContext.Provider value={terms ? { terms, caseSensitive } : null}>
        <Markdown>{md}</Markdown>
      </HighlightContext.Provider>,
    );
  }

  it('wraps matching terms in <mark> in prose', () => {
    const { container } = renderWithTerms(['flock'], 'the flock serializes writers');
    const marks = container.querySelectorAll('mark');
    expect(marks).toHaveLength(1);
    expect(marks[0].textContent).toBe('flock');
  });

  it('is case-insensitive by default', () => {
    const { container } = renderWithTerms(['flock'], 'The FLOCK and the Flock');
    const marks = Array.from(container.querySelectorAll('mark')).map((m) => m.textContent);
    expect(marks).toEqual(['FLOCK', 'Flock']);
  });

  it('honors the case-sensitive flag (#217 S4)', () => {
    // Only the exact-case "Flock" is marked; "FLOCK" is not.
    const { container } = renderWithTerms(['Flock'], 'The FLOCK and the Flock', true);
    const marks = Array.from(container.querySelectorAll('mark')).map((m) => m.textContent);
    expect(marks).toEqual(['Flock']);
  });

  it('marks every term in a multi-term query', () => {
    const { container } = renderWithTerms(['npm', 'build'], 'run npm build now');
    const marks = Array.from(container.querySelectorAll('mark')).map((m) => m.textContent);
    expect(marks).toContain('npm');
    expect(marks).toContain('build');
  });

  it('does NOT mark terms inside a fenced code block', () => {
    const { container } = renderWithTerms(['flock'], '```\nflock here\n```');
    expect(container.querySelector('pre')).not.toBeNull();
    expect(container.querySelector('mark')).toBeNull();
  });

  it('does NOT mark terms inside inline code', () => {
    const { container } = renderWithTerms(['flock'], 'use the `flock` call');
    expect(container.querySelector('code')).not.toBeNull();
    expect(container.querySelector('mark')).toBeNull();
  });

  it('null context is a zero-overhead passthrough (no marks)', () => {
    const { container } = renderWithTerms(null, 'the flock serializes writers');
    expect(container.querySelector('mark')).toBeNull();
    expect(container.textContent).toContain('the flock serializes writers');
  });

  it('empty term list is a passthrough (no marks)', () => {
    const { container } = renderWithTerms([], 'the flock serializes writers');
    expect(container.querySelector('mark')).toBeNull();
  });
});
