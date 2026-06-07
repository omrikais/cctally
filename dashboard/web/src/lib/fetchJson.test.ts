// dashboard/web/src/lib/fetchJson.test.ts
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchJson, HttpError, isAbortError } from './fetchJson';

function mock(body: unknown, ok = true, status = 200) {
  const fn = globalThis.fetch as ReturnType<typeof vi.fn>;
  fn.mockResolvedValueOnce({ ok, status, json: async () => body } as Response);
  return fn;
}

beforeEach(() => { globalThis.fetch = vi.fn(); });
afterEach(() => vi.restoreAllMocks());

describe('fetchJson', () => {
  it('resolves the parsed JSON body on ok', async () => {
    mock({ a: 1 });
    await expect(fetchJson<{ a: number }>('/x')).resolves.toEqual({ a: 1 });
  });

  it('throws HttpError carrying .status on a non-ok response', async () => {
    mock({}, false, 404);
    const err = await fetchJson('/missing').catch((e) => e);
    expect(err).toBeInstanceOf(HttpError);
    expect((err as HttpError).status).toBe(404);
  });

  it('passes the AbortSignal through to fetch', async () => {
    const fn = mock({});
    const ctl = new AbortController();
    await fetchJson('/x', ctl.signal);
    expect(fn.mock.calls[0][1]).toEqual({ signal: ctl.signal });
  });

  it('calls fetch without options when no signal is given', async () => {
    const fn = mock({});
    await fetchJson('/x');
    expect(fn.mock.calls[0][1]).toBeUndefined();
  });
});

describe('isAbortError', () => {
  it('is true for a DOMException-like with name AbortError', () => {
    expect(isAbortError(Object.assign(new Error('x'), { name: 'AbortError' }))).toBe(true);
  });
  it('is false for other errors and for null', () => {
    expect(isAbortError(new Error('x'))).toBe(false);
    expect(isAbortError(null)).toBe(false);
  });
});
