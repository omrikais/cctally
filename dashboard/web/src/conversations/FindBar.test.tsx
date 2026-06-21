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
  try { localStorage.clear(); } catch { /* jsdom always has it */ }
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

  // #217 S4 QA fix — Escape handled at the bar CONTAINER, so it closes find from
  // a focused BUTTON too (not only the input). The bug: Escape on a bar button
  // (Close / regex / case / prev / next) bubbled PAST the bar to the document
  // keydown listener and tore down the whole reader. The bar-level handler now
  // closes ONLY find and stopPropagation()s, regardless of which control held
  // focus. (The propagation half — that the reader is NOT torn down — is proven
  // at the integration level in ConversationsView.test.tsx scenario 15b.)
  it('Escape while focus is on a bar BUTTON closes find and stops propagation', () => {
    dispatch({ type: 'OPEN_CONV_FIND' });
    const onClose = vi.fn();
    // A document-level listener stands in for the ConversationsView global Esc
    // (the keymap dispatcher listens on `document`). The bar's stopPropagation
    // must keep the button's Escape from reaching it — proving the reader is not
    // torn down. The integration counterpart is ConversationsView.test 15b.
    const docEsc = vi.fn();
    const docListener = (e: KeyboardEvent) => { if (e.key === 'Escape') docEsc(); };
    document.addEventListener('keydown', docListener);
    try {
      render(<FindBar sessionId="s1" onClose={onClose} onTermsChange={() => {}} />);
      // Each bar control closes find when it holds focus and Escape is pressed,
      // and the document listener never sees it.
      for (const name of [/close find/i, /regular expression/i, /case-sensitive/i]) {
        dispatch({ type: 'OPEN_CONV_FIND' });
        onClose.mockClear();
        docEsc.mockClear();
        const btn = screen.getByRole('button', { name }) as HTMLButtonElement;
        btn.focus();
        act(() => { fireEvent.keyDown(btn, { key: 'Escape' }); });
        expect(onClose).toHaveBeenCalledTimes(1);
        expect(getState().convFindOpen).toBe(false);
        // stopPropagation kept the Escape from bubbling to the document listener.
        expect(docEsc).not.toHaveBeenCalled();
      }
    } finally {
      document.removeEventListener('keydown', docListener);
    }
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
    expect(onTermsChange).toHaveBeenLastCalledWith('cache.db', false, false);
  });

  // --- #217 S4 / I-1.4 — regex + case toggles, persistence, a11y ---

  it('renders a regex (.*) toggle and a case (Aa) toggle with aria-pressed', () => {
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const rx = screen.getByRole('button', { name: /regular expression/i });
    const aa = screen.getByRole('button', { name: /case-sensitive/i });
    expect(rx.getAttribute('aria-pressed')).toBe('false');
    expect(aa.getAttribute('aria-pressed')).toBe('false');
    expect(rx.textContent).toContain('.*');
    expect(aa.textContent).toContain('Aa');
  });

  it('clicking the regex toggle flips aria-pressed, persists, and appends &regex=1', async () => {
    mockFind({ anchors: [{ uuid: 'u1', match_kinds: [] }], total: 1, mode: 'regex' });
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    const rx = screen.getByRole('button', { name: /regular expression/i });
    act(() => { fireEvent.click(rx); });
    expect(rx.getAttribute('aria-pressed')).toBe('true');
    expect(localStorage.getItem('cctally.conv.find.regex')).toBe('1');
    await typeNeedle(input, 'f.o');
    const url = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0] as string;
    expect(url).toContain('regex=1');
  });

  it('clicking the case toggle flips aria-pressed, persists, and appends &case=1', async () => {
    mockFind({ anchors: [{ uuid: 'u1', match_kinds: [] }], total: 1, mode: 'like' });
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    const aa = screen.getByRole('button', { name: /case-sensitive/i });
    act(() => { fireEvent.click(aa); });
    expect(aa.getAttribute('aria-pressed')).toBe('true');
    expect(localStorage.getItem('cctally.conv.find.case')).toBe('1');
    await typeNeedle(input, 'Foo');
    const url = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0] as string;
    expect(url).toContain('case=1');
  });

  it('seeds the toggle state from localStorage on mount', () => {
    localStorage.setItem('cctally.conv.find.regex', '1');
    localStorage.setItem('cctally.conv.find.case', '1');
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    expect(screen.getByRole('button', { name: /regular expression/i }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: /case-sensitive/i }).getAttribute('aria-pressed')).toBe('true');
  });

  it('reports the case flag up via onTermsChange (case-aware marks)', async () => {
    mockFind({ anchors: [], total: 0 });
    const onTermsChange = vi.fn();
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={onTermsChange} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    act(() => { fireEvent.click(screen.getByRole('button', { name: /case-sensitive/i })); });
    await typeNeedle(input, 'Foo');
    expect(onTermsChange).toHaveBeenLastCalledWith('Foo', true, false);
  });

  it('reports the regex source up (not "") when regex mode is on (#223)', async () => {
    // #223 supersedes S4 decision b — regex mode no longer reports '' to suppress
    // inline marks; it reports the source so the reader drives best-effort
    // highlighting. The LAST call is (needle, caseSensitive, true), needle !== ''.
    mockFind({ anchors: [{ uuid: 'u1', match_kinds: [] }], total: 1, mode: 'regex' });
    const onTermsChange = vi.fn();
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={onTermsChange} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    act(() => { fireEvent.click(screen.getByRole('button', { name: /regular expression/i })); });
    await typeNeedle(input, 'f.o');
    const last = onTermsChange.mock.calls.at(-1);
    expect(last?.[0]).toBe('f.o');
    expect(last?.[0]).not.toBe('');
    expect(last?.[2]).toBe(true);
  });

  it('renders the invalid-regex hint with role="alert"', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      { ok: false, status: 400, json: async () => ({}) } as Response);
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    act(() => { fireEvent.click(screen.getByRole('button', { name: /regular expression/i })); });
    await typeNeedle(input, '(');
    const alert = screen.getByRole('alert');
    expect(alert.textContent!.toLowerCase()).toContain('invalid regex');
  });

  // --- #217 S4 / I-1.4 — focus trap ---

  it('Tab from the last control (close) wraps to the input; Shift+Tab from the input wraps to close', () => {
    render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    const close = screen.getByRole('button', { name: /close find/i }) as HTMLButtonElement;
    // Tab from the close button wraps to the input.
    close.focus();
    act(() => { fireEvent.keyDown(close, { key: 'Tab' }); });
    expect(document.activeElement).toBe(input);
    // Shift+Tab from the input wraps to the close button.
    input.focus();
    act(() => { fireEvent.keyDown(input, { key: 'Tab', shiftKey: true }); });
    expect(document.activeElement).toBe(close);
  });

  // --- #217 S4 / I-1.6 — cursor preserved by uuid across a refresh ---

  it('preserves the selected match by uuid across a tail refresh; resets to 0 only when it vanishes', async () => {
    mockFind({ anchors: [{ uuid: 'a', match_kinds: [] }, { uuid: 'b', match_kinds: [] }, { uuid: 'c', match_kinds: [] }], total: 3 });
    const { rerender } = render(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} tailRevision={0} />);
    const input = screen.getByLabelText(/find in conversation/i) as HTMLInputElement;
    await typeNeedle(input, 'needle');
    // Select b (cursor 1).
    act(() => { fireEvent.click(screen.getByRole('button', { name: /next match/i })); });
    expect(document.querySelector('.conv-findbar-count')!.textContent).toContain('2 / 3');
    // A tail refresh appends d → [a,b,c,d]; cursor stays on b (2 / 4).
    mockFind({ anchors: [{ uuid: 'a', match_kinds: [] }, { uuid: 'b', match_kinds: [] }, { uuid: 'c', match_kinds: [] }, { uuid: 'd', match_kinds: [] }], total: 4 });
    rerender(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} tailRevision={1} />);
    await act(async () => { vi.advanceTimersByTime(300); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(document.querySelector('.conv-findbar-count')!.textContent).toContain('2 / 4');
    // Another refresh drops b → [a,c,d]; cursor resets to 0 (1 / 3).
    mockFind({ anchors: [{ uuid: 'a', match_kinds: [] }, { uuid: 'c', match_kinds: [] }, { uuid: 'd', match_kinds: [] }], total: 3 });
    rerender(<FindBar sessionId="s1" onClose={() => {}} onTermsChange={() => {}} tailRevision={2} />);
    await act(async () => { vi.advanceTimersByTime(300); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(document.querySelector('.conv-findbar-count')!.textContent).toContain('1 / 3');
  });
});
