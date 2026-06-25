import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown } from '../components/Markdown';
import { CodeBlock, highlightBody } from './CodeBlock';
import { HighlightContext } from './HighlightContext';

const md = (s: string) => render(<Markdown>{s}</Markdown>).container;

describe('CodeBlock / Markdown code rendering', () => {
  it('inline code stays a bare <code> (no codeblock chrome)', () => {
    const c = md('hello `inline` world');
    expect(c.querySelector('.codeblock')).toBeNull();
    expect(c.querySelector('code')).toBeInTheDocument();
  });

  it('a language fence renders the language tag + token spans', () => {
    const c = md('```ts\nconst x = 1;\n```');
    expect(c.querySelector('.codeblock')).toBeInTheDocument();
    expect(c.textContent).toMatch(/typescript|ts/i);            // language tag
    expect(c.querySelector('.token')).toBeInTheDocument();      // refractor tokens
    expect(c.querySelectorAll('pre').length).toBe(1);           // NO <pre><pre>
  });

  it('a no-language fence renders exactly one plain <pre>', () => {
    const c = md('```\nplain text\n```');
    expect(c.querySelectorAll('pre').length).toBe(1);
    expect(c.querySelector('.token')).toBeNull();
  });

  it('an unknown language falls back to a plain <pre>, no tokens', () => {
    const c = md('```nope\nx\n```');
    expect(c.querySelectorAll('pre').length).toBe(1);
    expect(c.querySelector('.token')).toBeNull();
  });

  it('SECURITY: raw HTML in a fence stays escaped text, never markup', () => {
    const c = md('```html\n<script>alert(1)</script>\n```');
    expect(c.querySelector('script')).toBeNull();               // not injected
    expect(c.textContent).toContain('<script>alert(1)</script>');// escaped text
  });
});

describe('highlightBody (shared primitive)', () => {
  it('emits token spans for a registered language', () => {
    const { container } = render(<pre>{highlightBody('const x = 1;', 'ts')}</pre>);
    expect(container.querySelector('.token')).toBeInTheDocument();
  });

  it('degrades to raw text for an unknown language', () => {
    const { container } = render(<pre>{highlightBody('const x = 1;', 'nope')}</pre>);
    expect(container.querySelector('.token')).toBeNull();
    expect(container.textContent).toBe('const x = 1;');
  });
});

describe('CodeBlock find highlighting (#236)', () => {
  it('marks find terms in a registered-language block', () => {
    const { container } = render(
      <HighlightContext.Provider value={{ kind: 'terms', terms: ['flock'], caseSensitive: false }}>
        <CodeBlock lang="js" code={'const flock = 1;'} />
      </HighlightContext.Provider>,
    );
    expect(container.querySelector('mark')?.textContent).toBe('flock');
  });

  it('marks find terms in an unregistered-language block', () => {
    const { container } = render(
      <HighlightContext.Provider value={{ kind: 'terms', terms: ['flock'], caseSensitive: false }}>
        <CodeBlock lang="unknownlang" code={'plain flock text'} />
      </HighlightContext.Provider>,
    );
    expect(container.querySelector('mark')?.textContent).toBe('flock');
  });

  it('no context → no marks (unchanged)', () => {
    const { container } = render(<CodeBlock lang="js" code={'const flock = 1;'} />);
    expect(container.querySelector('mark')).toBeNull();
  });
});
