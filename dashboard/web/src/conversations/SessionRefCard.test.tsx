import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SessionRefCard } from './SessionRefCard';
import { TranscriptContext } from './TranscriptContext';
import type { ConversationBlock, ConversationSessionIndex } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;
type SessionRefCardShape = Extract<NonNullable<Call['native_card']>, { type: 'session_ref' }>;

const call = (card: Partial<SessionRefCardShape> = {}, over: Partial<Call> = {}): Call => ({
  kind: 'tool_call', name: 'write_stdin', input_summary: '{}', input: null,
  preview: 'yes', tool_use_id: 'cbk.session', payload_capable: true,
  result: { text: 'ok\n', truncated: false, is_error: false },
  native_card: {
    schema_version: 1, type: 'session_ref', scope: 'shell', ref: '1',
    operation: 'write', chars: 'yes\n', truncated: false, ...card,
  },
  ...over,
} as Call);

describe('SessionRefCard (#463 S3 §5.3/§5.4)', () => {
  it('shows the conversation-local ordinal and the characters written', () => {
    const { container } = render(<SessionRefCard call={call()} />);
    expect(container.querySelector('.conv-chip-name')?.textContent).toBe('write_stdin');
    expect(container.textContent).toContain('session 1');
    expect(container.querySelector('.conv-session-chars')?.textContent).toContain('yes');
  });

  it('renders a cell reference as a cell and never as a shell session', () => {
    const { container } = render(<SessionRefCard call={call(
      { scope: 'cell', ref: '12', operation: 'poll', chars: null },
      { name: 'wait' },
    )} />);
    expect(container.textContent).toContain('cell 12');
    expect(container.textContent).not.toContain('session');
    expect(container.querySelector('.conv-session-chars')).toBeNull();
  });

  it('renders no reference at all when the server could not resolve one', () => {
    // §3.2 — the index registers a session only from a standalone write_stdin
    // row, so a session named inside a program body has no ordinal. Inventing
    // one, or falling back to a provider identifier, is forbidden.
    const { container } = render(<SessionRefCard call={call({ ref: null })} />);
    expect(container.querySelector('.conv-session-ref')).toBeNull();
    expect(container.textContent).not.toMatch(/session \d/);
  });

  it('states the operation in the reader\'s vocabulary', () => {
    const write = render(<SessionRefCard call={call()} />);
    expect(write.container.textContent).toContain('wrote to');
    const poll = render(<SessionRefCard call={call(
      { scope: 'cell', ref: '9', operation: 'poll', chars: null }, { name: 'wait' },
    )} />);
    expect(poll.container.textContent).toContain('polled');
  });
});

// #463 S3 §5.3 — the opener, and the three labels that must stay distinct so a
// reader is never told something is absent when it was merely not loaded.
describe('SessionRefCard — opener resolution from the server index', () => {
  const withIndex = (node: React.ReactElement, sessionIndex: ConversationSessionIndex) =>
    render(
      <TranscriptContext.Provider value={{ sessionId: 's1', sessionIndex }}>{node}</TranscriptContext.Provider>,
    );

  it('marks the call that started the session', () => {
    const { container } = withIndex(
      <SessionRefCard call={call({}, { tool_use_id: 'cbk.opener' })} />,
      { sessions: { '1': { ordinal: 1, opener_block_key: 'cbk.opener' } }, truncated: false },
    );
    expect(container.querySelector('.conv-session-opener')).toBeTruthy();
    expect(container.querySelector('.conv-session-opener')?.textContent).toContain('started');
  });

  it('does not mark a later call in the same session as its opener', () => {
    const { container } = withIndex(
      <SessionRefCard call={call({}, { tool_use_id: 'cbk.later' })} />,
      { sessions: { '1': { ordinal: 1, opener_block_key: 'cbk.opener' } }, truncated: false },
    );
    expect(container.querySelector('.conv-session-opener')).toBeNull();
    expect(container.querySelector('.conv-session-note')).toBeNull();
  });

  it('says the opener is not in the retained data rather than saying it is unknown', () => {
    const { container } = withIndex(
      <SessionRefCard call={call()} />,
      { sessions: { '1': { ordinal: 1, opener_block_key: null } }, truncated: false },
    );
    const note = container.querySelector('.conv-session-note')?.textContent ?? '';
    expect(note).toContain('opener not in this conversation’s retained data');
    expect(note).not.toContain('unknown');
  });

  it('says the index itself was truncated rather than claiming the opener is absent', () => {
    const { container } = withIndex(
      <SessionRefCard call={call()} />,
      { sessions: { '1': { ordinal: 1, opener_block_key: null } }, truncated: true },
    );
    const note = container.querySelector('.conv-session-note')?.textContent ?? '';
    expect(note).toContain('index was truncated');
    expect(note).not.toContain('retained data');
  });

  it('says nothing about an opener when no index was published at all', () => {
    const { container } = render(<SessionRefCard call={call()} />);
    expect(container.querySelector('.conv-session-note')).toBeNull();
    expect(container.querySelector('.conv-session-opener')).toBeNull();
  });

  it('never resolves an opener for a cell reference', () => {
    // The two namespaces have zero overlapping values, so a cell id must never
    // be looked up in the shell-session index.
    const { container } = withIndex(
      <SessionRefCard call={call({ scope: 'cell', ref: '1', operation: 'poll', chars: null }, { name: 'wait', tool_use_id: 'cbk.opener' })} />,
      { sessions: { '1': { ordinal: 1, opener_block_key: 'cbk.opener' } }, truncated: false },
    );
    expect(container.querySelector('.conv-session-opener')).toBeNull();
    expect(container.querySelector('.conv-session-note')).toBeNull();
  });
});

// §5.4 — every rigid element added to the summary row carries a short wording,
// selected by the shipped .conv-status-wide/.conv-status-narrow pair.
describe('SessionRefCard — the mobile wording pair', () => {
  it('emits both wordings for the session badge', () => {
    const { container } = render(<SessionRefCard call={call()} />);
    const badge = container.querySelector('.conv-session-ref');
    expect(badge?.querySelector('.conv-status-wide')?.textContent).toBe('session 1');
    expect(badge?.querySelector('.conv-status-narrow')?.textContent).toBe('s1');
  });

  it('emits both wordings for a cell badge', () => {
    const { container } = render(<SessionRefCard call={call({ scope: 'cell', ref: '12', operation: 'poll', chars: null })} />);
    const badge = container.querySelector('.conv-session-ref');
    expect(badge?.querySelector('.conv-status-wide')?.textContent).toBe('cell 12');
    expect(badge?.querySelector('.conv-status-narrow')?.textContent).toBe('c12');
  });

  it('emits both wordings for the operation label', () => {
    // Measured in-browser at 390px: `.conv-session-op` was the one new rigid
    // summary child shipping a single wording, and it starved the flexible
    // preview to about 21px — two characters — on a `write_stdin` row.
    const write = render(<SessionRefCard call={call()} />);
    const op = write.container.querySelector('.conv-session-op');
    expect(op?.querySelector('.conv-status-wide')?.textContent).toBe('wrote to');
    expect(op?.querySelector('.conv-status-narrow')?.textContent).toBe('wrote');

    const poll = render(<SessionRefCard call={call({ scope: 'cell', ref: '9', operation: 'poll', chars: null }, { name: 'wait' })} />);
    const pollOp = poll.container.querySelector('.conv-session-op');
    expect(pollOp?.querySelector('.conv-status-wide')?.textContent).toBe('polled');
    expect(pollOp?.querySelector('.conv-status-narrow')?.textContent).toBe('poll');
  });

  it('states the recovered exit code and wall time in the expanded body', () => {
    // #463 S3 F11a — the evidence behind the outcome word was reachable only
    // through a `title` tooltip, which a touch viewport cannot open.
    const { container } = render(<SessionRefCard call={call({}, {
      outcome: { status: 'completed', exit_code: 0, wall_time_seconds: 0.5 },
    })} />);
    expect(container.querySelector('.conv-session-body .conv-outcome-evidence')?.textContent)
      .toBe('exit 0 · 0.5s');
  });

  it('shows the outcome on the right, with an explicit state for unknown', () => {
    const known = render(<SessionRefCard call={call({}, {
      outcome: { status: 'failed', exit_code: 3, wall_time_seconds: 0.5 },
      result: { text: 'refused\n', truncated: false, is_error: true },
    })} />);
    expect(known.container.querySelector('.conv-outcome')?.textContent).toContain('error');

    const unknown = render(<SessionRefCard call={call({}, {
      outcome: { status: 'unknown', exit_code: null, wall_time_seconds: null },
    })} />);
    const badge = unknown.container.querySelector('.conv-outcome');
    expect(badge).toBeTruthy();
    expect(badge?.querySelector('.conv-status-wide')?.textContent).toContain('outcome unknown');
    expect(badge?.querySelector('.conv-status-narrow')?.textContent).toContain('unknown');
  });
});
