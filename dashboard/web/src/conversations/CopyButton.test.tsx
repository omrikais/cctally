import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CopyButton } from './CopyButton';
import { TranscriptContext } from './TranscriptContext';
import { __clearAnonPlanCache } from './anonScrub';
import { _resetForTests, dispatch, selectConvAnonRefusal } from '../store/store';

describe('CopyButton', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('copies via navigator.clipboard and shows then reverts the copied state', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<CopyButton text="hello" />);
    const btn = screen.getByRole('button', { name: 'Copy' });
    fireEvent.click(btn);
    expect(writeText).toHaveBeenCalledWith('hello');
    // writeText resolves on a microtask, not a timer; flush it under act so the
    // copied-state setState commits (fake timers freeze findBy's polling).
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument();
  });

  it('falls back to execCommand when navigator.clipboard is absent', () => {
    // @ts-expect-error force non-secure-context shape
    delete navigator.clipboard;
    const exec = vi.fn().mockReturnValue(true);
    document.execCommand = exec as typeof document.execCommand;
    render(<CopyButton text="x" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(exec).toHaveBeenCalledWith('copy');
  });

  it('stops propagation so an enclosing <details> does not toggle', () => {
    const onToggle = vi.fn();
    render(
      <details onClick={onToggle}>
        <summary>s</summary>
        <CopyButton text="x" />
      </details>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    // click did not bubble to the details onClick
    expect(onToggle).not.toHaveBeenCalled();
  });
});

// #281 S4 — per-card copy follows the reader's Anonymize mode, FAIL-CLOSED.
const WIRE = {
  tokens: [{ text: '/home/u/proj', replacement: 'project-1', bounded: false }],
  patterns: [],
};

function renderAnon(text: string, opts: { sessionId?: string; anonMode?: boolean } = {}) {
  const { sessionId = 's1', anonMode = true } = opts;
  return render(
    <TranscriptContext.Provider value={{ sessionId, anonMode }}>
      <CopyButton text={text} />
    </TranscriptContext.Provider>,
  );
}

describe('CopyButton anon mode (fail-closed)', () => {
  beforeEach(() => {
    __clearAnonPlanCache();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('scrubs the text via the current session anon-map before copying', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => WIRE });
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderAnon('cwd /home/u/proj here');
    fireEvent.click(screen.getByRole('button', { name: /copy \(anonymized\)/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('cwd project-1 here'));
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/conversation/s1/anon-map'),
    );
  });

  it('fail-closed on a fetch failure — clipboard untouched, error state shown', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 500 });
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderAnon('secret /home/u/proj');
    fireEvent.click(screen.getByRole('button', { name: /copy \(anonymized\)/i }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /copy failed/i })).toBeInTheDocument(),
    );
    expect(writeText).not.toHaveBeenCalled();
  });

  it('fail-closed on an invalid pattern — clipboard untouched', async () => {
    const bad = {
      tokens: [],
      patterns: [{ name: 'x', source: '([', ignoreCase: false, keepGroup1: false }],
    };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => bad });
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderAnon('anything');
    fireEvent.click(screen.getByRole('button', { name: /copy \(anonymized\)/i }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /copy failed/i })).toBeInTheDocument(),
    );
    expect(writeText).not.toHaveBeenCalled();
  });

  it('fail-closed on malformed wire data — clipboard untouched', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ nope: 1 }) });
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderAnon('anything');
    fireEvent.click(screen.getByRole('button', { name: /copy \(anonymized\)/i }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /copy failed/i })).toBeInTheDocument(),
    );
    expect(writeText).not.toHaveBeenCalled();
  });

  it('concurrent clicks share ONE anon-map fetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => WIRE });
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderAnon('cwd /home/u/proj');
    const btn = screen.getByRole('button', { name: /copy \(anonymized\)/i });
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('a session switch mid-flight discards the stale plan (no raw write)', async () => {
    let resolveFetch: () => void = () => {};
    const fetchMock = vi.fn().mockReturnValue(
      new Promise((r) => {
        resolveFetch = () => r({ ok: true, json: async () => WIRE });
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const { rerender } = renderAnon('cwd /home/u/proj', { sessionId: 's1' });
    fireEvent.click(screen.getByRole('button', { name: /copy \(anonymized\)/i }));
    // Switch to a different session BEFORE the s1 anon-map resolves.
    rerender(
      <TranscriptContext.Provider value={{ sessionId: 's2', anonMode: true }}>
        <CopyButton text="cwd /home/u/proj" />
      </TranscriptContext.Provider>,
    );
    resolveFetch();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(writeText).not.toHaveBeenCalled(); // stale s1 plan discarded
  });

  it('mode OFF (no provider) copies raw, unchanged', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<CopyButton text="cwd /home/u/proj" />);
    // Flushed inside `act` for the same reason as the refusal cases below:
    // `useCopy` commits its copied state on a microtask.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    });
    expect(writeText).toHaveBeenCalledWith('cwd /home/u/proj');
  });
});

// #850 §4.9 / A25 client half — the anonymized copy asks the server EVERY time.
//
// `fetchAnonPlan` used to keep a fulfilled plan for the life of the page, so a
// thread corrupted after one successful anonymized copy was never observed and
// M5 was false for that page: every later copy wrote the clipboard from the
// cached plan without a request. The cached promise is now evicted when it
// settles, so concurrent clicks during one flight still share one fetch and
// every later copy observes the server's current decision.
const REFUSAL_BODY = {
  status: 'anonymization_unavailable',
  undecodable_cwd_rows: 1,
  remedy: 'cctally cache-sync --source codex --rebuild',
};
const REFUSAL_SENTENCE = 'Anonymized copy is unavailable: 1 Codex project '
  + 'path(s) could not be read. Run cctally cache-sync --source codex --rebuild.';
const AMBIGUOUS_SENTENCE = 'Anonymized copy is unavailable: 2 Codex project path(s) '
  + 'cannot be safely attributed to this account. Use a raw export or omit the account scope.';
const CONV_A = { source: 'codex' as const, key: 'v1.conv-a' };

function renderConv(text: string, ref = CONV_A) {
  return render(
    <TranscriptContext.Provider value={{ sessionId: ref.key, conversationRef: ref, anonMode: true }}>
      <CopyButton text={text} />
    </TranscriptContext.Provider>,
  );
}

describe('CopyButton anonymized refusal (#850)', () => {
  // Every store mutation here runs inside `act(...)`. `CopyButton` subscribes
  // to the store through `useSyncExternalStore`, so a dispatch outside `act`
  // re-renders a mounted button while React is not expecting it, and React
  // reports it as an update that was not wrapped.
  beforeEach(() => {
    __clearAnonPlanCache();
    act(() => {
      _resetForTests();
      dispatch({ type: 'OPEN_CONVERSATION', conversationRef: CONV_A });
    });
  });
  afterEach(() => {
    act(() => { _resetForTests(); });
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('explains ambiguous account provenance without recommending a cache rebuild', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 409,
      json: async () => ({
        status: 'anonymization_unavailable',
        reason: 'ambiguous_account_provenance',
        ambiguous_cwd_rows: 2,
        remedy: 'use a raw export or omit the account scope',
      }),
    }));
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderConv('cwd /home/u/proj');

    fireEvent.click(screen.getByRole('button', { name: 'Copy (anonymized)' }));
    await waitFor(() => expect(screen.getByRole('button', { name: AMBIGUOUS_SENTENCE })).toBeInTheDocument());
    expect(writeText).not.toHaveBeenCalled();
  });

  it('a later copy re-requests, observes the 409, writes nothing and arms the record', async () => {
    const ok = { ok: true, status: 200, json: async () => WIRE };
    const refused = { ok: false, status: 409, json: async () => REFUSAL_BODY };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(ok)
      .mockResolvedValue(refused);
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderConv('cwd /home/u/proj');

    // Only one button renders; its accessible name cycles through
    // "Copy (anonymized)", "Copied (anonymized)" and the refusal sentence, so
    // the role alone is the stable locator.
    const btn = () => screen.getByRole('button');
    fireEvent.click(btn());
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('cwd project-1'));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    writeText.mockClear();
    fireEvent.click(btn());
    await waitFor(() =>
      expect(screen.getByRole('button', { name: REFUSAL_SENTENCE })).toBeInTheDocument(),
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(writeText).not.toHaveBeenCalled();
    expect(selectConvAnonRefusal()).toBe(REFUSAL_SENTENCE);
    expect(btn()).toHaveClass('conv-copy-btn-anon-unavailable');
    expect(btn()).toHaveTextContent('✕');

    // The control stays activatable: a further activation re-requests, a 409
    // keeps the record, and a 2xx clears it and the copy succeeds in place.
    fireEvent.click(btn());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(selectConvAnonRefusal()).toBe(REFUSAL_SENTENCE);

    fetchMock.mockResolvedValue(ok);
    fireEvent.click(btn());
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('cwd project-1'));
    expect(selectConvAnonRefusal()).toBeNull();
    expect(btn()).toHaveAttribute('aria-label', 'Copied (anonymized)');
    expect(btn()).not.toHaveClass('conv-copy-btn-anon-unavailable');
    expect(btn()).not.toHaveTextContent('✕');
  });

  it('the raw copy is unaffected by an armed refusal', async () => {
    act(() => {
      dispatch({
        type: 'SET_CONV_ANON_REFUSAL', conversationRef: CONV_A, refused: true,
        message: REFUSAL_SENTENCE,
      });
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(
      <TranscriptContext.Provider value={{ sessionId: CONV_A.key, conversationRef: CONV_A }}>
        <CopyButton text="cwd /home/u/proj" />
      </TranscriptContext.Provider>,
    );
    const btn = screen.getByRole('button', { name: 'Copy' });
    // `useCopy` commits its copied state on a microtask, so the click is
    // flushed inside `act` rather than left to settle after the assertion.
    await act(async () => { fireEvent.click(btn); });
    expect(writeText).toHaveBeenCalledWith('cwd /home/u/proj');
  });

  it('concurrent clicks during one flight still share ONE fetch', async () => {
    let settle: () => void = () => {};
    const fetchMock = vi.fn().mockReturnValue(
      new Promise((r) => {
        settle = () => r({ ok: true, status: 200, json: async () => WIRE });
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderConv('cwd /home/u/proj');
    const btn = screen.getByRole('button');
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    settle();
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
