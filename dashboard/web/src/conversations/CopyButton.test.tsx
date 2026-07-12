import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CopyButton } from './CopyButton';
import { TranscriptContext } from './TranscriptContext';
import { __clearAnonPlanCache } from './anonScrub';

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

  it('mode OFF (no provider) copies raw, unchanged', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<CopyButton text="cwd /home/u/proj" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith('cwd /home/u/proj');
  });
});
