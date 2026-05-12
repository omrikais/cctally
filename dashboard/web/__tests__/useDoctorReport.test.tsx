import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useDoctorReport } from '../src/hooks/useDoctorReport';

const SAMPLE_REPORT = {
  schema_version: 1,
  generated_at: '2026-05-13T10:00:00Z',
  cctally_version: '1.7.0',
  overall: { severity: 'warn', counts: { ok: 5, warn: 2, fail: 0 } },
  categories: [
    {
      id: 'install',
      title: 'Install',
      severity: 'warn',
      checks: [
        {
          id: 'install.path',
          title: 'PATH',
          severity: 'warn',
          summary: '~/.local/bin not on $PATH',
          remediation: 'Append the export line to your shell rc',
          details: {},
        },
      ],
    },
  ],
};

describe('useDoctorReport', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(SAMPLE_REPORT), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('initially has null report and no error', () => {
    const { result } = renderHook(() => useDoctorReport());
    expect(result.current.report).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('fetches and stores the report on refresh()', async () => {
    const { result } = renderHook(() => useDoctorReport());
    await act(async () => { await result.current.refresh(); });
    expect(result.current.report).toEqual(SAMPLE_REPORT);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/doctor');
  });

  it('reports HTTP error on non-2xx', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      new Response('boom', { status: 500 }),
    );
    const { result } = renderHook(() => useDoctorReport());
    await act(async () => { await result.current.refresh(); });
    expect(result.current.report).toBeNull();
    expect(result.current.error).toBe('HTTP 500');
  });

  it('reports network errors', async () => {
    vi.mocked(globalThis.fetch).mockRejectedValueOnce(new Error('network down'));
    const { result } = renderHook(() => useDoctorReport());
    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBe('network down');
  });

  it('sets loading true while in flight', async () => {
    let resolveFetch: (r: Response) => void = () => {};
    vi.mocked(globalThis.fetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => { resolveFetch = resolve; }),
    );
    const { result } = renderHook(() => useDoctorReport());
    let refreshPromise: Promise<void>;
    act(() => { refreshPromise = result.current.refresh(); });
    await waitFor(() => expect(result.current.loading).toBe(true));
    resolveFetch(new Response(JSON.stringify(SAMPLE_REPORT), { status: 200 }));
    await act(async () => { await refreshPromise; });
    expect(result.current.loading).toBe(false);
  });
});
