import { describe, expect, it } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { WebFetchCard, domainOf, isHttpUrl } from './WebFetchCard';
import { TranscriptContext } from './TranscriptContext';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

const call = (over: Partial<Call>): Call =>
  ({
    kind: 'tool_call',
    name: 'WebFetch',
    input_summary: '{}',
    input: { url: 'https://ccusage.com/guide/codex/', prompt: 'summarize the codex flow' },
    preview: 'ccusage.com',
    tool_use_id: 't1',
    result: { text: 'A short summary.', truncated: false, is_error: false },
    ...over,
  }) as Call;

function withSession(node: React.ReactElement, sessionId = 's1') {
  return render(<TranscriptContext.Provider value={{ sessionId }}>{node}</TranscriptContext.Provider>);
}

describe('WebFetchCard helpers', () => {
  it('domainOf parses the hostname, empty on garbage', () => {
    expect(domainOf('https://ccusage.com/guide/codex/')).toBe('ccusage.com');
    expect(domainOf('not a url')).toBe('');
  });
  it('isHttpUrl accepts http(s) only', () => {
    expect(isHttpUrl('https://x.com')).toBe(true);
    expect(isHttpUrl('http://x.com')).toBe(true);
    expect(isHttpUrl('javascript:alert(1)')).toBe(false);
    expect(isHttpUrl('ftp://x.com')).toBe(false);
  });
});

describe('WebFetchCard', () => {
  it('shows the domain in the header + a green status chip when web_fetch is captured', () => {
    const { container } = withSession(
      <WebFetchCard call={call({ web_fetch: { code: 200, code_text: 'OK' } })} />,
    );
    expect(container.querySelector('.conv-web-domain')!.textContent).toBe('ccusage.com');
    const status = container.querySelector('.conv-web-status')!;
    expect(status.textContent).toContain('200');
    expect(status.textContent).toContain('OK');
    expect(status.classList.contains('conv-web-status--ok')).toBe(true);
  });

  it('renders a red status chip for a 4xx/5xx code', () => {
    const { container } = withSession(<WebFetchCard call={call({ web_fetch: { code: 404 } })} />);
    const status = container.querySelector('.conv-web-status')!;
    expect(status.textContent).toContain('404');
    expect(status.classList.contains('conv-web-status--err')).toBe(true);
  });

  it('renders no status chip when web_fetch is absent (old rows)', () => {
    const { container } = withSession(<WebFetchCard call={call({})} />);
    expect(container.querySelector('.conv-web-status')).toBeNull();
    // The domain is still shown — the card minus the chip (criterion 2).
    expect(container.querySelector('.conv-web-domain')!.textContent).toBe('ccusage.com');
  });

  it('renders the url field as an external link with the safe rel for https', () => {
    const { container } = withSession(<WebFetchCard call={call({})} />);
    const a = container.querySelector('.conv-web-field a')!;
    expect(a.getAttribute('href')).toBe('https://ccusage.com/guide/codex/');
    expect(a.getAttribute('target')).toBe('_blank');
    expect(a.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('renders a javascript: url as plain text, never an anchor (Codex F6)', () => {
    const { container } = withSession(
      <WebFetchCard call={call({ input: { url: 'javascript:alert(1)', prompt: 'p' } })} />,
    );
    const field = container.querySelector('.conv-web-field')!;
    expect(field.querySelector('a')).toBeNull();
    expect(field.textContent).toContain('javascript:alert(1)');
  });

  it('renders the prompt field', () => {
    const { container } = withSession(<WebFetchCard call={call({})} />);
    expect(container.textContent).toContain('summarize the codex flow');
  });

  it('clamps a long result and reveals it on "Show full summary"', () => {
    const long = Array.from({ length: 40 }, (_, i) => `line ${i}`).join('\n');
    const { container } = withSession(
      <WebFetchCard call={call({ result: { text: long, truncated: false, is_error: false } })} />,
    );
    expect(container.querySelector('.conv-web-md--clamp')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /show full summary/i }));
    expect(container.querySelector('.conv-web-md--clamp')).toBeNull();
  });

  it('shows the no-result note when result is null', () => {
    const { container } = withSession(<WebFetchCard call={call({ result: null })} />);
    expect(container.querySelector('.conv-tool-io-label--none')!.textContent).toContain('no result');
  });

  it('is a <details open> so the [ / ] collapse-all sweep reaches it', () => {
    const { container } = withSession(<WebFetchCard call={call({})} />);
    const d = container.querySelector('details.conv-web') as HTMLDetailsElement;
    expect(d.tagName.toLowerCase()).toBe('details');
    expect(d.open).toBe(true);
  });
});
