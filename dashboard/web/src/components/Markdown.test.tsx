import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown, MdLink } from './Markdown';
import { ExactFindContext, HighlightContext } from '../conversations/HighlightContext';

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

  it('renders one server occurrence across plain, bold, and plain leaves', () => {
    const occurrence = {
      occurrence_id: 'o1.abc', item_key: 'item', uuid: 'item', block_key: 'block',
      container_block_key: 'block', surface: 'body' as const, match_kinds: [],
      disclosure: [],
      fragments: [
        { leaf_key: 't0', start: 0, end: 1 },
        { leaf_key: 't1', start: 0, end: 1 },
        { leaf_key: 't2', start: 0, end: 1 },
      ],
    };
    const { container } = render(
      <ExactFindContext.Provider value={{ occurrences: [occurrence], selectedOccurrenceId: 'o1.abc' }}>
        <Markdown findBlockKey="block" findSurface="body">{'a**b**c'}</Markdown>
      </ExactFindContext.Provider>,
    );
    const marks = container.querySelectorAll('mark[data-find-occurrence-id="o1.abc"]');
    expect(marks).toHaveLength(3);
    expect(Array.from(marks).map((mark) => mark.textContent)).toEqual(['a', 'b', 'c']);
  });

  it('lands exact targets in a registered fence after syntax tokenization', () => {
    const occurrence = {
      occurrence_id: 'o1.fence', item_key: 'item', uuid: 'item', block_key: 'block',
      container_block_key: 'block', surface: 'body' as const, match_kinds: [],
      disclosure: [], fragments: [{ leaf_key: 't0', start: 6, end: 9 }],
    };
    const { container } = render(
      <ExactFindContext.Provider value={{ occurrences: [occurrence], selectedOccurrenceId: 'o1.fence' }}>
        <Markdown findBlockKey="block" findSurface="body">{'```ts\nconst hit = 1;\n```'}</Markdown>
      </ExactFindContext.Provider>,
    );
    const mark = container.querySelector('mark[data-find-occurrence-id="o1.fence"]');
    expect(mark?.textContent).toBe('hit');
    expect(container.querySelector('.codeblock')).not.toBeNull();
  });

  it('lands exact targets through GFM task, entity, autolink, and hard-break DOM leaves', () => {
    const occurrence = {
      occurrence_id: 'o1.gfm', item_key: 'item', uuid: 'item', block_key: 'block',
      container_block_key: 'block', surface: 'body' as const, match_kinds: [],
      disclosure: [], fragments: [{ leaf_key: 't1', start: 8, end: 12 }],
    };
    const { container } = render(
      <ExactFindContext.Provider value={{ occurrences: [occurrence], selectedOccurrenceId: 'o1.gfm' }}>
        <Markdown findBlockKey="block" findSurface="body">
          {'- [x] done &amp; <https://example.test>  \nnext'}
        </Markdown>
      </ExactFindContext.Provider>,
    );
    expect(container.querySelector('input[type="checkbox"]')).not.toBeNull();
    expect(container.querySelector('mark[data-find-occurrence-id="o1.gfm"]')?.textContent)
      .toBe('exam');
  });

  it('degrades local and placeholder image targets without mounting an image request', () => {
    const { container } = render(
      <Markdown>{'![private](/synthetic/private/screenshot.png) ![file](file:///Users/test/secret.png) ![placeholder](url:0)'}</Markdown>,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getAllByText('local screenshot unavailable')).toHaveLength(3);
    expect(container.innerHTML).not.toContain('/synthetic/private');
    expect(container.innerHTML).not.toContain('/Users/test');
    expect(container.innerHTML).not.toContain('url:0');
  });

  it('keeps ordinary remote Markdown images renderable', () => {
    const { container } = render(<Markdown>{'![chart](https://example.com/chart.png)'}</Markdown>);
    expect(container.querySelector('img')?.getAttribute('src')).toBe('https://example.com/chart.png');
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

describe('#463 S5 — non-http(s) targets are inert', () => {
  const inertLinks = [
    ['an absolute filesystem path', '/srv/example-project/SKILL.md'],
    ['a bare relative target', 'url'],
    ['a relative document path', 'docs/commands/dashboard.md'],
    ['a custom scheme', 'app://open/thing'],
    ['an in-page anchor', '#section-two'],
  ] as const;

  for (const [label, target] of inertLinks) {
    it(`renders no anchor for ${label}`, () => {
      const { container } = render(<Markdown>{`see [the doc](${target})`}</Markdown>);
      expect(container.querySelector('a')).toBeNull();
      expect(container.textContent).toContain('the doc');
    });

    it(`serializes ${label} into no attribute`, () => {
      const { container } = render(<Markdown>{`see [the doc](${target})`}</Markdown>);
      // Non-vacuity floor: the loop below asserts nothing at all if the render
      // produced no elements, so a Markdown component that silently rendered
      // nothing would pass this case.
      const elements = container.querySelectorAll('*');
      expect(elements.length, 'nothing rendered — this case cannot observe the defect').toBeGreaterThan(0);
      for (const el of elements) {
        for (const attr of Array.from(el.attributes)) {
          expect(attr.value, `${el.tagName}[${attr.name}] carries the destination`).not.toContain(target);
        }
      }
    });
  }

  it('still renders an anchor for https', () => {
    const { container } = render(<Markdown>{'see [the doc](https://example.com/x)'}</Markdown>);
    const a = container.querySelector('a');
    expect(a?.getAttribute('href')).toBe('https://example.com/x');
    expect(a?.getAttribute('target')).toBe('_blank');
    expect(a?.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('still renders an anchor for mailto', () => {
    const { container } = render(<Markdown>{'write to [us](mailto:a@example.com)'}</Markdown>);
    expect(container.querySelector('a')?.getAttribute('href')).toBe('mailto:a@example.com');
  });

  it('renders the unavailable note for a bare relative image target', () => {
    const { container } = render(<Markdown>{'![shot](url)'}</Markdown>);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('.conv-local-media-unavailable')).not.toBeNull();
  });

  it('still renders a remote image', () => {
    const { container } = render(<Markdown>{'![shot](https://example.com/a.png)'}</Markdown>);
    expect(container.querySelector('img')?.getAttribute('src')).toBe('https://example.com/a.png');
  });
});

describe('Markdown components override', () => {
  it('exports MdLink that opens in a new tab', () => {
    render(<MdLink href="https://x.com">x</MdLink>);
    const a = screen.getByText('x') as HTMLAnchorElement;
    expect(a.tagName).toBe('A');
    expect(a.target).toBe('_blank');
    expect(a.rel).toContain('noopener');
  });

  it('lets a caller override the link renderer', () => {
    render(
      <Markdown components={{ a: ({ children }) => <span data-testid="cite">{children}</span> }}>
        {'see [spec:69](</abs/path:69>)'}
      </Markdown>,
    );
    expect(screen.getByTestId('cite').textContent).toBe('spec:69');
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('overriding only `a` keeps fenced code blocks rendering as <pre>', () => {
    const { container } = render(
      <Markdown components={{ a: ({ children }) => <span>{children}</span> }}>
        {'```\nplain fence\n```'}
      </Markdown>,
    );
    expect(container.querySelector('pre')).not.toBeNull();
  });
});
