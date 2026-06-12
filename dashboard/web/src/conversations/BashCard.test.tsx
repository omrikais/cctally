import { render, fireEvent, act } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BashCard } from './BashCard';
import { TranscriptContext } from './TranscriptContext';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// Bash-shaped tool_call builder (base()/over convention).
const base = (over: Partial<Call> = {}): Call =>
  ({
    kind: 'tool_call',
    name: 'Bash',
    input_summary: '{}',
    preview: 'ls',
    tool_use_id: 'b1',
    input: { command: 'ls -la' },
    result: { text: 'out\nboom', truncated: false, is_error: true },
    stderr: 'boom',
    interrupted: false,
    ...over,
  }) as Call;

function renderCard(call: Call, sessionId: string | null = 's1') {
  return render(
    <TranscriptContext.Provider value={{ sessionId }}>
      <BashCard call={call} />
    </TranscriptContext.Provider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('BashCard', () => {
  it('renders the command as $ <command>, bash-highlighted', () => {
    const { container } = renderCard(base());
    const cmd = container.querySelector('.conv-term-cmd');
    expect(cmd?.textContent).toContain('$');
    expect(cmd?.textContent).toContain('ls -la');
  });

  it('splits stderr into a red block and badges error', () => {
    const { container } = renderCard(base());
    const stderr = container.querySelector('.conv-term-stderr');
    expect(stderr?.textContent).toContain('boom');
    // stdout portion = result.text minus the trailing stderr suffix.
    expect(container.querySelector('.conv-term-out')?.textContent).toContain('out');
    expect(container.querySelector('.conv-term-out')?.textContent).not.toContain('boom');
    expect(container.querySelector('.conv-term-badge--err')).toBeTruthy();
  });

  it('legacy row without stderr renders merged output, no stderr block', () => {
    const { container } = renderCard(
      base({ stderr: undefined, result: { text: 'just out', truncated: false, is_error: false } }),
    );
    expect(container.querySelector('.conv-term-stderr')).toBeNull();
    expect(container.querySelector('.conv-term-out')?.textContent).toContain('just out');
    expect(container.querySelector('.conv-term-badge--err')).toBeNull();
  });

  it('stderr present but not a suffix of result.text → merged output (guarded fallback)', () => {
    const { container } = renderCard(
      base({ stderr: 'XYZ', result: { text: 'out only', truncated: false, is_error: false } }),
    );
    // No suffix match → no split; render whole text, no red stderr block.
    expect(container.querySelector('.conv-term-stderr')).toBeNull();
    expect(container.querySelector('.conv-term-out')?.textContent).toContain('out only');
  });

  it('interrupted → interrupted badge', () => {
    const { container } = renderCard(
      base({ stderr: undefined, interrupted: true, result: { text: '', truncated: false, is_error: false } }),
    );
    expect(container.querySelector('.conv-term-badge--int')).toBeTruthy();
  });

  it('request-only (result === null) → command only, no terminal body', () => {
    const { container } = renderCard(base({ result: null, stderr: undefined }));
    expect(container.querySelector('.conv-term-cmd')).toBeTruthy();
    expect(container.querySelector('.conv-term-out')).toBeNull();
    expect(container.querySelector('.conv-term-stderr')).toBeNull();
  });

  it('result.truncated → load-full output affordance that re-renders', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        which: 'result',
        tool_use_id: 'b1',
        text: 'FULL OUTPUT',
        full_length: 99999,
        truncated: false,
        is_error: false,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const call = base({
      stderr: undefined,
      result: { text: 'partial', truncated: true, is_error: false, full_length: 99999 },
    });
    const { container, getByRole } = renderCard(call);
    const btn = getByRole('button', { name: /load full output/i });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(container.querySelector('.conv-term-out')?.textContent).toContain('FULL OUTPUT');
  });

  it('load-full preserves the stderr split from the loaded payload', async () => {
    // The truncated result carries discrete stderr in its full payload. After
    // load-full, the red stderr block must persist AND the stdout block must NOT
    // contain the stderr suffix — re-split against the LOADED stderr, not null.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        which: 'result',
        tool_use_id: 'b1',
        text: 'OUT\nERRTAIL',
        stderr: 'ERRTAIL',
        full_length: 12345,
        truncated: false,
        is_error: true,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const call = base({
      stderr: 'partialerr',
      result: { text: 'partial', truncated: true, is_error: true, full_length: 12345 },
    });
    const { container, getByRole } = renderCard(call);
    const btn = getByRole('button', { name: /load full output/i });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // Red stderr block still renders the loaded stderr text.
    const stderr = container.querySelector('.conv-term-stderr');
    expect(stderr?.textContent).toContain('ERRTAIL');
    // stdout block carries only the stdout portion, NOT the stderr suffix.
    const out = container.querySelector('.conv-term-out');
    expect(out?.textContent).toContain('OUT');
    expect(out?.textContent).not.toContain('ERRTAIL');
  });
});
