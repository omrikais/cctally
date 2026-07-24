import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useSourceDetail } from './useSourceDetail';
import { _resetForTests } from '../store/store';
import type { CodexSessionDetailBody } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(impl: (url: string) => Promise<Response> | Response) {
  const fn = vi.fn((url: string) => Promise.resolve(impl(url)));
  vi.stubGlobal('fetch', fn);
  return fn;
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

describe('useSourceDetail (§5.6)', () => {
  it('fetches the qualified route and unwraps {source, resource, data}', async () => {
    const fetchFn = stubFetch(() =>
      jsonResponse(200, {
        source: 'codex',
        resource: 'session',
        data: { detail_kind: 'codex_session', key: 'session:codex-a', cost_usd: 6.4 },
      }),
    );
    const { result } = renderHook(() =>
      useSourceDetail<CodexSessionDetailBody>('codex', 'session', 'session:codex-a'),
    );
    await waitFor(() => expect(result.current.data).not.toBeNull());
    expect(fetchFn).toHaveBeenCalledWith('/api/source/codex/session/session%3Acodex-a');
    expect(result.current.data?.detail_kind).toBe('codex_session');
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('an all-mode row fetches under its OWNING provider source (never `all`)', async () => {
    const fetchFn = stubFetch(() =>
      jsonResponse(200, { source: 'codex', resource: 'project', data: { detail_kind: 'codex_project', key: 'project:codex-alpha' } }),
    );
    renderHook(() => useSourceDetail('codex', 'project', 'project:codex-alpha'));
    await waitFor(() => expect(fetchFn).toHaveBeenCalled());
    const calledUrl = fetchFn.mock.calls[0][0] as string;
    expect(calledUrl.startsWith('/api/source/codex/')).toBe(true);
    expect(calledUrl.includes('/all/')).toBe(false);
  });

  it('binds a qualified Claude project request to the selected project window', async () => {
    const fetchFn = stubFetch(() =>
      jsonResponse(200, {
        source: 'claude',
        resource: 'project',
        data: { detail_kind: 'claude_project', key: 'project:opaque' },
      }),
    );
    renderHook(() =>
      useSourceDetail('claude', 'project', 'project:opaque', { windowWeeks: 4 }),
    );
    await waitFor(() => expect(fetchFn).toHaveBeenCalled());
    expect(fetchFn).toHaveBeenCalledWith(
      '/api/source/claude/project/project%3Aopaque?weeks=4',
    );
  });

  it('#341 Task 4 — an account-scoped block fetch carries the ?account= qualifier', async () => {
    const fetchFn = stubFetch(() =>
      jsonResponse(200, { source: 'codex', resource: 'block', data: { detail_kind: 'codex_block', key: 'block:x' } }),
    );
    const acct = 'a'.repeat(32);
    renderHook(() => useSourceDetail('codex', 'block', 'block:x', { account: acct }));
    await waitFor(() => expect(fetchFn).toHaveBeenCalled());
    expect(fetchFn).toHaveBeenCalledWith(`/api/source/codex/block/block%3Ax?account=${acct}`);
  });

  it('#341 Task 4 — no account option keeps the account-agnostic route (byte-stable)', async () => {
    const fetchFn = stubFetch(() =>
      jsonResponse(200, { source: 'codex', resource: 'block', data: { detail_kind: 'codex_block', key: 'block:y' } }),
    );
    renderHook(() => useSourceDetail('codex', 'block', 'block:y'));
    await waitFor(() => expect(fetchFn).toHaveBeenCalled());
    expect(fetchFn).toHaveBeenCalledWith('/api/source/codex/block/block%3Ay');
  });

  it('a 400 capability error surfaces as the friendly capability variant', async () => {
    stubFetch(() => jsonResponse(400, { code: 'source_capability_unavailable', error: 'x' }));
    const { result } = renderHook(() => useSourceDetail('codex', 'block', 'block:x'));
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error).toEqual({ kind: 'capability', code: 'source_capability_unavailable' });
    expect(result.current.data).toBeNull();
  });

  it('a 404 not-found error surfaces as the friendly not-found variant', async () => {
    stubFetch(() => jsonResponse(404, { code: 'source_resource_not_found', error: 'x' }));
    const { result } = renderHook(() => useSourceDetail('claude', 'session', 'session:gone'));
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error).toEqual({ kind: 'not-found', code: 'source_resource_not_found' });
  });

  it('a null key clears state and issues no request', async () => {
    const fetchFn = stubFetch(() => jsonResponse(200, {}));
    const { result } = renderHook(() => useSourceDetail('codex', 'session', null));
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(fetchFn).not.toHaveBeenCalled();
  });
});
