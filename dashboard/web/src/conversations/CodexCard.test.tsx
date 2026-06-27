import { describe, expect, it } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { CodexCard } from './CodexCard';
import { TranscriptContext } from './TranscriptContext';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

const okEnvelope = JSON.stringify({ threadId: '019f-aaaa-bbbb-cccc-dddd-d760', content: '**Findings**\n\nP0: boom' });

const call = (over: Partial<Call>): Call =>
  ({
    kind: 'tool_call',
    name: 'mcp__codex__codex',
    input_summary: '{}',
    input: {
      prompt: 'You are doing a PRE-PLAN review.\n\nSecond line.',
      model: 'gpt-5.2-codex',
      config: { model_reasoning_effort: 'high' },
      sandbox: 'read-only',
      'approval-policy': 'never',
      cwd: '/a/b/fix-239',
    },
    preview: 'You are doing a PRE-PLAN review.',
    tool_use_id: 't1',
    result: { text: okEnvelope, truncated: false, is_error: false },
    ...over,
  }) as Call;

function withSession(node: React.ReactElement, sessionId = 's1') {
  return render(<TranscriptContext.Provider value={{ sessionId }}>{node}</TranscriptContext.Provider>);
}

describe('CodexCard', () => {
  it('is collapsed by default (no open attribute) with a rich summary', () => {
    const { container } = withSession(<CodexCard call={call({})} />);
    const details = container.querySelector('details')!;
    expect(details.hasAttribute('open')).toBe(false);
    expect(container.querySelector('.conv-codex-brand')!.textContent).toBe('codex');
    expect(container.textContent).toContain('gpt-5.2-codex');
    expect(container.textContent).toContain('✓ ok');
    expect(container.querySelector('.conv-chip-preview')!.textContent).toContain('PRE-PLAN review');
  });

  it('renders the response content as Markdown (a heading element, not raw JSON)', () => {
    const { container } = withSession(<CodexCard call={call({})} />);
    expect(container.querySelector('.conv-codex-md strong')!.textContent).toBe('Findings');
    expect(container.textContent).not.toContain('threadId');
  });

  it('expands the prompt to Markdown on click', () => {
    const { container } = withSession(<CodexCard call={call({})} />);
    fireEvent.click(screen.getByText(/prompt/));
    expect(container.querySelector('.conv-codex-prompt-md')).not.toBeNull();
  });

  it('renders the dedicated error block for an error envelope', () => {
    const errEnvelope = JSON.stringify({ type: 'error', status: 400, error: { type: 'invalid_request_error', message: 'model not supported' } });
    const { container } = withSession(<CodexCard call={call({ result: { text: errEnvelope, truncated: false, is_error: true } })} />);
    expect(container.querySelector('.conv-codex--error')).not.toBeNull();
    expect(container.querySelector('.conv-codex-error-msg')!.textContent).toContain('model not supported');
    expect(container.querySelector('.conv-chip-preview') && container.textContent).toContain('✗ 400');
  });

  it('treats an is_error result with a non-envelope body as an error', () => {
    const { container } = withSession(<CodexCard call={call({ result: { text: 'boom (not json)', truncated: false, is_error: true } })} />);
    expect(container.querySelector('.conv-codex--error')).not.toBeNull();
    expect(container.querySelector('.conv-codex-error-msg')!.textContent).toContain('boom (not json)');
  });

  it('shows "no result" for a request-only call (result null)', () => {
    const { container } = withSession(<CodexCard call={call({ result: null })} />);
    expect(container.textContent).toContain('no result');
  });

  it('shows a thread chip and no model pill for codex-reply', () => {
    const { container } = withSession(
      <CodexCard call={call({ name: 'mcp__codex__codex-reply', input: { prompt: 'follow up', threadId: '019ed760' } })} />,
    );
    expect(container.querySelector('.conv-codex-thread')!.textContent).toContain('d760');
    expect(container.querySelector('.conv-codex-model')).toBeNull();
  });

  it('clamps a long response and reveals it on "show full"', () => {
    const long = JSON.stringify({ threadId: 't', content: 'para\n'.repeat(40) });
    const { container } = withSession(<CodexCard call={call({ result: { text: long, truncated: false, is_error: false } })} />);
    expect(container.querySelector('.conv-codex-md--clamp')).not.toBeNull();
    fireEvent.click(screen.getByText(/show full response/i));
    expect(container.querySelector('.conv-codex-md--clamp')).toBeNull();
  });

  it('offers LoadFull when the result is truncated', () => {
    const cut = '{"threadId":"t","content":"truncated mid';
    const { container } = withSession(
      <CodexCard call={call({ result: { text: cut, truncated: true, full_length: 99999, is_error: false } })} />,
    );
    expect(container.querySelector('.conv-loadfull')).not.toBeNull();
  });

  it('renders file:line citations as chips (not anchors) and http links as anchors', () => {
    const content = 'see [spec:69](</abs/path:69>) and [docs](https://x.com)';
    const { container } = withSession(<CodexCard call={call({ result: { text: JSON.stringify({ threadId: 't', content }), truncated: false, is_error: false } })} />);
    expect(container.querySelector('.conv-codex-cite')!.textContent).toBe('spec:69');
    const link = screen.getByText('docs') as HTMLAnchorElement;
    expect(link.tagName).toBe('A');
    expect(link.target).toBe('_blank');
  });
});
