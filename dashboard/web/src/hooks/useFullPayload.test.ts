import { renderHook, act } from '@testing-library/react';
import { afterEach, describe, it, expect, vi } from 'vitest';
import { useFullPayload } from './useFullPayload';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useFullPayload', () => {
  it('loads once and caches; no second fetch on a repeat load', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({ which: 'result', text: 'FULL' }) });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useFullPayload('s1', 't1', 'result'));
    expect(result.current.status).toBe('idle');
    await act(async () => {
      await result.current.load();
    });
    const loaded = result.current;
    expect(loaded.status).toBe('done');
    if (loaded.status === 'done' && loaded.data.which === 'result') {
      expect(loaded.data.text).toBe('FULL');
    }
    await act(async () => {
      await result.current.load();
    }); // cached, no 2nd fetch
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('coalesces two synchronous load() calls into a single fetch', async () => {
    // A user double-clicking the load-full affordance fires load() twice
    // SYNCHRONOUSLY (no await between). The async `state.status` guard can't see
    // the first call's 'loading' yet, so without a synchronous in-flight ref
    // both calls would fire a fetch. The ref must collapse them to one.
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({ which: 'result', text: 'FULL' }) });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useFullPayload('s1', 't1', 'result'));
    await act(async () => {
      // Two synchronous calls, both awaited together — the second must short-circuit.
      await Promise.all([result.current.load(), result.current.load()]);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe('done');
  });

  it('builds the route URL with encoded session/tool ids and the which discriminator', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({ which: 'input', input: {} }) });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useFullPayload('s/1', 'toolu_01', 'input'));
    await act(async () => {
      await result.current.load();
    });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe('/api/conversation/s%2F1/payload?tool_use_id=toolu_01&which=input');
  });

  it('surfaces 410 as a friendly "source no longer available" error', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 410, json: async () => ({}) });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useFullPayload('s1', 't1', 'result'));
    await act(async () => {
      await result.current.load();
    });
    const s = result.current;
    expect(s.status).toBe('error');
    if (s.status === 'error') expect(s.error).toBe('source no longer available');
  });

  it('surfaces a 403 as a generic unavailable error', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 403, json: async () => ({}) });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useFullPayload('s1', 't1', 'result'));
    await act(async () => {
      await result.current.load();
    });
    const s = result.current;
    expect(s.status).toBe('error');
    if (s.status === 'error') expect(s.error).toBe('unavailable');
  });

  it('surfaces a thrown fetch as a network error', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('boom'));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useFullPayload('s1', 't1', 'result'));
    await act(async () => {
      await result.current.load();
    });
    const s = result.current;
    expect(s.status).toBe('error');
    if (s.status === 'error') expect(s.error).toBe('network error');
  });

  it('no-ops (never fetches) when sessionId is null', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useFullPayload(null, 't1', 'result'));
    await act(async () => {
      await result.current.load();
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.status).toBe('idle');
  });
});
