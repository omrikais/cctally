import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FindBar } from './FindBar';
import { _resetForTests, dispatch, getState } from '../store/store';
import type { ConversationFindResult } from '../types/conversation';

// Drive the find hook by mocking fetch. Each render of FindBar mounts
// useConversationFind, which debounces (200ms) before fetching.
function mockFind(body: Partial<ConversationFindResult>) {
  const full: ConversationFindResult = {
    anchors: [], total: 0, anchors_truncated: false, mode: 'fts', search_depth: 'full', ...body,
  };
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, status: 200, json: async () => full } as Response);
}

// Type into the input and let the debounce + fetch settle.
async function typeNeedle(input: HTMLInputElement, value: string) {
  fireEvent.change(input, { target: { value } });
  await act(async () => { vi.advanceTimersByTime(250); });
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => {
  _resetForTests();
  globalThis.fetch = vi.fn();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('FindBar', () => {
  it('renders a search role pill with a 0 / 0 counter when empty', () => {
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    expect(screen.getByRole('search')).not.toBeNull();
    expect(screen.getByLabelText(/find in conversation/i)).not.toBeNull();
    expect(document.querySelector('.conv-findbar-count')!.textContent).toContain('0 / 0');
  });

  it('shows a 1-based counter after results land', async () => {
    mockFind({ anchors: [{ uuid: 'u1', match_kinds: ['tool'] }, { uuid: 'u2', match_kinds: [] }], total: 2 });
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    await typeNeedle(input, 'needle');
    // cursor starts at 0 → "1 / 2"
    expect(document.querySelector('.conv-findbar-count')!.textContent).toContain('1 / 2');
  });

  it('Enter steps to the next anchor and dispatches OPEN_CONVERSATION with the jump; expand_details reflects match_kinds', async () => {
    mockFind({ anchors: [{ uuid: 'u1', match_kinds: ['tool'] }, { uuid: 'u2', match_kinds: [] }], total: 2 });
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    await typeNeedle(input, 'needle');

    // Enter → cursor 0 → 1 → jumps to u2 (match_kinds empty → expand_details false).
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }); });
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'u2', expand_details: false });
    expect(document.querySelector('.conv-findbar-count')!.textContent).toContain('2 / 2');

    // Enter again wraps 1 → 0 → jumps to u1 (match_kinds ['tool'] → expand_details true).
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }); });
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'u1', expand_details: true });
    expect(document.querySelector('.conv-findbar-count')!.textContent).toContain('1 / 2');
  });

  it('Shift+Enter steps to the previous anchor (wraps backward from 0)', async () => {
    mockFind({ anchors: [{ uuid: 'u1', match_kinds: [] }, { uuid: 'u2', match_kinds: [] }, { uuid: 'u3', match_kinds: [] }], total: 3 });
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    await typeNeedle(input, 'needle');

    // Shift+Enter from cursor 0 wraps to the last (index 2 → u3).
    act(() => { fireEvent.keyDown(input, { key: 'Enter', shiftKey: true }); });
    expect(getState().conversationJump).toEqual({ session_id: 's1', uuid: 'u3', expand_details: false });
    expect(document.querySelector('.conv-findbar-count')!.textContent).toContain('3 / 3');
  });

  it('the next/prev buttons step too', async () => {
    mockFind({ anchors: [{ uuid: 'u1', match_kinds: [] }, { uuid: 'u2', match_kinds: [] }], total: 2 });
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    await typeNeedle(input, 'needle');
    fireEvent.click(screen.getByRole('button', { name: /next match/i }));
    expect(getState().conversationJump!.uuid).toBe('u2');
    fireEvent.click(screen.getByRole('button', { name: /previous match/i }));
    expect(getState().conversationJump!.uuid).toBe('u1');
  });

  it('Escape and the close button both invoke onClose and dispatch CLOSE_CONV_FIND', async () => {
    dispatch({ type: 'OPEN_CONV_FIND' });
    const onClose = vi.fn();
    render(<FindBar sessionId="s1" onClose={onClose} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    act(() => { fireEvent.keyDown(input, { key: 'Escape' }); });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(getState().convFindOpen).toBe(false);

    dispatch({ type: 'OPEN_CONV_FIND' });
    fireEvent.click(screen.getByRole('button', { name: /close find/i }));
    expect(onClose).toHaveBeenCalledTimes(2);
    expect(getState().convFindOpen).toBe(false);
  });

  it('shows the · first 500 note when truncated', async () => {
    const anchors = Array.from({ length: 5 }, (_, i) => ({ uuid: `u${i}`, match_kinds: [] as ('tool' | 'thinking')[] }));
    mockFind({ anchors, total: 700, anchors_truncated: true });
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    await typeNeedle(input, 'needle');
    expect(document.querySelector('.conv-findbar')!.textContent).toContain('first 500');
  });

  it('shows a basic-search hint in LIKE mode', async () => {
    mockFind({ anchors: [{ uuid: 'u1', match_kinds: [] }], total: 1, mode: 'like' });
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    await typeNeedle(input, 'needle');
    expect(document.querySelector('.conv-findbar')!.textContent!.toLowerCase()).toContain('basic search');
  });

  it('surfaces a "find failed" hint on a non-abort fetch failure (M4)', async () => {
    // A 500 makes fetchJson throw HttpError → the hook's non-abort catch sets
    // error. The bar must show the hint so 0 / 0 isn't read as "zero matches".
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      { ok: false, status: 500, json: async () => ({}) } as Response,
    );
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    await typeNeedle(input, 'needle');
    expect(document.querySelector('.conv-findbar')!.textContent!.toLowerCase()).toContain('find failed');
    // Still 0 / 0, but now disambiguated by the hint.
    expect(document.querySelector('.conv-findbar-count')!.textContent).toContain('0 / 0');
  });

  it('reports the debounced needle up via onTermsChange (for prose marks)', async () => {
    mockFind({ anchors: [], total: 0 });
    const onTermsChange = vi.fn();
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={onTermsChange} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    await typeNeedle(input, 'cache.db');
    expect(onTermsChange).toHaveBeenLastCalledWith('cache.db');
  });
});
