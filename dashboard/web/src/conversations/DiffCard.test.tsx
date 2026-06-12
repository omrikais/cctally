import { render, fireEvent, act } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DiffCard } from './DiffCard';
import { TranscriptContext } from './TranscriptContext';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// Edit-shaped tool_call builder (the TodoWriteCard base()/over convention).
const base = (over: Partial<Call> = {}): Call =>
  ({
    kind: 'tool_call',
    name: 'Edit',
    input_summary: '{}',
    preview: '/a/cost.ts',
    tool_use_id: 'e1',
    input: { file_path: '/a/cost.ts', old_string: 'return x', new_string: 'return x + 1' },
    result: { text: 'updated', truncated: false, is_error: false },
    ...over,
  }) as Call;

// Render inside a TranscriptContext provider so the load-full affordance has a
// session id to address (#178). Without one useFullPayload would no-op.
function renderCard(call: Call, sessionId: string | null = 's1') {
  return render(
    <TranscriptContext.Provider value={{ sessionId }}>
      <DiffCard call={call} />
    </TranscriptContext.Provider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('DiffCard', () => {
  it('renders a unified diff with add/del rows and the +N −M header stat', () => {
    const { container } = renderCard(base());
    expect(container.querySelector('.conv-diff-row--add')).toBeTruthy();
    expect(container.querySelector('.conv-diff-row--del')).toBeTruthy();
    // +1 added line, −1 removed line.
    expect(container.querySelector('.conv-diff-stat')?.textContent).toMatch(/\+1/);
    expect(container.querySelector('.conv-diff-stat')?.textContent).toMatch(/−1|-1/);
  });

  it('header shows basename bold + dim parent dir', () => {
    const { container } = renderCard(base());
    expect(container.querySelector('.conv-diff-base')?.textContent).toBe('cost.ts');
    expect(container.querySelector('.conv-diff-dir')?.textContent).toContain('/a');
  });

  it('changed lines carry word-emphasis segments; context lines are syntax-highlighted', () => {
    // A context line ('keep') plus a changed line so both render paths fire.
    const call = base({
      input: {
        file_path: '/a/x.ts',
        old_string: 'const keep = 1;\nreturn x',
        new_string: 'const keep = 1;\nreturn x + 1',
      },
    });
    const { container } = renderCard(call);
    // Word-emphasis span lives on a changed row.
    expect(container.querySelector('.conv-diff-row--add .conv-diff-word')).toBeTruthy();
    // Context rows route through highlightBody → refractor token spans.
    expect(container.querySelector('.conv-diff-row--context .token')).toBeTruthy();
  });

  it('replace all → a "replace all" tag', () => {
    const { container } = renderCard(
      base({ input: { file_path: '/a/x.ts', old_string: 'a', new_string: 'b', replace_all: true } }),
    );
    expect(container.textContent).toMatch(/replace all/);
  });

  it('MultiEdit renders one hunk per edit under an "edit k of n" divider', () => {
    const call = base({
      name: 'MultiEdit',
      input: {
        file_path: '/a/x.ts',
        edits: [
          { old_string: 'a', new_string: 'b' },
          { old_string: 'c', new_string: 'd' },
        ],
      },
    });
    const { container } = renderCard(call);
    expect(container.querySelectorAll('.conv-diff-hunk').length).toBe(2);
    expect(container.textContent).toMatch(/edit 1 of 2/);
    expect(container.textContent).toMatch(/edit 2 of 2/);
    // The "2 edits" header tag.
    expect(container.textContent).toMatch(/2 edits/);
  });

  it('Write renders all-added "wrote N lines"', () => {
    const w = base({ name: 'Write', input: { file_path: '/n.ts', content: 'a\nb' } });
    const { container } = renderCard(w);
    expect(container.querySelectorAll('.conv-diff-row--del').length).toBe(0);
    expect(container.querySelectorAll('.conv-diff-row--add').length).toBe(2);
    expect(container.textContent).toMatch(/wrote 2 lines/);
  });

  it('renders the collapsed result · cat -n sub-panel when a result is present', () => {
    const call = base({
      result: { text: '   1\tline one\n   2\tline two', truncated: false, is_error: false },
    });
    const { container } = renderCard(call);
    const sub = container.querySelector('.conv-diff-result');
    expect(sub).toBeTruthy();
    // Collapsed by default (diff stays primary).
    expect((sub as HTMLDetailsElement).open).toBe(false);
    // cat -n form → LineNumberedCode gutter.
    expect(sub?.querySelector('.cb-gutter')).toBeTruthy();
  });

  it('no result → no sub-panel', () => {
    const { container } = renderCard(base({ result: null }));
    expect(container.querySelector('.conv-diff-result')).toBeNull();
  });

  it('no truncation affordance unless input_truncated', () => {
    const { container } = renderCard(base());
    expect(container.querySelector('.conv-loadfull')).toBeNull();
  });

  it('input_truncated → load-full affordance that fetches and recomputes the diff', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        which: 'input',
        tool_use_id: 'e1',
        input: { file_path: '/a/cost.ts', old_string: 'return x', new_string: 'return x + 99' },
        full_length: 9000,
        truncated: false,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const call = base({ input_summary: '{}', input_truncated: true });
    const { container, getByRole } = renderCard(call);
    const btn = getByRole('button', { name: /load full/i });
    expect(btn).toBeTruthy();
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // The recomputed diff reflects the full new_string ("+ 99").
    expect(container.textContent).toContain('+ 99');
  });

  it('input null is never passed here (guard lives in dispatch) — still renders without throwing', () => {
    // Defensive: even an empty input object must not throw (dispatch guards the
    // real null case; DiffCard assumes valid input per spec §4.1).
    const { container } = renderCard(base({ input: { file_path: '/x.ts', old_string: '', new_string: '' } }));
    expect(container.querySelector('.conv-diff-card')).toBeTruthy();
  });
});
