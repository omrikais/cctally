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
    expect(container.querySelector('.conv-codex-summary-status')!.textContent).toContain('✗ 400');
  });

  it('treats an is_error result with a non-envelope body as an error', () => {
    const { container } = withSession(<CodexCard call={call({ result: { text: 'boom (not json)', truncated: false, is_error: true } })} />);
    expect(container.querySelector('.conv-codex--error')).not.toBeNull();
    expect(container.querySelector('.conv-codex-error-msg')!.textContent).toContain('boom (not json)');
  });

  it('degrades a malformed non-error result to raw <pre> without erroring', () => {
    const { container } = withSession(<CodexCard call={call({ result: { text: 'not json at all', truncated: false, is_error: false } })} />);
    const pre = container.querySelector('pre.conv-code--result');
    expect(pre).not.toBeNull();
    expect(pre!.textContent).toContain('not json at all');
    expect(container.querySelector('.conv-codex--error')).toBeNull();
  });

  it('shows "no result" for a request-only call (result null)', () => {
    const { container } = withSession(<CodexCard call={call({ result: null })} />);
    expect(container.textContent).toContain('no result');
  });

  it('shows a thread chip and suppresses the model pill for codex-reply', () => {
    // Fixture carries BOTH threadId AND model so the null model pill proves the
    // threadId branch *suppresses* it (non-vacuous), not that model was absent.
    const { container } = withSession(
      <CodexCard call={call({ name: 'mcp__codex__codex-reply', input: { prompt: 'follow up', threadId: '019ed760', model: 'gpt-5.2-codex' } })} />,
    );
    expect(container.querySelector('.conv-codex-thread')!.textContent).toContain('d760');
    expect(container.querySelector('.conv-codex-model')).toBeNull();
  });

  it('hides the status bar for a bare codex-reply with no run metadata', () => {
    // A real codex-reply input is just prompt + threadId — no model/sandbox/etc.
    // The status bar would otherwise be a lone dot, so it must not render.
    const { container } = withSession(
      <CodexCard call={call({ name: 'mcp__codex__codex-reply', input: { prompt: 'follow up', threadId: '019ed760' } })} />,
    );
    expect(container.querySelector('.conv-codex-bar')).toBeNull();
    expect(container.querySelector('.conv-codex-thread')).not.toBeNull();
  });

  it('clamps a long response and reveals it on "show full"', () => {
    const long = JSON.stringify({ threadId: 't', content: 'para\n'.repeat(40) });
    const { container } = withSession(<CodexCard call={call({ result: { text: long, truncated: false, is_error: false } })} />);
    expect(container.querySelector('.conv-codex-md--clamp')).not.toBeNull();
    fireEvent.click(screen.getByText(/show full response/i));
    expect(container.querySelector('.conv-codex-md--clamp')).toBeNull();
  });

  it('round-trips expand and collapse', () => {
    // The control used to render only while clamped and only ever set expanded
    // true, so expanding unmounted the one control that could collapse it —
    // there was no way back to the clamped state.
    const long = JSON.stringify({ threadId: 't', content: 'para\n'.repeat(40) });
    const { container } = withSession(
      <CodexCard call={call({ result: { text: long, truncated: false, is_error: false } })} />,
    );
    const expand = screen.getByRole('button', { name: /show full response/i });
    expect(expand).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(expand);
    const collapse = screen.getByRole('button', { name: /collapse response/i });
    expect(collapse).toHaveAttribute('aria-expanded', 'true');
    expect(container.querySelector('.conv-codex-md--clamp')).toBeNull();
    fireEvent.click(collapse);
    expect(screen.getByRole('button', { name: /show full response/i })).toBeInTheDocument();
    expect(container.querySelector('.conv-codex-md--clamp')).not.toBeNull();
  });

  it('offers no expand control for a short response', () => {
    // Non-vacuity: decoupling the control from the clamp state must not make it
    // render on every card.
    const { container } = withSession(<CodexCard call={call({})} />);
    expect(container.querySelector('.conv-codex-more')).toBeNull();
  });

  it('offers LoadFull when the result is truncated', () => {
    const cut = '{"threadId":"t","content":"truncated mid';
    const { container } = withSession(
      <CodexCard call={call({ result: { text: cut, truncated: true, full_length: 99999, is_error: false } })} />,
    );
    expect(container.querySelector('.conv-loadfull')).not.toBeNull();
  });

  // ---- backgrounded-MCP calls (spec 2026-07-31 §5) --------------------------
  // The card reported '✓ ok' for ANY non-null non-error result, so a still-
  // running placeholder rendered as a completed call — false success for the
  // exact case the server deliberately leaves unrecovered.
  //
  // RECOVERED means `background_completed_at` is set, NOT
  // `background_status === 'completed'`: the server stamps the status from
  // whatever the notification claimed, and two unrecovered cases legitimately
  // claim "completed" (a completion carrying no <result>, and an ambiguity whose
  // candidates are all completed). Only a real join writes the timestamp.
  const placeholder =
    'MCP tool "codex/codex" is still running after 120s. It was moved to the ' +
    'background as task kravg1b9s and keeps running; you\'ll receive a ' +
    'notification with the result when it completes.';

  it('does not report ok for a call still running in the background', () => {
    const { container } = withSession(
      <CodexCard call={call({ background_status: 'running',
                              result: { text: placeholder, truncated: false, is_error: false } })} />,
    );
    expect(container.textContent).not.toContain('✓ ok');
    expect(screen.getByText(/running in background/i)).toBeInTheDocument();
  });

  it('does not report ok for a completed notification that carried no result', () => {
    // `background_status` says completed; no `background_completed_at`, so the
    // result is still the placeholder and the call was never recovered.
    const { container } = withSession(
      <CodexCard call={call({ background_status: 'completed',
                              result: { text: placeholder, truncated: false, is_error: false } })} />,
    );
    expect(container.textContent).not.toContain('✓ ok');
    expect(screen.getByText(/running in background/i)).toBeInTheDocument();
  });

  it('shows the completion marker for a recovered response', () => {
    const { container } = withSession(
      <CodexCard call={call({ background_status: 'completed',
                              background_completed_at: '2026-07-30T20:51:16.312Z',
                              result: { text: okEnvelope, truncated: false, is_error: false } })} />,
    );
    expect(container.textContent).toContain('✓ ok');
    expect(screen.getByText(/ran in background/i)).toBeInTheDocument();
    expect(screen.getByText(/20:51/)).toBeInTheDocument();
  });

  it('reports a FAILED background call as an error, not as in-flight', () => {
    // Error precedence over pending is the one stated behavioural property of
    // the ternary and had no coverage: reordering it would relabel a genuinely
    // failed background call "running in background" and hide the failure.
    const errEnvelope = JSON.stringify({ type: 'error', status: 502, error: { type: 'api_error', message: 'upstream died' } });
    const { container } = withSession(
      <CodexCard call={call({ background_status: 'running',
                              result: { text: errEnvelope, truncated: false, is_error: true } })} />,
    );
    expect(container.querySelector('.conv-codex-summary-status')!.textContent).toContain('✗ 502');
    expect(container.textContent).not.toContain('running in background');
    expect(container.querySelector('.conv-codex-summary-status--pending')).toBeNull();
    expect(container.querySelector('.conv-codex--error')).not.toBeNull();
    expect(container.querySelector('.conv-codex--pending')).toBeNull();
  });

  it('a recovered call that failed still reports the error, not ✓ ok', () => {
    const { container } = withSession(
      <CodexCard call={call({ background_status: 'completed',
                              background_completed_at: '2026-07-30T20:51:16.312Z',
                              result: { text: 'boom (not json)', truncated: false, is_error: true } })} />,
    );
    expect(container.querySelector('.conv-codex-summary-status')!.textContent).toContain('✗ error');
    expect(container.textContent).not.toContain('✓ ok');
    // The completion marker still rides along — the call DID come back.
    expect(screen.getByText(/ran in background/i)).toBeInTheDocument();
  });

  // The pending label is a RIGID summary child (152px measured at 390px), which
  // drove .conv-chip-name and .conv-chip-preview to 0px. It ships in two
  // lengths and CSS picks one per viewport; JSDOM cannot evaluate the @media
  // flip, so this pins the markup the flip needs and the browser gate verifies
  // the widths.
  it('ships the pending label in a wide and a narrow form', () => {
    const { container } = withSession(
      <CodexCard call={call({ background_status: 'running',
                              result: { text: placeholder, truncated: false, is_error: false } })} />,
    );
    const status = container.querySelector('.conv-codex-summary-status')!;
    expect(status.querySelector('.conv-status-wide')!.textContent).toBe('⋯ running in background');
    expect(status.querySelector('.conv-status-narrow')!.textContent).toBe('⋯ running');
    // The narrow form drops words, so the full text stays reachable on hover.
    expect(status.getAttribute('title')).toBe('⋯ running in background');
  });

  it('marks only pending cards for the narrow two-line summary', () => {
    const pending = withSession(
      <CodexCard call={call({ background_status: 'running',
                              result: { text: placeholder, truncated: false, is_error: false } })} />,
    );
    expect(pending.container.querySelector('details')).toHaveClass('conv-codex--pending');
    pending.unmount();

    const completed = withSession(<CodexCard call={call({})} />);
    expect(completed.container.querySelector('details')).not.toHaveClass('conv-codex--pending');
  });

  it('a status with nothing to shorten stays a single label', () => {
    // Non-vacuity: the two-length treatment must apply to the background labels
    // only — an ordinary '✓ ok' must not grow a duplicate text node.
    const { container } = withSession(<CodexCard call={call({})} />);
    const status = container.querySelector('.conv-codex-summary-status')!;
    expect(status.textContent).toBe('✓ ok');
    expect(status.querySelector('.conv-status-wide')).toBeNull();
    expect(status.querySelector('.conv-status-narrow')).toBeNull();
  });

  it('leaves an ordinary (non-background) call untouched', () => {
    // Non-vacuity guard: the pending branch must key on the background fields,
    // not fire for every card.
    const { container } = withSession(<CodexCard call={call({})} />);
    expect(container.textContent).toContain('✓ ok');
    expect(screen.queryByText(/in background/i)).toBeNull();
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
