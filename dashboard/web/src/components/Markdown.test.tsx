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
      <HighlightContext.Provider value={terms ? { kind: 'terms', terms, caseSensitive } : null}>
        <Markdown>{md}</Markdown>
      </HighlightContext.Provider>,
    );
  }

  function renderWithRegex(source: string | null, md: string, caseSensitive = false) {
    return render(
      <HighlightContext.Provider value={source ? { kind: 'regex', source, caseSensitive } : null}>
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

  it('marks terms inside a fenced code block (registered language)', () => {
    // ```js\nconst flock = 1;\n``` with term "flock" — routed through CodeBlock,
    // which is highlight-aware as of #236.
    const { container } = renderWithTerms(['flock'], '```js\nconst flock = 1;\n```');
    expect(container.querySelector('mark')?.textContent).toBe('flock');
  });

  it('marks terms inside an unregistered / no-language fence', () => {
    const { container } = renderWithTerms(['flock'], '```\nplain flock here\n```');
    expect(container.querySelector('mark')?.textContent).toBe('flock');
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

  // ---- #223 item 2: best-effort regex <mark> highlighting ----
  it('wraps regex matches in <mark> in prose', () => {
    const { container } = renderWithRegex('ca.he', 'the cache layer');
    const marks = Array.from(container.querySelectorAll('mark')).map((m) => m.textContent);
    expect(marks).toEqual(['cache']);
  });

  it('regex is case-insensitive by default and case-sensitive when flagged', () => {
    const ci = renderWithRegex('cache', 'CACHE and cache');
    expect(Array.from(ci.container.querySelectorAll('mark')).map((m) => m.textContent)).toEqual(['CACHE', 'cache']);
    const cs = renderWithRegex('cache', 'CACHE and cache', true);
    expect(Array.from(cs.container.querySelectorAll('mark')).map((m) => m.textContent)).toEqual(['cache']);
  });

  it('marks multiple regex matches in one text node', () => {
    const { container } = renderWithRegex('\\d+', 'a1 b22 c333');
    expect(Array.from(container.querySelectorAll('mark')).map((m) => m.textContent)).toEqual(['1', '22', '333']);
  });

  it('invalid regex → no marks and no throw', () => {
    expect(() => renderWithRegex('(', 'whatever (text')).not.toThrow();
    const { container } = renderWithRegex('(', 'whatever (text');
    expect(container.querySelector('mark')).toBeNull();
  });

  it('zero-width regex → no hang, no empty <mark>', () => {
    const { container } = renderWithRegex('x*', 'abc');
    expect(container.querySelector('mark')).toBeNull();
  });

  it('does NOT mark regex inside inline code', () => {
    const { container } = renderWithRegex('cache', 'use the `cache` call');
    expect(container.querySelector('code')).not.toBeNull();
    expect(container.querySelector('mark')).toBeNull();
  });

  it('marks regex inside a fenced code block (registered language)', () => {
    const { container } = renderWithRegex('ca.he', '```js\nconst cache = 1;\n```');
    expect(container.querySelector('mark')?.textContent).toBe('cache');
  });

  it('marks regex inside an unregistered / no-language fence', () => {
    const { container } = renderWithRegex('ca.he', '```\nplain cache here\n```');
    expect(container.querySelector('mark')?.textContent).toBe('cache');
  });

  it('over-cap regex source → no marks (no-op)', () => {
    const longSource = 'a'.repeat(1001); // > FIND_REGEX_MAX_LEN (1000)
    const { container } = renderWithRegex(longSource, 'a'.repeat(1001));
    expect(container.querySelector('mark')).toBeNull();
  });

  it('over-cap text node → that node unmarked', () => {
    const big = 'x' + 'y'.repeat(200_001); // > FIND_SCAN_TEXT_CAP (200_000)
    const { container } = renderWithRegex('x', big);
    expect(container.querySelector('mark')).toBeNull();
  });

  // ---- #223 item 2: term-overlap regression (locks splitByTerms longest-first
  // before the makeMarkPlugin refactor) ----
  it('prefers the longer of two overlapping terms', () => {
    const { container } = renderWithTerms(['cache', 'cache.db'], 'open cache.db now');
    const marks = Array.from(container.querySelectorAll('mark')).map((m) => m.textContent);
    expect(marks).toEqual(['cache.db']);
  });
});
