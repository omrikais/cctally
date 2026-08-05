import { describe, expect, it } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { WebSearchCard } from './WebSearchCard';
import { ExactFindContext, HighlightContext } from './HighlightContext';
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
  it('uses the shared ok outcome wording for a native Codex completion', () => {
    const { container } = render(<WebSearchCard call={call({
      native_card: {
        schema_version: 1, type: 'web_search', source: 'web_search_call',
        call_status: 'completed', query: 'best cli usage tracker', action: {},
        completion: { status: 'returned', query: 'best cli usage tracker', action: {}, results: [] },
      },
      web_search: { query: 'best cli usage tracker', links: [] },
    })} />);
    expect(container.querySelector('.conv-outcome')?.textContent).toContain('ok');
    expect(container.querySelector('.conv-chip-status')).toBeNull();
  });

  it('uses the shared error outcome wording for a failed native Codex completion', () => {
    const { container } = render(<WebSearchCard call={call({
      native_card: {
        schema_version: 1, type: 'web_search', source: 'web_search_call',
        call_status: 'completed', query: 'best cli usage tracker', action: {},
        completion: { status: 'error', query: 'best cli usage tracker', action: {}, results: [], error: 'boom' },
      },
      web_search: { query: 'best cli usage tracker', links: [] },
      result: { text: 'boom', truncated: false, is_error: true },
    })} />);
    expect(container.querySelector('.conv-outcome')?.textContent).toContain('error');
    expect(container.querySelector('.conv-chip-status')).toBeNull();
  });

  it('maps a completion fragment onto the exact rendered result snippet', () => {
    const { container } = render(
      <ExactFindContext.Provider value={{
        selectedOccurrenceId: 'occ-web',
        occurrences: [{
          occurrence_id: 'occ-web', item_key: 'item', uuid: 'item',
          block_key: 'cbk.web-event', container_block_key: 'cbk.web', surface: 'completion',
          match_kinds: ['tool'], disclosure: ['cbk.web'],
          fragments: [{ leaf_key: 'results.0.snippet', start: 0, end: 6 }],
        }],
      }}>
        <WebSearchCard call={call({
          block_key: 'cbk.web',
          web_search: { query: 'q', links: [{ title: 'Result', url: 'https://example.test', snippet: 'needle web' }] },
        })} />
      </ExactFindContext.Provider>,
    );
    expect(container.querySelector('.conv-web-link-snippet mark')?.textContent).toBe('needle');
    expect(container.querySelector('details')?.dataset.disclosureKey).toBe('cbk.web');
  });

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

  // #217 S3 E10#9 — a green "ok" count chip on an errored search reads as
  // success. On result.is_error the count chip drops the --ok class and takes
  // the neutral/error style; the `· error` span still renders.
  it('the count chip is green (--ok) on a successful search', () => {
    const { container } = render(
      <WebSearchCard call={call({ web_search: { query: 'q', links: links(3) }, result: { text: '', truncated: false, is_error: false } })} />,
    );
    const status = container.querySelector('.conv-web-status')!;
    expect(status.classList.contains('conv-web-status--ok')).toBe(true);
    expect(status.classList.contains('conv-web-status--err')).toBe(false);
  });

  it('the count chip drops --ok and takes the error style when result.is_error', () => {
    const { container } = render(
      <WebSearchCard call={call({ web_search: { query: 'q', links: links(3) }, result: { text: 'boom', truncated: false, is_error: true } })} />,
    );
    const status = container.querySelector('.conv-web-status')!;
    expect(status.classList.contains('conv-web-status--ok')).toBe(false);
    expect(status.classList.contains('conv-web-status--err')).toBe(true);
    // The `· error` span still renders alongside the count.
    expect(container.querySelector('.conv-chip-status')!.textContent).toContain('error');
  });

  // #236 — the plain-text fallback panel highlights find matches when find is on.
  it('marks find terms in the plain fallback panel', () => {
    const { container } = render(
      <HighlightContext.Provider value={{ kind: 'terms', terms: ['flock'], caseSensitive: false }}>
        <WebSearchCard call={call({ result: { text: 'found flock in the result', truncated: false, is_error: false } })} />
      </HighlightContext.Provider>,
    );
    expect(container.querySelector('pre.conv-code--result mark')?.textContent).toBe('flock');
  });
});
