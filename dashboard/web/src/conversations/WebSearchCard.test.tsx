import { describe, expect, it } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { WebSearchCard } from './WebSearchCard';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

const links = (n: number) =>
  Array.from({ length: n }, (_, i) => ({ title: `Result ${i}`, url: `https://ex${i}.com/p` }));

const call = (over: Partial<Call>): Call =>
  ({
    kind: 'tool_call',
    name: 'WebSearch',
    input_summary: '{}',
    input: { query: 'best cli usage tracker' },
    preview: 'best cli',
    tool_use_id: 't1',
    result: { text: 'plain search result text', truncated: false, is_error: false },
    ...over,
  }) as Call;

describe('WebSearchCard', () => {
  it('shows the quoted query in the header', () => {
    const { container } = render(<WebSearchCard call={call({})} />);
    expect(container.querySelector('.conv-web-domain')!.textContent).toContain('best cli usage tracker');
  });

  it('renders 10 links then a "+ N more" expander that reveals the rest', () => {
    const { container } = render(<WebSearchCard call={call({ web_search: { query: 'q', links: links(12) } })} />);
    expect(container.querySelectorAll('.conv-web-link')).toHaveLength(10);
    const status = container.querySelector('.conv-web-status')!;
    expect(status.textContent).toContain('12');
    fireEvent.click(screen.getByRole('button', { name: /\+ 2 more results/i }));
    expect(container.querySelectorAll('.conv-web-link')).toHaveLength(12);
  });

  it('link titles are anchors for http(s) and plain text for javascript: (Codex F6)', () => {
    const { container } = render(<WebSearchCard call={call({ web_search: { query: 'q', links: [
      { title: 'Safe', url: 'https://safe.com/x' },
      { title: 'Evil', url: 'javascript:alert(1)' },
    ] } })} />);
    const rows = container.querySelectorAll('.conv-web-link');
    expect(rows[0].querySelector('a')!.getAttribute('href')).toBe('https://safe.com/x');
    expect(rows[0].querySelector('a')!.getAttribute('rel')).toBe('noopener noreferrer');
    expect(rows[1].querySelector('a')).toBeNull();
    expect(rows[1].textContent).toContain('Evil');
  });

  it('shows a "+" suffix on the count chip when links_truncated', () => {
    const { container } = render(
      <WebSearchCard call={call({ web_search: { query: 'q', links: links(50), links_truncated: true } })} />,
    );
    expect(container.querySelector('.conv-web-status')!.textContent).toMatch(/50\+\s*results/);
  });

  it('falls back to the plain text panel when web_search is absent', () => {
    const { container } = render(<WebSearchCard call={call({})} />);
    expect(container.querySelector('.conv-web-link')).toBeNull();
    expect(container.querySelector('pre.conv-code--result')!.textContent).toBe('plain search result text');
  });

  it('falls back to the plain text panel when links is empty', () => {
    const { container } = render(<WebSearchCard call={call({ web_search: { query: 'q', links: [] } })} />);
    expect(container.querySelector('.conv-web-link')).toBeNull();
    expect(container.querySelector('pre.conv-code--result')!.textContent).toBe('plain search result text');
  });

  it('is a <details open> so the [ / ] collapse-all sweep reaches it', () => {
    const { container } = render(<WebSearchCard call={call({})} />);
    const d = container.querySelector('details.conv-web') as HTMLDetailsElement;
    expect(d.tagName.toLowerCase()).toBe('details');
    expect(d.open).toBe(true);
  });
});
