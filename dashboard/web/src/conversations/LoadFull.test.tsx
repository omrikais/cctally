import { render, fireEvent, act } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LoadFull } from './LoadFull';
import { TranscriptContext } from './TranscriptContext';
import type { FullPayload } from '../types/conversation';

function renderAffordance(props: Partial<React.ComponentProps<typeof LoadFull>> = {}, sessionId = 's1') {
  const onLoaded = props.onLoaded ?? vi.fn();
  const utils = render(
    <TranscriptContext.Provider value={{ sessionId }}>
      <LoadFull toolUseId="t1" which="result" fullLength={null} label="load full output" onLoaded={onLoaded} {...props} />
    </TranscriptContext.Provider>,
  );
  return { ...utils, onLoaded };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('LoadFull', () => {
  it('renders a load button; clicking fetches and calls onLoaded with the payload', async () => {
    const payload: FullPayload = { which: 'result', tool_use_id: 't1', text: 'FULL', full_length: 5, truncated: false, is_error: false };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => payload });
    vi.stubGlobal('fetch', fetchMock);
    const { getByRole, onLoaded } = renderAffordance();
    const btn = getByRole('button', { name: /load full output/i });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onLoaded).toHaveBeenCalledWith(payload);
  });

  it('shows "showing X of Y" when fullLength is known', () => {
    const { container } = renderAffordance({ fullLength: 20000 });
    expect(container.textContent).toMatch(/of 20000/);
  });

  it('surfaces a friendly error on 410 source-gone', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 410, json: async () => ({}) });
    vi.stubGlobal('fetch', fetchMock);
    const { getByRole, container } = renderAffordance();
    await act(async () => {
      fireEvent.click(getByRole('button', { name: /load full output/i }));
    });
    expect(container.querySelector('.conv-loadfull-err')?.textContent).toMatch(/source no longer available/);
  });

  // #217 S3 E10#4 — a11y/disabled affordance (NOT a double-fetch fix; that is
  // already prevented by useFullPayload's inFlightRef/doneRef guards). When there
  // is no open session, load() no-ops, so the idle button must render `disabled`
  // rather than looking actionable.
  it('the idle button is disabled when there is no open session (load would no-op)', () => {
    const onLoaded = vi.fn();
    const { getByRole } = render(
      <TranscriptContext.Provider value={{ sessionId: null }}>
        <LoadFull toolUseId="t1" which="result" fullLength={null} label="load full output" onLoaded={onLoaded} />
      </TranscriptContext.Provider>,
    );
    const btn = getByRole('button', { name: /load full output/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it('the idle button is enabled when a session is open', () => {
    const { getByRole } = renderAffordance();
    const btn = getByRole('button', { name: /load full output/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  it('renders a reduced-motion-safe spinner element while loading', async () => {
    let resolveFetch: (v: unknown) => void = () => {};
    const fetchMock = vi.fn().mockReturnValue(new Promise((res) => { resolveFetch = res; }));
    vi.stubGlobal('fetch', fetchMock);
    const { getByRole, container } = renderAffordance();
    await act(async () => {
      fireEvent.click(getByRole('button', { name: /load full output/i }));
    });
    expect(container.querySelector('.conv-loadfull-spinner')).toBeTruthy();
    await act(async () => {
      resolveFetch({ ok: true, status: 200, json: async () => ({ which: 'result', tool_use_id: 't1', text: 'x', full_length: 1, truncated: false, is_error: false }) });
    });
  });
});
