import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { NativeProgramCard } from './NativeProgramCard';
import type { ConversationBlock, NativeProgramInvocation } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

const INVOCATIONS: NativeProgramInvocation[] = [
  { kind: 'command', command: 'ls -1', workdir: '/synthetic/root-a', metadata: { tty: true } },
  { kind: 'session', scope: 'shell', ref: '1', operation: 'write', chars: 'yes\n' },
  { kind: 'other', name: 'view_image' },
];

const call = (over: Partial<Call> = {}, card: Partial<Extract<NonNullable<Call['native_card']>, { type: 'program' }>> = {}): Call => ({
  kind: 'tool_call', name: 'exec', input_summary: 'program', input: null,
  preview: 'ls -1', tool_use_id: 'cbk.program', payload_capable: true,
  result: { text: 'ok\n', truncated: false, is_error: false },
  native_card: {
    schema_version: 1, type: 'program', title: null, complete: false,
    invocations: INVOCATIONS, truncated: false, ...card,
  },
  ...over,
} as Call);

describe('NativeProgramCard (#463 S3 §5.4)', () => {
  it('names the provider tool rather than relabelling it', () => {
    const { container } = render(<NativeProgramCard call={call()} />);
    expect(container.querySelector('.conv-chip-name')?.textContent).toBe('exec');
    expect(container.textContent).not.toContain('Bash');
  });

  it('renders every invocation by its own kind', () => {
    const { container } = render(<NativeProgramCard call={call()} />);
    const rows = container.querySelectorAll('.conv-program-invocation');
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain('ls -1');
    expect(rows[0].textContent).toContain('/synthetic/root-a');
    expect(rows[1].textContent).toContain('yes');
    expect(rows[2].textContent).toContain('view_image');
  });

  it('says a declined invocation names a tool and claims nothing about its arguments', () => {
    const { container } = render(<NativeProgramCard call={call({}, {
      invocations: [{ kind: 'other', name: 'view_image' }],
    })} />);
    expect(container.querySelector('.conv-program-invocation--other')?.textContent)
      .toContain('arguments not read');
  });

  it('never presents an incomplete program as the whole program', () => {
    const { container } = render(<NativeProgramCard call={call()} />);
    expect(container.querySelector('.conv-program-incomplete')).toBeTruthy();
    expect(container.querySelector('.conv-program-incomplete')?.textContent)
      .toContain('could not read');
  });

  it('says nothing about omissions when the whole body was recognized', () => {
    const { container } = render(<NativeProgramCard call={call({}, { complete: true })} />);
    expect(container.querySelector('.conv-program-incomplete')).toBeNull();
  });

  it('renders the collapsed preview the adapter already computed', () => {
    // The card used to recompute the same string from title + invocations. The
    // second copy drifted: a leading session invocation read "wrote to shell 1"
    // on the row and "session 1" in the body, for one invocation.
    const titled = render(<NativeProgramCard call={call({ preview: 'list the synthetic tree' })} />);
    expect(titled.container.querySelector('.conv-chip-preview')?.textContent)
      .toBe('list the synthetic tree');

    const untitled = render(<NativeProgramCard call={call({ preview: 'ls -1 +2 more' })} />);
    expect(untitled.container.querySelector('.conv-chip-preview')?.textContent)
      .toBe('ls -1 +2 more');
  });

  it('reports a capped invocation list', () => {
    const { container } = render(<NativeProgramCard call={call({}, { truncated: true })} />);
    expect(container.querySelector('.conv-program-truncated')).toBeTruthy();
  });

  it('states the recovered exit code and wall time in the expanded body', () => {
    // #463 S3 F11a — a `title` tooltip has no touch affordance, so the evidence
    // behind the outcome word must be reachable in the body as well.
    const { container } = render(<NativeProgramCard call={call({
      outcome: { status: 'failed', exit_code: 2, wall_time_seconds: 2 },
    })} />);
    expect(container.querySelector('.conv-program-body .conv-outcome-evidence')?.textContent)
      .toBe('exit 2 · 2s');
  });
});
