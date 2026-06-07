import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderSnippet } from './snippet';

describe('renderSnippet', () => {
  it('wraps [..] FTS markers in <mark>', () => {
    const { container } = render(<>{renderSnippet('the [flock] serializes')}</>);
    const marks = container.querySelectorAll('mark');
    expect(marks).toHaveLength(1);
    expect(marks[0].textContent).toBe('flock');
    expect(container.textContent).toBe('the flock serializes');
  });

  it('handles multiple markers', () => {
    const { container } = render(<>{renderSnippet('[a] x [b]')}</>);
    expect(container.querySelectorAll('mark')).toHaveLength(2);
    expect(container.textContent).toBe('a x b');
  });

  it('passes plain (LIKE) snippets through unchanged', () => {
    const { container } = render(<>{renderSnippet('… some context here …')}</>);
    expect(container.querySelectorAll('mark')).toHaveLength(0);
    expect(container.textContent).toBe('… some context here …');
  });

  it('does not interpret HTML — angle brackets are inert text', () => {
    const { container } = render(<>{renderSnippet('a <script>x</script> [b]')}</>);
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).toContain('<script>x</script>');
    expect(container.querySelectorAll('mark')).toHaveLength(1);
  });

  it('tolerates an unclosed marker (renders the remainder as text)', () => {
    const { container } = render(<>{renderSnippet('open [unterminated')}</>);
    expect(container.textContent).toBe('open [unterminated');
  });
});
