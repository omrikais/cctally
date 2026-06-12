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
