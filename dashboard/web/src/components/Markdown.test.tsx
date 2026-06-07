import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown } from './Markdown';

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

  it('renders fenced code as a plain <pre><code> block', () => {
    const { container } = render(<Markdown>{'```\nconst x = 1;\n```'}</Markdown>);
    expect(container.querySelector('pre code')).not.toBeNull();
    expect(container.textContent).toContain('const x = 1;');
  });
});
