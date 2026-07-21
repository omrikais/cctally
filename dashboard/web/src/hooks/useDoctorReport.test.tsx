import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useDoctorReport, type DoctorReport } from './useDoctorReport';

const report: DoctorReport = {
  schema_version: 1,
  generated_at: '2026-07-21T12:00:00Z',
  cctally_version: '1.76.0',
  overall: { severity: 'ok', counts: { ok: 1, warn: 0, fail: 0 } },
  categories: [{
    id: 'data',
    title: 'Data',
    severity: 'ok',
    checks: [{
      id: 'data.statusline_pipeline',
      title: 'Statusline pipeline',
      severity: 'ok',
      summary: 'no recent regular-pool timer observed',
      details: {
        transport_age_seconds: null,
        selected_age_seconds: null,
      },
    }],
  }],
};

beforeEach(() => {
  globalThis.fetch = vi.fn();
});

describe('useDoctorReport', () => {
  it('accepts nullable detail values from the strict JSON response', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => report,
    } as Response);
    const { result } = renderHook(() => useDoctorReport());

    await act(async () => { await result.current.refresh(); });

    expect(result.current.error).toBeNull();
    expect(result.current.report?.categories[0].checks[0].details).toEqual({
      transport_age_seconds: null,
      selected_age_seconds: null,
    });
  });

  it('preserves the last usable report when a refresh fails', async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => report } as Response)
      .mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) } as Response);
    const { result } = renderHook(() => useDoctorReport());

    await act(async () => { await result.current.refresh(); });
    await act(async () => { await result.current.refresh(); });

    expect(result.current.report).toBe(report);
    expect(result.current.error).toBe('HTTP 503');
  });
});
